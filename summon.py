"""On-demand review: `@vera review` in a PR comment (issue #28, slice 1).

Every review this machinery has ever run was triggered by a push-shaped event or the
sweep's backfill. There was no way for a human to *ask* for one — so the cheapest way to
ask a question about a PR was to alter the artifact you were asking about.

The gap has teeth. On protoAgent#2138 the panel posted a hallucinated blocker twice; the
operator refuted it on the thread with a blob citation and a CI-green test, and **nothing
consumed either** — a refutation had no path back into the panel, and the only exit was an
adjudicated merge past a standing block.

Shape borrowed from CodeRabbit: one handle, a small deterministic verb set, and `help` so
the surface is discoverable. Slice 1 is `review` and `help`; `pause`/`resume` and inline
thread chat follow.

Two rules that are not negotiable:

**Admin only, resolved SERVER-SIDE.** The comment payload carries `author_association`,
and it is exactly the kind of caller-supplied field this plugin refuses to trust anywhere
else (refs come from `gh`, never from a model or a hook body). Permission is read back
from GitHub. The reason is cost, not mischief: a summon spends a full panel — five
subagents, 5–9 minutes — and anyone who can comment on a public PR could otherwise spend
it in a loop.

**A refused or dropped summon always answers.** Silence from a bot you just addressed
reads as broken, and the operator's next move is to repeat the command. Every path here
produces a reply or a typed outcome the caller can see.
"""

from __future__ import annotations

import re

# Slice 1. `pause`/`resume` need somewhere durable to live (the marker line is the
# natural home — GitHub is already the store, ADR 0078 D5) and land next.
VERBS = ("review", "pause", "resume", "help")

# Webhook events a summon needs. `issue_comment` carries top-level `@vera <verb>`;
# `pull_request_review_comment` carries inline thread replies (the refutation channel).
# A GitHub App subscribed only to `pull_request` — as it was when this shipped — makes
# every summon vanish with no error: correct code, no event. `/api/plugins/pr-reviewer/
# summon/health` reports the gap rather than leaving it to be discovered as "broken".
_APP_EVENTS = ("issue_comment", "pull_request_review_comment")


def required_app_events() -> list[str]:
    return list(_APP_EVENTS)


# `pause` state lives in a marker on a posted comment — GitHub is already the store
# (ADR 0078 D5), so there is no local DB to drift and a container restart cannot forget
# that someone asked for quiet. Read the same way verdict markers are.
PAUSE_MARKER = "<!-- protoagent-qa-paused -->"
RESUME_MARKER = "<!-- protoagent-qa-resumed -->"

REFUSED_NOT_ADMIN = "summon:refused-not-admin"
REFUSED_UNKNOWN_VERB = "summon:unknown-verb"
NOT_A_SUMMON = "summon:not-addressed"

# `(?!\w)` rather than `\b`: a bot login ends in `]` (`qa-bot[bot]`), and there is no
# word boundary between `]` and a space — so `\b` silently never matched the handle the
# reviewer actually posts under. The lookahead still rejects `@verax` for handle `vera`.
_MENTION_TMPL = r"(?:^|\s)@{handle}(?!\w)[ \t]*(?P<verb>[a-zA-Z][a-zA-Z-]*)?"


def parse_command(body: str, handles: list[str]) -> str | None:
    """The verb addressed to us, `"help"` for a bare mention, or None if not addressed.

    Deliberately literal: only a real `@handle` mention counts, and only the word that
    immediately follows it. A PR body that *discusses* the reviewer ("vera flagged this")
    must never trigger a panel run — the trigger has to be something a human typed on
    purpose.
    """
    text = str(body or "")
    # Ignore quoted lines: a reply that quotes an earlier summon must not re-fire it.
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))
    for handle in handles:
        if not handle:
            continue
        m = re.search(_MENTION_TMPL.format(handle=re.escape(handle)), text, re.IGNORECASE)
        if not m:
            continue
        verb = (m.group("verb") or "").lower()
        if not verb:
            return "help"  # a bare mention is someone asking what this thing does
        return verb if verb in VERBS else verb  # unknown verbs answer with help, not silence
    return None


async def is_admin(run_gh, repo: str, login: str) -> bool:
    """Repo-admin, read back from GitHub — never from the webhook payload.

    Fails CLOSED: an unreadable permission is not an admin. The blast radius of a wrong
    `True` is an attacker spending our panel budget at will; the blast radius of a wrong
    `False` is an operator retrying a command.
    """
    if not login or "/" not in repo:
        return False
    rc, out, _err = await run_gh(
        ["api", f"repos/{repo}/collaborators/{login}/permission", "--jq", ".permission"], timeout=20
    )
    return rc == 0 and out.strip().lower() == "admin"


def is_paused(comment_bodies: list[str]) -> bool:
    """Is automated review paused for this PR? The LAST pause/resume marker wins.

    Ordering matters more than counting: `pause` then `resume` then `pause` must end
    paused, so this reads the newest marker rather than tallying. Bodies arrive
    oldest-first, as the GitHub comments API returns them.
    """
    state = False
    for body in comment_bodies or []:
        text = str(body or "")
        if PAUSE_MARKER in text:
            state = True
        elif RESUME_MARKER in text:
            state = False
    return state


def pause_text(login: str) -> str:
    return (
        f"{PAUSE_MARKER}\n**Automated review paused** for this PR at @{login}'s request.\n\n"
        f"Pushes will not trigger the panel. `@vera resume` restores it; `@vera review` still "
        f"runs a one-off review without resuming.\n\n"
        f"_A PR mid-rework does not need six rounds (issue #23) — this is the human-controlled "
        f"version of that._"
    )


def resume_text(login: str) -> str:
    return (
        f"{RESUME_MARKER}\n**Automated review resumed** for this PR at @{login}'s request. "
        f"The next push will be reviewed as normal."
    )


def help_text(handles: list[str]) -> str:
    handle = next((h for h in handles if h), "vera")
    return (
        f"**QA panel — on-demand commands** (repo admins only)\n\n"
        f"| command | effect |\n|---|---|\n"
        f"| `@{handle} review` | Re-review the current head now — including a head already "
        f"reviewed, which is the point when you think a verdict was wrong |\n"
        f"| `@{handle} pause` | Stop reviewing this PR on push (a one-off `review` still works) |\n"
        f"| `@{handle} resume` | Resume automated review |\n"
        f"| `@{handle} help` | This message |\n\n"
        f"A summon spends a full panel (five finders, ~5–9 min), which is why it is "
        f"admin-gated. Inline thread replies are not built yet (pr-reviewer-plugin#28).\n\n"
        f"**Disputing a FAIL on an unchanged head:** post the evidence as a PR comment (the PR "
        f"author or a maintainer), then `@{handle} review`. The re-review reads it as claims to "
        f"verify, not instructions. The FAIL is replaced only if that round, complete and "
        f"verified, refutes EVERY blocking finding with evidence and the verifier agrees at "
        f"this head; otherwise it keeps gating until a new commit or a maintainer merges past it."
    )


def outcome_reply(login: str, outcome: str) -> str | None:
    """The reply to a summon that did not post a verdict of its own, or None when the
    outcome speaks for itself (a posted review is the answer).

    Before issue #217 every `drop:` answered "<reason>: nothing ran. Try again once the
    current review finishes." — which is wrong for most reasons. A draft PR has no review to
    wait for; and on mythxengine-sdk#409 the operator read "in-flight" as "a round is stuck"
    when the standing answer was a FAIL from nine minutes earlier. Each outcome says what
    actually happened and what to do next.
    """
    kind, _, detail = str(outcome or "").partition(":")
    if kind == "reaffirmed":
        # A summon re-runs the panel (it bypasses the reaffirm), so this is defensive —
        # but if it ever happens the honest answer is "verdict unchanged", not "nothing ran".
        return (
            f"@{login} — the panel's verdict for this head is unchanged: **{detail}** "
            f"(reaffirmed; no new panel ran, the head has not changed)."
        )
    if kind != "drop":
        return None
    if detail == "in-flight":
        return (
            f"@{login} — a review round for this PR is already running, so this summon did not "
            f"start a second panel on top of it. Its verdict posts here when it finishes; if you "
            f"still dispute that verdict, summon again then."
        )
    if detail == "pr-not-eligible":
        return (
            f"@{login} — this PR can't be reviewed right now (draft, closed or locked), so "
            f"nothing ran. Mark it ready for review (or reopen / unlock it), then summon again."
        )
    return f"@{login} — `{detail}`: nothing ran."


def refusal_text(login: str, verb: str) -> str:
    return (
        f"@{login} — `{verb}` needs **admin** permission on this repository. A summon runs "
        f"the full panel (five finders, ~5–9 minutes), so it is gated on write-plus.\n\n"
        f"_Not silence: you addressed the reviewer and it is answering (pr-reviewer-plugin#28)._"
    )

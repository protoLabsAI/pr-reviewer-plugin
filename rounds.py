"""Re-review convergence: round history, prior-request memory, delta scoping (issue #23).

A re-review used to be memoryless in the way that matters. The dispatcher recalled the
PREVIOUS review's findings, and the panel then re-read the WHOLE PR diff — so every
delta the review itself caused became fresh review surface, and code the panel had
already blessed came back around on a later draw. projectBoard-plugin#88 ran eight
rounds on a small store fix: round 6 flagged the normalization round 3 demanded, and
round 8 flagged CLI-argument duplication untouched since round 1. Every finding was
individually confirmed and individually reasonable; collectively they never converged.

Three pieces, all pure — the dispatcher does the GitHub reads and passes facts in:

  `panel_rounds`          the PR's review history as ROUNDS. Promotion bodies carry our
                          marker but no findings, and a re-gate re-posts an existing
                          verdict body verbatim — neither is a round, and both used to
                          shadow the real one.
  `render_prior_requests` every prior round's findings as one wrapped data block, so a
                          finder can see that a line it is about to flag exists BECAUSE
                          the panel asked for it. A panel-requested change is verified
                          as implemented-correctly, not re-litigated as novel.
  `converge`              the exit rule. From round N, a WARN whose findings are all
                          minor/nit AND all anchored to lines that moved since the last
                          reviewed head becomes PASS-with-notes: the notes still post,
                          they just stop holding the verdict.

Convergence relief is fail-CLOSED, matching the rest of this plugin: an unreadable
compare, an uncertain major, a finding outside the delta — any of them and the WARN
stands. The rule only ever releases a verdict the panel already judged non-blocking;
a blocker/major FAIL converges never, however many rounds it takes (a defect a fix
introduced is still a defect — see #88 rounds 4 and 7).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re

from .grounding import quoted_snippets
from .verdicts import PASS, WARN, fenced_blocks, read_findings_record

# From this round on, the convergence rule is eligible to fire. Rounds 1–2 are the
# review doing its job; #88's loop only became self-referential at round 3+.
DEFAULT_CONVERGENCE_ROUNDS = 3

# A fix rarely lands on exactly the flagged line — the hunk that answers a finding
# drifts by a few lines as code moves. Padding the delta ranges keeps the rule from
# failing on an off-by-three.
DELTA_CONTEXT_LINES = 5

MAX_REQUESTS_PER_ROUND = 20
MAX_CLAIM_CHARS = 300

_WRAPPER_TAGS = ("prior_requests", "round", "request")
_CLOSING_TAG_RE = re.compile(r"</\s*(" + "|".join(_WRAPPER_TAGS) + r")\s*>", re.IGNORECASE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)

_RELIEVABLE = ("minor", "nit")


def diff_identity(merge_base_tree: str | None, head_tree: str | None) -> str | None:
    """Deterministic identity of a PR's review-relevant base↔head comparison, or None.

    A three-dot PR diff is a pure function of exactly two trees — the merge-base tree and
    the head tree — so the identity folds BOTH Git Merkle roots, NOT the changed-file
    patch list. That distinction is the whole correctness of reaffirming a verdict across
    a changed head SHA (issue #91):

      - It captures the WHOLE reviewed head context. A rebase that pulls a changed
        dependency in through the base rewrites the head tree, so the identity moves with
        it. The earlier changed-file-only hash missed exactly this — it reaffirmed a
        verdict produced against the old base even though the code at head had changed.
      - It is not bounded by GitHub's 3,000-file `/pulls/{n}/files` cap. A tree SHA is one
        hash over the entire tree however large, so a change in a file past that cap can
        never be silently omitted from the identity.
      - It is content-addressed, so it is STABLE across a reworded commit or a
        moved-but-identical-content base (same bytes ⇒ same tree root, new commit SHA) —
        which is the case this optimization exists to reuse.

    Fails CLOSED: either tree missing (an unreadable or ambiguous read) ⇒ None, and the
    caller declines to reaffirm and runs the normal review.
    """
    if not merge_base_tree or not head_tree:
        return None
    return hashlib.sha256(f"{merge_base_tree}\n{head_tree}".encode()).hexdigest()


def _escape(text: str) -> str:
    """Neutralize wrapper closing tags (whitespace-tolerant), same discipline as
    `threads._escape`: finding claims quote diff text, which anyone who can open a
    PR writes — a claim must never terminate the data block early."""
    return _CLOSING_TAG_RE.sub(lambda m: f"</{m.group(1).lower()}_>", text)


# An incomplete round is free against the cap only up to this multiple of it. The cap is
# a flood guard, and a flood of pushes whose panels keep failing is still a flood.
ROUND_CAP_CEILING = 2


def round_cap_reached(history: list[dict], max_rounds: int) -> bool:
    """Has this PR spent its push-triggered review budget? `max_rounds` 0 ⇒ never.

    The budget counts COMPLETE rounds. A round that lost a lane (a finder that died, the
    structural pass unavailable) is the panel's failure, not the author's push: it posts a
    coverage-capped verdict the gate will not promote, so the author has to push again to
    get a real one. Counting those locked PRs out of review on the panel's own flakiness —
    mythxengine#830 had a complete PASS, then the push fixing a human reviewer's findings
    was capped because incomplete rounds had eaten the budget (issue #130).

    Every round still counts toward a hard ceiling of `ROUND_CAP_CEILING` × the cap, so
    an unbounded run of incomplete rounds cannot spend the panel forever.
    """
    if not max_rounds:
        return False
    complete = sum(1 for r in history if r.get("complete", True))
    return complete >= max_rounds or len(history) >= max_rounds * ROUND_CAP_CEILING


def panel_rounds(reviews: list[dict]) -> list[dict]:
    """Our posted reviews → the ROUNDS the panel actually spent, oldest→newest.

    `[{head, verdict, findings: [...]}]`, one entry per reviewed head. Two kinds of
    marker-bearing review are NOT rounds and must not be counted or recalled from:

      - promotions (`promoted=true`) hold an approval line, no findings. Taking the
        newest marker-bearing review as "the prior review" (the old behaviour) meant
        that after any approve-on-green, the next round recalled a body with no
        findings JSON at all — `prior_findings` came through EMPTY and the delta
        re-review silently degraded to a cold first review. On #88 that hit rounds
        4, 6, 7 and 8, which is most of the loop.
      - a re-gate re-posts an earlier verdict body verbatim to arm the block, so the
        same head appears twice with identical findings; deduping by head (keeping
        the latest) stops one head inflating the round count.
    """
    by_head: dict[str, dict] = {}
    for review in reviews or []:
        if not isinstance(review, dict) or review.get("promoted"):
            continue
        head = str(review.get("head") or "")
        if not head:
            continue
        # From the body's findings RECORD only, never "the last fenced array anywhere":
        # claim text printed after the record can hold an array of its own.
        findings, recorded = read_findings_record(str(review.get("body") or ""))
        by_head[head] = {
            "head": head,
            "verdict": str(review.get("verdict") or ""),
            "findings": findings,
            # Carried from the marker so the promotion gate can refuse a clean verdict
            # that was produced over incomplete coverage (#49). Absent ⇒ complete.
            "complete": bool(review.get("complete", True)),
            # Did the body carry exactly ONE well-formed findings record? An explicit `[]`
            # (the panel looked and raised nothing) and an absent, malformed or ambiguous
            # record can all come out as `findings == []`, and the promotion gate must tell
            # them apart: only the former proves an incomplete round's WARN is the coverage
            # cap and nothing more (`dispatch.coverage_only_round`). Otherwise False ⇒ fails
            # closed.
            "findings_recorded": recorded,
            # The base↔head diff identity this round reviewed (issue #91), so a later
            # rebased head with a byte-identical diff can reaffirm this verdict without
            # re-spending the panel. Absent (older bodies) ⇒ None ⇒ reaffirm fails closed.
            "diff_id": review.get("diff_id") or None,
            # The head this verdict was carried from by an identical-diff reaffirm (issue
            # #135), or "". Such an entry is a VERDICT for its head — the gate reads it —
            # but no panel ran, so it is not a round: see `spent_rounds`.
            "reaffirmed": str(review.get("reaffirmed") or ""),
            "verified": bool(review.get("verified", True)),
            # GitHub's review id, which it assigns monotonically — the one ORDER signal a
            # round carries (#170: "did a verified round come after this one?"). Absent or
            # unreadable ⇒ 0, which no later round is older than.
            "id": _review_id(review.get("id")),
            # What this round did with the previous round's blocker/majors, from the marker's
            # `disp=` record (#234). None ⇒ absent or unreadable ⇒ it supersedes nothing.
            "disposed": decode_disposition_record(review.get("disp")),
        }
    return list(by_head.values())


def _review_id(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# One automatic re-run per head for a round whose verifier flaked (#220): the same fresh
# round a manual `@vera review` buys, without waiting for a human to notice.
VERIFY_RETRIES_PER_HEAD = 1
VERIFY_RETRY_DUE = "retry"
VERIFY_RETRY_EXHAUSTED = "exhausted"


def verify_retry_state(reviews: list[dict], head: str) -> str:
    """Should the sweep re-run the panel on `head` because its verdict is unverified? (#220)

    `reviews` is our posted reviews, raw — NOT folded by `panel_rounds`, which keeps one
    round per head and so cannot count how often this head's verifier failed. Promotions and
    reaffirmed verdicts are not rounds and do not count.

      ""            no unverified round at this head, or a VERIFIED round came after the
                    newest unverified one (that lifts the hold, #170) — nothing to do.
      "retry"       unverified rounds ≤ `VERIFY_RETRIES_PER_HEAD`: schedule a fresh round.
      "exhausted"   the automatic re-run was spent and came back unverified too — stop,
                    and tell a human.

    Counted from GitHub, so the bound survives a restart: a crash-looping container cannot
    turn one flaky verifier into a panel per sweep tick."""
    rounds = [
        r
        for rev in reviews or []
        if isinstance(rev, dict) and not rev.get("promoted") and str(rev.get("head") or "") == head
        for r in panel_rounds([rev])
        if not r.get("reaffirmed")
    ]
    unverified = [r for r in rounds if not r.get("verified", True)]
    if not unverified:
        return ""
    newest = max(r.get("id", 0) for r in unverified)
    if any(r.get("verified", True) and r.get("id", 0) > newest for r in rounds):
        return ""
    return VERIFY_RETRY_DUE if len(unverified) <= VERIFY_RETRIES_PER_HEAD else VERIFY_RETRY_EXHAUSTED


def spent_rounds(history: list[dict]) -> list[dict]:
    """The rounds a panel was actually SPENT on — `history` without reaffirmed verdicts.

    The round machinery counts and recalls these: the round number, the push-triggered
    cap, the convergence threshold, the request history, the prior round a delta review
    builds on. A verdict carried to a rebased head is none of those — counting it would
    let three rebases of a finished PR exhaust its review budget.
    """
    return [r for r in history or [] if not r.get("reaffirmed")]


# A `<request>`'s `status` — what became of it, so history is not mistaken for open debt.
REQUEST_OPEN = "open"  # the latest round with findings still carries it
REQUEST_REFUTED = "refuted"  # this panel's own verifier refuted it in that round
REQUEST_NOT_IN_LATEST = "not-in-latest-round"  # raised once, no longer carried


def render_prior_requests(rounds: list[dict]) -> str:
    """The `<prior_requests>` data block: what THIS panel has already asked for.

    Distinct from `prior_findings` (the previous round's open items, for drop/carry
    triage). This is the whole history, round-numbered, and it exists to answer a
    different question — "did we ask for this?" A finder that can see round 3 demanded
    the normalization does not report it as an unrequested behavioral change in round 6.
    """
    numbered = [(i + 1, r) for i, r in enumerate(rounds or []) if isinstance(r, dict) and r.get("findings")]
    if not numbered:
        return ""
    # What became of each request (issue #131). The block is the WHOLE history, and without
    # this an item the verifier refuted three rounds ago, or one fixed and dropped since,
    # reads exactly like an open one — so a brief listed a fixed, refuted note among
    # "standing items from round 1" (mythxengine#827). "Open" is what the LATEST round that
    # RECORDED findings still carries: an unaccounted blocker/major is re-recorded every
    # round (`merge_carried_findings`), so one that is absent there was positively cleared.
    #
    # "Recorded" includes an explicit empty record (issue #204). The latest round used to be
    # the latest with a NON-EMPTY record, which made a clean round invisible to the request
    # history: on terminal-plugin#10 a minor was re-listed (unverified, paraphrased) in the
    # round after the fix, then two rounds recorded `[]` — the panel looked and raised
    # nothing — yet the next round still read the request as `open` off the re-listing,
    # raised it again, and held the PR. A round that recorded `[]` speaks for every request
    # before it; only a round with NO readable record (`findings_recorded` absent or false,
    # e.g. a malformed body) is skipped, fail-closed as before. Coverage of that clean round
    # is not consulted here: an incomplete round already holds the gate on its own, and this
    # block is the panel's memory, not its gate.
    latest_round = next(
        (
            r
            for r in reversed(rounds or [])
            if isinstance(r, dict) and (r.get("findings") or r.get("findings_recorded") is True)
        ),
        numbered[-1][1],
    )
    latest = [
        f
        for f in (latest_round.get("findings") or [])
        if isinstance(f, dict) and str(f.get("verdict") or "").lower() != "refuted"
    ]
    out = ["<prior_requests>"]
    for number, round_ in numbered:
        out.append(f'  <round number="{number}" verdict="{_attr(round_.get("verdict"))}">')
        for finding in round_["findings"][:MAX_REQUESTS_PER_ROUND]:
            if isinstance(finding, dict) and finding.get("nearby"):
                continue  # a nearby note (#232) was never a request of this PR
            if str(finding.get("verdict") or "").lower() == "refuted":
                status = REQUEST_REFUTED
            elif any(same_prior(finding, f) for f in latest):  # a drifted anchor is the same request (#260)
                status = REQUEST_OPEN
            else:
                status = REQUEST_NOT_IN_LATEST
            severity = _attr(finding.get("severity"))
            location = str(finding.get("file") or "")
            line = finding.get("line")
            if isinstance(line, int):
                location = f"{location}:{line}"
            claim = str(finding.get("claim") or "")[:MAX_CLAIM_CHARS]
            out.append(f'    <request severity="{severity}" location="{_attr(location)}" status="{status}">')
            out.append(_escape(claim))
            out.append("    </request>")
        out.append("  </round>")
    out.append("</prior_requests>")
    return "\n".join(out)


def _attr(value: object) -> str:
    return str(value or "").replace('"', "&quot;").replace(">", "&gt;").replace("<", "&lt;")


def delta_ranges(compare_files: list[dict]) -> dict[str, list[tuple[int, int]]]:
    """Changed line ranges per file from a compare payload's `patch` hunks.

    Ranges are in HEAD-side line numbers (the side a finding cites), padded by
    `DELTA_CONTEXT_LINES`. A file present with no readable patch (binary, or GitHub
    truncated it) maps to `[]` — known-changed, unknown where; `in_delta` treats that
    as whole-file, since the alternative is refusing relief on a file we know moved.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    for entry in compare_files or []:
        if not isinstance(entry, dict):
            continue
        path = _norm(str(entry.get("filename") or ""))
        if not path:
            continue
        spans = ranges.setdefault(path, [])
        for match in _HUNK_RE.finditer(str(entry.get("patch") or "")):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count <= 0:  # a pure deletion hunk — the surrounding lines are the delta
                spans.append((max(1, start - DELTA_CONTEXT_LINES), start + DELTA_CONTEXT_LINES))
                continue
            spans.append((max(1, start - DELTA_CONTEXT_LINES), start + count - 1 + DELTA_CONTEXT_LINES))
    return ranges


def _norm(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path.removeprefix("/")


def in_delta(finding: dict, ranges: dict[str, list[tuple[int, int]]]) -> bool:
    """Does this finding anchor to code that moved since the last reviewed head?

    A file that isn't in the delta at all is untouched code — round 8's finding on
    argument construction unchanged since round 1 lands here, and blocks convergence,
    which is right: that one is about the PR, not about the review's own churn.
    """
    spans = ranges.get(_norm(str(finding.get("file") or "")))
    if spans is None:
        return False
    if not spans:  # changed file, unreadable patch — whole-file
        return True
    line = finding.get("line")
    # `line: 0` is the findings contract's "no particular line", not line zero — panels
    # emit it for file-level findings (seen on protoAgent#2139's CHANGELOG finding).
    # Treating it as a real line tested it against hunk ranges that start at 1, so it
    # could never be in-delta and silently blocked convergence forever.
    if not isinstance(line, int) or line <= 0:  # file-level finding on a changed file
        return True
    return any(start <= line <= end for start, end in spans)


def converge(
    verdict: str,
    findings: list[dict],
    *,
    round_number: int,
    ranges: dict[str, list[tuple[int, int]]] | None,
    threshold: int = DEFAULT_CONVERGENCE_ROUNDS,
) -> tuple[str, list[dict], str]:
    """(verdict, notes, reason). The exit rule — pure, like `verdict_for`.

    `ranges=None` means the compare was unreadable: no relief (an unreadable delta must
    never launder a WARN into a PASS, the same posture `confine_findings` takes with an
    unreadable file list). `threshold=0` disables the rule entirely.

    On relief the findings come back as `notes`: they still render, still post, still
    get read — they just stop being verdict-bearing, which is the whole point. Nothing
    is dropped or hidden.
    """
    if threshold <= 0:
        return verdict, [], "disabled"
    if verdict != WARN:
        # PASS needs no relief; FAIL is a blocker/major and never converges.
        return verdict, [], "not-warn"
    if round_number < threshold:
        return verdict, [], f"round-{round_number}-below-{threshold}"
    if not findings:
        return verdict, [], "no-findings"
    if ranges is None:
        return verdict, [], "delta-unreadable"
    for finding in findings:
        if str(finding.get("severity") or "").lower() not in _RELIEVABLE:
            # An "uncertain" major also lands on WARN — it is not a nit, and a round
            # budget must not retire it.
            return verdict, [], "non-minor-finding"
        if not in_delta(finding, ranges):
            return verdict, [], "finding-outside-delta"
    return PASS, list(findings), f"converged-round-{round_number}"


def unexplained_clearance(
    history: list[dict],
    verdict: str,
    findings: list[dict],
    *,
    corroborate: int = 2,
) -> dict | None:
    """The prior blocker/major this clean PASS silently dropped, or None (issue #26).

    A zero-finding PASS is the highest-consequence transition this machinery has: it
    dismisses our standing REQUEST_CHANGES and clears the promotion path. On
    protoAgent#2141 the panel confirmed a major on one head and returned PASS with zero
    findings on the next — the code unchanged — which lifted the block and the defect
    merged 44 seconds later.

    A miss cannot be caught the way a hallucination can: there is no claim to re-ground,
    and `findings=0` reads identically whether the code is clean or nobody looked. So
    the rule is structural rather than evidential — an unexplained *disappearance* of a
    blocker/major is treated as unproven, not as a clearance.

    `corroborate` is the escape hatch that stops this wedging a PR forever: the FIRST
    clean PASS after a blocker/major holds the block, a SECOND consecutive one lifts it.
    Two independent draws finding nothing is evidence; one is a coin flip.
    """
    if verdict != PASS or findings:
        return None  # only a *clean* PASS can silently drop a finding
    clean_runs = 1  # this round
    for round_ in reversed(history or []):
        prior = [f for f in (round_.get("findings") or []) if isinstance(f, dict)]
        if str(round_.get("verdict") or "") == PASS and not prior:
            clean_runs += 1
            continue
        for finding in prior:
            if finding.get("nearby"):
                continue  # a nearby note never gated (#232); its absence clears nothing
            severity = str(finding.get("severity") or "").lower()
            if severity in ("blocker", "major") and str(finding.get("verdict") or "").lower() != "refuted":
                if clean_runs >= corroborate:
                    return None  # corroborated by repeat draws — let the block lift
                return dict(finding)
        return None  # the last substantive round carried nothing gating
    return None


_VALID_DISPOSITIONS = ("fixed", "open", "refuted")


def parse_dispositions(output: str) -> list[dict]:
    """The report pass's `prior_dispositions` block: what happened to each prior finding.

    #27 could only ask "did a blocker/major vanish into a CLEAN pass?" — it had nothing
    to distinguish *fixed* from *forgotten*, so it could only guard the one verdict where
    silence is unambiguous. This block is the panel stating, per prior finding, which it
    was. That turns the guard from a heuristic about zero findings into a contract:
    a blocker/major must be dispositioned, whatever the new verdict is.

    Absent block ⇒ empty list ⇒ the caller falls back to #27's narrower rule. A recipe
    that doesn't emit dispositions must not become *less* guarded than before.

    Reads the LAST such block, not the first (protoAgent#2439). When the serving lane
    leaves deliberation in `content`, a model that drafts a dispositions block mid-thought
    and then revises it emits two — and a discarded draft must never outrank the decision
    it was discarded for. This guard decides whether a prior blocker stays blocking, so
    "which block did we read" is a correctness question, not a formatting one.
    """
    # `fenced_blocks`: a fence closes at a line start, never at a ``` inside a JSON string.
    arrays = [b.strip() for b in fenced_blocks(output, json_only=True) if b.strip().startswith("[")]
    for block in reversed(arrays):
        try:
            parsed = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        rows = [
            r
            for r in parsed
            if isinstance(r, dict)
            and str(r.get("disposition") or "").lower() in _VALID_DISPOSITIONS
            and (r.get("prior") or r.get("file"))
        ]
        if rows:  # the LAST block shaped like dispositions; findings arrays have no
            return rows  # `disposition` key, so they can never match
    return []


def _anchor(file: object, line: object) -> str:
    path = _norm(str(file or ""))
    return f"{path}:{line}" if isinstance(line, int) else path


def _disposition_anchor(row: dict) -> tuple[str, int | None]:
    """(file, line) a disposition points at — from `file`/`line`, or a `prior` "path:line"."""
    file = row.get("file")
    line = row.get("line")
    if not file and row.get("prior"):
        prior = str(row["prior"])
        head, _, tail = prior.rpartition(":")
        if head and tail.isdigit():
            file, line = head, int(tail)
        else:
            file = prior
    return _norm(str(file or "")), (line if isinstance(line, int) else None)


def _proof(
    prior: dict,
    ranges: dict[str, list[tuple[int, int]]] | None,
    since_ranges: dict[str, dict[str, list[tuple[int, int]]] | None] | None,
) -> dict[str, list[tuple[int, int]]] | None:
    """The delta a claim about `prior` is checked against: since the head it was RAISED at
    (`since_ranges[since]`, issue #131), else the prior-round delta. `is None`, not falsy: a
    READABLE delta with nothing in it proves nothing moved, and must not fall through to the
    narrower window. None ⇒ unreadable."""
    proof = (since_ranges or {}).get(str(prior.get("since") or ""))
    return ranges if proof is None else proof


def _line_moved(probe: dict, prior: dict, ranges, since_ranges) -> bool:
    proof = _proof(prior, ranges, since_ranges)
    return proof is not None and in_delta(probe, proof)


def prior_touched(
    prior: dict,
    ranges: dict[str, list[tuple[int, int]]] | None,
    since_ranges: dict[str, dict[str, list[tuple[int, int]]] | None] | None,
    *,
    line_level: bool = True,
) -> bool | None:
    """Did the PR change the code `prior` is about since it was raised? None ⇒ the delta was
    unreadable, which callers treat as "not proven" (fail-closed). `line_level=False` asks
    about the FILE: a fix rarely lands on the exact line cited — protoAgent#3811's finding
    cited the function at line 371 and the try/catch landed at 384-395, outside the padded
    hunk — so a re-verification is worth running when the file moved at all."""
    proof = _proof(prior, ranges, since_ranges)
    if proof is None:
        return None
    if line_level:
        return in_delta(prior, proof)
    return _norm(str(prior.get("file") or "")) in proof


def _verifier_confirmed(prior: dict) -> bool:
    """Did a verifier ever confirm this prior? A carried row is stamped `confirmed` either
    way, so `raised_unverified` (set by `merge_carried_findings`) is what remembers it."""
    return (
        str(prior.get("verdict") or "").lower() == "confirmed"
        and not prior.get("raised_unverified")
        and not evidence_unverified(prior)
    )


# ── one prior, one state (#260) ────────────────────────────────────────────────────────────
#
# Priors used to be keyed by exact `file:line`. A panel that re-anchors a finding by a line
# or two (protoLab#34: the same bare import at :51, :53 and :54 across rounds) turned one
# defect into several priors, so a disposition of :54 left :51 and :53 "unaccounted", the
# record carried duplicates, and the `disp=` marker held a row nobody dispositioned. A prior
# is now matched on its FILE plus a near-identical claim or a shared code quote, within a
# line window — the same keying the refutation stores use (`refutations.SAME_CLAIM_LINES`).

# A disposition row carries no claim, only an anchor: it names a prior within this many lines.
ANCHOR_WINDOW_LINES = 5

# Markers in a finding's own evidence/note that say no verifier actually read the code. A
# carry is never stamped `confirmed` over them (#260: a "confirmed" carry whose note read
# "gap: unverified — PR head reads 404").
_UNVERIFIED_RE = re.compile(
    r"\bunverified\b|\b404\b|source unavailable|could not (?:be )?read|cannot confirm|not (?:be )?verified",
    re.IGNORECASE,
)


def evidence_unverified(finding: dict) -> bool:
    """Does the finding's own record say it was never actually verified? Fail-closed toward
    "unverified": the flag set by grounding (`source_unavailable`) or any marker phrase in
    its note or evidence."""
    if finding.get("source_unavailable"):
        return True
    text = f"{finding.get('note') or ''}\n{finding.get('evidence') or ''}"
    return bool(_UNVERIFIED_RE.search(text))


def _same_file(a: object, b: object) -> bool:
    """Same path, or one is the other qualified by directories (`verify_coherence.py` vs
    `evals/graders/verify_coherence.py` — a panel that dropped the directory)."""
    pa, pb = _norm(str(a or "")), _norm(str(b or ""))
    if not pa or not pb:
        return False
    return pa == pb or pa.endswith("/" + pb) or pb.endswith("/" + pa)


def _int_line(finding: dict) -> int | None:
    line = finding.get("line")
    return line if isinstance(line, int) and not isinstance(line, bool) and line > 0 else None


def same_prior(a: dict, b: dict) -> bool:
    """Are `a` and `b` the same prior finding? Same file (`_same_file`), and either the same
    line, or the same defect worded alike at a moved line (`verdicts._same_defect`), or the
    same quoted code within `ANCHOR_WINDOW_LINES`. File-level findings (no line) match only
    each other, by the same claim."""
    from .verdicts import _same_defect

    if not _same_file(a.get("file"), b.get("file")):
        return False
    la, lb = _int_line(a), _int_line(b)
    if la is None or lb is None:
        if la is not None or lb is not None:
            return False
        return " ".join(str(a.get("claim") or "").lower().split()) == " ".join(
            str(b.get("claim") or "").lower().split()
        )
    if la == lb:
        return True
    if _same_defect({**a, "file": b.get("file")}, b):
        return True
    if abs(la - lb) <= ANCHOR_WINDOW_LINES:
        if set(quoted_snippets(a)) & set(quoted_snippets(b)):
            return True
        return _similar_claims(str(a.get("claim") or ""), str(b.get("claim") or ""))
    return False


def _similar_claims(a: str, b: str) -> bool:
    """A looser same-defect test, only ever applied within `ANCHOR_WINDOW_LINES`: the claims
    read alike (≥ 0.6) AND name mostly the same things (identifier Jaccard ≥ 0.5). The same
    "SKIP path never sets `failed`" worded twice passes; "no test for /oauth/poll" beside
    "no test for /oauth/start" shares the template, not the subject, and does not."""
    import difflib

    from .verdicts import identifier_tokens

    ca, cb = " ".join(a.lower().split()), " ".join(b.lower().split())
    if not ca or not cb or difflib.SequenceMatcher(None, ca, cb).ratio() < 0.6:
        return False
    ta, tb = identifier_tokens(a), identifier_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.5


def row_names(row: dict, prior: dict) -> bool:
    """Does disposition `row` (an anchor, no claim) name `prior`? The exact anchor, or a line
    within `ANCHOR_WINDOW_LINES` in the same file; a file-level row names every prior in it."""
    file, line = _disposition_anchor(row)
    if not _same_file(file, prior.get("file")):
        return False
    pl = _int_line(prior)
    if line is None or line <= 0:
        return True
    if pl is None:
        return False
    return abs(line - pl) <= ANCHOR_WINDOW_LINES


_SEVERITY_RANK = {"blocker": 3, "major": 2, "minor": 1, "nit": 0}


def group_priors(priors: list[dict]) -> list[dict]:
    """`priors` collapsed to one entry per defect (`same_prior`, transitively), in order.

    Each entry is the group's representative — a freshly raised member before a carried one,
    the highest severity of any member, `confirmed` only when some member was verifier-
    confirmed with nothing in its own record saying otherwise, and the `since` of the oldest
    raise among them (a carried member's) so a fix is proven over the whole window (#131) —
    plus `members`, the anchors it stands for."""
    groups: list[list[dict]] = []
    for p in priors or []:
        if not isinstance(p, dict):
            continue
        hit = [g for g in groups if any(same_prior(p, m) for m in g)]
        if not hit:
            groups.append([p])
            continue
        merged = hit[0]
        merged.append(p)
        for g in hit[1:]:
            merged.extend(g)
            groups.remove(g)
    out = []
    for g in groups:
        ranked = sorted(g, key=lambda m: (not _verifier_confirmed(m), bool(m.get("carried"))))
        rep = dict(ranked[0])
        rep["severity"] = max(
            (str(m.get("severity") or "").lower() for m in g), key=lambda s: _SEVERITY_RANK.get(s, -1)
        )
        if not _verifier_confirmed(rep) and str(rep.get("verdict") or "").lower() == "confirmed":
            rep["raised_unverified"] = True  # stamped `confirmed` by a carry, never verified (#260)
        carried_since = next((str(m.get("since")) for m in g if m.get("carried") and m.get("since")), "")
        if carried_since:
            rep["since"] = carried_since
        rep["members"] = [finding_anchor(m) for m in g]
        out.append(rep)
    return out


def matches_group(finding: dict, group: dict) -> bool:
    """Does `finding` (a prior, a re-listing or a carry) belong to `group`?"""
    if same_prior(finding, group):
        return True
    return finding_anchor(finding) in set(group.get("members") or [])


DISPOSITION_FIXED = "fixed"
DISPOSITION_REFUTED = "refuted"
# What the gate did with a disposition — one state per prior (#260).
OUTCOME_FIXED = "fixed"  # `fixed`, proven by the delta
OUTCOME_REFUTED = "refuted"  # `refuted`, honoured
OUTCOME_OPEN = "open"
OUTCOME_FIXED_UNPROVEN = "fixed-unproven"  # `fixed`, but the cited line did not move
OUTCOME_NOT_HONOURED = "refutation-not-honoured"  # #38: verifier-confirmed on unchanged code
OUTCOME_CONFLICT = "conflict"  # rows disagree
OUTCOME_NONE = "undispositioned"
_CLEARED_OUTCOMES = (OUTCOME_FIXED, OUTCOME_REFUTED)


def prior_ledger(
    history: list[dict],
    dispositions: list[dict],
    *,
    ranges: dict[str, list[tuple[int, int]]] | None = None,
    since_ranges: dict[str, dict[str, list[tuple[int, int]]] | None] | None = None,
    paths: list[str] | None = None,
) -> list[dict]:
    """Every gating prior of the last substantive round, ONE entry per defect, with what this
    round's dispositions did to it: `{"prior", "rows", "outcome"}` (#260).

    The rules are `unaccounted_priors`' (it is a view over this): `fixed` clears only when
    the cited line moved since the prior was raised; `refuted` clears an `uncertain` or
    never-verified prior, or one whose line moved (#218), and is otherwise NOT HONOURED
    (#38); `open` never clears, and vetoes a clearing row that disagrees with it. Priors the
    PR no longer changes, nearby notes, fabricated (ungrounded) and refuted ones are no debt."""
    round_ = last_substantive_round(history)
    if round_ is None:
        return []
    origin = str(round_.get("head") or "")
    changed = {_norm(p) for p in paths or [] if p and p.strip()}
    debt = []
    for f in round_.get("findings") or []:
        if not isinstance(f, dict) or f.get("ungrounded") or f.get("nearby"):
            continue
        if str(f.get("severity") or "").lower() not in _BLOCKING:
            continue
        if str(f.get("verdict") or "").lower() == "refuted":
            continue
        if changed and _norm(str(f.get("file") or "")) not in changed:
            continue  # confinement keeps it out of every verdict — not this PR's debt
        debt.append({**f, "since": str(f.get("since") or origin)})
    ledger = []
    for prior in group_priors(debt):
        rows = [r for r in dispositions or [] if isinstance(r, dict) and row_names(r, prior)]
        said = {str(r.get("disposition") or "").lower() for r in rows}
        outcomes = set()
        for r in rows:
            d = str(r.get("disposition") or "").lower()
            probe = {"file": prior.get("file"), "line": _disposition_anchor(r)[1] or _int_line(prior)}
            moved = _line_moved(probe, prior, ranges, since_ranges)
            if d == "open":
                outcomes.add(OUTCOME_OPEN)
            elif d == DISPOSITION_FIXED:
                outcomes.add(OUTCOME_FIXED if moved else OUTCOME_FIXED_UNPROVEN)
            elif d == DISPOSITION_REFUTED:
                unconfirmed = str(prior.get("verdict") or "").lower() == "uncertain"
                outcomes.add(OUTCOME_REFUTED if (unconfirmed or moved) else OUTCOME_NOT_HONOURED)
        if not rows:
            outcome = OUTCOME_NONE
        elif OUTCOME_OPEN in outcomes:
            outcome = OUTCOME_OPEN if len(said) == 1 else OUTCOME_CONFLICT
        elif outcomes & set(_CLEARED_OUTCOMES):
            outcome = next(o for o in _CLEARED_OUTCOMES if o in outcomes)
        else:
            outcome = next(iter(sorted(outcomes)))
        ledger.append({"prior": prior, "rows": rows, "outcome": outcome})
    return ledger


def ledger_debt(ledger: list[dict]) -> list[dict]:
    """The ledger's priors still owed — every entry whose outcome did not clear it."""
    return [{**e["prior"], "owed": e["outcome"]} for e in ledger if e["outcome"] not in _CLEARED_OUTCOMES]


def credited_minors(
    history: list[dict],
    reported: list[dict],
    *,
    ranges: dict[str, list[tuple[int, int]]] | None,
    since_ranges: dict[str, dict[str, list[tuple[int, int]]] | None] | None = None,
) -> list[dict]:
    """Prior minor/nit findings this round credits as addressed (#260): no longer raised in
    any form, near lines the PR changed since they were raised (`CREDIT_WINDOW_LINES`). Prior requests track only
    blocker/majors, so a fixed minor used to vanish without a word. Non-gating, and worded
    as what it is — the code moved and the panel stopped raising it, not a verified fix."""
    round_ = last_substantive_round(history)
    if round_ is None or ranges is None:
        return []
    origin = str(round_.get("head") or "")
    out = []
    for f in round_.get("findings") or []:
        if not isinstance(f, dict) or f.get("nearby") or f.get("ungrounded"):
            continue
        if str(f.get("severity") or "").lower() not in ("minor", "nit"):
            continue
        if str(f.get("verdict") or "").lower() == "refuted":
            continue
        if any(isinstance(r, dict) and same_prior(r, f) for r in reported or []):
            continue
        prior = {**f, "since": str(f.get("since") or origin)}
        proof = _proof(prior, ranges, since_ranges)
        if proof is not None and _near_delta(prior, proof, CREDIT_WINDOW_LINES):
            out.append(prior)
    return out


# Finders cite a minor's line loosely (data-plugin#1 round 1 put an `engine.py` field at :152
# that sat at :143), and a credit gates nothing — so a change within this many lines counts.
CREDIT_WINDOW_LINES = 25


def _near_delta(finding: dict, ranges: dict[str, list[tuple[int, int]]], window: int) -> bool:
    spans = ranges.get(_norm(str(finding.get("file") or "")))
    if spans is None:
        return False
    line = _int_line(finding)
    if not spans or line is None:
        return True
    return any(start - window <= line <= end + window for start, end in spans)


def _in(prior: dict, pool: list[dict]) -> bool:
    return any(isinstance(p, dict) and (same_prior(prior, p) or matches_group(p, prior)) for p in pool or [])


def undispositioned(ledger: list[dict], still_owed: list[dict]) -> list[dict]:
    """The owed priors this round said NOTHING about — the only ones the "unaccounted"
    footer may name (#260). A prior dispositioned `open`, or `fixed`/`refuted` without the
    gate honouring it, is explained in the Prior requests table, not called silent."""
    silent = [e["prior"] for e in ledger if e["outcome"] == OUTCOME_NONE]
    return [p for p in still_owed or [] if _in(p, silent)]


_DISPLAY = {
    OUTCOME_FIXED: ("✅", "fixed"),
    OUTCOME_REFUTED: ("🚫", "refuted"),
    OUTCOME_OPEN: ("🔴", "open"),
    OUTCOME_FIXED_UNPROVEN: ("⏸", "fixed — not proven: the cited line did not change; still carried"),
    OUTCOME_NOT_HONOURED: ("⚠️", "refutation not honoured — verifier-confirmed on unchanged code; still carried"),
    OUTCOME_CONFLICT: ("⚠️", "contradictory dispositions — still carried"),
}


def disposition_display(
    ledger: list[dict],
    dispositions: list[dict],
    *,
    still_owed: list[dict],
    cleared: list[dict] | None = None,
) -> list[dict]:
    """The dispositions as the body's Prior requests table shows them: one row per prior
    (#260), labelled with what the GATE did, not only what the report said.

    The report's rows are kept (its `why` is the human explanation), but a row whose prior
    the gate still carries says so — "refutation not honoured", "fixed — not proven" — and a
    prior cleared afterwards (evidence gone, re-verified as refuted) says that. Two rows for
    one prior (a drifted anchor) collapse into the first; a row that names no gating prior
    is labelled as such rather than read as a disposition of something real."""
    out: list[dict] = []
    used: set[int] = set()
    for entry in ledger:
        rows = entry["rows"]
        if not rows or id(rows[0]) in used:
            continue  # its row already speaks for an entry above — one line per row
        prior = entry["prior"]
        row = dict(rows[0])
        used.update(id(r) for r in rows)
        outcome = entry["outcome"]
        if outcome in _CLEARED_OUTCOMES or not _in(prior, still_owed):
            mark, label = _DISPLAY.get(outcome, ("✅", "cleared"))
            if outcome not in _CLEARED_OUTCOMES:
                why = next((c.get("recheck_note") for c in cleared or [] if _in(c, [prior])), None)
                mark, label = (
                    "✅",
                    "cleared at this head — re-verified" if why is not None else "cleared at this head",
                )
        else:
            rechecked = next((p for p in still_owed if same_prior(p, prior) and p.get("rechecked")), None)
            mark, label = _DISPLAY.get(outcome, ("•", outcome))
            if rechecked and outcome == OUTCOME_NOT_HONOURED:
                label = "refutation not honoured — re-verified as confirmed at this head; still carried"
        row["shown"] = (mark, label)
        out.append(row)
    for r in dispositions or []:
        if isinstance(r, dict) and id(r) not in used:
            out.append({**r, "shown": ("•", f"{str(r.get('disposition') or '?').lower()} — names no open prior")})
    return out


def verdict_with_debt(verdict: str, carried: list[dict]) -> str:
    """FAIL when this round carries a prior blocker/major that still gates — one a verifier
    confirmed, or one no round has verified yet — else `verdict` unchanged (#260). Pure:
    the carried set is the caller's fact."""
    gating = [
        f
        for f in carried or []
        if isinstance(f, dict)
        and str(f.get("severity") or "").lower() in _BLOCKING
        and str(f.get("verdict") or "").lower() not in ("uncertain", "refuted")
    ]
    return "FAIL" if gating else verdict


def render_debt_verdict_note(was: str, carried: list[dict]) -> str:
    """Why a round whose own findings came to `was` posts FAIL (#260)."""
    n = len([f for f in carried or [] if str(f.get("verdict") or "").lower() not in ("uncertain", "refuted")])
    return (
        f"\n\n---\n**FAIL on carried debt.** This round's own findings come to **{was}**, but it carries "
        f"{n} prior blocker/major finding(s) still owed (listed in Findings as `carried`). A carry keeps "
        "gating until a round fixes it (proven by the delta), refutes it, or re-verifies it away — so the "
        "verdict says so too (#260)."
    )


def render_credited_minors(credited: list[dict]) -> str:
    """A compact credit line for prior minors/nits the PR addressed (#260)."""
    if not credited:
        return ""
    items = ", ".join(f"`{finding_anchor(f)}`" for f in credited[:20])
    more = f" (+{len(credited) - 20} more)" if len(credited) > 20 else ""
    return (
        f"\n\n---\n**Addressed since the last round** ({len(credited)} minor/nit): {items}{more} — no "
        "longer raised, and the PR changed the code around them since they were raised."
    )


def unaccounted_priors(
    history: list[dict],
    dispositions: list[dict],
    *,
    ranges: dict[str, list[tuple[int, int]]] | None = None,
    since_ranges: dict[str, dict[str, list[tuple[int, int]]] | None] | None = None,
    paths: list[str] | None = None,
) -> list[dict]:
    """Prior blocker/major findings this round neither reported nor honestly dispositioned.

    The generalization of `unexplained_clearance`: it applies at ANY verdict, because a
    confirmed major can vanish into a WARN about unrelated nits just as easily as into a
    clean PASS — protoAgent#2150 round 3 did exactly that, and #27's rule (rightly)
    said nothing, because the verdict wasn't a clean PASS.

    A `fixed` disposition is only honoured when `ranges` shows the flagged line ACTUALLY
    MOVED. protoAgent#2208 shipped a real major to main because the model emitted
    `{"prior": "config_routes.py:271", "disposition": "fixed", "why": "verifier confirmed
    ... resolved in updated diff"}` on a line that is byte-identical across every head —
    a hallucinated fix, trusted because dispositions were the authority and nothing
    grounded the claim. This is the #25 lesson (a verdict follows the read, not the story)
    applied to the disposition itself: a fix that left no trace in the delta is unproven,
    and an unproven fix does not clear a blocker.

    Fail-CLOSED on `fixed`: without a readable delta a `fixed` claim cannot be verified,
    so it is not honoured — a real fix with an unreadable compare costs one extra round,
    a false one shipping a defect costs a production incident.

    `open` does NOT clear a prior blocker/major (protoAgent#2283). The original design
    assumed `open` meant "carried into the current findings at its severity", so the block
    would stand on the finding itself. It doesn't hold: on #2283 the panel dispositioned
    three still-present majors `open` ("PR does not address this — still exists") but
    re-graded the FINDINGS major→minor/nit, dropping the verdict FAIL→WARN and lifting the
    block on two real bugs (uncaught ValueError→500, session collision). An `open` blocker
    is still blocking — the disposition IS the panel confirming the defect persists — so it
    is treated as unaccounted, whatever severity the re-report used.

    `refuted` against a *confirmed* prior finding is treated as `open` — the block stands
    until the finding is delta-verified `fixed` or an operator dismisses it (issue #38).
    A single model pass must not override a grounded, confirmed blocker. `refuted` is only
    allowed to clear a prior finding graded `uncertain` (which has less grounding and where
    refutation is plausible). The unaccounted list already serves as the telemetry-ready
    signal: any `refuted` that was rejected appears in `missing` for `render_unaccounted_note`.

    A CARRIED finding is verified against the delta since the head it was RAISED at, not
    since the previous round (issue #131). `ranges` spans only prior-head→head, so a fix
    could be proven in exactly one round — the one right after it landed. If that round was
    incomplete, or called the finding "cannot confirm", the finding was carried into its
    record, and from then on the line never moved again inside any prior-head→head delta:
    `fixed` was unprovable forever and only a rebase cleared the PR (mythxengine#805, both
    majors fixed several commits before the rounds that kept carrying them). Each returned
    finding is stamped `since` — the head of the round that raised it, kept across carries —
    and `since_ranges[since]` (that head→current head) is what a `fixed` on it is checked
    against. Still fail-closed: a `since` with no readable delta falls back to `ranges`.

    A `refuted` on a CONFIRMED prior is honoured when the flagged line moved since the prior
    was raised — the same proof a `fixed` needs (#218). On protoAgent#3812 the panel wrote
    `refuted — the current head places the (#3805) reference inside the bold lead-in` about
    a line the delta rewrote: a fix, worded as a refutation. #38 still stands for the case it
    was written for — a refutation of a confirmed finding on code that did NOT move is one
    model draw against another, and the block holds.

    `paths` (the PR's changed files, when readable) drops a prior on a file the PR no longer
    changes: confinement would exclude it from any verdict, so it is not debt this PR can
    pay (#220 — protoContent#565 carried a confined `(repo root)` "no changeset" finding for
    five rounds after the changeset landed). Empty or None ⇒ no filtering, fail-closed.

    Only the LAST substantive round is consulted, same as `unexplained_clearance`.
    """
    if not dispositions:
        return []
    return ledger_debt(prior_ledger(history, dispositions, ranges=ranges, since_ranges=since_ranges, paths=paths))


RELISTED_NOTE = (
    "re-listed from a prior round without fresh evidence or a verifier ruling — graded uncertain, "
    "not re-confirmed (issue #204); it clears when a round records findings without it"
)


def normalize_relisted_priors(findings: list[dict], history: list[dict]) -> tuple[list[dict], list[dict]]:
    """(findings, the ones normalized) — a synthesizer's re-listing of a prior minor/nit is
    not a new finding (issue #204).

    On terminal-plugin#10 the panel re-emitted a prior minor in its own findings array: no
    verifier verdict, and `evidence` that paraphrased the original quote ("the function sets
    os.environ['LANG'] …" for code that never touched `os.environ`). Two things followed.
    `verification_ran` read the verdict-less row as a finding the verifier failed to reach,
    so the round posted `verified=false` and the head sat at `hold:unverified` — even though
    every finding the round actually raised was verified. And the paraphrase carried no
    checkable quote, so grounding had nothing to test and #197's evidence-gone rule could not
    fire: the row could be re-listed forever.

    The rule: a minor/nit with NO verdict that matches a finding of the last substantive round
    (same file:line, or the same defect at a moved line — `_same_defect`) is stamped
    `carried: true`, `carried_by: "synthesizer"`, `verdict: uncertain`, keeps `since` from the
    prior, and — when it quotes nothing of its own — inherits the prior's `evidence`, so
    grounding checks the ORIGINAL quote at head rather than prose. `uncertain` is the honest
    grade for a claim nobody re-verified: it still posts, still reads, cannot gate a merge,
    and does not read as a verifier gap.

    Scoped to minor/nit on purpose. A blocker/major is carried by `unaccounted_priors` +
    `merge_carried_findings` with its own fail-closed rules, and an unverified re-listing of
    one SHOULD still trip `verification_ran`: wrongly trusting it merges a defect.
    """
    from .verdicts import _same_defect  # history layer over the pure mapping

    prior_round = next((r for r in reversed(history or []) if isinstance(r, dict) and r.get("findings")), None)
    if prior_round is None or not findings:
        return list(findings or []), []
    origin = str(prior_round.get("head") or "")
    priors = [f for f in prior_round["findings"] if isinstance(f, dict)]
    out: list[dict] = []
    relisted: list[dict] = []
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity") or "").lower()
        if severity not in ("minor", "nit") or str(finding.get("verdict") or "").strip():
            out.append(finding)
            continue
        anchor = _anchor(finding.get("file"), finding.get("line"))
        match = next(
            (
                p
                for p in priors
                if str(p.get("verdict") or "").lower() != "refuted"
                and (_anchor(p.get("file"), p.get("line")) == anchor or _same_defect(finding, p))
            ),
            None,
        )
        if match is None:
            out.append(finding)
            continue
        normalized = {
            **finding,
            "verdict": "uncertain",
            "carried": True,
            "carried_by": "synthesizer",
            "since": str(match.get("since") or origin),
            "note": RELISTED_NOTE,
        }
        if not quoted_snippets(finding) and quoted_snippets(match):
            normalized["evidence"] = match.get("evidence")
        out.append(normalized)
        relisted.append(normalized)
    return out, relisted


_BLOCKING = ("blocker", "major")

# A re-verification of carried priors is one seeded verify step (seconds, not a panel), and
# bounded: a PR carrying more debt than this re-checks the first few each round.
MAX_PRIOR_RECHECK = 6

RECHECK_CONFIRMED_NOTE = "re-verified at this head (#220)"
INHERITED_NOTE = (
    "re-listed without a fresh verifier ruling on a line unchanged since a verifier confirmed it — "
    "the earlier confirmation stands (#232)"
)


def carried_debt(round_: dict | None) -> list[dict]:
    """The prior blocker/major findings a round's RECORD carries as standing debt — rows
    `merge_carried_findings` re-recorded because the round did not account for them.

    A clear verdict that carries such a row has NOT cleared the head: the defect a verifier
    confirmed is still on the books. That used to hold promotion only by accident — the
    carried rows made `verification_ran` read the round as unverified (#220) — so the hold
    said `hold:unverified` and could never lift without a waiver. It is its own fact now. A
    synthesizer re-listing (`carried_by: synthesizer`) is the round's OWN finding, judged by
    the verdict like any other, and is not debt in this sense."""
    return [
        f
        for f in (round_ or {}).get("findings") or []
        if isinstance(f, dict)
        and f.get("carried")
        and f.get("carried_by") != "synthesizer"
        and str(f.get("severity") or "").lower() in _BLOCKING
        and str(f.get("verdict") or "").lower() not in ("refuted", "uncertain")
    ]


def relisted_blocking_priors(findings: list[dict], history: list[dict]) -> tuple[list[dict], list[tuple[int, dict]]]:
    """(findings, [(index, prior)]) — this round's VERDICT-LESS blocker/major rows that re-list
    a finding of the last substantive round (#232 ask 5).

    The report recipe tells the panel to carry an `open` prior "into your findings array
    too", and it does — after the verify step, so the row never meets a verifier. On
    protoAgent#4017 r2 that row read "Carried from round 1; no fix observed in this round's
    diff" about a line the delta had just rewritten (`cfg, a, b` → `cfg, _, b`), and its
    verdict-less major FAILed the round. Each such row is returned with the prior it re-lists
    so the dispatcher can re-verify it against THIS head; the row gets the prior's `since`
    (its provenance — a fix is provable against the head it was raised at, #131, and the
    re-listing used to restart that window at every round) and, when it quotes nothing of its
    own, the prior's `evidence`. Its verdict is left to the re-check (`resolve_relisting`).
    """

    prior_round = next((r for r in reversed(history or []) if isinstance(r, dict) and r.get("findings")), None)
    out = [f for f in findings or [] if isinstance(f, dict)]
    if prior_round is None:
        return out, []
    origin = str(prior_round.get("head") or "")
    # Blocker/major priors only: a minor the panel now calls a major is an escalation, a claim
    # of its own — not the earlier ruling re-listed, and never one that may inherit it.
    priors = [
        p
        for p in prior_round["findings"]
        if isinstance(p, dict)
        and str(p.get("verdict") or "").lower() != "refuted"
        and str(p.get("severity") or "").lower() in _BLOCKING
        # a #232 `nearby` note never gated, so a re-listing of it has no ruling to inherit
        and not p.get("nearby")
        and not p.get("ungrounded")
    ]
    pairs: list[tuple[int, dict]] = []
    for i, finding in enumerate(out):
        if str(finding.get("severity") or "").lower() not in _BLOCKING or str(finding.get("verdict") or "").strip():
            continue
        # `same_prior` (#260): the same file, and the same claim or quoted code within a line
        # window — a re-listing re-anchored by a line or two is still the prior it re-lists.
        match = next((p for p in priors if same_prior(finding, p)), None)
        if match is None:
            continue
        prior = {**match, "since": str(match.get("since") or origin)}
        row = {**finding, "since": str(finding.get("since") or prior["since"])}
        # The prior's quote when the re-listing brought none (protoAgent#4017 r2: `"evidence":
        # ""`) or only prose — the verifier locates a claim by its quoted code.
        if prior.get("evidence") and (
            not str(row.get("evidence") or "").strip() or (not quoted_snippets(row) and quoted_snippets(prior))
        ):
            row["evidence"] = prior.get("evidence")
        out[i] = row
        pairs.append((i, prior))
    return out, pairs


def needs_recheck(prior: dict, ranges, since_ranges, *, disputed: bool = False) -> bool:
    """Could a re-verification of this UNACCOUNTED prior change anything? Only when its
    refutation could be honoured (`recheck_clears`): the prior was never verifier-confirmed,
    or its file moved since it was raised, or (#234) this same-head round DISPUTES it — see
    `disputed_anchors`. Otherwise a confirmed prior on untouched code stays debt whatever
    one more draw says (#38), so re-checking it would only spend a verify step."""
    if disputed:
        return True
    return not _verifier_confirmed(prior) or prior_touched(prior, ranges, since_ranges, line_level=False) is True


def recheck_clears(prior: dict, verdict: str, ranges, since_ranges, *, disputed: bool = False) -> bool:
    """Does a re-verification's `verdict` at this head clear `prior`? Fail-CLOSED.

    Only `refuted` clears, and only when it is new evidence rather than a re-draw: either no
    verifier ever confirmed the prior (#220 — protoContent#565 carried two never-verified
    priors until a waiver), or its file changed since it was raised (#218 — the fix to
    protoAgent#3811's finding landed thirteen lines from the line it cited). A refutation of
    a verifier-confirmed finding on code that has not moved does not override it (#38).

    `disputed` (#234) is the one exception to #38, and it is narrow: a re-review of the SAME
    head whose report refuted this prior WITH EVIDENCE (`disputed_anchors`), typically after
    the author posted counter-evidence. The verifier's refutation at this head is then a
    second, independent refutation, not a lone re-draw — and without it a FAIL on correct
    code could only be cleared by a cosmetic push (mythxengine-sdk#409)."""
    if str(verdict or "").lower() != "refuted":
        return False
    if disputed:
        return True
    return not _verifier_confirmed(prior) or prior_touched(prior, ranges, since_ranges, line_level=False) is True


# ── a refuted FAIL on an unchanged head (#234) ─────────────────────────────────────────────
#
# Strictest-verdict-wins per head (#89) guards a RACE: two panels on one head land a FAIL and
# a PASS in arbitrary order, and the PASS must not shadow the FAIL. It also made a FAIL on an
# unchanged head permanent: a later re-review that weighed the author's counter-evidence and
# refuted the finding was outvoted by the very FAIL it refuted (mythxengine-sdk#409). The rule
# below lets exactly one kind of round replace an earlier FAIL, and keeps strictest-wins for
# everything else. It is pure and takes the rounds as facts, so the gate (`QA panel`,
# approve-on-green) and `scripts/review_at_head.py` (`Review at head`) evaluate the SAME rule.

# A refutation's `why` shorter than this is an assertion ("false positive"), not evidence.
MIN_REFUTATION_EVIDENCE_CHARS = 20
# A round dispositioning more blocking priors than this writes no record — it supersedes
# nothing (fail-closed), and the marker stays a bounded size.
MAX_DISPOSITION_RECORD_ROWS = 50
# Bound on the encoded record a reader will decode.
_MAX_DISPOSITION_RECORD_CHARS = 16_000
_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def blocking_priors(round_: dict | None) -> list[dict]:
    """The blocker/major findings a round's record holds as gating debt — the same set
    `unaccounted_priors` holds a later round to: not refuted, not a #232 nearby note, not a
    finding grounding already called fabricated."""
    return [
        f
        for f in (round_ or {}).get("findings") or []
        if isinstance(f, dict)
        and str(f.get("severity") or "").lower() in _BLOCKING
        and str(f.get("verdict") or "").lower() != "refuted"
        and not f.get("nearby")
        and not f.get("ungrounded")
    ]


def last_substantive_round(history: list[dict]) -> dict | None:
    """The round a new round's dispositions answer: the newest with findings (the same round
    `unaccounted_priors` reads)."""
    return next((r for r in reversed(history or []) if isinstance(r, dict) and r.get("findings")), None)


def finding_anchor(finding: dict) -> str:
    """`file:line` (or the bare file), normalized the way every rule here matches anchors."""
    return _anchor(finding.get("file"), finding.get("line"))


def _row_anchor(row: dict) -> str:
    file, line = _disposition_anchor(row)
    return f"{file}:{line}" if isinstance(line, int) else file


def _has_evidence(row: dict) -> bool:
    return len(" ".join(str(row.get("why") or "").split())) >= MIN_REFUTATION_EVIDENCE_CHARS


def disputed_anchors(dispositions: list[dict]) -> set[str]:
    """Anchors (`file:line`, or the bare file) the report refuted WITH EVIDENCE.

    Only `refuted` rows whose `why` is at least `MIN_REFUTATION_EVIDENCE_CHARS` — and an
    anchor that ANY row also calls `fixed` or `open` is not disputed: a contradictory
    report has not refuted anything. Exact anchors only, no file-level fallback."""
    refuted: set[str] = set()
    other: set[str] = set()
    for row in dispositions or []:
        if not isinstance(row, dict):
            continue
        anchor = _row_anchor(row)
        if not anchor:
            continue
        if str(row.get("disposition") or "").lower() == "refuted" and _has_evidence(row):
            refuted.add(anchor)
        else:
            other.add(anchor)
    return refuted - other


def disposition_record(dispositioned: dict | None, dispositions: list[dict], *, still_open: list[dict]) -> dict | None:
    """The machine-readable account of what this round did with `dispositioned`'s blocking
    findings, or None when there is nothing to record (#234).

    `{"of": <review id of the dispositioned round>, "rows": [{"a": anchor, "d": disposition,
    "e": evidenced, "h": cleared}]}` — one row per blocking prior. `d` is the report's
    disposition for that exact anchor ("" when it gave none, "conflict" when rows disagree);
    `e` is whether a `refuted` row carried evidence; `h` is whether the prior left this round
    CLEARED, i.e. it is not among `still_open` (the priors this round still carries as debt:
    unaccounted after the re-check, or deferred). The posted body keeps the dispositions
    table for people; this record is what `supersedes` reads, on both checks.

    None — the round supersedes nothing — when the dispositioned round has no readable
    review id, no blocking findings, or more than `MAX_DISPOSITION_RECORD_ROWS` of them."""
    if not dispositioned:
        return None
    of = _review_id(dispositioned.get("id"))
    priors = blocking_priors(dispositioned)
    if of <= 0 or not priors or len(priors) > MAX_DISPOSITION_RECORD_ROWS:
        return None
    # One prior per defect (#260): a row dispositioning `run_livecodebench.py:54` answers the
    # same import flagged at :51 and :53, so all three record that answer — no phantom row
    # with `d=""`. Still one record row per prior ANCHOR, the shape `supersedes` reads.
    groups = group_priors(priors)
    rows = []
    for prior in priors:
        group = next((g for g in groups if matches_group(prior, g)), prior)
        mine = [r for r in dispositions or [] if isinstance(r, dict) and row_names(r, group)]
        given = {str(r.get("disposition") or "").lower() for r in mine}
        disposition = next(iter(given)) if len(given) == 1 else ("conflict" if given else "")
        evidenced = any(str(r.get("disposition") or "").lower() == "refuted" and _has_evidence(r) for r in mine)
        held = _in(group, still_open or [])
        rows.append({"a": finding_anchor(prior), "d": disposition, "e": evidenced, "h": not held})
    return {"of": of, "rows": rows}


def encode_disposition_record(record: dict | None) -> str:
    """`record` as one marker-safe token: unpadded base64url of compact JSON, or "".

    Carried as the marker attribute `disp=`, NOT as a fenced JSON block: the body's findings
    record must stay its one JSON block (`read_findings_record`), and a marker attribute sits
    in the code-written first line, where no model-authored text can forge it. No `=`, `>`
    or whitespace can appear in it, so neither marker reader can misparse it."""
    if not record:
        return ""
    raw = json.dumps(record, separators=(",", ":"), sort_keys=True).encode()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return token if len(token) <= _MAX_DISPOSITION_RECORD_CHARS else ""


def decode_disposition_record(token: object) -> dict | None:
    """A `disp=` token → `{"of", "rows"}`, or None for anything absent, oversized or
    malformed. Every reader treats None as "dispositioned nothing" (fail-closed)."""
    if not isinstance(token, str) or not token or len(token) > _MAX_DISPOSITION_RECORD_CHARS:
        return None
    if not _B64URL_RE.match(token):
        return None
    try:
        parsed = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
        return None
    of = parsed.get("of")
    if not isinstance(of, int) or isinstance(of, bool) or of <= 0:
        return None
    rows = []
    for row in parsed["rows"]:
        if not (
            isinstance(row, dict)
            and isinstance(row.get("a"), str)
            and isinstance(row.get("d"), str)
            and isinstance(row.get("e"), bool)
            and isinstance(row.get("h"), bool)
        ):
            return None
        rows.append({"a": row["a"], "d": row["d"], "e": row["e"], "h": row["h"]})
    return {"of": of, "rows": rows}


def supersedes(fail: dict, newer: dict) -> bool:
    """Does `newer` replace `fail` as the verdict for their head? Pure; fails CLOSED (#234).

    True only when ALL of these hold:

      - `fail` is a FAIL with a readable findings record and a known review id, not a
        reaffirmed (carried) verdict, and it has at least one blocking finding;
      - `newer` is a round on the SAME head, posted AFTER `fail` (GitHub's monotonic review
        id), not reaffirmed, COMPLETE, VERIFIED, with a readable findings record;
      - `newer` dispositioned `fail` itself — its record's `of` is `fail`'s review id. Two
        rounds racing on one head (#89) were both handed an older round, so neither names
        the other and strictest-wins still settles them;
      - EVERY blocking finding of `fail` has a row in that record that says `refuted`, with
        evidence, and that the round actually cleared (the refutation was honoured, not
        carried as debt) — `open`, `fixed` (impossible on an unchanged head, so suspect),
        no disposition, or contradictory rows all keep the FAIL;
      - and `newer`'s own record does not still hold a blocking finding at any of those
        anchors.

    `newer`'s own verdict is not consulted: a superseding FAIL or WARN is simply the newer
    verdict, and the strictest rule then applies to it like any other round."""
    if not isinstance(fail, dict) or not isinstance(newer, dict):
        return False
    if str(fail.get("verdict") or "").upper() != "FAIL" or fail.get("reaffirmed") or not fail.get("findings_recorded"):
        return False
    fail_id, newer_id = _review_id(fail.get("id")), _review_id(newer.get("id"))
    if fail_id <= 0 or newer_id <= fail_id:
        return False
    if not fail.get("head") or newer.get("head") != fail.get("head") or newer.get("reaffirmed"):
        return False
    if not (newer.get("complete") is True and newer.get("verified") is True and newer.get("findings_recorded") is True):
        return False
    record = newer.get("disposed")
    if not isinstance(record, dict) or record.get("of") != fail_id:
        return False
    priors = blocking_priors(fail)
    if not priors:
        return False
    rows: dict[str, list[dict]] = {}
    for row in record.get("rows") or []:
        rows.setdefault(str(row.get("a") or ""), []).append(row)
    still = {_anchor(f.get("file"), f.get("line")) for f in blocking_priors(newer)}
    for prior in priors:
        anchor = _anchor(prior.get("file"), prior.get("line"))
        mine = rows.get(anchor) or []
        if not mine or anchor in still:
            return False
        if not all(r.get("d") == "refuted" and r.get("e") is True and r.get("h") is True for r in mine):
            return False
    return True


def superseded_fails(rounds: list[dict]) -> list[tuple[dict, dict]]:
    """`[(fail, superseding round)]` among one head's rounds — every FAIL some later round
    `supersedes`. Everything not listed keeps its full strictest-wins (#89) weight."""
    pool = [r for r in rounds or [] if isinstance(r, dict)]
    out = []
    for fail in pool:
        by = next((r for r in pool if r is not fail and supersedes(fail, r)), None)
        if by is not None:
            out.append((fail, by))
    return out


def recheck_payload(finding: dict) -> dict:
    """What the verifier is handed for one re-check: the claim and its quote, never the
    earlier ruling — a verifier shown `verdict: confirmed` and a carry note re-reads the
    ruling, not the code."""
    return {k: finding[k] for k in ("file", "line", "severity", "category", "claim", "evidence") if k in finding}


def align_recheck(candidates: list[dict], annotated: list[dict]) -> list[dict | None]:
    """The verifier's annotated row for each candidate, or None where it gave none.

    Positional when the verifier returned exactly what it was handed, file for file — the
    contract ("return the same fenced findings array, annotated"). Otherwise by file and the
    same claim, or the same defect at a re-anchored line. Anything unmatched is None, which
    every caller treats as "not re-verified" — never as a ruling."""
    from .verdicts import _same_defect

    rows = [a for a in annotated or [] if isinstance(a, dict)]

    def _verdict(row: dict | None) -> dict | None:
        if row is None:
            return None
        verdict = str(row.get("verdict") or "").strip().lower()
        if verdict not in ("confirmed", "refuted", "uncertain"):
            return None
        return {"verdict": verdict, "note": str(row.get("note") or "").strip()}

    if len(rows) == len(candidates) and all(
        _norm(str(a.get("file") or "")) == _norm(str(c.get("file") or "")) for a, c in zip(rows, candidates)
    ):
        return [_verdict(a) for a in rows]
    out: list[dict | None] = []
    for c in candidates:
        claim = " ".join(str(c.get("claim") or "").split())
        match = next(
            (
                a
                for a in rows
                if _norm(str(a.get("file") or "")) == _norm(str(c.get("file") or ""))
                and (" ".join(str(a.get("claim") or "").split()) == claim or _same_defect(a, c))
            ),
            None,
        )
        out.append(_verdict(match))
    return out


def resolve_relisting(row: dict, prior: dict, ruling: dict | None, ranges, since_ranges) -> tuple[str, dict]:
    """What a verdict-less re-listing of a prior blocker/major becomes (#232 ask 5).

    ("confirmed", row)   the re-check confirmed it at this head — it blocks, verified.
    ("cleared", row)     the re-check refuted it and `recheck_clears` honours that — it is
                         dropped from this round; the body names it.
    ("inherited", row)   no ruling, but the cited line has NOT moved since a verifier
                         confirmed it — the earlier confirmation still describes this code.
    ("deferred", prior)  no ruling, and the cited line CHANGED since it was raised: the claim
                         is about code that is gone, so it may not block this round — but
                         it is not cleared either. The PRIOR goes back to the carry, which
                         records it as debt the next round must account for and which holds
                         promotion (`carried_debt`). Never a laundered finding, never a
                         blocking verdict nobody verified.
    ("unverified", row)  no ruling, and nothing to inherit — the round stays unverified,
                         exactly as before (the verify retry is the backstop).
    """
    verdict = (ruling or {}).get("verdict", "")
    note = (ruling or {}).get("note", "")
    if verdict == "confirmed":
        return "confirmed", {
            **row,
            "verdict": "confirmed",
            "note": f"{note} — {RECHECK_CONFIRMED_NOTE}" if note else RECHECK_CONFIRMED_NOTE,
        }
    if recheck_clears(prior, verdict, ranges, since_ranges):
        return "cleared", {**row, "verdict": "refuted", "note": note}
    moved = prior_touched(prior, ranges, since_ranges, line_level=True)
    if moved is True:
        return "deferred", prior
    if moved is False and _verifier_confirmed(prior):
        return "inherited", {**row, "verdict": "confirmed", "note": INHERITED_NOTE}
    return "unverified", row


def render_recheck_cleared_note(cleared: list[dict]) -> str:
    """Names the priors a re-verification at this head refuted (#218/#220)."""
    if not cleared:
        return ""
    lines = "\n".join(
        f"- `{_anchor(m.get('file'), m.get('line'))}` ({m.get('severity') or '?'}) — "
        f"{str(m.get('claim') or '')[:200]}"
        + (f" _(verifier: {str(m.get('recheck_note'))[:200]})_" if m.get("recheck_note") else "")
        for m in cleared
    )
    return (
        "\n\n---\n**Prior finding(s) cleared by re-verification.** The verifier re-read the code at "
        "this head and refuted these; each had either never been verified, or its file changed "
        "since it was raised (#218, #220):\n"
        f"{lines}\n"
    )


def render_deferred_note(deferred: list[dict]) -> str:
    """Names the re-listed priors this round could not re-verify on changed code (#232)."""
    if not deferred:
        return ""
    lines = "\n".join(
        f"- `{_anchor(m.get('file'), m.get('line'))}` ({m.get('severity') or '?'}) — {str(m.get('claim') or '')[:200]}"
        for m in deferred
    )
    return (
        "\n\n---\n**Re-listed prior finding(s) not re-verified.** The panel re-listed these, but the "
        "code they cite changed since they were raised and no verifier ruled on them at this head, "
        "so they do not block this verdict. They are not cleared either: they stay on the record as "
        "carried debt, and the gate holds until a round fixes, refutes, or re-confirms them (#232):\n"
        f"{lines}\n"
    )


def render_unaccounted_note(missing: list[dict]) -> str:
    """Names the prior findings this round left unaccounted, in the body of the round
    that dropped them — silence about a blocker is the thing being made loud."""
    if not missing:
        return ""

    def _tag(m: dict) -> str:
        # The state the record gives it (#260) — "confirmed" only when a verifier said so.
        sev = m.get("severity") or "?"
        if _verifier_confirmed(m):
            return f"{sev}, confirmed"
        return f"{sev}, uncertain" if str(m.get("verdict") or "").lower() == "uncertain" else f"{sev}, not yet verified"

    lines = "\n".join(
        f"- `{_anchor(m.get('file'), m.get('line'))}` ({_tag(m)}) — {str(m.get('claim') or '')[:220]}" for m in missing
    )
    return (
        "\n\n---\n**Unaccounted prior finding(s).** An earlier round of this panel raised "
        "the following, and this round neither reports them, nor says they were fixed, nor "
        "refutes them:\n"
        f"{lines}\n\n"
        "_A finding that disappears without a disposition is unproven, not resolved (issue #26). "
        "Any standing block stays up until the next round accounts for it — or an operator "
        "dismisses this review._"
    )


def render_evidence_gone_note(cleared: list[dict]) -> str:
    """Names the carried priors this round dropped because their quoted evidence is no
    longer at the reviewed head on a line the PR moved since raising them (issue #196)."""
    if not cleared:
        return ""
    lines = "\n".join(
        f"- `{_anchor(m.get('file'), m.get('line'))}` ({m.get('severity') or '?'}) — {str(m.get('claim') or '')[:220]}"
        for m in cleared
    )
    return (
        "\n\n---\n**Prior finding(s) cleared by the delta.** The code these quoted is gone at the "
        "reviewed head, on lines this PR changed since they were raised — treated as fixed "
        "(issue #196):\n"
        f"{lines}\n"
    )


def render_promotion_findings(findings: list[dict]) -> str:
    """Open findings restated in the APPROVE body (issue #22).

    A WARN is non-blocking by design, and approve-on-green promotes it — so thirty
    seconds after a confirmed minor lands, the PR reads APPROVED and the finding has no
    consumer. On projectBoard-plugin#80 a malformed-label defect shipped that way.

    The fix is NOT to make WARN block. This session produced the counterexample: the
    panel issued a twice-confirmed, escalated, hallucinated blocker, and the correct
    outcome was an adjudicated merge past it — gate rigidity must not outrun verdict
    reliability. So the findings ride ALONG with the approval instead: visible to a human
    scanning the PR, and machine-readable for anything gating on the marker.
    """
    if not findings:
        return ""
    lines = "\n".join(
        f"- **{f.get('severity') or '?'}** `{_anchor(f.get('file'), f.get('line'))}` — {str(f.get('claim') or '')[:200]}"
        for f in findings
    )
    return (
        "\n\n**Open findings carried by this approval** — non-blocking, but they did not "
        f"go away:\n{lines}\n\n_Approving a WARN does not resolve its findings (issue #22)._"
    )


def render_held_note(finding: dict) -> str:
    """Why the standing block did NOT lift, in the body of the very verdict that would
    otherwise have lifted it — so the next reader sees the disagreement, not a clean PASS."""
    location = str(finding.get("file") or "(no file)")
    if isinstance(finding.get("line"), int):
        location = f"{location}:{finding['line']}"
    return (
        "\n\n---\n**This PASS does not lift the standing block.** An earlier round of this "
        f"same panel confirmed a {finding.get('severity') or '?'} finding that this round "
        "neither reports nor explains:\n\n"
        f"> `{location}` — {str(finding.get('claim') or '')[:400]}\n\n"
        "A finding that disappears without being fixed, carried, or refuted is unproven, not "
        "resolved — and a clean PASS is exactly the verdict that would clear the merge path "
        "(issue #26). Either the fix landed (say so, and the next review will corroborate and "
        "lift), or the panel missed it on this draw. A second consecutive clean PASS lifts the "
        "block automatically; an operator can also dismiss this review directly."
    )


def render_degraded_note(degraded: list[str]) -> str:
    """Names the finder(s) the engine cut off at their time budget this round. A review
    that ran with fewer angles must say so — the same contract the grounding and
    confinement footnotes keep. A degraded finder is not a failure (the panel still
    produced a verdict), it is a coverage gap the reader should weigh."""
    if not degraded:
        return ""
    steps = ", ".join(f"`{s}`" for s in degraded)
    return (
        f"\n\n---\n_{len(degraded)} panel step(s) hit their time budget and were skipped this "
        f"round: {steps}. The verdict stands on the remaining angles; a finding only that step "
        f"would have caught could be missed — the next push re-runs the full panel._"
    )


def render_incomplete_note(incomplete: list[str]) -> str:
    """Names the finder(s) that ran but did not complete a real pass this round —
    a file read failed, a crash cut it off, or it exhausted its turn budget without
    a real answer (issue #117). Distinct from `render_degraded_note`: that one is the
    engine cutting a step off at its time budget, a known and bounded coverage gap;
    this one is a step that looked like it finished — no timeout, no crash the engine
    saw — but produced nothing trustworthy, which is why it needs its own, blunter
    wording rather than borrowing "hit their time budget"."""
    if not incomplete:
        return ""
    steps = ", ".join(f"`{s}`" for s in incomplete)
    return (
        f"\n\n---\n_{len(incomplete)} panel step(s) did not complete a real pass this "
        f"round: {steps}. The verdict stands on the remaining angles; treat this as "
        f"unreviewed from that angle, not as a clean pass — the next push re-runs the "
        f"full panel._"
    )


def render_notes_section(notes: list[dict]) -> str:
    """The follow-up checklist appended to a converged body — findings the verdict
    stopped carrying, in the form someone can actually act on later."""
    if not notes:
        return ""
    lines = "\n".join(
        f"- [ ] `{n.get('file') or '(no file)'}"
        + (f":{n['line']}" if isinstance(n.get("line"), int) else "")
        + f"` ({n.get('severity') or '?'}) — {str(n.get('claim') or '')[:200]}"
        for n in notes
    )
    return (
        "\n\n---\n**Converged — the following are notes, not gates.** Every finding below "
        "is minor/nit and lands on code that changed in response to this panel's own "
        "earlier rounds, so the verdict no longer holds on them (issue #23). They are "
        "worth doing; they are not worth another review round:\n"
        f"{lines}"
    )

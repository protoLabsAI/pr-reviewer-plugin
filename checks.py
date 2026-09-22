"""The `QA panel` check run — the panel's verdict as a gate GitHub can enforce.

**Why a check and not the review we already post.** An App's *approval* never satisfies
a required approving review: GitHub counts approvals from reviewers with write access,
and an App is not one (its reviews carry `author_association: NONE`). So on protoAgent
the panel could approve a PR and the merge stayed `BLOCKED` — the verdict had no way to
gate anything, however it was posted. A **check run** from the same App is a first-class
required status. So the facts that already drive approve-on-green drive a check run too,
and the same judgement finally blocks a merge: a human's PR and the board's auto-merge
alike (projectBoard-plugin gates on `mergeStateStatus`, which folds in required checks),
with no bot-as-reviewer fiction in the middle.

**What it reports — and what it deliberately doesn't.** This check speaks for the PANEL,
never for the rest of CI. Red or pending CI leaves it *in progress*, not failed: those
already block the merge on their own, and a second red X for the same cause reads as two
problems instead of one. It fails on exactly what the panel is for:

  * a **FAIL** verdict standing against the current head, and
  * a clear verdict whose **own findings nobody resolved** — the WARN case. Quinn's rule
    that WARN "does NOT block merge" is kept as written: a WARN whose threads are all
    resolved goes green. What holds is unaddressed feedback, not the WARN itself.

**Only the panel's OWN threads.** An unresolved review thread another reviewer opened —
CodeRabbit, Quinn, a human — holds *promotion* (approve-on-green fails closed on any open
thread), but it is not the panel's finding and must not fail this check or be reported as
one (issue #105). Ownership is decided server-side, from the thread's root-comment author:
a clear verdict with no open thread of the panel's own goes green even while an external
thread holds the promotion; the summary attributes that hold as external, never as a panel
defect. The panel never authors review threads itself, so in practice its "own" threads are
the rare ones its bot login raised — but the rule is what keeps the two concerns apart.

Everything unknown holds *in progress* rather than failing. A check that fails on an
unreadable thread count — or on threads whose OWNERSHIP we could not read — would block
merges on our own outage, which is the opposite of fail-closed: for a gate, refusing to say
"clear" IS the closed position.

The pure mapping lives here (facts in, state out) and the writing lives in the
dispatcher, the same split `approve.py` uses — so the interesting half is testable
without a GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass

from .approve import (
    HOLD_ALREADY_PROMOTED,
    HOLD_CHECKS_FAILED,
    HOLD_CHECKS_PENDING,
    HOLD_CHECKS_UNKNOWN,
    HOLD_INCOMPLETE,
    HOLD_NO_CLEAR_VERDICT,
    HOLD_STALE_HEAD,
    HOLD_THREADS_UNRESOLVED,
    PROMOTE,
)
from .verdicts import FAIL

# The check run's name — and therefore the ruleset's required-status *context*. Changing
# it silently unrequires the gate (the old context is never reported again, so PRs sit
# "Expected" forever), so it is a constant, not config.
CHECK_NAME = "QA panel"

IN_PROGRESS = "in_progress"
COMPLETED = "completed"
SUCCESS = "success"
FAILURE = "failure"
NEUTRAL = "neutral"


@dataclass(frozen=True)
class CheckRun:
    """What the check should say right now. `conclusion` is set only when completed."""

    status: str
    conclusion: str | None
    title: str
    summary: str


def _threads_phrase(unresolved: int | None) -> str:
    if not unresolved:
        return "review threads"
    return f"{unresolved} unresolved review thread{'s' if unresolved != 1 else ''}"


def check_for(
    decision: str,
    *,
    verdict: str | None = None,
    unresolved: int | None = None,
    panel_unresolved: int | None = None,
) -> CheckRun:
    """Map an approve-on-green decision to the check run to publish.

    `verdict` is the latest panel verdict **for the current head** (None when the panel
    has not spoken for this head yet — a stale verdict is not this head's verdict).
    `unresolved` is the TOTAL unresolved-thread count (every reviewer's), for messaging.
    `panel_unresolved` is how many of those threads the PANEL itself owns — the only ones
    that may fail this check — or None when that ownership could not be read server-side.
    """
    if decision in (PROMOTE, HOLD_ALREADY_PROMOTED):
        return CheckRun(
            COMPLETED,
            SUCCESS,
            "Cleared by the QA panel",
            "The panel's verdict for this head is clear and every finding it raised is resolved.",
        )
    if decision == HOLD_THREADS_UNRESOLVED:
        # Promotion holds on ANY open thread (fail-closed, in the dispatcher). This check,
        # though, speaks only for the PANEL — so it fails on the panel's OWN unaddressed
        # feedback and nothing else. A thread another reviewer opened is an external
        # promotion hold, not a panel finding, and must not turn a clear, finding-free
        # verdict into a red X or be reported as the panel's own (issue #105).
        if panel_unresolved is None:
            # We could not read who owns the open threads, so we can neither claim nor
            # disclaim them as the panel's. An unknown HOLDS — it never fails (the same
            # posture the rest of this file takes toward any unreadable fact).
            return CheckRun(
                IN_PROGRESS,
                None,
                "Promotion held — review threads open",
                f"Promotion is held by {_threads_phrase(unresolved)}, but the panel could not "
                "read who raised them, so it is not concluding this check either way.",
            )
        if panel_unresolved > 0:
            # The WARN gate. The verdict itself is non-blocking; the panel's own
            # unaddressed findings are not. Reported by the panel-owned count, so an
            # external thread never inflates or mislabels the number (issue #105).
            return CheckRun(
                COMPLETED,
                FAILURE,
                f"{_threads_phrase(panel_unresolved)}",
                "The panel's findings are still open. Resolve each thread (fix it, or reply "
                "saying why it stands) and this clears on the next pass.",
            )
        # Every open thread was raised by someone other than the panel. The panel's
        # verdict for this head is clear and none of ITS feedback is outstanding; the
        # merge is held elsewhere, which is a promotion concern, not a panel verdict.
        # Report the clear verdict and attribute the hold as external — never a failure.
        return CheckRun(
            COMPLETED,
            SUCCESS,
            "Cleared by the QA panel — merge held by other review threads",
            "The panel's verdict for this head is clear and every finding it raised is "
            f"resolved. Merge is separately held by {_threads_phrase(unresolved)} the panel "
            "did not raise — a promotion hold, not a panel defect. Resolve those (or a "
            "maintainer merge) to proceed.",
        )
    if decision == HOLD_NO_CLEAR_VERDICT:
        # Two very different states share this hold: a FAIL standing against this head,
        # and no verdict yet at all. Only the first is a failure.
        if verdict == FAIL:
            return CheckRun(
                COMPLETED,
                FAILURE,
                "FAIL verdict",
                "The panel found blocking defects in this head. Push a fix — the next "
                "review clears the verdict, and this check with it.",
            )
        return CheckRun(
            IN_PROGRESS,
            None,
            "Waiting for the panel",
            "No verdict for this head yet.",
        )
    if decision == HOLD_STALE_HEAD:
        return CheckRun(
            IN_PROGRESS,
            None,
            "Re-reviewing the new head",
            "The panel's verdict is for an earlier commit; this head has not been reviewed yet.",
        )
    if decision == HOLD_INCOMPLETE:
        # A clear verdict on incomplete coverage: the panel found nothing blocking in what
        # it DID cover, and a lane it meant to run did not. That withholds approve-on-green
        # (`promotion_decision`, unchanged) — but as an `in_progress` check it also blocked
        # the merge with no way out except a new commit, since only a complete pass
        # cleared it and the sweep does not re-run one on its own (#130). Neutral: not
        # cleared, not failed, and GitHub treats it as passing for a required check — the
        # coverage line in the review body says what was not looked at. The verdict is
        # capped at WARN already (#117); this makes the check agree with the verdict.
        return CheckRun(
            COMPLETED,
            NEUTRAL,
            "Incomplete pass — not blocking",
            "A finder did not run, so the clear verdict covers less than the whole diff. The "
            "review body names the lanes that did not complete. Auto-approve is withheld until "
            "a complete pass; the merge is not.",
        )
    if decision in (HOLD_CHECKS_PENDING, HOLD_CHECKS_FAILED, HOLD_CHECKS_UNKNOWN):
        # CI's business. It already blocks the merge; the panel just hasn't cleared the
        # head yet, and saying so twice in red would double-count one problem.
        return CheckRun(
            IN_PROGRESS,
            None,
            "Waiting on CI",
            "The panel clears a head once its checks are terminal-green.",
        )
    # Every other hold (threads unreadable, backoff, a decision added later) is an
    # unknown, and an unknown is not a failure — it is simply not a clearance.
    return CheckRun(
        IN_PROGRESS,
        None,
        "Not cleared yet",
        f"The panel has not cleared this head ({decision}).",
    )


def closed_run() -> CheckRun:
    """What a still-open run says once its PR closes or merges (#153, #130).

    Every non-terminal state above waits on something — CI going green, a complete pass,
    a verdict — that stops arriving the moment the PR closes: the sweep only evaluates
    open PRs, so nothing ever revisits the run and it sat `in_progress` for good (5 of 28
    merged PRs sampled, one four days old). `neutral`, not success or failure: the panel
    neither cleared this head nor found against it, and a closed PR has no merge left to
    gate. Only ever applied to a run that is still open — a concluded verdict stands.
    """
    return CheckRun(
        COMPLETED,
        NEUTRAL,
        "PR closed before the panel cleared this head",
        "This pull request was closed or merged while the check was still waiting, so "
        "nothing remains for it to gate. The panel did not clear this head.",
    )

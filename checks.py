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
  * a clear verdict whose **findings nobody resolved** — the WARN case. Quinn's rule
    that WARN "does NOT block merge" is kept as written: a WARN whose threads are all
    resolved goes green. What holds is unaddressed feedback, not the WARN itself.

Everything unknown holds *in progress* rather than failing. A check that fails on an
unreadable thread count would block merges on our own outage, which is the opposite of
fail-closed: for a gate, refusing to say "clear" IS the closed position.

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


def check_for(decision: str, *, verdict: str | None = None, unresolved: int | None = None) -> CheckRun:
    """Map an approve-on-green decision to the check run to publish.

    `verdict` is the latest panel verdict **for the current head** (None when the panel
    has not spoken for this head yet — a stale verdict is not this head's verdict).
    `unresolved` is the unresolved-thread count, for the failure's summary.
    """
    if decision in (PROMOTE, HOLD_ALREADY_PROMOTED):
        return CheckRun(
            COMPLETED,
            SUCCESS,
            "Cleared by the QA panel",
            "The panel's verdict for this head is clear and every finding it raised is resolved.",
        )
    if decision == HOLD_THREADS_UNRESOLVED:
        # The WARN gate. The verdict itself is non-blocking; unaddressed findings are not.
        return CheckRun(
            COMPLETED,
            FAILURE,
            f"{_threads_phrase(unresolved)}",
            "The panel's findings are still open. Resolve each thread (fix it, or reply "
            "saying why it stands) and this clears on the next pass.",
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
        return CheckRun(
            IN_PROGRESS,
            None,
            "Incomplete pass",
            "A finder did not run, so the clear verdict covers less than the whole diff. Holding for a complete pass.",
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

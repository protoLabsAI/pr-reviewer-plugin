"""Approve-on-green — ONE pure decision function, used by edge and sweep (ADR 0078 D2).

Quinn's #748/#888: two code paths deciding "promote to APPROVE?" drifted; the cure is
a single pure function over observed facts, called from both the webhook edge and the
level sweep. Her #858/#903 added the unresolved-threads gate to BOTH paths; #901
scoped the sweep. Ported: promote a COMMENTED PASS verdict to a formal APPROVE only
when

    every required check is terminal AND green
  ∧ zero unresolved review threads
  ∧ our posted PASS verdict is for the PR's CURRENT head SHA
  ∧ that verdict hasn't already been promoted (per-head-SHA dedup)
  ∧ no panel round for the PR is dispatched and unfinished (issue #217)

and EVERY unknown — checks unreadable, threads unreadable, no verdict found, verdict
for a stale head — falls through to a typed no-promote. The model is never in this
loop; refusing to promote is always safe (the sweep re-evaluates in 3 minutes).

The function takes OBSERVATIONS (plain values), not clients — trivially testable, and
the caller decides how facts are gathered.
"""

from __future__ import annotations

from dataclasses import dataclass

PROMOTE = "promote"
HOLD_CHECKS_PENDING = "hold:checks-pending"
HOLD_CHECKS_FAILED = "hold:checks-failed"
HOLD_CHECKS_UNKNOWN = "hold:checks-unknown"
HOLD_THREADS_UNRESOLVED = "hold:threads-unresolved"
HOLD_THREADS_UNKNOWN = "hold:threads-unknown"
HOLD_NO_CLEAR_VERDICT = "hold:no-clear-verdict"
HOLD_STALE_HEAD = "hold:stale-head"
HOLD_ALREADY_PROMOTED = "hold:already-promoted"
HOLD_NOT_OWNER = "hold:not-promotion-owner"
HOLD_INCOMPLETE = "hold:incomplete-coverage"
HOLD_UNVERIFIED = "hold:unverified"
HOLD_ROUND_IN_FLIGHT = "hold:round-in-flight"


@dataclass(frozen=True)
class Observations:
    """Facts as observed RIGHT NOW; None always means 'could not read' (fails closed)."""

    head_sha: str  # the PR's current head
    checks_state: str | None  # "green" | "pending" | "failed" | None (unreadable)
    unresolved_threads: int | None  # count | None (unreadable)
    # Head SHA our latest CLEAR (non-blocking: PASS or WARN) verdict names; None = no
    # clear verdict. Quinn's semantics, kept: WARN explicitly "does NOT block merge" —
    # her #888 auto-approves a COMMENTED verdict on green; the unresolved-threads gate
    # is what answers "were the flagged concerns seen/addressed". A promotion that
    # honored only PASS would quietly turn WARN into a forever-block.
    verdict_head: str | None
    verdict_promoted: bool  # the posted marker's promoted flag
    promotion_owner: bool  # this agent owns COMMENTED→APPROVE promotion for the repo
    # Did the panel that produced the clear verdict actually COVER the code — i.e. did
    # every finder that was meant to run actually run? A PASS emitted while a finder was
    # down (protoPatch gateway failure, a finder timeout) is a clean verdict on
    # incomplete analysis, and must not auto-approve (#49). Defaults True so a marker
    # from before this field existed is not retroactively treated as incomplete.
    complete: bool = True
    # False when findings existed but none carry a verdict — the verify pass did not
    # run. Defaults True so a marker from before this field is not retroactively held.
    verified: bool = True
    # Is a panel round for this PR dispatched and not yet finished (or queued to run)?
    # The clear verdict above is the newest COMPLETE round; a round still running on the
    # same head has not spoken yet, and it may FAIL. Promoting now approves the older
    # answer to a question that is being asked again (issue #217: mythxengine-sdk#409
    # approved round 1's PASS 6.5 min into round 2, which then FAILed). None = could not
    # tell, which holds like every other unknown. Defaults False so a caller that does not
    # track rounds keeps its old behaviour.
    round_in_flight: bool | None = False


def promotion_decision(obs: Observations) -> str:
    """'promote' or a typed hold. Order matters only for the reason reported —
    every path that is not provably green holds."""
    if not obs.promotion_owner:
        return HOLD_NOT_OWNER
    if obs.verdict_head is None:
        return HOLD_NO_CLEAR_VERDICT
    if obs.verdict_head != obs.head_sha:
        return HOLD_STALE_HEAD
    if obs.verdict_promoted:
        return HOLD_ALREADY_PROMOTED
    if obs.round_in_flight is not False:
        # After the dedup on purpose: this gate stops a NEW approval racing a round that
        # has not finished. An approval that already stands is corrected by the round
        # itself when it FAILs (`Dispatcher._retract_promotion`), not by flapping the
        # check every time a quick drop/reaffirm briefly holds the PR's slot.
        return HOLD_ROUND_IN_FLIGHT
    if not obs.verified:
        # A PASS nobody verified has not earned approve-on-green. This is the same
        # argument as incomplete coverage one step later in the pipeline: there, a
        # finder never looked; here, nothing checked what the finders claimed.
        return HOLD_UNVERIFIED
    if not obs.complete:
        # A clear verdict on incomplete coverage is not earned: a finder that was meant
        # to run didn't (protoPatch down, a finder timed out), so "no findings" is
        # "nobody looked", not "nothing there". Auto-approve is exactly the leg that can
        # ship an unreviewed PR — hold until a COMPLETE pass clears the head (#49).
        return HOLD_INCOMPLETE
    if obs.checks_state is None:
        return HOLD_CHECKS_UNKNOWN
    if obs.checks_state == "pending":
        return HOLD_CHECKS_PENDING
    if obs.checks_state != "green":
        return HOLD_CHECKS_FAILED
    if obs.unresolved_threads is None:
        return HOLD_THREADS_UNKNOWN
    if obs.unresolved_threads > 0:
        return HOLD_THREADS_UNRESOLVED
    return PROMOTE

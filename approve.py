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

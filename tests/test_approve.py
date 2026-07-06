"""The approve-on-green decision function — the fail-closed matrix, enumerated."""

from __future__ import annotations

from pr_reviewer.approve import (
    HOLD_ALREADY_PROMOTED,
    HOLD_CHECKS_FAILED,
    HOLD_CHECKS_PENDING,
    HOLD_CHECKS_UNKNOWN,
    HOLD_NO_PASS_VERDICT,
    HOLD_NOT_OWNER,
    HOLD_STALE_HEAD,
    HOLD_THREADS_UNKNOWN,
    HOLD_THREADS_UNRESOLVED,
    PROMOTE,
    Observations,
    promotion_decision,
)

HEAD = "a" * 40


def obs(**over):
    base = dict(
        head_sha=HEAD,
        checks_state="green",
        unresolved_threads=0,
        verdict_head=HEAD,
        verdict_promoted=False,
        promotion_owner=True,
    )
    base.update(over)
    return Observations(**base)


def test_the_one_green_path_promotes():
    assert promotion_decision(obs()) == PROMOTE


def test_every_unknown_falls_through_fail_closed():
    assert promotion_decision(obs(checks_state=None)) == HOLD_CHECKS_UNKNOWN
    assert promotion_decision(obs(unresolved_threads=None)) == HOLD_THREADS_UNKNOWN
    assert promotion_decision(obs(verdict_head=None)) == HOLD_NO_PASS_VERDICT


def test_non_green_facts_hold():
    assert promotion_decision(obs(checks_state="pending")) == HOLD_CHECKS_PENDING
    assert promotion_decision(obs(checks_state="failed")) == HOLD_CHECKS_FAILED
    assert promotion_decision(obs(unresolved_threads=2)) == HOLD_THREADS_UNRESOLVED


def test_stale_head_and_dedup_hold():
    assert promotion_decision(obs(verdict_head="b" * 40)) == HOLD_STALE_HEAD
    assert promotion_decision(obs(verdict_promoted=True)) == HOLD_ALREADY_PROMOTED


def test_promotion_ownership_gates_everything():
    # Two promoters racing is how double-merges happen — not-owner holds even on
    # a perfectly green PR (Quinn keeps promotion until per-repo handover).
    assert promotion_decision(obs(promotion_owner=False)) == HOLD_NOT_OWNER

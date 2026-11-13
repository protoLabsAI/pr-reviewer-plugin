"""Issue #357: a finder lane that STILL fails after the retry becomes a coverage Gap, not
an exhausted round — provided the other finder lanes delivered.

10+ times in one day a single finder (`find_crossfile`, `find_conventions`) failed for a
non-overrun reason while every other lane delivered, and the whole round exhausted with no
verdict, a red protoReview check, and an operator escalation. #176 already does the right
thing for a context overrun (`overran` → degraded Gap); this generalises it to any finder
failure, but only AFTER the retry is spent. A critical step (synthesize/verify/report), any
non-finder step, or EVERY finder failing is still D3 exhaustion."""

from __future__ import annotations

import pytest
from pr_reviewer.verdicts import coverage_gaps

from tests.dispatch_helpers import HEAD, accounted_empty_panel, make
from tests.test_dispatch import (
    _CLEAN_REPORT,
    LLM_LANES,
    _events,
    _panel_steps,
    _structural_gh,
)

_BOOM = "Error: step '{s}' raised SubagentError: boom"


def _fail_every_attempt(*steps: str):
    """A runner that fails `steps` on EVERY attempt; every other lane delivers an empty
    pass. A failed finder lane also carries its error as its step output (the real shape)."""
    calls = {"n": 0}

    async def runner(name, inputs):
        calls["n"] += 1
        over = {s: _BOOM.format(s=s) for s in steps if s.startswith("find_")}
        return {"output": _CLEAN_REPORT, "failed": list(steps), "steps": _panel_steps(**over)}

    return runner, calls


# ── r1: one finder fails every attempt, the others deliver → WARN Gap ────────────


async def test_a_finder_that_fails_after_the_retry_is_a_gap_not_an_exhausted_round(tmp_path):
    runner, calls = _fail_every_attempt("find_crossfile")
    gh = _structural_gh()
    d = make(tmp_path, cfg={"shadow_mode": False, "panel_retries": 1}, gh=gh, runner=runner)

    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    assert calls["n"] == 2  # retried once, THEN carried — not discarded

    body = gh.reviews_posted[0]["body"]
    assert "verdict=WARN" in body and "complete=false" in body
    assert "`find_crossfile` (failed after the retry" in body

    assert _events(tmp_path, "exhaustion") == []
    assert [e["lanes"] for e in _events(tmp_path, "finder_failed")] == [["find_crossfile"]]
    failed_event = _events(tmp_path, "finder_failed")[0]
    assert failed_event["attempt"] == 2 and failed_event["sha"] == HEAD
    row = _events(tmp_path, "reviewed")[-1]
    assert row["finder_failed"] == ["find_crossfile"] and row["complete"] is False


# ── r2: a finder that recovers on the retry posts a complete verdict ─────────────


async def test_a_finder_that_delivers_on_the_retry_posts_a_complete_verdict(tmp_path):
    calls = {"n": 0}

    async def runner(name, inputs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "output": _CLEAN_REPORT,
                "failed": ["find_crossfile"],
                "steps": _panel_steps(find_crossfile=_BOOM.format(s="find_crossfile")),
            }
        return accounted_empty_panel({"output": _CLEAN_REPORT, "failed": [], "steps": _panel_steps()})

    gh = _structural_gh()
    d = make(tmp_path, cfg={"shadow_mode": False, "panel_retries": 1}, gh=gh, runner=runner)

    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    assert calls["n"] == 2
    body = gh.reviews_posted[0]["body"]
    # A complete marker carries no `complete=` attribute at all (it defaults to complete).
    assert "complete=false" not in body and "failed after the retry" not in body
    assert _events(tmp_path, "finder_failed") == []
    assert _events(tmp_path, "exhaustion") == []
    row = _events(tmp_path, "reviewed")[-1]
    assert row["complete"] is True and row.get("finder_failed") is None


# ── r3: critical / compound / all-finder failures still exhaust ──────────────────


@pytest.mark.parametrize(
    "failed",
    [
        ["synthesize"],
        ["verify"],
        ["report"],
        ["find_crossfile", "synthesize"],  # a finder PLUS a critical step
        [*LLM_LANES, "find_structural"],  # every finder failed — no lane delivered
    ],
    ids=["synthesize", "verify", "report", "finder+critical", "every-finder"],
)
async def test_a_non_finder_or_total_failure_still_exhausts(tmp_path, failed):
    runner, calls = _fail_every_attempt(*failed)
    gh = _structural_gh()
    d = make(tmp_path, cfg={"shadow_mode": False, "panel_retries": 1}, gh=gh, runner=runner, inbox=lambda t, **k: None)

    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-exhausted"
    assert calls["n"] == 2  # retried, then exhausted
    assert gh.reviews_posted == []  # D3: no verdict posted
    assert _events(tmp_path, "finder_failed") == []
    assert _events(tmp_path, "exhaustion")  # telemetered as an exhaustion, as before


# ── r4: coverage_gaps carries the failed lanes, and is unchanged when omitted ────


def test_coverage_gaps_includes_failed_lanes():
    gaps = coverage_gaps([], [], False, failed_lanes=["find_conventions"])
    assert gaps == {"find_conventions": "failed after the retry"}


def test_coverage_gaps_is_unchanged_when_failed_lanes_is_omitted():
    assert coverage_gaps([], [], False) == {}
    assert coverage_gaps(["verify"], [], False) == {"verify": "hit its time budget"}

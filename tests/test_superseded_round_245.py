"""Issue #245 — a panel on a superseded head stops at the next step boundary.

pr-reviewer-plugin#242 (2026-10-02): a panel dispatched on `6e3553c8` kept running for 44+
minutes after a force-push to `071c712c`, whose `synchronize` was dropped `in-flight`. It
held one of three panel slots for a verdict that could only land on a head the PR no longer
pointed at (#211 keeps it off the new head).

Now a round checks the PR's head at the synthesize/verify boundary, and at every boundary
once an event signals a newer head. If the head is readable and has moved, it cancels the
attempt, posts nothing, records `superseded`, and hands its slot straight to the new head.
The new head is admitted before the old slot is released, so there is no gap.
"""

from __future__ import annotations

import asyncio

from pr_reviewer.chokepoint import DROP_IN_FLIGHT
from pr_reviewer.dispatch import PanelQueue
from pr_reviewer.verdicts import parse_verdict_marker

from tests.test_dispatch import REPORT, RoutedGH, facts, make

OLD = "1" * 40
NEW = "2" * 40
GREEN = [{"status": "completed", "conclusion": "success", "name": "CI"}]


class MovableGH(RoutedGH):
    """A PR whose head can be force-pushed mid-round, and whose facts read can fail."""

    def __init__(self, **kw):
        super().__init__(pr_facts=facts(head=OLD), reviews=[], checks=GREEN, **kw)
        self.facts_unreadable = False

    async def __call__(self, args, timeout=30):
        if self.facts_unreadable and len(args) > 1 and args[1] == "repos/o/r/pulls/1":
            self.calls.append(args)
            return 1, "", "HTTP 502"
        return await super().__call__(args, timeout)


class StepRunner:
    """A host runner that reports steps via `on_step`. The round on OLD parks in the finders
    until the test releases it — the window in which the branch is force-pushed."""

    def __init__(self, *, swallow_cancel=False):
        self.heads: list[str] = []
        self.release = asyncio.Event()
        self.parked = asyncio.Event()
        self.swallow_cancel = swallow_cancel
        self.on_start = None  # optional probe, called with the head as each round starts

    async def __call__(self, name, inputs, on_step=None):
        head = inputs["head_sha"]
        self.heads.append(head)
        if self.on_start:
            self.on_start(head)
        try:
            on_step("find_correctness")
            if head == OLD:
                self.parked.set()
                await self.release.wait()
            for step in ("synthesize", "verify", "report"):
                on_step(step)
                for _ in range(20):  # the step's own work: the cancel lands in here
                    await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            if not self.swallow_cancel:
                raise
        return {"output": REPORT, "steps": {}, "failed": []}


def _posted_heads(gh) -> list[str]:
    return [(parse_verdict_marker(p.get("body") or "") or {}).get("head") for p in gh.reviews_posted]


def _events(d, name):
    return [e for e in d.telemetry.read_all() if e.get("event") == name]


async def _force_push_mid_round(d, gh, runner, round_coro):
    """Start a round on OLD, force-push to NEW while it is in the finders, deliver the
    `synchronize` (dropped in-flight, as on #242), then let the round reach its boundary."""
    task = asyncio.create_task(round_coro)
    await asyncio.wait_for(runner.parked.wait(), 3)
    gh.pr_facts = facts(head=NEW)
    assert (await d.handle_pr_event("o/r", 1, NEW, "synchronize")) == f"drop:{DROP_IN_FLIGHT}"
    runner.release.set()
    return await asyncio.wait_for(task, 5)


async def test_a_superseded_round_stops_posts_nothing_and_hands_its_slot_to_the_new_head(tmp_path):
    gh, runner = MovableGH(), StepRunner()
    d = make(tmp_path, gh=gh, runner=runner)

    outcome = await _force_push_mid_round(d, gh, runner, d.handle_pr_event("o/r", 1, OLD, "opened"))

    assert outcome == "reviewed:FAIL"  # the NEW head's verdict, reviewed in the same slot
    assert runner.heads == [OLD, NEW]
    assert _posted_heads(gh) == [NEW]  # nothing posted for the superseded head
    sup = [e for e in _events(d, "superseded") if e.get("cancelled")]
    assert sup and sup[0]["sha"] == OLD and sup[0]["new_head"] == NEW and sup[0]["phase"] == "synthesize"
    assert _events(d, "superseded_handoff")
    assert not d.chokepoint._in_flight  # no leaked slot, old or new
    # The old head's protoReview run is concluded, not left dangling in_progress.
    assert any(w.get("conclusion") == "neutral" and "Superseded" in w.get("output[title]", "") for w in gh.check_writes)


async def test_there_is_no_promotion_window_between_the_cancel_and_the_new_round(tmp_path):
    gh, runner = MovableGH(), StepRunner()
    d = make(tmp_path, gh=gh, runner=runner)
    seen_at_new_start: list[bool] = []
    runner.on_start = lambda head: head == NEW and seen_at_new_start.append(d._round_in_flight("o/r", 1))
    after_done: list[bool] = []
    real_done = d.chokepoint.done

    def done(repo, pr, sha=None):
        real_done(repo, pr, sha)
        after_done.append(d.chokepoint.in_flight(repo, pr))

    d.chokepoint.done = done

    await _force_push_mid_round(d, gh, runner, d.handle_pr_event("o/r", 1, OLD, "opened"))

    assert seen_at_new_start == [True]
    # Every release but the last left the PR with a live slot: the new head was admitted
    # BEFORE the old one was let go, so promotion never saw "nothing running".
    assert after_done and all(after_done[:-1]) and after_done[-1] is False


async def test_an_unreadable_head_cancels_nothing(tmp_path):
    gh, runner = MovableGH(), StepRunner()
    d = make(tmp_path, gh=gh, runner=runner)
    task = asyncio.create_task(d.handle_pr_event("o/r", 1, OLD, "opened"))
    await asyncio.wait_for(runner.parked.wait(), 3)
    d._hint_newer_head("o/r", 1, NEW)  # an event said the head moved…
    gh.facts_unreadable = True  # …but GitHub cannot confirm it
    runner.release.set()
    await asyncio.sleep(0.15)  # past the synthesize/verify boundary checks
    gh.facts_unreadable = False
    assert (await asyncio.wait_for(task, 5)) == "reviewed:FAIL"
    assert runner.heads == [OLD]
    assert not [e for e in _events(d, "superseded") if e.get("cancelled")]
    assert not d.chokepoint._in_flight


async def test_an_unmoved_head_is_checked_at_the_tail_and_not_cancelled(tmp_path):
    gh, runner = MovableGH(), StepRunner()
    runner.release.set()
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, OLD, "opened")) == "reviewed:FAIL"
    assert runner.heads == [OLD]
    assert _posted_heads(gh) == [OLD]
    assert not [e for e in _events(d, "superseded") if e.get("cancelled")]


async def test_when_another_round_already_holds_the_new_head_the_handoff_stops(tmp_path):
    # A backfill admitted past the superseded round (#209) is already reviewing NEW; the
    # cancelled round must not start a second panel on it.
    gh, runner = MovableGH(), StepRunner()
    d = make(tmp_path, gh=gh, runner=runner)
    task = asyncio.create_task(d.handle_pr_event("o/r", 1, OLD, "opened"))
    await asyncio.wait_for(runner.parked.wait(), 3)
    gh.pr_facts = facts(head=NEW)
    assert d.chokepoint.admit("o/r", 1, NEW, supersede_stale=True) == "accept"  # the other round
    d._hint_newer_head("o/r", 1, NEW)
    runner.release.set()

    assert (await asyncio.wait_for(task, 5)) == "drop:superseded"
    assert runner.heads == [OLD]  # no second panel
    assert gh.reviews_posted == []
    assert any(e.get("action") == "superseded-handoff" and e.get("reason") == "in-flight" for e in _events(d, "drop"))
    assert list(d.chokepoint._in_flight["o/r#1"]) == [NEW]  # only the other round's slot


async def test_a_host_that_swallows_the_cancel_still_has_its_result_discarded(tmp_path):
    gh, runner = MovableGH(), StepRunner(swallow_cancel=True)
    d = make(tmp_path, gh=gh, runner=runner)
    await _force_push_mid_round(d, gh, runner, d.handle_pr_event("o/r", 1, OLD, "opened"))
    assert OLD not in _posted_heads(gh)
    assert [e for e in _events(d, "superseded") if e.get("cancelled")]


async def test_a_summon_round_hands_off_too(tmp_path):
    gh, runner = MovableGH(), StepRunner()
    d = make(tmp_path, gh=gh, runner=runner)
    outcome = await _force_push_mid_round(d, gh, runner, d.handle_summon("o/r", 1, "operator"))
    assert outcome == "reviewed:FAIL" and runner.heads == [OLD, NEW]
    assert _posted_heads(gh) == [NEW]
    assert not d.chokepoint._in_flight


async def test_a_backfill_round_frees_both_its_chokepoint_and_queue_slots(tmp_path):
    gh, runner = MovableGH(), StepRunner()
    d = make(tmp_path, gh=gh, runner=runner)
    queue = PanelQueue(1)
    d.panel_sem = queue
    outcome = await _force_push_mid_round(d, gh, runner, d.backfill_review("o/r", 1, OLD))
    assert outcome == "reviewed:FAIL" and runner.heads == [OLD, NEW]
    assert not d.chokepoint._in_flight
    assert queue._active == [] and queue._pending == [] and not queue.locked()

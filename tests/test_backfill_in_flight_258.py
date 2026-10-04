"""Issue #258: the sweep's backfill started a second panel for a PR whose round was running.

Two live traces (Vera, 2026-10-03):

* protoAgent#4023, SAME sha (the #89 duplicate). A queued push event for the older head
  `4c8ac0c1` got its panel slot after the PR had moved to `9e890531`. `handle_pr_event`
  keys the chokepoint slot by the EVENT's sha, so round A held `…@4c8ac0c1` while
  reviewing the resolved head `9e890531`. The backfill's sha-keyed
  `admit(9e890531, supersede_stale=True)` read the `4c8ac0c1` slot as a superseded
  head's and admitted a second panel on `9e890531`. Both posted.
* data-plugin#1, NEWER head: the backfill admitted the new head beside a running
  older-head round, which then only stopped at its synthesize boundary, 12 minutes after
  its head was superseded.

Now the backfill consults the PR-wide in-flight state (`_round_in_flight`: chokepoint plus
panel queue). For a newer head it sets #245's hint instead. A timer inside the running
step reads that hint, so the old round stops mid-finder and hands its slot to the new head.
"""

from __future__ import annotations

import asyncio

from pr_reviewer.chokepoint import DROP_IN_FLIGHT
from pr_reviewer.dispatch import PanelQueue

from tests.test_dispatch import REPORT, facts, make
from tests.test_superseded_round_245 import NEW, OLD, MovableGH, _events, _posted_heads

STALE_EVENT = "4" * 40  # protoAgent#4023's `4c8ac0c1`: an older push's event, delivered late


class LongFinderRunner:
    """A host runner stuck in ONE long finder step: no further step boundary until the test
    releases it. Tracks how many panels run at once."""

    def __init__(self, *, steps=True):
        self.heads: list[str] = []
        self.parked = asyncio.Event()
        self.release = asyncio.Event()
        self.running = 0
        self.peak = 0
        self.steps = steps

    async def _run(self, inputs, on_step):
        self.heads.append(inputs["head_sha"])
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if on_step:
                on_step("find_correctness")
            if len(self.heads) == 1:
                self.parked.set()
                await self.release.wait()  # the long finder
            return {"output": REPORT, "steps": {}, "failed": []}
        finally:
            self.running -= 1

    async def __call__(self, name, inputs, on_step=None):
        return await self._run(inputs, on_step if self.steps else None)


class NoStepRunner(LongFinderRunner):
    """An older host: no `on_step` at all, so no step boundary ever reaches the dispatcher."""

    async def __call__(self, name, inputs):
        return await self._run(inputs, None)


# ── the same-sha duplicate (protoAgent#4023) ─────────────────────────────────────


async def test_a_backfill_never_duplicates_a_round_keyed_by_a_stale_event_sha(tmp_path):
    gh = MovableGH()
    gh.pr_facts = facts(head=OLD)  # the PR is already at OLD (`9e890531`)…
    runner = LongFinderRunner()
    d = make(tmp_path, cfg={"supersede_poll_s": 0}, gh=gh, runner=runner)

    # …when an older push's queued event (`4c8ac0c1`) gets its turn: slot keyed by the
    # EVENT sha, round reviewing the RESOLVED head.
    round_a = asyncio.create_task(d.handle_pr_event("o/r", 1, STALE_EVENT, "synchronize"))
    await asyncio.wait_for(runner.parked.wait(), 3)
    assert runner.heads == [OLD]
    assert set(d.chokepoint._in_flight["o/r#1"]) == {STALE_EVENT}  # keyed by the EVENT sha

    assert (await d.backfill_review("o/r", 1, OLD)) == f"drop:{DROP_IN_FLIGHT}"
    assert runner.heads == [OLD] and runner.peak == 1  # no second panel on the same sha

    runner.release.set()
    assert (await asyncio.wait_for(round_a, 5)) == "reviewed:FAIL"
    assert _posted_heads(gh) == [OLD]  # one verdict, not two
    assert not d.chokepoint._in_flight


async def test_the_sweep_does_not_duplicate_the_running_round_either(tmp_path):
    gh = MovableGH()
    runner = LongFinderRunner()
    d = make(tmp_path, cfg={"supersede_poll_s": 0}, gh=gh, runner=runner)
    round_a = asyncio.create_task(d.handle_pr_event("o/r", 1, STALE_EVENT, "synchronize"))
    await asyncio.wait_for(runner.parked.wait(), 3)

    await d.sweep_once()
    await asyncio.sleep(0.05)
    assert runner.peak == 1
    runner.release.set()
    await asyncio.wait_for(round_a, 5)
    await d.drain_backfills()
    assert runner.heads == [OLD] and _posted_heads(gh) == [OLD]


async def test_a_queued_summon_keeps_the_backfill_out(tmp_path):
    # PR-wide includes the panel queue: a summon waiting for a cross-PR slot will run a
    # panel on this PR, so a backfill must not start one beside it.
    d = make(tmp_path, gh=MovableGH())
    queue = PanelQueue(1)
    d.panel_sem = queue
    other = queue.slot(repo="o/r", pr=99, kind="summon")
    await other.__aenter__()
    waiting = asyncio.create_task(queue.slot(repo="o/r", pr=1, kind="summon").__aenter__())
    await asyncio.sleep(0)
    try:
        assert (await d.backfill_review("o/r", 1, OLD)) == f"drop:{DROP_IN_FLIGHT}"
        assert not d.chokepoint._in_flight  # it never took a slot
    finally:
        waiting.cancel()
        await other.__aexit__(None, None, None)
        await asyncio.gather(waiting, return_exceptions=True)


# ── the newer head (data-plugin#1): hint, cancel mid-finder, hand off ─────────────


async def _newer_head_while_a_long_finder_runs(tmp_path, runner, *, poll=0.02, unreadable=False):
    gh = MovableGH()
    d = make(tmp_path, cfg={"supersede_poll_s": poll}, gh=gh, runner=runner)
    after_done: list[bool] = []
    real_done = d.chokepoint.done

    def done(repo, pr, sha=None):
        real_done(repo, pr, sha)
        after_done.append(d.chokepoint.in_flight(repo, pr))

    d.chokepoint.done = done
    round_a = asyncio.create_task(d.handle_pr_event("o/r", 1, OLD, "opened"))
    await asyncio.wait_for(runner.parked.wait(), 3)
    gh.pr_facts = facts(head=NEW)  # pushed while A is deep in a finder
    gh.facts_unreadable = unreadable
    assert (await d.backfill_review("o/r", 1, NEW)) == f"drop:{DROP_IN_FLIGHT}"
    assert d._newer_head_hint.get("o/r#1") == NEW
    return d, gh, round_a, after_done


async def test_a_backfill_for_a_newer_head_stops_the_old_round_mid_finder_and_hands_off(tmp_path):
    runner = LongFinderRunner()
    d, gh, round_a, after_done = await _newer_head_while_a_long_finder_runs(tmp_path, runner)

    # The old round is NOT released: no step boundary ever comes. The poll alone stops it.
    assert (await asyncio.wait_for(round_a, 5)) == "reviewed:FAIL"
    assert runner.heads == [OLD, NEW] and runner.peak == 1  # sequential, never two at once
    assert _posted_heads(gh) == [NEW]
    sup = [e for e in _events(d, "superseded") if e.get("cancelled")]
    assert sup and sup[0]["sha"] == OLD and sup[0]["new_head"] == NEW and sup[0]["phase"] == "finders"
    # #233's invariants: no leaked slot, and no moment with nothing in flight before the end.
    assert not d.chokepoint._in_flight
    assert after_done and all(after_done[:-1]) and after_done[-1] is False


async def test_the_poll_also_covers_a_host_without_step_callbacks(tmp_path):
    runner = NoStepRunner()
    _d, gh, round_a, _after = await _newer_head_while_a_long_finder_runs(tmp_path, runner)
    assert (await asyncio.wait_for(round_a, 5)) == "reviewed:FAIL"
    assert runner.heads == [OLD, NEW] and _posted_heads(gh) == [NEW]


async def test_an_unreadable_head_lets_the_old_round_run_on(tmp_path):
    runner = LongFinderRunner()
    d, gh, round_a, _after = await _newer_head_while_a_long_finder_runs(tmp_path, runner, unreadable=True)
    await asyncio.sleep(0.15)  # several polls, every read failing
    assert not round_a.done() and runner.heads == [OLD]  # fail-closed: nothing cancelled
    gh.facts_unreadable = False
    gh.pr_facts = facts(head=OLD)  # (the push turned out not to matter; the round finishes)
    runner.release.set()
    assert (await asyncio.wait_for(round_a, 5)) == "reviewed:FAIL"
    assert not [e for e in _events(d, "superseded") if e.get("cancelled")]


async def test_without_a_hint_the_poll_reads_nothing(tmp_path):
    gh = MovableGH()
    runner = LongFinderRunner()
    d = make(tmp_path, cfg={"supersede_poll_s": 0.01}, gh=gh, runner=runner)
    round_a = asyncio.create_task(d.handle_pr_event("o/r", 1, OLD, "opened"))
    await asyncio.wait_for(runner.parked.wait(), 3)
    reads = sum(1 for c in gh.calls if len(c) > 1 and c[1] == "repos/o/r/pulls/1")
    await asyncio.sleep(0.1)  # ~10 polls
    assert sum(1 for c in gh.calls if len(c) > 1 and c[1] == "repos/o/r/pulls/1") == reads
    runner.release.set()
    await asyncio.wait_for(round_a, 5)


async def test_a_hint_for_the_rounds_own_head_reads_nothing(tmp_path):
    # A redelivery of the SAME head dropped in-flight hints that head; it is not newer.
    gh = MovableGH()
    runner = LongFinderRunner()
    d = make(tmp_path, cfg={"supersede_poll_s": 0.01}, gh=gh, runner=runner)
    round_a = asyncio.create_task(d.handle_pr_event("o/r", 1, OLD, "opened"))
    await asyncio.wait_for(runner.parked.wait(), 3)
    d._hint_newer_head("o/r", 1, OLD)
    reads = sum(1 for c in gh.calls if len(c) > 1 and c[1] == "repos/o/r/pulls/1")
    await asyncio.sleep(0.1)
    assert sum(1 for c in gh.calls if len(c) > 1 and c[1] == "repos/o/r/pulls/1") == reads
    runner.release.set()
    await asyncio.wait_for(round_a, 5)


def test_the_poll_interval_is_clamped_and_zero_disables(tmp_path):
    assert make(tmp_path).supersede_poll_s == 30
    assert make(tmp_path, cfg={"supersede_poll_s": 0}).supersede_poll_s == 0
    assert make(tmp_path, cfg={"supersede_poll_s": 99999}).supersede_poll_s == 600
    assert make(tmp_path, cfg={"supersede_poll_s": "junk"}).supersede_poll_s == 30

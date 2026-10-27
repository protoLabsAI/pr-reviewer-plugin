"""Issue #297: the sweep re-panelled an exhausted head on every tick, with no backoff.

Vera, 2026-10-24: protoAgent#4505 @ `e1972b9` exhausted 14 times in 2.9 h, every round
refused at synthesize (#289), and never got a verdict. Across 459 exhaustions since July,
56% were repeats on a head that had already exhausted 3+ times (rabbit-hole.io#387: 59).

The retry is right — outages end, and most of those heads did get a verdict — so the sweep
now waits `exhaustion_backoff · 2^(n-1)` (capped) after the n-th exhaustion of a head.
A new head, a push, or a summon is never held back.
"""

from __future__ import annotations

from pr_reviewer.dispatch import DROP_EXHAUSTION_BACKOFF

from tests.dispatch_helpers import HEAD, REPORT, RoutedGH, facts, make
from tests.test_dispatch import NEXT_HEAD, _events

CFG = {"shadow_mode": False, "promotion_owner": True, "exhaustion_backoff": 900, "exhaustion_backoff_max": 3600}


def _runner(ran: list[str], *, fail: bool = True):
    async def runner(name, inputs):
        ran.append(inputs["head_sha"])
        if fail:
            return {"output": "partial", "failed": ["find_crossfile"]}
        return {"output": REPORT, "failed": []}

    return runner


def _age(d, seconds: float) -> None:
    """Pretend the last exhaustion happened `seconds` earlier."""
    key = "o/r#1"
    head, count, at = d._exhausted[key]
    d._exhausted[key] = (head, count, at - seconds)


async def test_an_exhausted_head_is_not_backfilled_again_inside_its_backoff(tmp_path):
    ran: list[str] = []
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(ran))
    d.chokepoint.cooldown_s = 0  # the per-head cooldown (`or 30` reads 0 as unset) is not under test

    await d.reconcile_pr("o/r", 1, backfill_budget=1)
    panels = len(ran)
    assert panels  # the first backfill ran (and exhausted)

    _outcome, budget = await d.reconcile_pr("o/r", 1, backfill_budget=1)
    assert len(ran) == panels  # the next tick does not re-panel the same head
    assert budget == 1  # and spends no backfill budget on it
    drops = [e for e in _events(tmp_path, "drop") if e.get("reason") == DROP_EXHAUSTION_BACKOFF]
    assert drops and drops[-1]["sha"] == HEAD and 0 < drops[-1]["wait_s"] <= 900

    _age(d, 901)
    await d.reconcile_pr("o/r", 1, backfill_budget=1)
    assert len(ran) == 2 * panels  # once the window passes, the retry still happens


async def test_the_backoff_doubles_per_exhaustion_up_to_its_cap(tmp_path):
    d = make(tmp_path, cfg=CFG, gh=RoutedGH(pr_facts=facts(), reviews=[]))
    waits = []
    for _ in range(5):
        d._note_exhaustion("o/r", 1, HEAD)
        waits.append(round(d._exhaustion_backoff_left("o/r", 1, HEAD), -1))
    assert waits == [900, 1800, 3600, 3600, 3600]


async def test_a_new_head_starts_over_and_is_backfilled_at_once(tmp_path):
    d = make(tmp_path, cfg=CFG, gh=RoutedGH(pr_facts=facts(), reviews=[]))
    for _ in range(3):
        d._note_exhaustion("o/r", 1, HEAD)
    assert d._exhaustion_backoff_left("o/r", 1, NEXT_HEAD) == 0
    d._note_exhaustion("o/r", 1, NEXT_HEAD)
    assert d._exhausted["o/r#1"][:2] == (NEXT_HEAD, 1)


async def test_a_summon_is_never_held_by_the_backoff(tmp_path):
    ran: list[str] = []
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(ran, fail=False))
    d._note_exhaustion("o/r", 1, HEAD)
    await d.handle_summon("o/r", 1, "a-human")
    assert ran  # "@vera review" still runs the panel inside the window


async def test_zero_disables_the_backoff(tmp_path):
    d = make(tmp_path, cfg={**CFG, "exhaustion_backoff": 0}, gh=RoutedGH(pr_facts=facts(), reviews=[]))
    d._note_exhaustion("o/r", 1, HEAD)
    assert d._exhaustion_backoff_left("o/r", 1, HEAD) == 0

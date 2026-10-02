"""Issue #217 — promotion raced a round in flight; a summon raced a sub-second slot hold.

mythxengine-sdk#409, one unchanged head (Vera telemetry, 2026-09-27):

    03:41  reviewed round=1 PASS
    04:13  summon → dispatch round=2
    04:20  promotion decision=promote          ← round 2 still running
    04:27  reviewed round=2 FAIL               ← APPROVED + green `QA panel` stand beside it
    04:36  summon drop reason=in-flight        ← 0.3s later: reaffirm verdict=FAIL

1. Approve-on-green approved round 1's PASS while round 2 was dispatched and unfinished.
   Promotion now holds (`hold:round-in-flight`) while any round for the PR runs or waits.
2. The converse: when that later round FAILs, it withdraws the approval and writes the
   `QA panel` check red itself — before, both stayed until a sweep pass that a draft PR
   never gets.
3. The 04:36 drop was not a stale slot: a `ready_for_review` webhook delivered alongside
   the summon held the slot for the 0.3s its reaffirm took. Sha-keyed slots (#209/#211)
   free correctly on every exit; the summon now waits out a sub-second hold.
4. The reply said "in-flight: nothing ran. Try again once the current review finishes."
   for every drop reason; each reason now says what actually happened.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from pr_reviewer.approve import (
    HOLD_ALREADY_PROMOTED,
    HOLD_NO_CLEAR_VERDICT,
    HOLD_ROUND_IN_FLIGHT,
    PROMOTE,
    Observations,
    promotion_decision,
)
from pr_reviewer.checks import COMPLETED, FAILURE, IN_PROGRESS, check_for
from pr_reviewer.chokepoint import DROP_IN_FLIGHT, Chokepoint
from pr_reviewer.dispatch import PanelQueue
from pr_reviewer.summon import outcome_reply

from tests.test_dispatch import HEAD, RoutedGH, facts, make, promotion_row, review_row

GREEN = [{"status": "completed", "conclusion": "success", "name": "CI"}]
OWNER = {"shadow_mode": False, "promotion_owner": True}


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


def qa_check_writes(gh) -> list[dict]:
    """The `QA panel` check-run writes (POST create / PATCH update), in order."""
    return [p for p in gh.posted if "check-runs" in p.get("url", "")] + [
        {"url": c[1], **{a.split("=", 1)[0]: a.split("=", 1)[1] for a in c if "=" in a}}
        for c in gh.calls
        if len(c) > 1 and "check-runs/" in c[1] and "PATCH" in c
    ]


def promoted(gh) -> bool:
    return any(p.get("event") == "APPROVE" for p in gh.reviews_posted)


# ── 1. the pure decision ────────────────────────────────────────────────────────


def test_a_round_in_flight_holds_promotion():
    assert promotion_decision(obs()) == PROMOTE
    assert promotion_decision(obs(round_in_flight=True)) == HOLD_ROUND_IN_FLIGHT


def test_an_unknown_in_flight_state_holds_too():
    # Fail closed: "could not tell" is not "nothing running".
    assert promotion_decision(obs(round_in_flight=None)) == HOLD_ROUND_IN_FLIGHT


def test_an_approval_that_already_stands_is_not_flapped_by_a_round_in_flight():
    # The in-flight hold stops a NEW approval; an existing one is corrected by the round
    # itself if it FAILs. Otherwise every sub-second slot hold would flip the green check.
    assert promotion_decision(obs(verdict_promoted=True, round_in_flight=True)) == HOLD_ALREADY_PROMOTED


def test_a_fail_standing_still_reads_as_the_fail_not_as_in_flight():
    # Order: no clear verdict is reported first, so a re-review of a FAILed head keeps the
    # check red rather than softening it to "in progress".
    assert promotion_decision(obs(verdict_head=None, round_in_flight=True)) == HOLD_NO_CLEAR_VERDICT


def test_the_in_flight_hold_is_an_in_progress_check_never_a_clearance():
    run = check_for(HOLD_ROUND_IN_FLIGHT)
    assert run.status == IN_PROGRESS and run.conclusion is None
    assert "in progress" in run.title.lower()


# ── chokepoint.in_flight — the read the gate uses ───────────────────────────────


def test_in_flight_reads_a_held_slot_and_forgets_it_on_done():
    c = Chokepoint()
    assert c.in_flight("o/r", 1) is False
    assert c.admit("o/r", 1, HEAD) == "accept"
    assert c.in_flight("o/r", 1) is True
    assert c.in_flight("o/r", 2) is False  # per PR
    c.done("o/r", 1, HEAD)
    assert c.in_flight("o/r", 1) is False


def test_in_flight_is_pr_wide_not_sha_exact():
    # The webhook keys its slot by the EVENT's sha while the round reviews the head it
    # resolves; a sha-exact read could miss the round about to post on the current head.
    c = Chokepoint()
    c.admit("o/r", 1, "e" * 40)
    assert c.in_flight("o/r", 1) is True


def test_in_flight_ignores_an_abandoned_slot_past_its_ttl():
    clock = [0.0]
    c = Chokepoint(in_flight_ttl_s=100, now=lambda: clock[0])
    c.admit("o/r", 1, HEAD)
    clock[0] = 99.0
    assert c.in_flight("o/r", 1) is True
    clock[0] = 100.0
    assert c.in_flight("o/r", 1) is False  # the kind `admit` reclaims — not a live round


# ── 1. end to end: the #409 timeline ─────────────────────────────────────────────


async def test_promotion_holds_while_a_newer_round_runs_on_the_same_head(tmp_path):
    """Round 1 PASSed this head; a summon is running round 2 on it. Before #217 the sweep
    approved round 1's PASS here (`promote`), and round 2 then FAILed."""
    started, release = asyncio.Event(), asyncio.Event()

    async def runner(name, inputs):
        started.set()
        await release.wait()
        from tests.test_dispatch import REPORT

        return {"output": REPORT, "failed": []}  # a confirmed major ⇒ FAIL

    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", id=1)], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh, runner=runner)

    summon = asyncio.create_task(d.handle_summon("o/r", 1, "operator"))
    await asyncio.wait_for(started.wait(), 3)

    assert (await d.evaluate_promotion("o/r", 1)) == HOLD_ROUND_IN_FLIGHT
    assert not promoted(gh)
    # …and the check says so: not cleared, not failed.
    titles = [w.get("output[title]") for w in qa_check_writes(gh)]
    assert titles and titles[-1] == "Re-review in progress"

    release.set()
    assert (await asyncio.wait_for(summon, 3)) == "reviewed:FAIL"
    # Round 2's FAIL now stands on the head (the fake does not store posts — add it).
    gh.reviews.append(review_row(HEAD, "FAIL", state="CHANGES_REQUESTED", id=2))
    assert (await d.evaluate_promotion("o/r", 1)) == HOLD_NO_CLEAR_VERDICT
    assert not promoted(gh)


async def test_promotion_proceeds_once_the_round_finishes_clear(tmp_path):
    # The hold is a wait, not a block: nothing running ⇒ the old behaviour, unchanged.
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", id=1)], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    d.chokepoint.admit("o/r", 1, HEAD)
    assert (await d.evaluate_promotion("o/r", 1)) == HOLD_ROUND_IN_FLIGHT
    d.chokepoint.done("o/r", 1, HEAD)
    assert (await d.evaluate_promotion("o/r", 1)) == PROMOTE


async def test_a_summon_waiting_for_a_panel_slot_holds_promotion(tmp_path):
    # A summon queued behind the cross-PR cap has not taken the chokepoint slot yet, but it
    # WILL run a panel on this PR — approving in that gap is the same race.
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", id=1)], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    queue = PanelQueue(1)
    d.panel_sem = queue
    hold = queue.slot(repo="o/r", pr=99, kind="summon")  # another PR's panel has the slot
    await hold.__aenter__()
    waiting = asyncio.create_task(queue.slot(repo="o/r", pr=1, kind="summon").__aenter__())
    await asyncio.sleep(0)
    try:
        assert (await d.evaluate_promotion("o/r", 1)) == HOLD_ROUND_IN_FLIGHT
    finally:
        waiting.cancel()
        await hold.__aexit__(None, None, None)
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert (await d.evaluate_promotion("o/r", 1)) == PROMOTE


async def test_a_queued_webhook_event_does_not_hold_promotion(tmp_path):
    # The webhook takes a queue slot for EVERY pull_request action (labeled, edited...)
    # before filtering; those never run a panel, and a webhook round that does run takes
    # the chokepoint slot. Only summon/backfill waiters count.
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", id=1)], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    queue = PanelQueue(1)
    d.panel_sem = queue
    slot = queue.slot(repo="o/r", pr=1, kind="webhook")
    await slot.__aenter__()
    try:
        assert (await d.evaluate_promotion("o/r", 1)) == PROMOTE
    finally:
        await slot.__aexit__(None, None, None)


# ── 2. the converse: a later FAIL withdraws the approval and reddens the check ──


async def test_a_fail_on_a_promoted_head_withdraws_the_approval_and_fails_the_check(tmp_path):
    reviews = [review_row(HEAD, "PASS", id=1), {**promotion_row(HEAD, "PASS"), "id": 9}]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)

    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"

    assert gh.dismissed == ["repos/o/r/pulls/1/reviews/9/dismissals"]
    qa = [w for w in qa_check_writes(gh) if w.get("output[title]") == "FAIL verdict"]
    assert qa and qa[-1].get("status") == COMPLETED and qa[-1].get("conclusion") == FAILURE
    events = [e for e in d.telemetry.read_all() if e.get("event") == "dismissal"]
    assert events and events[-1].get("kind") == "approval" and events[-1].get("ok") is True


async def test_a_fail_withdraws_every_standing_approval_not_only_this_heads(tmp_path):
    # GitHub falls back to the reviewer's PREVIOUS state when the latest is dismissed, so
    # an older head's approval left standing would become our effective state again.
    old = "c" * 40
    reviews = [
        {**promotion_row(old, "PASS"), "id": 5},
        review_row(HEAD, "PASS", id=6),
        {**promotion_row(HEAD), "id": 9},
    ]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert sorted(gh.dismissed) == ["repos/o/r/pulls/1/reviews/5/dismissals", "repos/o/r/pulls/1/reviews/9/dismissals"]


async def test_a_pass_withdraws_nothing(tmp_path):
    from tests.test_dispatch import _clean_runner

    reviews = [review_row(HEAD, "PASS", id=1), {**promotion_row(HEAD, "PASS"), "id": 9}]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh, runner=_clean_runner)
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:PASS"
    assert "repos/o/r/pulls/1/reviews/9/dismissals" not in gh.dismissed


async def test_shadow_mode_withdraws_nothing(tmp_path):
    reviews = [review_row(HEAD, "PASS", id=1), {**promotion_row(HEAD, "PASS"), "id": 9}]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=GREEN)
    d = make(tmp_path, gh=gh)  # shadow default
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert gh.dismissed == []
    assert not [w for w in qa_check_writes(gh) if w.get("output[title]") == "FAIL verdict"]


# ── 3. a finished round leaves no slot — however it finished ────────────────────


def _no_slot(d) -> bool:
    return not d.chokepoint.in_flight("o/r", 1) and not d.chokepoint._in_flight


async def test_a_summon_round_that_fails_leaves_no_slot(tmp_path):
    d = make(tmp_path, gh=RoutedGH(pr_facts=facts(), reviews=[], checks=GREEN))
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert _no_slot(d)
    # …so the next summon nine minutes later (here: immediately) is admitted, not in-flight.
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"


async def test_a_push_round_that_fails_leaves_no_slot(tmp_path):
    d = make(tmp_path, gh=RoutedGH(pr_facts=facts(), reviews=[], checks=GREEN))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert _no_slot(d)


async def test_a_backfill_round_that_fails_leaves_no_slot(tmp_path):
    d = make(tmp_path, gh=RoutedGH(pr_facts=facts(), reviews=[], checks=GREEN))
    assert (await d.backfill_review("o/r", 1, HEAD)) == "reviewed:FAIL"
    assert _no_slot(d)


@pytest.mark.parametrize("entry", ["summon", "push", "backfill"])
async def test_a_round_that_raises_leaves_no_slot(tmp_path, entry):
    d = make(tmp_path, gh=RoutedGH(pr_facts=facts(), reviews=[], checks=GREEN))

    async def _review(*_a, **_k):
        raise RuntimeError("the round blew up")

    d._review = _review
    with pytest.raises(RuntimeError):
        if entry == "summon":
            await d.handle_summon("o/r", 1, "operator")
        elif entry == "push":
            await d.handle_pr_event("o/r", 1, HEAD, "opened")
        else:
            await d.backfill_review("o/r", 1, HEAD)
    assert _no_slot(d)


async def test_a_reaffirmed_round_leaves_no_slot(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", id=1)], checks=GREEN)
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "ready_for_review")) == "reaffirmed:FAIL"
    assert _no_slot(d)


# ── 3. the 04:36 race: a summon alongside a webhook that only reaffirms ──────────


class SlowReviewsGH(RoutedGH):
    """The reviews read takes a moment — the webhook's round is still deciding to reaffirm
    when the summon arrives, as on #409 (the summon dropped 0.3s before the reaffirm)."""

    async def __call__(self, args, timeout=30):
        if len(args) > 1 and args[1].endswith("/pulls/1/reviews") and "-X" not in args:
            await asyncio.sleep(0.2)
        return await super().__call__(args, timeout)


async def test_a_summon_racing_a_reaffirming_webhook_runs_instead_of_dropping(tmp_path):
    """Mark ready + `@vera review` delivers both together. The webhook's round reaffirms the
    standing FAIL in a fraction of a second; the summon used to drop as `in-flight` in that
    window and answer "nothing ran" with no round running at all."""
    gh = SlowReviewsGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", id=1)], checks=GREEN)
    d = make(tmp_path, gh=gh)

    ready = asyncio.create_task(d.handle_pr_event("o/r", 1, HEAD, "ready_for_review"))
    await asyncio.sleep(0)  # the webhook has admitted and holds the slot
    assert d.chokepoint.in_flight("o/r", 1)

    summon = await asyncio.wait_for(d.handle_summon("o/r", 1, "operator"), 5)
    assert (await ready) == "reaffirmed:FAIL"
    assert summon == "reviewed:FAIL"  # the panel ran — a summon bypasses the reaffirm
    assert _no_slot(d)


async def test_a_summon_still_drops_when_a_real_round_holds_the_slot(tmp_path):
    d = make(tmp_path, cfg={"summon_in_flight_grace_s": 0.3}, gh=RoutedGH(pr_facts=facts(), checks=GREEN))
    d.chokepoint.admit("o/r", 1, HEAD)  # a round that will not finish within the grace
    t0 = time.monotonic()
    assert (await d.handle_summon("o/r", 1, "operator")) == f"drop:{DROP_IN_FLIGHT}"
    assert 0.25 <= time.monotonic() - t0 < 2  # it waited the grace, and no longer


def test_the_grace_is_clamped(tmp_path):
    assert make(tmp_path).summon_in_flight_grace_s == 10
    assert make(tmp_path, cfg={"summon_in_flight_grace_s": 9999}).summon_in_flight_grace_s == 120
    assert make(tmp_path, cfg={"summon_in_flight_grace_s": -5}).summon_in_flight_grace_s == 0
    assert make(tmp_path, cfg={"summon_in_flight_grace_s": "junk"}).summon_in_flight_grace_s == 10


# ── 4. the reply says what actually happened ─────────────────────────────────────


def test_an_in_flight_reply_says_a_round_is_running_and_its_verdict_will_post():
    text = outcome_reply("dev", "drop:in-flight")
    assert "@dev" in text and "already running" in text and "verdict posts" in text
    assert "nothing ran" not in text


def test_a_reaffirmed_outcome_says_the_verdict_was_reaffirmed():
    text = outcome_reply("dev", "reaffirmed:FAIL")
    assert "reaffirmed" in text and "FAIL" in text and "nothing ran" not in text


def test_a_draft_reply_does_not_tell_you_to_wait_for_a_review():
    text = outcome_reply("dev", "drop:pr-not-eligible")
    assert "draft" in text and "ready for review" in text
    assert "current review finishes" not in text


def test_any_other_drop_still_answers():
    assert "`reviews-unreadable`" in outcome_reply("dev", "drop:reviews-unreadable")


def test_a_posted_verdict_needs_no_reply():
    assert outcome_reply("dev", "reviewed:FAIL") is None

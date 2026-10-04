"""Issue #268 — a PASS reached the `QA panel` gate only on the next sweep pass.

homelab-iac#292 (Vera telemetry, 2026-10-04):

    17:44:17  reviewed round=1 FAIL      ← `QA panel` failure written inline, same second
    18:48:09  promotion hold:no-clear-verdict   (the PR's sweep cadence: ~500–540 s)
    18:53:08  reviewed round=2 PASS      ← `protoReview` green; NO `QA panel` run on the head
    18:56:25  merged by admin bypass     ← the required check only ever read "expected"

1. A round that ends refreshes the gate itself (`_refresh_after_round`), once its slots
   are free — except a FAIL, which already wrote its gate inline and must not be raced.
2. A round that starts opens the `QA panel` run as "reviewing", so a required check is
   visibly pending rather than absent. Never over a concluded run; never in shadow mode.
3. A foreign check completing (CI) refreshes the gate too, via the `check_run` webhook.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_reviewer.approve import HOLD_ROUND_IN_FLIGHT, PROMOTE
from pr_reviewer.checks import COMPLETED, IN_PROGRESS, SUCCESS, reviewing_run
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.webhook import build_routers

from tests.dispatch_helpers import HEAD, FakeGH, RoutedGH, facts, make, review_row
from tests.test_promotion_in_flight_217 import promoted
from tests.test_webhook import SECRET, SpyDispatcher, check_run_payload, signed

GREEN = [{"status": "completed", "conclusion": "success", "name": "CI"}]
OWNER = {"shadow_mode": False, "promotion_owner": True}
PASS_REPORT = "<!-- brief -->\nClean.\n<!-- /brief -->\n\n```json\n[]\n```"


async def pass_runner(name, inputs):
    return {"output": PASS_REPORT, "steps": {}, "failed": []}


def qa_check_writes(gh) -> list[dict]:
    """The `QA panel` run's writes only, in order. RoutedGH serves `protoReview` too, and
    its PATCHes go to an id it handed out — those are excluded here."""
    writes = []
    for c in gh.calls:
        if "-X" not in c or "check-runs" not in c[1]:
            continue
        fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in c if "=" in a}
        tail = c[1].rsplit("/", 1)[-1]
        if fields.get("name") == "protoReview" or (tail.isdigit() and int(tail) in gh._review_check_ids):
            continue
        writes.append(fields)
    return writes


def titles(gh) -> list[str]:
    return [w.get("output[title]") for w in qa_check_writes(gh)]


def refreshes(tmp_path) -> list[dict]:
    rows = []
    for f in sorted((tmp_path / "telemetry").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == "gate_refresh"]


# ── 1. the round refreshes its own gate ─────────────────────────────────────────


async def test_a_pass_round_clears_the_gate_without_waiting_for_the_sweep(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh, runner=pass_runner)

    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    # The fake does not store posts — the PASS the round just posted now stands.
    gh.reviews.append(review_row(HEAD, "PASS", id=1))
    await asyncio.wait_for(d.drain_gate_refreshes(), 5)

    assert titles(gh)[-1] == "Cleared by the QA panel"
    final = qa_check_writes(gh)[-1]
    assert final.get("status") == COMPLETED and final.get("conclusion") == SUCCESS
    assert promoted(gh)
    assert [r["decision"] for r in refreshes(tmp_path)] == [PROMOTE]


async def test_a_fail_round_does_not_refresh_and_cannot_approve_on_a_stale_read(tmp_path):
    # GitHub can serve the reviews list without the FAIL just posted; a refresh would read
    # the older PASS and approve beside the FAIL. The FAIL wrote its own gate inline.
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", id=1)], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)  # default runner: a confirmed major ⇒ FAIL

    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    await asyncio.wait_for(d.drain_gate_refreshes(), 5)

    assert not promoted(gh)
    assert refreshes(tmp_path) == []
    assert titles(gh)[-1] == "FAIL verdict"


async def test_the_refresh_waits_for_the_rounds_slot_to_free(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", id=1)], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    d.chokepoint.admit("o/r", 1, HEAD)  # e.g. a summon's queue slot, freed after it returns
    assert d.request_gate_refresh("o/r", 1, reason="reviewed:PASS")
    await asyncio.sleep(0.3)
    assert not promoted(gh)  # evaluated now it would only say "round in flight"
    d.chokepoint.done("o/r", 1, HEAD)
    await asyncio.wait_for(d.drain_gate_refreshes(), 5)
    assert promoted(gh)
    assert [r["decision"] for r in refreshes(tmp_path)] == [PROMOTE]


async def test_a_refresh_gives_way_to_a_newer_round_that_keeps_the_slot(tmp_path, monkeypatch):
    monkeypatch.setattr("pr_reviewer.dispatch.GATE_REFRESH_WAIT_S", 0.3)
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", id=1)], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    d.chokepoint.admit("o/r", 1, HEAD)
    d.request_gate_refresh("o/r", 1, reason="reviewed:PASS")
    await asyncio.wait_for(d.drain_gate_refreshes(), 5)
    assert not promoted(gh)  # that round refreshes the gate when IT ends
    assert [r["decision"] for r in refreshes(tmp_path)] == [HOLD_ROUND_IN_FLIGHT]
    d.chokepoint.done("o/r", 1, HEAD)


async def test_refresh_requests_for_one_pr_coalesce(tmp_path, monkeypatch):
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", id=1)], checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    calls = []

    async def slow_eval(repo, pr):
        calls.append((repo, pr))
        await asyncio.sleep(0.05)
        return PROMOTE

    monkeypatch.setattr(d, "evaluate_promotion", slow_eval)
    for _ in range(10):  # a CI suite finishing ten checks at once, before the pass starts
        assert d.request_gate_refresh("o/r", 1, reason="check-completed")
    await asyncio.wait_for(d.drain_gate_refreshes(), 5)
    assert calls == [("o/r", 1)]  # one pass reads the state all ten left behind

    calls.clear()
    d.request_gate_refresh("o/r", 1, reason="check-completed")
    await asyncio.sleep(0.02)  # that pass is now mid-evaluation, its facts already read
    for _ in range(5):
        d.request_gate_refresh("o/r", 1, reason="check-completed")
    await asyncio.wait_for(d.drain_gate_refreshes(), 5)
    # Requests that landed DURING a pass get exactly one more — never lost, never five.
    assert calls == [("o/r", 1), ("o/r", 1)]


async def test_no_refresh_in_shadow_mode_or_for_an_unlisted_repo(tmp_path):
    shadow = make(tmp_path, cfg={"shadow_mode": True, "promotion_owner": True})
    assert shadow.request_gate_refresh("o/r", 1) is False
    owner = make(tmp_path, cfg=OWNER)
    assert owner.request_gate_refresh("someone/else", 1) is False
    assert owner._gate_refreshes == {}


# ── 2. the round opens the gate as "reviewing" ──────────────────────────────────


async def test_a_round_opens_the_qa_panel_check_as_reviewing_before_its_verdict(tmp_path):
    seen: list[list[str]] = []
    gh = RoutedGH(pr_facts=facts(), reviews=[], checks=GREEN)

    async def runner(name, inputs):
        seen.append(titles(gh))  # what the gate said WHILE the panel ran
        return {"output": PASS_REPORT, "steps": {}, "failed": []}

    d = make(tmp_path, cfg=OWNER, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    await asyncio.wait_for(d.drain_gate_refreshes(), 5)
    assert seen and seen[0] == [reviewing_run().title]
    first = qa_check_writes(gh)[0]
    assert first.get("status") == IN_PROGRESS and not first.get("conclusion")


async def test_shadow_mode_writes_no_qa_panel_check_at_all(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[], checks=GREEN)
    d = make(tmp_path, cfg={"shadow_mode": True, "promotion_owner": True}, gh=gh, runner=pass_runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    await asyncio.wait_for(d.drain_gate_refreshes(), 5)
    assert qa_check_writes(gh) == []


class ConcludedCheckGH(FakeGH):
    """Serves an existing, concluded `QA panel` run for the head."""

    async def __call__(self, args, timeout=30):
        if "check-runs?check_name=" in " ".join(args):
            self.calls.append(args)
            return 0, json.dumps({"id": 5, "status": COMPLETED, "conclusion": SUCCESS, "title": "Cleared"}), ""
        return await super().__call__(args, timeout)


async def test_reviewing_never_reopens_a_concluded_verdict(tmp_path):
    # A summon on a cleared head must not flip its green check back to pending; the
    # refresh after the round writes whatever that round decides.
    gh = ConcludedCheckGH()
    d = make(tmp_path, cfg=OWNER, gh=gh)
    await d._publish_qa_check("o/r", HEAD, reviewing_run(), never_reopen=True)
    assert not [c for c in gh.calls if "-X" in c]


# ── 3. CI completing refreshes the gate (check_run webhook) ─────────────────────


class RefreshSpy(SpyDispatcher):
    def __init__(self):
        super().__init__()
        self.refreshed: list[tuple] = []

    def request_gate_refresh(self, repo, pr, *, reason=""):
        self.refreshed.append((repo, pr, reason))
        return True


def refresh_app(tmp_path):
    dispatcher = RefreshSpy()
    public, _api = build_routers(dispatcher, Telemetry(tmp_path), lambda: SECRET)
    app = FastAPI()
    app.include_router(public, prefix="/plugins/pr-reviewer")
    return app, dispatcher


def post(app, body: bytes):
    return TestClient(app).post(
        "/plugins/pr-reviewer/webhook", content=body, headers={**signed(body), "X-GitHub-Event": "check_run"}
    )


def test_a_completed_ci_check_refreshes_the_gate(tmp_path):
    app, dispatcher = refresh_app(tmp_path)
    r = post(app, check_run_payload(name="CI", action="completed"))
    assert r.json() == {"ok": True, "dispatched": True, "reason": "check-completed"}
    assert dispatcher.refreshed == [("o/r", 7, "check-completed")]


def test_our_own_checks_completing_refresh_nothing(tmp_path):
    # We conclude these ourselves; reacting to them would loop.
    app, dispatcher = refresh_app(tmp_path)
    for name in ("protoReview", "QA panel"):
        assert post(app, check_run_payload(name=name, action="completed")).json()["reason"] == "own-check"
    assert dispatcher.refreshed == []


def test_a_completed_check_with_no_pr_refreshes_nothing(tmp_path):
    app, dispatcher = refresh_app(tmp_path)
    assert post(app, check_run_payload(name="CI", action="completed", pr=0)).json()["reason"] == "check-run-no-pr"
    assert dispatcher.refreshed == []

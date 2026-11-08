"""#309: only a delivery that can start a panel waits for a panel slot.

Before #309 `_safe_handle` took a `PanelQueue` slot for EVERY pull_request delivery, and only
then did `handle_pr_event` drop the non-dispatch actions and the in-flight redeliveries. On
Vera (2026-11-08) the queue read `depth: 26` for ~14 distinct PRs, with one head queued 5x.
The supersede hint (#245/#258) and the `closed` hint (#261) waited behind real panels too.
These tests drive the real router with a dispatcher whose panels park until released, so a
full queue is observable.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
from fastapi import FastAPI
from httpx import ASGITransport
from pr_reviewer.chokepoint import Chokepoint
from pr_reviewer.dispatch import PanelQueue
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.webhook import build_routers

SECRET = "whsec"


def signed(body: bytes) -> dict:
    sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request"}


def payload(pr: int, head: str, action: str = "synchronize") -> bytes:
    return json.dumps(
        {"action": action, "repository": {"full_name": "o/r"}, "pull_request": {"number": pr, "head": {"sha": head}}}
    ).encode()


class ChokedDispatcher:
    """Real `Chokepoint`, panels parked on `release`. Like the real dispatcher it admits AFTER
    the webhook grants a slot, and a non-dispatch action returns without running a panel."""

    def __init__(self, limit: int = 1):
        self.max_concurrent_panels = limit
        self.cfg = {"summon_handle": "vera"}
        self.chokepoint = Chokepoint(cooldown_s=0)
        self.release = asyncio.Event()
        self.panels: list[tuple[int, str]] = []
        self.non_dispatch: list[tuple[int, str]] = []
        self.hints: list[tuple[int, str]] = []
        self.running = 0

    def _hint_newer_head(self, repo, pr, head):
        self.hints.append((pr, head))

    async def handle_pr_event(self, repo, pr, head, action):
        if action not in {"opened", "synchronize", "reopened", "ready_for_review"}:
            self.non_dispatch.append((pr, action))
            return "drop:not-a-dispatch-action"
        if self.chokepoint.admit(repo, pr, head) != "accept":
            return "drop:in-flight"
        self.panels.append((pr, head))
        self.running += 1
        try:
            await self.release.wait()
        finally:
            self.running -= 1
            self.chokepoint.done(repo, pr, head)
        return "reviewed:PASS"


def _app(tmp_path, dispatcher):
    telemetry = Telemetry(tmp_path)
    public, _api = build_routers(dispatcher, telemetry, lambda: SECRET)
    app = FastAPI()
    app.include_router(public, prefix="/plugins/pr-reviewer")
    return app, telemetry


async def _settle(predicate=lambda: False, turns: int = 200):
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)


async def _post(client, body):
    r = await client.post("/plugins/pr-reviewer/webhook", content=body, headers=signed(body))
    assert r.status_code == 200


async def test_non_dispatch_actions_never_wait_for_a_slot(tmp_path):
    d = ChokedDispatcher(limit=1)
    app, _t = _app(tmp_path, d)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, payload(1, "a" * 40, "opened"))
        await _settle(lambda: d.running == 1)
        for action in ("labeled", "edited", "review_requested", "closed"):
            await _post(c, payload(2, "b" * 40, action))
        await _settle(lambda: len(d.non_dispatch) == 4)
        q: PanelQueue = d.panel_sem
        # Handled at once, while the only slot is still held: none of them queued.
        assert [a for _pr, a in d.non_dispatch] == ["labeled", "edited", "review_requested", "closed"]
        assert q.status()["depth"] == 0
        d.release.set()


async def test_a_redelivery_of_the_head_under_review_drops_without_queueing(tmp_path):
    d = ChokedDispatcher(limit=1)
    app, telemetry = _app(tmp_path, d)
    head = "a" * 40
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, payload(1, head, "opened"))
        await _settle(lambda: d.running == 1)
        await _post(c, payload(1, head, "synchronize"))
        await _settle()
        assert d.panel_sem.status()["depth"] == 0
        drops = [e for e in telemetry.read_all() if e["event"] == "drop" and e.get("pre_queue")]
        assert [(e["reason"], e["sha"]) for e in drops] == [("in-flight", head)]
        assert d.hints == []  # the same head is not a NEWER head
        d.release.set()
    assert d.panels == [(1, head)]


async def test_a_second_delivery_for_a_waiting_head_is_deduped(tmp_path):
    d = ChokedDispatcher(limit=1)
    app, telemetry = _app(tmp_path, d)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, payload(1, "a" * 40, "opened"))  # takes the slot
        await _settle(lambda: d.running == 1)
        for _ in range(4):  # PR 2 at one head, delivered 4x while it waits
            await _post(c, payload(2, "b" * 40, "synchronize"))
        await _settle(lambda: d.panel_sem.status()["depth"] >= 1)
        await _settle()
        snap = d.panel_sem.status()
        assert [(w["pr"], w["head"]) for w in snap["queued"]] == [(2, "b" * 40)]  # ONE position, not four
        reasons = [e["reason"] for e in telemetry.read_all() if e["event"] == "drop" and e.get("pre_queue")]
        assert reasons == ["already-queued"] * 3
        d.release.set()
        await _settle(lambda: len(d.panels) == 2 and d.running == 0)
    assert d.panels == [(1, "a" * 40), (2, "b" * 40)]  # the waiter that stayed still reviews it


async def test_a_push_while_its_pr_is_under_review_hints_at_once_and_still_queues(tmp_path):
    d = ChokedDispatcher(limit=1)
    app, telemetry = _app(tmp_path, d)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, payload(1, "a" * 40, "opened"))
        await _settle(lambda: d.running == 1)
        await _post(c, payload(1, "c" * 40, "synchronize"))
        await _settle(lambda: d.panel_sem.status()["depth"] == 1)
        # The hint lands while the new head is still WAITING. Before #309 it was set only once
        # the delivery reached `handle_pr_event`, after a slot freed.
        assert d.hints == [(1, "c" * 40)]
        assert not [e for e in telemetry.read_all() if e["event"] == "drop" and e.get("pre_queue")]
        d.release.set()


async def test_ready_for_review_always_takes_the_normal_path(tmp_path):
    """`handle_pr_event` resets the round cap on ready_for_review, so it is never pre-dropped."""
    d = ChokedDispatcher(limit=1)
    app, telemetry = _app(tmp_path, d)
    head = "a" * 40
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await _post(c, payload(1, head, "opened"))
        await _settle(lambda: d.running == 1)
        await _post(c, payload(1, head, "ready_for_review"))
        await _settle(lambda: d.panel_sem.status()["depth"] == 1)
        assert not [e for e in telemetry.read_all() if e.get("pre_queue")]
        d.release.set()


def test_live_shas_ignores_an_abandoned_slot():
    clock = [0.0]
    cp = Chokepoint(cooldown_s=0, in_flight_ttl_s=10, now=lambda: clock[0])
    assert cp.admit("o/r", 1, "a") == "accept"
    assert cp.live_shas("o/r", 1) == {"a"}
    clock[0] = 11.0
    assert cp.live_shas("o/r", 1) == set()  # past the TTL: admit would reclaim it
    assert cp.held_shas("o/r", 1) == {"a"}  # and the pure read reclaimed nothing


def test_has_waiter_matches_repo_pr_head_and_kind():
    q = PanelQueue(1)
    from pr_reviewer.dispatch import _Waiter

    q._pending.append(_Waiter(repo="o/r", pr=1, head="h", kind="webhook", enqueued_at=0, mono=0))
    q._pending.append(_Waiter(repo="o/r", pr=2, head="k", kind="sweep-backfill", enqueued_at=0, mono=0))
    assert q.has_waiter("o/r", 1, "h")
    assert not q.has_waiter("o/r", 1, "other")
    assert not q.has_waiter("o/r", 2, "k")  # a backfill waiter is not a webhook one
    assert not q.has_waiter("o/r", 1, "")

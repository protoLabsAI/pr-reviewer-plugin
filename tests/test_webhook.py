"""The webhook route — HMAC is the auth; the eval endpoint reads telemetry."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.webhook import build_routers

SECRET = "whsec"


class SpyDispatcher:
    def __init__(self):
        self.events: list[tuple] = []
        self.promotions: list[tuple] = []

    async def handle_pr_event(self, repo, pr, head, action):
        self.events.append((repo, pr, head, action))
        return "reviewed:PASS"

    async def evaluate_promotion(self, repo, pr):
        # Recorded so the thread-resolution tests can assert the gate was actually
        # re-published, not merely that the request was accepted.
        self.promotions.append((repo, pr))
        return "hold:not-promotion-owner"


def make_app(tmp_path, secret=SECRET):
    dispatcher = SpyDispatcher()
    telemetry = Telemetry(tmp_path)
    public, api = build_routers(dispatcher, telemetry, lambda: secret)
    app = FastAPI()
    app.include_router(public, prefix="/plugins/pr-reviewer")
    app.include_router(api, prefix="/api/plugins/pr-reviewer")
    return app, dispatcher, telemetry


def signed(body: bytes, secret=SECRET) -> dict:
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": sig, "X-GitHub-Event": "pull_request"}


PAYLOAD = json.dumps(
    {"action": "opened", "repository": {"full_name": "o/r"}, "pull_request": {"number": 7, "head": {"sha": "a" * 40}}}
).encode()


def test_bad_signature_403s_and_no_secret_fails_closed(tmp_path):
    app, dispatcher, _t = make_app(tmp_path)
    client = TestClient(app)
    r = client.post("/plugins/pr-reviewer/webhook", content=PAYLOAD, headers=signed(PAYLOAD, "wrong"))
    assert r.status_code == 403

    app2, dispatcher2, _t2 = make_app(tmp_path, secret="")  # unconfigured secret
    r2 = TestClient(app2).post("/plugins/pr-reviewer/webhook", content=PAYLOAD, headers=signed(PAYLOAD))
    assert r2.status_code == 403
    assert dispatcher.events == [] and dispatcher2.events == []


def test_valid_delivery_dispatches_in_background(tmp_path):
    app, dispatcher, _t = make_app(tmp_path)
    with TestClient(app) as client:  # context manager runs background tasks to completion
        r = client.post("/plugins/pr-reviewer/webhook", content=PAYLOAD, headers=signed(PAYLOAD))
        assert r.status_code == 200 and r.json()["dispatched"] is True
    assert dispatcher.events == [("o/r", 7, "a" * 40, "opened")]


def test_non_pr_events_are_acknowledged_not_dispatched(tmp_path):
    app, dispatcher, _t = make_app(tmp_path)
    headers = {**signed(PAYLOAD), "X-GitHub-Event": "push"}
    r = TestClient(app).post("/plugins/pr-reviewer/webhook", content=PAYLOAD, headers=headers)
    assert r.status_code == 200 and r.json()["dispatched"] is False
    assert dispatcher.events == []


def test_manual_dispatch_and_eval_endpoints(tmp_path):
    app, dispatcher, telemetry = make_app(tmp_path)
    telemetry.emit("dispatch", repo="o/r", pr=7)
    telemetry.emit("reviewed", repo="o/r", pr=7, verdict="PASS", posted=True, latency_s=100.0, recipe="code-review")
    client = TestClient(app)
    r = client.post("/api/plugins/pr-reviewer/dispatch", json={"repo": "o/r", "pr": 7})
    assert r.json()["outcome"] == "reviewed:PASS"
    ev = client.get("/api/plugins/pr-reviewer/eval").json()
    assert ev["completion_rate"] == 1.0 and ev["verdict_mix"] == {"PASS": 1}


def test_three_way_endpoint_renders_the_report(tmp_path):
    async def fake_gh(args, timeout=30):
        return (
            0,
            json.dumps(
                [
                    {"login": "protoquinn[bot]", "state": "APPROVED"},
                    {"login": "coderabbitai[bot]", "state": "COMMENTED"},
                ]
            ),
            "",
        )

    dispatcher = SpyDispatcher()
    telemetry = Telemetry(tmp_path)
    telemetry.emit("dispatch", repo="o/r", pr=7)
    telemetry.emit("reviewed", repo="o/r", pr=7, verdict="PASS", posted=True, latency_s=60.0, recipe="code-review")
    public, api = build_routers(dispatcher, telemetry, lambda: "s", run_gh_fn=fake_gh)
    app = FastAPI()
    app.include_router(api, prefix="/api/plugins/pr-reviewer")
    r = TestClient(app).get("/api/plugins/pr-reviewer/eval/three-way").json()
    assert r["rows"] == [{"repo": "o/r", "pr": 7, "ours": "PASS", "quinn": "APPROVED", "coderabbit_reviews": 1}]
    assert "| o/r#7 | PASS | APPROVED | 1 |" in r["markdown"]
    assert "1/1 PRs also carry a Quinn verdict" in r["markdown"]


def test_webhook_secret_env_fallback_for_headless_deploys(tmp_path, monkeypatch):
    """Headless config-as-code can't bake the secrets overlay — the plugin falls
    back to PR_REVIEWER_WEBHOOK_SECRET (config wins when both are set).

    This test drives the REAL router, so a verified signature schedules the REAL
    dispatcher on a background task. Left alone that reaches `gh` over the network
    (issue #13: it hung indefinitely on a workstation with an authenticated `gh`,
    while passing in CI where `gh` fails fast) — a unit test's outcome must not
    depend on whoever runs it being logged out.

    The ALLOWLIST is the real guard: an allowlist excluding the payload's repo drops
    the dispatch at the gate, which by construction runs before any GitHub call. The
    `run_gh` stub below is belt-and-braces only — a background task's exception is
    swallowed, so it cannot fail this test (verified: admitting `o/r` still passes).
    It stops real network I/O; it does not detect it.
    """
    import pr_reviewer

    from tests.conftest import FakeRegistry

    def _no_network(*_a, **_kw):  # belt-and-braces; see the docstring
        raise AssertionError("the webhook suite must never shell out to gh")

    monkeypatch.setattr("pr_reviewer.dispatch.run_gh", _no_network)
    monkeypatch.setenv("PR_REVIEWER_WEBHOOK_SECRET", "env-secret")
    # An allowlist that excludes the payload's repo: the gate runs BEFORE any GitHub
    # call, so the background task drops at `unlisted-repo` and never dials out.
    reg = FakeRegistry({"repos": ["allowed/elsewhere"]})  # no webhook_secret in config
    pr_reviewer.register(reg)
    public, _prefix = reg.routers[0]
    app = FastAPI()
    app.include_router(public, prefix="/plugins/pr-reviewer")
    r = TestClient(app).post("/plugins/pr-reviewer/webhook", content=PAYLOAD, headers=signed(PAYLOAD, "env-secret"))
    assert r.status_code == 200  # env secret verified the HMAC

    reg2 = FakeRegistry({"webhook_secret": "config-secret", "repos": ["allowed/elsewhere"]})
    pr_reviewer.register(reg2)
    public2, _p = reg2.routers[0]
    app2 = FastAPI()
    app2.include_router(public2, prefix="/plugins/pr-reviewer")
    assert (
        TestClient(app2)
        .post("/plugins/pr-reviewer/webhook", content=PAYLOAD, headers=signed(PAYLOAD, "env-secret"))
        .status_code
        == 403
    )
    assert (
        TestClient(app2)
        .post("/plugins/pr-reviewer/webhook", content=PAYLOAD, headers=signed(PAYLOAD, "config-secret"))
        .status_code
        == 200
    )


# ── on-demand summon (issue #28, slice 1) ────────────────────────────────────


class SummonSpy(SpyDispatcher):
    def __init__(self, cfg=None):
        super().__init__()
        self.cfg = cfg or {"summon_handle": "vera"}
        self.summons: list[tuple] = []
        self.summon_outcome = "reviewed:FAIL"

    async def _viewer_login(self):
        return "qa-bot"

    async def handle_summon(self, repo, pr, actor):
        self.summons.append((repo, pr, actor))
        return self.summon_outcome


def comment_payload(text: str, login: str = "someone", *, is_pr: bool = True, action: str = "created") -> bytes:
    issue: dict = {"number": 7}
    if is_pr:
        issue["pull_request"] = {"url": "..."}
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": "o/r"},
            "issue": issue,
            "comment": {"body": text, "user": {"login": login}},
        }
    ).encode()


def summon_app(tmp_path, *, permission="admin"):
    dispatcher = SummonSpy()
    telemetry = Telemetry(tmp_path)
    posted: list[dict] = []

    async def fake_gh(args, timeout=30):
        joined = " ".join(args)
        if "/collaborators/" in joined:
            return 0, permission, ""
        if "-X" in args and "POST" in args and "/comments" in joined:
            posted.append({a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a})
            return 0, "{}", ""
        return 0, "", ""

    public, _api = build_routers(dispatcher, telemetry, lambda: SECRET, run_gh_fn=fake_gh)
    app = FastAPI()
    app.include_router(public, prefix="/plugins/pr-reviewer")
    return app, dispatcher, posted


def post_comment(app, body: bytes):
    return TestClient(app).post(
        "/plugins/pr-reviewer/webhook",
        content=body,
        headers={**signed(body), "X-GitHub-Event": "issue_comment"},
    )


def test_an_admin_summon_dispatches_a_review(tmp_path):
    app, dispatcher, _posted = summon_app(tmp_path)
    r = post_comment(app, comment_payload("@vera review"))
    assert r.json() == {"ok": True, "dispatched": True, "reason": "summon:review"}
    assert dispatcher.summons == [("o/r", 7, "someone")]


def test_a_non_admin_is_refused_with_a_reply_not_silence(tmp_path):
    app, dispatcher, posted = summon_app(tmp_path, permission="write")
    r = post_comment(app, comment_payload("@vera review"))
    assert r.json()["reason"] == "summon:refused-not-admin"
    assert dispatcher.summons == []  # no panel spent
    assert posted and "admin" in posted[0]["body"]  # the caller is told why


def test_an_unreadable_permission_refuses(tmp_path):
    # is_admin fails closed; the webhook must not spend a panel on it.
    app, dispatcher, posted = summon_app(tmp_path, permission="")
    assert post_comment(app, comment_payload("@vera review")).json()["reason"] == "summon:refused-not-admin"
    assert dispatcher.summons == []


def test_help_answers_without_spending_a_panel(tmp_path):
    app, dispatcher, posted = summon_app(tmp_path)
    assert post_comment(app, comment_payload("@vera help")).json()["reason"] == "summon:help"
    assert dispatcher.summons == []
    assert "@vera review" in posted[0]["body"]


def test_an_unknown_verb_gets_help_not_silence(tmp_path):
    app, _d, posted = summon_app(tmp_path)
    assert post_comment(app, comment_payload("@vera frobnicate")).json()["reason"] == "summon:unknown-verb"
    assert posted and "review" in posted[0]["body"]


def test_an_ordinary_comment_is_not_a_summon(tmp_path):
    app, dispatcher, posted = summon_app(tmp_path)
    r = post_comment(app, comment_payload("this looks good to me"))
    assert r.json()["reason"] == "summon:not-addressed"
    assert dispatcher.summons == [] and posted == []


def test_the_reviewer_never_answers_itself(tmp_path):
    # Our own verdict bodies mention the handle; replying to them is an infinite loop
    # with a five-subagent price tag.
    app, dispatcher, posted = summon_app(tmp_path)
    r = post_comment(app, comment_payload("@vera review", login="qa-bot[bot]"))
    assert r.json()["reason"] == "summon:self"
    assert dispatcher.summons == [] and posted == []


def test_a_plain_issue_is_not_reviewable(tmp_path):
    app, dispatcher, _posted = summon_app(tmp_path)
    r = post_comment(app, comment_payload("@vera review", is_pr=False))
    assert r.json()["reason"] == "not-a-pull-request"
    assert dispatcher.summons == []


def test_a_deleted_comment_action_does_nothing(tmp_path):
    app, dispatcher, _posted = summon_app(tmp_path)
    r = post_comment(app, comment_payload("@vera review", action="deleted"))
    assert r.json()["reason"] == "not-a-comment-action"
    assert dispatcher.summons == []


def test_an_unsigned_summon_is_rejected_like_any_other_delivery(tmp_path):
    app, dispatcher, _posted = summon_app(tmp_path)
    body = comment_payload("@vera review")
    r = TestClient(app).post("/plugins/pr-reviewer/webhook", content=body, headers={"X-GitHub-Event": "issue_comment"})
    assert r.status_code == 403
    assert dispatcher.summons == []


# ── re-running the protoReview gate (#95) ────────────────────────────────────


def check_run_payload(name: str = "protoReview", action: str = "rerequested", pr: int = 7, login: str = "dev") -> bytes:
    check_run: dict = {"name": name}
    if pr:
        check_run["pull_requests"] = [{"number": pr}]
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": "o/r"},
            "check_run": check_run,
            "sender": {"login": login},
        }
    ).encode()


def post_check_run(app, body: bytes):
    return TestClient(app).post(
        "/plugins/pr-reviewer/webhook",
        content=body,
        headers={**signed(body), "X-GitHub-Event": "check_run"},
    )


def test_rerequesting_the_protoreview_check_reruns_the_panel(tmp_path):
    """A required check you cannot re-run is a footgun: a red X could only be cleared by
    pushing a dummy commit. "Re-run" on the gate re-drives the panel (summon posture)."""
    app, dispatcher, _posted = summon_app(tmp_path)
    with TestClient(app) as client:  # context manager runs the background task to completion
        body = check_run_payload()
        r = client.post(
            "/plugins/pr-reviewer/webhook", content=body, headers={**signed(body), "X-GitHub-Event": "check_run"}
        )
        assert r.json() == {"ok": True, "dispatched": True, "reason": "check-run-rerequest"}
    assert dispatcher.summons == [("o/r", 7, "dev")]


def test_a_non_rerequest_check_run_is_ignored(tmp_path):
    """Our own `created`/`completed` events (we open and conclude the check) must not
    loop the panel — only the human-initiated `rerequested` acts."""
    app, dispatcher, _posted = summon_app(tmp_path)
    assert post_check_run(app, check_run_payload(action="completed")).json()["reason"] == "not-a-rerequest"
    assert dispatcher.summons == []


def test_a_rerequest_for_another_check_is_ignored(tmp_path):
    app, dispatcher, _posted = summon_app(tmp_path)
    assert post_check_run(app, check_run_payload(name="CI")).json()["reason"] == "not-our-check"
    assert dispatcher.summons == []


def test_a_check_run_not_tied_to_a_pr_is_a_no_op(tmp_path):
    app, dispatcher, _posted = summon_app(tmp_path)
    assert post_check_run(app, check_run_payload(pr=0)).json()["reason"] == "check-run-no-pr"
    assert dispatcher.summons == []


# ── resolving a thread must clear the gate it is measured by (issue #111) ────


def review_thread_payload(action="resolved", pr=7):
    return json.dumps(
        {
            "action": action,
            "repository": {"full_name": "o/r"},
            "pull_request": {"number": pr},
            "thread": {"id": 1},
        }
    ).encode()


def post_thread(app, body: bytes):
    return TestClient(app).post(
        "/plugins/pr-reviewer/webhook",
        content=body,
        headers={**signed(body), "X-GitHub-Event": "pull_request_review_thread"},
    )


def test_resolving_a_thread_republishes_the_gate(tmp_path):
    """The defect this guards. The `QA panel` check fails with "N unresolved review
    threads — resolve each thread ... and this clears on the next pass", but nothing
    subscribed to the event that says a thread WAS resolved, so doing exactly what the
    check asked left it red until an unrelated push. Live: protoAgent#3415 read FAILURE
    for ten hours after its threads were dealt with."""
    app, dispatcher, _posted = summon_app(tmp_path)
    with TestClient(app) as client:
        body = review_thread_payload()
        r = client.post(
            "/plugins/pr-reviewer/webhook",
            content=body,
            headers={**signed(body), "X-GitHub-Event": "pull_request_review_thread"},
        )
        assert r.json() == {"ok": True, "dispatched": True, "reason": "thread-resolved"}
    assert dispatcher.promotions == [("o/r", 7)]


def test_reopening_a_thread_also_republishes_the_gate(tmp_path):
    """The gate must not be one-way. If `unresolved` were ignored, a reopened finding
    would ride a green check until the next push."""
    app, dispatcher, _posted = summon_app(tmp_path)
    with TestClient(app) as client:
        body = review_thread_payload(action="unresolved")
        r = client.post(
            "/plugins/pr-reviewer/webhook",
            content=body,
            headers={**signed(body), "X-GitHub-Event": "pull_request_review_thread"},
        )
        assert r.json()["reason"] == "thread-unresolved"
    assert dispatcher.promotions == [("o/r", 7)]


def test_a_thread_edit_is_not_a_resolution(tmp_path):
    app, dispatcher, _posted = summon_app(tmp_path)
    assert post_thread(app, review_thread_payload(action="edited")).json()["reason"] == "not-a-resolution"
    assert dispatcher.promotions == []


def test_re_evaluating_the_gate_never_spends_a_panel(tmp_path):
    """`evaluate_promotion` re-reads state and republishes the check with no model call.
    Routing a thread resolution through the full panel would make every batch-resolve
    (GitHub sends one event PER THREAD) cost a review."""
    app, dispatcher, _posted = summon_app(tmp_path)
    with TestClient(app) as client:
        body = review_thread_payload()
        client.post(
            "/plugins/pr-reviewer/webhook",
            content=body,
            headers={**signed(body), "X-GitHub-Event": "pull_request_review_thread"},
        )
    assert dispatcher.promotions == [("o/r", 7)]
    assert dispatcher.summons == []
    assert dispatcher.events == []


def test_a_malformed_thread_payload_is_dropped(tmp_path):
    app, dispatcher, _posted = summon_app(tmp_path)
    body = json.dumps({"action": "resolved", "repository": {}}).encode()
    assert post_thread(app, body).json()["reason"] == "malformed-payload"
    assert dispatcher.promotions == []


# ── replay endpoint (in-process A/B runner, issue #20) ───────────────────────


async def test_replay_endpoint_runs_the_panel_and_never_posts(tmp_path):

    class ReplayDispatcher(SpyDispatcher):
        def _runner(self):
            async def run(recipe, inputs):
                return {"output": "brief\n```json\n[]\n```", "failed": [], "timings": {}, "usage": {}}

            return run

        @staticmethod
        def _parse_findings(output):
            return []

    dispatcher = ReplayDispatcher()
    telemetry = Telemetry(tmp_path)

    posted = []

    async def fake_gh(args, timeout=30):
        if "-X" in args:
            posted.append(args)
        if "/files" in " ".join(args):
            return 0, "x.py\n", ""
        return 0, "[]", ""

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _pub, api = build_routers(dispatcher, telemetry, lambda: "s", run_gh_fn=fake_gh)
    app = FastAPI()
    app.include_router(api, prefix="/api/plugins/pr-reviewer")
    r = TestClient(app).post(
        "/api/plugins/pr-reviewer/replay",
        json={"row": {"repo": "o/r", "pr": 1, "head": "a" * 40}, "model": "protolabs/fast"},
    )
    assert r.status_code == 200
    runs = r.json()["runs"]
    assert len(runs) == 1
    assert runs[0]["run"]["model"] == "protolabs/fast" and runs[0]["verdict"] == "PASS"
    assert posted == []  # side-effect-free


async def test_replay_endpoint_503s_without_a_runner(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    dispatcher = SpyDispatcher()
    dispatcher._runner = lambda: None
    _pub, api = build_routers(dispatcher, Telemetry(tmp_path), lambda: "s")
    app = FastAPI()
    app.include_router(api, prefix="/api/plugins/pr-reviewer")
    r = TestClient(app).post("/api/plugins/pr-reviewer/replay", json={"row": {"repo": "o/r", "pr": 1, "head": "a"}})
    assert r.status_code == 503


# ── summon health must not confuse "no" with "I don't know" (post-mortem) ─────


def _health_app(tmp_path, cfg):
    """A dispatcher whose cfg drives the health endpoint's App-credential lookup."""

    class D(SpyDispatcher):
        pass

    d = D()
    d.cfg = cfg
    telemetry = Telemetry(tmp_path)
    public, api = build_routers(d, telemetry, lambda: SECRET)
    app = FastAPI()
    app.include_router(public, prefix="/plugins/pr-reviewer")
    app.include_router(api, prefix="/api/plugins/pr-reviewer")
    return app


def test_unreadable_app_events_report_unknown_not_missing(tmp_path, monkeypatch):
    """The bug this endpoint shipped with: `GET /app` is JWT-only, so on installation-
    token auth it always failed, the failure was recorded as `subscribed: []`, and a
    WORKING summon surface was reported dead — on a deployment that had already taken
    331 `pull_request_review_comment` deliveries. Unknown must read as unknown."""
    # AppAuthConfig falls back to the ENV, so "no credentials" is only true if the env
    # is clear too — otherwise this passes on a laptop and takes a different path on any
    # box that actually has the App configured (vera's container, CI with secrets).
    monkeypatch.delenv("PROTOREVIEW_APP_ID", raising=False)
    monkeypatch.delenv("PROTOREVIEW_APP_PRIVATE_KEY", raising=False)
    app = _health_app(tmp_path, {"summon_handle": "vera"})
    body = TestClient(app).get("/api/plugins/pr-reviewer/summon/health").json()

    assert body["summon_reachable"] is None  # NOT False
    assert body["subscribed"] is None
    assert body["missing"] is None
    assert "says nothing about whether summons work" in body["note"]
    assert "help" in body["note"]  # points at the check that actually answers it


def test_subscribed_events_are_reported_when_the_app_read_succeeds(tmp_path, monkeypatch):
    async def fake_events(_config, **_kw):
        return ["pull_request", "issue_comment", "pull_request_review_comment"]

    monkeypatch.setattr("pr_reviewer.app_auth.fetch_app_events", fake_events)
    app = _health_app(tmp_path, {"app_id": "1", "app_private_key": "pem"})
    body = TestClient(app).get("/api/plugins/pr-reviewer/summon/health").json()

    assert body["summon_reachable"] is True
    assert body["missing"] == []


def test_a_genuinely_missing_event_is_still_reported(tmp_path, monkeypatch):
    async def fake_events(_config, **_kw):
        return ["pull_request"]  # the shape the feature actually shipped with

    monkeypatch.setattr("pr_reviewer.app_auth.fetch_app_events", fake_events)
    app = _health_app(tmp_path, {"app_id": "1", "app_private_key": "pem"})
    body = TestClient(app).get("/api/plugins/pr-reviewer/summon/health").json()

    assert body["summon_reachable"] is False
    assert "issue_comment" in body["missing"]


# ── cross-PR panel concurrency cap (#96) ─────────────────────────────────────
#
# A burst of N eligible events fired N background panels at once, each fanning to
# ~5 finders (a 14-PR burst measured 70 concurrent LLM calls). build_routers now
# sizes an asyncio.Semaphore from `max_concurrent_panels` and holds a slot across
# each dispatch; the overflow QUEUES rather than dropping. These tests drive the
# real router over an async transport so the background tasks run concurrently in
# one event loop, and gate the dispatcher's panels on an Event to observe the peak.


class GatedDispatcher:
    """A dispatcher whose panels block until the test releases them, so the test can
    observe how many run at once. Each panel registers as running, records the peak,
    then parks on `release`; the semaphore is what keeps that peak at the cap."""

    def __init__(self, max_concurrent_panels=2):
        self.max_concurrent_panels = max_concurrent_panels
        self.cfg = {"summon_handle": "vera"}
        self.running = 0
        self.peak = 0
        self.completed = 0
        self.release = asyncio.Event()

    async def _panel(self):
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.release.wait()
        finally:
            self.running -= 1
            self.completed += 1

    async def handle_pr_event(self, repo, pr, head, action):
        await self._panel()
        return "reviewed:PASS"

    async def handle_summon(self, repo, pr, actor):
        await self._panel()
        return "reviewed:PASS"

    async def _viewer_login(self):
        return "qa-bot"


def _gated_app(tmp_path, dispatcher, *, run_gh_fn=None):
    telemetry = Telemetry(tmp_path)
    public, _api = build_routers(dispatcher, telemetry, lambda: SECRET, run_gh_fn=run_gh_fn)
    app = FastAPI()
    app.include_router(public, prefix="/plugins/pr-reviewer")
    return app, telemetry


def pr_payload(pr: int) -> bytes:
    return json.dumps(
        {
            "action": "opened",
            "repository": {"full_name": "o/r"},
            "pull_request": {"number": pr, "head": {"sha": f"{pr:040x}"}},
        }
    ).encode()


def summon_comment(text: str, pr: int, login: str = "dev") -> bytes:
    return json.dumps(
        {
            "action": "created",
            "repository": {"full_name": "o/r"},
            "issue": {"number": pr, "pull_request": {"url": "..."}},
            "comment": {"body": text, "user": {"login": login}},
        }
    ).encode()


async def _yield_until(predicate, *, limit=500):
    """Cycle the event loop until `predicate()` holds or `limit` turns elapse — never
    hangs, so a broken bound fails the assertion instead of deadlocking the suite."""
    for _ in range(limit):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


def test_the_panel_semaphore_is_sized_from_config_and_injected(tmp_path):
    """r1/r2: build_routers reads `max_concurrent_panels` and injects a semaphore of
    that size onto the dispatcher (so the sweep's backfill can share it)."""
    dispatcher = GatedDispatcher(max_concurrent_panels=2)
    build_routers(dispatcher, Telemetry(tmp_path), lambda: SECRET)
    sem = dispatcher.panel_sem
    assert isinstance(sem, asyncio.Semaphore)

    async def _exhaust():
        await sem.acquire()
        assert not sem.locked()  # one slot left
        await sem.acquire()
        assert sem.locked()  # cap reached at 2 — a third dispatch would queue, not drop

    asyncio.run(_exhaust())


async def test_concurrent_webhook_dispatch_is_bounded_and_excess_queues(tmp_path):
    """A burst of five eligible events must not run five panels at once: the semaphore
    caps concurrency at two, and the overflow queues (still dispatched) — none dropped."""
    dispatcher = GatedDispatcher(max_concurrent_panels=2)
    app, telemetry = _gated_app(tmp_path, dispatcher)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for pr in range(1, 6):
            body = pr_payload(pr)
            r = await client.post("/plugins/pr-reviewer/webhook", content=body, headers=signed(body))
            assert r.json()["dispatched"] is True  # accepted, not dropped

        await _yield_until(lambda: dispatcher.running >= 2)
        for _ in range(10):  # let the overflow tasks reach (and park on) the full semaphore
            await asyncio.sleep(0)
        assert dispatcher.running == 2 and dispatcher.peak == 2  # BOUNDED — never five

        queued = [e for e in telemetry.read_all() if e["event"] == "queued"]
        assert queued and queued[0]["limit"] == 2  # the queue depth is visible in telemetry

        dispatcher.release.set()  # slots free — every queued dispatch now proceeds
        await _yield_until(lambda: dispatcher.completed >= 5)
        assert dispatcher.completed == 5  # all five eventually reviewed; queued != dropped


async def test_concurrent_summons_are_bounded_by_the_same_cap(tmp_path):
    """A summon is still a panel: several admin `@vera review`s at once queue behind the
    same cross-PR cap rather than firing a panel each (r4)."""
    dispatcher = GatedDispatcher(max_concurrent_panels=2)

    async def fake_gh(args, timeout=30):
        if "/collaborators/" in " ".join(args):
            return 0, "admin", ""
        return 0, "", ""

    app, telemetry = _gated_app(tmp_path, dispatcher, run_gh_fn=fake_gh)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for pr in range(1, 6):
            body = summon_comment("@vera review", pr=pr)
            r = await client.post(
                "/plugins/pr-reviewer/webhook",
                content=body,
                headers={**signed(body), "X-GitHub-Event": "issue_comment"},
            )
            assert r.json() == {"ok": True, "dispatched": True, "reason": "summon:review"}

        await _yield_until(lambda: dispatcher.running >= 2)
        for _ in range(10):
            await asyncio.sleep(0)
        assert dispatcher.running == 2 and dispatcher.peak == 2  # summons bounded too

        queued = [e for e in telemetry.read_all() if e["event"] == "queued" and e["kind"] == "summon"]
        assert queued

        dispatcher.release.set()
        await _yield_until(lambda: dispatcher.completed >= 5)
        assert dispatcher.completed == 5  # queued summons all proceed

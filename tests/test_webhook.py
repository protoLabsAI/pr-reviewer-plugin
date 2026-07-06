"""The webhook route — HMAC is the auth; the eval endpoint reads telemetry."""

from __future__ import annotations

import hashlib
import hmac
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.webhook import build_routers

SECRET = "whsec"


class SpyDispatcher:
    def __init__(self):
        self.events: list[tuple] = []

    async def handle_pr_event(self, repo, pr, head, action):
        self.events.append((repo, pr, head, action))
        return "reviewed:PASS"

    async def evaluate_promotion(self, repo, pr):
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

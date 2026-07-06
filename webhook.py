"""The webhook ingress + operator API routers (ADR 0078 C).

Two routers, the standard split:

  - PUBLIC `/plugins/pr-reviewer/webhook` (manifest `public_paths`) — GitHub can't
    send a bearer; the HMAC (`X-Hub-Signature-256`) IS the auth, verified inside the
    handler against the plugin's `webhook_secret`. No secret configured ⇒ every
    delivery 403s (fail closed, never an open dispatch surface).
  - GATED `/api/plugins/pr-reviewer/*` — manual dispatch (the dry-run/operator
    path), promotion evaluation, and the eval report.

The webhook handler answers 202 immediately and reviews in a background task —
GitHub redelivers on slow responses, and redeliveries are exactly what the
chokepoint exists to eat.

NO `from __future__ import annotations` here: FastAPI must resolve the `Request`
annotation at def time (it's imported inside build_routers), or it silently becomes
a body field and every delivery 422s.
"""

import asyncio
import json
import logging

from .chokepoint import verify_signature

log = logging.getLogger("protoagent.plugins.pr_reviewer")


def build_routers(dispatcher, telemetry, get_secret):
    """(public_router, api_router). `get_secret` is a callable so a webhook-secret
    edit in Settings applies without a restart (live_config pattern)."""
    from fastapi import APIRouter, Body, HTTPException, Request

    public = APIRouter()
    api = APIRouter()

    @public.post("/webhook")
    async def _webhook(request: Request):
        body = await request.body()
        if not verify_signature(get_secret(), body, request.headers.get("X-Hub-Signature-256")):
            telemetry.emit("drop", reason="bad-signature", path="/webhook")
            raise HTTPException(status_code=403, detail="bad signature")
        if request.headers.get("X-GitHub-Event") != "pull_request":
            telemetry.emit("drop", reason="not-a-pr-event", gh_event=request.headers.get("X-GitHub-Event", ""))
            return {"ok": True, "dispatched": False, "reason": "not-a-pr-event"}
        try:
            payload = json.loads(body)
            action = str(payload.get("action") or "")
            repo = str(payload["repository"]["full_name"])
            pr = int(payload["pull_request"]["number"])
            head = str(payload["pull_request"]["head"]["sha"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            telemetry.emit("drop", reason="malformed-payload")
            return {"ok": True, "dispatched": False, "reason": "malformed-payload"}
        asyncio.get_running_loop().create_task(_safe_handle(repo, pr, head, action))
        return {"ok": True, "dispatched": True}

    async def _safe_handle(repo: str, pr: int, head: str, action: str) -> None:
        try:
            outcome = await dispatcher.handle_pr_event(repo, pr, head, action)
            log.info("[pr-reviewer] %s#%s @%s (%s) -> %s", repo, pr, head[:7], action, outcome)
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] webhook dispatch crashed for %s#%s", repo, pr)

    @api.post("/dispatch")
    async def _dispatch(body: dict = Body(...)):
        """Manual dispatch — the operator/dry-run path. Same chokepoint, same everything."""
        repo, pr = str(body.get("repo") or ""), int(body.get("pr") or 0)
        if not repo or not pr:
            raise HTTPException(status_code=400, detail="need repo (owner/name) and pr (number)")
        outcome = await dispatcher.handle_pr_event(repo, pr, str(body.get("sha") or f"manual-{pr}"), "opened")
        return {"repo": repo, "pr": pr, "outcome": outcome}

    @api.post("/promote")
    async def _promote(body: dict = Body(...)):
        """Evaluate (and, when owned+formal, apply) approve-on-green for one PR."""
        repo, pr = str(body.get("repo") or ""), int(body.get("pr") or 0)
        if not repo or not pr:
            raise HTTPException(status_code=400, detail="need repo (owner/name) and pr (number)")
        return {"repo": repo, "pr": pr, "decision": await dispatcher.evaluate_promotion(repo, pr)}

    @api.get("/eval")
    async def _eval():
        from .eval import build_report

        return build_report(telemetry.read_all())

    return public, api

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


def build_routers(dispatcher, telemetry, get_secret, run_gh_fn=None):
    """(public_router, api_router). `get_secret` is a callable so a webhook-secret
    edit in Settings applies without a restart (live_config pattern). `run_gh_fn`
    serves the three-way eval's GitHub reads (tests inject; None = the real gh)."""
    from fastapi import APIRouter, Body, HTTPException, Request

    if run_gh_fn is None:
        from .gh_cli import run_gh as run_gh_fn

    public = APIRouter()
    api = APIRouter()

    @public.post("/webhook")
    async def _webhook(request: Request):
        body = await request.body()
        if not verify_signature(get_secret(), body, request.headers.get("X-Hub-Signature-256")):
            telemetry.emit("drop", reason="bad-signature", path="/webhook")
            raise HTTPException(status_code=403, detail="bad signature")
        gh_event = request.headers.get("X-GitHub-Event", "")
        if gh_event == "issue_comment":
            return await _handle_comment(body)
        if gh_event != "pull_request":
            telemetry.emit("drop", reason="not-a-pr-event", gh_event=gh_event)
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

    async def _handle_comment(body: bytes) -> dict:
        """`@vera <verb>` on a PR (issue #28). The HMAC already authenticated GITHUB;
        this authenticates the AUTHOR, server-side, before spending a panel."""
        from .summon import NOT_A_SUMMON, help_text, is_admin, parse_command, refusal_text

        try:
            payload = json.loads(body)
            if str(payload.get("action") or "") not in ("created", "edited"):
                return {"ok": True, "dispatched": False, "reason": "not-a-comment-action"}
            issue = payload["issue"]
            if not issue.get("pull_request"):  # a plain issue is not reviewable
                return {"ok": True, "dispatched": False, "reason": "not-a-pull-request"}
            repo = str(payload["repository"]["full_name"])
            pr = int(issue["number"])
            comment = payload["comment"]
            text = str(comment.get("body") or "")
            login = str((comment.get("user") or {}).get("login") or "")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            telemetry.emit("drop", reason="malformed-payload", gh_event="issue_comment")
            return {"ok": True, "dispatched": False, "reason": "malformed-payload"}

        handles = await _handles()
        verb = parse_command(text, handles)
        if verb is None:
            return {"ok": True, "dispatched": False, "reason": NOT_A_SUMMON}
        # Never answer ourselves: our own review bodies mention the handle, and a bot
        # replying to its own comment is an infinite loop with a five-subagent price tag.
        if login.lower().removesuffix("[bot]") in {h.lower().removesuffix("[bot]") for h in handles}:
            return {"ok": True, "dispatched": False, "reason": "summon:self"}
        if verb == "help":
            await _reply(repo, pr, help_text(handles))
            telemetry.emit("summon", repo=repo, pr=pr, actor=login, verb="help")
            return {"ok": True, "dispatched": False, "reason": "summon:help"}
        if verb not in ("review",):
            await _reply(repo, pr, help_text(handles))
            telemetry.emit("summon", repo=repo, pr=pr, actor=login, verb=verb, outcome="unknown-verb")
            return {"ok": True, "dispatched": False, "reason": "summon:unknown-verb"}
        if not await is_admin(run_gh_fn, repo, login):
            await _reply(repo, pr, refusal_text(login, verb))
            telemetry.emit("summon", repo=repo, pr=pr, actor=login, verb=verb, outcome="refused-not-admin")
            return {"ok": True, "dispatched": False, "reason": "summon:refused-not-admin"}
        asyncio.get_running_loop().create_task(_safe_summon(repo, pr, login))
        return {"ok": True, "dispatched": True, "reason": "summon:review"}

    async def _handles() -> list[str]:
        """Names this reviewer answers to: the configured handle plus its own login, so
        `@the-bot review` works without configuring anything."""
        cfg_handle = str((dispatcher.cfg or {}).get("summon_handle") or "vera")
        try:
            viewer = await dispatcher._viewer_login()
        except Exception:  # noqa: BLE001 — an unreadable login must not disable summons
            viewer = ""
        return [h for h in (cfg_handle, viewer, (viewer or "").removesuffix("[bot]")) if h]

    async def _reply(repo: str, pr: int, message: str) -> None:
        rc, _out, err = await run_gh_fn(
            ["api", f"repos/{repo}/issues/{pr}/comments", "-X", "POST", "-f", f"body={message}"], timeout=30
        )
        if rc != 0:
            log.warning("[pr-reviewer] summon reply failed on %s#%s: %s", repo, pr, err[-200:])

    async def _safe_summon(repo: str, pr: int, actor: str) -> None:
        try:
            outcome = await dispatcher.handle_summon(repo, pr, actor)
            log.info("[pr-reviewer] summon %s#%s by @%s -> %s", repo, pr, actor, outcome)
            if outcome.startswith("drop:"):
                await _reply(
                    repo, pr, f"@{actor} — {outcome[5:]}: nothing ran. Try again once the current review finishes."
                )
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] summon crashed for %s#%s", repo, pr)

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

    @api.get("/eval/three-way")
    async def _eval_three_way():
        """The stage-1 comparison: telemetry summary + per-PR rows (ours vs Quinn vs
        CodeRabbit) + the rendered markdown report."""
        from .eval import build_report, render_report_markdown, three_way_rows

        events = telemetry.read_all()
        summary = build_report(events)
        rows = await three_way_rows(events, run_gh_fn)
        return {"summary": summary, "rows": rows, "markdown": render_report_markdown(summary, rows)}

    return public, api

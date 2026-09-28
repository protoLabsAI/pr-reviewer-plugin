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
import contextlib
import json
import logging
import time

from .chokepoint import DISPATCH_ACTIONS, verify_signature

log = logging.getLogger("protoagent.plugins.pr_reviewer")


MAX_REPLAY_TRIALS = 10  # a replay spends one full panel per trial per row (issue #200)


def _whole_number(raw, name: str):
    """`raw` as an int, or None when absent, or the 400 that says why (issue #200).
    Booleans and non-integral floats are rejected, not coerced: `int(True)` is 1 and
    `int(7.5)` is 7, which is a different PR / a different spend than was typed."""
    from fastapi import HTTPException

    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise HTTPException(status_code=400, detail=f"{name} must be a number, got {raw!r}")
    if isinstance(raw, float) and not raw.is_integer():
        raise HTTPException(status_code=400, detail=f"{name} must be a whole number, got {raw!r}")
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be a number, got {raw!r}") from None


def _pr_number(body: dict):
    """The `pr` an operator route was handed, as a positive int — or a 400. `int()` on a
    stray string used to surface as a 500, and a float was silently truncated."""
    from fastapi import HTTPException

    pr = _whole_number(body.get("pr"), "pr")
    if pr is not None and pr <= 0:
        raise HTTPException(status_code=400, detail=f"pr must be positive, got {pr}")
    return pr


def _replay_trials(body: dict) -> int:
    from fastapi import HTTPException

    trials = _whole_number(body.get("trials"), "trials")
    if trials is None:
        return 1
    if not 1 <= trials <= MAX_REPLAY_TRIALS:
        raise HTTPException(status_code=400, detail=f"trials must be 1..{MAX_REPLAY_TRIALS}, got {trials}")
    return trials


def build_routers(dispatcher, telemetry, get_secret, run_gh_fn=None):
    """(public_router, api_router). `get_secret` is a callable so a webhook-secret
    edit in Settings applies without a restart (live_config pattern). `run_gh_fn`
    serves the three-way eval's GitHub reads (tests inject; None = the real gh)."""
    from fastapi import APIRouter, Body, HTTPException, Request

    if run_gh_fn is None:
        from .gh_cli import run_gh as run_gh_fn

    from .dispatch import PanelQueue

    public = APIRouter()
    api = APIRouter()

    # The CROSS-PR panel bound (#96). The webhook fires one background panel per eligible
    # event with no ceiling, so a burst of N PRs launched N panels at once, each fanning
    # to ~5 finders — a 14-PR burst measured 70 concurrent LLM calls, 7× latency, and 5
    # panels exhausted mid-run. This caps concurrent panels; excess dispatches QUEUE
    # rather than drop, so every PR still gets reviewed, just not all at once. The
    # Dispatcher's chokepoint already stops the SAME PR running twice — this bounds DIFFERENT
    # PRs. Built here (a webhook-layer concern), then injected into the dispatcher so the
    # sweep's backfill panels honour the same bound instead of stacking on a live burst.
    #
    # A `PanelQueue` IS an asyncio.Semaphore (same acquire/release/limit), so it still binds
    # to the loop lazily at register-time and the sweep's plain-semaphore test path is
    # untouched; on top it tracks depth/positions/phase for GET /queue (#209).
    #
    # A re-registered dispatcher (issue #198) keeps the queue its in-flight handlers already
    # hold: a fresh one would let the new routes start `panel_limit` MORE panels on top of
    # those still draining the old one (review on #199, round 1). On reuse we only re-point
    # the queue's telemetry sink at THIS registration's, so the `queued` event still lands.
    panel_limit = max(1, int(getattr(dispatcher, "max_concurrent_panels", 3)))
    existing = getattr(dispatcher, "panel_sem", None)
    if isinstance(existing, PanelQueue):
        _panel_queue = existing
        _panel_queue.telemetry = telemetry
    elif isinstance(existing, asyncio.Semaphore):
        # A plain semaphore an in-flight handler already holds (only ever a test injection
        # on this path) — do not swap it out from under those handlers; keep it, and the
        # /queue endpoint degrades to reporting the limit alone.
        _panel_queue = existing
    else:
        _panel_queue = PanelQueue(panel_limit, telemetry=telemetry)
        dispatcher.panel_sem = _panel_queue

    def _panel_slot(kind: str, repo: str, pr: int, head: str = "", action: str = ""):
        """The slot to hold across a dispatch. A PanelQueue registers the waiter/running
        entry for GET /queue and emits its own `queued` telemetry; a plain semaphore (only a
        pre-injected test double reaches here) keeps the best-effort queued signal and
        acquires directly. Racy by nature — a slot may free before `acquire` — and harmless.

        When the round has a head and must WAIT for a slot, the PanelQueue path also publishes
        a `queued` QA-panel check on that head so GitHub shows the wait instead of nothing
        (#209). The publish runs outside the queue lock and is best-effort — a failure is
        logged in the dispatcher and never blocks the round.

        It fires ONLY for a `DISPATCH_ACTIONS` event, though: `_safe_handle` takes this slot for
        EVERY pull_request action, BEFORE `handle_pr_event` applies the DISPATCH_ACTIONS /
        in-flight / cooldown / allowlist filters. Left ungated, a non-dispatch action (labeled,
        review_requested, edited, closed) or a redelivery that has to wait would rewrite the
        head's required check to "Queued behind N" and then be dropped with no round to move it
        forward — even over a panel already `in_progress` for that head. Gating here keeps the
        non-dispatch actions from ever engaging `on_wait`; the dispatcher's `publish_queued_check`
        adds the second rail (allowlist + never clobber an already-started check) for a
        redelivery of a dispatch action whose head is already under review."""
        publish = getattr(dispatcher, "publish_queued_check", None)
        on_wait = None
        if head and callable(publish) and action in DISPATCH_ACTIONS:

            async def on_wait(ahead: int, eta_s):
                await publish(repo, head, ahead, eta_s)

        if isinstance(_panel_queue, PanelQueue):
            return _panel_queue.slot(repo=repo, pr=pr, head=head, kind=kind, on_wait=on_wait)
        if isinstance(_panel_queue, asyncio.Semaphore):
            if _panel_queue.locked():
                telemetry.emit("queued", kind=kind, repo=repo, pr=pr, limit=panel_limit)
            return _panel_queue
        return contextlib.nullcontext()

    @public.post("/webhook")
    async def _webhook(request: Request):
        body = await request.body()
        if not verify_signature(get_secret(), body, request.headers.get("X-Hub-Signature-256")):
            telemetry.emit("drop", reason="bad-signature", path="/webhook")
            raise HTTPException(status_code=403, detail="bad signature")
        gh_event = request.headers.get("X-GitHub-Event", "")
        if gh_event == "issue_comment":
            return await _handle_comment(body)
        if gh_event == "check_run":
            return await _handle_check_run(body)
        if gh_event == "pull_request_review_thread":
            return await _handle_review_thread(body)
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
        from .summon import (
            NOT_A_SUMMON,
            help_text,
            is_admin,
            parse_command,
            pause_text,
            refusal_text,
            resume_text,
        )

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
        if verb in ("pause", "resume"):
            if not await is_admin(run_gh_fn, repo, login):
                await _reply(repo, pr, refusal_text(login, verb))
                telemetry.emit("summon", repo=repo, pr=pr, actor=login, verb=verb, outcome="refused-not-admin")
                return {"ok": True, "dispatched": False, "reason": "summon:refused-not-admin"}
            await _reply(repo, pr, pause_text(login) if verb == "pause" else resume_text(login))
            telemetry.emit("summon", repo=repo, pr=pr, actor=login, verb=verb, outcome="ok")
            return {"ok": True, "dispatched": False, "reason": f"summon:{verb}"}
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

    async def _handle_review_thread(body: bytes) -> dict:
        """A review thread was resolved or unresolved — re-evaluate promotion so the
        `QA panel` check reports the CURRENT thread count.

        Without this the check contradicts its own instructions. It fails with "N
        unresolved review threads — resolve each thread ... and this clears on the next
        pass", but nothing here subscribed to the event that says a thread WAS resolved,
        so the only things that produced a next pass were a new push, an `@vera` summon,
        or a manual re-request. Doing exactly what the check asked left it red. Observed
        on protoLabsAI/protoAgent#3415: the run read FAILURE for ten hours after the
        threads were dealt with, and projectBoard-plugin read that stale red as CI failure
        and burned a coder attempt per tier against it (issue #111).

        `unresolved` is deliberate as well as `resolved`: re-opening a thread must be able
        to take the check back to red, or the gate is one-way and a reopened finding rides
        a green check.

        This is NOT a panel — `evaluate_promotion` re-reads state and republishes the
        check, with no model call — so it does not take the panel semaphore. It is also
        idempotent, which matters because GitHub delivers one event per thread and a batch
        resolve fires several at once."""
        try:
            payload = json.loads(body)
            action = str(payload.get("action") or "")
            if action not in ("resolved", "unresolved"):
                return {"ok": True, "dispatched": False, "reason": "not-a-resolution"}
            repo = str(payload["repository"]["full_name"])
            pr = int(payload["pull_request"]["number"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            telemetry.emit("drop", reason="malformed-payload", gh_event="pull_request_review_thread")
            return {"ok": True, "dispatched": False, "reason": "malformed-payload"}
        telemetry.emit("thread", repo=repo, pr=pr, action=action)
        asyncio.get_running_loop().create_task(_safe_reevaluate(repo, pr, action))
        return {"ok": True, "dispatched": True, "reason": f"thread-{action}"}

    async def _safe_reevaluate(repo: str, pr: int, action: str) -> None:
        """Republish the gate for one PR. Best-effort: a failure here must never take the
        webhook down, and the sweep remains the backstop."""
        try:
            decision = await dispatcher.evaluate_promotion(repo, pr)
            log.info("[pr-reviewer] thread %s on %s#%s -> %s", action, repo, pr, decision)
        except Exception:  # noqa: BLE001 — a re-evaluation must not kill the handler task
            log.warning("[pr-reviewer] re-evaluate after thread %s failed on %s#%s", action, repo, pr, exc_info=True)

    async def _handle_check_run(body: bytes) -> dict:
        """A `check_run` webhook. We act ONLY on `rerequested` for our own `protoReview`
        gate — a human clicked "Re-run" on the required check — and re-run the panel, the
        same force posture as a summon. Making the check a required status is pointless if
        a red X can only be cleared by pushing a dummy commit; this is how it is re-driven.

        Every other action is ignored, deliberately: our own `created`/`completed` events
        (we open and conclude the check ourselves) would otherwise loop the panel."""
        from .dispatch import REVIEW_CHECK_NAME

        try:
            payload = json.loads(body)
            if str(payload.get("action") or "") != "rerequested":
                return {"ok": True, "dispatched": False, "reason": "not-a-rerequest"}
            check_run = payload["check_run"]
            if str(check_run.get("name") or "") != REVIEW_CHECK_NAME:
                return {"ok": True, "dispatched": False, "reason": "not-our-check"}
            repo = str(payload["repository"]["full_name"])
            prs = check_run.get("pull_requests") or []
            pr = int(prs[0]["number"]) if prs else 0
            # The re-run is user-initiated in the GitHub UI, which already gates on write
            # access; `sender` is who clicked it, for the telemetry trail.
            actor = str((payload.get("sender") or {}).get("login") or "check-rerequest")
        except (KeyError, TypeError, ValueError, IndexError, json.JSONDecodeError):
            telemetry.emit("drop", reason="malformed-payload", gh_event="check_run")
            return {"ok": True, "dispatched": False, "reason": "malformed-payload"}
        if not pr:
            # A check run is not always tied to a PR (a branch push builds one too) —
            # there is nothing to re-review.
            return {"ok": True, "dispatched": False, "reason": "check-run-no-pr"}
        telemetry.emit("summon", repo=repo, pr=pr, actor=actor, verb="check-rerequest")
        asyncio.get_running_loop().create_task(_safe_summon(repo, pr, actor))
        return {"ok": True, "dispatched": True, "reason": "check-run-rerequest"}

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
        # A summon is still a panel — bound it by the same cross-PR cap (#96).
        try:
            async with _panel_slot("summon", repo, pr):
                outcome = await dispatcher.handle_summon(repo, pr, actor)
            log.info("[pr-reviewer] summon %s#%s by @%s -> %s", repo, pr, actor, outcome)
            if outcome.startswith("drop:"):
                await _reply(
                    repo, pr, f"@{actor} — {outcome[5:]}: nothing ran. Try again once the current review finishes."
                )
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] summon crashed for %s#%s", repo, pr)

    async def _safe_handle(repo: str, pr: int, head: str, action: str) -> None:
        # Hold a panel slot across the dispatch; a burst beyond the cap queues here
        # rather than launching every panel at once (#96).
        try:
            async with _panel_slot("webhook", repo, pr, head, action):
                outcome = await dispatcher.handle_pr_event(repo, pr, head, action)
            log.info("[pr-reviewer] %s#%s @%s (%s) -> %s", repo, pr, head[:7], action, outcome)
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] webhook dispatch crashed for %s#%s", repo, pr)

    @api.post("/dispatch")
    async def _dispatch(body: dict = Body(...)):
        """Manual dispatch — the operator/dry-run path. Same chokepoint, same everything."""
        repo, pr = str(body.get("repo") or ""), _pr_number(body)
        if not repo or not pr:
            raise HTTPException(status_code=400, detail="need repo (owner/name) and pr (number)")
        outcome = await dispatcher.handle_pr_event(repo, pr, str(body.get("sha") or f"manual-{pr}"), "opened")
        return {"repo": repo, "pr": pr, "outcome": outcome}

    @api.post("/promote")
    async def _promote(body: dict = Body(...)):
        """Evaluate (and, when owned+formal, apply) approve-on-green for one PR."""
        repo, pr = str(body.get("repo") or ""), _pr_number(body)
        if not repo or not pr:
            raise HTTPException(status_code=400, detail="need repo (owner/name) and pr (number)")
        return {"repo": repo, "pr": pr, "decision": await dispatcher.evaluate_promotion(repo, pr)}

    @api.get("/summon/health")
    async def _summon_health():
        """Can a summon actually reach us? (issue #28)

        `@vera review` is delivered as an `issue_comment` webhook. If the GitHub App
        subscribes only to `pull_request` — which it did when the feature shipped — the
        comment never arrives and the feature looks broken with no error anywhere: the
        code is correct, the event simply does not exist. This endpoint saves a trip to
        the App settings page to find that out.

        It does need the App JWT (`app_id` + `app_private_key`): `GET /app` takes no
        other credential. Without it the honest answer is UNKNOWN, and that is what it
        returns — the earlier version claimed to work without a JWT, read the resulting
        failure as "subscribed to nothing", and reported a live summon surface as dead.
        """
        from .app_auth import AppAuthConfig, fetch_app_events
        from .summon import required_app_events

        # `GET /app` is JWT-only. This used to go through `gh api`, which authenticates
        # with the INSTALLATION token — a credential that can never read /app — and the
        # resulting failure was recorded as "subscribed to []", i.e. everything missing.
        # It reported a working summon surface as dead, on a deployment that had already
        # received 331 `pull_request_review_comment` deliveries. A diagnostic that cannot
        # tell "no" from "I don't know" is worse than no diagnostic: it is believed.
        try:
            subscribed = await fetch_app_events(AppAuthConfig(dispatcher.cfg or {}))
        except Exception:  # noqa: BLE001 — a crashed read is "I don't know", never a 500 (issue #200)
            log.exception("[pr-reviewer] summon health: reading the App's event subscriptions crashed")
            subscribed = None
        if subscribed is None:
            return {
                "subscribed": None,
                "required": required_app_events(),
                "missing": None,
                "summon_reachable": None,  # UNKNOWN — not False
                "note": (
                    "Could not read the App's event subscriptions (GET /app needs the App JWT: "
                    "set app_id + app_private_key, or check them). This says nothing about "
                    "whether summons work — verify by commenting `@<handle> help` on a PR, "
                    "which needs no admin and spends no panel."
                ),
            }
        missing = [e for e in required_app_events() if e not in subscribed]
        return {
            "subscribed": subscribed,
            "required": required_app_events(),
            "missing": missing,
            "summon_reachable": not missing,
            "note": (
                "Add the missing event(s) to the GitHub App's subscriptions — the webhook "
                "URL and secret are unchanged. Without `issue_comment` a summon never arrives."
                if missing
                else "Summon events are subscribed."
            ),
        }

    @api.post("/replay")
    async def _replay(body: dict = Body(...)):
        """Run the panel on pinned rounds off the live-PR path, findings to JSON — the
        model A/B (qaEngineer#20). MUST run in-process: replay needs the live runner
        (`STATE.workflow_run`) and the minted GitHub App token, neither of which a fresh
        CLI process has. Side-effect-free (no GitHub writes), so the gated API is safe.

        Body: `{"manifest": [row, ...]}` or a single `row`, optional `model` / `trials` /
        `stamp` overriding each row. Returns `{"runs": [run-output, ...]}`.
        """
        from .replay import replay_review

        runner = dispatcher._runner()
        if runner is None:
            raise HTTPException(status_code=503, detail="no workflow runner — replay needs a live protoAgent host")
        manifest = body.get("manifest")
        if manifest is not None:
            # An explicit manifest is the rows — including an explicit EMPTY one, which
            # used to fall through and replay the envelope itself as a row (issue #200).
            if not isinstance(manifest, list):
                raise HTTPException(status_code=400, detail="manifest must be a list of rows")
            rows = manifest
        else:
            rows = [body["row"]] if body.get("row") else [body]
        model, trials = body.get("model"), _replay_trials(body)
        stamp, include_raw = str(body.get("stamp") or ""), bool(body.get("include_raw"))
        runs = []
        for row in rows:
            if model:
                row = {**row, "model": model}
            for trial in range(trials):
                runs.append(
                    await replay_review(
                        row,
                        run_gh=run_gh_fn,
                        runner=runner,
                        parse_findings=dispatcher._parse_findings,
                        trial=trial,
                        stamp=stamp,
                        include_raw=include_raw,
                        finder_timeout=dispatcher.finder_timeout_s,
                    )
                )
        return {"runs": runs}

    @api.get("/queue")
    async def _queue(repo: str | None = None, pr: int | None = None):
        """The panel queue, from IN-MEMORY state only — no GitHub, no network (#209).

        No args: the whole board — `limit`, the `running` panels (with `elapsed_s`, `phase`,
        `attempt`, `model_retries`), the `queued` panels (FIFO `position` + `eta_start_s`),
        `depth`, rolling `p50_panel_s`/`p90_panel_s`, `oldest_queued_s`, and the live gateway
        signal (`gateway_degraded`, `gateway_retry_rate_5m`). `?repo=owner/name&pr=N` narrows to one PR:
        `{state: running|queued|idle, position, eta_start_s, eta_verdict_s, head}`.

        ETAs are null until there is duration data; they use p90 when the gateway is degraded.
        `gateway_degraded` / `gateway_retry_rate_5m` are the live model/SDK retry signal (#209),
        read from the dispatcher; a dispatcher that predates the field degrades to False/None."""
        degraded = bool(getattr(dispatcher, "gateway_degraded", False))
        retry_rate = getattr(dispatcher, "gateway_retry_rate_5m", None)
        if not isinstance(_panel_queue, PanelQueue):
            # A plain semaphore (only a pre-injected test double) carries no tracking.
            if repo and pr is not None:
                return {"state": "idle", "position": None, "eta_start_s": None, "eta_verdict_s": None, "head": None}
            return {
                "generated_at": time.time(),
                "limit": panel_limit,
                "running": [],
                "queued": [],
                "depth": 0,
                "p50_panel_s": None,
                "p90_panel_s": None,
                "gateway_degraded": degraded,
                "gateway_retry_rate_5m": retry_rate,
                "oldest_queued_s": None,
            }
        if repo and pr is not None:
            return _panel_queue.lookup(str(repo), int(pr), degraded=degraded)
        return {
            "generated_at": time.time(),
            "gateway_degraded": degraded,
            "gateway_retry_rate_5m": retry_rate,
            **_panel_queue.status(degraded=degraded),
        }

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

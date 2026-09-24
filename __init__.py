"""pr-reviewer plugin — the deterministic PR-review machinery (protoAgent ADR 0078).

`register(registry)` is the ONLY place plugin code runs. Phase B2 shipped the
structural seat (`protopatch_review` + `structural-finder` + the
`code-review-structural` recipe, auto-discovered from `workflows/`). Phase C adds
the reviewer machinery around the panel:

  - webhook ingress (public path, HMAC-authed) → the dispatch chokepoint (typed
    drops) → structural-trigger recipe selection → `STATE.workflow_run` → pure
    verdict mapping → posted review (shadow: always COMMENT);
  - the approve-on-green pure function + a 3-minute sweep surface (promotion stays
    OFF until `promotion_owner: true` AND `shadow_mode: false` — two promoters
    racing is how double-merges happen);
  - JSONL telemetry + the eval that reads it (`GET /api/plugins/pr-reviewer/eval`).

Host-only imports stay LAZY so the test suite imports these modules with no
protoAgent host present.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger("protoagent.plugins.pr_reviewer")


def _state_home(cfg: dict) -> Path:
    return Path(
        cfg.get("state_root") or os.environ.get("PR_REVIEWER_HOME") or Path.home() / ".protoagent" / "pr-reviewer"
    )


# One reviewer machinery per state home per PROCESS (issue #198). A host config reload
# re-runs `register()` but keeps a surviving surface — and the sweep loop it started —
# alive on the FIRST dispatcher, while the re-registered routers get a second one. Two
# chokepoints cannot see each other's in-flight panels, so the sweep's backfill ran a
# duplicate panel on a head whose webhook panel was still running (7 of 99 dispatches on
# Vera, 2026-09-24, every one after a `POST /api/config`). Keyed by state home so tests
# with per-test homes still get fresh instances.
_MACHINERY: dict[str, dict] = {}


def register(registry) -> None:
    cfg = registry.config or {}

    n_tools = 0
    try:
        from .protopatch import get_tools

        for t in get_tools(cfg):
            registry.register_tool(t)
            n_tools += 1
    except Exception:  # noqa: BLE001 — never let one group sink the rest
        log.exception("[pr-reviewer] registering tools failed")

    n_subagents = 0
    if hasattr(registry, "register_subagent"):
        try:
            from .subagents import get_subagents

            for s in get_subagents():
                registry.register_subagent(s)
                n_subagents += 1
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] registering subagents failed")

    # ── Phase C machinery: telemetry + dispatcher + routers + sweep surface ──
    machinery = False
    try:
        from .dispatch import Dispatcher, sweep_loop
        from .telemetry import Telemetry
        from .webhook import build_routers

        live = registry.live_config if hasattr(registry, "live_config") else (lambda: cfg)
        home = str(_state_home(cfg))
        shared = _MACHINERY.get(home)
        if shared is not None:
            # Re-registered in a live process (a config reload): keep the running
            # dispatcher — its chokepoint, backfills and round caps are the state that
            # must stay singular — and just refresh the config it was booted with.
            telemetry = shared["telemetry"]
            dispatcher = shared["dispatcher"]
            dispatcher._cfg = cfg or {}
            dispatcher._cfg_provider = live
            log.info("[pr-reviewer] re-registered: reusing the running dispatcher (issue #198)")
        else:
            telemetry = Telemetry(home)
            # The dispatcher resolves its knobs through the SAME live view the webhook
            # secret already used (issue #11) — `repos`, `shadow_mode`, the kill switches.
            # Snapshotting them meant an operator flipping the gate through Settings got a
            # silent no-op until the container restarted.
            dispatcher = Dispatcher(cfg, telemetry, cfg_provider=live)
            shared = _MACHINERY[home] = {"telemetry": telemetry, "dispatcher": dispatcher}

        def _secret() -> str:
            # Config first (Settings → secrets overlay); env fallback for headless
            # config-as-code deployments, where the secrets overlay can't be baked
            # (secret keys are stripped from the main YAML) — the linear-plugin
            # pattern. Empty ⇒ the webhook 403s everything (fail closed).
            try:
                value = str((live() or {}).get("webhook_secret") or "")
            except Exception:  # noqa: BLE001
                value = str(cfg.get("webhook_secret") or "")
            return value or os.environ.get("PR_REVIEWER_WEBHOOK_SECRET", "")

        public, api = build_routers(dispatcher, telemetry, _secret)
        registry.register_router(public, prefix="/plugins/pr-reviewer")
        registry.register_router(api, prefix="/api/plugins/pr-reviewer")

        # The agent-facing eval command (issue-tracked as the three-way report seam).
        from .eval import get_eval_tools
        from .gh_cli import run_gh

        for t in get_eval_tools(telemetry, run_gh):
            registry.register_tool(t)
            n_tools += 1

        if hasattr(registry, "register_surface"):
            # GitHub App identity (optional): when App credentials are configured,
            # a refresher keeps a fresh installation token in GH_TOKEN/GITHUB_TOKEN
            # (App tokens expire hourly; gh has no App mode). Reviews then post as
            # the App's bot identity — no machine-user PAT.
            #
            # Registered BEFORE the sweep so the installation token exists before the
            # sweep's first repo enumeration runs (issue #99). Registering it after
            # meant the first enumeration raced the token refresher, failed, and the
            # first sweep pass reviewed 0 repos for one interval.
            from .app_auth import AppAuthConfig, token_refresh_loop

            app_cfg = AppAuthConfig(cfg)
            if app_cfg.configured:
                auth_stop = asyncio.Event()

                def _auth_start():
                    return asyncio.get_running_loop().create_task(token_refresh_loop(app_cfg, auth_stop))

                def _auth_stop():
                    auth_stop.set()

                registry.register_surface(_auth_start, _auth_stop, name="pr-reviewer-app-auth")

            interval = int(cfg.get("sweep_interval_s") or 180)

            def _start():
                # Runs in the server's startup hook — the loop exists here. One sweep loop
                # per process (issue #198): a second start, whatever asks for it, returns
                # the task already running rather than racing it.
                running = shared.get("sweep_task")
                if running is not None and not running.done():
                    log.info("[pr-reviewer] sweep loop already running; not starting another (issue #198)")
                    return running
                stop_event = asyncio.Event()
                shared["sweep_stop"] = stop_event
                task = asyncio.get_running_loop().create_task(sweep_loop(dispatcher, interval, stop_event))
                shared["sweep_task"] = task
                return task

            def _stop():
                stop_event = shared.get("sweep_stop")
                if stop_event is not None:
                    stop_event.set()

            registry.register_surface(_start, _stop, name="pr-reviewer-sweep")
        machinery = True
    except Exception:  # noqa: BLE001
        log.exception("[pr-reviewer] registering the reviewer machinery failed")

    log.info(
        "[pr-reviewer] registered %d tool(s) + %d subagent(s)%s; workflows/ is host-discovered",
        n_tools,
        n_subagents,
        " + webhook/dispatch/sweep machinery" if machinery else "",
    )

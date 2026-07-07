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

        telemetry = Telemetry(_state_home(cfg))
        dispatcher = Dispatcher(cfg, telemetry)

        live = registry.live_config if hasattr(registry, "live_config") else (lambda: cfg)

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
            stop_event = asyncio.Event()
            interval = int(cfg.get("sweep_interval_s") or 180)

            def _start():
                # Runs in the server's startup hook — the loop exists here.
                return asyncio.get_running_loop().create_task(sweep_loop(dispatcher, interval, stop_event))

            def _stop():
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

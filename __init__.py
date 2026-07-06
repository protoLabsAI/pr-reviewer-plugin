"""pr-reviewer plugin — the deterministic PR-review machinery (protoAgent ADR 0078).

`register(registry)` is the ONLY place plugin code runs. Phase B2 scope: the
`protopatch_review` tool (a budgeted, checkout-cached protoPatch structural pass with
findings mapped into the ADR 0077 contract) and the `structural-finder` subagent that
seats it on the review panel. The `workflows/code-review-structural.yaml` recipe is
data — the host auto-discovers the conventional `workflows/` dir.

Later phases add the webhook chokepoint, structural-trigger dispatch, the
approve-on-green pure function + sweep, and the review eval (ADR 0078 Phase C).

Host-only imports stay LAZY (SubagentConfig inside subagents.get_subagents) so the
test suite imports these modules with no protoAgent host present.
"""

from __future__ import annotations

import logging

log = logging.getLogger("protoagent.plugins.pr_reviewer")


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

    log.info(
        "[pr-reviewer] registered %d tool(s) + %d subagent(s); workflows/ is host-discovered",
        n_tools,
        n_subagents,
    )

"""#318: the correctness and removed-behavior lanes run as `review-finder-deep`, on
`pr_reviewer.deep_finder_model` when it is set, and fall back to the default model when
that model fails."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field

import pytest
from pr_reviewer import deep_lanes, subagents
from pr_reviewer.deep_lanes import DEEP_LANES, DeepLanes


@dataclass
class _Cfg:
    name: str
    description: str = ""
    system_prompt: str = "finder prompt"
    tools: list = field(default_factory=lambda: ["github_pr_diff"])
    model: str = ""
    max_turns: int = 30
    lead_visible: bool = True
    allow_skill_emission: bool = True


class _Tel:
    def __init__(self):
        self.rows = []

    def emit(self, event, **kw):
        self.rows.append({"event": event, **kw})


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def host(monkeypatch):
    """A fake host: `graph.subagents.config` with REVIEW_FINDER_CONFIG and a registry."""
    registry: dict = {}
    base = _Cfg(name="review-finder")
    mod = types.ModuleType("graph.subagents.config")
    mod.REVIEW_FINDER_CONFIG = base
    mod.SUBAGENT_REGISTRY = registry
    mod.SubagentConfig = _Cfg
    for name in ("graph", "graph.subagents"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "graph.subagents.config", mod)
    deep = subagents.deep_finder()
    registry[deep.name] = deep
    return registry, base


def test_the_deep_finder_is_a_renamed_copy_of_the_host_finder(host):
    registry, base = host
    deep = registry["review-finder-deep"]
    assert deep is not base
    assert deep.system_prompt == base.system_prompt and deep.tools == base.tools
    assert deep.max_turns == base.max_turns
    assert deep.model == "" and deep.lead_visible is False
    assert base.name == "review-finder" and base.model == ""  # the original is untouched


def test_no_host_finder_means_no_deep_finder(monkeypatch):
    monkeypatch.setitem(sys.modules, "graph.subagents.config", types.ModuleType("graph.subagents.config"))
    assert subagents.deep_finder() is None


def test_unset_leaves_the_lanes_on_the_default_model(host):
    registry, _ = host
    lanes = DeepLanes(_Tel())
    assert lanes.apply("") == ""
    assert registry["review-finder-deep"].model == ""


def test_configured_model_is_pinned_on_the_registry_entry(host):
    registry, _ = host
    tel = _Tel()
    lanes = DeepLanes(tel, token_probe=lambda: True)
    assert lanes.apply("anthropic-oauth:claude-sonnet-5-5") == "anthropic-oauth:claude-sonnet-5-5"
    assert registry["review-finder-deep"].model == "anthropic-oauth:claude-sonnet-5-5"
    assert tel.rows[-1] == {
        "event": "deep_lane",
        "decision": "model",
        "model": "anthropic-oauth:claude-sonnet-5-5",
        "why": "configured",
    }
    # Unchanged on the next round: no repeat event.
    lanes.apply("anthropic-oauth:claude-sonnet-5-5")
    assert len(tel.rows) == 1
    # Cleared in config: back to the default on the next round.
    assert lanes.apply("") == ""
    assert registry["review-finder-deep"].model == ""


def test_no_oauth_token_keeps_the_default_model(host):
    registry, _ = host
    lanes = DeepLanes(_Tel(), token_probe=lambda: False)
    assert lanes.apply("anthropic-oauth:claude-sonnet-5-5") == ""
    assert registry["review-finder-deep"].model == ""
    assert lanes.effective("anthropic-oauth:claude-sonnet-5-5") == ("", "no-oauth-token")
    # A non-OAuth model is not gated on the Claude token.
    assert lanes.apply("gateway:protolabs/reasoning") == "gateway:protolabs/reasoning"


def test_a_deep_lane_failure_sends_the_retry_to_the_default_model(host):
    registry, _ = host
    tel, clock = _Tel(), _Clock()
    lanes = DeepLanes(tel, clock=clock, token_probe=lambda: True)
    model = "anthropic-oauth:claude-sonnet-5-5"
    lanes.apply(model)
    steps = {"find_correctness": "Error: Subagent 'review-finder-deep' failed: Error code: 429 - rate_limit_error"}
    assert lanes.trip(steps, ["find_correctness", "verify"], 1800) == ["find_correctness"]
    fb = [r for r in tel.rows if r.get("decision") == "fallback"][-1]
    assert fb["reason"] == "provider" and fb["lanes"] == ["find_correctness"] and fb["model"] == model
    # The retry: default model, and the event says why.
    assert lanes.apply(model) == ""
    assert registry["review-finder-deep"].model == ""
    assert tel.rows[-1]["why"] == "breaker-open:provider"
    # After the cooldown the configured model is back.
    clock.t += 1801
    assert lanes.apply(model) == model


def test_a_lane_error_that_is_not_the_provider_still_falls_back_but_says_so(host):
    tel = _Tel()
    lanes = DeepLanes(tel, token_probe=lambda: True)
    lanes.apply("anthropic-oauth:claude-sonnet-5-5")
    lanes.trip({"find_removed_behavior": "Error: KeyError: 'x'"}, ["find_removed_behavior"], 60)
    assert [r["reason"] for r in tel.rows if r.get("decision") == "fallback"] == ["lane-error"]


def test_failures_elsewhere_or_on_the_default_model_do_not_trip(host):
    tel = _Tel()
    lanes = DeepLanes(tel, token_probe=lambda: True)
    # On the default model a deep lane failure is just a failed lane: no breaker.
    lanes.apply("")
    assert lanes.trip({"find_correctness": "Error code: 429"}, ["find_correctness"], 60) == []
    lanes.apply("anthropic-oauth:claude-sonnet-5-5")
    assert lanes.trip({"find_crossfile": "Error code: 429"}, ["find_crossfile"], 60) == []
    assert not [r for r in tel.rows if r.get("decision") == "fallback"]


def test_the_deep_lanes_are_the_two_the_recipe_routes():
    import pathlib

    import yaml

    recipe = yaml.safe_load(
        (pathlib.Path(subagents.__file__).parent / "workflows" / "code-review-structural.yaml").read_text()
    )
    routed = sorted(s["id"] for s in recipe["steps"] if s.get("subagent") == subagents.DEEP_FINDER_NAME)
    assert routed == sorted(DEEP_LANES)


def test_get_subagents_registers_the_deep_finder(host):
    names = [s.name for s in subagents.get_subagents()]
    assert names == ["structural-finder", "review-finder-deep"]


def test_module_has_no_host_import_at_import_time():
    assert "graph" not in deep_lanes.__dict__


# ── through the dispatcher ──────────────────────────────────────────────────────────────

_CLEAN = "<!-- brief -->\nClean.\n<!-- /brief -->\n\n```json\n[]\n```"


async def test_dispatcher_retries_on_the_default_model_after_a_deep_lane_fails(tmp_path, host, monkeypatch):
    """Attempt 1: find_correctness fails on the Claude lane (429). The breaker opens, so
    attempt 2 runs it on the default model and the round posts instead of exhausting."""
    from tests.dispatch_helpers import HEAD, make
    from tests.test_dispatch import _events, _panel_steps, _structural_gh

    registry, _ = host
    monkeypatch.setattr(deep_lanes, "_oauth_token_present", lambda: True)
    seen = []

    async def runner(name, inputs):
        model = registry["review-finder-deep"].model
        seen.append(model)
        if model:
            err = "Error: step 'find_correctness' raised SubagentError: Subagent 'review-finder-deep' failed: Error code: 429"
            return {"output": _CLEAN, "failed": ["find_correctness"], "steps": _panel_steps(find_correctness=err)}
        return {"output": _CLEAN, "failed": [], "steps": _panel_steps()}

    cfg = {"shadow_mode": False, "panel_retries": 1, "deep_finder_model": "anthropic-oauth:claude-sonnet-5-5"}
    d = make(tmp_path, cfg=cfg, gh=_structural_gh(), runner=runner)
    d._deep_lanes._token_probe = lambda: True
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out.startswith("reviewed:"), out
    assert seen == ["anthropic-oauth:claude-sonnet-5-5", ""]
    fb = [e for e in _events(tmp_path, "deep_lane") if e.get("decision") == "fallback"]
    assert fb and fb[0]["reason"] == "provider"
    assert _events(tmp_path, "reviewed")[-1]["deep_model"] is None  # the posted round ran on the default


async def test_dispatcher_records_the_deep_model_on_the_reviewed_row(tmp_path, host):
    from tests.dispatch_helpers import HEAD, make
    from tests.test_dispatch import _events, _panel_steps, _structural_gh

    async def runner(name, inputs):
        return {"output": _CLEAN, "failed": [], "steps": _panel_steps()}

    cfg = {"shadow_mode": False, "deep_finder_model": "gateway:protolabs/reasoning"}
    d = make(tmp_path, cfg=cfg, gh=_structural_gh(), runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")
    assert _events(tmp_path, "reviewed")[-1]["deep_model"] == "gateway:protolabs/reasoning"
    assert host[0]["review-finder-deep"].model == "gateway:protolabs/reasoning"


async def test_dispatcher_unset_knob_changes_nothing(tmp_path, host):
    from tests.dispatch_helpers import HEAD, make
    from tests.test_dispatch import _events, _panel_steps, _structural_gh

    async def runner(name, inputs):
        return {"output": _CLEAN, "failed": [], "steps": _panel_steps()}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=_structural_gh(), runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")
    assert _events(tmp_path, "reviewed")[-1]["deep_model"] is None
    assert host[0]["review-finder-deep"].model == ""

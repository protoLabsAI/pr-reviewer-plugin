"""#335: plugin review copies read the PR through PR-head tools, not the lead's
empty-workspace reads, whose "(no matches)" used to read as evidence of absence."""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass, field, fields
from pathlib import Path

import pytest
import yaml
from pr_reviewer import subagents

WORKSPACE = ("search_files", "find_files", "list_dir", "read_file", "list_projects")
PR_HEAD = ("search_pr_repo", "list_pr_repo_files", "github_pr_diff", "github_read_pr_file", "github_read_file")


@dataclass
class _NewCfg:
    """A host SubagentConfig with read-tool inheritance (protoAgent#4471)."""

    name: str
    description: str = ""
    system_prompt: str = "finder prompt"
    tools: list = field(default_factory=list)
    model: str = ""
    max_turns: int = 30
    lead_visible: bool = True
    allow_skill_emission: bool = True
    inherit_read_tools: bool = True
    completion_check: object = None
    completion_contract: str = ""


@dataclass
class _OldCfg:
    """An older host SubagentConfig: no `inherit_read_tools`, rejects unknown fields."""

    name: str
    description: str = ""
    system_prompt: str = "finder prompt"
    tools: list = field(default_factory=lambda: ["github_pr_diff", "read_file"])
    model: str = ""
    max_turns: int = 30
    allow_skill_emission: bool = True


def _install(monkeypatch, cfg_cls, base):
    mod = types.ModuleType("graph.subagents.config")
    mod.REVIEW_FINDER_CONFIG = base
    mod.SubagentConfig = cfg_cls
    for name in ("graph", "graph.subagents"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "graph.subagents.config", mod)


@pytest.fixture
def new_host(monkeypatch):
    base = _NewCfg(
        name="review-finder",
        tools=[
            "github_pr_diff",
            "github_get_commit_diff",
            "read_file",
            "github_read_pr_file",
            "search_files",
            "github_get_pr",
            "search_pr_repo",
            "list_dir",
        ],
    )
    _install(monkeypatch, _NewCfg, base)
    return base


# ── criterion 1: the deep lane on an inheriting host ───────────────────────────


def test_deep_finder_opts_out_of_inheritance_and_lists_pr_head_tools(new_host):
    deep = subagents.deep_finder()
    assert deep.inherit_read_tools is False
    for name in WORKSPACE:
        assert name not in deep.tools, name
    for name in PR_HEAD:
        assert deep.tools.count(name) == 1, name
    # the host finder's other tools are kept, in order
    assert deep.tools[:4] == ["github_pr_diff", "github_get_commit_diff", "github_read_pr_file", "github_get_pr"]
    assert len(deep.tools) == len(set(deep.tools))


def test_the_host_finder_is_left_untouched(new_host):
    before = list(new_host.tools)
    deep = subagents.deep_finder()
    assert new_host.tools == before and new_host.inherit_read_tools is True
    assert deep.tools is not new_host.tools
    assert deep.name == "review-finder-deep" and deep.model == "" and deep.lead_visible is False


def test_a_host_finder_with_no_tools_still_gets_the_pr_head_tools(monkeypatch):
    _install(monkeypatch, _NewCfg, _NewCfg(name="review-finder", tools=None))
    deep = subagents.deep_finder()
    assert sorted(deep.tools) == sorted(PR_HEAD)


# ── criterion 2: an older host is unchanged ────────────────────────────────────


def test_an_older_host_gets_the_same_copy_as_before(monkeypatch):
    base = _OldCfg(name="review-finder")
    _install(monkeypatch, _OldCfg, base)
    deep = subagents.deep_finder()
    assert deep is not None
    assert "inherit_read_tools" not in {f.name for f in fields(deep)}
    assert deep.tools == base.tools  # no tool-list rewrite without the inheritance field
    assert deep.name == "review-finder-deep" and deep.model == ""


# ── criterion 3: the structural relay is unchanged ─────────────────────────────


def test_structural_finder_keeps_its_single_tool(new_host):
    by_name = {s.name: s for s in subagents.get_subagents()}
    relay = by_name["structural-finder"]
    assert relay.tools == ["protopatch_review"] and relay.inherit_read_tools is False
    assert by_name["review-finder-deep"].inherit_read_tools is False


# ── criterion 4: the recipe ────────────────────────────────────────────────────


def _steps() -> dict:
    path = Path(__file__).resolve().parent.parent / "workflows" / "code-review-structural.yaml"
    return {s["id"]: s for s in yaml.safe_load(path.read_text())["steps"]}


def test_deep_lane_prompts_drop_the_workspace_warning_but_keep_the_pr_search():
    steps = _steps()
    for sid in ("find_correctness", "find_removed_behavior"):
        assert steps[sid]["subagent"] == "review-finder-deep", sid
        prompt = steps[sid]["prompt"]
        assert "unrelated, empty local workspace" not in prompt, sid
        assert "search_pr_repo" in prompt and "list_pr_repo_files" in prompt, sid


LANE_SUBAGENTS = {
    "find_correctness": "review-finder-deep",
    "find_removed_behavior": "review-finder-deep",
    "find_crossfile": "review-finder-pr",
    "find_conventions": "review-finder-pr",
    "verify": "review-verifier",
}


def test_every_lane_and_the_verifier_run_on_a_plugin_pr_head_copy():
    steps = _steps()
    assert {sid: steps[sid]["subagent"] for sid in LANE_SUBAGENTS} == LANE_SUBAGENTS


def test_no_step_prompt_mentions_the_empty_workspace():
    steps = _steps()
    for sid, step in steps.items():
        prompt = step.get("prompt", "")
        assert "empty local workspace" not in prompt, sid
        assert "empty workspace" not in prompt, sid
    for sid in LANE_SUBAGENTS:
        prompt = steps[sid]["prompt"]
        assert "search_pr_repo" in prompt and "list_pr_repo_files" in prompt, sid
        assert "pr={{inputs.pr}}" in prompt and "SEARCH ERROR" in prompt, sid


def test_every_lane_subagent_is_registered_with_only_pr_head_reads(new_host):
    mod = sys.modules["graph.subagents.config"]
    mod.VERIFIER_CONFIG = _NewCfg(
        name="verifier",
        system_prompt="verify prompt",
        tools=["github_pr_diff", "read_file", "find_files", "search_files", "list_dir", "github_read_file"],
    )
    by_name = {cfg.name: cfg for cfg in subagents.get_subagents()}
    steps = _steps()
    lane_ids = [sid for sid, s in steps.items() if sid.startswith("find_") or sid == "verify"]
    assert set(LANE_SUBAGENTS) <= set(lane_ids)
    for sid in lane_ids:
        name = steps[sid]["subagent"]
        assert name in by_name, (sid, name)
        if sid == "find_structural":
            continue  # the protoPatch relay holds only protopatch_review (asserted above)
        cfg = by_name[name]
        assert cfg.inherit_read_tools is False, name
        for tool in ("search_files", "find_files", "list_dir", "read_file"):
            assert tool not in cfg.tools, (name, tool)
        assert "search_pr_repo" in cfg.tools, name


@pytest.mark.parametrize("cfg_cls", [_NewCfg, _OldCfg])
def test_registered_review_copies_preserve_host_contracts(monkeypatch, cfg_cls):
    from copy import deepcopy

    finder = cfg_cls(
        name="review-finder",
        model="finder-model",
        tools=[*WORKSPACE, "github_get_pr", *PR_HEAD, "search_pr_repo"],
    )
    verifier = cfg_cls(
        name="verifier",
        model="verifier-model",
        system_prompt="verify prompt",
        max_turns=17,
        tools=[*WORKSPACE, "github_get_commit_diff", *PR_HEAD, "github_pr_diff"],
    )
    _install(monkeypatch, cfg_cls, finder)
    sys.modules["graph.subagents.config"].VERIFIER_CONFIG = verifier
    originals = deepcopy((finder, verifier))
    registered = subagents.get_subagents()
    assert [cfg.name for cfg in registered] == [
        "structural-finder",
        "review-finder-deep",
        "review-finder-pr",
        "review-verifier",
    ]
    assert registered[0].tools == ["protopatch_review"]
    if cfg_cls is _NewCfg:
        assert registered[0].inherit_read_tools is False
    for cfg, source in zip(registered[1:], (finder, finder, verifier), strict=True):
        assert cfg is not source
        assert cfg.description != source.description
        assert cfg.system_prompt == source.system_prompt
        assert cfg.max_turns == source.max_turns
        assert cfg.allow_skill_emission == source.allow_skill_emission
        assert cfg.model == ("" if cfg.name == "review-finder-deep" else source.model)
        if cfg_cls is _NewCfg:
            assert cfg.lead_visible is False
            assert cfg.inherit_read_tools is False
            assert cfg.completion_check == source.completion_check
            assert cfg.completion_contract == source.completion_contract
            assert not set(WORKSPACE).intersection(cfg.tools)
            assert all(cfg.tools.count(tool) == 1 for tool in PR_HEAD)
            assert cfg.tools == list(dict.fromkeys(t for t in source.tools if t not in WORKSPACE))
            assert cfg.tools is not source.tools
        else:
            assert "inherit_read_tools" not in {f.name for f in fields(cfg)}
            assert cfg.tools == source.tools
    assert (finder, verifier) == originals


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        ("VERIFIER_CONFIG", ["structural-finder", "review-finder-deep", "review-finder-pr"]),
        ("REVIEW_FINDER_CONFIG", ["structural-finder", "review-verifier"]),
    ],
)
def test_missing_source_omits_only_its_copies(monkeypatch, missing, expected):
    _install(monkeypatch, _NewCfg, _NewCfg(name="review-finder"))
    mod = sys.modules["graph.subagents.config"]
    mod.VERIFIER_CONFIG = _NewCfg(name="verifier")
    delattr(mod, missing)
    assert [cfg.name for cfg in subagents.get_subagents()] == expected
    if missing == "VERIFIER_CONFIG":
        assert subagents.review_verifier() is None
    else:
        assert subagents.deep_finder() is None
        assert subagents.pr_finder() is None

"""register(registry) wires the tool and the panel seat — host-free."""

from __future__ import annotations

import pr_reviewer
import pytest

from tests.conftest import FakeRegistry


@pytest.fixture
def no_app_env(monkeypatch):
    """Clear ambient GitHub App credentials.

    `AppAuthConfig` falls back to the ENV, so any test asserting the BYO-GH_TOKEN
    shape passes on a laptop and fails on a box that actually has the App configured
    (vera's container, CI with secrets). Found by extending a review finding about the
    same dependence in the summon-health tests.
    """
    monkeypatch.delenv("PROTOREVIEW_APP_ID", raising=False)
    monkeypatch.delenv("PROTOREVIEW_APP_PRIVATE_KEY", raising=False)


def test_registers_tools_subagent_routers_and_surface(no_app_env):
    reg = FakeRegistry({"default_repo": "octo/repo"})
    pr_reviewer.register(reg)
    assert [t.name for t in reg.tools] == ["protopatch_review", "pr_review_eval"]
    assert [s.name for s in reg.subagents] == ["structural-finder"]
    assert [p for _r, p in reg.routers] == ["/plugins/pr-reviewer", "/api/plugins/pr-reviewer"]
    assert [s["name"] for s in reg.surfaces] == ["pr-reviewer-sweep"]


def test_tool_has_a_description():
    # An f-string docstring silently ships no description — pin the plain literal.
    reg = FakeRegistry({})
    pr_reviewer.register(reg)
    assert "structural" in (reg.tools[0].description or "").lower()


def test_structural_finder_is_a_thin_relay():
    reg = FakeRegistry({})
    pr_reviewer.register(reg)
    finder = reg.subagents[0]
    assert finder.tools == ["protopatch_review"]
    assert finder.max_turns <= 6  # a relay, not a reviewer
    assert finder.allow_skill_emission is False
    prompt = finder.system_prompt
    assert "EXACTLY ONCE" in prompt and "Gap" in prompt


def test_registers_on_a_minimal_host_without_subagent_seam():
    class MinimalRegistry:
        config = {}
        tools: list = []

        def register_tool(self, tool):
            self.tools.append(tool)

    reg = MinimalRegistry()
    pr_reviewer.register(reg)  # must not raise
    assert len(reg.tools) == 1


def test_machinery_registers_the_eval_tool():
    reg = FakeRegistry({})
    pr_reviewer.register(reg)
    names = [t.name for t in reg.tools]
    assert "pr_review_eval" in names
    tool = next(t for t in reg.tools if t.name == "pr_review_eval")
    assert "three-way" in (tool.description or "").lower() or "quinn" in (tool.description or "").lower()


def test_app_auth_surface_registers_only_when_configured(monkeypatch, no_app_env):
    reg = FakeRegistry({})
    pr_reviewer.register(reg)
    assert [s["name"] for s in reg.surfaces] == ["pr-reviewer-sweep"]  # BYO GH_TOKEN mode

    monkeypatch.setenv("PROTOREVIEW_APP_ID", "1")
    monkeypatch.setenv("PROTOREVIEW_APP_PRIVATE_KEY", "PEM")
    reg2 = FakeRegistry({})
    pr_reviewer.register(reg2)
    assert [s["name"] for s in reg2.surfaces] == ["pr-reviewer-sweep", "pr-reviewer-app-auth"]

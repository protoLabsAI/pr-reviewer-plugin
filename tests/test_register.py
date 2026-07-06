"""register(registry) wires the tool and the panel seat — host-free."""

from __future__ import annotations

import pr_reviewer

from tests.conftest import FakeRegistry


def test_registers_tools_subagent_routers_and_surface():
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

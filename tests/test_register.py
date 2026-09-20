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
    # A relay, not a reviewer — but the budget is host graph SUPER-STEPS, not model
    # turns; the floor is pinned by tests/test_structural_budget.py (#119).
    assert finder.max_turns <= 12
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
    # app-auth registers BEFORE sweep so the installation token exists before the
    # sweep's first repo enumeration runs (issue #99).
    assert [s["name"] for s in reg2.surfaces] == ["pr-reviewer-app-auth", "pr-reviewer-sweep"]


# ── the structural relay declares what a finished answer contains (protoAgent#3553) ──


def test_the_relay_contract_rejects_a_paraphrased_payload():
    from pr_reviewer.subagents import relay_delivered

    # Verbatim from a live run (pr-reviewer-plugin#133, 2026-09-20): protoPatch returned
    # three findings and the relay re-wrote them as a report. No array, so they were lost.
    paraphrase = (
        "## Protopatch Findings — `dispatch.py` & `tests/test_dispatch.py`\n\n"
        "**Scope:** 6 changed files · 3 reportable findings · all high-confidence\n\n---\n\n"
        "### 1. 🔹 `dispatch.py:493` — API Contract (minor)\n\n"
        "**Claim:** Unresolved review-thread count ignores GraphQL pagination."
    )
    assert not relay_delivered(paraphrase)
    assert not relay_delivered("I will relay the ```json array now.")  # an unclosed fence is not a payload
    assert not relay_delivered("")
    assert relay_delivered('head abc · 3 findings\n\n```json\n[{"file": "dispatch.py", "line": 493}]\n```')
    # The Gap reply is a complete answer too: the outage is reported, not dropped.
    assert relay_delivered("Gap: structural pass unavailable — clone failed\n\n```json\n[]\n```")


def test_the_relay_gets_the_contract_only_on_a_host_that_has_the_fields():
    from dataclasses import dataclass
    from typing import Any

    from pr_reviewer.subagents import _completion_contract, relay_delivered

    @dataclass
    class NewHost:
        name: str = ""
        completion_check: Any = None
        completion_contract: str = ""

    @dataclass
    class OldHost:  # rejects unknown kwargs — the plugin must not pass them
        name: str = ""

    fields = _completion_contract(NewHost)
    assert fields["completion_check"] is relay_delivered and "verbatim" in fields["completion_contract"]
    NewHost(name="structural-finder", **fields)
    assert _completion_contract(OldHost) == {}
    OldHost(name="structural-finder", **_completion_contract(OldHost))

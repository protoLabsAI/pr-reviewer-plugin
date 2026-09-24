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


# ── one dispatcher and one sweep loop per process across config reloads (issue #198) ──


def test_re_registering_reuses_the_running_dispatcher(tmp_path, no_app_env, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_HOME", str(tmp_path))
    first = FakeRegistry({"default_repo": "octo/repo"})
    pr_reviewer.register(first)
    shared = pr_reviewer._MACHINERY[str(pr_reviewer._state_home(first.config))]
    dispatcher = shared["dispatcher"]
    sem = dispatcher.panel_sem
    assert dispatcher.summon_enabled is True and dispatcher.chokepoint.cooldown_s == 30
    second = FakeRegistry({"default_repo": "octo/repo", "shadow_mode": True, "summon": False, "cooldown_s": 5})
    pr_reviewer.register(second)
    assert shared["dispatcher"] is dispatcher
    # The second registration re-pointed the SAME dispatcher at the new live view — and
    # rebuilt the boot-derived knobs from it (review on #199) — while the panel
    # semaphore the in-flight handlers hold stays the one they hold.
    assert dispatcher.cfg == second.config
    assert dispatcher.summon_enabled is False and dispatcher.chokepoint.cooldown_s == 5
    assert dispatcher.panel_sem is sem
    # A different state home is a different process-of-record: fresh machinery.
    other = FakeRegistry({"default_repo": "octo/repo", "state_root": str(tmp_path / "elsewhere")})
    pr_reviewer.register(other)
    assert pr_reviewer._MACHINERY[str(pr_reviewer._state_home(other.config))]["dispatcher"] is not shared["dispatcher"]


async def test_a_second_sweep_start_returns_the_running_loop(tmp_path, no_app_env, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_HOME", str(tmp_path))
    reg = FakeRegistry({"default_repo": "octo/repo", "sweep_interval_s": 3600})
    pr_reviewer.register(reg)
    sweep = next(s for s in reg.surfaces if s["name"] == "pr-reviewer-sweep")
    first = sweep["start"]()
    try:
        assert sweep["start"]() is first  # no second loop, whoever asks
    finally:
        sweep["stop"]()
        first.cancel()
        try:
            await first
        except BaseException:  # noqa: BLE001 — cancelled or stopped, either is fine here
            pass
    # Once the loop has ENDED, a start is a real restart: a new task on a fresh stop
    # event, not the finished one handed back (review on #199, round 2).
    again = sweep["start"]()
    try:
        assert again is not first and not again.done()
    finally:
        sweep["stop"]()
        again.cancel()
        try:
            await again
        except BaseException:  # noqa: BLE001
            pass

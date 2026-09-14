"""The structural-finder's turn budget must survive the host's step accounting (#119).

The host runs a subagent as a LangGraph `create_agent` with
``recursion_limit = SubagentConfig.max_turns`` (protoAgent ``graph/agent.py``). That
limit counts graph SUPER-STEPS, not model turns: every middleware with a
``before_model`` / ``after_model`` hook adds a graph node that runs on each model turn.
Since protoAgent v0.154.0 (#3199) the subagent stack carries
``CodexReasoningReplayRecoveryMiddleware`` — a ``before_model`` hook — so the relay's
one-tool-call path (before_model → model → tools → before_model → model) needs a
recursion limit of 6. With ``max_turns=4`` EVERY structural-finder run hit
GraphRecursionError right after ``protopatch_review`` returned: the host salvaged no
text (the tool-calling AIMessage is empty) and the lane became
``[structural-finder hard-stopped at max_turns …] -- no salvageable output`` — on every
repo, including protoAgent runs where the engine had just returned 38 findings.

These tests drive the REAL langgraph machinery with the finder's registered budget,
so they fail on the old value and on a zero-headroom one.
"""

from __future__ import annotations

import pr_reviewer
import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.errors import GraphRecursionError

from tests.conftest import FakeRegistry

RELAY = "protoPatch structural pass on o/n#1 — 1s, 0 reportable finding(s).\n\n```json\n[]\n```"


@tool
def protopatch_review(pr: int, repo: str = "") -> str:
    """Stand-in for the real tool: returns an empty findings block immediately."""
    return RELAY


class _BeforeModel(AgentMiddleware):
    """Shape of the host's CodexReasoningReplayRecoveryMiddleware: a before_model node."""

    def before_model(self, state, runtime):
        return None


class _AfterModel(AgentMiddleware):
    """A further node-adding middleware the host could add tomorrow."""

    def after_model(self, state, runtime):
        return None


class _ScriptedModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _script(tool_calls: int) -> list[AIMessage]:
    calls = [
        AIMessage(content="", tool_calls=[{"name": "protopatch_review", "args": {"pr": 1}, "id": f"call-{i}"}])
        for i in range(tool_calls)
    ]
    return [*calls, AIMessage(content=RELAY)]


def _finder_budget() -> int:
    reg = FakeRegistry({})
    pr_reviewer.register(reg)
    return reg.subagents[0].max_turns


async def _run_relay(middleware: list, tool_calls: int) -> str:
    """Run the relay the way the host does (astream + recursion_limit=max_turns)."""
    agent = create_agent(
        model=_ScriptedModel(messages=iter(_script(tool_calls))),
        tools=[protopatch_review],
        middleware=middleware,
    )
    last: dict = {}
    try:
        async for state in agent.astream(
            {"messages": [{"role": "user", "content": "Run the structural pass on PR #1."}]},
            config={"recursion_limit": _finder_budget()},
            stream_mode="values",
        ):
            last = state
    except GraphRecursionError:
        return "HARD-STOP"
    return last["messages"][-1].content


@pytest.mark.parametrize(
    ("shape", "middleware", "tool_calls"),
    [
        # Today's host (protoAgent >= v0.154.0): one before_model node, one tool call.
        ("host today", [_BeforeModel()], 1),
        # Headroom: one more node-adding middleware in the host stack.
        ("one more host node", [_BeforeModel(), _AfterModel()], 1),
        # Headroom: the model calls the tool a second time despite the prompt.
        ("second tool call", [_BeforeModel()], 2),
    ],
)
async def test_relay_completes_within_the_finder_budget(shape, middleware, tool_calls):
    out = await _run_relay(middleware, tool_calls)
    assert out != "HARD-STOP", (
        f"{shape}: the structural-finder hard-stops at max_turns={_finder_budget()} — "
        "the protoPatch result is discarded and the lane becomes a Gap (#119)"
    )
    assert out == RELAY  # the tool's result reaches the panel

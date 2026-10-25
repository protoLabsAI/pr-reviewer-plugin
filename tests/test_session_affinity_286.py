"""One gateway session id per review run (#286).

The protolabs gateway pins a request carrying `x-litellm-session-id` to the replica lane that
served the session's first request. The panel's passes share one big diff as a prompt prefix,
so they must share a session id — and a workflow run started from a webhook has neither a
LangGraph thread nor a tracing session, so it sent none. The host stamps the header from
`observability.tracing.current_session_id()`; these tests pin that this plugin sets it.
"""

from __future__ import annotations

import contextlib
import contextvars
import sys
import types

import pytest
from pr_reviewer.dispatch import _accepts_keyword, _in_review_session

_session: contextvars.ContextVar[str] = contextvars.ContextVar("session", default="")


@pytest.fixture
def tracing(monkeypatch):
    """A stand-in for the host's `observability.tracing` with the real contract: `trace_session`
    sets the session contextvar (also when Langfuse is off) and `current_session_id` reads it."""

    @contextlib.asynccontextmanager
    async def trace_session(session_id, name="agent-session", metadata=None, input=None, incognito=False):
        token = _session.set(session_id)
        try:
            yield None
        finally:
            _session.reset(token)

    mod = types.SimpleNamespace(trace_session=trace_session, current_session_id=lambda: _session.get())
    pkg = types.ModuleType("observability")
    pkg.tracing = mod
    monkeypatch.setitem(sys.modules, "observability", pkg)
    monkeypatch.setitem(sys.modules, "observability.tracing", mod)
    return mod


async def _runner(recipe, inputs, on_step=None, *, seed_outputs=None):
    return {"session": _session.get()}


async def test_every_call_of_one_head_shares_one_session(tracing):
    run = _in_review_session(_runner, "o/r", 7, "abc123")
    first, rerun, fresh = await run("r", {}), await run("r", {}, seed_outputs={"a": "b"}), await run("r", {})
    assert first["session"] == rerun["session"] == fresh["session"] == "pr-review:o/r#7@abc123"


async def test_a_new_head_or_pr_is_a_new_session(tracing):
    a = await _in_review_session(_runner, "o/r", 7, "abc123")("r", {})
    b = await _in_review_session(_runner, "o/r", 7, "def456")("r", {})
    c = await _in_review_session(_runner, "o/r", 8, "abc123")("r", {})
    assert len({a["session"], b["session"], c["session"]}) == 3


async def test_the_session_does_not_leak_past_the_call(tracing):
    await _in_review_session(_runner, "o/r", 7, "abc123")("r", {})
    assert tracing.current_session_id() == ""


async def test_a_session_inherited_from_the_caller_wins(tracing):
    token = _session.set("chat-turn-1")
    try:
        out = await _in_review_session(_runner, "o/r", 7, "abc123")("r", {})
    finally:
        _session.reset(token)
    assert out["session"] == "chat-turn-1"


async def test_the_signature_stays_visible_to_capability_probes(tracing):
    run = _in_review_session(_runner, "o/r", 7, "abc123")
    assert _accepts_keyword(run, "seed_outputs") and _accepts_keyword(run, "on_step")

    async def old_runner(recipe, inputs):
        return {}

    assert not _accepts_keyword(_in_review_session(old_runner, "o/r", 7, "abc123"), "seed_outputs")


async def test_a_host_without_observability_runs_unscoped(monkeypatch):
    monkeypatch.setitem(sys.modules, "observability", None)  # import raises ImportError
    out = await _in_review_session(_runner, "o/r", 7, "abc123")("r", {})
    assert out == {"session": ""}


async def test_an_exception_from_the_runner_propagates(tracing):
    async def boom(recipe, inputs):
        raise RuntimeError("panel failed")

    with pytest.raises(RuntimeError, match="panel failed"):
        await _in_review_session(boom, "o/r", 7, "abc123")("r", {})
    assert tracing.current_session_id() == ""


async def test_the_dispatcher_runs_the_panel_inside_the_review_session(tmp_path, tracing):
    from tests.test_dispatch import HEAD, REPORT, RoutedGH, facts, make

    seen: list[str] = []

    async def runner(name, inputs):
        seen.append(_session.get())
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=RoutedGH(pr_facts=facts()), runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert seen and set(seen) == {f"pr-review:o/r#1@{HEAD}"}

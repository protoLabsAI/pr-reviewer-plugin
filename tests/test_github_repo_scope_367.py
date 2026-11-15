"""Every github-plugin tool call a review round makes defaults to the ROUND's repo (#367).

Vera reviews 170 repos with `github.default_repo: ""`, so a `github_*` call that omitted `repo`
resolved to `github.repos[0]` (protoAgent): on mythxengine#966 a finder lane read protoAgent's
CHANGELOG and pyproject and reviewed the wrong codebase. github-plugin >= 0.12.0 exposes
`gh_issue.scoped_default_repo(repo)`; `_in_review_session` now wraps every pass of a round in it.
The stand-in below has the real contract: a ContextVar consulted after an explicit repo.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import sys
import types

import pytest
from pr_reviewer.dispatch import _GITHUB_PLUGIN_GH_ISSUE, _github_repo_scope, _in_review_session

_scoped: contextvars.ContextVar[str] = contextvars.ContextVar("scoped", default="")


@pytest.fixture
def github_plugin(monkeypatch):
    @contextlib.contextmanager
    def scoped_default_repo(repo):
        token = _scoped.set(repo)
        try:
            yield
        finally:
            _scoped.reset(token)

    def resolve_repo(explicit, default_repo=""):
        return (explicit or "").strip() or _scoped.get() or default_repo

    mod = types.SimpleNamespace(scoped_default_repo=scoped_default_repo, resolve_repo=resolve_repo)
    monkeypatch.setitem(sys.modules, _GITHUB_PLUGIN_GH_ISSUE, mod)
    return mod


async def test_a_github_call_inside_a_round_that_omits_repo_targets_the_round(github_plugin):
    async def runner(recipe, inputs):
        async def finder():  # a subagent step: its own task, started inside the round
            await asyncio.sleep(0)
            return github_plugin.resolve_repo(None, "protoLabsAI/protoAgent")

        return await asyncio.gather(finder(), finder())

    run = _in_review_session(runner, "protoLabsAI/mythxengine", 966, "df2b2266")
    assert await run("code-review-structural", {}) == ["protoLabsAI/mythxengine"] * 2
    # outside the round the agent-wide default applies again
    assert github_plugin.resolve_repo(None, "protoLabsAI/protoAgent") == "protoLabsAI/protoAgent"


async def test_an_explicit_repo_still_wins_so_the_lane_guard_still_sees_it(github_plugin):
    async def runner(recipe, inputs):
        return github_plugin.resolve_repo("protoLabsAI/protoAgent", "x/y")

    run = _in_review_session(runner, "protoLabsAI/mythxengine", 966, "df2b2266")
    assert await run("r", {}) == "protoLabsAI/protoAgent"


async def test_without_the_github_plugin_or_its_scope_the_round_runs_unscoped(monkeypatch):
    monkeypatch.delitem(sys.modules, _GITHUB_PLUGIN_GH_ISSUE, raising=False)

    async def runner(recipe, inputs):
        return "ran"

    assert await _in_review_session(runner, "o/r", 1, "h")("r", {}) == "ran"
    monkeypatch.setitem(sys.modules, _GITHUB_PLUGIN_GH_ISSUE, types.SimpleNamespace())  # an older plugin
    assert await _in_review_session(runner, "o/r", 1, "h")("r", {}) == "ran"


def test_a_scope_that_raises_never_stops_a_review(monkeypatch):
    def broken(repo):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, _GITHUB_PLUGIN_GH_ISSUE, types.SimpleNamespace(scoped_default_repo=broken))
    with _github_repo_scope("o/r"):
        pass

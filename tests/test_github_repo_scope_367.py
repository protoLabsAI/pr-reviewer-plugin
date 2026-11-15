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


# ── the lane guard judges an omitted repo against the round when the scope is active ──────────


async def test_under_the_scope_an_omitted_repo_is_a_clean_pass_and_a_named_other_repo_is_not(tmp_path, github_plugin):
    from tests.dispatch_helpers import make
    from tests.test_dispatch import _events
    from tests.test_lane_repo_binding_279 import BOUND, HEAD, OTHER, PR, _clean_runner_with, _gh

    cfg = {"repos": [BOUND], "default_repo": OTHER, "shadow_mode": False}
    omitted = {"find_removed_behavior": [{"tool": "github_read_pr_file", "args": {"number": PR, "path": "a.ts"}}]}
    d = make(tmp_path / "a", cfg=cfg, gh=_gh(), runner=_clean_runner_with(omitted))
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:PASS"
    assert not _events(tmp_path / "a", "lane_repo_unbound")

    named = {"find_removed_behavior": [{"tool": "search_pr_repo", "repo": OTHER, "pattern": "x"}]}
    d = make(tmp_path / "b", cfg=cfg, gh=_gh(), runner=_clean_runner_with(named))
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:WARN"
    assert _events(tmp_path / "b", "lane_repo_unbound")


async def test_without_the_scope_an_omitted_repo_is_still_flagged(tmp_path, monkeypatch):
    from tests.dispatch_helpers import make
    from tests.test_lane_repo_binding_279 import BOUND, HEAD, OTHER, PR, _clean_runner_with, _gh

    monkeypatch.delitem(sys.modules, _GITHUB_PLUGIN_GH_ISSUE, raising=False)
    cfg = {"repos": [BOUND], "default_repo": OTHER, "shadow_mode": False}
    omitted = {"find_removed_behavior": [{"tool": "github_read_pr_file", "args": {"number": PR}}]}
    d = make(tmp_path, cfg=cfg, gh=_gh(), runner=_clean_runner_with(omitted))
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:WARN"


async def test_a_scope_that_fails_to_enter_leaves_the_guard_strict(tmp_path, monkeypatch):
    # The plugin offers the seam but it raises: the round runs unscoped, so an omitted repo
    # really did resolve to the agent default and must still be flagged (#370 review).
    from tests.dispatch_helpers import make
    from tests.test_lane_repo_binding_279 import BOUND, HEAD, OTHER, PR, _clean_runner_with, _gh

    def broken(repo):
        raise RuntimeError("boom")

    monkeypatch.setitem(sys.modules, _GITHUB_PLUGIN_GH_ISSUE, types.SimpleNamespace(scoped_default_repo=broken))
    cfg = {"repos": [BOUND], "default_repo": OTHER, "shadow_mode": False}
    omitted = {"find_removed_behavior": [{"tool": "github_read_pr_file", "args": {"number": PR}}]}
    d = make(tmp_path, cfg=cfg, gh=_gh(), runner=_clean_runner_with(omitted))
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:WARN"


def test_only_an_entered_scope_is_recorded_for_its_round(github_plugin):
    from pr_reviewer.dispatch import _github_round_scoped, _review_key

    with _github_repo_scope("o/r", _review_key("o/r", 1, "h1")):
        pass
    assert _github_round_scoped("o/r", 1, "h1")
    assert not _github_round_scoped("o/r", 1, "other-head")


def test_a_new_round_does_not_inherit_an_earlier_rounds_scope_mark(github_plugin, monkeypatch):
    from pr_reviewer.dispatch import _github_round_scoped

    async def runner(recipe, inputs):
        return "ran"

    import asyncio as _asyncio

    _asyncio.run(_in_review_session(runner, "o/r", 1, "h")("r", {}))
    assert _github_round_scoped("o/r", 1, "h")
    # the next round of the same head, on a host whose seam now raises: no inherited mark
    monkeypatch.setitem(
        sys.modules, _GITHUB_PLUGIN_GH_ISSUE, types.SimpleNamespace(scoped_default_repo=lambda r: 1 / 0)
    )
    again = _in_review_session(runner, "o/r", 1, "h")
    assert not _github_round_scoped("o/r", 1, "h")
    _asyncio.run(again("r", {}))
    assert not _github_round_scoped("o/r", 1, "h")

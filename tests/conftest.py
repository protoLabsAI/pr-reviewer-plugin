"""Host-free test bootstrap: load the plugin as a synthetic `pr_reviewer` package (the
repo dir has a dash, and relative imports need a package), plus a stub host module for
`graph.subagents.config.SubagentConfig` so subagent registration runs with no host."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PKG = "pr_reviewer"

if PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(PKG, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    assert _spec and _spec.loader
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules[PKG] = _mod
    _spec.loader.exec_module(_mod)


class StubSubagentConfig:
    """Stand-in for graph.subagents.config.SubagentConfig — stores every kwarg as an
    attribute so registered configs stay assertable."""

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


if "graph" not in sys.modules:
    graph = types.ModuleType("graph")
    subagents = types.ModuleType("graph.subagents")
    config = types.ModuleType("graph.subagents.config")
    config.SubagentConfig = StubSubagentConfig
    graph.subagents = subagents
    subagents.config = config
    sys.modules["graph"] = graph
    sys.modules["graph.subagents"] = subagents
    sys.modules["graph.subagents.config"] = config


class FakeRegistry:
    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self.tools: list = []
        self.subagents: list = []
        self.routers: list = []
        self.surfaces: list = []

    def register_tool(self, tool) -> None:
        self.tools.append(tool)

    def register_subagent(self, config) -> None:
        self.subagents.append(config)

    def register_router(self, router, prefix=None) -> None:
        self.routers.append((router, prefix))

    def register_surface(self, start, stop=None, name=None, reload=None) -> None:
        self.surfaces.append({"start": start, "stop": stop, "name": name})

    def live_config(self):
        return self.config


# ── the gh fakes fail an unrecognised WRITE (#136) ─────────────────────────────
#
# Both fakes answered `0, ""` to anything they did not recognise, and most tests assert
# on one write (`reviews_posted[0]`) — so a dispatcher change that started making an
# EXTRA GitHub write passed unnoticed. The fakes now record any write outside the routes
# the dispatcher is known to make, and this fixture fails the test that caused it.
# Recorded-then-asserted rather than raised: the dispatcher's publish paths "degrade,
# never raise", so an exception thrown from inside the fake would be swallowed by the very
# code under test. Reads stay permissive — an unmatched read changes nothing on GitHub.

import re  # noqa: E402

KNOWN_WRITES = (
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews$")),
    ("PUT", re.compile(r"^repos/[^/]+/[^/]+/pulls/\d+/reviews/\d+/dismissals$")),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/issues/\d+/comments$")),
    ("POST", re.compile(r"^repos/[^/]+/[^/]+/check-runs$")),
    ("PATCH", re.compile(r"^repos/[^/]+/[^/]+/check-runs/\d+$")),
)
UNEXPECTED_WRITES: list[list[str]] = []


def note_write(args: list[str]) -> bool:
    """True when `args` is a write the fakes do not recognise — recorded for the fixture."""
    if "-X" not in args:
        return False
    method = args[args.index("-X") + 1] if args.index("-X") + 1 < len(args) else ""
    url = args[1] if len(args) > 1 else ""
    if method == "GET" or any(method == m and rx.match(url) for m, rx in KNOWN_WRITES):
        return False
    UNEXPECTED_WRITES.append(list(args))
    return True


@pytest.fixture(autouse=True)
def _no_unexpected_github_writes():
    UNEXPECTED_WRITES.clear()
    yield
    seen, UNEXPECTED_WRITES[:] = list(UNEXPECTED_WRITES), []
    assert not seen, f"the code under test made a GitHub write no fake recognises: {seen}"


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item):
    """A test module that reads its data at import (the README, the recipe) keeps a load
    failure in `_LOAD_ERROR` instead of raising during collection (#251). Every test in it
    then FAILS with that error — a visible test failure, not a collection error, and never a
    pass: a negative assertion over empty text would otherwise go green."""
    error = getattr(getattr(item, "module", None), "_LOAD_ERROR", None)
    if error is not None:
        pytest.fail(f"{item.module.__name__}: test data could not be loaded: {error!r}", pytrace=False)


@pytest.fixture(autouse=True)
def _no_checkout_clones(monkeypatch):
    """The dispatcher's absence search (#259) greps a checkout of the PR head, cloning one on a
    cache miss. A test must never clone from GitHub: the default resolver finds no checkout, and
    a test that wants the search to run injects `resolve_checkout` with a local repo."""
    import pr_reviewer.absence_search as absence

    async def _no_checkout(repo, head):
        return None

    monkeypatch.setattr(absence, "default_resolver", lambda cfg: _no_checkout)

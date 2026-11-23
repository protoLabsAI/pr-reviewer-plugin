"""Base-only path-scoped policy wiring; optional policy never voids a review."""

import base64
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from pr_reviewer import review_rules
from pr_reviewer.epic import EpicScope
from pr_reviewer.incremental import Plan

from tests.dispatch_helpers import HEAD, OLD_HEAD, RoutedGH, facts, make

BASE = "release/policy"
RULES = '- paths: ["migrations/**"]\n  rule: Migrations are append-only.\n'
PATHS = "migrations/0002_x.py\napp/views.py\n"


class RulesGH(RoutedGH):
    def __init__(self, blobs=None, failure=None):
        super().__init__(pr_facts=facts(base_ref=BASE), files=PATHS)
        self.blobs = blobs or {}
        self.failure = failure
        self.rules_reads = []

    async def __call__(self, args, timeout=30):
        if "/contents/.protoreview/rules.yaml?" in args[1]:
            assert "\x00" not in args[args.index("--jq") + 1]
            assert r"\u0000" in args[args.index("--jq") + 1]
            self.rules_reads.append(args[1])
            if isinstance(self.failure, Exception):
                raise self.failure
            if self.failure:
                return self.failure
            ref = parse_qs(urlsplit(args[1]).query)["ref"][0]
            if ref not in self.blobs:
                return 1, "", "gh: Not Found (HTTP 404)"
            return 0, "base64\x00" + base64.b64encode(self.blobs[ref].encode()).decode(), ""
        return await super().__call__(args, timeout)


async def dispatch(tmp_path, gh, scope=None, monkeypatch=None):
    seen = {}

    async def runner(name, inputs):
        seen.update(inputs)
        return {"output": "No findings.\n\n```json\n[]\n```", "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    if scope == "incremental":
        monkeypatch.setattr(
            d,
            "_incremental_plan",
            AsyncMock(return_value=Plan("scoped", prior_head=OLD_HEAD, in_scope={"app/views.py": "changed"})),
        )
    elif scope == "epic":
        monkeypatch.setattr(
            d,
            "_epic_scope",
            AsyncMock(return_value=EpicScope("epic/test", BASE, residual_paths=["app/views.py"])),
        )
    result = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert result.startswith("reviewed:"), result
    assert seen
    events = [e for e in d.telemetry.read_all() if e["event"] == "review_rules_invalid"]
    return seen, events


async def test_rules_are_grouped_only_with_matching_changed_files(tmp_path):
    gh = RulesGH({BASE: RULES, HEAD: "- paths: ['**']\n  rule: Head policy must not win."})
    inputs, events = await dispatch(tmp_path, gh)
    block = inputs["review_rules"]
    assert "Migrations are append-only." in block
    assert "migrations/0002_x.py" in block
    assert "app/views.py" not in block
    assert "Head policy" not in block
    assert gh.rules_reads == ["repos/o/r/contents/.protoreview/rules.yaml?ref=release%2Fpolicy"]
    assert events == []


@pytest.mark.parametrize("blobs", [{}, {HEAD: RULES}])
async def test_missing_at_base_is_silent_even_when_present_at_head(tmp_path, blobs):
    gh = RulesGH(blobs)
    inputs, events = await dispatch(tmp_path, gh)
    assert "review_rules" not in inputs
    assert events == []
    assert gh.rules_reads == ["repos/o/r/contents/.protoreview/rules.yaml?ref=release%2Fpolicy"]


@pytest.mark.parametrize(
    ("text", "failure", "reason"),
    [
        ("[", None, "invalid YAML"),
        ("paths: [x]", None, "top level must be a list"),
        (RULES, RuntimeError("read exploded"), "read exploded"),
        (RULES, (1, "", "HTTP 503 unavailable"), "503"),
        (RULES, (0, "not a blob", ""), "no base64 content"),
    ],
)
async def test_invalid_or_failed_read_degrades(tmp_path, text, failure, reason):
    inputs, events = await dispatch(tmp_path, RulesGH({BASE: text}, failure))
    assert "review_rules" not in inputs
    assert len(events) == 1
    assert events[0]["repo"] == "o/r" and events[0]["pr"] == 1
    assert reason in events[0]["reason"]
    assert len(events[0]["reason"]) <= 200


@pytest.mark.parametrize("text", ["", "[]", "- paths: ['docs/**']\n  rule: Document changes."])
async def test_empty_or_unmatched_rules_add_no_input(tmp_path, text):
    inputs, events = await dispatch(tmp_path, RulesGH({BASE: text}))
    assert "review_rules" not in inputs
    assert events == []


@pytest.mark.parametrize("scope", ["incremental", "epic"])
async def test_rules_only_see_round_scope(tmp_path, monkeypatch, scope):
    rules = RULES + "- paths: ['app/**']\n  rule: Keep views small."
    inputs, events = await dispatch(tmp_path, RulesGH({BASE: rules}), scope, monkeypatch)
    assert "Keep views small." in inputs["review_rules"]
    assert "app/views.py" in inputs["review_rules"]
    assert "migrations/" not in inputs["review_rules"]
    assert events == []


async def test_unexpected_render_failure_degrades(tmp_path, monkeypatch):
    def broken(_groups):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(review_rules, "render_rules_block", broken)
    inputs, events = await dispatch(tmp_path, RulesGH({BASE: RULES}))
    assert "review_rules" not in inputs
    assert len(events) == 1 and "render exploded" in events[0]["reason"]

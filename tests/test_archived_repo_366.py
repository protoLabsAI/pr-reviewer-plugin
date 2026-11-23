"""A PR in an archived repo is never reviewed (issue #366).

An archived repo is read-only: GitHub answers every write with `422 Repository was
archived so is read-only`, so the panel ran in full, posted nothing, and the PR was
reported UNREVIEWED. `ineligible_reason` now names `archived` before any panel runs.
"""

from __future__ import annotations

import json
import logging

import pytest
from pr_reviewer.dispatch import ineligible_reason

from tests.test_dispatch import HEAD, REPORT, RoutedGH, facts, make


def _rows(root, event):
    return [
        json.loads(line)
        for f in (root / "telemetry").glob("*.jsonl")
        for line in f.read_text().splitlines()
        if line.strip() and json.loads(line).get("event") == event
    ]


# ── the pure function ─────────────────────────────────────────────────────────


def test_archived_is_ineligible():
    assert ineligible_reason(facts(archived=True)) == "archived"


@pytest.mark.parametrize("over", [{}, {"archived": False}, {"archived": None}])
def test_missing_or_false_archived_is_unaffected(over):
    assert ineligible_reason(facts(**over)) is None


def test_archived_is_checked_after_not_open_and_before_draft():
    assert ineligible_reason(facts(state="closed", archived=True)) == "not-open"
    assert ineligible_reason(facts(archived=True, draft=True)) == "archived"
    assert ineligible_reason(facts(archived=True, locked=True)) == "archived"


# ── the facts read ────────────────────────────────────────────────────────────


async def test_pr_facts_projects_the_base_repo_archived_flag(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    await make(tmp_path, gh=gh)._pr_facts("o/r", 1)
    jq = next(a[a.index("--jq") + 1] for a in gh.calls if "repos/o/r/pulls/1" in a and "--jq" in a)
    assert "archived: .base.repo.archived" in jq


# ── the dispatch drop ─────────────────────────────────────────────────────────


async def test_webhook_and_sweep_drop_an_archived_pr_without_a_panel(tmp_path, caplog):
    gh = RoutedGH(pr_facts=facts(archived=True), reviews=[])
    ran, inbox = [], []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner, inbox=lambda text, **kw: inbox.append(text))

    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:pr-not-eligible"
    with caplog.at_level(logging.INFO, logger="protoagent.plugins.pr_reviewer"):
        assert (await d.needs_backfill("o/r", 1)) is None

    assert ran == []  # no panel spent
    assert gh.posted == [] and gh.check_writes == []  # nothing written into the read-only repo
    assert inbox == [] and _rows(tmp_path, "escalation") == []  # no UNREVIEWED escalation
    drops = [r for r in _rows(tmp_path, "drop") if r.get("why") == "archived"]
    assert len(drops) == 2  # webhook AND backfill both say why
    assert all(r.get("reason") == "pr-not-eligible" for r in drops)
    assert any("archived" in rec.getMessage() for rec in caplog.records)

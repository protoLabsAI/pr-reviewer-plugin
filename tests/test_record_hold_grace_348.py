"""Held reviewer-of-record heads expire through reconciliation (#348)."""

import time
from datetime import datetime, timezone

import pytest

from tests.dispatch_helpers import HEAD, facts, make
from tests.test_dispatch import _events
from tests.test_reviewer_of_record_318 import _GH, CFG, _runner


class HoldGH(_GH):
    async def __call__(self, args, timeout=30):
        if ".commit.committer.date" in args and self.head_age_s is None:
            self.calls.append(args)
            return 1, "", "unreadable commit"
        return await super().__call__(args, timeout)


def setup(tmp_path, **kw):
    calls = []
    gh = HoldGH(pr_facts=facts(labels=["reviewer-of-record:board"]), **kw)
    d = make(tmp_path, cfg={**CFG, "promotion_owner": True}, gh=gh, runner=_runner(calls))
    return d, gh, calls


@pytest.mark.parametrize("unreadable", [False, True])
async def test_first_reconcile_after_absent_grace_reviews(tmp_path, monkeypatch, unreadable):
    now = [time.time()]
    monkeypatch.setattr("pr_reviewer.dispatch.time.time", lambda: now[0])
    d, gh, calls = setup(tmp_path, head_age_s=None if unreadable else 1199)
    await d.reconcile_pr("o/r", 1, backfill_budget=1)
    assert not calls
    now[0] += 1200 if unreadable else 2
    if not unreadable:
        gh.head_age_s = 1201
    outcome, budget = await d.reconcile_pr("o/r", 1, backfill_budget=1)
    assert outcome.startswith("reviewed:")
    assert budget == 0 and calls
    event = _events(tmp_path, "reviewer_of_record")[-1]
    assert event["decision"] == "review-fallback"
    assert event["head_age_s"] is None if unreadable else event["head_age_s"] >= 1200
    assert event["status_age_s"] is None


async def test_pending_expires_on_next_reconcile(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("pr_reviewer.dispatch.time.time", lambda: now[0])
    stamp = datetime.fromtimestamp(now[0] - 3599, timezone.utc).isoformat()
    d, gh, calls = setup(tmp_path, status={"state": "pending", "updated_at": stamp})
    await d.reconcile_pr("o/r", 1, backfill_budget=1)
    assert not calls
    now[0] += 2
    outcome, _ = await d.reconcile_pr("o/r", 1, backfill_budget=1)
    assert outcome.startswith("reviewed:") and calls
    assert _events(tmp_path, "reviewer_of_record")[-1]["status_age_s"] >= 3600


@pytest.mark.parametrize("state", [None, "pending", "failure"])
async def test_hold_check_survives_promotion_refresh(tmp_path, state):
    stamp = datetime.now(timezone.utc).isoformat()
    d, gh, calls = setup(tmp_path, status={"state": state, "updated_at": stamp})
    await d.reconcile_pr("o/r", 1, backfill_budget=1)
    writes = [p for p in gh.posted if p.get("name") == "QA panel"]
    assert writes
    check = writes[-1]
    assert check["status"] == "completed" and check["conclusion"] == "neutral"
    summary = check["output[summary]"]
    if state == "failure":
        assert summary == "project board reported failure; its fix round owns this PR"
    else:
        assert summary.startswith("Waiting for project board — falls back to Vera’s panel at ")
        assert summary.endswith(" UTC")
    assert not calls and not gh.reviews_posted
    event = _events(tmp_path, "reviewer_of_record")[-1]
    assert "head_age_s" in event and "status_age_s" in event


async def test_sweep_reviews_old_head_on_first_pass(tmp_path):
    d, gh, calls = setup(tmp_path, head_age_s=1201)
    assert await d.sweep_once() == 1
    await d.drain_backfills()
    assert calls
    assert _events(tmp_path, "reviewer_of_record")[-1]["decision"] == "review-fallback"


@pytest.mark.parametrize("initial_status", ["queued", "in_progress"])
async def test_webhook_hold_completes_existing_check(tmp_path, initial_status):
    d, gh, calls = setup(tmp_path, checks={"id": 88, "status": initial_status})
    assert await d.handle_pr_event("o/r", 1, HEAD, "opened") == "hold:reviewer-of-record-absent"
    patches = [c for c in gh.calls if c[1] == "repos/o/r/check-runs/88" and "PATCH" in c]
    assert patches and "status=completed" in patches[-1] and "conclusion=neutral" in patches[-1]
    assert not calls


async def test_failed_status_read_remains_held_after_grace(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("pr_reviewer.dispatch.time.time", lambda: now[0])
    d, gh, calls = setup(tmp_path, status_rc=1)
    await d.reconcile_pr("o/r", 1, backfill_budget=1)
    now[0] += 7200
    await d.reconcile_pr("o/r", 1, backfill_budget=1)
    assert not calls
    event = _events(tmp_path, "reviewer_of_record")[-1]
    assert event["decision"] == "hold-pending"
    assert event["head_age_s"] is None and event["status_age_s"] is None


async def test_recovered_commit_date_takes_precedence_and_new_head_gets_own_grace(tmp_path, monkeypatch):
    now = [time.time()]
    monkeypatch.setattr("pr_reviewer.dispatch.time.time", lambda: now[0])
    d, gh, calls = setup(tmp_path, head_age_s=None)
    await d.needs_backfill("o/r", 1)
    now[0] += 1201
    gh.head_age_s = 60
    assert await d.needs_backfill("o/r", 1) is None
    gh.head_age_s = None
    assert await d.needs_backfill("o/r", 1) == HEAD
    gh.pr_facts["head"] = "b" * 40
    assert await d.needs_backfill("o/r", 1) is None
    assert not calls


async def test_first_hold_map_is_bounded(tmp_path):
    d, gh, calls = setup(tmp_path, head_age_s=None)
    for number in range(1025):
        await d._record_outcome("o/r", number, gh.pr_facts, HEAD, publish_hold=False)
    assert len(d._record_first_hold) == 512
    assert f"o/r#0@{HEAD}" not in d._record_first_hold
    assert f"o/r#1024@{HEAD}" in d._record_first_hold

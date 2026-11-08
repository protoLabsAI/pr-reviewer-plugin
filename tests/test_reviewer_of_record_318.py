"""#318: a PR another reviewer owns (the board's gate) is mirrored, held for, or — once that
reviewer has plainly not reviewed the head — reviewed by Vera herself."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from pr_reviewer.record import (
    HOLD_ABSENT,
    HOLD_FAILED,
    HOLD_PENDING,
    PASS_,
    REVIEW_FALLBACK,
    decide,
    match,
    parse_rules,
)

from tests.dispatch_helpers import HEAD, RoutedGH, facts, make
from tests.test_dispatch import _events

RULE = {"label": "reviewer-of-record:board", "status": "board/review-gate", "name": "project board", "grace_s": 1200}
CLEAN = "<!-- brief -->\nClean.\n<!-- /brief -->\n\n```json\n[]\n```"


# ── pure ────────────────────────────────────────────────────────────────────────────────


def test_parse_rules_accepts_list_json_and_one_dict_and_skips_junk():
    assert [r.label for r in parse_rules([RULE])] == ["reviewer-of-record:board"]
    assert [r.status for r in parse_rules(json.dumps([RULE]))] == ["board/review-gate"]
    assert len(parse_rules(RULE)) == 1
    assert parse_rules("") == [] and parse_rules("not json") == [] and parse_rules(None) == []
    assert (
        parse_rules([{"label": "x"}, {"status": "y"}, 3, {"label": "a", "status": "b", "grace_s": "zz"}])[0].grace_s
        == 1200
    )


def test_match_is_by_label_case_insensitively():
    rules = parse_rules([RULE])
    assert match(rules, ["Reviewer-Of-Record:Board", "x"]).name == "project board"
    assert match(rules, ["something-else"]) is None and match(rules, None) is None


@pytest.mark.parametrize(
    ("state", "age", "want"),
    [
        ("success", None, PASS_),
        ("pending", 99999, HOLD_PENDING),
        ("failure", 99999, HOLD_FAILED),
        ("error", 5, HOLD_FAILED),
        (None, 60, HOLD_ABSENT),
        (None, None, HOLD_ABSENT),  # unreadable age: hold, never spend a panel the record owns
        (None, 1201, REVIEW_FALLBACK),
    ],
)
def test_decide(state, age, want):
    assert decide(state, age, 1200) == want


def test_a_pending_record_that_died_mid_review_falls_back():
    assert decide("pending", None, 1200, status_age_s=3599, pending_grace_s=3600) == HOLD_PENDING
    assert decide("pending", None, 1200, status_age_s=3600, pending_grace_s=3600) == REVIEW_FALLBACK
    assert decide("pending", None, 1200, status_age_s=None) == HOLD_PENDING  # unreadable: hold
    assert parse_rules([{**RULE, "pending_grace_s": 99}])[0].pending_grace_s == 99


# ── through the dispatcher ──────────────────────────────────────────────────────────────


class _GH(RoutedGH):
    """RoutedGH plus the two reads the record path makes."""

    def __init__(self, *, status=None, head_age_s=60.0, status_rc=0, **kw):
        super().__init__(**kw)
        self.status, self.head_age_s, self.status_rc = status, head_age_s, status_rc

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/statuses" in joined and "-X" not in args:
            self.calls.append(args)
            if self.status_rc:
                return self.status_rc, "", "boom"
            return 0, json.dumps(self.status or {"state": None, "description": None}), ""
        if f"/commits/{HEAD}" in joined and ".commit.committer.date" in joined:
            self.calls.append(args)
            stamp = datetime.fromtimestamp(time.time() - self.head_age_s, tz=timezone.utc)
            return 0, stamp.isoformat().replace("+00:00", "Z"), ""
        return await super().__call__(args, timeout)


def _labelled(**kw):
    return _GH(pr_facts=facts(labels=["reviewer-of-record:board"]), **kw)


def _runner(calls):
    async def runner(name, inputs):
        calls.append(name)
        return {"output": CLEAN, "failed": [], "steps": {}}

    return runner


CFG = {"shadow_mode": False, "reviewers_of_record": [RULE]}


async def test_record_success_posts_an_attested_pass_and_runs_no_panel(tmp_path):
    calls: list = []
    gh = _labelled(status={"state": "success", "description": "Review gate: clean"})
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(calls))
    assert await d.handle_pr_event("o/r", 1, HEAD, "opened") == "attested:PASS"
    assert calls == []
    body = gh.reviews_posted[0]["body"]
    assert "verdict=PASS" in body and "Reviewer of record: project board" in body
    assert "board/review-gate" in body
    assert _events(tmp_path, "reviewer_of_record")[-1]["decision"] == PASS_


@pytest.mark.parametrize(("state", "want"), [("pending", "pending"), ("failure", "failed"), ("error", "failed")])
async def test_record_pending_or_failed_holds_and_posts_nothing(tmp_path, state, want):
    calls: list = []
    gh = _labelled(status={"state": state, "description": "x"})
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(calls))
    assert await d.handle_pr_event("o/r", 1, HEAD, "opened") == f"hold:reviewer-of-record-{want}"
    assert calls == [] and gh.reviews_posted == []
    # Above all, no FAIL: the board would read it back as the external panel failing its head.
    assert not [p for p in gh.posted if "verdict=FAIL" in str(p)]


async def test_no_record_status_inside_the_grace_holds(tmp_path):
    calls: list = []
    gh = _labelled(status=None, head_age_s=60)
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(calls))
    assert await d.handle_pr_event("o/r", 1, HEAD, "opened") == "hold:reviewer-of-record-absent"
    assert calls == []


async def test_no_record_status_past_the_grace_reviews_herself(tmp_path):
    calls: list = []
    gh = _labelled(status=None, head_age_s=5000)
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(calls))
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out.startswith("reviewed:"), out
    assert calls  # the panel ran
    assert _events(tmp_path, "reviewer_of_record")[-1]["decision"] == REVIEW_FALLBACK


async def test_a_stale_pending_record_status_reviews_herself(tmp_path):
    calls: list = []
    old = datetime.fromtimestamp(time.time() - 7200, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    gh = _labelled(status={"state": "pending", "description": "Review gate running…", "updated_at": old})
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(calls))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")
    assert calls


async def test_an_unreadable_record_status_holds(tmp_path):
    calls: list = []
    gh = _labelled(status_rc=1)
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(calls))
    assert await d.handle_pr_event("o/r", 1, HEAD, "opened") == "hold:reviewer-of-record-pending"
    assert calls == []


async def test_a_summon_always_runs_vera_s_own_panel(tmp_path):
    calls: list = []
    gh = _labelled(status={"state": "success"})
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(calls))
    out = await d._review("o/r", 1, force=True)
    assert out.startswith("reviewed:"), out
    assert calls


async def test_an_unlabelled_pr_is_reviewed_and_makes_no_record_reads(tmp_path):
    calls: list = []
    gh = _GH(pr_facts=facts(labels=["bug"]), status={"state": "success"})
    d = make(tmp_path, cfg=CFG, gh=gh, runner=_runner(calls))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")
    assert calls and not [c for c in gh.calls if "/statuses" in " ".join(c)]


async def test_no_rules_configured_changes_nothing(tmp_path):
    calls: list = []
    gh = _labelled(status={"state": "success"})
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=_runner(calls))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")
    assert calls


async def test_backfill_skips_a_held_head_and_takes_one_the_record_passed(tmp_path):
    held = _labelled(status={"state": "pending"})
    d = make(tmp_path, cfg=CFG, gh=held)
    assert await d.needs_backfill("o/r", 1) is None
    passed = _labelled(status={"state": "success"})
    d2 = make(tmp_path / "b", cfg=CFG, gh=passed)
    assert await d2.needs_backfill("o/r", 1) == HEAD

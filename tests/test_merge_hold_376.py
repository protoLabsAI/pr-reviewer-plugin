"""The operator's merge hold gates merging, never the review verdict."""

import logging

import pytest
from pr_reviewer.dispatch import HOLD_MERGE_HOLD, merge_held

from tests.test_auto_merge_opt_in import GREEN, OWNER, _promote
from tests.test_dispatch import HEAD, RoutedGH, facts, make, promotion_row, review_row


@pytest.mark.parametrize(
    "value, expected",
    [
        ({}, False),
        ({"labels": None}, False),
        ({"labels": []}, False),
        ({"labels": ["bug", " Merge-HOLD "]}, True),
        ({"labels": ["merge-hold-extra"]}, False),
    ],
)
def test_merge_held(value, expected):
    assert merge_held(value) is expected


@pytest.mark.parametrize("cfg, labels", [({"auto_merge_repos": ["o/r"]}, []), ({}, ["auto-merge"])])
async def test_held_promotion_still_approves_without_arming(tmp_path, cfg, labels):
    arms, event = await _promote(tmp_path, cfg=cfg, labels=[*labels, " MERGE-HOLD "])
    assert arms == []
    assert event["auto_merge_armed"] is False
    assert event["merge_held"] is True


async def test_unheld_promotion_arms(tmp_path):
    arms, event = await _promote(tmp_path, cfg={"auto_merge_repos": ["o/r"]}, labels=None)
    assert arms == [["pr", "merge", "1", "--repo", "o/r", "--auto", "--squash"]]
    assert event["merge_held"] is False


class HoldGH(RoutedGH):
    def __init__(self, *, failure=None, **kw):
        super().__init__(**kw)
        self.failure = failure

    async def __call__(self, args, timeout=30):
        if "--disable-auto" in args:
            self.calls.append(args)
            if isinstance(self.failure, Exception):
                raise self.failure
            if self.failure:
                return 1, "", self.failure
            self.pr_facts["auto_merge"] = False
            return 0, "", ""
        return await super().__call__(args, timeout)


def setup_hold(tmp_path, **over):
    gh = HoldGH(
        pr_facts=facts(labels=["merge-hold"], auto_merge=True, **over),
        reviews=[review_row(HEAD, "PASS")],
        checks=GREEN,
    )
    return make(tmp_path, cfg={**OWNER, "auto_merge_repos": ["o/r"]}, gh=gh), gh


async def test_reconcile_disarms_once_and_posts_approval(tmp_path):
    d, gh = setup_hold(tmp_path)
    await d.reconcile_pr("o/r", 1)
    assert [c for c in gh.calls if "--disable-auto" in c] == [["pr", "merge", "1", "--repo", "o/r", "--disable-auto"]]
    assert not any("--auto" in c for c in gh.calls)
    assert any(p.get("event") == "APPROVE" for p in gh.reviews_posted)
    assert any(e.get("decision") == HOLD_MERGE_HOLD for e in d.telemetry.read_all())
    projection = next(c[-1] for c in gh.calls if c[:2] == ["api", "repos/o/r/pulls/1"])
    assert "auto_merge: (.auto_merge != null)" in projection


@pytest.mark.parametrize("failure", ["permission denied", RuntimeError("offline")])
async def test_disarm_failure_warns_and_continues(tmp_path, caplog, failure):
    d, gh = setup_hold(tmp_path)
    gh.failure = failure
    with caplog.at_level(logging.WARNING):
        await d.reconcile_pr("o/r", 1)
    assert "merge-hold disarm" in caplog.text
    assert any(p.get("event") == "APPROVE" for p in gh.reviews_posted)
    assert not any("--auto" in c for c in gh.calls)


@pytest.mark.parametrize("over", [{"state": "closed"}, {"draft": True}, {"locked": True}])
async def test_open_held_pr_disarms_even_if_review_ineligible(tmp_path, over):
    d, gh = setup_hold(tmp_path, **over)
    await d.reconcile_pr("o/r", 1)
    assert sum("--disable-auto" in c for c in gh.calls) == (over.get("state") != "closed")


async def test_removing_hold_rearms_without_duplicate_approval(tmp_path):
    d, gh = setup_hold(tmp_path)
    gh.reviews.append(promotion_row(HEAD, "PASS"))
    await d.reconcile_pr("o/r", 1)
    gh.pr_facts["labels"] = []
    await d.reconcile_pr("o/r", 1)
    assert sum("--auto" in c for c in gh.calls) == 1
    assert gh.reviews_posted == []


@pytest.mark.parametrize("checks", [None, [{"status": "in_progress", "conclusion": None, "name": "CI"}]])
async def test_removed_hold_does_not_bypass_checks(tmp_path, checks):
    d, gh = setup_hold(tmp_path)
    gh.reviews.append(promotion_row(HEAD, "PASS"))
    gh.pr_facts.update(labels=[], auto_merge=False)
    gh.checks = checks
    await d.reconcile_pr("o/r", 1)
    assert not any("--auto" in c for c in gh.calls)
    assert gh.reviews_posted == []


@pytest.mark.parametrize("cfg", [{"shadow_mode": True}, {"promotion_owner": False}])
async def test_disarm_respects_ownership(tmp_path, cfg):
    _, gh = setup_hold(tmp_path)
    d = make(tmp_path / "other", cfg={**OWNER, **cfg}, gh=gh)
    await d.reconcile_pr("o/r", 1)
    assert not any("--disable-auto" in c for c in gh.calls)


@pytest.mark.parametrize("updates", [{"labels": None}, {"labels": []}, {"auto_merge": False}, {"auto_merge": None}])
async def test_reconcile_does_not_disarm_without_hold_and_known_arm(tmp_path, updates):
    d, gh = setup_hold(tmp_path)
    gh.pr_facts.update(updates)
    await d.reconcile_pr("o/r", 1)
    assert not any("--disable-auto" in c for c in gh.calls)


async def test_missing_facts_does_not_disarm(tmp_path):
    d, gh = setup_hold(tmp_path)
    gh.pr_facts = None
    await d.reconcile_pr("o/r", 1)
    assert not any("--disable-auto" in c for c in gh.calls)


async def test_disarm_precedes_backfill_early_return(tmp_path, monkeypatch):
    d, gh = setup_hold(tmp_path)

    async def needs_backfill(repo, pr):
        assert any("--disable-auto" in c for c in gh.calls)
        return HEAD

    async def backfill(repo, pr, head):
        return "backfilled"

    monkeypatch.setattr(d, "needs_backfill", needs_backfill)
    monkeypatch.setattr(d, "backfill_review", backfill)
    assert await d.reconcile_pr("o/r", 1, backfill_budget=1) == ("backfilled", 0)

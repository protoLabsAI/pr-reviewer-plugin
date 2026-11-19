"""Arming GitHub auto-merge on PASS is opt-in — a per-repo list or a PR label, off by default.

Promotion used to arm native squash auto-merge on every PR targeting `main`. That call
failed silently while the App held Contents: read; with Contents: write (needed so a FAIL
can DISARM, #235) it would land every promoted PR on every installed repo, even while the
author still has unpushed work. Arming now needs `auto_merge_repos` or `auto_merge_label`;
the FAIL disarm stays unconditional.
"""

from __future__ import annotations

import pytest

from tests.test_dispatch import HEAD, RoutedGH, facts, make, promotion_row, review_row
from tests.test_fail_disarms_auto_merge_235 import AutoMergeGH

GREEN = [{"status": "completed", "conclusion": "success", "name": "CI"}]
OWNER = {"shadow_mode": False, "promotion_owner": True}


@pytest.fixture(autouse=True)
def _no_ambient_opt_in(monkeypatch):
    monkeypatch.delenv("PR_REVIEWER_AUTO_MERGE_REPOS", raising=False)
    monkeypatch.delenv("PR_REVIEWER_AUTO_MERGE_LABEL", raising=False)


async def _promote(tmp_path, *, cfg=None, repo="o/r", **fact_over):
    gh = RoutedGH(pr_facts=facts(**fact_over), reviews=[review_row(HEAD, "PASS")], checks=GREEN)
    d = make(tmp_path, cfg={**OWNER, "repos": [repo], **(cfg or {})}, gh=gh)
    assert (await d.evaluate_promotion(repo, 1)) == "promote"
    approved = [p for p in gh.reviews_posted if p.get("event") == "APPROVE"]
    assert len(approved) == 1  # the APPROVE itself never depends on the opt-in
    arms = [c for c in gh.calls if c[0] == "pr" and "merge" in c and "--auto" in c]
    promoted = [e for e in d.telemetry.read_all() if e.get("event") == "promoted"]
    assert len(promoted) == 1
    return arms, promoted[0]


async def test_default_config_approves_without_arming(tmp_path):
    arms, event = await _promote(tmp_path, labels=["bug"])
    assert arms == []
    assert event["auto_merge_armed"] is False and event["auto_merge_opt_in"] == "none"


async def test_a_pr_without_label_facts_is_not_armed(tmp_path):
    # `labels` absent or null (an older or partial facts read) is no opt-in.
    arms, event = await _promote(tmp_path)
    assert arms == [] and event["auto_merge_opt_in"] == "none"
    arms, event = await _promote(tmp_path / "null", labels=None)
    assert arms == [] and event["auto_merge_opt_in"] == "none"


async def test_a_listed_repo_is_armed_case_insensitively(tmp_path):
    arms, event = await _promote(tmp_path, cfg={"auto_merge_repos": ["Proto/Repo"]}, repo="proto/repo")
    assert arms == [["pr", "merge", "1", "--repo", "proto/repo", "--auto", "--squash"]]
    assert event["auto_merge_armed"] is True and event["auto_merge_opt_in"] == "repo"


async def test_an_unlisted_repo_is_not_armed_by_another_repos_listing(tmp_path):
    arms, event = await _promote(tmp_path, cfg={"auto_merge_repos": ["o/other"]})
    assert arms == [] and event["auto_merge_opt_in"] == "none"


async def test_the_label_arms_a_pr_on_an_unlisted_repo(tmp_path):
    arms, event = await _promote(tmp_path, labels=["bug", "auto-merge"])
    assert arms == [["pr", "merge", "1", "--repo", "o/r", "--auto", "--squash"]]
    assert event["auto_merge_armed"] is True and event["auto_merge_opt_in"] == "label"


async def test_a_custom_label_replaces_the_default(tmp_path):
    arms, event = await _promote(tmp_path, cfg={"auto_merge_label": "ship-it"}, labels=["Ship-It"])
    assert len(arms) == 1 and event["auto_merge_opt_in"] == "label"
    arms, event = await _promote(tmp_path / "old", cfg={"auto_merge_label": "ship-it"}, labels=["auto-merge"])
    assert arms == [] and event["auto_merge_opt_in"] == "none"


async def test_an_empty_label_turns_the_label_path_off(tmp_path):
    arms, event = await _promote(tmp_path, cfg={"auto_merge_label": ""}, labels=["auto-merge", ""])
    assert arms == [] and event["auto_merge_opt_in"] == "none"


async def test_an_empty_label_leaves_the_repo_path_on(tmp_path):
    cfg = {"auto_merge_label": "", "auto_merge_repos": ["o/r"]}
    arms, event = await _promote(tmp_path, cfg=cfg)
    assert len(arms) == 1 and event["auto_merge_opt_in"] == "repo"


@pytest.mark.parametrize("cfg, labels", [({"auto_merge_repos": ["o/r"]}, []), ({}, ["auto-merge"])])
async def test_a_non_main_base_is_never_armed(tmp_path, cfg, labels):
    arms, event = await _promote(tmp_path, cfg=cfg, base_ref="develop", labels=labels)
    assert arms == []
    assert event["auto_merge_armed"] is False


async def test_a_declined_arm_is_reported_unarmed(tmp_path):
    # Best-effort: a repo with auto-merge disabled declines; the promotion still stands.
    class Declines(RoutedGH):
        async def __call__(self, args, timeout=30):
            if "--auto" in args:
                self.calls.append(args)
                return 1, "", "GraphQL: Auto merge is not allowed for this repository"
            return await super().__call__(args, timeout)

    gh = Declines(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=GREEN)
    d = make(tmp_path, cfg={**OWNER, "auto_merge_repos": ["o/r"]}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    event = [e for e in d.telemetry.read_all() if e.get("event") == "promoted"][-1]
    assert event["auto_merge_armed"] is False and event["auto_merge_opt_in"] == "repo"


# ── config / env resolution ──────────────────────────────────────────────────


def test_defaults_are_off(tmp_path):
    d = make(tmp_path)
    assert d.auto_merge_repos == []
    assert d.auto_merge_label == "auto-merge"


def test_env_repos_apply_when_the_config_key_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_AUTO_MERGE_REPOS", "o/one, o/two\no/three")
    assert make(tmp_path).auto_merge_repos == ["o/one", "o/two", "o/three"]
    # The manifest seeds `auto_merge_repos: []`; like `repos`, that still reads the env.
    assert make(tmp_path, cfg={"auto_merge_repos": []}).auto_merge_repos == ["o/one", "o/two", "o/three"]


def test_config_repos_win_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_AUTO_MERGE_REPOS", "o/env")
    assert make(tmp_path, cfg={"auto_merge_repos": ["o/cfg"]}).auto_merge_repos == ["o/cfg"]
    assert make(tmp_path, cfg={"auto_merge_repos": "o/a, o/b"}).auto_merge_repos == ["o/a", "o/b"]


def test_env_label_applies_when_the_config_key_is_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_AUTO_MERGE_LABEL", "ship-it")
    assert make(tmp_path).auto_merge_label == "ship-it"
    assert make(tmp_path, cfg={"auto_merge_label": "cfg-label"}).auto_merge_label == "cfg-label"
    assert make(tmp_path, cfg={"auto_merge_label": ""}).auto_merge_label == ""  # present ⇒ wins, even empty


def test_an_empty_env_label_turns_the_label_path_off(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_AUTO_MERGE_LABEL", "")
    assert make(tmp_path).auto_merge_label == ""


async def test_env_opt_ins_arm_on_promotion(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_AUTO_MERGE_REPOS", "O/R")
    arms, event = await _promote(tmp_path)
    assert len(arms) == 1 and event["auto_merge_opt_in"] == "repo"

    monkeypatch.delenv("PR_REVIEWER_AUTO_MERGE_REPOS")
    monkeypatch.setenv("PR_REVIEWER_AUTO_MERGE_LABEL", "ship-it")
    arms, event = await _promote(tmp_path / "label", labels=["ship-it"])
    assert len(arms) == 1 and event["auto_merge_opt_in"] == "label"


# ── the FAIL disarm is NOT opt-in ────────────────────────────────────────────


async def test_a_fail_disarms_auto_merge_on_a_repo_that_has_not_opted_in(tmp_path):
    # No `auto_merge_repos`, label path off, PR unlabelled: someone armed auto-merge by
    # hand. A FAIL on the head still turns it off.
    reviews = [review_row(HEAD, "PASS", id=1), {**promotion_row(HEAD, "PASS"), "id": 9}]
    gh = AutoMergeGH(pr_facts=facts(labels=[]), reviews=reviews, checks=GREEN)
    d = make(tmp_path, cfg={**OWNER, "auto_merge_label": ""}, gh=gh)
    assert d.auto_merge_repos == [] and d.auto_merge_label == ""
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert gh.disables == [["pr", "merge", "1", "--repo", "o/r", "--disable-auto"]]
    ev = [e for e in d.telemetry.read_all() if e.get("event") == "auto_merge_disabled"]
    assert ev and ev[-1]["ok"] is True

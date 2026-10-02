"""Issue #235 — a FAIL turns off the GitHub auto-merge approve-on-green armed.

#233 made a FAIL withdraw our approval and fail `QA panel`. That stops the merge only
where `QA panel` is a REQUIRED check; on a repo that requires something else, an armed
auto-merge still lands the PR over the FAIL once the rest goes green.
"""

from __future__ import annotations

from tests.test_dispatch import HEAD, RoutedGH, _clean_runner, facts, make, promotion_row, review_row

GREEN = [{"status": "completed", "conclusion": "success", "name": "CI"}]
OWNER = {"shadow_mode": False, "promotion_owner": True}


class AutoMergeGH(RoutedGH):
    """Serves the auto-merge read and scripts the `--disable-auto` result."""

    def __init__(self, *, auto_merge="true", disable=(0, ""), **kw):
        super().__init__(**kw)
        self.auto_merge = auto_merge  # the `.auto_merge != null` jq answer; None ⇒ read fails
        self.disable = disable
        self.disables: list[list[str]] = []

    async def __call__(self, args, timeout=30):
        if "--disable-auto" in args:
            self.calls.append(args)
            self.disables.append(args)
            rc, err = self.disable
            return rc, "", err
        if ".auto_merge != null" in args:
            self.calls.append(args)
            return (0, self.auto_merge, "") if self.auto_merge is not None else (1, "", "HTTP 502")
        return await super().__call__(args, timeout)


def _promoted_head():
    return [review_row(HEAD, "PASS", id=1), {**promotion_row(HEAD, "PASS"), "id": 9}]


def _events(d, name):
    return [e for e in d.telemetry.read_all() if e.get("event") == name]


async def test_a_fail_disables_an_armed_auto_merge(tmp_path):
    gh = AutoMergeGH(pr_facts=facts(), reviews=_promoted_head(), checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert gh.disables == [["pr", "merge", "1", "--repo", "o/r", "--disable-auto"]]
    ev = _events(d, "auto_merge_disabled")
    assert ev and ev[-1]["ok"] is True and ev[-1]["sha"] == HEAD and ev[-1]["known_enabled"] is True


async def test_nothing_to_disarm_makes_no_write(tmp_path):
    gh = AutoMergeGH(auto_merge="false", pr_facts=facts(), reviews=_promoted_head(), checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert gh.disables == []
    assert _events(d, "auto_merge_disabled") == []


async def test_an_unreadable_auto_merge_state_still_tries_to_disarm(tmp_path):
    # Fail closed: "could not read" is not "not armed".
    gh = AutoMergeGH(auto_merge=None, pr_facts=facts(), reviews=_promoted_head(), checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh)
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert len(gh.disables) == 1


async def test_a_refused_disarm_degrades_and_escalates(tmp_path):
    escalations: list[str] = []
    gh = AutoMergeGH(
        disable=(1, "GraphQL: Resource not accessible by integration"),
        pr_facts=facts(),
        reviews=_promoted_head(),
        checks=GREEN,
    )
    d = make(tmp_path, cfg=OWNER, gh=gh, inbox=lambda text, **_kw: escalations.append(text))
    # Degrade, never raise: the verdict still posts and the round still completes.
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert gh.reviews_posted  # the FAIL landed
    ev = _events(d, "auto_merge_disabled")
    assert ev and ev[-1]["ok"] is False
    assert any("auto-merge" in e and "o/r#1" in e for e in escalations), escalations


async def test_an_unknown_state_that_github_says_was_not_armed_does_not_escalate(tmp_path):
    escalations: list[str] = []
    gh = AutoMergeGH(
        auto_merge=None,
        disable=(1, "GraphQL: Auto merge is not enabled for this pull request"),
        pr_facts=facts(),
        reviews=_promoted_head(),
        checks=GREEN,
    )
    d = make(tmp_path, cfg=OWNER, gh=gh, inbox=lambda text, **_kw: escalations.append(text))
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert not any("auto-merge" in e for e in escalations)


async def test_not_the_promotion_owner_leaves_auto_merge_alone(tmp_path):
    gh = AutoMergeGH(pr_facts=facts(), reviews=_promoted_head(), checks=GREEN)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": False}, gh=gh)
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert gh.disables == []


async def test_shadow_mode_leaves_auto_merge_alone(tmp_path):
    gh = AutoMergeGH(pr_facts=facts(), reviews=_promoted_head(), checks=GREEN)
    d = make(tmp_path, gh=gh)  # shadow default
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:FAIL"
    assert gh.disables == []


async def test_a_pass_leaves_auto_merge_alone(tmp_path):
    gh = AutoMergeGH(pr_facts=facts(), reviews=_promoted_head(), checks=GREEN)
    d = make(tmp_path, cfg=OWNER, gh=gh, runner=_clean_runner)
    assert (await d.handle_summon("o/r", 1, "operator")) == "reviewed:PASS"
    assert gh.disables == []

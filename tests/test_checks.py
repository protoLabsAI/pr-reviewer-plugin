"""The `QA panel` check run — the mapping, and the writes the dispatcher makes.

The check is the only form of the panel's verdict that GitHub will actually gate a merge
on (an App's approval never satisfies a required review), so these pin two things: that
it fails on exactly what the panel is for and nothing else, and that publishing it cannot
deadlock the promotion it reports.
"""

from __future__ import annotations

import json

from pr_reviewer.approve import (
    HOLD_ALREADY_PROMOTED,
    HOLD_CHECKS_FAILED,
    HOLD_CHECKS_PENDING,
    HOLD_CHECKS_UNKNOWN,
    HOLD_INCOMPLETE,
    HOLD_NO_CLEAR_VERDICT,
    HOLD_STALE_HEAD,
    HOLD_THREADS_UNKNOWN,
    HOLD_THREADS_UNRESOLVED,
    PROMOTE,
)
from pr_reviewer.checks import CHECK_NAME, check_for

from tests.test_dispatch import HEAD, RoutedGH, facts, make, review_row, thread_node


class ChecksGH(RoutedGH):
    """RoutedGH plus the check-run endpoints: our per-name read, and the write.

    Writes are captured SEPARATELY from `posted` so a test can tell "we published a
    check" from "we posted a review" — the distinction the promotion path now turns on.
    """

    def __init__(self, *, existing=None, **kw):
        super().__init__(**kw)
        self.existing = existing  # our check run for this head, or None
        self.writes: list[dict] = []

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "check_name=" in joined:
            self.calls.append(args)
            return 0, json.dumps(self.existing) if self.existing else "null", ""
        if "/check-runs" in joined and "-X" in args and ("POST" in args or "PATCH" in args):
            self.calls.append(args)
            fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a}
            self.writes.append({"url": args[1], "method": "PATCH" if "PATCH" in args else "POST", **fields})
            return 0, "{}", ""
        return await super().__call__(args, timeout)


GREEN = [{"status": "completed", "conclusion": "success", "name": "CI"}]


def owned(tmp_path, gh):
    """A dispatcher that owns promotion (the posture the check rides on)."""
    return make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)


# ── the mapping ───────────────────────────────────────────────────────────────


def test_a_cleared_head_is_a_green_check():
    for decision in (PROMOTE, HOLD_ALREADY_PROMOTED):
        run = check_for(decision)
        assert (run.status, run.conclusion) == ("completed", "success")


def test_panel_owned_unresolved_findings_fail_the_check_and_say_how_many():
    """The WARN gate — scoped to the panel's OWN threads. The verdict stays non-blocking;
    what blocks is the panel's feedback nobody addressed. Reported by the panel-owned
    count, so an external thread never inflates or mislabels the number (issue #105)."""
    run = check_for(HOLD_THREADS_UNRESOLVED, verdict="WARN", unresolved=5, panel_unresolved=2)
    assert (run.status, run.conclusion) == ("completed", "failure")
    assert "2 unresolved review threads" in run.title  # the panel's 2, not the total 5
    assert "1 unresolved review thread" in check_for(HOLD_THREADS_UNRESOLVED, unresolved=1, panel_unresolved=1).title


def test_an_external_thread_hold_does_not_fail_the_check():
    """PASS/clear + only OTHER reviewers' threads open: promotion is held (elsewhere), but
    the panel raised none of those threads. The check stays green and names the hold
    external — it never converts an empty PASS into a panel failure (issue #105)."""
    run = check_for(HOLD_THREADS_UNRESOLVED, verdict="PASS", unresolved=1, panel_unresolved=0)
    assert (run.status, run.conclusion) == ("completed", "success")
    assert "finding" not in run.title.lower()  # never called a panel finding
    # The message distinguishes the (clear) verdict from the (held) promotion eligibility.
    summary = run.summary.lower()
    assert "clear" in summary and "held" in summary and "promotion hold" in summary


def test_unreadable_thread_ownership_holds_rather_than_fails():
    """When we can't read WHO owns the open threads, we can neither claim nor disclaim them
    as the panel's — so the check holds in progress, never a red X on our own read outage."""
    run = check_for(HOLD_THREADS_UNRESOLVED, unresolved=2, panel_unresolved=None)
    assert (run.status, run.conclusion) == ("in_progress", None)


def test_a_standing_fail_fails_the_check_but_silence_does_not():
    """Both states arrive as the same hold, and only one of them is a defect: a FAIL
    against this head, versus a head the panel simply hasn't reviewed yet."""
    failed = check_for(HOLD_NO_CLEAR_VERDICT, verdict="FAIL")
    assert (failed.status, failed.conclusion) == ("completed", "failure")

    waiting = check_for(HOLD_NO_CLEAR_VERDICT, verdict=None)
    assert (waiting.status, waiting.conclusion) == ("in_progress", None)


def test_ci_is_never_reported_twice():
    """Red or pending CI already blocks the merge. Failing our check for the same reason
    would show one problem as two, and point at the panel for CI's outage."""
    for decision in (HOLD_CHECKS_PENDING, HOLD_CHECKS_FAILED, HOLD_CHECKS_UNKNOWN):
        run = check_for(decision)
        assert (run.status, run.conclusion) == ("in_progress", None)


def test_every_unknown_holds_rather_than_fails():
    """An unreadable fact, or a hold added after this file was written, must not turn
    into a red X: refusing to say "clear" is already the closed position for a gate."""
    for decision in (HOLD_STALE_HEAD, HOLD_INCOMPLETE, HOLD_THREADS_UNKNOWN, "hold:something-new"):
        run = check_for(decision)
        assert (run.status, run.conclusion) == ("in_progress", None)


# ── the writes ────────────────────────────────────────────────────────────────


async def test_our_own_check_is_not_a_check_we_wait_on(tmp_path):
    """The deadlock this design would otherwise ship with.

    Our check sits `in_progress` until the panel clears the head. Counted among the
    checks promotion waits on, it makes the read "pending" forever: the panel holds on
    checks-pending, so it never clears, so its own check never concludes.
    """
    gh = ChecksGH(
        pr_facts=facts(),
        reviews=[review_row(HEAD, "PASS")],
        checks=[{"status": "in_progress", "conclusion": None, "name": CHECK_NAME}, *GREEN],
    )
    d = owned(tmp_path, gh)
    assert (await d._checks_state("o/r", HEAD)) == "green"
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"


async def test_a_repo_whose_only_check_is_ours_still_never_promotes(tmp_path):
    """Excluding ourselves must not manufacture a green: with our check filtered out
    there are NO checks, and a checkless head has never been promotable (it fails
    closed). The exclusion removes a deadlock, not the gate."""
    gh = ChecksGH(
        pr_facts=facts(),
        reviews=[review_row(HEAD, "PASS")],
        checks=[{"status": "in_progress", "conclusion": None, "name": CHECK_NAME}],
    )
    d = owned(tmp_path, gh)
    assert (await d._checks_state("o/r", HEAD)) == "no-checks"
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:checks-failed"


async def test_promotion_publishes_a_green_check_for_the_head(tmp_path):
    gh = ChecksGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=GREEN)
    d = owned(tmp_path, gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    assert len(gh.writes) == 1
    write = gh.writes[0]
    assert write["method"] == "POST" and write["url"] == "repos/o/r/check-runs"
    assert write["name"] == CHECK_NAME and write["head_sha"] == HEAD
    assert write["status"] == "completed" and write["conclusion"] == "success"


async def test_panel_owned_unresolved_thread_publishes_a_failing_check(tmp_path):
    """The end-to-end shape of "address the panel's findings before you merge": a thread
    the panel itself raised (root comment by our bot login) still fails the check."""
    gh = ChecksGH(
        pr_facts=facts(),
        reviews=[review_row(HEAD, "WARN")],
        checks=GREEN,
        threads=[thread_node("qa-bot[bot]")],  # ours
    )
    d = owned(tmp_path, gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:threads-unresolved"
    assert gh.writes[0]["conclusion"] == "failure"
    assert gh.reviews_posted == []  # held: no approval, and the check says why


async def test_an_external_unresolved_thread_does_not_fail_the_check(tmp_path):
    """Issue #105: a clean PASS with no panel findings must not publish a FAILING QA-panel
    check merely because an unrelated reviewer has an open thread — nor call it the panel's.
    Promotion stays fail-closed on the open thread; the check reflects the clear verdict."""
    gh = ChecksGH(
        pr_facts=facts(),
        reviews=[review_row(HEAD, "PASS")],
        checks=GREEN,
        threads=[thread_node("coderabbitai[bot]")],  # someone else's
    )
    d = owned(tmp_path, gh)
    # Promotion stays fail-closed while any thread is open — the external thread still holds.
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:threads-unresolved"
    assert gh.reviews_posted == []  # not approved — external thread holds promotion
    # ...but the QA-panel check does NOT fail, and does not call the thread a panel finding.
    write = gh.writes[0]
    assert write["conclusion"] == "success"
    assert "finding" not in write["output[title]"].lower()
    assert "held" in write["output[summary]"].lower()


async def test_an_unchanged_check_is_not_rewritten(tmp_path):
    """The sweep re-evaluates every open PR every few minutes and GitHub keeps every
    check run POSTed — so re-publishing the same state would bury the PR's check list
    under hundreds of identical runs within a day."""
    gh = ChecksGH(
        pr_facts=facts(),
        reviews=[review_row(HEAD, "PASS")],
        checks=GREEN,
        existing={
            "id": 55,
            "status": "completed",
            "conclusion": "success",
            "title": "Cleared by the QA panel",
        },
    )
    d = owned(tmp_path, gh)
    await d.evaluate_promotion("o/r", 1)
    assert gh.writes == []


async def test_a_changed_check_is_patched_in_place(tmp_path):
    """One check run per head, updated — not one per sweep pass."""
    gh = ChecksGH(
        pr_facts=facts(),
        reviews=[review_row(HEAD, "PASS")],
        checks=GREEN,
        existing={"id": 55, "status": "in_progress", "conclusion": None, "title": "Waiting on CI"},
    )
    d = owned(tmp_path, gh)
    await d.evaluate_promotion("o/r", 1)
    assert len(gh.writes) == 1
    assert gh.writes[0]["method"] == "PATCH"
    assert gh.writes[0]["url"] == "repos/o/r/check-runs/55"
    assert gh.writes[0]["conclusion"] == "success"


async def test_shadow_mode_publishes_nothing(tmp_path):
    """A REQUIRED check nobody drives blocks every merge in that repo forever, so the
    check rides the same per-repo handover as approve-on-green: no ownership, no check."""
    gh = ChecksGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=GREEN)
    d = make(tmp_path, cfg={"shadow_mode": True, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:not-promotion-owner"
    assert gh.writes == []


async def test_the_knob_turns_the_check_off_without_touching_promotion(tmp_path):
    gh = ChecksGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=GREEN)
    d = make(
        tmp_path,
        cfg={"shadow_mode": False, "promotion_owner": True, "qa_check": False},
        gh=gh,
    )
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    assert gh.writes == []
    assert gh.reviews_posted[0]["event"] == "APPROVE"

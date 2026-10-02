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
from pr_reviewer.checks import CHECK_NAME, check_for, queued_run

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
    for decision in (HOLD_STALE_HEAD, HOLD_THREADS_UNKNOWN, "hold:something-new"):
        run = check_for(decision)
        assert (run.status, run.conclusion) == ("in_progress", None)


def test_an_incomplete_pass_concludes_neutral_and_says_so():
    """#130: an incomplete clear pass held the check `in_progress` with nothing red and
    nothing to re-run — only a new commit cleared it. The verdict is already capped at WARN
    for the same gap (#117), and WARN does not block; the check now agrees. Approve-on-green
    is still withheld (`promotion_decision` is untouched), so this is a human's merge."""
    run = check_for(HOLD_INCOMPLETE)
    assert (run.status, run.conclusion) == ("completed", "neutral")
    assert "not blocking" in run.title.lower()
    assert "auto-approve is withheld" in run.summary.lower()


# ── the queued run: what a waiting head shows (#209) ────────────────────────────


def test_queued_run_reports_how_many_are_ahead_and_an_eta():
    """r5: the queued-check builder — status `queued`, "queued behind N", ETA rounded UP."""
    run = queued_run(2, 130.0)  # 130s → ceil to 3 whole minutes
    assert (run.status, run.conclusion) == ("queued", None)
    assert "queued behind 2" in run.title.lower()
    assert "3 min" in run.title  # 130s rounds UP, never down
    assert "3 min" in run.summary


def test_queued_run_omits_the_eta_when_there_is_no_data():
    """r5: no duration data yet → the count still shows, the ETA is simply dropped."""
    run = queued_run(1, None)
    assert run.status == "queued"
    assert "queued behind 1" in run.title.lower()
    assert "min" not in run.title.lower() and "eta" not in run.title.lower()
    # a non-positive estimate is treated the same as absent, never "~0 min"
    assert "min" not in queued_run(1, 0.0).title.lower()


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


# ── a waiting head shows a `queued` check (#209) ───────────────────────────────


async def test_a_waiting_head_publishes_a_queued_check_with_the_count_and_eta(tmp_path):
    """r1: `publish_queued_check` posts the QA-panel check as `queued` on the waiting head,
    naming how many are ahead and the ETA — so GitHub shows the wait instead of nothing."""
    gh = ChecksGH(pr_facts=facts(), existing=None)
    d = owned(tmp_path, gh)
    await d.publish_queued_check("o/r", HEAD, 1, 120.0)
    (write,) = gh.writes
    assert write["method"] == "POST" and write["url"] == "repos/o/r/check-runs"
    assert write["name"] == CHECK_NAME and write["head_sha"] == HEAD
    assert write["status"] == "queued" and "conclusion" not in write  # queued has no conclusion
    assert "queued behind 1" in write["output[title]"].lower() and "2 min" in write["output[title]"]


async def test_the_queued_check_becomes_the_in_progress_run_once_the_round_starts(tmp_path):
    """r4: the `queued` run IS the same QA-panel check, so the normal flow PATCHes it forward
    (queued → in_progress) once the slot is acquired — no second, dangling run."""
    gh = ChecksGH(
        pr_facts=facts(),
        existing={"id": 55, "status": "queued", "conclusion": None, "title": "Queued behind 1 (ETA ~2 min)"},
    )
    d = owned(tmp_path, gh)
    # "Waiting for the panel" — the state the check moves to as the round begins.
    await d._publish_qa_check("o/r", HEAD, check_for(HOLD_NO_CLEAR_VERDICT, verdict=None))
    (write,) = gh.writes
    assert write["method"] == "PATCH" and write["url"] == "repos/o/r/check-runs/55"
    assert write["status"] == "in_progress"


async def test_a_queued_check_publish_failure_is_swallowed(tmp_path):
    """r3: a failing publish degrades (logged, not raised) so the round it precedes still runs."""

    class BoomGH(ChecksGH):
        async def __call__(self, args, timeout=30):
            if "/check-runs" in " ".join(args) and "-X" in args:
                self.calls.append(args)
                return 1, "", "403 Resource not accessible by integration"
            return await super().__call__(args, timeout)

    gh = BoomGH(pr_facts=facts(), existing=None)
    d = owned(tmp_path, gh)
    await d.publish_queued_check("o/r", HEAD, 1, None)  # must not raise


async def test_shadow_mode_publishes_no_queued_check(tmp_path):
    """A `queued` REQUIRED check in a repo we do not drive would block every merge forever,
    so the queued check rides the same promotion-owner gate — no ownership, no GitHub call."""
    gh = ChecksGH(pr_facts=facts(), existing=None)
    d = make(tmp_path, cfg={"shadow_mode": True, "promotion_owner": True}, gh=gh)
    await d.publish_queued_check("o/r", HEAD, 1, 60.0)
    assert gh.writes == [] and gh.calls == []


async def test_the_qa_check_knob_turns_the_queued_check_off_too(tmp_path):
    gh = ChecksGH(pr_facts=facts(), existing=None)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True, "qa_check": False}, gh=gh)
    await d.publish_queued_check("o/r", HEAD, 1, 60.0)
    assert gh.writes == [] and gh.calls == []


# ── a closed PR ends the wait (#153) ──────────────────────────────────────────


WAITING_ON_CI = {"id": 106266412003, "status": "in_progress", "conclusion": None, "title": "Waiting on CI"}


async def test_closing_a_pr_concludes_a_run_that_was_still_waiting(tmp_path):
    """protoAgent#3564: the round parked on "Waiting on CI", the PR merged six minutes later,
    and the run was still `in_progress` eleven hours on — the sweep only visits OPEN PRs, so
    nothing was ever going to revisit it. The close is the last event the head gets."""
    gh = ChecksGH(pr_facts=facts(), existing=WAITING_ON_CI)
    d = owned(tmp_path, gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "closed")) == "drop:not-a-dispatch-action"
    (write,) = gh.writes
    assert write["method"] == "PATCH" and write["url"].endswith("/check-runs/106266412003")
    assert (write["status"], write["conclusion"]) == ("completed", "neutral")
    assert "closed" in write["output[title]"].lower()


async def test_closing_a_pr_never_rewrites_a_verdict_that_already_concluded(tmp_path):
    for conclusion in ("success", "failure"):
        done = {"id": 7, "status": "completed", "conclusion": conclusion, "title": "x"}
        gh = ChecksGH(pr_facts=facts(), existing=done)
        await owned(tmp_path, gh).handle_pr_event("o/r", 1, HEAD, "closed")
        assert gh.writes == []


async def test_closing_a_pr_that_never_had_a_run_creates_none(tmp_path):
    gh = ChecksGH(pr_facts=facts(), existing=None)
    await owned(tmp_path, gh).handle_pr_event("o/r", 1, HEAD, "closed")
    assert gh.writes == []


async def test_a_close_on_an_unlisted_repo_makes_no_github_call(tmp_path):
    gh = ChecksGH(pr_facts=facts(), existing=WAITING_ON_CI)
    d = make(tmp_path, cfg={"shadow_mode": False, "repos": ["o/other"]}, gh=gh)
    await d.handle_pr_event("o/r", 1, HEAD, "closed")
    assert gh.calls == [] and gh.writes == []


async def test_other_non_dispatch_actions_still_touch_nothing(tmp_path):
    gh = ChecksGH(pr_facts=facts(), existing=WAITING_ON_CI)
    await owned(tmp_path, gh).handle_pr_event("o/r", 1, HEAD, "labeled")
    assert gh.calls == [] and gh.writes == []


def test_an_unverified_hold_says_a_re_run_is_coming_then_what_to_do_when_it_failed():
    from pr_reviewer.approve import HOLD_UNVERIFIED

    pending = check_for(HOLD_UNVERIFIED, verdict="PASS", verify_retry="retry")
    assert pending.status == "in_progress" and "re-running" in pending.title
    assert "hold:unverified" in pending.summary
    gave_up = check_for(HOLD_UNVERIFIED, verdict="PASS", verify_retry="exhausted")
    assert gave_up.title == "Verifier failed twice — summon @vera review or push"
    assert gave_up.status == "in_progress" and "hold:unverified" in gave_up.summary


def test_every_hold_names_its_reason_in_the_summary():
    # #220: the board reads the check through `gh`, which does not always carry the title.
    from pr_reviewer.approve import HOLD_CHECKS_PENDING, HOLD_INCOMPLETE, HOLD_STALE_HEAD

    for decision in (HOLD_STALE_HEAD, HOLD_INCOMPLETE, HOLD_CHECKS_PENDING, "hold:promote-backoff"):
        assert decision in check_for(decision).summary
    assert "hold:" not in check_for("promote").summary

"""Re-review a force-pushed (rebased) head; never post a superseded head's verdict onto
the new head (protoLabsAI/pr-reviewer-plugin#209, defect 1).

The incident: design-system-plugin#19 was force-pushed 6300858 → 06acf0b while a round
for 6300858 was in flight. The old-head round finished and posted CHANGES_REQUESTED — and,
because a review POST carries no commit_id, that block landed on the NEW head, code the
panel never saw. Meanwhile the 3-min sweep never backfilled 06acf0b for 50+ minutes: the
PR's in-flight slot (held by the superseded round) suppressed the backfill, and its
`protoReview` / `QA panel` checks sat `in_progress` forever.

Three seams close it:
  1. `_post_verdict` posts a NON-BLOCKING comment (never REQUEST_CHANGES) when the head
     moved past the one the round pinned, and leaves the current head unreviewed so the
     sweep queues it.
  2. the chokepoint's in-flight guard is keyed by the reviewed sha, so a backfill of the
     CURRENT head is admitted even while a round for a SUPERSEDED head is still in flight.
  3. the current head's own round opens AND concludes its `protoReview` check.
"""

from __future__ import annotations

from pr_reviewer.chokepoint import DROP_IN_FLIGHT, Chokepoint
from pr_reviewer.verdicts import parse_verdict_marker

from tests.test_dispatch import (
    HEAD,
    OLD_HEAD,
    MidRoundPushGH,
    RoutedGH,
    _telemetry_events,
    capturing_runner,
    facts,
    make,
)

# The head the PR was force-pushed to (divergent from HEAD, so the pinned…current compare
# is unreadable — the realistic rebase shape, distinct from a fast-forward mid-round push).
FORCE_PUSHED_HEAD = "c" * 40

# A terminal-green CI, so a FAIL verdict WOULD arm a blocking REQUEST_CHANGES if the head
# had not moved — the control the superseded posture has to override.
GREEN = [{"status": "completed", "conclusion": "success", "name": "CI"}]


# ── the chokepoint seam: a superseded head in flight does not lock out the current one ──


def _clock(start=1000.0):
    state = {"t": start}
    return state, (lambda: state["t"])


def test_a_backfill_of_the_current_head_is_admitted_while_a_superseded_head_is_in_flight():
    _state, now = _clock()
    cp = Chokepoint(cooldown_s=0, now=now)
    # A round is in flight for the head the PR has since moved past.
    assert cp.admit("o/r", 1, OLD_HEAD) == "accept"
    # The default (webhook) posture is unchanged: a different head still drops as in-flight.
    assert cp.admit("o/r", 1, HEAD) == DROP_IN_FLIGHT
    # The sweep's backfill of the CURRENT head is admitted despite the superseded round.
    assert cp.admit("o/r", 1, HEAD, supersede_stale=True) == "accept"
    # …but two panels on the SAME head are still refused, even with the flag.
    assert cp.admit("o/r", 1, HEAD, supersede_stale=True) == DROP_IN_FLIGHT
    # Each round frees only its OWN slot — the superseded round finishing must not free
    # the current head's, nor vice versa.
    cp.done("o/r", 1, HEAD)
    assert cp.admit("o/r", 1, HEAD, supersede_stale=True) == "accept"  # current head free again
    assert cp.admit("o/r", 1, OLD_HEAD) == DROP_IN_FLIGHT  # superseded round still holds its slot


def test_a_same_head_round_in_flight_still_suppresses_its_backfill():
    # r2's boundary: the backfill runs "once no round for THAT head is in flight". A round
    # already on the head the backfill wants is exactly that — it must still drop.
    _state, now = _clock()
    cp = Chokepoint(cooldown_s=0, now=now)
    assert cp.admit("o/r", 1, HEAD) == "accept"
    assert cp.admit("o/r", 1, HEAD, supersede_stale=True) == DROP_IN_FLIGHT


# ── r1: a superseded round posts non-blocking, and leaves the new head queued ──────────


async def test_a_superseded_fail_posts_non_blocking_and_queues_the_new_head(tmp_path):
    # Round pins HEAD; the PR is force-pushed to FORCE_PUSHED_HEAD before the round posts.
    gh = MidRoundPushGH(
        pushed_head=FORCE_PUSHED_HEAD,
        stale_compare=None,  # divergent history: the pinned…current delta is unreadable
        pr_facts=facts(),
        reviews=[],
        checks=GREEN,  # terminal-green: a non-superseded FAIL here WOULD block
    )
    runner, _seen = capturing_runner()  # the default report is a FAIL (one confirmed major)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)

    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"

    (post,) = gh.reviews_posted
    # The verdict is FAIL, but it lands as a COMMENT — never a block on the new head.
    assert post["event"] == "COMMENT"
    marker = parse_verdict_marker(post["body"])
    assert marker["head"] == HEAD and marker["verdict"] == "FAIL"  # the round ran against HEAD
    assert "non-blocking comment against the superseded head" in post["body"]
    assert "PR advanced" in post["body"]

    # The move is recorded as its own countable class.
    (row,) = _telemetry_events(tmp_path, "superseded")
    assert row["sha"] == HEAD and row["verdict"] == "FAIL"

    # The current head has no verdict of ours, so the sweep will queue it for its own round.
    assert (await d.needs_backfill("o/r", 1)) == FORCE_PUSHED_HEAD


async def test_a_moved_head_never_blocks_even_when_the_delta_is_readable(tmp_path):
    # A readable mid-round delta demotes the findings it touched (issue #82) AND, now, keeps
    # the verdict non-blocking on the head that will merge (#209) — the two are complementary.
    compare = {"commits": 1, "files": [{"filename": "x.py", "patch": "@@ -1,3 +1,4 @@\n a\n+b\n c\n d\n"}]}
    gh = MidRoundPushGH(
        pushed_head=FORCE_PUSHED_HEAD, stale_compare=compare, pr_facts=facts(), reviews=[], checks=GREEN
    )
    runner, _seen = capturing_runner()
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)

    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    (post,) = gh.reviews_posted
    assert post["event"] == "COMMENT"
    assert "demoted to *possibly addressed*" in post["body"]
    assert "non-blocking comment against the superseded head" in post["body"]


# ── r2: the sweep backfills the current head despite a superseded round in flight ──────


async def test_sweep_backfills_the_current_head_while_a_superseded_round_is_in_flight(tmp_path):
    # The current head (HEAD) has no completed review; a round for the head the PR moved past
    # (OLD_HEAD) is still recorded as in flight and has NOT released its slot.
    gh = RoutedGH(pr_facts=facts(head=HEAD), reviews=[], checks=GREEN)
    d = make(tmp_path, gh=gh)
    d.chokepoint.admit("o/r", 1, OLD_HEAD)  # the superseded round, never done()

    assert (await d.sweep_once()) == 1
    await d.drain_backfills()  # the sweep detaches the backfill; wait for it to settle

    # The backfill ran and posted a verdict for the CURRENT head — not dropped as in-flight.
    assert gh.reviews_posted, "the current head was never backfilled"
    assert parse_verdict_marker(gh.reviews_posted[-1]["body"])["head"] == HEAD
    assert [e for e in _telemetry_events(tmp_path, "backfill") if e.get("sha") == HEAD]
    assert not [e for e in _telemetry_events(tmp_path, "drop") if e.get("reason") == "in-flight"]
    # The superseded round still holds its own slot — the backfill freed only the head it ran.
    assert d.chokepoint.admit("o/r", 1, OLD_HEAD) == DROP_IN_FLIGHT


async def test_sweep_still_holds_off_when_a_round_for_the_current_head_is_in_flight(tmp_path):
    # The complement: when the in-flight round IS for the current head, the backfill must
    # still stand down — the running round will post for this very head.
    gh = RoutedGH(pr_facts=facts(head=HEAD), reviews=[], checks=GREEN)
    d = make(tmp_path, gh=gh)
    d.chokepoint.admit("o/r", 1, HEAD)  # a round for the CURRENT head is already running

    await d.sweep_once()
    await d.drain_backfills()

    assert gh.reviews_posted == []  # no second panel on the head already under review
    assert [e for e in _telemetry_events(tmp_path, "drop") if e.get("reason") == "in-flight"]


# ── r3: the current head's round concludes its own protoReview check with its verdict ──


async def test_the_new_heads_round_concludes_its_protoReview_check_with_its_verdict(tmp_path):
    gh = RoutedGH(pr_facts=facts(head=FORCE_PUSHED_HEAD), reviews=[], checks=GREEN)
    d = make(tmp_path, gh=gh)  # shadow: the review-lifecycle check runs regardless

    assert (await d.handle_pr_event("o/r", 1, FORCE_PUSHED_HEAD, "synchronize")) == "reviewed:FAIL"

    opened = [w for w in gh.check_writes if w["method"] == "POST"]
    assert any(w.get("head_sha") == FORCE_PUSHED_HEAD and w.get("name") == "protoReview" for w in opened)
    concluded = [w for w in gh.check_writes if w["method"] == "PATCH"]
    assert concluded, "the protoReview check on the new head was left in_progress"
    last = concluded[-1]
    assert last["status"] == "completed" and last["conclusion"] == "failure"  # FAIL → failing check
    assert "FAIL" in last["output[summary]"]


# ── r4: an `edited` action is still not a dispatch action ──────────────────────────────


async def test_an_edited_action_is_still_dropped_as_not_a_dispatch_action(tmp_path):
    # The fix is the head-advanced / backfill path, not dispatching on edits: an `edited`
    # event is not a code change and stays a drop.
    d = make(tmp_path)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "edited")) == "drop:not-a-dispatch-action"

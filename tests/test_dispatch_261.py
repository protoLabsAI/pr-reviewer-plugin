"""Issues #261 and #265 through the dispatcher.

#261 part 2: a round whose PR merged or closed while it ran posts nothing (data-plugin#1 r2
posted 9 minutes after the merge; protoAgent#4025 3 minutes after). #261 parts 1/3 and #265: what
the posted findings RECORD says — the next round, `review_at_head.py`, the refutation stores and
eval read it, so it must match what the verdict read.
"""

from __future__ import annotations

import asyncio
import base64
import json
from urllib.parse import unquote

from pr_reviewer.dispatch import DROP_SUPERSEDED_CLOSED, DROP_SUPERSEDED_MERGED, pr_ended
from pr_reviewer.verdicts import read_findings_record, verdict_for

from tests.test_absence_claims_209 import AbsenceGH, absence_report
from tests.test_dispatch import HEAD, REPORT, RoutedGH, facts, make


def events(d, name):
    return [e for e in d.telemetry.read_all() if e.get("event") == name]


def test_pr_ended_reads_only_a_readable_closed_state():
    assert pr_ended({"state": "closed", "merged": True}) == "merged"
    assert pr_ended({"state": "closed", "merged": False}) == "closed"
    assert pr_ended({"state": "closed"}) == "closed"
    assert pr_ended({"state": "open"}) is None
    assert pr_ended(None) is None and pr_ended({}) is None  # unreadable: never stops a round


# ── part 2: post-after-merge ──


async def test_a_pr_merged_during_the_panel_gets_no_review(tmp_path):
    gh = RoutedGH(pr_facts=facts())

    async def runner(name, inputs):
        gh.pr_facts = facts(state="closed", merged=True)  # merged while the finders ran
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == f"drop:{DROP_SUPERSEDED_MERGED}"
    assert gh.reviews_posted == []
    (ev,) = events(d, "superseded")
    assert ev["superseded"] == "merged" and ev["phase"] == "posting"
    assert not events(d, "reviewed")
    # The round's protoReview run is concluded neutral, not left in progress.
    assert any(w.get("conclusion") == "neutral" for w in gh.check_writes if w["method"] == "PATCH")


async def test_a_pr_closed_unmerged_during_the_panel_gets_no_review(tmp_path):
    gh = RoutedGH(pr_facts=facts())

    async def runner(name, inputs):
        gh.pr_facts = facts(state="closed", merged=False)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == f"drop:{DROP_SUPERSEDED_CLOSED}"
    assert gh.reviews_posted == [] and events(d, "superseded")[0]["superseded"] == "closed"


async def test_an_unreadable_pr_at_post_time_still_posts(tmp_path):
    gh = RoutedGH(pr_facts=facts())

    async def runner(name, inputs):
        gh.pr_facts = None  # the read fails: a lost verdict is worse than a late one
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert len(gh.reviews_posted) == 1


async def test_a_merge_seen_at_the_verify_boundary_stops_the_round_before_verify_runs(tmp_path):
    gh = RoutedGH(pr_facts=facts())
    ran_verify = []

    async def runner(name, inputs, on_step=None):
        on_step("find_correctness")
        gh.pr_facts = facts(state="closed", merged=True)
        on_step("verify")  # the expensive tail: a PR read is scheduled here
        await asyncio.sleep(5)  # cancelled by the check long before this returns
        ran_verify.append(True)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    outcome = await asyncio.wait_for(d.handle_pr_event("o/r", 1, HEAD, "opened"), timeout=3)
    assert outcome == f"drop:{DROP_SUPERSEDED_MERGED}" and ran_verify == []
    assert gh.reviews_posted == []
    (ev,) = events(d, "superseded")
    assert ev["superseded"] == "merged" and ev["phase"] == "verify" and ev["cancelled"] is True


async def test_an_open_pr_at_the_boundary_runs_on(tmp_path):
    gh = RoutedGH(pr_facts=facts())

    async def runner(name, inputs, on_step=None):
        on_step("verify")
        await asyncio.sleep(0.05)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


# ── the record matches what the verdict read ──


async def test_265_a_truncated_diff_absence_demotion_reaches_the_record(tmp_path):
    # #209's siteprobe shape: the diff overran the budget and dropped the test file, so the
    # blocking "no test file" claim is unestablished and the verdict read it as uncertain.
    gh = AbsenceGH(
        tree={"pkg/siteprobe.py", "tests/test_site_audit.py"},
        file_patches={"pkg/siteprobe.py": "x" * 40, "tests/test_site_audit.py": "y" * 40},
    )

    async def runner(name, inputs):
        return {"output": absence_report("pkg/siteprobe.py", "pkg/siteprobe.py has no test file."), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False, "diff_char_budget": 50}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    record, recorded = read_findings_record(gh.reviews_posted[0]["body"])
    assert recorded and len(record) == 1
    assert record[0]["verdict"] == "uncertain" and record[0]["absence_demoted"] == "diff-truncated"
    assert record[0]["ungrounded"] is True
    assert verdict_for(record) == "WARN"  # the record, read back, gives the verdict that posted


class ContentsGH(AbsenceGH):
    """Serves real head contents per path, and records which contents URLs were read."""

    def __init__(self, *, contents, **kw):
        super().__init__(**kw)
        self.contents = dict(contents)
        self.reads: list[str] = []

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/contents/" in joined:
            self.calls.append(args)
            self.reads.append(args[1])
            path = unquote(joined.split("/contents/", 1)[1].split("?", 1)[0])
            if path not in self.contents:
                return 1, "", "404"
            return 0, "base64\x00" + base64.b64encode(self.contents[path].encode()).decode(), ""
        return await super().__call__(args, timeout=timeout)


def one(**f):
    base = {"severity": "minor", "category": "correctness", "verdict": "confirmed", "note": "Read it at head."}
    return "<!-- brief -->\nB.\n<!-- /brief -->\n\n```json\n" + json.dumps([{**base, **f}]) + "\n```"


async def test_261_the_record_carries_the_reanchored_line_and_the_panels_own(tmp_path):
    src = "import x\n\n\n\nvalue = compute_total(items, tax=rate)\n"
    gh = ContentsGH(contents={"a.py": src}, tree={"a.py"}, file_patches={"a.py": "+value"})

    async def runner(name, inputs):
        return {
            "output": one(file="a.py", line=1, claim="Bug.", evidence="`value = compute_total(items, tax=rate)`"),
            "failed": [],
        }

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    record, _ = read_findings_record(gh.reviews_posted[0]["body"])
    assert record[0]["line"] == 5 and record[0]["line_original"] == 1 and record[0]["line_corrected"] is True


async def test_261_a_quote_from_the_named_file_is_not_downgraded_and_the_read_is_pinned(tmp_path):
    contents = {
        "tests/test_plugin.py": "def test_register():\n    assert True\n",
        "tests/conftest.py": "def call(tool, **kw) -> str:\n    return tool.invoke(kw)\n",
    }
    gh = ContentsGH(contents=contents, tree=set(contents), file_patches={"tests/test_plugin.py": "+x"})

    async def runner(name, inputs):
        return {
            "output": one(
                file="tests/test_plugin.py",
                claim="The suite has a helper nobody exercises here.",
                evidence="tests/conftest.py defines `def call(tool, **kw) -> str: return tool.invoke(kw)`.",
            ),
            "failed": [],
        }

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    body = gh.reviews_posted[0]["body"]
    assert "quoted evidence not found" not in body
    record, _ = read_findings_record(body)
    assert record[0]["verdict"] == "confirmed" and not record[0].get("ungrounded")
    conftest_reads = [u for u in gh.reads if "tests/conftest.py" in u]
    assert conftest_reads and all(f"ref={HEAD}" in u for u in conftest_reads)


# ── the #258 poll: a PR merged mid-finder stops within one poll interval ──


class ParkedInFinder:
    """A runner stuck in one long finder: no step boundary after the first, so only the poll
    can stop it. `started` is set once the finder is running."""

    def __init__(self):
        self.started = asyncio.Event()
        self.finished = False

    async def __call__(self, name, inputs, on_step=None):
        if on_step:
            on_step("find_correctness")
        self.started.set()
        await asyncio.sleep(30)
        self.finished = True
        return {"output": REPORT, "failed": []}


async def test_a_pr_merged_mid_finder_stops_within_one_poll_interval(tmp_path):
    gh = RoutedGH(pr_facts=facts())
    runner = ParkedInFinder()
    d = make(tmp_path, cfg={"shadow_mode": False, "supersede_poll_s": 0.05}, gh=gh, runner=runner)
    round_ = asyncio.ensure_future(d.handle_pr_event("o/r", 1, HEAD, "opened"))
    await asyncio.wait_for(runner.started.wait(), timeout=2)
    gh.pr_facts = facts(state="closed", merged=True)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "closed")) == "drop:not-a-dispatch-action"
    outcome = await asyncio.wait_for(round_, timeout=2)  # far inside the 30s finder
    assert outcome == f"drop:{DROP_SUPERSEDED_MERGED}" and not runner.finished
    assert gh.reviews_posted == []
    (ev,) = events(d, "superseded")
    assert ev["superseded"] == "merged" and ev["phase"] == "finders" and ev["cancelled"] is True
    assert "o/r#1" not in d._ended_hint


async def test_a_closed_event_with_an_unreadable_pr_lets_the_round_run_on(tmp_path):
    gh = RoutedGH(pr_facts=facts())
    runner = ParkedInFinder()
    d = make(tmp_path, cfg={"shadow_mode": False, "supersede_poll_s": 0.02}, gh=gh, runner=runner)
    round_ = asyncio.ensure_future(d.handle_pr_event("o/r", 1, HEAD, "opened"))
    await asyncio.wait_for(runner.started.wait(), timeout=2)
    gh.pr_facts = None  # the read fails: fail-closed, the round keeps going
    await d.handle_pr_event("o/r", 1, HEAD, "closed")
    await asyncio.sleep(0.2)  # ~10 poll ticks
    assert not round_.done() and not events(d, "superseded")
    round_.cancel()
    try:
        await round_
    except asyncio.CancelledError:
        pass


async def test_without_a_closed_event_the_poll_reads_nothing(tmp_path):
    gh = RoutedGH(pr_facts=facts())
    runner = ParkedInFinder()
    d = make(tmp_path, cfg={"shadow_mode": False, "supersede_poll_s": 0.01}, gh=gh, runner=runner)
    round_ = asyncio.ensure_future(d.handle_pr_event("o/r", 1, HEAD, "opened"))
    await asyncio.wait_for(runner.started.wait(), timeout=2)
    before = sum("/pulls/1" in " ".join(c) for c in gh.calls)
    await asyncio.sleep(0.1)
    assert sum("/pulls/1" in " ".join(c) for c in gh.calls) == before
    round_.cancel()
    try:
        await round_
    except asyncio.CancelledError:
        pass


async def test_a_stale_closed_hint_is_dropped_when_a_round_opens(tmp_path):
    gh = RoutedGH(pr_facts=facts())

    async def runner(name, inputs):
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    d._hint_ended("o/r", 1)  # closed, then reopened before this round
    assert (await d.handle_pr_event("o/r", 1, HEAD, "reopened")).startswith("reviewed:")
    assert "o/r#1" not in d._ended_hint

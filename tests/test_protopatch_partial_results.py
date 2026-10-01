"""A pass that is cut short must not throw away the features that already finished (#205).

Any non-zero clawpatch exit, or the budget SIGKILL, used to return `PROTOPATCH UNAVAILABLE` and drop
everything — including the findings the completed features had already written to the state dir.
On 2026-09-30 three protoAgent passes were killed at the 900 s budget with 33-35 of 36 features
reviewed, and 49 findings were discarded with them.

Now, when at least one claimed feature finished, those findings come back as a PARTIAL result: still
a lane gap (the round is incomplete and the verdict capped at WARN — a pass that covered less must
not read as a clean PASS), but the findings are kept. With nothing finished it is the outage it
always was. Pinned here at the runner; the round's reaction is in test_dispatch.py.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import (
    GAP_PARTIAL_PREFIX,
    PARTIAL_HEADER,
    PARTIAL_PREFIX,
    STRUCTURAL_GAP_MARKERS,
    UNAVAILABLE_PREFIX,
    ProtoPatchRunner,
    classify_outage,
    is_partial_output,
    outage_reason,
    pass_coverage,
)

SHA_HEAD = "a" * 40
SHA_BASE = "b" * 40
FETCH_FAILED = "gateway review: request failed (fetch failed)"  # transient: retried once


def rec(title, path="lib/cache.ts", sig=None):
    return {
        "title": title,
        "category": "concurrency",
        "severity": "critical",
        "confidence": "high",
        "evidence": [{"path": path, "startLine": 42, "quote": "rmSync(...)"}],
        "reasoning": "r",
        "recommendation": "lock",
        "status": "open",
        "signature": sig or title,
    }


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    monkeypatch.delenv("CLAWPATCH_GATEWAY_TIMEOUT_MS", raising=False)

    async def fake_run_gh(args, timeout=30):
        return (0, f"{SHA_HEAD} {SHA_BASE}", "") if args[:1] == ["api"] else (0, "", "")

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


def make_git():
    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            os.makedirs(args[-1], exist_ok=True)
        return (0, "lib/cache.ts\n", "") if "diff" in args else (0, "", "")

    return run_git


def seed(state: Path, claimed: dict[str, str], findings: list[dict] = ()):
    """clawpatch's own state for a pass: a run claiming features, each feature's status, findings."""
    (state / "runs").mkdir(parents=True, exist_ok=True)
    (state / "features").mkdir(parents=True, exist_ok=True)
    (state / "findings").mkdir(parents=True, exist_ok=True)
    (state / "runs" / "20260930T000000-aaaaaa.json").write_text(
        json.dumps({"status": "running", "claimedFeatureIds": list(claimed)})
    )
    for fid, status in claimed.items():
        (state / "features" / f"{fid}.json").write_text(json.dumps({"featureId": fid, "status": status}))
    for i, f in enumerate(findings):
        (state / "findings" / f"f{i}.json").write_text(json.dumps(f))


def scripted(steps, calls, claimed=None, findings=()):
    """A fake clawpatch: `steps` is one {rc, stderr, timed_out} per call; each call seeds `claimed`."""
    it = iter(steps)

    async def run(args, cwd, env, budget_s):
        state = Path(args[args.index("--state-dir") + 1])
        calls.append(state)
        step = next(it, {})
        if claimed is not None:
            seed(state, claimed, findings)
        return step.get("rc", 0), "{}", step.get("stderr", ""), step.get("timed_out", False)

    return run


def runner(tmp_path, run, cfg=None):
    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    return ProtoPatchRunner({**base, **(cfg or {})}, run_git=make_git(), run_clawpatch=run)


def findings_in(text):
    return json.loads(text.split("```json\n", 1)[1].rsplit("```", 1)[0])


# 33 of 36 reviewed when the budget kill landed — the 09-30 shape, scaled down.
SOME_DONE = {"f1": "reviewed", "f2": "needs-fix", "f3": "reviewed", "f4": "claimed", "f5": "pending", "f6": "error"}


# -- reading how far a pass got ---------------------------------------------------------------------


def test_pass_coverage_counts_only_features_whose_review_finished(tmp_path):
    seed(tmp_path, SOME_DONE)
    assert pass_coverage(tmp_path) == (3, 6)  # reviewed + needs-fix; not claimed / pending / error


def test_pass_coverage_uses_the_latest_run_that_claimed_features(tmp_path):
    seed(tmp_path, {"old": "reviewed"})
    (tmp_path / "runs" / "20260930T000500-bbbbbb.json").write_text(
        json.dumps({"status": "running", "claimedFeatureIds": []})
    )
    (tmp_path / "runs" / "20260930T000900-cccccc.json").write_text(
        json.dumps({"claimedFeatureIds": ["a", "b"]})  # a retry's run: a LATER run that claimed more
    )
    for fid, st in (("a", "reviewed"), ("b", "claimed")):
        (tmp_path / "features" / f"{fid}.json").write_text(json.dumps({"status": st}))
    assert pass_coverage(tmp_path) == (1, 2)


@pytest.mark.parametrize("why", ["no state dir", "no runs", "run claimed nothing", "corrupt run"])
def test_pass_coverage_is_none_when_nothing_readable_was_claimed(tmp_path, why):
    if why == "no runs":
        (tmp_path / "runs").mkdir()
    if why == "run claimed nothing":
        seed(tmp_path, {})
    if why == "corrupt run":
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "x.json").write_text("{not json")
    assert pass_coverage(tmp_path / ("missing" if why == "no state dir" else ".")) is None


def test_pass_coverage_skips_a_half_written_feature_file(tmp_path):
    """A SIGKILL can leave a feature record half-written: it is skipped, never an exception."""
    seed(tmp_path, {"a": "reviewed", "b": "reviewed", "c": "reviewed"})
    (tmp_path / "features" / "b.json").write_text('{"status": "revie')
    (tmp_path / "features" / "c.json").unlink()
    assert pass_coverage(tmp_path) == (1, 3)


# -- a budget kill with features finished -----------------------------------------------------------


async def test_a_budget_kill_with_finished_features_keeps_their_findings(tmp_path):
    calls: list = []
    run = scripted([{"timed_out": True, "rc": -9}], calls, SOME_DONE, [rec("Race in prune().")])
    out = await runner(tmp_path, run, {"time_budget_s": 1500}).review(7, "octo/repo")

    assert out.startswith(PARTIAL_PREFIX) and not out.startswith(UNAVAILABLE_PREFIX)
    assert [f["claim"] for f in findings_in(out)] == ["Race in prune()."]  # the finished features' finding survived
    assert "3 of 6 features reviewed" in out.splitlines()[0]
    assert "timed out after 1500s (budget exceeded; findings from the finished features kept)" in out
    assert (
        f"{GAP_PARTIAL_PREFIX} — 3 of 6 features reviewed — timed out after 1500s" in out
    )  # the line the relay must state
    assert out.count(PARTIAL_HEADER) == 1  # the third marker rides in the run header
    assert "1 reportable finding(s) from the features that finished" in out
    # ...and it reads as what it is: a gap, classified as the budget kill, with the coverage in front.
    assert any(m in out for m in STRUCTURAL_GAP_MARKERS) and is_partial_output(out)
    assert outage_reason(out).startswith("3 of 6 features reviewed — timed out after 1500s")
    assert classify_outage(outage_reason(out)) == "budget-timeout"


async def test_a_partial_result_with_no_findings_still_says_how_far_it_got(tmp_path):
    calls: list = []
    run = scripted([{"timed_out": True, "rc": -9}], calls, SOME_DONE, [])  # 3 features finished, all clean
    out = await runner(tmp_path, run).review(7, "octo/repo")
    assert out.startswith(PARTIAL_PREFIX) and findings_in(out) == []
    assert "3 of 6 features reviewed" in out  # a clean-but-incomplete pass is information, not an outage


async def test_partial_findings_are_still_confined_to_the_prs_files(tmp_path):
    calls: list = []
    run = scripted(
        [{"timed_out": True, "rc": -9}],
        calls,
        SOME_DONE,
        [rec("In this PR."), rec("Somewhere else.", path="docs/other.md")],
    )
    out = await runner(tmp_path, run).review(7, "octo/repo")
    assert [f["claim"] for f in findings_in(out)] == ["In this PR."]  # same confinement as a complete pass


# -- a non-zero exit with features finished ---------------------------------------------------------


async def test_a_provider_failure_after_some_features_finished_is_partial_not_an_outage(tmp_path):
    calls: list = []
    run = scripted(
        [{"rc": 4, "stderr": "gateway review: empty choices[0].message.content in response (finish_reason=stop)"}],
        calls,
        SOME_DONE,
        [rec("Race in prune().")],
    )
    out = await runner(tmp_path, run).review(7, "octo/repo")
    assert out.startswith(PARTIAL_PREFIX)
    assert "clawpatch exit 4" in out and "empty choices" in out  # the original reason is kept
    assert [f["claim"] for f in findings_in(out)] == ["Race in prune()."]
    assert classify_outage(outage_reason(out)) == "exit-4:provider"  # the coverage lead does not hide the class


async def test_the_retry_path_ends_partial_too(tmp_path):
    """A transient failure is retried once; if the retry is then cut short, what finished is kept."""
    calls: list = []
    run = scripted([{"rc": 4, "stderr": FETCH_FAILED}, {"timed_out": True, "rc": -9}], calls, SOME_DONE, [rec("A.")])
    out = await runner(tmp_path, run, {"time_budget_s": 600}).review(7, "octo/repo")
    assert len(calls) == 2  # it really was retried
    assert out.startswith(PARTIAL_PREFIX) and "(after 2 attempts)" in outage_reason(out) + out


# -- nothing finished: the outage it always was -----------------------------------------------------


@pytest.mark.parametrize(
    "statuses",
    [{"a": "error", "b": "error"}, {"a": "claimed", "b": "pending"}, {}],
    ids=["every feature errored", "nothing finished", "nothing claimed"],
)
async def test_with_no_finished_feature_it_is_still_an_outage(tmp_path, statuses):
    calls: list = []
    run = scripted(
        [{"rc": 4, "stderr": "gateway review: HTTP 401 Unauthorized — bad key"}], calls, statuses, [rec("Stale.")]
    )
    out = await runner(tmp_path, run).review(7, "octo/repo")
    assert out.startswith(UNAVAILABLE_PREFIX) and not is_partial_output(out)
    assert "Stale." not in out  # nothing unreviewed is ever reported as a finding


async def test_with_no_state_at_all_it_is_still_an_outage(tmp_path):
    calls: list = []
    out = await runner(tmp_path, scripted([{"timed_out": True, "rc": -9}], calls)).review(7, "octo/repo")
    assert out.startswith(UNAVAILABLE_PREFIX)
    assert "review proceeds without it" in out  # the unchanged outage wording


async def test_a_failure_while_salvaging_degrades_to_the_outage_never_raises(tmp_path, monkeypatch):
    def boom(_):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(pp, "pass_coverage", boom)
    calls: list = []
    out = await runner(tmp_path, scripted([{"timed_out": True, "rc": -9}], calls, SOME_DONE)).review(7, "octo/repo")
    assert out.startswith(UNAVAILABLE_PREFIX)  # the safe answer, not an exception out of review()


# -- unchanged paths ----------------------------------------------------------------------------------


async def test_a_complete_pass_is_untouched(tmp_path):
    calls: list = []
    out = await runner(tmp_path, scripted([{"rc": 0}], calls, {"f1": "reviewed"}, [rec("Race in prune().")])).review(
        7, "octo/repo"
    )
    assert not out.startswith((PARTIAL_PREFIX, UNAVAILABLE_PREFIX)) and not is_partial_output(out)
    assert out.startswith("protoPatch structural pass on octo/repo#7 — head ")
    assert not any(m in out for m in STRUCTURAL_GAP_MARKERS)  # nothing that could read as a gap


async def test_a_missing_binary_is_still_an_outage_even_with_stale_state(tmp_path):
    calls: list = []
    out = await runner(tmp_path, scripted([{"rc": 127}], calls, SOME_DONE, [rec("Race.")])).review(7, "octo/repo")
    assert out.startswith(UNAVAILABLE_PREFIX) and "is not installed" in out


# -- the scratch dir --------------------------------------------------------------------------------


async def test_a_partial_pass_keeps_its_scratch_dir_for_a_postmortem_a_complete_one_drops_it(tmp_path):
    calls: list = []
    r = runner(tmp_path, scripted([{"timed_out": True, "rc": -9}, {"rc": 0}], calls, SOME_DONE, [rec("A.")]))
    await r.review(7, "octo/repo")  # partial
    await r.review(8, "octo/repo")  # complete
    assert calls[0].is_dir() and (calls[0] / "findings").is_dir()  # kept: it is evidence of how far the pass got
    assert not calls[1].exists()  # a pass that finished has nothing left to inspect

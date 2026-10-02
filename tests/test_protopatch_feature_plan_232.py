"""The plugin plans each structural pass's features instead of `clawpatch ci --since` (#232).

`ci --since <base>` reviews every feature that owns a changed file OR lists one as context, and
clawpatch's Python mapper lists `pyproject.toml` as context of every Python feature. protoAgent#4003
(~150 changed lines, 14 files) therefore planned 276 of 361 features — 271 of them only because
`pyproject.toml` changed — and timed out at the 1500 s budget on all four heads.

Now the plugin runs `init` + `map`, picks the features itself and runs `review --feature-list`:
lockfiles / generated files / dependency manifests never pull a feature in as context, the rest are
ranked by changed lines, at most `structural_max_features` are reviewed, and every feature left out
by the cap is a COVERAGE GAP (a partial pass, "N of M"), never a silent drop. A `structural_plan`
telemetry event records the plan and how each feature ended.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import (
    DEFAULT_MAX_FEATURES,
    FEATURE_CAP_REASON,
    PARTIAL_PREFIX,
    PLAN_FILENAME,
    STRUCTURAL_GAP_MARKERS,
    UNAVAILABLE_PREFIX,
    ProtoPatchRunner,
    classify_outage,
    feature_outcomes,
    is_low_signal,
    is_partial_output,
    outage_reason,
    parse_numstat,
    plan_features,
    structural_max_features,
)

SHA_HEAD = "a" * 40
SHA_BASE = "b" * 40
FETCH_FAILED = "gateway review: request failed (fetch failed)"


# ── pure pieces ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "web/yarn.lock",
        "THIRD_PARTY_LICENSES.md",
        "changelog.d/3950.fixed.md",
        "pyproject.toml",
        "packs/necromunda/Cargo.toml",
        "requirements-core.txt",
        "requirements.txt",
        "go.sum",
        "ui/package.json",
    ],
)
def test_lockfiles_generated_files_and_manifests_are_low_signal(path):
    assert is_low_signal(path)


@pytest.mark.parametrize(
    "path",
    [
        "a2a_impl/executor.py",
        "tests/test_a2a_handler.py",
        "docs/explanation/a2a-protocol.md",
        "lockfile.py",
        "pyproject.py",
    ],
)
def test_source_tests_and_docs_are_not(path):
    assert not is_low_signal(path)


def test_numstat_z_output_keeps_unusual_paths_verbatim():
    out = "3\t1\tdocs/caf\u00e9 notes.md\0" + "1\t0\tsrc/a.py\0"
    assert parse_numstat(out) == {"docs/caf\u00e9 notes.md": 4, "src/a.py": 1}


def test_numstat_counts_added_plus_deleted_and_a_binary_as_one():
    out = "9\t3\ta2a_impl/executor.py\n-\t-\tdocs/logo.png\n1\t1\tpyproject.toml\n\n"
    assert parse_numstat(out) == {"a2a_impl/executor.py": 12, "docs/logo.png": 1, "pyproject.toml": 2}


@pytest.mark.parametrize(
    "value, expected",
    [(None, DEFAULT_MAX_FEATURES), ("", DEFAULT_MAX_FEATURES), (8, 8), ("12", 12), (0, None), ("0", None)],
)
def test_the_cap_setting(value, expected):
    assert structural_max_features(value) == expected


@pytest.mark.parametrize("value", [-1, "many", 2.5, True])
def test_a_mistyped_cap_keeps_the_default_never_lifts_it(value):
    assert structural_max_features(value) == DEFAULT_MAX_FEATURES


def feature(fid, owned=(), context=()):
    return {
        "featureId": fid,
        "title": fid,
        "ownedFiles": [{"path": p, "reason": "x"} for p in owned],
        "contextFiles": [{"path": p, "reason": "x"} for p in context],
    }


def pr4003_shape(n_context_only=30):
    """protoAgent#4003 in miniature: a few features own the changed code, one owns pyproject.toml, and
    every other Python feature lists pyproject.toml (and nothing else changed) as context."""
    features = [
        feature("feat_executor", owned=["a2a_impl/executor.py"], context=["pyproject.toml"]),
        feature("feat_hitl", owned=["a2a_impl/hitl_routing.py"], context=["pyproject.toml"]),
        feature("feat_tests", owned=["tests/test_a2a_hitl_parked_routing.py"], context=["pyproject.toml"]),
        feature("feat_pyproject", owned=["pyproject.toml"]),
    ]
    features += [
        feature(f"feat_other_{i:02d}", owned=[f"other/m{i}.py"], context=["pyproject.toml"])
        for i in range(n_context_only)
    ]
    lines = {
        "a2a_impl/executor.py": 9,
        "a2a_impl/hitl_routing.py": 14,
        "tests/test_a2a_hitl_parked_routing.py": 38,
        "pyproject.toml": 2,
        "uv.lock": 8,
        "THIRD_PARTY_LICENSES.md": 4,
    }
    return features, lines


def test_a_version_bump_no_longer_pulls_in_every_feature_through_pyproject():
    features, lines = pr4003_shape()
    plan = plan_features(features, lines, cap=None)
    assert [f["id"] for f in plan.selected] == ["feat_tests", "feat_hitl", "feat_executor", "feat_pyproject"]
    assert plan.low_signal_only == 30  # counted and reported, not silently gone
    assert plan.low_signal_files == ["pyproject.toml"]
    assert plan.mapped == 34 and plan.dropped == 0


def test_features_are_ranked_by_changed_lines_they_own_then_by_context():
    features = [
        feature("feat_ctx", owned=["a.py"], context=["shared.py"]),
        feature("feat_small", owned=["b.py"]),
        feature("feat_big", owned=["c.py"]),
        feature("feat_ctx_only", owned=["d.py"], context=["shared.py"]),
    ]
    plan = plan_features(features, {"a.py": 1, "b.py": 3, "c.py": 40, "shared.py": 20}, cap=None)
    # a feature whose only link is a changed SOURCE file in its context is still eligible, ranked last
    assert [f["id"] for f in plan.selected] == ["feat_big", "feat_small", "feat_ctx", "feat_ctx_only"]
    assert plan.selected[2] == {"id": "feat_ctx", "owned_lines": 1, "context_lines": 20, "files": 2}


def test_the_cap_keeps_the_top_features_and_counts_the_rest():
    features = [feature(f"feat_{i:02d}", owned=[f"m{i}.py"]) for i in range(10)]
    plan = plan_features(features, {f"m{i}.py": i + 1 for i in range(10)}, cap=3)
    assert [f["id"] for f in plan.selected] == ["feat_09", "feat_08", "feat_07"]
    assert len(plan.eligible) == 10 and plan.dropped == 7
    assert "3 of 10 eligible feature(s)" in plan.summary() and "over the per-pass cap of 3" in plan.summary()


def test_feature_outcomes_tell_finished_in_flight_and_never_started_apart(tmp_path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "feat_a.json").write_text(
        json.dumps(
            {
                "status": "reviewed",
                "analysisHistory": [
                    {"summary": "0 finding(s); prompt=81489 bytes; approxTokens=20373; includedFiles=8"}
                ],
            }
        )
    )
    stderr = (
        "clawpatch review start run=r features=4 jobs=2\n"
        "clawpatch review feature-start index=1 total=4 feature=feat_a title=A\n"
        "clawpatch review feature-start index=2 total=4 feature=feat_b title=B\n"
        "clawpatch review feature-done index=1 total=4 feature=feat_a findings=0 elapsed=212s\n"
        "clawpatch review feature-start index=3 total=4 feature=feat_c title=C\n"
        "clawpatch review feature-error index=3 total=4 feature=feat_c elapsed=31s error=boom\n"
    )
    rows = {r["id"]: r for r in feature_outcomes(stderr, tmp_path, ["feat_a", "feat_b", "feat_c", "feat_d"])}
    assert rows["feat_a"] == {
        "id": "feat_a",
        "status": "finished",
        "elapsed_s": 212,
        "prompt_bytes": 81489,
        "approx_tokens": 20373,
    }
    assert rows["feat_b"]["status"] == "killed"  # in flight when the pass ended: what a hang looks like
    assert rows["feat_c"] == {"id": "feat_c", "status": "error", "elapsed_s": 31}
    assert rows["feat_d"] == {"id": "feat_d", "status": "not-started"}  # what over-planning looks like


def test_feature_outcomes_never_raise_on_garbage(tmp_path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "feat_a.json").write_text("{half")
    assert feature_outcomes("clawpatch review feature-done feature=feat_a elapsed=xs\n\x00", tmp_path, ["feat_a"]) == [
        {"id": "feat_a", "status": "finished"}
    ]


# ── the runner ─────────────────────────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    monkeypatch.delenv("CLAWPATCH_GATEWAY_TIMEOUT_MS", raising=False)

    async def fake_run_gh(args, timeout=30):
        return (0, f"{SHA_HEAD} {SHA_BASE}", "") if args[:1] == ["api"] else (0, "", "")

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


def make_git(lines: dict[str, int]):
    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            os.makedirs(args[-1], exist_ok=True)
        if "--numstat" in args:
            return 0, "".join(f"{n}\t0\t{p}\n" for p, n in lines.items()), ""
        if "diff" in args:
            return 0, "".join(f"{p}\n" for p in lines), ""
        return 0, "", ""

    return run_git


def finding(fid, path):
    return {
        "title": f"{fid} bug",
        "category": "correctness",
        "severity": "high",
        "confidence": "high",
        "evidence": [{"path": path, "startLine": 3, "quote": "x"}],
        "reasoning": "r",
        "recommendation": "fix",
        "status": "open",
        "signature": fid,
    }


class FakeClawpatch:
    """`map` writes the given features; `review --feature-list` reviews the listed ones (all, or
    `finish` of them) and writes their findings — the on-disk layout the real binary leaves."""

    def __init__(self, features, *, finish=None, review_script=(), map_rc=0):
        self.features = features
        self.finish = finish
        self.review_script = list(review_script)
        self.map_rc = map_rc
        self.calls: list[list[str]] = []
        self.listed: list[str] = []

    async def __call__(self, args, cwd, env, budget_s):
        self.calls.append(list(args))
        state = Path(args[args.index("--state-dir") + 1])
        if "init" in args:
            return 0, "{}", "", False
        if "map" in args:
            if self.map_rc:
                return self.map_rc, "", "boom", False
            (state / "features").mkdir(parents=True, exist_ok=True)
            for f in self.features:
                (state / "features" / f"{f['featureId']}.json").write_text(json.dumps({**f, "status": "pending"}))
            return 0, "{}", "", False
        assert args[1] == "review", args
        self.listed = Path(args[args.index("--feature-list") + 1]).read_text().split()
        (state / "runs").mkdir(exist_ok=True)
        (state / "findings").mkdir(exist_ok=True)
        (state / "runs" / "20261002T000000-aaaaaa.json").write_text(json.dumps({"claimedFeatureIds": self.listed}))
        done = self.listed if self.finish is None else self.listed[: self.finish]
        stderr = ""
        by_id = {f["featureId"]: f for f in self.features}
        for fid in self.listed:
            stderr += f"clawpatch review feature-start index=1 total=1 feature={fid} title=t\n"
        for fid in done:
            rec = by_id[fid]
            (state / "features" / f"{fid}.json").write_text(json.dumps({**rec, "status": "reviewed"}))
            (state / "findings" / f"{fid}.json").write_text(json.dumps(finding(fid, rec["ownedFiles"][0]["path"])))
            stderr += f"clawpatch review feature-done index=1 total=1 feature={fid} findings=1 elapsed=90s\n"
        if self.review_script:
            step = self.review_script.pop(0)
            return step.get("rc", 0), "", step.get("stderr", stderr), step.get("timed_out", False)
        return 0, "{}", stderr, False


def runner(tmp_path, claw, lines, cfg=None, telemetry=None):
    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    return ProtoPatchRunner(
        {**base, **(cfg or {})}, run_git=make_git(lines), run_clawpatch=claw, telemetry=telemetry or Recorder()
    )


def fenced(out):
    return json.loads(out.split("```json\n", 1)[1].rsplit("```", 1)[0])


async def test_the_plan_runs_review_with_a_feature_list_not_ci_since(tmp_path):
    features, lines = pr4003_shape()
    claw = FakeClawpatch(features)
    tele = Recorder()
    out = await runner(tmp_path, claw, lines, cfg={"model": "protolabs/smart"}, telemetry=tele).review(4003, "o/r")

    init, mapped, review = claw.calls
    assert init[1:] == ["--state-dir", init[2], "--json", "-q", "init"] and mapped[-1] == "map"
    assert review[:5] == ["clawpatch", "review", "--provider", "gateway", "--json"]
    assert "--since" not in review and "ci" not in review
    assert ["--jobs", "4"] == review[review.index("--jobs") :][:2]
    assert ["--model", "protolabs/smart"] == review[-2:]
    assert Path(review[review.index("--feature-list") + 1]).name == PLAN_FILENAME
    assert claw.listed == ["feat_tests", "feat_hitl", "feat_executor", "feat_pyproject"]  # not 34

    # A complete pass — nothing was capped — with the plan stated in the header.
    assert not any(m in out for m in STRUCTURAL_GAP_MARKERS)
    assert "plan: 4 of 4 eligible feature(s) of 34 mapped" in out
    assert "30 more depend on the diff only through a lockfile or dependency manifest" in out
    assert len(fenced(out)) == 4  # one per reviewed feature; pyproject's own cites pyproject.toml, a changed file
    [(event, row)] = tele.events
    assert event == "structural_plan" and row["outcome"] == "complete" and row["reason"] is None
    assert row["planner"] == "plugin" and (row["mapped"], row["eligible"], row["selected"]) == (34, 4, 4)
    assert row["low_signal_only"] == 30 and row["finished"] == 4 and row["sha"] == SHA_HEAD
    assert row["features"][0] == {
        "id": "feat_tests",
        "owned_lines": 38,
        "context_lines": 0,
        "files": 2,
        "status": "finished",
        "elapsed_s": 90,
    }


async def test_a_capped_pass_is_partial_never_a_complete_clean_pass(tmp_path):
    features = [feature(f"feat_{i:02d}", owned=[f"m{i}.py"]) for i in range(10)]
    lines = {f"m{i}.py": i + 1 for i in range(10)}
    claw = FakeClawpatch(features)
    tele = Recorder()
    out = await runner(tmp_path, claw, lines, cfg={"structural_max_features": 3}, telemetry=tele).review(1, "o/r")

    assert claw.listed == ["feat_09", "feat_08", "feat_07"]
    assert out.startswith(PARTIAL_PREFIX) and is_partial_output(out)
    assert "3 of 10 features reviewed" in out.splitlines()[0]
    assert f"Gap: structural pass partial — 3 of 10 features reviewed — {FEATURE_CAP_REASON}" in out
    assert classify_outage(outage_reason(out)) == "feature-cap"
    assert len(fenced(out)) == 3  # the reviewed features' findings still flow
    [(_, row)] = tele.events
    assert row["outcome"] == "partial" and row["reason"] == "feature-cap" and row["dropped"] == 7


async def test_no_cap_means_every_eligible_feature(tmp_path):
    features = [feature(f"feat_{i:02d}", owned=[f"m{i}.py"]) for i in range(20)]
    claw = FakeClawpatch(features)
    out = await runner(tmp_path, claw, {f"m{i}.py": 1 for i in range(20)}, cfg={"structural_max_features": 0}).review(
        1, "o/r"
    )
    assert len(claw.listed) == 20 and not any(m in out for m in STRUCTURAL_GAP_MARKERS)


async def test_nothing_but_lockfile_and_manifest_context_means_nothing_to_review(tmp_path):
    features = [feature(f"feat_{i}", owned=[f"m{i}.py"], context=["pyproject.toml"]) for i in range(5)]
    claw = FakeClawpatch(features)
    tele = Recorder()
    out = await runner(tmp_path, claw, {"uv.lock": 40, "pyproject.toml": 2}, telemetry=tele).review(1, "o/r")
    assert [c[-1] for c in claw.calls] == ["init", "map"]  # no review run at all
    assert fenced(out) == [] and not any(m in out for m in STRUCTURAL_GAP_MARKERS)
    assert "0 of 0 eligible" in out and "5 more depend on the diff only through a lockfile" in out
    [(_, row)] = tele.events
    assert row["selected"] == 0 and row["low_signal_only"] == 5 and row["outcome"] == "complete"


async def test_a_budget_kill_counts_capped_features_in_the_denominator(tmp_path):
    features = [feature(f"feat_{i:02d}", owned=[f"m{i}.py"]) for i in range(10)]
    claw = FakeClawpatch(features, finish=2, review_script=[{"timed_out": True, "rc": 124}])
    tele = Recorder()
    out = await runner(
        tmp_path, claw, {f"m{i}.py": i + 1 for i in range(10)}, cfg={"structural_max_features": 4}, telemetry=tele
    ).review(1, "o/r")
    assert out.startswith(PARTIAL_PREFIX)
    assert "2 of 10 features reviewed" in out.splitlines()[0]  # 2 finished of 10 eligible, not "2 of 4"
    assert classify_outage(outage_reason(out)) == "budget-timeout"
    [(_, row)] = tele.events
    statuses = [f["status"] for f in row["features"]]
    assert statuses == ["finished", "finished", "killed", "killed"] and row["finished"] == 2


async def test_a_budget_kill_with_nothing_finished_is_still_the_outage(tmp_path):
    features = [feature(f"feat_{i}", owned=[f"m{i}.py"]) for i in range(3)]
    claw = FakeClawpatch(features, finish=0, review_script=[{"timed_out": True, "rc": 124}])
    out = await runner(tmp_path, claw, {f"m{i}.py": 1 for i in range(3)}).review(1, "o/r")
    assert out.startswith(UNAVAILABLE_PREFIX)


async def test_the_transient_retry_keeps_the_feature_list(tmp_path):
    features = [feature(f"feat_{i}", owned=[f"m{i}.py"]) for i in range(3)]
    claw = FakeClawpatch(features, review_script=[{"rc": 4, "stderr": FETCH_FAILED}])
    out = await runner(tmp_path, claw, {f"m{i}.py": 1 for i in range(3)}).review(1, "o/r")
    reviews = [c for c in claw.calls if c[1] == "review"]
    assert len(reviews) == 2 and all("--feature-list" in c and "--since" not in c for c in reviews)
    assert not any(m in out for m in STRUCTURAL_GAP_MARKERS)


@pytest.mark.parametrize("map_rc", [1, 7])
async def test_a_failed_map_falls_back_to_ci_since(tmp_path, map_rc):
    claw = FakeClawpatch([], map_rc=map_rc)
    tele = Recorder()

    async def run(args, cwd, env, budget_s):
        if args[1] == "ci":
            claw.calls.append(list(args))
            return 0, "{}", "", False
        return await claw(args, cwd, env, budget_s)

    r = runner(tmp_path, run, {"m.py": 1}, telemetry=tele)
    out = await r.review(1, "o/r")
    assert claw.calls[-1][:2] == ["clawpatch", "ci"] and ["--since", SHA_BASE] == claw.calls[-1][-4:-2]
    assert not out.startswith(UNAVAILABLE_PREFIX)
    [(_, row)] = tele.events
    assert row["planner"] == "since"


async def test_an_empty_map_falls_back_to_ci_since(tmp_path):
    claw = FakeClawpatch([])
    calls = []

    async def run(args, cwd, env, budget_s):
        calls.append(args)
        return (0, "{}", "", False) if args[1] == "ci" else await claw(args, cwd, env, budget_s)

    await runner(tmp_path, run, {"m.py": 1}).review(1, "o/r")
    assert calls[-1][1] == "ci"


async def test_a_numstat_that_missed_files_never_plans_them_away(tmp_path):
    """The name-only diff lists `m1.py`; numstat (here: empty) does not. The plan must still review it."""
    features = [feature("feat_1", owned=["m1.py"])]
    claw = FakeClawpatch(features)

    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            os.makedirs(args[-1], exist_ok=True)
        if "--numstat" in args:
            return 0, "", ""
        return (0, "m1.py\n", "") if "diff" in args else (0, "", "")

    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    await ProtoPatchRunner(base, run_git=run_git, run_clawpatch=claw).review(1, "o/r")
    assert claw.listed == ["feat_1"]


@pytest.mark.parametrize("value", [False, "false", "off", "0"])
async def test_structural_plan_false_is_the_ci_since_rollback(tmp_path, value):
    calls = []

    async def run(args, cwd, env, budget_s):
        calls.append(args)
        return 0, "{}", "", False

    await runner(tmp_path, run, {"m.py": 1}, cfg={"structural_plan": value}).review(1, "o/r")
    assert [c[1] for c in calls] == ["ci"]


async def test_telemetry_that_raises_never_breaks_the_pass(tmp_path):
    class Broken:
        def emit(self, *a, **k):
            raise RuntimeError("disk full")

    features = [feature("feat_1", owned=["m1.py"])]
    out = await runner(tmp_path, FakeClawpatch(features), {"m1.py": 3}, telemetry=Broken()).review(1, "o/r")
    assert len(fenced(out)) == 1


async def test_an_engine_without_feature_list_falls_back_to_ci_since(tmp_path):
    """protoPatch < 0.7.0 rejects `--feature-list` with exit 2 (`unknown arg`): run the old way, don't lose the pass."""
    features = [feature("feat_1", owned=["m1.py"])]
    claw = FakeClawpatch(features)
    tele = Recorder()

    async def run(args, cwd, env, budget_s):
        if args[1] == "review":
            claw.calls.append(list(args))
            return 2, "", "Error: unknown arg: --feature-list", False
        if args[1] == "ci":
            claw.calls.append(list(args))
            return 0, "{}", "", False
        return await claw(args, cwd, env, budget_s)

    out = await runner(tmp_path, run, {"m1.py": 3}, telemetry=tele).review(1, "o/r")
    assert [c[1] for c in claw.calls[-2:]] == ["review", "ci"]
    assert claw.calls[-1][-4:] == ["--since", SHA_BASE, "--jobs", "4"]
    assert not out.startswith(UNAVAILABLE_PREFIX) and "plan:" not in out
    [(_, row)] = tele.events
    assert row["planner"] == "since" and row["feature_list_unsupported"] is True and row["attempts"] == 1


async def test_an_unknown_feature_record_shape_falls_back_to_ci_since(tmp_path):
    """A future clawpatch that renamed `ownedFiles` must not read as "no feature touches the diff"."""
    calls = []
    claw = FakeClawpatch([{"featureId": "feat_1", "owns": ["m1.py"]}])

    async def run(args, cwd, env, budget_s):
        calls.append(args)
        return (0, "{}", "", False) if args[1] == "ci" else await claw(args, cwd, env, budget_s)

    await runner(tmp_path, run, {"m1.py": 3}).review(1, "o/r")
    assert calls[-1][1] == "ci"


# ── the planned path keeps #236 (scoping) and #240 (lint check) ──────────────────────────────


def scoped_git(lines: dict[str, int], hunks: dict[str, tuple[int, int]]):
    """Numstat / name-only as `make_git`, plus a real-shaped `--unified=0` diff for `_changed_ranges`."""
    base = make_git(lines)

    async def run_git(args, timeout_s=180):
        if "--unified=0" in args:
            out = "".join(f"+++ b/{p}\n@@ -{s},{n} +{s},{n} @@\n" for p, (s, n) in hunks.items())
            return 0, out, ""
        return await base(args, timeout_s)

    return run_git


def cross_location(fid, untouched, changed_path, changed_line, title=None):
    return {
        "title": title or f"{fid}: this change breaks that caller",
        "category": "correctness",
        "severity": "high",
        "confidence": "high",
        "evidence": [
            {"path": untouched, "startLine": 90, "quote": "caller()"},
            {"path": changed_path, "startLine": changed_line, "quote": "changed()"},
        ],
        "reasoning": "r",
        "recommendation": "fix",
        "status": "open",
        "signature": fid,
    }


@pytest.mark.parametrize("shape", ["capped", "cut-short"])
async def test_partial_planned_passes_still_anchor_findings_at_the_changed_lines(tmp_path, shape):
    """#236: a finding is anchored at the evidence location the PR changed, not its first one. A plan
    pass that comes back partial (capped, or killed at the budget) goes through the same read."""
    features = [feature(f"feat_{i}", owned=[f"m{i}.py", "lib/caller.py"]) for i in range(3)]
    script = [{"timed_out": True, "rc": 124}] if shape == "cut-short" else []
    claw = FakeClawpatch(features, finish=1 if shape == "cut-short" else None, review_script=script)
    real_write = FakeClawpatch.__call__

    async def run(args, cwd, env, budget_s):
        result = await real_write(claw, args, cwd, env, budget_s)
        if args[1] == "review":  # replace the stock findings with cross-location ones
            state = Path(args[args.index("--state-dir") + 1]) / "findings"
            for f in state.glob("*.json"):
                fid = f.stem
                f.write_text(json.dumps(cross_location(fid, "lib/caller.py", f"m{fid[-1]}.py", 12)))
        return result

    lines = {f"m{i}.py": 10 - i for i in range(3)} | {"lib/caller.py": 1}
    hunks = {f"m{i}.py": (10, 5) for i in range(3)}  # caller.py's change is elsewhere (line 1)
    hunks["lib/caller.py"] = (1, 1)
    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    cfg = {**base, "structural_max_features": 2 if shape == "capped" else 0}
    out = await ProtoPatchRunner(cfg, run_git=scoped_git(lines, hunks), run_clawpatch=run).review(1, "o/r")
    assert out.startswith(PARTIAL_PREFIX), out[:200]
    found = fenced(out)
    assert found and all(f["file"].startswith("m") and f["line"] == 12 for f in found)  # not lib/caller.py:90


async def test_a_capped_planned_pass_still_drops_a_lint_claim_the_pinned_ruff_refutes(tmp_path):
    """#240: the lint check runs on what a planned pass found, partial or not."""
    from tests.test_lint_claims_232 import CHECKS_YML, F841_CLAIM, TEST_SRC, FakeTools

    path = "tests/test_fs_missing_root_3643.py"
    features = [feature("feat_t", owned=[path]), feature("feat_other", owned=["m.py"])]
    claw = FakeClawpatch(features)

    async def run(args, cwd, env, budget_s):
        result = await FakeClawpatch.__call__(claw, args, cwd, env, budget_s)
        if args[-1] == "init":  # the plan step: `clawpatch --state-dir … init`
            (Path(cwd) / "tests").mkdir(parents=True, exist_ok=True)
            (Path(cwd) / path).write_text(TEST_SRC)
            (Path(cwd) / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
            (Path(cwd) / ".github" / "workflows" / "checks.yml").write_text(CHECKS_YML)
        if args[1] == "review":
            rec = finding("feat_t", path) | {"title": F841_CLAIM, "category": "maintainability"}
            rec["evidence"] = [{"path": path, "startLine": 2, "quote": "cfg, a, b = two_projects"}]
            (Path(args[args.index("--state-dir") + 1]) / "findings" / "feat_t.json").write_text(json.dumps(rec))
        return result

    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    cfg = {**base, "structural_max_features": 1, "lint_tools_dir": str(tmp_path / "tools")}
    r = ProtoPatchRunner(cfg, run_git=make_git({path: 5, "m.py": 1}), run_clawpatch=run, run_lint=FakeTools([]))
    out = await r.review(4017, "o/r")
    assert claw.listed == ["feat_t"]
    assert out.startswith(PARTIAL_PREFIX) and classify_outage(outage_reason(out)) == "feature-cap"
    assert fenced(out) == []  # the F841 claim was refuted and dropped, before the relay
    assert "1 lint claim(s) refuted by the repo's pinned ruff 0.15.10" in out

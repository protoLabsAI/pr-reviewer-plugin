"""Each structural pass gets its OWN clawpatch state dir (#223).

The runner used to give every review of a repo the same state dir. Two PRs of one repo reviewed
at once then claimed overlapping features under the same locks, and the second failed its claims
with `exit 7` (`feature locked`) — its structural lane `unavailable`, its verdict capped at WARN —
with nothing stale. The shared dir had two more faults: findings from one PR surfaced on another
that touched the same file, and a redeploy that killed a run left its locks for good (#221).

The working state is now per review, under the repo's persistent dir; only what SHOULD persist
(refuted claims, provider-failure captures) stays in the repo dir.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import SCRATCH_DIRNAME, SCRATCH_KEEP_FAILED_S, ProtoPatchRunner

SHA = {n: chr(ord("a") + n) * 40 for n in range(6)}
SHA_BASE = "b" * 40

RECORD = {
    "title": "Race in prune().",
    "category": "concurrency",
    "severity": "critical",
    "confidence": "high",
    "evidence": [{"path": "lib/cache.ts", "startLine": 42, "quote": "rmSync(...)"}],
    "reasoning": "r",
    "recommendation": "lock",
    "status": "open",
    "signature": "sig-1",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")

    async def fake_run_gh(args, timeout=30):
        if args[:1] == ["api"]:
            # Each PR number maps to its own head, so concurrent reviews are different heads.
            n = int(args[1].rsplit("/", 1)[-1]) % len(SHA)
            return 0, f"{SHA[n]} {SHA_BASE}", ""
        return 0, "", ""

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


def make_git():
    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            os.makedirs(args[-1], exist_ok=True)
        if "diff" in args:
            return 0, "lib/cache.ts\n", ""
        return 0, "", ""

    return run_git


def state_of(args) -> Path:
    return Path(args[args.index("--state-dir") + 1])


def runner(tmp_path, run_clawpatch):
    # These pin the `clawpatch ci --since` path (`structural_plan: false`, the rollback switch); the
    # plugin-planned path (#232) reuses the same retry/salvage code and is pinned in test_protopatch_feature_plan_232.py.
    cfg = {
        "checkout_root": str(tmp_path / "co"),
        "state_root": str(tmp_path / "st"),
        "default_repo": "",
        "structural_plan": False,
    }
    return ProtoPatchRunner(cfg, run_git=make_git(), run_clawpatch=run_clawpatch)


def repo_dir(tmp_path, repo="octo-repo") -> Path:
    return tmp_path / "st" / repo


def scratch_dirs(tmp_path, repo="octo-repo") -> list[Path]:
    return sorted((repo_dir(tmp_path, repo) / SCRATCH_DIRNAME).glob("*"))


def ok_run(seen, *, findings=True):
    async def run(args, cwd, env, budget_s):
        d = state_of(args)
        seen.append(d)
        if findings:
            (d / "findings").mkdir(parents=True, exist_ok=True)
            (d / "findings" / "f1.json").write_text(json.dumps(RECORD))
        return 0, "{}", "", False

    return run


async def test_concurrent_reviews_of_one_repo_never_share_a_state_dir(tmp_path):
    seen: list[Path] = []
    both_started = asyncio.Barrier(2)

    async def run(args, cwd, env, budget_s):
        seen.append(state_of(args))
        await asyncio.wait_for(both_started.wait(), 5)  # the two passes are alive AT THE SAME TIME
        return 0, "{}", "", False

    r = runner(tmp_path, run)
    a, b = await asyncio.gather(r.review(1, "octo/repo"), r.review(2, "octo/repo"))
    assert "unavailable" not in a.lower() and "unavailable" not in b.lower()
    assert len(seen) == 2 and seen[0] != seen[1]
    for d in seen:
        assert d.parent == repo_dir(tmp_path) / SCRATCH_DIRNAME  # under the repo's dir, never IN it
        assert d != repo_dir(tmp_path)


async def test_one_reviews_findings_never_surface_on_another(tmp_path):
    proceed = asyncio.Event()

    async def run(args, cwd, env, budget_s):
        d = state_of(args)
        if cwd.name == SHA[1]:  # PR 1's pass (its checkout is named by its head) writes a finding...
            (d / "findings").mkdir(parents=True, exist_ok=True)
            (d / "findings" / "f1.json").write_text(json.dumps(RECORD))
            proceed.set()
        else:  # ...and PR 2's pass runs while that finding is on disk
            await asyncio.wait_for(proceed.wait(), 5)
        return 0, "{}", "", False

    r = runner(tmp_path, run)
    a, b = await asyncio.gather(r.review(1, "octo/repo"), r.review(2, "octo/repo"))
    assert "Race in prune()" in a
    assert "0 reportable finding(s)" in b and "Race in prune()" not in b  # the shared dir leaked it


async def test_a_successful_pass_removes_its_state_dir(tmp_path):
    seen: list[Path] = []
    out = await runner(tmp_path, ok_run(seen)).review(1, "octo/repo")
    assert "Race in prune()" in out
    assert seen and not seen[0].exists()
    assert scratch_dirs(tmp_path) == []


async def test_a_failed_pass_keeps_its_state_dir_for_a_postmortem(tmp_path):
    seen: list[Path] = []

    async def run(args, cwd, env, budget_s):
        seen.append(state_of(args))
        (seen[0] / "runs").mkdir()
        return 1, "", "boom", False

    out = await runner(tmp_path, run).review(1, "octo/repo")
    assert "unavailable" in out.lower()
    assert seen[0].is_dir() and (seen[0] / "runs").is_dir()


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


async def test_a_kept_dir_is_pruned_once_old_but_a_recent_one_is_not(tmp_path):
    old = repo_dir(tmp_path) / SCRATCH_DIRNAME / "old-run"
    recent = repo_dir(tmp_path) / SCRATCH_DIRNAME / "recent-run"
    for d in (old, recent):
        d.mkdir(parents=True)
    _age(old, SCRATCH_KEEP_FAILED_S + 60)
    _age(recent, 60)
    await runner(tmp_path, ok_run([])).review(1, "octo/repo")
    assert not old.exists() and recent.exists()


async def test_startup_prunes_old_scratch_dirs_across_every_repo(tmp_path):
    """A redeploy kills runs mid-flight; their dirs are orphans no later review of THAT repo may reach."""
    orphan = repo_dir(tmp_path, "octo-other") / SCRATCH_DIRNAME / "killed-by-a-roll"
    orphan.mkdir(parents=True)
    _age(orphan, SCRATCH_KEEP_FAILED_S + 60)
    await runner(tmp_path, ok_run([])).review(1, "octo/repo")  # a review of a DIFFERENT repo
    assert not orphan.exists()


async def test_provider_failure_captures_land_in_the_repos_persistent_dir(tmp_path):
    seen: list[Path] = []

    async def run(args, cwd, env, budget_s):
        d = state_of(args)
        seen.append(d)
        # What clawpatch's gateway provider does on an unusable reply: mkdir + write, under stateDir.
        (d / "provider-failures").mkdir(parents=True, exist_ok=True)
        (d / "provider-failures" / "20260930T000000000Z-gateway-review-x.json").write_text("{}")
        return 0, "{}", "", False

    await runner(tmp_path, run).review(1, "octo/repo")
    assert not seen[0].exists()  # the scratch dir went away on success...
    kept = repo_dir(tmp_path) / "provider-failures"
    assert [p.name for p in kept.iterdir()] == ["20260930T000000000Z-gateway-review-x.json"]  # ...the capture did not


async def test_the_repos_refutation_file_is_untouched_by_scratch_cleanup(tmp_path):
    repo_dir(tmp_path).mkdir(parents=True)
    refuted = repo_dir(tmp_path) / "refuted.json"
    refuted.write_text('{"keep": true}')
    await runner(tmp_path, ok_run([])).review(1, "octo/repo")
    assert refuted.read_text() == '{"keep": true}'

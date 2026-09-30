"""A roll's own killed panels must not lock their PRs out of the structural lane (#221).

A roll SIGKILLs running panels and the recreated container gets a new hostname. The clawpatch
feature locks those runs held are then foreign-host locks nothing releases, and protoPatch only
reclaims one past a 2 h age — so the PRs the roll killed, which are re-reviewed first, failed
their structural pass with `exit 7` (`feature locked`) and the verdict came out WARN/incomplete.

Before the first `ci` in a repo's state dir, once per process, the runner runs
`clawpatch clean-locks --stale-only` with the stale age forced to 1 ms: every foreign-host lock
goes (lock file and the feature record's copy), while a lock a live pid on THIS host holds stays.
The behaviour of that command was checked against the real clawpatch build; these tests pin
WHEN and HOW the runner calls it.
"""

from __future__ import annotations

import json

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import ProtoPatchRunner

SHA_HEAD = "a" * 40
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
def fresh_process(monkeypatch):
    """`_LOCK_SWEEPS_DONE` is per-process state; every test starts as a freshly booted process."""
    monkeypatch.setattr(pp, "_LOCK_SWEEPS_DONE", set(), raising=False)
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    monkeypatch.delenv("CLAWPATCH_LOCK_STALE_MS", raising=False)

    async def fake_run_gh(args, timeout=30):
        if args[:1] == ["api"]:
            return 0, f"{SHA_HEAD} {SHA_BASE}", ""
        return 0, "", ""

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


def make_git():
    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            import os

            os.makedirs(args[-1], exist_ok=True)
        if "diff" in args:
            return 0, "lib/cache.ts\n", ""
        return 0, "", ""

    return run_git


def recording_clawpatch(calls, sweep=None):
    """Fake clawpatch: `ci` succeeds with one finding on disk; `clean-locks` answers with `sweep`
    ({rc, stdout, stderr, timed_out} or an exception to raise)."""

    async def run(args, cwd, env, budget_s):
        calls.append({"args": list(args), "env": dict(env), "budget_s": budget_s})
        if args[1] == "clean-locks":
            if isinstance(sweep, Exception):
                raise sweep
            s = sweep or {}
            return (
                s.get("rc", 0),
                s.get("stdout", '{"cleared": 0, "lockFilesCleared": 0}'),
                s.get("stderr", ""),
                s.get("timed_out", False),
            )
        state = pp_state(args)
        (state / "findings").mkdir(parents=True, exist_ok=True)
        (state / "findings" / "f1.json").write_text(json.dumps(RECORD))
        return 0, "{}", "", False

    return run


def pp_state(args):
    from pathlib import Path

    return Path(args[args.index("--state-dir") + 1])


def runner(tmp_path, calls, sweep=None):
    cfg = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    return ProtoPatchRunner(cfg, run_git=make_git(), run_clawpatch=recording_clawpatch(calls, sweep))


def seed_locks(tmp_path, repo="octo-repo"):
    (tmp_path / "st" / repo / "locks").mkdir(parents=True)


def kinds(calls):
    return [c["args"][1] for c in calls]


async def test_sweeps_before_the_first_ci_with_a_forced_stale_age(tmp_path):
    seed_locks(tmp_path)
    calls: list = []
    out = await runner(tmp_path, calls).review(1, "octo/repo")
    assert kinds(calls) == ["clean-locks", "ci"]  # the sweep runs BEFORE the pass that would hit the lock
    sweep = calls[0]
    assert sweep["args"][2:5] == ["--stale-only", "--json", "--state-dir"]
    assert sweep["args"][5] == str(tmp_path / "st" / "octo-repo")
    # 1 ms, NOT 0: protoPatch reads a non-positive value as "unset" and falls back to its 2 h default.
    assert sweep["env"]["CLAWPATCH_LOCK_STALE_MS"] == "1"
    assert "Race in prune()" in out  # and the review itself is unaffected


async def test_the_forced_stale_age_never_leaks_into_the_review_itself(tmp_path):
    seed_locks(tmp_path)
    calls: list = []
    await runner(tmp_path, calls).review(1, "octo/repo")
    ci = next(c for c in calls if c["args"][1] == "ci")
    # A live run's lock must keep protoPatch's own 2 h rule during the pass.
    assert "CLAWPATCH_LOCK_STALE_MS" not in ci["env"]


async def test_sweeps_once_per_state_dir_per_process(tmp_path):
    seed_locks(tmp_path)
    calls: list = []
    r = runner(tmp_path, calls)
    await r.review(1, "octo/repo")
    await r.review(2, "octo/repo")
    assert kinds(calls).count("clean-locks") == 1
    # A config reload builds a NEW runner in the same process; it must not sweep again (a run the
    # old instance still has in flight would otherwise be swept under it).
    await runner(tmp_path, calls).review(3, "octo/repo")
    assert kinds(calls).count("clean-locks") == 1


async def test_each_repo_state_dir_is_swept_separately(tmp_path):
    seed_locks(tmp_path, "octo-repo")
    seed_locks(tmp_path, "octo-other")
    calls: list = []
    r = runner(tmp_path, calls)
    await r.review(1, "octo/repo")
    await r.review(1, "octo/other")
    swept = [pp_state(c["args"]).name for c in calls if c["args"][1] == "clean-locks"]
    assert swept == ["octo-repo", "octo-other"]


async def test_no_sweep_when_nothing_was_ever_claimed_there(tmp_path):
    calls: list = []  # no locks/ dir: a repo's first review ever
    await runner(tmp_path, calls).review(1, "octo/repo")
    assert kinds(calls) == ["ci"]


@pytest.mark.parametrize(
    "sweep",
    [
        {"rc": 1, "stderr": "project not initialized"},
        {"rc": 127},
        {"timed_out": True, "rc": -9},
        {"stdout": "not json at all"},
        RuntimeError("spawn failed"),
    ],
    ids=["nonzero", "not-installed", "timed-out", "garbled-output", "raises"],
)
async def test_a_failed_sweep_never_voids_the_review(tmp_path, sweep):
    seed_locks(tmp_path)
    calls: list = []
    out = await runner(tmp_path, calls, sweep).review(1, "octo/repo")
    assert kinds(calls) == ["clean-locks", "ci"]
    assert "Race in prune()" in out
    assert "unavailable" not in out.lower()

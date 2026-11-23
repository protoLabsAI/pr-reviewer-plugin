"""The clawpatch state root's retention (#373).

Before #223/#225 every pass worked in the repo dir itself, leaving `reports/`, `features/`,
`findings/`, `runs/`, `locks/`, `locks-stale/` and `patches/` there; since each pass got its own
scratch dir nothing reads them, yet `_prune_scratch` only looked inside `scratch/`, so they lived
forever (~90 MB on one host). `provider-failures/` captures were never aged out either. The prune
now removes the legacy dirs and captures older than 30 days, and leaves everything that persists
on purpose alone.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from pr_reviewer.protopatch import (
    LEGACY_REPO_DIRS,
    PROVIDER_FAILURES_KEEP_S,
    SCRATCH_DIRNAME,
    SCRATCH_KEEP_FAILED_S,
    ProtoPatchRunner,
)


def runner(tmp_path) -> ProtoPatchRunner:
    return ProtoPatchRunner({"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st")})


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def seed_legacy(repo: Path) -> None:
    for name in LEGACY_REPO_DIRS:
        (repo / name / "nested").mkdir(parents=True)
        (repo / name / "nested" / "x.json").write_text("{}")


def test_the_constants():
    assert LEGACY_REPO_DIRS == ("reports", "features", "findings", "runs", "locks", "locks-stale", "patches")
    assert PROVIDER_FAILURES_KEEP_S == 30 * 86400


def test_legacy_dirs_are_removed_from_the_repo_dir(tmp_path):
    repo = tmp_path / "st" / "octo-repo"
    seed_legacy(repo)
    removed = runner(tmp_path)._prune_scratch(repo)
    assert not any((repo / name).exists() for name in LEGACY_REPO_DIRS)
    assert removed == len(LEGACY_REPO_DIRS)


def test_startup_prune_removes_legacy_dirs_across_every_repo(tmp_path):
    repos = [tmp_path / "st" / "octo-a", tmp_path / "st" / "octo-b"]
    for r in repos:
        seed_legacy(r)
    runner(tmp_path)._prune_scratch()
    for r in repos:
        assert r.is_dir()
        assert not any((r / name).exists() for name in LEGACY_REPO_DIRS)


def test_provider_failures_older_than_30_days_go_newer_ones_and_the_dir_stay(tmp_path):
    repo = tmp_path / "st" / "octo-repo"
    captures = repo / "provider-failures"
    captures.mkdir(parents=True)
    old, fresh, edge = captures / "old.json", captures / "fresh.json", captures / "edge.json"
    for f in (old, fresh, edge):
        f.write_text("{}")
    _age(old, PROVIDER_FAILURES_KEEP_S + 3600)
    _age(fresh, 86400)
    _age(edge, PROVIDER_FAILURES_KEEP_S - 3600)
    sub = captures / "a-subdir"  # files only: a dir inside is never removed
    sub.mkdir()
    _age(sub, PROVIDER_FAILURES_KEEP_S + 3600)
    _age(captures, PROVIDER_FAILURES_KEEP_S + 3600)  # the dir's own age never matters

    removed = runner(tmp_path)._prune_scratch(repo)

    assert removed == 1
    assert captures.is_dir()
    assert sorted(p.name for p in captures.iterdir()) == ["a-subdir", "edge.json", "fresh.json"]


def test_a_live_scratch_symlink_to_the_captures_survives(tmp_path):
    repo = tmp_path / "st" / "octo-repo"
    captures = repo / "provider-failures"
    captures.mkdir(parents=True)
    live = repo / SCRATCH_DIRNAME / "live-run"
    live.mkdir(parents=True)
    (live / "provider-failures").symlink_to(captures, target_is_directory=True)
    runner(tmp_path)._prune_scratch(repo)
    assert captures.is_dir() and (live / "provider-failures").is_symlink()


def test_the_keep_list_is_untouched(tmp_path):
    root = tmp_path / "st"
    repo = root / "octo-repo"
    seed_legacy(repo)
    refuted = repo / "refuted.json"
    refuted.write_text('{"keep": true}')
    _age(refuted, PROVIDER_FAILURES_KEEP_S * 3)
    fresh = repo / SCRATCH_DIRNAME / "fresh-run"
    (fresh / "findings").mkdir(parents=True)  # a legacy NAME inside a scratch dir is not a legacy dir
    _age(fresh, 60)
    # The LLM refutation store sits beside the repo dirs; an owner named like a legacy dir stays.
    llm = root / "llm-refutations" / "patches"
    llm.mkdir(parents=True)
    (llm / "repo.json").write_text('{"entries": []}')
    (root / "llm-refutations" / "reports").mkdir()

    runner(tmp_path)._prune_scratch()

    assert refuted.read_text() == '{"keep": true}'
    assert (fresh / "findings").is_dir()
    assert (llm / "repo.json").read_text() == '{"entries": []}'
    assert (root / "llm-refutations" / "reports").is_dir()
    assert not any((repo / name).exists() for name in LEGACY_REPO_DIRS)


def test_old_scratch_still_pruned_and_counted_with_the_rest(tmp_path):
    repo = tmp_path / "st" / "octo-repo"
    old = repo / SCRATCH_DIRNAME / "old-run"
    old.mkdir(parents=True)
    _age(old, SCRATCH_KEEP_FAILED_S + 60)
    (repo / "locks").mkdir()
    assert runner(tmp_path)._prune_scratch(repo) == 2
    assert not old.exists() and not (repo / "locks").exists()


def test_an_empty_or_missing_state_root_never_raises(tmp_path):
    assert runner(tmp_path)._prune_scratch() == 0
    assert runner(tmp_path)._prune_scratch(tmp_path / "st" / "absent") == 0

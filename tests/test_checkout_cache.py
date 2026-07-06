"""The checkout cache — keying, freshness, half-clone cleanup, prune."""

from __future__ import annotations

import os
import time

import pytest
from pr_reviewer.checkout_cache import CheckoutCache, CheckoutError, redact


def make_git(calls, fail_on=None):
    async def run_git(args, timeout_s=180):
        calls.append(args)
        if args[0] == "clone":
            if fail_on == "clone":
                return 128, "", "fatal: repository not found"
            os.makedirs(args[-1], exist_ok=True)
        if args and "checkout" in args and fail_on == "checkout":
            return 1, "", "fatal: reference is not a tree"
        return 0, "", ""

    return run_git


async def test_rejects_bad_inputs(tmp_path):
    cache = CheckoutCache(tmp_path, run_git=make_git([]))
    with pytest.raises(CheckoutError):
        await cache.resolve("not-a-slug", "a" * 40)
    with pytest.raises(CheckoutError):
        await cache.resolve("octo/repo", "NOT-HEX")
    with pytest.raises(CheckoutError):
        await cache.resolve("octo/repo", "abc")  # too short (< 7)


async def test_clone_is_blobless_and_keyed_on_repo_at_sha(tmp_path):
    calls = []
    cache = CheckoutCache(tmp_path, run_git=make_git(calls))
    sha = "a" * 40
    target = await cache.resolve("octo/repo", sha, token="tok123")
    assert target == tmp_path / "octo-repo" / sha
    clone = calls[0]
    assert clone[0] == "clone" and "--filter=blob:none" in clone and "--no-checkout" in clone
    assert "x-access-token:tok123@github.com/octo/repo.git" in clone[-2]
    assert ["-C", str(target), "checkout", "--quiet", sha] == calls[-1]


async def test_fresh_hit_skips_git_and_bumps_mtime(tmp_path):
    calls = []
    cache = CheckoutCache(tmp_path, run_git=make_git(calls))
    sha = "b" * 40
    target = await cache.resolve("octo/repo", sha)
    n = len(calls)
    old = time.time() - 10
    os.utime(target, (old, old))
    assert await cache.resolve("octo/repo", sha) == target
    assert len(calls) == n  # no new git calls
    assert target.stat().st_mtime > old  # LRU bump


async def test_stale_entry_is_recloned(tmp_path):
    calls = []
    cache = CheckoutCache(tmp_path, ttl_s=1, run_git=make_git(calls))
    sha = "c" * 40
    target = await cache.resolve("octo/repo", sha)
    stale = time.time() - 5
    os.utime(target, (stale, stale))
    await cache.resolve("octo/repo", sha)
    assert sum(1 for c in calls if c[0] == "clone") == 2


async def test_failed_clone_never_leaves_a_fake_hit(tmp_path):
    cache = CheckoutCache(tmp_path, run_git=make_git([], fail_on="checkout"))
    sha = "d" * 40
    with pytest.raises(CheckoutError):
        await cache.resolve("octo/repo", sha, token="tok123")
    assert not cache.dir_for("octo/repo", sha).exists()


async def test_clone_error_redacts_the_token(tmp_path):
    cache = CheckoutCache(tmp_path, run_git=make_git([], fail_on="clone"))
    with pytest.raises(CheckoutError) as exc:
        await cache.resolve("octo/repo", "e" * 40, token="supersecret")
    assert "supersecret" not in str(exc.value)


def test_redact():
    assert redact("https://x:tok@host tok", "tok") == "https://x:***@host ***"
    assert redact("nothing", None) == "nothing"


def test_prune_ttl_and_lru_caps(tmp_path):
    cache = CheckoutCache(tmp_path, ttl_s=100, entry_limit=2, size_limit_bytes=10**9)
    now = time.time()
    for i, age in enumerate((500, 50, 30, 10)):  # first is past TTL
        d = tmp_path / "octo-repo" / (str(i) * 40)
        d.mkdir(parents=True)
        (d / "f").write_text("x" * 10)
        os.utime(d, (now - age, now - age))
    removed = cache.prune()
    # TTL takes the 500s-old entry; the entry cap (2) LRU-evicts the 50s-old one.
    assert removed == 2
    survivors = sorted(p.name[0] for p in (tmp_path / "octo-repo").iterdir())
    assert survivors == ["2", "3"]


def test_prune_on_missing_root_is_a_noop(tmp_path):
    assert CheckoutCache(tmp_path / "nope").prune() == 0

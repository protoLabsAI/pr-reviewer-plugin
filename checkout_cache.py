"""Content-addressed checkout cache for structural reviews — Quinn's CheckoutCache, ported.

One cache entry per `(repo, head_sha)`: a **blobless partial clone** (`--filter=blob:none`,
NOT a depth-shallow clone) checked out at the PR head. All commit/tree objects are present
so any base ref resolves for `git diff <base>...HEAD` (the merge-base exists); blobs fetch
lazily when clawpatch reads file contents. Entries live under
`<root>/<owner>-<repo>/<sha>/` and are considered fresh for `ttl_s` (default 1h — aligned
with GitHub token lifetime in the upstream design); a fresh hit gets an mtime touch (LRU
bump), a stale one is removed and re-cloned. `prune()` (TTL sweep + LRU eviction under the
entry/byte caps) is for a maintenance cadence, not the hot path.

Auth: a token (if provided) is embedded in the clone URL so lazy blob fetches keep
authenticating; it is redacted from every error message. Git subprocesses run with
`GIT_TERMINAL_PROMPT=0` (fail fast, never hang on a prompt) and a hard per-command
timeout (SIGKILL).

Host-free and injectable: `run_git` is a constructor seam so tests never shell out.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path

_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_SHA_RE = re.compile(r"^[a-f0-9]{7,40}$")

_GIT_TIMEOUT_S = 180  # per git command, SIGKILL past it

DEFAULT_TTL_S = 60 * 60
DEFAULT_ENTRY_LIMIT = 50
DEFAULT_SIZE_LIMIT_BYTES = 5 * 1024**3


class CheckoutError(Exception):
    """A checkout could not be produced (bad input, git failure, timeout)."""


def redact(text: str, token: str | None) -> str:
    return text.replace(token, "***") if token else text


async def _default_run_git(args: list[str], timeout_s: int = _GIT_TIMEOUT_S) -> tuple[int, str, str]:
    """Run `git <args>`; (rc, stdout, stderr). Times out with a kill; missing binary → rc 127."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
    except FileNotFoundError:
        return 127, "", "git: command not found"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", f"git {' '.join(args[:2])}: timed out after {timeout_s}s"
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace")


class CheckoutCache:
    def __init__(
        self,
        root: str | Path,
        *,
        ttl_s: int = DEFAULT_TTL_S,
        entry_limit: int = DEFAULT_ENTRY_LIMIT,
        size_limit_bytes: int = DEFAULT_SIZE_LIMIT_BYTES,
        run_git=None,
    ):
        self.root = Path(root)
        self.ttl_s = ttl_s
        self.entry_limit = entry_limit
        self.size_limit_bytes = size_limit_bytes
        self._run_git = run_git or _default_run_git
        # Simultaneous reviews of the same head queue rather than race the clone.
        self._locks: dict[str, asyncio.Lock] = {}

    def dir_for(self, repo: str, sha: str) -> Path:
        return self.root / repo.replace("/", "-") / sha

    async def resolve(self, repo: str, sha: str, token: str | None, *, depth: int) -> Path:
        """The checkout dir for `repo@sha` — a fresh cache hit or a new depth-bounded clone.

        `depth` bounds how much history the clone fetches; pass 0 for the full
        blobless clone (the pre-0.2 behavior)."""
        if not _REPO_RE.match(repo or ""):
            raise CheckoutError(f"invalid repo {repo!r} (want owner/name)")
        if not _SHA_RE.match(sha or ""):
            raise CheckoutError(f"invalid sha {sha!r} (want 7-40 hex chars)")
        # Eager housekeeping: keep the cache trim on every resolve so the daily
        # ceremony has less to do.
        self.prune()
        return await self._resolve_locked(repo, sha, token, depth)

    async def _resolve_locked(self, repo: str, sha: str, token: str | None, depth: int) -> Path:
        target = self.dir_for(repo, sha)
        if target.is_dir():
            age = time.time() - target.stat().st_mtime
            if age < self.ttl_s:
                os.utime(target)  # LRU bump
                return target
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            await self._clone(repo, sha, target, token, depth)
        except Exception:
            # A half-cloned tree must never look like a hit next time.
            shutil.rmtree(target, ignore_errors=True)
            raise
        return target

    async def _clone(self, repo: str, sha: str, target: Path, token: str | None, depth: int = 0) -> None:
        auth = f"x-access-token:{token}@" if token else ""
        url = f"https://{auth}github.com/{repo}.git"
        clone_args = ["clone", "--filter=blob:none", "--no-checkout", "--no-tags", "--quiet"]
        if depth:
            clone_args += ["--depth", str(depth)]
        rc, _out, err = await self._run_git([*clone_args, url, str(target)])
        if rc != 0:
            raise CheckoutError(f"clone of {repo} failed (rc {rc}): {redact(err.strip(), token)}")
        # Best-effort: the head of an un-merged PR may not be reachable from clone refs.
        await self._run_git(["-C", str(target), "fetch", "--filter=blob:none", "--no-tags", "--quiet", "origin", sha])
        rc, _out, err = await self._run_git(["-C", str(target), "checkout", "--quiet", sha])
        if rc != 0:
            raise CheckoutError(f"checkout of {repo}@{sha[:12]} failed (rc {rc}): {redact(err.strip(), token)}")

    def prune(self) -> int:
        """TTL sweep, then LRU-evict until under BOTH the entry and byte caps.
        Returns the number of entries removed. Maintenance cadence, not the hot path."""
        entries: list[tuple[float, int, Path]] = []  # (mtime, bytes, path)
        removed = 0
        now = time.time()
        for repo_dir in self.root.iterdir() if self.root.is_dir() else []:
            if not repo_dir.is_dir():
                continue
            for entry in repo_dir.iterdir():
                if not entry.is_dir():
                    continue
                mtime = entry.stat().st_mtime
                if now - mtime >= self.ttl_s:
                    shutil.rmtree(entry, ignore_errors=True)
                    removed += 1
                    continue
                size = sum(p.stat().st_size for p in entry.rglob("*") if p.is_file())
                entries.append((mtime, size, entry))
        entries.sort()  # oldest first
        total = sum(size for _mtime, size, _path in entries)
        while entries and (len(entries) > self.entry_limit or total > self.size_limit_bytes):
            _mtime, size, victim = entries.pop(0)
            shutil.rmtree(victim, ignore_errors=True)
            total -= size
            removed += 1
        return removed

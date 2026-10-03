"""A real, local git repo for the tests that search a checkout (#259)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git_repo(root: Path, files: dict[str, str]) -> str:
    """Write `files` under `root`, commit them, and return the commit SHA."""
    root.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **_ENV}
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "fixture"], check=True, env=env)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True
    ).stdout.strip()


def resolver_for(root: Path):
    """A `resolve_checkout` that always returns `root`."""

    async def resolve(repo: str, head: str) -> Path:
        return root

    return resolve

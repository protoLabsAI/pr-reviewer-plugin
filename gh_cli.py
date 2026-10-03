"""Async `gh` CLI runner — vendored so the plugin is host-free (same shape as github-plugin's).

Timeout + kill, missing-binary detection, token injection. Auth: GITHUB_TOKEN / GH_TOKEN
from the env when set, else `gh`'s own ambient auth (`gh auth login`).
"""

from __future__ import annotations

import asyncio
import json
import os
import re

_COMMAND_TIMEOUT = 30

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def bad_repo(repo: str) -> str | None:
    """Validate an `owner/name` repo slug; an Error string if invalid, else None."""
    if not repo or not REPO_RE.match(repo):
        return (
            f"Error: no usable repo (got {repo!r}). Pass repo='owner/name', or set "
            "pr_reviewer.default_repo so it can be omitted."
        )
    return None


def resolve_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


async def run_gh(args: list[str], timeout: int = _COMMAND_TIMEOUT) -> tuple[int, str, str]:
    """Run a `gh` command → (returncode, stdout, stderr). Kills on timeout; reports a
    clean error when `gh` isn't installed instead of raising."""
    env = os.environ.copy()
    token = resolve_token()
    if token:
        env["GITHUB_TOKEN"] = token
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "gh", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )
    except FileNotFoundError:
        return 127, "", "gh: command not found — install the GitHub CLI"
    except asyncio.TimeoutError:
        if proc is not None:
            proc.kill()
            await proc.communicate()
        return 124, "", f"gh {' '.join(args[:2])}: timed out after {timeout}s"


def gh_json_rows(out: str) -> list | None:
    """Parse `gh api --paginate --jq '.[] | …'` output → rows, or None if unparseable.

    `--paginate` applies the jq filter PER PAGE and concatenates the results, so an
    array-wrapping filter (`[.[] | …]`) emits `[…][…]` on the second page — not valid
    JSON, and every such read silently broke the moment a PR crossed 30 items
    (issue #75). Emitting one object per line instead is pagination-safe by
    construction: `gh` prints compact JSON, so embedded newlines stay escaped and one
    row really is one line.

    A single unparseable line makes the WHOLE read None rather than a short list.
    These rows drive "has this been reviewed", "did an operator pause this" and "are
    the checks green" — a silently-short answer is the failure mode of #71, where a
    partial read was indistinguishable from an absence.

    Still accepts a whole-array body, so a caller that drops --jq (or a fake that
    returns one array) keeps working.
    """
    text = (out or "").strip()
    if not text:
        return []
    try:
        whole = json.loads(text)
    except ValueError:
        pass
    else:
        return whole if isinstance(whole, list) else [whole]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            return None  # a partial read is worse than none — it looks complete
    return rows

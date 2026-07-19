"""Existing-review-threads context — fetch, escape, wrap (ADR 0078 D5 extension).

The panel runs alongside Quinn, CodeRabbit, and human reviewers and could not see
any of their inline threads — so it re-posted what was already being discussed.
This module feeds those threads to the recipe as ONE pre-rendered, explicitly
untrusted data block (`<pr_review_threads>`), the finders suppress overlaps.

Rendering discipline ported from open-swe's reviewer (thread bodies are
attacker-controlled text — anyone who can comment on a PR writes into them):

  - closing tags of our wrapper elements are neutralized whitespace-tolerantly
    (XML accepts ``</body >`` etc.), so a body can never break out of its wrapper;
  - author logins are validated against GitHub's username grammar — anything else
    renders as "unknown" rather than smuggling freeform text into an attribute;
  - bodies truncate per-comment, the block caps total threads, open threads sort
    first (they're the ones overlap suppression is really about).

Failure posture: `fetch_threads` returns None on any read problem and the
dispatcher simply omits the input (the recipe default "(none)" applies) — thread
awareness is an enhancement, never a review blocker.
"""

from __future__ import annotations

import json
import re

MAX_THREADS = 50
MAX_BODY_CHARS = 2000
MAX_COMMENTS_PER_THREAD = 10

# GitHub login grammar: alphanumerics with single inner hyphens, ≤39 chars,
# optional "[bot]" suffix. Anything else becomes "unknown".
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}(?:\[bot\])?$")

_WRAPPER_TAGS = ("pr_review_threads", "thread", "comment", "body")
_CLOSING_TAG_RE = re.compile(r"</\s*(" + "|".join(_WRAPPER_TAGS) + r")\s*>", re.IGNORECASE)


def _escape(text: str) -> str:
    """Neutralize wrapper closing tags (whitespace-tolerant) so a comment body
    can't terminate the data block early: ``</body>`` → ``</body_>``."""
    return _CLOSING_TAG_RE.sub(lambda m: f"</{m.group(1).lower()}_>", text)


def _safe_login(value: object) -> str:
    if isinstance(value, str) and _LOGIN_RE.match(value):
        return value
    return "unknown"


async def fetch_threads(run_gh, repo: str, pr: int) -> list[dict] | None:
    """The PR's inline review threads via GraphQL, or None when unreadable."""
    owner, name = repo.split("/", 1)
    rc, out, _err = await run_gh(
        [
            "api",
            "graphql",
            "-f",
            f'query=query {{ repository(owner: "{owner}", name: "{name}") '
            f"{{ pullRequest(number: {pr}) {{ reviewThreads(first: 100) {{ nodes {{ "
            f"isResolved isOutdated path line originalLine "
            f"comments(first: {MAX_COMMENTS_PER_THREAD}) {{ nodes {{ author {{ login }} body }} }} "
            f"}} }} }} }} }}",
            "--jq",
            ".data.repository.pullRequest.reviewThreads.nodes",
        ],
    )
    if rc != 0:
        return None
    try:
        nodes = json.loads(out)
    except json.JSONDecodeError:
        return None
    return nodes if isinstance(nodes, list) else None


def render_threads_block(threads: list[dict]) -> str:
    """The `<pr_review_threads>` data block, or "" when there is nothing to show.
    Open + non-outdated threads first, then by path/line for stability."""
    visible = [t for t in threads if isinstance(t, dict) and (t.get("comments") or {}).get("nodes")]
    if not visible:
        return ""

    def _key(t: dict) -> tuple[int, str, int]:
        open_first = 0 if not t.get("isResolved") and not t.get("isOutdated") else 1
        line = t.get("line") if isinstance(t.get("line"), int) else t.get("originalLine")
        return (open_first, str(t.get("path") or ""), line if isinstance(line, int) else 0)

    visible.sort(key=_key)
    out = ["<pr_review_threads>"]
    for t in visible[:MAX_THREADS]:
        path = str(t.get("path") or "<unknown>")
        line = t.get("line") if isinstance(t.get("line"), int) else t.get("originalLine")
        location = f"{path}:{line}" if isinstance(line, int) else path
        status = "resolved" if t.get("isResolved") else ("outdated" if t.get("isOutdated") else "open")
        safe_location = location.replace('"', "&quot;").replace(">", "&gt;")
        out.append(f'  <thread location="{safe_location}" status="{status}">')
        for c in t["comments"]["nodes"]:
            if not isinstance(c, dict):
                continue
            login = _safe_login((c.get("author") or {}).get("login"))
            body = c.get("body") if isinstance(c.get("body"), str) else ""
            if len(body) > MAX_BODY_CHARS:
                body = body[:MAX_BODY_CHARS] + "\n...[truncated]"
            out.append(f'    <comment author="{login}">')
            out.append("      <body>")
            out.append(_escape(body))
            out.append("      </body>")
            out.append("    </comment>")
        out.append("  </thread>")
    out.append("</pr_review_threads>")
    return "\n".join(out)

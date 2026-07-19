"""Findings → verdict, and the posted comment body (ADR 0078 C).

The verdict mapping is a PURE function of the final findings list — the model never
"chooses" a verdict (Quinn's #748 lesson, generalized). The posted body carries a
machine-readable marker line so prior-review recall and per-head-SHA promotion dedup
read GitHub itself as the store (ADR 0078 D5 — no local review DB to drift).

In-diff confinement (open-swe's `add_finding` lesson, applied at our seam): the panel
prompts ask finders to stay inside the diff, but nothing enforced it — a finding on an
untouched file could gate a merge. `confine_findings` makes it a property: findings
whose `file` isn't one of the PR's changed paths never reach `verdict_for`. It fails
OPEN on an empty/unreadable changed-path list — an unreadable file list must never
launder a FAIL into a PASS.

Severity → verdict: any confirmed-or-unverdicted blocker/major ⇒ FAIL (real defects
gate); only minors, or majors the verify pass left "uncertain" ⇒ WARN (worth a human
glance, not a block); empty or nits-only ⇒ PASS. A finding the verifier REFUTED never
reaches this function (the report pass drops them).
"""

from __future__ import annotations

import json
import re

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

_MARKER_RE = re.compile(
    r"<!--\s*protoagent-qa-review\s+head=(?P<head>[a-f0-9]{7,40})\s+verdict=(?P<verdict>PASS|WARN|FAIL)"
    r"(?:\s+promoted=(?P<promoted>true|false))?\s*-->"
)


def verdict_for(findings: list[dict]) -> str:
    """The pure mapping. `findings` are ADR 0077 dicts (post-report: refuted already dropped)."""
    worst = PASS
    for f in findings:
        sev = str(f.get("severity") or "").lower()
        verdict = str(f.get("verdict") or "").lower()
        if sev in ("blocker", "major"):
            if verdict == "uncertain":
                worst = WARN if worst != FAIL else worst
            else:  # confirmed, or no verify annotation — trust the panel
                return FAIL
        elif sev == "minor":
            worst = WARN if worst != FAIL else worst
        # nits never move the verdict
    return worst


def _norm_path(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path.removeprefix("/")


def confine_findings(findings: list[dict], changed_paths: list[str]) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). A finding must anchor to a file this PR actually changed —
    file-less findings are contract violations (gaps belong in prose, not the array)
    and drop too. Empty `changed_paths` means the file list was unreadable: skip
    confinement entirely (fail open) rather than dropping everything to PASS."""
    changed = {_norm_path(p) for p in changed_paths if p and p.strip()}
    if not changed:
        return list(findings), []
    kept: list[dict] = []
    dropped: list[dict] = []
    for finding in findings:
        file = _norm_path(str(finding.get("file") or ""))
        (kept if file and file in changed else dropped).append(finding)
    return kept, dropped


def render_verdict_body(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    verdict: str,
    report: str,
    shadow: bool,
    recipe: str,
    confined: list[dict] | None = None,
) -> str:
    """The comment body: marker line (machine) + header (human) + the panel's report,
    plus a confinement footnote when findings were excluded — the report's own JSON
    still shows them, so the reader needs to see why the verdict ignored them."""
    mode = "shadow — comment-only" if shadow else "formal"
    footnote = ""
    if confined:
        lines = "\n".join(
            f"- `{f.get('file') or '(no file)'}` ({f.get('severity') or '?'}) — {str(f.get('claim') or '')[:160]}"
            for f in confined
        )
        footnote = (
            f"\n\n---\n_{len(confined)} finding(s) excluded from the verdict by in-diff "
            f"confinement (file not among this PR's changed paths):_\n{lines}"
        )
    return (
        f"<!-- protoagent-qa-review head={head_sha} verdict={verdict} promoted=false -->\n"
        f"## QA panel review — **{verdict}**\n"
        f"_{recipe} · head `{head_sha[:12]}` · {mode}_\n\n"
        f"{report}{footnote}"
    )


def parse_verdict_marker(body: str) -> dict | None:
    """{'head', 'verdict', 'promoted'} from a posted body, or None if it isn't ours."""
    m = _MARKER_RE.search(body or "")
    if not m:
        return None
    return {
        "head": m.group("head"),
        "verdict": m.group("verdict"),
        "promoted": m.group("promoted") == "true",
    }


def extract_findings_json(body: str) -> str:
    """The findings JSON block from a posted verdict body (for `prior_findings` on a
    delta re-review). Returns the fenced block text, or '' when absent."""
    blocks = re.findall(r"```json\s*\n(.*?)```", body or "", re.DOTALL)
    for block in reversed(blocks):  # the findings array is the report's FINAL block
        text = block.strip()
        if text.startswith("["):
            try:
                json.loads(text)
            except json.JSONDecodeError:
                continue
            return text
    return ""

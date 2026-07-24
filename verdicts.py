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

# The marker must tolerate attributes it does not know about. It anchors on head +
# verdict, accepts `promoted`, and then allows ANY further `key=value` pairs before the
# close. v0.13.0 appended `findings=N` after `promoted=true` and this regex — which
# required `-->` right after `promoted` — stopped matching entirely. A marker that fails
# to parse is not read as ours, so `already-promoted` never fired and approve-on-green
# re-approved the same head every sweep tick, forever. An unparsed marker is silent and
# its consequences are not: extensibility here is a correctness property, not neatness.
_MARKER_RE = re.compile(
    r"<!--\s*protoagent-qa-review\s+head=(?P<head>[a-f0-9]{7,40})\s+verdict=(?P<verdict>PASS|WARN|FAIL)"
    r"(?:\s+promoted=(?P<promoted>true|false))?"
    r"(?:\s+[A-Za-z_][A-Za-z0-9_]*=[^\s>]*)*"
    r"\s*-->"
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


_SEV_MARK = {"blocker": "🔴", "major": "🟠", "minor": "🟡", "nit": "⚪"}
_VERDICT_MARK = {"confirmed": "confirmed", "uncertain": "⚠️ uncertain", "refuted": "~~refuted~~"}


def _cell(text: object, limit: int = 160) -> str:
    """One markdown table cell: no pipes, no newlines, bounded — so a claim quoting a
    diff (which contains both) can't break the table."""
    s = re.sub(r"\s+", " ", str(text or "")).replace("|", "\\|").strip()
    return (s[: limit - 1] + "…") if len(s) > limit else s


def render_findings_table(findings: list[dict]) -> str:
    """The findings as a scannable markdown table instead of a raw JSON dump. The JSON
    stays in the body too (collapsed) — prior-round recall reads it back, so it can't go.

    `verdict` here is the verifier's per-finding annotation (confirmed/uncertain/refuted),
    not the review verdict."""
    rows = [f for f in findings if isinstance(f, dict)]
    if not rows:
        return ""
    order = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
    rows.sort(key=lambda f: order.get(str(f.get("severity") or "").lower(), 4))
    out = ["| | Severity | Location | Finding | Verified |", "|---|---|---|---|---|"]
    for f in rows:
        sev = str(f.get("severity") or "?").lower()
        loc = str(f.get("file") or "(no file)")
        line = f.get("line")
        if isinstance(line, int) and line > 0:
            loc = f"{loc}:{line}"
        vmark = _VERDICT_MARK.get(str(f.get("verdict") or "").lower(), str(f.get("verdict") or ""))
        out.append(f"| {_SEV_MARK.get(sev, '•')} | {sev} | `{_cell(loc, 80)}` | {_cell(f.get('claim'))} | {vmark} |")
    return "\n".join(out)


def reflow_report(report: str) -> str:
    """Swap the report's trailing raw findings array for a human table, and tuck the raw
    JSON into a collapsed <details> — still present, so `extract_findings_json` (and thus
    prior-round recall) keeps reading it. A clean/empty array or an unparseable block is
    left exactly as-is: nothing to tabulate, and never risk mangling the machine record."""
    blocks = list(re.finditer(r"```json\s*\n(.*?)```", report or "", re.DOTALL))
    if not blocks:
        return report
    m = blocks[-1]  # the findings array is the report's FINAL json block
    text = m.group(1).strip()
    if not text.startswith("["):
        return report
    try:
        findings = json.loads(text)
    except json.JSONDecodeError:
        return report
    if not isinstance(findings, list) or not findings:
        return report  # a clean pass ([]) needs no table; leave the record untouched
    table = render_findings_table(findings)
    collapsed = f"<details>\n<summary>findings JSON (machine-readable)</summary>\n\n{m.group(0)}\n</details>"
    return report[: m.start()] + f"### Findings\n\n{table}\n\n{collapsed}" + report[m.end() :]


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
    notes: str = "",
) -> str:
    """The comment body: marker line (machine) + header (human) + the panel's report,
    plus a confinement footnote when findings were excluded — the report's own JSON
    still shows them, so the reader needs to see why the verdict ignored them.

    `notes` is a pre-rendered trailing section (the convergence checklist, issue #23);
    it arrives as text so this module stays free of the round machinery that builds it."""
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
        f"{reflow_report(report)}{footnote}{notes}"
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

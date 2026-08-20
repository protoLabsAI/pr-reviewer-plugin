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

# The per-finding status a mid-round push demotes `confirmed` to (issue #82). The round
# verified its findings against the head it pinned at dispatch; a commit pushed while
# the finders ran may have addressed them, so the finding keeps rendering and keeps
# being recalled — it just stops claiming an authority the verification no longer has.
POSSIBLY_ADDRESSED = "possibly addressed"

STALE_NOTE = (
    "the PR head advanced while this round ran and the new commits touch this finding's "
    "region — verified against the superseded head, so it may already be addressed"
)

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


# ── reading the report step's output (protoAgent#2439) ───────────────────────
# The raw output is NOT the review, and this module never treats it as such. It arrives
# wrapped in the engine's delegation banner (`[review-synthesizer completed: …]`, meant
# for an agent reading a delegation result, not for a PR) and — when the serving lane's
# reasoning parser doesn't split deliberation onto the native `reasoning` channel
# (homelab-iac#219) — with the model's entire chain-of-thought in `content` ahead of the
# answer. #2439 is what that looked like published: a full "Actually, let me
# reconsider…" monologue, including a DRAFT dispositions block, on a public PR.
#
# The fix is not to cut the preamble off. A cut has to decide where the answer starts,
# so it fails OPEN the moment the model doesn't cooperate — and non-cooperation is the
# whole failure mode here. The body is instead BUILT from three parsed blocks (brief,
# dispositions, findings); text outside them is never published because no code path
# publishes it. That holds whatever the lane does with `content`.
_HARDSTOP_RE = re.compile(r"\A\[[^\]\n]*\bhard-stopped at max_turns:[^\]\n]*\]")

_BRIEF_OPEN_FALLBACK, _BRIEF_CLOSE_FALLBACK = "<!-- brief -->", "<!-- /brief -->"

# A brief is 3-6 lines. The cap is a backstop against a model that dumps its whole
# deliberation INSIDE the delimiters — bounded, so the failure is a truncated brief
# rather than an unbounded one.
_BRIEF_LIMIT = 4000


def _brief_delimiters() -> tuple[str, str]:
    """The host's delimiters, so the two can't drift; the literals when there's no host
    (this module is imported host-free by the test suite, like `_parse_findings`)."""
    try:
        from graph.review.findings import BRIEF_CLOSE, BRIEF_OPEN

        return str(BRIEF_OPEN), str(BRIEF_CLOSE)
    except Exception:  # noqa: BLE001 — host-free fallback
        return _BRIEF_OPEN_FALLBACK, _BRIEF_CLOSE_FALLBACK


def _clean_brief(text: str) -> str:
    """Bound what the one free-text field can do.

    The brief is the only model-authored prose the body carries, and finder reports
    quote untrusted PR content into the panel, so it is reachable by anything a PR
    author can write. Two things it must not be able to do:

    - **Carry a fenced JSON array.** `extract_findings_json` reads this round's
      findings back off the posted body for next-round recall; an array in the brief
      is a second candidate for that read.
    - **Carry an HTML comment.** The verdict marker IS an HTML comment, and promotion
      dedup/round history parse it off the body. Neutering `<!--` outright is one rule
      instead of a blocklist, and a real brief has no reason to contain one.
    """
    out = re.sub(r"```.*?```", "", text or "", flags=re.DOTALL).replace("```", "")
    out = out.replace("<!--", "&lt;!--").strip()
    return (out[: _BRIEF_LIMIT - 1] + "…") if len(out) > _BRIEF_LIMIT else out


def extract_brief(output: str) -> tuple[str, bool]:
    """`(brief, found)` — the prose brief from BETWEEN its delimiters.

    Takes the LAST opener: a model that drafts its answer inside its deliberation emits
    the block twice, and the final one is the real deliverable.

    An opener with no closer is bounded at the next fence rather than run to EOF — an
    unterminated brief must not swallow the findings JSON (or any thinking that follows
    it). No opener at all ⇒ `("", False)`, and the caller says so in the body: an
    unreadable brief is a visibly missing brief, never a silent fallback to raw text.
    """
    text = output or ""
    open_d, close_d = _brief_delimiters()
    start = text.rfind(open_d)
    if start == -1:
        return "", False
    start += len(open_d)
    end = text.find(close_d, start)
    if end == -1:
        fence = text.find("```", start)
        end = fence if fence != -1 else len(text)
    return _clean_brief(text[start:end]), True


def report_hard_stopped(output: str) -> bool:
    """Did the report step get cut off at max_turns? The engine says so in a banner it
    prepends to the output. Nothing else surfaces it — `degraded`/`complete` cover finder
    timeouts, not a truncated synthesis — and since the body no longer echoes any raw
    text, this would otherwise be lost silently. Anchored, because the engine prepends
    it: a model quoting the phrase mid-report cannot fake one."""
    return bool(_HARDSTOP_RE.match(output or ""))


def verdict_for(findings: list[dict]) -> str:
    """The pure mapping. `findings` are ADR 0077 dicts (post-report: refuted already dropped)."""
    worst = PASS
    for f in findings:
        sev = str(f.get("severity") or "").lower()
        verdict = str(f.get("verdict") or "").lower()
        if sev in ("blocker", "major"):
            if verdict in ("uncertain", POSSIBLY_ADDRESSED):
                # Unproven, or verified against a head the PR has since replaced —
                # worth a human glance, never a block.
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
_VERDICT_MARK = {
    "confirmed": "confirmed",
    "uncertain": "⚠️ uncertain",
    "refuted": "~~refuted~~",
    POSSIBLY_ADDRESSED: "⏳ possibly addressed",
}


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


_DISPOSITION_MARK = {"fixed": "✅", "open": "🔴", "refuted": "🚫"}


def render_dispositions_table(rows: list[dict]) -> str:
    """What the panel says happened to each prior blocker/major. Rendered from the parsed
    rows rather than by echoing the model's fenced block — nothing reads dispositions back
    off a posted body (`parse_dispositions` runs on the raw output), so the body carries
    the human form only, and the findings array stays the one JSON block in it."""
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return ""
    out = ["| | Prior finding | Disposition | Why |", "|---|---|---|---|"]
    for r in rows:
        disp = str(r.get("disposition") or "").lower()
        prior = r.get("prior") or r.get("file") or "(unanchored)"
        out.append(
            f"| {_DISPOSITION_MARK.get(disp, '•')} | `{_cell(prior, 80)}` | {disp or '?'} | {_cell(r.get('why'))} |"
        )
    return "\n".join(out)


def render_findings_block(findings: list[dict]) -> str:
    """The findings as a table plus the machine-readable array in a collapsed <details>.

    The array is not decoration: `extract_findings_json` reads it back off the posted
    body, which is how prior-round recall and `panel_rounds` reconstruct what a round
    found (ADR 0078 D5 — GitHub is the store). It is emitted even when empty, so a clean
    round records an explicit `[]` rather than an absence the next round has to guess at.
    """
    payload = json.dumps(findings, indent=2)
    collapsed = f"<details>\n<summary>findings JSON (machine-readable)</summary>\n\n```json\n{payload}\n```\n</details>"
    if not findings:
        return f"_No findings — the review came back clean._\n\n{collapsed}"
    return f"### Findings\n\n{render_findings_table(findings)}\n\n{collapsed}"


CARRIED_NOTE = (
    "carried from a prior round — a confirmed blocker/major this round neither fixed nor "
    "refuted (protoAgent#2283); it keeps gating until positively cleared"
)


def _carry_key(finding: dict) -> tuple[str, object]:
    """The (file, line) a carried finding dedups on — so a round that DOES re-report the
    bug doesn't record it twice."""
    return (_norm_path(str(finding.get("file") or "")), finding.get("line"))


def merge_carried_findings(findings: list[dict], carried: list[dict]) -> list[dict]:
    """Add recovered prior blocker/major findings to the RECORDED findings list, so the
    debt survives into the posted body — the store the next round recalls from (ADR 0078
    D5). Returns the list unchanged when there's nothing to carry.

    `unaccounted_priors` recovers a still-open prior blocker/major every round, but it only
    rendered a PROSE note; the machine record (the findings array) kept whatever severity
    THIS round used. A round that de-escalated a major to a minor therefore laundered it out
    of history: `panel_rounds` rebuilds each round's findings from this array, and the
    guards consult only the last substantive round — so the original major was never seen
    again (protoAgent#2283 r3: a clean PASS emitted on two live bugs, two rounds after they
    were confirmed). Injecting the carried findings makes the block durable — it re-appears
    every round until a delta-verified `fixed` or a `refuted` disposition removes it from
    `unaccounted_priors`, at which point it stops being carried and ages out.

    Deduped against findings this round already reports at the same file:line. Marked
    `carried: true` and re-annotated `verdict: confirmed`: an unproven downgrade does not
    un-confirm a finding the panel previously confirmed, and `verdict_for` must FAIL on it
    if the next round recalls it into its live findings."""
    existing = [f for f in (findings or []) if isinstance(f, dict)]
    if not carried:
        return existing
    seen = {_carry_key(f) for f in existing}
    additions: list[dict] = []
    for finding in carried:
        key = _carry_key(finding)
        if key in seen:
            continue
        seen.add(key)
        item = {k: finding.get(k) for k in ("file", "line", "severity", "claim") if finding.get(k) is not None}
        item.setdefault("severity", "major")
        item["verdict"] = "confirmed"
        item["carried"] = True
        note = str(finding.get("note") or "").strip()
        item["note"] = f"{note} — {CARRIED_NOTE}" if note else CARRIED_NOTE
        additions.append(item)
    return existing + additions


def demote_stale_findings(findings: list[dict], ranges: dict[str, list[tuple[int, int]]]) -> tuple[list[dict], int]:
    """(findings, demoted). Strip `confirmed` authority from findings whose region the
    PR rewrote while the panel was still running (issue #82).

    The round verified against the head it pinned at dispatch; a fix pushed mid-round
    makes those findings claims about code the PR no longer has, and posting them as
    `confirmed` misleads the human reader (protoAgent#2854 r2 and #2868 r2 — one of
    them on an already-merged PR). Only findings the delta actually touches are demoted
    — a finding on untouched code is exactly as true at the new head as the old — and a
    `refuted` row is left alone (no authority left to strip). `ranges` comes from the
    pinned→current compare, same `in_delta` semantics as convergence: a file-level
    finding (no line) on a touched file counts as touched.

    Returns NEW dicts, never mutating the caller's (the `merge_carried_findings`
    contract). The demoted status lands in the recorded findings array, so the next
    round's recall re-verifies it rather than trusting it.
    """
    from .rounds import in_delta  # lazy — rounds imports this module at import time

    out: list[dict] = []
    demoted = 0
    for finding in findings or []:
        if not isinstance(finding, dict):
            out.append(finding)
            continue
        verdict = str(finding.get("verdict") or "").lower()
        if verdict == "refuted" or not in_delta(finding, ranges):
            out.append(finding)
            continue
        item = dict(finding)
        item["verdict"] = POSSIBLY_ADDRESSED
        note = str(item.get("note") or "").strip()
        item["note"] = f"{note} — {STALE_NOTE}" if note else STALE_NOTE
        out.append(item)
        demoted += 1
    return out, demoted


def render_verdict_body(
    *,
    repo: str,
    pr: int,
    head_sha: str,
    verdict: str,
    findings: list[dict],
    shadow: bool,
    recipe: str,
    brief: str = "",
    brief_found: bool = True,
    dispositions: list[dict] | None = None,
    truncated: bool = False,
    confined: list[dict] | None = None,
    notes: str = "",
    complete: bool = True,
    stale_note: str = "",
) -> str:
    """The comment body, ASSEMBLED — marker line (machine), header (human), the brief,
    the dispositions table, the findings table + machine-readable array, then the
    footnotes. Plus a confinement footnote when findings were excluded: the recorded
    array still shows them, so the reader needs to see why the verdict ignored them.

    Nothing here interpolates raw model output. `brief` is the one model-authored field
    and it arrives already extracted and bounded (`extract_brief`), which is what makes
    a leaked chain-of-thought unpublishable rather than merely trimmed (protoAgent#2439).
    `brief_found=False` renders that absence explicitly — a review whose brief could not
    be read says so, instead of quietly shipping less than it looks like it shipped.

    `notes` is a pre-rendered trailing section (the convergence checklist, issue #23);
    it arrives as text so this module stays free of the round machinery that builds it.

    `stale_note` is the stale-head synthesis header (issue #82) — the PR moved while
    the panel ran, or its current head could not be checked. It leads the human-readable
    body: everything below it was verified against the head the MARKER names, and the
    reader must know that before reading a single finding."""
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
    # `complete=false` records that a finder meant to run didn't (protoPatch down, a
    # finder timeout) — the promotion gate reads it to refuse auto-approve on a clean
    # verdict over incomplete coverage (#49). Emitted only when incomplete, so a normal
    # marker is unchanged and an older marker parses as complete by default.
    marker = f"<!-- protoagent-qa-review head={head_sha} verdict={verdict} promoted=false"
    if not complete:
        marker += " complete=false"
    marker += " -->"
    sections = [
        f"{marker}\n## QA panel review — **{verdict}**\n_{recipe} · head `{head_sha[:12]}` · {mode}_",
    ]
    if stale_note:
        sections.append(f"> ⚠️ {stale_note}")
    if truncated:
        sections.append("> ⚠️ The report pass was cut off at its turn limit — this round's findings may be incomplete.")
    if brief:
        sections.append(brief)
    elif not brief_found:
        sections.append(
            "_The panel's brief could not be read from this round's report (no delimited brief "
            "block). The findings below are unaffected._"
        )
    if dispositions:
        sections.append(f"### Prior requests\n\n{render_dispositions_table(dispositions)}")
    sections.append(render_findings_block(findings))
    return "\n\n".join(s for s in sections if s) + f"{footnote}{notes}"


def parse_verdict_marker(body: str) -> dict | None:
    """{'head', 'verdict', 'promoted'} from a posted body, or None if it isn't ours."""
    m = _MARKER_RE.search(body or "")
    if not m:
        return None
    # `complete` is a trailing attribute (absorbed by the marker's key=value tail); read
    # it out of the matched marker text. Absent ⇒ True (markers predate the attribute and
    # a plain review IS complete) — only an explicit `complete=false` withholds promotion.
    complete = "complete=false" not in m.group(0)
    return {
        "head": m.group("head"),
        "verdict": m.group("verdict"),
        "promoted": m.group("promoted") == "true",
        "complete": complete,
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

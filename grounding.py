"""Evidence grounding: a finding that quotes code which isn't there can't gate (issue #25).

The verify pass exists to kill plausible-but-wrong findings, and twice on 2026-07-22 it
did the opposite — it *confirmed* claims about code that does not exist at the reviewed
head, laundering a hallucination into a blocking verdict:

  protoAgent#2138  "`_writable_dir()` constructs `Path(str(configured))` but drops the
                   `.expanduser()` call" — the head contains `Path(configured).expanduser()`
                   verbatim and no `Path(str(configured))` anywhere. Confirmed on TWO
                   consecutive heads, escalated major -> blocker, and the round-2 body
                   ACKNOWLEDGED that the quoted hunk was absent from the diff before
                   confirming anyway. The operator's blob refutation and a CI-green test
                   asserting the behaviour were both already on the PR.

That last detail is why this lives in code and not only in the prompts. The panel was not
missing the evidence; it was *discounting evidence already in view*. Prompt discipline
made a promise it demonstrably cannot keep alone — the same lesson `confine_findings` drew
about in-diff scope, applied to the evidence itself.

WHAT THIS CATCHES, precisely: a finding whose quoted code appears NOWHERE in the file at
the reviewed head, nor in the PR's own patch for that file. That is the fabricated-quote
class. It does NOT catch a finding that quotes real code and reasons wrongly about it —
protoAgent#2150 quoted `any_prefix = f"{name}."` accurately and then claimed it matches
`"developer.env.TOKEN"` (it does not; the fourth character is `e`, not `.`). Claims that
are decidable string predicates need evaluation, not substring lookup, and that half stays
with the verify prompt.

Posture is fail-OPEN at every step, because a false downgrade silences a real defect:
unreadable source, no quotable evidence, or any one quote that DOES match — all leave the
finding untouched. Only when every checkable quote is absent does the finding lose its
gating power, and even then it is downgraded (`verdict: uncertain`, which ADR 0078 D3
already forbids from carrying a FAIL alone), never dropped: it still posts, still reads,
still gets a human's judgement. A hallucination that merely stops blocking is handled; a
real finding that gets deleted is not recoverable.
"""

from __future__ import annotations

import re

# Backtick spans (single or triple) — how the findings contract renders quoted code.
_TICKS_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```|`([^`\n]+)`", re.DOTALL)

# A quote must be long enough that finding it is evidence of anything. Short spans
# (`deps`, `name`) appear in every file and would ground a fabrication by accident.
MIN_QUOTE_CHARS = 14

# ...and must look like CODE. Prose in backticks ("the `running` state") is a naming
# reference, not a claim about text present in the file.
_CODE_HINT_RE = re.compile(r"[=(){}\[\];]|\.\w|->|=>|::|\w_\w")

# ...and must be STATEMENT-like: containing whitespace between tokens. A bare
# `_writable_dir()` is a reference to a thing, not an assertion about the file's text —
# and it is nearly always present, so counting it would ground a fabrication by
# association. protoAgent#2138 quoted the (real) function name alongside the (invented)
# `writable = Path(str(configured))`; only the latter is a claim this module can test.
_STATEMENT_RE = re.compile(r"\S\s+\S")

# Diff decorations the panel copies into evidence; stripped before matching so a quote
# lifted from a patch hunk still matches the file's own text.
_DIFF_PREFIX_RE = re.compile(r"^[+\-]\s?", re.MULTILINE)


def _normalize(text: str) -> str:
    """Collapse whitespace so indentation and wrapping never decide groundedness."""
    return re.sub(r"\s+", " ", _DIFF_PREFIX_RE.sub("", text)).strip()


def quoted_snippets(finding: dict) -> list[str]:
    """Checkable code quotes from a finding's `claim` + `evidence`, normalized.

    Only spans that are long enough AND look like code survive — everything else is
    prose, and prose is not a claim about what the file contains.
    """
    blob = f"{finding.get('claim') or ''}\n{finding.get('evidence') or ''}"
    out: list[str] = []
    for fenced, inline in _TICKS_RE.findall(blob):
        raw = fenced or inline
        text = _normalize(raw)
        if len(text) >= MIN_QUOTE_CHARS and _CODE_HINT_RE.search(text) and _STATEMENT_RE.search(text):
            out.append(text)
    return out


def ground_finding(finding: dict, source: str | None) -> tuple[bool, list[str]]:
    """(grounded?, quotes that were absent). `source` should be the file at the reviewed
    head PLUS the PR's patch for it — a removed-behaviour finding legitimately quotes
    code the head no longer has, and must not be downgraded for being right."""
    quotes = quoted_snippets(finding)
    if source is None or not quotes:
        return True, []  # nothing to check against, or nothing checkable — fail open
    haystack = _normalize(source)
    missing = [q for q in quotes if q not in haystack]
    if len(missing) < len(quotes):
        return True, []  # at least one quote landed — the finding is anchored in reality
    return False, missing


UNGROUNDED_NOTE = "evidence not found at the reviewed head — downgraded to uncertain, cannot gate a merge (issue #25)"


def apply_grounding(findings: list[dict], sources: dict[str, str | None]) -> tuple[list[dict], list[dict]]:
    """(findings, downgraded). Every finding whose quoted code is absent from its file at
    the reviewed head is annotated `verdict: uncertain` — which `verdict_for` already
    refuses to turn into a FAIL — and carries a note saying why.

    Findings are never removed. The report's own JSON still shows them and the posted body
    footnotes the downgrade, so a human can always overrule the machine: the failure mode
    this guards against is a fabrication that BLOCKS, not a fabrication that is visible.
    """
    out: list[dict] = []
    downgraded: list[dict] = []
    for finding in findings:
        file = str(finding.get("file") or "")
        grounded, missing = ground_finding(finding, sources.get(file))
        if grounded:
            out.append(finding)
            continue
        annotated = dict(finding)
        annotated["verdict"] = "uncertain"
        note = str(annotated.get("note") or "").strip()
        annotated["note"] = f"{note} — {UNGROUNDED_NOTE}" if note else UNGROUNDED_NOTE
        out.append(annotated)
        downgraded.append({"file": file, "severity": str(finding.get("severity") or ""), "missing": missing[:3]})
    return out, downgraded


def render_grounding_footnote(downgraded: list[dict]) -> str:
    """The posted-body note for downgraded findings — the verdict must never silently
    disagree with the report, the same contract the confinement footnote keeps."""
    if not downgraded:
        return ""
    lines = "\n".join(
        f"- `{d['file'] or '(no file)'}` ({d['severity'] or '?'}) — quoted evidence not found at this head: "
        + "; ".join(f"`{m[:120]}`" for m in d["missing"])
        for d in downgraded
    )
    return (
        f"\n\n---\n_{len(downgraded)} finding(s) downgraded to **uncertain**: the code they quote as evidence "
        f"does not appear in the file at the reviewed head, nor in this PR's patch for it. A finding that "
        f"cannot be grounded does not gate a merge (issue #25) — it still stands for a human to judge._\n{lines}"
    )

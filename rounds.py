"""Re-review convergence: round history, prior-request memory, delta scoping (issue #23).

A re-review used to be memoryless in the way that matters. The dispatcher recalled the
PREVIOUS review's findings, and the panel then re-read the WHOLE PR diff — so every
delta the review itself caused became fresh review surface, and code the panel had
already blessed came back around on a later draw. projectBoard-plugin#88 ran eight
rounds on a small store fix: round 6 flagged the normalization round 3 demanded, and
round 8 flagged CLI-argument duplication untouched since round 1. Every finding was
individually confirmed and individually reasonable; collectively they never converged.

Three pieces, all pure — the dispatcher does the GitHub reads and passes facts in:

  `panel_rounds`          the PR's review history as ROUNDS. Promotion bodies carry our
                          marker but no findings, and a re-gate re-posts an existing
                          verdict body verbatim — neither is a round, and both used to
                          shadow the real one.
  `render_prior_requests` every prior round's findings as one wrapped data block, so a
                          finder can see that a line it is about to flag exists BECAUSE
                          the panel asked for it. A panel-requested change is verified
                          as implemented-correctly, not re-litigated as novel.
  `converge`              the exit rule. From round N, a WARN whose findings are all
                          minor/nit AND all anchored to lines that moved since the last
                          reviewed head becomes PASS-with-notes: the notes still post,
                          they just stop holding the verdict.

Convergence relief is fail-CLOSED, matching the rest of this plugin: an unreadable
compare, an uncertain major, a finding outside the delta — any of them and the WARN
stands. The rule only ever releases a verdict the panel already judged non-blocking;
a blocker/major FAIL converges never, however many rounds it takes (a defect a fix
introduced is still a defect — see #88 rounds 4 and 7).
"""

from __future__ import annotations

import json
import re

from .verdicts import PASS, WARN, extract_findings_json

# From this round on, the convergence rule is eligible to fire. Rounds 1–2 are the
# review doing its job; #88's loop only became self-referential at round 3+.
DEFAULT_CONVERGENCE_ROUNDS = 3

# A fix rarely lands on exactly the flagged line — the hunk that answers a finding
# drifts by a few lines as code moves. Padding the delta ranges keeps the rule from
# failing on an off-by-three.
DELTA_CONTEXT_LINES = 5

MAX_REQUESTS_PER_ROUND = 20
MAX_CLAIM_CHARS = 300

_WRAPPER_TAGS = ("prior_requests", "round", "request")
_CLOSING_TAG_RE = re.compile(r"</\s*(" + "|".join(_WRAPPER_TAGS) + r")\s*>", re.IGNORECASE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)

_RELIEVABLE = ("minor", "nit")


def _escape(text: str) -> str:
    """Neutralize wrapper closing tags (whitespace-tolerant), same discipline as
    `threads._escape`: finding claims quote diff text, which anyone who can open a
    PR writes — a claim must never terminate the data block early."""
    return _CLOSING_TAG_RE.sub(lambda m: f"</{m.group(1).lower()}_>", text)


def panel_rounds(reviews: list[dict]) -> list[dict]:
    """Our posted reviews → the ROUNDS the panel actually spent, oldest→newest.

    `[{head, verdict, findings: [...]}]`, one entry per reviewed head. Two kinds of
    marker-bearing review are NOT rounds and must not be counted or recalled from:

      - promotions (`promoted=true`) hold an approval line, no findings. Taking the
        newest marker-bearing review as "the prior review" (the old behaviour) meant
        that after any approve-on-green, the next round recalled a body with no
        findings JSON at all — `prior_findings` came through EMPTY and the delta
        re-review silently degraded to a cold first review. On #88 that hit rounds
        4, 6, 7 and 8, which is most of the loop.
      - a re-gate re-posts an earlier verdict body verbatim to arm the block, so the
        same head appears twice with identical findings; deduping by head (keeping
        the latest) stops one head inflating the round count.
    """
    by_head: dict[str, dict] = {}
    for review in reviews or []:
        if not isinstance(review, dict) or review.get("promoted"):
            continue
        head = str(review.get("head") or "")
        if not head:
            continue
        findings = []
        text = extract_findings_json(str(review.get("body") or ""))
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = []
            findings = [f for f in parsed if isinstance(f, dict)] if isinstance(parsed, list) else []
        by_head[head] = {"head": head, "verdict": str(review.get("verdict") or ""), "findings": findings}
    return list(by_head.values())


def render_prior_requests(rounds: list[dict]) -> str:
    """The `<prior_requests>` data block: what THIS panel has already asked for.

    Distinct from `prior_findings` (the previous round's open items, for drop/carry
    triage). This is the whole history, round-numbered, and it exists to answer a
    different question — "did we ask for this?" A finder that can see round 3 demanded
    the normalization does not report it as an unrequested behavioral change in round 6.
    """
    numbered = [(i + 1, r) for i, r in enumerate(rounds or []) if isinstance(r, dict) and r.get("findings")]
    if not numbered:
        return ""
    out = ["<prior_requests>"]
    for number, round_ in numbered:
        out.append(f'  <round number="{number}" verdict="{_attr(round_.get("verdict"))}">')
        for finding in round_["findings"][:MAX_REQUESTS_PER_ROUND]:
            severity = _attr(finding.get("severity"))
            location = str(finding.get("file") or "")
            line = finding.get("line")
            if isinstance(line, int):
                location = f"{location}:{line}"
            claim = str(finding.get("claim") or "")[:MAX_CLAIM_CHARS]
            out.append(f'    <request severity="{severity}" location="{_attr(location)}">')
            out.append(_escape(claim))
            out.append("    </request>")
        out.append("  </round>")
    out.append("</prior_requests>")
    return "\n".join(out)


def _attr(value: object) -> str:
    return str(value or "").replace('"', "&quot;").replace(">", "&gt;").replace("<", "&lt;")


def delta_ranges(compare_files: list[dict]) -> dict[str, list[tuple[int, int]]]:
    """Changed line ranges per file from a compare payload's `patch` hunks.

    Ranges are in HEAD-side line numbers (the side a finding cites), padded by
    `DELTA_CONTEXT_LINES`. A file present with no readable patch (binary, or GitHub
    truncated it) maps to `[]` — known-changed, unknown where; `in_delta` treats that
    as whole-file, since the alternative is refusing relief on a file we know moved.
    """
    ranges: dict[str, list[tuple[int, int]]] = {}
    for entry in compare_files or []:
        if not isinstance(entry, dict):
            continue
        path = _norm(str(entry.get("filename") or ""))
        if not path:
            continue
        spans = ranges.setdefault(path, [])
        for match in _HUNK_RE.finditer(str(entry.get("patch") or "")):
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            if count <= 0:  # a pure deletion hunk — the surrounding lines are the delta
                spans.append((max(1, start - DELTA_CONTEXT_LINES), start + DELTA_CONTEXT_LINES))
                continue
            spans.append((max(1, start - DELTA_CONTEXT_LINES), start + count - 1 + DELTA_CONTEXT_LINES))
    return ranges


def _norm(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path.removeprefix("/")


def in_delta(finding: dict, ranges: dict[str, list[tuple[int, int]]]) -> bool:
    """Does this finding anchor to code that moved since the last reviewed head?

    A file that isn't in the delta at all is untouched code — round 8's finding on
    argument construction unchanged since round 1 lands here, and blocks convergence,
    which is right: that one is about the PR, not about the review's own churn.
    """
    spans = ranges.get(_norm(str(finding.get("file") or "")))
    if spans is None:
        return False
    if not spans:  # changed file, unreadable patch — whole-file
        return True
    line = finding.get("line")
    if not isinstance(line, int):  # file-level finding on a changed file
        return True
    return any(start <= line <= end for start, end in spans)


def converge(
    verdict: str,
    findings: list[dict],
    *,
    round_number: int,
    ranges: dict[str, list[tuple[int, int]]] | None,
    threshold: int = DEFAULT_CONVERGENCE_ROUNDS,
) -> tuple[str, list[dict], str]:
    """(verdict, notes, reason). The exit rule — pure, like `verdict_for`.

    `ranges=None` means the compare was unreadable: no relief (an unreadable delta must
    never launder a WARN into a PASS, the same posture `confine_findings` takes with an
    unreadable file list). `threshold=0` disables the rule entirely.

    On relief the findings come back as `notes`: they still render, still post, still
    get read — they just stop being verdict-bearing, which is the whole point. Nothing
    is dropped or hidden.
    """
    if threshold <= 0:
        return verdict, [], "disabled"
    if verdict != WARN:
        # PASS needs no relief; FAIL is a blocker/major and never converges.
        return verdict, [], "not-warn"
    if round_number < threshold:
        return verdict, [], f"round-{round_number}-below-{threshold}"
    if not findings:
        return verdict, [], "no-findings"
    if ranges is None:
        return verdict, [], "delta-unreadable"
    for finding in findings:
        if str(finding.get("severity") or "").lower() not in _RELIEVABLE:
            # An "uncertain" major also lands on WARN — it is not a nit, and a round
            # budget must not retire it.
            return verdict, [], "non-minor-finding"
        if not in_delta(finding, ranges):
            return verdict, [], "finding-outside-delta"
    return PASS, list(findings), f"converged-round-{round_number}"


def render_notes_section(notes: list[dict]) -> str:
    """The follow-up checklist appended to a converged body — findings the verdict
    stopped carrying, in the form someone can actually act on later."""
    if not notes:
        return ""
    lines = "\n".join(
        f"- [ ] `{n.get('file') or '(no file)'}"
        + (f":{n['line']}" if isinstance(n.get("line"), int) else "")
        + f"` ({n.get('severity') or '?'}) — {str(n.get('claim') or '')[:200]}"
        for n in notes
    )
    return (
        "\n\n---\n**Converged — the following are notes, not gates.** Every finding below "
        "is minor/nit and lands on code that changed in response to this panel's own "
        "earlier rounds, so the verdict no longer holds on them (issue #23). They are "
        "worth doing; they are not worth another review round:\n"
        f"{lines}"
    )

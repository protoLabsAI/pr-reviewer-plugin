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

import difflib
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


def render_findings_block(findings: list[dict], *, covered: bool = True) -> str:
    """The findings as a table plus the machine-readable array in a collapsed <details>.

    The array is not decoration: `extract_findings_json` reads it back off the posted
    body, which is how prior-round recall and `panel_rounds` reconstruct what a round
    found (ADR 0078 D5 — GitHub is the store). It is emitted even when empty, so a clean
    round records an explicit `[]` rather than an absence the next round has to guess at.

    `covered=False` (a lane did not deliver a full pass, #117) withholds the "came back
    clean" line: zero findings from part of the panel is not a clean review.
    """
    payload = json.dumps(findings, indent=2)
    collapsed = f"<details>\n<summary>{_RECORD_SUMMARY}</summary>\n\n```json\n{payload}\n```\n</details>"
    if not findings:
        if not covered:
            return (
                "_No findings from the lanes that ran — coverage was incomplete (see above), so "
                f"this is not a clean review._\n\n{collapsed}"
            )
        return f"_No findings — the review came back clean._\n\n{collapsed}"
    return f"### Findings\n\n{render_findings_table(findings)}\n\n{collapsed}"


CARRIED_NOTE = (
    "carried from a prior round — a confirmed blocker/major this round neither fixed nor "
    "refuted (protoAgent#2283); it keeps gating until positively cleared"
)


SAME_DEFECT_RATIO = 0.8  # claim similarity (SequenceMatcher) that reads as the same defect …
SAME_DEFECT_LINES = 25  # … at a line that MOVED, not anywhere in the file


_IDENTIFIER_TOKEN = re.compile(r"[A-Za-z_][\w.]*\(\)|[\w.-]*[_./:\[\]][\w.\-/:\[\]()]*|\b\d+\b|\b[a-z]+[A-Z]\w*")


def identifier_tokens(claim: str) -> frozenset[str]:
    """The tokens in a claim that name a THING — `list_users()`, `scripts/x.sh`, `foo_bar`,
    `Cargo.lock`, `v1`, `camelCase`, a bare number — as opposed to its prose. Two claims
    about different sites differ exactly here, however much boilerplate they share."""
    return frozenset(t.strip(".,;:").lower() for t in _IDENTIFIER_TOKEN.findall(str(claim or "")) if t.strip(".,;:"))


def _same_defect(a: dict, b: dict) -> bool:
    """Same file, a line within a few dozen of the original, a near-identical claim — and
    the same things named. The same defect in other words at a moved line passes all
    four; two sibling defects with template claims ("SQL built by concatenation in
    list_users()" / "… in delete_user()") share the boilerplate but name different
    things, and stay distinct however close they sit. Accepted residual: two aspects of
    ONE identifier at the same spot, worded alike, merge — the fresh row (with its own
    verdict) supersedes the carried one; a real defect is never invented, at worst one
    carried row hides behind a fresh row about the same thing. Fails closed on an empty
    claim or a missing line on either side."""
    if _norm_path(str(a.get("file") or "")) != _norm_path(str(b.get("file") or "")):
        return False
    try:
        la, lb = int(a.get("line")), int(b.get("line"))
    except (TypeError, ValueError):
        return False
    if abs(la - lb) > SAME_DEFECT_LINES:
        return False
    ca = " ".join(str(a.get("claim") or "").lower().split())
    cb = " ".join(str(b.get("claim") or "").lower().split())
    if not ca or not cb:
        return False
    if identifier_tokens(ca) != identifier_tokens(cb):
        return False
    return difflib.SequenceMatcher(None, ca, cb).ratio() >= SAME_DEFECT_RATIO


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
        # The same defect re-raised at a moved line, in other words (#185): a re-review
        # whose head advanced mid-round raised "the stack project name is taken from the
        # raw directory basename, so for stacks/roxy…" beside the carried "…basename
        # without lowercasing, so fo…" — 8 rows for 5 findings. A near-identical claim on
        # the same file is this round accounting for the carried one; the fresh row, with
        # its own (demoted) verdict, supersedes it.
        if any(_same_defect(finding, f) for f in existing):
            continue
        seen.add(key)
        # `since` rides along so the next round can prove a fix against the head this was
        # raised at, not merely against the round that carried it (issue #131).
        item = {k: finding.get(k) for k in ("file", "line", "severity", "claim") if finding.get(k) is not None}
        if finding.get("since"):
            item["since"] = str(finding["since"])
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


NOTHING_TO_VERIFY = "VERIFY_STATUS: nothing-to-verify"
VERIFY_GAP_PREFIX = "VERIFY_GAP:"

FINDER_REVIEWED = "FINDER_STATUS: reviewed"
FINDER_BLOCKED_PREFIX = "FINDER_STATUS: blocked"
_FINDER_STATUS_LINE = re.compile(r"^\s*FINDER_STATUS:\s*\S", re.MULTILINE)

# ── reading fenced blocks ─────────────────────────────────────────────────────
# A fence does NOT close at the first ``` anywhere. The old pattern (`.*?```) ended the
# block at the first triple backtick it met, including one INSIDE a JSON string: a finding
# quoting a reST-style docstring (``tests/x.py`` wrapped in a markdown code span) puts three
# backticks in a row mid-string, the block was cut there, the JSON failed to parse, and the
# stage read as "undelivered". A complete, correctly verified round was discarded and
# retried (protoPatch#13) — and because the text is deterministic, such a PR could never
# get a verdict however often it was summoned.
#
# So the close is chosen PER FENCE, by what it yields: the first ``` after the opener whose
# body is valid JSON ends the block. A ``` inside a string leaves a body that cannot parse
# and is passed over; a fence closed on the payload's own line still parses at its first
# ```, as it always did — and the two shapes can share a text, in either order (one pattern
# for the whole text, with the other as an all-or-nothing fallback, lost a block there).
# A fence holding no JSON at all closes at its first ```, as before.
_FENCE_CLOSE_TRIES = 32  # bounds the scan of a fence that never parses


def fenced_blocks(text: str, *, json_only: bool = False) -> list[str]:
    """The bodies of the fenced code blocks in `text`, in order.

    `json_only` takes ```json fences alone; otherwise an untagged ``` fence counts too.
    """
    text = text or ""
    opener_re = re.compile(r"```json\s*\n" if json_only else r"```(?:json)?\s*\n")
    out: list[str] = []
    pos = 0
    while (opener := opener_re.search(text, pos)) is not None:
        start = opener.end()
        first = close = text.find("```", start)
        if first == -1:
            break  # an unclosed fence is not a block
        for _ in range(_FENCE_CLOSE_TRIES):
            try:
                json.loads(text[start:close])
                break
            except json.JSONDecodeError:
                close = text.find("```", close + 3)
                if close == -1:
                    break
        else:
            close = -1
        if close == -1:
            close = first
        out.append(re.sub(r"\n[ \t]*\Z", "", text[start:close]))
        pos = close + 3
    return out


def finder_completed(output: str) -> bool:
    """Did this LLM finder step actually complete a real pass over the code?

    Issue #117: a finder that hits a wall partway through — every file read
    404ing, an early crash, exhausting its turn budget without a real answer —
    used to look IDENTICAL to one that looked and genuinely found nothing: both
    emit an empty findings array, and the engine's own `failed`/`degraded`
    tracking only catches a step the engine itself cut off (a timeout), not one
    that ran to a normal-looking finish on garbage input. Every finder's prompt
    now requires an explicit `FINDER_STATUS: reviewed` marker on completion, so
    its absence — an old-format reply, a crash mid-response, a truncated
    turn-limit exit — or an explicit `FINDER_STATUS: blocked` both mean the pass
    did not happen, the same way `verification_ran` reads the verify step's own
    status line rather than trusting an empty result at face value.
    """
    text = output or ""
    if FINDER_BLOCKED_PREFIX in text:
        return False
    if FINDER_REVIEWED in text:
        return True
    # A status line the recipe did not spell — `FINDER_STATUS: clean` on a full review
    # with an explicit `[]` (#186: two rounds capped WARN complete=false for a coverage gap
    # that did not exist). The marker tells a real pass from a garbage exit; a garbage exit
    # produces neither a status line nor an array, so the pair is the pass, whatever the
    # word. `blocked` above stays the one status that means "did not happen".
    return bool(_FINDER_STATUS_LINE.search(text)) and any(b.lstrip().startswith("[") for b in fenced_blocks(text))


def mentions_any(text: str, markers: str | tuple[str, ...]) -> bool:
    """Is any of `markers` in `text`? A bare string is one marker; empty markers never match."""
    options = (markers,) if isinstance(markers, str) else tuple(markers or ())
    return any(m and m in (text or "") for m in options)


def structural_relay_ok(output: str, unavailable_prefix: str | tuple[str, ...]) -> bool:
    """Did the structural finder actually relay a findings block or a proper Gap?

    Its contract (subagents.py) is narrower than the four LLM finders' — call
    `protopatch_review` once, relay verbatim — so a genuine `PROTOPATCH
    UNAVAILABLE` Gap is a CLEAN degrade (the panel proceeds on four finders, and
    `structural_unavailable` already flags it). But a reply that is neither a
    fenced findings array nor that Gap line — e.g. the relay subagent exhausting
    its turn budget mid-relay on a large findings payload — is a coverage hole
    that the exact-prefix check alone let through (issue #117): it isn't the
    UNAVAILABLE text, so it read as a normal, complete, empty-ish pass.
    """
    text = output or ""
    if mentions_any(text, unavailable_prefix):
        return True
    return bool(fenced_blocks(text, json_only=True))


# ── absent is not empty (issue #113) ───────────────────────────────────────────
# Every panel step is contracted to END with a fenced findings array — `[]` on a clean
# pass. An explicit `[]` and NO array are different facts: the first is "looked, found
# nothing", the second is "nothing reached this boundary" — a finder that died after its
# opening line, a synthesizer that ran out of turns, a report cut off before its JSON.
# `_parse_findings` reads both as `[]`, so an undelivered payload rendered as "came back
# clean" and posted PASS beneath a verify note saying the findings may have been lost
# (#113). This is replay's `looks_truncated` rule, applied at every stage boundary.

# A recipe's review lanes are its `find_*` steps — true of the core four-finder
# `code-review` recipe and of `code-review-structural` alike.
FINDER_STEP_PREFIX = "find_"


def _finding_shaped(item: object) -> bool:
    return isinstance(item, dict) and ("claim" in item or "severity" in item)


def findings_payload_present(output: str) -> bool:
    """Did this step deliver a findings array at all? An empty one counts; none does not.

    A fenced JSON array that is empty or holds at least one finding-shaped object. A
    dispositions block (`prior`/`disposition` rows) is not a findings payload, and nor is
    a stray `[404]` — a step whose only array is one of those delivered no findings.
    Fenced only, as the contract (and replay's `looks_truncated`) has it: a bare `[...]`
    in prose is too easy to hit by accident to count as delivery.
    """
    for block in fenced_blocks(output):
        body = block.strip()
        if not body.startswith("["):
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list) and (not data or any(_finding_shaped(item) for item in data)):
            return True
    return False


def _lane_delivered(output: str, timed_out: bool, unavailable_prefix: str | tuple[str, ...]) -> bool:
    """Did this lane hand the synthesizer a findings payload it stands behind? The
    engine's timeout Gap and the structural relay's UNAVAILABLE Gap both carry a synthetic
    `[]` for a pass that never happened, and a lane that declares `FINDER_STATUS: blocked`
    has said its array covers nothing — none of those is a delivery."""
    if timed_out or FINDER_BLOCKED_PREFIX in output or mentions_any(output, unavailable_prefix):
        return False
    return findings_payload_present(output)


def undelivered_stages(
    output: str, steps: dict | None, degraded: list[str] | None, unavailable_prefix: str | tuple[str, ...]
) -> list[str]:
    """The stage boundaries at which this round's findings payload never arrived, in
    pipeline order — empty when every boundary delivered (an explicit `[]` included).

    - `finders`: NO lane delivered a payload — nothing was reviewed at all. One blind or
      blocked lane among live ones is a coverage gap (WARN, below), not an absent round.
      A missing FINDER_STATUS line alone never makes a lane undelivered: an array without
      the line is still an array, and a model that drops the line must not void reviews.
    - `synthesize`: the merge step emitted no array, so the verifier had nothing to
      annotate — #113 exactly: "the findings were lost before reaching the verifier".
    - `report`: the deliverable carries no array; parsing that as `[]` posts a clean PASS.

    Any of these makes the round incomplete: the dispatcher retries it, then concludes
    with no verdict — it is never rendered as a review that "came back clean". The
    verify boundary needs no rule here: findings it failed to annotate already surface as
    unverified (`verification_ran`), and an un-annotated blocker/major already FAILs. A
    result with no `steps` (an older host) is judged on the report alone.
    """
    steps = steps or {}
    timed_out = {str(s) for s in (degraded or [])}
    stages: list[str] = []
    lanes = [str(s) for s in steps if str(s).startswith(FINDER_STEP_PREFIX)]
    if lanes and not any(_lane_delivered(str(steps.get(s) or ""), s in timed_out, unavailable_prefix) for s in lanes):
        stages.append("finders")
    if "synthesize" in steps and not findings_payload_present(str(steps.get("synthesize") or "")):
        stages.append("synthesize")
    if not findings_payload_present(output):
        stages.append("report")
    return stages


# ── coverage gaps cap a clean PASS (issue #117) ─────────────────────────────────


# A finder that overran the model's context window (pr-reviewer-plugin#176): the provider
# refused the call, the engine recorded a failed step, and the dispatcher retried the
# whole five-finder panel — 7 of 8 panels on one PR died this way, in three different
# lanes. It is the same shape as a lane the engine timed out: ONE lane's coverage is
# missing and the other four delivered. Read as a Gap, not a failed round.
CONTEXT_OVERRUN_MARKERS = ("ContextWindowExceeded", "context_length_exceeded", "maximum context length")


def context_overrun(step_output: str) -> bool:
    """Did this failed step die on the model's context window, not on a real crash?"""
    return mentions_any(str(step_output or ""), CONTEXT_OVERRUN_MARKERS)


def overrun_lanes(failed: list[str] | None, steps: dict | None) -> list[str]:
    """The finder lanes among `failed` whose error is a context overrun — a coverage gap
    the round can carry (like a timed-out lane), not a reason to discard it."""
    steps = steps or {}
    return [
        str(s)
        for s in (failed or [])
        if str(s).startswith(FINDER_STEP_PREFIX) and context_overrun(str(steps.get(s) or ""))
    ]


def coverage_gaps(
    degraded: list[str] | None,
    incomplete_finders: list[str] | None,
    structural_unavailable: bool,
    structural_reason: str = "",
    verify_undelivered: bool = False,
    overran: list[str] | None = None,
) -> dict[str, str]:
    """{lane: why} for every lane that did not deliver a full pass — the signals the
    dispatcher already records (`degraded`, `incomplete_finders`,
    `structural_unavailable`, and a verify step that returned nothing on a clean round),
    as the one record the coverage cap and note read."""
    gaps = {str(s): "hit its time budget" for s in (degraded or [])}
    for s in overran or []:
        gaps[str(s)] = "overran the model's context window — read more than it could hold"
    for s in incomplete_finders or []:
        gaps.setdefault(str(s), "did not complete a real pass")
    if structural_unavailable:
        why = "structural pass unavailable or cut short"
        # The lane's own reason, when it gave one (#140): without it the synthesizer guessed
        # a cause per round — "auth error", "provider error" — for what was one fault, and a
        # reader could not tell a wrong gateway key from an unusable model reply.
        gaps.setdefault("find_structural", f"{why}: {structural_reason}" if structural_reason else why)
    if verify_undelivered:
        gaps.setdefault("verify", "returned no findings array and no status line — the verify pass did not run")
    return gaps


def coverage_verdict(verdict: str, gaps: dict[str, str] | None) -> str:
    """Cap a clean PASS at WARN when any lane did not deliver a full pass (#117).

    Deliberately NOT a FAIL or a withheld verdict: a lane gap says the review covered
    less, not that the code is bad — and the structural lane gaps on most large
    protoAgent reviews today (#119), so blocking on it would block nearly every merge
    there. WARN is the tier the gates already read as "non-blocking, look closer", and
    the coverage note says what to look closer at. Applied by the caller AFTER the pure
    mapping and every history rule, so `verdict_for` stays a function of findings alone
    (ADR 0078 C) and convergence can never relieve the cap back to PASS.
    """
    return WARN if verdict == PASS and gaps else verdict


def render_coverage_note(gaps: dict[str, str] | None, lanes: int = 0) -> str:
    """The code-authored coverage line. The brief is model-written and cannot see the
    lanes — on protoAgent#3494 it said the structural pass completed with no coverage
    gaps while four lanes were blind — so this line, not the brief, is the record."""
    if not gaps:
        return ""
    listed = ", ".join(f"`{sid}` ({why})" for sid, why in gaps.items())
    of = f" of {lanes}" if lanes >= len(gaps) else ""
    return (
        f"**Coverage incomplete — this is not a clean pass.** {len(gaps)}{of} review lane(s) "
        f"did not complete a full pass this round: {listed}. Findings from the lanes that ran "
        "stand, but a defect only the missing lanes would catch may be missed, so a PASS is "
        "capped at WARN. Where the brief below implies full coverage, this line supersedes "
        "it. The next push re-runs the full panel."
    )


def verify_delivered(verify_output: str) -> bool:
    """Did the verify step hand anything back? The one question `verification_ran` cannot
    ask on a clean round (#151).

    With findings, a dead verifier shows: nothing is annotated, so `verification_ran` is
    False. With none, `verification_ran` returns True before it looks at the output — so
    "the verifier had nothing to check" and "the verifier announced its intent and
    stopped" were the same state, and on protoAgent#3564 the second one posted a clean
    PASS above a report body saying the verifier's input never arrived.

    Lenient on purpose: a fenced block that OPENS an array counts even when it is not
    valid JSON (verifier notes carry stray `\'` escapes often enough that strict parsing
    would flag working rounds), and so does either status line. Measured over 112 saved
    zero-finding runs this flags 4 — three verifiers that stopped at a preamble or asked
    to be sent the findings, and one that did the work in prose but returned no array.
    """
    out = verify_output or ""
    if NOTHING_TO_VERIFY in out or VERIFY_GAP_PREFIX in out or re.search(r"VERIFY_STATUS:\s*annotated", out):
        return True
    return any(block.lstrip().startswith("[") for block in fenced_blocks(out))


def restate_findings(findings: list[dict]) -> str:
    """The synthesizer's findings as a verify re-run hands them over (#182): the same
    array, byte-for-byte in substance, preceded by an explicit count and stripped of the
    delegation banner and prose brief. A verifier that answered `nothing-to-verify` over
    the original shape is not shown that shape again."""
    n = len(findings)
    return (
        f"FINDINGS_COUNT: {n}\n"
        f"`nothing-to-verify` is wrong here: {n} finding(s) follow — annotate every one.\n\n"
        "```json\n" + json.dumps(findings, indent=2) + "\n```"
    )


def verifier_contradicts_synthesis(verify_output: str, synthesized: list[dict] | None) -> bool:
    """The verifier says it received NOTHING while the synthesizer handed it findings (#167).

    Mechanical, no judgment: `nothing-to-verify` is defined by the recipe as "the array was
    literally `[]`", so over a non-empty synthesis it is a contradiction — the verifier did
    not read its input (5 of 29 rounds with findings; deterministic enough to hit one PR
    twice). Such a round is not wrong, it is unfinished: the verify step is the one to
    re-run, not the panel.
    """
    return bool(synthesized) and NOTHING_TO_VERIFY in (verify_output or "")


def verification_ran(verify_output: str, findings: list[dict] | None) -> bool:
    """Did the verify pass actually check the findings the panel is reporting?

    The distinction this exists to draw: an empty verify pass on a clean PR is the
    NORMAL result — there was nothing to check — while an empty verify pass over real
    findings means the verdict rests on claims nobody grounded. Those two looked
    identical downstream, so every clean review carried an alarming "no structural
    invariants were independently confirmed" note, which trains a reader to ignore the
    one case where it matters.

    Conservative by construction: unparseable output over real findings reads as
    unverified, because wrongly trusting an unverified PASS merges a defect behind a
    green badge, while wrongly withholding costs one human glance.
    """
    if not findings:
        return True  # nothing to verify is not a failure to verify
    if VERIFY_GAP_PREFIX in verify_output or NOTHING_TO_VERIFY in verify_output:
        # The verifier says it saw nothing while the panel is reporting findings.
        return False
    # EVERY finding, not merely one. A round that annotates some and silently drops the
    # rest reads as verified while carrying unchecked claims beside checked ones — which
    # is how a stale finding survived a round on protoAgent#3113, sitting verdict-less
    # next to a verified peer. Findings carried forward from a prior round are stamped
    # `verdict: confirmed` by `merge_carried_findings`, so legitimate debt does not trip
    # this; only a finding this round's verifier failed to reach does.
    unverified = [f for f in findings if not str(f.get("verdict") or "").strip()]
    if unverified:
        return False
    # And when the verifier states its own coverage, believe it over the annotations: a
    # count short of the findings it was handed is the verifier telling us it ran out.
    m = re.search(r"VERIFY_STATUS:\s*annotated\s+n=(\d+)", verify_output)
    return not (m and int(m.group(1)) < len(findings))


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
    verified: bool = True,
    stale_note: str = "",
    diff_id: str = "",
    coverage_gaps: dict[str, str] | None = None,
    lanes: int = 0,
    reaffirmed_from: str = "",
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
    reader must know that before reading a single finding.

    `coverage_gaps` names the lanes that did not deliver a full pass (#117). Its note sits
    ABOVE the brief for the same reason: the brief cannot see the lanes and has claimed
    full coverage over blind ones, so the reader meets the code-authored record first."""
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
    # `verified=false` records that findings existed but the verify pass annotated none
    # of them — the panel's evidence step did not actually run. Distinct from
    # `complete=false` (a FINDER didn't run): coverage was fine, the checking wasn't.
    # Both withhold auto-approve for the same reason — a clean verdict nobody checked is
    # not a clean verdict. Emitted only when true, so a normal marker is unchanged and an
    # older one parses as verified by default.
    # `diff=<id>` records the identity of the base↔head content this round reviewed
    # (issue #91). A later event whose PR diff hashes to the same id is byte-identical to
    # what this verdict already judged — even across a rebased/reworded head SHA — so it
    # reaffirms rather than re-spends the panel. A tolerated trailing attribute, so an
    # older marker (no `diff=`) simply parses as "no stored identity" and reaffirm fails
    # closed for it. Emitted only when known, so a normal marker is otherwise unchanged.
    marker = f"<!-- protoagent-qa-review head={head_sha} verdict={verdict} promoted=false"
    if not complete:
        marker += " complete=false"
    if not verified:
        marker += " verified=false"
    if diff_id:
        marker += f" diff={diff_id}"
    # `reaffirmed=<head>` marks a verdict CARRIED to this head from the round that judged
    # it (issue #135): the base↔head content is byte-identical, so no panel was spent. It
    # is a verdict for the gate and NOT a round — `panel_rounds` records the flag and the
    # round machinery (cap, convergence, request history) skips it.
    if reaffirmed_from:
        marker += f" reaffirmed={reaffirmed_from}"
    marker += " -->"
    sections = [
        f"{marker}\n## QA panel review — **{verdict}**\n_{recipe} · head `{head_sha[:12]}` · {mode}_",
    ]
    if stale_note:
        sections.append(f"> ⚠️ {stale_note}")
    if truncated:
        sections.append("> ⚠️ The report pass was cut off at its turn limit — this round's findings may be incomplete.")
    if coverage_gaps:
        sections.append(f"> ⚠️ {render_coverage_note(coverage_gaps, lanes)}")
    if brief:
        sections.append(brief)
    elif not brief_found:
        sections.append(
            "_The panel's brief could not be read from this round's report (no delimited brief "
            "block). The findings below are unaffected._"
        )
    if dispositions:
        sections.append(f"### Prior requests\n\n{render_dispositions_table(dispositions)}")
    sections.append(render_findings_block(findings, covered=not coverage_gaps))
    return "\n\n".join(s for s in sections if s) + f"{footnote}{notes}"


def parse_verdict_marker(body: str) -> dict | None:
    """{'head', 'verdict', 'promoted', 'complete', 'verified', 'diff_id', 'reaffirmed'} from
    a posted body, or None if it isn't ours. `reaffirmed` is the head a verdict was carried
    FROM by an identical-diff reaffirm (issue #135), or "" for a round the panel ran."""
    m = _MARKER_RE.search(body or "")
    if not m:
        return None
    # `complete` is a trailing attribute (absorbed by the marker's key=value tail); read
    # it out of the matched marker text. Absent ⇒ True (markers predate the attribute and
    # a plain review IS complete) — only an explicit `complete=false` withholds promotion.
    complete = "complete=false" not in m.group(0)
    verified = "verified=false" not in m.group(0)
    # `diff=<id>` is the base↔head diff identity this round reviewed (issue #91). Absent on
    # any marker written before the feature ⇒ None ⇒ the reaffirm short-circuit fails closed
    # and the normal review runs.
    diff_m = re.search(r"\bdiff=([0-9a-f]+)", m.group(0))
    reaffirmed_m = re.search(r"\breaffirmed=([0-9a-f]{7,40})", m.group(0))
    return {
        "head": m.group("head"),
        "verdict": m.group("verdict"),
        "promoted": m.group("promoted") == "true",
        "complete": complete,
        "verified": verified,
        "diff_id": diff_m.group(1) if diff_m else None,
        # The head this verdict was carried FROM, or "" for a round the panel actually ran.
        "reaffirmed": reaffirmed_m.group(1) if reaffirmed_m else "",
    }


# The findings RECORD exactly as `render_findings_block` writes it: one collapsed
# `<details>` block under this summary. The payload is `json.dumps` output, so it holds no
# raw newline inside a string, and the record ends at the first "\n```\n</details>".
_RECORD_SUMMARY = "findings JSON (machine-readable)"
_RECORD_RE = re.compile(
    r"<details>[ \t]*\r?\n<summary>" + re.escape(_RECORD_SUMMARY) + r"</summary>[ \t]*(?:\r?\n[ \t]*)+"
    r"```json[ \t]*\r?\n(.*?)\r?\n```[ \t]*\r?\n</details>",
    re.DOTALL,
)


def extract_findings_json(body: str) -> str:
    """The findings JSON block from a posted verdict body (for `prior_findings` on a
    delta re-review). Returns the fenced block text, or '' when absent."""
    blocks = fenced_blocks(body, json_only=True)
    for block in reversed(blocks):  # the findings array is the report's FINAL block
        text = block.strip()
        if text.startswith("["):
            try:
                json.loads(text)
            except json.JSONDecodeError:
                continue
            return text
    return ""


def _json_list(text: str) -> list | None:
    """`text` parsed as a JSON array, or None when it is empty, invalid, or not an array."""
    try:
        parsed = json.loads(text) if text and text.strip() else None
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def read_findings_record(body: str) -> tuple[list[dict], bool]:
    """A POSTED verdict body's findings → `(findings, recorded)`.

    Reads the findings record `render_findings_block` wrote — the collapsed block under
    `_RECORD_SUMMARY` — and nothing else. `extract_findings_json` takes the LAST fenced
    array anywhere, which suits raw model output but not a posted body: claim text is
    printed AFTER the record (the confinement footnote, convergence notes, the held and
    unaccounted notes), so an array quoted inside a claim would stand in for the record.

    `recorded` is True only when the body holds exactly ONE record block and it parses to
    a JSON list of finding objects — the one shape in which an empty list means "this
    round raised nothing". Every other shape reads False (fails closed) yet still recalls
    what it can, so no history is lost:
      - no record block — a body from before the collapsed block (v0.19.0), which also
        predates `complete=false` — falls back to `extract_findings_json`, unchanged;
      - more than one — claim text printed after the record reproduced the block — is not
        trusted, and recalls only the FIRST block, the renderer's own record (nothing
        printed before the record can form one), so a quoted block adds nothing to recall;
      - one block that does not parse, or holds a non-object entry, is not recorded.
    """
    text = body or ""
    blocks = _RECORD_RE.findall(text)
    if not blocks:
        legacy = _json_list(extract_findings_json(text)) or []
        return [f for f in legacy if isinstance(f, dict)], False
    if len(blocks) == 1:
        parsed = _json_list(blocks[0])
        if parsed is None:
            return [], False
        findings = [f for f in parsed if isinstance(f, dict)]
        return findings, len(findings) == len(parsed)
    # Several blocks: the FIRST is the renderer's own record — nothing this renderer
    # prints before it can form one (`_clean_brief` strips every fence from the brief,
    # and table cells collapse newlines) — so any later block is quoted claim text.
    # Recall the first block only, and never trust the body as a record.
    first = _json_list(blocks[0]) or []
    return [f for f in first if isinstance(f, dict)], False

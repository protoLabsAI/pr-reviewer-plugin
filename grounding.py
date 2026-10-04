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

Matching is tolerant of the two ways the model rewrites a quote without fabricating it —
quote-STYLE (`'` vs `"`) and ellipsis ABBREVIATION (`foo(...)`) — because the fail-open
posture cuts both ways: over the review window these two accounted for the bulk of a 19%
downgrade rate and masked three real majors (protoAgent#2189, #2283, #2284). Normalizing
quote chars and treating `...` as an ordered-fragment wildcard grounds the abbreviation
while a genuine fabrication, sharing no fragment with the file, still downgrades.
"""

from __future__ import annotations

import bisect
import re

# Backtick spans (single or triple) — how the findings contract renders quoted code.
_TICKS_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```|`([^`\n]+)`", re.DOTALL)

# A quote must be long enough that finding it is evidence of anything. Short spans
# (`deps`, `name`) appear in every file and would ground a fabrication by accident.
MIN_QUOTE_CHARS = 14

# ...and must look like CODE. Prose in backticks ("the `running` state") is a naming
# reference, not a claim about text present in the file.
_CODE_HINT_RE = re.compile(r"[=(){}\[\];]|\.\w|->|=>|::|\w_\w")

# ...and must not be PROSE. Backticks get used for emphasis as often as for code, and
# an explanatory sentence full of code-ish punctuation passes every other filter here.
# From production, 2026-07-23 — this was accepted as a "code quote" and downgraded a
# TRUE finding, because it is long, has whitespace, and contains `(`, `)`, `.`, `_`:
#
#   "— for dep='--5', lstrip('-') → '5' (isdigit() → True), then int('--5') raises
#    ValueError. The exception propagates out of create_from_plan uncaught, ..."
#
# A quoted LINE OF CODE is short, has few tokens, and carries no narrative punctuation.
# The semicolon pattern catches connective prose like `getData(); it then passes to
# render(); the state updates` — a finder narrative that landed verbatim in evidence.
# It is safe against for-loop code (`; i < n`, `; i++`) because those are never followed
# by `it`, `this`, `that`, or `the` as a standalone word. Whether the prose arrives as
# an inline backtick span or inside a fenced block, the same filter applies.
MAX_QUOTE_CHARS = 120
MAX_QUOTE_TOKENS = 14
_PROSE_RE = re.compile(
    r"[—→…]|\.\s+[A-Z]|\b(?:then|because|which|so that|i\.e\.|e\.g\.)\b|;\s+(?:it|this|that|the)\s",
    re.IGNORECASE,
)

# ...and must be STATEMENT-like: containing whitespace between tokens. A bare
# `_writable_dir()` is a reference to a thing, not an assertion about the file's text —
# and it is nearly always present, so counting it would ground a fabrication by
# association. protoAgent#2138 quoted the (real) function name alongside the (invented)
# `writable = Path(str(configured))`; only the latter is a claim this module can test.
_STATEMENT_RE = re.compile(r"\S\s+\S")

# ...and must not be the model's own CONNECTIVE PROSE. `_PROSE_RE` above catches
# narrative punctuation, but a short clause with none of it still clears every filter
# here — it is under the length/token caps, has whitespace, and a stray `(`/`:` reads as
# a code hint. From production (protoAgent#2631 r2, a **blocker**), all three of
#
#   "; diff (scheduler, same pattern at ~new line 1692):"
#   "; and the removed order-independent read:"
#   "(untouched by this PR) still does"
#
# were extracted as checkable code and, being prose, could never be found — downgrading
# a finding whose REAL evidence was never checked.
#
# TWO signals are required together, deliberately: a leading `;`/`,` followed by a
# lowercase word — a sentence fragment continuing the previous clause — AND a trailing
# `:`, the colon that introduces the narration's next breath.
#
# The leading fragment ALONE is not enough. A quote lifted from a wrapped construct
# begins exactly that way once `_normalize` has flattened it:
#
#   ", key=value, timeout=30)"        a continuation line of a wrapped call
#   "; i < n; i++) { total += i;"     the middle of a C-style for header
#   "; do_thing() } finally { … }"    a statement after an inline `;`
#
# Real code closes on a bracket or an operator; it does not end on a bare `:` after a
# lowercase-led fragment. Requiring both ends keeps all three of those, and both
# production prose strings still carry their colon.
#
# An English-function-word COUNT was tried for the third case and withdrawn; both of its
# formulations were unsafe, and the asymmetry here is brutal:
#
#   - counting words everywhere drops genuine code whose STRING LITERALS are English —
#     `raise ValueError("the value at this index is already removed")`
#   - masking string literals first re-admits prose that merely contains a quoted
#     fragment — `the "removed read" bug (still present)` masks down to two function
#     words and passes
#   - and `this`/`that` are English function words AND JS/TS keywords, so any wordlist
#     containing them drops `this.obj[this.key] = this.val`
#
# Each of those is a FALSE DROP, and a false drop is worse than the bug: `ground_finding`
# fail-opens when no quote survives extraction, so dropping a real quote does not merely
# skip a check — it lets a FABRICATED quote of the same shape past the #25 hallucination
# guard entirely. A filter guarding against hallucination must never widen the hole it
# guards. The unrecognised third case simply keeps the old behaviour (checked, not found,
# downgraded), which is a bad verdict but not an open bypass.
_FRAGMENT_START_RE = re.compile(r"^[;,]\s+[a-z]")


def _is_connective_prose(text: str) -> bool:
    return bool(_FRAGMENT_START_RE.search(text)) and text.rstrip().endswith(":")


# Diff decorations the panel copies into evidence; stripped before matching so a quote
# lifted from a patch hunk still matches the file's own text.
_DIFF_PREFIX_RE = re.compile(r"^[+\-]\s?", re.MULTILINE)

# Quote characters the model reformats freely — it quotes `rglob('*')` where the file has
# `rglob("*")`, and either is the same code. Unify them (incl. smart quotes) on both sides
# so a quote-STYLE difference never reads as fabrication. Confirmed false-downgrades:
# protoAgent#2284 r3 (a real `major` masked purely on ' vs "), #2189 r3.
_QUOTE_CHARS = str.maketrans({c: '"' for c in "'`‘’“”"})

# The model abbreviates long quotes with an ellipsis — `options={[...].map(...)}`,
# `[m for m in messages ...]`. A verbatim substring check can never match those, so a
# real finding that quoted an abbreviated line got downgraded (protoAgent#2189 r2/r3 — a
# `major` twice; #2283 r1). `...` (bare, `(...)`, `[...]`, or `{...}`) is treated as a
# wildcard: every substantial fragment around it must still appear, in order — so an
# abbreviation grounds but a fabrication (no fragment present) still does not.
#
# `[...]` and `{...}` are consumed as full units (not just the bare `...`) so that the
# surrounding bracket characters are not left attached to the adjacent fragments, making
# short-context quotes (e.g. `fn([...])`) unnecessarily hard to anchor.
#
# `_MIN_FRAGMENT` is kept at 6 (not lowered to 4): a 4-char fragment like `map(` or
# `res =` appears in nearly every file and would ground fabrications by coincidence —
# confirmed by the `res = ... + ...` test case which would falsely ground at threshold 4.
_ELLIPSIS_RE = re.compile(r"\s*(?:[\(\[\{]\s*)?\.\.\.+(?:\s*[\)\]\}])?\s*")
_MIN_FRAGMENT = 6  # a fragment shorter than this is too common to be evidence on its own


def _normalize(text: str) -> str:
    """Collapse whitespace and unify quote characters, so neither indentation/wrapping nor
    a `'`-vs-`"` choice ever decides groundedness."""
    return re.sub(r"\s+", " ", _DIFF_PREFIX_RE.sub("", text)).translate(_QUOTE_CHARS).strip()


def _present(quote: str, haystack: str) -> bool:
    """Is `quote` anchored in `haystack`? Verbatim first; failing that, if the quote was
    abbreviated with an ellipsis, require each substantial fragment to appear in order."""
    if quote in haystack:
        return True
    if "..." not in quote:
        return False
    fragments = [f for f in _ELLIPSIS_RE.split(quote) if len(f) >= _MIN_FRAGMENT]
    if not fragments:
        return False  # only tiny fragments survive — no evidence value, don't ground
    cursor = 0
    for fragment in fragments:
        found = haystack.find(fragment, cursor)
        if found < 0:
            return False
        cursor = found + len(fragment)
    return True


# A finding's text often goes on to say what the code SHOULD be — protoPatch appends a
# "Fix: Replace `secrets: inherit` with … e.g. `secrets:\n  discord_webhook: …`", finders write
# "should be `x`". That is the suggested replacement, which by definition is not in the file, and
# grounding it downgraded a TRUE finding (data-plugin#1@eb377e62 `release.yml:26`, issue #261).
# Everything from the first fix/recommendation marker on is dropped before quotes are taken...
_FIX_MARKER_RE = re.compile(
    r"(?:^|(?<=[\s.;:,)\]—–-]))(?:(?:suggested|recommended|proposed|possible|the)\s+)?"
    r"(?:fix|remediation|recommendation|suggestion|mitigation)\s*:"
    r"|\bto\s+fix\s+(?:this|it)\b",
    re.IGNORECASE,
)
# ...and a quote introduced as the replacement ("…with `b`", "should be `x`", "e.g. `y`") is not
# a claim about the file either. The quote it replaces ("replace `a` with") is, and is kept.
_PRESCRIPTIVE_TAIL_RE = re.compile(
    r"(?:\be\.g\.|\bfor\s+example|\bshould\s+(?:be|read|use|become)|\binstead\s+use|\buse\s+instead"
    r"|\breplac\w*\b[^.;]{0,80}\bwith|\bchang\w*\b[^.;]{0,60}\bto|\brewrit\w*\b[^.;]{0,60}\bas)"
    r"\s*[:,]?\s*(?:something\s+like\s*)?$",
    re.IGNORECASE,
)


def descriptive_text(text: str) -> str:
    """`text` without its suggested fix: everything from the first fix/recommendation marker on."""
    m = _FIX_MARKER_RE.search(text or "")
    return text[: m.start()] if m else (text or "")


def _snippets(text: str) -> list[str]:
    out: list[str] = []
    for m in _TICKS_RE.finditer(text):
        if _PRESCRIPTIVE_TAIL_RE.search(text[max(0, m.start() - 120) : m.start()]):
            continue  # the replacement the finding proposes, not code it says is there
        raw = m.group(1) or m.group(2) or ""
        text_ = _normalize(raw)
        if not (MIN_QUOTE_CHARS <= len(text_) <= MAX_QUOTE_CHARS):
            continue
        if len(text_.split()) > MAX_QUOTE_TOKENS or _PROSE_RE.search(text_):
            continue  # a sentence about the code, not a claim about the file's text
        if _is_connective_prose(text_):
            continue  # the model's own narration, captured as if it were evidence
        if _CODE_HINT_RE.search(text_) and _STATEMENT_RE.search(text_):
            out.append(text_)
    return out


def quoted_snippets(finding: dict) -> list[str]:
    """Checkable code quotes from a finding, normalized — taken from its EVIDENCE (issue #261).

    The evidence field is where the finding says what the file contains; the claim states the
    conclusion and, like a fix, may name code that is not there yet. Suggested-fix text is cut
    from it first (`descriptive_text`), and replacement quotes are skipped. Only when the
    evidence carries no checkable quote at all does the claim's own quote get checked: a claim
    that quotes fabricated code with prose-only evidence must still meet the #25 guard, or
    removing the claim from the haystack would let exactly that fabrication through.

    Only spans that are long enough AND look like code survive — everything else is prose,
    and prose is not a claim about what the file contains.
    """
    quotes = _snippets(descriptive_text(str(finding.get("evidence") or "")))
    if quotes:
        return quotes
    return _snippets(descriptive_text(str(finding.get("claim") or "")))


# A file the finding names in its own text (`tests/conftest.py defines …`). A quote it attributes
# to that file is checked there too before it is called missing (issue #261: `def call(tool,
# **kw)` was searched in the cited `tests/test_plugin.py`; it lives in `tests/conftest.py`).
_PATH_MENTION_RE = re.compile(
    r"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:py|pyi|js|jsx|ts|tsx|mjs|cjs|go|rs|rb|java|kt|sh|bash|"
    r"ya?ml|toml|json|cfg|ini|sql|c|h|cc|cpp|hpp|cs|php|swift|lua|vue|svelte))(?![\w/-])"
)
MAX_RELATED_PATHS = 3


def mentioned_paths(finding: dict) -> list[str]:
    """Repo paths (or bare file names) the finding's claim/evidence names, other than its own
    `file`, in order — at most `MAX_RELATED_PATHS`. A bare name is resolved by the reader."""
    own = str(finding.get("file") or "").lstrip("./")
    seen: dict[str, None] = {}
    blob = f"{finding.get('evidence') or ''}\n{finding.get('claim') or ''}"
    for m in _PATH_MENTION_RE.finditer(blob):
        path = m.group(1).lstrip("./")
        if path and path != own and "://" not in path and not path.startswith(".."):
            seen.setdefault(path, None)
    return list(seen)[:MAX_RELATED_PATHS]


def related_candidates(finding: dict) -> list[str]:
    """The repo paths to read for `mentioned_paths`: a name with a directory as written, a bare
    name next to the cited file first and then at the root."""
    own = str(finding.get("file") or "").lstrip("./")
    base = own.rsplit("/", 1)[0] if "/" in own else ""
    out: dict[str, None] = {}
    for path in mentioned_paths(finding):
        if "/" not in path and base:
            out.setdefault(f"{base}/{path}", None)
        out.setdefault(path, None)
    return [p for p in out if p != own]


def ground_finding(finding: dict, source: str | None, related: str = "") -> tuple[bool, list[str]]:
    """(grounded?, quotes that were absent). `source` should be the file at the reviewed
    head PLUS the PR's patch for it — a removed-behaviour finding legitimately quotes
    code the head no longer has, and must not be downgraded for being right.

    `related` is more text a quote may be found in before it is called missing (issue #261):
    the files the finding names in its own evidence, read at the same head, and the rest of the
    PR's patch. A fabricated quote is in none of them; a real one attributed to a neighbouring
    file is.

    `source` is text that was ACTUALLY READ (or `None` for "no source to check"). A FETCH
    FAILURE is a different animal — see `UNREADABLE`; `apply_grounding` handles it, and this
    function must never be handed the sentinel, because "quote absent from a file I read"
    and "I could not read the file" are opposite facts (issue #109)."""
    quotes = quoted_snippets(finding)
    if source is None or not quotes:
        return True, []  # nothing to check against, or nothing checkable — fail open
    haystack = _normalize(source)
    missing = [q for q in quotes if not _present(q, haystack)]
    if missing and related:
        extra = _normalize(related)
        missing = [q for q in missing if not _present(q, extra)]
    if len(missing) < len(quotes):
        return True, []  # at least one quote landed — the finding is anchored in reality
    return False, missing


UNGROUNDED_NOTE = "evidence not found at the reviewed head — downgraded to uncertain, cannot gate a merge (issue #25)"

# The reviewed head file could not be READ (a fetch failure — a 404 on an orphaned SHA
# after a force-push, a network error, an undecodable blob), as distinct from `None` (no
# source to check against) and from a successfully-read string. A fetch failure is NOT
# evidence: absence cannot be established against a source that was never read, so it must
# never masquerade as a fabricated quote (issue #109). `_finding_sources` passes this for a
# failed head read INSTEAD of a patch-only haystack — grounding a head-context quote against
# the patch alone would falsely downgrade a real finding for code the panel never saw.
UNREADABLE = object()

SOURCE_UNAVAILABLE_NOTE = (
    "source unavailable at the reviewed head — could NOT verify; severity unchanged, the "
    "finding was neither confirmed nor refuted on its merits (issue #109)"
)


def related_text(finding: dict, sources: dict[str, str | object | None], patches: str = "") -> str:
    """The text a finding's quote may also be found in (issue #261): every successfully READ
    source for a path the finding names (`related_candidates`), plus `patches` — the PR's
    whole patch. Unreadable or unread paths contribute nothing."""
    parts = [sources[p] for p in related_candidates(finding) if isinstance(sources.get(p), str)]
    if patches:
        parts.append(patches)
    return "\n".join(parts)


def apply_grounding(
    findings: list[dict], sources: dict[str, str | object | None], patches: str = ""
) -> tuple[list[dict], list[dict], list[dict]]:
    """(findings, downgraded, unreadable). Three dispositions, kept deliberately distinct:

    * quoted code ABSENT from a file that WAS read → annotated `verdict: uncertain` +
      `ungrounded` (the fabricated-quote downgrade `verdict_for` refuses to turn into a
      FAIL, issue #25), and listed in `downgraded`;
    * the file could NOT be read at the reviewed head (`sources[file] is UNREADABLE`) →
      severity and verdict left UNTOUCHED, annotated `source_unavailable` and listed in
      `unreadable` so the report can say the finding was neither confirmed nor refuted on
      its merits (issue #109) — a failed read must never lift a gate the panel earned;
    * anything else → left exactly as it is.

    Findings are never removed. The report's own JSON still shows them and the posted body
    footnotes BOTH the downgrade and the could-not-verify state, so a human can always
    overrule the machine. The failure mode guarded against is a fabrication that BLOCKS and,
    now, a fetch failure that silently UNBLOCKS.
    """
    out: list[dict] = []
    downgraded: list[dict] = []
    unreadable: list[dict] = []
    for finding in findings:
        file = str(finding.get("file") or "")
        source = sources.get(file)
        if source is UNREADABLE:
            # Fetch failure, NOT quote-absent: we learned nothing, so we change nothing but
            # the visibility. Severity and verdict stand; only the note is added.
            annotated = dict(finding)
            annotated["source_unavailable"] = True
            note = str(annotated.get("note") or "").strip()
            annotated["note"] = f"{note} — {SOURCE_UNAVAILABLE_NOTE}" if note else SOURCE_UNAVAILABLE_NOTE
            out.append(annotated)
            unreadable.append({"file": file, "severity": str(finding.get("severity") or "")})
            continue
        grounded, missing = ground_finding(finding, source, related_text(finding, sources, patches))
        if grounded:
            out.append(finding)
            continue
        annotated = dict(finding)
        annotated["verdict"] = "uncertain"
        annotated["ungrounded"] = True
        note = str(annotated.get("note") or "").strip()
        annotated["note"] = f"{note} — {UNGROUNDED_NOTE}" if note else UNGROUNDED_NOTE
        out.append(annotated)
        downgraded.append({"file": file, "severity": str(finding.get("severity") or ""), "missing": missing[:3]})
    return out, downgraded, unreadable


def _locate(quote: str, lines: list[str]) -> list[int]:
    """1-based lines where `quote` STARTS in the file, matching across line breaks the way
    `_present` does (a quote copied from a wrapped statement spans several lines). Each line is
    normalized on its own and the lines are joined with one space, so a character offset maps
    back to the line it came from. An ellipsis quote is located by its first fragment."""
    starts: list[int] = []
    parts: list[str] = []
    offset = 0
    for ln in lines:
        starts.append(offset)
        text = _normalize(ln)
        parts.append(text)
        offset += len(text) + 1
    joined = " ".join(parts)
    needle = quote
    if "..." in quote:
        fragments = [f for f in _ELLIPSIS_RE.split(quote) if len(f) >= _MIN_FRAGMENT]
        if not fragments or not _present(quote, joined):
            return []
        needle = fragments[0]
    hits: list[int] = []
    pos = joined.find(needle)
    while pos >= 0:
        line = bisect.bisect_right(starts, pos)  # the 1-based line whose span holds `pos`
        while line - 1 < len(parts) and not parts[line - 1]:
            line += 1  # an offset on an empty line's separator belongs to the next real line
        if line not in hits:
            hits.append(line)
        pos = joined.find(needle, pos + 1)
    return hits


def anchor_quotes(finding: dict) -> list[str]:
    """The quotes a finding's LINE is re-anchored by: its checkable quotes, or — when its
    evidence is bare code with no backticks, the shape protoPatch emits (`out = subprocess.run(`
    on its own line) — the evidence's first code-like line. Used only to locate, never to
    downgrade: bare evidence was never a grounding input and does not become one here."""
    quotes = quoted_snippets(finding)
    if quotes:
        return quotes
    evidence = descriptive_text(str(finding.get("evidence") or ""))
    if "`" in evidence:
        return []
    for raw in evidence.splitlines():
        line = _normalize(raw)
        if not (MIN_QUOTE_CHARS <= len(line) <= MAX_QUOTE_CHARS) or len(line.split()) > MAX_QUOTE_TOKENS:
            continue
        if _PROSE_RE.search(line) or not _CODE_HINT_RE.search(line) or not _STATEMENT_RE.search(line):
            continue
        return [line]
    return []


def correct_line_numbers(findings: list[dict], blobs: dict[str, str]) -> list[dict]:
    """Re-anchor each grounded finding's line to where its quoted EVIDENCE is (issue #261).

    The panel's anchors drift 5–70 lines and sometimes point at pre-existing code; the carried-
    prior keying and nearby scoping both read the line. For each grounded finding the quotes are
    tried longest first, and the first that occurs exactly ONCE in the raw blob (the file at
    head, without the patch) sets ``finding['line']`` — a quote spanning several lines anchors
    on the line it starts. ``line_corrected = True`` is emitted, and when the line actually
    moved the panel's own line is kept as ``line_original``. A quote found more than once is
    ambiguous and the next one is tried; none unique → left as-is (fail-open). Ungrounded
    findings and findings with no usable quotes are left untouched. This never downgrades,
    never removes, never changes severity, and never changes the finding's file.
    """
    out: list[dict] = []
    for finding in findings:
        if finding.get("ungrounded"):
            out.append(finding)
            continue
        file = str(finding.get("file") or "")
        blob = blobs.get(file, "")
        quotes = anchor_quotes(finding) if blob else []
        if not quotes:
            out.append(finding)
            continue
        lines = blob.splitlines()
        hit = None
        for quote in sorted(quotes, key=len, reverse=True):
            found = _locate(quote, lines)
            if len(found) == 1:
                hit = found[0]
                break
        if hit is None:
            out.append(finding)
            continue
        corrected = dict(finding)
        if finding.get("line") != hit and "line_original" not in corrected:
            corrected["line_original"] = finding.get("line")
        corrected["line"] = hit
        corrected["line_corrected"] = True
        out.append(corrected)
    return out


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


def render_unreadable_footnote(unreadable: list[dict]) -> str:
    """The posted-body note for findings whose cited file could not be READ at the reviewed
    head. A failed read is not absent evidence: the severity is UNCHANGED and the finding is
    neither refuted nor uncertain-on-its-merits — it simply could not be checked (issue #109).
    The note says exactly that, so the verdict never silently over- or under-claims what the
    grounding pass actually learned."""
    if not unreadable:
        return ""
    lines = "\n".join(
        f"- `{d['file'] or '(no file)'}` ({d['severity'] or '?'}) — file could not be read at this head"
        for d in unreadable
    )
    return (
        f"\n\n---\n_{len(unreadable)} finding(s) could NOT be evidence-checked: the file each cites could not "
        f"be read at the reviewed head. This is a failed READ, not absent evidence — the severity is UNCHANGED "
        f"and the finding was neither confirmed nor refuted on its merits (issue #109). A human should confirm "
        f"it against the PR head._\n{lines}"
    )


# ── absence claims: an "it isn't there" finding is grounded against the TREE (issue #209) ──
#
# The fabricated-quote guard above (issue #25) checks a POSITIVE claim — "the file contains
# X" — against the file's own text. An ABSENCE claim is the opposite shape — "there is no
# test for this module", "missing docs", "the PR exercises none of it" — and it cannot be
# grounded the same way: the code it says is missing is, by definition, not in the diff.
#
# The 2026-09 false positives (design-system-plugin#19): two blocking majors — "fetch.py …
# with no test file" and "siteprobe.py … has no test file" — when `tests/test_fetch.py` and
# `tests/test_site_audit.py` existed at the head and CI ran them. The 309 KB diff overran the
# panel's ~200K-char budget and the truncation dropped the alphabetically-late `tests/` files,
# so the panel never saw them and asserted their absence. The verify pass then CONFIRMED the
# claim, because there was nothing in the (truncated) diff to refute it — the same
# discounting-evidence-already-absent failure #25 and #109 are about, one level up.
#
# Demotion is fail-open and mirrors `apply_grounding`, but grounds the two absence FAMILIES
# differently (see `absence_kind`). A TEST/coverage absence ("no test file", "untested") is
# refuted by a plausible test in the head TREE; failing that, on a TRUNCATED diff whose dropped
# paths included a test file, it is left unestablished, because the panel's view of the test
# suite was incomplete. A code-shape absence ("used without validation", "missing error
# handling", "no docstring") is about the FILE'S OWN code, so a test file NEVER refutes it — it
# is unestablished only when truncation dropped THE FILE IT FAULTS, so a real flaw the panel
# quoted from a file it fully saw keeps gating even on a truncated diff. A GENUINE, established
# absence — a tree we could read with no plausible test, on a diff that showed the relevant
# files — stands exactly as the panel raised it.

# The negation-plus-target phrasings the panel uses for an absence. The negation carries the
# weight; an ordinary mention ("the test asserts X", "adds handling for Y") does not match.
#
# Two FAMILIES, kept apart deliberately (the 2026-09 review of #209 part 2). A TEST/coverage
# absence — "no test file", "untested", "exercises none of it" — is groundable against the head
# TREE: a plausible test file existing refutes it. A code-shape absence — "used without
# validation", "missing error handling", "no docstring" — is about the FILE'S OWN code, and a
# test file existing says NOTHING about it. Grounding the second family against
# `plausible_test_in_tree` demoted real "without validation" / "not handled" majors on a module
# merely because some test file existed, and mislabelled them "a plausible test exists".
#
# The bind between a negation and its target decides the family, and it must be the NEAREST
# target — NOT the first family that happens to match anywhere in the blob. Two independent
# `search`es (test-family first) let a single negation reach PAST a nearer code-shape target to a
# far "test": "used without validation, and the tests never fire" was read as a test absence
# because `without … (up to five words, commas included) … tests` matched, even though `without`
# plainly negates `validation` one word away. So the negation→target bind is resolved in ONE scan
# over both families (`_ABSENCE_NEG_TARGET_RE`), and a code-shape absence anywhere wins over a
# test absence (see `absence_kind`) — this is the 2026-09 #209 part-2 review, finding #1.
_TEST_ABSENCE_TARGET = r"(?:tests?|test[\s_.-]*files?|test[\s_.-]*cases?|test[\s_.-]*coverage|coverage)"
_OTHER_ABSENCE_TARGET = r"(?:documentation|docs?|docstrings?|handling|validation)"

# The negation vocabulary and the gap it may put before its target. Commas count as gap ("no X,
# Y, or test") — which is exactly why a non-greedy gap over the COMBINED target set is required:
# it binds each negation to the closest target, so the comma-spanning reach can no longer jump a
# nearer code-shape target on its way to a distant "test".
_ABSENCE_NEG = r"(?:no|missing|without|lacks?|lacking|absent|zero)"
_ABSENCE_GAP = r"(?:[\w'\"()./,-]+\s+){0,5}?"

# One scan, both families: each negation binds to whichever target — test OR code-shape — sits
# NEAREST it (the gap is non-greedy), and the named group that captured names the family. So
# `no` in "no test coverage for the error handling" binds to `test` (the `handling` is that
# test's OBJECT, not an independently-negated gap), while `without` in "used without validation,
# and the tests never fire" binds to `validation` and the un-negated "tests" is left alone.
_ABSENCE_NEG_TARGET_RE = re.compile(
    r"\b"
    + _ABSENCE_NEG
    + r"\b\s+"
    + _ABSENCE_GAP
    + r"(?:(?P<test>"
    + _TEST_ABSENCE_TARGET
    + r")|(?P<other>"
    + _OTHER_ABSENCE_TARGET
    + r"))\b",
    re.IGNORECASE,
)

# The ANCHORED forms, where the negation sits ON its target so there is no gap to mis-bind:
# `untested`, `not … tested`, `exercises none`, `none of it … tested` for the test family;
# `undocumented`, `not … handled`, `none of it … documented` for the code-shape family. The
# `not …` forms of the two families legitimately co-occur ("not handled and not tested") — each
# family matches its own, and `absence_kind` resolves the co-occurrence in favour of code-shape.
_TEST_ABSENCE_ANCHORED_RE = re.compile(
    r"\bun(?:tested|covered)\b"
    r"|\bnot\b(?:\s+\w+){0,3}?\s+(?:tested|covered|exercised)\b"
    r"|\b(?:exercise[sd]?|cover[sd]?|test[sd]?)\s+none\b"
    r"|\bnone\s+of\s+(?:it|them|this|these|the\s+\w+)\b(?:\s+\w+){0,4}?\s+(?:tested|covered|exercised)\b",
    re.IGNORECASE,
)
_OTHER_ABSENCE_ANCHORED_RE = re.compile(
    r"\bun(?:documented|validated)\b"
    r"|\bnot\b(?:\s+\w+){0,3}?\s+(?:documented|handled|validated)\b"
    r"|\bnone\s+of\s+(?:it|them|this|these|the\s+\w+)\b(?:\s+\w+){0,4}?\s+documented\b",
    re.IGNORECASE,
)

# What a test file looks like across ecosystems — a `tests/`/`__tests__/`/`spec/` directory,
# or a filename carrying a `test`/`spec` affix. Used both to recognise a test path in the tree
# and to refuse an absence-of-test claim raised against a file that IS itself a test.
_TEST_DIR_RE = re.compile(r"(?:^|/)(?:tests?|__tests__|specs?)(?:/|$)", re.IGNORECASE)


def absence_kind(finding: dict) -> str | None:
    """`"test"` (a PURE test/coverage absence, groundable against the head tree), `"other"` (a
    code-shape absence — missing validation/handling/docs, groundable only against whether the
    faulted file itself was seen), or `None` (not an absence claim). Read from `claim` +
    `evidence`, the same blob `quoted_snippets` grounds.

    A code-shape absence anywhere in the claim WINS over a test absence: a test file can never
    refute "used without validation", so a claim that faults the file's OWN code must not be
    tree-demoted just because it ALSO mentions tests ("used without validation, and the tests
    never fire", "not handled and not tested" — the 2026-09 #209 part-2 review, finding #1). Only
    a claim that is PURELY a test/coverage absence is groundable against the tree. Each negation
    binds to its NEAREST target, so "no test coverage for the error handling" stays a test absence
    — the `handling` there is the test's object, not an independently-negated code-shape gap."""
    blob = f"{finding.get('claim') or ''}\n{finding.get('evidence') or ''}"
    has_test = bool(_TEST_ABSENCE_ANCHORED_RE.search(blob))
    has_other = bool(_OTHER_ABSENCE_ANCHORED_RE.search(blob))
    for m in _ABSENCE_NEG_TARGET_RE.finditer(blob):
        if m.group("test"):
            has_test = True
        else:
            has_other = True
    if has_other:
        return "other"
    if has_test:
        return "test"
    return None


def is_absence_claim(finding: dict) -> bool:
    """Does this finding assert that something ISN'T there (no test, missing docs, unhandled
    case)? True for either absence family — see `absence_kind` for the distinction that decides
    how each is grounded. Read from `claim` + `evidence`, the same blob `quoted_snippets` grounds."""
    return absence_kind(finding) is not None


def _module_stem(path: str) -> str:
    """`pkg/sub/fetch.py` → `fetch`: the filename with its directory and last extension gone."""
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def _is_test_path(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    if _TEST_DIR_RE.search(path):
        return True
    return bool(base.startswith(("test_", "test.")) or re.search(r"[._](test|spec)\.", base))


def _test_subject(path: str) -> str:
    """The module a test path appears to cover: `test_fetch.py` → `fetch`, `fetch_test.go` →
    `fetch`, `Fetch.test.tsx` → `fetch`. Filename-convention only (the tree gives paths, not
    contents), so it is deliberately conservative: an exact subject match, never a substring."""
    base = path.rsplit("/", 1)[-1].lower()
    stem = re.sub(r"\.(py|js|jsx|ts|tsx|mjs|cjs|go|rb|java|rs|php|cs|kt|swift)$", "", base)
    stem = re.sub(r"^test[_.]", "", stem)
    stem = re.sub(r"[_.](test|spec)$", "", stem)
    return stem


def plausible_test_in_tree(file: str, tree: set[str] | None) -> str | None:
    """A path in the head `tree` that plausibly TESTS `file`, or None. Matches
    `tests/test_<stem>.py`, `<stem>_test.*`, `<stem>.test.*`, `<stem>.spec.*` and the like by
    filename convention — an exact subject match, so a genuine absence is not masked by a
    same-named test for a different module. `None` tree (unreadable) grounds nothing."""
    if not tree:
        return None
    stem = _module_stem(file).lower()
    if not stem or _is_test_path(file):
        return None  # an absence-of-test claim about a test file itself is not groundable here
    for path in tree:
        if _is_test_path(path) and _test_subject(path) == stem:
            return path
    return None


ABSENCE_TEST_EXISTS_NOTE = (
    "a plausible test for this module exists at the reviewed head — the absence claim is not "
    "grounded, downgraded to uncertain and cannot gate a merge (issue #209)"
)
ABSENCE_TRUNCATED_NOTE = (
    "the reviewed diff was truncated to the panel's char budget, so the whole change was not "
    "seen and an absence cannot be established — downgraded to uncertain and cannot gate a "
    "merge (issue #209)"
)


def ground_absence_claims(
    findings: list[dict],
    tree: set[str] | None,
    *,
    truncated: bool,
    dropped_paths: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """(findings, demoted). Demote a blocking/major absence claim that grounding refutes or
    leaves unestablished. Only a blocker/major is touched — a minor/nit never gates — and a
    genuine, established absence stands untouched. Grounds the two families separately (see
    `absence_kind`):

    * a TEST/coverage absence is refuted by a plausible test in the head `tree`; failing that, if
      the diff was TRUNCATED and a test file was among the `dropped_paths` (so the panel's view of
      the test suite was incomplete), it is left unestablished. Either way it is demoted;
    * a code-shape absence (missing validation/handling/docs) is about the FILE'S OWN code, so a
      test file NEVER refutes it. It is demoted only when the diff was truncated AND the finding's
      own file was among the `dropped_paths` — i.e. the panel did not see the code it faults. A
      code-shape absence on a file the panel fully saw keeps gating, even on a truncated diff.

    Demotion sets `verdict: uncertain` (which `verdict_for` refuses to turn into a FAIL) and
    `ungrounded: True` (so the prior-finding ledger excludes it, exactly like `apply_grounding`);
    the finding still posts and still reads for a human to judge."""
    dropped = set(dropped_paths or [])
    dropped_a_test = truncated and any(_is_test_path(p) for p in dropped)
    out: list[dict] = []
    demoted: list[dict] = []
    for finding in findings:
        sev = str(finding.get("severity") or "").lower()
        kind = absence_kind(finding)
        if sev not in ("blocker", "major") or kind is None:
            out.append(finding)
            continue
        file = str(finding.get("file") or "")
        disposition = reason = detail = None
        if kind == "test":
            match = plausible_test_in_tree(file, tree)
            if match:
                disposition, reason, detail = "test-exists", ABSENCE_TEST_EXISTS_NOTE, match
            elif truncated and (dropped_a_test or file in dropped):
                disposition, reason = "diff-truncated", ABSENCE_TRUNCATED_NOTE
                detail = plausible_test_in_tree(file, dropped) or file
        elif truncated and file in dropped:  # code-shape: only unestablished if the file was unseen
            disposition, reason, detail = "diff-truncated", ABSENCE_TRUNCATED_NOTE, file
        if disposition is None:
            out.append(finding)  # positively established (or established enough) → it stands
            continue
        annotated = dict(finding)
        annotated["verdict"] = "uncertain"
        annotated["ungrounded"] = True
        annotated["absence_demoted"] = disposition
        note = str(annotated.get("note") or "").strip()
        annotated["note"] = f"{note} — {reason}" if note else reason
        out.append(annotated)
        demoted.append({"file": file, "severity": sev, "kind": disposition, "detail": detail or ""})
    return out, demoted


def diff_truncation(diff_sizes: list[tuple[str, int]], budget: int) -> tuple[bool, list[str]]:
    """Replicate the review engine's char-budget truncation to learn what the panel did NOT
    see: (truncated?, dropped paths). The engine concatenates per-file patches in path order
    and trims the running total to `budget`, so alphabetically-late files fall off a large diff
    first — and `tests/` sorts near the end, which is how a real test file got dropped and its
    absence then asserted (issue #209). Once the budget is hit the drop LATCHES: every later
    path is dropped even if it would have fit, matching a single running concatenation. A
    non-positive budget means no limit (never truncated)."""
    if budget <= 0:
        return False, []
    kept = 0
    truncated = False
    dropped: list[str] = []
    for path, size in sorted(diff_sizes):
        if truncated or kept + max(0, size) > budget:
            truncated = True
            dropped.append(path)
        else:
            kept += max(0, size)
    return truncated, dropped


def render_absence_footnote(demoted: list[dict]) -> str:
    """The posted-body note for demoted absence claims — the verdict must never silently
    disagree with the report, the same contract `render_grounding_footnote` keeps. A dedicated
    renderer rather than that one because its wording ("quoted evidence not found") describes a
    fabricated positive quote, which would misdescribe an absence demotion."""
    if not demoted:
        return ""
    lines = []
    for d in demoted:
        if d.get("kind") == "test-exists":
            detail = d.get("detail") or ""
            why = f"a plausible test exists at this head{f': `{detail}`' if detail else ''}"
        else:
            why = "the reviewed diff was truncated to the panel's char budget — the whole change was not seen"
        lines.append(f"- `{d['file'] or '(no file)'}` ({d['severity'] or '?'}) — {why}")
    body = "\n".join(lines)
    return (
        f"\n\n---\n_{len(demoted)} absence finding(s) (“no test” / “missing” / “exercises none”) "
        f"downgraded to **uncertain**: an absence cannot be established against a source the panel "
        f"did not fully see. A finding that cannot be grounded does not gate a merge (issue #209) — "
        f"it still stands for a human to judge._\n{body}"
    )

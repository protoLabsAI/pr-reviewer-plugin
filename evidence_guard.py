"""A `confirmed` must rest on evidence the claim did not already contain (issue #259).

The 2026-10 audit found the verifier's notes restating the claim's premise instead of testing
it. data-plugin#1@17029d7e went FAIL on two majors, "data_schema / data_profile are dead because
the guard rejects DESCRIBE / SUMMARIZE", confirmed with:

    "Verified: data_schema calls run_query with 'DESCRIBE SELECT * FROM …'; run_query calls
     guard() which checks `st != duckdb.StatementType.SELECT` and raises. DuckDB's parser
     classifies DESCRIBE as its own StatementType, not SELECT, so the guard always rejects it."

The quoted code is real; the one sentence the verdict turns on, what DuckDB's parser does, is
asserted, not shown. On the locked duckdb 1.5.6, `extract_statements('DESCRIBE SELECT * FROM
t')[0].type` is `StatementType.SELECT`. Those two false majors were the whole FAIL.

Two deterministic checks, each demoting `confirmed` → `uncertain` (never dropping a finding,
never touching another verdict). `uncertain` still posts; `verdict_for` only refuses to let it
FAIL on its own:

  1. **Restated evidence.** The verifier's own note (the plugin's appended notes stripped)
     quotes no code at all, and nearly every content word in it already appears in the claim
     and evidence. That is the claim read back, not a check of it.
  2. **Unquoted library semantics.** The finding's quoted code calls into a library the repo
     DECLARES as a dependency (`duckdb.StatementType`), the claim or note asserts what that
     library does ("DuckDB's parser classifies…", "requests returns…"), and the note cites no
     library source, docs or execution of it. The verdict then rests on a belief about
     third-party behaviour. Only a declared dependency counts: the dependency list is read from
     the repo's manifests at head, so a local variable (`model.startswith`) or a repo module
     (`engine.run_query`) never trips it, and an unreadable manifest checks nothing.

What this does NOT catch: a wrong conclusion drawn from correctly quoted repo code (the audit's
"^0.0.x must be an exact patch" was a traced misreading of deliberate code), a terse note with
too few words to judge, and a library the claim names only in prose. Those stay with the verify
prompt.
"""

from __future__ import annotations

import json
import re
import sys

from .grounding import _TICKS_RE

# The plugin appends its own annotations to `note` as " — <text>"; a verifier note is judged
# without them. Each starts a segment that names an issue or a plugin state.
_PLUGIN_SEGMENT_RE = re.compile(
    r"\(issue #\d+\)|\(#\d+\)|protoAgent#\d+|^nearby:|carried from a prior round|downgraded to uncertain"
    r"|evidence not found at the reviewed head|source unavailable at the reviewed head|reported, not gated",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Words that carry no evidence: English function words and the verifier's own vocabulary.
_STOPWORDS = frozenset(
    """the and but for nor not yet are was were has have had does did doing done this that these
    those with without from into onto than then thus its it's their there here which who whom
    whose what when where why how all any both each few more most other some such only own same
    can will just should would could may might must also very too because since while after
    before above below over under again further once out off about against between through
    during per via upon been being one two three four five six first second
    verified verify verifies confirmed confirm confirms confirming read reads reading reread
    head line lines file files code finding findings claim claims present exists exist actually
    indeed correct correctly true accurate accurately substance stands holds valid real shows
    shown see seen look looked looks check checked checks matches match matching quoted quote
    quotes""".split()
)

RESTATE_OVERLAP = 0.8  # share of the note's content words already in the claim + evidence
RESTATE_MIN_TOKENS = 4  # a note shorter than this is too terse to judge either way

_DOTTED_PREFIX_RE = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)\.[A-Za-z_]")
# What a library is asserted to DO. The lookahead keeps a dotted use (`duckdb.StatementType`)
# from reading as prose: the assertion must follow the bare name.
_BEHAVIOUR_TEMPLATE = (
    r"(?<![\w.]){lib}(?:'s)?(?![\w.])[^.;\n]{{0,60}}?\b"
    r"(?:classif|reject|treat|pars|return|rais|throw|accept|allow|permit|support|interpret|coerce|"
    r"default|recogni|handl|refus|disallow|forbid|ignor|strip|normali|convert)\w*"
)
# The note shows the library's own behaviour: a link, its docs or source, or a run of it.
_LIBRARY_EVIDENCE_RE = re.compile(
    r"https?://|\bdocs?\b|\bdocumentation\b|\bchangelog\b|site-packages|>>>|\bREPL\b|\bran\b|\bexecuted\b"
    r"|\breproduc\w*|\boutput\b|\bprints?\b|\bprinted\b",
    re.IGNORECASE,
)
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ()))


def verifier_note(finding: dict) -> str:
    """The verifier's own note: `note` up to the first segment the plugin appended."""
    kept = []
    for segment in str(finding.get("note") or "").split(" — "):
        if _PLUGIN_SEGMENT_RE.search(segment.strip()):
            break
        kept.append(segment)
    return " — ".join(kept).strip()


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text)} - _STOPWORDS


def _spans(text: str) -> list[str]:
    return [(fenced or inline).strip() for fenced, inline in _TICKS_RE.findall(text) if (fenced or inline).strip()]


def _strip_spans(text: str) -> str:
    return _TICKS_RE.sub(" ", text)


def is_restated(finding: dict) -> bool:
    """A confirmed note that quotes no code and says almost nothing the claim and evidence did
    not already say — the claim read back as its own proof."""
    note = verifier_note(finding)
    if not note or any(len(s) >= 3 for s in _spans(note)):
        return False  # no note to judge, or it quotes code (grounding checks the quote is real)
    words = _tokens(note)
    if len(words) < RESTATE_MIN_TOKENS:
        return False
    known = _tokens(f"{finding.get('claim') or ''}\n{finding.get('evidence') or ''}")
    return len(words & known) / len(words) >= RESTATE_OVERLAP


def library_candidates(finding: dict) -> set[str]:
    """Names the finding's quoted code calls into (`X.attr`) AND the claim or verifier note
    asserts behaviour of, in prose — before knowing whether X is a library. Stdlib excluded."""
    quoted = " ".join(
        _spans(f"{finding.get('claim') or ''}\n{finding.get('evidence') or ''}\n{verifier_note(finding)}")
    )
    names = {m.group(1) for m in _DOTTED_PREFIX_RE.finditer(quoted)}
    names = {n for n in names if n not in _STDLIB and n.lower() not in ("self", "cls", "this", "super")}
    if not names:
        return set()
    prose = _strip_spans(f"{finding.get('claim') or ''}\n{verifier_note(finding)}")
    return {n for n in names if re.search(_BEHAVIOUR_TEMPLATE.format(lib=re.escape(n)), prose, re.IGNORECASE)}


def unquoted_library(finding: dict, dependencies: set[str] | None) -> str:
    """The declared dependency whose behaviour this confirmed finding asserts without quoting
    its source, docs or a run of it — or "". `dependencies` None (unreadable) checks nothing."""
    if not dependencies:
        return ""
    if _LIBRARY_EVIDENCE_RE.search(verifier_note(finding)):
        return ""
    for name in sorted(library_candidates(finding)):
        if normalize_dist(name) in dependencies:
            return name
    return ""


def _is_confirmed(finding: dict) -> bool:
    return str(finding.get("verdict") or "").strip().lower() == "confirmed" and not finding.get("ungrounded")


def needs_dependencies(findings: list[dict]) -> bool:
    """Is any confirmed finding a library-semantics candidate — worth reading the manifests?"""
    return any(_is_confirmed(f) and library_candidates(f) for f in findings)


RESTATED_NOTE = (
    "the verifier's note restates the claim and quotes no code — not evidence; downgraded to "
    "uncertain, cannot gate a merge (issue #259)"
)


def unquoted_library_note(lib: str) -> str:
    return (
        f"asserts what the `{lib}` library does without quoting its source, docs or a run of it — "
        "downgraded to uncertain, cannot gate a merge (issue #259)"
    )


def apply_evidence_guard(findings: list[dict], dependencies: set[str] | None) -> tuple[list[dict], list[dict]]:
    """(findings, demoted). Position for position; a demoted finding is an annotated copy with
    `verdict: uncertain` and `evidence_restated` or `semantics_unquoted: <lib>`."""
    out: list[dict] = []
    demoted: list[dict] = []
    for f in findings:
        if not _is_confirmed(f):
            out.append(f)
            continue
        lib = unquoted_library(f, dependencies)
        if lib:
            reason, flag, value = unquoted_library_note(lib), "semantics_unquoted", lib
        elif is_restated(f):
            reason, flag, value = RESTATED_NOTE, "evidence_restated", True
        else:
            out.append(f)
            continue
        annotated = dict(f)
        annotated["verdict"] = "uncertain"
        annotated[flag] = value
        prior = str(annotated.get("note") or "").strip()
        annotated["note"] = f"{prior} — {reason}" if prior else reason
        out.append(annotated)
        demoted.append(
            {
                "file": str(f.get("file") or ""),
                "severity": str(f.get("severity") or ""),
                "kind": "library" if lib else "restated",
                "detail": lib,
            }
        )
    return out, demoted


def render_evidence_footnote(demoted: list[dict]) -> str:
    if not demoted:
        return ""
    lines = []
    for d in demoted:
        why = (
            f"asserts `{d['detail']}` behaviour without quoting its source, docs or a run of it"
            if d.get("kind") == "library"
            else "the verifier's note restates the claim and quotes no code"
        )
        lines.append(f"- `{d['file'] or '(no file)'}` ({d['severity'] or '?'}) — {why}")
    return (
        f"\n\n---\n_{len(demoted)} finding(s) downgraded to **uncertain**: a `confirmed` must rest on code or tool "
        f"output the claim did not already contain, and a claim about a library's behaviour on that library's "
        f"own source, docs or a run of it (issue #259). Each still stands for a human to judge._\n" + "\n".join(lines)
    )


# ── the repo's declared dependencies, from its manifests at head ──

MANIFESTS = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "package.json")
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name.strip().lower())


def parse_dependencies(manifests: dict[str, str]) -> set[str]:
    """Normalized dependency names declared in `{manifest path: text}`. A manifest that does not
    parse contributes nothing; the set may be empty."""
    deps: set[str] = set()
    for path, text in manifests.items():
        name = path.rsplit("/", 1)[-1]
        try:
            if name.endswith(".txt"):
                for line in text.splitlines():
                    if line.strip().startswith(("#", "-")):
                        continue
                    if m := _REQ_NAME_RE.match(line):
                        deps.add(normalize_dist(m.group(1)))
            elif name == "package.json":
                data = json.loads(text)
                for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                    for dep in (data.get(key) or {}) if isinstance(data, dict) else {}:
                        deps.add(normalize_dist(str(dep).rsplit("/", 1)[-1]))
            elif name == "pyproject.toml":
                deps |= _pyproject_dependencies(text)
        except Exception:  # noqa: BLE001 — an unparseable manifest declares nothing we can trust
            continue
    return deps


def _pyproject_dependencies(text: str) -> set[str]:
    import tomllib

    data = tomllib.loads(text)
    specs: list[str] = []
    project = data.get("project") or {}
    specs += [str(s) for s in project.get("dependencies") or []]
    for group in (project.get("optional-dependencies") or {}).values():
        specs += [str(s) for s in group or []]
    for group in (data.get("dependency-groups") or {}).values():
        specs += [str(s) for s in group or [] if isinstance(s, str)]
    poetry = (data.get("tool") or {}).get("poetry") or {}
    names = {normalize_dist(k) for k in (poetry.get("dependencies") or {})}
    for spec in specs:
        if m := _REQ_NAME_RE.match(spec):
            names.add(normalize_dist(m.group(1)))
    return names - {"python"}

"""Absence claims are settled by a search of the repo at head, not by a reading (issue #259).

An absence claim ("no test covers X", "`helper` is dead", "never called", "no `x.py` exists") is a
statement about the WHOLE repository. The verify step checks it against the diff and a handful of
files, so it confirms what it cannot see. The 2026-10 audit of Vera's reviews found 4 of the 5
tests/coverage claims false, and every one of them `confirmed`:

  protoAgent#4025   "no Python tests; no tests/ files in PR": the PR added
                    `tests/test_plugin_services.py` and `tests/test_artifact_vega.py`. The
                    30-file list the panel reasoned from had dropped every `tests/` path.
  protoAgent#4025   "vega-lite not covered": `test_artifact_vega.py` covers it.
  data-plugin#1     "the conftest `call` helper is dead" / "no behavioral test exercises any
                    tool": 100+ `call(` uses across 8 test files.

#209 (`grounding.ground_absence_claims`) already demotes a blocking test-absence when a test with
the module's NAME exists in the tree, or when the diff was truncated. This generalises it to a
content search, for every severity, inside a checkout of the PR head:

  * TEST family (`grounding.absence_kind == "test"`): the subjects the claim names (backticked
    identifiers and paths, plus the cited module's own name) are searched for in the repo's TEST
    files. A reference in a test file other than the cited one refutes the claim.
  * USAGE family ("dead", "unused", "never called", "no callers"): a backticked name the claim
    calls dead, and that the cited file DEFINES (def/class/function/…), is searched for across
    the repo's code. A reference that is not a definition and not a comment refutes the claim.
  * EXISTENCE family ("no `x.py` exists", "does not exist", "is missing"): a backticked file name
    (in the backticked directory, when the claim names one) is looked up in the head tree.

Disposition, mirroring #209 and never dropping a finding:

  * refuted (a hit) → `verdict: uncertain` + `ungrounded` + `absence_refuted: "<path>:<line>"`
    and a footnote naming the hit. Not dropped, unlike #240's lint refutation: a grep hit can be
    a homonym, and an uncertain finding still posts for a human while it can no longer FAIL.
    `verdict: refuted` is never written here: `verdicts.verdict_for` reads an unknown or
    `refuted` blocker/major as a FAIL, so writing it would turn a refutation into a block.
  * searched, no hit → unchanged, with `absence_searched` recording what was searched.
  * not searched (no checkout, a git error, the budget ran out) → a `confirmed` finding becomes
    `uncertain` + `absence_unsearched`: an absence is confirmed only by a search. Never raises.

What is NOT checked: a claim with no backticked subject, a dead local variable or field (it has
no definition this can find), and a code-shape absence ("used without validation", "not
handled"), which a search cannot settle. Those keep whatever the verifier said.

The checkout is the structural pass's own (`checkout_cache.CheckoutCache`, keyed by repo@head):
a cache hit costs nothing. The LLM lanes have no checkout of their own (#240), so on a miss (the
small-diff recipe has no structural seat) a blobless clone at head is made, shielded from the
search's budget so a slow clone never leaves a half-written cache entry behind. The GitHub code
search API is no substitute: it indexes only the default branch, so it cannot see a PR head, and
the contents API reads one file per call, which cannot search a repo.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .grounding import _TICKS_RE, _is_test_path, _module_stem, absence_kind

log = logging.getLogger("protoagent.plugins.pr_reviewer")

TEST = "test"
USAGE = "usage"
EXISTENCE = "existence"

REFUTED = "refuted"
NO_HIT = "no-hit"
UNSEARCHED = "unsearched"
NO_QUERY = "no-query"  # a candidate the checkout showed has nothing searchable (a dead local)

MAX_CANDIDATES = 12  # absence findings searched per round; the rest are unsearched ⇒ uncertain
MAX_SUBJECTS = 8  # subjects per finding
MAX_OUTPUT_LINES = 20_000  # grep output lines read per search
GIT_TIMEOUT_S = 20

# Verdicts this pass acts on. `refuted-before` / `possibly-addressed` rows were settled elsewhere.
_SEARCHABLE_VERDICTS = ("", "confirmed", "uncertain")

# "dead", "unused", "never called", "no callers", "no test uses it" — about USE, not tests.
_USAGE_RE = re.compile(
    r"\bdead\b"
    r"|\bun(?:used|referenced|called)\b"
    r"|\bnever\s+(?:\w+\s+)?(?:called|used|invoked|referenced|imported|read)\b"
    r"|\bno\s+(?:\w+\s+){0,2}?(?:callers?|call[\s-]sites?|references|usages?)\b"
    r"|\bnot\s+(?:called|used|referenced|imported)\s+(?:anywhere|by\s+any)\b"
    r"|\bno\s+(?:\w+\s+)?tests?\s+(?:uses?|calls?|references?)\b",
    re.IGNORECASE,
)

# "no `x.py` module exists", "does not exist", "is missing" — about a FILE being there.
_EXISTENCE_RE = re.compile(
    r"\b(?:does\s+not|doesn't|do\s+not|don't)\s+exist\b"
    r"|\bno\s+(?:\S+\s+){0,3}?(?:file|module|script|package)\s+(?:\S+\s+){0,2}?exists?\b"
    r"|\bno\s+such\s+(?:file|module|script|package)\b"
    r"|\b(?:is|are)\s+missing\b"
    r"|\bmissing\s+(?:file|module|script)\b",
    re.IGNORECASE,
)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORDISH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*[A-Za-z0-9_]$")  # `vega-lite`
_DOTTED_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_FILENAME_RE = re.compile(r"^[\w./-]*[\w-]\.[A-Za-z0-9]{1,6}$")
_FILE_EXTS = frozenset(
    "py pyi js jsx ts tsx mjs cjs vue svelte go rs rb java kt kts scala php cs swift c h cc cpp hpp sh bash "
    "yml yaml toml json md rst txt cfg ini lock sql html css".split()
)
_CALL_SUFFIX_RE = re.compile(r"\s*\((?:\.\.\.|[^()]*)\)\s*$")

# Too generic to name a subject, whatever the claim says.
_STOP = frozenset(
    "self cls this none null true false def class function return import from async await "
    "test tests main init new get set run data value values item items list dict str int "
    "args kwargs config path file name type".split()
)

# The code files a usage / test search reads. Docs, lockfiles and data are not references.
CODE_GLOBS = tuple(
    f"*.{ext}"
    for ext in (
        "py pyi js jsx ts tsx mjs cjs vue svelte go rs java kt kts scala rb php cs fs swift "
        "c h cc cpp hpp m mm sh bash zsh lua ex exs erl clj dart r jl pl"
    ).split()
)
_EXCLUDE_GLOBS = ("*.min.js", "*.bundle.js")

_COMMENT_RE = re.compile(r"^\s*(?:#|//|/\*|\*|--|;)")


# A line that DEFINES `name` (Python, JS/TS, Go, Rust, Ruby, shell, …): not a use of it.
def _def_re(name: str) -> re.Pattern:
    n = re.escape(name)
    return re.compile(
        rf"^\s*(?:export\s+)?(?:default\s+)?(?:pub(?:\([\w:]+\))?\s+)?(?:async\s+)?"
        rf"(?:def|class|function\*?|fn|func|sub|interface|struct|enum|trait|type)\s+{n}\b"
        rf"|^\s*(?:export\s+)?(?:const|let|var)\s+{n}\s*=\s*(?:async\s*)?(?:function\b|\()"
        rf"|^\s*func\s+\([^)]*\)\s*{n}\b"
        rf"|^\s*{n}\s*\(\)\s*\{{",
        re.MULTILINE,
    )


_LANGUAGES = {
    "py": "py",
    "pyi": "py",
    "js": "js",
    "jsx": "js",
    "ts": "js",
    "tsx": "js",
    "mjs": "js",
    "cjs": "js",
    "vue": "js",
    "svelte": "js",
    "kt": "jvm",
    "kts": "jvm",
    "java": "jvm",
    "scala": "jvm",
    "c": "c",
    "h": "c",
    "cc": "c",
    "cpp": "c",
    "hpp": "c",
}


def _language(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return _LANGUAGES.get(ext, ext)


def distinctive(name: str) -> bool:
    """A name specific enough that ANY word-boundary mention of it is a reference to it:
    `plugin_services`, `vega-lite`, `showService`, `check_bundle_updates`. A short plain word
    (`call`, `run`, `sdk`) is matched only in a call or import, never in prose."""
    return len(name) >= 12 or "_" in name.strip("_") or "-" in name or bool(re.search(r"[a-z][A-Z]", name))


def absence_families(finding: dict) -> list[str]:
    """The searchable absence families the finding's CLAIM asserts — [] for none. A code-shape
    absence ("used without validation", `absence_kind == "other"`) is never searchable: a test
    file or a reference elsewhere says nothing about the cited code's own shape (#209)."""
    kind = absence_kind(finding)
    if kind == "other":
        return []
    claim = str(finding.get("claim") or "")
    families = []
    if kind == TEST:
        families.append(TEST)
    if _USAGE_RE.search(claim):
        families.append(USAGE)
    if _EXISTENCE_RE.search(claim):
        families.append(EXISTENCE)
    return families


def claim_subjects(finding: dict) -> tuple[list[str], list[str], list[str]]:
    """(names, file names, directories) the CLAIM names in backticks. Only the claim: the
    evidence quotes context (`gh`, a neighbouring call) that is not what the claim says is
    absent. Test names (`test_*`) are dropped: they name the test, not its subject."""
    names: dict[str, None] = {}
    files: dict[str, None] = {}
    dirs: dict[str, None] = {}
    for fenced, inline in _TICKS_RE.findall(str(finding.get("claim") or "")):
        if fenced:
            continue
        span = _CALL_SUFFIX_RE.sub("", inline.strip())
        if not span or " " in span:
            continue
        if span.endswith("/"):
            dirs.setdefault(span.strip("/"), None)
        elif "/" in span or (_FILENAME_RE.match(span) and span.rsplit(".", 1)[-1].lower() in _FILE_EXTS):
            files.setdefault(span.lstrip("./"), None)
        elif _DOTTED_RE.match(span):
            # `pkg.module` / `obj.attr`: its last part is the subject a test would mention.
            names.setdefault(span.rsplit(".", 1)[-1], None)
        elif _IDENT_RE.match(span) or _WORDISH_RE.match(span):
            names.setdefault(span, None)
    keep = [n for n in names if len(n.strip("_")) >= 3 and n.lower() not in _STOP and not n.startswith("test")]
    return keep[:MAX_SUBJECTS], list(files)[:MAX_SUBJECTS], list(dirs)[:MAX_SUBJECTS]


def _test_subjects(finding: dict) -> dict[str, str]:
    """What a test of the claim's subject would mention → how to recognise it: the backticked
    names (`"name"`), and the stems of the backticked paths plus the cited module's own stem
    (`"module"`, unless the cited file IS a test). A module stem is matched only as a MODULE
    reference — an import, `pkg.stem`, `dir/stem` — never as a bare word: `registry.py`'s stem
    is also English ("the registry's requirement")."""
    names, files, _dirs = claim_subjects(finding)
    out: dict[str, str] = dict.fromkeys(names, "name")
    cited = str(finding.get("file") or "")
    for path in [*files, cited]:
        if not path or _is_test_path(path):
            continue
        stem = _module_stem(path)
        if len(stem.strip("_")) >= 3 and stem.lower() not in _STOP and stem.lower() != "__init__":
            out.setdefault(stem, "module")
    return dict(list(out.items())[:MAX_SUBJECTS])


def is_candidate(finding: dict) -> bool:
    """Is this a finding the search acts on: a searchable absence family, a verdict this pass
    may move, not already demoted by grounding, and at least one subject to search for?"""
    if str(finding.get("verdict") or "").strip().lower() not in _SEARCHABLE_VERDICTS:
        return False
    if finding.get("ungrounded") or finding.get("absence_refuted"):
        return False
    families = absence_families(finding)
    if not families:
        return False
    names, files, _dirs = claim_subjects(finding)
    if TEST in families and _test_subjects(finding):
        return True
    if USAGE in families and names:
        return True
    return EXISTENCE in families and bool(files)


def _uses(name: str, line: str) -> bool:
    """Does `line` USE `name`? A distinctive name: any word-boundary mention. A plain word: only
    a call (`call(`, not `obj.call(`) or an import of it."""
    word = re.escape(name)
    if distinctive(name):
        return bool(re.search(rf"(?<![\w-]){word}(?![\w-])", line))
    return bool(
        re.search(rf"(?<![\w.]){word}\s*\(", line)
        or re.search(rf"\bimport\s+(?:[\w.]+\s*,\s*)*(?:[\w.]+\.)?{word}\b", line)
        or re.search(rf"\bfrom\s+[\w.]+\s+import\s+(?:\(?\s*[\w\s,]*\b)?{word}\b", line)
        or re.search(rf"\bfrom\s+(?:[\w.]*\.)?{word}\s+import\b", line)
        or re.search(rf"\brequire\(\s*['\"][^'\"]*\b{word}['\"]", line)
        or re.search(rf"\bfrom\s+['\"][^'\"]*[/.]{word}(?:\.\w+)?['\"]", line)
    )


def _module_uses(stem: str, line: str) -> bool:
    """Does `line` reference the MODULE `stem` — import it, or name it as `pkg.stem` / `dir/stem`?"""
    word = re.escape(stem)
    return bool(
        re.search(rf"\bimport\s+(?:[\w.]+\s*,\s*)*(?:[\w.]+\.)?{word}\b", line)
        or re.search(rf"\bfrom\s+(?:[\w.]*\.)?{word}\s+import\b", line)
        or re.search(rf"\bfrom\s+[\w.]+\s+import\s+(?:\(?\s*[\w\s,]*\b)?{word}\b", line)
        or re.search(rf"[\w)]\.{word}(?![\w-])", line)
        or re.search(rf"[/\\]{word}(?:\.\w+)?(?![\w-])", line)
        or re.search(rf"\brequire\(\s*['\"][^'\"]*\b{word}['\"]", line)
        or ("_" in stem.strip("_") and re.search(rf"(?<![\w.]){word}\.[A-Za-z_]", line))
    )


# Checkout resolves that outlived their search's budget, still cloning (see `_checkout`).
_BACKGROUND: set[asyncio.Task] = set()


def _settled(task: asyncio.Task) -> None:
    _BACKGROUND.discard(task)
    if not task.cancelled():
        task.exception()  # retrieved, so a failed background clone is not an unhandled-exception log


RunGit = Callable[[list[str], int], Awaitable[tuple[int, str, str]]]
ResolveCheckout = Callable[[str, str], Awaitable["Path | None"]]


class AbsenceSearcher:
    """Searches the head checkout for what an absence claim says is not there. One per round;
    `resolve_checkout(repo, head)` and `run_git(args, timeout_s)` are the seams."""

    def __init__(self, cfg: dict | None = None, *, resolve_checkout: ResolveCheckout, run_git: RunGit | None = None):
        cfg = cfg or {}
        self.budget_s = float(cfg.get("absence_search_budget_s") or 120)
        self._resolve = resolve_checkout
        if run_git is None:
            from .checkout_cache import _default_run_git

            run_git = _default_run_git
        self._run_git = run_git

    async def check(self, repo: str, head: str, findings: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
        """(findings, refuted, unsearched) — `findings` position for position, annotated copies
        where something changed. Never raises: on any failure every candidate is unsearched."""
        candidates = [i for i, f in enumerate(findings) if is_candidate(f)]
        if not candidates:
            return list(findings), [], []
        outcomes: dict[int, tuple[str, str, list[str]]] = {}
        try:
            outcomes = await self._search(repo, head, findings, candidates[:MAX_CANDIDATES])
        except Exception:  # noqa: BLE001 — a search bug must never cost a finding or the round
            log.exception("[pr-reviewer] %s absence search failed; absence claims left unsearched", repo)
            outcomes = {}
        out = list(findings)
        refuted: list[dict] = []
        unsearched: list[dict] = []
        for i in candidates:
            outcome, detail, searched = outcomes.get(i, (UNSEARCHED, "", []))
            f = findings[i]
            row = {"file": str(f.get("file") or ""), "severity": str(f.get("severity") or ""), "detail": detail}
            if outcome == REFUTED:
                out[i] = _annotate(f, verdict="uncertain", note=refuted_note(detail), absence_refuted=detail)
                out[i]["ungrounded"] = True
                refuted.append(row)
            elif outcome == NO_HIT:
                out[i] = {**f, "absence_searched": searched}
            elif outcome == NO_QUERY:
                continue
            elif str(f.get("verdict") or "").strip().lower() in ("", "confirmed"):
                out[i] = _annotate(f, verdict="uncertain", note=UNSEARCHED_NOTE, absence_unsearched=True)
                unsearched.append(row)
        return out, refuted, unsearched

    async def _search(
        self, repo: str, head: str, findings: list[dict], candidates: list[int]
    ) -> dict[int, tuple[str, str, list[str]]]:
        deadline = time.monotonic() + self.budget_s
        root = await self._checkout(repo, head, deadline)
        if root is None:
            return {}
        tree: set[str] | None = None
        outcomes: dict[int, tuple[str, str, list[str]]] = {}
        for i in candidates:
            if time.monotonic() >= deadline:
                break  # the rest stay unsearched ⇒ uncertain
            f = findings[i]
            families = absence_families(f)
            names, files, dirs = claim_subjects(f)
            searched: list[str] = []
            results: list[tuple[str, str]] = []
            if TEST in families and (subjects := _test_subjects(f)):
                searched += [f"tests: {s}" for s in subjects]
                results.append(await self._test_hit(root, head, f, subjects, deadline))
            if USAGE in families and names:
                usage = await self._usage_hit(root, head, f, names, deadline)
                if usage is not None:
                    searched += [f"uses: {n}" for n in usage[2]]
                    results.append(usage[:2])
            if EXISTENCE in families and files:
                if tree is None:
                    tree = await self._tree(root, head, deadline)
                searched += [f"path: {p}" for p in files]
                if tree is None:
                    results.append((UNSEARCHED, ""))
                else:
                    hit = _existing(files, dirs, tree)
                    results.append((REFUTED, hit) if hit else (NO_HIT, ""))
            if not results:
                # Nothing this search could form a query for (a dead local has no definition to
                # anchor on): left exactly as the verifier said.
                outcomes[i] = (NO_QUERY, "", [])
                continue
            # One hit refutes; otherwise any search that could not run leaves the claim unsearched.
            for outcome in (REFUTED, UNSEARCHED, NO_HIT):
                if found := next((r for r in results if r[0] == outcome), None):
                    outcomes[i] = (found[0], found[1], searched)
                    break
        return outcomes

    async def _checkout(self, repo: str, head: str, deadline: float) -> Path | None:
        """The head checkout, or None. The resolve is SHIELDED from the budget: a clone cut off
        mid-write would leave a half-checkout the cache then serves as a hit, so a slow clone is
        left to finish (or fail and clean up) in the background, and this round is unsearched."""
        task = asyncio.ensure_future(self._resolve(repo, head))
        _BACKGROUND.add(task)  # held strongly: the event loop keeps only a weak reference
        task.add_done_callback(_settled)
        try:
            root = await asyncio.wait_for(asyncio.shield(task), timeout=max(deadline - time.monotonic(), 0.1))
        except asyncio.TimeoutError:
            log.warning("[pr-reviewer] %s@%s checkout for the absence search overran its budget", repo, head[:12])
            return None
        except Exception as exc:  # noqa: BLE001 — no checkout ⇒ unsearched, never a raise
            log.warning("[pr-reviewer] %s@%s checkout for the absence search failed: %s", repo, head[:12], exc)
            return None
        return Path(root) if root else None

    async def _git(self, args: list[str], deadline: float) -> tuple[int, str]:
        timeout = int(max(min(GIT_TIMEOUT_S, deadline - time.monotonic()), 1))
        rc, out, _err = await self._run_git(args, timeout)
        return rc, out

    async def _grep(
        self, root: Path, head: str, names: list[str], deadline: float
    ) -> list[tuple[str, int, str]] | None:
        """(path, line, text) of every word-boundary mention of any of `names` in the code files
        at `head` — [] for none, None when the search failed (an unknown, never a 'no hit')."""
        if not names:
            return []
        args = ["-C", str(root), "grep", "-n", "-I", "-F", "-w", "--no-color"]
        for name in names:
            args += ["-e", name]
        args += [head, "--", *CODE_GLOBS, *(f":(exclude){g}" for g in _EXCLUDE_GLOBS)]
        rc, out = await self._git(args, deadline)
        if rc == 1:
            return []
        if rc != 0:
            return None
        prefix = f"{head}:"
        hits: list[tuple[str, int, str]] = []
        for raw in out.splitlines()[:MAX_OUTPUT_LINES]:
            if raw.startswith(prefix):
                raw = raw[len(prefix) :]
            m = re.match(r"^(.+?):(\d+):(.*)$", raw)
            if m:
                hits.append((m.group(1), int(m.group(2)), m.group(3)[:400]))
        return hits

    async def _test_hit(
        self, root: Path, head: str, finding: dict, subjects: dict[str, str], deadline: float
    ) -> tuple[str, str]:
        hits = await self._grep(root, head, list(subjects), deadline)
        if hits is None:
            return UNSEARCHED, ""
        cited = str(finding.get("file") or "").lstrip("./")
        lang = _language(cited)
        defs = {s: _def_re(s) for s in subjects}
        valid: list[tuple[str, int, bool, bool]] = []
        for path, line, text in hits:
            if path == cited or not _is_test_path(path) or _COMMENT_RE.match(text):
                continue
            same_lang = bool(lang) and _language(path) == lang
            for s, kind in subjects.items():
                if defs[s].search(text):
                    continue
                if kind == "module":
                    used, strong = _module_uses(s, text), True
                else:
                    # A plain word (`call`, `service`) only counts in a test in the cited file's
                    # own language: `service(` in a TypeScript e2e test does not test a Python module.
                    used, strong = _uses(s, text) and (distinctive(s) or same_lang), distinctive(s)
                if used:
                    valid.append((path, line, same_lang, strong))
                    break
        if not valid:
            return NO_HIT, ""
        per_file: dict[str, int] = {}
        for path, *_ in valid:
            per_file[path] = per_file.get(path, 0) + 1
        # The most telling hit: same language, a distinctive subject, the file that mentions it most.
        path, line, *_ = min(valid, key=lambda v: (not v[2], not v[3], -per_file[v[0]], v[0], v[1]))
        return REFUTED, f"{path}:{line}"

    async def _usage_hit(
        self, root: Path, head: str, finding: dict, names: list[str], deadline: float
    ) -> tuple[str, str, list[str]] | None:
        """Only a name the cited file DEFINES is searched: a dead local or field has no
        definition to anchor on and a repo-wide search for its name would hit homonyms."""
        cited = str(finding.get("file") or "").lstrip("./")
        if not names or not cited:
            return None
        rc, source = await self._git(["-C", str(root), "show", f"{head}:{cited}"], deadline)
        if rc != 0:
            return UNSEARCHED, "", []
        defined = [n for n in names if _IDENT_RE.match(n) and _def_re(n).search(source)]
        if not defined:
            return None
        hits = await self._grep(root, head, defined, deadline)
        if hits is None:
            return UNSEARCHED, "", defined
        defs = {n: _def_re(n) for n in defined}
        for path, line, text in hits:
            if _COMMENT_RE.match(text):
                continue
            for n in defined:
                if _uses(n, text) and not defs[n].search(text):
                    return REFUTED, f"{path}:{line}", defined
        return NO_HIT, "", defined

    async def _tree(self, root: Path, head: str, deadline: float) -> set[str] | None:
        rc, out = await self._git(["-C", str(root), "ls-tree", "-r", "--name-only", head], deadline)
        return {p.strip() for p in out.splitlines() if p.strip()} if rc == 0 else None


def _existing(files: list[str], dirs: list[str], tree: set[str]) -> str:
    """The first head path that a 'no such file' claim names, or "". A claim that also names a
    directory is held to it: "no `scorecard.py` in `evals/runners/`" is not refuted by an
    `evals/scorecard.py` elsewhere."""
    for name in files:
        name = name.strip("/")
        if name in tree:
            return name
        if "/" in name:
            if hit := next((p for p in sorted(tree) if p.endswith("/" + name)), None):
                return hit
            continue
        for path in sorted(tree):
            if path.rsplit("/", 1)[-1] != name:
                continue
            if not dirs or any(path == f"{d}/{name}" or path.endswith(f"/{d}/{name}") for d in dirs):
                return path
    return ""


def _annotate(finding: dict, *, verdict: str, note: str, **flags) -> dict:
    out = dict(finding)
    out["verdict"] = verdict
    out.update(flags)
    prior = str(out.get("note") or "").strip()
    out["note"] = f"{prior} — {note}" if prior else note
    return out


def refuted_note(hit: str) -> str:
    return (
        f"absence refuted by a repo-wide search at the reviewed head: referenced at `{hit}` — "
        "downgraded to uncertain, cannot gate a merge (issue #259)"
    )


UNSEARCHED_NOTE = (
    "an absence claim the repo could not be searched for at the reviewed head — downgraded to "
    "uncertain: an absence is confirmed only by a search (issue #259)"
)


def render_absence_search_footnote(refuted: list[dict], unsearched: list[dict]) -> str:
    """The posted-body note for absence claims the search refuted or could not run for."""
    if not refuted and not unsearched:
        return ""
    lines = [
        f"- `{r['file'] or '(no file)'}` ({r['severity'] or '?'}) — referenced at `{r['detail']}`" for r in refuted
    ] + [
        f"- `{r['file'] or '(no file)'}` ({r['severity'] or '?'}) — the repo could not be searched" for r in unsearched
    ]
    return (
        f"\n\n---\n_{len(refuted) + len(unsearched)} absence finding(s) (“no test” / “dead” / “never called” / "
        f"“does not exist”) downgraded to **uncertain**: an absence is a claim about the whole repo, so it is "
        f"checked by a search of the repo at this head, and a reference found there refutes it (issue #259). "
        f"Each still stands for a human to judge._\n" + "\n".join(lines)
    )


def checkout_resolver(cfg: dict | None) -> ResolveCheckout:
    """The default `resolve_checkout`: the structural pass's checkout cache (same root, same
    repo@head key), cloning on a miss unless `absence_search_clone` is false. Host-free."""
    from .checkout_cache import CheckoutCache, checkout_root_for

    cfg = cfg or {}
    cache = CheckoutCache(
        checkout_root_for(cfg),
        ttl_s=int(cfg.get("checkout_ttl_s") or 3600),
        entry_limit=int(cfg.get("checkout_max_entries") or 50),
        size_limit_bytes=int(cfg.get("checkout_max_bytes") or 5 * 1024**3),
    )
    clone = str(cfg.get("absence_search_clone", True)).strip().lower() not in ("false", "0", "no", "off")

    async def resolve(repo: str, head: str) -> Path | None:
        target = cache.dir_for(repo, head)
        hit = target.is_dir() and (time.time() - target.stat().st_mtime) < cache.ttl_s
        if not hit and not clone:
            return None
        token = None
        if not hit:
            from .gh_cli import resolve_token, run_gh

            token = resolve_token()
            if not token:
                rc, out, _err = await run_gh(["auth", "token"], timeout=10)
                token = out.strip() if rc == 0 and out.strip() else None
        path = await cache.resolve(repo, head, token)
        if not hit:
            try:
                await asyncio.to_thread(cache.prune)  # our clone counts against the same caps
            except Exception:  # noqa: BLE001 — maintenance is best-effort
                log.exception("[pr-reviewer] checkout cache prune after the absence-search clone failed")
        return path

    return resolve


# What the dispatcher calls for its default resolver — a separate name so the test suite can
# swap it for "no checkout" (tests/conftest.py) without losing `checkout_resolver` to test.
default_resolver = checkout_resolver

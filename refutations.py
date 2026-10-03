"""Per-repo memory of structural claims the verifier refuted (issue #190).

protoPatch's findings are deterministic per content, so a false one comes back on every
PR that touches the file: *"builtin_world panics via .expect()"* was raised on four
mythxengine-sdk PRs and refuted each time it was verified — a verify round per
resurfacing, twice feeding the `nothing-to-verify` contradiction. The panel already
remembers refutations WITHIN a PR (`prior_requests`); this is the memory ACROSS PRs.

One JSON file per repo under the protoPatch state root (`<state_root>/<owner-name>/
refuted.json`), written when a round posts a `source: protopatch` finding the verifier
marked `refuted`, read by the structural pass to hand the synthesizer a repeat already
marked — unless this PR changes the file at that location, in which case the claim is
live again and reports as new. Aged out after `ttl_days`. Fails open: an unreadable
store pre-marks nothing.
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import os
import re
import tempfile
import time
from pathlib import Path

# The one tokenizer (#251) — re-exported here, where the stores and their tests import it.
from .verdicts import identifier_tokens

log = logging.getLogger("protoagent.plugins.pr_reviewer")

DEFAULT_TTL_DAYS = 14
SAME_CLAIM_RATIO = 0.8  # SequenceMatcher on normalised claims — above what boilerplate alone reaches
SAME_CLAIM_LINES = 25  # a remembered refutation applies at (about) the line it was refuted at


# Clock skew a store written on another host may carry; anything further ahead is corrupt.
_FUTURE_SLACK_S = 86400


def _ts(entry: dict) -> float:
    """An entry's `at` as a timestamp — 0.0 for anything that is not a finite number, or that
    lies more than a day in the future (#254).

    The store only ever writes `time.time()`, so either is a corrupt or hand-edited file. A
    reader that raised on it would break *degrade, never raise* (the structural pre-mark runs
    inside the protopatch pass), and a far-future `at` would never age out; 0.0 is older than
    every cutoff, so the entry just ages out."""
    try:
        value = float(entry.get("at") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(value) or value > time.time() + _FUTURE_SLACK_S:
        return 0.0
    return value


def _day(entry: dict) -> str:
    """`at` as YYYY-MM-DD for a note; "unknown date" when it cannot be one."""
    try:
        return time.strftime("%Y-%m-%d", time.gmtime(_ts(entry)))
    except (OverflowError, OSError, ValueError):
        return "unknown date"


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via a unique temp file in the same directory + `os.replace`, so a
    crash mid-write never leaves a truncated store and two writers never share a temp file.
    Raises OSError like `write_text`; the temp file is removed on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _norm_path(path: str) -> str:
    """Strip a leading `./` PREFIX and a leading `/` — never leading characters: `.github/x.yml`
    keeps its dot (review on #194: `lstrip("./")` made every dot-prefixed file look untouched).
    Same rule as `rounds._norm` (#247), so a claim keyed `/src/x.py` by one finder matches
    `src/x.py` from another."""
    path = str(path or "").strip()
    while path.startswith("./"):
        path = path[2:]
    return path.removeprefix("/")


def same_claim(a: str, b: str) -> bool:
    """Near-identical wording that names the same things: two claims about different
    sites share their boilerplate and differ exactly in an identifier (`list_users()` vs
    `delete_user()`), and must never match however close the wording. Tokens come from the
    RAW claims, as in `verdicts._same_defect` — lower-casing first would hide camelCase."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if identifier_tokens(a) != identifier_tokens(b):
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= SAME_CLAIM_RATIO


def _root_and_ttl(cfg: dict) -> tuple[Path, int]:
    """The state root and TTL every refutation store is built from — structural (#190) and
    LLM-lane (#207) alike, so the two memories age out on the same configured clock."""
    import os

    home = Path(os.environ.get("PR_REVIEWER_HOME") or Path.home() / ".protoagent" / "pr-reviewer")
    root = Path((cfg or {}).get("state_root") or home / "clawpatch")
    ttl = (cfg or {}).get("refutation_ttl_days")
    return root, int(ttl) if ttl else DEFAULT_TTL_DAYS


class RefutationStore:
    def __init__(self, root: Path, *, ttl_days: int = DEFAULT_TTL_DAYS):
        self.root = Path(root)
        self.ttl_s = max(1, int(ttl_days)) * 86400

    @classmethod
    def from_cfg(cls, cfg: dict) -> "RefutationStore":
        """The ONE way both the writer (dispatcher) and the reader (structural pass) build
        the store, so root and TTL cannot drift apart between them (review on #194)."""
        root, ttl = _root_and_ttl(cfg)
        return cls(root, ttl_days=ttl)

    def _path(self, repo: str) -> Path:
        return self.root / repo.replace("/", "-") / "refuted.json"

    def _load(self, repo: str) -> list[dict]:
        try:
            data = json.loads(self._path(repo).read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        cutoff = time.time() - self.ttl_s
        return [e for e in data if isinstance(e, dict) and _ts(e) >= cutoff]

    def record(self, repo: str, findings: list[dict], *, pr: int, head: str) -> int:
        """Remember every posted `source: protopatch` finding the verifier refuted. Returns
        how many were written. Never raises: a full disk loses memory, not the review."""
        refuted = [
            f
            for f in findings or []
            if isinstance(f, dict)
            and str(f.get("source") or "").lower() == "protopatch"
            and str(f.get("verdict") or "").lower() == "refuted"
            and f.get("file")
            and f.get("claim")
        ]
        if not refuted:
            return 0
        entries = self._load(repo)
        now = time.time()
        for f in refuted:
            entries = [
                e
                for e in entries
                if not (
                    _norm_path(e.get("file", "")) == _norm_path(f["file"])
                    and same_claim(e.get("claim", ""), f["claim"])
                )
            ]
            entries.append(
                {
                    "file": _norm_path(str(f["file"])),
                    "line": f.get("line"),
                    "claim": str(f["claim"]),
                    "note": str(f.get("note") or "")[:300],
                    "pr": int(pr),
                    "head": str(head or "")[:12],
                    "at": now,
                }
            )
        try:
            _atomic_write(self._path(repo), json.dumps(entries, indent=2))
        except OSError:
            log.exception("[pr-reviewer] refutation store write failed for %s", repo)
            return 0
        return len(refuted)

    def match(self, repo: str, file: str, claim: str, line: int | None = None) -> dict | None:
        """The remembered refutation of this claim on this file, or None. With `line`, only
        a refutation recorded within `SAME_CLAIM_LINES` of it counts — a different site
        whose claim shares boilerplate wording is a different finding, never pre-marked."""
        nf = _norm_path(file)
        for e in self._load(repo):
            if _norm_path(e.get("file", "")) != nf or not same_claim(e.get("claim", ""), claim):
                continue
            if line is not None and e.get("line") is not None:
                try:
                    if abs(int(e["line"]) - int(line)) > SAME_CLAIM_LINES:
                        continue
                except (TypeError, ValueError):
                    continue
            return e
        return None


def premark_refuted(
    findings: list[dict], store: RefutationStore, repo: str, changed_ranges: dict[str, list[tuple[int, int]]] | None
) -> int:
    """Mark, in place, every structural finding whose claim this repo already refuted —
    unless the PR changes the file within a few lines of it, when the claim is live again.
    Returns how many were marked. `changed_ranges` None ⇒ the diff is unknown ⇒ mark
    nothing (fail open: an unverifiable repeat is reported, not hidden)."""
    if changed_ranges is None:
        return 0
    marked = 0
    for f in findings:
        if str(f.get("source") or "").lower() != "protopatch":
            continue
        line = int(f.get("line") or 0)
        hit = store.match(repo, str(f.get("file") or ""), str(f.get("claim") or ""), line)
        if not hit:
            continue
        touched = any(a - 3 <= line <= b + 3 for a, b in changed_ranges.get(_norm_path(str(f.get("file") or "")), []))
        if touched:
            continue
        when = _day(hit)
        f["verdict"] = "refuted"
        f["refuted_before"] = f"#{hit.get('pr')} @{hit.get('head')} {when}"
        note = str(hit.get("note") or "").strip()
        f["note"] = (
            f"Refuted before on #{hit.get('pr')} @{hit.get('head')} ({when}); this PR does not change the file there."
            + (f" Verifier then: {note}" if note else "")
        )
        marked += 1
    return marked


# ── LLM-lane memory (#207) ────────────────────────────────────────────────────
#
# The LLM finders' false positives recur across PRs the same way protoPatch's do: on
# mythxengine-sdk the `builtin_world` `.expect()` premise was raised on #387 and again on
# #388 (the function returns `Result` and uses `?`), each time costing a verify round and,
# when the verifier agreed with the finder, a wrong WARN the author had to disprove by hand.
#
# The LLM lanes cannot be pre-marked the way the structural pass is: their findings are
# model text the plugin never sees until the whole panel has run. So the remembered claims
# go to the synthesizer as a data block (`render_refuted_before`), it marks a repeat
# `verdict: "refuted-before"` and the verifier passes that row through unverified — and
# then the DISPATCHER decides, deterministically, whether the mark holds
# (`settle_refuted_before`). The model only proposes; relief is granted here and fails
# CLOSED: a mark that does not hold leaves the finding live and unverified, which
# `verdict_for` trusts like any unverified finding and `verification_ran` refuses to call
# verified. The host's findings parser does not know `refuted-before` and reads it as no
# verdict at all — also live — so neither reader can turn a stray mark into relief.

REFUTED_BEFORE = "refuted-before"
TOUCH_LINES = 3  # the #194 rule: a PR change within this many lines of the claim is new evidence
MAX_RENDERED = 40  # remembered claims handed to the synthesizer per round
MAX_HARVESTED_REVIEWS = 500
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
_MARK_SPELLINGS = frozenset({"refuted-before", "refuted_before", "refuted before"})
_VERIFIER_VERDICTS = frozenset({"confirmed", "uncertain", "refuted"})


def is_llm_finding(finding: dict) -> bool:
    """An LLM panel finder's finding: no `source` (ADR 0077 — `source` names an engine)."""
    return isinstance(finding, dict) and not str(finding.get("source") or "").strip()


def finding_lane(finding: dict) -> str:
    """The finder angle a finding is attributed to. The synthesizer merges lanes, so the
    finding's `category` (correctness / removed-behavior / cross-file / conventions / …)
    is the lane that survives the merge; a finding without one is `llm`."""
    return str(finding.get("category") or "").strip().lower() or "llm"


def is_refuted_before(finding: dict) -> bool:
    return str(finding.get("verdict") or "").strip().lower() in _MARK_SPELLINGS


def _line(finding: dict) -> int:
    try:
        line = int(finding.get("line") or 0)
    except (TypeError, ValueError):
        return 0
    return line if line > 0 else 0


def _key(finding: dict) -> tuple[str, int, str]:
    return (_norm_path(str(finding.get("file") or "")), _line(finding), _norm(str(finding.get("claim") or "")))


def _rememberable(finding: dict) -> bool:
    """What could ever be pre-marked: an LLM finding with a file, a line, a claim that
    names something, and not a blocker. Anything else is never stored."""
    return (
        is_llm_finding(finding)
        and bool(str(finding.get("file") or "").strip())
        and _line(finding) > 0
        and bool(identifier_tokens(str(finding.get("claim") or "")))
        and str(finding.get("severity") or "").strip().lower() != "blocker"
    )


def _repo_parts(repo: str) -> tuple[str, str] | None:
    """`owner/name`, lower-cased, or None. One directory per owner and one file per name —
    never `owner-name`, which `a-b/c` and `a/b-c` share — and nothing that could walk out
    of the store root."""
    parts = str(repo or "").strip().lower().split("/")
    if len(parts) != 2 or any(p in ("", ".", "..") or not _REPO_PART.match(p) for p in parts):
        return None
    return parts[0], parts[1]


class LlmRefutationStore:
    """Per-repo memory of LLM-lane claims the verifier refuted or an operator dismissed.

    `<state_root>/llm-refutations/<owner>/<name>.json` holds
    `{"repo", "entries": [{repo, lane, file, line, claim, tokens, note, origin, pr, head, at}],
    "dismissals": [review ids already harvested]}`. Every entry also carries its repo and a
    read keeps only this repo's — a store file can never answer for another repo. Entries
    age out after `ttl_days`; a later round that CONFIRMS the claim forgets it. Never
    raises: an unreadable or unwritable store remembers nothing and pre-marks nothing.
    """

    def __init__(self, root: Path, *, ttl_days: int = DEFAULT_TTL_DAYS):
        self.root = Path(root)
        self.ttl_s = max(1, int(ttl_days)) * 86400

    @classmethod
    def from_cfg(cls, cfg: dict) -> "LlmRefutationStore":
        root, ttl = _root_and_ttl(cfg)
        return cls(root, ttl_days=ttl)

    def _path(self, repo: str) -> Path | None:
        parts = _repo_parts(repo)
        return self.root / "llm-refutations" / parts[0] / f"{parts[1]}.json" if parts else None

    def _read(self, repo: str) -> dict:
        path = self._path(repo)
        empty = {"entries": [], "dismissals": []}
        if path is None:
            return empty
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return empty
        if not isinstance(data, dict):
            return empty
        want = "/".join(_repo_parts(repo) or ())
        cutoff = time.time() - self.ttl_s
        entries = [
            e
            for e in data.get("entries") or []
            if isinstance(e, dict) and str(e.get("repo") or "") == want and _ts(e) >= cutoff
        ]
        dismissals = [str(x) for x in data.get("dismissals") or []][-MAX_HARVESTED_REVIEWS:]
        return {"entries": entries, "dismissals": dismissals}

    def _write(self, repo: str, data: dict) -> bool:
        path = self._path(repo)
        if path is None:
            return False
        try:
            _atomic_write(path, json.dumps({"repo": "/".join(_repo_parts(repo) or ()), **data}, indent=2))
        except OSError:
            log.exception("[pr-reviewer] LLM refutation store write failed for %s", repo)
            return False
        return True

    def entries(self, repo: str) -> list[dict]:
        return self._read(repo)["entries"]

    def _add(self, repo: str, entries: list[dict], rows: list[dict], *, origin: str, pr: int, head: str) -> int:
        want = "/".join(_repo_parts(repo) or ())
        now = time.time()
        added = 0
        for f in rows:
            if not _rememberable(f):
                continue
            entries[:] = [e for e in entries if not _same_entry(e, f)]
            entries.append(
                {
                    "repo": want,
                    "lane": finding_lane(f),
                    "file": _norm_path(str(f["file"])),
                    "line": _line(f),
                    "claim": str(f["claim"]),
                    "tokens": sorted(identifier_tokens(str(f["claim"]))),
                    "note": str(f.get("note") or "")[:300],
                    "origin": origin,
                    "pr": int(pr),
                    "head": str(head or "")[:12],
                    "at": now,
                }
            )
            added += 1
        return added

    def observe(self, repo: str, verified: list[dict], *, pr: int, head: str) -> tuple[int, int]:
        """One posted round's verified LLM findings → (remembered, forgotten). A claim the
        verifier REFUTED is remembered; one it CONFIRMED is forgotten — and never
        remembered. A row still carrying `refuted-before` was not verified this round, so
        it neither refreshes nor clears anything."""
        if _repo_parts(repo) is None:
            return 0, 0
        rows = [f for f in verified or [] if is_llm_finding(f)]
        refuted = [f for f in rows if str(f.get("verdict") or "").strip().lower() == "refuted"]
        confirmed = [f for f in rows if str(f.get("verdict") or "").strip().lower() == "confirmed"]
        if not refuted and not confirmed:
            return 0, 0
        data = self._read(repo)
        entries = data["entries"]
        before = len(entries)
        entries[:] = [e for e in entries if not any(_same_entry(e, f) for f in confirmed)]
        forgotten = before - len(entries)
        remembered = self._add(repo, entries, refuted, origin="verifier", pr=pr, head=head)
        if not (remembered or forgotten) or not self._write(repo, data):
            return 0, 0
        return remembered, forgotten

    def harvested(self, repo: str, review_id: object) -> bool:
        return str(review_id) in self._read(repo)["dismissals"]

    def record_dismissal(self, repo: str, review_id: object, findings: list[dict], *, pr: int, head: str) -> int:
        """An operator dismissed one of our reviews: its LLM findings are remembered as
        refuted (origin `dismissal`). Each review is harvested once — a claim a later round
        confirmed and forgot must not come back from the same old dismissal."""
        if _repo_parts(repo) is None:
            return 0
        data = self._read(repo)
        if str(review_id) in data["dismissals"]:
            return 0
        live = [f for f in findings or [] if str(f.get("verdict") or "").strip().lower() != "refuted"]
        added = self._add(repo, data["entries"], live, origin="dismissal", pr=pr, head=head)
        data["dismissals"] = [*data["dismissals"], str(review_id)][-MAX_HARVESTED_REVIEWS:]
        return added if self._write(repo, data) else 0

    def match(self, repo: str, finding: dict) -> dict | None:
        """The remembered refutation of this finding, or None: same file, a line within
        `SAME_CLAIM_LINES`, equal non-empty identifier tokens, and `_same_defect`-similar
        wording (≥ 0.8). No line or no identifiers ⇒ None, always."""
        if _line(finding) <= 0 or not identifier_tokens(str(finding.get("claim") or "")):
            return None
        for e in self.entries(repo):
            if _same_entry(e, finding):
                return e
        return None


def _same_entry(entry: dict, finding: dict) -> bool:
    from .verdicts import _same_defect  # the panel's one definition of "the same defect"

    return bool(identifier_tokens(str(finding.get("claim") or ""))) and _same_defect(entry, finding)


def _touched(finding: dict, ranges: dict[str, list[tuple[int, int]]]) -> bool:
    """Does the PR change the finding's line ± `TOUCH_LINES`? A file missing from the ranges,
    or present with no readable hunks, counts as touched: unknown is not untouched."""
    spans = ranges.get(_norm_path(str(finding.get("file") or "")))
    if not spans:
        return True
    line = _line(finding)
    return any(a - TOUCH_LINES <= line <= b + TOUCH_LINES for a, b in spans)


def structural_refutations_in_change(rows: list[dict], ranges: dict[str, list[tuple[int, int]]] | None) -> list[dict]:
    """The refuted structural rows worth remembering (#238): only ones on a line this PR
    changed (`ranges` = the PR's context-padded hunks). A row flagged `nearby` (#232), with no
    line, on a file with unknown hunks, or any row when `ranges` is unreadable, is dropped —
    remembering it could later hide a claim, so every unknown records nothing."""
    if not ranges:
        return []
    out = []
    for f in rows or []:
        if not isinstance(f, dict) or f.get("nearby"):
            continue
        spans = ranges.get(_norm_path(str(f.get("file") or "")))
        line = _line(f)
        if spans and line and any(a <= line <= b for a, b in spans):
            out.append(f)
    return out


def premark_check(
    finding: dict, store: LlmRefutationStore, repo: str, ranges: dict[str, list[tuple[int, int]]] | None
) -> tuple[dict | None, str]:
    """(the remembered entry, "") when this finding may stand as refuted-before, else
    (None, why not). Every edge fails closed."""
    if not is_llm_finding(finding):
        return None, "not an LLM-lane finding"
    if finding.get("nearby"):
        return None, "a nearby note is never pre-marked"
    if str(finding.get("severity") or "").strip().lower() == "blocker":
        return None, "a blocker is never pre-marked"
    if _line(finding) <= 0:
        return None, "no line"
    if not identifier_tokens(str(finding.get("claim") or "")):
        return None, "the claim names no identifier"
    if ranges is None:
        return None, "the PR's diff was unreadable"
    if _touched(finding, ranges):
        return None, "this PR changes the code there"
    hit = store.match(repo, finding)
    if hit is None:
        return None, "no remembered refutation matches it"
    return hit, ""


def remembered_for(
    store: LlmRefutationStore, repo: str, paths: list[str] | set[str], ranges: dict[str, list[tuple[int, int]]] | None
) -> list[dict]:
    """The remembered claims worth showing the synthesizer this round: on a file this PR
    changes, at a spot it does not. None ranges ⇒ nothing (no relief without a diff)."""
    if ranges is None:
        return []
    changed = {_norm_path(str(p)) for p in paths or [] if p}
    return [
        e for e in store.entries(repo) if _norm_path(str(e.get("file") or "")) in changed and not _touched(e, ranges)
    ][-MAX_RENDERED:]


def _data(text: object, limit: int) -> str:
    """Stored claims quote PR text; neutralize anything that could close the data block."""
    s = " ".join(str(text or "").split())[:limit]
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def render_refuted_before(entries: list[dict]) -> str:
    """The `<refuted_before>` data block for the synthesize/verify/report steps, or ""."""
    if not entries:
        return ""
    out = ["<refuted_before>"]
    for i, e in enumerate(entries, 1):
        when = _day(e)
        out.append(
            f'  <claim id="R{i}" location="{_data(e.get("file"), 200)}:{_int(e.get("line"))}" '
            f'refuted_on="#{_int(e.get("pr"))} @{_data(e.get("head"), 12)} {when}" '
            f'by="{_data(e.get("origin") or "verifier", 20)}">'
        )
        out.append(f"    {_data(e.get('claim'), 400)}")
        note = _data(e.get("note"), 300)
        if note:
            out.append(f"    <why>{note}</why>")
        out.append("  </claim>")
    out.append("</refuted_before>")
    return "\n".join(out)


def refuted_before_marks(*texts: str) -> list[dict]:
    """Every finding row a step marked `refuted-before`, read with the plugin's own parser —
    the host's coerces the unknown verdict away, which is right for gating and useless for
    finding the marks."""
    from .verdicts import extract_findings_json

    marks: list[dict] = []
    seen: set[tuple[str, int, str]] = set()
    for text in texts:
        raw = extract_findings_json(str(text or ""))
        try:
            rows = json.loads(raw) if raw else []
        except json.JSONDecodeError:
            rows = []
        for f in rows if isinstance(rows, list) else []:
            if isinstance(f, dict) and is_refuted_before(f) and _key(f) not in seen:
                seen.add(_key(f))
                marks.append(f)
    return marks


def _unmarked(finding: dict, why: str) -> dict:
    """A mark that did not hold: live again, unverified (no verdict) — fail closed."""
    out = {k: v for k, v in finding.items() if k != "refuted_before"}
    out["verdict"] = ""
    note = str(finding.get("note") or "").strip()
    out["note"] = f"Pre-marked refuted-before but the mark does not hold ({why}); not verified this round." + (
        f" {note}" if note and REFUTED_BEFORE not in note.lower() else ""
    )
    return out


def settle_refuted_before(
    reported: list[dict],
    marks: list[dict],
    store: LlmRefutationStore,
    repo: str,
    ranges: dict[str, list[tuple[int, int]]] | None,
    *,
    verified: list[dict] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Decide every `refuted-before` mark the panel made → (reported, relieved, rejected).

    `relieved` rows leave the findings — the claim was refuted before on this repo, at a
    spot this PR does not change. A mark that does not hold (`premark_check`) leaves its
    finding LIVE and unverified; if the report dropped such a row it is put back, so a
    model cannot launder a finding out of the round by marking it. A row the verifier gave
    a verdict of its own this round (`verified`, or on the reported row) is the verifier's,
    and no mark overrides it."""
    reported = [f for f in reported or [] if isinstance(f, dict)]
    mark_keys = {_key(m) for m in marks}
    if not mark_keys and not any(is_refuted_before(f) for f in reported):
        return reported, [], []
    verdicted = {
        _key(f)
        for f in [*(verified or []), *reported]
        if isinstance(f, dict) and str(f.get("verdict") or "").strip().lower() in _VERIFIER_VERDICTS
    }
    relieved: list[dict] = []
    rejected: list[dict] = []
    kept: list[dict] = []
    for f in reported:
        if _key(f) in verdicted or not (is_refuted_before(f) or _key(f) in mark_keys):
            kept.append(f)
            continue
        hit, why = premark_check(f, store, repo, ranges)
        if hit is None:
            rejected.append({**f, "why": why})
            kept.append(_unmarked(f, why))
        else:
            relieved.append(_relieved(f, hit))
    settled = {_key(f) for f in [*relieved, *rejected]} | verdicted
    for m in marks:  # marked upstream, absent from the report
        if _key(m) in settled or any(_key(f) == _key(m) for f in kept):
            continue
        settled.add(_key(m))
        hit, why = premark_check(m, store, repo, ranges)
        if hit is None:
            rejected.append({**m, "why": why})
            if not any(_same_entry(f, m) for f in kept):
                kept.append(_unmarked(m, why))
        else:
            relieved.append(_relieved(m, hit))
    return kept, relieved, rejected


def _relieved(finding: dict, hit: dict) -> dict:
    when = _day(hit)
    return {
        "file": _norm_path(str(finding.get("file") or "")),
        "line": _line(finding),
        "severity": str(finding.get("severity") or ""),
        "lane": finding_lane(finding),
        "claim": str(finding.get("claim") or ""),
        "refuted_before": f"#{hit.get('pr')} @{hit.get('head')} {when}",
        "origin": str(hit.get("origin") or "verifier"),
        "note": str(hit.get("note") or ""),
    }


def render_refuted_before_note(relieved: list[dict]) -> str:
    """The body footnote: what the panel did not re-verify, and why — never silent."""
    if not relieved:
        return ""
    lines = [
        "",
        "",
        f"**Refuted before ({len(relieved)}, not re-verified):** this repo already refuted "
        "these claims at a spot this PR does not change (#207). A change there would make "
        "them live again.",
    ]
    for r in relieved[:20]:
        why = f" — {_data(r.get('note'), 200)}" if r.get("note") else ""
        lines.append(
            f"- `{r['file']}:{r['line']}` ({r['lane']}): {_data(r.get('claim'), 200)} "
            f"— {r.get('origin')} on {r['refuted_before']}{why}"
        )
    return "\n".join(lines)

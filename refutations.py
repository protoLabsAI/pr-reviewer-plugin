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
import time
from pathlib import Path

log = logging.getLogger("protoagent.plugins.pr_reviewer")

DEFAULT_TTL_DAYS = 14
SAME_CLAIM_RATIO = 0.8  # SequenceMatcher on normalised claims — above what boilerplate alone reaches
SAME_CLAIM_LINES = 25  # a remembered refutation applies at (about) the line it was refuted at


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _norm_path(path: str) -> str:
    return str(path or "").strip().lstrip("./")


def same_claim(a: str, b: str) -> bool:
    """Near-identical wording that names the same things: two claims about different
    sites share their boilerplate and differ exactly in an identifier (`list_users()` vs
    `delete_user()`), and must never match however close the wording."""
    from .verdicts import identifier_tokens  # lazy — verdicts is the plugin's core module

    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if identifier_tokens(na) != identifier_tokens(nb):
        return False
    return na == nb or difflib.SequenceMatcher(None, na, nb).ratio() >= SAME_CLAIM_RATIO


class RefutationStore:
    def __init__(self, root: Path, *, ttl_days: int = DEFAULT_TTL_DAYS):
        self.root = Path(root)
        self.ttl_s = max(1, int(ttl_days)) * 86400

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
        return [e for e in data if isinstance(e, dict) and float(e.get("at") or 0) >= cutoff]

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
            path = self._path(repo)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entries, indent=2))
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
        when = time.strftime("%Y-%m-%d", time.gmtime(float(hit.get("at") or 0)))
        f["verdict"] = "refuted"
        f["refuted_before"] = f"#{hit.get('pr')} @{hit.get('head')} {when}"
        note = str(hit.get("note") or "").strip()
        f["note"] = (
            f"Refuted before on #{hit.get('pr')} @{hit.get('head')} ({when}); this PR does not change the file there."
            + (f" Verifier then: {note}" if note else "")
        )
        marked += 1
    return marked

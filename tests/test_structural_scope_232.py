"""Structural findings are scoped to the change (#232 ask 7).

protoAgent#4017 r3: protoPatch raised seven findings in `operator_api/config_routes.py`, whose
only hunk in this PR was a comment — and they made up the verdict. File-level confinement keeps
anything on a touched file; this is its line-level step for the structural lane: a protoPatch
finding outside the PR's changed lines (and, in Python, outside the functions those lines sit
in) is a non-gating "nearby" note. Every unknown keeps today's behaviour — the finding gates.
"""

from __future__ import annotations

import base64
import json

from pr_reviewer.protopatch import map_finding
from pr_reviewer.rounds import delta_ranges, render_prior_requests, unaccounted_priors, unexplained_clearance
from pr_reviewer.verdicts import extract_findings_json, mark_nearby, nearby_structural

from tests.test_dispatch import HEAD, OLD_HEAD, RoutedGH, capturing_runner, facts, make

FILE = "operator_api/config_routes.py"

# A comment at line 10 (module level), and `explain` spanning lines 100–130.
SOURCE_LINES = [f"# line {n}" for n in range(1, 100)]
SOURCE_LINES[9] = "# the one comment this PR edits"
SOURCE_LINES += (
    ["def explain(cfg):"] + [f"    step_{n} = cfg  # line {n}" for n in range(101, 130)] + ["    return cfg"]
)
SOURCE_LINES += [f"# line {n}" for n in range(131, 141)]
SOURCE = "\n".join(SOURCE_LINES) + "\n"


def _patch(start: int, count: int = 1) -> str:
    return f"@@ -{start},{count} +{start},{count} @@\n-old\n+new\n"


def _structural(line, file=FILE, severity="major", **extra):
    return {"file": file, "line": line, "severity": severity, "claim": "crash", "source": "protopatch", **extra}


# ── the pure rule ─────────────────────────────────────────────────────────────


def test_a_structural_finding_in_an_untouched_function_is_nearby():
    ranges = delta_ranges([{"filename": FILE, "patch": _patch(10)}])  # only the comment changed
    assert nearby_structural([_structural(120)], ranges, {FILE: SOURCE}) == [0]


def test_a_structural_finding_inside_a_function_the_pr_changed_still_gates():
    # The hunk is line 102; the finding at 125 is 20+ lines away, beyond any hunk padding —
    # but in the same function, so the change may well be its cause.
    ranges = delta_ranges([{"filename": FILE, "patch": _patch(102)}])
    assert nearby_structural([_structural(125)], ranges, {FILE: SOURCE}) == []


def test_a_finding_on_a_changed_line_gates():
    ranges = delta_ranges([{"filename": FILE, "patch": _patch(120)}])
    assert nearby_structural([_structural(120)], ranges, {FILE: SOURCE}) == []


def test_llm_lane_findings_are_never_scoped():
    ranges = delta_ranges([{"filename": FILE, "patch": _patch(10)}])
    llm = {"file": FILE, "line": 120, "severity": "major", "claim": "crash"}  # no source: an LLM finder
    assert nearby_structural([llm], ranges, {FILE: SOURCE}) == []


def test_every_unknown_fails_closed():
    ranges = delta_ranges([{"filename": FILE, "patch": _patch(10)}, {"filename": "big.py", "patch": ""}])
    assert nearby_structural([_structural(0)], ranges, {FILE: SOURCE}) == []  # no line
    assert nearby_structural([_structural(None)], ranges, {FILE: SOURCE}) == []
    assert nearby_structural([_structural(120)], None, {FILE: SOURCE}) == []  # diff unreadable
    assert nearby_structural([_structural(120, file="big.py")], ranges, {"big.py": SOURCE}) == []  # no patch
    assert nearby_structural([_structural(120, file="other.py")], ranges, {}) == []  # not in the read
    assert nearby_structural([_structural(120)], ranges, {FILE: None}) == []  # head unreadable
    assert nearby_structural([_structural(120)], ranges, {}) == []  # head never fetched
    assert nearby_structural([_structural(120)], ranges, {FILE: "def broken(:\n" * 200}) == []  # unparseable
    assert nearby_structural([_structural(999)], ranges, {FILE: SOURCE}) == []  # past end of file


def test_non_python_files_scope_by_hunk():
    ranges = delta_ranges([{"filename": "lib/cache.ts", "patch": _patch(10)}])
    far, near = _structural(120, file="lib/cache.ts"), _structural(13, file="lib/cache.ts")
    assert nearby_structural([far, near], ranges, {}) == [0]


def test_mark_nearby_returns_a_new_flagged_dict():
    f = _structural(120)
    out = mark_nearby(f)
    assert out["nearby"] is True and "nearby" in out["note"] and "nearby" not in f
    assert mark_nearby(out)["note"] == out["note"]  # idempotent


# ── the ledger: a nearby note never gated, so it is never a debt ──────────────


def test_a_nearby_major_is_not_carried_as_an_unaccounted_prior():
    nearby = mark_nearby({**_structural(120), "verdict": "confirmed"})
    history = [{"head": OLD_HEAD, "verdict": "PASS", "findings": [nearby]}]
    dispositions = [{"prior": "x.py:1", "disposition": "fixed"}]
    assert unaccounted_priors(history, dispositions, ranges={}) == []
    # …while the same major without the flag still is.
    plain = {**_structural(120), "verdict": "confirmed"}
    history = [{"head": OLD_HEAD, "verdict": "FAIL", "findings": [plain]}]
    assert len(unaccounted_priors(history, dispositions, ranges={})) == 1


def test_a_nearby_major_does_not_hold_a_clean_pass():
    nearby = mark_nearby({**_structural(120), "verdict": "confirmed"})
    assert unexplained_clearance([{"head": OLD_HEAD, "verdict": "PASS", "findings": [nearby]}], "PASS", []) is None


def test_a_nearby_note_is_not_listed_as_a_prior_request():
    nearby = mark_nearby({**_structural(120), "claim": "pre-existing crash"})
    real = {"file": "x.py", "line": 3, "severity": "minor", "claim": "real ask"}
    block = render_prior_requests([{"head": OLD_HEAD, "verdict": "WARN", "findings": [nearby, real]}])
    assert "real ask" in block and "pre-existing crash" not in block


# ── protoPatch picks the anchor in the PR's code ──────────────────────────────


def test_a_cross_location_finding_is_anchored_where_the_pr_changed_code():
    record = {
        "status": "open",
        "title": "caller breaks on the new return type",
        "severity": "high",
        "evidence": [
            {"path": "app/caller.py", "startLine": 400, "endLine": 402, "quote": "x = load()[0]"},
            {"path": "app/loader.py", "startLine": 12, "endLine": 14, "quote": "return None"},
        ],
    }
    assert map_finding(record)["file"] == "app/caller.py"  # no ranges: first location, as before
    mapped = map_finding(record, {"app/loader.py": [(13, 13)]})
    assert (mapped["file"], mapped["line"]) == ("app/loader.py", 12)
    assert "return None" in mapped["evidence"]  # the quote travels with its anchor (grounding)
    assert map_finding(record, {"app/other.py": [(1, 5)]})["file"] == "app/caller.py"


# ── end to end through the dispatcher ─────────────────────────────────────────


def _report(*findings: dict) -> str:
    return "<!-- brief -->\nBrief.\n<!-- /brief -->\n\n```json\n" + json.dumps(list(findings)) + "\n```"


class ScopedGH(RoutedGH):
    """Serves the PR's patches and the file at head, so scoping (and grounding) can read."""

    def __init__(self, *, patch: str | None, source: str | None = SOURCE, **kw):
        super().__init__(files=f"{FILE}\n", **kw)
        self.patch, self.source = patch, source

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/contents/" in joined:
            if self.source is None:
                return 1, "", "404"
            return 0, "base64\x00" + base64.b64encode(self.source.encode()).decode(), ""
        if "/files" in joined and "filename: .filename, patch: .patch" in joined:
            if self.patch is None:
                return 1, "", "502"
            return 0, json.dumps({"filename": FILE, "patch": self.patch}), ""
        if "/files" in joined and "f: .filename, p: .patch" in joined:
            return 0, json.dumps({"f": FILE, "p": self.patch or ""}), ""
        return await super().__call__(args, timeout=timeout)


STRUCTURAL_MAJOR = {
    "file": FILE,
    "line": 120,
    "severity": "major",
    "category": "correctness",
    "claim": "explain() crashes on a None config.",
    "evidence": "protoPatch traced the call path.",
    "source": "protopatch",
    "verdict": "confirmed",
}


async def test_protoagent_4017_r3_a_comment_only_hunk_no_longer_fails_the_pr(tmp_path):
    gh = ScopedGH(patch=_patch(10), pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(STRUCTURAL_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    body = gh.reviews_posted[0]["body"]
    assert "nearby notes" in body and f"{FILE}:120" in body
    recorded = json.loads(extract_findings_json(body))
    assert recorded[0]["nearby"] is True  # the record carries it, so the next round won't carry it
    assert "nearby, not gating" in body
    events = {e["event"]: e for e in d.telemetry.read_all()}
    assert events["nearby"]["findings"] == [{"file": FILE, "line": 120, "severity": "major"}]
    assert events["reviewed"]["nearby"] == 1 and events["reviewed"]["findings"] == 0


async def test_a_structural_finding_in_a_changed_function_still_fails(tmp_path):
    gh = ScopedGH(patch=_patch(102), pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(STRUCTURAL_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_an_llm_finding_in_untouched_code_still_fails(tmp_path):
    gh = ScopedGH(patch=_patch(10), pr_facts=facts(), reviews=[])
    llm = {k: v for k, v in STRUCTURAL_MAJOR.items() if k != "source"}
    runner, _ = capturing_runner(_report(llm))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_scoping_stands_down_when_the_patches_cannot_be_read(tmp_path):
    gh = ScopedGH(patch=None, pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(STRUCTURAL_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_scoping_stands_down_when_the_python_head_cannot_be_read(tmp_path):
    gh = ScopedGH(patch=_patch(10), source=None, pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(STRUCTURAL_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_scoping_reads_the_head_itself_when_grounding_is_off(tmp_path):
    gh = ScopedGH(patch=_patch(10), pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(STRUCTURAL_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_grounding": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"


async def test_a_scoping_crash_keeps_every_finding_gating(tmp_path, monkeypatch):
    gh = ScopedGH(patch=_patch(10), pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(STRUCTURAL_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)

    async def boom(*_a, **_k):
        raise RuntimeError("scoping bug")

    monkeypatch.setattr(d, "_scope_structural", boom)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"

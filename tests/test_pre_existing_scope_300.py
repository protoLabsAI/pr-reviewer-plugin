"""Findings on unchanged lines that reproduce at the merge-base are pre-existing (#300).

protoAgent#4581 sat blocked for hours on `src/graph/config.py:418`, a line the PR never
changed, after the panel itself refuted it for that diff: rule #38 kept carrying the
verifier-confirmed finding, and the operator's dismissal did not stick because the harvest
remembered LLM-lane findings only. The gap: no rule asked whether the PR OWNS the defect.

The fix, every lane: a blocking finding on a line OUTSIDE the PR's changed hunks is classified
against the merge-base. Reproduces there (same line byte-identical, no changed file referenced)
⇒ `scope: pre-existing` — excluded from the verdict, rendered apart, never carried. Does not
reproduce, or anything unknown ⇒ it keeps gating (the load-bearing guard: real regressions cite
unchanged lines). Carried priors re-classify every round — the one documented exception to #38.
And the dismissal harvest now records structural findings too.
"""

from __future__ import annotations

import base64
import json

from pr_reviewer.refutations import LlmRefutationStore, RefutationStore, premark_refuted
from pr_reviewer.rounds import (
    OUTCOME_NOT_HONOURED,
    carried_debt,
    delta_ranges,
    gating_debt,
    unaccounted_priors,
)
from pr_reviewer.verdicts import (
    FAIL,
    IN_DIFF_SCOPE,
    PASS,
    PRE_EXISTING_SCOPE,
    extract_findings_json,
    is_pre_existing,
    line_in_diff,
    pre_existing_dedup_key,
    render_pre_existing_section,
    scope_for,
    suggested_issue_title,
    verdict_for,
)

from tests.test_dispatch import HEAD, RoutedGH, capturing_runner, facts, make

FILE = "pkg/config_routes.py"
BASE_SHA = "c" * 40

# `explain` spans 100–131; a module-level comment the PR edits sits at line 10.
_LINES = [f"# line {n}" for n in range(1, 100)]
_LINES[9] = "# the one comment this PR edits"
_LINES += ["def explain(cfg):"] + [f"    step_{n} = cfg  # line {n}" for n in range(101, 130)] + ["    return cfg"]
_LINES += [f"# line {n}" for n in range(131, 141)]
SOURCE = "\n".join(_LINES) + "\n"
# The same file at the merge-base with the cited line (120) rewritten — the defect does NOT
# reproduce there, so a finding on it is the PR's to own.
_BASE_LINES = list(_LINES)
_BASE_LINES[119] = "    step_120 = other  # a different line at the merge-base"
SOURCE_BASE_DIFFERS = "\n".join(_BASE_LINES) + "\n"


def _patch(start: int, count: int = 1) -> str:
    return f"@@ -{start},{count} +{start},{count} @@\n-old\n+new\n"


def _ranges(start: int = 10) -> dict:
    return delta_ranges([{"filename": FILE, "patch": _patch(start)}])


def _finding(line=120, file=FILE, severity="major", source=None, **extra):
    f = {"file": file, "line": line, "severity": severity, "claim": "explain() crashes on a None config.", **extra}
    if source is not None:
        f["source"] = source
    return f


# ── the pure classifier: scope_for ─────────────────────────────────────────────


def test_a_finding_on_a_changed_line_is_in_diff():
    assert scope_for(_finding(line=10), _ranges(10), {FILE: SOURCE}, {FILE: SOURCE}, {FILE}) == IN_DIFF_SCOPE


def test_a_finding_in_a_function_the_pr_changed_is_in_diff():
    # The hunk is at line 102 (inside `explain`); a finding at 120 in the same function keeps
    # gating — the change may well be its cause, so it is not pre-existing.
    assert scope_for(_finding(line=120), _ranges(102), {FILE: SOURCE}, {FILE: SOURCE}, {FILE}) == IN_DIFF_SCOPE


def test_an_unchanged_line_that_reproduces_at_base_is_pre_existing():
    assert scope_for(_finding(line=120), _ranges(10), {FILE: SOURCE}, {FILE: SOURCE}, {FILE}) == PRE_EXISTING_SCOPE


def test_an_unchanged_line_that_does_not_reproduce_at_base_keeps_gating():
    # Byte-different at the merge-base ⇒ the PR owns it (a cross-file/removed-behaviour break).
    assert (
        scope_for(_finding(line=120), _ranges(10), {FILE: SOURCE}, {FILE: SOURCE_BASE_DIFFERS}, {FILE}) == IN_DIFF_SCOPE
    )


def test_a_claim_that_names_a_changed_file_keeps_gating():
    # Reproduces byte-wise, but the claim leans on a file the PR changed — fail closed.
    f = _finding(line=120, claim="explain() breaks now that loader.py returns None")
    assert scope_for(f, _ranges(10), {FILE: SOURCE}, {FILE: SOURCE}, {FILE, "loader.py"}) == IN_DIFF_SCOPE


def test_every_unknown_fails_closed_to_in_diff():
    f = _finding(line=120)
    assert scope_for(f, None, {FILE: SOURCE}, {FILE: SOURCE}, {FILE}) == IN_DIFF_SCOPE  # diff unreadable
    assert scope_for(f, _ranges(10), {FILE: None}, {FILE: SOURCE}, {FILE}) == IN_DIFF_SCOPE  # head unreadable
    assert scope_for(f, _ranges(10), {FILE: SOURCE}, {FILE: None}, {FILE}) == IN_DIFF_SCOPE  # base unreadable
    assert scope_for(f, _ranges(10), {FILE: SOURCE}, {}, {FILE}) == IN_DIFF_SCOPE  # base never fetched
    assert (
        scope_for(_finding(line=9999), _ranges(10), {FILE: SOURCE}, {FILE: SOURCE}, {FILE}) == IN_DIFF_SCOPE
    )  # past EOF
    blank = {FILE: "\n\n\n   \n" + SOURCE}  # line 4 is whitespace-only
    assert (
        scope_for(_finding(line=4), _ranges(10), blank, blank, {FILE}) == IN_DIFF_SCOPE
    )  # a blank line proves nothing


def test_line_in_diff_is_none_when_the_diff_is_unreadable():
    assert line_in_diff(_finding(line=120), None, {FILE: SOURCE}) is None


# ── the pure mapping: verdict_for excludes pre-existing ─────────────────────────


def test_verdict_for_excludes_a_pre_existing_blocker():
    assert verdict_for([{**_finding(severity="blocker", verdict="confirmed"), "scope": PRE_EXISTING_SCOPE}]) == PASS
    assert verdict_for([{**_finding(severity="blocker", verdict="confirmed"), "scope": IN_DIFF_SCOPE}]) == FAIL
    # Mixed: one in-diff major still fails the round; the pre-existing one is simply ignored.
    mixed = [
        {**_finding(severity="major", verdict="confirmed"), "scope": PRE_EXISTING_SCOPE},
        {**_finding(line=10, severity="major", verdict="confirmed"), "scope": IN_DIFF_SCOPE},
    ]
    assert verdict_for(mixed) == FAIL


def test_is_pre_existing_reads_only_the_scope_field():
    assert is_pre_existing({"scope": "pre-existing"}) is True
    assert is_pre_existing({"scope": "in-diff"}) is False
    assert is_pre_existing({}) is False  # absent ⇒ in-diff by default


# ── tracked, not dropped: dedup key + suggested issue title (r6) ────────────────


def test_pre_existing_rows_carry_a_dedup_key_and_a_suggested_title():
    f = {**_finding(line=120), "scope": PRE_EXISTING_SCOPE}
    assert pre_existing_dedup_key(f) == "pkg/config_routes.py::explain() crashes on a none config."
    assert suggested_issue_title(f).startswith("Pre-existing: explain() crashes on a None config.")
    section = render_pre_existing_section([f])
    assert "Pre-existing, not blocking" in section
    assert "dedup key:" in section and pre_existing_dedup_key(f) in section
    assert "suggested issue:" in section
    assert render_pre_existing_section([]) == ""  # nothing to render when there are none


# ── carried priors re-classify every round: the #38 exception (r4) ──────────────


def _confirmed_prior(**extra):
    return {"file": "a.py", "line": 3, "severity": "major", "claim": "bug", "verdict": "confirmed", **extra}


_REFUTE_UNCHANGED = [{"prior": "a.py:3", "disposition": "refuted", "why": "this line is not modified by the diff"}]


def test_a_carried_in_diff_prior_refuted_on_unchanged_code_is_still_not_honoured():
    # #38 unchanged: a verifier-confirmed prior the report refutes on code that did not move
    # keeps gating — one model draw against another.
    history = [{"head": "b" * 40, "verdict": "FAIL", "findings": [_confirmed_prior()]}]
    owed = unaccounted_priors(history, _REFUTE_UNCHANGED, ranges={})
    assert [o.get("owed") for o in owed] == [OUTCOME_NOT_HONOURED]


def test_a_carried_prior_re_classified_pre_existing_clears():
    # The one documented exception to #38: the SAME prior, now stamped pre-existing (proven at
    # the merge-base), clears — not carried, not owed.
    history = [{"head": "b" * 40, "verdict": "FAIL", "findings": [_confirmed_prior(scope=PRE_EXISTING_SCOPE)]}]
    assert unaccounted_priors(history, _REFUTE_UNCHANGED, ranges={}) == []


def test_a_pre_existing_carry_is_neither_gating_debt_nor_carried_debt():
    assert gating_debt([_confirmed_prior()]) != []  # an in-diff confirmed carry gates
    assert gating_debt([_confirmed_prior(scope=PRE_EXISTING_SCOPE)]) == []
    carried = _confirmed_prior(carried=True)
    assert carried_debt({"findings": [carried]}) == [carried]
    assert carried_debt({"findings": [{**carried, "scope": PRE_EXISTING_SCOPE}]}) == []


# ── operator dismissal covers the structural lane (r5) ──────────────────────────

_STRUCTURAL = {
    "file": "src/graph/config.py",
    "line": 418,
    "severity": "major",
    "claim": 'route.base_url or "" in _migrated_providers can panic',
    "source": "protopatch",
    "verdict": "confirmed",
}


def test_a_dismissed_structural_finding_is_remembered_and_pre_marked(tmp_path):
    store = RefutationStore(tmp_path, ttl_days=14)
    assert store.record_dismissal("o/r", [_STRUCTURAL], pr=1, head="abc123") == 1
    hit = store.match("o/r", _STRUCTURAL["file"], _STRUCTURAL["claim"], 418)
    assert hit is not None and hit.get("origin") == "dismissal"
    # Next round: the structural pass re-raises it on a line the PR does not change → pre-marked
    # refuted, so it never gates again (protoAgent#4581's exit without an admin merge).
    findings = [dict(_STRUCTURAL)]
    assert premark_refuted(findings, store, "o/r", {_STRUCTURAL["file"]: [(10, 20)]}) == 1
    assert findings[0]["verdict"] == "refuted"


def test_a_dismissed_structural_finding_is_live_again_where_the_pr_changes_it(tmp_path):
    # Fail-closed like #194: a dismissal does not silence the claim on a PR that DOES touch it.
    store = RefutationStore(tmp_path, ttl_days=14)
    store.record_dismissal("o/r", [_STRUCTURAL], pr=1, head="abc123")
    findings = [dict(_STRUCTURAL)]
    assert premark_refuted(findings, store, "o/r", {_STRUCTURAL["file"]: [(417, 419)]}) == 0
    assert findings[0]["verdict"] == "confirmed"


def test_the_llm_store_never_remembers_a_structural_finding(tmp_path):
    # The LLM-lane store is source-less by name (#207); a structural finding routed to it was
    # dropped silently — which is exactly why #300 routes it to the structural store instead.
    llm = LlmRefutationStore(tmp_path)
    assert llm.record_dismissal("o/r", 99, [_STRUCTURAL], pr=1, head="abc123") == 0


# ── end to end through the dispatcher ───────────────────────────────────────────


def _report(*findings: dict) -> str:
    return "<!-- brief -->\nBrief.\n<!-- /brief -->\n\n```json\n" + json.dumps(list(findings)) + "\n```"


class PreExistingGH(RoutedGH):
    """Serves the PR's own patch, the cited file at head and at the merge-base, and the
    merge-base SHA — everything #300 scoping reads. `base_sha=""` makes the merge-base
    unreadable (the fail-closed case)."""

    def __init__(self, *, patch, head_src=SOURCE, base_src=SOURCE, base_sha=BASE_SHA, **kw):
        super().__init__(files=f"{FILE}\n", **kw)
        self.patch, self.head_src, self.base_src, self.base_sha = patch, head_src, base_src, base_sha

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "merge_base_commit.sha" in joined:  # _merge_base_sha
            return (0, self.base_sha, "") if self.base_sha else (1, "", "404")
        if "/contents/" in joined:
            ref = args[1].split("ref=", 1)[-1]
            src = self.base_src if ref == self.base_sha else self.head_src
            return (
                (0, "base64\x00" + base64.b64encode(src.encode()).decode(), "") if src is not None else (1, "", "404")
            )
        if "/files" in joined and "filename: .filename, patch: .patch" in joined:
            return (0, json.dumps({"filename": FILE, "patch": self.patch}), "") if self.patch else (1, "", "502")
        if "/files" in joined and "f: .filename, p: .patch" in joined:
            return 0, json.dumps({"f": FILE, "p": self.patch or ""}), ""
        return await super().__call__(args, timeout=timeout)


# An LLM-lane major (no `source`) on an unchanged line, with prose evidence grounding leaves
# confirmed — the same finding that still FAILs in test_structural_scope_232 when the merge-base
# cannot be read.
LLM_MAJOR = {
    "file": FILE,
    "line": 120,
    "severity": "major",
    "category": "correctness",
    "claim": "explain() crashes on a None config.",
    "evidence": "the call path dereferences cfg.",
    "verdict": "confirmed",
}


async def test_an_unchanged_line_reproducing_at_base_does_not_fail_and_is_listed(tmp_path):
    gh = PreExistingGH(patch=_patch(10), pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    body = gh.reviews_posted[0]["body"]
    assert "Pre-existing, not blocking" in body
    assert "dedup key:" in body and "suggested issue:" in body
    recorded = json.loads(extract_findings_json(body))
    assert recorded and recorded[0]["scope"] == PRE_EXISTING_SCOPE
    events = {e["event"]: e for e in d.telemetry.read_all()}
    assert events["pre_existing"]["findings"][0]["file"] == FILE


async def test_an_unchanged_line_that_does_not_reproduce_still_fails(tmp_path):
    gh = PreExistingGH(patch=_patch(10), base_src=SOURCE_BASE_DIFFERS, pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_an_unreadable_merge_base_keeps_the_finding_blocking(tmp_path):
    gh = PreExistingGH(patch=_patch(10), base_sha="", pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"

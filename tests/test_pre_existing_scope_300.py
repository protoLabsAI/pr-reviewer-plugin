"""Findings on unchanged lines that reproduce at the merge-base are pre-existing (#300).

protoAgent#4581 sat blocked for hours on `src/graph/config.py:418`, a line the PR never
changed, after the panel itself refuted it for that diff: rule #38 kept carrying the
verifier-confirmed finding, and the operator's dismissal did not stick because the harvest
remembered LLM-lane findings only. The gap: no rule asked whether the PR OWNS the defect.

The fix, every lane: a blocking finding whose cited line maps to the merge-base BYTE-IDENTICALLY
(a deterministic line-for-line mapping through the PR's hunks — never a text search) is handed,
with the merge-base text, to a verifier that answers `exists_at_base: yes|no|unsure`. Only `yes`
⇒ `scope: pre-existing` — excluded from the verdict, rendered apart, never carried. `no`, `unsure`,
a mapping miss, an unreadable merge-base, a claim that names a changed file, or no verifier at all
⇒ it keeps gating (the load-bearing guard: real regressions cite unchanged lines). Carried priors
re-classify every round — re-anchored to this head first — the one documented exception to #38.
And the dismissal harvest now records structural findings too.

Every fixture is built from a REAL base file + head file + the unified diff `difflib` produces
from them — no hand-made hunk/line combinations a real diff could not emit.
"""

from __future__ import annotations

import base64
import difflib
import json

from pr_reviewer.refutations import LlmRefutationStore, RefutationStore, premark_refuted
from pr_reviewer.rounds import (
    OUTCOME_NOT_HONOURED,
    blocking_priors,
    carried_debt,
    delta_ranges,
    encode_disposition_record,
    gating_debt,
    panel_rounds,
    superseded_fails,
    unaccounted_priors,
)
from pr_reviewer.verdicts import (
    FAIL,
    IN_DIFF_SCOPE,
    PASS,
    PRE_EXISTING_SCOPE,
    base_reproduction_precondition,
    extract_findings_json,
    is_pre_existing,
    map_base_line_to_head,
    map_head_line_to_base,
    parse_hunks,
    parse_verdict_marker,
    pre_existing_dedup_key,
    render_pre_existing_section,
    render_verdict_body,
    suggested_issue_title,
    verdict_for,
)

from tests.test_dispatch import HEAD, RoutedGH, capturing_runner, facts, make

FILE = "pkg/routes.py"
BASE_SHA = "c" * 40
PRIOR_HEAD = "b" * 40

# ── a real file, and the head the PR leaves behind ──────────────────────────────
# The pre-existing defect is at line 10 (`name = cfg.name`, which assumes cfg is not None); it
# is NOT what the PR edits. The PR edits line 3 (a module default), far enough away that no real
# diff hunk reaches line 10.
_BASE = [
    "import os",  # 1
    "",  # 2
    "DEFAULT_TIMEOUT = 30",  # 3  <- the PR edits this, unrelated to the defect
    "",  # 4
    "",  # 5
    "",  # 6
    "",  # 7
    "def explain(cfg):",  # 8
    "    # describe cfg",  # 9
    "    name = cfg.name",  # 10 <- the pre-existing defect (cfg may be None)
    "    return name",  # 11
]
DEFECT_LINE = 10
BASE = "\n".join(_BASE) + "\n"

_HEAD = list(_BASE)
_HEAD[2] = "DEFAULT_TIMEOUT = 60"  # the one line the PR changes
HEAD_SRC = "\n".join(_HEAD) + "\n"


def unified(base: str, head: str, path: str = FILE) -> str:
    """The unified diff a real `git`/GitHub would produce for base→head — the only hunks these
    tests ever feed the classifier (the card forbids hand-made hunk/line combinations)."""
    return "\n".join(
        difflib.unified_diff(
            base.splitlines(), head.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""
        )
    )


PR_PATCH = unified(BASE, HEAD_SRC)


def _finding(line=DEFECT_LINE, file=FILE, severity="major", source=None, **extra):
    f = {"file": file, "line": line, "severity": severity, "claim": "explain() dereferences a None config.", **extra}
    if source is not None:
        f["source"] = source
    return f


def _verifier(answer="yes", *, boom=False):
    async def verify(finding, base_sources):
        if boom:
            raise RuntimeError("verifier gateway down")
        return answer

    return verify


# ── the pure mapping: head↔base line, line-for-line through the hunks ────────────


def test_parse_hunks_reads_both_sides_of_a_real_diff():
    hunks = parse_hunks([{"filename": FILE, "patch": PR_PATCH}])
    assert FILE in hunks and hunks[FILE]  # at least one hunk around line 3
    # an in-place one-line edit adds no net lines, so a line below it maps to itself
    assert map_head_line_to_base(DEFECT_LINE, hunks[FILE]) == DEFECT_LINE


def test_a_file_the_pr_never_touched_maps_identically():
    # The reviewer's #301 finding: an out-of-diff FILE must still be mappable. No hunks ⇒ identity.
    assert map_head_line_to_base(418, None) == 418
    assert map_head_line_to_base(418, []) == 418
    assert map_base_line_to_head(418, []) == 418


def test_a_line_inside_a_changed_hunk_is_unmappable():
    hunks = parse_hunks([{"filename": FILE, "patch": PR_PATCH}])
    assert map_head_line_to_base(3, hunks[FILE]) is None  # line 3 IS the change


def test_an_insertion_above_shifts_a_later_line_by_the_net_delta():
    head_after_push = "\n".join(["# new 1", "# new 2", "# new 3", *_BASE]) + "\n"
    hunks = parse_hunks([{"filename": FILE, "patch": unified(BASE, head_after_push)}])[FILE]
    # the defect, at base line 10, is now three lines lower on the pushed head
    assert map_base_line_to_head(DEFECT_LINE, hunks) == DEFECT_LINE + 3
    # and the inverse agrees
    assert map_head_line_to_base(DEFECT_LINE + 3, hunks) == DEFECT_LINE


def test_a_deletion_above_shifts_a_later_line_back():
    head_after_push = "\n".join(_BASE[:1] + _BASE[2:]) + "\n"  # drop the blank line 2
    hunks = parse_hunks([{"filename": FILE, "patch": unified(BASE, head_after_push)}])[FILE]
    assert map_base_line_to_head(DEFECT_LINE, hunks) == DEFECT_LINE - 1


def test_non_positive_or_non_int_lines_do_not_map():
    assert map_head_line_to_base(0, []) is None
    assert map_head_line_to_base(None, []) is None
    assert map_head_line_to_base(True, []) is None


# ── the deterministic precondition: who the verifier is even worth asking ────────


def test_an_unchanged_byte_identical_line_is_eligible():
    assert base_reproduction_precondition(
        _finding(), parse_hunks([{"filename": FILE, "patch": PR_PATCH}])[FILE], {FILE: HEAD_SRC}, {FILE: BASE}, {FILE}
    )


def test_a_changed_line_is_not_eligible():
    # A finding literally on the PR's edited line maps to None ⇒ ineligible ⇒ IN-DIFF.
    assert not base_reproduction_precondition(
        _finding(line=3),
        parse_hunks([{"filename": FILE, "patch": PR_PATCH}])[FILE],
        {FILE: HEAD_SRC},
        {FILE: BASE},
        {FILE},
    )


def test_a_line_whose_base_text_differs_is_not_eligible():
    # The head and base genuinely differ at the defect line — so a real diff WOULD hunk it, but
    # even fed the identity mapping the byte-compare refuses: never `x in base_text.splitlines()`.
    base_differs = "\n".join(_HEAD[:9] + ["    name = other.name  # different at base"] + _HEAD[10:]) + "\n"
    assert not base_reproduction_precondition(_finding(), None, {FILE: HEAD_SRC}, {FILE: base_differs}, set())


def test_a_claim_that_names_a_changed_file_is_not_eligible():
    f = _finding(claim="explain() breaks now that routes.py rewrote the loader")
    assert not base_reproduction_precondition(f, None, {FILE: HEAD_SRC}, {FILE: HEAD_SRC}, {FILE})


def test_every_unknown_fails_closed_to_ineligible():
    hunks = parse_hunks([{"filename": FILE, "patch": PR_PATCH}])[FILE]
    assert not base_reproduction_precondition(_finding(), hunks, {FILE: None}, {FILE: BASE}, {FILE})  # head unreadable
    assert not base_reproduction_precondition(
        _finding(), hunks, {FILE: HEAD_SRC}, {FILE: None}, {FILE}
    )  # base unreadable
    assert not base_reproduction_precondition(_finding(), hunks, {FILE: HEAD_SRC}, {}, {FILE})  # base never fetched
    assert not base_reproduction_precondition(_finding(line=9999), hunks, {FILE: HEAD_SRC}, {FILE: BASE}, {FILE})  # EOF
    blank = {FILE: "\n\n\n   \n" + HEAD_SRC}
    assert not base_reproduction_precondition(
        _finding(line=4), None, blank, blank, set()
    )  # a blank line proves nothing


# ── the pure mapping: verdict_for excludes pre-existing ─────────────────────────


def test_verdict_for_excludes_a_pre_existing_blocker():
    assert verdict_for([{**_finding(severity="blocker", verdict="confirmed"), "scope": PRE_EXISTING_SCOPE}]) == PASS
    assert verdict_for([{**_finding(severity="blocker", verdict="confirmed"), "scope": IN_DIFF_SCOPE}]) == FAIL
    mixed = [
        {**_finding(severity="major", verdict="confirmed"), "scope": PRE_EXISTING_SCOPE},
        {**_finding(line=3, severity="major", verdict="confirmed"), "scope": IN_DIFF_SCOPE},
    ]
    assert verdict_for(mixed) == FAIL  # one in-diff major still fails the round


def test_is_pre_existing_reads_only_the_scope_field():
    assert is_pre_existing({"scope": "pre-existing"}) is True
    assert is_pre_existing({"scope": "in-diff"}) is False
    assert is_pre_existing({}) is False  # absent ⇒ in-diff by default


# ── tracked, not dropped: dedup key + suggested issue title (r6) ────────────────


def test_pre_existing_rows_carry_a_dedup_key_and_a_suggested_title():
    f = {**_finding(), "scope": PRE_EXISTING_SCOPE}
    assert pre_existing_dedup_key(f) == "pkg/routes.py::explain() dereferences a none config."
    assert suggested_issue_title(f).startswith("Pre-existing: explain() dereferences a None config.")
    section = render_pre_existing_section([f])
    assert "Pre-existing, not blocking" in section
    assert "dedup key:" in section and pre_existing_dedup_key(f) in section
    assert "suggested issue:" in section
    assert render_pre_existing_section([]) == ""


# ── carried priors re-classify every round: the #38 exception (r4) ──────────────


def _confirmed_prior(**extra):
    return {"file": "a.py", "line": 3, "severity": "major", "claim": "bug", "verdict": "confirmed", **extra}


_REFUTE_UNCHANGED = [{"prior": "a.py:3", "disposition": "refuted", "why": "this line is not modified by the diff"}]


def test_a_carried_in_diff_prior_refuted_on_unchanged_code_is_still_not_honoured():
    history = [{"head": "d" * 40, "verdict": "FAIL", "findings": [_confirmed_prior()]}]
    owed = unaccounted_priors(history, _REFUTE_UNCHANGED, ranges={})
    assert [o.get("owed") for o in owed] == [OUTCOME_NOT_HONOURED]


def test_a_carried_prior_re_classified_pre_existing_clears():
    history = [{"head": "d" * 40, "verdict": "FAIL", "findings": [_confirmed_prior(scope=PRE_EXISTING_SCOPE)]}]
    assert unaccounted_priors(history, _REFUTE_UNCHANGED, ranges={}) == []


def test_a_pre_existing_carry_is_neither_gating_debt_nor_carried_debt():
    assert gating_debt([_confirmed_prior()]) != []
    assert gating_debt([_confirmed_prior(scope=PRE_EXISTING_SCOPE)]) == []
    carried = _confirmed_prior(carried=True)
    assert carried_debt({"findings": [carried]}) == [carried]
    assert carried_debt({"findings": [{**carried, "scope": PRE_EXISTING_SCOPE}]}) == []


# ── the supersede path excludes pre-existing (r4, step 4) ───────────────────────

_SUP_MAJOR = {"file": "a.py", "line": 3, "severity": "major", "claim": "c", "evidence": "e", "verdict": "confirmed"}


def test_blocking_priors_excludes_a_pre_existing_finding():
    assert blocking_priors({"findings": [_SUP_MAJOR]}) == [_SUP_MAJOR]
    assert blocking_priors({"findings": [{**_SUP_MAJOR, "scope": PRE_EXISTING_SCOPE}]}) == []


def _body(verdict, findings, record, *, complete=True, verified=True):
    return render_verdict_body(
        repo="o/r",
        pr=1,
        head_sha=HEAD,
        verdict=verdict,
        findings=findings,
        shadow=False,
        recipe="code-review",
        brief="prose",
        complete=complete,
        verified=verified,
        disposition_token=encode_disposition_record(record),
    )


def _rounds(reviews):
    return [
        r for rev in reviews for r in panel_rounds([{**parse_verdict_marker(rev["body"]), **rev, "body": rev["body"]}])
    ]


def test_a_same_head_round_supersedes_a_fail_whose_finding_is_now_pre_existing():
    # The #4581 shape: round 1 FAILs on a blocking finding; round 2 on the SAME head carries it
    # as pre-existing (so `blocking_priors` drops it from round 2's standing debt) and dispositions
    # round 1's finding refuted-with-evidence-honoured. The FAIL is superseded.
    fail = {"id": 102, "body": _body("FAIL", [_SUP_MAJOR], None)}
    carried_pre_existing = {**_SUP_MAJOR, "scope": PRE_EXISTING_SCOPE, "carried": True}
    record = {"of": 102, "rows": [{"a": "a.py:3", "d": "refuted", "e": True, "h": True}]}
    newer = {"id": 103, "body": _body("PASS", [carried_pre_existing], record)}
    rounds = _rounds([fail, newer])
    gone = {id(f) for f, _ in superseded_fails(rounds)}
    assert [r["id"] for r in rounds if id(r) in gone] == [102]


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
    findings = [dict(_STRUCTURAL)]
    assert premark_refuted(findings, store, "o/r", {_STRUCTURAL["file"]: [(10, 20)]}) == 1
    assert findings[0]["verdict"] == "refuted"


def test_a_dismissed_structural_finding_is_live_again_where_the_pr_changes_it(tmp_path):
    store = RefutationStore(tmp_path, ttl_days=14)
    store.record_dismissal("o/r", [_STRUCTURAL], pr=1, head="abc123")
    findings = [dict(_STRUCTURAL)]
    assert premark_refuted(findings, store, "o/r", {_STRUCTURAL["file"]: [(417, 419)]}) == 0
    assert findings[0]["verdict"] == "confirmed"


def test_the_llm_store_never_remembers_a_structural_finding(tmp_path):
    llm = LlmRefutationStore(tmp_path)
    assert llm.record_dismissal("o/r", 99, [_STRUCTURAL], pr=1, head="abc123") == 0


# ── end to end through the dispatcher ───────────────────────────────────────────


def _report(*findings: dict) -> str:
    return "<!-- brief -->\nBrief.\n<!-- /brief -->\n\n```json\n" + json.dumps(list(findings)) + "\n```"


class PreExistingGH(RoutedGH):
    """Serves the PR's own patch, the cited file at head and at the merge-base, and the
    merge-base SHA — everything #300 scoping reads. `base_sha=""` makes the merge-base
    unreadable (the fail-closed case)."""

    def __init__(self, *, patch=PR_PATCH, head_src=HEAD_SRC, base_src=BASE, base_sha=BASE_SHA, **kw):
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


# An LLM-lane major (no `source`) on the unchanged defect line, grounded enough to stay confirmed.
LLM_MAJOR = {
    "file": FILE,
    "line": DEFECT_LINE,
    "severity": "major",
    "category": "correctness",
    "claim": "explain() dereferences a None config.",
    "evidence": "the call path reads cfg.name without a guard.",
    "verdict": "confirmed",
}


async def test_the_verifier_says_yes_so_it_does_not_fail_and_is_listed(tmp_path):
    gh = PreExistingGH(pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner, base_verifier=_verifier("yes"))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    body = gh.reviews_posted[0]["body"]
    assert "Pre-existing, not blocking" in body
    assert "dedup key:" in body and "suggested issue:" in body
    recorded = json.loads(extract_findings_json(body))
    assert recorded and recorded[0]["scope"] == PRE_EXISTING_SCOPE
    events = {e["event"]: e for e in d.telemetry.read_all()}
    assert events["pre_existing"]["findings"][0]["file"] == FILE


async def test_the_verifier_says_no_so_it_still_fails(tmp_path):
    # (a) An unchanged caller the PR's change broke: eligible (byte-identical), but the verifier,
    # seeing only the base, answers `no` — the defect is the PR's, so it keeps gating.
    gh = PreExistingGH(pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner, base_verifier=_verifier("no"))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_an_unsure_verifier_fails_closed(tmp_path):
    # (e) `unsure` is not `yes` ⇒ IN-DIFF.
    gh = PreExistingGH(pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner, base_verifier=_verifier("unsure"))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_a_raising_verifier_fails_closed(tmp_path):
    # (e) A verifier error must never fail the round, and never relieve — it reads `unsure`.
    gh = PreExistingGH(pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner, base_verifier=_verifier(boom=True))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_no_verifier_wired_fails_closed(tmp_path):
    # The production default until a verifier is wired: nothing is ever relieved to pre-existing.
    gh = PreExistingGH(pr_facts=facts(), reviews=[])
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_an_unreadable_merge_base_keeps_the_finding_blocking(tmp_path):
    # (r3) Even a `yes`-answering verifier cannot relieve when the merge-base cannot be read.
    gh = PreExistingGH(pr_facts=facts(), reviews=[], base_sha="")
    runner, _ = capturing_runner(_report(LLM_MAJOR))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner, base_verifier=_verifier("yes"))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


# ── re-anchoring a prior before scoping (steps 2 & 3) ───────────────────────────


class ReanchorGH(RoutedGH):
    """Serves the prior-head→head compare `_delta_hunks` reads to re-anchor a prior (#300 step 2)."""

    def __init__(self, *, delta_patch, **kw):
        super().__init__(pr_facts=facts(), reviews=[], **kw)
        self.delta_patch = delta_patch

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/compare/" in joined and "files" in joined:
            return 0, json.dumps([{"filename": FILE, "patch": self.delta_patch}]), ""
        return await super().__call__(args, timeout=timeout)


async def test_c_a_prior_is_reanchored_through_the_push_before_scoping(tmp_path):
    # A push inserted three lines above the defect between the prior head and this head. The prior
    # cites its own-head line; it must be re-anchored to THIS head before the classifier reads it,
    # and its RECORDED line must never be mutated.
    head_after_push = "\n".join(["# new 1", "# new 2", "# new 3", *_BASE]) + "\n"
    gh = ReanchorGH(delta_patch=unified(BASE, head_after_push))
    d = make(tmp_path, gh=gh, base_verifier=_verifier("yes"))
    seen: dict = {}

    async def spy(repo, pr, head, facts_, findings, *, raw=None):
        seen["line"] = findings[0]["line"]
        findings[0]["scope"] = PRE_EXISTING_SCOPE
        return {id(findings[0])}

    d._classify_scopes = spy
    prior = _finding(line=DEFECT_LINE, since=PRIOR_HEAD, verdict="confirmed")
    history = [{"head": PRIOR_HEAD, "verdict": "FAIL", "findings": [prior]}]
    await d._classify_prior_scopes("o/r", 1, HEAD, facts(), history, [prior])
    assert seen["line"] == DEFECT_LINE + 3  # re-anchored to the pushed head
    assert prior["line"] == DEFECT_LINE  # the ledger's anchor is untouched
    assert prior["scope"] == PRE_EXISTING_SCOPE


async def test_d_a_push_that_edits_a_previously_pre_existing_line_makes_it_in_diff(tmp_path):
    # The push since the prior round EDITED the defect line itself ⇒ the prior cannot be classified
    # and stays blocking, even if last round had called it pre-existing.
    head_after_edit = "\n".join(_BASE[:9] + ["    name = cfg.name or default"] + _BASE[10:]) + "\n"
    gh = ReanchorGH(delta_patch=unified(BASE, head_after_edit))
    d = make(tmp_path, gh=gh, base_verifier=_verifier("yes"))
    called = {"n": 0}

    async def spy(repo, pr, head, facts_, findings, *, raw=None):
        called["n"] += 1
        return set()

    d._classify_scopes = spy
    prior = _finding(line=DEFECT_LINE, since=PRIOR_HEAD, verdict="confirmed", scope=PRE_EXISTING_SCOPE)
    history = [{"head": PRIOR_HEAD, "verdict": "FAIL", "findings": [prior]}]
    await d._classify_prior_scopes("o/r", 1, HEAD, facts(), history, [prior])
    assert called["n"] == 0  # the touched prior never reaches the classifier
    assert prior["scope"] == IN_DIFF_SCOPE  # and its stale pre-existing label was reset


async def test_an_unreadable_prior_delta_cannot_reanchor_and_keeps_gating(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[], compare=None)  # the compare read fails
    d = make(tmp_path, gh=gh, base_verifier=_verifier("yes"))
    called = {"n": 0}

    async def spy(repo, pr, head, facts_, findings, *, raw=None):
        called["n"] += 1
        return set()

    d._classify_scopes = spy
    prior = _finding(line=DEFECT_LINE, since=PRIOR_HEAD, verdict="confirmed", scope=PRE_EXISTING_SCOPE)
    history = [{"head": PRIOR_HEAD, "verdict": "FAIL", "findings": [prior]}]
    await d._classify_prior_scopes("o/r", 1, HEAD, facts(), history, [prior])
    assert called["n"] == 0
    assert prior["scope"] == IN_DIFF_SCOPE


def test_delta_ranges_still_reads_the_same_patches():
    # The padded head-side ranges `line_in_diff`/#232 use are still derivable from the same patch.
    assert delta_ranges([{"filename": FILE, "patch": PR_PATCH}]).get(FILE)

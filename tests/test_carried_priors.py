"""Carried prior findings: dispositioned, re-verified, and never re-asserted unverified.

The pure layer of #218 (a fixed/refuted prior still carried, its carry note duplicated),
#220's refinement (carried priors read as a verifier failure, so the head held at
`hold:unverified` for good) and #232 ask 5 (a re-listed prior blocking a round on a line the
delta had just rewritten). The dispatcher end of the same cases is in test_dispatch.py.
"""

from __future__ import annotations

from pr_reviewer.approve import HOLD_CARRIED_PRIOR, HOLD_UNVERIFIED, PROMOTE, Observations, promotion_decision
from pr_reviewer.checks import FAILURE, check_for
from pr_reviewer.rounds import (
    align_recheck,
    carried_debt,
    needs_recheck,
    recheck_clears,
    recheck_payload,
    relisted_blocking_priors,
    resolve_relisting,
    unaccounted_priors,
)
from pr_reviewer.verdicts import CARRIED_NOTE, merge_carried_findings, verification_ran

RAISED = "b" * 40
MID = "c" * 40

# protoAgent#3811, raised at adf3b10c: the claim cites the function (line 371); the
# try/catch that fixed it landed at 384–395, in a hunk starting at 381.
A2A = {
    "file": "apps/web/src/lib/api/a2aStream.ts",
    "line": 371,
    "severity": "major",
    "claim": "A single malformed SSE frame kills the entire stream in drainSseBuffer.",
    "evidence": "drainSseBuffer calls JSON.parse(data) without a try/catch.",
    "verdict": "confirmed",
}
A2A_FIX_DELTA = {"apps/web/src/lib/api/a2aStream.ts": [(376, 403)]}

# protoAgent#3812: a changelog fragment's line 1, rewritten by the fix.
CHANGELOG = {
    "file": "changelog.d/3805.fixed.md",
    "line": 1,
    "severity": "major",
    "claim": "The `(#3805)` reference sits outside the bold lead-in.",
    "verdict": "confirmed",
}


def _history(*findings, head=RAISED):
    return [{"head": head, "verdict": "FAIL", "findings": list(findings)}]


# ── #218: the carry is idempotent and keeps what the next round needs ──────────


def test_a_re_carried_prior_keeps_exactly_one_carry_note():
    once = merge_carried_findings([], [{**A2A, "note": "Re-read at head: no try/catch."}])[0]
    twice = merge_carried_findings([], [once])[0]
    thrice = merge_carried_findings([], [twice])[0]
    assert thrice["note"].count(CARRIED_NOTE) == 1
    assert thrice["note"].startswith("Re-read at head: no try/catch.")
    bare = merge_carried_findings([], [{**A2A, "note": CARRIED_NOTE}])[0]
    assert bare["note"] == CARRIED_NOTE


def test_a_carry_keeps_the_evidence_and_remembers_a_prior_nobody_verified():
    carried = merge_carried_findings([], [A2A])[0]
    assert carried["evidence"] == A2A["evidence"]  # the next round's evidence-gone read needs it
    assert "raised_unverified" not in carried
    unverified = merge_carried_findings([], [{k: v for k, v in A2A.items() if k != "verdict"}])[0]
    assert unverified["verdict"] == "confirmed"  # still gates — an unproven downgrade must not un-block
    assert unverified["raised_unverified"] is True
    # …and the memory survives the next carry, which sees the stamped `confirmed`.
    assert merge_carried_findings([], [unverified])[0]["raised_unverified"] is True


def test_a_refutation_of_a_confirmed_prior_on_a_line_the_delta_rewrote_accounts_for_it():
    # protoAgent#3812: "refuted — the current head places the (#3805) reference inside the
    # bold lead-in", about line 1, which the delta rewrote. That is a fix, worded as a refutation.
    rows = [{"prior": "changelog.d/3805.fixed.md:1", "disposition": "refuted", "why": "now inside the bold"}]
    moved = {"changelog.d/3805.fixed.md": [(1, 13)]}
    assert unaccounted_priors(_history(CHANGELOG), rows, ranges=moved) == []


def test_a_refutation_of_a_confirmed_prior_on_unmoved_code_still_holds_the_block():
    # #38 unchanged: one model draw does not override a confirmed finding on the same code.
    rows = [{"prior": "changelog.d/3805.fixed.md:1", "disposition": "refuted", "why": "looks fine to me"}]
    assert len(unaccounted_priors(_history(CHANGELOG), rows, ranges={"other.md": [(1, 9)]})) == 1
    assert len(unaccounted_priors(_history(CHANGELOG), rows, ranges=None)) == 1  # unreadable ⇒ held


def test_a_prior_on_a_file_the_pr_does_not_change_is_not_this_prs_debt():
    # protoContent#565: a confined `(repo root)` "no changeset" finding carried for five rounds.
    root = {"file": "(repo root)", "line": 0, "severity": "major", "claim": "No changeset.", "verdict": "confirmed"}
    rows = [{"prior": "(repo root):0", "disposition": "fixed", "why": "the changeset is in"}]
    assert unaccounted_priors(_history(root), rows, ranges={}, paths=["docs/a.md", ".changeset/x.md"]) == []
    # An unreadable file list filters nothing — fail-closed.
    assert len(unaccounted_priors(_history(root), rows, ranges={}, paths=[])) == 1


# ── #220: carried debt is its own hold, not a verifier failure ────────────────


def test_carried_rows_are_not_findings_the_verifier_missed():
    carried = merge_carried_findings([], [A2A])
    # protoContent#565 r6: zero fresh findings, two carried, the verifier said nothing-to-verify.
    assert verification_ran("VERIFY_STATUS: nothing-to-verify", carried) is True
    # r4: the verifier annotated the one fresh finding; two carried rows rode along.
    fresh = {"file": "docs/x.md", "line": 3, "severity": "minor", "claim": "c", "verdict": "confirmed"}
    assert verification_ran("VERIFY_STATUS: annotated n=1", [fresh, *carried, *carried]) is True
    # A fresh finding the verifier never reached still reads as unverified…
    assert verification_ran("VERIFY_STATUS: nothing-to-verify", [*carried, {**fresh, "verdict": ""}]) is False
    # …and so does a carried row with no ruling at all: nothing vouched for it.
    assert verification_ran("", [{**A2A, "verdict": "", "carried": True}]) is False


def test_carried_debt_is_the_dispatchers_carry_not_a_relisting():
    record = merge_carried_findings([], [A2A])
    assert [f["file"] for f in carried_debt({"findings": record})] == [A2A["file"]]
    relisted = {**A2A, "carried": True, "carried_by": "synthesizer"}
    minor = {**A2A, "severity": "minor", "carried": True}
    assert carried_debt({"findings": [relisted, minor, A2A]}) == []
    assert carried_debt(None) == []


def test_promotion_holds_on_carried_debt_with_its_own_reason():
    base = dict(
        head_sha="h",
        checks_state="green",
        unresolved_threads=0,
        verdict_head="h",
        verdict_promoted=False,
        promotion_owner=True,
    )
    assert promotion_decision(Observations(**base)) == PROMOTE
    assert promotion_decision(Observations(**base, carried_debt=True)) == HOLD_CARRIED_PRIOR
    # A round that is ALSO unverified reports that first — the verify retry is its remedy.
    assert promotion_decision(Observations(**base, carried_debt=True, verified=False)) == HOLD_UNVERIFIED
    run = check_for(HOLD_CARRIED_PRIOR, verdict="PASS")
    assert run.conclusion == FAILURE and "Prior blocking finding" in run.title
    assert "@vera review" in run.summary


# ── the targeted re-check (#218, #220, #232 ask 5) ────────────────────────────


def test_a_refutation_at_the_new_head_clears_a_prior_only_on_new_evidence():
    # Its file moved (protoAgent#3811 — the fix landed 13 lines from the cited line).
    assert recheck_clears(A2A, "refuted", A2A_FIX_DELTA, None) is True
    # Never verified to begin with (protoContent#565's priors).
    unverified = {**A2A, "raised_unverified": True}
    assert recheck_clears(unverified, "refuted", {"other.ts": [(1, 5)]}, None) is True
    # A verifier-confirmed prior on a file nobody touched: one draw against another (#38).
    assert recheck_clears(A2A, "refuted", {"other.ts": [(1, 5)]}, None) is False
    assert recheck_clears(A2A, "refuted", None, None) is False  # unreadable delta ⇒ not proven
    # Only `refuted` clears.
    assert recheck_clears(unverified, "uncertain", A2A_FIX_DELTA, None) is False
    assert recheck_clears(unverified, "confirmed", A2A_FIX_DELTA, None) is False


def test_the_delta_since_a_prior_was_raised_decides_not_just_the_last_one():
    since = {**A2A, "since": RAISED}
    # The last round's delta touched nothing; the one since the prior was RAISED did (#131).
    assert recheck_clears(since, "refuted", {"other.ts": [(1, 5)]}, {RAISED: A2A_FIX_DELTA}) is True
    assert needs_recheck(since, {"other.ts": [(1, 5)]}, {RAISED: A2A_FIX_DELTA}) is True
    assert needs_recheck(since, {"other.ts": [(1, 5)]}, {RAISED: {}}) is False


def test_a_recheck_payload_hides_the_earlier_ruling():
    carried = merge_carried_findings([], [{**A2A, "note": "re-read: no try/catch"}])[0]
    payload = recheck_payload(carried)
    assert set(payload) == {"file", "line", "severity", "claim", "evidence"}


def test_align_recheck_is_positional_on_the_contract_and_by_claim_otherwise():
    other = {**CHANGELOG}
    candidates = [recheck_payload(A2A), recheck_payload(other)]
    annotated = [{**A2A, "verdict": "refuted", "note": "try/catch at 384"}, {**other, "verdict": "Confirmed"}]
    assert align_recheck(candidates, annotated) == [
        {"verdict": "refuted", "note": "try/catch at 384"},
        {"verdict": "confirmed", "note": ""},
    ]
    # Out of order, one dropped, one re-anchored — matched by file and claim; the rest None.
    reordered = [{**other, "line": 2, "verdict": "uncertain"}]
    assert align_recheck(candidates, reordered) == [None, {"verdict": "uncertain", "note": ""}]
    # A verdict outside the vocabulary (or none — "source unavailable") is not a ruling.
    assert align_recheck(candidates[:1], [{**A2A, "verdict": "SUPPORTED"}]) == [None]
    assert align_recheck(candidates[:1], [recheck_payload(A2A)]) == [None]


# protoAgent#4017 r2: round 1 confirmed an F841 at line 64; round 2's diff renamed exactly that
# line (`cfg, a, b` → `cfg, _, b`), and the report re-listed the finding with no verdict.
F841 = {
    "file": "tests/test_fs_missing_root_3643.py",
    "line": 64,
    "severity": "major",
    "claim": "The `a` variable unpacked from `two_projects` is assigned but never used, an F841.",
    "evidence": "cfg, a, b = two_projects",
    "verdict": "confirmed",
}
F841_RELISTED = {k: v for k, v in F841.items() if k != "verdict"} | {"evidence": ""}
F841_RENAMED = {"tests/test_fs_missing_root_3643.py": [(59, 69)]}


def test_a_verdict_less_relisting_of_a_prior_major_is_found_and_keeps_its_provenance():
    other = {"file": "x.py", "line": 9, "severity": "major", "claim": "a new bug", "evidence": "e"}
    findings, pairs = relisted_blocking_priors([F841_RELISTED, other], _history(F841))
    assert [i for i, _ in pairs] == [0]
    assert findings[0]["since"] == RAISED  # the window a fix is proven over starts at the raise
    assert pairs[0][1]["since"] == RAISED
    # A row with a verdict is this round's own verified finding — not a re-listing.
    _f, none = relisted_blocking_priors([{**F841_RELISTED, "verdict": "confirmed"}], _history(F841))
    assert none == []
    # Minor/nit re-listings stay #204's business.
    _f, none = relisted_blocking_priors([{**F841_RELISTED, "severity": "minor"}], _history(F841))
    assert none == []


def test_a_relisting_on_a_rewritten_line_never_blocks_unverified():
    findings, [(i, prior)] = relisted_blocking_priors([F841_RELISTED], _history(F841))
    row = findings[i]
    # Re-checked and refuted on code the delta changed → dropped.
    assert resolve_relisting(row, prior, {"verdict": "refuted", "note": "now `_`"}, F841_RENAMED, None)[0] == "cleared"
    # Re-checked and confirmed → blocks, verified.
    outcome, out = resolve_relisting(row, prior, {"verdict": "confirmed", "note": "still unused"}, F841_RENAMED, None)
    assert outcome == "confirmed" and out["verdict"] == "confirmed"
    # No ruling, and the cited line changed since it was raised → deferred to the carry: not a
    # blocking verdict nobody verified (ask 5), not cleared either.
    outcome, out = resolve_relisting(row, prior, None, F841_RENAMED, None)
    assert outcome == "deferred" and out is prior
    # No ruling, cited line untouched since a verifier confirmed it → that ruling still holds.
    outcome, out = resolve_relisting(row, prior, None, {"other.py": [(1, 3)]}, None)
    assert outcome == "inherited" and out["verdict"] == "confirmed"
    # No ruling, delta unreadable → exactly as before: verdict-less, unverified.
    assert resolve_relisting(row, prior, None, None, None) == ("unverified", row)
    # A refutation of a verifier-confirmed prior on untouched code does not clear it (#38).
    outcome, _ = resolve_relisting(row, prior, {"verdict": "refuted", "note": "n"}, {"other.py": [(1, 3)]}, None)
    assert outcome == "inherited"


def test_an_escalated_minor_is_not_a_relisting_of_it():
    minor = {**F841, "severity": "minor"}
    _f, pairs = relisted_blocking_priors([F841_RELISTED], _history(minor))
    assert pairs == []

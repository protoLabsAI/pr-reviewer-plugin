"""#306: a round whose synthesis had findings but whose verify/report lost them never
posts PASS.

careercoach-archetype#2 @2470a74: the lanes raised 7 findings, synthesize carried them,
the verifier said "the annotated findings list (n=7) and the PR diff were not included in
the input. No findings were verified", the report's findings array came out `[]`, and the
body rendered "No findings — the review came back clean" — a PASS approve-on-green then
promoted. Every guard judged the round by what the REPORT kept, so a report that dropped
everything looked clean."""

from __future__ import annotations

import json

from pr_reviewer.telemetry import Telemetry
from pr_reviewer.verdicts import merge_lost_findings, read_findings_record, verifier_ignored_synthesis

from tests.dispatch_helpers import HEAD, OLD_HEAD, RoutedGH, facts, make, review_row

GREEN = [{"status": "completed", "conclusion": "success"}]

LLM_LANES = ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions")
CLEAN_LANE = "No issues from this angle.\n\n```json\n[]\n```\nFINDER_STATUS: reviewed n=0"

# The verifier's reply on careercoach-archetype#2, near-verbatim: prose, no status line,
# no findings array. `verify_delivered` (#151) accepts nothing here, but that guard only
# runs when the round reports findings — which this round's report dropped.
VERIFY_IGNORED = (
    "The annotated findings list (n=7) and the PR diff were not included in the input. No findings were verified."
)

EMPTY_REPORT = "<!-- brief -->\nNo findings — clean.\n<!-- /brief -->\n\n```json\n[]\n```"


def findings(n=7, severity="major"):
    return [
        {
            "file": f"src/module_{i}.py",
            "line": 10 + i,
            "severity": severity,
            "category": "correctness",
            "claim": f"Defect {i} in handler_{i}().",
            "evidence": f"handler_{i}(data)",
        }
        for i in range(n)
    ]


def synth(rows):
    return "<!-- brief -->\nWhat the lanes raised.\n<!-- /brief -->\n\n```json\n" + json.dumps(rows) + "\n```"


def verify_annotated(rows, verdict):
    annotated = [{**r, "verdict": verdict, "note": "checked"} for r in rows]
    return f"VERIFY_STATUS: annotated n={len(rows)}\n\n```json\n" + json.dumps(annotated) + "\n```"


def core_steps(**over):
    """Every step of the core four-finder `code-review` recipe delivering an explicit
    `[]`, with `over` swapped in. No structural seat — the small-diff recipe."""
    steps = {s: CLEAN_LANE for s in LLM_LANES}
    steps["synthesize"] = "<!-- brief -->\nNothing raised.\n<!-- /brief -->\n\n```json\n[]\n```"
    steps["verify"] = "VERIFY_STATUS: nothing-to-verify\n\n```json\n[]\n```"
    steps.update(over)
    return steps


def structural_steps(**over):
    steps = core_steps(find_structural="protoPatch run: head/base resolved, 6 files.\n\n```json\n[]\n```")
    steps.update(over)
    return steps


def _structural_gh(**kw) -> RoutedGH:
    return RoutedGH(pr_facts=facts(changed_files=6, additions=300, deletions=50), files="x.py\nb\nc\nd\ne\nf\n", **kw)


def _events(tmp_path, name):
    return [e for e in Telemetry(tmp_path).read_all() if e.get("event") == name]


def test_the_306_verify_reply_reads_as_ignored_synthesis():
    rows = findings()
    assert verifier_ignored_synthesis(VERIFY_IGNORED, rows) is True
    # Either annotation signal counts as the verifier having answered…
    assert verifier_ignored_synthesis(verify_annotated(rows, "confirmed"), rows) is False
    assert verifier_ignored_synthesis("VERIFY_STATUS: annotated n=0", rows) is False
    assert (
        verifier_ignored_synthesis("```json\n" + json.dumps([{**rows[0], "verdict": "refuted"}]) + "\n```", rows)
        is False
    )
    # …while a verdict-less echo of the findings annotated nothing.
    assert verifier_ignored_synthesis("```json\n" + json.dumps(rows) + "\n```", rows) is True
    # An empty synthesis has nothing to ignore (the clean round, #151's territory).
    assert verifier_ignored_synthesis(VERIFY_IGNORED, []) is False


async def test_306_replay_core_recipe_posts_no_verdict_and_never_promotes(tmp_path):
    """r1: synthesize carries 7, verify annotates nothing, report array `[]` — the round
    concludes with NO verdict (the #113 outcome), after the seeded verify re-run had its
    chance, and approve-on-green has nothing to promote."""
    rows = findings()
    calls: list[str] = []

    async def runner(name, inputs, *, seed_outputs=None):
        calls.append("seeded" if seed_outputs is not None else name)
        steps = (
            {"verify": VERIFY_IGNORED, "report": EMPTY_REPORT}
            if seed_outputs is not None
            else core_steps(synthesize=synth(rows), verify=VERIFY_IGNORED)
        )
        return {"output": EMPTY_REPORT, "steps": steps, "failed": []}

    gh = RoutedGH(pr_facts=facts(), checks=GREEN)
    d = make(
        tmp_path,
        cfg={"shadow_mode": False, "promotion_owner": True, "verify_fallback_panel": False},
        gh=gh,
        runner=runner,
    )
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "error:panel-incomplete"
    # The re-run path ran first (#167/#182, broadened to this shape) and did not recover.
    assert calls == ["code-review", "seeded"]
    # No PASS — no review at all — and so nothing that could say "came back clean".
    assert gh.reviews_posted == []
    # Approve-on-green has no clear verdict to promote.
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:no-clear-verdict"
    assert gh.reviews_posted == []
    row = _events(tmp_path, "exhaustion")[-1]
    assert row["undelivered"] == ["verify"] and row["synthesized"] == 7


async def test_306_replay_on_a_host_that_cannot_seed_still_posts_no_verdict(tmp_path):
    calls = 0

    async def runner(name, inputs):  # protoAgent < #3571: no seed_outputs
        nonlocal calls
        calls += 1
        return {
            "output": EMPTY_REPORT,
            "steps": core_steps(synthesize=synth(findings()), verify=VERIFY_IGNORED),
            "failed": [],
        }

    gh = RoutedGH(pr_facts=facts())
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-incomplete"
    assert calls == 1 and gh.reviews_posted == []


async def test_a_seeded_rerun_that_recovers_the_verify_step_posts_normally(tmp_path):
    """The existing #167/#182 re-run path is reused first: a verifier that annotates on
    the restated handover clears the round, and it posts like any verified round."""
    # In-diff files (the PR's changed path is x.py), so confinement keeps the findings.
    rows = [{**f, "file": "x.py", "line": 3 + i} for i, f in enumerate(findings(2, severity="minor"))]
    verified_report = (
        "<!-- brief -->\nTwo, confirmed.\n<!-- /brief -->\n\n```json\n"
        + json.dumps([{**r, "verdict": "confirmed", "note": "traced"} for r in rows])
        + "\n```"
    )
    calls: list[str] = []

    async def runner(name, inputs, *, seed_outputs=None):
        calls.append("seeded" if seed_outputs is not None else "fresh")
        if seed_outputs is None:
            return {
                "output": EMPTY_REPORT,
                "steps": core_steps(synthesize=synth(rows), verify=VERIFY_IGNORED),
                "failed": [],
            }
        return {
            "output": verified_report,
            "steps": {"verify": verify_annotated(rows, "confirmed"), "report": verified_report},
            "failed": [],
        }

    gh = RoutedGH(pr_facts=facts())
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    assert calls == ["fresh", "seeded"]
    body = gh.reviews_posted[0]["body"]
    assert "report_lost_findings" not in body and "came back clean" not in body


async def test_a_full_explicit_refutation_may_still_post_pass(tmp_path):
    """r2: the verifier refuted all 7 and the report dropped them, as its contract says —
    a legitimate clean round, not a loss."""
    rows = findings()

    async def runner(name, inputs, *, seed_outputs=None):
        return {
            "output": EMPTY_REPORT,
            "steps": core_steps(synthesize=synth(rows), verify=verify_annotated(rows, "refuted")),
            "failed": [],
        }

    gh = RoutedGH(pr_facts=facts())
    d = make(tmp_path, cfg={"state_root": str(tmp_path / "st")}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    body = gh.reviews_posted[0]["body"]
    assert "came back clean" in body
    assert "report_lost_findings" not in body and "complete=false" not in body


async def test_confirmed_findings_dropped_by_the_report_cap_pass_at_warn_and_carry(tmp_path):
    """r3: verify annotates all as confirmed, the report drops them — WARN with a
    `report_lost_findings` coverage gap, round not complete, and the dropped findings
    recorded as `raised_unverified` carried debt."""
    rows = findings()

    async def runner(name, inputs, *, seed_outputs=None):
        return {
            "output": EMPTY_REPORT,
            "steps": core_steps(synthesize=synth(rows), verify=verify_annotated(rows, "confirmed")),
            "failed": [],
        }

    gh = RoutedGH(pr_facts=facts(), checks=GREEN)
    d = make(tmp_path, cfg={"state_root": str(tmp_path / "st")}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "`report_lost_findings` (7 synthesized, 0 reported, 7 not refuted)" in body
    assert "complete=false" in body and "came back clean" not in body
    recorded, _trusted = read_findings_record(body)
    assert len(recorded) == 7 and all(f.get("raised_unverified") for f in recorded)
    assert _events(tmp_path, "report_lost_findings")[-1]["not_refuted"] == 7
    # Approve-on-green never promotes this round: the posted marker holds it.
    gh2 = RoutedGH(pr_facts=facts(), reviews=[{"state": "COMMENTED", "body": body, "id": 9}], checks=GREEN)
    d2 = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh2)
    decision = await d2.evaluate_promotion("o/r", 1)
    assert decision != "promote" and decision.startswith("hold:")
    assert gh2.reviews_posted == []


async def test_a_partial_refutation_still_counts_the_unrefuted_as_lost(tmp_path):
    rows = findings(3)
    annotated = [
        {**rows[0], "verdict": "refuted", "note": "not so"},
        {**rows[1], "verdict": "confirmed", "note": "traced"},
        {**rows[2], "verdict": "uncertain", "note": "unclear"},
    ]
    verify = f"VERIFY_STATUS: annotated n=3\n\n```json\n{json.dumps(annotated)}\n```"

    async def runner(name, inputs, *, seed_outputs=None):
        return {"output": EMPTY_REPORT, "steps": core_steps(synthesize=synth(rows), verify=verify), "failed": []}

    gh = RoutedGH(pr_facts=facts())
    d = make(tmp_path, cfg={"state_root": str(tmp_path / "st")}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "`report_lost_findings` (3 synthesized, 0 reported, 2 not refuted)" in body
    recorded, _trusted = read_findings_record(body)
    assert [f["file"] for f in recorded] == [rows[1]["file"], rows[2]["file"]]


async def test_a_clean_round_still_posts_pass_exactly_as_before(tmp_path):
    """r4: synthesis `[]`, verify `nothing-to-verify`, report `[]` — untouched."""

    async def runner(name, inputs, *, seed_outputs=None):
        return {"output": EMPTY_REPORT, "steps": core_steps(), "failed": []}

    gh = RoutedGH(pr_facts=facts())
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    body = gh.reviews_posted[0]["body"]
    assert "came back clean" in body
    assert "complete=false" not in body and "verified=false" not in body


# ── merge_lost_findings dedup: one row per FINDING, not per file:line ─────────
#
# `rounds.same_prior` treats any two findings at one file:line as one — right for a
# disposition row, which carries only an anchor, wrong for lost synthesized rows, which
# each carry their own claim: two DISTINCT findings the report dropped on one line must
# both become debt, or the second vanishes while the `report_lost_findings` gap still
# counts it (review on this change, r1).

_LOST_A = {"file": "src/io.py", "line": 42, "severity": "major", "claim": "handler_close() double-frees the buffer."}
_LOST_B = {"file": "src/io.py", "line": 42, "severity": "major", "claim": "retry loop never decrements attempts_left."}


def test_two_distinct_lost_findings_on_one_line_are_both_recorded():
    merged = merge_lost_findings([], [_LOST_A, _LOST_B])
    assert len(merged) == 2
    assert all(f.get("raised_unverified") for f in merged)
    assert {f["claim"] for f in merged} == {_LOST_A["claim"], _LOST_B["claim"]}


def test_the_same_lost_finding_twice_is_still_one_debt_row():
    assert len(merge_lost_findings([], [_LOST_A, dict(_LOST_A)])) == 1
    # Reworded, same defect named: still one row (`rounds._similar_claims`).
    reworded = {**_LOST_A, "claim": "handler_close() double-frees the shared buffer."}
    assert len(merge_lost_findings([], [_LOST_A, reworded])) == 1


def test_a_lost_row_that_relists_a_carried_prior_dedups_against_the_carry():
    carry = {**_LOST_A, "carried": True, "note": "carried from a prior round"}
    merged = merge_lost_findings([carry], [dict(_LOST_A), _LOST_B])
    assert len(merged) == 2  # the carry, plus only the DISTINCT lost row
    assert merged[0] is carry or merged[0] == carry
    assert merged[1]["claim"] == _LOST_B["claim"]


def test_a_lost_row_with_no_claim_keeps_the_plain_anchor_matching():
    # Nothing left to tell them apart — fail toward one row, as before.
    bare = {"file": "src/io.py", "line": 42, "severity": "major"}
    assert len(merge_lost_findings([_LOST_A], [bare])) == 1


async def test_report_loss_holds_a_standing_block(tmp_path):
    """`hold_blocks` rides `report_lost` (#306): a round whose report dropped un-refuted
    findings has not earned lifting an earlier REQUEST_CHANGES, even though its own
    verdict (WARN) would otherwise dismiss it (`test_an_ordinary_clean_pass_still_lifts
    _the_block` is the counterfactual)."""
    rows = findings()

    async def runner(name, inputs, *, seed_outputs=None):
        return {
            "output": EMPTY_REPORT,
            "steps": core_steps(synthesize=synth(rows), verify=verify_annotated(rows, "confirmed")),
            "failed": [],
        }

    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(OLD_HEAD, "WARN", state="CHANGES_REQUESTED", id=77)])
    d = make(tmp_path, cfg={"shadow_mode": False, "state_root": str(tmp_path / "st")}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:WARN"
    assert gh.dismissed == []  # the standing REQUEST_CHANGES stays up
    assert _events(tmp_path, "reviewed")[-1]["held"] is True


async def test_lost_rows_merge_after_the_prior_carry_and_dedup_against_it(tmp_path):
    """The merge order (#306): prior carries land first, so a lost row that re-lists a
    carried prior dedups against the carry instead of sitting beside it — while a second,
    DISTINCT lost finding on the very same line is still recorded (review r1). The report
    dispositions the prior `open` (so the #260 ledger carries it) but drops every finding."""
    relist = {"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}
    distinct = {
        "file": "x.py",
        "line": 3,
        "severity": "major",
        "claim": "mutex never released in handle_retry().",
        "evidence": "lock.acquire()",
    }
    dispositions = [{"file": "x.py", "line": 3, "disposition": "open", "claim": "real bug"}]
    report = (
        "<!-- brief -->\nStill open; nothing kept.\n<!-- /brief -->\n\n```json\n"
        + json.dumps(dispositions)
        + "\n```\n\n```json\n[]\n```"
    )

    async def runner(name, inputs):  # cannot seed: no prior re-check, no verify re-run
        return {
            "output": report,
            "steps": core_steps(
                synthesize=synth([relist, distinct]),
                verify=verify_annotated([relist, distinct], "confirmed"),
            ),
            "failed": [],
        }

    prior = json.dumps([relist])
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=prior, id=77)],
        compare=[{"filename": "x.py", "patch": ""}],
    )
    d = make(tmp_path, cfg={"shadow_mode": False, "state_root": str(tmp_path / "st")}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:WARN"
    assert gh.dismissed == []
    body = gh.reviews_posted[0]["body"]
    assert "`report_lost_findings` (2 synthesized, 0 reported, 2 not refuted)" in body
    recorded, _trusted = read_findings_record(body)
    at_line = [f for f in recorded if f.get("file") == "x.py" and f.get("line") == 3]
    assert sorted(str(f.get("claim")) for f in at_line) == [distinct["claim"], "real bug"]
    carried = next(f for f in at_line if f.get("claim") == "real bug")
    assert carried.get("carried") is True  # ONE row for the prior — the carry, not a duplicate
    lost = next(f for f in at_line if f.get("claim") == distinct["claim"])
    assert lost.get("raised_unverified") is True


async def test_306_replay_structural_recipe_posts_no_verdict(tmp_path):
    """r5: the same loss through `code-review-structural` is caught the same way."""
    seen: list[str] = []

    async def runner(name, inputs):
        seen.append(name)
        return {
            "output": EMPTY_REPORT,
            "steps": structural_steps(synthesize=synth(findings()), verify=VERIFY_IGNORED),
            "failed": [],
        }

    gh = _structural_gh()
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-incomplete"
    assert seen == ["code-review-structural"] and gh.reviews_posted == []


async def test_structural_recipe_report_loss_caps_at_warn_with_the_gap(tmp_path):
    rows = findings()

    async def runner(name, inputs, *, seed_outputs=None):
        return {
            "output": EMPTY_REPORT,
            "steps": structural_steps(synthesize=synth(rows), verify=verify_annotated(rows, "confirmed")),
            "failed": [],
        }

    gh = _structural_gh()
    d = make(tmp_path, cfg={"state_root": str(tmp_path / "st")}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "`report_lost_findings` (7 synthesized, 0 reported, 7 not refuted)" in body
    assert "complete=false" in body and "came back clean" not in body
    recorded, _trusted = read_findings_record(body)
    assert len(recorded) == 7 and all(f.get("raised_unverified") for f in recorded)

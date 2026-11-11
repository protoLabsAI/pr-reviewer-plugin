"""Raw accounting is identity-based and independent from the prior-round ledger."""

import copy
import json

import pytest
from pr_reviewer import finding_accounting as a
from pr_reviewer.dispatch import _accounting_runner
from pr_reviewer.verdicts import parse_verdict_marker

from tests.dispatch_helpers import HEAD, RoutedGH, facts, make

F = {"file": "x.py", "line": 3, "severity": "minor", "claim": "Bug", "evidence": "quote", "source": "finder"}


def output(rows, rejected=()):
    return (
        "<!-- brief -->Review.<!-- /brief -->\n```accounting\n"
        + json.dumps({"dispositions": list(rejected)})
        + "\n```\n```json\n"
        + json.dumps(rows)
        + "\n```"
    )


def raw(rows):
    return {
        lane: output(rows if lane == a.LANES[0] else [])
        + f"\nFINDER_STATUS: reviewed n={len(rows) if lane == a.LANES[0] else 0}"
        for lane in a.LANES
    }


def prepared(rows=None):
    inv = a.inventory(raw([F] if rows is None else rows), {"head": HEAD, "attempt": "a"})
    tagged = [dict(v["finding"], raw_ids=[rid], finding_id=rid, verdict="confirmed") for rid, v in inv["raw"].items()]
    return inv, tagged


def check(inv, rows, rejected=()):
    text = output(rows, rejected)
    return a.reconcile(inv, {s: text for s in a.STAGES}, text)


def test_stable_lane_and_attempt_bound_ids_preserve_exact_evidence():
    inv, rows = prepared([F, F])
    assert len(inv["raw"]) == 2
    assert inv == prepared([F, F])[0]
    assert all(v["finding"] == F for v in inv["raw"].values())
    other = a.inventory(raw([F, F]), {"head": HEAD, "attempt": "b"})
    assert not set(inv["raw"]) & set(other["raw"])
    assert check(inv, rows)["complete"]


def test_empty_is_valid_but_missing_malformed_and_truncated_are_gaps():
    assert check(*prepared([]))["complete"]
    for bad in ("", "```json\n[", "```json\n[1]\n```", "```json\n[]", output([]) + "\nFINDER_STATUS: reviewed n=1"):
        steps = raw([])
        steps[a.LANES[0]] = bad
        assert a.inventory(steps, "attempt")["gaps"]
    assert a.array(output([dict(F, evidence="literal ``` in a quote")]))[0]["evidence"] == "literal ``` in a quote"


@pytest.mark.parametrize("kind", ["missing", "duplicate", "foreign", "stale", "malformed"])
def test_identity_errors_cannot_pass_by_equal_counts(kind):
    inv, rows = prepared([F, F])
    if kind == "missing":
        rows.pop()
    elif kind == "duplicate":
        rows[1] = rows[0]
    elif kind in ("foreign", "stale"):
        rows[1]["raw_ids"] = [kind]
        rows[1]["finding_id"] = kind
    else:
        rows[0]["raw_ids"] = [{"bad": True}]
    assert not check(inv, rows)["complete"]


def test_merge_disposition_and_severity_anchor_history():
    inv, rows = prepared([F, F])
    merged = dict(
        rows[0],
        raw_ids=[r["finding_id"] for r in rows],
        severity="major",
        line=8,
        accounting_reason="same defect, caller confirms impact",
    )
    result = check(inv, [merged])
    assert result["complete"]
    assert result["transitions"][0]["before"]["severity"] == "minor"
    assert result["transitions"][0]["after"]["line"] == 8
    del merged["accounting_reason"]
    assert not check(inv, [merged])["complete"]
    rejected = {
        "raw_ids": list(inv["raw"]),
        "disposition": "refuted",
        "reason": "guard prevents it",
        "evidence": "x.py:3 guard",
    }
    assert check(inv, [], [rejected])["complete"]
    rejected["reason"] = ""
    assert not check(inv, [], [rejected])["complete"]


@pytest.mark.parametrize("stage", a.STAGES)
def test_every_transition_detects_loss(stage):
    inv, rows = prepared()
    steps = {s: output(rows) for s in a.STAGES}
    steps[stage] = output([])
    assert not a.reconcile(inv, steps, steps["report"])["complete"]


def test_final_filter_is_audited_and_priors_do_not_count():
    inv, rows = prepared()
    result = check(inv, rows)
    a.finalize(result, output(rows), [dict(F, carried=True)])
    assert not result["complete"]
    assert "omission" in result["gaps"][0]
    assert a.coverage({}, "code-review")["complete"] is None


class NativeRunner:
    def __init__(self, loss=None, rows=None):
        self.rows = [F] if rows is None else rows
        self.calls = []
        self.loss = loss

    async def __call__(self, recipe, inputs, *, seed_outputs=None):
        self.calls.append((copy.deepcopy(inputs), copy.deepcopy(seed_outputs)))
        steps = dict(seed_outputs or {})
        for lane, text in raw(self.rows).items():
            steps.setdefault(lane, text)
        if "finding_accounting" in inputs:
            inv = json.loads(inputs["finding_accounting"])
            rows = [
                dict(v["finding"], raw_ids=[rid], finding_id=rid, verdict="confirmed") for rid, v in inv["raw"].items()
            ]
            for stage in a.STAGES:
                steps.setdefault(stage, output([] if stage == self.loss else rows))
            steps["verify"] += f"\nVERIFY_STATUS: annotated n={len(rows)}"
        return {"steps": steps, "output": steps["report"], "failed": [], "degraded": []}


async def test_barrier_and_seeded_rerun_keep_original_provenance():
    native = NativeRunner()
    run = _accounting_runner(native)
    first = await run("code-review-structural", {"head_sha": HEAD})
    assert len(native.calls) == 2
    assert set(native.calls[0][1]) == set(a.STAGES)
    assert a.coverage(first, "code-review-structural")["complete"]
    seeds = {k: v for k, v in first["steps"].items() if k not in ("verify", "report")}
    again = await run("code-review-structural", {"head_sha": HEAD}, seed_outputs=seeds)
    assert len(native.calls) == 3
    assert again["finding_accounting"] == first["finding_accounting"]
    assert a.coverage(again, "code-review-structural")["complete"]
    fresh = await run("code-review-structural", {"head_sha": HEAD})
    assert not set(fresh["finding_accounting"]["raw"]) & set(first["finding_accounting"]["raw"])


@pytest.mark.parametrize("loss", a.STAGES)
async def test_dispatcher_loss_holds_coverage_and_verification(tmp_path, loss):
    gh = RoutedGH(pr_facts=facts(changed_files=6, additions=150))
    d = make(tmp_path, gh=gh, runner=NativeRunner(loss), cfg={"grounding": False})
    outcome = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert outcome.startswith("reviewed:"), outcome
    body = gh.reviews_posted[-1]["body"]
    marker = parse_verdict_marker(body)
    assert marker["complete"] is False
    assert marker["verified"] is False
    assert "Raw finding accounting: incomplete" in body


def test_mixed_malformed_raw_array_preserves_valid_observations():
    steps = raw([])
    steps[a.LANES[0]] = output([F, None, {"severity": []}])
    inv = a.inventory(steps, "a")
    assert inv["gaps"]
    assert any(v["finding"] == F for v in inv["raw"].values())


def test_disposition_parser_does_not_recover_an_earlier_draft():
    text = output([], [{"raw_ids": ["a"], "evidence": "literal ``` quote"}])
    assert a.dispositions(text)[0]["evidence"] == "literal ``` quote"
    assert a.dispositions(text + '\n```accounting\n{"dispositions": [') is None


def test_code_dispositions_preserve_trace_without_consuming_prior_ledger():
    inv, rows = prepared()
    result = check(inv, rows)
    prior = dict(F, raw_ids=["previous-attempt"])
    a.dispose(result, prior, "refuted", "prior fixed", "read at head")
    assert "dispatcher_dispositions" not in result
    a.dispose(result, rows[0], "out-of-scope", "outside changed files", "x.py:3")
    a.finalize(result, rows, [])
    assert result["complete"]
    assert result["transitions"][-1]["after"]["disposition"] == "out-of-scope"


async def test_seeded_head_change_and_changed_finder_output_fail_closed():
    run = _accounting_runner(NativeRunner())
    first = await run("code-review-structural", {"head_sha": HEAD})
    seeds = {k: v for k, v in first["steps"].items() if k in a.LANES}
    stale = await run("code-review-structural", {"head_sha": "new"}, seed_outputs=seeds)
    assert not a.coverage(stale, "code-review-structural")["complete"]
    assert any("stale" in g for g in stale["finding_accounting"]["gaps"])


async def test_finder_execution_metadata_survives_seeded_second_pass():
    native = NativeRunner()

    async def runner(recipe, inputs, *, seed_outputs=None):
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        first = "finding_accounting" not in inputs
        result["timings"] = {a.LANES[0]: 42 if first else 0}
        result["tool_calls"] = {a.LANES[0]: [{"tool": "github_read_file", "repo": "wrong/repo"}] if first else []}
        return result

    result = await _accounting_runner(runner)("code-review-structural", {"head_sha": HEAD})
    assert result["timings"][a.LANES[0]] == 42
    assert result["tool_calls"][a.LANES[0]][0]["repo"] == "wrong/repo"


async def test_native_refutation_posts_complete_and_preserves_raw_sources(tmp_path):
    native = NativeRunner()

    async def runner(recipe, inputs, *, seed_outputs=None):
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        if "finding_accounting" in inputs:
            ids = list(json.loads(inputs["finding_accounting"])["raw"])
            rejected = [{"raw_ids": ids, "disposition": "refuted", "reason": "guard blocks this", "evidence": "x.py:3"}]
            result["output"] = output([], rejected)
            result["steps"]["report"] = result["output"]
        return result

    gh = RoutedGH(pr_facts=facts(changed_files=6))
    d = make(tmp_path, gh=gh, runner=runner)
    assert await d.handle_pr_event("o/r", 1, HEAD, "opened") == "reviewed:PASS"
    body = gh.reviews_posted[-1]["body"]
    marker = parse_verdict_marker(body)
    assert marker["complete"] and marker["verified"]
    assert '"source": "finder"' in body and "guard blocks this" in body


async def test_native_contradicted_verify_rerun_preserves_inventory(tmp_path):
    native = NativeRunner()
    passes = 0

    async def runner(recipe, inputs, *, seed_outputs=None):
        nonlocal passes
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        if "finding_accounting" in inputs:
            passes += 1
            if passes == 1:
                result["steps"]["verify"] = "VERIFY_STATUS: nothing-to-verify"
        return result

    wrapped = _accounting_runner(runner)
    inputs = {"head_sha": HEAD}
    initial = await wrapped("code-review-structural", inputs)
    d = make(tmp_path)
    again = await d._rerun_contradicted_verify(
        wrapped, "code-review-structural", inputs, initial, repo="o/r", pr=1, head=HEAD
    )
    assert passes == 2
    assert again["finding_accounting"] == initial["finding_accounting"]
    assert a.coverage(again, "code-review-structural")["complete"]


async def test_partial_verification_holds_even_with_a_nonempty_verified_array(tmp_path):
    native = NativeRunner(rows=[F, dict(F, claim="Second defect", line=8)])

    async def runner(recipe, inputs, *, seed_outputs=None):
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        if "finding_accounting" in inputs:
            rows = a.array(result["steps"]["verify"])
            result["steps"]["verify"] = output(rows[:1]) + "\nVERIFY_STATUS: annotated n=1"
        return result

    gh = RoutedGH(pr_facts=facts(changed_files=6))
    d = make(tmp_path, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    marker = parse_verdict_marker(gh.reviews_posted[-1]["body"])
    assert not marker["complete"] and not marker["verified"]
    assert "verify: missing identities" in gh.reviews_posted[-1]["body"]


@pytest.mark.parametrize("fault", ["unannotated", "carried", "short-count", None])
@pytest.mark.parametrize("retain_first", [True, False])
async def test_report_disposition_cannot_hide_partial_verification(tmp_path, fault, retain_first):
    native = NativeRunner(rows=[F, dict(F, claim="Second defect", line=8)])

    async def runner(recipe, inputs, *, seed_outputs=None):
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        if "finding_accounting" in inputs:
            rows = a.array(result["steps"]["verify"])
            if fault in ("unannotated", "carried"):
                rows[1].pop("verdict")
            if fault == "carried":
                rows[1]["carried"] = True
            count = 1 if fault in ("unannotated", "short-count") else 2
            result["steps"]["verify"] = output(rows) + f"\nVERIFY_STATUS: annotated n={count}"
            rejected = [
                {
                    "raw_ids": row["raw_ids"],
                    "disposition": "unsupported",
                    "reason": "Caller guard prevents the reported impact",
                    "evidence": "x.py:3 guard",
                }
                for row in (rows[1:] if retain_first else rows)
            ]
            result["output"] = output(rows[:1] if retain_first else [], rejected)
            result["steps"]["report"] = result["output"]
        return result

    gh = RoutedGH(pr_facts=facts(changed_files=6))
    d = make(tmp_path, gh=gh, runner=runner, cfg={"grounding": False})
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")
    body = gh.reviews_posted[-1]["body"]
    marker = parse_verdict_marker(body)
    assert marker["complete"] is (fault is None)
    assert marker["verified"] is (fault is None)
    if fault:
        assert "verify: incomplete survivor verification" in body
        assert "Raw finding accounting: incomplete" in body


async def test_native_empty_lanes_post_complete_coverage(tmp_path):
    gh = RoutedGH(pr_facts=facts(changed_files=6))
    d = make(tmp_path, gh=gh, runner=NativeRunner(rows=[]))
    assert await d.handle_pr_event("o/r", 1, HEAD, "opened") == "reviewed:PASS"
    marker = parse_verdict_marker(gh.reviews_posted[-1]["body"])
    assert marker["complete"] and marker["verified"]
    assert "Raw finding accounting: complete" in gh.reviews_posted[-1]["body"]


def test_malformed_accounting_payload_fails_closed_without_raising():
    for payload in ({}, {"supported": True, "raw": []}, None):
        result = a.coverage({"finding_accounting": payload}, "code-review-structural")
        assert not result["complete"] and result["gaps"]


async def test_seeded_barrier_placeholders_cannot_mask_missing_synthesis():
    native = NativeRunner()

    async def runner(recipe, inputs, *, seed_outputs=None):
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        if "finding_accounting" in inputs:
            del result["steps"]["synthesize"]
        return result

    result = await _accounting_runner(runner)("code-review-structural", {"head_sha": HEAD})
    assert result["steps"]["synthesize"] == ""
    assert not a.coverage(result, "code-review-structural")["complete"]


@pytest.mark.parametrize("budget", [0, 30, 300, 65000])
def test_large_accounting_render_respects_available_body_budget(budget):
    inv, rows = prepared([dict(F, evidence="x" * 20000)])
    accounting = check(inv, rows)
    assert accounting["complete"]
    assert len(a.render(accounting, max_chars=budget)) <= budget
    assert accounting["transitions"][0]["before"]["evidence"] == "x" * 20000


@pytest.mark.parametrize("notes_size", [0, 64000, 65000])
async def test_posting_large_accounting_preserves_postable_verdict(tmp_path, notes_size):
    inv, _rows = prepared([dict(F, evidence="x" * 20000)])
    accounting = check(
        inv,
        [],
        [{"raw_ids": list(inv["raw"]), "disposition": "refuted", "reason": "guard blocks this", "evidence": "x.py:3"}],
    )
    accounting["report_findings"] = []
    gh = RoutedGH(pr_facts=facts())
    d = make(tmp_path, gh=gh)
    assert await d._post_verdict(
        "o/r", 1, HEAD, "PASS", [], "code-review-structural", notes="n" * notes_size, accounting=accounting
    )
    body = gh.reviews_posted[-1]["body"]
    assert len(body) <= 65536
    assert "n" * notes_size in body
    assert parse_verdict_marker(body)["complete"]
    trace = next(e for e in d.telemetry.read_all() if e["event"] == "finding_accounting")
    assert trace["accounting"]["raw"] == inv["raw"]


@pytest.mark.parametrize(
    "checks",
    [[], [{"status": "completed", "conclusion": "success"}], [{"status": "completed", "conclusion": "failure"}]],
)
@pytest.mark.parametrize("remaining", [0, 100, 300])
async def test_accounting_fail_regates_within_body_limit(tmp_path, checks, remaining):
    inv, rows = prepared([dict(F, severity="major", verdict="confirmed")])
    accounting = check(inv, rows)
    accounting["report_findings"] = rows
    gh = RoutedGH(pr_facts=facts(), checks=[{"status": "in_progress", "conclusion": None}])
    d = make(tmp_path, gh=gh, cfg={"shadow_mode": False, "promotion_owner": True})
    # Size the provenance appendix to leave less than a re-gating note at the boundary.
    assert await d._post_verdict(
        "o/r", 1, HEAD, "FAIL", rows, "code-review-structural", accounting=copy.deepcopy(accounting)
    )
    initial_size = len(gh.reviews_posted[-1]["body"])
    accounting["padding"] = "x" * (65536 - remaining - initial_size - len(', "padding": ""'))
    assert await d._post_verdict("o/r", 1, HEAD, "FAIL", rows, "code-review-structural", accounting=accounting)
    initial = gh.reviews_posted[-1]
    assert initial["event"] == "COMMENT"
    assert len(initial["body"]) == 65536 - remaining
    gh.reviews = [{"state": "COMMENTED", "body": initial["body"], "id": 1}]
    assert await d.evaluate_regate("o/r", 1) == "hold:regate-checks-pending"
    gh.checks = checks
    assert await d.evaluate_regate("o/r", 1) == "regate"
    blocking = gh.reviews_posted[-1]
    assert blocking["event"] == "REQUEST_CHANGES"
    assert len(blocking["body"]) <= 65536
    assert blocking["body"].endswith(initial["body"])
    assert parse_verdict_marker(blocking["body"]) == parse_verdict_marker(initial["body"])
    assert blocking["body"].startswith("_Checks are terminal") == (remaining == 300)


@pytest.mark.parametrize("severity", ["blocker", "major", "minor", "nit"])
@pytest.mark.parametrize("verdict", [None, "confirmed", "uncertain"])
@pytest.mark.parametrize("old_ids", [None, ["old-attempt:1"]])
def test_known_prior_relisting_is_separate_but_cannot_hide_native_loss(old_ids, severity, verdict):
    prior = dict(F, severity=severity)
    if verdict is not None:
        prior["verdict"] = verdict
    if old_ids is not None:
        prior["raw_ids"] = old_ids
    history = [{"head": "old", "findings": [prior]}]
    inv, rows = prepared()
    text = output([*rows, prior])
    assert a.reconcile(inv, {s: text for s in a.STAGES}, text, history=history)["complete"]
    assert a.array(text)[-1] == prior  # matching must not strip the real verifier verdict
    assert not a.reconcile(inv, {s: text for s in a.STAGES}, text)["complete"]
    missing = output([prior])
    assert not a.reconcile(inv, {s: missing for s in a.STAGES}, missing, history=history)["complete"]
    foreign = output([*rows, dict(prior, raw_ids=["invented"])])
    assert not a.reconcile(inv, {s: foreign for s in a.STAGES}, foreign, history=history)["complete"]
    # Even a matched prior must account for any current-run IDs it claims.
    duplicate = output([*rows, dict(prior, raw_ids=rows[0]["raw_ids"], finding_id=rows[0]["finding_id"])])
    assert not a.reconcile(inv, {s: duplicate for s in a.STAGES}, duplicate, history=history)["complete"]


async def test_structural_prior_recheck_refutation_keeps_native_coverage_complete(tmp_path):
    from tests.test_dispatch import _F841_REPORT, F841_RELISTED, _f841_gh, verify_reply

    native = NativeRunner(rows=[])
    rechecked = False

    async def runner(recipe, inputs, *, seed_outputs=None):
        nonlocal rechecked
        if seed_outputs and seed_outputs.get("report") == report:
            rechecked = True
            return {
                "output": report,
                "steps": {
                    **seed_outputs,
                    "verify": verify_reply(dict(F841_RELISTED, verdict="refuted", note="now `_`")),
                },
                "failed": [],
            }
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        if "finding_accounting" in inputs:
            result["output"] = result["steps"]["report"] = report
            result["steps"]["verify"] = output([]) + "\nVERIFY_STATUS: nothing-to-verify"
        return result

    report = _F841_REPORT + '\n```accounting\n{"dispositions": []}\n```'
    gh = _f841_gh()
    gh.pr_facts.update(facts(changed_files=6))
    d = make(tmp_path, gh=gh, runner=runner, cfg={"evidence_grounding": False, "shadow_mode": False})
    assert await d.handle_pr_event("o/r", 1, HEAD, "synchronize") == "reviewed:PASS"
    assert rechecked
    body = gh.reviews_posted[-1]["body"]
    marker = parse_verdict_marker(body)
    assert marker["complete"] and marker["verified"]
    assert "cleared by re-verification" in body


@pytest.mark.parametrize("fault", [None, "finder-context", "reused-execution"])
async def test_accounting_barrier_validates_each_physical_execution(tmp_path, fault):
    from tests.runner_fixtures import native_runner

    native = native_runner(NativeRunner())
    physical_results = []

    async def runner(recipe, inputs, *, seed_outputs=None, seed_run_id=None):
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        if not physical_results:
            if fault == "finder-context":
                result["review_context"]["steps"][a.LANES[0]]["parent_history"] = "supplied"
        else:
            first = physical_results[0]
            assert seed_run_id == first["run_id"]
            # The host links the seeded lanes to the actual first invocation,
            # without inventing observations for them in the second execution.
            result["review_context"].pop("previous_executions", None)
            result["tool_receipts"].pop("previous_executions", None)
            for lane in a.LANES:
                original = first["review_context"]["steps"][lane]
                result["review_context"]["steps"][lane]["origin"] = {
                    k: original[k] for k in ("run_id", "step_id", "execution_id")
                }
            if fault == "reused-execution":
                eid = first["tool_receipts"]["execution_id"]
                for key in ("review_context", "tool_receipts"):
                    result[key]["execution_id"] = eid
                    for step in result[key]["steps"].values():
                        step["execution_id"] = eid
        physical_results.append(copy.deepcopy(result))
        return result

    d = make(tmp_path)
    wrapped = _accounting_runner(runner, invoke=d._invoke_runner)
    result = await d._invoke_runner(wrapped, "code-review-structural", {"head_sha": HEAD})
    assert len(physical_results) == 2
    assert a.coverage(result, "code-review-structural")["complete"]
    assert bool(result["isolation_gaps"]) is bool(fault)
    if fault == "reused-execution":
        assert "stale execution" in result["isolation_gaps"]["execution"]
    assert result["context_adoptions"][a.LANES[0]]["run_id"] == physical_results[0]["run_id"]


def test_combined_accounting_and_context_coverage_notes():
    from pr_reviewer.verdicts import render_coverage_note

    note = render_coverage_note({"finding_accounting": "missing ID", "context:verify": "unknown"})
    assert "Raw finding accounting has gaps" in note
    assert "Runner independence unverified" in note


async def test_dispatcher_preserves_both_accounting_and_context_holds(tmp_path):
    from tests.runner_fixtures import native_runner

    native = native_runner(NativeRunner("report"))

    async def runner(recipe, inputs, *, seed_outputs=None):
        result = await native(recipe, inputs, seed_outputs=seed_outputs)
        if "finding_accounting" in inputs:
            result["review_context"]["steps"]["verify"]["execution_id"] = "foreign"
        return result

    gh = RoutedGH(pr_facts=facts(changed_files=6, additions=150))
    dispatcher = make(tmp_path, gh=gh, runner=runner, native_context=False)
    assert await dispatcher.handle_pr_event("o/r", 1, HEAD, "opened") == "reviewed:WARN"
    body = gh.reviews_posted[-1]["body"]
    marker = parse_verdict_marker(body)
    assert marker["complete"] is False
    assert marker["verified"] is False
    assert "Raw finding accounting has gaps" in body
    assert "Runner independence unverified" in body


@pytest.mark.parametrize("fault", [None, "changed-output", "foreign-origin", "missing-receipt"])
def test_seeded_retry_uses_retained_finder_adoption(fault):
    from pr_reviewer.runner_contract import validate_result

    from tests.test_runner_contract_327 import result, seed_result

    original = result()
    composite = result()
    composite["context_adoptions"] = {
        "find_bugs": {k: copy.deepcopy(original[k]) for k in ("run_id", "review_context", "tool_receipts")}
    }
    retry = seed_result(original)
    if fault == "changed-output":
        composite["steps"]["find_bugs"] = "changed"
    elif fault == "foreign-origin":
        retry["review_context"]["steps"]["find_bugs"]["origin"]["execution_id"] = "foreign"
    elif fault == "missing-receipt":
        composite["context_adoptions"]["find_bugs"]["tool_receipts"] = {}
    gaps = validate_result(retry, seed_source=composite)
    assert bool(gaps) is bool(fault)

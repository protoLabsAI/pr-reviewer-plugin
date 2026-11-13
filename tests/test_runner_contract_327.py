"""Consumer behavior using #5091-shaped fixtures; no host imports or probe claims."""

from copy import deepcopy

import pytest
from pr_reviewer.runner_contract import context_note, validate_result
from pr_reviewer.verdicts import parse_verdict_marker

from tests.dispatch_helpers import HEAD, RoutedGH, facts, make
from tests.runner_fixtures import native_runner, observed_result
from tests.test_dispatch import _clean_runner


def result():
    return observed_result({"steps": {"find_bugs": "findings", "verify": "verified", "report": "report"}})


@pytest.mark.parametrize("state", ["fresh", "resumed"])
def test_shipped_observed_context_without_source_is_accepted(state):
    value = result()
    for env in value["review_context"]["steps"].values():
        env["workflow_state"] = state
        assert "source" not in env
    before = deepcopy(value)
    assert validate_result(value) == {}
    assert value == before
    note = context_note({})
    for phrase in (
        "prior findings",
        "mandatory repository guidance",
        "runtime additions remain unknown",
        "external policy remains unknown",
    ):
        assert phrase in note


@pytest.mark.parametrize("status", ["unknown", "not_executed", None, "observed-from-text"])
def test_unobserved_is_a_gap(status):
    value = result()
    value["review_context"]["steps"]["verify"]["status"] = status
    assert "verify" in validate_result(value)


@pytest.mark.parametrize("field", ["run_id", "step_id", "execution_id"])
@pytest.mark.parametrize("section", ["review_context", "tool_receipts"])
def test_foreign_or_stale_step_identity(field, section):
    value = result()
    value[section]["steps"]["verify"][field] = "foreign"
    assert "verify" in validate_result(value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("conversation", "shared"),
        ("parent_history", "supplied"),
        ("workflow_state", "seeded"),
        ("schema_version", True),
        ("inputs", {"history": "not_supplied", "prompt": "a prompt"}),
        ("instruction_sources", "unknown"),
        ("tool_restrictions", "unknown"),
    ],
)
def test_contradictory_or_incomplete_construction(field, value):
    data = result()
    data["review_context"]["steps"]["verify"][field] = value
    assert "verify" in validate_result(data)


@pytest.mark.parametrize("value", [None, {}, [], "model claims isolation", {"schema_version": 1, "status": "unknown"}])
def test_missing_malformed_and_legacy_context_degrade(value):
    data = result()
    data["review_context"] = value
    assert validate_result(data)


def seed_result(source):
    data = result()
    old = source["review_context"]["steps"]["find_bugs"]
    env = data["review_context"]["steps"]["find_bugs"]
    env.update(
        status="not_executed",
        workflow_state="seeded",
        conversation="unknown",
        parent_history="unknown",
        inputs="unknown",
        instruction_sources="unknown",
        tool_restrictions="unknown",
        origin={k: old[k] for k in ("run_id", "step_id", "execution_id")},
    )
    data["tool_receipts"]["steps"]["find_bugs"]["capture_status"] = "not_executed"
    return data


def test_seed_reference_needs_matching_original_observation_and_output():
    source = result()
    data = seed_result(source)
    assert validate_result(data, seed_source=source) == {}
    assert "find_bugs" in validate_result(data)  # a reference alone proves nothing
    source["steps"]["find_bugs"] = "different output"
    assert "find_bugs" in validate_result(data, seed_source=source)


@pytest.mark.parametrize("origin", ["unknown", {}, None, {"step_id": "other"}])
def test_unknown_seed_origins_preserved(origin):
    source = result()
    data = seed_result(source)
    data["review_context"]["steps"]["find_bugs"]["origin"] = origin
    assert "find_bugs" in validate_result(data, seed_source=source)


def test_resume_retained_originals_and_eviction():
    source = result()
    data = seed_result(source)
    data["run_id"] = source["run_id"]
    for section in ("review_context", "tool_receipts"):
        for env in data[section]["steps"].values():
            env["run_id"] = source["run_id"]
        data[section]["previous_executions"] = [source[section]]
    assert validate_result(data) == {}
    data["tool_receipts"]["previous_executions"] = []
    assert "find_bugs" in validate_result(data)


async def test_legacy_runner_executes_and_posts_incomplete_unverified_gap(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    calls = []

    async def legacy(recipe, inputs):
        calls.append((recipe, inputs))
        return await _clean_runner(recipe, inputs)

    dispatcher = make(tmp_path, runner=legacy, gh=gh, native_context=False)
    assert await dispatcher.handle_pr_event("o/r", 1, HEAD, "opened") == "reviewed:WARN"
    assert len(calls) == 1
    body = gh.reviews_posted[-1]["body"]
    assert "Gap: isolation" in body
    marker = parse_verdict_marker(body)
    assert marker["complete"] is False
    assert marker["verified"] is False
    assert "external policy remains unknown" in body


async def test_observed_runner_posts_complete_and_verified(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    dispatcher = make(tmp_path, runner=native_runner(_clean_runner), gh=gh, native_context=False)
    assert await dispatcher.handle_pr_event("o/r", 1, HEAD, "opened") == "reviewed:PASS"
    marker = parse_verdict_marker(gh.reviews_posted[-1]["body"])
    assert marker["complete"] is True
    assert marker["verified"] is True


async def test_reused_execution_cannot_be_rescued_by_shared_session(tmp_path):
    data = result()
    data["session_id"] = "pr-review:shared"

    async def runner(recipe, inputs):
        return deepcopy(data)

    dispatcher = make(tmp_path, native_context=False)
    first = await dispatcher._invoke_runner(runner, "review", {})
    second = await dispatcher._invoke_runner(runner, "review", {})
    assert first["isolation_gaps"] == {}
    assert "execution" in second["isolation_gaps"]


async def test_lazy_host_binding_uses_same_validation(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    async def legacy(recipe, inputs):
        return {"steps": {"verify": "claims independence"}}

    dispatcher = make(tmp_path, native_context=False)
    dispatcher._workflow_run = None
    monkeypatch.setitem(sys.modules, "runtime.state", SimpleNamespace(STATE=SimpleNamespace(workflow_run=legacy)))
    # The host import path is checked separately from injected test runners.
    bound = dispatcher._runner()
    assert bound is legacy
    assert (await dispatcher._invoke_runner(bound, "review", {}))["isolation_gaps"]


async def test_seed_source_kwarg_is_only_sent_when_supported(tmp_path):
    source = result()
    calls = []

    async def modern(recipe, inputs, *, seed_outputs, seed_run_id):
        calls.append(seed_run_id)
        return seed_result(source)

    dispatcher = make(tmp_path, native_context=False)
    answer = await dispatcher._invoke_runner(
        modern, "review", {}, seed_outputs={"find_bugs": "findings"}, seed_source=source
    )
    assert calls == [source["run_id"]]
    assert not answer["isolation_gaps"]

    async def legacy(recipe, inputs, *, seed_outputs):
        return seed_result(source)

    answer = await dispatcher._invoke_runner(
        legacy, "review", {}, seed_outputs={"find_bugs": "findings"}, seed_source=source
    )
    assert not answer["isolation_gaps"]


async def test_seeded_verify_report_adoption_retains_identity_gap(tmp_path):
    from tests.test_dispatch import (
        _REPORT_UNVERIFIED,
        _REPORT_VERIFIED,
        _SYNTH_ONE,
        _VERIFY_ANNOTATED,
        _VERIFY_FLAKED,
        _panel_steps,
    )

    initial = observed_result(
        {
            "output": _REPORT_UNVERIFIED,
            "steps": _panel_steps(synthesize=_SYNTH_ONE, verify=_VERIFY_FLAKED),
            "failed": [],
        }
    )

    async def runner(recipe, inputs, *, seed_outputs=None):
        if seed_outputs is None:
            return initial
        data = observed_result(
            {
                "output": _REPORT_VERIFIED,
                "steps": {"verify": _VERIFY_ANNOTATED, "report": _REPORT_VERIFIED},
                "failed": [],
            }
        )
        data["review_context"]["steps"]["report"]["execution_id"] = "stale"
        return data

    dispatcher = make(tmp_path, native_context=False)
    initial = await dispatcher._invoke_runner(runner, "review", {})
    adopted = await dispatcher._rerun_contradicted_verify(runner, "review", {}, initial, repo="o/r", pr=1, head=HEAD)
    assert adopted["output"] == _REPORT_VERIFIED
    assert "report" in adopted["isolation_gaps"]
    # Omitted seeded envelopes cannot hide unknown construction origins.
    assert "synthesize" in adopted["isolation_gaps"]


def test_context_gap_is_not_reported_as_an_unexecuted_finder_lane():
    from pr_reviewer.verdicts import render_coverage_note, verdict_for

    note = render_coverage_note({"context:verify": "Gap: isolation — unknown"}, lanes=5)
    assert "independence unverified" in note
    assert "review lane(s)" not in note
    assert verdict_for([]) == "PASS"  # history and isolation remain outside the pure mapping


@pytest.mark.parametrize("field", ["bound_tools", "inherited_fence", "enforcement"])
def test_tool_restrictions_unknown_do_not_erase_parent_history_observation(field):
    data = result()
    env = data["review_context"]["steps"]["verify"]
    env["tool_restrictions"][field] = "unknown"
    assert "tool restrictions" in validate_result(data)["verify"]
    assert env["parent_history"] == "not_supplied"


def test_composite_adopted_output_is_not_attributed_to_old_construction():
    source = result()
    source["context_adoptions"] = {"find_bugs": {"run_id": "newer-run"}}
    assert "find_bugs" in validate_result(seed_result(source), seed_source=source)


async def test_prior_recheck_with_foreign_verify_preserves_prior(tmp_path):
    from tests.dispatch_helpers import verify_reply

    prior = {
        "file": "x.py",
        "line": 3,
        "severity": "major",
        "category": "correctness",
        "claim": "Bug",
        "evidence": "e",
        "verdict": "confirmed",
    }

    async def runner(recipe, inputs, *, seed_outputs):
        data = observed_result({"steps": {**seed_outputs, "verify": verify_reply({**prior, "verdict": "refuted"})}})
        data["review_context"]["steps"]["verify"]["run_id"] = "foreign"
        return data

    dispatcher = make(tmp_path, native_context=False)
    gaps = {}
    reported, unaccounted, cleared, deferred = await dispatcher._recheck_priors(
        runner,
        "review",
        {},
        {"synthesize": "prior", "verify": "old"},
        "report",
        [],
        [],
        [prior],
        ranges={"x.py": [(1, 10)]},
        since_ranges={"x.py": [(1, 10)]},
        repo="o/r",
        pr=1,
        head=HEAD,
        round_number=2,
        isolation_gaps=gaps,
    )
    assert unaccounted == [prior]
    assert not reported and not cleared and not deferred
    assert "prior-recheck:verify" in gaps


@pytest.mark.parametrize(
    "section,field,value",
    [
        (None, "run_id", "foreign"),
        ("review_context", "execution_id", "stale"),
        ("tool_receipts", "execution_id", "stale"),
        ("tool_receipts", "schema_version", True),
    ],
)
def test_top_level_invocation_identity_and_schema(section, field, value):
    data = result()
    target = data if section is None else data[section]
    target[field] = value
    assert validate_result(data)


def test_check_summary_names_context_coverage_without_claiming_a_missing_lane():
    from pr_reviewer.dispatch import Dispatcher

    summary = Dispatcher._review_check_summary("WARN", [], {"context:verify": "Gap: isolation"})
    assert "independence is unverified" in summary
    assert "did not complete" not in summary


# ── dispatcher-authored seeds (mythxengine#953 r3, 2026-11-13) ─────────────────────────────────
# The prior re-check and the verify re-run seed `synthesize` with text the dispatcher writes
# (`restate_findings`). No run produced it, so its origin always reads unknown, and that one gap
# marked a live round "Runner independence unverified".


def _authored_seed(source):
    data = seed_result(source)
    data["review_context"]["steps"]["find_bugs"]["origin"] = "unknown"  # code-built: no source run
    return data


async def test_a_seed_the_dispatcher_wrote_is_not_an_isolation_gap(tmp_path):
    source = result()

    async def host(recipe, inputs, *, seed_outputs, seed_run_id):
        return _authored_seed(source)

    dispatcher = make(tmp_path, native_context=False)
    kwargs = dict(seed_outputs={"find_bugs": "restated"}, seed_source=source)
    assert "find_bugs" in (await dispatcher._invoke_runner(host, "review", {}, **kwargs))["isolation_gaps"]
    authored = await dispatcher._invoke_runner(host, "review", {}, authored={"find_bugs"}, **kwargs)
    assert authored["isolation_gaps"] == {}


async def test_authorship_waives_only_the_seed_origin_gap_of_that_step(tmp_path):
    source = result()

    async def host(recipe, inputs, *, seed_outputs, seed_run_id):
        data = _authored_seed(source)
        data["review_context"]["steps"]["verify"]["status"] = "unknown"  # the step that RAN is unobserved
        return data

    dispatcher = make(tmp_path, native_context=False)
    gaps = (
        await dispatcher._invoke_runner(
            host, "review", {}, seed_outputs={"find_bugs": "restated"}, seed_source=source, authored={"find_bugs"}
        )
    )["isolation_gaps"]
    assert "find_bugs" not in gaps and "verify" in gaps


async def test_the_accounting_wrapper_never_hands_authored_to_the_host_runner(tmp_path):
    from pr_reviewer.dispatch import _accounting_runner

    seen = {}

    async def host(recipe, inputs, **kwargs):
        seen.update(kwargs)
        return {"steps": {}, "output": ""}

    dispatcher = make(tmp_path, native_context=False)
    wrapped = _accounting_runner(host, invoke=dispatcher._invoke_runner)
    await dispatcher._invoke_runner(wrapped, "code-review", {}, authored={"synthesize"})
    assert "authored" not in seen

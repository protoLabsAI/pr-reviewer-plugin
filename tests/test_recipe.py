"""The code-review-structural recipe — structural assertions (the host engine
validates it fully at load; these pin the panel shape host-free)."""

from __future__ import annotations

from pathlib import Path

import yaml

RECIPE = yaml.safe_load(
    (Path(__file__).resolve().parent.parent / "workflows" / "code-review-structural.yaml").read_text()
)

STEPS = {s["id"]: s for s in RECIPE["steps"]}


def test_recipe_shape():
    assert RECIPE["name"] == "code-review-structural"
    assert {i["name"] for i in RECIPE["inputs"]} == {
        "pr",
        "repo",
        "prior_findings",
        "prior_requests",
        "review_round",
        "head_sha",
        "base_ref",
        "existing_threads",
    }
    assert RECIPE["output"] == "{{steps.report.output}}"


def test_llm_finders_see_existing_threads_and_ci_enforcement():
    for sid in ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions"):
        assert "{{inputs.existing_threads}}" in STEPS[sid]["prompt"], sid
    assert "check test enforcement specifically" in STEPS["find_conventions"]["prompt"]


def test_llm_finders_get_server_resolved_refs_and_wrapped_prior_findings():
    # The dispatcher resolves head/base server-side; finders pin code reads to the
    # head SHA and policy-doc reads to the base ref. Prior findings ride inside an
    # explicit data wrapper — recalled review text is re-evidenced, never obeyed.
    for sid in ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions"):
        prompt = STEPS[sid]["prompt"]
        assert "{{inputs.head_sha}}" in prompt and "{{inputs.base_ref}}" in prompt, sid
        assert "<prior_findings>" in prompt and "</prior_findings>" in prompt, sid
    assert "BASE ref" in STEPS["find_conventions"]["prompt"]
    assert "{{inputs.head_sha}}" in STEPS["verify"]["prompt"]


def test_finders_and_verifier_carry_the_panels_own_request_history():
    # Issue #23: a re-review must be able to tell "the panel asked for this" from
    # "this appeared unexplained" — otherwise round N re-litigates round N-3's demand.
    for sid in ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions", "verify"):
        prompt = STEPS[sid]["prompt"]
        assert "<prior_requests>" in prompt and "</prior_requests>" in prompt, sid
        assert "{{inputs.prior_requests}}" in prompt, sid
        assert "{{inputs.review_round}}" in prompt, sid
    # The relief is one-directional: a badly-implemented request is still a finding.
    assert "implemented CORRECTLY" in STEPS["find_correctness"]["prompt"]
    assert "REFUTED" in STEPS["verify"]["prompt"]


def test_five_finders_feed_the_synthesizer():
    finders = [sid for sid, s in STEPS.items() if sid.startswith("find_")]
    assert len(finders) == 5
    assert set(STEPS["synthesize"]["depends_on"]) == set(finders)
    for sid in finders:
        assert f"{{{{steps.{sid}.output}}}}" in STEPS["synthesize"]["prompt"]


def test_structural_seat_uses_the_plugin_subagent():
    step = STEPS["find_structural"]
    assert step["subagent"] == "structural-finder"
    assert "protopatch_review" in step["prompt"]
    # The four LLM lanes stay on the core role.
    for sid in ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions"):
        assert STEPS[sid]["subagent"] == "review-finder"
        assert "{{inputs.prior_findings}}" in STEPS[sid]["prompt"]


def test_verify_then_report_chain_preserves_source():
    assert STEPS["verify"]["depends_on"] == ["synthesize"]
    assert STEPS["report"]["depends_on"] == ["verify"]
    assert "`source`" in STEPS["verify"]["prompt"]


def test_verifier_must_derive_its_verdict_from_a_read_not_a_story():
    # Issue #25: the verify pass twice CONFIRMED code that wasn't in the file — once
    # with the refuting blob and a passing test already on the PR. The prompt half of
    # the fix; grounding.py is the deterministic half.
    p = STEPS["verify"]["prompt"]
    assert "github_read_file" in p and "{{inputs.head_sha}}" in p
    assert "REFUTED" in p and "uncertain" in p
    assert "startswith" in p  # the decidable-predicate rule carries its real example
    assert "Evidence already on the PR counts" in p


def test_report_pass_must_disposition_every_prior_blocker_or_major():
    # Issue #26: a confirmed major must not simply stop being mentioned.
    p = STEPS["report"]["prompt"]
    assert "prior_dispositions" in p or "dispositions" in p
    assert '"fixed"' in p or "`fixed`" in p
    assert "not a disposition" in p  # "I didn't see it this time" is explicitly excluded
    assert "{{inputs.prior_requests}}" in p


def test_the_panel_declares_its_own_fan_out_width():
    # Five finders under the caller's default cap of 4 ran as 4+1 — two waves, paying
    # the slowest finder twice (~136s of the measured p50). Needs protoAgent's
    # recipe-declared width; an older host ignores the key.
    finders = [sid for sid in STEPS if sid.startswith("find_")]
    assert RECIPE["max_concurrency"] == len(finders) == 5

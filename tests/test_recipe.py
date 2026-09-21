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
        "finder_timeout",
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


def test_a_failed_read_must_not_downgrade_a_finding():
    """protoAgent#2296 / issue #109: on a PR rebased mid-review the verifier hit a
    file-fetch 404, could not re-read the blob, and honestly marked still-present majors
    `uncertain` — which de-escalated them (major→minor) and dropped the verdict, lifting
    the gate on two real defects that were byte-for-byte still in the file.

    The prior prompt conflated 'I read the file and the quote wasn't there' (a weak
    finding) with 'I could not read the file' (learning nothing). Only the first earns
    a downgrade."""
    p = STEPS["verify"]["prompt"]
    assert "A failed READ is not evidence" in p
    # It must say what to do: hold the finding as-is, and name the distinct state.
    assert "Leave the finding exactly as it was" in p
    assert "de-escalate" in p
    assert "source unavailable" in p.lower()  # its own state — not refuted, not uncertain
    # Fail closed on head movement (r6): NEVER re-read the branch tip and attribute that
    # read to this head — a rebase/force-push makes the tip a different head.
    assert "Do NOT re-read the file at the branch tip" in p
    assert "Retry without a ref" not in p  # the old movable-ref retry is gone
    # Stale anchors are the same failure wearing a different hat (the issue's line
    # 176/287-vs-257/259/376 observation).
    assert "line number" in p and "re-anchor" in p


def test_verifier_pins_head_reads_and_names_source_unavailable_as_its_own_state():
    """r5/r6: the verifier reads PINNED to the immutable head SHA and reports an
    unreadable source as its own disposition, never as refuted or uncertain-on-merits."""
    p = STEPS["verify"]["prompt"]
    assert 'PINNED to head SHA "{{inputs.head_sha}}"' in p
    assert "confirmed nor refuted on its merits" in p
    assert "not evidence about this one" in p  # a read of another ref proves nothing here


def test_the_report_cannot_claim_coverage_it_cannot_see():
    """The report step sees only the merged findings, never the lanes — yet it was told an
    empty verify pass is "not a Gap" and to not mention a skipped structural pass, and on
    protoAgent#3494 it duly wrote "no coverage gaps" over four blind lanes (#117). And an
    ABSENT synthesized array must not be written up as clean (#113)."""
    prompt = STEPS["report"]["prompt"]
    assert "You cannot see the finder lanes" in prompt
    assert "never claim" in prompt and "no coverage" in prompt
    assert "NO findings array at all" in prompt
    assert "do not mention a skipped or empty" not in prompt


def test_the_status_line_contract_lives_where_the_dispatcher_expects_it():
    """STATUS_LINE_RECIPES is what the dispatcher uses to decide whether a missing
    FINDER_STATUS line is a gap. It must name this recipe, and this recipe must ask
    every LLM finder for the line — else a healthy finder reads as incomplete."""
    from pr_reviewer.dispatch import LLM_FINDER_STEPS, STATUS_LINE_RECIPES

    assert RECIPE["name"] in STATUS_LINE_RECIPES
    for sid in LLM_FINDER_STEPS:
        assert "FINDER_STATUS: reviewed" in STEPS[sid]["prompt"], sid


def test_the_tool_heavy_lanes_get_a_bounded_evidence_reminder():
    """pr-reviewer-plugin#124: crossfile and conventions are the only two lanes whose
    angle REQUIRES extra github_read_file calls beyond the initial diff fetch — and
    live telemetry showed they (never the tool-light correctness/removed_behavior
    lanes) are the ones that intermittently finish without their closing
    FINDER_STATUS line. The other two just reason over the given diff in one shot;
    these two run open-ended tool loops, so the closing-line instruction is furthest
    (in turns) from wherever it was last seen. A reminder placed right next to the
    tool-use instruction — not just at the very end of a long prompt — is the fix
    tried here; it must survive prompt edits."""
    for sid in ("find_crossfile", "find_conventions"):
        prompt = STEPS[sid]["prompt"]
        assert "bounded, honest pass beats an exhaustive one" in prompt, sid
        # The reminder must sit with the tool-use instruction, not only at the tail —
        # find it before the last quarter of the prompt.
        idx = prompt.index("bounded, honest pass beats an exhaustive one")
        assert idx < len(prompt) * 0.75, f"{sid}: reminder too close to the tail"
    # The tool-light lanes reason over the given diff directly — no such reminder needed.
    for sid in ("find_correctness", "find_removed_behavior"):
        assert "bounded, honest pass beats an exhaustive one" not in STEPS[sid]["prompt"], sid


def test_the_finder_budget_is_an_input_with_the_calibrated_default():
    # Issue #93: a recipe constant calibrated on one model silently truncates productive
    # finders on a slower one. Every parallel finder takes its budget from one input, whose
    # default keeps today's 900s for a dispatcher that passes nothing.
    declared = {i["name"]: i for i in RECIPE["inputs"]}
    assert declared["finder_timeout"]["default"] == 900
    finders = [s for s in RECIPE["steps"] if s["id"].startswith("find_")]
    assert len(finders) == 5
    assert {s["timeout"] for s in finders} == {"{{inputs.finder_timeout}}"}
    assert all("timeout" not in s for s in RECIPE["steps"] if not s["id"].startswith("find_"))


def test_the_manifest_requires_a_core_that_accepts_an_input_timeout():
    # An older engine REJECTS a string `timeout` at validation: the recipe would not load.
    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "protoagent.plugin.yaml").read_text())
    have = tuple(int(x) for x in str(manifest["min_protoagent_version"]).split("."))
    assert have >= (0, 170, 0)
    assert manifest["config"]["finder_timeout_s"] == 0


def test_a_404_on_a_guessed_path_is_not_a_reason_to_declare_blocked():
    # Seen live on two protoAgent release PRs: the cross-file lane went looking for a
    # `__version__` module that does not exist, got a 404, and declared the whole repo
    # inaccessible — `FINDER_STATUS: blocked` — voiding an otherwise complete round. The
    # status-line instructions used "file reads 404ing" as their example of being blocked.
    llm = [s for s in RECIPE["steps"] if s["id"].startswith("find_") and s["id"] != "find_structural"]
    assert len(llm) == 4
    for step in llm:
        prompt = step["prompt"]
        assert "A 404 on a path you GUESSED is not a blocker" in prompt, step["id"]
        assert "files the diff" in prompt and "itself names" in prompt, step["id"]
        assert '"file reads\n        404ing"' not in prompt and '"file reads 404ing"' not in prompt, step["id"]

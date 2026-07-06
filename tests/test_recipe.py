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
    assert {i["name"] for i in RECIPE["inputs"]} == {"pr", "repo", "prior_findings"}
    assert RECIPE["output"] == "{{steps.report.output}}"


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

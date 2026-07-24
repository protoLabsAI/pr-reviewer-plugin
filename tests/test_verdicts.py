"""Verdict mapping (pure) + the posted-body marker round-trip."""

from __future__ import annotations

import json

from pr_reviewer.verdicts import (
    FAIL,
    PASS,
    WARN,
    confine_findings,
    extract_findings_json,
    parse_verdict_marker,
    render_verdict_body,
    verdict_for,
)


def f(severity, verdict=""):
    return {"file": "a.py", "line": 1, "severity": severity, "claim": "x", "verdict": verdict}


def test_verdict_mapping_matrix():
    assert verdict_for([]) == PASS
    assert verdict_for([f("nit")]) == PASS
    assert verdict_for([f("minor")]) == WARN
    assert verdict_for([f("major", "uncertain")]) == WARN  # unproven major gates nothing
    assert verdict_for([f("major", "confirmed")]) == FAIL
    assert verdict_for([f("major")]) == FAIL  # no verify annotation — trust the panel
    assert verdict_for([f("blocker", "confirmed"), f("nit")]) == FAIL
    assert verdict_for([f("minor"), f("major", "uncertain")]) == WARN


def test_body_marker_roundtrip():
    body = render_verdict_body(
        repo="o/r",
        pr=7,
        head_sha="a" * 40,
        verdict=WARN,
        report='Prose brief.\n\n```json\n[{"file": "a.py", "line": 1, "severity": "minor", "claim": "x", "evidence": "e"}]\n```',
        shadow=True,
        recipe="code-review",
    )
    marker = parse_verdict_marker(body)
    assert marker == {"head": "a" * 40, "verdict": WARN, "promoted": False}
    assert "shadow" in body and "QA panel review" in body


def test_marker_ignores_foreign_comments():
    assert parse_verdict_marker("Just a normal review comment") is None
    assert parse_verdict_marker("<!-- coderabbit summary -->") is None


def test_confine_findings_drops_out_of_diff_and_unanchored():
    in_diff = {"file": "a.py", "line": 1, "severity": "major", "claim": "real"}
    dotted = {"file": "./b.py", "line": 2, "severity": "minor", "claim": "normalized"}
    outside = {"file": "untouched.py", "line": 9, "severity": "blocker", "claim": "laundered"}
    unanchored = {"file": "", "line": 0, "severity": "blocker", "claim": "gap dressed as finding"}
    kept, dropped = confine_findings([in_diff, dotted, outside, unanchored], ["a.py", "b.py"])
    assert kept == [in_diff, dotted]
    assert dropped == [outside, unanchored]
    # The whole point: the out-of-diff blocker can no longer gate the merge.
    assert verdict_for(kept) == FAIL and verdict_for([outside]) == FAIL


def test_confine_findings_fails_open_on_unreadable_file_list():
    # An empty changed-path list means the GitHub read failed — dropping everything
    # would launder a FAIL into a PASS, so confinement must stand down instead.
    finding = {"file": "a.py", "line": 1, "severity": "major", "claim": "x"}
    kept, dropped = confine_findings([finding], [])
    assert kept == [finding] and dropped == []


def test_confinement_footnote_rides_the_body_without_breaking_recall():
    report = 'prose\n```json\n[{"file": "a.py", "line": 1, "severity": "minor", "claim": "kept"}]\n```'
    body = render_verdict_body(
        repo="o/r",
        pr=7,
        head_sha="a" * 40,
        verdict=PASS,
        report=report,
        shadow=True,
        recipe="code-review",
        confined=[{"file": "untouched.py", "severity": "blocker", "claim": "dropped one"}],
    )
    assert "in-diff confinement" in body and "untouched.py" in body
    # The footnote adds no fenced JSON — prior-findings recall still sees the report's array.
    assert "kept" in extract_findings_json(body)
    assert parse_verdict_marker(body)["verdict"] == PASS


def test_extract_findings_json_takes_the_final_array_block():
    body = (
        'brief\n```json\n{"not": "an array"}\n```\n'
        'mid\n```json\n[{"claim": "old"}]\n```\n'
        'final\n```json\n[{"claim": "newest"}]\n```\n'
    )
    assert "newest" in extract_findings_json(body)
    assert extract_findings_json("no blocks here") == ""


# ── the marker must survive attributes added later ───────────────────────────


def test_a_marker_with_trailing_attributes_still_parses():
    # PRODUCTION REGRESSION (2026-07-23): v0.13.0 appended `findings=N` after
    # `promoted=true`. The regex required `-->` immediately after `promoted`, so the
    # marker stopped parsing — the promotion was no longer recognised as ours,
    # `already-promoted` never fired, and approve-on-green re-approved the same head
    # every sweep tick. 20+ duplicate APPROVE reviews before it was caught.
    body = "<!-- protoagent-qa-review head=abc1234 verdict=WARN promoted=true findings=1 -->\nPromoting..."
    m = parse_verdict_marker(body)
    assert m == {"head": "abc1234", "verdict": "WARN", "promoted": True}


def test_unknown_future_attributes_do_not_break_the_marker():
    body = "<!-- protoagent-qa-review head=abc1234 verdict=PASS promoted=false findings=0 mode=shadow x=1 -->"
    m = parse_verdict_marker(body)
    assert m and m["head"] == "abc1234" and m["verdict"] == "PASS" and m["promoted"] is False


def test_the_plain_marker_forms_still_parse():
    assert parse_verdict_marker("<!-- protoagent-qa-review head=abc1234 verdict=FAIL -->")["verdict"] == "FAIL"
    assert parse_verdict_marker("<!-- protoagent-qa-review head=abc1234 verdict=PASS promoted=true -->")["promoted"]
    assert parse_verdict_marker("not ours") is None


# ── findings render as a table, JSON preserved for recall ────────────────────


_FINDINGS = [
    {"file": "a.py", "line": 3, "severity": "major", "claim": "sync call blocks the loop", "verdict": "confirmed"},
    {"file": "b.py", "line": 0, "severity": "minor", "claim": "nested ternary | hard to read", "verdict": "uncertain"},
]
_REPORT = "## Brief\n\nOverall risk: medium.\n\n```json\n" + json.dumps(_FINDINGS) + "\n```"


def test_findings_render_as_a_table():
    body = render_verdict_body(
        repo="o/r", pr=1, head_sha="a" * 40, verdict="FAIL", report=_REPORT, shadow=False, recipe="code-review"
    )
    assert "### Findings" in body
    assert "| Severity | Location | Finding | Verified |" in body
    assert "`a.py:3`" in body and "`b.py`" in body  # line 0 → no :line
    assert "🟠" in body and "🟡" in body
    assert "⚠️ uncertain" in body


def test_the_pipe_in_a_claim_does_not_break_the_table():
    body = render_verdict_body(
        repo="o/r", pr=1, head_sha="a" * 40, verdict="FAIL", report=_REPORT, shadow=False, recipe="code-review"
    )
    assert "nested ternary \\| hard to read" in body  # escaped, not a column break


def test_the_raw_json_is_still_present_and_recallable():
    # THE critical property: reflowing to a table must not break prior-round recall,
    # which reads the findings JSON back out of the posted body.
    body = render_verdict_body(
        repo="o/r", pr=1, head_sha="a" * 40, verdict="FAIL", report=_REPORT, shadow=False, recipe="code-review"
    )
    assert "<details>" in body  # collapsed, not deleted
    recalled = extract_findings_json(body)
    assert json.loads(recalled) == _FINDINGS  # round-trips exactly


def test_a_clean_pass_is_left_untouched():
    clean = "Overall risk: low.\n\n```json\n[]\n```"
    body = render_verdict_body(
        repo="o/r", pr=1, head_sha="a" * 40, verdict="PASS", report=clean, shadow=False, recipe="code-review"
    )
    assert "### Findings" not in body and "<details>" not in body
    assert "```json\n[]\n```" in body  # the empty array stays as-is for recall


def test_prose_only_report_is_unchanged():
    prose = "PROTOPATCH UNAVAILABLE\n\nGap: the structural engine timed out."
    body = render_verdict_body(
        repo="o/r", pr=1, head_sha="a" * 40, verdict="PASS", report=prose, shadow=False, recipe="code-review"
    )
    assert "Gap: the structural engine timed out." in body and "### Findings" not in body

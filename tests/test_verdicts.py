"""Verdict mapping (pure) + the posted-body marker round-trip."""

from __future__ import annotations

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

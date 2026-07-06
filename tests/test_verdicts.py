"""Verdict mapping (pure) + the posted-body marker round-trip."""

from __future__ import annotations

from pr_reviewer.verdicts import (
    FAIL,
    PASS,
    WARN,
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


def test_extract_findings_json_takes_the_final_array_block():
    body = (
        'brief\n```json\n{"not": "an array"}\n```\n'
        'mid\n```json\n[{"claim": "old"}]\n```\n'
        'final\n```json\n[{"claim": "newest"}]\n```\n'
    )
    assert "newest" in extract_findings_json(body)
    assert extract_findings_json("no blocks here") == ""

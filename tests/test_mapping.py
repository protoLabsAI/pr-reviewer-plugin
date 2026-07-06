"""protoPatch FindingRecord → ADR 0077 finding mapping, and the state-dir read."""

from __future__ import annotations

import json

from pr_reviewer.protopatch import map_finding, read_findings

RECORD = {
    "schemaVersion": 1,
    "findingId": "f-1",
    "featureId": "feat-1",
    "title": "Race between prune() and resolve() on the same entry.",
    "category": "concurrency",
    "severity": "high",
    "confidence": "high",
    "evidence": [
        {"path": "lib/cache.ts", "startLine": 42, "endLine": 60, "symbol": None, "quote": "rmSync(victim.path)"}
    ],
    "reasoning": "prune() deletes while resolve() may be mid-checkout.",
    "recommendation": "Hold the per-key lock during prune.",
    "status": "open",
    "signature": "sig-1",
}


def test_maps_the_full_record():
    f = map_finding(RECORD)
    assert f == {
        "file": "lib/cache.ts",
        "line": 42,
        "severity": "major",
        "category": "concurrency",
        "claim": "Race between prune() and resolve() on the same entry.",
        "evidence": "rmSync(victim.path) Fix: Hold the per-key lock during prune. (protopatch confidence: high)",
        "source": "protopatch",
    }


def test_severity_scale_maps_onto_adr0077():
    for theirs, ours in (("critical", "blocker"), ("high", "major"), ("medium", "minor"), ("low", "nit")):
        assert map_finding({**RECORD, "severity": theirs})["severity"] == ours
    assert map_finding({**RECORD, "severity": "weird"})["severity"] == "minor"


def test_category_passes_through_verbatim():
    assert map_finding({**RECORD, "category": "api-contract"})["category"] == "api-contract"


def test_resolved_records_do_not_report():
    for status in ("fixed", "false-positive", "wont-fix"):
        assert map_finding({**RECORD, "status": status}) is None
    assert map_finding({**RECORD, "status": "uncertain"}) is not None


def test_no_quote_falls_back_to_reasoning():
    record = {**RECORD, "evidence": [{"path": "a.py", "startLine": None, "quote": None}]}
    f = map_finding(record)
    assert f["line"] == 0
    assert f["evidence"].startswith("prune() deletes")


def test_untitled_record_is_dropped():
    assert map_finding({**RECORD, "title": " "}) is None


# ── read_findings: the state-dir read + PR confinement ────────────────────────


def _write(state, name, record):
    (state / "findings").mkdir(parents=True, exist_ok=True)
    (state / "findings" / name).write_text(json.dumps(record))


def test_reads_and_confines_to_changed_files(tmp_path):
    _write(tmp_path, "a.json", RECORD)
    other = {
        **RECORD,
        "findingId": "f-2",
        "signature": "sig-2",
        "evidence": [{"path": "unrelated/file.ts", "startLine": 1, "quote": "x"}],
    }
    _write(tmp_path, "b.json", other)
    found = read_findings(tmp_path, {"lib/cache.ts"})
    assert len(found) == 1 and found[0]["file"] == "lib/cache.ts"


def test_unknown_diff_reports_unconfined(tmp_path):
    _write(tmp_path, "a.json", RECORD)
    assert len(read_findings(tmp_path, None)) == 1


def test_dedupes_by_signature_and_tolerates_junk(tmp_path):
    _write(tmp_path, "a.json", RECORD)
    _write(tmp_path, "dup.json", {**RECORD, "findingId": "f-other"})  # same signature
    (tmp_path / "findings" / "junk.json").write_text("not json {")
    (tmp_path / "findings" / "list.json").write_text("[1, 2]")
    assert len(read_findings(tmp_path, {"lib/cache.ts"})) == 1


def test_missing_findings_dir_is_a_clean_review(tmp_path):
    assert read_findings(tmp_path, {"a.py"}) == []

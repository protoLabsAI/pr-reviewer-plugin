"""Issue #259 layer 3: a `confirmed` must rest on evidence the claim did not already contain."""

from __future__ import annotations

import json

from pr_reviewer.evidence_guard import (
    apply_evidence_guard,
    is_restated,
    library_candidates,
    needs_dependencies,
    parse_dependencies,
    render_evidence_footnote,
    unquoted_library,
    verifier_note,
)
from pr_reviewer.verdicts import FAIL, WARN, verdict_for

# data-plugin#1@17029d7e, verbatim: the two false majors that were the whole FAIL.
DESCRIBE = {
    "file": "tools.py",
    "line": 130,
    "severity": "major",
    "verdict": "confirmed",
    "claim": "data_schema is non-functional: it calls engine.run_query with a DESCRIBE statement, but "
    "engine.guard() (invoked by run_query) only permits duckdb.StatementType.SELECT, so the tool always "
    "returns a 'Read-only: only SELECT queries run here (got DESCRIBE)' refusal.",
    "evidence": 'tools.py: `desc = engine.run_query([s], f"DESCRIBE SELECT * FROM {v}", cap=2000)` — engine.py '
    "guard(): `if st != duckdb.StatementType.SELECT: ... raise QueryError(...)`",
    "note": "Verified: data_schema calls run_query with 'DESCRIBE SELECT * FROM …'; run_query calls guard() which "
    "checks `st != duckdb.StatementType.SELECT` and raises. DuckDB's parser classifies DESCRIBE as its own "
    "StatementType, not SELECT, so the guard always rejects it.",
}
DEPS = {"duckdb", "openpyxl"}


def test_the_describe_major_is_demoted_for_unquoted_library_semantics():
    assert library_candidates(DESCRIBE) == {"duckdb"}
    assert unquoted_library(DESCRIBE, DEPS) == "duckdb"
    out, demoted = apply_evidence_guard([DESCRIBE], DEPS)
    assert out[0]["verdict"] == "uncertain" and out[0]["semantics_unquoted"] == "duckdb"
    assert "ungrounded" not in out[0]  # still carried as a prior to disposition next round
    assert demoted == [{"file": "tools.py", "severity": "major", "kind": "library", "detail": "duckdb"}]
    assert verdict_for([DESCRIBE]) == FAIL and verdict_for(out) == WARN


def test_a_note_that_shows_the_library_is_left_alone():
    for shown in (
        "Ran duckdb.extract_statements('DESCRIBE …') on 1.5.6: StatementType.DESCRIBE.",
        "Per https://duckdb.org/docs/sql/statements/describe the parser classifies it separately.",
        "The duckdb docs say DESCRIBE is its own statement type.",
    ):
        f = {**DESCRIBE, "note": shown + " DuckDB's parser classifies DESCRIBE as its own StatementType."}
        assert unquoted_library(f, DEPS) == "", shown


def test_only_a_declared_dependency_counts():
    assert unquoted_library(DESCRIBE, None) == ""  # manifests unreadable: check nothing
    assert unquoted_library(DESCRIBE, set()) == ""
    assert unquoted_library(DESCRIBE, {"pandas"}) == ""  # duckdb not declared: not a library we know


def test_stdlib_and_local_names_are_not_library_candidates():
    # protoLab#34 (TRUE): stdlib subprocess / a local `model` variable.
    stdlib = {
        "verdict": "confirmed",
        "claim": "latest_tag crashes: subprocess raises CalledProcessError on a non-zero exit.",
        "evidence": "`out = subprocess.run([...], check=True, timeout=30).stdout`",
        "note": "check=True raises CalledProcessError; subprocess raises TimeoutExpired past 30s.",
    }
    local = {
        "verdict": "confirmed",
        "claim": "extra_body is sent for every model, so cloud endpoints that reject unknown fields 400.",
        "evidence": '`if model.startswith("protolabs/"):`',
        "note": 'Line 48 has `if model.startswith("protolabs/"):` gating extra_body.',
    }
    assert library_candidates(stdlib) == set()
    assert unquoted_library(local, {"openai"}) == ""


def test_a_dotted_use_alone_is_not_a_behaviour_assertion():
    f = {**DESCRIBE, "note": "Line 130 reads `desc = engine.run_query(...)` and guard checks `duckdb.StatementType`."}
    assert library_candidates(f) == set()


RESTATED = {
    "verdict": "confirmed",
    "severity": "major",
    "claim": "The retry loop in fetch_page never backs off, so a rate-limited upstream is hammered until "
    "the attempt budget is exhausted.",
    "evidence": "fetch_page retries immediately on a 429.",
    "note": "Verified: fetch_page retries immediately and never backs off, so the upstream is hammered "
    "until the budget is exhausted.",
}


def test_a_note_that_reads_the_claim_back_is_demoted():
    assert is_restated(RESTATED)
    out, demoted = apply_evidence_guard([RESTATED], None)
    assert out[0]["verdict"] == "uncertain" and out[0]["evidence_restated"] is True
    assert demoted[0]["kind"] == "restated"


def test_a_note_that_quotes_code_or_adds_substance_is_not_restated():
    assert not is_restated({**RESTATED, "note": "Line 40 reads `time.sleep(0)` inside the retry loop."})
    assert not is_restated(
        {**RESTATED, "note": "Traced the loop: attempt counter increments, sleep is zero, jitter disabled by config."}
    )
    assert not is_restated({**RESTATED, "note": "Confirmed."})  # too terse to judge either way
    assert not is_restated({**RESTATED, "note": ""})


def test_plugin_appended_segments_are_not_the_verifiers_note():
    f = {
        "note": "Line 9 reads `x = 1`. — nearby: in code this PR did not change — reported, not gated (#232)"
        " — carried from a prior round (protoAgent#2283)"
    }
    assert verifier_note(f) == "Line 9 reads `x = 1`."


def test_only_confirmed_findings_are_touched():
    for verdict in ("uncertain", "refuted", "", "possibly-addressed"):
        f = {**RESTATED, "verdict": verdict}
        assert apply_evidence_guard([f], DEPS) == ([f], []), verdict
    grounded_out = {**DESCRIBE, "ungrounded": True}
    assert apply_evidence_guard([grounded_out], DEPS) == ([grounded_out], [])


def test_needs_dependencies_only_for_a_library_candidate():
    assert needs_dependencies([DESCRIBE])
    assert not needs_dependencies([RESTATED])
    assert not needs_dependencies([{**DESCRIBE, "verdict": "uncertain"}])


def test_parse_dependencies_reads_the_common_manifests():
    pyproject = """
[project]
dependencies = ["duckdb>=1.4,<2", "openpyxl>=3.1"]
[project.optional-dependencies]
xl = ["XlsxWriter>=3"]
[dependency-groups]
dev = ["pytest>=8", {include-group = "lint"}]
[tool.poetry.dependencies]
python = "^3.11"
Requests = "*"
"""
    reqs = "# pinned\nlangchain-mcp-adapters>=0.2,<0.4\n-r base.txt\nruff==0.15.10\n"
    pkg = json.dumps({"dependencies": {"vega-embed": "^6"}, "devDependencies": {"@scope/thing": "1"}})
    deps = parse_dependencies({"pyproject.toml": pyproject, "requirements.txt": reqs, "package.json": pkg})
    assert {"duckdb", "openpyxl", "xlsxwriter", "pytest", "requests", "langchain_mcp_adapters", "ruff"} <= deps
    assert {"vega_embed", "thing"} <= deps and "python" not in deps
    assert parse_dependencies({"pyproject.toml": "not [toml", "package.json": "{"}) == set()


def test_the_footnote():
    text = render_evidence_footnote(
        [
            {"file": "tools.py", "severity": "major", "kind": "library", "detail": "duckdb"},
            {"file": "a.py", "severity": "minor", "kind": "restated", "detail": ""},
        ]
    )
    assert "`duckdb`" in text and "restates the claim" in text and "#259" in text
    assert render_evidence_footnote([]) == ""

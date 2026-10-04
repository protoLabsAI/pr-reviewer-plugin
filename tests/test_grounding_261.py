"""Issue #261 parts 1 and 3: grounding checks the evidence (not the suggested fix), looks in the
file the evidence names before calling a quote missing, and re-anchors the line to the quote.

The two downgrades are data-plugin#1@eb377e62's, with the finding text verbatim from the audit
fixture (`fixtures/audit_259.json`).
"""

from __future__ import annotations

import json
from pathlib import Path

from pr_reviewer.grounding import (
    anchor_quotes,
    apply_grounding,
    correct_line_numbers,
    descriptive_text,
    ground_finding,
    quoted_snippets,
    related_candidates,
)

FIXTURE = {r["id"]: r for r in json.loads((Path(__file__).parent / "fixtures" / "audit_259.json").read_text())}
RELEASE = FIXTURE["data-plugin#1@eb377e62#1"]  # release.yml:26 `secrets: inherit` — TRUE
NO_TEST = FIXTURE["data-plugin#1@eb377e62#0"]  # cites tests/test_plugin.py, quotes conftest's `call`

RELEASE_YML = (
    "name: release\n" + "\n" * 21 + "  release:\n"
    "    if: startsWith(github.event.head_commit.message, 'chore: release v')\n"
    "    uses: protoLabsAI/release-tools/.github/workflows/plugin-release.yml@v2\n"
    "    secrets: inherit\n"
)
TEST_PLUGIN = "def test_register_contributes_tools_and_skills():\n    assert True\n"
CONFTEST = "import pytest\n\n\ndef call(tool, **kw) -> str:\n    return tool.invoke(kw)\n"


def finding(claim="", evidence="", file="x.py", line=1):
    return {
        "file": file,
        "line": line,
        "severity": "major",
        "claim": claim,
        "evidence": evidence,
        "verdict": "confirmed",
    }


# ── part 1a: the suggested fix is not evidence ──


def test_the_suggested_replacement_is_not_checked():
    assert "Fix:" in RELEASE["evidence"] and "discord_webhook" in RELEASE["evidence"]
    assert not any("discord_webhook" in q for q in quoted_snippets(RELEASE))
    assert "discord_webhook" not in descriptive_text(RELEASE["evidence"])


def test_the_true_release_yml_finding_is_no_longer_downgraded():
    out, downgraded, _ = apply_grounding([RELEASE], {".github/workflows/release.yml": RELEASE_YML})
    assert downgraded == [] and out[0] is RELEASE


def test_a_fabricated_quote_before_the_fix_still_downgrades():
    f = finding(
        evidence="The call is `client = make_client(timeout=None)` here. Fix: use `make_client(timeout=30)`.",
    )
    grounded, missing = ground_finding(f, "client = make_client()\n")
    assert not grounded and missing == ["client = make_client(timeout=None)"]


def test_replace_a_with_b_checks_a_and_skips_b():
    f = finding(evidence="Replace `retries = retry_count(0)` with `retries = retry_count(3)` so it backs off.")
    assert quoted_snippets(f) == ["retries = retry_count(0)"]
    for lead in ("should be", "e.g.", "for example,", "change it to", "rewrite it as"):
        g = finding(evidence=f"The guard `if token is None: return []` {lead} `if token is None: raise Missing()`.")
        assert quoted_snippets(g) == ["if token is None: return []"], lead


def test_evidence_quotes_win_over_the_claims():
    # The claim states the conclusion and may name code that is not there yet; the evidence is
    # what the finding says the file contains.
    f = finding(
        claim="It should call `close_all(sessions)` on exit.",
        evidence="`atexit.register(flush_logs, force=True)` only.",
    )
    assert quoted_snippets(f) == ["atexit.register(flush_logs, force=True)"]
    assert ground_finding(f, "atexit.register(flush_logs, force=True)\n") == (True, [])


def test_a_claim_quote_is_still_checked_when_the_evidence_quotes_nothing():
    # #25's guard: a fabricated claim quote with prose-only evidence must not fail open.
    f = finding(claim="`_writable_dir()` builds `writable = Path(str(configured))`.", evidence="See the helper.")
    grounded, missing = ground_finding(f, "writable = Path(configured).expanduser()\n")
    assert not grounded and missing == ["writable = Path(str(configured))"]


# ── part 1b: resolve the file the evidence names ──


def test_related_candidates_resolve_a_named_file():
    assert related_candidates(NO_TEST)[:1] == ["tests/conftest.py"]
    bare = finding(file="tests/test_plugin.py", evidence="conftest.py defines it")
    assert related_candidates(bare) == ["tests/conftest.py", "conftest.py"]


def test_a_quote_from_the_named_file_is_found_there():
    quotes = quoted_snippets(NO_TEST)
    assert any(q.startswith("def call(tool, **kw)") for q in quotes)
    # Searched in the cited file alone it is missing (the audit's downgrade)...
    out, downgraded, _ = apply_grounding([NO_TEST], {"tests/test_plugin.py": TEST_PLUGIN})
    assert downgraded
    # ...and found in the file the evidence names.
    sources = {"tests/test_plugin.py": TEST_PLUGIN, "tests/conftest.py": CONFTEST}
    out, downgraded, _ = apply_grounding([NO_TEST], sources)
    assert downgraded == [] and out[0] is NO_TEST


def test_a_quote_in_another_files_patch_is_found():
    f = finding(file="a.py", evidence="`total = compute_total(items)` feeds it.")
    patches = "@@ -1 +1 @@\n+total = compute_total(items)\n"
    assert apply_grounding([f], {"a.py": "x = 1\n"}, patches=patches)[1] == []
    assert apply_grounding([f], {"a.py": "x = 1\n"})[1]  # without it: missing


def test_a_fabrication_is_found_nowhere():
    f = finding(file="tests/test_plugin.py", evidence="tests/conftest.py defines `def call(tool, *, strict=True)`.")
    sources = {"tests/test_plugin.py": TEST_PLUGIN, "tests/conftest.py": CONFTEST}
    assert apply_grounding([f], sources, patches="+def call(tool, **kw)\n")[1]


# ── part 3: the line follows the evidence ──


def test_bare_code_evidence_reanchors_the_line():
    # analyst-archetype#1@082970f6: anchored at 31, the quoted call is at 41.
    row = FIXTURE["analyst-archetype#1@082970f6#1"]
    assert row["line"] == 31 and anchor_quotes(row) == ["out = subprocess.run("]
    blob = "\n" * 38 + "def latest_tag(url: str) -> str | None:\n    tags = []\n    out = subprocess.run(\n"
    out = correct_line_numbers([row], {row["file"]: blob})
    assert out[0]["line"] == 41 and out[0]["line_original"] == 31 and out[0]["line_corrected"] is True


def test_a_multiline_quote_anchors_on_its_first_line():
    f = finding(file="a.py", line=2, evidence="```python\nfor f in CORPUS_FILES:\n    text += f.read_text()\n```")
    blob = "x = 1\n\n\nfor f in CORPUS_FILES:\n    text += f.read_text()\n"
    out = correct_line_numbers([f], {"a.py": blob})
    assert out[0]["line"] == 4 and out[0]["line_original"] == 2


def test_an_ambiguous_longest_quote_falls_back_to_a_unique_one():
    f = finding(file="a.py", line=1, evidence="`value = compute(a, b)` then `report(value, verbose=True)`")
    blob = "value = compute(a, b)\nvalue = compute(a, b)\n\n\nreport(value, verbose=True)\n"
    assert correct_line_numbers([f], {"a.py": blob})[0]["line"] == 5


def test_an_unmoved_line_records_no_original_and_none_unique_is_left_alone():
    f = finding(file="a.py", line=2, evidence="`value = compute(a, b)`")
    out = correct_line_numbers([f], {"a.py": "x = 1\nvalue = compute(a, b)\n"})
    assert out[0]["line"] == 2 and "line_original" not in out[0]
    dup = correct_line_numbers([f], {"a.py": "value = compute(a, b)\nvalue = compute(a, b)\n"})
    assert dup[0] is f

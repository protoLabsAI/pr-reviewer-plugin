"""The README documents the draft→ready lifecycle contract (issue #98).

Docs-as-code: undrafting a watched PR hands it to the QA panel, which arms native
squash auto-merge on a promoted current PASS. That contract is nowhere in code to
assert against, so we assert the README states it — and, crucially, that the wording
does NOT imply a PASS bypasses the stale-head / completeness / CI / unresolved-thread
guards `promotion_decision` enforces.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Read at import, but a missing/unreadable README must FAIL the tests, not error the
# collection (#251): the error is kept in `_LOAD_ERROR`, which tests/conftest.py turns into
# a failure of every test in this module.
try:
    README = (ROOT / "README.md").read_text()
    _LOAD_ERROR: Exception | None = None
except OSError as exc:
    README, _LOAD_ERROR = "", exc


def _top_level_section(needle: str) -> str:
    """Body of the `## ` section whose title contains `needle` (excludes `### `)."""
    lines = README.splitlines()

    def is_h2(line: str) -> bool:
        return line.startswith("## ") and not line.startswith("### ")

    start = next((i for i, line in enumerate(lines) if is_h2(line) and needle in line), None)
    assert start is not None, f"no top-level section titled ~{needle!r}"
    end = next((j for j in range(start + 1, len(lines)) if is_h2(lines[j])), len(lines))
    return "\n".join(lines[start:end])


try:
    CONTRACT = _top_level_section("draft").lower()
except AssertionError as exc:
    CONTRACT, _LOAD_ERROR = "", _LOAD_ERROR or exc
# Whitespace-collapsed copy so phrase regexes survive the README's line wrapping.
CONTRACT_FLAT = re.sub(r"\s+", " ", CONTRACT)


def test_readme_says_draft_prs_are_skipped():
    # r1: draft PRs are skipped by the panel.
    assert re.search(r"draft[^.]*?\bskip", CONTRACT_FLAT), CONTRACT


def test_readme_says_ready_pr_can_arm_squash_auto_merge_on_pass():
    # r2: an eligible ready-for-review PR with a promoted current PASS can have native
    # squash auto-merge armed by the panel.
    assert "ready for review" in CONTRACT or "ready-for-review" in CONTRACT
    assert "squash auto-merge" in CONTRACT or ("squash" in CONTRACT and "auto-merge" in CONTRACT)
    assert "pass" in CONTRACT


def test_readme_tells_operators_to_keep_unshippable_prs_draft():
    # r3: keep a PR draft when it is reviewed but must not ship yet.
    assert re.search(r"keep (it|the pr)[^.]*?draft", CONTRACT_FLAT), CONTRACT


def test_readme_does_not_imply_pass_bypasses_the_guards():
    # r4: the wording must name every fail-closed guard so a PASS reads as gated,
    # not as a bypass — stale-head, completeness, CI, and unresolved threads.
    assert "stale" in CONTRACT
    assert "complete" in CONTRACT  # covers "complete" and "incomplete" coverage
    assert "thread" in CONTRACT
    assert "ci" in CONTRACT or "check" in CONTRACT

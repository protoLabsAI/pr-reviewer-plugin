"""Absence claims are grounded against the head TREE, not the (truncatable) diff — issue #209.

design-system-plugin#19 shipped two blocking majors — "fetch.py … with no test file" and
"siteprobe.py … has no test file" — that were false: `tests/test_fetch.py` and
`tests/test_site_audit.py` existed at the head and CI ran them. The 309 KB diff overran the
panel's ~200K-char budget, the truncation dropped the alphabetically-late `tests/` files, and
the panel then asserted (and CONFIRMED) their absence. An absence cannot be established against
a source that was never fully seen — so:

  * a plausible test in the head tree REFUTES a "no test file" major (r1);
  * a truncated diff makes an ungroundable absence claim non-blocking, with a note (r2);
  * a genuine absence — no plausible test in a readable tree, diff intact — still gates (r3);
  * the char-budget truncation itself lives in the protoAgent workflow engine (the recipe
    fetches base↔head and trims it); this repo only DETECTS it. `diff_truncation` shows the
    detector correctly identifies the alphabetically-late `tests/` files as the ones dropped (r4).
"""

from __future__ import annotations

import base64
import json
from urllib.parse import unquote

from pr_reviewer.grounding import (
    diff_truncation,
    ground_absence_claims,
    is_absence_claim,
    plausible_test_in_tree,
    render_absence_footnote,
)
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.verdicts import FAIL, WARN, verdict_for

from tests.test_dispatch import HEAD, RoutedGH, facts, make

# ── unit: recognising an absence claim ────────────────────────────────────────


def test_is_absence_claim_recognises_the_panels_phrasings():
    for text in (
        "fetch.py is added with no test file",
        "siteprobe.py has no test file",
        "the module has no tests",
        "this handler is missing test coverage",
        "the new code is untested",
        "the change adds no documentation",
        "error paths are not tested",
        "the PR exercises none of it",
        "missing error handling for the timeout case",
    ):
        assert is_absence_claim({"claim": text, "evidence": ""}) is True, text


def test_is_absence_claim_ignores_ordinary_mentions():
    for text in (
        "the test asserts the wrong value on line 40",
        "add handling for the retry case here",
        "the documentation says otherwise",
        "this test file duplicates coverage from another",
    ):
        assert is_absence_claim({"claim": text, "evidence": ""}) is False, text


# ── unit: matching a plausible test in the tree ───────────────────────────────


def test_plausible_test_in_tree_matches_conventions_across_ecosystems():
    assert plausible_test_in_tree("pkg/fetch.py", {"tests/test_fetch.py"}) == "tests/test_fetch.py"
    assert plausible_test_in_tree("fetch.go", {"fetch_test.go"}) == "fetch_test.go"
    assert plausible_test_in_tree("src/Fetch.tsx", {"src/Fetch.test.tsx"}) == "src/Fetch.test.tsx"
    assert plausible_test_in_tree("src/Fetch.tsx", {"__tests__/Fetch.spec.ts"}) == "__tests__/Fetch.spec.ts"


def test_plausible_test_in_tree_requires_an_exact_subject_not_a_lookalike():
    # The real siteprobe case: a test file EXISTS (`tests/test_site_audit.py`) but its subject
    # is not the module — filename convention cannot ground it, so this returns None and the
    # claim falls to the truncation branch instead (r2), never a false positive-grounding.
    assert plausible_test_in_tree("pkg/siteprobe.py", {"tests/test_site_audit.py"}) is None
    # A same-named test for a DIFFERENT module must not ground: `test_fetcher` ≠ `fetch`.
    assert plausible_test_in_tree("pkg/fetch.py", {"tests/test_fetcher.py"}) is None


def test_plausible_test_in_tree_fails_open_on_an_unreadable_tree():
    assert plausible_test_in_tree("pkg/fetch.py", None) is None


def test_an_absence_claim_about_a_test_file_itself_is_not_groundable_here():
    assert plausible_test_in_tree("tests/test_fetch.py", {"tests/test_fetch.py"}) is None


# ── unit: ground_absence_claims dispositions ──────────────────────────────────

FETCH_MAJOR = {
    "file": "pkg/fetch.py",
    "line": 1,
    "severity": "major",
    "verdict": "confirmed",
    "claim": "pkg/fetch.py is added with no test file — the change exercises none of it.",
    "evidence": "There is no test for this module.",
}


def test_a_no_test_major_is_demoted_when_a_test_exists_in_the_tree():
    out, demoted = ground_absence_claims([FETCH_MAJOR], {"tests/test_fetch.py"}, truncated=False)
    assert verdict_for(out) == WARN  # demoted below the gate
    assert out[0]["verdict"] == "uncertain" and out[0]["ungrounded"] is True
    assert out[0]["absence_demoted"] == "test-exists"
    assert len(demoted) == 1 and demoted[0]["detail"] == "tests/test_fetch.py"


def test_an_ungroundable_absence_on_a_truncated_diff_is_nonblocking():
    # No plausible test in the tree, but the diff was truncated — the panel did not see the
    # whole change, so the absence cannot be established. Non-blocking with a truncation note.
    siteprobe = {**FETCH_MAJOR, "file": "pkg/siteprobe.py"}
    out, demoted = ground_absence_claims([siteprobe], {"tests/test_site_audit.py"}, truncated=True)
    assert verdict_for(out) == WARN
    assert out[0]["absence_demoted"] == "diff-truncated"
    assert demoted[0]["kind"] == "diff-truncated"
    assert "truncated" in render_absence_footnote(demoted)


def test_a_genuine_absence_still_gates():
    # No plausible test in a tree we COULD read, and the diff was NOT truncated — the absence
    # is established, so the major stands and still fails the verdict.
    out, demoted = ground_absence_claims([FETCH_MAJOR], {"pkg/fetch.py", "README.md"}, truncated=False)
    assert verdict_for(out) == FAIL
    assert demoted == [] and out[0].get("absence_demoted") is None


def test_ground_absence_claims_only_touches_gating_severities():
    nit = {**FETCH_MAJOR, "severity": "nit"}
    out, demoted = ground_absence_claims([nit], {"tests/test_fetch.py"}, truncated=True)
    assert demoted == [] and out[0].get("absence_demoted") is None  # a nit never gates anyway


def test_a_non_absence_finding_is_never_touched():
    real = {
        "file": "pkg/fetch.py",
        "severity": "major",
        "verdict": "confirmed",
        "claim": "the retry loop never breaks on success",
        "evidence": "`while True:` with no break",
    }
    out, demoted = ground_absence_claims([real], {"tests/test_fetch.py"}, truncated=True)
    assert demoted == [] and out[0] == real


# ── unit: diff truncation detection (r4 — the truncation itself lives in the engine) ──


def test_diff_truncation_drops_the_alphabetically_late_tests_first():
    # A large early source file eats the budget; the alphabetically-late `tests/` file is what
    # falls off — exactly how the panel came to assert a test's absence (issue #209).
    sizes = [("src/big_generated.py", 180), ("tests/test_fetch.py", 60)]
    truncated, dropped = diff_truncation(sizes, budget=200)
    assert truncated is True and dropped == ["tests/test_fetch.py"]


def test_diff_truncation_latches_once_the_budget_is_hit():
    sizes = [("a.py", 150), ("m.py", 100), ("z.py", 1)]  # z would "fit" but the drop has latched
    truncated, dropped = diff_truncation(sizes, budget=200)
    assert truncated is True and dropped == ["m.py", "z.py"]


def test_diff_truncation_under_budget_and_disabled():
    assert diff_truncation([("a.py", 10), ("b.py", 10)], budget=200) == (False, [])
    assert diff_truncation([("a.py", 10_000)], budget=0) == (False, [])  # non-positive → no limit


def test_render_absence_footnote_is_empty_when_nothing_demoted():
    assert render_absence_footnote([]) == ""


# ── end-to-end through the dispatcher ─────────────────────────────────────────


def absence_report(file, claim, severity="major"):
    return (
        "<!-- brief -->\nBrief.\n<!-- /brief -->\n\n```json\n"
        + json.dumps(
            [
                {
                    "file": file,
                    "line": 1,
                    "severity": severity,
                    "category": "testing",
                    "claim": claim,
                    "evidence": "There is no test for this module.",
                    "verdict": "confirmed",
                }
            ]
        )
        + "\n```"
    )


class AbsenceGH(RoutedGH):
    """Serves the head tree, per-file patch sizes, and file contents, so the absence-grounding
    reads (`_head_tree`, `_diff_truncation`, `_finding_sources`) all resolve. `file_patches`
    keys are the PR's changed files; `tree` is the head's blob paths."""

    def __init__(self, *, tree, file_patches, tree_sha="tree0", contents404=(), **kw):
        super().__init__(pr_facts=facts(), reviews=[], **kw)
        self.tree = set(tree)
        self.file_patches = dict(file_patches)
        self.tree_sha = tree_sha
        self.contents404 = set(contents404)

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if ".commit.tree.sha" in joined and "/commits/" in joined:  # _commit_tree
            self.calls.append(args)
            return 0, self.tree_sha, ""
        if "/git/trees/" in joined:  # _head_tree
            self.calls.append(args)
            return 0, "\n".join(sorted(self.tree)), ""
        if "/contents/" in joined:  # _finding_sources head-file read
            self.calls.append(args)
            path = unquote(joined.split("/contents/", 1)[1].split("?", 1)[0])
            if path in self.contents404:
                return 1, "", "404"
            src = "def placeholder(): pass\n"
            return 0, "base64\x00" + base64.b64encode(src.encode()).decode(), ""
        if "/files" in joined and "n: ((.patch" in joined:  # _diff_truncation
            self.calls.append(args)
            rows = [{"f": f, "n": len(p)} for f, p in self.file_patches.items()]
            return 0, "\n".join(json.dumps(r) for r in rows), ""
        if "/files" in joined and "p: .patch" in joined:  # _finding_sources patches
            self.calls.append(args)
            rows = [{"f": f, "p": p} for f, p in self.file_patches.items()]
            return 0, "\n".join(json.dumps(r) for r in rows), ""
        if "/files" in joined:  # _changed_paths
            self.calls.append(args)
            return 0, "".join(f"{f}\n" for f in self.file_patches), ""
        return await super().__call__(args, timeout=timeout)


async def test_no_test_file_major_is_demoted_when_the_test_exists_in_the_head_tree(tmp_path):
    # r1: `tests/test_fetch.py` exists at the head — the blocking "no test file" major is
    # refuted and cannot request changes.
    gh = AbsenceGH(
        tree={"pkg/fetch.py", "tests/test_fetch.py"},
        file_patches={"pkg/fetch.py": "diff-a" * 5},
    )

    async def runner(name, inputs):
        return {"output": absence_report("pkg/fetch.py", "pkg/fetch.py is added with no test file."), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert gh.posted[0]["event"] == "COMMENT"  # not REQUEST_CHANGES
    assert "downgraded to **uncertain**" in body
    assert "tests/test_fetch.py" in body  # the refuting test is named


async def test_absence_on_a_truncated_diff_is_nonblocking_with_a_truncation_note(tmp_path):
    # r2: the siteprobe case. No test matches the module by name, but the diff overran the
    # budget and the alphabetically-late test was dropped — so the claim cannot gate.
    gh = AbsenceGH(
        tree={"pkg/siteprobe.py", "tests/test_site_audit.py"},
        file_patches={"pkg/siteprobe.py": "x" * 40, "tests/test_site_audit.py": "y" * 40},
    )

    async def runner(name, inputs):
        return {"output": absence_report("pkg/siteprobe.py", "pkg/siteprobe.py has no test file."), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False, "diff_char_budget": 50}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert gh.posted[0]["event"] == "COMMENT"
    assert "truncated" in body


async def test_a_genuine_absence_still_requests_changes(tmp_path):
    # r3: no plausible test in a tree we read, and the diff fits the budget — the absence is
    # established, so the major stands and gates the merge.
    gh = AbsenceGH(
        tree={"pkg/fetch.py", "README.md"},
        file_patches={"pkg/fetch.py": "diff" * 5},
        checks=[{"status": "completed", "conclusion": "failure"}],  # so a FAIL arms REQUEST_CHANGES
    )

    async def runner(name, inputs):
        return {"output": absence_report("pkg/fetch.py", "pkg/fetch.py is added with no test file."), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    body = gh.reviews_posted[0]["body"]
    assert gh.posted[0]["event"] == "REQUEST_CHANGES"
    assert "downgraded to **uncertain**" not in body


async def test_absence_grounding_is_skipped_when_disabled(tmp_path):
    # With grounding off the absence claim gates as raised — and the tree/size reads are never
    # spent (the whole feature is behind the same switch as the #25 quote grounding).
    gh = AbsenceGH(
        tree={"pkg/fetch.py", "tests/test_fetch.py"},
        file_patches={"pkg/fetch.py": "diff" * 5},
        checks=[{"status": "completed", "conclusion": "failure"}],
    )

    async def runner(name, inputs):
        return {"output": absence_report("pkg/fetch.py", "pkg/fetch.py is added with no test file."), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_grounding": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert not any("/git/trees/" in " ".join(c) for c in gh.calls)  # no tree read when disabled


def test_diff_char_budget_config_and_env(tmp_path, monkeypatch):
    from pr_reviewer.dispatch import DIFF_CHAR_BUDGET, Dispatcher

    monkeypatch.delenv("PR_REVIEWER_DIFF_CHAR_BUDGET", raising=False)
    assert Dispatcher({}, Telemetry(tmp_path)).diff_char_budget == DIFF_CHAR_BUDGET
    monkeypatch.setenv("PR_REVIEWER_DIFF_CHAR_BUDGET", "1234")
    assert Dispatcher({}, Telemetry(tmp_path)).diff_char_budget == 1234
    assert Dispatcher({"diff_char_budget": 99}, Telemetry(tmp_path)).diff_char_budget == 99  # cfg wins

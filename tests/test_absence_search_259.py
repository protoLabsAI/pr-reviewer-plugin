"""Issue #259 layer 1: an absence claim is checked by a search of the head checkout.

The shapes are the audit's: data-plugin#1's conftest `call` helper "dead" while 100+ tests call
it, protoAgent#4025's "no Python tests" when `tests/test_plugin_services.py` imports the module.
Every repo here is a real local git repo, searched with real `git grep`.
"""

from __future__ import annotations

import asyncio

from pr_reviewer.absence_search import (
    EXISTENCE,
    TEST,
    USAGE,
    AbsenceSearcher,
    absence_families,
    claim_subjects,
    distinctive,
    is_candidate,
    render_absence_search_footnote,
)
from pr_reviewer.verdicts import FAIL, WARN, verdict_for

from tests.git_helpers import git_repo, resolver_for

CONFTEST = '''import pytest


def call(tool, **kw) -> str:
    """Invoke a tool the way the agent does."""
    return tool.invoke(kw)
'''
TEST_CHART = """from conftest import call

import tools


def test_chart():
    assert call(tools.data_chart, sql="select 1")
"""
DATA_PLUGIN = {
    "tools.py": "def data_chart():\n    rows, extra = [], []\n    return ''.join(extra)\n",
    "engine.py": "class Result:\n    notes: list = None\n\n\ndef run_query(sql):\n    return sql\n",
    "tests/conftest.py": CONFTEST,
    "tests/test_plugin.py": "def test_register():\n    assert True\n",
    "tests/test_chart.py": TEST_CHART,
}


def finding(file, claim, *, severity="major", verdict="confirmed", line=1, evidence="", note=""):
    return {
        "file": file,
        "line": line,
        "severity": severity,
        "claim": claim,
        "evidence": evidence,
        "verdict": verdict,
        "note": note,
    }


async def run(tmp_path, files, findings, **kw):
    sha = git_repo(tmp_path / "repo", files)
    searcher = AbsenceSearcher({}, resolve_checkout=resolver_for(tmp_path / "repo"), **kw)
    return await searcher.check("o/r", sha, findings)


# ── recognising the claim ──


def test_families_cover_test_usage_and_existence_and_skip_code_shape():
    assert absence_families(finding("a.py", "`foo` has no test coverage.")) == [TEST]
    assert absence_families(finding("a.py", "The `call` helper is dead code: it is never called.")) == [USAGE]
    assert absence_families(finding("a.sh", "No `scorecard.py` module exists in `evals/runners/`.")) == [EXISTENCE]
    # A code-shape absence is about the cited code itself: a search cannot settle it (#209).
    assert absence_families(finding("a.py", "the input is used without validation; `foo` is never called")) == []
    assert absence_families(finding("a.py", "`foo` returns None on a miss.")) == []


def test_subjects_come_from_the_claims_backticks_only():
    f = finding(
        "graph/plugin_services.py",
        "The new module `graph/plugin_services.py`, `register_service` in `registry.py`, `service()` and "
        "`test_show_artifact` have no test coverage.",
        evidence="`gh` is stubbed",
    )
    names, files, dirs = claim_subjects(f)
    assert names == ["register_service", "service"]  # `test_*` names the test, not its subject
    assert files == ["graph/plugin_services.py", "registry.py"]
    assert "gh" not in names  # evidence context is not what the claim says is absent
    assert dirs == []


def test_distinctive_names():
    assert distinctive("plugin_services") and distinctive("vega-lite") and distinctive("showService")
    assert distinctive("plugininstaller") and not distinctive("registry")
    assert not distinctive("call") and not distinctive("service") and not distinctive("sdk")


def test_a_finding_without_a_subject_or_already_refuted_is_not_a_candidate():
    assert not is_candidate(finding("a.py", "There are no tests for any of this."))  # cited stem `a` too short
    assert not is_candidate(finding("tools.py", "`call` is dead.", verdict="refuted"))
    assert not is_candidate({**finding("tools.py", "`call` is dead."), "ungrounded": True})
    assert is_candidate(finding("tools.py", "`call` is dead."))


# ── refuting ──


async def test_a_dead_helper_that_tests_call_is_refuted(tmp_path):
    # data-plugin#1@eb377e62: "The `call` helper is dead code: defined but never called by any test."
    f = finding("tests/conftest.py", "The `call` helper is dead code: it is defined but never called.", line=4)
    out, refuted, unsearched = await run(tmp_path, DATA_PLUGIN, [f])
    assert out[0]["verdict"] == "uncertain" and out[0]["ungrounded"] is True
    assert out[0]["absence_refuted"].startswith("tests/test_chart.py:")
    assert "issue #259" in out[0]["note"]
    assert [r["file"] for r in refuted] == ["tests/conftest.py"] and unsearched == []


async def test_no_behavioral_test_is_refuted_by_a_test_calling_the_helper(tmp_path):
    f = finding(
        "tests/test_plugin.py",
        "No behavioral test exercises any of the tools — the conftest's `call` helper that no test uses.",
    )
    out, refuted, _ = await run(tmp_path, DATA_PLUGIN, [f])
    assert out[0]["absence_refuted"].startswith("tests/test_chart.py:")
    assert verdict_for(out) == WARN  # was FAIL as a confirmed major


async def test_no_tests_for_a_module_is_refuted_by_a_test_importing_it(tmp_path):
    files = {
        "graph/plugin_services.py": "def is_service_name(n):\n    return bool(n)\n",
        "graph/registry.py": "def register_service():\n    pass\n",
        "tests/test_plugin_services.py": "from graph.plugin_services import is_service_name\n",
        "tests/test_other.py": "def test_x():\n    assert 'the registry' # the registry's requirement\n",
    }
    f = finding(
        "graph/plugin_services.py",
        "The new module `graph/plugin_services.py` (5 public functions) has no test coverage.",
        severity="minor",
    )
    out, refuted, _ = await run(tmp_path, files, [f])
    assert out[0]["absence_refuted"] == "tests/test_plugin_services.py:1"


async def test_a_distinctive_subject_in_another_test_file_refutes_not_covered(tmp_path):
    # protoAgent#4025: "The new `vega-lite` kind is not covered by the parametrize list" — the
    # cited test file is excluded; `test_artifact_vega.py` covers it.
    files = {
        "tests/test_artifact_plugin.py": "KINDS = ['html', 'svg']\n",
        "tests/test_artifact_vega.py": "def test_chart():\n    show(kind='vega-lite')\n    show(kind='vega-lite')\n",
    }
    f = finding("tests/test_artifact_plugin.py", "The new `vega-lite` kind is not covered by the existing list.")
    out, _, _ = await run(tmp_path, files, [f])
    assert out[0]["absence_refuted"] == "tests/test_artifact_vega.py:2"


async def test_a_file_that_exists_refutes_does_not_exist(tmp_path):
    files = {"evals/eval.sh": "python -m runners.scorecard\n", "evals/runners/scorecard.py": "x = 1\n"}
    f = finding("evals/eval.sh", "No `scorecard.py` module exists in `evals/runners/` at the PR head.")
    out, _, _ = await run(tmp_path, files, [f])
    assert out[0]["absence_refuted"] == "evals/runners/scorecard.py"


# ── a genuine absence stands, and says what was searched ──


async def test_a_genuinely_untested_module_stays_confirmed(tmp_path):
    # analyst-archetype#1: "check_bundle_updates.py … with no unit tests in this repo" — TRUE.
    files = {
        "scripts/check_bundle_updates.py": "def is_compatible(a, b):\n    return a == b\n",
        "scripts/verify_bundle.py": "import check_bundle_updates  # not a test\n",
    }
    f = finding("scripts/check_bundle_updates.py", "This script adds semver logic with no unit tests in this repo.")
    out, refuted, unsearched = await run(tmp_path, files, [f])
    assert out[0]["verdict"] == "confirmed" and not refuted and not unsearched
    assert out[0]["absence_searched"] == ["tests: check_bundle_updates"]
    assert verdict_for(out) == FAIL


async def test_a_genuinely_dead_function_stays_confirmed(tmp_path):
    files = {"pkg/util.py": "def orphan_helper():\n    pass\n\n\n# orphan_helper is kept for now\n"}
    f = finding("pkg/util.py", "`orphan_helper` is dead: nothing calls it.")
    out, refuted, _ = await run(tmp_path, files, [f])
    assert out[0]["verdict"] == "confirmed" and not refuted  # the comment is not a use


async def test_a_missing_file_in_the_named_directory_stands_even_if_it_exists_elsewhere(tmp_path):
    # protoLab#34: "no `scorecard.py` module exists in `evals/runners/`" — TRUE. A scorecard.py
    # somewhere else does not satisfy `python -m runners.scorecard`.
    files = {"evals/eval.sh": "python -m runners.scorecard\n", "tools/scorecard.py": "x = 1\n"}
    f = finding("evals/eval.sh", "No `scorecard.py` module exists in `evals/runners/` at the PR head.")
    out, refuted, _ = await run(tmp_path, files, [f])
    assert out[0]["verdict"] == "confirmed" and not refuted


async def test_a_plain_word_in_another_language_does_not_refute(tmp_path):
    # `service(` in a TypeScript e2e test is not a test of a Python module.
    files = {
        "graph/sdk.py": "def service(name):\n    return name\n",
        "apps/web/e2e/fleet.spec.ts": "await page.service('x');\nservice(1);\n",
    }
    f = finding("graph/sdk.py", "`service()` in `sdk.py` has no test coverage.", severity="minor")
    out, refuted, _ = await run(tmp_path, files, [f])
    assert not refuted and out[0]["verdict"] == "confirmed"


async def test_a_module_stem_counts_only_as_a_module_reference(tmp_path):
    files = {
        "graph/registry.py": "def register():\n    pass\n",
        "tests/test_misc.py": "def test_x():\n    assert ok  # the registry's requirement\n",
    }
    f = finding("graph/registry.py", "The registry module has no test coverage.", severity="minor")
    out, refuted, _ = await run(tmp_path, files, [f])
    assert not refuted


async def test_a_dead_local_or_field_has_nothing_to_search_and_is_left_alone(tmp_path):
    # data-plugin#1@17029d7e: "`notes` is dead", "`extra` is never appended to" — both TRUE, and
    # neither has a definition a repo-wide search could anchor on.
    fs = [
        finding("engine.py", "The Result dataclass field `notes` is dead: it is never read.", severity="minor"),
        finding("tools.py", "In data_chart, the `extra` list is never appended to — dead code.", severity="minor"),
    ]
    out, refuted, unsearched = await run(tmp_path, DATA_PLUGIN, fs)
    assert out == fs and not refuted and not unsearched


# ── no search ⇒ never confirmed ──


async def test_no_checkout_makes_a_confirmed_absence_uncertain(tmp_path):
    async def none(repo, head):
        return None

    f = finding("tests/conftest.py", "The `call` helper is dead code.")
    out, refuted, unsearched = await AbsenceSearcher({}, resolve_checkout=none).check("o/r", "a" * 40, [f])
    assert out[0]["verdict"] == "uncertain" and out[0]["absence_unsearched"] is True
    assert "ungrounded" not in out[0]  # not refuted: it still carries as a prior next round
    assert not refuted and [u["file"] for u in unsearched] == ["tests/conftest.py"]
    assert verdict_for(out) == WARN


async def test_an_already_uncertain_finding_is_not_reported_as_unsearched(tmp_path):
    async def none(repo, head):
        return None

    f = finding("tests/conftest.py", "The `call` helper is dead code.", verdict="uncertain")
    out, refuted, unsearched = await AbsenceSearcher({}, resolve_checkout=none).check("o/r", "a" * 40, [f])
    assert out == [f] and not unsearched


async def test_a_git_failure_is_unsearched_not_a_pass(tmp_path):
    git_repo(tmp_path / "repo", DATA_PLUGIN)
    f = finding("tests/conftest.py", "The `call` helper is dead code.")
    searcher = AbsenceSearcher({}, resolve_checkout=resolver_for(tmp_path / "repo"))
    out, _, unsearched = await searcher.check("o/r", "f" * 40, [f])  # a head the checkout lacks
    assert out[0]["absence_unsearched"] is True and unsearched


async def test_a_resolver_that_raises_never_raises_out(tmp_path):
    async def boom(repo, head):
        raise RuntimeError("clone failed")

    f = finding("tests/conftest.py", "The `call` helper is dead code.")
    out, _, unsearched = await AbsenceSearcher({}, resolve_checkout=boom).check("o/r", "a" * 40, [f])
    assert out[0]["absence_unsearched"] is True and unsearched


async def test_a_slow_checkout_is_shielded_from_the_budget(tmp_path):
    # A clone cut off mid-write would leave a half checkout the cache later serves as a hit: the
    # budget gives up on WAITING, never cancels the clone.
    done = asyncio.Event()

    async def slow(repo, head):
        await asyncio.sleep(0.3)
        done.set()
        return None

    f = finding("tests/conftest.py", "The `call` helper is dead code.")
    searcher = AbsenceSearcher({"absence_search_budget_s": 0.05}, resolve_checkout=slow)
    out, _, unsearched = await searcher.check("o/r", "a" * 40, [f])
    assert out[0]["absence_unsearched"] is True
    await asyncio.wait_for(done.wait(), timeout=2)  # the resolve ran to completion


async def test_non_absence_findings_never_touch_the_checkout(tmp_path):
    calls = []

    async def resolve(repo, head):
        calls.append(head)
        return None

    f = finding("tools.py", "`run_query` returns rows unsorted.")
    out, _, _ = await AbsenceSearcher({}, resolve_checkout=resolve).check("o/r", "a" * 40, [f])
    assert out == [f] and calls == []


def test_the_footnote_names_each_outcome():
    text = render_absence_search_footnote(
        [{"file": "a.py", "severity": "major", "detail": "tests/test_a.py:3"}],
        [{"file": "b.py", "severity": "minor", "detail": ""}],
    )
    assert "tests/test_a.py:3" in text and "could not be searched" in text and "#259" in text
    assert render_absence_search_footnote([], []) == ""


# ── the checkout is the structural pass's own ──


def test_the_checkout_root_is_shared_with_the_structural_pass(tmp_path, monkeypatch):
    from pr_reviewer.checkout_cache import checkout_root_for
    from pr_reviewer.protopatch import ProtoPatchRunner

    monkeypatch.setenv("PR_REVIEWER_HOME", str(tmp_path))
    assert ProtoPatchRunner({}).checkout_root == checkout_root_for({}) == tmp_path / "checkouts"
    cfg = {"checkout_root": str(tmp_path / "elsewhere")}
    assert ProtoPatchRunner(cfg).checkout_root == checkout_root_for(cfg)


def test_two_caches_over_one_root_share_the_clone_lock(tmp_path):
    from pr_reviewer.checkout_cache import _LOCKS, CheckoutCache

    a, b = CheckoutCache(tmp_path), CheckoutCache(tmp_path)
    assert a._locks is b._locks is _LOCKS


async def test_the_default_resolver_reuses_a_fresh_entry_and_never_clones_when_told_not_to(tmp_path):
    from pr_reviewer.absence_search import checkout_resolver

    sha = "c" * 40
    entry = tmp_path / "o-r" / sha
    entry.mkdir(parents=True)
    resolve = checkout_resolver({"checkout_root": str(tmp_path), "absence_search_clone": False})
    assert await resolve("o/r", sha) == entry  # the structural pass's checkout: a cache hit
    assert await resolve("o/r", "d" * 40) is None  # a miss, and cloning is off


def test_regrade_record_carries_the_259_demotions_into_the_record():
    from pr_reviewer.dispatch import regrade_record

    recorded = [
        {"file": "a.py", "claim": "`x` is dead.", "verdict": "confirmed", "note": "n"},
        {"file": "b.py", "claim": "other", "verdict": "confirmed"},
        {"file": "c.py", "claim": "no test file", "verdict": "confirmed"},
    ]
    verdict_input = [
        {
            **recorded[0],
            "line": 9,
            "verdict": "uncertain",
            "ungrounded": True,
            "absence_refuted": "t.py:1",
            "note": "m",
        },
        dict(recorded[1]),
        # #209's own demotion keeps its pre-#259 behaviour: it does not write the record.
        {**recorded[2], "verdict": "uncertain", "ungrounded": True, "absence_demoted": "test-exists"},
    ]
    out = regrade_record(recorded, verdict_input)
    assert out[0] == {
        **recorded[0],
        "verdict": "uncertain",
        "ungrounded": True,
        "absence_refuted": "t.py:1",
        "note": "m",
    }
    assert out[1] is recorded[1] and out[2] is recorded[2]
    assert regrade_record(recorded, [dict(f) for f in recorded]) is recorded

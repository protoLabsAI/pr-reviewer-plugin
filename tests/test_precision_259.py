"""Issue #259 ask 4: verifier precision, scored on a labelled set.

`fixtures/audit_259.json` is the 2026-10-03 audit of Vera's 8 non-PASS formal reviews on
v0.54–v0.56.1: 34 findings as posted (minimal fields), each labelled TRUE/FALSE against the code
at its head. 10 are FALSE and 9 of those were `confirmed`. Each FALSE carries its class:
`absence` (a whole-repo claim made from a partial view), `library-semantics` (a library's
behaviour asserted, not shown) or `semantics-misread` (a wrong reading of correctly quoted code).

The deterministic layers run here exactly as the dispatcher runs them: the absence search over a
real git checkout, then the evidence guard with the dependencies read from that checkout's
manifests. The audited repos are not vendored; each is a small synthetic repo with the SHAPE the
finding turns on (a conftest-defined `call` used across test files, a module imported by its
test, a `pyproject.toml` declaring duckdb, a missing `evals/runners/scorecard.py`).

The bar: every FALSE these layers can mechanically catch (both mechanical classes) stops being
`confirmed`, and no TRUE finding is demoted. A verifier or layer change is scored by re-running
this file.
"""

from __future__ import annotations

import json
from pathlib import Path

from pr_reviewer.absence_search import AbsenceSearcher
from pr_reviewer.evidence_guard import MANIFESTS, apply_evidence_guard, parse_dependencies
from pr_reviewer.verdicts import verdict_for

from tests.git_helpers import git_repo, resolver_for

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "audit_259.json").read_text())

REPOS: dict[str, dict[str, str]] = {
    "data-plugin": {
        "pyproject.toml": '[project]\nname = "data"\ndependencies = ["duckdb>=1.4,<2", "openpyxl>=3.1"]\n',
        "engine.py": (
            "import duckdb\n\n\nclass Result:\n    notes: list = None\n\n\n"
            "def guard(sql):\n    st = duckdb.extract_statements(sql)[0].type\n"
            "    if st != duckdb.StatementType.SELECT:\n        raise ValueError(sql)\n\n\n"
            "def run_query(sources, sql, cap=0, timeout_s=0):\n    guard(sql)\n"
        ),
        "tools.py": (
            "import engine\n\n\ndef data_schema(s, v):\n"
            '    return engine.run_query([s], f"DESCRIBE SELECT * FROM {v}")\n\n\n'
            "def data_profile(s, v):\n    rows, extra = [], []\n"
            '    engine.run_query([s], f"SUMMARIZE SELECT * FROM {v}")\n    return "".join(extra)\n'
        ),
        "tests/conftest.py": "def call(tool, **kw) -> str:\n    return tool.invoke(kw)\n",
        "tests/test_plugin.py": "def test_register_contributes_tools_and_skills():\n    assert True\n",
        "tests/test_chart.py": (
            "from conftest import call\n\nimport tools\n\n\n"
            "def test_chart():\n    assert call(tools.data_schema, s=1, v='t')\n"
        ),
    },
    "protoAgent": {
        "pyproject.toml": '[project]\nname = "protoagent"\ndependencies = ["langchain-mcp-adapters>=0.2,<0.4"]\n',
        "graph/plugin_services.py": "def is_service_name(name):\n    return bool(name)\n",
        "graph/plugins/registry.py": "def register_service(name, svc):\n    return name\n",
        "graph/plugins/testkit.py": (
            "class FakeRegistry:\n    def register_service(self, name, svc):\n"
            "        from graph.plugin_services import is_service_name\n\n        return is_service_name(name)\n"
        ),
        "graph/sdk.py": "def service(name):\n    return name\n",
        "plugins/artifact/_tools.py": "def show_service(kind):\n    return kind\n",
        "tests/test_plugin_services.py": "from graph.plugin_services import is_service_name\n",
        "tests/test_artifact_vega.py": "def test_show_artifact_creates_a_vega_lite_chart():\n    show(kind='vega-lite')\n",
        "tests/test_artifact_plugin.py": "KINDS = ['html', 'svg', 'mermaid', 'react', 'markdown']\n",
        "tests/test_bundled_config_assets.py": "def test_x(p):\n    p.read_text()\n",
    },
    "analyst-archetype": {
        "scripts/check_bundle_updates.py": (
            "import subprocess\n\n\ndef latest_tag(url):\n"
            "    return subprocess.run(['git', 'ls-remote', url], check=True, timeout=30).stdout\n\n\n"
            "def is_compatible(a, b):\n    return b[0] == 0 and b[1] == 0\n"
        ),
        "scripts/verify_bundle.py": "print('verify')\n",
    },
    "protoLab": {
        "evals/eval-model.sh": 'python -m runners.scorecard --out-dir "$OUT"\n',
        "evals/runners/__init__.py": "",
        "evals/runners/sampling.py": "def resolve(name):\n    return name\n",
        "evals/runners/run_livecodebench.py": "from sampling import resolve, to_openai_kwargs\n",
        "evals/graders/verify_coherence.py": "CORPUS_FILES = []\n",
    },
}


def confirmed_counts(rows: list[dict], *, gating: bool = False) -> tuple[int, int]:
    """(confirmed findings, FALSE among them) — the verifier's precision, as counts."""
    pool = [r for r in rows if not gating or str(r.get("severity")) in ("blocker", "major")]
    confirmed = [r for r in pool if r.get("verdict") == "confirmed"]
    return len(confirmed), sum(r["label"] == "FALSE" for r in confirmed)


async def run_layers(tmp_path) -> list[dict]:
    out: list[dict] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in FIXTURE:
        groups.setdefault((row["repo"], row["head"]), []).append(dict(row))
    shas: dict[str, str] = {}
    for (repo, _head), rows in groups.items():
        root = tmp_path / repo
        if repo not in shas:
            shas[repo] = git_repo(root, REPOS[repo])
        searcher = AbsenceSearcher({}, resolve_checkout=resolver_for(root))
        rows, _refuted, _unsearched = await searcher.check(f"protoLabsAI/{repo}", shas[repo], rows)
        manifests = {m: (root / m).read_text() for m in MANIFESTS if (root / m).is_file()}
        rows, _demoted = apply_evidence_guard(rows, parse_dependencies(manifests) if manifests else None)
        out += rows
    return out


def test_the_fixture_is_the_audit():
    assert len(FIXTURE) == 34
    assert sum(r["label"] == "FALSE" for r in FIXTURE) == 10
    assert sum(r["label"] == "FALSE" and r["verdict"] == "confirmed" for r in FIXTURE) == 9
    assert {r.get("false_class") for r in FIXTURE if r["label"] == "FALSE"} == {
        "absence",
        "library-semantics",
        "semantics-misread",
    }


async def test_precision_before_and_after_the_deterministic_layers(tmp_path):
    after = await run_layers(tmp_path)
    assert [r["id"] for r in after] == [r["id"] for r in FIXTURE]  # nothing dropped, order kept

    # Before: 32 confirmed, 9 of them FALSE (precision 23/32 = 72%); of the 9 confirmed
    # blocker/majors, 4 FALSE — the DuckDB majors, twice each — and they made a FAIL.
    assert confirmed_counts(FIXTURE) == (32, 9)
    assert confirmed_counts(FIXTURE, gating=True) == (9, 4)
    # After: 25 confirmed, 2 FALSE (23/25 = 92%); no FALSE blocker/major is confirmed.
    assert confirmed_counts(after) == (25, 2)
    assert confirmed_counts(after, gating=True) == (5, 0)


async def test_no_true_finding_is_demoted(tmp_path):
    after = await run_layers(tmp_path)
    moved = [a["id"] for a, b in zip(after, FIXTURE) if a["label"] == "TRUE" and a["verdict"] != b["verdict"]]
    assert moved == []


async def test_each_mechanical_class_is_caught_and_only_the_misreads_remain(tmp_path):
    after = await run_layers(tmp_path)
    false = {r["id"]: r for r in after if r["label"] == "FALSE"}
    for rid, r in false.items():
        if r["false_class"] == "absence":
            assert r.get("absence_refuted"), rid  # refuted with the hit, not just unsearched
        elif r["false_class"] == "library-semantics":
            assert r.get("semantics_unquoted") == "duckdb", rid
        else:  # a wrong reading of correctly quoted code: no mechanical check settles it
            assert r["verdict"] == "confirmed", rid
    assert sum(r["false_class"] == "semantics-misread" for r in false.values()) == 2


async def test_the_data_plugin_fail_becomes_a_warn(tmp_path):
    # data-plugin#1@17029d7e went FAIL on its two false majors alone; the true findings were
    # 2 minors and 2 nits. After the layers it is a WARN.
    after = await run_layers(tmp_path)
    head = [r for r in after if r["id"].startswith("data-plugin#1@17029d7e")]
    before = [r for r in FIXTURE if r["id"].startswith("data-plugin#1@17029d7e")]
    assert verdict_for(before) == "FAIL" and verdict_for(head) == "WARN"

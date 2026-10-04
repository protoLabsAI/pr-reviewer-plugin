"""Issue #259 through the dispatcher: the verdict, the posted body and the posted RECORD.

The record matters as much as the verdict. A demotion that changed only the verdict input would
post a findings array still saying `confirmed`, and the next round recalls its priors from that
array — so the same claim would come back as a confirmed major. Each test reads the record back
with the plugin's own `read_findings_record`, the reader `rounds.panel_rounds` uses.
"""

from __future__ import annotations

import base64
import json
from urllib.parse import unquote

from pr_reviewer.verdicts import read_findings_record

from tests.git_helpers import git_repo, resolver_for
from tests.test_absence_claims_209 import AbsenceGH
from tests.test_dispatch import facts, make

DEAD_CALL = {
    "file": "tests/conftest.py",
    "line": 1,
    "severity": "major",
    "category": "testing",
    "claim": "The `call` helper is dead code: it is defined but never called by any test in the suite.",
    "evidence": "The helper is defined in the conftest and no test module references it.",
    "verdict": "confirmed",
    # Quotes what was searched, so the evidence guard has nothing to say: only the search decides.
    "note": "Searched `tests/` for `call(` at head: no use outside the conftest.",
}
REPO = {
    "tests/conftest.py": "def call(tool, **kw):\n    return tool.invoke(kw)\n",
    "tests/test_chart.py": "from conftest import call\n\n\ndef test_chart():\n    assert call(len, obj=1)\n",
}

DESCRIBE_SRC = (
    "import engine\n\n\ndef data_schema(s, v):\n"
    '    desc = engine.run_query([s], f"DESCRIBE SELECT * FROM {v}", cap=2000)\n'
    "    return desc\n"
)
DESCRIBE = {
    "file": "tools.py",
    "line": 5,
    "severity": "major",
    "category": "correctness",
    "claim": "data_schema is non-functional: engine.guard() only permits SELECT statements, so the tool "
    "always returns a refusal.",
    "evidence": '`desc = engine.run_query([s], f"DESCRIBE SELECT * FROM {v}", cap=2000)`',
    "verdict": "confirmed",
    "note": "Verified: run_query calls guard() which checks `duckdb.StatementType.SELECT`. DuckDB's parser "
    "classifies DESCRIBE as its own StatementType, so the guard always rejects it.",
}


def report(*findings) -> str:
    return "<!-- brief -->\nBrief.\n<!-- /brief -->\n\n```json\n" + json.dumps(list(findings)) + "\n```"


class SourcesGH(AbsenceGH):
    """AbsenceGH, with real file contents at head (for grounding and the manifests)."""

    def __init__(self, *, sources, **kw):
        super().__init__(**kw)
        self.sources = dict(sources)
        self.content_reads: list[str] = []

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/contents/" in joined:
            self.calls.append(args)
            self.content_reads.append(args[1])
            path = unquote(joined.split("/contents/", 1)[1].split("?", 1)[0])
            if path not in self.sources:
                return 1, "", "404"
            return 0, "base64\x00" + base64.b64encode(self.sources[path].encode()).decode(), ""
        return await super().__call__(args, timeout=timeout)


def failing_checks():
    return [{"status": "completed", "conclusion": "failure"}]  # so a FAIL arms REQUEST_CHANGES


async def test_a_dead_helper_major_the_search_refutes_posts_warn_and_records_it(tmp_path):
    sha = git_repo(tmp_path / "repo", REPO)
    gh = SourcesGH(
        sources=REPO,
        tree=set(REPO),
        file_patches={"tests/conftest.py": "+def call(tool, **kw):"},
        checks=failing_checks(),
    )
    gh.pr_facts = facts(head=sha)

    async def runner(name, inputs):
        return {"output": report(DEAD_CALL), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    d._resolve_checkout = resolver_for(tmp_path / "repo")
    assert (await d.handle_pr_event("o/r", 1, sha, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert gh.reviews_posted[0]["event"] == "COMMENT"
    assert "tests/test_chart.py:" in body and "issue #259" in body
    record, recorded = read_findings_record(body)
    assert recorded and len(record) == 1  # demoted, never dropped
    assert record[0]["verdict"] == "uncertain" and record[0]["absence_refuted"].startswith("tests/test_chart.py:")
    assert record[0]["ungrounded"] is True  # a refuted claim is not carried as debt


async def test_with_no_checkout_a_confirmed_absence_major_cannot_fail(tmp_path):
    # The autouse fixture leaves the default resolver with no checkout.
    gh = SourcesGH(
        sources=REPO,
        tree=set(REPO),
        file_patches={"tests/conftest.py": "+def call(tool, **kw):"},
        checks=failing_checks(),
    )

    async def runner(name, inputs):
        return {"output": report(DEAD_CALL), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, gh.pr_facts["head"], "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "could not be searched" in body
    record, _ = read_findings_record(body)
    assert record[0]["verdict"] == "uncertain" and record[0]["absence_unsearched"] is True
    assert not record[0].get("ungrounded")  # unsearched is not refuted: it still carries


async def test_absence_search_off_leaves_the_major_gating(tmp_path):
    gh = SourcesGH(
        sources=REPO,
        tree=set(REPO),
        file_patches={"tests/conftest.py": "+def call(tool, **kw):"},
        checks=failing_checks(),
    )

    async def runner(name, inputs):
        return {"output": report(DEAD_CALL), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False, "absence_search": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, gh.pr_facts["head"], "opened")) == "reviewed:FAIL"


async def test_an_unquoted_library_major_posts_warn_from_manifests_read_at_head(tmp_path):
    sources = {"tools.py": DESCRIBE_SRC, "pyproject.toml": '[project]\ndependencies = ["duckdb>=1.4,<2"]\n'}
    gh = SourcesGH(sources=sources, tree=set(sources), file_patches={"tools.py": "+x"}, checks=failing_checks())
    head = gh.pr_facts["head"]

    async def runner(name, inputs):
        return {"output": report(DESCRIBE), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, head, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "`duckdb`" in body and "issue #259" in body
    record, _ = read_findings_record(body)
    assert record[0]["verdict"] == "uncertain" and record[0]["semantics_unquoted"] == "duckdb"
    assert not record[0].get("ungrounded")  # still a prior the next round must disposition
    manifest_reads = [u for u in gh.content_reads if "pyproject.toml" in u]
    assert manifest_reads and all(f"ref={head}" in u for u in manifest_reads)  # pinned to the head


async def test_an_undeclared_library_or_unreadable_manifest_changes_nothing(tmp_path):
    sources = {"tools.py": DESCRIBE_SRC}  # no manifest at head
    gh = SourcesGH(sources=sources, tree=set(sources), file_patches={"tools.py": "+x"}, checks=failing_checks())

    async def runner(name, inputs):
        return {"output": report(DESCRIBE), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, gh.pr_facts["head"], "opened")) == "reviewed:FAIL"


async def test_evidence_guard_off_leaves_the_major_gating(tmp_path):
    sources = {"tools.py": DESCRIBE_SRC, "pyproject.toml": '[project]\ndependencies = ["duckdb"]\n'}
    gh = SourcesGH(sources=sources, tree=set(sources), file_patches={"tools.py": "+x"}, checks=failing_checks())

    async def runner(name, inputs):
        return {"output": report(DESCRIBE), "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_guard": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, gh.pr_facts["head"], "opened")) == "reviewed:FAIL"
    assert not any("pyproject.toml" in u for u in gh.content_reads)

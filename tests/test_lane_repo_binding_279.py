"""#279 — review lanes are pinned to the bound repo.

On protoContent#596 (repos: [], default_repo: '') the `find_conventions` lane's github
calls omitted `repo`, resolved to the agent's default repo (protoAgent), came back with no
findings, and counted as coverage: a clean PASS on the WRONG codebase. These tests pin the
fix end to end — the lane inputs name the bound repo + head SHA, a lane that reads another
repo (or omits the repo with a differing default) is marked incomplete with a Gap naming the
wrong repo and cannot post a clean PASS, a lane that stays on the bound repo is untouched, and
the structural tool prefers the bound repo over a stray default_repo."""

from __future__ import annotations

import json

import pytest
from pr_reviewer import protopatch as pp
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.verdicts import lane_repo_violation

from tests.conftest import note_write
from tests.dispatch_helpers import HEAD, FakeGH, facts, make

BOUND = "protoLabsAI/protoContent"
OTHER = "protoLabsAI/protoAgent"
PR = 596

LLM_LANES = ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions")
CLEAN_LANE = "No issues from this angle.\n\n```json\n[]\n```\nFINDER_STATUS: reviewed n=0"
CLEAN_REPORT = "<!-- brief -->\nNothing raised.\n<!-- /brief -->\n\n```json\n[]\n```"


def _panel_steps() -> dict:
    """Every step of the structural recipe delivering an explicit, honest clean pass."""
    steps = {s: CLEAN_LANE for s in LLM_LANES}
    steps["find_structural"] = "protoPatch run: head/base resolved, 2 files.\n\n```json\n[]\n```"
    steps["synthesize"] = "<!-- brief -->\nNothing raised.\n<!-- /brief -->\n\n```json\n[]\n```"
    steps["verify"] = "VERIFY_STATUS: nothing-to-verify\n\n```json\n[]\n```"
    return steps


class BoundGH(FakeGH):
    """A canned gh for a round bound to `repo`#`pr` — repo/PR aware (unlike the shared
    RoutedGH, which pins PR 1) so the test can use the incident's protoLabsAI/protoContent#596.
    First review, no prior rounds, no threads; records the verdict review and the check writes."""

    def __init__(self, repo: str, pr: int, *, pr_facts: dict, files: str):
        super().__init__()
        self.repo, self.pr = repo, pr
        self.pr_facts, self.files = pr_facts, files
        self.check_writes: list[dict] = []
        self._next_check_id = 2000
        self._review_check_ids: set[int] = set()

    async def __call__(self, args, timeout=30):
        self.calls.append(args)
        if note_write(args):
            return 1, "", "unexpected write (tests/conftest.py KNOWN_WRITES)"
        joined = " ".join(args)
        if "/check-runs" in joined and "-X" in args:
            fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a}
            if "POST" in args and fields.get("name") == "protoReview":
                cid = self._next_check_id
                self._next_check_id += 1
                self._review_check_ids.add(cid)
                self.check_writes.append({"method": "POST", "url": args[1], "id": cid, **fields})
                return 0, str(cid), ""
            tail = args[1].rsplit("/", 1)[-1] if len(args) > 1 else ""
            if "PATCH" in args and tail.isdigit() and int(tail) in self._review_check_ids:
                self.check_writes.append({"method": "PATCH", "url": args[1], **fields})
                return 0, "{}", ""
        if "-X" in args and "POST" in args and "/reviews" in joined:
            fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("query=")}
            self.posted.append({"url": args[1] if len(args) > 1 else "", **fields})
            return 0, "{}", ""
        if args[1] == "user":
            return 0, "qa-bot", ""
        if "/compare/" in joined:
            return 1, "", "404"
        if f"/pulls/{self.pr}/files" in joined:
            return 0, self.files, ""
        if f"/pulls/{self.pr}/reviews" in joined:
            return 0, "[]", ""
        if "comments(first" in joined:
            return 0, "\x00", ""  # the threads fetch: an empty connection
        if "graphql" in joined:
            return 0, json.dumps({"pageInfo": {"hasNextPage": False, "endCursor": ""}, "nodes": []}), ""
        if f"/pulls/{self.pr}" in joined:
            return 0, json.dumps(self.pr_facts), ""
        if "/pulls?" in joined:
            return 0, f"[{self.pr}]", ""
        return 0, "", ""


def _gh() -> BoundGH:
    return BoundGH(
        BOUND,
        PR,
        pr_facts=facts(changed_files=6, additions=300, deletions=50),
        files="plugins/coding_agent/acp_client.py\nCHANGELOG.md\nc\nd\ne\nf\n",
    )


def _events(tmp_path, name: str) -> list[dict]:
    return [e for e in Telemetry(tmp_path).read_all() if e.get("event") == name]


def _clean_runner_with(tool_calls=None):
    """A structural panel that runs clean on every lane, carrying `tool_calls` (what the host
    records each lane called) through to the dispatcher's grounding check."""

    async def runner(name, inputs):
        result = {"output": CLEAN_REPORT, "failed": [], "steps": _panel_steps()}
        if tool_calls is not None:
            result["tool_calls"] = tool_calls
        return result

    return runner


# ── the pure helper ─────────────────────────────────────────────────────────────


def test_helper_flags_a_call_naming_another_repo():
    why = lane_repo_violation([{"tool": "github_read_file", "repo": OTHER}], BOUND, OTHER)
    assert why and OTHER in why and BOUND in why


def test_helper_passes_when_every_call_names_the_bound_repo():
    calls = [{"tool": "github_pr_diff", "repo": BOUND}, {"tool": "github_read_file", "args": {"repo": BOUND}}]
    assert lane_repo_violation(calls, BOUND, OTHER) is None


def test_helper_is_case_insensitive_on_the_repo():
    assert lane_repo_violation([{"tool": "github_read_file", "repo": "ProtoLabsAI/ProtoContent"}], BOUND) is None


def test_helper_flags_an_omitted_repo_when_the_default_differs():
    why = lane_repo_violation([{"tool": "github_read_file"}], BOUND, OTHER)
    assert why and OTHER in why and BOUND in why


def test_helper_flags_an_omitted_repo_when_the_default_is_empty():
    # Fail-closed: an empty/unknown agent default is not provably the bound repo.
    why = lane_repo_violation([{"tool": "github_read_file"}], BOUND, "")
    assert why and BOUND in why


def test_helper_allows_an_omitted_repo_only_when_the_default_is_the_bound_repo():
    assert lane_repo_violation([{"tool": "github_read_file"}], BOUND, BOUND) is None


def test_helper_ignores_non_repo_scoped_calls_and_empty_input():
    assert lane_repo_violation([{"tool": "think"}, {"tool": "web_search"}], BOUND, OTHER) is None
    assert lane_repo_violation([], BOUND, OTHER) is None
    assert lane_repo_violation(None, BOUND, OTHER) is None


# ── r1: the lane inputs carry the bound repo + head SHA ──────────────────────────


async def test_lane_inputs_name_the_bound_repo_and_head_sha(tmp_path):
    captured: dict = {}

    async def runner(name, inputs):
        captured.update(inputs)
        return {"output": CLEAN_REPORT, "failed": [], "steps": _panel_steps()}

    d = make(tmp_path, cfg={"repos": [BOUND]}, gh=_gh(), runner=runner)
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:PASS"
    assert captured["repo"] == BOUND
    assert captured["head_sha"] == HEAD
    assert captured["pr"] == str(PR)


# ── r2: a lane that read the wrong repo cannot post a clean pass ─────────────────


async def test_a_lane_that_read_another_repo_is_incomplete_with_a_gap_naming_it(tmp_path):
    gh = _gh()
    tool_calls = {"find_conventions": [{"tool": "github_read_file", "repo": OTHER, "path": "acp_client.py"}]}
    d = make(tmp_path, cfg={"repos": [BOUND], "shadow_mode": False}, gh=gh, runner=_clean_runner_with(tool_calls))

    # A clean-looking panel, but the lane read the wrong tree — capped, never a clean PASS.
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "verdict=WARN" in body and "complete=false" in body
    assert "`find_conventions`" in body and OTHER in body and BOUND in body

    reviewed = _events(tmp_path, "reviewed")[-1]
    assert reviewed["verdict"] == "WARN"
    assert "find_conventions" in (reviewed["incomplete_finders"] or [])
    assert reviewed["complete"] is False
    assert reviewed["coverage_capped"] is True

    unbound = _events(tmp_path, "lane_repo_unbound")
    assert unbound and unbound[-1]["lanes"] == ["find_conventions"] and unbound[-1]["bound_repo"] == BOUND


async def test_a_lane_that_omitted_the_repo_with_a_differing_default_is_incomplete(tmp_path):
    gh = _gh()
    tool_calls = {"find_crossfile": [{"tool": "github_pr_diff"}]}  # no repo → resolves to the default
    d = make(
        tmp_path,
        cfg={"repos": [BOUND], "default_repo": OTHER, "shadow_mode": False},
        gh=gh,
        runner=_clean_runner_with(tool_calls),
    )
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "`find_crossfile`" in body and OTHER in body and "complete=false" in body
    assert "find_crossfile" in (_events(tmp_path, "reviewed")[-1]["incomplete_finders"] or [])


# ── r3: a lane that stayed on the bound repo is untouched ────────────────────────


async def test_a_lane_that_stayed_on_the_bound_repo_passes_clean(tmp_path):
    gh = _gh()
    tool_calls = {
        "find_conventions": [{"tool": "github_read_file", "repo": BOUND, "path": "CLAUDE.md"}],
        "find_crossfile": [{"tool": "github_read_file", "args": {"repo": BOUND}}],
    }
    d = make(tmp_path, cfg={"repos": [BOUND], "shadow_mode": False}, gh=gh, runner=_clean_runner_with(tool_calls))
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:PASS"
    reviewed = _events(tmp_path, "reviewed")[-1]
    assert reviewed["complete"] is True
    assert reviewed["incomplete_finders"] is None
    assert not _events(tmp_path, "lane_repo_unbound")


async def test_an_omitted_repo_is_clean_when_the_default_is_the_bound_repo(tmp_path):
    gh = _gh()
    tool_calls = {"find_crossfile": [{"tool": "github_pr_diff"}]}  # omitted, but default == bound
    d = make(
        tmp_path,
        cfg={"repos": [BOUND], "default_repo": BOUND, "shadow_mode": False},
        gh=gh,
        runner=_clean_runner_with(tool_calls),
    )
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:PASS"
    assert _events(tmp_path, "reviewed")[-1]["complete"] is True


async def test_a_host_that_records_no_tool_calls_is_unaffected(tmp_path):
    # Additive: an older host omits the `tool_calls` key, so there is nothing to ground and
    # the lane is judged exactly as before (r3 / existing clean-lane behavior preserved).
    gh = _gh()
    d = make(
        tmp_path,
        cfg={"repos": [BOUND], "default_repo": OTHER, "shadow_mode": False},
        gh=gh,
        runner=_clean_runner_with(None),
    )
    assert (await d.handle_pr_event(BOUND, PR, HEAD, "opened")) == "reviewed:PASS"
    assert _events(tmp_path, "reviewed")[-1]["complete"] is True


# ── r4: protopatch prefers the bound repo over a stray default_repo ──────────────


async def test_protopatch_review_uses_the_bound_repo_over_default_repo(tmp_path, monkeypatch):
    seen: list[str] = []

    async def fake_review(self, pr, repo):
        seen.append(repo)
        return "PROTOPATCH UNAVAILABLE — stubbed"

    monkeypatch.setattr(pp.ProtoPatchRunner, "review", fake_review)
    tools = pp.get_tools({"default_repo": OTHER, "state_root": str(tmp_path)})
    tool = next(t for t in tools if t.name == "protopatch_review")

    try:
        pp.set_bound_repo(BOUND, PR)
        # repo omitted inside a bound round → the bound repo, never the stray default.
        await tool.ainvoke({"pr": PR})
        assert seen[-1] == BOUND
    finally:
        pp.set_bound_repo("", PR)

    # Outside any bound round the old fallback to default_repo holds.
    await tool.ainvoke({"pr": PR})
    assert seen[-1] == OTHER


async def test_protopatch_review_honours_an_explicit_repo(tmp_path, monkeypatch):
    seen: list[str] = []

    async def fake_review(self, pr, repo):
        seen.append(repo)
        return "PROTOPATCH UNAVAILABLE — stubbed"

    monkeypatch.setattr(pp.ProtoPatchRunner, "review", fake_review)
    tools = pp.get_tools({"default_repo": OTHER, "state_root": str(tmp_path)})
    tool = next(t for t in tools if t.name == "protopatch_review")
    try:
        pp.set_bound_repo(BOUND, PR)
        await tool.ainvoke({"pr": PR, "repo": "someone/else"})
        assert seen[-1] == "someone/else"
    finally:
        pp.set_bound_repo("", PR)


@pytest.fixture(autouse=True)
def _clear_bound_repos():
    pp._BOUND_REPOS.clear()
    yield
    pp._BOUND_REPOS.clear()

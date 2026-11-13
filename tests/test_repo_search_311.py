"""#311 — the review lanes get a working, read-only search of the PR head.

Before: 459 `search_files` / `find_files` calls over the audit window, 0 hits. 394 named a
project the host's fs tools do not know, and 24 searched the empty `/sandbox/workspace` and got
"(no matches)", which a lane then read as proof of absence. These tests pin the replacement:
scoped to one repo@head checkout, path escapes refused, output bounded, binary files skipped, and
an error that can never read as "no matches".
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pr_reviewer
import pytest
import yaml
from pr_reviewer import protopatch as pp
from pr_reviewer import repo_search as rs

from tests.conftest import FakeRegistry
from tests.git_helpers import git_repo, resolver_for
from tests.test_dispatch import HEAD, REPORT, RoutedGH, facts, make

REPO = "octo/app"
PR = 7


@pytest.fixture(autouse=True)
def _clean_bindings():
    rs._HEADS.clear()
    yield
    rs._HEADS.clear()
    pp.set_bound_repo("", PR)


def _repo(tmp_path: Path, files: dict[str, str] | None = None) -> tuple[Path, str]:
    root = tmp_path / "checkout"
    sha = git_repo(
        root,
        files
        or {
            "app/core.py": "def helper():\n    return 1\n\n\ndef unused_thing():\n    pass\n",
            "app/cli.py": "from app.core import helper\n\nprint(helper())\n",
            "tests/test_core.py": "from app.core import helper\n\n\ndef test_helper():\n    assert helper() == 1\n",
            "changelog.d/311.md": "search\n",
            "changelog.d/fix-typo.md": "typo\n",
            "README.md": "helper docs\n",
        },
    )
    return root, sha


def _searcher(root: Path, sha: str, **kw) -> rs.RepoSearch:
    rs.bind_review(REPO, PR, sha, warm=False)
    return rs.RepoSearch({}, resolve_checkout=resolver_for(root), **kw)


# ── hits, no-matches, listing ────────────────────────────────────────────────────


async def test_a_hit_is_file_line_and_text(tmp_path):
    root, sha = _repo(tmp_path)
    out = await _searcher(root, sha).search(r"helper\(\)", PR, REPO)
    assert out.startswith(rs.RESULTS)
    assert "complete: 4 match(es)" in out  # def helper(), def test_helper(), two calls
    assert "app/cli.py:3: print(helper())" in out
    assert "tests/test_core.py:5:     assert helper() == 1" in out
    assert f"{REPO}@{sha[:12]}" in out


async def test_no_matches_is_its_own_answer_and_says_what_was_searched(tmp_path):
    root, sha = _repo(tmp_path)
    out = await _searcher(root, sha).search("unused_thing", PR, REPO, "tests/")
    assert out.startswith(rs.NO_MATCHES)
    assert "none of the 1 file(s) under `tests/`" in out
    assert "IS evidence" in out and rs.ERROR not in out


async def test_a_path_that_matches_nothing_says_nothing_was_searched(tmp_path):
    root, sha = _repo(tmp_path)
    out = await _searcher(root, sha).search("helper", PR, REPO, "spec/")
    assert out.startswith(rs.NO_FILES) and "nothing was searched" in out
    assert not out.startswith(rs.NO_MATCHES)


async def test_listing_counts_the_files_a_universality_claim_is_about(tmp_path):
    # The audit's example: "every changelog.d entry is named by issue number" — 38 of 152 were not.
    root, sha = _repo(tmp_path)
    s = _searcher(root, sha)
    out = await s.list_files(PR, REPO, "changelog.d/*.md")
    assert out.startswith(rs.FILES) and "2 file(s) (complete: 2)" in out
    assert "changelog.d/fix-typo.md" in out and "changelog.d/311.md" in out
    missing = await s.list_files(PR, REPO, "docs/")
    assert missing.startswith(rs.NO_FILES) and "complete listing" in missing


@pytest.mark.parametrize(
    "path, expected",
    [
        ("**/*.py", {"app/core.py", "app/cli.py", "tests/test_core.py"}),
        ("*.py", set()),  # a glob matches the whole path, like git's
        ("*.md", {"README.md"}),
        ("app", {"app/core.py", "app/cli.py"}),
        ("app/", {"app/core.py", "app/cli.py"}),
        ("./tests/test_core.py", {"tests/test_core.py"}),
        ("tests/**", {"tests/test_core.py"}),
        ("changelog.d/[0-9]*.md", {"changelog.d/311.md"}),
        (
            "",
            {
                "app/core.py",
                "app/cli.py",
                "tests/test_core.py",
                "changelog.d/311.md",
                "changelog.d/fix-typo.md",
                "README.md",
            },
        ),
    ],
)
def test_path_globs(path, expected):
    tree = [
        "app/core.py",
        "app/cli.py",
        "tests/test_core.py",
        "changelog.d/311.md",
        "changelog.d/fix-typo.md",
        "README.md",
    ]
    match = rs.path_matcher(path)
    assert {f for f in tree if match(f)} == expected


async def test_options_fixed_ignore_case_word_files_only(tmp_path):
    root, sha = _repo(tmp_path, {"a.py": "Foo(x)\nfoo(y)\nfoobar\n", "b.py": "nothing\n"})
    s = _searcher(root, sha)
    assert "complete: 1 match(es)" in await s.search("Foo(", PR, REPO, fixed=True)
    assert "complete: 3 match(es)" in await s.search("foo", PR, REPO, ignore_case=True)
    assert "complete: 2 match(es)" in await s.search("foo", PR, REPO, ignore_case=True, word=True)
    listed = await s.search("foo", PR, REPO, files_only=True)
    assert listed.splitlines()[1:] == ["a.py"] and "1 file(s)" in listed


# ── scoping ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["../outside", "app/../../x", "/etc/passwd", "~/.ssh", ":(top)app", "a\\b", "C:/x"])
async def test_a_path_that_could_leave_the_commit_is_refused_before_git_runs(tmp_path, path):
    calls: list = []

    async def run_git(args, max_lines, timeout_s):
        calls.append(args)
        return 0, [], "", False

    root, sha = _repo(tmp_path)
    s = _searcher(root, sha, run_git=run_git)
    for out in (await s.search("x", PR, REPO, path), await s.list_files(PR, REPO, path)):
        assert out.startswith(rs.ERROR) and "NOT evidence of absence" in out
    assert calls == []


async def test_it_reads_the_commit_never_the_working_tree_or_a_symlink_target(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET\n")
    root = tmp_path / "checkout"
    sha = git_repo(root, {"a.py": "x = 1\n"})
    os.symlink(secret, root / "link.txt")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env.update({"GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull})
    subprocess.run(["git", "-C", str(root), "add", "link.txt"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "link"], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (root / "a.py").write_text("x = 1\nUNCOMMITTED\n")  # working-tree edit, not in the commit
    s = _searcher(root, sha)
    assert (await s.search("TOPSECRET", PR, REPO)).startswith(rs.NO_MATCHES)
    assert (await s.search("UNCOMMITTED", PR, REPO)).startswith(rs.NO_MATCHES)
    # The symlink itself is a file in the commit (its blob is the target's NAME).
    assert "link.txt" in await s.list_files(PR, REPO)


async def test_the_head_is_the_rounds_never_the_models(tmp_path):
    root, sha = _repo(tmp_path)
    seen: list = []

    async def resolve(repo, head):
        seen.append((repo, head))
        return root

    async def resolve_head(repo, pr):
        raise AssertionError("a bound round must not re-resolve its head")

    rs.bind_review(REPO, PR, sha, warm=False)
    s = rs.RepoSearch({}, resolve_checkout=resolve, resolve_head=resolve_head)
    assert (await s.search("helper", PR, REPO)).startswith(rs.RESULTS)
    assert seen == [(REPO, sha)]


async def test_outside_a_round_the_head_is_resolved_server_side(tmp_path):
    root, sha = _repo(tmp_path)
    asked: list = []

    async def resolve_head(repo, pr):
        asked.append((repo, pr))
        return sha

    s = rs.RepoSearch({}, resolve_checkout=resolver_for(root), resolve_head=resolve_head)
    assert (await s.search("helper", PR, REPO)).startswith(rs.RESULTS)
    assert asked == [(REPO, PR)]


async def test_an_omitted_repo_is_the_rounds_and_another_repo_is_refused(tmp_path):
    root, sha = _repo(tmp_path)
    pp.set_bound_repo(REPO, PR)
    s = _searcher(root, sha)
    assert (await s.search("helper", PR)).startswith(rs.RESULTS)
    wrong = await s.search("helper", PR, "octo/other")
    assert wrong.startswith(rs.ERROR) and "under review in octo/app" in wrong


async def test_disabled_is_an_error_not_an_empty_search(tmp_path):
    root, sha = _repo(tmp_path)
    rs.bind_review(REPO, PR, sha, warm=False)
    s = rs.RepoSearch({"repo_search": False}, resolve_checkout=resolver_for(root))
    assert (await s.search("helper", PR, REPO)).startswith(rs.ERROR)


# ── caps ────────────────────────────────────────────────────────────────────────


async def test_results_are_capped_and_say_so(tmp_path):
    root, sha = _repo(tmp_path, {"big.py": "".join(f"hit {i}\n" for i in range(300)), "long.py": "hit " + "x" * 5000})
    s = _searcher(root, sha)
    out = await s.search("hit", PR, REPO)
    rows = out.splitlines()[1:]
    assert len(rows) == rs.DEFAULT_MAX_RESULTS and "truncated" in out and "there are more" in out
    capped = await s.search("hit", PR, REPO, max_results=10_000)
    assert len(capped.splitlines()) - 1 <= rs.MAX_RESULTS_CAP and len(capped) <= rs.OUTPUT_CHARS + 400
    long = await s.search("hit", PR, REPO, "long.py")
    assert "…[line cut]" in long and len(long) < 1000


async def test_exactly_the_cap_is_complete_one_more_is_truncated(tmp_path):
    root, sha = _repo(tmp_path, {"a.py": "hit\n" * 5, "b.py": "hit\n"})
    s = _searcher(root, sha)
    assert "complete: 6 match(es)" in await s.search("hit", PR, REPO, max_results=6)
    assert "truncated" in await s.search("hit", PR, REPO, max_results=5)


async def test_a_search_split_across_git_calls_still_sees_every_file(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "PATHS_PER_GREP", 1)
    root, sha = _repo(tmp_path, {"a.py": "x\n", "b.py": "hit\n", "c.py": "hit\n"})
    s = _searcher(root, sha)
    out = await s.search("hit", PR, REPO)
    assert "complete: 2 match(es)" in out
    # The cap is filled by one chunk; the next chunk's first line marks the result truncated.
    assert "truncated" in await s.search("hit", PR, REPO, max_results=1)


async def test_listing_is_capped(tmp_path):
    root, sha = _repo(tmp_path, {f"d/{i}.md": "x\n" for i in range(30)})
    out = await _searcher(root, sha).list_files(PR, REPO, "d/", max_results=5)
    assert "30 file(s) (showing the first 5 of 30" in out and len(out.splitlines()) == 6


async def test_binary_files_are_skipped(tmp_path):
    root = tmp_path / "checkout"
    sha = git_repo(root, {"a.py": "needle\n"})
    (root / "blob.bin").write_bytes(b"\x00\x01needle\x00")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env.update({"GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": os.devnull})
    subprocess.run(["git", "-C", str(root), "add", "blob.bin"], check=True, env=env)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "bin"], check=True, env=env)
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = await _searcher(root, sha).search("needle", PR, REPO)
    assert "complete: 1 match(es)" in out and "blob.bin" not in out


# ── an error is never "no matches" ───────────────────────────────────────────────


async def test_a_bad_regex_is_an_error_with_a_hint(tmp_path):
    root, sha = _repo(tmp_path)
    out = await _searcher(root, sha).search("helper(", PR, REPO)
    assert out.startswith(rs.ERROR) and "fixed=true" in out and rs.NO_MATCHES not in out


@pytest.mark.parametrize(
    "rc, err",
    [
        (1, "fatal: unable to read tree"),  # rc 1 WITH stderr: an error, not "no match"
        (128, "fatal: bad object"),
        (124, "git timed out after 20s"),
        (127, "git: command not found"),
    ],
)
async def test_a_failing_grep_is_an_error(tmp_path, rc, err):
    root, sha = _repo(tmp_path)

    async def run_git(args, max_lines, timeout_s):
        if "ls-tree" in args:
            return 0, ["a.py"], "", False
        return rc, [], err, False

    out = await _searcher(root, sha, run_git=run_git).search("helper", PR, REPO)
    assert out.startswith(rs.ERROR) and "NOT evidence of absence" in out
    assert rs.NO_MATCHES not in out


async def test_no_checkout_a_failed_clone_or_an_unknown_head_is_an_error(tmp_path):
    async def none(repo, head):
        return None

    async def boom(repo, head):
        raise RuntimeError("clone failed (rc 128)")

    async def no_head(repo, pr):
        raise LookupError("could not resolve PR #7")

    rs.bind_review(REPO, PR, "a" * 40, warm=False)
    for s in (rs.RepoSearch({}, resolve_checkout=none), rs.RepoSearch({}, resolve_checkout=boom)):
        assert (await s.search("x", PR, REPO)).startswith(rs.ERROR)
        assert (await s.list_files(PR, REPO)).startswith(rs.ERROR)
    unbound = rs.RepoSearch({}, resolve_checkout=none, resolve_head=no_head)
    out = await unbound.search("x", PR, "octo/unbound")
    assert out.startswith(rs.ERROR) and "could not resolve" in out


async def test_a_slow_checkout_is_an_error_and_the_clone_carries_on(tmp_path):
    root, sha = _repo(tmp_path)
    release = asyncio.Event()
    finished: list = []

    async def slow(repo, head):
        await release.wait()
        finished.append(head)
        return root

    rs.bind_review(REPO, PR, sha, warm=False)
    s = rs.RepoSearch({"repo_search_checkout_wait_s": 0.05}, resolve_checkout=slow)
    out = await s.search("helper", PR, REPO)
    assert out.startswith(rs.ERROR) and "still being built" in out
    release.set()
    await asyncio.sleep(0.05)
    assert finished == [sha]  # shielded: not cancelled with the call


@pytest.mark.parametrize("pattern", ["", "   ", "a\nb", "x" * 600])
async def test_a_bad_pattern_is_an_error(tmp_path, pattern):
    root, sha = _repo(tmp_path)
    assert (await _searcher(root, sha).search(pattern, PR, REPO)).startswith(rs.ERROR)


# ── registration and the host's inheritance contract ────────────────────────────


def _host_is_read_tool(tool) -> bool:
    """protoAgent's `graph.tool_access.is_read_tool` (#4327): what a subagent inherits."""
    return (getattr(tool, "metadata", None) or {}).get("access") == "read"


def test_register_adds_both_tools_marked_read_only():
    reg = FakeRegistry({})
    pr_reviewer.register(reg)
    tools = {t.name: t for t in reg.tools}
    for name in ("search_pr_repo", "list_pr_repo_files"):
        assert name in tools, name
        assert _host_is_read_tool(tools[name]), name
        assert "SEARCH ERROR" in tools[name].description and "NOT evidence" in tools[name].description
    assert not _host_is_read_tool(tools["protopatch_review"])  # an expensive engine is never inherited


async def test_the_registered_tool_degrades_with_no_checkout():
    # The suite's resolver finds no checkout (tests/conftest.py): through the real registration
    # path the call is an explicit SEARCH ERROR, never "(no matches)".
    reg = FakeRegistry({})
    pr_reviewer.register(reg)
    tool = next(t for t in reg.tools if t.name == "search_pr_repo")
    rs.bind_review(REPO, PR, "b" * 40, warm=False)
    out = await tool.ainvoke({"pattern": "helper", "pr": PR, "repo": REPO})
    assert out.startswith(rs.ERROR) and rs.NO_MATCHES not in out


async def test_the_tools_search_a_real_checkout(tmp_path):
    root, sha = _repo(tmp_path)
    search, listing = rs.get_tools({}, searcher=_searcher(root, sha))
    out = await search.ainvoke({"pattern": "def test_", "pr": PR, "repo": REPO, "path": "tests/"})
    assert out.startswith(rs.RESULTS) and "tests/test_core.py:4: def test_helper():" in out
    assert (await listing.ainvoke({"pr": PR, "repo": REPO, "path": "app"})).startswith(rs.FILES)


async def test_a_round_binds_its_head_for_the_lanes(tmp_path):
    seen: dict = {}

    async def runner(name, inputs):
        seen["head"] = rs.bound_head(inputs["repo"], int(inputs["pr"]))
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=RoutedGH(pr_facts=facts()), runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert seen.get("head") == HEAD


# ── the recipe tells every lane to use it ───────────────────────────────────────


def _recipe() -> dict:
    path = Path(__file__).resolve().parent.parent / "workflows" / "code-review-structural.yaml"
    return {s["id"]: s for s in yaml.safe_load(path.read_text())["steps"]}


def test_every_finder_and_the_verifier_are_told_to_search_the_pr_head():
    steps = _recipe()
    for sid in ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions", "verify"):
        prompt = steps[sid]["prompt"]
        assert "search_pr_repo" in prompt and "list_pr_repo_files" in prompt, sid
        assert "pr={{inputs.pr}}" in prompt and 'repo="{{inputs.repo}}"' in prompt, sid
        assert "`search_files`" in prompt, sid  # named as the tool that does NOT see the PR
    for sid in ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions"):
        prompt = steps[sid]["prompt"]
        assert "SEARCH ERROR" in prompt and "not a finding" in prompt, sid
    assert "SEARCH ERROR" in steps["verify"]["prompt"]


async def test_a_git_without_pcre_falls_back_to_extended_regex(tmp_path):
    root, sha = _repo(tmp_path)
    modes: list = []

    async def run_git(args, max_lines, timeout_s):
        if "ls-tree" in args:
            return 0, ["app/core.py"], "", False
        modes.append("-P" if "-P" in args else "-E")
        if "-P" in args:
            return 128, [], "fatal: cannot use Perl-compatible regexes when not compiled with USE_LIBPCRE", False
        return 0, [f"{sha}:app/core.py\x001\x00def helper():"], "", False

    out = await _searcher(root, sha, run_git=run_git).search("helper", PR, REPO)
    assert modes == ["-P", "-E"] and out.startswith(rs.RESULTS) and "regex (ERE)" in out


# ── a matched line longer than asyncio's stream limit (#337) ───────────────────────────────────


async def test_a_hit_on_a_line_past_the_stream_limit_is_cut_not_an_error(tmp_path):
    # `StreamReader.readline()` raises `ValueError: Separator is found, but chunk is longer than
    # limit` past 64 KiB and discards the data: the whole search failed, the other hits with it.
    huge = "needle " + "x" * 200_000
    root, sha = _repo(tmp_path, {"dist/bundle.min.js": huge + "\n", "src/a.py": "needle = 1\n", "src/z.py": "needle\n"})
    out = await _searcher(root, sha).search("needle", PR, REPO)
    assert "ValueError" not in out and "unexpected" not in out
    assert "dist/bundle.min.js:1:" in out and "[line cut]" in out
    assert "src/a.py:1:" in out and "src/z.py:1:" in out  # the hits after the long line survive
    assert len(out) < 4000


async def test_stdout_lines_reads_any_length_and_any_chunking():
    class Chunks:
        def __init__(self, *chunks):
            self.chunks = list(chunks)

        async def read(self, n):
            return self.chunks.pop(0) if self.chunks else b""

    async def lines(*chunks, keep=8):
        return [line async for line in rs._stdout_lines(Chunks(*chunks), keep)]

    assert await lines(b"a\nbb\n") == [b"a", b"bb"]
    assert await lines(b"a\nb", b"b\n", b"tail") == [b"a", b"bb", b"tail"]  # split mid-line, no final newline
    assert await lines(b"0123456789abc\nx\n") == [b"01234567", b"x"]  # cut to `keep`, the rest dropped
    assert await lines(b"01234", b"56789", b"abc\n", b"y\n") == [b"01234567", b"y"]  # a long line across chunks
    assert await lines(b"\n\nq\n") == [b"", b"", b"q"]
    assert await lines() == []

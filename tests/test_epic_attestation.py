"""Epic attestation — an `epic/*` → default-branch PR is reviewed by its RESIDUAL only.

Every slice of an epic already passed the panel in its own PR; the epic → main PR must not
pay for the whole epic again. These tests pin the attribution (slice / sync / residual) on a
REAL git repository — a mock-only git seam would ship an inert fix — with GitHub faked, and
the dispatcher's two outcomes: an all-attested PASS posted through the normal verdict path,
and a panel scoped to the residual diff. Every unreadable fact falls back to a FULL review.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from pr_reviewer import epic
from pr_reviewer.dispatch import Dispatcher
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.verdicts import parse_verdict_marker, render_verdict_body

from tests.dispatch_helpers import _CLEAN_STEPS, RoutedGH, facts
from tests.git_helpers import _ENV, resolver_for

# ── a real repository ────────────────────────────────────────────────────────────


class Repo:
    def __init__(self, root: Path):
        self.root = root
        self.env = {**os.environ, **_ENV}
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, check: bool = True) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.root), *args], env=self.env, capture_output=True, text=True, check=False
        )
        if check and out.returncode != 0:
            raise AssertionError(f"git {args}: {out.stderr}")
        return out.stdout.strip()

    def commit(self, message: str, files: dict[str, str]) -> str:
        for rel, text in files.items():
            (self.root / rel).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def merge(self, branch: str, message: str) -> str:
        self.git("merge", "-q", "--no-ff", "--no-edit", "-m", message, branch)
        return self.git("rev-parse", "HEAD")

    def clone_at(self, dest: Path, sha: str) -> Path:
        """A checkout the way the checkout cache makes one: a clone of `origin`, detached
        at the head under review."""
        subprocess.run(["git", "clone", "-q", str(self.root), str(dest)], env=self.env, check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", "--detach", sha], env=self.env, check=True)
        return dest


def build_epic(tmp_path: Path, *, residual: bool) -> tuple[Repo, dict[str, str]]:
    """main ← epic/x with two squash slices around a clean sync merge of main; with
    `residual`, also a direct push, a slice that touches f.txt, and a conflict-resolution
    merge of main (both sides changed f.txt line 2)."""
    r = Repo(tmp_path / "origin")
    sha: dict[str, str] = {}
    sha["base"] = r.commit("base", {"a.txt": "a\n", "f.txt": "one\ntwo\nthree\n"})
    r.git("checkout", "-q", "-b", "epic/x")
    sha["s1"] = r.commit("slice one (#21)", {"s1.py": "SLICE_ONE = 1\n"})
    r.git("checkout", "-q", "main")
    sha["m1"] = r.commit("main moves", {"m1.txt": "main\n"})
    r.git("checkout", "-q", "epic/x")
    sha["sync"] = r.merge("main", "sync main into epic")
    sha["s2"] = r.commit("slice two (#22)", {"s2.py": "SLICE_TWO = 2\n"})
    if residual:
        sha["direct"] = r.commit("hotfix straight onto the epic", {"direct.py": "DIRECT_PUSH = 'unreviewed'\n"})
        sha["s3"] = r.commit("slice three (#23)", {"f.txt": "one\nepic-two\nthree\n"})
        r.git("checkout", "-q", "main")
        sha["m2"] = r.commit("main edits f", {"f.txt": "one\nmain-two\nthree\n"})
        r.git("checkout", "-q", "epic/x")
        r.git("merge", "-q", "--no-ff", "--no-edit", "main", check=False)  # conflicts on f.txt
        (r.root / "f.txt").write_text("one\nRESOLVED-two\nthree\n")
        r.git("add", "-A")
        r.git("commit", "-q", "--no-edit")
        sha["conflict"] = r.git("rev-parse", "HEAD")
    sha["head"] = r.git("rev-parse", "HEAD")
    return r, sha


def pass_round(complete: bool = True, verified: bool = True, verdict: str = "PASS") -> dict:
    return {"verdict": verdict, "complete": complete, "verified": verified}


def pulls(number: int, sha: str, *, base: str = "epic/x", head: str = "") -> list[dict]:
    return [
        {
            "number": number,
            "merged_at": "2026-10-01T00:00:00Z",
            "merge_commit_sha": sha,
            "base": base,
            "head": head or f"{number:02d}".ljust(40, "c"),
        }
    ]


def github(slices: dict[str, list[dict]], rounds: dict[int, dict | None]):
    async def pulls_for(sha: str) -> list[dict]:
        return slices.get(sha, [])

    async def slice_round(number: int, head: str) -> dict | None:
        return rounds.get(number)

    return pulls_for, slice_round


async def scope_for(tmp_path, repo: Repo, sha: dict, slices, rounds, *, budget: int = 200_000) -> epic.EpicScope:
    from pr_reviewer.checkout_cache import _default_run_git

    work = repo.clone_at(tmp_path / "work", sha["head"])
    pulls_for, slice_round = github(slices, rounds)
    return await epic.build_scope(
        _default_run_git,
        work,
        epic_ref="epic/x",
        base_ref="main",
        base_sha="",
        head=sha["head"],
        pulls_for=pulls_for,
        slice_round=slice_round,
        diff_budget=budget,
    )


# ── attribution on real git ──────────────────────────────────────────────────────


async def test_an_epic_of_passed_slices_and_a_clean_sync_is_fully_attested(tmp_path):
    repo, sha = build_epic(tmp_path, residual=False)
    slices = {sha["s1"]: pulls(21, sha["s1"]), sha["s2"]: pulls(22, sha["s2"])}
    scope = await scope_for(tmp_path, repo, sha, slices, {21: pass_round(), 22: pass_round()})

    assert [(a.sha, a.kind) for a in scope.commits] == [
        (sha["s1"], epic.SLICE),
        (sha["sync"], epic.SYNC),
        (sha["s2"], epic.SLICE),
    ]
    assert scope.all_attested
    assert scope.residual_diff == "" and scope.residual_paths == []
    table = epic.render_attestation_table(scope)
    assert f"`{sha['s1'][:12]}`" in table and "#21" in table and "#22" in table and "clean sync merge" in table


async def test_a_direct_push_and_a_conflict_resolution_are_the_residual(tmp_path):
    repo, sha = build_epic(tmp_path, residual=True)
    slices = {
        sha["s1"]: pulls(21, sha["s1"]),
        sha["s2"]: pulls(22, sha["s2"]),
        sha["s3"]: pulls(23, sha["s3"]),
    }
    scope = await scope_for(tmp_path, repo, sha, slices, {21: pass_round(), 22: pass_round(), 23: pass_round()})

    kinds = {a.sha: a.kind for a in scope.commits}
    assert kinds[sha["s1"]] == kinds[sha["s2"]] == kinds[sha["s3"]] == epic.SLICE
    assert kinds[sha["sync"]] == epic.SYNC
    assert kinds[sha["direct"]] == epic.RESIDUAL
    assert kinds[sha["conflict"]] == epic.RESIDUAL
    assert not scope.all_attested
    # The residual diff is the direct push plus the RESOLUTION — not the slices' content,
    # and not main's own (already reviewed) side of the merge.
    assert "DIRECT_PUSH" in scope.residual_diff
    assert "RESOLVED-two" in scope.residual_diff
    assert "SLICE_ONE" not in scope.residual_diff and "SLICE_TWO" not in scope.residual_diff
    assert "m1.txt" not in scope.residual_diff
    assert set(scope.residual_paths) == {"direct.py", "f.txt"}
    block = epic.render_scope_block(scope)
    assert block.startswith("<review_scope>") and block.endswith("</review_scope>")
    assert sha["direct"][:12] in block and "OUT OF SCOPE" in block


async def test_a_slice_whose_pass_was_incomplete_is_reviewed_again(tmp_path):
    # `complete=false` is the `QA panel` check's `neutral`: a lane never ran, so the slice's
    # code was not fully reviewed — it is residual, and its diff joins the scope.
    repo, sha = build_epic(tmp_path, residual=False)
    slices = {sha["s1"]: pulls(21, sha["s1"]), sha["s2"]: pulls(22, sha["s2"])}
    scope = await scope_for(tmp_path, repo, sha, slices, {21: pass_round(), 22: pass_round(complete=False)})

    s2 = next(a for a in scope.commits if a.sha == sha["s2"])
    assert s2.kind == epic.RESIDUAL and s2.pr == 22 and "incomplete" in s2.verdict
    assert "SLICE_TWO" in scope.residual_diff and "SLICE_ONE" not in scope.residual_diff
    assert scope.residual_paths == ["s2.py"]


@pytest.mark.parametrize(
    "round_",
    [None, pass_round(verdict="WARN"), pass_round(verdict="FAIL"), pass_round(verified=False)],
    ids=["no-verdict", "warn", "fail", "unverified"],
)
async def test_only_a_complete_verified_pass_attests_a_slice(tmp_path, round_):
    repo, sha = build_epic(tmp_path, residual=False)
    slices = {sha["s1"]: pulls(21, sha["s1"]), sha["s2"]: pulls(22, sha["s2"])}
    scope = await scope_for(tmp_path, repo, sha, slices, {21: pass_round(), 22: round_})
    assert [a.sha for a in scope.residual] == [sha["s2"]]


async def test_a_pr_merged_elsewhere_does_not_attest_the_commit(tmp_path):
    # GitHub associates a commit with every PR that CONTAINS it. Only the PR into THIS
    # epic whose merge commit IS the commit speaks for it.
    repo, sha = build_epic(tmp_path, residual=False)
    slices = {
        sha["s1"]: pulls(21, sha["s1"], base="main"),  # merged into main, not the epic
        sha["s2"]: [{**pulls(22, sha["s2"])[0], "merge_commit_sha": sha["s1"]}],  # a different merge commit
    }
    scope = await scope_for(tmp_path, repo, sha, slices, {21: pass_round(), 22: pass_round()})
    assert {a.sha for a in scope.residual} == {sha["s1"], sha["s2"]}


async def test_a_clean_merge_of_an_unreviewed_branch_is_residual_with_its_content(tmp_path):
    repo, sha = build_epic(tmp_path, residual=False)
    repo.git("checkout", "-q", "-b", "side", sha["s2"])
    repo.commit("side work", {"side.py": "SIDE = 'never reviewed'\n"})
    repo.git("checkout", "-q", "epic/x")
    sha["side-merge"] = repo.merge("side", "merge side branch")
    sha["head"] = sha["side-merge"]
    slices = {sha["s1"]: pulls(21, sha["s1"]), sha["s2"]: pulls(22, sha["s2"])}
    scope = await scope_for(tmp_path, repo, sha, slices, {21: pass_round(), 22: pass_round()})

    merged = next(a for a in scope.commits if a.sha == sha["side-merge"])
    assert merged.kind == epic.RESIDUAL  # clean, but its second parent is not on main
    assert "never reviewed" in scope.residual_diff and scope.residual_paths == ["side.py"]


async def test_the_residual_diff_is_bounded_by_the_budget(tmp_path):
    repo, sha = build_epic(tmp_path, residual=True)
    slices = {sha[k]: pulls(n, sha[k]) for k, n in (("s1", 21), ("s2", 22), ("s3", 23))}
    scope = await scope_for(
        tmp_path, repo, sha, slices, {21: pass_round(), 22: pass_round(), 23: pass_round()}, budget=80
    )
    assert scope.diff_truncated and len(scope.residual_diff) == 80
    assert "TRUNCATED" in epic.render_scope_block(scope)


async def test_an_unreadable_github_lookup_raises_rather_than_attesting(tmp_path):
    from pr_reviewer.checkout_cache import _default_run_git

    repo, sha = build_epic(tmp_path, residual=False)
    work = repo.clone_at(tmp_path / "work", sha["head"])

    async def pulls_for(sha_: str) -> list[dict]:
        raise epic.AttestationUnavailable("HTTP 502")

    async def slice_round(number, head):
        return pass_round()

    with pytest.raises(epic.AttestationUnavailable):
        await epic.build_scope(
            _default_run_git,
            work,
            epic_ref="epic/x",
            base_ref="main",
            base_sha="",
            head=sha["head"],
            pulls_for=pulls_for,
            slice_round=slice_round,
            diff_budget=200_000,
        )


# ── pure pieces ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({}, True),
        ({"head_ref": "feat/x"}, False),
        ({"head_ref": "epic/"}, False),
        ({"base_ref": "epic/parent"}, False),  # a nested epic → epic PR
        ({"default_branch": ""}, False),  # unknown default branch: not provably an epic PR
        ({"head_repo": "fork/r"}, False),  # a fork's epic/* had no slice PRs here
    ],
)
def test_is_epic_pr(over, expected):
    base = {"head_ref": "epic/x", "base_ref": "main", "default_branch": "main", "head_repo": "o/r"}
    assert epic.is_epic_pr({**base, **over}, "o/r") is expected


async def test_attribution_needs_no_github_read_for_a_sync_merge():
    calls: list[str] = []
    commits = [epic.Commit("a" * 40, ["0" * 40]), epic.Commit("b" * 40, ["a" * 40, "f" * 40])]

    async def is_sync(c):
        return c.sha == "b" * 40

    async def pulls_for(sha):
        calls.append(sha)
        return pulls(21, sha)

    async def slice_round(number, head):
        return pass_round()

    out = await epic.attribute_commits(
        commits, epic_ref="epic/x", is_sync=is_sync, pulls_for=pulls_for, slice_round=slice_round
    )
    assert [a.kind for a in out] == [epic.SLICE, epic.SYNC]
    assert calls == ["a" * 40]


# ── the dispatcher, end to end (real git, faked GitHub) ──────────────────────────


def slice_review(head: str, *, verdict="PASS", complete=True) -> dict:
    body = render_verdict_body(
        repo="o/r",
        pr=21,
        head_sha=head,
        verdict=verdict,
        brief="prose",
        findings=[],
        shadow=True,
        recipe="code-review-structural",
        complete=complete,
    )
    return {"state": "COMMENTED", "body": body, "id": 5, "author": "qa-bot"}


class EpicGH(RoutedGH):
    """RoutedGH plus the two reads attestation makes: `commits/{sha}/pulls` and a slice
    PR's own reviews (`pulls/{n}/reviews`, n ≥ 20)."""

    def __init__(self, *, commit_pulls: dict[str, list[dict]], slice_reviews: dict[int, list[dict]], **kw):
        super().__init__(**kw)
        self.commit_pulls, self.slice_reviews = commit_pulls, slice_reviews
        self.pulls_rc = 0

    async def __call__(self, args, timeout=30):
        url = args[1] if len(args) > 1 else ""
        if "/commits/" in url and url.endswith("/pulls"):
            self.calls.append(args)
            if self.pulls_rc:
                return self.pulls_rc, "", "HTTP 502"
            sha = url.split("/commits/")[1].split("/")[0]
            return 0, "\n".join(json.dumps(p) for p in self.commit_pulls.get(sha, [])), ""
        for number, rows in self.slice_reviews.items():
            if url == f"repos/o/r/pulls/{number}/reviews":
                self.calls.append(args)
                return 0, json.dumps(rows), ""
        return await super().__call__(args, timeout)


def epic_dispatcher(tmp_path, repo: Repo, sha: dict, gh, runner, cfg=None) -> Dispatcher:
    work = repo.clone_at(tmp_path / "work", sha["head"])
    d = Dispatcher(
        {"repos": ["o/r"], "cooldown_s": 30, **(cfg or {})},
        Telemetry(tmp_path),
        run_gh_fn=gh,
        workflow_run=runner,
        resolve_checkout=resolver_for(work),
    )
    return d


def epic_facts(head: str) -> dict:
    return facts(head=head, head_ref="epic/x", head_repo="o/r", default_branch="main", base_ref="main")


def recording_runner():
    calls: list[tuple[str, dict]] = []

    async def runner(name, inputs):
        calls.append((name, inputs))
        report = "<!-- brief -->\nResidual reviewed.\n<!-- /brief -->\n\n```json\n[]\n```"
        return {"output": report, "steps": {**_CLEAN_STEPS, "report": report}, "failed": []}

    return runner, calls


async def test_an_all_attested_epic_posts_a_pass_with_the_table_and_runs_no_panel(tmp_path):
    repo, sha = build_epic(tmp_path, residual=False)
    slice_heads = {21: "1" * 40, 22: "2" * 40}
    gh = EpicGH(
        pr_facts=epic_facts(sha["head"]),
        reviews=[],
        commit_pulls={sha["s1"]: pulls(21, sha["s1"], head="1" * 40), sha["s2"]: pulls(22, sha["s2"], head="2" * 40)},
        slice_reviews={n: [slice_review(h)] for n, h in slice_heads.items()},
    )
    runner, calls = recording_runner()
    d = epic_dispatcher(tmp_path, repo, sha, gh, runner)

    assert await d._review("o/r", 1) == "attested:PASS"
    assert calls == []  # no panel spent
    [posted] = gh.reviews_posted
    marker = parse_verdict_marker(posted["body"])
    # A real verdict marker AT the head — what "Review at head" and the QA panel gate read.
    assert marker["head"] == sha["head"] and marker["verdict"] == "PASS"
    assert marker["complete"] and marker["verified"]
    assert "Epic attestation" in posted["body"]
    for n in (21, 22):
        assert f"#{n}" in posted["body"]
    assert sha["sync"][:12] in posted["body"]


async def test_an_epic_with_residual_commits_runs_the_panel_on_the_residual_only(tmp_path):
    repo, sha = build_epic(tmp_path, residual=True)
    heads = {21: "1" * 40, 22: "2" * 40, 23: "3" * 40}
    gh = EpicGH(
        pr_facts=epic_facts(sha["head"]),
        reviews=[],
        commit_pulls={sha[k]: pulls(n, sha[k], head=heads[n]) for k, n in (("s1", 21), ("s2", 22), ("s3", 23))},
        slice_reviews={n: [slice_review(h)] for n, h in heads.items()},
    )
    runner, calls = recording_runner()
    d = epic_dispatcher(tmp_path, repo, sha, gh, runner)

    assert await d._review("o/r", 1) == "reviewed:PASS"
    [(recipe, inputs)] = calls
    assert recipe == "code-review-structural"  # the recipe that reads `review_scope`
    scope = inputs["review_scope"]
    assert "DIRECT_PUSH" in scope and "RESOLVED-two" in scope and "SLICE_ONE" not in scope
    [posted] = gh.reviews_posted
    body = posted["body"]
    assert "Epic attestation" in body and "reviewed only the residual diff of the other 2 commit(s)" in body
    assert sha["direct"][:12] in body and sha["conflict"][:12] in body and "#23" in body


async def test_an_unreadable_lookup_falls_back_to_a_full_review(tmp_path):
    repo, sha = build_epic(tmp_path, residual=False)
    gh = EpicGH(pr_facts=epic_facts(sha["head"]), reviews=[], commit_pulls={}, slice_reviews={})
    gh.pulls_rc = 1
    runner, calls = recording_runner()
    d = epic_dispatcher(tmp_path, repo, sha, gh, runner)

    assert await d._review("o/r", 1) == "reviewed:PASS"
    [(_recipe, inputs)] = calls
    assert "review_scope" not in inputs  # the WHOLE PR, never an attested PASS
    assert "Epic attestation" not in gh.reviews_posted[0]["body"]


async def test_an_unreadable_slice_history_falls_back_to_a_full_review(tmp_path):
    repo, sha = build_epic(tmp_path, residual=False)

    class BrokenSliceReviews(EpicGH):
        async def __call__(self, args, timeout=30):
            if len(args) > 1 and args[1] == "repos/o/r/pulls/21/reviews":
                return 1, "", "HTTP 502"
            return await super().__call__(args, timeout)

    gh = BrokenSliceReviews(
        pr_facts=epic_facts(sha["head"]),
        reviews=[],
        commit_pulls={sha["s1"]: pulls(21, sha["s1"]), sha["s2"]: pulls(22, sha["s2"])},
        slice_reviews={22: [slice_review(pulls(22, "x")[0]["head"])]},
    )
    runner, calls = recording_runner()
    d = epic_dispatcher(tmp_path, repo, sha, gh, runner)
    assert await d._review("o/r", 1) == "reviewed:PASS"
    assert "review_scope" not in calls[0][1]


@pytest.mark.parametrize("off", [{"epic_attestation": False}, {"epic_attestation": "off"}])
async def test_the_switch_turns_attestation_off(tmp_path, off):
    repo, sha = build_epic(tmp_path, residual=False)
    gh = EpicGH(
        pr_facts=epic_facts(sha["head"]),
        reviews=[],
        commit_pulls={sha["s1"]: pulls(21, sha["s1"]), sha["s2"]: pulls(22, sha["s2"])},
        slice_reviews={},
    )
    runner, calls = recording_runner()
    d = epic_dispatcher(tmp_path, repo, sha, gh, runner, cfg=off)
    assert await d._review("o/r", 1) == "reviewed:PASS"
    assert "review_scope" not in calls[0][1]
    assert not any(c[1].startswith("repos/o/r/commits/") and c[1].endswith("/pulls") for c in gh.calls)


def test_the_env_switch_defaults_on(tmp_path, monkeypatch):
    d = Dispatcher({"repos": ["o/r"]}, Telemetry(tmp_path), run_gh_fn=RoutedGH(pr_facts=facts()))
    monkeypatch.delenv("PR_REVIEWER_EPIC_ATTESTATION", raising=False)
    assert d.epic_attestation is True
    monkeypatch.setenv("PR_REVIEWER_EPIC_ATTESTATION", "false")
    assert d.epic_attestation is False


async def test_a_summon_reviews_the_whole_epic(tmp_path):
    # `force` is an operator disputing the verdict: the whole PR, not an attestation.
    repo, sha = build_epic(tmp_path, residual=False)
    gh = EpicGH(
        pr_facts=epic_facts(sha["head"]),
        reviews=[],
        commit_pulls={sha["s1"]: pulls(21, sha["s1"]), sha["s2"]: pulls(22, sha["s2"])},
        slice_reviews={},
    )
    runner, calls = recording_runner()
    d = epic_dispatcher(tmp_path, repo, sha, gh, runner)
    assert await d._review("o/r", 1, force=True) == "reviewed:PASS"
    assert "review_scope" not in calls[0][1]

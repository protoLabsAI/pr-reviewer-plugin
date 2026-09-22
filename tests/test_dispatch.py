"""The dispatcher — gate order, recall, exhaustion, posting, promotion. All GitHub
reads/writes go through a canned fake `gh`; the workflow runner is a stub."""

from __future__ import annotations

import asyncio
import json
import time

from pr_reviewer.dispatch import POST_MAX_FAILURES, Dispatcher
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.verdicts import extract_findings_json, parse_verdict_marker, render_verdict_body, verdict_for

from tests.conftest import note_write

HEAD = "a" * 40
OLD_HEAD = "b" * 40

REPORT = (
    "<!-- brief -->\nBrief prose.\n<!-- /brief -->\n\n```json\n"
    + json.dumps(
        [
            {
                "file": "x.py",
                "line": 3,
                "severity": "major",
                "category": "correctness",
                "claim": "Bug.",
                "evidence": "e",
                "verdict": "confirmed",
            }
        ]
    )
    + "\n```"
)


class FakeGH:
    """Canned `gh api` responses keyed by URL substring; records every call."""

    def __init__(self, responses=None):
        self.calls: list[list[str]] = []
        self.posted: list[dict] = []
        self.responses = responses or {}

    @property
    def reviews_posted(self) -> list[dict]:
        """The POSTs that were REVIEWS. `posted` also carries the `QA panel` check-run
        writes now, so a test that means "the review we posted" must filter — asserting
        on `posted[0]` would silently start reading a different write."""
        return [p for p in self.posted if "/reviews" in p.get("url", "")]

    async def __call__(self, args, timeout=30):
        self.calls.append(args)
        if note_write(args):
            return 1, "", "unexpected write (tests/conftest.py KNOWN_WRITES)"
        url = args[1] if len(args) > 1 else ""
        if "-X" in args and "POST" in args and "/reviews" in url:
            fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("query=")}
            self.posted.append({"url": url, **fields})
            return 0, "{}", ""
        for key, value in self.responses.items():
            if key in " ".join(args):
                return 0, value if isinstance(value, str) else json.dumps(value), ""
        return 0, "", ""


async def _no_sleep(_delay):
    """Retry backoff, without the wall-clock cost, for the post-retry tests."""
    return None


def facts(**over):
    base = {
        "head": HEAD,
        "base_ref": "main",
        "state": "open",
        "draft": False,
        "locked": False,
        "changed_files": 2,
        "additions": 10,
        "deletions": 5,
        "author": "someone",
    }
    base.update(over)
    return base


def make(tmp_path, *, cfg=None, gh=None, runner=None, inbox=None):
    async def default_runner(name, inputs):
        return {"output": REPORT, "steps": {}, "failed": []}

    d = Dispatcher(
        {"repos": ["o/r"], "cooldown_s": 30, **(cfg or {})},
        Telemetry(tmp_path),
        run_gh_fn=gh or FakeGH(),
        workflow_run=runner or default_runner,
        inbox_add=inbox,
    )
    return d


# ── gate order ────────────────────────────────────────────────────────────────


async def test_unlisted_repo_drops_before_any_github_call(tmp_path):
    gh = FakeGH()
    d = make(tmp_path, gh=gh)
    out = await d.handle_pr_event("evil/repo", 1, HEAD, "opened")
    assert out == "drop:unlisted-repo"
    assert gh.calls == []  # the allowlist gate ran first


async def test_non_dispatch_actions_drop(tmp_path):
    d = make(tmp_path)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "labeled")) == "drop:not-a-dispatch-action"


async def test_an_unknown_viewer_login_stops_the_review_instead_of_disabling_the_rail(tmp_path):
    """A failed `api user` lookup used to cache "" forever (the retry was gated on
    `is None`), so ONE transient failure permanently disabled the never-review-your-own-PR
    rail — and as promotion owner that is self-approval. Same shape as issue #71: a failed
    read remembered as a definitive answer."""

    class NoViewerGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            if len(args) > 1 and args[1] == "user":
                return 1, "", "HTTP 503"
            return await super().__call__(args, timeout)

    gh = NoViewerGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:viewer-unknown"
    assert gh.posted == []
    assert d._viewer is None  # NOT "" — caching "" as the answer is the bug itself

    # …and once the lookup recovers, the rail works again in the SAME process — the
    # point of the fix: the earlier failure must not have poisoned the cache. A second
    # SHA, because the chokepoint cooldown is keyed repo#pr@sha and would drop a replay.
    gh2 = RoutedGH(pr_facts=facts(head=OLD_HEAD, author="qa-bot[bot]"), reviews=[])
    d._run_gh = gh2
    assert (await d.handle_pr_event("o/r", 1, OLD_HEAD, "opened")) == "drop:self-authored"


async def test_configured_viewer_login_is_used_without_probing(tmp_path):
    """On GitHub App auth `gh api user` 403s every time — an installation token is not
    a user — so identity has to be configured. Without this the fail-closed rail drops
    every eligible PR, which it did in production for ~30 events before this landed."""

    class NoUserGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            if len(args) > 1 and args[1] == "user":
                return 1, "", "HTTP 403: Resource not accessible by integration"
            return await super().__call__(args, timeout)

    gh = NoUserGH(pr_facts=facts(author="qa-bot[bot]"), reviews=[])
    d = make(tmp_path, cfg={"viewer_login": "QA-Bot[bot]"}, gh=gh)
    # Configured identity ⇒ the rail works even though the probe cannot: this PR is ours.
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:self-authored"
    assert not any(a[1] == "user" for a in gh.calls if len(a) > 1)  # never probed

    # …and a PR by someone else still reviews normally.
    gh2 = NoUserGH(pr_facts=facts(author="someone"), reviews=[])
    d2 = make(tmp_path, cfg={"viewer_login": "qa-bot[bot]"}, gh=gh2)
    assert (await d2.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")


async def test_self_authored_pr_drops(tmp_path):
    # RoutedGH's viewer login is "qa-bot" — a PR authored by qa-bot[bot] is ours.
    gh = RoutedGH(pr_facts=facts(author="qa-bot[bot]"))
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:self-authored"
    assert gh.posted == []


# ── recall: reaffirm + delta ──────────────────────────────────────────────────


def review_row(head, verdict, state="COMMENTED", findings_json="", id=None, complete=True, diff_id=""):
    body = render_verdict_body(
        repo="o/r",
        pr=1,
        head_sha=head,
        verdict=verdict,
        brief="prose",
        findings=json.loads(findings_json or "[]"),
        shadow=True,
        recipe="code-review",
        complete=complete,
        diff_id=diff_id,
    )
    return {"state": state, "body": body, "id": id}


def thread_node(author, *, resolved=False):
    """A review-thread node shaped like the GraphQL fetch returns — a single root comment
    by `author`. `count_unresolved_threads` reads only `isResolved`; thread OWNERSHIP
    (issue #105) reads the root comment's author to decide whose thread it is."""
    return {"isResolved": resolved, "comments": {"nodes": [{"author": {"login": author}, "body": "…"}]}}


class RoutedGH(FakeGH):
    def __init__(
        self,
        *,
        pr_facts,
        reviews=None,
        checks=None,
        files="x.py\n",
        threads=None,
        compare=None,
        reviews_rc=0,
        reviews_err="",
        post_results=None,
    ):
        super().__init__()
        self.pr_facts, self.reviews, self.checks, self.files = pr_facts, reviews or [], checks, files
        self.threads = threads  # None → an empty connection: no threads, so nothing unresolved
        self.compare = compare  # None → the compare read fails (no convergence relief)
        self.dismissed: list[str] = []
        # reviews_rc != 0 → the reviews READ fails, the shape that made `_our_reviews`
        # fail open before issue #71. Kept separate from `reviews=[]`, which is the
        # legitimate "this PR has no reviews".
        self.reviews_rc, self.reviews_err = reviews_rc, reviews_err
        # Author stamped on served reviews — feeds the viewer_login cross-check.
        self.review_author = "qa-bot"
        # Successive (rc, err) results for the verdict POST — for the retry path (#72).
        self.post_results = list(post_results or [])
        # The dispatch-path `protoReview` check writes (#95), captured SEPARATELY from
        # `posted`: those are reviews/comments, and a test asserting on `posted[0]` must
        # not start reading a check write. Only OUR check (name=protoReview / a PATCH to
        # an id we handed out) is intercepted; the `QA panel` promotion check falls
        # through to the generic handlers unchanged.
        self.check_writes: list[dict] = []
        self._next_check_id = 1000
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
                return 0, str(cid), ""  # `--jq .id` yields the bare id
            tail = args[1].rsplit("/", 1)[-1] if len(args) > 1 else ""
            if "PATCH" in args and tail.isdigit() and int(tail) in self._review_check_ids:
                self.check_writes.append({"method": "PATCH", "url": args[1], **fields})
                return 0, "{}", ""
        if "-X" in args and "PUT" in args and "/dismissals" in joined:
            self.dismissed.append(args[1])
            return 0, "{}", ""
        if "-X" in args and "POST" in args:
            fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("query=")}
            # Record the URL like FakeGH does: without it a test asserting on `posted`
            # cannot tell a review POST from any other write, so an unintended extra
            # write passes unnoticed — the exact duplicate-post risk under test here.
            self.posted.append({"url": args[1] if len(args) > 1 else "", **fields})
            if self.post_results:
                rc, err = self.post_results.pop(0)
                return rc, "" if rc else "{}", err
            return 0, "{}", ""
        if args[1] == "user":
            return 0, "qa-bot", ""
        if "/compare/" in joined:
            return (0, json.dumps(self.compare), "") if self.compare is not None else (1, "", "404")
        if "/files" in joined:
            return 0, self.files, ""
        if "/reviews" in joined:
            if self.reviews_rc:
                return self.reviews_rc, "", self.reviews_err
            # `author` is what the viewer_login cross-check and the own-reviews filter read;
            # the real jq selects `.user.login` into that key. A row's own `author` wins, so a
            # test can serve a review written by another account.
            return 0, json.dumps([{"author": self.review_author, **r} for r in self.reviews]), ""
        if "/check-runs" in joined:
            return (0, json.dumps(self.checks), "") if self.checks is not None else (1, "", "403")
        if "comments(first" in joined:  # the threads fetch (before the count query below)
            # The jq selects the reviewThreads CONNECTION now (nodes + pageInfo), so the
            # fake serves one complete page rather than a bare node list.
            if self.threads is None:
                return 0, "\x00", ""
            page = {"pageInfo": {"hasNextPage": False, "endCursor": ""}, "nodes": self.threads}
            return 0, json.dumps(page), ""
        if "graphql" in joined:
            # The unresolved-COUNT query (isResolved only, no bodies). It now reads a
            # paginated connection like the fetch above, not a scalar count.
            nodes = [{"isResolved": bool(t.get("isResolved"))} for t in (self.threads or [])]
            return 0, json.dumps({"pageInfo": {"hasNextPage": False, "endCursor": ""}, "nodes": nodes}), ""
        if "/pulls/1" in joined:
            return 0, json.dumps(self.pr_facts), ""
        if "/pulls?" in joined:
            return 0, "[1]", ""
        return 0, "", ""


async def test_unchanged_head_reaffirms_without_spending_the_panel(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")])
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:PASS"
    assert ran == [] and gh.posted == []


async def test_advanced_head_runs_a_delta_review_with_prior_findings(tmp_path):
    prior = json.dumps([{"file": "x.py", "line": 1, "severity": "minor", "claim": "old", "evidence": "e"}])
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(OLD_HEAD, "WARN", findings_json=prior)])
    seen = {}

    async def runner(name, inputs):
        seen.update(name=name, inputs=inputs)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "reviewed:FAIL"
    assert "old" in seen["inputs"]["prior_findings"]


# ── reaffirm across a changed head with a byte-identical diff (issue #91) ──────


class DiffIdGH(RoutedGH):
    """Serves the pinned tree-SHA reads the diff-identity uses (issue #91): a head-commit
    tree per head SHA, and the merge-base tree of a base↔head compare. Everything else is
    RoutedGH. The fake pre-applies the `--jq`, same discipline as `pr_facts`/`files`, and
    routes on the jq so it never shadows the `/commits/{sha}/check-runs` or delta-`/compare/`
    reads that share those URL prefixes."""

    def __init__(self, *, head_trees=None, merge_base_tree="mb0", **kw):
        super().__init__(**kw)
        # {head_sha: tree_sha}; an absent head serves "" → the read fails closed to None.
        self.head_trees = head_trees or {}
        self.merge_base_tree = merge_base_tree

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "merge_base_commit.commit.tree.sha" in joined:  # _merge_base_tree
            self.calls.append(args)
            return 0, self.merge_base_tree, ""
        if ".commit.tree.sha" in joined and "/commits/" in joined:  # _commit_tree
            self.calls.append(args)
            ref = args[1].rsplit("/", 1)[-1]
            return 0, self.head_trees.get(ref, ""), ""
        return await super().__call__(args, timeout)


def _no_panel_runner(ran):
    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    return runner


async def test_a_rebased_head_with_a_byte_identical_diff_reaffirms(tmp_path):
    """A new head SHA (rebase / reworded commit / moved-but-identical base) whose base↔head
    tree pair is byte-identical to the last round reaffirms that verdict — no panel, and
    the verdict is RECORDED at the new head so the SHA-keyed gate can find it (issue #135:
    left unrecorded, 12 of 23 such heads sat `hold:stale-head` until a real-diff push)."""
    from pr_reviewer.rounds import diff_identity

    did = diff_identity("mbA", "treeA")
    gh = DiffIdGH(
        pr_facts=facts(),  # head=HEAD
        reviews=[review_row(OLD_HEAD, "PASS", diff_id=did)],
        head_trees={OLD_HEAD: "treeA", HEAD: "treeA"},  # same tree content, new head SHA
        merge_base_tree="mbA",
    )
    ran: list[str] = []
    d = make(tmp_path, gh=gh, runner=_no_panel_runner(ran))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:PASS"
    assert ran == []  # the panel never ran
    (post,) = gh.posted  # exactly one review: the carried verdict, at the NEW head
    assert post["event"] == "COMMENT"
    marker = parse_verdict_marker(post["body"])
    assert (marker["head"], marker["verdict"], marker["reaffirmed"]) == (HEAD, "PASS", OLD_HEAD)
    assert marker["diff_id"] == did and marker["complete"] is True
    assert "No panel was spent" in post["body"] and OLD_HEAD[:12] in post["body"]
    assert gh.check_writes == []  # no review check was opened, so none is concluded
    (row,) = _telemetry_events(tmp_path, "reaffirm-recorded")
    assert row["posted"] is True and row["prior_head"] == OLD_HEAD


async def test_an_incomplete_verdict_is_never_reaffirmed_the_push_runs_the_panel(tmp_path):
    """#179 (mythxengine#858): a round capped WARN complete=false said "the next push
    re-runs the full panel"; an identical-diff push was REAFFIRMED instead, so the only way
    to a complete pass was to change the content hash on purpose."""
    from pr_reviewer.rounds import diff_identity

    did = diff_identity("mbA", "treeA")
    for marker_tail, reason in (
        (" complete=false -->", "prior-incomplete"),
        (" verified=false -->", "prior-unverified"),
    ):
        prior = review_row(OLD_HEAD, "WARN", diff_id=did)
        prior["body"] = prior["body"].replace(" -->", marker_tail, 1)
        gh = DiffIdGH(
            pr_facts=facts(), reviews=[prior], head_trees={OLD_HEAD: "treeA", HEAD: "treeA"}, merge_base_tree="mbA"
        )
        ran: list[str] = []
        d = make(tmp_path / reason, gh=gh, runner=_no_panel_runner(ran))
        out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
        assert out == "reviewed:FAIL" and ran, (reason, out)  # the panel ran
        (miss,) = _telemetry_events(tmp_path / reason, "reaffirm-miss")
        assert miss["reason"] == reason
        assert _telemetry_events(tmp_path / reason, "reaffirm-recorded") == []


def _reaffirm_gh(prior_review, **kw):
    from pr_reviewer.rounds import diff_identity

    return DiffIdGH(
        pr_facts=facts(),
        reviews=[prior_review(diff_identity("mbA", "treeA"))],
        head_trees={OLD_HEAD: "treeA", HEAD: "treeA"},
        merge_base_tree="mbA",
        **kw,
    )


async def test_a_reaffirmed_fail_is_not_carried_to_the_new_head(tmp_path):
    # Fail-closed: a FAIL at the old head leaves the new head with NO verdict, as before.
    gh = _reaffirm_gh(lambda did: review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", diff_id=did))
    d = make(tmp_path, gh=gh, runner=_no_panel_runner([]))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:FAIL"
    assert gh.posted == [] and not _telemetry_events(tmp_path, "reaffirm-recorded")


async def test_an_incomplete_round_stays_incomplete_when_carried(tmp_path):
    # The carried verdict is no better than the round it came from: the gate still holds it.
    gh = _reaffirm_gh(lambda did: review_row(OLD_HEAD, "WARN", complete=False, diff_id=did))
    d = make(tmp_path, gh=gh, runner=_no_panel_runner([]))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:WARN"
    marker = parse_verdict_marker(gh.posted[0]["body"])
    assert marker["complete"] is False and marker["reaffirmed"] == OLD_HEAD


async def test_a_carried_verdict_never_dismisses_a_standing_block(tmp_path):
    # Nothing new was judged, so the reaffirm post must not lift anything (`hold_blocks`).
    gh = _reaffirm_gh(lambda did: review_row(OLD_HEAD, "PASS", diff_id=did))
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=_no_panel_runner([]))
    await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert gh.dismissed == []


async def test_the_carried_verdict_is_posted_once_not_on_every_event(tmp_path):
    # Second event for the same head: the recorded verdict IS this head's verdict now.
    from pr_reviewer.rounds import diff_identity

    did = diff_identity("mbA", "treeA")
    carried = review_row(HEAD, "PASS", diff_id=did)
    carried["body"] = carried["body"].replace(" -->", f" reaffirmed={OLD_HEAD} -->", 1)
    gh = DiffIdGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "PASS", diff_id=did), carried],
        head_trees={OLD_HEAD: "treeA", HEAD: "treeA"},
        merge_base_tree="mbA",
    )
    d = make(tmp_path, gh=gh, runner=_no_panel_runner([]))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:PASS"
    assert gh.posted == []


def test_a_carried_verdict_is_a_verdict_but_not_a_round():
    from pr_reviewer.rounds import panel_rounds, round_cap_reached, spent_rounds

    carried = review_row(HEAD, "PASS")
    carried["body"] = carried["body"].replace(" -->", f" reaffirmed={OLD_HEAD} -->", 1)
    rows = [{**parse_verdict_marker(r["body"]), **r} for r in (review_row(OLD_HEAD, "PASS"), carried)]
    verdicts = panel_rounds(rows)
    assert [v["head"] for v in verdicts] == [OLD_HEAD, HEAD]  # the gate can find the new head's
    assert [r["head"] for r in spent_rounds(verdicts)] == [OLD_HEAD]  # but one panel was spent
    assert round_cap_reached(verdicts, 2) and not round_cap_reached(spent_rounds(verdicts), 2)


async def test_the_gate_promotes_a_carried_pass_at_the_new_head(tmp_path):
    # End to end for #135: the head that used to sit `hold:stale-head` now promotes.
    green = [{"status": "completed", "conclusion": "success"}]
    carried = review_row(HEAD, "PASS")
    carried["body"] = carried["body"].replace(" -->", f" reaffirmed={OLD_HEAD} -->", 1)
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(OLD_HEAD, "PASS"), carried], checks=green)
    assert (await formal(tmp_path, gh).evaluate_promotion("o/r", 1)) == "promote"
    # Without the carried verdict the same PR holds — the old behaviour.
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(OLD_HEAD, "PASS")], checks=green)
    assert (await formal(tmp_path / "old", gh).evaluate_promotion("o/r", 1)) == "hold:stale-head"


async def test_an_unverified_round_holds_the_gate_end_to_end(tmp_path):
    # `verified=false` was parsed from the marker and then DROPPED by `panel_rounds`, so the
    # gate's `hold:unverified` could never fire. A carried verdict needs the flag to ride along.
    green = [{"status": "completed", "conclusion": "success"}]
    unverified = review_row(HEAD, "PASS")
    unverified["body"] = unverified["body"].replace(" -->", " verified=false -->", 1)
    gh = RoutedGH(pr_facts=facts(), reviews=[unverified], checks=green)
    assert (await formal(tmp_path, gh).evaluate_promotion("o/r", 1)) == "hold:unverified"


async def test_a_material_diff_change_runs_a_new_panel(tmp_path):
    """The head tree differs — e.g. a rebase pulled a changed dependency in through the
    base, the exact case the changed-file-only identity missed. The identity differs, so
    the panel runs rather than reaffirming a stale verdict (acceptance r2)."""
    from pr_reviewer.rounds import diff_identity

    did_old = diff_identity("mbA", "treeA")
    gh = DiffIdGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "PASS", diff_id=did_old)],
        head_trees={OLD_HEAD: "treeA", HEAD: "treeB"},  # reviewed content changed
        merge_base_tree="mbA",
    )
    ran: list[str] = []
    d = make(tmp_path, gh=gh, runner=_no_panel_runner(ran))
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "reviewed:FAIL"  # the panel ran and posted its own verdict
    assert len(ran) == 1


async def test_an_unreadable_diff_identity_does_not_reaffirm(tmp_path):
    """The current head's tree cannot be read, so the identity is unknown. Fail closed —
    run the panel rather than reuse a prior verdict (acceptance r3)."""
    from pr_reviewer.rounds import diff_identity

    did_old = diff_identity("mbA", "treeA")
    gh = DiffIdGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "PASS", diff_id=did_old)],
        head_trees={},  # HEAD's tree read returns "" → identity unreadable
        merge_base_tree="mbA",
    )
    ran: list[str] = []
    d = make(tmp_path, gh=gh, runner=_no_panel_runner(ran))
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "reviewed:FAIL"
    assert len(ran) == 1


async def test_a_prior_round_without_a_stored_identity_does_not_reaffirm(tmp_path):
    """An older marker carries no `diff=` id, so sameness cannot be proven even when the
    current diff reads fine. Fail closed to a fresh panel (acceptance r3)."""
    gh = DiffIdGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "PASS")],  # no diff_id on the prior round
        head_trees={OLD_HEAD: "treeA", HEAD: "treeA"},
        merge_base_tree="mbA",
    )
    ran: list[str] = []
    d = make(tmp_path, gh=gh, runner=_no_panel_runner(ran))
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "reviewed:FAIL"
    assert len(ran) == 1


async def test_a_summon_bypasses_the_diff_reaffirm_and_runs_a_fresh_panel(tmp_path):
    """`@vera review` is 'I think you got this wrong' — it forces a fresh panel even when
    the diff is byte-identical to a reaffirmable round (acceptance r4)."""
    from pr_reviewer.rounds import diff_identity

    did = diff_identity("mbA", "treeA")
    gh = DiffIdGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "PASS", diff_id=did)],
        head_trees={OLD_HEAD: "treeA", HEAD: "treeA"},  # byte-identical diff
        merge_base_tree="mbA",
    )
    ran: list[str] = []
    d = make(tmp_path, gh=gh, runner=_no_panel_runner(ran))
    out = await d.handle_summon("o/r", 1, "operator")
    assert out == "reviewed:FAIL"  # a fresh panel, despite the identical diff
    assert len(ran) == 1


async def test_a_posted_verdict_stamps_its_diff_identity_for_a_later_rebase(tmp_path):
    """The posted verdict's marker carries the reviewed diff id, so a LATER rebased head can
    reaffirm against this round. Closes the loop the reaffirm read depends on."""
    from pr_reviewer.rounds import diff_identity

    gh = DiffIdGH(
        pr_facts=facts(),
        reviews=[],  # first review of this PR — the panel runs and posts
        head_trees={HEAD: "treeA"},
        merge_base_tree="mbA",
    )
    ran: list[str] = []
    d = make(tmp_path, gh=gh, runner=_no_panel_runner(ran))
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "reviewed:FAIL" and len(ran) == 1
    review_post = gh.reviews_posted[0]
    assert f"diff={diff_identity('mbA', 'treeA')}" in review_post["body"]


# ── existing-threads context ──────────────────────────────────────────────────


async def test_existing_threads_block_reaches_the_panel_as_wrapped_data(tmp_path):
    nodes = [
        {
            "isResolved": False,
            "isOutdated": False,
            "path": "x.py",
            "line": 3,
            "originalLine": 3,
            "comments": {"nodes": [{"author": {"login": "coderabbitai[bot]"}, "body": "possible dup"}]},
        }
    ]
    gh = RoutedGH(pr_facts=facts(), threads=nodes)
    seen = {}

    async def runner(name, inputs):
        seen.update(inputs=inputs)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    block = seen["inputs"]["existing_threads"]
    assert block.startswith("<pr_review_threads>") and "coderabbitai[bot]" in block


async def test_unreadable_threads_never_block_the_review(tmp_path):
    gh = RoutedGH(pr_facts=facts())  # threads fetch degrades (null nodes)
    seen = {}

    async def runner(name, inputs):
        seen.update(inputs=inputs)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert "existing_threads" not in seen["inputs"]  # recipe default "(none)" applies


# ── thread ownership for the QA-panel check (issue #105) ──────────────────────


async def test_panel_owned_unresolved_counts_only_our_own_open_threads(tmp_path):
    """Server-authoritative and author-based: a thread is the panel's iff its ROOT comment
    is our own bot login. Resolved threads and other reviewers' threads don't count."""
    gh = RoutedGH(
        pr_facts=facts(),
        threads=[
            thread_node("qa-bot[bot]"),  # ours, open → counts
            thread_node("coderabbitai[bot]"),  # external → does not
            thread_node("qa-bot[bot]", resolved=True),  # ours but resolved → does not
            thread_node("some-human"),  # external → does not
        ],
    )
    d = make(tmp_path, gh=gh)  # RoutedGH's `user` probe resolves our login to qa-bot
    assert (await d._panel_owned_unresolved("o/r", 1)) == 1


async def test_panel_ownership_is_unknown_when_identity_is_unknown(tmp_path):
    """Without a resolvable identity we cannot claim any thread as ours — None (a HOLD),
    never a count, so an unread identity can't fail the check on someone else's thread."""

    class NoViewerGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            if len(args) > 1 and args[1] == "user":
                return 1, "", "HTTP 403"
            return await super().__call__(args, timeout)

    gh = NoViewerGH(pr_facts=facts(), threads=[thread_node("coderabbitai[bot]")])
    d = make(tmp_path, gh=gh)  # no viewer_login configured, and the probe fails
    assert (await d._panel_owned_unresolved("o/r", 1)) is None


async def test_panel_ownership_is_unknown_when_threads_are_unreadable(tmp_path):
    """An unreadable thread list is 'unknown', not 'zero of ours' — None, so the check
    HOLDS rather than either failing or falsely clearing."""
    gh = RoutedGH(pr_facts=facts(), threads=None)  # the fetch degrades to null nodes
    d = make(tmp_path, cfg={"viewer_login": "qa-bot[bot]"}, gh=gh)
    assert (await d._panel_owned_unresolved("o/r", 1)) is None


async def test_external_thread_holds_promotion_without_a_panel_failure(tmp_path):
    """Issue #105 end-to-end: a clean PASS with no panel findings and only ANOTHER
    reviewer's open thread. Promotion stays fail-closed (no APPROVE posted), but the
    QA-panel check we publish is NOT a failure and never calls the external thread a panel
    finding — the verdict and promotion eligibility are kept apart."""
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(HEAD, "PASS")],
        checks=[{"status": "completed", "conclusion": "success", "name": "CI"}],
        threads=[thread_node("coderabbitai[bot]")],
    )
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:threads-unresolved"
    assert gh.reviews_posted == []  # fail-closed: the external thread holds promotion
    qa_writes = [p for p in gh.posted if "check-runs" in p.get("url", "")]
    assert qa_writes, "the QA-panel check should still be published"
    qa = qa_writes[-1]
    assert qa.get("conclusion") == "success"  # the panel is clear — not a red X
    assert "finding" not in qa.get("output[title]", "").lower()  # not called a panel finding
    assert "held" in qa.get("output[summary]", "").lower()  # attributed as a promotion hold


# ── in-diff confinement ───────────────────────────────────────────────────────


async def test_out_of_diff_finding_is_confined_and_cannot_gate(tmp_path):
    # REPORT's confirmed major sits on x.py; the PR only touched y.py — the finding
    # is dropped before the verdict, footnoted in the body, and telemetered.
    gh = RoutedGH(pr_facts=facts(), files="y.py\n")
    d = make(tmp_path, gh=gh)
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "reviewed:PASS"
    assert "in-diff confinement" in gh.posted[0]["body"] and "x.py" in gh.posted[0]["body"]
    events = {e["event"]: e for e in d.telemetry.read_all()}
    assert events["confined"]["dropped"] == [{"file": "x.py", "severity": "major"}]
    assert events["reviewed"]["confined"] == 1 and events["reviewed"]["findings"] == 0


async def test_confinement_stands_down_when_the_file_list_is_unreadable(tmp_path):
    # No changed-path list (the /files read returned nothing) — the FAIL must survive.
    gh = RoutedGH(pr_facts=facts(), files="")
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert "in-diff confinement" not in gh.posted[0]["body"]


# ── exhaustion (D3) ───────────────────────────────────────────────────────────


async def test_failed_panel_step_escalates_and_posts_nothing(tmp_path):
    gh = RoutedGH(pr_facts=facts())
    escalations = []

    async def runner(name, inputs):
        return {"output": "partial", "failed": ["find_crossfile"]}

    d = make(tmp_path, gh=gh, runner=runner, inbox=lambda text, **kw: escalations.append((text, kw)))
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "error:panel-exhausted"
    # D3 holds: no verdict review posted (the exhaustion comment is not a verdict)
    assert all("event" not in p for p in gh.posted)
    assert escalations and "UNREVIEWED" in escalations[0][0]


# ── the protoReview check run (#95) ───────────────────────────────────────────


def _clean_report() -> str:
    """A brief + empty findings array — the panel's PASS shape."""
    return "<!-- brief -->\nAll good.\n<!-- /brief -->\n\n```json\n[]\n```"


async def _clean_runner(name, inputs):
    return {"output": _clean_report(), "failed": []}


async def test_dispatch_opens_a_protoreview_check_in_progress(tmp_path):
    """r1/r9: a panel that proceeds past the gates opens an `in_progress` check keyed to
    the server-resolved head — the handle branch protection can require."""
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")
    creates = [w for w in gh.check_writes if w["method"] == "POST"]
    assert len(creates) == 1
    c = creates[0]
    assert c["url"] == "repos/o/r/check-runs"
    assert c["name"] == "protoReview" and c["head_sha"] == HEAD and c["status"] == "in_progress"


async def test_pass_verdict_concludes_the_check_success(tmp_path):
    """r2/r6: PASS clears the gate; the summary carries the verdict text, and the PATCH
    targets the id the create handed back."""
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, gh=gh, runner=_clean_runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    create = next(w for w in gh.check_writes if w["method"] == "POST")
    patches = [w for w in gh.check_writes if w["method"] == "PATCH"]
    assert len(patches) == 1
    p = patches[0]
    assert p["url"] == f"repos/o/r/check-runs/{create['id']}"
    assert p["status"] == "completed" and p["conclusion"] == "success"
    assert "PASS" in p["output[summary]"]


async def test_fail_verdict_concludes_the_check_failure(tmp_path):
    """r3/r6: a FAIL holds the gate red, and the verdict text rides in the summary."""
    gh = RoutedGH(pr_facts=facts(), reviews=[])  # default REPORT is a confirmed major on x.py
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    patches = [w for w in gh.check_writes if w["method"] == "PATCH"]
    assert patches and patches[0]["conclusion"] == "failure"
    assert patches[0]["status"] == "completed" and "FAIL" in patches[0]["output[summary]"]


async def test_exhaustion_concludes_the_check_failure(tmp_path):
    """r4: THE key closure — an exhausted panel used to leave no signal at all. Now the
    check goes red and its summary names the exhaustion, while D3 still posts no verdict."""
    gh = RoutedGH(pr_facts=facts(), reviews=[])

    async def runner(name, inputs):
        return {"output": "partial", "failed": ["find_crossfile"]}

    d = make(tmp_path, gh=gh, runner=runner, inbox=lambda text, **kw: None)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-exhausted"
    assert len([w for w in gh.check_writes if w["method"] == "POST"]) == 1  # opened
    patches = [w for w in gh.check_writes if w["method"] == "PATCH"]
    assert patches and patches[0]["conclusion"] == "failure"
    assert "exhaust" in (patches[0]["output[title]"] + patches[0]["output[summary]"]).lower()
    assert all("event" not in p for p in gh.posted)  # D3: still no verdict review


async def test_a_dropped_event_opens_no_check(tmp_path):
    """r5: draft (dropped inside `_review`) and allowlist miss (dropped before any GitHub
    call) both open no check — a check exists only for panels that actually run."""
    draft = RoutedGH(pr_facts=facts(draft=True), reviews=[])
    d = make(tmp_path, gh=draft)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:pr-not-eligible"
    assert draft.check_writes == []

    unlisted = RoutedGH(pr_facts=facts(), reviews=[])
    d2 = make(tmp_path, gh=unlisted)
    assert (await d2.handle_pr_event("evil/repo", 1, HEAD, "opened")) == "drop:unlisted-repo"
    assert unlisted.check_writes == [] and unlisted.calls == []


async def test_a_reaffirm_opens_no_new_check(tmp_path):
    """An unchanged head with a posted verdict reaffirms without re-spending the panel —
    and without opening a second check; the head already carries one from its real review."""
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")])
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:PASS"
    assert gh.check_writes == []


async def test_check_create_failure_never_blocks_the_verdict(tmp_path):
    """r7: a missing `checks: write` (the create 403s) must not cost the review — the
    verdict posts, and no conclude is attempted against an id we never got."""

    class NoCheckWriteGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            joined = " ".join(args)
            if "/check-runs" in joined and "-X" in args and "POST" in args and "protoReview" in joined:
                self.calls.append(args)
                return 1, "", "HTTP 403: Resource not accessible by integration"
            return await super().__call__(args, timeout)

    gh = NoCheckWriteGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert gh.reviews_posted and gh.reviews_posted[0]["event"] == "COMMENT"  # shadow FAIL
    assert [w for w in gh.check_writes if w["method"] == "PATCH"] == []  # nothing to conclude


async def test_check_conclude_failure_never_blocks_the_verdict(tmp_path):
    """r7, the other half: the create succeeds but the conclude PATCH 500s — the verdict
    already landed (it posts BEFORE the check is concluded), so it is never lost."""

    class PatchFailsGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            joined = " ".join(args)
            if "/check-runs/" in joined and "-X" in args and "PATCH" in args:
                self.calls.append(args)
                return 1, "", "HTTP 500"
            return await super().__call__(args, timeout)

    gh = PatchFailsGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, gh=gh, runner=_clean_runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    assert gh.reviews_posted and "event" in gh.reviews_posted[0]


async def test_our_review_check_is_not_a_check_we_wait_on(tmp_path):
    """The deadlock this would otherwise ship with: `protoReview` sits `in_progress`
    while the panel runs, so counting it among the checks a FAIL gate reads would force
    every FAIL to post as a comment instead of a block. It must be filtered like `QA
    panel` is."""
    from pr_reviewer.dispatch import REVIEW_CHECK_NAME

    gh = RoutedGH(
        pr_facts=facts(),
        checks=[
            {"status": "in_progress", "conclusion": None, "name": REVIEW_CHECK_NAME},
            {"status": "completed", "conclusion": "success", "name": "CI"},
        ],
    )
    d = make(tmp_path, gh=gh)
    assert (await d._checks_state("o/r", HEAD)) == "green"


# ── a PR GitHub will not accept a review on (issue #78) ──────────────────────


async def test_a_locked_pr_is_never_reviewed(tmp_path):
    """GitHub answers a review on a locked conversation with `422 lock prevents
    review`, so the panel would run in full and the verdict die at the post. Two
    auto-locked dependabot PRs burned a panel every ~7 minutes this way."""
    gh = RoutedGH(pr_facts=facts(locked=True), reviews=[])
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:pr-not-eligible"
    assert ran == []  # the panel was never spent
    assert (await d.needs_backfill("o/r", 1)) is None  # and the sweep won't pick it up


def test_ineligible_reason_names_the_condition():
    from pr_reviewer.dispatch import ineligible_reason

    assert ineligible_reason(facts()) is None
    assert ineligible_reason(None) == "facts-unreadable"
    assert ineligible_reason(facts(state="closed")) == "not-open"
    assert ineligible_reason(facts(draft=True)) == "draft"
    assert ineligible_reason(facts(locked=True)) == "locked"


async def test_a_skipped_pr_says_why_including_the_silent_backfill_path(tmp_path):
    """Four conditions used to collapse into one opaque `pr-not-eligible`, and the
    backfill path emitted NOTHING — so a permanently-skipped PR looked identical to one
    the sweep had not reached yet. That is the invisibility that made a 2.5h duplicate
    loop and an all-evening 422 loop expensive to find."""
    gh = RoutedGH(pr_facts=facts(locked=True), reviews=[])
    d = make(tmp_path, gh=gh)

    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:pr-not-eligible"
    assert (await d.needs_backfill("o/r", 1)) is None

    # glob rather than recompute the UTC day — the file rolls at midnight and a test
    # that computes the date races it.
    rows = [
        json.loads(line)
        for f in (tmp_path / "telemetry").glob("*.jsonl")
        for line in f.read_text().splitlines()
        if line.strip()
    ]
    whys = [r.get("why") for r in rows if r.get("event") == "drop"]
    assert whys.count("locked") == 2  # BOTH paths now say it, backfill included


async def test_a_locked_pr_holds_promotion_and_regate_too(tmp_path):
    """`locked` was added to all four eligibility checks, but only the review and
    backfill paths were covered. A locked conversation refuses an APPROVE and a
    REQUEST_CHANGES exactly as it refuses a review, so both sweep legs must hold."""
    gh = RoutedGH(pr_facts=facts(locked=True), reviews=[review_row(HEAD, "PASS")], checks=[])
    d = make(tmp_path, cfg={"promotion_owner": True, "shadow_mode": False}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:pr-not-eligible"
    assert gh.posted == []  # no APPROVE attempted into the lock

    gh2 = RoutedGH(pr_facts=facts(locked=True), reviews=[review_row(HEAD, "FAIL")], checks=[])
    d2 = make(tmp_path / "regate", cfg={"shadow_mode": False, "regate": True}, gh=gh2)
    assert (await d2.evaluate_regate("o/r", 1)) == "hold:pr-not-eligible"
    assert gh2.reviews_posted == []

    # …and BOTH sweep legs say why. Asserting the return value alone would let a
    # regression drop `why=` silently, which is the exact class of gap this PR closes.
    def _whys(root, event):
        return [
            json.loads(line).get("why")
            for f in (root / "telemetry").glob("*.jsonl")
            for line in f.read_text().splitlines()
            if line.strip() and json.loads(line).get("event") == event
        ]

    assert "locked" in _whys(tmp_path, "promotion")
    assert "locked" in _whys(tmp_path / "regate", "regate")


async def test_a_wrong_viewer_login_is_caught_by_our_own_posted_reviews(tmp_path):
    """`viewer_login` is a string an operator typed — a typo silently disarms the
    self-authored rail instead of erroring. Our own reviews then arrive under an App
    login that is not `viewer_login`: that is warned about once, and the review history
    reads as unreadable (every caller holds) rather than as "no reviews". The rows are
    already fetched, so checking costs nothing."""
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")])
    gh.review_author = "protoreview[bot]"
    d = make(tmp_path, cfg={"viewer_login": "protoreviw[bot]"}, gh=gh)  # typo
    await d._viewer_login()
    assert (await d._our_reviews("o/r", 1)) is None  # a mistyped login holds: unreadable, not "none"

    rows = [
        json.loads(line)
        for f in (tmp_path / "telemetry").glob("*.jsonl")
        for line in f.read_text().splitlines()
        if line.strip()
    ]
    mism = [r for r in rows if r.get("event") == "viewer-mismatch"]
    assert mism and mism[0]["actual"] == "protoreview[bot]"
    assert len(mism) == 1  # warned once, not once per review

    # the correct login is silent
    gh2 = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")])
    gh2.review_author = "protoreview[bot]"
    d2 = make(tmp_path / "ok", cfg={"viewer_login": "protoreview[bot]"}, gh=gh2)
    await d2._viewer_login()
    assert len(await d2._our_reviews("o/r", 1)) == 1
    rows2 = [
        json.loads(line)
        for f in (tmp_path / "ok" / "telemetry").glob("*.jsonl")
        for line in f.read_text().splitlines()
        if line.strip()
    ]
    assert not [r for r in rows2 if r.get("event") == "viewer-mismatch"]


async def test_api_error_detail_is_folded_into_the_log(tmp_path):
    """`gh` puts its status on stderr and GitHub's REASON in the JSON on stdout.
    Logging stderr alone gives 'Unprocessable Entity (HTTP 422)' and nothing to act on —
    `lock prevents review` sat unread in that body while verdicts were lost."""
    from pr_reviewer.dispatch import _with_api_detail

    body = '{"message": "Unprocessable Entity", "errors": ["lock prevents review"]}'
    out = _with_api_detail("gh: Unprocessable Entity (HTTP 422)", body)
    assert "lock prevents review" in out
    # object-shaped errors[] entries survive too
    obj = '{"message": "Validation Failed", "errors": [{"resource": "PullRequestReview", "code": "custom"}]}'
    assert "PullRequestReview" in _with_api_detail("x", obj)
    # non-JSON and empty bodies degrade quietly
    assert _with_api_detail("stderr only", "") == "stderr only"
    assert "some html" in _with_api_detail("x", "some html")


async def test_a_repeatedly_refused_post_stops_costing_a_panel(tmp_path, monkeypatch):
    """Retry handles a GitHub blip; this handles a GitHub DECISION. Without it the
    sweep re-reviews forever: panel → 422 → discarded → backfill → panel."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    runs = []

    async def runner(name, inputs):
        runs.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=RoutedGH(pr_facts=facts(), reviews=[], post_results=[(1, "HTTP 422")] * 9), runner=runner)
    for _ in range(POST_MAX_FAILURES):
        assert (await d._review("o/r", 1)) == "error:post-failed:FAIL"  # verdict computed, post refused
    assert len(runs) == POST_MAX_FAILURES
    # …and now the panel is no longer spent on it
    assert (await d._review("o/r", 1)) == "drop:post-refused"
    assert len(runs) == POST_MAX_FAILURES  # unchanged — no further panel
    # an operator summon still overrides
    assert (await d._review("o/r", 1, force=True)) == "error:post-failed:FAIL"


async def test_a_transient_refusal_never_latches_a_pr_out_of_review(tmp_path, monkeypatch):
    """A degradation must not become a lasting gap — only non-transient refusals count."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    gh = RoutedGH(pr_facts=facts(), reviews=[], post_results=[(1, "HTTP 503")] * 30)
    d = make(tmp_path, gh=gh)
    for _ in range(4):
        assert (await d._review("o/r", 1)) == "error:post-failed:FAIL"
    assert d._post_failures == {}  # 503s left no latch


# ── posting: a verdict must survive a transient refusal (issue #72) ───────────


def test_transient_gh_failure_classifies_by_shape():
    from pr_reviewer.dispatch import transient_gh_failure

    assert transient_gh_failure(1, "gh: No server is currently available ... (HTTP 503)")
    assert transient_gh_failure(1, "HTTP 502 Bad Gateway")
    assert transient_gh_failure(1, "You have exceeded a secondary rate limit")
    assert transient_gh_failure(124, "")  # our own timeout kill
    # A truncated/malformed response body fails gh's own JSON decode before we ever
    # see a status line — a #72 recurrence with a different error shape (2026-09-13:
    # 4 verdict-lost events in one day, all "attempts": 1, because this string wasn't
    # classified as transient and the retry loop never got a second try).
    assert transient_gh_failure(1, "unexpected end of JSON input")
    # A request GitHub means to refuse — sleeping and re-sending changes nothing.
    assert not transient_gh_failure(1, "HTTP 422: Validation Failed")
    assert not transient_gh_failure(1, "HTTP 404: Not Found")
    assert not transient_gh_failure(127, "gh: command not found")


async def test_verdict_post_retries_a_transient_refusal_and_survives(tmp_path, monkeypatch):
    """The panel run is the expensive part; the post is the last step. A 503 on the
    post used to discard the whole verdict with a WARNING and no retry."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    gh = RoutedGH(pr_facts=facts(), post_results=[(1, "HTTP 503"), (0, "")])
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert len(gh.posted) == 2  # first refused, second landed


async def test_verdict_post_does_not_retry_a_422(tmp_path, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    gh = RoutedGH(pr_facts=facts(), post_results=[(1, "HTTP 422: Validation Failed"), (0, "")])
    d = make(tmp_path, gh=gh)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert len(gh.posted) == 1  # gave up immediately — a retry cannot fix a 422


async def test_a_timed_out_post_that_landed_is_not_re_sent(tmp_path, monkeypatch):
    """A timeout is our kill, not GitHub's refusal — the POST may have been accepted.
    Re-sending it duplicates the review, which is the pathology this release fixes.
    The retry re-reads the PR first and stands down when the verdict is already there.
    """
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL")], post_results=[(124, "timed out")])
    d = make(tmp_path, gh=gh)
    # `force` bypasses the reaffirm short-circuit so the panel actually posts.
    assert await d._post_verdict("o/r", 1, HEAD, "FAIL", REPORT, "code-review")
    assert len(gh.posted) == 1  # timed out, confirmed landed, NOT re-sent


async def test_a_timed_out_post_is_re_sent_when_it_demonstrably_did_not_land(tmp_path, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    gh = RoutedGH(pr_facts=facts(), reviews=[], post_results=[(124, "timed out"), (0, "")])
    d = make(tmp_path, gh=gh)
    assert await d._post_verdict("o/r", 1, HEAD, "FAIL", REPORT, "code-review")
    assert len(gh.posted) == 2  # no verdict on the PR ⇒ safe to re-send


async def test_a_timed_out_post_is_not_re_sent_when_the_outcome_cannot_be_confirmed(tmp_path, monkeypatch):
    """Blind after a timeout: a duplicate review is permanent, a lost verdict escalates
    and is recoverable. Prefer the recoverable failure."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    gh = RoutedGH(pr_facts=facts(), reviews_rc=1, reviews_err="HTTP 503", post_results=[(124, "timed out")])
    d = make(tmp_path, gh=gh)
    assert not await d._post_verdict("o/r", 1, HEAD, "FAIL", REPORT, "code-review")
    assert len(gh.posted) == 1


async def test_a_lost_verdict_reports_the_attempts_that_actually_happened(tmp_path, monkeypatch):
    """A 422 gives up after ONE attempt; an alert claiming three describes a retry
    storm that never occurred."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    gh = RoutedGH(pr_facts=facts(), post_results=[(1, "HTTP 422: Validation Failed")])
    escalations = []
    d = make(tmp_path, gh=gh, inbox=lambda text, **kw: escalations.append(text))
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert escalations and "after 1 attempt(s)" in escalations[0]


async def test_a_lost_verdict_escalates_instead_of_vanishing(tmp_path, monkeypatch):
    """Three 503s in 15 minutes discarded three verdicts silently on 2026-08-17.
    Exhausting the retries must leave a trace an operator actually sees."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    gh = RoutedGH(pr_facts=facts(), post_results=[(1, "HTTP 503")] * 3)
    escalations = []
    d = make(tmp_path, gh=gh, inbox=lambda text, **kw: escalations.append(text))
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert len(gh.posted) == 3  # bounded — not forever (issue #6)
    assert escalations and "LOST" in escalations[0]
    assert "o/r#1" in escalations[0]


# ── posting + trigger ─────────────────────────────────────────────────────────


async def test_shadow_mode_posts_comment_and_structural_trigger_picks_recipe(tmp_path):
    gh = RoutedGH(pr_facts=facts(changed_files=6, additions=300, deletions=50), files="x.py\nb\nc\nd\ne\nf\n")
    seen = {}

    async def runner(name, inputs):
        seen["recipe"] = name
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "reviewed:FAIL"
    assert seen["recipe"] == "code-review-structural"
    assert gh.posted[0]["event"] == "COMMENT"  # shadow: FAIL still comments
    assert f"head={HEAD}" in gh.posted[0]["body"]


# protoAgent#2439 end to end: the serving lane put the report step's whole
# chain-of-thought in `content`, and the publisher echoed it onto a public PR. The body
# is now BUILT from the brief/dispositions/findings blocks, so no amount of surrounding
# text has a path into it. This drives the real dispatcher, not the renderer alone.
LEAKED_REPORT = (
    "[review-synthesizer completed: workflow code-review-structural:report]\n\n"
    "Let me process the prior requests. Actually, let me reconsider whether the fix is evident.\n\n"
    '```json\n[{"prior": "x.py:3", "disposition": "fixed", "why": "draft — revised below"}]\n```\n\n'
    "Let me write the final output.\n"
    "<!-- brief -->\nOverall risk is moderate.\n<!-- /brief -->\n\n"
    '```json\n[{"prior": "x.py:3", "disposition": "open", "why": "not addressed this pass"}]\n```\n\n'
    + REPORT.split("\n\n", 1)[1]
)


async def test_a_leaked_chain_of_thought_never_reaches_the_posted_body(tmp_path):
    gh = RoutedGH(pr_facts=facts(), files="x.py\n")

    async def runner(name, inputs):
        return {"output": LEAKED_REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    body = gh.reviews_posted[0]["body"]
    assert "let me reconsider" not in body.lower()
    assert "review-synthesizer completed" not in body
    # Nor the deliberation's DRAFT disposition — the decided one is what the panel is
    # recorded as saying, and it's what the convergence guard reads.
    assert "draft — revised below" not in body
    assert "not addressed this pass" in body
    # …and the review itself is fully intact: brief, findings, machine record.
    assert "Overall risk is moderate." in body
    assert json.loads(extract_findings_json(body))[0]["file"] == "x.py"


async def test_a_report_with_no_delimited_brief_still_posts_and_says_so(tmp_path):
    gh = RoutedGH(pr_facts=facts(), files="x.py\n")

    async def runner(name, inputs):
        return {"output": "undelimited thinking\n\n" + REPORT.split("\n\n", 1)[1], "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    body = gh.reviews_posted[0]["body"]
    assert "undelimited thinking" not in body  # fails CLOSED — the old cut failed open here
    assert "brief could not be read" in body
    assert json.loads(extract_findings_json(body))[0]["file"] == "x.py"  # the review still lands


_SYNTH_CLEAN = "<!-- brief -->\nAll five finders came back clean.\n<!-- /brief -->\n\n```json\n[]\n```"
_SYNTH_FOUND = (
    "<!-- brief -->\nOne defect in x.py.\n<!-- /brief -->\n\n"
    '```json\n[{"file": "x.py", "line": 1, "severity": "minor", "claim": "c"}]\n```'
)


async def test_a_clean_report_that_dropped_its_brief_borrows_the_synthesizers(tmp_path):
    """#168: 5 of 150 posted reviews, every one a clean round — a PASS with no word on
    what was looked at, while the synthesizer's delimited brief sat in the same run."""
    gh = RoutedGH(pr_facts=facts(), files="x.py\n")

    async def runner(name, inputs):
        return {"output": "```json\n[]\n```\n\nNo findings.", "failed": [], "steps": {"synthesize": _SYNTH_CLEAN}}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    body = gh.reviews_posted[0]["body"]
    assert "All five finders came back clean." in body and "brief could not be read" not in body
    row = _telemetry_events(tmp_path, "reviewed")[-1]
    assert row["brief_borrowed"] is True  # the report step is still off its contract: countable


async def test_a_synthesizer_brief_that_could_disagree_with_the_verdict_is_not_borrowed(tmp_path):
    # Written BEFORE verification: it describes a finding the report no longer carries.
    gh = RoutedGH(pr_facts=facts(), files="x.py\n")

    async def runner(name, inputs):
        return {"output": "```json\n[]\n```\n\nNo findings.", "failed": [], "steps": {"synthesize": _SYNTH_FOUND}}

    d = make(tmp_path, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    body = gh.reviews_posted[0]["body"]
    assert "One defect in x.py." not in body and "brief could not be read" in body


async def test_formal_fail_blocks_only_on_terminal_ci(tmp_path):
    pending = [{"status": "in_progress", "conclusion": None}]
    gh = RoutedGH(pr_facts=facts(), checks=pending)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert gh.posted[0]["event"] == "COMMENT"  # pending CI: never a blocking verdict

    done = [{"status": "completed", "conclusion": "success"}]
    gh2 = RoutedGH(pr_facts=facts(), checks=done)
    d2 = make(tmp_path, cfg={"shadow_mode": False}, gh=gh2)
    await d2.handle_pr_event("o/r", 1, HEAD, "opened")
    assert gh2.posted[0]["event"] == "REQUEST_CHANGES"


CLEAN_REPORT = "all good\n```json\n[]\n```"


async def clean_runner(name, inputs):
    return {"output": CLEAN_REPORT, "failed": []}


async def test_formal_clear_dismisses_our_stale_block(tmp_path):
    # A FAILed head left our REQUEST_CHANGES standing; the fixed head clears as a
    # COMMENT — which GitHub does NOT treat as superseding the same reviewer's
    # block — so the dispatcher must dismiss its own stale blocker (the gate
    # lifts itself; APPROVE stays reserved for the promotion owner).
    green = [{"status": "completed", "conclusion": "success"}]
    stale = review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", id=77)
    gh = RoutedGH(pr_facts=facts(), reviews=[stale], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=clean_runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "reviewed:PASS"
    assert gh.posted[0]["event"] == "COMMENT"
    assert gh.dismissed == ["repos/o/r/pulls/1/reviews/77/dismissals"]


async def test_formal_fail_keeps_the_block(tmp_path):
    # Still failing on the new head: the fresh REQUEST_CHANGES supersedes — the
    # old blocker must NOT be dismissed (dismissing would flap the gate open
    # between the dismissal and the new review landing).
    green = [{"status": "completed", "conclusion": "success"}]
    stale = review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", id=77)
    gh = RoutedGH(pr_facts=facts(), reviews=[stale], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh)  # default runner → FAIL report
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "reviewed:FAIL"
    assert gh.dismissed == []


async def test_shadow_mode_never_dismisses(tmp_path):
    # Shadow never posted a blocking review, so it must never dismiss either
    # (a shadow instance touching review state would be a silent write).
    stale = review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", id=77)
    gh = RoutedGH(pr_facts=facts(), reviews=[stale])
    d = make(tmp_path, gh=gh, runner=clean_runner)  # shadow default
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "reviewed:PASS"
    assert gh.dismissed == []


# ── promotion ─────────────────────────────────────────────────────────────────


async def test_promotion_green_path_approves_when_owned(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    assert gh.reviews_posted[0]["event"] == "APPROVE" and "promoted=true" in gh.reviews_posted[0]["body"]


async def test_promotion_holds_in_shadow_or_without_ownership(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, gh=gh)  # shadow default, not owner
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:not-promotion-owner"
    assert gh.posted == []
    # Short-circuit: a structurally-impossible promotion does ZERO GitHub reads — the
    # decision is fixed before any facts/reviews/checks/threads fetch (sweep-cost win).
    assert gh.calls == []


async def test_promotion_holds_on_an_incomplete_clear_verdict(tmp_path):
    # #49: a clean PASS produced while a finder was down must not auto-approve, even on
    # perfectly green CI. The clearing review's marker carries complete=false.
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS", complete=False)], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:incomplete-coverage"
    assert gh.reviews_posted == []  # held, not approved


LLM_LANES = ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions")
CLEAN_LANE = "No issues from this angle.\n\n```json\n[]\n```\nFINDER_STATUS: reviewed n=0"


def _lane(step: str, body: str = CLEAN_LANE, recipe: str = "code-review-structural") -> str:
    """A finder step's output as the host's run_subagent returns it: the delegation
    banner, then the reply — a clean angle ends with an explicit fenced `[]`."""
    return f"[review-finder completed: workflow {recipe}:{step}]\n\n{body}"


def _panel_steps(**over) -> dict:
    """Every step of the structural recipe delivering an explicit `[]`, with `over` swapped in."""
    steps = {s: _lane(s) for s in LLM_LANES}
    steps["find_structural"] = "protoPatch run: head/base resolved, 6 files.\n\n```json\n[]\n```"
    steps["synthesize"] = "<!-- brief -->\nNothing raised.\n<!-- /brief -->\n\n```json\n[]\n```"
    steps["verify"] = "VERIFY_STATUS: nothing-to-verify\n\n```json\n[]\n```"
    steps.update(over)
    return steps


_ONE_FINDING = json.dumps(
    [{"file": "x.py", "line": 3, "severity": "minor", "category": "correctness", "claim": "Bug.", "evidence": "e"}]
)
_SYNTH_ONE = f"<!-- brief -->\nOne.\n<!-- /brief -->\n\n```json\n{_ONE_FINDING}\n```"
_VERIFY_FLAKED = "VERIFY_STATUS: nothing-to-verify\n\nNo findings were provided in the <synthesized> tags."
_VERIFY_ANNOTATED = (
    "VERIFY_STATUS: annotated n=1\n\n```json\n"
    + json.dumps([{**json.loads(_ONE_FINDING)[0], "verdict": "confirmed", "note": "traced"}])
    + "\n```"
)
_REPORT_UNVERIFIED = (
    f"<!-- brief -->\nOne, unverified.\n<!-- /brief -->\n\nVERIFY_GAP: unverified=1\n\n```json\n{_ONE_FINDING}\n```"
)
_REPORT_VERIFIED = (
    "<!-- brief -->\nOne, confirmed.\n<!-- /brief -->\n\n```json\n"
    + json.dumps([{**json.loads(_ONE_FINDING)[0], "verdict": "confirmed", "note": "traced"}])
    + "\n```"
)


async def test_a_verifier_that_contradicts_the_synthesizer_is_re_run_alone(tmp_path):
    """#167: the verifier said nothing-to-verify over a synthesis carrying one finding
    (5 of 29 such rounds). Only verify + report run again, seeded with everything else."""
    calls: list[dict] = []

    async def runner(name, inputs, *, seed_outputs=None):
        calls.append({"seed": seed_outputs})
        if seed_outputs is None:
            steps = _panel_steps(synthesize=_SYNTH_ONE, verify=_VERIFY_FLAKED)
            return {"output": _REPORT_UNVERIFIED, "steps": steps, "failed": [], "timings": {"verify": 8.0}}
        return {
            "output": _REPORT_VERIFIED,
            "steps": {"verify": _VERIFY_ANNOTATED, "report": _REPORT_VERIFIED},
            "failed": [],
            "timings": {"verify": 9.0},
        }

    d = make(tmp_path, gh=_structural_gh(), runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "reviewed:WARN"
    # The re-run was seeded with every step BUT verify and report — the finders did not run twice.
    assert len(calls) == 2 and set(calls[1]["seed"]) == set(_panel_steps()) - {"verify", "report"}
    row = _events(tmp_path, "reviewed")[-1]
    assert row["verdict"] == "WARN" and row["findings"] == 1
    events = _events(tmp_path, "verify-contradicted")
    assert [(e["attempt"], e["rerun"], e["cleared"]) for e in events] == [(1, True, True)]


async def test_a_verifier_that_stays_contradicted_posts_unverified_as_before(tmp_path):
    async def runner(name, inputs, *, seed_outputs=None):
        if seed_outputs is None:
            return {
                "output": _REPORT_UNVERIFIED,
                "steps": _panel_steps(synthesize=_SYNTH_ONE, verify=_VERIFY_FLAKED),
                "failed": [],
            }
        return {
            "output": _REPORT_UNVERIFIED,
            "steps": {"verify": _VERIFY_FLAKED, "report": _REPORT_UNVERIFIED},
            "failed": [],
        }

    gh = _structural_gh()
    d = make(tmp_path, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert "verified=false" in gh.reviews_posted[0]["body"]  # fail-closed, exactly as today
    events = _events(tmp_path, "verify-contradicted")
    assert [(e["attempt"], e["rerun"], e["cleared"]) for e in events] == [(1, True, False)]


async def test_a_host_whose_runner_cannot_seed_only_counts_the_contradiction(tmp_path):
    calls = 0

    async def runner(name, inputs):  # protoAgent < #3571: no seed_outputs
        nonlocal calls
        calls += 1
        return {
            "output": _REPORT_UNVERIFIED,
            "steps": _panel_steps(synthesize=_SYNTH_ONE, verify=_VERIFY_FLAKED),
            "failed": [],
        }

    gh = _structural_gh()
    d = make(tmp_path, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert calls == 1 and "verified=false" in gh.reviews_posted[0]["body"]
    assert [(e["rerun"], e["cleared"]) for e in _events(tmp_path, "verify-contradicted")] == [(False, False)]


async def test_verify_reruns_zero_disables_the_re_run_on_a_host_that_could_seed(tmp_path):
    calls = 0

    async def runner(name, inputs, *, seed_outputs=None):  # a seed-capable host
        nonlocal calls
        calls += 1
        return {
            "output": _REPORT_UNVERIFIED,
            "steps": _panel_steps(synthesize=_SYNTH_ONE, verify=_VERIFY_FLAKED),
            "failed": [],
        }

    gh = _structural_gh()
    d = make(tmp_path, cfg={"verify_reruns": 0}, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert calls == 1 and "verified=false" in gh.reviews_posted[0]["body"]
    assert [(e["rerun"], e["cleared"]) for e in _events(tmp_path, "verify-contradicted")] == [(False, False)]


async def test_a_verifier_that_read_its_input_is_not_re_run(tmp_path):
    calls = 0

    async def runner(name, inputs, *, seed_outputs=None):
        nonlocal calls
        calls += 1
        return {
            "output": _REPORT_VERIFIED,
            "steps": _panel_steps(synthesize=_SYNTH_ONE, verify=_VERIFY_ANNOTATED),
            "failed": [],
        }

    d = make(tmp_path, gh=_structural_gh(), runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert calls == 1 and _events(tmp_path, "verify-contradicted") == []


_OVERRUN = (
    "Error: step 'find_crossfile' raised SubagentError: Subagent 'review-finder' failed: Error code: 400 - "
    "{'error': {'message': \"litellm.ContextWindowExceededError: litellm.BadRequestError: "
    "ContextWindowExceededError: OpenAIException - This model's maximum context length is 262144 tokens. "
    'However, you requested 32768 output tokens and your prompt contains at least 229377 input tokens"}}'
)
_CLEAN_REPORT = "<!-- brief -->\nFour lanes clean.\n<!-- /brief -->\n\n```json\n[]\n```"


async def test_a_lane_that_overran_its_context_is_a_gap_not_a_failed_round(tmp_path):
    """#176: mythxengine#858 — 8 panels, 7 killed by ContextWindowExceeded in three different
    lanes, the other four delivered every time. Retrying the panel re-rolls the same dice."""
    calls = 0

    async def runner(name, inputs):
        nonlocal calls
        calls += 1
        steps = _panel_steps(find_crossfile=_OVERRUN)
        return {"output": _CLEAN_REPORT, "failed": ["find_crossfile"], "steps": steps}

    gh = _structural_gh()
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    assert calls == 1  # no retry: the round is carried, not discarded
    body = gh.reviews_posted[0]["body"]
    assert "verdict=WARN" in body and "complete=false" in body
    assert "`find_crossfile` (overran the model's context window" in body
    assert _events(tmp_path, "exhaustion") == [] and _events(tmp_path, "panel_retry") == []
    assert [e["lanes"] for e in _events(tmp_path, "finder_overran")] == [["find_crossfile"]]
    row = _events(tmp_path, "reviewed")[-1]
    assert row["overran"] == ["find_crossfile"] and row["complete"] is False


async def test_a_lane_that_crashed_for_another_reason_still_retries_and_exhausts(tmp_path):
    calls = 0

    async def runner(name, inputs):
        nonlocal calls
        calls += 1
        steps = _panel_steps(find_crossfile="Error: step 'find_crossfile' raised SubagentError: boom")
        return {"output": _CLEAN_REPORT, "failed": ["find_crossfile"], "steps": steps}

    gh = _structural_gh()
    d = make(tmp_path, cfg={"shadow_mode": False, "panel_retries": 1}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-exhausted"
    assert calls == 2 and gh.reviews_posted == []
    assert _events(tmp_path, "finder_overran") == []


def _structural_gh() -> RoutedGH:
    return RoutedGH(pr_facts=facts(changed_files=6, additions=300, deletions=50), files="x.py\nb\nc\nd\ne\nf\n")


def _events(tmp_path, name: str) -> list[dict]:
    return [e for e in Telemetry(tmp_path).read_all() if e.get("event") == name]


STRUCTURAL_HARD_STOP = (
    "[structural-finder hard-stopped at max_turns: workflow code-review-structural:find_structural] "
    "-- no salvageable output; treat this lane as a Gap, not a verdict."
)


async def test_a_structural_gateway_failure_stamps_complete_false_and_caps_the_pass(tmp_path):
    # End to end: protoPatch returns its PROTOPATCH UNAVAILABLE Gap (gateway auth/config)
    # and the four LLM finders find nothing. The marker records complete=false so the
    # promotion gate above refuses to auto-approve it (#49) — and the verdict is capped at
    # WARN rather than posted as a clean PASS, because the structural angle never ran (#117).
    gh = _structural_gh()
    unavailable = (
        "PROTOPATCH UNAVAILABLE — gateway auth/config failure\n\nGap: structural pass unavailable\n\n```json\n[]\n```"
    )

    async def runner(name, inputs):
        return {"output": CLEAN_REPORT, "failed": [], "steps": _panel_steps(find_structural=unavailable)}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "complete=false" in body
    assert "`find_structural` (structural pass unavailable or cut short" in body  # …then its reason (#140)
    assert "came back clean" not in body


async def test_a_relay_that_obeys_the_tool_still_reads_as_a_structural_outage(tmp_path):
    # The tool tells the relay to write the Gap line and an empty array INSTEAD of echoing
    # its own text, so a faithful relay's reply has no "PROTOPATCH UNAVAILABLE" in it. Knowing
    # only that prefix, the gate read the empty array as a clean structural pass: live, 33
    # rounds with protoPatch down were recorded complete and 22 of them auto-approved. This
    # is the reply verbatim (mythxengine#807, 2026-09-19).
    gh = _structural_gh()
    obedient = _lane(
        "find_structural",
        "Gap: structural pass unavailable — clawpatch exit 4 (gateway provider failure: auth, HTTP "
        "error, or an unusable model reply): ors=2\n\n```json\n[]\n```",
    )
    assert "PROTOPATCH UNAVAILABLE" not in obedient

    async def runner(name, inputs):
        return {"output": CLEAN_REPORT, "failed": [], "steps": _panel_steps(find_structural=obedient)}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"  # not a clean PASS
    body = gh.reviews_posted[0]["body"]
    assert "complete=false" in body  # so the promotion gate will not auto-approve it
    assert "`find_structural` (structural pass unavailable or cut short" in body  # …then its reason (#140)
    (row,) = _telemetry_events(tmp_path, "reviewed")
    assert row["structural_unavailable"] is True and row["complete"] is False


async def test_the_outage_reason_reaches_the_coverage_banner(tmp_path):
    # #140: the detail was captured, handed to the relay, and paraphrased away — one fault
    # posted as "gateway auth error" one round and "provider error" the next.
    gh = _structural_gh()
    outage = _lane(
        "find_structural",
        "Gap: structural pass unavailable — clawpatch exit 4 (gateway provider failure: auth, HTTP error, or an "
        "unusable model reply): gateway review: full response saved to "
        "/sandbox/pr-reviewer/clawpatch/o-r/provider-failures/20260919T184825346Z-gateway-review-5cc64695.json — "
        "response was not parseable JSON (finish_reason=stop)\n\n```json\n[]\n```",
    )

    async def runner(name, inputs):
        return {"output": CLEAN_REPORT, "failed": [], "steps": _panel_steps(find_structural=outage)}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "structural pass unavailable or cut short: clawpatch exit 4" in body
    assert "/sandbox/" not in body and "(path)" in body  # never the reviewer's filesystem layout


def test_outage_reason_is_display_safe():
    from pr_reviewer.protopatch import outage_reason

    assert outage_reason("") == "" and outage_reason("```json\n[]\n```") == ""
    assert outage_reason("PROTOPATCH UNAVAILABLE — checkout failed: clone error") == "checkout failed: clone error"
    hostile = "Gap: structural pass unavailable — 401 from http://gateway:4000/v1/chat `rm -rf` [x](y) **bold** | <b>"
    got = outage_reason(hostile)
    assert "gateway:4000" not in got and "(url)" in got
    assert not set(got) & set("`*[]|<>")
    assert len(outage_reason("Gap: structural pass unavailable — " + "x" * 500)) == 180


def test_the_gap_line_the_tool_prescribes_is_the_one_the_gate_recognises():
    # One constant on both sides, so the instruction and the detector cannot drift apart.
    from pr_reviewer.protopatch import GAP_LINE_PREFIX, STRUCTURAL_GAP_MARKERS, UNAVAILABLE_PREFIX, unavailable

    text = unavailable("clone failed")
    assert f"`{GAP_LINE_PREFIX} — clone failed`" in text and text.startswith(UNAVAILABLE_PREFIX)
    assert set(STRUCTURAL_GAP_MARKERS) == {UNAVAILABLE_PREFIX, GAP_LINE_PREFIX}


# ── a lane that did not run is visible, and is not a clean PASS (#117) ─────────


async def test_blind_lanes_cap_a_clean_pass_at_warn_and_the_body_names_them(tmp_path):
    """protoAgent#3494 @ 73d8ee90: conventions died after its opening line, structural
    gave up at its turn limit — and every completeness signal read clean, the brief
    claimed "no coverage gaps", and the PASS was promoted and merged. Here crossfile is
    also cut off by the engine, whose timeout Gap carries a synthetic `[]`."""
    gh = _structural_gh()
    steps = _panel_steps(
        find_conventions=_lane(
            "find_conventions", "Let me read the relevant source files to understand the context around the changes."
        ),
        find_structural=STRUCTURAL_HARD_STOP,
        find_crossfile=(
            "Gap: step 'find_crossfile' exceeded its 900s time budget and was cut off — no result "
            "from this step this run.\n\n```json\n[]\n```"
        ),
    )
    overclaiming = (
        "<!-- brief -->\nThe structural pass completed; no coverage gaps.\n<!-- /brief -->\n\n```json\n[]\n```"
    )

    async def runner(name, inputs):
        return {"output": overclaiming, "failed": [], "degraded": ["find_crossfile"], "steps": steps}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert "verdict=WARN" in body and "complete=false" in body
    assert "Coverage incomplete — this is not a clean pass." in body
    assert "3 of 5 review lane(s)" in body
    assert "`find_crossfile` (hit its time budget)" in body
    assert "`find_conventions` (did not complete a real pass)" in body
    assert "`find_structural` (structural pass unavailable or cut short)" in body
    assert "`find_correctness`" not in body  # a lane that ran is not swept in
    assert "came back clean" not in body
    # The code-authored record comes first; the model's overclaim is below it.
    assert body.index("Coverage incomplete") < body.index("no coverage gaps")

    reviewed = _events(tmp_path, "reviewed")[-1]
    assert reviewed["verdict"] == "WARN"
    assert reviewed["degraded"] == ["find_crossfile"]
    assert reviewed["incomplete_finders"] == ["find_conventions"]
    assert reviewed["structural_unavailable"] is True
    assert reviewed["complete"] is False
    assert reviewed["coverage_capped"] is True

    # WARN stays non-blocking on the dispatch check, and its summary does not overclaim.
    patch = [w for w in gh.check_writes if w["method"] == "PATCH"][-1]
    assert patch["conclusion"] == "success"
    assert "Coverage was incomplete" in patch["output[summary]"]


async def test_a_coverage_gap_never_softens_a_fail(tmp_path):
    gh = _structural_gh()

    async def runner(name, inputs):
        return {"output": REPORT, "failed": [], "steps": _panel_steps(find_structural=STRUCTURAL_HARD_STOP)}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert "`find_structural` (structural pass unavailable or cut short)" in gh.reviews_posted[0]["body"]
    assert _events(tmp_path, "reviewed")[-1]["coverage_capped"] is None  # FAIL was not capped


# The verify step's real output on protoAgent#3564 (run 2f7796fe): a completion marker and a
# statement of intent, then nothing.
VERIFY_PREAMBLE_ONLY = (
    "[verifier completed: workflow code-review:verify]\n\n"
    "I'll verify the findings by examining the actual PR diff and relevant code context.\n"
)


async def test_a_verifier_that_returns_only_a_preamble_is_not_a_clean_pass(tmp_path):
    """#151: with zero findings `verification_ran` is True without reading the verify output,
    so a verifier that stopped at its preamble posted "came back clean" above a report saying
    its input never arrived. It is a coverage gap — named, capped at WARN, counted — and NOT a
    voided round: nothing went unverified, so re-running the panel would buy nothing."""
    gh = _structural_gh()
    calls = []

    async def runner(name, inputs):
        calls.append(name)
        return {"output": CLEAN_REPORT, "failed": [], "steps": _panel_steps(verify=VERIFY_PREAMBLE_ONLY)}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    assert len(calls) == 1  # a gap, not a retry
    body = gh.reviews_posted[0]["body"]
    assert "came back clean" not in body
    assert "`verify` (returned no findings array and no status line" in body
    reviewed = _events(tmp_path, "reviewed")[-1]
    assert reviewed["verify_undelivered"] is True and reviewed["coverage_capped"] is True
    assert reviewed["complete"] is True  # every finder covered the diff; the hold is not for this


async def test_an_explicit_empty_array_at_every_boundary_is_still_a_clean_pass(tmp_path):
    """The guards must not turn a genuinely clean review into a gap: every lane, the
    synthesizer and the report each delivered an explicit `[]`."""
    gh = _structural_gh()
    calls = []

    async def runner(name, inputs):
        calls.append(name)
        return {"output": CLEAN_REPORT, "failed": [], "steps": _panel_steps()}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    assert len(calls) == 1  # no retry
    body = gh.reviews_posted[0]["body"]
    assert "_No findings — the review came back clean._" in body
    assert "complete=false" not in body and "Coverage incomplete" not in body
    reviewed = _events(tmp_path, "reviewed")[-1]
    assert reviewed["complete"] is True and reviewed["coverage_capped"] is None
    assert reviewed["degraded"] is None and reviewed["structural_unavailable"] is None
    assert reviewed["verify_undelivered"] is None


async def test_a_small_diff_review_is_not_incomplete_for_a_contract_it_never_had(tmp_path):
    """The small-diff `code-review` recipe (protoAgent's) has no structural seat and never
    asks for a FINDER_STATUS line. Judging it by the structural recipe's contract marked
    every healthy small-diff review incomplete — and would now cap every one at WARN."""
    gh = RoutedGH(pr_facts=facts())  # 2 files / 15 lines: the trigger does not fire
    seen = {}
    lane = "No issues from this angle.\n\n```json\n[]\n```"  # no status line: never asked for

    async def runner(name, inputs):
        seen["recipe"] = name
        steps = {s: _lane(s, lane, recipe="code-review") for s in LLM_LANES}
        steps["synthesize"] = "<!-- brief -->\nNothing.\n<!-- /brief -->\n\n```json\n[]\n```"
        return {"output": CLEAN_REPORT, "failed": [], "steps": steps}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    assert seen["recipe"] == "code-review"
    body = gh.reviews_posted[0]["body"]
    assert "complete=false" not in body and "did not complete a real pass" not in body
    reviewed = _events(tmp_path, "reviewed")[-1]
    assert reviewed["complete"] is True
    assert reviewed["incomplete_finders"] is None and reviewed["structural_unavailable"] is None


# ── an absent findings payload is not an empty one (#113) ─────────────────────


async def test_an_undelivered_synthesis_is_retried_then_posts_no_verdict(tmp_path):
    """#113: the synthesizer delivered nothing, so the verifier "received no findings array
    to annotate" — and the report still printed `[]`, which posted PASS under a note saying
    the findings may have been lost. An absent payload is an incomplete round: retry it,
    then say there is no verdict. Never render it as clean."""
    gh = _structural_gh()
    calls = []
    escalations = []
    lost = (
        "<!-- brief -->\nThe verification pass was a no-op: the verifier received no findings array "
        "(or the findings were lost before reaching the verifier).\n<!-- /brief -->\n\n```json\n[]\n```"
    )

    async def runner(name, inputs):
        calls.append(name)
        synth = "[review-synthesizer completed: workflow code-review-structural:synthesize] -- no output produced."
        return {"output": lost, "failed": [], "steps": _panel_steps(synthesize=synth)}

    d = make(tmp_path, gh=gh, runner=runner, inbox=lambda text, **kw: escalations.append(text))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-incomplete"
    assert len(calls) == 2  # retried once before giving up
    assert gh.reviews_posted == []  # no verdict — above all, no PASS
    assert not any("came back clean" in str(p.get("body") or "") for p in gh.posted)
    patch = [w for w in gh.check_writes if w["method"] == "PATCH"][-1]
    assert patch["conclusion"] == "failure" and patch["output[title]"] == "QA panel incomplete — no verdict"
    assert "no findings payload at stage(s) synthesize" in patch["output[summary]"]
    assert escalations and "UNREVIEWED" in escalations[0]
    assert _events(tmp_path, "panel_retry")[0]["undelivered"] == ["synthesize"]
    exhaustion = _events(tmp_path, "exhaustion")[-1]
    assert exhaustion["undelivered"] == ["synthesize"] and exhaustion["attempts"] == 2
    assert _events(tmp_path, "reviewed") == []


async def test_a_report_with_no_findings_array_is_not_a_clean_pass(tmp_path):
    """The final boundary, on a host result with no per-step outputs at all: parsing an
    absent array as `[]` would post a clean PASS."""
    gh = RoutedGH(pr_facts=facts())

    async def runner(name, inputs):
        return {"output": "<!-- brief -->\nLooks fine to me.\n<!-- /brief -->\n", "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-incomplete"
    assert gh.reviews_posted == []
    assert _events(tmp_path, "exhaustion")[-1]["undelivered"] == ["report"]


async def test_no_lane_delivering_is_an_absent_round_not_a_warn(tmp_path):
    """One blind lane among live ones is a coverage gap (WARN). Every lane blind means
    nothing was reviewed — this repo's own #120 at 4dd608e8, where "no review-finder
    produced a findings list" and the panel still posted PASS."""
    gh = _structural_gh()
    dead = {s: _lane(s, "Let me read the relevant source files first.") for s in LLM_LANES}

    async def runner(name, inputs):
        return {
            "output": CLEAN_REPORT,
            "failed": [],
            "steps": _panel_steps(find_structural=STRUCTURAL_HARD_STOP, **dead),
        }

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-incomplete"
    assert gh.reviews_posted == []
    assert _events(tmp_path, "exhaustion")[-1]["undelivered"] == ["finders"]


async def test_an_absent_payload_recovered_on_retry_posts_the_real_verdict(tmp_path):
    gh = _structural_gh()
    calls = []

    async def runner(name, inputs):
        calls.append(name)
        if len(calls) == 1:
            return {"output": "The report was cut off before its findings.", "failed": []}
        return {"output": CLEAN_REPORT, "failed": [], "steps": _panel_steps()}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:PASS"
    assert len(calls) == 2
    assert _events(tmp_path, "panel_retry")[0]["undelivered"] == ["report"]
    assert _events(tmp_path, "exhaustion") == []


async def test_a_finder_missing_its_status_line_stamps_complete_false(tmp_path):
    # protoAgent#3494 (issue #117): removed_behavior and crossfile read nothing but
    # 404s and reported zero findings anyway — a clean PASS to every existing signal
    # (no timeout, no crash, no PROTOPATCH UNAVAILABLE). The required FINDER_STATUS
    # marker is what catches it: two finders here never declare `reviewed`.
    gh = RoutedGH(pr_facts=facts(changed_files=6, additions=300, deletions=50), files="x.py\nb\nc\nd\ne\nf\n")

    async def runner(name, inputs):
        return {
            "output": CLEAN_REPORT,
            "failed": [],
            "steps": {
                "find_correctness": "no issues\n```json\n[]\n```\nFINDER_STATUS: reviewed n=0",
                "find_removed_behavior": "reading files...\n[404]\n[404]\n[404]\n",
                "find_crossfile": "reading files...\n[404]\n[404]\n",
                "find_conventions": "no issues\n```json\n[]\n```\nFINDER_STATUS: reviewed n=0",
                "find_structural": "PROTOPATCH UNAVAILABLE — clone failed\n\nGap: ...",
            },
        }

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    # The coverage cap on top of #120's signals: a blind lane is not a clean PASS.
    assert out == "reviewed:WARN"
    body = gh.posted[0]["body"]
    assert "complete=false" in body
    assert "did not complete a real pass" in body
    assert "Coverage incomplete — this is not a clean pass." in body
    assert "came back clean" not in body
    assert "`find_removed_behavior`" in body and "`find_crossfile`" in body
    # The two that DID declare themselves reviewed must not be swept in with them.
    assert "`find_correctness`" not in body
    assert "`find_conventions`" not in body


async def test_all_finders_declaring_reviewed_stays_complete(tmp_path):
    """The status-line requirement must not turn every normal clean pass into a
    false coverage gap — every finder here plays by the new contract."""
    gh = RoutedGH(pr_facts=facts(changed_files=6, additions=300, deletions=50), files="x.py\nb\nc\nd\ne\nf\n")

    async def runner(name, inputs):
        clean = "no issues\n```json\n[]\n```\nFINDER_STATUS: reviewed n=0"
        return {
            "output": CLEAN_REPORT,
            "failed": [],
            "steps": {
                "find_correctness": clean,
                "find_removed_behavior": clean,
                "find_crossfile": clean,
                "find_conventions": clean,
                "find_structural": "```json\n[]\n```",
            },
        }

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "reviewed:PASS"
    body = gh.posted[0]["body"]
    assert "complete=false" not in body
    assert "did not complete a real pass" not in body


async def test_promotion_dedups_per_head_via_review_state(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    reviews = [review_row(HEAD, "PASS"), review_row(HEAD, "PASS", state="APPROVED")]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:already-promoted"


async def test_promotion_fails_closed_on_unreadable_checks_and_no_checks(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=None)  # 403
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:checks-unknown"

    gh2 = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=[])  # no checks at all
    d2 = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh2)
    assert (await d2.evaluate_promotion("o/r", 1)) == "hold:checks-failed"


async def test_sweep_covers_open_prs(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, gh=gh)
    assert (await d.sweep_once()) == 1  # one repo, one open PR evaluated


async def test_allow_self_review_lifts_the_rail_for_testing_only(tmp_path):
    gh = RoutedGH(pr_facts=facts(author="qa-bot[bot]"))
    d = make(tmp_path, cfg={"allow_self_review": True}, gh=gh)
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out.startswith("reviewed:")  # the rail is config-lifted, default stays closed


async def test_promotion_arms_auto_merge_on_main_only(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    merges = [c for c in gh.calls if c[0] == "pr" and "merge" in c]
    assert merges and "--auto" in merges[0] and "--squash" in merges[0]

    gh2 = RoutedGH(pr_facts=facts(base_ref="develop"), reviews=[review_row(HEAD, "PASS")], checks=green)
    d2 = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh2)
    assert (await d2.evaluate_promotion("o/r", 1)) == "promote"
    assert not [c for c in gh2.calls if c[0] == "pr" and "merge" in c]  # stacked PR: never armed


async def test_warn_verdict_is_non_blocking_and_promotes_on_green(tmp_path):
    # Quinn's semantics: WARN "does NOT block merge" — COMMENTED + green auto-approves;
    # the unresolved-threads gate is what answers "were the concerns seen".
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "WARN")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    assert "WARN verdict" in gh.reviews_posted[0]["body"]


async def test_latest_fail_holds_even_after_an_earlier_pass(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    reviews = [review_row(OLD_HEAD, "PASS"), review_row(HEAD, "FAIL")]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:no-clear-verdict"


async def test_strictest_verdict_wins_for_the_same_head(tmp_path):
    # #89: two concurrent reviews land for the SAME head — one FAIL, one PASS. GitHub
    # returns them in an arbitrary order, and panel_rounds' dedup-by-head keeps whichever
    # it saw LAST. With the PASS returned last (the losing ordering for the old code), a
    # last-writer-wins read would auto-approve straight past the FAIL. The strictest
    # verdict must hold regardless of arrival order — auto-approval does NOT fire.
    green = [{"status": "completed", "conclusion": "success"}]
    pass_last = [review_row(HEAD, "FAIL"), review_row(HEAD, "PASS")]
    gh = RoutedGH(pr_facts=facts(), reviews=pass_last, checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:no-clear-verdict"
    assert gh.reviews_posted == []  # no APPROVE posted — the strictest (FAIL) wins the tie

    # ...and symmetrically with the FAIL returned last, so the guarantee is order-free.
    fail_last = [review_row(HEAD, "PASS"), review_row(HEAD, "FAIL")]
    gh2 = RoutedGH(pr_facts=facts(), reviews=fail_last, checks=green)
    d2 = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh2)
    assert (await d2.evaluate_promotion("o/r", 1)) == "hold:no-clear-verdict"
    assert gh2.reviews_posted == []


class FailingApproveGH(RoutedGH):
    """APPROVE POSTs always fail (GitHub 422-style); everything else routed normally."""

    async def __call__(self, args, timeout=30):
        if "-X" in args and "POST" in args and "event=APPROVE" in " ".join(args):
            self.calls.append(args)
            return 1, "", "gh: Unprocessable Entity (HTTP 422)"
        return await super().__call__(args, timeout)


async def test_promotion_backs_off_after_repeated_approve_failures(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = FailingApproveGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    escalations = []
    d = make(
        tmp_path,
        cfg={"shadow_mode": False, "promotion_owner": True},
        gh=gh,
        inbox=lambda text, **kw: escalations.append(text),
    )
    for _ in range(3):
        assert (await d.evaluate_promotion("o/r", 1)) == "error:approve-failed"
    # Fourth tick: typed backoff hold, no further APPROVE attempts.
    approve_attempts_before = sum(1 for c in gh.calls if "event=APPROVE" in " ".join(c))
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:promote-backoff"
    assert sum(1 for c in gh.calls if "event=APPROVE" in " ".join(c)) == approve_attempts_before == 3
    assert escalations and "backing off" in escalations[0]


async def test_backoff_clears_on_a_new_head(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = FailingApproveGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    for _ in range(3):
        await d.evaluate_promotion("o/r", 1)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:promote-backoff"
    # A new push: different head, different backoff key — promotion re-enters
    # (and holds stale-head here because the verdict names the OLD head).
    gh.pr_facts = facts(head=OLD_HEAD)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:stale-head"


async def test_successful_approve_resets_the_failure_count(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]

    class FlakyGH(RoutedGH):
        fail_next = 2

        async def __call__(self, args, timeout=30):
            if "-X" in args and "POST" in args and "event=APPROVE" in " ".join(args) and self.fail_next > 0:
                self.fail_next -= 1
                self.calls.append(args)
                return 1, "", "HTTP 502"
            return await super().__call__(args, timeout)

    gh = FlakyGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "error:approve-failed"
    assert (await d.evaluate_promotion("o/r", 1)) == "error:approve-failed"
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"  # 3rd attempt succeeds, count resets
    assert d._promote_failures == {}


# ── managed state: config-first with an env fallback (headless config-as-code) ──


def test_repos_fall_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_REVIEWER_REPOS", "o/one, o/two\no/three")
    d = Dispatcher({}, Telemetry(tmp_path))
    assert d.repos == ["o/one", "o/two", "o/three"]


def test_config_repos_win_over_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_REVIEWER_REPOS", "o/env")
    d = Dispatcher({"repos": ["o/cfg"]}, Telemetry(tmp_path))
    assert d.repos == ["o/cfg"]  # a present, non-empty config list wins


def test_empty_config_repos_fall_through_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_REVIEWER_REPOS", "o/env")
    d = Dispatcher({"repos": []}, Telemetry(tmp_path))
    assert d.repos == ["o/env"]  # seed ships repos: [] — the disposable-volume case


def test_no_repos_anywhere_is_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("PR_REVIEWER_REPOS", raising=False)
    d = Dispatcher({}, Telemetry(tmp_path))
    assert d.repos == []


def test_shadow_and_promotion_env_fallback(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_REVIEWER_SHADOW_MODE", "false")
    monkeypatch.setenv("PR_REVIEWER_PROMOTION_OWNER", "true")
    d = Dispatcher({}, Telemetry(tmp_path))
    assert d.shadow is False and d.promotion_owner is True


def test_bool_defaults_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("PR_REVIEWER_SHADOW_MODE", raising=False)
    monkeypatch.delenv("PR_REVIEWER_PROMOTION_OWNER", raising=False)
    d = Dispatcher({}, Telemetry(tmp_path))
    assert d.shadow is True and d.promotion_owner is False  # safe defaults


def test_explicit_config_bool_wins_over_env(monkeypatch, tmp_path):
    # A present key wins even when it's the "falsy" value — an operator who set
    # shadow_mode: false in config must not be flipped back to shadow by a stale env.
    monkeypatch.setenv("PR_REVIEWER_SHADOW_MODE", "true")
    d = Dispatcher({"shadow_mode": False}, Telemetry(tmp_path))
    assert d.shadow is False


# ── panel retry before exhaustion (D3's "retry or escalate", issue #18) ───────


async def test_a_transient_panel_failure_is_retried_and_the_review_lands(tmp_path):
    gh = RoutedGH(pr_facts=facts())
    attempts = []

    async def runner(name, inputs):
        attempts.append(name)
        if len(attempts) == 1:
            return {"output": "partial", "failed": ["find_crossfile"]}
        return {"output": REPORT, "failed": []}

    escalations = []
    d = make(tmp_path, gh=gh, runner=runner, inbox=lambda text, **kw: escalations.append(text))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert len(attempts) == 2  # one retry, then a real verdict
    assert escalations == []  # a recovered run is not an operator problem
    assert gh.posted  # and the PR is no longer left UNREVIEWED


async def test_retries_are_bounded_and_still_never_synthesize_a_partial_verdict(tmp_path):
    gh = RoutedGH(pr_facts=facts())
    attempts = []

    async def runner(name, inputs):
        attempts.append(name)
        return {"output": "partial", "failed": ["find_crossfile"]}

    escalations = []
    d = make(tmp_path, cfg={"panel_retries": 2}, gh=gh, runner=runner, inbox=lambda t, **kw: escalations.append(t))
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-exhausted"
    assert len(attempts) == 3  # the original + 2 retries
    # D3 holds: no verdict review posted (the exhaustion comment is not a verdict)
    assert all("event" not in p for p in gh.posted)
    assert escalations and "UNREVIEWED" in escalations[0]


async def test_panel_retries_can_be_disabled(tmp_path):
    attempts = []

    async def runner(name, inputs):
        attempts.append(name)
        return {"output": "partial", "failed": ["report"]}

    d = make(tmp_path, cfg={"panel_retries": 0}, gh=RoutedGH(pr_facts=facts()), runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-exhausted"
    assert len(attempts) == 1


async def test_a_crashing_runner_is_retried_too(tmp_path):
    attempts = []

    async def runner(name, inputs):
        attempts.append(name)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=RoutedGH(pr_facts=facts()), runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    assert len(attempts) == 2


# ── re-gate: arm a block CI timing beat us to (issue #16) ─────────────────────


def formal(tmp_path, gh):
    return make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)


async def test_regate_arms_a_pending_ci_fail_once_checks_go_terminal(tmp_path):
    # The exact shape of the miss: a FAIL that posted as a COMMENT because CI was
    # still queued when the panel landed.
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", state="COMMENTED")], checks=green)
    d = formal(tmp_path, gh)
    assert (await d.evaluate_regate("o/r", 1)) == "regate"
    assert gh.posted[0]["event"] == "REQUEST_CHANGES"
    # the original judgement is re-used verbatim — no second panel spend
    assert "verdict=FAIL" in gh.posted[0]["body"]


async def test_regate_holds_while_checks_are_pending(tmp_path):
    pending = [{"status": "in_progress", "conclusion": None}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", state="COMMENTED")], checks=pending)
    d = formal(tmp_path, gh)
    assert (await d.evaluate_regate("o/r", 1)) == "hold:regate-checks-pending"
    assert gh.posted == []  # #863: never block against non-terminal CI


async def test_regate_holds_when_checks_are_unreadable(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", state="COMMENTED")], checks=None)
    d = formal(tmp_path, gh)
    assert (await d.evaluate_regate("o/r", 1)) == "hold:regate-checks-unknown"
    assert gh.posted == []


async def test_regate_is_idempotent_once_the_block_is_up(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    reviews = [review_row(HEAD, "FAIL", state="COMMENTED"), review_row(HEAD, "FAIL", state="CHANGES_REQUESTED")]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
    d = formal(tmp_path, gh)
    assert (await d.evaluate_regate("o/r", 1)) == "hold:regate-already-blocking"
    assert gh.posted == []


async def test_regate_never_fires_in_shadow(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", state="COMMENTED")], checks=green)
    d = make(tmp_path, gh=gh)  # shadow default
    assert (await d.evaluate_regate("o/r", 1)) == "hold:regate-shadow"
    assert gh.posted == []


async def test_regate_ignores_a_fail_that_a_later_verdict_superseded(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    reviews = [review_row(HEAD, "FAIL", state="COMMENTED"), review_row(HEAD, "PASS")]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
    d = formal(tmp_path, gh)
    assert (await d.evaluate_regate("o/r", 1)) == "hold:regate-no-current-fail"
    assert gh.posted == []


async def test_regate_ignores_a_fail_against_a_stale_head(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(OLD_HEAD, "FAIL", state="COMMENTED")], checks=green)
    d = formal(tmp_path, gh)
    assert (await d.evaluate_regate("o/r", 1)) == "hold:regate-no-current-fail"
    assert gh.posted == []


async def test_regate_backs_off_after_repeated_post_failures(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]

    class RefusingGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            if "-X" in args and "POST" in args and "/reviews" in " ".join(args):
                self.calls.append(args)
                return 1, "", "422 Unprocessable"
            return await super().__call__(args, timeout)

    gh = RefusingGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", state="COMMENTED")], checks=green)
    escalations = []
    d = make(
        tmp_path,
        cfg={"shadow_mode": False, "promotion_owner": True},
        gh=gh,
        inbox=lambda text, **kw: escalations.append(text),
    )
    for _ in range(3):
        assert (await d.evaluate_regate("o/r", 1)) == "error:regate-failed"
    assert (await d.evaluate_regate("o/r", 1)) == "hold:regate-backoff"
    assert escalations and "NOT blocking" in escalations[0]


# ── backfill: a first review the event stream never delivered (issue #17) ─────


async def test_backfill_is_needed_only_when_the_current_head_has_no_verdict(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    assert (await make(tmp_path, gh=gh).needs_backfill("o/r", 1)) == HEAD

    gh2 = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")])
    assert (await make(tmp_path, gh=gh2).needs_backfill("o/r", 1)) is None

    # a verdict against an OLD head does not count — that PR is unreviewed at HEAD
    gh3 = RoutedGH(pr_facts=facts(), reviews=[review_row(OLD_HEAD, "PASS")])
    assert (await make(tmp_path, gh=gh3).needs_backfill("o/r", 1)) == HEAD


async def test_backfill_skips_drafts_and_closed_prs(tmp_path):
    gh = RoutedGH(pr_facts=facts(draft=True), reviews=[])
    assert (await make(tmp_path, gh=gh).needs_backfill("o/r", 1)) is None

    gh2 = RoutedGH(pr_facts=facts(state="closed"), reviews=[])
    assert (await make(tmp_path, gh=gh2).needs_backfill("o/r", 1)) is None


# ── paginated reads: one row per line, never a concatenated array (issue #75) ─


def test_gh_json_rows_accepts_both_shapes_and_refuses_a_partial():
    from pr_reviewer.dispatch import gh_json_rows

    # whole array — a caller that drops --jq, or a fake that returns one array
    assert gh_json_rows('[{"id": 1}, {"id": 2}]') == [{"id": 1}, {"id": 2}]
    # one row per line — what `--jq '.[] | …'` emits
    assert gh_json_rows('{"id": 1}\n{"id": 2}') == [{"id": 1}, {"id": 2}]
    assert gh_json_rows("") == []
    # THE BUG: `--paginate` + `[.[] | …]` concatenates one array per page. Not valid
    # JSON, and not salvageable line-wise either — it must read as unreadable, never
    # as the first page's worth of rows.
    assert gh_json_rows('[{"id": 1}][{"id": 2}]') is None
    assert gh_json_rows('{"id": 1}\nnot json\n{"id": 2}') is None


async def test_our_reviews_reads_every_page_not_just_the_first(tmp_path):
    """A PR crossing 30 reviews used to make this read permanently unparseable, and
    post-#71's fail-closed posture that is a permanent stall: never backfilled, never
    promoted, never re-gated."""
    # Shaped like the real read: its jq selects `.user.login` into `author`, and only our
    # own reviews are read as rounds — so each served row carries our login.
    rows = [{**review_row(HEAD, "PASS"), "author": "qa-bot"}, {**review_row(OLD_HEAD, "FAIL"), "author": "qa-bot"}]

    class PagedGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            if "/reviews" in " ".join(args):
                # two pages, each already filtered to one object per line
                return 0, "\n".join(json.dumps(r) for r in rows), ""
            return await super().__call__(args, timeout)

    d = make(tmp_path, gh=PagedGH(pr_facts=facts()))
    ours = await d._our_reviews("o/r", 1)
    assert ours is not None and len(ours) == 2
    assert {r["head"] for r in ours} == {HEAD, OLD_HEAD}


async def test_our_reviews_fails_closed_on_the_concatenated_array_shape(tmp_path):
    """Belt and braces: if the old filter ever comes back, this read must go None
    (unreadable ⇒ every caller holds) rather than silently yield page one."""

    class LegacyGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            if "/reviews" in " ".join(args):
                page = json.dumps([{"id": 1, "state": "COMMENTED", "body": "x"}])
                return 0, page + page, ""
            return await super().__call__(args, timeout)

    assert (await make(tmp_path, gh=LegacyGH(pr_facts=facts()))._our_reviews("o/r", 1)) is None


async def test_checks_state_sees_a_failure_on_the_second_page(tmp_path):
    """A commit with 30+ check runs paginates, and this decides PROMOTION — dropping
    the one failed run on page 2 would read as all-green."""

    class PagedChecksGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            if "/check-runs" in " ".join(args):
                lines = [
                    json.dumps({"status": "completed", "conclusion": "success"}),
                    json.dumps({"status": "completed", "conclusion": "failure"}),
                ]
                return 0, "\n".join(lines), ""
            return await super().__call__(args, timeout)

    assert (await make(tmp_path, gh=PagedChecksGH(pr_facts=facts()))._checks_state("o/r", HEAD)) == "failed"


# ── sweep scope comes from the App installation when no allowlist is set ──────


class InstallGH(FakeGH):
    """Serves /installation/repositories; counts how often it is asked."""

    def __init__(self, repos, rc=0):
        super().__init__()
        self.repos_out, self.rc, self.enumerations = repos, rc, 0

    async def __call__(self, args, timeout=30):
        self.calls.append(args)
        if "/installation/repositories" in " ".join(args):
            self.enumerations += 1
            if self.rc:
                return self.rc, "", "HTTP 503"
            return 0, "\n".join(self.repos_out), ""
        return 0, "[]", ""


async def test_sweep_scope_falls_back_to_the_installation_when_no_allowlist(tmp_path):
    gh = InstallGH(["o/a", "o/b"])
    d = make(tmp_path, cfg={"repos": []}, gh=gh)
    assert (await d.sweep_repos()) == ["o/a", "o/b"]


async def test_an_explicit_allowlist_still_wins(tmp_path):
    gh = InstallGH(["o/a", "o/b"])
    d = make(tmp_path, cfg={"repos": ["o/only"]}, gh=gh)
    assert (await d.sweep_repos()) == ["o/only"]
    assert gh.enumerations == 0  # no reason to ask GitHub


async def test_installation_scope_is_cached_between_sweeps(tmp_path):
    gh = InstallGH(["o/a"])
    d = make(tmp_path, cfg={"repos": []}, gh=gh)
    for _ in range(4):
        assert (await d.sweep_repos()) == ["o/a"]
    assert gh.enumerations == 1  # the sweep ticks every ~3min; don't re-ask each time


async def test_a_successful_empty_enumeration_is_authoritative(tmp_path):
    """ "The App is installed nowhere" is an ANSWER, not a failure. Reusing the cache
    for it meant uninstalling the App from a repo silently did nothing — the sweep
    kept walking it for the life of the process."""
    gh = InstallGH(["o/a", "o/b"])
    d = make(tmp_path, cfg={"repos": []}, gh=gh)
    assert (await d.sweep_repos()) == ["o/a", "o/b"]

    gh.repos_out = []  # uninstalled from everything
    d._installation_repos_at = 0.0
    assert (await d.sweep_repos()) == []
    # …and the empty answer is CACHED, not re-asked on every tick
    assert (await d.sweep_repos()) == []
    assert gh.enumerations == 2


async def test_a_failed_enumeration_reuses_the_last_good_scope(tmp_path):
    """Fail closed on scope: reuse what we knew, never invent or silently widen."""
    gh = InstallGH(["o/a", "o/b"])
    d = make(tmp_path, cfg={"repos": []}, gh=gh)
    assert (await d.sweep_repos()) == ["o/a", "o/b"]
    d._installation_repos_at = 0.0  # force a refresh
    gh.rc = 1
    assert (await d.sweep_repos()) == ["o/a", "o/b"]

    # and with nothing cached at all, the sweep simply skips a pass
    gh2 = InstallGH([], rc=1)
    assert (await make(tmp_path, cfg={"repos": []}, gh=gh2).sweep_repos()) == []


async def test_empty_allowlist_reviews_any_repo_the_webhook_delivers(tmp_path):
    """The webhook half already allowed all on an empty list; assert it, because the
    sweep half quietly did the opposite until sweep_repos existed."""
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, cfg={"repos": []}, gh=gh)
    assert (await d.handle_pr_event("brand/new", 1, HEAD, "opened")) != "drop:unlisted-repo"


# ── blind on our own reviews: every path fails CLOSED (issue #71) ─────────────


async def test_unreadable_reviews_never_backfill(tmp_path):
    """The 2026-08-17 loop, in one assertion.

    An unreadable reviews read used to yield `[]`, indistinguishable from "this PR has
    no verdict" — so the sweep backfilled a PR that WAS already reviewed, spent a full
    panel, and posted a duplicate review. Ten times each on two PRs against a static
    head. A read failure must never be read as an absence.
    """
    gh = RoutedGH(pr_facts=facts(), reviews_rc=1, reviews_err="gh: ... (HTTP 503)")
    assert (await make(tmp_path, gh=gh).needs_backfill("o/r", 1)) is None

    # And the contrast that must keep working: genuinely no reviews ⇒ backfill.
    gh2 = RoutedGH(pr_facts=facts(), reviews=[])
    assert (await make(tmp_path, gh=gh2).needs_backfill("o/r", 1)) == HEAD


async def test_unreadable_reviews_drop_the_review_instead_of_spending_the_panel(tmp_path):
    """Blind, the reaffirm short-circuit misses and `round_number` resets to 1 — which
    disarms the max-rounds cap and the convergence rule together. Drop instead."""
    gh = RoutedGH(pr_facts=facts(), reviews_rc=1, reviews_err="HTTP 502")
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:reviews-unreadable"
    assert ran == []  # the panel was never spent
    assert gh.posted == []  # and nothing was posted on the PR


async def test_unreadable_reviews_hold_promotion_and_regate(tmp_path):
    from pr_reviewer.dispatch import HOLD_REVIEWS_UNREADABLE

    gh = RoutedGH(pr_facts=facts(), reviews_rc=1, reviews_err="HTTP 503", checks=[])
    d = make(tmp_path, cfg={"promotion_owner": True, "shadow_mode": False}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == HOLD_REVIEWS_UNREADABLE
    assert gh.posted == []  # no APPROVE on a head we cannot confirm we haven't approved

    gh2 = RoutedGH(pr_facts=facts(), reviews_rc=1, reviews_err="HTTP 503", checks=[])
    d2 = make(tmp_path, cfg={"shadow_mode": False, "regate": True}, gh=gh2)
    assert (await d2.evaluate_regate("o/r", 1)) == HOLD_REVIEWS_UNREADABLE


async def test_unreadable_reviews_dismiss_no_blocks(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews_rc=1, reviews_err="HTTP 503")
    await make(tmp_path, gh=gh)._dismiss_stale_blocks("o/r", 1)
    assert gh.dismissed == []


async def test_sweep_backfills_a_never_reviewed_pr(tmp_path):
    # The stuck shape observed in production: an open PR that predates the reviewer,
    # holding hold:no-clear-verdict on every tick with nothing to break the loop.
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    ran = []

    async def runner(name, inputs):
        ran.append(inputs["pr"])
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh, runner=runner)
    assert (await d.sweep_once()) == 1
    await d.drain_backfills()  # the sweep starts the panel and moves on
    assert ran == ["1"]  # the sweep created the first review itself
    assert gh.posted and "verdict=FAIL" in gh.posted[0]["body"]


async def test_backfill_still_honours_the_self_authored_rail(tmp_path):
    gh = RoutedGH(pr_facts=facts(author="qa-bot[bot]"), reviews=[])
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.backfill_review("o/r", 1, HEAD)) == "drop:self-authored"
    assert gh.posted == []


async def test_sweep_backfill_waits_on_an_injected_panel_semaphore(tmp_path):
    """The sweep shares a process with the webhook, so its backfill panels honour the
    same cross-PR cap (#96): with the semaphore full, a backfill QUEUES — and says so in
    telemetry — instead of launching a panel on top of a live burst. When a slot frees it
    proceeds and posts, proving the bound queues rather than drops."""
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, gh=gh)
    d.panel_sem = asyncio.Semaphore(0)  # every slot held by (imagined) in-flight webhook panels

    task = asyncio.create_task(d.backfill_review("o/r", 1, HEAD))
    for _ in range(50):  # give the backfill every chance to run; it must stay parked
        await asyncio.sleep(0)
        if task.done():
            break
    assert not task.done()  # queued behind the full semaphore
    assert gh.posted == []  # the panel never started, so nothing was posted yet
    queued = [e for e in d.telemetry.read_all() if e["event"] == "queued"]
    assert queued and queued[0]["kind"] == "sweep-backfill"

    d.panel_sem.release()  # a webhook panel finished — the queued backfill gets the slot
    outcome = await asyncio.wait_for(task, timeout=5)
    assert outcome.startswith("reviewed:")  # it ran to a verdict
    assert gh.posted  # …and finally posted it — the dispatch queued, it did not drop


async def test_backfill_budget_bounds_one_sweep_pass(tmp_path):
    """A deployment adopting a repo with a backlog must not fire N panels at once."""

    class ManyPRsGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            joined = " ".join(args)
            if "/pulls?" in joined:
                self.calls.append(args)
                return 0, "[1, 2, 3, 4, 5]", ""
            if "/pulls/" in joined and "/files" not in joined and "/reviews" not in joined:
                self.calls.append(args)
                return 0, json.dumps(self.pr_facts), ""
            return await super().__call__(args, timeout)

    gh = ManyPRsGH(pr_facts=facts(), reviews=[])
    ran = []

    async def runner(name, inputs):
        ran.append(inputs["pr"])
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"backfill_per_pass": 2, "shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.sweep_once()) == 5  # every PR still reconciled
    await d.drain_backfills()
    assert len(ran) == 2  # but only the budgeted number of panels spent


async def test_a_slow_backfill_does_not_hold_up_the_rest_of_the_sweep(tmp_path):
    """The pass is the only thing that re-gates and promotes, for every PR: a 10-minute
    panel run inline froze all of it. The sweep starts the backfill and moves on."""

    class TwoPRsGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            joined = " ".join(args)
            if "/pulls?" in joined:
                self.calls.append(args)
                return 0, "[1, 2]", ""
            return await super().__call__(args, timeout)

    release = asyncio.Event()
    started = []

    async def runner(name, inputs):
        started.append(inputs["pr"])
        await release.wait()  # a panel that never finishes on its own
        return {"output": REPORT, "failed": []}

    gh = TwoPRsGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, cfg={"backfill_per_pass": 1, "shadow_mode": False}, gh=gh, runner=runner)
    assert (await asyncio.wait_for(d.sweep_once(), timeout=5)) == 2  # returned with the panel still running
    await asyncio.sleep(0)
    assert len(d._backfills) == 1
    # A second pass while it runs: the cap is full, so nothing new starts and nothing blocks.
    assert (await asyncio.wait_for(d.sweep_once(), timeout=5)) == 2
    assert len(d._backfills) == 1
    release.set()
    await d.drain_backfills()
    assert len(started) == 1 and not d._backfills  # one panel, never a duplicate


async def test_a_detached_backfill_that_raises_is_logged_not_lost(tmp_path, caplog):
    d = make(tmp_path, gh=RoutedGH(pr_facts=facts(), reviews=[]))

    async def boom(repo, pr, head):
        raise RuntimeError("panel blew up")

    d.backfill_review = boom
    assert d._detach_backfill("o/r", 1, HEAD) == "backfill:started"
    await d.drain_backfills()
    assert not d._backfills and "detached backfill of o/r#1 failed" in caplog.text


async def test_reconcile_prefers_regate_over_promotion_on_the_same_pass(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", state="COMMENTED")], checks=green)
    d = formal(tmp_path, gh)
    outcome, _budget = await d.reconcile_pr("o/r", 1, backfill_budget=0)
    assert outcome == "regate"
    # exactly one write: the block. Nothing approves a PR we just blocked.
    assert [p["event"] for p in gh.posted] == ["REQUEST_CHANGES"]


def test_int_knobs_fall_back_to_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PR_REVIEWER_PANEL_RETRIES", "3")
    monkeypatch.setenv("PR_REVIEWER_BACKFILL_PER_PASS", "7")
    d = Dispatcher({}, Telemetry(tmp_path))
    assert d.panel_retries == 3 and d.backfill_per_pass == 7

    monkeypatch.setenv("PR_REVIEWER_PANEL_RETRIES", "not-a-number")
    assert Dispatcher({}, Telemetry(tmp_path)).panel_retries == 1  # unreadable → default


async def test_regate_can_be_disabled_without_leaving_formal_mode(tmp_path):
    """The blast-radius switch: stop arming blocks (e.g. the panel is emitting false
    FAILs) while KEEPING the formal seat, promotion and backfill."""
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL", state="COMMENTED")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True, "regate": False}, gh=gh)
    assert (await d.evaluate_regate("o/r", 1)) == "hold:regate-disabled"
    assert gh.posted == []
    assert d.shadow is False and d.promotion_owner is True  # still a formal, promoting seat


async def test_disabling_regate_leaves_promotion_working(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True, "regate": False}, gh=gh)
    outcome, _ = await d.reconcile_pr("o/r", 1, backfill_budget=0)
    assert outcome == "promote"
    assert gh.reviews_posted[0]["event"] == "APPROVE"


def test_regate_env_fallback_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PR_REVIEWER_REGATE", raising=False)
    assert Dispatcher({}, Telemetry(tmp_path)).regate_enabled is True  # on by default
    monkeypatch.setenv("PR_REVIEWER_REGATE", "false")
    assert Dispatcher({}, Telemetry(tmp_path)).regate_enabled is False
    # a present config key wins over the env, in both directions
    assert Dispatcher({"regate": True}, Telemetry(tmp_path)).regate_enabled is True


# ── convergence: rounds, request memory, the exit rule (issue #23) ────────────

MID_HEAD = "c" * 40
MINOR = [{"file": "x.py", "line": 12, "severity": "minor", "claim": "dup", "evidence": "e", "verdict": "confirmed"}]
MINOR_REPORT = "Brief prose.\n\n```json\n" + json.dumps(MINOR) + "\n```"
PATCH = "@@ -10,3 +10,6 @@ def f():\n ctx\n+a\n+b\n+c\n"


def promotion_row(head, verdict="WARN"):
    """What approve-on-green posts: our marker, and no findings JSON at all."""
    return {
        "state": "APPROVED",
        "id": 9,
        "body": (
            f"<!-- protoagent-qa-review head={head} verdict={verdict} promoted=true -->\n"
            f"Promoting the {verdict} verdict for head `{head[:12]}`."
        ),
    }


def capturing_runner(report=REPORT):
    seen = {}

    async def runner(name, inputs):
        seen.update(name=name, inputs=inputs)
        return {"output": report, "failed": []}

    return runner, seen


# ── the finder budget is operator config, not a recipe constant (issue #93) ────


async def test_finder_timeout_is_passed_to_the_recipe_only_when_set(tmp_path):
    runner, seen = capturing_runner()
    d = make(tmp_path / "unset", gh=RoutedGH(pr_facts=facts()), runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert "finder_timeout" not in seen["inputs"]  # the recipe's own default applies

    runner, seen = capturing_runner()
    d = make(tmp_path / "set", cfg={"finder_timeout_s": 1200}, gh=RoutedGH(pr_facts=facts()), runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert seen["inputs"]["finder_timeout"] == 1200


def test_finder_timeout_reads_env_clamps_and_never_means_unbounded(tmp_path, monkeypatch, caplog):
    assert make(tmp_path / "a").finder_timeout_s == 0
    for junk in ("soon", "", None, -5, 0):  # unset, never "no timeout"
        assert make(tmp_path / f"j{junk}", cfg={"finder_timeout_s": junk}).finder_timeout_s == 0
    assert make(tmp_path / "s", cfg={"finder_timeout_s": "1500"}).finder_timeout_s == 1500

    monkeypatch.setenv("PR_REVIEWER_FINDER_TIMEOUT", "1100")
    assert make(tmp_path / "env").finder_timeout_s == 1100
    assert make(tmp_path / "cfg-wins", cfg={"finder_timeout_s": 700}).finder_timeout_s == 700

    # At or above the attempt's own budget, the attempt would be cancelled first — a
    # crashed panel instead of a one-lane Gap. Clamped a minute under it, and said so.
    with caplog.at_level("WARNING"):
        d = make(tmp_path / "big", cfg={"finder_timeout_s": 5000, "panel_attempt_timeout": 1800})
        assert d.finder_timeout_s == 1740
    assert "finder_timeout_s=5000" in caplog.text


async def test_a_promotion_no_longer_shadows_the_prior_findings_recall(tmp_path):
    # #23's root cause: with the promotion review newest, recall used to read a body
    # with no findings JSON — `prior_findings` came through empty and the delta
    # re-review silently degraded to a cold first review.
    prior = json.dumps([{"file": "x.py", "line": 1, "severity": "minor", "claim": "old", "evidence": "e"}])
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "WARN", findings_json=prior), promotion_row(OLD_HEAD)],
    )
    runner, seen = capturing_runner()
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:FAIL"
    assert "old" in seen["inputs"]["prior_findings"]


async def test_the_panels_own_request_history_reaches_the_recipe(tmp_path):
    first = json.dumps([{"file": "x.py", "line": 1, "severity": "major", "claim": "edges dropped", "evidence": "e"}])
    second = json.dumps([{"file": "x.py", "line": 5, "severity": "minor", "claim": "normalize it", "evidence": "e"}])
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "FAIL", findings_json=first), review_row(MID_HEAD, "WARN", findings_json=second)],
    )
    runner, seen = capturing_runner()
    d = make(tmp_path, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    block = seen["inputs"]["prior_requests"]
    assert "edges dropped" in block and "normalize it" in block
    assert '<round number="1"' in block and '<round number="2"' in block
    assert seen["inputs"]["review_round"] == "3"


async def test_first_review_carries_no_request_history(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    runner, seen = capturing_runner()
    d = make(tmp_path, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert "prior_requests" not in seen["inputs"] and "review_round" not in seen["inputs"]


def two_prior_rounds():
    return [review_row(OLD_HEAD, "FAIL"), review_row(MID_HEAD, "WARN")]


async def test_round_three_minor_in_delta_posts_pass_with_notes(tmp_path):
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=two_prior_rounds(),
        compare=[{"filename": "x.py", "patch": PATCH}],
    )
    runner, _seen = capturing_runner(MINOR_REPORT)
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    body = gh.reviews_posted[0]["body"]
    assert f"head={HEAD} verdict=PASS" in body
    assert "notes, not gates" in body and "- [ ] `x.py:12`" in body  # nothing hidden
    assert '"claim": "dup"' in body  # the findings JSON still ships


async def test_an_unreadable_delta_keeps_the_warn(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=two_prior_rounds(), compare=None)
    runner, _seen = capturing_runner(MINOR_REPORT)
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:WARN"
    assert "notes, not gates" not in gh.posted[0]["body"]


async def test_a_finding_on_code_the_review_never_touched_keeps_the_warn(tmp_path):
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=two_prior_rounds(),
        compare=[{"filename": "other.py", "patch": PATCH}],  # x.py:12 is untouched since MID_HEAD
    )
    runner, _seen = capturing_runner(MINOR_REPORT)
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:WARN"


async def test_a_major_still_fails_at_any_round(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=two_prior_rounds(), compare=[{"filename": "x.py", "patch": PATCH}])
    runner, _seen = capturing_runner()  # REPORT is a confirmed major on x.py
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:FAIL"


async def test_early_rounds_skip_the_compare_read_entirely(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(OLD_HEAD, "WARN")], compare=[])
    runner, _seen = capturing_runner(MINOR_REPORT)
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:WARN"
    assert not any("/compare/" in " ".join(c) for c in gh.calls)


async def test_convergence_can_be_disabled(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=two_prior_rounds(), compare=[{"filename": "x.py", "patch": PATCH}])
    runner, _seen = capturing_runner(MINOR_REPORT)
    d = make(tmp_path, cfg={"convergence_rounds": 0}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:WARN"


def test_convergence_env_fallback_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PR_REVIEWER_CONVERGENCE_ROUNDS", raising=False)
    assert Dispatcher({}, Telemetry(tmp_path)).convergence_rounds == 3
    monkeypatch.setenv("PR_REVIEWER_CONVERGENCE_ROUNDS", "5")
    assert Dispatcher({}, Telemetry(tmp_path)).convergence_rounds == 5
    assert Dispatcher({"convergence_rounds": 0}, Telemetry(tmp_path)).convergence_rounds == 0


# ── the block-hold end to end (issue #26) ────────────────────────────────────


CLEAN_PASS_REPORT = "Nothing found.\n\n```json\n[]\n```"


async def test_a_clean_pass_that_drops_a_prior_major_does_not_dismiss_the_block(tmp_path):
    major = json.dumps([{"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}])
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[
            review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=major, id=77),
        ],
    )
    runner, _seen = capturing_runner(CLEAN_PASS_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    assert gh.dismissed == []  # the standing REQUEST_CHANGES stays up
    body = gh.reviews_posted[0]["body"]
    assert "does not lift the standing block" in body
    assert "real bug" in body  # the dropped finding is named, not merely counted


async def test_a_coverage_capped_round_still_holds_the_standing_block(tmp_path):
    """The coverage cap (#117) comes AFTER the clearance guard. Capped first, this
    zero-finding round would read as WARN, the guard (which only watches a clean PASS)
    would stand down, and a round that reviewed LESS would lift the block a full round
    never could."""
    major = json.dumps([{"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}])
    gh = RoutedGH(
        pr_facts=facts(changed_files=6, additions=300, deletions=50),
        files="x.py\nb\nc\nd\ne\nf\n",
        reviews=[review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=major, id=77)],
    )

    async def runner(name, inputs):
        return {"output": CLEAN_PASS_REPORT, "failed": [], "steps": _panel_steps(find_structural=STRUCTURAL_HARD_STOP)}

    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:WARN"
    assert gh.dismissed == []  # the standing REQUEST_CHANGES stays up
    body = gh.reviews_posted[0]["body"]
    assert "does not lift the standing block" in body and "real bug" in body


async def test_a_second_consecutive_clean_pass_lifts_the_block(tmp_path):
    major = json.dumps([{"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}])
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[
            review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=major, id=77),
            review_row(MID_HEAD, "PASS"),  # the first clean draw — held
        ],
    )
    runner, _seen = capturing_runner(CLEAN_PASS_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    assert gh.dismissed  # corroborated by a second draw — the gate lifts
    assert "does not lift the standing block" not in gh.posted[0]["body"]


async def test_an_ordinary_clean_pass_still_lifts_the_block(tmp_path):
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "WARN", state="CHANGES_REQUESTED", id=77)],
    )
    runner, _seen = capturing_runner(CLEAN_PASS_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    assert gh.dismissed  # no prior major — nothing to hold for


async def test_the_hold_can_be_disabled(tmp_path):
    major = json.dumps([{"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}])
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=major, id=77)],
    )
    runner, _seen = capturing_runner(CLEAN_PASS_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False, "hold_unexplained_clearance": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    assert gh.dismissed


def test_hold_env_fallback_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PR_REVIEWER_HOLD_UNEXPLAINED_CLEARANCE", raising=False)
    assert Dispatcher({}, Telemetry(tmp_path)).hold_unexplained is True
    monkeypatch.setenv("PR_REVIEWER_HOLD_UNEXPLAINED_CLEARANCE", "false")
    assert Dispatcher({}, Telemetry(tmp_path)).hold_unexplained is False


# ── evidence grounding end to end (issue #25) ────────────────────────────────

import base64  # noqa: E402

SRC_WITH_EXPANDUSER = "writable = Path(configured).expanduser()\nwritable.mkdir(parents=True)\n"
FABRICATED_REPORT = (
    "Brief.\n\n```json\n"
    + json.dumps(
        [
            {
                "file": "x.py",
                "line": 36,
                "severity": "blocker",
                "category": "correctness",
                "claim": "It constructs `writable = Path(str(configured))` and drops the expanduser call.",
                "evidence": "The diff moves `writable = Path(str(configured))` in unchanged.",
                "verdict": "confirmed",
            }
        ]
    )
    + "\n```"
)


class GroundingGH(RoutedGH):
    """Serves file contents at a ref, so grounding has a haystack."""

    def __init__(self, *, source: str | None, **kw):
        super().__init__(**kw)
        self.source = source

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/contents/" in joined:
            if self.source is None:
                return 1, "", "404"
            return 0, "base64\x00" + base64.b64encode(self.source.encode()).decode(), ""
        if "/files" in joined and "--jq" in joined and ".patch" in joined:
            return 0, json.dumps([{"f": "x.py", "p": ""}]), ""
        return await super().__call__(args, timeout=timeout)


async def test_a_fabricated_blocker_is_downgraded_and_cannot_fail(tmp_path):
    # protoAgent#2138: a confirmed blocker quoting code absent from the head. Without
    # grounding this posts FAIL and blocks a correct PR.
    gh = GroundingGH(source=SRC_WITH_EXPANDUSER, pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"
    body = gh.reviews_posted[0]["body"]
    assert gh.posted[0]["event"] == "COMMENT"  # not REQUEST_CHANGES
    assert "downgraded to **uncertain**" in body
    assert "Path(str(configured))" in body  # the absent quote is named


async def test_a_grounded_blocker_still_fails(tmp_path):
    gh = GroundingGH(source="writable = Path(str(configured))\n", pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_an_unreadable_blob_never_downgrades(tmp_path):
    gh = GroundingGH(source=None, pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"  # fail open


async def test_grounding_can_be_disabled(tmp_path):
    gh = GroundingGH(source=SRC_WITH_EXPANDUSER, pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_grounding": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


def test_grounding_env_fallback_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PR_REVIEWER_EVIDENCE_GROUNDING", raising=False)
    assert Dispatcher({}, Telemetry(tmp_path)).grounding_enabled is True
    monkeypatch.setenv("PR_REVIEWER_EVIDENCE_GROUNDING", "false")
    assert Dispatcher({}, Telemetry(tmp_path)).grounding_enabled is False


# ── PR-head file grounding: reliable reads, fail closed on unreadable (issue #109) ──


class HeadReadGH(RoutedGH):
    """Grounding fake with an independently-controllable head-file read and PR patch, so a
    404 on the head read can be reproduced WHILE a patch IS present — the exact #109 path,
    which `GroundingGH` (patch always empty) cannot express."""

    def __init__(self, *, source: str | None, patch: str = "", **kw):
        super().__init__(**kw)
        self.source = source
        self.patch = patch
        self.contents_calls: list[str] = []  # the `?ref=` string on every head read

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/contents/" in joined:
            self.contents_calls.append(args[1] if len(args) > 1 else "")
            if self.source is None:
                return 1, "", "404 Not Found"  # orphaned SHA / transient / wrong ref
            return 0, "base64\x00" + base64.b64encode(self.source.encode()).decode(), ""
        if "/files" in joined and "--jq" in joined and ".patch" in joined:
            return 0, json.dumps([{"f": "x.py", "p": self.patch}]), ""
        return await super().__call__(args, timeout=timeout)


# A patch that is genuinely present but does NOT contain the finding's quoted code — the
# quote lives in unchanged head context, exactly the case the head read (not the patch) must
# supply. Grounding this against the patch alone would find the quote "absent" and downgrade.
_UNRELATED_PATCH = "@@ -1,2 +1,3 @@\n a\n+unrelated = True\n b\n"
_TERMINAL_CHECKS = [{"status": "completed", "conclusion": "failure"}]  # so a FAIL can arm REQUEST_CHANGES


async def test_the_head_read_is_pinned_to_the_immutable_head_sha(tmp_path):
    # r1/r6: the contextual read targets `ref=<head SHA>`, never a bare path (which would
    # resolve the movable default branch / current tip and mis-ground against a wrong head).
    gh = HeadReadGH(source=SRC_WITH_EXPANDUSER, patch="", pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert gh.contents_calls, "the verifier's contextual read never ran"
    assert all(f"ref={HEAD}" in c for c in gh.contents_calls)  # pinned to the reviewed head
    assert not any(c.split("?")[0].endswith("/contents/x.py") and "ref=" not in c for c in gh.contents_calls)


async def test_a_head_404_with_a_patch_present_does_not_downgrade(tmp_path):
    # THE #109 bug: the head read 404s (orphaned SHA / transient), but the PR patch IS
    # present. The old path grounded the blocker against the patch alone, found its quote
    # absent, and downgraded a real blocker to uncertain (a fail-open→fail-closed flip). A
    # failed READ is not absent evidence — we learned nothing, so the blocker must STAND.
    gh = HeadReadGH(source=None, patch=_UNRELATED_PATCH, pr_facts=facts(), reviews=[], checks=_TERMINAL_CHECKS)
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"  # NOT downgraded
    body = gh.reviews_posted[0]["body"]
    assert gh.posted[0]["event"] == "REQUEST_CHANGES"  # the blocker still gates the merge
    assert "could NOT be evidence-checked" in body  # source-unavailable is surfaced (r5)
    assert "downgraded to **uncertain**" not in body  # and NOT the fabricated-quote downgrade


async def test_a_successful_read_with_a_patch_still_downgrades_an_absent_quote(tmp_path):
    # r3, the contrast to the test above: SAME finding, SAME (unrelated) patch, but the head
    # read SUCCEEDS and the file genuinely lacks the quoted construction. A read that shows
    # the quote absent keeps the fabricated-evidence downgrade — this half must not regress.
    gh = HeadReadGH(source=SRC_WITH_EXPANDUSER, patch=_UNRELATED_PATCH, pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"  # downgraded
    body = gh.reviews_posted[0]["body"]
    assert "downgraded to **uncertain**" in body
    assert "could NOT be evidence-checked" not in body  # distinct from the unreadable state


async def test_a_zero_byte_head_file_is_a_successful_read_and_downgrades(tmp_path):
    # The review-flagged regression: a zero-byte file at the head reads back as empty
    # `.content` (`rc == 0`, `out.strip() == ""`). Gating the read on non-empty output would
    # misclassify that real, empty file as UNREADABLE and PRESERVE the fabricated blocker's
    # gating verdict. An empty file was READ — its quote is genuinely absent, so the blocker
    # must DOWNGRADE, not stand as source-unavailable.
    gh = HeadReadGH(source="", patch="", pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:WARN"  # downgraded
    body = gh.reviews_posted[0]["body"]
    assert gh.posted[0]["event"] == "COMMENT"  # not REQUEST_CHANGES — the gate is lifted
    assert "downgraded to **uncertain**" in body
    assert "could NOT be evidence-checked" not in body  # NOT the unreadable/could-not-verify state


async def test_finding_sources_treats_an_oversized_file_as_unreadable(tmp_path):
    """GitHub returns `content: ""` with `encoding: "none"` for a 1–100 MB file — OMITTED,
    not empty. Treating it as a zero-byte read marked it read_ok, so a real finding in an
    oversized file was downgraded for evidence that was never fetched. `encoding` is the
    only field that separates an omitted file from a genuinely empty one."""
    from pr_reviewer.grounding import UNREADABLE

    class BigFileGH:
        async def __call__(self, args, timeout=30):
            joined = " ".join(args)
            if "/pulls/" in joined and "/files" in joined:
                return 0, json.dumps([{"filename": "big.bin", "patch": "patchB"}]), ""
            if "/contents/big.bin" in joined:
                return 0, "none\x00", ""  # 1–100 MB: encoding none, content omitted
            return 1, "", "unexpected call"

    d = make(tmp_path, gh=BigFileGH())
    sources = await d._finding_sources("o/r", 1, HEAD, [{"file": "big.bin"}])
    assert sources["big.bin"][1] is UNREADABLE  # severity preserved — never a silent downgrade


async def test_finding_sources_splits_empty_read_from_null_content(tmp_path):
    # Unit-level proof of the two `rc == 0` branches the review flagged:
    #   * empty `.content` (a zero-byte file) is a SUCCESSFUL read → `combined` is a real
    #     string haystack (blob + patch), so grounding can still prove a quote absent;
    #   * an absent object (a submodule / directory) is
    #     NOT readable source → the `UNREADABLE` sentinel, which preserves severity.
    from pr_reviewer.grounding import UNREADABLE

    class TwoFileGH:
        async def __call__(self, args, timeout=30):
            joined = " ".join(args)
            if "/pulls/" in joined and "/files" in joined:
                return 0, json.dumps([{"f": "empty.py", "p": "patchE"}, {"f": "sub", "p": "patchS"}]), ""
            if "/contents/empty.py" in joined:
                return 0, "base64\x00", ""  # zero-byte file: base64 encoding, empty content
            if "/contents/sub" in joined:
                return 0, "\x00", ""  # `.content` absent — not a readable file
            return 1, "", "unexpected call"

    d = make(tmp_path, gh=TwoFileGH())
    sources = await d._finding_sources("o/r", 1, HEAD, [{"file": "empty.py"}, {"file": "sub"}])
    blob_e, combined_e = sources["empty.py"]
    assert blob_e == ""  # the file really is empty…
    assert isinstance(combined_e, str) and combined_e.endswith("patchE")  # …but the read SUCCEEDED
    _blob_s, combined_s = sources["sub"]
    assert combined_s is UNREADABLE  # `null` content cannot ground anything — fail closed


# ── unaccounted priors hold the block at any verdict (issue #26) ─────────────


def report_with_dispositions(dispositions, findings="[]"):
    return "prose\n\n```json\n" + json.dumps(dispositions) + "\n```\n\nbrief\n\n```json\n" + findings + "\n```"


async def test_a_warn_that_drops_a_prior_major_holds_the_block(tmp_path):
    # protoAgent#2150 r3 in miniature: the major vanishes into a WARN about other things.
    # #27's clean-PASS rule cannot see this; the dispositions contract can.
    major = json.dumps([{"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}])
    nit = json.dumps([{"file": "x.py", "line": 9, "severity": "minor", "claim": "unrelated nit", "evidence": "e"}])
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=major, id=77)],
    )
    runner, _seen = capturing_runner(report_with_dispositions([{"prior": "other.py:1", "disposition": "fixed"}], nit))
    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_grounding": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:WARN"
    assert gh.dismissed == []  # the block stays up
    assert "Unaccounted prior finding" in gh.posted[0]["body"]
    assert "real bug" in gh.posted[0]["body"]


async def test_a_dispositioned_major_lets_the_verdict_clear(tmp_path):
    major = json.dumps([{"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}])
    # A `fixed` disposition now clears only if the delta shows x.py:3 actually moved.
    compare = [{"filename": "x.py", "patch": "@@ -1,4 +1,4 @@\n a\n b\n-old line 3\n+fixed line 3\n"}]
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=major, id=77)],
        compare=compare,
    )
    runner, _seen = capturing_runner(
        report_with_dispositions([{"prior": "x.py:3", "disposition": "fixed", "why": "guard added"}])
    )
    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_grounding": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    assert gh.dismissed  # verified fix → the gate lifts
    assert "Unaccounted prior finding" not in gh.posted[0]["body"]


class _CompareByBase(RoutedGH):
    """`compare` keyed by the BASE head of the request, so a test can tell which delta was read."""

    def __init__(self, by_base, **kw):
        super().__init__(**kw)
        self.by_base = by_base

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/compare/" in joined and "merge_base" not in joined:
            base = joined.split("/compare/", 1)[1].split("...", 1)[0]
            if base in self.by_base:
                self.calls.append(args)
                return 0, json.dumps(self.by_base[base]), ""
        return await super().__call__(args, timeout=timeout)


async def test_a_carried_major_is_cleared_by_a_fix_that_predates_the_carrying_round(tmp_path):
    # Issue #131 / mythxengine#805: the major was raised at OLD_HEAD and fixed before
    # MID_HEAD, but MID_HEAD's round lost a lane and CARRIED it. MID_HEAD→HEAD never
    # touches x.py again, so `fixed` was unprovable for good. OLD_HEAD→HEAD shows the fix.
    major = {"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}
    carried = {**major, "verdict": "confirmed", "carried": True, "since": OLD_HEAD}
    gh = _CompareByBase(
        {
            MID_HEAD: [{"filename": "unrelated.py", "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c\n"}],
            OLD_HEAD: [{"filename": "x.py", "patch": "@@ -1,4 +1,4 @@\n a\n b\n-old line 3\n+fixed line 3\n"}],
        },
        pr_facts=facts(),
        reviews=[
            review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=json.dumps([major]), id=77),
            review_row(MID_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=json.dumps([carried]), id=78),
        ],
    )
    runner, _seen = capturing_runner(
        report_with_dispositions([{"prior": "x.py:3", "disposition": "fixed", "why": "guard added"}])
    )
    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_grounding": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    assert "Unaccounted prior finding" not in gh.posted[0]["body"]
    assert "carried from a prior round" not in gh.posted[0]["body"]
    assert any(f"/compare/{OLD_HEAD}..." in " ".join(c) for c in gh.calls)  # proven against the raising head


async def test_a_hallucinated_fixed_disposition_holds_the_block_end_to_end(tmp_path):
    # protoAgent#2208 exactly: FAIL on x.py:3, then a clean PASS whose report claims
    # `fixed` — but the delta touches only OTHER files, so x.py:3 never moved.
    major = json.dumps([{"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}])
    compare = [{"filename": "unrelated.py", "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c\n"}]
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=major, id=77)],
        compare=compare,
    )
    runner, _seen = capturing_runner(
        report_with_dispositions([{"prior": "x.py:3", "disposition": "fixed", "why": "resolved in updated diff"}])
    )
    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_grounding": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    assert gh.dismissed == []  # the block is HELD — an unverified fix does not clear it
    assert "Unaccounted prior finding" in gh.posted[0]["body"]
    assert "real bug" in gh.posted[0]["body"]


async def test_a_false_disposition_still_holds_via_the_narrow_rule_when_the_block_is_absent(tmp_path):
    # The fallback chain: no dispositions block at all → #27's clean-PASS rule applies.
    major = json.dumps([{"file": "x.py", "line": 3, "severity": "major", "claim": "real bug", "evidence": "e"}])
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "FAIL", state="CHANGES_REQUESTED", findings_json=major, id=77)],
    )
    runner, _seen = capturing_runner(CLEAN_PASS_REPORT)  # no dispositions emitted
    d = make(tmp_path, cfg={"shadow_mode": False, "evidence_grounding": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:PASS"
    assert gh.dismissed == []
    assert "does not lift the standing block" in gh.posted[0]["body"]


# ── a promoted WARN carries its findings (issue #22) ─────────────────────────


WARN_FINDING = json.dumps(
    [{"file": "x.py", "line": 4, "severity": "minor", "claim": "malformed diff: label", "evidence": "e"}]
)


async def test_promoting_a_warn_carries_its_findings_into_the_approval(tmp_path):
    # projectBoard-plugin#80: a confirmed minor, promoted 32s later, and the finding had
    # no consumer — the PR just read APPROVED. The defect shipped.
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "WARN", findings_json=WARN_FINDING)], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    body = gh.reviews_posted[0]["body"]
    assert gh.reviews_posted[0]["event"] == "APPROVE"  # still non-blocking — NOT a gate
    assert "findings=1" in body  # machine-readable, no prose parsing needed
    assert "Open findings carried by this approval" in body
    assert "malformed diff: label" in body


async def test_a_clean_pass_promotion_is_unchanged(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    body = gh.reviews_posted[0]["body"]
    assert "findings=" not in body and "Open findings carried" not in body


async def test_our_own_promotion_body_does_not_shadow_the_verdict_it_promoted(tmp_path):
    # `ours[-1]` after an approve-on-green is the promotion body — marker-bearing, no
    # findings. The same shadowing #24 fixed for delta recall; here it would promote
    # with an empty findings list and silently defeat the carry-forward.
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(
        pr_facts=facts(),
        reviews=[review_row(OLD_HEAD, "WARN", findings_json=WARN_FINDING), promotion_row(OLD_HEAD)],
        checks=green,
    )
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    # head has advanced past the promoted one → stale-head hold, but the point is that
    # panel_rounds (not ours[-1]) is what the decision reads.
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:stale-head"


# ── coverage recovered: a coverage-only WARN never outranks a complete round ────

FAIL_FINDING = json.dumps(
    [{"file": "x.py", "line": 3, "severity": "major", "claim": "Bug.", "evidence": "e", "verdict": "confirmed"}]
)


def _qa_check(gh):
    """The last `QA panel` check-run write the promotion path published."""
    writes = [p for p in gh.posted if "check-runs" in p.get("url", "")]
    assert writes, "the QA-panel check should be published"
    return writes[-1]


async def test_a_complete_pass_recovers_a_coverage_only_warn_on_the_same_head(tmp_path):
    # qaEngineer#59 @ 13fbcaba, live: `find_removed_behavior` did not complete, so the
    # round posted `WARN complete=false` with NO findings; a re-review 18 min later was a
    # complete, finding-free PASS. The #89 strictest pick kept the incomplete WARN, so the
    # head held hold:incomplete-coverage (QA panel "Incomplete pass") until a new commit —
    # neither `@vera review` nor a check re-run could ever clear it.
    green = [{"status": "completed", "conclusion": "success"}]
    capped = review_row(HEAD, "WARN", complete=False)
    assert "verdict=WARN promoted=false complete=false" in capped["body"]  # the #59 marker
    complete_pass = review_row(HEAD, "PASS")
    # Order-free, like #89: GitHub returns same-head reviews in an arbitrary order.
    for reviews in ([capped, complete_pass], [complete_pass, capped]):
        gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
        d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
        assert (await d.evaluate_promotion("o/r", 1)) == "promote"
        approval = gh.reviews_posted[0]
        assert approval["event"] == "APPROVE"
        assert f"head={HEAD} verdict=PASS promoted=true -->" in approval["body"]  # the complete PASS governs
        qa = _qa_check(gh)
        assert qa.get("status") == "completed" and qa.get("conclusion") == "success"
        assert qa.get("output[title]") == "Cleared by the QA panel"


async def test_a_warn_with_findings_still_governs_after_a_later_complete_pass(tmp_path):
    # #89's guarantee holds: a WARN that raised a REAL finding is not a coverage cap, so a
    # complete PASS for the same head cannot shadow it — whether that WARN was complete or
    # not. It governs exactly as a complete WARN does today. The panel does not open review
    # threads itself (checks.py), so on green it promotes straight away and the approval
    # carries the WARN's finding forward (#22); the later PASS does not replace it.
    green = [{"status": "completed", "conclusion": "success"}]
    for complete in (True, False):
        reviews = [review_row(HEAD, "WARN", findings_json=WARN_FINDING, complete=complete), review_row(HEAD, "PASS")]
        gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
        d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
        assert (await d.evaluate_promotion("o/r", 1)) == "promote"
        body = gh.reviews_posted[0]["body"]
        assert f"head={HEAD} verdict=WARN promoted=true findings=1 -->" in body  # the WARN, not the PASS
        assert "malformed diff: label" in body
        assert _qa_check(gh).get("conclusion") == "success"


async def test_a_later_verified_round_at_the_same_head_clears_an_unverified_hold(tmp_path):
    # mythxengine-sdk#384, live (#170): round 1 WARN with one finding, `verified=false`
    # (the verifier flaked, #167); round 2 on a re-summon, same head, a verified clean
    # PASS that dispositioned the finding. The strictest pick kept round 1, so the head
    # held hold:unverified until a new commit — the documented remedy never worked.
    green = [{"status": "completed", "conclusion": "success"}]
    unverified = review_row(HEAD, "WARN", findings_json=WARN_FINDING, id=10)
    unverified["body"] = unverified["body"].replace(" -->", " verified=false -->", 1)
    later_verified = review_row(HEAD, "PASS", id=11)
    # Arrival order is not the signal — GitHub's review id is.
    for reviews in ([unverified, later_verified], [later_verified, unverified]):
        gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
        d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
        assert (await d.evaluate_promotion("o/r", 1)) == "promote"
        body = gh.reviews_posted[0]["body"]
        assert f"head={HEAD} verdict=WARN promoted=true findings=1 -->" in body  # the WARN still governs


async def test_an_earlier_verified_round_does_not_clear_a_later_unverified_one(tmp_path):
    # The earlier round never saw the unverified findings as prior requests, so it proves
    # nothing about them: the hold stands.
    green = [{"status": "completed", "conclusion": "success"}]
    earlier_verified = review_row(HEAD, "PASS", id=10)
    unverified = review_row(HEAD, "WARN", findings_json=WARN_FINDING, id=11)
    unverified["body"] = unverified["body"].replace(" -->", " verified=false -->", 1)
    for reviews in ([earlier_verified, unverified], [unverified, earlier_verified]):
        gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
        d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
        assert (await d.evaluate_promotion("o/r", 1)) == "hold:unverified"


async def test_only_unverified_rounds_for_the_head_still_hold_unverified(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    rows = []
    for _ in range(2):
        r = review_row(HEAD, "WARN", findings_json=WARN_FINDING)
        r["body"] = r["body"].replace(" -->", " verified=false -->", 1)
        rows.append(r)
    gh = RoutedGH(pr_facts=facts(), reviews=rows, checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:unverified"


async def test_only_incomplete_rounds_for_the_head_still_hold_incomplete(tmp_path):
    # No complete round ⇒ nothing recovered coverage: two blind passes are not one full
    # pass (#49), so the head still holds exactly as before.
    green = [{"status": "completed", "conclusion": "success"}]
    reviews = [review_row(HEAD, "WARN", complete=False), review_row(HEAD, "WARN", complete=False)]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:incomplete-coverage"
    assert gh.reviews_posted == []
    qa = _qa_check(gh)
    # Held from auto-approve; concluded neutral on the check so a human can still merge (#130).
    assert qa.get("status") == "completed" and qa.get("conclusion") == "neutral"
    assert qa.get("output[title]") == "Incomplete pass — not blocking"


async def test_an_incomplete_fail_with_findings_still_holds_after_a_complete_pass(tmp_path):
    # An incomplete round's findings stand: a FAIL is never a coverage cap, so a complete
    # PASS for the same head — in either arrival order — cannot promote past it.
    green = [{"status": "completed", "conclusion": "success"}]
    blind_fail = review_row(HEAD, "FAIL", findings_json=FAIL_FINDING, complete=False)
    complete_pass = review_row(HEAD, "PASS")
    for reviews in ([blind_fail, complete_pass], [complete_pass, blind_fail]):
        gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
        d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
        assert (await d.evaluate_promotion("o/r", 1)) == "hold:no-clear-verdict"
        assert gh.reviews_posted == []
        assert _qa_check(gh).get("conclusion") != "success"


async def test_an_incomplete_round_without_a_readable_findings_record_stays_strict(tmp_path):
    # Only an EXPLICIT empty array proves a round raised nothing. A body whose findings
    # record is absent or malformed (older, truncated, hand-edited) cannot be read as a
    # pure coverage cap, so it keeps holding even after a complete PASS — fails closed.
    green = [{"status": "completed", "conclusion": "success"}]
    marker = f"<!-- protoagent-qa-review head={HEAD} verdict=WARN promoted=false complete=false -->\n"
    for tail in ("x", '```json\n[{"file": "x.py"\n```'):
        blind = {"state": "COMMENTED", "id": None, "body": marker + tail}
        gh = RoutedGH(pr_facts=facts(), reviews=[blind, review_row(HEAD, "PASS")], checks=green)
        d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
        assert (await d.evaluate_promotion("o/r", 1)) == "hold:incomplete-coverage"
        assert gh.reviews_posted == []


async def test_an_incomplete_fail_with_an_empty_record_still_holds_after_a_complete_pass(tmp_path):
    # A FAIL is never a coverage cap, even with no findings recorded. `verdict_for` never
    # yields one, but the gate must not lean on that: the FAIL keeps its full #89 weight
    # and a complete PASS for the same head, in either order, cannot promote past it.
    green = [{"status": "completed", "conclusion": "success"}]
    blind_fail = review_row(HEAD, "FAIL", complete=False)
    complete_pass = review_row(HEAD, "PASS")
    for reviews in ([blind_fail, complete_pass], [complete_pass, blind_fail]):
        gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
        d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
        assert (await d.evaluate_promotion("o/r", 1)) == "hold:no-clear-verdict"
        assert gh.reviews_posted == []


async def test_a_fenced_array_quoted_in_a_claim_cannot_empty_a_rounds_findings(tmp_path):
    # Claim text is printed after the findings record (here, the confinement footnote),
    # and a claim can quote a fenced array. The round reads the renderer's own record, so
    # the incomplete WARN keeps its real finding: it is not a coverage cap, it governs,
    # and the approval carries that finding forward rather than promoting the PASS.
    green = [{"status": "completed", "conclusion": "success"}]
    real = {"file": "x.py", "line": 4, "severity": "minor", "claim": "real minor defect", "evidence": "e"}
    quoting = {"file": "other.py", "line": 1, "severity": "nit", "claim": "see\n```json\n[]\n```"}
    body = render_verdict_body(
        repo="o/r",
        pr=1,
        head_sha=HEAD,
        verdict="WARN",
        brief="prose",
        findings=[real, quoting],
        shadow=True,
        recipe="code-review-structural",
        confined=[quoting],
        complete=False,
    )
    reviews = [{"state": "COMMENTED", "id": 5, "body": body}, review_row(HEAD, "PASS")]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    approval = gh.reviews_posted[0]["body"]
    assert "verdict=WARN promoted=true" in approval and "real minor defect" in approval


# ── panel rounds come only from the reviewer's own reviews ────────────────────

OTHER_ACCOUNT = "some-human"


def _authored(row: dict, author: str) -> dict:
    """`row` as written by `author` — RoutedGH serves a row's own `author` when it has one."""
    return {**row, "author": author}


async def test_a_review_by_another_account_is_not_a_panel_round(tmp_path):
    # Panel rounds are read only from the reviewer's own reviews. The verdict marker is
    # plain text, and a review by another account that carries one (a human pasting a
    # verdict, say) must not count as a round — not as the complete round that recovers
    # coverage for a head, and not as a verdict to promote on its own.
    green = [{"status": "completed", "conclusion": "success"}]
    quoted = _authored(review_row(HEAD, "PASS"), OTHER_ACCOUNT)
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "WARN", complete=False), quoted], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:incomplete-coverage"
    assert gh.reviews_posted == []

    gh2 = RoutedGH(pr_facts=facts(), reviews=[quoted], checks=green)
    d2 = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh2)
    assert (await d2.evaluate_promotion("o/r", 1)) == "hold:no-clear-verdict"
    assert gh2.reviews_posted == []


async def test_a_review_by_another_account_does_not_mark_the_head_reviewed(tmp_path):
    # The "already reviewed this head" check reads the same list, so another account's
    # marker-bearing review does not make the head reviewed: the panel still runs.
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    gh = RoutedGH(pr_facts=facts(), reviews=[_authored(review_row(HEAD, "PASS"), OTHER_ACCOUNT)])
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reviewed:FAIL"
    assert ran  # reviewed, not reaffirmed


async def test_our_own_reviews_still_count_under_either_login_form(tmp_path):
    # Our login is probed as `qa-bot`; an App posts as `qa-bot[bot]`. Both are ours, and a
    # verdict of ours for the head still reaffirms without spending the panel.
    for author in ("qa-bot", "qa-bot[bot]", "QA-Bot[bot]"):
        gh = RoutedGH(pr_facts=facts(), reviews=[_authored(review_row(HEAD, "PASS"), author)])
        d = make(tmp_path / author, gh=gh)
        assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:PASS"
        assert len(await d._our_reviews("o/r", 1)) == 1


async def test_an_unreadable_viewer_login_holds_promotion(tmp_path):
    # Without our login, our own reviews cannot be told from another account's, so the
    # history is unreadable — the same fail-closed hold as a failed reviews read (#71).
    class NoViewerGH(RoutedGH):
        async def __call__(self, args, timeout=30):
            if len(args) > 1 and args[1] == "user":
                return 1, "", "HTTP 403: Resource not accessible by integration"
            return await super().__call__(args, timeout)

    green = [{"status": "completed", "conclusion": "success"}]
    gh = NoViewerGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d._our_reviews("o/r", 1)) is None
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:reviews-unreadable"
    assert gh.reviews_posted == []


# ── our own login: exact, one-directional (`is_own_login`) ─────────────────────


def test_is_own_login_accepts_the_login_and_only_widens_a_bare_one():
    from pr_reviewer.dispatch import is_own_login

    # A configured App login accepts exactly that login, in any case...
    assert is_own_login("x[bot]", "x[bot]")
    assert is_own_login("X[Bot]", "x[bot]")
    # ...and never the plain account of the same name: that is a different account.
    assert not is_own_login("x", "x[bot]")
    # A bare login also accepts its App form: GitHub allows no App name that collides
    # with an existing account, so `x[bot]` can only be ours.
    assert is_own_login("x", "x")
    assert is_own_login("x[bot]", "x")
    assert is_own_login("X[BOT]", "X")
    # Nothing else, and nothing when either side is unknown.
    assert not is_own_login("xy[bot]", "x")
    assert not is_own_login("", "x") and not is_own_login("x", "")


async def test_a_plain_account_named_like_our_app_is_not_ours(tmp_path):
    # Under `viewer_login: qa-bot[bot]`, a review by a plain account named `qa-bot` is
    # another account's: not a round, so it does not recover coverage for the head.
    green = [{"status": "completed", "conclusion": "success"}]
    ours = _authored(review_row(HEAD, "WARN", complete=False), "qa-bot[bot]")
    plain = _authored(review_row(HEAD, "PASS"), "qa-bot")
    gh = RoutedGH(pr_facts=facts(), reviews=[ours, plain], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True, "viewer_login": "qa-bot[bot]"}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:incomplete-coverage"
    assert gh.reviews_posted == []
    assert [r["verdict"] for r in await d._our_reviews("o/r", 1)] == ["WARN"]


async def test_thread_ownership_uses_the_same_one_directional_login(tmp_path):
    # The QA check's count of the panel's own threads uses the same rule: under
    # `viewer_login: qa-bot[bot]`, a thread opened by a plain `qa-bot` account is external.
    threads = [thread_node("qa-bot"), thread_node("qa-bot[bot]"), thread_node("QA-Bot[bot]")]
    gh = RoutedGH(pr_facts=facts(), threads=threads)
    d = make(tmp_path, cfg={"viewer_login": "qa-bot[bot]"}, gh=gh)
    assert (await d._panel_owned_unresolved("o/r", 1)) == 2


# ── a mistyped viewer_login holds instead of re-reviewing everything ───────────


def _telemetry_events(path, event):
    rows = [
        json.loads(line)
        for f in (path / "telemetry").glob("*.jsonl")
        for line in f.read_text().splitlines()
        if line.strip()
    ]
    return [r for r in rows if r.get("event") == event]


async def test_pr_size_rides_on_the_dispatch_reviewed_and_exhaustion_rows(tmp_path):
    # Issue #116: a PR too large for the lanes exhausts on every head, and nothing recorded
    # size against outcome — so a "too large" threshold could only be guessed.
    size = {"changed_files": 61, "lines_changed": 15514}
    big = facts(changed_files=61, additions=15392, deletions=122)

    async def clean(name, inputs):
        return {"output": REPORT, "failed": []}

    d = make(tmp_path / "ok", gh=RoutedGH(pr_facts=big), runner=clean)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")).startswith("reviewed:")
    for event in ("dispatch", "reviewed"):
        (row,) = _telemetry_events(tmp_path / "ok", event)
        assert {k: row[k] for k in size} == size, event

    async def dead(name, inputs):
        return {"output": "partial", "failed": ["find_correctness"]}

    d = make(tmp_path / "dead", gh=RoutedGH(pr_facts=big), runner=dead, inbox=lambda text, **kw: None)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "error:panel-exhausted"
    (row,) = _telemetry_events(tmp_path / "dead", "exhaustion")
    assert {k: row[k] for k in size} == size


async def test_a_mistyped_viewer_login_holds_instead_of_re_reviewing(tmp_path):
    # Our own reviews arrive under an App login that is not `viewer_login`. Read as "no
    # reviews", every head would look unreviewed: the sweep would backfill and the event
    # path re-run the full panel on every PR, every tick, with the round count stuck at 1
    # (issue #71's failure mode). Another App's marker makes the history UNREADABLE
    # instead, so each caller holds — and the operator is told, once.
    from pr_reviewer.dispatch import DROP_REVIEWS_UNREADABLE

    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    rows = [_authored(review_row(HEAD, "PASS"), "protoreview[bot]")]
    gh = RoutedGH(pr_facts=facts(), reviews=rows)
    d = make(tmp_path, cfg={"viewer_login": "protoreviw[bot]"}, gh=gh, runner=runner)  # typo
    assert (await d._our_reviews("o/r", 1)) is None
    assert (await d.needs_backfill("o/r", 1)) is None  # no backfill on a guess
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == f"drop:{DROP_REVIEWS_UNREADABLE}"
    assert ran == [] and gh.reviews_posted == []  # the panel never ran
    mism = _telemetry_events(tmp_path, "viewer-mismatch")
    assert len(mism) == 1 and mism[0]["actual"] == "protoreview[bot]"  # loud, once

    # The correct login reads the same rows as ours: reaffirmed, no hold, no warning.
    gh2 = RoutedGH(pr_facts=facts(), reviews=rows)
    d2 = make(tmp_path / "ok", cfg={"viewer_login": "protoreview[bot]"}, gh=gh2, runner=runner)
    assert (await d2.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:PASS"
    assert ran == [] and not _telemetry_events(tmp_path / "ok", "viewer-mismatch")


async def test_a_persons_quoted_marker_is_skipped_without_a_hold_or_a_warning(tmp_path):
    # A person cannot post as an App, so a human quoting a verdict is simply not ours: it
    # is skipped, the history stays readable, and the one-time viewer_login warning is
    # NOT spent on it — so a real mismatch later is still reported.
    rows = [_authored(review_row(HEAD, "PASS"), OTHER_ACCOUNT), review_row(HEAD, "PASS")]  # ours is `qa-bot`
    gh = RoutedGH(pr_facts=facts(), reviews=rows)
    d = make(tmp_path, gh=gh)
    ours = await d._our_reviews("o/r", 1)
    assert ours is not None and len(ours) == 1
    assert not _telemetry_events(tmp_path, "viewer-mismatch") and d._viewer_checked is False

    gh.reviews = [*rows, _authored(review_row(HEAD, "PASS"), "renamed-app[bot]")]
    assert (await d._our_reviews("o/r", 1)) is None
    mism = _telemetry_events(tmp_path, "viewer-mismatch")
    assert len(mism) == 1 and mism[0]["actual"] == "renamed-app[bot]"


# ── config is live, not snapshotted (issue #11) ──────────────────────────────


def test_config_edits_take_effect_without_a_restart(tmp_path):
    # The footgun: an operator flips the gate through Settings, sees "config saved /
    # reloaded", and the running dispatcher keeps its boot values. Believing you are
    # formal-blocking a repo you are not is the dangerous direction.
    cfg = {"repos": ["o/r"], "shadow_mode": True, "regate": True}
    d = Dispatcher(cfg, Telemetry(tmp_path), cfg_provider=lambda: cfg)
    assert d.shadow is True and d.repos == ["o/r"] and d.regate_enabled is True

    cfg.clear()
    cfg.update({"repos": ["o/r", "o/newly-managed"], "shadow_mode": False, "regate": False})
    assert d.shadow is False  # the gate flip took effect
    assert "o/newly-managed" in d.repos  # a newly-added repo dispatches
    assert d.regate_enabled is False  # the kill switch works without a restart


def test_a_replaced_config_object_is_still_seen(tmp_path):
    # The host may hand back a NEW dict on reload rather than mutating in place.
    box = {"cfg": {"shadow_mode": True}}
    d = Dispatcher(box["cfg"], Telemetry(tmp_path), cfg_provider=lambda: box["cfg"])
    assert d.shadow is True
    box["cfg"] = {"shadow_mode": False}
    assert d.shadow is False


def test_a_failing_provider_falls_back_to_boot_config(tmp_path):
    def _boom():
        raise RuntimeError("config store is down")

    d = Dispatcher({"shadow_mode": False}, Telemetry(tmp_path), cfg_provider=_boom)
    assert d.shadow is False  # degrades to boot values; a review never dies on this


def test_without_a_provider_the_boot_config_still_applies(tmp_path):
    d = Dispatcher({"shadow_mode": False, "repos": ["o/r"]}, Telemetry(tmp_path))
    assert d.shadow is False and d.repos == ["o/r"]


async def test_a_promotion_carrying_findings_is_not_re_promoted(tmp_path):
    # The regression end to end: v0.13.0's `findings=N` broke marker parsing, so the
    # APPROVE was not recognised as ours and the sweep re-approved every tick.
    green = [{"status": "completed", "conclusion": "success"}]
    warn_finding = json.dumps([{"file": "x.py", "line": 4, "severity": "minor", "claim": "c", "evidence": "e"}])
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "WARN", findings_json=warn_finding)], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    body = gh.reviews_posted[0]["body"]
    assert "findings=1" in body

    # Feed our own promotion back in, exactly as _our_reviews would see it next tick.
    gh.reviews.append({"state": "APPROVED", "body": body, "id": 99})
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:already-promoted"
    assert len(gh.reviews_posted) == 1  # not a second APPROVE


# ── a summon forces a review the reaffirm path would have skipped (#28) ──────


async def test_a_summon_re_reviews_an_unchanged_head(tmp_path):
    # Normally an unchanged head with a posted verdict reaffirms without spending the
    # panel. `@vera review` on that head is the "I think you got this wrong" case —
    # reaffirming would answer the question with the answer under dispute.
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "FAIL")])
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "reaffirmed:FAIL"
    assert ran == []
    assert (await d.handle_summon("o/r", 1, "an-admin")) == "reviewed:FAIL"
    assert len(ran) == 1  # the panel actually ran (recipe choice is the trigger's job)


async def test_a_summon_bypasses_the_cooldown(tmp_path):
    gh = RoutedGH(pr_facts=facts(), reviews=[])
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    # An immediate second webhook is eaten by the cooldown...
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "drop:cooldown"
    # ...but a human who typed a command is not a webhook burst.
    assert (await d.handle_summon("o/r", 1, "an-admin")) == "reviewed:FAIL"


async def test_a_summon_still_respects_the_allowlist(tmp_path):
    d = make(tmp_path, gh=RoutedGH(pr_facts=facts()))
    assert (await d.handle_summon("evil/repo", 1, "an-admin")) == "drop:unlisted-repo"


async def test_every_guard_records_its_decision_even_when_it_declines(tmp_path):
    # The gap this closes: "grounding checked 6 findings and downgraded 0" and
    # "grounding never ran" were indistinguishable in telemetry, so verifying a guard
    # meant hand-fetching blobs. Absence of an event is not evidence.
    gh = GroundingGH(source="writable = Path(str(configured))\n", pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner(FABRICATED_REPORT)
    d = make(tmp_path, cfg={"shadow_mode": False}, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    reviewed = [e for e in Telemetry(tmp_path).read_all() if e.get("event") == "reviewed"]
    assert reviewed
    e = reviewed[-1]
    assert e["grounding_checked"] == 1  # it RAN
    assert e["grounding_downgraded"] == 0  # and declined to downgrade
    assert e["converge_reason"]  # a reason on every review, firing or not
    assert e["unaccounted"] == 0 and e["held"] is False


# ── pause suppresses automated review only (issue #28 slice 2) ───────────────


class PausedGH(RoutedGH):
    def __init__(self, *, comments, **kw):
        super().__init__(**kw)
        self.comments = comments

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "/issues/" in joined and "/comments" in joined and "-X" not in args:
            return 0, json.dumps(self.comments), ""
        return await super().__call__(args, timeout=timeout)


async def test_a_paused_pr_is_not_reviewed_on_push(tmp_path):
    from pr_reviewer.summon import pause_text

    gh = PausedGH(comments=[pause_text("an-admin")], pr_facts=facts(), reviews=[])
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "drop:paused-by-operator"
    assert ran == []


async def test_an_explicit_summon_still_runs_on_a_paused_pr(tmp_path):
    # "stop reviewing every push" and "never look at this again" are different requests.
    from pr_reviewer.summon import pause_text

    gh = PausedGH(comments=[pause_text("an-admin")], pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner()
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_summon("o/r", 1, "an-admin")) == "reviewed:FAIL"


async def test_resume_restores_automated_review(tmp_path):
    from pr_reviewer.summon import pause_text, resume_text

    gh = PausedGH(comments=[pause_text("a"), resume_text("a")], pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner()
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


async def test_summon_disabled_skips_the_pause_check_entirely(tmp_path):
    from pr_reviewer.summon import pause_text

    gh = PausedGH(comments=[pause_text("a")], pr_facts=facts(), reviews=[])
    runner, _seen = capturing_runner()
    d = make(tmp_path, cfg={"summon": False}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"


# ── panel-exhaustion PR comment (issue #54) ────────────────────────────────────


async def test_exhaustion_comment_posted_on_pr_with_head_sha_and_marker(tmp_path):
    """When the panel exhausts, a visible comment is posted naming the head and the marker."""
    gh = RoutedGH(pr_facts=facts())
    escalations = []

    async def runner(name, inputs):
        return {"output": "partial", "failed": ["find_crossfile"]}

    d = make(tmp_path, gh=gh, runner=runner, inbox=lambda text, **kw: escalations.append((text, kw)))
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "error:panel-exhausted"
    # An exhaustion comment (no `event` key — this is not a review verdict) is posted.
    comments = [p for p in gh.posted if "body" in p and "event" not in p]
    assert len(comments) == 1
    body = comments[0]["body"]
    assert HEAD[:12] in body
    assert "QA panel exhausted" in body
    assert f"<!-- protoagent-qa-exhausted head={HEAD} -->" in body
    # Inbox and telemetry still fire.
    assert escalations and "UNREVIEWED" in escalations[0][0]


async def test_exhaustion_comment_dedup_skips_if_same_head_already_commented(tmp_path):
    """If the exhaustion marker for the current head already exists, no duplicate is posted."""
    marker = f"<!-- protoagent-qa-exhausted head={HEAD} -->"
    existing = f"⚠️ **QA panel exhausted** — this PR has not been reviewed.\n{marker}"
    gh = PausedGH(comments=[existing], pr_facts=facts(), reviews=[])

    async def runner(name, inputs):
        return {"output": "partial", "failed": ["find_crossfile"]}

    d = make(tmp_path, gh=gh, runner=runner)
    await d.handle_pr_event("o/r", 1, HEAD, "opened")
    # No new comment POST — the existing marker was found.
    comments_posted = [p for p in gh.posted if "body" in p and "event" not in p]
    assert comments_posted == []


class FailingCommentGH(RoutedGH):
    """Issues-comment POST always fails; reviews and other POSTs succeed normally."""

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "-X" in args and "POST" in args and "/issues/" in joined and "/comments" in joined:
            self.calls.append(args)
            return 1, "", "gh: 403 Forbidden"
        return await super().__call__(args, timeout)


async def test_exhaustion_comment_failure_does_not_prevent_escalation(tmp_path):
    """If the comment POST fails, telemetry and inbox still fire (fail-open)."""
    gh = FailingCommentGH(pr_facts=facts())
    escalations = []

    async def runner(name, inputs):
        return {"output": "partial", "failed": ["find_crossfile"]}

    d = make(tmp_path, gh=gh, runner=runner, inbox=lambda text, **kw: escalations.append((text, kw)))
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "error:panel-exhausted"
    assert escalations and "UNREVIEWED" in escalations[0][0]  # inbox still fires
    events = {e["event"]: e for e in d.telemetry.read_all()}
    assert "escalation" in events  # telemetry still fires


# ── max-rounds cap: suppress push-triggered reviews after the limit (issue #60) ─


def two_rounds():
    """Two completed panel rounds in history (round_number would be 3 for the next)."""
    return [review_row(OLD_HEAD, "WARN"), review_row(MID_HEAD, "WARN")]


async def test_push_capped_after_max_rounds_posts_comment_and_drops(tmp_path):
    """When push round_number > max_rounds, the panel is not spent and a comment is posted."""
    gh = RoutedGH(pr_facts=facts(), reviews=two_rounds())
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 2}, gh=gh, runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "drop:max-rounds-capped"
    assert ran == []  # panel was not spent
    # A comment explaining the cap was posted (not a review verdict — no `event` field).
    comments = [p for p in gh.posted if "body" in p and "event" not in p]
    assert len(comments) == 1
    assert "Review cap reached" in comments[0]["body"]
    assert "<!-- protoagent-qa-max-rounds" in comments[0]["body"]


async def test_incomplete_rounds_do_not_spend_the_cap(tmp_path):
    """A round that lost a lane is the panel's failure, not a push the author spent (#130):
    mythxengine#830's fix push was capped because flaky lanes had eaten its budget."""
    reviews = [review_row(OLD_HEAD, "WARN", complete=False), review_row(MID_HEAD, "WARN", complete=False)]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews)
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 2}, gh=gh, runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out != "drop:max-rounds-capped"
    assert ran  # the panel ran: two incomplete rounds did not use up a cap of two


async def test_incomplete_rounds_still_hit_a_hard_ceiling(tmp_path):
    """The cap is a flood guard, and a flood of pushes whose panels keep failing is still a flood."""
    heads = [f"{i:x}" * 40 for i in range(1, 5)]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(h, "WARN", complete=False) for h in heads])
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 2}, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "drop:max-rounds-capped"
    assert ran == []


async def test_subsequent_push_drops_early_without_comment(tmp_path):
    """After the cap is set, a second push drops in handle_pr_event before the panel or comment."""
    gh = RoutedGH(pr_facts=facts(), reviews=two_rounds())
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 2}, gh=gh, runner=runner)
    # First push sets the cap (posts one comment).
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "drop:max-rounds-capped"
    posts_after_first = len(gh.posted)
    # Second push (new head) is dropped early — no new comment, no panel.
    new_head = "d" * 40
    out = await d.handle_pr_event("o/r", 1, new_head, "synchronize")
    assert out == "drop:max-rounds-capped"
    assert ran == []
    assert len(gh.posted) == posts_after_first  # no additional posts


async def test_ready_for_review_resets_the_max_rounds_cap(tmp_path):
    """ready_for_review event clears the cap so the next push can proceed."""
    gh = RoutedGH(pr_facts=facts(), reviews=two_rounds())
    d = make(tmp_path, cfg={"max_rounds": 2}, gh=gh)
    # Hit the cap.
    await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert "o/r#1" in d._round_cap
    # ready_for_review resets it (even when the action itself may be outside DISPATCH_ACTIONS).
    await d.handle_pr_event("o/r", 1, HEAD, "ready_for_review")
    assert "o/r#1" not in d._round_cap


async def test_summon_resets_cap_and_runs_review(tmp_path):
    """A manual summon clears the cap and runs the panel regardless of round count."""
    gh = RoutedGH(pr_facts=facts(), reviews=two_rounds())
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 2}, gh=gh, runner=runner)
    # Hit the cap.
    assert (await d.handle_pr_event("o/r", 1, HEAD, "synchronize")) == "drop:max-rounds-capped"
    assert "o/r#1" in d._round_cap
    # Summon overrides the cap: resets it and runs the panel.
    out = await d.handle_summon("o/r", 1, "an-admin")
    assert out == "reviewed:FAIL"
    assert ran  # panel actually ran
    assert "o/r#1" not in d._round_cap  # cap was cleared by the summon


async def test_cap_resets_after_cooldown_elapses(tmp_path):
    """After the cooldown period, the next push is accepted and treated as a new round."""
    gh = RoutedGH(pr_facts=facts(), reviews=two_rounds())
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 2, "max_rounds_cooldown": 3600}, gh=gh, runner=runner)
    # Hit the cap.
    await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    # Backdate the cap timestamp past the cooldown.
    d._round_cap["o/r#1"] -= 3601
    # The next push should be accepted (cooldown elapsed).
    new_head = "d" * 40
    out = await d.handle_pr_event("o/r", 1, new_head, "synchronize")
    # round_number = 4 > max_rounds 2, so the cap fires again immediately — but this
    # proves the EARLY check in handle_pr_event let us through (cooldown path).
    assert out == "drop:max-rounds-capped"
    assert "o/r#1" in d._round_cap  # re-capped on this fresh review attempt


async def test_backfill_is_not_subject_to_max_rounds_cap(tmp_path):
    """Backfill reviews bypass the cap — they are not push-triggered."""
    gh = RoutedGH(pr_facts=facts(), reviews=two_rounds())
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 2}, gh=gh, runner=runner)
    # Arm the cap manually.
    d._round_cap["o/r#1"] = time.monotonic()
    # Backfill goes through _review without push_triggered=True — cap never fires.
    out = await d.backfill_review("o/r", 1, HEAD)
    assert out.startswith("reviewed:")
    assert ran  # panel ran


async def test_summon_is_not_subject_to_max_rounds_cap(tmp_path):
    """Summon calls bypass the cap even when round count exceeds max_rounds."""
    gh = RoutedGH(pr_facts=facts(), reviews=two_rounds())
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 2}, gh=gh, runner=runner)
    # No cap in _round_cap — summon should just work despite history > max_rounds.
    out = await d.handle_summon("o/r", 1, "an-admin")
    assert out == "reviewed:FAIL"
    assert ran
    # Summon itself does not set the cap.
    assert "o/r#1" not in d._round_cap


async def test_max_rounds_zero_disables_the_cap(tmp_path):
    """max_rounds=0 disables the cap entirely — every push is reviewed."""
    gh = RoutedGH(pr_facts=facts(), reviews=two_rounds())
    ran = []

    async def runner(name, inputs):
        ran.append(name)
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"max_rounds": 0}, gh=gh, runner=runner)
    out = await d.handle_pr_event("o/r", 1, HEAD, "synchronize")
    assert out == "reviewed:FAIL"
    assert ran  # panel ran — cap is disabled


def test_max_rounds_env_fallback_and_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PR_REVIEWER_MAX_ROUNDS", raising=False)
    monkeypatch.delenv("PR_REVIEWER_MAX_ROUNDS_COOLDOWN", raising=False)
    d = Dispatcher({}, Telemetry(tmp_path))
    assert d.max_rounds == 6
    assert d.max_rounds_cooldown_s == 7200

    monkeypatch.setenv("PR_REVIEWER_MAX_ROUNDS", "3")
    monkeypatch.setenv("PR_REVIEWER_MAX_ROUNDS_COOLDOWN", "1800")
    d2 = Dispatcher({}, Telemetry(tmp_path))
    assert d2.max_rounds == 3
    assert d2.max_rounds_cooldown_s == 1800

    d3 = Dispatcher({"max_rounds": 4, "max_rounds_cooldown": 900}, Telemetry(tmp_path))
    assert d3.max_rounds == 4
    assert d3.max_rounds_cooldown_s == 900


# ── stale-head demotion: the PR advanced mid-round (issue #82) ────────────────
#
# protoAgent#2854 r2 / #2868 r2: a fix commit pushed while the finders were running,
# and the round posted `confirmed` findings verified against the superseded head —
# once on an already-merged PR. _post_verdict re-resolves the head at post time and
# demotes the findings the pinned→current delta touches.

PUSHED_HEAD = "e" * 40

STALE_ROUND_REPORT = (
    "<!-- brief -->\nBrief prose.\n<!-- /brief -->\n\n```json\n"
    + json.dumps(
        [
            {
                "file": "x.py",
                "line": 3,
                "severity": "major",
                "category": "correctness",
                "claim": "Bug the push may have fixed.",
                "evidence": "e",
                "verdict": "confirmed",
            },
            {
                "file": "z.py",
                "line": 5,
                "severity": "major",
                "category": "correctness",
                "claim": "Bug the push never touched.",
                "evidence": "e",
                "verdict": "confirmed",
            },
        ]
    )
    + "\n```"
)


class MidRoundPushGH(RoutedGH):
    """The head advances while the panel runs: the FIRST /pulls/1 read (the round's pin)
    serves the old head; every later one — including _post_verdict's re-resolution —
    serves the pushed head, or fails outright for the degradation path. The
    pinned…pushed compare is served from `stale_compare` (an OBJECT: {commits, files}),
    distinct from RoutedGH.compare (the prior-round convergence compare, a files list)."""

    def __init__(self, *, pushed_head, stale_compare=None, head_lookup_rc=0, **kw):
        super().__init__(**kw)
        self.pushed_head = pushed_head
        self.stale_compare = stale_compare  # None → the pinned…pushed compare is unreadable
        self.head_lookup_rc = head_lookup_rc  # non-zero → post-time facts reads fail
        self.facts_reads = 0

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if f"/compare/{HEAD}...{self.pushed_head}" in joined:
            self.calls.append(args)
            if self.stale_compare is None:
                return 1, "", "HTTP 404"
            return 0, json.dumps(self.stale_compare), ""
        if len(args) > 1 and args[1] == "repos/o/r/pulls/1":
            self.facts_reads += 1
            if self.facts_reads > 1:
                self.calls.append(args)
                if self.head_lookup_rc:
                    return self.head_lookup_rc, "", "HTTP 502"
                return 0, json.dumps({**self.pr_facts, "head": self.pushed_head}), ""
        return await super().__call__(args, timeout)


async def test_a_mid_round_push_demotes_only_the_findings_it_touched(tmp_path):
    # The push rewrote x.py around line 3; z.py:5 is untouched and keeps `confirmed`.
    compare = {"commits": 2, "files": [{"filename": "x.py", "patch": "@@ -1,3 +1,5 @@\n a\n+b\n+c\n d\n"}]}
    gh = MidRoundPushGH(
        pushed_head=PUSHED_HEAD, stale_compare=compare, pr_facts=facts(), reviews=[], files="x.py\nz.py\n"
    )
    runner, _seen = capturing_runner(STALE_ROUND_REPORT)
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    body = gh.reviews_posted[0]["body"]
    # The synthesis header names the move and the demotion count…
    assert f"PR advanced 2 commit(s) during this round (`{HEAD[:12]}` → `{PUSHED_HEAD[:12]}`)" in body
    assert "1 finding(s) in the delta were demoted to *possibly addressed*" in body
    # …while the marker still records the head the round RAN AGAINST — the promotion
    # gate's stale-head hold depends on it naming the reviewed head, not the current one.
    assert f"head={HEAD} verdict=FAIL" in body
    recorded = {f["file"]: f for f in json.loads(extract_findings_json(body))}
    assert recorded["x.py"]["verdict"] == "possibly addressed"
    assert recorded["z.py"]["verdict"] == "confirmed"
    events = [e for e in d.telemetry.read_all() if e.get("event") == "stale_head"]
    assert events and events[0]["demoted"] == 1 and events[0]["current"] == PUSHED_HEAD


async def test_an_unresolvable_current_head_posts_as_is_with_a_note(tmp_path):
    # Blind on the current head, the verdict still posts — findings untouched, plus a
    # note that the staleness check could not run. Degrade, never raise: a lost verdict
    # is worse than a stale-marked one.
    gh = MidRoundPushGH(pushed_head=PUSHED_HEAD, head_lookup_rc=1, pr_facts=facts(), reviews=[], files="x.py\nz.py\n")
    runner, _seen = capturing_runner(STALE_ROUND_REPORT)
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    body = gh.reviews_posted[0]["body"]
    assert "could not be resolved at post time" in body
    assert all(f["verdict"] == "confirmed" for f in json.loads(extract_findings_json(body)))
    assert not any("/compare/" in " ".join(c) for c in gh.calls)  # nothing to compare against


async def test_an_unreadable_stale_delta_notes_the_move_but_demotes_nothing(tmp_path):
    # The head moved but the compare is unreadable: demotion fails CLOSED (no relief
    # without proof the region was touched — the converge posture) while the move
    # itself is still stated for the reader.
    gh = MidRoundPushGH(pushed_head=PUSHED_HEAD, stale_compare=None, pr_facts=facts(), reviews=[], files="x.py\nz.py\n")
    runner, _seen = capturing_runner(STALE_ROUND_REPORT)
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    body = gh.reviews_posted[0]["body"]
    assert "the delta could not be read" in body
    assert all(f["verdict"] == "confirmed" for f in json.loads(extract_findings_json(body)))


async def test_an_unmoved_head_posts_the_unchanged_body_with_no_stale_machinery(tmp_path):
    # The common case pays nothing: no compare read, no header, no telemetry event —
    # the posting path is byte-identical to before the guard existed.
    gh = RoutedGH(pr_facts=facts(), reviews=[], files="x.py\nz.py\n")
    runner, _seen = capturing_runner(STALE_ROUND_REPORT)
    d = make(tmp_path, gh=gh, runner=runner)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "reviewed:FAIL"
    body = gh.reviews_posted[0]["body"]
    assert "PR advanced" not in body and "possibly addressed" not in body
    assert "could not be resolved at post time" not in body
    assert not any("/compare/" in " ".join(c) for c in gh.calls)
    assert not [e for e in d.telemetry.read_all() if e.get("event") == "stale_head"]


# ── a round can't hold the PR's slot forever (#3431) ─────────────────────────────
#
# Only the panel's finders carry a step timeout. A round that hung anywhere else held
# the PR's chokepoint slot until the process restarted, and the push webhook, the
# backfill sweep and the operator's `@vera review` all pass that gate — so on #3431 a
# summon answered "in-flight: nothing ran" for over an hour.


class HangOnceGH(RoutedGH):
    """The first `gh` call never returns — a stuck call OUTSIDE the panel runner."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.hung = False

    async def __call__(self, args, timeout=30):
        if not self.hung:
            self.hung = True
            await asyncio.Event().wait()
        return await super().__call__(args, timeout=timeout)


async def test_a_hung_round_gives_the_prs_slot_back_so_a_summon_gets_through(tmp_path):
    escalations: list[str] = []
    d = make(
        tmp_path,
        cfg={"round_timeout": 0.2},
        gh=HangOnceGH(pr_facts=facts()),
        inbox=lambda text, **_kw: escalations.append(text),
    )
    push = asyncio.create_task(d.handle_pr_event("o/r", 1, HEAD, "opened"))
    await asyncio.sleep(0.05)
    # While the round is genuinely live, refusing a second panel is correct.
    assert (await d.handle_summon("o/r", 1, "operator")) == "drop:in-flight"

    finished, _ = await asyncio.wait({push}, timeout=3)
    assert finished, "the hung round never ended — the PR's slot stays held until a restart"
    assert push.result() == "drop:round-timeout"
    # …and the operator hears about it — a hung reviewer must not be silent.
    assert any("ran past 0.2s" in e and "UNREVIEWED" in e for e in escalations), escalations
    assert (await d.handle_summon("o/r", 1, "operator")).startswith("reviewed:")


async def test_a_hung_panel_attempt_is_cut_off_and_retried(tmp_path):
    attempts = []

    async def runner(name, inputs):
        attempts.append(name)
        if len(attempts) == 1:
            await asyncio.Event().wait()  # the unbounded step that never answers
        return {"output": REPORT, "failed": []}

    d = make(tmp_path, cfg={"panel_attempt_timeout": 0.1}, gh=RoutedGH(pr_facts=facts()), runner=runner)
    assert (await asyncio.wait_for(d.handle_pr_event("o/r", 1, HEAD, "opened"), 5)) == "reviewed:FAIL"
    assert len(attempts) == 2


async def test_a_panel_that_hangs_every_attempt_says_it_timed_out_not_crashed(tmp_path):
    escalations: list[str] = []

    async def runner(name, inputs):
        await asyncio.Event().wait()

    gh = RoutedGH(pr_facts=facts())
    d = make(
        tmp_path,
        cfg={"panel_attempt_timeout": 0.05, "panel_retries": 1},
        gh=gh,
        runner=runner,
        inbox=lambda text, **_kw: escalations.append(text),
    )
    assert (await asyncio.wait_for(d.handle_pr_event("o/r", 1, HEAD, "opened"), 5)) == "error:run-timed-out"
    titles = [w.get("output[title]", "") for w in gh.check_writes]
    assert any("timed out" in t for t in titles), titles
    assert escalations and "timed out after 0.05s" in escalations[-1]


async def test_a_timeout_from_inside_the_round_is_not_mistaken_for_the_rounds_own(tmp_path):
    # Only the round's OWN bound expiring is a round timeout; anything else keeps its
    # original meaning instead of being relabelled and swallowed.
    d = make(tmp_path, cfg={"round_timeout": 60})

    async def _review(*_a, **_k):
        raise TimeoutError("some inner call gave up")

    d._review = _review
    try:
        await d.handle_pr_event("o/r", 1, HEAD, "opened")
    except TimeoutError as exc:
        assert "inner call" in str(exc)
    else:
        raise AssertionError("an inner TimeoutError was reported as the round's own timeout")


def test_the_slot_ttl_outlasts_every_legitimate_round(tmp_path):
    # The chokepoint only reclaims a slot whose round can't end: its TTL sits above the
    # round bound, which sits above every attempt's budget including the retry.
    d = make(tmp_path, cfg={"panel_attempt_timeout": 1800, "panel_retries": 1})
    assert d.round_timeout_s >= (d.panel_retries + 1) * d.panel_attempt_timeout_s
    assert d.chokepoint.in_flight_ttl_s > d.round_timeout_s


# ── the verdict never reads FEWER findings than the report carries ─────────────


def _host_parser(monkeypatch, result):
    """Install a stand-in `graph.review.findings.parse_findings` (the host's reader)."""
    import sys
    import types

    class _F:
        def __init__(self, d):
            self._d = d

        def to_dict(self):
            return dict(self._d)

    mod = types.ModuleType("graph.review.findings")
    mod.parse_findings = lambda _text: [_F(d) for d in result]
    review = types.ModuleType("graph.review")
    review.findings = mod
    monkeypatch.setitem(sys.modules, "graph.review", review)
    monkeypatch.setitem(sys.modules, "graph.review.findings", mod)


_STACKED_CLAIM = "reads `covered by ``tests/test_review_at_head.py```; the sweep skips the rest"
_STACKED_REPORT = (
    '```json\n[{"prior": "scripts/x.py:220", "disposition": "open", "why": "unchanged"}]\n```\n\n'
    "```json\n"
    + json.dumps([{"file": "scripts/x.py", "line": 12, "severity": "major", "claim": _STACKED_CLAIM}], indent=2)
    + "\n```"
)


def test_a_host_parser_that_drops_the_findings_block_does_not_produce_a_clean_pass(monkeypatch, caplog):
    # The host's fence pattern ends a block at a ``` INSIDE a JSON string; with a dispositions
    # block ahead of it, its fallback never runs and it returns ZERO findings. After #162 the
    # plugin no longer discards such a report — so this reader is what stands between it and
    # a PASS. Reproduced against the real host parser: parse_findings -> 0.
    from pr_reviewer.dispatch import Dispatcher

    _host_parser(monkeypatch, [])  # what the host returns for _STACKED_REPORT today
    with caplog.at_level("WARNING"):
        findings = Dispatcher._parse_findings(_STACKED_REPORT)
    assert [f["claim"] for f in findings] == [_STACKED_CLAIM] and findings[0]["severity"] == "major"
    assert "host parser read 0 finding(s) where the report carries 1" in caplog.text
    assert verdict_for(findings) != "PASS"  # an unverified major does not clear


def test_the_host_parser_stays_the_reader_when_it_sees_at_least_as_much(monkeypatch):
    from pr_reviewer.dispatch import Dispatcher

    coerced = [
        {
            "file": "scripts/x.py",
            "line": 12,
            "severity": "major",
            "claim": "coerced by the host",
            "verdict": "confirmed",
        }
    ]
    _host_parser(monkeypatch, coerced)
    assert Dispatcher._parse_findings(_STACKED_REPORT) == coerced  # same count: the host's coercion wins
    _host_parser(monkeypatch, [])
    assert Dispatcher._parse_findings("clean.\n\n```json\n[]\n```") == []  # a clean report stays clean

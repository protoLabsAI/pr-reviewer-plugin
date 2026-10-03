"""Shared dispatcher test fakes: a canned `gh`, PR facts, a dispatcher factory and the
panel-runner stubs. Imported by tests/test_dispatch.py and the issue-specific dispatcher
suites, which used to reach into test_dispatch.py for them (#251)."""

from __future__ import annotations

import json

from pr_reviewer.dispatch import Dispatcher
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.verdicts import render_verdict_body

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


def report_with_dispositions(dispositions, findings="[]"):
    return "prose\n\n```json\n" + json.dumps(dispositions) + "\n```\n\nbrief\n\n```json\n" + findings + "\n```"


_CLEAN_STEPS = {
    "synthesize": "<!-- brief -->\nNothing raised.\n<!-- /brief -->\n\n```json\n[]\n```",
    "verify": "VERIFY_STATUS: nothing-to-verify\n\n```json\n[]\n```",
}


def verify_reply(*rows):
    return f"VERIFY_STATUS: annotated n={len(rows)}\n\n```json\n" + json.dumps(list(rows)) + "\n```"


def recheck_runner(report, reply=None, steps=None):
    """A seed-capable host (protoAgent#3571): the first call is the panel; a seeded call is the
    targeted re-check, answered with `reply` as the verify step's output."""
    calls: list[dict | None] = []

    async def runner(name, inputs, *, seed_outputs=None):
        calls.append(seed_outputs)
        if seed_outputs is None:
            return {"output": report, "steps": {**(steps or _CLEAN_STEPS), "report": report}, "failed": []}
        return {"output": seed_outputs["report"], "steps": {**seed_outputs, "verify": reply or ""}, "failed": []}

    return runner, calls

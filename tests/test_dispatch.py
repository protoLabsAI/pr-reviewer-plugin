"""The dispatcher — gate order, recall, exhaustion, posting, promotion. All GitHub
reads/writes go through a canned fake `gh`; the workflow runner is a stub."""

from __future__ import annotations

import json

from pr_reviewer.dispatch import Dispatcher
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.verdicts import render_verdict_body

HEAD = "a" * 40
OLD_HEAD = "b" * 40

REPORT = (
    "Brief prose.\n\n```json\n"
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

    async def __call__(self, args, timeout=30):
        self.calls.append(args)
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


async def test_self_authored_pr_drops(tmp_path):
    # RoutedGH's viewer login is "qa-bot" — a PR authored by qa-bot[bot] is ours.
    gh = RoutedGH(pr_facts=facts(author="qa-bot[bot]"))
    d = make(tmp_path, gh=gh)
    assert (await d.handle_pr_event("o/r", 1, HEAD, "opened")) == "drop:self-authored"
    assert gh.posted == []


# ── recall: reaffirm + delta ──────────────────────────────────────────────────


def review_row(head, verdict, state="COMMENTED", findings_json=""):
    body = render_verdict_body(
        repo="o/r",
        pr=1,
        head_sha=head,
        verdict=verdict,
        report=f"prose\n```json\n{findings_json or '[]'}\n```",
        shadow=True,
        recipe="code-review",
    )
    return {"state": state, "body": body}


class RoutedGH(FakeGH):
    def __init__(self, *, pr_facts, reviews=None, checks=None, files="x.py\n"):
        super().__init__()
        self.pr_facts, self.reviews, self.checks, self.files = pr_facts, reviews or [], checks, files

    async def __call__(self, args, timeout=30):
        self.calls.append(args)
        joined = " ".join(args)
        if "-X" in args and "POST" in args:
            fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("query=")}
            self.posted.append(fields)
            return 0, "{}", ""
        if args[1] == "user":
            return 0, "qa-bot", ""
        if "/files" in joined:
            return 0, self.files, ""
        if "/reviews" in joined:
            return 0, json.dumps(self.reviews), ""
        if "/check-runs" in joined:
            return (0, json.dumps(self.checks), "") if self.checks is not None else (1, "", "403")
        if "graphql" in joined:
            return 0, "0", ""
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


# ── exhaustion (D3) ───────────────────────────────────────────────────────────


async def test_failed_panel_step_escalates_and_posts_nothing(tmp_path):
    gh = RoutedGH(pr_facts=facts())
    escalations = []

    async def runner(name, inputs):
        return {"output": "partial", "failed": ["find_crossfile"]}

    d = make(tmp_path, gh=gh, runner=runner, inbox=lambda text, **kw: escalations.append((text, kw)))
    out = await d.handle_pr_event("o/r", 1, HEAD, "opened")
    assert out == "error:panel-exhausted"
    assert gh.posted == []
    assert escalations and "UNREVIEWED" in escalations[0][0]


# ── posting + trigger ─────────────────────────────────────────────────────────


async def test_shadow_mode_posts_comment_and_structural_trigger_picks_recipe(tmp_path):
    gh = RoutedGH(pr_facts=facts(changed_files=6, additions=300, deletions=50), files="a\nb\nc\nd\ne\nf\n")
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


# ── promotion ─────────────────────────────────────────────────────────────────


async def test_promotion_green_path_approves_when_owned(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "promote"
    assert gh.posted[0]["event"] == "APPROVE" and "promoted=true" in gh.posted[0]["body"]


async def test_promotion_holds_in_shadow_or_without_ownership(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    gh = RoutedGH(pr_facts=facts(), reviews=[review_row(HEAD, "PASS")], checks=green)
    d = make(tmp_path, gh=gh)  # shadow default, not owner
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:not-promotion-owner"
    assert gh.posted == []


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

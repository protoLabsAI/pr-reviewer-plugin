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


def review_row(head, verdict, state="COMMENTED", findings_json="", id=None):
    body = render_verdict_body(
        repo="o/r",
        pr=1,
        head_sha=head,
        verdict=verdict,
        report=f"prose\n```json\n{findings_json or '[]'}\n```",
        shadow=True,
        recipe="code-review",
    )
    return {"state": state, "body": body, "id": id}


class RoutedGH(FakeGH):
    def __init__(self, *, pr_facts, reviews=None, checks=None, files="x.py\n", threads=None, compare=None):
        super().__init__()
        self.pr_facts, self.reviews, self.checks, self.files = pr_facts, reviews or [], checks, files
        self.threads = threads  # None → the generic graphql "0" (fetch degrades to no block)
        self.compare = compare  # None → the compare read fails (no convergence relief)
        self.dismissed: list[str] = []

    async def __call__(self, args, timeout=30):
        self.calls.append(args)
        joined = " ".join(args)
        if "-X" in args and "PUT" in args and "/dismissals" in joined:
            self.dismissed.append(args[1])
            return 0, "{}", ""
        if "-X" in args and "POST" in args:
            fields = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("query=")}
            self.posted.append(fields)
            return 0, "{}", ""
        if args[1] == "user":
            return 0, "qa-bot", ""
        if "/compare/" in joined:
            return (0, json.dumps(self.compare), "") if self.compare is not None else (1, "", "404")
        if "/files" in joined:
            return 0, self.files, ""
        if "/reviews" in joined:
            return 0, json.dumps(self.reviews), ""
        if "/check-runs" in joined:
            return (0, json.dumps(self.checks), "") if self.checks is not None else (1, "", "403")
        if "comments(first" in joined:  # the threads fetch (before the count query below)
            return (0, json.dumps(self.threads), "") if self.threads is not None else (0, "null", "")
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
    assert gh.posted == []
    assert escalations and "UNREVIEWED" in escalations[0][0]


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
    assert "WARN verdict" in gh.posted[0]["body"]


async def test_latest_fail_holds_even_after_an_earlier_pass(tmp_path):
    green = [{"status": "completed", "conclusion": "success"}]
    reviews = [review_row(OLD_HEAD, "PASS"), review_row(HEAD, "FAIL")]
    gh = RoutedGH(pr_facts=facts(), reviews=reviews, checks=green)
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.evaluate_promotion("o/r", 1)) == "hold:no-clear-verdict"


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
    assert gh.posted == []  # D3 holds: no verdict from a partial panel
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
    assert ran == ["1"]  # the sweep created the first review itself
    assert gh.posted and "verdict=FAIL" in gh.posted[0]["body"]


async def test_backfill_still_honours_the_self_authored_rail(tmp_path):
    gh = RoutedGH(pr_facts=facts(author="qa-bot[bot]"), reviews=[])
    d = make(tmp_path, cfg={"shadow_mode": False, "promotion_owner": True}, gh=gh)
    assert (await d.backfill_review("o/r", 1, HEAD)) == "drop:self-authored"
    assert gh.posted == []


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
    assert len(ran) == 2  # but only the budgeted number of panels spent


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
    assert gh.posted[0]["event"] == "APPROVE"


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
    body = gh.posted[0]["body"]
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

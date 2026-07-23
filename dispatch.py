"""The review dispatcher — webhook/sweep to posted verdict, deterministically (ADR 0078 C).

The model reviews; everything around the review is code:

  gate (allowlist, BEFORE any GitHub call) → chokepoint (typed drops) → facts
  (PR JSON + full file list + prior reviews, all server-side) → structural
  trigger → recipe run (STATE.workflow_run) → fail-closed exhaustion check →
  pure verdict mapping → post (shadow: always COMMENT) → telemetry.

Identity: the dispatcher never reviews the token's own PRs (self-approval loops).
Prior-review recall reads our marker line out of the PR's posted reviews (GitHub is
the store, ADR 0078 D5); the recalled findings JSON becomes the recipe's
`prior_findings` input (delta re-review).

Fail-closed exhaustion (D3): a run with ANY failed panel step posts nothing and
escalates to the operator inbox — a partial panel must never produce a verdict.

Blocking verdicts (formal mode) go out only against terminal CI — the same #863
policy the github-plugin tools enforce against the MODEL, enforced here in code for
the deterministic path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from .approve import PROMOTE, Observations, promotion_decision
from .chokepoint import DISPATCH_ACTIONS, Chokepoint
from .gh_cli import bad_repo, run_gh
from .grounding import apply_grounding, render_grounding_footnote
from .rounds import (
    DEFAULT_CONVERGENCE_ROUNDS,
    converge,
    delta_ranges,
    panel_rounds,
    parse_dispositions,
    render_held_note,
    render_notes_section,
    render_prior_requests,
    render_promotion_findings,
    render_unaccounted_note,
    unaccounted_priors,
    unexplained_clearance,
)
from .telemetry import Telemetry
from .trigger import structural_trigger
from .verdicts import (
    FAIL,
    PASS,
    WARN,
    confine_findings,
    parse_verdict_marker,
    render_verdict_body,
    verdict_for,
)

log = logging.getLogger("protoagent.plugins.pr_reviewer")

DROP_SELF_AUTHORED = "self-authored"
DROP_PR_NOT_ELIGIBLE = "pr-not-eligible"  # closed, draft, or facts unreadable
DROP_NO_RUNNER = "no-workflow-runner"

HOLD_PROMOTE_BACKOFF = "hold:promote-backoff"
# Consecutive APPROVE failures on one repo#pr@head before the sweep stops retrying
# (issue #6): a promotion GitHub keeps refusing (422, persistent 5xx) otherwise
# re-attempts every tick forever. A NEW head is a new key, so a real fix always
# re-enters; in-memory, so a restart retries once more — fail-open by one attempt,
# same posture as the chokepoint.
PROMOTE_MAX_FAILURES = 3

# Re-gate (issue #16): _post_verdict decides COMMENT-vs-REQUEST_CHANGES once, at
# review time, and a FAIL that landed while CI was still pending stays non-blocking
# for that head forever — the sweep only ever promoted, it never re-gated. These are
# the typed outcomes of the sweep's second look. Same backoff posture as promotion.
REGATE = "regate"
HOLD_REGATE_SHADOW = "hold:regate-shadow"
HOLD_REGATE_DISABLED = "hold:regate-disabled"
HOLD_REGATE_NO_FAIL = "hold:regate-no-current-fail"
HOLD_REGATE_ALREADY = "hold:regate-already-blocking"
HOLD_REGATE_CHECKS_UNKNOWN = "hold:regate-checks-unknown"
HOLD_REGATE_CHECKS_PENDING = "hold:regate-checks-pending"
HOLD_REGATE_BACKOFF = "hold:regate-backoff"
REGATE_MAX_FAILURES = 3

# Backfill (issue #17): a PR with no verdict for its current head is unreviewable by
# the promotion path forever — it holds `no-clear-verdict` on every tick. Dispatch
# actions only fire for LIVE events, so anything opened before the reviewer existed
# (or while it was down, or that exhausted its panel) never gets a first review.
BACKFILL_ACTION = "sweep-backfill"

_NON_TERMINAL = {"queued", "in_progress", "waiting", "requested", "pending"}
_GREEN = {"success", "neutral", "skipped"}


def _env_repos() -> list[str]:
    """Managed allowlist from PR_REVIEWER_REPOS — comma/space/newline separated."""
    return [r for r in re.split(r"[,\s]+", os.environ.get("PR_REVIEWER_REPOS", "").strip()) if r]


def _env_bool(name: str, default: bool) -> bool:
    """A tri-state env flag: unset → default; else truthy iff 1/true/yes/on."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    """An int env knob; unreadable/negative → default (never raises at boot)."""
    try:
        value = int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default
    return value if value >= 0 else default


class Dispatcher:
    def __init__(
        self,
        cfg: dict,
        telemetry: Telemetry,
        *,
        run_gh_fn=None,
        workflow_run=None,
        inbox_add=None,
        cfg_provider=None,
    ):
        # Config is resolved LIVE, never snapshotted (issue #11). Every knob below used
        # to be read once in __init__, so an operator editing `repos` or flipping
        # `shadow_mode` through Settings saw "config saved / reloaded" and got a silent
        # no-op until the container restarted. These read as ordinary settings — the
        # core schema cannot know a plugin cached them — so the operator believes a gate
        # flip took effect when it did not. That is the dangerous direction.
        self._cfg = cfg or {}
        self._cfg_provider = cfg_provider
        self.telemetry = telemetry
        # Boot-time by necessity: the chokepoint owns in-flight/cooldown state, so it
        # cannot be rebuilt per read without dropping the bookkeeping it exists for.
        self.chokepoint = Chokepoint(cooldown_s=int(self._cfg.get("cooldown_s") or 30))
        self._run_gh = run_gh_fn or run_gh
        self._workflow_run = workflow_run  # None → resolve STATE.workflow_run lazily
        self._inbox_add = inbox_add  # None → resolve STATE.inbox_store lazily
        self._viewer: str | None = None
        self._promote_failures: dict[str, int] = {}  # repo#pr@head -> consecutive APPROVE failures
        self._regate_failures: dict[str, int] = {}  # repo#pr@head -> consecutive REQUEST_CHANGES failures

    # ── config, resolved live ────────────────────────────────────────────────
    #
    # Config-first, ENV fallback — the same posture as webhook_secret. HEADLESS
    # config-as-code seeds the config volume ONCE, so state baked only there can't be
    # updated on an image roll; the compose env (re-applied every roll) carries it,
    # keeping the config volume disposable. A config key present wins over the env; for
    # the bools that means an explicit `shadow_mode: false` is honoured, not treated as
    # unset.

    @property
    def cfg(self) -> dict:
        """The CURRENT plugin config. `cfg_provider` is the host's live view (the same
        `registry.live_config` the webhook secret already uses); without one this falls
        back to the dict handed in at construction."""
        if self._cfg_provider is not None:
            try:
                return self._cfg_provider() or {}
            except Exception:  # noqa: BLE001 — a failing provider must never break a review
                log.exception("[pr-reviewer] live config read failed; using boot config")
        return self._cfg

    @property
    def repos(self) -> list[str]:
        """Managed allowlist. Config wins only when NON-empty — a seed shipping
        `repos: []` must fall through to the env."""
        return [str(r) for r in (self.cfg.get("repos") or []) if r] or _env_repos()

    @property
    def shadow(self) -> bool:
        cfg = self.cfg
        return bool(cfg["shadow_mode"]) if "shadow_mode" in cfg else _env_bool("PR_REVIEWER_SHADOW_MODE", True)

    @property
    def promotion_owner(self) -> bool:
        cfg = self.cfg
        return (
            bool(cfg["promotion_owner"])
            if "promotion_owner" in cfg
            else _env_bool("PR_REVIEWER_PROMOTION_OWNER", False)
        )

    @property
    def panel_retries(self) -> int:
        """D3 says an exhausted run is "retry or escalate" — we do both, in that order.
        A failed panel step is usually transient, and the alternative to retrying is a
        PR that merges UNREVIEWED."""
        cfg = self.cfg
        return int(cfg["panel_retries"]) if "panel_retries" in cfg else _env_int("PR_REVIEWER_PANEL_RETRIES", 1)

    @property
    def backfill_per_pass(self) -> int:
        """Reviews the sweep may backfill per pass, across all repos — bounds the
        first-pass stampede on a deployment adopting a repo with a PR backlog."""
        cfg = self.cfg
        return (
            int(cfg["backfill_per_pass"])
            if "backfill_per_pass" in cfg
            else _env_int("PR_REVIEWER_BACKFILL_PER_PASS", 2)
        )

    @property
    def regate_enabled(self) -> bool:
        """Independent kill switch for the re-gate. Arming a block is the one thing this
        machinery does that can WEDGE someone else's merge, so it needs an off switch
        that doesn't also cost you promotion and backfill — and one that takes effect
        WITHOUT a restart, which is the whole point of #11."""
        cfg = self.cfg
        return bool(cfg["regate"]) if "regate" in cfg else _env_bool("PR_REVIEWER_REGATE", True)

    @property
    def convergence_rounds(self) -> int:
        cfg = self.cfg
        return (
            int(cfg["convergence_rounds"])
            if "convergence_rounds" in cfg
            else _env_int("PR_REVIEWER_CONVERGENCE_ROUNDS", DEFAULT_CONVERGENCE_ROUNDS)
        )

    @property
    def hold_unexplained(self) -> bool:
        cfg = self.cfg
        return (
            bool(cfg["hold_unexplained_clearance"])
            if "hold_unexplained_clearance" in cfg
            else _env_bool("PR_REVIEWER_HOLD_UNEXPLAINED_CLEARANCE", True)
        )

    @property
    def grounding_enabled(self) -> bool:
        cfg = self.cfg
        return (
            bool(cfg["evidence_grounding"])
            if "evidence_grounding" in cfg
            else _env_bool("PR_REVIEWER_EVIDENCE_GROUNDING", True)
        )

    # ── plumbing ──────────────────────────────────────────────────────────────

    def _runner(self):
        if self._workflow_run is not None:
            return self._workflow_run
        try:
            from runtime.state import STATE

            return STATE.workflow_run
        except Exception:  # noqa: BLE001 — host-free
            return None

    def _escalate(self, text: str, dedup_key: str) -> None:
        """Operator escalation — inbox when the host offers one, always telemetry."""
        self.telemetry.emit("escalation", text=text, dedup_key=dedup_key)
        add = self._inbox_add
        if add is None:
            try:
                from runtime.state import STATE

                add = STATE.inbox_store.add if STATE.inbox_store else None
            except Exception:  # noqa: BLE001
                add = None
        if add:
            try:
                add(text, priority="next", source="pr-reviewer", dedup_key=dedup_key)
            except Exception:  # noqa: BLE001
                log.exception("[pr-reviewer] inbox escalation failed")

    async def _viewer_login(self) -> str:
        if self._viewer is None:
            rc, out, _err = await self._run_gh(["api", "user", "--jq", ".login"])
            self._viewer = out.strip().lower() if rc == 0 else ""
        return self._viewer

    # ── facts (all server-side) ───────────────────────────────────────────────

    async def _pr_facts(self, repo: str, pr: int) -> dict | None:
        rc, out, _err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/pulls/{pr}",
                "--jq",
                "{head: .head.sha, base_ref: .base.ref, state: .state, draft: .draft, "
                "changed_files: .changed_files, additions: .additions, deletions: .deletions, "
                "author: .user.login}",
            ],
        )
        if rc != 0:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return None

    async def _changed_paths(self, repo: str, pr: int) -> list[str]:
        rc, out, _err = await self._run_gh(
            ["api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--jq", ".[].filename"]
        )
        return [line.strip() for line in out.splitlines() if line.strip()] if rc == 0 else []

    async def _our_reviews(self, repo: str, pr: int) -> list[dict]:
        """Our posted reviews (marker-bearing), oldest→newest: [{head, verdict, promoted, state, body, id}]."""
        rc, out, _err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/pulls/{pr}/reviews",
                "--paginate",
                "--jq",
                "[.[] | {id: .id, state: .state, body: .body}]",
            ],
        )
        if rc != 0:
            return []
        try:
            rows = json.loads(out)
        except json.JSONDecodeError:
            return []
        ours = []
        for row in rows if isinstance(rows, list) else []:
            marker = parse_verdict_marker(row.get("body") or "")
            if marker:
                ours.append(
                    {**marker, "state": row.get("state", ""), "body": row.get("body") or "", "id": row.get("id")}
                )
        return ours

    async def _finding_sources(self, repo: str, pr: int, head: str, findings: list[dict]) -> dict[str, str | None]:
        """{file: text-to-ground-against} for the files the findings cite.

        The haystack is the file AT THE REVIEWED HEAD plus this PR's patch for it. The
        patch matters: a removed-behaviour finding legitimately quotes code the head no
        longer contains, and grounding it against the head alone would downgrade the
        panel's sharpest angle. An unreadable file maps to None — fail open, never
        downgrade on a failed read (the `confine_findings` posture).
        """
        patches: dict[str, str] = {}
        rc, out, _err = await self._run_gh(
            ["api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--jq", "[.[] | {f: .filename, p: .patch}]"]
        )
        if rc == 0:
            try:
                for row in json.loads(out) or []:
                    if isinstance(row, dict) and row.get("f"):
                        patches[str(row["f"])] = str(row.get("p") or "")
            except json.JSONDecodeError:
                pass
        sources: dict[str, str | None] = {}
        for file in {str(f.get("file") or "") for f in findings if f.get("file")}:
            rc, out, _err = await self._run_gh(["api", f"repos/{repo}/contents/{file}?ref={head}", "--jq", ".content"])
            blob = ""
            if rc == 0 and out.strip():
                try:
                    import base64

                    blob = base64.b64decode(out.strip()).decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001 — an undecodable blob is a failed read
                    blob = ""
            patch = patches.get(file, "")
            sources[file] = f"{blob}\n{patch}" if (blob or patch) else None
        return sources

    async def _delta_ranges(self, repo: str, base: str, head: str) -> dict | None:
        """Line ranges that moved between two reviewed heads, or None (unreadable).

        None is load-bearing: `converge` grants no relief without a readable delta, so
        a failed compare costs a round of churn, never a laundered verdict.
        """
        if not base or not head or base == head:
            return None
        rc, out, _err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/compare/{base}...{head}",
                "--jq",
                "[.files[]? | {filename: .filename, patch: .patch}]",
            ],
        )
        if rc != 0:
            return None
        try:
            files = json.loads(out)
        except json.JSONDecodeError:
            return None
        return delta_ranges(files) if isinstance(files, list) else None

    async def _checks_state(self, repo: str, sha: str) -> str | None:
        """'green' | 'pending' | 'failed' | None(unreadable). NO check runs at all →
        'no-checks' (terminal by definition, but NEVER green — Quinn's allChecksGreen
        fails closed on empty; a checkless repo never auto-promotes)."""
        rc, out, _err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/commits/{sha}/check-runs",
                "--paginate",
                "--jq",
                "[.check_runs[] | {status: .status, conclusion: .conclusion}]",
            ],
        )
        if rc != 0:
            return None
        try:
            runs = json.loads(out)
        except json.JSONDecodeError:
            return None
        if not runs:
            return "no-checks"
        if any(r.get("status") in _NON_TERMINAL for r in runs):
            return "pending"
        if all((r.get("conclusion") or "") in _GREEN for r in runs):
            return "green"
        return "failed"

    async def _unresolved_threads(self, repo: str, pr: int) -> int | None:
        owner, name = repo.split("/", 1)
        rc, out, _err = await self._run_gh(
            [
                "api",
                "graphql",
                "-f",
                f'query=query {{ repository(owner: "{owner}", name: "{name}") '
                f"{{ pullRequest(number: {pr}) {{ reviewThreads(first: 100) {{ nodes {{ isResolved }} }} }} }} }}",
                "--jq",
                "[.data.repository.pullRequest.reviewThreads.nodes[].isResolved] | map(select(. == false)) | length",
            ],
        )
        if rc != 0:
            return None
        try:
            return int(out.strip())
        except ValueError:
            return None

    async def _existing_threads_block(self, repo: str, pr: int) -> str:
        """The rendered <pr_review_threads> block, or "" (unreadable/none — the
        recipe default "(none)" applies; thread awareness never blocks a review)."""
        from .threads import fetch_threads, render_threads_block

        try:
            nodes = await fetch_threads(self._run_gh, repo, pr)
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] existing-threads fetch failed on %s#%s", repo, pr)
            return ""
        return render_threads_block(nodes) if nodes else ""

    # ── the review path ───────────────────────────────────────────────────────

    async def handle_pr_event(self, repo: str, pr: int, head_sha: str, action: str) -> str:
        """Webhook/manual entry. Returns 'reviewed:<verdict>' or a typed drop/outcome."""
        if action not in DISPATCH_ACTIONS:
            self.telemetry.emit("drop", repo=repo, pr=pr, reason="not-a-dispatch-action", action=action)
            return "drop:not-a-dispatch-action"
        if bad_repo(repo) or (self.repos and repo not in self.repos):
            # The allowlist gate runs BEFORE any GitHub call — an unmanaged repo must
            # not trigger PR lookups on our credentials (Quinn's gate ordering).
            self.telemetry.emit("drop", repo=repo, pr=pr, reason="unlisted-repo")
            return "drop:unlisted-repo"
        decision = self.chokepoint.admit(repo, pr, head_sha)
        if decision != "accept":
            self.telemetry.emit("drop", repo=repo, pr=pr, sha=head_sha, reason=decision)
            return f"drop:{decision}"
        try:
            return await self._review(repo, pr)
        finally:
            self.chokepoint.done(repo, pr)

    async def handle_summon(self, repo: str, pr: int, actor: str) -> str:
        """An operator asked for a review (issue #28). Same panel, two differences.

        A summon bypasses the COOLDOWN (that exists to eat webhook bursts; a human who
        typed a command is not a burst) and the REAFFIRM short-circuit (an unchanged head
        with a posted verdict normally reaffirms without re-spending the panel — but
        `@vera review` on an unchanged head is precisely the "I think you got this wrong"
        case, and reaffirming it would answer the question with the answer under dispute).

        Everything else is unchanged: allowlist, eligibility, self-authored, in-flight,
        confinement, grounding, fail-closed exhaustion.
        """
        if bad_repo(repo) or (self.repos and repo not in self.repos):
            self.telemetry.emit("drop", repo=repo, pr=pr, reason="unlisted-repo", summon=actor)
            return "drop:unlisted-repo"
        decision = self.chokepoint.admit(repo, pr, f"summon-{pr}", bypass_cooldown=True)
        if decision != "accept":
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=decision, summon=actor)
            return f"drop:{decision}"
        self.telemetry.emit("summon", repo=repo, pr=pr, actor=actor)
        try:
            return await self._review(repo, pr, force=True)
        finally:
            self.chokepoint.done(repo, pr)

    async def _review(self, repo: str, pr: int, *, force: bool = False) -> str:
        started = time.monotonic()
        facts = await self._pr_facts(repo, pr)
        if not facts or facts.get("state") != "open" or facts.get("draft"):
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_PR_NOT_ELIGIBLE)
            return f"drop:{DROP_PR_NOT_ELIGIBLE}"
        viewer = await self._viewer_login()
        author = str(facts.get("author") or "").lower()
        if (
            viewer
            and not bool(self.cfg.get("allow_self_review", False))
            and (author == viewer or author.removesuffix("[bot]") == viewer.removesuffix("[bot]"))
        ):
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_SELF_AUTHORED, author=author)
            return f"drop:{DROP_SELF_AUTHORED}"

        head = str(facts["head"])
        paths = await self._changed_paths(repo, pr)
        fires, reasons = structural_trigger(
            changed_files=int(facts.get("changed_files") or len(paths)),
            lines_changed=int(facts.get("additions") or 0) + int(facts.get("deletions") or 0),
            changed_paths=paths,
        )
        recipe = "code-review-structural" if fires else "code-review"

        ours = await self._our_reviews(repo, pr)
        # ROUNDS, not reviews (issue #23): promotion bodies carry our marker and no
        # findings, so `ours[-1]` after an approve-on-green was an empty recall — the
        # delta re-review silently degraded to a cold one, which is what kept #88
        # rediscovering the same surface. `panel_rounds` also folds a re-gate's
        # verbatim re-post back into the head it belongs to.
        history = panel_rounds(ours)
        current = next((r for r in reversed(history) if r["head"] == head), None)
        if current and not force:  # `force` = an operator summon disputing this verdict
            # Unchanged head with a posted verdict — reaffirm, don't re-spend the panel.
            self.telemetry.emit("reaffirm", repo=repo, pr=pr, sha=head, verdict=current["verdict"])
            return f"reaffirmed:{current['verdict']}"
        prior = history[-1] if history else None
        round_number = len(history) + 1
        prior_findings = json.dumps(prior["findings"]) if prior and prior["findings"] else ""
        prior_requests = render_prior_requests(history)

        runner = self._runner()
        if runner is None:
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_NO_RUNNER)
            return f"drop:{DROP_NO_RUNNER}"
        self.telemetry.emit(
            "dispatch",
            repo=repo,
            pr=pr,
            sha=head,
            recipe=recipe,
            trigger_reasons=reasons,
            delta=bool(prior_findings),
            round=round_number,
        )
        # Server-resolved refs ride along: finders pin code reads to the head SHA
        # and policy-doc reads to the base ref (a PR must not rewrite the rules it
        # is judged by). A host recipe without these declared just ignores them.
        inputs = {
            "pr": str(pr),
            "repo": repo,
            "head_sha": head,
            "base_ref": str(facts.get("base_ref") or ""),
        }
        if prior_findings:
            inputs["prior_findings"] = prior_findings
        if prior_requests:
            # The panel's own request history — a change it demanded is verified as
            # implemented, not re-litigated as a novel unrequested delta (issue #23).
            inputs["prior_requests"] = prior_requests
            inputs["review_round"] = str(round_number)
        threads_block = await self._existing_threads_block(repo, pr)
        if threads_block:
            inputs["existing_threads"] = threads_block
        # D3 spells the caller's options as "retry or escalate to the operator" — a
        # partial panel still never synthesizes a verdict, we just don't give up on
        # the FIRST failure. Retries re-run the whole recipe (the runner's unit of
        # work); the panel is deterministic in its inputs, so a rerun is a fresh
        # draw against whatever starved the failed step.
        result: dict = {}
        failed: list = []
        for attempt in range(1, self.panel_retries + 2):
            last = attempt == self.panel_retries + 1
            try:
                result = await runner(recipe, inputs)
            except Exception as exc:  # noqa: BLE001
                if not last:
                    self.telemetry.emit(
                        "panel_retry", repo=repo, pr=pr, sha=head, attempt=attempt, crashed=type(exc).__name__
                    )
                    continue
                self._escalate(
                    f"pr-reviewer: review run crashed on {repo}#{pr} ({type(exc).__name__}: {exc}) — PR is UNREVIEWED.",
                    dedup_key=f"pr-reviewer-crash:{repo}#{pr}@{head[:7]}",
                )
                return "error:run-crashed"
            failed = list(result.get("failed") or [])
            if not failed:
                break
            if not last:
                self.telemetry.emit("panel_retry", repo=repo, pr=pr, sha=head, attempt=attempt, failed=failed)
        if failed:
            # Retries spent: D3's other branch. No verdict, operator escalation — and
            # the sweep's backfill will try again on a later pass (issue #17), so an
            # exhausted PR is no longer abandoned for good.
            self._escalate(
                f"pr-reviewer: panel step(s) {failed} failed on {repo}#{pr} "
                f"after {self.panel_retries + 1} attempt(s) — no verdict posted; PR is UNREVIEWED.",
                dedup_key=f"pr-reviewer-exhaustion:{repo}#{pr}@{head[:7]}",
            )
            self.telemetry.emit(
                "exhaustion", repo=repo, pr=pr, sha=head, failed=failed, attempts=self.panel_retries + 1
            )
            return "error:panel-exhausted"

        # Per-step timings (protoAgent's engine, additive — {} on an older host). The
        # panel's cost is nine LLM steps and a single `latency_s` cannot say which one to
        # attack; this is what turns "the panel is slow" into a step name.
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        output = str(result.get("output") or "")
        findings, confined = confine_findings(self._parse_findings(output), paths)
        if confined:
            # Server-side in-diff enforcement — prompt discipline made a promise,
            # this keeps it. The drops are telemetered (eval evidence) and footnoted
            # in the posted body so the verdict never silently disagrees with the report.
            self.telemetry.emit(
                "confined",
                repo=repo,
                pr=pr,
                sha=head,
                dropped=[
                    {"file": str(f.get("file") or ""), "severity": str(f.get("severity") or "")} for f in confined
                ],
            )
        # Evidence grounding (issue #25) runs BEFORE the mapping, unlike convergence:
        # it doesn't reconsider a verdict, it corrects the findings the verdict is
        # computed from. A finding quoting code that isn't at the reviewed head is
        # annotated `uncertain`, which verdict_for already refuses to turn into a FAIL.
        grounded_findings, ungrounded = [], []
        grounding_checked = 0
        if self.grounding_enabled and findings:
            sources = await self._finding_sources(repo, pr, head, findings)
            grounded_findings, ungrounded = apply_grounding(findings, sources)
            grounding_checked = len(findings)
            findings = grounded_findings
        if ungrounded:
            self.telemetry.emit("ungrounded", repo=repo, pr=pr, sha=head, round=round_number, downgraded=ungrounded)
        verdict = verdict_for(findings)
        # Convergence (issue #23) sits AFTER the pure mapping, never inside it: ADR
        # 0078 C's rule is that findings decide the verdict, and that still holds —
        # this only asks whether a non-blocking verdict is still worth another round.
        ranges = None
        if prior and self.convergence_rounds and verdict == WARN and round_number >= self.convergence_rounds:
            ranges = await self._delta_ranges(repo, prior["head"], head)
        verdict, notes, reason = converge(
            verdict, findings, round_number=round_number, ranges=ranges, threshold=self.convergence_rounds
        )
        if notes or reason.startswith("converged"):
            self.telemetry.emit(
                "converged", repo=repo, pr=pr, sha=head, round=round_number, reason=reason, notes=len(notes)
            )
        # An unexplained clearance (issue #26): this clean PASS would dismiss our own
        # standing block, but a prior round of this same panel confirmed a blocker/major
        # that this round neither reports nor explains. Hold the block; the verdict still
        # posts, and a second consecutive clean PASS lifts it.
        # #26 in its general form: a prior blocker/major must be DISPOSITIONED
        # (fixed / open / refuted), whatever this round's verdict is. `unexplained_clearance`
        # could only guard a clean PASS, because silence there is unambiguous; with an
        # explicit dispositions block the same debt is visible at any verdict. A recipe
        # that emits no block falls back to the narrower rule rather than losing the guard.
        dispositions = parse_dispositions(output) if self.hold_unexplained else []
        unaccounted = unaccounted_priors(history, dispositions)
        # The two guards are a fallback chain, not a belt-and-braces pair. When the panel
        # HAS dispositioned its priors, that statement is the authority — re-applying the
        # clean-PASS heuristic on top would hold a block the panel just explained, making
        # the explicit contract worthless exactly where it matters most.
        dropped_finding = (
            unexplained_clearance(history, verdict, findings) if (self.hold_unexplained and not dispositions) else None
        )
        trailer = render_notes_section(notes) + render_grounding_footnote(ungrounded)
        if unaccounted:
            trailer += render_unaccounted_note(unaccounted)
            self.telemetry.emit(
                "unaccounted_priors",
                repo=repo,
                pr=pr,
                sha=head,
                round=round_number,
                findings=[
                    {"file": str(m.get("file") or ""), "severity": str(m.get("severity") or "")} for m in unaccounted
                ],
            )
        if dropped_finding:
            trailer += render_held_note(dropped_finding)
            self.telemetry.emit(
                "clearance_held",
                repo=repo,
                pr=pr,
                sha=head,
                round=round_number,
                severity=str(dropped_finding.get("severity") or ""),
                file=str(dropped_finding.get("file") or ""),
            )
        elapsed = time.monotonic() - started
        posted = await self._post_verdict(
            repo,
            pr,
            head,
            verdict,
            output,
            recipe,
            confined=confined,
            notes=trailer,
            hold_blocks=bool(dropped_finding) or bool(unaccounted),
        )
        self.telemetry.emit(
            "reviewed",
            repo=repo,
            pr=pr,
            sha=head,
            recipe=recipe,
            verdict=verdict,
            round=round_number,
            findings=len(findings),
            notes=len(notes),
            held=bool(dropped_finding) or bool(unaccounted),
            confined=len(confined),
            # Every guard reports what it DECIDED, not only when it acted. A rule that
            # is silent unless it fires cannot be distinguished from a rule that never
            # ran — twice tonight "grounding checked N and downgraded 0" had to be
            # reconstructed by hand-fetching blobs. Absence of an event is not evidence.
            converge_reason=reason,
            grounding_checked=grounding_checked,
            grounding_downgraded=len(ungrounded),
            dispositions=len(dispositions),
            unaccounted=len(unaccounted),
            latency_s=round(elapsed, 1),
            step_s=timings or None,
            slowest_step=(max(timings, key=timings.get) if timings else None),
            posted=posted,
            shadow=self.shadow,
        )
        return f"reviewed:{verdict}" if posted else f"error:post-failed:{verdict}"

    @staticmethod
    def _parse_findings(output: str) -> list[dict]:
        try:
            from graph.review.findings import parse_findings

            return [f.to_dict() for f in parse_findings(output)]
        except Exception:  # noqa: BLE001 — host-free fallback: last fenced array
            from .verdicts import extract_findings_json

            text = extract_findings_json(output)
            try:
                return json.loads(text) if text else []
            except json.JSONDecodeError:
                return []

    async def _post_verdict(
        self,
        repo: str,
        pr: int,
        head: str,
        verdict: str,
        report: str,
        recipe: str,
        confined: list[dict] | None = None,
        notes: str = "",
        hold_blocks: bool = False,
    ) -> bool:
        body = render_verdict_body(
            repo=repo,
            pr=pr,
            head_sha=head,
            verdict=verdict,
            report=report,
            shadow=self.shadow,
            recipe=recipe,
            confined=confined,
            notes=notes,
        )
        event = "COMMENT"
        if not self.shadow and verdict == FAIL:
            # A blocking verdict only against terminal CI (#863) — else comment now;
            # the next push/sweep re-evaluates.
            checks = await self._checks_state(repo, head)
            event = "REQUEST_CHANGES" if checks in ("green", "failed", "no-checks") else "COMMENT"
        rc, _out, err = await self._run_gh(
            ["api", f"repos/{repo}/pulls/{pr}/reviews", "-X", "POST", "-f", f"event={event}", "-f", f"body={body}"],
            timeout=60,
        )
        if rc != 0:
            log.warning("[pr-reviewer] posting %s on %s#%s failed: %s", event, repo, pr, err[-300:])
            return False
        if not self.shadow and verdict != FAIL and not hold_blocks:
            # A cleared verdict must also LIFT our earlier block: PASS/WARN post as
            # COMMENT, and a comment never supersedes the same reviewer's REQUEST_CHANGES.
            # `hold_blocks` is the one exception — a clean PASS that silently dropped a
            # prior blocker/major has not earned the dismissal yet (issue #26).
            await self._dismiss_stale_blocks(repo, pr)
        return True

    async def _dismiss_stale_blocks(self, repo: str, pr: int) -> None:
        """Dismiss our own now-stale REQUEST_CHANGES reviews after a later head clears.
        With a `pull_request` branch rule active, changes-requested blocks the merge at
        ANY approval count, and only an APPROVE or a dismissal lifts it — never a
        comment. APPROVE stays reserved for the promotion owner, so the gate lifts
        itself by dismissal. Every non-dismissed blocker goes (GitHub rolls the
        reviewer's effective state back to the previous one otherwise)."""
        for review in await self._our_reviews(repo, pr):
            if review.get("state") != "CHANGES_REQUESTED" or not review.get("id"):
                continue
            rc, _out, err = await self._run_gh(
                [
                    "api",
                    f"repos/{repo}/pulls/{pr}/reviews/{review['id']}/dismissals",
                    "-X",
                    "PUT",
                    "-f",
                    "message=Superseded — a later head cleared the QA panel (see the newest verdict).",
                    "-f",
                    "event=DISMISS",
                ],
                timeout=60,
            )
            if rc != 0:
                log.warning("[pr-reviewer] dismissing stale block on %s#%s failed: %s", repo, pr, err[-300:])
            self.telemetry.emit("dismissal", repo=repo, pr=pr, review_id=review["id"], ok=rc == 0)

    # ── re-gate: arm a block the CI clock beat us to (issue #16) ──────────────

    async def evaluate_regate(self, repo: str, pr: int) -> str:
        """Post the blocking review a pending-CI FAIL couldn't post at review time.

        `_post_verdict` must decide COMMENT vs REQUEST_CHANGES the moment the panel
        lands, and #863 forbids blocking against non-terminal CI — so a fast reviewer
        (verdict in ~10s, CI still queued) posts a comment and the gate never arms.
        This is the mirror of `_dismiss_stale_blocks`: that one LIFTS our block when a
        later head clears, this one ARMS it when the checks we were waiting on finish.

        The stored verdict body is re-posted verbatim (plus a one-line note): the
        judgement was already made and paid for — only the GitHub review event changes.
        """
        if self.shadow:
            return HOLD_REGATE_SHADOW
        if not self.regate_enabled:
            return HOLD_REGATE_DISABLED
        facts = await self._pr_facts(repo, pr)
        if not facts or facts.get("state") != "open" or facts.get("draft"):
            return "hold:pr-not-eligible"
        head = str(facts["head"])
        ours = await self._our_reviews(repo, pr)
        latest = ours[-1] if ours else None
        if not latest or latest["head"] != head or latest["verdict"] != FAIL:
            # No FAIL standing against the current head — a later PASS/WARN supersedes
            # (and `_dismiss_stale_blocks` already handled any block it left behind).
            return HOLD_REGATE_NO_FAIL
        if any(r.get("state") == "CHANGES_REQUESTED" and r["head"] == head for r in ours):
            return HOLD_REGATE_ALREADY
        backoff_key = f"{repo}#{pr}@{head}"
        if self._regate_failures.get(backoff_key, 0) >= REGATE_MAX_FAILURES:
            self.telemetry.emit("regate", repo=repo, pr=pr, sha=head, decision=HOLD_REGATE_BACKOFF)
            return HOLD_REGATE_BACKOFF
        checks = await self._checks_state(repo, head)
        if checks is None:
            decision = HOLD_REGATE_CHECKS_UNKNOWN
        elif checks == "pending":
            decision = HOLD_REGATE_CHECKS_PENDING
        else:
            decision = REGATE
        if decision != REGATE:
            self.telemetry.emit("regate", repo=repo, pr=pr, sha=head, decision=decision)
            return decision
        body = (
            f"_Checks are terminal ({checks}) — arming the FAIL verdict below as a blocking review "
            f"(it posted as a comment while CI was still pending)._\n\n" + latest["body"]
        )
        rc, _out, err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/pulls/{pr}/reviews",
                "-X",
                "POST",
                "-f",
                "event=REQUEST_CHANGES",
                "-f",
                f"body={body}",
            ],
            timeout=60,
        )
        if rc != 0:
            failures = self._regate_failures.get(backoff_key, 0) + 1
            self._regate_failures[backoff_key] = failures
            if len(self._regate_failures) > 1024:  # bounded, like the promote counter
                self._regate_failures = dict(list(self._regate_failures.items())[-512:])
            log.warning(
                "[pr-reviewer] re-gate REQUEST_CHANGES on %s#%s failed (%d/%d): %s",
                repo,
                pr,
                failures,
                REGATE_MAX_FAILURES,
                err[-300:],
            )
            if failures >= REGATE_MAX_FAILURES:
                self._escalate(
                    f"pr-reviewer: re-gate keeps failing on {repo}#{pr} @{head[:7]} ({failures}× — backing off "
                    f"until a new head). A FAIL verdict is standing but NOT blocking. Last error: {err[-200:]}",
                    dedup_key=f"pr-reviewer-regate-backoff:{backoff_key[:64]}",
                )
            self.telemetry.emit("regate", repo=repo, pr=pr, sha=head, decision="error:regate-failed")
            return "error:regate-failed"
        self._regate_failures.pop(backoff_key, None)
        self.telemetry.emit("regate", repo=repo, pr=pr, sha=head, decision=REGATE, checks=checks)
        return REGATE

    # ── promotion (edge + sweep share this) ───────────────────────────────────

    async def evaluate_promotion(self, repo: str, pr: int) -> str:
        """One PR through the approve-on-green pure function; applies only when we own
        promotion AND not shadow. Every hold is telemetered (the dry-run evidence)."""
        facts = await self._pr_facts(repo, pr)
        if not facts or facts.get("state") != "open" or facts.get("draft"):
            return "hold:pr-not-eligible"
        head = str(facts["head"])
        ours = await self._our_reviews(repo, pr)
        # The latest PANEL ROUND decides — not `ours[-1]`, which can be our own promotion
        # body (marker-bearing, no findings). Same shadowing that made delta re-reviews
        # recall nothing before #24; here it would silently promote with an empty
        # findings list and defeat the carry-forward below.
        history = panel_rounds(ours)
        # PASS/WARN are non-blocking (promotable — Quinn's WARN "does NOT block merge");
        # a latest FAIL holds until a re-review clears it.
        latest = history[-1] if history else None
        clear = latest if latest and latest["verdict"] in (PASS, WARN) else None
        promoted = any(r["state"] == "APPROVED" and r["head"] == head for r in ours)
        obs = Observations(
            head_sha=head,
            checks_state=await self._map_checks_for_promotion(repo, head),
            unresolved_threads=await self._unresolved_threads(repo, pr),
            verdict_head=clear["head"] if clear else None,
            verdict_promoted=promoted,
            promotion_owner=self.promotion_owner and not self.shadow,
        )
        backoff_key = f"{repo}#{pr}@{head}"
        if self._promote_failures.get(backoff_key, 0) >= PROMOTE_MAX_FAILURES:
            self.telemetry.emit("promotion", repo=repo, pr=pr, sha=head, decision=HOLD_PROMOTE_BACKOFF)
            return HOLD_PROMOTE_BACKOFF
        decision = promotion_decision(obs)
        self.telemetry.emit("promotion", repo=repo, pr=pr, sha=head, decision=decision)
        if decision != PROMOTE:
            return decision
        verdict = clear["verdict"] if clear else PASS
        # A promoted WARN carries its findings forward (issue #22). Otherwise the PR
        # reads APPROVED seconds after a confirmed finding lands and the finding has no
        # consumer at all — which is how projectBoard-plugin#80 shipped a defect. The
        # marker gains `findings=N` so merge tooling can gate on "approved WITH findings"
        # without parsing prose. Deliberately NOT a block: this session showed a
        # hallucinated blocker surviving two rounds, so gate rigidity must not outrun
        # verdict reliability.
        open_findings = [f for f in (clear.get("findings") or []) if isinstance(f, dict)] if clear else []
        marker = f"<!-- protoagent-qa-review head={head} verdict={verdict} promoted=true"
        if open_findings:
            marker += f" findings={len(open_findings)}"
        body = (
            f"{marker} -->\n"
            f"Promoting the {verdict} verdict for head `{head[:12]}`: all checks terminal-green, "
            f"zero unresolved review threads. (approve-on-green)"
            f"{render_promotion_findings(open_findings)}"
        )
        rc, _out, err = await self._run_gh(
            ["api", f"repos/{repo}/pulls/{pr}/reviews", "-X", "POST", "-f", "event=APPROVE", "-f", f"body={body}"],
            timeout=60,
        )
        if rc != 0:
            failures = self._promote_failures.get(backoff_key, 0) + 1
            self._promote_failures[backoff_key] = failures
            if len(self._promote_failures) > 1024:  # bounded, like the chokepoint
                self._promote_failures = dict(list(self._promote_failures.items())[-512:])
            log.warning(
                "[pr-reviewer] promotion APPROVE on %s#%s failed (%d/%d): %s",
                repo,
                pr,
                failures,
                PROMOTE_MAX_FAILURES,
                err[-300:],
            )
            if failures >= PROMOTE_MAX_FAILURES:
                self._escalate(
                    f"pr-reviewer: promotion APPROVE keeps failing on {repo}#{pr} @{head[:7]} "
                    f"({failures}× — backing off until a new head). Last error: {err[-200:]}",
                    dedup_key=f"pr-reviewer-promote-backoff:{backoff_key[:64]}",
                )
            return "error:approve-failed"
        self._promote_failures.pop(backoff_key, None)
        armed = False
        if str(facts.get("base_ref") or "") == "main":
            # Quinn's last step: arm native squash auto-merge — but only onto main
            # (stacked PRs excluded; #901's scope lesson). Best-effort: a repo with
            # auto-merge disabled just declines.
            rc2, _o, _e = await self._run_gh(["pr", "merge", str(pr), "--repo", repo, "--auto", "--squash"], timeout=30)
            armed = rc2 == 0
        self.telemetry.emit("promoted", repo=repo, pr=pr, sha=head, auto_merge_armed=armed)
        return PROMOTE

    async def _map_checks_for_promotion(self, repo: str, sha: str) -> str | None:
        state = await self._checks_state(repo, sha)
        # 'no-checks' is terminal but NEVER green for promotion (fails closed).
        return "failed" if state == "no-checks" else state

    # ── backfill: a first review for a PR no live event ever reached (issue #17) ──

    async def needs_backfill(self, repo: str, pr: int) -> str | None:
        """The head SHA to review, or None when this PR already has a current verdict.

        `hold:no-clear-verdict` is otherwise terminal: dispatch actions only fire for
        live webhook events, so a PR opened before the reviewer existed — or while it
        was down, or whose panel exhausted — holds on every tick, forever, and can
        never be promoted. Cheap checks first; this runs per-PR per-pass.
        """
        facts = await self._pr_facts(repo, pr)
        if not facts or facts.get("state") != "open" or facts.get("draft"):
            return None
        head = str(facts["head"])
        ours = await self._our_reviews(repo, pr)
        if any(r["head"] == head for r in ours):
            return None  # a verdict for the CURRENT head exists — nothing to backfill
        return head

    async def backfill_review(self, repo: str, pr: int, head: str) -> str:
        """Review a PR the sweep found without a verdict — same path as the edge, so
        every guard (self-authored, eligibility, cooldown, in-flight) still applies."""
        decision = self.chokepoint.admit(repo, pr, head)
        if decision != "accept":
            self.telemetry.emit("drop", repo=repo, pr=pr, sha=head, reason=decision, action=BACKFILL_ACTION)
            return f"drop:{decision}"
        self.telemetry.emit("backfill", repo=repo, pr=pr, sha=head)
        try:
            return await self._review(repo, pr)
        finally:
            self.chokepoint.done(repo, pr)

    async def reconcile_pr(self, repo: str, pr: int, *, backfill_budget: int = 0) -> tuple[str, int]:
        """One PR reconciled to the state its verdict implies. Returns (outcome, budget left).

        Order matters, and it is the cheap-and-decisive-first order:
          1. backfill — no verdict at all, so nothing downstream can decide anything
          2. re-gate  — a FAIL that isn't blocking yet (the merge-race is live NOW)
          3. promote  — a clear verdict on terminal-green (the existing behaviour)
        A backfilled PR skips 2 and 3 this pass: the fresh review just posted its own
        verdict through the normal path, and the next tick sees the settled state.
        """
        if backfill_budget > 0:
            head = await self.needs_backfill(repo, pr)
            if head:
                return await self.backfill_review(repo, pr, head), backfill_budget - 1
        regated = await self.evaluate_regate(repo, pr)
        if regated == REGATE:
            # A block just went up; promotion on the same pass would be incoherent.
            return regated, backfill_budget
        return await self.evaluate_promotion(repo, pr), backfill_budget

    async def sweep_once(self) -> int:
        """The 3-minute level pass: every open PR in every managed repo reconciled
        (backfill → re-gate → promote). Returns PRs evaluated. Never raises."""
        count = 0
        budget = self.backfill_per_pass
        for repo in self.repos:
            try:
                rc, out, _err = await self._run_gh(
                    ["api", f"repos/{repo}/pulls?state=open&per_page=100", "--jq", "[.[].number]"]
                )
                numbers = json.loads(out) if rc == 0 else []
            except Exception:  # noqa: BLE001
                numbers = []
            for pr in numbers if isinstance(numbers, list) else []:
                try:
                    _outcome, budget = await self.reconcile_pr(repo, int(pr), backfill_budget=budget)
                    count += 1
                except Exception:  # noqa: BLE001
                    log.exception("[pr-reviewer] sweep reconcile failed on %s#%s", repo, pr)
        return count


async def sweep_loop(dispatcher: Dispatcher, interval_s: int, stop_event: asyncio.Event) -> None:
    """The background surface body — single-flight by construction (one loop)."""
    while not stop_event.is_set():
        try:
            await dispatcher.sweep_once()
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] sweep pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass

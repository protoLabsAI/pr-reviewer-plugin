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
import time

from .approve import PROMOTE, Observations, promotion_decision
from .chokepoint import DISPATCH_ACTIONS, Chokepoint
from .gh_cli import bad_repo, run_gh
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

_NON_TERMINAL = {"queued", "in_progress", "waiting", "requested", "pending"}
_GREEN = {"success", "neutral", "skipped"}


class Dispatcher:
    def __init__(self, cfg: dict, telemetry: Telemetry, *, run_gh_fn=None, workflow_run=None, inbox_add=None):
        self.cfg = cfg or {}
        self.telemetry = telemetry
        self.repos = [str(r) for r in (self.cfg.get("repos") or []) if r]
        self.shadow = bool(self.cfg.get("shadow_mode", True))
        self.promotion_owner = bool(self.cfg.get("promotion_owner", False))
        self.chokepoint = Chokepoint(cooldown_s=int(self.cfg.get("cooldown_s") or 30))
        self._run_gh = run_gh_fn or run_gh
        self._workflow_run = workflow_run  # None → resolve STATE.workflow_run lazily
        self._inbox_add = inbox_add  # None → resolve STATE.inbox_store lazily
        self._viewer: str | None = None
        self._promote_failures: dict[str, int] = {}  # repo#pr@head -> consecutive APPROVE failures

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

    async def _review(self, repo: str, pr: int) -> str:
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
        prior = ours[-1] if ours else None
        if prior and prior["head"] == head:
            # Unchanged head with a posted verdict — reaffirm, don't re-spend the panel.
            self.telemetry.emit("reaffirm", repo=repo, pr=pr, sha=head, verdict=prior["verdict"])
            return f"reaffirmed:{prior['verdict']}"
        prior_findings = ""
        if prior:
            from .verdicts import extract_findings_json

            prior_findings = extract_findings_json(prior["body"])

        runner = self._runner()
        if runner is None:
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_NO_RUNNER)
            return f"drop:{DROP_NO_RUNNER}"
        self.telemetry.emit(
            "dispatch", repo=repo, pr=pr, sha=head, recipe=recipe, trigger_reasons=reasons, delta=bool(prior_findings)
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
        try:
            result = await runner(recipe, inputs)
        except Exception as exc:  # noqa: BLE001
            self._escalate(
                f"pr-reviewer: review run crashed on {repo}#{pr} ({type(exc).__name__}: {exc}) — PR is UNREVIEWED.",
                dedup_key=f"pr-reviewer-crash:{repo}#{pr}@{head[:7]}",
            )
            return "error:run-crashed"

        failed = list(result.get("failed") or [])
        if failed:
            # D3: a partial panel is not a review. No verdict, operator escalation.
            self._escalate(
                f"pr-reviewer: panel step(s) {failed} failed on {repo}#{pr} — no verdict posted; PR is UNREVIEWED.",
                dedup_key=f"pr-reviewer-exhaustion:{repo}#{pr}@{head[:7]}",
            )
            self.telemetry.emit("exhaustion", repo=repo, pr=pr, sha=head, failed=failed)
            return "error:panel-exhausted"

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
        verdict = verdict_for(findings)
        elapsed = time.monotonic() - started
        posted = await self._post_verdict(repo, pr, head, verdict, output, recipe, confined=confined)
        self.telemetry.emit(
            "reviewed",
            repo=repo,
            pr=pr,
            sha=head,
            recipe=recipe,
            verdict=verdict,
            findings=len(findings),
            confined=len(confined),
            latency_s=round(elapsed, 1),
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
        if not self.shadow and verdict != FAIL:
            # A cleared verdict must also LIFT our earlier block: PASS/WARN post as
            # COMMENT, and a comment never supersedes the same reviewer's REQUEST_CHANGES.
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

    # ── promotion (edge + sweep share this) ───────────────────────────────────

    async def evaluate_promotion(self, repo: str, pr: int) -> str:
        """One PR through the approve-on-green pure function; applies only when we own
        promotion AND not shadow. Every hold is telemetered (the dry-run evidence)."""
        facts = await self._pr_facts(repo, pr)
        if not facts or facts.get("state") != "open" or facts.get("draft"):
            return "hold:pr-not-eligible"
        head = str(facts["head"])
        ours = await self._our_reviews(repo, pr)
        # The latest verdict decides: PASS/WARN are non-blocking (promotable — Quinn's
        # WARN "does NOT block merge"); a latest FAIL holds until a re-review clears it.
        latest = ours[-1] if ours else None
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
        body = (
            f"<!-- protoagent-qa-review head={head} verdict={verdict} promoted=true -->\n"
            f"Promoting the {verdict} verdict for head `{head[:12]}`: all checks terminal-green, "
            f"zero unresolved review threads. (approve-on-green)"
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

    async def sweep_once(self) -> int:
        """The 3-minute level pass: every open PR in every managed repo through the
        promotion decision. Returns PRs evaluated. Never raises."""
        count = 0
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
                    await self.evaluate_promotion(repo, int(pr))
                    count += 1
                except Exception:  # noqa: BLE001
                    log.exception("[pr-reviewer] sweep promotion failed on %s#%s", repo, pr)
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

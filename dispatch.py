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

from .approve import HOLD_NOT_OWNER, PROMOTE, Observations, promotion_decision
from .chokepoint import DISPATCH_ACTIONS, Chokepoint
from .gh_cli import bad_repo, run_gh
from .grounding import apply_grounding, correct_line_numbers, render_grounding_footnote
from .protopatch import UNAVAILABLE_PREFIX
from .rounds import (
    DEFAULT_CONVERGENCE_ROUNDS,
    converge,
    delta_ranges,
    panel_rounds,
    parse_dispositions,
    render_degraded_note,
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
    extract_brief,
    merge_carried_findings,
    parse_verdict_marker,
    render_verdict_body,
    report_hard_stopped,
    verdict_for,
)

log = logging.getLogger("protoagent.plugins.pr_reviewer")

DROP_SELF_AUTHORED = "self-authored"
DROP_PR_NOT_ELIGIBLE = "pr-not-eligible"  # closed, draft, LOCKED, or facts unreadable
DROP_NO_RUNNER = "no-workflow-runner"
DROP_PAUSED = "paused-by-operator"  # `@vera pause` (issue #28)
DROP_REVIEWS_UNREADABLE = "reviews-unreadable"  # blind on our own history (issue #71)
DROP_VIEWER_UNKNOWN = "viewer-unknown"  # blind on our own IDENTITY — can't rule out self-review
DROP_POST_REFUSED = "post-refused"  # GitHub keeps rejecting this verdict post (issue #78)

# Our own reviews could not be read this pass (issue #71). Distinct from every other
# hold because it says nothing about the PR — only that we are blind — and blind is
# precisely when acting is most expensive. Emitted rather than folded into a
# neighbouring hold so a degradation is VISIBLE in telemetry: the 2026-08-17 loop ran
# 2.5h without a single log line naming the read failure that caused it.
HOLD_REVIEWS_UNREADABLE = "hold:reviews-unreadable"

# Posting a verdict is the LAST step of the most expensive thing this plugin does, and
# it used to be the only step with no retry — a transient refusal discarded the whole
# panel run (issue #72). Bounded, because #6: a refusal GitHub means must not re-attempt
# forever. Backoff is per-retry; the tuple's last value repeats if attempts ever grow.
POST_MAX_ATTEMPTS = 3
POST_RETRY_BACKOFF_S = (2, 8)

# Consecutive NON-transient post refusals on one repo#pr@head before the panel stops
# being spent on it (issue #78). Retry (above) handles a GitHub blip; this handles a
# GitHub *decision* — a locked conversation, a body it will not accept — where the
# review runs in full, is refused, is discarded, and the sweep backfills it again on
# the next tick, forever. Transient failures deliberately do NOT count: a degradation
# must not latch a PR out of review. A new head is a new key, so a real fix re-enters,
# and it is in-memory, so a restart grants one more look.
POST_MAX_FAILURES = 2

# gh exit codes / stderr shapes worth a second attempt. 5xx and the secondary
# rate-limit texts are the ones observed in production; a 4xx other than 429 is GitHub
# rejecting the REQUEST, and retrying an unchanged request cannot fix it.
_TRANSIENT_GH_TEXT = (
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "no server is currently available",
    "secondary rate limit",
    "api rate limit exceeded",
    "was submitted too quickly",
    "timed out",
    "connection reset",
    "eof occurred",
)


def ineligible_reason(facts: dict | None) -> str | None:
    """Why this PR cannot be reviewed, or None if it can.

    Four conditions used to collapse into one opaque `pr-not-eligible`, and one of
    them (`locked`) skipped silently in the sweep with no telemetry at all — so
    "ignored because the conversation is locked" was indistinguishable from "never
    seen". That is the same invisibility that let a duplicate-review loop run 2.5h
    and a 422 loop run all evening: the system knew, and never said.
    """
    if not facts:
        return "facts-unreadable"
    if facts.get("state") != "open":
        return "not-open"
    if facts.get("draft"):
        return "draft"
    if facts.get("locked"):
        return "locked"  # GitHub refuses reviews here: `422 lock prevents review`
    return None


def _with_api_detail(err: str, out: str) -> str:
    """Fold GitHub's own error text (on stdout) into `gh`'s terse stderr line.

    A failed `gh api` prints its status to stderr and the API's JSON body to stdout.
    The body is where the reason lives — `{"message": "...", "errors": [...]}` — so a
    log built from stderr alone says "Unprocessable Entity (HTTP 422)" and nothing an
    operator can act on. Best-effort: a non-JSON body is appended verbatim, truncated.
    """
    text = (out or "").strip()
    if not text:
        return err
    detail = ""
    try:
        payload = json.loads(text)
    except ValueError:
        detail = text[:200]
    else:
        if isinstance(payload, dict):
            parts = [str(payload.get("message") or "").strip()]
            errors = payload.get("errors")
            if isinstance(errors, list):
                # entries are either strings or {resource, field, code} objects
                parts += [e if isinstance(e, str) else json.dumps(e) for e in errors[:3]]
            detail = " — ".join(p for p in parts if p)[:300]
    return f"{err} :: {detail}" if detail else err


def transient_gh_failure(rc: int, err: str) -> bool:
    """Is this `gh` failure worth retrying? Timeout (124) always; else by stderr shape."""
    if rc == 124:  # our own timeout kill
        return True
    if rc == 127:  # gh missing — no amount of retrying installs it
        return False
    return any(t in (err or "").lower() for t in _TRANSIENT_GH_TEXT)


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

# How long an enumerated GitHub App installation scope is reused before re-reading it.
# The sweep ticks every ~3 min and installation membership changes rarely, so this
# keeps the extra call to roughly one per TTL while a newly-installed repo still comes
# under review within a few minutes.
INSTALLATION_REPOS_TTL_S = 600

# Marks our "the panel could not finish" comments (#61) so they can be deduplicated when
# posted AND found again when a later verdict makes them false. Shared by both sides on
# purpose: two copies of this string is how a notice gets posted but never cleared.
EXHAUSTION_MARKER = "protoagent-qa-exhausted"

_NON_TERMINAL = {"queued", "in_progress", "waiting", "requested", "pending"}
_GREEN = {"success", "neutral", "skipped"}


def gh_json_rows(out: str) -> list | None:
    """Parse `gh api --paginate --jq '.[] | …'` output → rows, or None if unparseable.

    `--paginate` applies the jq filter PER PAGE and concatenates the results, so an
    array-wrapping filter (`[.[] | …]`) emits `[…][…]` on the second page — not valid
    JSON, and every such read silently broke the moment a PR crossed 30 items
    (issue #75). Emitting one object per line instead is pagination-safe by
    construction: `gh` prints compact JSON, so embedded newlines stay escaped and one
    row really is one line.

    A single unparseable line makes the WHOLE read None rather than a short list.
    These rows drive "has this been reviewed", "did an operator pause this" and "are
    the checks green" — a silently-short answer is the failure mode of #71, where a
    partial read was indistinguishable from an absence.

    Still accepts a whole-array body, so a caller that drops --jq (or a fake that
    returns one array) keeps working.
    """
    text = (out or "").strip()
    if not text:
        return []
    try:
        whole = json.loads(text)
    except ValueError:
        pass
    else:
        return whole if isinstance(whole, list) else [whole]
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            return None  # a partial read is worse than none — it looks complete
    return rows


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
        # On-demand summon surface (issue #28). Off disables the comment commands
        # entirely, including the pause check — a repo that never wants comment-driven
        # behaviour pays nothing for it.
        self.summon_enabled = (
            bool(self.cfg["summon"]) if "summon" in self.cfg else _env_bool("PR_REVIEWER_SUMMON", True)
        )
        self.chokepoint = Chokepoint(cooldown_s=int(self._cfg.get("cooldown_s") or 30))
        self._run_gh = run_gh_fn or run_gh
        self._workflow_run = workflow_run  # None → resolve STATE.workflow_run lazily
        self._inbox_add = inbox_add  # None → resolve STATE.inbox_store lazily
        self._viewer: str | None = None
        self._promote_failures: dict[str, int] = {}  # repo#pr@head -> consecutive APPROVE failures
        self._regate_failures: dict[str, int] = {}  # repo#pr@head -> consecutive REQUEST_CHANGES failures
        self._post_failures: dict[str, int] = {}  # repo#pr@head -> NON-transient verdict-post refusals
        self._viewer_checked = False  # viewer_login vs our reviews' real author, warned once
        self._round_cap: dict[str, float] = {}  # repo#pr -> monotonic timestamp when capped
        self._installation_repos: list[str] = []  # last good App-installation scope
        self._installation_repos_at: float = 0.0

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
        """Managed allowlist, or EMPTY meaning "whatever the GitHub App installation
        covers" — GitHub is the gate, not a second list maintained here.

        Empty has always meant allow-all on the webhook path (`self.repos and ...`
        below), but the sweep iterated this list literally, so an empty list silently
        turned off backfill, re-gate and promotion — the level-triggered half of the
        reviewer — while the edge-triggered half kept working. `sweep_repos()` closes
        that: it resolves the installation's own repositories when this is empty.

        Config wins only when NON-empty, so a seed shipping `repos: []` still falls
        through to the env — meaning "empty = the installation's scope" holds only when
        PR_REVIEWER_REPOS is ALSO unset. A deployment that wants installation-wide scope
        has to clear both; setting either one narrows to it.
        """
        return [str(r) for r in (self.cfg.get("repos") or []) if r] or _env_repos()

    async def sweep_repos(self) -> list[str]:
        """The repos the sweep should walk: the explicit allowlist when one is set,
        else every repo the App installation can see.

        Installing the App on a repo is already an explicit, revocable, audited grant.
        Requiring a second enumeration here meant every new repo silently got
        "webhooks delivered, nothing reviewed" until someone remembered to add it —
        four repos were sitting in exactly that state (qaEngineer#38).

        Cached: the sweep runs every ~3 min and installation membership changes rarely,
        so this is roughly one extra API call per TTL. A failed enumeration reuses the
        last good list and, failing that, returns empty — the sweep skips a pass rather
        than inventing scope.
        """
        explicit = self.repos
        if explicit:
            return explicit
        now = time.monotonic()
        # Freshness is the TIMESTAMP, not the contents. Gating the cache hit on a
        # non-empty list makes an authoritatively-empty scope re-enumerate every tick —
        # the same conflation of "empty" with "absent" as below, one line up.
        if self._installation_repos_at and now - self._installation_repos_at < INSTALLATION_REPOS_TTL_S:
            return self._installation_repos
        # NOTE: no `[...]` wrapper in the jq — with --paginate, gh applies the filter
        # per page and concatenates, so an array-wrapping filter emits `[..][..]`,
        # which is not valid JSON. Newline-separated scalars concatenate cleanly.
        rc, out, err = await self._run_gh(
            ["api", "/installation/repositories", "--paginate", "--jq", ".repositories[].full_name"]
        )
        if rc != 0:
            log.warning(
                "[pr-reviewer] could not enumerate installation repositories (%s); sweeping the last known %d repo(s)",
                err[-200:],
                len(self._installation_repos),
            )
            return self._installation_repos
        # rc == 0 is AUTHORITATIVE, including when it lists nothing: "the App is
        # installed nowhere" is a real answer, not a failure. Treating empty as a
        # failure and reusing the cache meant an uninstalled repo kept being swept
        # for as long as the process lived — the uninstall silently did nothing.
        found = [line.strip() for line in out.splitlines() if line.strip()]
        if set(found) != set(self._installation_repos):
            log.info("[pr-reviewer] installation scope: %d repo(s)", len(found))
        if not found:
            log.warning("[pr-reviewer] installation covers NO repositories — the sweep has nothing to do")
        self._installation_repos, self._installation_repos_at = found, now
        return found

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

    @property
    def max_rounds(self) -> int:
        """Maximum push-triggered panel reviews before the cap suppresses further pushes.
        Zero disables the cap."""
        cfg = self.cfg
        return int(cfg["max_rounds"]) if "max_rounds" in cfg else _env_int("PR_REVIEWER_MAX_ROUNDS", 6)

    @property
    def max_rounds_cooldown_s(self) -> int:
        """Seconds before the max-rounds cap resets without an explicit trigger."""
        cfg = self.cfg
        return (
            int(cfg["max_rounds_cooldown"])
            if "max_rounds_cooldown" in cfg
            else _env_int("PR_REVIEWER_MAX_ROUNDS_COOLDOWN", 7200)
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

    async def _escalate(
        self,
        text: str,
        dedup_key: str,
        *,
        repo: str | None = None,
        pr: int | None = None,
        head_sha: str | None = None,
    ) -> None:
        """Operator escalation — inbox when the host offers one, always telemetry.

        When repo/pr/head_sha are given (panel-exhaustion path), also posts a visible
        comment on the PR — fail-open and deduplicated by head SHA.
        """
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
        if repo and pr is not None and head_sha:
            await self._post_exhaustion_comment(repo, pr, head_sha)

    async def _post_exhaustion_comment(self, repo: str, pr: int, head_sha: str) -> None:
        """Post a visible exhaustion notice on the PR — fail-open, deduplicated by head SHA."""
        marker = f"<!-- {EXHAUSTION_MARKER} head={head_sha} -->"
        rc, out, _err = await self._run_gh(
            # `| tojson` because a BARE string prints raw, so a body containing
            # newlines spans several lines and stops being one-row-per-line. Objects
            # already print compact; only scalars need the encode.
            ["api", f"repos/{repo}/issues/{pr}/comments", "--paginate", "--jq", ".[] | .body | tojson"]
        )
        if rc == 0:
            bodies = gh_json_rows(out)
            if bodies is not None and any(marker in str(b or "") for b in bodies):
                return
            # unreadable → post anyway (fail-open: a duplicate notice beats silence)
        retries = self.panel_retries + 1
        body = (
            f"⚠️ **QA panel exhausted** — this PR has not been reviewed.\n"
            f"The review panel failed after {retries} attempt(s) on head `{head_sha[:12]}`. No verdict was posted.\n"
            f"A new push will re-trigger the review.\n"
            f"{marker}"
        )
        rc, _out, err = await self._run_gh(
            ["api", f"repos/{repo}/issues/{pr}/comments", "-X", "POST", "-f", f"body={body}"],
            timeout=60,
        )
        if rc != 0:
            log.warning("[pr-reviewer] exhaustion comment on %s#%s failed: %s", repo, pr, err[-300:])

    async def _post_max_rounds_comment(self, repo: str, pr: int) -> None:
        """Post a visible notice that the push-triggered review cap has been hit — fail-open."""
        cooldown_h = self.max_rounds_cooldown_s // 3600
        body = (
            f"🛑 **Review cap reached** — this PR has had {self.max_rounds} automated review "
            f"round(s). Further pushes will not trigger new reviews.\n\n"
            f"The cap resets when:\n"
            f"- The PR is marked **ready-for-review**\n"
            f"- An operator runs `@vera review` (manual summon)\n"
            f"- {cooldown_h} hour(s) elapse since the cap was hit\n\n"
            f"<!-- protoagent-qa-max-rounds repo={repo} pr={pr} -->"
        )
        rc, _out, err = await self._run_gh(
            ["api", f"repos/{repo}/issues/{pr}/comments", "-X", "POST", "-f", f"body={body}"],
            timeout=60,
        )
        if rc != 0:
            log.warning("[pr-reviewer] max-rounds comment on %s#%s failed: %s", repo, pr, err[-300:])

    def _check_viewer_matches(self, author: str) -> None:
        """Warn once if `viewer_login` disagrees with who actually posted our reviews.

        `viewer_login` is unvalidated by construction — it is a string an operator
        typed. A typo does not error; it silently makes the self-authored comparison
        never match, which is precisely the disabled rail this config exists to
        prevent. But a marker-bearing review is proof of identity: only we write that
        marker, so its author IS us. Comparing the two costs nothing (the rows are
        already fetched) and turns a silent misconfiguration into one loud line.

        Warn, never correct: an operator who set this deliberately (a migration, a
        renamed app) should not have the reviewer quietly overrule them, and being
        wrong in the SAFE direction — comparing against a login that never matches —
        only ever causes extra review, not self-approval.
        """
        author = (author or "").strip().lower()
        if not author or self._viewer_checked or not self._viewer:
            return
        self._viewer_checked = True
        if author.removesuffix("[bot]") != self._viewer.removesuffix("[bot]"):
            log.warning(
                "[pr-reviewer] viewer_login is %r but our own posted reviews are authored by %r — "
                "the never-review-your-own-PR rail will not fire. Fix pr_reviewer.viewer_login.",
                self._viewer,
                author,
            )
            self.telemetry.emit("viewer-mismatch", configured=self._viewer, actual=author)

    async def _viewer_login(self) -> str:
        """Our own login, cached — ONLY on success.

        Caching `""` on a failed lookup and gating the retry on `is None` meant one
        transient failure permanently disabled the never-review-your-own-PR rail for
        the life of the process: `viewer` stays empty, the self-authored comparison
        never matches, and the reviewer reviews (and, as promotion owner, can
        approve-on-green) its own PRs. Same shape as issue #71 — a failed read cached
        as a definitive answer — so it fails the same way: retry, don't remember it.

        `GET /user` is the WRONG source under GitHub App auth and cannot be made to
        work: an installation token authenticates an installation, not a user, so the
        call 403s every time. A deployment on App auth therefore has no discoverable
        identity, and once the guard started failing closed on that (correctly — it
        cannot rule out self-review) it dropped a large share of its own reviews.
        `viewer_login` is the answer: the App's bot login (`<app-slug>[bot]`) is a
        deployment fact the operator already knows, so it is configured, not probed.
        """
        if not self._viewer:
            # Configured identity first — reliable, and the only option on App auth.
            configured = str(self.cfg.get("viewer_login") or os.environ.get("PR_REVIEWER_VIEWER_LOGIN") or "").strip()
            if configured:
                self._viewer = configured.lower()
                return self._viewer
            rc, out, _err = await self._run_gh(["api", "user", "--jq", ".login"])
            if rc == 0 and out.strip():
                self._viewer = out.strip().lower()
            else:
                return ""  # unknown this pass; the self-authored guard fails CLOSED below
        return self._viewer

    # ── facts (all server-side) ───────────────────────────────────────────────

    async def _pr_facts(self, repo: str, pr: int) -> dict | None:
        rc, out, _err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/pulls/{pr}",
                "--jq",
                "{head: .head.sha, base_ref: .base.ref, state: .state, draft: .draft, locked: .locked, "
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

    async def _our_reviews(self, repo: str, pr: int) -> list[dict] | None:
        """Our posted reviews (marker-bearing), oldest→newest: [{head, verdict, promoted, state, body, id}].

        **None means the read FAILED — it is not the same as `[]` (this PR has no
        reviews), and callers must not collapse the two** (issue #71). Returning `[]`
        on an unreadable read is a fail-OPEN: `needs_backfill` reads it as "no verdict
        exists" and re-reviews a PR that is already reviewed, which cost two PRs ten
        full panels each on a static head during a GitHub degradation. Every caller
        here decides explicitly, and every one of them fails CLOSED.
        """
        rc, out, _err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/pulls/{pr}/reviews",
                "--paginate",
                "--jq",
                # One object per line, NOT `[.[] | …]`: with --paginate the wrapper
                # emits `[…][…]` past 30 reviews and this read — the one everything
                # about review history depends on — went permanently unreadable (#75).
                ".[] | {id: .id, state: .state, body: .body, author: .user.login}",
            ],
        )
        if rc != 0:
            return None
        rows = gh_json_rows(out)
        if rows is None:
            return None
        ours = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            marker = parse_verdict_marker(row.get("body") or "")
            if marker:
                self._check_viewer_matches(str(row.get("author") or ""))
                ours.append(
                    {**marker, "state": row.get("state", ""), "body": row.get("body") or "", "id": row.get("id")}
                )
        return ours

    async def _pr_comments(self, repo: str, pr: int) -> list[str]:
        """This PR's issue-comment bodies, oldest→newest — where pause markers live.

        The ONE read here that deliberately fails OPEN, so it is worth being exact
        about what that means rather than leaving it looking like the oversight the
        rest of this file just finished fixing: unreadable ⇒ `[]` ⇒ `is_paused()` is
        False ⇒ we review. During a comments-read failure we can therefore review a PR
        an operator asked quiet for (#28).

        That is the better of two bad options. The alternative — refusing to review
        whenever this read fails — turns any GitHub degradation into a full review
        outage, while the cost here is one unwanted review on one PR: annoying,
        visible, and immediately recoverable by re-pausing. Truncation is handled
        separately: a PARTIAL list could silently drop a late `@vera pause`, so
        `gh_json_rows` returns None on a bad line and that lands here as empty too.
        """
        rc, out, _err = await self._run_gh(
            ["api", f"repos/{repo}/issues/{pr}/comments", "--paginate", "--jq", ".[] | .body | tojson"]
        )
        if rc != 0:
            return []
        rows = gh_json_rows(out)
        return [str(b or "") for b in rows] if rows is not None else []

    async def _finding_sources(
        self, repo: str, pr: int, head: str, findings: list[dict]
    ) -> dict[str, tuple[str, str | None]]:
        """{file: (blob, combined)} for the files the findings cite.

        ``blob`` is the raw file at the reviewed head — used for line-number correction.
        ``combined`` is ``blob + patch`` — used for grounding existence checks (a
        removed-behaviour finding legitimately quotes code the head no longer has).
        An unreadable file gives ``("", None)`` — fail open, never downgrade on a
        failed read (the ``confine_findings`` posture).
        """
        patches: dict[str, str] = {}
        rc, out, _err = await self._run_gh(
            ["api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--jq", ".[] | {f: .filename, p: .patch}"]
        )
        if rc == 0:
            # A PR touching 30+ files paginates, and this feeds grounding: a missing
            # patch makes a removed-behaviour finding look unquotable and get
            # downgraded. Fail open (empty patches) exactly as before, never partial.
            for row in gh_json_rows(out) or []:
                if isinstance(row, dict) and row.get("f"):
                    patches[str(row["f"])] = str(row.get("p") or "")
        sources: dict[str, tuple[str, str | None]] = {}
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
            combined = f"{blob}\n{patch}" if (blob or patch) else None
            sources[file] = (blob, combined)
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
                # Not `[…]`: a commit with 30+ check runs paginates, and a truncated
                # read here decides PROMOTION — dropping the one pending or failed run
                # on page 2 reads as all-green (#75).
                ".check_runs[] | {status: .status, conclusion: .conclusion}",
            ],
        )
        if rc != 0:
            return None
        runs = gh_json_rows(out)
        if runs is None:
            return None  # unreadable ⇒ checks-unknown, which holds promotion
        if not runs:
            return "no-checks"
        if any(r.get("status") in _NON_TERMINAL for r in runs):
            return "pending"
        if all((r.get("conclusion") or "") in _GREEN for r in runs):
            return "green"
        return "failed"

    async def _unresolved_threads(self, repo: str, pr: int) -> int | None:
        """Unresolved-thread count for the promotion gate — paginated in threads.py.

        This is the query the gate reads. `fetch_threads` (the panel's context block)
        is a DIFFERENT query that was truncated the same way; paginating that one alone
        looks like a fix and leaves the gate still counting only the first hundred.
        """
        from .threads import count_unresolved_threads

        return await count_unresolved_threads(self._run_gh, repo, pr)

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
        # A draft→ready conversion resets the max-rounds cap before any other gate so
        # the fresh-open review proceeds even if the PR was flood-capped while in draft.
        if action == "ready_for_review":
            self._round_cap.pop(f"{repo}#{pr}", None)
        if action not in DISPATCH_ACTIONS:
            self.telemetry.emit("drop", repo=repo, pr=pr, reason="not-a-dispatch-action", action=action)
            return "drop:not-a-dispatch-action"
        if bad_repo(repo) or (self.repos and repo not in self.repos):
            # The allowlist gate runs BEFORE any GitHub call — an unmanaged repo must
            # not trigger PR lookups on our credentials (Quinn's gate ordering).
            self.telemetry.emit("drop", repo=repo, pr=pr, reason="unlisted-repo")
            return "drop:unlisted-repo"
        # Max-rounds cap: suppress push-triggered reviews after the limit, before the
        # chokepoint so the in-flight slot is never consumed for a capped drop.
        cap_key = f"{repo}#{pr}"
        if cap_key in self._round_cap:
            cap_age = time.monotonic() - self._round_cap[cap_key]
            if cap_age < self.max_rounds_cooldown_s:
                self.telemetry.emit("drop", repo=repo, pr=pr, sha=head_sha, reason="max-rounds-capped")
                return "drop:max-rounds-capped"
            del self._round_cap[cap_key]  # cooldown elapsed — let the next push through
        decision = self.chokepoint.admit(repo, pr, head_sha)
        if decision != "accept":
            self.telemetry.emit("drop", repo=repo, pr=pr, sha=head_sha, reason=decision)
            return f"drop:{decision}"
        try:
            return await self._review(repo, pr, push_triggered=True)
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
        # A manual summon resets the max-rounds cap — "I think you got this wrong" must
        # override the flood guard, and the operator has implicitly acknowledged the cost.
        self._round_cap.pop(f"{repo}#{pr}", None)
        try:
            return await self._review(repo, pr, force=True)
        finally:
            self.chokepoint.done(repo, pr)

    async def _review(self, repo: str, pr: int, *, force: bool = False, push_triggered: bool = False) -> str:
        started = time.monotonic()
        facts = await self._pr_facts(repo, pr)
        # `locked` matters as much as closed/draft: GitHub refuses a review on a locked
        # conversation with `422 lock prevents review`, so the panel would run in full
        # and the verdict be discarded at the post. Two dependabot PRs auto-locked this
        # way burned a panel every ~7 minutes indefinitely (issue #78) — the sweep saw
        # no verdict, backfilled, and posted into the same lock forever. Cheapest fix is
        # not to start: never review what cannot receive a review.
        why = ineligible_reason(facts)
        if why:
            # `why` alongside the reason: four different conditions used to arrive here
            # as one opaque `pr-not-eligible`, so a PR being skipped told you nothing
            # about whether that was correct.
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_PR_NOT_ELIGIBLE, why=why)
            return f"drop:{DROP_PR_NOT_ELIGIBLE}"
        viewer = await self._viewer_login()
        author = str(facts.get("author") or "").lower()
        allow_self = bool(self.cfg.get("allow_self_review", False))
        if not viewer and not allow_self:
            # We do not know who we are, so we cannot rule out that this PR is ours.
            # Reviewing it risks self-review — and, as promotion owner, self-approval.
            # Skipping costs one pass, which the sweep retries with a fresh lookup.
            # LOUD, because on App auth this is not transient: `gh api user` can never
            # succeed, so every eligible PR drops here until `viewer_login` is set.
            log.warning(
                "[pr-reviewer] identity unknown — dropping %s#%s rather than risk self-review. "
                "On GitHub App auth set pr_reviewer.viewer_login (or PR_REVIEWER_VIEWER_LOGIN) "
                "to the app's bot login, e.g. 'myapp[bot]'.",
                repo,
                pr,
            )
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_VIEWER_UNKNOWN)
            return f"drop:{DROP_VIEWER_UNKNOWN}"
        if (
            viewer
            and not allow_self
            and (author == viewer or author.removesuffix("[bot]") == viewer.removesuffix("[bot]"))
        ):
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_SELF_AUTHORED, author=author)
            return f"drop:{DROP_SELF_AUTHORED}"

        head = str(facts["head"])
        # GitHub has already refused this exact verdict post more than once for a reason
        # that will not change on its own (issue #78). Running the panel again produces a
        # verdict with nowhere to go. `force` (an operator summon) overrides — asking
        # explicitly is a reason to try once more.
        if not force and self._post_failures.get(f"{repo}#{pr}@{head}", 0) >= POST_MAX_FAILURES:
            self.telemetry.emit("drop", repo=repo, pr=pr, sha=head, reason=DROP_POST_REFUSED)
            return f"drop:{DROP_POST_REFUSED}"
        if not force and self.summon_enabled:
            # An operator asked for quiet (issue #28). Push-triggered review stops; an
            # explicit `@vera review` still runs, because "stop reviewing every push" and
            # "never look at this again" are different requests.
            from .summon import is_paused

            if is_paused(await self._pr_comments(repo, pr)):
                self.telemetry.emit("drop", repo=repo, pr=pr, sha=head, reason=DROP_PAUSED)
                return f"drop:{DROP_PAUSED}"
        paths = await self._changed_paths(repo, pr)
        fires, reasons = structural_trigger(
            changed_files=int(facts.get("changed_files") or len(paths)),
            lines_changed=int(facts.get("additions") or 0) + int(facts.get("deletions") or 0),
            changed_paths=paths,
        )
        recipe = "code-review-structural" if fires else "code-review"

        ours = await self._our_reviews(repo, pr)
        if ours is None:
            # Blind on our own history, and EVERY downstream decision here reads it:
            # the reaffirm short-circuit below sees no verdict at this head and spends
            # the panel again, and `round_number` resets to 1 so the max-rounds cap and
            # the convergence rule both disarm. That combination is what posted ten
            # CHANGES_REQUESTED on one static head (issue #71). Drop instead — the
            # sweep's backfill re-reaches this PR once the read recovers.
            self.telemetry.emit("drop", repo=repo, pr=pr, sha=head, reason=DROP_REVIEWS_UNREADABLE)
            return f"drop:{DROP_REVIEWS_UNREADABLE}"
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
        # Max-rounds cap: arm on the first push-triggered review that exceeds the limit so
        # the panel is never spent — post the notice once, then drop all subsequent pushes.
        # Backfill and summon calls pass push_triggered=False and are always exempt.
        if push_triggered and self.max_rounds and round_number > self.max_rounds:
            cap_key = f"{repo}#{pr}"
            self._round_cap[cap_key] = time.monotonic()
            if len(self._round_cap) > 1024:
                self._round_cap = dict(list(self._round_cap.items())[-512:])
            await self._post_max_rounds_comment(repo, pr)
            self.telemetry.emit("drop", repo=repo, pr=pr, sha=head, reason="max-rounds-capped", round=round_number)
            return "drop:max-rounds-capped"
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
                await self._escalate(
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
            await self._escalate(
                f"pr-reviewer: panel step(s) {failed} failed on {repo}#{pr} "
                f"after {self.panel_retries + 1} attempt(s) — no verdict posted; PR is UNREVIEWED.",
                dedup_key=f"pr-reviewer-exhaustion:{repo}#{pr}@{head[:7]}",
                repo=repo,
                pr=pr,
                head_sha=head,
            )
            self.telemetry.emit(
                "exhaustion", repo=repo, pr=pr, sha=head, failed=failed, attempts=self.panel_retries + 1
            )
            return "error:panel-exhausted"

        # Per-step timings (protoAgent's engine, additive — {} on an older host). The
        # panel's cost is nine LLM steps and a single `latency_s` cannot say which one to
        # attack; this is what turns "the panel is slow" into a step name.
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        # Steps the engine cut off at their opt-in `timeout` and degraded to an empty Gap
        # (a slow finder, not a crash — see the recipe's finder `timeout`). Additive: an
        # engine without the feature omits the key, so this is [] on an older host. A
        # degraded finder means the panel reviewed with one fewer angle this round; that
        # is surfaced (telemetry + body note) so it is never a silent gap in coverage.
        degraded = [str(s) for s in (result.get("degraded") or [])]
        # Panel completeness (#49): did every finder meant to run actually run? The
        # structural finder degrades to a `PROTOPATCH UNAVAILABLE` Gap on a gateway/clone
        # failure WITHOUT failing the step, so it's invisible to `failed`/`degraded` — read
        # the raw step output for the token. A degraded (timed-out) finder also leaves a
        # coverage hole. An incomplete panel still POSTS its verdict, but the marker records
        # `complete=false` so the promotion gate refuses to auto-approve a clean-looking
        # verdict that was produced over code a finder never examined.
        steps_out = result.get("steps") if isinstance(result.get("steps"), dict) else {}
        structural_unavailable = UNAVAILABLE_PREFIX in str(steps_out.get("find_structural") or "")
        complete = not structural_unavailable and not degraded
        output = str(result.get("output") or "")
        # The raw output is read for BLOCKS and never published as text (protoAgent#2439
        # — see verdicts.py). `reported` is what the panel said this round and what the
        # body records; `findings` is the confined subset the verdict is computed from.
        reported = self._parse_findings(output)
        brief, brief_found = extract_brief(output)
        truncated = report_hard_stopped(output)
        findings, confined = confine_findings(reported, paths)
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
            raw = await self._finding_sources(repo, pr, head, findings)
            blobs = {f: v[0] for f, v in raw.items()}
            grounding_sources = {f: v[1] for f, v in raw.items()}
            grounded_findings, ungrounded = apply_grounding(findings, grounding_sources)
            grounding_checked = len(findings)
            findings = grounded_findings
            findings = correct_line_numbers(findings, blobs)
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
        # A `fixed` disposition is only honoured if the flagged line actually moved
        # (issue: protoAgent#2208 shipped a major to main on a hallucinated "fixed" that
        # left the line byte-identical). We need the delta prior-head→head to verify that;
        # compute it here when there are dispositions and it wasn't already computed for
        # convergence. Fail-closed: an unreadable delta means `fixed` can't be verified,
        # so it isn't trusted.
        if dispositions and ranges is None and prior:
            ranges = await self._delta_ranges(repo, prior["head"], head)
        unaccounted = unaccounted_priors(history, dispositions, ranges=ranges)
        # The two guards are a fallback chain, not a belt-and-braces pair. When the panel
        # HAS dispositioned its priors, that statement is the authority — re-applying the
        # clean-PASS heuristic on top would hold a block the panel just explained, making
        # the explicit contract worthless exactly where it matters most.
        dropped_finding = (
            unexplained_clearance(history, verdict, findings) if (self.hold_unexplained and not dispositions) else None
        )
        trailer = render_notes_section(notes) + render_grounding_footnote(ungrounded)
        if degraded:
            trailer += render_degraded_note(degraded)
        if unaccounted:
            trailer += render_unaccounted_note(unaccounted)
            # Write the recovered majors into the recorded findings, not just the prose
            # trailer — else `panel_rounds` rebuilds this round from the (de-escalated) array
            # and the next round launders the debt away (protoAgent#2283 r3). The carry
            # propagates until a verified `fixed`/`refuted` clears it from `unaccounted`.
            # It lands in `reported` (what the body records), NOT in `findings`: this
            # round's verdict is already computed, and the carry gates via the NEXT round's
            # recall — injecting it here would silently re-decide the verdict.
            reported = merge_carried_findings(reported, unaccounted)
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
            reported,
            recipe,
            brief=brief,
            brief_found=brief_found,
            dispositions=dispositions,
            truncated=truncated,
            confined=confined,
            notes=trailer,
            hold_blocks=bool(dropped_finding) or bool(unaccounted),
            complete=complete,
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
            # Did the report step delimit a brief? False means the body shipped with an
            # explicit "brief could not be read" instead — the review still lands, but a
            # rising count here is the panel drifting off its output contract (#2439).
            brief_found=brief_found,
            report_truncated=truncated or None,
            grounding_checked=grounding_checked,
            grounding_downgraded=len(ungrounded),
            dispositions=len(dispositions),
            unaccounted=len(unaccounted),
            latency_s=round(elapsed, 1),
            step_s=timings or None,
            slowest_step=(max(timings, key=timings.get) if timings else None),
            degraded=degraded or None,
            complete=complete,
            structural_unavailable=structural_unavailable or None,
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
        findings: list[dict],
        recipe: str,
        brief: str = "",
        brief_found: bool = True,
        dispositions: list[dict] | None = None,
        truncated: bool = False,
        confined: list[dict] | None = None,
        notes: str = "",
        hold_blocks: bool = False,
        complete: bool = True,
    ) -> bool:
        body = render_verdict_body(
            repo=repo,
            pr=pr,
            head_sha=head,
            verdict=verdict,
            findings=findings,
            brief=brief,
            brief_found=brief_found,
            dispositions=dispositions,
            truncated=truncated,
            shadow=self.shadow,
            recipe=recipe,
            confined=confined,
            notes=notes,
            complete=complete,
        )
        event = "COMMENT"
        if not self.shadow and verdict == FAIL:
            # A blocking verdict only against terminal CI (#863) — else comment now;
            # the next push/sweep re-evaluates.
            checks = await self._checks_state(repo, head)
            event = "REQUEST_CHANGES" if checks in ("green", "failed", "no-checks") else "COMMENT"
        rc, err, attempts = await self._post_review_with_retry(repo, pr, head, event, body)
        if rc != 0:
            # The verdict is GONE — the panel ran, the judgement was made and paid for,
            # and the only copy of it was in this call frame (issue #72). ERROR, not
            # WARNING: a discarded verdict evaded every `grep ERROR` health check we had,
            # which is how three of them went unnoticed for hours on 2026-08-17.
            # `attempts` is what actually happened, not POST_MAX_ATTEMPTS — a 422 gives
            # up after one, and an alert that rounds that up to "3 attempts" is telling
            # the operator a story about a retry storm that never occurred.
            log.error(
                "[pr-reviewer] VERDICT LOST — posting %s on %s#%s failed after %d attempt(s): %s",
                event,
                repo,
                pr,
                attempts,
                err[-300:],
            )
            # `review_event`, not `event`: Telemetry.emit takes the event NAME first.
            self.telemetry.emit(
                "verdict-lost",
                repo=repo,
                pr=pr,
                sha=head,
                verdict=verdict,
                review_event=event,
                attempts=attempts,
                error=err[-300:],
            )
            await self._escalate(
                f"QA verdict LOST on {repo}#{pr} — the panel produced {verdict} on head "
                f"{head[:12]} and GitHub refused the post after {attempts} attempt(s). "
                f"The PR shows no verdict. Last error: {err[-200:]}",
                dedup_key=f"verdict-lost:{repo}#{pr}@{head}",
            )
            # Count only a refusal GitHub MEANS. A transient failure already exhausted
            # its retries above, and latching a PR out of review over a blip would turn
            # a degradation into a lasting gap.
            if not transient_gh_failure(rc, err):
                key = f"{repo}#{pr}@{head}"
                self._post_failures[key] = self._post_failures.get(key, 0) + 1
                if len(self._post_failures) > 1024:  # bounded, like the promote counter
                    self._post_failures = dict(list(self._post_failures.items())[-512:])
            return False
        self._post_failures.pop(f"{repo}#{pr}@{head}", None)
        # A verdict exists now, so any "this PR has not been reviewed" notice we left on
        # an earlier exhausted head (#61) is stale and contradicts the review above it.
        await self._clear_exhaustion_comments(repo, pr)
        if not self.shadow and verdict != FAIL and not hold_blocks:
            # A cleared verdict must also LIFT our earlier block: PASS/WARN post as
            # COMMENT, and a comment never supersedes the same reviewer's REQUEST_CHANGES.
            # `hold_blocks` is the one exception — a clean PASS that silently dropped a
            # prior blocker/major has not earned the dismissal yet (issue #26).
            await self._dismiss_stale_blocks(repo, pr)
        return True

    async def _post_review_with_retry(
        self, repo: str, pr: int, head: str, event: str, body: str
    ) -> tuple[int, str, int]:
        """POST the verdict review, retrying TRANSIENT refusals → (rc, last error, attempts).

        Bounded on purpose (issue #6: a promotion GitHub keeps refusing must not
        re-attempt forever). Only transient classes are retried — a 422 is GitHub
        telling us the request is wrong, and sleeping changes nothing.

        A review POST is NOT idempotent, so a retry is only safe when the previous
        attempt definitely did not land. A timeout is the one failure where that is
        unknown — we killed the call, GitHub may still have accepted it — so a timeout
        re-reads the PR before re-sending. Without that check the retry turns a
        timed-out-but-landed post into a duplicate review, which is the very pathology
        this release exists to stop. `while True` rather than a bounded `for`: every
        exit is an explicit return, so there is no unreachable tail.
        """
        attempt = 0
        while True:
            attempt += 1
            rc, out, err = await self._run_gh(
                ["api", f"repos/{repo}/pulls/{pr}/reviews", "-X", "POST", "-f", f"event={event}", "-f", f"body={body}"],
                timeout=60,
            )
            if rc == 0:
                if attempt > 1:
                    log.info("[pr-reviewer] posting %s on %s#%s succeeded on attempt %d", event, repo, pr, attempt)
                return 0, "", attempt
            # `gh` puts "Unprocessable Entity (HTTP 422)" on stderr and GitHub's actual
            # reason in the JSON on STDOUT, so logging stderr alone records that
            # something was refused and never why. `lock prevents review` sat in that
            # body unread while two PRs lost a verdict every 7 minutes (#78).
            err = _with_api_detail(err, out)
            if rc == 124:
                # Ambiguous: our timeout, not GitHub's refusal. Ask GitHub what it has.
                landed = await self._verdict_landed(repo, pr, head)
                if landed is True:
                    log.warning(
                        "[pr-reviewer] posting %s on %s#%s timed out but LANDED — not re-sending", event, repo, pr
                    )
                    return 0, "", attempt
                if landed is None:
                    # Cannot confirm either way. Re-sending risks a duplicate review,
                    # which is permanent; giving up loses the verdict, which escalates
                    # and is recoverable. Prefer the recoverable failure (issue #71).
                    return rc, f"{err} (post outcome unconfirmed — not re-sent)", attempt
            if attempt >= POST_MAX_ATTEMPTS or not transient_gh_failure(rc, err):
                return rc, err, attempt
            delay = POST_RETRY_BACKOFF_S[min(attempt - 1, len(POST_RETRY_BACKOFF_S) - 1)]
            log.warning(
                "[pr-reviewer] posting %s on %s#%s failed (attempt %d/%d), retrying in %ss: %s",
                event,
                repo,
                pr,
                attempt,
                POST_MAX_ATTEMPTS,
                delay,
                err[-200:],
            )
            await asyncio.sleep(delay)

    async def _verdict_landed(self, repo: str, pr: int, head: str) -> bool | None:
        """Did a verdict of ours for `head` reach the PR? None when we cannot tell.

        Only meaningful after an ambiguous POST (a timeout). Reuses the marker that
        `_our_reviews` already parses, so "did it land" is answered by the same source
        of truth that decides everything else about review history.
        """
        ours = await self._our_reviews(repo, pr)
        if ours is None:
            return None
        return any(r["head"] == head for r in ours)

    async def _clear_exhaustion_comments(self, repo: str, pr: int) -> None:
        """Delete our exhaustion notices once a real verdict lands (#54 follow-on to #61).

        The notice claims "this PR has not been reviewed". A verdict makes that false, and
        leaving it sitting above the review is worse than never posting it — a reader has
        to work out which of two contradictory bot comments is current.

        Every failure path LOGS. The notice is cosmetic, so a failure here must not fail
        the verdict that just posted; but swallowing `rc != 0` silently means a permanently
        contradictory PR with nothing in the log to explain it, which is how the original
        exhaustion silence went unnoticed for five weeks in the first place.
        """
        rc, out, err = await self._run_gh(
            ["api", f"repos/{repo}/issues/{pr}/comments", "--paginate", "--jq", ".[] | {id, body}"],
            timeout=60,
        )
        if rc != 0:
            log.warning("[pr-reviewer] listing comments to clear on %s#%s failed: %s", repo, pr, err[-300:])
            return
        # This read already used the one-object-per-line form (it is the shape the rest
        # of the file should have had all along); it now shares the parser. Clearing a
        # cosmetic notice is fail-open, so an unreadable list simply clears nothing.
        for item in gh_json_rows(out) or []:
            if not isinstance(item, dict) or EXHAUSTION_MARKER not in str(item.get("body") or ""):
                continue
            rc, _out, err = await self._run_gh(
                ["api", f"repos/{repo}/issues/comments/{item['id']}", "-X", "DELETE"], timeout=60
            )
            if rc != 0:
                log.warning("[pr-reviewer] clearing exhaustion notice on %s#%s failed: %s", repo, pr, err[-300:])

    async def _dismiss_stale_blocks(self, repo: str, pr: int) -> None:
        """Dismiss our own now-stale REQUEST_CHANGES reviews after a later head clears.
        With a `pull_request` branch rule active, changes-requested blocks the merge at
        ANY approval count, and only an APPROVE or a dismissal lifts it — never a
        comment. APPROVE stays reserved for the promotion owner, so the gate lifts
        itself by dismissal. Every non-dismissed blocker goes (GitHub rolls the
        reviewer's effective state back to the previous one otherwise)."""
        # Unreadable ⇒ dismiss nothing. Same shape as before (an unreadable read used
        # to yield `[]`, which also dismissed nothing), but now it says so (issue #71):
        # a block left standing is recoverable next tick, a wrongly-lifted one is not.
        for review in await self._our_reviews(repo, pr) or []:
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
        why = ineligible_reason(facts)
        if why:
            self.telemetry.emit("regate", repo=repo, pr=pr, decision="hold:pr-not-eligible", why=why)
            return "hold:pr-not-eligible"
        head = str(facts["head"])
        ours = await self._our_reviews(repo, pr)
        if ours is None:
            # Blind: `latest` would read as "no FAIL standing" and silently skip a
            # re-gate that IS owed. Hold and re-look next tick (issue #71).
            self.telemetry.emit("regate", repo=repo, pr=pr, sha=head, decision=HOLD_REVIEWS_UNREADABLE)
            return HOLD_REVIEWS_UNREADABLE
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
                await self._escalate(
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
        # Short-circuit BEFORE any GitHub read when promotion is structurally impossible:
        # not the owner, or in shadow. `promotion_decision` returns HOLD_NOT_OWNER first
        # thing in that case regardless of facts/reviews/checks/threads, so the four reads
        # below are pure waste on every sweep pass for every PR. On a non-owner/shadow
        # posture that was the bulk of the sweep's GitHub traffic (audit: 62% of promotion
        # evals). Same decision, same telemetry — just without the I/O.
        if not (self.promotion_owner and not self.shadow):
            self.telemetry.emit("promotion", repo=repo, pr=pr, decision=HOLD_NOT_OWNER)
            return HOLD_NOT_OWNER
        facts = await self._pr_facts(repo, pr)
        why = ineligible_reason(facts)
        if why:
            self.telemetry.emit("promotion", repo=repo, pr=pr, decision="hold:pr-not-eligible", why=why)
            return "hold:pr-not-eligible"
        head = str(facts["head"])
        ours = await self._our_reviews(repo, pr)
        if ours is None:
            # `promoted` below is computed FROM `ours`, so a blind pass reads as
            # "never promoted" and re-approves a head we already approved — the exact
            # every-tick re-approval loop the marker-parse regression caused (issue #71).
            self.telemetry.emit("promotion", repo=repo, pr=pr, sha=head, decision=HOLD_REVIEWS_UNREADABLE)
            return HOLD_REVIEWS_UNREADABLE
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
            # Only a clear verdict from a COMPLETE panel may auto-approve (#49): an
            # incomplete pass (a finder was down) holds until a full pass clears the head.
            complete=bool(clear.get("complete", True)) if clear else True,
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
                await self._escalate(
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
        why = ineligible_reason(facts)
        if why:
            # Previously returned None with NO telemetry, so a permanently-skipped PR
            # (a locked conversation, say) looked exactly like one the sweep never
            # reached. `locked` is worth a log line because it is not self-correcting.
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_PR_NOT_ELIGIBLE, why=why, action=BACKFILL_ACTION)
            if why == "locked":
                log.info("[pr-reviewer] %s#%s is locked — GitHub refuses reviews there; skipping", repo, pr)
            return None
        head = str(facts["head"])
        ours = await self._our_reviews(repo, pr)
        if ours is None:
            # Reviews unreadable ⇒ we cannot know whether a verdict exists, and
            # backfilling on a guess costs a full panel AND posts a duplicate review
            # on a PR that may already be reviewed (issue #71). The cost is asymmetric:
            # a skipped backfill is retried on the very next tick, a spurious one is
            # permanent on the PR. Fail CLOSED and wait for a readable pass.
            return None
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
        for repo in await self.sweep_repos():
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

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
import contextlib
import json
import logging
import os
import re
import time
from urllib.parse import quote

from .approve import HOLD_NOT_OWNER, HOLD_THREADS_UNRESOLVED, PROMOTE, Observations, promotion_decision
from .checks import CHECK_NAME, COMPLETED, FAILURE, IN_PROGRESS, SUCCESS, CheckRun, check_for, closed_run
from .chokepoint import DISPATCH_ACTIONS, Chokepoint
from .gh_cli import bad_repo, run_gh
from .grounding import (
    UNREADABLE,
    apply_grounding,
    correct_line_numbers,
    ground_finding,
    render_grounding_footnote,
    render_unreadable_footnote,
)
from .protopatch import STRUCTURAL_GAP_MARKERS, classify_outage, outage_reason
from .rounds import (
    DEFAULT_CONVERGENCE_ROUNDS,
    converge,
    delta_ranges,
    diff_identity,
    in_delta,
    normalize_relisted_priors,
    panel_rounds,
    parse_dispositions,
    render_degraded_note,
    render_evidence_gone_note,
    render_held_note,
    render_incomplete_note,
    render_notes_section,
    render_prior_requests,
    render_promotion_findings,
    render_unaccounted_note,
    round_cap_reached,
    spent_rounds,
    unaccounted_priors,
    unexplained_clearance,
)
from .telemetry import REAFFIRM_DIFF, REAFFIRM_HEAD, REAFFIRM_MISS, REAFFIRM_RECORDED, VERIFY_CONTRADICTED, Telemetry
from .trigger import structural_trigger
from .verdicts import (
    FAIL,
    FINDER_STEP_PREFIX,
    PASS,
    WARN,
    confine_findings,
    coverage_gaps,
    coverage_verdict,
    demote_stale_findings,
    extract_brief,
    finder_completed,
    mentions_any,
    merge_carried_findings,
    overrun_lanes,
    parse_verdict_marker,
    render_verdict_body,
    report_hard_stopped,
    restate_findings,
    structural_relay_ok,
    undelivered_stages,
    verdict_for,
    verification_ran,
    verifier_contradicts_synthesis,
    verify_delivered,
)

log = logging.getLogger("protoagent.plugins.pr_reviewer")

DROP_SELF_AUTHORED = "self-authored"
DROP_PR_NOT_ELIGIBLE = "pr-not-eligible"  # closed, draft, LOCKED, or facts unreadable
DROP_NO_RUNNER = "no-workflow-runner"
DROP_PAUSED = "paused-by-operator"  # `@vera pause` (issue #28)
DROP_REVIEWS_UNREADABLE = "reviews-unreadable"  # blind on our own history (issue #71)
DROP_VIEWER_UNKNOWN = "viewer-unknown"  # blind on our own IDENTITY — can't rule out self-review
DROP_POST_REFUSED = "post-refused"  # GitHub keeps rejecting this verdict post (issue #78)
DROP_ROUND_TIMEOUT = "round-timeout"  # a round outlived round_timeout_s and was cancelled

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
    # A response body that got cut short mid-transfer (connection dropped, proxy
    # hiccup) fails `gh`'s own JSON decode before we ever see a status line — the
    # same transient-network family as "connection reset", just caught one layer
    # up. Without this, `transient_gh_failure` classified it as a hard refusal and
    # `_post_review_with_retry` gave up on attempt 1 (issue #72's failure class
    # recurring under a different error shape): 4 verdict-lost events in one day,
    # one of which (ebay-plugin#3) merged with zero visible reviews because that
    # repo also lacked a required-review gate.
    "unexpected end of json input",
)

# The four LLM review-finder steps in `code-review-structural.yaml` — everything
# except the non-LLM structural relay, which has its own completeness check
# (`structural_relay_ok`) since its contract (call a tool once, relay verbatim) is
# narrower than "review the code and report."
LLM_FINDER_STEPS = ("find_correctness", "find_removed_behavior", "find_crossfile", "find_conventions")

# Recipes whose finder prompts REQUIRE the `FINDER_STATUS` line (`finder_completed`). The
# small-diff `code-review` recipe lives in protoAgent and asks for no such line, so its
# finders are judged by what they delivered alone — reading a line nobody asked for as
# missing marked every small-diff review incomplete.
STATUS_LINE_RECIPES = frozenset({"code-review-structural"})

# Compare calls one round may spend re-proving carried findings against the heads they
# were raised at (`_since_ranges`). Carried findings of one PR share a handful of heads.
SINCE_RANGES_LIMIT = 4


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


def is_own_login(author: str, viewer: str) -> bool:
    """Is `author` our own account, given our login `viewer`? Case-insensitive.

    Exactly `viewer`, and — only when `viewer` has no `[bot]` suffix — also
    `<viewer>[bot]`: a bare login may be the slug of the App whose reviews post as
    `<slug>[bot]`, and GitHub allows no App name that collides with an existing account,
    so that form can only be ours. Never the reverse: for `viewer` = `x[bot]`, a plain
    account named `x` is a different account and is not ours.
    """
    a = (author or "").strip().lower()
    v = (viewer or "").strip().lower()
    if not a or not v:
        return False
    return a == v or (not v.endswith("[bot]") and a == f"{v}[bot]")


# Severity order for the strictest-verdict-wins tie-break (issue #89): a higher rank
# is stricter, so a FAIL for a head can never be shadowed by a co-landed PASS.
_VERDICT_RANK = {PASS: 0, WARN: 1, FAIL: 2}


def coverage_only_round(round_: dict) -> bool:
    """Is this round's verdict the incomplete-coverage cap and nothing more?

    True for an INCOMPLETE round (a lane did not deliver a full pass — marker
    `complete=false`, #49) that recorded an explicit, EMPTY findings array. Its WARN is
    `coverage_verdict` capping a clean PASS (#117): a statement that the panel covered
    less, not that it found anything. A COMPLETE round for the same head answers exactly
    that question, so this round stops speaking for the head once one exists.

    Everything else is a real verdict and keeps its full #89 weight: a FAIL (whatever its
    completeness), any round with findings, and a round whose findings record is absent
    or malformed (`findings_recorded` False) — we cannot prove it raised nothing, so it
    fails closed and is never discarded.
    """
    return (
        not round_.get("complete", True)
        and round_.get("verdict") in (PASS, WARN)
        and bool(round_.get("findings_recorded"))
        and not round_.get("findings")
    )


def _accepts_keyword(fn, name: str) -> bool:
    """Does `fn` take keyword `name`? The host runner grew `seed_outputs` in
    protoAgent#3571; an older host's (and a test fake's) does not, and calling it so
    would turn a re-run into a crash."""
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def strictest_head_round(reviews: list[dict], head: str) -> dict | None:
    """The STRICTEST panel round posted for `head`, or None when the head has none.

    Two concurrent reviews can complete for the same head and land as two separate
    reviews — a FAIL and a PASS — and GitHub returns them in an arbitrary order.
    `panel_rounds` folds a head's reviews into ONE round, keeping whichever it saw
    LAST, so a PASS arriving after a co-landed FAIL would shadow it and auto-approve
    straight past the blocker (issue #89). Collapsing by STRICTEST verdict instead
    (FAIL > WARN > PASS) fails closed: the harsher verdict wins the tie regardless of
    arrival order. Findings/`complete` come from the NEWEST round bearing that verdict,
    so a promoted WARN still carries its most recent findings forward (issue #22).

    Coverage recovered: once a COMPLETE round exists for the head, a `coverage_only_round`
    (incomplete, zero findings — its WARN is the coverage cap alone) is left out of the
    tie-break. Otherwise ONE blind lane poisoned the head for good: the strictest pick
    stayed that `WARN complete=false` round however many complete PASSes followed, so
    promotion held `hold:incomplete-coverage` and the `QA panel` check sat at "Incomplete
    pass" until a new commit (qaEngineer#59 @ 13fbcaba). Order-free, like the tie-break.
    A FAIL or any round with findings still competes — incomplete or not — so this never
    promotes past a real finding. If such an incomplete round wins, its verdict and
    findings govern, but the head's coverage WAS recovered by the complete round, so the
    pick is judged complete (WARN ⇒ the threads/findings gate), not held forever — as long
    as its findings record is readable; an unreadable one still holds, fail-closed.

    Promotions (`promoted=true`) are not rounds and are excluded, same as `panel_rounds`.
    """
    rounds = [
        r
        for rev in reviews or []
        if not rev.get("promoted") and str(rev.get("head") or "") == head
        for r in panel_rounds([rev])
    ]
    if not rounds:
        return None
    recovered = any(r["complete"] for r in rounds)
    if recovered:
        # Never empties the list: a complete round is not coverage-only by definition.
        rounds = [r for r in rounds if not coverage_only_round(r)]
    strictest = max(_VERDICT_RANK.get(r["verdict"], -1) for r in rounds)
    pick = next(r for r in reversed(rounds) if _VERDICT_RANK.get(r["verdict"], -1) == strictest)
    # Only a pick whose findings we can READ is judged complete: its findings then govern
    # through the threads gate and carry forward on promotion. One with an absent or
    # malformed record stays incomplete (holds) — promoting it would carry nothing forward
    # while it may hold a real finding.
    if recovered and not pick["complete"] and pick.get("findings_recorded"):
        pick = {**pick, "complete": True}
    # Verification recovered (#170): one round whose verifier flaked (`verified=false`,
    # #167) outranked every VERIFIED round that followed at the same head — WARN > PASS —
    # so the head held `hold:unverified` until a new commit; the documented remedy, a
    # re-summon, could never clear it (mythxengine-sdk#384). Only a verified round that
    # came AFTER the pick lifts the hold: that round was handed the pick's findings as
    # open prior requests and re-raised or dispositioned them, so the head's claims were
    # checked. An earlier verified round never saw them and proves nothing — so this is
    # NOT order-free like the coverage rule; it reads GitHub's monotonic review ids. The
    # pick's verdict and findings still govern; only the hold lifts — and only for a pick
    # whose findings record is readable, fail-closed like coverage.
    if (
        not pick.get("verified", True)
        and pick.get("findings_recorded")
        and any(r.get("verified", True) and r.get("id", 0) > pick.get("id", 0) for r in rounds)
    ):
        pick = {**pick, "verified": True}
    return pick


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
# `reconcile_pr` outcomes for a backfill the sweep did not wait for (`_detach_backfill`).
BACKFILL_STARTED = "backfill:started"
BACKFILL_DEFERRED = "backfill:deferred"  # the per-pass cap of outstanding backfills is full

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

# The dispatch-path check run (#95), keyed to the reviewed head. Distinct from checks.py's
# `QA panel` (the promotion gate): that one reports whether a head is CLEARED for merge and
# only writes where this agent owns promotion, so a shadow deployment publishes nothing and
# an EXHAUSTED panel — no verdict — leaves it sitting `in_progress`, indistinguishable from
# a head still under review. `protoReview` is different in kind: opened for every panel that
# proceeds past the drop/skip gates, and concluded by the SAME review that opened it, so it
# never dangles and is safe to require even in shadow mode. Its whole job is the closure an
# advisory review could not give — an exhausted panel leaves a red X, not the silence that
# let 12 PRs merge unreviewed. The name is the required-status context branch protection
# pins, so it is a constant, not config (renaming it silently unrequires the gate).
REVIEW_CHECK_NAME = "protoReview"

# Both of our own check runs sit `in_progress` while the panel runs; neither is a check we
# WAIT on (see `_checks_state`), or the gate deadlocks on itself.
OUR_CHECK_NAMES = frozenset({CHECK_NAME, REVIEW_CHECK_NAME})


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
        # Refuted structural claims, remembered per repo (#190) — written here when a round
        # posts, read by the structural pass; one constructor so root and TTL cannot drift.
        from .refutations import RefutationStore

        self.refutations = RefutationStore.from_cfg(self._cfg)  # one constructor: root and TTL cannot drift
        # Boot-time by necessity: the chokepoint owns in-flight/cooldown state, so it
        # cannot be rebuilt per read without dropping the bookkeeping it exists for.
        # On-demand summon surface (issue #28). Off disables the comment commands
        # entirely, including the pause check — a repo that never wants comment-driven
        # behaviour pays nothing for it.
        self.summon_enabled = (
            bool(self.cfg["summon"]) if "summon" in self.cfg else _env_bool("PR_REVIEWER_SUMMON", True)
        )
        # The TTL sits above the round bound so a slow-but-live round is never raced by a
        # second panel on the same PR; it only reclaims a slot whose round can't end.
        self.chokepoint = Chokepoint(
            cooldown_s=int(self._cfg.get("cooldown_s") or 30),
            in_flight_ttl_s=self.round_timeout_s + 600,
            on_reclaim=self._on_in_flight_reclaimed,
        )
        self._run_gh = run_gh_fn or run_gh
        self._workflow_run = workflow_run  # None → resolve STATE.workflow_run lazily
        self._inbox_add = inbox_add  # None → resolve STATE.inbox_store lazily
        self._viewer: str | None = None
        self._promote_failures: dict[str, int] = {}  # repo#pr@head -> consecutive APPROVE failures
        self._regate_failures: dict[str, int] = {}  # repo#pr@head -> consecutive REQUEST_CHANGES failures
        self._post_failures: dict[str, int] = {}  # repo#pr@head -> NON-transient verdict-post refusals
        self._viewer_checked = False  # viewer_login vs our reviews' real author, warned once
        self._round_cap: dict[str, float] = {}  # repo#pr -> monotonic timestamp when capped
        # Backfill panels the sweep started and did not wait for (`_detach_backfill`). Held
        # here so the tasks are not garbage-collected mid-run, and counted against
        # `backfill_per_pass` so successive passes cannot pile an unbounded queue onto the
        # panel semaphore.
        self._backfills: set[asyncio.Task] = set()
        self._installation_repos: list[str] = []  # last good App-installation scope
        self._installation_repos_at: float = 0.0
        # The cross-PR panel cap (#96) is a WEBHOOK-LAYER concern: build_routers sizes an
        # asyncio.Semaphore from `max_concurrent_panels` and INJECTS it here, so the
        # sweep's backfill panels queue behind the same bound as webhook dispatches
        # instead of piling on top of a burst. None ⇒ unbounded (a sweep-only wiring, or
        # a test that never built the routers); the dispatcher never creates or sizes it.
        self.panel_sem: asyncio.Semaphore | None = None

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
    def qa_check(self) -> bool:
        """Publish the `QA panel` check run (checks.py) alongside the verdict review.

        On by default, and inert until a repo's ruleset lists `QA panel` as a required
        status — until then it is one more line in the PR's check list. It rides the same
        promotion-owner gate as approve-on-green, so a shadow-mode repo publishes nothing:
        a REQUIRED check that nobody drives blocks every merge in that repo forever."""
        cfg = self.cfg
        return bool(cfg["qa_check"]) if "qa_check" in cfg else _env_bool("PR_REVIEWER_QA_CHECK", True)

    @property
    def panel_retries(self) -> int:
        """D3 says an exhausted run is "retry or escalate" — we do both, in that order.
        A failed panel step is usually transient, and the alternative to retrying is a
        PR that merges UNREVIEWED."""
        cfg = self.cfg
        return int(cfg["panel_retries"]) if "panel_retries" in cfg else _env_int("PR_REVIEWER_PANEL_RETRIES", 1)

    @property
    def panel_attempt_timeout_s(self) -> float:
        """Budget for ONE panel attempt (one `runner(recipe, inputs)` call). Only the
        finders carry a step timeout; the verifier, synthesis and grounding steps don't,
        so a hang in any of them used to hang the round — and, holding the PR's
        in-flight slot, every later review of that PR. Past the budget the attempt is
        cancelled and counts as a crashed attempt: retried, then concluded as "QA panel
        crashed" on the PR, visibly. Default 30 min, above the finders' 15."""
        cfg = self.cfg
        return (
            float(cfg["panel_attempt_timeout"])
            if "panel_attempt_timeout" in cfg
            else _env_int("PR_REVIEWER_PANEL_ATTEMPT_TIMEOUT", 1800)
        )

    @property
    def verify_reruns(self) -> int:
        """How many times a verifier that contradicted the synthesizer is re-run ALONE
        (#167) before the round posts as unverified. Seconds each, seeded with the
        finders' and synthesizer's outputs — the alternative is a fresh five-finder
        panel or a human re-summon. 0 disables; needs a host whose runner takes
        `seed_outputs` (protoAgent#3571), else the round is only counted."""
        cfg = self.cfg
        return max(0, int(cfg["verify_reruns"])) if "verify_reruns" in cfg else _env_int("PR_REVIEWER_VERIFY_RERUNS", 1)

    @property
    def verify_fallback_panel(self) -> bool:
        """After a RESTATED verify re-run is still contradicted, run one fresh panel before
        posting a held PASS (#189). Every observed fresh round on this shape has verified;
        without it the PR waits for a human summon. Off ⇒ the held PASS posts as before."""
        cfg = self.cfg
        return (
            bool(cfg["verify_fallback_panel"])
            if "verify_fallback_panel" in cfg
            else _env_bool("PR_REVIEWER_VERIFY_FALLBACK_PANEL", True)
        )

    @property
    def finder_timeout_s(self) -> int:
        """Seconds each parallel finder may run, or 0 to leave the recipe's default (#93).

        The right budget depends on the model the deployment runs — a constant calibrated on
        one lane silently truncates productive finders on a slower one — so it is operator
        config, passed to the recipe as its `finder_timeout` input. Clamped a minute under
        `panel_attempt_timeout_s`: a finder budget at or above the attempt's own would let
        the attempt be cancelled first, which concludes as a crashed panel instead of a
        one-lane Gap. A value that is not a positive number reads as unset, never as "no
        timeout" — an unbounded finder is what the budget exists to prevent.
        """
        cfg = self.cfg
        raw = cfg["finder_timeout_s"] if "finder_timeout_s" in cfg else _env_int("PR_REVIEWER_FINDER_TIMEOUT", 0)
        try:
            seconds = int(float(raw or 0))
        except (TypeError, ValueError):
            seconds = 0
        if seconds <= 0:
            return 0
        ceiling = int(self.panel_attempt_timeout_s) - 60
        if ceiling > 0 and seconds > ceiling:
            log.warning(
                "[pr-reviewer] finder_timeout_s=%s is not below panel_attempt_timeout (%ss); using %ss",
                seconds,
                int(self.panel_attempt_timeout_s),
                ceiling,
            )
            return ceiling
        return seconds

    @property
    def round_timeout_s(self) -> float:
        """Backstop for a WHOLE round — every attempt plus the GitHub calls around them —
        for a hang outside the panel runner. Defaults to every attempt's budget plus ten
        minutes, so it can never cut a legitimate retry short."""
        cfg = self.cfg
        if "round_timeout" in cfg:
            return float(cfg["round_timeout"])
        default = (self.panel_retries + 1) * self.panel_attempt_timeout_s + 600
        return _env_int("PR_REVIEWER_ROUND_TIMEOUT", default)

    def _on_in_flight_reclaimed(self, repo: str, pr: int, held_s: float) -> None:
        # Past the round bound, a still-held slot means a round that stopped making
        # progress and never ended — a defect, so make it loud, not just unlocked.
        log.warning(
            "[pr-reviewer] %s#%s: reclaimed an in-flight slot held %.0fs — its round never finished",
            repo,
            pr,
            held_s,
        )
        self.telemetry.emit("in_flight_reclaimed", repo=repo, pr=pr, held_s=round(held_s))

    def rebind_config(self, cfg: dict | None, cfg_provider=None) -> None:
        """Re-point a RUNNING dispatcher at a re-registered config (issue #198).

        The in-flight/cooldown state, the backfill set and the round caps are exactly
        what must survive a config reload — but the boot-derived knobs must not go
        stale next to them: the summon switch, the refutation store (root + TTL) and
        the chokepoint's cooldown are rebuilt from the new config here (review on
        #199, round 1)."""
        from .refutations import RefutationStore

        self._cfg = cfg or {}
        self._cfg_provider = cfg_provider
        self.refutations = RefutationStore.from_cfg(self._cfg)
        self.summon_enabled = (
            bool(self.cfg["summon"]) if "summon" in self.cfg else _env_bool("PR_REVIEWER_SUMMON", True)
        )
        self.chokepoint.cooldown_s = int(self._cfg.get("cooldown_s") or 30)
        self.chokepoint.in_flight_ttl_s = self.round_timeout_s + 600

    async def _bounded_review(self, repo: str, pr: int, **review_kwargs) -> str:
        """``_review`` under ``round_timeout_s``. The callers hold the chokepoint slot and
        release it in their ``finally``; bounding the round here is what guarantees they
        get there."""
        bound = asyncio.timeout(self.round_timeout_s)
        try:
            async with bound:
                return await self._review(repo, pr, **review_kwargs)
        except TimeoutError:
            if not bound.expired():
                raise  # a TimeoutError from inside the round is not the round's own bound
            log.warning("[pr-reviewer] %s#%s: round exceeded %gs — cancelled", repo, pr, self.round_timeout_s)
            self.telemetry.emit("drop", repo=repo, pr=pr, reason=DROP_ROUND_TIMEOUT, timeout_s=self.round_timeout_s)
            # Tell the operator, not just the log: a round this long means something the
            # panel depends on has stopped answering, and every later round will likely
            # hang the same way. The incident this bound came from went unnoticed for
            # hours precisely because nothing said so.
            await self._escalate(
                f"pr-reviewer: a review round on {repo}#{pr} ran past {self.round_timeout_s:g}s and was "
                f"cancelled — PR is UNREVIEWED. Rounds hanging this long usually mean a dependency "
                f"of the panel (model gateway, workflow runner) has stopped answering.",
                dedup_key=f"pr-reviewer-round-timeout:{repo}#{pr}",
            )
            return f"drop:{DROP_ROUND_TIMEOUT}"

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
    def max_concurrent_panels(self) -> int:
        """Cross-PR cap on panels running at once, sized so `× 5 finders` stays within
        one gateway lane (#96). Read here so an operator can retune it live, but the
        SEMAPHORE lives at the webhook dispatch layer — `build_routers` sizes one from
        this and injects it as `panel_sem`. Clamped to ≥1: a `Semaphore(0)` would deadlock
        every dispatch, so zero degrades to serial, never to a stall."""
        cfg = self.cfg
        n = (
            int(cfg["max_concurrent_panels"])
            if "max_concurrent_panels" in cfg
            else _env_int("PR_REVIEWER_MAX_CONCURRENT_PANELS", 3)
        )
        return max(1, n)

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
            f"🛑 **Review cap reached** — this PR has used its budget of {self.max_rounds} complete "
            f"automated review round(s). Further pushes will not trigger new reviews.\n\n"
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
        """Warn once when another App's review carries our verdict marker.

        `viewer_login` is unvalidated by construction — it is a string an operator
        typed. A typo does not error; it silently makes the self-authored comparison
        never match, which is precisely the disabled rail this config exists to
        prevent. Our own reviews then arrive under an App (`[bot]`) login that is not
        `viewer_login`, which `_our_reviews` reads as an UNREADABLE history — every
        caller holds — so this line is what tells an operator why. Only an App author is
        considered: a person cannot post as one, so a human quoting a verdict says
        nothing about our login and must not use up this one warning before a real typo.

        Warn, never correct: an operator who set this deliberately (a migration, a
        renamed app) should not have the reviewer quietly overrule them, and being
        wrong in the SAFE direction — comparing against a login that never matches —
        only ever causes held promotions and skipped reviews, not self-approval.
        """
        author = (author or "").strip().lower()
        if not author.endswith("[bot]") or self._viewer_checked or not self._viewer:
            return
        if is_own_login(author, self._viewer):
            return
        self._viewer_checked = True
        log.warning(
            "[pr-reviewer] viewer_login is %r but a marker-bearing review is authored by the app %r — "
            "review history reads as unreadable, so reviews and promotions hold until they agree. If "
            "that app is ours, fix pr_reviewer.viewer_login (the never-review-your-own-PR rail will "
            "not fire either).",
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

    async def _commit_tree(self, repo: str, ref: str) -> str | None:
        """The Git tree SHA of `ref`'s commit — one Merkle root over the WHOLE tree, or None.

        Read PINNED to the SHA the caller passes (`gh` resolves it; the model never supplies
        a ref, ADR 0078). Being a single hash over the entire tree, it is immune to the
        3,000-file `/pulls/{n}/files` truncation the changed-file diff suffers — a change in
        a file past that cap still moves the tree root. Fails CLOSED: non-zero rc or an empty
        read ⇒ None.
        """
        rc, out, _err = await self._run_gh(["api", f"repos/{repo}/commits/{ref}", "--jq", ".commit.tree.sha"])
        tree = out.strip() if rc == 0 else ""
        return tree or None

    async def _merge_base_tree(self, repo: str, base: str, head: str) -> str | None:
        """The tree SHA of the base↔head merge base — the left endpoint of the three-dot
        diff, or None. Derived server-side from the PINNED head SHA, so a push landing after
        the head was resolved cannot move it. Fails CLOSED like `_commit_tree`.
        """
        if not base or not head:
            return None
        rc, out, _err = await self._run_gh(
            ["api", f"repos/{repo}/compare/{base}...{head}", "--jq", ".merge_base_commit.commit.tree.sha"]
        )
        tree = out.strip() if rc == 0 else ""
        return tree or None

    async def _pr_diff_id(self, repo: str, base: str, head: str) -> str | None:
        """Identity of the PR's current review-relevant base↔head diff, or None (issue #91).

        Both endpoints of the three-dot diff are read as Git tree SHAs, PINNED to the
        resolved head — the head tree by its SHA, the merge-base tree derived from that same
        SHA — and folded by `diff_identity`. The identity is stable across a rebase, a
        reworded commit, or a moved-but-identical base (the head SHA changes, the reviewed
        content does not), and it moves the instant any reviewed byte does — INCLUDING a
        dependency the rebase pulled in through the base, which a changed-file-only hash
        could not see (the correctness gap the earlier attempt shipped).

        Fails CLOSED: either tree unreadable ⇒ None (via the helpers and `diff_identity`),
        so the reaffirm short-circuit declines and the normal review runs. It posts nothing
        and never relaxes the SHA-keyed stale-head protections — it only decides whether a
        redundant panel can be skipped.
        """
        head_tree = await self._commit_tree(repo, head)
        if head_tree is None:
            return None
        merge_base_tree = await self._merge_base_tree(repo, base, head)
        return diff_identity(merge_base_tree, head_tree)

    def _reaffirm_by_diff(self, repo: str, pr: int, head: str, prior: dict, current_id: str | None) -> str | None:
        """Reuse `prior`'s verdict when the PR's current diff (`current_id`) is byte-identical
        to what that round reviewed, even though the head SHA changed — a rebase, a reworded
        commit, or a moved-but-identical base (issue #91). Returns ``reaffirmed:<verdict>`` or
        None to fall through to a fresh panel.

        A PURE decision over ids the caller already read — it issues no GitHub call. Fails
        CLOSED in every uncertain direction, each telemetered so a reuse that stops is never
        silent: the current diff was unreadable, the prior round stored no identity (an older
        body), or the two differ ⇒ None and the normal review runs. The decision itself posts
        nothing; the caller records a reaffirmed PASS/WARN at the new head
        (`_record_reaffirmed`, issue #135) so the SHA-keyed gate can find it. Only a
        proven-identical diff suppresses the redundant panel.
        """
        if current_id is None:
            self.telemetry.emit(REAFFIRM_MISS, repo=repo, pr=pr, sha=head, reason="diff-unreadable")
            return None
        prior_id = prior.get("diff_id")
        if not prior_id:
            self.telemetry.emit(REAFFIRM_MISS, repo=repo, pr=pr, sha=head, reason="prior-has-no-diff-id")
            return None
        if current_id != prior_id:
            self.telemetry.emit(REAFFIRM_MISS, repo=repo, pr=pr, sha=head, reason="diff-changed")
            return None
        # An INCOMPLETE or UNVERIFIED round is not a verdict worth carrying (#179): a lane was
        # down (or the verifier flaked), the body says "coverage incomplete — the next push
        # re-runs the full panel", and reaffirming it made that promise false — the only way
        # left to earn a complete pass was to change the content hash on purpose. An
        # identical diff earns the identical verdict only when that verdict was earned.
        if not prior.get("complete", True) or not prior.get("verified", True):
            reason = "prior-incomplete" if not prior.get("complete", True) else "prior-unverified"
            self.telemetry.emit(REAFFIRM_MISS, repo=repo, pr=pr, sha=head, reason=reason)
            return None
        self.telemetry.emit(
            REAFFIRM_DIFF, repo=repo, pr=pr, sha=head, prior_head=prior.get("head"), verdict=prior.get("verdict")
        )
        return f"reaffirmed:{prior['verdict']}"

    async def _record_reaffirmed(self, repo: str, pr: int, head: str, prior: dict, diff_id: str | None) -> None:
        """Record the reaffirmed verdict AT the new head, so the gate can find it (issue #135).

        `_reaffirm_by_diff` rightly refuses to re-spend the panel on a byte-identical diff,
        but it used to leave the new head with NO verdict: the panel would not review it
        (identical diff → reaffirm, every time) and the SHA-keyed gate would not accept the
        old head's — `hold:stale-head` until a push that CHANGED the diff, which a finished
        PR has no honest reason to make. 12 of 23 reaffirmed heads were held that way
        (p50 68 min, max 5.5 h).

        Sound because `diff_identity` folds BOTH tree roots — the merge-base tree and the
        head tree — so an equal identity means the whole reviewed content is byte-identical
        and only commit metadata moved; the verdict asserts nothing the panel did not check.

        Deliberately narrow. Only a PASS/WARN is carried: a reaffirmed FAIL stays as it was
        (no verdict at the new head ⇒ the gate fails closed). An incomplete or unverified
        round never reaches here — `_reaffirm_by_diff` declines it and the panel runs (#179)
        — so a carried verdict is always complete and verified; the flags ride along as a
        record, not a hold. `hold_blocks=True`: nothing new was judged, so this post never dismisses a
        standing block. The marker's `reaffirmed=` keeps it out of the round count.
        """
        verdict = str(prior.get("verdict") or "")
        if verdict not in (PASS, WARN):
            return
        origin = str(prior.get("head") or "")
        brief = (
            f"Reaffirmed from `{origin[:12]}` — this head's base↔head content is byte-identical to what "
            f"that round reviewed (same merge-base tree, same head tree; only commit metadata moved), so "
            f"its **{verdict}** applies unchanged. No panel was spent on this head."
        )
        posted = await self._post_verdict(
            repo,
            pr,
            head,
            verdict,
            [f for f in (prior.get("findings") or []) if isinstance(f, dict)],
            "reaffirmed",
            brief=brief,
            hold_blocks=True,
            complete=bool(prior.get("complete", True)),
            verified=bool(prior.get("verified", True)),
            diff_id=diff_id,
            reaffirmed_from=origin,
        )
        self.telemetry.emit(
            REAFFIRM_RECORDED, repo=repo, pr=pr, sha=head, prior_head=origin, verdict=verdict, posted=posted
        )

    async def _our_reviews(self, repo: str, pr: int) -> list[dict] | None:
        """Our posted reviews (marker-bearing, authored by our own login), oldest→newest:
        [{head, verdict, promoted, state, body, id}].

        **None means the read FAILED — it is not the same as `[]` (this PR has no
        reviews), and callers must not collapse the two** (issue #71). Returning `[]`
        on an unreadable read is a fail-OPEN: `needs_backfill` reads it as "no verdict
        exists" and re-reviews a PR that is already reviewed, which cost two PRs ten
        full panels each on a static head during a GitHub degradation. Every caller
        here decides explicitly, and every one of them fails CLOSED.

        Panel rounds are read ONLY from the reviewer's own reviews. The verdict marker is
        plain text, so a review by another account can carry one too — a human quoting or
        pasting a verdict, say — and such a review is not a panel round and must not count
        as one: not for promotion, re-gating, coverage recovery, or the "already reviewed
        this head" check, all of which read this list. Ours means `is_own_login`: exactly
        our login, never a plain account that shares an App's name. Telling the two apart
        needs our login, so an UNKNOWN login makes the history unreadable (None), the same
        fail-closed answer as a failed reviews read.

        A marker-bearing review by another App (`[bot]`) is different from a person's: a
        person cannot post as an App, so it is almost always OUR reviews under a login
        that is not `viewer_login` (a typo, a renamed app). Skipping those would make every
        head look unreviewed and re-spend the panel on every PR each tick (issue #71's
        failure mode), so that also returns None and warns once. A person's review that
        carries the marker is simply skipped.
        """
        viewer = await self._viewer_login()
        if not viewer:
            return None
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
        not_ours = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            marker = parse_verdict_marker(row.get("body") or "")
            if not marker:
                continue
            author = str(row.get("author") or "").strip().lower()
            if not is_own_login(author, viewer):
                if author.endswith("[bot]"):
                    # Another App carries our marker: most likely our own reviews under a
                    # login that is not `viewer_login`. Unreadable, so every caller holds.
                    self._check_viewer_matches(author)
                    self.telemetry.emit(
                        "reviews-unreadable", repo=repo, pr=pr, why="marker-by-another-app", author=author
                    )
                    return None
                not_ours += 1  # carries the marker, but a person's account wrote it
                continue
            ours.append({**marker, "state": row.get("state", ""), "body": row.get("body") or "", "id": row.get("id")})
        if not_ours:
            self.telemetry.emit("marker_not_ours", repo=repo, pr=pr, skipped=not_ours)
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
    ) -> dict[str, tuple[str, str | object | None]]:
        """{file: (blob, combined)} for the files the findings cite.

        ``blob`` is the raw file at the reviewed head — used for line-number correction.
        ``combined`` is ``blob + patch`` when the head read SUCCEEDS — used for grounding
        existence checks (a removed-behaviour finding legitimately quotes code the head no
        longer has). When the head file cannot be read, ``combined`` is the ``UNREADABLE``
        sentinel — NOT a patch-only haystack: absence cannot be established against a source
        that was never read, and grounding must treat that as could-not-verify (issue #109),
        never as fabricated evidence that lifts the gate.

        SUCCESS is keyed off ``rc == 0``, not off non-empty output: a zero-byte file at the
        head reads back as empty ``.content`` (``out.strip() == ""``) yet is a genuine,
        successful read of an empty file — its (empty) blob still grounds a fabricated quote
        as absent and downgrades it, so it must NOT be mistaken for an unreadable source.

        The read is PINNED to the immutable head SHA (``ref=<head>``, resolved server-side —
        never a model ref, ADR 0078) against the correct repository over the authenticated
        ``gh`` client. A force-push mid-review can only 404 the orphaned SHA (⇒ UNREADABLE),
        never silently resolve a DIFFERENT head, because we never fall back to the movable
        branch tip (a bare ``contents/{file}``) — a read of another head is not evidence
        about this one (acceptance r1/r6). Path and ref are URL-encoded so a filename with a
        space or a ref with a slash cannot 404 by malformed request.
        """
        import base64

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
        sources: dict[str, tuple[str, str | object | None]] = {}
        for file in {str(f.get("file") or "") for f in findings if f.get("file")}:
            ref = quote(head, safe="")
            rc, out, _err = await self._run_gh(
                [
                    "api",
                    f"repos/{repo}/contents/{quote(file, safe='/')}?ref={ref}",
                    "--jq",
                    '(.encoding // "") + "\\u0000" + (.content // "")',
                ]
            )
            blob = ""
            read_ok = False
            # ``rc == 0`` is a SUCCESSFUL read — INCLUDING a zero-byte file, whose ``.content``
            # is the empty string. Gating ``read_ok`` on non-empty output would misclassify
            # that empty-but-real file as UNREADABLE and PRESERVE an ungrounded blocker/major
            # instead of downgrading a quote genuinely absent from a file we DID read (the
            # issue #109 regression this guard exists for).
            #
            # But an empty ``.content`` is NOT always an empty file. GitHub's Contents API
            # returns ``content: ""`` with ``encoding: "none"`` for a file between 1 and
            # 100 MB — the content is OMITTED, not absent. Reading that as a zero-byte file
            # marked it read_ok and let a real finding in an oversized file be downgraded for
            # "missing" evidence that was never fetched — the exact failure #109 is about,
            # wearing a different hat.
            #
            # ``encoding`` is what separates them: ``base64`` is a real read (including a
            # genuinely empty file), anything else — ``none`` for oversized, or an absent
            # object for a directory / submodule / 404 — is UNREADABLE, so the finding's
            # severity is preserved and the report says the source was unavailable.
            encoding, _, payload = out.partition("\x00")
            if rc == 0 and encoding.strip() == "base64":
                try:
                    blob = base64.b64decode(payload.strip()).decode("utf-8", errors="replace")
                    read_ok = True
                except Exception:  # noqa: BLE001 — an undecodable blob is a failed read
                    blob = ""
            # A successful read grounds against blob + patch; a failed read is UNREADABLE,
            # so a finding quoting real head code is preserved, not downgraded, and the
            # report says the source was unavailable rather than claiming absence.
            combined: str | object = f"{blob}\n{patches.get(file, '')}" if read_ok else UNREADABLE
            sources[file] = (blob, combined)
        return sources

    async def _clear_by_evidence(
        self,
        repo: str,
        pr: int,
        head: str,
        priors: list[dict],
        *,
        ranges: dict | None,
        since_ranges: dict[str, dict | None] | None,
    ) -> tuple[list[dict], list[dict]]:
        """(still unaccounted, cleared as fixed-by-evidence) — issue #196.

        A prior is cleared only when ALL of these hold, each fail-closed on its own:
        the delta since the head it was raised at (`since_ranges[since]`, else `ranges`)
        is readable AND touches the flagged line; the cited file was READ at `head`
        (`UNREADABLE` keeps the carry, issue #109); the prior quotes checkable code; and
        every quote is absent from the file at head PLUS the PR's patch for it. A
        removed-behaviour prior stays grounded through the patch's `-` lines and carries.
        """
        candidates: list[int] = []
        for i, prior in enumerate(priors):
            proof = (since_ranges or {}).get(str(prior.get("since") or ""))
            if proof is None:
                proof = ranges
            if proof is None:
                continue
            if in_delta({"file": prior.get("file"), "line": prior.get("line")}, proof):
                candidates.append(i)
        if not candidates:
            return list(priors), []
        raw = await self._finding_sources(repo, pr, head, [priors[i] for i in candidates])
        kept: list[dict] = []
        cleared: list[dict] = []
        for i, prior in enumerate(priors):
            if i not in candidates:
                kept.append(prior)
                continue
            combined = raw.get(str(prior.get("file") or ""), ("", None))[1]
            if combined is None or combined is UNREADABLE or not isinstance(combined, str):
                kept.append(prior)
                continue
            grounded, _missing = ground_finding(prior, combined)
            (kept if grounded else cleared).append(prior)
        return kept, cleared

    async def _since_ranges(self, repo: str, history: list[dict], head: str) -> dict[str, dict | None]:
        """`{raised-at head: delta from it to `head`}` for the carried findings of the last
        substantive round (issue #131), so a `fixed` on a finding carried across rounds is
        checked against everything pushed since it was RAISED. Only heads older than the
        prior round's are fetched — that one is `ranges` already — and at most
        `SINCE_RANGES_LIMIT` compares are spent; an unread head falls back to `ranges`."""
        carried = next((r for r in reversed(history) if r.get("findings")), None)
        if carried is None:
            return {}
        # A finding with no `since` was raised by this round itself (`unaccounted_priors`
        # stamps it the same way) — which is older than the prior round when the rounds
        # after it raised nothing.
        raised_at = (
            str(f.get("since") or carried.get("head") or "") for f in carried["findings"] if isinstance(f, dict)
        )
        heads = [h for h in dict.fromkeys(raised_at) if h and h != history[-1].get("head")]
        return {h: await self._delta_ranges(repo, h, head) for h in heads[:SINCE_RANGES_LIMIT]}

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
                ".check_runs[] | {status: .status, conclusion: .conclusion, name: .name}",
            ],
        )
        if rc != 0:
            return None
        runs = gh_json_rows(out)
        if runs is None:
            return None  # unreadable ⇒ checks-unknown, which holds promotion
        # Our OWN check runs are not among the checks we are waiting on. Both the
        # promotion gate's `QA panel` and the dispatch lifecycle's `protoReview` (#95) sit
        # `in_progress` until we conclude them, so counting either makes this read
        # "pending" forever: the panel would hold on checks-pending, never clear, and
        # never conclude its own check (and a FAIL would post as a comment, not a block).
        # A gate deadlocked on itself.
        runs = [r for r in runs if r.get("name") not in OUR_CHECK_NAMES]
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

    async def _panel_owned_unresolved(self, repo: str, pr: int) -> int | None:
        """How many UNRESOLVED review threads the PANEL itself raised, or None when the
        threads (or our own identity) cannot be read.

        Server-authoritative and author-based: a thread is the panel's iff its ROOT comment
        was written by our own bot login. Threads other reviewers opened are external —
        they hold PROMOTION (fail-closed, via the total count) but are not the panel's
        findings, so they must not fail or misattribute the `QA panel` check (issue #105).

        None is load-bearing: it means "ownership unknown", which the check maps to a HOLD,
        never a failure. We never claim a thread as ours on an unreadable identity or an
        unreadable thread list — the safe direction is to say nothing, not to fail.
        """
        viewer = await self._viewer_login()
        if not viewer:
            return None  # we don't know who we are ⇒ cannot claim any thread as the panel's
        from .threads import fetch_threads

        try:
            nodes = await fetch_threads(self._run_gh, repo, pr)
        except Exception:  # noqa: BLE001 — an unreadable thread list is "unknown", never a failure
            log.exception("[pr-reviewer] thread-ownership fetch failed on %s#%s", repo, pr)
            return None
        if nodes is None:
            return None
        owned = 0
        for t in nodes:
            if not isinstance(t, dict) or t.get("isResolved"):
                continue
            comments = (t.get("comments") or {}).get("nodes") or []
            root = next((c for c in comments if isinstance(c, dict)), None)
            author = str(((root or {}).get("author") or {}).get("login") or "").strip().lower()
            if is_own_login(author, viewer):
                owned += 1
        return owned

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
            # A close is not a dispatch, but it is the last event this head will ever get:
            # give a still-waiting `QA panel` run its terminal state now (#153). Behind the
            # same allowlist gate as everything else — no GitHub call for an unmanaged repo.
            if action == "closed" and head_sha and not (bad_repo(repo) or (self.repos and repo not in self.repos)):
                await self._publish_qa_check(repo, head_sha, closed_run(), only_if_open=True)
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
            return await self._bounded_review(repo, pr, push_triggered=True)
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
            return await self._bounded_review(repo, pr, force=True)
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
            # Deliberately broader than `is_own_login`: this rail errs toward NOT reviewing.
            # Matching a plain account that shares our App's name only skips that PR; a
            # narrower match could let a misconfigured login review its own PR.
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
        # The PR's size rides on the dispatch / reviewed / exhaustion rows: a PR too large for
        # the lanes' budgets exhausts on every head (issue #116), and a "too large" threshold
        # can only be measured, not guessed — nothing recorded size against outcome before.
        size = {
            "changed_files": int(facts.get("changed_files") or len(paths)),
            "lines_changed": int(facts.get("additions") or 0) + int(facts.get("deletions") or 0),
        }
        fires, reasons = structural_trigger(**size, changed_paths=paths)
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
        verdicts = panel_rounds(ours)
        # A verdict carried to this head by an earlier reaffirm (#135) counts here — that is
        # what stops it being re-posted on every later event — but not as a round below.
        current = next((r for r in reversed(verdicts) if r["head"] == head), None)
        if current and not force:  # `force` = an operator summon disputing this verdict
            # Unchanged head with a posted verdict — reaffirm, don't re-spend the panel.
            self.telemetry.emit(REAFFIRM_HEAD, repo=repo, pr=pr, sha=head, verdict=current["verdict"])
            return f"reaffirmed:{current['verdict']}"
        history = spent_rounds(verdicts)
        # The identity of the base↔head content this event would have the panel review —
        # read ONCE, pinned to the resolved head SHA (issue #91). It serves two ends: the
        # byte-identical-diff reaffirm just below, and the marker stamp further down, so a
        # LATER rebase can reaffirm against THIS round. A summon still computes it for the
        # stamp but never reuses a verdict from it — `force` means the operator is disputing.
        diff_id = await self._pr_diff_id(repo, str(facts.get("base_ref") or ""), head)
        if history and not force:
            # A new head SHA whose base↔head diff is byte-identical to the most recent round
            # reaffirms that verdict without re-spending the panel — a rebase, a reworded
            # commit, or a moved-but-identical base. Fails closed to a fresh review when the
            # identity is unreadable, absent on the prior round, or different.
            reaffirmed = self._reaffirm_by_diff(repo, pr, head, history[-1], diff_id)
            if reaffirmed is not None:
                await self._record_reaffirmed(repo, pr, head, history[-1], diff_id)
                return reaffirmed
        prior = history[-1] if history else None
        round_number = len(history) + 1
        # Max-rounds cap: arm on the first push-triggered review that exceeds the limit so
        # the panel is never spent — post the notice once, then drop all subsequent pushes.
        # Backfill and summon calls pass push_triggered=False and are always exempt.
        # The budget counts complete rounds (#130): see `round_cap_reached`.
        if push_triggered and round_cap_reached(history, self.max_rounds):
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
            **size,
        )
        # Open the `protoReview` check the moment we commit to a panel — every drop/skip
        # gate is already behind us (r5), so this fires for exactly the reviews that run
        # (r1). The id threads through to `_post_verdict` / the exhaustion path, whichever
        # concludes it. Keyed on the SERVER-resolved head, never the webhook's `head_sha`.
        review_check_id = await self._start_review_check(repo, head)
        # Server-resolved refs ride along: finders pin code reads to the head SHA
        # and policy-doc reads to the base ref (a PR must not rewrite the rules it
        # is judged by). A host recipe without these declared just ignores them.
        inputs = {
            "pr": str(pr),
            "repo": repo,
            "head_sha": head,
            "base_ref": str(facts.get("base_ref") or ""),
        }
        if self.finder_timeout_s:
            inputs["finder_timeout"] = self.finder_timeout_s  # else the recipe's default (#93)
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
        # Stage boundaries whose findings payload never arrived (#113). An ABSENT array is
        # not an empty one, and parsing it as `[]` is what posted a clean PASS beneath a
        # verify note saying the findings may have been lost. It is handled like a failed
        # step: this loop is the natural retry point (a rerun is a fresh draw against
        # whatever starved the stage), and a round that still delivers nothing ends with
        # NO verdict below — never one that reads as clean.
        undelivered: list[str] = []
        for attempt in range(1, self.panel_retries + 2):
            last = attempt == self.panel_retries + 1
            # Bounded: a hung attempt must end as a failed attempt (retried, then reported
            # on the PR), not hold the round — and with it the PR's slot — forever.
            attempt_bound = asyncio.timeout(self.panel_attempt_timeout_s)
            try:
                async with attempt_bound:
                    result = await runner(recipe, inputs)
            except Exception as exc:  # noqa: BLE001 — the attempt's own TimeoutError included
                timed_out = isinstance(exc, TimeoutError) and attempt_bound.expired()
                why = f"timed out after {self.panel_attempt_timeout_s:g}s" if timed_out else type(exc).__name__
                if not last:
                    self.telemetry.emit(
                        "panel_retry",
                        repo=repo,
                        pr=pr,
                        sha=head,
                        attempt=attempt,
                        crashed="timeout" if timed_out else type(exc).__name__,
                    )
                    continue
                await self._conclude_review_check(
                    repo,
                    review_check_id,
                    FAILURE,
                    "QA panel timed out — no verdict" if timed_out else "QA panel crashed — no verdict",
                    f"The review run {'timed out' if timed_out else 'crashed'} on head `{head[:12]}` ({why}). "
                    f"No verdict was posted; push a fix or comment `@vera review` to re-trigger the review.",
                )
                await self._escalate(
                    f"pr-reviewer: review run {'timed out' if timed_out else 'crashed'} on {repo}#{pr} "
                    f"({why}: {exc}) — PR is UNREVIEWED.",
                    dedup_key=f"pr-reviewer-crash:{repo}#{pr}@{head[:7]}",
                )
                return "error:run-timed-out" if timed_out else "error:run-crashed"
            failed = list(result.get("failed") or [])
            steps_now = result.get("steps") if isinstance(result.get("steps"), dict) else {}
            # A finder that overran the model's context window (#176) is a coverage gap
            # the round carries — like a lane the engine timed out — not a failed round.
            # The other lanes delivered; retrying all five re-rolls the same dice (7 of 8
            # panels on one PR). The lane is moved to `degraded` with its error as the
            # output, so everything downstream reads it exactly as a timed-out lane.
            overran = overrun_lanes(failed, steps_now)
            if overran:
                failed = [s for s in failed if s not in overran]
                result = {
                    **result,
                    "failed": failed,
                    "degraded": [*(result.get("degraded") or []), *overran],
                    "overran": overran,
                }
                self.telemetry.emit("finder_overran", repo=repo, pr=pr, sha=head, lanes=overran, attempt=attempt)
            undelivered = (
                []
                if failed
                else undelivered_stages(
                    str(result.get("output") or ""), steps_now, result.get("degraded"), STRUCTURAL_GAP_MARKERS
                )
            )
            if not failed and not undelivered:
                break
            if not last:
                retry_why = {"failed": failed} if failed else {"undelivered": undelivered}
                self.telemetry.emit("panel_retry", repo=repo, pr=pr, sha=head, attempt=attempt, **retry_why)
        if failed:
            # Retries spent: D3's other branch. No verdict, operator escalation — and
            # the sweep's backfill will try again on a later pass (issue #17), so an
            # exhausted PR is no longer abandoned for good.
            # THE key closure (#95): this is the state that used to leave NO signal at
            # all — a red X here is the difference between "exhausted" and "approved".
            await self._conclude_review_check(
                repo,
                review_check_id,
                FAILURE,
                "QA panel exhausted — no verdict",
                f"The review panel failed on head `{head[:12]}` after {self.panel_retries + 1} "
                f"attempt(s) (step(s) {', '.join(str(s) for s in failed)}). No verdict was posted; "
                f"push a fix to re-trigger the review.",
            )
            await self._escalate(
                f"pr-reviewer: panel step(s) {failed} failed on {repo}#{pr} "
                f"after {self.panel_retries + 1} attempt(s) — no verdict posted; PR is UNREVIEWED.",
                dedup_key=f"pr-reviewer-exhaustion:{repo}#{pr}@{head[:7]}",
                repo=repo,
                pr=pr,
                head_sha=head,
            )
            self.telemetry.emit(
                "exhaustion", repo=repo, pr=pr, sha=head, failed=failed, attempts=self.panel_retries + 1, **size
            )
            return "error:panel-exhausted"
        if undelivered:
            # Absent ≠ empty (#113): the panel ran, but a stage boundary delivered NO findings
            # array on every attempt. That is the exhaustion outcome, not a verdict: nothing is
            # posted, the protoReview check goes red, the operator is told, and the sweep's
            # backfill retries the head later (it has no verdict). Telemetered as an
            # exhaustion so the eval's unreviewed-PR count sees it; `undelivered` says why.
            stages = ", ".join(undelivered)
            attempts = self.panel_retries + 1
            await self._conclude_review_check(
                repo,
                review_check_id,
                FAILURE,
                "QA panel incomplete — no verdict",
                f"The review panel ran on head `{head[:12]}` but delivered no findings payload at "
                f"stage(s) {stages} after {attempts} attempt(s). An absent payload is not a clean "
                f"result, so no verdict was posted; push a fix or comment `@vera review` to re-trigger the review.",
            )
            await self._escalate(
                f"pr-reviewer: panel delivered no findings payload ({stages}) on {repo}#{pr} "
                f"after {attempts} attempt(s) — no verdict posted; PR is UNREVIEWED.",
                dedup_key=f"pr-reviewer-incomplete:{repo}#{pr}@{head[:7]}",
                repo=repo,
                pr=pr,
                head_sha=head,
            )
            self.telemetry.emit(
                "exhaustion",
                repo=repo,
                pr=pr,
                sha=head,
                failed=[],
                undelivered=undelivered,
                attempts=attempts,
                **size,
            )
            return "error:panel-incomplete"

        result = await self._rerun_contradicted_verify(runner, recipe, inputs, result, repo=repo, pr=pr, head=head)

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
        structural_out = str(steps_out.get("find_structural") or "")
        # Judge only the lanes this recipe actually ran, against the contract it actually
        # gave them. The small-diff `code-review` recipe has no structural seat and never
        # asks its finders for a FINDER_STATUS line, so reading those absences as gaps
        # marked EVERY small-diff review incomplete — all four finders "did not complete",
        # structural "unavailable" — which held its promotion and would cap it at WARN
        # below. A result with no `steps` at all (an older host) says nothing either way.
        structural_unavailable = "find_structural" in steps_out and (
            # Either marker: a relay that OBEYS the tool writes the Gap line and an empty
            # array, not the tool's own prefix — which read as a clean structural pass, so
            # 33 rounds with protoPatch down were recorded complete and 22 auto-approved.
            mentions_any(structural_out, STRUCTURAL_GAP_MARKERS)
            or ("find_structural" not in degraded and not structural_relay_ok(structural_out, STRUCTURAL_GAP_MARKERS))
        )
        # The four LLM finders' own completeness (#117): a finder that ran to a
        # normal-looking finish on garbage input (every file read 404ing, a crash mid-
        # response, a turn-limit exit) is invisible to `degraded`/`failed` exactly like
        # the structural relay case above — it never told the engine anything went
        # wrong. `finder_completed` reads each finder's required status marker instead
        # of trusting an empty findings array at face value. Steps the engine already
        # cut off at their timeout are skipped here — they're already coverage gaps.
        incomplete_finders = [
            s
            for s in LLM_FINDER_STEPS
            if recipe in STATUS_LINE_RECIPES
            and s in steps_out
            and s not in degraded
            and not finder_completed(str(steps_out.get(s) or ""))
        ]
        complete = not structural_unavailable and not degraded and not incomplete_finders
        lanes = len({str(s) for s in (*steps_out, *degraded) if str(s).startswith(FINDER_STEP_PREFIX)})
        output = str(result.get("output") or "")
        # The raw output is read for BLOCKS and never published as text (protoAgent#2439
        # — see verdicts.py). `reported` is what the panel said this round and what the
        # body records; `findings` is the confined subset the verdict is computed from.
        reported = self._parse_findings(output)
        # A re-listing of a prior minor/nit with no verdict and no fresh quote is the panel's
        # memory leaking into its findings, not a finding (issue #204): it is normalized to a
        # carried, `uncertain` row BEFORE grounding and the verified check see it, so the
        # original quote is what gets grounded and a verdict-less echo does not read as a
        # verifier gap. Applied to `reported` so the recorded body carries the same shape.
        reported, relisted = normalize_relisted_priors(reported, history)
        if relisted:
            self.telemetry.emit(
                "relisted_prior",
                repo=repo,
                pr=pr,
                sha=head,
                round=round_number,
                findings=[
                    {"file": str(f.get("file") or ""), "line": f.get("line"), "severity": str(f.get("severity") or "")}
                    for f in relisted
                ],
            )
        # A verify step that handed nothing back on a CLEAN round (#151). With findings,
        # `verification_ran` already catches it; with none it cannot, and the round posted
        # "came back clean" above a report saying the verifier never ran. A gap, not a
        # voided round: nothing went unverified, so re-running five finders buys nothing —
        # but the body must say so and the dead step must be countable.
        verify_undelivered = (
            not reported
            and "verify" in steps_out
            and "verify" not in degraded
            and not verify_delivered(str(steps_out.get("verify") or ""))
        )
        # The same signals as one record, for the coverage cap and note below (#117).
        overran = [str(s) for s in (result.get("overran") or [])]
        gaps = coverage_gaps(
            degraded,
            incomplete_finders,
            structural_unavailable,
            outage_reason(structural_out),
            verify_undelivered,
            overran=overran,
        )
        brief, brief_found = extract_brief(output)
        # A report that dropped its brief (#168: 5 of 150 posted reviews, every one a clean
        # round) leaves the author a PASS with no word on what was looked at. The
        # synthesizer's brief is the same round's, already delimited — but it is written
        # BEFORE verification, so it is only borrowed when it cannot disagree with the
        # verdict: the synthesizer carried no findings and neither does the report.
        brief_source = "report" if brief_found else ""
        if not brief_found and not reported:
            synthesized = str(steps_out.get("synthesize") or "")
            if not self._parse_findings(synthesized):
                brief, brief_found = extract_brief(synthesized)
                brief_source = "synthesize" if brief_found else ""
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
        grounded_findings, ungrounded, unreadable = [], [], []
        grounding_checked = 0
        if self.grounding_enabled and findings:
            raw = await self._finding_sources(repo, pr, head, findings)
            blobs = {f: v[0] for f, v in raw.items()}
            grounding_sources = {f: v[1] for f, v in raw.items()}
            grounded_findings, ungrounded, unreadable = apply_grounding(findings, grounding_sources)
            grounding_checked = len(findings)
            findings = grounded_findings
            findings = correct_line_numbers(findings, blobs)
        if ungrounded:
            self.telemetry.emit("ungrounded", repo=repo, pr=pr, sha=head, round=round_number, downgraded=ungrounded)
        if unreadable:
            # A failed head read is a DEGRADATION, not a downgrade — surfaced so a
            # could-not-verify pass is never mistaken for a clean one (issue #109).
            self.telemetry.emit(
                "source_unavailable", repo=repo, pr=pr, sha=head, round=round_number, findings=unreadable
            )
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
        since_ranges = await self._since_ranges(repo, history, head) if dispositions and prior else None
        unaccounted = unaccounted_priors(history, dispositions, ranges=ranges, since_ranges=since_ranges)
        # A carried prior whose quoted evidence is GONE at head, on a line the delta since
        # it was raised actually touched, was fixed — that is the same read `ground_finding`
        # applies to a fresh finding, and it is stronger than a model's `fixed` claim. Without
        # it a fixed major carries round after round with "no evidence of fix", because the
        # fresh diff gives the panel nothing to disposition (issue #196, plugin#193 r4–r5).
        evidence_gone: list[dict] = []
        if unaccounted and self.grounding_enabled:
            unaccounted, evidence_gone = await self._clear_by_evidence(
                repo, pr, head, unaccounted, ranges=ranges, since_ranges=since_ranges
            )
            if evidence_gone:
                self.telemetry.emit(
                    "carried_prior_cleared",
                    repo=repo,
                    pr=pr,
                    sha=head,
                    round=round_number,
                    reason="evidence-gone",
                    findings=[
                        {
                            "file": str(m.get("file") or ""),
                            "line": m.get("line"),
                            "severity": str(m.get("severity") or ""),
                        }
                        for m in evidence_gone
                    ],
                )
        # The two guards are a fallback chain, not a belt-and-braces pair. When the panel
        # HAS dispositioned its priors, that statement is the authority — re-applying the
        # clean-PASS heuristic on top would hold a block the panel just explained, making
        # the explicit contract worthless exactly where it matters most.
        dropped_finding = (
            unexplained_clearance(history, verdict, findings) if (self.hold_unexplained and not dispositions) else None
        )
        # The coverage cap (#117) comes AFTER every findings and history rule, so none of
        # them sees it: convergence would otherwise relieve the WARN straight back to PASS,
        # and the clearance guard above judges the finding-based verdict, so a capped
        # zero-finding round holds a standing block exactly as a clean one would. A gap
        # never softens a FAIL and never withholds the verdict — it caps PASS at WARN.
        finding_verdict = verdict
        verdict = coverage_verdict(verdict, gaps)
        trailer = (
            render_notes_section(notes) + render_grounding_footnote(ungrounded) + render_unreadable_footnote(unreadable)
        )
        if degraded:
            trailer += render_degraded_note(degraded)
        if incomplete_finders:
            trailer += render_incomplete_note(incomplete_finders)
        if evidence_gone:
            trailer += render_evidence_gone_note(evidence_gone)
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
            coverage_gaps=gaps,
            lanes=lanes,
            review_check_id=review_check_id,
            # Did anything actually CHECK these findings? Empty over a clean panel is
            # normal; empty over real findings means the verdict is ungrounded, and the
            # promotion gate must not auto-approve it (mirrors `complete` one step up).
            verified=verification_ran(str(steps_out.get("verify") or ""), reported),
            # Stamp the reviewed base↔head diff identity into the marker (issue #91) so a
            # later rebased/reworded head with a byte-identical diff reaffirms this verdict.
            diff_id=diff_id,
        )
        if posted:
            # Structural claims the verifier refuted this round: remembered for the repo, so
            # the next PR touching the file does not spend a verify round on them (#190).
            remembered = self.refutations.record(repo, reported, pr=pr, head=head)
            if remembered:
                log.info("[pr-reviewer] %s#%s remembered %d refuted structural claim(s)", repo, pr, remembered)
        self.telemetry.emit(
            "reviewed",
            repo=repo,
            pr=pr,
            sha=head,
            recipe=recipe,
            verdict=verdict,
            round=round_number,
            **size,
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
            # The report dropped its brief and the synthesizer's stood in (#168) — still the
            # report step off its contract, so it stays countable.
            brief_borrowed=(brief_source == "synthesize") or None,
            report_truncated=truncated or None,
            overran=overran or None,
            grounding_checked=grounding_checked,
            grounding_downgraded=len(ungrounded),
            grounding_unreadable=len(unreadable),
            dispositions=len(dispositions),
            unaccounted=len(unaccounted),
            latency_s=round(elapsed, 1),
            step_s=timings or None,
            slowest_step=(max(timings, key=timings.get) if timings else None),
            degraded=degraded or None,
            incomplete_finders=incomplete_finders or None,
            complete=complete,
            structural_unavailable=structural_unavailable or None,
            # WHY the structural lane was out, as a countable class (#205): a clawpatch
            # per-request gateway timeout, our own budget SIGKILL, auth, a missing binary…
            structural_reason=(classify_outage(outage_reason(structural_out)) or None)
            if structural_unavailable
            else None,
            verify_undelivered=verify_undelivered or None,
            # A clean PASS posted as WARN because a lane did not deliver a full pass (#117).
            coverage_capped=(verdict != finding_verdict) or None,
            posted=posted,
            shadow=self.shadow,
        )
        return f"reviewed:{verdict}" if posted else f"error:post-failed:{verdict}"

    @staticmethod
    def _parse_findings(output: str) -> list[dict]:
        """The report's findings, for the verdict — never FEWER than the report carries.

        The host's `parse_findings` is the normal reader (it coerces and picks the best
        array). But its fence pattern ends a block at the first ``` it meets, including
        one inside a JSON string, so a finding quoting stacked backticks makes it lose
        the findings block; when an earlier block (the dispositions array) still parses,
        its bare-array fallback never runs and it returns ZERO findings — a clean PASS
        over a report that carries a finding. Until v0.45.2 the plugin's own delivery
        check tripped on the same text and discarded the round; #162 fixed that check,
        which left this reader as the only thing standing between that report and a PASS.

        So the plugin reads the report too, with the line-anchored `fenced_blocks`, and
        takes whichever reader found MORE findings. Dropping a finding is the one failure
        this must not have; an extra look costs nothing.
        """
        from .verdicts import extract_findings_json

        own: list[dict] = []
        text = extract_findings_json(output)
        if text:
            try:
                own = [f for f in json.loads(text) if isinstance(f, dict) and ("claim" in f or "severity" in f)]
            except json.JSONDecodeError:
                own = []
        try:
            from graph.review.findings import parse_findings

            host = [f.to_dict() for f in parse_findings(output)]
        except Exception:  # noqa: BLE001 — host-free: the plugin's own read is all there is
            return own
        if len(own) > len(host):
            log.warning(
                "[pr-reviewer] the host parser read %d finding(s) where the report carries %d; using the report's",
                len(host),
                len(own),
            )
            return own
        return host

    # ── stale-head demotion: the PR moved while the panel ran (issue #82) ──────

    async def _stale_head_delta(self, repo: str, pinned: str, current: str) -> tuple[dict, int] | None:
        """(changed ranges, commit count) for pinned→current, or None (unreadable).

        A separate read from `_delta_ranges` because the synthesis header needs the
        COMMIT COUNT too, and both must come from the same compare — a count from one
        read and ranges from another could straddle yet another push.
        """
        rc, out, _err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/compare/{pinned}...{current}",
                "--jq",
                "{commits: .total_commits, files: [.files[]? | {filename: .filename, patch: .patch}]}",
            ],
        )
        if rc != 0:
            return None
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
            return None
        commits = payload.get("commits")
        return delta_ranges(payload["files"]), (commits if isinstance(commits, int) and commits >= 0 else 0)

    async def _stale_head_guard(self, repo: str, pr: int, head: str, findings: list[dict]) -> tuple[list[dict], str]:
        """Re-resolve the PR's head at POST time → (findings, synthesis header or "").

        The panel runs for minutes and the round pins `head` at dispatch, so a fix
        commit pushed while the finders run leaves the verdict verified against a head
        the PR no longer has. The promotion gate already refuses that verdict
        (`verdict_head != head_sha`), so nothing auto-approves off it — but the POSTED
        review still tells a human `confirmed` about code the push may have just fixed
        (protoAgent#2854 r2 and #2868 r2, one on an already-merged PR). Cheapest viable
        fix: post-filter, don't cancel — the round is already paid for.

        Resolved server-side (`_pr_facts`, the same read the round opened with) — never
        a model-supplied ref. Every failure DEGRADES to posting with a note, never
        raising: a lost verdict is worse than a stale-marked one (the #72 posture). And
        demotion itself fails CLOSED like every other relief here: an unreadable delta
        demotes nothing — the head having moved is stated, the findings keep their
        authority (the `converge` posture: no relief without a readable delta).
        """
        facts = await self._pr_facts(repo, pr)
        current = str(facts.get("head") or "") if facts else ""
        if not current:
            self.telemetry.emit("stale_head", repo=repo, pr=pr, sha=head, current=None, demoted=0)
            return findings, (
                f"The PR's current head could not be resolved at post time — this review was "
                f"verified against `{head[:12]}` and may not reflect later pushes."
            )
        if current == head:
            return findings, ""  # the common case: byte-identical body, no note, no event
        delta = await self._stale_head_delta(repo, head, current)
        if delta is None:
            self.telemetry.emit(
                "stale_head", repo=repo, pr=pr, sha=head, current=current, demoted=0, delta="unreadable"
            )
            return findings, (
                f"PR advanced during this round (`{head[:12]}` → `{current[:12]}`) and the delta "
                f"could not be read — findings were verified against the older head and some may "
                f"already be addressed."
            )
        ranges, commits = delta
        demoted = 0
        if isinstance(findings, list):
            findings, demoted = demote_stale_findings(findings, ranges)
        self.telemetry.emit("stale_head", repo=repo, pr=pr, sha=head, current=current, commits=commits, demoted=demoted)
        return findings, (
            f"PR advanced {commits} commit(s) during this round (`{head[:12]}` → `{current[:12]}`); "
            f"{demoted} finding(s) in the delta were demoted to *possibly addressed*."
        )

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
        review_check_id: int | None = None,
        verified: bool = True,
        diff_id: str | None = None,
        coverage_gaps: dict[str, str] | None = None,
        lanes: int = 0,
        reaffirmed_from: str = "",
    ) -> bool:
        # Immediately before posting — the last moment a mid-round push can be caught.
        # The marker keeps the PINNED head on purpose: the round ran against it, and
        # rewriting it would tell the promotion gate the verdict covers code it never saw.
        findings, stale_note = await self._stale_head_guard(repo, pr, head, findings)
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
            verified=verified,
            stale_note=stale_note,
            diff_id=diff_id or "",
            coverage_gaps=coverage_gaps,
            lanes=lanes,
            reaffirmed_from=reaffirmed_from,
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
            # A verdict that never landed is, for the gate, an exhaustion: conclude the
            # check red rather than leave it dangling `in_progress` (which would read as
            # "still reviewing" — the very ambiguity #95 exists to remove).
            await self._conclude_review_check(
                repo,
                review_check_id,
                FAILURE,
                f"{verdict} verdict — post refused",
                f"The panel returned **{verdict}** for head `{head[:12]}`, but GitHub refused the "
                f"review post after {attempts} attempt(s), so no verdict is recorded on the PR.",
            )
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
        # The verdict landed — conclude the check with it (r2/r3). PASS/WARN clear the
        # gate; a FAIL holds it. This runs AFTER the post, so a check-write failure can
        # never cost the verdict (r7). The verdict text rides in the summary (r6).
        await self._conclude_review_check(
            repo,
            review_check_id,
            SUCCESS if verdict != FAIL else FAILURE,
            f"QA panel: {verdict}",
            self._review_check_summary(verdict, findings, coverage_gaps),
        )
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

    async def _rerun_contradicted_verify(
        self, runner, recipe: str, inputs: dict, result: dict, *, repo: str, pr: int, head: str
    ) -> dict:
        """A verifier that answered `nothing-to-verify` over a synthesis carrying findings
        (#167) is re-run ALONE: the finders' and synthesizer's outputs are seeded, so only
        `verify` and `report` run again — seconds, where the panel is minutes. Bounded by
        `verify_reruns`. A host whose runner cannot seed (protoAgent < #3571) still counts
        the contradiction; either way, a round that stays contradicted posts exactly as
        today (`verified=false`, held), never as clean.
        """
        steps = result.get("steps") if isinstance(result.get("steps"), dict) else {}
        if "verify" not in steps or "synthesize" not in steps:
            return result
        synthesized = self._parse_findings(str(steps.get("synthesize") or ""))
        if not verifier_contradicts_synthesis(str(steps.get("verify") or ""), synthesized):
            return result
        if not _accepts_keyword(runner, "seed_outputs") or self.verify_reruns <= 0:
            self.telemetry.emit(VERIFY_CONTRADICTED, repo=repo, pr=pr, sha=head, attempt=0, rerun=False, cleared=False)
            return result
        seeded = {k: str(v) for k, v in steps.items() if k not in ("verify", "report")}
        # Not a byte-for-byte repeat (#182): the identical request reproduced the identical
        # contradiction (2 of 2, and #384's separate panels agree), while the same payload
        # in any other shape reads fine. The re-run hands the verifier the SAME array,
        # restated — an explicit count first, no delegation banner, no prose brief.
        seeded["synthesize"] = restate_findings(synthesized)
        for attempt in range(1, self.verify_reruns + 1):
            try:
                async with asyncio.timeout(self.panel_attempt_timeout_s):
                    again = await runner(recipe, inputs, seed_outputs=seeded)
            except Exception as exc:  # noqa: BLE001 — a failed re-run leaves the original round
                log.warning("[pr-reviewer] %s#%s verify re-run %d failed: %s", repo, pr, attempt, exc)
                break
            again_steps = again.get("steps") if isinstance(again.get("steps"), dict) else {}
            if again.get("failed") or "verify" not in again_steps or not again.get("output"):
                break
            cleared = not verifier_contradicts_synthesis(str(again_steps["verify"] or ""), synthesized)
            if cleared:
                # Only a re-run that VERIFIED replaces the round's verify and report. A re-run
                # that contradicted again wrote its report over a "nothing-to-verify" reply
                # and, on protoAgent#3591, carried 1 of the 2 findings the original carried —
                # adopting it would drop a finding on the fail-closed path. The original
                # round (all findings, `verified=false`) stands until something verifies.
                result = {
                    **result,
                    "output": again["output"],
                    "steps": {**steps, **{k: v for k, v in again_steps.items() if k in ("verify", "report")}},
                    "timings": {**(result.get("timings") or {}), **(again.get("timings") or {})},
                    "degraded": [*(result.get("degraded") or []), *(again.get("degraded") or [])],
                }
                steps = result["steps"]
            self.telemetry.emit(
                VERIFY_CONTRADICTED,
                repo=repo,
                pr=pr,
                sha=head,
                attempt=attempt,
                rerun=True,
                restated=True,
                cleared=cleared,
            )
            if cleared:
                return result
        # Still contradicted after the restated re-run (#189: 1 in 11). Every fresh round
        # on this shape has verified — the contradiction is a property of the verify turn,
        # not the PR — and the sweep re-dispatches nothing for a head that holds a verdict,
        # so a held PASS here waits for a person. One fresh panel, counted like any attempt.
        if not self.verify_fallback_panel:
            return result
        try:
            async with asyncio.timeout(self.panel_attempt_timeout_s):
                fresh = await runner(recipe, inputs)
        except Exception as exc:  # noqa: BLE001 — a failed fallback leaves the restated round
            log.warning("[pr-reviewer] %s#%s verify fallback panel failed: %s", repo, pr, exc)
            return result
        fresh_steps = fresh.get("steps") if isinstance(fresh.get("steps"), dict) else {}
        if (
            fresh.get("failed")
            or "verify" not in fresh_steps
            or "synthesize" not in fresh_steps
            or not fresh.get("output")
        ):
            return result
        fresh_synth = self._parse_findings(str(fresh_steps.get("synthesize") or ""))
        cleared = not verifier_contradicts_synthesis(str(fresh_steps.get("verify") or ""), fresh_synth)
        self.telemetry.emit(
            VERIFY_CONTRADICTED,
            repo=repo,
            pr=pr,
            sha=head,
            attempt=self.verify_reruns + 1,
            rerun=True,
            fallback=True,
            cleared=cleared,
        )
        # The fresh panel is a whole round: its finders, synthesis and report replace the
        # contradicted round's — whichever way its verifier went, it is the newer evidence.
        return fresh

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
        # Strictest-verdict-wins for the CURRENT head (issue #89). When the newest round
        # is for the current head, re-derive its verdict as the STRICTEST across EVERY
        # panel round that names this head. `panel_rounds` folds a head's reviews into
        # one round keeping whichever GitHub returned LAST — a last-writer-wins race when
        # two reviews land concurrently for the same head. A PASS arriving after a
        # co-landed FAIL would otherwise shadow it and auto-approve straight past the
        # blocker. Fails closed: FAIL > WARN > PASS, the harsher verdict wins the tie. A
        # stale-head `latest` is left untouched — it holds regardless of its verdict.
        # A zero-finding incomplete round (the coverage cap alone) drops out once a
        # COMPLETE round exists for the head, so one blind lane cannot hold the head at
        # hold:incomplete-coverage forever (qaEngineer#59) — see `strictest_head_round`.
        if latest is not None and latest["head"] == head:
            latest = strictest_head_round(ours, head) or latest
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
            verified=bool(clear.get("verified", True)) if clear else True,
        )
        backoff_key = f"{repo}#{pr}@{head}"
        if self._promote_failures.get(backoff_key, 0) >= PROMOTE_MAX_FAILURES:
            self.telemetry.emit("promotion", repo=repo, pr=pr, sha=head, decision=HOLD_PROMOTE_BACKOFF)
            return HOLD_PROMOTE_BACKOFF
        decision = promotion_decision(obs)
        self.telemetry.emit("promotion", repo=repo, pr=pr, sha=head, decision=decision)
        # Promotion holds on ANY open thread (fail-closed, above), but the `QA panel` check
        # speaks only for the PANEL — so a thread another reviewer opened must not fail it
        # (issue #105). Split the open threads by server-side authorship into the panel's
        # own vs. everyone else's, and hand the OWN count to the mapping. Read only when the
        # decision actually turns on threads, so the common paths pay nothing for it.
        panel_unresolved = await self._panel_owned_unresolved(repo, pr) if decision == HOLD_THREADS_UNRESOLVED else None
        # The gate GitHub can enforce (checks.py). Written from the SAME decision that
        # drives approve-on-green — one judgement, published two ways — and before the
        # early return, so a hold is what the check reports too. It deliberately does not
        # wait on the APPROVE below: the check states what the PANEL concluded about this
        # head, which is true whether or not the courtesy review posts.
        await self._publish_qa_check(
            repo,
            head,
            check_for(
                decision,
                # The latest verdict FOR THIS HEAD — a stale one is not this head's.
                verdict=(latest["verdict"] if latest and latest["head"] == head else None),
                unresolved=obs.unresolved_threads,
                panel_unresolved=panel_unresolved,
            ),
        )
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

    # ── the dispatch-path `protoReview` check (#95) ───────────────────────────

    async def _start_review_check(self, repo: str, sha: str) -> int | None:
        """Open an `in_progress` `protoReview` check run for this head → its id, or None.

        Posted for every panel that proceeds past the drop/skip gates, regardless of
        shadow/ownership: unlike the `QA panel` promotion gate, this check is driven by
        the review itself (the same `_review` call opens AND concludes it), so it never
        dangles and is safe to require even in shadow mode.

        Degrades, never raises (r7): a failure is logged at WARNING and returns None; the
        review is the job and must not be lost to bookkeeping. A None id makes every later
        `_conclude_review_check` a no-op, so the whole lifecycle no-ops without the App
        permission rather than half-writing a check that can never conclude.
        """
        rc, out, err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/check-runs",
                "-X",
                "POST",
                "-f",
                f"name={REVIEW_CHECK_NAME}",
                "-f",
                f"head_sha={sha}",
                "-f",
                f"status={IN_PROGRESS}",
                "--jq",
                ".id",
            ],
            timeout=60,
        )
        if rc != 0:
            # A missing `checks: write` on the App installation lands here; name the
            # permission once you read the log rather than leaving a bare 403.
            log.warning(
                "[pr-reviewer] could not open the %s check on %s@%s (needs App permission `Checks: read & write`): %s",
                REVIEW_CHECK_NAME,
                repo,
                sha[:7],
                err[-200:],
            )
            return None
        try:
            check_run_id = int(str(out).strip())
        except (TypeError, ValueError):
            log.warning(
                "[pr-reviewer] %s check create on %s@%s returned no id (%r) — cannot conclude it",
                REVIEW_CHECK_NAME,
                repo,
                sha[:7],
                (out or "")[:80],
            )
            return None
        self.telemetry.emit("review_check", repo=repo, sha=sha, status=IN_PROGRESS, check_run_id=check_run_id)
        return check_run_id

    async def _conclude_review_check(
        self, repo: str, check_run_id: int | None, conclusion: str, title: str, summary: str
    ) -> None:
        """PATCH the `protoReview` check to completed+conclusion. Degrades, never raises (r7).

        `check_run_id is None` (the open never succeeded) is a no-op — the verdict still
        posted, which is the job; the check is bookkeeping. A failed PATCH is logged at
        WARNING and swallowed for the same reason.
        """
        if check_run_id is None:
            return
        rc, _out, err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/check-runs/{check_run_id}",
                "-X",
                "PATCH",
                "-f",
                f"status={COMPLETED}",
                "-f",
                f"conclusion={conclusion}",
                "-f",
                f"output[title]={title}",
                "-f",
                f"output[summary]={summary}",
            ],
            timeout=60,
        )
        if rc != 0:
            log.warning(
                "[pr-reviewer] could not conclude the %s check (%s) on %s via id %s: %s",
                REVIEW_CHECK_NAME,
                conclusion,
                repo,
                check_run_id,
                err[-200:],
            )
            return
        self.telemetry.emit(
            "review_check", repo=repo, status=COMPLETED, conclusion=conclusion, check_run_id=check_run_id
        )

    @staticmethod
    def _review_check_summary(verdict: str, findings: list[dict] | None, gaps: dict[str, str] | None = None) -> str:
        """The `output.summary` for a concluded check — carries the verdict text (r6), and
        says so when a lane did not deliver a full pass, so a green check never reads as
        full coverage it did not have (#117)."""
        n = len([f for f in (findings or []) if isinstance(f, dict)])
        tail = f" ({n} finding{'s' if n != 1 else ''})" if n else ""
        coverage = (
            f" Coverage was incomplete — {', '.join(f'`{s}`' for s in gaps)} did not complete a full "
            "pass, so this is not a clean pass."
            if gaps
            else ""
        )
        if verdict == FAIL:
            return (
                f"The QA panel returned **{verdict}** — blocking defects stand against this "
                f"head{tail}. See the review for details; push a fix to clear it.{coverage}"
            )
        return f"The QA panel returned **{verdict}**{tail}. See the review for details.{coverage}"

    async def _publish_qa_check(self, repo: str, sha: str, run: CheckRun, *, only_if_open: bool = False) -> None:
        """Publish (or update) this head's `QA panel` check run. Degrades, never raises.

        Idempotent by state, not by call: the sweep re-evaluates every open PR every few
        minutes, and re-POSTing would stack a new check run per pass — GitHub keeps them
        all, so a week-old PR would carry hundreds and the PR's check list would become
        unreadable. So: read ours for this SHA, PATCH it when what we would say changed,
        and write nothing at all when it hasn't.

        `only_if_open` is for the close path: it may finish a run that is still waiting,
        and must never create one or overwrite a verdict that already concluded.
        """
        if not self.qa_check:
            return
        rc, out, _err = await self._run_gh(
            [
                "api",
                f"repos/{repo}/commits/{sha}/check-runs?check_name={quote(CHECK_NAME)}",
                "--jq",
                ".check_runs[0] | {id: .id, status: .status, conclusion: .conclusion, title: .output.title}",
            ],
        )
        existing: dict = {}
        if rc == 0 and out.strip() and out.strip() != "null":
            try:
                parsed = json.loads(out)
            except json.JSONDecodeError:
                parsed = None
            # Anything but an object means the read didn't answer the question we asked
            # (a jq that matched nothing, a shape change) — treat it as "no run yet" and
            # create one, rather than subscripting whatever came back.
            existing = parsed if isinstance(parsed, dict) else {}
        if only_if_open and (not existing.get("id") or existing.get("status") == COMPLETED):
            return
        if (
            existing.get("status") == run.status
            and (existing.get("conclusion") or None) == run.conclusion
            and (existing.get("title") or "") == run.title
        ):
            return  # already says exactly this
        fields = [
            "-f",
            f"status={run.status}",
            "-f",
            f"output[title]={run.title}",
            "-f",
            f"output[summary]={run.summary}",
        ]
        if run.conclusion:
            fields += ["-f", f"conclusion={run.conclusion}"]
        if existing.get("id"):
            args = ["api", f"repos/{repo}/check-runs/{existing['id']}", "-X", "PATCH", *fields]
        else:
            args = [
                "api",
                f"repos/{repo}/check-runs",
                "-X",
                "POST",
                "-f",
                f"name={CHECK_NAME}",
                "-f",
                f"head_sha={sha}",
                *fields,
            ]
        rc, _out, err = await self._run_gh(args, timeout=60)
        if rc != 0:
            # A missing `checks: write` on the App installation lands here on every pass;
            # say which permission, once you read the log, rather than a bare 403.
            log.warning(
                "[pr-reviewer] could not publish the %s check on %s@%s (needs App permission "
                "`Checks: read & write`): %s",
                CHECK_NAME,
                repo,
                sha[:7],
                err[-200:],
            )
            return
        self.telemetry.emit(
            "qa_check", repo=repo, sha=sha, status=run.status, conclusion=run.conclusion or "", title=run.title
        )

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
        every guard (self-authored, eligibility, cooldown, in-flight) still applies.

        The sweep is already bounded to `backfill_per_pass` reviews and runs them
        serially, but those panels share a process with the webhook's — so a backfill
        firing during a webhook burst would push total concurrency past the cap. When
        `build_routers` injected a `panel_sem`, the backfill queues behind the SAME
        cross-PR bound (#96); with none injected it runs unbounded, as before.
        """
        decision = self.chokepoint.admit(repo, pr, head)
        if decision != "accept":
            self.telemetry.emit("drop", repo=repo, pr=pr, sha=head, reason=decision, action=BACKFILL_ACTION)
            return f"drop:{decision}"
        self.telemetry.emit("backfill", repo=repo, pr=pr, sha=head)
        # `locked()` is True exactly when no slot is free, i.e. this panel WILL wait —
        # the queue-depth signal the operator reads out of the panel stats.
        if self.panel_sem is not None and self.panel_sem.locked():
            self.telemetry.emit("queued", kind=BACKFILL_ACTION, repo=repo, pr=pr, sha=head)
        slot = self.panel_sem if self.panel_sem is not None else contextlib.nullcontext()
        try:
            async with slot:
                return await self._bounded_review(repo, pr)
        finally:
            self.chokepoint.done(repo, pr)

    def _detach_backfill(self, repo: str, pr: int, head: str) -> str:
        """Start a backfill panel WITHOUT waiting for it, so the sweep pass moves on.

        A panel takes 5–15 minutes, plus however long it waits for a `panel_sem` slot. Run
        inline, one backfill froze the whole level pass for that long — and the pass is also
        the ONLY thing that re-gates a FAIL or promotes a clear verdict, for every PR in every
        repo. Under a review flood the sweep went from ~80 gate decisions per 10 minutes to
        none: complete PASS verdicts sat unpromoted for 20+ minutes behind someone else's
        backfill, and a re-gate — "the merge-race is live NOW" — waited just as long.

        Safe to detach: `backfill_review` admits through the chokepoint before its first
        await, so a webhook or a later pass reaching the same PR drops as `in-flight`, never
        a second panel. Outstanding backfills are capped at `backfill_per_pass`.
        """
        if len(self._backfills) >= max(1, self.backfill_per_pass):
            return BACKFILL_DEFERRED
        task = asyncio.ensure_future(self.backfill_review(repo, pr, head))
        self._backfills.add(task)

        def _settled(t: asyncio.Task) -> None:
            self._backfills.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("[pr-reviewer] detached backfill of %s#%s failed", repo, pr, exc_info=t.exception())

        task.add_done_callback(_settled)
        return BACKFILL_STARTED

    async def drain_backfills(self) -> None:
        """Wait for every detached backfill to settle — shutdown, and tests."""
        while self._backfills:
            await asyncio.gather(*list(self._backfills), return_exceptions=True)

    async def reconcile_pr(
        self, repo: str, pr: int, *, backfill_budget: int = 0, detach_backfill: bool = False
    ) -> tuple[str, int]:
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
                if detach_backfill:  # the sweep: see `_detach_backfill`
                    return self._detach_backfill(repo, pr, head), backfill_budget - 1
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
                    _outcome, budget = await self.reconcile_pr(
                        repo, int(pr), backfill_budget=budget, detach_backfill=True
                    )
                    count += 1
                except Exception:  # noqa: BLE001
                    log.exception("[pr-reviewer] sweep reconcile failed on %s#%s", repo, pr)
        return count


# How long the first sweep pass waits for the App token before going ahead without it.
# Bounded: a mint that keeps failing must not stall the sweep forever — it enumerates with
# whatever `gh` has (a PAT, or nothing) exactly as before, one log line later.
AUTH_READY_WAIT_S = 20.0


async def sweep_loop(
    dispatcher: Dispatcher,
    interval_s: int,
    stop_event: asyncio.Event,
    *,
    auth_ready: asyncio.Event | None = None,
    auth_ready_wait_s: float = AUTH_READY_WAIT_S,
) -> None:
    """The background surface body — single-flight by construction (one loop).

    The first pass waits (bounded) for `auth_ready`, which the App-auth surface sets once
    the installation token is published (issue #99). Without it the first enumeration ran
    ~600 ms before the token existed, failed, and swept 0 repos for a whole interval on
    every boot and every image roll."""
    if auth_ready is not None and not auth_ready.is_set():
        try:
            await asyncio.wait_for(auth_ready.wait(), timeout=auth_ready_wait_s)
        except asyncio.TimeoutError:
            log.warning(
                "[pr-reviewer] App token not ready after %.0fs; first sweep pass proceeds without it (issue #99)",
                auth_ready_wait_s,
            )
    while not stop_event.is_set():
        try:
            await dispatcher.sweep_once()
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] sweep pass failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass

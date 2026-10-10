"""The protoPatch (`clawpatch`) structural pass — resolve, run, map (ADR 0078 B2).

protoPatch is the cross-file/systemic analysis engine a hunk-by-hunk diff read can't
match; here it joins the ADR 0077 review panel as a fifth, NON-LLM finder. This module
owns the deterministic machinery:

  - `resolve_pr_refs` — head+base SHAs from the PR via `gh`, SERVER-SIDE (the model
    never supplies a ref; a model-picked SHA is how you review the wrong code).
  - `run_clawpatch` — `clawpatch ci --provider gateway --json --state-dir <per-review>
    --since <baseSha>` in the cached checkout, under a hard wall-clock budget
    (SIGKILL past it; the CLI has no timeout flag of its own). The state dir is this
    pass's OWN scratch dir under the repo's (#223), never one shared between reviews.
  - `read_findings` / `map_finding` — `ci --json` emits COUNTS only, so the finding
    objects are read from `<state>/findings/*.json`, filtered to open items whose
    evidence touches this PR's changed files, and mapped into the ADR 0077 contract
    with `source: "protopatch"`.

Failure posture (ADR 0078 D3): every failure — timeout, missing binary, missing
gateway credentials, clone failure, non-zero exit — degrades to a typed
`PROTOPATCH UNAVAILABLE` message the structural-finder turns into a Gap + empty
findings array. The tool never raises: a starved structural pass must not void the
four-finder panel review.

Keep tool docstrings PLAIN string literals (an f-string docstring → __doc__ is None →
the tool ships with no description).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from .checkout_cache import CheckoutCache, CheckoutError, checkout_root_for, redact
from .gh_cli import bad_repo, resolve_token, run_gh
from .lintcheck import LintChecker, render_refuted
from .refutations import RefutationStore, _norm_path, premark_refuted

log = logging.getLogger("protoagent.plugins.pr_reviewer")

# protoPatch severity → the ADR 0077 scale.
SEVERITY_MAP = {"critical": "blocker", "high": "major", "medium": "minor", "low": "nit"}

# clawpatch exit codes (protoPatch docs/spec.md) → operator-readable reasons.
_EXIT_REASONS = {
    2: "invalid usage/config or git failure",
    3: "dirty worktree",
    # clawpatch raises exit 4 for EVERY provider failure — auth, an HTTP error, a failed
    # request, an empty reply, or a reply that isn't parseable JSON (its `provider-failure`
    # class). Naming only auth sent diagnosis the wrong way; the detail after this says which.
    4: "gateway provider failure: auth, HTTP error, or an unusable model reply",
    5: "gateway quota/rate limit",
    6: "tests/validation failed",
    7: "state lock conflict (another run in flight?)",
    8: "malformed provider output",
}

UNAVAILABLE_PREFIX = "PROTOPATCH UNAVAILABLE"
# The line `unavailable()` tells the relay to write INSTEAD of echoing the tool's text. A
# relay that obeys produces this and an empty array, with `UNAVAILABLE_PREFIX` nowhere in
# its reply — so an outage check that knows only the prefix reads a faithful relay of an
# outage as a clean, delivered, empty structural pass.
GAP_LINE_PREFIX = "Gap: structural pass unavailable"
# A pass that was CUT SHORT (budget SIGKILL, or a feature failed) after some features had already
# finished is not an outage: those features' findings are real and used to be thrown away with the
# whole pass (#205). `partial_result()` returns them under this prefix with an explicit Gap line,
# so the lane is still flagged incomplete (verdict capped at WARN) while its findings flow through.
PARTIAL_PREFIX = "PROTOPATCH PARTIAL"
GAP_PARTIAL_PREFIX = "Gap: structural pass partial"
# A third chance to be seen: the run header a partial result opens with. The relay is a model, and the
# failure that matters is one that drops BOTH markers above and reads as a clean, complete pass (the
# #138 fail-open class) — but it reliably echoes the header it is told to relay. Detection only: it
# carries no reason (see `_NO_REASON_MARKERS`).
PARTIAL_HEADER = "protoPatch structural pass partial on"
# Any one in the structural lane's output means the structural pass did not run in full.
STRUCTURAL_GAP_MARKERS = (UNAVAILABLE_PREFIX, GAP_LINE_PREFIX, PARTIAL_PREFIX, GAP_PARTIAL_PREFIX, PARTIAL_HEADER)
_NO_REASON_MARKERS = (PARTIAL_HEADER,)
# Feature statuses (clawpatch `featureStatuses`) that mean a review of the feature FINISHED.
COMPLETED_FEATURE_STATUSES = frozenset({"reviewed", "needs-fix"})


_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+")
_ABS_PATH_RE = re.compile(r"(?<![\w.])/(?:[\w.@-]+/)+[\w.@-]*")
_MARKDOWN_RE = re.compile(r"[`*_\[\]|<>]")


def outage_reason(structural_output: str, limit: int = 180) -> str:
    """The reason a structural outage gave, fit to print in a PR comment — "" when none.

    Read from the lane's own Gap line (either marker). The text has been through the relay
    model and began as clawpatch's stderr, so it is untrusted display text: URLs and
    absolute paths are dropped (a PR is no place for the reviewer's gateway address or
    filesystem layout), markdown control characters are removed, and it is clipped. Tokens
    were already redacted at the source.
    """
    text = structural_output or ""
    for marker in STRUCTURAL_GAP_MARKERS:
        if marker in _NO_REASON_MARKERS:
            continue  # detection-only: what follows is a repo#pr, not a reason
        _before, found, after = text.partition(marker)
        if not found:
            continue
        reason = (after.splitlines() or [""])[0].lstrip(" —-:")
        reason = _ABS_PATH_RE.sub("(path)", _URL_RE.sub("(url)", reason))
        reason = " ".join(_MARKDOWN_RE.sub("", reason).split())
        if reason:
            return reason if len(reason) <= limit else reason[: limit - 1].rstrip() + "…"
    return ""


_EXIT_RE = re.compile(r"clawpatch exit (\d+)")
# The coverage a partial result puts at the FRONT of its Gap line, so the display clip in
# `outage_reason` can never drop it and `classify_outage` can strip it before matching.
_PARTIAL_LEAD_RE = re.compile(r"^\s*\d+ of \d+ features? reviewed\s*[—-]\s*", re.IGNORECASE)


def classify_outage(reason: str) -> str:
    """A countable class for a structural outage reason — "" when there was none.

    `outage_reason` is display text; this is the telemetry key (issue #205). Ten of the
    eighteen incomplete rounds in one week were `find_structural` outages, and telling a
    clawpatch per-request timeout (`exit 4 … no reply within the 270000ms gateway timeout`)
    from an auth failure (`exit 4 … 401`) or a missing binary meant grepping the container
    log, because the reviewed row only said `structural_unavailable: true`.

    Classes: `budget-timeout` (our SIGKILL), `feature-cap` (the plan left features unreviewed, #232),
    `not-installed`, `no-credentials`, `checkout`,
    `exit-N` for a clawpatch exit code, refined for exit 4 into `exit-4:gateway-timeout`
    (clawpatch's own provider timeout) or `exit-4:provider` (anything else in that class),
    and `other` for a reason this does not recognise.
    """
    text = _PARTIAL_LEAD_RE.sub("", (reason or "").strip(), count=1)  # "33 of 36 features reviewed — "
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("timed out after"):
        return "budget-timeout"
    if lowered.startswith(FEATURE_CAP_REASON):
        return "feature-cap"
    if "is not installed" in lowered or "command not found" in lowered:
        return "not-installed"
    if lowered.startswith("no gateway credentials"):
        return "no-credentials"
    if lowered.startswith("checkout failed"):
        return "checkout"
    m = _EXIT_RE.search(text)
    if m:
        code = int(m.group(1))
        if code == 4:
            return "exit-4:gateway-timeout" if "gateway timeout" in lowered else "exit-4:provider"
        return f"exit-{code}"
    return "other"


def unavailable(reason: str) -> str:
    return (
        f"{UNAVAILABLE_PREFIX} — {reason}\n\n"
        "The structural pass did not run. In your reply, state exactly one Gap line — "
        f"`{GAP_LINE_PREFIX} — {reason}` — and emit an empty findings "
        "array (```json\n[]\n```). Do not retry, do not invent findings."
    )


def partial_result(coverage: str, reason: str, header: str, findings: list[dict]) -> str:
    """The tool's answer for a pass that was cut short AFTER some features finished (#205).

    Same shape as a normal result — a header and the fenced findings array — plus the Gap line
    the relay must state, so the lane is flagged incomplete (WARN cap) without losing the findings
    the completed features produced. `coverage` ("33 of 36 features reviewed") leads the reason
    on purpose: see `_PARTIAL_LEAD_RE`."""
    gap = f"{GAP_PARTIAL_PREFIX} — {coverage} — {reason}"
    return (
        f"{PARTIAL_PREFIX} — {coverage} — {reason}\n\n"
        f"{header}\n\n"
        "The pass was cut short, but the features that finished produced the findings below. In your "
        f"reply, state exactly one Gap line — `{gap}` — then relay the fenced findings array below "
        "EXACTLY as given: same items, nothing added, edited, re-graded or dropped. Do not call the "
        "tool again.\n\n"
        f"```json\n{json.dumps(findings, indent=2)}\n```"
    )


def is_partial_output(text: str) -> bool:
    """Did the structural lane's output come from a partial pass (either marker)?"""
    out = text or ""
    return PARTIAL_PREFIX in out or GAP_PARTIAL_PREFIX in out or PARTIAL_HEADER in out


def pass_coverage(state_dir: Path) -> tuple[int, int] | None:
    """(features finished, features claimed) for THIS pass, from clawpatch's own state dir.

    The latest run record that claimed features names them; each feature's record says whether its
    review finished. None when nothing readable was claimed — callers then treat the pass as having
    produced nothing. Never raises: a half-written file (the pass was SIGKILLed) is just skipped."""
    claimed: list[str] = []
    for run in sorted((state_dir / "runs").glob("*.json"), reverse=True):
        try:
            ids = json.loads(run.read_text()).get("claimedFeatureIds")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(ids, list) and ids:
            claimed = [str(i) for i in ids]
            break
    if not claimed:
        return None
    done = 0
    for fid in claimed:
        try:
            status = json.loads((state_dir / "features" / f"{fid}.json").read_text()).get("status")
        except (OSError, ValueError, AttributeError):
            continue
        done += status in COMPLETED_FEATURE_STATUSES
    return done, len(claimed)


# clawpatch's own gateway-request timeout (CLAWPATCH_GATEWAY_TIMEOUT_MS), sized PER ATTEMPT and
# kept strictly inside the attempt's slice of the wall-clock budget (#209). The DEFAULT still scales
# with the budget — `budget - headroom`, floored at 30s — so an operator who raised time_budget_s
# for a slow gateway review still gets the long request they asked for; an inherited value is
# honoured but capped to that same window. Only a value allowed to outlive our SIGKILL dies as an
# opaque `fetch failed` (~300s socket) instead of clawpatch's own clean, classifiable gateway
# timeout. On the retry the attempt budget shrinks, so the second request's timeout tightens below
# the ~300s socket window on its own — no fixed ceiling needed.
GATEWAY_TIMEOUT_HEADROOM_S = 30  # keep the request this far under the wall-clock SIGKILL
GATEWAY_TIMEOUT_FLOOR_MS = 30_000  # never below 30s, however small the attempt budget

# A transient gateway failure gets ONE retry, but only when at least this much of the wall-clock
# budget survives the first attempt — below it a second attempt cannot finish, so we degrade now.
RETRY_MIN_BUDGET_S = 90

# Concurrent feature reviews per structural pass (#221). Left alone, clawpatch runs about half the
# host's CPU cores of them at once, capped at 10 (Vera's host has 24 cores, so 10) — each a
# 50-115k-token prompt. One big PR (5+ features, ~5% of passes, median 9) then puts 0.3-0.7M tokens
# of KV cache into the smart lane in seconds; the lane holds ~0.8M per replica, saturates, and every
# request on it crawls until the 600s gateway timeout kills it (homelab-iac#284). Typical passes
# have 1-3 features (95%), so a cap of 4 leaves them untouched and only trims the rare burst.
DEFAULT_STRUCTURAL_JOBS = 4
MAX_STRUCTURAL_JOBS = 10  # clawpatch's own ceiling for its default; a higher setting is clamped


def structural_jobs(value) -> int | None:
    """The `--jobs` value for one structural pass, from the `structural_jobs` setting.

    Unset / blank -> DEFAULT_STRUCTURAL_JOBS. `0` -> None: pass no `--jobs`, so clawpatch picks its
    own default (the pre-#221 behaviour, and the rollback switch). Above MAX_STRUCTURAL_JOBS it is
    clamped. Anything that is not a whole number >= 0 (a bool, a float, text, a negative) falls back
    to the default with a warning rather than raising: a typo in one setting must not stop the
    structural pass, and must not silently turn the cap off either."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_STRUCTURAL_JOBS
    parsed: int | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        parsed = int(value)
    if parsed is None or parsed < 0:
        log.warning(
            "[pr-reviewer] ignoring structural_jobs=%r (expected a whole number >= 0); using %d",
            value,
            DEFAULT_STRUCTURAL_JOBS,
        )
        return DEFAULT_STRUCTURAL_JOBS
    if parsed == 0:
        return None
    return min(parsed, MAX_STRUCTURAL_JOBS)


# ── The feature plan (#232) ─────────────────────────────────────────────────────────────────────
# `clawpatch ci --since <base>` reviews every feature that OWNS a changed file or lists one as
# CONTEXT. Its Python mapper lists `pyproject.toml` as context of every Python feature, so a one-line
# version bump selected 276 of protoAgent's 361 features (protoAgent#4003: a ~150-line diff, 5
# features own a changed file, 271 came in through `pyproject.toml` alone). At `--jobs 4` that is
# hours of review; every such pass hit the budget. The plugin now picks the features itself:
#   - a lockfile, generated file, changelog fragment or dependency manifest never pulls a feature
#     in through CONTEXT (the feature's own code did not change; its findings are confined to the
#     diff anyway, so reviewing it for a version bump buys nothing reportable),
#   - the rest are ranked by changed lines in files the feature owns, then in its context files,
#   - at most `structural_max_features` are reviewed; the rest are a COVERAGE GAP (partial pass,
#     "N of M features reviewed"), never a silent drop.
LOW_SIGNAL_BASENAMES = frozenset(
    {
        # lockfiles
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "pdm.lock",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lockb",
        "bun.lock",
        "Cargo.lock",
        "Gemfile.lock",
        "composer.lock",
        "go.sum",
        "mix.lock",
        "Package.resolved",
        # generated
        "THIRD_PARTY_LICENSES.md",
        "CHANGELOG.md",
        # dependency manifests
        "pyproject.toml",
        "Pipfile",
        "setup.cfg",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Gemfile",
        "composer.json",
        "mix.exs",
    }
)
LOW_SIGNAL_DIRS = ("changelog.d",)
_REQUIREMENTS_RE = re.compile(r"^requirements[\w.-]*\.(?:txt|in)$", re.IGNORECASE)

# Features reviewed per pass. At `--jobs 4` the smart lane finishes roughly 1.5-2.5 features a
# minute (one feature is ~20-90 s of prompt and 100-300 s of reply), so 16 is ~4 waves, about
# 10-12 minutes, inside the 1500 s budget with room for a slow wave or the transient retry.
DEFAULT_MAX_FEATURES = 16
FEATURE_CAP_REASON = "feature cap reached"
PLAN_FILENAME = "feature-plan.txt"
PLAN_STEP_BUDGET_S = 120  # `init` + `map` are local and heuristic (~2-3 s on protoAgent)


def is_low_signal(path: str) -> bool:
    """A changed file that says nothing about which features a change touches: a lockfile, a
    generated file, a changelog fragment, or a dependency manifest (#232)."""
    parts = path.replace("\\", "/").strip("/").split("/")
    name = parts[-1] if parts else ""
    return (
        name in LOW_SIGNAL_BASENAMES
        or bool(_REQUIREMENTS_RE.match(name))
        or any(d in parts[:-1] for d in LOW_SIGNAL_DIRS)
    )


def structural_max_features(value) -> int | None:
    """The per-pass feature cap from the `structural_max_features` setting.

    Unset / blank -> DEFAULT_MAX_FEATURES. `0` -> None: no cap (the lockfile/manifest scoping still
    applies). Anything that is not a whole number >= 0 falls back to the default with a warning, so a
    typo can neither stop the pass nor silently lift the cap."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_MAX_FEATURES
    parsed: int | None = None
    if isinstance(value, bool):
        parsed = None
    elif isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"\s*\d+\s*", value):
        parsed = int(value)
    if parsed is None or parsed < 0:
        log.warning(
            "[pr-reviewer] ignoring structural_max_features=%r (expected a whole number >= 0); using %d",
            value,
            DEFAULT_MAX_FEATURES,
        )
        return DEFAULT_MAX_FEATURES
    return parsed or None


def parse_numstat(out: str) -> dict[str, int]:
    """{path: changed lines} from `git diff --numstat` (with or without `-z`, which keeps unusual paths
    unquoted, as clawpatch reads them). A binary file (`-\t-`) counts as 1."""
    lines: dict[str, int] = {}
    text = out or ""
    for row in text.split("\0") if "\0" in text else text.splitlines():
        parts = row.split("\t")
        if len(parts) < 3 or not parts[2].strip():
            continue
        added, deleted = parts[0].strip(), parts[1].strip()
        n = (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
        lines[_norm_path(parts[2].strip())] = max(n, 1)
    return lines


def read_feature_records(state_dir: Path) -> list[dict]:
    """The features `clawpatch map` wrote to `<state>/features/`. Unreadable records are skipped."""
    out: list[dict] = []
    for path in sorted((state_dir / "features").glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("featureId"):
            out.append(record)
    return out


def _paths(entries) -> set[str]:
    return {_norm_path(str(e.get("path"))) for e in entries or [] if isinstance(e, dict) and e.get("path")}


class FeaturePlan:
    """Which features one structural pass reviews, and why (#232)."""

    def __init__(self, mapped: int, eligible: list[dict], cap: int | None, low_signal_only: int, low_signal_files):
        self.mapped = mapped
        self.eligible = eligible  # ranked, most changed lines first
        self.cap = cap
        self.selected = eligible if cap is None else eligible[:cap]
        self.low_signal_only = low_signal_only  # matched ONLY through a lockfile/manifest as context
        self.low_signal_files = sorted(low_signal_files)

    @property
    def dropped(self) -> int:
        return len(self.eligible) - len(self.selected)

    def summary(self) -> str:
        text = f"plan: {len(self.selected)} of {len(self.eligible)} eligible feature(s) of {self.mapped} mapped"
        if self.dropped:
            text += f", {self.dropped} over the per-pass cap of {self.cap}"
        if self.low_signal_only:
            text += (
                f"; {self.low_signal_only} more depend on the diff only through a lockfile or dependency "
                "manifest and are out of scope"
            )
        return text


def plan_features(features: list[dict], changed_lines: dict[str, int], cap: int | None) -> FeaturePlan:
    """Rank the features a diff touches and keep the top `cap` (None = all).

    Eligible: the feature OWNS a changed file, or lists a changed file that is not low-signal as
    context. Ranked by changed lines in owned files that are not low-signal, then by changed lines in
    such context files, then by feature id (deterministic). A feature that owns only a low-signal
    file (the config feature for `pyproject.toml`) stays eligible, ranked last."""
    changed = set(changed_lines)
    signal = {p for p in changed if not is_low_signal(p)}
    eligible: list[dict] = []
    low_signal_only = 0
    low_signal_files: set[str] = set()
    for feature in features:
        owned = _paths(feature.get("ownedFiles")) & changed
        context = (_paths(feature.get("contextFiles")) & changed) - owned
        if not owned and not (context & signal):
            if context:
                low_signal_only += 1
                low_signal_files |= context
            continue
        eligible.append(
            {
                "id": str(feature["featureId"]),
                "owned_lines": sum(changed_lines[p] for p in owned & signal),
                "context_lines": sum(changed_lines[p] for p in context & signal),
                "files": len(_paths(feature.get("ownedFiles")) | _paths(feature.get("contextFiles"))),
            }
        )
    eligible.sort(key=lambda f: (-f["owned_lines"], -f["context_lines"], f["id"]))
    return FeaturePlan(len(features), eligible, cap, low_signal_only, low_signal_files)


_PROGRESS_RE = re.compile(r"^clawpatch review (feature-start|feature-done|feature-error) (.*)$")
_PROMPT_RE = re.compile(r"prompt=(\d+) bytes; approxTokens=(\d+)")


def feature_outcomes(stderr: str, state_dir: Path, ids: list[str]) -> list[dict]:
    """Per planned feature: how it ended and how long it took (#232 ask 1).

    `status` is `finished` (its review completed), `error` (it failed), `killed` (in flight when the
    pass ended) or `not-started` (it never got a worker). A pass where most features never started is
    over-planned; one where a few have been in flight for the whole budget is a hang. `elapsed_s` is
    clawpatch's own per-feature figure from its progress lines; prompt size comes from the feature's
    record. Never raises."""
    seen: dict[str, dict] = {}
    for line in (stderr or "").splitlines():
        m = _PROGRESS_RE.match(line.strip())
        if not m:
            continue
        fields = dict(kv.split("=", 1) for kv in m.group(2).split(" ") if "=" in kv)
        fid = fields.get("feature")
        if not fid:
            continue
        row = seen.setdefault(fid, {"status": "killed"})
        if m.group(1) != "feature-start":
            row["status"] = "finished" if m.group(1) == "feature-done" else "error"
            elapsed = fields.get("elapsed", "").rstrip("s")
            if elapsed.isdigit():
                row["elapsed_s"] = int(elapsed)
    out: list[dict] = []
    for fid in ids:
        row = {"id": fid, "status": "not-started", **seen.get(fid, {})}
        try:
            record = json.loads((state_dir / "features" / f"{fid}.json").read_text())
            status = record.get("status")
            if status in COMPLETED_FEATURE_STATUSES:
                row["status"] = "finished"
            for entry in reversed(record.get("analysisHistory") or []):
                m = _PROMPT_RE.search(str(entry.get("summary") or ""))
                if m:
                    row["prompt_bytes"], row["approx_tokens"] = int(m.group(1)), int(m.group(2))
                    break
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        out.append(row)
    return out


def gateway_timeout_ms(attempt_budget_s: int, inherited: str | None = None) -> int:
    """CLAWPATCH_GATEWAY_TIMEOUT_MS for one attempt. The default SCALES with the attempt's budget
    (`budget - headroom`, floored at 30s), so raising time_budget_s really does buy a longer gateway
    request; an inherited value takes precedence but is capped at that same `budget - headroom` so
    the request can never outlive our SIGKILL. An inherited value larger than the budget is exactly
    what let a request run to ~300s and die as an opaque `fetch failed` instead of clawpatch's own
    clean, classifiable gateway timeout (#209)."""
    cap_ms = max((attempt_budget_s - GATEWAY_TIMEOUT_HEADROOM_S) * 1000, GATEWAY_TIMEOUT_FLOOR_MS)
    try:
        desired = int(inherited) if inherited not in (None, "") else cap_ms
    except (TypeError, ValueError):
        desired = cap_ms
    return max(min(desired, cap_ms), 1)


# The shape of a TRANSIENT gateway failure worth one retry: a dropped/blocked request, a socket or
# provider timeout, or a gateway 5xx. clawpatch collapses its whole provider-failure class into
# exit 4, so the stderr detail is the only separator (mirrors `classify_outage`).
_TRANSIENT_GATEWAY_RE = re.compile(
    r"fetch failed|request failed|gateway timeout|no reply within|socket hang ?up|"
    r"econnreset|econnrefused|etimedout|network|timed out|\b50[234]\b",
    re.IGNORECASE,
)


def is_transient_gateway_failure(rc: int, text: str) -> bool:
    """True when a clawpatch exit is a transient gateway failure a single retry could clear — as
    opposed to auth (401/403), a bad model reply, or quota, where the same call just fails again."""
    if rc != 4:
        return False
    return bool(_TRANSIENT_GATEWAY_RE.search(text or ""))


def _with_attempts(reason: str, attempts: int) -> str:
    """Append the clawpatch attempt count to a degradation reason (#209): with a retry in play the
    reason alone no longer tells the operator how many gateway runs were spent."""
    return f"{reason} (after {attempts} attempt{'' if attempts == 1 else 's'})"


async def resolve_pr_refs(repo: str, pr: int) -> tuple[str, str] | str:
    """(head_sha, base_sha) for the PR, resolved server-side; an error string on failure."""
    rc, out, err = await run_gh(
        ["api", f"repos/{repo}/pulls/{pr}", "--jq", '(.head.sha // "") + " " + (.base.sha // "")'],
        timeout=15,
    )
    if rc != 0:
        return f"could not resolve PR #{pr} in {repo}: {err or out or f'gh exit {rc}'}"
    parts = out.split()
    if len(parts) != 2:
        return f"PR #{pr} in {repo} returned no head/base SHAs"
    return parts[0], parts[1]


async def _default_run_clawpatch(args: list[str], cwd: Path, env: dict, budget_s: int) -> tuple[int, str, str, bool]:
    """Run the clawpatch CLI → (rc, stdout, stderr, timed_out). SIGKILL past the budget."""
    try:
        proc = await asyncio.create_subprocess_exec(
            args[0],
            *args[1:],
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return 127, "", f"{args[0]}: command not found", False
    # Read both pipes as they fill rather than with `communicate()`, so a pass SIGKILLed at the budget
    # still hands back the stderr it wrote: its per-feature progress lines are the only record of
    # which features were in flight, done or never started (#232).
    out: list[bytes] = []
    err: list[bytes] = []

    async def pump(stream, sink: list[bytes]) -> None:
        while chunk := await stream.read(65536):
            sink.append(chunk)

    done = asyncio.gather(pump(proc.stdout, out), pump(proc.stderr, err), proc.wait())
    timed_out = False
    try:
        await asyncio.wait_for(asyncio.shield(done), timeout=budget_s)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        try:
            await asyncio.wait_for(done, timeout=10)  # the pipes close with the process
        except (asyncio.TimeoutError, asyncio.CancelledError):
            done.cancel()
    text = (b"".join(out).decode(errors="replace"), b"".join(err).decode(errors="replace"))
    if timed_out:
        return 124, text[0], text[1], True
    return proc.returncode or 0, text[0], text[1], False


# Lines of context around a changed hunk that still count as "this PR's code" when picking a
# finding's anchor — the same padding `rounds.DELTA_CONTEXT_LINES` gives the dispatcher's scoping.
ANCHOR_CONTEXT_LINES = 5


def _touches_change(ref: dict, ranges: dict[str, list[tuple[int, int]]] | None) -> bool:
    """Does one evidence location overlap a line this PR changed (± `ANCHOR_CONTEXT_LINES`)?"""
    if not ranges:
        return False
    spans = ranges.get(_norm_path(str(ref.get("path") or "")))
    if not spans:
        return False
    try:
        start = int(ref.get("startLine") or 0)
        end = int(ref.get("endLine") or start)
    except (TypeError, ValueError):
        return False
    if start <= 0:
        return False
    end = max(end, start)
    return any(start <= hi + ANCHOR_CONTEXT_LINES and lo - ANCHOR_CONTEXT_LINES <= end for lo, hi in spans)


def map_finding(record: dict, ranges: dict[str, list[tuple[int, int]]] | None = None) -> dict | None:
    """One protoPatch FindingRecord → an ADR 0077 finding dict, or None if not reportable.

    Category passes through verbatim (the contract's category vocabulary is advisory);
    severity maps critical/high/medium/low → blocker/major/minor/nit; `source` is
    always "protopatch". Only open/uncertain findings report — fixed, wont-fix and
    false-positive records are protoPatch's own resolved state.

    The finding is anchored (file, line, quote) at its FIRST evidence location — unless
    `ranges` (this PR's changed lines) shows another of its locations in code the PR changed,
    which then wins (#232). The dispatcher scopes structural findings by their anchor: a
    cross-location finding ("this change breaks that caller") anchored at its untouched end
    would read as a pre-existing note about code the PR never touched.
    """
    if record.get("status") not in ("open", "uncertain"):
        return None
    title = str(record.get("title") or "").strip()
    if not title:
        return None
    evidence_refs = [e for e in record.get("evidence") or [] if isinstance(e, dict) and e.get("path")]
    first = next((e for e in evidence_refs if _touches_change(e, ranges)), evidence_refs[0] if evidence_refs else {})
    quote = str(first.get("quote") or "").strip()
    reasoning = str(record.get("reasoning") or "").strip()
    recommendation = str(record.get("recommendation") or "").strip()
    evidence = quote or reasoning[:400]
    if recommendation:
        evidence = f"{evidence} Fix: {recommendation[:200]}".strip()
    confidence = str(record.get("confidence") or "").strip()
    if confidence:
        evidence = f"{evidence} (protopatch confidence: {confidence})"
    return {
        "file": str(first.get("path") or ""),
        "line": int(first.get("startLine") or 0),
        "severity": SEVERITY_MAP.get(str(record.get("severity") or "").lower(), "minor"),
        "category": str(record.get("category") or "").strip().lower(),
        "claim": title,
        "evidence": evidence.strip(),
        "source": "protopatch",
    }


def read_findings(
    state_dir: Path, changed_files: set[str] | None, ranges: dict[str, list[tuple[int, int]]] | None = None
) -> list[dict]:
    """Open findings from `<state>/findings/*.json`, confined to this PR.

    The state dir is per REVIEW (#223): it holds only this pass's findings, so another PR's
    findings on a shared file can never leak in. `changed_files`, when known, still confines the
    report to the diff. Deduped by protoPatch `signature`. `ranges` (changed lines per file)
    picks each finding's anchor — see `map_finding`.
    """
    findings_dir = state_dir / "findings"
    if not findings_dir.is_dir():
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for path in sorted(findings_dir.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        sig = str(record.get("signature") or record.get("findingId") or path.name)
        if sig in seen:
            continue
        paths = {str(e.get("path")) for e in record.get("evidence") or [] if isinstance(e, dict) and e.get("path")}
        if changed_files is not None and paths and not (paths & changed_files):
            continue  # a prior PR's finding — not this diff's
        mapped = map_finding(record, ranges)
        if mapped:
            seen.add(sig)
            out.append(mapped)
    return out


# One structural pass gets its OWN clawpatch state dir, under the repo's persistent one (#223).
# A per-repo dir shared by concurrent reviews made them fail each other's feature claims with
# `exit 7` (`feature locked`), let one PR pick up another's findings on a shared file, and kept a
# lock forever when a redeploy killed a run. The repo dir stays the home of what SHOULD persist
# (refuted claims, provider-failure captures); the working state is per review.
SCRATCH_DIRNAME = "scratch"
# A scratch dir kept for a postmortem (its run failed) is dropped once it is this old.
SCRATCH_KEEP_FAILED_S = 6 * 3600


# The residual paths of an epic PR reviewed by its residual only (#273), keyed by the
# SERVER-resolved (repo, pr, head). The dispatcher sets it when it scopes a round; the structural
# pass, which the panel reaches through a model-called tool that only carries (pr, repo), reads it
# back after resolving the head itself — so the scope can never come from the model. A different
# head finds nothing and plans from the whole PR diff, as before. Bounded: rounds overwrite.
_STRUCTURAL_SCOPES: dict[tuple[str, int, str], frozenset[str]] = {}
_STRUCTURAL_SCOPES_MAX = 256


def set_structural_scope(repo: str, pr: int, head: str, paths: list[str] | None) -> None:
    """Scope this head's structural pass to `paths`, or (None) clear it so it plans the whole diff."""
    key = (repo, int(pr), head)
    _STRUCTURAL_SCOPES.pop(key, None)
    if paths is None:
        return
    _STRUCTURAL_SCOPES[key] = frozenset(_norm_path(p) for p in paths if p)
    while len(_STRUCTURAL_SCOPES) > _STRUCTURAL_SCOPES_MAX:
        _STRUCTURAL_SCOPES.pop(next(iter(_STRUCTURAL_SCOPES)))


def structural_scope(repo: str, pr: int, head: str) -> frozenset[str] | None:
    return _STRUCTURAL_SCOPES.get((repo, int(pr), head))


class ProtoPatchRunner:
    """The orchestration the tool calls — every step degrades to `unavailable(...)`."""

    def __init__(self, cfg: dict, *, run_clawpatch=None, run_git=None, run_lint=None, telemetry=None):
        self.cfg = cfg or {}
        # Where the per-pass `structural_plan` event goes (#232); None = not recorded.
        self.telemetry = telemetry
        home = Path(os.environ.get("PR_REVIEWER_HOME") or Path.home() / ".protoagent" / "pr-reviewer")
        self.checkout_root = checkout_root_for(self.cfg)  # shared with the absence search (#259)
        self.state_root = Path(self.cfg.get("state_root") or home / "clawpatch")
        # Claims this repo's verifier already refuted (#190) — shared with the dispatcher,
        # which writes them when a round posts; the structural pass reads them here.
        self.refutations = RefutationStore.from_cfg(self.cfg)
        # Lint-rule claims checked with the repo's own pinned ruff, in this checkout (#232 ask 6).
        self.lint = LintChecker(self.cfg, tools_dir=home / "tools", run=run_lint)
        self.budget_s = int(self.cfg.get("time_budget_s") or 600)
        self.jobs = structural_jobs(self.cfg.get("structural_jobs"))  # None = clawpatch's own default
        self.max_features = structural_max_features(self.cfg.get("structural_max_features"))  # None = no cap
        # False = the pre-#232 path: `clawpatch ci --since <base>` picks the features (the rollback switch).
        self.plan_features = str(self.cfg.get("structural_plan", "")).strip().lower() not in ("false", "0", "no", "off")
        self.bin = str(self.cfg.get("clawpatch_bin") or "clawpatch")
        self.model = str(self.cfg.get("model") or "")
        self.gateway_base_url = str(self.cfg.get("gateway_base_url") or "")
        self.cache = CheckoutCache(
            self.checkout_root,
            ttl_s=int(self.cfg.get("checkout_ttl_s") or 3600),
            entry_limit=int(self.cfg.get("checkout_max_entries") or 50),
            size_limit_bytes=int(self.cfg.get("checkout_max_bytes") or 5 * 1024**3),
            run_git=run_git,
        )
        self._run_clawpatch = run_clawpatch or _default_run_clawpatch
        self._run_git = run_git  # tests inject; None = the cache's default runner
        self._startup_pruned = False  # one-time cache sweep, deferred to first use

    async def _resolve_git_token(self) -> str | None:
        if token := resolve_token():
            return token
        rc, out, _err = await run_gh(["auth", "token"], timeout=10)
        return out.strip() if rc == 0 and out.strip() else None

    def _gateway_creds(self) -> tuple[str, str]:
        """(api_key, base_url) for clawpatch's gateway provider. Env wins; inside a
        host, the agent's own model config (`model.api_key` / `model.api_base`) is
        the fallback — wizard-configured deployments keep the key in config, not
        env, so env-only resolution would starve the subprocess. Host-free (tests,
        standalone) the lazy import just fails and env is all there is."""
        key = os.environ.get("GATEWAY_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        base = self.gateway_base_url
        if not key or not base:
            try:
                from graph.sdk import config as host_config

                hc = host_config()
                key = key or str(getattr(hc, "api_key", "") or "")
                base = base or str(getattr(hc, "api_base", "") or "")
            except Exception:  # noqa: BLE001 — no host present
                pass
        return key, base

    async def _changed_ranges(self, checkout: Path, base_sha: str) -> dict[str, list[tuple[int, int]]] | None:
        """{file: [(start, end), …]} of head-side lines this PR changes — what decides whether
        a remembered refutation still applies (#190). None when the diff is unreadable."""
        from .checkout_cache import _default_run_git

        run = self._run_git or _default_run_git
        rc, out, _err = await run(["-C", str(checkout), "diff", "--unified=0", f"{base_sha}...HEAD"])
        if rc != 0:
            return None
        ranges: dict[str, list[tuple[int, int]]] = {}
        current = ""
        for line in out.splitlines():
            if line.startswith("+++ "):
                current = line[4:].strip()
                current = current[2:] if current.startswith("b/") else current
            elif line.startswith("@@ ") and current:
                m = re.search(r"\+(\d+)(?:,(\d+))?", line)
                if m:
                    start = int(m.group(1))
                    count = int(m.group(2)) if m.group(2) is not None else 1
                    ranges.setdefault(_norm_path(current), []).append((start, start + max(count, 1) - 1))
        return ranges

    async def _changed_files(self, checkout: Path, base_sha: str) -> set[str] | None:
        from .checkout_cache import _default_run_git

        run = self._run_git or _default_run_git
        rc, out, _err = await run(["-C", str(checkout), "diff", "--name-only", f"{base_sha}...HEAD"])
        if rc != 0:
            return None  # unknown — report unconfined rather than dropping everything
        return {line.strip() for line in out.splitlines() if line.strip()}

    def _prune(self) -> int:
        """Best-effort checkout-cache maintenance (TTL sweep + LRU entry/byte caps).
        Degrades, never raises — a failed prune must not void the review (ADR 0078 D3)."""
        try:
            removed = self.cache.prune()
        except Exception:  # noqa: BLE001 — maintenance is best-effort, never fatal
            log.exception("[pr-reviewer] checkout cache prune failed")
            return 0
        if removed:
            log.info("[pr-reviewer] pruned %d stale checkout(s) from the cache", removed)
        return removed

    def _prune_scratch(self, repo_dir: Path | None = None) -> int:
        """Drop scratch state dirs kept for a postmortem once they are `SCRATCH_KEEP_FAILED_S`
        old — for one repo, or (at startup) every repo, which also clears dirs a redeploy
        orphaned mid-run. Best-effort, never raises."""
        removed = 0
        try:
            dirs = [repo_dir] if repo_dir is not None else [d for d in self.state_root.glob("*") if d.is_dir()]
            now = time.time()
            for d in dirs:
                for scratch in (d / SCRATCH_DIRNAME).glob("*"):
                    try:
                        if now - scratch.stat().st_mtime > SCRATCH_KEEP_FAILED_S:
                            shutil.rmtree(scratch, ignore_errors=True)
                            removed += 1
                    except OSError:
                        continue
        except Exception:  # noqa: BLE001 — maintenance is best-effort, never fatal
            log.exception("[pr-reviewer] scratch state prune failed")
        return removed

    async def _changed_lines(self, checkout: Path, base_sha: str) -> dict[str, int] | None:
        """{file: changed lines} for the diff clawpatch's `--since` reads; None when unreadable."""
        from .checkout_cache import _default_run_git

        run = self._run_git or _default_run_git
        rc, out, _err = await run(
            ["-C", str(checkout), "diff", "--numstat", "-z", "--no-renames", f"{base_sha}...HEAD"]
        )
        return parse_numstat(out) if rc == 0 else None

    async def _plan(
        self,
        checkout: Path,
        base_sha: str,
        state_dir: Path,
        env: dict,
        deadline: float,
        changed: set[str] | None,
        scope: frozenset[str] | None = None,
    ):
        """Map the checkout into this pass's state dir and pick the features to review (#232).

        None when a plan cannot be made (map failed, nothing mapped, diff unreadable): the pass then
        falls back to `clawpatch ci --since`, the pre-#232 behaviour. Never raises."""
        try:
            for step in (["init"], ["map"]):
                budget = max(min(PLAN_STEP_BUDGET_S, int(deadline - time.monotonic())), 1)
                rc, _out, _err, timed_out = await self._run_clawpatch(
                    [self.bin, "--state-dir", str(state_dir), "--json", "-q", *step], checkout, env, budget
                )
                if rc != 0 or timed_out:
                    log.warning(
                        "[pr-reviewer] structural plan: clawpatch %s failed (exit %s%s); using --since",
                        step[0],
                        rc,
                        ", timed out" if timed_out else "",
                    )
                    return None
            features = read_feature_records(state_dir)
            lines = await self._changed_lines(checkout, base_sha)
            if lines is None or changed is None or not any(isinstance(f.get("ownedFiles"), list) for f in features):
                return None  # nothing mapped, or a record shape this planner does not know: let clawpatch pick
            if scope is not None:
                lines = {path: n for path, n in lines.items() if path in scope}  # epic residual (#273)
            # Every file the name-only diff lists is in the plan, even one numstat did not count: a
            # plan built from an incomplete diff must never decide that nothing needs reviewing.
            for path in changed:
                lines.setdefault(_norm_path(path), 1)
            return plan_features(features, lines, self.max_features)
        except Exception:  # noqa: BLE001 — a failed plan falls back to the pre-#232 path, never raises
            log.exception("[pr-reviewer] structural plan failed; using --since")
            return None

    def _emit_plan(self, repo: str, pr: int, record: dict, result: str) -> None:
        """One `structural_plan` event per pass (#232 ask 1): what was planned, what finished, and how
        long each feature took — so an over-planned pass can be told from a hang without the log."""
        if self.telemetry is None or not record:
            return
        try:
            outcome = (
                "unavailable"
                if result.startswith(UNAVAILABLE_PREFIX)
                else "partial"
                if result.startswith(PARTIAL_PREFIX)
                else "complete"
            )
            reason = classify_outage(outage_reason(result)) if outcome != "complete" else None
            self.telemetry.emit("structural_plan", repo=repo, pr=pr, outcome=outcome, reason=reason or None, **record)
        except Exception:  # noqa: BLE001 — telemetry never breaks the pass
            log.exception("[pr-reviewer] structural_plan telemetry failed")

    async def review(self, pr: int, repo: str) -> str:
        """The full structural pass → prose header + fenced ADR 0077 findings JSON,
        or an `unavailable(...)` degradation message. Never raises.

        Cache maintenance rides on this entry point (pr-reviewer#87): a one-time
        startup prune clears any garbage accumulated before this wiring existed, and a
        prune after every run — win or fail — holds the checkout cache under its TTL +
        entry/byte caps. Maintenance cadence, never the hot path."""
        if not self._startup_pruned:
            self._startup_pruned = True
            self._prune()  # first use: sweep pre-fix accumulation before anything runs
            self._prune_scratch()  # and scratch dirs a redeploy orphaned mid-run
        scratch: list[Path] = []
        record: dict = {}
        try:
            result = await self._run_review(pr, repo, scratch, record)
            if not result.startswith((UNAVAILABLE_PREFIX, PARTIAL_PREFIX)):
                # A pass that produced findings has nothing left to inspect: drop its state dir
                # (each holds a full report set). A failed pass keeps its dir for a postmortem.
                for d in scratch:
                    shutil.rmtree(d, ignore_errors=True)
            if result.startswith(UNAVAILABLE_PREFIX):
                # The ONLY place the reason is kept (#140). It goes to the relay subagent,
                # which paraphrases it, and a synthesizer that guessed "gateway auth error"
                # one round and "provider error" the next for the same fault; nothing logged
                # it, so the operator who could fix a route or a key never saw which it was.
                log.warning("[pr-reviewer] structural pass unavailable on %s#%s: %s", repo, pr, result.splitlines()[0])
            elif result.startswith(PARTIAL_PREFIX):
                log.warning("[pr-reviewer] structural pass PARTIAL on %s#%s: %s", repo, pr, result.splitlines()[0])
            self._emit_plan(repo, pr, record, result)
            return result
        finally:
            self._prune()  # after each use — success or degradation alike

    async def _run_review(
        self, pr: int, repo: str, scratch: list[Path] | None = None, record: dict | None = None
    ) -> str:
        record = {} if record is None else record
        if err := bad_repo(repo):
            return unavailable(err)
        gateway_key, gateway_base = self._gateway_creds()
        if not gateway_key:
            return unavailable(
                "no gateway credentials (GATEWAY_API_KEY / OPENAI_API_KEY unset, no model.api_key in host config)"
            )

        refs = await resolve_pr_refs(repo, pr)
        if isinstance(refs, str):
            return unavailable(refs)
        head_sha, base_sha = refs

        token = await self._resolve_git_token()
        try:
            checkout = await self.cache.resolve(repo, head_sha, token)
        except CheckoutError as exc:
            return unavailable(f"checkout failed: {exc}")

        changed = await self._changed_files(checkout, base_sha)
        # An epic reviewed by its residual (#273): plan, select and confine to the residual commits'
        # files only, so the attested slices neither fill the feature cap nor cost the budget.
        scope = structural_scope(repo, pr, head_sha)
        if scope is not None and changed is not None:
            changed = {p for p in changed if _norm_path(p) in scope}

        repo_dir = self.state_root / repo.replace("/", "-")
        repo_dir.mkdir(parents=True, exist_ok=True)
        self._prune_scratch(repo_dir)
        state_dir = repo_dir / SCRATCH_DIRNAME / f"{head_sha[:12]}-{uuid.uuid4().hex[:8]}"
        state_dir.mkdir(parents=True)
        if scratch is not None:
            scratch.append(state_dir)  # review() drops it on success, keeps it on failure
        # Provider-failure captures are diagnostics that must outlive the scratch dir: point the
        # dir clawpatch writes them into at the repo's persistent one.
        captures = repo_dir / "provider-failures"
        captures.mkdir(exist_ok=True)
        (state_dir / "provider-failures").symlink_to(captures, target_is_directory=True)

        env = os.environ.copy()
        env["GATEWAY_API_KEY"] = gateway_key
        if gateway_base:
            env["OPENAI_BASE_URL"] = gateway_base
        # The CLI's own gateway-request timeout must sit strictly inside our wall-clock budget, and
        # an inherited CLAWPATCH_GATEWAY_TIMEOUT_MS must NOT override that (#209): a request allowed
        # to outlive the budget dies by our SIGKILL as an opaque `fetch failed` (~300s) instead of
        # clawpatch's own clean, classifiable gateway timeout. It is set per attempt below.
        inherited_timeout = env.get("CLAWPATCH_GATEWAY_TIMEOUT_MS")

        started = time.monotonic()
        deadline = started + self.budget_s
        record.update(sha=head_sha, base=base_sha, budget_s=self.budget_s, jobs=self.jobs, cap=self.max_features)
        if scope is not None:
            record["scoped_paths"] = len(scope)
        plan = (
            await self._plan(checkout, base_sha, state_dir, env, deadline, changed, scope)
            if self.plan_features
            else None
        )
        render = dict(
            pr=pr,
            repo=repo,
            head_sha=head_sha,
            base_sha=base_sha,
            checkout=checkout,
            changed=changed,
            state_dir=state_dir,
            plan=plan,
        )
        if plan is None:
            record["planner"] = "since"
            args = [self.bin, "ci", "--provider", "gateway", "--json", "--state-dir", str(state_dir)]
            args += ["--since", base_sha]
        else:
            record.update(
                planner="plugin",
                mapped=plan.mapped,
                eligible=len(plan.eligible),
                selected=len(plan.selected),
                dropped=plan.dropped,
                low_signal_only=plan.low_signal_only,
                low_signal_files=plan.low_signal_files[:20] or None,
            )
            if not plan.selected:
                # Nothing owns or depends on a changed source file: there is nothing to review, the same
                # answer `ci --since` gives an untouched map (see `FeaturePlan` for the lockfile case).
                record["elapsed_s"] = round(time.monotonic() - started, 1)
                return await self._render_findings(**render, elapsed=time.monotonic() - started)
            plan_path = state_dir / PLAN_FILENAME
            plan_path.write_text("".join(f"{f['id']}\n" for f in plan.selected))
            args = [self.bin, "review", "--provider", "gateway", "--json", "--state-dir", str(state_dir)]
            args += ["--feature-list", str(plan_path)]
        args_tail: list[str] = []
        if self.jobs is not None:
            args_tail += ["--jobs", str(self.jobs)]  # cap the burst one big PR puts on the lane (#221)
        if self.model:
            args_tail += ["--model", self.model]
        args += args_tail

        # One structural pass may run clawpatch twice: a TRANSIENT gateway failure (a dropped
        # request / socket timeout / gateway 5xx — #209) gets a single retry when enough of the
        # budget survives the first attempt. Any other exit degrades exactly as before, never raises.
        attempt = 0
        stderr_seen: list[str] = []
        try:
            while True:
                attempt += 1
                remaining_s = deadline - time.monotonic()
                attempt_budget_s = max(round(remaining_s), 1)
                gateway_ms = gateway_timeout_ms(attempt_budget_s, inherited_timeout)
                env["CLAWPATCH_GATEWAY_TIMEOUT_MS"] = str(gateway_ms)
                log.debug(
                    "[pr-reviewer] clawpatch attempt %d: gateway timeout=%dms, wall-clock=%ds (budget=%ds, inherited=%s)",
                    attempt,
                    gateway_ms,
                    attempt_budget_s,
                    self.budget_s,
                    inherited_timeout,
                )
                rc, stdout, stderr, timed_out = await self._run_clawpatch(args, checkout, env, attempt_budget_s)
                stderr_seen.append(stderr or "")
                if timed_out:
                    return await self._cut_short(
                        f"timed out after {self.budget_s}s (budget exceeded; review proceeds without it)",
                        f"timed out after {self.budget_s}s (budget exceeded; findings from the finished features kept)",
                        attempt,
                        **render,
                        elapsed=time.monotonic() - started,
                    )
                if rc == 127:
                    return unavailable(
                        _with_attempts(f"`{self.bin}` is not installed (npm: @protolabsai/protopatch)", attempt)
                    )
                if rc == 0:
                    break
                if plan is not None and rc == 2 and "feature-list" in f"{stderr}{stdout}":
                    # An engine without `review --feature-list` (protoPatch < 0.7.0): run the pass the
                    # pre-#232 way instead of losing it. Not a gateway attempt, so it is not counted.
                    log.warning("[pr-reviewer] clawpatch has no --feature-list; using ci --since (needs >= 0.7.0)")
                    plan = render["plan"] = None
                    record.update(planner="since", feature_list_unsupported=True)
                    args = [self.bin, "ci", "--provider", "gateway", "--json", "--state-dir", str(state_dir)]
                    args += ["--since", base_sha] + args_tail
                    attempt -= 1
                    continue
                reason_name = _EXIT_REASONS.get(rc, "runtime failure")
                detail = redact(redact((stderr or stdout).strip()[-400:], token), gateway_key)
                reason = f"clawpatch exit {rc} ({reason_name}): {detail}"
                remaining_s = deadline - time.monotonic()
                if (
                    attempt == 1
                    and is_transient_gateway_failure(rc, stderr or stdout)
                    and remaining_s >= RETRY_MIN_BUDGET_S
                ):
                    log.warning(
                        "[pr-reviewer] clawpatch transient gateway failure (exit %d) on attempt %d; retrying "
                        "once with %.0fs of the %ds budget left (#209)",
                        rc,
                        attempt,
                        remaining_s,
                        self.budget_s,
                    )
                    continue
                return await self._cut_short(reason, reason, attempt, **render, elapsed=time.monotonic() - started)
            if plan is not None and plan.dropped:
                # Every planned feature finished, but the plan itself left some out: a coverage gap,
                # never a complete pass (the fail-open this guards: a capped pass reading as clean).
                done = pass_coverage(state_dir)
                return await self._render_findings(
                    **render,
                    elapsed=time.monotonic() - started,
                    partial=(
                        f"{done[0] if done else len(plan.selected)} of {len(plan.eligible)} features reviewed",
                        f"{FEATURE_CAP_REASON}: the {len(plan.selected)} features with the most changed lines "
                        f"were reviewed, {plan.dropped} were not (structural_max_features={plan.cap})",
                    ),
                )
            return await self._render_findings(**render, elapsed=time.monotonic() - started)
        finally:
            record["attempts"] = attempt
            record["elapsed_s"] = round(time.monotonic() - started, 1)
            if plan is not None and plan.selected:
                ids = [f["id"] for f in plan.selected]
                outcomes = feature_outcomes(stderr_seen[-1] if stderr_seen else "", state_dir, ids)
                ranked = {f["id"]: f for f in plan.selected}
                record["features"] = [{**ranked[o["id"]], **o} for o in outcomes]
                record["finished"] = sum(o["status"] == "finished" for o in outcomes)

    async def _cut_short(self, reason: str, partial_reason: str, attempt: int, **render) -> str:
        """The answer for a pass that did not finish: its findings so far, or an outage (#205).

        Any non-zero exit or budget kill used to return `unavailable()` and drop everything — including
        the findings features that HAD finished had already written (49 of them across three
        budget-killed passes on one night). When at least one claimed feature finished, those findings
        are returned as a PARTIAL result: still a lane gap (the verdict stays capped at WARN), but the
        findings are not lost. With nothing finished it is the outage it always was. Never raises.

        With a plan (#232) the denominator is every ELIGIBLE feature, not just the planned ones, so a
        pass that was both capped and cut short says how much of the diff's features it really covered."""
        try:
            coverage = pass_coverage(render["state_dir"])
            if coverage is not None and coverage[0] > 0:
                plan = render.get("plan")
                total = max(coverage[1], len(plan.eligible)) if plan is not None else coverage[1]
                return await self._render_findings(
                    **render,
                    partial=(
                        f"{coverage[0]} of {total} features reviewed",
                        _with_attempts(partial_reason, attempt),
                    ),
                )
        except Exception:  # noqa: BLE001 — salvage is best-effort; the outage below is the safe answer
            log.exception("[pr-reviewer] could not salvage a cut-short structural pass")
        return unavailable(_with_attempts(reason, attempt))

    async def _render_findings(
        self,
        *,
        pr: int,
        repo: str,
        head_sha: str,
        base_sha: str,
        checkout: Path,
        changed: set[str] | None,
        state_dir: Path,
        elapsed: float,
        partial: tuple[str, str] | None = None,
        plan: FeaturePlan | None = None,
    ) -> str:
        """The header + fenced findings array for a pass, complete — or partial when `partial` is
        (coverage, reason). The one place findings are read, confined and pre-marked."""
        ranges = await self._changed_ranges(checkout, base_sha)
        findings = read_findings(state_dir, changed, ranges)
        # A finding that cites a lint rule is settled by the linter CI runs, not by a model
        # (#232 ask 6, protoAgent#4017 r1: F841 on a tuple-unpack target, which F841 never
        # flags). Refuted ⇒ dropped here, before the relay can carry it; anything uncertain
        # leaves the finding as it was. Bounded and never raises.
        findings, lint_refuted, lint_version = await self.lint.check(checkout, findings)
        # A repeat of a claim this repo's verifier already refuted, at a spot this PR does
        # not touch, goes to the synthesizer already marked (#190) — it is dropped there
        # instead of costing a verify round on every PR that touches the file.
        premarked = premark_refuted(findings, self.refutations, repo, ranges)
        confinement = f"{len(changed)} changed file(s)" if changed is not None else "unconfined (diff unavailable)"
        repeats = f", {premarked} refuted before (pre-marked)" if premarked else ""
        header = (
            f"{PARTIAL_HEADER if partial else 'protoPatch structural pass on'} {repo}#{pr} — "
            f"head {head_sha[:12]}, base {base_sha[:12]}, {elapsed:.0f}s, {len(findings)} reportable finding(s)"
            f"{' from the features that finished' if partial else ''}{repeats}"
            f"{render_refuted(lint_refuted, lint_version)}, scope: {confinement}"
            f"{f'; {plan.summary()}' if plan is not None else ''}."
        )
        if partial:
            return partial_result(partial[0], partial[1], header, findings)
        return f"{header}\n\n```json\n{json.dumps(findings, indent=2)}\n```"


def _telemetry(cfg: dict):
    """The plugin's telemetry sink, at the same home the dispatcher writes to (see __init__)."""
    from . import _state_home
    from .telemetry import Telemetry

    return Telemetry(_state_home(cfg or {}))


def get_tools(cfg: dict) -> list:
    """The plugin's tools — built against the live per-agent config."""
    from langchain_core.tools import tool

    runner = ProtoPatchRunner(cfg, telemetry=_telemetry(cfg))
    default_repo = str((cfg or {}).get("default_repo") or "")

    @tool
    async def protopatch_review(pr: int, repo: str = "") -> str:
        """Run the protoPatch structural analysis engine over a pull request and return its findings as the standard fenced findings JSON (each item carries source: "protopatch"). Head and base SHAs are resolved from the PR server-side. Expensive (an LLM-backed engine, minutes): call it at most ONCE per review. If it reports PROTOPATCH UNAVAILABLE, relay the Gap it describes and an empty findings array — never retry, never invent findings. Args: pr = the pull-request number; repo = owner/name (omit to use the configured default)."""
        target = (repo or "").strip() or default_repo
        try:
            return await runner.review(int(pr), target)
        except Exception as exc:  # noqa: BLE001 — the panel must degrade, never crash
            log.exception("[pr-reviewer] protopatch_review failed unexpectedly")
            return unavailable(f"unexpected error: {type(exc).__name__}: {exc}")

    return [protopatch_review]

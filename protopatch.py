"""The protoPatch (`clawpatch`) structural pass — resolve, run, map (ADR 0078 B2).

protoPatch is the cross-file/systemic analysis engine a hunk-by-hunk diff read can't
match; here it joins the ADR 0077 review panel as a fifth, NON-LLM finder. This module
owns the deterministic machinery:

  - `resolve_pr_refs` — head+base SHAs from the PR via `gh`, SERVER-SIDE (the model
    never supplies a ref; a model-picked SHA is how you review the wrong code).
  - `run_clawpatch` — `clawpatch ci --provider gateway --json --state-dir <per-repo>
    --since <baseSha>` in the cached checkout, under a hard wall-clock budget
    (SIGKILL past it; the CLI has no timeout flag of its own).
  - `read_findings` / `map_finding` — `ci --json` emits COUNTS only, so the finding
    objects are read from `<state>/findings/*.json`, filtered to open items whose
    evidence touches this PR's changed files (the per-repo state dir accumulates
    across PRs), and mapped into the ADR 0077 contract with `source: "protopatch"`.

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
import time
from pathlib import Path

from .checkout_cache import CheckoutCache, CheckoutError, redact
from .gh_cli import bad_repo, resolve_token, run_gh
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
# Either one in the structural lane's output means the structural pass did not run.
STRUCTURAL_GAP_MARKERS = (UNAVAILABLE_PREFIX, GAP_LINE_PREFIX)


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


def classify_outage(reason: str) -> str:
    """A countable class for a structural outage reason — "" when there was none.

    `outage_reason` is display text; this is the telemetry key (issue #205). Ten of the
    eighteen incomplete rounds in one week were `find_structural` outages, and telling a
    clawpatch per-request timeout (`exit 4 … no reply within the 270000ms gateway timeout`)
    from an auth failure (`exit 4 … 401`) or a missing binary meant grepping the container
    log, because the reviewed row only said `structural_unavailable: true`.

    Classes: `budget-timeout` (our SIGKILL), `not-installed`, `no-credentials`, `checkout`,
    `exit-N` for a clawpatch exit code, refined for exit 4 into `exit-4:gateway-timeout`
    (clawpatch's own provider timeout) or `exit-4:provider` (anything else in that class),
    and `other` for a reason this does not recognise.
    """
    text = (reason or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith("timed out after"):
        return "budget-timeout"
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
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=budget_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, "", "", True
    return proc.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"), False


def map_finding(record: dict) -> dict | None:
    """One protoPatch FindingRecord → an ADR 0077 finding dict, or None if not reportable.

    Category passes through verbatim (the contract's category vocabulary is advisory);
    severity maps critical/high/medium/low → blocker/major/minor/nit; `source` is
    always "protopatch". Only open/uncertain findings report — fixed, wont-fix and
    false-positive records are protoPatch's own resolved state.
    """
    if record.get("status") not in ("open", "uncertain"):
        return None
    title = str(record.get("title") or "").strip()
    if not title:
        return None
    evidence_refs = [e for e in record.get("evidence") or [] if isinstance(e, dict) and e.get("path")]
    first = evidence_refs[0] if evidence_refs else {}
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


def read_findings(state_dir: Path, changed_files: set[str] | None) -> list[dict]:
    """Open findings from `<state>/findings/*.json`, confined to this PR.

    The state dir is per-REPO and persistent (protoPatch's cross-run memory), so
    records from other PRs accumulate; when `changed_files` is known, only findings
    whose evidence touches one of them report. Deduped by protoPatch `signature`.
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
        mapped = map_finding(record)
        if mapped:
            seen.add(sig)
            out.append(mapped)
    return out


class ProtoPatchRunner:
    """The orchestration the tool calls — every step degrades to `unavailable(...)`."""

    def __init__(self, cfg: dict, *, run_clawpatch=None, run_git=None):
        self.cfg = cfg or {}
        home = Path(os.environ.get("PR_REVIEWER_HOME") or Path.home() / ".protoagent" / "pr-reviewer")
        self.checkout_root = Path(self.cfg.get("checkout_root") or home / "checkouts")
        self.state_root = Path(self.cfg.get("state_root") or home / "clawpatch")
        # Claims this repo's verifier already refuted (#190) — shared with the dispatcher,
        # which writes them when a round posts; the structural pass reads them here.
        self.refutations = RefutationStore.from_cfg(self.cfg)
        self.budget_s = int(self.cfg.get("time_budget_s") or 300)
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
        try:
            result = await self._run_review(pr, repo)
            if result.startswith(UNAVAILABLE_PREFIX):
                # The ONLY place the reason is kept (#140). It goes to the relay subagent,
                # which paraphrases it, and a synthesizer that guessed "gateway auth error"
                # one round and "provider error" the next for the same fault; nothing logged
                # it, so the operator who could fix a route or a key never saw which it was.
                log.warning("[pr-reviewer] structural pass unavailable on %s#%s: %s", repo, pr, result.splitlines()[0])
            return result
        finally:
            self._prune()  # after each use — success or degradation alike

    async def _run_review(self, pr: int, repo: str) -> str:
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

        state_dir = self.state_root / repo.replace("/", "-")
        state_dir.mkdir(parents=True, exist_ok=True)

        args = [self.bin, "ci", "--provider", "gateway", "--json", "--state-dir", str(state_dir), "--since", base_sha]
        if self.model:
            args += ["--model", self.model]
        env = os.environ.copy()
        env["GATEWAY_API_KEY"] = gateway_key
        if gateway_base:
            env["OPENAI_BASE_URL"] = gateway_base
        # The CLI's own provider timeout must sit inside our wall-clock budget.
        env.setdefault("CLAWPATCH_GATEWAY_TIMEOUT_MS", str(max((self.budget_s - 30) * 1000, 30_000)))

        started = time.monotonic()
        rc, stdout, stderr, timed_out = await self._run_clawpatch(args, checkout, env, self.budget_s)
        elapsed = time.monotonic() - started
        if timed_out:
            return unavailable(f"timed out after {self.budget_s}s (budget exceeded; review proceeds without it)")
        if rc == 127:
            return unavailable(f"`{self.bin}` is not installed (npm: @protolabsai/protopatch)")
        if rc != 0:
            reason = _EXIT_REASONS.get(rc, "runtime failure")
            detail = redact(redact((stderr or stdout).strip()[-400:], token), gateway_key)
            return unavailable(f"clawpatch exit {rc} ({reason}): {detail}")

        findings = read_findings(state_dir, changed)
        # A repeat of a claim this repo's verifier already refuted, at a spot this PR does
        # not touch, goes to the synthesizer already marked (#190) — it is dropped there
        # instead of costing a verify round on every PR that touches the file.
        premarked = premark_refuted(findings, self.refutations, repo, await self._changed_ranges(checkout, base_sha))
        confinement = f"{len(changed)} changed file(s)" if changed is not None else "unconfined (diff unavailable)"
        repeats = f", {premarked} refuted before (pre-marked)" if premarked else ""
        header = (
            f"protoPatch structural pass on {repo}#{pr} — head {head_sha[:12]}, base {base_sha[:12]}, "
            f"{elapsed:.0f}s, {len(findings)} reportable finding(s){repeats}, scope: {confinement}."
        )
        return f"{header}\n\n```json\n{json.dumps(findings, indent=2)}\n```"


def get_tools(cfg: dict) -> list:
    """The plugin's tools — built against the live per-agent config."""
    from langchain_core.tools import tool

    runner = ProtoPatchRunner(cfg)
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

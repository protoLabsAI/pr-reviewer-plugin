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
import time
from pathlib import Path

from .checkout_cache import CheckoutCache, CheckoutError, redact
from .gh_cli import bad_repo, resolve_token, run_gh

log = logging.getLogger("protoagent.plugins.pr_reviewer")

# protoPatch severity → the ADR 0077 scale.
SEVERITY_MAP = {"critical": "blocker", "high": "major", "medium": "minor", "low": "nit"}

# clawpatch exit codes (protoPatch docs/spec.md) → operator-readable reasons.
_EXIT_REASONS = {
    2: "invalid usage/config or git failure",
    3: "dirty worktree",
    4: "gateway auth/config failure",
    5: "gateway quota/rate limit",
    6: "tests/validation failed",
    7: "state lock conflict (another run in flight?)",
    8: "malformed provider output",
}

UNAVAILABLE_PREFIX = "PROTOPATCH UNAVAILABLE"


def unavailable(reason: str) -> str:
    return (
        f"{UNAVAILABLE_PREFIX} — {reason}\n\n"
        "The structural pass did not run. In your reply, state exactly one Gap line — "
        f"`Gap: structural pass unavailable — {reason}` — and emit an empty findings "
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

    async def _changed_files(self, checkout: Path, base_sha: str) -> set[str] | None:
        from .checkout_cache import _default_run_git

        run = self._run_git or _default_run_git
        rc, out, _err = await run(["-C", str(checkout), "diff", "--name-only", f"{base_sha}...HEAD"])
        if rc != 0:
            return None  # unknown — report unconfined rather than dropping everything
        return {line.strip() for line in out.splitlines() if line.strip()}

    async def review(self, pr: int, repo: str) -> str:
        """The full structural pass → prose header + fenced ADR 0077 findings JSON,
        or an `unavailable(...)` degradation message. Never raises."""
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
        confinement = f"{len(changed)} changed file(s)" if changed is not None else "unconfined (diff unavailable)"
        header = (
            f"protoPatch structural pass on {repo}#{pr} — head {head_sha[:12]}, base {base_sha[:12]}, "
            f"{elapsed:.0f}s, {len(findings)} reportable finding(s), scope: {confinement}."
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

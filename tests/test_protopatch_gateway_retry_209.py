"""protoPatch structural pass survives a transient gateway `fetch failed` (#209 part 3).

Five times in three hours the structural pass died on `clawpatch exit 4 (gateway provider
failure …) elapsed=301s error=gateway review: request failed (fetch failed)` — a ~300s
client/socket timeout on the gateway request, not a provider outage — and every hit capped
the verdict at WARN with incomplete coverage.

Two guards, both here:

  * The gateway request timeout the plugin hands clawpatch is kept STRICTLY inside the
    wall-clock budget, even when an inherited CLAWPATCH_GATEWAY_TIMEOUT_MS is larger (that
    inherited value is exactly what let a request run to ~300s and die as `fetch failed`).
  * A transient gateway failure gets ONE retry when enough of the budget survives the first
    attempt; anything else degrades exactly as before (PROTOPATCH UNAVAILABLE + Gap line,
    never raises — ADR 0078 D3), now with the attempt count in the message.
"""

from __future__ import annotations

import json

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import (
    GATEWAY_TIMEOUT_HEADROOM_S,
    RETRY_MIN_BUDGET_S,
    ProtoPatchRunner,
    gateway_timeout_ms,
    is_transient_gateway_failure,
)

SHA_HEAD = "a" * 40
SHA_BASE = "b" * 40

# The exact shape from the incident: clawpatch exit 4, stderr a gateway `fetch failed`.
FETCH_FAILED = "gateway review: request failed (fetch failed)"

RECORD = {
    "title": "Race in prune().",
    "category": "concurrency",
    "severity": "critical",
    "confidence": "high",
    "evidence": [{"path": "lib/cache.ts", "startLine": 42, "quote": "rmSync(...)"}],
    "reasoning": "r",
    "recommendation": "lock",
    "status": "open",
    "signature": "sig-1",
}


@pytest.fixture
def gateway_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    monkeypatch.delenv("CLAWPATCH_GATEWAY_TIMEOUT_MS", raising=False)


@pytest.fixture
def pr_refs(monkeypatch):
    async def fake_run_gh(args, timeout=30):
        if args[:1] == ["api"]:
            return 0, f"{SHA_HEAD} {SHA_BASE}", ""
        return 0, "", ""

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


def make_git(diff_out="lib/cache.ts\n"):
    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            import os

            os.makedirs(args[-1], exist_ok=True)
        if "diff" in args:
            return 0, diff_out, ""
        return 0, "", ""

    return run_git


def scripted_clawpatch(script, calls):
    """A fake runner driven by `script` — one dict per expected call:
    {rc, stdout, stderr, timed_out, on_run}. Each invocation appends a record (args, cwd, a
    COPY of env, budget_s) to `calls`, so the per-attempt env can be inspected after the fact."""
    seq = iter(script)

    async def run(args, cwd, env, budget_s):
        step = next(seq)
        calls.append({"args": args, "cwd": cwd, "env": dict(env), "budget_s": budget_s})
        if step.get("on_run"):
            step["on_run"](args, cwd, env, budget_s)
        return step.get("rc", 0), step.get("stdout", "{}"), step.get("stderr", ""), step.get("timed_out", False)

    return run


def runner(tmp_path, cfg=None, **kw):
    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    return ProtoPatchRunner({**base, **(cfg or {})}, run_git=kw.pop("run_git", make_git()), **kw)


def _seed_findings(tmp_path):
    def seed(args, cwd, env, budget_s):
        state = tmp_path / "st" / "octo-repo" / "findings"
        state.mkdir(parents=True, exist_ok=True)
        (state / "f1.json").write_text(json.dumps(RECORD))

    return seed


def _fenced(out):
    return json.loads(out.split("```json\n", 1)[1].split("```")[0])


# ── r1: a transient failure then a success yields the findings, not UNAVAILABLE ─────────


async def test_transient_fetch_failed_then_success_yields_findings(tmp_path, gateway_env, pr_refs):
    calls = []
    script = [
        {"rc": 4, "stderr": FETCH_FAILED},  # first attempt: the incident's exact shape
        {"rc": 0, "on_run": _seed_findings(tmp_path)},  # retry: clean, emits a finding
    ]
    out = await runner(tmp_path, run_clawpatch=scripted_clawpatch(script, calls)).review(12, "octo/repo")

    assert not out.startswith("PROTOPATCH UNAVAILABLE")
    assert "reportable finding(s)" in out
    [finding] = _fenced(out)
    assert finding["source"] == "protopatch" and finding["severity"] == "blocker"
    assert len(calls) == 2  # it retried exactly once


# ── r2: two consecutive gateway failures degrade with the attempt count, never raise ────


async def test_two_transient_failures_degrade_with_attempt_count(tmp_path, gateway_env, pr_refs):
    calls = []
    script = [{"rc": 4, "stderr": FETCH_FAILED}, {"rc": 4, "stderr": FETCH_FAILED}]
    out = await runner(tmp_path, run_clawpatch=scripted_clawpatch(script, calls)).review(12, "octo/repo")

    assert out.startswith("PROTOPATCH UNAVAILABLE")
    assert "Gap: structural pass unavailable" in out  # the prescribed Gap line survives
    assert "(after 2 attempts)" in out  # the attempt count is reported
    assert "```json\n[]\n```" in out  # the empty-array instruction — it degraded, not raised
    assert len(calls) == 2  # one retry, then it stopped


async def test_a_non_transient_exit_is_not_retried(tmp_path, gateway_env, pr_refs):
    # An auth / unusable-reply exit 4 is NOT transient: retrying the same call cannot help, so
    # it degrades on the first attempt (and the token is still redacted).
    calls = []
    script = [{"rc": 4, "stderr": "401 unauthorized: key ghtok rejected"}]
    out = await runner(tmp_path, run_clawpatch=scripted_clawpatch(script, calls)).review(12, "octo/repo")

    assert out.startswith("PROTOPATCH UNAVAILABLE")
    assert "(after 1 attempt)" in out
    assert "ghtok" not in out
    assert len(calls) == 1  # no retry for a non-transient failure


# ── r3: no retry when the remaining budget is below the threshold ────────────────────────


async def test_no_retry_when_budget_below_threshold(tmp_path, gateway_env, pr_refs):
    # A budget below RETRY_MIN_BUDGET_S leaves too little for a second attempt: degrade now.
    calls = []
    script = [{"rc": 4, "stderr": FETCH_FAILED}, {"rc": 0, "on_run": _seed_findings(tmp_path)}]
    r = runner(
        tmp_path,
        cfg={"time_budget_s": RETRY_MIN_BUDGET_S - 30},
        run_clawpatch=scripted_clawpatch(script, calls),
    )
    out = await r.review(12, "octo/repo")

    assert out.startswith("PROTOPATCH UNAVAILABLE")
    assert "(after 1 attempt)" in out  # singular — the retry never ran
    assert len(calls) == 1  # the second (success) script entry was NOT reached


# ── r4: the gateway timeout is always kept strictly inside the wall-clock budget ─────────


async def test_gateway_timeout_capped_below_budget_even_when_inherited_larger(
    tmp_path, gateway_env, pr_refs, monkeypatch
):
    monkeypatch.setenv("CLAWPATCH_GATEWAY_TIMEOUT_MS", str(999_999_999))  # inherited far above budget
    calls = []
    r = runner(tmp_path, cfg={"time_budget_s": 300}, run_clawpatch=scripted_clawpatch([{"rc": 0}], calls))
    await r.review(12, "octo/repo")

    passed = int(calls[0]["env"]["CLAWPATCH_GATEWAY_TIMEOUT_MS"])
    assert passed < 300 * 1000  # strictly inside the wall-clock budget
    assert passed <= (300 - GATEWAY_TIMEOUT_HEADROOM_S) * 1000  # under the SIGKILL headroom


# ── the pure helpers ────────────────────────────────────────────────────────────────────


def test_gateway_timeout_ms_caps_inherited_and_defaults():
    # No inherited value: the fixed per-attempt ceiling, inside the budget.
    assert gateway_timeout_ms(300, None) == 270_000
    # Fixed PER ATTEMPT — a larger budget does not stretch a single request (that room is a retry).
    assert gateway_timeout_ms(600, None) == 270_000
    # An inherited value above the budget is capped strictly below it (#209).
    assert gateway_timeout_ms(300, str(10_000_000)) == 270_000
    assert gateway_timeout_ms(300, str(10_000_000)) < 300 * 1000
    # A smaller inherited value is honoured.
    assert gateway_timeout_ms(300, str(50_000)) == 50_000
    # Garbage falls back to the ceiling rather than raising.
    assert gateway_timeout_ms(300, "not-a-number") == 270_000
    # Even a tiny budget stays strictly inside the wall-clock.
    assert gateway_timeout_ms(40, None) < 40 * 1000


def test_is_transient_gateway_failure_shape():
    assert is_transient_gateway_failure(4, FETCH_FAILED)
    assert is_transient_gateway_failure(4, "no reply within 270000ms gateway timeout")
    assert is_transient_gateway_failure(4, "upstream returned 503")
    assert is_transient_gateway_failure(4, "read ECONNRESET")
    # Not transient: auth, an unusable reply, a non-failing exit, or quota (exit 5).
    assert not is_transient_gateway_failure(4, "401 unauthorized: bad api key")
    assert not is_transient_gateway_failure(4, "response was not parseable JSON")
    assert not is_transient_gateway_failure(0, FETCH_FAILED)
    assert not is_transient_gateway_failure(5, FETCH_FAILED)

"""ProtoPatchRunner — the degradation ladder and the success path.

Every failure mode must return a PROTOPATCH UNAVAILABLE message (with the Gap
instruction) rather than raise: under ADR 0078 D3 a raising step voids the whole
panel review, and a starved structural pass must degrade it to four finders instead.
"""

from __future__ import annotations

import json

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import ProtoPatchRunner, unavailable

SHA_HEAD = "a" * 40
SHA_BASE = "b" * 40


@pytest.fixture
def gateway_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")


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


def make_clawpatch(rc=0, stdout="{}", stderr="", timed_out=False, on_run=None):
    async def run(args, cwd, env, budget_s):
        if on_run:
            on_run(args, cwd, env, budget_s)
        return rc, stdout, stderr, timed_out

    return run


def runner(tmp_path, cfg=None, **kw):
    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    return ProtoPatchRunner({**base, **(cfg or {})}, run_git=kw.pop("run_git", make_git()), **kw)


# ── degradations ──────────────────────────────────────────────────────────────


async def test_bad_repo_degrades(tmp_path, gateway_env):
    out = await runner(tmp_path).review(1, "nope")
    assert out.startswith("PROTOPATCH UNAVAILABLE") and "Gap:" in out


async def test_missing_gateway_credentials_degrade(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = await runner(tmp_path).review(1, "octo/repo")
    assert "gateway credentials" in out and out.startswith("PROTOPATCH UNAVAILABLE")


async def test_unresolvable_pr_degrades(tmp_path, gateway_env, monkeypatch):
    async def fake_run_gh(args, timeout=30):
        return 1, "", "HTTP 404: Not Found"

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)
    out = await runner(tmp_path).review(9999, "octo/repo")
    assert "could not resolve PR #9999" in out


async def test_timeout_degrades_with_the_budget_named(tmp_path, gateway_env, pr_refs):
    r = runner(tmp_path, cfg={"time_budget_s": 7}, run_clawpatch=make_clawpatch(timed_out=True))
    out = await r.review(1, "octo/repo")
    assert "timed out after 7s" in out and "Gap:" in out


async def test_missing_binary_degrades_with_install_hint(tmp_path, gateway_env, pr_refs):
    r = runner(tmp_path, run_clawpatch=make_clawpatch(rc=127, stderr="not found"))
    out = await r.review(1, "octo/repo")
    assert "@protolabsai/protopatch" in out


async def test_nonzero_exit_degrades_with_typed_reason_and_redacted_token(tmp_path, gateway_env, pr_refs):
    r = runner(tmp_path, run_clawpatch=make_clawpatch(rc=4, stderr="auth ghtok rejected"))
    out = await r.review(1, "octo/repo")
    assert "exit 4 (gateway auth/config failure)" in out
    assert "ghtok" not in out


async def test_never_raises_even_on_unexpected_errors(tmp_path, gateway_env, pr_refs, monkeypatch):
    import pr_reviewer

    from tests.conftest import FakeRegistry

    reg = FakeRegistry({"default_repo": "octo/repo"})
    pr_reviewer.register(reg)
    tool = reg.tools[0]

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(pp.ProtoPatchRunner, "review", boom)
    out = await tool.ainvoke({"pr": 1})
    assert out.startswith("PROTOPATCH UNAVAILABLE") and "kaboom" in out


# ── the success path ──────────────────────────────────────────────────────────

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


async def test_success_emits_header_and_sourced_findings(tmp_path, gateway_env, pr_refs):
    seen = {}

    def on_run(args, cwd, env, budget_s):
        seen.update(args=args, cwd=cwd, env=env, budget=budget_s)
        state = tmp_path / "st" / "octo-repo" / "findings"
        state.mkdir(parents=True, exist_ok=True)
        (state / "f1.json").write_text(json.dumps(RECORD))

    r = runner(tmp_path, cfg={"model": "protolabs/smart"}, run_clawpatch=make_clawpatch(on_run=on_run))
    out = await r.review(12, "octo/repo")

    # The invocation contract: ci, gateway provider, per-repo state dir, server-resolved base.
    assert seen["args"][:5] == ["clawpatch", "ci", "--provider", "gateway", "--json"]
    assert ["--since", SHA_BASE] == seen["args"][seen["args"].index("--since") :][:2]
    assert str(tmp_path / "st" / "octo-repo") in seen["args"]
    assert ["--model", "protolabs/smart"] == seen["args"][-2:]
    assert seen["cwd"] == tmp_path / "co" / "octo-repo" / SHA_HEAD
    assert "CLAWPATCH_GATEWAY_TIMEOUT_MS" in seen["env"]

    # The output contract: header + fenced findings with source attribution.
    assert f"octo/repo#12 — head {SHA_HEAD[:12]}, base {SHA_BASE[:12]}" in out
    fenced = out.split("```json\n", 1)[1].split("```")[0]
    [finding] = json.loads(fenced)
    assert finding["source"] == "protopatch"
    assert finding["severity"] == "blocker"
    assert finding["category"] == "concurrency"


async def test_clean_run_emits_an_empty_array(tmp_path, gateway_env, pr_refs):
    r = runner(tmp_path, run_clawpatch=make_clawpatch())
    out = await r.review(12, "octo/repo")
    assert "0 reportable finding(s)" in out
    assert json.loads(out.split("```json\n", 1)[1].split("```")[0]) == []


def test_unavailable_message_prescribes_the_gap_verbatim():
    msg = unavailable("timed out after 300s")
    assert "Gap: structural pass unavailable — timed out after 300s" in msg

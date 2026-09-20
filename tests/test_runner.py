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


async def test_gateway_creds_fall_back_to_host_model_config(tmp_path, pr_refs, monkeypatch):
    # Wizard-configured deployments keep the key in model.api_key (config), not env —
    # the runner must feed the subprocess from the host config when env is empty.
    import sys
    import types

    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    sdk = types.ModuleType("graph.sdk")
    sdk.config = lambda: types.SimpleNamespace(api_key="cfg-key", api_base="http://localhost:4000/v1")
    monkeypatch.setitem(sys.modules, "graph.sdk", sdk)

    seen = {}
    r = runner(tmp_path, run_clawpatch=make_clawpatch(on_run=lambda a, c, env, b: seen.update(env=env)))
    out = await r.review(1, "octo/repo")
    assert "reportable finding(s)" in out
    assert seen["env"]["GATEWAY_API_KEY"] == "cfg-key"
    assert seen["env"]["OPENAI_BASE_URL"] == "http://localhost:4000/v1"


async def test_explicit_gateway_base_url_beats_host_config(tmp_path, pr_refs, monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    seen = {}
    r = runner(
        tmp_path,
        cfg={"gateway_base_url": "http://elsewhere:9000/v1"},
        run_clawpatch=make_clawpatch(on_run=lambda a, c, env, b: seen.update(env=env)),
    )
    await r.review(1, "octo/repo")
    assert seen["env"]["OPENAI_BASE_URL"] == "http://elsewhere:9000/v1"


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
    assert "exit 4 (gateway provider failure: auth, HTTP error, or an unusable model reply)" in out
    assert "ghtok" not in out


async def test_an_outage_is_logged_with_its_reason(tmp_path, gateway_env, pr_refs, caplog):
    # #140: the reason went only to the relay, which paraphrased it away — `docker logs`
    # held no protopatch line at all, so nobody could tell a bad key from an unusable reply.
    r = runner(tmp_path, run_clawpatch=make_clawpatch(rc=4, stderr="response was not parseable JSON; key ghtok"))
    with caplog.at_level("WARNING"):
        await r.review(7, "octo/repo")
    (line,) = [m for m in caplog.messages if "structural pass unavailable" in m]
    assert "octo/repo#7" in line and "clawpatch exit 4" in line and "not parseable JSON" in line
    assert "ghtok" not in line  # the log gets the REDACTED reason, same as the relay

    caplog.clear()
    ok = runner(tmp_path, run_clawpatch=make_clawpatch(rc=0))
    with caplog.at_level("WARNING"):
        await ok.review(7, "octo/repo")
    assert not [m for m in caplog.messages if "structural pass unavailable" in m]  # silent when it works


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


# ── cache pruning: wire CheckoutCache.prune() into the review lifecycle (#87) ────


def _count_prunes(monkeypatch, r):
    """Replace the runner's cache.prune with a call counter; return the list."""
    calls: list[int] = []
    monkeypatch.setattr(r.cache, "prune", lambda: (calls.append(1), 0)[1])
    return calls


async def test_prune_runs_on_first_use_then_after_every_review(tmp_path, gateway_env, pr_refs, monkeypatch):
    r = runner(tmp_path, run_clawpatch=make_clawpatch())
    calls = _count_prunes(monkeypatch, r)

    # First review: a one-time startup sweep PLUS the post-run prune.
    await r.review(12, "octo/repo")
    assert len(calls) == 2

    # Subsequent reviews: only the post-run prune (the startup sweep never repeats).
    await r.review(13, "octo/repo")
    assert len(calls) == 3


async def test_construction_does_not_prune(tmp_path, gateway_env, monkeypatch):
    # The startup sweep is deferred to first use, not registration time — building a
    # runner (as get_tools does) must not touch the filesystem.
    r = runner(tmp_path)
    calls = _count_prunes(monkeypatch, r)
    assert calls == []


async def test_prune_runs_after_a_failed_review(tmp_path, gateway_env, pr_refs, monkeypatch):
    # A non-zero clawpatch exit degrades — the post-run prune must still fire.
    r = runner(tmp_path, run_clawpatch=make_clawpatch(rc=4, stderr="boom"))
    calls = _count_prunes(monkeypatch, r)
    out = await r.review(12, "octo/repo")
    assert out.startswith("PROTOPATCH UNAVAILABLE")
    assert len(calls) == 2  # startup sweep + post-run prune, even on the failure path


async def test_prune_runs_even_when_it_never_reaches_clawpatch(tmp_path, gateway_env, monkeypatch):
    # An early degradation (bad repo) still gets the startup + post-run prune via the
    # try/finally, so garbage is cleaned regardless of how the review exits.
    r = runner(tmp_path)
    calls = _count_prunes(monkeypatch, r)
    out = await r.review(1, "nope")
    assert out.startswith("PROTOPATCH UNAVAILABLE")
    assert len(calls) == 2


async def test_prune_failure_never_voids_the_review(tmp_path, gateway_env, pr_refs, monkeypatch):
    # Maintenance is best-effort: a raising prune must not bubble out of review().
    r = runner(tmp_path, run_clawpatch=make_clawpatch())

    def boom():
        raise OSError("disk gone")

    monkeypatch.setattr(r.cache, "prune", boom)
    out = await r.review(12, "octo/repo")
    assert "reportable finding(s)" in out  # the review still returns its result


async def test_first_review_cleans_a_preexisting_oversized_cache(tmp_path, gateway_env, pr_refs):
    # Simulate the reference deployment: entries accumulated past TTL before the fix.
    import os
    import time

    co = tmp_path / "co"
    stale = co / "old-repo"
    stale.mkdir(parents=True)
    now = time.time()
    for i in range(5):
        d = stale / (str(i) * 40)
        d.mkdir()
        (d / "blob").write_text("x" * 1000)
        os.utime(d, (now - 10_000, now - 10_000))  # well past the configured TTL

    r = runner(tmp_path, cfg={"checkout_ttl_s": 100}, run_clawpatch=make_clawpatch())
    await r.review(12, "octo/repo")

    # The pre-fix garbage is gone; the fresh checkout for this review survives.
    assert list(stale.iterdir()) == []
    assert (co / "octo-repo" / SHA_HEAD).is_dir()

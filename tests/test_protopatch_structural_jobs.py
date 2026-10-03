"""One structural pass must not flood the smart lane (#221, homelab-iac#284).

Left alone, clawpatch reviews about half the host's CPU cores of features at once (capped at 10 —
Vera's host has 24 cores, so 10), each a 50-115k-token prompt. One big PR then puts 0.3-0.7M tokens
of KV cache into the lane in seconds; the lane saturates, every request crawls, and the gateway's
600s timeout kills them. The runner now passes `--jobs` (setting `structural_jobs`, default 4).

Pinned here: the setting's parsing (including every way it can be mistyped), exactly what reaches
clawpatch, that a retry keeps the cap, and that the shipped yaml default agrees with the code. That
the real clawpatch honours `--jobs` is proven in test_structural_jobs_clawpatch_e2e.py.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pr_reviewer.protopatch as pp
import pytest
import yaml
from pr_reviewer.protopatch import (
    DEFAULT_STRUCTURAL_JOBS,
    MAX_STRUCTURAL_JOBS,
    ProtoPatchRunner,
    structural_jobs,
)

SHA_HEAD = "a" * 40
SHA_BASE = "b" * 40
FETCH_FAILED = "gateway review: request failed (fetch failed)"  # a TRANSIENT failure: retried once

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


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    monkeypatch.delenv("CLAWPATCH_GATEWAY_TIMEOUT_MS", raising=False)

    async def fake_run_gh(args, timeout=30):
        if args[:1] == ["api"]:
            return 0, f"{SHA_HEAD} {SHA_BASE}", ""
        return 0, "", ""

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


def make_git():
    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            import os

            os.makedirs(args[-1], exist_ok=True)
        if "diff" in args:
            return 0, "lib/cache.ts\n", ""
        return 0, "", ""

    return run_git


def recording(calls, script=None):
    """Fake clawpatch: records every invocation; `script` = one {rc, stderr} per call (default ok)."""
    steps = iter(script or [])

    async def run(args, cwd, env, budget_s):
        calls.append(list(args))
        step = next(steps, {})
        if step.get("rc", 0) == 0:
            d = Path(args[args.index("--state-dir") + 1])
            (d / "findings").mkdir(parents=True, exist_ok=True)
            (d / "findings" / "f1.json").write_text(json.dumps(RECORD))
        return step.get("rc", 0), "{}", step.get("stderr", ""), False

    return run


def runner(tmp_path, calls, cfg=None, script=None):
    # These pin the `clawpatch ci --since` path (`structural_plan: false`, the rollback switch); the
    # plugin-planned path (#232) reuses the same retry/salvage code and is pinned in test_protopatch_feature_plan_232.py.
    base = {
        "checkout_root": str(tmp_path / "co"),
        "state_root": str(tmp_path / "st"),
        "default_repo": "",
        "structural_plan": False,
    }
    return ProtoPatchRunner({**base, **(cfg or {})}, run_git=make_git(), run_clawpatch=recording(calls, script))


# -- the setting ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        # unset / blank -> the default cap
        (None, DEFAULT_STRUCTURAL_JOBS),
        ("", DEFAULT_STRUCTURAL_JOBS),
        ("   ", DEFAULT_STRUCTURAL_JOBS),
        # whole numbers, as ints or numeric text (the console's int field and a YAML/env string)
        (1, 1),
        (2, 2),
        (4, 4),
        (10, 10),
        ("3", 3),
        (" 5 ", 5),
        # above clawpatch's own ceiling -> clamped, never passed through
        (11, MAX_STRUCTURAL_JOBS),
        (32, MAX_STRUCTURAL_JOBS),
        (10_000, MAX_STRUCTURAL_JOBS),
        ("99", MAX_STRUCTURAL_JOBS),
        # 0 = "no --jobs, clawpatch decides": the rollback switch
        (0, None),
        ("0", None),
        ("00", None),
    ],
)
def test_structural_jobs_accepts_whole_numbers_and_quietly_clamps(value, expected, caplog):
    with caplog.at_level(logging.WARNING, logger=pp.log.name):
        assert structural_jobs(value) == expected
    assert not [r for r in caplog.records if "structural_jobs" in r.getMessage()]  # valid input never warns


@pytest.mark.parametrize(
    "value",
    [True, False, -1, -100, "-2", "abc", "4x", "x4", "1e1", "0x4", 3.5, 2.0, 0.0, [], [4], {}, {"n": 4}, "4.0", "four"],
    ids=repr,
)
def test_a_mistyped_setting_falls_back_to_the_default_with_a_warning(value, caplog):
    """A typo must neither stop the structural pass nor silently turn the cap OFF — the failure that
    would matter: `False`/`0.0`/`"off"` read as 0 and restore the flood this exists to stop."""
    with caplog.at_level(logging.WARNING, logger=pp.log.name):
        assert structural_jobs(value) == DEFAULT_STRUCTURAL_JOBS
    msgs = [r.getMessage() for r in caplog.records if "structural_jobs" in r.getMessage()]
    assert len(msgs) == 1 and repr(value) in msgs[0]


def test_the_default_is_a_real_cap_below_clawpatchs_own_ceiling():
    assert 1 <= DEFAULT_STRUCTURAL_JOBS < MAX_STRUCTURAL_JOBS == 10


def test_the_runner_keeps_the_parsed_value(tmp_path):
    c: list = []
    assert runner(tmp_path, c).jobs == DEFAULT_STRUCTURAL_JOBS
    assert runner(tmp_path, c, {"structural_jobs": 2}).jobs == 2
    assert runner(tmp_path, c, {"structural_jobs": 0}).jobs is None
    assert runner(tmp_path, c, {"structural_jobs": "nope"}).jobs == DEFAULT_STRUCTURAL_JOBS


# -- what reaches clawpatch ------------------------------------------------------------------------


def value_after(args, flag):
    return args[args.index(flag) + 1]


async def test_by_default_the_pass_is_capped_at_the_default(tmp_path):
    calls: list = []
    out = await runner(tmp_path, calls).review(1, "octo/repo")
    [args] = calls
    assert value_after(args, "--jobs") == str(DEFAULT_STRUCTURAL_JOBS)
    assert args.count("--jobs") == 1
    assert "Race in prune()" in out  # and the pass is otherwise unchanged


@pytest.mark.parametrize("cfg, expect", [(1, "1"), (2, "2"), (7, "7"), (10, "10"), (25, "10"), ("3", "3")])
async def test_a_configured_cap_is_what_clawpatch_receives(tmp_path, cfg, expect):
    calls: list = []
    await runner(tmp_path, calls, {"structural_jobs": cfg}).review(1, "octo/repo")
    assert value_after(calls[0], "--jobs") == expect


async def test_zero_passes_no_jobs_flag_at_all(tmp_path):
    calls: list = []
    await runner(tmp_path, calls, {"structural_jobs": 0}).review(1, "octo/repo")
    assert "--jobs" not in calls[0]  # clawpatch's own default applies — the pre-#221 behaviour


async def test_a_mistyped_setting_still_caps_the_pass(tmp_path):
    calls: list = []
    await runner(tmp_path, calls, {"structural_jobs": "lots"}).review(1, "octo/repo")
    assert value_after(calls[0], "--jobs") == str(DEFAULT_STRUCTURAL_JOBS)


async def test_the_rest_of_the_invocation_contract_is_untouched(tmp_path):
    calls: list = []
    await runner(tmp_path, calls, {"model": "protolabs/smart", "structural_jobs": 3}).review(12, "octo/repo")
    args = calls[0]
    assert args[:5] == ["clawpatch", "ci", "--provider", "gateway", "--json"]
    assert value_after(args, "--since") == SHA_BASE
    assert Path(value_after(args, "--state-dir")).parent == tmp_path / "st" / "octo-repo" / "scratch"
    assert args[-2:] == ["--model", "protolabs/smart"]  # --jobs sits BEFORE the optional --model
    assert args.index("--jobs") < args.index("--model")
    # exactly the flags we expect, nothing else crept in
    flags = [a for a in args if a.startswith("--")]
    assert flags == ["--provider", "--json", "--state-dir", "--since", "--jobs", "--model"]


async def test_jobs_is_a_separate_argument_never_glued_to_another(tmp_path):
    calls: list = []
    await runner(tmp_path, calls, {"structural_jobs": 4}).review(1, "octo/repo")
    args = calls[0]
    i = args.index("--jobs")
    assert args[i + 1] == "4" and not args[i + 1].startswith("-")
    assert all(isinstance(a, str) for a in args)  # an int here would crash the subprocess spawn


async def test_a_transient_failure_retry_keeps_the_cap(tmp_path):
    """The retry re-runs the same command: it must not quietly drop the cap and re-flood the lane."""
    calls: list = []
    r = runner(
        tmp_path,
        calls,
        {"time_budget_s": 600, "structural_jobs": 3},
        script=[{"rc": 4, "stderr": FETCH_FAILED}, {"rc": 0}],
    )
    out = await r.review(1, "octo/repo")
    assert len(calls) == 2, "expected the transient failure to be retried once"
    assert [value_after(c, "--jobs") for c in calls] == ["3", "3"]
    assert "Race in prune()" in out


async def test_concurrent_reviews_each_carry_the_cap(tmp_path):
    import asyncio

    calls: list = []
    r = runner(tmp_path, calls, {"structural_jobs": 2})
    await asyncio.gather(r.review(1, "octo/repo"), r.review(2, "octo/repo"), r.review(3, "octo/other"))
    assert len(calls) == 3 and all(value_after(c, "--jobs") == "2" for c in calls)


# -- the shipped manifest ----------------------------------------------------------------------------


MANIFEST = Path(pp.__file__).parent / "protoagent.plugin.yaml"


def test_the_manifest_default_agrees_with_the_code(caplog):
    cfg = yaml.safe_load(MANIFEST.read_text())["config"]
    assert "structural_jobs" in cfg, "the default must be declared so the console shows the real value"
    with caplog.at_level(logging.WARNING, logger=pp.log.name):
        assert structural_jobs(cfg["structural_jobs"]) == DEFAULT_STRUCTURAL_JOBS
    assert not caplog.records


def test_the_manifest_exposes_the_setting_as_an_editable_int():
    settings = yaml.safe_load(MANIFEST.read_text())["settings"]
    [field] = [f for f in settings if f["key"] == "structural_jobs"]
    assert field["type"] == "int"

"""The REAL clawpatch, killed mid-pass by the plugin's own budget, leaves state the salvage can read (#205).

test_protopatch_partial_results.py drives the salvage with a fake clawpatch writing the state layout
we ASSUME. This proves the assumption: the real binary, SIGKILLed by `ProtoPatchRunner` when
`time_budget_s` runs out, has persisted exactly the features that finished — their `claimed` set,
their status and their findings — and `review()` turns that into a PARTIAL result.

Hermetic like test_structural_jobs_clawpatch_e2e.py (same skip rules; needs clawpatch >= 0.8.0 and
git): a throwaway repo with 8 changed workflow files (8 features) and a local stand-in for the
gateway that answers 5 of them at once with a valid finding and hangs on the other 3.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import (
    PARTIAL_PREFIX,
    ProtoPatchRunner,
    classify_outage,
    outage_reason,
    pass_coverage,
    read_findings,
)

# Reuse the guard, the throwaway repo and its size from the jobs e2e (same real-binary requirements).
from tests.test_structural_jobs_clawpatch_e2e import CLAWPATCH, FEATURES, repo  # noqa: F401  (fixture import)

pytestmark = pytest.mark.skipif(
    not CLAWPATCH or not shutil.which("git"), reason="needs a working clawpatch >= 0.8.0 and git"
)

FAST = {0, 1, 2, 3, 4}  # these workflow files get an immediate answer; the rest hang
BUDGET_S = 8  # the plugin SIGKILLs the pass this long after it starts


def review_with_finding(i: int) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "title": f"w{i}: echo v2 should be pinned",
                    "category": "build-release",
                    "severity": "medium",
                    "confidence": "high",
                    "evidence": [
                        {
                            "path": f".github/workflows/w{i}.yml",
                            "startLine": 7,
                            "endLine": 7,
                            "symbol": None,
                            "quote": "run: echo v2",
                        }
                    ],
                    "reasoning": "r",
                    "reproduction": None,
                    "recommendation": "pin it",
                    "whyTestsDoNotAlreadyCoverThis": "no test",
                    "suggestedRegressionTest": None,
                    "minimumFixScope": "one line",
                }
            ],
            "inspected": {"files": [f".github/workflows/w{i}.yml"], "symbols": [], "notes": ["ok"]},
        }
    )


class StandInGateway:
    """Answers the features for FAST workflow files at once; hangs on the others until closed."""

    def __init__(self):
        self.answered = 0
        self.lock = threading.Lock()
        self.release = threading.Event()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0))).decode("utf8", "replace")
                hit = [i for i in range(FEATURES) if f"w{i}.yml" in body]
                i = hit[0] if hit else -1
                if i not in FAST:
                    outer.release.wait(60)  # a feature the budget will cut off
                    return
                payload = json.dumps(
                    {"choices": [{"finish_reason": "stop", "message": {"content": review_with_finding(i)}}]}
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                with outer.lock:
                    outer.answered += 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    g = StandInGateway()
    yield g
    g.close()


async def test_a_real_budget_kill_keeps_the_features_that_finished(tmp_path, repo, gateway, monkeypatch):  # noqa: F811
    repo_dir, base = repo
    head = "c" * 40
    changed = "\n".join(f".github/workflows/w{i}.yml" for i in range(FEATURES)) + "\n"

    async def fake_run_gh(args, timeout=30):
        return (0, f"{head} {base}", "") if args[:1] == ["api"] else (0, "", "")

    async def run_git(args, timeout_s=180):
        if args[0] == "clone":  # "clone" the throwaway repo (with its real history) into the cache slot
            shutil.copytree(repo_dir, args[-1], dirs_exist_ok=True)
            return 0, "", ""
        return (0, changed, "") if "diff" in args else (0, "", "")

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    monkeypatch.setenv("OPENAI_BASE_URL", gateway.url)
    cfg = {
        "checkout_root": str(tmp_path / "co"),
        "state_root": str(tmp_path / "st"),
        "default_repo": "",
        "clawpatch_bin": CLAWPATCH,
        "time_budget_s": BUDGET_S,
        "structural_jobs": 8,  # all 8 in flight, so the 5 fast ones finish and the 3 hung ones are cut off
        "gateway_base_url": gateway.url,
    }
    events: list = []

    class Recorder:
        def emit(self, event, **fields):
            events.append((event, fields))

    # the REAL `_run_clawpatch`: a real subprocess, a real SIGKILL
    runner = ProtoPatchRunner(cfg, run_git=run_git, telemetry=Recorder())

    started = time.monotonic()
    out = await runner.review(1, "octo/repo")
    took = time.monotonic() - started

    assert took >= BUDGET_S * 0.9, "the pass was supposed to run into the budget"
    assert gateway.answered == len(FAST), "the 5 fast features should have been answered before the kill"
    # The plugin's own result: PARTIAL, with the finished features' findings and how far it got.
    assert out.startswith(PARTIAL_PREFIX), out[:300]
    assert f"{len(FAST)} of {FEATURES} features reviewed" in out.splitlines()[0]
    findings = json.loads(out.split("```json\n", 1)[1].rsplit("```", 1)[0])
    assert sorted(f["file"] for f in findings) == sorted(f".github/workflows/w{i}.yml" for i in sorted(FAST))
    assert classify_outage(outage_reason(out)) == "budget-timeout"

    # And the state it read really is what a SIGKILLed clawpatch leaves behind: the scratch dir was
    # kept (a partial pass is evidence), and it holds the claimed set and the 5 finished features.
    (state,) = list((tmp_path / "st" / "octo-repo" / "scratch").glob("*"))
    assert pass_coverage(state) == (len(FAST), FEATURES)
    assert len(read_findings(state, None)) == len(FAST)
    assert os.path.isdir(state / "provider-failures")  # the symlinked diagnostics dir is intact too

    # #232: the stderr a SIGKILLed clawpatch wrote survives the kill, so the plan event can say which
    # features finished and which were still in flight (a hang) rather than never started (too many).
    [(event, row)] = events
    assert event == "structural_plan" and row["outcome"] == "partial" and row["reason"] == "budget-timeout"
    statuses = sorted(f["status"] for f in row["features"])
    assert statuses == ["finished"] * len(FAST) + ["killed"] * (FEATURES - len(FAST))
    assert all(isinstance(f.get("elapsed_s"), int) for f in row["features"] if f["status"] == "finished")

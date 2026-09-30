"""The REAL clawpatch honours the cap the runner asks for (#221).

test_protopatch_structural_jobs.py proves what argv the runner builds. This proves the other half:
that the real binary, handed exactly that argv, never has more than `--jobs` feature reviews in
flight. Unit tests with a fake runner cannot show that, and it is the whole point — the flag is only
worth shipping if clawpatch really stops flooding the model lane.

Hermetic: a throwaway git repo with 8 changed workflow files (8 features) and a local HTTP server
standing in for the gateway that holds each request for a beat and records how many are open at once.
No network, no model. Needs `clawpatch` (npm: @protolabsai/protopatch) and `git`; skipped without
them — CI is Python-only, so run it locally:

    CLAWPATCH_BIN=/path/to/clawpatch pytest tests/test_structural_jobs_clawpatch_e2e.py
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import DEFAULT_STRUCTURAL_JOBS, ProtoPatchRunner


def _usable_clawpatch() -> str | None:
    """A clawpatch that actually RUNS — one that is merely on PATH (a pnpm shim with no `node`
    behind it, say) must skip these tests, not fail them."""
    found = os.environ.get("CLAWPATCH_BIN") or shutil.which("clawpatch")
    if not found:
        return None
    try:
        ok = subprocess.run([found, "--version"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return found if ok else None


CLAWPATCH = _usable_clawpatch()
pytestmark = pytest.mark.skipif(
    not CLAWPATCH or not shutil.which("git"), reason="needs a working clawpatch CLI (CLAWPATCH_BIN) and git"
)

FEATURES = 8  # one per workflow file
HOLD_S = 0.6  # how long the stand-in gateway holds each request — long enough that waves are distinct
REVIEW = json.dumps({"findings": [], "inspected": {"files": [], "symbols": [], "notes": ["ok"]}})
SHA_HEAD = "a" * 40
SHA_BASE = "b" * 40


class Gateway:
    """A chat-completions stand-in that counts how many requests are open at the same moment."""

    def __init__(self):
        outer = self
        self.lock = threading.Lock()
        self.live = self.peak = self.total = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length", 0)))
                with outer.lock:
                    outer.live += 1
                    outer.total += 1
                    outer.peak = max(outer.peak, outer.live)
                time.sleep(HOLD_S)
                body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": REVIEW}}]}).encode()
                with outer.lock:
                    outer.live -= 1
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    g = Gateway()
    yield g
    g.close()


@pytest.fixture
def repo(tmp_path):
    """8 workflow files committed, then all 8 changed: `ci --since HEAD~1` reviews 8 features."""
    r = tmp_path / "repo"
    (r / ".github" / "workflows").mkdir(parents=True)

    def git(*a):
        return subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, text=True).stdout.strip()

    def write(v):
        for i in range(FEATURES):
            (r / ".github" / "workflows" / f"w{i}.yml").write_text(
                f"name: w{i}\non: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo {v}\n"
            )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    write("v1")
    git("add", "-A")
    git("commit", "-qm", "base")
    write("v2")
    git("add", "-A")
    git("commit", "-qm", "change")
    return r, git("rev-parse", "HEAD~1")


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")

    async def fake_run_gh(args, timeout=30):
        return (0, f"{SHA_HEAD} {SHA_BASE}", "") if args[:1] == ["api"] else (0, "", "")

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


async def runner_argv(tmp_path, cfg):
    """The exact argv the plugin's runner builds for a pass, captured rather than re-derived."""
    captured: list = []

    async def fake(args, cwd, env, budget_s):
        captured.append(list(args))
        return 0, "{}", "", False

    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            os.makedirs(args[-1], exist_ok=True)
        return (0, "x.yml\n", "") if "diff" in args else (0, "", "")

    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    await ProtoPatchRunner({**base, **cfg, "clawpatch_bin": CLAWPATCH}, run_git=run_git, run_clawpatch=fake).review(
        1, "o/r"
    )
    return captured[0]


def run_real(argv, repo_dir, base_sha, gateway, tmp_path):
    """Run the REAL binary with the runner's argv, pointed at the local gateway and the throwaway repo."""
    argv = list(argv)
    argv[argv.index("--since") + 1] = base_sha
    argv[argv.index("--state-dir") + 1] = str(tmp_path / "real-state")
    env = dict(os.environ, GATEWAY_API_KEY="gk", OPENAI_BASE_URL=gateway.url)
    started = time.monotonic()
    p = subprocess.run(argv, cwd=repo_dir, env=env, capture_output=True, text=True, timeout=120)
    return p, time.monotonic() - started


@pytest.mark.parametrize("jobs", [1, 2, 3, 4])
async def test_clawpatch_never_has_more_reviews_in_flight_than_the_cap(tmp_path, repo, gateway, jobs):
    repo_dir, base = repo
    argv = await runner_argv(tmp_path, {"structural_jobs": jobs})
    assert ["--jobs", str(jobs)] == argv[argv.index("--jobs") : argv.index("--jobs") + 2]
    p, took = run_real(argv, repo_dir, base, gateway, tmp_path)
    assert p.returncode == 0, p.stderr[-400:]
    assert gateway.total == FEATURES  # every feature was reviewed — the cap delays work, never drops it
    assert gateway.peak == jobs  # the cap is reached (8 features >= jobs) and never exceeded
    # serialised into waves: it cannot finish faster than ceil(features/jobs) holds back to back
    assert took >= math.ceil(FEATURES / jobs) * HOLD_S * 0.9


async def test_the_default_cap_applies_with_nothing_configured(tmp_path, repo, gateway):
    repo_dir, base = repo
    argv = await runner_argv(tmp_path, {})  # no structural_jobs at all
    p, _ = run_real(argv, repo_dir, base, gateway, tmp_path)
    assert p.returncode == 0, p.stderr[-400:]
    assert gateway.total == FEATURES and gateway.peak == DEFAULT_STRUCTURAL_JOBS


async def test_without_the_flag_clawpatch_floods_the_lane(tmp_path, repo, gateway):
    """The control: `structural_jobs: 0` (no --jobs) is the old behaviour, and it puts far more than
    the capped number in flight at once — this is the burst the cap exists to prevent."""
    repo_dir, base = repo
    # clawpatch's own default is about half the cores (max 10): only a big host floods past the cap.
    if (os.cpu_count() or 1) < 16:
        pytest.skip("this host's clawpatch default is not clearly above the cap, so there is no flood to show")
    argv = await runner_argv(tmp_path, {"structural_jobs": 0})
    assert "--jobs" not in argv
    p, _ = run_real(argv, repo_dir, base, gateway, tmp_path)
    assert p.returncode == 0, p.stderr[-400:]
    assert gateway.total == FEATURES
    assert gateway.peak > DEFAULT_STRUCTURAL_JOBS


async def test_an_over_large_setting_is_clamped_before_clawpatch_sees_it(tmp_path, repo, gateway):
    repo_dir, base = repo
    argv = await runner_argv(tmp_path, {"structural_jobs": 500})
    assert argv[argv.index("--jobs") + 1] == "10"
    p, _ = run_real(argv, repo_dir, base, gateway, tmp_path)
    assert p.returncode == 0, p.stderr[-400:]
    assert gateway.peak == FEATURES  # 8 features < 10: all in flight, but never beyond the work there is

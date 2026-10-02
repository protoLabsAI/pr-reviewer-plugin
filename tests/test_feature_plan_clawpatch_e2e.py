"""The feature plan (#232) against the REAL clawpatch: init, map, `review --feature-list`.

protoAgent#4003 in miniature. A Python repo with six packages; the PR bumps the version in
`pyproject.toml`, touches `uv.lock`, and changes one module. clawpatch's Python mapper lists
`pyproject.toml` as context of every package's feature, so `ci --since` reviews all 7 features (6
packages + the pyproject config feature). The plugin's plan reviews the 2 that own a changed file.

Hermetic like test_structural_jobs_clawpatch_e2e.py (same skip rules: clawpatch >= 0.8.0 and git), with
a local stand-in gateway that records which files each review request was about.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import (
    PARTIAL_PREFIX,
    STRUCTURAL_GAP_MARKERS,
    ProtoPatchRunner,
    classify_outage,
    outage_reason,
)

from tests.test_structural_jobs_clawpatch_e2e import CLAWPATCH, REVIEW

pytestmark = pytest.mark.skipif(
    not CLAWPATCH or not shutil.which("git"), reason="needs a working clawpatch >= 0.8.0 and git"
)

PACKAGES = 6


class RecordingGateway:
    def __init__(self):
        self.bodies: list[str] = []
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0))).decode("utf8", "replace")
                with outer.lock:
                    outer.bodies.append(body)
                payload = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": REVIEW}}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    g = RecordingGateway()
    yield g
    g.close()


@pytest.fixture
def pyrepo(tmp_path):
    r = tmp_path / "pyrepo"
    r.mkdir()

    def git(*a):
        return subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (r / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    (r / "uv.lock").write_text("lock 1\n")
    for i in range(PACKAGES):
        (r / f"p{i}").mkdir()
        (r / f"p{i}" / "__init__.py").write_text("")
        (r / f"p{i}" / "mod.py").write_text(f"def f{i}(x):\n    return x + {i}\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    (r / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.2.0"\n')
    (r / "uv.lock").write_text("lock 2\n")
    (r / "p0" / "mod.py").write_text("def f0(x):\n    return x * 2\n")
    git("commit", "-qam", "change")
    return r, git("rev-parse", "HEAD~1")


async def run_pass(tmp_path, pyrepo, gateway, monkeypatch, **cfg):
    repo_dir, base = pyrepo
    head = "c" * 40

    async def fake_run_gh(args, timeout=30):
        return (0, f"{head} {base}", "") if args[:1] == ["api"] else (0, "", "")

    async def run_git(args, timeout_s=180):
        if args[0] == "clone":  # "clone" the throwaway repo (with its real history) into the cache slot
            shutil.copytree(repo_dir, args[-1], dirs_exist_ok=True)
            return 0, "", ""
        if "diff" in args:  # the REAL diff, so numstat and name-only come from git itself
            p = subprocess.run(["git", *args], capture_output=True, text=True)
            return p.returncode, p.stdout, p.stderr
        return 0, "", ""

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    base_cfg = {
        "checkout_root": str(tmp_path / "co"),
        "state_root": str(tmp_path / "st"),
        "default_repo": "",
        "clawpatch_bin": CLAWPATCH,
        "time_budget_s": 120,
        "gateway_base_url": gateway.url,
    }
    return await ProtoPatchRunner({**base_cfg, **cfg}, run_git=run_git).review(1, "octo/repo")


def packages_reviewed(gateway) -> list[str]:
    return sorted({f"p{i}" for b in gateway.bodies for i in range(PACKAGES) if f"p{i}/mod.py" in b})


async def test_the_control_ci_since_reviews_every_feature_for_a_version_bump(tmp_path, pyrepo, gateway, monkeypatch):
    out = await run_pass(tmp_path, pyrepo, gateway, monkeypatch, structural_plan=False)
    assert not any(m in out for m in STRUCTURAL_GAP_MARKERS), out[:300]
    assert len(gateway.bodies) == PACKAGES + 1  # every package, pulled in by pyproject.toml as context
    assert packages_reviewed(gateway) == [f"p{i}" for i in range(PACKAGES)]


async def test_the_plan_reviews_only_the_features_that_own_a_changed_file(tmp_path, pyrepo, gateway, monkeypatch):
    out = await run_pass(tmp_path, pyrepo, gateway, monkeypatch)
    assert not any(m in out for m in STRUCTURAL_GAP_MARKERS), out[:300]  # complete: nothing was capped
    assert len(gateway.bodies) == 2  # p0 (changed code) + the pyproject config feature (owns the bump)
    assert packages_reviewed(gateway) == ["p0"]
    assert "plan: 2 of 2 eligible feature(s) of 7 mapped" in out
    assert "5 more depend on the diff only through a lockfile or dependency manifest" in out


async def test_a_cap_below_the_plan_is_a_partial_pass_ranked_by_changed_code(tmp_path, pyrepo, gateway, monkeypatch):
    out = await run_pass(tmp_path, pyrepo, gateway, monkeypatch, structural_max_features=1)
    assert out.startswith(PARTIAL_PREFIX), out[:300]
    assert "1 of 2 features reviewed" in out.splitlines()[0]
    assert classify_outage(outage_reason(out)) == "feature-cap"
    assert len(gateway.bodies) == 1 and packages_reviewed(gateway) == ["p0"]  # the code change outranks the bump

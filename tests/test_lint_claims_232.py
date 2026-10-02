"""Lint-rule claims are checked with the repo's pinned linter (#232 ask 6).

protoAgent#4017 r1 went FAIL on "F841" for `cfg, a, b = two_projects`. F841 does not fire on
tuple-unpack targets, and CI's pinned `ruff==0.15.10` passed the file. The structural pass now
asks that ruff before the claim can reach a verdict: no diagnostic at the cited line means the
finding is dropped. Any unknown leaves the finding exactly as it was.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pr_reviewer.lintcheck as lc
import pytest
from pr_reviewer.lintcheck import CONFIRMED, REFUTED, UNCHECKED, LintChecker, cited_rule_codes, ruff_pin

from tests.test_runner import gateway_env, make_clawpatch, make_git, pr_refs, runner  # noqa: F401 — fixtures

F841_CLAIM = "F841: local variable `a` is assigned to but never used"
TEST_FILE = "tests/test_fs_missing_root_3643.py"
TEST_SRC = "def test_root(two_projects):\n    cfg, a, b = two_projects\n    assert cfg and b\n"
CHECKS_YML = "jobs:\n  lint:\n    steps:\n      - run: pip install ruff==0.15.10 import-linter==2.11\n"


def _repo(root: Path, workflow: str | None = CHECKS_YML) -> Path:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / TEST_FILE).write_text(TEST_SRC)
    if workflow is not None:
        (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (root / ".github" / "workflows" / "checks.yml").write_text(workflow)
    return root


def _finding(claim=F841_CLAIM, file=TEST_FILE, line=2, **extra):
    return {"file": file, "line": line, "severity": "major", "claim": claim, "source": "protopatch", **extra}


class FakeTools:
    """Stands in for pip (creates the binary) and ruff (returns canned diagnostics)."""

    def __init__(self, diagnostics=None, *, ruff_rc=None, pip_rc=0, delay=0.0, boom=False):
        self.diagnostics = diagnostics if diagnostics is not None else []
        self.ruff_rc, self.pip_rc, self.delay, self.boom = ruff_rc, pip_rc, delay, boom
        self.calls: list[list[str]] = []

    async def __call__(self, args, cwd, timeout_s):
        self.calls.append(list(args))
        if self.boom:
            raise RuntimeError("tool runner bug")
        if self.delay:
            await asyncio.sleep(self.delay)
        if args[1:3] == ["-m", "pip"]:
            target = Path(args[args.index("--target") + 1])
            if self.pip_rc == 0:
                (target / "bin").mkdir(parents=True, exist_ok=True)
                (target / "bin" / "ruff").write_text("#!fake\n")
            return self.pip_rc, "", "pip failed" if self.pip_rc else ""
        rc = self.ruff_rc if self.ruff_rc is not None else (1 if self.diagnostics else 0)
        return rc, json.dumps(self.diagnostics), ""

    @property
    def ruff_calls(self):
        return [c for c in self.calls if c[1:2] == ["check"]]


def _diag(code, row, end=None):
    return {"code": code, "location": {"row": row}, "end_location": {"row": end or row}}


def _checker(tmp_path, tools, **cfg):
    return LintChecker({"lint_tools_dir": str(tmp_path / "tools"), **cfg}, tools_dir=tmp_path / "unused", run=tools)


# ── what counts as a lint claim, and as a pin ─────────────────────────────────


def test_rule_codes_are_read_from_the_claim_only():
    assert cited_rule_codes(_finding()) == ["F841"]
    assert cited_rule_codes(_finding(claim="PLR0913 and F841, again F841")) == ["PLR0913", "F841"]
    assert cited_rule_codes(_finding(claim="crash on None", evidence="ruff F841 aside")) == []
    assert cited_rule_codes(_finding(claim="the X1 field")) == []


@pytest.mark.parametrize(
    ("workflow", "pin"),
    [
        (CHECKS_YML, "0.15.10"),
        ("steps:\n  - run: uvx ruff@0.16.2 check .\n", "0.16.2"),
        ("steps:\n  - uses: astral-sh/ruff-action@v3\n    with:\n      version: '0.14.1'\n", "0.14.1"),
        ("steps:\n  - run: pip install 'ruff>=0.15'\n", None),  # a range is not a pin
        ("steps:\n  - run: pip install ruff==0.15.10\n  - run: uvx ruff@0.16.2 check\n", None),  # ambiguous
        ("steps:\n  - run: pip install pyruff==1.2.3\n", None),
    ],
)
def test_the_pin_comes_from_the_ci_workflows(tmp_path, workflow, pin):
    assert ruff_pin(_repo(tmp_path, workflow)) == pin


def test_no_workflows_no_pin(tmp_path):
    assert ruff_pin(_repo(tmp_path, workflow=None)) is None


# ── the check ─────────────────────────────────────────────────────────────────


async def test_protoagent_4017_r1_an_f841_ruff_does_not_report_is_dropped(tmp_path):
    root = _repo(tmp_path / "co")
    tools = FakeTools(diagnostics=[])  # the pinned ruff reports nothing for F841
    kept, refuted, version = await _checker(tmp_path, tools).check(root, [_finding(), _finding(claim="real bug")])
    assert [f["claim"] for f in kept] == ["real bug"] and refuted[0]["claim"] == F841_CLAIM
    assert version == "0.15.10"
    [pip] = [c for c in tools.calls if c[1:3] == ["-m", "pip"]]
    assert "ruff==0.15.10" in pip and "--only-binary=:all:" in pip and "--no-deps" in pip
    [run] = tools.ruff_calls
    assert run[run.index("--select") + 1] == "F841" and run[-1] == TEST_FILE and "--no-cache" in run


async def test_a_lint_claim_the_linter_confirms_stands(tmp_path):
    root = _repo(tmp_path / "co")
    tools = FakeTools(diagnostics=[_diag("F841", 3)])  # within tolerance of line 2
    kept, refuted, _ = await _checker(tmp_path, tools).check(root, [_finding()])
    assert kept == [_finding()] and refuted == []


async def test_a_diagnostic_far_from_the_cited_line_does_not_confirm_it(tmp_path):
    root = _repo(tmp_path / "co")
    tools = FakeTools(diagnostics=[_diag("F841", 40)])
    _, refuted, _ = await _checker(tmp_path, tools).check(root, [_finding()])
    assert len(refuted) == 1


async def test_a_neighbouring_diagnostic_about_another_name_does_not_confirm_it(tmp_path):
    root = _repo(tmp_path / "co")
    other = {**_diag("F841", 3), "message": "Local variable `unused` is assigned to but never used"}
    same = {**_diag("F841", 2), "message": "Local variable `a` is assigned to but never used"}
    _, refuted, _ = await _checker(tmp_path, FakeTools(diagnostics=[other])).check(root, [_finding()])
    assert len(refuted) == 1
    _, refuted, _ = await _checker(tmp_path, FakeTools(diagnostics=[other, same])).check(root, [_finding()])
    assert refuted == []


async def test_the_install_is_reused(tmp_path):
    root = _repo(tmp_path / "co")
    tools = FakeTools()
    checker = _checker(tmp_path, tools)
    await checker.check(root, [_finding()])
    await checker.check(root, [_finding()])
    assert len([c for c in tools.calls if c[1:3] == ["-m", "pip"]]) == 1
    assert (tmp_path / "tools" / "ruff" / "0.15.10" / "bin" / "ruff").is_file()
    assert not [p for p in (tmp_path / "tools" / "ruff").iterdir() if p.name.startswith(".tmp-")]


@pytest.mark.parametrize(
    "tools",
    [
        FakeTools(ruff_rc=2),  # ruff error: unknown code, bad config, required-version mismatch
        FakeTools(ruff_rc=124),  # the run timed out
        FakeTools(pip_rc=1),  # no install
        FakeTools(boom=True),  # a bug in the runner
    ],
)
async def test_every_failure_leaves_the_finding_unchecked(tmp_path, tools):
    root = _repo(tmp_path / "co")
    kept, refuted, _ = await _checker(tmp_path, tools).check(root, [_finding()])
    assert kept == [_finding()] and refuted == []


async def test_the_whole_check_is_time_bounded(tmp_path):
    root = _repo(tmp_path / "co")
    tools = FakeTools(delay=5)
    started = asyncio.get_running_loop().time()
    kept, refuted, _ = await _checker(tmp_path, tools, lint_check_budget_s=0.2).check(root, [_finding()])
    assert kept == [_finding()] and refuted == []
    assert asyncio.get_running_loop().time() - started < 2


@pytest.mark.parametrize(
    "finding",
    [
        _finding(line=0),  # no line
        _finding(line=None),
        _finding(file="lib/cache.ts"),  # not Python
        _finding(file="../outside.py"),  # escapes the checkout
        _finding(file="/etc/passwd.py"),
        _finding(file="tests/missing.py"),
        _finding(claim="crash on None"),  # cites no rule
    ],
)
async def test_unmappable_findings_are_not_checked(tmp_path, finding):
    root = _repo(tmp_path / "co")
    (tmp_path / "outside.py").write_text("x = 1\n")
    tools = FakeTools()
    kept, refuted, _ = await _checker(tmp_path, tools).check(root, [finding])
    assert kept == [finding] and refuted == [] and tools.calls == []


async def test_no_pin_runs_nothing(tmp_path):
    root = _repo(tmp_path / "co", workflow=None)
    tools = FakeTools()
    kept, refuted, _ = await _checker(tmp_path, tools).check(root, [_finding()])
    assert kept == [_finding()] and refuted == [] and tools.calls == []


async def test_disabled_runs_nothing(tmp_path):
    tools = FakeTools()
    kept, _, _ = await _checker(tmp_path, tools, lint_check=False).check(_repo(tmp_path / "co"), [_finding()])
    assert kept == [_finding()] and tools.calls == []


async def test_a_version_that_is_not_digits_and_dots_is_never_installed(tmp_path):
    tools = FakeTools()
    assert await _checker(tmp_path, tools)._install("0.1.0 --index-url=https://evil") is None
    assert tools.calls == []


def test_the_subprocess_environment_carries_no_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror.example/simple")
    env = lc._env()
    assert "ghp_secret" not in env.values() and "gk" not in env.values() and "sk" not in env.values()
    assert env["PIP_INDEX_URL"] == "https://mirror.example/simple" and env["PIP_NO_INPUT"] == "1"


# ── the real linter: F841 really does not fire on a tuple-unpack target ───────

_RUFF = shutil.which("ruff") or str(Path(sys.executable).parent / "ruff")


@pytest.mark.skipif(not os.access(_RUFF, os.X_OK), reason="no ruff binary on this machine")
async def test_against_a_real_ruff_the_4017_claim_is_refuted_and_a_real_one_confirmed(tmp_path):
    root = _repo(tmp_path / "co")
    (root / "tests" / "test_real.py").write_text("def test_x():\n    unused = 1\n")
    # A real F841 one line below the 4017 claim's line must not confirm a claim about `a`.
    (root / TEST_FILE).write_text(TEST_SRC.replace("    assert", "    unused = 1\n    assert"))
    checker = LintChecker({}, tools_dir=tmp_path / "tools")
    assert await checker.verdict(Path(_RUFF), root, _finding()) == REFUTED
    real = await checker.verdict(
        Path(_RUFF), root, _finding(claim="F841 `unused` is never used", file="tests/test_real.py", line=2)
    )
    assert real == CONFIRMED
    assert await checker.verdict(Path(_RUFF), root, _finding(claim="SHA256 of the payload")) == UNCHECKED


# ── through the structural pass ───────────────────────────────────────────────

RECORD = {
    "findingId": "f-1",
    "signature": "sig-1",
    "status": "open",
    "title": F841_CLAIM,
    "severity": "high",
    "category": "maintainability",
    "evidence": [{"path": TEST_FILE, "startLine": 2, "endLine": 2, "quote": "cfg, a, b = two_projects"}],
}


def _seed(args, cwd, env, budget_s):
    _repo(Path(cwd))
    state = Path(args[args.index("--state-dir") + 1]) / "findings"
    state.mkdir(parents=True, exist_ok=True)
    (state / "f1.json").write_text(json.dumps(RECORD))


async def test_the_structural_pass_drops_a_refuted_lint_claim_and_says_why(tmp_path, gateway_env, pr_refs):  # noqa: F811
    tools = FakeTools(diagnostics=[])
    r = runner(
        tmp_path,
        cfg={"lint_tools_dir": str(tmp_path / "tools")},
        run_git=make_git(f"{TEST_FILE}\n"),
        run_clawpatch=make_clawpatch(on_run=_seed),
        run_lint=tools,
    )
    out = await r.review(4017, "octo/repo")
    assert json.loads(out.split("```json\n", 1)[1].split("```")[0]) == []
    assert "1 lint claim(s) refuted by the repo's pinned ruff 0.15.10" in out
    assert f"F841 at {TEST_FILE}:2" in out


async def test_the_structural_pass_keeps_a_confirmed_lint_claim(tmp_path, gateway_env, pr_refs):  # noqa: F811
    r = runner(
        tmp_path,
        cfg={"lint_tools_dir": str(tmp_path / "tools")},
        run_git=make_git(f"{TEST_FILE}\n"),
        run_clawpatch=make_clawpatch(on_run=_seed),
        run_lint=FakeTools(diagnostics=[_diag("F841", 2)]),
    )
    out = await r.review(4017, "octo/repo")
    [finding] = json.loads(out.split("```json\n", 1)[1].split("```")[0])
    assert finding["claim"] == F841_CLAIM and "lint claim" not in out

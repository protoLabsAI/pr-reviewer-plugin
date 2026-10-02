"""Mechanically checkable claims, checked mechanically (#232 ask 6).

A finding that cites a lint rule ("F841: `a` is assigned but never used") makes a claim the
repo's own linter can settle in milliseconds. protoAgent#4017 r1 went FAIL on exactly that:
F841 on `cfg, a, b = two_projects`. F841 does not fire on tuple-unpack targets, and CI's pinned
`ruff==0.15.10` passed the file. The claim was confirmed by an LLM and posted as a major.

So, inside the structural pass's checkout at the PR head: when a finding's claim cites a ruff
rule code, on a Python file, and the repo pins ruff in its CI workflows, that ruff (exact
version, from PyPI) is run with `--select <code>` on the cited file. If it reports nothing for
that rule at or near the cited line, the claim is refuted and the finding is dropped, with that
evidence in the pass's header. If it fires, the finding stands.

Safety and posture:
  - Never raises; every unknown is "unchecked" and leaves the finding exactly as it was: no
    pin, an ambiguous pin, no `pip`, a failed install, a non-zero ruff error, unparseable
    output, a timeout, a file outside the checkout.
  - Time-bounded: the install, each run and the whole check have their own wall-clock limit.
  - Runs no repo code. ruff only reads files. Its config (pyproject/ruff.toml) is declarative,
    and `--no-cache` keeps it from writing into the checkout. The install is
    `pip --only-binary=:all: --no-deps ruff==X.Y.Z`: a wheel, no build step. The version is
    accepted only as digits and dots, so a repo cannot steer the install anywhere else. Both
    subprocesses run with a whitelisted environment, so no token reaches them.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("protoagent.plugins.pr_reviewer")

# A ruff rule code: 1–4 capitals + 3–4 digits (F841, E501, B008, UP006, PLR0913, RUF100, ASYNC100).
# Anything this matches that ruff does not know (SHA256, ISO8601) makes ruff exit 2 ⇒ unchecked.
_RULE_CODE_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Z]{1,5}[0-9]{3,4})(?![A-Za-z0-9_])")
_VERSION = r"(\d+\.\d+\.\d+)"
_PIN_PATTERNS = (
    re.compile(r"(?<![\w-])ruff\s*==\s*" + _VERSION + r"(?![\w.])"),  # pip install ruff==0.15.10
    re.compile(r"(?<![\w-])ruff@v?" + _VERSION + r"(?![\w.])"),  # uvx ruff@0.15.10
)
_RUFF_ACTION_RE = re.compile(r"ruff-action@")
_ACTION_VERSION_RE = re.compile(r"^\s*version:\s*['\"]?v?" + _VERSION + r"['\"]?\s*$", re.MULTILINE)
_STRICT_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_PYTHON_SUFFIXES = (".py", ".pyi")

LINE_TOLERANCE = 3  # a cited line a few off the diagnostic's row is the same claim
MAX_WORKFLOW_FILES = 64
MAX_WORKFLOW_BYTES = 256 * 1024
MAX_CHECKS = 10  # findings checked per pass — the rest stay unchecked
KEEP_VERSIONS = 4  # installed ruff versions kept on disk

# Environment the subprocesses see: enough to reach PyPI (or the operator's mirror) and run,
# and nothing else — no GitHub or gateway token.
_ENV_KEEP = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_TRUSTED_HOST",
    "PIP_CERT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
)

CONFIRMED = "confirmed"
REFUTED = "refuted"
UNCHECKED = "unchecked"


def cited_rule_codes(finding: dict) -> list[str]:
    """The lint rule codes a finding's CLAIM cites, in order, deduped. Only the claim: a code
    mentioned in passing in the evidence does not make the finding a lint claim."""
    seen: dict[str, None] = {}
    for code in _RULE_CODE_RE.findall(str(finding.get("claim") or "")):
        seen.setdefault(code, None)
    return list(seen)


def ruff_pin(root: Path) -> str | None:
    """The exact ruff version the repo's CI workflows pin, or None (none, or more than one).

    Read from `.github/workflows/*.y(a)ml` only: that is what CI runs. `ruff==X.Y.Z`,
    `ruff@X.Y.Z` and astral-sh/ruff-action's `version: X.Y.Z` count; a range (`ruff>=0.15`)
    is not a pin. Two different pins are ambiguous ⇒ None ⇒ nothing is checked."""
    found: set[str] = set()
    try:
        wf = root / ".github" / "workflows"
        files = sorted(p for p in wf.glob("*") if p.suffix in (".yml", ".yaml"))[:MAX_WORKFLOW_FILES]
        for path in files:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_WORKFLOW_BYTES:
                continue
            text = path.read_text(errors="replace")
            for pattern in _PIN_PATTERNS:
                found.update(pattern.findall(text))
            for m in _RUFF_ACTION_RE.finditer(text):
                window = "\n".join(text[m.end() :].splitlines()[1:12])
                if v := _ACTION_VERSION_RE.search(window):
                    found.add(v.group(1))
    except OSError:
        return None
    return found.pop() if len(found) == 1 else None


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


async def _default_run(args: list[str], cwd: Path, timeout_s: float) -> tuple[int, str, str]:
    """Run one subprocess under a hard timeout; (rc, stdout, stderr). rc 124 on timeout, 127 when
    the binary is missing. Kills the child on timeout AND on cancellation (the outer budget)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            env=_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return 127, "", str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        _kill(proc)
        with contextlib.suppress(Exception):
            await proc.communicate()
        return 124, "", "timed out"
    except BaseException:
        _kill(proc)
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _kill(proc) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


class LintChecker:
    """Checks lint-rule claims with the repo's pinned ruff. One per structural runner."""

    def __init__(self, cfg: dict | None = None, *, tools_dir: Path, run=None):
        cfg = cfg or {}
        self.enabled = bool(cfg.get("lint_check", True))
        self.budget_s = float(cfg.get("lint_check_budget_s") or 90)
        self.install_timeout_s = float(cfg.get("lint_install_timeout_s") or 60)
        self.run_timeout_s = float(cfg.get("lint_run_timeout_s") or 20)
        self.tools_dir = Path(cfg.get("lint_tools_dir") or tools_dir).absolute()  # pip runs with cwd inside it
        self._run = run or _default_run

    async def check(self, root: Path, findings: list[dict]) -> tuple[list[dict], list[dict], str]:
        """(kept, refuted, version). Never raises: on any failure every finding is kept."""
        if not self.enabled or not findings:
            return list(findings), [], ""
        try:
            return await asyncio.wait_for(self._check(root, findings), timeout=self.budget_s)
        except asyncio.TimeoutError:
            log.warning("[pr-reviewer] lint-claim check exceeded %.0fs; findings left unchecked", self.budget_s)
        except Exception:  # noqa: BLE001 — a checker bug must never cost a finding or the pass
            log.exception("[pr-reviewer] lint-claim check failed; findings left unchecked")
        return list(findings), [], ""

    async def _check(self, root: Path, findings: list[dict]) -> tuple[list[dict], list[dict], str]:
        candidates = [i for i, f in enumerate(findings) if self._checkable(root, f)][:MAX_CHECKS]
        if not candidates:
            return list(findings), [], ""
        version = ruff_pin(root)
        if not version:
            return list(findings), [], ""
        ruff = await self._install(version)
        if ruff is None:
            return list(findings), [], ""
        refuted: set[int] = set()
        for i in candidates:
            if await self.verdict(ruff, root, findings[i]) == REFUTED:
                refuted.add(i)
        return (
            [f for i, f in enumerate(findings) if i not in refuted],
            [findings[i] for i in sorted(refuted)],
            version,
        )

    @staticmethod
    def _checkable(root: Path, finding: dict) -> bool:
        line = finding.get("line")
        file = str(finding.get("file") or "")
        if not cited_rule_codes(finding) or not file.endswith(_PYTHON_SUFFIXES):
            return False
        if isinstance(line, bool) or not isinstance(line, int) or line <= 0:
            return False  # no line: nothing to compare a diagnostic against
        return _inside(root, file) is not None

    async def verdict(self, ruff: Path, root: Path, finding: dict) -> str:
        """CONFIRMED, REFUTED or UNCHECKED for one finding — does ruff report a cited rule at
        (about) the cited line? REFUTED only on a clean, parsed run that reported none of them."""
        codes = cited_rule_codes(finding)
        rel = str(finding.get("file") or "")
        if _inside(root, rel) is None:
            return UNCHECKED
        args = [
            str(ruff),
            "check",
            "--no-cache",
            "--no-fix",
            "--output-format",
            "json",
            "--select",
            ",".join(codes),
            "--",
            rel,
        ]
        rc, out, _err = await self._run(args, root, self.run_timeout_s)
        if rc not in (0, 1):
            return UNCHECKED  # 2 = ruff error (unknown code, bad config); 124 timeout; 127 missing
        try:
            diagnostics = json.loads(out or "[]")
        except json.JSONDecodeError:
            return UNCHECKED
        if not isinstance(diagnostics, list):
            return UNCHECKED
        line = int(finding["line"])
        named = _names(str(finding.get("claim") or ""))
        for d in diagnostics:
            if not isinstance(d, dict) or d.get("code") not in codes:
                continue
            start = (d.get("location") or {}).get("row")
            end = (d.get("end_location") or {}).get("row") or start
            if not (isinstance(start, int) and start - LINE_TOLERANCE <= line <= int(end) + LINE_TOLERANCE):
                continue
            # A neighbouring diagnostic about a DIFFERENT name is not this claim: "F841 on `a`"
            # is not confirmed by ruff's F841 on `unused` two lines down.
            reported = _names(str(d.get("message") or ""))
            if named and reported and not (named & reported):
                continue
            return CONFIRMED
        return REFUTED

    async def _install(self, version: str) -> Path | None:
        """The ruff binary for `version`, installed once into the tools dir. None on any failure."""
        if not _STRICT_VERSION_RE.match(version):
            return None
        base = self.tools_dir / "ruff"
        final = base / version
        binary = final / "bin" / "ruff"
        if binary.is_file():
            return binary
        try:
            base.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=base))
        except OSError:
            return None
        try:
            rc, out, err = await self._run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--only-binary=:all:",
                    "--no-compile",
                    "--target",
                    str(tmp),
                    f"ruff=={version}",
                ],
                base,
                self.install_timeout_s,
            )
            if rc != 0 or not (tmp / "bin" / "ruff").is_file():
                log.warning(
                    "[pr-reviewer] could not install ruff %s for the lint-claim check: %s", version, (err or out)[-300:]
                )
                return None
            try:
                tmp.rename(final)
            except OSError:
                pass  # another review installed it first — use theirs
            self._prune(base, keep=version)
            return binary if binary.is_file() else None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    @staticmethod
    def _prune(base: Path, keep: str) -> None:
        """Hold the tools dir to `KEEP_VERSIONS` installed versions (oldest dropped). Best-effort."""
        try:
            installed = sorted(
                (p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name != keep),
                key=lambda p: p.stat().st_mtime,
            )
            for stale in installed[: max(0, len(installed) - (KEEP_VERSIONS - 1))]:
                shutil.rmtree(stale, ignore_errors=True)
        except OSError:
            pass


_BACKTICKED = re.compile(r"`([^`\n]{1,80})`")


def _names(text: str) -> set[str]:
    """The `backticked` names in a claim or a ruff message."""
    return {m.strip() for m in _BACKTICKED.findall(text) if m.strip()}


def _inside(root: Path, rel: str) -> Path | None:
    """`root/rel` when it is a regular file that really lives inside `root` (no `..`, no
    absolute path, no symlink out of the checkout); else None."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        return None
    try:
        base = root.resolve()
        path = (root / rel).resolve()
        path.relative_to(base)
    except (OSError, ValueError, RuntimeError):
        return None
    return path if path.is_file() else None


def render_refuted(refuted: list[dict], version: str) -> str:
    """The header clause naming what the pinned linter refuted — "" when nothing was."""
    if not refuted:
        return ""
    items = "; ".join(
        f"{'/'.join(cited_rule_codes(f))} at {f.get('file')}:{f.get('line')}" for f in refuted[:MAX_CHECKS]
    )
    return (
        f", {len(refuted)} lint claim(s) refuted by the repo's pinned ruff {version} and dropped "
        f"(no such diagnostic at the cited line: {items})"
    )

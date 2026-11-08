"""Read-only search of the PR head checkout, for the panel's lanes (issue #311).

The review lanes had no working repo search. In the 10-30 → 11-08 audit window, 459
`search_files` / `find_files` calls (in 42% of panels) got **0** hits:

  * 394 failed with "unknown project 'protoLabsAI/<repo>'. Known: workspace". Those are the
    host's filesystem tools, inherited by every subagent since protoAgent#4471. On Vera they are
    bound to ONE project: the empty default workspace `/sandbox/workspace`. No project is a PR.
  * 24 searched that empty workspace and got "(no matches)", which a lane then read as proof
    that a test, caller or file is absent. An error that looks like an answer is worse than one
    that does not.
  * The github plugin has no code search at all; GitHub's own code search indexes only the
    default branch, so it cannot see a PR head either.

Absence and universality claims ("no tests", "never called", "every entry is named X") are the
main false-positive class in two precision audits. They are claims about the whole repo, and
the lanes had no way to check one where it is made. `absence_search.py` checks some of them
after the fact; these tools let the lane check before it writes the finding.

Two tools, both read-only, both over the SAME checkout the structural pass and the absence
search use (`CheckoutCache`, keyed by repo@head):

  * `search_pr_repo(pattern, pr, repo, path, ...)`: `git grep` of the head TREE, so file:line
    hits with the line text, bounded.
  * `list_pr_repo_files(pr, repo, path, ...)`: the head tree's paths under a directory or glob,
    with the total count.

Scoping. The model names a PR, never a ref or a directory. The head is the one the dispatcher
bound for that (repo, PR) when it opened the round (`bind_review`); outside a round (a manual
`run_workflow`) it is resolved from GitHub, server-side, as `protopatch_review` does. Every
git command reads the commit's OBJECTS (`git grep <sha>`, `git ls-tree <sha>`), never the
working tree. A symlink is a blob holding its target's name, and is never followed. Nothing
outside that commit can be read: there is no path argument that reaches the filesystem, and a
path with `..`, an absolute path or pathspec magic is refused before git runs.

Three outcomes, which never read as one another:

  * `SEARCH RESULTS` / `FILES`: hits, with a `complete` or `truncated` marker.
  * `NO MATCHES` / `PATH MATCHES NO FILES`: the search RAN over the named files at the head,
    and says how many files it read. That is evidence of absence.
  * `SEARCH ERROR`: the search could not run: no checkout, a bad regex, git failed, a timeout.
    It says outright that it is NOT evidence of absence. A git exit status of 1 with anything on
    stderr is an error, not "no matches".

Host-free: `langchain_core` is imported only in `get_tools`; the git runner, the checkout
resolver and the head resolver are seams.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path

log = logging.getLogger("protoagent.plugins.pr_reviewer")

# What a result starts with. The lane prompts and the tests key on these.
RESULTS = "SEARCH RESULTS"
FILES = "FILES"
NO_MATCHES = "NO MATCHES"
NO_FILES = "PATH MATCHES NO FILES"
ERROR = "SEARCH ERROR"

DEFAULT_MAX_RESULTS = 40  # match lines per search; a lane's context is shared with the diff
MAX_RESULTS_CAP = 100
DEFAULT_MAX_FILES = 200  # paths per listing (a 152-entry changelog.d fits)
MAX_FILES_CAP = 500
LINE_CHARS = 240  # a match line past this is cut, with a marker
OUTPUT_CHARS = 12_000  # a whole result past this is cut at a line boundary
PATTERN_CHARS = 500
GIT_TIMEOUT_S = 20
PATHS_PER_GREP = 1_000  # paths per `git grep` argv (well under ARG_MAX)
DEFAULT_CHECKOUT_WAIT_S = 90

_HEX_RE = re.compile(r"^[0-9a-f]{7,40}$")
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_GLOB_CHARS = re.compile(r"[*?\[]")

RunGit = Callable[[list[str], int, int], Awaitable[tuple[int, list[str], str, bool]]]
ResolveCheckout = Callable[[str, str], Awaitable["Path | None"]]
ResolveHead = Callable[[str, int], Awaitable[str]]


# ── round binding: which head a (repo, PR) is being reviewed at ─────────────────────────────
#
# Set by the dispatcher when it opens a round, next to `set_bound_repo` (#279). The tools carry
# only (pr, repo), so the head always comes from here or from GitHub, never from the model.
# Bounded LRU like the other per-round maps.
_HEADS: OrderedDict[tuple[str, int], str] = OrderedDict()
_HEADS_MAX = 256
_BACKGROUND: set[asyncio.Task] = set()  # warm-up clones, held so the loop does not drop them


def enabled(cfg: dict | None) -> bool:
    cfg = cfg or {}
    raw = cfg["repo_search"] if "repo_search" in cfg else os.environ.get("PR_REVIEWER_REPO_SEARCH", "true")
    return str(raw).strip().lower() not in ("false", "0", "no", "off", "")


def bind_review(repo: str, pr: int, head: str, cfg: dict | None = None, *, warm: bool = True) -> None:
    """Bind (repo, pr) to the round's server-resolved `head`, and start building its checkout in
    the background so it is ready before the finders' first search. Never raises."""
    try:
        key = (str(repo).strip(), int(pr))
        if not key[0] or not _HEX_RE.match(str(head or "")):
            return
        _HEADS.pop(key, None)
        _HEADS[key] = str(head)
        while len(_HEADS) > _HEADS_MAX:
            _HEADS.popitem(last=False)
        if warm and enabled(cfg):
            task = asyncio.get_running_loop().create_task(_default_resolve(cfg)(key[0], str(head)))
            _BACKGROUND.add(task)
            task.add_done_callback(_settled)
    except Exception:  # noqa: BLE001 — a search aid must never break a round
        log.exception("[pr-reviewer] binding the repo search for %s#%s failed", repo, pr)


def bound_head(repo: str, pr: int) -> str:
    return _HEADS.get((str(repo).strip(), int(pr)), "")


def _settled(task: asyncio.Task) -> None:
    _BACKGROUND.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("[pr-reviewer] repo-search checkout warm-up failed: %s", task.exception())


def _default_resolve(cfg: dict | None) -> ResolveCheckout:
    # Looked up at call time, so the test suite's no-clone resolver (tests/conftest.py) applies.
    from . import absence_search

    return absence_search.default_resolver({**(cfg or {}), "absence_search_clone": True})


async def _default_resolve_head(repo: str, pr: int) -> str:
    from .protopatch import resolve_pr_refs

    refs = await resolve_pr_refs(repo, pr)
    if isinstance(refs, str):
        raise LookupError(refs)
    return refs[0]


async def _default_run_git(args: list[str], max_lines: int, timeout_s: int) -> tuple[int, list[str], str, bool]:
    """Run `git <args>`, reading at most `max_lines` stdout lines: (rc, lines, stderr, cut).
    `cut` = the reader stopped early and killed git, which is a complete-enough success, not an
    error. A timeout is rc 124; a missing binary rc 127."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
    except FileNotFoundError:
        return 127, [], "git: command not found", False
    lines: list[str] = []
    cut = False

    async def read() -> None:
        nonlocal cut
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            if len(lines) >= max_lines:
                cut = True
                return
            lines.append(raw.decode(errors="replace").rstrip("\n"))

    try:
        await asyncio.wait_for(read(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, lines, f"git timed out after {timeout_s}s", False
    if cut:
        proc.kill()
    assert proc.stderr is not None
    err = (await proc.stderr.read()).decode(errors="replace")
    rc = await proc.wait()
    return (0 if cut else rc), lines, err, cut


# ── paths ───────────────────────────────────────────────────────────────────────────────────


def check_path(path: str) -> str:
    """The normalised repo-relative path or glob, or raise ValueError. Refuses anything that
    could name something outside the commit: absolute paths, `..`, pathspec magic, NUL."""
    p = str(path or "").strip()
    if p in ("", ".", "./", "/"):
        return ""
    if "\0" in p or "\\" in p:
        raise ValueError("path may not contain NUL or backslashes")
    if p.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", p):
        raise ValueError(f"path {p!r} is absolute; give it relative to the repo root (e.g. `tests/`)")
    if p.startswith(":"):
        raise ValueError(f"path {p!r} starts with ':' (git pathspec magic is not accepted)")
    while p.startswith("./"):
        p = p[2:]
    if any(part == ".." for part in p.split("/")):
        raise ValueError(f"path {p!r} climbs out of the repo ('..' is not accepted)")
    return p


def _glob_re(glob: str) -> re.Pattern:
    """git-style wildmatch over a full repo path: `*` and `?` stay inside one directory,
    `**/` spans directories, a trailing `/**` is everything below."""
    out, i, n = [], 0, len(glob)
    while i < n:
        c = glob[i]
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[" and (j := glob.find("]", i + 2)) != -1:
            body = glob[i + 1 : j]
            neg = body[:1] in ("!", "^")
            body = body[1:] if neg else body
            out.append("[" + ("^" if neg else "") + body.replace("\\", "\\\\") + "]")
            i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matcher(path: str) -> Callable[[str], bool]:
    """A predicate over repo paths: a literal names that file or everything under that
    directory; a glob (`*`, `?`, `[`) matches the whole path (`**/*.py` for any depth)."""
    p = check_path(path)
    if not p:
        return lambda _f: True
    if not _GLOB_CHARS.search(p):
        d = p.rstrip("/")
        return lambda f: f == d or f.startswith(d + "/")
    rx = _glob_re(p.rstrip("/") if not p.endswith("**/") else p)
    return lambda f: bool(rx.match(f))


def _clip(text: str) -> str:
    return text if len(text) <= LINE_CHARS else text[:LINE_CHARS] + " …[line cut]"


def _bounded(lines: list[str]) -> tuple[list[str], bool]:
    out, total = [], 0
    for line in lines:
        total += len(line) + 1
        if total > OUTPUT_CHARS:
            return out, True
        out.append(line)
    return out, False


def error(reason: str) -> str:
    return (
        f"{ERROR} — NOT evidence of absence: {reason}. Nothing was searched, so this says nothing "
        "about whether the code exists. Fix the call and retry, or treat any absence or "
        "universality claim this search was meant to back as unverified (do not report it as a "
        "confirmed defect)."
    )


# ── the searcher ────────────────────────────────────────────────────────────────────────────


class RepoSearch:
    """What the two tools call. Every public method returns text and never raises."""

    def __init__(
        self,
        cfg: dict | None = None,
        *,
        resolve_checkout: ResolveCheckout | None = None,
        resolve_head: ResolveHead | None = None,
        run_git: RunGit | None = None,
        telemetry=None,
    ):
        self.cfg = cfg or {}
        self._resolve_checkout = resolve_checkout
        self._resolve_head = resolve_head or _default_resolve_head
        self._run_git = run_git or _default_run_git
        self._telemetry = telemetry
        self.checkout_wait_s = float(self.cfg.get("repo_search_checkout_wait_s") or DEFAULT_CHECKOUT_WAIT_S)
        self._trees: OrderedDict[tuple[str, str], list[str]] = OrderedDict()  # (root, head) → paths

    # -- target ----------------------------------------------------------------------------

    async def _target(self, pr, repo: str) -> tuple[str, str, Path] | str:
        """(repo, head, checkout root), or an error reason."""
        if not enabled(self.cfg):
            return "repo search is disabled on this agent (`repo_search: false`)"
        try:
            number = int(pr)
        except (TypeError, ValueError):
            return f"pr must be the pull-request number, got {pr!r}"
        from .protopatch import bound_repo

        name = (repo or "").strip() or bound_repo(number) or str(self.cfg.get("default_repo") or "")
        if not _REPO_RE.match(name):
            return f"repo must be owner/name, got {name!r}"
        head = bound_head(name, number)
        if not head:
            other = bound_repo(number)
            if other and other != name:
                # A round is reviewing PR #N in ANOTHER repo: a lane passing this repo is reading
                # the wrong codebase (#279), so it gets told, not a search of some other PR.
                return f"PR #{number} is under review in {other}, not {name}; pass repo={other!r}"
            try:
                head = await self._resolve_head(name, number)
            except Exception as exc:  # noqa: BLE001
                return f"could not resolve the head of {name}#{number}: {exc}"
        if not _HEX_RE.match(head or ""):
            return f"no head commit for {name}#{number}"
        root = await self._checkout(name, head)
        if isinstance(root, str):
            return root
        return name, head, root

    async def _checkout(self, repo: str, head: str) -> Path | str:
        resolve = self._resolve_checkout or _default_resolve(self.cfg)
        task = asyncio.ensure_future(resolve(repo, head))
        _BACKGROUND.add(task)
        task.add_done_callback(_settled)
        try:
            # Shielded: a clone cut off mid-write would leave a half-checkout the cache serves as a
            # hit, so a slow clone finishes in the background and the next call finds it.
            root = await asyncio.wait_for(asyncio.shield(task), timeout=self.checkout_wait_s)
        except asyncio.TimeoutError:
            return f"the checkout of {repo}@{head[:12]} is still being built; retry in a minute"
        except Exception as exc:  # noqa: BLE001
            return f"no checkout of {repo}@{head[:12]}: {exc}"
        if not root:
            return f"no checkout of {repo}@{head[:12]} is available on this agent"
        return Path(root)

    async def _tree(self, root: Path, head: str) -> list[str] | str:
        key = (str(root), head)
        if key in self._trees:
            self._trees.move_to_end(key)
            return self._trees[key]
        rc, lines, err, _cut = await self._run_git(
            ["-C", str(root), "-c", "core.quotePath=false", "ls-tree", "-r", "--name-only", head],
            1_000_000,
            GIT_TIMEOUT_S,
        )
        if rc != 0:
            return f"could not list the tree at {head[:12]} (git rc {rc}: {err.strip()[:200]})"
        paths = [p for p in lines if p]
        self._trees[key] = paths
        while len(self._trees) > 16:
            self._trees.popitem(last=False)
        return paths

    def _emit(self, kind: str, repo: str, pr, head: str, outcome: str, started: float, **fields) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.emit(
                "repo_search",
                kind=kind,
                repo=repo,
                pr=pr,
                sha=head,
                outcome=outcome,
                elapsed_ms=round((time.monotonic() - started) * 1000),
                **fields,
            )
        except Exception:  # noqa: BLE001
            pass

    # -- search ----------------------------------------------------------------------------

    async def search(
        self,
        pattern: str,
        pr,
        repo: str = "",
        path: str = "",
        *,
        fixed: bool = False,
        ignore_case: bool = False,
        word: bool = False,
        files_only: bool = False,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> str:
        started = time.monotonic()
        try:
            return await self._search(
                pattern, pr, repo, path, fixed, ignore_case, word, files_only, max_results, started
            )
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            log.exception("[pr-reviewer] search_pr_repo failed")
            self._emit("search", repo, pr, "", "error", started, why=type(exc).__name__)
            return error(f"unexpected {type(exc).__name__}: {exc}")

    async def _search(self, pattern, pr, repo, path, fixed, ignore_case, word, files_only, max_results, started):
        pattern = str(pattern or "")
        if not pattern.strip():
            return error("pattern is empty")
        if len(pattern) > PATTERN_CHARS or "\n" in pattern or "\0" in pattern:
            return error(f"pattern must be one line of at most {PATTERN_CHARS} characters")
        try:
            match = path_matcher(path)
            shown_path = check_path(path) or "(whole repo)"
        except ValueError as exc:
            return error(str(exc))
        limit = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), MAX_RESULTS_CAP))
        target = await self._target(pr, repo)
        if isinstance(target, str):
            self._emit("search", repo, pr, "", "error", started, why=target[:120])
            return error(target)
        name, head, root = target
        tree = await self._tree(root, head)
        if isinstance(tree, str):
            self._emit("search", name, pr, head, "error", started, why=tree[:120])
            return error(tree)
        files = [f for f in tree if match(f)]
        where = f"{name}@{head[:12]}"
        if not files:
            self._emit("search", name, pr, head, "no-files", started, path=shown_path)
            return (
                f"{NO_FILES} — `{shown_path}` matches none of the {len(tree)} file(s) in {where}, so "
                "nothing was searched. Check the path with list_pr_repo_files (a glob is matched "
                "against the whole path: use `**/*.py` for any depth)."
            )
        mode = "-F" if fixed else "-P"
        # `-z` keeps a path holding ':' parseable on match lines; with `-l` it would join every path
        # into ONE NUL-separated line, so a file listing stays newline-separated.
        flags = ["-I", "--no-color"] + (["-l"] if files_only else ["-n", "-z"])
        flags += (["-i"] if ignore_case else []) + (["-w"] if word else [])
        hits: list[str] = []
        cut = False
        for start in range(0, len(files), PATHS_PER_GREP):
            chunk = files[start : start + PATHS_PER_GREP]
            args = ["-C", str(root), "-c", "core.quotePath=false", "grep", *flags, mode, "-e", pattern, head, "--"]
            args += [f":(literal){f}" for f in chunk]
            rc, lines, err, stopped = await self._run_git(args, limit - len(hits), GIT_TIMEOUT_S)
            if rc == 128 and mode == "-P" and ("Perl-compatible" in err or "PCRE" in err):
                mode = "-E"  # a git built without PCRE: extended regex instead
                args[args.index("-P")] = "-E"
                rc, lines, err, stopped = await self._run_git(args, limit - len(hits), GIT_TIMEOUT_S)
            if rc == 1 and not err.strip():
                continue  # this chunk has no match
            if rc != 0:
                why = err.strip().splitlines()[-1] if err.strip() else f"git exited {rc}"
                hint = " (use fixed=true to search for the text literally)" if "fatal: -e option" in err else ""
                self._emit("search", name, pr, head, "error", started, why=why[:120])
                return error(f"git grep failed: {why[:300]}{hint}")
            hits.extend(lines)
            if stopped:
                cut = True  # git had more lines than the cap: say so, never a silent "complete"
                break
        regex = "literal text" if fixed else ("regex (PCRE)" if mode == "-P" else "regex (ERE)")
        opts = ", ".join(x for x in (regex, "ignore case" if ignore_case else "", "whole word" if word else "") if x)
        if not hits:
            self._emit("search", name, pr, head, "no-matches", started, path=shown_path, files=len(files))
            return (
                f"{NO_MATCHES} — `{pattern}` ({opts}) occurs in none of the {len(files)} file(s) under "
                f"`{shown_path}` in {where}. This was a complete search of those files at the reviewed "
                "head (binary files skipped): it IS evidence that the text is absent there."
            )
        rows = [self._row(raw, head, files_only) for raw in hits]
        body, over = _bounded(rows)
        cut = cut or over
        n = len(body)
        what = "file(s)" if files_only else "match(es)"
        status = (
            f"truncated: showing the first {n} {what}; there are more — narrow the pattern or path"
            if cut
            else f"complete: {n} {what}"
        )
        self._emit("search", name, pr, head, "hits", started, path=shown_path, files=len(files), hits=n, cut=cut)
        header = f"{RESULTS} — `{pattern}` ({opts}) under `{shown_path}` in {where}, {len(files)} file(s) searched — {status}"
        return header + "\n" + "\n".join(body)

    @staticmethod
    def _row(raw: str, head: str, files_only: bool) -> str:
        # `-z`: "<sha>:<path>\0<line>\0<text>", or "<sha>:<path>" with -l.
        parts = raw.split("\0", 2)
        file = parts[0].removeprefix(f"{head}:")
        if files_only or len(parts) < 3:
            return file
        return f"{file}:{parts[1]}: {_clip(parts[2])}"

    # -- listing ---------------------------------------------------------------------------

    async def list_files(self, pr, repo: str = "", path: str = "", max_results: int = DEFAULT_MAX_FILES) -> str:
        started = time.monotonic()
        try:
            try:
                match = path_matcher(path)
                shown_path = check_path(path) or "(whole repo)"
            except ValueError as exc:
                return error(str(exc))
            limit = max(1, min(int(max_results or DEFAULT_MAX_FILES), MAX_FILES_CAP))
            target = await self._target(pr, repo)
            if isinstance(target, str):
                self._emit("list", repo, pr, "", "error", started, why=target[:120])
                return error(target)
            name, head, root = target
            tree = await self._tree(root, head)
            if isinstance(tree, str):
                self._emit("list", name, pr, head, "error", started, why=tree[:120])
                return error(tree)
            files = [f for f in tree if match(f)]
            where = f"{name}@{head[:12]}"
            if not files:
                self._emit("list", name, pr, head, "no-files", started, path=shown_path)
                return (
                    f"{NO_FILES} — no file at the reviewed head of {where} matches `{shown_path}` "
                    f"(a complete listing of its {len(tree)} file(s)). A glob is matched against the "
                    "whole path: use `**/` for any depth."
                )
            body, over = _bounded(files[:limit])
            cut = over or len(files) > len(body)
            status = (
                f"showing the first {len(body)} of {len(files)}; narrow the path" if cut else f"complete: {len(files)}"
            )
            self._emit("list", name, pr, head, "files", started, path=shown_path, files=len(files))
            return f"{FILES} — `{shown_path}` in {where}: {len(files)} file(s) ({status})\n" + "\n".join(body)
        except Exception as exc:  # noqa: BLE001 — degrade, never raise
            log.exception("[pr-reviewer] list_pr_repo_files failed")
            return error(f"unexpected {type(exc).__name__}: {exc}")


def get_tools(cfg: dict, *, searcher: RepoSearch | None = None) -> list:
    """`search_pr_repo` + `list_pr_repo_files`, marked read-only (`metadata.access = "read"`), so
    a host with subagent read-tool inheritance (protoAgent#4327) binds them to every lane that
    inherits reads: the review-finders and the verifier."""
    from langchain_core.tools import tool

    if searcher is None:
        from .protopatch import _telemetry

        searcher = RepoSearch(cfg, telemetry=_telemetry(cfg))

    @tool
    async def search_pr_repo(
        pattern: str,
        pr: int,
        repo: str = "",
        path: str = "",
        fixed: bool = False,
        ignore_case: bool = False,
        word: bool = False,
        files_only: bool = False,
        max_results: int = 40,
    ) -> str:
        """Search the WHOLE repository at the head commit of the pull request under review (read-only; git grep over a checkout of that exact commit) and return file:line hits with the line text. Use it to back any claim that something is absent or universal ("no test covers X", "f is never called", "every file under d/ does Y") and to find callers, importers and readers of a changed name. This is the only search that sees this PR: search_files / find_files / read_file point at an unrelated local workspace. Results start with SEARCH RESULTS (hits), NO MATCHES (the search ran over the stated number of files and found nothing: evidence of absence), PATH MATCHES NO FILES (nothing was searched) or SEARCH ERROR (the search did not run: NOT evidence of anything). Args: pattern = a Perl-compatible regex (set fixed=true to search literal text such as `foo(`); pr = the pull-request number; repo = owner/name (always pass the review's repo); path = optional directory or glob relative to the repo root, e.g. `tests/`, `**/*.py`, `changelog.d/*.md` (a glob matches the whole path); fixed, ignore_case, word = grep options; files_only = list matching files instead of lines; max_results = up to 100 (default 40)."""
        return await searcher.search(
            pattern,
            pr,
            repo,
            path,
            fixed=fixed,
            ignore_case=ignore_case,
            word=word,
            files_only=files_only,
            max_results=max_results,
        )

    @tool
    async def list_pr_repo_files(pr: int, repo: str = "", path: str = "", max_results: int = 200) -> str:
        """List the files in the repository at the head commit of the pull request under review (read-only), under a directory or glob, with the total count. Use it to check that a file exists, to see what a directory holds, and to count before claiming that every file of a kind follows a convention. Results start with FILES, PATH MATCHES NO FILES (a complete listing found none: evidence the path does not exist at this head) or SEARCH ERROR (NOT evidence of anything). Args: pr = the pull-request number; repo = owner/name (always pass the review's repo); path = optional directory or glob relative to the repo root, e.g. `tests/`, `**/test_*.py` (a glob matches the whole path); max_results = up to 500 (default 200)."""
        return await searcher.list_files(pr, repo, path, max_results=max_results)

    for t in (search_pr_repo, list_pr_repo_files):
        t.metadata = {**(t.metadata or {}), "access": "read"}
    return [search_pr_repo, list_pr_repo_files]

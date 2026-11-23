"""Per-file selection — which changed files the panel's finders review (#385, slice 1).

Before this, every changed file went to every finder: a lockfile bump spent finder
budget on `uv.lock`, and one huge generated file could crowd out the code around it.
`select_files` makes one decision per changed file, in input order, each with exactly
one typed reason:

- ``selected`` — the finders review it.
- ``excluded:path`` — a default excluded path (the lockfiles in `LOCKFILE_NAMES`,
  matched by basename at any depth, and `*.min.js`) or a caller-supplied glob.
- ``excluded:generated`` — `.gitattributes` marks it `linguist-generated`
  (or `linguist-generated=true`); `-linguist-generated`, `!linguist-generated` and
  `linguist-generated=false` unset it, and the last matching line wins, as in git.
- ``deleted`` — GitHub reports it `removed`. Kept in the result for context, never
  selected: there is no head-side code to review.
- ``too-large`` — its patch is STRICTLY over the per-file limit. A file exactly at the
  limit stays selected. Never silently dropped: the coverage line names each one.

Precedence when several apply (first match wins): ``deleted`` → ``excluded:path`` →
``excluded:generated`` → ``too-large``. A removed `uv.lock` is ``deleted``; a huge
lockfile is ``excluded:path``, not ``too-large``.

Patch size is measured in CHARACTERS of the file's unified-diff `patch` text as
GitHub's `pulls/{n}/files` returns it (`changed_file_from_github`), not changed lines:
characters are what a finder's context budget actually spends, and a few very long
lines (minified or generated output) would pass a line limit untouched.

PURE: no I/O, no `gh` calls, no config reads. The dispatcher (slice 2) fetches the
file list and the `.gitattributes` text and passes them in. Exclusion is not a
coverage gap — nothing here feeds `coverage_gaps` / `coverage_verdict`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase

SELECTED = "selected"
EXCLUDED_PATH = "excluded:path"
EXCLUDED_GENERATED = "excluded:generated"
DELETED = "deleted"
TOO_LARGE = "too-large"

# Every reason, in precedence order after `selected` (the order the coverage line groups by).
REASONS = (SELECTED, DELETED, EXCLUDED_PATH, EXCLUDED_GENERATED, TOO_LARGE)

LOCKFILE_NAMES = frozenset({"uv.lock", "package-lock.json", "pnpm-lock.yaml", "Cargo.lock", "go.sum"})
DEFAULT_EXCLUDE_GLOBS = ("*.min.js",)

# Per-file patch limit in characters — the config default slice 2 wires in. ~12k tokens:
# roomy for any hand-written change, well under one finder's working budget.
DEFAULT_MAX_PATCH_CHARS = 50_000

REMOVED_STATUS = "removed"  # GitHub's `pulls/{n}/files` status for a deleted file


@dataclass(frozen=True)
class ChangedFile:
    """One changed file: `path` (the head-side name for a rename), GitHub's `status`
    (added / modified / removed / renamed / …) and `patch_size` in characters."""

    path: str
    status: str
    patch_size: int


@dataclass(frozen=True)
class FileDecision:
    """The decision for one file. `rule` is the default/caller glob or lockfile name
    that excluded it (``excluded:path``), else empty."""

    path: str
    reason: str
    patch_size: int = 0
    rule: str = ""

    @property
    def selected(self) -> bool:
        return self.reason == SELECTED


def _norm_path(path: str) -> str:
    path = (path or "").strip()
    while path.startswith("./"):
        path = path[2:]
    return path.removeprefix("/")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def changed_file_from_github(row: Mapping[str, object]) -> ChangedFile:
    """A `pulls/{n}/files` row → `ChangedFile`. GitHub omits `patch` for binary files
    and for patches too big to inline; those count as size 0 here, so a caller that
    needs to catch the latter must size them from `changes` itself."""
    patch = row.get("patch")
    return ChangedFile(
        path=str(row.get("filename") or ""),
        status=str(row.get("status") or ""),
        patch_size=len(patch) if isinstance(patch, str) else 0,
    )


def _glob_match(path: str, pattern: str) -> bool:
    """A glob with no `/` matches the basename at any depth (`*.min.js`, `go.sum`); one
    with a `/` matches the whole repo-relative path (a leading `/` anchors nothing extra)."""
    pattern = pattern.strip()
    if not pattern:
        return False
    if "/" not in pattern.rstrip("/"):
        return fnmatchcase(_basename(path), pattern) or fnmatchcase(path, pattern)
    pattern = pattern.removeprefix("/")
    if pattern.endswith("/"):
        pattern += "**"
    return fnmatchcase(path, pattern)


def _excluded_path_rule(path: str, extra_globs: Iterable[str]) -> str:
    base = _basename(path)
    if base in LOCKFILE_NAMES:
        return base
    for pattern in (*DEFAULT_EXCLUDE_GLOBS, *extra_globs):
        if _glob_match(path, pattern):
            return pattern.strip()
    return ""


GeneratedRules = tuple[tuple[str, bool], ...]


def parse_linguist_generated(gitattributes: str) -> GeneratedRules:
    """`.gitattributes` text → ordered `(pattern, generated)` rules for the
    `linguist-generated` attribute only. `linguist-generated` / `=true` sets it;
    `-linguist-generated`, `!linguist-generated` and `=false` unset it. Comments, blank
    lines and lines without the attribute are skipped. Order is kept: the last matching
    rule wins (`is_linguist_generated`)."""
    rules: list[tuple[str, bool]] = []
    for raw in (gitattributes or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, attrs = parts[0], parts[1:]
        value: bool | None = None
        for attr in attrs:
            if attr in ("linguist-generated", "linguist-generated=true"):
                value = True
            elif attr in ("-linguist-generated", "!linguist-generated", "linguist-generated=false"):
                value = False
        if value is not None:
            rules.append((pattern, value))
    return tuple(rules)


def is_linguist_generated(path: str, rules: GeneratedRules) -> bool:
    """Does the LAST rule matching `path` mark it generated? Patterns follow the same
    matching as `_glob_match` (no `/` ⇒ basename at any depth)."""
    path = _norm_path(path)
    generated = False
    for pattern, value in rules:
        if _glob_match(path, pattern):
            generated = value
    return generated


def select_files(
    files: Iterable[ChangedFile],
    *,
    extra_exclude_globs: Iterable[str] = (),
    max_patch_chars: int = DEFAULT_MAX_PATCH_CHARS,
    gitattributes: str | GeneratedRules = "",
) -> list[FileDecision]:
    """One `FileDecision` per input file, in input order (see the module docstring for
    the reasons and their precedence). `gitattributes` is the raw `.gitattributes` text
    or rules already parsed by `parse_linguist_generated`."""
    extra = tuple(g for g in extra_exclude_globs if isinstance(g, str) and g.strip())
    rules = parse_linguist_generated(gitattributes) if isinstance(gitattributes, str) else tuple(gitattributes)
    decisions: list[FileDecision] = []
    for f in files:
        path = _norm_path(f.path)
        size = max(int(f.patch_size or 0), 0)
        if f.status == REMOVED_STATUS:
            decisions.append(FileDecision(path, DELETED, size))
        elif rule := _excluded_path_rule(path, extra):
            decisions.append(FileDecision(path, EXCLUDED_PATH, size, rule))
        elif is_linguist_generated(path, rules):
            decisions.append(FileDecision(path, EXCLUDED_GENERATED, size))
        elif size > max_patch_chars:
            decisions.append(FileDecision(path, TOO_LARGE, size))
        else:
            decisions.append(FileDecision(path, SELECTED, size))
    return decisions


def selected_paths(decisions: Iterable[FileDecision]) -> list[str]:
    """The paths the finders review, in input order."""
    return [d.path for d in decisions if d.selected]

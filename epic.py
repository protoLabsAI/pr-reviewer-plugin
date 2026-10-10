"""Epic attestation — review only what the slice PRs did not already review.

protoAgent ships large features on long-lived `epic/*` branches. Every slice PR (base
`epic/<name>`) gets the full panel and is squash-merged into the epic, so when the epic
finally opens against the default branch its diff is the WHOLE epic, every line of which
was already reviewed slice by slice. Re-reviewing it is slow, expensive and pointless.

For such a PR every commit on the epic's first-parent chain (`base..head`) is attributed:

- **slice** — the merge (usually squash) commit of a merged PR whose base was this epic,
  and that PR has a COMPLETE, VERIFIED **PASS** panel round at its final head (the same
  `strictest_head_round` the `QA panel` gate reads). A WARN, an incomplete pass, an
  unverified pass or no verdict at all does not attest: the slice is residual.
- **sync** — a merge bringing the base branch into the epic (its second parent is on the
  base branch) whose tree is exactly what `git merge-tree` of its parents produces: it
  introduces nothing beyond its parents, so there is nothing of its own to review.
- **residual** — everything else: direct pushes, conflict-resolution merges, merges of
  other branches, and slices without a complete PASS.

All attested ⇒ the dispatcher posts a PASS whose body is the attestation table. Any
residual ⇒ the panel runs, scoped to the residual commits' combined diff.

Fails CLOSED: a lookup that cannot be answered (a `gh` read failing, an unreadable
verdict history, a git command failing) raises `AttestationUnavailable`, and the caller
falls back to a FULL review — never to an attested PASS. A git question that has an
honest "no" (merge-tree reports conflicts, a parent is not on the base) makes the commit
residual, which only means more review.

Host-free: git runs through an injected `run_git` (the checkout cache's), GitHub through
injected lookups, so tests drive this with a real repo and faked GitHub.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

EPIC_PREFIX = "epic/"
SLICE = "slice"
SYNC = "sync"
EMPTY = "empty"  # a residual-looking commit that, diffed, introduces nothing
RESIDUAL = "residual"

# More commits than this on one epic ⇒ attestation is not attempted (a full review runs).
# Each commit can cost two GitHub reads; the bound keeps one event from spending hundreds.
MAX_EPIC_COMMITS = 300
# Attestation-table rows rendered into a posted body before the rest are summarized —
# GitHub caps a review body at 65,536 characters.
MAX_TABLE_ROWS = 150

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# Wrapper tags of the `<review_scope>` block; a diff line cannot close them early.
_SCOPE_TAGS = ("review_scope", "residual_diff")
_CLOSING_TAG_RE = re.compile(r"</\s*(" + "|".join(_SCOPE_TAGS) + r")\s*>", re.IGNORECASE)

RunGit = Callable[[list[str]], Awaitable[tuple[int, str, str]]]


class AttestationUnavailable(Exception):
    """A fact attestation needs could not be read. The caller runs a FULL review."""


def is_epic_pr(facts: dict | None, repo: str) -> bool:
    """An `epic/*` branch of THIS repo, opened against the repo's default branch."""
    if not facts:
        return False
    head_ref = str(facts.get("head_ref") or "")
    base_ref = str(facts.get("base_ref") or "")
    default = str(facts.get("default_branch") or "")
    head_repo = str(facts.get("head_repo") or "")
    return (
        head_ref.startswith(EPIC_PREFIX)
        and len(head_ref) > len(EPIC_PREFIX)
        and bool(default)
        and base_ref == default
        # A fork's `epic/x` is not this repo's epic: its slices were never PRs here.
        and head_repo.lower() == repo.lower()
    )


@dataclass
class Commit:
    sha: str
    parents: list[str]
    subject: str = ""


@dataclass
class Attribution:
    sha: str
    kind: str  # SLICE | SYNC | RESIDUAL
    subject: str = ""
    pr: int | None = None  # the slice PR, when one was found (attested or not)
    verdict: str = ""  # the slice PR's verdict at its final head, as read
    slice_head: str = ""
    why: str = ""  # one line: why this commit is what it is


@dataclass
class EpicScope:
    """The attribution of an epic PR's commits, plus — when some are residual — the
    combined residual diff the panel is scoped to."""

    epic_ref: str
    base_ref: str
    commits: list[Attribution] = field(default_factory=list)
    residual_diff: str = ""
    residual_paths: list[str] = field(default_factory=list)
    diff_truncated: bool = False

    @property
    def attested(self) -> list[Attribution]:
        return [c for c in self.commits if c.kind != RESIDUAL]

    @property
    def residual(self) -> list[Attribution]:
        return [c for c in self.commits if c.kind == RESIDUAL]

    @property
    def all_attested(self) -> bool:
        return bool(self.commits) and not self.residual


# ── git (real, through the injected runner) ──────────────────────────────────────


async def _git(run_git: RunGit, cwd: Path, *args: str) -> str:
    rc, out, err = await run_git(["-C", str(cwd), *args])
    if rc != 0:
        raise AttestationUnavailable(f"git {args[0]} failed (rc {rc}): {err.strip()[:200]}")
    return out


async def resolve_base(run_git: RunGit, cwd: Path, base_ref: str, base_sha: str = "") -> str:
    """The base branch's CURRENT tip in the checkout: fetched fresh, else the clone's
    remote-tracking ref, else the PR's recorded base SHA. Unresolvable ⇒ unavailable."""
    rc, _out, _err = await run_git(
        ["-C", str(cwd), "fetch", "--filter=blob:none", "--no-tags", "--quiet", "origin", f"refs/heads/{base_ref}"]
    )
    candidates = (["FETCH_HEAD"] if rc == 0 else []) + [f"refs/remotes/origin/{base_ref}"]
    if base_sha:
        candidates.append(base_sha)
    for ref in candidates:
        rc, out, _err = await run_git(["-C", str(cwd), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
        sha = out.strip()
        if rc == 0 and _SHA_RE.match(sha):
            return sha
    raise AttestationUnavailable(f"base {base_ref!r} not resolvable in the checkout")


async def epic_commits(run_git: RunGit, cwd: Path, base: str, head: str) -> list[Commit]:
    """The epic's own commits, oldest first: the first-parent chain of `head` not
    reachable from `base`. `%P` is the commit's TRUE parent list (first-parent only limits
    the walk), so a merge still shows both parents."""
    out = await _git(run_git, cwd, "log", "--first-parent", "--reverse", "--format=%H %P%x09%s", f"{base}..{head}")
    commits: list[Commit] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        shas, _, subject = line.partition("\t")
        parts = shas.split()
        if not parts or not all(_SHA_RE.match(p) for p in parts):
            raise AttestationUnavailable(f"unparseable git log line: {line[:80]!r}")
        commits.append(Commit(sha=parts[0], parents=parts[1:], subject=subject.strip()))
    return commits


async def merge_tree(run_git: RunGit, cwd: Path, ours: str, theirs: str) -> tuple[str | None, bool]:
    """(tree git would produce merging `theirs` into `ours`, clean?). rc 0 = clean, rc 1 =
    conflicted (the tree then carries conflict markers); anything else — an old git
    without `--write-tree`, a failure — is (None, False): unknown, so never clean."""
    rc, out, _err = await run_git(["-C", str(cwd), "merge-tree", "--write-tree", ours, theirs])
    first = out.splitlines()[0].strip() if out.strip() else ""
    if rc in (0, 1) and _SHA_RE.match(first):
        return first, rc == 0
    return None, False


async def tree_of(run_git: RunGit, cwd: Path, sha: str) -> str:
    return (await _git(run_git, cwd, "rev-parse", f"{sha}^{{tree}}")).strip()


async def is_ancestor(run_git: RunGit, cwd: Path, ancestor: str, of: str) -> bool:
    rc, _out, _err = await run_git(["-C", str(cwd), "merge-base", "--is-ancestor", ancestor, of])
    if rc not in (0, 1):
        raise AttestationUnavailable(f"git merge-base --is-ancestor failed (rc {rc})")
    return rc == 0


async def clean_merge(run_git: RunGit, cwd: Path, commit: Commit) -> bool:
    """A two-parent merge whose tree is EXACTLY its parents' automatic merge — it adds no
    conflict resolution and no edit of its own."""
    if len(commit.parents) != 2:
        return False
    tree, clean = await merge_tree(run_git, cwd, commit.parents[0], commit.parents[1])
    return bool(clean and tree and tree == await tree_of(run_git, cwd, commit.sha))


async def sync_merge(run_git: RunGit, cwd: Path, commit: Commit, base: str) -> bool:
    """A clean merge bringing the BASE branch into the epic. A clean merge of any other
    branch is not attested: that branch's content was never reviewed anywhere."""
    if len(commit.parents) != 2 or not await is_ancestor(run_git, cwd, commit.parents[1], base):
        return False
    return await clean_merge(run_git, cwd, commit)


async def residual_diff_for(run_git: RunGit, cwd: Path, commit: Commit, base: str) -> tuple[str, list[str]]:
    """(diff, paths) of what `commit` itself introduces into the epic. For a merge of the
    BASE branch that is the difference between git's automatic merge of its parents and what
    was committed — the conflict resolution (and any edit), not the base's own, already
    reviewed changes. Any other commit, a merge of some other branch included, is diffed
    against its first parent: everything it brought in. When merge-tree cannot say, the
    first-parent diff too (more review, never less)."""
    left = commit.parents[0] if commit.parents else ""
    if len(commit.parents) == 2 and await is_ancestor(run_git, cwd, commit.parents[1], base):
        tree, _clean = await merge_tree(run_git, cwd, commit.parents[0], commit.parents[1])
        if tree:
            left = tree
    if not left:  # a root commit — everything it holds is its own
        left = (await _git(run_git, cwd, "hash-object", "-t", "tree", "/dev/null")).strip()
    diff = await _git(run_git, cwd, "diff", "--no-color", "--no-ext-diff", left, commit.sha)
    names = await _git(run_git, cwd, "diff", "--name-only", "--no-ext-diff", left, commit.sha)
    return diff, [n.strip() for n in names.splitlines() if n.strip()]


# ── attribution ──────────────────────────────────────────────────────────────────

# `pulls_for(sha)` → the PRs GitHub associates with a commit (dicts with number,
# merged_at, merge_commit_sha, base, head) — raising AttestationUnavailable when unreadable.
PullsFor = Callable[[str], Awaitable[list[dict]]]
# `slice_round(pr, head)` → the strictest panel round at that head (verdict, complete,
# verified), or None when the PR has no round there — raising when unreadable.
SliceRound = Callable[[int, str], Awaitable[dict | None]]


def _slice_pr(pulls: list[dict], sha: str, epic_ref: str) -> dict | None:
    """The merged PR into `epic_ref` whose merge commit IS `sha`. A PR GitHub merely
    associates with the commit (it contained it, it was opened from it) does not count."""
    for p in pulls:
        if not isinstance(p, dict):
            continue
        if (
            p.get("merged_at")
            and str(p.get("merge_commit_sha") or "") == sha
            and str(p.get("base") or "") == epic_ref
            and isinstance(p.get("number"), int)
        ):
            return p
    return None


def slice_attests(round_: dict | None) -> tuple[bool, str]:
    """(attests?, the verdict as shown). Only a COMPLETE, VERIFIED PASS — the same bar the
    promotion gate holds an approve-on-green to. A WARN, an incomplete-coverage pass (the
    `QA panel` check's `neutral`), an unverified pass or no round at all do not."""
    if not round_:
        return False, "no verdict"
    verdict = str(round_.get("verdict") or "")
    if verdict != "PASS":
        return False, verdict or "no verdict"
    if not round_.get("complete", True):
        return False, "PASS (incomplete coverage)"
    if not round_.get("verified", True):
        return False, "PASS (unverified)"
    return True, "PASS"


async def attribute_commits(
    commits: list[Commit],
    *,
    epic_ref: str,
    is_sync: Callable[[Commit], Awaitable[bool]],
    pulls_for: PullsFor,
    slice_round: SliceRound,
) -> list[Attribution]:
    """Attribute every commit (see the module docstring). Any lookup that raises
    `AttestationUnavailable` propagates: the caller must not attest on a partial read."""
    out: list[Attribution] = []
    for c in commits:
        if len(c.parents) == 2 and await is_sync(c):
            out.append(Attribution(c.sha, SYNC, c.subject, why="clean merge of the base branch"))
            continue
        pr = _slice_pr(await pulls_for(c.sha), c.sha, epic_ref)
        if pr is None:
            why = "conflict-resolution or non-base merge" if len(c.parents) > 1 else "direct push (no slice PR)"
            out.append(Attribution(c.sha, RESIDUAL, c.subject, why=why))
            continue
        number, slice_head = int(pr["number"]), str(pr.get("head") or "")
        if len(c.parents) > 1 and not slice_head:
            out.append(Attribution(c.sha, RESIDUAL, c.subject, pr=number, why="slice merge without a head"))
            continue
        ok, shown = slice_attests(await slice_round(number, slice_head) if slice_head else None)
        kind = SLICE if ok else RESIDUAL
        why = f"slice #{number} {shown}" if ok else f"slice #{number} not attested: {shown}"
        out.append(Attribution(c.sha, kind, c.subject, pr=number, verdict=shown, slice_head=slice_head, why=why))
    return out


async def merge_slice_is_clean(run_git: RunGit, cwd: Path, commit: Commit, slice_head: str) -> bool:
    """A slice landed with a MERGE commit (not a squash) is attested only when that merge
    took the reviewed head as-is: second parent = the reviewed head, no resolution."""
    return len(commit.parents) == 2 and commit.parents[1] == slice_head and await clean_merge(run_git, cwd, commit)


# ── rendering ────────────────────────────────────────────────────────────────────


def _escape(text: str) -> str:
    return _CLOSING_TAG_RE.sub(lambda m: f"</{m.group(1).lower()}_>", text)


def _cell(text: str, limit: int = 72) -> str:
    text = " ".join(str(text or "").split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _row(a: Attribution) -> str:
    pr = f"#{a.pr}" if a.pr else "—"
    verdict = a.verdict or {SYNC: "clean sync merge", EMPTY: "empty"}.get(a.kind, "—")
    kind = {SLICE: "✅ slice", SYNC: "✅ sync merge", EMPTY: "✅ no changes", RESIDUAL: "🔍 reviewed"}[a.kind]
    return f"| `{a.sha[:12]}` | {_cell(a.subject)} | {kind} | {pr} | {_cell(verdict, 40)} |"


def render_attestation_table(scope: EpicScope) -> str:
    rows = scope.commits[:MAX_TABLE_ROWS]
    lines = [
        "| Commit | Subject | Attribution | Slice PR | Slice verdict |",
        "|---|---|---|---|---|",
        *(_row(a) for a in rows),
    ]
    if len(scope.commits) > len(rows):
        rest = scope.commits[len(rows) :]
        lines.append(
            f"\n_…and {len(rest)} more commit(s): {sum(a.kind != RESIDUAL for a in rest)} attested, "
            f"{sum(a.kind == RESIDUAL for a in rest)} reviewed._"
        )
    return "\n".join(lines)


def render_attested_brief(scope: EpicScope) -> str:
    slices = sum(a.kind == SLICE for a in scope.commits)
    syncs = sum(a.kind == SYNC for a in scope.commits)
    empty = sum(a.kind == EMPTY for a in scope.commits)
    return (
        f"**Epic attestation** — `{scope.epic_ref}` → `{scope.base_ref}`. Every one of the "
        f"{len(scope.commits)} commit(s) on this epic was already reviewed: {slices} slice PR "
        f"merge(s), each carrying a complete, verified **PASS** at its final head, and {syncs} clean "
        f"sync merge(s) of `{scope.base_ref}` that introduce nothing beyond their parents"
        + (f", plus {empty} commit(s) that change nothing" if empty else "")
        + ". No panel was spent on this head."
    )


def render_attestation_notes(scope: EpicScope) -> str:
    """The trailing section of a posted verdict: what was attested, what was reviewed."""
    residual = scope.residual
    head = (
        "\n\n---\n### Epic attestation\n"
        f"`{scope.epic_ref}` → `{scope.base_ref}`: {len(scope.attested)} of {len(scope.commits)} commit(s) "
        "were already reviewed in their slice PRs (or are clean sync merges) and were **not** re-reviewed"
    )
    if residual:
        head += (
            f"; the panel reviewed only the residual diff of the other {len(residual)} commit(s)"
            + (" (truncated to the diff budget)" if scope.diff_truncated else "")
            + ". Findings outside the residual files are excluded from the verdict."
        )
    else:
        head += "."
    return head + "\n\n" + render_attestation_table(scope)


def render_scope_block(scope: EpicScope) -> str:
    """The `<review_scope>` data block the finders and synthesizer read."""
    attested = "\n".join(f"  - {a.sha[:12]} {_cell(a.subject)} — {a.why}" for a in scope.attested[:MAX_TABLE_ROWS])
    residual = "\n".join(f"  - {a.sha[:12]} {_cell(a.subject)} — {a.why}" for a in scope.residual)
    note = " (TRUNCATED to the diff budget — read the listed commits for the rest)" if scope.diff_truncated else ""
    return (
        "<review_scope>\n"
        f"This PR merges the long-lived epic branch `{scope.epic_ref}` into `{scope.base_ref}`. "
        f"{len(scope.attested)} of its {len(scope.commits)} commits were ALREADY reviewed by this "
        "panel in their own slice PRs (or are clean sync merges of the base) — they are OUT OF SCOPE. "
        "Review ONLY the residual changes in <residual_diff> below. You may read any code for "
        "context, but report a finding only when a residual change causes it; do not report "
        "defects in attested code.\n"
        f"Attested commits (out of scope):\n{attested or '  (none)'}\n"
        f"Residual commits (IN scope):\n{residual}\n"
        f"<residual_diff>{note}\n"
        f"{_escape(scope.residual_diff)}\n"
        "</residual_diff>\n"
        "</review_scope>"
    )


# ── the whole pass ────────────────────────────────────────────────────────────────


async def build_scope(
    run_git: RunGit,
    cwd: Path,
    *,
    epic_ref: str,
    base_ref: str,
    base_sha: str,
    head: str,
    pulls_for: PullsFor,
    slice_round: SliceRound,
    diff_budget: int,
    max_commits: int = MAX_EPIC_COMMITS,
) -> EpicScope:
    """Attribute the epic's commits in the checkout at `cwd`, and build the residual diff.
    Raises `AttestationUnavailable` whenever a fact cannot be read."""
    base = await resolve_base(run_git, cwd, base_ref, base_sha)
    commits = await epic_commits(run_git, cwd, base, head)
    if not commits:
        raise AttestationUnavailable("no epic commits between base and head")
    if len(commits) > max_commits:
        raise AttestationUnavailable(f"{len(commits)} commits exceeds the attestation bound ({max_commits})")
    by_sha = {c.sha: c for c in commits}

    async def is_sync(c: Commit) -> bool:
        return await sync_merge(run_git, cwd, c, base)

    attributions = await attribute_commits(
        commits, epic_ref=epic_ref, is_sync=is_sync, pulls_for=pulls_for, slice_round=slice_round
    )
    for a in attributions:
        c = by_sha[a.sha]
        # A slice that landed as a merge commit attests only if it merged the reviewed head clean.
        if a.kind == SLICE and len(c.parents) > 1 and not await merge_slice_is_clean(run_git, cwd, c, a.slice_head):
            a.kind, a.why = RESIDUAL, f"slice #{a.pr} merge commit adds content beyond the reviewed head"
    scope = EpicScope(epic_ref=epic_ref, base_ref=base_ref, commits=attributions)
    parts: list[str] = []
    paths: dict[str, None] = {}
    for a in scope.residual:
        diff, names = await residual_diff_for(run_git, cwd, by_sha[a.sha], base)
        if not diff.strip():
            # An empty commit, or a merge whose resolution changed nothing: nothing to review.
            a.kind, a.why = EMPTY, "introduces no changes of its own"
            continue
        parts.append(f"### commit {a.sha[:12]} — {_cell(a.subject)}\n{diff}")
        paths.update(dict.fromkeys(names))
    text = "\n".join(parts)
    if len(text) > diff_budget:
        text, scope.diff_truncated = text[:diff_budget], True
    scope.residual_diff, scope.residual_paths = text, list(paths)
    return scope

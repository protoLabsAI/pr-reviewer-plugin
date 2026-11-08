"""Incremental re-review (issue #313): re-review only what changed since the last round.

A re-review used to re-read the whole PR. Across 105 measured re-rounds the push since the
prior round touched a median 50% of the PR's files and 12% of its changed lines, yet a
re-round cost ~95% of round 1, and 48% of re-round panel time went on files byte-identical
to what the panel had already judged. The convergence machinery in `rounds.py` exists to
suppress what that re-reading produces: carried priors, relisted-prior normalization,
"round 3+ prefer silence", credited minors.

The unit here is a PR FILE, content-addressed by `(path, head blob sha, merge-base sha)`:

  - the head side comes from `/pulls/{n}/files[].sha`;
  - the prior side comes from `compare/{merge_base}...{prior_head}` `files[].sha`;
  - the cached RESULT for an unchanged file is the last spent round's posted findings
    record, i.e. GitHub-native review memory (ADR 0078 D5). This slice adds no new store.

Selection (`select_scope`). The in-scope set S is:

  - every file whose blob changed or is new, plus
  - its DEPENDENTS: an unchanged file whose PR patch names a changed (or reverted) file, or
    which holds a prior finding whose evidence cites one.

Every other file is carried (K). Its prior findings are carried forward verbatim. The
dispatcher re-grounds them at the new head and drops any whose quoted evidence is gone. The
panel is scoped through the recipe's existing `review_scope` input (`render_scope_block`),
which every finder and the synthesizer read, so no recipe prompt changes.

Everything fails CLOSED to a full round, today's path: an unreadable read, a moved
merge-base, a manifest change, no carried file, or no in-scope file. The lanes' read sets
are not observable on the current host (no `tool_calls` in the run result), so the PR is
treated as ONE cluster for read-set purposes. That is why a moved base forces a full
round rather than a per-file decision.

Pure: the dispatcher does the GitHub reads and passes facts in, like `rounds.py`.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from .grounding import mentioned_paths
from .verdicts import _norm_path

# The only recipe that declares `review_scope` (the core four-finder `code-review` lives in
# the host and has no such input). A small-diff round always reviews in full; it is cheap.
INCREMENTAL_RECIPE = "code-review-structural"

# What a carried row is stamped with. `carried: True` plus a `carried_by` other than
# "synthesizer" is the existing contract for a DISPATCHER carry: `verification_ran` does not
# read it as a finding the verifier missed, and `rounds.carried_debt` holds promotion on a
# confirmed blocking one (#220/#260). An incremental carry is exactly that.
CARRIED_BY = "incremental"
INCREMENTAL_NOTE = "carried unchanged: this file is byte-identical to the round that raised it (incremental, #313)"

# The compare API lists at most 300 files. A list that long may be truncated, so it cannot
# prove a file unchanged and the round goes full.
COMPARE_FILE_CAP = 300

# A change to one of these can alter how EVERY file builds or behaves, so a PR-wide
# dependency is assumed and the round goes full.
FULL_REVIEW_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "pyproject.toml",
        "uv.lock",
        "poetry.lock",
        "pipfile",
        "pipfile.lock",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "requirements-dev.txt",
        "cargo.toml",
        "cargo.lock",
        "go.mod",
        "go.sum",
        "gemfile",
        "gemfile.lock",
        "tsconfig.json",
        "composer.json",
        "composer.lock",
        "protoagent.plugin.yaml",
    }
)

# A bare stem this generic would match nearly every patch, so it is matched only as
# `dir/stem` (or the full path).
GENERIC_STEMS = frozenset(
    {
        "index",
        "__init__",
        "__main__",
        "main",
        "mod",
        "lib",
        "util",
        "utils",
        "helpers",
        "types",
        "constants",
        "config",
        "settings",
        "common",
        "core",
        "base",
        "test",
        "tests",
        "conftest",
        "setup",
        "app",
        "readme",
        "changelog",
        "init",
        "errors",
        "models",
        "schema",
        "style",
        "styles",
    }
)
MIN_STEM_CHARS = 4

# Bounds on the rendered `<review_scope>` block.
MAX_SCOPE_LIST = 150

# Why a round reviewed in full (the `incremental` telemetry value is `full:<reason>`).
FULL_SUMMON = "summon"
FULL_EPIC = "epic-scope"
FULL_RECIPE = "recipe"
FULL_FIRST = "first-round"
FULL_PRIOR_INCOMPLETE = "prior-incomplete"
FULL_PRIOR_UNVERIFIED = "prior-unverified"
FULL_PRIOR_RECIPE = "prior-recipe"
FULL_UNREADABLE = "unreadable"
FULL_BASE_MOVED = "merge-base-moved"
FULL_TRUNCATED = "file-list-truncated"
FULL_MANIFEST = "manifest-changed"
FULL_NOTHING_CHANGED = "no-pr-file-changed"
FULL_EVERYTHING_CHANGED = "every-file-in-scope"

_RECIPE_RE = re.compile(r"^_([a-z0-9][a-z0-9-]*) · head `", re.MULTILINE)
_CLOSING_TAG_RE = re.compile(r"</\s*(review_scope|residual_diff)\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class FileRow:
    """One PR file as GitHub reports it: its path, blob sha, status and patch."""

    path: str
    sha: str
    status: str = ""
    patch: str = ""
    previous: str = ""


@dataclass
class Plan:
    """What an incremental round reviews and what it carries.

    `mode` is "scoped" (review S, carry K) or "full" (today's round). Only a scoped plan
    changes anything."""

    mode: str
    reason: str = ""
    prior_head: str = ""
    # path -> why it is in scope: "changed" | "new" | "dependent" | "cited"
    in_scope: dict[str, str] = field(default_factory=dict)
    carried_files: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # Prior findings carried forward on K, stamped (see `carry_row`).
    carried: list[dict] = field(default_factory=list)
    # Prior findings the panel still sees as `prior_findings`: every prior NOT on K.
    scoped_priors: list[dict] = field(default_factory=list)
    # Carried rows dropped at the new head (re-grounding found their evidence gone), and
    # priors not carried because their record already called them ungrounded or refuted.
    carried_dropped: list[dict] = field(default_factory=list)
    total_files: int = 0

    @property
    def scoped(self) -> bool:
        return self.mode == "scoped"

    @classmethod
    def full(cls, reason: str) -> "Plan":
        return cls(mode="full", reason=reason)

    def label(self) -> str:
        return "scoped" if self.scoped else f"full:{self.reason}"


def recipe_of(body: str) -> str:
    """The recipe a posted verdict body names in its header line (`_<recipe> · head …_`),
    or "" when there is none."""
    m = _RECIPE_RE.search(body or "")
    return m.group(1) if m else ""


def file_rows(raw: object) -> dict[str, FileRow] | None:
    """`{path: FileRow}` from GitHub file rows, or None when any row is unusable."""
    if not isinstance(raw, list):
        return None
    out: dict[str, FileRow] = {}
    for row in raw:
        if not isinstance(row, dict):
            return None
        path = _norm_path(str(row.get("filename") or ""))
        sha = str(row.get("sha") or "")
        if not path:
            return None
        out[path] = FileRow(
            path=path,
            sha=sha,
            status=str(row.get("status") or ""),
            patch=str(row.get("patch") or ""),
            previous=_norm_path(str(row.get("previous_filename") or "")),
        )
    return out


def reference_tokens(path: str) -> set[str]:
    """How other code names `path`: the extension-less path, its dotted module form, its
    `dir/stem` form, and the bare stem when that is distinctive enough."""
    path = _norm_path(path)
    root, _ext = posixpath.splitext(path)
    stem = posixpath.basename(root)
    parent = posixpath.basename(posixpath.dirname(root))
    tokens = {path, root}
    if "/" in root:
        tokens.add(root.replace("/", "."))
    if parent:
        tokens.add(f"{parent}/{stem}")
    if len(stem) >= MIN_STEM_CHARS and stem.lower() not in GENERIC_STEMS:
        tokens.add(stem)
    if stem.lower() in ("index", "__init__", "mod") and parent and len(parent) >= MIN_STEM_CHARS:
        # `import "./widgets"` / `from . import widgets` names the package, not its index.
        if parent.lower() not in GENERIC_STEMS:
            tokens.add(parent)
    return {t for t in tokens if t}


def references(text: str, tokens: set[str]) -> bool:
    """Does `text` name any token as a whole word (`rounds` in `from .rounds import x`, not in
    `surrounds`)?"""
    if not text or not tokens:
        return False
    for token in tokens:
        if re.search(r"(?<![\w])" + re.escape(token) + r"(?![\w])", text):
            return True
    return False


def _cites(finding: dict, changed: set[str]) -> bool:
    """Does the finding's evidence cite a changed file (by path or bare name)?"""
    for cited in mentioned_paths(finding):
        cited = _norm_path(cited)
        for path in changed:
            if path == cited or path.endswith("/" + cited):
                return True
    return False


def carry_row(finding: dict, origin: str) -> dict:
    """A prior finding carried verbatim, stamped as a dispatcher carry. Its severity,
    verdict, line, evidence and flags are the round's own; `since` keeps the head it was
    RAISED at (#131) so a later fix is proven against that head."""
    note = str(finding.get("note") or "").strip()
    if INCREMENTAL_NOTE not in note:
        note = f"{note} — {INCREMENTAL_NOTE}" if note else INCREMENTAL_NOTE
    return {
        **finding,
        "carried": True,
        "carried_by": CARRIED_BY,
        "since": str(finding.get("since") or origin),
        "note": note,
    }


def select_scope(
    prior_round: dict | None,
    head_rows: dict[str, FileRow] | None,
    prior_rows: dict[str, FileRow] | None,
    *,
    prior_recipe: str,
    recipe: str,
) -> Plan:
    """Decide what this re-round reviews and what it carries. Pure. Fails closed: any
    missing fact gives a full plan."""
    if prior_round is None:
        return Plan.full(FULL_FIRST)
    if recipe != INCREMENTAL_RECIPE:
        return Plan.full(FULL_RECIPE)
    if prior_recipe != INCREMENTAL_RECIPE:
        return Plan.full(FULL_PRIOR_RECIPE)
    if not prior_round.get("complete", True):
        # A lane did not finish last round, so "unchanged since round N-1" is not "reviewed".
        return Plan.full(FULL_PRIOR_INCOMPLETE)
    if not prior_round.get("verified", True):
        return Plan.full(FULL_PRIOR_UNVERIFIED)
    if not head_rows or prior_rows is None:
        return Plan.full(FULL_UNREADABLE)
    origin = str(prior_round.get("head") or "")
    in_scope: dict[str, str] = {}
    for path, row in head_rows.items():
        before = prior_rows.get(path)
        if before is None:
            in_scope[path] = "new"
        elif not row.sha or not before.sha or row.sha != before.sha or row.status != before.status:
            in_scope[path] = "changed"
    removed = sorted(p for p in prior_rows if p not in head_rows)
    moved = set(in_scope) | set(removed)
    if any(posixpath.basename(p).lower() in FULL_REVIEW_NAMES for p in moved):
        return Plan.full(FULL_MANIFEST)
    if not moved:
        # Same PR files at the same bytes, under a new head: nothing for a scoped panel to
        # do. Reaffirm-by-diff normally catches this first; when it could not, review in full.
        return Plan.full(FULL_NOTHING_CHANGED)
    tokens: set[str] = set()
    for path in moved:
        tokens |= reference_tokens(path)
    for path, row in head_rows.items():
        if path not in in_scope and references(row.patch, tokens):
            in_scope[path] = "dependent"
    priors = [f for f in (prior_round.get("findings") or []) if isinstance(f, dict)]
    for f in priors:
        file = _norm_path(str(f.get("file") or ""))
        if file in head_rows and file not in in_scope and _cites(f, moved):
            in_scope[file] = "cited"
    carried_files = sorted(p for p in head_rows if p not in in_scope)
    if not carried_files:
        return Plan.full(FULL_EVERYTHING_CHANGED)
    keep = set(carried_files)
    carried: list[dict] = []
    scoped_priors: list[dict] = []
    dropped: list[dict] = []
    for f in priors:
        file = _norm_path(str(f.get("file") or ""))
        if file not in keep:
            scoped_priors.append(f)  # the panel dispositions it, exactly as today
            continue
        if str(f.get("verdict") or "").lower() == "refuted" or f.get("ungrounded"):
            dropped.append(f)  # never carried as a live finding; the ledger skips these too
            continue
        carried.append(carry_row(f, origin))
    return Plan(
        mode="scoped",
        prior_head=origin,
        in_scope=dict(sorted(in_scope.items())),
        carried_files=carried_files,
        removed=removed,
        carried=carried,
        scoped_priors=scoped_priors,
        carried_dropped=dropped,
        total_files=len(head_rows),
    )


def drop_ungrounded_carries(plan: Plan, grounded: list[dict]) -> None:
    """Apply the new head's re-grounding to the carried rows (positional, like
    `apply_grounding`). A carried row whose quoted evidence is no longer at head is dropped
    from the carry and recorded in `carried_dropped`."""
    keep: list[dict] = []
    for before, after in zip(plan.carried, grounded):
        if after.get("ungrounded"):
            plan.carried_dropped.append(before)
        else:
            keep.append(before)
    plan.carried = keep


def split_panel_findings(reported: list[dict], plan: Plan) -> tuple[list[dict], list[dict]]:
    """(kept, dropped). Removes the panel's own findings on a CARRIED file when they are:

      - minor/nit: fresh re-sampling of code already judged at identical bytes, the
        re-litigation class #313 measured (31 of 73 re-round findings, almost all
        minor/nit); or
      - a re-statement of a row the dispatcher already carries.

    A blocker/major the panel raises there is kept: a defect an in-scope change causes in
    unchanged code must not be lost to scoping."""
    keep = set(plan.carried_files)
    from .rounds import same_prior

    kept: list[dict] = []
    dropped: list[dict] = []
    for f in reported or []:
        if not isinstance(f, dict):
            continue
        if _norm_path(str(f.get("file") or "")) not in keep:
            kept.append(f)
            continue
        minor = str(f.get("severity") or "").lower() in ("minor", "nit")
        if minor or any(same_prior(f, c) for c in plan.carried):
            dropped.append(f)
        else:
            kept.append(f)
    return kept, dropped


def merge_carries(reported: list[dict], plan: Plan) -> list[dict]:
    """`reported` with the plan's carried rows appended."""
    return [*reported, *plan.carried]


def _escape(text: str) -> str:
    return _CLOSING_TAG_RE.sub(lambda m: f"</{m.group(1).lower()}_>", text)


def render_scope_block(plan: Plan, round_number: int, residual: dict[str, str] | None, budget: int) -> str:
    """The `<review_scope>` data block every finder and the synthesizer read. `residual` maps
    an in-scope path to its prior→head patch, and is None when the push rewrote history (no
    clean delta). The diff is bounded by `budget` characters."""
    prior = plan.prior_head[:12]
    scope_rows = [f"  - {p} ({why})" for p, why in plan.in_scope.items()]
    carried_count: dict[str, int] = {}
    for c in plan.carried:
        path = _norm_path(str(c.get("file") or ""))
        carried_count[path] = carried_count.get(path, 0) + 1
    carry_rows = [
        f"  - {p}" + (f" ({carried_count[p]} carried finding(s))" if carried_count.get(p) else "")
        for p in plan.carried_files
    ]

    def bounded(rows: list[str]) -> str:
        if len(rows) <= MAX_SCOPE_LIST:
            return "\n".join(rows)
        return "\n".join(rows[:MAX_SCOPE_LIST]) + f"\n  - … and {len(rows) - MAX_SCOPE_LIST} more"

    diff = ""
    note = ""
    if residual is None:
        note = " (unavailable: the push rewrote history since that round, so review each in-scope file's whole PR diff)"
    else:
        parts: list[str] = []
        used = 0
        for path in plan.in_scope:
            patch = residual.get(path)
            if not patch:
                continue
            chunk = f"--- {path}\n{patch}\n"
            if budget > 0 and used + len(chunk) > budget:
                note = " (TRUNCATED to the diff budget — read the in-scope files for the rest)"
                break
            parts.append(chunk)
            used += len(chunk)
        diff = "".join(parts)
    removed = ""
    if plan.removed:
        removed = "Reverted since that round (no longer in this PR):\n" + bounded([f"  - {p}" for p in plan.removed])
        removed += "\n"
    return (
        "<review_scope>\n"
        f"INCREMENTAL RE-REVIEW, round {round_number}. This panel already reviewed this PR in full "
        f"at head `{prior}` (round {round_number - 1}). Only the files listed IN SCOPE below changed "
        "since then, or depend on a file that did. Every file listed OUT OF SCOPE is byte-identical "
        "to what that round reviewed. The dispatcher carries that round's findings on those files "
        "forward itself, so do not re-report them.\n"
        "Review ONLY the residual changes: the in-scope files, with the changes shown in "
        "<residual_diff>. You may read any file for context, including out-of-scope ones. Report a "
        "finding only when an in-scope change causes it. Anchor it at the in-scope change where "
        "you can. A defect a residual change causes in an out-of-scope file IS in scope: report it "
        "only if it is a blocker or major. Do not report fresh minor/nit findings in out-of-scope "
        "files: they were judged at identical content.\n"
        f"In scope ({len(plan.in_scope)} of {plan.total_files} files):\n{bounded(scope_rows)}\n"
        f"Out of scope, unchanged since round {round_number - 1} ({len(plan.carried_files)} files):\n"
        f"{bounded(carry_rows)}\n"
        f"{removed}"
        f"<residual_diff>{note}\n"
        f"{_escape(diff)}"
        "</residual_diff>\n"
        "</review_scope>"
    )


def render_incremental_note(plan: Plan, out_of_scope: list[dict]) -> str:
    """The body trailer for a scoped round, so a reader knows what this round re-read and what
    it carried."""
    lines = [
        "\n\n---\n_Incremental re-review (#313): "
        f"{len(plan.in_scope)} of {plan.total_files} file(s) changed since head `{plan.prior_head[:12]}` "
        f"(or depend on a change) and were reviewed. {len(plan.carried_files)} unchanged file(s) were not "
        f"re-read. {len(plan.carried)} finding(s) on them are carried forward unchanged._"
    ]
    if plan.carried_dropped:
        lines.append(
            f"\n_{len(plan.carried_dropped)} prior finding(s) on unchanged files were not carried: "
            "their evidence is no longer at this head, or their record already called them refuted or "
            "ungrounded._"
        )
    if out_of_scope:
        lines.append(
            f"\n_{len(out_of_scope)} fresh minor/nit (or duplicate) finding(s) on unchanged files were "
            "set aside: that code was already judged at identical content._"
        )
    lines.append("\n_Comment `@vera review` for a full re-review._")
    return "".join(lines)


def lane_seconds_saved(timings: dict | None, plan: Plan) -> float | None:
    """An estimate of the finder-lane seconds this round did not spend: Σ finder seconds ×
    carried/in-scope files. That assumes cost ∝ files, the audit's conservative case. It is
    capped at 10× so a one-file scope cannot claim an absurd saving. None when there are no
    timings."""
    if not timings or not plan.in_scope:
        return None
    finder_s = sum(float(v) for k, v in timings.items() if str(k).startswith("find_") and isinstance(v, (int, float)))
    ratio = min(len(plan.carried_files) / len(plan.in_scope), 10.0)
    return round(finder_s * ratio, 1)


def telemetry_fields(plan: Plan | None, *, out_of_scope: int = 0, timings: dict | None = None) -> dict:
    """The `reviewed`-row fields. Empty when the flag is off, so that row is unchanged."""
    if plan is None:
        return {}
    if not plan.scoped:
        return {"incremental": plan.label()}
    return {
        "incremental": plan.label(),
        "files_reviewed": len(plan.in_scope),
        "files_carried": len(plan.carried_files),
        "carried_kept": len(plan.carried),
        "carried_dropped": len(plan.carried_dropped),
        "out_of_scope_dropped": out_of_scope,
        "lane_s_saved_est": lane_seconds_saved(timings, plan),
    }

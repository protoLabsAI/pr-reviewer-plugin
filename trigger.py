"""The structural trigger — full panel or lite, decided server-side (ADR 0078 D2).

Quinn's #891: eyeballing "is this PR big/sensitive?" from a truncated diff under-fired
at ~5%; computing it from authoritative PR JSON + the full changed-file list fired at
61.4%. Ported VERBATIM from protoWorkstacean's pr-inspector `computeStructuralTrigger`:
strictly more than 3 files, strictly more than 120 changed lines (additions +
deletions, from the PR JSON — never a truncated diff), or any changed path matching
the sensitive-path regex (run over the FULL diff's paths). Any reason ⇒ the
structural (five-finder) recipe; none ⇒ the lite four-finder panel.

Pure function — the dispatcher feeds it fields from `GET /pulls/{n}` and the full
changed-path list; nothing here talks to the network.
"""

from __future__ import annotations

import re

STRUCTURAL_FILE_THRESHOLD = 3  # strictly more ⇒ structural
STRUCTURAL_LINE_THRESHOLD = 120  # additions + deletions; strictly more ⇒ structural

# Quinn's SENSITIVE_PATH_RE, ported verbatim (pr-inspector.ts:456).
SENSITIVE_PATH_RE = re.compile(
    r"(^|/)(\.github/|dockerfile|docker-compose|compose\.ya?ml)"
    r"|(auth|session|token|crypto|oauth|password|secret|payment|billing|migrat)",
    re.IGNORECASE,
)


def structural_trigger(*, changed_files: int, lines_changed: int, changed_paths: list[str]) -> tuple[bool, list[str]]:
    """(fires, reasons). Reasons are stable strings — they land in telemetry and the
    posted verdict header. `changed_files`/`lines_changed` come from the PR JSON
    (`changed_files`, `additions + deletions`); `changed_paths` from the full diff."""
    reasons: list[str] = []
    if changed_files > STRUCTURAL_FILE_THRESHOLD:
        reasons.append(f"{changed_files} files changed (> {STRUCTURAL_FILE_THRESHOLD})")
    if lines_changed > STRUCTURAL_LINE_THRESHOLD:
        reasons.append(f"{lines_changed} lines changed (> {STRUCTURAL_LINE_THRESHOLD})")
    sensitive = [p for p in changed_paths if SENSITIVE_PATH_RE.search(p)]
    if sensitive:
        reasons.append(f"sensitive path(s): {', '.join(sensitive[:5])}")
    return bool(reasons), reasons

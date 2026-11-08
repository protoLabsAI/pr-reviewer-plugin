"""Reviewer of record: a PR another reviewer owns, mirrored instead of re-reviewed (issue #318).

The project board runs protoAgent's review-finder panel on a frontier model as its own
review gate, and posts the verdict as a commit status (`board/review-gate`). On the board's
PRs that gate caught 16 real defects in a 24-finding sample, of which Vera raised 1 in
full; and of Vera's Major findings on heads the gate passed, 2 of 10 were real. Board PRs
were 47% of her panel time. So on a PR the board marks as its own (a label), Vera stops
spending a panel and carries the board's verdict into the gates that read hers:

- the record's status is `success` at the head: post a PASS through the normal verdict
  path (marker, `QA panel` check, everything `Review at head` and the board read), saying
  whose verdict it is. No panel runs.
- `pending`: hold. The record is still reviewing; the sweep comes back.
- `failure`/`error`: hold. The record's own fix round owns the PR. Vera posts NOTHING:
  a FAIL from her would be read back by the board as the external panel failing its head
  (projectBoard#354/#512), and the merge stays blocked anyway because no verdict exists.
- no status at all after `grace_s`: the record is not reviewing this head (the board
  dropped the card, an operator pushed past it, the gate is off). Vera reviews it herself,
  so no head is ever left with nobody reviewing it.

An operator summon always runs Vera's own panel. Configured by `pr_reviewer.reviewers_of_record`:
a list of `{label, status, grace_s, name}`; empty (the default) changes nothing.

Pure decisions here; the dispatcher does the GitHub reads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

DEFAULT_GRACE_S = 1200

PASS_ = "pass"
HOLD_PENDING = "hold-pending"
HOLD_FAILED = "hold-failed"
HOLD_ABSENT = "hold-absent"
REVIEW_FALLBACK = "review-fallback"
HOLDS = (HOLD_PENDING, HOLD_FAILED, HOLD_ABSENT)


@dataclass(frozen=True)
class Rule:
    label: str
    status: str
    grace_s: int = DEFAULT_GRACE_S
    name: str = ""

    @property
    def who(self) -> str:
        return self.name or self.status


def parse_rules(raw) -> list[Rule]:
    """`reviewers_of_record` as configured (a list, or its JSON) → rules. Malformed entries
    are skipped: a bad rule must never stop Vera reviewing (it only ever widens what she does)."""
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return []
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    rules: list[Rule] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        status = str(item.get("status") or "").strip()
        if not label or not status:
            continue
        try:
            grace = int(item.get("grace_s", DEFAULT_GRACE_S))
        except (TypeError, ValueError):
            grace = DEFAULT_GRACE_S
        rules.append(Rule(label, status, max(0, grace), str(item.get("name") or "").strip()))
    return rules


def match(rules: list[Rule], labels) -> Rule | None:
    have = {str(x).strip().lower() for x in (labels or [])}
    return next((r for r in rules if r.label.lower() in have), None)


def decide(state: str | None, head_age_s: float | None, grace_s: int) -> str:
    """The record's status state at the head (None: it posted none) → what Vera does.

    An unreadable head age is treated as fresh: holding costs one sweep pass, while reviewing
    a head the record is about to review spends a whole panel for nothing."""
    s = (state or "").strip().lower()
    if s == "success":
        return PASS_
    if s == "pending":
        return HOLD_PENDING
    if s in ("failure", "error"):
        return HOLD_FAILED
    if head_age_s is None or head_age_s < grace_s:
        return HOLD_ABSENT
    return REVIEW_FALLBACK


def attested_brief(rule: Rule, head: str, description: str) -> str:
    desc = f" (“{description.strip()}”)" if description and description.strip() else ""
    return (
        f"**Reviewer of record: {rule.who}.** This PR is labelled `{rule.label}`, so its review "
        f"belongs to that reviewer, and its `{rule.status}` status is **success** at `{head[:12]}`{desc}. "
        f"This PASS carries that verdict into the gates that read this panel's; no panel ran on this head. "
        f"Comment `@vera review` for an independent review."
    )

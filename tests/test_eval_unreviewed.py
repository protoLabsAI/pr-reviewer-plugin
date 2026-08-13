"""Exhaustion outcomes in the eval report (issue #54).

`exhaustions` counts panels that produced no verdict; it cannot say whether the sweep's
backfill later recovered the PR. That difference is the whole signal — a recovered
exhaustion is noise, an unrecovered one is a PR that can merge past the gate with no
review at all.
"""

from __future__ import annotations

from pr_reviewer.eval import build_report


def _exhaustion(repo: str, pr: int, ts: float) -> dict:
    return {"event": "exhaustion", "repo": repo, "pr": pr, "ts": ts, "failed": ["find_crossfile"]}


def _reviewed(repo: str, pr: int, ts: float, posted: bool = True) -> dict:
    return {"event": "reviewed", "repo": repo, "pr": pr, "ts": ts, "posted": posted, "verdict": "PASS"}


def test_exhaustion_with_no_later_verdict_is_unreviewed():
    report = build_report([_exhaustion("o/r", 1, 100.0)])
    assert report["unreviewed_prs"] == {"count": 1, "recovered": 0, "prs": ["o/r#1"]}


def test_a_later_posted_verdict_counts_as_recovered():
    report = build_report([_exhaustion("o/r", 1, 100.0), _reviewed("o/r", 1, 200.0)])
    assert report["unreviewed_prs"]["count"] == 0
    assert report["unreviewed_prs"]["recovered"] == 1
    assert report["unreviewed_prs"]["prs"] == []


def test_a_verdict_BEFORE_the_exhaustion_does_not_recover_it():
    """Ordering is the whole point: an earlier round's verdict says nothing about a
    later head that then failed to review."""
    report = build_report([_reviewed("o/r", 1, 100.0), _exhaustion("o/r", 1, 200.0)])
    assert report["unreviewed_prs"]["count"] == 1


def test_an_unposted_review_does_not_recover():
    """`complete` but `posted: false` never reached the PR, so the gate is still silent."""
    report = build_report([_exhaustion("o/r", 1, 100.0), _reviewed("o/r", 1, 200.0, posted=False)])
    assert report["unreviewed_prs"]["count"] == 1


def test_repeat_exhaustion_keys_on_the_last_one():
    """Exhausted, recovered, exhausted again → still outstanding, counted once."""
    events = [
        _exhaustion("o/r", 1, 100.0),
        _reviewed("o/r", 1, 150.0),
        _exhaustion("o/r", 1, 200.0),
    ]
    report = build_report(events)
    assert report["unreviewed_prs"] == {"count": 1, "recovered": 0, "prs": ["o/r#1"]}


def test_recovery_is_per_pr_not_global():
    events = [
        _exhaustion("o/r", 1, 100.0),
        _exhaustion("o/r", 2, 100.0),
        _reviewed("o/r", 1, 200.0),
    ]
    report = build_report(events)
    assert report["unreviewed_prs"]["count"] == 1
    assert report["unreviewed_prs"]["recovered"] == 1
    assert report["unreviewed_prs"]["prs"] == ["o/r#2"]


def test_pr_list_is_capped_but_count_is_not():
    events = [_exhaustion("o/r", n, 100.0) for n in range(30)]
    report = build_report(events)
    assert report["unreviewed_prs"]["count"] == 30
    assert len(report["unreviewed_prs"]["prs"]) == 20


def test_no_exhaustions_is_clean_not_absent():
    """The key must always be present — a cron alerting on it shouldn't KeyError on a
    healthy day."""
    report = build_report([_reviewed("o/r", 1, 100.0)])
    assert report["unreviewed_prs"] == {"count": 0, "recovered": 0, "prs": []}


def test_empty_telemetry_does_not_crash():
    assert build_report([])["unreviewed_prs"]["count"] == 0

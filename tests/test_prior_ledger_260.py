"""One state per prior (#260), on the real bodies the 2026-10-03 audit found contradicting
themselves (tests/fixtures/prior_ledger_260/):

- data-plugin#1@eb377e62 (round 2): `tools.py:130`/`:155` read 🚫 refuted in Prior requests,
  `carried: true, verdict: confirmed … keeps gating` in Findings, AND "unaccounted" in the
  footer — under a WARN verdict. Round 1's four minors were fixed and never credited.
- protoLab#34@3ced0209 (round 9): one bare-import blocker recorded at :51, :53 and :54; priors
  dispositioned `open` also called unaccounted; a `disp=` row (:51) nobody dispositioned; and
  a "confirmed" carry whose own note reads "gap: unverified — PR head reads 404".
"""

from __future__ import annotations

import json
from pathlib import Path

from pr_reviewer.dispatch import Dispatcher
from pr_reviewer.rounds import (
    OUTCOME_NOT_HONOURED,
    OUTCOME_OPEN,
    credited_minors,
    delta_ranges,
    disposition_display,
    disposition_record,
    ledger_debt,
    panel_rounds,
    prior_ledger,
    same_prior,
    undispositioned,
    verdict_with_debt,
)
from pr_reviewer.telemetry import Telemetry
from pr_reviewer.verdicts import (
    CARRIED_NOTE,
    extract_findings_json,
    merge_carried_findings,
    parse_verdict_marker,
    read_findings_record,
    render_dispositions_table,
)

FIX = Path(__file__).parent / "fixtures" / "prior_ledger_260"
DP_R1 = (FIX / "data-plugin_1_r1.md").read_text()
DP_R2 = (FIX / "data-plugin_1_r2.md").read_text()
DP_COMPARE = json.loads((FIX / "data-plugin_1_r1_r2_compare.json").read_text())
PL_R8 = (FIX / "protoLab_34_r8.md").read_text()
PL_R9 = (FIX / "protoLab_34_r9.md").read_text()

DP_R1_HEAD = "17029d7ef0c903f1ee4234ce664ba520f28aa188"
DP_R2_HEAD = "eb377e62851d0225371bc0fb0b0ce2ec33e8b113"
# What data-plugin round 2's report said about the two round-1 majors (its Prior requests).
DP_REFUTED = [
    {
        "prior": "tools.py:130",
        "disposition": "refuted",
        "why": "DuckDB classifies DESCRIBE as StatementType.SELECT, so engine.guard() does not reject it",
    },
    {
        "prior": "tools.py:155",
        "disposition": "refuted",
        "why": "DuckDB classifies SUMMARIZE as StatementType.SELECT, so engine.guard() does not reject it",
    },
]
# What protoLab round 9's report said (its Prior requests, verbatim anchors).
PL_DISPOSITIONS = [
    {"prior": "evals/runners/run_livecodebench.py:54", "disposition": "open", "why": "bare import unchanged at head"},
    {"prior": "evals/eval-model.sh:119", "disposition": "open", "why": "still calls python -m runners.scorecard"},
    {"prior": "evals/graders/verify_coherence.py:176", "disposition": "open", "why": "SKIP still never sets failed"},
    {"prior": "evals/runners/run_ctibench.py:88", "disposition": "open", "why": "f.result() still unguarded"},
]


def _history(body: str, id: int) -> list[dict]:
    return panel_rounds([{**parse_verdict_marker(body), "body": body, "id": id}])


def _record(body: str) -> list[dict]:
    return read_findings_record(body)[0]


# ── data-plugin#1: a refutation #38 does not honour is not shown as 🚫 refuted ────────────


def test_a_refutation_of_a_confirmed_prior_on_unchanged_code_has_one_state():
    history = _history(DP_R1, 70)
    ranges = delta_ranges(DP_COMPARE)  # tools.py changed — but not at 130/155
    ledger = prior_ledger(history, DP_REFUTED, ranges=ranges)
    assert [(e["prior"]["line"], e["outcome"]) for e in ledger] == [
        (130, OUTCOME_NOT_HONOURED),
        (155, OUTCOME_NOT_HONOURED),
    ]
    owed = ledger_debt(ledger)
    assert [p["line"] for p in owed] == [130, 155]
    # Dispositioned, so the footer does not call them silent…
    assert undispositioned(ledger, owed) == []
    # …and the table says what the gate did, not 🚫.
    table = render_dispositions_table(disposition_display(ledger, DP_REFUTED, still_owed=owed))
    assert "🚫" not in table and table.count("refutation not honoured — verifier-confirmed on unchanged code") == 2
    # A carry that "keeps gating" and a WARN cannot coexist: the verdict is the one that was wrong.
    assert verdict_with_debt("WARN", owed) == "FAIL"


def test_the_minors_round_two_fixed_are_credited():
    # Round 1's four confirmed minors were all fixed in 3306afb; round 2 raised none of them.
    history = _history(DP_R1, 70)
    r2_findings = _record(DP_R2)
    credited = credited_minors(history, r2_findings, ranges=delta_ranges(DP_COMPARE))
    anchors = {f"{f['file']}:{f['line']}" for f in credited}
    assert {"engine.py:152", "engine.py:249", "tools.py:300"} <= anchors
    assert all(f["severity"] in ("minor", "nit") for f in credited)
    # Not without a readable delta — a credit is only ever made on evidence the code moved.
    assert credited_minors(history, r2_findings, ranges=None) == []


# ── protoLab#34: one defect at :51/:53/:54 is one prior ──────────────────────────────────


def test_a_drifted_anchor_is_the_same_prior():
    history = _history(PL_R8, 80)
    ledger = prior_ledger(history, PL_DISPOSITIONS, ranges={})
    lcb = [e for e in ledger if e["prior"]["file"].endswith("run_livecodebench.py")]
    assert len(lcb) == 1 and lcb[0]["outcome"] == OUTCOME_OPEN
    assert set(lcb[0]["prior"]["members"]) == {
        "evals/runners/run_livecodebench.py:51",
        "evals/runners/run_livecodebench.py:53",
        "evals/runners/run_livecodebench.py:54",
    }
    # The short-path re-anchor (`verify_coherence.py:178`) is the `evals/graders/…:176` prior.
    vc = [e for e in ledger if e["prior"]["file"].endswith("verify_coherence.py")]
    assert len(vc) == 1 and vc[0]["outcome"] == OUTCOME_OPEN
    # Every prior was dispositioned `open`: none is "unaccounted".
    assert undispositioned(ledger, ledger_debt(ledger)) == []


def test_the_disposition_record_has_no_phantom_row():
    # Round 9's marker carried `run_livecodebench.py:51` with `d=""` — the panel answered the
    # same import at :54. Every member of the defect now records that answer.
    history = _history(PL_R8, 80)
    ledger = prior_ledger(history, PL_DISPOSITIONS, ranges={})
    record = disposition_record(history[-1], PL_DISPOSITIONS, still_open=ledger_debt(ledger))
    assert record is not None and record["of"] == 80
    assert all(row["d"] == "open" for row in record["rows"]), record["rows"]


def test_a_re_anchored_re_report_is_not_carried_beside_itself():
    # Round 9 re-reported the blocker at :54; the carry of the same defect must not add :51/:53.
    history = _history(PL_R8, 80)
    owed = ledger_debt(prior_ledger(history, PL_DISPOSITIONS, ranges={}))
    fresh = [f for f in _record(PL_R9) if not f.get("carried")]
    merged = merge_carried_findings(fresh, owed)
    lcb = [f for f in merged if f["file"].endswith("run_livecodebench.py") and f["severity"] in ("blocker", "major")]
    assert len(lcb) == 1, [(f["line"], f.get("carried")) for f in lcb]


def test_a_carry_whose_own_record_says_unverified_is_never_confirmed():
    # The legacy :53 row: stamped `confirmed` by an older carry, its note reading
    # "gap: unverified — … PR head reads 404".
    legacy = next(f for f in _record(PL_R9) if f.get("carried") and f["line"] == 53)
    assert legacy["verdict"] == "confirmed" and "404" in legacy["note"]
    [carried] = merge_carried_findings([], [legacy])
    assert carried.get("verdict") != "confirmed" and carried["raised_unverified"] is True
    assert CARRIED_NOTE not in carried["note"]  # it no longer claims to be a confirmed blocker
    # An `uncertain` prior is never upgraded by a carry either.
    [uncertain] = merge_carried_findings([], [{**legacy, "verdict": "uncertain", "note": "n"}])
    assert uncertain["verdict"] == "uncertain"
    # A verifier-confirmed one with a clean record still is.
    clean = {**legacy, "note": "Line 54 has the bare import.", "carried": False}
    clean.pop("raised_unverified", None)
    assert merge_carried_findings([], [clean])[0]["verdict"] == "confirmed"


def test_same_prior_keeps_distinct_defects_apart():
    a = {"file": "x.py", "line": 10, "claim": "No test coverage for POST /api/config/oauth/poll."}
    b = {"file": "x.py", "line": 12, "claim": "No test coverage for POST /api/config/oauth/start."}
    assert not same_prior(a, b)
    assert not same_prior({**a, "file": "y.py"}, a)
    assert not same_prior(a, {**a, "line": 10 + 40})


# ── end to end: data-plugin round 2 re-run through the dispatcher ────────────────────────


class _GH:
    """data-plugin#1 at round 2: round 1's FAIL is the prior, the delta is the real compare."""

    def __init__(self):
        self.posted: list[dict] = []

    async def __call__(self, args, timeout=30):
        joined = " ".join(args)
        if "-X" in args and "POST" in args and "/reviews" in joined:
            self.posted.append({a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a})
            return 0, "{}", ""
        if "-X" in args:
            return 0, "{}", ""
        if args[1] == "user":
            return 0, "qa-bot", ""
        if "/compare/" in joined:
            return 0, json.dumps(DP_COMPARE), ""
        if "/files" in joined:
            return 0, "tools.py\nengine.py\ntests/test_plugin.py\ntests/conftest.py\n", ""
        if "/reviews" in joined:
            row = {"author": "qa-bot", "state": "CHANGES_REQUESTED", "body": DP_R1, "id": 70}
            return 0, json.dumps([row]), ""
        if "/pulls/1" in joined and "/check-runs" not in joined:
            facts = {
                "head": DP_R2_HEAD,
                "base_ref": "main",
                "state": "open",
                "draft": False,
                "locked": False,
                "changed_files": 4,
                "additions": 60,
                "deletions": 10,
                "author": "someone",
            }
            return 0, json.dumps(facts), ""
        return 0, "", ""


async def test_data_plugin_round_two_posts_one_state_per_prior(tmp_path):
    fresh = [f for f in _record(DP_R2) if not f.get("carried")]
    report = (
        "<!-- brief -->\nRound 2.\n<!-- /brief -->\n\n```json\n"
        + json.dumps(DP_REFUTED)
        + "\n```\n\n```json\n"
        + json.dumps(fresh)
        + "\n```"
    )

    async def runner(name, inputs):  # no seeding host — the gate rules on the dispositions alone
        return {"output": report, "failed": []}

    gh = _GH()
    d = Dispatcher(
        {"repos": ["o/r"], "cooldown_s": 30, "shadow_mode": False, "evidence_grounding": False},
        Telemetry(tmp_path),
        run_gh_fn=gh,
        workflow_run=runner,
    )
    out = await d.handle_pr_event("o/r", 1, DP_R2_HEAD, "synchronize")
    body = next(p["body"] for p in gh.posted if "body" in p)
    assert out == "reviewed:FAIL" and "verdict=FAIL" in body.splitlines()[0]
    assert "FAIL on carried debt" in body
    assert "🚫" not in body and "refutation not honoured" in body
    assert "Unaccounted prior finding" not in body
    carried = [f for f in json.loads(extract_findings_json(body)) if f.get("carried")]
    assert sorted(f["line"] for f in carried) == [130, 155]
    # The carry's note tells the same story as the table — not "neither fixed nor refuted".
    assert all(
        f["verdict"] == "confirmed" and "reported refuted, but a verifier confirmed" in f["note"] for f in carried
    )
    assert all(CARRIED_NOTE not in f["note"] for f in carried)
    assert "Addressed since the last round" in body and "`engine.py:152`" in body

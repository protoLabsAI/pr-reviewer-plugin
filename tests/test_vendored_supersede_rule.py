"""Drift guard for the supersede rule vendored into `scripts/review_at_head.py` (#234).

`Review at head` runs as a required check in protoAgent, protoPatch and qaEngineer, which have
no plugin checkout, so the rule it shares with `QA panel` is COPIED into the script. A copy
silently diverging from `rounds.py` is how the two checks would start disagreeing again —
the #239 symptom #234 fixed. Three guards:

1. the header's `rounds-ast-sha256` still matches the rule's source in `rounds.py`/
   `verdicts.py` — change the rule there and this fails until the block is re-vendored;
2. the header's `block-ast-sha256` matches the block (downstream copies assert the same);
3. on a broad corpus of head histories, the vendored rule and `rounds.superseded_fails`
   supersede exactly the same rounds — the hashes prove "unchanged", this proves "equal".
"""

from __future__ import annotations

import importlib.util
import itertools
import random
import sys
from pathlib import Path

from pr_reviewer.rounds import encode_disposition_record, panel_rounds, superseded_fails
from pr_reviewer.verdicts import parse_verdict_marker, render_verdict_body

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rah = _load("review_at_head_vendored", ROOT / "scripts" / "review_at_head.py")
vendor = _load("vendor_supersede_rule", ROOT / "scripts" / "vendor_supersede_rule.py")
SCRIPT = (ROOT / "scripts" / "review_at_head.py").read_text()

HEAD = "a" * 40


def test_the_vendored_block_still_matches_rounds_py():
    assert vendor.recorded(SCRIPT)["rounds"] == vendor.rounds_hash(), (
        "rounds.py's supersede rule changed: re-vendor it into scripts/review_at_head.py, run "
        "`python3 scripts/vendor_supersede_rule.py --stamp <commit>`, and re-sync the copies in "
        "protoAgent, protoPatch and qaEngineer (`--sync <their scripts/review_at_head.py>`)"
    )


def test_the_vendored_block_has_not_been_edited_in_place():
    assert vendor.recorded(SCRIPT)["block"] == vendor.block_hash(SCRIPT)


def test_the_hash_ignores_formatting_comments_and_docstrings_but_not_code():
    block = vendor.block(SCRIPT)
    reflowed = block.replace("    if not fail_id", "    # a comment a formatter might keep\n    if not fail_id", 1)
    reflowed = reflowed.replace("Fails CLOSED.", "Fails CLOSED, reworded.")
    reflowed = reflowed.replace(
        '_V_BLOCKING = ("blocker", "major")', '_V_BLOCKING = (\n    "blocker",\n    "major",\n)'
    )
    assert vendor.block_hash(SCRIPT.replace(block, reflowed)) == vendor.block_hash(SCRIPT)
    edited = block.replace('r.get("d") == "refuted"', 'r.get("d") in ("refuted", "fixed")')
    assert edited != block and vendor.block_hash(SCRIPT.replace(block, edited)) != vendor.block_hash(SCRIPT)


# ── behavioural equivalence on a corpus ─────────────────────────────────────────

FILES = ["a.py", "./a.py", "b.py"]
MAJOR = {"file": "a.py", "line": 3, "severity": "major", "claim": "c", "evidence": "e", "verdict": "confirmed"}


def _findings(rng: random.Random) -> list[dict]:
    out = []
    for _ in range(rng.randint(0, 3)):
        out.append(
            {
                "file": rng.choice(FILES),
                "line": rng.choice([3, 4, 0, None]),
                "severity": rng.choice(["major", "blocker", "minor", "MAJOR"]),
                "claim": "c",
                "evidence": "e",
                "verdict": rng.choice(["confirmed", "refuted", "uncertain", ""]),
                **({"nearby": True} if rng.random() < 0.1 else {}),
                **({"ungrounded": True} if rng.random() < 0.1 else {}),
                **({"carried": True} if rng.random() < 0.2 else {}),
            }
        )
    return out


def _record(rng: random.Random, ids: list[int]) -> dict | None:
    if rng.random() < 0.2:
        return None
    rows = [
        {
            "a": rng.choice(["a.py:3", "a.py:4", "b.py:3", "a.py", "a.py:0"]),
            "d": rng.choice(["refuted", "refuted", "fixed", "open", "", "conflict"]),
            "e": rng.random() < 0.8,
            "h": rng.random() < 0.8,
        }
        for _ in range(rng.randint(0, 3))
    ]
    return {"of": rng.choice(ids), "rows": rows}


def _review(rng: random.Random, review_id: int, ids: list[int]) -> dict:
    verdict = rng.choice(["FAIL", "FAIL", "PASS", "WARN"])
    findings = [MAJOR] if verdict == "FAIL" and rng.random() < 0.5 else _findings(rng)
    body = render_verdict_body(
        repo="o/r",
        pr=1,
        head_sha=HEAD,
        verdict=verdict,
        findings=findings,
        shadow=False,
        recipe="code-review",
        brief="prose",
        complete=rng.random() < 0.85,
        verified=rng.random() < 0.85,
        reaffirmed_from=("b" * 40) if rng.random() < 0.05 else "",
        disposition_token=encode_disposition_record(_record(rng, ids)),
    )
    if rng.random() < 0.05:
        body = body.replace("```json", "```jsn", 1)  # an unreadable findings record
    return {"id": review_id, "body": body}


def _plugin(reviews):
    rounds = [
        r for rev in reviews for r in panel_rounds([{**parse_verdict_marker(rev["body"]), **rev, "body": rev["body"]}])
    ]
    gone = {id(f) for f, _ in superseded_fails(rounds)}
    return [r["id"] for r in rounds if id(r) in gone]


def _vendored(reviews):
    rounds = [rah._v_panel_round(rah.parse_marker(rev["body"]), rev["body"], rev["id"]) for rev in reviews]
    gone = {id(f) for f, _ in rah._v_superseded_fails(rounds)}
    return [r["id"] for r in rounds if id(r) in gone]


def test_the_vendored_rule_supersedes_exactly_what_rounds_py_does():
    rng = random.Random(234)
    fired = 0
    for _case in range(3000):
        n = rng.randint(1, 4)
        ids = sorted(rng.sample(range(100, 110), n))
        if rng.random() < 0.1:
            ids[rng.randrange(n)] = 0  # an unreadable id
        reviews = [_review(rng, i, ids + [101, 102]) for i in ids]
        if rng.random() < 0.5:
            reviews = _near_miss(rng)
        expected = _plugin(reviews)
        assert _vendored(reviews) == expected, reviews
        fired += bool(expected)
    assert fired > 50  # the corpus really exercises supersession, not just the refusals


def test_the_canonical_cases_agree():
    fail = {"id": 102, "body": _fixed_body("FAIL", [MAJOR], None)}
    ok = {"a": "a.py:3", "d": "refuted", "e": True, "h": True}
    for d, e, h, complete, verified in itertools.product(
        ["refuted", "fixed", "open"], [True, False], [True, False], [True, False], [True, False]
    ):
        newer = {
            "id": 103,
            "body": _fixed_body(
                "PASS", [], {"of": 102, "rows": [{**ok, "d": d, "e": e, "h": h}]}, complete=complete, verified=verified
            ),
        }
        assert _vendored([fail, newer]) == _plugin([fail, newer])
    good = {"id": 103, "body": _fixed_body("PASS", [], {"of": 102, "rows": [ok]})}
    assert _vendored([fail, good]) == _plugin([fail, good]) == [102]


def _near_miss(rng: random.Random) -> list[dict]:
    """A superseding history, then (usually) ONE fact perturbed — the edges, densely."""
    ok = {"a": "a.py:3", "d": "refuted", "e": True, "h": True}
    fail_findings = [MAJOR] + ([{**MAJOR, "line": 4}] if rng.random() < 0.3 else [])
    rows = [ok] + ([{**ok, "a": "a.py:4"}] if rng.random() < 0.7 else [])
    newer = dict(verdict=rng.choice(["PASS", "WARN", "FAIL"]), findings=[], record={"of": 102, "rows": rows})
    knob = rng.choice(["none", "none", "of", "row", "complete", "verified", "still", "order", "rows", "racer"])
    if knob == "of":
        newer["record"]["of"] = 101
    elif knob == "row":
        rows[0] = {**ok, rng.choice("deh"): rng.choice(["fixed", False, ""])}
    elif knob == "still":
        newer["findings"] = [{**MAJOR, "carried": True}]
    elif knob == "rows":
        rows.append({**ok, "d": "open"})
    reviews = [
        {"id": 101, "body": _fixed_body("PASS", [], None)},
        {"id": 102, "body": _fixed_body("FAIL", fail_findings, None)},
        {
            "id": 103 if knob != "order" else 100,
            "body": _fixed_body(
                newer["verdict"],
                newer["findings"],
                newer["record"],
                complete=knob != "complete",
                verified=knob != "verified",
            ),
        },
    ]
    if knob == "racer":
        reviews.append({"id": 104, "body": _fixed_body("FAIL", [MAJOR], {"of": 101, "rows": [ok]})})
    return reviews


def _fixed_body(verdict, findings, record, *, complete=True, verified=True):
    return render_verdict_body(
        repo="o/r",
        pr=1,
        head_sha=HEAD,
        verdict=verdict,
        findings=findings,
        shadow=False,
        recipe="code-review",
        brief="prose",
        complete=complete,
        verified=verified,
        disposition_token=encode_disposition_record(record),
    )

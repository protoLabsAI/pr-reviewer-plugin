"""Per-repo memory of refuted structural claims (#190)."""

from __future__ import annotations

import json
import time

from pr_reviewer.refutations import RefutationStore, premark_refuted, same_claim

CLAIM = "builtin_world panics via .expect() on TOML parse failure"
REWORDED = (
    "builtin_world panics via .expect() on TOML parse failure; for a public crate a Result would let callers recover"
)


def _finding(**over):
    return {
        "file": "packs/necromunda/src/lib.rs",
        "line": 11,
        "severity": "minor",
        "claim": CLAIM,
        "source": "protopatch",
        **over,
    }


def test_record_keeps_only_refuted_protopatch_findings_and_match_finds_them(tmp_path):
    store = RefutationStore(tmp_path)
    n = store.record(
        "o/r",
        [
            _finding(verdict="refuted", note="the function returns Result and uses ?"),
            _finding(verdict="confirmed", claim="something real"),
            {**_finding(verdict="refuted"), "source": ""},  # an LLM finding: stochastic, never remembered
        ],
        pr=384,
        head="5a30cd965035",
    )
    assert n == 1
    hit = store.match("o/r", "packs/necromunda/src/lib.rs", REWORDED)  # near-identical wording still matches
    assert hit and hit["pr"] == 384 and hit["head"] == "5a30cd965035" and "returns Result" in hit["note"]
    assert store.match("o/r", "packs/necromunda/src/other.rs", CLAIM) is None
    assert store.match("o/other", "packs/necromunda/src/lib.rs", CLAIM) is None


def test_entries_age_out_and_an_unreadable_store_matches_nothing(tmp_path):
    store = RefutationStore(tmp_path, ttl_days=1)
    store.record("o/r", [_finding(verdict="refuted")], pr=1, head="abc")
    path = tmp_path / "o-r" / "refuted.json"
    data = json.loads(path.read_text())
    data[0]["at"] = time.time() - 3 * 86400
    path.write_text(json.dumps(data))
    assert store.match("o/r", "packs/necromunda/src/lib.rs", CLAIM) is None
    path.write_text("{not json")
    assert store.match("o/r", "packs/necromunda/src/lib.rs", CLAIM) is None
    assert store.record("o/r", [_finding(verdict="refuted")], pr=2, head="def") == 1  # rewrites cleanly


def test_premark_marks_a_repeat_the_pr_does_not_touch_and_leaves_a_touched_one_live(tmp_path):
    store = RefutationStore(tmp_path)
    store.record("o/r", [_finding(verdict="refuted", note="returns Result")], pr=384, head="5a30cd96")
    untouched = [_finding()]
    assert premark_refuted(untouched, store, "o/r", {"packs/necromunda/src/lib.rs": [(60, 61)]}) == 1
    assert untouched[0]["verdict"] == "refuted" and untouched[0]["refuted_before"].startswith("#384 @5a30cd96")
    assert "returns Result" in untouched[0]["note"]
    touched = [_finding()]
    assert (
        premark_refuted(touched, store, "o/r", {"packs/necromunda/src/lib.rs": [(9, 14)]}) == 0
    )  # the PR edits that spot
    assert "verdict" not in touched[0]
    unknown = [_finding()]
    assert premark_refuted(unknown, store, "o/r", None) == 0  # diff unreadable ⇒ fail open, report it


def test_same_claim_is_normalised_and_fails_closed_on_empty():
    assert same_claim("  Builtin_World  panics ", "builtin_world panics")
    assert not same_claim("", CLAIM) and not same_claim(CLAIM, "")


def test_a_different_site_with_boilerplate_wording_is_never_pre_marked(tmp_path):
    store = RefutationStore(tmp_path)
    boiler = "SQL built by string concatenation from a request field in "
    store.record(
        "o/r",
        [{"file": "db.py", "line": 40, "claim": boiler + "list_users()", "source": "protopatch", "verdict": "refuted"}],
        pr=1,
        head="abc",
    )
    far = [{"file": "db.py", "line": 400, "claim": boiler + "delete_user()", "source": "protopatch"}]
    assert premark_refuted(far, store, "o/r", {}) == 0 and "verdict" not in far[0]
    near_same = [
        {"file": "db.py", "line": 43, "claim": boiler + "list_users() — id not parameterised", "source": "protopatch"}
    ]
    assert premark_refuted(near_same, store, "o/r", {}) == 1

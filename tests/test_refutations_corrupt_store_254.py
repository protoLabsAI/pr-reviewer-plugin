"""#254 item 1: a corrupt refutation store degrades, it never raises; writes are atomic.

The stores only ever write `time.time()` as `at`, so a non-numeric one is a corrupt or
hand-edited file. Every reader used `float(e.get("at") or 0)` outside `_load`'s try, so one
bad entry raised `ValueError`/`TypeError` out of the structural pre-mark, which runs inside
the protopatch pass and must degrade, never raise.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from pr_reviewer import refutations as rf
from pr_reviewer.refutations import LlmRefutationStore, RefutationStore, premark_refuted, render_refuted_before

CLAIM = "builtin_world panics via .expect() on TOML parse failure"
FILE = "packs/necromunda/src/lib.rs"
CORRUPT_AT = ["yesterday", [1], {"t": 1}, "nan", "inf", 1e300, True]


@pytest.mark.parametrize("at", CORRUPT_AT, ids=[repr(a) for a in CORRUPT_AT])
def test_a_structural_entry_with_a_corrupt_at_ages_out_instead_of_raising(tmp_path, at):
    store = RefutationStore(tmp_path)
    path = store._path("o/r")
    path.parent.mkdir(parents=True)
    good = {"file": FILE, "line": 11, "claim": CLAIM, "pr": 1, "head": "abc", "at": time.time()}
    path.write_text(json.dumps([{**good, "at": at}, {**good, "file": "other.rs"}]))
    assert [e["file"] for e in store._load("o/r")] == ["other.rs"]  # the corrupt one aged out
    finding = {"file": FILE, "line": 11, "claim": CLAIM, "source": "protopatch", "severity": "minor"}
    assert premark_refuted([finding], store, "o/r", {}) == 0  # and pre-marks nothing


@pytest.mark.parametrize("at", CORRUPT_AT, ids=[repr(a) for a in CORRUPT_AT])
def test_an_llm_entry_with_a_corrupt_at_ages_out_instead_of_raising(tmp_path, at):
    store = LlmRefutationStore(tmp_path)
    path = store._path("o/r")
    path.parent.mkdir(parents=True)
    entry = {"repo": "o/r", "lane": "correctness", "file": FILE, "line": 11, "claim": CLAIM, "at": at}
    path.write_text(json.dumps({"repo": "o/r", "entries": [entry], "dismissals": []}))
    assert store.entries("o/r") == []


def test_dates_and_numbers_in_rendered_notes_never_raise():
    for at in [*CORRUPT_AT, None, 0]:
        assert rf._day({"at": at}) in ("1970-01-01", "unknown date")
    block = render_refuted_before([{"file": FILE, "line": "eleven", "pr": "x", "head": "abc", "claim": CLAIM}])
    assert f'location="{FILE}:0"' in block and 'refuted_on="#0 @abc 1970-01-01"' in block
    assert rf._day({"at": 86400 * 2}) == "1970-01-03"


@pytest.mark.parametrize("store_kind", ["structural", "llm"])
def test_both_stores_write_through_a_temp_file_and_rename(tmp_path, monkeypatch, store_kind):
    replaced = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(rf.os, "replace", spy)
    finding = {"file": FILE, "line": 11, "claim": CLAIM, "verdict": "refuted", "note": "returns Result"}
    if store_kind == "structural":
        store = RefutationStore(tmp_path)
        assert store.record("o/r", [{**finding, "source": "protopatch"}], pr=1, head="abc") == 1
        path = store._path("o/r")
    else:
        store = LlmRefutationStore(tmp_path)
        assert store.observe("o/r", [{**finding, "category": "correctness", "severity": "minor"}], pr=1, head="abc")[0]
        path = store._path("o/r")
    assert len(replaced) == 1 and replaced[0][1] == str(path) and replaced[0][0] != str(path)
    assert json.loads(path.read_text())  # the store is whole
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # no temp file left behind


def test_a_failed_write_leaves_the_old_store_and_no_temp_file(tmp_path, monkeypatch):
    store = RefutationStore(tmp_path)
    finding = {"file": FILE, "line": 11, "claim": CLAIM, "verdict": "refuted", "source": "protopatch"}
    assert store.record("o/r", [finding], pr=1, head="abc") == 1
    path = store._path("o/r")
    before = path.read_text()

    def disk_full(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rf.os, "replace", disk_full)
    assert store.record("o/r", [{**finding, "file": "b.rs"}], pr=2, head="def") == 0  # never raises
    assert path.read_text() == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]

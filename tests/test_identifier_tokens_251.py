"""One identifier tokenizer for the refutation stores and the verdict readers (#251).

`refutations.py` and `verdicts.py` each carried a copy of `identifier_tokens`, and the copies
had already drifted (".,;:" vs ".,;:()"), so the same pair of claims could be "the same
defect" to the carried-prior reader and a different one to the refutation store — or the
reverse. And `same_claim` tokenized the LOWER-CASED claims, so camelCase identifiers vanished
and two sibling claims differing only in one (`saveUser` / `loadUser`) matched.
"""

from __future__ import annotations

import pytest
from pr_reviewer import refutations, verdicts
from pr_reviewer.refutations import LlmRefutationStore, RefutationStore, same_claim
from pr_reviewer.verdicts import _same_defect, identifier_tokens


def test_there_is_one_tokenizer():
    assert refutations.identifier_tokens is verdicts.identifier_tokens
    assert not hasattr(refutations, "_IDENTIFIER_TOKEN")


@pytest.mark.parametrize(
    ("claim", "tokens"),
    [
        ("list_users() builds SQL by concatenation", {"list_users"}),
        ("foo_bar is never closed (see foo_bar)", {"foo_bar"}),  # prose paren: not part of the name
        ("scripts/x.sh exits 0 on failure", {"scripts/x.sh", "0"}),
        ("items[0] can be empty", {"items[0]"}),  # interior brackets keep their shape
        ("saveUser drops the row", {"saveuser"}),  # camelCase, read before lower-casing
        ("the error handling here is wrong", set()),
    ],
)
def test_tokens(claim, tokens):
    assert identifier_tokens(claim) == frozenset(tokens)


PAIRS = [
    # (a, b, same) — the readers must agree on every one.
    ("foo_bar is never closed (see foo_bar)", "foo_bar is never closed, see foo_bar", True),  # diverged before
    ("list_users() builds SQL by concatenation", "list_users builds SQL by concatenation", True),
    ("list_users() builds SQL by concatenation", "delete_user() builds SQL by concatenation", False),
    ("saveUser drops the row on retry", "loadUser drops the row on retry", False),  # diverged before
    (
        "builtin_world panics via .expect() on TOML parse failure",
        "Builtin_world panics via .expect() on a TOML parse failure",
        True,
    ),
]


@pytest.mark.parametrize(("a", "b", "same"), PAIRS)
def test_the_stores_and_the_verdict_readers_agree(tmp_path, a, b, same):
    row = {"file": "src/x.py", "line": 10, "severity": "major", "category": "correctness"}
    # The verdict reader (carried-prior dedup).
    assert _same_defect({**row, "claim": a}, {**row, "claim": b}) is same
    # The structural store (#190).
    assert same_claim(a, b) is same
    structural = RefutationStore(tmp_path / "s")
    structural.record("o/r", [{**row, "claim": a, "source": "protopatch", "verdict": "refuted"}], pr=1, head="h")
    assert (structural.match("o/r", "src/x.py", b, 10) is not None) is same
    # The LLM-lane store (#207) — skipped where a claim names nothing (it is never stored).
    if identifier_tokens(a):
        llm = LlmRefutationStore(tmp_path / "l")
        llm.observe("o/r", [{**row, "claim": a, "verdict": "refuted"}], pr=1, head="h")
        assert (llm.match("o/r", {**row, "claim": b}) is not None) is same

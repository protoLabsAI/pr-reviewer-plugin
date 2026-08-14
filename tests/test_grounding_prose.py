"""Connective prose must not be extracted as checkable evidence (issue #56).

`_PROSE_RE` catches narrative punctuation. A short clause without any of it still cleared
every filter — under the length/token caps, has whitespace, and a stray `(`/`:` reads as
a code hint — so the model's own narration got treated as a quote, could never be found,
and downgraded a finding whose real evidence was never checked.

The scope here is deliberately ONE signal (a leading `;`/`,` + lowercase word). A word-
count heuristic was tried and withdrawn: every formulation of it produced a FALSE DROP,
and a false drop is worse than the bug — `ground_finding` fail-opens when no quote
survives, so dropping a real quote opens the #25 hallucination bypass rather than merely
skipping a check. Those withdrawn cases are pinned below so they cannot creep back.
"""

from __future__ import annotations

import pytest
from pr_reviewer.grounding import ground_finding, quoted_snippets

# Verbatim from protoAgent#2631 round 2, where these downgraded a **blocker**.
CAUGHT_FROM_PRODUCTION = [
    "; diff (scheduler, same pattern at ~new line 1692):",
    "; and the removed order-independent read:",
]


@pytest.mark.parametrize("prose", CAUGHT_FROM_PRODUCTION)
def test_connective_prose_is_not_extracted_as_a_quote(prose):
    assert quoted_snippets({"claim": f"the bug: `{prose}` as shown", "evidence": ""}) == []


def test_prose_only_evidence_fails_open_rather_than_downgrading():
    """With nothing checkable left, `ground_finding` must fail OPEN — the finding may be
    perfectly real, and its actual evidence was simply never quoted. Downgrading here is
    what produced the #2631 blocker regression."""
    finding = {"claim": "broken `; and the removed order-independent read:` here", "evidence": ""}
    grounded, missing = ground_finding(finding, source="irrelevant source text")
    assert grounded is True
    assert missing == []


def test_a_real_quote_alongside_prose_is_still_checked():
    """Prose is dropped, not contagious: the code quote next to it must still ground."""
    finding = {
        "claim": "`; and the removed read:` and `await asyncio.to_thread(supervisor.down, names)`",
        "evidence": "",
    }
    quotes = quoted_snippets(finding)
    assert len(quotes) == 1 and "asyncio.to_thread" in quotes[0]
    grounded, _ = ground_finding(finding, source="x = await asyncio.to_thread(supervisor.down, names)")
    assert grounded is True


def test_a_fabricated_quote_alongside_prose_still_downgrades():
    """The filter must not become an escape hatch — dropping prose cannot rescue a
    finding whose code quote is invented."""
    finding = {"claim": "`; and the removed read:` and `zzz = never_in_the_file(1, 2)`", "evidence": ""}
    grounded, missing = ground_finding(finding, source="something else entirely")
    assert grounded is False
    assert missing and "never_in_the_file" in missing[0]


# ── Regression pins: the false drops that killed the word-count rule ──────────────
#
# All three shapes below were dropped by one formulation or another of an English-
# function-word count (found by the panel on PR #59). Each is genuine code, and dropping
# genuine code is what opens the #25 bypass — so each stays extractable, and each stays
# groundable when fabricated.

FALSE_DROP_REGRESSIONS = [
    # `this`/`that` are English function words AND JS/TS keywords.
    "this.obj[this.key] = this.val",
    "this.a && this.b || this.c",
    "if (this.state) { this.emit(this.value) }",
    # Error messages and log lines are English by design.
    'raise ValueError("the value at this index is already removed")',
    'log.warning("could not find the file at this path, it is already gone")',
    'return {"error": "the request was already handled by this worker"}',
]


@pytest.mark.parametrize("code", FALSE_DROP_REGRESSIONS)
def test_genuine_code_is_never_dropped(code):
    assert quoted_snippets({"claim": f"see `{code}`", "evidence": ""}), f"dropped real code: {code}"


@pytest.mark.parametrize("code", FALSE_DROP_REGRESSIONS)
def test_a_fabricated_quote_of_that_shape_still_downgrades(code):
    """The consequence of a false drop, stated directly: if the quote is dropped, this
    finding grounds instead of downgrading, and a hallucinated blocker keeps its teeth."""
    grounded, missing = ground_finding({"claim": f"bug: `{code}`", "evidence": ""}, source="unrelated contents")
    assert grounded is False, "fabricated quote failed OPEN — the #25 guard is bypassed"
    assert missing


def test_the_third_production_case_is_knowingly_not_caught():
    """`(untouched by this PR) still does` has no leading-connective marker, so it is
    still extracted and will downgrade its finding — the pre-existing behaviour.

    This is a deliberate limit, not an oversight: every rule that caught it also dropped
    genuine code. Documented so a future change that "fixes" this is forced to show it
    does not reopen the bypass pinned above.
    """
    assert quoted_snippets({"claim": "`(untouched by this PR) still does`", "evidence": ""}) != []

"""Connective prose must not be extracted as checkable evidence (issue #56).

`_PROSE_RE` catches narrative punctuation. A short clause without any of it still cleared
every filter — under the length/token caps, has whitespace, and a stray `(`/`:` reads as
a code hint — so the model's own narration got treated as a quote, could never be found,
and downgraded a finding whose real evidence was never checked.

The regression direction matters as much as the catch: over-filtering silently stops
grounding real quotes, which is worse than the bug.
"""

from __future__ import annotations

import pytest
from pr_reviewer.grounding import ground_finding, quoted_snippets

# Verbatim from protoAgent#2631 round 2, where these downgraded a **blocker**.
PROSE_FROM_PRODUCTION = [
    "; diff (scheduler, same pattern at ~new line 1692):",
    "; and the removed order-independent read:",
    "(untouched by this PR) still does",
]

# Real failed quotes from the recorded corpus — code across several languages. These
# must stay checkable; if the filter eats them, grounding quietly stops working.
REAL_CODE = [
    "next((l[len(LABEL_SOURCE_PREFIX):] for l in labels if l.startswith(LABEL_SOURCE_PREFIX)), '')",
    'if not isinstance(messages, list): return {"error": "messages must be an array"}, 400',
    "files = [base] if base.is_file() else [p for p in base.rglob('*') if p.is_file()]",
    'let id = table.get("id").and_then(toml::Value::as_str).map(str::trim).unwrap_or_default();',
    "await asyncio.to_thread(supervisor.down, names)",
    '<Td className="plugin-cell-name">',
    ".pl-drawer--top, .pl-drawer--bottom { max-height: 85vh; max-height: 85dvh; }",
    "dry_run: bool = Body(False, embed=True)",
    "assert memory_path() == \"/custom/mem\" # env override verbatim",
    "impl fmt::Debug for FamilyDef",
]


@pytest.mark.parametrize("prose", PROSE_FROM_PRODUCTION)
def test_connective_prose_is_not_extracted_as_a_quote(prose):
    assert quoted_snippets({"claim": f"the bug: `{prose}` as shown", "evidence": ""}) == []


@pytest.mark.parametrize("code", REAL_CODE)
def test_real_code_quotes_still_extract(code):
    assert quoted_snippets({"claim": f"see `{code}`", "evidence": ""}), f"filter ate real code: {code}"


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
        "claim": "`(untouched by this PR) still does` and `await asyncio.to_thread(supervisor.down, names)`",
        "evidence": "",
    }
    quotes = quoted_snippets(finding)
    assert len(quotes) == 1 and "asyncio.to_thread" in quotes[0]
    grounded, _ = ground_finding(finding, source="x = await asyncio.to_thread(supervisor.down, names)")
    assert grounded is True


def test_a_fabricated_quote_alongside_prose_still_downgrades():
    """The filter must not become an escape hatch — dropping prose cannot rescue a
    finding whose code quote is invented."""
    finding = {"claim": "`(untouched by this PR) still does` and `zzz = never_in_the_file(1, 2)`", "evidence": ""}
    grounded, missing = ground_finding(finding, source="something else entirely")
    assert grounded is False
    assert missing and "never_in_the_file" in missing[0]

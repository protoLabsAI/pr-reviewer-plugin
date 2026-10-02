"""`three_way_rows` reads a PR's reviews with `gh api --paginate`; a busy PR has more than
one page, and an array-wrapping jq filter emitted `[…][…]` — one array per page — which
failed to parse, so the comparison silently lost every review (the #75 shape)."""

from __future__ import annotations

import json

from pr_reviewer.eval import three_way_rows

PAGE_1 = [{"login": "coderabbitai[bot]", "state": "COMMENTED"}] * 30
PAGE_2 = [{"login": "protoquinn", "state": "APPROVED"}, {"login": "coderabbitai[bot]", "state": "COMMENTED"}]


class TwoPageGH:
    """Serves two pages the way `gh --paginate` does: the jq filter applied per page and
    the outputs concatenated."""

    def __init__(self):
        self.jq = ""

    async def __call__(self, args, timeout=30):
        self.jq = args[args.index("--jq") + 1]
        if self.jq.startswith("["):  # an array-wrapping filter: one array per page
            return 0, json.dumps(PAGE_1) + json.dumps(PAGE_2), ""
        return 0, "\n".join(json.dumps(r) for r in PAGE_1 + PAGE_2), ""


async def test_a_second_page_of_reviews_is_read():
    gh = TwoPageGH()
    events = [{"event": "reviewed", "posted": True, "repo": "o/r", "pr": 7, "verdict": "PASS"}]
    [row] = await three_way_rows(events, gh)
    assert not gh.jq.startswith("[")
    assert row["quinn"] == "APPROVED"  # only on page two
    assert row["coderabbit_reviews"] == 31


async def test_an_unreadable_page_reads_as_no_reviews_not_a_crash():
    async def broken(args, timeout=30):
        return 0, '{"login": "protoquinn", "state": "APPROVED"}\nnot json', ""

    events = [{"event": "reviewed", "posted": True, "repo": "o/r", "pr": 7, "verdict": "PASS"}]
    [row] = await three_way_rows(events, broken)
    assert row["quinn"] == "" and row["coderabbit_reviews"] == 0

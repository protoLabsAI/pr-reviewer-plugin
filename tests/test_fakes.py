"""The gh fakes themselves (#136): a guard on the harness under every dispatch test, so it
gets the test a guard needs — one that feeds it the thing it exists to catch."""

from __future__ import annotations

from tests import conftest
from tests.test_dispatch import FakeGH, RoutedGH, facts


async def test_a_write_on_an_unrecognised_route_is_recorded_and_refused():
    for gh in (FakeGH(), RoutedGH(pr_facts=facts())):
        rc, _out, err = await gh(["api", "repos/o/r/pulls/1/merge", "-X", "PUT"])
        assert rc == 1 and "unexpected write" in err
        rc, _out, _err = await gh(["api", "repos/o/r/issues/comments/9", "-X", "DELETE"])
        assert rc == 1
    assert [a[1] for a in conftest.UNEXPECTED_WRITES] == ["repos/o/r/pulls/1/merge", "repos/o/r/issues/comments/9"] * 2
    # Consumed here on purpose: left in place, the autouse fixture fails this test — which
    # is the behaviour being proven, just not one a passing suite can demonstrate directly.
    conftest.UNEXPECTED_WRITES.clear()


async def test_the_writes_the_dispatcher_really_makes_and_every_read_stay_accepted():
    gh = RoutedGH(pr_facts=facts())
    for args in (
        ["api", "repos/o/r/pulls/1/reviews", "-X", "POST", "-f", "body=x"],
        ["api", "repos/o/r/pulls/1/reviews/7/dismissals", "-X", "PUT"],
        ["api", "repos/o/r/issues/1/comments", "-X", "POST", "-f", "body=x"],
        ["api", "repos/o/r/check-runs", "-X", "POST", "-f", "name=QA panel"],
        ["api", "repos/o/r/check-runs/5", "-X", "PATCH"],
        ["api", "repos/o/r/some/route/nobody/faked"],  # an unmatched READ stays permissive
    ):
        await gh(args)
    assert conftest.UNEXPECTED_WRITES == []

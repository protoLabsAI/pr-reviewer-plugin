"""The chokepoint — HMAC ingress + dedup/cooldown/in-flight, all drops typed."""

from __future__ import annotations

import hashlib
import hmac as hmac_mod

from pr_reviewer.chokepoint import (
    DROP_COOLDOWN,
    DROP_IN_FLIGHT,
    Chokepoint,
    verify_signature,
)

SECRET = "hunter2"
BODY = b'{"action": "opened"}'


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_roundtrip_and_rejections():
    good = _sign(SECRET, BODY)
    assert verify_signature(SECRET, BODY, good)
    assert not verify_signature(SECRET, BODY, _sign("wrong", BODY))
    assert not verify_signature(SECRET, b"tampered", good)
    assert not verify_signature(SECRET, BODY, None)
    assert not verify_signature(SECRET, BODY, good.removeprefix("sha256="))  # missing scheme
    assert not verify_signature("", BODY, good)  # no secret configured ⇒ fail closed


def make_clock(start=1000.0):
    state = {"t": start}

    def now():
        return state["t"]

    return state, now


def test_burst_collapses_to_one_dispatch():
    clock, now = make_clock()
    cp = Chokepoint(cooldown_s=30, now=now)
    assert cp.admit("o/r", 5, "a" * 40) == "accept"
    assert cp.admit("o/r", 5, "a" * 40) == DROP_IN_FLIGHT  # same PR still running
    cp.done("o/r", 5)
    assert cp.admit("o/r", 5, "a" * 40) == DROP_COOLDOWN  # same head inside the window
    clock["t"] += 31
    assert cp.admit("o/r", 5, "a" * 40) == "accept"  # window passed


def test_new_head_reenters_after_done_without_cooldown():
    clock, now = make_clock()
    cp = Chokepoint(cooldown_s=30, now=now)
    assert cp.admit("o/r", 5, "a" * 40) == "accept"
    cp.done("o/r", 5)
    # A new push (different sha) is a different cooldown key.
    assert cp.admit("o/r", 5, "b" * 40) == "accept"


def test_in_flight_is_per_pr_not_per_repo():
    _clock, now = make_clock()
    cp = Chokepoint(cooldown_s=30, now=now)
    assert cp.admit("o/r", 1, "a" * 40) == "accept"
    assert cp.admit("o/r", 2, "c" * 40) == "accept"  # sibling PR unaffected
    assert cp.admit("o/other", 1, "d" * 40) == "accept"  # sibling repo unaffected


def test_done_is_idempotent_and_safe_when_never_admitted():
    _clock, now = make_clock()
    cp = Chokepoint(now=now)
    cp.done("o/r", 99)  # no-op, no raise

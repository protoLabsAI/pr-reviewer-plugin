"""The dispatch chokepoint — every review request passes one gate (ADR 0078 C).

Quinn's issues #437/#444/#459/#465 all trace to reviews dispatched twice or while a
prior run was in flight; the cure was ONE chokepoint with typed drops, not smarter
callers. Ported:

  - HMAC ingress check (GitHub `X-Hub-Signature-256`, constant-time compare).
  - Per-`repo#pr@sha` cooldown (default 30s) — a webhook burst (synchronize +
    labeled + review_requested for one push) collapses to one dispatch.
  - An in-flight map — a second request for the same PR while a panel is running
    is dropped, not queued (the running review will post on the same head; a NEW
    head clears the entry on completion and re-enters normally). An entry older
    than ``in_flight_ttl_s`` is treated as ABANDONED and reclaimed: the slot is
    freed only by ``done()``, so a round that hangs — or can never finish
    cancelling — would otherwise lock that PR out of every review path (push,
    backfill sweep AND the operator's ``@vera review``, which all pass this gate)
    until the process restarts.

Every decision returns a typed verdict (`accept` or `drop:<reason>`) so telemetry
records WHY, never a silent skip. Pure/in-memory — restart forgets cooldowns, which
fails OPEN into one extra review, never a lost one.
"""

from __future__ import annotations

import hashlib
import hmac
import time

DROP_BAD_SIGNATURE = "bad-signature"
DROP_UNLISTED_REPO = "unlisted-repo"
DROP_NOT_A_PR_EVENT = "not-a-pr-event"
DROP_COOLDOWN = "cooldown"
DROP_IN_FLIGHT = "in-flight"

#: How long an in-flight slot may be held before ``admit`` reclaims it. Deliberately
#: generous — above the dispatcher's own round bound, so a slow-but-live round is never
#: raced by a second panel on the same PR; this is the backstop for the round that
#: never ends at all.
DEFAULT_IN_FLIGHT_TTL_S = 2 * 3600

# PR webhook actions that mean "the code under review may have changed / review is wanted".
DISPATCH_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """GitHub `X-Hub-Signature-256` check, constant-time. No secret configured → False
    (an unauthenticated webhook surface fails closed, never open)."""
    if not secret or not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={expected}", signature_header)


class Chokepoint:
    def __init__(
        self,
        cooldown_s: int = 30,
        *,
        in_flight_ttl_s: float = DEFAULT_IN_FLIGHT_TTL_S,
        on_reclaim=None,
        now=time.monotonic,
    ):
        self.cooldown_s = cooldown_s
        self.in_flight_ttl_s = in_flight_ttl_s
        # Called as on_reclaim(repo, pr, held_s) when an abandoned slot is reclaimed —
        # a hung round is a defect worth seeing, not just a lock worth breaking.
        self._on_reclaim = on_reclaim
        self._now = now
        self._last: dict[str, float] = {}  # key -> last accept time
        self._in_flight: dict[str, float] = {}  # repo#pr -> when its slot was taken

    @staticmethod
    def _key(repo: str, pr: int, sha: str) -> str:
        return f"{repo}#{pr}@{sha}"

    def admit(self, repo: str, pr: int, sha: str, *, bypass_cooldown: bool = False) -> str:
        """'accept' or a typed drop reason. An accept marks the PR in-flight —
        the caller MUST call `done()` when the review run finishes (however it ends).

        `bypass_cooldown` is for an operator summon (issue #28). The cooldown exists to
        eat webhook bursts — a synchronize storm, a redelivery — and a human who typed a
        command is neither. The IN-FLIGHT guard still applies: it protects against two
        panels running on one PR, which a summon must not do either.
        """
        flight_key = f"{repo}#{pr}"
        now = self._now()
        taken = self._in_flight.get(flight_key)
        if taken is not None:
            held = now - taken
            if held < self.in_flight_ttl_s:
                return DROP_IN_FLIGHT
            # Abandoned: its round never called done(). Reclaim rather than refuse
            # forever — the TTL sits above the dispatcher's own round bound, so this
            # only ever fires for a round that has stopped making progress.
            del self._in_flight[flight_key]
            if self._on_reclaim is not None:
                try:
                    self._on_reclaim(repo, pr, held)
                except Exception:  # noqa: BLE001 — observability must not block the gate
                    pass
        key = self._key(repo, pr, sha)
        last = self._last.get(key)
        if last is not None and not bypass_cooldown and now - last < self.cooldown_s:
            return DROP_COOLDOWN
        self._last[key] = now
        self._in_flight[flight_key] = now
        # Bounded memory: drop cooldown entries past 10× the window.
        if len(self._last) > 4096:
            cutoff = now - 10 * self.cooldown_s
            self._last = {k: t for k, t in self._last.items() if t >= cutoff}
        return "accept"

    def done(self, repo: str, pr: int) -> None:
        self._in_flight.pop(f"{repo}#{pr}", None)

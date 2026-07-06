"""The dispatch chokepoint — every review request passes one gate (ADR 0078 C).

Quinn's issues #437/#444/#459/#465 all trace to reviews dispatched twice or while a
prior run was in flight; the cure was ONE chokepoint with typed drops, not smarter
callers. Ported:

  - HMAC ingress check (GitHub `X-Hub-Signature-256`, constant-time compare).
  - Per-`repo#pr@sha` cooldown (default 30s) — a webhook burst (synchronize +
    labeled + review_requested for one push) collapses to one dispatch.
  - An in-flight map — a second request for the same PR while a panel is running
    is dropped, not queued (the running review will post on the same head; a NEW
    head clears the entry on completion and re-enters normally).

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
    def __init__(self, cooldown_s: int = 30, *, now=time.monotonic):
        self.cooldown_s = cooldown_s
        self._now = now
        self._last: dict[str, float] = {}  # key -> last accept time
        self._in_flight: set[str] = set()  # repo#pr currently under review

    @staticmethod
    def _key(repo: str, pr: int, sha: str) -> str:
        return f"{repo}#{pr}@{sha}"

    def admit(self, repo: str, pr: int, sha: str) -> str:
        """'accept' or a typed drop reason. An accept marks the PR in-flight —
        the caller MUST call `done()` when the review run finishes (however it ends)."""
        flight_key = f"{repo}#{pr}"
        if flight_key in self._in_flight:
            return DROP_IN_FLIGHT
        key = self._key(repo, pr, sha)
        now = self._now()
        last = self._last.get(key)
        if last is not None and now - last < self.cooldown_s:
            return DROP_COOLDOWN
        self._last[key] = now
        self._in_flight.add(flight_key)
        # Bounded memory: drop cooldown entries past 10× the window.
        if len(self._last) > 4096:
            cutoff = now - 10 * self.cooldown_s
            self._last = {k: t for k, t in self._last.items() if t >= cutoff}
        return "accept"

    def done(self, repo: str, pr: int) -> None:
        self._in_flight.discard(f"{repo}#{pr}")

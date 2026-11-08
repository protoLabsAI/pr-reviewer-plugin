"""Which model the correctness and removed-behavior lanes run on (issue #318).

The board's review gate runs protoAgent's same review-finder panel on a frontier model and
caught 16 real defects in a 24-finding sample. Vera, running the panel on the local lane,
raised only 1 of them in full. In the run records that survive, her finders never raised the
defect at all: the gap is the model, not verify or grounding. Those two lanes are where most
of the misses sit, so `pr_reviewer.deep_finder_model` moves them (the `review-finder-deep`
subagent) to another model while the other lanes stay local.

A frontier model reached through a subscription can be rate-limited or lose its token. A
deep lane that fails while the model is set trips a breaker: the lanes go back to the
default model for `deep_finder_cooldown_s`, so the panel's retry runs on the local lane
instead of failing the same way twice and exhausting the round. Every switch is a
`deep_lane` telemetry event, and the `reviewed` row records the model each round used.

Host imports stay lazy: the test suite runs with no host.
"""

from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)

DEEP_LANES = ("find_correctness", "find_removed_behavior")

# An error that says the MODEL could not be used, as opposed to the lane's own work failing.
# Both trip the breaker (falling back costs nothing but the better model), but the reason is
# recorded so the telemetry tells a lost token or rate limit from a lane bug.
_PROVIDER_ERROR = re.compile(
    r"OAuthCredentialError|rate.?limit|overloaded|authentication_error|permission_error|"
    r"usage limit|quota|Error code: (?:401|403|429|5\d\d)|\b(?:401|403|429|529)\b",
    re.IGNORECASE,
)


def _oauth_token_present() -> bool | None:
    """Whether a Claude OAuth token is readable on this host (None: the host can't say)."""
    try:
        from graph.providers.oauth import peek_anthropic_access_token
    except Exception:  # noqa: BLE001 — an older host: assume present and let the breaker judge
        return None
    try:
        return peek_anthropic_access_token() is not None
    except Exception:  # noqa: BLE001
        return False


class DeepLanes:
    """The deep lanes' model, plus the breaker that sends them back to the default model."""

    def __init__(self, telemetry=None, clock=time.monotonic, token_probe=_oauth_token_present):
        self.telemetry = telemetry
        self._clock = clock
        self._token_probe = token_probe
        self._open_until = 0.0
        self._open_reason = ""
        self._last = ""
        self._last_state: tuple[str, str] | None = None

    def _subagent(self):
        try:
            from graph.subagents.config import SUBAGENT_REGISTRY

            from .subagents import DEEP_FINDER_NAME

            return SUBAGENT_REGISTRY.get(DEEP_FINDER_NAME)
        except Exception:  # noqa: BLE001
            return None

    def _emit(self, **kw) -> None:
        if self.telemetry is not None:
            try:
                self.telemetry.emit("deep_lane", **kw)
            except Exception:  # noqa: BLE001 — telemetry never breaks a round
                pass

    def effective(self, configured: str) -> tuple[str, str]:
        """(model the deep lanes should use, why). "" means the default model."""
        configured = (configured or "").strip()
        if not configured:
            return "", "unset"
        if self._clock() < self._open_until:
            return "", f"breaker-open:{self._open_reason}"
        if configured.lower().startswith("anthropic-oauth:") and self._token_probe() is False:
            return "", "no-oauth-token"
        return configured, "configured"

    def apply(self, configured: str) -> str:
        """Point `review-finder-deep` at this round's model; returns the model ("" = default)."""
        model, why = self.effective(configured)
        sub = self._subagent()
        if sub is not None and getattr(sub, "model", None) != model:
            try:
                sub.model = model
            except Exception:  # noqa: BLE001 — a frozen host config: leave it, say so
                log.warning("[pr-reviewer] could not set the deep lanes' model to %r", model)
                why = "unsettable"
                model = getattr(sub, "model", "") or ""
        if (model, why) != self._last_state:
            self._emit(decision="model", model=model or "default", why=why)
        self._last_state = (model, why)
        self._last = model
        return model

    def trip(self, steps: dict | None, failed: list | None, cooldown_s: float) -> list[str]:
        """After an attempt: if a deep lane failed while it ran on the configured model, open the
        breaker so the retry runs on the default model. Returns the deep lanes that tripped it."""
        if not self._last:
            return []
        lanes = [s for s in (failed or []) if s in DEEP_LANES]
        if not lanes:
            return []
        text = " ".join(str((steps or {}).get(s) or "") for s in lanes)
        reason = "provider" if _PROVIDER_ERROR.search(text) else "lane-error"
        self._open_until = self._clock() + max(0.0, float(cooldown_s))
        self._open_reason = reason
        self._emit(
            decision="fallback",
            model=self._last,
            lanes=lanes,
            reason=reason,
            cooldown_s=cooldown_s,
            error=text[:300],
        )
        log.warning(
            "[pr-reviewer] deep lanes %s failed on %s (%s); default model for %ss",
            lanes,
            self._last,
            reason,
            cooldown_s,
        )
        return lanes

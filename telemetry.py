"""Append-only JSONL telemetry — the substrate the review eval reads (ADR 0078 C).

Every dispatch decision (accepted or a typed drop), review outcome, posted verdict,
and promotion decision lands here as one JSON line. The eval script is the consumer
IN THE SAME PHASE (the anti-lesson from Quinn's write-only retrieval flywheel: no
consumer, no writes).

One file per UTC day under `<root>/telemetry/`, so pruning is `rm` on old files and
a crashed writer can't corrupt more than a line. `emit()` prunes day-files older than
`retention_days` at most once per UTC day (issue #372); `read_all(since_days=N)` lets a
reader parse only a recent window instead of the whole history.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("protoagent.plugins.pr_reviewer")

# Reaffirm event names (issue #91): a verdict REUSED without re-spending the panel, plus
# the near-misses where a reuse was considered and declined. Named here rather than inlined
# as string literals in the dispatcher so the eval reads one vocabulary for "the panel did
# not run because the reviewed content had not changed." REAFFIRM_HEAD keeps the original
# "reaffirm" name so existing telemetry and dashboards are unbroken.
REAFFIRM_HEAD = "reaffirm"  # the exact same head SHA already carries this verdict
REAFFIRM_DIFF = "reaffirm-diff"  # head SHA changed, but the base↔head diff is byte-identical
REAFFIRM_MISS = "reaffirm-miss"  # a diff reaffirm was considered and declined (fail-closed)
REAFFIRM_RECORDED = "reaffirm-recorded"  # a diff-reaffirmed PASS/WARN was carried to the new head (#135)
VERIFY_CONTRADICTED = (
    "verify-contradicted"  # the verifier said nothing-to-verify over a synthesis that carried findings (#167)
)


DEFAULT_RETENTION_DAYS = 90

_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def _utc_day(ts: float) -> date:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date()


def _file_day(path: Path) -> date | None:
    """The UTC day a `YYYY-MM-DD.jsonl` file holds, or None for any other name."""
    m = _DAY_FILE.match(path.name)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def parse_retention_days(value, default: int = DEFAULT_RETENTION_DAYS) -> int:
    """Coerce a configured retention to an int; unset or unparseable ⇒ `default`.

    An explicit `0` (or a negative) is kept — it means "never prune".
    """
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, str) and not value.strip():
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Telemetry:
    def __init__(self, root: str | Path, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.dir = Path(root) / "telemetry"
        self.retention_days = retention_days
        self._last_prune_day: date | None = None

    def emit(self, event: str, **fields) -> None:
        """Best-effort append; telemetry must never break the review path."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            now = time.time()
            day = time.strftime("%Y-%m-%d", time.gmtime(now))
            line = json.dumps({"ts": round(now, 3), "event": event, **fields}, default=str)
            with open(self.dir / f"{day}.jsonl", "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:  # noqa: BLE001
            log.exception("[pr-reviewer] telemetry write failed (event=%s)", event)
            return
        self._maybe_prune(now)

    def _maybe_prune(self, now: float) -> None:
        """Prune at most once per UTC day (issue #372); never raises."""
        try:
            today = _utc_day(now)
            if self._last_prune_day == today:
                return
            self._last_prune_day = today
            self.prune(now)
        except Exception:  # noqa: BLE001
            log.warning("[pr-reviewer] telemetry prune scheduling failed", exc_info=True)

    def prune(self, now: float | None = None) -> int:
        """Delete day-files dated more than `retention_days` before today (UTC).

        The date comes from the filename, not the mtime. Today's file is never deleted,
        names that are not `YYYY-MM-DD.jsonl` are left alone, and `retention_days <= 0`
        disables pruning. Best-effort: logs a WARNING on error and returns the count
        removed — never raises (telemetry must never break the review path).
        """
        removed = 0
        try:
            days = int(self.retention_days)
            if days <= 0 or not self.dir.is_dir():
                return 0
            today = _utc_day(time.time() if now is None else now)
            cutoff = today - timedelta(days=days)
            for path in self.dir.glob("*.jsonl"):
                day = _file_day(path)
                if day is None or day >= today or day >= cutoff:
                    continue
                try:
                    path.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
                except OSError:
                    log.warning("[pr-reviewer] telemetry prune could not remove %s", path, exc_info=True)
        except Exception:  # noqa: BLE001
            log.warning("[pr-reviewer] telemetry prune failed", exc_info=True)
        if removed:
            log.info("[pr-reviewer] telemetry prune removed %d day-file(s)", removed)
        return removed

    def read_all(self, since_days: int | None = None) -> list[dict]:
        """Every event, oldest file first — the eval's input.

        `since_days=N` reads only the day-files dated within the last N days (by filename,
        UTC; today counts as day 0), skipping files whose name is not a date. The default
        `None` reads every file, as before.
        """
        out: list[dict] = []
        if not self.dir.is_dir():
            return out
        cutoff: date | None = None
        if since_days is not None:
            cutoff = _utc_day(time.time()) - timedelta(days=max(int(since_days), 0))
        for path in sorted(self.dir.glob("*.jsonl")):
            if cutoff is not None:
                day = _file_day(path)
                if day is None or day < cutoff:
                    continue
            for raw in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return out

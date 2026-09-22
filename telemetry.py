"""Append-only JSONL telemetry — the substrate the review eval reads (ADR 0078 C).

Every dispatch decision (accepted or a typed drop), review outcome, posted verdict,
and promotion decision lands here as one JSON line. The eval script is the consumer
IN THE SAME PHASE (the anti-lesson from Quinn's write-only retrieval flywheel: no
consumer, no writes).

One file per UTC day under `<root>/telemetry/`, so pruning is `rm` on old files and
a crashed writer can't corrupt more than a line.
"""

from __future__ import annotations

import json
import logging
import time
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


class Telemetry:
    def __init__(self, root: str | Path):
        self.dir = Path(root) / "telemetry"

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

    def read_all(self) -> list[dict]:
        """Every event, oldest file first — the eval's input."""
        out: list[dict] = []
        if not self.dir.is_dir():
            return out
        for path in sorted(self.dir.glob("*.jsonl")):
            for raw in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return out

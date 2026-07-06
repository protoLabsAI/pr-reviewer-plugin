"""The review eval — telemetry in, comparable numbers out (ADR 0078 C).

The consumer that justifies the telemetry writes (Quinn's write-only-flywheel
anti-lesson). Reads the plugin's JSONL events and answers, per Quinn's own eval
vocabulary (her metrics are the floor):

  - completion rate — dispatches that ended in a posted verdict (her 99.4%);
  - verdict mix — PASS/WARN/FAIL distribution (a reviewer that always PASSes or
    always FAILs is broken in different ways);
  - latency — p50/p90 seconds (her 44.7s median is the floor; the lite recipe is
    our lever);
  - drop mix — every typed drop by reason (silent skips would hide here if we had
    any; we don't);
  - per-finding resolution substrate — findings counts per review (ws-91a: "do
    findings reach the verdict" — resolution needs the follow-up review, which the
    delta re-review records as `delta: true`).

Three-way comparison (us vs Quinn's verdicts vs CodeRabbit threads on the same PRs)
needs GitHub reads — `three_way_rows()` builds the PR list from telemetry so the
caller (CLI/API) fetches only what's needed.
"""

from __future__ import annotations

import json
from collections import Counter


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    idx = min(int(round(q * (len(xs) - 1))), len(xs) - 1)
    return xs[idx]


def build_report(events: list[dict]) -> dict:
    """The numeric summary over raw telemetry events."""
    dispatches = [e for e in events if e.get("event") == "dispatch"]
    reviewed = [e for e in events if e.get("event") == "reviewed"]
    reaffirmed = [e for e in events if e.get("event") == "reaffirm"]
    drops = Counter(str(e.get("reason")) for e in events if e.get("event") == "drop")
    exhaustions = [e for e in events if e.get("event") == "exhaustion"]
    promotions = Counter(str(e.get("decision")) for e in events if e.get("event") == "promotion")
    latencies = [float(e["latency_s"]) for e in reviewed if e.get("latency_s") is not None]
    return {
        "dispatches": len(dispatches),
        "reviews_posted": sum(1 for e in reviewed if e.get("posted")),
        "completion_rate": (
            round(sum(1 for e in reviewed if e.get("posted")) / len(dispatches), 4) if dispatches else None
        ),
        "reaffirmed": len(reaffirmed),
        "verdict_mix": dict(Counter(str(e.get("verdict")) for e in reviewed)),
        "recipe_mix": dict(Counter(str(e.get("recipe")) for e in reviewed)),
        "latency_s": {"p50": _percentile(latencies, 0.5), "p90": _percentile(latencies, 0.9), "n": len(latencies)},
        "findings_per_review": (
            round(sum(int(e.get("findings") or 0) for e in reviewed) / len(reviewed), 2) if reviewed else None
        ),
        "delta_reviews": sum(1 for e in dispatches if e.get("delta")),
        "drops": dict(drops),
        "exhaustions": len(exhaustions),
        "promotion_decisions": dict(promotions),
    }


def reviewed_prs(events: list[dict]) -> list[tuple[str, int]]:
    """Distinct (repo, pr) we posted verdicts on — the three-way comparison universe."""
    seen: dict[tuple[str, int], None] = {}
    for e in events:
        if e.get("event") == "reviewed" and e.get("posted"):
            seen[(str(e["repo"]), int(e["pr"]))] = None
    return list(seen)


async def three_way_rows(
    events: list[dict], run_gh_fn, *, quinn_logins=("protoquinn",), coderabbit_login="coderabbitai"
) -> list[dict]:
    """Per reviewed PR: our verdict vs Quinn's latest review state vs CodeRabbit
    thread count. The comparison dataset Phase D's shadow report aggregates."""
    ours_by_pr: dict[tuple[str, int], str] = {}
    for e in events:
        if e.get("event") == "reviewed" and e.get("posted"):
            ours_by_pr[(str(e["repo"]), int(e["pr"]))] = str(e.get("verdict"))
    rows = []
    for (repo, pr), our_verdict in ours_by_pr.items():
        rc, out, _err = await run_gh_fn(
            [
                "api",
                f"repos/{repo}/pulls/{pr}/reviews",
                "--paginate",
                "--jq",
                "[.[] | {login: .user.login, state: .state}]",
            ],
        )
        quinn_state, rabbit_reviews = "", 0
        if rc == 0:
            try:
                for row in json.loads(out):
                    login = str(row.get("login") or "").lower().removesuffix("[bot]")
                    if any(login.startswith(q) for q in quinn_logins):
                        quinn_state = row.get("state", "")
                    if login.startswith(coderabbit_login):
                        rabbit_reviews += 1
            except json.JSONDecodeError:
                pass
        rows.append(
            {"repo": repo, "pr": pr, "ours": our_verdict, "quinn": quinn_state, "coderabbit_reviews": rabbit_reviews}
        )
    return rows

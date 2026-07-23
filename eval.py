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


def _step_percentiles(reviewed: list[dict]) -> dict:
    """p50 wall-clock per recipe step, over reviews whose host reported timings.

    The panel's cost is a DAG, not a number: the four/five finders run as one parallel
    stage (so the stage costs the slowest of them), then synthesize -> verify -> report
    run strictly in sequence. Attacking the wrong one buys nothing.
    """
    per: dict[str, list[float]] = {}
    for e in reviewed:
        for sid, secs in (e.get("step_s") or {}).items():
            try:
                per.setdefault(str(sid), []).append(float(secs))
            except (TypeError, ValueError):
                continue
    return {sid: _percentile(v, 0.5) for sid, v in sorted(per.items())}


def build_report(events: list[dict]) -> dict:
    """The numeric summary over raw telemetry events."""
    dispatches = [e for e in events if e.get("event") == "dispatch"]
    reviewed = [e for e in events if e.get("event") == "reviewed"]
    reaffirmed = [e for e in events if e.get("event") == "reaffirm"]
    drops = Counter(str(e.get("reason")) for e in events if e.get("event") == "drop")
    exhaustions = [e for e in events if e.get("event") == "exhaustion"]
    promotions = Counter(str(e.get("decision")) for e in events if e.get("event") == "promotion")
    latencies = [float(e["latency_s"]) for e in reviewed if e.get("latency_s") is not None]
    rounds: dict[tuple[str, int], int] = {}
    for e in reviewed:
        key = (str(e.get("repo")), int(e.get("pr") or 0))
        rounds[key] = max(rounds.get(key, 0), int(e.get("round") or 1))
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
        # Convergence (issue #23). `rounds_per_pr` is the metric the loop was invisible
        # in: an average creeping past ~3, or a max in the high single digits, is a
        # panel re-reviewing its own churn — #88 hit 8 before anyone counted.
        # Where the 7.5-minute p50 actually goes. The panel is nine LLM steps; a single
        # latency number cannot say which one to attack (issue: latency work, 2026-07-23).
        "slowest_step_mix": dict(Counter(str(e.get("slowest_step")) for e in reviewed if e.get("slowest_step"))),
        "step_p50_s": _step_percentiles(reviewed),
        "rounds_per_pr": round(sum(rounds.values()) / len(rounds), 2) if rounds else None,
        "max_rounds": max(rounds.values()) if rounds else None,
        "converged": sum(
            1 for e in events if e.get("event") == "converged" and str(e.get("reason", "")).startswith("converged")
        ),
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


def render_report_markdown(summary: dict, rows: list[dict] | None = None) -> str:
    """The human rendering — what the eval command/endpoint returns. `rows` is the
    three-way comparison (optional: telemetry-only callers skip the GitHub reads)."""
    lat = summary.get("latency_s") or {}
    completion = summary.get("completion_rate")
    lines = [
        "## PR-review eval",
        "",
        f"- **Dispatches:** {summary.get('dispatches', 0)} · **verdicts posted:** "
        f"{summary.get('reviews_posted', 0)} · **completion:** "
        f"{f'{completion:.1%}' if completion is not None else 'n/a'} (Quinn's floor: 99.4%)",
        f"- **Verdict mix:** {summary.get('verdict_mix') or {}} · **recipes:** {summary.get('recipe_mix') or {}}",
        f"- **Latency:** p50 {lat.get('p50')}s / p90 {lat.get('p90')}s over {lat.get('n', 0)} review(s) "
        f"(Quinn's floor: 44.7s median — the lite recipe is the lever)",
        f"- **Reaffirmed (unchanged head):** {summary.get('reaffirmed', 0)} · **delta re-reviews:** "
        f"{summary.get('delta_reviews', 0)} · **findings/review:** {summary.get('findings_per_review')}",
        f"- **Step p50s:** {summary.get('step_p50_s') or 'n/a (host reports no timings)'} · "
        f"**slowest step:** {summary.get('slowest_step_mix') or {}}",
        f"- **Rounds/PR:** {summary.get('rounds_per_pr')} (max {summary.get('max_rounds')}) · "
        f"**converged to PASS-with-notes:** {summary.get('converged', 0)} — a climbing max is the "
        f"panel re-reviewing its own churn (issue #23)",
        f"- **Exhaustions (fail-closed, no verdict):** {summary.get('exhaustions', 0)}",
        f"- **Typed drops:** {summary.get('drops') or {}}",
        f"- **Promotion decisions:** {summary.get('promotion_decisions') or {}}",
    ]
    if rows is not None:
        lines += ["", "### Three-way comparison (ours vs Quinn vs CodeRabbit, same PRs)", ""]
        if not rows:
            lines.append("_No posted verdicts yet — the comparison universe is empty._")
        else:
            lines += [
                "| PR | ours | Quinn | CodeRabbit reviews |",
                "|---|---|---|---|",
            ]
            for r in sorted(rows, key=lambda x: (x["repo"], x["pr"])):
                quinn = r.get("quinn") or "—"
                lines.append(f"| {r['repo']}#{r['pr']} | {r['ours']} | {quinn} | {r.get('coderabbit_reviews', 0)} |")
            overlap = sum(1 for r in rows if r.get("quinn"))
            lines += ["", f"_{overlap}/{len(rows)} PRs also carry a Quinn verdict (the dual-layer overlap)._"]
    return "\n".join(lines)


def get_eval_tools(telemetry, run_gh_fn) -> list:
    """The agent-facing eval command (registered when the machinery wires up).

    Keep the docstring a PLAIN string literal (an f-string docstring ships no
    description)."""
    from langchain_core.tools import tool

    @tool
    async def pr_review_eval(three_way: bool = True) -> str:
        """Render the PR-review eval report: completion rate, verdict/recipe mix, latency percentiles, typed drops, promotion decisions — and (three_way=True, default) the per-PR three-way comparison of our verdicts vs Quinn's review states vs CodeRabbit activity on the same PRs. Reads the plugin's telemetry; the three-way section also reads each PR's reviews from GitHub. Use it to check how the shadow reviewer is performing or to assemble the stage-1 report."""
        summary = build_report(telemetry.read_all())
        rows = None
        if three_way:
            try:
                rows = await three_way_rows(telemetry.read_all(), run_gh_fn)
            except Exception as exc:  # noqa: BLE001 — degrade to the telemetry-only report
                return render_report_markdown(summary) + f"\n\n_(three-way rows unavailable: {exc})_"
        return render_report_markdown(summary, rows)

    return [pr_review_eval]

"""CLI for replay mode — run the panel over a pinned manifest, one JSON run-output per row.

    python -m pr_reviewer.replay_cli --manifest replay_manifest.jsonl [--model protolabs/fast] \
        [--trials 1] > runs.jsonl

Side-effect-free: reads GitHub (blobs, diffs, compares), never writes. The MODEL is the
A/B knob — passed here, threaded into each row, and resolved by the host runner the plugin
is loaded in. Runs OUTSIDE the live-PR path; no chokepoint, no posting.

Reconcile the output shape with protoLab:evals/review-eval/SCHEMA.md before scoring — the
lab-side contract isn't pushed where the plugin can read it, so this matches issue #20's
described fields (run header + findings + telemetry incl. `truncated`).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _main() -> int:
    ap = argparse.ArgumentParser(description="Replay the QA panel over a pinned manifest.")
    ap.add_argument("--manifest", required=True, help="replay_manifest.jsonl — one pinned round per line")
    ap.add_argument("--model", default="", help="gateway alias for the A/B (e.g. protolabs/fast); overrides row.model")
    ap.add_argument("--trials", type=int, default=1, help="repeat each row N times (determinism is not expected)")
    ap.add_argument("--stamp", default="", help="opaque run stamp carried into every output (time is not read here)")
    args = ap.parse_args()

    from .gh_cli import run_gh
    from .replay import replay_review

    def parse_findings(output: str) -> list[dict]:
        try:
            from graph.review.findings import parse_findings as host_parse

            return [f.to_dict() for f in host_parse(output)]
        except Exception:  # noqa: BLE001 — host-free fallback: last fenced array
            from .verdicts import extract_findings_json

            text = extract_findings_json(output)
            try:
                return json.loads(text) if text else []
            except json.JSONDecodeError:
                return []

    try:
        from runtime.state import STATE

        runner = STATE.workflow_run
    except Exception:  # noqa: BLE001
        print("replay: no workflow runner (STATE.workflow_run) — run inside a protoAgent host", file=sys.stderr)
        return 2

    rows = [json.loads(ln) for ln in open(args.manifest) if ln.strip()]
    for row in rows:
        if args.model:
            row["model"] = args.model
        for trial in range(args.trials):
            out = await replay_review(
                row, run_gh=run_gh, runner=runner, parse_findings=parse_findings, trial=trial, stamp=args.stamp
            )
            print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

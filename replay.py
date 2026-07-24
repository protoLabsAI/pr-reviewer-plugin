"""Replay mode — run the panel on a pinned checkout+diff, findings to JSON (issue #28→#20).

The model-swap gate (protoLab#26) has to run the panel *end to end* — finders + verify +
every guard, exactly as deployed — not the raw model, because the guards are most of what
determines a verdict. This is the runner that lets the panel run OUTSIDE the live-PR path:

  - inputs pinned to a SHA (the reviewed blob is the only thing ground truth is valid against)
  - the recipe run through a caller-supplied `runner`, which is where the A/B model alias
    lives — the plugin doesn't resolve models, the harness binds the runner to `fast`/`smart`
  - findings + verdict + the #34 guard telemetry emitted as one JSON object, NOT posted
  - NO GitHub writes — replay is side-effect-free, so it runs repeatedly per model per trial

Reuses the exact guard functions the live path uses (`confine_findings`, `apply_grounding`,
`verdict_for`, `converge`, `parse_dispositions`, `unaccounted_priors`) — the whole point is
to measure the panel as deployed, so replay must not fork the logic. It deliberately does
NOT touch `Dispatcher._review`: the live gating path is not refactored for a measurement
harness.

Truncation is first-class. A model that burns its budget on hidden reasoning and emits no
final answer (the `fast` incident: 6k tokens, no output) produces an empty report — and
`findings=[] & truncated=true` must be distinguishable from a clean pass, which emits an
explicit `[]`. The scorer needs to see the difference or a truncated run reads as "found
nothing," inflating the miss rate against the model unfairly.

The output contract here matches issue #20's description; reconcile with
`protoLab:evals/review-eval/SCHEMA.md` (lab-side, not yet pushed where the plugin can read
it) before wiring the scorer — the field NAMES may need aligning, the SHAPE is right.
"""

from __future__ import annotations

import json
import re

from .grounding import apply_grounding
from .rounds import converge, delta_ranges, parse_dispositions, unaccounted_priors
from .verdicts import confine_findings, extract_findings_json, verdict_for

# The report step is contracted to end with a fenced findings array — `[]` on a clean
# pass. Its ABSENCE (not emptiness) is the truncation signal: the model never produced a
# final answer. A present-but-empty array is a genuine "nothing found".
_FENCED_ARRAY_RE = re.compile(r"```json\s*\n\s*\[", re.DOTALL)


def looks_truncated(output: str, findings: list[dict]) -> bool:
    """No emitted findings array at all ⇒ the report never landed (truncation), which is
    NOT the same as an emitted `[]` (clean). If we did parse findings, it landed."""
    if findings:
        return False
    return not _FENCED_ARRAY_RE.search(output or "")


async def replay_review(
    row: dict,
    *,
    run_gh,
    runner,
    parse_findings,
    trial: int = 0,
    stamp: str = "",
) -> dict:
    """Run the panel against one pinned round and return the JSON run-output.

    `row` (a `replay_manifest.jsonl` entry):
        repo, pr, head            — the pinned reviewed SHA (required)
        base_ref | base_sha       — for delta/convergence (optional; "" ⇒ skip delta guards)
        recipe                    — defaults to "code-review-structural"
        prior_findings            — JSON string of the previous round's findings (for the
                                    honesty/false-negative probes — #2208 r2, #2141 r3)
        prior_requests            — pre-rendered <prior_requests> block (optional)
        round                     — round number (default 1)

    `runner(recipe, inputs)` is bound to the model under test by the caller — that binding
    IS the A/B knob. `parse_findings` is injected (the host's findings parser, or the
    plugin's fallback) so replay matches the live parse exactly.

    Time and randomness are passed in (`stamp`, `trial`), never read here — a replay must
    be reproducible and stamping happens outside.
    """
    repo = str(row["repo"])
    pr = int(row["pr"])
    head = str(row["head"])
    base_ref = str(row.get("base_ref") or row.get("base_sha") or "")
    recipe = str(row.get("recipe") or "code-review-structural")
    round_number = int(row.get("round") or 1)

    inputs = {"pr": str(pr), "repo": repo, "head_sha": head, "base_ref": base_ref}
    if row.get("prior_findings"):
        inputs["prior_findings"] = str(row["prior_findings"])
    if row.get("prior_requests"):
        inputs["prior_requests"] = str(row["prior_requests"])
        inputs["review_round"] = str(round_number)

    result = await runner(recipe, inputs)
    output = str(result.get("output") or "")
    failed = list(result.get("failed") or [])
    timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}

    # Same guard pipeline as the live path, same order, same functions.
    paths = await _changed_paths(run_gh, repo, pr)
    findings, confined = confine_findings(parse_findings(output), paths)

    grounding_checked = 0
    ungrounded: list[dict] = []
    if findings:
        sources = await _finding_sources(run_gh, repo, pr, head, findings)
        findings, ungrounded = apply_grounding(findings, sources)
        grounding_checked = len(findings)

    verdict = verdict_for(findings)

    # Convergence + disposition guards need the delta prior-head→head. The manifest row
    # carries the prior head when a prior round exists (the probes that exercise these).
    ranges = None
    prior_head = str(row.get("prior_head") or "")
    if prior_head and prior_head != head:
        ranges = await _delta_ranges(run_gh, repo, prior_head, head)

    verdict, notes, converge_reason = converge(verdict, findings, round_number=round_number, ranges=ranges)

    dispositions = parse_dispositions(output)
    history = _history_from_row(row)
    unaccounted = unaccounted_priors(history, dispositions, ranges=ranges)

    truncated = looks_truncated(output, findings) and not failed

    return {
        "run": {
            "repo": repo,
            "pr": pr,
            "head": head,
            "recipe": recipe,
            "round": round_number,
            "model": str(row.get("model") or ""),
            "trial": trial,
            "stamp": stamp,
        },
        "verdict": verdict,
        "findings": findings,
        "telemetry": {
            "failed_steps": failed,
            "truncated": truncated,
            "confined": len(confined),
            "grounding_checked": grounding_checked,
            "grounding_downgraded": len(ungrounded),
            "converge_reason": converge_reason,
            "converge_notes": len(notes),
            "dispositions": len(dispositions),
            "unaccounted_priors": len(unaccounted),
            "step_seconds": timings,
            "token_usage": usage,  # {} unless the host runner surfaces it
        },
    }


# ── GitHub reads (read-only — replay never writes) ─────────────────────────────────────
#
# These mirror the Dispatcher's own helpers rather than importing them: replay must not
# depend on constructing a full Dispatcher (chokepoint, telemetry, config), and the reads
# are three plain `gh api` calls. Kept in lockstep with dispatch.py by shape.


async def _changed_paths(run_gh, repo: str, pr: int) -> list[str]:
    rc, out, _e = await run_gh(["api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--jq", ".[].filename"])
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []


async def _finding_sources(run_gh, repo: str, pr: int, head: str, findings: list[dict]) -> dict:
    patches: dict[str, str] = {}
    rc, out, _e = await run_gh(
        ["api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--jq", "[.[] | {f: .filename, p: .patch}]"]
    )
    if rc == 0:
        try:
            for r in json.loads(out) or []:
                if isinstance(r, dict) and r.get("f"):
                    patches[str(r["f"])] = str(r.get("p") or "")
        except json.JSONDecodeError:
            pass
    sources: dict[str, str | None] = {}
    for file in {str(f.get("file") or "") for f in findings if f.get("file")}:
        rc, out, _e = await run_gh(["api", f"repos/{repo}/contents/{file}?ref={head}", "--jq", ".content"])
        blob = ""
        if rc == 0 and out.strip():
            try:
                import base64

                blob = base64.b64decode(out.strip()).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                blob = ""
        patch = patches.get(file, "")
        sources[file] = f"{blob}\n{patch}" if (blob or patch) else None
    return sources


async def _delta_ranges(run_gh, repo: str, base: str, head: str) -> dict | None:
    rc, out, _e = await run_gh(
        ["api", f"repos/{repo}/compare/{base}...{head}", "--jq", "[.files[]? | {filename: .filename, patch: .patch}]"]
    )
    if rc != 0:
        return None
    try:
        files = json.loads(out)
    except json.JSONDecodeError:
        return None
    return delta_ranges(files) if isinstance(files, list) else None


def _history_from_row(row: dict) -> list[dict]:
    """The prior round's findings, as `panel_rounds` would present them — supplied by the
    manifest (replay pins single rounds, so prior context is data, not a GitHub read).
    `prior_findings` is the same JSON string the live `prior_findings` input carries."""
    raw = row.get("prior_findings")
    if not raw:
        return []
    text = raw if isinstance(raw, str) else json.dumps(raw)
    inner = extract_findings_json(f"```json\n{text}\n```") or text
    try:
        parsed = json.loads(inner)
    except json.JSONDecodeError:
        return []
    findings = [f for f in parsed if isinstance(f, dict)] if isinstance(parsed, list) else []
    return [{"head": str(row.get("prior_head") or "prior"), "verdict": "FAIL", "findings": findings}]

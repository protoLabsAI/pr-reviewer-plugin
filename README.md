# pr-reviewer-plugin

The deterministic half of protoAgent's PR-review QA tier
([ADR 0078](https://github.com/protoLabsAI/protoAgent/blob/main/docs/adr/0078-fleet-pr-review-qa-tier.md)).

## What it ships (Phase B2)

- **`protopatch_review`** — runs the [protoPatch](https://github.com/protoLabsAI/protoPatch)
  (`clawpatch`) structural analysis engine over a pull request and returns its findings
  in the ADR 0077 findings contract with `source: "protopatch"`.
  - Head/base SHAs resolved **server-side** from the PR (never model-supplied refs).
  - A content-addressed checkout cache: blobless partial clones (`--filter=blob:none`)
    keyed on `repo@headSha`, 1h TTL, LRU `prune()` under entry/byte caps.
  - `clawpatch ci --provider gateway --json --state-dir <per-repo> --since <baseSha>`
    under a hard wall-clock budget (default 300s, SIGKILL past it).
  - Findings read from the per-repo state dir, confined to the PR's changed files,
    severity mapped (critical/high/medium/low → blocker/major/minor/nit), category
    preserved verbatim.
  - **Every failure degrades** (`PROTOPATCH UNAVAILABLE` + a prescribed Gap line) —
    a starved structural pass must never void the panel review (ADR 0078 D3).
- **`structural-finder`** — the subagent seat: calls the tool once, relays the findings
  verbatim, reports the Gap on unavailability. A relay, not a reviewer.
- **`workflows/code-review-structural.yaml`** — the five-finder panel recipe: the four
  core LLM finders + the structural seat → dedup/rank → independent verify → report.
  protoPatch findings get the same adversarial verify as everything else — the edge
  over wiring the engine straight into a verdict.

Phase C shipped the deterministic loop around the panel: webhook chokepoint,
structural-trigger dispatch, approve-on-green + sweep, and the review eval.

- **In-diff confinement (v0.4.0)** — parsed findings whose `file` isn't one of the
  PR's changed paths are dropped server-side before the verdict mapping (telemetered,
  footnoted in the posted body). The panel prompts promise in-diff discipline; the
  dispatcher now enforces it. Fails open when the changed-path list is unreadable —
  a failed GitHub read must never launder a FAIL into a PASS.

## Requirements

- protoAgent ≥ the version carrying the findings `source` field (see the manifest pin).
- `git`, `gh` (authenticated, or `GITHUB_TOKEN`/`GH_TOKEN`), and the `clawpatch` CLI
  (`npm i -g @protolabsai/protopatch`).
- Gateway credentials in the host env: `GATEWAY_API_KEY` or `OPENAI_API_KEY`
  (+ `OPENAI_BASE_URL` / `pr_reviewer.gateway_base_url` for a non-default gateway).

## Dev

```
pip install -r requirements-dev.txt
ruff check . && pytest -q
```

Host-free: the suite stubs `graph.subagents.config` and never shells out.

_Reviewed by its own machinery — see ADR 0078._

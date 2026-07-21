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
- **Existing-thread awareness (v0.5.0)** — the dispatcher fetches the PR's inline
  review threads (Quinn's, CodeRabbit's, humans'), renders them as one escaped
  `<pr_review_threads>` data block (closing-tag neutralization, login-grammar
  validation, body truncation), and passes it as the `existing_threads` recipe
  input; finders suppress candidates that overlap a live thread. Unreadable
  threads degrade to "(none)" — awareness never blocks a review.

## Requirements

- protoAgent ≥ the version carrying the findings `source` field (see the manifest pin).
- `git`, `gh` (authenticated, or `GITHUB_TOKEN`/`GH_TOKEN`), and the `clawpatch` CLI
  (`npm i -g @protolabsai/protopatch`).
- Gateway credentials in the host env: `GATEWAY_API_KEY` or `OPENAI_API_KEY`
  (+ `OPENAI_BASE_URL` / `pr_reviewer.gateway_base_url` for a non-default gateway).

## Config (env fallbacks)

The operator-tunable state reads **config first, env as a fallback** — the same
posture as `webhook_secret`, for headless config-as-code deployments where the
config volume is seed-once and can't be re-edited on an image roll. A config key
present always wins; the env only fills an unset/empty key. Put these in the
compose env (re-applied every roll) to keep the config volume disposable:

| Env | Config key | Default | Notes |
|---|---|---|---|
| `PR_REVIEWER_REPOS` | `pr_reviewer.repos` | `[]` | Managed allowlist; comma/space/newline separated. Config wins only when non-empty (seed ships `repos: []` → env applies). |
| `PR_REVIEWER_SHADOW_MODE` | `pr_reviewer.shadow_mode` | `true` | `1/true/yes/on` ⇒ shadow. A present config bool (incl. `false`) wins over the env. |
| `PR_REVIEWER_PROMOTION_OWNER` | `pr_reviewer.promotion_owner` | `false` | Same tri-state semantics. |
| `PR_REVIEWER_PANEL_RETRIES` | `pr_reviewer.panel_retries` | `1` | Re-runs of a recipe whose panel reported a failed step, before D3 escalation. `0` restores the old give-up-on-first-failure behaviour. |
| `PR_REVIEWER_BACKFILL_PER_PASS` | `pr_reviewer.backfill_per_pass` | `2` | Reviews the sweep may backfill per pass, across all repos. `0` disables backfill. |

### What the sweep does (every `sweep_interval_s`, default 180s)

Each open PR in each managed repo is reconciled in this order — cheapest and most
decisive first:

1. **Backfill** — no verdict for the current head ⇒ review it. Dispatch actions only
   fire for live webhook events, so a PR opened before the reviewer existed (or while
   it was down, or whose panel exhausted) would otherwise hold `no-clear-verdict`
   forever and never become promotable. Budgeted by `backfill_per_pass`.
2. **Re-gate** — a FAIL standing against the current head that isn't blocking yet, now
   that checks are terminal ⇒ post the stored verdict as `REQUEST_CHANGES`. A verdict
   must decide its review event when the panel lands, and #863 forbids blocking against
   pending CI, so a fast reviewer's FAIL posts as a comment and the gate never arms.
   This is the mirror of the stale-block dismissal: that lifts a block, this arms one.
3. **Promote** — the existing approve-on-green path.

A PR that was just backfilled skips 2 and 3 for that pass; the fresh review posts its
own verdict through the normal path and the next tick sees settled state.

## Dev

```
pip install -r requirements-dev.txt
ruff check . && pytest -q
```

Host-free: the suite stubs `graph.subagents.config` and never shells out.

_Reviewed by its own machinery — see ADR 0078._

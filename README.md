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
- **Re-review convergence (v0.8.0)** — a review loop now has an exit. Three parts,
  all in `rounds.py` (issue #23; the case was projectBoard-plugin#88, eight rounds
  on a small store fix where the panel kept reviewing changes it had itself demanded):
  - **Rounds, not reviews.** Recall reads the PR's *panel rounds*. A promotion body
    carries our marker and no findings, so taking the newest marker-bearing review as
    "the prior review" meant that after any approve-on-green the next round recalled
    an empty `prior_findings` and silently re-reviewed **cold**. A re-gate's verbatim
    re-post no longer double-counts a head either.
  - **Prior-request memory** — every round's findings ride along as one escaped
    `<prior_requests>` block plus `review_round`. A finder can see that the line it is
    about to flag exists *because the panel asked for it*: it verifies the change was
    implemented correctly instead of re-litigating it as unexplained new scope. A
    wrong, partial or defect-introducing fix is still a finding.
  - **The exit rule** — from round 3 (`PR_REVIEWER_CONVERGENCE_ROUNDS`), a **WARN**
    whose findings are *all* minor/nit **and** *all* anchored to lines that moved since
    the previous reviewed head becomes **PASS with notes**: the findings still post, as
    a follow-up checklist, they just stop holding the verdict. Fails closed in every
    direction — a FAIL never converges, an uncertain major never converges, a finding
    on code the review never touched never converges, and an unreadable compare grants
    no relief at all.

- **Unexplained-clearance hold (v0.9.0)** — a zero-finding PASS is the highest-consequence
  verdict this machinery posts: it dismisses our own `REQUEST_CHANGES` and clears the
  promotion path. On protoAgent#2141 the panel confirmed a major on one head, returned
  PASS with zero findings on the next with the code unchanged, and the defect merged 44
  seconds later. A miss cannot be caught the way a hallucination can — there is no claim
  to re-ground, and `findings=0` reads identically whether the code is clean or nobody
  looked — so the rule is structural: a blocker/major that *disappears* without being
  fixed, carried, or refuted is treated as unproven, and the block stays up. The verdict
  still posts, names the dropped finding, and a **second consecutive** clean PASS lifts
  the block automatically (two independent draws are evidence; one is a coin flip).

- **Evidence grounding (v0.10.0)** — the verify pass exists to kill plausible-but-wrong
  findings, and twice on 2026-07-22 it did the opposite: it *confirmed* claims about code
  that isn't in the file, escalating one to a blocker on a head where the operator had
  already posted the refuting blob **and** a passing test asserting the behaviour. The
  panel wasn't missing the evidence, it was discounting evidence in view — which is why
  this is code and not only prompt discipline (the `confine_findings` lesson, applied to
  the evidence itself). A finding whose quoted code appears nowhere in the cited file at
  the reviewed head, nor in this PR's patch for it, is annotated `uncertain`; nothing is
  ever dropped, and `verdict_for` already refuses to turn `uncertain` into a FAIL.
  Fail-open throughout — unreadable blob, no quotable evidence, or any one quote that
  matches, and the finding stands. It catches the fabricated-quote class; a finding that
  quotes real code and reasons wrongly about it (a prefix that doesn't actually match) is
  the verify prompt's half.

- **Prior-finding dispositions (v0.11.0)** — the general form of the clearance hold. The
  report pass must state, per prior **blocker/major**, whether it was `fixed` (naming the
  change), is still `open`, or was `refuted` (on evidence). A confirmed major that simply
  stops being mentioned holds any standing block, **whatever the new verdict is** — the
  v0.9.0 rule could only guard a zero-finding PASS, because silence there is unambiguous,
  and protoAgent#2150 showed a major vanishing into a WARN about unrelated nits instead.
  The two guards are a **fallback chain**: when dispositions are present they are the
  authority (re-applying the clean-PASS heuristic on top would hold a block the panel just
  explained); a recipe that emits no block keeps the narrower v0.9.0 rule.

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
| `PR_REVIEWER_EVIDENCE_GROUNDING` | `pr_reviewer.evidence_grounding` | `true` | A finding whose quoted code appears nowhere in the cited file at the reviewed head (nor in this PR's patch for it) is annotated `uncertain` — it still posts, it just can't carry a FAIL. Fails open on an unreadable blob or unquotable evidence. |
| `PR_REVIEWER_HOLD_UNEXPLAINED_CLEARANCE` | `pr_reviewer.hold_unexplained_clearance` | `true` | A zero-finding PASS does not dismiss our standing block when a prior round confirmed a blocker/major it neither reports nor explains. A second consecutive clean PASS lifts it. `false` restores the old always-dismiss behaviour. |
| `PR_REVIEWER_CONVERGENCE_ROUNDS` | `pr_reviewer.convergence_rounds` | `3` | The round from which an all-minor, all-in-delta WARN retires to PASS-with-notes. `0` disables the rule — the panel keeps re-reviewing rather than ever floor a minor. |
| `PR_REVIEWER_REGATE` | `pr_reviewer.regate` | `true` | Master switch for step 2 below. `false` stops arming blocks while KEEPING the formal seat, promotion and backfill — the lever to pull when the panel is emitting false FAILs. |

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

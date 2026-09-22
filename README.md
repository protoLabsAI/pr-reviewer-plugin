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

- **Panel latency work (v0.12.0)** — the five finders are one parallel stage, but the
  host's `subagent_max_concurrency` defaults to **4**, so the stage silently ran as
  **4+1** and paid the slowest finder twice. Measured over 60 reviews: the five-finder
  recipe's p50 was **458s** against **322s** for the otherwise-identical four-finder one,
  which solves to ~136s per finder and ~186s for the sequential tail. The recipe now
  declares `max_concurrency: 5` (needs protoAgent#2168; an older host ignores it). The
  dispatcher also records the engine's per-step `timings`, and the eval report shows a
  p50 per step plus a slowest-step histogram — "the panel is slow" was never an
  actionable number across nine steps.

- **A promoted WARN carries its findings (v0.13.0)** — approve-on-green promotes WARN by
  design (a WARN "does NOT block merge"), so a confirmed finding could land and the PR
  read **APPROVED** thirty seconds later with the finding having no consumer at all; that
  is how projectBoard-plugin#80 shipped a malformed-label defect. The approval body now
  restates the open findings and the marker gains `findings=N`, so merge tooling can gate
  on "approved WITH findings" without parsing prose. Deliberately **not** a block:
  making WARN gate would have hard-blocked a correct PR on the hallucinated blocker this
  panel produced twice in one night — gate rigidity must not outrun verdict reliability.
  The promotion path also now reads panel *rounds* rather than `ours[-1]`, which could be
  our own promotion body (marker-bearing, findings-free) — the same shadowing #24 fixed
  for delta recall.

- **On-demand review (v0.15.0, slice 1 of #28)** — `@vera review` in a PR comment runs the
  panel now. Every review before this was triggered by a push or the sweep, so the cheapest
  way to ask a question about a PR was to alter the artifact you were asking about — and a
  refutation of a wrong finding had nowhere to go (protoAgent#2138: the operator posted a
  blob citation *and* a passing test, and nothing consumed either).
  - **Admin only, resolved server-side** via the collaborator-permission API — never from
    the payload's `author_association`, which is caller-supplied. Fails closed. The gate is
    about cost: a summon spends five subagents for 5–9 minutes.
  - **A summon overrides the reaffirm short-circuit.** An unchanged head normally reaffirms
    without re-spending the panel; `@vera review` on that head is precisely the "I think you
    got this wrong" case, and reaffirming would answer with the answer under dispute.
  - **Bypasses the cooldown, not the in-flight guard** — the cooldown eats webhook bursts,
    and a human who typed a command is not a burst; two panels on one PR is still wrong.
    The guard can't wedge a PR, though: each panel attempt and each round is bounded
    (`panel_attempt_timeout` / `round_timeout`, below), and a slot still held past the round
    bound + 10 min is reclaimed as abandoned — logged, and `in_flight_reclaimed` in
    telemetry. Before that, one hung round answered every `@vera review` with "in-flight"
    until the process restarted.
  - **Never silent.** Refusals, unknown verbs and drops all reply. `@vera help` lists the
    verbs. `@vera` alone is treated as asking what this thing does.
  - Handle is `summon_handle` (default `vera`) *plus* the reviewer's own login, and it never
    answers itself — its own verdict bodies mention the handle.
  - **`pause` / `resume` (v0.17.0)** — stop reviewing a PR on push while it is being
    reworked; an explicit `@vera review` still runs, because "stop reviewing every push"
    and "never look at this again" are different requests. State rides in a marker on a
    posted comment, so GitHub is the store (ADR 0078 D5) and a restart cannot forget it.
    The **last** marker wins, not a tally — pause → resume → pause ends paused.
  - ⚠️ **Requires GitHub App events.** A summon arrives as `issue_comment` (and, for
    inline replies, `pull_request_review_comment`). An App subscribed only to
    `pull_request` — as ours was when this shipped — makes every summon vanish with no
    error at all: correct code, no event. `GET /api/plugins/pr-reviewer/summon/health`
    reports exactly which events are missing.

- **Replay mode (v0.18.0)** — run the panel against a **pinned checkout+diff**, findings
  to JSON instead of GitHub, for the model A/B (qaEngineer#20, protoLab#26). Same finders,
  verify pass, and guards as the live path (it reuses the exact functions, not a fork);
  the model is a per-run gateway alias (`protolabs/fast` vs `protolabs/smart`) — that's the
  entire A/B knob. **Side-effect-free**: reads blobs/diffs, never writes. Truncation is
  first-class — a model that burns its budget on hidden reasoning and emits no answer
  (`findings=[] & truncated=true`) is distinguished from a clean pass (an emitted `[]`), so
  a truncated run isn't scored as "found nothing". `python -m pr_reviewer.replay_cli
  --manifest replay_manifest.jsonl --model protolabs/fast`.

- **Findings render as a table (v0.19.0)** — the posted review shows a scannable
  severity-sorted markdown table (severity · location · finding · verified) instead of a
  raw JSON dump. The machine-readable JSON is kept in a collapsed `<details>` — prior-round
  recall reads it back out of the body, so it can't be removed. A clean pass (`[]`) or a
  prose-only report is left untouched.

- **Absent is not empty; a blind lane is not a clean pass (issues #113, #117)** — an
  explicit `[]` means "looked, found nothing"; *no array at all* means nothing reached that
  boundary. Both used to parse as `[]`, so a lost payload posted PASS with "the review came
  back clean" (#113), and a PASS over one real lane of five was promoted and merged (#117).
  - **An absent payload is an incomplete round, never a verdict.** If no finder lane
    delivered a findings array (a timeout Gap, a `PROTOPATCH UNAVAILABLE` relay, or a
    `FINDER_STATUS: blocked` lane is not a delivery), or the synthesize step or the final
    report emitted none, the panel is re-run (`panel_retries`); if it still delivers
    nothing it ends like an exhausted panel — no review posted, `protoReview` red ("QA
    panel incomplete — no verdict"), the operator escalated, an `exhaustion` event with
    `undelivered: [...]`, and the sweep's backfill retries the head later. A missing
    `FINDER_STATUS` line alone never voids a lane: that is a coverage gap, below.
  - **A coverage gap caps a clean PASS at WARN.** Any lane the engine timed out, any LLM
    finder that did not declare `FINDER_STATUS: reviewed`, or a structural relay that was
    unavailable or cut short: `complete=false` (promotion holds, as before), a
    code-authored **coverage line above the brief** naming each lane and why (it
    supersedes any claim of full coverage in the model-written brief), no "came back
    clean" line, and PASS posted as **WARN** (`coverage_capped` in telemetry). Not FAIL
    and not "no verdict": a gap means the review covered less, not that the code is bad,
    and the structural lane gaps on most large protoAgent reviews today (#119). Lanes are
    judged only where the recipe ran them under that contract — the small-diff
    `code-review` recipe has no structural seat and asks for no status line.
  - **A verify step that hands nothing back on a clean round is a gap too (#151).** With
    findings, a dead verifier already shows (nothing is annotated → `verified=false`).
    With none it could not: "nothing to check" and "stopped at its preamble" looked the
    same, and the round posted "came back clean" above a report saying the verifier never
    ran. Now a zero-finding round whose `verify` output carries no fenced array and no
    `VERIFY_STATUS` line names `verify` in the coverage line and posts WARN
    (`verify_undelivered` in telemetry). `complete` stays true — every finder covered the
    diff — and the round is not retried: nothing went unverified.

## The draft → ready contract — undrafting a PR is the act of shipping it

On a repository this plugin watches, a PR's **draft** state is its merge gate. Read
this before you mark a PR ready for review.

- **Draft PRs are skipped by the panel.** A PR in the draft state is dropped before
  the panel runs — the same eligibility gate that skips a closed or a locked
  conversation (telemetered `pr-not-eligible`, `why=draft`). No review is posted, no
  verdict is produced, and nothing can promote while the PR stays draft.
- **Marking a PR ready for review hands it to the QA pipeline.** The draft→ready
  transition is a review trigger: on a watched, `main`-targeting PR the panel runs,
  and once it posts a **current, complete PASS** and the promotion guards are all
  green, approve-on-green posts a formal APPROVE **and arms native GitHub squash
  auto-merge** (`gh pr merge --auto --squash`). Arming is best-effort and scoped to
  `main`-targeting PRs (stacked PRs are excluded); a repo with auto-merge disabled
  simply declines.

So the practical contract is: **undraft a PR only when it is ready to ship, not when
you merely want eyes on it.** The moment the panel is satisfied, an eligible
ready-for-review PR with a promoted current PASS lands on its own.

### A PASS does not bypass the guards

Auto-merge is armed only when a clear PASS/WARN verdict clears **every** fail-closed
guard in `promotion_decision` — approve-on-green is exactly as conservative as the
panel, and a PASS is not a skeleton key past any of these:

- this agent **owns promotion** for the repo and is **not in shadow mode**;
- a **clear verdict** (PASS or WARN) exists for the PR's **current head SHA** — a
  verdict for a superseded head is **stale** and holds (`hold:stale-head`), so a PASS
  never lands a commit the panel never saw;
- that verdict has **not already been promoted** (per-head dedup);
- the pass was **verified** (the verify pass ran) over **complete** coverage (every
  finder lane delivered a full pass — none timed out, came back blocked or without its
  status line, or found its structural engine down) — an incomplete pass is "nobody
  looked", not "nothing there", and holds;
- **CI is terminal-green** — checks unknown, pending, or failing all hold; and
- there are **zero unresolved review threads**.

Every unknown (unreadable checks, unreadable threads, no verdict) falls through to a
typed hold and the sweep re-evaluates next pass. Even once armed, native GitHub
auto-merge still waits on branch protection and required status checks (including
`QA panel` / `protoReview` where required) before it merges — arming it is not
merging it.

### Holding a PR that is reviewed but must not ship yet

There is no separate "reviewed but held" state today. If a PR is complete and wants
scrutiny but must **not** land yet — it shares a file with another PR in flight, waits
on a sibling landing first, a release window, or a coordinated rollout — **keep it in
draft** (or do not undraft it) until that dependency or sequencing is resolved. A
draft is skipped by the panel and can never promote, so draft is the safe hold:
nothing arms auto-merge while the PR stays draft. Undraft only once it is genuinely
clear to ship.

## Requirements

- protoAgent ≥ the version carrying the findings `source` field (see the manifest pin).
- `git`, `gh` (authenticated, or `GITHUB_TOKEN`/`GH_TOKEN`), and the `clawpatch` CLI
  (`npm i -g @protolabsai/protopatch`).
- Gateway credentials in the host env: `GATEWAY_API_KEY` or `OPENAI_API_KEY`
  (+ `OPENAI_BASE_URL` / `pr_reviewer.gateway_base_url` for a non-default gateway).

## Config (env fallbacks)

Every key below is resolved **live on each use** (v0.14.0, issue #11) through the host's
`live_config` view — editing `repos` or flipping `shadow_mode` in Settings takes effect
without a restart. They previously snapshotted at boot, so an operator saw *"config
saved / reloaded"* and got a silent no-op; believing you are formal-blocking a repo you
are not is the dangerous direction. (`cooldown_s` is the exception — the chokepoint owns
in-flight state and can't be rebuilt per read.)

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
| `PR_REVIEWER_FINDER_TIMEOUT` | `pr_reviewer.finder_timeout_s` | recipe default (`900`) | Seconds each parallel finder may run before the engine degrades it to a Gap. **Calibrate it to your model** — just above the slow finders' p95 in the telemetry `step_s` — because a budget tuned on one lane silently truncates productive finders on a slower one (#93). Clamped a minute under `panel_attempt_timeout`. Needs protoAgent ≥ 0.170.0. |
| `PR_REVIEWER_VERIFY_RERUNS` | `pr_reviewer.verify_reruns` | `1` | How many times a verifier that answered `nothing-to-verify` over a synthesis carrying findings is re-run **alone** — seeded with the finders' and synthesizer's outputs, seconds instead of a fresh panel (#167). A round that stays contradicted posts `verified=false` and holds, as before. `0` disables. Needs a host whose runner takes `seed_outputs` (protoAgent#3571); on an older host the contradiction is only counted (`verify-contradicted` telemetry). |
| `PR_REVIEWER_PANEL_ATTEMPT_TIMEOUT` | `pr_reviewer.panel_attempt_timeout` | `1800` | Seconds one panel attempt may run. Only the finders carry a step timeout, so a hung verifier/synthesis step used to hang the round. Past the budget the attempt is cancelled and counts as failed: retried, then concluded on the PR as **"QA panel timed out"**. |
| `PR_REVIEWER_ROUND_TIMEOUT` | `pr_reviewer.round_timeout` | every attempt + 600 | Backstop for a whole round (every attempt plus the GitHub calls around them). Defaults to `(panel_retries + 1) × panel_attempt_timeout + 600`, so it never cuts a legitimate retry short. |
| `PR_REVIEWER_BACKFILL_PER_PASS` | `pr_reviewer.backfill_per_pass` | `2` | Reviews the sweep may backfill per pass, across all repos. `0` disables backfill. |
| `PR_REVIEWER_SUMMON` | `pr_reviewer.summon` | `true` | The comment-command surface (`@vera review` / `pause` / `resume` / `help`) **and** the pause check on the automated path. `false` costs nothing for a repo that never wants comment-driven behaviour. |
| `PR_REVIEWER_EVIDENCE_GROUNDING` | `pr_reviewer.evidence_grounding` | `true` | A finding whose quoted code appears nowhere in the cited file at the reviewed head (nor in this PR's patch for it) is annotated `uncertain` — it still posts, it just can't carry a FAIL. Fails open on an unreadable blob or unquotable evidence. |
| `PR_REVIEWER_HOLD_UNEXPLAINED_CLEARANCE` | `pr_reviewer.hold_unexplained_clearance` | `true` | A zero-finding PASS does not dismiss our standing block when a prior round confirmed a blocker/major it neither reports nor explains. A second consecutive clean PASS lifts it. `false` restores the old always-dismiss behaviour. |
| `PR_REVIEWER_CONVERGENCE_ROUNDS` | `pr_reviewer.convergence_rounds` | `3` | The round from which an all-minor, all-in-delta WARN retires to PASS-with-notes. `0` disables the rule — the panel keeps re-reviewing rather than ever floor a minor. |
| `PR_REVIEWER_QA_CHECK` | `pr_reviewer.qa_check` | `true` | Publish the **`QA panel` check run** (below). Rides the promotion-owner gate, so a shadow repo publishes nothing. `false` keeps approve-on-green without the check. |
| `PR_REVIEWER_REGATE` | `pr_reviewer.regate` | `true` | Master switch for step 2 below. `false` stops arming blocks while KEEPING the formal seat, promotion and backfill — the lever to pull when the panel is emitting false FAILs. |

### The `QA panel` check run — the verdict as an enforceable gate

An App's **approval never satisfies a required approving review**: GitHub counts
approvals from reviewers with write access, and an App is not one (its reviews carry
`author_association: NONE`). So on a repo that requires review, the panel could approve
and the merge stayed `BLOCKED` — the verdict had no way to gate anything.

A **check run** from the same App is a first-class required status, so the panel now
publishes one, named **`QA panel`**, driven by the same decision as approve-on-green:

| The panel's state | The check |
|---|---|
| Clear verdict, findings resolved (or already promoted) | ✅ success |
| Findings still open — unresolved review threads | ❌ failure |
| `FAIL` verdict standing against this head | ❌ failure |
| No verdict yet / stale head / incomplete pass | ⏳ in progress |
| CI pending, red, or unreadable | ⏳ in progress — CI already blocks; we don't say it twice |
| PR closed or merged while the check was still in progress | ⚪ neutral — every wait above ends with the PR; a run that already concluded is left as it stands (#153) |

Note the WARN rule is unchanged: a WARN whose threads are all resolved goes **green**.
What blocks is feedback nobody addressed.

**To make it enforce**, add `QA panel` to the branch's required status checks (ruleset →
*Require status checks to pass*). Everything inherits it — a human's PR, and
projectBoard-plugin's auto-merge, which gates on `mergeStateStatus`.

Requires the App installation to carry **Checks: read & write**; without it the write
logs a warning naming that permission and the panel otherwise behaves as before. The
check is written only where this agent owns promotion — a *required* check that nobody
drives would block every merge in that repo forever.

### The `protoReview` check run — verdicts you can require on every merge

`QA panel` above answers *"is this head cleared for merge?"*, and only where this agent
**owns promotion** — so a shadow deployment publishes nothing, and an **exhausted** panel
(no verdict) leaves it sitting *in progress*, indistinguishable from a head still under
review. That is exactly the gap that let **12 PRs merge unreviewed** in one deployment
(lifetime 29): the verdict was an *advisory* GitHub review, and an exhausted panel that
posts no review is indistinguishable from an approved one.

**`protoReview`** closes it. It is a check run of a different kind — it tracks the
**dispatch lifecycle itself**, not the promotion decision:

| Moment | The check |
|---|---|
| Panel dispatched (past the drop/skip gates) | ⏳ in progress |
| `PASS` / `WARN` verdict posted | ✅ success |
| `FAIL` verdict posted | ❌ failure |
| **Panel exhausted / crashed** (no verdict) | ❌ failure — *the key new signal* |
| Panel delivered **no findings payload** — absent, not `[]` (no verdict) | ❌ failure |
| Verdict produced but the post was refused | ❌ failure (not left dangling) |
| Dropped (draft, closed, allowlist miss) | *no check — the panel never ran* |

Because the **same review that opens the check also concludes it**, `protoReview` never
dangles — so, unlike `QA panel`, it is published for **every** panel that runs regardless
of shadow mode or promotion ownership, and is safe to require everywhere. Its `head_sha`
is resolved **server-side** from the PR (never the webhook's ref), and the concluding
`output.summary` carries the verdict text or the exhaustion reason.

**To make it enforce**, add **`protoReview`** to the branch's required status checks
(ruleset → *Require status checks to pass*). An exhausted panel then leaves a **red X**
that blocks the merge, instead of the silence a merge would sail straight through — a
human's PR and projectBoard-plugin's auto-merge (which gates on `mergeStateStatus`) alike.

**Re-running it.** A red `protoReview` is cleared by pushing a fix (a new head re-triggers
the panel), or by clicking **Re-run** on the check itself — GitHub delivers that as a
`check_run` *rerequested* event, which re-runs the panel with the same force posture as a
manual summon. That needs the App subscribed to the **`check_run`** event (like
`issue_comment` for summons); without it the push path still works.

**Clearing it by resolving the threads.** When the check reads *"N unresolved review
threads"*, resolving them re-publishes the gate directly — GitHub delivers that as a
`pull_request_review_thread` *resolved* event and the handler re-runs `evaluate_promotion`
(a state re-read and a check publish; no panel, no model call). Re-opening a thread takes
it back to red the same way.

⚠️ **This needs the App subscribed to the `pull_request_review_thread` event.** Without it
the check still asks you to resolve the threads and doing so will not clear it — the exact
contradiction issue #111 was filed for, which cost protoAgent#3415 ten hours of stale red
and burned board coder attempts against a signal no code change could fix.

Requires the App installation to carry **Checks: read & write**. Without it the create
logs a warning naming that permission and the whole lifecycle no-ops — the review still
posts as before (bookkeeping must never cost the verdict).

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

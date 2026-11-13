# Configuration

Everything lives under `pr_reviewer:` in `langgraph-config.yaml` (or Settings → Plugins → PR
reviewer for the fields it exposes). Defaults are the manifest's; your config overlays them.

## How a value is resolved

Operator-tunable keys read **config first, env as a fallback**, so headless config-as-code
deployments (where the config volume is seed-once) can set them in the compose env, which is
re-applied on every roll. A config key that is **present** wins; the env only fills an unset or
empty key. For `repos` and the booleans, the seed config ships `repos: []` / the default boolean,
so an env value applies only where noted. When in doubt, set the config key.

Reads are **live**: every key is resolved on each use through the host's `live_config` view
(issue #11). Editing `repos` or flipping `shadow_mode` in Settings takes effect without a restart.
They previously snapshotted at boot, so an operator saw "config saved" and got a silent no-op;
believing you are formal-blocking a repo you are not is the dangerous direction.

**Exceptions that need a restart or re-registration:** `cooldown_s` (the chokepoint owns in-flight
state and is not rebuilt per read; a config reload rebuilds it), `max_concurrent_panels` (sizes the
shared panel queue at boot), and `sweep_interval_s` (read when the sweep surface starts).

**Secrets** (`webhook_secret`, `app_private_key`) are routed to the untracked secrets overlay,
never the main YAML.

## Scope and mode

| Key | Default | Env | |
|---|---|---|---|
| `repos` | `[]` | `PR_REVIEWER_REPOS` | Repos (`owner/name`) the dispatcher manages. Env is comma, space or newline separated; config wins only when non-empty. **Empty in both = every repo the App installation covers.** |
| `default_repo` | `""` | | Repo the `protopatch_review` tool falls back to when `repo` is omitted and no round binds one |
| `shadow_mode` | `true` | `PR_REVIEWER_SHADOW_MODE` | `true`: every verdict posts as a `COMMENT` review and never blocks. A present config bool (including `false`) wins over env |
| `promotion_owner` | `false` | `PR_REVIEWER_PROMOTION_OWNER` | `true` (and `shadow_mode: false`): this agent owns approve-on-green and the `QA panel` check. Two promoters racing double-merge |
| `qa_check` | `true` | `PR_REVIEWER_QA_CHECK` | Publish the [`QA panel`](checks.md#qa-panel) check run. Rides the promotion-owner gate. `false` keeps approve-on-green without the check |
| `regate` | `true` | `PR_REVIEWER_REGATE` | Arm a block for a standing FAIL once CI is terminal. `false` stops arming blocks while keeping the formal seat, promotion and backfill: the lever to pull when the panel emits false FAILs |
| `summon` | `true` | `PR_REVIEWER_SUMMON` | The [comment-command surface](usage.md#comment-commands) and the pause check on the automated path |
| `summon_handle` | `vera` | | The `@handle` commands answer to, in addition to the reviewer's own login |
| `epic_attestation` | `true` | `PR_REVIEWER_EPIC_ATTESTATION` | [Review an `epic/*` PR by its residual only](epic-attestation.md) |
| `incremental_review` | `false` | `PR_REVIEWER_INCREMENTAL_REVIEW` | [Re-review only the files that changed since the last round](incremental-review.md), carrying that round's findings on the rest. Off: every round is byte-for-byte a full review |
| `incremental_review_repos` | `[]` | `PR_REVIEWER_INCREMENTAL_REVIEW_REPOS` | With `incremental_review` on, the `owner/name` repos it applies to (the #313 A/B). A YAML list or a comma/space separated string; matching is case-insensitive. Empty: the flag governs every repo. A repo not listed takes exactly the flag-off path. Read each round, so a change needs no restart |
| `deep_finder_model` | `""` | `PR_REVIEWER_DEEP_FINDER_MODEL` | The model the correctness and removed-behavior lanes run on (the `review-finder-deep` subagent; #318), e.g. `anthropic-oauth:claude-sonnet-5-5`. Empty: the same model as the other finders. Read each round, so a change needs no restart. An `anthropic-oauth:` model with no readable Claude token stays on the default |
| `deep_finder_cooldown_s` | `1800` | `PR_REVIEWER_DEEP_FINDER_COOLDOWN_S` | After a deep lane fails on `deep_finder_model`, how long both deep lanes run on the default model. The failed attempt's retry already runs there, so a rate limit or lost token costs one attempt, not the round |
| `reviewers_of_record` | `[]` | `PR_REVIEWER_REVIEWERS_OF_RECORD` (JSON) | PRs another reviewer owns (#318): `[{label, status, grace_s, name}]`. On a PR carrying `label`: when that reviewer's `status` is `success` at the head, post an attested PASS (no panel). When it is `pending`, `failure` or `error`, hold and post nothing; a FAIL would be read back by the board as this panel failing its head. When it has posted no status after `grace_s` (default 1200), or its `pending` is older than `pending_grace_s` (default 3600, a gate that died mid-review), review normally. An operator summon always runs the panel. Board PRs: `[{"label": "reviewer-of-record:board", "status": "board/review-gate", "name": "project board"}]` |
| `allow_self_review` | `false` | | **Testing only.** Lifts the never-review-your-own-PR rail |
| `viewer_login` | `""` | `PR_REVIEWER_VIEWER_LOGIN` | Our own login, for the self-review rail. **Required on App auth** (e.g. `my-reviewer[bot]`); auto-detected with a PAT |

## GitHub identity

| Key | Default | Env | |
|---|---|---|---|
| `webhook_secret` | `""` | `PR_REVIEWER_WEBHOOK_SECRET` | HMAC secret. **Empty means every delivery is a 403** |
| `app_id` | `""` | `PROTOREVIEW_APP_ID` | GitHub App ID. With the private key, a refresher keeps `GH_TOKEN` a fresh installation token (hourly expiry). Empty = bring your own `GH_TOKEN` |
| `app_private_key` | `""` | `PROTOREVIEW_APP_PRIVATE_KEY` | The App's PEM |
| `installation_id` | `""` | `PROTOREVIEW_APP_INSTALLATION_ID` | Optional; auto-discovered |

## Panel pacing and timeouts

| Key | Default | Env | |
|---|---|---|---|
| `cooldown_s` | `30` | | Chokepoint window per `repo#pr@sha`; collapses webhook bursts |
| `max_concurrent_panels` | `3` | `PR_REVIEWER_MAX_CONCURRENT_PANELS` | Cross-PR cap on panels running at once. Sized so the peak (× 5 finders) fits one gateway lane; the excess **queues**. A 14-PR burst measured 70 concurrent LLM calls without it |
| `max_rounds` | `6` | `PR_REVIEWER_MAX_ROUNDS` | Complete push-triggered reviews of one PR before further pushes are suppressed. `0` disables the cap |
| `max_rounds_cooldown` | `7200` | `PR_REVIEWER_MAX_ROUNDS_COOLDOWN` | Seconds before the cap resets without an explicit trigger |
| `panel_retries` | `1` | `PR_REVIEWER_PANEL_RETRIES` | Re-runs of a recipe whose panel reported a failed step, before escalation. `0` gives up on the first failure |
| `finder_timeout_s` | `0` (recipe: 900) | `PR_REVIEWER_FINDER_TIMEOUT` | Seconds each parallel finder may run before the engine degrades it to a Gap. **Calibrate to your model**: just above the slow finders' p95 in the telemetry `step_s`. Clamped a minute under `panel_attempt_timeout`. Needs protoAgent ≥ 0.170.0 |
| `synthesize_timeout_s` | `0` (recipe: 300) | `PR_REVIEWER_SYNTHESIZE_TIMEOUT` | Seconds the synthesize **and** report steps may each run. A timed-out step degrades to empty output, so it can never post a clean PASS: the round is voided, retried, then concluded visibly. Clamped to 900 |
| `verify_timeout_s` | `0` (recipe: 300) | `PR_REVIEWER_VERIFY_TIMEOUT` | Seconds the verify step may run. Same degradation. Clamped to 900 |
| `panel_attempt_timeout` | `1800` | `PR_REVIEWER_PANEL_ATTEMPT_TIMEOUT` | Budget for **one** panel attempt: the backstop for a hang outside the step timeouts. **Hard ceiling 4200**; a larger value is clamped with a warning naming the key. Size it as `finder_timeout_s` + 900 for the tail steps, or a slow finder leaves the tail no room. Past the budget the attempt counts as failed |
| `round_timeout` | attempts + 600 | `PR_REVIEWER_ROUND_TIMEOUT` | Backstop for a whole round: `(panel_retries + 1) × panel_attempt_timeout + 600`. **Hard ceiling 9000** |
| `backfill_per_pass` | `2` | `PR_REVIEWER_BACKFILL_PER_PASS` | Reviews the sweep may backfill per pass, across all repos. `0` disables backfill |
| `exhaustion_backoff` | `900` | `PR_REVIEWER_EXHAUSTION_BACKOFF` | Seconds the sweep waits before backfilling a head whose panel just exhausted. Doubles with each further exhaustion of that head. A new head, a push or a summon is never held. `0` disables the backoff |
| `exhaustion_backoff_max` | `14400` | `PR_REVIEWER_EXHAUSTION_BACKOFF_MAX` | Ceiling on that doubling backoff |
| `sweep_interval_s` | `180` | | Sweep cadence |
| `supersede_poll_s` | `30` | `PR_REVIEWER_SUPERSEDE_POLL_S` | Seconds between in-step checks for a superseded head (0 to 600; `0` disables) |
| `summon_in_flight_grace_s` | `10` | `PR_REVIEWER_SUMMON_IN_FLIGHT_GRACE_S` | How long a summon waits for the in-flight slot before answering `in-flight` (0 to 120) |
| `diff_char_budget` | `200000` | `PR_REVIEWER_DIFF_CHAR_BUDGET` | The size above which the engine truncates the diff the finders read. Used only to *detect* truncation, so an ungroundable absence claim never gates |
| `gateway_degraded_retries_5m` | `6` | `PR_REVIEWER_GATEWAY_DEGRADED_RETRIES_5M` | Model/SDK retries in 5 minutes above which the gateway reads as degraded |
| `gateway_degraded_escalate_s` | `900` | `PR_REVIEWER_GATEWAY_DEGRADED_ESCALATE` | Seconds degraded before **one** warning is logged |

## Verification

| Key | Default | Env | |
|---|---|---|---|
| `verify_reruns` | `1` | `PR_REVIEWER_VERIFY_RERUNS` | Times a verifier that answered `nothing-to-verify` over findings is re-run **alone**, seeded with earlier outputs. `0` disables. Needs a host whose runner takes `seed_outputs` (protoAgent#3571); on an older host the contradiction is only counted (`verify-contradicted`) |
| `verify_fallback_panel` | `true` | `PR_REVIEWER_VERIFY_FALLBACK_PANEL` | After a re-run is still contradicted, run one fresh panel before posting a held PASS. Off posts the held PASS as before |
| `prior_recheck` | `true` | `PR_REVIEWER_PRIOR_RECHECK` | Re-verify prior blocker/majors at the new head, alone, before re-asserting them. See [verdicts](verdicts.md#dispositions-and-the-clearance-hold) |
| `finding_accounting_gate` | `losses` | `PR_REVIEWER_FINDING_ACCOUNTING_GATE` | Which [raw finding accounting](verdicts.md) gaps decide a structural round. `losses`: **integrity** gaps — a finding one stage carried that the next neither carries nor disposes of, an unreadable report with findings in flight, a severity/file/line move or emptied evidence with no reason, a flipped / resurrected / unreasoned disposition, a verifier that left survivors unannotated, a dispatcher omission, stale or changed seeded inputs — make the round incomplete **and** unverified; a finder lane's **coverage** gap (raw input cut short, malformed, degraded) makes it incomplete only, since a verify re-run cannot recover a lane; **id bookkeeping** (missing or typo'd ids, an undisposed restatement) decides nothing. `all` is the #329 behaviour — any gap holds (on 40 live rounds 20 held, none for a lost finding: #354). `off` decides nothing. The trail is computed, emitted and rendered in every mode |
| `evidence_grounding` | `true` | `PR_REVIEWER_EVIDENCE_GROUNDING` | Quoted code not found at head marks the finding `uncertain` |
| `absence_search` | `true` | `PR_REVIEWER_ABSENCE_SEARCH` | `git grep` the head for what an absence claim says is missing. Needs grounding on |
| `absence_search_budget_s` | `120` | | Wall-clock bound on that search |
| `absence_search_clone` | `true` | | `false` uses only a checkout the structural pass already made, never a fresh clone |
| `repo_search` | `true` | `PR_REVIEWER_REPO_SEARCH` | Back the lanes' `search_pr_repo` / `list_pr_repo_files` with a checkout of the PR head (issue #311). `false` makes both return a `SEARCH ERROR` |
| `repo_search_checkout_wait_s` | `90` | | How long one search waits for the head checkout to finish building before answering `SEARCH ERROR` (the clone carries on in the background) |
| `evidence_guard` | `true` | `PR_REVIEWER_EVIDENCE_GUARD` | Demote a `confirmed` that quotes no code. Needs grounding on |
| `hold_unexplained_clearance` | `true` | `PR_REVIEWER_HOLD_UNEXPLAINED_CLEARANCE` | A zero-finding PASS does not dismiss our standing block when a prior confirmed blocker/major vanished unexplained. `false` restores always-dismiss |
| `convergence_rounds` | `3` | `PR_REVIEWER_CONVERGENCE_ROUNDS` | The round from which an all-minor, all-in-delta WARN retires to PASS-with-notes. `0` disables |
| `llm_refutation_memory` | `true` | `PR_REVIEWER_LLM_REFUTATION_MEMORY` | Remember refuted LLM-lane claims per repo |
| `refutation_ttl_days` | `14` | | How long a refuted claim is remembered, per repo (protoPatch and LLM lanes) |

## Structural engine

See [structural](structural.md) for what each does.

| Key | Default | |
|---|---|---|
| `clawpatch_bin` | `clawpatch` | The protoPatch CLI (`npm i -g @protolabsai/protopatch`) |
| `time_budget_s` | `600` | Hard wall-clock budget for the whole pass (SIGKILL past it). Covers up to two clawpatch runs |
| `structural_jobs` | `4` | Concurrent feature reviews (`--jobs`). `0` = clawpatch's default. Clamped to 10. If you lower it, raise `time_budget_s` |
| `structural_max_features` | `16` | Features reviewed per pass, touched features first, then by changed lines. `0` = no cap. A touched feature left out is a coverage gap |
| `structural_plan` | `true` | `false` lets `clawpatch ci --since` pick the features (the rollback) |
| `structural_symbol_scope` | `true` | Rank features the diff touches ahead of siblings that only share a changed file, and stop counting left-out siblings as a coverage gap ([details](structural.md#planning)). Python only. `false` ranks by file ownership alone (the rollback) |
| `structural_result_cache` | `true` | Serve a planned feature from its cached result when its owned+context file blobs at head, the engine version, the model and the plugin version are unchanged since a COMPLETED review ([details](structural.md#result-cache)). Partial, timed-out or failed features are never cached; any cache fault re-runs the feature. `false` always runs every planned feature |
| `model` | `""` | `clawpatch --model` override; empty = its default (`protolabs/smart`) |
| `gateway_base_url` | `""` | Exported as `OPENAI_BASE_URL` to clawpatch; empty = its default |
| `lint_check` | `true` | Re-run a cited ruff rule with the repo's pinned ruff |
| `lint_check_budget_s` | `90` | Cap for the whole check (install + runs) per pass |
| `lint_install_timeout_s` | `60` | Cap for one ruff install |
| `lint_run_timeout_s` | `20` | Cap for one ruff run |
| `lint_tools_dir` | `<state home>/tools` | Root under which pinned ruff versions are installed (`<root>/ruff/<version>`) |

## Storage

| Key | Default | Env | |
|---|---|---|---|
| `checkout_root` | `""` | | Empty = `~/.protoagent/pr-reviewer/checkouts` |
| `state_root` | `""` | `PR_REVIEWER_HOME` | Empty = `~/.protoagent/pr-reviewer`. The state home also holds [telemetry](operations.md#telemetry) and per-repo clawpatch state |
| `checkout_ttl_s` | `3600` | | Cache entry freshness window (about a token lifetime) |
| `checkout_max_entries` | `50` | | LRU prune cap |
| `checkout_max_bytes` | `5000000000` | | LRU prune cap (5 GB) |

## Settings UI

These are editable in the console: `repos`, `shadow_mode`, `promotion_owner`, `webhook_secret`,
`app_id`, `app_private_key`, `default_repo`, `viewer_login`, `time_budget_s`, `structural_jobs`,
`structural_max_features`, `structural_plan`, `model`. Everything else is config-file or env only.

The manifest also declares `public_paths: [/plugins/pr-reviewer/webhook]` (the HMAC is the auth)
and `capabilities` (network: `api.github.com`, `github.com`; filesystem: the state home). The
capabilities block is shown in the console but not yet enforced.

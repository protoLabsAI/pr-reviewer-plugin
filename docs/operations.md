# Operations

## Telemetry

Every decision writes a JSON line to `<state home>/telemetry/<day>.jsonl` (state home is
`state_root`, else `PR_REVIEWER_HOME`, else `~/.protoagent/pr-reviewer`). Event kinds you will
meet most:

| Event | |
|---|---|
| `reviewed` | A round posted. Carries the verdict, recipe, `complete`, `verified`, per-step `step_s` / `timings`, `structural_*` fields, `coverage_capped`, `brief_found`, `findings` counts |
| `drop` | The event was not acted on. `reason` is typed: `bad-signature`, `not-a-pr-event`, `malformed-payload`, `pr-not-eligible` (with `why`), `in-flight`, `round-timeout`, `superseded-merged`, and others |
| `reaffirm` / `reaffirm-diff` / `reaffirm-miss` / `reaffirm-recorded` | Head or diff unchanged; verdict carried without a panel, or the attempt declined |
| `promotion` / `regate` | A promote or re-gate decision, with its `hold:*` |
| `exhaustion` | Panel ended with no verdict. `undelivered: [...]` names lanes. `causes` lists per-attempt arrays of `{stage, cause, seconds}`: cause is `timeout`, `empty`, `no-array`, or `failed`; seconds is null when the host supplies no timing (an attempt that crashed before returning has an empty array) |
| `queued` / `superseded` | Queue wait; a round stood down for a newer head |
| `structural_plan` | Per-pass feature plan and outcomes |
| `counter_evidence` | `count`, `chars` of dispute comments passed to a re-review |
| `fail_superseded` | A refutation replaced a FAIL |
| `in_flight_reclaimed` | An abandoned slot was reclaimed |

To count structural gaps, count either `structural_unavailable` (delivered nothing) or
`structural_partial` (cut short), not just the first.

## The eval report

`GET /eval`, `GET /eval/three-way` and the `pr_review_eval` agent tool render it: completion rate,
verdict and recipe mix, latency percentiles with a **p50 per step** and a slowest-step histogram
("the panel is slow" is not actionable across nine steps), typed drops, promotion decisions, and
the three-way comparison of our verdicts against Quinn's and CodeRabbit's on the same PRs.

Use it to decide when to leave shadow mode.

## Queue

`GET /queue` shows what is running and waiting, with ETAs ([api](api.md#get-queue)). A PR waiting in
line otherwise reads "no QA panel verdict" in `Review at head` with nothing saying it is queued.

## Replay

Run the panel against a **pinned checkout and diff**, findings to JSON instead of GitHub, to A/B
models. It reuses the exact live functions (same finders, verify and guards), not a fork. The
model is a per-run gateway alias (`protolabs/fast` vs `protolabs/smart`); that is the whole knob.
It is side-effect-free: it reads blobs and diffs and never writes.

Truncation is first-class: a model that burns its budget on hidden reasoning and emits nothing
(`findings=[] & truncated=true`) is distinguished from a clean pass (an emitted `[]`), so a
truncated run is not scored as "found nothing".

```bash
python -m pr_reviewer.replay_cli --manifest replay_manifest.jsonl --model protolabs/fast
# or, in-process against a live host:
curl -X POST .../api/plugins/pr-reviewer/replay -d '{"manifest": [...], "model": "protolabs/fast"}'
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Webhook returns 403 | No `webhook_secret`, or it does not match the App's. Fail-closed by design |
| Commands (`@vera review`) do nothing, no reply | The App is not subscribed to `issue_comment`. Check [`/summon/health`](api.md#get-summonhealth) |
| Resolving threads does not clear the check | The App is not subscribed to `pull_request_review_thread` |
| **Re-run** on `protoReview` does nothing | The App is not subscribed to **Check run** |
| `QA panel` / `protoReview` never appears | App lacks **Checks: read & write**; the log names the permission. `QA panel` also needs `promotion_owner` |
| Reviewer reviews its own PRs | `viewer_login` unset on App auth |
| Every structural review is a gap | Check `structural_reason`: `not-installed` (no `clawpatch`), `no-credentials`, `budget-timeout`, `exit-4:gateway-*` |
| Structural passes time out after you lowered `structural_jobs` | Raise `time_budget_s`: a pass takes `ceil(features / jobs)` waves |
| A PR sits at `hold:unverified` | The verifier flaked. The sweep runs one fresh round; then summon `@vera review` or push |
| A PR never gets reviewed | Draft, not in `repos`, or paused. A backfill picks up the rest within a sweep or two |
| A panel hangs | Bounded by `panel_attempt_timeout` and `round_timeout`; the slot is reclaimed past the round bound + 10 min |
| A `FAIL` you believe is wrong, head unchanged | [Dispute it](usage.md#disputing-a-fail-on-an-unchanged-head) |
| A `FAIL` is a false positive pattern | Flip `regate: false` while you investigate; this stops arming blocks without losing the seat |

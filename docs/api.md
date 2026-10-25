# HTTP API

Two routers. The **public** surface carries no bearer, because GitHub cannot send one; its auth is
the HMAC. The **operator** surface is behind the host's bearer auth.

The drift test fails if a route is added, renamed or removed without this page changing.

## Public

### `POST /plugins/pr-reviewer/webhook`

GitHub App deliveries. Verified against `X-Hub-Signature-256` with `webhook_secret`; a missing
secret or a bad signature is a **403** (and a `bad-signature` drop in telemetry). Handles
`pull_request`, `issue_comment`, `check_run` and `pull_request_review_thread`; any other event
(including `pull_request_review_comment`) returns
`{"ok": true, "dispatched": false, "reason": "not-a-pr-event"}`. Dispatch is asynchronous: the
response is `{"ok": true, "dispatched": true}` as soon as the work is queued.

## Operator

Prefix: `/api/plugins/pr-reviewer`.

### `POST /dispatch`

Manual dispatch. The same chokepoint and everything else as a webhook.

```json
{"repo": "owner/name", "pr": 12, "sha": "optional"}
```

Returns `{"repo", "pr", "outcome"}`. `400` without `repo` and `pr`.

### `POST /promote`

Evaluate (and, when this agent owns promotion and is not in shadow mode, apply) approve-on-green
for one PR.

```json
{"repo": "owner/name", "pr": 12}
```

Returns `{"repo", "pr", "decision"}`, where `decision` is `promote` or a [`hold:*`](promotion.md#hold-reasons).

### `GET /summon/health`

Can a comment command reach us? Reads the App's subscribed events with the App JWT and reports
which of the required events (`issue_comment`, `pull_request_review_comment`) are missing.

```json
{"subscribed": [...], "required": [...], "missing": [], "summon_reachable": true, "note": "..."}
```

When the App credentials are not set, or `GET /app` fails, the answer is **unknown**:
`summon_reachable: null`. It does not say false. A diagnostic that cannot tell "no" from "I don't
know" is worse than none. Verify by commenting `@vera help` on a PR.

### `GET /queue`

Panel throughput from in-memory state only (no GitHub, no network). All three panel paths (webhook,
summons and check re-requests, sweep backfill) share one `PanelQueue` of `max_concurrent_panels`
slots.

```jsonc
{
  "generated_at": 1727470000.0,
  "limit": 3,
  "running": [
    { "repo": "o/r", "pr": 12, "head": "abc123…", "kind": "webhook",
      "started_at": 1727469900.0, "elapsed_s": 100.0,
      "attempt": 1, "phase": "verify", "model_retries": 0 }
  ],
  "queued": [
    { "repo": "o/r", "pr": 15, "head": "def456…", "kind": "summon",
      "enqueued_at": 1727469990.0, "position": 1, "eta_start_s": 45.0 }
  ],
  "depth": 1,
  "p50_panel_s": 180.0,
  "p90_panel_s": 300.0,
  "gateway_degraded": false,
  "gateway_retry_rate_5m": null,
  "oldest_queued_s": 10.0
}
```

- `kind`: `webhook` | `summon` | `sweep-backfill` | `verify-retry`.
- `phase`: `finders` | `structural` | `verify` | `synthesize` | `posting`.
- `position` is the FIFO place (1 = next). `depth` is `len(queued)`.
- ETAs for position *k* are the *k*-th smallest of `max(0, p50 − elapsed_s)` across running panels,
  plus `(⌈k/limit⌉ − 1) × p50` for each later wave. They use **p90 when `gateway_degraded`**, and are
  `null` until there is duration data.

**Per-PR lookup:** `GET /queue?repo=owner/name&pr=N`

```json
{ "state": "running", "position": 1, "eta_start_s": 45.0, "eta_verdict_s": 225.0, "head": "def456…" }
```

`state` is `running` | `queued` | `idle`. `eta_verdict_s = eta_start_s + p50`. A running PR reports
`eta_start_s: 0`; an idle one reports nulls.

### `POST /replay`

Run the panel on pinned rounds off the live-PR path, findings to JSON. Side-effect-free (no GitHub
writes). It must run in-process because replay needs the live workflow runner and the minted App
token. See [operations](operations.md#replay).

Body: `{"manifest": [row, ...]}`, `{"row": row}` or a bare row, with optional `model`, `trials`, `stamp` and `include_raw` applied to each. Returns `{"runs": [...]}`. `503` without a live workflow runner; `400` if `manifest` is not a list.

### `GET /eval`

The telemetry summary as JSON.

### `GET /eval/three-way`

The stage-1 comparison: the telemetry summary, per-PR rows (ours vs Quinn vs CodeRabbit), and the
rendered markdown report.

# How it works

A review is one pass through a fixed pipeline. Nothing in it asks the model to decide
whether the review is trustworthy: that is code.

```
GitHub App webhook ──► chokepoint ──► trigger ──► panel ──► guards ──► verdict ──► post ──► checks
   (HMAC verified)     (dedup, caps)  (which      (find →    (confine,   (PASS/WARN/  (review +   (QA panel,
                                       recipe)     dedup →    ground,     FAIL)        marker)     protoReview)
                                                   verify)    converge)
```

## 1. Events in

`POST /plugins/pr-reviewer/webhook` is the only public route. The HMAC signature
(`X-Hub-Signature-256`) is the auth; no secret configured means every delivery is a 403.

| Event | Does |
|---|---|
| `pull_request` `opened` / `synchronize` / `reopened` / `ready_for_review` | Starts a review of that head |
| `issue_comment` | Parses an `@vera <verb>` [command](usage.md) in the PR conversation |
| `check_run` `rerequested` | **Re-run** on `protoReview` is a forced review |
| `pull_request_review_thread` | A thread resolving or reopening re-publishes the [check](checks.md) |

Everything else is dropped with a typed reason in [telemetry](operations.md#telemetry).

## 2. The chokepoint

Before a panel is spent, the event has to survive:

- **Eligibility.** Closed, **draft** and locked PRs are dropped (`pr-not-eligible`). A repo not in
  `repos` is dropped. The plugin never reviews its own PRs (`viewer_login`).
- **Cooldown** (`cooldown_s`, default 30). A burst of events for one `repo#pr@sha` collapses to one.
- **One panel per PR.** If a round is running or queued for the PR, the new event is dropped
  `in-flight`. A *push* during a round also tells the running round its head is stale, and it
  stands down ([usage](usage.md#a-push-stops-the-old-round)).
- **Reaffirm.** If the exact head already carries a verdict, or the head changed but the
  base-to-head diff is byte-identical, the verdict is reaffirmed without a panel. An explicit
  `@vera review` overrides this.
- **Round cap.** After `max_rounds` (6) complete reviews of a PR, further pushes are suppressed
  for `max_rounds_cooldown` (2 h) with a comment saying so.
- **Cross-PR cap.** `max_concurrent_panels` (3) panels run at once across all PRs. Overflow
  **queues** rather than drops; see [`GET /queue`](api.md#get-queue).

## 3. Choosing the recipe

The trigger is computed server-side from the PR JSON and the full changed-path list
(ADR 0078 D2), not eyeballed from a truncated diff. A PR gets the **structural** recipe if it has:

- more than **3** changed files, or
- more than **120** changed lines (additions + deletions), or
- any path matching the sensitive-path pattern: `.github/`, Dockerfile / compose files, or a
  name containing `auth`, `session`, `token`, `crypto`, `oauth`, `password`, `secret`,
  `payment`, `billing`, `migrat`.

Otherwise it gets the lite four-finder `code-review` recipe from protoAgent core.

## 4. The panel

`workflows/code-review-structural.yaml` runs five finders as **one parallel stage**
(`max_concurrency: 5`):

| Finder | |
|---|---|
| correctness | Logic errors in the change |
| removed behavior | What the change deletes that something depended on |
| cross-file | Effects outside the diff |
| conventions | The repo's own rules |
| **structural** | [protoPatch](structural.md), a non-LLM-driven engine behind the `protopatch_review` tool, seated via the `structural-finder` relay subagent |

Then: **synthesize** (dedup and rank) → **verify** (an independent pass that tries to refute each
finding against the code) → **report** (a brief plus the canonical fenced findings JSON, with a
disposition for every prior blocker/major).

Every finding follows the ADR 0077 contract and carries a `source`. Protopatch findings get
exactly the same adversarial verify as the LLM finders. That is the point of seating the engine
as a finder rather than wiring it straight into a verdict.

Each step has a time budget (`finder_timeout_s`, `synthesize_timeout_s`, `verify_timeout_s`),
each attempt has `panel_attempt_timeout`, and a whole round has `round_timeout`. A timed-out
step degrades to *empty output*, which the plugin treats as a [coverage gap](verdicts.md#coverage-gaps),
never as a clean result.

## 5. Guards, then a verdict

The model's output is not the verdict. Code post-processes it: findings outside the diff are
dropped, quoted evidence is checked against the file, absence claims are grepped, lint claims
are re-run, repeat refutations are remembered, and review history decides whether a loop has
converged. All of that is in [verdicts](verdicts.md). `verdict_for` itself is a pure function
from findings to `PASS` / `WARN` / `FAIL`; history-aware rules layer on top and always fail
**closed**.

## 6. Post and publish

The verdict posts as a review with a visible findings table, a hidden machine-readable record,
and a marker. In [shadow mode](install.md#start-in-shadow-mode) it is a `COMMENT`; otherwise a
`FAIL` is `REQUEST_CHANGES`. **GitHub is the store** (ADR 0078 D5): rounds, dispositions, pause
state and promotion are all read back from markers on posted comments and reviews, so a restart
forgets nothing and there is no local database to drift.

[`protoReview`](checks.md#protoreview) tracks the dispatch lifecycle itself.
[`QA panel`](checks.md#qa-panel) publishes the promotion decision. The sweep then
[promotes](promotion.md) a clean PASS.

## Design rules worth knowing

These shape behavior you will otherwise find surprising:

- **Degrade, never raise.** A failing structural pass returns `PROTOPATCH UNAVAILABLE` with a
  prescribed Gap line. A raising panel step would void the whole review (ADR 0078 D3).
- **Server-side refs.** Head and base SHAs come from GitHub inside the tool, never from the model.
- **Absent is not empty.** `[]` means "looked, found nothing". No array at all means nothing
  arrived, and that is an incomplete round, never a PASS.
- **Fail closed, with one exception per direction.** Unreadable data holds a promotion, keeps a
  block, or refuses convergence. The only fail-*open* paths are the ones that can only add
  caution (for example grounding leaves a finding standing if the blob is unreadable).
- **A promotion is not a round.** An approve-on-green body carries a marker but no findings, so
  anything that recalls "the previous review" reads panel *rounds*, not the newest marker.

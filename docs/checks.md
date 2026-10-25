# Checks

An App's **approval never satisfies a required approving review**: GitHub counts approvals from
reviewers with write access, and an App is not one. So on a repo that requires review, the panel
could approve and the merge stayed `BLOCKED`. The verdict had no way to gate anything.

A **check run** from the same App is a first-class required status. The plugin publishes two, and
this repo's CI adds a third.

| Check | Answers | Written when |
|---|---|---|
| [`QA panel`](#qa-panel) | Is this head cleared for merge? | Only where this agent **owns promotion** |
| [`protoReview`](#protoreview) | Did the review pipeline run, and what did it conclude? | **Every** panel that runs, shadow or not |
| [`Review at head`](#review-at-head) | Is there a verdict for *this* commit? | A GitHub Actions job in the repo |

All need the App to carry **Checks: read & write**. Without it the write logs a warning naming
the permission and the review still posts; bookkeeping never costs the verdict.

## `QA panel`

Driven by the same decision as approve-on-green.

| The panel's state | The check |
|---|---|
| Clear verdict, findings resolved (or already promoted) | ✅ success |
| Findings still open: unresolved review threads | ❌ failure |
| `FAIL` standing against this head | ❌ failure |
| A panel round is running on this head | ⏳ in progress, "Panel reviewing this head". Never overwrites a run that already concluded |
| No verdict yet, or a stale head | ⏳ in progress |
| Clear verdict on an **incomplete pass** (a lane did not run) | ⚪ neutral. It passes a required check, but auto-approve is withheld until a complete pass. The verdict is WARN-capped for the same gap |
| CI pending, red or unreadable | ⏳ in progress. CI already blocks; we don't say it twice |
| PR closed or merged while still in progress | ⚪ neutral. A run that already concluded is left as it stands |

A WARN whose threads are all resolved goes **green**. What blocks is feedback nobody addressed.

**When it updates.** A round that ends re-publishes the check as soon as its slots free: a PASS
goes green in the moment the review posts, not on the next sweep (which can be ~9 minutes away). A
FAIL writes the check itself, inline. A review thread resolving or reopening refreshes it, and so
does another check on the head completing (CI going green is half of approve-on-green), which
needs the App subscribed to **Check run**. The sweep is the backstop.

**To make it enforce**, add `QA panel` to the branch's required status checks (ruleset → *Require
status checks to pass*). Everything inherits it, including a human's PR and projectBoard-plugin's
auto-merge, which gates on `mergeStateStatus`.

It is written **only where this agent owns promotion**, because a *required* check that nobody
drives would block every merge in that repo forever.

## `protoReview`

`QA panel` is silent when it should not be: a shadow deployment publishes nothing, and an
**exhausted** panel (no verdict) leaves it sitting in progress, indistinguishable from a head still
under review. That gap let 12 PRs merge unreviewed in one deployment. The verdict was an
*advisory* review, and an exhausted panel that posts no review looks the same as an approved one.

`protoReview` tracks the **dispatch lifecycle itself**, not the promotion decision.

| Moment | The check |
|---|---|
| Panel dispatched (past the drop and skip gates) | ⏳ in progress |
| `PASS` / `WARN` posted | ✅ success |
| `FAIL` posted | ❌ failure |
| **Panel exhausted or crashed** (no verdict) | ❌ failure. This is the key signal |
| Panel delivered **no findings payload**: absent, not `[]` | ❌ failure |
| Verdict produced but the post was refused | ❌ failure (never left dangling) |
| Superseded by a push, or the PR merged or closed | ⚪ neutral |
| Dropped (draft, closed, allowlist miss) | *no check; the panel never ran* |

Because the same review that opens the check also concludes it, `protoReview` never dangles. Unlike
`QA panel`, it is published for every panel that runs regardless of shadow mode or promotion
ownership, so it is **safe to require everywhere**. Its `head_sha` is resolved server-side from
the PR, never from the webhook, and the concluding summary carries the verdict text or the
exhaustion reason.

**To make it enforce**, add `protoReview` to required status checks. An exhausted panel then leaves
a red X that blocks the merge instead of a silence a merge sails through.

**Re-running it.** A red `protoReview` clears by pushing a fix, or by clicking **Re-run** on the
check. GitHub delivers that as a `check_run` *rerequested* event, which re-runs the panel with the
same force posture as a manual summon. That needs the App subscribed to **Check run**.

### Clearing by resolving threads

When the check reads "N unresolved review threads", resolving them re-publishes the gate directly.
GitHub delivers a `pull_request_review_thread` *resolved* event, and the handler re-runs the
promotion evaluation (a state re-read and a check publish; no panel, no model call). Reopening a
thread takes it back to red the same way.

> **Needs the App subscribed to `pull_request_review_thread`.** Without it the check still asks
> you to resolve the threads and doing so will not clear it. That contradiction (issue #111) cost
> one PR ten hours of stale red.

## `Review at head`

`scripts/review_at_head.py`, run by `.github/workflows/review-at-head.yml`, posts a commit status on
every open PR that answers one question: **is there a QA-panel verdict for this head SHA?** It does
not re-judge code. A WARN satisfies it. What fails: no verdict for the head, an explicitly blocking
verdict, and a verdict whose coverage is not complete or that retains a standing block.

That makes it safe to mark **required** without making the advisory tier secretly mandatory, and it
is the check that cannot park forever: `QA panel` has two observed ways to sit in progress without
ever going red (a lane dying with no terminal state, and "Waiting on CI" after a PR merges). The
documented escape hatch is the `skip-review-gate` label.

It is vendored from protoAgent's own gate; the supersede rule it shares with `QA panel` is vendored
by `scripts/vendor_supersede_rule.py` and tested for drift (`tests/test_vendored_supersede_rule.py`).

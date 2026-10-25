# Promotion: approve-on-green

## The draft → ready contract

**Undrafting a PR is the act of shipping it.** On a repository this plugin watches, a PR's
draft state is its merge gate.

- **Draft PRs are skipped by the panel.** A draft is dropped before the panel runs, by the same
  eligibility gate that skips a closed or locked conversation (telemetered `pr-not-eligible`,
  `why=draft`). No review is posted, no verdict is produced, and nothing can promote while the PR
  stays draft.
- **Marking a PR ready for review hands it to the QA pipeline.** The draft → ready transition is a
  review trigger. On a watched, `main`-targeting PR the panel runs, and once it posts a
  **current, complete PASS** and the promotion guards are all green, approve-on-green posts a
  formal APPROVE **and arms native GitHub squash auto-merge** (`gh pr merge --auto --squash`).
  Arming is best-effort and scoped to `main`-targeting PRs (stacked PRs are excluded). A repo with
  auto-merge disabled simply declines.

So the practical contract is: **undraft a PR only when it is ready to ship, not when you merely
want eyes on it.**

### A PASS does not bypass the guards

Auto-merge is armed only when a clear PASS or WARN verdict clears **every** fail-closed guard in
`promotion_decision`. A PASS is not a skeleton key:

- this agent **owns promotion** for the repo (`promotion_owner: true`) and is **not in shadow
  mode**;
- a **clear verdict** (PASS or WARN) exists for the PR's **current head SHA**. A verdict for a
  superseded head is **stale** and holds (`hold:stale-head`), so a PASS never lands a commit the
  panel never saw;
- that verdict has **not already been promoted** (per-head dedup);
- **no panel round for the PR is still running or queued** (`hold:round-in-flight`). The clear
  verdict is the newest *finished* round, and a round still running on the same head may FAIL it.
  `QA panel` reads "Re-review in progress" meanwhile. The converse is handled when that round
  posts: a FAIL dismisses our earlier approval, writes `QA panel` red and turns off GitHub
  auto-merge on the PR (escalated if GitHub refuses) at once, rather than leaving an APPROVED
  review and a green check standing beside the FAIL until the next sweep;
- the pass was **verified** (the verify pass ran) over **complete** coverage. Every finder lane
  delivered a full pass; none timed out, came back blocked or without its status line, or found its
  structural engine down. An incomplete pass is "nobody looked", not "nothing there", and holds;
- **CI is terminal-green**: checks unknown, pending or failing all hold; and
- there are **zero unresolved review threads**.

Every unknown (unreadable checks, unreadable threads, no verdict) falls through to a typed
`hold:*` and the sweep re-evaluates next pass. Even once armed, native GitHub auto-merge still
waits on branch protection and required status checks, including `QA panel` / `protoReview`
where you require them. **Arming it is not merging it.**

### Hold reasons

| Hold | Meaning |
|---|---|
| `hold:not-promotion-owner` | This agent does not own promotion for the repo, or is in shadow mode |
| `hold:no-clear-verdict` | No PASS/WARN for the current head yet (the sweep's backfill will review it) |
| `hold:stale-head` | The verdict is for an older commit |
| `hold:already-promoted` | This verdict was already promoted (per-head dedup) |
| `hold:round-in-flight` | A panel is running or queued for this PR |
| `hold:incomplete-coverage` | A finder lane did not deliver; "nobody looked" |
| `hold:unverified` | The verify pass did not run or was contradicted. The sweep runs one fresh round |
| `hold:carried-prior` | A prior blocker/major was re-listed without being re-verified on a line the delta changed |
| `hold:checks-pending` / `checks-failed` / `checks-unknown` | CI is not terminal-green, or could not be read |
| `hold:no-checks` | The repo has no checks at all. `QA panel` concludes neutral rather than waiting on CI forever |
| `hold:threads-unresolved` / `threads-unknown` | Open review threads, or threads that could not be read |
| `hold:pr-not-eligible` | Draft, closed or locked |
| `hold:promote-backoff` | A recent promotion attempt failed; retrying after a delay |

The re-gate has its own `hold:regate-*` reasons (`shadow`, `disabled`, `no-current-fail`,
`already-blocking`, `checks-pending`, `checks-unknown`, `backoff`).

`POST /promote` ([api](api.md#post-promote)) evaluates the decision for one PR and tells you which.

### A promoted WARN carries its findings

A WARN "does not block merge", so a confirmed finding could land and the PR read APPROVED thirty
seconds later with the finding having no consumer. The approval body therefore restates the open
findings, and the marker gains `findings=N` so merge tooling can gate on "approved WITH findings"
without parsing prose.

This is deliberately **not** a block. Making WARN gate would hard-block a correct PR on a
hallucinated blocker. Gate rigidity must not outrun verdict reliability.

### Holding a PR that is reviewed but must not ship yet

There is no separate "reviewed but held" state. If a PR is complete and wants scrutiny but must
**not** land yet (it shares a file with another PR in flight, waits on a sibling, a release window,
a coordinated rollout), **keep it in draft**, or do not undraft it, until that dependency is
resolved. A draft is skipped by the panel and can never promote, so draft is the safe hold.
Undraft only once it is genuinely clear to ship.

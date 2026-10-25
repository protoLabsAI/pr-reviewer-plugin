# Verdicts and precision guards

The panel is a language model, and a language model produces plausible, wrong findings. This page
is the code that sits between its output and the verdict. Each rule exists because a specific
review went wrong; the issue number is the receipt.

`verdict_for` (in `verdicts.py`) maps findings to a verdict and **nothing else**:

| Verdict | When |
|---|---|
| `FAIL` | Any blocker or major that is `confirmed`, or carries no verify annotation |
| `WARN` | Any minor, or a blocker/major the verifier left `uncertain` or `possibly-addressed` (unproven, or verified against a head the PR has since replaced) |
| `PASS` | Nothing above. Nits never move the verdict |

An `uncertain` finding can never carry a `FAIL`. Anything that needs review *history*
layers on top in `rounds.py` and takes its facts as arguments. Relief always **fails closed**:
an unreadable delta, an uncertain major, or a finding outside the delta means the WARN stands.

Severity mapping from protoPatch: critical/high/medium/low become blocker/major/minor/nit.

## What a posted review contains

A code-authored header and **coverage line**, the panel's brief, a scannable severity-sorted
**findings table** (severity · location · finding · verified), and the machine-readable JSON in a
collapsed `<details>`. Prior-round recall reads that JSON back out of the body, so it cannot be
removed. The comment body is **assembled from parsed blocks**, never the model's raw output, so
deliberation cannot leak into a review.

## In the diff, or it doesn't count

### Confinement

Findings whose `file` is not one of the PR's changed paths are dropped server-side before the
verdict mapping, telemetered, and footnoted in the posted body. The prompts promise in-diff
discipline; the dispatcher enforces it. It fails open when the changed-path list is unreadable, so
a failed GitHub read can never launder a FAIL into a PASS.

### Structural scoping

Confinement's line-level step, for `source: protopatch` findings only. protoPatch reviews whole
features, so on a touched file it also reports code that was already there. A structural finding
outside the PR's changed lines (±5), and in a Python file outside every function those lines sit
in, is a **nearby note**: listed in the body, flagged `nearby: true`, left out of the verdict, never
carried as prior debt. It **fails closed** (the finding gates as before) on no line, an unreadable
patch or head file, a file without a patch, or Python that does not parse. The LLM lanes are not
scoped: they read the diff, so a line they cite outside a hunk is usually the change's consequence.

## Claims are checked

| Guard | Catches | Toggle |
|---|---|---|
| **Evidence grounding** | A finding whose quoted code appears nowhere in the cited file at the reviewed head, nor in the PR's patch. Annotated `uncertain` (never dropped). Quotes come from the finding's *evidence*, with any suggested fix cut off first; a quote not found is also looked for in the files the evidence names and the rest of the patch. A grounded finding's `line` is re-anchored to where its evidence starts (`line_original` keeps the panel's). Fails open on an unreadable blob or unquotable evidence | `evidence_grounding` |
| **Absence search** | "No test", "dead", "never called", "no `x.py` exists". The backticked subjects and the cited module are `git grep`ed in a head checkout. A reference elsewhere, a non-definition use, or the named file existing refutes the claim; a search that could not run leaves a `confirmed` absence `uncertain` | `absence_search` |
| **Evidence guard** | A `confirmed` note that quotes no code and restates the claim, or asserts what a declared dependency does without quoting its source, docs or a run of it | `evidence_guard` |
| **Lint-rule check** | A protoPatch finding citing a ruff code (F841, E501, …) is run past the ruff version the repo's CI pins; no such diagnostic near the cited line drops it. See [structural](structural.md#lint-rule-claims) | `lint_check` |
| **Refutation memory** | A claim the verifier refuted is remembered per repo; a repeat on code the PR does not touch is pre-marked | `llm_refutation_memory`, `refutation_ttl_days` |

Absence search and the evidence guard require evidence grounding on. Together they were measured
on a labelled audit of 34 findings (`tests/fixtures/audit_259.json`, scored by
`tests/test_precision_259.py`): confirmed findings 32 with 9 false became 25 with 2 false, and no
true finding was demoted.

The residual class is a finding that quotes real code and *reasons wrongly* about it. That is the
verify prompt's half.

## Review history

### Rounds, not reviews

Recall reads the PR's *panel rounds*. A promotion body carries our marker and no findings, so
taking the newest marker-bearing review as "the prior review" meant that after any approve-on-green
the next round recalled an empty `prior_findings` and silently re-reviewed cold. A re-gate's
verbatim re-post no longer double-counts a head either.

### Prior-request memory

Every round's findings ride along as an escaped `<prior_requests>` block plus `review_round`. A
finder can see that the line it is about to flag exists *because the panel asked for it*, and
verifies the change was implemented correctly instead of re-litigating it as unexplained new scope.
A wrong, partial or defect-introducing fix is still a finding.

### Existing threads

The dispatcher fetches the PR's inline review threads (human, CodeRabbit, others), renders them as
one escaped `<pr_review_threads>` block (closing-tag neutralization, login validation, body
truncation), and finders suppress candidates that overlap a live thread. Unreadable threads degrade
to "(none)"; awareness never blocks a review.

### Convergence

A review loop needs an exit. From round `convergence_rounds` (default 3), a **WARN** whose findings
are *all* minor/nit **and** *all* anchored to lines that moved since the previous reviewed head
becomes **PASS with notes**: the findings still post, as a follow-up checklist, and stop holding
the verdict. A FAIL never converges. Neither does an uncertain major, a finding on code the review
never touched, or an unreadable compare. `0` disables it.

A carried prior is also **cleared by the delta**: when the lines it flagged moved since it was
raised and every code quote in it is absent from the file at the reviewed head and the PR's patch,
it was fixed. That is the same read grounding applies, and stronger than a `fixed` the model
asserts. Fail-closed on an unreadable delta or file, and on a prior that quotes nothing checkable.

### Dispositions and the clearance hold

The report pass must state, per prior **blocker/major**, whether it was `fixed` (naming the change),
is still `open`, or was `refuted` (on evidence). A confirmed major that simply stops being
mentioned **holds any standing block, whatever the new verdict is**. The origin: a zero-finding PASS
dismissed a block and the defect merged 44 seconds later, with `findings=0` reading identically
whether the code was clean or nobody looked. The verdict still posts and names the dropped finding,
and a **second consecutive** clean PASS lifts the block (two independent draws are evidence; one is
a coin flip).

When dispositions are present they are the authority. A recipe that emits no block keeps the
narrower zero-finding rule. `hold_unexplained_clearance: false` restores always-dismiss.

When `prior_recheck` is on, prior blocker/majors are re-verified at the new head, **alone** (one
seeded verify step), before being re-asserted. A `refuted` clears the prior. A re-listing nobody
re-verified on a line the delta changed never blocks: it is carried as debt, and promotion holds
`hold:carried-prior`.

## Coverage gaps

An explicit `[]` means "looked, found nothing". No array at all means nothing reached that boundary.
Both used to parse as `[]`, so a lost payload posted PASS.

- **An absent payload is an incomplete round, never a verdict.** If no finder lane delivered an
  array (a timeout Gap, a `PROTOPATCH UNAVAILABLE` relay, or a `FINDER_STATUS: blocked` lane is not
  a delivery), or synthesize or the final report emitted none, the panel re-runs (`panel_retries`).
  If it still delivers nothing: no review posted, `protoReview` red ("QA panel incomplete, no
  verdict"), the operator escalated, an `exhaustion` event with `undelivered: [...]`, and the
  sweep's backfill retries later. A missing `FINDER_STATUS` line alone never voids a lane.
- **A coverage gap caps a clean PASS at WARN.** Any lane the engine timed out, any LLM finder that
  did not declare `FINDER_STATUS: reviewed`, or a structural relay that was unavailable or cut short:
  `complete=false` (promotion holds), a code-authored coverage line above the brief naming each lane
  and why, no "came back clean" line, and PASS posted as **WARN** (`coverage_capped` in telemetry).
  Not FAIL and not "no verdict": a gap means the review covered less, not that the code is bad.
  Lanes are judged only where the recipe ran them under that contract; the lite `code-review` recipe
  has no structural seat and asks for no status line.
- **A verify step that hands nothing back on a clean round is a gap too.** With findings, a dead
  verifier shows (nothing annotated, `verified=false`). With none it could not: "nothing to check"
  and "stopped at its preamble" looked the same. A zero-finding round whose `verify` output has no
  fenced array and no `VERIFY_STATUS` line names `verify` in the coverage line and posts WARN
  (`verify_undelivered`). `complete` stays true and the round is not retried.
- **A verifier that contradicts the synthesizer** (`nothing-to-verify` over a synthesis that has
  findings) is re-run alone, seeded with the earlier outputs, up to `verify_reruns` times. If it
  stays contradicted, one fresh panel runs (`verify_fallback_panel`) before a held PASS posts.
  A round that still cannot verify posts `verified=false` and holds.

## Latency note

The five finders are one parallel stage, but the host's `subagent_max_concurrency` defaults to 4,
which ran the stage as 4+1 and paid the slowest finder twice (measured p50 458s versus 322s for
the four-finder recipe). The recipe declares `max_concurrency: 5`; an older host ignores it. The
dispatcher records per-step `timings`, and the [eval report](operations.md#the-eval-report) shows a
p50 per step.

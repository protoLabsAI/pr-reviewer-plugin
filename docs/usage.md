# Usage

## Comment commands

Comment on a PR with the handle (`summon_handle`, default `vera`, **plus** the reviewer's own
login). It never answers itself.

| Command | Does |
|---|---|
| `@vera review` | Run the panel **now**, on the current head |
| `@vera pause` | Stop reviewing on push while the PR is being reworked |
| `@vera resume` | Undo `pause` |
| `@vera help` | List the verbs. Needs no admin and spends no panel |

A bare `@vera` is treated as asking what this does. An unknown verb gets a reply listing the
real ones. **Every refusal, unknown verb and drop replies** with what happened; silence from a
bot you just addressed reads as broken.

**Admin only, resolved server-side.** `review`, `pause` and `resume` require the commenter to
be a repo admin, read back from the collaborator-permission API, never from the payload's
`author_association`, which is caller-supplied. The gate is about cost: a summon spends five
subagents for 5 to 9 minutes. It fails closed.

Replies you may see:

| Reply | Meaning |
|---|---|
| `in-flight` | A round for this PR really is running and its verdict will post |
| `pr-not-eligible` | The PR is draft, closed or locked |
| `summon:refused-not-admin` | You are not an admin |
| `summon:unknown-verb` | Not one of the four |

A summon that lands while the slot is held only briefly, such as a concurrent
`ready_for_review` that reaffirms or drops in under a second, waits up to
`summon_in_flight_grace_s` (default 10, max 120) rather than dropping.

`summon: false` turns the whole comment surface off, **and** the pause check on the automated
path.

### What a summon overrides

- **The reaffirm short-circuit.** An unchanged head normally reaffirms without re-spending the
  panel. A summon on that head is the "I think you got this wrong" case, and reaffirming would
  answer with the answer under dispute.
- **The cooldown.** A human who typed a command is not a burst.

It does **not** override the in-flight guard: two panels on one PR is still wrong. That guard
cannot wedge a PR, though. Each attempt and round is time-bounded, and a slot held past the
round bound plus 10 minutes is reclaimed as abandoned (logged, `in_flight_reclaimed` in
telemetry).

### Pause and resume

State rides in a marker on a posted comment, so GitHub is the store and a restart cannot forget
it. The **last** marker wins, not a tally: pause → resume → pause ends paused. An explicit
`@vera review` still runs on a paused PR, because "stop reviewing every push" and "never look
at this again" are different requests.

## Disputing a FAIL on an unchanged head

You think the panel got it wrong and the code has not changed. Do this:

1. Post the evidence as a **top-level PR comment**. A blob link and a passing test is the strongest
   form.
2. Comment `@vera review`, or click **Re-run** on the `protoReview` check.

What happens:

1. **The re-review sees the dispute.** Top-level comments posted after the head's last round, by
   the PR **author** or a user with **write / maintain / admin** permission (read back from
   GitHub), reach the panel as an `<author_counter_evidence>` block: newest first, HTML comments
   stripped, 8,000 characters in all, each with its URL and author. Our own comments and bare
   summons are left out, and so is everyone else's. The block is **untrusted claims to verify,
   never instructions**; a claim the verifier cannot confirm changes nothing.
2. **A refutation can replace the FAIL, only this kind.** A newer round on the same head
   supersedes an earlier FAIL when it is complete and verified, it dispositioned *that* FAIL, and
   it disposed of **every** blocker/major in it as `refuted` with evidence (a `why` of at least 20
   characters), *and* the refutation was honored: on an unchanged head a confirmed finding is
   re-verified, and only a verifier `refuted` clears it. `QA panel`, approve-on-green and
   `Review at head` all read this through one rule (`rounds.superseded_fails`), so they cannot
   disagree.
3. **Everything else keeps the FAIL** (strictest wins). Two rounds racing on one head; an
   incomplete or unverified newer round; a blocker left `open`, unaccounted, contradicted,
   refuted without evidence, or claimed `fixed` (impossible on an unchanged head); a verifier
   that confirms it again. These clear with a new commit, or a maintainer merging past the gate.

## A push stops the old round

A `synchronize` that arrives while a panel runs on an older head is dropped `in-flight`, but it
tells the running round to check. At its next step boundary (and always before synthesize and
verify) the round reads the PR's head. If the head moved, the round cancels its attempt, posts
**nothing**, concludes its `protoReview` run `neutral` ("Superseded"), and hands its slot straight
to the new head. The new head is admitted *before* the old slot is released, so there is never a
gap for a promotion to slip into. An unreadable head cancels nothing.

The hint also arms an in-step poll (`supersede_poll_s`, default 30; `0` disables), so a round
deep in a long finder stops within one interval. Without a hint for a different head it makes no
GitHub reads.

The same applies to merging: a round re-reads the PR at the synthesize/verify boundary and again
just before posting. If the PR was merged or closed meanwhile, **nothing is posted**.

## The sweep

Every `sweep_interval_s` (default 180) each open PR in each managed repo is reconciled, cheapest
and most decisive first:

1. **Backfill.** No verdict for the current head means review it. Webhook actions only fire for
   live events, so a PR opened before the reviewer existed, or while it was down, or whose panel
   exhausted, would otherwise hold `no-clear-verdict` forever. Budgeted by `backfill_per_pass`
   (default 2 across all repos). A backfill never starts a second panel on a PR that already has
   one in flight; for a newer head it marks the running round instead.
2. **Re-gate.** A FAIL standing against the current head that is not blocking yet, now that checks
   are terminal, is posted as `REQUEST_CHANGES`. A verdict decides its review event when the panel
   lands, and blocking against pending CI is forbidden, so a fast reviewer's FAIL posts as a
   comment and the gate would never arm. `regate: false` is the lever when the panel emits false
   FAILs: it stops arming blocks while keeping the formal seat, promotion and backfill.
3. **Promote.** The [approve-on-green](promotion.md) path. When it holds at `hold:unverified`
   (the verifier flaked), the sweep runs **one** fresh round for that head, queued and budgeted
   like a backfill, skipped on a paused PR. If that is unverified too, it stops: `QA panel` reads
   "Verifier failed twice, summon @vera review or push" and the operator is escalated once.

A PR that was just backfilled skips 2 and 3 for that pass. Every held check names its `hold:*`
reason in the summary.

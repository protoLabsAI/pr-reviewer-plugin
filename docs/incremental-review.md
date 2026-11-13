# Incremental re-review

Behind `incremental_review` (default `false`; issue #313). With it off, every round is a full
review, exactly as before.

## What it does

A re-round normally re-reads the whole PR. With the flag on, it reviews only:

- the files that **changed since the last round**, and
- the files that **depend on** a changed file.

The last round's findings on every other file are **carried forward** unchanged.

A file counts as unchanged when its blob SHA at this head equals its blob SHA at the last
round's head, under the same merge-base. Its findings were already judged on identical bytes,
so the dispatcher carries them itself and the panel does not re-read the file. The scope reaches
the panel through the recipe's `review_scope` input, which every finder and the synthesizer
read. The structural pass plans the in-scope files only.

| | |
|---|---|
| **In scope** | A file whose blob changed or is new. Also a file whose PR patch names a changed (or reverted) file, by path or module stem, and a file holding a prior finding whose evidence cites one |
| **Carried** | Every other PR file. Its prior findings are recorded again verbatim (severity, verdict, `since`), stamped `carried_by: incremental`. Grounding, the absence search, the evidence guard and pre-existing scoping all re-check them at the new head. A carry whose quoted evidence is gone is dropped |
| **Panel findings on carried files** | Fresh minor/nit findings (and re-statements of a carry) are set aside. That code was judged at identical content. A blocker/major is kept |
| **Priors the panel sees** | Only those on in-scope files, which it dispositions as usual. A carried confirmed major still FAILs the round and still holds promotion (`carried_debt`) |

The posted body ends with a note saying how many files were reviewed, how many were carried,
and that `@vera review` buys a full re-review.

## Enable on chosen repos

`incremental_review_repos` (env `PR_REVIEWER_INCREMENTAL_REVIEW_REPOS`) narrows the flag to an
`owner/name` allowlist, so the A/B can start on one or two high-churn repos:

```yaml
incremental_review: true
incremental_review_repos: [protoLabsAI/protoAgent]
```

A YAML list or a comma/space separated string; matching is case-insensitive. Empty (the
default): the flag governs every repo, as before. A repo not on the list takes exactly the
flag-off path — its rounds make no incremental reads and are byte-for-byte full reviews. With
the flag off, the list changes nothing: no repo is incremental.

## When it reviews in full

Each of these reviews the whole PR. The reason is recorded as `full:<reason>`.

- an operator summon (`summon`)
- an epic residual round (`epic-scope`)
- the small-diff recipe (`recipe`)
- the first round (`first-round`)
- a prior round that was incomplete, unverified, or ran another recipe (`prior-incomplete`, `prior-unverified`, `prior-recipe`)
- any read that failed (`unreadable`)
- a moved merge-base, e.g. a merge or rebase of the base branch (`merge-base-moved`)
- a truncated file list (`file-list-truncated`)
- a changed manifest or lockfile (`manifest-changed`)
- no file changed (`no-pr-file-changed`)
- every file in scope (`every-file-in-scope`)

A moved base forces a full round because the lanes' read sets are not observable on the
current host. A change to any file outside the PR could matter to any lane, so the PR is
treated as one cluster.

## Measuring it

The `incremental_scope` event records each decision. The `reviewed` row carries these fields:

| Field | Meaning |
|---|---|
| `incremental` | `scoped`, or `full:<reason>` |
| `files_reviewed` | Files in scope |
| `files_carried` | Files carried |
| `carried_kept` | Carried findings kept |
| `carried_dropped` | Carried findings dropped |
| `out_of_scope_dropped` | Panel findings set aside on carried files |
| `lane_s_saved_est` | Σ finder seconds × carried/in-scope files. It assumes cost ∝ files and is capped at 10× |

To compare against flag-off rounds, compare `latency_s` and `step_s` on re-rounds.

## Not yet

A superseded round's finished lanes are not reused. Its replacement is scoped against the last
**posted** round, so it is incremental, but the work the cancelled run already did is discarded
as before. Reusing it per file needs per-lane scope, which is issue #313's next slice.

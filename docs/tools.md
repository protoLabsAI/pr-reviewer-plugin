# Tools

Four agent tools, registered by the plugin. The drift test fails if one is added or removed
without this page changing.

## `protopatch_review`

Run the [protoPatch](structural.md) structural engine over a pull request and return its findings
as the standard fenced findings JSON, each item carrying `source: "protopatch"`.

| Arg | Type | |
|---|---|---|
| `pr` | int | The pull-request number |
| `repo` | string | `owner/name`. Omit to use the round's own repo, else `default_repo` |

- Head and base SHAs are resolved from the PR **server-side**.
- **Expensive** (an LLM-backed engine, minutes): call it at most **once** per review.
- On `PROTOPATCH UNAVAILABLE` it returns the Gap line and an empty array. **Never retry, never
  invent findings.**
- When the PR is being reviewed by a bound round, an omitted `repo` resolves to *that round's* repo,
  never `default_repo`, so a `default_repo` pointing elsewhere cannot run the pass over the wrong PR.
  An explicit `repo` still wins.

This is the tool the `structural-finder` subagent calls; you rarely call it yourself.

## `search_pr_repo`

Search the whole repository at the head commit of the PR under review: `git grep` over that
commit in the shared checkout cache, returning `file:line: text` hits (issue #311). It is how a
review lane backs an absence or universality claim ("no test covers X", "never called", "every
entry is named Y") where it makes it.

| Arg | Type | Default | |
|---|---|---|---|
| `pattern` | string | | A Perl-compatible regex (`-E` on a git built without PCRE) |
| `pr` | int | | The pull-request number |
| `repo` | string | the round's repo, else `default_repo` | `owner/name` |
| `path` | string | whole repo | A directory or glob relative to the repo root: `tests/`, `**/*.py`, `changelog.d/*.md`. A glob matches the whole path |
| `fixed` | bool | `false` | Search literal text |
| `ignore_case`, `word` | bool | `false` | grep `-i` / `-w` |
| `files_only` | bool | `false` | List matching files instead of lines |
| `max_results` | int | `40` | Capped at 100. Lines are cut at 240 characters, a result at 12,000 |

- **Read-only and scoped.** The model names a PR, never a ref or a directory. The head is the one
  the dispatcher bound when it opened the round; outside a round it is resolved from GitHub,
  server-side. git reads the commit's objects, never the working tree, so a symlink is not
  followed; a path with `..`, an absolute path or pathspec magic is refused.
- **Four outcomes that never read as one another**: `SEARCH RESULTS` (with `complete` or
  `truncated`), `NO MATCHES` (states how many files were searched: evidence of absence),
  `PATH MATCHES NO FILES` (nothing searched) and `SEARCH ERROR` (the search did not run, and says
  it is *not* evidence of absence).
- Marked `access: read`, so the host's read-tool inheritance (protoAgent ≥ 0.213.0) binds it to
  the review finders and the verifier. The checkout starts building when the round opens.

## `list_pr_repo_files`

List the files at the head commit of the PR under review, under a directory or glob, with the
total count: to check that a file exists, or to count before claiming every file of a kind
follows a convention. Same scoping and outcomes as `search_pr_repo`; `FILES` for a listing.

| Arg | Type | Default | |
|---|---|---|---|
| `pr` | int | | The pull-request number |
| `repo` | string | the round's repo, else `default_repo` | `owner/name` |
| `path` | string | whole repo | A directory or glob, as for `search_pr_repo` |
| `max_results` | int | `200` | Capped at 500 |

## `pr_review_eval`

Render the PR-review eval report: completion rate, verdict and recipe mix, latency percentiles,
typed drops, promotion decisions, and (with `three_way=True`, the default) a per-PR comparison of
our verdicts against Quinn's review states and CodeRabbit activity on the same PRs.

| Arg | Type | Default | |
|---|---|---|---|
| `three_way` | bool | `true` | Include the per-PR three-way section. This reads each PR's reviews from GitHub; if that fails the report degrades to telemetry-only with a note |

Registered only when the dispatcher machinery wires up. See
[operations](operations.md#the-eval-report).

## Subagent and workflow

Not tools, but registered alongside them:

| Name | Kind | |
|---|---|---|
| `structural-finder` | subagent | The relay seat. Calls `protopatch_review` once, passes the array through verbatim, reports the Gap on unavailability. Must never fail its step |
| `code-review-structural` | workflow | The five-finder recipe. Run by hand with `run_workflow("code-review-structural", {"pr": "69", "repo": "owner/name"})` |

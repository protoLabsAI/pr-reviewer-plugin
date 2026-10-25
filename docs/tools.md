# Tools

Two agent tools, registered by the plugin. The drift test fails if one is added or removed
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

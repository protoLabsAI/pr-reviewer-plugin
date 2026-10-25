# The structural engine (protoPatch)

`protopatch_review` runs [protoPatch](https://github.com/protoLabsAI/protoPatch) (the `clawpatch`
CLI) over a pull request and returns its findings in the ADR 0077 contract with
`source: "protopatch"`. It is the fifth, non-LLM-driven seat on the [panel](overview.md#4-the-panel),
reached through the `structural-finder` subagent: a **relay** that calls the tool once and passes
the findings through verbatim. It is not a reviewer.

Its findings get the same dedup and independent verify as every LLM finder's.

## Never raises

Every failure returns `PROTOPATCH UNAVAILABLE` with a prescribed Gap line. A raising panel step
would void the whole review (ADR 0078 D3); a starved structural pass degrades the panel to four
finders instead. The relay also has a completion contract: a turn that ends without the fenced
array is sent back to the model once (host ≥ 0.170.0), because on 23% of early runs the model
rewrote the findings as markdown and the lane read as unavailable.

## A run

1. **Resolve** head and base SHAs from GitHub, server-side. Never a model-supplied ref.
2. **Check out** into a content-addressed cache: blobless partial clones (`--filter=blob:none`) keyed
   on `repo@headSha`, TTL `checkout_ttl_s` (1 h), LRU `prune()` under `checkout_max_entries` /
   `checkout_max_bytes`.
3. `clawpatch init` + `map` into a **per-review state dir**.
4. **Plan** which features to review ([below](#planning)).
5. `clawpatch review --provider gateway --json --state-dir <per-review> --feature-list <plan> --jobs <n>`
   under a hard wall-clock budget (`time_budget_s`, SIGKILL past it).
6. **Read** findings from the state dir, confine them to the PR's changed files, map severity,
   keep `category` verbatim.

`structural_plan: false` is the rollback: a single `clawpatch ci … --since <baseSha>` run. It is
also the fallback when `map` fails.

## Planning

`ci --since` reviews every feature that owns a changed file *or lists one as context*, and
clawpatch's Python mapper lists `pyproject.toml` as context of every Python feature: a one-line
version bump on protoAgent planned 276 of 361 features and could not finish at any budget.

So the plugin plans the features:

- A lockfile, generated file, changelog fragment or dependency manifest (`uv.lock`,
  `package-lock.json`, `pnpm-lock.yaml`, `THIRD_PARTY_LICENSES.md`, `changelog.d/`, `pyproject.toml`,
  `package.json`, `Cargo.toml`, `requirements*.txt`, …) **never pulls a feature in as context**. Its
  findings are confined to the diff anyway.
- The remaining features are **ranked** by changed lines in files they own, then in their context
  files, and at most `structural_max_features` (default 16; `0` = no cap) are reviewed.
- **Features the cap leaves out are a coverage gap**, never a silent drop. The pass comes back
  partial (`N of M features reviewed — feature cap reached …`, `structural_reason: feature-cap`),
  the round is incomplete, and the verdict is capped at WARN.

Every pass writes a `structural_plan` telemetry event: features mapped / eligible / selected /
dropped / out of scope, and per planned feature its owned and context changed lines, file count,
prompt size, `elapsed_s` and how it ended (`finished`, `error`, `killed` = in flight at the kill,
`not-started`). That tells an over-planned pass from a hang without the container log. Under
[epic attestation](epic-attestation.md) the plan, selection and confinement use the residual files
only (`scoped_paths`).

## Concurrency

`structural_jobs` (default 4) is clawpatch's `--jobs`. Left alone clawpatch reviews about half the
host's cores of features at once (max 10), each a 50 to 115k-token prompt, so one big PR floods the
model lane's KV cache and everything on it times out. 95% of passes have ≤ 3 features and are
untouched. A pass takes `ceil(features / jobs)` waves, so **if you lower it, raise
`time_budget_s`**. `0` means no `--jobs` (clawpatch's default). Values above 10 are clamped; a
mistyped value falls back to the default with a warning.

## Budget and retry

`time_budget_s` (default 600) bounds the whole pass and covers up to **two** clawpatch runs: a
*transient* gateway failure (dropped request, socket timeout, gateway 5xx) is retried once if at
least ~90 s remain. The CLI's own gateway-request timeout scales with the budget (budget − 30 s);
an inherited `CLAWPATCH_GATEWAY_TIMEOUT_MS` is honored but capped to that window.

## One state dir per review

Each pass runs with its own state dir under the repo's (`<state_root>/<owner-name>/scratch/<head>-<id>`),
so concurrent reviews of one repo cannot fail each other's feature claims (`exit 7`, `feature
locked`), one PR's findings cannot surface on another that touches the same file, and a redeploy
that kills a run leaves no lock behind. A successful pass deletes its dir. A failed one keeps it for
a postmortem and it is pruned after 6 h (and at startup). What persists stays in the repo's own
dir: `refuted.json` and `provider-failures/` (captures of unusable model replies).

## Partial results

When the budget kills the pass or a feature errors, but at least one claimed feature had already
finished, the findings those features wrote come back as a **partial** result (`PROTOPATCH PARTIAL`,
header `protoPatch structural pass partial on …`, and `Gap: structural pass partial — N of M features
reviewed — <reason>`) instead of being thrown away.

It is still a lane gap: the round is incomplete and the verdict capped at WARN. The telemetry row
carries `structural_partial: true` but **not** `structural_unavailable`, which means the lane
delivered nothing. Count a structural gap as either flag. With nothing finished it is the outage it
always was.

## Gap classes

The `reviewed` row's `structural_reason` is never empty when the lane did not deliver a full pass.

| `structural_reason` | Means |
|---|---|
| `budget-timeout` | Hit `time_budget_s` |
| `feature-cap` | `structural_max_features` left features out |
| `not-installed` | `clawpatch` not found |
| `no-credentials` | No gateway credentials |
| `checkout` | The clone failed |
| `exit-N` | clawpatch exited N. Exit 4 refines to `exit-4:gateway-timeout`, `exit-4:gateway-<status>` or `exit-4:provider` |
| `no-output:max-turns` / `no-output` | The relay model looped until max turns, or answered without calling the tool |
| `unclassified` | None of the above |

For `unclassified` and `no-output*` the row also carries `structural_detail`: what the lane said,
≤ 160 characters, with URLs, paths and secrets scrubbed.

## Lint-rule claims

A protoPatch finding whose claim cites a ruff rule code on a `.py` / `.pyi` file is run past the
ruff version the repo's `.github/workflows/*.yml` pin (`ruff==X.Y.Z`, `ruff@X.Y.Z`, or ruff-action's
`version:`), with `--select <code>` on that file in the checkout. If ruff reports no such
diagnostic at (±3 lines of) the cited line, about the name the claim cites, the finding is dropped
and the header says so. Each version is pip-installed once (`--only-binary=:all: --no-deps`) into
`~/.protoagent/pr-reviewer/tools/ruff/`. Both subprocesses get a token-free environment, ruff runs
with `--no-cache`, and the check is bounded by `lint_check_budget_s` (90).

No pin, two different pins, no line, a failed install, a ruff error or a timeout leave the finding
as it was. `lint_check: false` turns it off.

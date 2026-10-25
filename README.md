# pr-reviewer-plugin

A **protoAgent plugin** that reviews pull requests the way a careful second engineer would,
and can hold the merge button until it is satisfied. It is the deterministic half of
protoAgent's PR-review QA tier ([ADR 0078](https://github.com/protoLabsAI/protoAgent/blob/main/docs/adr/0078-fleet-pr-review-qa-tier.md),
findings contract [ADR 0077](https://github.com/protoLabsAI/protoAgent/blob/main/docs/adr/0077-adversarial-code-review-workflow.md)).

A GitHub App delivers PR events to this plugin. For each one it runs a **five-finder
review panel** (four LLM finders plus a non-LLM structural engine, [protoPatch](https://github.com/protoLabsAI/protoPatch)),
dedups and ranks the findings, **independently verifies** each one, checks the claims
against the actual code, and posts a verdict: `PASS`, `WARN` or `FAIL`. Optionally it then
approves and arms auto-merge when everything is green.

## What you get

- **A panel, not a prompt.** Four parallel LLM finders and a structural pass feed one
  dedup → verify → report pipeline. See [overview](docs/overview.md).
- **Findings that are checked, not trusted.** Quoted code must exist, "no test" claims are
  grepped, lint-rule claims are re-run, out-of-diff findings are dropped.
  See [verdicts and precision guards](docs/verdicts.md).
- **A review loop with an exit.** Prior requests are remembered and low-severity churn
  converges to a pass. See [verdicts](docs/verdicts.md#convergence).
- **Enforceable checks.** `QA panel` and `protoReview` check runs a ruleset can require, so
  an unreviewed merge shows up as red. See [checks](docs/checks.md).
- **Approve-on-green.** A complete, verified, current PASS with green CI and no open threads
  is approved and queued for squash auto-merge. See [promotion](docs/promotion.md).
- **Comment commands.** `@vera review`, `pause`, `resume`, `help`. See [usage](docs/usage.md).
- **Shadow mode by default.** It ships disabled, and when enabled it only comments until you
  say otherwise.

> **Undrafting a PR is the act of shipping it.** On a watched repo, marking a PR ready for
> review hands it to the panel, and a current, complete PASS can arm squash auto-merge.
> Keep a PR in draft until it is ready to land. Draft PRs are skipped and can never promote.
> The full contract, including every guard a PASS must clear, is in [promotion](docs/promotion.md).

## Quick start

```bash
python -m server plugin install https://github.com/protoLabsAI/pr-reviewer-plugin --ref main
npm i -g @protolabsai/protopatch     # the structural engine (clawpatch)
```

Installing does not enable it. Create a GitHub App, point its webhook at your agent, then
enable **PR reviewer** in **Settings → Plugins**:

```yaml
# config/langgraph-config.yaml
plugins:
  enabled: [github, pr-reviewer]

pr_reviewer:
  repos: [my-org/my-repo]     # empty = every repo the App installation covers
  shadow_mode: true           # verdicts post as comments only; flip to false to formally block
  viewer_login: "my-reviewer[bot]"
```

Secrets (`webhook_secret`, `app_private_key`) go in Settings, which routes them to the
untracked secrets overlay. Step-by-step setup, App permissions and events, and a smoke test
are in [install](docs/install.md).

## Documentation

Everything lives in [`docs/`](docs/README.md):

| Start here | |
|---|---|
| [install](docs/install.md) | Requirements, the GitHub App, webhook, enabling, smoke test |
| [overview](docs/overview.md) | How a review happens, end to end |
| [usage](docs/usage.md) | Comment commands, disputing a FAIL, the sweep |
| [configuration](docs/configuration.md) | Every config key, default, env fallback |
| [tools](docs/tools.md) · [api](docs/api.md) | Agent tools and HTTP routes |

## Development

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest -q
```

The suite is host-free: it stubs `graph.*` and never shells out. Conventions for agents and
contributors are in [PROTO.md](PROTO.md); layout and release notes in
[development](docs/development.md).

_Reviewed by its own machinery. See ADR 0078._

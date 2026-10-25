# Development

```bash
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest -q
```

`ruff` is **pinned** in `requirements-dev.txt`, and the local gate must run that exact version. An
unpinned range once let CI resolve a newer ruff than the gate's venv; they disagreed and a change
passed locally and failed CI. Bump the pin deliberately, and upgrade the gate venv in the same move.

There is **no changelog file**. Describe the change in the PR title and commit message.

The suite is **host-free**: it stubs `graph.subagents.config` and never shells out. Conventions for
agents working in this repo are in [PROTO.md](../PROTO.md).

## Layout

| File | Role |
|---|---|
| `__init__.py` | `register()`: tools, subagent, routers, the sweep surface, App-token refresher. One machinery per state home per process |
| `dispatch.py` | The orchestrator: eligibility, rounds, the panel run, post, promotion, re-gate, backfill, sweep. Most behavior lives here |
| `webhook.py` | Routers: HMAC verify, event routing, `/queue`, `/replay`, `/eval`, `/summon/health` |
| `chokepoint.py` | Signature check, cooldown, in-flight slots, `DISPATCH_ACTIONS` |
| `trigger.py` | The structural trigger (files / lines / sensitive paths) |
| `verdicts.py` | `verdict_for` (pure), confinement, finding parsing, marker handling, coverage |
| `rounds.py` | Panel rounds, convergence, dispositions, `superseded_fails`. History-aware rules |
| `approve.py` | `promotion_decision` and the `hold:*` reasons |
| `checks.py` | Pure decision → `QA panel` check mapping (the writer is in `dispatch.py`) |
| `summon.py` | `@vera` command parsing and replies, `pause`/`resume` markers |
| `threads.py` | Review-thread fetch and escaped rendering |
| `counter_evidence.py` | The `<author_counter_evidence>` block |
| `grounding.py` | Evidence grounding |
| `absence_search.py` | Absence-claim search |
| `evidence_guard.py` | Quote-free `confirmed` demotion |
| `lintcheck.py` | Lint-rule claim check |
| `refutations.py` | Refutation memory (protoPatch and LLM lanes) |
| `epic.py` | Epic attestation |
| `protopatch.py` | The `protopatch_review` tool and the clawpatch runner |
| `checkout_cache.py` | The content-addressed blobless clone cache |
| `subagents.py` | The `structural-finder` relay |
| `workflows/code-review-structural.yaml` | The five-finder panel recipe |
| `app_auth.py` | GitHub App JWT, installation token, event subscriptions |
| `gh_cli.py` | `gh` wrapper |
| `telemetry.py` · `eval.py` | Event log; the eval report and agent tool |
| `replay.py` · `replay_cli.py` | Replay mode |
| `scripts/review_at_head.py` | The `Review at head` commit-status gate |
| `scripts/vendor_supersede_rule.py` | Vendors the supersede rule that gate shares with `QA panel` |

## Tests

`tests/test_docs_reference.py` makes the reference docs honest: it fails when a route, agent tool
or config key is added, renamed or removed without `api.md`, `tools.md` or `configuration.md`
changing. That is a coverage guarantee; what each entry *means* is a human's job.

Other tests worth knowing: `test_version.py` (manifest and `pyproject.toml` versions in lockstep),
`test_recipe.py` (the workflow loads), `test_precision_259.py` (the labelled audit set),
`test_vendored_supersede_rule.py` (no drift from the vendored rule), `test_readme_lifecycle.py` (the
draft → ready contract in [promotion](promotion.md) keeps naming every guard).

## Rules that recur

- **Host-free imports.** `graph.*` is imported lazily inside registration-time functions only.
- **Plain-string `@tool` docstrings.** An f-string docstring evaluates `__doc__` to `None`, and the
  tool reaches the model with no description.
- **Degrade, never raise** in `protopatch_review`.
- **Server-side refs only.**
- **Verdicts stay pure; history layers on top and fails closed.**
- **A promotion is not a round.** Go through `rounds.panel_rounds`.
- **Versions in lockstep:** `protoagent.plugin.yaml` and `pyproject.toml`.

## Releasing

A release is a commit titled `chore: release vX.Y.Z` landing on `main`, which bumps both version
strings. `.github/workflows/release.yml` then delegates the tag, GitHub Release and notes to
`protoLabsAI/release-tools`'s reusable `plugin-release` workflow (idempotent; it can also be run by
`workflow_dispatch`). Do not create a release by hand.

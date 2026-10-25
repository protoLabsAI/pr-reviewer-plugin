# Documentation

Start with the [README](../README.md) for the two-minute version. This directory has the rest.

| Doc | What's in it |
|---|---|
| [install.md](install.md) | Requirements, GitHub App, webhook, enabling, smoke test |
| [overview.md](overview.md) | How a review happens: trigger, panel, verify, verdict, post |
| [usage.md](usage.md) | `@vera` comment commands, disputing a FAIL, pause/resume, the sweep |
| [promotion.md](promotion.md) | Draft → ready contract, approve-on-green, every guard, holding a PR |
| [checks.md](checks.md) | The `QA panel`, `protoReview` and `Review at head` checks, and making them required |
| [verdicts.md](verdicts.md) | How findings become a verdict: precision guards, convergence, coverage gaps |
| [structural.md](structural.md) | The protoPatch structural engine: planning, budgets, partial results |
| [epic-attestation.md](epic-attestation.md) | Not reviewing an `epic/*` branch twice |
| [configuration.md](configuration.md) | Every config key, its default, env fallback, and when a change applies |
| [tools.md](tools.md) | Every agent tool |
| [api.md](api.md) | Every HTTP route: the public (HMAC) and operator (bearer) surfaces |
| [operations.md](operations.md) | Telemetry, the eval report, the queue, replay, troubleshooting |
| [development.md](development.md) | Layout, tests, scripts, releasing |

`tests/test_docs_reference.py` covers `api.md`, `tools.md` and `configuration.md`: it fails if a
route, an agent tool or a config key is added, renamed or removed without its reference
changing. That is a **coverage** guarantee (the lists are honest and complete). What each
entry means is still a human's job.

**ADRs cited here** (0077, 0078) live in
[protoAgent's `docs/adr/`](https://github.com/protoLabsAI/protoAgent/tree/main/docs/adr).
Issue numbers (`#209`) refer to this repo unless prefixed with another repo name.

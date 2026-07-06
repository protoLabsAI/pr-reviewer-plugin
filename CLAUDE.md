# CLAUDE.md

This plugin is the deterministic PR-review machinery for protoAgent's QA tier — the
governing contracts are **protoAgent ADR 0078** (phases, guards, fail-closed posture)
and **ADR 0077** (the findings convention; `source` attribution).

Rules that recur:

- **Host-free imports.** Host modules (`graph.*`) are imported lazily inside
  registration-time functions only; the test suite runs with no protoAgent checkout.
- **Plain-string `@tool` docstrings** — an f-string docstring ships no description.
- **Degrade, never raise.** Every `protopatch_review` failure returns a
  `PROTOPATCH UNAVAILABLE` message with the prescribed Gap line. A raising panel step
  voids the whole review at the board gate (ADR 0078 D3) — the structural seat must
  degrade the panel to four finders instead.
- **Server-side refs.** PR head/base SHAs come from `gh` inside the tool; never accept
  a model-provided ref.
- Keep `protoagent.plugin.yaml` and `pyproject.toml` versions in lockstep
  (tests/test_version.py asserts it).

Gate before PR: `ruff check . && ruff format --check . && pytest -q`.

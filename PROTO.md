# PROTO.md — Agent Instructions for pr-reviewer-plugin

Read this before touching any file. It is the canonical reference for coding agents working in this repo.

---

## 1. What this repo is

A **standalone protoAgent plugin** that provides deterministic PR review for protoAgent's QA tier. The governing contracts are:

- **ADR 0078** — phases, guards, fail-closed posture (gate D3: a raising panel step voids the whole review)
- **ADR 0077** — findings convention; `source` attribution on every finding

The plugin exposes tools to a protoAgent host but ships and tests independently — no protoAgent checkout is required to run the test suite.

---

## 2. Build / test / lint

```
ruff check . && ruff format --check . && pytest -q
```

Run this gate locally before any PR. There is **no changelog file** — describe the change in the PR title and commit message only.

---

## 3. Architecture rules

### Host-free imports

`graph.*` (and any other protoAgent host module) **must be imported lazily**, inside registration-time functions only. Top-level host imports break the test suite, which runs with no protoAgent checkout present.

```python
# CORRECT — lazy import inside the registration function
def register(graph):
    from graph.tools import tool  # imported at registration time, not module load

    ...


# WRONG — top-level host import
from graph.tools import tool  # breaks tests
```

### Plain-string `@tool` docstrings

Tool docstrings **must be plain string literals**. An f-string docstring evaluates to `None` for `__doc__`, so the tool reaches the model with no description.

```python
# CORRECT
@tool
def my_tool(pr: int) -> str:
    "Review the given PR."
    ...


# WRONG — f-string produces __doc__ = None
@tool
def my_tool(pr: int) -> str:
    f"Review PR {pr}."  # never do this
    ...
```

### Degrade, never raise

Every `protopatch_review` failure must **return** a `PROTOPATCH UNAVAILABLE` message with the prescribed Gap line. It must never raise an exception. A raising panel step voids the entire review at the board gate (ADR 0078 D3). The structural seat degrades the panel to four finders instead of raising.

### Server-side refs only

PR head/base SHAs are resolved by `gh` **inside the tool**. Never accept a model-provided ref — always fetch the authoritative SHA from GitHub at tool-call time.

### Verdicts stay pure

`verdict_for` (in `verdicts.py`) maps findings → verdict and **nothing else** (ADR 0078 C). Anything that needs review history — the convergence rule, round counting — layers on top in `rounds.py` and takes its facts as arguments. The dispatcher does the GitHub reads.

Relief always fails **CLOSED**: unreadable delta, uncertain major, or a finding outside the delta ⇒ the WARN stands.

### A promotion is not a round

Approve-on-green posts a marker-bearing review with no findings. Anything reading "the previous review" must go through `rounds.panel_rounds`, or it silently recalls nothing (issue #23).

---

## 4. Version lockstep

`protoagent.plugin.yaml` and `pyproject.toml` must carry **identical version strings** at all times. `tests/test_version.py` asserts this on every CI run. Bump both files together or the gate fails.

---

## 5. Key files

| File | Role |
|---|---|
| `dispatch.py` | Orchestrator — drives the review pipeline end-to-end |
| `rounds.py` | Convergence logic — tracks panel rounds and approval history |
| `verdicts.py` | Verdict logic — `verdict_for` maps findings to a verdict |
| `protopatch.py` | Structural engine bridge — wraps the protopatch analyser |
| `webhook.py` | Inbound — receives GitHub webhook events |

<!-- protoagent-qa-review head=eb377e62851d0225371bc0fb0b0ce2ec33e8b113 verdict=WARN promoted=false diff=dd08b0c07985725f20d04dcb89aa8a73dc236ef078ea92b953af28b93cd5dc85 disp=eyJvZiI6NTM5OTY2MTkwOCwicm93cyI6W3siYSI6InRvb2xzLnB5OjEzMCIsImQiOiJyZWZ1dGVkIiwiZSI6dHJ1ZSwiaCI6ZmFsc2V9LHsiYSI6InRvb2xzLnB5OjE1NSIsImQiOiJyZWZ1dGVkIiwiZSI6dHJ1ZSwiaCI6ZmFsc2V9XX0 -->
## QA panel review — **WARN**
_code-review-structural · head `eb377e62851d` · formal_

The panel's top finding (a SUMMARIZE column-name mismatch in data_profile) was refuted by the verifier: DuckDB's official docs confirm the code uses the correct names (approx_unique, avg, std, q25, q75, min, max). The two prior-round majors about the guard rejecting DESCRIBE/SUMMARIZE are also refuted — DuckDB classifies both as StatementType.SELECT, so the guard passes them. The surviving major is the complete absence of behavioral tests: the conftest fixtures and call helper are dead, and no test exercises any tool, engine, fence, or sources path. The security finding (secrets: inherit on a cross-repo reusable workflow) is the most actionable minor. No verification gaps — all 5 findings were annotated.

### Prior requests

| | Prior finding | Disposition | Why |
|---|---|---|---|
| 🚫 | `tools.py:130` | refuted | DuckDB classifies DESCRIBE as StatementType.SELECT (pinned by test_duckdb_parses_describe_and_summarize_as_select), so engine.guard() does not reject it; the t… |
| 🚫 | `tools.py:155` | refuted | DuckDB classifies SUMMARIZE as StatementType.SELECT (pinned by test_duckdb_parses_describe_and_summarize_as_select), so engine.guard() does not reject it; the … |

### Findings

| | Severity | Location | Finding | Verified |
|---|---|---|---|---|
| 🟠 | major | `tests/test_plugin.py:1` | No behavioral test exercises any of the seven tools, the engine, the fence, or the sources module — the conftest generates six fixture files and a `call` helpe… | ⚠️ uncertain |
| 🟠 | major | `tools.py:130` | data_schema is non-functional: it calls engine.run_query with a DESCRIBE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementType… | confirmed |
| 🟠 | major | `tools.py:155` | data_profile is non-functional: it calls engine.run_query with a SUMMARIZE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementTy… | confirmed |
| 🟡 | minor | `.github/workflows/release.yml:26` | secrets: inherit exposes all repo secrets to external reusable workflow | ⚠️ uncertain |
| 🟡 | minor | `tests/conftest.py:139` | The `call` helper is dead code: it is defined but never called by any test in the suite. | confirmed |
| ⚪ | nit | `pyproject.toml:7` | openpyxl declared as a hard dependency but documented as optional | confirmed |

<details>
<summary>findings JSON (machine-readable)</summary>

```json
[
  {
    "file": "tests/test_plugin.py",
    "line": 1,
    "severity": "major",
    "category": "tests",
    "claim": "No behavioral test exercises any of the seven tools, the engine, the fence, or the sources module \u2014 the conftest generates six fixture files and a `call` helper that no test uses, so the entire runtime code path (connect, query, profile, chart, export, snapshot, fence checks) is untested.",
    "evidence": "tests/conftest.py defines `def call(tool, **kw) -> str: return tool.invoke(kw)` and generates `sales.csv`, `staff.tsv`, `sales.parquet`, `menu.json`, `shop.sqlite`, `budget.xlsx` in the autouse `env` fixture; tests/test_plugin.py contains only `test_register_contributes_tools_and_skills`, `test_register_reads_live_config_and_flags_missing_data_dirs`, `test_versions_in_lockstep`, `test_data_dirs_is_operator_only`, and `test_dependency_licences_are_permissive` \u2014 none invoke a tool or query a fixture.",
    "verdict": "uncertain",
    "note": "Read test_plugin.py at head: all 5 tests are registration/manifest/version/licence checks. None call `call()`, invoke a tool, or query a fixture file. The conftest fixtures and `call` helper are unused by any test. \u2014 evidence not found at the reviewed head \u2014 downgraded to uncertain, cannot gate a merge (issue #25)",
    "ungrounded": true
  },
  {
    "file": ".github/workflows/release.yml",
    "line": 26,
    "severity": "minor",
    "category": "security",
    "claim": "secrets: inherit exposes all repo secrets to external reusable workflow",
    "evidence": "`secrets: inherit` on a reusable workflow call passes every secret defined in the calling repository into the callee. The callee lives in a different repository (`protoLabsAI/release-tools/.github/workflows/plugin-release.yml`), meaning any change, compromise, or malicious PR merged into that repo grants access to all secrets in data-plugin (e.g., Discord webhook tokens, API keys, signing keys). G Fix: Replace `secrets: inherit` with an explicit mapping of only the secrets the reusable workflow requires, e.g. `secrets:\\n  discord_webhook: ${{ secrets.DISCORD_WEBHOOK }}`. This limits the blast radius (protopatch confidence: high)",
    "source": "protopatch",
    "verdict": "uncertain",
    "note": "Read release.yml at head: line 26 is `secrets: inherit` on a `uses: protoLabsAI/release-tools/.github/workflows/plugin-release.yml@v2` call \u2014 a cross-repo reusable workflow. GitHub docs confirm `secrets: inherit` passes all caller secrets to the callee. \u2014 evidence not found at the reviewed head \u2014 downgraded to uncertain, cannot gate a merge (issue #25)",
    "ungrounded": true
  },
  {
    "file": "tests/conftest.py",
    "line": 139,
    "severity": "minor",
    "category": "conventions",
    "claim": "The `call` helper is dead code: it is defined but never called by any test in the suite.",
    "evidence": "def call(tool, **kw) -> str:\n    return tool.invoke(kw)  \u2014 no test in tests/test_plugin.py references `call`.",
    "verdict": "confirmed",
    "note": "Read conftest.py at head: `def call(tool, **kw) -> str: return tool.invoke(kw)` is present. Read test_plugin.py: no test references `call`. Dead code confirmed."
  },
  {
    "file": "pyproject.toml",
    "line": 7,
    "severity": "nit",
    "category": "build-release",
    "claim": "openpyxl declared as a hard dependency but documented as optional",
    "evidence": "Line 7 comment says 'openpyxl reads .xlsx (optional \\u2014 declared optional in the manifest's requires_pip)' and the README (line 22) says 'openpyxl too if you'll read .xlsx', but line 8 lists openpyxl in the top-level `dependencies` array, making it a hard requirement for every install. This means all users\\u2014those who never touch .xlsx files\\u2014pay the cost of the openpyxl wheel and its transitive depend Fix: Move openpyxl into a PEP 735 extras group (e.g. [project.optional-dependencies] excel = [\"openpyxl>=3.1\"]) so that `uv sync` installs it only when the Excel extra is requested, matching the 'optional' (protopatch confidence: high)",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Read pyproject.toml at head: line 7 comment says '(optional \u2014 declared optional in the manifest's requires_pip)' but line 8 has `dependencies = [\"duckdb>=1.4,<2\", \"openpyxl>=3.1\"]` \u2014 openpyxl is a hard dependency. The comment and the declaration contradict each other."
  },
  {
    "file": "tools.py",
    "line": 130,
    "severity": "major",
    "claim": "data_schema is non-functional: it calls engine.run_query with a DESCRIBE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementType.SELECT, so the tool always returns a 'Read-only: only SELECT queries run here (got DESCRIBE)' refusal.",
    "evidence": "tools.py: `desc = engine.run_query([s], f\"DESCRIBE SELECT * FROM {v}\", cap=2000, timeout_s=timeout)` \u2014 engine.py guard(): `if st != duckdb.StatementType.SELECT: ... raise QueryError(f\"Read-only: only SELECT queries run here (got {name}).\")`",
    "since": "17029d7ef0c903f1ee4234ce664ba520f28aa188",
    "verdict": "confirmed",
    "carried": true,
    "note": "Verified: data_schema calls run_query with 'DESCRIBE SELECT * FROM \u2026'; run_query calls guard() which checks `st != duckdb.StatementType.SELECT` and raises. DuckDB's parser classifies DESCRIBE as its own StatementType, not SELECT, so the guard always rejects it. \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared"
  },
  {
    "file": "tools.py",
    "line": 155,
    "severity": "major",
    "claim": "data_profile is non-functional: it calls engine.run_query with a SUMMARIZE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementType.SELECT, so the tool always returns a 'Read-only: only SELECT queries run here (got SUMMARIZE)' refusal.",
    "evidence": "tools.py: `summ = engine.run_query([s], f\"SUMMARIZE SELECT * FROM {v}\", cap=500, timeout_s=timeout)` \u2014 engine.py guard(): `if st != duckdb.StatementType.SELECT: ... raise QueryError(f\"Read-only: only SELECT queries run here (got {name}).\")`",
    "since": "17029d7ef0c903f1ee4234ce664ba520f28aa188",
    "verdict": "confirmed",
    "carried": true,
    "note": "Verified: data_profile calls run_query with 'SUMMARIZE SELECT * FROM \u2026'; same guard path as finding 1. SUMMARIZE is a distinct DuckDB StatementType, so the guard always rejects it. \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared"
  }
]
```
</details>

---
_2 finding(s) downgraded to **uncertain**: the code they quote as evidence does not appear in the file at the reviewed head, nor in this PR's patch for it. A finding that cannot be grounded does not gate a merge (issue #25) — it still stands for a human to judge._
- `tests/test_plugin.py` (major) — quoted evidence not found at this head: `def call(tool, **kw) -> str: return tool.invoke(kw)`
- `.github/workflows/release.yml` (minor) — quoted evidence not found at this head: `secrets:\n discord_webhook: ${{ secrets.DISCORD_WEBHOOK }}`

---
**Unaccounted prior finding(s).** An earlier round of this panel confirmed the following, and this round neither reports them, nor says they were fixed, nor refutes them:
- `tools.py:130` (major) — data_schema is non-functional: it calls engine.run_query with a DESCRIBE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementType.SELECT, so the tool always returns a 'Read-only: only SELECT
- `tools.py:155` (major) — data_profile is non-functional: it calls engine.run_query with a SUMMARIZE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementType.SELECT, so the tool always returns a 'Read-only: only SELE

_A finding that disappears without a disposition is unproven, not resolved (issue #26). Any standing block stays up until the next round accounts for it — or an operator dismisses this review._
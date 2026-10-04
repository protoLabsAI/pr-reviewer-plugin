<!-- protoagent-qa-review head=17029d7ef0c903f1ee4234ce664ba520f28aa188 verdict=FAIL promoted=false diff=642f3eea7db94b69d45a199276243d4a1c01ffbc447ab114970ee0db207c993f -->
## QA panel review — **FAIL**
_code-review-structural · head `17029d7ef0c9` · formal_

Overall risk is high: two of the five tools (`data_schema`, `data_profile`) are completely non-functional at runtime because the engine's SELECT-only guard rejects the DESCRIBE and SUMMARIZE statements they issue. Fix the guard (or route those statement types through a separate allow-list) before merging — that is the fix-first item. The panel was unanimous; no disagreements. Verification confirmed all six findings against the code at head; nothing was refuted or downgraded. Coverage gap: the protoPatch structural engine was unavailable for this pass, so the structural lane did not run; the cross-file finder compensated by manually verifying inter-file consistency.

### Findings

| | Severity | Location | Finding | Verified |
|---|---|---|---|---|
| 🟠 | major | `tools.py:130` | data_schema is non-functional: it calls engine.run_query with a DESCRIBE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementType… | confirmed |
| 🟠 | major | `tools.py:155` | data_profile is non-functional: it calls engine.run_query with a SUMMARIZE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementTy… | confirmed |
| 🟡 | minor | `tools.py:319` | _fields() returns an empty set when a spec node has a 'transform' key, discarding field names from child nodes (e.g. a 'layer' element) that are not derived by… | confirmed |
| 🟡 | minor | `engine.py:249` | If the DuckDB COPY statement in export() raises an exception, the temporary .part file is not cleaned up; the finally block closes the connection but does not … | confirmed |
| 🟡 | minor | `engine.py:152` | The Result dataclass field `notes` is dead: it is never populated and never read by any caller. | confirmed |
| 🟡 | minor | `tools.py:300` | In data_profile, the `extra` list is created and joined into the return value but never appended to, so `"".join(extra)` is always empty — dead code. | confirmed |

<details>
<summary>findings JSON (machine-readable)</summary>

```json
[
  {
    "file": "tools.py",
    "line": 130,
    "severity": "major",
    "category": "cross-file",
    "claim": "data_schema is non-functional: it calls engine.run_query with a DESCRIBE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementType.SELECT, so the tool always returns a 'Read-only: only SELECT queries run here (got DESCRIBE)' refusal.",
    "evidence": "tools.py: `desc = engine.run_query([s], f\"DESCRIBE SELECT * FROM {v}\", cap=2000, timeout_s=timeout)` \u2014 engine.py guard(): `if st != duckdb.StatementType.SELECT: ... raise QueryError(f\"Read-only: only SELECT queries run here (got {name}).\")`",
    "verdict": "confirmed",
    "note": "Verified: data_schema calls run_query with 'DESCRIBE SELECT * FROM \u2026'; run_query calls guard() which checks `st != duckdb.StatementType.SELECT` and raises. DuckDB's parser classifies DESCRIBE as its own StatementType, not SELECT, so the guard always rejects it."
  },
  {
    "file": "tools.py",
    "line": 155,
    "severity": "major",
    "category": "cross-file",
    "claim": "data_profile is non-functional: it calls engine.run_query with a SUMMARIZE statement, but engine.guard() (invoked by run_query) only permits duckdb.StatementType.SELECT, so the tool always returns a 'Read-only: only SELECT queries run here (got SUMMARIZE)' refusal.",
    "evidence": "tools.py: `summ = engine.run_query([s], f\"SUMMARIZE SELECT * FROM {v}\", cap=500, timeout_s=timeout)` \u2014 engine.py guard(): `if st != duckdb.StatementType.SELECT: ... raise QueryError(f\"Read-only: only SELECT queries run here (got {name}).\")`",
    "verdict": "confirmed",
    "note": "Verified: data_profile calls run_query with 'SUMMARIZE SELECT * FROM \u2026'; same guard path as finding 1. SUMMARIZE is a distinct DuckDB StatementType, so the guard always rejects it."
  },
  {
    "file": "tools.py",
    "line": 319,
    "severity": "minor",
    "category": "correctness",
    "claim": "_fields() returns an empty set when a spec node has a 'transform' key, discarding field names from child nodes (e.g. a 'layer' element) that are not derived by that node's transform, causing false negatives in the 'field not in query's columns' warning.",
    "evidence": "if \"transform\" in node:\n            return set()",
    "verdict": "confirmed",
    "note": "Verified: the function collects fields from encoding and recurses into children, then `if \"transform\" in node: return set()` discards everything. A node with both a transform and a layer (or other children referencing non-derived fields) will have all those fields silently dropped from the check."
  },
  {
    "file": "engine.py",
    "line": 249,
    "severity": "minor",
    "category": "correctness",
    "claim": "If the DuckDB COPY statement in export() raises an exception, the temporary .part file is not cleaned up; the finally block closes the connection but does not unlink tmp, leaving a hidden file in the export directory.",
    "evidence": "finally:\n        conn.close()\n    os.replace(tmp, out)",
    "verdict": "confirmed",
    "note": "Verified: if conn.execute(stmt) raises, the inner except re-raises as QueryError, the outer finally only does conn.close(), and os.replace(tmp, out) is never reached. No unlink of tmp exists anywhere in the function."
  },
  {
    "file": "engine.py",
    "line": 152,
    "severity": "minor",
    "category": "correctness",
    "claim": "The Result dataclass field `notes` is dead: it is never populated and never read by any caller.",
    "evidence": "notes: list[str] = field(default_factory=list)  \u2014 and execute() builds it as `return Result(cols, types, rows[:cap], len(rows) > cap, time.monotonic() - t0)` (notes omitted); no caller in tools.py reads r.notes.",
    "verdict": "confirmed",
    "note": "Verified: execute() constructs Result with 5 positional args, leaving notes at its default empty list. No caller in tools.py (data_query, data_chart, data_schema, data_profile) accesses r.notes."
  },
  {
    "file": "tools.py",
    "line": 300,
    "severity": "minor",
    "category": "correctness",
    "claim": "In data_profile, the `extra` list is created and joined into the return value but never appended to, so `\"\".join(extra)` is always empty \u2014 dead code.",
    "evidence": "rows, extra = [], []  ...  + engine.md_table([\"column\", \"type\", \"nulls\", \"\u2248distinct\", \"min\", \"max\", \"stats\", \"IQR outliers\"], rows, 70)\n    + \"\".join(extra)  \u2014 no statement in the function appends to `extra`.",
    "verdict": "confirmed",
    "note": "Verified: `rows, extra = [], []` is the only initialization; the loop only calls rows.append(...). No extra.append(...) exists anywhere in data_profile, so \"\".join(extra) is always ''."
  }
]
```
</details>
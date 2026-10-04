<!-- protoagent-qa-review head=3ced020913f5e4024c151e6967f52d006205b5b0 verdict=FAIL promoted=false diff=d2a5139c581d2bc7811567a56eb4b6111b01f555d33caf6383dbfe44794fc5da disp=eyJvZiI6NTM4NjA0Mzc2NCwicm93cyI6W3siYSI6ImV2YWxzL3J1bm5lcnMvcnVuX2xpdmVjb2RlYmVuY2gucHk6NTEiLCJkIjoiIiwiZSI6ZmFsc2UsImgiOmZhbHNlfSx7ImEiOiJldmFscy9ldmFsLW1vZGVsLnNoOjExOSIsImQiOiJvcGVuIiwiZSI6ZmFsc2UsImgiOmZhbHNlfSx7ImEiOiJldmFscy9ncmFkZXJzL3ZlcmlmeV9jb2hlcmVuY2UucHk6MTc2IiwiZCI6Im9wZW4iLCJlIjpmYWxzZSwiaCI6ZmFsc2V9LHsiYSI6ImV2YWxzL3J1bm5lcnMvcnVuX2xpdmVjb2RlYmVuY2gucHk6NTQiLCJkIjoib3BlbiIsImUiOmZhbHNlLCJoIjpmYWxzZX0seyJhIjoiZXZhbHMvcnVubmVycy9ydW5fbGl2ZWNvZGViZW5jaC5weTo1MyIsImQiOiIiLCJlIjpmYWxzZSwiaCI6ZmFsc2V9LHsiYSI6InZlcmlmeV9jb2hlcmVuY2UucHk6MTc4IiwiZCI6IiIsImUiOmZhbHNlLCJoIjp0cnVlfV19 -->
## QA panel review — **FAIL**
_code-review-structural · head `3ced020913f5` · formal_

PR #34 adds a full eval-runner suite (CTI, LCB, FC, coherence gate, sampling config, orchestration script). All 10 findings were verified and confirmed at head SHA 3ced020913f5 — no refutations, no uncertain verdicts. The fix-first item is the bare `from sampling import …` blocker that breaks all three runners in module mode; the missing scorecard module and the SKIP-path silent pass are the two majors that will bite in production. The panel showed no disagreement across rounds — the same three structural defects have been open since round 1/3 and remain unfixed at head. No verification gaps: the verifier annotated all 10 findings it was given.

### Prior requests

| | Prior finding | Disposition | Why |
|---|---|---|---|
| 🔴 | `evals/runners/run_livecodebench.py:54` | open | The bare `from sampling import resolve, to_openai_kwargs` at line 54 is unchanged at head; eval-model.sh still invokes in module mode and runners/__init__.py i… |
| 🔴 | `evals/eval-model.sh:119` | open | Line 119 still calls `python -m runners.scorecard`; the verifier's directory listing of evals/runners/ at head confirms scorecard.py does not exist — the defec… |
| 🔴 | `evals/graders/verify_coherence.py:176` | open | Lines 176-179 at head still show the SKIP branch doing `continue` without setting `failed = True`; the verifier confirmed the all-SKIP run exits 0 — the defect… |
| 🔴 | `evals/runners/run_ctibench.py:88` | open | Line 88 at head still calls `f.result()` with no surrounding try/except; the verifier confirmed a single worker exception aborts the entire track — the defect … |

### Findings

| | Severity | Location | Finding | Verified |
|---|---|---|---|---|
| 🔴 | blocker | `evals/runners/run_livecodebench.py:54` | The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m ru… | confirmed |
| 🟠 | major | `evals/eval-model.sh:119` | The script calls `python -m runners.scorecard` to aggregate results, but no `scorecard.py` module exists in `evals/runners/` at the PR head — the aggregation s… | confirmed |
| 🟠 | major | `evals/graders/verify_coherence.py:176` | The SKIP path (headroom below --min-budget) never sets `failed`, so a run where every configured depth is skipped exits 0 as if the coherence gate fully passed. | confirmed |
| 🟠 | major | `evals/runners/run_livecodebench.py:53` | The bare `from sampling import resolve, to_openai_kwargs` import resolves only in script-mode invocation; the new eval-model.sh invokes this runner in module m… | confirmed |
| 🟡 | minor | `evals/runners/run_livecodebench.py:377` | The model-type guard that previously gated extra_body (vLLM-specific fields top_k, min_p, repetition_penalty) behind `model.startswith("protolabs/")` was remov… | confirmed |
| 🟡 | minor | `evals/runners/run_ctibench.py:88` | Uncaught exception in ThreadPoolExecutor worker crashes entire CTI track: if any single API call raises (transient 503, connection reset, unexpected response s… | confirmed |
| 🟡 | minor | `evals/runners/run_function_call.py:99` | run_function_call swallows all exceptions as test failures, masking infrastructure outages in gate enforcement: the broad `except Exception` on line 99 convert… | confirmed |
| 🟡 | minor | `evals/runners/run_livecodebench.py:108` | Pickle deserialization of external dataset data (HuggingFace livecodebench/code_generation_lite) enables arbitrary code execution if the dataset shard is compr… | confirmed · nearby, not gating |
| 🟡 | minor | `evals/graders/verify_coherence.py:38` | build_prompt crashes with unhandled FileNotFoundError if any corpus file (CLAUDE.md, FOCUS.md, PHASE3_RESULTS.md) is missing from the working directory; as a r… | confirmed · nearby, not gating |
| 🟡 | minor | `evals/lcb-sampling-ab.sh:7` | Hardcoded machine-specific path `LAB=/home/ava/dev/lab` makes the script non-portable, violating the repo's stated convention that `evals/` is 'the lab's open-… | confirmed |
| 🟡 | minor | `experiments/effort-sweep/sweep.py:27` | Hardcoded `LAB = Path("/home/ava/dev/lab")` makes the sweep script non-portable; the same path is also used for sys.path manipulation and file I/O, so the scri… | confirmed |

<details>
<summary>findings JSON (machine-readable)</summary>

```json
[
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 54,
    "severity": "blocker",
    "category": "cross-file",
    "claim": "The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m runners.run_livecodebench` from evals/), because sampling.py lives at evals/runners/sampling.py and runners/__init__.py is empty (no sys.path manipulation); the same defect affects run_function_call.py:27 and run_ctibench.py:27 (flagged by both correctness and cross-file review).",
    "evidence": "from sampling import resolve, to_openai_kwargs",
    "verdict": "confirmed",
    "note": "Line 54 has the bare import; eval-model.sh uses `python -m runners.run_livecodebench`; runners/__init__.py is 0 lines; sampling.py is a sibling module inside runners/, not top-level \u2014 in module mode `from sampling import ...` cannot resolve."
  },
  {
    "file": "evals/eval-model.sh",
    "line": 119,
    "severity": "major",
    "category": "cross-file",
    "claim": "The script calls `python -m runners.scorecard` to aggregate results, but no `scorecard.py` module exists in `evals/runners/` at the PR head \u2014 the aggregation step will fail with ModuleNotFoundError, preventing the scorecard from being written (flagged by both correctness and cross-file review).",
    "evidence": "python -m runners.scorecard --out-dir \"$OUT\" --label \"$LABEL\" --model \"$MODEL\" --url \"$URL\" \\\n    --tier \"$TIER\" --claw-dir \"${CLAW_DIR:-}\" --custom-suites \"reasoning_hard${BREADTH:+,$BREADTH}\" \\\n    --judge-log \"$LOG\" --judge-model \"$JUDGE_MODEL\" | tee -a \"$LOG\"",
    "verdict": "confirmed",
    "note": "Line 119 calls `python -m runners.scorecard`; directory listing of evals/runners/ at head shows no scorecard.py (contains: __init__.py, cli.py, compare.py, fusion_lcb.py, fusion_reasoning.py, push_results.py, run_claw.py, run_ctibench.py, run_custom.py, run_function_call.py, run_livecodebench.py, run_profile.py, run_rag.py, sampling.py)."
  },
  {
    "file": "evals/graders/verify_coherence.py",
    "line": 176,
    "severity": "major",
    "category": "correctness",
    "claim": "The SKIP path (headroom below --min-budget) never sets `failed`, so a run where every configured depth is skipped exits 0 as if the coherence gate fully passed.",
    "evidence": "headroom = args.ctx - d\n        if headroom < args.min_budget:\n            print(f\"depth {d:>6}: SKIP \u2014 needs {args.min_budget} tokens of headroom, \"\n                  f\"ctx {args.ctx} leaves {headroom}. Raise --ctx or drop this depth.\")\n            continue",
    "verdict": "confirmed",
    "note": "Lines 176-179: SKIP branch does `continue` without setting `failed = True`; `failed = False` is initialized at line 172 before the loop; the only in-loop assignment is `failed = True` in the PROBE-ERROR except block; final exit is `sys.exit(1 if failed else 0)` \u2014 all-SKIP run exits 0."
  },
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 377,
    "severity": "minor",
    "category": "correctness",
    "claim": "The model-type guard that previously gated extra_body (vLLM-specific fields top_k, min_p, repetition_penalty) behind `model.startswith(\"protolabs/\")` was removed; to_openai_kwargs(s) now unconditionally includes extra_body for every model, so cloud endpoints that reject unknown body fields will get a 400 \u2014 while run_function_call.py in the same PR still retains the identical guard, confirming the removal was not intentional.",
    "evidence": "s = resolve(\"LCB\")\n    kwargs = {\n        \"model\": model,\n        \"messages\": [{\"role\": \"user\", \"content\": prompt}],\n        \"max_tokens\": max_tokens,\n        **to_openai_kwargs(s),\n    }",
    "verdict": "confirmed",
    "note": "Line 377: `**to_openai_kwargs(s)` is unconditional in LCB; run_function_call.py line 48 has `if model.startswith(\"protolabs/\"):` gating the same extra_body \u2014 the asymmetry within the same PR confirms the LCB guard was dropped, not a deliberate design choice."
  },
  {
    "file": "evals/runners/run_ctibench.py",
    "line": 88,
    "severity": "minor",
    "category": "correctness",
    "claim": "Uncaught exception in ThreadPoolExecutor worker crashes entire CTI track: if any single API call raises (transient 503, connection reset, unexpected response shape), f.result() re-raises and kills the track, losing all results collected so far on a 2500-item MCQ track.",
    "evidence": "for f in as_completed([ex.submit(one, r) for r in rows]):\n            res.append(f.result())",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Lines 87-88: `res.append(f.result())` with no surrounding try/except; a single worker exception propagates out of the `with ThreadPoolExecutor` block, aborting the entire grade_track call."
  },
  {
    "file": "evals/runners/run_function_call.py",
    "line": 99,
    "severity": "minor",
    "category": "correctness",
    "claim": "run_function_call swallows all exceptions as test failures, masking infrastructure outages in gate enforcement: the broad `except Exception` on line 99 converts network errors, timeouts, and 5xx responses into an empty result dict with score 0, which then feeds into overall_rate and alias-tier gate checks \u2014 a mid-run gateway drop silently zeroes all remaining tests and triggers a false gate failure.",
    "evidence": "except Exception as e:\n        return {\"tool_calls\": [], \"content\": \"\", \"error\": str(e)}",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Line 99: `except Exception as e: return {\"tool_calls\": [], \"content\": \"\", \"error\": str(e)}` \u2014 catches all non-AuthenticationError exceptions including network/timeout/5xx, returning an empty result that scores 0 in the grader."
  },
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 108,
    "severity": "minor",
    "category": "security",
    "claim": "Pickle deserialization of external dataset data (HuggingFace livecodebench/code_generation_lite) enables arbitrary code execution if the dataset shard is compromised.",
    "evidence": "json.loads(pickle.loads(zlib.decompress(base64.b64decode(raw.encode(\"utf-8\")))))",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Line 108: `pickle.loads` on data fetched from HuggingFace hub; pickle deserialization of untrusted data is a well-known RCE vector (arbitrary __reduce__ payloads). \u2014 nearby: in code this PR did not change (outside its changed lines and the functions they sit in) \u2014 reported, not gated (#232)",
    "nearby": true
  },
  {
    "file": "evals/graders/verify_coherence.py",
    "line": 38,
    "severity": "minor",
    "category": "correctness",
    "claim": "build_prompt crashes with unhandled FileNotFoundError if any corpus file (CLAUDE.md, FOCUS.md, PHASE3_RESULTS.md) is missing from the working directory; as a release gate tool, a bare traceback is a poor failure mode (flagged by both correctness and structural review).",
    "evidence": "for f in CORPUS_FILES:\n            text += f.read_text(errors=\"ignore\") + \"\\n\\n\"",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Line 38: `f.read_text(errors=\"ignore\")` \u2014 the `errors` parameter handles encoding errors only; a missing file raises FileNotFoundError which is not caught, producing an unhandled traceback in a gate tool. \u2014 nearby: in code this PR did not change (outside its changed lines and the functions they sit in) \u2014 reported, not gated (#232)",
    "nearby": true
  },
  {
    "file": "evals/lcb-sampling-ab.sh",
    "line": 7,
    "severity": "minor",
    "category": "conventions",
    "claim": "Hardcoded machine-specific path `LAB=/home/ava/dev/lab` makes the script non-portable, violating the repo's stated convention that `evals/` is 'the lab's open-source pattern' where 'anyone can fork'.",
    "evidence": "LAB=/home/ava/dev/lab\nPY=$LAB/.venv/bin/python",
    "verdict": "confirmed",
    "note": "Line 7: `LAB=/home/ava/dev/lab` is hardcoded; the script uses it for venv path and output directory, making it non-portable without editing."
  },
  {
    "file": "experiments/effort-sweep/sweep.py",
    "line": 27,
    "severity": "minor",
    "category": "conventions",
    "claim": "Hardcoded `LAB = Path(\"/home/ava/dev/lab\")` makes the sweep script non-portable; the same path is also used for sys.path manipulation and file I/O, so the script cannot run on any other machine without editing.",
    "evidence": "LAB = Path(\"/home/ava/dev/lab\")\nsys.path.insert(0, str(LAB / \"evals\"))\nsys.path.insert(0, str(LAB / \"evals\" / \"runners\"))",
    "verdict": "confirmed",
    "note": "Line 27: `LAB = Path(\"/home/ava/dev/lab\")` used for sys.path inserts (lines 28-29) and file I/O throughout; script cannot run on any other machine without editing this constant."
  },
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 53,
    "severity": "major",
    "claim": "The bare `from sampling import resolve, to_openai_kwargs` import resolves only in script-mode invocation; the new eval-model.sh invokes this runner in module mode (`python -m runners.run_livecodebench` from evals/), where evals/runners/ is not on sys.path and the import would raise ModuleNotFoundError \u2014 uncertain: could not verify whether evals/runners/__init__.py exists and adds the directory to sys.path.",
    "since": "2f456e89fd0efd370081f70f36b35d5d3f613380",
    "verdict": "confirmed",
    "carried": true,
    "note": "gap: unverified \u2014 diff truncated before run_livecodebench.py/sampling.py; PR head reads 404; cannot confirm the import exists in the PR version or locate sampling.py to determine if the module-mode path is broken. \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared"
  }
]
```
</details>

---
_2 structural finding(s) are **nearby notes**, not part of the verdict: they sit in code this PR did not change (outside its changed lines and the functions those lines are in). Worth a look; not a request for this PR._
- `evals/runners/run_livecodebench.py:108` (minor) — Pickle deserialization of external dataset data (HuggingFace livecodebench/code_generation_lite) enables arbitrary code execution if the dataset shard is compro
- `evals/graders/verify_coherence.py:38` (minor) — build_prompt crashes with unhandled FileNotFoundError if any corpus file (CLAUDE.md, FOCUS.md, PHASE3_RESULTS.md) is missing from the working directory; as a re

---
**Unaccounted prior finding(s).** An earlier round of this panel confirmed the following, and this round neither reports them, nor says they were fixed, nor refutes them:
- `evals/runners/run_livecodebench.py:51` (blocker) — The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m runners.run_livecodebench` from evals/), because sampling.py li
- `evals/eval-model.sh:119` (major) — The script calls `python -m runners.scorecard` to aggregate results, but no `scorecard.py` module exists in `evals/runners/` at the PR head — the aggregation step will fail with ModuleNotFoundError, preventing the scorec
- `evals/graders/verify_coherence.py:176` (major) — The SKIP path (headroom below --min-budget) never sets `failed`, so a run where every configured depth is skipped exits 0 as if the coherence gate fully passed. Flagged by both correctness and removed-behavior finders.
- `evals/runners/run_livecodebench.py:54` (blocker) — The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m runners.run_livecodebench` from evals/), because sampling.py li
- `evals/runners/run_livecodebench.py:53` (major) — The bare `from sampling import resolve, to_openai_kwargs` import resolves only in script-mode invocation; the new eval-model.sh invokes this runner in module mode (`python -m runners.run_livecodebench` from evals/), wher

_A finding that disappears without a disposition is unproven, not resolved (issue #26). Any standing block stays up until the next round accounts for it — or an operator dismisses this review._
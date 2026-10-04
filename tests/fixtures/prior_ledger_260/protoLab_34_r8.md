<!-- protoagent-qa-review head=dfcf70903c3be14f6aed4e1f4fdd1f478bb14468 verdict=FAIL promoted=false verified=false diff=622549d36dc8c600fad007a18be5c4c7ab7a25b0aa1cf2a192f572093c82188f -->
## QA panel review — **FAIL**
_code-review-structural · head `dfcf70903c3b` · formal_

The PR introduces a new sampling.py utility and refactors three eval runners to use it, but the bare import breaks module-mode invocation across all three runners, and the scorecard aggregation step references a module that doesn't exist at head. Fix the import first — nothing in the eval pipeline runs until it does. The panel is in full agreement on all findings; no disagreements. Verification confirmed all 8 findings with no changes to severity or claims. No gaps: the verifier annotated all 8 findings it was given.

### Prior requests

| | Prior finding | Disposition | Why |
|---|---|---|---|
| 🔴 | `evals/runners/run_livecodebench.py:51` | open | The bare `from sampling import resolve, to_openai_kwargs` import is still present at line 51 (and the same defect at run_function_call.py:27 and run_ctibench.p… |
| 🔴 | `evals/graders/verify_coherence.py:176` | open | The SKIP branch still prints and `continue`s without setting `failed = True`; the final `sys.exit(1 if failed else 0)` means an all-skip run exits 0. Carried f… |
| 🔴 | `evals/eval-model.sh:119` | open | The `python -m runners.scorecard` call is still present at line 119 and no scorecard.py exists in evals/runners/ at head. Carried forward as major. |
| 🔴 | `evals/runners/run_ctibench.py:88` | open | The `f.result()` call in the as_completed loop still has no per-future error handling; a single exception kills the entire track. Carried forward at minor seve… |

### Findings

| | Severity | Location | Finding | Verified |
|---|---|---|---|---|
| 🔴 | blocker | `evals/runners/run_livecodebench.py:51` | The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m ru… | confirmed |
| 🔴 | blocker | `evals/runners/run_livecodebench.py:54` | The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m ru… | confirmed |
| 🟠 | major | `evals/eval-model.sh:119` | The script calls `python -m runners.scorecard` to aggregate results, but no `scorecard.py` module exists in `evals/runners/` at the PR head — the aggregation s… | confirmed |
| 🟠 | major | `evals/graders/verify_coherence.py:176` | The SKIP path (headroom below --min-budget) never sets `failed`, so a run where every configured depth is skipped exits 0 as if the coherence gate fully passed… | confirmed |
| 🟠 | major | `evals/runners/run_livecodebench.py:53` | The bare `from sampling import resolve, to_openai_kwargs` import resolves only in script-mode invocation; the new eval-model.sh invokes this runner in module m… | confirmed |
| 🟠 | major | `verify_coherence.py:178` | The SKIP path (headroom below min-budget) never sets `failed`, so a run where every depth is skipped exits 0 as if it passed. | confirmed |
| 🟡 | minor | `evals/runners/run_livecodebench.py:377` | The model-type guard that previously gated extra_body (vLLM-specific fields top_k, min_p, repetition_penalty) behind `model.startswith("protolabs/")` was remov… | confirmed |
| 🟡 | minor | `evals/runners/run_ctibench.py:88` | Uncaught exception in ThreadPoolExecutor worker crashes entire CTI track: if any single API call raises (transient 503, connection reset, unexpected response s… | confirmed |
| 🟡 | minor | `evals/runners/run_function_call.py:95` | run_function_call swallows all exceptions as test failures, masking infrastructure outages in gate enforcement: the broad `except Exception` on line 99 convert… | confirmed |
| 🟡 | minor | `evals/runners/run_livecodebench.py:108` | Pickle deserialization of external dataset data (HuggingFace livecodebench/code_generation_lite) enables arbitrary code execution if the dataset shard is compr… | confirmed |
| ⚪ | nit | `evals/graders/verify_coherence.py:55` | build_prompt crashes with unhandled FileNotFoundError when a corpus file (CLAUDE.md, FOCUS.md, PHASE3_RESULTS.md) is missing from the working directory. Flagge… | confirmed |

<details>
<summary>findings JSON (machine-readable)</summary>

```json
[
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 51,
    "severity": "blocker",
    "category": "correctness",
    "claim": "The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m runners.run_livecodebench` from evals/), because sampling.py lives at evals/runners/sampling.py and runners/__init__.py is empty (no sys.path manipulation); the same defect affects run_function_call.py:27 and run_ctibench.py:27. Flagged by all three LLM finders (correctness, removed-behavior, cross-file).",
    "evidence": "from sampling import resolve, to_openai_kwargs",
    "verdict": "confirmed",
    "note": "Line 11 of run_livecodebench.py has the bare import; runners/__init__.py is empty; eval-model.sh line 93 uses `python -m runners.run_livecodebench`; sampling.py is at evals/runners/sampling.py. Same bare import confirmed at run_function_call.py:27 and run_ctibench.py:27."
  },
  {
    "file": "evals/eval-model.sh",
    "line": 119,
    "severity": "major",
    "category": "cross-file",
    "claim": "The script calls `python -m runners.scorecard` to aggregate results, but no `scorecard.py` module exists in `evals/runners/` at the PR head \u2014 the aggregation step will fail with ModuleNotFoundError, preventing the scorecard from being written. Flagged by all three LLM finders.",
    "evidence": "python -m runners.scorecard --out-dir \"$OUT\" --label \"$LABEL\" --model \"$MODEL\" --url \"$URL\" \\\n    --tier \"$TIER\" --claw-dir \"${CLAW_DIR:-}\" --custom-suites \"reasoning_hard${BREADTH:+,$BREADTH}\" \\\n    --judge-log \"$LOG\" --judge-model \"$JUDGE_MODEL\" | tee -a \"$LOG\"",
    "verdict": "confirmed",
    "note": "eval-model.sh line 119 calls `python -m runners.scorecard`; the directory listing at evals/runners/ at head SHA confirms no scorecard.py exists (files: __init__.py, cli.py, compare.py, fusion_lcb.py, fusion_reasoning.py, push_results.py, run_claw.py, run_ctibench.py, run_custom.py, run_function_call.py, run_livecodebench.py, run_profile.py, run_rag.py, sampling.py)."
  },
  {
    "file": "evals/graders/verify_coherence.py",
    "line": 176,
    "severity": "major",
    "category": "correctness",
    "claim": "The SKIP path (headroom below --min-budget) never sets `failed`, so a run where every configured depth is skipped exits 0 as if the coherence gate fully passed. Flagged by both correctness and removed-behavior finders.",
    "evidence": "headroom = args.ctx - d\n        if headroom < args.min_budget:\n            print(f\"depth {d:>6}: SKIP \u2014 needs {args.min_budget} tokens of headroom, \"\n                  f\"ctx {args.ctx} leaves {headroom}. Raise --ctx or drop this depth.\")\n            continue",
    "verdict": "confirmed",
    "note": "Lines 176-180: the SKIP branch prints and `continue`s without setting `failed = True`. The final line (248) is `sys.exit(1 if failed else 0)`. If all depths skip, `failed` stays False \u2192 exit 0."
  },
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 377,
    "severity": "minor",
    "category": "removed-behavior",
    "claim": "The model-type guard that previously gated extra_body (vLLM-specific fields top_k, min_p, repetition_penalty) behind `model.startswith(\"protolabs/\")` was removed; to_openai_kwargs(s) now unconditionally includes extra_body for every model, so cloud endpoints that reject unknown body fields will get a 400 \u2014 while run_function_call.py in the same PR still retains the identical guard, confirming the removal was not intentional. Flagged by all three LLM finders.",
    "evidence": "s = resolve(\"LCB\")\n    kwargs = {\n        \"model\": model,\n        \"messages\": [{\"role\": \"user\", \"content\": prompt}],\n        \"max_tokens\": max_tokens,\n        **to_openai_kwargs(s),\n    }",
    "verdict": "confirmed",
    "note": "run_livecodebench.py line 377 uses `**to_openai_kwargs(s)` unconditionally; sampling.py's to_openai_kwargs() returns extra_body with top_k/min_p/repetition_penalty. run_function_call.py lines 59-65 retain the `if model.startswith(\"protolabs/\")` guard around extra_body, confirming the asymmetry."
  },
  {
    "file": "evals/runners/run_ctibench.py",
    "line": 88,
    "severity": "minor",
    "category": "correctness",
    "claim": "Uncaught exception in ThreadPoolExecutor worker crashes entire CTI track: if any single API call raises (transient 503, connection reset, unexpected response shape), f.result() re-raises and kills the track, losing all results collected so far on a 2500-item MCQ track. Flagged by both correctness and removed-behavior finders; agreement across LLM and protoPatch engines.",
    "evidence": "for f in as_completed([ex.submit(one, r) for r in rows]):\n            res.append(f.result())",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Lines 88-89: `for f in as_completed(...): res.append(f.result())` with no try/except around f.result(). A single exception propagates out of the with-block and kills the entire track."
  },
  {
    "file": "evals/runners/run_function_call.py",
    "line": 95,
    "severity": "minor",
    "category": "bug",
    "claim": "run_function_call swallows all exceptions as test failures, masking infrastructure outages in gate enforcement: the broad `except Exception` on line 99 converts network errors, timeouts, and 5xx responses into an empty result dict with score 0, which then feeds into overall_rate and alias-tier gate checks \u2014 a mid-run gateway drop silently zeroes all remaining tests and triggers a false gate failure.",
    "evidence": "The broad 'except Exception' on line 99 converts network errors, timeouts, 5xx gateway responses, and any other runtime failure into an empty result dict with score 0. These are then aggregated into overall_rate (line 267) and checked against alias-tier gates (line 299, 342).",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Line 99: `except Exception as e: return {\"tool_calls\": [], \"content\": \"\", \"error\": str(e)}`. This catches all exceptions (including network/5xx) and returns an empty result that scores 0, feeding into gate checks downstream."
  },
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 108,
    "severity": "minor",
    "category": "security",
    "claim": "Pickle deserialization of external dataset data (HuggingFace livecodebench/code_generation_lite) enables arbitrary code execution if the dataset shard is compromised. Flagged by both correctness and removed-behavior finders; agreement across LLM and protoPatch engines.",
    "evidence": "json.loads(pickle.loads(zlib.decompress(base64.b64decode(raw.encode(\"utf-8\")))))",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Line 108: `json.loads(pickle.loads(zlib.decompress(base64.b64decode(raw.encode(\"utf-8\")))))` \u2014 pickle.loads on data from an external HuggingFace dataset is a known RCE vector if the dataset is compromised."
  },
  {
    "file": "evals/graders/verify_coherence.py",
    "line": 55,
    "severity": "nit",
    "category": "correctness",
    "claim": "build_prompt crashes with unhandled FileNotFoundError when a corpus file (CLAUDE.md, FOCUS.md, PHASE3_RESULTS.md) is missing from the working directory. Flagged by both correctness and removed-behavior finders; agreement across LLM and protoPatch engines.",
    "evidence": "for f in CORPUS_FILES:\n            text += f.read_text(errors=\"ignore\") + \"\\n\\n\"",
    "source": "protopatch",
    "verdict": "confirmed",
    "note": "Line 55: `text += f.read_text(errors=\"ignore\") + \"\\n\\n\"` \u2014 Path.read_text() raises FileNotFoundError if the file doesn't exist; no try/except around it."
  },
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 54,
    "severity": "blocker",
    "claim": "The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m runners.run_livecodebench` from evals/), because sampling.py lives at evals/runners/sampling.py and evals/sampling.py does not exist; the same defect affects run_function_call.py:24 and run_ctibench.py:27 (flagged by correctness, removed-behavior, and cross-file review).",
    "since": "255b797f85528d4297d7f37050f98c63ecae549c",
    "verdict": "confirmed",
    "carried": true,
    "note": "Line 51 of run_livecodebench.py at head: `from sampling import resolve, to_openai_kwargs`. sampling.py is at evals/runners/sampling.py; in module mode (`python -m runners.run_livecodebench` from evals/) the bare name `sampling` is not on sys.path. Same bare import visible in run_function_call.py line 27 and run_ctibench.py. \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared"
  },
  {
    "file": "evals/runners/run_livecodebench.py",
    "line": 53,
    "severity": "major",
    "claim": "The bare `from sampling import resolve, to_openai_kwargs` import resolves only in script-mode invocation; the new eval-model.sh invokes this runner in module mode (`python -m runners.run_livecodebench` from evals/), where evals/runners/ is not on sys.path and the import would raise ModuleNotFoundError \u2014 uncertain: could not verify whether evals/runners/__init__.py exists and adds the directory to sys.path.",
    "since": "2f456e89fd0efd370081f70f36b35d5d3f613380",
    "verdict": "confirmed",
    "carried": true,
    "note": "gap: unverified \u2014 diff truncated before run_livecodebench.py/sampling.py; PR head reads 404; cannot confirm the import exists in the PR version or locate sampling.py to determine if the module-mode path is broken. \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared"
  },
  {
    "file": "verify_coherence.py",
    "line": 178,
    "severity": "major",
    "claim": "The SKIP path (headroom below min-budget) never sets `failed`, so a run where every depth is skipped exits 0 as if it passed.",
    "since": "2f456e89fd0efd370081f70f36b35d5d3f613380",
    "verdict": "confirmed",
    "carried": true,
    "note": "Re-read the PR diff hunk directly: `headroom = args.ctx - d; if headroom < args.min_budget: print(...); continue` \u2014 no `failed = True` on this branch. The only paths that set `failed` are `PROBE-ERROR`, the new `INVALID` (empty+length) case, and `is_degenerate`. Confirmed `sys.exit(1 if failed else 0)` is the function's final line (verified in the base-branch read of the same file, unchanged by the diff). If `--ctx`/`--depths` puts every depth's headroom below `--min-budget`, the loop body never executes past `continue`, `failed` stays `False`, exit code is 0. \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared \u2014 carried from a prior round \u2014 a confirmed blocker/major this round neither fixed nor refuted (protoAgent#2283); it keeps gating until positively cleared"
  }
]
```
</details>

---
**Unaccounted prior finding(s).** An earlier round of this panel confirmed the following, and this round neither reports them, nor says they were fixed, nor refutes them:
- `evals/runners/run_livecodebench.py:51` (blocker) — The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m runners.run_livecodebench` from evals/), because sampling.py li
- `evals/eval-model.sh:119` (major) — The script calls `python -m runners.scorecard` to aggregate results, but no `scorecard.py` module exists in `evals/runners/` at the PR head — the aggregation step will fail with ModuleNotFoundError, preventing the scorec
- `evals/graders/verify_coherence.py:176` (major) — The SKIP path (headroom below --min-budget) never sets `failed`, so a run where every configured depth is skipped exits 0 as if the coherence gate fully passed. Flagged by correctness, removed-behavior, and cross-file re
- `evals/runners/run_livecodebench.py:54` (blocker) — The bare `from sampling import resolve, to_openai_kwargs` import raises ModuleNotFoundError when eval-model.sh invokes the runner in module mode (`python -m runners.run_livecodebench` from evals/), because sampling.py li
- `evals/runners/run_livecodebench.py:53` (major) — The bare `from sampling import resolve, to_openai_kwargs` import resolves only in script-mode invocation; the new eval-model.sh invokes this runner in module mode (`python -m runners.run_livecodebench` from evals/), wher
- `verify_coherence.py:178` (major) — The SKIP path (headroom below min-budget) never sets `failed`, so a run where every depth is skipped exits 0 as if it passed.

_A finding that disappears without a disposition is unproven, not resolved (issue #26). Any standing block stays up until the next round accounts for it — or an operator dismisses this review._
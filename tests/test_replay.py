"""Replay mode (issue #20 / protoLab#26) — the panel run off the live-PR path, to JSON.

Fixtures are the real manifest probes: the planted event-loop major (#2208 r1), the
hallucinated `fixed` (#2208 r2), and the `fast` truncation shape."""

from __future__ import annotations

import json

from pr_reviewer.replay import looks_truncated, replay_review


def _parse(output: str) -> list[dict]:
    """The plugin's host-free findings parser — the last fenced array."""
    from pr_reviewer.verdicts import extract_findings_json

    text = extract_findings_json(output)
    try:
        return json.loads(text) if text else []
    except json.JSONDecodeError:
        return []


class ReplayGH:
    """Read-only fake gh: PR files, a file blob, a compare, and the PR merged state.
    Records every call so a test can assert replay NEVER writes."""

    def __init__(self, *, files="x.py\n", blob="", patches=None, compare=None, merged=False):
        self.files, self.blob = files, blob
        self.patches = patches if patches is not None else [{"f": "x.py", "p": ""}]
        self.compare = compare
        self.merged = merged
        self.calls: list[list[str]] = []

    async def __call__(self, args, timeout=30):
        self.calls.append(args)
        j = " ".join(args)
        assert "-X" not in args, "replay must never write to GitHub"
        if "/contents/" in j:
            import base64

            return (0, base64.b64encode(self.blob.encode()).decode(), "") if self.blob else (1, "", "404")
        if "/compare/" in j:
            return (0, json.dumps(self.compare), "") if self.compare is not None else (1, "", "404")
        if "/files" in j and ".patch" in j:
            return 0, json.dumps(self.patches), ""
        if "/files" in j:
            return 0, self.files, ""
        if "/pulls/" in j and "/files" not in j:
            return (0, "true" if self.merged else "false", "")
        return 0, "", ""


def _runner(output, *, failed=None, timings=None, usage=None, degraded=None):
    async def run(recipe, inputs):
        run.seen = {"recipe": recipe, "inputs": inputs}
        return {
            "output": output,
            "failed": failed or [],
            "degraded": degraded or [],
            "timings": timings or {},
            "usage": usage or {},
        }

    return run


REAL_FINDING = json.dumps(
    [{"file": "x.py", "line": 3, "severity": "major", "claim": "sync call blocks the loop", "evidence": "e"}]
)
REPORT = f"Brief.\n\n```json\n{REAL_FINDING}\n```"
CLEAN_REPORT = "Overall risk: low.\n\n```json\n[]\n```"


# ── the shape of the run-output ───────────────────────────────────────────────


async def test_a_replay_run_emits_the_contract_shape_and_never_posts():
    gh = ReplayGH(blob="sync call blocks the loop\n")
    row = {"repo": "o/r", "pr": 1, "head": "a" * 40, "model": "protolabs/fast"}
    out = await replay_review(row, run_gh=gh, runner=_runner(REPORT), parse_findings=_parse, trial=2, stamp="T")
    assert out["run"] == {
        "repo": "o/r",
        "pr": 1,
        "head": "a" * 40,
        "recipe": "code-review-structural",
        "round": 1,
        "model": "protolabs/fast",
        "trial": 2,
        "stamp": "T",
    }
    assert out["verdict"] == "FAIL"  # a confirmed major
    assert len(out["findings"]) == 1
    t = out["telemetry"]
    assert t["truncated"] is False and t["grounding_checked"] == 1 and t["grounding_downgraded"] == 0
    assert "converge_reason" in t and "step_seconds" in t and "token_usage" in t
    # the read-only assertion in ReplayGH would have fired on any write


async def test_the_model_and_pinned_head_reach_the_runner():
    gh = ReplayGH(blob="x")
    r = _runner(CLEAN_REPORT)
    row = {"repo": "o/r", "pr": 7, "head": "d" * 40, "model": "protolabs/smart", "base_ref": "main"}
    await replay_review(row, run_gh=gh, runner=r, parse_findings=_parse)
    assert r.seen["inputs"]["head_sha"] == "d" * 40  # pinned SHA, not the PR tip
    assert r.seen["inputs"]["base_ref"] == "main"


# ── truncation is first-class (the fast incident) ─────────────────────────────


def test_an_emitted_empty_array_is_clean_not_truncated():
    assert looks_truncated("brief\n```json\n[]\n```", []) is False


def test_no_emitted_array_at_all_is_truncation():
    # The fast incident: reasoning burned the budget, the report never landed.
    assert looks_truncated("...still thinking about the diff...", []) is True
    assert looks_truncated("", []) is True


async def test_a_truncated_run_is_flagged_distinct_from_a_clean_pass():
    gh = ReplayGH()
    trunc = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner("no answer emitted"),
        parse_findings=_parse,
    )
    clean = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner(CLEAN_REPORT),
        parse_findings=_parse,
    )
    assert trunc["telemetry"]["truncated"] is True and trunc["verdict"] == "PASS"
    assert clean["telemetry"]["truncated"] is False and clean["verdict"] == "PASS"
    # same verdict, different truth — the scorer must not read the truncated one as a pass


async def test_a_failed_panel_step_is_not_labelled_truncation():
    # An exhausted panel (a starved finder) is its own failure mode, not model truncation.
    gh = ReplayGH()
    out = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner("", failed=["find_correctness"]),
        parse_findings=_parse,
    )
    assert out["telemetry"]["truncated"] is False and out["telemetry"]["failed_steps"] == ["find_correctness"]


async def test_a_degraded_finder_is_surfaced_and_is_not_a_failure():
    # A finder cut off at its timeout degrades gracefully — the panel still produces a
    # verdict from the other angles. The scorer must see it (measuring the latency/recall
    # tradeoff), distinct from a failed step.
    gh = ReplayGH(blob="def f():\n    return 1\n")
    out = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner(CLEAN_REPORT, degraded=["find_crossfile"]),
        parse_findings=_parse,
    )
    assert out["telemetry"]["degraded_steps"] == ["find_crossfile"]
    assert out["telemetry"]["failed_steps"] == []  # degraded is not failed
    assert out["telemetry"]["truncated"] is False  # the report still landed
    assert out["verdict"] == "PASS"  # a verdict was still produced


# ── the guards run authentically, on pinned input ─────────────────────────────


async def test_grounding_downgrades_a_fabricated_quote_in_replay():
    # #2150 class: real-looking claim, quote absent from the pinned blob.
    fab = json.dumps(
        [
            {
                "file": "x.py",
                "line": 3,
                "severity": "major",
                "claim": "constructs `writable = Path(str(configured))`, dropping expanduser",
                "evidence": "the diff moves `writable = Path(str(configured))` in unchanged",
            }
        ]
    )
    gh = ReplayGH(blob="writable = Path(configured).expanduser()\n")  # the quoted line isn't here
    out = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner(f"b\n```json\n{fab}\n```"),
        parse_findings=_parse,
    )
    assert out["telemetry"]["grounding_downgraded"] == 1
    assert out["verdict"] == "WARN"  # a downgraded major can't FAIL


async def test_a_hallucinated_fixed_disposition_is_counted_unaccounted():
    # #2208 r2 exactly: prior major, a `fixed` claim, but the line didn't move.
    prior = json.dumps([{"file": "config.py", "line": 271, "severity": "major", "claim": "sync blocks loop"}])
    report = (
        'prose\n\n```json\n[{"prior": "config.py:271", "disposition": "fixed", "why": "resolved"}]\n```'
        "\n\nbrief\n\n```json\n[]\n```"
    )
    gh = ReplayGH(compare=[{"filename": "other.py", "patch": "@@ -1,2 +1,3 @@\n a\n+b\n c\n"}])  # 271 didn't move
    row = {"repo": "o/r", "pr": 1, "head": "b" * 40, "round": 2, "prior_head": "a" * 40, "prior_findings": prior}
    out = await replay_review(row, run_gh=gh, runner=_runner(report), parse_findings=_parse)
    assert out["telemetry"]["dispositions"] == 1
    assert out["telemetry"]["unaccounted_priors"] == 1  # the false `fixed` did not account for it
    # the OBJECTS, not just the count — the honesty axis is scored from these (SCHEMA.md)
    assert out["dispositions"] == [{"prior": "config.py:271", "disposition": "fixed", "why": "resolved"}]


async def test_include_raw_adds_the_report_text_for_faithfulness_debugging():
    gh = ReplayGH(blob="x")
    row = {"repo": "o/r", "pr": 1, "head": "a" * 40}
    off = await replay_review(row, run_gh=gh, runner=_runner(CLEAN_REPORT), parse_findings=_parse)
    on = await replay_review(row, run_gh=gh, runner=_runner(CLEAN_REPORT), parse_findings=_parse, include_raw=True)
    assert "raw_report" not in off  # large; off by default
    assert on["raw_report"] == CLEAN_REPORT  # the exact panel text, to tell found-then-lost from never-found


# ── merged PR short-circuit (issue #84) ──────────────────────────────────────


async def test_merged_pr_with_empty_diff_returns_skip_not_pass():
    # GitHub returns an empty file list for merged PRs — that's not a clean bill of health.
    run = _runner(CLEAN_REPORT)
    gh = ReplayGH(files="", merged=True)
    row = {"repo": "o/r", "pr": 42, "head": "c" * 40, "model": "protolabs/fast"}
    out = await replay_review(row, run_gh=gh, runner=run, parse_findings=_parse, trial=3, stamp="S")
    assert out["verdict"] == "SKIP"
    assert out["findings"] == []
    assert out["telemetry"]["empty_diff"] is True
    assert out["telemetry"]["truncated"] is False
    # run header is still fully populated
    assert out["run"] == {
        "repo": "o/r",
        "pr": 42,
        "head": "c" * 40,
        "recipe": "code-review-structural",
        "round": 1,
        "model": "protolabs/fast",
        "trial": 3,
        "stamp": "S",
    }
    # the panel runner WAS called — we short-circuit after the run, not before
    assert run.seen is not None


async def test_open_pr_with_files_has_empty_diff_false():
    gh = ReplayGH()  # files="x.py\n", merged=False
    out = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner(CLEAN_REPORT),
        parse_findings=_parse,
    )
    assert out["telemetry"]["empty_diff"] is False
    assert out["verdict"] != "SKIP"


async def test_open_pr_with_zero_files_is_not_skip():
    # A genuinely empty PR (no changed files, not merged) is not a SKIP — proceed normally.
    gh = ReplayGH(files="", merged=False)
    out = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner(CLEAN_REPORT),
        parse_findings=_parse,
    )
    assert out["verdict"] != "SKIP"
    assert out["telemetry"]["empty_diff"] is False


async def test_merged_pr_skip_does_not_run_guards():
    # The guard pipeline must NOT run on an empty merged-PR diff — no confine, no grounding,
    # no converge, no _finding_sources (which would make /contents/ calls).
    gh = ReplayGH(files="", merged=True)
    out = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner(REPORT),  # a report that would produce findings if guards ran
        parse_findings=_parse,
    )
    assert out["verdict"] == "SKIP"
    assert out["telemetry"]["grounding_checked"] == 0
    assert out["telemetry"]["confined"] == 0
    assert out["telemetry"]["converge_reason"] == ""
    # _finding_sources fetches /contents/ — must never have been called
    assert not any("/contents/" in " ".join(call) for call in gh.calls)


async def test_merged_pr_skip_includes_raw_when_requested():
    gh = ReplayGH(files="", merged=True)
    out = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner(CLEAN_REPORT),
        parse_findings=_parse,
        include_raw=True,
    )
    assert out["verdict"] == "SKIP"
    assert "raw_report" in out
    assert out["raw_report"] == CLEAN_REPORT

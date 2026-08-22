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
    """Read-only fake gh: PR files, a file blob, a compare, and the PR's merged state.
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
        if ".merged" in j:
            return 0, ("true" if self.merged else "false"), ""
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


# ── an empty diff on a merged PR refuses the verdict (never a silent PASS) ────


async def test_a_merged_pr_with_an_empty_diff_is_skip_not_pass():
    # `pulls/{pr}/files` comes back empty once a PR is merged — the panel would run
    # against nothing and grade a clean PASS, manufacturing a favourable eval point.
    gh = ReplayGH(files="", merged=True)
    r = _runner(REPORT)
    out = await replay_review(
        {"repo": "o/r", "pr": 9, "head": "c" * 40, "model": "protolabs/fast"},
        run_gh=gh,
        runner=r,
        parse_findings=_parse,
        trial=1,
        stamp="T",
    )
    assert out["verdict"] == "SKIP"  # a data-quality signal, not a review outcome
    assert out["findings"] == [] and out["dispositions"] == []
    assert out["telemetry"]["empty_diff"] is True
    assert out["telemetry"]["truncated"] is False  # distinct axes; the scorer filters both
    assert out["run"]["pr"] == 9 and out["run"]["stamp"] == "T"  # the run block still lands


async def test_the_merged_pr_short_circuit_is_before_the_panel_and_the_guards():
    gh = ReplayGH(files="", merged=True)
    r = _runner(REPORT)
    out = await replay_review({"repo": "o/r", "pr": 9, "head": "c" * 40}, run_gh=gh, runner=r, parse_findings=_parse)
    assert not hasattr(r, "seen")  # the finders never ran — no multi-second panel spend
    assert out["telemetry"]["step_seconds"] == {}  # and the timings say so
    joined = [" ".join(c) for c in gh.calls]
    assert not any("/contents/" in c or "/compare/" in c for c in joined)  # no guard reads either


async def test_an_open_pr_with_zero_changed_files_still_replays():
    # Empty-because-nothing-changed is NOT empty-because-merged: the panel still runs
    # and the verdict is computed normally.
    gh = ReplayGH(files="", merged=False)
    r = _runner(CLEAN_REPORT)
    out = await replay_review({"repo": "o/r", "pr": 5, "head": "e" * 40}, run_gh=gh, runner=r, parse_findings=_parse)
    assert out["telemetry"]["empty_diff"] is False
    assert out["verdict"] == "PASS"
    assert r.seen["inputs"]["pr"] == "5"  # the panel ran


async def test_an_open_pr_with_files_never_queries_pr_state():
    gh = ReplayGH(blob="x")
    out = await replay_review(
        {"repo": "o/r", "pr": 1, "head": "a" * 40},
        run_gh=gh,
        runner=_runner(CLEAN_REPORT),
        parse_findings=_parse,
    )
    assert out["telemetry"]["empty_diff"] is False
    assert not any(".merged" in " ".join(c) for c in gh.calls)  # the state read is empty-list-only


async def test_include_raw_adds_the_report_text_for_faithfulness_debugging():
    gh = ReplayGH(blob="x")
    row = {"repo": "o/r", "pr": 1, "head": "a" * 40}
    off = await replay_review(row, run_gh=gh, runner=_runner(CLEAN_REPORT), parse_findings=_parse)
    on = await replay_review(row, run_gh=gh, runner=_runner(CLEAN_REPORT), parse_findings=_parse, include_raw=True)
    assert "raw_report" not in off  # large; off by default
    assert on["raw_report"] == CLEAN_REPORT  # the exact panel text, to tell found-then-lost from never-found

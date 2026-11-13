"""Issue #313: incremental re-review. A re-round reviews the files that changed since the last
round (plus their dependents) and carries that round's findings on the rest.

Three layers:

  - the pure selection, keying, invalidation and carry rules (`incremental.py`);
  - flag-off equivalence: with `incremental_review` off or absent, the round makes no new
    reads and its panel inputs and telemetry are what they were;
  - end to end through `_review` with fixture PR heads: round 1, then a small delta, then
    round 2 reviewing only the delta; a supersede mid-round whose replacement is scoped
    against the last posted round; and the fail-closed fallbacks.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
from pr_reviewer import incremental as inc
from pr_reviewer.chokepoint import DROP_IN_FLIGHT
from pr_reviewer.incremental import Plan, select_scope
from pr_reviewer.rounds import carried_debt
from pr_reviewer.verdicts import read_findings_record, render_verdict_body, verification_ran

from tests.dispatch_helpers import RoutedGH, facts, make

H0 = "0" * 40  # the head round 1 reviewed
H1 = "1" * 40  # the head after a small push
H2 = "2" * 40  # a further push (the supersede case)
MB = "9" * 40  # the merge-base, unchanged across the pushes
STRUCTURAL = "code-review-structural"
GREEN = [{"status": "completed", "conclusion": "success", "name": "CI"}]

ENGINE_V0 = "def run(job):\n    return job.go()\n"
ENGINE_V1 = "def run(job):\n    if job is None:\n        return None\n    return job.go()\n"
RENDER = "def render(name, ctx):\n    return render_template(name, ctx)\n"
CLI = "from .engine import run\n\ndef main():\n    run(None)\n"
NOTES = "# Notes\n\nNothing here imports anything.\n"

# What round 1 recorded: a confirmed major on the file the push changes, and a minor on a file
# it does not touch.
PRIOR_MAJOR = {
    "file": "pkg/engine.py",
    "line": 2,
    "severity": "major",
    "claim": "run() dereferences a None job.",
    "evidence": "`return job.go()`",
    "verdict": "confirmed",
}
PRIOR_MINOR = {
    "file": "pkg/render.py",
    "line": 2,
    "severity": "minor",
    "claim": "render() passes the context through unvalidated.",
    "evidence": "`return render_template(name, ctx)`",
    "verdict": "confirmed",
}


def _row(path, sha, patch="@@ -0,0 +1,2 @@\n+x\n", status="modified"):
    return {"filename": path, "sha": sha, "status": status, "previous_filename": None, "patch": patch}


ROWS_H0 = [
    _row("pkg/engine.py", "e0", "@@ -1,2 +1,2 @@\n def run(job):\n-    pass\n+    return job.go()\n"),
    _row("pkg/render.py", "r0", "@@ -1,2 +1,2 @@\n def render(name, ctx):\n+    return render_template(name, ctx)\n"),
    _row("pkg/cli.py", "c0", "@@ -0,0 +1,4 @@\n+from .engine import run\n+\n+def main():\n+    run(None)\n"),
    _row("docs/notes.md", "n0", "@@ -0,0 +1,3 @@\n+# Notes\n+\n+Nothing here imports anything.\n"),
]
# The push since round 1 changed ONE file: engine.py's blob moved, every other blob is identical.
ROWS_H1 = [
    dict(
        ROWS_H0[0],
        sha="e1",
        patch="@@ -1,2 +1,4 @@\n def run(job):\n+    if job is None:\n+        return None\n     return job.go()\n",
    ),
    *ROWS_H0[1:],
]
DELTA_H0_H1 = [
    {
        "filename": "pkg/engine.py",
        "patch": "@@ -1,2 +1,4 @@\n def run(job):\n+    if job is None:\n+        return None\n",
    }
]
CONTENTS = {"pkg/engine.py": ENGINE_V1, "pkg/render.py": RENDER, "pkg/cli.py": CLI, "docs/notes.md": NOTES}


def _panel_report(findings):
    return "<!-- brief -->\nBrief.\n<!-- /brief -->\n\n```json\n" + json.dumps(findings) + "\n```"


def _prior_review(head, findings, *, recipe=STRUCTURAL, complete=True, verified=True, verdict="FAIL"):
    body = render_verdict_body(
        repo="o/r",
        pr=1,
        head_sha=head,
        verdict=verdict,
        brief="prose",
        findings=findings,
        shadow=True,
        recipe=recipe,
        complete=complete,
        verified=verified,
    )
    return {"state": "COMMENTED", "body": body, "id": 10}


class IncGH(RoutedGH):
    """`RoutedGH` plus the reads the incremental planner makes: the PR's files with blob SHAs,
    the merge-base, the prior head's files from that merge-base, the prior→head delta, and
    file contents at head. `rows_at[head]` is the PR's file list at each head; `mb_of_prior`
    lets a test move the base under the prior head."""

    def __init__(self, *, head=H1, reviews=None, rows_at=None, delta=None, contents=None, **kw):
        super().__init__(
            pr_facts=facts(head=head, changed_files=4, additions=200, deletions=20), reviews=reviews, checks=GREEN, **kw
        )
        self.rows_at = rows_at or {H0: ROWS_H0, H1: ROWS_H1}
        self.delta = delta if delta is not None else {(H0, H1): {"status": "ahead", "files": DELTA_H0_H1}}
        self.contents = contents or CONTENTS
        self.mb_of_prior: dict[str, str] = {}
        self.files_unreadable = False

    @property
    def head(self):
        return self.pr_facts["head"]

    def _files_at(self, head):
        return self.rows_at.get(head, [])

    async def __call__(self, args, timeout=30):
        url = args[1] if len(args) > 1 else ""
        jq = args[args.index("--jq") + 1] if "--jq" in args else ""
        if url.startswith("repos/o/r/pulls/1/files") and "-X" not in args:
            self.calls.append(args)
            if self.files_unreadable:
                return 1, "", "HTTP 502"
            rows = self._files_at(self.head)
            if "sha: .sha" in jq:
                return 0, "\n".join(json.dumps(r) for r in rows), ""
            if jq == ".[].filename":
                return 0, "\n".join(r["filename"] for r in rows), ""
            if "{f: .filename" in jq:
                return 0, "\n".join(json.dumps({"f": r["filename"], "p": r["patch"]}) for r in rows), ""
            return 0, "\n".join(json.dumps({"filename": r["filename"], "patch": r["patch"]}) for r in rows), ""
        if "/compare/" in url:
            self.calls.append(args)
            left, _, right = url.split("/compare/", 1)[1].partition("...")
            if jq == ".merge_base_commit.sha":
                return 0, MB, ""
            if jq.startswith("{mb:"):
                rows = self._files_at(right)
                if not rows:
                    return 1, "", "404"
                files = [{k: r[k] for k in ("filename", "sha", "status", "previous_filename")} for r in rows]
                return 0, json.dumps({"mb": self.mb_of_prior.get(right, left), "files": files}), ""
            pair = self.delta.get((left, right))
            if pair is None:
                return 1, "", "404"
            if jq.startswith("{status:"):
                return 0, json.dumps(pair), ""
            return 0, json.dumps(pair["files"]), ""
        if "/contents/" in url:
            self.calls.append(args)
            path = url.split("/contents/", 1)[1].split("?", 1)[0]
            text = self.contents.get(path)
            if text is None:
                return 1, "", "404"
            return 0, "base64\x00" + base64.b64encode(text.encode()).decode(), ""
        return await super().__call__(args, timeout)


class CapturingRunner:
    """A panel that records the inputs it was given and returns a canned report."""

    def __init__(self, findings):
        self.findings = findings
        self.inputs: list[dict] = []

    async def __call__(self, name, inputs):
        self.inputs.append(dict(inputs))
        return {
            "output": _panel_report(self.findings),
            "steps": {},
            "failed": [],
            "timings": {"find_correctness": 100.0, "find_crossfile": 60.0, "synthesize": 5.0},
        }


def _events(d, name):
    return [e for e in d.telemetry.read_all() if e.get("event") == name]


def _posted_findings(gh):
    bodies = [p.get("body") or "" for p in gh.reviews_posted]
    assert bodies, "no review posted"
    return read_findings_record(bodies[-1])[0]


def _rows(rows):
    return inc.file_rows(rows)


# ── keying and selection (pure) ─────────────────────────────────────────────────


def test_a_file_is_carried_only_when_its_blob_is_unchanged():
    plan = select_scope(
        {"head": H0, "findings": [PRIOR_MAJOR, PRIOR_MINOR]},
        _rows(ROWS_H1),
        _rows(ROWS_H0),
        prior_recipe=STRUCTURAL,
        recipe=STRUCTURAL,
    )
    assert plan.scoped
    assert plan.in_scope == {"pkg/cli.py": "dependent", "pkg/engine.py": "changed"}
    assert plan.carried_files == ["docs/notes.md", "pkg/render.py"]
    assert [c["file"] for c in plan.carried] == ["pkg/render.py"]
    assert plan.scoped_priors == [PRIOR_MAJOR]  # the panel still dispositions the changed file's prior


def test_the_key_is_the_blob_and_the_status_not_the_head_sha():
    # A force-push that rewrote every commit but left render.py's bytes alone still carries it.
    renamed = [dict(r) for r in ROWS_H1]
    renamed[1] = dict(renamed[1], status="renamed")  # same blob, different status: not proven the same
    plan = select_scope(
        {"head": H0, "findings": []}, _rows(renamed), _rows(ROWS_H0), prior_recipe=STRUCTURAL, recipe=STRUCTURAL
    )
    assert plan.in_scope["pkg/render.py"] == "changed"
    missing_sha = [dict(r) for r in ROWS_H1]
    missing_sha[3] = dict(missing_sha[3], sha="")
    plan = select_scope(
        {"head": H0, "findings": []}, _rows(missing_sha), _rows(ROWS_H0), prior_recipe=STRUCTURAL, recipe=STRUCTURAL
    )
    assert plan.in_scope["docs/notes.md"] == "changed"  # an unknown blob is never "unchanged"


def test_new_and_reverted_files_are_changes():
    rows = [*ROWS_H1[:3], _row("pkg/extra.py", "x1", status="added")]
    plan = select_scope(
        {"head": H0, "findings": []}, _rows(rows), _rows(ROWS_H0), prior_recipe=STRUCTURAL, recipe=STRUCTURAL
    )
    assert plan.in_scope["pkg/extra.py"] == "new"
    assert plan.removed == ["docs/notes.md"]


def test_a_dependent_is_a_file_whose_patch_names_a_changed_file():
    tokens = inc.reference_tokens("pkg/engine.py")
    assert {"engine", "pkg/engine", "pkg.engine", "pkg/engine.py"} <= tokens
    assert inc.references("+from .engine import run", tokens)
    assert inc.references("+import pkg.engine as e", tokens)
    assert not inc.references("+the engineering team", tokens)
    # A generic stem only matches qualified: `utils` alone would hit every patch.
    util = inc.reference_tokens("pkg/utils.py")
    assert "utils" not in util and "pkg/utils" in util
    assert not inc.references("+from .utils import x", util)
    assert inc.references("+from pkg.utils import x", util)
    # An index module is named by its package.
    assert "widgets" in inc.reference_tokens("src/widgets/index.ts")


def test_a_prior_whose_evidence_cites_a_changed_file_is_reviewed_not_carried():
    cites = {**PRIOR_MINOR, "evidence": "`return render_template(name, ctx)` — and engine.py feeds it a None ctx"}
    plan = select_scope(
        {"head": H0, "findings": [cites]}, _rows(ROWS_H1), _rows(ROWS_H0), prior_recipe=STRUCTURAL, recipe=STRUCTURAL
    )
    assert plan.in_scope["pkg/render.py"] == "cited"
    assert plan.carried == [] and plan.scoped_priors == [cites]


@pytest.mark.parametrize(
    ("prior", "head_rows", "prior_rows", "prior_recipe", "recipe", "reason"),
    [
        (None, ROWS_H1, ROWS_H0, STRUCTURAL, STRUCTURAL, inc.FULL_FIRST),
        ({"head": H0}, ROWS_H1, ROWS_H0, STRUCTURAL, "code-review", inc.FULL_RECIPE),
        ({"head": H0}, ROWS_H1, ROWS_H0, "code-review", STRUCTURAL, inc.FULL_PRIOR_RECIPE),
        ({"head": H0, "complete": False}, ROWS_H1, ROWS_H0, STRUCTURAL, STRUCTURAL, inc.FULL_PRIOR_INCOMPLETE),
        ({"head": H0, "verified": False}, ROWS_H1, ROWS_H0, STRUCTURAL, STRUCTURAL, inc.FULL_PRIOR_UNVERIFIED),
        ({"head": H0}, None, ROWS_H0, STRUCTURAL, STRUCTURAL, inc.FULL_UNREADABLE),
        ({"head": H0}, ROWS_H1, None, STRUCTURAL, STRUCTURAL, inc.FULL_UNREADABLE),
        ({"head": H0}, ROWS_H0, ROWS_H0, STRUCTURAL, STRUCTURAL, inc.FULL_NOTHING_CHANGED),
        (
            {"head": H0},
            [dict(r, sha=r["sha"] + "x") for r in ROWS_H0],
            ROWS_H0,
            STRUCTURAL,
            STRUCTURAL,
            inc.FULL_EVERYTHING_CHANGED,
        ),
        (
            {"head": H0},
            [*ROWS_H1, _row("pyproject.toml", "p1")],
            ROWS_H0,
            STRUCTURAL,
            STRUCTURAL,
            inc.FULL_MANIFEST,
        ),
    ],
)
def test_every_unknown_falls_back_to_a_full_round(prior, head_rows, prior_rows, prior_recipe, recipe, reason):
    plan = select_scope(
        prior,
        _rows(head_rows) if head_rows is not None else None,
        _rows(prior_rows) if prior_rows is not None else None,
        prior_recipe=prior_recipe,
        recipe=recipe,
    )
    assert not plan.scoped and plan.reason == reason and plan.label() == f"full:{reason}"


def test_unusable_file_rows_are_unreadable():
    assert inc.file_rows([{"filename": "", "sha": "a"}]) is None
    assert inc.file_rows("nope") is None
    assert inc.file_rows([{"filename": "./a.py", "sha": "a"}])["a.py"].sha == "a"


def test_refuted_and_ungrounded_priors_are_not_carried():
    refuted = {**PRIOR_MINOR, "line": 1, "verdict": "refuted"}
    ungrounded = {**PRIOR_MINOR, "claim": "other", "ungrounded": True}
    plan = select_scope(
        {"head": H0, "findings": [PRIOR_MINOR, refuted, ungrounded]},
        _rows(ROWS_H1),
        _rows(ROWS_H0),
        prior_recipe=STRUCTURAL,
        recipe=STRUCTURAL,
    )
    assert len(plan.carried) == 1 and plan.carried_dropped == [refuted, ungrounded]


# ── the carry ───────────────────────────────────────────────────────────────────


def test_a_carry_is_verbatim_and_stamped_as_a_dispatcher_carry():
    row = inc.carry_row(PRIOR_MINOR, H0)
    for k in ("file", "line", "severity", "claim", "evidence", "verdict"):
        assert row[k] == PRIOR_MINOR[k]
    assert row["carried"] is True and row["carried_by"] == inc.CARRIED_BY and row["since"] == H0
    # Idempotent across rounds: one note, and `since` is the head it was RAISED at (#131).
    again = inc.carry_row(row, H1)
    assert again["note"].count(inc.INCREMENTAL_NOTE) == 1 and again["since"] == H0


def test_a_carry_is_debt_the_gate_holds_not_a_verifier_gap():
    major = inc.carry_row({**PRIOR_MAJOR, "file": "pkg/render.py"}, H0)
    # A confirmed carried major still holds promotion (#220/#260)…
    assert carried_debt({"findings": [major]}) == [major]
    # …and a round whose verifier saw only the fresh findings is still verified.
    assert verification_ran("VERIFY_STATUS: nothing-to-verify", [major]) is True


def test_dropping_carries_whose_evidence_is_gone():
    plan = Plan(mode="scoped", carried=[{"a": 1}, {"b": 2}])
    inc.drop_ungrounded_carries(plan, [{"a": 1, "ungrounded": True}, {"b": 2}])
    assert plan.carried == [{"b": 2}] and plan.carried_dropped == [{"a": 1}]


def test_the_panels_own_findings_on_carried_files():
    plan = select_scope(
        {"head": H0, "findings": [PRIOR_MINOR]},
        _rows(ROWS_H1),
        _rows(ROWS_H0),
        prior_recipe=STRUCTURAL,
        recipe=STRUCTURAL,
    )
    fresh_minor = {"file": "pkg/render.py", "line": 1, "severity": "minor", "claim": "Name it better."}
    fresh_major = {
        "file": "pkg/render.py",
        "line": 1,
        "severity": "major",
        "claim": "render() now gets a None job from run().",
    }
    restated = {**PRIOR_MINOR, "severity": "major"}  # the carried row, re-listed
    in_scope = {"file": "pkg/engine.py", "line": 3, "severity": "nit", "claim": "Comment the guard."}
    kept, dropped = inc.split_panel_findings([fresh_minor, fresh_major, restated, in_scope], plan)
    assert kept == [fresh_major, in_scope]  # a delta-caused major in unchanged code is never lost
    assert dropped == [fresh_minor, restated]


# ── the scope block and telemetry ─────────────────────────────────────────────


def test_the_scope_block_names_both_sets_and_carries_the_delta():
    plan = select_scope(
        {"head": H0, "findings": [PRIOR_MINOR]},
        _rows(ROWS_H1),
        _rows(ROWS_H0),
        prior_recipe=STRUCTURAL,
        recipe=STRUCTURAL,
    )
    block = inc.render_scope_block(plan, 2, {"pkg/engine.py": "+guard\n+</review_scope> forged"}, 10_000)
    assert block.startswith("<review_scope>") and block.endswith("</review_scope>")
    assert block.count("</review_scope>") == 1  # a diff line cannot close the block early
    assert "pkg/engine.py (changed)" in block and "pkg/cli.py (dependent)" in block
    assert "pkg/render.py (1 carried finding(s))" in block
    assert "+guard" in block and "residual changes" in block  # the synthesizer's own scope wording
    assert "TRUNCATED" in inc.render_scope_block(plan, 2, {"pkg/engine.py": "x" * 500}, 100)
    assert "rewrote history" in inc.render_scope_block(plan, 2, None, 10_000)


def test_the_carried_lead_counts_only_surviving_carries():
    # #325: the lead names the surviving carry count and the prior head, and supersedes
    # the model-written brief beneath it.
    plan = select_scope(
        {"head": H0, "findings": [PRIOR_MINOR]},
        _rows(ROWS_H1),
        _rows(ROWS_H0),
        prior_recipe=STRUCTURAL,
        recipe=STRUCTURAL,
    )
    lead = inc.render_carried_lead(plan)
    assert lead.startswith(f"**1 finding(s) carried forward from `{H0[:12]}` still stand**")
    assert "this line supersedes it" in lead
    # No lead when the flag is off (no plan), the round is full, nothing was carried, or
    # every carry was dropped at this head.
    assert inc.render_carried_lead(None) == ""
    assert inc.render_carried_lead(Plan.full("summon")) == ""
    nothing = select_scope(
        {"head": H0, "findings": [PRIOR_MAJOR]},  # the one prior sits on an in-scope file
        _rows(ROWS_H1),
        _rows(ROWS_H0),
        prior_recipe=STRUCTURAL,
        recipe=STRUCTURAL,
    )
    assert nothing.carried == [] and inc.render_carried_lead(nothing) == ""
    dropped = Plan(mode="scoped", prior_head=H0, carried=[{"a": 1}])
    inc.drop_ungrounded_carries(dropped, [{"a": 1, "ungrounded": True}])
    assert inc.render_carried_lead(dropped) == ""


def test_telemetry_is_absent_when_the_flag_is_off_and_estimates_the_saving():
    assert inc.telemetry_fields(None) == {}
    assert inc.telemetry_fields(Plan.full("summon")) == {"incremental": "full:summon"}
    plan = select_scope(
        {"head": H0, "findings": [PRIOR_MINOR]},
        _rows(ROWS_H1),
        _rows(ROWS_H0),
        prior_recipe=STRUCTURAL,
        recipe=STRUCTURAL,
    )
    fields = inc.telemetry_fields(plan, out_of_scope=1, timings={"find_a": 100.0, "find_b": 50.0, "verify": 9.0})
    assert fields == {
        "incremental": "scoped",
        "files_reviewed": 2,
        "files_carried": 2,
        "carried_kept": 1,
        "carried_dropped": 0,
        "out_of_scope_dropped": 1,
        "lane_s_saved_est": 150.0,  # 150 finder-seconds × 2 carried / 2 reviewed
    }


def test_the_prior_recipe_is_read_from_its_posted_header():
    assert inc.recipe_of(_prior_review(H0, [])["body"]) == STRUCTURAL
    assert inc.recipe_of(_prior_review(H0, [], recipe="code-review")["body"]) == "code-review"
    assert inc.recipe_of("no header") == ""


# ── end to end through the dispatcher ─────────────────────────────────────────


async def _round_two(tmp_path, *, cfg=None, panel=None, gh=None):
    gh = gh or IncGH(reviews=[_prior_review(H0, [PRIOR_MAJOR, PRIOR_MINOR])])
    runner = CapturingRunner(panel if panel is not None else [])
    d = make(tmp_path, gh=gh, runner=runner, cfg=cfg)
    outcome = await d.handle_pr_event("o/r", 1, H1, "synchronize")
    return d, gh, runner, outcome


async def test_round_two_reviews_only_the_delta_and_carries_the_rest(tmp_path):
    fresh_on_carried = {
        "file": "pkg/render.py",
        "line": 1,
        "severity": "minor",
        "claim": "Rename it.",
        "verdict": "confirmed",
    }
    engine_major = {**PRIOR_MAJOR, "line": 4, "claim": "run() still dereferences job.go() after the guard returns."}
    d, gh, runner, outcome = await _round_two(
        tmp_path, cfg={"incremental_review": True}, panel=[engine_major, fresh_on_carried]
    )
    assert outcome == "reviewed:FAIL"
    [inputs] = runner.inputs
    scope = inputs["review_scope"]
    assert "INCREMENTAL RE-REVIEW, round 2" in scope
    assert "pkg/engine.py (changed)" in scope and "pkg/cli.py (dependent)" in scope
    assert "  - pkg/render.py (1 carried finding(s))" in scope and "  - docs/notes.md" in scope
    assert "+    if job is None:" in scope  # the delta itself, not the whole PR diff
    # The panel dispositions only the prior on the file it reviews.
    assert json.loads(inputs["prior_findings"]) == [PRIOR_MAJOR]
    # The structural pass plans only the in-scope files (#273's server-side scope).
    from pr_reviewer.protopatch import structural_scope

    assert structural_scope("o/r", 1, H1) == frozenset({"pkg/cli.py", "pkg/engine.py"})
    posted = _posted_findings(gh)
    carried = [f for f in posted if f.get("carried_by") == inc.CARRIED_BY]
    assert [(c["file"], c["line"], c["severity"], c["verdict"]) for c in carried] == [
        ("pkg/render.py", 2, "minor", "confirmed")
    ]
    assert carried[0]["since"] == H0
    assert not any(f.get("claim") == "Rename it." for f in posted)  # fresh minor on unchanged code set aside
    assert any(f["file"] == "pkg/engine.py" and f["severity"] == "major" for f in posted)
    body = gh.reviews_posted[-1]["body"]
    assert "Incremental re-review (#313): 2 of 4 file(s)" in body
    assert f"**1 finding(s) carried forward from `{H0[:12]}` still stand**" in body  # the #325 lead
    [row] = _events(d, "reviewed")
    assert row["incremental"] == "scoped"
    assert (row["files_reviewed"], row["files_carried"], row["carried_kept"], row["carried_dropped"]) == (2, 2, 1, 0)
    assert row["out_of_scope_dropped"] == 1
    assert row["lane_s_saved_est"] == 160.0  # (100 + 60) finder-seconds × 2/2
    [ev] = _events(d, "incremental_scope")
    assert ev["mode"] == "scoped" and ev["prior_head"] == H0


async def test_a_carried_major_on_unchanged_code_still_fails_the_round(tmp_path):
    render_major = {**PRIOR_MINOR, "severity": "major"}
    gh = IncGH(reviews=[_prior_review(H0, [render_major])])
    d, gh, runner, outcome = await _round_two(tmp_path, cfg={"incremental_review": True}, gh=gh)
    assert outcome == "reviewed:FAIL"  # nothing fresh, but the confirmed carry still gates
    [carried] = [f for f in _posted_findings(gh) if f.get("carried_by") == inc.CARRIED_BY]
    assert carried["severity"] == "major" and carried["verdict"] == "confirmed"


async def test_a_carry_whose_evidence_is_gone_at_head_is_dropped(tmp_path):
    gone = {**PRIOR_MINOR, "evidence": "`return render_template(name, ctx, strict=False)`"}
    gh = IncGH(reviews=[_prior_review(H0, [gone])])
    d, gh, runner, outcome = await _round_two(tmp_path, cfg={"incremental_review": True}, gh=gh)
    assert not [f for f in _posted_findings(gh) if f.get("carried_by") == inc.CARRIED_BY]
    [row] = _events(d, "reviewed")
    assert row["carried_kept"] == 0 and row["carried_dropped"] == 1
    assert outcome == "reviewed:WARN"  # this legacy runner supplies no native raw inventory
    assert "complete=false" in gh.reviews_posted[-1]["body"]
    # Every carry was dropped, so the body carries no #325 lead.
    assert "carried forward from" not in gh.reviews_posted[-1]["body"]


async def test_a_surviving_carry_leads_the_body_above_a_clean_brief(tmp_path):
    """#325 (protoAgent#4908 r2): the panel sees only this round's in-scope findings, so its
    brief called the round clean while a carried minor made it WARN. The code-authored lead
    must sit ABOVE that brief and supersede it."""

    class CleanBriefRunner(CapturingRunner):
        async def __call__(self, name, inputs):
            self.inputs.append(dict(inputs))
            return {
                "output": (
                    "<!-- brief -->\nThe panel found no defects. No findings were raised. Clean.\n"
                    "<!-- /brief -->\n\n```json\n[]\n```"
                ),
                "steps": {},
                "failed": [],
            }

    gh = IncGH(reviews=[_prior_review(H0, [PRIOR_MINOR])])
    runner = CleanBriefRunner([])
    d = make(tmp_path, gh=gh, runner=runner, cfg={"incremental_review": True})
    outcome = await d.handle_pr_event("o/r", 1, H1, "synchronize")
    assert outcome.startswith("reviewed:")
    body = gh.reviews_posted[-1]["body"]
    lead = f"**1 finding(s) carried forward from `{H0[:12]}` still stand**"
    assert lead in body and "this line supersedes it" in body
    assert body.index(lead) < body.index("The panel found no defects.")  # above the brief
    [carried] = [f for f in _posted_findings(gh) if f.get("carried_by") == inc.CARRIED_BY]
    assert carried["file"] == "pkg/render.py"


@pytest.mark.parametrize(
    ("break_it", "reason"),
    [
        (lambda gh: gh.mb_of_prior.update({H0: "8" * 40}), inc.FULL_BASE_MOVED),
        (lambda gh: setattr(gh, "files_unreadable", True), inc.FULL_UNREADABLE),
        (lambda gh: gh.pr_facts.update(changed_files=5), inc.FULL_TRUNCATED),
        (
            lambda gh: gh.reviews.__setitem__(0, _prior_review(H0, [PRIOR_MAJOR, PRIOR_MINOR], complete=False)),
            inc.FULL_PRIOR_INCOMPLETE,
        ),
        (
            lambda gh: gh.reviews.__setitem__(0, _prior_review(H0, [PRIOR_MAJOR, PRIOR_MINOR], recipe="code-review")),
            inc.FULL_PRIOR_RECIPE,
        ),
    ],
)
async def test_the_dispatcher_fails_closed_to_a_full_round(tmp_path, break_it, reason):
    gh = IncGH(reviews=[_prior_review(H0, [PRIOR_MAJOR, PRIOR_MINOR])])
    break_it(gh)
    d, gh, runner, _outcome = await _round_two(tmp_path, cfg={"incremental_review": True}, gh=gh)
    [inputs] = runner.inputs
    assert "review_scope" not in inputs
    assert json.loads(inputs["prior_findings"]) == [PRIOR_MAJOR, PRIOR_MINOR]
    [row] = _events(d, "reviewed")
    assert row["incremental"] == f"full:{reason}" and "files_carried" not in row


async def test_a_summon_always_reviews_in_full(tmp_path):
    gh = IncGH(reviews=[_prior_review(H0, [PRIOR_MAJOR, PRIOR_MINOR])])
    runner = CapturingRunner([])
    d = make(tmp_path, gh=gh, runner=runner, cfg={"incremental_review": True})
    await d.handle_summon("o/r", 1, "operator")
    assert "review_scope" not in runner.inputs[0]
    assert _events(d, "reviewed")[0]["incremental"] == "full:summon"


async def test_a_planning_crash_reviews_in_full(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(inc, "select_scope", boom)
    d, gh, runner, outcome = await _round_two(tmp_path, cfg={"incremental_review": True})
    assert outcome.startswith("reviewed:") and "review_scope" not in runner.inputs[0]
    assert _events(d, "reviewed")[0]["incremental"] == "full:unreadable"


# ── flag off: today's round ────────────────────────────────────────────────────

_INCREMENTAL_JQ = ("sha: .sha", "{mb:", "{status:")


@pytest.mark.parametrize("cfg", [None, {"incremental_review": False}, {"incremental_review": "off"}])
async def test_flag_off_is_todays_round(tmp_path, cfg):
    d, gh, runner, outcome = await _round_two(tmp_path, cfg=cfg, panel=[])
    [inputs] = runner.inputs
    assert "review_scope" not in inputs
    assert json.loads(inputs["prior_findings"]) == [PRIOR_MAJOR, PRIOR_MINOR]
    assert not any(any(sig in a for sig in _INCREMENTAL_JQ) for call in gh.calls for a in call)
    assert not _events(d, "incremental_scope")
    [row] = _events(d, "reviewed")
    assert not {"incremental", "files_reviewed", "files_carried", "carried_kept", "lane_s_saved_est"} & set(row)
    assert not any(f.get("carried_by") == inc.CARRIED_BY for f in _posted_findings(gh))
    assert "Incremental re-review" not in gh.reviews_posted[-1]["body"]


async def test_flag_off_and_absent_make_identical_rounds(tmp_path):
    runs = []
    for i, cfg in enumerate((None, {"incremental_review": False})):
        d, gh, runner, outcome = await _round_two(tmp_path / str(i), cfg=cfg, panel=[PRIOR_MAJOR])
        rows = [{k: v for k, v in e.items() if k not in ("ts", "latency_s")} for e in d.telemetry.read_all()]
        runs.append((outcome, runner.inputs, gh.calls, [p.get("body") for p in gh.reviews_posted], rows))
    assert runs[0] == runs[1]


# ── supersede: the replacement round is scoped against the last POSTED round ────────


class ParkingRunner(CapturingRunner):
    """Parks the round on H1 in its finders until released; the PR is pushed to H2 meanwhile."""

    def __init__(self, findings):
        super().__init__(findings)
        self.parked = asyncio.Event()
        self.release = asyncio.Event()

    async def __call__(self, name, inputs, on_step=None):
        self.inputs.append(dict(inputs))
        on_step("find_correctness")
        if inputs["head_sha"] == H1:
            self.parked.set()
            await self.release.wait()
        for step in ("synthesize", "verify", "report"):
            on_step(step)
            for _ in range(20):
                await asyncio.sleep(0.005)
        return {"output": _panel_report(self.findings), "steps": {}, "failed": []}


async def test_a_superseded_rounds_replacement_is_scoped_against_the_last_posted_round(tmp_path):
    # H1 → H2 touched docs/notes.md. Against round 1 (H0), engine.py and notes.md changed.
    rows_h2 = [ROWS_H1[0], ROWS_H1[1], ROWS_H1[2], dict(ROWS_H1[3], sha="n2")]
    gh = IncGH(
        reviews=[_prior_review(H0, [PRIOR_MAJOR, PRIOR_MINOR])],
        rows_at={H0: ROWS_H0, H1: ROWS_H1, H2: rows_h2},
        delta={
            (H0, H1): {"status": "ahead", "files": DELTA_H0_H1},
            (H0, H2): {"status": "ahead", "files": [*DELTA_H0_H1, {"filename": "docs/notes.md", "patch": "+more\n"}]},
        },
    )
    runner = ParkingRunner([])
    d = make(tmp_path, gh=gh, runner=runner, cfg={"incremental_review": True})
    task = asyncio.create_task(d.handle_pr_event("o/r", 1, H1, "synchronize"))
    await asyncio.wait_for(runner.parked.wait(), 3)
    gh.pr_facts = dict(gh.pr_facts, head=H2)
    assert (await d.handle_pr_event("o/r", 1, H2, "synchronize")) == f"drop:{DROP_IN_FLIGHT}"
    runner.release.set()
    outcome = await asyncio.wait_for(task, 5)
    assert outcome.startswith("reviewed:")
    assert [i["head_sha"] for i in runner.inputs] == [H1, H2]
    scope = runner.inputs[1]["review_scope"]
    assert f"at head `{H0[:12]}`" in scope  # the carry base is the last POSTED round
    assert "pkg/engine.py (changed)" in scope and "docs/notes.md (changed)" in scope
    assert "  - pkg/render.py (1 carried finding(s))" in scope
    assert [e["mode"] for e in _events(d, "incremental_scope")] == ["scoped", "scoped"]
    [row] = _events(d, "reviewed")
    assert row["sha"] == H2 and (row["files_reviewed"], row["files_carried"]) == (3, 1)


async def test_a_scoped_round_with_no_in_scope_priors_still_tells_the_panel_it_is_a_re_review(tmp_path):
    gh = IncGH(reviews=[_prior_review(H0, [PRIOR_MINOR])])
    d, gh, runner, _outcome = await _round_two(tmp_path, cfg={"incremental_review": True}, gh=gh)
    [inputs] = runner.inputs
    # "[]", never the recipe's "(none — first review of this PR)" default.
    assert inputs["prior_findings"] == "[]" and "review_scope" in inputs


async def test_an_unreadable_prior_head_reviews_in_full(tmp_path):
    gh = IncGH(reviews=[_prior_review(H0, [PRIOR_MAJOR, PRIOR_MINOR])], rows_at={H1: ROWS_H1})
    d, gh, runner, _outcome = await _round_two(tmp_path, cfg={"incremental_review": True}, gh=gh)
    assert "review_scope" not in runner.inputs[0]
    assert _events(d, "reviewed")[0]["incremental"] == f"full:{inc.FULL_UNREADABLE}"

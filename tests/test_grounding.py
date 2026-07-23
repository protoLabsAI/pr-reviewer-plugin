"""Evidence grounding (issue #25) — a finding quoting absent code can't gate.

The fixtures are the real 2026-07-22 false positives. Two of the three are caught here;
the third is deliberately NOT, and its test says so — a substring check cannot refute a
finding that quotes real code and reasons wrongly about it."""

from __future__ import annotations

from pr_reviewer.grounding import (
    apply_grounding,
    ground_finding,
    quoted_snippets,
    render_grounding_footnote,
)
from pr_reviewer.verdicts import FAIL, WARN, verdict_for

# The actual head content at protoAgent#2138@47906140 — the call the panel said was gone.
WRITABLE_DIR_SRC = '''
def _writable_dir() -> Path:
    """The writable workflow dir."""
    from infra.paths import instance_paths

    cfg = sdk.config()
    configured = getattr(cfg, "workflow_dir", "") or ""
    if configured and not str(configured).startswith("/sandbox"):
        writable = Path(configured).expanduser()
    else:
        writable = instance_paths().store("workflows")
    writable.mkdir(parents=True, exist_ok=True)
    return writable
'''

# The actual #2138 finding: quotes a construction that exists nowhere at that head.
FABRICATED = {
    "file": "plugins/workflows/__init__.py",
    "line": 36,
    "severity": "blocker",
    "verdict": "confirmed",
    "claim": "`_writable_dir()` constructs `Path(str(configured))` but drops the `.expanduser()` call.",
    "evidence": "The diff moves `writable = Path(str(configured))` into the new helper unchanged.",
}


def test_the_2138_fabrication_is_downgraded():
    grounded, missing = ground_finding(FABRICATED, WRITABLE_DIR_SRC)
    assert grounded is False
    assert any("Path(str(configured))" in m for m in missing)


def test_a_downgraded_blocker_can_no_longer_fail_the_verdict():
    # The whole point: verdict_for maps an `uncertain` blocker/major to WARN, not FAIL.
    assert verdict_for([FABRICATED]) == FAIL
    out, downgraded = apply_grounding([FABRICATED], {FABRICATED["file"]: WRITABLE_DIR_SRC})
    assert len(downgraded) == 1
    assert verdict_for(out) == WARN
    assert out[0]["verdict"] == "uncertain"
    assert "not found at the reviewed head" in out[0]["note"]


def test_nothing_is_ever_dropped():
    out, _ = apply_grounding([FABRICATED], {FABRICATED["file"]: WRITABLE_DIR_SRC})
    assert len(out) == 1  # still posts, still readable, still a human's call
    assert out[0]["claim"] == FABRICATED["claim"]


# ── what it must NOT do ───────────────────────────────────────────────────────


def test_a_finding_quoting_real_code_is_left_alone():
    real = {
        "file": "plugins/workflows/__init__.py",
        "severity": "major",
        "claim": "The guard `writable = Path(configured).expanduser()` runs only in one branch.",
        "evidence": 'It sits under `if configured and not str(configured).startswith("/sandbox"):`',
    }
    assert ground_finding(real, WRITABLE_DIR_SRC)[0] is True


def test_the_2150_class_is_NOT_caught_and_that_is_expected():
    # protoAgent#2150: quoted `any_prefix = f"{name}."` accurately, then claimed it
    # matches "developer.env.TOKEN" — it does not. The quotes are real, so substring
    # grounding cannot refute it. This belongs to the verify prompt, not to this module.
    src = 'env_prefix = f"{name}{ENV_KEY_SEP}"\n    any_prefix = f"{name}."\n'
    real_quotes_wrong_inference = {
        "file": "plugins/delegates/store.py",
        "severity": "major",
        "claim": 'The prefix match `k.startswith(f"{name}.")` catches other delegates\' secrets.',
        "evidence": '`any_prefix = f"{name}."` followed by `if k.startswith(any_prefix) or k.startswith(env_prefix):`',
    }
    assert ground_finding(real_quotes_wrong_inference, src)[0] is True


def test_removed_behavior_findings_survive_via_the_patch():
    # A regression finding quotes code the head no longer has — grounding it against the
    # head alone would downgrade the panel's sharpest angle. The patch is part of the
    # haystack precisely so this stays grounded.
    head = "        return deps + [d for d in optional if _dep_pkg_name(d) not in soft_missing]\n"
    patch = (
        "@@ -896,7 +897,8 @@\n"
        "-            log.warning('optional dep(s) %s missing', soft_missing)\n"
        "+            return deps + [d for d in optional if _dep_pkg_name(d) not in soft_missing]\n"
    )
    finding = {
        "file": "graph/plugins/installer.py",
        "severity": "major",
        "claim": "The except branch drops satisfied optional deps.",
        "evidence": "The old code had `log.warning('optional dep(s) %s missing', soft_missing)` on that path.",
    }
    assert ground_finding(finding, head)[0] is False  # head alone would downgrade it
    assert ground_finding(finding, f"{head}\n{patch}")[0] is True  # with the patch, safe


# ── fail-open posture ─────────────────────────────────────────────────────────


def test_an_unreadable_source_never_downgrades():
    assert ground_finding(FABRICATED, None)[0] is True


def test_a_finding_with_no_quotable_evidence_is_left_alone():
    prose = {
        "file": "x.py",
        "severity": "major",
        "claim": "The two files disagree about who owns eviction.",
        "evidence": "Neither module documents the ownership, so the state leaks between them.",
    }
    assert quoted_snippets(prose) == []
    assert ground_finding(prose, "anything")[0] is True


def test_one_matching_quote_grounds_the_finding():
    # Panels paraphrase; requiring EVERY quote to match would refute honest findings.
    mixed = {
        "file": "x.py",
        "severity": "major",
        "claim": "See `writable = Path(configured).expanduser()` and `some_invented_call(x, y)`.",
        "evidence": "",
    }
    assert ground_finding(mixed, WRITABLE_DIR_SRC)[0] is True


def test_short_and_prose_quotes_are_not_checkable():
    # `deps` or `running` appear everywhere; treating them as evidence would ground a
    # fabrication by accident.
    assert quoted_snippets({"claim": "the `running` map and `deps` list", "evidence": ""}) == []


def test_whitespace_and_diff_markers_do_not_decide_groundedness():
    finding = {
        "file": "x.py",
        "severity": "minor",
        "claim": "quote: `+     writable = Path(configured).expanduser()`",
        "evidence": "",
    }
    assert ground_finding(finding, WRITABLE_DIR_SRC)[0] is True


def test_footnote_names_the_missing_quote():
    _out, downgraded = apply_grounding([FABRICATED], {FABRICATED["file"]: WRITABLE_DIR_SRC})
    note = render_grounding_footnote(downgraded)
    assert "downgraded to **uncertain**" in note
    assert "Path(str(configured))" in note
    assert render_grounding_footnote([]) == ""

"""Issue #313's A/B rollout: `incremental_review_repos`, a per-repo allowlist for
`incremental_review`.

The flag stays the master switch; a non-empty allowlist narrows it to the named repos. A repo
off the list takes exactly the flag-off path — `_incremental_plan` returns None before any
read, so its rounds make no incremental reads and are byte-for-byte today's. Empty or absent,
the allowlist changes nothing: the flag governs every repo, as before. The cfg list, a cfg
comma/whitespace string and PR_REVIEWER_INCREMENTAL_REVIEW_REPOS all parse to the same
case-folded set.
"""

from __future__ import annotations

import pytest

from tests.dispatch_helpers import make
from tests.test_incremental_313 import _INCREMENTAL_JQ, PRIOR_MAJOR, _events, _round_two

# ── parsing and the per-repo gate (pure) ─────────────────────────────────────────


def test_cfg_list_cfg_strings_and_env_parse_to_the_same_set(tmp_path, monkeypatch):
    forms = [
        {"incremental_review_repos": ["ProtoLabsAI/protoAgent", " o/r "]},
        {"incremental_review_repos": "ProtoLabsAI/protoAgent, o/r"},
        {"incremental_review_repos": "ProtoLabsAI/protoAgent\n\to/r"},
    ]
    dispatchers = [make(tmp_path / str(i), cfg=cfg) for i, cfg in enumerate(forms)]
    monkeypatch.setenv("PR_REVIEWER_INCREMENTAL_REVIEW_REPOS", "ProtoLabsAI/protoAgent o/r")
    dispatchers.append(make(tmp_path / "env", cfg=None))
    assert [d.incremental_review_repos for d in dispatchers] == [{"protolabsai/protoagent", "o/r"}] * 4


def test_incremental_review_for_needs_the_flag_and_the_list_when_one_is_set(tmp_path):
    on_empty = make(tmp_path / "a", cfg={"incremental_review": True})
    assert on_empty.incremental_review_for("o/r") and on_empty.incremental_review_for("Any/Repo")
    on_listed = make(tmp_path / "b", cfg={"incremental_review": True, "incremental_review_repos": ["O/R"]})
    assert on_listed.incremental_review_for("o/r")  # case-insensitive
    assert on_listed.incremental_review_for(" o/R ")  # surrounding whitespace ignored
    assert not on_listed.incremental_review_for("other/repo")
    # Flag off (or absent): a non-empty list enables nothing.
    off_listed = make(tmp_path / "c", cfg={"incremental_review": False, "incremental_review_repos": ["o/r"]})
    assert not off_listed.incremental_review_for("o/r")
    absent_flag = make(tmp_path / "d", cfg={"incremental_review_repos": ["o/r"]})
    assert not absent_flag.incremental_review_for("o/r")


def test_a_present_cfg_key_wins_over_env_and_is_read_each_round(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_INCREMENTAL_REVIEW_REPOS", "other/repo")
    d = make(tmp_path, cfg={"incremental_review": True, "incremental_review_repos": []})
    assert d.incremental_review_for("o/r")  # cfg's empty list wins: every repo, as today
    # Like the flag, the list is re-read from the live config: no restart needed.
    d._cfg["incremental_review_repos"] = ["other/repo"]
    assert not d.incremental_review_for("o/r")
    d._cfg["incremental_review_repos"] = ["o/r"]
    assert d.incremental_review_for("o/r")


# ── end to end through the dispatcher ─────────────────────────────────────────


async def test_round_two_on_an_allowlisted_repo_is_scoped(tmp_path):
    d, gh, runner, outcome = await _round_two(
        tmp_path, cfg={"incremental_review": True, "incremental_review_repos": ["O/R"]}
    )
    assert outcome.startswith("reviewed:")
    [inputs] = runner.inputs
    assert "review_scope" in inputs
    assert _events(d, "reviewed")[0]["incremental"] == "scoped"


@pytest.mark.parametrize("repos", [[], "", "  "], ids=["empty-list", "empty-string", "whitespace-string"])
async def test_an_empty_allowlist_keeps_every_repo_incremental(tmp_path, repos):
    d, gh, runner, _outcome = await _round_two(
        tmp_path, cfg={"incremental_review": True, "incremental_review_repos": repos}
    )
    assert "review_scope" in runner.inputs[0]
    assert _events(d, "reviewed")[0]["incremental"] == "scoped"


@pytest.mark.parametrize(
    "cfg",
    [
        {"incremental_review": False, "incremental_review_repos": ["o/r"]},
        {"incremental_review_repos": ["o/r"]},
    ],
    ids=["flag-off", "flag-absent"],
)
async def test_flag_off_with_a_non_empty_allowlist_makes_no_repo_incremental(tmp_path, cfg):
    d, gh, runner, _outcome = await _round_two(tmp_path, cfg=cfg)
    assert "review_scope" not in runner.inputs[0]
    assert not any(any(sig in a for sig in _INCREMENTAL_JQ) for call in gh.calls for a in call)
    assert not _events(d, "incremental_scope")


async def test_an_off_list_repos_round_is_byte_for_byte_flag_off(tmp_path):
    """The acceptance bar: a repo not on the allowlist makes no incremental reads and
    produces the same runner inputs, gh calls and posted body as the flag being off."""
    runs = []
    cfgs = (
        {"incremental_review": False},
        {"incremental_review": True, "incremental_review_repos": ["protoLabsAI/protoAgent"]},
        {"incremental_review": True, "incremental_review_repos": "protoLabsAI/protoAgent, another/one"},
    )
    for i, cfg in enumerate(cfgs):
        d, gh, runner, outcome = await _round_two(tmp_path / str(i), cfg=cfg, panel=[PRIOR_MAJOR])
        assert not any(any(sig in a for sig in _INCREMENTAL_JQ) for call in gh.calls for a in call)
        rows = [{k: v for k, v in e.items() if k not in ("ts", "latency_s")} for e in d.telemetry.read_all()]
        runs.append((outcome, runner.inputs, gh.calls, [p.get("body") for p in gh.reviews_posted], rows))
    assert runs[0] == runs[1] == runs[2]


async def test_the_env_allowlist_gates_the_round_the_same_way(tmp_path, monkeypatch):
    monkeypatch.setenv("PR_REVIEWER_INCREMENTAL_REVIEW_REPOS", "Other/Repo,O/R")
    d, gh, runner, _outcome = await _round_two(tmp_path / "on", cfg={"incremental_review": True})
    assert "review_scope" in runner.inputs[0]
    monkeypatch.setenv("PR_REVIEWER_INCREMENTAL_REVIEW_REPOS", "other/repo")
    d, gh, runner, _outcome = await _round_two(tmp_path / "off", cfg={"incremental_review": True})
    assert "review_scope" not in runner.inputs[0]
    assert not any(any(sig in a for sig in _INCREMENTAL_JQ) for call in gh.calls for a in call)

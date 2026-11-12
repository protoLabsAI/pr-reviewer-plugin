"""Per-feature structural results are memoized across passes (#312).

Across 367 `structural_plan` events, 425 of 1,716 features (25% of tokens, 23% of
feature-seconds) were byte-identical to the previous pass of the same PR and were re-run
anyway. Now each SELECTED feature is keyed by sha256 over its owned+context files' head blob
SHAs, its id, the engine version (`clawpatch --version`), the configured `model` and this
plugin's version; a key that already maps to a COMPLETED review is served from a small JSON
store next to the checkout cache instead of re-run — if every feature hits, the engine is
skipped entirely. Replayed records take the SAME `_render_findings` road as fresh ones (diff
confinement, dedup, anchor choice), so a changed base or scope is re-applied on every pass.

Partial, timed-out and failed features are never cached; a finding that cannot be attributed
to a reviewed feature blocks caching for the whole run (an entry that silently dropped a
finding would replay as a clean pass). Every cache fault — unreadable tree, unknown engine
version, corrupt entry, failed write — degrades to running the feature.
`structural_result_cache: false` restores the pre-#312 behaviour exactly.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import (
    PARTIAL_PREFIX,
    STRUCTURAL_GAP_MARKERS,
    UNAVAILABLE_PREFIX,
    ProtoPatchRunner,
    attribute_findings,
    classify_outage,
    outage_reason,
)
from pr_reviewer.result_cache import StructuralResultCache, plugin_version, result_key

from tests.test_protopatch_feature_plan_232 import FakeClawpatch, Recorder, feature, fenced, finding

SHA_HEAD = "a" * 40
SHA_BASE = "b" * 40

FEATURES = [feature(f"feat_{i}", owned=[f"m{i}.py"]) for i in range(3)]
LINES = {f"m{i}.py": 1 for i in range(3)}  # equal lines: selection order is by feature id
BLOBS = {f"m{i}.py": f"blob{i}-v1" for i in range(3)}


# ── pure pieces ────────────────────────────────────────────────────────────────────────────────


def test_result_key_tells_content_feature_engine_model_and_plugin_apart():
    blobs = {"a.py": "s1", "b.py": "s2"}
    base = result_key("f1", ["a.py"], ["b.py"], blobs, "1.0.0", "m", "0.71.0")
    assert base == result_key("f1", ["a.py"], ["b.py"], dict(blobs), "1.0.0", "m", "0.71.0")
    assert base != result_key("f1", ["a.py"], ["b.py"], {**blobs, "a.py": "s9"}, "1.0.0", "m", "0.71.0")
    assert base != result_key("f1", ["a.py"], ["b.py"], {**blobs, "b.py": "s9"}, "1.0.0", "m", "0.71.0")
    assert base != result_key("f2", ["a.py"], ["b.py"], blobs, "1.0.0", "m", "0.71.0")
    assert base != result_key("f1", ["a.py"], ["b.py"], blobs, "2.0.0", "m", "0.71.0")
    assert base != result_key("f1", ["a.py"], ["b.py"], blobs, "1.0.0", "other", "0.71.0")
    assert base != result_key("f1", ["a.py"], ["b.py"], blobs, "1.0.0", "m", "0.72.0")
    # a third, unrelated file's blob is not part of this feature's key
    assert base == result_key("f1", ["a.py"], ["b.py"], {**blobs, "c.py": "s3"}, "1.0.0", "m", "0.71.0")


def test_plugin_version_reads_the_manifest():
    import yaml

    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "protoagent.plugin.yaml").read_text())
    assert plugin_version() == manifest["version"]


def test_store_roundtrip_ttl_touch_and_entry_cap(tmp_path):
    cache = StructuralResultCache(tmp_path / "store", ttl_s=100, entry_limit=2)
    cache.put("k1", {"outcome": "finished", "records": []})
    assert cache.get("k1") == {"outcome": "finished", "records": []}
    old = time.time() - 101
    os.utime(cache.path_for("k1"), (old, old))
    assert cache.get("k1") is None  # stale = a plain miss…
    assert not cache.path_for("k1").exists()  # …and the entry is gone
    for i, key in enumerate(["a", "b", "c"]):
        cache.put(key, {"i": i})
        t = time.time() - 50 + i
        os.utime(cache.path_for(key), (t, t))
    assert cache.prune() == 1  # LRU under the entry cap: the oldest goes first
    assert cache.get("a") is None and cache.get("b") and cache.get("c")


def test_store_never_raises_on_corrupt_unreadable_or_unwritable(tmp_path):
    cache = StructuralResultCache(tmp_path / "store")
    assert cache.get("missing") is None
    cache.put("k", {"x": 1})
    cache.path_for("k").write_text("{half a record")
    assert cache.get("k") is None  # corrupt entry = a plain miss
    cache.put("k2", {"x": 2})
    cache.path_for("k2").write_text("[1, 2]")
    assert cache.get("k2") is None  # wrong shape = a plain miss
    cache.path_for("adir").mkdir()
    assert cache.get("adir") is None  # unreadable (a directory) = a plain miss
    blocked = StructuralResultCache(tmp_path / "rootfile")
    (tmp_path / "rootfile").write_text("in the way")
    blocked.put("k", {"x": 1})  # an unwritable root is a dropped write, never a raise
    assert blocked.get("k") is None
    assert StructuralResultCache(tmp_path / "never-made").prune() == 0


def test_attribute_findings_engine_id_wins_then_evidence_subset_then_orphans():
    scopes = {"fa": {"a.py", "shared.py"}, "fb": {"b.py", "shared.py"}}
    by_id = {"featureId": "fb", "evidence": [{"path": "a.py", "startLine": 1}]}
    in_a = {"evidence": [{"path": "a.py", "startLine": 1}]}
    in_both = {"evidence": [{"path": "shared.py", "startLine": 1}]}
    outside = {"evidence": [{"path": "elsewhere.py", "startLine": 1}]}
    no_evidence = {"title": "t"}
    attributed, orphans = attribute_findings([by_id, in_a, in_both, outside, no_evidence], scopes)
    assert attributed["fa"] == [in_a, in_both]
    assert attributed["fb"] == [by_id, in_both]
    assert orphans == 2  # outside every scope, or citing nothing at all


# ── the runner ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    monkeypatch.delenv("CLAWPATCH_GATEWAY_TIMEOUT_MS", raising=False)

    async def fake_run_gh(args, timeout=30):
        return (0, f"{SHA_HEAD} {SHA_BASE}", "") if args[:1] == ["api"] else (0, "", "")

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


def make_git(lines: dict[str, int], blobs: dict[str, str] | None):
    """Numstat / name-only diffs from `lines` (as the #232 tests), plus `ls-tree -r HEAD` from
    `blobs` — the head-content identity the result cache keys on. `blobs=None` = an unreadable
    tree (git failure): the cache must sit the pass out."""

    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            os.makedirs(args[-1], exist_ok=True)
        if "ls-tree" in args:
            if blobs is None:
                return 1, "", "fatal: not a tree"
            return 0, "".join(f"100644 blob {sha}\t{p}\0" for p, sha in blobs.items()), ""
        if "--numstat" in args:
            return 0, "".join(f"{n}\t0\t{p}\n" for p, n in lines.items()), ""
        if "diff" in args:
            return 0, "".join(f"{p}\n" for p in lines), ""
        return 0, "", ""

    return run_git


class Versioned:
    """Answers `clawpatch --version` (and counts the asks); everything else goes to `inner`."""

    def __init__(self, inner, version="1.4.2"):
        self.inner = inner
        self.version = version
        self.version_calls = 0

    async def __call__(self, args, cwd, env, budget_s):
        if args[-1] == "--version":
            self.version_calls += 1
            return 0, f"{self.version}\n", "", False
        return await self.inner(args, cwd, env, budget_s)


def runner(root, claw, lines, blobs, cfg=None, telemetry=None):
    base = {"checkout_root": str(root / "co"), "state_root": str(root / "st"), "default_repo": ""}
    return ProtoPatchRunner(
        {**base, **(cfg or {})}, run_git=make_git(lines, blobs), run_clawpatch=claw, telemetry=telemetry or Recorder()
    )


async def first_pass(root, *, version="1.4.2", cfg=None, blobs=BLOBS, lines=LINES, features=FEATURES):
    claw = Versioned(FakeClawpatch(features), version)
    tele = Recorder()
    out = await runner(root, claw, lines, blobs, cfg=cfg, telemetry=tele).review(1, "o/r")
    return out, claw, tele


def reviews(claw: Versioned) -> list[list[str]]:
    return [c for c in claw.inner.calls if c[1:2] == ["review"]]


async def test_a_second_pass_over_the_same_head_serves_every_feature_from_cache(tmp_path):
    out1, claw1, tele1 = await first_pass(tmp_path)
    assert not any(m in out1 for m in STRUCTURAL_GAP_MARKERS)
    assert claw1.inner.listed == ["feat_0", "feat_1", "feat_2"]
    [(_, row1)] = tele1.events
    assert (row1["features_cached"], row1["features_run"]) == (0, 3)

    claw2 = Versioned(FakeClawpatch(FEATURES))
    tele2 = Recorder()
    out2 = await runner(tmp_path, claw2, LINES, BLOBS, telemetry=tele2).review(1, "o/r")
    assert reviews(claw2) == []  # the engine ran once — in pass 1
    assert claw2.version_calls == 1
    assert not any(m in out2 for m in STRUCTURAL_GAP_MARKERS)  # a fully-cached full pass is complete
    assert "3 feature(s) served from the result cache" in out2
    by_claim = lambda f: f["claim"]  # noqa: E731
    assert sorted(fenced(out2), key=by_claim) == sorted(fenced(out1), key=by_claim)  # identical findings
    [(_, row2)] = tele2.events
    assert row2["outcome"] == "complete"
    assert (row2["features_cached"], row2["features_run"]) == (3, 0)
    assert [f["status"] for f in row2["features"]] == ["cached"] * 3


async def test_changing_one_owned_file_invalidates_only_that_feature(tmp_path):
    await first_pass(tmp_path)
    claw2 = Versioned(FakeClawpatch(FEATURES))
    tele2 = Recorder()
    out2 = await runner(tmp_path, claw2, LINES, {**BLOBS, "m1.py": "blob1-v2"}, telemetry=tele2).review(1, "o/r")
    assert claw2.inner.listed == ["feat_1"]  # only the changed feature re-runs
    assert len(fenced(out2)) == 3  # two replayed + one fresh, merged
    [(_, row)] = tele2.events
    assert (row["features_cached"], row["features_run"]) == (2, 1)
    assert {f["id"]: f["status"] for f in row["features"]} == {
        "feat_0": "cached",
        "feat_1": "finished",
        "feat_2": "cached",
    }


@pytest.mark.parametrize("variant", ["model", "engine"])
async def test_a_model_or_engine_version_change_invalidates_every_key(tmp_path, variant):
    await first_pass(tmp_path)
    cfg = {"model": "protolabs/other"} if variant == "model" else None
    claw2 = Versioned(FakeClawpatch(FEATURES), version="9.9.9" if variant == "engine" else "1.4.2")
    await runner(tmp_path, claw2, LINES, BLOBS, cfg=cfg, telemetry=Recorder()).review(1, "o/r")
    assert claw2.inner.listed == ["feat_0", "feat_1", "feat_2"]


async def test_features_a_cut_short_pass_did_not_finish_are_never_cached(tmp_path):
    claw1 = Versioned(FakeClawpatch(FEATURES, finish=1, review_script=[{"timed_out": True, "rc": 124}]))
    out1 = await runner(tmp_path, claw1, LINES, BLOBS, telemetry=Recorder()).review(1, "o/r")
    assert out1.startswith(PARTIAL_PREFIX)
    claw2 = Versioned(FakeClawpatch(FEATURES))
    tele2 = Recorder()
    out2 = await runner(tmp_path, claw2, LINES, BLOBS, telemetry=tele2).review(1, "o/r")
    assert claw2.inner.listed == ["feat_1", "feat_2"]  # the killed two re-run; the finished one hit
    assert not any(m in out2 for m in STRUCTURAL_GAP_MARKERS) and len(fenced(out2)) == 3
    [(_, row)] = tele2.events
    assert (row["features_cached"], row["features_run"]) == (1, 2)


async def test_a_failed_pass_caches_nothing(tmp_path):
    script = [{"rc": 4, "stderr": "HTTP 401 Unauthorized"}]
    claw1 = Versioned(FakeClawpatch(FEATURES, finish=0, review_script=script))
    out1 = await runner(tmp_path, claw1, LINES, BLOBS, telemetry=Recorder()).review(1, "o/r")
    assert out1.startswith(UNAVAILABLE_PREFIX)
    claw2 = Versioned(FakeClawpatch(FEATURES))
    tele2 = Recorder()
    await runner(tmp_path, claw2, LINES, BLOBS, telemetry=tele2).review(1, "o/r")
    assert claw2.inner.listed == ["feat_0", "feat_1", "feat_2"]
    assert tele2.events[0][1]["features_cached"] == 0


async def test_a_corrupt_store_degrades_to_a_normal_run(tmp_path):
    await first_pass(tmp_path)
    entries = list((tmp_path / "structural-results").glob("*.json"))
    assert len(entries) == 3  # pass 1 populated the store, one entry per feature
    for f in entries:
        f.write_text("{half a record")
    claw2 = Versioned(FakeClawpatch(FEATURES))
    tele2 = Recorder()
    out2 = await runner(tmp_path, claw2, LINES, BLOBS, telemetry=tele2).review(1, "o/r")
    assert claw2.inner.listed == ["feat_0", "feat_1", "feat_2"]  # every entry read as a miss
    assert not any(m in out2 for m in STRUCTURAL_GAP_MARKERS) and len(fenced(out2)) == 3
    assert tele2.events[0][1]["features_cached"] == 0


@pytest.mark.parametrize("value", [False, "false", "0", "off"])
async def test_the_off_switch_restores_the_pre_cache_behaviour(tmp_path, value):
    cfg = {"structural_result_cache": value}
    out1, claw1, tele1 = await first_pass(tmp_path, cfg=cfg)
    claw2 = Versioned(FakeClawpatch(FEATURES))
    tele2 = Recorder()
    out2 = await runner(tmp_path, claw2, LINES, BLOBS, cfg=cfg, telemetry=tele2).review(1, "o/r")
    assert claw1.version_calls == claw2.version_calls == 0  # the key is never even computed
    assert claw2.inner.listed == ["feat_0", "feat_1", "feat_2"]  # everything runs, as before
    assert not (tmp_path / "structural-results").exists()  # and nothing is ever stored
    for row in (tele1.events[0][1], tele2.events[0][1]):
        assert "features_cached" not in row and "features_run" not in row  # today's event, exactly
    assert "result cache" not in out1 and "result cache" not in out2


async def test_replayed_findings_are_reconfined_to_this_pass_diff(tmp_path):
    """A cached row is re-validated like a fresh one: a later pass whose diff no longer touches
    the cited file (same head content, different base) must not report it."""
    features = [feature("feat_a", owned=["m2.py", "m1.py"])]
    blobs = {"m1.py": "b1", "m2.py": "b2"}
    out1, _claw1, _tele1 = await first_pass(tmp_path, features=features, lines={"m1.py": 1, "m2.py": 1}, blobs=blobs)
    assert [f["file"] for f in fenced(out1)] == ["m2.py"]
    claw2 = Versioned(FakeClawpatch(features))
    tele2 = Recorder()
    out2 = await runner(tmp_path, claw2, {"m1.py": 1}, blobs, telemetry=tele2).review(1, "o/r")
    assert reviews(claw2) == []  # same head blobs: a hit
    assert fenced(out2) == []  # replayed, then confined to THIS diff — m2.py is not in it
    assert tele2.events[0][1]["features_cached"] == 1


async def test_a_finding_that_fits_no_reviewed_feature_blocks_caching_for_the_run(tmp_path):
    inner = FakeClawpatch(FEATURES)
    real = FakeClawpatch.__call__

    async def with_orphan(args, cwd, env, budget_s):
        result = await real(inner, args, cwd, env, budget_s)
        if args[1] == "review":
            state = Path(args[args.index("--state-dir") + 1])
            (state / "findings" / "zz-orphan.json").write_text(json.dumps(finding("orphan", "elsewhere/x.py")))
        return result

    out1 = await runner(tmp_path, Versioned(with_orphan), LINES, BLOBS, telemetry=Recorder()).review(1, "o/r")
    assert not any(m in out1 for m in STRUCTURAL_GAP_MARKERS)
    claw2 = Versioned(FakeClawpatch(FEATURES))
    tele2 = Recorder()
    await runner(tmp_path, claw2, LINES, BLOBS, telemetry=tele2).review(1, "o/r")
    assert claw2.inner.listed == ["feat_0", "feat_1", "feat_2"]  # nothing was cached
    assert tele2.events[0][1]["features_cached"] == 0


async def test_an_all_hit_capped_pass_is_still_partial_never_a_clean_pass(tmp_path):
    cfg = {"structural_max_features": 2}
    out1, _claw1, _tele1 = await first_pass(tmp_path, cfg=cfg)
    assert out1.startswith(PARTIAL_PREFIX)
    claw2 = Versioned(FakeClawpatch(FEATURES))
    tele2 = Recorder()
    out2 = await runner(tmp_path, claw2, LINES, BLOBS, cfg=cfg, telemetry=tele2).review(1, "o/r")
    assert reviews(claw2) == []  # both selected features hit
    assert out2.startswith(PARTIAL_PREFIX) and "2 of 3 features reviewed" in out2
    assert classify_outage(outage_reason(out2)) == "feature-cap"
    assert len(fenced(out2)) == 2
    [(_, row)] = tele2.events
    assert row["outcome"] == "partial" and (row["features_cached"], row["features_run"]) == (2, 0)


async def test_a_cut_short_pass_still_returns_the_cached_features_findings(tmp_path):
    features = FEATURES[:2]
    blobs = {"m0.py": "b0", "m1.py": "b1"}
    lines = {"m0.py": 1, "m1.py": 1}
    await first_pass(tmp_path, features=features, lines=lines, blobs=blobs)
    # feat_1's file changed, so it must run — and its run dies with nothing finished.
    claw2 = Versioned(FakeClawpatch(features, finish=0, review_script=[{"timed_out": True, "rc": 124}]))
    out2 = await runner(tmp_path, claw2, lines, {**blobs, "m1.py": "b1-v2"}, telemetry=Recorder()).review(1, "o/r")
    assert claw2.inner.listed == ["feat_1"]
    assert out2.startswith(PARTIAL_PREFIX) and "1 of 2 features reviewed" in out2
    assert [f["claim"] for f in fenced(out2)] == ["feat_0 bug"]  # the hit survives the kill


async def test_an_unreadable_head_tree_sits_the_cache_out(tmp_path):
    out1, claw1, tele1 = await first_pass(tmp_path, blobs=None)
    assert not any(m in out1 for m in STRUCTURAL_GAP_MARKERS)  # the pass itself is untouched
    assert claw1.version_calls == 0  # no key material → the engine version is not even asked for
    [(_, row1)] = tele1.events
    assert (row1["features_cached"], row1["features_run"]) == (0, 3)
    claw2 = Versioned(FakeClawpatch(FEATURES))
    await runner(tmp_path, claw2, LINES, None, telemetry=Recorder()).review(1, "o/r")
    assert claw2.inner.listed == ["feat_0", "feat_1", "feat_2"]  # nothing was stored either

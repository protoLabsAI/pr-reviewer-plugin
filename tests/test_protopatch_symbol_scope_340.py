"""The structural plan ranks the features a diff TOUCHES ahead of those that only share a file (#340).

clawpatch maps one feature per route handler and each handler owns its whole file, so after a
six-line change every handler of that file was eligible, tied on changed lines, and the per-pass
cap chose among them by feature id. Measured on protoAgent, 2026-11-12/13 (cap 32):

  - #5173 (4 files, ~90 lines, 48 eligible): both rounds left out `_watch_update`, a handler the PR
    edits, while reviewing 30 it never touched;
  - #5169 (6 files, 50-65 lines, 36 eligible): `complete: false` / WARN on all five rounds.

Now a feature whose own code the diff provably does not touch is a SIBLING: ranked after every
touched feature, still reviewed while the cap has room, and — when the cap leaves it out — stated
in the header instead of making the pass partial. Anything the planner cannot prove stays ranked
as touched, and `structural_symbol_scope: false` is the rollback. A changed file that reaches a
feature as context (its test module, a helper it imports) counts against it only when that change
mentions the feature's handler or route, or touches a name the handler uses (#346).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pr_reviewer.protopatch as pp
import pytest
from pr_reviewer.protopatch import (
    FEATURE_CAP_REASON,
    PARTIAL_PREFIX,
    STRUCTURAL_GAP_MARKERS,
    ProtoPatchRunner,
    parse_hunk_ranges,
    plan_features,
    read_head_source,
    sibling_features,
)
from pr_reviewer.symbol_scope import MAX_SOURCE_BYTES, touched_source, touched_symbols

SHA_HEAD = "a" * 40
SHA_BASE = "b" * 40

ROUTES = """\
import json
from store import load, save

LIMIT = 10


def _check(scope):
    return scope == "admin"


def _fmt(row):
    return json.dumps(row)


def register(app, cfg):
    plane = cfg["plane"]

    @app.get("/items")
    async def _list_items():
        return [_fmt(r) for r in load()][:LIMIT]

    @app.post("/items")
    async def _add_item(body, scope):
        if not _check(scope):
            raise PermissionError(plane)
        save(body)

    @app.delete("/items/{id}")
    async def _delete_item(id):
        save(None)

    @app.get("/health")
    async def _health():
        return "ok"

    app.add_api_route("/legacy", _health)
"""


def line_of(needle: str, source: str = ROUTES) -> int:
    return next(i for i, text in enumerate(source.splitlines(), 1) if needle in text)


def touched(*needles: str, source: str = ROUTES) -> set[str]:
    result = touched_symbols(source, [(line_of(n, source), line_of(n, source)) for n in needles])
    assert result is not None
    return result[0]


# ── which definitions a diff touches ───────────────────────────────────────────────────────────


def test_a_change_inside_a_nested_closure_touches_every_definition_enclosing_it():
    # The handler never mentions `_inner` by name (it is registered by its decorator), so only the
    # enclosing chain can say the handler changed.
    source = (
        "def register(app):\n"
        "    async def _stream():\n"
        "        @app.on_close\n"
        "        def _inner():\n"
        "            return 1\n"
        "        return 2\n"
        "\n"
        "    async def _other():\n"
        "        return 3\n"
    )
    names = touched("return 1", source=source)
    assert {"_inner", "_stream", "register"} <= names and "_other" not in names


def test_a_changed_class_header_touches_every_method():
    source = "class Api(Base):\n    def get(self):\n        return 1\n\n    def put(self):\n        return 2\n"
    assert {"Api", "get", "put"} <= touched("class Api(Base):", source=source)
    # a changed method body still touches that method (and the class holding it), not its sibling
    assert touched("return 1", source=source) == {"Api", "get"}


def test_a_changed_factory_signature_touches_every_handler_it_defines():
    names = touched("def register(app, cfg):")
    assert {"_list_items", "_add_item", "_delete_item", "_health"} <= names


def test_a_changed_condition_touches_the_definitions_it_guards():
    source = "FLAG = True\n\nif FLAG:\n    def a():\n        return 1\n\ndef b():\n    return 2\n"
    names = touched("if FLAG:", source=source)
    assert "a" in names and "b" not in names


def test_names_the_base_side_lost_count_against_what_still_refers_to_them():
    # The diff deleted `_check` (or renamed it): on the head side nothing in the file is called
    # `_check` any more, but `_add_item` still calls it.
    head = ROUTES.replace('def _check(scope):\n    return scope == "admin"\n', "")
    blank = head.splitlines().index("LIMIT = 10") + 2
    alone = touched_symbols(head, [(blank, blank)])
    assert alone is not None and "_add_item" not in alone[0]
    removed = touched_symbols(ROUTES, [(line_of("def _check(scope):"), line_of('return scope == "admin"'))])
    assert removed is not None and "_check" in removed[0]
    seeded = touched_symbols(head, [(blank, blank)], removed[0])
    assert seeded is not None and "_add_item" in seeded[0] and "_health" not in seeded[0]


def test_a_change_inside_one_handler_touches_that_handler_and_its_factory_only():
    names = touched("save(body)")
    assert "_add_item" in names
    assert not names & {"_list_items", "_delete_item", "_health", "_check", "_fmt"}


def test_a_changed_decorator_touches_its_handler():
    assert "_delete_item" in touched('@app.delete("/items/{id}")')
    assert "_add_item" not in touched('@app.delete("/items/{id}")')


def test_a_changed_helper_touches_every_handler_that_calls_it():
    names = touched('return scope == "admin"')
    assert {"_check", "_add_item"} <= names
    assert not names & {"_list_items", "_delete_item", "_health"}


def test_a_changed_module_constant_or_import_touches_what_references_it():
    assert "_list_items" in touched("LIMIT = 10") and "_add_item" not in touched("LIMIT = 10")
    by_import = touched("from store import load, save")
    assert {"_list_items", "_add_item", "_delete_item"} <= by_import and "_health" not in by_import
    # `import json` reaches `_list_items` through the helper it calls: the closure is transitive
    assert {"_fmt", "_list_items"} <= touched("import json")


def test_a_changed_closure_variable_touches_the_handlers_that_read_it():
    names = touched('plane = cfg["plane"]')
    assert "_add_item" in names and not names & {"_list_items", "_delete_item", "_health"}


def test_a_changed_registration_line_touches_the_handler_it_names():
    assert "_health" in touched('app.add_api_route("/legacy", _health)')


def test_a_change_in_no_definition_touches_nothing():
    blank = ROUTES.splitlines().index("") + 1
    result = touched_symbols(ROUTES, [(blank, blank)])
    assert result is not None and result[0] == set()
    assert {"register", "_list_items", "_add_item", "_delete_item", "_health", "_check", "_fmt"} == result[1]


@pytest.mark.parametrize("source", ["def broken(:\n", "x" * (MAX_SOURCE_BYTES + 1), None])
def test_a_file_that_cannot_be_analysed_is_unknown_never_a_crash(source):
    assert touched_symbols(source, [(1, 1)]) is None


def test_methods_are_definitions_too():
    source = "class Api:\n    def get(self):\n        return 1\n\n    def put(self):\n        return 2\n"
    names, known = touched_symbols(source, [(3, 3)])
    assert "get" in names and "put" not in names and {"Api", "get", "put"} <= known


# ── which features are siblings ────────────────────────────────────────────────────────────────


def route(fid, symbol, path="api/routes.py", context=("pyproject.toml",), owned=None):
    return {
        "featureId": fid,
        "title": fid,
        "kind": "route",
        "entrypoints": [{"path": path, "symbol": symbol}],
        "ownedFiles": [{"path": p, "reason": "x"} for p in (owned or [path])],
        "contextFiles": [{"path": p, "reason": "x"} for p in context],
    }


FEATURES = [
    route("feat_route_list", "_list_items"),
    route("feat_route_add", "_add_item"),
    route("feat_route_delete", "_delete_item"),
    route("feat_route_health", "_health"),
]
SOURCES = {"api/routes.py": ROUTES}


def siblings(features, lines, ranges, sources=SOURCES, base_ranges=None, base=None):
    return sibling_features(features, lines, ranges, sources.get, base_ranges or {}, (base or {}).get)


def test_a_helper_the_diff_removes_keeps_its_callers_out_of_the_siblings():
    head = ROUTES.replace('def _check(scope):\n    return scope == "admin"\n', "")
    at = head.splitlines().index("LIMIT = 10") + 3  # where the deleted lines sat, clear of `LIMIT`
    gone = (line_of("def _check(scope):"), line_of('return scope == "admin"'))
    args = (FEATURES, {"api/routes.py": 2}, {"api/routes.py": [(at, at)]}, {"api/routes.py": head})
    found = siblings(*args, base_ranges={"api/routes.py": [gone]}, base=SOURCES)
    assert found == {"feat_route_list", "feat_route_delete", "feat_route_health"}
    # the base text is unreadable ⇒ nothing in the file is provable
    assert siblings(*args, base_ranges={"api/routes.py": [gone]}, base={}) == set()
    # and without the base side's ranges at all, nothing anywhere is
    assert sibling_features(*args[:3], args[3].get) == set()


def test_only_the_handlers_the_diff_leaves_alone_are_siblings():
    hunk = [(line_of("save(body)"), line_of("save(body)"))]
    found = siblings(FEATURES, {"api/routes.py": 1}, {"api/routes.py": hunk})
    assert found == {"feat_route_list", "feat_route_delete", "feat_route_health"}


def test_a_changed_helper_keeps_its_callers_out_of_the_siblings():
    hunk = [(line_of('return scope == "admin"'),) * 2]
    assert "feat_route_add" not in siblings(FEATURES, {"api/routes.py": 1}, {"api/routes.py": hunk})


def test_a_deletion_at_a_handlers_edge_counts_against_it():
    # `git diff --unified=0` reports a pure deletion as the line BEFORE it; the widened range reaches
    # the handler whose first line went.
    before = line_of('@app.delete("/items/{id}")') - 1
    found = siblings(FEATURES, {"api/routes.py": 1}, {"api/routes.py": [(before, before)]})
    assert "feat_route_delete" not in found


@pytest.mark.parametrize(
    "case",
    ["no-ranges", "file-has-no-ranges", "unreadable", "unparseable", "not-python", "unknown-symbol", "no-entrypoint"],
)
def test_anything_the_planner_cannot_prove_is_ranked_as_touched(case):
    hunk = [(line_of("save(body)"),) * 2]
    features, lines, ranges, sources = FEATURES, {"api/routes.py": 1}, {"api/routes.py": hunk}, dict(SOURCES)
    if case == "no-ranges":
        ranges = None
    elif case == "file-has-no-ranges":
        ranges = {"other.py": hunk}
    elif case == "unreadable":
        sources = {}
    elif case == "unparseable":
        sources = {"api/routes.py": "def broken(:\n"}
    elif case == "not-python":
        features = [route("feat_ts", "listItems", path="web/routes.ts")]
        lines, ranges, sources = {"web/routes.ts": 1}, {"web/routes.ts": hunk}, {"web/routes.ts": ROUTES}
    elif case == "unknown-symbol":
        features = [route("feat_gone", "_renamed_away")]
    elif case == "no-entrypoint":
        features = [{**route("feat_lib", "_health"), "entrypoints": []}]
    assert siblings(features, lines, ranges, sources) == set()


def test_a_second_owned_file_without_an_entrypoint_keeps_a_feature_touched():
    hunk = [(line_of("save(body)"),) * 2]
    two_files = route("feat_two", "_health", owned=["api/routes.py", "api/models.py"])
    alone = route("feat_alone", "_health")
    lines = {"api/routes.py": 1, "api/models.py": 4}
    ranges = {"api/routes.py": hunk, "api/models.py": [(1, 4)]}
    sources = {**SOURCES, "api/models.py": "X = 1\n"}
    assert siblings([two_files, alone], lines, ranges, sources) == {"feat_alone"}


# ── changed files that reach a feature as context (#346) ───────────────────────────────────────

TESTS = """\
import pytest

BASE = "/items"


def test_list(client):
    assert client.get("/items").status_code == 200


def test_add_requires_admin(client):
    assert client.post(BASE, json={}).status_code == 403


def test_delete(client):
    item = 7
    assert client.delete(f"/items/{item}").status_code == 204


def test_health_handler_directly():
    from api.routes import _health

    assert _health
"""

WITH_TESTS = [
    {**route("feat_route_list", "_list_items", context=("tests/test_routes.py",)), "route": None},
    route("feat_route_add", "_add_item", context=("tests/test_routes.py",)),
    route("feat_route_delete", "_delete_item", context=("tests/test_routes.py",)),
    route("feat_route_health", "_health", context=("tests/test_routes.py",)),
]
for _feature, _route in zip(
    WITH_TESTS, ["GET /items", "POST /items", "DELETE /items/{id}", "GET /health"], strict=True
):
    _feature["entrypoints"][0]["route"] = _route


def with_tests(test_needle, routes_needle=None, tests=TESTS):
    lines = {"tests/test_routes.py": 1}
    ranges = {"tests/test_routes.py": [(line_of(test_needle, tests),) * 2]}
    if routes_needle:
        lines["api/routes.py"] = 1
        ranges["api/routes.py"] = [(line_of(routes_needle),) * 2]
    return siblings(WITH_TESTS, lines, ranges, {**SOURCES, "tests/test_routes.py": tests})


def test_a_changed_test_counts_only_against_the_handler_it_exercises():
    # The test of DELETE changed: its text names the route `/items/{item}`, which is DELETE's
    # `/items/{id}` — and, being a longer spelling of `/items`, the list and add routes too.
    assert with_tests("item = 7") == {"feat_route_health"}
    # The test that imports `_health` by name changed: only the health handler is touched.
    assert with_tests("assert _health") == {"feat_route_list", "feat_route_add", "feat_route_delete"}


def test_a_changed_test_constant_counts_against_the_routes_its_text_spells():
    # `BASE` is used by the add test, so the closure pulls that test's text in next to the changed
    # line itself: `"/items"` is the list and add routes. DELETE's `/items/{id}` is not spelled
    # anywhere in that text — the reverse rule reads text, it does not evaluate it.
    assert with_tests('BASE = "/items"') == {"feat_route_delete", "feat_route_health"}


def test_a_handler_edit_and_an_unrelated_test_edit_leave_the_other_handlers_siblings():
    found = with_tests("assert _health", routes_needle="save(body)")
    assert found == {"feat_route_list", "feat_route_delete"}  # `add` by its own code, `health` by its test


def test_names_a_changed_context_module_touches_reach_the_handlers_that_use_them():
    store = "def load():\n    return []\n\n\ndef save(row):\n    return row\n\n\ndef unused():\n    return 0\n"
    features = [route(f["featureId"], f["entrypoints"][0]["symbol"], context=("store.py",)) for f in FEATURES]
    sources = {**SOURCES, "store.py": store}

    def changed(needle):
        at = line_of(needle, store)
        return siblings(features, {"store.py": 1}, {"store.py": [(at, at)]}, sources)

    # `_list_items` calls `load`; nothing in routes.py calls `unused`; `routes.py` itself is unchanged
    assert changed("return []") == {"feat_route_add", "feat_route_delete", "feat_route_health"}
    assert changed("return 0") == {f["featureId"] for f in features}
    assert changed("return row") == {"feat_route_list", "feat_route_health"}


@pytest.mark.parametrize("case", ["not-python", "unparseable", "unreadable", "no-ranges", "home-unreadable"])
def test_a_changed_context_file_the_planner_cannot_analyse_keeps_the_feature_touched(case):
    path = "web/client.ts" if case == "not-python" else "store.py"
    features = [route("feat_route_health", "_health", context=(path,))]
    sources = {**SOURCES, path: "def broken(:\n" if case == "unparseable" else "def load():\n    return 1\n"}
    ranges = {path: [(2, 2)]}
    if case == "unreadable":
        del sources[path]
    elif case == "no-ranges":
        ranges = {"elsewhere.py": [(1, 1)]}
    elif case == "home-unreadable":
        del sources["api/routes.py"]  # the change touches `load`, so the handler's own file must be read
    assert siblings(features, {path: 1}, ranges, sources) == set()


@pytest.mark.parametrize(
    ("route_spec", "text", "hit"),
    [
        ("GET /items", 'client.get("/items")', True),
        ("GET /items", 'client.get("/items/3")', True),  # a longer spelling: over-inclusion is fine
        ("GET /items", 'client.get("/itemsets")', False),
        ("DELETE /items/{id}", 'client.delete(f"/items/{item}")', True),
        ("DELETE /items/{id}", 'client.delete("/items/42")', True),
        ("DELETE /items/{id}", 'client.get("/items")', False),
        ("POST /api/config/providers/{pid}/models", '"/api/config/providers/p1/models"', True),
        ("POST /api/config/providers/{pid}/models", '"/api/config/providers"', False),
        ("GET /", "anything /", None),
        (None, "x", None),
        ("", "x", None),
    ],
)
def test_route_patterns_match_the_way_tests_spell_a_route(route_spec, text, hit):
    pattern = pp._route_pattern(route_spec)
    assert (pattern is None) if hit is None else (bool(pattern.search(text)) is hit)


def test_touched_source_is_the_changed_lines_and_every_touched_definition():
    at = line_of('BASE = "/items"', TESTS)
    names, _known = touched_symbols(TESTS, [(at, at)])
    text = touched_source(TESTS, [(at, at)], names)
    assert 'BASE = "/items"' in text and "def test_add_requires_admin" in text and "test_delete" not in text
    assert touched_source("def broken(:\n", [(1, 1)], set()) is None


def test_a_reader_that_raises_ranks_everything_as_touched():
    def boom(path):
        raise RuntimeError("disk")

    assert sibling_features(FEATURES, {"api/routes.py": 1}, {"api/routes.py": [(1, 1)]}, boom, {}) == set()


def test_the_head_reader_refuses_what_is_not_a_regular_file_inside_the_checkout(tmp_path):
    checkout = tmp_path / "co"
    checkout.mkdir()
    (checkout / "ok.py").write_text("x = 1\n")
    (tmp_path / "secret.py").write_text("token = 1\n")
    os.symlink(tmp_path / "secret.py", checkout / "link.py")
    (checkout / "big.py").write_text("x" * (MAX_SOURCE_BYTES + 1))
    assert read_head_source(checkout, "ok.py") == "x = 1\n"
    assert read_head_source(checkout, "link.py") is None
    assert read_head_source(checkout, "../secret.py") is None
    assert read_head_source(checkout, "big.py") is None
    assert read_head_source(checkout, "missing.py") is None


def test_hunk_headers_give_both_sides_of_the_diff():
    diff = (
        "--- a/api/routes.py\n+++ b/api/routes.py\n"
        "@@ -7,2 +6,0 @@ LIMIT\n"  # a pure deletion: two base lines, sits after head line 6
        "@@ -20 +18,3 @@\n"  # one base line became three
        "@@ -30,0 +31,2 @@\n"  # a pure addition: no base lines
        "--- /dev/null\n+++ b/api/new.py\n@@ -0,0 +1,9 @@\n"
        "--- a/api/gone.py\n+++ /dev/null\n@@ -1,4 +0,0 @@\n"
    )
    head, base = parse_hunk_ranges(diff)
    assert head == {"api/routes.py": [(6, 6), (18, 20), (31, 32)], "api/new.py": [(1, 9)]}
    assert base == {"api/routes.py": [(7, 8), (20, 20)], "api/gone.py": [(1, 4)]}


# ── the plan ───────────────────────────────────────────────────────────────────────────────────


def test_touched_features_rank_ahead_of_siblings_whatever_their_ids():
    plan = plan_features(FEATURES, {"api/routes.py": 6}, cap=2, siblings={"feat_route_add", "feat_route_delete"})
    # by id alone the cap of 2 would keep `add` and `delete`; the touched ones come first instead
    assert [f["id"] for f in plan.selected] == ["feat_route_health", "feat_route_list"]
    assert [f["id"] for f in plan.eligible[2:]] == ["feat_route_add", "feat_route_delete"]
    assert plan.eligible[2]["sibling"] is True and "sibling" not in plan.eligible[0]
    assert (plan.dropped, plan.dropped_siblings, plan.gap, plan.siblings, plan.required) == (2, 2, 0, 2, 2)
    assert "over the per-pass cap" not in plan.summary().split(";")[0]
    assert "2 more own a changed file but the diff touches none of their own code" in plan.summary()


def test_a_touched_feature_over_the_cap_is_still_a_gap():
    plan = plan_features(FEATURES, {"api/routes.py": 6}, cap=1, siblings={"feat_route_delete"})
    assert (plan.dropped, plan.dropped_siblings, plan.gap, plan.required) == (3, 1, 2, 3)
    assert ", 2 over the per-pass cap of 1" in plan.summary() and "1 more own a changed file" in plan.summary()


def test_without_siblings_the_plan_is_what_it_was():
    plan = plan_features(FEATURES, {"api/routes.py": 6}, cap=2)
    assert [f["id"] for f in plan.selected] == ["feat_route_add", "feat_route_delete"]
    assert (plan.dropped, plan.gap, plan.dropped_siblings, plan.required) == (2, 2, 0, 4)


# ── end to end through the runner ──────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_API_KEY", "gk")
    monkeypatch.setenv("GITHUB_TOKEN", "ghtok")
    monkeypatch.delenv("CLAWPATCH_GATEWAY_TIMEOUT_MS", raising=False)

    async def fake_run_gh(args, timeout=30):
        return (0, f"{SHA_HEAD} {SHA_BASE}", "") if args[:1] == ["api"] else (0, "", "")

    monkeypatch.setattr(pp, "run_gh", fake_run_gh)


def make_git(files: dict[str, str], hunks: dict[str, list[tuple[int, int]]], base_files=None, removed=None):
    """A checkout holding `files`, whose diff changes `hunks` (head-side line ranges). `removed`
    gives base-side ranges per file (default: a pure addition) and `base_files` the merge-base text."""
    removed = removed or {}

    async def run_git(args, timeout_s=180):
        if args[0] == "clone":
            for path, text in files.items():
                target = Path(args[-1]) / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        if "--numstat" in args:
            return 0, "".join(f"{sum(b - a + 1 for a, b in h)}\t0\t{p}\n" for p, h in hunks.items()), ""
        if "--unified=0" in args:
            out = ""
            for path, ranges in hunks.items():
                gone = removed.get(path) or [(a, a - 1) for a, _ in ranges]
                out += f"--- a/{path}\n+++ b/{path}\n"
                for (a, b), (c, d) in zip(ranges, gone, strict=True):
                    out += f"@@ -{c},{d - c + 1} +{a},{b - a + 1} @@\n"
            return 0, out, ""
        if "merge-base" in args:
            return (0, SHA_BASE + "\n", "") if base_files is not None else (128, "", "fatal")
        if "show" in args:
            path = args[-1].split(":", 1)[1]
            return (0, base_files[path], "") if path in (base_files or {}) else (128, "", "fatal")
        if "diff" in args:
            return 0, "".join(f"{p}\n" for p in hunks), ""
        return 0, "", ""

    return run_git


class FakeClawpatch:
    def __init__(self, features):
        self.features = features
        self.listed: list[str] = []

    async def __call__(self, args, cwd, env, budget_s):
        state = Path(args[args.index("--state-dir") + 1])
        if "init" in args:
            return 0, "{}", "", False
        if "map" in args:
            (state / "features").mkdir(parents=True, exist_ok=True)
            for f in self.features:
                (state / "features" / f"{f['featureId']}.json").write_text(json.dumps({**f, "status": "pending"}))
            return 0, "{}", "", False
        self.listed = Path(args[args.index("--feature-list") + 1]).read_text().split()
        (state / "runs").mkdir(exist_ok=True)
        (state / "runs" / "20261113T000000-aaaaaa.json").write_text(json.dumps({"claimedFeatureIds": self.listed}))
        stderr = ""
        for fid in self.listed:
            rec = next(f for f in self.features if f["featureId"] == fid)
            (state / "features" / f"{fid}.json").write_text(json.dumps({**rec, "status": "reviewed"}))
            stderr += f"clawpatch review feature-start index=1 total=1 feature={fid} title=t\n"
            stderr += f"clawpatch review feature-done index=1 total=1 feature={fid} findings=0 elapsed=90s\n"
        return 0, "{}", stderr, False


def run(tmp_path, features, hunks, cfg=None, files=SOURCES, **git):
    claw, tele = FakeClawpatch(features), Recorder()
    base = {"checkout_root": str(tmp_path / "co"), "state_root": str(tmp_path / "st"), "default_repo": ""}
    base["structural_result_cache"] = False
    runner = ProtoPatchRunner(
        {**base, **(cfg or {})}, run_git=make_git(files, hunks, **git), run_clawpatch=claw, telemetry=tele
    )
    return runner, claw, tele


EDIT_HEALTH = {"api/routes.py": [(line_of('return "ok"'), line_of('return "ok"'))]}


async def test_the_cap_reviews_the_edited_handler_and_leaving_siblings_out_is_a_complete_pass(tmp_path):
    # Feature ids sort `add` < `delete` < `health` < `list`: by file ownership alone a cap of 2
    # reviews `add` and `delete` and leaves out `health` — the one handler this diff edits (#5173).
    runner, claw, tele = run(tmp_path, FEATURES, EDIT_HEALTH, cfg={"structural_max_features": 2})
    out = await runner.review(5173, "o/r")

    assert claw.listed == ["feat_route_health", "feat_route_add"]
    assert not out.startswith(PARTIAL_PREFIX) and not any(m in out for m in STRUCTURAL_GAP_MARKERS)
    assert "plan: 2 of 4 eligible feature(s) of 4 mapped; 2 more own a changed file but the diff touches none" in out
    [(_, row)] = tele.events
    assert row["outcome"] == "complete" and row["reason"] is None
    assert (row["eligible"], row["selected"], row["dropped"]) == (4, 2, 2)
    assert (row["siblings"], row["dropped_siblings"]) == (3, 2)
    assert [f.get("sibling") for f in row["features"]] == [None, True]


async def test_a_cap_below_the_touched_features_is_still_partial_and_counts_only_them(tmp_path):
    hunks = {"api/routes.py": [(line_of('return scope == "admin"'),) * 2, (line_of('return "ok"'),) * 2]}
    runner, claw, tele = run(tmp_path, FEATURES, hunks, cfg={"structural_max_features": 1})
    out = await runner.review(1, "o/r")

    assert claw.listed == ["feat_route_add"]  # touched: `add` (through `_check`) and `health`
    assert out.startswith(PARTIAL_PREFIX)
    assert "1 of 2 features reviewed" in out.splitlines()[0]
    assert f"{FEATURE_CAP_REASON}: the 1 features with the most changed lines were reviewed, 1 were not" in out
    [(_, row)] = tele.events
    assert row["outcome"] == "partial" and row["reason"] == "feature-cap"
    assert (row["dropped"], row["dropped_siblings"]) == (3, 2)


@pytest.mark.parametrize("value", [False, "false", "off", "0"])
async def test_the_rollback_switch_ranks_and_gaps_by_file_ownership_again(tmp_path, value):
    cfg = {"structural_max_features": 2, "structural_symbol_scope": value}
    runner, claw, tele = run(tmp_path, FEATURES, EDIT_HEALTH, cfg=cfg)
    out = await runner.review(5173, "o/r")

    assert claw.listed == ["feat_route_add", "feat_route_delete"]
    assert out.startswith(PARTIAL_PREFIX) and "2 of 4 features reviewed" in out.splitlines()[0]
    [(_, row)] = tele.events
    assert row["outcome"] == "partial" and (row["siblings"], row["dropped_siblings"]) == (0, 0)


async def test_under_the_cap_every_sibling_is_still_reviewed(tmp_path):
    runner, claw, _ = run(tmp_path, FEATURES, EDIT_HEALTH)
    out = await runner.review(1, "o/r")
    assert claw.listed[0] == "feat_route_health" and sorted(claw.listed) == sorted(f["featureId"] for f in FEATURES)
    assert "plan: 4 of 4 eligible feature(s)" in out and "not reviewed" not in out


async def test_a_helper_removed_by_the_diff_keeps_its_caller_ahead_of_the_siblings(tmp_path):
    head = ROUTES.replace('def _check(scope):\n    return scope == "admin"\n', "")
    at = head.splitlines().index("LIMIT = 10") + 3  # where the deleted lines sat, clear of `LIMIT`
    gone = [(line_of("def _check(scope):"), line_of('return scope == "admin"'))]
    git = dict(files={"api/routes.py": head}, removed={"api/routes.py": gone})
    cfg = {"structural_max_features": 1}

    runner, claw, tele = run(tmp_path / "a", FEATURES, {"api/routes.py": [(at, at)]}, cfg, base_files=SOURCES, **git)
    out = await runner.review(1, "o/r")
    assert claw.listed == ["feat_route_add"]  # the one handler that still calls `_check`
    assert not out.startswith(PARTIAL_PREFIX) and tele.events[0][1]["dropped_siblings"] == 3

    # The merge base cannot be read: the file's base side is unknown, so every feature ranks as
    # touched and the cap is a coverage gap again — never a silent "untouched".
    runner, claw, tele = run(tmp_path / "b", FEATURES, {"api/routes.py": [(at, at)]}, cfg, **git)
    out = await runner.review(1, "o/r")
    assert out.startswith(PARTIAL_PREFIX) and tele.events[0][1]["siblings"] == 0

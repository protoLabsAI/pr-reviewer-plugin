"""Per-file selection (#385 slice 1) — pure: one typed decision per changed file."""

from __future__ import annotations

import pytest
from pr_reviewer.selection import (
    DEFAULT_MAX_PATCH_CHARS,
    DELETED,
    EXCLUDED_GENERATED,
    EXCLUDED_PATH,
    LOCKFILE_NAMES,
    SELECTED,
    TOO_LARGE,
    ChangedFile,
    changed_file_from_github,
    is_linguist_generated,
    parse_linguist_generated,
    select_files,
    selected_paths,
)


def _f(path: str, status: str = "modified", size: int = 100) -> ChangedFile:
    return ChangedFile(path=path, status=status, patch_size=size)


def _reason(path: str, **kwargs) -> str:
    status = kwargs.pop("status", "modified")
    size = kwargs.pop("size", 100)
    (decision,) = select_files([_f(path, status, size)], **kwargs)
    assert decision.path == path
    return decision.reason


def test_ordinary_file_is_selected():
    assert _reason("app.py") == SELECTED
    assert _reason("src/app.py", status="added") == SELECTED
    assert _reason("src/new.py", status="renamed") == SELECTED


@pytest.mark.parametrize("name", sorted(LOCKFILE_NAMES))
def test_each_default_lockfile_is_excluded_by_path(name):
    assert _reason(name) == EXCLUDED_PATH


@pytest.mark.parametrize("path", ["web/package-lock.json", "crates/core/Cargo.lock", "svc/go.sum", "a/b/uv.lock"])
def test_lockfile_in_subdirectory_is_excluded_by_path(path):
    assert _reason(path) == EXCLUDED_PATH


def test_lockfile_name_must_match_exactly():
    assert _reason("uv.lock.py") == SELECTED
    assert _reason("docs/go.sum.md") == SELECTED


def test_minified_js_is_excluded_by_path():
    assert _reason("static/vendor/app.min.js") == EXCLUDED_PATH
    assert _reason("app.min.js") == EXCLUDED_PATH
    assert _reason("static/app.js") == SELECTED


def test_caller_glob_is_added_to_defaults():
    globs = ["vendor/**", "*.snap"]
    assert _reason("vendor/lib/x.go", extra_exclude_globs=globs) == EXCLUDED_PATH
    assert _reason("tests/__snapshots__/a.snap", extra_exclude_globs=globs) == EXCLUDED_PATH
    # defaults still apply alongside the caller's globs
    assert _reason("uv.lock", extra_exclude_globs=globs) == EXCLUDED_PATH
    assert _reason("src/vendor.py", extra_exclude_globs=globs) == SELECTED
    (d,) = select_files([_f("vendor/lib/x.go")], extra_exclude_globs=globs)
    assert d.rule == "vendor/**"


def test_linguist_generated_from_gitattributes_is_excluded():
    attrs = "# generated code\napi/gen/*.py linguist-generated\nschema.pb.go linguist-generated=true\n"
    assert _reason("api/gen/client.py", gitattributes=attrs) == EXCLUDED_GENERATED
    assert _reason("proto/schema.pb.go", gitattributes=attrs) == EXCLUDED_GENERATED
    assert _reason("api/client.py", gitattributes=attrs) == SELECTED


def test_unset_linguist_generated_stays_selected():
    attrs = "gen/** linguist-generated\ngen/keep.py -linguist-generated\nother.py linguist-generated=false\n"
    assert _reason("gen/x.py", gitattributes=attrs) == EXCLUDED_GENERATED
    assert _reason("gen/keep.py", gitattributes=attrs) == SELECTED
    assert _reason("other.py", gitattributes=attrs) == SELECTED


def test_parsed_gitattributes_rules_are_accepted():
    rules = parse_linguist_generated("*.gen.ts linguist-generated text eol=lf\n*.md text\n")
    assert rules == (("*.gen.ts", True),)
    assert is_linguist_generated("web/api.gen.ts", rules)
    assert _reason("web/api.gen.ts", gitattributes=rules) == EXCLUDED_GENERATED


def test_removed_file_is_deleted_never_selected():
    assert _reason("old.py", status="removed") == DELETED
    assert selected_paths(select_files([_f("old.py", "removed")])) == []


def test_patch_over_limit_is_too_large_and_at_limit_stays_selected():
    assert _reason("big.py", size=1001, max_patch_chars=1000) == TOO_LARGE
    assert _reason("edge.py", size=1000, max_patch_chars=1000) == SELECTED
    assert _reason("huge.py", size=DEFAULT_MAX_PATCH_CHARS + 1) == TOO_LARGE
    assert _reason("ok.py", size=DEFAULT_MAX_PATCH_CHARS) == SELECTED


def test_precedence_deleted_then_path_then_generated_then_too_large():
    attrs = "*.lock linguist-generated\ngen/** linguist-generated\n"
    kw = {"gitattributes": attrs, "max_patch_chars": 10}
    assert _reason("uv.lock", status="removed", size=99, **kw) == DELETED
    assert _reason("gen/x.py", status="removed", **kw) == DELETED
    assert _reason("uv.lock", size=99, **kw) == EXCLUDED_PATH
    assert _reason("gen/x.py", size=99, **kw) == EXCLUDED_GENERATED
    assert _reason("big.py", size=99, **kw) == TOO_LARGE


def test_one_decision_per_file_in_input_order():
    files = [
        _f("z.py"),
        _f("uv.lock"),
        _f("gone.py", "removed"),
        _f("a.py", size=10_000),
        _f("m.py"),
    ]
    decisions = select_files(files, max_patch_chars=5_000)
    assert [d.path for d in decisions] == ["z.py", "uv.lock", "gone.py", "a.py", "m.py"]
    assert [d.reason for d in decisions] == [SELECTED, EXCLUDED_PATH, DELETED, TOO_LARGE, SELECTED]
    assert selected_paths(decisions) == ["z.py", "m.py"]
    assert select_files([]) == []


def test_changed_file_from_github_sizes_patch_in_characters():
    row = {"filename": "app.py", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b"}
    assert changed_file_from_github(row) == ChangedFile("app.py", "modified", len(row["patch"]))
    assert changed_file_from_github({"filename": "logo.png", "status": "added"}).patch_size == 0

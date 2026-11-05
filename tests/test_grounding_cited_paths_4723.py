"""protoAgent#4723: ground a finding against every file its evidence cites, not only its anchor
and the PR patch.

On #4723 (head 426610f0) a `uv.lock:3` finding quoted `version: "0.11.29"` from `checks.yml` and
`prepare-release.yml`. Those files live under `.github/workflows/`, so a search of the anchor
(`uv.lock`) and the patch alone could not find the quote and the TRUE finding was downgraded to
`uncertain` — the verdict came out WARN with a non-gating major and the release stalled for a
human. `resolve_cited_paths` resolves a cited bare name against the head tree so the real file is
read; the quote is then found and the finding keeps its grounding.
"""

from __future__ import annotations

from pr_reviewer.grounding import apply_grounding, resolve_cited_paths

# The #4723 shape: anchored in uv.lock, evidence quoting a line that lives in two workflow files.
FINDING_4723 = {
    "file": "uv.lock",
    "line": 3,
    "severity": "major",
    "claim": "uv.lock pins protolabs 0.11.28 but the release workflows pin 0.11.29, so the lock is stale",
    "evidence": 'Both `checks.yml` and `prepare-release.yml` declare `version: "0.11.29"`, but uv.lock:3 still has 0.11.28.',
    "verdict": "confirmed",
}

# The tree at the reviewed head — the workflow files live under `.github/workflows/`, not the root.
HEAD_TREE = {
    "uv.lock",
    "pyproject.toml",
    ".github/workflows/checks.yml",
    ".github/workflows/prepare-release.yml",
}

UV_LOCK = 'version = 1\nrequires-python = ">=3.11"\n# protolabs 0.11.28 is pinned here\n'
CHECKS_YML = (
    'jobs:\n  build:\n    steps:\n      - uses: astral-sh/setup-uv@v3\n        with:\n          version: "0.11.29"\n'
)
PREPARE_YML = "name: prepare-release\njobs:\n  prep:\n    steps:\n      - run: echo nothing quotable here\n"


def _related(finding: dict, tree: set[str] | None, blobs: dict[str, str]) -> dict[str, str]:
    """What `_related_sources` builds: the text of each cited file, resolved against the tree and
    keyed by the cited name (so `related_text` finds it). A resolved path with no blob is skipped,
    exactly as a 404/undecodable read would be."""
    out: dict[str, str] = {}
    for cited, path in resolve_cited_paths(finding, tree).items():
        if path in blobs:
            out[cited] = blobs[path]
    return out


# ── the resolver: a cited bare name resolves to where it lives, restricted to the head tree ──


def test_resolve_cited_paths_resolves_a_bare_name_to_its_subdirectory():
    # r1 / r3: `checks.yml` and `prepare-release.yml` are cited bare but live in `.github/workflows/`.
    resolved = resolve_cited_paths(FINDING_4723, HEAD_TREE)
    assert resolved == {
        "checks.yml": ".github/workflows/checks.yml",
        "prepare-release.yml": ".github/workflows/prepare-release.yml",
    }


def test_resolve_cited_paths_restricts_to_files_in_the_tree():
    # r4: a cited name with no file in the repo at the reviewed head drives no read.
    assert resolve_cited_paths(FINDING_4723, {"uv.lock", "pyproject.toml"}) == {}
    # A path written with a directory that the tree lacks is also dropped — never read outside the repo.
    f = {"file": "a.py", "evidence": "see `does/not/exist.py`"}
    assert resolve_cited_paths(f, {"a.py", "b.py"}) == {}


def test_resolve_cited_paths_prefers_the_cited_files_own_directory():
    # Two subtrees carry a `helpers.py`; the bare cited name binds to the one BESIDE the cited
    # file, not the namesake in another directory.
    f = {"file": "pkg/sub/mod.py", "evidence": "the helper in `helpers.py` is used"}
    tree = {"pkg/sub/mod.py", "pkg/sub/helpers.py", "other/helpers.py"}
    assert resolve_cited_paths(f, tree) == {
        "pkg/sub/helpers.py": "pkg/sub/helpers.py",
        "helpers.py": "pkg/sub/helpers.py",
    }


def test_resolve_cited_paths_takes_a_verbatim_root_file_over_resolution():
    # A cited name that IS a real file at the path written wins verbatim; resolution only steps in
    # when the written path has no file in the tree.
    f = {"file": "pkg/sub/mod.py", "evidence": "the helper in `helpers.py` is used"}
    tree = {"pkg/sub/mod.py", "pkg/sub/helpers.py", "helpers.py"}
    assert resolve_cited_paths(f, tree) == {
        "pkg/sub/helpers.py": "pkg/sub/helpers.py",
        "helpers.py": "helpers.py",
    }


def test_resolve_cited_paths_takes_an_exact_tree_path_verbatim():
    # A fully-spelled path that exists is read as written (the #261 `tests/conftest.py` shape).
    f = {"file": "tests/test_plugin.py", "evidence": "`tests/conftest.py` defines the helper"}
    tree = {"tests/test_plugin.py", "tests/conftest.py"}
    assert resolve_cited_paths(f, tree) == {"tests/conftest.py": "tests/conftest.py"}


def test_resolve_cited_paths_without_a_tree_is_the_pre_tree_behaviour():
    # Fail-open: an unreadable tree (None) maps every candidate to itself, so a related read that
    # would otherwise have landed is still attempted — the behaviour before this fix.
    assert resolve_cited_paths(FINDING_4723, None) == {
        "checks.yml": "checks.yml",
        "prepare-release.yml": "prepare-release.yml",
    }
    assert resolve_cited_paths(FINDING_4723, set()) == {
        "checks.yml": "checks.yml",
        "prepare-release.yml": "prepare-release.yml",
    }


# ── end to end through apply_grounding ──


def test_the_4723_finding_is_downgraded_against_its_anchor_alone():
    # The bug: searched in uv.lock (and its patch) only, the quote is absent and the TRUE major
    # is wrongly downgraded to uncertain.
    out, downgraded, _ = apply_grounding([FINDING_4723], {"uv.lock": UV_LOCK})
    assert downgraded and out[0].get("ungrounded") is True


def test_the_4723_finding_stays_grounded_via_a_resolved_cited_workflow_file():
    # r1 / r3: resolve the cited bare names against the head tree, read the real files, and the
    # quote is found — the finding keeps its grounding and its gating severity.
    blobs = {".github/workflows/checks.yml": CHECKS_YML, ".github/workflows/prepare-release.yml": PREPARE_YML}
    related = _related(FINDING_4723, HEAD_TREE, blobs)
    out, downgraded, _ = apply_grounding([FINDING_4723], {"uv.lock": UV_LOCK, **related})
    assert downgraded == [] and out[0] is FINDING_4723


def test_a_quote_in_no_cited_file_is_still_downgraded():
    # r2: grounding only relaxes when a cited file actually carries the quote. If none does — even
    # with the cited files read — the finding downgrades exactly as today.
    blobs = {
        ".github/workflows/checks.yml": "jobs:\n  build:\n    steps:\n      - run: echo hi\n",
        ".github/workflows/prepare-release.yml": PREPARE_YML,
    }
    related = _related(FINDING_4723, HEAD_TREE, blobs)
    out, downgraded, _ = apply_grounding([FINDING_4723], {"uv.lock": UV_LOCK, **related})
    assert downgraded and out[0].get("ungrounded") is True

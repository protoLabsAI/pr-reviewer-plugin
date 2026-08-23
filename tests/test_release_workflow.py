"""The release workflow is a thin caller for the reusable plugin-release workflow.

Config-as-code: this asserts the wiring that makes releases fire on the right event,
delegate to the pinned reusable workflow, and pass secrets through. A drift here
(wrong repo guard, unpinned ref, missing trigger) breaks releases silently, so the
gate must catch it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _load():
    return yaml.safe_load(RELEASE.read_text())


def test_release_workflow_exists():
    assert RELEASE.is_file()


def test_triggers_on_main_push_and_manual_dispatch():
    # YAML parses the bare `on:` key as the boolean True.
    wf = _load()
    on = wf[True]
    assert on["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in on


def test_calls_pinned_reusable_workflow_with_inherited_secrets():
    job = _load()["jobs"]["release"]
    assert job["uses"] == "protoLabsAI/release-tools/.github/workflows/plugin-release.yml@v2"
    # Pinned to a release tag, not a moving branch.
    assert job["uses"].split("@")[-1] == "v2"
    assert job["secrets"] == "inherit"


def test_guard_restricts_to_upstream_and_release_commits_or_dispatch():
    guard = _load()["jobs"]["release"]["if"]
    # Forks never publish under our name.
    assert "github.repository == 'protoLabsAI/pr-reviewer-plugin'" in guard
    # Manual runs always allowed; auto-runs only on a release commit.
    assert "github.event_name == 'workflow_dispatch'" in guard
    assert "startsWith(github.event.head_commit.message, 'chore: release v')" in guard

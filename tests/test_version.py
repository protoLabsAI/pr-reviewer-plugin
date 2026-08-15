"""Manifest, pyproject, and uv.lock versions move in lockstep."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_versions_in_lockstep():
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert manifest["version"] == pyproject["project"]["version"]


def test_uv_lock_in_lockstep():
    # CI installs via pip, so a stale uv.lock is invisible to the gate
    # unless asserted here (drifted silently at v0.24.0 and v0.25.0).
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    name = pyproject["project"]["name"]
    roots = [pkg for pkg in lock["package"] if pkg["name"] == name and pkg.get("source") == {"virtual": "."}]
    assert len(roots) == 1
    assert roots[0]["version"] == pyproject["project"]["version"]
    assert roots[0]["version"] == manifest["version"]


def test_manifest_ships_disabled_and_names_its_repo():
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert manifest["enabled"] is False
    assert manifest["id"] == "pr-reviewer"
    assert re.match(r"^https://github\.com/", manifest["repository"])

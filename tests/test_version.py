"""Manifest and pyproject versions move in lockstep."""

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


def test_manifest_ships_disabled_and_names_its_repo():
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    assert manifest["enabled"] is False
    assert manifest["id"] == "pr-reviewer"
    assert re.match(r"^https://github\.com/", manifest["repository"])

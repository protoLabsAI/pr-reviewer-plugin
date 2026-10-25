"""The reference docs must describe the code that exists.

A README that is merely INCOMPLETE costs a reader time. One that is WRONG costs them trust,
and this repo's README had become a 676-line changelog that no operator could use to set the
plugin up. The reference now lives in `docs/`. Docs drift because nothing fails when they do;
these tests fail.

They check COVERAGE, not prose: every route, agent tool and config key the code exposes
appears in its reference, and the reference names no route or tool that no longer exists. What
each one MEANS is a human's job — this only guarantees the list is honest and complete.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text()


def _routes() -> set[tuple[str, str]]:
    """(VERB, path) for every route `webhook.py` registers, by AST."""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(ast.parse((ROOT / "webhook.py").read_text())):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            verb = getattr(dec.func, "attr", "")
            owner = getattr(getattr(dec.func, "value", None), "id", "")
            if verb in ("get", "post", "put", "patch", "delete") and owner in ("api", "public"):
                if dec.args and isinstance(dec.args[0], ast.Constant):
                    found.add((verb.upper(), dec.args[0].value))
    return found


def _tool_names() -> set[str]:
    """Names of every `@tool`-decorated function (sync or async) in the tool modules."""
    names: set[str] = set()
    for module in ("protopatch.py", "eval.py"):
        for node in ast.walk(ast.parse((ROOT / module).read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(getattr(d, "id", "") == "tool" for d in node.decorator_list):
                    names.add(node.name)
    return names


def _config_keys() -> set[str]:
    """Every key an operator can set: the manifest's `config:` block plus every key the
    dispatcher or a helper reads out of the config that the manifest does not default."""
    manifest = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    keys = set(manifest["config"]) | set(manifest.get("secrets") or [])
    for module in ("dispatch.py", "lintcheck.py", "absence_search.py", "summon.py", "webhook.py"):
        text = (ROOT / module).read_text()
        keys |= set(re.findall(r'"([a-z][a-z_0-9]+)" in cfg\b', text))
        keys |= set(re.findall(r'\bcfg\.get\(\s*"([a-z][a-z_0-9]+)"', text))
        keys |= set(re.findall(r'\bself\.cfg\.get\(\s*"([a-z][a-z_0-9]+)"', text))
    return keys


def test_every_route_is_in_the_api_reference():
    doc = _read("api.md")
    missing = []
    for verb, path in sorted(_routes()):
        shown = f"{verb} {path}" if path.startswith("/plugins") else f"{verb} {path}"
        if shown not in doc and not (verb == "POST" and path == "/webhook" and "/plugins/pr-reviewer/webhook" in doc):
            missing.append(shown)
    assert not missing, f"routes registered in webhook.py but absent from docs/api.md: {missing}"


def test_api_reference_names_no_route_that_does_not_exist():
    routes = {path for _, path in _routes()}
    documented = set(re.findall(r"^### `(?:GET|POST|PUT|PATCH|DELETE) (/[^`?]*)", _read("api.md"), re.M))
    documented = {p.removeprefix("/plugins/pr-reviewer") for p in documented}
    assert documented <= routes, f"docs/api.md documents routes that no longer exist: {documented - routes}"


def test_every_agent_tool_is_in_the_tools_reference():
    doc = _read("tools.md")
    tools = _tool_names()
    assert tools, "found no @tool functions: the scan is broken, not the docs"
    missing = [t for t in sorted(tools) if f"`{t}`" not in doc]
    assert not missing, f"agent tools absent from docs/tools.md: {missing}"


def test_tools_reference_names_no_tool_that_does_not_exist():
    documented = set(re.findall(r"^## `([a-z_]+)`", _read("tools.md"), re.M))
    assert documented <= _tool_names(), (
        f"docs/tools.md documents tools that no longer exist: {documented - _tool_names()}"
    )


def test_every_config_key_is_in_the_configuration_reference():
    doc = _read("configuration.md")
    keys = _config_keys()
    # Sentinels: a scan that quietly finds nothing would pass this test vacuously.
    assert {"shadow_mode", "panel_attempt_timeout", "evidence_grounding", "max_rounds", "lint_tools_dir"} <= keys
    missing = [k for k in sorted(keys) if f"`{k}`" not in doc]
    assert not missing, f"config keys absent from docs/configuration.md: {missing}"


def test_documented_env_fallbacks_exist_in_code():
    code = "".join((ROOT / m).read_text() for m in ROOT.glob("*.py"))
    envs = set(
        re.findall(r"`((?:PR_REVIEWER|PROTOREVIEW)_[A-Z0-9_]+)`", _read("configuration.md") + _read("install.md"))
    )
    assert envs
    missing = [e for e in sorted(envs) if e not in code]
    assert not missing, f"docs name env vars the code never reads: {missing}"


def test_relative_links_resolve():
    broken = []
    for md in [ROOT / "README.md", *DOCS.glob("*.md")]:
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", md.read_text()):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (md.parent / target).resolve().exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")
    assert not broken, f"broken relative links: {broken}"


def test_every_docs_page_is_in_the_index():
    index = _read("README.md")
    unlisted = [p.name for p in sorted(DOCS.glob("*.md")) if p.name != "README.md" and f"]({p.name})" not in index]
    assert not unlisted, f"docs pages missing from docs/README.md: {unlisted}"

#!/usr/bin/env python3
"""The vendored supersede rule in `scripts/review_at_head.py` (pr-reviewer-plugin#234).

`Review at head` is a required check in repos with no plugin checkout (protoAgent, protoPatch,
qaEngineer), so the rule it shares with the plugin's `QA panel` gate is COPIED into the script
between `BEGIN/END VENDORED SUPERSEDE RULE` markers, with two hashes in its header:

  rounds-ast-sha256  the rule's source in THIS repo (`rounds.py` + the findings-record reader
                     in `verdicts.py`) — `tests/test_vendored_supersede_rule.py` fails when the
                     plugin's rule changes and the copy was not re-vendored;
  block-ast-sha256   the vendored block itself — every copy's own test fails on a local edit.

Both are sha256 over `canonical()` — the AST without comments, docstrings, or empty/None
fields — so a repo's formatter, comments, and the Python minor version never trip them
(`ast.dump` itself changed in 3.13: it omits empty fields).

  python3 scripts/vendor_supersede_rule.py --check          # this repo's copy, both hashes
  python3 scripts/vendor_supersede_rule.py --stamp COMMIT   # re-stamp both hashes + the commit
  python3 scripts/vendor_supersede_rule.py --sync PATH      # copy this block into another
                                                            #   repo's review_at_head.py

Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "review_at_head.py"

BEGIN = "# ── BEGIN VENDORED SUPERSEDE RULE"
END = "# ── END VENDORED SUPERSEDE RULE"

# The plugin definitions the block mirrors, per module: what `rounds-ast-sha256` covers.
SOURCES = {
    "rounds.py": (
        "_BLOCKING",
        "_MAX_DISPOSITION_RECORD_CHARS",
        "_B64URL_RE",
        "_norm",
        "_anchor",
        "_review_id",
        "decode_disposition_record",
        "blocking_priors",
        "supersedes",
        "superseded_fails",
    ),
    "verdicts.py": ("_RECORD_SUMMARY", "_RECORD_RE", "_json_list", "read_findings_record"),
}

_HASH_LINE = re.compile(r"^(#\s+(rounds|block)-ast-sha256:\s*)(\S+)", re.MULTILINE)
_COMMIT = re.compile(r"(`verdicts\.py`\) @ )(\S+)( — )")


def canonical(node) -> str:
    """A version-stable, formatting-free serialization of an AST (sub)tree."""
    if isinstance(node, list):
        items = [
            canonical(n)
            for n in node
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
        ]
        return "[" + ",".join(items) + "]"
    if isinstance(node, ast.AST):
        fields = []
        for name in node._fields:
            value = getattr(node, name, None)
            if value is None or value == []:
                continue
            fields.append(f"{name}={canonical(value)}")
        return f"{type(node).__name__}(" + ",".join(fields) + ")"
    return repr(node)


def block(text: str) -> str:
    """The vendored block's text, BEGIN through END marker lines inclusive."""
    start = text.index(BEGIN)
    end = text.index("\n", text.index(END))
    return text[start:end]


def block_hash(text: str) -> str:
    return hashlib.sha256(canonical(ast.parse(block(text)).body).encode()).hexdigest()


def recorded(text: str) -> dict[str, str]:
    return {m.group(2): m.group(3) for m in _HASH_LINE.finditer(block(text))}


def rounds_hash(root: Path = ROOT) -> str:
    parts = []
    for module, names in SOURCES.items():
        tree = ast.parse((root / module).read_text())
        found = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                found[node.name] = node
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id in names:
                        found[target.id] = node
        missing = [n for n in names if n not in found]
        if missing:
            raise SystemExit(f"{module}: the vendored rule's source is gone: {missing}")
        parts += [canonical(found[n]) for n in names]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def stamp(text: str, *, commit: str | None = None, rounds: str | None = None) -> str:
    current = block(text)
    updated = current
    if commit:
        updated = _COMMIT.sub(lambda m: f"{m.group(1)}{commit}{m.group(3)}", updated, count=1)
    hashes = {"rounds": rounds or recorded(text).get("rounds", ""), "block": block_hash(text)}
    updated = _HASH_LINE.sub(lambda m: f"{m.group(1)}{hashes[m.group(2)]}", updated)
    return text.replace(current, updated)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--stamp", metavar="COMMIT")
    group.add_argument("--sync", metavar="PATH", type=Path)
    args = parser.parse_args()
    text = SCRIPT.read_text()
    if args.stamp:
        SCRIPT.write_text(stamp(text, commit=args.stamp, rounds=rounds_hash()))
        return 0
    if args.sync:
        target = args.sync.read_text()
        args.sync.write_text(target.replace(block(target), block(text)))
        return 0
    want = recorded(text)
    problems = []
    if want.get("rounds") != rounds_hash():
        problems.append("rounds.py's rule changed: re-vendor it, then --stamp")
    if want.get("block") != block_hash(text):
        problems.append("the vendored block was edited: --stamp after re-vendoring")
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())

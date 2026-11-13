"""Which definitions in one Python file does a diff touch? (#340)

clawpatch maps one feature per route handler, and every handler in `routes.py` OWNS the whole
file. A six-line change to two handlers therefore made all 25 route features of that file
"eligible", tied on changed lines, and the per-pass cap then chose among them by feature id:
protoAgent#5173 reviewed 30 handlers the PR never touched and left out one of the two it did.

This module answers the question the planner needs, from the file's AST and the diff's head-side
line ranges alone: the NAMES a change touches —

  - a definition (function / method / class, at any depth) with a changed line of its own, i.e. a
    line inside it that is not inside a definition nested in it;
  - a name bound by a changed statement of a shared scope (the module, or a function that holds
    nested definitions — a route factory's closure variables), and any definition such a changed
    statement mentions (a changed `add_api_route("/x", handler)` touches `handler`);
  - every definition ENCLOSING one with a changed line (a handler is touched by a change inside a
    closure it defines, whether or not it mentions that closure by name);
  - every definition nested under a changed HEADER: the signature, bases or decorators of a function
    or class that holds other definitions (a renamed factory parameter, a changed base class), and
    the header of an `if` / `with` / `try` / loop of a shared scope that wraps definitions;
  - the caller's `seeds`: the names the same analysis finds touched on the BASE side of the diff, so
    a helper, constant or import the diff removes or renames still counts against every definition
    that goes on referring to it;
  - transitively, every definition that references a touched name (a handler that calls a changed
    helper is touched).

It over-includes on purpose: names are matched as bare identifiers and attribute names, so a
changed `get` touches everything that says `.get`. The caller only ever uses the answer to rank a
feature LOWER when it is provably untouched, so a wrong "touched" costs one review and a wrong
"untouched" could cost a finding. Self-contained (no host or plugin imports) and never raises.
"""

from __future__ import annotations

import ast

MAX_SOURCE_BYTES = 1_000_000  # a generated module is not worth a parse; unknown ⇒ treated as touched
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _span(node) -> tuple[int, int]:
    """First to last line of a definition, decorators included (a changed decorator — the route
    path, its dependencies — changes the handler)."""
    lo = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return lo, getattr(node, "end_lineno", None) or node.lineno


def _refs(node) -> set[str]:
    """Every identifier and attribute name a node mentions, nested definitions included."""
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
    return out


def _own_nodes(scope):
    """The nodes of one scope, not descending into the definitions nested in it (those are yielded,
    not entered)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _DEFS):
            stack.extend(ast.iter_child_nodes(node))


def _bound(stmt) -> set[str]:
    """Names a statement binds itself (not what a nested block binds)."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return {(a.asname or a.name).split(".")[0] for a in stmt.names}
    targets: list = []
    if isinstance(stmt, ast.Assign):
        targets = list(stmt.targets)
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.With, ast.AsyncWith)):
        targets = [i.optional_vars for i in stmt.items if i.optional_vars is not None]
    return {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}


def _hit(ranges, lo: int, hi: int) -> bool:
    return any(a <= hi and b >= lo for a, b in ranges)


def _header(node) -> tuple[int, int]:
    """The lines of a definition or compound statement before its first nested statement: the
    signature / bases / decorators, or the `if …:` / `with …:` line(s)."""
    lo = _span(node)[0] if isinstance(node, _DEFS) else node.lineno
    first = next((c.lineno for c in ast.iter_child_nodes(node) if isinstance(c, ast.stmt)), None)
    body = getattr(node, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        first = body[0].lineno
    return lo, max(lo, (first or lo + 1) - 1)


def _nested_defs(node) -> list:
    return [n for n in ast.walk(node) if n is not node and isinstance(n, _DEFS)]


def touched_symbols(source: str, ranges, seeds=()) -> tuple[set[str], set[str]] | None:
    """(names the diff touches, every definition name in the file) — or None when the file cannot
    be analysed (not parseable, too large), which the caller must read as "everything touched".

    `ranges` are `(first, last)` line pairs of this source, inclusive. `seeds` are names already
    known to be touched (the base side's answer, when this is the head side)."""
    try:
        if source is None or len(source) > MAX_SOURCE_BYTES:
            return None
        tree = ast.parse(source)
        spans = [(int(a), int(b)) for a, b in ranges or []]
        defs = [n for n in ast.walk(tree) if isinstance(n, _DEFS)]
        known = {d.name for d in defs}
        parent: dict = {}
        for d in defs:
            for child in _own_nodes(d):
                if isinstance(child, _DEFS):
                    parent[child] = d
        n_lines = source.count("\n") + 2
        # owner[line] = the innermost definition holding that line (None = module level).
        owner: list = [None] * (n_lines + 1)
        for d in sorted(defs, key=lambda d: _span(d)[1] - _span(d)[0], reverse=True):
            lo, hi = _span(d)
            for line in range(max(lo, 1), min(hi, n_lines) + 1):
                owner[line] = d
        touched: set[str] = {str(name) for name in seeds or ()}
        direct: set = set()
        for a, b in spans:
            for line in range(max(a, 1), min(b, n_lines) + 1):
                if owner[line] is not None:
                    direct.add(owner[line])
        for d in direct:
            node = d
            while node is not None:  # a change inside a definition touches everything enclosing it
                touched.add(node.name)
                node = parent.get(node)
            if _hit(spans, *_header(d)):  # a changed signature / base list / decorator
                touched |= {n.name for n in _nested_defs(d)}
        # Changed statements of a SHARED scope: the module, and any definition holding nested
        # definitions. What they bind, and the definitions they mention, are touched too.
        shared = [tree] + [d for d in defs if any(isinstance(n, _DEFS) for n in _own_nodes(d))]
        for scope in shared:
            for node in _own_nodes(scope):
                if not isinstance(node, ast.stmt) or isinstance(node, _DEFS):
                    continue
                compound = any(isinstance(c, ast.stmt) for c in ast.iter_child_nodes(node))
                lo, hi = _header(node) if compound else (node.lineno, getattr(node, "end_lineno", None) or node.lineno)
                if not _hit(spans, lo, hi):
                    continue
                touched |= _bound(node)
                if compound:
                    touched |= {n.name for n in _nested_defs(node)}  # the condition guards them all
                else:
                    touched |= _refs(node) & known
        refs = {d: _refs(d) for d in defs}
        grew = True
        while grew:
            grew = False
            for d in defs:
                if d.name not in touched and refs[d] & touched:
                    touched.add(d.name)
                    grew = True
        return touched, known
    except Exception:  # noqa: BLE001 — SyntaxError, RecursionError, a hostile file: unknown, never a crash
        return None

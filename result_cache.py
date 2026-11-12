"""Per-feature structural results, memoized across passes (issue #312).

A quarter of structural-pass tokens (and feature-seconds) were spent re-reviewing features
that were byte-identical to the previous pass of the same PR — same feature, same prompt
bytes, same owned lines. The checkout cache (`checkout_cache.py`) is content-addressed for
CHECKOUTS only; this store is the same idea for RESULTS: one small JSON entry per
(feature prompt inputs × engine/model/plugin version), holding the raw FindingRecords the
feature's COMPLETED review produced.

The key is `result_key(...)`: sha256 over the feature id, the head blob SHA of every file the
feature owns or lists as context (the inputs its review prompt is built from — issue #312
calls the blob-SHA and prompt-hash formulations equivalent), the engine version
(`clawpatch --version`), the configured `model`, and this plugin's own version. Changing any
of them misses; nothing is ever served across a content, engine, model or plugin change.

Entries live as `<root>/<key>.json` next to the checkout cache root, fresh for `ttl_s` (a
fresh hit gets an mtime touch — LRU bump), swept by `prune()` (TTL + entry cap, the
`CheckoutCache` style), written atomically. Every read and write degrades to a miss / a
no-op: a cache fault must cost a re-run, never the pass (PROTO.md: degrade, never raise).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from .checkout_cache import checkout_root_for

log = logging.getLogger("protoagent.plugins.pr_reviewer")

# Results stay valid for as long as their key's inputs exist — the TTL only bounds growth, so
# it is far longer than the checkout cache's token-lifetime hour. Entries are a few KB each.
DEFAULT_RESULT_TTL_S = 7 * 24 * 3600
DEFAULT_RESULT_ENTRY_LIMIT = 4096

_VERSION_RE = re.compile(r'^version:\s*"?([^"\s#]+)"?', re.M)
_PLUGIN_VERSION: str | None = None


def plugin_version() -> str:
    """This plugin's own version, from the manifest shipped next to the package (kept in
    lockstep with pyproject.toml by tests/test_version.py). "unknown" when unreadable — the
    key then stops telling plugin releases apart but still tells content/engine/model apart."""
    global _PLUGIN_VERSION
    if _PLUGIN_VERSION is None:
        try:
            text = (Path(__file__).resolve().parent / "protoagent.plugin.yaml").read_text()
            m = _VERSION_RE.search(text)
            _PLUGIN_VERSION = m.group(1) if m else "unknown"
        except OSError:
            _PLUGIN_VERSION = "unknown"
    return _PLUGIN_VERSION


def result_root_for(cfg: dict | None) -> Path:
    """The result store root — a `structural-results` dir NEXT TO the checkout cache root, so
    the two content-addressed caches travel together under one configurable home."""
    return checkout_root_for(cfg).parent / "structural-results"


def result_key(
    feature_id: str,
    owned,
    context,
    blobs: dict[str, str],
    engine_version: str,
    model: str,
    plugin: str,
) -> str:
    """The sha256 cache key for one feature's structural result (#312).

    `owned` / `context` are the feature's file paths; `blobs` maps path → head blob SHA. A
    path absent from the head tree hashes as "" — deterministic for a file the feature lists
    but the head deleted. Callers must never pass an EMPTY `blobs` for a readable tree (see
    `ProtoPatchRunner._head_blobs`): an unreadable tree has to sit the cache out, not key
    every feature to the same all-absent hash."""
    payload = {
        "feature": str(feature_id),
        "owned": [[p, blobs.get(p, "")] for p in sorted(owned)],
        "context": [[p, blobs.get(p, "")] for p in sorted(context)],
        "engine": engine_version,
        "model": model,
        "plugin": plugin,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class StructuralResultCache:
    def __init__(
        self,
        root: str | Path,
        *,
        ttl_s: int = DEFAULT_RESULT_TTL_S,
        entry_limit: int = DEFAULT_RESULT_ENTRY_LIMIT,
    ):
        self.root = Path(root)
        self.ttl_s = ttl_s
        self.entry_limit = entry_limit

    def path_for(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> dict | None:
        """The stored entry, or None — missing, stale, unreadable and corrupt all read as a
        plain miss (the feature is then just re-run). A fresh hit gets an mtime touch (LRU)."""
        path = self.path_for(key)
        try:
            if time.time() - path.stat().st_mtime >= self.ttl_s:
                path.unlink(missing_ok=True)
                return None
            entry = json.loads(path.read_text())
            if not isinstance(entry, dict):
                return None
            os.utime(path)  # LRU bump
            return entry
        except (OSError, ValueError):
            return None

    def put(self, key: str, entry: dict) -> None:
        """Store an entry atomically (tmp + rename, so a crashed writer can never leave a
        half-entry that reads as a hit). A failed write is dropped: the next pass re-runs."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.root / f".{key[:16]}.{uuid.uuid4().hex[:8]}.tmp"
            tmp.write_text(json.dumps(entry))
            os.replace(tmp, self.path_for(key))
        except (OSError, TypeError, ValueError):
            log.warning("[pr-reviewer] structural result cache write failed for %s…", key[:12], exc_info=True)

    def prune(self) -> int:
        """TTL sweep (entries and orphaned tmp files), then LRU-evict until under the entry
        cap. Returns entries removed. Maintenance cadence, not the hot path."""
        entries: list[tuple[float, Path]] = []
        removed = 0
        now = time.time()
        if not self.root.is_dir():
            return 0
        for path in self.root.iterdir():
            try:
                if not path.is_file():
                    continue
                mtime = path.stat().st_mtime
                if now - mtime >= self.ttl_s or (path.name.endswith(".tmp") and now - mtime >= 3600):
                    path.unlink(missing_ok=True)
                    removed += 1
                elif path.suffix == ".json":
                    entries.append((mtime, path))
            except OSError:
                continue
        entries.sort()  # oldest first
        while len(entries) > self.entry_limit:
            _mtime, victim = entries.pop(0)
            try:
                victim.unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        return removed

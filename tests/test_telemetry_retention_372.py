"""Telemetry retention (issue #372): prune old day-files, read a recent window."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pr_reviewer
import pytest
from pr_reviewer import telemetry as telemetry_mod
from pr_reviewer.telemetry import Telemetry, parse_retention_days

from tests.conftest import FakeRegistry

# A fixed "now": 2026-11-23 12:00 UTC.
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc).timestamp()
TODAY = datetime.fromtimestamp(NOW, tz=timezone.utc).date()


def _day_name(days_ago: int) -> str:
    return f"{(TODAY - timedelta(days=days_ago)).isoformat()}.jsonl"


def _write_day(t: Telemetry, days_ago: int, event: str | None = None) -> None:
    t.dir.mkdir(parents=True, exist_ok=True)
    (t.dir / _day_name(days_ago)).write_text(json.dumps({"event": event or f"d{days_ago}"}) + "\n", encoding="utf-8")


def _names(t: Telemetry) -> set[str]:
    return {p.name for p in t.dir.iterdir()}


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(telemetry_mod.time, "time", lambda: NOW)


def test_prune_deletes_only_files_older_than_retention(tmp_path):
    t = Telemetry(tmp_path, retention_days=90)
    for ago in (0, 1, 89, 90, 91, 200):
        _write_day(t, ago)
    (t.dir / "notes.jsonl").write_text("{}\n")
    (t.dir / "2020-13-45.jsonl").write_text("{}\n")  # date-shaped, not a date
    (t.dir / "2020-01-01.jsonl.bak").write_text("{}\n")

    assert t.prune(now=NOW) == 2

    assert _names(t) == {
        _day_name(0),
        _day_name(1),
        _day_name(89),
        _day_name(90),
        "notes.jsonl",
        "2020-13-45.jsonl",
        "2020-01-01.jsonl.bak",
    }


def test_prune_uses_the_filename_date_not_the_mtime(tmp_path):
    import os

    t = Telemetry(tmp_path, retention_days=90)
    _write_day(t, 0)
    _write_day(t, 120)
    # A fresh mtime on the old file, an ancient one on today's: neither changes the outcome.
    os.utime(t.dir / _day_name(120), (NOW, NOW))
    os.utime(t.dir / _day_name(0), (0, 0))
    assert t.prune(now=NOW) == 1
    assert _names(t) == {_day_name(0)}


@pytest.mark.parametrize("days", [0, -5])
def test_non_positive_retention_disables_pruning(tmp_path, days):
    t = Telemetry(tmp_path, retention_days=days)
    for ago in (0, 100, 1000):
        _write_day(t, ago)
    assert t.prune(now=NOW) == 0
    assert len(_names(t)) == 3


def test_prune_never_deletes_today_even_with_tiny_retention(tmp_path):
    t = Telemetry(tmp_path, retention_days=1)
    for ago in (0, 1, 2):
        _write_day(t, ago)
    assert t.prune(now=NOW) == 1
    assert _names(t) == {_day_name(0), _day_name(1)}


def test_prune_is_best_effort_and_never_raises(tmp_path, monkeypatch, caplog):
    t = Telemetry(tmp_path, retention_days=90)
    _write_day(t, 200)
    assert t.prune(now=NOW) == 1
    # Missing directory: nothing to do.
    assert Telemetry(tmp_path / "absent").prune(now=NOW) == 0

    _write_day(t, 300)

    def boom(self, *a, **k):
        raise PermissionError("read-only")

    monkeypatch.setattr(telemetry_mod.Path, "unlink", boom)
    with caplog.at_level("WARNING", logger="protoagent.plugins.pr_reviewer"):
        assert t.prune(now=NOW) == 0
    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert _day_name(300) in _names(t)


def test_emit_prunes_at_most_once_per_utc_day(tmp_path, monkeypatch):
    clock = {"now": NOW}
    monkeypatch.setattr(telemetry_mod.time, "time", lambda: clock["now"])
    t = Telemetry(tmp_path, retention_days=90)
    calls: list[float | None] = []
    real_prune = t.prune

    def counting(now=None):
        calls.append(now)
        return real_prune(now)

    monkeypatch.setattr(t, "prune", counting)
    _write_day(t, 200)

    t.emit("a")
    assert len(calls) == 1 and _day_name(200) not in _names(t)

    _write_day(t, 300)
    clock["now"] = NOW + 3600
    t.emit("b")
    t.emit("c")
    assert len(calls) == 1  # same UTC day: no second prune
    assert _day_name(300) in _names(t)

    clock["now"] = NOW + 86400  # next UTC day
    t.emit("d")
    assert len(calls) == 2
    assert _day_name(300) not in _names(t)


def test_emit_still_writes_when_pruning_is_disabled(tmp_path, frozen_now):
    t = Telemetry(tmp_path, retention_days=0)
    _write_day(t, 500)
    t.emit("x", n=1)
    assert _day_name(500) in _names(t)
    assert [e["event"] for e in t.read_all()][-1] == "x"


def test_read_all_since_days_reads_only_the_recent_window(tmp_path, frozen_now):
    t = Telemetry(tmp_path, retention_days=0)
    for ago in (0, 1, 7, 8, 30):
        _write_day(t, ago)
    (t.dir / "notes.jsonl").write_text(json.dumps({"event": "notes"}) + "\n")

    assert [e["event"] for e in t.read_all(since_days=7)] == ["d7", "d1", "d0"]
    assert [e["event"] for e in t.read_all(since_days=0)] == ["d0"]
    # No argument: everything, every *.jsonl, oldest file first — unchanged behaviour.
    assert [e["event"] for e in t.read_all()] == ["d30", "d8", "d7", "d1", "d0", "notes"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 90), ("", 90), ("abc", 90), (True, 90), (30, 30), ("45", 45), (0, 0), (-1, -1)],
)
def test_parse_retention_days(raw, expected):
    assert parse_retention_days(raw) == expected


def _registered_telemetry(reg: FakeRegistry) -> Telemetry:
    return pr_reviewer._MACHINERY[str(pr_reviewer._state_home(reg.config))]["telemetry"]


def test_setting_reaches_the_telemetry_instance(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTOREVIEW_APP_ID", raising=False)
    monkeypatch.delenv("PROTOREVIEW_APP_PRIVATE_KEY", raising=False)
    default = FakeRegistry({"default_repo": "octo/repo", "state_root": str(tmp_path / "a")})
    pr_reviewer.register(default)
    assert _registered_telemetry(default).retention_days == 90

    junk = FakeRegistry(
        {"default_repo": "octo/repo", "state_root": str(tmp_path / "b"), "telemetry_retention_days": "soon"}
    )
    pr_reviewer.register(junk)
    assert _registered_telemetry(junk).retention_days == 90

    home = str(tmp_path / "c")
    first = FakeRegistry({"default_repo": "octo/repo", "state_root": home, "telemetry_retention_days": 14})
    pr_reviewer.register(first)
    t = _registered_telemetry(first)
    assert t.retention_days == 14
    # A config reload reuses the running telemetry but picks up the new retention.
    second = FakeRegistry({"default_repo": "octo/repo", "state_root": home, "telemetry_retention_days": 0})
    pr_reviewer.register(second)
    assert _registered_telemetry(second) is t and t.retention_days == 0


def test_time_module_is_the_one_patched():
    # Guard the fixtures above: telemetry reads `time.time` via the module attribute.
    assert telemetry_mod.time is time

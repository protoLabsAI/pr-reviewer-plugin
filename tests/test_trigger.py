"""The structural trigger — Quinn's verbatim thresholds/regex, server-side data only."""

from __future__ import annotations

from pr_reviewer.trigger import structural_trigger


def test_small_clean_pr_runs_lite():
    fires, reasons = structural_trigger(changed_files=1, lines_changed=15, changed_paths=["src/util.py"])
    assert not fires and reasons == []


def test_file_count_threshold_is_strictly_more_than_three():
    assert not structural_trigger(changed_files=3, lines_changed=1, changed_paths=["a", "b", "c"])[0]
    fires, reasons = structural_trigger(changed_files=4, lines_changed=1, changed_paths=["a", "b", "c", "d"])
    assert fires and "4 files changed" in reasons[0]


def test_line_threshold_is_strictly_more_than_120():
    assert not structural_trigger(changed_files=1, lines_changed=120, changed_paths=["a.py"])[0]
    fires, reasons = structural_trigger(changed_files=1, lines_changed=121, changed_paths=["a.py"])
    assert fires and "121 lines changed" in reasons[0]


def test_sensitive_paths_fire_regardless_of_size():
    for path in (
        "src/auth/login.py",
        "lib/session.ts",
        "billing/invoice.py",
        "db/migrations/0042.sql",  # 'migrat' stem
        ".github/workflows/ci.yml",
        "Dockerfile",
        "deploy/docker-compose.yml",
        "compose.yaml",
        "config/oauth.ts",
        "vault/secrets.py",
    ):
        fires, reasons = structural_trigger(changed_files=1, lines_changed=1, changed_paths=[path])
        assert fires and reasons[0].startswith("sensitive path(s):"), path


def test_dotgithub_matches_as_prefix_not_substring_stem():
    # `.github/` fires via the path-prefix alternative; a file merely named
    # 'mygithub.py' must not.
    assert structural_trigger(changed_files=1, lines_changed=1, changed_paths=[".github/dependabot.yml"])[0]
    assert not structural_trigger(changed_files=1, lines_changed=1, changed_paths=["docs/mygithub.py"])[0]


def test_reasons_accumulate_and_sensitive_list_caps_at_five():
    paths = [f"auth/f{i}.py" for i in range(8)]
    fires, reasons = structural_trigger(changed_files=8, lines_changed=500, changed_paths=paths)
    assert fires and len(reasons) == 3
    assert reasons[2].count(",") == 4  # 5 paths shown


def test_telemetry_reads_back_what_it_wrote(tmp_path):
    from pr_reviewer.telemetry import Telemetry

    t = Telemetry(tmp_path)
    t.emit("dispatch", repo="o/r", pr=1, decision="accept")
    t.emit("drop", repo="o/r", pr=1, reason="cooldown")
    events = t.read_all()
    assert [e["event"] for e in events] == ["dispatch", "drop"]
    assert all("ts" in e for e in events)

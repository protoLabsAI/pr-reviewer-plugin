"""On-demand review (issue #28, slice 1) — parsing, admin gate, forced re-review."""

from __future__ import annotations

from pr_reviewer.summon import help_text, is_admin, parse_command, refusal_text

HANDLES = ["vera", "qa-bot[bot]"]


# ── parsing: only something a human typed on purpose ─────────────────────────


def test_a_verb_after_the_handle_is_the_command():
    assert parse_command("@vera review", HANDLES) == "review"
    assert parse_command("hey @vera review please", HANDLES) == "review"
    assert parse_command("@VERA Review", HANDLES) == "review"


def test_a_bare_mention_asks_for_help():
    # Someone typing "@vera" wants to know what this thing does, not silence.
    assert parse_command("@vera", HANDLES) == "help"
    assert parse_command("@vera\n", HANDLES) == "help"


def test_prose_about_the_reviewer_is_not_a_command():
    # The trigger must be an @-mention, or a PR discussing the panel spends a panel.
    assert parse_command("vera flagged this earlier", HANDLES) is None
    assert parse_command("the vera review was wrong", HANDLES) is None
    assert parse_command("see averatar@example.com", HANDLES) is None


def test_a_quoted_summon_does_not_re_fire():
    # A reply quoting an earlier command must not run a second panel.
    assert parse_command("> @vera review\n\nagreed, it was wrong", HANDLES) is None


def test_the_bots_own_login_is_a_handle():
    assert parse_command("@qa-bot[bot] review", HANDLES) == "review"


def test_an_unknown_verb_is_returned_so_the_caller_can_be_answered():
    # Not None: an unrecognised verb gets help back, never silence.
    assert parse_command("@vera frobnicate", HANDLES) == "frobnicate"


# ── admin gate: server-side, fails closed ────────────────────────────────────


async def _gh(result: tuple[int, str, str]):
    async def run_gh(args, timeout=30):
        return result

    return run_gh


async def test_only_admin_permission_passes():
    assert await is_admin(await _gh((0, "admin\n", "")), "o/r", "someone") is True
    for perm in ("write", "maintain", "read", "triage", "none"):
        assert await is_admin(await _gh((0, perm, "")), "o/r", "someone") is False


async def test_an_unreadable_permission_is_not_admin():
    # Fails closed: a wrong True lets anyone spend a five-subagent panel at will.
    assert await is_admin(await _gh((1, "", "404")), "o/r", "someone") is False


async def test_a_missing_login_or_repo_is_not_admin():
    assert await is_admin(await _gh((0, "admin", "")), "o/r", "") is False
    assert await is_admin(await _gh((0, "admin", "")), "not-a-repo", "someone") is False


async def test_permission_is_read_from_github_not_the_payload():
    seen = {}

    async def run_gh(args, timeout=30):
        seen["args"] = args
        return 0, "admin", ""

    await is_admin(run_gh, "o/r", "someone")
    assert seen["args"][1] == "repos/o/r/collaborators/someone/permission"


# ── the replies a human actually sees ────────────────────────────────────────


def test_help_names_the_commands_and_the_cost():
    text = help_text(HANDLES)
    assert "@vera review" in text and "@vera help" in text
    assert "admin" in text.lower()
    assert "5–9 min" in text or "5-9 min" in text  # the cost is why it is gated


def test_refusal_names_the_person_the_verb_and_the_reason():
    text = refusal_text("someone", "review")
    assert "@someone" in text and "`review`" in text and "admin" in text

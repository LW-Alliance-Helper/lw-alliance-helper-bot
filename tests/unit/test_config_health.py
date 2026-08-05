"""The shared config-health mechanism (#414 / #379).

This is the generalization of #413's stuck-watcher notice, so these tests are
largely that feature's guarantees restated against the shared machinery:

  * A problem repeating on every tick posts once, then goes quiet.
  * An unfixed problem re-nudges after a day, so one post can't scroll away.
  * A *different* problem on the same subject is heard immediately rather than
    hiding behind the old one's quiet window.
  * A recovery is confirmed, because an alliance that was told the feature was
    dead needs to know their fix took.

Plus the two things the generalization added: several broken subjects batch
into one digest instead of one embed each, and a guild whose leadership
channel is itself unreachable still gets the notice somewhere.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import config  # noqa: E402
import config_health  # noqa: E402
from tests.constants import TEST_GUILD_ID  # noqa: E402

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
LEADERSHIP_ID = 111111111111111111
SYSTEM_ID = 555555555555555555

SHEET_SUBJECT = "test.sheet"
CHANNEL_SUBJECT = "test.channel"


@pytest.fixture(autouse=True)
def _subjects():
    """Register throwaway subjects, then restore the real registry.

    Registration is global module state, so a test that leaks a subject would
    change what another test's digest renders.
    """
    saved = dict(config_health._SUBJECTS)
    config_health.register(
        config_health.Subject(
            key=SHEET_SUBJECT, label="your test sheet", fix_hub="/test", fix_btn="Set it up"
        )
    )
    config_health.register(config_health.Subject(key=CHANNEL_SUBJECT, label="your test channel"))
    yield
    config_health._SUBJECTS.clear()
    config_health._SUBJECTS.update(saved)


def _perms(view=True, send=True):
    p = MagicMock()
    p.view_channel = view
    p.send_messages = send
    return p


def _channel(channel_id, *, view=True, send=True):
    ch = AsyncMock()
    ch.id = channel_id
    ch.send = AsyncMock()
    ch.permissions_for = MagicMock(return_value=_perms(view, send))
    return ch


def _bot(guild_id=TEST_GUILD_ID, *, leadership=None, system=None):
    guild = MagicMock()
    guild.id = guild_id
    guild.me = MagicMock()
    guild.system_channel = system
    bot = MagicMock()
    bot.get_guild = MagicMock(return_value=guild)
    bot.get_channel = MagicMock(return_value=leadership)
    return bot, guild


def _sent_embeds(channel):
    return [c.kwargs["embed"] for c in channel.send.call_args_list]


# ── Signature ────────────────────────────────────────────────────────────────


class TestSignature:
    def test_same_problem_same_signature(self):
        a = config_health.signature_for("missing_tab", "Applicants")
        b = config_health.signature_for("missing_tab", "Applicants")
        assert a == b

    def test_kind_and_discriminator_both_distinguish(self):
        base = config_health.signature_for("missing_tab", "Applicants")
        assert base != config_health.signature_for("no_access", "Applicants")
        assert base != config_health.signature_for("missing_tab", "Other")

    def test_blank_discriminator_is_stable(self):
        assert config_health.signature_for("channel_gone") == config_health.signature_for(
            "channel_gone", ""
        )


# ── Subject registry ─────────────────────────────────────────────────────────


class TestSubjects:
    def test_registered_label_is_used(self):
        assert config_health.get_subject(SHEET_SUBJECT).label == "your test sheet"

    def test_unregistered_subject_falls_back_rather_than_raising(self):
        """A row can outlive its registration. A vague notice beats a notifier
        pass that dies and takes every other guild's digest with it."""
        assert config_health.get_subject("nope.gone").label == "part of your setup"

    def test_fix_instruction_names_the_owning_surface(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        problem = config_health.problems(TEST_GUILD_ID)[0]
        text = config_health.fix_instruction(problem)
        assert "/test" in text and "Set it up" in text

    def test_fix_instruction_without_a_hub_still_says_something(self, temp_db):
        config_health.record(
            TEST_GUILD_ID, CHANNEL_SUBJECT, config_health.CHANNEL_NO_VIEW, "", now=NOW
        )
        problem = config_health.problems(TEST_GUILD_ID)[0]
        text = config_health.fix_instruction(problem)
        assert "View Channel" in text
        assert "None" not in text

    def test_no_access_points_at_sharing_not_at_setup(self, temp_db):
        """#413's rule: re-picking the sheet does not fix a permissions problem,
        so the copy must not send leadership down that path first."""
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.NO_ACCESS, "", now=NOW)
        problem = config_health.problems(TEST_GUILD_ID)[0]
        assert "Share" in config_health.fix_instruction(problem)


# ── Recording ────────────────────────────────────────────────────────────────


class TestRecord:
    def test_records_a_problem(self, temp_db):
        config_health.record(
            TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "tab gone", now=NOW
        )
        problems = config_health.problems(TEST_GUILD_ID)
        assert len(problems) == 1
        assert problems[0].kind == config_health.MISSING_TAB
        assert problems[0].detail == "tab gone"
        assert problems[0].notified_at is None

    def test_same_problem_again_keeps_the_quiet_window(self, temp_db):
        """The whole point: a poll that fails every 30 minutes must not
        re-alert. notified_at surviving the second record is what holds it."""
        config_health.record(
            TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "x", discriminator="A", now=NOW
        )
        config_health._mark_notified(TEST_GUILD_ID, [SHEET_SUBJECT], NOW)
        config_health.record(
            TEST_GUILD_ID,
            SHEET_SUBJECT,
            config_health.MISSING_TAB,
            "x",
            discriminator="A",
            now=NOW + timedelta(minutes=30),
        )
        problem = config_health.problems(TEST_GUILD_ID)[0]
        assert problem.notified_at == NOW.isoformat()
        assert problem.first_seen_at == NOW.isoformat()

    def test_a_different_problem_resets_the_quiet_window(self, temp_db):
        config_health.record(
            TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "x", discriminator="A", now=NOW
        )
        config_health._mark_notified(TEST_GUILD_ID, [SHEET_SUBJECT], NOW)
        config_health.record(
            TEST_GUILD_ID,
            SHEET_SUBJECT,
            config_health.NO_ACCESS,
            "y",
            now=NOW + timedelta(minutes=30),
        )
        problem = config_health.problems(TEST_GUILD_ID)[0]
        assert problem.notified_at is None
        assert problem.kind == config_health.NO_ACCESS

    def test_subjects_are_independent(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        config_health.record(
            TEST_GUILD_ID, CHANNEL_SUBJECT, config_health.CHANNEL_GONE, "", now=NOW
        )
        assert len(config_health.problems(TEST_GUILD_ID)) == 2

    def test_healthy_guild_has_no_rows(self, temp_db):
        assert config_health.problems(TEST_GUILD_ID) == []


class TestIsNewProblem:
    def test_unknown_subject_is_new(self, temp_db):
        assert config_health.is_new_problem(TEST_GUILD_ID, SHEET_SUBJECT, "missing_tab") is True

    def test_same_signature_is_not_new(self, temp_db):
        config_health.record(
            TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", discriminator="A", now=NOW
        )
        assert (
            config_health.is_new_problem(
                TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, discriminator="A"
            )
            is False
        )

    def test_changed_discriminator_is_new(self, temp_db):
        config_health.record(
            TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", discriminator="A", now=NOW
        )
        assert (
            config_health.is_new_problem(
                TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, discriminator="B"
            )
            is True
        )

    def test_a_resolved_problem_coming_back_is_new(self, temp_db):
        config_health.record(
            TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", discriminator="A", now=NOW
        )
        config_health._mark_notified(TEST_GUILD_ID, [SHEET_SUBJECT], NOW)
        config_health.clear(TEST_GUILD_ID, SHEET_SUBJECT, now=NOW)
        assert (
            config_health.is_new_problem(
                TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, discriminator="A"
            )
            is True
        )


class TestClear:
    def test_clearing_an_unannounced_problem_deletes_it(self, temp_db):
        """Nobody was told there was a problem, so confirming a recovery would
        be the bot's first and only word on the subject."""
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        config_health.clear(TEST_GUILD_ID, SHEET_SUBJECT, now=NOW)
        assert config_health.problems(TEST_GUILD_ID) == []
        assert config_health._pending(NOW) == []

    def test_clearing_an_announced_problem_leaves_a_recovery_owed(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        config_health._mark_notified(TEST_GUILD_ID, [SHEET_SUBJECT], NOW)
        config_health.clear(TEST_GUILD_ID, SHEET_SUBJECT, now=NOW)
        assert config_health.problems(TEST_GUILD_ID) == []  # not a problem any more
        pending = config_health._pending(NOW)
        assert len(pending) == 1 and pending[0].resolved_at is not None

    def test_clearing_a_healthy_subject_is_a_noop(self, temp_db):
        config_health.clear(TEST_GUILD_ID, SHEET_SUBJECT, now=NOW)
        assert config_health._pending(NOW) == []


# ── Which rows owe a post ────────────────────────────────────────────────────


class TestPending:
    def test_unnotified_problem_is_pending(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        assert len(config_health._pending(NOW)) == 1

    def test_inside_the_quiet_window_is_not_pending(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        config_health._mark_notified(TEST_GUILD_ID, [SHEET_SUBJECT], NOW)
        assert config_health._pending(NOW + timedelta(hours=6)) == []

    def test_renudges_after_a_day(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        config_health._mark_notified(TEST_GUILD_ID, [SHEET_SUBJECT], NOW)
        later = NOW + timedelta(hours=config_health.RENOTIFY_HOURS, minutes=1)
        assert len(config_health._pending(later)) == 1


# ── Embeds ───────────────────────────────────────────────────────────────────


class TestEmbeds:
    def _problems(self, n, temp_db):
        for i in range(n):
            config_health.register(config_health.Subject(key=f"test.s{i}", label=f"thing {i}"))
            config_health.record(
                TEST_GUILD_ID, f"test.s{i}", config_health.MISSING_TAB, f"detail {i}", now=NOW
            )
        return config_health.problems(TEST_GUILD_ID)

    def test_single_problem_reads_singular(self, temp_db):
        items = self._problems(1, temp_db)
        embed = config_health.build_digest_embed(items)
        assert "Something I was told to use" in embed.description
        assert len(embed.fields) == 1

    def test_several_problems_are_one_embed(self, temp_db):
        """A reorg that breaks six things is one problem to the alliance, and
        six red posts read as six emergencies."""
        items = self._problems(4, temp_db)
        embed = config_health.build_digest_embed(items)
        assert "4 things" in embed.description
        assert len(embed.fields) == 4

    def test_detail_wins_over_the_generic_reason(self, temp_db):
        items = self._problems(1, temp_db)
        assert "detail 0" in config_health.describe(items[0])

    def test_missing_detail_falls_back_to_the_kind_copy(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.NO_ACCESS, "", now=NOW)
        problem = config_health.problems(TEST_GUILD_ID)[0]
        assert "permission" in config_health.describe(problem)

    def test_a_long_list_is_truncated_with_a_pointer(self, temp_db):
        items = self._problems(config_health._MAX_DIGEST_FIELDS + 3, temp_db)
        embed = config_health.build_digest_embed(items)
        assert len(embed.fields) == config_health._MAX_DIGEST_FIELDS + 1
        assert "3 other" in embed.fields[-1].value

    def test_recovery_names_what_came_back(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        embed = config_health.build_recovery_embed(config_health.problems(TEST_GUILD_ID))
        assert "your test sheet" in embed.description


# ── Where the notice goes ────────────────────────────────────────────────────


class TestChannelResolution:
    def test_prefers_the_leadership_channel(self, seeded_db):
        leadership = _channel(LEADERSHIP_ID)
        bot, guild = _bot(leadership=leadership)
        assert config_health.resolve_notice_channel(bot, guild) is leadership

    def test_falls_back_to_the_system_channel(self, seeded_db):
        """The leadership channel is itself a piece of config that can rot, and
        it's the one subject this mechanism can't announce in the usual place."""
        system = _channel(SYSTEM_ID)
        bot, guild = _bot(leadership=None, system=system)
        assert config_health.resolve_notice_channel(bot, guild) is system

    def test_unpostable_leadership_channel_falls_through(self, seeded_db):
        blocked = _channel(LEADERSHIP_ID, send=False)
        system = _channel(SYSTEM_ID)
        bot, guild = _bot(leadership=blocked, system=system)
        assert config_health.resolve_notice_channel(bot, guild) is system

    def test_nowhere_to_post_returns_none(self, seeded_db):
        bot, guild = _bot(leadership=None, system=None)
        assert config_health.resolve_notice_channel(bot, guild) is None

    def test_unpostable_system_channel_is_not_used(self, seeded_db):
        system = _channel(SYSTEM_ID, view=False)
        bot, guild = _bot(leadership=None, system=system)
        assert config_health.resolve_notice_channel(bot, guild) is None


# ── The notifier pass ────────────────────────────────────────────────────────


class TestNotifierPass:
    @pytest.mark.asyncio
    async def test_posts_a_digest_and_marks_notified(self, seeded_db):
        config_health.record(
            TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "tab gone", now=NOW
        )
        leadership = _channel(LEADERSHIP_ID)
        bot, _ = _bot(leadership=leadership)

        posted = await config_health.run_notifier_pass(bot, now=NOW)

        assert posted == 1
        assert leadership.send.await_count == 1
        assert _sent_embeds(leadership)[0].title == config_health.STUCK_TITLE
        assert config_health.problems(TEST_GUILD_ID)[0].notified_at == NOW.isoformat()

    @pytest.mark.asyncio
    async def test_second_pass_inside_the_window_stays_quiet(self, seeded_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        leadership = _channel(LEADERSHIP_ID)
        bot, _ = _bot(leadership=leadership)

        await config_health.run_notifier_pass(bot, now=NOW)
        await config_health.run_notifier_pass(bot, now=NOW + timedelta(minutes=15))

        assert leadership.send.await_count == 1

    @pytest.mark.asyncio
    async def test_several_broken_subjects_are_one_post(self, seeded_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        config_health.record(
            TEST_GUILD_ID, CHANNEL_SUBJECT, config_health.CHANNEL_GONE, "", now=NOW
        )
        leadership = _channel(LEADERSHIP_ID)
        bot, _ = _bot(leadership=leadership)

        await config_health.run_notifier_pass(bot, now=NOW)

        assert leadership.send.await_count == 1
        assert len(_sent_embeds(leadership)[0].fields) == 2

    @pytest.mark.asyncio
    async def test_recovery_is_posted_and_the_row_disappears(self, seeded_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        leadership = _channel(LEADERSHIP_ID)
        bot, _ = _bot(leadership=leadership)
        await config_health.run_notifier_pass(bot, now=NOW)

        config_health.clear(TEST_GUILD_ID, SHEET_SUBJECT, now=NOW + timedelta(hours=1))
        await config_health.run_notifier_pass(bot, now=NOW + timedelta(hours=1))

        titles = [e.title for e in _sent_embeds(leadership)]
        assert titles == [config_health.STUCK_TITLE, config_health.RECOVERED_TITLE]
        assert config_health._pending(NOW + timedelta(days=7)) == []

    @pytest.mark.asyncio
    async def test_recovery_for_something_never_announced_says_nothing(self, seeded_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        config_health.clear(TEST_GUILD_ID, SHEET_SUBJECT, now=NOW)
        leadership = _channel(LEADERSHIP_ID)
        bot, _ = _bot(leadership=leadership)

        assert await config_health.run_notifier_pass(bot, now=NOW) == 0
        leadership.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_reachable_channel_still_marks_notified(self, seeded_db):
        """Otherwise every pass forever re-attempts a guild that can't be
        reached. The hub and setup banners carry the state for that case."""
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        bot, _ = _bot(leadership=None, system=None)

        posted = await config_health.run_notifier_pass(bot, now=NOW)

        assert posted == 0
        assert config_health.problems(TEST_GUILD_ID)[0].notified_at == NOW.isoformat()

    @pytest.mark.asyncio
    async def test_a_send_failure_does_not_retry_every_pass(self, seeded_db):
        import discord

        leadership = _channel(LEADERSHIP_ID)
        leadership.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "nope"))
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        bot, _ = _bot(leadership=leadership)

        await config_health.run_notifier_pass(bot, now=NOW)
        await config_health.run_notifier_pass(bot, now=NOW + timedelta(minutes=15))

        assert leadership.send.await_count == 1

    @pytest.mark.asyncio
    async def test_rows_for_a_guild_the_bot_left_are_dropped(self, seeded_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        bot = MagicMock()
        bot.get_guild = MagicMock(return_value=None)

        assert await config_health.run_notifier_pass(bot, now=NOW) == 0
        assert config_health.problems(TEST_GUILD_ID) == []

    @pytest.mark.asyncio
    async def test_nothing_pending_is_a_cheap_noop(self, seeded_db):
        leadership = _channel(LEADERSHIP_ID)
        bot, _ = _bot(leadership=leadership)
        assert await config_health.run_notifier_pass(bot, now=NOW) == 0
        leadership.send.assert_not_awaited()


# ── Schema ───────────────────────────────────────────────────────────────────


class TestSchema:
    def test_the_retired_transfer_columns_are_gone(self, temp_db):
        """#413's three columns moved into guild_config_health. Leaving them
        behind would break `GuildConfig(**dict(row))`-style construction and
        leave two sources of truth."""
        with config._get_conn() as conn:
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(guild_transfer_config)").fetchall()
            }
        assert "sheet_error_signature" not in cols
        assert "sheet_error_detail" not in cols
        assert "sheet_error_notified_at" not in cols

    def test_init_db_is_idempotent(self, temp_db):
        config_health.record(TEST_GUILD_ID, SHEET_SUBJECT, config_health.MISSING_TAB, "", now=NOW)
        config.init_db()
        assert len(config_health.problems(TEST_GUILD_ID)) == 1


class TestMigrationFromTheOldColumns:
    """#413's state has to survive the move, or the deploy that ships this
    re-notifies every alliance whose watcher is stuck right now."""

    @pytest.fixture
    def legacy_db(self, tmp_path, monkeypatch):
        """A database still on the pre-#414 schema, with one stuck guild."""
        import sqlite3

        db_path = str(tmp_path / "legacy.db")

        def patched_get_conn():
            conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
            conn.row_factory = sqlite3.Row
            return conn

        monkeypatch.setattr(config, "DB_PATH", db_path)
        monkeypatch.setattr(config, "_get_conn", patched_get_conn)

        with patched_get_conn() as conn:
            conn.execute("""
                CREATE TABLE guild_transfer_config (
                    guild_id                INTEGER PRIMARY KEY,
                    sheet_error_signature   TEXT DEFAULT '',
                    sheet_error_detail      TEXT DEFAULT '',
                    sheet_error_notified_at TEXT DEFAULT ''
                )
            """)
            conn.execute(
                "INSERT INTO guild_transfer_config VALUES (?, ?, ?, ?)",
                (
                    TEST_GUILD_ID,
                    "alliance|missing_tab|Applicants",
                    "That spreadsheet no longer has a tab named `Applicants`.",
                    NOW.isoformat(),
                ),
            )
            conn.commit()
        return db_path

    def test_a_stuck_guild_carries_over(self, legacy_db):
        config.init_db()
        problems = config_health.problems(TEST_GUILD_ID)
        assert len(problems) == 1
        assert problems[0].subject == "transfer.alliance"
        assert problems[0].kind == "missing_tab"
        assert "Applicants" in problems[0].detail

    def test_the_quiet_window_carries_over_so_it_does_not_re_notify(self, legacy_db):
        config.init_db()
        problem = config_health.problems(TEST_GUILD_ID)[0]
        assert problem.notified_at == NOW.isoformat()
        assert config_health._pending(NOW + timedelta(hours=1)) == []

    def test_the_old_columns_are_gone_afterwards(self, legacy_db):
        config.init_db()
        with config._get_conn() as conn:
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(guild_transfer_config)").fetchall()
            }
        assert not {c for c in cols if c.startswith("sheet_error_")}

    def test_migration_is_idempotent(self, legacy_db):
        config.init_db()
        config.init_db()
        assert len(config_health.problems(TEST_GUILD_ID)) == 1

    def test_a_healthy_guild_ports_nothing(self, tmp_path, monkeypatch):
        import sqlite3

        db_path = str(tmp_path / "legacy_clean.db")

        def patched_get_conn():
            conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
            conn.row_factory = sqlite3.Row
            return conn

        monkeypatch.setattr(config, "DB_PATH", db_path)
        monkeypatch.setattr(config, "_get_conn", patched_get_conn)
        with patched_get_conn() as conn:
            conn.execute("""
                CREATE TABLE guild_transfer_config (
                    guild_id              INTEGER PRIMARY KEY,
                    sheet_error_signature TEXT DEFAULT ''
                )
            """)
            conn.execute("INSERT INTO guild_transfer_config VALUES (?, '')", (TEST_GUILD_ID,))
            conn.commit()

        config.init_db()
        assert config_health.problems(TEST_GUILD_ID) == []


if __name__ == "__main__":
    pytest.main([__file__])

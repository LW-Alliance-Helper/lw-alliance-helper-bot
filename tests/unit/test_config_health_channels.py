"""Channel rot (#379): the clock-driven post loops and the /setup surface.

The reported case: several alliances had a configured channel become
unreachable to the bot in a channel reorg, and the per-minute post loop
skipped them for days with nothing but a `print()` in Railway logs. No Sentry
signal, no in-Discord indication, no way for leadership to know without
someone grepping server logs.

Two halves here. The push half records from the loops so the config-health
digest says so. The pull half marks the channel on `/setup` → View
configuration, which is where someone goes when they suspect something.

Note the ticket's first candidate fix (Sentry-capture an unresolvable
channel) is deliberately *not* implemented: it predates #413, and config rot
is the alliance's to fix rather than a bug to page on.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import config_health  # noqa: E402
from tests.constants import TEST_GUILD_ID  # noqa: E402

CHANNEL_ID = 900100200300


def _perms(view=True, send=True):
    p = MagicMock()
    p.view_channel = view
    p.send_messages = send
    return p


def _channel(view=True, send=True, guild_id=TEST_GUILD_ID):
    guild = MagicMock()
    guild.id = guild_id
    guild.me = MagicMock()
    ch = MagicMock()
    ch.id = CHANNEL_ID
    ch.guild = guild
    ch.permissions_for = MagicMock(return_value=_perms(view, send))
    return ch


def _bot(channel=None):
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    return bot


def _problem(subject="test.channel"):
    return next((p for p in config_health.problems(TEST_GUILD_ID) if p.subject == subject), None)


class TestCheckChannel:
    def test_healthy_channel_is_none(self):
        assert config_health.check_channel(_bot(_channel()), CHANNEL_ID) is None

    def test_unset_channel_is_not_a_problem(self):
        """Plenty of guilds deliberately leave an optional post channel blank."""
        assert config_health.check_channel(_bot(), 0) is None
        assert config_health.check_channel(_bot(), None) is None

    def test_unresolvable_channel_is_gone(self):
        assert config_health.check_channel(_bot(None), CHANNEL_ID) == config_health.CHANNEL_GONE

    def test_no_view_permission(self):
        assert (
            config_health.check_channel(_bot(_channel(view=False)), CHANNEL_ID)
            == config_health.CHANNEL_NO_VIEW
        )

    def test_no_send_permission(self):
        assert (
            config_health.check_channel(_bot(_channel(send=False)), CHANNEL_ID)
            == config_health.CHANNEL_NO_SEND
        )

    def test_a_shape_without_permissions_is_not_invented_as_a_problem(self):
        """A DM or an odd channel type shouldn't manufacture an alert."""
        odd = MagicMock(spec=[])
        assert config_health.check_channel(_bot(odd), CHANNEL_ID) is None

    def test_a_throwing_permissions_lookup_does_not_break_the_caller(self):
        ch = _channel()
        ch.permissions_for = MagicMock(side_effect=RuntimeError("boom"))
        assert config_health.check_channel(_bot(ch), CHANNEL_ID) is None

    def test_a_non_permissions_return_does_not_break_the_caller(self):
        """Every caller is inside a per-minute loop or a render path, so an
        unreadable shape has to resolve to "no problem" rather than raising."""
        ch = _channel()
        ch.permissions_for = MagicMock(return_value=object())
        assert config_health.check_channel(_bot(ch), CHANNEL_ID) is None


class TestCheckChannelPrecise:
    @pytest.mark.asyncio
    async def test_cache_answer_wins_when_it_is_definite(self):
        bot = _bot(_channel(send=False))
        bot.fetch_channel = MagicMock()
        assert (
            await config_health.check_channel_precise(bot, CHANNEL_ID)
            == config_health.CHANNEL_NO_SEND
        )
        bot.fetch_channel.assert_not_called()

    @pytest.mark.asyncio
    async def test_403_separates_invisible_from_deleted(self):
        """The one thing the cheap check can't tell you."""
        import discord

        bot = _bot(None)

        async def _fetch(_id):
            raise discord.Forbidden(MagicMock(), "nope")

        bot.fetch_channel = _fetch
        assert (
            await config_health.check_channel_precise(bot, CHANNEL_ID)
            == config_health.CHANNEL_NO_VIEW
        )

    @pytest.mark.asyncio
    async def test_404_is_genuinely_gone(self):
        import discord

        bot = _bot(None)

        async def _fetch(_id):
            raise discord.NotFound(MagicMock(), "gone")

        bot.fetch_channel = _fetch
        assert (
            await config_health.check_channel_precise(bot, CHANNEL_ID) == config_health.CHANNEL_GONE
        )


class TestNoteChannel:
    def test_a_broken_channel_records(self, temp_db):
        assert (
            config_health.note_channel(_bot(None), TEST_GUILD_ID, "test.channel", CHANNEL_ID)
            is False
        )
        assert _problem().kind == config_health.CHANNEL_GONE

    def test_a_healthy_channel_clears(self, temp_db):
        config_health.record(TEST_GUILD_ID, "test.channel", config_health.CHANNEL_GONE, "")
        assert (
            config_health.note_channel(_bot(_channel()), TEST_GUILD_ID, "test.channel", CHANNEL_ID)
            is True
        )
        assert _problem() is None

    def test_repeating_on_every_tick_stays_one_problem(self, temp_db):
        """The loops call this per minute; it must not churn the row."""
        bot = _bot(None)
        for _ in range(5):
            config_health.note_channel(bot, TEST_GUILD_ID, "test.channel", CHANNEL_ID)
        assert len(config_health.problems(TEST_GUILD_ID)) == 1
        assert _problem().notified_at is None

    def test_a_changed_channel_id_is_a_new_problem(self, temp_db):
        """Re-pointing at a second broken channel deserves a fresh notice
        rather than hiding behind the first one's quiet window."""
        bot = _bot(None)
        config_health.note_channel(bot, TEST_GUILD_ID, "test.channel", CHANNEL_ID)
        config_health._mark_notified(
            TEST_GUILD_ID, ["test.channel"], config_health.datetime.now(config_health.timezone.utc)
        )
        config_health.note_channel(bot, TEST_GUILD_ID, "test.channel", CHANNEL_ID + 1)
        assert _problem().notified_at is None


class TestResolveConfiguredChannel:
    def test_returns_the_channel_when_usable(self, temp_db):
        ch = _channel()
        got = config_health.resolve_configured_channel(
            _bot(ch), TEST_GUILD_ID, "test.channel", CHANNEL_ID
        )
        assert got is ch

    def test_returns_none_and_records_when_not(self, temp_db):
        got = config_health.resolve_configured_channel(
            _bot(None), TEST_GUILD_ID, "test.channel", CHANNEL_ID
        )
        assert got is None
        assert _problem() is not None

    def test_no_send_permission_is_not_usable(self, temp_db):
        """Resolvable is not the same as postable — the old `is None` check
        missed exactly this case."""
        got = config_health.resolve_configured_channel(
            _bot(_channel(send=False)), TEST_GUILD_ID, "test.channel", CHANNEL_ID
        )
        assert got is None
        assert _problem().kind == config_health.CHANNEL_NO_SEND


class TestLoopSubjectsAreRegistered:
    """A subject without copy renders as "part of your setup", which would make
    the digest useless for naming which channel to go fix."""

    @pytest.mark.parametrize(
        "module_name,attr",
        [
            ("bot", "SHINY_POST_CHANNEL_SUBJECT"),
            ("train_cog", "TRAIN_REMINDER_CHANNEL_SUBJECT"),
            ("survey", "SURVEY_REMINDER_CHANNEL_SUBJECT"),
            ("storm_signup_scheduler", "SIGNUP_CHANNEL_SUBJECT"),
            # #462: the event scheduler was the loop #379 never reached.
            ("scheduler", "EVENT_DRAFT_CHANNEL_SUBJECT"),
            ("scheduler", "EVENT_ANNOUNCE_CHANNEL_SUBJECT"),
        ],
    )
    def test_subject_has_copy(self, module_name, attr):
        module = __import__(module_name)
        key = getattr(module, attr)
        subject = config_health.get_subject(key)
        assert subject.label != "part of your setup"
        assert subject.fix_hub and subject.fix_btn


class TestStormSignupStatusMapping:
    def test_channel_statuses_map_to_kinds(self):
        import storm_signup_scheduler as sched

        assert sched._CHANNEL_STATUS_KINDS["channel_gone"] == config_health.CHANNEL_GONE
        assert sched._CHANNEL_STATUS_KINDS["forbidden"] == config_health.CHANNEL_NO_SEND

    def test_non_channel_statuses_are_not_mapped(self):
        """`no_channel` means nothing is configured and `missing_slot_labels`
        is a different setup gap — neither is a channel that rotted."""
        import storm_signup_scheduler as sched

        assert "no_channel" not in sched._CHANNEL_STATUS_KINDS
        assert "missing_slot_labels" not in sched._CHANNEL_STATUS_KINDS


if __name__ == "__main__":
    pytest.main([__file__])

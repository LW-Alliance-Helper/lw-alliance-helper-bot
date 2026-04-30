"""
Tests for `TrainCog.check_reminder` — the @tasks.loop background task
that fires the daily train reminder + birthday announcement at each
guild's configured local time.

Audit gap #3: the actual loop body (guild iteration, time-match check,
ReminderView post, and the Premium DM to the assigned member) was
untested. Only `dm.py` had unit coverage.

The reminder path is critical: the train reminder is the bot's
flagship daily announcement. A regression that silently mis-fires
or stops firing entirely costs the alliance a daily community moment.

Coverage:
  * Train reminder fires only at the guild-local hour:minute that
    matches `train_cfg.reminder_time`.
  * Wrong minute or wrong hour → no post (most ticks of the per-minute
    loop hit this branch).
  * `reminders_fired` set prevents double-fire within the same day.
  * `reminders_enabled=0` → no fire even when time matches.
  * Premium DM-to-assignee fires alongside the channel post.
  * Free-tier guilds: channel post still goes out (DM may silently
    no-op via `dm.send_dm`); not a regression target here, but the
    channel post must always work.
"""

from __future__ import annotations

import os
import sys
from datetime import date as date_cls, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

GUILD_ID            = 12345
LEADERSHIP_CHAN_ID  = 1111
REMINDER_CHAN_ID    = 5555
ET = ZoneInfo("America/New_York")


# ── Fixtures and helpers ─────────────────────────────────────────────────────

def _make_cog():
    """A real TrainCog instance with a mocked bot. Subsequent tests
    overwrite `cog.bot.guilds` and patch the config loaders."""
    import train_cog
    bot = MagicMock()
    bot.guilds      = []
    bot.get_channel = MagicMock(return_value=None)
    cog = train_cog.TrainCog.__new__(train_cog.TrainCog)
    cog.bot                  = bot
    cog.last_reminder_date   = None
    cog.reminder_sent_today  = False
    cog.reminders_fired      = set()
    return cog


def _make_guild(guild_id: int = GUILD_ID):
    g = MagicMock()
    g.id   = guild_id
    g.name = "Test Alliance"
    return g


def _make_cfg(timezone: str = "America/New_York"):
    cfg = MagicMock()
    cfg.guild_id              = GUILD_ID
    cfg.setup_complete        = True
    cfg.leadership_channel_id = LEADERSHIP_CHAN_ID
    cfg.timezone              = timezone
    return cfg


def _train_cfg(*, reminders_enabled: int = 1, reminder_time: str = "22:00",
               reminder_channel_id: int = REMINDER_CHAN_ID,
               blurbs_enabled: int = 1, dm_message: str = ""):
    return {
        "reminders_enabled":   reminders_enabled,
        "reminder_time":       reminder_time,
        "reminder_channel_id": reminder_channel_id,
        "blurbs_enabled":      blurbs_enabled,
        "dm_message":          dm_message,
    }


def _bday_cfg(enabled: int = 0):
    """Birthday config kept disabled for train-focused tests so we
    don't have to mock load_birthdays."""
    return {
        "enabled":              enabled,
        "train_integration":    0,
        "reminders_enabled":    0,
        "reminder_time":        "08:00",
        "reminder_channel_id":  0,
        "tab_name":             "Birthdays",
    }


async def _run_loop(cog, *, now_in_guild_tz: datetime, schedule: dict | None = None,
                    train_cfg=None, bday_cfg=None,
                    bot_guilds=None, channels: dict | None = None,
                    mention_resolver=None):
    """Drive a single `check_reminder` tick with everything mocked.

    `now_in_guild_tz` is what `datetime.now(tz=...)` returns inside the
    loop body — pin this to control whether the time-match branch
    fires."""
    import train_cog
    cog.bot.guilds  = bot_guilds or [_make_guild()]
    if channels is None:
        channels = {}
    cog.bot.get_channel = MagicMock(side_effect=lambda cid: channels.get(cid))

    schedule    = schedule or {}
    train_cfg   = train_cfg or _train_cfg()
    bday_cfg    = bday_cfg  or _bday_cfg()
    mention_fn  = mention_resolver or AsyncMock(return_value="**alice**")

    # `datetime.now(tz=ET)` and `datetime.now(tz=guild_tz)` both run
    # inside check_reminder. Patching `train_cog.datetime` covers both.
    fake_dt = MagicMock(wraps=datetime)
    fake_dt.now = MagicMock(return_value=now_in_guild_tz)

    send_dm_spy = AsyncMock(return_value=True)

    with patch("train_cog.datetime", fake_dt), \
         patch("config.get_config", return_value=_make_cfg()), \
         patch("config.get_train_config", return_value=train_cfg), \
         patch("config.get_birthday_config", return_value=bday_cfg), \
         patch("train_cog.load_schedule", return_value=schedule), \
         patch("dm.send_dm", send_dm_spy), \
         patch("dm.mention_or_name", mention_fn):
        await type(cog).check_reminder.coro(cog)

    return send_dm_spy


# ── Time-match firing ────────────────────────────────────────────────────────

class TestTrainReminderFiringAtConfiguredTime:

    @pytest.mark.asyncio
    async def test_fires_when_local_time_matches(self):
        cog = _make_cog()
        chan = AsyncMock(); chan.send = AsyncMock()
        today_iso = date_cls.today().isoformat()

        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )

        chan.send.assert_called_once()
        body = chan.send.await_args.args[0]
        assert "alice" in body or "**alice**" in body
        assert GUILD_ID in cog.reminders_fired

    @pytest.mark.asyncio
    async def test_does_not_fire_when_minute_does_not_match(self):
        """The loop runs every minute. Most ticks land off-time."""
        cog = _make_cog()
        chan = AsyncMock(); chan.send = AsyncMock()

        today_iso = date_cls.today().isoformat()
        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 1, tzinfo=ET),  # one minute past
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )
        chan.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_when_hour_does_not_match(self):
        cog = _make_cog()
        chan = AsyncMock(); chan.send = AsyncMock()
        today_iso = date_cls.today().isoformat()
        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 21, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )
        chan.send.assert_not_called()


# ── Idempotency ──────────────────────────────────────────────────────────────

class TestTrainReminderIdempotency:

    @pytest.mark.asyncio
    async def test_same_guild_does_not_fire_twice_in_one_day(self):
        cog = _make_cog()
        chan = AsyncMock(); chan.send = AsyncMock()
        today_iso = date_cls.today().isoformat()

        # First tick fires.
        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )
        assert chan.send.await_count == 1

        # Second tick (same minute) should be suppressed by reminders_fired.
        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )
        assert chan.send.await_count == 1, \
            "Reminder should be suppressed on second tick of same day"


# ── Configuration gates ──────────────────────────────────────────────────────

class TestTrainReminderConfigGates:

    @pytest.mark.asyncio
    async def test_reminders_enabled_zero_skips_post(self):
        cog = _make_cog()
        chan = AsyncMock(); chan.send = AsyncMock()
        today_iso = date_cls.today().isoformat()

        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            train_cfg=_train_cfg(reminders_enabled=0),
            channels={REMINDER_CHAN_ID: chan},
        )
        chan.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_assignment_today_marks_fired_and_skips(self):
        """Empty schedule for today → no post but reminders_fired
        gets the guild added so we don't re-check next minute."""
        cog = _make_cog()
        chan = AsyncMock(); chan.send = AsyncMock()

        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={},
            channels={REMINDER_CHAN_ID: chan},
        )
        chan.send.assert_not_called()
        assert GUILD_ID in cog.reminders_fired

    @pytest.mark.asyncio
    async def test_missing_reminder_channel_marks_fired_and_skips(self):
        """Configured reminder channel id is unknown to the bot →
        skip cleanly, mark fired so we don't retry every tick."""
        cog = _make_cog()
        today_iso = date_cls.today().isoformat()
        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={},  # nothing wired up
        )
        assert GUILD_ID in cog.reminders_fired


# ── Premium DM-to-assignee ───────────────────────────────────────────────────

class TestTrainReminderPremiumDM:

    @pytest.mark.asyncio
    async def test_dm_sent_to_assignee_alongside_channel_post(self):
        """The Premium feature: after posting to the channel, also DM
        the member assigned to today's train. Free-tier guilds: dm.send_dm
        is wired to silently no-op via mention_or_name returning name."""
        cog = _make_cog()
        chan = AsyncMock(); chan.send = AsyncMock()
        today_iso = date_cls.today().isoformat()

        send_dm_spy = await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )

        send_dm_spy.assert_awaited_once()
        # `dm.send_dm(bot, guild_id, name, content=...)` — verify name + body.
        args   = send_dm_spy.await_args.args
        kwargs = send_dm_spy.await_args.kwargs
        assert args[1] == GUILD_ID
        assert args[2] == "alice"
        assert "today's train is for you" in kwargs["content"]

    @pytest.mark.asyncio
    async def test_dm_uses_configured_template_when_set(self):
        """When `guild_train_config.dm_message` is non-empty, the
        Premium DM uses that text (with `{name}` substituted) instead
        of the hardcoded default."""
        cog = _make_cog()
        chan = AsyncMock(); chan.send = AsyncMock()
        today_iso = date_cls.today().isoformat()

        custom = "Hey {name}, train day! Don't forget to fill it out."
        send_dm_spy = await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            train_cfg=_train_cfg(dm_message=custom),
            channels={REMINDER_CHAN_ID: chan},
        )

        body = send_dm_spy.await_args.kwargs["content"]
        assert body == "Hey alice, train day! Don't forget to fill it out."
        # Default copy must NOT bleed through.
        assert "Heads up" not in body


# ── Daily reset ──────────────────────────────────────────────────────────────

class TestTrainReminderDailyReset:

    @pytest.mark.asyncio
    async def test_reminders_fired_clears_when_date_rolls_over(self):
        """`reminders_fired` is a per-day set; rollover at midnight ET
        clears it so the next day's reminder can fire."""
        cog = _make_cog()
        cog.reminders_fired    = {GUILD_ID}
        cog.last_reminder_date = date_cls(2026, 5, 14)  # yesterday

        chan = AsyncMock(); chan.send = AsyncMock()
        today_iso = date_cls.today().isoformat()
        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )
        chan.send.assert_called_once(), \
            "After date rollover, reminders_fired is cleared and reminder fires again"

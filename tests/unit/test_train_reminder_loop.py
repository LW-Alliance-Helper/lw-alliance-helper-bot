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

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

GUILD_ID = 12345
LEADERSHIP_CHAN_ID = 1111
REMINDER_CHAN_ID = 5555
ET = ZoneInfo("America/New_York")

# Tests pin `datetime.now(tz=ET)` to 2026-05-15 22:00 ET inside the loop.
# The train reminder fires at the in-game server reset and resolves "today"
# against the Last War server date (UTC-2) via `config.server_date_for`, so a
# 10pm EDT fire is already the next in-game day — 2026-05-16. Schedule keys
# must match that server date, not the local calendar date, for the lookup to
# hit. (The birthday auto-population path still keys off the local date.)
PATCHED_TODAY_ISO = "2026-05-16"


# ── Fixtures and helpers ─────────────────────────────────────────────────────


def _make_cog():
    """A real TrainCog instance with a mocked bot. Subsequent tests
    overwrite `cog.bot.guilds` and patch the config loaders. The
    birthday auto-pop dedup lives in SQLite (`guild_birthday_config
    .last_train_population_date`) post-#89, so tests that exercise the
    dedup path patch the SQLite helpers instead of poking at a cog
    attribute."""
    import train_cog

    bot = MagicMock()
    bot.guilds = []
    bot.get_channel = MagicMock(return_value=None)
    cog = train_cog.TrainCog.__new__(train_cog.TrainCog)
    cog.bot = bot
    cog.last_reminder_date = None
    cog.reminders_fired = set()
    return cog


def _make_guild(guild_id: int = GUILD_ID):
    g = MagicMock()
    g.id = guild_id
    g.name = "Test Alliance"
    return g


def _make_cfg(timezone: str = "America/New_York"):
    cfg = MagicMock()
    cfg.guild_id = GUILD_ID
    cfg.setup_complete = True
    cfg.leadership_channel_id = LEADERSHIP_CHAN_ID
    cfg.timezone = timezone
    return cfg


def _train_cfg(
    *,
    reminders_enabled: int = 1,
    reminder_time: str = "22:00",
    reminder_channel_id: int = REMINDER_CHAN_ID,
    blurbs_enabled: int = 1,
    dm_message: str = "",
):
    return {
        "reminders_enabled": reminders_enabled,
        "reminder_time": reminder_time,
        "reminder_channel_id": reminder_channel_id,
        "blurbs_enabled": blurbs_enabled,
        "dm_message": dm_message,
    }


def _bday_cfg(enabled: int = 0):
    """Birthday config kept disabled for train-focused tests so we
    don't have to mock load_birthdays."""
    return {
        "enabled": enabled,
        "train_integration": 0,
        "reminders_enabled": 0,
        "reminder_time": "08:00",
        "reminder_channel_id": 0,
        "tab_name": "Birthdays",
    }


async def _run_loop(
    cog,
    *,
    now_in_guild_tz: datetime,
    schedule: dict | None = None,
    train_cfg=None,
    bday_cfg=None,
    bot_guilds=None,
    channels: dict | None = None,
    mention_resolver=None,
):
    """Drive a single `check_reminder` tick with everything mocked.

    `now_in_guild_tz` is what `datetime.now(tz=...)` returns inside the
    loop body — pin this to control whether the time-match branch
    fires."""
    import train_cog

    cog.bot.guilds = bot_guilds or [_make_guild()]
    if channels is None:
        channels = {}
    cog.bot.get_channel = MagicMock(side_effect=lambda cid: channels.get(cid))

    schedule = schedule or {}
    train_cfg = train_cfg or _train_cfg()
    bday_cfg = bday_cfg or _bday_cfg()
    mention_fn = mention_resolver or AsyncMock(return_value="**alice**")

    # `datetime.now(tz=ET)` and `datetime.now(tz=guild_tz)` both run
    # inside check_reminder. Patching `train_cog.datetime` covers both.
    fake_dt = MagicMock(wraps=datetime)
    fake_dt.now = MagicMock(return_value=now_in_guild_tz)

    send_dm_spy = AsyncMock(return_value=True)

    with (
        patch("train_cog.datetime", fake_dt),
        patch("config.get_config", return_value=_make_cfg()),
        patch("config.get_train_config", return_value=train_cfg),
        patch("config.get_birthday_config", return_value=bday_cfg),
        patch("train_cog.load_schedule", return_value=schedule),
        patch("dm.send_dm", send_dm_spy),
        patch("dm.mention_or_name", mention_fn),
        # The loop stamps a heartbeat at the end of each tick (#227); these
        # tests run without a real DB, so no-op it.
        patch("config.stamp_loop_heartbeat"),
    ):
        await type(cog).check_reminder.coro(cog)

    return send_dm_spy


# ── Time-match firing ────────────────────────────────────────────────────────


class TestTrainReminderFiringAtConfiguredTime:
    @pytest.mark.asyncio
    async def test_fires_when_local_time_matches(self):
        cog = _make_cog()
        chan = AsyncMock()
        chan.send = AsyncMock()
        today_iso = PATCHED_TODAY_ISO

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
        chan = AsyncMock()
        chan.send = AsyncMock()

        today_iso = PATCHED_TODAY_ISO
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
        chan = AsyncMock()
        chan.send = AsyncMock()
        today_iso = PATCHED_TODAY_ISO
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
        chan = AsyncMock()
        chan.send = AsyncMock()
        today_iso = PATCHED_TODAY_ISO

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
        assert chan.send.await_count == 1, (
            "Reminder should be suppressed on second tick of same day"
        )


# ── Configuration gates ──────────────────────────────────────────────────────


class TestTrainReminderConfigGates:
    @pytest.mark.asyncio
    async def test_reminders_enabled_zero_skips_post(self):
        cog = _make_cog()
        chan = AsyncMock()
        chan.send = AsyncMock()
        today_iso = PATCHED_TODAY_ISO

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
        chan = AsyncMock()
        chan.send = AsyncMock()

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
        today_iso = PATCHED_TODAY_ISO
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
        chan = AsyncMock()
        chan.send = AsyncMock()
        today_iso = PATCHED_TODAY_ISO

        send_dm_spy = await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )

        send_dm_spy.assert_awaited_once()
        # `dm.send_dm(bot, guild_id, name, content=...)` — verify name + body.
        args = send_dm_spy.await_args.args
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
        chan = AsyncMock()
        chan.send = AsyncMock()
        today_iso = PATCHED_TODAY_ISO

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
        cog.reminders_fired = {GUILD_ID}
        cog.last_reminder_date = date_cls(2026, 5, 14)  # yesterday

        chan = AsyncMock()
        chan.send = AsyncMock()
        today_iso = PATCHED_TODAY_ISO
        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )
        (
            chan.send.assert_called_once(),
            "After date rollover, reminders_fired is cleared and reminder fires again",
        )


# ── Birthday auto-population gate ────────────────────────────────────────────


class TestBirthdayAutoPopulationGate:
    """The birthday → train auto-population must fire exactly once per
    day at 22:00 ET (10pm ET == 00:00 server time, matching the
    alliance's nightly reset). Regression for issue #29: previously
    fired on every Railway redeploy and at 8pm ET (UTC midnight) due
    to a tz-naive `date.today()` rollover gate."""

    def _bday_on(self):
        cfg = _bday_cfg(enabled=1)
        cfg["train_integration"] = 1
        return cfg

    @pytest.mark.asyncio
    async def test_fires_at_22_00_ET(self):
        cog = _make_cog()
        with (
            patch("train_cog.check_and_add_birthdays", return_value=({}, [])) as mock_pop,
            patch("train_cog.save_schedule"),
            patch("config.get_birthday_population_last_fired", return_value=""),
            patch("config.mark_birthday_population_fired") as mock_mark,
        ):
            await _run_loop(
                cog,
                now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
                bday_cfg=self._bday_on(),
            )
        mock_pop.assert_called_once()
        # After firing, the SQLite stamp should land with today's ISO date.
        mock_mark.assert_called_once_with(GUILD_ID, "2026-05-15")

    @pytest.mark.asyncio
    async def test_does_not_fire_at_deploy_time(self):
        """Most ticks of the per-minute loop are not 22:00 — including
        every redeploy that lands at some other minute. Regression for
        the 'fires on every push' bug."""
        cog = _make_cog()
        with (
            patch("train_cog.check_and_add_birthdays", return_value=({}, [])) as mock_pop,
            patch("train_cog.save_schedule"),
            patch("config.mark_birthday_population_fired") as mock_mark,
        ):
            await _run_loop(
                cog,
                now_in_guild_tz=datetime(2026, 5, 15, 10, 23, tzinfo=ET),
                bday_cfg=self._bday_on(),
            )
        mock_pop.assert_not_called()
        mock_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_one_minute_off(self):
        """Same exact-minute discipline as the train reminder."""
        cog = _make_cog()
        with (
            patch("train_cog.check_and_add_birthdays", return_value=({}, [])) as mock_pop,
            patch("train_cog.save_schedule"),
        ):
            await _run_loop(
                cog,
                now_in_guild_tz=datetime(2026, 5, 15, 22, 1, tzinfo=ET),
                bday_cfg=self._bday_on(),
            )
        mock_pop.assert_not_called()

    @pytest.mark.asyncio
    async def test_does_not_fire_twice_in_one_day(self):
        """Dedup persists in `guild_birthday_config.last_train_population_date`
        so Railway restarts at 22:00 don't re-fire the conflict alerts.
        Same-minute retick (or any retick before midnight ET) with the
        SQLite stamp present must not re-run. Regression for #89."""
        cog = _make_cog()
        with (
            patch("train_cog.check_and_add_birthdays", return_value=({}, [])) as mock_pop,
            patch("train_cog.save_schedule"),
            patch("config.get_birthday_population_last_fired", return_value="2026-05-15"),
            patch("config.mark_birthday_population_fired") as mock_mark,
        ):
            await _run_loop(
                cog,
                now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
                bday_cfg=self._bday_on(),
            )
        mock_pop.assert_not_called()
        # Already-fired path must not re-stamp either.
        mock_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_train_integration_off_skips(self):
        cog = _make_cog()
        bcfg = self._bday_on()
        bcfg["train_integration"] = 0
        with (
            patch("train_cog.check_and_add_birthdays", return_value=({}, [])) as mock_pop,
            patch("train_cog.save_schedule"),
        ):
            await _run_loop(
                cog,
                now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
                bday_cfg=bcfg,
            )
        mock_pop.assert_not_called()


# ── Birthday channel Forbidden isolation ─────────────────────────────────────


class TestBirthdayChannelForbiddenIsolation:
    """Regression for 1.1.4: a `discord.Forbidden` raised by
    `bday_channel.send` (e.g. bot lacks Send Messages on the
    configured birthday channel for one alliance) used to escape the
    inner loop, hit the broad outer `except Exception`, and abort
    every other guild's birthday announcement for that minute. The
    fix catches Forbidden at the channel-send call, logs the channel
    info, and breaks out of that single guild's birthday loop —
    without unwinding the whole task."""

    def _bday_announcing(self):
        """Birthday config that announces (not auto-pop) at the same
        time the test pins the loop to."""
        cfg = _bday_cfg(enabled=1)
        cfg["reminders_enabled"] = 1
        cfg["reminder_time"] = "08:00"
        cfg["reminder_channel_id"] = REMINDER_CHAN_ID
        return cfg

    @pytest.mark.asyncio
    async def test_forbidden_on_bday_channel_send_does_not_propagate(self, capsys):
        """When the channel send raises Forbidden, the exception must
        be swallowed and a per-guild log line must name the channel —
        otherwise the outer loop catches a generic 'Error during
        birthday check' and we can't tell leadership which alliance to
        fix."""
        cog = _make_cog()

        # Member dict month/day must match real today, since the loop
        # filters via `_d2.today()` (not the patched ET datetime).
        from datetime import date as _date

        real_today = _date.today()
        members = [
            {
                "name": "alice",
                "month": real_today.month,
                "day": real_today.day,
                "discord_id": None,
            }
        ]

        chan = AsyncMock()
        chan.name = "birthdays"
        forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access")
        chan.send = AsyncMock(side_effect=forbidden)

        with patch("train_cog.load_birthdays", return_value=members):
            # Loop must complete cleanly even though the send raises.
            await _run_loop(
                cog,
                now_in_guild_tz=datetime(2026, 5, 15, 8, 0, tzinfo=ET),
                bday_cfg=self._bday_announcing(),
                channels={REMINDER_CHAN_ID: chan},
            )

        chan.send.assert_called_once()
        out = capsys.readouterr().out
        assert "Missing perms" in out, "Forbidden must produce the per-guild diagnostic log line"
        assert str(REMINDER_CHAN_ID) in out, (
            "Log must name the channel id so leadership knows what to fix"
        )
        assert "birthdays" in out, "Log must include the channel name"
        assert str(GUILD_ID) in out, "Log must name the guild"

    @pytest.mark.asyncio
    async def test_forbidden_breaks_inner_member_loop(self, capsys):
        """Two birthdays today, both would fail with Forbidden — the
        loop must break after the first failure rather than spamming
        identical errors for every member."""
        cog = _make_cog()

        from datetime import date as _date

        real_today = _date.today()
        members = [
            {"name": "alice", "month": real_today.month, "day": real_today.day, "discord_id": None},
            {"name": "bob", "month": real_today.month, "day": real_today.day, "discord_id": None},
        ]

        chan = AsyncMock()
        chan.name = "birthdays"
        forbidden = discord.Forbidden(MagicMock(status=403, reason="Forbidden"), "Missing Access")
        chan.send = AsyncMock(side_effect=forbidden)

        with patch("train_cog.load_birthdays", return_value=members):
            await _run_loop(
                cog,
                now_in_guild_tz=datetime(2026, 5, 15, 8, 0, tzinfo=ET),
                bday_cfg=self._bday_announcing(),
                channels={REMINDER_CHAN_ID: chan},
            )

        assert chan.send.await_count == 1, (
            "After the first Forbidden, remaining members for that guild are skipped"
        )


# ── ReminderView lifecycle ───────────────────────────────────────────────────


class TestReminderViewMessageWiring:
    """The reminder loop must capture the sent message into
    `view.message` so the View's 1-hour on_timeout can strip the
    button. Without this, the prompt button stays visible all night
    after the view stops listening — clicks fail with 'Interaction
    failed'."""

    @pytest.mark.asyncio
    async def test_loop_assigns_view_message(self):
        cog = _make_cog()
        sent = MagicMock(name="reminder_msg")
        chan = AsyncMock()
        chan.send = AsyncMock(return_value=sent)
        today_iso = PATCHED_TODAY_ISO

        await _run_loop(
            cog,
            now_in_guild_tz=datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            schedule={today_iso: {"name": "alice"}},
            channels={REMINDER_CHAN_ID: chan},
        )

        # `channel.send(msg, view=view)` — view is the second arg as kwarg.
        kwargs = chan.send.await_args.kwargs
        view = kwargs["view"]
        assert view.message is sent, (
            "ReminderView.message must be set so on_timeout can clean up the buttons"
        )


class TestReminderViewOnTimeout:
    """The 1-hour ReminderView timeout must strip the button and tell
    the assignee to use /train instead. Bug it fixes: previously
    on_timeout didn't exist, so the button looked active for an hour
    after the view stopped listening."""

    @pytest.mark.asyncio
    async def test_on_timeout_strips_button_and_posts_use_train(self):
        from train import ReminderView

        view = ReminderView(cog=MagicMock(), date_str="2026-05-15", name="alice")
        view.message = MagicMock()
        view.message.content = "🚂 Reset! Today's train is for alice."
        view.message.edit = AsyncMock()

        await view.on_timeout()

        view.message.edit.assert_awaited_once()
        kwargs = view.message.edit.await_args.kwargs
        assert kwargs["view"] is None
        assert "/train" in kwargs["content"]
        assert "timed out" in kwargs["content"].lower()
        # Original announcement preserved.
        assert "alice" in kwargs["content"]

    @pytest.mark.asyncio
    async def test_on_timeout_no_message_is_safe(self):
        """View constructed without ever being sent (e.g. test) → no raise."""
        from train import ReminderView

        view = ReminderView(cog=MagicMock(), date_str="2026-05-15", name="alice")
        # message left None
        await view.on_timeout()  # should not raise

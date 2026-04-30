"""
Audit gap #6: extra coverage for `SurveyCog.check_scheduled_reminders`
beyond what `test_survey_hub_flows.py` already covers.

The existing tests cover:
  * Daily + channel-destination happy path (fires once, idempotent
    on second tick the same minute).
  * frequency="off" → never fires.

This file adds:
  * **DM destination + Premium guild** → fires `_send_reminder_via_dm`
    and stamps reminder_last_fired.
  * **DM destination + Premium lapsed** → silently skipped (no DMs,
    no last_fired stamp so the next tick after upgrade still fires).
  * **Weekly schedule** → only fires on the configured weekday.
  * **Multi-guild loop** → one guild's failure doesn't block another.
  * **Same-day idempotency** via `reminder_last_fired` stored in DB
    (vs the in-memory case the existing test already covers).
"""

from __future__ import annotations

import asyncio
import importlib
from datetime import datetime, date as date_cls
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from tests.conftest import TEST_GUILD_ID, PREMIUM_TEST_GUILD_ID

ET = ZoneInfo("America/New_York")


# ── Premium env isolation (mirrors test_member_roster.py / test_storm_remind.py) ──

@pytest.fixture(autouse=True)
def _isolate_premium_env(monkeypatch):
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PREMIUM_BYPASS_GUILD_IDS", str(PREMIUM_TEST_GUILD_ID))
    import premium as _premium
    importlib.reload(_premium)
    _premium.clear_cache()
    yield
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM", "PREMIUM_BYPASS_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(_premium)
    _premium.clear_cache()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cog():
    """SurveyCog with the @tasks.loop cancelled before any test body runs."""
    from survey import SurveyCog
    bot = MagicMock()
    bot.add_view          = MagicMock()
    bot.add_dynamic_items = MagicMock()
    cog = SurveyCog(bot)
    try: cog.check_scheduled_reminders.cancel()
    except Exception: pass
    return cog


def _seed_setup_complete(guild_id: int):
    import config
    cfg = config.get_or_create_config(guild_id)
    cfg.timezone       = "America/New_York"
    cfg.setup_complete = True
    config.save_config(cfg)


def _matching_time_now() -> tuple[datetime, str]:
    """Return (now-in-ET, 'HH:MM' string for now). Used to seed a
    reminder whose time matches the current minute."""
    now      = datetime.now(tz=ET)
    time_str = f"{now.hour:02d}:{now.minute:02d}"
    return now, time_str


# ── DM destination + Premium ──────────────────────────────────────────────────

class TestScheduledReminderDmBranch:

    @pytest.mark.asyncio
    async def test_premium_guild_fires_dm_path(self, seeded_db):
        """Configured DM reminder + premium → `_send_reminder_via_dm`
        gets invoked exactly once, last_fired stamp updated."""
        import config
        _seed_setup_complete(PREMIUM_TEST_GUILD_ID)
        config.save_survey_config(
            PREMIUM_TEST_GUILD_ID, "Squad Powers", "Survey History", [], "intro",
        )

        _, target_time = _matching_time_now()
        config.save_survey_reminder(
            PREMIUM_TEST_GUILD_ID, "default",
            enabled=1, frequency="daily",
            day_of_week=0, time_str=target_time,
            channel_id=0, use_dm=1,
            message="DM reminder body",
        )

        cog = _make_cog()
        send_dm_spy = AsyncMock(return_value=(2, 0))  # (sent, skipped)

        with patch("survey._send_reminder_via_dm", send_dm_spy):
            await cog.check_scheduled_reminders()

        send_dm_spy.assert_awaited_once()
        # `(bot, guild_id, body)` signature.
        args = send_dm_spy.await_args.args
        assert args[1] == PREMIUM_TEST_GUILD_ID
        assert "DM reminder body" in args[2]

        # last_fired should now be today — gates the next tick.
        sched = config.list_scheduled_survey_reminders()
        entry = next(e for e in sched if e["guild_id"] == PREMIUM_TEST_GUILD_ID)
        assert entry["reminder_last_fired"] == datetime.now(tz=ET).date().isoformat()

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    async def test_premium_lapsed_silently_skips_dm_path(self, seeded_db):
        """Premium lapsed (TEST_GUILD_ID isn't in the bypass set) →
        `_send_reminder_via_dm` is NOT called and `reminder_last_fired`
        stays empty so the next tick after re-upgrade fires."""
        import config
        _seed_setup_complete(TEST_GUILD_ID)
        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History", [], "intro",
        )

        _, target_time = _matching_time_now()
        config.save_survey_reminder(
            TEST_GUILD_ID, "default",
            enabled=1, frequency="daily",
            day_of_week=0, time_str=target_time,
            channel_id=0, use_dm=1,
            message="DM body",
        )

        cog = _make_cog()
        send_dm_spy = AsyncMock(return_value=(0, 0))

        with patch("survey._send_reminder_via_dm", send_dm_spy):
            await cog.check_scheduled_reminders()

        send_dm_spy.assert_not_called()
        # Crucially: last_fired stays empty (not stamped to today),
        # so re-upgrading premium tomorrow actually re-fires.
        sched = config.list_scheduled_survey_reminders()
        entry = next(e for e in sched if e["guild_id"] == TEST_GUILD_ID)
        assert (entry["reminder_last_fired"] or "") == ""


# ── Weekly schedule ──────────────────────────────────────────────────────────

class TestScheduledReminderWeeklySchedule:

    @pytest.mark.asyncio
    async def test_weekly_only_fires_on_configured_weekday(self, seeded_db):
        """Weekly reminders include `reminder_day_of_week` (Mon=0).
        On a non-matching weekday → no fire."""
        import config
        _seed_setup_complete(PREMIUM_TEST_GUILD_ID)
        config.save_survey_config(
            PREMIUM_TEST_GUILD_ID, "Squad Powers", "Survey History", [], "intro",
        )

        # Pick a weekday that's NOT today.
        from zoneinfo import ZoneInfo
        guild_now    = datetime.now(tz=ZoneInfo("America/New_York"))
        wrong_weekday = (guild_now.weekday() + 3) % 7
        target_time   = f"{guild_now.hour:02d}:{guild_now.minute:02d}"

        config.save_survey_reminder(
            PREMIUM_TEST_GUILD_ID, "default",
            enabled=1, frequency="weekly",
            day_of_week=wrong_weekday, time_str=target_time,
            channel_id=33333, use_dm=0,
            message="weekly body",
        )

        cog    = _make_cog()
        target = AsyncMock(); target.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=target)

        await cog.check_scheduled_reminders()
        target.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_weekly_fires_when_weekday_matches(self, seeded_db):
        import config
        _seed_setup_complete(PREMIUM_TEST_GUILD_ID)
        config.save_survey_config(
            PREMIUM_TEST_GUILD_ID, "Squad Powers", "Survey History", [], "intro",
        )

        from zoneinfo import ZoneInfo
        guild_now      = datetime.now(tz=ZoneInfo("America/New_York"))
        matching_dow   = guild_now.weekday()
        target_time    = f"{guild_now.hour:02d}:{guild_now.minute:02d}"

        config.save_survey_reminder(
            PREMIUM_TEST_GUILD_ID, "default",
            enabled=1, frequency="weekly",
            day_of_week=matching_dow, time_str=target_time,
            channel_id=33334, use_dm=0,
            message="hello",
        )

        cog    = _make_cog()
        target = AsyncMock(); target.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=target)

        await cog.check_scheduled_reminders()
        target.send.assert_called_once_with("hello")


# ── Multi-guild loop isolation ───────────────────────────────────────────────

class TestScheduledReminderMultiGuildIsolation:

    @pytest.mark.asyncio
    async def test_one_guild_helper_failure_does_not_block_another(self, seeded_db):
        """Two guilds both due. Guild A's channel send raises; Guild
        B's still fires."""
        import config

        gid_a = TEST_GUILD_ID
        gid_b = TEST_GUILD_ID + 1
        for gid in (gid_a, gid_b):
            _seed_setup_complete(gid)
            config.save_survey_config(
                gid, "Squad Powers", "Survey History", [], "intro",
            )

        _, target_time = _matching_time_now()
        config.save_survey_reminder(
            gid_a, "default", enabled=1, frequency="daily",
            day_of_week=0, time_str=target_time,
            channel_id=10001, use_dm=0, message="A",
        )
        config.save_survey_reminder(
            gid_b, "default", enabled=1, frequency="daily",
            day_of_week=0, time_str=target_time,
            channel_id=10002, use_dm=0, message="B",
        )

        sent_to: list[int] = []

        async def fake_send(bot, gid, channel_id, body):
            if gid == gid_a:
                raise RuntimeError("A is down")
            sent_to.append(gid)
            return True

        cog = _make_cog()
        with patch("survey._send_reminder_to_channel", side_effect=fake_send):
            await cog.check_scheduled_reminders()

        # B made it through despite A raising.
        assert gid_b in sent_to, \
            f"Guild B's reminder must still fire after Guild A raised. sent_to={sent_to}"


# ── Persistent idempotency across ticks ──────────────────────────────────────

class TestScheduledReminderDbIdempotency:

    @pytest.mark.asyncio
    async def test_db_last_fired_stamp_blocks_second_tick_same_day(self, seeded_db):
        """Once `reminder_last_fired` is today, the next tick (even
        with the time still matching) skips. This complements the
        existing in-memory test by exercising the DB-stamped path."""
        import config
        _seed_setup_complete(PREMIUM_TEST_GUILD_ID)
        config.save_survey_config(
            PREMIUM_TEST_GUILD_ID, "Squad Powers", "Survey History", [], "intro",
        )

        from zoneinfo import ZoneInfo
        guild_now = datetime.now(tz=ZoneInfo("America/New_York"))
        target_time = f"{guild_now.hour:02d}:{guild_now.minute:02d}"

        # Pre-stamp last_fired = today.
        config.save_survey_reminder(
            PREMIUM_TEST_GUILD_ID, "default",
            enabled=1, frequency="daily",
            day_of_week=0, time_str=target_time,
            channel_id=22222, use_dm=0,
            message="should not fire",
        )
        config.update_survey_reminder_last_fired(
            PREMIUM_TEST_GUILD_ID, "default", guild_now.date().isoformat(),
        )

        cog = _make_cog()
        target = AsyncMock(); target.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=target)

        await cog.check_scheduled_reminders()
        target.send.assert_not_called()

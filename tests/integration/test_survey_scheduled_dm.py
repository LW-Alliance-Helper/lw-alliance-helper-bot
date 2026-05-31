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

# Arbitrary Wednesday at 14:30 ET — picked so weekday math (`+3 % 7`)
# trivially yields a different weekday and so the time-of-day match in
# `survey.check_scheduled_reminders` is exercised, not flaked.
FROZEN = datetime(2026, 5, 13, 14, 30, tzinfo=ET)


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


# ── Frozen clock ─────────────────────────────────────────────────────────────


@pytest.fixture
def frozen_clock():
    """Pin `survey.datetime.now(...)` to `FROZEN` for the test body.

    `check_scheduled_reminders` reads `datetime.now(tz=guild_tz)` and
    compares hour+minute to the seeded `reminder_time`. Without freezing,
    the test seeds `target_time` from one wall-clock read and the SUT
    compares against another — a minute roll between the two false-FAILs
    positive assertions and pass-by-accidents negative ones.
    """
    with patch("survey.datetime") as mock_dt:
        mock_dt.now.return_value = FROZEN
        # Preserve the `datetime(...)` constructor for any callers that
        # still build fresh datetimes through `survey.datetime`.
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        yield FROZEN


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_cog():
    """SurveyCog with the @tasks.loop cancelled before any test body runs."""
    from survey import SurveyCog

    bot = MagicMock()
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    cog = SurveyCog(bot)
    try:
        cog.check_scheduled_reminders.cancel()
    except Exception:
        pass
    return cog


def _seed_setup_complete(guild_id: int):
    import config

    cfg = config.get_or_create_config(guild_id)
    cfg.timezone = "America/New_York"
    cfg.setup_complete = True
    config.save_config(cfg)


def _hhmm(dt: datetime) -> str:
    return f"{dt.hour:02d}:{dt.minute:02d}"


# ── DM destination + Premium ──────────────────────────────────────────────────


class TestScheduledReminderDmBranch:
    @pytest.mark.asyncio
    async def test_premium_guild_fires_dm_path(self, seeded_db, frozen_clock):
        """Configured DM reminder + premium → `_send_reminder_via_dm`
        gets invoked exactly once, last_fired stamp updated."""
        import config

        _seed_setup_complete(PREMIUM_TEST_GUILD_ID)
        config.save_survey_config(
            PREMIUM_TEST_GUILD_ID,
            "Squad Powers",
            "Survey History",
            [],
            "intro",
        )

        config.save_survey_reminder(
            PREMIUM_TEST_GUILD_ID,
            "default",
            enabled=1,
            frequency="daily",
            day_of_week=0,
            time_str=_hhmm(frozen_clock),
            channel_id=0,
            use_dm=1,
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

        # last_fired should now be the frozen date — gates the next tick.
        sched = config.list_scheduled_survey_reminders()
        entry = next(e for e in sched if e["guild_id"] == PREMIUM_TEST_GUILD_ID)
        assert entry["reminder_last_fired"] == frozen_clock.date().isoformat()

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    async def test_premium_lapsed_silently_skips_dm_path(self, seeded_db, frozen_clock):
        """Premium lapsed (TEST_GUILD_ID isn't in the bypass set) →
        `_send_reminder_via_dm` is NOT called and `reminder_last_fired`
        stays empty so the next tick after re-upgrade fires."""
        import config

        _seed_setup_complete(TEST_GUILD_ID)
        config.save_survey_config(
            TEST_GUILD_ID,
            "Squad Powers",
            "Survey History",
            [],
            "intro",
        )

        config.save_survey_reminder(
            TEST_GUILD_ID,
            "default",
            enabled=1,
            frequency="daily",
            day_of_week=0,
            time_str=_hhmm(frozen_clock),
            channel_id=0,
            use_dm=1,
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
    async def test_weekly_only_fires_on_configured_weekday(self, seeded_db, frozen_clock):
        """Weekly reminders include `reminder_day_of_week` (Mon=0).
        On a non-matching weekday → no fire."""
        import config

        _seed_setup_complete(PREMIUM_TEST_GUILD_ID)
        config.save_survey_config(
            PREMIUM_TEST_GUILD_ID,
            "Squad Powers",
            "Survey History",
            [],
            "intro",
        )

        # Pick a weekday that's NOT the frozen one.
        wrong_weekday = (frozen_clock.weekday() + 3) % 7

        config.save_survey_reminder(
            PREMIUM_TEST_GUILD_ID,
            "default",
            enabled=1,
            frequency="weekly",
            day_of_week=wrong_weekday,
            time_str=_hhmm(frozen_clock),
            channel_id=33333,
            use_dm=0,
            message="weekly body",
        )

        cog = _make_cog()
        target = AsyncMock()
        target.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=target)

        await cog.check_scheduled_reminders()
        target.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_weekly_fires_when_weekday_matches(self, seeded_db, frozen_clock):
        import config

        _seed_setup_complete(PREMIUM_TEST_GUILD_ID)
        config.save_survey_config(
            PREMIUM_TEST_GUILD_ID,
            "Squad Powers",
            "Survey History",
            [],
            "intro",
        )

        config.save_survey_reminder(
            PREMIUM_TEST_GUILD_ID,
            "default",
            enabled=1,
            frequency="weekly",
            day_of_week=frozen_clock.weekday(),
            time_str=_hhmm(frozen_clock),
            channel_id=33334,
            use_dm=0,
            message="hello",
        )

        cog = _make_cog()
        target = AsyncMock()
        target.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=target)

        await cog.check_scheduled_reminders()
        target.send.assert_called_once_with("hello")


# ── Multi-guild loop isolation ───────────────────────────────────────────────


class TestScheduledReminderMultiGuildIsolation:
    @pytest.mark.asyncio
    async def test_one_guild_helper_failure_does_not_block_another(self, seeded_db, frozen_clock):
        """Two guilds both due. Guild A's channel send raises; Guild
        B's still fires."""
        import config

        gid_a = TEST_GUILD_ID
        gid_b = TEST_GUILD_ID + 1
        for gid in (gid_a, gid_b):
            _seed_setup_complete(gid)
            config.save_survey_config(
                gid,
                "Squad Powers",
                "Survey History",
                [],
                "intro",
            )

        target_time = _hhmm(frozen_clock)
        config.save_survey_reminder(
            gid_a,
            "default",
            enabled=1,
            frequency="daily",
            day_of_week=0,
            time_str=target_time,
            channel_id=10001,
            use_dm=0,
            message="A",
        )
        config.save_survey_reminder(
            gid_b,
            "default",
            enabled=1,
            frequency="daily",
            day_of_week=0,
            time_str=target_time,
            channel_id=10002,
            use_dm=0,
            message="B",
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
        assert gid_b in sent_to, (
            f"Guild B's reminder must still fire after Guild A raised. sent_to={sent_to}"
        )


# ── Persistent idempotency across ticks ──────────────────────────────────────


class TestScheduledReminderDbIdempotency:
    @pytest.mark.asyncio
    async def test_db_last_fired_stamp_blocks_second_tick_same_day(self, seeded_db, frozen_clock):
        """Once `reminder_last_fired` is today, the next tick (even
        with the time still matching) skips. This complements the
        existing in-memory test by exercising the DB-stamped path."""
        import config

        _seed_setup_complete(PREMIUM_TEST_GUILD_ID)
        config.save_survey_config(
            PREMIUM_TEST_GUILD_ID,
            "Squad Powers",
            "Survey History",
            [],
            "intro",
        )

        # Pre-stamp last_fired = today (frozen).
        config.save_survey_reminder(
            PREMIUM_TEST_GUILD_ID,
            "default",
            enabled=1,
            frequency="daily",
            day_of_week=0,
            time_str=_hhmm(frozen_clock),
            channel_id=22222,
            use_dm=0,
            message="should not fire",
        )
        config.update_survey_reminder_last_fired(
            PREMIUM_TEST_GUILD_ID,
            "default",
            frozen_clock.date().isoformat(),
        )

        cog = _make_cog()
        target = AsyncMock()
        target.send = AsyncMock()
        cog.bot.get_channel = MagicMock(return_value=target)

        await cog.check_scheduled_reminders()
        target.send.assert_not_called()

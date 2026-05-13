"""
Tests for storm_signup_scheduler.py (#131).

Covers the pure-data helpers (date math, fire-decision, row reader)
and the integration loop with a stubbed bot + post helper. The
@tasks.loop machinery itself is exercised by discord.py; we test
`_run_one_tick` directly.
"""

import asyncio
import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import storm_signup_scheduler as sss
from tests.unit.test_config import TEST_GUILD_ID


class TestNextEventDate:
    def test_today_is_event_day_returns_today(self):
        # Monday, target Monday → 0 days.
        monday = _dt.date(2026, 5, 11)
        assert sss._next_event_date(monday, 0) == monday

    def test_advances_to_upcoming_dow(self):
        # Wednesday, target Sunday → 4 days.
        wed = _dt.date(2026, 5, 13)
        assert sss._next_event_date(wed, 6) == _dt.date(2026, 5, 17)

    def test_wraps_to_next_week(self):
        # Sunday, target Monday → 1 day (next Monday).
        sun = _dt.date(2026, 5, 17)
        assert sss._next_event_date(sun, 0) == _dt.date(2026, 5, 18)


class TestParseHHMM:
    def test_valid(self):
        assert sss._parse_hhmm("14:30") == _dt.time(14, 30)
        assert sss._parse_hhmm("00:00") == _dt.time(0, 0)
        assert sss._parse_hhmm("23:59") == _dt.time(23, 59)

    def test_invalid(self):
        for bad in ("", "garbage", "25:00", "14", "14:60", "-1:00"):
            assert sss._parse_hhmm(bad) is None


class TestShouldFireNow:
    def test_fires_on_exact_match(self):
        # Today = Tuesday 2026-05-12, event = Sunday 2026-05-17 (5 days
        # out), lead = 5 days → fire today at signup_time.
        today = _dt.date(2026, 5, 12)
        now   = _dt.time(14, 0)
        should, event_date = sss._should_fire_now(
            today=today, now=now,
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is True
        assert event_date == _dt.date(2026, 5, 17)

    def test_no_fire_on_wrong_minute(self):
        today = _dt.date(2026, 5, 12)
        should, event_date = sss._should_fire_now(
            today=today, now=_dt.time(14, 1),
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is False
        # event_date still populated for diagnostics.
        assert event_date == _dt.date(2026, 5, 17)

    def test_no_fire_on_wrong_hour(self):
        today = _dt.date(2026, 5, 12)
        should, _ = sss._should_fire_now(
            today=today, now=_dt.time(13, 0),
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is False

    def test_no_fire_outside_lead_window(self):
        # 3 days before event, lead_days=5 → not yet (early).
        today = _dt.date(2026, 5, 14)  # Thursday
        should, _ = sss._should_fire_now(
            today=today, now=_dt.time(14, 0),
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is False

    def test_wraps_to_next_weeks_event_when_past_window(self):
        # Today = Saturday (1 day before event), lead = 5. Today+5 isn't
        # this week's Sunday — it's next week's Friday. The function
        # should bump the target to next week's Sunday + recompute.
        today = _dt.date(2026, 5, 16)  # Saturday
        should, event_date = sss._should_fire_now(
            today=today, now=_dt.time(14, 0),
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        # Today + 5 = May 21 (Thursday), which is 3 days BEFORE next
        # week's Sunday May 24 — so we don't fire.
        assert should is False
        assert event_date == _dt.date(2026, 5, 24)

    def test_invalid_dow_no_fire(self):
        today = _dt.date(2026, 5, 12)
        should, event_date = sss._should_fire_now(
            today=today, now=_dt.time(14, 0),
            event_dow=-1, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is False
        assert event_date is None


class TestScheduledStormRows:
    def test_only_returns_enabled_and_scheduled(self, seeded_db):
        import config
        # Storm rows must exist before save_structured_storm_config
        # can UPDATE them.
        for et in ("DS", "CS"):
            config.save_storm_config(
                TEST_GUILD_ID, et,
                tab_name=f"{et} Tab", mail_template="x",
                timezone="America/New_York", log_channel_id=0,
            )

        # DS: structured enabled + scheduled.
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            event_day_of_week=6, signup_lead_days=5, signup_time="14:00",
        )
        # CS: structured enabled but not scheduled (default dow=-1).
        config.save_structured_storm_config(
            TEST_GUILD_ID, "CS",
            structured_flow_enabled=True,
        )

        rows = sss._scheduled_storm_rows()
        events = {(r["guild_id"], r["event_type"]) for r in rows}
        assert (TEST_GUILD_ID, "DS") in events
        assert (TEST_GUILD_ID, "CS") not in events

    def test_excludes_structured_disabled(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=False,
            event_day_of_week=6, signup_lead_days=5, signup_time="14:00",
        )
        rows = sss._scheduled_storm_rows()
        # No matches because structured is off.
        assert not any(
            r["guild_id"] == TEST_GUILD_ID and r["event_type"] == "DS"
            for r in rows
        )


class TestRunOneTick:
    """Drives _run_one_tick with a fake bot + patched post_registration
    + a frozen local clock to verify the orchestration logic."""

    def _seed(self, dow: int, signup_time: str, *, enabled: bool = True):
        """Seed the test DB with a DS row that matches the test parameters."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=enabled,
            event_day_of_week=dow, signup_lead_days=5,
            signup_time=signup_time,
            signup_channel_id=99,
        )

    def _fake_bot(self):
        bot = MagicMock()
        bot.is_closed.return_value = False
        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        bot.get_guild.return_value = guild
        return bot, guild

    @pytest.mark.asyncio
    async def test_fires_on_match(self, seeded_db):
        # Wednesday 2026-05-13 14:00 ET. Event = Sunday 2026-05-17, lead 5.
        self._seed(dow=6, signup_time="14:00")
        bot, _guild = self._fake_bot()

        post_mock = AsyncMock(return_value={"status": "ok",
                                            "channel_id": 99, "message_id": 1234})
        with patch.object(sss, "_guild_today_and_now",
                          return_value=(_dt.date(2026, 5, 12), _dt.time(14, 0))), \
             patch("storm_signup_post.post_registration", post_mock):
            fired = await sss._run_one_tick(bot)
        assert fired == 1
        post_mock.assert_awaited_once()
        # The helper was called with the correct event_date.
        call_kwargs = post_mock.await_args.kwargs
        call_args   = post_mock.await_args.args
        # post_registration(bot, guild, event_type, event_date, *, structured=...)
        assert call_args[2] == "DS"
        assert call_args[3] == "2026-05-17"

    @pytest.mark.asyncio
    async def test_does_not_fire_off_minute(self, seeded_db):
        self._seed(dow=6, signup_time="14:00")
        bot, _guild = self._fake_bot()
        post_mock = AsyncMock(return_value={"status": "ok"})
        with patch.object(sss, "_guild_today_and_now",
                          return_value=(_dt.date(2026, 5, 12), _dt.time(14, 5))), \
             patch("storm_signup_post.post_registration", post_mock):
            fired = await sss._run_one_tick(bot)
        assert fired == 0
        post_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_disabled_at_fire_time(self, seeded_db):
        # Structured flow gets disabled AFTER the schedule was set but
        # BEFORE the fire time. We still pass _scheduled_storm_rows
        # because of stale reads in flight; the second check in
        # _run_one_tick should bail.
        self._seed(dow=6, signup_time="14:00", enabled=True)
        bot, _guild = self._fake_bot()
        # Now simulate a "racy" disable — re-save with enabled=False.
        import config
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=False,
            event_day_of_week=6, signup_lead_days=5, signup_time="14:00",
            signup_channel_id=99,
        )
        post_mock = AsyncMock(return_value={"status": "ok"})
        with patch.object(sss, "_guild_today_and_now",
                          return_value=(_dt.date(2026, 5, 12), _dt.time(14, 0))), \
             patch("storm_signup_post.post_registration", post_mock):
            fired = await sss._run_one_tick(bot)
        assert fired == 0
        post_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_guild_not_in_cache(self, seeded_db):
        self._seed(dow=6, signup_time="14:00")
        bot = MagicMock()
        bot.is_closed.return_value = False
        bot.get_guild.return_value = None  # guild not in cache
        post_mock = AsyncMock()
        with patch.object(sss, "_guild_today_and_now",
                          return_value=(_dt.date(2026, 5, 12), _dt.time(14, 0))), \
             patch("storm_signup_post.post_registration", post_mock):
            fired = await sss._run_one_tick(bot)
        assert fired == 0
        post_mock.assert_not_awaited()

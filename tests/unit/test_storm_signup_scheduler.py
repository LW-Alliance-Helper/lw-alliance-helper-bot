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
        # The shared parser is permissive about formats (accepts "14"
        # as 14:00, "2pm" as 14:00, etc.) so these are the genuinely
        # garbage inputs.
        for bad in ("", "garbage", "25:00", "14:60", "-1:00"):
            assert sss._parse_hhmm(bad) is None

    def test_bare_hour_accepted(self):
        # New permissive behaviour — "14" parses as 14:00 since the
        # scheduler now shares the wizard's parser via config.
        assert sss._parse_hhmm("14") == _dt.time(14, 0)


class TestShouldFireNow:
    def test_fires_on_exact_match(self):
        # Today = Tuesday 2026-05-12, event = Sunday 2026-05-17 (5 days
        # out), lead = 5 days → fire today at signup_time.
        today = _dt.date(2026, 5, 12)
        now   = _dt.time(14, 0)
        should, event_date, skipped = sss._should_fire_now(
            today=today, now=now,
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is True
        assert event_date == _dt.date(2026, 5, 17)
        assert skipped is False

    def test_no_fire_on_wrong_minute(self):
        today = _dt.date(2026, 5, 12)
        should, event_date, _ = sss._should_fire_now(
            today=today, now=_dt.time(14, 1),
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is False
        # event_date still populated for diagnostics.
        assert event_date == _dt.date(2026, 5, 17)

    def test_no_fire_on_wrong_hour(self):
        today = _dt.date(2026, 5, 12)
        should, _, _ = sss._should_fire_now(
            today=today, now=_dt.time(13, 0),
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is False

    def test_no_fire_outside_lead_window(self):
        # 3 days before event, lead_days=5 → not yet (early).
        today = _dt.date(2026, 5, 14)  # Thursday
        should, _, _ = sss._should_fire_now(
            today=today, now=_dt.time(14, 0),
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is False

    def test_wraps_to_next_weeks_event_when_past_window(self):
        # Today = Saturday (1 day before event), lead = 5. Today+5 isn't
        # this week's Sunday — it's next week's Friday. The function
        # should bump the target to next week's Sunday and surface
        # week_skipped=True so the caller can warn leadership.
        today = _dt.date(2026, 5, 16)  # Saturday
        should, event_date, skipped = sss._should_fire_now(
            today=today, now=_dt.time(14, 0),
            event_dow=6, lead_days=5, signup_time=_dt.time(14, 0),
        )
        # Today + 5 = May 21 (Thursday), which is 3 days BEFORE next
        # week's Sunday May 24 — so we don't fire.
        assert should is False
        assert event_date == _dt.date(2026, 5, 24)
        # Critical bit: this signals "this week was skipped" so the
        # tick can log a one-shot warning.
        assert skipped is True

    def test_invalid_dow_no_fire(self):
        today = _dt.date(2026, 5, 12)
        should, event_date, skipped = sss._should_fire_now(
            today=today, now=_dt.time(14, 0),
            event_dow=-1, lead_days=5, signup_time=_dt.time(14, 0),
        )
        assert should is False
        assert event_date is None
        assert skipped is False


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
        # Tuesday 2026-05-12 14:00 ET. Event = Sunday 2026-05-17, lead 5.
        self._seed(dow=6, signup_time="14:00")
        bot, _guild = self._fake_bot()

        post_mock = AsyncMock(return_value={"status": "ok",
                                            "channel_id": 99, "message_id": 1234})
        with patch.object(sss, "_guild_today_and_now",
                          return_value=(_dt.date(2026, 5, 12), _dt.time(14, 0))), \
             patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("storm_signup_post.post_registration", post_mock):
            fired = await sss._run_one_tick(bot)
        assert fired == 1
        post_mock.assert_awaited_once()
        # The helper was called with the correct event_date.
        call_args   = post_mock.await_args.args
        # post_registration(bot, guild, event_type, event_date, *, structured=...)
        assert call_args[2] == "DS"
        assert call_args[3] == "2026-05-17"

    @pytest.mark.asyncio
    async def test_skips_when_guild_not_premium(self, seeded_db):
        """A guild whose Premium lapsed (but whose structured_flow_enabled
        row is still on disk) must not get auto-posted. Defense-in-depth
        check at fire time."""
        self._seed(dow=6, signup_time="14:00")
        bot, _guild = self._fake_bot()
        post_mock = AsyncMock(return_value={"status": "ok"})
        with patch.object(sss, "_guild_today_and_now",
                          return_value=(_dt.date(2026, 5, 12), _dt.time(14, 0))), \
             patch("premium.is_premium", new=AsyncMock(return_value=False)), \
             patch("storm_signup_post.post_registration", post_mock):
            fired = await sss._run_one_tick(bot)
        assert fired == 0
        post_mock.assert_not_awaited()

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


class TestSkippedWeekWarning:
    """When `lead_days` is larger than days-remaining to this week's
    event, the scheduler bumps to next week's event — but leadership
    expected a fire this week, so we log a one-shot warning."""

    @pytest.mark.asyncio
    async def test_warning_logged_on_first_skip_then_silent(self, seeded_db, caplog):
        import config
        import logging
        # Today = Saturday May 16 2026 (one day before Sunday event),
        # lead = 5 days → the THIS-week fire window (May 11) is gone.
        # Scheduler targets next week's Sunday May 24; should log once.
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            event_day_of_week=6, signup_lead_days=5, signup_time="14:00",
            signup_channel_id=99,
        )
        # Clear any prior skip-memo from earlier tests.
        sss._skip_warned.clear()

        bot = MagicMock()
        bot.is_closed.return_value = False
        guild = MagicMock(); guild.id = TEST_GUILD_ID
        bot.get_guild.return_value = guild

        post_mock = AsyncMock(return_value={"status": "ok"})
        with patch.object(sss, "_guild_today_and_now",
                          return_value=(_dt.date(2026, 5, 16), _dt.time(14, 0))), \
             patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("storm_signup_post.post_registration", post_mock):
            with caplog.at_level(logging.WARNING, logger="storm_signup_scheduler"):
                await sss._run_one_tick(bot)
                first_warnings = [r for r in caplog.records
                                  if "window for this week" in r.getMessage()]
                # Second tick of the same minute — must NOT re-log.
                caplog.clear()
                await sss._run_one_tick(bot)
                second_warnings = [r for r in caplog.records
                                   if "window for this week" in r.getMessage()]

        assert len(first_warnings) == 1
        assert second_warnings == []
        # And it never auto-posted (the skipped event's window is gone).
        post_mock.assert_not_awaited()


class TestSaveStructuredClampsLeadDays:
    """Defense-in-depth — the wizard clamps lead_days >14 already, but
    a malformed direct call to save_structured_storm_config used to be
    able to smuggle in lead=50, which would cause the scheduler to skip
    forever (fire date never matches today)."""

    def test_lead_above_14_clamped_at_storage(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            event_day_of_week=6, signup_lead_days=50, signup_time="14:00",
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["signup_lead_days"] == 14

    def test_negative_lead_clamped_to_zero(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            event_day_of_week=6, signup_lead_days=-3, signup_time="14:00",
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["signup_lead_days"] == 0


class TestParseStormSignupTime:
    """Canonical HHMM parser in config.py — shared by wizard + scheduler."""

    def test_accepts_24h_format(self):
        from config import parse_storm_signup_time
        assert parse_storm_signup_time("14:00") == "14:00"
        assert parse_storm_signup_time("23:59") == "23:59"

    def test_accepts_pm_format(self):
        from config import parse_storm_signup_time
        assert parse_storm_signup_time("2pm") == "14:00"
        assert parse_storm_signup_time("2:30pm") == "14:30"
        assert parse_storm_signup_time("2:30 PM") == "14:30"

    def test_accepts_bare_hour(self):
        from config import parse_storm_signup_time
        assert parse_storm_signup_time("14") == "14:00"

    def test_midnight_am(self):
        from config import parse_storm_signup_time
        assert parse_storm_signup_time("12:00am") == "00:00"

    def test_noon_pm(self):
        from config import parse_storm_signup_time
        assert parse_storm_signup_time("12:00pm") == "12:00"

    def test_empty_returns_none(self):
        from config import parse_storm_signup_time
        assert parse_storm_signup_time("") is None
        assert parse_storm_signup_time("   ") is None
        assert parse_storm_signup_time(None) is None

    def test_garbage_returns_none(self):
        from config import parse_storm_signup_time
        for bad in ("garbage", "25:00", "14:60", "-1:00"):
            assert parse_storm_signup_time(bad) is None

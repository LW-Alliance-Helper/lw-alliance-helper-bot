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
    """Post-#164: simple match. Fires when today's weekday == poll_dow
    AND now's hour:minute == signup_time. Event date no longer derives
    from a lead-days computation — `storm_date_helpers.next_event_date`
    owns event-day inference (game-defined: DS=Friday, CS=Thursday)."""

    def test_fires_on_exact_match(self):
        # Today = Tuesday 2026-05-12; poll-day = Tuesday (1). 14:00 ET.
        today = _dt.date(2026, 5, 12)
        now   = _dt.time(14, 0)
        assert sss._should_fire_now(
            today=today, now=now,
            poll_dow=1, signup_time=_dt.time(14, 0),
        ) is True

    def test_no_fire_on_wrong_minute(self):
        today = _dt.date(2026, 5, 12)
        assert sss._should_fire_now(
            today=today, now=_dt.time(14, 1),
            poll_dow=1, signup_time=_dt.time(14, 0),
        ) is False

    def test_no_fire_on_wrong_hour(self):
        today = _dt.date(2026, 5, 12)
        assert sss._should_fire_now(
            today=today, now=_dt.time(13, 0),
            poll_dow=1, signup_time=_dt.time(14, 0),
        ) is False

    def test_no_fire_on_wrong_weekday(self):
        # Today = Tuesday; poll-day = Wednesday → don't fire.
        today = _dt.date(2026, 5, 12)
        assert sss._should_fire_now(
            today=today, now=_dt.time(14, 0),
            poll_dow=2, signup_time=_dt.time(14, 0),
        ) is False

    def test_invalid_dow_no_fire(self):
        today = _dt.date(2026, 5, 12)
        assert sss._should_fire_now(
            today=today, now=_dt.time(14, 0),
            poll_dow=-1, signup_time=_dt.time(14, 0),
        ) is False


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
            poll_day_of_week=1, signup_time="14:00",
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
            poll_day_of_week=1, signup_time="14:00",
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

    def _seed(self, poll_dow: int, signup_time: str, *, enabled: bool = True):
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
            poll_day_of_week=poll_dow,
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
        # Tuesday 2026-05-12 14:00 ET. Poll day = Tuesday (1).
        # DS event = Friday by Rule H, so next event date = Fri May 15.
        self._seed(poll_dow=1, signup_time="14:00")
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
        call_args   = post_mock.await_args.args
        # post_registration(bot, guild, event_type, event_date, *, structured=...)
        assert call_args[2] == "DS"
        assert call_args[3] == "2026-05-15"  # next Friday after Tue 5/12

    @pytest.mark.asyncio
    async def test_skips_when_guild_not_premium(self, seeded_db):
        """A guild whose Premium lapsed (but whose structured_flow_enabled
        row is still on disk) must not get auto-posted. Defense-in-depth
        check at fire time."""
        self._seed(poll_dow=1, signup_time="14:00")
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
        self._seed(poll_dow=1, signup_time="14:00")
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
        self._seed(poll_dow=1, signup_time="14:00", enabled=True)
        bot, _guild = self._fake_bot()
        # Now simulate a "racy" disable — re-save with enabled=False.
        import config
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=False,
            poll_day_of_week=1, signup_time="14:00",
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
        self._seed(poll_dow=1, signup_time="14:00")
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

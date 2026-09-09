"""
Tests for `time_helpers` — the single home for "what day is it, and in
whose calendar?".

The bugs this module exists to prevent all look the same in local
testing, because in a US timezone during business hours every calendar
agrees. These tests pin specific instants where they *disagree*, which is
the only way the distinction is visible at all.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import time_helpers
from time_helpers import (
    SERVER_TZ,
    local_today,
    next_clock_time,
    server_date_for,
    server_today,
)


# ── The server (in-game) day ────────────────────────────────────────────────


class TestServerDate:
    """Last War runs on UTC-2, so the in-game midnight lands at 02:00 UTC.
    For the ~2 hours after UTC midnight the in-game day has NOT rolled over
    yet, and the bare container clock disagrees with the game."""

    def test_offset_is_utc_minus_two(self):
        assert SERVER_TZ.utcoffset(None) == timedelta(hours=-2)

    def test_lags_utc_in_the_gap_after_utc_midnight(self):
        """00:30 UTC on Sept 6 is still 22:30 Sept 5 in-game."""
        moment = datetime(2026, 9, 6, 0, 30, tzinfo=timezone.utc)
        assert server_date_for(moment) == date(2026, 9, 5)

    def test_rolls_over_at_02_00_utc(self):
        assert server_date_for(datetime(2026, 9, 6, 1, 59, tzinfo=timezone.utc)) == date(2026, 9, 5)
        assert server_date_for(datetime(2026, 9, 6, 2, 0, tzinfo=timezone.utc)) == date(2026, 9, 6)

    def test_agrees_with_utc_outside_the_gap(self):
        """Guards against the offset being applied in the wrong direction,
        which would show up as a permanent one-day skew rather than a
        two-hour window."""
        moment = datetime(2026, 9, 6, 15, 0, tzinfo=timezone.utc)
        assert server_date_for(moment) == date(2026, 9, 6)

    def test_an_eastern_evening_is_already_the_next_in_game_day(self):
        """The case the train/shiny "day behind" bugs (#318 / #330) came
        from: 11pm Eastern is past 02:00 UTC, so in-game it is tomorrow."""
        edt = timezone(timedelta(hours=-4))
        assert server_date_for(datetime(2026, 9, 4, 23, 7, tzinfo=edt)) == date(2026, 9, 5)

    def test_server_today_reads_the_clock_through_server_date_for(self):
        moment = datetime(2026, 9, 6, 0, 30, tzinfo=timezone.utc)
        with patch.object(time_helpers, "datetime") as mock_dt:
            mock_dt.now.return_value = moment
            assert server_today() == date(2026, 9, 5)


class TestServerDateFromEasternClock:
    """The same boundary read off an Eastern clock, which is how leadership
    and the reminder defaults actually experience it. (Moved here from
    tests/unit/test_config.py when `server_date_for` moved to this module.)"""

    ET = ZoneInfo("America/New_York")

    def test_evening_local_is_next_in_game_day(self):
        # 10pm ET on a summer (EDT) Tuesday == 00:00 server time the next day.
        dt = datetime(2026, 6, 2, 22, 0, tzinfo=self.ET)
        assert server_date_for(dt).isoformat() == "2026-06-03"

    def test_daytime_local_is_same_in_game_day(self):
        # A mid-day local time is well before the reset → same calendar date.
        dt = datetime(2026, 6, 2, 9, 0, tzinfo=self.ET)
        assert server_date_for(dt).isoformat() == "2026-06-02"

    def test_just_before_reset_stays_today(self):
        # The reset lands at 10pm EDT (00:00 server time). 9:59pm EDT is one
        # minute before it == 23:59 server time → still the same in-game date.
        dt = datetime(2026, 6, 2, 21, 59, tzinfo=self.ET)
        assert server_date_for(dt).isoformat() == "2026-06-02"


# ── The guild-local day (the documented exception) ──────────────────────────


class TestLocalToday:
    """For human-calendar data only — a birthday's bare month/day carries
    no timezone, so it must be compared against the humans' own calendar,
    not the game's."""

    def test_uses_the_supplied_timezone(self):
        """08:00 in Tokyo on Oct 12 is still Oct 11 in UTC and Oct 11
        in-game — the exact skew that fired birthday announcements a day
        late for alliances far from UTC-2."""
        tokyo = ZoneInfo("Asia/Tokyo")
        moment = datetime(2026, 10, 12, 8, 0, tzinfo=tokyo)
        with patch.object(time_helpers, "datetime") as mock_dt:
            mock_dt.now.return_value = moment
            assert local_today(tokyo) == date(2026, 10, 12)
        assert server_date_for(moment) == date(2026, 10, 11)

    def test_defaults_to_et(self):
        moment = datetime(2026, 10, 12, 8, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch.object(time_helpers, "datetime") as mock_dt:
            mock_dt.now.return_value = moment
            assert local_today() == date(2026, 10, 12)


# ── Resolving a typed clock time ────────────────────────────────────────────


class TestNextClockTime:
    """A leader typing a time means "the next time the clock reads this".
    Building it from a separately-tracked date instead is what let an
    edited time land days away from the day it was typed for."""

    ET = ZoneInfo("America/New_York")

    def _at(self, moment, *args, **kwargs):
        with patch.object(time_helpers, "datetime") as mock_dt:
            mock_dt.now.return_value = moment
            return next_clock_time(*args, **kwargs)

    def test_time_later_today_stays_today(self):
        now = datetime(2026, 5, 8, 10, 0, tzinfo=self.ET)
        dt = self._at(now, 17, 0, tz=self.ET)
        assert dt.date() == date(2026, 5, 8)
        assert (dt.hour, dt.minute) == (17, 0)

    def test_time_already_passed_rolls_to_tomorrow(self):
        now = datetime(2026, 5, 8, 23, 0, tzinfo=self.ET)
        dt = self._at(now, 17, 0, tz=self.ET)
        assert dt.date() == date(2026, 5, 9)
        assert dt.hour == 17

    def test_exactly_now_rolls_forward_rather_than_firing_instantly(self):
        now = datetime(2026, 5, 8, 17, 0, tzinfo=self.ET)
        assert self._at(now, 17, 0, tz=self.ET).date() == date(2026, 5, 9)

    def test_explicit_tz_is_preserved_not_converted(self):
        seoul = ZoneInfo("Asia/Seoul")
        now = datetime(2026, 5, 8, 10, 0, tzinfo=seoul)
        dt = self._at(now, 17, 0, tz=seoul)
        assert dt.tzinfo == seoul
        assert dt.hour == 17  # local hour, not converted

    def test_defaults_to_et(self):
        now = datetime(2026, 5, 8, 10, 0, tzinfo=self.ET)
        assert self._at(now, 17, 0).tzinfo == self.ET

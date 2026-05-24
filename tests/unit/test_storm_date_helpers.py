"""
Tests for storm_date_helpers (#145 / #146).

Display formatters, permissive parser, and per-command inference.
"""

import datetime as _dt
import sqlite3
from unittest.mock import patch

import pytest

import storm_date_helpers as sdh


# ── format_event_date / format_event_date_compact ───────────────────────────


class TestFormatEventDate:
    def test_long_form_includes_weekday_month_day_year(self):
        rendered = sdh.format_event_date("2026-05-17")
        # 2026-05-17 is a Sunday.
        assert "Sunday" in rendered
        assert "May" in rendered
        assert "17" in rendered
        assert "2026" in rendered

    def test_long_form_strips_leading_zero_on_day(self):
        rendered = sdh.format_event_date("2026-05-08")
        # ", 08, " would mean we kept the strftime zero-padded form.
        assert "8" in rendered
        assert ", 08," not in rendered

    def test_falls_back_to_raw_when_unparseable(self):
        assert sdh.format_event_date("not-a-date") == "not-a-date"

    def test_handles_empty_input(self):
        assert sdh.format_event_date("") == ""
        assert sdh.format_event_date(None) == ""

    def test_compact_form_has_short_weekday_no_year(self):
        rendered = sdh.format_event_date_compact("2026-05-17")
        assert "Sun" in rendered
        assert "May" in rendered
        assert "17" in rendered
        assert "2026" not in rendered

    def test_compact_falls_back_to_raw(self):
        assert sdh.format_event_date_compact("garbage") == "garbage"


# ── parse_event_date ────────────────────────────────────────────────────────


class TestParseEventDate:
    """Today is pinned to 2026-05-13 (a Wednesday) for every case so
    inference is deterministic."""

    TODAY = _dt.date(2026, 5, 13)

    def _parse(self, raw):
        return sdh.parse_event_date(raw, today=self.TODAY)

    # ISO

    def test_iso_dash(self):
        assert self._parse("2026-05-18") == _dt.date(2026, 5, 18)

    def test_iso_slash(self):
        assert self._parse("2026/05/18") == _dt.date(2026, 5, 18)

    def test_iso_dot(self):
        assert self._parse("2026.05.18") == _dt.date(2026, 5, 18)

    # US slash/dash

    def test_us_slash_with_year(self):
        assert self._parse("5/18/2026") == _dt.date(2026, 5, 18)

    def test_us_dash_with_year(self):
        assert self._parse("5-18-2026") == _dt.date(2026, 5, 18)

    def test_us_slash_no_year_future_in_this_year(self):
        # 5/18 with today = 5/13 -> stays in 2026
        assert self._parse("5/18") == _dt.date(2026, 5, 18)

    def test_us_slash_no_year_past_rolls_to_next(self):
        # 1/5 with today = 5/13 -> Jan 5 has already passed this year
        assert self._parse("1/5") == _dt.date(2027, 1, 5)

    # Month names

    def test_long_month_name_with_year(self):
        assert self._parse("May 18, 2026") == _dt.date(2026, 5, 18)

    def test_long_month_name_no_year(self):
        assert self._parse("May 18") == _dt.date(2026, 5, 18)

    def test_short_month_name_no_year(self):
        assert self._parse("May 18") == _dt.date(2026, 5, 18)
        assert self._parse("may 18") == _dt.date(2026, 5, 18)

    def test_day_first_month_name(self):
        assert self._parse("18 May") == _dt.date(2026, 5, 18)
        assert self._parse("18 May 2026") == _dt.date(2026, 5, 18)

    def test_abbreviated_month_name(self):
        assert self._parse("May 18") == _dt.date(2026, 5, 18)
        assert self._parse("Jan 5") == _dt.date(2027, 1, 5)

    def test_ordinal_suffix_tolerant(self):
        assert self._parse("May 18th") == _dt.date(2026, 5, 18)
        assert self._parse("18th May") == _dt.date(2026, 5, 18)
        assert self._parse("1st May") == _dt.date(2027, 5, 1)

    # Keyword tokens

    def test_today(self):
        assert self._parse("today") == self.TODAY
        assert self._parse("TODAY") == self.TODAY

    def test_tomorrow(self):
        assert self._parse("tomorrow") == self.TODAY + _dt.timedelta(days=1)

    def test_tmrw(self):
        assert self._parse("tmrw") == self.TODAY + _dt.timedelta(days=1)

    def test_yesterday(self):
        assert self._parse("yesterday") == self.TODAY - _dt.timedelta(days=1)

    # Weekday tokens

    def test_weekday_name_advances_to_next_occurrence(self):
        # Today (Wed) -> sunday is 4 days out
        assert self._parse("Sunday") == _dt.date(2026, 5, 17)

    def test_weekday_today_rolls_a_full_week(self):
        # Today is Wednesday; "Wednesday" means next week, not today
        assert self._parse("Wednesday") == self.TODAY + _dt.timedelta(days=7)

    def test_next_prefix_works(self):
        assert self._parse("next sunday") == _dt.date(2026, 5, 17)
        # "next wednesday" while today is Wednesday still means next week
        assert self._parse("next wednesday") == self.TODAY + _dt.timedelta(days=7)

    def test_this_weekday_does_not_roll(self):
        # "this sunday" while today is Wed -> this week's Sunday
        assert self._parse("this sunday") == _dt.date(2026, 5, 17)

    def test_short_weekday(self):
        assert self._parse("sun") == _dt.date(2026, 5, 17)

    # Whitespace and punctuation tolerance

    def test_strips_trailing_punctuation(self):
        assert self._parse("May 18,") == _dt.date(2026, 5, 18)
        assert self._parse("2026-05-18.") == _dt.date(2026, 5, 18)

    def test_collapses_internal_whitespace(self):
        assert self._parse("May   18,   2026") == _dt.date(2026, 5, 18)

    # Failure cases

    def test_empty_returns_none(self):
        assert self._parse("") is None
        assert self._parse(None) is None
        assert self._parse("   ") is None

    def test_garbage_returns_none(self):
        assert self._parse("not a date") is None
        assert self._parse("yesterday-ish") is None

    def test_impossible_date_returns_none(self):
        assert self._parse("Feb 30") is None
        assert self._parse("13/45/2026") is None


class TestParseEventDateIso:
    def test_returns_iso_string(self):
        today = _dt.date(2026, 5, 13)
        assert sdh.parse_event_date_iso("May 18", today=today) == "2026-05-18"

    def test_none_when_unparseable(self):
        assert sdh.parse_event_date_iso("garbage") is None
        assert sdh.parse_event_date_iso("") is None


# ── next_event_date ─────────────────────────────────────────────────────────


class TestNextEventDate:
    """Post-Rule H (#164): event day is game-defined. DS = Friday (4),
    CS = Thursday (3). `guild_id` is accepted for signature stability
    but ignored. No config lookup happens."""

    TODAY = _dt.date(2026, 5, 13)  # Wednesday

    def test_ds_returns_next_friday(self):
        iso = sdh.next_event_date(123, "DS", today=self.TODAY)
        # Next Friday after Wednesday is 2 days later
        assert iso == "2026-05-15"

    def test_cs_returns_next_thursday(self):
        iso = sdh.next_event_date(123, "CS", today=self.TODAY)
        # Next Thursday after Wednesday is 1 day later
        assert iso == "2026-05-14"

    def test_ds_today_is_friday_rolls_to_next_week(self):
        # Friday 2026-05-15 -> next Friday is 2026-05-22 (same_day_rolls=True)
        friday = _dt.date(2026, 5, 15)
        iso = sdh.next_event_date(123, "DS", today=friday)
        assert iso == "2026-05-22"

    def test_cs_today_is_thursday_rolls_to_next_week(self):
        thursday = _dt.date(2026, 5, 14)
        iso = sdh.next_event_date(123, "CS", today=thursday)
        assert iso == "2026-05-21"

    def test_unknown_event_type_falls_back_to_sunday(self):
        # Defensive: anything other than DS/CS uses Sunday (6).
        iso = sdh.next_event_date(123, "XX", today=self.TODAY)
        # Sunday after Wednesday is 4 days later
        assert iso == "2026-05-17"


# ── most_recent_event_date ──────────────────────────────────────────────────


class TestMostRecentEventDate:
    TODAY = _dt.date(2026, 5, 13)

    def test_returns_most_recent_past_event(self, seeded_db):
        # Seed two registration posts: one past, one future.
        import config
        config.record_storm_registration_post(
            777, "DS", "2026-05-10",
            channel_id=1, message_id=10,
        )
        config.record_storm_registration_post(
            777, "DS", "2026-05-20",
            channel_id=1, message_id=20,
        )
        # Different event type — should be ignored.
        config.record_storm_registration_post(
            777, "CS", "2026-05-12",
            channel_id=1, message_id=30,
        )
        iso = sdh.most_recent_event_date(777, "DS", today=self.TODAY)
        assert iso == "2026-05-10"

    def test_returns_none_when_no_posts(self, seeded_db):
        iso = sdh.most_recent_event_date(777, "DS", today=self.TODAY)
        assert iso is None

    def test_today_inclusive(self, seeded_db):
        import config
        config.record_storm_registration_post(
            777, "DS", self.TODAY.isoformat(),
            channel_id=1, message_id=99,
        )
        iso = sdh.most_recent_event_date(777, "DS", today=self.TODAY)
        assert iso == self.TODAY.isoformat()

    def test_returns_none_when_only_future_posts(self, seeded_db):
        import config
        config.record_storm_registration_post(
            777, "DS", "2026-06-01",
            channel_id=1, message_id=50,
        )
        iso = sdh.most_recent_event_date(777, "DS", today=self.TODAY)
        assert iso is None

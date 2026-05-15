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
    TODAY = _dt.date(2026, 5, 13)  # Wednesday

    def test_uses_configured_event_day_of_week(self):
        # 5 = Saturday
        with patch("config.get_structured_storm_config") as m:
            m.return_value = {"event_day_of_week": 5}
            iso = sdh.next_event_date(123, "DS", today=self.TODAY)
        # Next Saturday after a Wednesday is 3 days later
        assert iso == "2026-05-16"

    def test_falls_back_to_sunday_when_unconfigured(self):
        with patch("config.get_structured_storm_config") as m:
            m.return_value = {"event_day_of_week": -1}
            iso = sdh.next_event_date(123, "DS", today=self.TODAY)
        # Sunday is 4 days after Wednesday
        assert iso == "2026-05-17"

    def test_falls_back_to_sunday_when_config_missing_key(self):
        with patch("config.get_structured_storm_config") as m:
            m.return_value = {}
            iso = sdh.next_event_date(123, "DS", today=self.TODAY)
        assert iso == "2026-05-17"

    def test_falls_back_to_sunday_when_config_raises(self):
        with patch("config.get_structured_storm_config", side_effect=RuntimeError("db down")):
            iso = sdh.next_event_date(123, "DS", today=self.TODAY)
        assert iso == "2026-05-17"

    def test_today_is_event_day_rolls_to_next_week(self):
        # event_day = 2 (Wednesday) and today is Wednesday
        with patch("config.get_structured_storm_config") as m:
            m.return_value = {"event_day_of_week": 2}
            iso = sdh.next_event_date(123, "DS", today=self.TODAY)
        # +7 days -> 2026-05-20
        assert iso == "2026-05-20"


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

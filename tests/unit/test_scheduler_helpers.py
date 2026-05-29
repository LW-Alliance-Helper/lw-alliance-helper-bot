"""
Tests for the standalone helpers in scheduler.py — `next_event_dates`
(repeating-event date arithmetic) and `is_friday`.

These are the pieces that survived the OGV-strip refactor: the public
``next_event_dates`` no longer reads global config, it just computes
the next N occurrences from an explicit (anchor, cycle) pair. The
``/events`` slash command in bot.py and the scheduler's main loop
both call it with values pulled from per-guild ``guild_events`` rows.
"""

from __future__ import annotations

from datetime import date

import pytest

import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scheduler import next_event_dates, is_friday


class TestNextEventDates:
    """Exercises the cycle math. Anchor is when the event series began
    (or any past/future occurrence); cycle is the day-interval; from_date
    is the date we're computing forward from."""

    def test_from_date_on_event_day_returns_that_date_first(self):
        # Anchor = 2026-03-30, cycle = 3 → fires on 03-30, 04-02, 04-05, ...
        results = next_event_dates(
            from_date=date(2026, 4, 5),
            count=1,
            anchor=date(2026, 3, 30),
            cycle=3,
        )
        assert results == [date(2026, 4, 5)]

    def test_from_date_between_events_skips_to_next_cycle_day(self):
        # 04-03 is one day after a 3-day cycle hit on 04-02 → next is 04-05
        results = next_event_dates(
            from_date=date(2026, 4, 3),
            count=1,
            anchor=date(2026, 3, 30),
            cycle=3,
        )
        assert results == [date(2026, 4, 5)]

    def test_returns_count_consecutive_cycle_dates(self):
        results = next_event_dates(
            from_date=date(2026, 3, 30),
            count=4,
            anchor=date(2026, 3, 30),
            cycle=3,
        )
        assert results == [
            date(2026, 3, 30),
            date(2026, 4, 2),
            date(2026, 4, 5),
            date(2026, 4, 8),
        ]

    def test_from_date_before_anchor_still_lands_on_cycle(self):
        # Anchor in the future. The cycle extends both directions from
        # anchor — with anchor = 2026-03-30 and cycle = 3, the cycle hits
        # 03-21, 03-24, 03-27, 03-30, 04-02, ... From 03-25, the next
        # cycle-aligned date is 03-27 (two days forward).
        results = next_event_dates(
            from_date=date(2026, 3, 25),
            count=1,
            anchor=date(2026, 3, 30),
            cycle=3,
        )
        assert results == [date(2026, 3, 27)]

    def test_weekly_cycle(self):
        # cycle = 7 = weekly
        results = next_event_dates(
            from_date=date(2026, 4, 1),  # Wednesday
            count=3,
            anchor=date(2026, 3, 30),  # Monday
            cycle=7,
        )
        assert results == [
            date(2026, 4, 6),  # next Monday
            date(2026, 4, 13),
            date(2026, 4, 20),
        ]

    def test_count_one_returns_single_date(self):
        results = next_event_dates(
            from_date=date(2026, 5, 1),
            count=1,
            anchor=date(2026, 3, 30),
            cycle=3,
        )
        assert len(results) == 1

    def test_count_zero_returns_empty(self):
        results = next_event_dates(
            from_date=date(2026, 5, 1),
            count=0,
            anchor=date(2026, 3, 30),
            cycle=3,
        )
        assert results == []


class TestIsFriday:
    """Trivial day-of-week check, sanity-tested only because the scheduler's
    Friday-shield path uses it."""

    def test_friday(self):
        assert is_friday(date(2026, 4, 3)) is True  # 2026-04-03 is a Friday

    def test_not_friday(self):
        assert is_friday(date(2026, 4, 2)) is False  # Thursday
        assert is_friday(date(2026, 4, 4)) is False  # Saturday


# ── parse_time_str ────────────────────────────────────────────────────────────


class TestParseTimeStr:
    """The /events editor's Add Event handler feeds raw user input through
    `parse_time_str` to derive (hour, minute) for the new event's
    datetime. If parsing returns None the editor posts a "Could not
    parse that time" error and bails — so any regression here breaks
    leadership's ability to add events at non-default times."""

    # ── 12-hour format ────────────────────────────────────────────────────────

    def test_pm_basic(self):
        from scheduler import parse_time_str

        assert parse_time_str("10:15pm") == (22, 15)

    def test_pm_no_minutes(self):
        from scheduler import parse_time_str

        assert parse_time_str("5pm") == (17, 0)

    def test_am_basic(self):
        from scheduler import parse_time_str

        assert parse_time_str("9:00am") == (9, 0)

    def test_am_no_minutes(self):
        from scheduler import parse_time_str

        assert parse_time_str("8am") == (8, 0)

    def test_uppercase_period_accepted(self):
        from scheduler import parse_time_str

        assert parse_time_str("10:15PM") == (22, 15)

    def test_mixed_case_period_accepted(self):
        from scheduler import parse_time_str

        assert parse_time_str("9:00 Am") == (9, 0)

    def test_whitespace_between_time_and_period(self):
        from scheduler import parse_time_str

        assert parse_time_str("9:00 AM") == (9, 0)
        assert parse_time_str("9:00   am") == (9, 0)

    # ── 12am / 12pm boundary cases ────────────────────────────────────────────
    # These are the easiest to get wrong — and the ones leadership trips
    # on most often when entering literal "noon" or "midnight".

    def test_12am_is_midnight(self):
        from scheduler import parse_time_str

        assert parse_time_str("12am") == (0, 0)

    def test_12_30am_is_just_past_midnight(self):
        from scheduler import parse_time_str

        assert parse_time_str("12:30am") == (0, 30)

    def test_12pm_is_noon(self):
        from scheduler import parse_time_str

        assert parse_time_str("12pm") == (12, 0)

    def test_12_30pm_is_just_past_noon(self):
        from scheduler import parse_time_str

        assert parse_time_str("12:30pm") == (12, 30)

    # ── 24-hour format ────────────────────────────────────────────────────────

    def test_24h_evening(self):
        from scheduler import parse_time_str

        assert parse_time_str("17:00") == (17, 0)

    def test_24h_midnight(self):
        from scheduler import parse_time_str

        assert parse_time_str("00:00") == (0, 0)

    def test_24h_noon(self):
        from scheduler import parse_time_str

        assert parse_time_str("12:00") == (12, 0)

    def test_24h_late_night(self):
        from scheduler import parse_time_str

        assert parse_time_str("23:45") == (23, 45)

    def test_24h_minutes_preserved(self):
        from scheduler import parse_time_str

        assert parse_time_str("9:30") == (9, 30)

    # ── 12-hour wins when both formats look plausible ────────────────────────

    def test_12h_format_takes_precedence_over_24h_when_period_present(self):
        """`10:30pm` should parse as 22:30, not as bare 10:30 (which
        would be ambiguous). The regex tries 12h first."""
        from scheduler import parse_time_str

        assert parse_time_str("10:30pm") == (22, 30)

    # ── Garbage input ─────────────────────────────────────────────────────────

    def test_empty_string_returns_none(self):
        from scheduler import parse_time_str

        assert parse_time_str("") is None

    def test_garbage_returns_none(self):
        from scheduler import parse_time_str

        assert parse_time_str("not a time") is None

    def test_letters_only_returns_none(self):
        from scheduler import parse_time_str

        assert parse_time_str("evening") is None

    def test_lone_number_without_format_returns_none(self):
        """A bare `5` isn't enough — parser needs at least am/pm or HH:MM."""
        from scheduler import parse_time_str

        assert parse_time_str("5") is None

    # ── Surrounding-text tolerance ────────────────────────────────────────────
    # parse_time_str uses re.search (not match), so it'll find times
    # embedded in longer strings. Document this behavior so callers know
    # what they're getting.

    def test_finds_time_embedded_in_sentence(self):
        from scheduler import parse_time_str

        assert parse_time_str("event at 10:15pm please") == (22, 15)


# ── build_announcement: blurb resolution ─────────────────────────────────────


class TestBuildAnnouncementBlurb:
    """Regression tests for the bug where a custom announcement blurb
    configured via /setup_events showed up as the lowercase short_key
    fallback in the daily draft (e.g. "glacieradon at 10:30am" instead of
    "We will be doing Glacieradon...").

    Root cause: the EventEditorView's "Add Event" handler appended only
    {key, dt} to event_list — without `blurb` — so build_announcement's
    `event.get("blurb") or ""` short-circuited to empty, and since the
    user's `glacieradon` short_key isn't in the legacy EVENT_LIBRARY,
    the final fallback was the f-string `f"{key} at {time} (...)"` —
    rendering the lowercase key.
    """

    def _make_event(self, **kwargs):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ET = ZoneInfo("America/New_York")
        defaults = {
            "key": "glacieradon",
            "dt": datetime(2026, 5, 1, 10, 30, tzinfo=ET),
        }
        defaults.update(kwargs)
        return defaults

    def test_uses_event_dict_blurb_when_present(self):
        from scheduler import build_announcement

        event = self._make_event(blurb="Custom blurb at {time} ({server_time}).")
        msg = build_announcement([event])
        assert "Custom blurb at" in msg
        assert "Server Time" not in msg or "Custom blurb" in msg

    def test_falls_back_to_resolve_event_info_when_blurb_missing(self, seeded_db):
        """The bug: event_list dict has no 'blurb' key. With guild_id
        passed, build_announcement should re-look-up the configured
        blurb from guild_events."""
        from scheduler import build_announcement
        from config import save_guild_event
        from tests.constants import TEST_GUILD_ID

        save_guild_event(
            TEST_GUILD_ID,
            {
                "short_key": "glacieradon",
                "name": "Glacieradon",
                "timezone": "America/New_York",
                "default_time": "10:30",
                "announcement_blurb": "We will be doing Glacieradon at {time} ({server_time} Server Time)! "
                "Remember to start with only 10 hits.",
                "schedule_type": "manual",
                "anchor_date": "",
                "interval_days": 3,
                "draft_channel_id": 100,
                "announcement_channel_id": 200,
                "draft_time": "09:00",
                "five_min_warning": 1,
                "active": 1,
            },
        )

        # Simulating the buggy add-event path: event_list dict lacks 'blurb'.
        event = self._make_event()  # only key + dt
        msg = build_announcement([event], guild_id=TEST_GUILD_ID)

        # The custom blurb must show up.
        assert "We will be doing Glacieradon" in msg
        assert "10 hits" in msg
        # The lowercase short_key fallback must NOT be present.
        assert "glacieradon at" not in msg, (
            f"Bug regression: short_key fallback used despite saved blurb.\n{msg}"
        )

    def test_no_guild_id_falls_back_to_short_key_string(self):
        """Backward-compat: callers that don't pass guild_id keep the
        old behavior — fall through to EVENT_LIBRARY then the f-string."""
        from scheduler import build_announcement

        event = self._make_event()  # no blurb, key not in EVENT_LIBRARY
        msg = build_announcement([event])  # no guild_id
        assert "glacieradon at" in msg

    def test_legacy_event_library_keys_still_resolve(self):
        """marauder/siege/etc. live in EVENT_LIBRARY for legacy guilds with
        no per-guild events table. Verify those still render."""
        from scheduler import build_announcement, EVENT_LIBRARY

        if "marauder" not in EVENT_LIBRARY:
            pytest.skip("marauder not in EVENT_LIBRARY")
        event = self._make_event(key="marauder")
        msg = build_announcement([event])
        # The hardcoded marauder blurb should render — no fallback f-string.
        assert "marauder at 10:30am" not in msg.lower() or "Marauder" in msg


# ── EventEditorView.add_event: regression ─────────────────────────────────────


class TestEventEditorAddEventBlurb:
    """The user-driven 'Add Event' button in the /events editor must
    populate the event_list dict with the configured blurb so the
    follow-up 'Build Announcement' click produces the right text. This
    used to drop the blurb on the floor."""

    def test_added_event_dict_carries_resolved_blurb(self, seeded_db):
        """White-box: the dict appended by the add_event handler should
        contain key/name/dt/blurb so build_announcement renders it
        correctly, even when called without a guild_id fallback."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from config import save_guild_event
        from scheduler import _resolve_event_info, build_announcement
        from tests.constants import TEST_GUILD_ID

        save_guild_event(
            TEST_GUILD_ID,
            {
                "short_key": "test_event",
                "name": "Test Event",
                "timezone": "America/New_York",
                "default_time": "10:00",
                "announcement_blurb": "TEST_EVENT_BLURB at {time} ({server_time}).",
                "schedule_type": "manual",
                "anchor_date": "",
                "interval_days": 3,
                "draft_channel_id": 1,
                "announcement_channel_id": 2,
                "draft_time": "09:00",
                "five_min_warning": 1,
                "active": 1,
            },
        )

        # Reproduce what the fixed add_event handler now does.
        info = _resolve_event_info("test_event", TEST_GUILD_ID)
        ET = ZoneInfo("America/New_York")
        event = {
            "key": "test_event",
            "name": info.get("name", "test_event"),
            "dt": datetime(2026, 5, 1, 10, 0, tzinfo=ET),
            "blurb": info.get("blurb", ""),
        }
        assert event["blurb"], "Bug: resolved info has no blurb"

        # And feeding that into build_announcement (no guild_id) renders it.
        msg = build_announcement([event])
        assert "TEST_EVENT_BLURB at" in msg


# ── format_et: timezone abbreviation in {time} ───────────────────────────────


class TestFormatEtTimezoneSuffix:
    """Regression: announcements rendered `5:00pm (19:00 Server Time)` with
    no label on the local time, leaving members to guess which tz the
    leader meant. format_et now appends dt.tzname() so {time} surfaces
    `5:00pm EDT` (or KST, etc., for non-ET alliances)."""

    def test_appends_et_abbreviation_for_eastern_time(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from scheduler import format_et

        # May → EDT (DST active)
        dt = datetime(2026, 5, 1, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        assert format_et(dt) == "5:00pm EDT"

    def test_appends_winter_abbreviation_for_eastern_time(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from scheduler import format_et

        # January → EST (no DST)
        dt = datetime(2026, 1, 15, 17, 0, tzinfo=ZoneInfo("America/New_York"))
        assert format_et(dt) == "5:00pm EST"

    def test_appends_non_et_abbreviation(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from scheduler import format_et

        dt = datetime(2026, 5, 1, 17, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        assert format_et(dt) == "5:00pm KST"

    def test_announcement_includes_local_tz_label(self):
        """End-to-end: a custom blurb using {time} should now render with
        the timezone abbreviation, not bare `5:00pm`."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from scheduler import build_announcement

        event = {
            "key": "marauder",
            "name": "Plague Marauder",
            "dt": datetime(2026, 5, 8, 17, 0, tzinfo=ZoneInfo("America/New_York")),
            "blurb": "Plague Marauder at {time} ({server_time} Server Time).",
        }
        msg = build_announcement([event])
        assert "5:00pm EDT" in msg
        assert "(19:00 Server Time)" in msg


# ── make_event_datetime: per-event tz instead of forced ET ───────────────────


class TestMakeEventDatetimeTimezone:
    """Regression: Add Event / Edit Time used to call make_et_datetime
    which always pinned the dt to America/New_York, silently coercing
    any non-ET alliance's edits into ET. The new helper accepts an
    explicit tz so the editor can preserve the per-event setting."""

    def test_default_tz_is_et_when_none_passed(self):
        from datetime import date
        from zoneinfo import ZoneInfo
        from scheduler import make_event_datetime

        dt = make_event_datetime(date(2026, 5, 8), 17, 0)
        assert dt.tzinfo == ZoneInfo("America/New_York")
        assert dt.hour == 17

    def test_explicit_tz_is_preserved(self):
        from datetime import date
        from zoneinfo import ZoneInfo
        from scheduler import make_event_datetime

        seoul = ZoneInfo("Asia/Seoul")
        dt = make_event_datetime(date(2026, 5, 8), 17, 0, tz=seoul)
        assert dt.tzinfo == seoul
        assert dt.hour == 17  # local hour, not converted

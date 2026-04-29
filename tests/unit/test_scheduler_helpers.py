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
            date(2026, 4,  2),
            date(2026, 4,  5),
            date(2026, 4,  8),
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
            anchor=date(2026, 3, 30),    # Monday
            cycle=7,
        )
        assert results == [
            date(2026, 4,  6),  # next Monday
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
        assert is_friday(date(2026, 4,  3)) is True   # 2026-04-03 is a Friday

    def test_not_friday(self):
        assert is_friday(date(2026, 4,  2)) is False  # Thursday
        assert is_friday(date(2026, 4,  4)) is False  # Saturday

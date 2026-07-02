"""Tests for member_stats.read_storm_attendance_map (roster attendance, #316).

The bulk attendance map sums attended/tracked across DS + CS into one % per
member, mirroring what `/member_stats` shows per member. The underlying
per-event-type log read is patched.
"""

from __future__ import annotations

import member_stats
import storm_log


def test_combines_event_types(monkeypatch):
    def fake_window(guild_id, event_type, count, key):
        if event_type == "DS":
            return (
                ["d1", "d2"],
                {"Ada": {"d1": "Yes", "d2": "No"}, "Bo": {"d1": "Yes", "d2": "Yes"}},
            )
        return (["d3"], {"Ada": {"d3": "Yes"}})  # CS

    monkeypatch.setattr(storm_log, "read_member_log_window", fake_window)
    out = member_stats.read_storm_attendance_map(123)
    assert out["ada"] == round(2 / 3 * 100)  # DS 1/2 + CS 1/1 = 2/3
    assert out["bo"] == 100  # DS 2/2, absent from CS


def test_empty_when_no_events(monkeypatch):
    monkeypatch.setattr(storm_log, "read_member_log_window", lambda *a, **k: ([], {}))
    assert member_stats.read_storm_attendance_map(123) == {}


def test_degrades_on_read_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sheet down")

    monkeypatch.setattr(storm_log, "read_member_log_window", boom)
    assert member_stats.read_storm_attendance_map(123) == {}

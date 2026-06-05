"""Tests for config.read_member_roster_values short-TTL cache (#269).

Rapid officer iteration through the storm hub / roster builder / sign-up
views fired one uncached Sheets read per click and blew Google's
60-reads/min quota. The cache collapses a burst into a single read while
staying fresh enough that a recent roster edit still appears.
"""

from unittest.mock import MagicMock, patch

import config


def _fake_ws(rows):
    ws = MagicMock()
    ws.get_all_values.return_value = rows
    return ws


class TestRosterReadCache:
    def setup_method(self):
        config.clear_roster_read_cache()

    def teardown_method(self):
        config.clear_roster_read_cache()

    def test_second_call_within_ttl_reuses_cache(self):
        ws = _fake_ws([["Header"], ["row1"]])
        with patch("config.get_member_roster_sheet", return_value=ws) as mock_open:
            v1 = config.read_member_roster_values(123, "Member Roster")
            v2 = config.read_member_roster_values(123, "Member Roster")
        assert v1 == v2 == [["Header"], ["row1"]]
        # The whole point: only one Sheets round-trip for the burst.
        assert mock_open.call_count == 1
        assert ws.get_all_values.call_count == 1

    def test_clear_forces_a_fresh_read(self):
        ws = _fake_ws([["Header"]])
        with patch("config.get_member_roster_sheet", return_value=ws) as mock_open:
            config.read_member_roster_values(123, "Member Roster")
            config.clear_roster_read_cache()
            config.read_member_roster_values(123, "Member Roster")
        assert mock_open.call_count == 2

    def test_entry_older_than_ttl_is_refreshed(self):
        ws = _fake_ws([["Header"]])
        with (
            patch("config.get_member_roster_sheet", return_value=ws) as mock_open,
            patch("config.time.monotonic", side_effect=[0.0, 100.0]),
        ):
            config.read_member_roster_values(123, "Member Roster")
            config.read_member_roster_values(123, "Member Roster")  # 100s later > TTL
        assert mock_open.call_count == 2

    def test_distinct_guild_or_tab_keys_cache_separately(self):
        ws_roster = _fake_ws([["Member Roster"]])
        ws_power = _fake_ws([["Squad Powers"]])

        def opener(guild_id, tab):
            return ws_roster if tab == "Member Roster" else ws_power

        with patch("config.get_member_roster_sheet", side_effect=opener):
            va = config.read_member_roster_values(123, "Member Roster")
            vb = config.read_member_roster_values(123, "Squad Powers")
            # Different guild, same tab, is also a distinct key.
            vc = config.read_member_roster_values(999, "Member Roster")
        assert va == [["Member Roster"]]
        assert vb == [["Squad Powers"]]
        assert vc == [["Member Roster"]]

    def test_read_failure_propagates_and_is_not_cached(self):
        with patch("config.get_member_roster_sheet", side_effect=RuntimeError("boom")):
            try:
                config.read_member_roster_values(123, "Member Roster")
                raise AssertionError("expected RuntimeError to propagate")
            except RuntimeError:
                pass
        # A failed read must not poison the cache with a bad/empty entry.
        ws = _fake_ws([["Header"]])
        with patch("config.get_member_roster_sheet", return_value=ws) as mock_open:
            config.read_member_roster_values(123, "Member Roster")
        assert mock_open.call_count == 1

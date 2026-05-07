"""
Unit tests for growth.py — load_member_data, _run_growth_snapshot_inner,
snapshot skipping logic, column header generation, and the
compute_next_snapshot helper used to surface "next fire" date in
/setup_growth and /growth.
"""
from datetime import datetime
import pytest
from unittest.mock import patch, MagicMock, call
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.constants import TEST_GUILD_ID


class TestLoadMemberData:
    """Test load_member_data reads correct columns from configured source tab."""

    def _mock_sheet(self, rows):
        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=rows)
        sh = MagicMock()
        sh.worksheet = MagicMock(return_value=ws)
        return sh

    def test_loads_name_and_metrics(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source="Squad Powers", name_col="A",
            metrics=[{"col": "B", "label": "Power"}, {"col": "C", "label": "THP"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly", snapshot_day=1,
            snapshot_interval=30, data_start_row=2,
        )

        rows = [
            ["Name",  "Power", "THP"],
            ["Alice", "43.27", "301"],
            ["Bob",   "38.50", "280"],
        ]
        mock_sh = self._mock_sheet(rows)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 2
        assert members[0]["name"]  == "Alice"
        assert members[0]["Power"] == 43.27
        assert members[0]["THP"]   == 301.0
        assert members[1]["name"]  == "Bob"

    def test_skips_empty_name_rows(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID, enabled=1, tab_source="Members", name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth", snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        rows = [
            ["Name",  "Power"],
            ["Alice", "43.27"],
            ["",      "38.50"],  # empty name — should be skipped
            ["Carol", "41.00"],
        ]
        mock_sh = self._mock_sheet(rows)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 2
        names = [m["name"] for m in members]
        assert "Alice" in names
        assert "Carol" in names

    def test_returns_empty_without_config(self, seeded_db):
        from growth import load_member_data
        # Growth not configured (enabled=0 by default)
        members = load_member_data(TEST_GUILD_ID)
        assert members == []

    def test_custom_column_letters(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID, enabled=1, tab_source="Roster", name_col="D",
            metrics=[{"col": "E", "label": "Squad1"}, {"col": "G", "label": "Squad2"}],
            tab_growth="Growth", snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        # D=col3, E=col4, G=col6 (0-indexed)
        rows = [
            ["A", "B", "C", "Name",  "Squad1", "X", "Squad2"],
            ["", "", "", "Alice", "43.27",  "",  "38.50"],
        ]
        mock_sh = self._mock_sheet(rows)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 1
        assert members[0]["name"]   == "Alice"
        assert members[0]["Squad1"] == 43.27
        assert members[0]["Squad2"] == 38.50


class TestRunGrowthSnapshotInner:
    """Test _run_growth_snapshot_inner skipping and write logic."""

    def test_skips_when_disabled(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID, enabled=0,
            tab_source="Squad Powers", name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth", snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        with patch("growth._get_spreadsheet") as mock_sh:
            _run_growth_snapshot_inner(TEST_GUILD_ID)
            mock_sh.assert_not_called()

    def test_skips_when_no_spreadsheet_id(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config, get_config, save_config

        cfg = get_config(TEST_GUILD_ID)
        cfg.spreadsheet_id = ""
        save_config(cfg)

        save_growth_config(
            TEST_GUILD_ID, enabled=1, tab_source="Tab", name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth", snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        with patch("growth._get_spreadsheet") as mock_sh:
            _run_growth_snapshot_inner(TEST_GUILD_ID)
            mock_sh.assert_not_called()

    def test_skips_duplicate_period(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        from datetime import datetime

        save_growth_config(
            TEST_GUILD_ID, enabled=1, tab_source="Powers", name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth", snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        month_label = datetime.now().strftime("%b %Y")

        # Sheet already has this period's column
        existing_headers = ["Name", f"Power ({month_label})"]
        mock_ws = MagicMock()
        mock_ws.row_count    = 5
        mock_ws.row_values   = MagicMock(return_value=existing_headers)
        mock_ws.get_all_values = MagicMock(return_value=[existing_headers])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        with patch("growth._get_spreadsheet", return_value=mock_sh), \
             patch("growth.load_member_data", return_value=[]):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        # batch_update should not have been called
        mock_ws.batch_update.assert_not_called()

    def test_writes_member_data(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        from datetime import datetime

        save_growth_config(
            TEST_GUILD_ID, enabled=1, tab_source="Powers", name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth", snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        month_label = datetime.now().strftime("%b %Y")
        col_name    = f"Power ({month_label})"

        mock_ws = MagicMock()
        mock_ws.row_count      = 10
        mock_ws.row_values     = MagicMock(return_value=["Name"])
        mock_ws.get_all_values = MagicMock(return_value=[["Name"]])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        members = [
            {"name": "Alice", "row_index": 2, "Power": 43.27},
            {"name": "Bob",   "row_index": 3, "Power": 38.50},
        ]

        with patch("growth._get_spreadsheet", return_value=mock_sh), \
             patch("growth.load_member_data", return_value=members):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        # Header should be updated with new column
        mock_ws.update.assert_called()
        header_call = mock_ws.update.call_args_list[0]
        updated_header = header_call[0][1][0]
        assert col_name in updated_header

    def test_new_members_appended_in_one_batched_call(self, seeded_db):
        """Regression for #40: a populated roster (60+ members) on a first-ever
        snapshot must not call ws.append_row once per member — that exhausts
        the 60/min Sheets write quota mid-write and aborts the snapshot. The
        snapshot must use ws.append_rows (plural) once with all new rows."""
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        from datetime import datetime

        save_growth_config(
            TEST_GUILD_ID, enabled=1, tab_source="Powers", name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth", snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        mock_ws = MagicMock()
        mock_ws.row_count      = 100
        mock_ws.row_values     = MagicMock(return_value=["Name"])
        mock_ws.get_all_values = MagicMock(return_value=[["Name"]])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        members = [
            {"name": f"Member{i:02d}", "row_index": i + 2, "Power": float(i)}
            for i in range(60)
        ]

        with patch("growth._get_spreadsheet", return_value=mock_sh), \
             patch("growth.load_member_data", return_value=members):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        mock_ws.append_row.assert_not_called()
        mock_ws.append_rows.assert_called_once()
        appended = mock_ws.append_rows.call_args[0][0]
        assert len(appended) == 60, \
            f"expected 60 rows in the single append_rows call, got {len(appended)}"
        assert appended[0][0] == "Member00"
        assert appended[-1][0] == "Member59"

    def test_creates_growth_tab_if_missing(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        import gspread

        save_growth_config(
            TEST_GUILD_ID, enabled=1, tab_source="Powers", name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="New Growth Tab",  # tab that doesn't exist yet
            snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        mock_new_ws = MagicMock()
        mock_new_ws.row_count      = 10
        mock_new_ws.row_values     = MagicMock(return_value=[])
        mock_new_ws.get_all_values = MagicMock(return_value=[])
        mock_new_ws.update         = MagicMock()
        mock_new_ws.batch_update   = MagicMock()

        mock_source_ws = MagicMock()
        mock_source_ws.get_all_values = MagicMock(return_value=[
            ["Name", "Power"],
            ["Alice", "43.27"],
        ])

        def fake_worksheet(name):
            if name == "Powers":
                return mock_source_ws
            raise gspread.exceptions.WorksheetNotFound(name)

        mock_sh = MagicMock()
        mock_sh.worksheet     = MagicMock(side_effect=fake_worksheet)
        mock_sh.add_worksheet = MagicMock(return_value=mock_new_ws)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        mock_sh.add_worksheet.assert_called_once_with(
            title="New Growth Tab", rows=500, cols=50
        )


# ── Next-snapshot date helper ─────────────────────────────────────────────────

class TestComputeNextSnapshot:
    """The wizard and /growth use this to tell users when the next snapshot
    will fire. Must stay aligned with the scheduling logic in
    bot.growth_task — same epoch (2026-01-01), same fire hour (22:00 ET)."""

    def _now(self, year, month, day, hour=12):
        from growth import ET
        return datetime(year, month, day, hour, 0, tzinfo=ET)

    def test_returns_none_when_disabled(self):
        from growth import compute_next_snapshot
        assert compute_next_snapshot({}) is None
        assert compute_next_snapshot({"enabled": 0}) is None

    # ── Monthly schedule ──────────────────────────────────────────────────────

    def test_monthly_today_before_snapshot_day_returns_this_month(self):
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 5, 1),
        )
        assert nxt.date().isoformat() == "2026-05-05"
        assert nxt.hour == 22

    def test_monthly_today_equals_snapshot_day_before_22_returns_today(self):
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 5, 5, hour=14),
        )
        assert nxt.date().isoformat() == "2026-05-05"

    def test_monthly_today_equals_snapshot_day_after_22_returns_next_month(self):
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 5, 5, hour=23),
        )
        assert nxt.date().isoformat() == "2026-06-05"

    def test_monthly_today_after_snapshot_day_returns_next_month(self):
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 5, 15),
        )
        assert nxt.date().isoformat() == "2026-06-05"

    def test_monthly_december_wraps_into_january(self):
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 12, 10),
        )
        assert nxt.date().isoformat() == "2027-01-05"

    def test_monthly_clamps_snapshot_day_to_28(self):
        """Stored snapshot_day is already clamped to 1..28 by the wizard,
        but the helper guards against bad data slipping through."""
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 99},
            now=self._now(2026, 5, 1),
        )
        # Clamps to 28, so May 28 is the next valid fire.
        assert nxt.day == 28

    # ── Interval schedule ─────────────────────────────────────────────────────

    def test_interval_today_is_epoch_before_22_returns_today(self):
        """2026-01-01 is the epoch — interval=14 means today is a fire day."""
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 14},
            now=self._now(2026, 1, 1, hour=14),
        )
        assert nxt.date().isoformat() == "2026-01-01"
        assert nxt.hour == 22

    def test_interval_today_is_epoch_after_22_skips_to_next_window(self):
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 14},
            now=self._now(2026, 1, 1, hour=23),
        )
        assert nxt.date().isoformat() == "2026-01-15"

    def test_interval_today_not_a_multiple_returns_next_multiple(self):
        from growth import compute_next_snapshot
        # Jan 5 → 4 days into the cycle, next 14-day mark is Jan 15.
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 14},
            now=self._now(2026, 1, 5),
        )
        assert nxt.date().isoformat() == "2026-01-15"

    def test_interval_one_day_returns_today_or_tomorrow(self):
        """interval=1 should always be today (before 22:00) or tomorrow."""
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 1},
            now=self._now(2026, 5, 15, hour=14),
        )
        assert nxt.date().isoformat() == "2026-05-15"

        nxt2 = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 1},
            now=self._now(2026, 5, 15, hour=23),
        )
        assert nxt2.date().isoformat() == "2026-05-16"

    def test_interval_alignment_matches_scheduler_epoch(self):
        """Critical: the helper's "next fire" must agree with the
        scheduler's `(today - epoch).days % interval == 0` rule. If we
        ever change INTERVAL_EPOCH on one side and forget the other,
        users will see one date but get a snapshot on a different one."""
        from growth import compute_next_snapshot, INTERVAL_EPOCH
        # Sanity: epoch matches the bot.growth_task hardcode.
        assert INTERVAL_EPOCH.isoformat() == "2026-01-01"

        # Pick an arbitrary day, verify the helper returns a date that
        # would actually trigger the scheduler.
        from growth import ET
        from datetime import date as _date
        now = self._now(2026, 7, 4)
        for interval in (1, 7, 14, 30, 90):
            nxt = compute_next_snapshot(
                {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": interval},
                now=now,
            )
            delta = (nxt.date() - INTERVAL_EPOCH).days
            assert delta % interval == 0, \
                f"interval={interval}: next={nxt.date()} (delta={delta}) is not a multiple"

    # ── Defensive ─────────────────────────────────────────────────────────────

    def test_unknown_frequency_returns_none(self):
        from growth import compute_next_snapshot
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "weekly"},
            now=self._now(2026, 5, 1),
        )
        assert nxt is None

    def test_naive_datetime_treated_as_et(self):
        """Pass a naive datetime — the helper should interpret it as ET
        rather than UTC, otherwise the "before 22:00" comparison wraps."""
        from growth import compute_next_snapshot
        naive = datetime(2026, 5, 5, 14, 0)  # no tzinfo
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=naive,
        )
        assert nxt.date().isoformat() == "2026-05-05"

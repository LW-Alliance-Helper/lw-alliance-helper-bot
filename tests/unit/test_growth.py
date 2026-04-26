"""
Unit tests for growth.py — load_member_data, _run_growth_snapshot_inner,
snapshot skipping logic, column header generation.
"""
import pytest
from unittest.mock import patch, MagicMock, call
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


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

    def test_creates_growth_tab_if_missing(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        import gspread

        save_growth_config(
            TEST_GUILD_ID, enabled=1, tab_source="Powers", name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth Tracking", snapshot_frequency="monthly",
            snapshot_day=1, snapshot_interval=30, data_start_row=2,
        )

        mock_new_ws = MagicMock()
        mock_new_ws.row_count      = 10
        mock_new_ws.row_values     = MagicMock(return_value=[])
        mock_new_ws.get_all_values = MagicMock(return_value=[])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(
            side_effect=gspread.exceptions.WorksheetNotFound("Growth Tracking")
        )
        mock_sh.add_worksheet = MagicMock(return_value=mock_new_ws)

        with patch("growth._get_spreadsheet", return_value=mock_sh), \
             patch("growth.load_member_data", return_value=[]):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        mock_sh.add_worksheet.assert_called_once_with(
            title="Growth Tracking", rows=500, cols=50
        )

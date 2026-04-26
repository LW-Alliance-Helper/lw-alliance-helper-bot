"""
Real Google Sheet read tests.
Writes seed data to test tabs, then verifies the bot reads it correctly.
Requires GOOGLE_CREDENTIALS_JSON env var.
"""
import pytest
import time
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, TEST_SHEET_ID

pytestmark = pytest.mark.sheets


def get_real_sheet_client():
    import gspread, json
    from google.oauth2.service_account import Credentials
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        pytest.skip("GOOGLE_CREDENTIALS_JSON not set")
    info   = json.loads(creds_json)
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds  = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(TEST_SHEET_ID)


class TestGrowthSheetReads:
    """Seed member data in test tab, verify load_member_data reads it."""

    def test_load_member_data_reads_correct_columns(
        self, seeded_db, fresh_tab
    ):
        from growth import load_member_data
        from config import save_growth_config
        from unittest.mock import patch

        ws = fresh_tab

        # Seed: Name=A, Power=B, THP=C
        ws.update("A1", [
            ["Name",  "Power", "THP"],
            ["Alice", "43.27", "301"],
            ["Bob",   "38.50", "280"],
            ["Carol", "41.00", "295"],
        ], value_input_option="USER_ENTERED")
        time.sleep(1)

        save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source=ws.title, name_col="A",
            metrics=[
                {"col": "B", "label": "Power"},
                {"col": "C", "label": "THP"},
            ],
            tab_growth="_test_growth_output",
            snapshot_frequency="monthly", snapshot_day=1,
            snapshot_interval=30, data_start_row=2,
        )

        sh = get_real_sheet_client()
        with patch("growth._get_spreadsheet", return_value=sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 3
        alice = next(m for m in members if m["name"] == "Alice")
        assert alice["Power"] == 43.27
        assert alice["THP"]   == 301.0

        bob = next(m for m in members if m["name"] == "Bob")
        assert bob["Power"] == 38.50

    def test_load_member_data_skips_empty_rows(
        self, seeded_db, fresh_tab
    ):
        from growth import load_member_data
        from config import save_growth_config
        from unittest.mock import patch

        ws = fresh_tab
        ws.update("A1", [
            ["Name",  "Power"],
            ["Alice", "43.27"],
            ["",      "99.99"],  # empty name
            ["Carol", "41.00"],
            ["",      ""],       # completely empty
        ], value_input_option="USER_ENTERED")
        time.sleep(1)

        save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source=ws.title, name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="_test_growth_out",
            snapshot_frequency="monthly", snapshot_day=1,
            snapshot_interval=30, data_start_row=2,
        )

        sh = get_real_sheet_client()
        with patch("growth._get_spreadsheet", return_value=sh):
            members = load_member_data(TEST_GUILD_ID)

        names = [m["name"] for m in members]
        assert "Alice" in names
        assert "Carol" in names
        assert ""      not in names
        assert len(members) == 2

    def test_load_member_data_custom_column_positions(
        self, seeded_db, fresh_tab
    ):
        """Test reading with non-standard column positions (D and G)."""
        from growth import load_member_data
        from config import save_growth_config
        from unittest.mock import patch

        ws = fresh_tab
        # Name in D (col 3), Power in G (col 6)
        ws.update("A1", [
            ["X",  "Y",  "Z",  "Name",  "A", "B", "Power"],
            ["",   "",   "",   "Alice", "",  "",   "43.27"],
            ["",   "",   "",   "Bob",   "",  "",   "38.50"],
        ], value_input_option="USER_ENTERED")
        time.sleep(1)

        save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source=ws.title, name_col="D",
            metrics=[{"col": "G", "label": "Power"}],
            tab_growth="_test_growth_out",
            snapshot_frequency="monthly", snapshot_day=1,
            snapshot_interval=30, data_start_row=2,
        )

        sh = get_real_sheet_client()
        with patch("growth._get_spreadsheet", return_value=sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 2
        assert members[0]["name"]  == "Alice"
        assert members[0]["Power"] == 43.27


class TestGrowthSnapshotWrites:
    """Run a real growth snapshot and verify output tab."""

    def test_snapshot_creates_header_and_data_rows(
        self, seeded_db, fresh_tab, growth_tab
    ):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config, get_config, save_config
        from unittest.mock import patch
        from datetime import datetime

        source_ws        = fresh_tab
        growth_ws, g_name= growth_tab

        # Seed source data
        source_ws.update("A1", [
            ["Name",  "Power"],
            ["Alice", "43.27"],
            ["Bob",   "38.50"],
        ], value_input_option="USER_ENTERED")
        time.sleep(0.5)

        save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source=source_ws.title, name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth=g_name,
            snapshot_frequency="monthly", snapshot_day=1,
            snapshot_interval=30, data_start_row=2,
        )

        sh = get_real_sheet_client()
        with patch("growth._get_spreadsheet", return_value=sh):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        time.sleep(1)
        all_vals = growth_ws.get_all_values()
        assert len(all_vals) >= 1, "Growth tab should have data"

        header      = all_vals[0]
        month_label = datetime.now().strftime("%b %Y")
        col_name    = f"Power ({month_label})"

        assert "Name"    in header, f"Header should contain Name: {header}"
        assert col_name  in header, f"Header should contain {col_name}: {header}"

        names = [r[0] for r in all_vals[1:] if r]
        assert "Alice" in names
        assert "Bob"   in names

    def test_snapshot_does_not_duplicate_period(
        self, seeded_db, fresh_tab, growth_tab
    ):
        """Running snapshot twice for the same period should not add duplicate columns."""
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        from unittest.mock import patch
        from datetime import datetime

        source_ws        = fresh_tab
        growth_ws, g_name= growth_tab

        source_ws.update("A1", [
            ["Name",  "Power"],
            ["Alice", "43.27"],
        ], value_input_option="USER_ENTERED")
        time.sleep(0.5)

        save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source=source_ws.title, name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth=g_name,
            snapshot_frequency="monthly", snapshot_day=1,
            snapshot_interval=30, data_start_row=2,
        )

        sh = get_real_sheet_client()
        with patch("growth._get_spreadsheet", return_value=sh):
            _run_growth_snapshot_inner(TEST_GUILD_ID)
            time.sleep(0.5)
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        time.sleep(1)
        month_label = datetime.now().strftime("%b %Y")
        header      = growth_ws.row_values(1)
        col_name    = f"Power ({month_label})"
        count       = header.count(col_name)
        assert count == 1, f"Expected 1 column for {col_name}, got {count}: {header}"


class TestTrainScheduleSheetReads:
    """Verify train schedule reads from the correct tab."""

    def test_birthday_data_read_from_configured_tab(
        self, seeded_db, fresh_tab
    ):
        from train import load_birthdays
        from config import save_birthday_config
        from unittest.mock import patch

        ws = fresh_tab
        ws.update("A1", [
            ["Name",  "Birthday"],
            ["Alice", "3/15"],
            ["Bob",   "December 25"],
            ["Carol", "7/4"],
            ["Dave",  "not-a-date"],  # invalid — should be skipped
        ], value_input_option="USER_ENTERED")
        time.sleep(1)

        save_birthday_config(
            TEST_GUILD_ID, tab_name=ws.title,
            name_col=0, birthday_col=1,
            data_start_row=2, enabled=1,
            train_integration=1, flexible_placement=0,
            lookahead_days=14,
        )

        sh = get_real_sheet_client()

        def fake_get_member_sheet(tab_name, gid=None):
            return sh.worksheet(tab_name)

        with patch("train._get_member_sheet", side_effect=fake_get_member_sheet):
            members = load_birthdays(ws.title, guild_id=TEST_GUILD_ID)

        names = [m["name"] for m in members]
        assert "Alice" in names
        assert "Bob"   in names
        assert "Carol" in names
        assert "Dave"  not in names  # invalid date skipped

        alice = next(m for m in members if m["name"] == "Alice")
        assert alice["month"] == 3
        assert alice["day"]   == 15

        bob = next(m for m in members if m["name"] == "Bob")
        assert bob["month"] == 12
        assert bob["day"]   == 25

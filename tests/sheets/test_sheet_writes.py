"""
Real Google Sheet write tests for survey.py.
These tests write to the test spreadsheet, verify the data,
then clean up. Requires GOOGLE_CREDENTIALS_JSON env var.
"""
import pytest
import time
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, TEST_SHEET_ID
from tests.sheets.conftest import *


pytestmark = pytest.mark.sheets  # mark all tests in this file


class TestSurveySheetWrites:
    """Write survey responses to a real sheet and verify them."""

    def test_update_squad_powers_creates_header_row(
        self, seeded_db, squad_powers_tab
    ):
        ws, tab_name = squad_powers_tab
        from survey import update_squad_powers
        from config import save_survey_config, get_config, save_config

        questions = [
            {"key": "squad1", "label": "1st Squad Power", "type": "text",
             "options": [], "placeholder": "", "max_chars": 0},
            {"key": "role", "label": "Role", "type": "dropdown",
             "options": ["War Leader", "Engineer"],
             "placeholder": "", "max_chars": 0},
        ]
        save_survey_config(TEST_GUILD_ID, tab_name, "_unused_history",
                           questions, "Test survey")

        # Patch _get_spreadsheet to return the real test sheet
        import gspread, json
        from google.oauth2.service_account import Credentials
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        info       = json.loads(creds_json)
        scopes     = ["https://spreadsheets.google.com/feeds",
                      "https://www.googleapis.com/auth/drive"]
        creds  = Credentials.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)
        sh     = client.open_by_key(TEST_SHEET_ID)

        from unittest.mock import patch
        with patch("survey._get_spreadsheet", return_value=sh):
            update_squad_powers(
                "111111111", "TestUser",
                {"squad1": "43.27", "role": "War Leader"},
                guild_id=TEST_GUILD_ID,
            )

        time.sleep(1)  # allow sheet to settle
        all_vals = ws.get_all_values()

        assert len(all_vals) >= 2, "Expected header + data row"
        header = all_vals[0]
        assert "Username"          in header
        assert "1st Squad Power"   in header
        assert "Role"              in header

        data_row = all_vals[1]
        assert "TestUser"   in data_row
        assert "43.27"      in data_row
        assert "War Leader" in data_row

    def test_update_squad_powers_updates_existing_row(
        self, seeded_db, squad_powers_tab
    ):
        ws, tab_name = squad_powers_tab
        from survey import update_squad_powers
        from config import save_survey_config

        questions = [
            {"key": "power", "label": "Power", "type": "text",
             "options": [], "placeholder": "", "max_chars": 0},
        ]
        save_survey_config(TEST_GUILD_ID, tab_name, "_unused_history",
                           questions, "")

        import gspread, json
        from google.oauth2.service_account import Credentials
        from unittest.mock import patch
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        info       = json.loads(creds_json)
        scopes     = ["https://spreadsheets.google.com/feeds",
                      "https://www.googleapis.com/auth/drive"]
        creds  = Credentials.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)
        sh     = client.open_by_key(TEST_SHEET_ID)

        with patch("survey._get_spreadsheet", return_value=sh):
            # First submission
            update_squad_powers("222222", "Alice", {"power": "40.00"},
                                guild_id=TEST_GUILD_ID)
            time.sleep(1)
            # Second submission — same user, different value
            update_squad_powers("222222", "Alice", {"power": "43.27"},
                                guild_id=TEST_GUILD_ID)

        time.sleep(1)
        all_vals = ws.get_all_values()

        # Should still be exactly 2 rows (header + Alice)
        data_rows = [r for r in all_vals[1:] if any(r)]
        assert len(data_rows) == 1, f"Expected 1 data row, got {len(data_rows)}"
        assert "43.27" in data_rows[0], "Row should have updated value"

    def test_append_survey_history_creates_timestamped_rows(
        self, seeded_db, squad_powers_tab, history_tab
    ):
        _, sp_tab_name   = squad_powers_tab
        ws_h, hist_tab_name = history_tab
        from survey import append_survey_history
        from config import save_survey_config

        questions = [
            {"key": "power", "label": "Power", "type": "text",
             "options": [], "placeholder": "", "max_chars": 0},
        ]
        save_survey_config(TEST_GUILD_ID, sp_tab_name, hist_tab_name,
                           questions, "")

        import gspread, json
        from google.oauth2.service_account import Credentials
        from unittest.mock import patch
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        info       = json.loads(creds_json)
        scopes     = ["https://spreadsheets.google.com/feeds",
                      "https://www.googleapis.com/auth/drive"]
        creds  = Credentials.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)
        sh     = client.open_by_key(TEST_SHEET_ID)

        with patch("survey._get_spreadsheet", return_value=sh):
            append_survey_history("333333", "Bob",   {"power": "38.50"},
                                  guild_id=TEST_GUILD_ID)
            time.sleep(0.5)
            append_survey_history("444444", "Carol", {"power": "41.00"},
                                  guild_id=TEST_GUILD_ID)

        time.sleep(1)
        all_vals = ws_h.get_all_values()

        header    = all_vals[0]
        data_rows = all_vals[1:]

        assert "Timestamp" in header
        assert "Discord ID" in header
        assert len(data_rows) == 2

        names = [r[2] for r in data_rows if len(r) > 2]
        assert "Bob"   in names
        assert "Carol" in names

    def test_multiple_users_each_get_own_row(
        self, seeded_db, squad_powers_tab
    ):
        ws, tab_name = squad_powers_tab
        from survey import update_squad_powers
        from config import save_survey_config

        questions = [
            {"key": "power", "label": "Power", "type": "text",
             "options": [], "placeholder": "", "max_chars": 0},
        ]
        save_survey_config(TEST_GUILD_ID, tab_name, "_unused", questions, "")

        import gspread, json
        from google.oauth2.service_account import Credentials
        from unittest.mock import patch
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        info       = json.loads(creds_json)
        scopes     = ["https://spreadsheets.google.com/feeds",
                      "https://www.googleapis.com/auth/drive"]
        creds  = Credentials.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)
        sh     = client.open_by_key(TEST_SHEET_ID)

        users = [("111", "Alice", "43.27"), ("222", "Bob", "38.50"),
                 ("333", "Carol", "41.00")]

        with patch("survey._get_spreadsheet", return_value=sh):
            for uid, name, power in users:
                update_squad_powers(uid, name, {"power": power},
                                    guild_id=TEST_GUILD_ID)
                time.sleep(0.3)

        time.sleep(1)
        all_vals = ws.get_all_values()
        data_rows = all_vals[1:]
        names = [r[0] for r in data_rows if r]
        assert "Alice" in names
        assert "Bob"   in names
        assert "Carol" in names

"""
Phase 5 of the full-coverage suite: live Google Sheet writes.

Hits the **real** test spreadsheet (TEST_SHEET_ID) using the bot's
service-account credentials and verifies the data the bot writes ends up
in the right cells. Skipped when GOOGLE_CREDENTIALS_JSON isn't set, so
local CI runs without sheets credentials still pass.

Existing tests/sheets/test_sheet_writes.py covers survey writes
(update_squad_powers + append_survey_history). This file adds:

  * append_participation_row — the new (#20) configurable participation
    log writer creates a header row on first use, then appends rows
    whose columns line up with the configured questions.

NOT covered (yet): save_ds_assignments and save_cs_assignments. Both
reference an undefined module-level `cfg` variable, which today raises
NameError swallowed by the surrounding `except Exception`. Adding live
tests against broken code is misleading — flagged for a separate fix
before any test here can pass.

Why no /survey_remind sheet tests: scheduled reminders dispatch via
Discord channels and DMs, not Sheets. The DM path reads the Member
Roster sheet (covered by member_roster's own tests), but writes happen
through Discord, not gspread.
"""
from __future__ import annotations

import os
import json
import random
import time
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, TEST_SHEET_ID


pytestmark = pytest.mark.sheets


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_real_spreadsheet():
    """Open the live test spreadsheet using the service account creds."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        pytest.skip("GOOGLE_CREDENTIALS_JSON not set")
    info   = json.loads(creds_json)
    scopes = ["https://spreadsheets.google.com/feeds",
              "https://www.googleapis.com/auth/drive"]
    creds  = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds).open_by_key(TEST_SHEET_ID)


@pytest.fixture
def participation_tab(test_spreadsheet):
    """Fresh participation-log tab per test, deleted after."""
    name = f"_test_part_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=30)
    yield ws, name
    try:
        test_spreadsheet.del_worksheet(ws)
    except Exception:
        pass


# ── append_participation_row ─────────────────────────────────────────────────

class TestParticipationRowWrite:
    """The new (#20) participation log writer creates the header row on
    first call and appends rows that line up with the configured
    questions."""

    def test_first_write_creates_header_then_data(
        self, seeded_db, participation_tab,
    ):
        from datetime import date
        from unittest.mock import patch
        from config import save_storm_config, save_participation_config
        import storm_log

        ws, tab_name = participation_tab

        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Tab", "Body",
            "", "", "", "", "", "",
            "America/New_York", 0,
        )
        save_participation_config(
            TEST_GUILD_ID, "DS",
            enabled=1, tab_name=tab_name,
            questions=[
                {"key": "vote_count", "label": "Vote Count", "type": "numeric"},
                {"key": "outcome",    "label": "Outcome",    "type": "text"},
                {"key": "sitting_out","label": "Sitting Out","type": "roster_names"},
            ],
            roster_tab="Roster", roster_name_col=0,
            roster_alias_col=-1, roster_start_row=2,
        )

        sh = _make_real_spreadsheet()
        with patch.object(storm_log, "_get_spreadsheet", return_value=sh):
            storm_log.append_participation_row(
                guild_id=TEST_GUILD_ID,
                event_type="DS",
                log_date=date(2026, 4, 14),
                answers={
                    "vote_count":  "38",
                    "outcome":     "Win",
                    "sitting_out": ["Alice", "Bob"],
                },
            )

        time.sleep(1.0)  # let the sheet settle
        rows = ws.get_all_values()

        # Header on row 1
        assert rows[0][0] == "Date"
        assert rows[0][1] == "Event"
        assert rows[0][2] == "Vote Count"
        assert rows[0][3] == "Outcome"
        assert rows[0][4] == "Sitting Out"

        # Data on row 2
        assert rows[1][0] == "4/14/2026"
        assert rows[1][1] == "DS"
        assert rows[1][2] == "38"
        assert rows[1][3] == "Win"
        # Roster names list joined with ", "
        assert "Alice" in rows[1][4]
        assert "Bob"   in rows[1][4]

    def test_subsequent_writes_append_below_existing_header(
        self, seeded_db, participation_tab,
    ):
        """Two writes against the same tab → header is reused, both
        rows appended in order."""
        from datetime import date
        from unittest.mock import patch
        from config import save_storm_config, save_participation_config
        import storm_log

        ws, tab_name = participation_tab

        save_storm_config(
            TEST_GUILD_ID, "CS", "CS Tab", "Body",
            "", "", "", "", "", "",
            "America/New_York", 0,
        )
        save_participation_config(
            TEST_GUILD_ID, "CS",
            enabled=1, tab_name=tab_name,
            questions=[
                {"key": "outcome", "label": "Outcome", "type": "text"},
            ],
            roster_tab="Roster", roster_name_col=0,
            roster_alias_col=-1, roster_start_row=2,
        )

        sh = _make_real_spreadsheet()
        with patch.object(storm_log, "_get_spreadsheet", return_value=sh):
            storm_log.append_participation_row(
                guild_id=TEST_GUILD_ID,
                event_type="CS",
                log_date=date(2026, 4, 14),
                answers={"outcome": "Win"},
            )
            time.sleep(0.6)
            storm_log.append_participation_row(
                guild_id=TEST_GUILD_ID,
                event_type="CS",
                log_date=date(2026, 4, 21),
                answers={"outcome": "Loss"},
            )

        time.sleep(1.0)
        rows = ws.get_all_values()

        data_rows = [r for r in rows[1:] if any(r)]
        assert len(data_rows) == 2, f"expected 2 rows; got {len(data_rows)}"
        assert data_rows[0][2] == "Win"
        assert data_rows[1][2] == "Loss"



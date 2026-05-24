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
  * save_ds_assignments / save_cs_assignments — DS/CS draft "Post &
    Copy" path writes the parsed zone assignments back to the sheet
    so they become next week's default. Now that the missing
    `guild_id` plumbing has been fixed (see commit history), these
    actually persist.

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
    """Fresh participation-log tab per test. Tab persists for inspection
    — the session-scoped cleanup in tests/sheets/conftest.py wipes it
    at the START of the next session."""
    name = f"_test_part_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=30)
    yield ws, name


@pytest.fixture
def assignments_tab(test_spreadsheet):
    """Fresh DS/CS assignments tab per test. Tab persists for inspection."""
    name = f"_test_assign_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=30)
    yield ws, name


@pytest.fixture
def train_tab(test_spreadsheet):
    """Fresh Train Schedule tab per test. The bot manages its own header
    in row 1 (Date / Name / Theme / Tone / Notes / Prompt Retrieved),
    so we pre-seed that here to match what /setup_train would have
    created on a real install."""
    name = f"_test_train_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=10)
    ws.update("A1", [[
        "Date", "Name", "Theme", "Tone", "Notes", "Prompt Retrieved",
    ]], value_input_option="USER_ENTERED")
    yield ws, name


@pytest.fixture
def roster_tab(test_spreadsheet):
    """Fresh Member Roster tab per test. write_roster does its own
    ws.clear() + writes header + rows, so we don't pre-seed anything."""
    name = f"_test_roster_{random.randint(100000, 999999)}"
    ws   = test_spreadsheet.add_worksheet(title=name, rows=200, cols=10)
    yield ws, name


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


# ── save_ds_assignments / save_cs_assignments ────────────────────────────────

class TestStormAssignmentsWrite:
    """The DS/CS draft 'Post & Copy' path persists the parsed zone
    assignments back to the sheet so they become next week's default
    (visible in the next /[event]_draft as the loaded template).

    Both functions now take guild_id properly — pre-fix, they
    referenced an undefined module-level `cfg` and silently failed
    via `except Exception`.
    """

    def test_save_ds_assignments_persists_zones_and_subs(
        self, seeded_db, assignments_tab,
    ):
        from unittest.mock import patch
        import config as _config
        import storm

        ws, tab_name = assignments_tab

        # Point the guild's DS-assignments tab at our test tab so the
        # writer lands on the worksheet we just created.
        gcfg = _config.get_config(TEST_GUILD_ID)
        gcfg.tab_ds_assignments = tab_name
        _config.save_config(gcfg)

        sh = _make_real_spreadsheet()
        zones = {
            "Nuclear Silo":   "Alice, Bob",
            "Oil Refinery I": "Carol",
        }
        subs = [("Carol", "Dave")]

        with patch.object(storm, "_get_spreadsheet", return_value=sh):
            storm.save_ds_assignments("A", zones, subs, guild_id=TEST_GUILD_ID)

        time.sleep(1.0)
        rows = ws.get_all_values()
        flat = [c for r in rows for c in r]

        # Section headers
        assert "DS_A_ZONES" in flat
        assert "DS_A_SUBS"  in flat
        # Zone data
        assert any("Nuclear Silo"   == c for c in flat)
        assert any("Alice, Bob"     == c for c in flat)
        # Sub data
        assert any("Carol" == c for c in flat)
        assert any("Dave"  == c for c in flat)

    def test_save_cs_assignments_persists_zones(
        self, seeded_db, assignments_tab,
    ):
        from unittest.mock import patch
        import config as _config
        import storm

        ws, tab_name = assignments_tab

        gcfg = _config.get_config(TEST_GUILD_ID)
        gcfg.tab_ds_assignments = tab_name  # CS shares the DS-assignments tab
        _config.save_config(gcfg)

        sh    = _make_real_spreadsheet()
        zones = {
            "s1_power_tower": "Alice, Bob",
            "s2_ds1":         "Carol, Dave",
        }

        with patch.object(storm, "_get_spreadsheet", return_value=sh):
            storm.save_cs_assignments("A", zones, guild_id=TEST_GUILD_ID)

        time.sleep(1.0)
        rows = ws.get_all_values()
        flat = [c for r in rows for c in r]

        assert "CS_A_ZONES"             in flat
        assert any("s1_power_tower" == c for c in flat)
        assert any("Alice, Bob"     == c for c in flat)


# ── train.save_schedule ──────────────────────────────────────────────────────

class TestTrainScheduleWrite:
    """The bot writes the full train schedule back to the configured
    `tab_train_schedule` whenever leadership uses `/train` to add /
    update / clear entries. save_schedule is the round-trip writer."""

    def test_save_schedule_writes_all_entries(
        self, seeded_db, train_tab,
    ):
        from unittest.mock import patch
        import config as _config
        import train

        ws, tab_name = train_tab

        # Point the guild's train-schedule tab at our test tab.
        gcfg = _config.get_config(TEST_GUILD_ID)
        gcfg.tab_train_schedule = tab_name
        _config.save_config(gcfg)

        schedule = {
            "2026-04-14": {
                "name": "Alice", "theme": "Birthday",
                "tone": "Casual", "notes": "Turning 30",
                "prompt_retrieved": True,
            },
            "2026-04-15": {
                "name": "Bob", "theme": "Milestone",
                "tone": "Default", "notes": "",
                "prompt_retrieved": False,
            },
        }

        # train.py auth/opens the sheet via a single _get_train_sheet
        # helper (no separate _get_spreadsheet like the other modules).
        # Patching it directly gives save_schedule our test worksheet.
        with patch.object(train, "_get_train_sheet", return_value=ws):
            train.save_schedule(schedule, guild_id=TEST_GUILD_ID)

        time.sleep(1.0)
        rows = ws.get_all_values()

        # Header row should be intact (we pre-seeded it; save_schedule
        # batch-clears A2:F1000 then writes from A2 down — header stays).
        assert rows[0][:6] == ["Date", "Name", "Theme", "Tone", "Notes", "Prompt Retrieved"]

        # Two data rows in date order.
        data = [r for r in rows[1:] if any(r)]
        assert len(data) == 2
        assert data[0][:6] == ["2026-04-14", "Alice", "Birthday", "Casual", "Turning 30", "TRUE"]
        assert data[1][:6] == ["2026-04-15", "Bob", "Milestone", "Default", "", "FALSE"]

    def test_save_schedule_replaces_previous_rows(
        self, seeded_db, train_tab,
    ):
        """Round-trip the writer twice with different schedules — the
        second write should fully replace the first (batch_clear of
        A2:F1000) so no leftovers remain."""
        from unittest.mock import patch
        import config as _config
        import train

        ws, tab_name = train_tab

        gcfg = _config.get_config(TEST_GUILD_ID)
        gcfg.tab_train_schedule = tab_name
        _config.save_config(gcfg)

        first = {
            "2026-04-14": {"name": "Alice", "theme": "X",  "tone": "Y", "notes": "first"},
            "2026-04-15": {"name": "Bob",   "theme": "",   "tone": "",  "notes": ""},
        }
        second = {
            "2026-05-01": {"name": "Carol", "theme": "Z",  "tone": "W", "notes": "second"},
        }

        with patch.object(train, "_get_train_sheet", return_value=ws):
            train.save_schedule(first,  guild_id=TEST_GUILD_ID)
            time.sleep(0.6)
            train.save_schedule(second, guild_id=TEST_GUILD_ID)

        time.sleep(1.0)
        rows = ws.get_all_values()
        data = [r for r in rows[1:] if any(r)]

        # Only Carol should remain — Alice + Bob got cleared.
        assert len(data) == 1
        assert data[0][1] == "Carol"
        assert data[0][4] == "second"


# ── member_roster.write_roster ───────────────────────────────────────────────

class TestMemberRosterWrite:
    """write_roster builds rows from a discord.Guild's members list and
    rewrites the configured roster tab from scratch (clear + update).
    This is the function that powers Member Roster Sync (Premium)."""

    def test_write_roster_writes_header_and_member_rows(
        self, seeded_db, roster_tab,
    ):
        from unittest.mock import MagicMock, patch
        from datetime import datetime, timezone
        import config as _config
        import member_roster

        ws, tab_name = roster_tab

        # Configure roster sync to write to our test tab. Default column
        # layout: 0=Discord ID, 1=Name, 2=Display Name, 3=Joined, 4=Roles.
        _config.save_member_roster_config(
            TEST_GUILD_ID,
            enabled=1, tab_name=tab_name,
            discord_id_col=0, name_col=1, display_col=2,
            joined_col=3, roles_col=4,
            role_filter_id=0, auto_sync=1,
        )

        # Build a fake guild with three members — one bot (filtered out),
        # one regular member, one with a role we can verify renders.
        def _member(uid, name, display, joined, role_names, is_bot=False):
            m = MagicMock()
            m.id, m.name, m.display_name = uid, name, display
            m.bot                        = is_bot
            m.joined_at                  = joined
            m.roles                      = [MagicMock(name=r, id=99) for r in role_names]
            # MagicMock's .name kwarg doesn't stick — set explicitly:
            for role, role_name in zip(m.roles, role_names):
                role.name = role_name
            return m

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [
            _member(111, "alice",  "Alice",  datetime(2025, 1, 5,  tzinfo=timezone.utc), ["@everyone", "Member"]),
            _member(222, "bob",    "Bob",    datetime(2025, 3, 12, tzinfo=timezone.utc), ["@everyone", "Member", "Leadership"]),
            _member(333, "rogue",  "Rogue",  datetime(2025, 6, 1,  tzinfo=timezone.utc), ["@everyone"], is_bot=True),
        ]

        sh = _make_real_spreadsheet()
        with patch.object(member_roster, "get_member_roster_sheet", return_value=ws):
            written, _report = member_roster.write_roster(guild, _config.get_member_roster_config(TEST_GUILD_ID))

        # write_roster returns the count excluding header. Two non-bots.
        assert written == 2

        time.sleep(1.0)
        rows = ws.get_all_values()

        # Header
        assert rows[0][:5] == ["Discord ID", "Name", "Display Name", "Joined", "Roles"]

        # Two member rows, sorted by display_name lower → Alice, Bob
        data = [r for r in rows[1:] if any(r)]
        assert len(data) == 2
        assert data[0][0] == "111"          # Alice's id
        assert data[0][1] == "alice"
        assert data[0][2] == "Alice"
        assert data[0][3] == "2025-01-05"
        assert "Member" in data[0][4]
        assert "Leadership" not in data[0][4]

        assert data[1][0] == "222"          # Bob's id
        assert data[1][2] == "Bob"
        assert "Leadership" in data[1][4]   # Bob has the role
        # Bot 333 should be excluded
        assert all("333" not in r[0] for r in data)


# ── train_birthdays integration: birthday auto-add → train schedule ──────────

class TestBirthdayToTrainScheduleIntegration:
    """When birthdays + train integration are both configured, the
    daily task adds upcoming birthdays directly into the train schedule
    sheet. This exercises that full pipeline end-to-end against a real
    Train Schedule tab — confirming the new row lands with the right
    name, theme="Birthday", and date."""

    def test_birthday_added_to_train_schedule_lands_in_sheet(
        self, seeded_db, train_tab,
    ):
        from unittest.mock import patch
        from datetime import date, timedelta
        import config as _config
        import train
        import train_birthdays

        ws, tab_name = train_tab

        # Wire the train-schedule tab to our test tab.
        gcfg = _config.get_config(TEST_GUILD_ID)
        gcfg.tab_train_schedule = tab_name
        _config.save_config(gcfg)

        # Birthdays config — train_integration=1 so the helper will
        # actually add to the schedule. lookahead_days=14 by default.
        _config.save_birthday_config(
            guild_id           = TEST_GUILD_ID,
            tab_name           = "_unused_birthdays",
            name_col           = 0,
            birthday_col       = 1,
            data_start_row     = 2,
            enabled            = 1,
            train_integration  = 1,
            flexible_placement = 1,
        )

        # A birthday three days from today, so it falls inside the
        # 14-day lookahead window.
        target_date = date.today() + timedelta(days=3)
        members = [{
            "name":  "BdayPerson",
            "month": target_date.month,
            "day":   target_date.day,
        }]

        # Both load_schedule and save_schedule go through
        # train._get_train_sheet — patching it once covers the round-trip.
        with patch.object(train, "_get_train_sheet", return_value=ws), \
             patch.object(train_birthdays, "load_birthdays", return_value=members):

            # Existing schedule starts empty. The helper computes the
            # adds, the cog persists them via save_schedule.
            current   = train.load_schedule(TEST_GUILD_ID)
            updated, alerts = train_birthdays.check_and_add_birthdays(
                current, guild_id=TEST_GUILD_ID,
            )
            assert not alerts, f"Did not expect placement alerts; got {alerts}"
            train.save_schedule(updated, guild_id=TEST_GUILD_ID)

        time.sleep(1.0)
        rows = ws.get_all_values()
        data = [r for r in rows[1:] if any(r)]

        # The birthday should have produced exactly one row in the
        # configured train tab.
        assert len(data) == 1, (
            f"Expected one birthday row in the train schedule; got {len(data)}: {data}"
        )
        assert data[0][0] == target_date.isoformat()
        assert data[0][1] == "BdayPerson"
        assert data[0][2] == "Birthday"



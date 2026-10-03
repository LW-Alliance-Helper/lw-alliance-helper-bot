"""The storm Member Log and Participation Log, built by the bot's own writers
and left formatted, for Kevin to open (#729).

Each test rebuilds one tab in the test spreadsheet from scratch through the
functions the bot calls in production. The Member Log starts as a tab from
before #729 (question keys for headers, lowercase codes), so the run shows
the bot adopting it. Mock data only: Alpha, Bravo, Charlie, alliance ABC.
"""

from datetime import date
from unittest.mock import patch

import pytest

import sheet_format
import sheet_identity
import storm_attendance
import storm_log
from tests.conftest import TEST_GUILD_ID

pytestmark = pytest.mark.sheets

MEMBERS = [
    ("100000000000000001", "Alpha"),
    ("100000000000000002", "Bravo"),
    ("100000000000000003", "Charlie"),
]

QUESTIONS = [
    {"key": "showed_up", "label": "Did this member show up?", "type": "roster_multi_select"},
    {"key": "sat_out", "label": "Who sat out this week?", "type": "roster_multi_select"},
    {"key": "sit_out_count_4", "label": "Sit-outs in past 4 events", "type": "derived_count"},
    {"key": "votes", "label": "How many votes?", "type": "numeric"},
]


def _fresh(sh, title: str):
    """Delete the previous run's tab, so every run starts from nothing."""
    for ws in sh.worksheets():
        if ws.title == title:
            sh.del_worksheet(ws)


def _meta(sh, title: str) -> dict:
    fields = (
        "sheets(properties(gridProperties(frozenRowCount,frozenColumnCount)),basicFilter(range),"
        "data(columnMetadata(hiddenByUser),"
        "rowData(values(userEnteredFormat(backgroundColor,textFormat(bold,fontSize))))))"
    )
    return sh.fetch_sheet_metadata(params={"ranges": f"'{title}'!A1:H2", "fields": fields})[
        "sheets"
    ][0]


def _env(sh, monkeypatch, *, member_log: str = "", participation: str = ""):
    monkeypatch.setattr(sheet_format, "_met", set())
    monkeypatch.setattr(sheet_identity, "_found", {})
    monkeypatch.setattr(storm_log, "_tags_seen", {})
    roster = sheet_identity.build_roster([(did, name, "") for did, name in MEMBERS])
    pcfg = {"tab_name": participation, "questions": list(QUESTIONS)}
    return (
        patch("config.get_spreadsheet", return_value=sh),
        patch("storm_log._get_spreadsheet", return_value=sh),
        patch("storm_log._member_log_tab_name", return_value=member_log),
        patch("config.get_participation_config", return_value=pcfg),
        patch("sheet_identity.load_roster", return_value=roster),
        patch("sheet_identity.settings_for", return_value=sheet_identity.Settings()),
    )


def test_preview_member_log(seeded_db, test_spreadsheet, monkeypatch):
    title = "Preview: DS Member Log"
    sh = test_spreadsheet
    _fresh(sh, title)

    # A tab as the bot wrote it before #729: keys for headers, codes in cells.
    old = sh.add_worksheet(title=title, rows=200, cols=8)
    old.update(
        "A1",
        [
            ["Event Date", "Member", "sat_out", "showed_up"],
            ["2026-09-20", "Alpha", "yes", "yes"],
            ["2026-09-20", "Bravo", "no", ""],
            ["2026-09-20", "Charlie", "no", "yes"],
        ],
        value_input_option="USER_ENTERED",
    )

    patches = _env(sh, monkeypatch, member_log=title)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        # The participation log, then attendance, for the next event.
        storm_log.upsert_member_log_rows(
            TEST_GUILD_ID,
            "DS",
            date(2026, 9, 27),
            {
                "Alpha": {"sat_out": "no", "sit_out_count_4": "0"},
                "Bravo": {"sat_out": "yes", "sit_out_count_4": "2"},
                "Charlie": {"sat_out": "no", "sit_out_count_4": "1"},
            },
            ["sat_out", "sit_out_count_4"],
        )
        storm_attendance.save_attendance(
            TEST_GUILD_ID,
            "DS",
            "2026-09-27",
            statuses={("A", "Power Tower", "Alpha"): "attended", ("A", "", "Charlie"): ""},
            officer_id=0,
        )

        ws = sh.worksheet(title)
        # The writers formatted the tab on its first write; a second pass is a no-op.
        assert sheet_format.ensure_formatted(ws, storm_log.MEMBER_LOG_READ) is False

        rows = ws.get_all_values()
        assert rows[0][:5] == [
            "Event Date",
            "Member",
            "Who sat out this week?",
            "Did this member show up?",
            "Sit-outs in past 4 events",
        ]
        assert rows[0][5] == "Discord ID"
        # The old rows, reworded; then Bravo's 2026-09-27 row, kept from the
        # participation write; then Alpha's and Charlie's, rewritten by
        # attendance with their answers merged in (#714).
        assert [r[1] for r in rows[1:]] == [
            "Alpha",
            "Bravo",
            "Charlie",
            "Bravo",
            "Alpha",
            "Charlie",
        ]
        assert [r[2] for r in rows[1:]] == ["Yes", "No", "No", "Yes", "No", "No"]
        assert [r[3] for r in rows[1:]] == ["Yes", "", "Yes", "", "Yes", ""]  # blank, deliberately
        assert [r[4] for r in rows[4:]] == ["2", "0", "1"]
        assert rows[5][5] == "100000000000000001"
        # The Event Date column shows the locale's format, not the ISO text...
        assert rows[1][0] != "2026-09-20"

        # ...and the bot's readers still read it back exactly.
        dates, sat_out = storm_log.read_member_log_window(TEST_GUILD_ID, "DS", 4, "sat_out")
        assert dates == ["2026-09-27", "2026-09-20"]
        assert sat_out["Bravo"] == {"2026-09-20": "No", "2026-09-27": "Yes"}
        counts = storm_log.count_member_flags_in_window(TEST_GUILD_ID, "DS", 4, "sat_out")
        assert counts == {"Alpha": 1, "Bravo": 1, "Charlie": 0}
        flags = storm_attendance._read_member_log_for_date(TEST_GUILD_ID, "DS", "2026-09-27")
        assert flags == {"Alpha": "Yes", "Bravo": "", "Charlie": ""}

    meta = _meta(sh, title)
    grid = meta["properties"]["gridProperties"]
    assert (grid.get("frozenRowCount"), grid.get("frozenColumnCount")) == (1, 2)
    assert "basicFilter" not in meta  # filters are the alliance's to add
    header = meta["data"][0]["rowData"][0]["values"][0]["userEnteredFormat"]
    assert header["textFormat"]["bold"] is True and header["textFormat"]["fontSize"] == 14
    hidden = [bool(c.get("hiddenByUser")) for c in meta["data"][0]["columnMetadata"]]
    assert hidden[5] is True  # Discord ID, hidden by default


def test_preview_participation_log(seeded_db, test_spreadsheet, monkeypatch):
    title = "Preview: DS Participation Log"
    sh = test_spreadsheet
    _fresh(sh, title)

    patches = _env(sh, monkeypatch, participation=title)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        storm_log.append_participation_row(
            TEST_GUILD_ID, "DS", date(2026, 9, 20), {"votes": "18", "sat_out": "1 member(s)"}
        )
        ws = sh.worksheet(title)
        # A row as the bot wrote it before #729, kept as the text it was.
        ws.append_row(["9/24/2026", "CS", "", "2 member(s)", "", "12"], value_input_option="RAW")
        storm_log.append_participation_row(
            TEST_GUILD_ID, "DS", date(2026, 9, 27), {"votes": "20", "sat_out": "1 member(s)"}
        )

        assert sheet_format.ensure_formatted(ws, storm_log.PARTICIPATION_LOG_READ) is False
        rows = ws.get_all_values()
        assert rows[0][:2] == ["Date", "Event"]
        assert [r[1] for r in rows[1:]] == ["Desert Storm", "CS", "Desert Storm"]
        assert rows[1][0] != "2026-09-20"  # a real date, in the locale's format

        assert storm_log.list_recent_log_dates("DS", 5, guild_id=TEST_GUILD_ID) == [
            date(2026, 9, 27),
            date(2026, 9, 20),
        ]
        assert storm_log.list_recent_log_dates("CS", 5, guild_id=TEST_GUILD_ID) == [
            date(2026, 9, 24)
        ]
        entry = storm_log.lookup_log_entry("DS", date(2026, 9, 20), guild_id=TEST_GUILD_ID)
        assert ("How many votes?", "18") in entry["fields"]

    meta = _meta(sh, title)
    grid = meta["properties"]["gridProperties"]
    assert (grid.get("frozenRowCount"), grid.get("frozenColumnCount")) == (1, 2)
    assert "basicFilter" not in meta

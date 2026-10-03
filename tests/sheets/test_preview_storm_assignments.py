"""The DS Assignments tab built by the bot's own writers, for Kevin to open (#729).

The tab starts in the stacked layout from before #729 (marker rows, Canyon
Storm keyed by internal keys), so the first save shows the conversion: the
tab ends as one table (Event, Team, Stage, Zone, Members) in the house style.
Named "Preview: ..." so the session scrub leaves it in place. Mock data only:
Alpha, Bravo, alliance ABC.
"""

from unittest.mock import patch

import pytest

import config
import sheet_format
import storm
from tests.conftest import TEST_GUILD_ID

pytestmark = pytest.mark.sheets

TITLE = "Preview: DS Assignments"
HEADER = ["Event", "Team", "Stage", "Zone", "Members"]

STACKED = [
    ["DS_A_ZONES", ""],
    ["Nuclear Silo", "Alpha"],
    ["", ""],
    ["DS_A_SUBS", ""],
    ["Bravo"],
    ["", ""],
    ["CS_B_ZONES", ""],
    ["s1_power_tower", "Charlie"],
    ["s3_power_tower", "Delta"],
    ["s3_pop_pair1", "Echo, Foxtrot"],
    ["", ""],
]


def _fresh(sh, title: str):
    for ws in sh.worksheets():
        if ws.title == title:
            sh.del_worksheet(ws)


def _meta(sh, title: str) -> dict:
    fields = (
        "sheets(properties(gridProperties(frozenRowCount,frozenColumnCount)),basicFilter(range),"
        "data(rowData(values(userEnteredFormat(backgroundColor,textFormat(bold,fontSize)),"
        "dataValidation(condition(type,values))))))"
    )
    return sh.fetch_sheet_metadata(params={"ranges": f"'{title}'!A1:E2", "fields": fields})[
        "sheets"
    ][0]


def test_preview_ds_assignments(seeded_db, test_spreadsheet, monkeypatch):
    sh = test_spreadsheet
    _fresh(sh, TITLE)
    monkeypatch.setattr(sheet_format, "_met", set())
    ws = sh.add_worksheet(title=TITLE, rows=100, cols=8)
    ws.update(values=STACKED, range_name="A1")

    gcfg = config.get_config(TEST_GUILD_ID)
    gcfg.tab_ds_assignments = TITLE
    config.save_config(gcfg)

    with patch.object(storm, "_get_spreadsheet", return_value=sh):
        # The old layout reads before anything is saved.
        assert storm.load_ds_assignments("A", TEST_GUILD_ID) == (
            {"Nuclear Silo": "Alpha"},
            ["Bravo"],
        )

        # The first save converts the tab; Canyon Storm Team B comes across.
        storm.save_ds_assignments(
            "A",
            {"Nuclear Silo": "Alpha, Golf", "Arsenal": "Hotel", "Science Hub": ""},
            ["Bravo", "India"],
            guild_id=TEST_GUILD_ID,
        )
        storm.save_ds_assignments("B", {"Info Center": "Juliet"}, ["Kilo"], guild_id=TEST_GUILD_ID)
        storm.save_cs_assignments(
            "A",
            {
                "s1_power_tower": "Lima",
                "s1_dc1": "Mike, November",
                "s2_sf1": "Oscar",
                "s3_power_tower": "Papa",
                "s3_virus_lab": "Quebec",
                storm.CS_SUBS_KEY: ["Romeo", "Sierra"],
            },
            guild_id=TEST_GUILD_ID,
        )

        ds_a = storm.load_ds_assignments("A", TEST_GUILD_ID)
        cs_a = storm.load_cs_assignments("A", TEST_GUILD_ID)
        cs_b = storm.load_cs_assignments("B", TEST_GUILD_ID)

    ws = sh.worksheet(TITLE)
    # The writers formatted the tab on its first write; a second pass is a no-op.
    assert sheet_format.ensure_formatted(ws, storm.ASSIGNMENTS_FORMAT) is False

    rows = [r[:5] for r in ws.get_all_values() if any(r)]
    assert rows[0] == HEADER
    assert not any(storm._is_section_header(r[0]) for r in rows)
    assert ["Desert Storm", "A", "", "Nuclear Silo", "Alpha, Golf"] in rows
    assert ["Desert Storm", "A", "", "Subs", "India"] in rows
    assert ["Desert Storm", "B", "", "Info Center", "Juliet"] in rows
    assert ["Canyon Storm", "A", "1", "Power Tower", "Lima"] in rows
    assert ["Canyon Storm", "A", "3", "Power Tower", "Papa"] in rows
    assert ["Canyon Storm", "A", "", "Subs", "Sierra"] in rows
    assert ["Canyon Storm", "B", "3", "Power Tower", "Delta"] in rows  # converted, not saved
    assert ["Canyon Storm", "B", "", "Subs", "Foxtrot"] in rows

    # The bot's readers read the table back to the same keys as before.
    assert ds_a == (
        {"Nuclear Silo": "Alpha, Golf", "Arsenal": "Hotel", "Science Hub": ""},
        ["Bravo", "India"],
    )
    assert cs_a["s1_dc1"] == "Mike, November" and cs_a["s3_power_tower"] == "Papa"
    assert cs_a[storm.CS_SUBS_KEY] == ["Romeo", "Sierra"]
    assert cs_b == {
        "s1_power_tower": "Charlie",
        "s3_power_tower": "Delta",
        storm.CS_SUBS_KEY: ["Echo", "Foxtrot"],
    }

    meta = _meta(sh, TITLE)
    grid = meta["properties"]["gridProperties"]
    assert grid.get("frozenRowCount") == 1
    assert "basicFilter" not in meta
    header = meta["data"][0]["rowData"][0]["values"][0]["userEnteredFormat"]
    assert header["textFormat"]["bold"] is True and header["textFormat"]["fontSize"] == 14
    body = meta["data"][0]["rowData"][1]["values"]
    event_rule = body[0]["dataValidation"]["condition"]
    assert [v["userEnteredValue"] for v in event_rule["values"]] == ["Desert Storm", "Canyon Storm"]
    team_rule = body[1]["dataValidation"]["condition"]
    assert [v["userEnteredValue"] for v in team_rule["values"]] == ["A", "B"]

"""The shared DS/CS assignments tab: each save rewrites only its own team.

#683: the Desert Storm loader stopped a section only at a `DS_` header, so
Team B's subs ran on into the Canyon Storm sections below, and a Desert Storm
save wrote the Canyon Storm rows back as Team B subs. #678: the saves rebuilt
the tab from loaders that fall back to defaults on a failed read, so a Google
error during a save overwrote the other team with the defaults.

#729: the tab is a table (Event, Team, Stage, Zone, Members) with the game's
zone names, not stacked sections keyed by internal codes. The readers take
both layouts; the first save converts a stacked tab.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import storm  # noqa: E402

HEADER = ["Event", "Team", "Stage", "Zone", "Members"]

# The stacked layout from before #729.
TAB = [
    ["DS_A_ZONES", ""],
    ["Zone 1", "Alpha"],
    ["", ""],
    ["DS_A_SUBS", ""],
    ["Sub Alpha"],
    ["", ""],
    ["DS_B_ZONES", ""],
    ["Zone 1", "Bravo"],
    ["", ""],
    ["DS_B_SUBS", ""],
    ["Sub Bravo"],
    ["", ""],
    ["CS_A_ZONES", ""],
    ["s1_power_tower", "Charlie"],
    ["", ""],
    ["CS_B_ZONES", ""],
    ["s1_power_tower", "Delta"],
    ["", ""],
]

# The same assignments as a table.
TABLE = [
    HEADER,
    ["Desert Storm", "A", "", "Zone 1", "Alpha"],
    ["Desert Storm", "A", "", "Subs", "Sub Alpha"],
    ["Desert Storm", "B", "", "Zone 1", "Bravo"],
    ["Desert Storm", "B", "", "Subs", "Sub Bravo"],
    ["Canyon Storm", "A", "1", "Power Tower", "Charlie"],
    ["Canyon Storm", "B", "1", "Power Tower", "Delta"],
]


def _sheet(rows=None, read_error=None):
    ws = MagicMock()
    if read_error is not None:
        ws.get_all_values.side_effect = read_error
    else:
        ws.get_all_values.return_value = [list(r) for r in (rows if rows is not None else TAB)]
    sh = MagicMock()
    sh.worksheet.return_value = ws
    return ws, sh


def _patched(sh):
    return (
        patch.object(storm, "_get_spreadsheet", return_value=sh),
        patch("config.get_config", return_value=None),
        patch.object(storm, "_note_assignments_ok"),
        patch.object(storm, "_note_assignments_error"),
    )


def _grid(ws):
    """The grid as written, without the blank rows and trailing blank cells
    that square it off over the old contents."""
    ws.update.assert_called_once()
    assert ws.update.call_args.kwargs["value_input_option"] == "USER_ENTERED"
    out = []
    for row in ws.update.call_args.args[1]:
        row = list(row)
        while row and row[-1] == "":
            row.pop()
        if row:
            out.append(row)
    return out


def _padded(rows, width=5):
    return [list(r) + [""] * (width - len(r)) for r in rows]


def _save_then_reread(ws):
    """Make the next read return what the last save wrote."""
    ws.get_all_values.return_value = [list(r) for r in ws.update.call_args.args[1]]
    ws.update.reset_mock()


# ── Reading the old stacked layout ───────────────────────────────────────────


def test_ds_team_b_stops_at_the_canyon_storm_header():
    ws, sh = _sheet()
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        _zones, subs = storm.load_ds_assignments("B")
    assert subs == ["Sub Bravo"]


def test_an_old_stacked_tab_reads_as_before():
    stacked = TAB[:12] + [
        ["CS_A_ZONES", ""],
        ["s1_power_tower", "Charlie"],
        ["s3_power_tower", "Golf"],
        ["s3_pop_pair1", "Hotel, India - Juliet"],
        ["", ""],
    ]
    ws, sh = _sheet(rows=stacked)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        ds = storm.load_ds_assignments("A")
        cs = storm.load_cs_assignments("A")
    assert ds == ({"Zone 1": "Alpha"}, ["Sub Alpha"])
    assert cs == {
        "s1_power_tower": "Charlie",
        "s3_power_tower": "Golf",
        "s3_pop_pair1": ["Hotel", "India", "Juliet"],
    }


def test_a_legacy_two_column_sub_row_keeps_the_sub():
    rows = [["DS_A_ZONES", ""], ["Zone 1", "Alpha"], ["DS_A_SUBS", ""], ["Starter", "Sub Kilo"]]
    ws, sh = _sheet(rows=rows)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        _zones, subs = storm.load_ds_assignments("A")
    assert subs == ["Sub Kilo"]


# ── Reading the table ────────────────────────────────────────────────────────


def test_the_table_reads_the_same_as_the_stacked_tab():
    for rows in (TAB, TABLE):
        ws, sh = _sheet(rows=rows)
        a, b, c, d = _patched(sh)
        with a, b, c, d:
            assert storm.load_ds_assignments("B") == ({"Zone 1": "Bravo"}, ["Sub Bravo"])
            assert storm.load_cs_assignments("A") == {"s1_power_tower": "Charlie"}


def test_the_table_reads_any_case_and_the_old_codes():
    rows = [
        HEADER,
        ["desert storm", "team a", "", "Nuclear Silo", "Alpha"],
        ["DS", "a", "", "subs", "Bravo"],
        ["canyon storm", "A", "3", "power tower", "Charlie"],
        ["CS", "A", "", "s2_sf1", "Delta"],  # an old internal key in Zone
        ["Canyon Storm", "A", "", "Virus Lab", "Echo"],  # one stage has it
        ["Canyon Storm", "A", "", "Pop Pairs", "Foxtrot"],  # the old subs label
    ]
    ws, sh = _sheet(rows=rows)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        assert storm.load_ds_assignments("A") == ({"Nuclear Silo": "Alpha"}, ["Bravo"])
        assert storm.load_cs_assignments("A") == {
            "s3_power_tower": "Charlie",
            "s2_sf1": "Delta",
            "s3_virus_lab": "Echo",
            "s3_pop_pair1": ["Foxtrot"],
        }


def test_a_zone_that_is_not_the_games_reads_as_typed_for_the_typo_warning():
    rows = [
        HEADER,
        ["Canyon Storm", "B", "1", "Powr Tower", "Golf"],
        ["Canyon Storm", "B", "", "Power Tower", "Hotel"],  # two stages have it
    ]
    ws, sh = _sheet(rows=rows)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        zones = storm.load_cs_assignments("B")
    assert storm._non_canonical_cs_zones(zones) == {"Powr Tower": "Golf", "Power Tower": "Hotel"}


# ── Writing the table ────────────────────────────────────────────────────────


def test_a_save_writes_the_table():
    ws, sh = _sheet(rows=[])
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments(
            "A", {"Nuclear Silo": "Alpha, Bravo", "Arsenal": ""}, ["Charlie", ("x", "Delta")]
        )
    assert _grid(ws) == [
        HEADER,
        ["Desert Storm", "A", "", "Nuclear Silo", "Alpha, Bravo"],
        ["Desert Storm", "A", "", "Arsenal"],
        ["Desert Storm", "A", "", "Subs", "Charlie"],
        ["Desert Storm", "A", "", "Subs", "Delta"],
    ]
    ws.clear.assert_not_called()


def test_canyon_storm_writes_the_games_zone_names_with_the_stage():
    ws, sh = _sheet(rows=[])
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_cs_assignments(
            "B",
            {
                "s1_power_tower": "Alpha",
                "s3_power_tower": ["Bravo", "Charlie"],
                "s3_pop_pair1": ["Delta", "Echo"],
            },
        )
    assert _grid(ws) == [
        HEADER,
        ["Canyon Storm", "B", "1", "Power Tower", "Alpha"],
        ["Canyon Storm", "B", "3", "Power Tower", "Bravo, Charlie"],
        ["Canyon Storm", "B", "", "Subs", "Delta"],
        ["Canyon Storm", "B", "", "Subs", "Echo"],
    ]


def test_canyon_storm_keys_round_trip():
    zones = {key: f"Member {n}" for n, (_, key, _) in enumerate(storm.CS_ZONE_STRUCTURE)}
    zones[storm.CS_SUBS_KEY] = ["Alpha", "Bravo"]
    ws, sh = _sheet(rows=[])
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_cs_assignments("A", zones)
        _save_then_reread(ws)
        assert storm.load_cs_assignments("A") == zones
        # A team with no subs keeps its Subs row, so the key reads back empty.
        storm.save_cs_assignments("A", {"s1_dc1": "Alpha", storm.CS_SUBS_KEY: []})
        _save_then_reread(ws)
        assert storm.load_cs_assignments("A") == {"s1_dc1": "Alpha", storm.CS_SUBS_KEY: []}


def test_an_old_stacked_tab_is_converted_on_its_first_save():
    ws, sh = _sheet()
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("A", {"Zone 1": "Echo"}, ["Sub Echo"])
    assert _grid(ws) == [
        HEADER,
        ["Desert Storm", "A", "", "Zone 1", "Echo"],
        ["Desert Storm", "A", "", "Subs", "Sub Echo"],
        ["Desert Storm", "B", "", "Zone 1", "Bravo"],
        ["Desert Storm", "B", "", "Subs", "Sub Bravo"],
        ["Canyon Storm", "A", "1", "Power Tower", "Charlie"],
        ["Canyon Storm", "B", "1", "Power Tower", "Delta"],
    ]
    ws.clear.assert_not_called()


def test_a_canyon_storm_save_converts_and_keeps_desert_storm_and_the_other_team():
    ws, sh = _sheet()
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_cs_assignments("B", {"s1_power_tower": "Foxtrot"})
    grid = _grid(ws)
    assert grid[0] == HEADER
    assert ["Canyon Storm", "B", "1", "Power Tower", "Foxtrot"] in grid
    assert ["Canyon Storm", "A", "1", "Power Tower", "Charlie"] in grid
    assert ["Desert Storm", "A", "", "Zone 1", "Alpha"] in grid
    assert ["Desert Storm", "B", "", "Subs", "Sub Bravo"] in grid
    assert not any(storm._is_section_header(row[0]) for row in grid)


def test_a_desert_storm_save_keeps_the_canyon_storm_rows():
    ws, sh = _sheet(rows=TABLE)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("B", {"Zone 1": "Golf"}, [])
        _save_then_reread(ws)
        assert storm.load_cs_assignments("A") == {"s1_power_tower": "Charlie"}
        assert storm.load_cs_assignments("B") == {"s1_power_tower": "Delta"}
        assert storm.load_ds_assignments("A") == ({"Zone 1": "Alpha"}, ["Sub Alpha"])
        assert storm.load_ds_assignments("B") == ({"Zone 1": "Golf"}, [])
        # And the other way round.
        storm.save_cs_assignments("A", {"s2_ds1": "Hotel"})
        _save_then_reread(ws)
        assert storm.load_ds_assignments("B") == ({"Zone 1": "Golf"}, [])
        assert storm.load_cs_assignments("A") == {"s2_ds1": "Hotel"}


def test_a_desert_storm_only_tab_gains_no_canyon_storm_rows():
    ws, sh = _sheet(rows=TAB[:12])
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("B", {"Zone 1": "Golf"}, [])
    grid = _grid(ws)
    assert not any(row[0] == "Canyon Storm" for row in grid)
    assert ["Desert Storm", "A", "", "Zone 1", "Alpha"] in grid


def test_the_alliances_own_columns_and_rows_are_kept():
    rows = _padded(
        [
            HEADER + ["Notes"],
            ["Desert Storm", "A", "", "Zone 1", "Alpha", "hold the line"],
            ["Desert Storm", "A", "", "Zone 2", "Bravo", "gone next save"],
            ["Desert Storm", "A", "", "Subs", "Charlie", "on call"],
            ["Canyon Storm", "A", "1", "Power Tower", "Delta", "first in"],
            ["Reminder", "", "", "", "", "check the times"],
        ],
        width=6,
    )
    ws, sh = _sheet(rows=rows)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("A", {"Zone 1": "Echo"}, ["Charlie", "Foxtrot"])
    assert _grid(ws) == [
        HEADER + ["Notes"],
        ["Desert Storm", "A", "", "Zone 1", "Echo", "hold the line"],
        ["Desert Storm", "A", "", "Subs", "Charlie", "on call"],
        ["Desert Storm", "A", "", "Subs", "Foxtrot"],
        ["Canyon Storm", "A", "1", "Power Tower", "Delta", "first in"],
        ["Reminder", "", "", "", "", "check the times"],
    ]


def test_a_header_an_officer_renamed_stays_theirs():
    rows = [["Event", "Team", "Stage", "Zone", "Players"], *TABLE[1:]]
    ws, sh = _sheet(rows=rows)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("A", {"Zone 1": "Echo"}, [])
    assert _grid(ws)[0] == ["Event", "Team", "Stage", "Zone", "Players"]


def test_a_deleted_header_row_comes_back_without_losing_the_first_row():
    ws, sh = _sheet(rows=TABLE[1:])
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        assert storm.load_ds_assignments("A") == ({"Zone 1": "Alpha"}, ["Sub Alpha"])
        storm.save_ds_assignments("B", {"Zone 1": "Golf"}, [])
    grid = _grid(ws)
    assert grid[0] == HEADER
    assert grid[1] == ["Desert Storm", "A", "", "Zone 1", "Alpha"]


def test_old_codes_in_rows_nobody_saved_are_reworded():
    rows = [HEADER, ["DS", "a", "", "Zone 1", "Alpha"], ["cs", "B", "1", "Power Tower", "Bravo"]]
    ws, sh = _sheet(rows=rows)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("B", {}, [])
    assert _grid(ws)[1:] == [
        ["Desert Storm", "A", "", "Zone 1", "Alpha"],
        ["Canyon Storm", "B", "1", "Power Tower", "Bravo"],
    ]


def test_a_write_formats_the_tab_once_with_dropdowns():
    ws, sh = _sheet(rows=TABLE)
    a, b, c, d = _patched(sh)
    with a, b, c, d, patch("sheet_format.ensure_formatted") as fmt:
        storm.save_ds_assignments("A", {"Zone 1": "Echo"}, [])
    fmt.assert_called_once_with(ws, storm.ASSIGNMENTS_FORMAT)
    dropdowns = dict(storm.ASSIGNMENTS_FORMAT.dropdowns)
    assert dropdowns[0] == ("Desert Storm", "Canyon Storm")
    assert dropdowns[1] == ("A", "B")
    assert "Nuclear Silo" in dropdowns[3] and "Virus Lab" in dropdowns[3]
    assert dropdowns[3][-1] == "Subs" and len(set(dropdowns[3])) == len(dropdowns[3])


@pytest.mark.parametrize(
    "save",
    [
        lambda: storm.save_ds_assignments("A", {"Zone 1": "Echo"}, []),
        lambda: storm.save_cs_assignments("A", {"s1_power_tower": "Echo"}),
    ],
)
def test_a_failed_read_writes_nothing(save):
    ws, sh = _sheet(read_error=RuntimeError("503"))
    a, b, c, d = _patched(sh)
    with a, b, c, d as note_error, patch("sheet_format.ensure_formatted") as fmt:
        save()
    ws.update.assert_not_called()
    ws.clear.assert_not_called()
    fmt.assert_not_called()
    note_error.assert_called_once()


def test_the_write_blanks_rows_the_new_tab_no_longer_reaches():
    """Team A had six zones and now has one: the tab gets shorter, and the
    rows at the bottom are written blank rather than left behind."""
    long_tab = [TAB[0]] + [[f"Zone {n}", "Alpha"] for n in range(1, 7)] + TAB[2:]
    ws, sh = _sheet(rows=long_tab)
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("A", {"Zone 1": "Echo"}, [])
    grid = ws.update.call_args.args[1]
    assert len(grid) >= len(long_tab)
    assert all("Zone 6" not in row for row in grid)
    assert all(cell == "" for cell in grid[-1])


def test_cover_previous_extent_pads_both_ways():
    from config import cover_previous_extent

    out = cover_previous_extent([["a"]], [["x", "y", "z"], ["x"], ["x"]])
    assert out == [["a", "", ""], ["", "", ""], ["", "", ""]]
    assert cover_previous_extent([], []) == []

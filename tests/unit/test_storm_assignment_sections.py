"""The shared DS/CS assignments tab: each save rewrites only its own team.

#683: the Desert Storm loader stopped a section only at a `DS_` header, so
Team B's subs ran on into the Canyon Storm sections below, and a Desert Storm
save wrote the Canyon Storm rows back as Team B subs. #678: the saves rebuilt
the tab from loaders that fall back to defaults on a failed read, so a Google
error during a save overwrote the other team with the defaults.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import storm  # noqa: E402

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


def _written(ws):
    """The sections as written, with the padding that squares the grid off
    trimmed from each row so they compare against what was read."""
    ws.update.assert_called_once()
    sections = storm._split_sections(ws.update.call_args.args[1])
    trimmed = {}
    for key, rows in sections.items():
        out = []
        for row in rows:
            row = list(row)
            while len(row) > 2 and row[-1] == "":
                row.pop()
            if len(row) == 2 and row[1] == "" and key.endswith("_SUBS"):
                row.pop()
            out.append(row)
        trimmed[key] = out
    return trimmed


def test_ds_team_b_stops_at_the_canyon_storm_header():
    ws, sh = _sheet()
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        _zones, subs = storm.load_ds_assignments("B")
    assert subs == ["Sub Bravo"]


def test_a_desert_storm_save_keeps_canyon_storm_exactly():
    ws, sh = _sheet()
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("A", {"Zone 1": "Echo"}, ["Sub Echo"])
    out = _written(ws)
    assert out["DS_A_ZONES"] == [["Zone 1", "Echo"]]
    assert out["DS_A_SUBS"] == [["Sub Echo"]]
    assert out["DS_B_ZONES"] == [["Zone 1", "Bravo"]]
    assert out["DS_B_SUBS"] == [["Sub Bravo"]]
    assert out["CS_A_ZONES"] == [["s1_power_tower", "Charlie"]]
    assert out["CS_B_ZONES"] == [["s1_power_tower", "Delta"]]
    ws.clear.assert_not_called()


def test_a_canyon_storm_save_keeps_desert_storm_and_the_other_team():
    ws, sh = _sheet()
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_cs_assignments("B", {"s1_power_tower": "Foxtrot"})
    out = _written(ws)
    assert out["CS_B_ZONES"] == [["s1_power_tower", "Foxtrot"]]
    assert out["CS_A_ZONES"] == [["s1_power_tower", "Charlie"]]
    assert out["DS_A_ZONES"] == [["Zone 1", "Alpha"]]
    assert out["DS_B_SUBS"] == [["Sub Bravo"]]


def test_a_desert_storm_only_tab_gains_no_canyon_storm_sections():
    ws, sh = _sheet(rows=TAB[:12])
    a, b, c, d = _patched(sh)
    with a, b, c, d:
        storm.save_ds_assignments("B", {"Zone 1": "Golf"}, [])
    out = _written(ws)
    assert "CS_A_ZONES" not in out and "CS_B_ZONES" not in out
    assert out["DS_A_ZONES"] == [["Zone 1", "Alpha"]]


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
    with a, b, c, d as note_error:
        save()
    ws.update.assert_not_called()
    ws.clear.assert_not_called()
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
    assert all(row[0] != "Zone 6" for row in grid)
    assert grid[-1] == ["", ""]


def test_cover_previous_extent_pads_both_ways():
    from config import cover_previous_extent

    out = cover_previous_extent([["a"]], [["x", "y", "z"], ["x"], ["x"]])
    assert out == [["a", "", ""], ["", "", ""], ["", "", ""]]
    assert cover_previous_extent([], []) == []

"""Unit tests for buddy.py — the Sheet I/O layer (#289).

A FakeWS models the gspread surface buddy.py uses (get_all_values /
batch_clear / update / update_cell / append_row). config.get_spreadsheet and
config.get_or_create_worksheet are patched to hand back FakeWS instances so
save → load round-trips through real module logic, and the single-cell
profession write can be asserted against sibling cells.
"""

from unittest.mock import patch

import pytest

import buddy
from buddy import Member, Pair, assign_buddies

GID = 999


class FakeWS:
    """In-memory worksheet. Row 0 is the header."""

    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def row_values(self, n):
        return list(self.rows[n - 1]) if 0 < n <= len(self.rows) else []

    def batch_clear(self, ranges):
        self.rows = self.rows[:1]  # keep header only

    def update(self, rng, values, value_input_option=None):
        start = 0 if str(rng).upper().startswith("A1") else 1
        self.rows = self.rows[:start] + [list(r) for r in values]

    def update_cell(self, row, col, value):
        while len(self.rows) < row:
            self.rows.append([])
        r = self.rows[row - 1]
        while len(r) < col:
            r.append("")
        r[col - 1] = value

    def append_row(self, row, value_input_option=None):
        self.rows.append(list(row))


@pytest.fixture
def sheets():
    """Patch the gspread client so buddy.py talks to shared FakeWS tabs.

    Yields a dict tab_name → FakeWS. Pre-seed Squad Powers here when a test
    needs profession formulas or profession reads/writes."""
    tabs: dict[str, FakeWS] = {}

    class FakeSpreadsheet:
        def worksheet(self, name):
            if name not in tabs:
                raise Exception(f"Worksheet {name} not found")
            return tabs[name]

    def fake_get_or_create(sh, tab_name, header_row=None, rows=None, cols=None):
        if tab_name not in tabs:
            tabs[tab_name] = FakeWS([list(header_row)] if header_row else [])
        return tabs[tab_name]

    with (
        patch("config.get_spreadsheet", return_value=FakeSpreadsheet()),
        patch("config.get_or_create_worksheet", side_effect=fake_get_or_create),
    ):
        yield tabs


def W(name, did):
    return Member(name=name, discord_id=did, profession=buddy.WAR_LEADER)


def E(name, did):
    return Member(name=name, discord_id=did, profession=buddy.ENGINEER)


def pair_keys(pairs):
    return {(p.wl_discord_id, p.eng_discord_id) for p in pairs}


# ── round-trip ────────────────────────────────────────────────────────────────


def test_save_then_load_roundtrips_links(sheets):
    members = [W("Wanda", "1"), W("Walt", "2"), E("Eve", "3"), E("Ed", "4")]
    result = assign_buddies(members, [])
    assert buddy.save_pairs(GID, "Buddies", result, "Squad Powers", "Profession") is True

    loaded = buddy.load_pairs(GID, "Buddies")
    assert pair_keys(loaded) == pair_keys(result.pairs)


def test_doubled_war_leader_renders_two_engineers_and_parses_back(sheets):
    members = [W("Walt", "1"), E("Eve", "3"), E("Ed", "4")]
    result = assign_buddies(members, [], engineer_doubling=True)
    assert len(result.pairs) == 2  # Walt receives both Engineers
    buddy.save_pairs(GID, "Buddies", result, "Squad Powers", "Profession")

    body = sheets["Buddies"].rows[1:]
    assert len(body) == 1  # single War-Leader row
    row = body[0]
    assert row[0] == "1"  # Walt in the left block
    assert row[3] == "3" or row[3] == "4"  # Engineer 1 in D-F
    assert row[6] in ("3", "4")  # Engineer 2 in G-I

    loaded = buddy.load_pairs(GID, "Buddies")
    assert pair_keys(loaded) == {("1", "3"), ("1", "4")}


def test_unpaired_engineer_in_middle_block_blank_left(sheets):
    members = [W("Walt", "1"), E("Eve", "3"), E("Ed", "4")]
    result = assign_buddies(members, [], engineer_doubling=False)
    assert len(result.unpaired_eng) == 1
    buddy.save_pairs(GID, "Buddies", result, "Squad Powers", "Profession")

    body = sheets["Buddies"].rows[1:]
    # One WL row + one unpaired-Engineer row.
    unpaired_rows = [r for r in body if not (r[0] or r[1]) and (r[3] or r[4])]
    assert len(unpaired_rows) == 1
    assert unpaired_rows[0][0] == ""  # blank left block = the "unpaired" signal

    # The unpaired Engineer produces no pair on load. "Ed" (4) sorts before
    # "Eve" (3), so Walt pairs with Ed and Eve is the leftover.
    loaded = buddy.load_pairs(GID, "Buddies")
    assert pair_keys(loaded) == {("1", "4")}


# ── profession formula ─────────────────────────────────────────────────────────


def test_profession_cells_are_formulas_not_static(sheets):
    sheets["Squad Powers"] = FakeWS([["Username", "Discord ID", "1st Squad Power", "Profession"]])
    members = [W("Walt", "1"), E("Eve", "3")]
    result = assign_buddies(members, [])
    buddy.save_pairs(GID, "Buddies", result, "Squad Powers", "Profession")

    row = sheets["Buddies"].rows[1]
    # Profession columns (C / F) carry live-lookup formulas, not "War Leader".
    assert row[2].startswith("=IFERROR(INDEX('Squad Powers'!$D:$D")
    assert "MATCH(A2" in row[2]
    assert row[5].startswith("=IFERROR(INDEX('Squad Powers'!$D:$D")
    assert "MATCH(D2" in row[5]


def test_profession_static_fallback_when_squad_powers_missing(sheets):
    # No Squad Powers tab seeded → columns unresolvable → static values.
    members = [W("Walt", "1"), E("Eve", "3")]
    result = assign_buddies(members, [])
    buddy.save_pairs(GID, "Buddies", result, "Squad Powers", "Profession")
    row = sheets["Buddies"].rows[1]
    assert row[2] == buddy.WAR_LEADER
    assert row[5] == buddy.ENGINEER


# ── single-cell profession write (anti-clobber) ───────────────────────────────


def test_write_profession_cell_updates_one_cell_only(sheets):
    sheets["Squad Powers"] = FakeWS(
        [
            ["Username", "Discord ID", "1st Squad Power", "Profession"],
            ["Wanda", "1", "123456789", "War Leader"],
        ]
    )
    ok = buddy.write_profession_cell(GID, "Squad Powers", "Profession", "1", "Wanda", "Engineer")
    assert ok is True
    row = sheets["Squad Powers"].rows[1]
    assert row == ["Wanda", "1", "123456789", "Engineer"]  # power untouched


def test_write_profession_cell_appends_bare_row_when_id_absent(sheets):
    sheets["Squad Powers"] = FakeWS([["Username", "Discord ID", "1st Squad Power", "Profession"]])
    ok = buddy.write_profession_cell(GID, "Squad Powers", "Profession", "777", "NewGuy", "Engineer")
    assert ok is True
    assert len(sheets["Squad Powers"].rows) == 2
    new = sheets["Squad Powers"].rows[1]
    assert new[0] == "NewGuy"
    assert new[1] == "777"
    assert new[3] == "Engineer"


# ── read professions by header (order-independent) ─────────────────────────────


def test_read_all_professions_locates_columns_by_header(sheets):
    # Columns deliberately reordered from the survey default.
    sheets["Squad Powers"] = FakeWS(
        [
            ["Profession", "Name", "Discord ID", "Power"],
            ["War Leader", "Wanda", "1", "100"],
            ["Engineer", "Eve", "3", "200"],
        ]
    )
    members = buddy.read_all_professions(GID, "Squad Powers", "Profession")
    by_id = {m.discord_id: m for m in members}
    assert by_id["1"].name == "Wanda"
    assert by_id["1"].profession == "War Leader"
    assert by_id["3"].profession == "Engineer"

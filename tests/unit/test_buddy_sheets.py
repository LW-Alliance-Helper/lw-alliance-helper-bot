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


def test_read_members_from_buddy_tab_position_implied(sheets):
    # Profession cells blank (formulas would render empty with no Squad Powers).
    sheets["Buddy System"] = FakeWS(
        [
            list(buddy.BUDDY_HEADER),
            ["1", "Walt", "", "3", "Eve", "", "", "", ""],
            ["", "", "", "5", "Zed", "", "", "", ""],  # unpaired Engineer row
        ]
    )
    members = buddy.read_members_from_buddy_tab(GID, "Buddy System")
    by = {m.discord_id: m for m in members}
    assert by["1"].profession == buddy.WAR_LEADER  # left block implies War Leader
    assert by["3"].profession == buddy.ENGINEER  # middle block implies Engineer
    assert by["5"].profession == buddy.ENGINEER  # extended middle = unpaired Engineer


def test_bootstrap_from_existing_buddy_tab_with_empty_squad_powers(sheets):
    # An alliance drops in their existing buddy list; Squad Powers is empty.
    sheets["Buddy System"] = FakeWS(
        [
            list(buddy.BUDDY_HEADER),
            ["1", "Walt", "", "3", "Eve", "", "", "", ""],
        ]
    )
    squad = buddy.read_all_professions(GID, "Squad Powers", "Profession")  # empty tab
    assert squad == []
    merged = buddy.merge_members(squad, buddy.read_members_from_buddy_tab(GID, "Buddy System"))
    pairs = buddy.load_pairs(GID, "Buddy System")
    result = assign_buddies(merged, pairs)
    # The imported pair survives even with no survey data.
    assert pair_keys(result.pairs) == {("1", "3")}
    assert not result.unpaired_wl and not result.unpaired_eng


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


# ── Opt-out column + from-scratch rebuild (#427) ─────────────────────────────


def test_include_column_absent_leaves_everyone_eligible(sheets):
    sheets["Squad Powers"] = FakeWS(
        [
            ["Discord ID", "Name", "Profession"],
            ["1", "Wanda", "War Leader"],
            ["3", "Eve", "Engineer"],
        ]
    )
    # Header configured but not present on the tab — must not exclude anyone.
    members = buddy.read_all_professions(GID, "Squad Powers", "Profession", "In Buddy System")
    assert [m.included for m in members] == [True, True]


@pytest.mark.parametrize("cell", ["no", "No", "FALSE", "0", " off ", "left", "exclude"])
def test_include_column_excludes_on_falsy_values(sheets, cell):
    sheets["Squad Powers"] = FakeWS(
        [
            ["Discord ID", "Name", "Profession", "In Buddy System"],
            ["1", "Wanda", "War Leader", cell],
            ["3", "Eve", "Engineer", ""],
        ]
    )
    members = buddy.read_all_professions(GID, "Squad Powers", "Profession", "In Buddy System")
    by_id = {m.discord_id: m for m in members}
    assert by_id["1"].included is False
    # A blank cell means "still in" — the opt-out has to be deliberate.
    assert by_id["3"].included is True


def test_excluded_member_is_not_resurrected_by_their_buddy_tab_row(sheets):
    """The #427 regression: blanking a departed member's Profession cell wasn't
    enough, because their leftover Buddies-tab row re-implied a profession and
    merge_members kept it. The exclusion must outrank the fallback."""
    sheets["Squad Powers"] = FakeWS(
        [
            ["Discord ID", "Name", "Profession", "In Buddy System"],
            # Departed: profession blanked AND marked out.
            ["1", "Walt", "", "no"],
            ["2", "Wanda", "War Leader", ""],
            ["3", "Eve", "Engineer", ""],
        ]
    )
    sheets["Buddy System"] = FakeWS(
        [
            list(buddy.BUDDY_HEADER),
            # Walt still sits in the left (War Leader) block from the last save.
            ["1", "Walt", "", "3", "Eve", "", "", "", ""],
        ]
    )
    primary = buddy.read_all_professions(GID, "Squad Powers", "Profession", "In Buddy System")
    fallback = buddy.read_members_from_buddy_tab(GID, "Buddy System")

    # merge_members alone keeps Walt — that's the old, broken behaviour.
    assert "1" in {m.discord_id for m in buddy.merge_members(primary, fallback)}
    # eligible_members drops him.
    assert "1" not in {m.discord_id for m in buddy.eligible_members(primary, fallback)}


def test_eligible_members_matches_merge_when_nothing_excluded(sheets):
    primary = [Member(name="Wanda", discord_id="2", profession="War Leader")]
    fallback = [Member(name="Eve", discord_id="3", profession="Engineer")]
    assert {m.discord_id for m in buddy.eligible_members(primary, fallback)} == {"2", "3"}


def test_exclusion_matches_by_name_when_no_discord_id(sheets):
    sheets["Squad Powers"] = FakeWS(
        [
            ["Name", "Profession", "In Buddy System"],
            ["Walt", "War Leader", "no"],
        ]
    )
    sheets["Buddy System"] = FakeWS(
        [list(buddy.BUDDY_HEADER), ["", "Walt", "", "", "", "", "", "", ""]]
    )
    primary = buddy.read_all_professions(GID, "Squad Powers", "Profession", "In Buddy System")
    fallback = buddy.read_members_from_buddy_tab(GID, "Buddy System")
    assert buddy.eligible_members(primary, fallback) == []


def test_names_dropped_by_reports_tab_members_missing_from_result():
    result = buddy.PairingResult(
        pairs=[Pair("Wanda", "2", "Eve", "3")],
        unpaired_wl=[Member(name="Wes", discord_id="4", profession="War Leader")],
        unpaired_eng=[],
    )
    current = [
        Member(name="Wanda", discord_id="2"),
        Member(name="Eve", discord_id="3"),
        Member(name="Wes", discord_id="4"),
        Member(name="Walt", discord_id="1"),
        Member(name="Ada", discord_id="9"),
        Member(name="ada", discord_id="9"),  # duplicate block on the same row
    ]
    # Sorted, deduped, and only the people the result no longer carries.
    assert buddy.names_dropped_by(result, current) == ["Ada", "Walt"]


def test_from_scratch_rebuild_sheds_buddy_tab_only_members(sheets):
    """`Re-pair from scratch` used to keep departed members because it only
    discarded pairings, not the pool. It now rebuilds the pool from Squad
    Powers alone, so a leftover Buddies-tab row no longer carries anyone."""
    import buddy_ui

    sheets["Squad Powers"] = FakeWS(
        [
            ["Discord ID", "Name", "Profession"],
            ["2", "Wanda", "War Leader"],
            ["3", "Eve", "Engineer"],
        ]
    )
    sheets["Buddy System"] = FakeWS(
        [
            list(buddy.BUDDY_HEADER),
            # Walt has been taken off Squad Powers but is still on this tab.
            ["1", "Walt", "", "3", "Eve", "", "", "", ""],
            ["2", "Wanda", "", "", "", "", "", "", ""],
        ]
    )
    cfg = {
        "profession_tab": "Squad Powers",
        "profession_col_header": "Profession",
        "buddy_tab": "Buddy System",
        "include_col_header": "",
    }

    kept = buddy_ui.compute_autofill(GID, cfg, from_scratch=False)
    assert "Walt" in _all_names(kept)

    rebuilt, dropped = buddy_ui.preview_scratch_rebuild(GID, cfg)
    assert "Walt" not in _all_names(rebuilt)
    assert dropped == ["Walt"]
    # And the people who are still on Squad Powers survive the rebuild.
    assert {"Wanda", "Eve"} <= _all_names(rebuilt)


def _all_names(result) -> set:
    names = set()
    for p in result.pairs:
        names.add(p.war_leader)
        names.add(p.engineer)
    for m in list(result.unpaired_wl) + list(result.unpaired_eng):
        names.add(m.name)
    return names


# ── Roster-sourced eligibility (#428) ────────────────────────────────────────


def test_build_roster_index_collects_ids_and_names():
    idx = buddy.build_roster_index(
        [{"name": "Wanda", "discord_id": "2"}, {"name": "Eve", "discord_id": ""}]
    )
    assert idx.ids == {"2"}
    assert idx.names == {"wanda", "eve"}
    assert idx


def test_roster_index_matches_by_id_or_name():
    """Identity is tier-dependent: a synced roster has Discord IDs, a
    hand-maintained one has names only. Either match keeps a member."""
    idx = buddy.build_roster_index([{"name": "Wanda", "discord_id": "2"}])
    # Renamed on Squad Powers but the ID still matches.
    assert idx.has(Member(name="Wanda The Great", discord_id="2"))
    # No ID anywhere (free tier) but the name matches, case-insensitively.
    assert idx.has(Member(name="  wanda ", discord_id=""))
    assert not idx.has(Member(name="Walt", discord_id="99"))


def test_roster_intersect_drops_members_not_on_the_roster():
    primary = [
        Member(name="Wanda", discord_id="2", profession="War Leader"),
        Member(name="Eve", discord_id="3", profession="Engineer"),
        Member(name="Walt", discord_id="1", profession="War Leader"),  # left
    ]
    roster = buddy.build_roster_index(
        [{"name": "Wanda", "discord_id": "2"}, {"name": "Eve", "discord_id": "3"}]
    )
    kept = {m.discord_id for m in buddy.eligible_members(primary, [], roster)}
    assert kept == {"2", "3"}


def test_empty_roster_skips_the_intersect_instead_of_emptying_the_pool():
    """load_roster_members returns [] on a renamed tab, revoked access or any
    read failure. Applying that naively would un-pair the whole alliance, so an
    empty roster must leave the pool untouched."""
    primary = [
        Member(name="Wanda", discord_id="2", profession="War Leader"),
        Member(name="Eve", discord_id="3", profession="Engineer"),
    ]
    for roster in (None, buddy.RosterIndex(), buddy.build_roster_index([])):
        kept = {m.discord_id for m in buddy.eligible_members(primary, [], roster)}
        assert kept == {"2", "3"}, f"empty roster {roster!r} emptied the pool"


def test_read_roster_index_degrades_to_empty_on_failure():
    with patch("train_rotation.load_roster_members", side_effect=RuntimeError("boom")):
        assert not buddy.read_roster_index(GID)


def test_members_missing_from_roster_only_counts_real_candidates():
    primary = [
        Member(name="Wanda", discord_id="2", profession="War Leader"),
        Member(name="Walt", discord_id="1", profession="War Leader"),  # off roster
        Member(name="Nobody", discord_id="7", profession=""),  # no profession
        Member(name="OptedOut", discord_id="8", profession="Engineer", included=False),
    ]
    roster = buddy.build_roster_index([{"name": "Wanda", "discord_id": "2"}])
    # Only the member who'd otherwise have been paired counts as a near-miss.
    assert buddy.members_missing_from_roster(primary, roster) == ["Walt"]
    # No roster configured means no warning at all.
    assert buddy.members_missing_from_roster(primary, None) == []


def test_roster_filter_end_to_end_drops_departed_member(sheets):
    import buddy_ui

    sheets["Squad Powers"] = FakeWS(
        [
            ["Discord ID", "Name", "Profession"],
            ["2", "Wanda", "War Leader"],
            ["3", "Eve", "Engineer"],
            ["1", "Walt", "War Leader"],
        ]
    )
    cfg = {
        "profession_tab": "Squad Powers",
        "profession_col_header": "Profession",
        "buddy_tab": "Buddy System",
        "include_col_header": "",
        "roster_filter_enabled": 1,
    }
    roster_rows = [{"name": "Wanda", "discord_id": "2"}, {"name": "Eve", "discord_id": "3"}]
    with patch("train_rotation.load_roster_members", return_value=roster_rows):
        result = buddy_ui.compute_autofill(GID, cfg)
        assert "Walt" not in _all_names(result)
        assert {"Wanda", "Eve"} <= _all_names(result)
        assert "Walt" in buddy_ui.roster_warning(GID, cfg)

    # Roster unreadable: pool stays intact and leadership is told why.
    with patch("train_rotation.load_roster_members", return_value=[]):
        result = buddy_ui.compute_autofill(GID, cfg)
        assert "Walt" in _all_names(result)
        assert "Couldn't read your member roster" in buddy_ui.roster_warning(GID, cfg)


def test_roster_warning_silent_when_filter_off(sheets):
    import buddy_ui

    assert buddy_ui.roster_warning(GID, {"roster_filter_enabled": 0}) == ""

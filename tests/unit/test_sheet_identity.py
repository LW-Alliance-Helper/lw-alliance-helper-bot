"""Discord ID columns on member tabs (#723): the shared helper every feature uses."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import sheet_identity as si

GID = 4242


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(si, "_found", {})
    monkeypatch.setattr(si, "_last_stamped", {})


class FakeSpreadsheet:
    def __init__(self):
        self.requests: list[dict] = []

    def batch_update(self, body):
        self.requests.extend(body["requests"])


class FakeWS:
    """The gspread surface the helper uses: header cell writes, grid size,
    per-cell batch writes, and the ids the column cache keys on."""

    def __init__(self, rows, *, title="Tab", sheet_id=7):
        self.rows = [list(r) for r in rows]
        self.title = title
        self.id = sheet_id
        self.spreadsheet_id = "sheet-abc"
        self.spreadsheet = FakeSpreadsheet()
        self.col_count = 26
        self.cell_writes: list[tuple] = []

    def update_cell(self, row, col, value):
        target = self.rows[row - 1]
        target.extend([""] * (col - len(target)))
        target[col - 1] = value
        self.cell_writes.append((row, col, value))

    def add_cols(self, n):
        self.col_count += n

    def batch_update(self, data, value_input_option=None):
        for item in data:
            ref = item["range"]
            col = ord(ref[0]) - ord("A")
            row = self.rows[int(ref[1:]) - 1]
            row.extend([""] * (col + 1 - len(row)))
            row[col] = item["values"][0][0]


def _roster(*members):
    return si.build_roster([(did, name, display) for did, name, display in members])


# ── Who is who ───────────────────────────────────────────────────────────────


def test_roster_maps_both_spellings_and_prefers_the_display_name():
    roster = _roster(("111", "Alpha", "Alpha [ABC]"))
    assert roster.id_for("alpha") == "111"
    assert roster.id_for(" Alpha [ABC] ") == "111"
    assert roster.name_for("111") == "Alpha [ABC]"


def test_an_id_shared_by_two_rows_is_no_id():
    roster = _roster(("R4", "Alpha", ""), ("R4", "Bravo", ""), ("111", "Charlie", ""))
    assert roster.id_for("Alpha") == ""
    assert roster.name_for("R4") == ""
    assert roster.id_for("Charlie") == "111"


def test_current_name_follows_the_id():
    roster = _roster(("111", "Bravo", ""))
    assert roster.current_name("Alpha", "111") == "Bravo"
    assert roster.current_name("Alpha", "") == "Alpha"
    assert roster.current_name("Alpha", "999") == "Alpha"


@pytest.mark.parametrize(
    "a, b, same",
    [
        (("Alpha", "111"), ("Bravo", "111"), True),  # a rename
        (("Alpha", "111"), ("Alpha", "222"), False),  # two people, one name
        (("Alpha", ""), ("alpha ", "111"), True),  # no ID on one side → name
        (("", ""), ("", ""), False),
    ],
)
def test_same_member(a, b, same):
    assert si.same_member(*a, *b) is same


def test_only_a_synced_roster_gives_ids():
    rows = [("111", "Alpha", "")]
    with (
        patch("config.get_member_roster_config", return_value={"enabled": 0}),
        patch("member_roster.roster_identity_rows", return_value=rows),
    ):
        assert si.load_roster(GID).by_name == {}
    with (
        patch("config.get_member_roster_config", return_value={"enabled": 1}),
        patch("member_roster.roster_identity_rows", return_value=rows),
    ):
        assert si.load_roster(GID).id_for("Alpha") == "111"


def test_an_unreadable_roster_is_an_empty_one():
    with patch("config.get_member_roster_config", side_effect=RuntimeError("no db")):
        assert si.load_roster(GID).by_name == {}


# ── Rows ─────────────────────────────────────────────────────────────────────


def test_row_index_finds_by_id_then_name():
    rows = [["Alpha", "111"], ["Bravo", ""], ["Alpha", "222"]]
    index = si.RowIndex(rows, name_col=0, id_col=1, first_row=2)
    assert index.find("Renamed", "222") == 4
    assert index.find("bravo", "333") == 3
    assert index.find("Alpha", "") == 2  # the first of two rows wins
    assert index.find("Nobody", "") is None


def test_fill_ids_stamps_name_only_rows_it_can_name():
    rows = [["Alpha", ""], ["Bravo"], ["Charlie", "999"], ["Zulu", ""]]
    roster = _roster(("111", "Alpha", ""), ("222", "Bravo", ""), ("333", "Charlie", ""))
    changed = si.fill_ids(rows, name_col=0, id_col=1, roster=roster)
    assert changed == [0, 1]
    assert rows == [["Alpha", "111"], ["Bravo", "222"], ["Charlie", "999"], ["Zulu", ""]]


def test_maybe_stamp_writes_once_per_interval():
    ws = FakeWS([["Member", "Discord ID"], ["Alpha", ""]])
    roster = _roster(("111", "Alpha", ""))
    with patch.object(si, "load_roster", return_value=roster) as load:
        assert si.maybe_stamp(ws, ws.rows[1:], guild_id=GID, name_col=0, id_col=1) == 1
        ws.rows.append(["Alpha", ""])
        assert si.maybe_stamp(ws, ws.rows[1:], guild_id=GID, name_col=0, id_col=1) == 0
    assert load.call_count == 1
    assert ws.rows[1] == ["Alpha", "111"]


def test_maybe_stamp_skips_the_roster_when_every_row_has_an_id():
    ws = FakeWS([["Member", "Discord ID"], ["Alpha", "111"]])
    with patch.object(si, "load_roster") as load:
        assert si.maybe_stamp(ws, ws.rows[1:], guild_id=GID, name_col=0, id_col=1) == 0
    load.assert_not_called()


# ── The column ───────────────────────────────────────────────────────────────


def test_find_column_by_tag_wherever_it_moved():
    header = ["Member", "ID please", "Notes"]
    assert si.find_column(header, {1: dict(si.TAG)}) == (1, False)


def test_find_column_adopts_an_old_header():
    assert si.find_column(["Member", "discord id"], {}) == (1, True)
    assert si.find_column(["Member"], {}) == (-1, False)


def _requests_of(ws, kind):
    return [r[kind] for r in ws.spreadsheet.requests if kind in r]


def test_ensure_column_adds_a_tagged_hidden_text_column():
    ws = FakeWS([["Member", "Rule Type", "Notes"]])
    header = list(ws.rows[0])
    with (
        patch.object(si, "read_tags", return_value={}),
        patch.object(si, "settings_for", return_value=si.Settings()),
    ):
        idx = si.ensure_column(ws, header, guild_id=GID)
    assert idx == 3
    assert header[3] == "Discord ID"
    assert ws.rows[0][3] == "Discord ID"
    (meta,) = _requests_of(ws, "createDeveloperMetadata")
    assert meta["developerMetadata"]["location"]["dimensionRange"]["startIndex"] == 3
    (fmt,) = _requests_of(ws, "repeatCell")
    assert fmt["cell"]["userEnteredFormat"]["numberFormat"]["type"] == "TEXT"
    assert fmt["range"]["startRowIndex"] == 1  # below the header
    (vis,) = _requests_of(ws, "updateDimensionProperties")
    assert vis["properties"] == {"hiddenByUser": True}


def test_ensure_column_shows_it_when_the_alliance_said_so():
    ws = FakeWS([["Member"]])
    with (
        patch.object(si, "read_tags", return_value={}),
        patch.object(si, "settings_for", return_value=si.Settings(shown=True, answered=True)),
    ):
        si.ensure_column(ws, ["Member"], guild_id=GID)
    (vis,) = _requests_of(ws, "updateDimensionProperties")
    assert vis["properties"] == {"hiddenByUser": False}


def test_ensure_column_adopts_an_old_column_without_hiding_it_unasked():
    ws = FakeWS([["Date", "Member", "Discord ID"]])
    with (
        patch.object(si, "read_tags", return_value={}),
        patch.object(si, "settings_for", return_value=si.Settings()),
    ):
        assert si.ensure_column(ws, list(ws.rows[0]), guild_id=GID) == 2
    assert ws.cell_writes == []
    assert _requests_of(ws, "createDeveloperMetadata")
    assert _requests_of(ws, "updateDimensionProperties") == []


def test_ensure_column_adopting_follows_an_answered_setting():
    ws = FakeWS([["Date", "Member", "Discord ID"]])
    with (
        patch.object(si, "read_tags", return_value={}),
        patch.object(si, "settings_for", return_value=si.Settings(shown=False, answered=True)),
    ):
        si.ensure_column(ws, list(ws.rows[0]), guild_id=GID)
    (vis,) = _requests_of(ws, "updateDimensionProperties")
    assert vis["properties"] == {"hiddenByUser": True}


def test_a_tagged_column_is_left_alone():
    ws = FakeWS([["Member", "Who"]])
    with patch.object(si, "read_tags", return_value={1: dict(si.TAG)}):
        assert si.ensure_column(ws, ["Member", "Who"], guild_id=GID) == 1
    assert ws.spreadsheet.requests == []


def test_the_column_is_remembered_until_its_header_changes():
    ws = FakeWS([["Member", "Who"]])
    with patch.object(si, "read_tags", return_value={1: dict(si.TAG)}) as tags:
        assert si.locate_column(ws, ["Member", "Who"]) == 1
        assert si.locate_column(ws, ["Member", "Who"]) == 1
        assert tags.call_count == 1
        # An officer moved the column: the cached index now holds something
        # else, so the tags are read again.
        assert si.locate_column(ws, ["Who", "Member"]) == 1
        assert tags.call_count == 2


def test_locate_column_never_adds_one():
    ws = FakeWS([["Member"]])
    with patch.object(si, "read_tags", return_value={}):
        assert si.locate_column(ws, ["Member"]) == -1
    assert ws.cell_writes == [] and ws.spreadsheet.requests == []


# ── The alliance's choice ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "scope, shown, expected",
    [
        ("", 1, si.Settings()),
        ("bot", 1, si.Settings(alliance_tabs=False, shown=True, answered=True)),
        ("all", 0, si.Settings(alliance_tabs=True, shown=False, answered=True)),
    ],
)
def test_settings_for(scope, shown, expected):
    cfg = SimpleNamespace(id_columns_scope=scope, id_columns_shown=shown)
    with patch("config.get_config", return_value=cfg):
        assert si.settings_for(GID) == expected


def test_settings_default_when_unreadable():
    with patch("config.get_config", side_effect=RuntimeError("no db")):
        assert si.settings_for(GID) == si.Settings()


def test_apply_visibility_covers_every_tagged_id_column():
    sh = FakeSpreadsheet()
    tags = {
        7: {3: dict(si.TAG), 0: {"t": "growth", "k": "name"}},
        9: {5: {"t": "growth", "k": "id"}},
    }
    with (
        patch("config.get_spreadsheet", return_value=sh),
        patch("sheet_tags.read_column_tags", return_value=tags),
    ):
        assert si.apply_visibility(GID, shown=True) == 2
    targets = {
        (
            r["updateDimensionProperties"]["range"]["sheetId"],
            r["updateDimensionProperties"]["range"]["startIndex"],
        )
        for r in sh.requests
    }
    assert targets == {(7, 3), (9, 5)}
    assert all(
        r["updateDimensionProperties"]["properties"] == {"hiddenByUser": False} for r in sh.requests
    )


# ── Tabs the alliance made ───────────────────────────────────────────────────


def _birthday_tab():
    return FakeWS(
        [["Alliance ABC birthdays"], ["Name", "Birthday"], ["Alpha", "1/2"], ["Zulu", "3/4"]],
        title="Birthdays",
    )


def _alliance_patches(settings, roster):
    return (
        patch.object(si, "settings_for", return_value=settings),
        patch.object(si, "load_roster", return_value=roster),
        patch.object(si, "read_tags", return_value={}),
        patch("config.get_member_roster_config", return_value={"tab_name": "Member Roster"}),
    )


def test_an_alliance_tab_gets_a_column_when_they_asked():
    ws = _birthday_tab()
    p = _alliance_patches(
        si.Settings(alliance_tabs=True, answered=True), _roster(("111", "Alpha", ""))
    )
    with p[0], p[1], p[2], p[3]:
        idx = si.alliance_tab_column(ws, ws.rows, guild_id=GID, name_col=0, first_row=3)
    assert idx == 2
    assert ws.rows[1] == ["Name", "Birthday", "Discord ID"]  # the header row above the data
    assert ws.rows[2] == ["Alpha", "1/2", "111"]
    assert ws.rows[3] == ["Zulu", "3/4"]


def test_an_alliance_tab_is_left_alone_by_default():
    ws = _birthday_tab()
    p = _alliance_patches(si.Settings(), _roster(("111", "Alpha", "")))
    with p[0], p[1], p[2], p[3]:
        assert si.alliance_tab_column(ws, ws.rows, guild_id=GID, name_col=0, first_row=3) == -1
    assert ws.cell_writes == []


def test_no_column_when_the_roster_has_no_ids():
    ws = _birthday_tab()
    p = _alliance_patches(si.Settings(alliance_tabs=True, answered=True), si.Roster())
    with p[0], p[1], p[2], p[3]:
        assert si.alliance_tab_column(ws, ws.rows, guild_id=GID, name_col=0, first_row=3) == -1
    assert ws.cell_writes == []


def test_the_roster_tab_itself_never_gets_a_second_column():
    ws = FakeWS([["Discord ID", "Name"], ["111", "Alpha"]], title="Member Roster")
    p = _alliance_patches(
        si.Settings(alliance_tabs=True, answered=True), _roster(("111", "Alpha", ""))
    )
    with p[0], p[1], p[2], p[3]:
        si.alliance_tab_column(ws, ws.rows, guild_id=GID, name_col=1, first_row=2)
    assert ws.cell_writes == [] and ws.spreadsheet.requests == []


def test_a_tab_with_no_header_row_gets_no_column():
    ws = FakeWS([["Alpha", "1/2"]])
    p = _alliance_patches(
        si.Settings(alliance_tabs=True, answered=True), _roster(("111", "Alpha", ""))
    )
    with p[0], p[1], p[2], p[3]:
        assert si.alliance_tab_column(ws, ws.rows, guild_id=GID, name_col=0, first_row=1) == -1

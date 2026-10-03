"""The storm Rosters and Signups tabs in words and the house style (#729).

`FakeSheet` stands in for a worksheet that behaves like Sheets does: a
`USER_ENTERED` write turns ISO dates and stamps into date serials and digits
into numbers (a leading apostrophe keeps text as text), a formatted read
shows a date the way a US locale would (`9/28/2026`), and an unformatted
read hands back the serials. So a reader that compared the displayed date
would fail these tests, the way it would fail on a real formatted tab.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest

import storm_history
import storm_roster_builder as srb
import storm_roster_writeback as wb
import storm_sheet_tabs as tabs
import storm_signup_view as sv
import storm_strategy as ss
import storm_attendance as sa

GID = 1497432945827516639
EPOCH = datetime(1899, 12, 30)


class Serial:
    """A cell Sheets holds as a date or date-time."""

    def __init__(self, value: float):
        self.value = value

    def shown(self) -> str:
        moment = EPOCH + timedelta(days=self.value)
        text = f"{moment.month}/{moment.day}/{moment.year}"
        if self.value != int(self.value):
            text += moment.strftime(" %H:%M:%S")
        return text


def _entered(value):
    """What Sheets stores for a `USER_ENTERED` value."""
    if not isinstance(value, str):
        return value
    if value.startswith("'"):
        return value[1:]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return Serial((datetime.fromisoformat(value) - EPOCH).days)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        delta = datetime.fromisoformat(value) - EPOCH
        return Serial(delta.days + delta.seconds / 86400)
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return value


class FakeSheet:
    def __init__(self, rows=None, *, title="DS Rosters"):
        self.title = title
        self.rows = [list(r) for r in (rows or [])]
        self.writes: list[str] = []  # value_input_option of each data write
        self.deleted: list[tuple[int, int]] = []
        self.text_cols: set[int] = set()  # columns formatted as plain text

    def _store(self, values, option):
        self.writes.append(option)
        if option == "USER_ENTERED":
            return [
                [v if c in self.text_cols else _entered(v) for c, v in enumerate(row)]
                for row in values
            ]
        return [[v if isinstance(v, str) else str(v) for v in row] for row in values]

    def get_all_values(self, value_render_option=None, date_time_render_option=None):
        if value_render_option == "UNFORMATTED_VALUE":
            return [[c.value if isinstance(c, Serial) else c for c in r] for r in self.rows]
        return [[c.shown() if isinstance(c, Serial) else str(c) for c in r] for r in self.rows]

    def row_values(self, n):
        return self.get_all_values()[n - 1] if n <= len(self.rows) else []

    def update(self, range_, values, value_input_option=None):
        start = int(re.match(r"^A(\d+)", range_).group(1)) - 1
        for i, row in enumerate(self._store(values, value_input_option)):
            while len(self.rows) <= start + i:
                self.rows.append([])
            self.rows[start + i] = row

    def batch_update(self, updates, value_input_option=None):
        for item in updates:
            ref = item["range"]
            col = ord(ref[0]) - 65
            row = self.rows[int(ref[1:]) - 1]
            row.extend([""] * (col + 1 - len(row)))
            row[col] = item["values"][0][0]

    def append_rows(self, rows, value_input_option=None):
        self.rows.extend(self._store(rows, value_input_option))

    def append_row(self, row, value_input_option=None):
        self.append_rows([row], value_input_option)

    def clear(self):
        self.rows = []

    def delete_rows(self, start, end=None):
        self.deleted.append((start, end or start))


class FakeBook:
    def __init__(self, **tabs_):
        self.tabs = dict(tabs_)

    def worksheet(self, title):
        if title not in self.tabs:
            import gspread

            raise gspread.WorksheetNotFound(title)
        return self.tabs[title]

    def add_worksheet(self, title, rows=0, cols=0):
        self.tabs[title] = FakeSheet(title=title)
        return self.tabs[title]


@pytest.fixture
def book(monkeypatch):
    """A spreadsheet with the structured flow on, the shared-piece calls
    that need a real Sheet recorded instead."""
    book = FakeBook()
    formatted: list = []
    adopted: list = []
    monkeypatch.setattr("config.get_spreadsheet", lambda gid: book)
    monkeypatch.setattr(
        "config.get_structured_storm_config",
        lambda gid, et: {"structured_flow_enabled": True, "signups_tab": "DS Signups"},
    )
    monkeypatch.setattr(
        "sheet_format.ensure_formatted", lambda ws, spec, **kw: formatted.append(spec)
    )

    def adopt(ws, cols, **kw):
        adopted.append(list(cols))
        ws.text_cols.update(cols)

    monkeypatch.setattr("sheet_identity.adopt_columns", adopt)
    monkeypatch.setattr(tabs, "_signups_prepared", set())
    book.formatted, book.adopted = formatted, adopted
    return book


def _session(members, *, event_date="2026-09-28"):
    preset = ss.PresetBuffer(
        name="Standard",
        event_type="DS",
        zones=[ss.ZoneRow(zone="Power Tower", max_players=4, min_power_a=0, min_power_b=0)],
    )
    session = srb.RosterBuilderSession(
        guild_id=GID,
        user_id=42,
        event_type="DS",
        team="A",
        preset=preset,
        members={k: {"key": k, "name": n, "discord_id": k, "power": p} for k, n, p in members},
        per_member_rules=[],
        power_band_rules=[],
        sub_mode="pool",
    )
    session.event_date = event_date
    return session


ALPHA = "100000000000000001"
BRAVO = "100000000000000002"
CHARLIE = "100000000000000003"


def _post(book):
    session = _session([(ALPHA, "Alpha", 412_000_000), (BRAVO, "Bravo", None), (CHARLIE, "007", 1)])
    session.assignments["Power Tower"] += [ALPHA, BRAVO]
    session.below_floor_overrides.add(BRAVO)
    session.subs.append(CHARLIE)
    with patch("time_helpers.server_stamp", return_value="2026-09-28 20:00:05"):
        assert srb._write_rosters_tab(session) == []
    return book.tabs["DS Rosters"]


# ── Cells ────────────────────────────────────────────────────────────────────


def test_role_words_read_old_codes_and_new_words_in_any_case():
    for cell, code in (("primary", "primary"), ("Primary", "primary"), ("SUB", "sub")):
        assert tabs.ROLE_WORDS.code(cell) == code
    assert tabs.ROLE_WORDS.options == ("Primary", "Sub")


def test_override_and_power_cells():
    assert [tabs.is_override(c) for c in ("Yes", "yes", "x", "TRUE", "", "no")] == [
        True,
        True,
        True,
        True,
        False,
        False,
    ]
    assert tabs.power_cell(None) == "Unknown" and tabs.power_cell(412_000_000) == "412000000"
    assert tabs.power_code("Unknown") == "unknown" and tabs.power_code("unknown") == "unknown"
    assert tabs.power_code("412000000") == "412000000"


def test_names_that_sheets_would_parse_stay_text():
    assert tabs.text_cell("Alpha") == "Alpha"
    assert tabs.text_cell("=Alpha") == "'=Alpha"
    assert tabs.text_cell("007") == "'007"
    assert tabs.text_cell("") == ""


def test_format_is_found_by_header_name():
    spec = tabs.rosters_format(tabs.ROSTERS_HEADER)
    assert (spec.date, spec.quantity, spec.text, spec.datetime) == ((0,), (6,), (7,), (10,))
    assert spec.frozen_columns == 0
    assert spec.dropdowns == ((5, ("Primary", "Sub")), (8, ("Yes",)))
    # A Map Manager tab from before #729 has the short header.
    short = ["Event Date", "Team", "Stage", "Zone", "Member", "Role", "Power at Assignment"]
    assert tabs.rosters_format(short).datetime == ()


# ── Rosters: the builder's writer ────────────────────────────────────────────


def test_builder_writes_words_real_dates_and_numbers(book):
    ws = _post(book)
    assert ws.rows[0] == tabs.ROSTERS_HEADER
    assert ws.writes[-1] == "USER_ENTERED"
    alpha, bravo, charlie = ws.rows[1:]
    assert isinstance(alpha[0], Serial)  # Event Date is a date, not text
    assert alpha[4:9] == ["Alpha", "Primary", 412000000, ALPHA, ""]
    assert bravo[5:9] == ["Primary", "Unknown", BRAVO, "Yes"]
    assert charlie[4:6] == ["007", "Sub"]  # kept as text, not the number 7
    assert isinstance(alpha[10], Serial)  # Posted At, a date-time
    assert book.adopted == [[7]]
    assert book.formatted[-1] == tabs.rosters_format(tabs.ROSTERS_HEADER)


def test_builder_renames_only_the_old_posted_at_header(book):
    old = list(tabs.ROSTERS_HEADER)
    old[10] = "Posted At (UTC)"
    book.tabs["DS Rosters"] = FakeSheet([old])
    assert _post(book).rows[0][10] == "Posted At (server time)"

    mine = list(tabs.ROSTERS_HEADER)
    mine[10] = "When we posted"
    book.tabs["DS Rosters"] = FakeSheet([mine])
    assert _post(book).rows[0][10] == "When we posted"


def test_header_migration_rewords_old_rows(book):
    book.tabs["DS Rosters"] = FakeSheet(
        [
            [
                "Event Date",
                "Team",
                "Zone",
                "Member",
                "Role",
                "Power at Assignment",
                "Discord ID",
                "Override Below Floor",
                "Posted At (UTC)",
            ],
            ["2026-05-11", "A", "Power Tower", "Alpha", "sub", "unknown", ALPHA, "yes", ""],
        ]
    )
    ws = _post(book)
    assert ws.rows[0] == tabs.ROSTERS_HEADER
    old = ws.rows[1]
    assert old[2] == 1 and old[4:9] == ["Alpha", "Sub", "Unknown", ALPHA, "Yes"]
    assert isinstance(old[0], Serial)
    # The ID column adopted is where the migration puts the IDs, not where
    # the old header had them.
    assert book.adopted == [[7]]


# ── Rosters: readers ─────────────────────────────────────────────────────────


def test_readers_read_the_new_tab_back(book, monkeypatch):
    _post(book)
    monkeypatch.setattr("config.get_structured_storm_config", lambda gid, et: {})
    dates, errors = storm_history.list_event_dates(GID, "DS")
    assert (dates, errors) == (["2026-09-28"], [])
    slots, _ = storm_history.load_event_roster(GID, "DS", "2026-09-28")
    by_name = {s["member"]: s for s in slots}
    assert by_name["Alpha"]["role"] == "primary" and by_name["Alpha"]["power"] == "412000000"
    assert by_name["Bravo"]["power"] == "unknown" and by_name["Bravo"]["override_below_floor"]
    assert by_name["007"]["role"] == "sub"
    assert by_name["Alpha"]["discord_id"] == ALPHA

    slots, errors = sa.load_rostered_slots(GID, "DS", "2026-09-28")
    assert errors == [] and {s["member"]: s["role"] for s in slots} == {
        "Alpha": "primary",
        "Bravo": "primary",
        "007": "sub",
    }


def test_readers_still_read_old_rows(book, monkeypatch):
    book.tabs["DS Rosters"] = FakeSheet(
        [
            ["Event Date", "Member", "Role", "Power at Assignment", "Override Below Floor"],
            ["2026-05-11", "Alpha", "sub", "unknown", "yes"],
        ]
    )
    monkeypatch.setattr("config.get_structured_storm_config", lambda gid, et: {})
    (slot,), _ = storm_history.load_event_roster(GID, "DS", "2026-05-11")
    assert (slot["role"], slot["power"], slot["override_below_floor"]) == ("sub", "unknown", True)


# ── Rosters: the Map Manager write-back ──────────────────────────────────────


def test_write_back_writes_what_the_builder_writes(book, monkeypatch):
    monkeypatch.setattr("config.get_structured_storm_config", lambda gid, et: {})
    monkeypatch.setattr(wb, "_read_power_index", lambda gid, et: {ALPHA: {"power": 5_000_000}})
    assignments = [
        {"member_name": "Alpha", "discord_id": ALPHA, "team": "A", "role": "primary", "zone": "X"},
        {"member_name": "Bravo", "discord_id": None, "team": "B", "role": "sub"},
    ]
    result = wb.write_mm_storm_roster(GID, "ds", "2026-09-28", assignments)
    assert result == {"written": True, "rows": 2}
    ws = book.tabs["DS Rosters"]
    assert ws.rows[0] == tabs.ROSTERS_HEADER
    assert ws.writes[-1] == "USER_ENTERED"
    alpha, bravo = ws.rows[1:]
    assert isinstance(alpha[0], Serial)
    assert alpha[4:8] == ["Alpha", "Primary", 5000000, ALPHA]
    assert bravo[5:7] == ["Sub", "Unknown"]
    assert book.adopted == [[7]]

    # Overwrite finds the date rows through the formatted date column.
    result = wb.write_mm_storm_roster(GID, "ds", "2026-09-28", assignments, overwrite=True)
    assert ws.deleted == [(2, 3)]


# ── Signups ──────────────────────────────────────────────────────────────────


def _vote(**kw):
    args = dict(
        guild_id=GID,
        event_type="DS",
        event_date="2026-09-28",
        target_label="Alpha",
        voter_id=int(ALPHA),
        vote="a",
        is_on_behalf=False,
    )
    args.update(kw)
    with patch("time_helpers.server_stamp", return_value="2026-09-28 20:00:05"):
        sv._mirror_vote_to_sheet(**args)


def test_a_vote_is_written_in_words_with_real_dates(book):
    _vote()
    _vote(target_label="Bravo", is_on_behalf=True)
    ws = book.tabs["DS Signups"]
    assert ws.rows[0] == tabs.SIGNUPS_HEADER
    alpha, bravo = ws.rows[1:]
    assert isinstance(alpha[0], Serial) and isinstance(alpha[5], Serial)
    assert alpha[1:5] == ["Alpha", "Team A", ALPHA, "No"]  # the ID kept as text
    assert bravo[4] == "Yes"
    assert ws.writes[-2:] == ["USER_ENTERED", "USER_ENTERED"]
    assert book.adopted[0] == [3]
    assert book.formatted[-1] == tabs.SIGNUPS_FORMAT


def test_signups_header_renamed_only_from_the_old_text(book):
    old = list(tabs.SIGNUPS_HEADER)
    old[5] = "Voted At (UTC)"
    book.tabs["DS Signups"] = FakeSheet([old], title="DS Signups")
    _vote()
    assert book.tabs["DS Signups"].rows[0][5] == "Voted At (server time)"

    tabs._signups_prepared.clear()
    mine = list(tabs.SIGNUPS_HEADER)
    mine[5] = "When"
    book.tabs["DS Signups"] = FakeSheet([mine], title="DS Signups")
    _vote()
    assert book.tabs["DS Signups"].rows[0][5] == "When"


def test_prune_reads_dates_and_on_behalf_old_and_new(book):
    _vote(target_label="Alpha")
    _vote(target_label="Bravo", is_on_behalf=True)
    ws = book.tabs["DS Signups"]
    ws.rows.append(["2026-09-28", "Charlie", "Team B", "3", "yes", "2026-09-28T22:00:05+00:00"])
    ws.rows.append(["2026-10-05", "Charlie", "Team B", "3", "yes", ""])
    with patch.object(ws, "delete_rows", lambda idx: ws.deleted.append(idx)):
        deleted = sv._prune_votes_from_sheet(
            guild_id=GID, event_type="DS", event_date="2026-09-28", on_behalf_only=True
        )
    assert deleted == 2 and ws.deleted == [4, 3]  # Charlie's old "yes" row, then Bravo's


def test_serial_dates_read_back_as_iso():
    serial = (date(2026, 9, 28) - EPOCH.date()).days
    ws = FakeSheet([tabs.SIGNUPS_HEADER, [Serial(serial), "Alpha", "Team A", "1", "No", ""]])
    assert ws.get_all_values()[1][0] == "9/28/2026"
    assert tabs.read_signups(ws)[1][0] == "2026-09-28"

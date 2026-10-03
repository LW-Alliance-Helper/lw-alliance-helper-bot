"""Storm Rosters and Signups tabs built by the bot's own writers (#729).

Each test rebuilds one tab in the test spreadsheet through the functions the
bot calls in production (the roster builder's Approve & Post write, the Map
Manager write-back, the sign-up vote mirror and its clear-votes prune), then
checks the words, the real dates and numbers, the house style, and that the
bot's own readers read it back. The tabs are named "Preview: ..." so the
session scrub leaves them for Kevin to open. Mock data only: Alpha, Bravo,
Charlie, Delta.
"""

from unittest.mock import patch

import pytest

import sheet_format
import sheet_identity
import storm_attendance
import storm_history
import storm_roster_builder as srb
import storm_roster_writeback as wb
import storm_sheet_tabs as tabs
import storm_signup_view as sv
import storm_strategy as ss
from tests.conftest import TEST_GUILD_ID

pytestmark = pytest.mark.sheets

ALPHA = "100000000000000001"
BRAVO = "100000000000000002"
CHARLIE = "100000000000000003"
DELTA = "100000000000000004"


def _fresh(sh, title: str, header: list[str]):
    """Delete the previous run's tab and start again from the header the bot
    wrote before #729, so the header rename runs too."""
    for ws in sh.worksheets():
        if ws.title == title:
            sh.del_worksheet(ws)
    ws = sh.add_worksheet(title=title, rows=200, cols=len(header))
    ws.update("A1", [header], value_input_option="RAW")


def _meta(sh, title: str) -> dict:
    fields = (
        "sheets(properties(gridProperties(frozenRowCount,frozenColumnCount)),basicFilter(range),"
        "data(columnMetadata(hiddenByUser),"
        "rowData(values(userEnteredFormat(backgroundColor,textFormat(bold,fontSize))))))"
    )
    return sh.fetch_sheet_metadata(params={"ranges": f"'{title}'!A1:K2", "fields": fields})[
        "sheets"
    ][0]


@pytest.fixture
def storm_env(seeded_db, test_spreadsheet, monkeypatch):
    monkeypatch.setattr(sheet_format, "_met", set())
    monkeypatch.setattr(sheet_identity, "_found", {})
    monkeypatch.setattr(sheet_identity, "_adopted", set())
    monkeypatch.setattr(tabs, "_signups_prepared", set())
    with (
        patch("config.get_spreadsheet", return_value=test_spreadsheet),
        patch("sheet_identity.settings_for", return_value=sheet_identity.Settings()),
    ):
        yield test_spreadsheet


def _session(members, event_date: str):
    preset = ss.PresetBuffer(
        name="Standard",
        event_type="DS",
        zones=[
            ss.ZoneRow(zone="Power Tower", max_players=4, min_power_a=0, min_power_b=0),
            ss.ZoneRow(zone="Nuclear Silo", max_players=4, min_power_a=0, min_power_b=0),
        ],
    )
    session = srb.RosterBuilderSession(
        guild_id=TEST_GUILD_ID,
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


def test_preview_storm_rosters(storm_env):
    sh = storm_env
    title = "Preview: DS Rosters"
    old_header = list(tabs.ROSTERS_HEADER)
    old_header[10] = "Posted At (UTC)"
    _fresh(sh, title, old_header)
    cfg = {"rosters_tab": title, "structured_flow_enabled": True}

    with patch("config.get_structured_storm_config", return_value=cfg):
        session = _session(
            [
                (ALPHA, "Alpha", 412_000_000),
                (BRAVO, "Bravo", 287_500_000),
                (CHARLIE, "Charlie", None),
            ],
            "2026-09-28",
        )
        session.assignments["Power Tower"].append(ALPHA)
        session.assignments["Nuclear Silo"].append(BRAVO)
        session.below_floor_overrides.add(BRAVO)
        session.subs.append(CHARLIE)
        assert srb._write_rosters_tab(session) == []

        # The Map Manager write-back adds the next storm under the same header.
        with patch.object(wb, "_read_power_index", return_value={DELTA: {"power": 301_000_000}}):
            result = wb.write_mm_storm_roster(
                TEST_GUILD_ID,
                "ds",
                "2026-10-05",
                [
                    {
                        "member_name": "Delta",
                        "discord_id": DELTA,
                        "team": "A",
                        "role": "primary",
                        "zone": "Power Tower",
                    },
                    {"member_name": "Alpha", "discord_id": ALPHA, "team": "A", "role": "sub"},
                ],
            )
        assert result == {"written": True, "rows": 2}

        ws = sh.worksheet(title)
        assert sheet_format.ensure_formatted(ws, tabs.rosters_format(tabs.ROSTERS_HEADER)) is False
        rows = ws.get_all_values()
        assert rows[0] == tabs.ROSTERS_HEADER  # "Posted At (UTC)" renamed
        assert [r[5] for r in rows[1:]] == ["Primary", "Primary", "Sub", "Primary", "Sub"]
        assert [r[6] for r in rows[1:]][:3] == ["412,000,000", "287,500,000", "Unknown"]
        assert rows[2][8] == "Yes"
        assert rows[1][0] != "2026-09-28"  # a real date, in the locale's format

        raw = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
        assert raw[1][6] == 412000000  # a number, not text
        assert raw[1][7] == ALPHA  # the ID kept as text, not rounded

        # The bot's own readers read it back.
        dates, errors = storm_history.list_event_dates(TEST_GUILD_ID, "DS")
        assert (dates, errors) == (["2026-10-05", "2026-09-28"], [])
        slots, _ = storm_history.load_event_roster(TEST_GUILD_ID, "DS", "2026-09-28")
        by_name = {s["member"]: s for s in slots}
        assert by_name["Alpha"]["role"] == "primary" and by_name["Alpha"]["power"] == "412000000"
        assert by_name["Bravo"]["override_below_floor"] is True
        assert by_name["Charlie"]["role"] == "sub" and by_name["Charlie"]["power"] == "unknown"
        slots, errors = storm_attendance.load_rostered_slots(TEST_GUILD_ID, "DS", "2026-10-05")
        assert errors == [] and {s["member"] for s in slots} == {"Delta", "Alpha"}

    meta = _meta(sh, title)
    grid = meta["properties"]["gridProperties"]
    assert grid.get("frozenRowCount") == 1 and not grid.get("frozenColumnCount")
    assert "basicFilter" not in meta
    header = meta["data"][0]["rowData"][0]["values"][0]["userEnteredFormat"]
    assert header["textFormat"]["bold"] is True and header["textFormat"]["fontSize"] == 14
    hidden = [bool(c.get("hiddenByUser")) for c in meta["data"][0]["columnMetadata"]]
    assert hidden[7] is True  # Discord ID, hidden by default


def test_preview_storm_signups(storm_env):
    sh = storm_env
    title = "Preview: DS Signups"
    old_header = list(tabs.SIGNUPS_HEADER)
    old_header[5] = "Voted At (UTC)"
    _fresh(sh, title, old_header)
    cfg = {"signups_tab": title, "structured_flow_enabled": True}

    with patch("config.get_structured_storm_config", return_value=cfg):
        for day, name, voter, vote, behalf in (
            ("2026-09-28", "Alpha", ALPHA, "a", False),
            ("2026-09-28", "Bravo", BRAVO, "either", False),
            ("2026-09-28", "Charlie", BRAVO, "cannot", True),
            ("2026-10-05", "Delta", BRAVO, "b", True),
        ):
            sv._mirror_vote_to_sheet(
                guild_id=TEST_GUILD_ID,
                event_type="DS",
                event_date=day,
                target_label=name,
                voter_id=int(voter),
                vote=vote,
                is_on_behalf=behalf,
            )
        # The clear-votes prune finds the 5 October on-behalf row through
        # the formatted date column, and leaves the rest.
        assert (
            sv._prune_votes_from_sheet(
                guild_id=TEST_GUILD_ID,
                event_type="DS",
                event_date="2026-10-05",
                on_behalf_only=True,
            )
            == 1
        )

    ws = sh.worksheet(title)
    rows = ws.get_all_values()
    assert rows[0] == tabs.SIGNUPS_HEADER  # "Voted At (UTC)" renamed
    assert [r[1:3] for r in rows[1:]] == [
        ["Alpha", "Team A"],
        ["Bravo", "Either time works"],
        ["Charlie", "Cannot participate"],
    ]
    assert [r[4] for r in rows[1:]] == ["No", "No", "Yes"]
    assert rows[1][0] != "2026-09-28"
    values = tabs.read_signups(ws)
    assert values[1][0] == "2026-09-28" and len(values[1][5]) == len("2026-09-28 20:00:05")
    raw = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
    assert raw[1][3] == ALPHA

    meta = _meta(sh, title)
    grid = meta["properties"]["gridProperties"]
    assert (grid.get("frozenRowCount"), grid.get("frozenColumnCount")) == (1, 2)
    assert "basicFilter" not in meta
    hidden = [bool(c.get("hiddenByUser")) for c in meta["data"][0]["columnMetadata"]]
    assert hidden[3] is True  # Voter Discord ID, hidden by default

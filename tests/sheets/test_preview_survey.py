"""The survey, buddy and roster tabs, built by the bot's own writers and left
formatted for Kevin to open (#729).

Each test rebuilds its tabs in the test spreadsheet from scratch, through the
functions the bot calls in production, then checks the house style and that
the bot's own readers read the tabs back. Tabs are named "Preview: ..." so the
session scrub leaves them in place. Mock data only: Alpha, Bravo, Charlie,
Delta, fake 18-digit IDs.
"""

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import buddy
import member_roster
import member_stats
import sheet_format
import sheet_identity
import storm_roster_powers
import survey
from tests.conftest import TEST_GUILD_ID
from time_helpers import server_today

pytestmark = pytest.mark.sheets

ALPHA, BRAVO, CHARLIE, DELTA = (f"10000000000000000{i}" for i in range(1, 5))

QUESTIONS = [
    {"key": "s1", "label": "1st Squad Power", "type": "numeric"},
    {
        "key": "prof",
        "label": "Profession",
        "type": "dropdown",
        "options": ["War Leader", "Engineer"],
    },
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
    return sh.fetch_sheet_metadata(params={"ranges": f"'{title}'!A1:J2", "fields": fields})[
        "sheets"
    ][0]


def _frozen(meta: dict) -> tuple:
    grid = meta["properties"]["gridProperties"]
    return grid.get("frozenRowCount"), grid.get("frozenColumnCount")


def _hidden(meta: dict) -> list[bool]:
    return [bool(c.get("hiddenByUser")) for c in meta["data"][0].get("columnMetadata", [])]


@pytest.fixture
def clean_caches(monkeypatch):
    monkeypatch.setattr(sheet_format, "_met", set())
    monkeypatch.setattr(sheet_identity, "_found", {})
    monkeypatch.setattr(sheet_identity, "_adopted", set())
    monkeypatch.setattr(member_roster, "_joined_reads", {})


def test_preview_squad_powers_and_buddy_tabs(seeded_db, test_spreadsheet, clean_caches):
    sh = test_spreadsheet
    powers, buddies, presets = (
        "Preview: Squad Powers",
        "Preview: Buddy System",
        "Preview: Buddy Presets",
    )
    for title in (powers, buddies, presets):
        _fresh(sh, title)

    # Delta's row is from before #729: "Date Modified" typed as M/D/YYYY.
    ws = sh.add_worksheet(title=powers, rows=50, cols=10)
    ws.update(
        "A1",
        [
            ["Username", "Discord ID", "1st Squad Power", "Profession", "Date Modified"],
            ["Delta", DELTA, "180000000", "War Leader", "9/1/2026"],
        ],
        value_input_option="USER_ENTERED",
    )
    ws.update("B2", [[DELTA]], value_input_option="RAW")  # an ID was text then too
    # And the Buddies tab carries the old three "Name" headers.
    sh.add_worksheet(title=buddies, rows=50, cols=9).update(
        "A1", [["Discord ID", "Name", "Profession"] * 3]
    )

    sv = {"tab_squad_powers": powers, "questions": QUESTIONS}
    with (
        patch("survey._get_spreadsheet", return_value=sh),
        patch("config.get_spreadsheet", return_value=sh),
        patch("sheet_identity.settings_for", return_value=sheet_identity.Settings()),
    ):
        for did, name, power, prof in (
            (ALPHA, "Alpha", "304743912", "War Leader"),
            (BRAVO, "Bravo", "251000000", "Engineer"),
            (CHARLIE, "Charlie", "199500000", "Engineer"),
        ):
            survey.update_squad_powers(did, name, {"s1": power, "prof": prof}, TEST_GUILD_ID, sv)

        members = buddy.read_all_professions(TEST_GUILD_ID, powers, "Profession")
        result = buddy.assign_buddies(members, [])
        assert buddy.save_pairs(TEST_GUILD_ID, buddies, result, powers, "Profession")
        assert buddy.save_preset(TEST_GUILD_ID, presets, "Standard Week", result)

        pairs = buddy.load_pairs(TEST_GUILD_ID, buddies)
        preset = buddy.load_preset(TEST_GUILD_ID, presets, "Standard Week")

    # Squad Powers: full numbers, a real date, IDs intact.
    rows = sh.worksheet(powers).get_all_values()
    assert rows[0] == ["Username", "Discord ID", "1st Squad Power", "Profession", "Date Modified"]
    alpha = next(r for r in rows if r[0] == "Alpha")
    assert alpha[1] == ALPHA and alpha[2] == "304,743,912"
    assert alpha[4] != server_today().isoformat()  # shown in the locale's Date format

    with patch("config.get_member_roster_sheet", return_value=sh.worksheet(powers)):
        by_id, _names, errors = storm_roster_powers.build_last_updated_index(
            TEST_GUILD_ID, powers, 4, 1
        )
        power_by_id, _pn, _pe = storm_roster_powers.build_cross_tab_power_index(
            TEST_GUILD_ID, powers, 2, 1
        )
    assert errors == []
    assert by_id[ALPHA] == server_today() and by_id[DELTA] == date(2026, 9, 1)
    assert power_by_id[ALPHA] == 304743912

    meta = _meta(sh, powers)
    assert _frozen(meta) == (1, 1)
    assert "basicFilter" not in meta

    # Buddy System: headers say who is who, and the Profession formulas match
    # the text IDs against Squad Powers' text IDs.
    rows = sh.worksheet(buddies).get_all_values()
    assert rows[0] == buddy.BUDDY_HEADER
    by_wl = {r[1]: r for r in rows[1:]}
    assert by_wl["Alpha"][2] == "War Leader" and by_wl["Alpha"][5] == "Engineer"
    assert by_wl["Alpha"][0] == ALPHA
    assert {(p.wl_discord_id, p.eng_discord_id) for p in pairs} == {
        (p.wl_discord_id, p.eng_discord_id) for p in result.pairs
    }
    meta = _meta(sh, buddies)
    assert _frozen(meta) == (1, 2)
    assert "basicFilter" not in meta

    # Buddy Presets: the pairs read back.
    rows = sh.worksheet(presets).get_all_values()
    assert rows[0] == buddy.PRESET_HEADER
    assert {(p.wl_discord_id, p.eng_discord_id) for p in preset} == {
        (p.wl_discord_id, p.eng_discord_id) for p in result.pairs
    }
    assert _frozen(_meta(sh, presets)) == (1, 1)


def test_preview_survey_history(seeded_db, test_spreadsheet, clean_caches):
    sh = test_spreadsheet
    title = "Preview: Survey History"
    _fresh(sh, title)
    # Bravo's row is from before #729: a UTC stamp as text.
    sh.add_worksheet(title=title, rows=50, cols=10).update(
        "A1",
        [
            ["Timestamp", "Discord ID", "Username", "1st Squad Power", "Profession"],
            ["9/28/2026 22:00 UTC", BRAVO, "Bravo", "251000000", "Engineer"],
        ],
        value_input_option="RAW",
    )

    sv = {"tab_history": title, "questions": QUESTIONS}
    with (
        patch("survey._get_spreadsheet", return_value=sh),
        patch("sheet_identity.settings_for", return_value=sheet_identity.Settings()),
    ):
        survey.append_survey_history(
            ALPHA, "Alpha", {"s1": "304743912", "prof": "War Leader"}, TEST_GUILD_ID, sv
        )

    rows = sh.worksheet(title).get_all_values()
    assert rows[0][0] == "Timestamp (server time)"
    assert rows[2][1] == ALPHA and rows[2][3] == "304,743,912"
    assert "UTC" not in rows[2][0]

    def last(name, did):
        target = member_stats.Target(name=name, discord_id=int(did), joined="")
        with patch("config.get_spreadsheet", return_value=sh):
            return member_stats._last_survey_response(TEST_GUILD_ID, title, target)

    today = server_today()
    assert last("Alpha", ALPHA) == f"{today:%b} {today.day}, {today.year}"
    assert last("Bravo", BRAVO) == "Sep 28, 2026"  # 22:00 UTC is 20:00 server time

    meta = _meta(sh, title)
    assert _frozen(meta) == (1, 3)


def _member(did: str, name: str, joined: datetime):
    return SimpleNamespace(
        id=int(did), name=name.lower(), display_name=name, bot=False, roles=[], joined_at=joined
    )


def test_preview_member_roster(seeded_db, test_spreadsheet, clean_caches):
    sh = test_spreadsheet
    title = "Preview: Member Roster"
    _fresh(sh, title)
    ws = sh.add_worksheet(title=title, rows=50, cols=10)

    guild = MagicMock()
    guild.id = TEST_GUILD_ID
    guild.member_count = 3
    guild.members = [
        # 02:00 UTC on the 28th is the 27th in Los Angeles.
        _member(ALPHA, "Alpha", datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)),
        _member(BRAVO, "Bravo", datetime(2025, 3, 14, 18, 0, tzinfo=timezone.utc)),
        _member(CHARLIE, "Charlie", datetime(2024, 12, 1, 12, 0, tzinfo=timezone.utc)),
    ]
    cfg = {
        "tab_name": title,
        "enabled": 1,
        "discord_id_col": 0,
        "name_col": 1,
        "display_col": 2,
        "joined_col": 3,
        "roles_col": 4,
        "role_filter_id": 0,
    }
    with (
        patch("member_roster.get_member_roster_sheet", return_value=ws),
        patch("member_roster.get_spreadsheet", return_value=sh),
        patch(
            "member_roster.get_config", return_value=SimpleNamespace(timezone="America/Los_Angeles")
        ),
        patch("sheet_identity.settings_for", return_value=sheet_identity.Settings()),
    ):
        count, _report = member_roster.write_roster(guild, cfg)
    assert count == 3

    rows = ws.get_all_values()
    assert rows[0] == [
        "Discord ID",
        "Name",
        "Display Name",
        "Joined",
        "Roles",
        "Is this user in Discord?",
    ]
    alpha = next(r for r in rows if r[2] == "Alpha")
    assert alpha[0] == ALPHA and alpha[5] == "Yes"
    assert alpha[3] != "2026-09-27"  # shown in the locale's Date format

    # Every reader that shows or sends Joined still gets ISO.
    with (
        patch("config.get_member_roster_config", return_value=cfg),
        patch("config.get_member_roster_sheet", return_value=ws),
    ):
        api_rows = {r["name"]: r for r in member_roster.read_roster_members(TEST_GUILD_ID)}
        target = member_stats._resolve_self(TEST_GUILD_ID, int(ALPHA))
    assert api_rows["alpha"]["joined_at"] == "2026-09-27"
    assert target.joined == "2026-09-27"

    meta = _meta(sh, title)
    assert _frozen(meta) == (1, 2)
    assert "basicFilter" not in meta
    # Nobody answered the /setup question, so the ID column's visibility is left alone.
    assert not (_hidden(meta) or [False])[0]

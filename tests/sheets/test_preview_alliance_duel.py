"""An Alliance Duel (VS) tab built by the bot's own writers, left formatted
for Kevin to open (#729).

Rebuilt from scratch on every run through `alliance_duel_entry.save_rows`,
the one function every VS write goes through, then read back through
`alliance_duel_setup.load_rows`, the one every VS read goes through. Named
"Preview: ..." so the session scrub in conftest leaves it in place. Mock
data only: alliances ABC and DEF in warzone 999, a fake Discord ID.
"""

import asyncio
import datetime as _dt
from unittest.mock import patch

import pytest

import alliance_duel as ad
import alliance_duel_entry as entry
import alliance_duel_hub as hub
import alliance_duel_setup as ad_setup
import sheet_format
import sheet_identity
import sheet_tags
from tests.conftest import TEST_GUILD_ID

pytestmark = pytest.mark.sheets

TITLE = "Preview: Alliance Duel (VS)"
LEAGUE = ad.LeagueKey("S99", "Diamond", "12 - 1")
MONDAY = _dt.date(2026, 9, 28)
ABC = ad.AllianceKey.of("ABC", "999")
DEF = ad.AllianceKey.of("DEF", "999")
PICKER = "100000000000000001"


def _fresh(sh, title: str):
    """Delete the previous run's tab, so every run starts from nothing."""
    for ws in sh.worksheets():
        if ws.title == title:
            sh.del_worksheet(ws)


def _week(alliance, week, **kw):
    return ad.AllianceWeek(
        league=LEAGUE,
        week=week,
        alliance=alliance,
        week_date=MONDAY + _dt.timedelta(weeks=week - 1),
        tag_display=alliance.tag.upper(),
        warzone_display=alliance.warzone,
        **kw,
    )


def _col(name: str) -> int:
    return list(ad.SHEET_COLUMNS).index(name)


def test_preview_alliance_duel(seeded_db, test_spreadsheet, monkeypatch):
    sh = test_spreadsheet
    _fresh(sh, TITLE)
    monkeypatch.setattr(sheet_format, "_met", set())
    monkeypatch.setattr(sheet_identity, "_adopted", set())
    cfg = {"guild_id": TEST_GUILD_ID, "tab_name": TITLE, "own_tag": "ABC", "own_warzone": "999"}
    state = hub.HubState(TEST_GUILD_ID, cfg, [])

    week_one = [
        _week(
            ABC,
            1,
            ranking=1,
            power=301_000_000,
            members=98,
            gift_level=40,
            opponent=DEF,
            day_scores={1: 12_500_000, 2: 640_000_000, 6: 1_200_000_000},
            day_outcomes={1: "W", 2: "W", 3: "L", 4: "W", 5: "L", 6: "W"},
            week_score=9,
            week_outcome="W",
            intent=ad.INTENT_PUSH,
        ),
        _week(
            DEF,
            1,
            ranking=2,
            power=288_000_000,
            members=95,
            opponent=ABC,
            day_outcomes={1: "L", 2: "L", 3: "W", 4: "L", 5: "W", 6: "L"},
            week_score=4,
            week_outcome="L",
            picked="L",
            picked_by=PICKER,
        ),
    ]
    week_two = [_week(ABC, 2, ranking=1, intent=ad.INTENT_SAVE)]

    with (
        patch("config.get_spreadsheet", return_value=sh),
        patch("sheet_identity.settings_for", return_value=sheet_identity.Settings()),
    ):
        for rows in (week_one, week_two):
            assert asyncio.run(entry.save_rows(state, rows, observed=False)) == ""
        # Clearing week 2's declaration empties the cell.
        cleared = _week(ABC, 2, intent=ad.INTENT_NONE)
        assert asyncio.run(entry.save_rows(state, [cleared], observed=False)) == ""
        # A row an officer typed before #729, in the old codes.
        ws = sh.worksheet(TITLE)
        old = [""] * len(ad.SHEET_COLUMNS)
        for name, value in (
            (ad.COL_SEASON, LEAGUE.season),
            (ad.COL_TIER, LEAGUE.tier),
            (ad.COL_GROUP, LEAGUE.group),
            (ad.COL_WEEK, "2"),
            (ad.COL_TAG, "DEF"),
            (ad.COL_WARZONE, "999"),
            (ad.day_outcome_col(1), "W"),
            (ad.COL_WEEK_OUTCOME, "L"),
            (ad.COL_PICKED, "W"),
            (ad.COL_INTENT, "save"),
        ):
            old[_col(name)] = value
        ws.append_rows([old], value_input_option="RAW")

        loaded = ad_setup.load_rows(TEST_GUILD_ID, TITLE)

    # The writers formatted the tab on its first write; a second pass is a no-op.
    assert sheet_format.ensure_formatted(ws, ad.tab_format(ad.SHEET_COLUMNS)) is False

    rows = ws.get_all_values()
    assert rows[0] == list(ad.SHEET_COLUMNS)
    abc, def_, abc2 = rows[1], rows[2], rows[3]
    assert [abc[_col(ad.day_outcome_col(d))] for d in range(1, 7)] == [
        "Win",
        "Win",
        "Lose",
        "Win",
        "Lose",
        "Win",
    ]
    assert abc[_col(ad.COL_WEEK_OUTCOME)] == "Win" and def_[_col(ad.COL_WEEK_OUTCOME)] == "Lose"
    assert abc[_col(ad.COL_INTENT)] == "Push to win"
    assert abc2[_col(ad.COL_INTENT)] == ""  # declared "save", then cleared
    assert def_[_col(ad.COL_PICKED)] == "Lose"
    assert def_[_col(ad.COL_PICKED_BY)] == PICKER  # text: never rounded
    assert abc[_col(ad.COL_POWER)] == "301,000,000"
    assert abc[_col(ad.day_score_col(6))] == "1,200,000,000"
    # The Date column shows the locale's format, not the ISO text...
    assert abc[_col(ad.COL_WEEK_DATE)] != "2026-09-28"

    # ...and the bot reads every row back exactly, old codes included.
    assert loaded is not None
    by_key = {(r.alliance, r.week): r for r in loaded}
    r = by_key[(ABC, 1)]
    assert r.week_date == MONDAY and r.power == 301_000_000
    assert r.day_scores == {1: 12_500_000, 2: 640_000_000, 6: 1_200_000_000}
    assert r.day_outcomes == {1: "W", 2: "W", 3: "L", 4: "W", 5: "L", 6: "W"}
    assert (r.week_outcome, r.intent) == ("W", ad.INTENT_PUSH)
    assert by_key[(ABC, 2)].intent is None
    assert by_key[(ABC, 2)].week_date == MONDAY + _dt.timedelta(weeks=1)
    d = by_key[(DEF, 1)]
    assert (d.picked, d.picked_by, d.week_outcome) == ("L", PICKER, "L")
    typed = by_key[(DEF, 2)]
    assert (typed.day_outcomes, typed.week_outcome, typed.picked, typed.intent) == (
        {1: "W"},
        "L",
        "W",
        ad.INTENT_SAVE,
    )

    fields = (
        "sheets(properties(gridProperties(frozenRowCount,frozenColumnCount)),basicFilter(range),"
        "data(rowData(values(userEnteredFormat(numberFormat,textFormat(bold,fontSize)),"
        "dataValidation(condition(values(userEnteredValue)))))))"
    )
    last = ad.transfer.col_index_to_letter(len(ad.SHEET_COLUMNS) - 1)
    meta = sh.fetch_sheet_metadata(params={"ranges": f"'{TITLE}'!A1:{last}2", "fields": fields})[
        "sheets"
    ][0]
    grid = meta["properties"]["gridProperties"]
    assert grid.get("frozenRowCount") == 1 and not grid.get("frozenColumnCount")
    assert "basicFilter" not in meta  # filters are the alliance's to add
    header_row, first_row = meta["data"][0]["rowData"][:2]
    header = header_row["values"][0]["userEnteredFormat"]
    assert header["textFormat"]["bold"] is True and header["textFormat"]["fontSize"] == 14
    cells = first_row["values"]

    def number_format(name):
        return (cells[_col(name)].get("userEnteredFormat") or {}).get("numberFormat") or {}

    def options(name):
        values = cells[_col(name)]["dataValidation"]["condition"]["values"]
        return [v["userEnteredValue"] for v in values]

    assert number_format(ad.COL_WEEK_DATE)["type"] == "DATE"
    assert number_format(ad.COL_POWER)["pattern"] == "#,##0"
    assert number_format(ad.COL_PICKED_BY)["type"] == "TEXT"
    assert options(ad.COL_WEEK_OUTCOME) == ["Win", "Lose"]
    assert options(ad.COL_PICKED) == ["Win", "Lose"]
    assert options(ad.COL_INTENT) == ["Push to win", "Save for a later week"]

    tags = sheet_tags.tags_for_sheet(sheet_tags.read_column_tags(sh), ws)
    assert sheet_identity.is_id_tag(tags.get(_col(ad.COL_PICKED_BY)) or {})

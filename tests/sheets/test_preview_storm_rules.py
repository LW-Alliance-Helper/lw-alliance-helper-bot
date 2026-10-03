"""The storm Member Rules and Strategies tabs, built by the bot's own writers
and left formatted, for Kevin to open (#729).

Same shape as `test_preview_tabs.py`: each test rebuilds one "Preview: ..."
tab from scratch through the functions the bot calls in production, then
checks the words, the numbers and the house style, and that the bot's own
readers read it back. Mock data only: Alpha, Bravo, Charlie.
"""

from unittest.mock import patch

import pytest

import config
import sheet_format
import sheet_identity
import storm_member_rules as smr
import storm_strategy as ss
from tests.conftest import TEST_GUILD_ID

pytestmark = pytest.mark.sheets

MEMBERS = [
    ("100000000000000001", "Alpha"),
    ("100000000000000002", "Bravo"),
    ("100000000000000003", "Charlie"),
]

RULES_TAB = "Preview: DS Member Rules"
STRATEGIES_TAB = "Preview: DS Strategies"


def _fresh(sh, title: str):
    """Delete the previous run's tab, so every run starts from nothing."""
    for ws in sh.worksheets():
        if ws.title == title:
            sh.del_worksheet(ws)


def _meta(sh, title: str) -> dict:
    fields = (
        "sheets(properties(gridProperties(frozenRowCount,frozenColumnCount)),basicFilter(range),"
        "data(rowData(values(userEnteredFormat(backgroundColor,textFormat(bold,fontSize))))))"
    )
    return sh.fetch_sheet_metadata(params={"ranges": f"'{title}'!A1:N2", "fields": fields})[
        "sheets"
    ][0]


@pytest.fixture
def storm_tabs(seeded_db):
    """Point the guild's DS rules and strategies tabs at the preview tabs."""
    config.save_storm_config(
        TEST_GUILD_ID,
        "DS",
        tab_name="DS Tab",
        mail_template="x",
        timezone="America/New_York",
        log_channel_id=0,
    )
    config.save_structured_storm_config(
        TEST_GUILD_ID,
        "DS",
        structured_flow_enabled=True,
        strategies_tab=STRATEGIES_TAB,
        member_rules_tab=RULES_TAB,
    )


def test_preview_ds_member_rules(storm_tabs, test_spreadsheet, monkeypatch):
    sh = test_spreadsheet
    _fresh(sh, RULES_TAB)
    monkeypatch.setattr(sheet_format, "_met", set())
    monkeypatch.setattr(sheet_identity, "_found", {})
    roster = sheet_identity.build_roster([(did, name, "") for did, name in MEMBERS])

    with (
        patch("config.get_spreadsheet", return_value=sh),
        patch("sheet_identity.load_roster", return_value=roster),
        patch("sheet_identity.settings_for", return_value=sheet_identity.Settings()),
    ):
        # A row from before #729, in codes, to be reworded by the next save.
        assert smr.list_rules(TEST_GUILD_ID, "DS") == []
        sh.worksheet(RULES_TAB).append_row(
            ["per_member", "Charlie", "zone", "Arsenal", ""], value_input_option="RAW"
        )
        for rule in (
            smr.Rule("power_band", "250000000", "Nuclear Silo", notes="Top floor"),
            smr.Rule("per_member", "Alpha", "A", sub_type="team"),
            smr.Rule("per_member", "Bravo", "Info Center", sub_type="zone"),
        ):
            ok, msg = smr.save_rule(TEST_GUILD_ID, "DS", rule)
            assert ok, msg

        ws = sh.worksheet(RULES_TAB)
        assert sheet_format.ensure_formatted(ws, smr.RULES_FORMAT) is False

        rows = ws.get_all_values()
        assert rows[0][:5] == smr._HEADER and rows[0][5] == "Discord ID"
        assert [r[0] for r in rows[1:]] == ["Per member", "Power band", "Per member", "Per member"]
        assert [r[2] for r in rows[1:]] == ["Zone", "", "Team", "Zone"]
        assert rows[3][5] == "100000000000000001"

        rules = smr.list_rules(TEST_GUILD_ID, "DS")
    assert [(r.rule_type, r.sub_type, r.value) for r in rules] == [
        ("per_member", "zone", "Arsenal"),
        ("power_band", "", "Nuclear Silo"),
        ("per_member", "team", "A"),
        ("per_member", "zone", "Info Center"),
    ]
    assert rules[2].subject == "100000000000000001"  # matched by Discord ID

    meta = _meta(sh, RULES_TAB)
    grid = meta["properties"]["gridProperties"]
    assert (grid.get("frozenRowCount"), grid.get("frozenColumnCount", 0)) == (1, 0)
    assert "basicFilter" not in meta
    header = meta["data"][0]["rowData"][0]["values"][0]["userEnteredFormat"]
    assert header["textFormat"]["bold"] is True and header["textFormat"]["fontSize"] == 14


def test_preview_ds_strategies(storm_tabs, test_spreadsheet, monkeypatch):
    sh = test_spreadsheet
    _fresh(sh, STRATEGIES_TAB)
    monkeypatch.setattr(sheet_format, "_met", set())

    with patch("config.get_spreadsheet", return_value=sh):
        assert ss.save_preset(
            TEST_GUILD_ID,
            "DS",
            ss.PresetBuffer(
                name="Standard",
                event_type="DS",
                zones=[
                    ss.ZoneRow("Nuclear Silo", max_players=4, min_power_a=300_000_000, priority=1),
                    ss.ZoneRow(
                        "Oil Refinery I",
                        max_players=4,
                        min_power_a=250_000_000,
                        min_power_b=180_000_000,
                        priority=2,
                    ),
                ],
            ),
        )
        assert ss.save_preset(
            TEST_GUILD_ID,
            "DS",
            ss.PresetBuffer(
                name="Two Stages",
                event_type="DS",
                phase_count=2,
                zones=[
                    ss.ZoneRow(
                        "Info Center",
                        max_phase1=4,
                        max_phase2=2,
                        min_power_a=1_200_000_000,
                        priority_phase1=1,
                        priority_phase2=2,
                    ),
                ],
            ),
        )

        ws = sh.worksheet(STRATEGIES_TAB)
        assert sheet_format.ensure_formatted(ws, ss.DS_FORMAT) is False

        shown = ws.get_all_values()
        assert shown[0] == ss._DS_HEADER
        assert [r[12] for r in shown[1:]] == ["Flat", "Flat", "2 Stages (S1 + S2)"]
        assert shown[1][6] == "300,000,000" and shown[3][6] == "1,200,000,000"
        raw = ws.get_all_values(value_render_option="UNFORMATTED_VALUE")
        assert raw[1][6] == 300_000_000 and raw[1][2] == 4

        standard = ss.load_preset(TEST_GUILD_ID, "DS", "Standard")
        staged = ss.load_preset(TEST_GUILD_ID, "DS", "Two Stages")
        names = ss.list_strategies(TEST_GUILD_ID, "DS")
        zone_rules = ss.zone_rules_for(TEST_GUILD_ID, "DS", "Standard")
    assert [z.min_power_a for z in standard.zones] == [300_000_000, 250_000_000]
    assert standard.phase_count == 0
    assert staged.phase_count == 2 and staged.zones[0].max_phase1 == 4
    assert staged.zones[0].min_power_a == 1_200_000_000
    assert [n["name"] for n in names] == ["Standard", "Two Stages"]
    assert zone_rules[1]["min_b"] == 180_000_000

    meta = _meta(sh, STRATEGIES_TAB)
    grid = meta["properties"]["gridProperties"]
    assert (grid.get("frozenRowCount"), grid.get("frozenColumnCount")) == (1, 1)
    assert "basicFilter" not in meta
    header = meta["data"][0]["rowData"][0]["values"][0]["userEnteredFormat"]
    assert header["textFormat"]["bold"] is True and header["textFormat"]["fontSize"] == 14

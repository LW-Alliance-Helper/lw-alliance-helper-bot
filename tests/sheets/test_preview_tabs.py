"""Tabs built by the bot's own writers and left formatted, for Kevin to open (#729).

Each test rebuilds one tab in the test spreadsheet from scratch, through the
same functions the bot calls in production, then runs the house-style pass
on it. The tabs are named "Preview: ..." rather than "_test_...", so the
session scrub in conftest leaves them in place between runs. Mock data only:
Alpha, Bravo, alliance ABC.
"""

from unittest.mock import patch

import pytest

import sheet_format
import sheet_identity
import train_rotation as tr
from tests.conftest import TEST_GUILD_ID

pytestmark = pytest.mark.sheets

MEMBERS = [
    ("100000000000000001", "Alpha"),
    ("100000000000000002", "Bravo"),
    ("100000000000000003", "Charlie"),
    ("100000000000000004", "Delta"),
    ("100000000000000005", "Echo"),
    ("100000000000000006", "Foxtrot"),
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
    return sh.fetch_sheet_metadata(params={"ranges": f"'{title}'!A1:H2", "fields": fields})[
        "sheets"
    ][0]


def test_preview_train_history(seeded_db, test_spreadsheet, monkeypatch):
    title = "Preview: Train History"
    sh = test_spreadsheet
    _fresh(sh, title)
    monkeypatch.setattr(sheet_format, "_met", set())
    monkeypatch.setattr(sheet_identity, "_found", {})
    roster = sheet_identity.build_roster([(did, name, "") for did, name in MEMBERS])

    with (
        patch("config.get_spreadsheet", return_value=sh),
        patch("sheet_identity.load_roster", return_value=roster),
        patch("sheet_identity.settings_for", return_value=sheet_identity.Settings()),
    ):
        # Written out of date order on purpose: the tab sorts itself.
        for day, member, reason, posted_at, notes in (
            ("2026-09-28", "Alpha", "auto", "2026-09-28T22:00:05+00:00", ""),
            ("2026-09-29", "Bravo", "vs", "2026-09-29T22:00:12+00:00", ""),
            ("2026-10-01", "Delta", "leadership", "2026-10-01T22:00:03+00:00", ""),
            ("2026-09-30", "Charlie", "birthday", "2026-09-30T22:01:40+00:00", "birthday 🎂"),
        ):
            assert tr.set_day_status(
                TEST_GUILD_ID,
                title,
                day,
                member=member,
                reason=reason,
                status=tr.STATUS_POSTED,
                posted_at=posted_at,
                notes=notes,
            )
        assert tr.write_draft_rows(
            TEST_GUILD_ID,
            title,
            [
                tr.DraftDay("2026-10-02", 4, tr.RULE_AUTO, "Echo", "auto"),
                tr.DraftDay("2026-10-03", 5, tr.RULE_VS, None, "vs", needs_picking=True),
                tr.DraftDay("2026-10-04", 6, tr.RULE_CONTEST, "Foxtrot", "contest"),
            ],
        )

    ws = sh.worksheet(title)
    assert sheet_format.ensure_formatted(ws, tr.HISTORY_FORMAT) is True

    rows = ws.get_all_values()
    assert [r[1] for r in rows[1:]] == ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "", "Foxtrot"]
    assert rows[0][6] == "Discord ID" and rows[1][6] == "100000000000000001"

    meta = _meta(sh, title)
    grid = meta["properties"]["gridProperties"]
    assert (grid.get("frozenRowCount"), grid.get("frozenColumnCount")) == (1, 2)
    assert "basicFilter" in meta
    header = meta["data"][0]["rowData"][0]["values"][0]["userEnteredFormat"]
    assert header["textFormat"]["bold"] is True and header["textFormat"]["fontSize"] == 14
    hidden = [bool(c.get("hiddenByUser")) for c in meta["data"][0]["columnMetadata"]]
    assert hidden[6] is True  # Discord ID, hidden by default

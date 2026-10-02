"""Discord ID columns against the real Sheets API (#723).

What only a live sheet can show: the column the bot adds is found again by
its tag after an officer moves it, it is hidden by default, and an 18-19
digit ID typed into it the way the bot writes rows (`USER_ENTERED`) comes
back exactly, not rounded to a number.
"""

import time

import sheet_identity
from tests.conftest import TEST_GUILD_ID

REAL_ID = "1234567890123456789"


def _hidden_columns(sh, title: str) -> list[bool]:
    meta = sh.fetch_sheet_metadata(
        params={"ranges": f"'{title}'", "fields": "sheets(data(columnMetadata(hiddenByUser)))"}
    )
    columns = meta["sheets"][0]["data"][0].get("columnMetadata", [])
    return [bool(c.get("hiddenByUser")) for c in columns]


def test_the_id_column_is_tagged_hidden_and_keeps_real_ids(seeded_db, fresh_tab, monkeypatch):
    monkeypatch.setattr(sheet_identity, "_found", {})
    ws = fresh_tab
    sh = ws.spreadsheet
    ws.update("A1", [["Member", "Rule Type"], ["Alpha", "opt_out"]])

    idx = sheet_identity.ensure_column(ws, ["Member", "Rule Type"], guild_id=TEST_GUILD_ID)
    assert idx == 2
    ws.update("C2", [[REAL_ID]], value_input_option="USER_ENTERED")
    time.sleep(1.0)

    assert ws.get_all_values()[:2] == [
        ["Member", "Rule Type", "Discord ID"],
        ["Alpha", "opt_out", REAL_ID],
    ]
    assert _hidden_columns(sh, ws.title)[2] is True  # hidden: nobody answered /setup

    # An officer drags the column to the front. The tag moves with it.
    sh.batch_update(
        {
            "requests": [
                {
                    "moveDimension": {
                        "source": {
                            "sheetId": ws.id,
                            "dimension": "COLUMNS",
                            "startIndex": 2,
                            "endIndex": 3,
                        },
                        "destinationIndex": 0,
                    }
                }
            ]
        }
    )
    time.sleep(1.0)
    monkeypatch.setattr(sheet_identity, "_found", {})
    header = ws.row_values(1)
    assert sheet_identity.locate_column(ws, header) == 0


def test_apply_visibility_shows_the_columns(seeded_db, fresh_tab, monkeypatch):
    from unittest.mock import patch

    monkeypatch.setattr(sheet_identity, "_found", {})
    ws = fresh_tab
    ws.update("A1", [["Member"]])
    sheet_identity.ensure_column(ws, ["Member"], guild_id=TEST_GUILD_ID)
    time.sleep(1.0)
    assert _hidden_columns(ws.spreadsheet, ws.title)[1] is True

    with patch("config.get_spreadsheet", return_value=ws.spreadsheet):
        assert sheet_identity.apply_visibility(TEST_GUILD_ID, shown=True) >= 1
    time.sleep(1.0)
    assert _hidden_columns(ws.spreadsheet, ws.title)[1] is False

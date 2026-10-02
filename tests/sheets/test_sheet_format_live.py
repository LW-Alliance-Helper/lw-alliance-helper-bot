"""The house-style pass against the real Sheets API (#729).

The question only a live sheet answers: a filter set over the whole sheet
still covers a row added after it, including one past the grid's original
last row, so a sort or filter never strands a row the bot appends later.
The formatted tab is left in the test spreadsheet to look at.
"""

import time

import sheet_format


def _filter_range(sh, title: str) -> dict:
    meta = sh.fetch_sheet_metadata(
        params={"ranges": f"'{title}'", "fields": "sheets(basicFilter(range))"}
    )
    return meta["sheets"][0]["basicFilter"]["range"]


def test_the_filter_covers_rows_added_later(fresh_tab, monkeypatch):
    monkeypatch.setattr(sheet_format, "_met", set())
    ws = fresh_tab  # 200 rows
    sh = ws.spreadsheet
    ws.update("A1", [["Date", "Member", "Power"], ["2026-10-01", "Alpha", 123456789]])
    spec = sheet_format.TabSpec(quantity=(2,), frozen_columns=2)
    assert sheet_format.ensure_formatted(ws, spec) is True

    ws.append_rows([["2026-10-02", "Bravo", 5000]] * 3, value_input_option="USER_ENTERED")
    ws.add_rows(50)
    ws.update("A240", [["2026-10-03", "Charlie", 7]], value_input_option="USER_ENTERED")
    time.sleep(1.0)

    rng = _filter_range(sh, ws.title)
    assert rng.get("startRowIndex", 0) == 0
    assert "endRowIndex" not in rng or rng["endRowIndex"] >= 240

    # Quantities read with separators, and a second call does nothing.
    assert ws.acell("C2").value == "123,456,789"
    assert sheet_format.ensure_formatted(ws, spec) is False


def test_a_tab_already_styled_is_left_alone(fresh_tab, monkeypatch):
    monkeypatch.setattr(sheet_format, "_met", set())
    ws = fresh_tab
    ws.update("A1", [["Date", "Member"]])
    ws.format("A1:B1", {"textFormat": {"bold": True}})
    assert sheet_format.ensure_formatted(ws, sheet_format.TabSpec()) is False
    monkeypatch.setattr(sheet_format, "_met", set())
    assert sheet_format.ensure_formatted(ws, sheet_format.TabSpec()) is False  # marked

"""The house-style pass against the real Sheets API (#729).

What only a live sheet answers: a filter an officer added survives the pass
and the bot's writes after it, since filters are the alliance's to add and
the bot's to leave alone (Kevin, 2026-10-03). The formatted tab is left in
the test spreadsheet to look at.
"""

import time

import pytest

import sheet_format

pytestmark = pytest.mark.sheets


def _filter(sh, title: str):
    meta = sh.fetch_sheet_metadata(
        params={"ranges": f"'{title}'", "fields": "sheets(basicFilter(range))"}
    )
    return meta["sheets"][0].get("basicFilter")


def test_an_officers_filter_survives_the_pass_and_later_writes(fresh_tab, monkeypatch):
    monkeypatch.setattr(sheet_format, "_met", set())
    ws = fresh_tab
    sh = ws.spreadsheet
    ws.update("A1", [["Date", "Member", "Power"], ["2026-10-01", "Alpha", 123456789]])
    ws.set_basic_filter("A1:C2")  # the officer's own
    spec = sheet_format.TabSpec(quantity=(2,), frozen_columns=2)
    assert sheet_format.ensure_formatted(ws, spec) is True

    # The kinds of write the bot makes: clear-and-rewrite, and appends.
    ws.batch_clear(["A2:C100"])
    ws.update("A2", [["2026-10-02", "Bravo", 5000]], value_input_option="USER_ENTERED")
    ws.append_rows([["2026-10-03", "Charlie", 7]], value_input_option="USER_ENTERED")
    time.sleep(1.0)

    flt = _filter(sh, ws.title)
    assert flt is not None
    assert flt["range"].get("endColumnIndex") == 3

    # Quantities read with separators, and a second call does nothing.
    assert ws.acell("C2").value == "5,000"
    assert sheet_format.ensure_formatted(ws, spec) is False


def test_the_pass_adds_no_filter(fresh_tab, monkeypatch):
    monkeypatch.setattr(sheet_format, "_met", set())
    ws = fresh_tab
    ws.update("A1", [["Date", "Member"]])
    assert sheet_format.ensure_formatted(ws, sheet_format.TabSpec()) is True
    time.sleep(1.0)
    assert _filter(ws.spreadsheet, ws.title) is None


def test_a_tab_already_styled_is_left_alone(fresh_tab, monkeypatch):
    monkeypatch.setattr(sheet_format, "_met", set())
    ws = fresh_tab
    ws.update("A1", [["Date", "Member"]])
    ws.format("A1:B1", {"textFormat": {"bold": True}})
    assert sheet_format.ensure_formatted(ws, sheet_format.TabSpec()) is False
    monkeypatch.setattr(sheet_format, "_met", set())
    assert sheet_format.ensure_formatted(ws, sheet_format.TabSpec()) is False  # marked

"""Decision-column data-validation request builder (#16). The bot creates the
decision column's checkbox / dropdown validation, so the GridRange it targets
must cover the column's data rows (row 2 to the grid's last row)."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import transfer_sheets


class _FakeWS:
    id = 12345
    row_count = 1000


def test_data_validation_request_targets_the_column_data_range():
    req = transfer_sheets._data_validation_request(_FakeWS(), 4, {"condition": {"type": "BOOLEAN"}})
    rng = req["setDataValidation"]["range"]
    assert rng["sheetId"] == 12345
    assert rng["startRowIndex"] == 1  # 0-based; skips the header row
    assert rng["endRowIndex"] == 1000
    assert rng["startColumnIndex"] == 4
    assert rng["endColumnIndex"] == 5
    assert req["setDataValidation"]["rule"] == {"condition": {"type": "BOOLEAN"}}


class _RenameWS:
    def __init__(self, header):
        self._header = header
        self.updated = None

    def row_values(self, _row):
        return self._header

    def update_cell(self, row, col, value):
        self.updated = (row, col, value)


class _RenameSH:
    def __init__(self, ws):
        self._ws = ws

    def worksheet(self, _tab):
        return self._ws


def _patch_sheet(monkeypatch, ws):
    import config

    monkeypatch.setattr(config, "get_spreadsheet_by_id", lambda _sid: _RenameSH(ws))


def test_rename_column_in_place(monkeypatch):
    ws = _RenameWS(["Name", "Confirmed", "Status"])
    _patch_sheet(monkeypatch, ws)
    assert transfer_sheets.rename_column("s", "t", "confirmed", "Approved") == "renamed"
    assert ws.updated == (1, 2, "Approved")  # header cell of the 2nd column (1-based)


def test_rename_column_not_found_leaves_sheet_untouched(monkeypatch):
    ws = _RenameWS(["Name", "Status"])
    _patch_sheet(monkeypatch, ws)
    assert transfer_sheets.rename_column("s", "t", "Confirmed", "Approved") == "not_found"
    assert ws.updated is None


def test_rename_column_collision_refused(monkeypatch):
    ws = _RenameWS(["Name", "Confirmed", "Approved"])
    _patch_sheet(monkeypatch, ws)
    # renaming Confirmed -> Approved would duplicate an existing header
    assert transfer_sheets.rename_column("s", "t", "Confirmed", "Approved") == "collision"
    assert ws.updated is None

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

"""Transfer Management (#16) — Google Sheets I/O.

A thin gspread layer the setup wizard and the poll loop share. Kept apart
from ``transfer.py`` (pure logic) so that module stays Discord/gspread-free
and trivially testable, and apart from ``config.py`` so the transfer sheets
(which the guild doesn't own) don't get tangled with the guild's main sheet
helpers.

All reads/writes target an *arbitrary* sheet by ID via
``config.get_spreadsheet_by_id`` — a transfer sheet, a server-wide pool, or
an intake-form output, none of which are the guild's configured spreadsheet.
Functions raise on open/read/write failure; callers surface a friendly
message via ``config.describe_sheet_error``.
"""

from __future__ import annotations

import config


def read_sheet(sheet_id: str, tab: str) -> tuple[list, list]:
    """Return ``(header_row, data_rows)`` for a sheet tab: row 1 is the
    header, rows 2+ are data. An empty tab yields ``([], [])``. Raises on
    open/read failure (bad ID, no access, missing tab)."""
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    values = ws.get_all_values()
    if not values:
        return [], []
    return values[0], values[1:]


def read_header(sheet_id: str, tab: str) -> list:
    """Just the header row (row 1) of a sheet tab. Used by the wizard to
    drive auto-mapping without pulling every data row."""
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    return ws.row_values(1)


def append_rows(sheet_id: str, tab: str, rows: list) -> None:
    """Append whole rows to a sheet tab (the source-copy path writes the
    *complete* source row, every column). No-op on an empty list. Uses
    ``USER_ENTERED`` so values land as the recruiter would type them (numbers
    stay numbers, not forced text)."""
    if not rows:
        return
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    ws.append_rows(rows, value_input_option="USER_ENTERED")


def write_cell(
    sheet_id: str, tab: str, row_index_1based: int, col_index_0based: int, value
) -> None:
    """Write a single cell — the decision write-back path (e.g. set a Want?
    cell to ``TRUE``). ``row_index_1based`` includes the header row (so the
    first data row is 2); ``col_index_0based`` is a 0-based column index that
    we shift to gspread's 1-based column. ``update_cell`` sends
    ``USER_ENTERED``, so ``"TRUE"`` lands as a real boolean and a checkbox
    column ticks."""
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    ws.update_cell(row_index_1based, col_index_0based + 1, value)


def _norm(value) -> str:
    return str(value or "").strip().casefold()


def ensure_columns(sheet_id: str, tab: str, headers_to_ensure: list) -> list:
    """Make sure each header in ``headers_to_ensure`` exists in row 1; append
    any missing one as a new column at the end (growing the grid if needed).
    Returns the updated header row. Used by the decision setup so the bot can
    create the decision column it's about to add a checkbox/dropdown to."""
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    header = ws.row_values(1)
    have = {_norm(h) for h in header}
    to_add = [h for h in headers_to_ensure if h and _norm(h) not in have]
    if not to_add:
        return header
    if ws.col_count < len(header) + len(to_add):
        ws.add_cols(len(header) + len(to_add) - ws.col_count)
    for offset, h in enumerate(to_add):
        ws.update_cell(1, len(header) + 1 + offset, h)
    return header + to_add


def rename_column(sheet_id: str, tab: str, old_header: str, new_header: str) -> str:
    """Rename a column's header in place (row 1), keeping the column's data and
    validation. Returns ``"renamed"``, ``"not_found"`` (the old header isn't on
    the sheet), or ``"collision"`` (a *different* column already uses the new
    name — refused so we don't create a duplicate header)."""
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    header = ws.row_values(1)
    norm_old, norm_new = _norm(old_header), _norm(new_header)
    target = next((i for i, h in enumerate(header) if _norm(h) == norm_old), None)
    if target is None:
        return "not_found"
    if any(i != target and _norm(h) == norm_new for i, h in enumerate(header)):
        return "collision"
    ws.update_cell(1, target + 1, new_header)
    return "renamed"


def _data_validation_request(ws, col_0based: int, rule: dict) -> dict:
    """A ``setDataValidation`` request covering a column's data rows (row 2 to
    the grid's last row), for the worksheet's grid id."""
    return {
        "setDataValidation": {
            "range": {
                "sheetId": ws.id,
                "startRowIndex": 1,  # 0-based; row 2 (skip the header)
                "endRowIndex": ws.row_count,
                "startColumnIndex": col_0based,
                "endColumnIndex": col_0based + 1,
            },
            "rule": rule,
        }
    }


def set_checkbox_validation(sheet_id: str, tab: str, col_0based: int) -> None:
    """Apply checkbox (BOOLEAN) data validation to a column's data rows, so the
    bot's ``TRUE``/``FALSE`` writes render as a ticked/unticked checkbox.
    ``strict=False`` leaves any existing non-boolean cells untouched."""
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    rule = {"condition": {"type": "BOOLEAN"}, "showCustomUi": True, "strict": False}
    sh.batch_update({"requests": [_data_validation_request(ws, col_0based, rule)]})


def set_dropdown_validation(sheet_id: str, tab: str, col_0based: int, options: list) -> None:
    """Apply a single-select dropdown (ONE_OF_LIST) to a column's data rows,
    with chips for ``options``. ``strict=False`` so existing values that aren't
    in the list aren't flagged."""
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    rule = {
        "condition": {
            "type": "ONE_OF_LIST",
            "values": [{"userEnteredValue": str(o)} for o in options if str(o).strip()],
        },
        "showCustomUi": True,
        "strict": False,
    }
    sh.batch_update({"requests": [_data_validation_request(ws, col_0based, rule)]})

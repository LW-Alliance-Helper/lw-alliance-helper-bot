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
    we shift to gspread's 1-based column."""
    sh = config.get_spreadsheet_by_id(sheet_id)
    ws = sh.worksheet(tab)
    ws.update_cell(row_index_1based, col_index_0based + 1, value)

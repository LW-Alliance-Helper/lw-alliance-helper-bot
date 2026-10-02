"""One formatting pass for every tab the bot creates (#729).

A tab the bot writes is a user-facing surface: an officer opens it and has to
read it without help. The house style is the design contract's (DESIGN.md,
Sheet tabs), taken from Kevin's own alliance sheet:

- Header row: Google Sheets "dark gray 4" fill, white bold text, size 14,
  wrapped rather than widening the column.
- Body: size 14, black, left-aligned.
- Frozen header row, and the name column where the tab has one.
- A filter on every header, set over the whole sheet so a row added later is
  always inside it.
- Quantities `#,##0`; identifiers plain text; dates and times in Sheets'
  built-in Date / Time / Date time formats, so each alliance's locale
  renders them.

**Once per tab, never over an officer's styling.** The pass runs the first
time the bot writes a tab after this shipped, and marks the tab with a
sheet-level developer-metadata tag so it never runs there again: an officer
who restyles the tab afterwards keeps their styling. A tab whose header row
already carries styling when the pass first meets it was styled by someone,
and is marked and left alone. Tabs the alliance made are never passed in.

Best-effort like `sheet_tags`: a failed search or format logs and leaves the
tab as it was, to be tried again after a restart. Formatting never fails a
write.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

MARK_KEY = "lwah.tab"
STYLE_VERSION = 1

_SEARCH_URL = "https://sheets.googleapis.com/v4/spreadsheets/{id}/developerMetadata:search"

HEADER_FILL = {"red": 67 / 255, "green": 67 / 255, "blue": 67 / 255}  # dark gray 4, #434343
WHITE = {"red": 1, "green": 1, "blue": 1}
BLACK = {"red": 0, "green": 0, "blue": 0}
FONT_SIZE = 14

NUMBER_FORMATS = {
    "quantity": {"type": "NUMBER", "pattern": "#,##0"},
    "text": {"type": "TEXT"},
    "date": {"type": "DATE"},
    "time": {"type": "TIME"},
    "datetime": {"type": "DATE_TIME"},
}


@dataclass(frozen=True)
class TabSpec:
    """What a tab's columns hold, by 0-based index, for the formats that
    depend on it. Every other column takes the body style and no number
    format. `frozen_columns` counts from the left: 2 freezes A and B, for a
    tab whose name column is B."""

    quantity: tuple[int, ...] = ()
    text: tuple[int, ...] = ()
    date: tuple[int, ...] = ()
    time: tuple[int, ...] = ()
    datetime: tuple[int, ...] = ()
    frozen_columns: int = 0


def format_requests(sheet_id: int, spec: TabSpec) -> list[dict]:
    """The `batchUpdate` requests that put one tab in the house style."""
    requests: list[dict] = [
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "backgroundColor": HEADER_FILL,
                        "textFormat": {
                            "foregroundColor": WHITE,
                            "bold": True,
                            "fontSize": FONT_SIZE,
                        },
                        "wrapStrategy": "WRAP",
                        "verticalAlignment": "MIDDLE",
                    }
                },
                "fields": (
                    "userEnteredFormat(backgroundColor,textFormat,wrapStrategy,verticalAlignment)"
                ),
            }
        },
        {
            "repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 1},
                "cell": {
                    "userEnteredFormat": {
                        "textFormat": {"foregroundColor": BLACK, "fontSize": FONT_SIZE},
                        "horizontalAlignment": "LEFT",
                    }
                },
                "fields": "userEnteredFormat(textFormat,horizontalAlignment)",
            }
        },
    ]
    for kind, columns in (
        ("quantity", spec.quantity),
        ("text", spec.text),
        ("date", spec.date),
        ("time", spec.time),
        ("datetime", spec.datetime),
    ):
        for col in columns:
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "startColumnIndex": col,
                            "endColumnIndex": col + 1,
                        },
                        "cell": {"userEnteredFormat": {"numberFormat": NUMBER_FORMATS[kind]}},
                        "fields": "userEnteredFormat.numberFormat",
                    }
                }
            )
    requests.append(
        {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id,
                    "gridProperties": {
                        "frozenRowCount": 1,
                        "frozenColumnCount": spec.frozen_columns,
                    },
                },
                "fields": "gridProperties.frozenRowCount,gridProperties.frozenColumnCount",
            }
        }
    )
    # No row or column bounds: the filter covers the whole sheet, so a row
    # the bot (or an officer) adds later is never stranded outside it.
    requests.append({"setBasicFilter": {"filter": {"range": {"sheetId": sheet_id}}}})
    return requests


def mark_request(sheet_id: int, *, formatted: bool) -> dict:
    """The tag that says the pass has met this tab. `formatted` False means it
    found the tab already styled and left it alone."""
    value = {"style": STYLE_VERSION, "formatted": formatted}
    return {
        "createDeveloperMetadata": {
            "developerMetadata": {
                "metadataKey": MARK_KEY,
                "metadataValue": json.dumps(value, separators=(",", ":"), sort_keys=True),
                "location": {"sheetId": sheet_id},
                "visibility": "PROJECT",
            }
        }
    }


def parse_marks(body) -> set[int]:
    """Sheet ids carrying the pass's mark, from a `developerMetadata:search`."""
    out: set[int] = set()
    if not isinstance(body, dict):
        return out
    for match in body.get("matchedDeveloperMetadata") or []:
        meta = (match or {}).get("developerMetadata") or {}
        if meta.get("metadataKey") != MARK_KEY:
            continue
        sheet_id = (meta.get("location") or {}).get("sheetId")
        if isinstance(sheet_id, int):
            out.add(sheet_id)
    return out


def header_is_styled(body) -> bool:
    """True when a header-row read (see `_read_header_format`) shows a fill or
    bold text: somebody styled this tab, and the pass leaves it as it is."""
    try:
        values = body["sheets"][0]["data"][0]["rowData"][0]["values"]
    except (KeyError, IndexError, TypeError):
        return False
    for cell in values or []:
        fmt = (cell or {}).get("userEnteredFormat") or {}
        fill = fmt.get("backgroundColor") or {}
        if fill and any(fill.get(c, 1) != 1 for c in ("red", "green", "blue")):
            return True
        if (fmt.get("textFormat") or {}).get("bold"):
            return True
    return False


# Tabs already met in this process, so a write doesn't search every time.
_met: set[tuple] = set()


def ensure_formatted(ws, spec: TabSpec, *, sh=None) -> bool:
    """Put a bot-created tab in the house style, once. Returns True when this
    call formatted it."""
    sh = sh or getattr(ws, "spreadsheet", None)
    sheet_id = getattr(ws, "id", None)
    if sh is None or not isinstance(sheet_id, int):
        return False
    key = (getattr(sh, "id", None), sheet_id)
    if key in _met:
        return False
    try:
        resp = sh.client.request(
            "post",
            _SEARCH_URL.format(id=sh.id),
            json={"dataFilters": [{"developerMetadataLookup": {"metadataKey": MARK_KEY}}]},
        )
        marked = parse_marks(resp.json())
    except Exception as e:
        print(f"[SHEET FORMAT] Could not read tab marks: {e}")
        return False
    if sheet_id in marked:
        _met.add(key)
        return False
    styled = _read_header_format(sh, ws)
    if styled is None:
        return False
    requests = [] if styled else format_requests(sheet_id, spec)
    requests.append(mark_request(sheet_id, formatted=not styled))
    try:
        sh.batch_update({"requests": requests})
    except Exception as e:
        print(f"[SHEET FORMAT] Could not format '{getattr(ws, 'title', '?')}': {e}")
        return False
    _met.add(key)
    return not styled


def _read_header_format(sh, ws) -> bool | None:
    """Whether the header row is already styled; None when it can't be read."""
    title = getattr(ws, "title", "")
    try:
        body = sh.fetch_sheet_metadata(
            params={
                "ranges": f"'{title}'!1:1",
                "fields": (
                    "sheets(data(rowData(values(userEnteredFormat(backgroundColor,textFormat(bold))))))"
                ),
            }
        )
    except Exception as e:
        print(f"[SHEET FORMAT] Could not read the header of '{title}': {e}")
        return None
    return header_is_styled(body)

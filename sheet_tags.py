"""Hidden labels on the columns the bot owns in an alliance's Sheet (#668).

The bot used to find its columns by their header text, so a header had to
stay a long machine key (`Aug 2026 - Sep 2026 1st Squad Power %`) and a person
who renamed, shortened or moved one broke the reader. A column the bot owns
now carries a Sheets developer-metadata tag: invisible, kept when the column
is moved, renamed or restyled, and deleted along with the column. Readers
resolve columns by tag first and fall back to the header text only for
columns written before tags existed; the writers tag those the next time they
touch the tab, so a tab converts itself on its first write.

Tags are PROJECT-visible: only the bot's own Google Cloud project can read or
change them, so nothing an alliance or another add-on does can collide.

One key for every column the bot owns; the value is a small JSON object saying
what the column is. `t` names the tab's role (`growth`, `breakdown`), `k` the
column's kind within it, and the rest is whatever that tab needs to tell its
columns apart (a metric and a period, say). Owning modules define their tag
shapes; this module only stores and finds them.

Everything here is best-effort. A failed search reads as "no tags", which puts
readers back on header text, the behaviour before this existed; a failed tag
write logs and leaves the column to be tagged on the next write.
"""

from __future__ import annotations

import json

TAG_KEY = "lwah.column"

_SEARCH_URL = "https://sheets.googleapis.com/v4/spreadsheets/{id}/developerMetadata:search"


def read_column_tags(sh) -> dict[int, dict[int, dict]]:
    """Every tagged column in the spreadsheet, as `{sheet_id: {column: tag}}`.

    One API call covers every tab, so a caller touching two tabs of the same
    spreadsheet reads once. `column` is the 0-based index where the column
    sits now, which is not necessarily where the bot wrote it.
    """
    try:
        resp = sh.client.request(
            "post",
            _SEARCH_URL.format(id=sh.id),
            json={"dataFilters": [{"developerMetadataLookup": {"metadataKey": TAG_KEY}}]},
        )
        body = resp.json()
    except Exception as e:
        print(f"[SHEET_TAGS] Could not read column tags: {e}")
        return {}
    return parse_search_response(body)


def parse_search_response(body) -> dict[int, dict[int, dict]]:
    """Shape a `developerMetadata:search` response into `{sheet_id: {column: tag}}`.

    Skips anything that isn't a single tagged column with a JSON object value,
    so a malformed or foreign entry can never be mistaken for a bot column.
    """
    out: dict[int, dict[int, dict]] = {}
    if not isinstance(body, dict):
        return out
    for match in body.get("matchedDeveloperMetadata") or []:
        meta = (match or {}).get("developerMetadata") or {}
        if meta.get("metadataKey") != TAG_KEY:
            continue
        dim = ((meta.get("location") or {}).get("dimensionRange")) or {}
        if dim.get("dimension") != "COLUMNS":
            continue
        start, end = dim.get("startIndex", 0), dim.get("endIndex")
        if not isinstance(start, int) or end != start + 1:
            continue
        try:
            tag = json.loads(meta.get("metadataValue") or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(tag, dict):
            continue
        sheet_id = dim.get("sheetId", 0)
        out.setdefault(sheet_id, {})[start] = tag
    return out


def tags_for_sheet(all_tags: dict[int, dict[int, dict]], ws) -> dict[int, dict]:
    """The tags on one worksheet's columns, from a `read_column_tags` result."""
    if not all_tags:
        return {}
    try:
        return dict(all_tags.get(ws.id) or {})
    except (AttributeError, TypeError):
        return {}


def create_requests(sheet_id: int, tags: dict[int, dict]) -> list[dict]:
    """`batchUpdate` requests attaching each of `tags` (`{column: tag}`)."""
    return [
        {
            "createDeveloperMetadata": {
                "developerMetadata": {
                    "metadataKey": TAG_KEY,
                    "metadataValue": json.dumps(tag, separators=(",", ":"), sort_keys=True),
                    "location": {
                        "dimensionRange": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": col,
                            "endIndex": col + 1,
                        }
                    },
                    "visibility": "PROJECT",
                }
            }
        }
        for col, tag in sorted(tags.items())
    ]


def tag_columns(sh, ws, tags: dict[int, dict]) -> None:
    """Attach `tags` (`{column: tag}`) to `ws`'s columns in one call.

    Call it only for columns that carry no tag yet: a second tag on the same
    column is harmless to readers (the last one read wins) but is clutter.
    """
    if not tags:
        return
    try:
        sh.batch_update({"requests": create_requests(ws.id, tags)})
    except Exception as e:
        title = getattr(ws, "title", "?")
        print(f"[SHEET_TAGS] Could not tag {len(tags)} column(s) on '{title}': {e}")


def ensure_columns(ws, needed: int) -> None:
    """Widen `ws` to at least `needed` columns before writing past its edge.

    The bot creates its tabs 50 columns wide, and each snapshot or breakdown
    adds more, so a long-running tab outgrows the grid it was created with.
    """
    count = getattr(ws, "col_count", None)
    if isinstance(count, int) and isinstance(needed, int) and count < needed:
        ws.add_cols(needed - count)

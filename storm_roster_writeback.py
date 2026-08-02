"""Write an MM-built DS/CS plan to the storm `rosters_tab` (#316 write-back).

Map Manager's rebuilt Storm Planner posts a finished event roster to
`POST /api/guilds/:id/sheet/storm-roster`; this module persists it to the bot's
`rosters_tab` so the normal storm flow (officer view, attendance) can read it.

The contract (handoff §6.1): MM sends `{ event_type, event_date, assignments }`
where each assignment is `{ member_name, discord_id?, team, role }` plus `zone`
(primaries only) and `stage` (CS primaries only). The bot fills `Power at
Assignment` from the configured power source and writes a row per assignment,
**only if that (event_type, date) has no rows yet** (or `overwrite=True`).

Row-building is a pure function (`build_rows`) so the column mapping is unit
tested without Sheets; `write_mm_storm_roster` does the I/O (if-empty check,
power lookup, append). Identity merge key is `discord_id` then `member_name`.
"""

from __future__ import annotations

# The columns the bot writes for a fresh rosters_tab. The bot's readers address
# columns by header NAME (not position), so an existing tab's own header order
# is honored on append; this is only used when the tab has no header yet.
DEFAULT_ROSTER_HEADER = [
    "Event Date",
    "Team",
    "Stage",
    "Zone",
    "Member",
    "Role",
    "Power at Assignment",
    "Discord ID",
]


def _power_for(assignment: dict, power_index: dict) -> str:
    """Look up the member's power from the storm power index, by discord_id then
    name. Returns "" when unknown (the index has no entry, or power is None)."""
    discord_id = assignment.get("discord_id")
    key = str(discord_id) if discord_id else (assignment.get("member_name") or "")
    info = power_index.get(key) or {}
    power = info.get("power")
    return "" if power is None else str(power)


def assignment_to_fields(event_date: str, assignment: dict, power_index: dict) -> dict:
    """Map one MM assignment to `{column_name: value}` for the rosters_tab.

    Subs carry no zone/stage (MM omits those keys for `role: "sub"`); CS
    primaries carry a numeric stage, DS none. Power is filled from the index.
    """
    is_sub = assignment.get("role") == "sub"
    discord_id = assignment.get("discord_id")
    stage = assignment.get("stage")
    return {
        "Event Date": event_date,
        "Team": assignment.get("team") or "",
        "Stage": "" if (is_sub or stage is None) else str(stage),
        "Zone": "" if is_sub else (assignment.get("zone") or ""),
        "Member": assignment.get("member_name") or "",
        "Role": assignment.get("role") or "primary",
        "Power at Assignment": _power_for(assignment, power_index),
        "Discord ID": str(discord_id) if discord_id else "",
    }


def build_rows(
    header: list[str], event_date: str, assignments: list[dict], power_index: dict
) -> list[list[str]]:
    """Build rosters_tab rows aligned to `header` (so the bot's name-addressed
    readers pick up each column; unknown header columns are left blank)."""
    rows: list[list[str]] = []
    for assignment in assignments:
        fields = assignment_to_fields(event_date, assignment, power_index)
        rows.append([fields.get(col.strip(), "") for col in header])
    return rows


def _read_power_index(guild_id: int, event_type: str) -> dict:
    """The storm power index `{key: {power, ...}}` (key = discord_id or name).
    Degrades to `{}` so a power-source misconfig leaves Power blank rather than
    failing the whole write."""
    try:
        import storm_roster_builder

        index, _errors = storm_roster_builder._read_roster_powers(guild_id, event_type)
        return index or {}
    except Exception as e:  # noqa: BLE001
        print(f"[STORM WRITEBACK] power index read failed (guild {guild_id}): {e}")
        return {}


def _delete_date_rows(ws, all_values: list[list[str]], header: list[str], event_date: str) -> None:
    """Delete existing rows for `event_date` (used on overwrite).

    Merges the matching row indices into contiguous ranges and issues one
    `delete_rows(start, end)` call per range instead of one call per row
    (#366) — a 20-60+ row event roster (primaries + subs across two teams,
    up to three phases) would otherwise issue 20-60+ sequential Sheets API
    calls on every re-sync, a real quota risk. Ranges are deleted bottom-up
    so earlier deletes don't shift the row numbers of ranges still pending.
    """
    try:
        date_idx = header.index("Event Date")
    except ValueError:
        date_idx = 0
    to_delete = [
        i
        for i, row in enumerate(all_values[1:], start=2)  # row 1 = header
        if date_idx < len(row) and row[date_idx].strip() == event_date
    ]
    if not to_delete:
        return

    ranges: list[tuple[int, int]] = []
    range_start = range_end = to_delete[0]
    for idx in to_delete[1:]:
        if idx == range_end + 1:
            range_end = idx
            continue
        ranges.append((range_start, range_end))
        range_start = range_end = idx
    ranges.append((range_start, range_end))

    for start, end in reversed(ranges):
        ws.delete_rows(start, end)


def write_mm_storm_roster(
    guild_id: int,
    event_type: str,
    event_date: str,
    assignments: list[dict],
    *,
    overwrite: bool = False,
) -> dict:
    """Persist an MM storm plan to `rosters_tab`. `event_type` is "ds"/"cs".

    Returns `{ "written": bool, "rows": int, "skipped_reason"?: str }`. Skips
    (without writing) when the date already has rows and `overwrite` is False.
    Blocking (gspread) — call via `asyncio.to_thread`.
    """
    import config
    import storm_history

    et = event_type.upper()  # bot uses "DS" / "CS"
    existing, _errors = storm_history.load_event_roster(guild_id, et, event_date)
    if existing and not overwrite:
        return {"written": False, "rows": 0, "skipped_reason": "date_has_data"}

    tab = storm_history._rosters_tab_name(guild_id, et)
    sh = config.get_spreadsheet(guild_id)
    ws = config.get_or_create_worksheet(sh, tab)
    all_values = ws.get_all_values()

    if all_values and all_values[0]:
        header = [c.strip() for c in all_values[0]]
    else:
        header = list(DEFAULT_ROSTER_HEADER)
        ws.update("A1", [header], value_input_option="USER_ENTERED")

    if overwrite and existing:
        _delete_date_rows(ws, all_values, header, event_date)

    power_index = _read_power_index(guild_id, et)
    rows = build_rows(header, event_date, assignments, power_index)
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")
    return {"written": True, "rows": len(rows)}

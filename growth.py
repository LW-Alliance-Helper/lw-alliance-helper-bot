"""
growth.py — Configurable per-guild growth snapshots.

Snapshots fire at 22:00 ET on the day picked in `/setup_growth`
(monthly day-of-month, or every-N-days from a fixed anchor). The
snapshot reads each member's metric values (one column per metric,
configured per guild) from the source tab and appends them as a new
period column to the growth tab.

The source tab, name column, data start row, and metric columns are
all per-guild config. Nothing in here is hardcoded to a particular
sheet layout — see `guild_growth_config` in `config.py`.
"""

import os
import json
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Snapshots fire at 22:00 ET (10pm) — matches bot.growth_task. Single source
# of truth so compute_next_snapshot stays in sync with the scheduler.
SNAPSHOT_FIRE_HOUR_ET = 22

# Anchor for interval-based schedules: every N days from this date. Matches
# the epoch baked into bot.growth_task.
INTERVAL_EPOCH = date(2026, 1, 1)


def compute_next_snapshot(gcfg: dict, now: datetime | None = None) -> datetime | None:
    """Compute the next scheduled snapshot datetime, in America/New_York.

    Returns None if growth tracking isn't enabled. Otherwise returns the
    next datetime at which `bot.growth_task` will actually fire — i.e.
    22:00 ET on:
      * monthly  → the next occurrence of day == snapshot_day (1–28)
      * interval → the next date where (date - INTERVAL_EPOCH).days is a
                   multiple of snapshot_interval

    `now` is injectable for tests; defaults to the real current time. If
    a naive datetime is passed it's interpreted as ET to keep the
    semantics consistent with the scheduler.
    """
    if not gcfg.get("enabled"):
        return None

    if now is None:
        now = datetime.now(tz=ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    today = now.date()
    freq  = gcfg.get("snapshot_frequency", "monthly")

    if freq == "monthly":
        # Stored value is always clamped to 1..28 by the wizard, so we can
        # rely on the date being valid in every month.
        day = max(1, min(28, int(gcfg.get("snapshot_day", 1))))
        candidate = today.replace(day=day)
        if candidate < today or (
            candidate == today and now.hour >= SNAPSHOT_FIRE_HOUR_ET
        ):
            year, month = today.year, today.month + 1
            if month > 12:
                year, month = year + 1, 1
            candidate = date(year, month, day)
        return datetime(
            candidate.year, candidate.month, candidate.day,
            SNAPSHOT_FIRE_HOUR_ET, 0, tzinfo=ET,
        )

    if freq == "interval":
        interval = max(1, int(gcfg.get("snapshot_interval", 30)))
        delta    = (today - INTERVAL_EPOCH).days
        remainder = delta % interval
        if remainder == 0 and now.hour < SNAPSHOT_FIRE_HOUR_ET:
            candidate = today
        else:
            candidate = today + timedelta(days=interval - remainder)
        return datetime(
            candidate.year, candidate.month, candidate.day,
            SNAPSHOT_FIRE_HOUR_ET, 0, tzinfo=ET,
        )

    return None




def _get_spreadsheet(guild_id: int = None):
    """Return an authenticated gspread Spreadsheet object."""
    from config import get_spreadsheet
    return get_spreadsheet(guild_id)


def _safe_float(val: str) -> float:
    """Parse a string to float, returning 0.0 if blank or invalid."""
    try:
        return float(str(val).strip()) if val and str(val).strip() else 0.0
    except ValueError:
        return 0.0


def load_member_data(guild_id: int = None) -> list[dict]:
    """
    Load member data from the configured source tab for growth tracking.
    Returns a list of { "name": str, "row_index": int, ...metric_key: value }
    using the column configuration from guild_growth_config.
    """
    from config import get_growth_config
    gcfg       = get_growth_config(guild_id)
    tab_source = gcfg.get("tab_source", "")
    name_col   = gcfg.get("name_col", "A")
    metrics    = gcfg.get("metrics", [])
    start_row  = gcfg.get("data_start_row", 2)

    if not tab_source or not metrics:
        print(f"[GROWTH] No source tab or metrics configured for guild {guild_id}")
        return []

    try:
        sh   = _get_spreadsheet(guild_id)
        ws   = sh.worksheet(tab_source)
        rows = ws.get_all_values()

        name_idx    = ord(name_col.upper()) - ord('A')
        metric_idxs = {m["label"]: ord(m["col"].upper()) - ord('A') for m in metrics}

        members = []
        for i, row in enumerate(rows[start_row - 1:], start=start_row):
            if len(row) <= name_idx or not row[name_idx].strip():
                continue
            entry = {"name": row[name_idx].strip(), "row_index": i}
            for label, idx in metric_idxs.items():
                entry[label] = _safe_float(row[idx]) if len(row) > idx else 0.0
            members.append(entry)

        print(f"[GROWTH] Loaded {len(members)} members from '{tab_source}' for guild {guild_id}")
        return members
    except Exception as e:
        print(f"[GROWTH] Error loading member data for guild {guild_id}: {e}")
        return []


def run_growth_snapshot():
    """
    Take a snapshot for all configured guilds that have growth tracking enabled.
    """
    import traceback, sqlite3
    from config import DB_PATH
    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT guild_id FROM guild_configs WHERE setup_complete = 1"
            ).fetchall()
        guild_ids = [row[0] for row in rows] or [None]
    except Exception as e:
        print(f"[GROWTH] Could not read guild list: {e}")
        guild_ids = [None]

    for gid in guild_ids:
        try:
            _run_growth_snapshot_inner(gid)
        except Exception as e:
            err_str = str(e)
            if "WorksheetNotFound" in type(e).__name__ or "WorksheetNotFound" in err_str:
                print(f"[GROWTH] Skipping guild {gid} — sheet tab not found. Configure via /setup_growth.")
            else:
                print(f"[GROWTH] Snapshot failed for guild {gid}: {e}")
                print(f"[GROWTH] Traceback:\n{traceback.format_exc()}")


def _run_growth_snapshot_inner(guild_id: int = None):
    from config import get_config, get_growth_config
    cfg   = get_config(guild_id)
    gcfg  = get_growth_config(guild_id)

    # Skip if growth tracking not enabled or configured
    if not gcfg.get("enabled"):
        return
    if not cfg or not cfg.spreadsheet_id:
        print(f"[GROWTH] Skipping guild {guild_id} — no sheet configured")
        return
    if not gcfg.get("tab_source") or not gcfg.get("tab_growth") or not gcfg.get("metrics"):
        print(f"[GROWTH] Skipping guild {guild_id} — growth tracking not fully configured. Run /setup_growth.")
        return

    import gspread
    now         = datetime.now(tz=ET)
    month_label = now.strftime("%b %Y")

    # Check if snapshot already exists for this period
    sh  = _get_spreadsheet(guild_id)
    tab_growth = gcfg["tab_growth"]
    try:
        ws = sh.worksheet(tab_growth)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_growth, rows=500, cols=50)
        print(f"[GROWTH] Created growth tracking tab '{tab_growth}' for guild {guild_id}")

    existing_headers = ws.row_values(1) if ws.row_count > 0 else []
    metric_labels    = [m["label"] for m in gcfg["metrics"]]

    # Build column headers: Name, then one column per metric per snapshot period
    # Check if this period's columns already exist
    period_cols = [h for h in existing_headers if h.endswith(f"({month_label})")]
    if len(period_cols) >= len(metric_labels):
        print(f"[GROWTH] Snapshot for {month_label} already exists — skipping")
        return

    print(f"[GROWTH] Running snapshot for {month_label} (guild {guild_id})")

    members = load_member_data(guild_id)
    if not members:
        print(f"[GROWTH] No member data found for guild {guild_id}")
        return

    all_values = ws.get_all_values()

    # Ensure header row has Name column
    if not all_values or not all_values[0]:
        ws.update("A1", [["Name"]], value_input_option="USER_ENTERED")
        all_values = [["Name"]]

    # Add new metric columns for this period
    header_row  = all_values[0] if all_values else []
    new_headers = [f"{label} ({month_label})" for label in metric_labels]

    for new_header in new_headers:
        if new_header not in header_row:
            header_row.append(new_header)

    # Write updated header
    ws.update("A1", [header_row], value_input_option="USER_ENTERED")

    # Build name → row index map
    name_to_row = {}
    for i, row in enumerate(all_values[1:], start=2):
        if row and row[0].strip():
            name_to_row[row[0].strip().lower()] = i

    # Write data rows
    updates = []
    new_member_rows = []
    for member in members:
        name     = member["name"]
        row_idx  = name_to_row.get(name.lower())

        if row_idx is None:
            # Reserve a row for this new member; the actual sheet append is
            # batched into one call after the loop so a roster of 60+ members
            # doesn't exhaust the 60/min Sheets write quota (#40).
            new_row = [name] + [""] * (len(header_row) - 1)
            row_idx = len(all_values) + 1
            new_member_rows.append(new_row)
            all_values.append(new_row)
            name_to_row[name.lower()] = row_idx
            print(f"[GROWTH] New member added: {name}")

        # Write each metric value into its column
        for label in metric_labels:
            col_name = f"{label} ({month_label})"
            if col_name in header_row:
                col_idx   = header_row.index(col_name)
                col_letter = chr(ord('A') + col_idx)
                val        = member.get(label, "")
                updates.append({
                    "range": f"{col_letter}{row_idx}",
                    "values": [[val]],
                })

    if new_member_rows:
        ws.append_rows(new_member_rows, value_input_option="USER_ENTERED")

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    print(f"[GROWTH] Snapshot complete for {month_label} — {len(members)} members (guild {guild_id})")


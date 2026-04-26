"""
growth.py — Monthly squad power growth tracking

Runs on the 1st of every month at 10pm ET (server reset).
Also runs once immediately on first deploy to establish a baseline.

Process:
  1. Read all members from the Squad Powers tab (Discord ID, Username, 1st/2nd/3rd Squad)
  2. Calculate combined squad power per member (1st + 2nd + 3rd)
  3. Open the Growth Tracking tab
  4. Match members by Discord ID — update existing rows, add new rows
  5. Append a new "Combined Power MMM YYYY" column with this month's totals
  6. If a previous month's column exists, append a "% Growth MMM-MMM YYYY" column

Sheet structure (Growth Tracking tab):
  Col A: Discord ID
  Col B: Name (kept current)
  Col C+: alternating Combined Power and % Growth columns per month
"""

import os
import json
from datetime import datetime, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")



# Column indices in Squad Powers (0-based)
SP_USERNAME_COL   = 0   # A
SP_DISCORD_ID_COL = 1   # B
SP_SQUAD1_COL     = 2   # C
SP_SQUAD2_COL     = 4   # E
SP_SQUAD3_COL     = 6   # G


def _get_spreadsheet(guild_id: int = None):
    """Return an authenticated gspread Spreadsheet object."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        info  = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds    = Credentials.from_service_account_file(key_file, scopes=scopes)

    gc = gspread.authorize(creds)
    from config import get_spreadsheet_id
    sheet_id = get_spreadsheet_id(guild_id)
    return gc.open_by_key(sheet_id)


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

    now         = datetime.now(tz=ET)
    month_label = now.strftime("%b %Y")
    col_header  = month_label

    # Check if snapshot already exists for this period
    sh  = _get_spreadsheet(guild_id)
    tab_growth = gcfg["tab_growth"]
    try:
        ws = sh.worksheet(tab_growth)
    except Exception:
        # Create the tab if it doesn't exist
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
    for member in members:
        name     = member["name"]
        row_idx  = name_to_row.get(name.lower())

        if row_idx is None:
            # Append new row
            new_row = [name] + [""] * (len(header_row) - 1)
            ws.append_row(new_row, value_input_option="USER_ENTERED")
            row_idx = len(all_values) + 1
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

    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    print(f"[GROWTH] Snapshot complete for {month_label} — {len(members)} members (guild {guild_id})")


def run_growth_snapshot():
    """
    Take a monthly snapshot for all configured guilds.
    Only runs for guilds that have growth tracking configured (tab names set).
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
            # Catch missing sheet tab gracefully — guild hasn't set up growth tracking yet
            err_str = str(e)
            if "WorksheetNotFound" in type(e).__name__ or "WorksheetNotFound" in err_str:
                print(f"[GROWTH] Skipping guild {gid} — sheet tab not found. Run /setup to configure growth tracking.")
            else:
                print(f"[GROWTH] Snapshot failed for guild {gid}: {e}")
                print(f"[GROWTH] Traceback:\n{traceback.format_exc()}")


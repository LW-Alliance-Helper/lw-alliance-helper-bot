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


def _run_growth_snapshot_inner(guild_id: int = None):
    from config import get_config
    cfg = get_config(guild_id)

    # Skip if this guild hasn't configured their sheet tabs yet
    if not cfg or not cfg.spreadsheet_id:
        print(f"[GROWTH] Skipping guild {guild_id} — no sheet configured")
        return
    if not cfg.tab_growth_tracking or not cfg.tab_squad_powers:
        print(f"[GROWTH] Skipping guild {guild_id} — growth tracking tabs not configured")
        return

    now         = datetime.now(tz=ET)
    month_label = now.strftime("%b %Y")
    col_header  = f"Combined Power\n{month_label}"

    print(f"[GROWTH] Running snapshot for {month_label} (guild {guild_id})")

    sh      = _get_spreadsheet(guild_id)
    ws      = sh.worksheet(cfg.tab_growth_tracking if cfg else "Growth Tracking")
    members = load_squad_powers(guild_id)

    # Read entire Growth Tracking sheet
    all_values = ws.get_all_values()

    # ── Parse existing headers ─────────────────────────────────────────────────
    # Row 1 is headers. Cols A=Discord ID, B=Name, C+ = data columns
    headers = all_values[0] if all_values else []

    # Check if this month's column already exists — avoid double-writing
    # Strip whitespace and newlines for comparison
    clean_headers = [h.replace("\n", " ").strip() for h in headers]
    this_month_clean = col_header.replace("\n", " ").strip()

    if this_month_clean in clean_headers:
        print(f"[GROWTH] Snapshot for {month_label} already exists — skipping")
        return

    # Find the index of the previous Combined Power column (last one before this)
    prev_combined_col_idx = None
    for i, h in enumerate(clean_headers):
        if h.startswith("Combined Power"):
            prev_combined_col_idx = i  # keep updating — we want the last one

    # ── Build Discord ID → row index map ──────────────────────────────────────
    # Row 1 = headers (index 0), data starts at index 1
    id_to_row = {}  # discord_id → 0-based index in all_values
    for i, row in enumerate(all_values[1:], start=1):
        if row and row[0].strip():
            id_to_row[row[0].strip()] = i

    # ── Determine new column positions ────────────────────────────────────────
    # New combined power column goes at the end
    new_col_idx = len(headers)  # 0-based index of new column in the sheet

    # If there's a previous combined power column, we also add a % growth column
    add_growth_col = prev_combined_col_idx is not None

    # ── Prepare updates ───────────────────────────────────────────────────────
    import gspread

    # We'll build a full batch of cell updates
    updates = []

    # Write the new Combined Power header
    new_col_letter = _col_letter(new_col_idx)
    updates.append({
        "range": f"{new_col_letter}1",
        "values": [[col_header]],
    })

    if add_growth_col:
        # % Growth column goes right after the new combined power column
        growth_col_idx    = new_col_idx + 1
        growth_col_letter = _col_letter(growth_col_idx)
        prev_month_label  = clean_headers[prev_combined_col_idx].replace("Combined Power", "").strip()
        growth_header     = f"% Growth\n{prev_month_label}-{month_label}"
        updates.append({
            "range": f"{growth_col_letter}1",
            "values": [[growth_header]],
        })

    # ── Write data for existing members ───────────────────────────────────────
    rows_to_append = []

    processed_ids = set()

    for discord_id, member in members.items():
        processed_ids.add(discord_id)
        combined = member["combined"]
        name     = member["name"]

        if discord_id in id_to_row:
            row_idx    = id_to_row[discord_id]
            sheet_row  = row_idx + 1  # 1-based sheet row number
            existing   = all_values[row_idx]

            # Update name in col B (in case it changed)
            updates.append({
                "range": f"B{sheet_row}",
                "values": [[name]],
            })

            # Write new combined power
            updates.append({
                "range": f"{new_col_letter}{sheet_row}",
                "values": [[combined]],
            })

            # Calculate % growth if we have a previous column
            if add_growth_col:
                prev_val_raw = existing[prev_combined_col_idx] if len(existing) > prev_combined_col_idx else ""
                prev_val     = _safe_float(prev_val_raw)
                if prev_val > 0:
                    pct = round((combined - prev_val) / prev_val * 100, 2)
                else:
                    pct = 0.0
                updates.append({
                    "range": f"{growth_col_letter}{sheet_row}",
                    "values": [[pct]],
                })
        else:
            # New member — will be appended below
            rows_to_append.append((discord_id, name, combined))

    # ── Append new members ─────────────────────────────────────────────────────
    # Find the next empty row
    next_row = len(all_values) + 1

    for discord_id, name, combined in rows_to_append:
        # Build a full row padded out to the new combined power column
        row_data = [discord_id, name]
        # Pad with empty strings for any columns between B and the new combined column
        while len(row_data) < new_col_idx:
            row_data.append("")
        row_data.append(combined)
        if add_growth_col:
            row_data.append(0.0)  # 0% growth for brand new members

        updates.append({
            "range": f"A{next_row}",
            "values": [row_data],
        })
        print(f"[GROWTH] New member added to Growth Tracking: {name} ({discord_id})")
        next_row += 1

    # ── Execute all updates in one batch ──────────────────────────────────────
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
        print(f"[GROWTH] Snapshot complete for {month_label} — "
              f"{len(members)} members updated, {len(rows_to_append)} new members added")
    else:
        print(f"[GROWTH] No updates to write for {month_label}")


def _col_letter(idx: int) -> str:
    """Convert a 0-based column index to a spreadsheet column letter (A, B, ... Z, AA, AB...)."""
    result = ""
    idx += 1  # make 1-based
    while idx > 0:
        idx, remainder = divmod(idx - 1, 26)
        result = chr(65 + remainder) + result
    return result

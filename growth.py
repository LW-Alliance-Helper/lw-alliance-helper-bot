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


def load_squad_powers(guild_id: int = None) -> dict:
    """
    Load current squad powers from the Squad Powers tab.
    Returns { discord_id: { "name": str, "combined": float } }
    """
    from config import get_config
    cfg  = get_config(guild_id)
    sh   = _get_spreadsheet(guild_id)
    ws   = sh.worksheet(cfg.tab_squad_powers if cfg else "Squad Powers")
    rows = ws.get_all_values()

    members = {}
    for row in rows[1:]:  # skip header
        if len(row) <= SP_DISCORD_ID_COL or not row[SP_DISCORD_ID_COL].strip():
            continue

        discord_id = row[SP_DISCORD_ID_COL].strip()
        username   = row[SP_USERNAME_COL].strip() if len(row) > SP_USERNAME_COL else ""
        squad1     = _safe_float(row[SP_SQUAD1_COL] if len(row) > SP_SQUAD1_COL else "")
        squad2     = _safe_float(row[SP_SQUAD2_COL] if len(row) > SP_SQUAD2_COL else "")
        squad3     = _safe_float(row[SP_SQUAD3_COL] if len(row) > SP_SQUAD3_COL else "")
        combined   = round(squad1 + squad2 + squad3, 2)

        members[discord_id] = {"name": username, "combined": combined}

    print(f"[GROWTH] Loaded {len(members)} members from Squad Powers")
    return members


def run_growth_snapshot():
    """
    Take a monthly snapshot for all configured guilds.
    This is the main entry point called by the scheduler.
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
            print(f"[GROWTH] Snapshot failed for guild {gid}: {e}")
            print(f"[GROWTH] Traceback:\n{traceback.format_exc()}")


def _run_growth_snapshot_inner(guild_id: int = None):
    from config import get_config
    cfg  = get_config(guild_id)
    now        = datetime.now(tz=ET)
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

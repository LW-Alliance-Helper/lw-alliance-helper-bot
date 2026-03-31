import os
import json
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv

load_dotenv()

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
SHEET_NAME     = os.getenv("SHEET_NAME", "Sheet1")

# Column order must exactly match your Google Sheet headers
# Username (col A) is handled separately — preserved on update, set on insert
COLUMN_ORDER = [
    "Discord ID",
    "1st Squad",
    "1st Squad Type",
    "2nd Squad",
    "2nd Squad Type",
    "3rd Squad",
    "3rd Squad Type",
    "Gorilla Level",
    "Drone Level",
    "Date Modified",
]


def get_sheet():
    """Authenticate and return the target worksheet.

    Supports two credential methods:
    1. GOOGLE_CREDENTIALS_JSON env var — paste the entire JSON as a single env var (Railway)
    2. GOOGLE_SERVICE_ACCOUNT_FILE env var — path to a local JSON file (local dev)
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    # Method 1: full JSON stored as environment variable (Railway / cloud)
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        info = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        # Method 2: path to local JSON file (local development)
        key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds = Credentials.from_service_account_file(key_file, scopes=scopes)

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(SHEET_NAME)


def find_and_update_or_create_row(data: dict) -> str:
    """
    Look up the Discord ID in column B (index 1).
    - If found: update that row with the new data.
    - If not found: append a new row.

    Returns a string describing what action was taken.
    """
    sheet = get_sheet()
    discord_id = str(data["Discord ID"])

    # Fetch all values to search for the Discord ID
    all_values = sheet.get_all_values()

    # Row 1 is headers, data starts at row 2 (index 1 in all_values)
    target_row_index = None  # 1-based sheet row number
    for i, row in enumerate(all_values[1:], start=2):  # start=2 → sheet row number
        # Discord ID is column B → index 1
        if len(row) > 1 and str(row[1]).strip() == discord_id:
            target_row_index = i
            break

    # Build values for columns B–K (Discord ID through Date Modified)
    # Column A (Username) is handled separately
    data_values = [str(data.get(col, "")) for col in COLUMN_ORDER]

    if target_row_index is not None:
        # Update columns B–K only — leave column A (Username) untouched
        cell_range = f"B{target_row_index}:K{target_row_index}"
        sheet.update(cell_range, [data_values], value_input_option="USER_ENTERED")
        return f"Updated row {target_row_index} for Discord ID {discord_id}"
    else:
        # New row — prepend Username in column A since it doesn't exist yet
        username = str(data.get("Username", ""))
        full_row = [username] + data_values
        sheet.append_row(full_row, value_input_option="USER_ENTERED")
        return f"Created new row for Discord ID {discord_id} ({username})"
"""
train_birthdays.py — Birthday roster + schedule integration.

Loads birthdays from the configured member sheet, places them into the
train schedule with conflict avoidance, and exposes the birthday lookahead
window. Pure logic — no Discord types — so it can be imported by both train.py
and the train cog without circular import concerns.
"""

import json
import os
import re
from datetime import date, timedelta


def _get_member_sheet_inner(tab_name: str, guild_id: int = None):
    """Return the active member worksheet (gspread Worksheet)."""
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
    sh = gc.open_by_key(get_spreadsheet_id(guild_id))
    return sh.worksheet(tab_name)


# Minimal month-name lookup used by parse_birthday.
_BIRTHDAY_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}



# ── Birthday sheet config ──────────────────────────────────────────────────────
#
# The active member tab name is stored in cell H1 of the Train Schedule tab.
# G1 contains the label "Active Member Tab:" for human readability.
# Default value if not set: "Season 5 - Off-Season"

DEFAULT_MEMBER_TAB = "Season 5 - Off-Season"
BIRTHDAY_LOOKAHEAD = 14  # default, overridden per-guild by database

def get_birthday_lookahead(guild_id: int = None) -> int:
    """Return the birthday lookahead days for a guild."""
    from config import get_birthday_config
    cfg = get_birthday_config(guild_id) if guild_id else {}
    return cfg.get("lookahead_days", 14)

def load_birthdays(tab_name: str, guild_id: int = None) -> list[dict]:
    """
    Load all members with birthdays from the member sheet.
    Column indices and data start row come from guild birthday config.
    Returns a list of { "name": str, "month": int, "day": int }
    """
    from config import get_birthday_config
    bcfg         = get_birthday_config(guild_id) if guild_id else {}
    name_col     = bcfg.get("name_col", 4)
    bday_col     = bcfg.get("birthday_col", 8)
    start_row    = bcfg.get("data_start_row", 10)
    min_cols     = max(name_col, bday_col) + 1
    try:
        ws   = _get_member_sheet_inner(tab_name, guild_id)
        rows = ws.get_all_values()
        members = []
        for row in rows[start_row - 1:]:
            if len(row) < min_cols:
                continue
            name     = row[name_col].strip()
            bday_raw = row[bday_col].strip()
            if not name or not bday_raw:
                continue
            parsed = parse_birthday(bday_raw)
            if parsed:
                entry = {"name": name, "month": parsed[0], "day": parsed[1]}
                # Include Discord ID if configured and available
                if "discord_id_col" in bcfg and bcfg["discord_id_col"] >= 0:
                    did_col = bcfg["discord_id_col"]
                    if len(row) > did_col and row[did_col].strip():
                        entry["discord_id"] = row[did_col].strip()
                members.append(entry)
        print(f"[BIRTHDAY] Loaded {len(members)} members with birthdays from '{tab_name}'")
        return members
    except Exception as e:
        print(f"[BIRTHDAY] Error loading birthdays from '{tab_name}': {e}")
        return []

def get_member_tab_name(guild_id: int = None) -> str:
    """Get the active member tab name from the config database."""
    from config import get_member_tab
    if guild_id is None:
        guild_id = None
    tab = get_member_tab(guild_id)
    return tab if tab else "Season 5 - Off-Season"

def parse_birthday(raw: str) -> tuple[int, int] | None:
    """
    Parse a birthday string into (month, day), ignoring the year.
    Handles formats like: 'December 7', 'December 7, 1990', '12/7', '12/7/1990'
    Returns None if unparseable.
    """
    if not raw or not raw.strip():
        return None

    raw = raw.strip()

    # Numeric: 12/7 or 12/7/1990
    numeric = re.match(r"^(\d{1,2})/(\d{1,2})(?:/\d+)?$", raw)
    if numeric:
        try:
            return int(numeric.group(1)), int(numeric.group(2))
        except ValueError:
            return None

    # Month name: December 7 or December 7, 1990
    named = re.match(
        r"^([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*\d+)?$",
        raw, re.IGNORECASE,
    )
    if named:
        month = _BIRTHDAY_MONTH_MAP.get(named.group(1).lower())
        if month:
            try:
                return month, int(named.group(2))
            except ValueError:
                return None

    return None

def check_and_add_birthdays(schedule: dict, guild_id: int = None) -> tuple[dict, list[str]]:
    """
    Look ahead lookahead_days from today (from guild birthday config).
    Uses configured tab, name column, and birthday column.
    """
    from config import get_birthday_config
    bcfg       = get_birthday_config(guild_id) if guild_id else {}
    if not bcfg.get("enabled", 0) and guild_id:
        return schedule, []
    if not bcfg.get("train_integration", 1) and guild_id:
        return schedule, []

    tab_name  = bcfg.get("tab_name") or get_member_tab_name(guild_id)
    lookahead = bcfg.get("lookahead_days", BIRTHDAY_LOOKAHEAD)
    members   = load_birthdays(tab_name, guild_id)
    if not members:
        return schedule, []

    today       = date.today()
    check_year  = today.year
    added_count = 0
    alerts      = []

    for member in members:
        month = member["month"]
        day   = member["day"]
        name  = member["name"]

        # Find this year's birthday date — handle Dec/Jan year boundary
        try:
            bday = date(check_year, month, day)
        except ValueError:
            continue  # invalid date (e.g. Feb 29 in non-leap year)

        # If birthday already passed this year, check next year
        if bday < today:
            try:
                bday = date(check_year + 1, month, day)
            except ValueError:
                continue

        # Only care about birthdays in the lookahead window
        days_until = (bday - today).days
        if days_until > lookahead:
            continue

        # Person-specific conflict check: is this member's name already
        # in the schedule on bday-1, bday, or bday+1? If so, skip entirely.
        already_scheduled = False
        for delta in (-1, 0, 1):
            check_date = (bday + timedelta(days=delta)).isoformat()
            if check_date in schedule:
                existing_name = schedule[check_date].get("name", "").strip().lower()
                if existing_name == name.lower():
                    already_scheduled = True
                    break

        if already_scheduled:
            print(f"[BIRTHDAY] Skipping {name} — already in schedule near {bday.isoformat()}")
            continue

        # Try to place: bday first, then bday-1, then bday+1
        placed_date = None
        for candidate in (bday, bday - timedelta(days=1), bday + timedelta(days=1)):
            if candidate.isoformat() not in schedule:
                placed_date = candidate
                break

        if placed_date is None:
            # All three dates occupied — alert leadership and leave schedule untouched
            bday_fmt  = f"{bday:%A, %B} {bday.day}"
            taken = []
            for candidate in (bday, bday - timedelta(days=1), bday + timedelta(days=1)):
                occupant = schedule[candidate.isoformat()].get("name", "someone")
                taken.append(f"{candidate:%b} {candidate.day} ({occupant})")
            alert = (
                f"🚨 **Birthday scheduling conflict — manual action needed!**\n"
                f"**{name}'s** birthday is **{bday_fmt}** but all three surrounding dates are taken:\n"
                + "\n".join(f"• {t}" for t in taken)
                + f"\nPlease manually add {name} to the schedule."
            )
            alerts.append(alert)
            print(f"[BIRTHDAY] CONFLICT — could not place {name} around {bday.isoformat()}")
            continue

        # Schedule the birthday entry on the chosen date
        note = "Auto-added from birthday sheet"
        if placed_date != bday:
            direction = "day before" if placed_date < bday else "day after"
            note = f"Auto-added from birthday sheet (placed {direction} due to conflict on actual birthday)"
            print(f"[BIRTHDAY] {name} placed on {placed_date.isoformat()} ({direction} birthday {bday.isoformat()})")
        else:
            print(f"[BIRTHDAY] Added {name} on {placed_date.isoformat()}")

        schedule[placed_date.isoformat()] = {
            "name":             name,
            "theme":            "Birthday",
            "tone":             "",
            "notes":            note,
            "prompt_retrieved": False,
        }
        added_count += 1

    if added_count:
        print(f"[BIRTHDAY] Added {added_count} birthday entr{'y' if added_count == 1 else 'ies'} to schedule")

    return schedule, alerts

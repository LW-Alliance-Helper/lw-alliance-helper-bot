"""
train_birthdays.py — Birthday roster + schedule integration.

Loads birthdays from the configured member sheet and places them into
the train schedule with conflict avoidance. Pure logic — no Discord
types — so it can be imported by both train.py and the train cog
without circular import concerns. The lookahead window comes from
each guild's `guild_birthday_config.lookahead_days`.
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
        info = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds = Credentials.from_service_account_file(key_file, scopes=scopes)

    gc = gspread.authorize(creds)
    from config import get_spreadsheet_id

    sh = gc.open_by_key(get_spreadsheet_id(guild_id))
    return sh.worksheet(tab_name)


# Month-name lookup used by parse_birthday. Includes full names plus 3-letter
# and 4-letter abbreviations leadership commonly types ("Sept", "Sep").
_BIRTHDAY_MONTH_MAP = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Days per month, with February allowing 29 to keep leap-year birthdays
# (we don't know the year, so we accept Feb 29 even in non-leap years).
_DAYS_IN_MONTH = {
    1: 31,
    2: 29,
    3: 31,
    4: 30,
    5: 31,
    6: 30,
    7: 31,
    8: 31,
    9: 30,
    10: 31,
    11: 30,
    12: 31,
}


def _validate_month_day(month: int, day: int) -> bool:
    if not (1 <= month <= 12):
        return False
    return 1 <= day <= _DAYS_IN_MONTH[month]


# ── Birthday sheet config ──────────────────────────────────────────────────────

BIRTHDAY_LOOKAHEAD = 14  # default, overridden per-guild by database


def load_birthdays(tab_name: str, guild_id: int = None) -> list[dict]:
    """
    Load all members with birthdays from the member sheet.
    Column indices and data start row come from guild birthday config.
    Returns a list of { "name": str, "month": int, "day": int }
    """
    from config import get_birthday_config

    bcfg = get_birthday_config(guild_id) if guild_id else {}
    name_col = bcfg.get("name_col", 4)
    bday_col = bcfg.get("birthday_col", 8)
    start_row = bcfg.get("data_start_row", 10)
    min_cols = max(name_col, bday_col) + 1
    try:
        ws = _get_member_sheet_inner(tab_name, guild_id)
        rows = ws.get_all_values()
        members = []
        for row in rows[start_row - 1 :]:
            if len(row) < min_cols:
                continue
            name = row[name_col].strip()
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
    """Parse a birthday string into (month, day), ignoring the year.

    Tolerates the variations leadership and members commonly type into the
    Sheet: slash, dash, dot separators; ISO 8601 with year; abbreviated and
    full month names; day-first ("7 December", "7-Dec") as well as
    month-first; ordinal suffixes ("December 7th"); 2-digit and 4-digit
    years. Bare numeric ambiguity (`7/12`) defaults to **M/D** unless the
    first number is > 12, in which case it must be a day → swap.

    Returns None for anything we can't confidently interpret OR for a
    parseable but invalid (month, day) like `Feb 30` or `13/45` — so the
    load loop's `if parsed:` skip kicks in instead of silently writing
    garbage to the train schedule.
    """
    if not raw or not raw.strip():
        return None

    # Collapse whitespace around separators ("12 / 7" → "12/7") so the
    # patterns below don't have to match optional spacing.
    raw = re.sub(r"\s*([-/.])\s*", r"\1", raw.strip())

    # 1. RFC 6450 month-day-only ("--12-07")
    md_iso = re.match(r"^--(\d{1,2})-(\d{1,2})$", raw)
    if md_iso:
        m, d = int(md_iso.group(1)), int(md_iso.group(2))
        return (m, d) if _validate_month_day(m, d) else None

    # 2. ISO 8601 with year (1990-12-07, 2026-12-07)
    iso = re.match(r"^\d{4}-(\d{1,2})-(\d{1,2})$", raw)
    if iso:
        m, d = int(iso.group(1)), int(iso.group(2))
        return (m, d) if _validate_month_day(m, d) else None

    # 3. Numeric M/D, M-D, M.D with optional trailing year
    numeric = re.match(r"^(\d{1,2})[-/.](\d{1,2})(?:[-/.]\d+)?$", raw)
    if numeric:
        a, b = int(numeric.group(1)), int(numeric.group(2))
        # If the first number is unambiguously a day, swap.
        if a > 12 and b <= 12:
            month, day = b, a
        else:
            month, day = a, b
        return (month, day) if _validate_month_day(month, day) else None

    # 4. Month-name first: "December 7", "Dec 7", "Dec-7", "December 7th",
    #    optionally followed by a year ("December 7, 1990").
    named_first = re.match(
        r"^([A-Za-z]+)[\s\-](\d{1,2})(?:st|nd|rd|th)?(?:[\s,\-]+\d+)?$",
        raw,
        re.IGNORECASE,
    )
    if named_first:
        month = _BIRTHDAY_MONTH_MAP.get(named_first.group(1).lower())
        if month is not None:
            day = int(named_first.group(2))
            return (month, day) if _validate_month_day(month, day) else None

    # 5. Day-first with month name: "7 December", "7-Dec", "7th December",
    #    optionally followed by a year ("7 December 1990").
    named_last = re.match(
        r"^(\d{1,2})(?:st|nd|rd|th)?[\s\-]([A-Za-z]+)(?:[\s,\-]+\d+)?$",
        raw,
        re.IGNORECASE,
    )
    if named_last:
        month = _BIRTHDAY_MONTH_MAP.get(named_last.group(2).lower())
        if month is not None:
            day = int(named_last.group(1))
            return (month, day) if _validate_month_day(month, day) else None

    return None


def birthday_lookup_for_dates(dates, guild_id: int = None) -> dict[str, list[str]]:
    """Return {ISO date: [member names]} for members whose birthday (month/day)
    falls on any of the given `dates`.

    Used by the Train Conductor Rotation (#55) weekly-draft generator in
    `override` birthday mode. Loads the birthday sheet once and matches by
    month+day, ignoring year. Returns {} when birthdays aren't enabled or the
    sheet has no usable rows — the draft generator then simply makes no
    birthday placements (rotation fills every day)."""
    from config import get_birthday_config

    bcfg = get_birthday_config(guild_id) if guild_id else {}
    if guild_id and not bcfg.get("enabled", 0):
        return {}

    tab_name = bcfg.get("tab_name") or get_member_tab_name(guild_id)
    members = load_birthdays(tab_name, guild_id)
    if not members:
        return {}

    # Map (month, day) → ISO for the dates we care about, then bucket members.
    want = {(d.month, d.day): d.isoformat() for d in dates}
    out: dict[str, list[str]] = {}
    for m in members:
        iso = want.get((m.get("month"), m.get("day")))
        if iso:
            out.setdefault(iso, []).append(m["name"])
    return out


# How far on either side of a birthday a member's existing schedule entry
# counts as "already handled", suppressing the conflict alert. Wider than
# the ±1-day placement window on purpose: leadership often resolves a
# conflict by hand-placing the member a few days off (e.g. birthday+2), and
# the alert must see that and go quiet. See #89 follow-up.
CONFLICT_HANDLED_WINDOW = 7


def conflict_key(member: dict, bday_iso: str) -> str:
    """Stable identity for one member's birthday conflict on a specific
    year's date. Discord-ID-first (survives a sheet rename), name-fallback
    (lowercased). Used as the dedup key for the dismissed-conflict set so an
    "Ignore" click silences exactly this birthday occurrence — next year's
    same-day birthday gets a fresh key via the year embedded in `bday_iso`."""
    did = str(member.get("discord_id") or "").strip()
    if did:
        return f"id:{did}|{bday_iso}"
    return f"name:{member.get('name', '').strip().lower()}|{bday_iso}"


def render_conflict_message(conflicts: list[dict]) -> str:
    """Render the combined human-readable body of the interactive birthday
    conflict alert: one header, one section per affected member, and
    singular/plural closing copy. A standalone helper so the cog (which
    attaches the resolution buttons) and the tests share one source of truth
    for the wording — multiple conflicts still collapse into one message
    (#89)."""
    sections = [
        f"**{c['name']}'s** birthday is **{c['bday_fmt']}** but all three "
        f"surrounding dates are taken:\n" + "\n".join(f"• {t}" for t in c["taken"])
        for c in conflicts
    ]
    return (
        "🚨 **Birthday scheduling conflict — manual action needed!**\n\n"
        + "\n\n".join(sections)
        + "\n\nUse the buttons below to place "
        + ("this member" if len(conflicts) == 1 else "these members")
        + " on an open day, show the surrounding schedule, or dismiss this alert."
    )


def check_and_add_birthdays(schedule: dict, guild_id: int = None) -> tuple[dict, list[dict]]:
    """
    Look ahead lookahead_days from today (from guild birthday config).
    Uses configured tab, name column, and birthday column.

    Returns `(schedule, conflicts)` where `conflicts` is a list of
    structured dicts (one per member who couldn't be placed), each with
    `name`, `discord_id`, `bday_iso`, `bday_fmt`, `taken`, `open_dates`,
    and a dedup `key`. An empty list means nothing needs leadership's
    attention. The cog turns the list into a single interactive alert via
    `render_conflict_message` + the resolution buttons.
    """
    from config import get_birthday_config

    bcfg = get_birthday_config(guild_id) if guild_id else {}
    if not bcfg.get("enabled", 0) and guild_id:
        return schedule, []
    if not bcfg.get("train_integration", 1) and guild_id:
        return schedule, []

    tab_name = bcfg.get("tab_name") or get_member_tab_name(guild_id)
    lookahead = bcfg.get("lookahead_days", BIRTHDAY_LOOKAHEAD)
    members = load_birthdays(tab_name, guild_id)
    if not members:
        return schedule, []

    today = date.today()
    check_year = today.year
    added_count = 0
    # Per-member structured conflict records collected during the loop and
    # returned for the cog to render into one combined interactive alert,
    # so leadership gets a single post listing every affected member.
    conflicts: list[dict] = []

    for member in members:
        month = member["month"]
        day = member["day"]
        name = member["name"]

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

        # Person-specific conflict check: is this member already in the
        # schedule anywhere within ±CONFLICT_HANDLED_WINDOW days of their
        # birthday? If so, treat the conflict as handled and skip entirely.
        # The window is wider than the ±1-day placement attempt so a manual
        # fix a few days off the birthday (the common leadership workaround)
        # silences the alert instead of letting it re-fire daily.
        already_scheduled = False
        for delta in range(-CONFLICT_HANDLED_WINDOW, CONFLICT_HANDLED_WINDOW + 1):
            check_date = (bday + timedelta(days=delta)).isoformat()
            if check_date in schedule:
                existing_name = schedule[check_date].get("name", "").strip().lower()
                if existing_name == name.lower():
                    already_scheduled = True
                    break

        if already_scheduled:
            print(f"[BIRTHDAY] Skipping {name} — already in schedule near {bday.isoformat()}")
            continue

        # Leadership explicitly dismissed this conflict from a prior alert.
        # Don't auto-place and don't re-alert — they've handled it their way.
        key = conflict_key(member, bday.isoformat())
        if guild_id:
            from config import get_ignored_conflict_keys

            if key in get_ignored_conflict_keys(guild_id):
                print(f"[BIRTHDAY] Skipping {name} — conflict dismissed by leadership")
                continue

        # Try to place: bday first, then bday-1, then bday+1
        placed_date = None
        for candidate in (bday, bday - timedelta(days=1), bday + timedelta(days=1)):
            if candidate.isoformat() not in schedule:
                placed_date = candidate
                break

        if placed_date is None:
            # All three dates occupied — record the conflict, leave the
            # schedule untouched, and continue collecting any other
            # conflicts so we can post a single combined alert at the
            # bottom of the loop.
            taken = []
            for candidate in (bday, bday - timedelta(days=1), bday + timedelta(days=1)):
                occupant = schedule[candidate.isoformat()].get("name", "someone")
                taken.append(f"{candidate:%b} {candidate.day} ({occupant})")
            # Open slots in the birthday→+7 window that leadership can place
            # the member into straight from the alert's dropdown.
            open_dates = [
                (bday + timedelta(days=i)).isoformat()
                for i in range(0, 8)
                if (bday + timedelta(days=i)).isoformat() not in schedule
            ]
            conflicts.append(
                {
                    "name": name,
                    "discord_id": member.get("discord_id"),
                    "bday_iso": bday.isoformat(),
                    "bday_fmt": f"{bday:%A, %B} {bday.day}",
                    "taken": taken,
                    "open_dates": open_dates,
                    "key": key,
                }
            )
            print(f"[BIRTHDAY] CONFLICT — could not place {name} around {bday.isoformat()}")
            continue

        # Schedule the birthday entry on the chosen date
        note = "Auto-added from birthday sheet"
        if placed_date != bday:
            direction = "day before" if placed_date < bday else "day after"
            note = f"Auto-added from birthday sheet (placed {direction} due to conflict on actual birthday)"
            print(
                f"[BIRTHDAY] {name} placed on {placed_date.isoformat()} ({direction} birthday {bday.isoformat()})"
            )
        else:
            print(f"[BIRTHDAY] Added {name} on {placed_date.isoformat()}")

        schedule[placed_date.isoformat()] = {
            "name": name,
            "theme": "Birthday",
            "tone": "",
            "notes": note,
            "prompt_retrieved": False,
        }
        added_count += 1

    if added_count:
        print(
            f"[BIRTHDAY] Added {added_count} birthday entr{'y' if added_count == 1 else 'ies'} to schedule"
        )

    # Return the structured conflicts. The cog renders them into a single
    # combined message (render_conflict_message) with interactive
    # resolution buttons attached — one Discord post no matter how many
    # members hit the conflict path (#89). An empty list means no alert.
    return schedule, conflicts

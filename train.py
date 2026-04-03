"""
train.py — Train schedule blurb generator + schedule management

Commands (OGV Leadership only, leadership channel only):
  !train              — Launch the blurb wizard (manual, any name)
  !cancel             — Cancel any active wizard session
  !schedule           — Input the upcoming train schedule (with optional details per person)
  !schedule list      — View the schedule as an embed
  !schedule clear     — Clear the entire schedule
  !trainschedule      — Shortcut to view the schedule embed
  !trainprompt        — Retrieve today's stored prompt (or pick a date)

Schedule reminder:
  - At 10pm ET (reset / 00:00 server) the bot reminds leadership that it's
    the new train day and prompts them to pull the ChatGPT prompt whenever
    the team is ready to run — no fixed run time assumed.
"""

import asyncio
import json
import os
import re
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

LEADERSHIP_CHANNEL_ID = 1488693874938482799
REQUIRED_ROLE_NAME    = "OGV Leadership"
TRAIN_SHEET_NAME      = "Train Schedule"

WIZARD_TIMEOUT = 300  # seconds

# Tracks active wizard sessions: { user_id: asyncio.Event }
active_wizards: dict[int, asyncio.Event] = {}

# ── Google Sheets persistence ──────────────────────────────────────────────────
#
# Sheet columns:
#   A: Date (YYYY-MM-DD)
#   B: Name
#   C: Theme
#   D: Tone
#   E: Notes
#   F: Prompt Retrieved (TRUE/FALSE)
#
# All reads/writes go through gspread using the same service account as sheets.py

def _get_train_sheet():
    """Return the Train Schedule worksheet."""
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
    sh = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
    return sh.worksheet(TRAIN_SHEET_NAME)


def load_schedule() -> dict:
    """
    Load the full schedule from the Train Schedule sheet.
    Returns { "YYYY-MM-DD": { name, theme, tone, notes, prompt_retrieved } }
    """
    try:
        ws   = _get_train_sheet()
        rows = ws.get_all_values()
        schedule = {}
        for row in rows[1:]:  # skip header row
            if not row or not row[0].strip():
                continue
            date_str = row[0].strip()
            schedule[date_str] = {
                "name":             row[1].strip() if len(row) > 1 else "",
                "theme":            row[2].strip() if len(row) > 2 else "",
                "tone":             row[3].strip() if len(row) > 3 else "",
                "notes":            row[4].strip() if len(row) > 4 else "",
                "prompt_retrieved": row[5].strip().upper() == "TRUE" if len(row) > 5 else False,
            }
        return schedule
    except Exception as e:
        print(f"[TRAIN] Error loading schedule from sheet: {e}")
        return {}


def save_schedule(schedule: dict):
    """
    Write the full schedule back to the Train Schedule sheet.
    Clears everything below the header and rewrites all rows.
    """
    try:
        ws = _get_train_sheet()
        # Clear all data rows (keep header in row 1)
        ws.batch_clear(["A2:F1000"])

        if not schedule:
            return

        rows = []
        for date_str, entry in sorted(schedule.items()):
            rows.append([
                date_str,
                entry.get("name", ""),
                entry.get("theme", ""),
                entry.get("tone", ""),
                entry.get("notes", ""),
                "TRUE" if entry.get("prompt_retrieved", False) else "FALSE",
            ])

        ws.update("A2", rows, value_input_option="USER_ENTERED")
        print(f"[TRAIN] Schedule saved to sheet ({len(rows)} entries)")
    except Exception as e:
        print(f"[TRAIN] Error saving schedule to sheet: {e}")


def mark_blurb_generated(date_str: str):
    """Mark a specific date's prompt_retrieved flag as TRUE in the sheet."""
    try:
        ws   = _get_train_sheet()
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0].strip() == date_str:
                ws.update(f"F{i}", [["TRUE"]], value_input_option="USER_ENTERED")
                print(f"[TRAIN] Marked prompt retrieved for {date_str}")
                return
        print(f"[TRAIN] Could not find row for {date_str} to mark as retrieved")
    except Exception as e:
        print(f"[TRAIN] Error marking blurb generated: {e}")


def blurb_generated_today() -> bool:
    """Check if today's prompt has been retrieved."""
    try:
        today    = date.today().isoformat()
        ws       = _get_train_sheet()
        rows     = ws.get_all_values()
        for row in rows[1:]:
            if row and row[0].strip() == today:
                return len(row) > 5 and row[5].strip().upper() == "TRUE"
        return False
    except Exception as e:
        print(f"[TRAIN] Error checking blurb log: {e}")
        return False


def load_blurb_log() -> set:
    """Return the set of all dates where prompt has been retrieved."""
    try:
        ws   = _get_train_sheet()
        rows = ws.get_all_values()
        return {
            row[0].strip()
            for row in rows[1:]
            if row and len(row) > 5 and row[5].strip().upper() == "TRUE"
        }
    except Exception as e:
        print(f"[TRAIN] Error loading blurb log: {e}")
        return set()


# ── Birthday sheet config ──────────────────────────────────────────────────────
#
# The active member tab name is stored in cell H1 of the Train Schedule tab.
# G1 contains the label "Active Member Tab:" for human readability.
# Default value if not set: "Season 5 - Off-Season"

DEFAULT_MEMBER_TAB = "Season 5 - Off-Season"
BIRTHDAY_LOOKAHEAD = 14  # days ahead to check for birthdays


def get_member_tab_name() -> str:
    """Read the active member tab name from H1 of the Train Schedule tab."""
    try:
        ws  = _get_train_sheet()
        val = ws.acell("H1").value
        return val.strip() if val and val.strip() else DEFAULT_MEMBER_TAB
    except Exception as e:
        print(f"[BIRTHDAY] Error reading member tab name: {e}")
        return DEFAULT_MEMBER_TAB


def set_member_tab_name(name: str):
    """Write the active member tab name to H1 of the Train Schedule tab."""
    try:
        ws = _get_train_sheet()
        ws.update("G1", [["Active Member Tab:"]], value_input_option="USER_ENTERED")
        ws.update("H1", [[name]], value_input_option="USER_ENTERED")
        print(f"[BIRTHDAY] Member tab name set to: {name}")
    except Exception as e:
        print(f"[BIRTHDAY] Error setting member tab name: {e}")


def _get_member_sheet(tab_name: str):
    """Return the active member worksheet."""
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
    sh = gc.open_by_key(os.getenv("SPREADSHEET_ID"))
    return sh.worksheet(tab_name)


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
        month = MONTH_MAP.get(named.group(1).lower())
        if month:
            try:
                return month, int(named.group(2))
            except ValueError:
                return None

    return None


def load_birthdays(tab_name: str) -> list[dict]:
    """
    Load all members with birthdays from the member sheet.
    Returns a list of { "name": str, "month": int, "day": int }
    Data starts at row 10 (index 9). Name = col E (index 4), Birthday = col H (index 7).
    """
    try:
        ws   = _get_member_sheet(tab_name)
        rows = ws.get_all_values()
        members = []
        for row in rows[9:]:  # data starts at row 10
            if len(row) < 8:
                continue
            name     = row[4].strip()   # Column E
            bday_raw = row[7].strip()   # Column H
            if not name or not bday_raw:
                continue
            parsed = parse_birthday(bday_raw)
            if parsed:
                members.append({"name": name, "month": parsed[0], "day": parsed[1]})
        print(f"[BIRTHDAY] Loaded {len(members)} members with birthdays from '{tab_name}'")
        return members
    except Exception as e:
        print(f"[BIRTHDAY] Error loading birthdays from '{tab_name}': {e}")
        return []


def check_and_add_birthdays(schedule: dict) -> dict:
    """
    Look ahead BIRTHDAY_LOOKAHEAD days from today.
    For each member whose birthday falls in that window:
      - Check if that member's name already appears in the schedule
        on their birthday ±1 day (person-specific conflict check)
      - If no conflict, add them with theme=Birthday
    Returns the updated schedule dict.
    """
    tab_name = get_member_tab_name()
    members  = load_birthdays(tab_name)
    if not members:
        return schedule

    today       = date.today()
    check_year  = today.year
    added_count = 0

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
        if days_until > BIRTHDAY_LOOKAHEAD:
            continue

        # Person-specific conflict check: is this member's name already
        # in the schedule on bday-1, bday, or bday+1?
        conflict = False
        for delta in (-1, 0, 1):
            check_date = (bday + timedelta(days=delta)).isoformat()
            if check_date in schedule:
                existing_name = schedule[check_date].get("name", "").strip().lower()
                if existing_name == name.lower():
                    conflict = True
                    break

        if conflict:
            print(f"[BIRTHDAY] Skipping {name} — already in schedule near {bday.isoformat()}")
            continue

        # Add birthday entry
        bday_str = bday.isoformat()
        schedule[bday_str] = {
            "name":             name,
            "theme":            "Birthday",
            "tone":             "",
            "notes":            "Auto-added from birthday sheet",
            "prompt_retrieved": False,
        }
        added_count += 1
        print(f"[BIRTHDAY] Added {name} on {bday_str}")

    if added_count:
        print(f"[BIRTHDAY] Added {added_count} birthday entr{'y' if added_count == 1 else 'ies'} to schedule")

    return schedule


# ── Schedule date parsing ──────────────────────────────────────────────────────

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date_and_name(line: str) -> tuple[date, str, str | None] | tuple[None, None, None]:
    """
    Parse a single 'Date - Name' line.
    Returns (date, clean_name, theme_hint) or (None, None, None).

    Handles parenthetical notes in names:
      4/02 - Emperor Gold (birthday)  →  name="Emperor Gold", theme_hint="Birthday"
      4/03 - Badkimberdog (birtbday)  →  name="Badkimberdog", theme_hint="Birthday"
      4/05 - Nomination or R4         →  name="Nomination or R4", theme_hint=None
    """
    current_year = datetime.now(tz=ET).year

    # Theme hint keywords found in parentheses
    THEME_HINTS = {
        "birthday": "Birthday",
        "birtday": "Birthday",   # common typo
        "birtbday": "Birthday",  # another common typo
        "bday": "Birthday",
        "welcome": "Welcome to OGV",
        "milestone": "Milestone",
        "war": "War / Performance",
        "performance": "War / Performance",
        "celebration": "General Celebration",
        "raffle": "Contest / Raffle",
        "nomination": "Contest / Raffle",
    }

    def extract_name_and_hint(raw_name: str) -> tuple[str, str | None]:
        """Strip parenthetical notes from name and detect theme hints."""
        paren_match = re.search(r"\(([^)]+)\)", raw_name)
        hint = None
        if paren_match:
            paren_content = paren_match.group(1).lower().strip()
            # Check each word in the parenthetical against hint keywords
            for word in re.split(r"\W+", paren_content):
                if word in THEME_HINTS:
                    hint = THEME_HINTS[word]
                    break
            # Remove the parenthetical from the name
            raw_name = raw_name[:paren_match.start()].strip()
        return raw_name.strip(), hint

    # Numeric: 4/1 - Name or 4/1/2026 - Name
    numeric = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s*[-:]\s*(.+)$", line)
    if numeric:
        try:
            d = date(
                int(numeric.group(3)) if numeric.group(3) else current_year,
                int(numeric.group(1)),
                int(numeric.group(2)),
            )
            name, hint = extract_name_and_hint(numeric.group(4).strip())
            return d, name, hint
        except ValueError:
            return None, None, None

    # Month name: April 1 - Name or April 1: Name
    named = re.match(
        r"^([A-Za-z]+)\s+(\d{1,2})(?:\s*(?:st|nd|rd|th))?\s*[-:]\s*(.+)$",
        line, re.IGNORECASE,
    )
    if not named:
        named = re.match(
            r"^([A-Za-z]+)\s+(\d{1,2})(?:\s*(?:st|nd|rd|th))?\s+(.+)$",
            line, re.IGNORECASE,
        )
    if named:
        month = MONTH_MAP.get(named.group(1).lower())
        if month:
            try:
                d = date(current_year, month, int(named.group(2)))
                name, hint = extract_name_and_hint(named.group(3).strip())
                return d, name, hint
            except ValueError:
                return None, None, None

    return None, None, None


# ── Theme and tone options ─────────────────────────────────────────────────────

THEMES = [
    "Welcome to OGV",
    "Birthday",
    "Milestone",
    "War / Performance",
    "General Celebration",
    "Contest / Raffle",
    "Custom",
]

TONES = [
    "Default (match the theme)",
    "More casual",
    "More intense",
    "Funny",
    "Serious",
    "Cinematic / Dramatic",
]

# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_chatgpt_prompt(name: str, theme: str, tone: str, notes: str) -> str:
    """Format a ready-to-paste ChatGPT thread prompt from stored entry data."""
    lines = [
        f"1. {name}",
        f"3. {theme}" + (f" — {tone}" if tone and tone != "Default (match the theme)" else ""),
    ]
    if notes:
        lines.append(f"4. {notes}")
    return "\n".join(lines)


# ── Embed builders ─────────────────────────────────────────────────────────────

def build_schedule_embed(schedule: dict, blurb_log: set) -> discord.Embed:
    """Build a scannable embed showing the upcoming train schedule."""
    today = date.today()

    if not schedule:
        embed = discord.Embed(
            title="🚂 OGV Train Schedule",
            description="*No schedule set. Use `/schedule` to add entries.*",
            color=discord.Color.gold(),
        )
        return embed

    upcoming = {k: v for k, v in sorted(schedule.items()) if date.fromisoformat(k) >= today}
    past     = {k: v for k, v in sorted(schedule.items()) if date.fromisoformat(k) < today}

    embed = discord.Embed(
        title="🚂 OGV Train Schedule",
        color=discord.Color.gold(),
        timestamp=datetime.now(tz=ET),
    )

    # ── Upcoming entries ───────────────────────────────────────────────────────
    if upcoming:
        for date_str, entry in upcoming.items():
            d           = date.fromisoformat(date_str)
            name        = entry.get("name", "Unknown")
            is_today    = d == today
            has_details = any(entry.get(k) for k in ("theme", "notes"))
            prompted    = date_str in blurb_log

            # Status badge
            if prompted:
                status = "✅ Prompt Retrieved"
            elif has_details:
                status = "📋 Details Stored"
            else:
                status = "⏳ Pending"

            # Field name — highlight today
            if is_today:
                field_name = f"🔴 TODAY — {d.strftime('%A, %B %-d')}"
            else:
                days_away  = (d - today).days
                day_label  = f"in {days_away} day{'s' if days_away != 1 else ''}"
                field_name = f"📅 {d.strftime('%A, %B %-d')} ({day_label})"

            # Field value — details summary if stored
            lines = [f"**{name}**", f"Status: {status}"]
            if entry.get("theme"):
                lines.append(f"Theme: {entry['theme']}")
            if entry.get("tone") and entry["tone"] != "Default (match the theme)":
                lines.append(f"Tone: {entry['tone']}")
            if entry.get("notes"):
                # Truncate long notes for scannability
                notes = entry["notes"]
                if len(notes) > 60:
                    notes = notes[:57] + "..."
                lines.append(f"Notes: *{notes}*")

            embed.add_field(name=field_name, value="\n".join(lines), inline=False)

    # ── Recent past entries (compact) ─────────────────────────────────────────
    if past:
        past_lines = []
        for date_str, entry in list(past.items())[-5:]:
            d    = date.fromisoformat(date_str)
            name = entry.get("name", "Unknown")
            icon = "✅" if date_str in blurb_log else "—"
            past_lines.append(f"{icon} ~~{d.strftime('%b %-d')}~~ — {name}")
        embed.add_field(
            name="📁 Recent Past",
            value="\n".join(past_lines),
            inline=False,
        )

    embed.set_footer(text="✅ Prompt retrieved  📋 Details stored  ⏳ Pending  |  Use !trainprompt to retrieve")
    return embed


def build_entry_embed(date_str: str, entry: dict, blurb_log: set) -> discord.Embed:
    """Build a detail embed for a single schedule entry."""
    d     = date.fromisoformat(date_str)
    name  = entry.get("name", "Unknown")
    done  = date_str in blurb_log

    embed = discord.Embed(
        title=f"🚂 {name} — {d.strftime('%A, %B %-d')}",
        color=discord.Color.green() if done else discord.Color.blurple(),
    )

    embed.add_field(name="Theme",  value=entry.get("theme", "*Not set*"),  inline=True)
    embed.add_field(name="Tone",   value=entry.get("tone",  "*Not set*"),  inline=True)
    embed.add_field(name="\u200b", value="\u200b",                         inline=True)
    embed.add_field(name="Notes",  value=entry.get("notes", "*Not set*"),  inline=False)

    status = "✅ Prompt already retrieved" if done else "⏳ Prompt not yet retrieved"
    embed.set_footer(text=status)

    return embed


# ── UI Views ───────────────────────────────────────────────────────────────────

class ThemeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None
        select = discord.ui.Select(
            placeholder="Choose a theme...",
            options=[discord.SelectOption(label=t, value=t) for t in THEMES],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = interaction.data["values"][0]
        await interaction.response.defer()
        self.stop()


class ToneSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None
        select = discord.ui.Select(
            placeholder="Choose a tone...",
            options=[discord.SelectOption(label=t, value=t) for t in TONES],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = interaction.data["values"][0]
        await interaction.response.defer()
        self.stop()


class ConfirmPromptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.action = None

    @discord.ui.button(label="📋 Get Prompt", style=discord.ButtonStyle.success)
    async def get_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "prompt"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "cancel"
        await interaction.response.defer()
        self.stop()


class SkipOrFillView(discord.ui.View):
    """Used during schedule input to ask whether to fill in details for a person."""
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.action = None

    @discord.ui.button(label="✏️ Add Details", style=discord.ButtonStyle.primary)
    async def fill(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "fill"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="⏭️ Skip for Now", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "skip"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="🛑 Done Entering", style=discord.ButtonStyle.danger)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "done"
        await interaction.response.defer()
        self.stop()


class ReminderView(discord.ui.View):
    def __init__(self, cog, date_str: str, name: str):
        super().__init__(timeout=3600)
        self.cog      = cog
        self.date_str = date_str
        self.name     = name

    @discord.ui.button(label="📋 View & Get Prompt", style=discord.ButtonStyle.success)
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_names = [r.name for r in interaction.user.roles]
        if REQUIRED_ROLE_NAME not in role_names:
            await interaction.response.send_message(
                f"⛔ You need the **{REQUIRED_ROLE_NAME}** role.", ephemeral=True
            )
            return

        button.disabled = True
        await interaction.response.edit_message(view=self)

        schedule   = load_schedule()
        blurb_log  = load_blurb_log()
        entry      = schedule.get(self.date_str, {"name": self.name})
        channel    = interaction.channel

        await retrieve_and_confirm(self.cog.bot, channel, interaction.user, self.date_str, entry, blurb_log)
        self.stop()


# ── Retrieve and confirm flow ──────────────────────────────────────────────────

async def retrieve_and_confirm(bot, channel, user, date_str: str, entry: dict, blurb_log: set):
    """Show stored details for an entry and offer to post the prompt."""
    detail_embed = build_entry_embed(date_str, entry, blurb_log)
    confirm_view = ConfirmPromptView()

    name = entry.get("name", "Unknown")
    has_details = any(entry.get(k) for k in ("theme", "notes"))

    if not has_details:
        await channel.send(
            f"⚠️ No details stored for **{name}** yet. "
            f"Use `/train` to build the prompt manually.",
            embed=detail_embed,
        )
        return

    await channel.send(
        f"📋 Here are the stored details for **{name}**. Ready to get the ChatGPT prompt?",
        embed=detail_embed,
        view=confirm_view,
    )
    await confirm_view.wait()

    if confirm_view.action != "prompt":
        return

    prompt = build_chatgpt_prompt(
        name=name,
        theme=entry.get("theme", ""),
        tone=entry.get("tone", ""),
        notes=entry.get("notes", ""),
    )
    await channel.send(
        f"✅ **ChatGPT prompt for {name}** — copy and paste into the thread:\n```\n{prompt}\n```"
    )
    mark_blurb_generated(date_str)


# ── Schedule collection wizard ─────────────────────────────────────────────────

async def collect_schedule(bot, ctx: commands.Context, cancel_event: asyncio.Event):
    """
    Walk through schedule input:
    1. Collect all date/name lines at once
    2. For each parsed entry, offer to add theme/tone/notes or skip
    """
    channel = ctx.channel
    user    = ctx.author

    def check_msg(m):
        return m.author == user and m.channel == channel

    async def wait_for_msg(prompt_text: str) -> str | None:
        msg = await channel.send(prompt_text)
        try:
            reply_task  = asyncio.ensure_future(bot.wait_for("message", check=check_msg, timeout=WIZARD_TIMEOUT))
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            done, pending = await asyncio.wait([reply_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if cancel_event.is_set():
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
                return None
            reply = done.pop().result()
            try:
                await msg.delete()
                await reply.delete()
            except discord.HTTPException:
                pass
            return reply.content.strip()
        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out. Use `/schedule` to try again.")
            return None

    async def wait_for_view(view: discord.ui.View, msg: discord.Message) -> bool:
        """Wait for a view interaction or cancel. Returns False if cancelled."""
        view_task   = asyncio.ensure_future(view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if cancel_event.is_set():
            for item in view.children:
                item.disabled = True
            try:
                await msg.edit(view=view)
            except discord.HTTPException:
                pass
            return False
        return True

    # Step 1: Collect date/name lines
    await channel.send(
        f"📋 **Train Schedule Input** — {user.mention}\n\n"
        f"Paste your schedule below, one entry per line:\n"
        f"```\n"
        f"April 2 - PlayerName\n"
        f"April 5 - PlayerName\n"
        f"4/8 - PlayerName\n"
        f"```\n"
        f"This will **merge** with the current schedule (existing entries are kept).\n"
        f"*(Type `/cancel` at any time to stop)*"
    )

    # Wait directly for the user's reply
    try:
        reply_task  = asyncio.ensure_future(bot.wait_for("message", check=check_msg, timeout=WIZARD_TIMEOUT))
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([reply_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()

        if cancel_event.is_set():
            await channel.send("❌ Schedule input cancelled.")
            return

        reply = done.pop().result()
        raw   = reply.content.strip()
        print(f"[TRAIN] Schedule input received: {repr(raw)}")

        try:
            await reply.delete()
        except discord.HTTPException:
            pass

    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out. Use `/schedule` to try again.")
        return

    if not raw:
        await channel.send("⚠️ No input received. Use `/schedule` to try again.")
        return

    # Parse all lines
    parsed  = []
    errors  = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        print(f"[TRAIN] Parsing line: {repr(line)}")
        d, name, hint = parse_date_and_name(line)
        if d and name:
            print(f"[TRAIN] Parsed: {d} → {name} (hint: {hint})")
            parsed.append((d, name, hint))
        else:
            print(f"[TRAIN] Could not parse: {repr(line)}")
            errors.append(line)

    if not parsed:
        err_list = "\n".join(f"• {e}" for e in errors)
        await channel.send(
            f"⚠️ Could not parse any entries. Check your format and try again.\n\n"
            f"**Could not parse:**\n{err_list}"
        )
        return

    # Load existing schedule to merge into
    schedule = load_schedule()

    # Step 2: For each parsed entry, offer to add details
    await channel.send(
        f"✅ Parsed **{len(parsed)}** entr{'y' if len(parsed) == 1 else 'ies'}. "
        f"Now you can add details for each person, or skip and do it later.\n"
        f"*(Details are stored and used to auto-build the ChatGPT prompt when needed)*"
    )

    for i, (d, name, theme_hint) in enumerate(parsed):
        date_str       = d.isoformat()
        existing_entry = schedule.get(date_str, {})
        existing_name  = existing_entry.get("name", name) if isinstance(existing_entry, dict) else name

        hint_note = f" *(theme hint detected: **{theme_hint}**)*" if theme_hint else ""
        skip_view = SkipOrFillView()
        skip_msg  = await channel.send(
            f"**Entry {i+1} of {len(parsed)}: {existing_name} — {d.strftime('%A, %B %-d')}**{hint_note}\n"
            f"Would you like to add theme, tone, and notes now?",
            view=skip_view,
        )

        ok = await wait_for_view(skip_view, skip_msg)
        if not ok:
            return

        try:
            await skip_msg.delete()
        except discord.HTTPException:
            pass

        if skip_view.action == "done":
            # Add current entry
            existing_entry["name"] = existing_name
            if theme_hint and not existing_entry.get("theme"):
                existing_entry["theme"] = theme_hint
            schedule[date_str] = existing_entry

            # Also add all remaining unparsed entries with just their names
            # so nothing in the list gets silently dropped
            for remaining_d, remaining_name, remaining_hint in parsed[i+1:]:
                remaining_str   = remaining_d.isoformat()
                remaining_entry = schedule.get(remaining_str, {})
                if isinstance(remaining_entry, dict):
                    remaining_entry["name"] = remaining_entry.get("name", remaining_name)
                else:
                    remaining_entry = {"name": remaining_name}
                if remaining_hint and not remaining_entry.get("theme"):
                    remaining_entry["theme"] = remaining_hint
                schedule[remaining_str] = remaining_entry
            break

        if skip_view.action == "skip":
            existing_entry["name"] = existing_name
            if theme_hint and not existing_entry.get("theme"):
                existing_entry["theme"] = theme_hint
            schedule[date_str] = existing_entry
            continue

        # Fill in details
        entry = dict(existing_entry)
        entry["name"] = existing_name

        # Theme
        theme_msg  = await channel.send(f"🎯 **Theme** for {existing_name}:")
        theme_view = ThemeSelectView()
        await theme_msg.edit(view=theme_view)
        ok = await wait_for_view(theme_view, theme_msg)
        if not ok:
            save_schedule(schedule)  # save whatever we have before exiting
            return
        try:
            await theme_msg.delete()
        except discord.HTTPException:
            pass

        if theme_view.selected:
            theme = theme_view.selected
            if theme == "Custom":
                custom = await wait_for_msg("Type your custom theme:")
                if custom is None:
                    save_schedule(schedule)
                    return
                theme = custom
            entry["theme"] = theme

        # Tone
        tone_msg  = await channel.send(f"🎭 **Tone** for {existing_name}:")
        tone_view = ToneSelectView()
        await tone_msg.edit(view=tone_view)
        ok = await wait_for_view(tone_view, tone_msg)
        if not ok:
            save_schedule(schedule)
            return
        try:
            await tone_msg.delete()
        except discord.HTTPException:
            pass

        if tone_view.selected and tone_view.selected != "Default (match the theme)":
            entry["tone"] = tone_view.selected

        # Notes
        notes_raw = await wait_for_msg(
            f"📝 **Notes** for {existing_name} *(or type `skip`)*:\n"
            f"Add anything personal — role, personality, achievements, story moments."
        )
        if notes_raw is None:
            save_schedule(schedule)
            return
        if notes_raw.lower() != "skip":
            entry["notes"] = notes_raw

        schedule[date_str] = entry

        await channel.send(
            f"✅ Details collected for **{existing_name}**.",
            delete_after=5,
        )

    # Single save to the sheet at the end — all entries written at once
    await ctx.channel.send("💾 Saving schedule to Google Sheets...")
    save_schedule(schedule)

    # Build error note if any lines couldn't be parsed
    error_text = ""
    if errors:
        error_text = f"\n\n⚠️ **Could not parse {len(errors)} line(s):**\n" + "\n".join(f"• {e}" for e in errors)

    # Post updated schedule embed — fall back to plain text if embed permission is missing
    blurb_log = load_blurb_log()
    embed     = build_schedule_embed(schedule, blurb_log)
    try:
        await channel.send(f"📋 **Schedule saved!**{error_text}", embed=embed)
    except discord.Forbidden:
        # No embed permission — post a plain text summary instead
        today    = date.today()
        lines    = [f"📋 **Schedule saved!**{error_text}\n"]
        upcoming = {k: v for k, v in sorted(schedule.items()) if date.fromisoformat(k) >= today}
        for date_str, entry in upcoming.items():
            d    = date.fromisoformat(date_str)
            name = entry.get("name", "Unknown")
            icon = "📋" if any(entry.get(k) for k in ("theme", "notes")) else "⏳"
            lines.append(f"{icon} **{d.strftime('%a, %b %-d')}** — {name}")
        await channel.send("\n".join(lines))


# ── Train wizard (manual, for any name) ───────────────────────────────────────

async def run_wizard_steps(bot, channel, user, prefilled_name: str = None):
    cancel_event = asyncio.Event()
    active_wizards[user.id] = cancel_event

    def check_msg(m):
        return m.author == user and m.channel == channel

    async def ask(prompt: str) -> str | None:
        msg = await channel.send(prompt) if prompt else None
        try:
            reply_task  = asyncio.ensure_future(bot.wait_for("message", check=check_msg, timeout=WIZARD_TIMEOUT))
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            done, pending = await asyncio.wait([reply_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if cancel_event.is_set():
                if msg:
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        pass
                return None
            reply = done.pop().result()
            try:
                if msg:
                    await msg.delete()
                await reply.delete()
            except discord.HTTPException:
                pass
            return reply.content.strip()
        except asyncio.TimeoutError:
            await channel.send("⏰ Wizard timed out. Use `/train` to start again.")
            return None

    async def wait_for_view(view, msg):
        view_task   = asyncio.ensure_future(view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if cancel_event.is_set():
            for item in view.children:
                item.disabled = True
            try:
                await msg.edit(view=view)
            except discord.HTTPException:
                pass
            return False
        return True

    try:
        # Step 1: Name
        if prefilled_name:
            name = prefilled_name
            await channel.send(
                f"🚂 **Train Blurb Wizard** — started by {user.mention}\n\n"
                f"👤 **Name pre-filled:** {name}\n"
                f"*(Type `/cancel` at any time to stop)*"
            )
        else:
            await channel.send(
                f"🚂 **Train Blurb Wizard** — started by {user.mention}\n\n"
                f"**Step 1 of 4 — Member Name**\n"
                f"Type the member's name exactly as it should appear:\n"
                f"*(Type `/cancel` at any time to stop)*"
            )
            name = await ask("")
            if not name or cancel_event.is_set():
                return False

        # Step 2: Theme
        theme_msg  = await channel.send("**Step 2 of 4 — Theme**\nSelect the theme for this train:")
        theme_view = ThemeSelectView()
        await theme_msg.edit(view=theme_view)
        if not await wait_for_view(theme_view, theme_msg):
            return False
        if theme_view.selected is None:
            await channel.send("⏰ Wizard timed out. Use `/train` to start again.")
            return False
        theme = theme_view.selected
        if theme == "Custom":
            theme = await ask("Type your custom theme:")
            if not theme or cancel_event.is_set():
                return False

        # Step 3: Tone
        tone_msg  = await channel.send("**Step 3 of 4 — Tone**\nSelect the tone:")
        tone_view = ToneSelectView()
        await tone_msg.edit(view=tone_view)
        if not await wait_for_view(tone_view, tone_msg):
            return False
        if tone_view.selected is None:
            await channel.send("⏰ Wizard timed out. Use `/train` to start again.")
            return False
        tone = tone_view.selected

        # Step 4: Notes
        await channel.send(
            "**Step 4 of 4 — Notes** *(highly recommended)*\n"
            "Add anything personal — role, personality, achievements, story moments.\n"
            "Type your notes, or type `skip`:"
        )
        notes_raw = await ask("")
        if notes_raw is None or cancel_event.is_set():
            return False
        notes = "" if notes_raw.lower() == "skip" else notes_raw

        # Summary
        tone_display  = tone if tone != "Default (match the theme)" else "Default"
        notes_display = notes if notes else "*(none)*"

        confirm_view = ConfirmPromptView()
        await channel.send(
            f"**Ready to build prompt — here's your input:**\n\n"
            f"👤 **Name:** {name}\n"
            f"🎯 **Theme:** {theme}\n"
            f"🎭 **Tone:** {tone_display}\n"
            f"📝 **Notes:** {notes_display}\n\n"
            f"Post the ChatGPT prompt?",
            view=confirm_view,
        )
        if not await wait_for_view(confirm_view, discord.utils.MISSING):
            return False
        if confirm_view.action != "prompt":
            await channel.send("❌ Cancelled. Use `/train` to start over.")
            return False

        prompt = build_chatgpt_prompt(name, theme, tone, notes)
        await channel.send(
            f"✅ **ChatGPT prompt for {name}** — copy and paste into the thread:\n"
            f"```\n{prompt}\n```"
        )
        mark_blurb_generated(date.today().isoformat())
        return True

    finally:
        active_wizards.pop(user.id, None)


async def run_train_wizard(interaction: discord.Interaction):
    await run_wizard_steps(interaction.client, interaction.channel, interaction.user)


async def run_train_wizard_prefilled(bot, channel, user, name: str):
    await run_wizard_steps(bot, channel, user, prefilled_name=name)


# ── Slash command guards ───────────────────────────────────────────────────────

GUILD_ID = 1266229297723605052
GUILD    = discord.Object(id=GUILD_ID)

def _is_leadership(interaction: discord.Interaction) -> bool:
    return REQUIRED_ROLE_NAME in [r.name for r in interaction.user.roles]

def _in_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == LEADERSHIP_CHANNEL_ID

async def _guard(interaction: discord.Interaction) -> bool:
    if not _in_channel(interaction):
        await interaction.response.send_message(
            "⛔ This command can only be used in the leadership channel.", ephemeral=True
        )
        return False
    if not _is_leadership(interaction):
        await interaction.response.send_message(
            f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to use this command.", ephemeral=True
        )
        return False
    return True


# ── Cog ────────────────────────────────────────────────────────────────────────

class TrainCog(commands.Cog):
    def __init__(self, bot):
        self.bot                 = bot
        self.reminder_sent_today = False
        self.last_reminder_date  = None
        self.check_reminder.start()

    def cog_unload(self):
        self.check_reminder.cancel()

    # ── /checkbirthdays ────────────────────────────────────────────────────────

    @app_commands.command(
        name="checkbirthdays",
        description="Manually run the birthday check and add upcoming birthdays to the schedule",
    )
    @app_commands.guilds(GUILD)
    async def checkbirthdays(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        try:
            current_schedule = load_schedule()
            before_count     = len(current_schedule)
            updated_schedule = check_and_add_birthdays(current_schedule)
            after_count      = len(updated_schedule)
            added            = after_count - before_count

            if added > 0:
                save_schedule(updated_schedule)
                await interaction.followup.send(
                    f"✅ Birthday check complete — added **{added}** birthday entr{'y' if added == 1 else 'ies'} to the schedule.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"✅ Birthday check complete — no new entries to add within the next {BIRTHDAY_LOOKAHEAD} days.",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.followup.send(
                f"⚠️ Birthday check failed: {e}",
                ephemeral=True,
            )

    # ── /setbirthdays ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="setbirthdays",
        description="Set the member sheet tab name used for birthday lookups",
    )
    @app_commands.describe(tab_name="Exact name of the tab, e.g. 'Season 6 - Off-Season'")
    @app_commands.guilds(GUILD)
    async def setbirthdays(self, interaction: discord.Interaction, tab_name: str):
        if not await _guard(interaction):
            return

        # Verify the tab actually exists before saving
        try:
            _get_member_sheet(tab_name)
        except Exception:
            await interaction.response.send_message(
                f"⚠️ Could not find a tab named **{tab_name}** in your Google Sheet. "
                f"Please check the spelling and try again.",
                ephemeral=True,
            )
            return

        set_member_tab_name(tab_name)
        await interaction.response.send_message(
            f"✅ Birthday source tab updated to **{tab_name}**.\n"
            f"The next nightly check at 10pm ET will use this tab.",
            ephemeral=True,
        )

    # ── /cancel ────────────────────────────────────────────────────────────────

    @app_commands.command(name="cancel", description="Cancel your active wizard session")
    @app_commands.guilds(GUILD)
    async def cancel(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        if interaction.user.id in active_wizards:
            active_wizards[interaction.user.id].set()
            await interaction.response.send_message(
                f"❌ Wizard cancelled.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "ℹ️ You don't have an active wizard running.", ephemeral=True
            )

    # ── /train ─────────────────────────────────────────────────────────────────

    @app_commands.command(name="train", description="Launch the train blurb wizard to build a ChatGPT prompt")
    @app_commands.guilds(GUILD)
    async def train(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        if interaction.user.id in active_wizards:
            await interaction.response.send_message(
                "⚠️ You already have an active session running. Use `/cancel` to stop it first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("🚂 Starting wizard...", ephemeral=True)
        await run_train_wizard(interaction)

    # ── /schedule ──────────────────────────────────────────────────────────────

    @app_commands.command(name="schedule", description="Input, view, or clear the train schedule")
    @app_commands.describe(action="What to do: input (default), list, or clear")
    @app_commands.choices(action=[
        app_commands.Choice(name="Input new schedule", value="input"),
        app_commands.Choice(name="List current schedule", value="list"),
        app_commands.Choice(name="Clear schedule", value="clear"),
    ])
    @app_commands.guilds(GUILD)
    async def schedule(self, interaction: discord.Interaction, action: str = "input"):
        if not await _guard(interaction):
            return

        if action == "list":
            await interaction.response.defer()
            await self._post_schedule_embed(interaction.channel)
            await interaction.delete_original_response()
            return

        if action == "clear":
            save_schedule({})
            await interaction.response.send_message("🗑️ Train schedule cleared.")
            return

        # Input mode
        if interaction.user.id in active_wizards:
            await interaction.response.send_message(
                "⚠️ You already have an active session running. Use `/cancel` to stop it first.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message("📋 Starting schedule input...", ephemeral=True)

        cancel_event = asyncio.Event()
        active_wizards[interaction.user.id] = cancel_event

        # Build a fake ctx-like object so collect_schedule can send messages
        class FakeCtx:
            channel = interaction.channel
            author  = interaction.user
        fake_ctx = FakeCtx()
        fake_ctx.channel = interaction.channel

        try:
            await collect_schedule(self.bot, fake_ctx, cancel_event)
        finally:
            active_wizards.pop(interaction.user.id, None)

    # ── /trainschedule ─────────────────────────────────────────────────────────

    @app_commands.command(name="trainschedule", description="View the current train schedule")
    @app_commands.guilds(GUILD)
    async def trainschedule(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        await interaction.response.defer()
        await self._post_schedule_embed(interaction.channel)
        await interaction.delete_original_response()

    async def _post_schedule_embed(self, channel):
        schedule  = load_schedule()
        blurb_log = load_blurb_log()
        embed     = build_schedule_embed(schedule, blurb_log)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            today    = date.today()
            lines    = ["📋 **OGV Train Schedule**\n*(Enable Embed Links permission for a better display)*\n"]
            upcoming = {k: v for k, v in sorted(schedule.items()) if date.fromisoformat(k) >= today}
            if not upcoming:
                lines.append("*No upcoming entries.*")
            for date_str, entry in upcoming.items():
                d         = date.fromisoformat(date_str)
                name      = entry.get("name", "Unknown")
                done      = "✅" if date_str in blurb_log else ("📋" if any(entry.get(k) for k in ("theme", "notes")) else "⏳")
                today_tag = " — **TODAY**" if d == today else ""
                lines.append(f"{done} **{d.strftime('%a, %b %-d')}** — {name}{today_tag}")
            await channel.send("\n".join(lines))

    # ── /trainprompt ───────────────────────────────────────────────────────────

    @app_commands.command(name="trainprompt", description="Retrieve a stored ChatGPT prompt for a scheduled person")
    @app_commands.describe(date="Date to retrieve, e.g. 'April 5' or '4/5' (defaults to today)")
    @app_commands.guilds(GUILD)
    async def trainprompt(self, interaction: discord.Interaction, date: str = None):
        if not await _guard(interaction):
            return

        await interaction.response.defer()

        schedule  = load_schedule()
        blurb_log = load_blurb_log()

        if date:
            d, _, _ = parse_date_and_name(f"{date} - placeholder")
            if d is None:
                await interaction.followup.send(
                    f"⚠️ Could not parse date `{date}`. Try formats like `April 5` or `4/5`.",
                    ephemeral=True,
                )
                return
            date_str = d.isoformat()
        else:
            date_str = date_cls.today().isoformat()

        if date_str not in schedule:
            d_label = date_cls.fromisoformat(date_str).strftime("%A, %B %-d")
            await interaction.followup.send(f"ℹ️ Nothing scheduled for **{d_label}**.", ephemeral=True)
            return

        await interaction.delete_original_response()
        entry = schedule[date_str]
        await retrieve_and_confirm(self.bot, interaction.channel, interaction.user, date_str, entry, blurb_log)

    # ── Reminder loop ──────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_reminder(self):
        now   = datetime.now(tz=ET)
        today = date_cls.today()

        if self.last_reminder_date != today:
            self.last_reminder_date  = today
            self.reminder_sent_today = False

        if now.hour != 22 or now.minute != 0:
            return
        if self.reminder_sent_today:
            return

        # ── Birthday auto-population ───────────────────────────────────────────
        # Run once at reset — load schedule, add any upcoming birthdays, save back
        try:
            current_schedule = load_schedule()
            updated_schedule = check_and_add_birthdays(current_schedule)
            if updated_schedule != current_schedule:
                save_schedule(updated_schedule)
        except Exception as e:
            print(f"[BIRTHDAY] Error during birthday check: {e}")

        # ── Train reminder ─────────────────────────────────────────────────────

        schedule  = load_schedule()
        today_str = today.isoformat()
        if today_str not in schedule:
            return
        if blurb_generated_today():
            return

        entry   = schedule[today_str]
        name    = entry.get("name", "Unknown")
        channel = self.bot.get_channel(LEADERSHIP_CHANNEL_ID)
        if channel is None:
            return

        view = ReminderView(cog=self, date_str=today_str, name=name)
        await channel.send(
            f"🚂 **Reset! Today's train is for {name}.**\n\n"
            f"Click below whenever you're ready to get the ChatGPT prompt — no rush, run it when the team is available.",
            view=view,
        )
        self.reminder_sent_today = True
        print(f"[TRAIN] Reminder sent for {name} on {today_str}")

    @check_reminder.before_loop
    async def before_check_reminder(self):
        await self.bot.wait_until_ready()


# Alias to avoid conflict with `date` parameter name in slash commands
date_cls = date


async def setup(bot: commands.Bot):
    await bot.add_cog(TrainCog(bot))


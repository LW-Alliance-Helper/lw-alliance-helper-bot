"""
train.py — Train schedule blurb generator + schedule management

Slash commands (leadership role + channel only):
  /train              — View the schedule with Add / Update / Generate Prompt / Clear buttons
  /train_log [date]   — Show recent prompt log entries (defaults to last 14 days)
  /train_addbirthdays — Manually run the birthday check now
  /birthdays          — Show upcoming birthdays in the next 14 days
  /cancel             — Cancel any active wizard session

Schedule reminder:
  - At the configured reminder_time the bot pings leadership that it's
    the new train day and offers a button to run the blurb wizard whenever
    the team is ready — no fixed run time assumed.
"""

import asyncio
import json
import os
import re
import discord
from discord.ext import commands
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from config import get_config

# Birthday helpers + member-sheet loader live in train_birthdays.py.
# Re-export here so existing imports (`from train import load_birthdays`,
# `check_and_add_birthdays`, etc.) keep working.
from train_birthdays import (
    BIRTHDAY_LOOKAHEAD,
    load_birthdays,
    get_member_tab_name,
    parse_birthday,
    check_and_add_birthdays,
)
from train_birthdays import _get_member_sheet_inner as _get_member_sheet  # noqa: F401


ET = ZoneInfo("America/New_York")


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
# All reads/writes go through gspread using the same service account as the rest of the bot

def _get_train_sheet(guild_id: int = None):
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
    from config import get_spreadsheet_id, get_config
    sh  = gc.open_by_key(get_spreadsheet_id(guild_id))
    cfg = get_config(guild_id)
    tab = cfg.tab_train_schedule if cfg else "Train Schedule"
    return sh.worksheet(tab)


def load_schedule(guild_id: int = None) -> dict:
    """
    Load the full schedule from the Train Schedule sheet.
    Returns { "YYYY-MM-DD": { name, theme, tone, notes, prompt_retrieved } }
    """
    try:
        ws   = _get_train_sheet(guild_id)
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


def save_schedule(schedule: dict, guild_id: int = None):
    """
    Write the full schedule back to the Train Schedule sheet.
    Clears everything below the header and rewrites all rows.
    """
    try:
        ws = _get_train_sheet(guild_id)
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


def mark_blurb_generated(date_str: str, guild_id: int = None):
    """Mark a specific date's prompt_retrieved flag as TRUE in the sheet."""
    try:
        ws   = _get_train_sheet(guild_id)
        rows = ws.get_all_values()
        for i, row in enumerate(rows[1:], start=2):
            if row and row[0].strip() == date_str:
                ws.update(f"F{i}", [["TRUE"]], value_input_option="USER_ENTERED")
                print(f"[TRAIN] Marked prompt retrieved for {date_str}")
                return
        print(f"[TRAIN] Could not find row for {date_str} to mark as retrieved")
    except Exception as e:
        print(f"[TRAIN] Error marking blurb generated: {e}")


def blurb_generated_today(guild_id: int = None) -> bool:
    """Check if today's prompt has been retrieved."""
    try:
        today    = date.today().isoformat()
        ws       = _get_train_sheet(guild_id)
        rows     = ws.get_all_values()
        for row in rows[1:]:
            if row and row[0].strip() == today:
                return len(row) > 5 and row[5].strip().upper() == "TRUE"
        return False
    except Exception as e:
        print(f"[TRAIN] Error checking blurb log: {e}")
        return False


def load_blurb_log(guild_id: int = None) -> set:
    """Return the set of all dates where prompt has been retrieved."""
    try:
        ws   = _get_train_sheet(guild_id)
        rows = ws.get_all_values()
        return {
            row[0].strip()
            for row in rows[1:]
            if row and len(row) > 5 and row[5].strip().upper() == "TRUE"
        }
    except Exception as e:
        print(f"[TRAIN] Error loading blurb log: {e}")
        return set()


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
        "welcome": "Welcome",
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
# These are now per-guild via the database. The functions below load them
# dynamically. These defaults are only used as a final fallback.

DEFAULT_THEMES = [
    "Welcome to the Alliance",
    "Birthday",
    "Milestone",
    "War / Performance",
    "General Celebration",
    "Contest / Raffle",
    "Custom",
]

DEFAULT_TONES = [
    "Default (match the theme)",
    "More casual",
    "More intense",
    "Funny",
    "Serious",
    "Cinematic / Dramatic",
]


def get_themes(guild_id: int = None) -> list:
    """Return the theme list for a guild."""
    from config import get_train_config
    cfg = get_train_config(guild_id) if guild_id else {}
    return cfg.get("themes") or DEFAULT_THEMES


def get_tones(guild_id: int = None) -> list:
    """Return the tone list for a guild."""
    from config import get_train_config
    cfg = get_train_config(guild_id) if guild_id else {}
    return cfg.get("tones") or DEFAULT_TONES


def get_prompt_template(guild_id: int = None, template_name: str = None) -> str:
    """
    Return a named ChatGPT prompt template for a guild. When `template_name`
    is None, returns the guild's configured default. Falls back to the legacy
    `prompt_template` column for backwards compatibility.
    """
    from config import get_train_config
    cfg = get_train_config(guild_id) if guild_id else {}
    templates = cfg.get("templates") or []
    target    = template_name or cfg.get("default_template") or "Default"
    for t in templates:
        if t.get("name") == target:
            return t.get("template", "") or ""
    if templates:
        return templates[0].get("template", "") or ""
    return cfg.get("prompt_template") or ""


def get_train_template_names(guild_id: int = None) -> list[str]:
    """Return the list of saved template names for a guild (premium feature)."""
    from config import get_train_config
    cfg = get_train_config(guild_id) if guild_id else {}
    return [t.get("name", "") for t in (cfg.get("templates") or []) if t.get("name")]

# ── Prompt builder ─────────────────────────────────────────────────────────────

def build_chatgpt_prompt(name: str, theme: str, tone: str, notes: str,
                          guild_id: int = None, template_name: str = None) -> str:
    """Format a ready-to-paste ChatGPT prompt using the guild's stored template."""
    template = get_prompt_template(guild_id, template_name=template_name)
    if template:
        return template.format(
            name=name,
            theme=theme,
            tone=tone if tone else "Default",
            notes=notes if notes else "None",
        )
    # Fallback if no template configured
    lines = [
        f"Member: {name}",
        f"Theme: {theme}" + (f" — {tone}" if tone and tone != "Default (match the theme)" else ""),
    ]
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join(lines)


# ── Embed builders ─────────────────────────────────────────────────────────────

def build_train_view_embed(schedule: dict, blurb_log: set) -> discord.Embed:
    """Build a scannable embed showing the next 14 days of the train schedule."""
    today = date.today()

    embed = discord.Embed(
        title="🚂 Alliance Train Schedule",
        color=discord.Color.gold(),
        timestamp=datetime.now(tz=ET),
    )

    if not schedule:
        embed.description = "*No schedule set. Use the **➕ Add** button below to add entries.*"
        return embed

    # ── Upcoming: next 14 days ───────────────────────────────────────────────
    upcoming_lines = []
    for i in range(14):
        d        = today + timedelta(days=i)
        date_str = d.isoformat()
        entry    = schedule.get(date_str)
        day_str  = f"{d:%A, %B} {d.day}"

        if i == 0:
            # Today
            if entry:
                name    = entry.get("name", "Unknown")
                is_bday = entry.get("theme", "").lower() == "birthday"
                bday    = " 🎂" if is_bday else ""
                done    = date_str in blurb_log
                status  = "✅ Done" if done else "⏳ Pending"
                upcoming_lines.append(f"🟢 {day_str} — {name}{bday} — {status}")
            else:
                upcoming_lines.append(f"🟢 {day_str} — [Empty]")
        else:
            if entry:
                name    = entry.get("name", "Unknown")
                is_bday = entry.get("theme", "").lower() == "birthday"
                bday    = " 🎂" if is_bday else ""
                upcoming_lines.append(f"{day_str} — {name}{bday}")
            else:
                upcoming_lines.append(f"{day_str} — [Empty]")

    embed.description = "\n".join(upcoming_lines)

    # ── Past 7 days ───────────────────────────────────────────────────────────
    past_names = []
    for i in range(1, 8):
        d        = today - timedelta(days=i)
        date_str = d.isoformat()
        entry    = schedule.get(date_str)
        if entry:
            past_names.append(entry.get("name", "Unknown"))

    if past_names:
        embed.add_field(
            name="✅ Past 7 Days",
            value=", ".join(past_names),
            inline=False,
        )

    return embed


# ── UI Views ───────────────────────────────────────────────────────────────────

class ThemeSelectView(discord.ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None
        themes = get_themes(guild_id)
        select = discord.ui.Select(
            placeholder="Choose a theme...",
            options=[discord.SelectOption(label=t, value=t) for t in themes],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = interaction.data["values"][0]
        await interaction.response.defer()
        self.stop()


class ToneSelectView(discord.ui.View):
    def __init__(self, guild_id: int = None):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None
        tones = get_tones(guild_id)
        select = discord.ui.Select(
            placeholder="Choose a tone...",
            options=[discord.SelectOption(label=t, value=t) for t in tones],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        self.selected = interaction.data["values"][0]
        await interaction.response.defer()
        self.stop()


class ReminderView(discord.ui.View):
    def __init__(self, cog, date_str: str, name: str):
        super().__init__(timeout=3600)
        self.cog      = cog
        self.date_str = date_str
        self.name     = name
        # Set by the reminder loop right after channel.send so on_timeout
        # can strip the button + post the re-initiate hint.
        self.message  = None

    async def on_timeout(self):
        """Strip the prompt button and tell the assignee how to re-open
        it. Without this, the button looks live for an hour after the
        view stopped listening — clicks fail with 'Interaction failed'."""
        from wizard_registry import expire_view_message
        await expire_view_message(self.message, command_hint="/train")

    @discord.ui.button(label="📋 View & Get Prompt", style=discord.ButtonStyle.success)
    async def launch(self, interaction: discord.Interaction, button: discord.ui.Button):
        role_names = [r.name for r in interaction.user.roles]
        from config import get_config
        cfg = get_config(interaction.guild_id)
        if not cfg:
            await interaction.response.send_message("⚙️ Bot not configured. Run `/setup`.", ephemeral=True)
            return
        req_role = cfg.leadership_role_name
        if req_role not in role_names:
            await interaction.response.send_message(
                f"⛔ You need the **{req_role}** role.", ephemeral=True
            )
            return

        button.disabled = True
        await interaction.response.edit_message(view=self)

        # Lazy import to avoid the train ⇆ train_ui circular import at load time
        from train_ui import run_blurb_wizard_for_entry
        await run_blurb_wizard_for_entry(
            self.cog.bot, interaction.channel, interaction.user,
            self.date_str, self.name, interaction.guild_id,
        )
        self.stop()


# ── Slash command guards ───────────────────────────────────────────────────────

def _is_leadership(interaction: discord.Interaction) -> bool:
    cfg = get_config(interaction.guild_id)
    if not cfg:
        return False
    return cfg.leadership_role_name in [r.name for r in interaction.user.roles]

def _in_channel(interaction: discord.Interaction) -> bool:
    """Accept commands in any channel or thread within the leadership category."""
    cfg = get_config(interaction.guild_id)
    if not cfg:
        return False
    cat_id = cfg.leadership_category_id
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return parent is not None and getattr(parent, "category_id", None) == cat_id
    return getattr(channel, "category_id", None) == cat_id

async def _guard(interaction: discord.Interaction) -> bool:
    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(
            "⚙️ This bot hasn't been set up yet. Run `/setup` to get started.", ephemeral=True
        )
        return False
    if not _in_channel(interaction):
        await interaction.response.send_message(
            "⛔ This command can only be used in the leadership channel.", ephemeral=True
        )
        return False
    if not _is_leadership(interaction):
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.", ephemeral=True
        )
        return False
    return True


# /train UI components live in train_ui.py and are imported lazily at their
# call sites (TrainActionView from the cog, run_blurb_wizard_for_entry from
# ReminderView.launch above) to avoid the train ⇆ train_ui circular import.


# Alias to avoid conflict with `date` parameter name in slash commands
date_cls = date


async def setup(bot: commands.Bot):
    from train_cog import TrainCog
    await bot.add_cog(TrainCog(bot))

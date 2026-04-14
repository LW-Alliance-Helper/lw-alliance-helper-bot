"""
logging.py — DS and CS event logging

Slash commands:
  /logds  — Log Desert Storm participation data
  /logcs  — Log Canyon Storm participation data

Writes to the "DS-CS Sit-outs" tab in Google Sheets and posts a
summary to the dedicated log thread (STORM_LOG_THREAD_ID).

DS columns:
  Date | Event | Vote Count | RTF No Vote | Sitting Out | Prior Sit-Out No Request

CS columns:
  Date | Event | (blank) | (blank) | Sitting Out | Prior Sit-Out No Request
"""

import asyncio
import json
import os
from datetime import date, datetime

import discord
from discord import app_commands
from discord.ext import commands

# ── Config ─────────────────────────────────────────────────────────────────────

GUILD_ID             = 1266229297723605052
GUILD                = discord.Object(id=GUILD_ID)
STORM_LOG_THREAD_ID  = 1483977424231469229
LOG_SHEET_NAME       = "DS-CS Sit-outs"
REQUIRED_ROLE_NAME   = "OGV"
LEADERSHIP_CAT_ID    = 1266243885743603783
WIZARD_TIMEOUT       = 600  # 10 minutes

# Sheet column headers (written on first use if the sheet is empty)
LOG_HEADERS = [
    "Date", "Event", "Vote Count", "RTF No Vote",
    "Sitting Out", "Prior Sit-Out No Request",
]


# ── Sheets helpers ─────────────────────────────────────────────────────────────

def _get_spreadsheet():
    import gspread
    from google.oauth2.service_account import Credentials
    scopes           = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        info  = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds    = Credentials.from_service_account_file(key_file, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.getenv("SPREADSHEET_ID"))


def _get_log_sheet():
    return _get_spreadsheet().worksheet(LOG_SHEET_NAME)


def _ensure_headers(ws):
    """Write headers to row 1 if the sheet is empty."""
    existing = ws.row_values(1)
    if not any(existing):
        ws.update("A1", [LOG_HEADERS], value_input_option="USER_ENTERED")


def append_log_row(event_type: str, log_date: date, vote_count: int | None,
                   rtf_no_vote: list[str], sitting_out: list[str],
                   prior_no_request: list[str]):
    """Append one row to the DS-CS Sit-outs sheet."""
    ws = _get_log_sheet()
    _ensure_headers(ws)
    row = [
        log_date.strftime("%-m/%-d/%Y"),
        event_type,
        str(vote_count) if vote_count is not None else "",
        ", ".join(rtf_no_vote),
        ", ".join(sitting_out),
        ", ".join(prior_no_request),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"[LOG] {event_type} log appended for {log_date.isoformat()}")


def load_member_names() -> list[str]:
    """
    Load member names from the active member tab (col E, starting row 10).
    Falls back to empty list on error.
    """
    try:
        from train import get_member_tab_name, _get_member_sheet
        tab_name = get_member_tab_name()
        ws       = _get_member_sheet(tab_name)
        rows     = ws.get_all_values()
        names    = []
        for row in rows[9:]:   # row 10 onward
            if len(row) >= 5:
                name = row[4].strip()  # col E
                if name:
                    names.append(name)
        print(f"[LOG] Loaded {len(names)} member names from '{tab_name}'")
        return names
    except Exception as e:
        print(f"[LOG] Error loading member names: {e}")
        return []


def get_prior_sitouts(event_type: str) -> list[str]:
    """
    Read the most recent log row for this event type and return the
    'Sitting Out' names — used to pre-populate the prior sit-out selector.
    """
    try:
        ws   = _get_log_sheet()
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return []
        # Walk backwards through rows (skip header)
        for row in reversed(rows[1:]):
            if len(row) >= 2 and row[1].strip().upper() == event_type.upper():
                sitting_out_raw = row[4].strip() if len(row) >= 5 else ""
                if sitting_out_raw:
                    return [n.strip() for n in sitting_out_raw.split(",") if n.strip()]
        return []
    except Exception as e:
        print(f"[LOG] Error reading prior sit-outs: {e}")
        return []


# ── Multi-select view (paginated for large rosters) ───────────────────────────

class NameSelectView(discord.ui.View):
    """
    Presents up to 25 names per select menu (Discord limit).
    If the roster is larger, splits into multiple selects on the same view.
    Stores all selected names across all selects in self.selected (set).
    Confirms with a Done button.
    """
    def __init__(self, names: list[str], label: str, optional: bool = False):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected : set[str] = set()
        self.confirmed           = False
        self.optional            = optional

        # Add select menus in chunks of 25 (Discord's max options per select)
        chunks = [names[i:i+25] for i in range(0, len(names), 25)]
        # Discord allows max 5 components per row, max 5 rows → max 5 selects
        for idx, chunk in enumerate(chunks[:4]):
            select = discord.ui.Select(
                placeholder=f"{label} ({idx+1})" if len(chunks) > 1 else label,
                options=[discord.SelectOption(label=n, value=n) for n in chunk],
                min_values=0,
                max_values=len(chunk),
                custom_id=f"name_select_{idx}",
            )
            async def _cb(interaction: discord.Interaction, s=select):
                for v in s.values:
                    self.selected.add(v)
                # Remove deselected values
                for opt in s.options:
                    if opt.value not in s.values:
                        self.selected.discard(opt.value)
                await interaction.response.defer()
            select.callback = _cb
            self.add_item(select)

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=4)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Skip (none)", style=discord.ButtonStyle.secondary, row=4)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.selected  = set()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


# ── Shared log flow ────────────────────────────────────────────────────────────

async def run_log_flow(bot, channel, user, event_type: str):
    """
    Shared interactive logging flow for DS and CS.
    DS:  date, vote count, RTF-no-vote, sitting out, prior-sit-out-no-request
    CS:  date, sitting out, prior-sit-out-no-request
    """
    is_ds = event_type.upper() == "DS"

    def check(m):
        return m.author == user and m.channel == channel

    async def send_and_wait(content, view=None):
        msg = await channel.send(content, view=view) if view else await channel.send(content)
        return msg

    event_label = "Desert Storm" if is_ds else "Canyon Storm"

    # Post a persistent header that stays visible throughout the entire flow
    header_msg = await channel.send(
        f"📋 **{event_label} Log** — started by {user.mention}"
    )

    # ── Step 1: Date ──────────────────────────────────────────────────────────
    prompt_msg = await channel.send(
        f"**Step 1 — Event date**\n"
        f"Type the date (e.g. `April 14`, `4/14`) or type `today`:"
    )
    try:
        reply = await bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT)
    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out. Use the log command to start again.")
        return
    try:
        await prompt_msg.delete()
        await reply.delete()
    except discord.HTTPException:
        pass

    raw_date = reply.content.strip().lower()
    if raw_date == "today":
        log_date = date.today()
    else:
        # Reuse train.py's date parser
        from train import parse_date_and_name
        parsed_d, _, _ = parse_date_and_name(f"{reply.content.strip()} - placeholder")
        if not parsed_d:
            await channel.send("⚠️ Could not parse that date. Use the log command to start again.")
            return
        log_date = parsed_d

    # ── Step 2 (DS only): Vote count ──────────────────────────────────────────
    vote_count = None
    if is_ds:
        prompt_msg = await channel.send(
            f"**Step 2 — Vote count**\n"
            f"How many members voted in the participation poll? (type a number)"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT)
        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out. Use the log command to start again.")
            return
        try:
            await prompt_msg.delete()
            await reply.delete()
        except discord.HTTPException:
            pass
        try:
            vote_count = int(reply.content.strip())
        except ValueError:
            await channel.send("⚠️ That doesn't look like a number. Use the log command to start again.")
            return

    # ── Load member names for selects ─────────────────────────────────────────
    names = await asyncio.get_event_loop().run_in_executor(None, load_member_names)
    if not names:
        await channel.send("⚠️ Could not load member names from the sheet. Check `/setmembertab` and try again.")
        return

    # ── Step 3 (DS only): RTF but no vote ─────────────────────────────────────
    rtf_no_vote = []
    if is_ds:
        step_num  = 3
        rtf_view  = NameSelectView(names, "Select members who RTF'd but didn't vote", optional=True)
        rtf_msg   = await channel.send(
            f"**Step {step_num} — Requested to Fight but did not vote**\n"
            f"Select any members who submitted RTF but did not vote in the poll. "
            f"Press **Skip** if none.",
            view=rtf_view,
        )
        await rtf_view.wait()
        try:
            await rtf_msg.delete()
        except discord.HTTPException:
            pass
        if not rtf_view.confirmed:
            await channel.send("⏰ Timed out. Use the log command to start again.")
            return
        rtf_no_vote = sorted(rtf_view.selected)

    # ── Step 4: Sitting out this week ─────────────────────────────────────────
    step_num   = 4 if is_ds else 2
    sit_view   = NameSelectView(names, "Select members sitting out this week", optional=True)
    sit_msg    = await channel.send(
        f"**Step {step_num} — Sitting out this week**\n"
        f"Select any members who are sitting out today. "
        f"Press **Skip** if none.",
        view=sit_view,
    )
    await sit_view.wait()
    try:
        await sit_msg.delete()
    except discord.HTTPException:
        pass
    if not sit_view.confirmed:
        await channel.send("⏰ Timed out. Use the log command to start again.")
        return
    sitting_out = sorted(sit_view.selected)

    # ── Step 5: Prior sit-outs who didn't request this week ───────────────────
    step_num     = 5 if is_ds else 3
    prior_names  = await asyncio.get_event_loop().run_in_executor(
        None, get_prior_sitouts, event_type
    )

    prior_no_request = []
    if prior_names:
        prior_view = NameSelectView(
            prior_names,
            "Select prior sit-outs who didn't request this week",
            optional=True,
        )
        prior_msg = await channel.send(
            f"**Step {step_num} — Prior sit-outs who did not {'vote' if is_ds else 'request'} this week**\n"
            f"These members sat out last time. Select any who did not {'vote' if is_ds else 'request to fight'} this week. "
            f"Press **Skip** if none.",
            view=prior_view,
        )
        await prior_view.wait()
        try:
            await prior_msg.delete()
        except discord.HTTPException:
            pass
        if not prior_view.confirmed:
            await channel.send("⏰ Timed out. Use the log command to start again.")
            return
        prior_no_request = sorted(prior_view.selected)
    else:
        await channel.send(
            f"**Step {step_num} — Prior sit-outs who did not request this week**\n"
            f"*(No prior sit-outs found in last log — skipping this step)*",
            delete_after=5,
        )

    # ── Save to sheet ─────────────────────────────────────────────────────────
    await channel.send("💾 Saving log...")
    try:
        await asyncio.get_event_loop().run_in_executor(
            None,
            append_log_row,
            event_type.upper(),
            log_date,
            vote_count,
            rtf_no_vote,
            sitting_out,
            prior_no_request,
        )
    except Exception as e:
        await channel.send(f"⚠️ Error saving to sheet: {e}")
        return

    # ── Build summary ─────────────────────────────────────────────────────────
    date_str    = log_date.strftime("%A, %B %-d, %Y")

    lines = [f"📋 **{event_label} Log — {date_str}**"]
    if is_ds:
        lines.append(f"**Votes:** {vote_count}")
        lines.append(f"**RTF No Vote:** {', '.join(rtf_no_vote) if rtf_no_vote else 'None'}")
    lines.append(f"**Sitting Out:** {', '.join(sitting_out) if sitting_out else 'None'}")
    lines.append(f"**Prior Sit-Out No {'Vote' if is_ds else 'Request'}:** {', '.join(prior_no_request) if prior_no_request else 'None'}")
    summary = "\n".join(lines)

    # Post to channel
    await channel.send(f"✅ **Log saved!**\n\n{summary}")

    # Post to dedicated log thread
    try:
        thread = bot.get_channel(STORM_LOG_THREAD_ID)
        if thread:
            await thread.send(summary)
        else:
            print(f"[LOG] Could not find log thread {STORM_LOG_THREAD_ID}")
    except Exception as e:
        print(f"[LOG] Error posting to log thread: {e}")


# ── Guard ──────────────────────────────────────────────────────────────────────

def _in_channel(interaction: discord.Interaction) -> bool:
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return parent is not None and getattr(parent, "category_id", None) == LEADERSHIP_CAT_ID
    return getattr(channel, "category_id", None) == LEADERSHIP_CAT_ID


async def _guard(interaction: discord.Interaction) -> bool:
    if not _in_channel(interaction):
        await interaction.response.send_message(
            "⛔ This command can only be used in the leadership channel.", ephemeral=True
        )
        return False
    if REQUIRED_ROLE_NAME not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to use this command.", ephemeral=True
        )
        return False
    return True


# ── Cog ────────────────────────────────────────────────────────────────────────

class LogCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="logds",
        description="Log Desert Storm participation data",
    )
    @app_commands.guilds(GUILD)
    async def logds(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        await interaction.response.send_message("📋 Starting DS log...", ephemeral=True)
        await run_log_flow(self.bot, interaction.channel, interaction.user, "DS")

    @app_commands.command(
        name="logcs",
        description="Log Canyon Storm participation data",
    )
    @app_commands.guilds(GUILD)
    async def logcs(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        await interaction.response.send_message("📋 Starting CS log...", ephemeral=True)
        await run_log_flow(self.bot, interaction.channel, interaction.user, "CS")


async def setup(bot: commands.Bot):
    await bot.add_cog(LogCog(bot))

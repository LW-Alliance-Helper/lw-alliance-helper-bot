"""
survey.py — Squad Powers Survey

A persistent button in the survey channel lets any OGV member submit their
squad powers. Clicking it opens a private thread, walks them through the
questions, then:
  - Updates their row in the Squad Powers sheet
  - Appends a timestamped row to the Survey History sheet
  - Archives the thread

Slash commands:
  /postsurvey  — Post (or repost) the persistent survey button (leadership only)
"""

import asyncio
import json
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

# ── Config ─────────────────────────────────────────────────────────────────────

GUILD_ID            = 1266229297723605052
GUILD               = discord.Object(id=GUILD_ID)
SURVEY_CHANNEL_ID   = 1399401720026759198
REQUIRED_ROLE_NAME  = "OGV"
LEADERSHIP_CAT_ID   = 1266243885743603783
SURVEY_TIMEOUT      = 600  # 10 minutes per step

SQUAD_POWERS_TAB    = "Squad Powers"
SURVEY_HISTORY_TAB  = "Survey History"
SQUAD_TYPES         = ["Missile", "Air", "Tank"]
PROFESSIONS         = ["War Leader", "Engineer"]
BANNER_OPTIONS      = ["Yes", "No"]
AID_REMOVAL_OPTIONS = ["Yes", "Only Medical Aid", "Only Ruin Removal", "No"]

# Squad Powers sheet columns (0-indexed):
# A=Username, B=Discord ID, C=1st Squad, D=1st Squad Type, E=2nd Squad,
# F=3rd Squad, G=Drone Level, H=Gorilla Level, I=THP, J=Total Kills,
# K=Profession, L=Banner, M=Aid/Removal, N=Date Modified

HISTORY_HEADERS = [
    "Timestamp", "Discord ID", "Username",
    "1st Squad", "1st Squad Type", "2nd Squad", "3rd Squad",
    "Drone Level", "Gorilla Level", "Total Hero Power (THP)",
    "Total Kills", "Profession", "Banner", "Aid/Removal",
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


def _to_millions(val: str) -> str:
    """Convert a user-entered number to millions. '301' → '301000000'."""
    try:
        return str(int(float(val.strip()) * 1_000_000))
    except (ValueError, AttributeError):
        return val


def update_squad_powers(discord_id: str, username: str, data: dict):
    """
    Update or insert a member's row in the Squad Powers sheet.
    Matches on Discord ID (col B). Appends if not found.
    """
    sh   = _get_spreadsheet()
    ws   = sh.worksheet(SQUAD_POWERS_TAB)
    rows = ws.get_all_values()

    now_str = datetime.now(timezone.utc).strftime("%-m/%-d/%Y")
    new_row = [
        username,
        discord_id,
        data.get("squad1_power", ""),
        data.get("squad1_type", ""),
        data.get("squad2_power", ""),
        data.get("squad3_power", ""),
        data.get("drone_level", ""),
        data.get("gorilla_level", ""),
        _to_millions(data.get("thp", "")),
        _to_millions(data.get("total_kills", "")),
        data.get("profession", ""),
        data.get("banner", ""),
        data.get("aid_removal", ""),
        now_str,
    ]

    for i, row in enumerate(rows):
        if len(row) >= 2 and row[1].strip() == discord_id:
            ws.update(f"A{i+1}", [new_row], value_input_option="USER_ENTERED")
            print(f"[SURVEY] Updated Squad Powers row {i+1} for {username}")
            return

    ws.append_row(new_row, value_input_option="USER_ENTERED")
    print(f"[SURVEY] Appended new Squad Powers row for {username}")


def append_survey_history(discord_id: str, username: str, data: dict):
    """Append a timestamped row to the Survey History sheet."""
    sh = _get_spreadsheet()
    ws = sh.worksheet(SURVEY_HISTORY_TAB)

    existing = ws.row_values(1)
    if not any(existing):
        ws.update("A1", [HISTORY_HEADERS], value_input_option="USER_ENTERED")
        try:
            ws.set_basic_filter()
        except Exception:
            pass

    now_str = datetime.now(timezone.utc).strftime("%-m/%-d/%Y %H:%M UTC")
    row = [
        now_str,
        discord_id,
        username,
        data.get("squad1_power", ""),
        data.get("squad1_type", ""),
        data.get("squad2_power", ""),
        data.get("squad3_power", ""),
        data.get("drone_level", ""),
        data.get("gorilla_level", ""),
        _to_millions(data.get("thp", "")),
        _to_millions(data.get("total_kills", "")),
        data.get("profession", ""),
        data.get("banner", ""),
        data.get("aid_removal", ""),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"[SURVEY] Appended Survey History row for {username}")


# ── Dropdown views ─────────────────────────────────────────────────────────────

class DropdownView(discord.ui.View):
    """Generic single-select dropdown that stops on selection."""
    def __init__(self, placeholder: str, options: list):
        super().__init__(timeout=SURVEY_TIMEOUT)
        self.selected  = None
        self.confirmed = False

        select = discord.ui.Select(
            placeholder=placeholder,
            options=[discord.SelectOption(label=o, value=o) for o in options],
            row=0,
        )
        async def _cb(interaction: discord.Interaction):
            self.selected  = select.values[0]
            self.confirmed = True
            select.disabled = True
            await interaction.response.edit_message(view=self)
            self.stop()
        select.callback = _cb
        self.add_item(select)


# ── Survey flow ────────────────────────────────────────────────────────────────

async def run_survey(bot, thread: discord.Thread, user: discord.Member):
    """Walk the user through all survey questions in their private thread."""

    def check(m):
        return m.author == user and m.channel == thread

    async def ask_number(prompt: str, max_chars: int = 10) -> str | None:
        """Post a prompt and wait for a typed number reply."""
        msg = await thread.send(prompt)
        try:
            reply = await bot.wait_for("message", check=check, timeout=SURVEY_TIMEOUT)
        except asyncio.TimeoutError:
            await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
            return None
        try:
            await msg.delete()
        except discord.HTTPException:
            pass
        val = reply.content.strip()
        if len(val) > max_chars:
            await thread.send(f"⚠️ That entry is too long (max {max_chars} characters). Please try the survey again.")
            return None
        return val

    async def ask_dropdown(prompt: str, options: list, placeholder: str) -> str | None:
        """Post a prompt with a dropdown and wait for selection."""
        view = DropdownView(placeholder, options)
        msg  = await thread.send(prompt, view=view)
        await view.wait()
        if not view.confirmed:
            await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
            return None
        return view.selected

    data = {}

    # ── Q1: 1st Squad Power ───────────────────────────────────────────────────
    val = await ask_number("**1st Squad Power** (e.g. 43.27)\n*Maximum characters: 5*", max_chars=5)
    if val is None:
        return
    data["squad1_power"] = val

    # ── Q2: 1st Squad Type ────────────────────────────────────────────────────
    val = await ask_dropdown("**1st Squad Type**", SQUAD_TYPES, "Select squad type...")
    if val is None:
        return
    data["squad1_type"] = val

    # ── Q3: 2nd Squad Power ───────────────────────────────────────────────────
    val = await ask_number("**2nd Squad Power** (e.g. 43.27)\n*Maximum characters: 5*", max_chars=5)
    if val is None:
        return
    data["squad2_power"] = val

    # ── Q4: 3rd Squad Power ───────────────────────────────────────────────────
    val = await ask_number("**3rd Squad Power** (e.g. 43.27)\n*Maximum characters: 5*", max_chars=5)
    if val is None:
        return
    data["squad3_power"] = val

    # ── Q5: Drone Level ───────────────────────────────────────────────────────
    val = await ask_number("**Drone Level** (e.g. 243)")
    if val is None:
        return
    data["drone_level"] = val

    # ── Q6: Gorilla Level ─────────────────────────────────────────────────────
    val = await ask_number("**Gorilla Level** (e.g. 70)")
    if val is None:
        return
    data["gorilla_level"] = val

    # ── Q7: Total Hero Power ──────────────────────────────────────────────────
    val = await ask_number(
        "**Total Hero Power (THP)**\n*Rounded to nearest million (e.g. 301)\nMaximum characters: 3*",
        max_chars=3,
    )
    if val is None:
        return
    data["thp"] = val

    # ── Q8: Total Kills ───────────────────────────────────────────────────────
    val = await ask_number("**Total Kills** (e.g. 55.40)\n*Maximum characters: 5*", max_chars=5)
    if val is None:
        return
    data["total_kills"] = val

    # ── Q9: Profession ────────────────────────────────────────────────────────
    val = await ask_dropdown("**What is your Profession?**", PROFESSIONS, "Select profession...")
    if val is None:
        return
    data["profession"] = val

    # ── Q10: Profession follow-up ─────────────────────────────────────────────
    if val == "War Leader":
        follow = await ask_dropdown(
            "**Do you have a charge banner?**",
            BANNER_OPTIONS,
            "Select...",
        )
        if follow is None:
            return
        data["banner"]      = follow
        data["aid_removal"] = ""
    else:
        follow = await ask_dropdown(
            "**Do you have medical aid and ruin removal?**",
            AID_REMOVAL_OPTIONS,
            "Select...",
        )
        if follow is None:
            return
        data["aid_removal"] = follow
        data["banner"]      = ""

    # ── Save to sheets ────────────────────────────────────────────────────────
    await thread.send("⏳ Saving your responses...")
    try:
        discord_id = str(user.id)
        username   = user.display_name
        loop       = asyncio.get_event_loop()
        await loop.run_in_executor(None, update_squad_powers,    discord_id, username, data)
        await loop.run_in_executor(None, append_survey_history,  discord_id, username, data)
    except Exception as e:
        await thread.send(f"⚠️ There was an error saving your responses: {e}\nPlease let leadership know.")
        print(f"[SURVEY] Error saving for {user.display_name}: {e}")
        return

    # ── Completion message ────────────────────────────────────────────────────
    class CloseThreadView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.closed = False

        @discord.ui.button(label="❌ Close Thread", style=discord.ButtonStyle.danger)
        async def close_now(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.closed = True
            await interaction.response.send_message("Closing thread...", ephemeral=True)
            self.stop()

        async def on_timeout(self):
            self.closed = True
            self.stop()

    close_view = CloseThreadView()
    await thread.send(
        f"✅ **Survey Complete!**\n\n"
        f"Your response has been saved successfully! Thanks for keeping your stats up to date, "
        f"it helps us to balance teams, track alliance growth, and prepare for season events.\n\n"
        f"This thread will be deleted in 60 seconds or you can close it now.",
        view=close_view,
    )

    await close_view.wait()
    await asyncio.sleep(2)
    try:
        await thread.delete()
    except discord.HTTPException as e:
        print(f"[SURVEY] Could not delete thread: {e}")


# ── Persistent survey button ───────────────────────────────────────────────────

class SurveyButtonView(discord.ui.View):
    """
    Persistent view — survives bot restarts because it is re-registered
    on every on_ready via bot.add_view().
    """
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(
        label="📋 Answer",
        style=discord.ButtonStyle.success,
        custom_id="survey_answer_button",
    )
    async def answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check OGV role
        if REQUIRED_ROLE_NAME not in [r.name for r in interaction.user.roles]:
            await interaction.response.send_message(
                f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to fill out this survey.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "🚀 Let's get started! Your private thread is being created...",
            ephemeral=True,
        )

        # Create private thread in the survey channel
        channel = interaction.channel
        thread_name = f"survey-squad-powers-{interaction.user.name}"
        try:
            thread = await channel.create_thread(
                name=thread_name,
                type=discord.ChannelType.private_thread,
                invitable=False,
            )
            await thread.add_user(interaction.user)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"⚠️ Could not create your survey thread: {e}",
                ephemeral=True,
            )
            return

        await run_survey(interaction.client, thread, interaction.user)


# ── Guard (leadership only) ────────────────────────────────────────────────────

def _in_leadership(interaction: discord.Interaction) -> bool:
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return parent is not None and getattr(parent, "category_id", None) == LEADERSHIP_CAT_ID
    return getattr(channel, "category_id", None) == LEADERSHIP_CAT_ID


async def _guard(interaction: discord.Interaction) -> bool:
    if not _in_leadership(interaction):
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

class SurveyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Re-register the persistent view so buttons work after restarts
        self.bot.add_view(SurveyButtonView())

    @app_commands.command(
        name="postsurvey",
        description="Post (or repost) the squad powers survey button in the survey channel",
    )
    @app_commands.guilds(GUILD)
    async def postsurvey(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        channel = self.bot.get_channel(SURVEY_CHANNEL_ID)
        if channel is None:
            await interaction.followup.send("⚠️ Could not find the survey channel.", ephemeral=True)
            return

        view = SurveyButtonView()
        await channel.send(
            "**Let us know your Squad Powers!**\n\n"
            "Please fill out this survey each week, if possible, to help us keep track of "
            "squad powers, better balance our Desert Storm teams, track alliance growth, "
            "and prepare for season events!\n\n"
            "*Role required: @OGV*",
            view=view,
        )
        await interaction.followup.send("✅ Survey button posted.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SurveyCog(bot))

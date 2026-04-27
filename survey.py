"""
survey.py — Squad Powers Survey

A persistent button in the survey channel lets any OGV member submit their
squad powers. Clicking it opens a private thread, walks them through the
questions, then:
  - Updates their row in the Squad Powers sheet
  - Appends a timestamped row to the Survey History sheet
  - Archives the thread

Slash commands:
  /survey_post — Post (or repost) the persistent survey button (leadership only)
"""

import asyncio
import json
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from config import get_config

# ── Config ─────────────────────────────────────────────────────────────────────

SURVEY_TIMEOUT      = 600  # 10 minutes per step

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

def _get_spreadsheet(guild_id: int = None):
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
    from config import get_spreadsheet_id
    sheet_id = get_spreadsheet_id(guild_id)
    return gc.open_by_key(sheet_id)


def _to_millions(val: str) -> str:
    """Convert a user-entered number to millions. '301' → '301000000'."""
    try:
        return str(int(float(val.strip()) * 1_000_000))
    except (ValueError, AttributeError):
        return val


def update_squad_powers(discord_id: str, username: str, data: dict, guild_id: int = None):
    """
    Update or insert a member's row in the Squad Powers sheet.
    Columns are derived from the guild's survey question config.
    """
    from config import get_config, get_survey_config
    survey_cfg = get_survey_config(guild_id) if guild_id else {}
    questions  = survey_cfg.get("questions") or []
    cfg        = get_config(guild_id)
    sh         = _get_spreadsheet(guild_id)
    ws         = sh.worksheet(cfg.tab_squad_powers if cfg else survey_cfg.get("tab_squad_powers", "Squad Powers"))
    rows       = ws.get_all_values()

    _now     = datetime.now(timezone.utc)
    now_str  = f"{_now.month}/{_now.day}/{_now.year}"
    q_keys   = [q.get("key", f"field_{i}") for i, q in enumerate(questions)]
    q_labels = [q.get("label", k) for k, q in zip(q_keys, questions)]

    # Ensure header row exists
    if not rows or not any(rows[0]):
        header = ["Username", "Discord ID"] + q_labels + ["Date Modified"]
        ws.update("A1", [header], value_input_option="USER_ENTERED")
        rows = ws.get_all_values()

    new_row = [username, discord_id] + [data.get(k, "") for k in q_keys] + [now_str]

    for i, row in enumerate(rows):
        if len(row) >= 2 and row[1].strip() == discord_id:
            ws.update(f"A{i+1}", [new_row], value_input_option="USER_ENTERED")
            print(f"[SURVEY] Updated Squad Powers row {i+1} for {username}")
            return

    ws.append_row(new_row, value_input_option="USER_ENTERED")
    print(f"[SURVEY] Appended new Squad Powers row for {username}")


def append_survey_history(discord_id: str, username: str, data: dict, guild_id: int = None):
    """Append a timestamped row to the Survey History sheet."""
    from config import get_config, get_survey_config
    survey_cfg = get_survey_config(guild_id) if guild_id else {}
    questions  = survey_cfg.get("questions") or []
    cfg        = get_config(guild_id)
    sh         = _get_spreadsheet(guild_id)
    ws         = sh.worksheet(cfg.tab_survey_history if cfg else survey_cfg.get("tab_history", "Survey History"))

    q_keys   = [q.get("key", f"field_{i}") for i, q in enumerate(questions)]
    q_labels = [q.get("label", k) for k, q in zip(q_keys, questions)]

    existing = ws.row_values(1)
    if not any(existing):
        header = ["Timestamp", "Discord ID", "Username"] + q_labels
        ws.update("A1", [header], value_input_option="USER_ENTERED")
        try:
            ws.set_basic_filter()
        except Exception:
            pass

    _now    = datetime.now(timezone.utc)
    now_str = f"{_now.month}/{_now.day}/{_now.year} {_now:%H:%M} UTC"
    row     = [now_str, discord_id, username] + [data.get(k, "") for k in q_keys]
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"[SURVEY] Appended Survey History row for {username}")


# ── Dropdown views ─────────────────────────────────────────────────────────────

class DropdownView(discord.ui.View):
    """Generic single-select dropdown that persists the selected value after selection."""
    def __init__(self, placeholder: str, options: list, label: str = ""):
        super().__init__(timeout=SURVEY_TIMEOUT)
        self.selected  = None
        self.confirmed = False
        self.label     = label

        select = discord.ui.Select(
            placeholder=placeholder,
            options=[discord.SelectOption(label=o, value=o) for o in options],
            row=0,
        )
        async def _cb(interaction: discord.Interaction):
            self.selected  = select.values[0]
            self.confirmed = True
            select.disabled = True
            await interaction.response.edit_message(
                content=f"**{self.label}** {self.selected}",
                view=self,
            )
            self.stop()
        select.callback = _cb
        self.add_item(select)


# ── Survey flow ────────────────────────────────────────────────────────────────

async def run_survey(bot, thread: discord.Thread, user: discord.Member):
    """Walk the user through all survey questions from the guild's config."""
    gid = user.guild.id if hasattr(user, "guild") and user.guild else None

    from config import get_survey_config
    survey_cfg = get_survey_config(gid) if gid else {}
    questions  = survey_cfg.get("questions") or []

    if not questions:
        await thread.send("⚠️ No survey questions configured. Ask leadership to run `/setup_survey`.")
        return

    def check(m):
        return m.author == user and m.channel == thread

    async def ask_number(prompt: str, max_chars: int = 10) -> str | None:
        await thread.send(prompt)
        try:
            reply = await bot.wait_for("message", check=check, timeout=SURVEY_TIMEOUT)
        except asyncio.TimeoutError:
            await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
            return None
        val = reply.content.strip()
        if len(val) > max_chars:
            await thread.send(f"⚠️ That entry is too long (max {max_chars} characters). Please try the survey again.")
            return None
        return val

    async def ask_dropdown(prompt: str, options: list, placeholder: str, label: str = "") -> str | None:
        view = DropdownView(placeholder, options, label=label)
        await thread.send(prompt, view=view)
        await view.wait()
        if not view.confirmed:
            await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
            return None
        return view.selected

    data = {}

    for i, q in enumerate(questions):
        key         = q.get("key", f"field_{i}")
        label       = q.get("label", f"Question {i+1}")
        qtype       = q.get("type", "text")
        options     = q.get("options", [])
        placeholder = q.get("placeholder", "")
        max_chars   = q.get("max_chars", 10) or 10

        if qtype == "text":
            hint = f"\n*{placeholder}*" if placeholder else ""
            if max_chars:
                hint += f"\n*Maximum characters: {max_chars}*"
            val = await ask_number(f"**{label}**{hint}", max_chars=max_chars)
        elif qtype == "dropdown":
            val = await ask_dropdown(
                f"**{label}**",
                options,
                placeholder or f"Select {label}...",
                label=f"{label}:",
            )
        else:
            val = await ask_number(f"**{label}**", max_chars=max_chars)

        if val is None:
            return
        data[key] = val

    # ── Save to sheets ────────────────────────────────────────────────────────
    await thread.send("⏳ Saving your responses...")
    try:
        discord_id = str(user.id)
        username   = user.display_name
        loop       = asyncio.get_event_loop()
        await loop.run_in_executor(None, update_squad_powers,   discord_id, username, data, gid)
        await loop.run_in_executor(None, append_survey_history, discord_id, username, data, gid)
    except Exception as e:
        await thread.send(f"⚠️ There was an error saving your responses: {e}\nPlease let leadership know.")
        print(f"[SURVEY] Error saving for {user.display_name}: {e}")
        return

    # ── Notify leadership ─────────────────────────────────────────────────────
    try:
        from config import get_config as _sgc
        _scfg = _sgc(user.guild.id) if hasattr(user, 'guild') else None
        _notify_id = _scfg.survey_notify_channel_id if _scfg else 0
        notify_channel = bot.get_channel(_notify_id)
        if notify_channel:
            _now     = datetime.now(timezone.utc)
            _hour12  = _now.hour % 12 or 12
            date_str = f"{_now:%B} {_now.day}, {_now.year} at {_hour12}:{_now:%M %p} UTC"
            profession_line = data.get("profession", "")
            if data.get("banner"):
                profession_line += f" — Charge Banner: {data['banner']}"
            elif data.get("aid_removal"):
                profession_line += f" — Medical Aid/Ruin Removal: {data['aid_removal']}"

            embed = discord.Embed(
                title="📋 New Survey Response",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Member", value=user.mention, inline=True)
            embed.add_field(name="Submitted", value=date_str, inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            embed.add_field(
                name="Responses",
                value=(
                    f"**1st Squad Power:** {data.get('squad1_power', '—')}\n"
                    f"**1st Squad Type:** {data.get('squad1_type', '—')}\n"
                    f"**2nd Squad Power:** {data.get('squad2_power', '—')}\n"
                    f"**3rd Squad Power:** {data.get('squad3_power', '—')}\n"
                    f"**Drone Level:** {data.get('drone_level', '—')}\n"
                    f"**Gorilla Level:** {data.get('gorilla_level', '—')}\n"
                    f"**THP:** {data.get('thp', '—')}M\n"
                    f"**Total Kills:** {data.get('total_kills', '—')}M\n"
                    f"**Profession:** {profession_line}"
                ),
                inline=False,
            )
            await notify_channel.send(embed=embed)
    except Exception as e:
        print(f"[SURVEY] Error sending leadership notification: {e}")

    await _finalize_survey_thread(thread)


class CloseThreadView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.closed = False

    @discord.ui.button(label="❌ Close Thread", style=discord.ButtonStyle.secondary)
    async def close_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.closed = True
        await interaction.response.defer()
        self.stop()

    async def on_timeout(self):
        self.closed = True
        self.stop()


async def _finalize_survey_thread(thread):
    """Send the success embed with a Close Thread button, then delete the thread."""
    embed = discord.Embed(
        title="✅ Survey Complete!",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="Thank you!",
        value=(
            "Your response has been saved successfully! Thanks for keeping your stats up to date, "
            "it helps us to balance teams, track alliance growth, and prepare for season events."
        ),
        inline=False,
    )
    embed.set_footer(text="This thread will be deleted in 60 seconds or you can close it now.")

    close_view = CloseThreadView()
    await thread.send(embed=embed, view=close_view)

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
        cfg = get_config(interaction.guild_id)
        if not cfg or not cfg.setup_complete:
            await interaction.response.send_message("⚙️ This bot hasn't been set up yet.", ephemeral=True)
            return
        if cfg.member_role_name not in [r.name for r in interaction.user.roles]:
            await interaction.response.send_message(
                f"⛔ You need the **{cfg.member_role_name}** role to fill out this survey.",
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

        await interaction.followup.send(
            f"🚀 Your thread is ready — head over here to get started: {thread.mention}",
            ephemeral=True,
        )

        await run_survey(interaction.client, thread, interaction.user)


# ── Guard (leadership only) ────────────────────────────────────────────────────

def _in_leadership(interaction: discord.Interaction) -> bool:
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
    if not _in_leadership(interaction):
        await interaction.response.send_message(
            "⛔ This command can only be used in the leadership channel.", ephemeral=True
        )
        return False
    if cfg.leadership_role_name not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.", ephemeral=True
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
        name="survey_post",
        description="Post (or repost) the squad powers survey button in the survey channel",
    )
    async def survey_post(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        from config import get_config
        cfg     = get_config(interaction.guild_id)
        if not cfg:
            await interaction.followup.send("⚙️ Bot not configured. Run `/setup` first.", ephemeral=True)
            return
        channel = self.bot.get_channel(cfg.survey_channel_id)
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

    @app_commands.command(
        name="survey",
        description="Show the configured survey questions",
    )
    async def survey(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        from config import get_survey_config
        scfg = get_survey_config(interaction.guild_id)
        questions = scfg.get("questions") or []

        embed = discord.Embed(
            title="📋 Survey Configuration",
            color=discord.Color.blurple(),
        )

        if not questions:
            embed.description = "*No survey questions configured. Run `/setup_survey` to add some.*"
        else:
            lines = []
            for i, q in enumerate(questions, start=1):
                qtype = q.get("type", "text")
                if qtype == "dropdown":
                    options = ", ".join(q.get("options") or [])
                    lines.append(f"**{i}. {q['label']}** *(dropdown: {options})*")
                else:
                    lines.append(f"**{i}. {q['label']}** *(text)*")
                if q.get("help"):
                    lines.append(f"   _{q['help']}_")
            embed.description = "\n".join(lines)[:4000]

        embed.add_field(name="Stats Tab",       value=scfg.get("tab_squad_powers", "*not set*"), inline=False)
        embed.add_field(name="History Tab",     value=scfg.get("tab_history", "*not set*"),      inline=False)
        embed.add_field(
            name="Intro Message",
            value="✅ Configured" if scfg.get("intro_message") else "❌ Not configured",
            inline=False,
        )
        embed.set_footer(text="Run /setup_survey to update. Run /survey_post to post the button.")

        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SurveyCog(bot))

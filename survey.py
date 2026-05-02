"""
survey.py — Squad Powers Survey

A persistent button in the survey channel lets any alliance member submit
their squad powers. Clicking it opens a private thread, walks them through
the questions, then:
  - Updates their row in the Squad Powers sheet
  - Appends a timestamped row to the Survey History sheet
  - Archives the thread

Slash commands:
  /survey_post   — Post (or repost) the persistent survey button (leadership)
  /survey        — Show configured survey(s) — list view when multiple are configured
  /survey_remind — DM roster members to fill the survey (Premium)

Multi-survey support (Premium): a guild may have a "default" survey plus
any number of extras, each with its own questions, channel, intro message,
and reminder DM body. The persistent answer button is registered as a
DynamicItem so each extra survey gets its own button keyed by survey_id.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone, date as date_cls

import discord
from discord import app_commands
from discord.ext import commands, tasks
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


def update_squad_powers(discord_id: str, username: str, data: dict,
                        guild_id: int = None, survey: dict | None = None):
    """
    Update or insert a member's row in the Squad Powers sheet.
    Columns are derived from the survey's question config. If `survey` is
    provided (multi-survey path), its questions/tab override the default.
    """
    from config import get_config, get_survey_config
    if survey is None:
        survey_cfg = get_survey_config(guild_id) if guild_id else {}
    else:
        survey_cfg = survey
    questions  = survey_cfg.get("questions") or []
    cfg        = get_config(guild_id)
    sh         = _get_spreadsheet(guild_id)
    # Prefer the survey's own tab name. Fall back to guild-level for legacy.
    tab_name   = (
        survey_cfg.get("tab_squad_powers")
        or (cfg.tab_squad_powers if cfg else "Squad Powers")
    )
    ws         = sh.worksheet(tab_name)
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


def append_survey_history(discord_id: str, username: str, data: dict,
                          guild_id: int = None, survey: dict | None = None):
    """Append a timestamped row to the Survey History sheet."""
    from config import get_config, get_survey_config
    if survey is None:
        survey_cfg = get_survey_config(guild_id) if guild_id else {}
    else:
        survey_cfg = survey
    questions  = survey_cfg.get("questions") or []
    cfg        = get_config(guild_id)
    sh         = _get_spreadsheet(guild_id)
    tab_name   = (
        survey_cfg.get("tab_history")
        or (cfg.tab_survey_history if cfg else "Survey History")
    )
    ws         = sh.worksheet(tab_name)

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
            content = f"**{self.label}** {self.selected}"
            try:
                await interaction.response.edit_message(content=content, view=self)
            except discord.NotFound:
                # Interaction token expired (10062) — fall back to a direct edit
                # so the dropdown still shows the selection and the survey continues.
                try:
                    await interaction.message.edit(content=content, view=self)
                except discord.HTTPException:
                    pass
            self.stop()
        select.callback = _cb
        self.add_item(select)


# ── Survey flow ────────────────────────────────────────────────────────────────

async def run_survey(bot, thread: discord.Thread, user: discord.Member,
                     survey: dict | None = None):
    """
    Walk the user through all survey questions.

    `survey` is an optional pre-fetched survey dict (default or extra). When
    omitted, falls back to the guild's default survey config.
    """
    gid = user.guild.id if hasattr(user, "guild") and user.guild else None

    from config import get_survey_config
    if survey is None:
        survey_cfg = get_survey_config(gid) if gid else {}
    else:
        survey_cfg = survey
    questions  = survey_cfg.get("questions") or []

    if not questions:
        await thread.send("⚠️ No survey questions configured. Ask leadership to run `/setup_survey`.")
        return

    def check(m):
        return m.author == user and m.channel == thread

    async def ask_number(prompt: str, max_chars: int = 10) -> str | None:
        """
        Text question with a length cap. On too-long input, re-prompts the
        same question (up to 5 attempts) so the user doesn't have to restart
        the whole survey for one slip — e.g. typing `153,725,881` instead
        of `154` for a THP-in-millions field.
        """
        attempts_left = 5
        first_pass    = True
        while attempts_left > 0:
            if first_pass:
                await thread.send(prompt)
                first_pass = False
            try:
                reply = await bot.wait_for("message", check=check, timeout=SURVEY_TIMEOUT)
            except asyncio.TimeoutError:
                await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
                return None
            val = reply.content.strip()
            if len(val) > max_chars:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ That entry is too long (max {max_chars} characters). "
                    f"Please re-enter your answer for this question."
                )
                continue
            return val

        await thread.send(
            "⚠️ Too many invalid attempts on this question. "
            "Cancelling the survey — click the Answer button to start over when you're ready."
        )
        return None

    async def ask_dropdown(prompt: str, options: list, placeholder: str, label: str = "") -> str | None:
        view = DropdownView(placeholder, options, label=label)
        await thread.send(prompt, view=view)
        await view.wait()
        if not view.confirmed:
            await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
            return None
        return view.selected

    async def ask_numeric(prompt: str, min_val: float | None = None,
                           max_val: float | None = None) -> str | None:
        """
        Premium type: numeric input with optional min/max bounds.

        On invalid input or out-of-bounds values the user is re-prompted
        for the same question (up to 5 attempts) instead of having the
        whole survey cancel out from under them.
        """
        full = prompt
        if min_val is not None or max_val is not None:
            bits = []
            if min_val is not None: bits.append(f"min: {min_val}")
            if max_val is not None: bits.append(f"max: {max_val}")
            full += f"\n*({', '.join(bits)})*"

        attempts_left = 5
        first_pass    = True
        while attempts_left > 0:
            if first_pass:
                await thread.send(full)
                first_pass = False
            try:
                reply = await bot.wait_for("message", check=check, timeout=SURVEY_TIMEOUT)
            except asyncio.TimeoutError:
                await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
                return None
            raw = reply.content.strip()
            try:
                n = float(raw) if "." in raw else int(raw)
            except ValueError:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ `{raw}` isn't a number. Please re-enter your answer for this question."
                )
                continue
            if min_val is not None and n < min_val:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ Must be at least **{min_val}**. Please re-enter your answer for this question."
                )
                continue
            if max_val is not None and n > max_val:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ Must be at most **{max_val}**. Please re-enter your answer for this question."
                )
                continue
            return str(n)

        await thread.send(
            "⚠️ Too many invalid attempts on this question. "
            "Cancelling the survey — click the Answer button to start over when you're ready."
        )
        return None

    async def ask_multi_select(prompt: str, options: list,
                                placeholder: str, label: str = "") -> str | None:
        """Premium type: Discord multi-select (up to len(options) picks).
        Returns a comma-joined string."""
        if not options:
            await thread.send(f"⚠️ Question has no options configured. Please contact leadership.")
            return None

        view = discord.ui.View(timeout=SURVEY_TIMEOUT)
        result = {"values": None}

        select = discord.ui.Select(
            placeholder=placeholder or f"Select {label}…",
            min_values=1,
            max_values=min(len(options), 25),
            options=[discord.SelectOption(label=o, value=o) for o in options[:25]],
        )

        async def _cb(inter: discord.Interaction):
            result["values"] = list(select.values)
            select.disabled  = True
            content = f"**{label}** {', '.join(result['values'])}"
            try:
                await inter.response.edit_message(content=content, view=view)
            except discord.NotFound:
                try:
                    await inter.message.edit(content=content, view=view)
                except discord.HTTPException:
                    pass
            view.stop()
        select.callback = _cb
        view.add_item(select)

        await thread.send(prompt, view=view)
        await view.wait()
        if result["values"] is None:
            await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
            return None
        return ", ".join(result["values"])

    async def ask_date(prompt: str, date_format: str = "%m/%d/%Y") -> str | None:
        """
        Premium type: parse a date with strptime, return as ISO string.

        On a parse failure the user is re-prompted for the same question
        (up to 5 attempts) instead of having the whole survey cancel out.
        """
        from datetime import datetime as _dt
        full = prompt + f"\n*(format: `{date_format}`)*"

        attempts_left = 5
        first_pass    = True
        while attempts_left > 0:
            if first_pass:
                await thread.send(full)
                first_pass = False
            try:
                reply = await bot.wait_for("message", check=check, timeout=SURVEY_TIMEOUT)
            except asyncio.TimeoutError:
                await thread.send("⏰ Survey timed out. You can start again by clicking the Answer button.")
                return None
            raw = reply.content.strip()
            try:
                d = _dt.strptime(raw, date_format).date()
            except ValueError:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ `{raw}` doesn't match `{date_format}`. "
                    f"Please re-enter your answer for this question."
                )
                continue
            return d.isoformat()

        await thread.send(
            "⚠️ Too many invalid attempts on this question. "
            "Cancelling the survey — click the Answer button to start over when you're ready."
        )
        return None

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
        elif qtype == "numeric":
            val = await ask_numeric(
                f"**{label}**" + (f"\n*{placeholder}*" if placeholder else ""),
                min_val=q.get("min"),
                max_val=q.get("max"),
            )
        elif qtype == "multi_select":
            val = await ask_multi_select(
                f"**{label}**",
                options,
                placeholder or f"Select {label}...",
                label=label,
            )
        elif qtype == "date":
            val = await ask_date(
                f"**{label}**" + (f"\n*{placeholder}*" if placeholder else ""),
                date_format=q.get("date_format", "%m/%d/%Y"),
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
        await loop.run_in_executor(
            None, update_squad_powers,   discord_id, username, data, gid, survey_cfg,
        )
        await loop.run_in_executor(
            None, append_survey_history, discord_id, username, data, gid, survey_cfg,
        )
    except Exception as e:
        await thread.send(f"⚠️ There was an error saving your responses: {e}\nPlease let leadership know.")
        print(f"[SURVEY] Error saving for {user.display_name}: {e}")
        return

    # ── Notify leadership ─────────────────────────────────────────────────────
    try:
        from config import get_config as _sgc
        _scfg = _sgc(user.guild.id) if hasattr(user, 'guild') else None
        # Extras may override the notify channel; fall back to guild-level.
        _notify_id = (
            int(survey_cfg.get("notify_channel_id") or 0)
            or (_scfg.survey_notify_channel_id if _scfg else 0)
        )
        notify_channel = bot.get_channel(_notify_id)
        if notify_channel:
            _now     = datetime.now(timezone.utc)
            _hour12  = _now.hour % 12 or 12
            date_str = f"{_now:%B} {_now.day}, {_now.year} at {_hour12}:{_now:%M %p} UTC"
            embed = discord.Embed(
                title="📋 New Survey Response",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Member", value=user.mention, inline=True)
            embed.add_field(name="Submitted", value=date_str, inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            # Iterate the actual configured questions in order so guilds with
            # custom surveys see their own labels (not a hardcoded LW set).
            response_lines = []
            for q in questions:
                key   = q.get("key", "")
                label = q.get("label", key) or key
                if not key:
                    continue
                value = data.get(key, "")
                if value == "" or value is None:
                    value = "—"
                response_lines.append(f"**{label}:** {value}")

            embed.add_field(
                name="Responses",
                value="\n".join(response_lines)[:1024] if response_lines else "*(no responses)*",
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

# Custom-id format for the multi-survey answer button. The capture group
# names the survey (`default` for the guild's main survey, otherwise the
# `survey_id` from `guild_extra_surveys`).
SURVEY_BUTTON_CUSTOM_ID_PREFIX = "survey_answer_button"
SURVEY_BUTTON_CUSTOM_ID_RE     = re.compile(
    r"^survey_answer_button(?::(?P<survey_id>[A-Za-z0-9_\-]{1,64}))?$"
)


async def _start_survey_answer_flow(interaction: discord.Interaction,
                                    survey_id: str = "default"):
    """Shared handler for both legacy and dynamic survey-answer buttons."""
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

    from config import get_survey
    survey_cfg = get_survey(interaction.guild_id, survey_id)
    if survey_cfg is None:
        await interaction.response.send_message(
            "⚠️ This survey is no longer configured. Ask leadership to repost it.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "🚀 Let's get started! Your private thread is being created...",
        ephemeral=True,
    )

    # Create a private thread named after the chosen survey (slugified).
    title_source = (
        survey_cfg.get("survey_name")
        or survey_cfg.get("tab_squad_powers")
        or "survey"
    )
    slug = re.sub(r"[^a-z0-9]+", "-", title_source.lower()).strip("-") or "survey"
    channel     = interaction.channel
    thread_name = f"survey-{slug}-{interaction.user.name}"[:100]
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

    await run_survey(interaction.client, thread, interaction.user, survey=survey_cfg)


class SurveyButtonView(discord.ui.View):
    """
    Persistent view for the **default** survey button. Re-registered every
    on_ready via `bot.add_view(SurveyButtonView())`. Extra surveys use the
    `DynamicSurveyButton` below so each one keeps its own custom_id.
    """
    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(
        label="📋 Answer",
        style=discord.ButtonStyle.success,
        custom_id="survey_answer_button",
    )
    async def answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_survey_answer_flow(interaction, survey_id="default")


class DynamicSurveyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"survey_answer_button:(?P<survey_id>[A-Za-z0-9_\-]{1,64})",
):
    """
    Persistent button for an extra (non-default) survey. Each extra survey
    posts its own button whose custom_id encodes the `survey_id`. Discord
    re-creates these via `from_custom_id` after a bot restart.
    """

    def __init__(self, survey_id: str):
        super().__init__(
            discord.ui.Button(
                label="📋 Answer",
                style=discord.ButtonStyle.success,
                custom_id=f"survey_answer_button:{survey_id}",
            )
        )
        self.survey_id = survey_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["survey_id"])

    async def callback(self, interaction: discord.Interaction):
        await _start_survey_answer_flow(interaction, survey_id=self.survey_id)


def build_survey_button_view(survey_id: str = "default") -> discord.ui.View:
    """Return the right persistent view for a given survey id."""
    if survey_id == "default":
        return SurveyButtonView()
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicSurveyButton(survey_id))
    return view


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


# ── Survey selector helper (Premium multi-survey) ─────────────────────────────

class _SurveyPickView(discord.ui.View):
    """Internal: dropdown for picking which survey to act on."""

    def __init__(self, surveys: list[dict]):
        super().__init__(timeout=120)
        self.selected_id: str | None = None

        options = []
        for s in surveys[:25]:
            label = s.get("survey_name") or s.get("survey_id") or "?"
            sid   = s.get("survey_id") or "default"
            desc  = ", ".join(
                q.get("label", "") for q in (s.get("questions") or [])[:3]
            )
            options.append(discord.SelectOption(
                label=label[:100],
                value=sid[:100],
                description=(desc[:100] if desc else None),
            ))

        sel = discord.ui.Select(placeholder="Pick a survey…", options=options)

        async def _cb(inter: discord.Interaction):
            self.selected_id = sel.values[0]
            sel.disabled = True
            picked = next((s for s in surveys if (s.get("survey_id") or "default") == self.selected_id), None)
            label  = picked.get("survey_name", self.selected_id) if picked else self.selected_id
            await inter.response.edit_message(content=f"✅ Survey: **{label}**", view=self)
            self.stop()

        sel.callback = _cb
        self.add_item(sel)


async def _pick_survey(interaction: discord.Interaction, *, prompt: str) -> dict | None:
    """
    For premium guilds with more than one configured survey, prompt the
    caller to pick one. Returns the chosen survey dict (always at least the
    default). Returns `None` only if the picker timed out.
    """
    from config import list_surveys
    import premium as _prem

    surveys = list_surveys(interaction.guild_id)

    if not await _prem.is_premium(interaction.guild_id, bot=interaction.client) or len(surveys) <= 1:
        return surveys[0]  # default-only path

    view = _SurveyPickView(surveys)
    await interaction.followup.send(prompt, view=view, ephemeral=True)
    await view.wait()
    if view.selected_id is None:
        return None
    return next(
        (s for s in surveys if (s.get("survey_id") or "default") == view.selected_id),
        surveys[0],
    )


# ── Multi-survey manage view (Premium /survey UX) ─────────────────────────────

class _SurveyManageView(discord.ui.View):
    """
    The button row shown beneath `/survey`'s list view for premium guilds.
    Replaces the old `/setup_survey_extra` and `/remove_survey` slash
    commands — Add / Edit / Remove all live here so leadership has one
    surface for survey management.
    """

    def __init__(self, has_extras: bool):
        super().__init__(timeout=180)
        # Disable Remove when there are no extras, since the default
        # survey can't be removed via this flow.
        for item in self.children:
            if getattr(item, "label", "") == "🗑️ Remove Survey":
                item.disabled = not has_extras

    @discord.ui.button(label="➕ Add Survey", style=discord.ButtonStyle.success)
    async def add_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        from setup_cog import run_create_new_extra_survey
        for item in self.children: item.disabled = True
        await inter.response.edit_message(view=self)
        await run_create_new_extra_survey(inter, inter.client)
        self.stop()

    @discord.ui.button(label="✏️ Edit Survey", style=discord.ButtonStyle.primary)
    async def edit_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        from setup_cog import run_pick_survey_to_edit
        for item in self.children: item.disabled = True
        await inter.response.edit_message(view=self)
        await run_pick_survey_to_edit(inter, inter.client)
        self.stop()

    @discord.ui.button(label="🗑️ Remove Survey", style=discord.ButtonStyle.danger)
    async def remove_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        from setup_cog import run_remove_extra_survey
        for item in self.children: item.disabled = True
        await inter.response.edit_message(view=self)
        await run_remove_extra_survey(inter, inter.client)
        self.stop()


async def _send_survey_manage_view(interaction: discord.Interaction, bot):
    """
    Render the premium /survey list view with Add / Edit / Remove buttons.
    Called from the /survey command after the premium check passes.
    Assumes the caller has already deferred ephemerally.
    """
    from config import list_surveys

    surveys    = list_surveys(interaction.guild_id)
    has_extras = any((s.get("survey_id") or "default") != "default" for s in surveys)

    embed = discord.Embed(
        title="📋 Configured Surveys",
        color=discord.Color.blurple(),
        description=(
            "💎 **Premium** — manage every survey from here.\n"
            "Use the buttons below to **Add**, **Edit**, or **Remove** a survey."
        ),
    )
    for s in surveys[:25]:
        sid    = s.get("survey_id") or "default"
        name   = s.get("survey_name") or sid
        n_q    = len(s.get("questions") or [])
        tab    = s.get("tab_squad_powers") or "*not set*"
        ch_id  = int(s.get("survey_channel_id") or 0)
        ch_str = f"<#{ch_id}>" if ch_id else "_(uses default channel)_"
        embed.add_field(
            name=f"{name}" + (" *(default)*" if sid == "default" else ""),
            value=f"**{n_q}** question(s) · Stats tab: `{tab}` · Channel: {ch_str}",
            inline=False,
        )
    embed.set_footer(text="Use /survey_post to publish the answer button. /survey_remind to send or schedule reminders.")

    view = _SurveyManageView(has_extras=has_extras)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ── Cog ────────────────────────────────────────────────────────────────────────

class SurveyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Re-register the persistent view (default survey button) and the
        # dynamic-item handler (extra survey buttons) so both keep working
        # across bot restarts.
        self.bot.add_view(SurveyButtonView())
        try:
            self.bot.add_dynamic_items(DynamicSurveyButton)
        except AttributeError:
            # discord.py older than 2.4 — dynamic items unsupported. The
            # default survey button will still work; extras will not survive
            # restarts on this version. Surfacing this in logs lets us notice
            # when a deploy needs an upgrade.
            print("[SURVEY] discord.py too old for DynamicItem — extra-survey "
                  "buttons will not be persistent on this version.")
        # Start the per-minute scheduler tick that fires scheduled reminders
        # (#27). Stamps `reminder_last_fired` on each survey row so we don't
        # double-fire on a restart in the same minute.
        self.check_scheduled_reminders.start()

    def cog_unload(self):
        try:
            self.check_scheduled_reminders.cancel()
        except Exception:
            pass

    @tasks.loop(minutes=1)
    async def check_scheduled_reminders(self):
        """
        Walk every guild's scheduled survey reminders. Fire the ones whose
        frequency, day-of-week, and time match `now` in the guild's timezone.
        DM-via-roster reminders silently no-op for non-Premium guilds.
        """
        from zoneinfo import ZoneInfo
        from config import (
            list_scheduled_survey_reminders,
            update_survey_reminder_last_fired,
            get_config as _get_config,
        )
        import premium as _prem

        try:
            scheduled = list_scheduled_survey_reminders()
        except Exception as e:
            print(f"[SURVEY] Error listing scheduled reminders: {e}")
            return

        for entry in scheduled:
            try:
                guild_id   = int(entry["guild_id"])
                survey_id  = entry.get("survey_id") or "default"
                frequency  = (entry.get("reminder_frequency") or "off").lower()
                if frequency == "off":
                    continue

                cfg = _get_config(guild_id)
                if not cfg or not cfg.setup_complete:
                    continue

                tz_str    = cfg.timezone or "America/New_York"
                guild_tz  = ZoneInfo(tz_str)
                guild_now = datetime.now(tz=guild_tz)

                # Time-of-day match (HH:MM, minute granularity)
                time_str = entry.get("reminder_time") or "12:00"
                try:
                    r_h, r_m = int(time_str.split(":")[0]), int(time_str.split(":")[1])
                except Exception:
                    continue
                if guild_now.hour != r_h or guild_now.minute != r_m:
                    continue

                # Day-of-week match for weekly schedules. Python: Monday=0
                if frequency == "weekly":
                    target_day = int(entry.get("reminder_day_of_week") or 1)
                    if guild_now.weekday() != target_day:
                        continue

                # Idempotency — don't fire twice for the same date
                today_iso  = guild_now.date().isoformat()
                last_fired = entry.get("reminder_last_fired") or ""
                if last_fired == today_iso:
                    continue

                # Resolve the survey config (so we can format the reminder body)
                from config import get_survey
                survey = get_survey(guild_id, survey_id) or {
                    "survey_name":      entry.get("survey_name") or "Default",
                    "reminder_message": entry.get("reminder_message") or "",
                }
                # Refresh body with the latest custom message + sensible default
                body = (survey.get("reminder_message")
                        or entry.get("reminder_message")
                        or _default_reminder_body(survey))

                use_dm     = bool(entry.get("reminder_use_dm"))
                channel_id = int(entry.get("reminder_channel_id") or 0)

                if use_dm:
                    # DM path is Premium-only because it depends on Member
                    # Roster Sync. Silently skip when the guild lapses.
                    if not await _prem.is_premium(guild_id, bot=self.bot):
                        print(f"[SURVEY] Skipping DM reminder for guild {guild_id}: Premium lapsed.")
                        continue
                    sent, skipped = await _send_reminder_via_dm(self.bot, guild_id, body)
                    print(f"[SURVEY] Scheduled DM reminder fired for guild={guild_id} "
                          f"survey={survey_id} sent={sent} skipped={skipped}")
                elif channel_id:
                    ok = await _send_reminder_to_channel(self.bot, guild_id, channel_id, body)
                    if not ok:
                        print(f"[SURVEY] Channel reminder failed for guild={guild_id} "
                              f"survey={survey_id} channel={channel_id}")
                        continue
                    print(f"[SURVEY] Scheduled channel reminder fired for guild={guild_id} "
                          f"survey={survey_id} channel={channel_id}")
                else:
                    # No destination configured — schedule is incomplete; skip.
                    continue

                update_survey_reminder_last_fired(guild_id, survey_id, today_iso)

            except Exception as e:
                print(f"[SURVEY] Error firing scheduled reminder: {e}")

    @check_scheduled_reminders.before_loop
    async def _before_check_scheduled(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="survey_post",
        description="Post (or repost) the survey button in its configured channel",
    )
    async def survey_post(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        from config import get_config
        cfg = get_config(interaction.guild_id)
        if not cfg:
            await interaction.followup.send("⚙️ Bot not configured. Run `/setup` first.", ephemeral=True)
            return

        # Premium guilds with multiple surveys pick which one to post.
        survey = await _pick_survey(
            interaction,
            prompt="📋 You have multiple surveys configured — which one do you want to post?",
        )
        if survey is None:
            await interaction.followup.send("⏰ Picker timed out. Run `/survey_post` again.", ephemeral=True)
            return

        survey_id = survey.get("survey_id") or "default"
        channel_id = (
            int(survey.get("survey_channel_id") or 0) or cfg.survey_channel_id
        )
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            await interaction.followup.send(
                f"⚠️ Could not find the survey channel for **{survey.get('survey_name', 'this survey')}**.",
                ephemeral=True,
            )
            return

        intro = survey.get("intro_message") or (
            "**Let us know your Squad Powers!**\n\n"
            "Please fill out this survey each week, if possible, to help us keep track of "
            "squad powers, better balance our Desert Storm teams, track alliance growth, "
            "and prepare for season events!"
        )

        view = build_survey_button_view(survey_id)
        await channel.send(intro, view=view)
        await interaction.followup.send(
            f"✅ Survey button posted for **{survey.get('survey_name', 'Default')}** in {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="survey",
        description="Show configured survey(s); Premium gets Add / Edit / Remove buttons here",
    )
    async def survey(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        from config import list_surveys, get_survey_config
        import premium as _prem

        is_premium_flag = await _prem.is_premium(
            interaction.guild_id, interaction=interaction, bot=self.bot,
        )

        # Premium tier: always show the list view + Add / Edit / Remove
        # buttons, regardless of how many surveys are configured. This is
        # the consolidated multi-survey UX — one command, three buttons.
        if is_premium_flag:
            await interaction.response.defer(ephemeral=True)
            await _send_survey_manage_view(interaction, self.bot)
            return

        # Free tier: single-survey detail view (matches prior behavior).
        scfg      = get_survey_config(interaction.guild_id)
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

    @app_commands.command(
        name="survey_remind",
        description="Send a survey reminder now or manage scheduled reminders",
    )
    async def survey_remind(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        await _run_remind_hub(interaction, self.bot)


# ── Reminder helpers ──────────────────────────────────────────────────────────

def _default_reminder_body(survey: dict) -> str:
    """Fallback reminder message when the survey doesn't have one saved."""
    name = survey.get("survey_name") or "the survey"
    return (
        f"📋 **Friendly reminder** — your alliance is asking you to fill out "
        f"**{name}** this week. Open the survey channel in Discord and click "
        f"the **📋 Answer** button to get started. Thanks!"
    )


async def _send_reminder_to_channel(bot, guild_id: int, channel_id: int, body: str) -> bool:
    """Post a reminder body to a guild channel. Returns True on success."""
    channel = bot.get_channel(channel_id)
    if channel is None:
        return False
    try:
        await channel.send(body)
        return True
    except Exception as e:
        print(f"[REMINDER] Channel post failed (guild={guild_id}, channel={channel_id}): {e}")
        return False


async def _send_reminder_via_dm(bot, guild_id: int, body: str) -> tuple[int, int]:
    """
    DM every member listed in the guild's Member Roster sheet. Returns
    (sent, skipped). Premium-gating happens at the call site — this helper
    just does the work.
    """
    import dm
    from config import get_member_roster_config, get_member_roster_sheet

    roster_cfg = get_member_roster_config(guild_id)
    if not roster_cfg.get("enabled"):
        return (0, 0)

    try:
        ws   = get_member_roster_sheet(guild_id, roster_cfg["tab_name"])
        rows = await asyncio.get_event_loop().run_in_executor(None, ws.get_all_values)
    except Exception as e:
        print(f"[REMINDER] Could not read roster for guild {guild_id}: {e}")
        return (0, 0)

    did_col = roster_cfg["discord_id_col"]
    sent    = 0
    skipped = 0
    for row in rows[1:]:  # skip header
        if did_col >= len(row):
            continue
        did = row[did_col].strip()
        if not did:
            skipped += 1
            continue
        ok = await dm.send_dm_to_id(bot, guild_id, did, content=body)
        if ok:
            sent += 1
        else:
            skipped += 1
    return (sent, skipped)


# ── Wizard hub ────────────────────────────────────────────────────────────────

class _ReminderHubView(discord.ui.View):
    """Top-level picker shown by /survey_remind."""

    def __init__(self):
        super().__init__(timeout=120)
        self.choice: str | None = None  # "send" | "schedule" | None

    @discord.ui.button(label="📤 Send reminder now", style=discord.ButtonStyle.success)
    async def send_now(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "send"
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="⚙️ Manage scheduled reminders", style=discord.ButtonStyle.primary)
    async def manage(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "schedule"
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = None
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(content="Cancelled.", view=self)
        self.stop()


async def _run_remind_hub(interaction: discord.Interaction, bot):
    import premium as _prem

    is_premium_flag = await _prem.is_premium(
        interaction.guild_id, interaction=interaction, bot=bot,
    )

    view = _ReminderHubView()
    await interaction.response.send_message(
        "📋 **Survey Reminders**\n"
        "What would you like to do?\n"
        f"*Tier: {'💎 Premium' if is_premium_flag else 'Free'}*",
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if view.choice == "send":
        await _run_send_now(interaction, bot, is_premium_flag)
    elif view.choice == "schedule":
        await _run_schedule_wizard(interaction, bot, is_premium_flag)


# ── Send-now path ─────────────────────────────────────────────────────────────

class _DestinationPickView(discord.ui.View):
    """Channel vs DM picker. DM option only enabled for Premium guilds."""

    def __init__(self, allow_dm: bool):
        super().__init__(timeout=120)
        self.choice: str | None = None  # "channel" | "dm" | None

        ch_btn = discord.ui.Button(
            label="📢 Post to a channel",
            style=discord.ButtonStyle.primary,
        )
        async def _ch(inter: discord.Interaction):
            self.choice = "channel"
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(view=self)
            self.stop()
        ch_btn.callback = _ch
        self.add_item(ch_btn)

        dm_btn = discord.ui.Button(
            label="📨 DM via Member Roster" + ("" if allow_dm else " (💎 Premium)"),
            style=discord.ButtonStyle.secondary,
            disabled=not allow_dm,
        )
        async def _dm(inter: discord.Interaction):
            self.choice = "dm"
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(view=self)
            self.stop()
        dm_btn.callback = _dm
        self.add_item(dm_btn)


class _ChannelPickView(discord.ui.View):
    """Single-channel picker for the send-now flow."""

    def __init__(self):
        super().__init__(timeout=120)
        self.channel: discord.abc.GuildChannel | None = None

        sel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Pick a channel…",
        )
        async def _cb(inter: discord.Interaction):
            self.channel = sel.values[0].resolve() or sel.values[0]
            sel.disabled = True
            await inter.response.edit_message(
                content=f"✅ Channel: {self.channel.mention if hasattr(self.channel, 'mention') else self.channel}",
                view=self,
            )
            self.stop()
        sel.callback = _cb
        self.add_item(sel)


async def _run_send_now(interaction: discord.Interaction, bot, is_premium_flag: bool):
    # Premium with multiple surveys gets a survey selector; otherwise it's
    # the only configured survey.
    survey = await _pick_survey(
        interaction,
        prompt="📋 You have multiple surveys — which one are you reminding members about?",
    )
    if survey is None:
        await interaction.followup.send(
            "⏰ Picker timed out. Run `/survey_remind` again.", ephemeral=True,
        )
        return

    body = survey.get("reminder_message") or _default_reminder_body(survey)

    dest_view = _DestinationPickView(allow_dm=is_premium_flag)
    await interaction.followup.send(
        f"📋 Reminder for **{survey.get('survey_name', 'Default')}** — where should it go?\n"
        f"{'' if is_premium_flag else 'ℹ️ *DM-via-roster is Premium-only — `/upgrade` to unlock.*'}",
        view=dest_view,
        ephemeral=True,
    )
    await dest_view.wait()
    if dest_view.choice is None:
        await interaction.followup.send("⏰ Timed out. Run `/survey_remind` again.", ephemeral=True)
        return

    if dest_view.choice == "channel":
        ch_view = _ChannelPickView()
        await interaction.followup.send("📢 Pick the channel to post to:", view=ch_view, ephemeral=True)
        await ch_view.wait()
        if ch_view.channel is None:
            await interaction.followup.send("⏰ Timed out. Run `/survey_remind` again.", ephemeral=True)
            return
        ok = await _send_reminder_to_channel(bot, interaction.guild_id, ch_view.channel.id, body)
        if ok:
            await interaction.followup.send(
                f"✅ Posted reminder for **{survey.get('survey_name', 'Default')}** in "
                f"{ch_view.channel.mention if hasattr(ch_view.channel, 'mention') else '#?'}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "⚠️ Could not post to that channel — make sure the bot has permission.",
                ephemeral=True,
            )
        return

    # dest_view.choice == "dm" (Premium only)
    from config import get_member_roster_config
    roster_cfg = get_member_roster_config(interaction.guild_id)
    if not roster_cfg.get("enabled"):
        await interaction.followup.send(
            "⚙️ DM reminders need Member Roster Sync. Run `/setup_members` first.",
            ephemeral=True,
        )
        return
    sent, skipped = await _send_reminder_via_dm(bot, interaction.guild_id, body)
    await interaction.followup.send(
        f"✅ Sent {sent} reminder DM{'s' if sent != 1 else ''} for "
        f"**{survey.get('survey_name', 'Default')}**. "
        f"{skipped} skipped (DMs closed, missing ID, or other failures).",
        ephemeral=True,
    )


# ── Schedule-management path ──────────────────────────────────────────────────

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class _FrequencyPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.choice: str | None = None

    @discord.ui.button(label="Off (disable)", style=discord.ButtonStyle.danger)
    async def off(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "off"
        for item in self.children: item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Daily", style=discord.ButtonStyle.primary)
    async def daily(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "daily"
        for item in self.children: item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Weekly", style=discord.ButtonStyle.success)
    async def weekly(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "weekly"
        for item in self.children: item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


class _DayPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.day: int | None = None
        sel = discord.ui.Select(
            placeholder="Day of the week…",
            options=[discord.SelectOption(label=name, value=str(i)) for i, name in enumerate(DAYS_OF_WEEK)],
        )
        async def _cb(inter: discord.Interaction):
            self.day = int(sel.values[0])
            sel.disabled = True
            await inter.response.edit_message(content=f"✅ Day: **{DAYS_OF_WEEK[self.day]}**", view=self)
            self.stop()
        sel.callback = _cb
        self.add_item(sel)


async def _run_schedule_wizard(interaction: discord.Interaction, bot, is_premium_flag: bool):
    """Walk leadership through configuring a survey's scheduled reminder."""
    from config import save_survey_reminder, _parse_12h_time as _parse_time_helper  # type: ignore
    # Pick which survey
    survey = await _pick_survey(
        interaction,
        prompt="⚙️ Which survey are you scheduling reminders for?",
    )
    if survey is None:
        await interaction.followup.send(
            "⏰ Picker timed out. Run `/survey_remind` again.", ephemeral=True,
        )
        return

    survey_id   = survey.get("survey_id") or "default"
    survey_name = survey.get("survey_name") or "Default"

    # Show current settings as context
    cur_freq    = survey.get("reminder_frequency") or "off"
    cur_day     = int(survey.get("reminder_day_of_week") or 1)
    cur_time    = survey.get("reminder_time") or "12:00"
    cur_ch      = int(survey.get("reminder_channel_id") or 0)
    cur_use_dm  = bool(survey.get("reminder_use_dm"))
    cur_msg     = survey.get("reminder_message") or ""

    cur_dest = (
        "DM via Member Roster" if cur_use_dm
        else (f"<#{cur_ch}>" if cur_ch else "*(not set)*")
    )
    cur_when = (
        "Off" if cur_freq == "off"
        else f"Daily at {cur_time}" if cur_freq == "daily"
        else f"Weekly on {DAYS_OF_WEEK[cur_day]} at {cur_time}"
    )

    await interaction.followup.send(
        f"⚙️ **Scheduling reminders for `{survey_name}`**\n"
        f"**Current schedule:** {cur_when}\n"
        f"**Current destination:** {cur_dest}\n"
        f"**Current message:** {('*set*' if cur_msg else '*default*')}",
        ephemeral=True,
    )

    # ── Step 1: Frequency ─────────────────────────────────────────────────────
    freq_view = _FrequencyPickView()
    await interaction.followup.send(
        "**Step 1 — Frequency**\nHow often should this reminder fire?",
        view=freq_view,
        ephemeral=True,
    )
    await freq_view.wait()
    if freq_view.choice is None:
        await interaction.followup.send("⏰ Timed out. Run `/survey_remind` again.", ephemeral=True)
        return

    new_freq = freq_view.choice
    if new_freq == "off":
        save_survey_reminder(
            interaction.guild_id, survey_id,
            enabled=0, frequency="off",
            day_of_week=cur_day, time_str=cur_time,
            channel_id=cur_ch, use_dm=int(cur_use_dm),
            message=cur_msg,
        )
        await interaction.followup.send(
            f"✅ Scheduled reminders disabled for **{survey_name}**. "
            f"Run `/survey_remind` again to re-enable.",
            ephemeral=True,
        )
        return

    # ── Step 2: Day-of-week (weekly only) ─────────────────────────────────────
    new_day = cur_day
    if new_freq == "weekly":
        day_view = _DayPickView()
        await interaction.followup.send(
            "**Step 2 — Day of the week**\nWhich day should the reminder fire each week?",
            view=day_view,
            ephemeral=True,
        )
        await day_view.wait()
        if day_view.day is None:
            await interaction.followup.send("⏰ Timed out. Run `/survey_remind` again.", ephemeral=True)
            return
        new_day = day_view.day

    # ── Step 3: Time of day ───────────────────────────────────────────────────
    new_time, ok = await _ask_time(interaction, default=cur_time, step_label="Step 3 — Time of day")
    if not ok:
        return

    # ── Step 4: Destination ───────────────────────────────────────────────────
    dest_view = _DestinationPickView(allow_dm=is_premium_flag)
    await interaction.followup.send(
        f"**Step 4 — Where to send the reminder**\n"
        f"{'' if is_premium_flag else 'ℹ️ *DM-via-roster is Premium-only.*'}",
        view=dest_view,
        ephemeral=True,
    )
    await dest_view.wait()
    if dest_view.choice is None:
        await interaction.followup.send("⏰ Timed out. Run `/survey_remind` again.", ephemeral=True)
        return

    new_use_dm   = 0
    new_channel  = 0
    if dest_view.choice == "dm":
        new_use_dm = 1
    else:
        ch_view = _ChannelPickView()
        await interaction.followup.send("📢 Pick the channel to post the reminder to:", view=ch_view, ephemeral=True)
        await ch_view.wait()
        if ch_view.channel is None:
            await interaction.followup.send("⏰ Timed out. Run `/survey_remind` again.", ephemeral=True)
            return
        new_channel = ch_view.channel.id

    # ── Step 5: Message body ──────────────────────────────────────────────────
    new_msg, ok = await _ask_reminder_message(interaction, bot, default=cur_msg)
    if not ok:
        return

    # ── Save ──────────────────────────────────────────────────────────────────
    save_survey_reminder(
        interaction.guild_id, survey_id,
        enabled=1,
        frequency=new_freq,
        day_of_week=new_day,
        time_str=new_time,
        channel_id=new_channel,
        use_dm=new_use_dm,
        message=new_msg,
    )

    when = (
        f"Daily at {new_time}" if new_freq == "daily"
        else f"Weekly on {DAYS_OF_WEEK[new_day]} at {new_time}"
    )
    where = "DMs to every roster member" if new_use_dm else f"<#{new_channel}>"
    await interaction.followup.send(
        f"✅ **{survey_name} reminders scheduled.**\n"
        f"**When:** {when} *(in your guild's timezone)*\n"
        f"**Where:** {where}\n"
        f"**Message:** {('*custom*' if new_msg else '*default*')}\n\n"
        f"Run `/survey_remind` again any time to update or disable.",
        ephemeral=True,
    )


async def _ask_time(interaction: discord.Interaction, *, default: str,
                    step_label: str) -> tuple[str, bool]:
    """
    Ask leadership for a HH:MM time via a one-field modal. Re-prompts up to
    3 times on unparseable input. Returns (time_str_24h, ok).
    """
    from setup_cog import _parse_12h_time

    class _TimeModal(discord.ui.Modal, title="Reminder time"):
        time_in = discord.ui.TextInput(
            label="Time (e.g. 9:00am, 22:30, 12:00pm)",
            default=default,
            max_length=8,
            required=True,
        )
        def __init__(self):
            super().__init__()
            self.value: str | None = None
        async def on_submit(self, inter: discord.Interaction):
            self.value = str(self.time_in.value).strip()
            await inter.response.defer(ephemeral=True)
            self.stop()

    class _TimeView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.modal: _TimeModal | None = None
        @discord.ui.button(label=f"⏰ Set time (current: {default})", style=discord.ButtonStyle.primary)
        async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
            self.modal = _TimeModal()
            await inter.response.send_modal(self.modal)
            await self.modal.wait()
            self.stop()

    attempts_left = 3
    while True:
        view = _TimeView()
        await interaction.followup.send(
            f"**{step_label}**\nWhat time should the reminder fire? *(your guild's timezone)*",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.modal is None or view.modal.value is None:
            await interaction.followup.send("⏰ Timed out. Run `/survey_remind` again.", ephemeral=True)
            return ("", False)
        raw = view.modal.value
        parsed = _parse_12h_time(raw)
        if parsed:
            return (parsed, True)
        if (len(raw) == 5 and raw[2] == ":" and raw.replace(":", "").isdigit()):
            return (raw, True)
        attempts_left -= 1
        if attempts_left <= 0:
            await interaction.followup.send(
                "⚠️ Could not read that time after a few tries. "
                "Run `/survey_remind` to start over.",
                ephemeral=True,
            )
            return ("", False)
        await interaction.followup.send(
            f"⚠️ Could not read **`{raw}`** as a time. "
            f"Try `9:00am`, `22:30`, or `12:00pm`. Let's try once more.",
            ephemeral=True,
        )


async def _ask_reminder_message(interaction: discord.Interaction, bot,
                                 *, default: str) -> tuple[str, bool]:
    """
    Prompt for the reminder message body. Empty input keeps the existing
    custom message, or falls back to the generic default at fire time.
    Returns (body, ok).
    """

    class _MsgModal(discord.ui.Modal, title="Reminder message"):
        body_in = discord.ui.TextInput(
            label="Reminder message body",
            style=discord.TextStyle.paragraph,
            default=default[:4000] if default else "",
            placeholder=(
                "📋 Reminder — please fill out the survey this week!\n"
                "(Leave blank to use the bot's default message.)"
            ),
            required=False,
            max_length=2000,
        )
        def __init__(self):
            super().__init__()
            self.value: str | None = None
            self.confirmed = False
        async def on_submit(self, inter: discord.Interaction):
            self.value     = str(self.body_in.value)
            self.confirmed = True
            await inter.response.defer(ephemeral=True)
            self.stop()

    class _MsgView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            self.modal: _MsgModal | None = None
        @discord.ui.button(label="✏️ Edit message", style=discord.ButtonStyle.primary)
        async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
            self.modal = _MsgModal()
            await inter.response.send_modal(self.modal)
            await self.modal.wait()
            self.stop()
        @discord.ui.button(label="Use default", style=discord.ButtonStyle.secondary)
        async def use_default(self, inter: discord.Interaction, button: discord.ui.Button):
            self.modal = _MsgModal()
            self.modal.value = ""
            self.modal.confirmed = True
            await inter.response.edit_message(content="✅ Will use the default reminder message.", view=self)
            self.stop()

    view = _MsgView()
    await interaction.followup.send(
        "**Step 5 — Reminder message**\n"
        "What should the reminder say? Leave blank to use the bot's default.",
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if view.modal is None or not view.modal.confirmed:
        await interaction.followup.send("⏰ Timed out. Run `/survey_remind` again.", ephemeral=True)
        return ("", False)
    return ((view.modal.value or "").strip(), True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SurveyCog(bot))

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
  - 15 minutes after reset (10:30pm ET every day), if no prompt has been
    retrieved for today's person, the bot pings leadership with a button
    to pull up the stored details and confirm posting the prompt.
"""

import asyncio
import json
import os
import re
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

LEADERSHIP_CHANNEL_ID = 1488693874938482799
REQUIRED_ROLE_NAME    = "OGV Leadership"

WIZARD_TIMEOUT = 300  # seconds

# Tracks active wizard sessions: { user_id: asyncio.Event }
active_wizards: dict[int, asyncio.Event] = {}

# Persistence files
SCHEDULE_FILE = "train_schedule.json"
BLURB_LOG_FILE = "train_blurb_log.json"

# ── Schedule data structure ────────────────────────────────────────────────────
#
# Schedule is stored as:
# {
#   "YYYY-MM-DD": {
#     "name": "PlayerName",
#     "theme": "Birthday",          # optional
#     "tone": "More casual",        # optional
#     "notes": "queen energy..."    # optional
#   }
# }

def load_schedule() -> dict:
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, "r") as f:
            data = json.load(f)
            # Migrate old format { "YYYY-MM-DD": "Name" } to new format
            migrated = {}
            for k, v in data.items():
                if isinstance(v, str):
                    migrated[k] = {"name": v}
                else:
                    migrated[k] = v
            return migrated
    return {}


def save_schedule(schedule: dict):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f, indent=2)


def load_blurb_log() -> set:
    if os.path.exists(BLURB_LOG_FILE):
        with open(BLURB_LOG_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_blurb_log(log: set):
    with open(BLURB_LOG_FILE, "w") as f:
        json.dump(list(log), f, indent=2)


def mark_blurb_generated(date_str: str):
    log = load_blurb_log()
    log.add(date_str)
    save_blurb_log(log)


def blurb_generated_today() -> bool:
    return date.today().isoformat() in load_blurb_log()


# ── Schedule date parsing ──────────────────────────────────────────────────────

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_date_and_name(line: str) -> tuple[date, str] | tuple[None, None]:
    """Parse a single 'Date - Name' line. Returns (date, name) or (None, None)."""
    current_year = datetime.now(tz=ET).year

    # Numeric: 4/1 - Name or 4/1/2026 - Name
    numeric = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s*[-:]\s*(.+)$", line)
    if numeric:
        try:
            d = date(
                int(numeric.group(3)) if numeric.group(3) else current_year,
                int(numeric.group(1)),
                int(numeric.group(2)),
            )
            return d, numeric.group(4).strip()
        except ValueError:
            return None, None

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
                return d, named.group(3).strip()
            except ValueError:
                return None, None

    return None, None


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
            description="*No schedule set. Use `!schedule` to add entries.*",
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
            f"Use `!train` to build the prompt manually.",
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
            await channel.send("⏰ Timed out. Use `!schedule` to try again.")
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
    prompt_msg = await channel.send(
        f"📋 **Train Schedule Input** — {user.mention}\n\n"
        f"Paste your schedule below, one entry per line:\n"
        f"```\n"
        f"April 2 - PlayerName\n"
        f"April 5 - PlayerName\n"
        f"4/8 - PlayerName\n"
        f"```\n"
        f"This will **merge** with the current schedule (existing entries are kept).\n"
        f"*(Type `!cancel` at any time to stop)*"
    )

    raw = await wait_for_msg("")
    try:
        await prompt_msg.delete()
    except discord.HTTPException:
        pass
    if raw is None:
        return

    # Parse all lines
    parsed  = []
    errors  = []
    for line in raw.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        d, name = parse_date_and_name(line)
        if d and name:
            parsed.append((d, name))
        else:
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

    for i, (d, name) in enumerate(parsed):
        date_str       = d.isoformat()
        existing_entry = schedule.get(date_str, {})
        existing_name  = existing_entry.get("name", name) if isinstance(existing_entry, dict) else name

        skip_view = SkipOrFillView()
        skip_msg  = await channel.send(
            f"**Entry {i+1} of {len(parsed)}: {existing_name} — {d.strftime('%A, %B %-d')}**\n"
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
            # Save what we have so far and stop
            schedule[date_str] = {"name": existing_name}
            break

        if skip_view.action == "skip":
            schedule[date_str] = {"name": existing_name}
            continue

        # Fill in details for this person
        entry = {"name": existing_name}

        # Theme
        theme_msg  = await channel.send(f"🎯 **Theme** for {existing_name}:")
        theme_view = ThemeSelectView()
        await theme_msg.edit(view=theme_view)
        ok = await wait_for_view(theme_view, theme_msg)
        if not ok:
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
                    return
                theme = custom
            entry["theme"] = theme

        # Tone
        tone_msg  = await channel.send(f"🎭 **Tone** for {existing_name}:")
        tone_view = ToneSelectView()
        await tone_msg.edit(view=tone_view)
        ok = await wait_for_view(tone_view, tone_msg)
        if not ok:
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
            return
        if notes_raw.lower() != "skip":
            entry["notes"] = notes_raw

        schedule[date_str] = entry

        await channel.send(
            f"✅ Saved details for **{existing_name}**.",
            delete_after=5,
        )

    save_schedule(schedule)

    # Show errors if any
    error_text = ""
    if errors:
        error_text = f"\n\n⚠️ **Could not parse {len(errors)} line(s):**\n" + "\n".join(f"• {e}" for e in errors)

    # Post updated schedule embed
    blurb_log = load_blurb_log()
    embed = build_schedule_embed(schedule, blurb_log)
    await channel.send(
        f"📋 **Schedule saved!**{error_text}",
        embed=embed,
    )


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
            await channel.send("⏰ Wizard timed out. Use `!train` to start again.")
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
                f"*(Type `!cancel` at any time to stop)*"
            )
        else:
            await channel.send(
                f"🚂 **Train Blurb Wizard** — started by {user.mention}\n\n"
                f"**Step 1 of 4 — Member Name**\n"
                f"Type the member's name exactly as it should appear:\n"
                f"*(Type `!cancel` at any time to stop)*"
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
            await channel.send("⏰ Wizard timed out. Use `!train` to start again.")
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
            await channel.send("⏰ Wizard timed out. Use `!train` to start again.")
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
            await channel.send("❌ Cancelled. Use `!train` to start over.")
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


async def run_train_wizard(ctx: commands.Context):
    await run_wizard_steps(ctx.bot, ctx.channel, ctx.author)


async def run_train_wizard_prefilled(bot, channel, user, name: str):
    await run_wizard_steps(bot, channel, user, prefilled_name=name)


# ── Cog ────────────────────────────────────────────────────────────────────────

class TrainCog(commands.Cog):
    def __init__(self, bot):
        self.bot                 = bot
        self.reminder_sent_today = False
        self.last_reminder_date  = None
        self.check_reminder.start()

    def cog_unload(self):
        self.check_reminder.cancel()

    def _has_role(self, ctx_or_member) -> bool:
        if hasattr(ctx_or_member, "author"):
            roles = ctx_or_member.author.roles
        else:
            roles = ctx_or_member.roles
        return REQUIRED_ROLE_NAME in [r.name for r in roles]

    def _in_channel(self, ctx) -> bool:
        return ctx.channel.id == LEADERSHIP_CHANNEL_ID

    async def _guard(self, ctx) -> bool:
        """Delete command message and return False if checks fail."""
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        if not self._in_channel(ctx):
            return False
        if not self._has_role(ctx):
            await ctx.channel.send(
                f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to use this command.",
                delete_after=10,
            )
            return False
        return True

    # ── !cancel ────────────────────────────────────────────────────────────────

    @commands.command(name="cancel")
    async def cancel(self, ctx: commands.Context):
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        if not self._in_channel(ctx):
            return
        if ctx.author.id in active_wizards:
            active_wizards[ctx.author.id].set()
            await ctx.channel.send(f"❌ Wizard cancelled by {ctx.author.mention}.", delete_after=10)
        else:
            await ctx.channel.send("ℹ️ You don't have an active wizard running.", delete_after=10)

    # ── !train ─────────────────────────────────────────────────────────────────

    @commands.command(name="train")
    async def train(self, ctx: commands.Context):
        if not await self._guard(ctx):
            return
        await run_train_wizard(ctx)

    # ── !schedule ──────────────────────────────────────────────────────────────

    @commands.command(name="schedule")
    async def schedule(self, ctx: commands.Context, subcommand: str = None):
        if not await self._guard(ctx):
            return

        if subcommand and subcommand.lower() == "list":
            await self._post_schedule_embed(ctx.channel)
            return

        if subcommand and subcommand.lower() == "clear":
            save_schedule({})
            await ctx.channel.send("🗑️ Train schedule cleared.")
            return

        # Input mode — register cancel event
        cancel_event = asyncio.Event()
        active_wizards[ctx.author.id] = cancel_event
        try:
            await collect_schedule(self.bot, ctx, cancel_event)
        finally:
            active_wizards.pop(ctx.author.id, None)

    # ── !trainschedule ─────────────────────────────────────────────────────────

    @commands.command(name="trainschedule")
    async def trainschedule(self, ctx: commands.Context):
        if not await self._guard(ctx):
            return
        await self._post_schedule_embed(ctx.channel)

    async def _post_schedule_embed(self, channel):
        schedule  = load_schedule()
        blurb_log = load_blurb_log()
        embed     = build_schedule_embed(schedule, blurb_log)
        await channel.send(embed=embed)

    # ── !trainprompt ───────────────────────────────────────────────────────────

    @commands.command(name="trainprompt")
    async def trainprompt(self, ctx: commands.Context, *, date_arg: str = None):
        """
        Retrieve the stored prompt for a scheduled person.
        Defaults to today. Pass a date to retrieve a specific entry:
          !trainprompt April 5
          !trainprompt 4/5
        """
        if not await self._guard(ctx):
            return

        schedule  = load_schedule()
        blurb_log = load_blurb_log()

        if date_arg:
            d, _ = parse_date_and_name(f"{date_arg} - placeholder")
            if d is None:
                await ctx.channel.send(
                    f"⚠️ Could not parse date `{date_arg}`. Try formats like `April 5` or `4/5`."
                )
                return
            date_str = d.isoformat()
        else:
            date_str = date.today().isoformat()

        if date_str not in schedule:
            d_label = date.fromisoformat(date_str).strftime("%A, %B %-d")
            await ctx.channel.send(f"ℹ️ Nothing scheduled for **{d_label}**.")
            return

        entry = schedule[date_str]
        await retrieve_and_confirm(self.bot, ctx.channel, ctx.author, date_str, entry, blurb_log)

    # ── Reminder loop ──────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_reminder(self):
        now   = datetime.now(tz=ET)
        today = date.today()

        if self.last_reminder_date != today:
            self.last_reminder_date  = today
            self.reminder_sent_today = False

        # Train reminder always at 10:30pm ET
        if now.hour != 22 or now.minute != 30:
            return
        if self.reminder_sent_today:
            return

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
            f"⏰ **Train prompt reminder!**\n\n"
            f"Today's train is for **{name}** and the prompt hasn't been retrieved yet. "
            f"Click below to view stored details and get the ChatGPT prompt.",
            view=view,
        )
        self.reminder_sent_today = True
        print(f"[TRAIN] Reminder sent for {name} on {today_str}")

    @check_reminder.before_loop
    async def before_check_reminder(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(TrainCog(bot))

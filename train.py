"""
train.py — Train schedule blurb generator + schedule management

Commands (OGV Leadership only, leadership channel only):
  !train              — Launch the blurb wizard (manual, any name)
  !cancel             — Cancel any active wizard session
  !schedule           — Input/replace the upcoming train schedule
  !schedule list      — View the current schedule
  !schedule clear     — Clear the entire schedule
  !trainschedule      — Shortcut to view the current schedule

Schedule reminder:
  - 15 minutes after reset each day, if a blurb hasn't been generated
    for today's scheduled person, a reminder is posted in the leadership
    channel with a button to launch the wizard with the name pre-filled.
  - Reminder always fires at 10:30pm ET regardless of day.
"""

import asyncio
import json
import os
import re
import aiohttp
import discord
from discord.ext import commands, tasks
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

LEADERSHIP_CHANNEL_ID = 1488693874938482799
REQUIRED_ROLE_NAME    = "OGV Leadership"

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL   = "claude-sonnet-4-20250514"

WIZARD_TIMEOUT = 300  # seconds

# Tracks active wizard sessions: { user_id: asyncio.Event }
# Setting the event signals the wizard to cancel cleanly.
active_wizards: dict[int, asyncio.Event] = {}

# File to persist the schedule and generated blurb log across restarts
SCHEDULE_FILE    = "train_schedule.json"
BLURB_LOG_FILE   = "train_blurb_log.json"

# ── Persistence helpers ────────────────────────────────────────────────────────

def load_schedule() -> dict:
    """
    Returns a dict of { "YYYY-MM-DD": "Member Name" }
    """
    if os.path.exists(SCHEDULE_FILE):
        with open(SCHEDULE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_schedule(schedule: dict):
    with open(SCHEDULE_FILE, "w") as f:
        json.dump(schedule, f, indent=2)


def load_blurb_log() -> set:
    """
    Returns a set of date strings "YYYY-MM-DD" for which a blurb
    has been generated and approved today.
    """
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
    today = date.today().isoformat()
    return today in load_blurb_log()


# ── Schedule parsing ───────────────────────────────────────────────────────────

def parse_schedule_input(text: str) -> dict[str, str]:
    """
    Parse a multi-line schedule input into { "YYYY-MM-DD": "Name" }.

    Accepts flexible formats:
      April 1 - PlayerName
      4/1 - PlayerName
      4/1/2026 - PlayerName
      April 1: PlayerName
      April 1 PlayerName       (space separated)
    """
    current_year = datetime.now(tz=ET).year
    schedule = {}
    errors   = []

    month_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        # Try numeric format: 4/1, 4/1/2026
        numeric = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\s*[-:]\s*(.+)$", line)
        if numeric:
            month = int(numeric.group(1))
            day   = int(numeric.group(2))
            year  = int(numeric.group(3)) if numeric.group(3) else current_year
            name  = numeric.group(4).strip()
            try:
                d = date(year, month, day)
                schedule[d.isoformat()] = name
            except ValueError:
                errors.append(f"Invalid date: {line}")
            continue

        # Try month-name format: April 1 - Name / April 1: Name / April 1 Name
        named = re.match(
            r"^([A-Za-z]+)\s+(\d{1,2})(?:\s*(?:st|nd|rd|th))?\s*[-:]\s*(.+)$",
            line, re.IGNORECASE
        )
        if not named:
            # Try without separator: "April 1 PlayerName"
            named = re.match(
                r"^([A-Za-z]+)\s+(\d{1,2})(?:\s*(?:st|nd|rd|th))?\s+(.+)$",
                line, re.IGNORECASE
            )
        if named:
            month_str = named.group(1).lower()
            day       = int(named.group(2))
            name      = named.group(3).strip()
            month     = month_map.get(month_str)
            if month is None:
                errors.append(f"Unknown month '{named.group(1)}': {line}")
                continue
            try:
                d = date(current_year, month, day)
                schedule[d.isoformat()] = name
            except ValueError:
                errors.append(f"Invalid date: {line}")
            continue

        errors.append(f"Could not parse: {line}")

    return schedule, errors


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

# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You write short train announcements for OGV, a mobile game alliance. These are posted publicly to celebrate members — birthdays, new joins, milestones, war performances, contest wins, and more.

## Your voice and style

- Talk TO the alliance ABOUT the person — never narrate from the outside looking in
- Open with a strong statement that sets the scene or names the person directly
- Build momentum through the middle with specific details from the notes
- Land on a punchy, memorable closing line — a callback, a twist, or something clever tied to their name or the achievement
- Warm but confident. Never cheesy, never corporate, never stiff
- Feel like a quick real post someone actually typed — not a template
- Vary the structure every time. No two blurbs should open or flow the same way
- Use emojis sparingly and only when they fit naturally
- STRICT limit: under 500 characters including spaces

## What to avoid
- Do NOT open with "there's something about..." or soft observational language
- Do NOT over-explain or describe the vibe — state it and move on
- Do NOT use filler transition lines like "Feels like a natural fit already"
- Do NOT break ideas into separate short lines — flow them together
- Do NOT sound like an AI wrote it

## Style examples (study these carefully)

### Welcome to OGV
Input: Name=Cho Cho Bunny, Theme=Welcome to OGV, Notes=fun energy, easygoing
Output: Cho Cho Bunny bringing good vibes right on into OGV with them. Fun, friendly, and came ready to jump right into what our alliance has to offer. Welcome to OGV! 🐰

### Birthday
Input: Name=LaReyna, Theme=Birthday, Notes=queen energy, style and strength, amazing presence
Output: Happy Birthday LaReyna! Today we're celebrating a true queen! LaReyna brings style, strength, and amazing energy everywhere she goes. So today we raise the balloons, the cake, and all the birthday confetti in your honor. Wishing you a day full of love, laughter, and everything that makes you smile. May this year bring you big wins, unforgettable moments, and plenty of reasons to celebrate.

### Contest / Raffle — heartfelt moment
Input: Name=Landers, Theme=Contest win given away, Notes=won the train raffle but immediately tried to give it to PinkCatBoi
Output: Tonight's train raffle winner was Landers… but in true OGV fashion, he immediately tried to pass the spotlight to someone else. Instead of keeping the win, Landers wanted to give the train to PinkCatBoi. That right there is exactly what OGV has always been about — teammates lifting each other up and celebrating together. So whether it's Landers stepping up with the win or PinkCatBoi being the kind of teammate people want to share it with, this is the kind of energy that makes OGV special. Sometimes the best part of winning… is sharing it.

### Achievement / Milestone
Input: Name=Super Kale, Theme=Achievement, Notes=engineered Season Five, big-brain moves, clutch calls, strategy and coordination, superhero-level work
Output: Season Five didn't just happen… it was strategically engineered — and we all know who was behind the master plan. From the big-brain moves to the clutch calls that kept the alliance flying high, Super Kale has been out here doing superhero-level work for OGV. Your planning, coordination, and next-level foresight kept this train on the rails and the whole squad moving forward strong. Season Five absolutely would not have been the same without you. Honestly? The only word that fits is… superKALEifragilistic.

### Cinematic / Dramatic
Input: Name=TRC, Theme=General celebration, Tone=Cinematic, Notes=protecting the alliance, watching over everyone
Output: Out on the frontier, not every train makes it through the night… but this one's got protection. Mounted up and watching the tracks, TRC stands guard over the OGV Express — making sure the gold stays safe and the ride keeps rolling. Smoke in the air, iron on the rails, and anyone thinking about stopping this train better think twice. Around here the rule is simple: You ride with OGV… or you get left in the dust.

## Instructions
Generate exactly ONE blurb. Output only the blurb text — no explanation, no commentary, no label, nothing else. Make it feel personal, specific, and real."""


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


class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = None

    @discord.ui.button(label="✅ Generate Blurb", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        await interaction.response.defer()
        self.stop()


class RegenerateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.action = None

    @discord.ui.button(label="🔄 Regenerate", style=discord.ButtonStyle.primary)
    async def regenerate(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "regenerate"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="✅ Looks Good", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.action = "approve"
        await interaction.response.defer()
        self.stop()


class ReminderView(discord.ui.View):
    """Reminder button that launches the wizard with the name pre-filled."""

    def __init__(self, cog, name: str):
        super().__init__(timeout=3600)
        self.cog  = cog
        self.name = name

    @discord.ui.button(label="🚂 Generate Blurb Now", style=discord.ButtonStyle.success)
    async def launch_wizard(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Check role
        role_names = [r.name for r in interaction.user.roles]
        if REQUIRED_ROLE_NAME not in role_names:
            await interaction.response.send_message(
                f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to use this.",
                ephemeral=True,
            )
            return

        # Disable the button so it can't be clicked twice
        button.disabled = True
        await interaction.response.edit_message(view=self)

        # Launch wizard with name pre-filled
        channel = interaction.channel
        await run_train_wizard_prefilled(self.cog.bot, channel, interaction.user, self.name)
        self.stop()


# ── Claude API call ────────────────────────────────────────────────────────────

async def generate_blurb(name: str, theme: str, tone: str, notes: str) -> str:
    tone_str  = f"\nTone: {tone}" if tone != "Default (match the theme)" else ""
    notes_str = f"\nNotes: {notes}" if notes else "\nNotes: (none provided — keep it general but warm)"

    user_prompt = (
        f"Generate a train announcement blurb with these details:\n"
        f"Name: {name}\n"
        f"Theme: {theme}"
        f"{tone_str}"
        f"{notes_str}\n\n"
        f"Remember: under 500 characters, output only the blurb, no commentary."
    )

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 1000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_prompt}],
    }

    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(ANTHROPIC_API_URL, json=payload, headers=headers) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Claude API error {resp.status}: {text}")
            data = await resp.json()
            return data["content"][0]["text"].strip()


# ── Wizard core (shared between !train and reminder button) ────────────────────

async def run_wizard_steps(bot, channel, user, prefilled_name: str = None):
    """
    Core wizard logic. If prefilled_name is provided, skip Step 1.
    Returns True if a blurb was approved, False otherwise.
    """
    # Register this wizard session so !cancel can stop it
    cancel_event = asyncio.Event()
    active_wizards[user.id] = cancel_event

    def check_msg(m):
        return m.author == user and m.channel == channel

    async def ask(prompt: str) -> str | None:
        """Send a prompt and wait for a text reply. Returns None on timeout or cancel."""
        msg = await channel.send(prompt) if prompt else None
        try:
            # Wait for either a message reply or cancellation
            reply_task  = asyncio.ensure_future(bot.wait_for("message", check=check_msg, timeout=WIZARD_TIMEOUT))
            cancel_task = asyncio.ensure_future(cancel_event.wait())

            done, pending = await asyncio.wait(
                [reply_task, cancel_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

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

    try:
        # ── Step 1: Name ───────────────────────────────────────────────────────
        if prefilled_name:
            name = prefilled_name
            await channel.send(
                f"🚂 **Train Blurb Wizard** — started by {user.mention}\n\n"
                f"👤 **Name pre-filled from schedule:** {name}\n"
                f"*(Type `!cancel` at any time to stop the wizard)*"
            )
        else:
            await channel.send(
                f"🚂 **Train Blurb Wizard** — started by {user.mention}\n\n"
                f"**Step 1 of 4 — Member Name**\n"
                f"Type the member's name exactly as it should appear in the announcement.\n"
                f"*(Type `!cancel` at any time to stop the wizard)*"
            )
            name = await ask("")
            if not name or cancel_event.is_set():
                return False

        # ── Step 2: Theme ──────────────────────────────────────────────────────
        theme_msg  = await channel.send(f"**Step 2 of 4 — Theme**\nSelect the theme for this train:")
        theme_view = ThemeSelectView()
        await theme_msg.edit(view=theme_view)

        # Wait for theme selection or cancellation
        view_task   = asyncio.ensure_future(theme_view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        if cancel_event.is_set():
            for item in theme_view.children:
                item.disabled = True
            await theme_msg.edit(view=theme_view)
            return False

        if theme_view.selected is None:
            await channel.send("⏰ Wizard timed out. Use `!train` to start again.")
            return False

        theme = theme_view.selected
        if theme == "Custom":
            theme = await ask("Type your custom theme:")
            if not theme or cancel_event.is_set():
                return False

        # ── Step 3: Tone ───────────────────────────────────────────────────────
        tone_msg  = await channel.send(f"**Step 3 of 4 — Tone**\nSelect the tone (or leave as default to match the theme):")
        tone_view = ToneSelectView()
        await tone_msg.edit(view=tone_view)

        view_task   = asyncio.ensure_future(tone_view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        if cancel_event.is_set():
            for item in tone_view.children:
                item.disabled = True
            await tone_msg.edit(view=tone_view)
            return False

        if tone_view.selected is None:
            await channel.send("⏰ Wizard timed out. Use `!train` to start again.")
            return False

        tone = tone_view.selected

        # ── Step 4: Notes ──────────────────────────────────────────────────────
        await channel.send(
            f"**Step 4 of 4 — Notes** *(highly recommended)*\n"
            f"Add any details that make this personal — role, personality, achievements, "
            f"story moments, anything specific about them.\n"
            f"Type your notes below, or type `skip` to skip:"
        )
        notes_raw = await ask("")
        if notes_raw is None or cancel_event.is_set():
            return False
        notes = "" if notes_raw.lower() == "skip" else notes_raw

        # ── Summary & confirm ──────────────────────────────────────────────────
        tone_display  = tone if tone != "Default (match the theme)" else "Default"
        notes_display = notes if notes else "*(none)*"

        confirm_view = ConfirmView()
        await channel.send(
            f"**Ready to generate — here's your input:**\n\n"
            f"👤 **Name:** {name}\n"
            f"🎯 **Theme:** {theme}\n"
            f"🎭 **Tone:** {tone_display}\n"
            f"📝 **Notes:** {notes_display}\n\n"
            f"Generate the blurb?",
            view=confirm_view,
        )

        view_task   = asyncio.ensure_future(confirm_view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

        if cancel_event.is_set():
            for item in confirm_view.children:
                item.disabled = True
            return False

        if not confirm_view.confirmed:
            await channel.send("❌ Cancelled. Use `!train` to start over.")
            return False

        # ── Generate loop ──────────────────────────────────────────────────────
        generating_msg = await channel.send("✍️ Generating blurb...")

        while True:
            if cancel_event.is_set():
                await generating_msg.edit(content="❌ Wizard cancelled.")
                return False

            try:
                blurb = await generate_blurb(name, theme, tone, notes)
            except Exception as e:
                await generating_msg.edit(content=f"⚠️ Error generating blurb: {e}")
                return False

            char_count = len(blurb)
            warning = (
                f" ⚠️ *({char_count} chars — slightly over 500, consider editing)*"
                if char_count > 500 else f" ✅ *({char_count} chars)*"
            )

            regen_view = RegenerateView()
            await generating_msg.edit(
                content=f"🚂 **Generated blurb for {name}:**{warning}\n\n```\n{blurb}\n```",
                view=regen_view,
            )

            view_task   = asyncio.ensure_future(regen_view.wait())
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

            if cancel_event.is_set():
                for item in regen_view.children:
                    item.disabled = True
                await generating_msg.edit(view=regen_view)
                await generating_msg.edit(content="❌ Wizard cancelled.")
                return False

            if regen_view.action == "approve" or regen_view.action is None:
                for item in regen_view.children:
                    item.disabled = True
                await generating_msg.edit(view=regen_view)
                await channel.send(f"✅ **Final blurb for {name}** — ready to use:\n\n{blurb}")
                mark_blurb_generated(date.today().isoformat())
                return True

            await generating_msg.edit(content="✍️ Regenerating blurb...", view=None)

    finally:
        # Always clean up the registry when the wizard ends for any reason
        active_wizards.pop(user.id, None)


async def run_train_wizard(ctx: commands.Context):
    await run_wizard_steps(ctx.bot, ctx.channel, ctx.author)


async def run_train_wizard_prefilled(bot, channel, user, name: str):
    await run_wizard_steps(bot, channel, user, prefilled_name=name)


# ── Cog ────────────────────────────────────────────────────────────────────────

class TrainCog(commands.Cog):
    def __init__(self, bot):
        self.bot              = bot
        self.reminder_sent_today = False
        self.last_reminder_date  = None
        self.check_reminder.start()

    def cog_unload(self):
        self.check_reminder.cancel()

    def _has_leadership_role(self, ctx) -> bool:
        return REQUIRED_ROLE_NAME in [r.name for r in ctx.author.roles]

    def _is_leadership_channel(self, ctx) -> bool:
        return ctx.channel.id == LEADERSHIP_CHANNEL_ID

    # ── !cancel command ────────────────────────────────────────────────────────

    @commands.command(name="cancel")
    async def cancel(self, ctx: commands.Context):
        """Cancel any active wizard session for this user."""
        if not self._is_leadership_channel(ctx):
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            return

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        if ctx.author.id in active_wizards:
            active_wizards[ctx.author.id].set()
            await ctx.channel.send(
                f"❌ **Wizard cancelled** by {ctx.author.mention}.",
                delete_after=10,
            )
        else:
            await ctx.channel.send(
                f"ℹ️ You don't have an active wizard running.",
                delete_after=10,
            )

    # ── !trainschedule command (alias for !schedule list) ─────────────────────

    @commands.command(name="trainschedule")
    async def trainschedule(self, ctx: commands.Context):
        """Shortcut to view the current train schedule."""
        if not self._is_leadership_channel(ctx):
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            return

        if not self._has_leadership_role(ctx):
            await ctx.send(
                f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to use this command.",
                delete_after=10,
            )
            return

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        await self._show_schedule(ctx.channel)

    # ── !train command ─────────────────────────────────────────────────────────

    @commands.command(name="train")
    async def train(self, ctx: commands.Context):
        if not self._is_leadership_channel(ctx):
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            return

        if not self._has_leadership_role(ctx):
            await ctx.send(
                f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to use this command.",
                delete_after=10,
            )
            return

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        await run_train_wizard(ctx)

    # ── !schedule command ──────────────────────────────────────────────────────

    @commands.command(name="schedule")
    async def schedule(self, ctx: commands.Context, subcommand: str = None):
        if not self._is_leadership_channel(ctx):
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            return

        if not self._has_leadership_role(ctx):
            await ctx.send(
                f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to use this command.",
                delete_after=10,
            )
            return

        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

        # !schedule list
        if subcommand and subcommand.lower() == "list":
            await self._show_schedule(ctx.channel)
            return

        # !schedule clear
        if subcommand and subcommand.lower() == "clear":
            save_schedule({})
            await ctx.channel.send("🗑️ Train schedule cleared.")
            return

        # !schedule — input mode
        await self._collect_schedule(ctx)

    async def _show_schedule(self, channel):
        schedule = load_schedule()
        if not schedule:
            await channel.send("📋 No train schedule set. Use `!schedule` to add one.")
            return

        today = date.today()
        lines = ["📋 **Upcoming Train Schedule:**\n"]
        for date_str, name in sorted(schedule.items()):
            d = date.fromisoformat(date_str)
            generated = "✅" if date_str in load_blurb_log() else "⏳"
            past_marker = " *(past)*" if d < today else ""
            lines.append(f"{generated} **{d.strftime('%A, %B %-d')}** — {name}{past_marker}")

        await channel.send("\n".join(lines))

    async def _collect_schedule(self, ctx: commands.Context):
        channel = ctx.channel
        user    = ctx.author

        prompt_msg = await channel.send(
            f"📋 **Train Schedule Input** — {user.mention}\n\n"
            f"Paste your schedule below, one entry per line. Accepted formats:\n"
            f"```\n"
            f"April 2 - PlayerName\n"
            f"April 5 - PlayerName\n"
            f"4/8 - PlayerName\n"
            f"4/11/2026 - PlayerName\n"
            f"```\n"
            f"This will **replace** the current schedule. Type your schedule now:"
        )

        def check(m):
            return m.author == user and m.channel == channel

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT)
        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out. Use `!schedule` to try again.")
            return

        try:
            await prompt_msg.delete()
            await reply.delete()
        except discord.HTTPException:
            pass

        schedule, errors = parse_schedule_input(reply.content)

        if not schedule and errors:
            error_list = "\n".join(f"• {e}" for e in errors)
            await channel.send(
                f"⚠️ Could not parse any entries. Check your format and try again.\n\n"
                f"**Errors:**\n{error_list}"
            )
            return

        save_schedule(schedule)

        # Build confirmation message
        lines = ["✅ **Schedule saved:**\n"]
        for date_str, name in sorted(schedule.items()):
            d = date.fromisoformat(date_str)
            lines.append(f"• **{d.strftime('%A, %B %-d')}** — {name}")

        if errors:
            lines.append(f"\n⚠️ **Could not parse {len(errors)} line(s):**")
            for e in errors:
                lines.append(f"• {e}")

        await channel.send("\n".join(lines))

    # ── Reminder loop ──────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_reminder(self):
        """Check every minute if a blurb reminder needs to be sent."""
        now   = datetime.now(tz=ET)
        today = date.today()

        # Reset the sent flag at midnight each day
        if self.last_reminder_date != today:
            self.last_reminder_date  = today
            self.reminder_sent_today = False

        # Train reminder always fires at 10:30pm ET regardless of day
        # (Saturday time shift only applies to alliance events, not trains)
        reminder_hour, reminder_min = 22, 30

        if now.hour != reminder_hour or now.minute != reminder_min:
            return

        if self.reminder_sent_today:
            return

        # Check if today is on the schedule
        schedule = load_schedule()
        today_str = today.isoformat()
        if today_str not in schedule:
            return  # Nothing scheduled today

        # Check if a blurb has already been generated
        if blurb_generated_today():
            return

        # Fire the reminder
        name    = schedule[today_str]
        channel = self.bot.get_channel(LEADERSHIP_CHANNEL_ID)
        if channel is None:
            print(f"[TRAIN] Could not find leadership channel for reminder")
            return

        view = ReminderView(cog=self, name=name)
        await channel.send(
            f"⏰ **Train blurb reminder!**\n\n"
            f"Today's train is for **{name}** and no blurb has been generated yet. "
            f"Click below to launch the wizard with their name pre-filled.",
            view=view,
        )

        self.reminder_sent_today = True
        print(f"[TRAIN] Blurb reminder sent for {name} on {today_str}")

    @check_reminder.before_loop
    async def before_check_reminder(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(TrainCog(bot))

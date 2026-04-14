"""
storm_log.py — DS and CS event logging

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
from datetime import date

import discord
from discord import app_commands
from discord.ext import commands

# ── Config ─────────────────────────────────────────────────────────────────────

GUILD_ID            = 1266229297723605052
GUILD               = discord.Object(id=GUILD_ID)
STORM_LOG_THREAD_ID = 1483977424231469229
LOG_SHEET_NAME      = "DS-CS Sit-outs"
REQUIRED_ROLE_NAME  = "OGV"
LEADERSHIP_CAT_ID   = 1266243885743603783
WIZARD_TIMEOUT      = 600  # 10 minutes

# Active log sessions — user_id → asyncio.Event (cancel signal)
# Checked by /cancel in train.py so one command covers everything
active_logs: dict = {}

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
    existing = ws.row_values(1)
    if not any(existing):
        ws.update("A1", [LOG_HEADERS], value_input_option="USER_ENTERED")


def append_log_row(event_type, log_date, vote_count, rtf_no_vote, sitting_out, prior_no_request):
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


def load_member_names():
    try:
        from train import get_member_tab_name, _get_member_sheet
        tab_name = get_member_tab_name()
        ws       = _get_member_sheet(tab_name)
        rows     = ws.get_all_values()
        names    = []
        for row in rows[9:]:
            if len(row) >= 5:
                name = row[4].strip()
                if name:
                    names.append(name)
        print(f"[LOG] Loaded {len(names)} member names from '{tab_name}'")
        return names
    except Exception as e:
        print(f"[LOG] Error loading member names: {e}")
        return []


def get_prior_sitouts(event_type):
    try:
        ws   = _get_log_sheet()
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return []
        for row in reversed(rows[1:]):
            if len(row) >= 2 and row[1].strip().upper() == event_type.upper():
                raw = row[4].strip() if len(row) >= 5 else ""
                if raw:
                    return [n.strip() for n in raw.split(",") if n.strip()]
        return []
    except Exception as e:
        print(f"[LOG] Error reading prior sit-outs: {e}")
        return []


# ── Paginated name select view ─────────────────────────────────────────────────

class NameSelectView(discord.ui.View):
    """
    Single paginated dropdown — 25 names per page with Prev/Next navigation.
    Selected names accumulate across pages. Done confirms, Skip clears and confirms.
    """
    def __init__(self, names, label, optional=False):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.all_names = names
        self.label     = label
        self.optional  = optional
        self.selected  = set()
        self.confirmed = False
        self.page      = 0
        self.page_size = 25
        self._build_select()

    @property
    def total_pages(self):
        return max(1, -(-len(self.all_names) // self.page_size))

    def _page_names(self):
        start = self.page * self.page_size
        return self.all_names[start:start + self.page_size]

    def _build_select(self):
        for item in list(self.children):
            if isinstance(item, discord.ui.Select):
                self.remove_item(item)
        page_names = self._page_names()
        select = discord.ui.Select(
            placeholder=f"{self.label} — page {self.page + 1}/{self.total_pages}",
            options=[
                discord.SelectOption(label=n, value=n, default=(n in self.selected))
                for n in page_names
            ],
            min_values=0,
            max_values=len(page_names),
            row=0,
        )
        async def _cb(interaction: discord.Interaction):
            for n in page_names:
                if n in select.values:
                    self.selected.add(n)
                else:
                    self.selected.discard(n)
            self._build_select()
            await interaction.response.edit_message(content=self._selected_text(), view=self)
        select.callback = _cb
        self.add_item(select)

    def _selected_text(self):
        if self.selected:
            return f"**Selected ({len(self.selected)}):** {', '.join(sorted(self.selected))}"
        return "*No one selected yet — use the dropdown or press Skip.*"

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self._build_select()
        await interaction.response.edit_message(content=self._selected_text(), view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
            self._build_select()
        await interaction.response.edit_message(content=self._selected_text(), view=self)

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=1)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Skip (none)", style=discord.ButtonStyle.secondary, row=1)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.selected  = set()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


# ── Shared log flow ────────────────────────────────────────────────────────────

async def run_log_flow(bot, channel, user, event_type):
    is_ds        = event_type.upper() == "DS"
    event_label  = "Desert Storm" if is_ds else "Canyon Storm"
    cancel_event = asyncio.Event()
    active_logs[user.id] = cancel_event

    def check(m):
        return m.author == user and m.channel == channel

    async def wait_for_msg(prompt_text):
        prompt_msg = await channel.send(prompt_text)
        try:
            reply_task  = asyncio.ensure_future(bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT))
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            done, pending = await asyncio.wait([reply_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if cancel_event.is_set():
                try:
                    await prompt_msg.delete()
                except discord.HTTPException:
                    pass
                return None
            reply = done.pop().result()
            try:
                await prompt_msg.delete()
                await reply.delete()
            except discord.HTTPException:
                pass
            return reply.content.strip()
        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out. Use the log command to start again.")
            return None

    async def wait_for_select(view, prompt_msg):
        view_task   = asyncio.ensure_future(view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        try:
            await prompt_msg.delete()
        except discord.HTTPException:
            pass
        if cancel_event.is_set():
            for item in view.children:
                item.disabled = True
            return False
        if not view.confirmed:
            await channel.send("⏰ Timed out. Use the log command to start again.")
            return False
        return True

    try:
        await channel.send(
            f"📋 **{event_label} Log** — started by {user.mention}\n"
            "*Use `/cancel` at any time to stop.*"
        )

        # ── Step 1: Date ──────────────────────────────────────────────────────
        raw_date = await wait_for_msg(
            "**Step 1 — Event date**\n"
            "Type the date (e.g. `April 14`, `4/14`) or type `today`:"
        )
        if raw_date is None:
            if cancel_event.is_set():
                await channel.send("❌ Log cancelled.")
            return

        if raw_date.lower() == "today":
            log_date = date.today()
        else:
            from train import parse_date_and_name
            parsed_d, _, _ = parse_date_and_name(f"{raw_date} - placeholder")
            if not parsed_d:
                await channel.send("⚠️ Could not parse that date. Use the log command to start again.")
                return
            log_date = parsed_d

        # ── Step 2 (DS only): Vote count ──────────────────────────────────────
        vote_count = None
        if is_ds:
            raw_votes = await wait_for_msg(
                "**Step 2 — Vote count**\n"
                "How many members voted in the participation poll? (type a number)"
            )
            if raw_votes is None:
                if cancel_event.is_set():
                    await channel.send("❌ Log cancelled.")
                return
            try:
                vote_count = int(raw_votes)
            except ValueError:
                await channel.send("⚠️ That doesn't look like a number. Use the log command to start again.")
                return

        # ── Load member names ─────────────────────────────────────────────────
        names = await asyncio.get_event_loop().run_in_executor(None, load_member_names)
        if not names:
            await channel.send("⚠️ Could not load member names. Check `/setmembertab` and try again.")
            return

        # ── Step 3 (DS only): RTF but no vote ─────────────────────────────────
        rtf_no_vote = []
        if is_ds:
            rtf_view = NameSelectView(names, "Select members who RTF'd but didn't vote")
            rtf_msg  = await channel.send(
                "**Step 3 — Requested to Fight but did not vote**\n"
                "Select any members who submitted RTF but did not vote in the poll. "
                "Press **Skip** if none.",
                view=rtf_view,
            )
            if not await wait_for_select(rtf_view, rtf_msg):
                if cancel_event.is_set():
                    await channel.send("❌ Log cancelled.")
                return
            rtf_no_vote = sorted(rtf_view.selected)

        # ── Step 4: Sitting out this week ─────────────────────────────────────
        step_num = 4 if is_ds else 2
        sit_view = NameSelectView(names, "Select members sitting out this week")
        sit_msg  = await channel.send(
            f"**Step {step_num} — Sitting out this week**\n"
            "Select any members sitting out today. Press **Skip** if none.",
            view=sit_view,
        )
        if not await wait_for_select(sit_view, sit_msg):
            if cancel_event.is_set():
                await channel.send("❌ Log cancelled.")
            return
        sitting_out = sorted(sit_view.selected)

        # ── Step 5: Prior sit-outs who didn't request ─────────────────────────
        step_num    = 5 if is_ds else 3
        prior_names = await asyncio.get_event_loop().run_in_executor(
            None, get_prior_sitouts, event_type
        )

        prior_no_request = []
        if prior_names:
            action_word = "vote" if is_ds else "request to fight"
            prior_view  = NameSelectView(prior_names, "Select prior sit-outs who didn't participate")
            prior_msg   = await channel.send(
                f"**Step {step_num} — Prior sit-outs who did not {action_word} this week**\n"
                "These members sat out last time. Select any who did not participate this week. "
                "Press **Skip** if none.",
                view=prior_view,
            )
            if not await wait_for_select(prior_view, prior_msg):
                if cancel_event.is_set():
                    await channel.send("❌ Log cancelled.")
                return
            prior_no_request = sorted(prior_view.selected)
        else:
            await channel.send(
                f"**Step {step_num} — Prior sit-outs**\n"
                "*(No prior sit-outs found in last log — skipping)*",
                delete_after=5,
            )

        # ── Save to sheet ─────────────────────────────────────────────────────
        await channel.send("💾 Saving log...")
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, append_log_row,
                event_type.upper(), log_date, vote_count,
                rtf_no_vote, sitting_out, prior_no_request,
            )
        except Exception as e:
            await channel.send(f"⚠️ Error saving to sheet: {e}")
            return

        # ── Summary ───────────────────────────────────────────────────────────
        date_str     = log_date.strftime("%A, %B %-d, %Y")
        action_label = "Vote" if is_ds else "Request"
        lines = [f"📋 **{event_label} Log — {date_str}**"]
        if is_ds:
            lines.append(f"**Votes:** {vote_count}")
            lines.append(f"**RTF No Vote:** {', '.join(rtf_no_vote) if rtf_no_vote else 'None'}")
        lines.append(f"**Sitting Out:** {', '.join(sitting_out) if sitting_out else 'None'}")
        lines.append(f"**Prior Sit-Out No {action_label}:** {', '.join(prior_no_request) if prior_no_request else 'None'}")
        summary = "\n".join(lines)

        await channel.send(f"✅ **Log saved!**\n\n{summary}")

        # Only post to the log thread if we're not already in it
        try:
            if channel.id != STORM_LOG_THREAD_ID:
                thread = bot.get_channel(STORM_LOG_THREAD_ID)
                if thread:
                    await thread.send(summary)
        except Exception as e:
            print(f"[LOG] Error posting to log thread: {e}")

    finally:
        active_logs.pop(user.id, None)


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

    @app_commands.command(name="logds", description="Log Desert Storm participation data")
    @app_commands.guilds(GUILD)
    async def logds(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        if interaction.user.id in active_logs:
            await interaction.response.send_message(
                "⚠️ You already have an active log session. Use `/cancel` to stop it first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("📋 Starting DS log...", ephemeral=True)
        await run_log_flow(self.bot, interaction.channel, interaction.user, "DS")

    @app_commands.command(name="logcs", description="Log Canyon Storm participation data")
    @app_commands.guilds(GUILD)
    async def logcs(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        if interaction.user.id in active_logs:
            await interaction.response.send_message(
                "⚠️ You already have an active log session. Use `/cancel` to stop it first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("📋 Starting CS log...", ephemeral=True)
        await run_log_flow(self.bot, interaction.channel, interaction.user, "CS")


async def setup(bot: commands.Bot):
    await bot.add_cog(LogCog(bot))

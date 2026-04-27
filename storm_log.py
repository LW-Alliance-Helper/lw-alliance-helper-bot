"""
storm_log.py — DS and CS event logging

Slash commands:
  /desertstorm_participation — Log Desert Storm participation data
  /canyonstorm_participation — Log Canyon Storm participation data
  /desertstorm_log [date]    — View a Desert Storm log entry
  /canyonstorm_log [date]    — View a Canyon Storm log entry

Writes to the "DS-CS Sit-outs" tab in Google Sheets and posts a
summary to the dedicated log thread configured for this guild.

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
from config import get_config

# ── Config ─────────────────────────────────────────────────────────────────────

WIZARD_TIMEOUT      = 600  # 10 minutes

# Active log sessions — user_id → asyncio.Event (cancel signal)
# Checked by /cancel in train.py so one command covers everything
active_logs: dict = {}

LOG_HEADERS = [
    "Date", "Event", "Vote Count", "RTF No Vote",
    "Sitting Out", "Prior Sit-Out No Request",
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


def _get_log_sheet(guild_id: int = None):
    from config import get_config
    cfg = get_config(guild_id)
    tab = cfg.tab_sitouts if cfg else "DS-CS Sit-outs"
    return _get_spreadsheet(guild_id).worksheet(tab)


def _ensure_headers(ws):
    existing = ws.row_values(1)
    if not any(existing):
        ws.update("A1", [LOG_HEADERS], value_input_option="USER_ENTERED")


def append_log_row(event_type, log_date, vote_count, rtf_no_vote, sitting_out, prior_no_request, guild_id=None):
    ws = _get_log_sheet(guild_id)
    _ensure_headers(ws)
    row = [
        f"{log_date.month}/{log_date.day}/{log_date.year}",
        event_type,
        str(vote_count) if vote_count is not None else "",
        ", ".join(rtf_no_vote),
        ", ".join(sitting_out),
        ", ".join(prior_no_request),
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"[LOG] {event_type} log appended for {log_date.isoformat()}")


def load_member_names():
    """
    Load member names and aliases from the active member tab.
    Col E (index 4) = Name, Col F (index 5) = Alias (optional), starting row 10.
    Returns (names, alias_map) where alias_map is { alias.lower(): full_name }.
    """
    try:
        from train import get_member_tab_name, _get_member_sheet
        tab_name  = get_member_tab_name()
        ws        = _get_member_sheet(tab_name)
        rows      = ws.get_all_values()
        names     = []
        alias_map = {}
        for row in rows[9:]:
            if len(row) >= 5:
                name = row[4].strip()
                if name:
                    names.append(name)
                    # Read alias from col F if present
                    if len(row) >= 6:
                        alias = row[5].strip()
                        if alias:
                            alias_map[alias.lower()] = name
        print(f"[LOG] Loaded {len(names)} members ({len(alias_map)} aliases) from '{tab_name}'")
        return names, alias_map
    except Exception as e:
        print(f"[LOG] Error loading member names: {e}")
        return [], {}


def get_prior_sitouts(event_type, guild_id=None):
    try:
        ws   = _get_log_sheet(guild_id)
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


# ── Name entry modal ──────────────────────────────────────────────────────────

class NameEntryModal(discord.ui.Modal):
    """
    Popup text box where the user types names comma-separated or one per line.
    Matches against the known roster by exact name or alias (col F in member tab).
    """
    def __init__(self, all_names: list, label: str, alias_map: dict = None):
        super().__init__(title=label[:45])
        self.all_names  = all_names
        self.name_map   = {n.lower(): n for n in all_names}  # lower → original
        self.alias_map  = alias_map or {}                     # alias.lower() → original name
        self.confirmed  = False
        self.selected   = []
        self.unrecognized = []

        self.text_input = discord.ui.TextInput(
            label="Names (comma-separated or one per line)",
            style=discord.TextStyle.paragraph,
            placeholder="e.g. Jon, Lionel, Ice — or leave blank and submit for none",
            required=False,
            max_length=1000,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.text_input.value.strip()
        if not raw:
            self.selected     = []
            self.unrecognized = []
            self.confirmed    = True
            await interaction.response.defer()
            self.stop()
            return

        import re
        parts = [p.strip() for p in re.split(r"[,\n]+", raw) if p.strip()]

        recognized   = []
        unrecognized = []
        for part in parts:
            lower = part.lower()
            if lower in self.name_map:
                # Exact case-insensitive match
                recognized.append(self.name_map[lower])
            elif lower in self.alias_map:
                # Alias match (e.g. "INSH4F" or "landers" → full decorated name)
                recognized.append(self.alias_map[lower])
            else:
                unrecognized.append(part)

        self.selected     = recognized
        self.unrecognized = unrecognized
        self.confirmed    = True
        await interaction.response.defer()
        self.stop()


class UnrecognizedView(discord.ui.View):
    """
    Shown when unrecognized names are submitted. Lets the user save as-is
    (visitor) or go back and re-enter.
    """
    def __init__(self, unrecognized: list):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.unrecognized = unrecognized
        self.save_as_is   = False
        self.redo         = False

    @discord.ui.button(label="Save as Visitor", style=discord.ButtonStyle.secondary, row=0)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.save_as_is = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Re-enter Names", style=discord.ButtonStyle.primary, row=0)
    async def redo_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.redo = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class NameEntryView(discord.ui.View):
    """
    Shows an Enter Names button that opens a modal.
    If unrecognized names are submitted, asks the user to save as visitor or re-enter.
    Loops until the user is satisfied or skips.
    """
    def __init__(self, all_names: list, label: str, alias_map: dict = None):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.all_names    = all_names
        self.label        = label
        self.alias_map    = alias_map or {}
        self.confirmed    = False
        self.selected     = []
        self.unrecognized = []

    @discord.ui.button(label="✏️ Enter Names", style=discord.ButtonStyle.primary, row=0)
    async def enter_names(self, interaction: discord.Interaction, button: discord.ui.Button):
        while True:
            modal = NameEntryModal(self.all_names, self.label, self.alias_map)
            await interaction.response.send_modal(modal)
            timed_out = await modal.wait()
            if timed_out or not modal.confirmed:
                return  # Let outer timeout handler deal with it

            recognized   = modal.selected
            unrecognized = modal.unrecognized

            if not unrecognized:
                # All names recognized — done
                self.selected     = recognized
                self.unrecognized = []
                self.confirmed    = True
                for item in self.children:
                    item.disabled = True
                result = f"**Entered ({len(recognized)}):** {', '.join(recognized)}" if recognized else "*None entered.*"
                try:
                    await interaction.message.edit(content=result, view=self)
                except discord.HTTPException:
                    pass
                self.stop()
                return

            # Some unrecognized — ask what to do
            unrecog_str  = ", ".join(unrecognized)
            unrecog_view = UnrecognizedView(unrecognized)
            try:
                await interaction.message.edit(
                    content=(
                        f"⚠️ **Not recognized:** {unrecog_str}\n"
                        "These names aren't in the roster. Are they visitors or did you make a typo?"
                    ),
                    view=unrecog_view,
                )
            except discord.HTTPException:
                pass

            await unrecog_view.wait()

            if unrecog_view.save_as_is:
                # Save recognized + unrecognized (visitors)
                self.selected     = recognized
                self.unrecognized = unrecognized
                self.confirmed    = True
                for item in self.children:
                    item.disabled = True
                lines = []
                if recognized:
                    lines.append(f"**Entered ({len(recognized)}):** {', '.join(recognized)}")
                if unrecognized:
                    lines.append(f"**Visitors:** {unrecog_str}")
                result = "\n".join(lines) if lines else "*None entered.*"
                try:
                    await interaction.message.edit(content=result, view=self)
                except discord.HTTPException:
                    pass
                self.stop()
                return

            if unrecog_view.redo:
                # Restore the Enter Names button so they can try again
                try:
                    await interaction.message.edit(
                        content="*Re-enter names — press Enter Names again:*",
                        view=self,
                    )
                except discord.HTTPException:
                    pass
                # Loop back to modal
                # Need a new interaction — can't reuse the old one after message edit
                # So we just stop and let the button be clickable again
                return

    @discord.ui.button(label="Skip (none)", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed    = True
        self.selected     = []
        self.unrecognized = []
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="*Skipped — none.*", view=self)
        self.stop()


class ShortSelectView(discord.ui.View):
    """Simple single-page select for short lists (e.g. prior sit-outs, always < 25)."""
    def __init__(self, names: list, label: str):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = False
        self.selected  = set()

        select = discord.ui.Select(
            placeholder=label,
            options=[discord.SelectOption(label=n, value=n) for n in names],
            min_values=0,
            max_values=len(names),
            row=0,
        )
        async def _cb(interaction: discord.Interaction):
            self.selected = set(select.values)
            await interaction.response.defer()
        select.callback = _cb
        self.add_item(select)

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

    async def wait_for_view(view, prompt_msg):
        """Wait for any view (NameEntryView or ShortSelectView). Returns False if cancelled/timed out."""
        view_task   = asyncio.ensure_future(view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if cancel_event.is_set():
            for item in view.children:
                item.disabled = True
            try:
                await prompt_msg.edit(view=view)
            except discord.HTTPException:
                pass
            return False
        if not view.confirmed:
            try:
                await prompt_msg.delete()
            except discord.HTTPException:
                pass
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
        loading_msg = await channel.send("⏳ Gathering member list...")
        names, alias_map = await asyncio.get_event_loop().run_in_executor(None, load_member_names)
        try:
            await loading_msg.delete()
        except discord.HTTPException:
            pass
        if not names:
            await channel.send(
                "⚠️ Could not load member names. "
                "Run `/setup_birthdays` (or another module setup) to confirm the member tab name and try again."
            )
            return

        roster_preview = ", ".join(names)

        # ── Step 3: Sitting out this week ─────────────────────────────────────
        step_num = 3 if is_ds else 2
        sit_view = NameEntryView(names, "Sitting Out", alias_map)
        sit_msg  = await channel.send(
            f"**Step {step_num} — Sitting out this week**\n"
            "Press **Enter Names** to type who is sitting out today. "
            "Press **Skip** if none.\n"
            f"*Roster: {roster_preview}*",
            view=sit_view,
        )
        if not await wait_for_view(sit_view, sit_msg):
            if cancel_event.is_set():
                await channel.send("❌ Log cancelled.")
            return
        sitting_out = sorted(sit_view.selected)
        if sit_view.unrecognized:
            sitting_out += sorted(sit_view.unrecognized)

        # ── Step 4 (DS only): RTF but no vote ─────────────────────────────────
        rtf_no_vote = []
        if is_ds:
            rtf_view = NameEntryView(names, "RTF No Vote", alias_map)
            rtf_msg  = await channel.send(
                "**Step 4 — Requested to Fight but did not vote**\n"
                "Press **Enter Names** to type who submitted RTF but did not vote. "
                "Press **Skip** if none.\n"
                f"*Roster: {roster_preview}*",
                view=rtf_view,
            )
            if not await wait_for_view(rtf_view, rtf_msg):
                if cancel_event.is_set():
                    await channel.send("❌ Log cancelled.")
                return
            rtf_no_vote = sorted(rtf_view.selected)
            if rtf_view.unrecognized:
                rtf_no_vote += sorted(rtf_view.unrecognized)

        # ── Step 5: Prior sit-outs who didn't request ─────────────────────────
        step_num      = 5 if is_ds else 3
        loading_msg   = await channel.send("⏳ Checking previous log...")
        _gid2 = channel.guild.id if hasattr(channel, "guild") and channel.guild else None
        prior_names   = await asyncio.get_event_loop().run_in_executor(
            None, get_prior_sitouts, event_type, _gid2
        )
        try:
            await loading_msg.delete()
        except discord.HTTPException:
            pass

        prior_no_request = []
        if prior_names:
            action_word = "vote" if is_ds else "request to fight"
            prior_view  = ShortSelectView(prior_names, "Select prior sit-outs who didn't participate")
            prior_msg   = await channel.send(
                f"**Step {step_num} — Prior sit-outs who did not {action_word} this week**\n"
                "These members sat out last time. Select any who did not participate this week. "
                "Press **Skip** if none.",
                view=prior_view,
            )
            if not await wait_for_view(prior_view, prior_msg):
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
        date_str     = f"{log_date:%A, %B} {log_date.day}, {log_date.year}"
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
            from config import get_config as _slcfg
            cfg_sl   = _slcfg(channel.guild.id) if hasattr(channel, 'guild') else None
            thread_id = cfg_sl.storm_log_thread_id if cfg_sl else 0
            if channel.id != thread_id:
                thread = bot.get_channel(thread_id)
                if thread:
                    await thread.send(summary)
        except Exception as e:
            print(f"[LOG] Error posting to log thread: {e}")

    finally:
        active_logs.pop(user.id, None)


# ── Log lookup ─────────────────────────────────────────────────────────────────

def list_recent_log_dates(event_type: str, n: int, guild_id=None) -> list[date]:
    """
    Return up to `n` most-recent log dates for `event_type`, sorted newest-first.
    Used to gate free-tier access (only the N most recent entries are visible).
    """
    out: list[date] = []
    try:
        ws   = _get_log_sheet(guild_id)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return out
        from datetime import datetime
        seen: set[date] = set()
        parsed: list[date] = []
        for row in rows[1:]:
            if len(row) < 2:
                continue
            if row[1].strip().upper() != event_type.upper():
                continue
            row_date = row[0].strip()
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    d = datetime.strptime(row_date, fmt).date()
                    if d not in seen:
                        seen.add(d)
                        parsed.append(d)
                    break
                except ValueError:
                    continue
        parsed.sort(reverse=True)
        return parsed[:n]
    except Exception as e:
        print(f"[STORM_LOG] Could not list recent dates: {e}")
        return out


def lookup_log_entry(event_type: str, log_date: date, guild_id=None):
    """
    Find the most recent log row matching event_type and log_date.
    Returns a dict of the row data, or None if not found.
    """
    try:
        ws   = _get_log_sheet(guild_id)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return None
        target_date = f"{log_date.month}/{log_date.day}/{log_date.year}"
        for row in reversed(rows[1:]):
            if len(row) < 2:
                continue
            if row[1].strip().upper() != event_type.upper():
                continue
            # Normalize date for comparison
            row_date = row[0].strip()
            try:
                # Parse various date formats that might be in the sheet
                from datetime import datetime
                for fmt in ("%-m/%-d/%Y", "%m/%d/%Y", "%Y-%m-%d"):
                    try:
                        parsed = datetime.strptime(row_date, fmt).date()
                        if parsed == log_date:
                            return {
                                "date":             row[0] if len(row) > 0 else "",
                                "event":            row[1] if len(row) > 1 else "",
                                "vote_count":       row[2] if len(row) > 2 else "",
                                "rtf_no_vote":      row[3] if len(row) > 3 else "",
                                "sitting_out":      row[4] if len(row) > 4 else "",
                                "prior_no_request": row[5] if len(row) > 5 else "",
                            }
                    except ValueError:
                        continue
            except Exception:
                continue
        return None
    except Exception as e:
        print(f"[LOG] Error looking up log entry: {e}")
        return None


# ── Guard ──────────────────────────────────────────────────────────────────────

def _in_channel(interaction: discord.Interaction) -> bool:
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
    if cfg.leadership_role_name not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.", ephemeral=True
        )
        return False
    return True


# ── Cog ────────────────────────────────────────────────────────────────────────

class LogCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="desertstorm_participation",
        description="Log Desert Storm participation data",
    )
    async def desertstorm_participation(self, interaction: discord.Interaction):
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

    @app_commands.command(
        name="canyonstorm_participation",
        description="Log Canyon Storm participation data",
    )
    async def canyonstorm_participation(self, interaction: discord.Interaction):
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

    @app_commands.command(
        name="desertstorm_log",
        description="View a Desert Storm log entry (defaults to today)",
    )
    @app_commands.describe(date="Optional date, e.g. 'April 14' or '4/14' (defaults to today)")
    async def desertstorm_log(self, interaction: discord.Interaction, date: str = None):
        await _show_storm_log(interaction, "DS", date)

    @app_commands.command(
        name="canyonstorm_log",
        description="View a Canyon Storm log entry (defaults to today)",
    )
    @app_commands.describe(date="Optional date, e.g. 'April 14' or '4/14' (defaults to today)")
    async def canyonstorm_log(self, interaction: discord.Interaction, date: str = None):
        await _show_storm_log(interaction, "CS", date)


async def _show_storm_log(interaction: discord.Interaction, event: str, date: str | None):
    """Shared handler for /desertstorm_log and /canyonstorm_log."""
    if not await _guard(interaction):
        return

    await interaction.response.defer()

    if date:
        from train import parse_date_and_name
        parsed_d, _, _ = parse_date_and_name(f"{date} - placeholder")
        if not parsed_d:
            await interaction.followup.send(
                f"⚠️ Could not parse date **{date}**. Try a format like `April 14` or `4/14`.",
                ephemeral=True,
            )
            return
    else:
        from datetime import date as date_cls
        parsed_d = date_cls.today()

    event_label = "Desert Storm" if event == "DS" else "Canyon Storm"

    # Free tier sees only the most recent N storm participation log entries.
    import premium
    recent_cap = await premium.get_limit(
        "storm_log_recent", interaction.guild_id, interaction=interaction,
    )
    if recent_cap is not None:
        recent_dates = await asyncio.get_event_loop().run_in_executor(
            None, list_recent_log_dates, event, recent_cap, interaction.guild_id,
        )
        if recent_dates and parsed_d not in recent_dates:
            embed = premium.limit_reached_embed(
                feature_label=f"{event_label} log lookback",
                current=recent_cap, cap=recent_cap,
                plural_unit="most-recent log entries",
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

    entry = await asyncio.get_event_loop().run_in_executor(
        None, lookup_log_entry, event, parsed_d
    )

    if entry is None:
        await interaction.followup.send(
            f"❌ No **{event_label}** log found for **{parsed_d:%B} {parsed_d.day}, {parsed_d.year}**.",
            ephemeral=True,
        )
        return

    date_str     = f"{parsed_d:%A, %B} {parsed_d.day}, {parsed_d.year}"
    action_label = "Vote" if event == "DS" else "Request"

    lines = [f"📋 **{event_label} Log — {date_str}**"]
    if event == "DS":
        lines.append(f"**Votes:** {entry['vote_count'] or 'Not recorded'}")
        lines.append(f"**RTF No Vote:** {entry['rtf_no_vote'] or 'None'}")
    lines.append(f"**Sitting Out:** {entry['sitting_out'] or 'None'}")
    lines.append(f"**Prior Sit-Out No {action_label}:** {entry['prior_no_request'] or 'None'}")

    await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(LogCog(bot))

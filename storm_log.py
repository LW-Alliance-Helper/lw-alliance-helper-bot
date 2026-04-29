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


def _get_log_sheet(guild_id: int = None, event_type: str | None = None):
    """
    Resolve the worksheet that holds participation rows. Prefers the
    per-event-type `participation_tab_name` from the new config; falls
    back to the legacy `tab_sitouts` shared tab so any data written
    under the pre-rework schema keeps loading via /[event]_log.
    """
    from config import get_config, get_participation_config
    sh = _get_spreadsheet(guild_id)
    if event_type and guild_id:
        pcfg = get_participation_config(guild_id, event_type)
        tab = pcfg.get("tab_name") or ""
        if tab:
            try:
                return sh.worksheet(tab)
            except Exception:
                pass  # tab missing — fall back below
    cfg = get_config(guild_id)
    tab = cfg.tab_sitouts if cfg else "DS-CS Sit-outs"
    return sh.worksheet(tab)


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


# ── Roster + sheet helpers (new configurable participation flow) ──────────────

def load_roster_from_config(guild_id: int, event_type: str) -> tuple[list[str], dict[str, str]]:
    """
    Read the configured roster source for the given (guild, event_type) and
    return (names, alias_map). Replaces an earlier hardcoded col-E lookup
    in `load_member_names()` — every alliance configures their own roster
    tab + name column + optional alias column via /setup_desertstorm or
    /setup_canyonstorm.
    """
    from config import get_participation_config
    pcfg = get_participation_config(guild_id, event_type)
    tab       = pcfg.get("roster_tab") or ""
    name_col  = int(pcfg.get("roster_name_col") or 0)
    alias_col = int(pcfg.get("roster_alias_col") if pcfg.get("roster_alias_col") is not None else -1)
    start_row = int(pcfg.get("roster_start_row") or 2)

    names: list[str] = []
    alias_map: dict[str, str] = {}
    if not tab:
        return names, alias_map

    try:
        ws   = _get_spreadsheet(guild_id).worksheet(tab)
        rows = ws.get_all_values()
    except Exception as e:
        print(f"[LOG] Could not read roster tab `{tab}` for guild {guild_id}: {e}")
        return names, alias_map

    for row in rows[start_row - 1:]:  # start_row is 1-indexed
        if name_col >= len(row):
            continue
        name = row[name_col].strip()
        if not name:
            continue
        names.append(name)
        if alias_col >= 0 and alias_col < len(row):
            alias = row[alias_col].strip()
            if alias:
                alias_map[alias.lower()] = name

    print(f"[LOG] Loaded {len(names)} roster names ({len(alias_map)} aliases) "
          f"from `{tab}` (guild {guild_id}, {event_type})")
    return names, alias_map


def append_participation_row(guild_id: int, event_type: str,
                              log_date, answers: dict[str, str]) -> None:
    """
    Append a row to the configured participation tab. Header columns are:
    Date | Event | <one column per configured question, in order>.
    """
    from config import get_participation_config
    pcfg = get_participation_config(guild_id, event_type)
    tab  = pcfg.get("tab_name") or (
        "DS Participation Log" if event_type.upper() == "DS" else "CS Participation Log"
    )
    questions = pcfg.get("questions") or []

    sh = _get_spreadsheet(guild_id)
    try:
        ws = sh.worksheet(tab)
    except Exception:
        # Create the tab if it doesn't exist
        ws = sh.add_worksheet(title=tab, rows=200, cols=max(8, len(questions) + 4))

    headers = ["Date", "Event"] + [q.get("label", q.get("key", "?")) for q in questions]
    existing_header = ws.row_values(1)
    if not any(existing_header):
        ws.update("A1", [headers], value_input_option="USER_ENTERED")

    row = [
        f"{log_date.month}/{log_date.day}/{log_date.year}",
        event_type.upper(),
    ]
    for q in questions:
        val = answers.get(q.get("key", ""), "")
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val)
        row.append(str(val) if val is not None else "")
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"[LOG] Participation row appended for guild={guild_id} "
          f"event={event_type} date={log_date.isoformat()}")


# ── Shared log flow (new configurable version) ───────────────────────────────

async def run_log_flow(bot, channel, user, event_type):
    """
    Walk leadership through the participation log flow. The questions
    asked are read from the per-guild participation config saved by
    /setup_desertstorm or /setup_canyonstorm. The date is always asked
    first (mandatory, never configurable).
    """
    is_ds        = event_type.upper() == "DS"
    event_label  = "Desert Storm" if is_ds else "Canyon Storm"
    log_cmd      = "/desertstorm_participation" if is_ds else "/canyonstorm_participation"
    setup_cmd    = "/setup_desertstorm"          if is_ds else "/setup_canyonstorm"
    guild_id     = channel.guild.id if hasattr(channel, "guild") and channel.guild else None
    cancel_event = asyncio.Event()
    active_logs[user.id] = cancel_event

    from config import get_participation_config
    pcfg = get_participation_config(guild_id, event_type) if guild_id else {}

    if not pcfg.get("enabled"):
        await channel.send(
            f"⚙️ Participation tracking isn't enabled for {event_label} yet. "
            f"Run `{setup_cmd}` and walk through Step 6 to define what you want to track."
        )
        active_logs.pop(user.id, None)
        return

    questions = pcfg.get("questions") or []
    if not questions:
        await channel.send(
            f"⚙️ Participation tracking is enabled but no questions are configured. "
            f"Run `{setup_cmd}` to add questions."
        )
        active_logs.pop(user.id, None)
        return

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
            await channel.send(f"⏰ Timed out. Run `{log_cmd}` to start again.")
            return None

    async def wait_for_view(view, prompt_msg):
        """Wait for any view (NameEntryView, YesNoLogView, etc). Returns False if cancelled/timed out."""
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
        if not getattr(view, "confirmed", True):
            try:
                await prompt_msg.delete()
            except discord.HTTPException:
                pass
            await channel.send(f"⏰ Timed out. Run `{log_cmd}` to start again.")
            return False
        return True

    try:
        total_steps = len(questions) + 1  # +1 for the always-required date
        await channel.send(
            f"📋 **{event_label} Log** — started by {user.mention}\n"
            f"*{total_steps} step(s) total. Use `/cancel` at any time to stop.*"
        )

        # ── Step 1: Date (always asked, never configurable) ──────────────────
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
                await channel.send(
                    f"⚠️ Could not parse `{raw_date}` as a date. "
                    f"Run `{log_cmd}` to start again."
                )
                return
            log_date = parsed_d

        # ── Roster (lazy — only loaded if any question needs it) ─────────────
        roster_loaded = False
        names: list[str] = []
        alias_map: dict[str, str] = {}

        async def _ensure_roster():
            nonlocal roster_loaded, names, alias_map
            if roster_loaded:
                return
            loading_msg = await channel.send("⏳ Loading roster from your configured tab…")
            names, alias_map = await asyncio.get_event_loop().run_in_executor(
                None, load_roster_from_config, guild_id, event_type,
            )
            try:
                await loading_msg.delete()
            except discord.HTTPException:
                pass
            roster_loaded = True

        # ── Walk through configured questions ────────────────────────────────
        answers: dict[str, str] = {}
        for idx, q in enumerate(questions, start=2):
            qkey   = q.get("key", f"q{idx}")
            qlabel = q.get("label", qkey)
            qtype  = q.get("type", "text")

            header = f"**Step {idx} of {total_steps} — {qlabel}**"

            if qtype == "yes_no":
                yn = _YesNoLogView()
                msg = await channel.send(f"{header}\nPick one.", view=yn)
                if not await wait_for_view(yn, msg):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                answers[qkey] = "Yes" if yn.value else "No"

            elif qtype == "numeric":
                lo = q.get("min")
                hi = q.get("max")
                bound_hint = ""
                if lo is not None or hi is not None:
                    bits = []
                    if lo is not None: bits.append(f"min `{lo}`")
                    if hi is not None: bits.append(f"max `{hi}`")
                    bound_hint = f" *({', '.join(bits)})*"
                attempts = 5
                value: str | None = None
                while attempts > 0:
                    raw = await wait_for_msg(f"{header}{bound_hint}\nType a number.")
                    if raw is None:
                        if cancel_event.is_set():
                            await channel.send("❌ Log cancelled.")
                        return
                    try:
                        n = float(raw) if "." in raw else int(raw)
                    except ValueError:
                        attempts -= 1
                        await channel.send(
                            f"⚠️ `{raw}` isn't a number. Please re-enter your answer."
                        )
                        continue
                    if lo is not None and n < lo:
                        attempts -= 1
                        await channel.send(f"⚠️ Must be at least **{lo}**. Please re-enter.")
                        continue
                    if hi is not None and n > hi:
                        attempts -= 1
                        await channel.send(f"⚠️ Must be at most **{hi}**. Please re-enter.")
                        continue
                    value = str(n)
                    break
                if value is None:
                    await channel.send(
                        "⚠️ Too many invalid attempts. Cancelling the log — "
                        f"run `{log_cmd}` when you're ready to try again."
                    )
                    return
                answers[qkey] = value

            elif qtype == "roster_names":
                await _ensure_roster()
                if not names:
                    await channel.send(
                        "⚠️ The configured roster tab is empty or unreachable. "
                        f"Run `{log_cmd.replace('_participation', '').replace('/', '/setup')}` "
                        f"to update the roster source, then try again."
                    )
                    return
                preview = ", ".join(names) if len(names) <= 25 else f"{len(names)} members loaded"
                view = NameEntryView(names, qlabel, alias_map)
                prompt = await channel.send(
                    f"{header}\nPress **Enter Names** to type who applies. "
                    f"Press **Skip** if none.\n*Roster: {preview}*",
                    view=view,
                )
                if not await wait_for_view(view, prompt):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                picked = sorted(view.selected)
                if view.unrecognized:
                    picked += sorted(view.unrecognized)
                answers[qkey] = ", ".join(picked)

            elif qtype == "single_select":
                opts = q.get("options") or []
                if not opts:
                    answers[qkey] = ""
                    continue
                view = ShortSelectView(opts, qlabel)
                prompt = await channel.send(f"{header}\nPick one.", view=view)
                if not await wait_for_view(view, prompt):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                answers[qkey] = next(iter(view.selected), "")

            elif qtype == "multi_select":
                opts = q.get("options") or []
                if not opts:
                    answers[qkey] = ""
                    continue
                view = ShortSelectView(opts, qlabel)
                prompt = await channel.send(f"{header}\nPick any that apply.", view=view)
                if not await wait_for_view(view, prompt):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                answers[qkey] = ", ".join(sorted(view.selected))

            elif qtype == "date":
                fmt = q.get("date_format") or "%m/%d/%Y"
                attempts = 5
                value = None
                while attempts > 0:
                    raw = await wait_for_msg(f"{header} *(format `{fmt}`)*")
                    if raw is None:
                        if cancel_event.is_set():
                            await channel.send("❌ Log cancelled.")
                        return
                    try:
                        from datetime import datetime as _dt
                        d = _dt.strptime(raw, fmt).date()
                        value = d.isoformat()
                        break
                    except ValueError:
                        attempts -= 1
                        await channel.send(
                            f"⚠️ `{raw}` doesn't match `{fmt}`. Please re-enter."
                        )
                if value is None:
                    await channel.send(
                        "⚠️ Too many invalid attempts. Cancelling the log."
                    )
                    return
                answers[qkey] = value

            else:  # "text" or unknown — fall back to free text
                raw = await wait_for_msg(
                    f"{header}\nType your answer (or `skip` for none)."
                )
                if raw is None:
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                answers[qkey] = "" if raw.lower() == "skip" else raw

        # ── Save row ─────────────────────────────────────────────────────────
        await channel.send("💾 Saving log…")
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, append_participation_row,
                guild_id, event_type, log_date, answers,
            )
        except Exception as e:
            await channel.send(f"⚠️ Error saving to sheet: {e}")
            return

        # ── Summary ──────────────────────────────────────────────────────────
        date_str = f"{log_date:%A, %B} {log_date.day}, {log_date.year}"
        lines = [f"📋 **{event_label} Log — {date_str}**"]
        for q in questions:
            qkey   = q.get("key", "")
            qlabel = q.get("label", qkey)
            v      = answers.get(qkey, "")
            lines.append(f"**{qlabel}:** {v if v not in ('', None) else 'None'}")
        summary = "\n".join(lines)

        await channel.send(f"✅ **Log saved!**\n\n{summary}")

        # Mirror the summary into the configured log channel (if different).
        try:
            log_channel_id = int(pcfg.get("log_channel_id") or 0)
            if log_channel_id and channel.id != log_channel_id:
                target = bot.get_channel(log_channel_id)
                if target:
                    await target.send(summary)
        except Exception as e:
            print(f"[LOG] Error mirroring summary to log channel: {e}")

    finally:
        active_logs.pop(user.id, None)


class _YesNoLogView(discord.ui.View):
    """Simple Yes/No picker for participation `yes_no` questions."""

    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = False
        self.value: bool | None = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, inter: discord.Interaction, button: discord.ui.Button):
        self.value     = True
        self.confirmed = True
        for c in self.children: c.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, inter: discord.Interaction, button: discord.ui.Button):
        self.value     = False
        self.confirmed = True
        for c in self.children: c.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


# ── Log lookup ─────────────────────────────────────────────────────────────────

def list_recent_log_dates(event_type: str, n: int, guild_id=None) -> list[date]:
    """
    Return up to `n` most-recent log dates for `event_type`, sorted newest-first.
    Used to gate free-tier access (only the N most recent entries are visible).
    """
    out: list[date] = []
    try:
        ws   = _get_log_sheet(guild_id, event_type=event_type)
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
    Returns a dict shaped like `{"date": ..., "event": ..., "fields":
    [(label, value), ...]}` so /[event]_log can format it generically
    against whatever questions the alliance configured. Falls back to
    the legacy column shape (Vote Count / RTF / Sitting Out / Prior)
    when a guild is still on the old "DS-CS Sit-outs" tab.
    """
    try:
        ws   = _get_log_sheet(guild_id, event_type=event_type)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return None
        header_row = rows[0]
        # Resolve column labels: skip Date and Event, use the remaining
        # header cells as the field labels. Empty header cells fall back
        # to a generic name so we don't crash on malformed sheets.
        field_labels = [(h.strip() or f"Column {i+1}") for i, h in enumerate(header_row[2:])]

        from datetime import datetime
        for row in reversed(rows[1:]):
            if len(row) < 2:
                continue
            if row[1].strip().upper() != event_type.upper():
                continue
            row_date = row[0].strip()
            for fmt in ("%-m/%-d/%Y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(row_date, fmt).date()
                except ValueError:
                    continue
                if parsed != log_date:
                    break
                fields: list[tuple[str, str]] = []
                for i, label in enumerate(field_labels):
                    fields.append((label, row[i + 2] if len(row) > i + 2 else ""))
                return {
                    "date":   row[0] if len(row) > 0 else "",
                    "event":  row[1] if len(row) > 1 else "",
                    "fields": fields,
                    # Legacy aliases for callers that haven't migrated yet:
                    "vote_count":       row[2] if len(row) > 2 else "",
                    "rtf_no_vote":      row[3] if len(row) > 3 else "",
                    "sitting_out":      row[4] if len(row) > 4 else "",
                    "prior_no_request": row[5] if len(row) > 5 else "",
                }
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

    @app_commands.command(
        name="desertstorm_remind",
        description="💎 DM every roster member to participate in this week's Desert Storm",
    )
    async def desertstorm_remind(self, interaction: discord.Interaction):
        await _send_storm_reminder(self.bot, interaction, "DS")

    @app_commands.command(
        name="canyonstorm_remind",
        description="💎 DM every roster member to participate in this week's Canyon Storm",
    )
    async def canyonstorm_remind(self, interaction: discord.Interaction):
        await _send_storm_reminder(self.bot, interaction, "CS")


async def _send_storm_reminder(bot, interaction: discord.Interaction, event_type: str):
    """DM every roster member with a participation reminder for the given storm."""
    if not await _guard(interaction):
        return

    import premium
    import dm
    from config import get_member_roster_config, get_member_roster_sheet

    if not await premium.is_premium(
        interaction.guild_id, interaction=interaction, bot=bot,
    ):
        await interaction.response.send_message(
            embed=premium.premium_locked_embed(
                feature_label="Storm participation DMs",
                description=(
                    "Storm participation reminders are part of Alliance Helper "
                    "Premium and require Member Roster Sync (`/setup_members`). "
                    "Run `/upgrade` to unlock."
                ),
            ),
            view=premium.upgrade_view(),
            ephemeral=True,
        )
        return

    roster_cfg = get_member_roster_config(interaction.guild_id)
    if not roster_cfg.get("enabled"):
        await interaction.response.send_message(
            "⚙️ Member Roster Sync isn't configured yet. Run `/setup_members` first.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    try:
        ws   = get_member_roster_sheet(interaction.guild_id, roster_cfg["tab_name"])
        rows = await asyncio.get_event_loop().run_in_executor(
            None, ws.get_all_values,
        )
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Could not read the roster sheet: {e}", ephemeral=True,
        )
        return

    did_col = roster_cfg["discord_id_col"]
    sent    = 0
    skipped = 0
    for row in rows[1:]:
        if did_col >= len(row):
            continue
        did = row[did_col].strip()
        if not did:
            skipped += 1
            continue
        ok = await dm.send_dm_to_id(
            bot, interaction.guild_id, did,
            content=(
                f"⚔️ **{label} reminder** — your alliance is preparing for this week's "
                f"{label}. Please confirm your participation in Discord and check the "
                f"team channel for your zone assignment. Good luck out there!"
            ),
        )
        if ok:
            sent += 1
        else:
            skipped += 1

    await interaction.followup.send(
        f"✅ Sent {sent} **{label}** reminder DM{'s' if sent != 1 else ''}. "
        f"{skipped} skipped.",
        ephemeral=True,
    )


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
            embed = discord.Embed(
                title=f"📊 {event_label} log lookback — Free tier limit",
                description=(
                    f"You can only see the **{recent_cap} most recent** log "
                    f"entries with the free tier. Upgrade to "
                    f"{premium.PREMIUM_BRAND} to unlock unlimited lookback."
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(
                embed=embed, view=premium.upgrade_view(), ephemeral=True,
            )
            return

    entry = await asyncio.get_event_loop().run_in_executor(
        None, lookup_log_entry, event, parsed_d, interaction.guild_id,
    )

    if entry is None:
        await interaction.followup.send(
            f"❌ No **{event_label}** log found for **{parsed_d:%B} {parsed_d.day}, {parsed_d.year}**.",
            ephemeral=True,
        )
        return

    date_str = f"{parsed_d:%A, %B} {parsed_d.day}, {parsed_d.year}"
    lines    = [f"📋 **{event_label} Log — {date_str}**"]
    # Prefer the generic `fields` list (set by the new participation flow);
    # fall back to the legacy DS/CS column shape so pre-rework data still
    # renders nicely.
    fields = entry.get("fields") or []
    if fields:
        for label_, value in fields:
            lines.append(f"**{label_}:** {value or 'None'}")
    else:
        action_label = "Vote" if event == "DS" else "Request"
        if event == "DS":
            lines.append(f"**Votes:** {entry.get('vote_count') or 'Not recorded'}")
            lines.append(f"**RTF No Vote:** {entry.get('rtf_no_vote') or 'None'}")
        lines.append(f"**Sitting Out:** {entry.get('sitting_out') or 'None'}")
        lines.append(f"**Prior Sit-Out No {action_label}:** {entry.get('prior_no_request') or 'None'}")

    await interaction.followup.send("\n".join(lines))


async def setup(bot: commands.Bot):
    await bot.add_cog(LogCog(bot))

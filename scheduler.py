"""
scheduler.py — Event reminder scheduler with leadership approval workflow

Schedule logic:
  - Events run on a 3-day cooldown anchored to 2026-03-30
  - Normal run: Marauder 10:15pm ET, Siege 10:45pm ET
  - Friday exception: shifted to Saturday at 5:00pm ET / 5:30pm ET
  - Server time = ET + 2 hours (fixed UTC-2 offset)

Announcement flow:
  - Noon on event day → leadership sees the event list editor
  - Leadership can add/edit/remove events and times, add optional notes
  - Build Announcement → crafts the message from the event list
  - Approval flow → Send As-Is or Edit & Send (shows current text for easy copying)
  - On approval → posts to Announcements with @OGV tag, stamps leadership channel
  - 5-minute warning auto-fires based on first event's time
  - Friday 9:55pm ET → shield reminder through same approval flow
"""

import asyncio
import re
from copy import deepcopy
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import discord
import discord.ext.commands
from config import get_config, init_db

# ── Channel IDs ────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

from config import get_config

# ── Per-guild config helpers ───────────────────────────────────────────────────

def get_guild_cfg(guild_id: int):
    from config import get_config
    return get_config(guild_id)

BUTTON_TIMEOUT = 3600

# ── Pending 5-minute warnings ──────────────────────────────────────────────────
pending_warnings: dict[str, datetime] = {}


# ── Event library ──────────────────────────────────────────────────────────────
# Each event has:
#   key      — internal identifier
#   name     — display name
#   blurb    — announcement text, use {time} and {server} as placeholders
#   optional — if True, not included by default (can be added via dropdown)

EVENT_LIBRARY = {
    "marauder": {
        "name":     "Plague Marauder",
        "blurb":    "Marauder (AE) at {time} ({server} server). Make sure to have offline participation checked!",
        "optional": False,
    },
    "siege": {
        "name":     "Zombie Siege",
        "blurb":    "Zombies at {time} ({server} server). Be sure you have squads on your wall!",
        "optional": False,
    },
    # Other in-game events (Glacieradon, Blimp, etc.) are intentionally NOT
    # listed here — alliance leaders add them as custom events through
    # /setup_events with the in-game name they prefer. Once we can confirm
    # the official in-game names for additional events, they may be added
    # back to this default library.
}

OPTIONAL_EVENTS = {k: v for k, v in EVENT_LIBRARY.items() if v["optional"]}


def _resolve_event_info(key: str, guild_id: int = None) -> dict:
    """
    Resolve an event key to its display info. When a guild has configured
    events via /setup_events, those take precedence; otherwise fall back to
    the hardcoded EVENT_LIBRARY for backwards compatibility.

    Returns {"name", "blurb", "optional"}. Guild-configured events are
    always treated as optional (i.e. removable from the editor) — only the
    legacy EVENT_LIBRARY enforces the marauder/siege "always included" rule.
    """
    if guild_id is not None:
        try:
            from config import get_guild_event
            ev = get_guild_event(guild_id, key)
        except Exception:
            ev = None
        if ev:
            return {
                "name":     ev.get("name", key),
                "blurb":    ev.get("announcement_blurb", "")
                            or EVENT_LIBRARY.get(key, {}).get("blurb", ""),
                "optional": True,
            }
    return EVENT_LIBRARY.get(key, {"name": key, "blurb": "", "optional": True})


def _available_events_for_guild(guild_id: int = None) -> dict:
    """
    Pool of optional/addable events for the editor. With a configured
    guild, returns every guild-defined event keyed by short_key. Without
    one, returns the legacy OPTIONAL_EVENTS dict.
    """
    if guild_id is not None:
        try:
            from config import get_guild_events
            events = get_guild_events(guild_id, active_only=True)
        except Exception:
            events = []
        if events:
            return {
                e["short_key"]: {
                    "name":     e.get("name", e["short_key"]),
                    "blurb":    e.get("announcement_blurb", ""),
                    "optional": True,
                }
                for e in events
            }
    return dict(OPTIONAL_EVENTS)


# ── Schedule helpers ───────────────────────────────────────────────────────────

def next_event_dates(from_date: date, count: int, anchor: date, cycle: int) -> list[date]:
    """Compute the next `count` occurrences of a repeating event on a fixed
    cycle, on or after `from_date`. Anchor + cycle define when the event
    fires; the result starts from the first cycle date >= from_date.

    All four args are required — historically the function was called with
    no args and fell back to hardcoded OGV values. Per-guild callers now
    look up their anchor + cycle from the `guild_events` table and pass
    them in explicitly.
    """
    days_since = (from_date - anchor).days
    remainder  = days_since % cycle
    offset     = 0 if remainder == 0 else cycle - remainder
    results    = []
    candidate  = from_date + timedelta(days=offset)
    while len(results) < count:
        results.append(candidate)
        candidate += timedelta(days=cycle)
    return results


def is_friday(d: date) -> bool:
    return d.weekday() == 4


def to_server_time_str(et_dt: datetime) -> str:
    return (et_dt + timedelta(hours=2)).strftime("%H:%M")


def format_et(dt: datetime) -> str:
    hour12 = dt.hour % 12 or 12
    return f"{hour12}:{dt:%M%p}".lower()


def __noon_dt_for(event_date: date) -> datetime:
    run_date = event_date + timedelta(days=1) if is_friday(event_date) else event_date
    return datetime(run_date.year, run_date.month, run_date.day, 12, 0, tzinfo=ET)


def make_et_datetime(run_date: date, hour: int, minute: int) -> datetime:
    return datetime(run_date.year, run_date.month, run_date.day, hour, minute, tzinfo=ET)


# ── Event list helpers ─────────────────────────────────────────────────────────
# An "event list" is a list of dicts:
# [{ "key": "marauder", "name": "...", "dt": datetime, "blurb": "..." }, ...]
# Built per-guild from rows in `guild_events`. See bot.py /events command
# and the scheduler main loop for the construction sites.


def build_announcement(event_list: list[dict], notes: str = "", role_mention: str = "@everyone") -> str:
    """
    Craft the full announcement message from the event list.
    Uses blurb from the event dict if present (database-driven),
    falls back to EVENT_LIBRARY for backward compatibility.
    Placeholders: {time} = local time, {server_time} = UTC/Server Time
    """
    bullet_lines = []
    for event in event_list:
        key    = event["key"]
        dt     = event["dt"]
        et_str = format_et(dt)
        sv_str = to_server_time_str(dt)

        # Use stored blurb if available, otherwise fall back to EVENT_LIBRARY
        blurb = event.get("blurb") or ""
        if not blurb:
            lib   = EVENT_LIBRARY.get(key, {})
            blurb = lib.get("blurb", f"{key} at {{time}} ({{server_time}} Server Time).")

        bullet_lines.append("- " + blurb.format(time=et_str, server_time=sv_str, server=sv_str))

    lines = [
        f"Hey {role_mention}!",
        "Here is the schedule for events today:",
        "",
    ] + bullet_lines

    if notes and notes.strip():
        lines += ["", notes.strip()]

    return "\n".join(lines)


def build_warning_message(event_list: list[dict], guild_id: int = None) -> str:
    """
    Build the 5-minute warning based on the first event.

    Resolution order for the message body:
      1. The event's stored `warning_blurb` (if guild defined a custom 5-min
         warning text in /setup_events).
      2. The event's stored `announcement_blurb` (if defined) — adapted to
         "in 5 minutes" by substituting the {time} placeholder.
      3. Hardcoded special case for `marauder` (legacy compat).
      4. Generic fallback: "<Name> in 5 minutes!" using the configured name.
    """
    if not event_list:
        return "Event starting in 5 minutes! Make sure you're online!"
    first = event_list[0]
    key   = first["key"]

    info        = _resolve_event_info(key, guild_id)
    custom_warn = (first.get("warning_blurb") or "").strip()
    if custom_warn:
        return custom_warn.format(time="5 minutes", server_time="5 minutes", server="5 minutes")

    custom_blurb = (first.get("blurb") or info.get("blurb") or "").strip()
    if custom_blurb and key not in ("marauder",):
        # Re-use the configured announcement blurb, swapping the time
        # placeholder for "5 minutes" so the message reads "<event> in 5 minutes...".
        try:
            return custom_blurb.format(time="5 minutes", server_time="5 minutes", server="5 minutes")
        except (KeyError, IndexError):
            pass

    if key == "marauder":
        return (
            "Marauder (AE) in 5 minutes! Make sure you hop online and get your points! "
            "Zombies right after, check your wall to make sure you have squads on it!"
        )
    name = info.get("name") or key
    return f"{name} in 5 minutes! Make sure you're online!"


SHIELD_REMINDER = "Buster day reminder - log in and shield up if you aren't going hunting!"


# ── Time parsing ───────────────────────────────────────────────────────────────

def parse_time_str(text: str) -> tuple[int, int] | None:
    """Parse a time string like '10:15pm', '5pm', '17:00' into (hour, minute)."""
    # 12-hour format
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.IGNORECASE)
    if match:
        hour   = int(match.group(1))
        minute = int(match.group(2)) if match.group(2) else 0
        ampm   = match.group(3).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return hour, minute
    # 24-hour format
    match = re.search(r"(\d{1,2}):(\d{2})", text)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def first_event_warning_dt(event_list: list[dict]) -> datetime | None:
    if not event_list:
        return None
    return event_list[0]["dt"] - timedelta(minutes=5)


# ── Event editor UI ────────────────────────────────────────────────────────────

class EventEditorView(discord.ui.View):
    """
    Interactive event list editor. Shows the current event list and lets
    leadership add, edit times, or remove optional events before building
    the announcement.
    """

    def __init__(self, bot, event_list: list[dict], event_key: str, run_date: date, guild_id: int = None):
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.bot        = bot
        self.event_list = deepcopy(event_list)
        self.event_key  = event_key
        self.run_date   = run_date
        self.notes      = ""
        self.guild_id   = guild_id

    def format_event_list_text(self) -> str:
        lines = []
        for i, event in enumerate(self.event_list, 1):
            info = _resolve_event_info(event["key"], self.guild_id)
            name = info["name"]
            t    = format_et(event["dt"])
            sv   = to_server_time_str(event["dt"])
            lines.append(f"{i}. **{name}** — {t} ET ({sv} server)")
        return "\n".join(lines) if lines else "*No events set*"

    async def refresh(self, interaction: discord.Interaction):
        """Update the editor message with the current event list."""
        content = (
            f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
            f"**Current events:**\n{self.format_event_list_text()}\n\n"
            f"**Announcement text:** {self.notes if self.notes else '*None*'}"
        )
        await interaction.message.edit(content=content, view=self)

    async def on_timeout(self):
        """Disable buttons when the view expires."""
        try:
            # We don't have the message reference here, so just stop cleanly
            pass
        except Exception:
            pass

    @discord.ui.button(label="➕ Add Event", style=discord.ButtonStyle.primary, row=0)
    async def add_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only show events not already in the list
        current_keys = {e["key"] for e in self.event_list}
        pool         = _available_events_for_guild(self.guild_id)
        available    = {k: v for k, v in pool.items() if k not in current_keys}

        if not available:
            await interaction.response.send_message(
                "All available events are already in the list.", ephemeral=True
            )
            return

        select = discord.ui.Select(
            placeholder="Choose an event to add...",
            options=[
                discord.SelectOption(label=v["name"], value=k)
                for k, v in available.items()
            ],
        )

        async def on_select(select_interaction: discord.Interaction):
            chosen_key = select_interaction.data["values"][0]
            await select_interaction.response.defer()
            select_msg_ref[0].stop()

            chosen_info = _resolve_event_info(chosen_key, self.guild_id)
            chosen_name = chosen_info["name"]

            # Ask for the time
            channel = interaction.channel
            time_prompt = await channel.send(
                f"⏰ What time is **{chosen_name}**? *(e.g. 10:30pm or 22:30)*"
            )

            def check(m):
                return m.author == interaction.user and m.channel == channel

            try:
                reply = await self.bot.wait_for("message", check=check, timeout=120)
                parsed = parse_time_str(reply.content)
                try:
                    await time_prompt.delete()
                    await reply.delete()
                except discord.HTTPException:
                    pass

                if parsed:
                    h, m = parsed
                    dt = make_et_datetime(self.run_date, h, m)
                    self.event_list.append({"key": chosen_key, "dt": dt})
                    # Keep list sorted by time
                    self.event_list.sort(key=lambda e: e["dt"])
                    await channel.send(
                        f"✅ **{chosen_name}** added at {format_et(dt)} ET.",
                        delete_after=5,
                    )
                else:
                    await channel.send("⚠️ Could not parse that time. Try again with Add Event.", delete_after=8)

            except asyncio.TimeoutError:
                await channel.send("⏰ Timed out waiting for time input.", delete_after=8)

            # Refresh the editor
            await interaction.message.edit(
                content=(
                    f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
                    f"**Current events:**\n{self.format_event_list_text()}\n\n"
                    f"**Announcement text:** {self.notes if self.notes else '*None*'}"
                ),
                view=self,
            )

        select.callback = on_select
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        select_msg_ref = [view]
        await interaction.response.send_message("Select an event to add:", view=view, ephemeral=True)

    @discord.ui.button(label="✏️ Edit Time", style=discord.ButtonStyle.secondary, row=0)
    async def edit_time(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.event_list:
            await interaction.response.send_message("No events to edit.", ephemeral=True)
            return

        select = discord.ui.Select(
            placeholder="Choose an event to edit...",
            options=[
                discord.SelectOption(
                    label=f"{_resolve_event_info(e['key'], self.guild_id)['name']} — {format_et(e['dt'])} ET",
                    value=str(i),
                )
                for i, e in enumerate(self.event_list)
            ],
        )

        async def on_select(select_interaction: discord.Interaction):
            idx = int(select_interaction.data["values"][0])
            await select_interaction.response.defer()
            select_msg_ref[0].stop()

            event    = self.event_list[idx]
            lib_name = _resolve_event_info(event["key"], self.guild_id)["name"]
            channel  = interaction.channel

            time_prompt = await channel.send(
                f"⏰ New time for **{lib_name}**? *(e.g. 10:30pm or 22:30)*"
            )

            def check(m):
                return m.author == interaction.user and m.channel == channel

            try:
                reply  = await self.bot.wait_for("message", check=check, timeout=120)
                parsed = parse_time_str(reply.content)
                try:
                    await time_prompt.delete()
                    await reply.delete()
                except discord.HTTPException:
                    pass

                if parsed:
                    h, m = parsed
                    self.event_list[idx]["dt"] = make_et_datetime(self.run_date, h, m)
                    self.event_list.sort(key=lambda e: e["dt"])
                    await channel.send(
                        f"✅ **{lib_name}** updated to {format_et(self.event_list[idx]['dt'])} ET.",
                        delete_after=5,
                    )
                else:
                    await channel.send("⚠️ Could not parse that time.", delete_after=8)

            except asyncio.TimeoutError:
                await channel.send("⏰ Timed out.", delete_after=8)

            await interaction.message.edit(
                content=(
                    f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
                    f"**Current events:**\n{self.format_event_list_text()}\n\n"
                    f"**Announcement text:** {self.notes if self.notes else '*None*'}"
                ),
                view=self,
            )

        select.callback = on_select
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        select_msg_ref = [view]
        await interaction.response.send_message("Choose an event to edit:", view=view, ephemeral=True)

    @discord.ui.button(label="🗑️ Remove Event", style=discord.ButtonStyle.danger, row=0)
    async def remove_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Any event in the current draft can be removed — even ones on a
        # repeating schedule. Removal only affects this one announcement,
        # not the schedule itself, which gives leadership flexibility to
        # skip an event for a specific day or push it to a different time.
        removable = list(enumerate(self.event_list))
        if not removable:
            await interaction.response.send_message(
                "No events to remove.",
                ephemeral=True,
            )
            return

        select = discord.ui.Select(
            placeholder="Choose an event to remove...",
            options=[
                discord.SelectOption(
                    label=_resolve_event_info(e["key"], self.guild_id)["name"],
                    value=str(i),
                )
                for i, e in removable
            ],
        )

        async def on_select(select_interaction: discord.Interaction):
            idx      = int(select_interaction.data["values"][0])
            lib_name = _resolve_event_info(self.event_list[idx]["key"], self.guild_id)["name"]
            self.event_list.pop(idx)
            await select_interaction.response.edit_message(
                content=f"✅ **{lib_name}** removed.", view=None
            )
            await interaction.message.edit(
                content=(
                    f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
                    f"**Current events:**\n{self.format_event_list_text()}\n\n"
                    f"**Announcement text:** {self.notes if self.notes else '*None*'}"
                ),
                view=self,
            )

        select.callback = on_select
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message("Choose an event to remove:", view=view, ephemeral=True)

    @discord.ui.button(label="📝 Add Announcement Text", style=discord.ButtonStyle.secondary, row=1)
    async def add_notes(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        await interaction.response.defer()

        current_note = f"\n\nCurrent announcement text:\n> {self.notes}" if self.notes else ""
        prompt = await channel.send(
            f"📝 {interaction.user.mention} — type the additional announcement text "
            f"that should be appended to today's announcement, or type `clear` to remove "
            f"existing text.{current_note}"
        )

        def check(m):
            return m.author == interaction.user and m.channel == channel

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=300)
            try:
                await prompt.delete()
                await reply.delete()
            except discord.HTTPException:
                pass

            if reply.content.strip().lower() == "clear":
                self.notes = ""
                await channel.send("✅ Announcement text cleared.", delete_after=5)
            else:
                self.notes = reply.content.strip()
                await channel.send("✅ Announcement text saved.", delete_after=5)

        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out.", delete_after=8)

        await interaction.message.edit(
            content=(
                f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
                f"**Current events:**\n{self.format_event_list_text()}\n\n"
                f"**Announcement text:** {self.notes if self.notes else '*None*'}"
            ),
            view=self,
        )

    @discord.ui.button(label="📣 Build Announcement", style=discord.ButtonStyle.success, row=1)
    async def build_announcement_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        if not self.event_list:
            await interaction.followup.send(
                "⚠️ No events in the list. Use `/events` to open a fresh editor.",
                ephemeral=True,
            )
            return

        try:
            # Disable all buttons
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass

        try:
            from config import get_config
            cfg = get_config(self.guild_id)
            role_mention = cfg.role_mention if cfg else "@everyone"
            announcement = build_announcement(self.event_list, self.notes, role_mention=role_mention)
        except Exception as e:
            print(f"[SCHEDULER] Error building announcement: {e}")
            await interaction.followup.send(f"⚠️ Error building announcement: {e}", ephemeral=True)
            return

        view = ApprovalView(
            bot=self.bot,
            draft_message=announcement,
            event_list=self.event_list,
            event_key=self.event_key,
            is_shield=False,
            guild_id=self.guild_id,
        )

        cfg = get_config(self.guild_id)
        channel = self.bot.get_channel(cfg.leadership_channel_id) if cfg else None
        if channel:
            await channel.send(
                f"📣 **Announcement draft — please review and approve:**\n\n{announcement}",
                view=view,
            )
        else:
            await interaction.followup.send("⚠️ Could not find the leadership channel.", ephemeral=True)

        self.stop()


# ── Approval UI ────────────────────────────────────────────────────────────────

class ApprovalView(discord.ui.View):
    def __init__(self, bot, draft_message: str, event_key: str,
                 event_list: list[dict] = None, is_shield: bool = False, guild_id: int = None):
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.bot           = bot
        self.draft_message = draft_message
        self.event_key     = event_key
        self.event_list    = event_list or []
        self.is_shield     = is_shield
        self.guild_id      = guild_id
        # guild_id will be set by the caller

    async def _post_to_announcements(self, message: str):
        from config import get_config
        cfg = get_config(self.guild_id)
        channel = self.bot.get_channel(cfg.announcement_channel_id) if cfg else None
        if channel is None:
            print("[SCHEDULER][ERROR] Announcements channel not found")
            return

        await channel.send(message)

        # Schedule 5-minute warning based on first event time
        if not self.is_shield and self.event_list:
            warn_dt = first_event_warning_dt(self.event_list)
            if warn_dt:
                pending_warnings[self.event_key] = (warn_dt, self.event_list, self.guild_id)
                print(f"[SCHEDULER] 5-min warning scheduled for {warn_dt.strftime('%Y-%m-%d %H:%M %Z')}")

    async def _disable_buttons(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="✅ Send As-Is", style=discord.ButtonStyle.success)
    async def send_as_is(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable_buttons(interaction)
        await self._post_to_announcements(self.draft_message)

        from config import get_config
        cfg = get_config(self.guild_id)
        leadership = self.bot.get_channel(cfg.leadership_channel_id) if cfg else None
        if leadership:
            _now = datetime.now(tz=ET)
            _h12 = _now.hour % 12 or 12
            _ts  = f"{_h12}:{_now:%M%p ET}".lower()
            await leadership.send(
                f"✅ **Approved by {interaction.user.display_name} at {_ts}**\n"
                f"```\n{self.draft_message}\n```"
            )
        self.stop()

    @discord.ui.button(label="✏️ Edit & Send", style=discord.ButtonStyle.primary)
    async def edit_and_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable_buttons(interaction)

        from config import get_config
        cfg = get_config(self.guild_id)
        channel = self.bot.get_channel(cfg.leadership_channel_id) if cfg else None
        if channel is None:
            return

        # Post the current text as a quoted block for easy copying
        prompt = await channel.send(
            f"✏️ {interaction.user.mention} — copy and edit the message below, then send your revised version:\n\n"
            f"```\n{self.draft_message}\n```"
        )

        def check(m):
            return m.author == interaction.user and m.channel.id == (cfg.leadership_channel_id if cfg else 0)

        try:
            reply        = await self.bot.wait_for("message", check=check, timeout=300)
            revised_text = reply.content

            try:
                await prompt.delete()
                await reply.delete()
            except discord.HTTPException:
                pass

            new_view = ApprovalView(
                bot=self.bot,
                draft_message=revised_text,
                event_key=self.event_key,
                event_list=self.event_list,
                is_shield=self.is_shield,
                guild_id=self.guild_id,
            )
            await channel.send(
                f"📝 **Revised draft** (edited by {interaction.user.display_name}):\n\n{revised_text}",
                view=new_view,
            )

        except asyncio.TimeoutError:
            await channel.send(
                f"⏰ Edit timed out — no message received from {interaction.user.mention} within 5 minutes."
            )

        self.stop()

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


# ── Main scheduler loop ────────────────────────────────────────────────────────

async def run_scheduler(bot: discord.ext.commands.Bot):
    await bot.wait_until_ready()
    print("[SCHEDULER] Started.")

    while not bot.is_closed():
        now   = datetime.now(tz=ET)
        today = now.date()

        triggers = []

        # Build triggers for every configured guild
        import sqlite3
        from config import DB_PATH
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM guild_configs WHERE setup_complete = 1"
            ).fetchall()

        for row in rows:
            from config import GuildConfig, get_guild_events
            cfg    = GuildConfig(**dict(row))
            events = get_guild_events(cfg.guild_id, active_only=True)

            if not events:
                continue

            # Group events that share the same anchor/interval — same in-game day = same draft
            # Key: (anchor_date, interval_days) → list of event configs
            from collections import defaultdict
            groups = defaultdict(list)
            manual_events = []
            for ev in events:
                if ev["schedule_type"] == "repeating" and ev["anchor_date"]:
                    groups[(ev["anchor_date"], ev["interval_days"])].append(ev)
                else:
                    manual_events.append(ev)

            for (anchor_str, interval), group_events in groups.items():
                try:
                    from datetime import date as _date
                    anchor = _date.fromisoformat(anchor_str)
                except ValueError:
                    continue

                event_dates = next_event_dates(from_date=today, count=4, anchor=anchor, cycle=interval)

                for event_date in event_dates:
                    # Build event list for this date from all events in the group
                    event_list = []
                    draft_channel_id     = 0
                    announcement_chan_id = 0
                    draft_h, draft_m     = 12, 0
                    five_min_warn        = False
                    tz_str               = "America/New_York"

                    for ev in group_events:
                        try:
                            from zoneinfo import ZoneInfo as _ZI
                            ev_tz   = _ZI(ev["timezone"])
                            t_h, t_m = int(ev["default_time"].split(":")[0]), int(ev["default_time"].split(":")[1])
                            ev_dt   = datetime(event_date.year, event_date.month, event_date.day, t_h, t_m, tzinfo=ev_tz)
                            event_list.append({
                                "key":   ev["short_key"],
                                "name":  ev["name"],
                                "dt":    ev_dt,
                                "blurb": ev["announcement_blurb"],
                            })
                            draft_channel_id     = ev["draft_channel_id"] or draft_channel_id
                            announcement_chan_id = ev["announcement_channel_id"] or announcement_chan_id
                            if ev["draft_time"]:
                                draft_h = int(ev["draft_time"].split(":")[0])
                                draft_m = int(ev["draft_time"].split(":")[1])
                            if ev["five_min_warning"]:
                                five_min_warn = True
                            tz_str = ev["timezone"]
                        except Exception as e:
                            print(f"[SCHEDULER] Error processing event {ev['short_key']}: {e}")

                    if not event_list:
                        continue

                    event_list.sort(key=lambda x: x["dt"])

                    try:
                        from zoneinfo import ZoneInfo as _ZI2
                        ev_tz2    = _ZI2(tz_str)
                        draft_dt  = datetime(event_date.year, event_date.month, event_date.day, draft_h, draft_m, tzinfo=ev_tz2)
                    except Exception:
                        draft_dt = datetime(event_date.year, event_date.month, event_date.day, 12, 0, tzinfo=ET)

                    event_key = f"event-{cfg.guild_id}-{event_date.isoformat()}"

                    # Draft trigger
                    triggers.append((
                        draft_dt,
                        f"event-draft-{cfg.guild_id}-{event_date}",
                        lambda el=event_list, k=event_key, rd=event_date,
                               dc=draft_channel_id, ac=announcement_chan_id,
                               fw=five_min_warn, c=cfg: post_editor(
                            bot, el, k, rd, c,
                            draft_channel_id=dc,
                            announcement_channel_id=ac,
                            five_min_warning=fw,
                        ),
                    ))

        # Pending 5-minute warnings
        for key, val in list(pending_warnings.items()):
            warn_dt, event_list, guild_id = val
            cfg = get_config(guild_id)
            if cfg:
                triggers.append((
                    warn_dt,
                    f"5min-warning-{key}",
                    lambda k=key, el=event_list, c=cfg: fire_warning(bot, k, el, c),
                ))

        cutoff   = now - timedelta(seconds=60)
        upcoming = [(dt, label, fn) for dt, label, fn in triggers if dt > cutoff]
        upcoming.sort(key=lambda x: x[0])

        if not upcoming:
            await asyncio.sleep(3600)
            continue

        next_dt, next_label, next_fn = upcoming[0]
        seconds_until = (next_dt - datetime.now(tz=ET)).total_seconds()

        if seconds_until <= 30:
            print(f"[SCHEDULER] Firing: {next_label}")
            try:
                await next_fn()
            except Exception as e:
                print(f"[SCHEDULER][ERROR] Failed to fire {next_label}: {e}")
            await asyncio.sleep(90)
        else:
            sleep_for = max(seconds_until - 30, 60)
            print(f"[SCHEDULER] Next: {next_label} at {next_dt.strftime('%Y-%m-%d %H:%M %Z')} — sleeping {sleep_for:.0f}s")
            await asyncio.sleep(sleep_for)


# ── Trigger actions ────────────────────────────────────────────────────────────

async def post_editor(bot, event_list: list[dict], event_key: str, run_date: date,
                      cfg=None, draft_channel_id: int = 0,
                      announcement_channel_id: int = 0, five_min_warning: bool = True):
    """Post the event editor to the draft channel."""
    if cfg is None:
        return
    # Use per-event channel if set, fall back to guild leadership channel
    channel_id = draft_channel_id or cfg.leadership_channel_id
    channel    = bot.get_channel(channel_id)
    if channel is None:
        print("[SCHEDULER][ERROR] Draft channel not found")
        return

    view = EventEditorView(bot=bot, event_list=event_list, event_key=event_key, run_date=run_date, guild_id=cfg.guild_id)

    lines = []
    for event in event_list:
        lib  = EVENT_LIBRARY.get(event["key"], {})
        name = lib.get("name", event["key"])
        t    = format_et(event["dt"])
        sv   = to_server_time_str(event["dt"])
        lines.append(f"{len(lines)+1}. **{name}** — {t} ET ({sv} server)")

    await channel.send(
        f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
        f"**Current events:**\n" + "\n".join(lines) + "\n\n**Announcement text:** *None*",
        view=view,
    )
    print(f"[SCHEDULER] Event editor posted for {event_key}")


async def post_shield_draft(bot, event_key: str, cfg=None):
    if cfg is None:
        return
    channel = bot.get_channel(cfg.leadership_channel_id)
    if channel is None:
        return

    view = ApprovalView(
        bot=bot,
        draft_message=SHIELD_REMINDER,
        event_key=event_key,
        is_shield=True,
        guild_id=cfg.guild_id,
    )
    await channel.send(
        f"🛡️ **Friday shield reminder — please review and approve:**\n\n{SHIELD_REMINDER}",
        view=view,
    )
    print("[SCHEDULER] Shield reminder draft posted")


async def fire_warning(bot, event_key: str, event_list: list[dict], cfg=None):
    if cfg is None:
        return
    channel = bot.get_channel(cfg.announcement_channel_id)
    if channel is None:
        return

    message = build_warning_message(event_list, guild_id=getattr(cfg, "guild_id", None))
    await channel.send(message)

    leadership = bot.get_channel(cfg.leadership_channel_id)
    if leadership:
        _now = datetime.now(tz=ET)
        _h12 = _now.hour % 12 or 12
        _ts  = f"{_h12}:{_now:%M%p ET}".lower()
        await leadership.send(
            f"⏱️ **5-minute warning auto-posted** at {_ts}"
        )

    pending_warnings.pop(event_key, None)
    print(f"[SCHEDULER] 5-minute warning fired for {event_key}")

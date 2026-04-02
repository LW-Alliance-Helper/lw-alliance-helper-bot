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

# ── Channel IDs ────────────────────────────────────────────────────────────────
ANNOUNCEMENT_CHANNEL_ID = 1414725199257010336
LEADERSHIP_CHANNEL_ID   = 1488693874938482799

# The role mention to tag in public announcements
OGV_ROLE_MENTION = "<@&1266235041600503880>"

ET = ZoneInfo("America/New_York")

# ── Schedule constants ─────────────────────────────────────────────────────────
ANCHOR_DATE  = date(2026, 3, 30)
CYCLE_DAYS   = 3

NORMAL_MARAUDER_TIME   = (22, 15)
NORMAL_SIEGE_TIME      = (22, 45)
SATURDAY_MARAUDER_TIME = (17,  0)
SATURDAY_SIEGE_TIME    = (17, 30)

FRIDAY_SHIELD_WARNING_TIME = (21, 55)

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
        "blurb":    "Marauder at {time} ({server} server). Make sure to have offline participation checked!",
        "optional": False,
    },
    "siege": {
        "name":     "Zombie Siege",
        "blurb":    "Zombies at {time} ({server} server). Be sure you have squads on your wall!",
        "optional": False,
    },
    "glacieradon": {
        "name":     "Glacieradon",
        "blurb":    "We will be doing Glacieradon at {time} ({server} server)! Remember to start with only 5 hits and watch chat for more instructions.",
        "optional": True,
    },
    "blimp": {
        "name":     "Blimp",
        "blurb":    "The Blimp will be at {time} ({server} server)! This doesn't pull for offline participation, so try to be there!",
        "optional": True,
    },
}

OPTIONAL_EVENTS = {k: v for k, v in EVENT_LIBRARY.items() if v["optional"]}


# ── Schedule helpers ───────────────────────────────────────────────────────────

def next_event_dates(from_date: date = None, count: int = 6) -> list[date]:
    if from_date is None:
        from_date = date.today()
    days_since = (from_date - ANCHOR_DATE).days
    remainder  = days_since % CYCLE_DAYS
    offset     = 0 if remainder == 0 else CYCLE_DAYS - remainder
    results    = []
    candidate  = from_date + timedelta(days=offset)
    while len(results) < count:
        results.append(candidate)
        candidate += timedelta(days=CYCLE_DAYS)
    return results


def is_friday(d: date) -> bool:
    return d.weekday() == 4


def get_event_datetimes(event_date: date) -> tuple[datetime, datetime]:
    if is_friday(event_date):
        run_date = event_date + timedelta(days=1)
        m_h, m_m = SATURDAY_MARAUDER_TIME
        s_h, s_m = SATURDAY_SIEGE_TIME
    else:
        run_date = event_date
        m_h, m_m = NORMAL_MARAUDER_TIME
        s_h, s_m = NORMAL_SIEGE_TIME
    marauder_dt = datetime(run_date.year, run_date.month, run_date.day, m_h, m_m, tzinfo=ET)
    siege_dt    = datetime(run_date.year, run_date.month, run_date.day, s_h, s_m, tzinfo=ET)
    return marauder_dt, siege_dt


def to_server_time_str(et_dt: datetime) -> str:
    return (et_dt + timedelta(hours=2)).strftime("%H:%M")


def format_et(dt: datetime) -> str:
    return dt.strftime("%-I:%M%p").lower()


def noon_dt_for(event_date: date) -> datetime:
    run_date = event_date + timedelta(days=1) if is_friday(event_date) else event_date
    return datetime(run_date.year, run_date.month, run_date.day, 12, 0, tzinfo=ET)


def make_et_datetime(run_date: date, hour: int, minute: int) -> datetime:
    return datetime(run_date.year, run_date.month, run_date.day, hour, minute, tzinfo=ET)


# ── Event list helpers ─────────────────────────────────────────────────────────
# An "event list" is a list of dicts:
# [{ "key": "marauder", "dt": datetime }, ...]

def default_event_list(marauder_dt: datetime, siege_dt: datetime) -> list[dict]:
    return [
        {"key": "marauder", "dt": marauder_dt},
        {"key": "siege",    "dt": siege_dt},
    ]


def build_announcement(event_list: list[dict], notes: str = "") -> str:
    """
    Craft the full announcement message from the event list.
    Format:
      Hey @OGV!
      Here is the schedule for events today:
      • [event blurb]
      • [event blurb]

      [notes if any]
    """
    bullet_lines = []
    for event in event_list:
        key    = event["key"]
        dt     = event["dt"]
        lib    = EVENT_LIBRARY.get(key, {})
        blurb  = lib.get("blurb", f"{key} at {{time}} ({{server}} server).")
        et_str = format_et(dt)
        sv_str = to_server_time_str(dt)
        bullet_lines.append("- " + blurb.format(time=et_str, server=sv_str))

    lines = [
        f"Hey {OGV_ROLE_MENTION}!",
        "Here is the schedule for events today:",
        "",
    ] + bullet_lines

    if notes and notes.strip():
        lines += ["", notes.strip()]

    return "\n".join(lines)


def build_warning_message(event_list: list[dict]) -> str:
    """Build the 5-minute warning based on the first event."""
    if not event_list:
        return "Event starting in 5 minutes! Make sure you're online!"
    first = event_list[0]
    key   = first["key"]
    if key == "marauder":
        return (
            "Marauder in 5 minutes! Make sure you hop online and get your points! "
            "Zombies right after, check your wall to make sure you have squads on it!"
        )
    name = EVENT_LIBRARY.get(key, {}).get("name", key)
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

    def __init__(self, bot, event_list: list[dict], event_key: str, run_date: date):
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.bot        = bot
        self.event_list = deepcopy(event_list)
        self.event_key  = event_key
        self.run_date   = run_date
        self.notes      = ""

    def format_event_list_text(self) -> str:
        lines = []
        for i, event in enumerate(self.event_list, 1):
            lib  = EVENT_LIBRARY.get(event["key"], {})
            name = lib.get("name", event["key"])
            t    = format_et(event["dt"])
            sv   = to_server_time_str(event["dt"])
            lines.append(f"{i}. **{name}** — {t} ET ({sv} server)")
        return "\n".join(lines) if lines else "*No events set*"

    async def refresh(self, interaction: discord.Interaction):
        """Update the editor message with the current event list."""
        content = (
            f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
            f"**Current events:**\n{self.format_event_list_text()}\n\n"
            f"**Notes:** {self.notes if self.notes else '*None*'}"
        )
        await interaction.message.edit(content=content, view=self)

    @discord.ui.button(label="➕ Add Event", style=discord.ButtonStyle.primary, row=0)
    async def add_event(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only show events not already in the list
        current_keys = {e["key"] for e in self.event_list}
        available    = {k: v for k, v in OPTIONAL_EVENTS.items() if k not in current_keys}

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

            # Ask for the time
            channel = interaction.channel
            time_prompt = await channel.send(
                f"⏰ What time is **{EVENT_LIBRARY[chosen_key]['name']}**? "
                f"*(e.g. 10:30pm or 22:30)*"
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
                        f"✅ **{EVENT_LIBRARY[chosen_key]['name']}** added at {format_et(dt)} ET.",
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
                    f"**Notes:** {self.notes if self.notes else '*None*'}"
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
                    label=f"{EVENT_LIBRARY.get(e['key'], {}).get('name', e['key'])} — {format_et(e['dt'])} ET",
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
            lib_name = EVENT_LIBRARY.get(event["key"], {}).get("name", event["key"])
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
                    f"**Notes:** {self.notes if self.notes else '*None*'}"
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
        # Can't remove default events (marauder/siege)
        removable = [
            (i, e) for i, e in enumerate(self.event_list)
            if EVENT_LIBRARY.get(e["key"], {}).get("optional", True)
        ]
        if not removable:
            await interaction.response.send_message(
                "No optional events to remove. Marauder and Siege are always included.",
                ephemeral=True,
            )
            return

        select = discord.ui.Select(
            placeholder="Choose an event to remove...",
            options=[
                discord.SelectOption(
                    label=EVENT_LIBRARY.get(e["key"], {}).get("name", e["key"]),
                    value=str(i),
                )
                for i, e in removable
            ],
        )

        async def on_select(select_interaction: discord.Interaction):
            idx      = int(select_interaction.data["values"][0])
            lib_name = EVENT_LIBRARY.get(self.event_list[idx]["key"], {}).get("name", "Event")
            self.event_list.pop(idx)
            await select_interaction.response.edit_message(
                content=f"✅ **{lib_name}** removed.", view=None
            )
            await interaction.message.edit(
                content=(
                    f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
                    f"**Current events:**\n{self.format_event_list_text()}\n\n"
                    f"**Notes:** {self.notes if self.notes else '*None*'}"
                ),
                view=self,
            )

        select.callback = on_select
        view = discord.ui.View(timeout=60)
        view.add_item(select)
        await interaction.response.send_message("Choose an event to remove:", view=view, ephemeral=True)

    @discord.ui.button(label="📝 Add Notes", style=discord.ButtonStyle.secondary, row=1)
    async def add_notes(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        await interaction.response.defer()

        current_note = f"\n\nCurrent notes:\n> {self.notes}" if self.notes else ""
        prompt = await channel.send(
            f"📝 {interaction.user.mention} — type your additional notes below, or type `clear` to remove existing notes.{current_note}"
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
                await channel.send("✅ Notes cleared.", delete_after=5)
            else:
                self.notes = reply.content.strip()
                await channel.send("✅ Notes saved.", delete_after=5)

        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out.", delete_after=8)

        await interaction.message.edit(
            content=(
                f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
                f"**Current events:**\n{self.format_event_list_text()}\n\n"
                f"**Notes:** {self.notes if self.notes else '*None*'}"
            ),
            view=self,
        )

    @discord.ui.button(label="📣 Build Announcement", style=discord.ButtonStyle.success, row=1)
    async def build_announcement_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        # Disable all buttons
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

        announcement = build_announcement(self.event_list, self.notes)
        view = ApprovalView(
            bot=self.bot,
            draft_message=announcement,
            event_list=self.event_list,
            event_key=self.event_key,
            is_shield=False,
        )

        channel = self.bot.get_channel(LEADERSHIP_CHANNEL_ID)
        if channel:
            await channel.send(
                f"📣 **Announcement draft — please review and approve:**\n\n{announcement}",
                view=view,
            )

        self.stop()


# ── Approval UI ────────────────────────────────────────────────────────────────

class ApprovalView(discord.ui.View):
    def __init__(self, bot, draft_message: str, event_key: str,
                 event_list: list[dict] = None, is_shield: bool = False):
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.bot           = bot
        self.draft_message = draft_message
        self.event_key     = event_key
        self.event_list    = event_list or []
        self.is_shield     = is_shield

    async def _post_to_announcements(self, message: str):
        channel = self.bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if channel is None:
            print("[SCHEDULER][ERROR] Announcements channel not found")
            return

        await channel.send(message)

        # Schedule 5-minute warning based on first event time
        if not self.is_shield and self.event_list:
            warn_dt = first_event_warning_dt(self.event_list)
            if warn_dt:
                pending_warnings[self.event_key] = (warn_dt, self.event_list)
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

        leadership = self.bot.get_channel(LEADERSHIP_CHANNEL_ID)
        if leadership:
            await leadership.send(
                f"✅ **Approved by {interaction.user.display_name} at "
                f"{datetime.now(tz=ET).strftime('%-I:%M%p ET').lower()}**\n"
                f"```\n{self.draft_message}\n```"
            )
        self.stop()

    @discord.ui.button(label="✏️ Edit & Send", style=discord.ButtonStyle.primary)
    async def edit_and_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable_buttons(interaction)

        channel = self.bot.get_channel(LEADERSHIP_CHANNEL_ID)
        if channel is None:
            return

        # Post the current text as a quoted block for easy copying
        prompt = await channel.send(
            f"✏️ {interaction.user.mention} — copy and edit the message below, then send your revised version:\n\n"
            f"```\n{self.draft_message}\n```"
        )

        def check(m):
            return m.author == interaction.user and m.channel.id == LEADERSHIP_CHANNEL_ID

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
        now    = datetime.now(tz=ET)
        today  = now.date()
        events = next_event_dates(from_date=today, count=4)

        triggers = []

        for event_date in events:
            marauder_dt, siege_dt = get_event_datetimes(event_date)
            event_key  = f"event-{event_date.isoformat()}"
            noon_time  = noon_dt_for(event_date)
            event_list = default_event_list(marauder_dt, siege_dt)

            # Noon draft
            triggers.append((
                noon_time,
                f"noon-draft-{event_date}",
                lambda el=event_list, k=event_key, rd=marauder_dt.date(): post_editor(bot, el, k, rd),
            ))

            # Friday shield reminder
            if is_friday(event_date):
                shield_dt = datetime(
                    event_date.year, event_date.month, event_date.day,
                    *FRIDAY_SHIELD_WARNING_TIME, tzinfo=ET,
                )
                triggers.append((
                    shield_dt,
                    f"shield-draft-{event_date}",
                    lambda k=f"shield-{event_date}": post_shield_draft(bot, k),
                ))

        # Pending 5-minute warnings
        for key, val in list(pending_warnings.items()):
            warn_dt, event_list = val
            triggers.append((
                warn_dt,
                f"5min-warning-{key}",
                lambda k=key, el=event_list: fire_warning(bot, k, el),
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

async def post_editor(bot, event_list: list[dict], event_key: str, run_date: date):
    """Post the event editor to leadership at noon."""
    channel = bot.get_channel(LEADERSHIP_CHANNEL_ID)
    if channel is None:
        print("[SCHEDULER][ERROR] Leadership channel not found")
        return

    view = EventEditorView(bot=bot, event_list=event_list, event_key=event_key, run_date=run_date)

    lines = []
    for event in event_list:
        lib  = EVENT_LIBRARY.get(event["key"], {})
        name = lib.get("name", event["key"])
        t    = format_et(event["dt"])
        sv   = to_server_time_str(event["dt"])
        lines.append(f"{len(lines)+1}. **{name}** — {t} ET ({sv} server)")

    await channel.send(
        f"📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n"
        f"**Current events:**\n" + "\n".join(lines) + "\n\n**Notes:** *None*",
        view=view,
    )
    print(f"[SCHEDULER] Event editor posted for {event_key}")


async def post_shield_draft(bot, event_key: str):
    channel = bot.get_channel(LEADERSHIP_CHANNEL_ID)
    if channel is None:
        return

    view = ApprovalView(
        bot=bot,
        draft_message=SHIELD_REMINDER,
        event_key=event_key,
        is_shield=True,
    )
    await channel.send(
        f"🛡️ **Friday shield reminder — please review and approve:**\n\n{SHIELD_REMINDER}",
        view=view,
    )
    print("[SCHEDULER] Shield reminder draft posted")


async def fire_warning(bot, event_key: str, event_list: list[dict]):
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        return

    message = build_warning_message(event_list)
    await channel.send(message)

    leadership = bot.get_channel(LEADERSHIP_CHANNEL_ID)
    if leadership:
        await leadership.send(
            f"⏱️ **5-minute warning auto-posted** at "
            f"{datetime.now(tz=ET).strftime('%-I:%M%p ET').lower()}"
        )

    pending_warnings.pop(event_key, None)
    print(f"[SCHEDULER] 5-minute warning fired for {event_key}")

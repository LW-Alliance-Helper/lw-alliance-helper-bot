"""
scheduler.py — Event reminder scheduler with leadership approval workflow

Schedule logic:
  - Events run on a 3-day cooldown anchored to 2026-03-30
  - Normal run: Marauder 10:15pm ET, Siege 10:45pm ET
  - Friday exception: shifted to Saturday at 5:00pm ET (Marauder), 5:30pm ET (Siege)
  - Server time = ET + 2 hours (fixed UTC-2 offset)

Announcement flow:
  - Noon on event day → draft posted to leadership channel for approval
  - On approval → posted to Announcements, copy kept in leadership channel,
    5-minute warning auto-scheduled based on time parsed from approved message
  - Friday 9:55pm ET → shield reminder draft posted to leadership for approval
  - 5-minute warning → fires automatically, no approval needed
"""

import asyncio
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import discord

# ── Channel IDs ────────────────────────────────────────────────────────────────
ANNOUNCEMENT_CHANNEL_ID = 1414725199257010336
LEADERSHIP_CHANNEL_ID   = 1488693874938482799

# ── Timezone ───────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

# ── Schedule constants ─────────────────────────────────────────────────────────
ANCHOR_DATE  = date(2026, 3, 30)
CYCLE_DAYS   = 3

NORMAL_MARAUDER_TIME   = (22, 15)  # 10:15pm ET
NORMAL_SIEGE_TIME      = (22, 45)  # 10:45pm ET
SATURDAY_MARAUDER_TIME = (17,  0)  # 5:00pm ET
SATURDAY_SIEGE_TIME    = (17, 30)  # 5:30pm ET

FRIDAY_SHIELD_WARNING_TIME = (21, 55)  # 9:55pm ET — 5 min before Friday reset

# Button interaction timeout (seconds) — buttons expire after this long
BUTTON_TIMEOUT = 3600  # 1 hour


# ── Pending 5-minute warnings ──────────────────────────────────────────────────
# Keyed by event date string "YYYY-MM-DD", value is the datetime to fire the warning
pending_warnings: dict[str, datetime] = {}


# ── Schedule helpers ───────────────────────────────────────────────────────────

def next_event_dates(from_date: date = None, count: int = 6) -> list[date]:
    """Return the next `count` event dates starting from from_date."""
    if from_date is None:
        from_date = date.today()

    days_since = (from_date - ANCHOR_DATE).days
    remainder  = days_since % CYCLE_DAYS
    offset     = 0 if remainder == 0 else CYCLE_DAYS - remainder

    results  = []
    candidate = from_date + timedelta(days=offset)
    while len(results) < count:
        results.append(candidate)
        candidate += timedelta(days=CYCLE_DAYS)
    return results


def is_friday(d: date) -> bool:
    return d.weekday() == 4


def get_event_datetimes(event_date: date) -> tuple[datetime, datetime]:
    """
    Return (marauder_dt, siege_dt) as ET-aware datetimes.
    Applies the Friday → Saturday shift automatically.
    """
    if is_friday(event_date):
        run_date = event_date + timedelta(days=1)  # Saturday
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
    """Convert ET datetime to server time string (ET + 2h)."""
    server_dt = et_dt + timedelta(hours=2)
    return server_dt.strftime("%H:%M")


def format_et(dt: datetime) -> str:
    """Format as 12-hour ET time, e.g. 10:15pm."""
    return dt.strftime("%-I:%M%p").lower()


def noon_dt_for(event_date: date) -> datetime:
    """Return noon ET on the day the event actually runs (Saturday if Friday shifted)."""
    if is_friday(event_date):
        run_date = event_date + timedelta(days=1)
    else:
        run_date = event_date
    return datetime(run_date.year, run_date.month, run_date.day, 12, 0, tzinfo=ET)


# ── Message builders ───────────────────────────────────────────────────────────

def build_event_draft(marauder_dt: datetime, siege_dt: datetime) -> str:
    m_et     = format_et(marauder_dt)
    m_server = to_server_time_str(marauder_dt)
    return (
        f"Hello Everyone!\n"
        f"• Marauder at {m_et} Eastern ({m_server} server) with Zombies right after!\n"
        f"• Make sure you have offline participation checked and squads on the wall for Zombies!"
    )


def build_warning_message() -> str:
    return (
        "Marauder in 5 minutes! Make sure you hop online and get your points! "
        "Zombies right after, check your wall to make sure you have squads on it!"
    )


SHIELD_REMINDER = (
    "Buster day reminder - log in and shield up if you aren't going hunting!"
)


# ── Time parser (for edited messages) ─────────────────────────────────────────

def parse_marauder_time_from_message(message_text: str) -> datetime | None:
    """
    Try to extract the Marauder event time from an approved/edited message.
    Looks for patterns like '10:15pm', '5:00pm', '5pm', etc.
    Returns an ET-aware datetime for today or tomorrow as appropriate,
    or None if no time could be parsed.
    """
    pattern = r"(\d{1,2}):?(\d{2})?\s*(am|pm)"
    match = re.search(pattern, message_text, re.IGNORECASE)
    if not match:
        return None

    hour   = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    ampm   = match.group(3).lower()

    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    now = datetime.now(tz=ET)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If the parsed time is already in the past today, try tomorrow
    if candidate < now:
        candidate += timedelta(days=1)

    return candidate


# ── Approval UI ────────────────────────────────────────────────────────────────

class ApprovalView(discord.ui.View):
    """
    Renders Send As-Is and Edit & Send buttons.

    Parameters:
        bot            — the discord bot instance
        draft_message  — the announcement text to be approved or edited
        event_key      — unique string identifying this event (e.g. "event-2026-04-02")
                         used to register the pending 5-min warning
        is_shield      — True for the Friday shield reminder (no 5-min warning needed)
    """

    def __init__(self, bot, draft_message: str, event_key: str, is_shield: bool = False):
        super().__init__(timeout=BUTTON_TIMEOUT)
        self.bot           = bot
        self.draft_message = draft_message
        self.event_key     = event_key
        self.is_shield     = is_shield

    async def _post_to_announcements(self, interaction: discord.Interaction, message: str):
        """Send the approved message to the Announcements channel."""
        ann_channel = self.bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
        if ann_channel is None:
            await interaction.followup.send(
                "⚠️ Could not find the Announcements channel.", ephemeral=True
            )
            return

        await ann_channel.send(message)

        # Schedule the 5-minute warning if this is an event announcement (not shield)
        if not self.is_shield:
            warning_time = parse_marauder_time_from_message(message)
            if warning_time:
                warn_dt = warning_time - timedelta(minutes=5)
                pending_warnings[self.event_key] = warn_dt
                print(f"[SCHEDULER] 5-min warning scheduled for {warn_dt.strftime('%Y-%m-%d %H:%M %Z')}")
            else:
                print(f"[SCHEDULER][WARN] Could not parse event time from approved message — 5-min warning not scheduled")

    async def _disable_buttons(self, interaction: discord.Interaction):
        """Disable all buttons on this view after an action is taken."""
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="✅ Send As-Is", style=discord.ButtonStyle.success)
    async def send_as_is(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable_buttons(interaction)

        # Post to announcements
        await self._post_to_announcements(interaction, self.draft_message)

        # Keep a copy in leadership channel with approval stamp
        leadership_channel = self.bot.get_channel(LEADERSHIP_CHANNEL_ID)
        if leadership_channel:
            await leadership_channel.send(
                f"✅ **Approved by {interaction.user.display_name} at "
                f"{datetime.now(tz=ET).strftime('%-I:%M%p ET').lower()}**\n"
                f"```\n{self.draft_message}\n```"
            )

        self.stop()

    @discord.ui.button(label="✏️ Edit & Send", style=discord.ButtonStyle.primary)
    async def edit_and_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable_buttons(interaction)

        leadership_channel = self.bot.get_channel(LEADERSHIP_CHANNEL_ID)
        if leadership_channel is None:
            return

        # Ask the user to type their revised message
        prompt = await leadership_channel.send(
            f"✏️ {interaction.user.mention} — please type your revised announcement below. "
            f"It will be posted here for review before sending."
        )

        def check(m):
            # Accept the next message from this user in the leadership channel
            return m.author == interaction.user and m.channel.id == LEADERSHIP_CHANNEL_ID

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=300)
            revised_text = reply.content

            # Delete the prompt and the user's raw reply to keep chat clean
            try:
                await prompt.delete()
                await reply.delete()
            except discord.HTTPException:
                pass

            # Post the revised draft back for another round of review
            new_view = ApprovalView(
                bot=self.bot,
                draft_message=revised_text,
                event_key=self.event_key,
                is_shield=self.is_shield,
            )
            await leadership_channel.send(
                f"📝 **Revised draft** (edited by {interaction.user.display_name}):\n\n{revised_text}",
                view=new_view,
            )

        except asyncio.TimeoutError:
            await leadership_channel.send(
                f"⏰ Edit timed out — no message received from {interaction.user.mention} within 5 minutes. "
                f"Please re-run or wait for the next scheduled draft."
            )

        self.stop()

    async def on_timeout(self):
        """Disable buttons when the view expires."""
        for item in self.children:
            item.disabled = True


# ── Main scheduler loop ────────────────────────────────────────────────────────

async def run_scheduler(bot: discord.ext.commands.Bot):
    """
    Main loop. Calculates all upcoming trigger times, sleeps until the next
    one is due, fires it, and repeats.
    """
    await bot.wait_until_ready()
    print("[SCHEDULER] Started.")

    while not bot.is_closed():
        now    = datetime.now(tz=ET)
        today  = now.date()
        events = next_event_dates(from_date=today, count=4)

        # Collect all upcoming triggers as (fire_at, label, coroutine_factory)
        triggers = []

        for event_date in events:
            marauder_dt, siege_dt = get_event_datetimes(event_date)
            event_key = f"event-{event_date.isoformat()}"
            noon_time = noon_dt_for(event_date)

            # ── Noon draft to leadership ───────────────────────────────────────
            triggers.append((
                noon_time,
                f"noon-draft-{event_date}",
                lambda m=marauder_dt, s=siege_dt, k=event_key: post_draft(bot, m, s, k),
            ))

            # ── Friday shield reminder draft ───────────────────────────────────
            if is_friday(event_date):
                shield_dt = datetime(
                    event_date.year, event_date.month, event_date.day,
                    *FRIDAY_SHIELD_WARNING_TIME, tzinfo=ET
                )
                triggers.append((
                    shield_dt,
                    f"shield-draft-{event_date}",
                    lambda d=shield_dt, k=f"shield-{event_date}": post_shield_draft(bot, k),
                ))

        # ── Pending 5-minute warnings ──────────────────────────────────────────
        for key, warn_dt in list(pending_warnings.items()):
            triggers.append((
                warn_dt,
                f"5min-warning-{key}",
                lambda k=key: fire_warning(bot, k),
            ))

        # Filter to future triggers only (60s buffer to avoid re-firing)
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

async def post_draft(bot, marauder_dt: datetime, siege_dt: datetime, event_key: str):
    """Post the noon event draft to the leadership channel for approval."""
    channel = bot.get_channel(LEADERSHIP_CHANNEL_ID)
    if channel is None:
        print(f"[SCHEDULER][ERROR] Leadership channel not found")
        return

    draft = build_event_draft(marauder_dt, siege_dt)
    view  = ApprovalView(bot=bot, draft_message=draft, event_key=event_key, is_shield=False)

    await channel.send(
        f"📣 **Upcoming event announcement — please review and approve:**\n\n{draft}",
        view=view,
    )
    print(f"[SCHEDULER] Event draft posted to leadership for {event_key}")


async def post_shield_draft(bot, event_key: str):
    """Post the Friday shield reminder draft to the leadership channel for approval."""
    channel = bot.get_channel(LEADERSHIP_CHANNEL_ID)
    if channel is None:
        print(f"[SCHEDULER][ERROR] Leadership channel not found")
        return

    view = ApprovalView(bot=bot, draft_message=SHIELD_REMINDER, event_key=event_key, is_shield=True)

    await channel.send(
        f"🛡️ **Friday shield reminder — please review and approve:**\n\n{SHIELD_REMINDER}",
        view=view,
    )
    print(f"[SCHEDULER] Shield reminder draft posted to leadership")


async def fire_warning(bot, event_key: str):
    """Fire the 5-minute Marauder warning to the Announcements channel."""
    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if channel is None:
        print(f"[SCHEDULER][ERROR] Announcements channel not found")
        return

    message = build_warning_message()
    await channel.send(message)

    # Also log in leadership channel
    leadership = bot.get_channel(LEADERSHIP_CHANNEL_ID)
    if leadership:
        await leadership.send(
            f"⏱️ **5-minute warning auto-posted** at "
            f"{datetime.now(tz=ET).strftime('%-I:%M%p ET').lower()}"
        )

    # Remove from pending
    pending_warnings.pop(event_key, None)
    print(f"[SCHEDULER] 5-minute warning fired for {event_key}")


# Needed for type hint in ApprovalView
import discord.ext.commands

"""
events_hub.py: the single `/events` event-hub entry point that
replaced the three `/events overview|show|log` subcommands plus the
event-list management section of the `/setup → 📣 Events` wizard
(#249).

Officers hit one command. The hub embed surfaces the alliance's
current event config + a button grid that dispatches into every
existing event flow:

  - 📅 Today's events  → scheduler.EventEditorView for today
  - 🔜 Upcoming events → cycle projections (lifted from /events overview)
  - 📜 Event log       → recent approved posts (lifted from /events log)
  - ➕ Create an event → preset picker OR define-your-own free-text flow
  - ⏸️ Pause or resume → toggle `guild_events.active`, re-anchoring a
                         repeating event on the way back on
  - 🗑️ Delete an event → picker over guild_events + permanent row removal

Pause vs delete is a deliberate split: pausing an event between seasons
keeps every setting and is one click to undo, while delete is the
irreversible door for events an alliance is genuinely done with. Before
this split, delete was the only stop available and it soft-deleted with
no way back, so an off-season pause looked like permanent data loss.

Event creation moved out of `/setup → 📣 Events` so leadership can manage
their event roster from one home (`/events`) instead of crawling through
the setup wizard whenever a new event drops or rotates out. Custom-event
creation stays first-class via the "Define my own" path — alliance-
internal events, regional themes, anything outside the canonical list
all use the same wizard the setup flow used to.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import date as date_cls, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord

from messages import (
    CANCEL_BACKPEDAL,
    CANCEL_PLAIN,
    DATE_PARSE_GIVE_UP,
    DATE_PARSE_REJECT,
    DATE_PARSE_RETRY,
    DENY_NOT_OWNER,
    GENERIC_CMD_TIMEOUT,
    INPUT_INVALID_NO_EXAMPLE,
    LEADERSHIP_INACCESSIBLE,
    LEADERSHIP_NO_READ_PERM,
    LEADERSHIP_NOT_CONFIGURED,
    TIER_COMPARISON,
    TIME_PARSE_GIVE_UP,
    TIME_PARSE_RETRY,
)

logger = logging.getLogger(__name__)


# ── Button labels (exported) ─────────────────────────────────────────────────
#
# Single source of truth for the hub's button labels. Imported by every
# module that quotes a button name in error / timeout / followup copy,
# so a rename here updates every caller automatically. Matches the
# HUB_BTN_* pattern in storm_event_hub.py.

EVENTS_HUB_TITLE = "📣 Event Announcements"
EVENTS_HUB_CMD = "/events"
EVENTS_HUB_BTN_TODAY = "📅 Today's events"
EVENTS_HUB_BTN_UPCOMING = "🔜 Upcoming events"
EVENTS_HUB_BTN_LOG = "📜 Event log"
EVENTS_HUB_BTN_CREATE = "➕ Create an event"
EVENTS_HUB_BTN_WARNING = "✏️ Edit 5-minute warning"
EVENTS_HUB_BTN_PAUSE = "⏸️ Pause or resume"
EVENTS_HUB_BTN_DELETE = "🗑️ Delete an event"

# Anchor-date formats we advertise, shared by the wizard prompt and the
# retry notice so the examples we suggest never drift from each other.
# The parser accepts more than this (see `_parse_month_day`); these are
# the four shapes officers actually type.
ANCHOR_DATE_EXAMPLES = "`March 30`, `7/30`, `2026-07-30`, or `today`"


# ── Preset library ──────────────────────────────────────────────────────────
#
# Curated set of canonical LW events. Picking a preset prefills the
# event's name, default blurb, and an interval suggestion; the officer
# still enters anchor date + time at the wizard. Officers needing
# alliance-internal or regional events fall through to "Define my own"
# from the Create wizard.
#
# `stage_note` shows in the dropdown description so officers can pick
# the right AE variant for their game stage without guessing. It is NOT
# saved to the event name; only `name` ends up in guild_events.
#
# The AE family is split into three picks because the in-game name
# changes per game stage even though the mechanics are identical.

_DEFAULT_BLURB = "{name} at {time} ({server_time} Server Time)."

AE_EVENT_PRESETS: list[dict] = [
    {
        "key": "ae_plague_marauder",
        "name": "Alliance Exercise: Plague Marauder",
        "stage_note": "S5 Off-season and later · every 3 days",
        "blurb": _DEFAULT_BLURB,
        "interval_days": 3,
    },
    {
        "key": "ae_marshalls_guard",
        "name": "Alliance Exercise: Marshall's Guard",
        "stage_note": "Early seasons (pre-S3) · every 3 days",
        "blurb": _DEFAULT_BLURB,
        "interval_days": 3,
    },
    {
        "key": "ae_sandworm",
        "name": "Alliance Exercise: Sandworm",
        "stage_note": "Seasons 3 and 4 · every 3 days",
        "blurb": _DEFAULT_BLURB,
        "interval_days": 3,
    },
    {
        "key": "zombie_siege",
        "name": "Zombie Siege",
        "stage_note": "Alliance defense · every 3 days",
        "blurb": _DEFAULT_BLURB,
        "interval_days": 3,
    },
    {
        "key": "glacieradon",
        "name": "Glacieradon",
        "stage_note": "Pairs with Gold Zombies · every other week if recurring",
        "blurb": _DEFAULT_BLURB,
        "interval_days": 14,
    },
    {
        "key": "sky_predator",
        "name": "Sky Predator",
        "stage_note": "Pairs with General's Trials · every other week if recurring",
        "blurb": _DEFAULT_BLURB,
        "interval_days": 14,
    },
]


def _preset_by_key(key: str) -> Optional[dict]:
    """Look up a preset by its key. Returns None if the picker passed a
    value that isn't in the library (shouldn't happen — the dropdown
    options are sourced from the library — but defensive anyway)."""
    for p in AE_EVENT_PRESETS:
        if p["key"] == key:
            return p
    return None


# ── Embed builder ────────────────────────────────────────────────────────────


def describe_event_schedule(ev: dict, *, today: Optional[date_cls] = None) -> str:
    """One-line plain-English summary of when an event fires next.

    Shared by the hub embed's event list and the resume preview, so the
    schedule an officer reads before resuming is rendered by exactly the
    same code that lists it afterwards. Never raises — a malformed anchor
    or interval degrades to an explanatory string rather than blanking the
    surface it's embedded in.
    """
    from scheduler import next_event_dates

    today = today or date_cls.today()
    if ev.get("schedule_type") != "repeating" or not ev.get("anchor_date"):
        return "Manual (add it to a draft from the editor)"
    try:
        anchor = date_cls.fromisoformat(ev["anchor_date"])
        interval = int(ev["interval_days"] or 0)
        upcoming = (
            next_event_dates(from_date=today, count=1, anchor=anchor, cycle=interval)
            if interval > 0
            else []
        )
    except (ValueError, TypeError):
        return "Schedule invalid (re-create the event)"
    if not upcoming:
        return f"Recurring every {interval} days (next instance not yet computable)"
    nxt = upcoming[0]
    days = (nxt - today).days
    when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
    return f"Next event instance: {nxt:%a %b} {nxt.day} ({when}) - every {interval} days"


def _build_events_hub_embed(guild: discord.Guild) -> discord.Embed:
    """Build the hub embed showing the alliance's current event config
    plus a one-glance "what's available right now" summary.

    Reads from `guild_events` and `guild_configs`. Skips per-event
    next-firing-date computation when nothing is configured — the hub
    is also the discovery surface for new alliances, so the empty
    state needs to render usefully."""
    import config

    embed = discord.Embed(
        title=EVENTS_HUB_TITLE,
        color=discord.Color.blurple(),
        description=(
            "Manage your alliance's event announcements. Pick an action "
            "below — every event flow lives behind one of these buttons."
        ),
    )

    try:
        cfg = config.get_config(guild.id)
    except Exception:
        cfg = None
    try:
        all_events = config.get_guild_events(guild.id, active_only=False)
    except Exception:
        all_events = []
    events = [e for e in all_events if e.get("active")]
    paused = [e for e in all_events if not e.get("active")]

    # Foundation block: channels + draft cadence.
    draft_id = cfg.event_draft_channel_id if cfg else 0
    announce_id = cfg.event_announce_channel_id if cfg else 0
    draft_time = cfg.event_draft_time if cfg else None

    config_lines = []
    config_lines.append(f"**Draft channel:** {f'<#{draft_id}>' if draft_id else '*not set*'}")
    config_lines.append(
        f"**Announcement channel:** {f'<#{announce_id}>' if announce_id else '*not set*'}"
    )
    config_lines.append(f"**Draft time:** {draft_time or '*not set*'}")
    # No server-level 5-minute warning line here. It is per event (#566), and
    # one on/off for the whole alliance could only ever be wrong for some of
    # them. Each event's state is on its own row under
    # `EVENTS_HUB_BTN_WARNING`.
    embed.add_field(name="Configuration", value="\n".join(config_lines), inline=False)

    # Event list with next-firing-date hint per repeating event.
    if not events:
        embed.add_field(
            name="Events",
            value=f"*No events configured yet. Click* **{EVENTS_HUB_BTN_CREATE}** *to add one.*",
            inline=False,
        )
    else:
        today = date_cls.today()
        lines = [
            f"**{ev.get('name') or '(unnamed)'}** - {describe_event_schedule(ev, today=today)}"
            for ev in events
        ]
        embed.add_field(
            name=f"Events ({len(events)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )

    # Paused events keep every setting and fire nothing. Surfaced here so
    # an off-season pause is visible rather than looking like the event
    # vanished — that ambiguity is what made the old delete-only flow feel
    # unrecoverable.
    if paused:
        paused_names = ", ".join(f"**{e.get('name') or '(unnamed)'}**" for e in paused)
        embed.add_field(
            name=f"Paused ({len(paused)})",
            value=(f"{paused_names}\n*Click* **{EVENTS_HUB_BTN_PAUSE}** *to turn them back on.*")[
                :1024
            ],
            inline=False,
        )

    return embed


# ── Hub view ─────────────────────────────────────────────────────────────────


class _EventsHubView(discord.ui.View):
    """Hub button grid. Each button dispatches into the matching flow.

    Layout (2 rows, 6 buttons):
        Row 0 (read surfaces):
          📅 Today's events (blue) | 🔜 Upcoming events (secondary) |
          📜 Event log (secondary)
        Row 1 (write surfaces):
          ➕ Create an event (green) | ✏️ Edit 5-minute warning (secondary) |
          ⏸️ Pause or resume (secondary) | 🗑️ Delete an event (red)

    The write surfaces sit on their own row so they don't visually
    compete with the read-only buttons above. Today's events takes the
    primary-blue style since that's the most common "I'm about to
    publish today's draft" action. Pause sits next to Delete
    deliberately: it's the reversible middle ground, and putting it next
    to the red button makes it the obvious alternative to deleting.

    Edit 5-minute warning (#566) went in after Create rather than at the end
    of the row, which does shift Pause and Delete one position right.
    The alternative was putting a routine action next to the red button,
    and DESIGN.md is explicit that a destructive control sits at the end
    of its row and never adjacent to a frequently-clicked one. Keeping
    Delete last, and keeping Pause as its neighbour, won over leaving
    the other two positions untouched.
    """

    def __init__(self, bot, guild_id: int, owner_user_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.message: Optional[discord.Message] = None
        self._build_buttons()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=EVENTS_HUB_CMD)

    def _build_buttons(self) -> None:
        # Row 0: read surfaces
        self._add(EVENTS_HUB_BTN_TODAY, discord.ButtonStyle.primary, 0, self._on_today)
        self._add(EVENTS_HUB_BTN_UPCOMING, discord.ButtonStyle.secondary, 0, self._on_upcoming)
        self._add(EVENTS_HUB_BTN_LOG, discord.ButtonStyle.secondary, 0, self._on_log)
        # Row 1: write surfaces
        self._add(EVENTS_HUB_BTN_CREATE, discord.ButtonStyle.success, 1, self._on_create)
        self._add(EVENTS_HUB_BTN_WARNING, discord.ButtonStyle.secondary, 1, self._on_warning)
        self._add(EVENTS_HUB_BTN_PAUSE, discord.ButtonStyle.secondary, 1, self._on_pause)
        self._add(EVENTS_HUB_BTN_DELETE, discord.ButtonStyle.danger, 1, self._on_delete)

    def _add(self, label, style, row, callback):
        btn = discord.ui.Button(label=label[:80], style=style, row=row)
        btn.callback = callback
        self.add_item(btn)

    # ── Button callbacks ─────────────────────────────────────────────────

    async def _on_today(self, inter: discord.Interaction) -> None:
        await _open_today_editor(self.bot, inter)

    async def _on_upcoming(self, inter: discord.Interaction) -> None:
        await _render_upcoming_followup(inter)

    async def _on_log(self, inter: discord.Interaction) -> None:
        await _render_log_followup(self.bot, inter)

    async def _on_create(self, inter: discord.Interaction) -> None:
        await _open_create_picker(self.bot, inter)

    async def _on_warning(self, inter: discord.Interaction) -> None:
        await _open_warning_picker(inter)

    async def _on_pause(self, inter: discord.Interaction) -> None:
        await _open_pause_picker(inter)

    async def _on_delete(self, inter: discord.Interaction) -> None:
        await _open_delete_picker(inter)


# ── Today's events: dispatch into the existing EventEditorView ──────────────


async def _open_today_editor(bot, interaction: discord.Interaction) -> None:
    """Reuse scheduler.post_editor for today's date — same flow as the
    pre-hub /events show ran with no date arg."""
    from config import get_config, get_guild_events
    from scheduler import next_event_dates, post_editor

    await interaction.response.defer(ephemeral=False)
    guild_id = interaction.guild_id
    cfg = get_config(guild_id)
    events = get_guild_events(guild_id, active_only=True)
    today = date_cls.today()

    if not events:
        await interaction.followup.send(
            f"ℹ️ No events configured. Click **{EVENTS_HUB_BTN_CREATE}** to add one.",
            ephemeral=True,
        )
        return

    # Group repeating events by (anchor, interval) to find the soonest
    # event date on or after today.
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for ev in events:
        if ev["schedule_type"] == "repeating" and ev["anchor_date"]:
            groups[(ev["anchor_date"], ev["interval_days"])].append(ev)

    if not groups:
        # Manual-only alliance: no repeating event anchors a date, so open
        # the editor on today with an empty draft. Leadership then uses the
        # editor's "Add to today's draft" button (which already offers every
        # configured event, manual included) to drop in a manual event.
        # Without this, manual events were a dead end — the hub tells users
        # to add them "from the editor" but the editor never opened.
        draft_channel_id = 0
        announce_channel_id = 0
        five_min_warn = False
        for ev in events:
            draft_channel_id = ev["draft_channel_id"] or draft_channel_id
            announce_channel_id = ev["announcement_channel_id"] or announce_channel_id
            if ev["five_min_warning"]:
                five_min_warn = True
        event_key = f"event-{guild_id}-{today.isoformat()}-hub"
        posted = await post_editor(
            bot,
            [],
            event_key,
            today,
            cfg=cfg,
            draft_channel_id=draft_channel_id,
            announcement_channel_id=announce_channel_id,
            five_min_warning=five_min_warn,
        )
        if posted is False:
            target = draft_channel_id or cfg.leadership_channel_id
            await interaction.followup.send(
                f"⚠️ I couldn't post the event editor to <#{target}> — check that "
                "I can view and send messages in that channel.",
                ephemeral=True,
            )
        return

    next_per_group: list[tuple[date_cls, tuple[str, int]]] = []
    for key in groups:
        anchor_str, interval = key
        try:
            anchor = date_cls.fromisoformat(anchor_str)
        except ValueError:
            continue
        upcoming = next_event_dates(from_date=today, count=1, anchor=anchor, cycle=interval)
        if upcoming:
            next_per_group.append((upcoming[0], key))

    if not next_per_group:
        await interaction.followup.send(
            "ℹ️ Couldn't compute the next event date — your repeating events "
            "have invalid anchor dates.",
            ephemeral=True,
        )
        return

    next_per_group.sort(key=lambda x: x[0])
    event_date = next_per_group[0][0]
    days_diff = (event_date - today).days
    if days_diff > 0:
        await interaction.followup.send(
            f"ℹ️ **{today:%B} {today.day}** is not an event day. "
            f"Showing the next event date: **{event_date:%A, %B} {event_date.day}**.",
            ephemeral=True,
        )

    event_list: list[dict] = []
    draft_channel_id = 0
    announce_channel_id = 0
    five_min_warn = False
    for (anchor_str, interval), group_events in groups.items():
        try:
            anchor = date_cls.fromisoformat(anchor_str)
        except ValueError:
            continue
        upcoming = next_event_dates(from_date=event_date, count=1, anchor=anchor, cycle=interval)
        if not upcoming or upcoming[0] != event_date:
            continue
        for ev in group_events:
            try:
                ev_tz = ZoneInfo(ev["timezone"])
                t_h, t_m = (int(p) for p in ev["default_time"].split(":")[:2])
                ev_dt = datetime(
                    event_date.year, event_date.month, event_date.day, t_h, t_m, tzinfo=ev_tz
                )
                event_list.append(
                    {
                        "key": ev["short_key"],
                        "name": ev["name"],
                        "dt": ev_dt,
                        "blurb": ev["announcement_blurb"],
                        "warning_blurb": ev["warning_blurb"],
                    }
                )
                draft_channel_id = ev["draft_channel_id"] or draft_channel_id
                announce_channel_id = ev["announcement_channel_id"] or announce_channel_id
                if ev["five_min_warning"]:
                    five_min_warn = True
            except Exception as e:
                logger.warning(
                    "[EVENTS HUB] Error processing event %s: %s", ev.get("short_key", "?"), e
                )

    if not event_list:
        await interaction.followup.send(
            "⚠️ No events to show on the next event date — likely a bad timezone "
            "or default_time on one of your configured events.",
            ephemeral=True,
        )
        return

    event_list.sort(key=lambda x: x["dt"])
    event_key = f"event-{guild_id}-{event_date.isoformat()}-hub"
    posted = await post_editor(
        bot,
        event_list,
        event_key,
        event_date,
        cfg=cfg,
        draft_channel_id=draft_channel_id,
        announcement_channel_id=announce_channel_id,
        five_min_warning=five_min_warn,
    )
    if posted is False:
        target = draft_channel_id or cfg.leadership_channel_id
        await interaction.followup.send(
            f"⚠️ I couldn't post the event editor to <#{target}> — check that "
            "I can view and send messages in that channel.",
            ephemeral=True,
        )


# ── Upcoming events: lifted from the old /events overview ────────────────────

UPCOMING_WINDOW_DAYS = 30
UPCOMING_MAX_DATES_SHOWN = 12


async def _render_upcoming_followup(interaction: discord.Interaction) -> None:
    """Render the configured event types + every occurrence due in the next
    UPCOMING_WINDOW_DAYS, so leadership can see what weekday each one lands
    on (repeating events whose interval isn't a multiple of 7 drift across
    weekdays cycle to cycle). Lifted from the pre-hub /events overview slash
    so the read-only pre-flight content stays accessible without the
    subcommand."""
    from config import get_guild_events
    from scheduler import next_event_dates

    events = get_guild_events(interaction.guild_id, active_only=True)
    today = date_cls.today()
    window_end = today + timedelta(days=UPCOMING_WINDOW_DAYS)

    embed = discord.Embed(
        title="🔜 Upcoming events",
        description=f"Next {UPCOMING_WINDOW_DAYS} days",
        color=discord.Color.blurple(),
    )

    if not events:
        embed.description = (
            f"No event types configured yet. Click **{EVENTS_HUB_BTN_CREATE}** to add some."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    repeating_lines: list[str] = []
    manual_lines: list[str] = []
    for ev in events:
        name = ev.get("name") or "(unnamed)"
        if ev["schedule_type"] == "repeating" and ev.get("anchor_date"):
            try:
                anchor = date_cls.fromisoformat(ev["anchor_date"])
                interval = int(ev["interval_days"] or 0)
            except (ValueError, TypeError):
                repeating_lines.append(f"• **{name}** — schedule invalid")
                continue
            if interval <= 0:
                repeating_lines.append(f"• **{name}** — every {interval}d")
                continue

            fetch_count = UPCOMING_WINDOW_DAYS // interval + 2
            upcoming = next_event_dates(
                from_date=today, count=fetch_count, anchor=anchor, cycle=interval
            )
            in_window = [d for d in upcoming if d <= window_end]

            if in_window:
                shown = in_window[:UPCOMING_MAX_DATES_SHOWN]
                date_lines = "\n".join(f"  {d:%a %b} {d.day}" for d in shown)
                if len(in_window) > UPCOMING_MAX_DATES_SHOWN:
                    date_lines += f"\n  … +{len(in_window) - UPCOMING_MAX_DATES_SHOWN} more"
                repeating_lines.append(f"• **{name}** — every {interval}d\n{date_lines}")
            else:
                nxt = upcoming[0]
                days = (nxt - today).days
                when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
                repeating_lines.append(
                    f"• **{name}** — every {interval}d, next on {nxt:%a %b} {nxt.day} ({when})"
                )
        else:
            manual_lines.append(f"• **{name}** — manual entries only")

    if repeating_lines:
        embed.add_field(
            name=f"Repeating ({len(repeating_lines)})",
            value="\n\n".join(repeating_lines)[:1024],
            inline=False,
        )
    if manual_lines:
        embed.add_field(
            name=f"Manual ({len(manual_lines)})", value="\n".join(manual_lines)[:1024], inline=False
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Event log: lifted from the old /events log ───────────────────────────────


async def _render_log_followup(bot, interaction: discord.Interaction) -> None:
    """Show recent approved event posts. Window depends on tier (7d
    free / 30d Premium). Lifted from the pre-hub /events log."""
    import premium
    from config import get_config

    await interaction.response.defer(ephemeral=True)

    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.leadership_channel_id:
        await interaction.followup.send(
            LEADERSHIP_NOT_CONFIGURED,
            ephemeral=True,
        )
        return

    leadership = bot.get_channel(cfg.leadership_channel_id)
    if leadership is None:
        await interaction.followup.send(
            LEADERSHIP_INACCESSIBLE,
            ephemeral=True,
        )
        return

    days = await premium.get_limit(
        "events_log_days", interaction.guild_id, interaction=interaction, bot=bot
    )
    days = days or 30
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    matches = []
    try:
        async for msg in leadership.history(after=cutoff, limit=500):
            if msg.author.id != bot.user.id:
                continue
            if msg.content.startswith("✅ **Approved by"):
                matches.append(msg)
    except discord.Forbidden:
        await interaction.followup.send(
            LEADERSHIP_NO_READ_PERM,
            ephemeral=True,
        )
        return

    matches.sort(key=lambda m: m.created_at, reverse=True)

    embed = discord.Embed(
        title=f"📜 Event log — past {days} days",
        description=f"*Showing approved event posts from the past {days} days.*",
        color=discord.Color.blurple(),
    )

    # Local clock conversion uses the bot's ET helper to match the
    # pre-hub log copy. Officers reading the log see the same "logged
    # at X" text they did before.
    from bot import ET

    if not matches:
        embed.add_field(
            name="No approvals found",
            value=f"*No event posts have been approved in the past {days} days.*",
            inline=False,
        )
    else:
        lines = []
        for msg in matches[:25]:
            header = msg.content.split("\n", 1)[0]
            ldt = msg.created_at.astimezone(ET)
            hr12 = ldt.hour % 12 or 12
            local_dt = f"{ldt:%a %b} {ldt.day}, {hr12}:{ldt:%M%p} ET".replace("AM", "am").replace(
                "PM", "pm"
            )
            lines.append(f"• {header} *— logged {local_dt}*")
        embed.add_field(
            name=f"Approvals ({len(matches)})", value="\n".join(lines)[:1024], inline=False
        )

    if days < 30:
        embed.set_footer(
            text=TIER_COMPARISON.format(free_limit="7-day window", premium_limit="30 days")
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Create flow: preset picker -> wizard ─────────────────────────────────────


class _CreatePickerView(discord.ui.View):
    """Two equally-weighted entry buttons: 📋 Pick a preset, ✏️ Define
    my own. Custom events stay first-class — this view exists only to
    branch on which prefill the officer wants."""

    def __init__(self, bot, owner_user_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.owner_user_id = owner_user_id
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="📋 Pick a preset", style=discord.ButtonStyle.primary, row=0)
    async def pick_preset(self, inter: discord.Interaction, _btn: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()
        await _open_preset_dropdown(self.bot, inter)

    @discord.ui.button(label="✏️ Define my own", style=discord.ButtonStyle.secondary, row=0)
    async def define_own(self, inter: discord.Interaction, _btn: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()
        await _run_create_event_wizard(self.bot, inter, preset=None)


async def _open_create_picker(bot, interaction: discord.Interaction) -> None:
    """Free-tier event cap is checked here (before the picker shows)
    so officers don't pick a preset and only then learn the cap is
    full."""
    import premium
    from config import get_guild_events

    events = get_guild_events(interaction.guild_id, active_only=True)
    cap = await premium.get_limit(
        "events",
        interaction.guild_id,
        interaction=interaction,
        bot=bot,
    )
    if cap is not None and len(events) >= cap:
        await interaction.response.send_message(
            embed=premium.limit_reached_embed(
                feature_label="Event Announcements",
                current=len(events),
                cap=cap,
                plural_unit="events",
            ),
            ephemeral=True,
        )
        return

    view = _CreatePickerView(bot, interaction.user.id)
    await interaction.response.send_message(
        "**Create an event** — pick a preset to prefill name + blurb + "
        "cycle suggestion, or define a custom event from scratch. "
        "Both paths still ask for anchor date + time.",
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


async def _open_preset_dropdown(bot, interaction: discord.Interaction) -> None:
    """Single-select dropdown over AE_EVENT_PRESETS. Once an officer
    picks, the wizard launches with that preset's prefills."""
    options = [
        discord.SelectOption(
            label=p["name"][:100],
            description=p["stage_note"][:100],
            value=p["key"],
        )
        for p in AE_EVENT_PRESETS
    ]
    select = discord.ui.Select(placeholder="Pick a preset event…", options=options)
    view = discord.ui.View(timeout=180)
    view.add_item(select)

    async def on_pick(inter: discord.Interaction):
        chosen_key = inter.data["values"][0]
        preset = _preset_by_key(chosen_key)
        select.disabled = True
        await inter.response.edit_message(view=view)
        view.stop()
        if preset:
            await _run_create_event_wizard(bot, inter, preset=preset)
        else:
            await inter.followup.send("⚠️ Could not load that preset.", ephemeral=True)

    select.callback = on_pick
    await interaction.followup.send(
        "Pick the event you want to add — the name and blurb come from the "
        "preset, you'll still enter the anchor date and time:",
        view=view,
        ephemeral=True,
    )


async def _run_create_event_wizard(
    bot,
    interaction: discord.Interaction,
    *,
    preset: Optional[dict] = None,
) -> None:
    """Walk an officer through creating one event. If `preset` is given,
    name + blurb + interval are prefilled; the officer still confirms
    each step (so they can override) and always enters anchor date +
    time. If `preset` is None, the full free-text wizard runs (matches
    the pre-#249 "define my own" path that used to live in the setup
    wizard).

    Wizard prompts happen in the channel via `bot.wait_for("message")`
    — same shape as the existing setup_cog wizards. The hub button
    that opened this is ephemeral; the wizard surface itself is
    publicly visible in the channel so officers can copy-paste and
    iterate."""
    import wizard_registry
    from config import (
        get_config,
        get_or_create_config,
        save_guild_event,
        get_guild_events,
    )
    from scheduler import WARNING_BLURB_DEFAULT
    from setup_cog import _parse_12h_time, _parse_month_day

    guild_id = interaction.guild_id
    channel = interaction.channel
    user = interaction.user
    cancel_event = wizard_registry.register(user.id)

    guild_cfg = get_config(guild_id) or get_or_create_config(guild_id)
    tz = guild_cfg.timezone or "America/New_York"

    # Pull the events-wide settings already saved on guild_configs so
    # we can stamp the new event with them. Officers configure these
    # via /setup → 📣 Events (the wizard for channels + draft time
    # still lives there; only event-list management moved to the hub).
    draft_channel_id = guild_cfg.event_draft_channel_id or 0
    announce_channel_id = guild_cfg.event_announce_channel_id or 0
    draft_time = guild_cfg.event_draft_time or "12:00"

    if not draft_channel_id or not announce_channel_id:
        await channel.send(
            "⚙️ Set up the event channels and draft time first — run "
            "`/setup` → **📣 Events** to configure the draft channel, "
            "announcement channel, draft time, and 5-minute warning, "
            "then come back to **➕ Create an event**."
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 1000):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=120),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send(CANCEL_PLAIN)
            else:
                await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="events"))
            return None
        return reply.content.strip()[:max_chars]

    # ── Name ─────────────────────────────────────────────────────────────────
    if preset:
        await channel.send(
            f"✅ Using preset: **{preset['name']}** ({preset['stage_note']})\n"
            "You'll still pick the schedule, anchor date, and time below. "
            "Pick **✏️ Manual** at the schedule step if you run this event "
            "ad-hoc rather than on a fixed cycle."
        )
        name = preset["name"]
    else:
        name_raw = await ask_text(
            "**Event Name**\n"
            "What is this event called? (e.g. `Plague Marauder (AE)`, `Zombie Siege`)"
        )
        if not name_raw:
            return
        name = name_raw.strip()
        if not name:
            await channel.send("⚠️ Empty name. Canceled.")
            wizard_registry.unregister(user.id, cancel_event)
            return
    short_key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

    # If the short_key already exists, append a numeric suffix so the
    # save doesn't silently overwrite an existing event with the same
    # normalised slug.
    existing_keys = {e["short_key"] for e in get_guild_events(guild_id, active_only=False)}
    if short_key in existing_keys:
        suffix = 2
        while f"{short_key}_{suffix}" in existing_keys:
            suffix += 1
        short_key = f"{short_key}_{suffix}"

    # ── Time ────────────────────────────────────────────────────────────────
    attempts_left = 3
    default_time = None
    while True:
        time_raw = await ask_text(
            f"**{name} — Event Time**\n"
            f"What time does this event usually start? *(in {tz})*\n"
            "*(e.g. `10:15pm`, `9:00am`, or `21:00`)*"
        )
        if not time_raw:
            return
        parsed = _parse_12h_time(time_raw)
        if parsed:
            default_time = parsed
            break
        if len(time_raw) == 5 and time_raw[2] == ":" and time_raw.replace(":", "").isdigit():
            default_time = time_raw
            break
        attempts_left -= 1
        if attempts_left <= 0:
            await channel.send(
                TIME_PARSE_GIVE_UP.format(
                    recovery=f"`{EVENTS_HUB_CMD}` → **{EVENTS_HUB_BTN_CREATE}**",
                )
            )
            wizard_registry.unregister(user.id, cancel_event)
            return
        await channel.send(TIME_PARSE_RETRY.format(raw=time_raw))

    # ── Schedule: repeating vs manual ────────────────────────────────────────
    class _ScheduleView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.selected: Optional[str] = None

        @discord.ui.button(label="🔁 Repeating", style=discord.ButtonStyle.primary)
        async def repeating(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.selected = "repeating"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✏️ Manual", style=discord.ButtonStyle.secondary)
        async def manual(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.selected = "manual"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

    sched_view = _ScheduleView()
    await channel.send(
        f"**{name} — Schedule**\n"
        "Does this event repeat on a fixed cycle, or do you add it manually each time?",
        view=sched_view,
    )
    await wizard_registry.wait_view_or_cancel(sched_view, cancel_event)
    if cancel_event.is_set():
        return
    if not sched_view.selected:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="events"))
        wizard_registry.unregister(user.id, cancel_event)
        return
    schedule_type = sched_view.selected

    anchor_date = ""
    interval_days = preset["interval_days"] if preset else 7

    if schedule_type == "repeating":
        attempts_left = 3
        while True:
            anchor_raw = await ask_text(
                f"**{name} — Anchor Date**\n"
                "Enter a recent or upcoming date when this event occurs.\n"
                f"*(e.g. {ANCHOR_DATE_EXAMPLES})*"
            )
            if not anchor_raw:
                return
            parsed_anchor = _parse_month_day(anchor_raw)
            if parsed_anchor:
                anchor_date = parsed_anchor
                break
            attempts_left -= 1
            if attempts_left <= 0:
                await channel.send(
                    DATE_PARSE_GIVE_UP.format(
                        recovery=f"`{EVENTS_HUB_CMD}` → **{EVENTS_HUB_BTN_CREATE}**",
                    )
                )
                wizard_registry.unregister(user.id, cancel_event)
                return
            await channel.send(
                DATE_PARSE_RETRY.format(raw=anchor_raw, examples=ANCHOR_DATE_EXAMPLES)
            )

        interval_prompt = (
            f"**{name} — Cycle Interval**\n"
            f"How many days between each occurrence? *(default: {interval_days})*\n"
            "Type a number, or `keep` to use the default."
        )
        interval_raw = await ask_text(interval_prompt, max_chars=10)
        if interval_raw is None:
            return
        if interval_raw.strip().lower() not in ("", "keep"):
            try:
                interval_days = int(interval_raw.strip())
            except ValueError:
                await channel.send(
                    INPUT_INVALID_NO_EXAMPLE.format(
                        type="whole number", recovery=f"`{EVENTS_HUB_CMD}`"
                    )
                )
                wizard_registry.unregister(user.id, cancel_event)
                return

    # ── Blurb ───────────────────────────────────────────────────────────────
    # Concrete preview with the placeholders shown so officers see what
    # actually renders when the event fires.
    preview_blurb = f"{name} at {{time}} ({{server_time}} Server Time)."

    class _BlurbChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.choice: Optional[str] = None

        @discord.ui.button(label="✅ Use default blurb", style=discord.ButtonStyle.success)
        async def use_default(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.choice = "default"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Using default blurb:\n`{preview_blurb}`",
                view=self,
            )
            self.stop()

        @discord.ui.button(label="✏️ Enter my own", style=discord.ButtonStyle.secondary)
        async def enter_own(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.choice = "custom"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

    blurb_view = _BlurbChoiceView()
    await channel.send(
        # Colon, not an em dash: UX.md bans those in anything a user sees,
        # and the 5-minute warning step below uses one (#566 sign-off).
        f"**{name}: Announcement Blurb**\n"
        "This message gets posted when this event fires.\n"
        "Use `{time}` for the event time in your timezone and `{server_time}` for Server Time.\n\n"
        f"**Default:** `{preview_blurb}`",
        view=blurb_view,
    )
    await wizard_registry.wait_view_or_cancel(blurb_view, cancel_event)
    if cancel_event.is_set():
        return
    if not blurb_view.choice:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="events"))
        wizard_registry.unregister(user.id, cancel_event)
        return

    if blurb_view.choice == "default":
        blurb = preview_blurb
    else:
        blurb_raw = await ask_text(
            "Enter your announcement blurb:\n*(Use `{time}` and `{server_time}` as placeholders)*",
            max_chars=1000,
        )
        if blurb_raw is None:
            return
        blurb = blurb_raw.strip() or preview_blurb

    # ── 5-minute warning ────────────────────────────────────────────────────
    # Two questions, in the order the officer thinks them: do I want one for
    # this event, and if so what should it say. The wording question is
    # skipped entirely on a no -- asking someone to word a post that will
    # never fire is a question about nothing.
    #
    # Taking the default wording stores '' rather than the rendered line.
    # '' means "has not chosen", which is what lets this step honestly label
    # the generic line as the default instead of showing it back as a saved
    # value they picked, and it means a future change to
    # WARNING_BLURB_DEFAULT reaches everyone who never overrode it.

    class _WarningOnOffView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.choice: Optional[bool] = None

        @discord.ui.button(label=_WARN_BTN_ON, style=discord.ButtonStyle.success)
        async def want_it(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.choice = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label=_WARN_BTN_OFF, style=discord.ButtonStyle.secondary)
        async def skip_it(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.choice = False
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ No 5-minute warning for **{name}**.",
                view=self,
            )
            self.stop()

    onoff_view = _WarningOnOffView()
    await channel.send(
        f"**{name}: 5-Minute Warning**\n"
        "Do you want a heads-up posted 5 minutes before this event starts?",
        view=onoff_view,
    )
    await wizard_registry.wait_view_or_cancel(onoff_view, cancel_event)
    if cancel_event.is_set():
        return
    if onoff_view.choice is None:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="events"))
        wizard_registry.unregister(user.id, cancel_event)
        return

    five_min_warning = 1 if onoff_view.choice else 0
    warning_blurb = ""
    if five_min_warning:
        preview_warning = WARNING_BLURB_DEFAULT.format(name=name)

        class _WarningChoiceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.choice: Optional[str] = None

            @discord.ui.button(label="✅ Use default warning", style=discord.ButtonStyle.success)
            async def use_default(self, inter: discord.Interaction, _b: discord.ui.Button):
                self.choice = "default"
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"✅ Using default warning:\n`{preview_warning}`",
                    view=self,
                )
                self.stop()

            @discord.ui.button(label="✏️ Enter my own", style=discord.ButtonStyle.secondary)
            async def enter_own(self, inter: discord.Interaction, _b: discord.ui.Button):
                self.choice = "custom"
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

        warning_view = _WarningChoiceView()
        await channel.send(
            f"**{name}: What the warning says**\n"
            "Use `{time}` for the event time in your timezone and `{server_time}` "
            "for Server Time.\n\n"
            f"**Default:** `{preview_warning}`",
            view=warning_view,
        )
        await wizard_registry.wait_view_or_cancel(warning_view, cancel_event)
        if cancel_event.is_set():
            return
        if not warning_view.choice:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="events"))
            wizard_registry.unregister(user.id, cancel_event)
            return

        if warning_view.choice == "custom":
            warning_raw = await ask_text(
                "Enter your 5-minute warning:\n"
                "*(Use `{time}` and `{server_time}` as placeholders)*",
                max_chars=1000,
            )
            if warning_raw is None:
                return
            warning_blurb = warning_raw.strip()

    # ── Save ────────────────────────────────────────────────────────────────
    event = {
        "short_key": short_key,
        "name": name,
        "timezone": tz,
        "default_time": default_time,
        "announcement_blurb": blurb,
        "warning_blurb": warning_blurb,
        "schedule_type": schedule_type,
        "anchor_date": anchor_date,
        "interval_days": interval_days,
        "draft_channel_id": draft_channel_id,
        "announcement_channel_id": announce_channel_id,
        "draft_time": draft_time,
        "five_min_warning": five_min_warning,
        "active": 1,
    }
    save_guild_event(guild_id, event)
    await channel.send(
        f"✅ Added **{name}**.\n"
        f"Open `{EVENTS_HUB_CMD}` again to see it in your event list, "
        "or click **📅 Today's events** to draft today's announcement."
    )
    wizard_registry.unregister(user.id, cancel_event)
    logger.info("[EVENTS HUB] Created event %s for guild %s", short_key, guild_id)


# ── Pause / resume flow ──────────────────────────────────────────────────────
#
# Pausing is the reversible stop an alliance actually wants between
# seasons: the row keeps its name, blurb, time, anchor and interval, and
# every runtime reader filters on `active = 1`, so nothing fires while
# it's off. Resuming a *repeating* event offers a fresh anchor date,
# because the in-game cycle routinely shifts over a season break and
# `scheduler.next_event_dates` derives every future firing from the
# anchor — resuming onto a stale cycle would post on the wrong days.


# Button labels for the pause/resume confirmation step. Module-level so
# the modal's retry copy can name the button the officer needs to click
# without the two strings drifting apart.
_RESUME_BTN_KEEP = "▶️ Resume with this schedule"
_RESUME_BTN_REANCHOR = "📅 Set a new anchor date"
_PAUSE_BTN_CONFIRM = "⏸️ Yes, pause it"


class _AnchorDateModal(discord.ui.Modal):
    """Re-anchor a repeating event and resume it in one submit.

    A modal rather than a channel prompt so re-anchoring stays inside the
    ephemeral hub surface — no public wizard messages, and no `wait_for`
    timeout to lose the flow to. A date we can't parse leaves the picker
    message and its buttons intact, so the officer just clicks through
    again instead of restarting.
    """

    def __init__(self, guild_id: int, short_key: str, name: str, current_anchor: str):
        super().__init__(title=f"Anchor date: {name}"[:45])
        self.guild_id = guild_id
        self.short_key = short_key
        self.event_name = name
        self.field = discord.ui.TextInput(
            label="When did this event last run?",
            placeholder="March 30 · 7/30 · 2026-07-30 · today",
            default=current_anchor or None,
            required=True,
            max_length=40,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from config import get_guild_event, set_guild_event_active, set_guild_event_anchor
        from setup_cog import _parse_month_day

        raw = self.field.value.strip()
        parsed = _parse_month_day(raw)
        if not parsed:
            await interaction.response.send_message(
                DATE_PARSE_REJECT.format(raw=raw, examples=ANCHOR_DATE_EXAMPLES)
                + f" **{_RESUME_BTN_REANCHOR}** is still there, click it to try again.",
                ephemeral=True,
            )
            return

        set_guild_event_anchor(self.guild_id, self.short_key, parsed)
        set_guild_event_active(self.guild_id, self.short_key, True)
        ev = get_guild_event(self.guild_id, self.short_key) or {}
        await interaction.response.edit_message(
            content=(
                f"▶️ Resumed **{self.event_name}**, anchored to {parsed}.\n"
                f"{describe_event_schedule(ev)}"
            ),
            view=None,
        )
        logger.info(
            "[EVENTS HUB] Resumed event %s for guild %s with new anchor %s",
            self.short_key,
            self.guild_id,
            parsed,
        )


async def _open_pause_picker(interaction: discord.Interaction) -> None:
    """Dropdown over every event — active and paused — that toggles the
    picked one. Active events pause after a confirm; paused repeating
    events get a resume-as-is / re-anchor choice; paused manual events
    resume straight away (they have no cycle to drift)."""
    from config import get_guild_event, get_guild_events, set_guild_event_active

    guild_id = interaction.guild_id
    events = get_guild_events(guild_id, active_only=False)
    if not events:
        await interaction.response.send_message(
            f"ℹ️ No events configured yet. Click **{EVENTS_HUB_BTN_CREATE}** to add one first.",
            ephemeral=True,
        )
        return

    today = date_cls.today()
    # Active first, then paused — matches the hub embed's reading order.
    events.sort(key=lambda e: 0 if e.get("active") else 1)
    options = [
        discord.SelectOption(
            label=e["name"][:100],
            value=e["short_key"],
            emoji="▶️" if e.get("active") else "⏸️",
            description=(
                describe_event_schedule(e, today=today)[:100]
                if e.get("active")
                else "Paused - pick to turn it back on"
            ),
        )
        for e in events[:25]
    ]
    select = discord.ui.Select(placeholder="Pick an event to pause or resume…", options=options)
    view = discord.ui.View(timeout=180)
    view.add_item(select)

    async def on_pick(inter: discord.Interaction):
        chosen_key = inter.data["values"][0]
        ev = get_guild_event(guild_id, chosen_key) or {}
        name = ev.get("name") or chosen_key
        is_active = bool(ev.get("active"))
        confirm = discord.ui.View(timeout=180)

        async def do_pause(c_inter: discord.Interaction):
            set_guild_event_active(guild_id, chosen_key, False)
            await c_inter.response.edit_message(
                content=(
                    f"⏸️ Paused **{name}**. It stops posting immediately and keeps "
                    f"every setting.\nTurn it back on any time from "
                    f"**{EVENTS_HUB_BTN_PAUSE}**."
                ),
                view=None,
            )
            logger.info("[EVENTS HUB] Paused event %s for guild %s", chosen_key, guild_id)

        async def do_resume(c_inter: discord.Interaction):
            set_guild_event_active(guild_id, chosen_key, True)
            fresh = get_guild_event(guild_id, chosen_key) or {}
            await c_inter.response.edit_message(
                content=f"▶️ Resumed **{name}**.\n{describe_event_schedule(fresh)}",
                view=None,
            )
            logger.info("[EVENTS HUB] Resumed event %s for guild %s", chosen_key, guild_id)

        async def do_reanchor(c_inter: discord.Interaction):
            await c_inter.response.send_modal(
                _AnchorDateModal(guild_id, chosen_key, name, ev.get("anchor_date") or "")
            )

        async def do_cancel(c_inter: discord.Interaction):
            await c_inter.response.edit_message(
                content=CANCEL_BACKPEDAL.format(detail=f"**{name}** is unchanged."),
                view=None,
            )

        if is_active:
            prompt = (
                f"**{name}** is running - {describe_event_schedule(ev, today=today)}\n\n"
                "Pausing stops it posting but keeps every setting, so you can "
                "turn it back on later."
            )
            buttons = [
                (_PAUSE_BTN_CONFIRM, discord.ButtonStyle.primary, do_pause),
                ("↩️ Cancel", discord.ButtonStyle.secondary, do_cancel),
            ]
        elif ev.get("schedule_type") == "repeating":
            prompt = (
                f"**{name}** is paused. On its saved schedule it would next fire:\n"
                f"{describe_event_schedule(ev, today=today)}\n\n"
                "If the in-game cycle shifted while it was off, set a new anchor "
                "date instead - the anchor is what every future date is counted from."
            )
            buttons = [
                (_RESUME_BTN_KEEP, discord.ButtonStyle.success, do_resume),
                (_RESUME_BTN_REANCHOR, discord.ButtonStyle.primary, do_reanchor),
                ("↩️ Cancel", discord.ButtonStyle.secondary, do_cancel),
            ]
        else:
            prompt = (
                f"**{name}** is paused. It's a manual event, so resuming just "
                "makes it available in the draft editor again."
            )
            buttons = [
                ("▶️ Resume it", discord.ButtonStyle.success, do_resume),
                ("↩️ Cancel", discord.ButtonStyle.secondary, do_cancel),
            ]

        for label, style, callback in buttons:
            btn = discord.ui.Button(label=label[:80], style=style)
            btn.callback = callback
            confirm.add_item(btn)

        await inter.response.edit_message(content=prompt, view=confirm)

    select.callback = on_pick
    await interaction.response.send_message(
        "Pick an event to pause or resume:",
        view=view,
        ephemeral=True,
    )


# ── Edit 5-minute warning flow ───────────────────────────────────────────────────
#
# The create wizard asks for a 5-minute warning, but an alliance only walks
# that wizard once per event, and every event that existed before #566 shipped
# never saw the question. Without this surface the feature would be reachable
# only by deleting an event and rebuilding it, which costs the alliance its
# anchor date and its announcement wording to change one line of text.


# Button labels for the pick-then-choose step. Module-level so the confirm
# copy and the buttons cannot drift apart, matching the pause flow above.
_WARN_BTN_EDIT = "✏️ Change the wording"
_WARN_BTN_OFF = "🔕 Disable warning for this event"
_WARN_BTN_ON = "🔔 Enable 5-minute warning"


def _default_warning_for(name: str) -> str:
    """The default line, rendered for one event. Local import because
    scheduler imports this module at module level."""
    from scheduler import WARNING_BLURB_DEFAULT

    return WARNING_BLURB_DEFAULT.format(name=name)


class _WarningBlurbModal(discord.ui.Modal):
    """Edit one event's 5-minute warning text.

    A modal rather than a channel prompt, for the same reason
    `_AnchorDateModal` is one: the flow stays inside the ephemeral hub, with
    no public wizard messages and no `wait_for` timeout to lose it to.

    Submitting an empty field is a real action, not a cancel. It clears the
    row back to '' and the warning returns to the default, which is the only
    way back once an alliance has written their own.
    """

    def __init__(self, guild_id: int, short_key: str, name: str, current: str):
        # Local, like every other scheduler import in this module: scheduler
        # imports events_hub at module level, so the reverse cannot be.
        from scheduler import WARNING_BLURB_DEFAULT

        super().__init__(title=f"5-minute warning: {name}"[:45])
        self.guild_id = guild_id
        self.short_key = short_key
        self.event_name = name
        self.field = discord.ui.TextInput(
            label="What should the warning say?",
            style=discord.TextStyle.paragraph,
            # The default line as placeholder, so the officer can see what
            # they get by leaving it blank without it looking like a value
            # they already chose.
            placeholder=WARNING_BLURB_DEFAULT.format(name=name)[:100],
            default=current or None,
            required=False,
            max_length=1000,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from config import set_guild_event_warning_blurb
        from scheduler import WARNING_BLURB_DEFAULT

        text = (self.field.value or "").strip()
        set_guild_event_warning_blurb(self.guild_id, self.short_key, text)

        if text:
            preview = text.replace("{name}", self.event_name)
            body = (
                f"✅ Updated the 5-minute warning for **{self.event_name}**.\n"
                f"It will post: `{preview}`"
            )
        else:
            body = (
                f"✅ Cleared the custom warning for **{self.event_name}**.\n"
                "It goes back to: "
                f"`{WARNING_BLURB_DEFAULT.format(name=self.event_name)}`"
            )
        await interaction.response.edit_message(content=body, view=None)
        logger.info(
            "[EVENTS HUB] Warning blurb %s for event %s, guild %s",
            "set" if text else "cleared",
            self.short_key,
            self.guild_id,
        )


def _warning_summary(event: dict) -> str:
    """The one line under an event in the picker.

        5-minute warning: Active - Custom
        5-minute warning: Active - Default
        5-minute warning: Off

    State first, because state is the thing that varies and the thing
    that decides whether wording it is worth doing at all. An earlier
    version put the wording itself here; it answered "which one is this"
    at the cost of the answer to "does this one even fire", and for an
    alliance with a handful of events the second question is the live one.

    Custom against Default stays because it is cheap and still says which
    events have been worded.
    """
    if not event.get("five_min_warning"):
        return "5-minute warning: Off"
    worded = "Custom" if (event.get("warning_blurb") or "").strip() else "Default"
    return f"5-minute warning: Active - {worded}"


async def _open_warning_picker(interaction: discord.Interaction) -> None:
    """Dropdown over every event, then on/off and wording for the picked one.

    Paused events are listed too: settling an event's warning while it is
    off between seasons is exactly the sort of tidying that gets done then.
    They carry no running/paused marker here, unlike the pause and delete
    pickers. Two on/off states on one row -- the event's, and the
    warning's -- read as one state and got the wrong one believed.

    The two controls sit behind one button on purpose. "Do I want a warning
    for this event" and "what should it say" are the same thought, and
    splitting them would put the on/off switch on a surface an officer only
    reaches by first deciding they want to edit wording.
    """
    from config import get_guild_event, get_guild_events, set_guild_event_five_min_warning

    guild_id = interaction.guild_id
    events = get_guild_events(guild_id, active_only=False)
    if not events:
        await interaction.response.send_message(
            f"ℹ️ No events yet. Click **{EVENTS_HUB_BTN_CREATE}** to add one first.",
            ephemeral=True,
        )
        return

    options = [
        discord.SelectOption(
            label=e["name"][:100],
            value=e["short_key"],
            description=_warning_summary(e)[:100],
        )
        for e in events[:25]
    ]
    select = discord.ui.Select(placeholder="Pick an event", options=options)
    view = discord.ui.View(timeout=180)
    view.add_item(select)

    async def on_pick(inter: discord.Interaction):
        chosen_key = inter.data["values"][0]
        ev = get_guild_event(guild_id, chosen_key) or {}
        name = ev.get("name") or chosen_key
        warning_on = bool(ev.get("five_min_warning"))
        current = ev.get("warning_blurb") or ""

        choice = discord.ui.View(timeout=180)

        async def do_edit(c_inter: discord.Interaction):
            await c_inter.response.send_modal(
                _WarningBlurbModal(guild_id, chosen_key, name, current)
            )

        async def do_turn_off(c_inter: discord.Interaction):
            # The wording is kept, and the confirmation deliberately does not
            # say so (#566 sign-off): turning it back on shows what will post,
            # which demonstrates it rather than promising it.
            set_guild_event_five_min_warning(guild_id, chosen_key, False)
            await c_inter.response.edit_message(
                content=f"🔕 No 5-minute warning for **{name}** any more.",
                view=None,
            )
            logger.info(
                "[EVENTS HUB] 5-min warning off for event %s, guild %s", chosen_key, guild_id
            )

        async def do_turn_on(c_inter: discord.Interaction):
            set_guild_event_five_min_warning(guild_id, chosen_key, True)
            from scheduler import WARNING_BLURB_DEFAULT

            posts = current.strip() or WARNING_BLURB_DEFAULT.format(name=name)
            await c_inter.response.edit_message(
                content=(
                    f"🔔 **{name}** warns 5 minutes before it starts.\nIt will post: `{posts}`"
                ),
                view=None,
            )
            logger.info(
                "[EVENTS HUB] 5-min warning on for event %s, guild %s", chosen_key, guild_id
            )

        async def do_cancel(c_inter: discord.Interaction):
            await c_inter.response.edit_message(
                content=CANCEL_BACKPEDAL.format(detail=f"**{name}** is unchanged."),
                view=None,
            )

        if warning_on:
            worded = "your own wording" if current.strip() else "the default wording"
            prompt = (
                f"**{name}** warns 5 minutes before it starts, using {worded}.\n"
                f"`{current.strip() or _default_warning_for(name)}`"
            )
            buttons = [
                (_WARN_BTN_EDIT, discord.ButtonStyle.primary, do_edit),
                (_WARN_BTN_OFF, discord.ButtonStyle.secondary, do_turn_off),
                ("↩️ Cancel", discord.ButtonStyle.secondary, do_cancel),
            ]
        else:
            prompt = (
                f"**{name}** has no 5-minute warning. Turning it on posts "
                f"5 minutes before it starts."
            )
            buttons = [
                (_WARN_BTN_ON, discord.ButtonStyle.success, do_turn_on),
                ("↩️ Cancel", discord.ButtonStyle.secondary, do_cancel),
            ]

        for label, style, callback in buttons:
            btn = discord.ui.Button(label=label[:80], style=style)
            btn.callback = callback
            choice.add_item(btn)

        await inter.response.edit_message(content=prompt, view=choice)

    select.callback = on_pick
    await interaction.response.send_message(
        "Pick an event to change its 5-minute warning:",
        view=view,
        ephemeral=True,
    )


# ── Delete flow ──────────────────────────────────────────────────────────────


async def _open_delete_picker(interaction: discord.Interaction) -> None:
    """Dropdown over every event, then a confirmation step before the row
    is permanently removed.

    Delete is the irreversible door; **{EVENTS_HUB_BTN_PAUSE}** is the
    reversible one, and the confirm copy says so. Paused events are listed
    too — an event you paused and then decided you're done with is exactly
    the thing you'd come here to clear out."""
    from config import get_guild_events, delete_guild_event, get_guild_event

    events = get_guild_events(interaction.guild_id, active_only=False)
    if not events:
        await interaction.response.send_message(
            f"ℹ️ No events to delete. Click **{EVENTS_HUB_BTN_CREATE}** to add one first.",
            ephemeral=True,
        )
        return

    options = [
        discord.SelectOption(
            label=e["name"][:100],
            value=e["short_key"],
            emoji="▶️" if e.get("active") else "⏸️",
            description=None if e.get("active") else "Currently paused",
        )
        for e in events[:25]
    ]
    select = discord.ui.Select(placeholder="🗑️ Pick an event to delete…", options=options)
    view = discord.ui.View(timeout=180)
    view.add_item(select)

    async def on_pick(inter: discord.Interaction):
        chosen_key = inter.data["values"][0]
        ev = get_guild_event(interaction.guild_id, chosen_key)
        name = (ev or {}).get("name") or chosen_key

        confirm = discord.ui.View(timeout=60)
        yes_btn = discord.ui.Button(
            label="🗑️ Yes, delete permanently", style=discord.ButtonStyle.danger
        )
        no_btn = discord.ui.Button(label="↩️ Cancel", style=discord.ButtonStyle.secondary)

        async def do_delete(c_inter: discord.Interaction):
            delete_guild_event(interaction.guild_id, chosen_key)
            for item in confirm.children:
                item.disabled = True
            await c_inter.response.edit_message(
                content=f"🗑️ Deleted **{name}** permanently.",
                view=confirm,
            )
            confirm.stop()
            logger.info(
                "[EVENTS HUB] Deleted event %s for guild %s",
                chosen_key,
                interaction.guild_id,
            )

        async def do_cancel(c_inter: discord.Interaction):
            for item in confirm.children:
                item.disabled = True
            await c_inter.response.edit_message(
                content=CANCEL_BACKPEDAL.format(detail=f"**{name}** was not deleted."),
                view=confirm,
            )
            confirm.stop()

        yes_btn.callback = do_delete
        no_btn.callback = do_cancel
        confirm.add_item(yes_btn)
        confirm.add_item(no_btn)

        select.disabled = True
        await inter.response.edit_message(view=view)
        await inter.followup.send(
            f"Delete **{name}** permanently? Its name, blurb, time and "
            "schedule are gone for good and this can't be undone.\n"
            f"To stop it for a season and keep everything, use "
            f"**{EVENTS_HUB_BTN_PAUSE}** instead.",
            view=confirm,
            ephemeral=True,
        )

    select.callback = on_pick
    await interaction.response.send_message(
        "Pick an event to delete:",
        view=view,
        ephemeral=True,
    )


# ── Entry point ──────────────────────────────────────────────────────────────


async def handle_events_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/events`. Leadership-gated via the same
    guard the previous /events subcommands used."""
    from bot import guard

    if not await guard(interaction):
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "⚠️ This command must be used inside a server.",
            ephemeral=True,
        )
        return

    embed = await asyncio.to_thread(_build_events_hub_embed, guild)
    view = _EventsHubView(
        bot=bot,
        guild_id=guild.id,
        owner_user_id=interaction.user.id,
    )

    if interaction.response.is_done():
        sent = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        sent = await interaction.original_response()
    view.message = sent

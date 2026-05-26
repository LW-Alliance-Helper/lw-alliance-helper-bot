"""
events_hub.py: the single `/events` event-hub entry point that
replaced the three `/events overview|show|log` subcommands plus the
event-list management section of the `/setup → 📣 Events` wizard
(#249).

Officers hit one command. The hub embed surfaces the alliance's
current event config + a button grid that dispatches into every
existing event flow:

  - 📅 Today's events  → scheduler.EventEditorView for today
  - 📆 Upcoming events → cycle projections (lifted from /events overview)
  - 📜 Event log       → recent approved posts (lifted from /events log)
  - ➕ Create an event → preset picker OR define-your-own free-text flow
  - 🗑️ Delete an event → picker over guild_events + soft-delete

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
    DENY_NOT_OWNER,
    GENERIC_CMD_TIMEOUT,
    LEADERSHIP_INACCESSIBLE,
    LEADERSHIP_NO_READ_PERM,
    LEADERSHIP_NOT_CONFIGURED,
)

logger = logging.getLogger(__name__)


# ── Button labels (exported) ─────────────────────────────────────────────────
#
# Single source of truth for the hub's button labels. Imported by every
# module that quotes a button name in error / timeout / followup copy,
# so a rename here updates every caller automatically. Matches the
# HUB_BTN_* pattern in storm_event_hub.py.

EVENTS_HUB_TITLE        = "📣 Event Announcements"
EVENTS_HUB_CMD          = "/events"
EVENTS_HUB_BTN_TODAY    = "📅 Today's events"
EVENTS_HUB_BTN_UPCOMING = "📆 Upcoming events"
EVENTS_HUB_BTN_LOG      = "📜 Event log"
EVENTS_HUB_BTN_CREATE   = "➕ Create an event"
EVENTS_HUB_BTN_DELETE   = "🗑️ Delete an event"


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
        "key":           "ae_plague_marauder",
        "name":          "Alliance Exercise: Plague Marauder",
        "stage_note":    "S5 Off-season and later · every 3 days",
        "blurb":         _DEFAULT_BLURB,
        "interval_days": 3,
    },
    {
        "key":           "ae_marshalls_guard",
        "name":          "Alliance Exercise: Marshall's Guard",
        "stage_note":    "Early seasons (pre-S3) · every 3 days",
        "blurb":         _DEFAULT_BLURB,
        "interval_days": 3,
    },
    {
        "key":           "ae_sandworm",
        "name":          "Alliance Exercise: Sandworm",
        "stage_note":    "Seasons 3 and 4 · every 3 days",
        "blurb":         _DEFAULT_BLURB,
        "interval_days": 3,
    },
    {
        "key":           "zombie_siege",
        "name":          "Zombie Siege",
        "stage_note":    "Alliance defense · every 3 days",
        "blurb":         _DEFAULT_BLURB,
        "interval_days": 3,
    },
    {
        "key":           "glacieradon",
        "name":          "Glacieradon",
        "stage_note":    "Pairs with Gold Zombies · every other week if recurring",
        "blurb":         _DEFAULT_BLURB,
        "interval_days": 14,
    },
    {
        "key":           "sky_predator",
        "name":          "Sky Predator",
        "stage_note":    "Pairs with General's Trials · every other week if recurring",
        "blurb":         _DEFAULT_BLURB,
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


def _build_events_hub_embed(guild: discord.Guild) -> discord.Embed:
    """Build the hub embed showing the alliance's current event config
    plus a one-glance "what's available right now" summary.

    Reads from `guild_events` and `guild_configs`. Skips per-event
    next-firing-date computation when nothing is configured — the hub
    is also the discovery surface for new alliances, so the empty
    state needs to render usefully."""
    import config
    from scheduler import next_event_dates

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
        events = config.get_guild_events(guild.id, active_only=True)
    except Exception:
        events = []

    # Foundation block: channels + draft cadence.
    draft_id    = cfg.event_draft_channel_id if cfg else 0
    announce_id = cfg.event_announce_channel_id if cfg else 0
    draft_time  = cfg.event_draft_time if cfg else None
    warn_on     = cfg.event_five_min_warning if cfg else None

    config_lines = []
    config_lines.append(f"**Draft channel:** {f'<#{draft_id}>' if draft_id else '*not set*'}")
    config_lines.append(f"**Announcement channel:** {f'<#{announce_id}>' if announce_id else '*not set*'}")
    config_lines.append(f"**Draft time:** {draft_time or '*not set*'}")
    config_lines.append(f"**5-min warning:** {'on' if warn_on else 'off'}")
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
        lines = []
        for ev in events:
            name = ev.get("name") or "(unnamed)"
            if ev["schedule_type"] == "repeating" and ev.get("anchor_date"):
                try:
                    anchor   = date_cls.fromisoformat(ev["anchor_date"])
                    interval = int(ev["interval_days"] or 0)
                    upcoming = next_event_dates(
                        from_date=today, count=1, anchor=anchor, cycle=interval,
                    ) if interval > 0 else []
                    if upcoming:
                        nxt  = upcoming[0]
                        days = (nxt - today).days
                        when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
                        lines.append(f"**{name}** - Next event instance: {nxt:%a %b} {nxt.day} ({when}) - every {interval} days")
                    else:
                        lines.append(f"**{name}** - Recurring every {interval} days (next instance not yet computable)")
                except (ValueError, TypeError):
                    lines.append(f"**{name}** - Schedule invalid (re-create the event)")
            else:
                lines.append(f"**{name}** - Manual (add it to a draft from the editor)")
        embed.add_field(
            name=f"Events ({len(events)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )

    return embed


# ── Hub view ─────────────────────────────────────────────────────────────────


class _EventsHubView(discord.ui.View):
    """Hub button grid. Each button dispatches into the matching flow.

    Layout (2 rows, 5 buttons):
        Row 0 (read surfaces):
          📅 Today's events (blue) | 📆 Upcoming events (secondary) |
          📜 Event log (secondary)
        Row 1 (write surfaces):
          ➕ Create an event (green) | 🗑️ Delete an event (red)

    The two write surfaces sit on their own row so they don't visually
    compete with the read-only buttons above. Today's events takes the
    primary-blue style since that's the most common "I'm about to
    publish today's draft" action.
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
        self._add(EVENTS_HUB_BTN_TODAY,    discord.ButtonStyle.primary,   0, self._on_today)
        self._add(EVENTS_HUB_BTN_UPCOMING, discord.ButtonStyle.secondary, 0, self._on_upcoming)
        self._add(EVENTS_HUB_BTN_LOG,      discord.ButtonStyle.secondary, 0, self._on_log)
        # Row 1: write surfaces
        self._add(EVENTS_HUB_BTN_CREATE,   discord.ButtonStyle.success,   1, self._on_create)
        self._add(EVENTS_HUB_BTN_DELETE,   discord.ButtonStyle.danger,    1, self._on_delete)

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
    cfg      = get_config(guild_id)
    events   = get_guild_events(guild_id, active_only=True)
    today    = date_cls.today()

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
        await interaction.followup.send(
            "ℹ️ No repeating events configured. The event editor only "
            "applies to events with a recurring schedule.",
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
    days_diff  = (event_date - today).days
    if days_diff > 0:
        await interaction.followup.send(
            f"ℹ️ **{today:%B} {today.day}** is not an event day. "
            f"Showing the next event date: **{event_date:%A, %B} {event_date.day}**.",
            ephemeral=True,
        )

    event_list: list[dict] = []
    draft_channel_id    = 0
    announce_channel_id = 0
    five_min_warn       = False
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
                ev_tz    = ZoneInfo(ev["timezone"])
                t_h, t_m = (int(p) for p in ev["default_time"].split(":")[:2])
                ev_dt    = datetime(event_date.year, event_date.month, event_date.day, t_h, t_m, tzinfo=ev_tz)
                event_list.append({
                    "key":   ev["short_key"],
                    "name":  ev["name"],
                    "dt":    ev_dt,
                    "blurb": ev["announcement_blurb"],
                })
                draft_channel_id    = ev["draft_channel_id"] or draft_channel_id
                announce_channel_id = ev["announcement_channel_id"] or announce_channel_id
                if ev["five_min_warning"]:
                    five_min_warn = True
            except Exception as e:
                logger.warning("[EVENTS HUB] Error processing event %s: %s", ev.get("short_key", "?"), e)

    if not event_list:
        await interaction.followup.send(
            "⚠️ No events to show on the next event date — likely a bad timezone "
            "or default_time on one of your configured events.",
            ephemeral=True,
        )
        return

    event_list.sort(key=lambda x: x["dt"])
    event_key = f"event-{guild_id}-{event_date.isoformat()}-hub"
    await post_editor(
        bot, event_list, event_key, event_date,
        cfg=cfg,
        draft_channel_id=draft_channel_id,
        announcement_channel_id=announce_channel_id,
        five_min_warning=five_min_warn,
    )


# ── Upcoming events: lifted from the old /events overview ────────────────────


async def _render_upcoming_followup(interaction: discord.Interaction) -> None:
    """Render the configured event types + their next firing dates.
    Lifted from the pre-hub /events overview slash so the read-only
    pre-flight content stays accessible without the subcommand."""
    from config import get_guild_events
    from scheduler import next_event_dates

    events = get_guild_events(interaction.guild_id, active_only=True)
    today  = date_cls.today()

    embed = discord.Embed(title="📆 Upcoming events", color=discord.Color.blurple())

    if not events:
        embed.description = (
            f"No event types configured yet. Click **{EVENTS_HUB_BTN_CREATE}** to add some."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    repeating_lines: list[str] = []
    manual_lines:    list[str] = []
    for ev in events:
        name = ev.get("name") or "(unnamed)"
        if ev["schedule_type"] == "repeating" and ev.get("anchor_date"):
            try:
                anchor   = date_cls.fromisoformat(ev["anchor_date"])
                interval = int(ev["interval_days"] or 0)
            except (ValueError, TypeError):
                repeating_lines.append(f"• **{name}** — schedule invalid")
                continue
            upcoming = next_event_dates(
                from_date=today, count=1, anchor=anchor, cycle=interval,
            ) if interval > 0 else []
            if upcoming:
                nxt  = upcoming[0]
                days = (nxt - today).days
                when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
                repeating_lines.append(
                    f"• **{name}** — every {interval}d, next on "
                    f"{nxt:%a %b} {nxt.day} ({when})"
                )
            else:
                repeating_lines.append(f"• **{name}** — every {interval}d")
        else:
            manual_lines.append(f"• **{name}** — manual entries only")

    if repeating_lines:
        embed.add_field(name=f"Repeating ({len(repeating_lines)})", value="\n".join(repeating_lines)[:1024], inline=False)
    if manual_lines:
        embed.add_field(name=f"Manual ({len(manual_lines)})", value="\n".join(manual_lines)[:1024], inline=False)

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
            LEADERSHIP_INACCESSIBLE, ephemeral=True,
        )
        return

    days   = await premium.get_limit("events_log_days", interaction.guild_id, interaction=interaction, bot=bot)
    days   = days or 30
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
        embed.add_field(name="No approvals found", value=f"*No event posts have been approved in the past {days} days.*", inline=False)
    else:
        lines = []
        for msg in matches[:25]:
            header   = msg.content.split("\n", 1)[0]
            ldt      = msg.created_at.astimezone(ET)
            hr12     = ldt.hour % 12 or 12
            local_dt = f"{ldt:%a %b} {ldt.day}, {hr12}:{ldt:%M%p} ET".replace("AM", "am").replace("PM", "pm")
            lines.append(f"• {header} *— logged {local_dt}*")
        embed.add_field(name=f"Approvals ({len(matches)})", value="\n".join(lines)[:1024], inline=False)

    if days < 30:
        embed.set_footer(text="Free tier: 7-day window. Upgrade to Premium for 30 days.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Create flow: preset picker -> wizard ─────────────────────────────────────


class _CreatePickerView(discord.ui.View):
    """Two equally-weighted entry buttons: 🎯 Pick a preset, ✏️ Define
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

    @discord.ui.button(label="🎯 Pick a preset", style=discord.ButtonStyle.primary, row=0)
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
    cap    = await premium.get_limit(
        "events", interaction.guild_id, interaction=interaction, bot=bot,
    )
    if cap is not None and len(events) >= cap:
        await interaction.response.send_message(
            embed=premium.limit_reached_embed(
                feature_label="Event Announcements",
                current=len(events), cap=cap, plural_unit="events",
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
    view   = discord.ui.View(timeout=180)
    view.add_item(select)

    async def on_pick(inter: discord.Interaction):
        chosen_key = inter.data["values"][0]
        preset     = _preset_by_key(chosen_key)
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
        get_config, get_or_create_config, save_guild_event, get_guild_events,
    )
    from setup_cog import _parse_12h_time, _parse_month_day

    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    guild_cfg = get_config(guild_id) or get_or_create_config(guild_id)
    tz        = guild_cfg.timezone or "America/New_York"

    # Pull the events-wide settings already saved on guild_configs so
    # we can stamp the new event with them. Officers configure these
    # via /setup → 📣 Events (the wizard for channels + draft time
    # still lives there; only event-list management moved to the hub).
    draft_channel_id    = guild_cfg.event_draft_channel_id or 0
    announce_channel_id = guild_cfg.event_announce_channel_id or 0
    draft_time          = guild_cfg.event_draft_time or "12:00"
    five_min_warning    = guild_cfg.event_five_min_warning if guild_cfg.event_five_min_warning is not None else 1

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
            "Pick **📅 Manual** at the schedule step if you run this event "
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
            await channel.send("⚠️ Empty name — cancelled.")
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
    default_time  = None
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
        if (len(time_raw) == 5 and time_raw[2] == ":"
                and time_raw.replace(":", "").isdigit()):
            default_time = time_raw
            break
        attempts_left -= 1
        if attempts_left <= 0:
            await channel.send(
                f"⚠️ Could not read that time after a few tries. "
                f"Run `{EVENTS_HUB_CMD}` → **{EVENTS_HUB_BTN_CREATE}** to start over."
            )
            wizard_registry.unregister(user.id, cancel_event)
            return
        await channel.send(
            f"⚠️ Could not read **`{time_raw}`** as a time. "
            f"Try `10:15pm`, `9:00am`, or `21:00`. Let's try once more."
        )

    # ── Schedule: repeating vs manual ────────────────────────────────────────
    class _ScheduleView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.selected: Optional[str] = None

        @discord.ui.button(label="🔁 Repeating", style=discord.ButtonStyle.primary)
        async def repeating(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.selected = "repeating"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="📅 Manual", style=discord.ButtonStyle.secondary)
        async def manual(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.selected = "manual"
            for item in self.children: item.disabled = True
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

    anchor_date   = ""
    interval_days = (preset["interval_days"] if preset else 7)

    if schedule_type == "repeating":
        anchor_raw = await ask_text(
            f"**{name} — Anchor Date**\n"
            "Enter a recent or upcoming date when this event occurs. "
            "Type the month and day (e.g. `March 30`, `April 14`)."
        )
        if not anchor_raw:
            return
        parsed_anchor = _parse_month_day(anchor_raw)
        if not parsed_anchor:
            await channel.send(
                f"⚠️ Could not read that date. Try `March 30`. Run "
                f"`{EVENTS_HUB_CMD}` → **{EVENTS_HUB_BTN_CREATE}** to try again."
            )
            wizard_registry.unregister(user.id, cancel_event)
            return
        anchor_date = parsed_anchor

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
                    f"⚠️ Please enter a whole number. Run `{EVENTS_HUB_CMD}` to try again."
                )
                wizard_registry.unregister(user.id, cancel_event)
                return

    # ── Blurb ───────────────────────────────────────────────────────────────
    preset_blurb  = preset["blurb"] if preset else _DEFAULT_BLURB
    default_blurb = preset_blurb.format(
        name="{name}", time="{time}", server_time="{server_time}",
    ) if "{name}" in preset_blurb else preset_blurb
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
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content=f"✅ Using default blurb:\n`{preview_blurb}`", view=self,
            )
            self.stop()

        @discord.ui.button(label="✏️ Enter my own", style=discord.ButtonStyle.secondary)
        async def enter_own(self, inter: discord.Interaction, _b: discord.ui.Button):
            self.choice = "custom"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

    blurb_view = _BlurbChoiceView()
    await channel.send(
        f"**{name} — Announcement Blurb**\n"
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
            "Enter your announcement blurb:\n"
            "*(Use `{time}` and `{server_time}` as placeholders)*",
            max_chars=1000,
        )
        if blurb_raw is None:
            return
        blurb = blurb_raw.strip() or preview_blurb

    # ── Save ────────────────────────────────────────────────────────────────
    event = {
        "short_key":               short_key,
        "name":                    name,
        "timezone":                tz,
        "default_time":            default_time,
        "announcement_blurb":      blurb,
        "schedule_type":           schedule_type,
        "anchor_date":             anchor_date,
        "interval_days":           interval_days,
        "draft_channel_id":        draft_channel_id,
        "announcement_channel_id": announce_channel_id,
        "draft_time":              draft_time,
        "five_min_warning":        five_min_warning,
        "active":                  1,
    }
    save_guild_event(guild_id, event)
    await channel.send(
        f"✅ Added: **{name}**\n"
        f"Open `{EVENTS_HUB_CMD}` again to see it in your event list, "
        "or click **📅 Today's events** to draft today's announcement."
    )
    wizard_registry.unregister(user.id, cancel_event)
    logger.info("[EVENTS HUB] Created event %s for guild %s", short_key, guild_id)


# ── Delete flow ──────────────────────────────────────────────────────────────


async def _open_delete_picker(interaction: discord.Interaction) -> None:
    """Dropdown over active events, then a confirmation step before
    the soft-delete fires. Mirrors the delete flow inside the old
    setup-wizard step 5 but accessible directly from the hub."""
    from config import get_guild_events, delete_guild_event, get_guild_event

    events = get_guild_events(interaction.guild_id, active_only=True)
    if not events:
        await interaction.response.send_message(
            f"ℹ️ No events to delete. Click **{EVENTS_HUB_BTN_CREATE}** to add one first.",
            ephemeral=True,
        )
        return

    options = [
        discord.SelectOption(label=e["name"][:100], value=e["short_key"])
        for e in events[:25]
    ]
    select = discord.ui.Select(placeholder="🗑️ Pick an event to delete…", options=options)
    view   = discord.ui.View(timeout=180)
    view.add_item(select)

    async def on_pick(inter: discord.Interaction):
        chosen_key = inter.data["values"][0]
        ev         = get_guild_event(interaction.guild_id, chosen_key)
        name       = (ev or {}).get("name") or chosen_key

        confirm = discord.ui.View(timeout=60)
        yes_btn = discord.ui.Button(label="🗑️ Yes, delete", style=discord.ButtonStyle.danger)
        no_btn  = discord.ui.Button(label="↩️ Cancel",      style=discord.ButtonStyle.secondary)

        async def do_delete(c_inter: discord.Interaction):
            delete_guild_event(interaction.guild_id, chosen_key)
            for item in confirm.children: item.disabled = True
            await c_inter.response.edit_message(
                content=f"🗑️ Deleted: **{name}**.",
                view=confirm,
            )
            confirm.stop()
            logger.info(
                "[EVENTS HUB] Deleted event %s for guild %s",
                chosen_key, interaction.guild_id,
            )

        async def do_cancel(c_inter: discord.Interaction):
            for item in confirm.children: item.disabled = True
            await c_inter.response.edit_message(
                content=CANCEL_BACKPEDAL.format(detail=f"**{name}** was not deleted."),
                view=confirm,
            )
            confirm.stop()

        yes_btn.callback = do_delete
        no_btn.callback  = do_cancel
        confirm.add_item(yes_btn)
        confirm.add_item(no_btn)

        select.disabled = True
        await inter.response.edit_message(view=view)
        await inter.followup.send(
            f"Delete **{name}**? This soft-deletes the event — "
            "scheduled posts stop firing, but the row stays in the DB.",
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
    view  = _EventsHubView(
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

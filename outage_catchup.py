"""Outage catch-up digest (#227).

When the bot returns from an outage (a Railway redeploy that runs long, a
Discord gateway hiccup, or a real multi-hour downtime), every clock-driven
loop silently skips whatever was due while we were down. Leadership gets no
signal — the day's birthday announcement, shiny post, survey reminder, or
storm sign-up just never fires.

This module converts that silent loss into one-click recovery. On return it:

1. Detects the outage window from the per-minute loop heartbeats.
2. Scans each clock-driven surface for posts that were due during the window
   AND are still inside their per-surface catch-up window AND have not already
   fired (persistent dedup).
3. Posts a single digest to each affected guild's leadership channel offering
   to fire the missed items. Quiet outages with nothing to recover post nothing.

Detection model
---------------
The outage window is derived ONLY from the four reliable per-minute
``@tasks.loop`` loops (``shiny_post``, ``survey_reminder``, ``train_reminder``,
``storm_signup``), which tick every 60s. The ``scheduler`` loop sleeps a
variable amount (up to an hour when nothing is upcoming), so its gap is not a
reliable outage signal and is deliberately excluded from window detection —
including it would over-report the outage start whenever the bot died
mid-sleep. The scheduler still stamps a heartbeat for observability and its
own catch-up adapter, just not for the window math (see ``scheduler.py``).

Ordering
--------
Heartbeats are stamped at the END of each tick, and the loops start during
``on_ready``. So the scan must snapshot heartbeats at the very start of
``on_ready`` — BEFORE the loops start and overwrite them — and detect against
that snapshot after a short settle delay. ``snapshot_heartbeats()`` exists for
exactly this; ``run_catchup_scan()`` takes the snapshot it produced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import discord

import config

logger = logging.getLogger(__name__)

# The per-minute loops whose gap reliably bounds an outage window. The
# scheduler is intentionally absent (variable sleep — see module docstring).
HEARTBEAT_LOOPS = ("shiny_post", "survey_reminder", "train_reminder", "storm_signup")

# A gap larger than this marks a loop as having been offline. Sized so a
# normal Railway redeploy (sub-2-minute) never trips it — only real outages do.
OUTAGE_THRESHOLD = timedelta(minutes=5)

# How long after on_ready to wait before scanning, so the guild cache and
# channels are populated and the bot has settled.
SETTLE_DELAY_SECONDS = 60

_DEFAULT_TZ = "America/New_York"


# ── Data model ───────────────────────────────────────────────────────────────


@dataclass
class OutageWindow:
    """The detected offline span, both ends tz-aware UTC."""

    start: datetime
    end: datetime

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass
class MissedItem:
    """One recoverable post for the digest.

    ``title`` is the descriptive left side of the row ("Birthday announcement:
    Sally", "Shiny tasks post", 'Survey reminder "weekly check-in": DM to 32
    members'). ``scheduled_local`` is when it was due, in the guild's tz.
    ``destination`` is the trailing "sent to #x" / "sent individually to
    members" clause. ``fire`` performs the actual recovery post and returns
    True on success; it owns any fire-time Premium re-check.
    """

    surface: str
    title: str
    scheduled_local: datetime
    destination: str
    fire: Callable[[], Awaitable[bool]] = field(repr=False)


# ── Detection ────────────────────────────────────────────────────────────────


def snapshot_heartbeats() -> dict[str, Optional[datetime]]:
    """Capture the current heartbeat of every detection loop. Call this at the
    very start of ``on_ready``, before the loops start and overwrite them."""
    return {name: config.get_loop_heartbeat(name) for name in HEARTBEAT_LOOPS}


def detect_outage_window(
    snapshot: dict[str, Optional[datetime]],
    now: datetime,
) -> Optional[OutageWindow]:
    """Return the outage window implied by ``snapshot``, or None if no loop
    shows a gap past the threshold (quiet redeploy, fresh DB, or no outage).

    The window starts at the earliest gapped heartbeat (the widest gap — the
    loop that has gone longest without a clean tick) and ends at ``now`` (when
    we came back). A loop that has never stamped (``None``) carries no baseline
    and is ignored, so a first-ever boot never manufactures a phantom outage.
    """
    gapped = [
        last for last in snapshot.values() if last is not None and (now - last) > OUTAGE_THRESHOLD
    ]
    if not gapped:
        return None
    return OutageWindow(start=min(gapped), end=now)


# ── Time helpers ─────────────────────────────────────────────────────────────


def _guild_tz(cfg) -> ZoneInfo:
    try:
        return ZoneInfo((getattr(cfg, "timezone", "") or _DEFAULT_TZ))
    except Exception:
        return ZoneInfo(_DEFAULT_TZ)


def _parse_hhmm(raw: str) -> Optional[tuple[int, int]]:
    try:
        hh, mm = str(raw).split(":")
        hh, mm = int(hh), int(mm)
    except (ValueError, AttributeError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def _scheduled_today(window: OutageWindow, tz: ZoneInfo, hh: int, mm: int) -> datetime:
    """The configured HH:MM today (guild tz), where 'today' is the guild-local
    date at the moment we came back online (``window.end``)."""
    today = window.end.astimezone(tz).date()
    return datetime(today.year, today.month, today.day, hh, mm, tzinfo=tz)


def _was_missed(scheduled_local: datetime, window: OutageWindow) -> bool:
    """True when the scheduled post fell inside the outage window — i.e. it was
    already due (at or before we came back) and not before we went down."""
    scheduled_utc = scheduled_local.astimezone(timezone.utc)
    return window.start <= scheduled_utc <= window.end


def fmt_clock(dt: datetime, *, with_tz: bool) -> str:
    """Render a tz-aware datetime as ``8:10 AM`` (digest row) or
    ``8:10 AM EDT`` (window header). Uppercase AM/PM per the canonical copy."""
    hour12 = dt.hour % 12 or 12
    base = f"{hour12}:{dt.strftime('%M')} {dt.strftime('%p').upper()}"
    if with_tz and dt.tzinfo is not None:
        tz = dt.tzname()
        if tz:
            return f"{base} {tz}"
    return base


def humanize_duration(delta: timedelta) -> str:
    """``about 5h 40m`` / ``about 42m`` / ``about 3h``."""
    total_min = int(delta.total_seconds() // 60)
    h, m = divmod(total_min, 60)
    if h and m:
        return f"about {h}h {m}m"
    if h:
        return f"about {h}h"
    return f"about {m}m"


# ── Surface adapters ─────────────────────────────────────────────────────────
#
# Each adapter inspects one clock-driven surface for a single guild and returns
# the MissedItems that were due during the outage, are still catchable, and
# have not already fired. The fire closure performs the recovery post and owns
# any fire-time Premium re-check (so a guild that lapses between digest and
# click still gets the channel-side action but not a paid DM blast).


async def scan_shiny(bot, guild, cfg, window: OutageWindow) -> list[MissedItem]:
    """Daily shiny-tasks post. Catch-up window: until end of the guild day
    (which holds by construction — we scan on the same guild-day). Free tier."""
    scfg = config.get_shiny_tasks_config(guild.id)
    if not scfg.get("enabled"):
        return []
    parsed = _parse_hhmm(scfg.get("post_time") or "")
    if parsed is None:
        return []
    tz = _guild_tz(cfg)
    scheduled = _scheduled_today(window, tz, *parsed)
    if not _was_missed(scheduled, window):
        return []
    today_iso = scheduled.date().isoformat()
    if scfg.get("last_posted_date") == today_iso:
        return []  # already posted

    server_min = int(scfg.get("server_min") or 0)
    server_max = int(scfg.get("server_max") or 0)
    channel_id = int(scfg.get("channel_id") or 0)

    # Only surface a row when there is actually something to post today.
    rows = config.get_shiny_task_servers_in_range(server_min, server_max)
    from shiny_tasks import build_announcement_for_guild

    body = build_announcement_for_guild(
        server_rows=rows,
        server_min=server_min,
        server_max=server_max,
        today=scheduled.date(),
        template=scfg.get("message_template") or "",
    )
    if body is None:
        # No shinies in range today — nothing to recover. Stamp so the live
        # loop doesn't reconsider, and surface no row.
        config.mark_shiny_tasks_posted(guild.id, today_iso)
        return []

    channel = bot.get_channel(channel_id)
    destination = f"sent to #{getattr(channel, 'name', 'the shiny channel')}"

    async def _fire() -> bool:
        ch = bot.get_channel(channel_id)
        if ch is None:
            return False
        try:
            await ch.send(body)
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning("[CATCHUP] shiny post failed for guild=%s: %s", guild.id, e)
            return False
        config.mark_shiny_tasks_posted(guild.id, today_iso)
        return True

    return [
        MissedItem(
            surface="shiny_post",
            title="Shiny tasks post",
            scheduled_local=scheduled,
            destination=destination,
            fire=_fire,
        )
    ]


async def scan_survey(bot, guild, cfg, window: OutageWindow) -> list[MissedItem]:
    """Scheduled survey reminders (channel or DM). Catch-up window: until end
    of the guild day. The DM path is Premium and re-checks at fire time."""
    items: list[MissedItem] = []
    tz = _guild_tz(cfg)
    scheduled_date = window.end.astimezone(tz).date()
    for entry in config.list_scheduled_survey_reminders():
        if int(entry.get("guild_id") or 0) != guild.id:
            continue
        frequency = (entry.get("reminder_frequency") or "off").lower()
        if frequency == "off":
            continue
        parsed = _parse_hhmm(entry.get("reminder_time") or "")
        if parsed is None:
            continue
        # Weekly reminders only fire on their configured weekday; if today
        # isn't that day the reminder wasn't due, so there's nothing to catch.
        if frequency == "weekly":
            target_day = int(entry.get("reminder_day_of_week") or 1)
            if scheduled_date.weekday() != target_day:
                continue
        scheduled = _scheduled_today(window, tz, *parsed)
        if not _was_missed(scheduled, window):
            continue
        today_iso = scheduled.date().isoformat()
        if (entry.get("reminder_last_fired") or "") == today_iso:
            continue

        survey_id = entry.get("survey_id") or "default"
        use_dm = bool(entry.get("reminder_use_dm"))
        channel_id = int(entry.get("reminder_channel_id") or 0)
        if not use_dm and not channel_id:
            continue  # incomplete schedule — no destination

        survey = config.get_survey(guild.id, survey_id) or {}
        from survey import _default_reminder_body

        body = (
            survey.get("reminder_message")
            or entry.get("reminder_message")
            or _default_reminder_body(survey or {"survey_name": "Default"})
        )
        survey_name = survey.get("survey_name") or entry.get("survey_name") or "your survey"

        if use_dm:
            title = f'Survey reminder "{survey_name}": DM to your roster'
            destination = "sent individually to members"
        else:
            channel = bot.get_channel(channel_id)
            title = f'Survey reminder "{survey_name}"'
            destination = f"sent to #{getattr(channel, 'name', 'the survey channel')}"

        def _make_fire(
            body=body,
            use_dm=use_dm,
            channel_id=channel_id,
            survey_id=survey_id,
            today_iso=today_iso,
        ):
            async def _fire() -> bool:
                from survey import _send_reminder_to_channel, _send_reminder_via_dm
                import premium

                if use_dm:
                    # Premium re-check at fire time (#227): a guild that lapsed
                    # between digest and click gets no paid DM blast.
                    if not await premium.is_premium(guild.id, bot=bot):
                        logger.info(
                            "[CATCHUP] survey DM skipped for guild=%s — Premium lapsed",
                            guild.id,
                        )
                        return False
                    await _send_reminder_via_dm(bot, guild.id, body)
                else:
                    ok = await _send_reminder_to_channel(bot, guild.id, channel_id, body)
                    if not ok:
                        return False
                config.update_survey_reminder_last_fired(guild.id, survey_id, today_iso)
                return True

            return _fire

        items.append(
            MissedItem(
                surface="survey_reminder",
                title=title,
                scheduled_local=scheduled,
                destination=destination,
                fire=_make_fire(),
            )
        )
    return items


async def scan_birthday(bot, guild, cfg, window: OutageWindow) -> list[MissedItem]:
    """Daily birthday Discord announcement. Catch-up window: until end of the
    guild day. Free announcement; the per-member personal DM is Premium and
    self-gates inside dm.send_dm_to_id, mirroring the live loop."""
    bcfg = config.get_birthday_config(guild.id)
    if not bcfg.get("enabled") or not bcfg.get("reminders_enabled"):
        return []
    parsed = _parse_hhmm(bcfg.get("reminder_time") or "08:00")
    if parsed is None:
        return []
    tz = _guild_tz(cfg)
    scheduled = _scheduled_today(window, tz, *parsed)
    if not _was_missed(scheduled, window):
        return []

    channel_id = int(bcfg.get("reminder_channel_id") or 0)
    channel = bot.get_channel(channel_id)
    if channel is None:
        return []

    from train import load_birthdays

    tab_name = bcfg.get("tab_name", "Birthdays")
    members = load_birthdays(tab_name, guild.id)
    sched_date = scheduled.date()
    todays = [
        m for m in members if m.get("month") == sched_date.month and m.get("day") == sched_date.day
    ]
    if not todays:
        return []

    names = ", ".join(m.get("name", "a member") for m in todays)
    from train_cog import DEFAULT_BIRTHDAY_DM, _render_dm_body

    dm_tmpl = (bcfg.get("dm_message") or "").strip() or DEFAULT_BIRTHDAY_DM

    async def _fire() -> bool:
        import dm

        ch = bot.get_channel(channel_id)
        if ch is None:
            return False
        any_sent = False
        for m in todays:
            name = m.get("name", "a member")
            discord_id = m.get("discord_id")
            mention = f"<@{discord_id}>" if discord_id else f"**{name}**"
            try:
                await ch.send(f"🎂 Today is {mention}'s birthday!")
                any_sent = True
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning("[CATCHUP] birthday post failed for guild=%s: %s", guild.id, e)
                break
            if discord_id:
                await dm.send_dm_to_id(
                    bot, guild.id, discord_id, content=_render_dm_body(dm_tmpl, name=name)
                )
        return any_sent

    return [
        MissedItem(
            surface="birthday_announce",
            title=f"Birthday announcement: {names}",
            scheduled_local=scheduled,
            destination=f"sent to #{getattr(channel, 'name', 'the birthday channel')}",
            fire=_fire,
        )
    ]


async def scan_train_reminder(bot, guild, cfg, window: OutageWindow) -> list[MissedItem]:
    """Legacy daily 'today's train is for X' reminder (rotation-off guilds).
    Catch-up window: until end of the guild day. No persistent dedup needed —
    the window check only surfaces a reminder whose minute fell during the
    downtime, when it provably did not fire."""
    train_cfg = config.get_train_config(guild.id)
    if train_cfg.get("rotation_enabled"):
        return []  # rotation guilds use the #55 daily confirm, not this
    if not train_cfg.get("reminders_enabled", 1):
        return []
    parsed = _parse_hhmm(train_cfg.get("reminder_time") or "22:00")
    if parsed is None:
        return []
    tz = _guild_tz(cfg)
    scheduled = _scheduled_today(window, tz, *parsed)
    if not _was_missed(scheduled, window):
        return []

    from train import load_schedule

    schedule = load_schedule(guild.id)
    today_str = scheduled.date().isoformat()
    entry = schedule.get(today_str)
    if not entry:
        return []  # no conductor scheduled today
    name = entry.get("name", "Unknown")

    channel_id = int(train_cfg.get("reminder_channel_id") or 0) or int(
        getattr(cfg, "leadership_channel_id", 0) or 0
    )
    channel = bot.get_channel(channel_id)
    if channel is None:
        return []

    async def _fire() -> bool:
        import dm

        ch = bot.get_channel(channel_id)
        if ch is None:
            return False
        display = await dm.mention_or_name(bot, guild.id, name)
        msg = (
            f"🚂 **Reset! Today's train is for {display}.**\n\n"
            f"To get the ChatGPT prompt, use `/train` → 📋 Schedule overview → 📋 Generate Prompt."
        )
        try:
            await ch.send(msg)
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning("[CATCHUP] train reminder failed for guild=%s: %s", guild.id, e)
            return False
        # 💎 Premium: also DM the assignee (self-gates inside dm.send_dm).
        from train_cog import DEFAULT_TRAIN_DM, _render_dm_body

        train_dm_tmpl = (train_cfg.get("dm_message") or "").strip() or DEFAULT_TRAIN_DM
        await dm.send_dm(bot, guild.id, name, content=_render_dm_body(train_dm_tmpl, name=name))
        return True

    return [
        MissedItem(
            surface="train_reminder",
            title=f"Train reminder: {name}",
            scheduled_local=scheduled,
            destination=f"sent to #{getattr(channel, 'name', 'the reminder channel')}",
            fire=_fire,
        )
    ]


async def scan_storm_signup(bot, guild, cfg, window: OutageWindow) -> list[MissedItem]:
    """Weekly storm sign-up auto-post (DS + CS). Catch-up window: up to the
    event date minus one day. Premium, re-checked at fire time. Posting is
    idempotent (``post_registration(force=False)`` returns ``already_posted``
    if a post for the event already exists)."""
    import datetime as _dt

    from storm_date_helpers import next_event_date

    items: list[MissedItem] = []
    tz = _guild_tz(cfg)
    _EVENT_LABEL = {"DS": "Desert Storm", "CS": "Canyon Storm"}
    for row in config.get_scheduled_storm_rows():
        if int(row.get("guild_id") or 0) != guild.id:
            continue
        event_type = (row.get("event_type") or "").upper()
        if event_type not in _EVENT_LABEL:
            continue
        parsed = _parse_hhmm(row.get("signup_time") or "")
        if parsed is None:
            continue
        poll_dow = int(row.get("poll_day_of_week", -1))
        scheduled = _scheduled_today(window, tz, *parsed)
        if scheduled.weekday() != poll_dow:
            continue  # sign-up only auto-posts on its configured weekday
        if not _was_missed(scheduled, window):
            continue

        structured = config.get_structured_storm_config(guild.id, event_type)
        if not structured.get("structured_flow_enabled"):
            continue

        event_date_iso = next_event_date(guild.id, event_type, today=scheduled.date())
        try:
            event_date = _dt.date.fromisoformat(event_date_iso)
        except (TypeError, ValueError):
            continue
        # Window closes the day before the event — a sign-up poll is useless
        # once we're inside that final day.
        if scheduled.date() > event_date - _dt.timedelta(days=1):
            continue

        signup_channel = bot.get_channel(int(structured.get("signup_channel_id") or 0))

        def _make_fire(event_type=event_type, event_date_iso=event_date_iso, structured=structured):
            async def _fire() -> bool:
                import premium
                from storm_signup_post import post_registration

                if not await premium.is_premium(guild.id, bot=bot):
                    logger.info(
                        "[CATCHUP] storm sign-up skipped for guild=%s — Premium lapsed",
                        guild.id,
                    )
                    return False
                result = await post_registration(
                    bot, guild, event_type, event_date_iso, structured=structured
                )
                return result.get("status") in ("ok", "already_posted")

            return _fire

        items.append(
            MissedItem(
                surface="storm_signup",
                title=f"{_EVENT_LABEL[event_type]} sign-up poll",
                scheduled_local=scheduled,
                destination=f"sent to #{getattr(signup_channel, 'name', 'the sign-up channel')}",
                fire=_make_fire(),
            )
        )
    return items


async def scan_event_draft(bot, guild, cfg, window: OutageWindow) -> list[MissedItem]:
    """Daily event editor draft (the `EventEditorView` the scheduler posts so
    leadership can approve → announce). Catch-up window: up to the event start
    minus 30 minutes, so leadership still has time to act before the event.

    The announcement is not a separately scheduled post — it's approval-driven
    downstream of this draft — so re-posting the draft restores the whole flow
    (#227). Reuses ``scheduler.iter_guild_event_drafts`` (the same computation
    the live loop uses) and ``scheduler.post_editor`` as the fire path."""
    import datetime as _dt

    import scheduler

    tz = _guild_tz(cfg)
    today = window.end.astimezone(tz).date()
    items: list[MissedItem] = []
    for d in scheduler.iter_guild_event_drafts(cfg, today):
        draft_dt = d["draft_dt"]
        if not _was_missed(draft_dt, window):
            continue  # draft post time wasn't inside the outage (future/past day)
        # The draft is only worth re-posting while leadership can still act on
        # it before the event starts. event_list is sorted; [0] is earliest.
        event_start = d["event_list"][0]["dt"]
        if window.end > event_start - _dt.timedelta(minutes=30):
            continue

        channel = bot.get_channel(
            int(d["draft_channel_id"] or getattr(cfg, "leadership_channel_id", 0) or 0)
        )
        names = ", ".join(e["name"] for e in d["event_list"])

        def _make_fire(d=d):
            async def _fire() -> bool:
                try:
                    await scheduler.post_editor(
                        bot,
                        d["event_list"],
                        d["event_key"],
                        d["event_date"],
                        cfg,
                        draft_channel_id=d["draft_channel_id"],
                        announcement_channel_id=d["announcement_channel_id"],
                        five_min_warning=d["five_min_warning"],
                    )
                except Exception as e:
                    logger.warning("[CATCHUP] event draft failed for guild=%s: %s", guild.id, e)
                    return False
                return True

            return _fire

        items.append(
            MissedItem(
                surface="event_draft",
                title=f"Event draft: {names}",
                scheduled_local=draft_dt.astimezone(tz),
                destination=f"sent to #{getattr(channel, 'name', 'the draft channel')}",
                fire=_make_fire(),
            )
        )
    return items


# Registry of surface adapters.
SURFACE_ADAPTERS: tuple[Callable[..., Awaitable[list[MissedItem]]], ...] = (
    scan_event_draft,
    scan_shiny,
    scan_survey,
    scan_birthday,
    scan_train_reminder,
    scan_storm_signup,
)


# ── Orchestrator ─────────────────────────────────────────────────────────────


async def _scan_guild(bot, guild, cfg, window: OutageWindow) -> list[MissedItem]:
    items: list[MissedItem] = []
    for adapter in SURFACE_ADAPTERS:
        try:
            items.extend(await adapter(bot, guild, cfg, window))
        except Exception as e:  # one bad surface must not sink the digest
            logger.exception(
                "[CATCHUP] adapter %s failed for guild=%s: %s",
                getattr(adapter, "__name__", adapter),
                guild.id,
                e,
            )
    # Stable, friendly ordering: earliest-scheduled first.
    items.sort(key=lambda it: it.scheduled_local)
    return items


async def run_catchup_scan(
    bot,
    snapshot: dict[str, Optional[datetime]],
    snapshot_time: datetime,
) -> None:
    """Entry point called from ``on_ready`` after the settle delay. Detects the
    outage window from ``snapshot`` (captured before the loops restarted) and,
    for each affected guild, posts a single recovery digest."""
    window = detect_outage_window(snapshot, snapshot_time)
    if window is None:
        return

    logger.info(
        "[CATCHUP] Outage detected: %s to %s (%s)",
        window.start.isoformat(),
        window.end.isoformat(),
        humanize_duration(window.duration),
    )

    for guild in list(bot.guilds):
        try:
            cfg = config.get_config(guild.id)
            if not cfg or not cfg.setup_complete:
                continue
            items = await _scan_guild(bot, guild, cfg, window)
            if not items:
                continue
            await _post_digest(bot, guild, cfg, window, items)
        except Exception as e:
            logger.exception("[CATCHUP] scan failed for guild=%s: %s", guild.id, e)


async def _post_digest(bot, guild, cfg, window: OutageWindow, items: list[MissedItem]) -> None:
    channel = bot.get_channel(getattr(cfg, "leadership_channel_id", 0) or 0)
    if channel is None:
        logger.info(
            "[CATCHUP] guild=%s has %d missed item(s) but no resolvable "
            "leadership channel — skipping digest.",
            guild.id,
            len(items),
        )
        return
    view = OutageCatchupView(items)
    content = render_digest(window, _guild_tz(cfg), items)
    try:
        view.message = await channel.send(content, view=view)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("[CATCHUP] could not post digest for guild=%s: %s", guild.id, e)


# ── Digest copy + view ───────────────────────────────────────────────────────


def render_digest(window: OutageWindow, tz: ZoneInfo, items: list[MissedItem]) -> str:
    """Build the digest message body. Copy is canonical (see #227) — friendly
    apology baked in, plain commas/colons, window in the guild's timezone."""
    start_local = window.start.astimezone(tz)
    end_local = window.end.astimezone(tz)
    header = (
        f"📋 It looks like we were offline from roughly {fmt_clock(start_local, with_tz=False)} "
        f"to {fmt_clock(end_local, with_tz=True)} ({humanize_duration(window.duration)}), "
        f"sorry about that! We are caught back up but we missed a few things while offline."
    )
    rows = ["", "Items that are still relevant if you want to send them today:"]
    for it in items:
        rows.append(
            f"  ☐ {it.title} (scheduled for {fmt_clock(it.scheduled_local, with_tz=False)}, "
            f"{it.destination})"
        )
    return "\n".join([header, *rows])


class OutageCatchupView(discord.ui.View):
    """The digest's interactive controls: a multi-select row picker plus the
    three action buttons. Times out per the auto-post pattern — buttons strip
    and a hint to re-run is appended (``wizard_registry.expire_view_message``).
    """

    def __init__(self, items: list[MissedItem], *, timeout: float = 60 * 60 * 6):
        super().__init__(timeout=timeout)
        self.items = items
        self.message = None
        self._selected: set[int] = set()
        self._done = False

        options = [
            discord.SelectOption(
                label=_trim(it.title, 100),
                value=str(idx),
                description=_trim(
                    f"scheduled for {fmt_clock(it.scheduled_local, with_tz=False)}", 100
                ),
            )
            for idx, it in enumerate(items)
        ]
        select = discord.ui.Select(
            placeholder="Select which to send",
            min_values=0,
            max_values=len(options),
            options=options,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        self._selected = {int(v) for v in interaction.data.get("values", [])}
        await interaction.response.defer()

    async def _fire_and_report(self, interaction: discord.Interaction, indices: list[int]):
        if self._done:
            await interaction.response.send_message(
                "This digest was already actioned.", ephemeral=True
            )
            return
        self._done = True
        await interaction.response.defer()
        sent, failed = 0, 0
        for idx in indices:
            try:
                ok = await self.items[idx].fire()
            except Exception as e:
                logger.exception("[CATCHUP] fire failed for item %s: %s", idx, e)
                ok = False
            sent += 1 if ok else 0
            failed += 0 if ok else 1
        summary = f"✅ Sent {sent} item(s)."
        if failed:
            summary += f" {failed} could not be sent (check the bot's channel permissions)."
        for child in self.children:
            child.disabled = True
        self.stop()
        try:
            base = getattr(self.message, "content", "") or ""
            await self.message.edit(content=f"{base}\n\n{summary}", view=self)
        except Exception:
            pass

    @discord.ui.button(label="Send selected messages", style=discord.ButtonStyle.primary)
    async def send_selected(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._selected:
            await interaction.response.send_message(
                "Pick at least one item from the menu first, or use **Send all now**.",
                ephemeral=True,
            )
            return
        await self._fire_and_report(interaction, sorted(self._selected))

    @discord.ui.button(label="Send all now", style=discord.ButtonStyle.success)
    async def send_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._fire_and_report(interaction, list(range(len(self.items))))

    @discord.ui.button(label="Dismiss without sending", style=discord.ButtonStyle.secondary)
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._done = True
        for child in self.children:
            child.disabled = True
        self.stop()
        await interaction.response.defer()
        try:
            base = getattr(self.message, "content", "") or ""
            await self.message.edit(
                content=f"{base}\n\n*Dismissed. You can still run each command by hand.*",
                view=self,
            )
        except Exception:
            pass

    async def on_timeout(self):
        from wizard_registry import expire_view_message

        await expire_view_message(
            self.message,
            command_hint="each item's own command",
        )


def _trim(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"

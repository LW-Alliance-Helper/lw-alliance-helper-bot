"""
Auto-scheduler for the storm sign-up post (#131).

Every minute, checks each guild with structured_flow_enabled=1 AND a
configured (event_day_of_week, signup_lead_days, signup_time) schedule.
When today's date + signup_lead_days lands on the next event day AND
the current minute matches signup_time (in the alliance's local
timezone), it fires the sign-up post via the shared
`storm_signup_post.post_registration` helper.

Idempotence is preserved by `storm_registration_posts` — the post
helper itself short-circuits if a registration already exists for
the target event date.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from typing import Optional

import discord
from discord.ext import tasks

logger = logging.getLogger(__name__)


_VALID_EVENT_TYPES = ("DS", "CS")


def _guild_today_and_now(tz_name: str | None) -> tuple[_dt.date, _dt.time]:
    """Return today's date and the current time-of-day in the alliance's
    configured timezone. Falls back to UTC when the tz name is empty
    or invalid — same convention as `_today_in_guild_tz` in
    storm_signup_post."""
    from zoneinfo import ZoneInfo
    try:
        tz = ZoneInfo(tz_name) if tz_name else _dt.timezone.utc
    except Exception:
        tz = _dt.timezone.utc
    now_local = _dt.datetime.now(tz)
    return now_local.date(), now_local.time().replace(microsecond=0)


def _next_event_date(today: _dt.date, event_dow: int) -> _dt.date:
    """Date of the next occurrence of `event_dow` (0=Monday..6=Sunday)
    on or after `today`."""
    days_ahead = (event_dow - today.weekday()) % 7
    return today + _dt.timedelta(days=days_ahead)


def _parse_hhmm(value: str) -> Optional[_dt.time]:
    """'HH:MM' → time, or None on garbage."""
    if not value:
        return None
    parts = str(value).split(":", 1)
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    return _dt.time(hour, minute)


def _should_fire_now(
    *,
    today: _dt.date,
    now: _dt.time,
    event_dow: int,
    lead_days: int,
    signup_time: _dt.time,
) -> tuple[bool, Optional[_dt.date]]:
    """Decide whether the scheduler should fire for this guild right
    now. Returns `(should_fire, event_date)`. `event_date` is the
    upcoming event the post would be for — populated whether we fire
    or not so the caller can log.

    Fires when:
      * today + lead_days == event_date
      * hour:minute of `now` matches `signup_time`

    The minute match is exact — the loop runs once per minute, so we
    rely on the loop cadence rather than a wider window (which would
    risk double-fires).
    """
    if event_dow < 0 or event_dow > 6:
        return False, None
    if lead_days < 0:
        return False, None

    # Pick the upcoming event so leadership's choice of "post 5 days
    # ahead of Sunday" works whether today is Tuesday-of-this-week or
    # Tuesday-of-next-week.
    target_event = _next_event_date(today, event_dow)
    # If lead_days is large enough that today+lead_days > target event
    # (i.e. we've already passed the fire window for this week's event),
    # bump to the NEXT week's event.
    while target_event - today < _dt.timedelta(days=lead_days):
        target_event = target_event + _dt.timedelta(days=7)

    fire_date = target_event - _dt.timedelta(days=lead_days)
    if fire_date != today:
        return False, target_event
    if now.hour != signup_time.hour or now.minute != signup_time.minute:
        return False, target_event
    return True, target_event


def _scheduled_storm_rows() -> list[dict]:
    """Read every (guild, event_type) row with structured flow enabled
    AND a configured schedule. Returns a list of dicts ready for the
    scheduler to iterate."""
    from config import _get_conn  # internal but stable
    rows = []
    with _get_conn() as conn:
        for row in conn.execute(
            "SELECT guild_id, event_type, event_day_of_week, "
            "       signup_lead_days, signup_time "
            "FROM guild_storm_config "
            "WHERE structured_flow_enabled = 1 "
            "  AND event_day_of_week >= 0 "
            "  AND signup_time != ''",
        ).fetchall():
            rows.append(dict(row))
    return rows


async def _run_one_tick(bot: discord.Client) -> int:
    """Single pass of the scheduler. Returns count of registration posts
    actually fired this tick. Exposed for tests."""
    from config import get_config, get_structured_storm_config
    from storm_signup_post import post_registration

    fired = 0
    rows = _scheduled_storm_rows()
    for row in rows:
        guild_id  = int(row["guild_id"])
        event_type = row["event_type"]
        if event_type not in _VALID_EVENT_TYPES:
            continue

        # Compute today + now in the guild's local tz.
        cfg = get_config(guild_id)
        tz_name = (cfg.timezone if cfg and cfg.timezone else "") or ""
        today, now = _guild_today_and_now(tz_name)

        signup_time = _parse_hhmm(row.get("signup_time") or "")
        if signup_time is None:
            continue

        should, event_date = _should_fire_now(
            today=today, now=now,
            event_dow=int(row["event_day_of_week"]),
            lead_days=int(row.get("signup_lead_days") or 0),
            signup_time=signup_time,
        )
        if not should or event_date is None:
            continue

        # Resolve the guild from the bot cache. Bot must be ready and
        # actually in the guild; skip silently otherwise (the next tick
        # will retry).
        guild = bot.get_guild(guild_id)
        if guild is None:
            continue

        structured = get_structured_storm_config(guild_id, event_type)
        # Premium opt-in might have been disabled between setup and
        # firing; skip rather than post for a downgraded guild.
        if not structured.get("structured_flow_enabled"):
            continue

        result = await post_registration(
            bot, guild, event_type, event_date.isoformat(),
            structured=structured,
        )
        status = result.get("status")
        if status == "ok":
            fired += 1
            logger.info(
                "[STORM SCHEDULER] auto-posted sign-up for guild=%s event=%s/%s "
                "channel=%s message=%s",
                guild_id, event_type, event_date.isoformat(),
                result.get("channel_id"), result.get("message_id"),
            )
        elif status == "already_posted":
            # Idempotence: another tick (or a manual /storm_post_signup
            # earlier in the same minute) already posted. Quiet.
            pass
        else:
            logger.warning(
                "[STORM SCHEDULER] auto-post for guild=%s event=%s/%s returned %s "
                "(details=%s)",
                guild_id, event_type, event_date.isoformat(), status, result,
            )
    return fired


@tasks.loop(minutes=1)
async def storm_signup_loop_task():
    """Discord.py task wrapper. Each minute, walks every scheduled
    guild and fires posts that match the current local time."""
    bot = storm_signup_loop_task.bot_ref  # type: ignore[attr-defined]
    if bot is None or bot.is_closed():
        return
    try:
        await _run_one_tick(bot)
    except Exception as e:
        logger.exception("[STORM SCHEDULER] tick crashed: %s", e)


@storm_signup_loop_task.before_loop
async def _wait_for_ready():
    bot = storm_signup_loop_task.bot_ref  # type: ignore[attr-defined]
    if bot is not None:
        await bot.wait_until_ready()


def start_storm_signup_scheduler(bot: discord.Client) -> None:
    """Wire the loop's bot reference and start it. Called once from
    `bot.py` `on_ready`. Safe to re-call — the loop dedupes its own
    start."""
    storm_signup_loop_task.bot_ref = bot  # type: ignore[attr-defined]
    if not storm_signup_loop_task.is_running():
        storm_signup_loop_task.start()

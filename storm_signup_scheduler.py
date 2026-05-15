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
    """Parse a stored signup_time string into a `datetime.time`.

    Delegates to the canonical `config.parse_storm_signup_time` so the
    wizard's permissive parser and the scheduler agree on what counts
    as a valid time. Returns `None` for empty / garbage.
    """
    from config import parse_storm_signup_time
    normalised = parse_storm_signup_time(value)
    if normalised is None:
        return None
    h, _, m = normalised.partition(":")
    return _dt.time(int(h), int(m))


# Track skipped-this-week warnings per (guild, event_type, target_event)
# so the minute-loop doesn't spam the log every tick for the same skip.
# Cleared naturally — entries are tagged with the target_event date, so
# next week's event gets its own slot.
_skip_warned: set[tuple[int, str, str]] = set()


def _should_fire_now(
    *,
    today: _dt.date,
    now: _dt.time,
    event_dow: int,
    lead_days: int,
    signup_time: _dt.time,
) -> tuple[bool, Optional[_dt.date], bool]:
    """Decide whether the scheduler should fire for this guild right
    now. Returns `(should_fire, event_date, week_skipped)`.

    `event_date` is the upcoming event the post would be for — populated
    whether we fire or not so the caller can log.
    `week_skipped` is True iff lead_days is large enough that this
    week's event already passed its fire window, and the bot is now
    targeting next week's event. Lets the caller surface a one-shot
    warning so leadership knows they missed an auto-fire.

    Fires when:
      * today + lead_days == event_date
      * hour:minute of `now` matches `signup_time`

    The minute match is exact — the loop runs once per minute, so we
    rely on the loop cadence rather than a wider window (which would
    risk double-fires).
    """
    if event_dow < 0 or event_dow > 6:
        return False, None, False
    if lead_days < 0:
        return False, None, False

    # Pick the upcoming event so leadership's choice of "post 5 days
    # ahead of Sunday" works whether today is Tuesday-of-this-week or
    # Tuesday-of-next-week.
    target_event = _next_event_date(today, event_dow)
    week_skipped = False
    # If lead_days is large enough that today+lead_days > target event
    # (i.e. we've already passed the fire window for this week's event),
    # bump to the NEXT week's event.
    while target_event - today < _dt.timedelta(days=lead_days):
        target_event = target_event + _dt.timedelta(days=7)
        week_skipped = True

    fire_date = target_event - _dt.timedelta(days=lead_days)
    if fire_date != today:
        return False, target_event, week_skipped
    if now.hour != signup_time.hour or now.minute != signup_time.minute:
        return False, target_event, week_skipped
    return True, target_event, week_skipped


def _scheduled_storm_rows() -> list[dict]:
    """Internal alias kept for test patching — delegates to
    `config.get_scheduled_storm_rows()` so the SQL lives next to the
    schema, not next to the consumer."""
    from config import get_scheduled_storm_rows
    return get_scheduled_storm_rows()


async def _run_one_tick(bot: discord.Client) -> int:
    """Single pass of the scheduler. Returns count of registration posts
    actually fired this tick. Exposed for tests."""
    import premium
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

        should, event_date, week_skipped = _should_fire_now(
            today=today, now=now,
            event_dow=int(row["event_day_of_week"]),
            lead_days=int(row.get("signup_lead_days") or 0),
            signup_time=signup_time,
        )
        if event_date is not None and week_skipped:
            # The configured lead is longer than days-remaining to this
            # week's event — the fire window already passed. Warn once
            # per (guild, event_type, skipped_event_date) so leadership
            # has a signal in logs that an auto-post didn't go out.
            # The skipped event is today + (event_date - today) - 7 days,
            # i.e. the THIS-week occurrence we just bypassed.
            skipped_event = event_date - _dt.timedelta(days=7)
            skip_key = (guild_id, event_type, skipped_event.isoformat())
            if skip_key not in _skip_warned:
                _skip_warned.add(skip_key)
                logger.warning(
                    "[STORM SCHEDULER] guild=%s event=%s/%s — auto-post "
                    "window for this week's event already passed "
                    "(lead=%s days, days-remaining=%s); next auto-post "
                    "targets %s. Run the parent group's post_signup "
                    "subcommand manually if you need to post for the "
                    "skipped event.",
                    guild_id, event_type, skipped_event.isoformat(),
                    row.get("signup_lead_days"),
                    (skipped_event - today).days,
                    event_date.isoformat(),
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
        # Defense-in-depth premium re-check at fire time. A guild whose
        # subscription lapsed (or whose Premium assignment was moved to
        # a different guild) between setup and fire still has
        # `structured_flow_enabled=1` on disk; without this check the
        # scheduler would keep auto-posting for downgraded guilds.
        if not await premium.is_premium(guild_id, bot=bot):
            logger.info(
                "[STORM SCHEDULER] guild=%s event=%s/%s — skipping auto-post "
                "because guild is no longer Premium (structured_flow_enabled "
                "still on disk).",
                guild_id, event_type, event_date.isoformat(),
            )
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
            # Idempotence: another tick (or a manual post_signup earlier
            # in the same minute) already posted. Quiet.
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

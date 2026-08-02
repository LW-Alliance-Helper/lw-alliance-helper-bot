"""
Auto-scheduler for the storm sign-up post (#131 + Rule H / #164).

Every minute, checks each guild with structured_flow_enabled=1 AND a
configured (poll_day_of_week, signup_time) schedule. When today's
weekday in the alliance's tz matches `poll_day_of_week` AND the
current time is at or past `signup_time` (#365 — not an exact minute
match, so a tick that lands late doesn't silently skip the whole
week), it fires the sign-up post via the shared
`storm_signup_post.post_registration` helper.

Event day is game-defined (DS = Friday, CS = Thursday); the event
date in the post comes from `storm_date_helpers.next_event_date`.

Idempotence is preserved by `storm_registration_posts` — the post
helper itself short-circuits if a registration already exists for
the target event date. A per-(guild, event_type) retry cooldown
(`_RETRY_COOLDOWN`) bounds how often a guild that keeps failing (bad
channel, missing permission) gets retried, now that a match isn't a
one-shot-per-minute event.
"""

from __future__ import annotations

import datetime as _dt
import logging
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


def _should_fire_now(
    *,
    today: _dt.date,
    now: _dt.time,
    poll_dow: int,
    signup_time: _dt.time,
) -> bool:
    """Decide whether the scheduler should fire for this guild right
    now.

    Fires when today's weekday matches `poll_dow` AND `now` is at or past
    `signup_time` — not an exact minute match (#365). A shared per-minute
    tick that runs long (many guilds, a slow Sheets call earlier in the
    loop) can land on a guild's row a minute or more late; an exact match
    would then silently skip that guild's post for the entire week with no
    retry. `post_registration`'s own idempotent short-circuit (a
    `storm_registration_posts` row already exists for the event date)
    prevents a late tick from double-posting — the retry-cooldown in
    `_run_one_tick` bounds how often a *failing* guild gets retried. This
    mirrors the fix already shipped for the Shiny Tasks loop after the
    exact-match version silently missed three consecutive nights in
    production (see bot.py's `shiny_tasks_post_task`).
    """
    if poll_dow < 0 or poll_dow > 6:
        return False
    if today.weekday() != poll_dow:
        return False
    return now >= signup_time


# Guards the at-or-past retry (#365) from re-attempting a guild+event that
# keeps failing (bad channel, missing permission, transient Sheets/Discord
# error) on every single tick for the rest of the day. Keyed by
# (guild_id, event_type) since a guild can have both DS and CS scheduled.
# Not persisted; in-memory is enough since the only cost of losing it on
# restart is one extra attempt. Mirrors bot.py's `_shiny_last_attempt`.
_RETRY_COOLDOWN = _dt.timedelta(minutes=5)
_last_attempt: dict[tuple[int, str], _dt.datetime] = {}


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
    from storm_date_helpers import next_event_date
    from storm_signup_post import post_registration

    fired = 0
    rows = _scheduled_storm_rows()
    for row in rows:
        guild_id = int(row["guild_id"])
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

        if not _should_fire_now(
            today=today,
            now=now,
            poll_dow=int(row["poll_day_of_week"]),
            signup_time=signup_time,
        ):
            continue

        # Throttle retries for a guild+event that's overdue but keeps
        # failing — without this, at-or-past matching would retry every
        # single tick for the rest of the day instead of once every few
        # minutes (#365).
        cooldown_key = (guild_id, event_type)
        last_attempt = _last_attempt.get(cooldown_key)
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        if last_attempt is not None and (now_utc - last_attempt) < _RETRY_COOLDOWN:
            continue
        _last_attempt[cooldown_key] = now_utc

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
                "[STORM SCHEDULER] guild=%s event=%s — skipping auto-post "
                "because guild is no longer Premium (structured_flow_enabled "
                "still on disk).",
                guild_id,
                event_type,
            )
            continue

        # The post's event_date is the next occurrence of the
        # game-defined event day (DS=Friday, CS=Thursday).
        event_date = next_event_date(guild_id, event_type, today=today)
        result = await post_registration(
            bot,
            guild,
            event_type,
            event_date,
            structured=structured,
        )
        status = result.get("status")
        if status == "ok":
            fired += 1
            logger.info(
                "[STORM SCHEDULER] auto-posted sign-up for guild=%s event=%s/%s "
                "channel=%s message=%s",
                guild_id,
                event_type,
                event_date,
                result.get("channel_id"),
                result.get("message_id"),
            )
        elif status == "already_posted":
            # Idempotence: another tick (or a manual post_signup earlier
            # in the same minute) already posted. Quiet.
            pass
        else:
            logger.warning(
                "[STORM SCHEDULER] auto-post for guild=%s event=%s/%s returned %s (details=%s)",
                guild_id,
                event_type,
                event_date,
                status,
                result,
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
        return
    # Clean tick — stamp liveness for the outage catch-up scan (#227).
    from config import stamp_loop_heartbeat

    stamp_loop_heartbeat("storm_signup")


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

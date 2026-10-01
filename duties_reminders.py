"""Leadership Duties (#687): sending reminders.

`run_reminder_tick` is one pass of the per-minute loop in `duties_cog`;
`deliver` sends one firing and is shared with the outage catch-up digest.

**Order of a tick, and why.** Reminders are evaluated against each server's
own clock (its timezone from `/setup`, the same clock storm sign-ups use).
Premium is checked only for servers with something due, so the loop doesn't
look up every server every minute. A due reminder is then **claimed** with
`duties_db.mark_reminder_fired`, a conditional write, before anything is
sent: two overlapping ticks can't both send it, and a send that fails
partway doesn't repeat every minute for the rest of the day. The claim is a
write inside a loop tick, so it goes through `asyncio.to_thread`.

**Who hears it** is read from the duty at fire time (`duties.plan_delivery`),
so a reminder follows the duty when it changes hands. A DM that can't be
delivered falls back to the leadership channel, naming who it was for.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord

import config
import config_health
import duties as d
import duties_copy as c
import duties_db
import duties_health
import duties_render as r
from time_helpers import ET

logger = logging.getLogger(__name__)

PREMIUM_FEATURE = d.PREMIUM_FEATURE

#: The heartbeat the loop stamps. Mirrored in `outage_catchup.HEARTBEAT_LOOPS`.
HEARTBEAT = "duty_reminder"


def guild_tz(cfg) -> ZoneInfo:
    try:
        return ZoneInfo((getattr(cfg, "timezone", "") or "") or "America/New_York")
    except (ZoneInfoNotFoundError, ValueError):
        return ET


def _display_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member is not None else r.mention(user_id)


def _leadership_role(guild: discord.Guild, cfg) -> discord.Role | None:
    from duties_hub import leadership_role

    return leadership_role(guild, cfg)


async def deliver(bot, guild: discord.Guild, cfg, duty: d.Duty, reminder: d.DutyReminder) -> bool:
    """Send one firing of `reminder`. Returns True if it reached anyone."""
    plan = d.plan_delivery(
        duty, reminder, leadership_channel_id=(cfg.leadership_channel_id or 0) if cfg else 0
    )
    reached = False
    failed: list[int] = []

    if plan.dm_user_ids:
        # A DM names people the way a DM can show them: by name. A mention
        # only renders for someone whose client has the member loaded.
        dm_text = r.reminder_text(
            duty,
            reminder,
            lambda uid: _display_name(guild, uid),
            server=guild.name,
            dm=True,
        )
        for uid in plan.dm_user_ids:
            member = guild.get_member(uid)
            if member is None:
                failed.append(uid)
                continue
            try:
                await member.send(dm_text)
                reached = True
            except discord.HTTPException:
                failed.append(uid)

    if plan.channel_id:
        subject = duties_health.reminder_channel_subject(plan.channel_id)
        channel = config_health.resolve_configured_channel(bot, guild.id, subject, plan.channel_id)
        if channel is not None:
            text = r.reminder_text(duty, reminder, r.mention, server=guild.name, dm=False)
            pings = [r.mention(uid) for uid in plan.ping_user_ids]
            role = _leadership_role(guild, cfg) if plan.ping_leadership else None
            if role is not None:
                pings.append(role.mention)
            content = f"{' '.join(pings)}\n{text}" if pings else text
            # Only the people the reminder asked to ping are pinged. A
            # `{primary}` in the text is a mention too, and must not ping
            # when the officer turned pinging off.
            allowed = discord.AllowedMentions(
                everyone=False,
                users=[discord.Object(id=uid) for uid in plan.ping_user_ids],
                roles=[role] if role is not None else False,
            )
            try:
                await channel.send(content[:2000], allowed_mentions=allowed)
                reached = True
            except discord.Forbidden:
                config_health.record(
                    guild.id,
                    subject,
                    config_health.CHANNEL_NO_SEND,
                    "",
                    discriminator=str(plan.channel_id),
                )
            except discord.HTTPException as e:
                logger.warning("[DUTIES] guild=%s reminder post failed: %s", guild.id, e)

    if failed:
        reached |= await _fallback(bot, guild, cfg, duty, reminder, failed)
    return reached


async def _fallback(bot, guild, cfg, duty, reminder, failed: list[int]) -> bool:
    """Post the DMs that didn't land in the leadership channel, naming (and
    pinging) who they were for, so the reminder isn't lost to someone's
    privacy settings."""
    channel_id = (cfg.leadership_channel_id or 0) if cfg else 0
    channel = bot.get_channel(channel_id) if channel_id else None
    if channel is None:
        logger.info("[DUTIES] guild=%s DM fallback has no leadership channel", guild.id)
        return False
    text = d.render_reminder(
        reminder, duty, name_of=r.mention, anyone_in_leadership=c.ANYONE_IN_LEADERSHIP
    )
    content = r.fallback_text(duty, [r.mention(uid) for uid in failed], text)
    try:
        await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=[discord.Object(id=uid) for uid in failed]
            ),
        )
        return True
    except discord.HTTPException as e:
        logger.warning("[DUTIES] guild=%s DM fallback failed: %s", guild.id, e)
        return False


async def run_reminder_tick(bot, now: datetime | None = None) -> int:
    """One pass. Returns how many reminders were sent."""
    import premium

    now = now or datetime.now(timezone.utc)
    by_guild: dict[int, list[d.DutyReminder]] = {}
    for reminder in duties_db.enabled_reminders():
        by_guild.setdefault(reminder.guild_id, []).append(reminder)

    sent = 0
    for guild_id, reminders in by_guild.items():
        try:
            guild = bot.get_guild(guild_id)
            cfg = config.get_config(guild_id)
            if guild is None or cfg is None:
                continue
            now_local = now.astimezone(guild_tz(cfg))
            duties_by_id = {x.id: x for x in duties_db.list_duties(guild_id)}
            due = []
            for reminder in reminders:
                duty = duties_by_id.get(reminder.duty_id)
                occurrence = d.should_fire(duty, reminder, now_local) if duty else None
                if occurrence is not None:
                    due.append((duty, reminder, occurrence))
            if not due:
                continue
            if not await premium.feature_gate(PREMIUM_FEATURE, guild_id, bot=bot):
                # Without Premium the occurrence is used up, not held back:
                # resubscribing mid-day then waits for each reminder's next
                # time rather than sending everything it missed at once.
                for _duty, reminder, occurrence in due:
                    await asyncio.to_thread(
                        duties_db.mark_reminder_fired, guild_id, reminder.id, occurrence
                    )
                continue
            for duty, reminder, occurrence in due:
                claimed = await asyncio.to_thread(
                    duties_db.mark_reminder_fired, guild_id, reminder.id, occurrence
                )
                if not claimed:
                    continue
                await deliver(bot, guild, cfg, duty, reminder)
                sent += 1
        except Exception as e:  # noqa: BLE001 - one server must not sink the tick
            logger.exception("[DUTIES] reminder tick failed for guild %s: %s", guild_id, e)
            try:
                import sentry_sdk

                sentry_sdk.capture_exception(e)
            except Exception:  # noqa: BLE001
                pass
    return sent


async def handle_departure(bot, member: discord.Member) -> list[str]:
    """Someone left the server. Open every position they held, post the
    notice naming them, and update every posted message. Returns the duties
    that changed."""
    import duties_posts

    names = await asyncio.to_thread(
        duties_db.vacate_holder, member.guild.id, member.id, member.display_name
    )
    if not names:
        return []
    await asyncio.to_thread(duties_health.note_departures, member.guild.id)
    await duties_posts.refresh_all(bot, member.guild.id)
    return names

"""
dm.py — Premium DM helpers shared by birthday, train, survey, and storm flows.

Two entry points:
  * `send_dm_to_id(bot, guild_id, discord_id, ...)` — caller already knows
    the Discord user ID (e.g. read from the birthday sheet).
  * `send_dm(bot, guild_id, name, ...)` — caller knows only a display name;
    looks up via the Member Roster sheet.

Both verify the guild is on Premium and silently swallow errors so
scheduled tasks don't crash on permission/closed-DM failures.

`mention_or_name(bot, guild_id, name)` returns either `<@id>` (if the
roster knows the member and the guild is premium) or the plain name —
used to swap pings into announcement templates.
"""

import asyncio

import discord

import premium
from config import lookup_discord_id_for_name


async def send_dm_to_id(
    bot,
    guild_id: int,
    discord_id: int | str,
    *,
    content: str = "",
    embed: discord.Embed = None,
) -> bool:
    """DM a Discord user by ID. Premium-gated; returns True on success."""
    if not await premium.is_premium(guild_id, bot=bot):
        return False
    try:
        did = int(discord_id)
    except (TypeError, ValueError):
        return False
    try:
        user = await bot.fetch_user(did)
    except discord.NotFound:
        return False
    except Exception as e:
        print(f"[DM] Could not fetch user {did} for guild {guild_id}: {e}")
        return False
    try:
        await user.send(content=content or None, embed=embed)
        return True
    except discord.Forbidden:
        # Closed DMs is the most common silent failure across train,
        # birthday, survey, and storm reminder loops. Logging the
        # (guild, user) pair lets leadership tell that member to
        # re-open DMs instead of guessing why reminders stopped.
        print(f"[DM] Forbidden — user {did} likely has DMs closed (guild={guild_id})")
        return False
    except Exception as e:
        print(f"[DM] Failed to send DM to {did} in guild {guild_id}: {e}")
        return False


async def send_dm(
    bot,
    guild_id: int,
    name: str,
    *,
    content: str = "",
    embed: discord.Embed = None,
) -> bool:
    """DM a member resolved through the Member Roster sheet. Premium-gated."""
    if not await premium.is_premium(guild_id, bot=bot):
        return False
    did = await asyncio.to_thread(lookup_discord_id_for_name, guild_id, name)
    if not did:
        return False
    return await send_dm_to_id(bot, guild_id, did, content=content, embed=embed)


async def mention_or_name(bot, guild_id: int, name: str) -> str:
    """
    Return `<@id>` if we can resolve `name` to a Discord ID via the roster
    (and the guild is premium), else return the original `name`.
    """
    if not await premium.is_premium(guild_id, bot=bot):
        return name
    did = await asyncio.to_thread(lookup_discord_id_for_name, guild_id, name)
    return f"<@{did}>" if did else name

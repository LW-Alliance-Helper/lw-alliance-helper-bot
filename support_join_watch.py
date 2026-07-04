"""Support-server join watch (owner/support tooling).

When someone joins the bot's *support* server, post a short notice to a
configured, hidden channel listing which other servers-with-the-bot-installed
that person is already in. A member who belongs to zero bot-installed servers
is a weak spam signal worth keeping an eye on (the someone-joined-and-spammed
case this was built for). It is only a *weak* signal — plenty of legitimate
users find the support server before installing the bot, and this can only ever
see overlap with servers the bot is also in.

The "support server" isn't hardcoded: it's simply the guild that owns the
configured watch channel. Set the channel with `/admin set_join_watch` and the
join listener fires for joins to that channel's guild. This keeps the whole
feature to a single stored value (`app_settings` key below) with no extra
env var.

This module is Discord-thin on purpose: the cross-guild lookup and message
rendering live here as plain functions so they're unit-testable; bot.py owns
the listener wiring and the two `/admin` subcommands.
"""

from __future__ import annotations

import discord

# app_settings key holding the watch channel id (as TEXT). See
# config.get_app_setting / set_app_setting.
WATCH_CHANNEL_SETTING = "support_join_watch_channel_id"

# Cap how many shared-guild names we spell out inline before summarising the
# remainder, so the notice never blows Discord's 2000-char message limit for a
# user who happens to be in dozens of bot servers.
_MAX_NAMED_GUILDS = 25


def shared_bot_guilds(
    bot: discord.Client, user_id: int, exclude_guild_id: int
) -> list[discord.Guild]:
    """Return every guild the bot is in (other than `exclude_guild_id`) that
    currently contains `user_id`, sorted by name.

    Uses the member *cache* (`guild.get_member`), which is populated for all
    guilds at startup because the bot has the privileged members intent and
    `chunk_guilds_at_startup` defaults to True. No network calls, so this is
    safe to run in a tight loop over every guild / every member.
    """
    out = [g for g in bot.guilds if g.id != exclude_guild_id and g.get_member(user_id) is not None]
    out.sort(key=lambda g: g.name.lower())
    return out


def format_join_notice(member: discord.Member, shared: list[discord.Guild]) -> str:
    """Build the join-notice message for a single member.

    `shared` is the result of `shared_bot_guilds`. Renders the requested
    "... Servers they belong to with LW Alliance Helper installed: A, B, C"
    line, or "None" when the list is empty.
    """
    who = f"{member.mention} (`{member}` · ID `{member.id}`)"
    if not shared:
        return (
            f"👤 {who} just joined the server.\n"
            f"Servers they belong to with LW Alliance Helper installed: **None**"
        )

    names = [g.name for g in shared[:_MAX_NAMED_GUILDS]]
    listed = ", ".join(names)
    if len(shared) > _MAX_NAMED_GUILDS:
        listed += f", …(+{len(shared) - _MAX_NAMED_GUILDS} more)"
    return (
        f"👤 {who} just joined the server.\n"
        f"Servers they belong to with LW Alliance Helper installed "
        f"({len(shared)}): {listed}"
    )

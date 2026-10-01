"""Leadership Duties: the messages the bot posts and keeps current.

Three posts, one of each per server, and one routine behind all of them:

- **The contact buttons** (`duties_panel`, #687): members open a private
  thread with a duty's people.
- **The full list** in the leadership channel (#706), with a **👤 My duties**
  button that shows whoever clicks it their own duties, privately.
- **The curated list** shared with members (#707): the duties leadership
  picked, and who is in charge of each, with no buttons.

Each is posted by `post`, which replaces any earlier post of the same kind
so there is only ever one, and edited in place by `refresh` whenever a duty
changes. `refresh_all` is what every change calls. A message someone deleted
is forgotten rather than reported, since deleting it was leadership's own
choice; a channel the bot can no longer reach is config rot and goes to
`config_health`.

**The My duties button is persistent and static**: one custom id, the server
read from the click, registered in `duties_cog.cog_load`, so it works after a
redeploy. It answers people with the Leadership role from `/setup` and
anyone currently assigned to a duty, and turns everyone else away: the
channel isn't the gate, since not everyone who can see it has the role.
It stays live after Premium ends, as the hub's viewing controls do (UX.md,
Principle 5): it changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import discord

import config_health
import duties as d
import duties_copy as c
import duties_db
import duties_health
import duties_render

logger = logging.getLogger(__name__)


# ── One routine for every post ───────────────────────────────────────────────


@dataclass(frozen=True)
class PostKind:
    """Where one kind of post is recorded and what it says."""

    #: `DutiesSettings` fields holding the channel and the message.
    channel_field: str
    message_field: str
    #: The `config_health` subject for its channel.
    subject: str
    #: guild id → the keyword arguments for `send` and `edit`.
    payload: Callable[[int], dict]
    #: For the log line.
    name: str


def _record(guild_id: int, kind: PostKind, problem: str, channel_id: int) -> None:
    config_health.record(guild_id, kind.subject, problem, "", discriminator=str(channel_id))


async def post(bot, guild_id: int, kind: PostKind, channel_id: int) -> discord.Message | None:
    """Post `kind` in `channel_id`, replacing any earlier post of it so there
    is only ever one. Returns the message, or None if the bot couldn't post
    (recorded against the channel)."""
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if channel is None:
        _record(guild_id, kind, config_health.CHANNEL_GONE, channel_id)
        return None
    settings = duties_db.get_settings(guild_id)
    try:
        message = await channel.send(**kind.payload(guild_id))
    except discord.Forbidden:
        _record(guild_id, kind, config_health.CHANNEL_NO_SEND, channel_id)
        return None
    except discord.HTTPException as e:
        logger.warning("[DUTIES] guild=%s could not post the %s: %s", guild_id, kind.name, e)
        return None
    config_health.clear(guild_id, kind.subject)
    old_channel = getattr(settings, kind.channel_field)
    old_id = getattr(settings, kind.message_field)
    duties_db.update_settings(
        guild_id, **{kind.channel_field: int(channel_id), kind.message_field: message.id}
    )
    if old_id and old_id != message.id:
        old = bot.get_channel(old_channel) if old_channel else None
        if old is not None:
            try:
                await old.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass  # already gone, or no longer ours to delete
    return message


async def refresh(bot, guild_id: int, kind: PostKind) -> bool:
    """Edit `kind`'s post to match the duty list. Returns True if it was
    edited. Safe to call after every change: no post, no-op."""
    settings = duties_db.get_settings(guild_id)
    channel_id = getattr(settings, kind.channel_field)
    message_id = getattr(settings, kind.message_field)
    if not message_id or not channel_id:
        return False
    channel = bot.get_channel(channel_id)
    if channel is None:
        _record(guild_id, kind, config_health.CHANNEL_GONE, channel_id)
        return False
    try:
        await channel.get_partial_message(message_id).edit(**kind.payload(guild_id))
    except discord.NotFound:
        duties_db.update_settings(guild_id, **{kind.message_field: 0})
        return False
    except discord.Forbidden:
        _record(guild_id, kind, config_health.CHANNEL_NO_VIEW, channel_id)
        return False
    except discord.HTTPException as e:
        logger.warning("[DUTIES] guild=%s could not update the %s: %s", guild_id, kind.name, e)
        return False
    config_health.clear(guild_id, kind.subject)
    return True


async def refresh_all(bot, guild_id: int) -> None:
    """Bring every posted message up to date. What every change calls."""
    import duties_panel

    for kind in (duties_panel.CONTACT, ROSTER, SHARED):
        await refresh(bot, guild_id, kind)


# ── The full list, in the leadership channel (#706) ─────────────────────────


class RosterView(discord.ui.View):
    """The My duties button under the full list. Persistent and static."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label=c.BTN_MY_DUTIES, style=discord.ButtonStyle.secondary, custom_id="duties_roster_mine"
    )
    async def mine(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_my_duties(interaction)


def _roster_payload(guild_id: int) -> dict:
    return {
        "embeds": duties_render.roster_embeds(
            duties_db.list_duties(guild_id), duties_render.mention
        ),
        "view": RosterView(),
        # People are named in the embeds, where mentions never ping.
        "allowed_mentions": discord.AllowedMentions.none(),
    }


ROSTER = PostKind(
    channel_field="roster_channel_id",
    message_field="roster_message_id",
    subject=duties_health.ROSTER_CHANNEL,
    payload=_roster_payload,
    name="duty list",
)


def can_see_own_duties(member, role, duties: list[d.Duty]) -> bool:
    """Who the My duties button answers: anyone with the Leadership role,
    or anyone assigned to a duty right now (someone who lost the role, or
    was picked with Discord's own user picker, still has duties to see)."""
    if role is not None and role in getattr(member, "roles", ()):
        return True
    return any(duty.holds(member.id) for duty in duties)


async def show_my_duties(interaction: discord.Interaction) -> None:
    import config
    from duties_hub import leadership_role

    guild = interaction.guild
    if guild is None:
        return
    duties = duties_db.list_duties(guild.id)
    role = leadership_role(guild, config.get_config(guild.id))
    if not can_see_own_duties(interaction.user, role, duties):
        await interaction.response.send_message(c.ROSTER_DENIED, ephemeral=True)
        return
    primary, backup = d.held_by(duties, interaction.user.id)
    await interaction.response.send_message(
        embed=duties_render.my_duties_embed(primary, backup), ephemeral=True
    )


# ── The curated list, shared with members (#707) ────────────────────────────


def _shared_payload(guild_id: int) -> dict:
    settings = duties_db.get_settings(guild_id)
    return {
        "embeds": duties_render.shared_embeds(
            duties_db.list_duties(guild_id),
            duties_render.mention,
            backups=settings.shared_backups,
        ),
        "allowed_mentions": discord.AllowedMentions.none(),
    }


SHARED = PostKind(
    channel_field="shared_channel_id",
    message_field="shared_message_id",
    subject=duties_health.SHARED_CHANNEL,
    payload=_shared_payload,
    name="curated list",
)


def register_persistent_items(bot) -> None:
    """Called from `duties_cog.cog_load`."""
    bot.add_view(RosterView())

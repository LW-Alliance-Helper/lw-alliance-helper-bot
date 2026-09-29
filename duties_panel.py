"""Leadership Duties (#687): the posted contact buttons and the threads they open.

The member-facing half of the feature. One public message in a channel
leadership picks carries a button per contact-enabled duty; a click opens a
private thread with only the member and that duty's holders in it (the
ticket model). The message edits itself when a duty changes hands, pauses or
resumes, so members never see a stale holder.

**Persistence.** Each button is a `DynamicItem` whose custom id carries the
duty id (`duty_contact:<id>`), the same shape as `survey.DynamicSurveyButton`,
so a click after a redeploy still resolves without re-registering a view per
message. The Close button inside a thread is one static custom id for the
same reason. Both are registered in `duties_cog.cog_load`.

**Premium is checked at click time**, never only when the message was
posted: a lapsed alliance keeps its message, and a click tells the member the
feature isn't available right now.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import discord

import config_health
import duties as d
import duties_copy as c
import duties_db
import duties_health
import duties_render

logger = logging.getLogger(__name__)

PREMIUM_FEATURE = d.PREMIUM_FEATURE

#: A second click inside this long gets the thread it already opened, not a
#: new one. Long enough to absorb a double-tap on a phone and a nervous
#: second click, short enough that someone with a genuinely new message
#: isn't turned away.
COOLDOWN_SECONDS = 60

_recent: dict[tuple[int, int, int], tuple[float, int]] = {}
_in_flight: set[tuple[int, int, int]] = set()

_MENTION_USERS = discord.AllowedMentions(users=True, roles=False, everyone=False)


# ── The posted message ───────────────────────────────────────────────────────


class DutyContactButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"duty_contact:(?P<duty_id>[0-9]{1,19})",
):
    """One duty's button. The label is the duty name, bare: every button in
    the set is the same kind of thing and differs only by which duty, so a
    glyph would repeat across all of them (DESIGN.md, Emoji rule 7)."""

    def __init__(self, duty_id: int, label: str = "​"):
        super().__init__(
            discord.ui.Button(
                label=(label or "​")[:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"duty_contact:{duty_id}",
            )
        )
        self.duty_id = duty_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(int(match["duty_id"]), label=item.label or "")

    async def callback(self, interaction: discord.Interaction):
        await open_ticket(interaction, self.duty_id)


def build_panel_view(duties: list[d.Duty]) -> discord.ui.View:
    """Persistent, `timeout=None`: this is a post, not a screen."""
    view = discord.ui.View(timeout=None)
    for duty in duties_render.contact_duties(duties):
        view.add_item(DutyContactButton(duty.id, label=duty.name))
    return view


def _panel_payload(guild_id: int) -> dict:
    duties = duties_db.list_duties(guild_id)
    return {
        "embed": duties_render.panel_embed(duties, duties_render.mention),
        "view": build_panel_view(duties),
        # Holders are named in the embed, where mentions never ping. Nothing
        # in this message should notify anyone just because a duty changed.
        "allowed_mentions": discord.AllowedMentions.none(),
    }


async def post_panel(bot, guild_id: int, channel_id: int) -> discord.Message | None:
    """Post the contact buttons in `channel_id`, replacing any earlier post
    so there is only ever one. Returns the message, or None if the bot
    couldn't post (recorded against the panel channel)."""
    channel = bot.get_channel(int(channel_id)) if channel_id else None
    if channel is None:
        config_health.record(
            guild_id,
            duties_health.PANEL_CHANNEL,
            config_health.CHANNEL_GONE,
            "",
            discriminator=str(channel_id),
        )
        return None
    settings = duties_db.get_settings(guild_id)
    try:
        message = await channel.send(**_panel_payload(guild_id))
    except discord.Forbidden:
        config_health.record(
            guild_id,
            duties_health.PANEL_CHANNEL,
            config_health.CHANNEL_NO_SEND,
            "",
            discriminator=str(channel_id),
        )
        return None
    except discord.HTTPException as e:
        logger.warning("[DUTIES] guild=%s could not post contact buttons: %s", guild_id, e)
        return None
    config_health.clear(guild_id, duties_health.PANEL_CHANNEL)
    old_channel, old_id = settings.panel_channel_id, settings.panel_message_id
    duties_db.save_settings(
        duties_db.DutiesSettings(
            guild_id=guild_id,
            panel_channel_id=int(channel_id),
            panel_message_id=message.id,
            ticket_channel_id=settings.ticket_channel_id,
        )
    )
    if old_id and old_id != message.id:
        old = bot.get_channel(old_channel) if old_channel else None
        if old is not None:
            try:
                await old.get_partial_message(old_id).delete()
            except discord.HTTPException:
                pass  # already gone, or no longer ours to delete
    return message


async def refresh_panel(bot, guild_id: int) -> bool:
    """Edit the posted message to match the duty list. Returns True if it
    was edited. Safe to call after every change: no post, no-op.

    A message someone deleted is forgotten rather than reported, since
    deleting it was leadership's own choice and the settings screen shows it
    as not posted. A channel the bot can no longer reach is config rot and
    goes to `config_health`.
    """
    settings = duties_db.get_settings(guild_id)
    if not settings.panel_message_id or not settings.panel_channel_id:
        return False
    channel = bot.get_channel(settings.panel_channel_id)
    if channel is None:
        config_health.record(
            guild_id,
            duties_health.PANEL_CHANNEL,
            config_health.CHANNEL_GONE,
            "",
            discriminator=str(settings.panel_channel_id),
        )
        return False
    try:
        await channel.get_partial_message(settings.panel_message_id).edit(
            **_panel_payload(guild_id)
        )
    except discord.NotFound:
        duties_db.save_settings(
            duties_db.DutiesSettings(
                guild_id=guild_id,
                panel_channel_id=settings.panel_channel_id,
                panel_message_id=0,
                ticket_channel_id=settings.ticket_channel_id,
            )
        )
        return False
    except discord.Forbidden:
        config_health.record(
            guild_id,
            duties_health.PANEL_CHANNEL,
            config_health.CHANNEL_NO_VIEW,
            "",
            discriminator=str(settings.panel_channel_id),
        )
        return False
    except discord.HTTPException as e:
        logger.warning("[DUTIES] guild=%s could not update contact buttons: %s", guild_id, e)
        return False
    config_health.clear(guild_id, duties_health.PANEL_CHANNEL)
    return True


def register_persistent_items(bot) -> None:
    """Called from `duties_cog.cog_load`."""
    bot.add_dynamic_items(DutyContactButton)
    bot.add_view(TicketCloseView())


# ── Opening a thread ─────────────────────────────────────────────────────────


def _leadership_role(guild):
    import config
    from duties_hub import leadership_role

    return leadership_role(guild, config.get_config(guild.id))


def ticket_channel_problem(channel, member, me) -> str | None:
    """Why a thread opened under `channel` wouldn't work for `member`, as a
    `config_health` kind, or None if it would.

    A private thread is only visible to someone who can view its parent, so
    a channel the member can't see opens a thread they can never read. That
    is checked here, at click time, rather than trusting the settings screen
    once: permissions change.
    """
    if not isinstance(channel, discord.TextChannel):
        return config_health.CHANNEL_GONE
    mine = channel.permissions_for(me)
    if not mine.view_channel:
        return config_health.CHANNEL_NO_VIEW
    if not (mine.create_private_threads and mine.send_messages_in_threads):
        return config_health.NO_THREADS
    if not channel.permissions_for(member).view_channel:
        return config_health.MEMBERS_CANT_VIEW
    return None


async def _reply(interaction: discord.Interaction, text: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


async def open_ticket(interaction: discord.Interaction, duty_id: int) -> None:
    """A member clicked a duty's button."""
    import premium

    guild = interaction.guild
    if guild is None:
        return
    # First, before anything that can wait on the network: the Premium
    # lookup can miss its cache, and Discord allows three seconds for the
    # first response. Every answer below is a follow-up to this.
    await interaction.response.defer(ephemeral=True, thinking=True)
    if not await premium.feature_gate(PREMIUM_FEATURE, guild.id, interaction=interaction):
        await _reply(interaction, c.TICKET_UNAVAILABLE)
        return

    duty = duties_db.get_duty(guild.id, duty_id)
    if duty is None or duty.paused or not duty.contact_enabled:
        name = duty.name if duty else "That duty"
        await _reply(interaction, c.TICKET_DUTY_UNAVAILABLE.format(duty=name))
        # The button shouldn't have been there. Put the message right.
        await refresh_panel(interaction.client, guild.id)
        return

    named = [uid for uid in duty.all_holders() if guild.get_member(uid) is not None]
    role = _leadership_role(guild) if duty.any_anyone() else None
    # "Anyone in leadership" holding the duty puts every leader in the
    # thread, as named holders are.
    leaders = [m.id for m in role.members if not m.bot] if role is not None else []
    holders = list(dict.fromkeys(named + leaders))
    if not holders:
        await _reply(interaction, c.TICKET_NOBODY.format(duty=duty.name))
        return

    key = (guild.id, interaction.user.id, duty.id)
    recent = _recent.get(key)
    if recent is not None and time.monotonic() - recent[0] < COOLDOWN_SECONDS:
        await _reply(interaction, c.TICKET_RECENT.format(duty=duty.name, thread=f"<#{recent[1]}>"))
        return
    if key in _in_flight:
        # The first click is still opening it and will link it; this one
        # still owes the member an answer, having deferred.
        await _reply(interaction, c.TICKET_OPENING.format(duty=duty.name))
        return
    _in_flight.add(key)
    try:
        await _open(interaction, guild, duty, holders, key, named=named, role=role)
    finally:
        _in_flight.discard(key)


async def _open(interaction, guild, duty: d.Duty, holders: list[int], key, *, named, role) -> None:
    settings = duties_db.get_settings(guild.id)
    channel_id = settings.ticket_channel_id or settings.panel_channel_id or interaction.channel_id
    channel = guild.get_channel(channel_id)
    problem = ticket_channel_problem(channel, interaction.user, guild.me)
    if problem is not None:
        config_health.record(
            guild.id, duties_health.TICKET_CHANNEL, problem, "", discriminator=str(channel_id)
        )
        await _reply(interaction, c.TICKET_FAILED)
        return

    name = duties_render.thread_name(
        duty.name, interaction.user.display_name, datetime.now(timezone.utc)
    )
    try:
        thread = await channel.create_thread(
            name=name, type=discord.ChannelType.private_thread, invitable=False
        )
    except discord.Forbidden:
        config_health.record(
            guild.id,
            duties_health.TICKET_CHANNEL,
            config_health.NO_THREADS,
            "",
            discriminator=str(channel_id),
        )
        await _reply(interaction, c.TICKET_FAILED)
        return
    except discord.HTTPException as e:
        logger.warning("[DUTIES] guild=%s could not open a thread: %s", guild.id, e)
        await _reply(interaction, c.TICKET_FAILED)
        return

    config_health.clear(guild.id, duties_health.TICKET_CHANNEL)
    for uid in [interaction.user.id, *holders]:
        try:
            await thread.add_user(discord.Object(id=uid))
        except discord.HTTPException as e:
            # One holder who can't be added mustn't cost the member their
            # thread; the opener still mentions them.
            logger.info("[DUTIES] guild=%s could not add %s to a thread: %s", guild.id, uid, e)
    try:
        await thread.send(
            duties_render.thread_opener(
                duty,
                interaction.user.id,
                named,
                role_mention=role.mention if role is not None else "",
            ),
            view=TicketCloseView(),
            allowed_mentions=_MENTION_USERS,
        )
    except discord.HTTPException as e:
        logger.warning("[DUTIES] guild=%s thread opened but opener failed: %s", guild.id, e)

    _recent[key] = (time.monotonic(), thread.id)
    await _reply(interaction, c.TICKET_READY.format(thread=thread.mention))


# ── Closing ──────────────────────────────────────────────────────────────────


class TicketCloseView(discord.ui.View):
    """The Close button under a thread's opening message. Persistent and
    static: the thread it closes is always the one it is clicked in."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label=c.BTN_CLOSE, style=discord.ButtonStyle.secondary, custom_id="duties_ticket_close"
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        from storm_permissions import is_leader_or_admin

        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            return
        if not is_leader_or_admin(interaction):
            await interaction.response.send_message(c.CLOSE_DENIED, ephemeral=True)
            return
        # Public on purpose: everyone in the thread should see it was closed
        # and by whom, and that typing reopens it (DESIGN.md, Ephemerality).
        await interaction.response.send_message(
            c.THREAD_CLOSED.format(who=interaction.user.mention),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        try:
            # Archived, not locked: anyone in the thread typing again reopens
            # it, which is how a follow-up gets back to the same people.
            await thread.edit(archived=True, locked=False)
        except discord.HTTPException as e:
            logger.info("[DUTIES] could not archive thread %s: %s", thread.id, e)

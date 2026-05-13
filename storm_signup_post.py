"""
Storm sign-up post command (#124).

Leadership runs `/storm_post_signup event_type:DS|CS event_date:YYYY-MM-DD`
to publish a registration message in the alliance's configured sign-up
channel. The message embeds a `SignupView` (#123) so members click to
vote; the persistent-View infra handles vote capture + Sheet mirroring.

v1 scope: leadership-triggered only. Auto-scheduling (a recurring task
that fires N days before the next event day) is intentionally deferred
to a follow-up sub-issue — leadership posting on demand more closely
matches how alliances actually run storm prep.
"""

from __future__ import annotations

import datetime as _dt
import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


# ── Time labels ──────────────────────────────────────────────────────────────
#
# Game-defined slot times are rendered via `config.get_storm_slot_labels`
# (same helper TimeSelectView and the draft flow already use), so the
# sign-up message and the draft show consistent time labels.

def _slot_labels(event_type: str, guild_id: int) -> tuple[str, str]:
    """Return (label_a, label_b) for the two DS time slots, or
    (label_a, '') for CS (which uses a single slot per faction)."""
    from config import get_storm_slot_labels
    try:
        labels = get_storm_slot_labels(event_type, guild_id)
    except Exception:
        labels = []
    if event_type == "CS":
        return ((labels[0] if labels else ""), "")
    label_a = labels[0] if len(labels) > 0 else ""
    label_b = labels[1] if len(labels) > 1 else ""
    return (label_a, label_b)


def _build_registration_embed(event_type: str, event_date_iso: str,
                              time_a: str, time_b: str) -> discord.Embed:
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    emoji = "⚔️" if event_type == "DS" else "🏜️"
    try:
        d = _dt.date.fromisoformat(event_date_iso)
        date_pretty = d.strftime("%A, %B %d, %Y")
    except ValueError:
        date_pretty = event_date_iso
    desc = (
        f"Pick one option below. Changing your vote replaces the previous "
        f"one — feel free to update if your availability shifts before the event."
    )
    embed = discord.Embed(
        title=f"{emoji} {label} — Sign Up for {date_pretty}",
        description=desc,
        color=discord.Color.gold() if event_type == "DS" else discord.Color.orange(),
    )
    if time_a or time_b:
        time_lines = []
        if time_a:
            time_lines.append(f"• **{time_a}**")
        if time_b:
            time_lines.append(f"• **{time_b}**")
        embed.add_field(name="Available time slots", value="\n".join(time_lines), inline=False)
    embed.set_footer(text=f"Vote recorded with timestamp — leadership uses /storm_signups to review.")
    return embed


# ── Slash command ────────────────────────────────────────────────────────────


def _user_can_run(interaction: discord.Interaction) -> bool:
    from config import get_config
    member = interaction.user
    if isinstance(member, discord.Member) and member.guild_permissions.administrator:
        return True
    cfg = get_config(interaction.guild_id) if interaction.guild_id else None
    leader_role_id = getattr(cfg, "leader_role_id", 0) if cfg else 0
    if leader_role_id and isinstance(member, discord.Member):
        return any(r.id == leader_role_id for r in member.roles)
    return False


class StormSignupPostCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="storm_post_signup",
        description="Post a sign-up message for an upcoming Desert Storm or Canyon Storm event",
    )
    @app_commands.describe(
        event_type="Which event to post sign-ups for",
        event_date="Date of the event (YYYY-MM-DD)",
    )
    @app_commands.choices(event_type=[
        app_commands.Choice(name="Desert Storm", value="DS"),
        app_commands.Choice(name="Canyon Storm", value="CS"),
    ])
    async def storm_post_signup(
        self,
        interaction: discord.Interaction,
        event_type: app_commands.Choice[str],
        event_date: str,
    ):
        if not _user_can_run(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to post a storm sign-up.",
                ephemeral=True,
            )
            return

        et = event_type.value
        date_clean = event_date.strip()
        try:
            parsed_date = _dt.date.fromisoformat(date_clean)
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ `{event_date}` isn't a valid date. Use the format `YYYY-MM-DD` "
                f"(e.g. `2026-05-18`).",
                ephemeral=True,
            )
            return
        if parsed_date < _dt.date.today():
            await interaction.response.send_message(
                f"⚠️ Event date `{date_clean}` is in the past. Sign-ups should be "
                f"posted for upcoming events.",
                ephemeral=True,
            )
            return

        import config
        structured = config.get_structured_storm_config(interaction.guild_id, et)
        if not structured.get("structured_flow_enabled"):
            label = "Desert Storm" if et == "DS" else "Canyon Storm"
            cmd = "/setup_desertstorm" if et == "DS" else "/setup_canyonstorm"
            await interaction.response.send_message(
                f"⚠️ The structured roster flow isn't enabled for {label}. "
                f"Run `{cmd}` and turn on **Structured Roster Flow** (Premium) first.",
                ephemeral=True,
            )
            return

        channel_id = structured.get("signup_channel_id") or 0
        if not channel_id:
            cmd = "/setup_desertstorm" if et == "DS" else "/setup_canyonstorm"
            await interaction.response.send_message(
                f"⚠️ No sign-up channel configured. Run `{cmd}` and pick a "
                f"sign-up channel during the structured-flow setup.",
                ephemeral=True,
            )
            return

        target_channel = interaction.guild.get_channel(channel_id) if interaction.guild else None
        if target_channel is None:
            await interaction.response.send_message(
                f"⚠️ The configured sign-up channel (<#{channel_id}>) no longer "
                f"exists or the bot can't see it. Re-run setup to pick a new channel.",
                ephemeral=True,
            )
            return

        # Idempotence: re-running for the same event date short-circuits.
        if config.has_registration_post(interaction.guild_id, et, date_clean):
            await interaction.response.send_message(
                f"ℹ️ A sign-up post already exists for {date_clean} ({et}). "
                f"Check {target_channel.mention} for the existing post — members "
                f"can keep voting on it. If you need to re-post, delete the prior "
                f"message first.",
                ephemeral=True,
            )
            return

        # Build time labels from the canonical slot helper.
        time_a, time_b = _slot_labels(et, interaction.guild_id)

        # Defer the interaction so we can do a Sheet/Discord call without
        # blowing the 3-second initial response window.
        await interaction.response.defer(ephemeral=True)

        # Post the message + View.
        from storm_signup_view import SignupView
        view = SignupView(
            interaction.guild_id, et, date_clean,
            time_a_label=(time_a or "Team A"),
            time_b_label=(time_b or "Team B"),
        )
        embed = _build_registration_embed(et, date_clean, time_a, time_b)
        try:
            posted = await target_channel.send(embed=embed, view=view)
        except discord.Forbidden:
            await interaction.followup.send(
                f"⚠️ I don't have permission to send messages in "
                f"{target_channel.mention}. Check the channel permissions and try again.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            logger.warning("[STORM SIGNUP POST] Discord send failed: %s", e)
            await interaction.followup.send(
                "⚠️ Discord refused the message. See logs for details.",
                ephemeral=True,
            )
            return

        # Record the post so the startup hook can re-register the View
        # after a restart, and so re-running the command short-circuits.
        config.record_storm_registration_post(
            interaction.guild_id, et, date_clean,
            channel_id=target_channel.id,
            message_id=posted.id,
            time_a_label=(time_a or "Team A"),
            time_b_label=(time_b or "Team B"),
        )

        # Make sure the View is also registered against this exact
        # message_id immediately, so the persistence layer knows about it
        # before any vote click. (add_view is idempotent for the same
        # message_id + View.)
        try:
            self.bot.add_view(view, message_id=posted.id)
        except Exception as e:
            logger.warning(
                "[STORM SIGNUP POST] add_view failed for message=%s: %s",
                posted.id, e,
            )

        label = "Desert Storm" if et == "DS" else "Canyon Storm"
        await interaction.followup.send(
            f"✅ Sign-up post for {label} on **{date_clean}** is live in "
            f"{target_channel.mention}. Members can vote any time before the event. "
            f"Open `/storm_signups` to review who's voted.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StormSignupPostCog(bot))

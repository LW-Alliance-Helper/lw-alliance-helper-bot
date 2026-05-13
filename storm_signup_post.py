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


def _today_in_guild_tz(guild_id: int | None) -> _dt.date:
    """Today's date in the alliance's configured timezone, falling back
    to UTC if the guild has no timezone (or hasn't completed setup)."""
    from zoneinfo import ZoneInfo
    from config import get_config
    tz_name = ""
    if guild_id:
        cfg = get_config(guild_id)
        tz_name = (cfg.timezone if cfg else "") or ""
    try:
        tz = ZoneInfo(tz_name) if tz_name else _dt.timezone.utc
    except Exception:
        tz = _dt.timezone.utc
    return _dt.datetime.now(tz).date()


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
    embed.set_footer(text="Vote recorded with timestamp — leadership uses /storm_signups to review.")
    return embed


# ── Slash command ────────────────────────────────────────────────────────────


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
    @app_commands.guild_only()
    async def storm_post_signup(
        self,
        interaction: discord.Interaction,
        event_type: app_commands.Choice[str],
        event_date: str,
    ):
        from storm_permissions import (
            is_leader_or_admin,
            deny_non_leader,
            ensure_premium_structured,
        )

        if not is_leader_or_admin(interaction):
            await deny_non_leader(interaction)
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

        # Compare against today in the alliance's configured timezone, not
        # the host's local clock — Railway runs UTC, so an east-of-UTC
        # alliance posting near midnight their time would otherwise see
        # their own event date flagged "in the past".
        today_local = _today_in_guild_tz(interaction.guild_id)
        if parsed_date < today_local:
            await interaction.response.send_message(
                f"⚠️ Event date `{date_clean}` is in the past. Sign-ups should be "
                f"posted for upcoming events.",
                ephemeral=True,
            )
            return

        ok, structured = await ensure_premium_structured(
            interaction, et,
            bot=self.bot,
            feature_label="`/storm_post_signup`",
        )
        if not ok:
            return

        import config

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

        # Refuse to post if the configured slot labels are empty — members
        # would otherwise see a sign-up with buttons that lie about which
        # time they're voting for.
        if et == "DS" and not (time_a and time_b):
            cmd = "/setup_desertstorm"
            await interaction.response.send_message(
                f"⚠️ Both Desert Storm time slots need to be configured before "
                f"posting a sign-up. Run `{cmd}` and pick the two times first.",
                ephemeral=True,
            )
            return
        if et == "CS" and not time_a:
            cmd = "/setup_canyonstorm"
            await interaction.response.send_message(
                f"⚠️ The Canyon Storm time slot needs to be configured before "
                f"posting a sign-up. Run `{cmd}` and pick the time first.",
                ephemeral=True,
            )
            return

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

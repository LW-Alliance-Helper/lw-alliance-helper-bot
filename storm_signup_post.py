"""
Storm sign-up post command (#124).

Leadership runs `/desertstorm post_signup event_date:YYYY-MM-DD` (or the CS equivalent)
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
from typing import Optional

import discord

logger = logging.getLogger(__name__)


# ── Time labels ──────────────────────────────────────────────────────────────
#
# Game-defined slot times are rendered via `config.get_storm_slot_labels`
# (same helper TimeSelectView and the draft flow already use), so the
# sign-up message and the draft show consistent time labels.

def _slot_labels(event_type: str, guild_id: int) -> tuple[str, str]:
    """Return (label_a, label_b) for the two game-defined time slots.

    Both DS and CS have two slots (DS_SERVER_TIMES / CS_SERVER_TIMES per
    `config.py`); the alliance's `teams` config gates which slot(s) the
    post actually surfaces. Pre-Rule A / #166 the CS branch hardcoded
    single-slot, which contradicted #166's revert.
    """
    from config import get_storm_slot_labels
    try:
        labels = get_storm_slot_labels(event_type, guild_id)
    except Exception:
        labels = []
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
                              time_a: str, time_b: str,
                              teams: str = "both") -> discord.Embed:
    from storm_date_helpers import format_event_date

    # `time_a` / `time_b` / `teams` are accepted on the signature so
    # the caller (`post_registration`) can pass the configured slot
    # labels through to the SignupView's button labels (the view
    # surfaces the time on each button). The embed itself stays
    # minimal: title, one-line ask, and the vote-replacement disclaimer.
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    emoji = "⚔️" if event_type == "DS" else "🏜️"
    date_pretty = format_event_date(event_date_iso)
    desc = (
        f"Select your availability for {label}!\n"
        f"Only 1 vote can be recorded. If you select a 2nd one, it will "
        f"replace the first vote you cast."
    )
    embed = discord.Embed(
        title=f"{emoji} {label} — Sign Up for {date_pretty}",
        description=desc,
        color=discord.Color.gold() if event_type == "DS" else discord.Color.orange(),
    )
    return embed


# ── Reusable post helper ─────────────────────────────────────────────────────


async def post_registration(
    bot: discord.Client,
    guild: discord.Guild,
    event_type: str,
    event_date: str,
    *,
    structured: dict | None = None,
) -> dict:
    """Build and post a structured-flow sign-up message for one event.

    Idempotent on `(guild_id, event_type, event_date)` — if a post
    already exists, returns status `already_posted` without sending
    again. Used by both the leadership-triggered `/desertstorm post_signup`
    slash command (and the CS equivalent, which shape the response into
    user-facing copy) and the auto-scheduler loop (#131) (which logs status).

    Returns a dict carrying at minimum a `status` key. Possible values:
      * `ok`               — message sent + recorded; `message_id` and
                             `channel_id` populated.
      * `already_posted`   — registration post for this event already
                             exists; `channel_id` populated.
      * `no_channel`       — `signup_channel_id` isn't configured.
      * `channel_gone`     — channel_id set but the channel was deleted
                             or the bot can't see it.
      * `missing_slot_labels` — alliance hasn't set the time-option labels;
                                posting would surface buttons with empty labels.
      * `forbidden`        — channel.send raised Forbidden.
      * `send_failed`      — other Discord error during send; `error`
                             populated with str(exception).
    """
    import config
    from storm_signup_view import SignupView

    if structured is None:
        structured = config.get_structured_storm_config(guild.id, event_type)

    channel_id = int(structured.get("signup_channel_id") or 0)
    if not channel_id:
        return {"status": "no_channel"}

    channel = guild.get_channel(channel_id) if guild else None
    if channel is None:
        return {"status": "channel_gone", "channel_id": channel_id}

    if config.has_registration_post(guild.id, event_type, event_date):
        return {"status": "already_posted", "channel_id": channel_id}

    # Read the per-alliance team gate (#148 + Rule A / #166). Applies
    # identically to DS and CS — both events can be run as one team or
    # both, decided by leadership. Pre-#166 fix this path read DS config
    # for the CS event, which clobbered CS's own `teams` setting.
    cfg = config.get_storm_config(guild.id, event_type) or {}
    teams_setting = (cfg.get("teams") or "both").strip()
    if teams_setting not in ("both", "A", "B"):
        teams_setting = "both"

    time_a, time_b = _slot_labels(event_type, guild.id)
    # Slot-label validation gates only the slots an alliance actually
    # uses. A `teams=A` alliance with no Team B time configured is fine;
    # their post only needs the Team A label.
    needs_a = teams_setting in ("both", "A")
    needs_b = teams_setting in ("both", "B")
    if (needs_a and not time_a) or (needs_b and not time_b):
        return {"status": "missing_slot_labels", "channel_id": channel_id}

    view = SignupView(
        guild.id, event_type, event_date,
        time_a_label=(time_a or ""),
        time_b_label=(time_b or ""),
        teams=teams_setting,
    )
    embed = _build_registration_embed(
        event_type, event_date, time_a, time_b, teams=teams_setting,
    )
    try:
        posted = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        return {"status": "forbidden", "channel_id": channel_id}
    except discord.HTTPException as e:
        logger.warning(
            "[STORM SIGNUP POST] Discord send failed for guild=%s event=%s/%s: %s",
            guild.id, event_type, event_date, e,
        )
        return {"status": "send_failed", "channel_id": channel_id, "error": str(e)}

    config.record_storm_registration_post(
        guild.id, event_type, event_date,
        channel_id=channel.id,
        message_id=posted.id,
        time_a_label=(time_a or ""),
        time_b_label=(time_b or ""),
    )

    try:
        bot.add_view(view, message_id=posted.id)
    except Exception as e:
        logger.warning(
            "[STORM SIGNUP POST] add_view failed for message=%s: %s",
            posted.id, e,
        )

    return {
        "status":     "ok",
        "channel_id": channel.id,
        "message_id": posted.id,
    }


# ── Slash command handler ────────────────────────────────────────────────────
#
# The slash command itself is registered by `storm_commands_root` under the
# `/desertstorm post_signup` and `/canyonstorm post_signup` parents. This
# module just exposes the handler body so the root cog stays a thin
# dispatcher.


async def handle_post_signup(
    bot,
    interaction: discord.Interaction,
    event_type: str,
    event_date: Optional[str] = None,
) -> None:
    from storm_permissions import (
        is_leader_or_admin,
        deny_non_leader,
        ensure_premium_structured,
    )
    from storm_date_helpers import (
        parse_event_date, next_event_date, format_event_date,
    )

    if not is_leader_or_admin(interaction):
        await deny_non_leader(interaction)
        return

    et = event_type
    # Compare against today in the alliance's configured timezone, not
    # the host's local clock — Railway runs UTC, so an east-of-UTC
    # alliance posting near midnight their time would otherwise see
    # their own event date flagged "in the past".
    today_local = _today_in_guild_tz(interaction.guild_id)

    raw_input = (event_date or "").strip()
    if not raw_input:
        # No date passed — infer next configured event day, matching
        # the alliance's structured-flow schedule when set.
        date_clean = next_event_date(
            interaction.guild_id, et, today=today_local,
        )
    else:
        parsed = parse_event_date(raw_input, today=today_local)
        if parsed is None:
            await interaction.response.send_message(
                f"⚠️ `{event_date}` isn't a date I can parse. Try `May 18`, "
                f"`5/18`, `2026-05-18`, `Sunday`, or `tomorrow`.",
                ephemeral=True,
            )
            return
        date_clean = parsed.isoformat()

    parsed_date = _dt.date.fromisoformat(date_clean)
    if parsed_date < today_local:
        pretty = format_event_date(date_clean)
        await interaction.response.send_message(
            f"⚠️ Event date {pretty} is in the past. Sign-ups should be "
            f"posted for upcoming events.",
            ephemeral=True,
        )
        return

    feature_label = (
        f"`/{'desertstorm' if et == 'DS' else 'canyonstorm'} post_signup`"
    )
    ok, structured = await ensure_premium_structured(
        interaction, et,
        bot=bot,
        feature_label=feature_label,
    )
    if not ok:
        return

    # Defer so the post helper has headroom over the 3-second window.
    await interaction.response.defer(ephemeral=True)

    result = await post_registration(
        bot, interaction.guild, et, date_clean,
        structured=structured,
    )
    await interaction.followup.send(
        _format_post_result_message(et, date_clean, result),
        ephemeral=True,
    )


def _format_post_result_message(
    event_type: str, event_date: str, result: dict,
) -> str:
    """Render `post_registration`'s result dict into officer-facing copy.

    Used by the slash command. The scheduler logs against the same
    status codes but doesn't surface a user message.
    """
    from storm_date_helpers import format_event_date

    status = result.get("status")
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    setup_cmd = "/setup → ⚔️ Desert Storm" if event_type == "DS" else "/setup → 🏜️ Canyon Storm"
    date_pretty = format_event_date(event_date)

    parent = "desertstorm" if event_type == "DS" else "canyonstorm"
    if status == "ok":
        cid = result.get("channel_id")
        return (
            f"✅ Sign-up post for {label} on **{date_pretty}** is live in "
            f"<#{cid}>. Members can vote any time before the event. "
            f"Open `/{parent} signups` to review who's voted."
        )
    if status == "already_posted":
        cid = result.get("channel_id")
        return (
            f"ℹ️ A sign-up post already exists for {date_pretty} ({event_type}). "
            f"Check <#{cid}> for the existing post — members can keep voting on "
            f"it. If you need to re-post, delete the prior message first."
        )
    if status == "no_channel":
        return (
            f"⚠️ No sign-up channel configured. Run `{setup_cmd}` and pick a "
            f"sign-up channel during the structured-flow setup."
        )
    if status == "channel_gone":
        cid = result.get("channel_id")
        return (
            f"⚠️ The configured sign-up channel (<#{cid}>) no longer exists or "
            f"the bot can't see it. Re-run `{setup_cmd}` to pick a new channel."
        )
    if status == "missing_slot_labels":
        if event_type == "DS":
            return (
                f"⚠️ Both Desert Storm time slots need to be configured before "
                f"posting a sign-up. Run `{setup_cmd}` and pick the two times first."
            )
        return (
            f"⚠️ The Canyon Storm time slot needs to be configured before "
            f"posting a sign-up. Run `{setup_cmd}` and pick the time first."
        )
    if status == "forbidden":
        cid = result.get("channel_id")
        return (
            f"⚠️ I don't have permission to send messages in <#{cid}>. Check the "
            f"channel permissions and try again."
        )
    if status == "send_failed":
        err = (result.get("error") or "unknown error")[:120]
        return (
            f"⚠️ Discord refused the sign-up message: `{err}`. See bot logs for details."
        )
    return f"⚠️ Sign-up post returned unexpected status `{status}`."

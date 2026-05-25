"""
Storm sign-up post (#124).

Reached via the `📣 Post sign-up poll` button on `/desertstorm` and
`/canyonstorm` (hub-restructure #187; legacy
`/desertstorm post_signup event_date:YYYY-MM-DD` subcommand pre-#187).
The button publishes a registration message in the alliance's
configured sign-up channel. The message embeds a `SignupView` (#123)
so members click to vote; the persistent-View infra handles vote
capture + Sheet mirroring.

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


# ── Team-ordered slot labels ─────────────────────────────────────────────────
#
# Pre-#251 this helper returned the two game-defined slots in slot order
# (slot 1 then slot 2). Now it returns labels in TEAM order — Team A's
# label first, Team B's label second — driven by the per-guild
# `team_*_slot_index` mapping (or a per-week override the officer
# picked). When both teams share a slot the labels are intentionally
# identical; downstream callers (button rendering, mail `{time}`)
# treat that as the desired outcome rather than an anomaly.

def _slot_labels(
    event_type: str, guild_id: int,
    *,
    override_a_idx: int | None = None,
    override_b_idx: int | None = None,
    event_date: str | None = None,
) -> tuple[str, str]:
    """Return (team_a_label, team_b_label) for an event (#251).

    `override_a_idx` / `override_b_idx`, when set (1 or 2), pin the
    label for that team for this single render — used by the weekly
    override path when the officer picks a non-default slot. When
    overrides aren't supplied, falls back to `config.resolve_storm_team_slots`
    (which checks `storm_registration_posts` for a per-event mapping
    before falling back to the guild default).

    Returns empty strings for teams that haven't been assigned a slot
    yet — callers gate posting on whether the labels the alliance's
    `teams` setting requires are non-empty.
    """
    from config import (
        get_storm_slot_label_by_index, resolve_storm_team_slots,
    )

    if override_a_idx in (1, 2) or override_b_idx in (1, 2):
        a_idx = override_a_idx if override_a_idx in (1, 2) else None
        b_idx = override_b_idx if override_b_idx in (1, 2) else None
        # Fill in non-overridden side from the resolved guild/event mapping
        # so a partial override doesn't blank the other team's label.
        if a_idx is None or b_idx is None:
            resolved_a, resolved_b = resolve_storm_team_slots(
                guild_id, event_type, event_date,
            )
            if a_idx is None:
                a_idx = resolved_a
            if b_idx is None:
                b_idx = resolved_b
    else:
        a_idx, b_idx = resolve_storm_team_slots(
            guild_id, event_type, event_date,
        )

    return (
        get_storm_slot_label_by_index(event_type, a_idx, guild_id),
        get_storm_slot_label_by_index(event_type, b_idx, guild_id),
    )


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
        title=f"{emoji} {label}: Sign Up for {date_pretty}",
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
    override_a_idx: int | None = None,
    override_b_idx: int | None = None,
    force: bool = False,
) -> dict:
    """Build and post a structured-flow sign-up message for one event.

    Used by both the leadership-triggered `📣 Post sign-up poll` hub
    button (which shapes the response into user-facing copy) and the
    auto-scheduler loop (#131) (which logs status).

    `force` (#265) controls the once-per-event guard:
      * `force=False` (default, auto-scheduler) — returns
        `already_posted` without sending if a registration post already
        exists for this event. Prevents the daily scheduler tick from
        re-posting after a successful tick earlier in the day.
      * `force=True` (manual leadership clicks) — skips the guard and
        always posts. Votes still aggregate to the same event in
        SQLite, so multiple live posts collect into one tally.
        Persistent-View re-registration attaches to every recorded
        message_id on startup, so old posts stay clickable.

    `override_a_idx` / `override_b_idx`, when set (1 or 2), pin the
    team→slot mapping for this single event, recorded on the
    `storm_registration_posts` row. Without them, the guild default
    from `guild_storm_config.team_*_slot_index` is used. The status
    `missing_slot_labels` now signals "leadership hasn't picked the
    team→slot mapping yet for the slots the alliance's `teams` setting
    requires" (#251), not "no game-time constants" — the constants are
    always defined.

    Returns a dict carrying at minimum a `status` key. Possible values:
      * `ok`               — message sent + recorded; `message_id` and
                             `channel_id` populated.
      * `already_posted`   — registration post for this event already
                             exists; `channel_id` populated. Only
                             returned when `force=False`.
      * `no_channel`       — `signup_channel_id` isn't configured.
      * `channel_gone`     — channel_id set but the channel was deleted
                             or the bot can't see it.
      * `missing_slot_labels` — alliance hasn't set Team A's / Team B's
                                slot mapping for this event type yet.
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

    if not force and config.has_registration_post(guild.id, event_type, event_date):
        return {"status": "already_posted", "channel_id": channel_id}

    # Read the per-alliance team gate (#148 + Rule A / #166). Applies
    # identically to DS and CS — both events can be run as one team or
    # both, decided by leadership. Pre-#166 fix this path read DS config
    # for the CS event, which clobbered CS's own `teams` setting.
    cfg = config.get_storm_config(guild.id, event_type) or {}
    teams_setting = (cfg.get("teams") or "both").strip()
    if teams_setting not in ("both", "A", "B"):
        teams_setting = "both"

    # Resolve final team→slot indices for this event. Overrides win;
    # otherwise the guild default applies.
    resolved_a, resolved_b = config.resolve_storm_team_slots(
        guild.id, event_type, event_date,
    )
    final_a_idx = override_a_idx if override_a_idx in (1, 2) else resolved_a
    final_b_idx = override_b_idx if override_b_idx in (1, 2) else resolved_b

    time_a, time_b = _slot_labels(
        event_type, guild.id,
        override_a_idx=final_a_idx,
        override_b_idx=final_b_idx,
        event_date=event_date,
    )
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
        team_a_slot_index=final_a_idx or 0,
        team_b_slot_index=final_b_idx or 0,
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


# ── Hub button handler ───────────────────────────────────────────────────────
#
# Wired from the `📣 Post sign-up poll` button on the `/desertstorm`
# and `/canyonstorm` event hubs (storm_event_hub.py). This module
# exposes the handler body so the hub stays a thin dispatcher.


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

    # Pre-check the team→slot mapping (#251) before opening the override
    # picker — there's no point asking "keep or override" when there's
    # no default to keep yet.
    import config as _config
    cfg = _config.get_storm_config(interaction.guild_id, et) or {}
    teams_setting = (cfg.get("teams") or "both").strip()
    if teams_setting not in ("both", "A", "B"):
        teams_setting = "both"
    default_a_idx, default_b_idx = _config.resolve_storm_team_slots(
        interaction.guild_id, et, date_clean,
    )
    needs_a = teams_setting in ("both", "A")
    needs_b = teams_setting in ("both", "B")
    if (needs_a and default_a_idx not in (1, 2)) or (needs_b and default_b_idx not in (1, 2)):
        await interaction.followup.send(
            _format_post_result_message(
                et, date_clean, {"status": "missing_slot_labels"},
            ),
            ephemeral=True,
        )
        return

    # Run the per-week confirm + optional override picker. Returns the
    # indices to post with (either the defaults or the officer's pick),
    # or None if the officer cancelled / timed out.
    chosen = await _run_post_signup_confirm_flow(
        interaction, et, date_clean,
        teams_setting=teams_setting,
        default_a_idx=default_a_idx, default_b_idx=default_b_idx,
    )
    if chosen is None:
        return
    final_a_idx, final_b_idx = chosen

    result = await post_registration(
        bot, interaction.guild, et, date_clean,
        structured=structured,
        override_a_idx=final_a_idx,
        override_b_idx=final_b_idx,
        # Leadership-triggered repost (#265): bypass the once-per-event
        # guard so a fresh post can land even if an earlier one is
        # still on the channel. Votes aggregate into the same event.
        force=True,
    )
    await interaction.followup.send(
        _format_post_result_message(et, date_clean, result),
        ephemeral=True,
    )


async def _run_post_signup_confirm_flow(
    interaction: discord.Interaction,
    event_type: str,
    event_date: str,
    *,
    teams_setting: str,
    default_a_idx: int | None,
    default_b_idx: int | None,
) -> tuple[int | None, int | None] | None:
    """Confirm-and-optionally-override flow for the weekly sign-up post (#251).

    Renders an ephemeral confirmation listing the team→slot mapping in
    effect (the guild default) and offers three actions:

      • **Post with these times** — use the guild default.
      • **Override for this week** — walk a sequential per-team slot
        picker (one ephemeral message per running team), then post
        with the picked indices. The guild default is not modified.
      • **Cancel** — bail out without posting.

    Returns `(team_a_slot_index, team_b_slot_index)` for the post (each
    either 1, 2, or None for an unused team), or `None` if the officer
    cancelled or timed out.
    """
    from config import get_storm_slot_labels
    from storm_date_helpers import format_event_date
    try:
        slot_labels = get_storm_slot_labels(event_type, interaction.guild_id)
    except Exception:
        slot_labels = []

    def _label_for(idx):
        if idx in (1, 2) and len(slot_labels) >= idx:
            return slot_labels[idx - 1]
        return "—"

    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    emoji = "⚔️" if event_type == "DS" else "🏜️"
    date_pretty = format_event_date(event_date)

    needs_a = teams_setting in ("both", "A")
    needs_b = teams_setting in ("both", "B")

    summary_lines = [f"{emoji} **{label} sign-up for {date_pretty}**", ""]
    if needs_a:
        summary_lines.append(f"🅰️ Team A: **{_label_for(default_a_idx)}**")
    if needs_b:
        summary_lines.append(f"🅱️ Team B: **{_label_for(default_b_idx)}**")
    summary_lines.append("")
    summary_lines.append(
        "Post with these times, or override them for this week only?"
    )

    class _ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.outcome: str | None = None  # "post" | "override" | "cancel"

        @discord.ui.button(label="✅ Post with these times", style=discord.ButtonStyle.success)
        async def post(self, inter: discord.Interaction, button: discord.ui.Button):
            self.outcome = "post"
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(view=self)
            self.stop()

        @discord.ui.button(label="✏️ Override for this week", style=discord.ButtonStyle.primary)
        async def override(self, inter: discord.Interaction, button: discord.ui.Button):
            self.outcome = "override"
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(view=self)
            self.stop()

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
            self.outcome = "cancel"
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(view=self)
            self.stop()

    confirm_view = _ConfirmView()
    await interaction.followup.send(
        "\n".join(summary_lines), view=confirm_view, ephemeral=True,
    )
    timed_out = await confirm_view.wait()
    if timed_out or confirm_view.outcome in (None, "cancel"):
        if confirm_view.outcome != "cancel":
            await interaction.followup.send(
                "⏰ Timed out. Click **📣 Post sign-up poll** again to retry.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "❌ Sign-up post cancelled. Nothing was posted.",
                ephemeral=True,
            )
        return None

    if confirm_view.outcome == "post":
        return (default_a_idx, default_b_idx)

    # ── Override path: pick per running team ──────────────────────────
    async def _pick(team_letter: str, current_idx) -> int | None:
        class _SlotPick(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=180)
                self.selected: int | None = None

            @discord.ui.button(label=slot_labels[0] if len(slot_labels) > 0 else "Slot 1",
                                style=discord.ButtonStyle.primary)
            async def s1(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 1
                for item in self.children:
                    item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label=slot_labels[1] if len(slot_labels) > 1 else "Slot 2",
                                style=discord.ButtonStyle.primary)
            async def s2(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 2
                for item in self.children:
                    item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="Keep current default", style=discord.ButtonStyle.success)
            async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = current_idx if current_idx in (1, 2) else None
                for item in self.children:
                    item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        pick = _SlotPick()
        await interaction.followup.send(
            f"Which time slot does **Team {team_letter}** run this week?\n"
            f"Current default: **{_label_for(current_idx)}**",
            view=pick, ephemeral=True,
        )
        timed_out = await pick.wait()
        if timed_out or pick.selected is None:
            await interaction.followup.send(
                "⏰ Override timed out. Nothing was posted.",
                ephemeral=True,
            )
            return None
        return pick.selected

    new_a_idx = default_a_idx
    new_b_idx = default_b_idx
    if needs_a:
        picked = await _pick("A", default_a_idx)
        if picked is None:
            return None
        new_a_idx = picked
    if needs_b:
        picked = await _pick("B", default_b_idx)
        if picked is None:
            return None
        new_b_idx = picked

    return (new_a_idx, new_b_idx)


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

    from storm_event_hub import HUB_COMMAND, HUB_BTN_VIEW_SIGNUPS
    hub_cmd = HUB_COMMAND[event_type]
    if status == "ok":
        cid = result.get("channel_id")
        return (
            f"✅ Sign-up post for {label} on **{date_pretty}** is live in "
            f"<#{cid}>. Members can vote any time before the event. "
            f"Run `{hub_cmd}` and click **{HUB_BTN_VIEW_SIGNUPS}** to "
            f"review who's voted."
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
        return (
            f"⚠️ Your alliance hasn't picked which time slot each team runs "
            f"at for {label} yet. Run `{setup_cmd}` and complete **Step 3: "
            f"Team Time Slots** first — you can override the picks for a "
            f"single week from this same button after the default is set."
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

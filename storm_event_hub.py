"""
storm_event_hub.py: the single `/desertstorm` (and `/canyonstorm`)
event-hub entry point that replaced 11 separate subcommands per
event type (#187).

Officers hit one command per event type. The hub embed surfaces the
alliance's current config + a button grid that dispatches into every
existing storm flow. Premium-gated buttons render disabled on the
free tier with a click-upsell, so leadership can see at a glance
which capabilities are unlocked.

This module owns the embed builder and the button view; the actual
flow handlers live in their existing modules (storm.py,
storm_log.py, storm_signup_post.py, storm_officer_view.py,
storm_attendance.py, storm_strategy.py, storm_member_rules.py,
storm_history.py). Each button is a thin dispatcher.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord

from messages import DENY_NOT_OWNER

logger = logging.getLogger(__name__)


_EVENT_LABEL = {"DS": "Desert Storm", "CS": "Canyon Storm"}
_EVENT_EMOJI = {"DS": "⚔️", "CS": "🏜️"}
_PARENT_CMD = {"DS": "desertstorm", "CS": "canyonstorm"}
_FIXED_EVENT_DAY = {"DS": "Friday", "CS": "Thursday"}

# Canonical slash-command surface, with leading slash. Use this in
# user-facing copy that tells officers which hub to open.
HUB_COMMAND = {"DS": "/desertstorm", "CS": "/canyonstorm"}

# ── Button labels (exported) ─────────────────────────────────────────────────
#
# Single source of truth for the hub's button labels. Imported by every
# module that quotes a button name in error / timeout / followup copy,
# so a rename here updates every caller automatically.
#
# These are the "active" (premium-unlocked) forms. The free-tier locked
# form is derived via `_locked()` so we don't carry both spellings.
HUB_BTN_POST_SIGNUP = "📣 Post sign-up poll"
HUB_BTN_VIEW_SIGNUPS = "👁️ View sign-ups + set up teams"
HUB_BTN_ATTENDANCE = "📋 Record attendance"
HUB_BTN_PARTICIPATION = "📊 Fill out participation questions"
HUB_BTN_REMIND = "🔔 Send DM reminder to roster"
HUB_BTN_PRESETS = "🧮 Manage strategy presets"
HUB_BTN_RULES = "👤 Manage member rules"
HUB_BTN_DRAFT = "📄 Generate mail"
HUB_BTN_LOGS = "📜 View past participation logs"
HUB_BTN_TRENDS = "🔍 View trends across events"
HUB_BTN_PAST_ROSTERS = "📜 View past rosters"
HUB_BTN_SETUP = "⚙️ Open setup"


def _locked(label: str) -> str:
    """Free-tier-locked variant of a hub button label: replace the
    leading emoji with 💎. Keeps the active label as the single source
    of truth — the locked form is computed, not stored separately."""
    if " " not in label:
        return f"💎 {label}"
    _, rest = label.split(" ", 1)
    return f"💎 {rest}"


# ── Embed builder ────────────────────────────────────────────────────────────


def _build_event_hub_embed(
    guild: discord.Guild,
    event_type: str,
    *,
    is_premium: bool,
) -> discord.Embed:
    """Build the hub embed showing the alliance's current storm config
    plus a one-glance "what's available right now" summary. Premium
    flag drives the Structured Flow line; everything else is observed
    from the saved config + game-defined event schedule.

    The embed body deliberately omits the slash-command-style verbose
    explanations the old `/desertstorm overview` showed. Officers see
    actions in the button row, not the embed body.
    """
    import config
    from storm_date_helpers import next_event_date, format_event_date

    label = _EVENT_LABEL[event_type]
    emoji = _EVENT_EMOJI[event_type]
    color = discord.Color.gold() if event_type == "DS" else discord.Color.orange()

    # Pull config — fall through to defaults if any piece isn't configured.
    try:
        cfg = config.get_storm_config(guild.id, event_type) or {}
    except Exception:
        cfg = {}
    try:
        structured = config.get_structured_storm_config(guild.id, event_type) or {}
    except Exception:
        structured = {}

    teams_setting = (cfg.get("teams") or "both").strip()
    teams_display = {
        "both": "A & B",
        "A":    "Team A only",
        "B":    "Team B only",
    }.get(teams_setting, "A & B")

    signup_channel_id = int(structured.get("signup_channel_id") or 0)
    signup_channel_line = (
        f"<#{signup_channel_id}>" if signup_channel_id else "_not configured_"
    )

    # Poll auto-schedule line. Use an explicit `is None` check rather
    # than `or -1`: `poll_day_of_week == 0` is Monday, which is falsy
    # and would silently collapse to -1 (dropping the schedule line)
    # if we used `or`.
    poll_dow_raw = structured.get("poll_day_of_week")
    poll_dow = int(poll_dow_raw) if poll_dow_raw is not None else -1
    poll_time = (structured.get("signup_time") or "").strip()
    if poll_dow >= 0 and poll_time:
        _DOW = ["Monday", "Tuesday", "Wednesday", "Thursday",
                "Friday", "Saturday", "Sunday"]
        signup_channel_line += (
            f"\n   auto-posted {_DOW[poll_dow]} at {poll_time} server time"
        )

    structured_on = bool(structured.get("structured_flow_enabled"))
    power_column = (structured.get("power_metric_column") or "").strip().upper()
    if structured_on and power_column:
        structured_line = f"✅ Enabled (power column **{power_column}**)"
    elif structured_on:
        structured_on = True
        structured_line = "✅ Enabled"
    else:
        structured_line = "⚪ Not enabled (free-tier flow only)"

    # Preset count.
    try:
        import storm_strategy as ss
        preset_count = len(ss.list_presets(guild.id, event_type) or [])
    except Exception:
        preset_count = 0

    # Next event date.
    try:
        next_iso = next_event_date(guild.id, event_type)
        next_event_line = format_event_date(next_iso)
    except Exception:
        next_event_line = (
            f"next {_FIXED_EVENT_DAY[event_type]} "
            f"(game-defined)"
        )

    description_lines = [
        f"📅 **Next event:** {next_event_line}",
        f"📍 **Sign-up post:** {signup_channel_line}",
        f"🧑‍🤝‍🧑 **Teams:** {teams_display}",
        f"📋 **Presets saved:** {preset_count}",
        f"💎 **Structured Flow:** {structured_line}",
    ]
    if not is_premium:
        description_lines.append(
            "\n_Free tier: Premium-only buttons below are disabled. "
            "Run `/upgrade` to unlock the structured roster flow._"
        )

    embed = discord.Embed(
        title=f"{emoji} {label}: {guild.name}",
        description="\n".join(description_lines),
        color=color,
    )
    embed.set_footer(
        text=f"Pick a button below. Re-run /{_PARENT_CMD[event_type]} "
             f"any time to refresh this view.",
    )
    return embed


# ── Hub view ─────────────────────────────────────────────────────────────────


class _EventHubView(discord.ui.View):
    """Hub button grid. Each button dispatches into an existing storm
    handler. Premium-gated buttons render disabled on the free tier.

    Layout (3 rows, 11 buttons total):
        Row 0 (active event-day actions):
          📣 Post sign-up poll (blue) | 👁️ View sign-ups + set up teams (green) |
          📋 Record attendance | 📊 Fill out participation questions
        Row 1 (Communications + configuration):
          🔔 Send DM reminder to roster | 🧮 Manage strategy presets |
          👤 Manage member rules | 📄 Generate mail
        Row 2 (Reference + setup):
          📜 View past participation logs | 📜 View past rosters |
          ⚙️ Open setup

    Only the two primary action buttons in Row 0 use a coloured style
    (Post sign-up poll → primary blue, View sign-ups + set up teams →
    success green). Everything else is `ButtonStyle.secondary` so the
    visual hierarchy points officers at "what do I do RIGHT NOW for
    this event" without competing with the reference / config rows.

    `owner_user_id` is the leadership member who ran `/desertstorm`.
    Discord caps the View at 25 components; 11 well under.
    """

    def __init__(
        self,
        bot,
        guild_id: int,
        event_type: str,
        owner_user_id: int,
        is_premium: bool,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        self.event_type = event_type
        self.owner_user_id = owner_user_id
        self.is_premium = is_premium
        self.message: Optional[discord.Message] = None
        self._build_buttons()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        """Only the officer who opened the hub can click buttons. Same
        pattern every other shared view uses."""
        if inter.user.id != self.owner_user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message
        await expire_view_message(
            self.message,
            command_hint=f"/{_PARENT_CMD[self.event_type]}",
        )

    def _premium_disabled(self) -> bool:
        return not self.is_premium

    def _build_buttons(self) -> None:
        premium_off = self._premium_disabled()

        # ── Row 0: active event-day actions ──────────────────────────────
        # Post sign-up poll (Premium): blue, the "start the cycle"
        # action. Most events begin with this button click.
        self._add_button(
            label=_locked(HUB_BTN_POST_SIGNUP) if premium_off else HUB_BTN_POST_SIGNUP,
            style=discord.ButtonStyle.primary,
            disabled=premium_off,
            row=0,
            callback=self._on_post_signup,
        )
        # View sign-ups + set up teams (Premium): green, the "build
        # the roster" action. Highest-traffic button once the poll is
        # up — gets the most visual weight after Post.
        self._add_button(
            label=_locked(HUB_BTN_VIEW_SIGNUPS) if premium_off else HUB_BTN_VIEW_SIGNUPS,
            style=discord.ButtonStyle.success,
            disabled=premium_off,
            row=0,
            callback=self._on_view_signups,
        )
        # Record attendance (Premium): secondary — same row because
        # it's part of the event-day flow, but visually deprioritised
        # so officers don't click it mid-build by accident.
        self._add_button(
            label=_locked(HUB_BTN_ATTENDANCE) if premium_off else HUB_BTN_ATTENDANCE,
            style=discord.ButtonStyle.secondary,
            disabled=premium_off,
            row=0,
            callback=self._on_attendance,
        )
        # Fill out participation questions (free tier): same row
        # because participation logs are an event-day chore.
        self._add_button(
            label=HUB_BTN_PARTICIPATION,
            style=discord.ButtonStyle.secondary,
            disabled=False,
            row=0,
            callback=self._on_participation,
        )

        # ── Row 1: Communications + configuration ────────────────────────
        # DM roster reminder (Premium): leads the comms/config row.
        self._add_button(
            label=_locked(HUB_BTN_REMIND) if premium_off else HUB_BTN_REMIND,
            style=discord.ButtonStyle.secondary,
            disabled=premium_off,
            row=1,
            callback=self._on_remind,
        )
        self._add_button(
            label=HUB_BTN_PRESETS,
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_manage_presets,
        )
        self._add_button(
            label=HUB_BTN_RULES,
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_manage_rules,
        )
        self._add_button(
            label=HUB_BTN_DRAFT,
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_draft,
        )

        # ── Row 2: Reference + setup ─────────────────────────────────────
        self._add_button(
            label=HUB_BTN_LOGS,
            style=discord.ButtonStyle.secondary,
            disabled=False, row=2,
            callback=self._on_log,
        )
        # Trends Viewer (Premium): same reference row as Logs and Past
        # Rosters — same data lineage (post-event lookback) just a
        # different lens.
        self._add_button(
            label=_locked(HUB_BTN_TRENDS) if premium_off else HUB_BTN_TRENDS,
            style=discord.ButtonStyle.secondary,
            disabled=premium_off,
            row=2,
            callback=self._on_trends,
        )
        self._add_button(
            label=_locked(HUB_BTN_PAST_ROSTERS) if premium_off else HUB_BTN_PAST_ROSTERS,
            style=discord.ButtonStyle.secondary,
            disabled=premium_off,
            row=2,
            callback=self._on_past_rosters,
        )
        self._add_button(
            label=HUB_BTN_SETUP,
            style=discord.ButtonStyle.secondary,
            disabled=False, row=2,
            callback=self._on_setup,
        )

    def _add_button(
        self,
        *,
        label: str,
        style: discord.ButtonStyle,
        disabled: bool,
        row: int,
        callback,
    ) -> None:
        btn = discord.ui.Button(
            label=label[:80],  # Discord button-label cap
            style=style,
            disabled=disabled,
            row=row,
        )
        btn.callback = callback
        self.add_item(btn)

    # ── Button callbacks ────────────────────────────────────────────────
    # Each one dispatches to an existing handler. Premium-gated handlers
    # already do their own ensure_premium_structured check, so even if a
    # disabled button is bypassed somehow, the upsell still fires.

    async def _on_post_signup(self, inter: discord.Interaction) -> None:
        from storm_signup_post import handle_post_signup
        await handle_post_signup(self.bot, inter, self.event_type, None)

    async def _on_view_signups(self, inter: discord.Interaction) -> None:
        from storm_officer_view import handle_storm_signups
        await handle_storm_signups(self.bot, inter, self.event_type, None)

    async def _on_attendance(self, inter: discord.Interaction) -> None:
        from storm_attendance import handle_storm_attendance
        await handle_storm_attendance(self.bot, inter, self.event_type, None)

    async def _on_past_rosters(self, inter: discord.Interaction) -> None:
        from storm_history import open_history
        await open_history(inter, self.event_type, None)

    async def _on_remind(self, inter: discord.Interaction) -> None:
        from storm_log import handle_storm_remind
        await handle_storm_remind(self.bot, inter, self.event_type)

    async def _on_manage_presets(self, inter: discord.Interaction) -> None:
        # Opens the strategy list view (#169 already gives it the
        # Create/Edit/Delete inline buttons, so the hub doesn't need
        # to add a sub-hub for preset CRUD).
        from storm_strategy import open_strategy_list
        await open_strategy_list(inter, self.event_type)

    async def _on_manage_rules(self, inter: discord.Interaction) -> None:
        # Same shape as Manage presets: open the rule-list view which
        # already has [➕ Add rule] + [🗑 Clear N] buttons per #169.
        from storm_member_rules import open_member_rule_list
        await open_member_rule_list(inter, self.event_type, member_filter=None)

    async def _on_draft(self, inter: discord.Interaction) -> None:
        from storm import handle_storm_draft
        await handle_storm_draft(self.bot, inter, self.event_type)

    async def _on_participation(self, inter: discord.Interaction) -> None:
        from storm_log import handle_storm_participation
        await handle_storm_participation(self.bot, inter, self.event_type)

    async def _on_log(self, inter: discord.Interaction) -> None:
        from storm_log import handle_storm_log
        await handle_storm_log(self.bot, inter, self.event_type, None)

    async def _on_trends(self, inter: discord.Interaction) -> None:
        from storm_trends import handle_storm_trends
        await handle_storm_trends(self.bot, inter, self.event_type)

    async def _on_setup(self, inter: discord.Interaction) -> None:
        # Post-#201: the per-feature setup wizards moved behind the
        # /setup hub buttons, with their bodies exposed as standalone
        # launcher helpers. Dispatch into `_launch_storm_setup`
        # directly so the storm hub's ⚙️ Open setup button opens
        # the wizard inline instead of telling officers to type
        # another slash command. Disable the hub buttons first so
        # officers can't double-fire the wizard by clicking again
        # mid-flow (matches the /growth Edit Config pattern).
        import wizard_registry
        from setup_cog import _launch_storm_setup
        for item in self.children:
            item.disabled = True
        try:
            await wizard_registry.safe_edit_response(inter, view=self)
        except Exception:
            pass
        self.stop()
        await _launch_storm_setup(inter, self.bot, self.event_type)


# ── Entry point ──────────────────────────────────────────────────────────────


async def handle_event_hub(
    bot,
    interaction: discord.Interaction,
    event_type: str,
) -> None:
    """Top-level handler for `/desertstorm` and `/canyonstorm`.

    Leadership-gated. Premium status is observed (drives button enable
    state) but doesn't gate hub access — the hub is the free-tier
    discovery surface too, with Premium buttons clearly marked.
    """
    from storm_permissions import is_leader_or_admin, deny_non_leader

    if not is_leader_or_admin(interaction):
        await deny_non_leader(interaction)
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "⚠️ This command must be used inside a server.",
            ephemeral=True,
        )
        return

    # Premium flag drives button disable state. Read it before we
    # build the view; cache on the view instance so callbacks don't
    # re-check.
    import premium
    try:
        is_premium = bool(await premium.is_premium(guild.id, bot=bot))
    except Exception as e:
        logger.warning(
            "[STORM HUB] premium check failed for guild=%s event=%s: %s",
            guild.id, event_type, e,
        )
        is_premium = False

    # `_build_event_hub_embed` reads strategy presets from gspread, which
    # is a blocking network call. Run it off the event loop so the bot's
    # heartbeat doesn't stall on cold connections (the Railway redeploy
    # boot path was hitting 10-20s blocks before this `to_thread` wrap).
    embed = await asyncio.to_thread(
        _build_event_hub_embed, guild, event_type, is_premium=is_premium,
    )
    view = _EventHubView(
        bot=bot,
        guild_id=guild.id,
        event_type=event_type,
        owner_user_id=interaction.user.id,
        is_premium=is_premium,
    )

    # Ephemeral — only the officer who ran the command sees the hub.
    # Avoids two problems with the previous non-ephemeral approach:
    # (1) when multiple officers run /desertstorm in the same channel,
    # the channel collects duplicate visible-but-unclickable embeds;
    # (2) the owner-only interaction_check meant other leadership
    # could see the hub but couldn't act on it, which read as a bug.
    # Each officer gets their own ephemeral hub instance.
    if interaction.response.is_done():
        sent = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        sent = await interaction.original_response()
    view.message = sent

    # First-run tour offer (#190): the hub is now the front door for
    # the storm flow, so the tour fires here instead of the legacy
    # `/<event> signups` officer-view trigger. Sent as an ephemeral
    # followup so only the officer who opened the hub sees it. The
    # offer no-ops if the officer has already dismissed it.
    try:
        from storm_walkthrough import maybe_offer_storm_hub_tour
        await maybe_offer_storm_hub_tour(
            interaction, event_type=event_type,
        )
    except Exception as e:
        # Walkthrough offer is non-essential; don't let a tour-side
        # failure (DB error, ephemeral send fail) take down the hub.
        logger.warning(
            "[STORM HUB] tour offer failed for guild=%s event=%s: %s",
            guild.id, event_type, e,
        )

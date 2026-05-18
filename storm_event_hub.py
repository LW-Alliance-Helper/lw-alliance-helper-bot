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

import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)


_EVENT_LABEL = {"DS": "Desert Storm", "CS": "Canyon Storm"}
_EVENT_EMOJI = {"DS": "⚔️", "CS": "🏜️"}
_PARENT_CMD = {"DS": "desertstorm", "CS": "canyonstorm"}
_FIXED_EVENT_DAY = {"DS": "Friday", "CS": "Thursday"}


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

    # Poll auto-schedule line.
    poll_dow = int(structured.get("poll_day_of_week", -1) or -1)
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
            "\n_Free tier — Premium-only buttons below are disabled. "
            "Run `/upgrade` to unlock the structured roster flow._"
        )

    embed = discord.Embed(
        title=f"{emoji} {label} — {guild.name}",
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
                "⛔ Only the officer who opened this view can use it.",
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
            label="💎 Post sign-up poll" if premium_off else "📣 Post sign-up poll",
            style=discord.ButtonStyle.primary,
            disabled=premium_off,
            row=0,
            callback=self._on_post_signup,
        )
        # View sign-ups + set up teams (Premium): green, the "build
        # the roster" action. Highest-traffic button once the poll is
        # up — gets the most visual weight after Post.
        self._add_button(
            label="💎 View sign-ups + set up teams" if premium_off else "👁️ View sign-ups + set up teams",
            style=discord.ButtonStyle.success,
            disabled=premium_off,
            row=0,
            callback=self._on_view_signups,
        )
        # Record attendance (Premium): secondary — same row because
        # it's part of the event-day flow, but visually deprioritised
        # so officers don't click it mid-build by accident.
        self._add_button(
            label="💎 Record attendance" if premium_off else "📋 Record attendance",
            style=discord.ButtonStyle.secondary,
            disabled=premium_off,
            row=0,
            callback=self._on_attendance,
        )
        # Fill out participation questions (free tier): same row
        # because participation logs are an event-day chore.
        self._add_button(
            label="📊 Fill out participation questions",
            style=discord.ButtonStyle.secondary,
            disabled=False,
            row=0,
            callback=self._on_participation,
        )

        # ── Row 1: Communications + configuration ────────────────────────
        # DM roster reminder (Premium): leads the comms/config row.
        self._add_button(
            label="💎 Send DM reminder to roster" if premium_off else "🔔 Send DM reminder to roster",
            style=discord.ButtonStyle.secondary,
            disabled=premium_off,
            row=1,
            callback=self._on_remind,
        )
        self._add_button(
            label="🧮 Manage strategy presets",
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_manage_presets,
        )
        self._add_button(
            label="👤 Manage member rules",
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_manage_rules,
        )
        self._add_button(
            label="📄 Generate mail",
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_draft,
        )

        # ── Row 2: Reference + setup ─────────────────────────────────────
        self._add_button(
            label="📜 View past participation logs",
            style=discord.ButtonStyle.secondary,
            disabled=False, row=2,
            callback=self._on_log,
        )
        self._add_button(
            label="💎 View past rosters" if premium_off else "📜 View past rosters",
            style=discord.ButtonStyle.secondary,
            disabled=premium_off,
            row=2,
            callback=self._on_past_rosters,
        )
        self._add_button(
            label="⚙️ Open setup",
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

    async def _on_setup(self, inter: discord.Interaction) -> None:
        # Post-#201: the per-feature setup wizards moved behind the
        # /setup hub buttons, with their bodies exposed as standalone
        # launcher helpers. Dispatch into `_launch_storm_setup`
        # directly so the storm hub's ⚙️ Open setup wizard button
        # opens the wizard inline instead of telling officers to type
        # another slash command. Same gates as the hub-button path
        # (leadership-or-admin + channel-perms via _check_wizard_can_run).
        from setup_cog import _launch_storm_setup
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

    embed = _build_event_hub_embed(guild, event_type, is_premium=is_premium)
    view = _EventHubView(
        bot=bot,
        guild_id=guild.id,
        event_type=event_type,
        owner_user_id=interaction.user.id,
        is_premium=is_premium,
    )

    # Send as a regular (non-ephemeral) message so other leadership
    # can see the same hub if Kevin shares the channel. The
    # interaction-check guard restricts clicks to the opener so
    # accidental cross-officer clicks fail loudly.
    if interaction.response.is_done():
        sent = await interaction.followup.send(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view)
        sent = await interaction.original_response()
    view.message = sent

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
        Row 0 (Premium event flow):
          📣 Post sign-up poll | 👁️ View signups + Set up teams |
          📋 Record attendance | 📜 Past rosters | 🔔 DM roster reminder
        Row 1 (Configuration + free-tier flows):
          🧮 Manage strategy presets | 👤 Manage member rules |
          📄 Generate mail | 📊 Log participation | 📜 View past log
        Row 2 (Setup):
          ⚙️ Open setup wizard

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
        parent = _PARENT_CMD[self.event_type]
        label_event = _EVENT_LABEL[self.event_type]

        # ── Row 0: Premium event flow ────────────────────────────────────
        self._add_button(
            label="💎 Post sign-up poll" if self._premium_disabled() else "📣 Post sign-up poll",
            style=discord.ButtonStyle.primary,
            disabled=self._premium_disabled(),
            row=0,
            callback=self._on_post_signup,
        )
        self._add_button(
            label="💎 View signups + Set up teams" if self._premium_disabled() else "👁️ View signups + Set up teams",
            style=discord.ButtonStyle.success,
            disabled=self._premium_disabled(),
            row=0,
            callback=self._on_view_signups,
        )
        self._add_button(
            label="💎 Record attendance" if self._premium_disabled() else "📋 Record attendance",
            style=discord.ButtonStyle.primary,
            disabled=self._premium_disabled(),
            row=0,
            callback=self._on_attendance,
        )
        self._add_button(
            label="💎 Past rosters" if self._premium_disabled() else "📜 Past rosters",
            style=discord.ButtonStyle.secondary,
            disabled=self._premium_disabled(),
            row=0,
            callback=self._on_past_rosters,
        )
        self._add_button(
            label="💎 DM roster reminder" if self._premium_disabled() else "🔔 DM roster reminder",
            style=discord.ButtonStyle.secondary,
            disabled=self._premium_disabled(),
            row=0,
            callback=self._on_remind,
        )

        # ── Row 1: Configuration + free-tier flows ───────────────────────
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
            label="📄 Generate mail (free tier)",
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_draft,
        )
        self._add_button(
            label="📊 Log participation",
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_participation,
        )
        self._add_button(
            label="📜 View past log",
            style=discord.ButtonStyle.secondary,
            disabled=False, row=1,
            callback=self._on_log,
        )

        # ── Row 2: Setup ─────────────────────────────────────────────────
        self._add_button(
            label="⚙️ Open setup wizard",
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
        # The setup wizard runs as a slash command (/setup_desertstorm)
        # not a handler we can directly invoke with an interaction,
        # because the wizard captures channel messages mid-flow and
        # needs a fresh slash interaction to start. Direct officers
        # there via an ephemeral pointer instead of trying to fake the
        # invocation.
        setup_cmd = (
            "/setup_desertstorm" if self.event_type == "DS"
            else "/setup_canyonstorm"
        )
        await inter.response.send_message(
            f"⚙️ Run `{setup_cmd}` to open the setup wizard. (The wizard "
            f"runs as its own slash command so it can capture follow-up "
            f"messages in this channel.)",
            ephemeral=True,
        )


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

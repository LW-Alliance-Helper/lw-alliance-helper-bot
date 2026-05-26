"""
setup_hub.py — the single `/setup` event-hub entry point that replaced
the 12 separate `/setup_*` slash commands (#201).

Officers run one command. The hub embed surfaces the alliance's current
foundation state (leadership role + channel + timezone + sheet) plus a
per-feature configured/unconfigured snapshot, and a button grid that
dispatches into every existing setup wizard. Premium-gated buttons render
disabled on the free tier with a click-upsell.

This module owns the embed builder and the button view; the actual
wizard handlers live in their existing modules (setup_cog.run_train_setup,
run_growth_setup, run_birthday_setup, run_storm_setup, run_event_setup,
run_survey_setup, run_shiny_tasks_setup, run_growth_breakdown_setup;
member_roster.run_member_roster_setup; setup_cog.run_setup;
setup_cog._send_view_configuration; setup_cog._reset_config). Each
button is a thin dispatcher.
"""
from __future__ import annotations

import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)


# ── Hub button label constants ───────────────────────────────────────────────
# Extracted so other modules can import these instead of duplicating the
# literal in error copy, wizard timeout messages, /help text, etc. (#208).
# Mirrors the HUB_BTN_* pattern already in storm_event_hub.py.

HUB_BTN_SETUP_WIZARD = "⚙️ Open setup wizard"
HUB_BTN_VIEW_CONFIG  = "🗂️ View configuration"
HUB_BTN_RESET        = "🗑️ Reset configuration"
HUB_BTN_RELEASE_ANN  = "📢 Release announcements"  # base; live label suffixed in _refresh_release_announcement_label
HUB_BTN_TRAIN        = "🚂 Train"
HUB_BTN_GROWTH       = "📈 Growth"
HUB_BTN_BIRTHDAYS    = "🎂 Birthdays"
HUB_BTN_EVENTS       = "📣 Events"
HUB_BTN_DS           = "⚔️ Desert Storm"
HUB_BTN_CS           = "🏜️ Canyon Storm"
HUB_BTN_SHINY        = "🌟 Shiny Tasks"
HUB_BTN_MEMBERS      = "👥 Member Sync"
HUB_BTN_SURVEY       = "📋 Survey"
HUB_BTN_BREAKDOWN    = "📊 Growth Breakdown"

STORM_SETUP_NAV = {
    "DS": f"/setup → {HUB_BTN_DS}",
    "CS": f"/setup → {HUB_BTN_CS}",
}


# ── Embed builder ────────────────────────────────────────────────────────────


def _state_dot(is_configured: bool, *, premium_locked: bool = False) -> str:
    """Compact configured/unconfigured/locked indicator for the embed body."""
    if premium_locked:
        return "💎"
    return "✅" if is_configured else "⚪"


def _build_setup_hub_embed(
    guild: discord.Guild,
    *,
    is_premium: bool,
) -> discord.Embed:
    """Build the hub embed showing the alliance's current setup state.

    Foundations come from `guild_configs`; per-feature state comes from
    each feature's own config helper. Premium-only features show 💎 on
    the free tier rather than ⚪ so officers see at a glance which
    capabilities are locked vs simply unconfigured.
    """
    import config
    from config import (
        get_config, get_train_config, get_growth_config,
        get_birthday_config, get_storm_config, get_guild_events,
        get_survey_config, get_member_roster_config,
        get_shiny_tasks_config,
    )

    cfg = get_config(guild.id)
    setup_done = bool(cfg and cfg.setup_complete)

    leadership_role = (
        f"`@{cfg.leadership_role_name}`"
        if (cfg and cfg.leadership_role_name)
        else "_not configured_"
    )
    leadership_channel = (
        f"<#{cfg.leadership_channel_id}>"
        if (cfg and cfg.leadership_channel_id)
        else "_not configured_"
    )
    timezone_str = (cfg.timezone if cfg else "") or "_not configured_"
    sheet_id     = ((cfg.spreadsheet_id if cfg else "") or "").strip()
    sheet_line   = (
        f"`{sheet_id[:24]}…`" if sheet_id else "_not configured_"
    )

    # Per-feature state. Each helper returns a dict with sensible defaults
    # for unconfigured guilds — `enabled` flag (or its absence) is the
    # canonical "is this feature on" signal.
    try:
        train_on = bool((get_train_config(guild.id) or {}).get("setup_complete"))
    except Exception:
        train_on = False
    try:
        growth_cfg = get_growth_config(guild.id) or {}
        growth_on  = bool(growth_cfg.get("enabled"))
        breakdown_post_on = bool(growth_cfg.get("breakdown_post_channel_id"))
    except Exception:
        growth_on = False
        breakdown_post_on = False
    try:
        birthdays_on = bool((get_birthday_config(guild.id) or {}).get("enabled"))
    except Exception:
        birthdays_on = False
    try:
        ds_on = bool((get_storm_config(guild.id, "DS") or {}).get("setup_complete"))
    except Exception:
        ds_on = False
    try:
        cs_on = bool((get_storm_config(guild.id, "CS") or {}).get("setup_complete"))
    except Exception:
        cs_on = False
    try:
        events_count = len(get_guild_events(guild.id, active_only=True) or [])
        events_on    = events_count > 0
    except Exception:
        events_on = False
    try:
        survey_cfg = get_survey_config(guild.id) or {}
        survey_on  = bool(survey_cfg.get("questions"))
    except Exception:
        survey_on = False
    try:
        shiny_on = bool((get_shiny_tasks_config(guild.id) or {}).get("enabled"))
    except Exception:
        shiny_on = False
    try:
        members_on = bool((get_member_roster_config(guild.id) or {}).get("enabled"))
    except Exception:
        members_on = False

    # Premium-gated features show 💎 on free tier instead of ⚪.
    def _free(state: bool) -> str:
        return _state_dot(state)

    def _premium(state: bool) -> str:
        return _state_dot(state, premium_locked=not is_premium)

    description_lines = [
        f"**Leadership role:** {leadership_role}",
        f"**Leadership channel:** {leadership_channel}",
        f"**Timezone:** {timezone_str}",
        f"**Sheet ID:** {sheet_line}",
        "",
        "**Features**",
        f"{_free(train_on)} Train schedule",
        f"{_free(growth_on)} Growth tracking",
        f"{_premium(breakdown_post_on)} Growth Breakdown auto-post",
        f"{_free(birthdays_on)} Birthdays",
        f"{_free(events_on)} Events ({events_count if events_on else 0} configured)",
        f"{_free(ds_on)} Desert Storm",
        f"{_free(cs_on)} Canyon Storm",
        f"{_free(shiny_on)} Shiny Tasks",
        f"{_premium(survey_on)} Survey",
        f"{_premium(members_on)} Member Sync",
    ]
    if not setup_done:
        description_lines.insert(
            0,
            "_The foundation wizard hasn't been run yet._ "
            "**Click ⚙️ Open setup wizard below to start.**",
        )
        description_lines.insert(1, "")
    if not is_premium:
        description_lines.append("")
        description_lines.append(
            "_Free tier — 💎 buttons below are disabled. Run `/upgrade` to unlock._"
        )

    embed = discord.Embed(
        title=f"⚙️ Setup — {guild.name}",
        description="\n".join(description_lines),
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text="Click a button to open a wizard. Re-run /setup any time to refresh this view.",
    )
    return embed


# ── Hub view ─────────────────────────────────────────────────────────────────


class _SetupHubView(discord.ui.View):
    """Hub button grid. Each button dispatches into an existing setup
    wizard handler. Premium-gated buttons render disabled on the free
    tier with the 💎 prefix.

    Layout (4 rows, 13 buttons total):
        Row 0 (foundations + utilities):
          ⚙️ Open setup wizard | 🗂️ View configuration | 🗑️ Reset configuration
        Row 1 (free-tier features):
          🚂 Train | 📈 Growth | 🎂 Birthdays | 📣 Events
        Row 2 (Premium event flow):
          ⚔️ Desert Storm | 🏜️ Canyon Storm | 🌟 Shiny Tasks
        Row 3 (Premium roster + survey + growth breakdown):
          👥 Member Sync 💎 | 📋 Survey 💎 | 📊 Growth Breakdown 💎

    Discord caps the View at 25 components; 13 fits comfortably.
    """

    def __init__(self, bot, guild_id: int, owner_user_id: int, *, is_premium: bool):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.is_premium = is_premium
        self._gate_premium_buttons()
        self._refresh_release_announcement_label()

    def _gate_premium_buttons(self) -> None:
        """Disable Premium-only buttons on free tier and prefix their
        labels with 💎. Mirrors the same idiom as storm_event_hub."""
        if self.is_premium:
            return
        for button in (self.btn_members, self.btn_survey, self.btn_growth_breakdown):
            button.disabled = True
            if not button.label.startswith("💎"):
                button.label = f"💎 {button.label}"

    def _refresh_release_announcement_label(self) -> None:
        """Reflect current opt-in state in the toggle button label so
        officers see the state at a glance without clicking. Called
        from `__init__` so each hub render picks up the latest value
        from `guild_configs`."""
        from config import get_config
        cfg = get_config(self.guild_id)
        enabled = bool(cfg.release_announcements_enabled) if cfg else True
        state = "ON" if enabled else "OFF"
        self.btn_release_announcements.label = f"📢 Release announcements: {state}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Hub buttons inherit the same admin-or-leadership gate as the
        # slash command that opened the hub. Some alliances let
        # leadership configure the bot without granting full server
        # admin, mirroring the per-feature `/setup_*` wizard gate
        # (`setup_cog._has_leadership_or_admin`).
        from setup_cog import _has_leadership_or_admin
        if not _has_leadership_or_admin(interaction):
            from config import get_config
            cfg = get_config(interaction.guild_id)
            role_name = (cfg.leadership_role_name if cfg else None) or "Leadership"
            await interaction.response.send_message(
                f"⛔ You need server administrator permission or the "
                f"**{role_name}** role to use the setup hub.",
                ephemeral=True,
            )
            return False
        return True

    # ── Row 0: foundations + utilities ────────────────────────────────────────

    @discord.ui.button(label=HUB_BTN_SETUP_WIZARD, style=discord.ButtonStyle.primary, row=0)
    async def btn_root_wizard(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import run_setup, _check_wizard_can_run
        if not await _check_wizard_can_run(inter, "setup"):
            return
        await inter.response.send_message(
            "⚙️ Starting setup — check the channel for prompts!", ephemeral=True,
        )
        await run_setup(inter, self.bot)

    @discord.ui.button(label=HUB_BTN_VIEW_CONFIG, style=discord.ButtonStyle.secondary, row=0)
    async def btn_view_config(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _send_view_configuration
        from config import get_config
        cfg = get_config(inter.guild_id)
        if not cfg or not cfg.setup_complete:
            await inter.response.send_message(
                "⚙️ This server hasn't been set up yet. Click **⚙️ Open setup wizard** above to start.",
                ephemeral=True,
            )
            return
        await _send_view_configuration(inter, cfg)

    @discord.ui.button(label=HUB_BTN_RESET, style=discord.ButtonStyle.danger, row=0)
    async def btn_reset(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _run_reset_flow
        await _run_reset_flow(inter)

    @discord.ui.button(label=HUB_BTN_RELEASE_ANN, style=discord.ButtonStyle.secondary, row=0)
    async def btn_release_announcements(self, inter: discord.Interaction, _b: discord.ui.Button):
        """One-click opt-in/opt-out for the leadership-channel release
        notification posted on each new major/minor version (#253). The
        label always reflects current state (set in
        `_refresh_release_announcement_label`); clicking flips it and
        replies ephemerally with the new state. Default is ON."""
        from config import get_config, get_or_create_config, update_config_field
        cfg = get_config(inter.guild_id) or get_or_create_config(inter.guild_id)
        currently_on = bool(cfg.release_announcements_enabled)
        new_value = 0 if currently_on else 1
        update_config_field(inter.guild_id, "release_announcements_enabled", new_value)
        if new_value:
            msg = (
                "📢 **Release announcements are now ON.** The next time a major "
                "or minor version of LW Alliance Helper deploys, you'll see a "
                "short summary embed in your leadership channel."
            )
        else:
            msg = (
                "📢 **Release announcements are now OFF.** You won't see release "
                "notifications in your leadership channel. You can turn them "
                "back on anytime by clicking this button again."
            )
        await inter.response.send_message(msg, ephemeral=True)

    # ── Row 1: free-tier features ────────────────────────────────────────────

    @discord.ui.button(label=HUB_BTN_TRAIN, style=discord.ButtonStyle.secondary, row=1)
    async def btn_train(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_train_setup
        await _launch_train_setup(inter, self.bot)

    @discord.ui.button(label=HUB_BTN_GROWTH, style=discord.ButtonStyle.secondary, row=1)
    async def btn_growth(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_growth_setup
        await _launch_growth_setup(inter, self.bot)

    @discord.ui.button(label=HUB_BTN_BIRTHDAYS, style=discord.ButtonStyle.secondary, row=1)
    async def btn_birthdays(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_birthday_setup
        await _launch_birthday_setup(inter, self.bot)

    @discord.ui.button(label=HUB_BTN_EVENTS, style=discord.ButtonStyle.secondary, row=1)
    async def btn_events(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_event_setup
        await _launch_event_setup(inter, self.bot)

    # ── Row 2: Premium event flow (Storm + Shiny Tasks) ─────────────────────

    @discord.ui.button(label=HUB_BTN_DS, style=discord.ButtonStyle.secondary, row=2)
    async def btn_desertstorm(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_storm_setup
        await _launch_storm_setup(inter, self.bot, "DS")

    @discord.ui.button(label=HUB_BTN_CS, style=discord.ButtonStyle.secondary, row=2)
    async def btn_canyonstorm(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_storm_setup
        await _launch_storm_setup(inter, self.bot, "CS")

    @discord.ui.button(label=HUB_BTN_SHINY, style=discord.ButtonStyle.secondary, row=2)
    async def btn_shiny_tasks(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_shiny_tasks_setup
        await _launch_shiny_tasks_setup(inter, self.bot)

    # ── Row 3: Premium-gated (Member Sync + Survey + Growth Breakdown) ──────

    @discord.ui.button(label=HUB_BTN_MEMBERS, style=discord.ButtonStyle.secondary, row=3)
    async def btn_members(self, inter: discord.Interaction, _b: discord.ui.Button):
        from member_roster import _launch_member_roster_setup
        await _launch_member_roster_setup(inter, self.bot)

    @discord.ui.button(label=HUB_BTN_SURVEY, style=discord.ButtonStyle.secondary, row=3)
    async def btn_survey(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_survey_setup
        await _launch_survey_setup(inter, self.bot)

    @discord.ui.button(label=HUB_BTN_BREAKDOWN, style=discord.ButtonStyle.secondary, row=3)
    async def btn_growth_breakdown(self, inter: discord.Interaction, _b: discord.ui.Button):
        from setup_cog import _launch_growth_breakdown_setup
        await _launch_growth_breakdown_setup(inter, self.bot)


# ── Slash-command entry point ────────────────────────────────────────────────


async def handle_setup_hub(bot, interaction: discord.Interaction) -> None:
    """Admin-or-leadership entry point invoked by /setup. Reads premium
    status via `premium.is_premium` and caches it on the view so
    per-button callbacks don't re-check.

    Server administrators or members with the configured leadership
    role can run `/setup`. On a fresh install (no saved config yet)
    only admins pass the gate — they configure the leadership role
    during the wizard, after which their officers can re-open setup
    without needing admin permissions.
    """
    from setup_cog import _has_leadership_or_admin
    if not _has_leadership_or_admin(interaction):
        from config import get_config
        cfg = get_config(interaction.guild_id)
        role_name = (cfg.leadership_role_name if cfg else None) or "Leadership"
        await interaction.response.send_message(
            f"⛔ You need server administrator permission or the "
            f"**{role_name}** role to run `/setup`.",
            ephemeral=True,
        )
        return

    import premium
    is_premium = await premium.is_premium(
        interaction.guild_id, interaction=interaction, bot=bot,
    )

    embed = _build_setup_hub_embed(interaction.guild, is_premium=is_premium)
    view  = _SetupHubView(
        bot, interaction.guild_id, interaction.user.id,
        is_premium=is_premium,
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

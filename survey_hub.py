"""
survey_hub.py — the single `/survey` hub that replaced the
`/survey overview | post | remind` subcommand group.

One command, one embed listing every configured survey (default plus any
Premium extras), and a button grid covering both the config actions
(Add / Edit / Remove / Survey Translation) and the operational ones
(Post / Reminders). Same shape as `/train`, `/events`, and the storm hubs.

Two doors open this hub: the `/survey` command itself and the `/setup`
hub's 📋 Survey button. Both render the identical surface, so there's one
Survey screen instead of two that drift apart.

Premium gating is per-button, not per-hub: extras are Premium
(`multiple_surveys`), so ➕ Add and 🗑️ Remove render disabled with a 💎
prefix on the free tier, while editing the default survey, posting it,
reminders, and translation stay free. That per-button split is what makes
the free tier able to configure its one survey at all — the whole hub used
to sit behind a Premium-disabled button.

The actual flows live in their existing modules: `survey.run_post_survey`,
`survey._run_remind_hub`, `survey.run_translation_helper_setup`,
`setup_cog.run_create_new_extra_survey`, `setup_cog.run_pick_survey_to_edit`,
`setup_cog.run_remove_extra_survey`, `setup_cog.run_survey_setup`. Each
button is a thin dispatcher.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)

SURVEY_HUB_CMD = "/survey"

# ── Hub button label constants ───────────────────────────────────────────────
# Imported by /help copy, the /setup hub, and tests rather than duplicating
# the literals (#208 pattern).

SURVEY_HUB_BTN_ADD = "➕ Add Survey"
SURVEY_HUB_BTN_EDIT = "✏️ Edit Survey"
# Shown in place of Edit when nothing is configured yet, so a fresh alliance
# isn't told to "edit" a survey that doesn't exist.
SURVEY_HUB_BTN_SETUP = "⚙️ Set Up Survey"
SURVEY_HUB_BTN_REMOVE = "🗑️ Remove Survey"
SURVEY_HUB_BTN_POST = "📮 Post Survey"
SURVEY_HUB_BTN_REMIND = "🔔 Reminders"
SURVEY_HUB_BTN_TRANSLATE = "🌐 Survey Translation"

_DENY_NOT_OWNER = "⛔ Only the person who opened this hub can use these buttons."


# ── Embed ─────────────────────────────────────────────────────────────────────


def _build_survey_hub_embed(
    guild: discord.Guild,
    surveys: list[dict],
    *,
    is_premium: bool,
    translate_bot_id: int,
) -> discord.Embed:
    """
    List every configured survey, or say plainly that there are none.

    Renders the same whether the alliance has 0, 1, or 10 surveys, so the
    hub never changes shape underneath you.
    """
    embed = discord.Embed(title="📋 Surveys", color=discord.Color.blurple())

    configured = [s for s in surveys if (s.get("questions") or [])]
    if not configured:
        embed.description = (
            "*No survey configured yet.*\n\n"
            f"Click **{SURVEY_HUB_BTN_SETUP}** to pick the channels, sheet tabs, "
            "and questions your members will answer."
        )
    else:
        for s in surveys[:25]:
            sid = s.get("survey_id") or "default"
            name = s.get("survey_name") or sid
            n_q = len(s.get("questions") or [])
            tab = s.get("tab_squad_powers") or "*not set*"
            ch_id = int(s.get("survey_channel_id") or 0)
            ch_str = f"<#{ch_id}>" if ch_id else "_(uses default channel)_"
            embed.add_field(
                name=f"{name}" + (" *(default)*" if sid == "default" else ""),
                value=f"**{n_q}** question(s) · Stats tab: `{tab}` · Channel: {ch_str}",
                inline=False,
            )

    if not is_premium:
        embed.add_field(
            name="💎 Premium",
            value=(
                "Additional named surveys, unlimited questions, and DM reminders "
                "are Premium. Your default survey is free."
            ),
            inline=False,
        )

    if translate_bot_id:
        helper = guild.get_member(translate_bot_id) if guild else None
        embed.add_field(
            name="🌐 Translation",
            value=(
                f"{helper.mention} is added to every survey thread."
                if helper
                else f"⚠️ Configured bot (`{translate_bot_id}`) is no longer in this server."
            ),
            inline=False,
        )

    return embed


# ── View ──────────────────────────────────────────────────────────────────────


class _SurveyHubView(discord.ui.View):
    """Hub button grid. Config actions on row 0, operational ones on row 1."""

    def __init__(
        self,
        bot,
        guild_id: int,
        owner_user_id: int,
        *,
        is_premium: bool,
        has_extras: bool,
        has_default: bool,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.is_premium = is_premium
        self.has_extras = has_extras
        self.has_default = has_default
        self.message: Optional[discord.Message] = None
        self._build_buttons()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=SURVEY_HUB_CMD)

    def _add(self, label, style, row, cb, *, disabled=False):
        btn = discord.ui.Button(label=label[:80], style=style, row=row, disabled=disabled)
        btn.callback = cb
        self.add_item(btn)

    def _build_buttons(self):
        # Row 0 — configuration. Add and Remove touch extra surveys, which
        # are Premium; editing the default survey never is.
        add_label = SURVEY_HUB_BTN_ADD if self.is_premium else f"💎 {SURVEY_HUB_BTN_ADD}"
        self._add(
            add_label,
            discord.ButtonStyle.success,
            0,
            self._on_add,
            disabled=not self.is_premium,
        )

        edit_label = SURVEY_HUB_BTN_EDIT if self.has_default else SURVEY_HUB_BTN_SETUP
        self._add(edit_label, discord.ButtonStyle.primary, 0, self._on_edit)

        # Remove only ever targets extras, so it stays off with none to remove.
        remove_label = SURVEY_HUB_BTN_REMOVE if self.is_premium else f"💎 {SURVEY_HUB_BTN_REMOVE}"
        self._add(
            remove_label,
            discord.ButtonStyle.danger,
            0,
            self._on_remove,
            disabled=not (self.is_premium and self.has_extras),
        )

        # Row 1 — running the survey. All free; the DM destination inside
        # Reminders does its own Premium check.
        self._add(
            SURVEY_HUB_BTN_POST,
            discord.ButtonStyle.secondary,
            1,
            self._on_post,
            disabled=not self.has_default,
        )
        self._add(
            SURVEY_HUB_BTN_REMIND,
            discord.ButtonStyle.secondary,
            1,
            self._on_remind,
            disabled=not self.has_default,
        )
        self._add(SURVEY_HUB_BTN_TRANSLATE, discord.ButtonStyle.secondary, 1, self._on_translate)

    async def _close(self, inter: discord.Interaction):
        """Disable the grid before dispatching, so a slow wizard can't be
        double-launched by an impatient second click."""
        import wizard_registry

        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    # ── Row 0 dispatchers ────────────────────────────────────────────────────

    async def _on_add(self, inter: discord.Interaction):
        from setup_cog import _check_wizard_can_run, run_create_new_extra_survey

        await self._close(inter)
        # The wizards below talk in-channel via `channel.send`, so keep the
        # missing-permissions explainer the old `/setup` door applied. Without
        # it a click in a channel the bot can't post in just hangs.
        if not await _check_wizard_can_run(inter, "survey"):
            return
        await run_create_new_extra_survey(inter, self.bot)

    async def _on_edit(self, inter: discord.Interaction):
        # One survey means nothing to pick — go straight into the wizard.
        # The picker only earns its click when extras exist.
        from setup_cog import _check_wizard_can_run, run_pick_survey_to_edit, run_survey_setup

        await self._close(inter)
        if not await _check_wizard_can_run(inter, "survey"):
            return
        if self.has_extras:
            await run_pick_survey_to_edit(inter, self.bot)
        else:
            await run_survey_setup(inter, self.bot)

    async def _on_remove(self, inter: discord.Interaction):
        from setup_cog import run_remove_extra_survey

        await self._close(inter)
        await run_remove_extra_survey(inter, self.bot)

    # ── Row 1 dispatchers ────────────────────────────────────────────────────

    async def _on_post(self, inter: discord.Interaction):
        from survey import run_post_survey

        await self._close(inter)
        await run_post_survey(inter, self.bot)

    async def _on_remind(self, inter: discord.Interaction):
        from survey import _run_remind_hub

        await self._close(inter)
        await _run_remind_hub(inter, self.bot)

    async def _on_translate(self, inter: discord.Interaction):
        from survey import run_translation_helper_setup

        # Doesn't disable the grid: the translation picker replies with its
        # own ephemeral, so leaving the hub live lets an officer set the
        # helper and then immediately post or edit.
        await run_translation_helper_setup(inter)


# ── Entry point ───────────────────────────────────────────────────────────────


async def handle_survey_hub(bot, interaction: discord.Interaction) -> None:
    """
    Render the survey hub. Called by `/survey` and by the `/setup` hub's
    📋 Survey button; each caller applies its own permission gate first
    (`survey._guard` and `setup_cog._has_leadership_or_admin` respectively),
    so this function makes no authorisation decision of its own.
    """
    from config import get_config, list_surveys
    import premium as _prem

    is_premium_flag = await _prem.is_premium(
        interaction.guild_id,
        interaction=interaction,
        bot=bot,
    )

    surveys = list_surveys(interaction.guild_id)
    has_extras = any((s.get("survey_id") or "default") != "default" for s in surveys)
    has_default = any(
        (s.get("survey_id") or "default") == "default" and (s.get("questions") or [])
        for s in surveys
    )

    cfg = get_config(interaction.guild_id)
    translate_bot_id = int((cfg.survey_translate_bot_id if cfg else 0) or 0)

    embed = _build_survey_hub_embed(
        interaction.guild,
        surveys,
        is_premium=is_premium_flag,
        translate_bot_id=translate_bot_id,
    )
    view = _SurveyHubView(
        bot,
        interaction.guild_id,
        interaction.user.id,
        is_premium=is_premium_flag,
        has_extras=has_extras,
        has_default=has_default,
    )
    # Both doors pass a fresh interaction (slash command / button click), so
    # respond directly rather than deferring — same idiom as the train and
    # setup hubs. `message` is captured for the on_timeout cleanup.
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()

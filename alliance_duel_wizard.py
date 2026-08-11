"""Alliance Duel (VS) setup wizard (#399 / #448).

The interactive half of VS setup: ask which alliance is yours, ask which
shape they want to track, create the tab, and show the column guide.

`alliance_duel_setup.py` owns the embeds this renders, so everything here is
flow control. Two steps only, in the order the user thinks rather than the
order the schema stores: *who are you* then *what do you want to track*.

The mode question is step two on purpose. It is the one thing that cannot be
inferred (skeleton generation is a write, so the shape has to be known before
there is data to infer it from), and asking it after the alliance identity
means the upsell lands on someone who has already committed to setting this
up rather than on a cold open.
"""

from __future__ import annotations

import logging

import discord

import alliance_duel as ad
import alliance_duel_setup as ads
import config
from messages import CANCEL_BACKPEDAL_DEFAULT
from setup_hub import HUB_BTN_VS
from wizard_registry import expire_view_message, safe_edit_response

logger = logging.getLogger(__name__)

#: A wizard step involving typing or thought, per the DESIGN.md timeout tiers.
STEP_TIMEOUT = 300


class OwnAllianceModal(discord.ui.Modal, title="Your alliance"):
    """Step 1: which of the sixteen rows is yours.

    Stored once in config rather than as a repeated sheet column. Your
    alliance is otherwise just another row; this is what tells the bot which.
    """

    tag = discord.ui.TextInput(
        label="Alliance tag",
        placeholder="ABC",
        max_length=10,
        required=True,
    )
    warzone = discord.ui.TextInput(
        label="Warzone",
        placeholder="1234",
        max_length=10,
        required=True,
    )

    def __init__(self, parent: "VSSetupView") -> None:
        super().__init__()
        self._parent = parent
        current = parent.cfg
        if current.get("own_tag"):
            self.tag.default = current["own_tag"]
        if current.get("own_warzone"):
            self.warzone.default = current["own_warzone"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defer before any sheet or DB round-trip, per the 1.1.7 rule: a slow
        # call otherwise expires the 3-second initial-response token.
        await interaction.response.defer(ephemeral=True)

        key = ad.AllianceKey.of(str(self.tag), str(self.warzone))
        if key is None:
            await interaction.followup.send(
                "⚠️ I need both an alliance tag and a warzone. "
                f"Run `/setup` and click **{HUB_BTN_VS}** to start again.",
                ephemeral=True,
            )
            return

        config.save_vs_config(
            self._parent.guild_id,
            own_tag=str(self.tag).strip(),
            own_warzone=str(self.warzone).strip(),
        )
        self._parent.cfg = config.get_vs_config(self._parent.guild_id)
        await self._parent.show_mode_step(interaction)


class TrackingModeView(discord.ui.View):
    """Step 2: own alliance or the full bracket.

    Neither button is `primary`. The bracket is the option that unlocks more,
    but presenting it as recommended would be the bot leaning on a choice the
    design says belongs to the alliance, and own-alliance is a supported shape
    rather than a lesser one.
    """

    def __init__(self, parent: "VSSetupView") -> None:
        super().__init__(timeout=STEP_TIMEOUT)
        self._parent = parent
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        await expire_view_message(self.message, command_hint=ads.VS_SETUP_NAV)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._parent.owns(interaction)

    @discord.ui.button(label=ads.MODE_BTN_OWN, style=discord.ButtonStyle.secondary)
    async def btn_own(self, inter: discord.Interaction, _b: discord.ui.Button):
        await self._parent.finish(inter, ad.MODE_OWN_ALLIANCE)

    @discord.ui.button(label=ads.MODE_BTN_FULL, style=discord.ButtonStyle.secondary)
    async def btn_full(self, inter: discord.Interaction, _b: discord.ui.Button):
        await self._parent.finish(inter, ad.MODE_FULL_BRACKET)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def btn_cancel(self, inter: discord.Interaction, _b: discord.ui.Button):
        # Backpedal, not plain: the alliance identity saved in step 1 survives,
        # and telling them "Cancelled." flat would imply it did not.
        self.stop()
        await safe_edit_response(
            inter,
            content=(
                f"{CANCEL_BACKPEDAL_DEFAULT} Your alliance is still saved. "
                f"Run `/setup` and click **{HUB_BTN_VS}** to pick a tracking mode."
            ),
            embed=None,
            view=None,
        )


class VSSetupView(discord.ui.View):
    """Holds the wizard's state across its two steps."""

    def __init__(self, guild_id: int, owner_user_id: int) -> None:
        super().__init__(timeout=STEP_TIMEOUT)
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.cfg = config.get_vs_config(guild_id)
        self.message: discord.Message | None = None

    async def owns(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        from messages import DENY_NOT_OWNER

        await interaction.response.send_message(DENY_NOT_OWNER, ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        await expire_view_message(self.message, command_hint=ads.VS_SETUP_NAV)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.owns(interaction)

    @discord.ui.button(label="🏆 Set up Alliance Duel (VS)", style=discord.ButtonStyle.primary)
    async def btn_start(self, inter: discord.Interaction, _b: discord.ui.Button):
        await inter.response.send_modal(OwnAllianceModal(self))

    async def show_mode_step(self, interaction: discord.Interaction) -> None:
        view = TrackingModeView(self)
        await interaction.followup.send(embed=ads.tracking_mode_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def finish(self, interaction: discord.Interaction, tracking_mode: str) -> None:
        """Save the mode, create the tab, and show the column guide."""
        await interaction.response.defer(ephemeral=True)
        was = self.cfg.get("tracking_mode")
        config.save_vs_config(self.guild_id, tracking_mode=tracking_mode, enabled=1)
        self.cfg = config.get_vs_config(self.guild_id)

        tab_name = self.cfg.get("tab_name") or "Alliance Duel (VS)"
        created = await _create_tab(self.guild_id, tab_name)
        if not created:
            await interaction.followup.send(
                "⚠️ I saved your settings, but could not reach your sheet to "
                f"create the **{tab_name}** tab. Check the bot still has access, "
                f"then run `/setup` and click **{HUB_BTN_VS}** to start again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            content=(
                f"✅ Set up Alliance Duel (VS), tracking {ads.mode_label(tracking_mode)}. "
                f"Your **{tab_name}** tab is ready to fill in."
            ),
            embed=ads.column_guide_embed(tracking_mode),
            ephemeral=True,
        )

        # Widening mid-league leaves the sheet short of a full bracket. Offer
        # the rows rather than leaving fifteen per week to be typed by hand
        # (#448). Only on the widening direction: narrowing needs no rows, and
        # nothing is ever deleted.
        if was == ad.MODE_OWN_ALLIANCE and tracking_mode == ad.MODE_FULL_BRACKET:
            await self._offer_missing_rows(interaction, tab_name)

    async def _offer_missing_rows(self, interaction: discord.Interaction, tab_name: str) -> None:
        import asyncio

        rows = await asyncio.to_thread(ads.load_rows, self.guild_id, tab_name)
        if not rows:
            return  # nothing recorded yet, or the sheet is unreachable and already reported
        league = ad.latest_league(rows)
        if league is None:
            return
        missing = ad.missing_bracket_rows(rows, league)
        if not missing:
            return

        view = FillBracketView(self, league, missing, tab_name)
        await interaction.followup.send(
            embed=ads.fill_bracket_embed(league, missing), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()


class FillBracketView(discord.ui.View):
    """Offers the blank rows, and never writes without being asked.

    Declining is a real answer: an alliance may want the fuller views without
    backfilling a league already half over.
    """

    def __init__(self, parent: "VSSetupView", league, missing, tab_name: str) -> None:
        super().__init__(timeout=STEP_TIMEOUT)
        self._parent = parent
        self._league = league
        self._missing = missing
        self._tab = tab_name
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        await expire_view_message(self.message, command_hint=ads.VS_SETUP_NAV)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._parent.owns(interaction)

    @discord.ui.button(label="Add the rows", style=discord.ButtonStyle.primary)
    async def btn_add(self, inter: discord.Interaction, _b: discord.ui.Button):
        await inter.response.defer(ephemeral=True)
        self.stop()
        added = await _append_blank_rows(
            self._parent.guild_id, self._tab, self._league, self._missing
        )
        if added is None:
            await safe_edit_response(
                inter,
                content=(
                    "⚠️ Could not reach your sheet to add the rows. Your tracking "
                    f"mode is saved. Run `/setup` and click **{HUB_BTN_VS}** to try "
                    "the rows again."
                ),
                embed=None,
                view=None,
            )
            return
        await safe_edit_response(
            inter,
            content=(
                f"✅ Added {added} blank row{'s' if added != 1 else ''} to your "
                f"**{self._tab}** tab. Fill in the tag, warzone and seed for each "
                "from the in-game bracket screen."
            ),
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Not now", style=discord.ButtonStyle.secondary)
    async def btn_skip(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.stop()
        await safe_edit_response(
            inter,
            content=(
                "Left your sheet as it is. You can add the rows yourself, or run "
                f"`/setup` and click **{HUB_BTN_VS}** to be offered them again."
            ),
            embed=None,
            view=None,
        )


async def _append_blank_rows(guild_id: int, tab_name: str, league, missing) -> int | None:
    """Append the placeholder rows. Returns how many, or None if unreachable.

    Appends rather than upserting: a blank row has no Tag or Warzone, so it has
    no key to upsert on. Once the user fills identity in, later writes locate
    it normally.
    """
    import asyncio

    def _work():
        import config_health

        spreadsheet = config.get_spreadsheet(guild_id)
        worksheet = ads.ensure_tab(spreadsheet, tab_name)
        header = worksheet.row_values(1) or list(ad.SHEET_COLUMNS)
        lines = []
        for week, (count, stamp) in sorted(missing.items()):
            for _ in range(count):
                lines.append(ad.blank_bracket_values(header, league, week, stamp))
        if lines:
            worksheet.append_rows(lines, value_input_option="USER_ENTERED")
        config_health.clear(guild_id, ads.VS_SHEET_SUBJECT)
        return len(lines)

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001 - classified by config_health
        import config_health

        config_health.record_sheet_failure(guild_id, ads.VS_SHEET_SUBJECT, e, tab=tab_name)
        logger.warning(
            "[VS] could not append bracket rows for guild=%s: %s",
            guild_id,
            config.describe_sheet_error(e, guild_id=guild_id, tab=tab_name),
        )
        return None


async def _create_tab(guild_id: int, tab_name: str) -> bool:
    """Create the tab off the event loop. Returns False if the sheet is
    unreachable, which `load_rows` will already have reported through
    `config_health`."""
    import asyncio

    def _work():
        spreadsheet = config.get_spreadsheet(guild_id)
        ads.ensure_tab(spreadsheet, tab_name)
        return True

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001 - classified by config_health
        import config_health

        config_health.record_sheet_failure(guild_id, ads.VS_SHEET_SUBJECT, e, tab=tab_name)
        logger.warning(
            "[VS] could not create tab for guild=%s: %s",
            guild_id,
            config.describe_sheet_error(e, guild_id=guild_id, tab=tab_name),
        )
        return False


async def run_vs_setup(interaction: discord.Interaction, bot=None) -> None:
    """Entry point from the `/setup` hub.

    Re-entry shows what is already configured rather than starting blank, so
    an officer who did not set this up can see the current state before
    changing it.
    """
    guild_id = interaction.guild_id
    cfg = config.get_vs_config(guild_id)

    if cfg.get("enabled"):
        described = (
            f"Tracking {ads.mode_label(cfg['tracking_mode'])} for "
            f"**[{cfg['own_tag'].upper()}] {cfg['own_warzone']}**."
        )
    else:
        described = "Not set up yet."

    embed = discord.Embed(
        title="🏆 Alliance Duel (VS)",
        description=(
            f"{described}\n\n"
            "Record your VS league in your sheet, and the bot reads it back "
            "as your bracket, your projected path, and your history against "
            "the alliances you have faced."
        ),
        color=discord.Color.blurple(),
    )
    view = VSSetupView(guild_id, interaction.user.id)
    if cfg.get("enabled"):
        view.btn_start.label = "✏️ Change alliance or mode"
        view.btn_start.style = discord.ButtonStyle.secondary

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()


__all__ = ["run_vs_setup", "VSSetupView", "TrackingModeView", "OwnAllianceModal"]

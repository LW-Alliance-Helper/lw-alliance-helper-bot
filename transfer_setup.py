"""Transfer Management (#16) — the Setup Transfers wizard.

Reachable from both the `/setup` hub (🔁 Transfers button) and the
`/transfers` hub (⚙️ Setup Transfers). Premium-only. Lives in its own
module rather than bloating ``setup_cog.py`` (mirrors
``member_roster.run_member_roster_setup``); reuses the shared wizard
helpers (`ask_proceed_with_existing_config`, cancel registry, premium gate).

Build status: this slice covers the entry + Step 1 (the transfer sheet) +
Step 2 (column mapping with auto-map review). The notification channel /
style / filter, optional sources, write-back, and templates steps — plus
the silent baseline read that enables the watcher — are layered on next.
The wizard saves the sheet + column map incrementally, so a partial run
leaves a coherent (still-disabled) config.
"""

from __future__ import annotations

import asyncio
import json
import logging

import discord

import config
import premium
import transfer
import transfer_sheets
import wizard_registry

logger = logging.getLogger(__name__)

_STEP_TIMEOUT = 300
_DENY_NOT_OWNER = "⛔ Only the person who started setup can use these controls."
_TIMEOUT_MSG = "⏰ Timed out. Run `/transfers` → **⚙️ Setup Transfers** to start again."


# ── Entry point (premium gate) ────────────────────────────────────────────────


async def _launch_transfer_setup(interaction: discord.Interaction, bot) -> None:
    """Leadership + premium gate, then hand off to the wizard. Called by the
    `/setup` hub button and the `/transfers` hub Setup button."""
    from setup_cog import _has_leadership_or_admin, _check_wizard_can_run

    if not _has_leadership_or_admin(interaction):
        await interaction.response.send_message(
            "⛔ You need the leadership role (or admin) to configure Transfer Management.",
            ephemeral=True,
        )
        return

    if not await premium.feature_gate(
        "transfers", interaction.guild_id, interaction=interaction, bot=bot
    ):
        await interaction.response.send_message(
            embed=premium.premium_locked_embed(
                feature_label="Transfer Management",
                description=(
                    "Transfer Management watches your recruiting sheet, pings you on new "
                    "applicants and status changes, pulls matching players from a server-wide "
                    "sheet, and drafts your in-game messages. It's part of LW Alliance Helper "
                    "Premium — run `/upgrade` to unlock it."
                ),
            ),
            view=premium.upgrade_view(),
            ephemeral=True,
        )
        return

    if not await _check_wizard_can_run(interaction, "setup"):
        return

    await interaction.response.send_message(
        "⚙️ Starting Transfer Management setup — check the channel for prompts.",
        ephemeral=True,
    )
    await run_transfer_setup(interaction, bot)


# ── Step 1: the transfer sheet ────────────────────────────────────────────────


class _SheetModal(discord.ui.Modal, title="Transfer sheet"):
    """Collects the Google Sheet ID + tab name."""

    def __init__(self, *, default_id: str, default_tab: str, on_submit):
        super().__init__()
        self._on_submit = on_submit
        self.sheet_id = discord.ui.TextInput(
            label="Google Sheet ID (from the sheet's URL)",
            default=default_id or None,
            placeholder="1AbC...long-id...xyz",
            required=True,
            max_length=120,
        )
        self.tab = discord.ui.TextInput(
            label="Tab name",
            default=default_tab or None,
            placeholder="e.g. Applicants",
            required=True,
            max_length=100,
        )
        self.add_item(self.sheet_id)
        self.add_item(self.tab)

    async def on_submit(self, interaction: discord.Interaction):
        await self._on_submit(interaction, self.sheet_id.value.strip(), self.tab.value.strip())


class _SheetStepView(discord.ui.View):
    """Enter (or keep) the transfer sheet, verifying read access before
    advancing. On success ``result`` is ``(sheet_id, tab, header, rows)``."""

    def __init__(self, *, owner_id: int, default_id: str, default_tab: str):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.default_id = default_id
        self.default_tab = default_tab
        self.message: discord.Message | None = None
        self.result = None
        self.confirmed = False

        enter = discord.ui.Button(label="📝 Enter sheet", style=discord.ButtonStyle.primary)
        enter.callback = self._enter
        self.add_item(enter)
        if default_id and default_tab:
            keep = discord.ui.Button(
                label="✅ Keep current sheet", style=discord.ButtonStyle.secondary
            )
            keep.callback = self._keep
            self.add_item(keep)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _enter(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _SheetModal(
                default_id=self.default_id, default_tab=self.default_tab, on_submit=self._verify
            )
        )

    async def _keep(self, interaction: discord.Interaction):
        await self._verify(interaction, self.default_id, self.default_tab)

    async def _verify(self, interaction: discord.Interaction, sheet_id: str, tab: str):
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            header, rows = await asyncio.to_thread(transfer_sheets.read_sheet, sheet_id, tab)
        except Exception as e:  # noqa: BLE001 — surface any gspread failure as friendly text
            detail = config.describe_sheet_error(e)
            await interaction.followup.send(
                f"⚠️ Couldn't read that sheet/tab: {detail}\n"
                "Check the Sheet ID and tab name, and that the bot's service account has "
                "access to the sheet, then try again.",
                ephemeral=True,
            )
            return
        if not header:
            await interaction.followup.send(
                "⚠️ That tab has no header row. Put your column headers in row 1, then try again.",
                ephemeral=True,
            )
            return

        self.result = (sheet_id, tab, header, rows)
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(
                    content=(
                        f"✅ Read **{tab}** — {len(header)} columns, {len(rows)} rows. "
                        "Mapping below."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass
        await interaction.followup.send("✅ Got it — continuing below.", ephemeral=True)
        self.stop()


# ── Step 2: column mapping (four category pickers) ────────────────────────────


class _ColumnMapView(discord.ui.View):
    """Name (required) + Status / Display / Also-identity multi-selects, all
    seeded from the auto-map guess. ``saved`` + the ``sel_*`` attrs carry the
    result. Only the first 25 headers are pickable (Discord's select cap)."""

    def __init__(self, *, owner_id: int, headers: list, initial_map: dict):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.headers = headers[:25]
        self.truncated = len(headers) > 25
        self.message: discord.Message | None = None
        self.saved = False
        self.sel_name = initial_map.get("name") or None
        self.sel_status = list(initial_map.get("status") or [])
        self.sel_display = list(initial_map.get("display") or [])
        self.sel_identity = list(initial_map.get("identity_extra") or [])

        name_sel = discord.ui.Select(
            placeholder="Name column (required)",
            min_values=1,
            max_values=1,
            row=0,
            options=[
                discord.SelectOption(label=h[:100], value=h, default=(h == self.sel_name))
                for h in self.headers
            ],
        )
        name_sel.callback = self._on_name
        self.add_item(name_sel)

        self._add_multi("Status columns to watch (optional)", 1, self.sel_status, self._on_status)
        self._add_multi(
            "Columns to show in notices (optional)", 2, self.sel_display, self._on_display
        )
        self._add_multi(
            "Also-identity columns, e.g. Server (optional)", 3, self.sel_identity, self._on_identity
        )

        save = discord.ui.Button(label="✅ Save mapping", style=discord.ButtonStyle.success, row=4)
        save.callback = self._on_save
        self.add_item(save)

    def _add_multi(self, placeholder: str, row: int, selected: list, callback):
        sel = discord.ui.Select(
            placeholder=placeholder,
            min_values=0,
            max_values=len(self.headers),
            row=row,
            options=[
                discord.SelectOption(label=h[:100], value=h, default=(h in selected))
                for h in self.headers
            ],
        )
        sel.callback = callback
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    def _ordered(self, chosen) -> list:
        """Keep multi-select results in sheet order (predictable, stable)."""
        picked = set(chosen)
        return [h for h in self.headers if h in picked]

    async def _on_name(self, interaction: discord.Interaction):
        self.sel_name = interaction.data["values"][0]
        await interaction.response.defer()

    async def _on_status(self, interaction: discord.Interaction):
        self.sel_status = self._ordered(interaction.data["values"])
        await interaction.response.defer()

    async def _on_display(self, interaction: discord.Interaction):
        self.sel_display = self._ordered(interaction.data["values"])
        await interaction.response.defer()

    async def _on_identity(self, interaction: discord.Interaction):
        self.sel_identity = self._ordered(interaction.data["values"])
        await interaction.response.defer()

    def column_map(self) -> dict:
        out: dict = {"name": self.sel_name}
        if self.sel_identity:
            out["identity_extra"] = self.sel_identity
        if self.sel_status:
            out["status"] = self.sel_status
        if self.sel_display:
            out["display"] = self.sel_display
        return out

    async def _on_save(self, interaction: discord.Interaction):
        if not self.sel_name:
            await interaction.response.send_message(
                "⚠️ Pick a **Name** column — it's the one required field.", ephemeral=True
            )
            return
        self.saved = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


def _map_embed(column_map: dict, truncated: bool) -> discord.Embed:
    embed = discord.Embed(
        title="🔁 Step 2 — Map your columns",
        color=discord.Color.blurple(),
        description=(
            "I read your sheet and pre-filled my best guess below. Adjust the dropdowns, "
            "then **Save mapping**.\n\n"
            "• **Name** is the only required column (it identifies each applicant).\n"
            "• **Status** columns are watched — a change posts a status-change notice.\n"
            "• **Display** columns show in your notifications (pick as many as you like).\n"
            "• **Also-identity** columns (like Server) tell apart two people with the same name.\n\n"
            f"My guess:\n{transfer.summarize_column_map(column_map)}"
        ),
    )
    if truncated:
        embed.set_footer(text="Only the first 25 columns are pickable here.")
    return embed


# ── Wizard ────────────────────────────────────────────────────────────────────


async def run_transfer_setup(interaction: discord.Interaction, bot):
    """Walk leadership through the transfer sheet + column mapping."""
    from setup_cog import ask_proceed_with_existing_config

    guild_id = interaction.guild_id
    channel = interaction.channel
    user = interaction.user
    cancel_event = wizard_registry.register(user.id)
    try:
        current = config.get_transfer_config(guild_id)
        already = config.has_transfer_config(guild_id)
        saved_map = (
            transfer.parse_column_map(current.get("alliance_column_map_json")) if already else {}
        )

        if already and (current.get("alliance_sheet_id") or saved_map):
            sid = current.get("alliance_sheet_id") or ""
            fields = [
                (
                    "Transfer sheet",
                    f"`{sid[:18]}…` · tab **{current.get('alliance_sheet_tab') or '*not set*'}**"
                    if sid
                    else "*not set*",
                ),
                ("Columns", transfer.summarize_column_map(saved_map)),
            ]
            proceed = await ask_proceed_with_existing_config(
                channel,
                title="💎 Transfer Management — current setup",
                description="Transfer Management is already set up. Edit the sheet + column mapping?",
                fields=fields,
                cancel_event=cancel_event,
                no_changes_message="✅ No changes made. Your transfer setup is unchanged.",
            )
            if proceed is not True:
                return

        await channel.send(
            "💎 **Transfer Management setup**\n"
            "Point the bot at your recruiting sheet and it'll watch for new applicants and "
            "status changes. We'll start with the sheet and how to read it; notifications, "
            "sources, and templates come after."
        )

        # ── Step 1: the transfer sheet ────────────────────────────────────────
        sheet_view = _SheetStepView(
            owner_id=user.id,
            default_id=current.get("alliance_sheet_id") or "",
            default_tab=current.get("alliance_sheet_tab") or "",
        )
        sheet_view.message = await channel.send(
            "**Step 1 — Your transfer sheet**\n"
            "Click **Enter sheet** and paste the Google Sheet ID + the tab your applicants "
            "live on. The bot's service account needs at least read access.",
            view=sheet_view,
        )
        await wizard_registry.wait_view_or_cancel(sheet_view, cancel_event)
        if sheet_view.cancelled:
            return
        if not sheet_view.confirmed or not sheet_view.result:
            await channel.send(_TIMEOUT_MSG)
            return
        sheet_id, tab, header, _rows = sheet_view.result

        # ── Step 2: column mapping ────────────────────────────────────────────
        initial_map = saved_map or transfer.suggest_column_map(header)
        map_view = _ColumnMapView(owner_id=user.id, headers=header, initial_map=initial_map)
        map_view.message = await channel.send(
            embed=_map_embed(initial_map, len(header) > 25), view=map_view
        )
        await wizard_registry.wait_view_or_cancel(map_view, cancel_event)
        if map_view.cancelled:
            return
        if not map_view.saved:
            await channel.send(_TIMEOUT_MSG)
            return

        column_map = map_view.column_map()
        config.update_transfer_config_fields(
            guild_id,
            alliance_sheet_id=sheet_id,
            alliance_sheet_tab=tab,
            alliance_column_map_json=json.dumps(column_map),
        )
        await channel.send(
            "✅ **Saved your sheet and column mapping.**\n\n"
            f"{transfer.summarize_column_map(column_map)}\n\n"
            "*Next up (coming in the next build step): notification channel + style, the "
            "new-applicant filter, optional server-wide / form sources, decision write-back, "
            "and your message templates — then the watcher goes live.*"
        )
    finally:
        wizard_registry.unregister(user.id, cancel_event)

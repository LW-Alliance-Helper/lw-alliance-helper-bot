"""Transfer Management (#16) — the Setup Transfers wizard.

Reachable from both the `/setup` hub (🔁 Transfers button) and the
`/transfers` hub (⚙️ Setup Transfers). Premium-only. Lives in its own
module rather than bloating ``setup_cog.py`` (mirrors
``member_roster.run_member_roster_setup``); reuses the shared wizard
helpers (`ask_proceed_with_existing_config`, cancel registry, premium gate).

Step 1 is a **setup-shape picker** built around each sheet's *role*, not
its owner (see ``notes/DESIGN_transfer_management.md`` — the 2026-06-08
reconciliation addendum + the role-model follow-up). The real distinction
is "a read-only sheet the bot copies *from*" vs "a sheet the bot reads
*and* can write to", which may be one sheet or two:

- ``source_to_own`` — a read-only shared/intake sheet feeds a separate
  sheet the alliance edits. The bot copies matching rows in, watches the
  alliance's own sheet, and (opt-in) writes status back to it.
- ``own`` — a single read-write sheet. Applicants are already in it; the
  bot watches + (opt-in) writes back. Optional intake sources (a form, a
  shared sheet) can feed it.
- ``watch`` — a single read-only sheet the alliance only watches. The bot
  pings on new applicants and never edits it (no status / removal /
  write-back).

Internally the alliance's *watched/tracked* sheet is always
``alliance_sheet_*``; the two intake-source slots are ``server_wide_*``
(a shared sheet) and ``alliance_form_*`` (the alliance's own form). The
poll loop that consumes this config lives in ``transfer_cog.py``.
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

# Setup-shape modes (stored in `setup_mode`).
_MODE_SOURCE_TO_OWN = "source_to_own"
_MODE_OWN = "own"
_MODE_WATCH = "watch"
_MODE_LABELS = {
    _MODE_SOURCE_TO_OWN: "A shared sheet that populates my own sheet",
    _MODE_OWN: "My own sheet",
    _MODE_WATCH: "A shared sheet I watch",
}


# ── Select-option helpers ─────────────────────────────────────────────────────
#
# Discord caps a SelectOption *value* at 1–100 chars, but a sheet header can be
# long (a whole survey question) or blank. So we key options by the column
# *index* (a short, unique, never-empty value) and map back to the header text.


def _header_options(headers: list, selected=()) -> list:
    """SelectOptions for picking columns, valued by column index. Skips
    blank-header columns (you can't address one), truncates the label to 100,
    and caps at Discord's 25 options. ``selected`` is the list of header texts
    to pre-check."""
    sel = set(selected)
    opts = []
    for i, header in enumerate(headers):
        text = str(header).strip()
        if not text:
            continue
        opts.append(discord.SelectOption(label=text[:100], value=str(i), default=(header in sel)))
        if len(opts) >= 25:
            break
    return opts


def _headers_from_values(headers: list, values) -> list:
    """Map selected option values (column-index strings) back to header text,
    in sheet order."""
    picked = set()
    for v in values:
        try:
            picked.add(int(v))
        except (TypeError, ValueError):
            continue
    return [headers[i] for i in sorted(picked) if 0 <= i < len(headers)]


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
                    "Premium. Run `/upgrade` to unlock it."
                ),
            ),
            view=premium.upgrade_view(),
            ephemeral=True,
        )
        return

    if not await _check_wizard_can_run(interaction, "setup"):
        return

    await interaction.response.send_message(
        "⚙️ Starting Transfer Management setup. Check the channel for prompts.",
        ephemeral=True,
    )
    await run_transfer_setup(interaction, bot)


# ── Sheet entry (a single Sheet ID + tab) ─────────────────────────────────────


class _SheetModal(discord.ui.Modal, title="Transfer sheet"):
    """Collects one Google Sheet ID + tab name."""

    def __init__(self, *, default_id: str, default_tab: str, on_submit):
        super().__init__()
        self._on_submit = on_submit
        self.sheet_id = discord.ui.TextInput(
            label="Google Sheet ID or link",
            default=default_id or None,
            placeholder="Paste the sheet's URL or just its ID",
            required=True,
            max_length=300,
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
    """Enter (or keep) a sheet, verifying read access before advancing. On
    success ``result`` is ``(sheet_id, tab, header, rows)``. Used for the
    optional intake-source sheets."""

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
        # Accept a pasted full Sheets URL, not just the bare ID.
        sheet_id = transfer.extract_sheet_id(sheet_id)
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
                        f"✅ Read **{tab}**: {len(header)} columns, {len(rows)} rows. "
                        "Continuing below."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass
        await interaction.followup.send("✅ Got it, continuing below.", ephemeral=True)
        self.stop()


# ── Step 1: setup-shape picker ────────────────────────────────────────────────


class _Mode1Modal(discord.ui.Modal, title="Your two sheets"):
    """The two-sheet (``source_to_own``) modal: the read-only intake sheet on
    top (the entry point), the alliance's own editable sheet below."""

    def __init__(self, *, current: dict, on_submit):
        super().__init__()
        self._on_submit = on_submit
        self.intake_id = discord.ui.TextInput(
            label="Shared (intake) Sheet ID or link",
            default=current.get("server_wide_sheet_id") or None,
            placeholder="The read-only sheet applicants come in on",
            required=True,
            max_length=300,
        )
        self.intake_tab = discord.ui.TextInput(
            label="Shared sheet tab name",
            default=current.get("server_wide_sheet_tab") or None,
            placeholder="e.g. Applications",
            required=True,
            max_length=100,
        )
        self.own_id = discord.ui.TextInput(
            label="Your Sheet ID or link",
            default=current.get("alliance_sheet_id") or None,
            placeholder="The sheet you edit and track in",
            required=True,
            max_length=300,
        )
        self.own_tab = discord.ui.TextInput(
            label="Your sheet tab name",
            default=current.get("alliance_sheet_tab") or None,
            placeholder="e.g. Applicants",
            required=True,
            max_length=100,
        )
        for item in (self.intake_id, self.intake_tab, self.own_id, self.own_tab):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await self._on_submit(
            interaction,
            self.intake_id.value.strip(),
            self.intake_tab.value.strip(),
            self.own_id.value.strip(),
            self.own_tab.value.strip(),
        )


class _ModeStepView(discord.ui.View):
    """Three setup shapes. Each button opens the right modal; on submit the
    sheet(s) are read to verify access, and ``mode`` + ``result`` carry the
    choice. ``result`` maps a role (``"intake"`` / ``"alliance"``) to a
    ``(sheet_id, tab, header, rows)`` tuple."""

    def __init__(self, *, owner_id: int, current: dict):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.current = current
        self.message: discord.Message | None = None
        self.mode: str | None = None
        self.result: dict | None = None
        self.confirmed = False

        b1 = discord.ui.Button(
            label="🔀 A shared sheet that populates my own sheet",
            style=discord.ButtonStyle.primary,
            row=0,
        )
        b1.callback = self._pick_source_to_own
        self.add_item(b1)
        b2 = discord.ui.Button(label="🏠 My own sheet", style=discord.ButtonStyle.primary, row=1)
        b2.callback = self._pick_own
        self.add_item(b2)
        b3 = discord.ui.Button(
            label="👀 A shared sheet that I watch", style=discord.ButtonStyle.primary, row=2
        )
        b3.callback = self._pick_watch
        self.add_item(b3)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _pick_source_to_own(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _Mode1Modal(current=self.current, on_submit=self._on_mode1)
        )

    async def _pick_own(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _SheetModal(
                default_id=self.current.get("alliance_sheet_id") or "",
                default_tab=self.current.get("alliance_sheet_tab") or "",
                on_submit=self._on_single(_MODE_OWN),
            )
        )

    async def _pick_watch(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _SheetModal(
                default_id=self.current.get("alliance_sheet_id") or "",
                default_tab=self.current.get("alliance_sheet_tab") or "",
                on_submit=self._on_single(_MODE_WATCH),
            )
        )

    def _on_single(self, mode: str):
        async def _cb(interaction: discord.Interaction, sheet_id: str, tab: str):
            await self._verify(interaction, mode, [("alliance", sheet_id, tab)])

        return _cb

    async def _on_mode1(self, interaction, intake_id, intake_tab, own_id, own_tab):
        await self._verify(
            interaction,
            _MODE_SOURCE_TO_OWN,
            [("intake", intake_id, intake_tab), ("alliance", own_id, own_tab)],
        )

    async def _verify(self, interaction: discord.Interaction, mode: str, sheets: list):
        await interaction.response.defer(thinking=True, ephemeral=True)
        labels = {
            "intake": "shared (intake)",
            "alliance": "watched" if mode == _MODE_WATCH else "your",
        }
        read: dict = {}
        for role, sid, tab in sheets:
            sid = transfer.extract_sheet_id(sid)
            try:
                header, rows = await asyncio.to_thread(transfer_sheets.read_sheet, sid, tab)
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(
                    f"⚠️ Couldn't read the {labels.get(role, role)} sheet/tab: "
                    f"{config.describe_sheet_error(e)}\n"
                    "Check the Sheet ID + tab and that the bot's service account has access, "
                    "then click the button again.",
                    ephemeral=True,
                )
                return
            if not header:
                await interaction.followup.send(
                    f"⚠️ The {labels.get(role, role)} tab has no header row. Put your column "
                    "headers in row 1, then click the button again.",
                    ephemeral=True,
                )
                return
            read[role] = (sid, tab, header, rows)

        self.mode = mode
        self.result = read
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                summary = " · ".join(f"{labels.get(r, r)}: {len(read[r][2])} cols" for r in read)
                await self.message.edit(
                    content=f"✅ Connected ({summary}). Continuing below.", view=self
                )
            except discord.HTTPException:
                pass
        await interaction.followup.send("✅ Got it, continuing below.", ephemeral=True)
        self.stop()


def _mode_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🔁 Step 1: How is your applicant data set up?",
        color=discord.Color.blurple(),
        description=(
            "Pick the shape that matches how your alliance handles transfer applicants. "
            "You can re-run setup to change it later."
        ),
    )
    embed.add_field(
        name="🔀 A shared sheet that populates my own sheet",
        value=(
            "This is a two-sheet setup where you have one that is the entry point for data and "
            "then one that you edit separately. (E.g. your entire server shares a sheet but your "
            "alliance needs to add notes and track statuses of your own applicants)"
        ),
        inline=False,
    )
    embed.add_field(
        name="🏠 My own sheet",
        value=(
            "This is when you have a single sheet that you add transfers to, record statuses, and "
            "make edits all in one place."
        ),
        inline=False,
    )
    embed.add_field(
        name="👀 A shared sheet that I watch",
        value=(
            "This is when there is a sheet you have access to see, but you do not make any edits "
            "and do not want to make edits. (E.g. you can see your server-wide sheet but don't "
            "track anything yourself in sheets)"
        ),
        inline=False,
    )
    return embed


# ── Column mapping (category pickers) ─────────────────────────────────────────


class _ColumnMapView(discord.ui.View):
    """Name (required) + optional Status / Display / Identity-Fallback
    multi-selects, all seeded from the auto-map guess. ``include_status=False``
    drops the Status picker (a read-only watched sheet has nothing to write or
    track changes on). Only the first 25 headers are pickable (Discord's
    select cap)."""

    def __init__(self, *, owner_id: int, headers: list, initial_map: dict, include_status=True):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.headers = headers[:25]
        self.truncated = len(headers) > 25
        self.message: discord.Message | None = None
        self.saved = False
        self.include_status = include_status
        self.sel_name = initial_map.get("name") or None
        self.sel_status = list(initial_map.get("status") or [])
        self.sel_display = list(initial_map.get("display") or [])
        self.sel_identity = list(initial_map.get("identity_extra") or [])

        # Dropdowns are numbered ①–④ and pre-filled with the guess so the
        # selected values stay visible. Discord hides a select's placeholder
        # once anything is selected, so the embed carries a numbered legend
        # (top-to-bottom) saying what each dropdown is; an *empty* category
        # falls back to showing its numbered placeholder.
        name_sel = discord.ui.Select(
            placeholder="① Name column (required)",
            min_values=1,
            max_values=1,
            row=0,
            options=_header_options(self.headers, [self.sel_name] if self.sel_name else []),
        )
        name_sel.callback = self._on_name
        self.add_item(name_sel)

        row = 1
        if include_status:
            self._add_multi(
                "② Status columns to watch (optional)", row, self.sel_status, self._on_status
            )
            row += 1
            disp_num, id_num = "③", "④"
        else:
            disp_num, id_num = "②", "③"
        self._add_multi(
            f"{disp_num} Columns to show in notices (optional)",
            row,
            self.sel_display,
            self._on_display,
        )
        row += 1
        self._add_multi(
            f"{id_num} Identity Fallback, e.g. Server (optional)",
            row,
            self.sel_identity,
            self._on_identity,
        )

        save = discord.ui.Button(label="✅ Save mapping", style=discord.ButtonStyle.success, row=4)
        save.callback = self._on_save
        self.add_item(save)

    def _add_multi(self, placeholder: str, row: int, selected: list, callback):
        opts = _header_options(self.headers, selected)
        sel = discord.ui.Select(
            placeholder=placeholder,
            min_values=0,
            max_values=max(1, len(opts)),
            row=row,
            options=opts,
        )
        sel.callback = callback
        self.add_item(sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _on_name(self, interaction: discord.Interaction):
        picked = _headers_from_values(self.headers, interaction.data["values"])
        self.sel_name = picked[0] if picked else None
        await interaction.response.defer()

    async def _on_status(self, interaction: discord.Interaction):
        self.sel_status = _headers_from_values(self.headers, interaction.data["values"])
        await interaction.response.defer()

    async def _on_display(self, interaction: discord.Interaction):
        self.sel_display = _headers_from_values(self.headers, interaction.data["values"])
        await interaction.response.defer()

    async def _on_identity(self, interaction: discord.Interaction):
        self.sel_identity = _headers_from_values(self.headers, interaction.data["values"])
        await interaction.response.defer()

    def column_map(self) -> dict:
        out: dict = {"name": self.sel_name}
        if self.sel_identity:
            out["identity_extra"] = self.sel_identity
        if self.include_status and self.sel_status:
            out["status"] = self.sel_status
        if self.sel_display:
            out["display"] = self.sel_display
        return out

    async def _on_save(self, interaction: discord.Interaction):
        if not self.sel_name:
            await interaction.response.send_message(
                "⚠️ Pick a **Name** column. It's the one required field.", ephemeral=True
            )
            return
        self.saved = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


def _map_embed(column_map: dict, truncated: bool, include_status: bool = True) -> discord.Embed:
    if include_status:
        legend = (
            "① **Name** (required): identifies each applicant\n"
            "② **Status to watch**: a change here posts a status-change notice\n"
            "③ **Show in notices**: the columns shown in each ping (pick as many as you like)\n"
            "④ **Identity Fallback** (e.g. Server): tells apart two people with the same name"
        )
    else:
        legend = (
            "① **Name** (required): identifies each applicant\n"
            "② **Show in notices**: the columns shown in each ping (pick as many as you like)\n"
            "③ **Identity Fallback** (e.g. Server): tells apart two people with the same name"
        )
    embed = discord.Embed(
        title="🔁 Map your columns",
        color=discord.Color.blurple(),
        description=(
            "I read your sheet and pre-filled my best guess. The dropdowns below are, top to "
            "bottom:\n"
            f"{legend}\n\n"
            "Change a dropdown to override, then **Save mapping**.\n\n"
            f"**My guess:**\n{transfer.summarize_column_map(column_map)}"
        ),
    )
    if truncated:
        embed.set_footer(text="Only the first 25 columns are pickable here.")
    return embed


async def _map_step(
    channel, guild_id, owner_id, header, saved_map, *, include_status, cancel_event
):
    """Run the column-mapping step against ``header``, save it to
    ``alliance_column_map_json``, and return the column map (or a
    ``"CANCEL"`` / ``"TIMEOUT"`` sentinel)."""
    initial_map = saved_map or transfer.suggest_column_map(header)
    if not include_status:
        initial_map = {k: v for k, v in initial_map.items() if k != "status"}
    view = _ColumnMapView(
        owner_id=owner_id, headers=header, initial_map=initial_map, include_status=include_status
    )
    view.message = await channel.send(
        embed=_map_embed(initial_map, len(header) > 25, include_status=include_status), view=view
    )
    await wizard_registry.wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return "CANCEL"
    if not view.saved:
        return "TIMEOUT"
    column_map = view.column_map()
    config.update_transfer_config_field(
        guild_id, "alliance_column_map_json", json.dumps(column_map)
    )
    await channel.send(f"✅ **Mapping saved.**\n{transfer.summarize_column_map(column_map)}")
    return column_map


# ── Notification channel / style ──────────────────────────────────────────────


class _ChannelStepView(discord.ui.View):
    """Pick the text channel new-applicant / status-change notices post to."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.selected_id: int | None = None
        self.confirmed = False
        self.message: discord.Message | None = None
        self._sel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text],
            placeholder="Pick the notifications channel",
            min_values=1,
            max_values=1,
        )
        self._sel.callback = self._cb
        self.add_item(self._sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _cb(self, interaction: discord.Interaction):
        self.selected_id = self._sel.values[0].id
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


class _StyleStepView(discord.ui.View):
    """Per-applicant message vs a batched digest when several land at once."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.style: str | None = None
        self.confirmed = False

        each = discord.ui.Button(
            label="📨 A message per applicant", style=discord.ButtonStyle.primary
        )
        each.callback = self._make("each")
        self.add_item(each)
        digest = discord.ui.Button(
            label="📋 One digest when several arrive", style=discord.ButtonStyle.secondary
        )
        digest.callback = self._make("digest")
        self.add_item(digest)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    def _make(self, value: str):
        async def _cb(interaction: discord.Interaction):
            self.style = value
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(interaction, view=self)
            self.stop()

        return _cb


# Template kinds walked at the end of setup, in order.
_TEMPLATE_STEPS = [
    ("apply_invitation", "Initial outreach"),
    ("confirm_request", "Confirmation request"),
    ("decline", "Decline"),
]


# ── New-applicant / pull filter builder ───────────────────────────────────────

# Comparison label → operator for the numeric-column control. Labels spell out
# the symbol so it's unambiguous.
_FILTER_OPS = [
    ("≥ (at least)", ">="),
    ("≤ (at most)", "<="),
    ("= (equals)", "=="),
    ("> (greater than)", ">"),
    ("< (less than)", "<"),
]


class _ButtonChoiceView(discord.ui.View):
    """Generic labelled-button row → ``value`` (+ ``confirmed`` / ``cancelled``).
    Reused for yes/no, operator pick, and add-another."""

    def __init__(self, owner_id: int, options: list):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.value = None
        self.confirmed = False
        for label, value, style in options:
            btn = discord.ui.Button(label=label[:80], style=style)
            btn.callback = self._make(value)
            self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    def _make(self, value):
        async def _cb(interaction: discord.Interaction):
            self.value = value
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(interaction, view=self)
            self.stop()

        return _cb


class _FilterColumnView(discord.ui.View):
    """Single-select of the sheet's columns to filter on."""

    def __init__(self, owner_id: int, header: list):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self._headers = header
        self.column = None
        self.confirmed = False
        self._sel = discord.ui.Select(
            placeholder="Pick a column to filter on",
            min_values=1,
            max_values=1,
            options=_header_options(header),
        )
        self._sel.callback = self._cb
        self.add_item(self._sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _cb(self, interaction: discord.Interaction):
        picked = _headers_from_values(self._headers, interaction.data["values"])
        self.column = picked[0] if picked else None
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


class _FilterMultiView(discord.ui.View):
    """Multi-select of a column's distinct values (the ``in`` control)."""

    def __init__(self, owner_id: int, distinct: list):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self._distinct = distinct[:25]
        self.values = None
        self.confirmed = False
        # Index-valued options: a distinct cell value can exceed Discord's
        # 100-char option-value cap, so map back by position.
        opts = [
            discord.SelectOption(label=(str(v)[:100] or " "), value=str(i))
            for i, v in enumerate(self._distinct)
        ]
        self._sel = discord.ui.Select(
            placeholder="Pick the value(s) to match",
            min_values=1,
            max_values=len(opts),
            options=opts,
        )
        self._sel.callback = self._cb
        self.add_item(self._sel)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _cb(self, interaction: discord.Interaction):
        picked = []
        for v in interaction.data["values"]:
            try:
                picked.append(self._distinct[int(v)])
            except (TypeError, ValueError, IndexError):
                continue
        self.values = picked
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


class _FilterValueModal(discord.ui.Modal):
    def __init__(self, *, title: str, label: str, on_submit):
        super().__init__(title=title[:45])
        self._on_submit = on_submit
        self.value_input = discord.ui.TextInput(label=label[:45], required=True, max_length=100)
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self._on_submit(interaction, self.value_input.value.strip())


class _FilterValueView(discord.ui.View):
    """Button → modal for a free-text / numeric-threshold filter value."""

    def __init__(self, owner_id: int, *, prompt_title: str, prompt_label: str):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.value = None
        self.confirmed = False
        self.message: discord.Message | None = None
        self._title = prompt_title
        self._label = prompt_label
        btn = discord.ui.Button(label="✏️ Enter value", style=discord.ButtonStyle.primary)
        btn.callback = self._enter
        self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _enter(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            _FilterValueModal(title=self._title, label=self._label, on_submit=self._save)
        )

    async def _save(self, interaction: discord.Interaction, value: str):
        self.value = value
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        await interaction.response.send_message(f"✅ Using **{value}**", ephemeral=True)
        self.stop()


async def _build_clause(channel, owner_id, col, kind, distinct, cancel_event):
    """One filter clause for column ``col``, control chosen by ``kind``.
    Returns a clause dict, or the ``"CANCEL"`` / ``"TIMEOUT"`` sentinel."""
    if kind == "numeric":
        opv = _ButtonChoiceView(
            owner_id, [(lbl, op, discord.ButtonStyle.secondary) for lbl, op in _FILTER_OPS]
        )
        await channel.send(f"How should **{col}** compare?", view=opv)
        await wizard_registry.wait_view_or_cancel(opv, cancel_event)
        if opv.cancelled:
            return "CANCEL"
        if not opv.confirmed:
            return "TIMEOUT"
        valv = _FilterValueView(owner_id, prompt_title=col, prompt_label="Threshold (e.g. 250M)")
        valv.message = await channel.send(
            f"Enter the threshold for **{col}** (e.g. `250M`):", view=valv
        )
        await wizard_registry.wait_view_or_cancel(valv, cancel_event)
        if valv.cancelled:
            return "CANCEL"
        if not valv.confirmed:
            return "TIMEOUT"
        return {"column": col, "op": opv.value, "value": valv.value}

    # Non-numeric. The sampled values are NOT the full set of what could ever
    # appear (a cell might be "t0s, OGV, open to sponsorship"), so "contains
    # text" is always offered, not just an exact-value pick.
    match = "contains"
    if distinct:
        how = _ButtonChoiceView(
            owner_id,
            [
                ("🔤 Contains text", "contains", discord.ButtonStyle.primary),
                ("🎯 Is one of specific values", "in", discord.ButtonStyle.secondary),
            ],
        )
        await channel.send(f"How should **{col}** match?", view=how)
        await wizard_registry.wait_view_or_cancel(how, cancel_event)
        if how.cancelled:
            return "CANCEL"
        if not how.confirmed:
            return "TIMEOUT"
        match = how.value

    if match == "in":
        mv = _FilterMultiView(owner_id, distinct)
        await channel.send(f"Pick the value(s) **{col}** must be one of:", view=mv)
        await wizard_registry.wait_view_or_cancel(mv, cancel_event)
        if mv.cancelled:
            return "CANCEL"
        if not mv.confirmed:
            return "TIMEOUT"
        return {"column": col, "op": "in", "value": mv.values}

    valv = _FilterValueView(owner_id, prompt_title=col, prompt_label="Contains text")
    valv.message = await channel.send(
        f"What text should **{col}** contain? (e.g. `OGV`)", view=valv
    )
    await wizard_registry.wait_view_or_cancel(valv, cancel_event)
    if valv.cancelled:
        return "CANCEL"
    if not valv.confirmed:
        return "TIMEOUT"
    return {"column": col, "op": "contains", "value": valv.value}


async def _build_filter(
    channel,
    owner_id,
    header,
    rows,
    cancel_event,
    *,
    intro: str | None = None,
    none_label: str = "✅ Every new applicant",
    build_label: str = "🔎 Only ones matching a filter",
):
    """Walk the recruiter through an AND filter. Returns the filter dict,
    ``None`` (no filter), or a ``"CANCEL"`` / ``"TIMEOUT"`` sentinel. Reused
    for the new-applicant notification filter, the "for us" watch filter, and
    the source pull filter — the labels/intro adapt via the keyword args."""
    if intro is None:
        intro = (
            "**New-applicant filter**\n"
            "Get pinged for *every* new applicant, or only the ones matching a filter? "
            "(Status changes always notify, filter or not.)"
        )
    start = _ButtonChoiceView(
        owner_id,
        [
            (none_label, "none", discord.ButtonStyle.secondary),
            (build_label, "build", discord.ButtonStyle.primary),
        ],
    )
    await channel.send(intro, view=start)
    await wizard_registry.wait_view_or_cancel(start, cancel_event)
    if start.cancelled:
        return "CANCEL"
    if not start.confirmed:
        return "TIMEOUT"
    if start.value == "none":
        return None

    hidx = transfer.header_index(header)
    clauses: list = []
    while True:
        colv = _FilterColumnView(owner_id, header)
        await channel.send("Pick a column to filter on:", view=colv)
        await wizard_registry.wait_view_or_cancel(colv, cancel_event)
        if colv.cancelled:
            return "CANCEL"
        if not colv.confirmed:
            return "TIMEOUT"
        col = colv.column
        idx = hidx.get(transfer._norm_header(col))
        samples = [r[idx] for r in rows if idx is not None and idx < len(r)]
        kind, distinct = transfer.column_value_kind(samples)
        clause = await _build_clause(channel, owner_id, col, kind, distinct, cancel_event)
        if clause in ("CANCEL", "TIMEOUT"):
            return clause
        if isinstance(clause, dict):
            clauses.append(clause)
        more = _ButtonChoiceView(
            owner_id,
            [
                ("➕ Add another", "more", discord.ButtonStyle.primary),
                ("✅ Done", "done", discord.ButtonStyle.success),
            ],
        )
        await channel.send(
            f"**Filter so far:** {transfer.describe_filter({'and': clauses})}", view=more
        )
        await wizard_registry.wait_view_or_cancel(more, cancel_event)
        if more.cancelled:
            return "CANCEL"
        if not more.confirmed or more.value == "done":
            break
    return {"and": clauses} if clauses else None


# ── Intake sources (a shared sheet / the alliance's own form) ─────────────────


class _SourceMapView(discord.ui.View):
    """A lighter mapping for a *source* sheet: just Name (required) + any
    Identity-Fallback columns, used to dedup which rows get copied. Display /
    status don't apply — the whole row is copied, aligned to the alliance
    sheet's columns."""

    def __init__(self, *, owner_id: int, headers: list, initial_map: dict):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.headers = headers[:25]
        self.saved = False
        self.sel_name = initial_map.get("name") or None
        self.sel_identity = list(initial_map.get("identity_extra") or [])

        name_sel = discord.ui.Select(
            placeholder="① Name column (required)",
            min_values=1,
            max_values=1,
            row=0,
            options=_header_options(self.headers, [self.sel_name] if self.sel_name else []),
        )
        name_sel.callback = self._on_name
        self.add_item(name_sel)

        id_opts = _header_options(self.headers, self.sel_identity)
        id_sel = discord.ui.Select(
            placeholder="② Identity Fallback, e.g. Server (optional)",
            min_values=0,
            max_values=max(1, len(id_opts)),
            row=1,
            options=id_opts,
        )
        id_sel.callback = self._on_identity
        self.add_item(id_sel)

        save = discord.ui.Button(label="✅ Save", style=discord.ButtonStyle.success, row=2)
        save.callback = self._on_save
        self.add_item(save)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _on_name(self, interaction: discord.Interaction):
        picked = _headers_from_values(self.headers, interaction.data["values"])
        self.sel_name = picked[0] if picked else None
        await interaction.response.defer()

    async def _on_identity(self, interaction: discord.Interaction):
        self.sel_identity = _headers_from_values(self.headers, interaction.data["values"])
        await interaction.response.defer()

    def column_map(self) -> dict:
        out: dict = {"name": self.sel_name}
        if self.sel_identity:
            out["identity_extra"] = self.sel_identity
        return out

    async def _on_save(self, interaction: discord.Interaction):
        if not self.sel_name:
            await interaction.response.send_message(
                "⚠️ Pick a **Name** column — it's required to avoid copying the same person twice.",
                ephemeral=True,
            )
            return
        self.saved = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


def _source_map_embed(column_map: dict) -> discord.Embed:
    return discord.Embed(
        title="🔁 Source sheet: identity",
        color=discord.Color.blurple(),
        description=(
            "Pick the **Name** column (and any **Identity Fallback** like Server) so the bot can "
            "tell applicants apart and not copy the same person twice. The *whole* matching row "
            "gets copied into your sheet.\n\n" + transfer.summarize_column_map(column_map)
        ),
    )


async def _configure_source(
    channel,
    guild_id,
    user_id,
    *,
    prefix,
    sheet_id,
    tab,
    header,
    rows,
    current,
    cancel_event,
    filter_intro=None,
):
    """Map + filter an already-entered source sheet and save its ``{prefix}_*``
    config (enabled). Returns ``"OK"`` / ``"CANCEL"`` / ``"TIMEOUT"``. Used
    inline when the source sheet was already read (the two-sheet mode's intake
    sheet), and by :func:`_run_source_step` after it reads an optional one."""
    existing = (
        transfer.parse_column_map(current.get(f"{prefix}_column_map_json"))
        if current.get(f"{prefix}_sheet_id")
        else {}
    )
    initial = existing or transfer.suggest_column_map(header)
    map_view = _SourceMapView(owner_id=user_id, headers=header, initial_map=initial)
    map_view.message = await channel.send(embed=_source_map_embed(initial), view=map_view)
    await wizard_registry.wait_view_or_cancel(map_view, cancel_event)
    if map_view.cancelled:
        return "CANCEL"
    if not map_view.saved:
        return "TIMEOUT"
    src_map = map_view.column_map()

    filt = await _build_filter(
        channel,
        user_id,
        header,
        rows,
        cancel_event,
        intro=filter_intro
        or (
            "**Which rows should the bot pull into your sheet?**\n"
            "For example: only rows where *Requested Landing Alliance* contains your tag."
        ),
        none_label="📥 Pull every row",
        build_label="🔎 Only rows matching a filter",
    )
    if filt in ("CANCEL", "TIMEOUT"):
        return filt

    config.update_transfer_config_fields(
        guild_id,
        **{
            f"{prefix}_enabled": 1,
            f"{prefix}_sheet_id": sheet_id,
            f"{prefix}_sheet_tab": tab,
            f"{prefix}_column_map_json": json.dumps(src_map),
            f"{prefix}_filter_json": json.dumps(filt) if filt else "",
        },
    )
    await channel.send(
        f"✅ Connected. Rows matching **{transfer.describe_filter(filt)}** will be copied into "
        "your sheet (whole row, aligned to your columns)."
    )
    return "OK"


async def _run_source_step(
    channel, guild_id, user_id, *, step_label, intro_text, prefix, current, cancel_event
):
    """Offer one *optional* intake source (``server_wide`` / ``alliance_form``):
    skip, or enter a sheet then map + filter it. Returns ``"OK"`` / ``"SKIP"`` /
    ``"CANCEL"`` / ``"TIMEOUT"`` and saves the ``{prefix}_*`` config."""
    choice = _ButtonChoiceView(
        user_id,
        [
            ("⏭️ Skip", "skip", discord.ButtonStyle.secondary),
            ("➕ Connect a sheet", "connect", discord.ButtonStyle.primary),
        ],
    )
    await channel.send(f"**{step_label}**\n{intro_text}", view=choice)
    await wizard_registry.wait_view_or_cancel(choice, cancel_event)
    if choice.cancelled:
        return "CANCEL"
    if not choice.confirmed:
        return "TIMEOUT"
    if choice.value == "skip":
        config.update_transfer_config_field(guild_id, f"{prefix}_enabled", 0)
        await channel.send("⏭️ Skipped.")
        return "SKIP"

    sheet_view = _SheetStepView(
        owner_id=user_id,
        default_id=current.get(f"{prefix}_sheet_id") or "",
        default_tab=current.get(f"{prefix}_sheet_tab") or "",
    )
    sheet_view.message = await channel.send(
        "Enter the source sheet (Sheet ID + tab). The bot's service account needs read access.",
        view=sheet_view,
    )
    await wizard_registry.wait_view_or_cancel(sheet_view, cancel_event)
    if sheet_view.cancelled:
        return "CANCEL"
    if not sheet_view.confirmed or not sheet_view.result:
        return "TIMEOUT"
    s_id, s_tab, s_header, s_rows = sheet_view.result
    return await _configure_source(
        channel,
        guild_id,
        user_id,
        prefix=prefix,
        sheet_id=s_id,
        tab=s_tab,
        header=s_header,
        rows=s_rows,
        current=current,
        cancel_event=cancel_event,
    )


# ── Shared tail steps (channel / style / templates / removal / write-back) ────


async def _step_channel(channel, owner_id, cancel_event):
    view = _ChannelStepView(owner_id=owner_id)
    view.message = await channel.send(
        "**Notification channel**\n"
        "Where should new-applicant and status-change notices post? A dedicated recruiting "
        "channel works well.",
        view=view,
    )
    await wizard_registry.wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return ("CANCEL", None)
    if not view.confirmed or not view.selected_id:
        return ("TIMEOUT", None)
    return ("OK", view.selected_id)


async def _step_style(channel, owner_id, cancel_event):
    view = _StyleStepView(owner_id=owner_id)
    await channel.send(
        "**How should notifications arrive?**\n"
        "• **A message per applicant**: richest; great for a dedicated channel where you watch "
        "people arrive.\n"
        "• **One digest**: a single batched message when several land in the same check (tidier "
        "on a busy day).",
        view=view,
    )
    await wizard_registry.wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return ("CANCEL", None)
    if not view.confirmed or not view.style:
        return ("TIMEOUT", None)
    return ("OK", view.style)


async def _step_templates(channel, guild_id, owner_id, current, cancel_event):
    """Walk the three in-game message templates. Returns ``"OK"`` or
    ``"ABORT"`` (``ask_keep_or_change`` posts its own cancel/timeout notice)."""
    from setup_cog import ask_keep_or_change
    from defaults import DEFAULT_TRANSFER_TEMPLATES

    await channel.send(
        "**In-game message templates**\n"
        "These are the messages you copy into game chat. Use `{name}` for the applicant, "
        "`{alliance_name}` for your alliance, or any column you show in notices as `{token}` "
        "(e.g. `{total_hero_power}`)."
    )
    for kind, label in _TEMPLATE_STEPS:
        default_body = DEFAULT_TRANSFER_TEMPLATES[kind]
        chosen = await ask_keep_or_change(
            channel,
            f"**{label} template**",
            default=default_body,
            current=current.get(f"template_{kind}", ""),
            modal_title=label[:45],
            modal_label="Message text",
            cancel_event=cancel_event,
        )
        if chosen is None:
            return "ABORT"
        stored = "" if chosen.strip() == default_body.strip() else chosen
        config.update_transfer_config_field(guild_id, f"template_{kind}", stored)
    return "OK"


async def _step_removal(channel, guild_id, owner_id, cancel_event):
    from setup_cog import YesNoView

    view = YesNoView()
    await channel.send(
        "**Removal notices?**\n"
        "When someone who'd been marked (Confirmed / Declined / …) is *removed* from your sheet, "
        "want a heads-up? Pending rows that never had a status set never notify.",
        view=view,
    )
    await wizard_registry.wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return "CANCEL"
    if view.selected is None:
        return "TIMEOUT"
    config.update_transfer_config_field(guild_id, "notify_on_delete", 1 if view.selected else 0)
    return "OK"


async def _step_writeback(channel, guild_id, owner_id, current, cancel_event, status_cols):
    from setup_cog import YesNoView, ask_keep_or_change

    view = YesNoView()
    await channel.send(
        "**Decision write-back?**\n"
        f"Add buttons to each notification so you can mark an applicant "
        f"(**{', '.join(status_cols)}**) right from Discord, and the bot writes it back to your "
        "sheet. Leave off to keep the bot read-only.",
        view=view,
    )
    await wizard_registry.wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return "CANCEL"
    if view.selected is None:
        return "TIMEOUT"
    if view.selected:
        wb_value = await ask_keep_or_change(
            channel,
            "**Write-back value**\n"
            "What should the bot write when you click a Set button? Google Sheets checkboxes use "
            "**TRUE** (so the box ticks); use your own word if your sheet expects e.g. `Yes`.",
            default="TRUE",
            current=current.get("writeback_value", ""),
            modal_title="Write-back value",
            modal_label="Value to write",
            cancel_event=cancel_event,
        )
        if wb_value is None:
            return "ABORT"
        config.update_transfer_config_fields(
            guild_id, writeback_enabled=1, writeback_value=wb_value or "TRUE"
        )
    else:
        config.update_transfer_config_field(guild_id, "writeback_enabled", 0)
    return "OK"


# ── Wizard ────────────────────────────────────────────────────────────────────


async def run_transfer_setup(interaction: discord.Interaction, bot):
    """Walk leadership through the setup-shape picker, sheet mapping, sources,
    notifications, templates, and go-live."""
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
                ("Setup type", _MODE_LABELS.get(current.get("setup_mode") or "", "—")),
                (
                    "Tracked sheet",
                    f"`{sid[:18]}…` · tab **{current.get('alliance_sheet_tab') or '*not set*'}**"
                    if sid
                    else "*not set*",
                ),
                ("Columns", transfer.summarize_column_map(saved_map)),
            ]
            proceed = await ask_proceed_with_existing_config(
                channel,
                title="💎 Transfer Management: current setup",
                description="Transfer Management is already set up. Want to reconfigure it?",
                fields=fields,
                cancel_event=cancel_event,
                no_changes_message="✅ No changes made. Your transfer setup is unchanged.",
            )
            if proceed is not True:
                return

        await channel.send(
            "💎 **Transfer Management setup**\n"
            "The bot watches a recruiting sheet, pings you on new applicants and status changes, "
            "and drafts your in-game messages. First, tell it how your sheets are set up."
        )

        # ── Step 1: setup shape ───────────────────────────────────────────────
        mode_view = _ModeStepView(owner_id=user.id, current=current)
        mode_view.message = await channel.send(embed=_mode_embed(), view=mode_view)
        await wizard_registry.wait_view_or_cancel(mode_view, cancel_event)
        if mode_view.cancelled:
            return
        if not mode_view.confirmed or not mode_view.mode or not mode_view.result:
            await channel.send(_TIMEOUT_MSG)
            return
        mode = mode_view.mode
        config.update_transfer_config_field(guild_id, "setup_mode", mode)

        # ── Sheets + mapping (per mode) ───────────────────────────────────────
        if mode == _MODE_SOURCE_TO_OWN:
            intake_id, intake_tab, intake_header, intake_rows = mode_view.result["intake"]
            sheet_id, tab, header, rows = mode_view.result["alliance"]
            config.update_transfer_config_fields(
                guild_id, alliance_sheet_id=sheet_id, alliance_sheet_tab=tab
            )

            # Intake source first (the entry point): map + pull filter.
            await channel.send(
                "**Your shared (intake) sheet**\n"
                "This is the read-only sheet applicants come in on. The bot copies matching rows "
                "from it into your own sheet (it never edits the shared one)."
            )
            src = await _configure_source(
                channel,
                guild_id,
                user.id,
                prefix="server_wide",
                sheet_id=intake_id,
                tab=intake_tab,
                header=intake_header,
                rows=intake_rows,
                current=current,
                cancel_event=cancel_event,
            )
            if src == "CANCEL":
                return
            if src == "TIMEOUT":
                await channel.send(_TIMEOUT_MSG)
                return

            await channel.send(
                "**Your own sheet**\nNow map the sheet you edit and track applicants in."
            )
            cm = await _map_step(
                channel,
                guild_id,
                user.id,
                header,
                saved_map,
                include_status=True,
                cancel_event=cancel_event,
            )

        elif mode == _MODE_OWN:
            sheet_id, tab, header, rows = mode_view.result["alliance"]
            config.update_transfer_config_fields(
                guild_id, alliance_sheet_id=sheet_id, alliance_sheet_tab=tab
            )
            await channel.send(
                "**Your sheet**\nMap the columns of the sheet you track applicants in."
            )
            cm = await _map_step(
                channel,
                guild_id,
                user.id,
                header,
                saved_map,
                include_status=True,
                cancel_event=cancel_event,
            )

        else:  # _MODE_WATCH
            sheet_id, tab, header, rows = mode_view.result["alliance"]
            config.update_transfer_config_fields(
                guild_id, alliance_sheet_id=sheet_id, alliance_sheet_tab=tab
            )
            # Read-only watch: never write back, no removal, no sources. Clear
            # any stale opt-ins from a previous setup shape.
            config.update_transfer_config_fields(
                guild_id,
                writeback_enabled=0,
                notify_on_delete=0,
                server_wide_enabled=0,
                alliance_form_enabled=0,
            )
            await channel.send(
                "**The sheet you watch**\nMap its columns. The bot only reads this sheet and "
                "pings you; it never edits it, so there's no status-change tracking here."
            )
            cm = await _map_step(
                channel,
                guild_id,
                user.id,
                header,
                saved_map,
                include_status=False,
                cancel_event=cancel_event,
            )

        if cm == "CANCEL":
            return
        if cm == "TIMEOUT":
            await channel.send(_TIMEOUT_MSG)
            return
        column_map = cm

        # ── Notification channel + style (all modes) ──────────────────────────
        st, chan_id = await _step_channel(channel, user.id, cancel_event)
        if st == "CANCEL":
            return
        if st == "TIMEOUT":
            await channel.send(_TIMEOUT_MSG)
            return
        config.update_transfer_config_field(guild_id, "notification_channel_id", chan_id)

        st, style = await _step_style(channel, user.id, cancel_event)
        if st == "CANCEL":
            return
        if st == "TIMEOUT":
            await channel.send(_TIMEOUT_MSG)
            return
        config.update_transfer_config_field(guild_id, "notification_style", style)

        # ── Filter ────────────────────────────────────────────────────────────
        # Mode 1 has no notification filter: the source pull filter is the gate.
        if mode == _MODE_OWN:
            filt = await _build_filter(channel, user.id, header, rows, cancel_event)
            if filt == "CANCEL":
                return
            if filt == "TIMEOUT":
                await channel.send(_TIMEOUT_MSG)
                return
            config.update_transfer_config_field(
                guild_id, "notification_filter_json", json.dumps(filt) if filt else ""
            )
            await channel.send(f"✅ Filter: {transfer.describe_filter(filt)}")
        elif mode == _MODE_WATCH:
            filt = await _build_filter(
                channel,
                user.id,
                header,
                rows,
                cancel_event,
                intro=(
                    "**Which applicants should ping you?**\n"
                    "This is a shared sheet, so it may hold applicants for other alliances too. "
                    "Get pinged for every new row, or only the ones matching a filter (e.g. "
                    "*Requested Alliance contains your tag*)?"
                ),
                none_label="✅ Every new applicant on the sheet",
                build_label="🔎 Only ones matching a filter",
            )
            if filt == "CANCEL":
                return
            if filt == "TIMEOUT":
                await channel.send(_TIMEOUT_MSG)
                return
            config.update_transfer_config_field(
                guild_id, "notification_filter_json", json.dumps(filt) if filt else ""
            )
            await channel.send(f"✅ Filter: {transfer.describe_filter(filt)}")

        # ── Optional intake sources ───────────────────────────────────────────
        # A form feeds modes 1 + 2; an extra shared sheet feeds mode 2.
        if mode in (_MODE_SOURCE_TO_OWN, _MODE_OWN):
            fm = await _run_source_step(
                channel,
                guild_id,
                user.id,
                step_label="Your own form (optional)",
                intro_text=(
                    "If your alliance runs its own Google Form, the bot can copy each new response "
                    "into your sheet. Only add this if you want the bot to copy responses over: if "
                    "your form already writes straight into this sheet, skip it and the bot will "
                    "see those rows on its own."
                ),
                prefix="alliance_form",
                current=current,
                cancel_event=cancel_event,
            )
            if fm == "CANCEL":
                return
            if fm == "TIMEOUT":
                await channel.send(_TIMEOUT_MSG)
                return

        if mode == _MODE_OWN:
            sw = await _run_source_step(
                channel,
                guild_id,
                user.id,
                step_label="Pull from a shared sheet (optional)",
                intro_text=(
                    "Also pull matching applicants in from a server-wide / shared sheet (read-only: "
                    "the bot copies from it, never edits it). Skip if you only track your own sheet."
                ),
                prefix="server_wide",
                current=current,
                cancel_event=cancel_event,
            )
            if sw == "CANCEL":
                return
            if sw == "TIMEOUT":
                await channel.send(_TIMEOUT_MSG)
                return

        # ── Templates (all modes) ─────────────────────────────────────────────
        if await _step_templates(channel, guild_id, user.id, current, cancel_event) != "OK":
            return  # cancel/timeout already messaged

        # ── Removal + write-back (writable modes only) ────────────────────────
        if mode != _MODE_WATCH:
            rm = await _step_removal(channel, guild_id, user.id, cancel_event)
            if rm == "CANCEL":
                return
            if rm == "TIMEOUT":
                await channel.send(_TIMEOUT_MSG)
                return

            status_cols = transfer.parse_column_map(
                config.get_transfer_config(guild_id).get("alliance_column_map_json")
            ).get("status", [])
            if status_cols:
                wb = await _step_writeback(
                    channel, guild_id, user.id, current, cancel_event, status_cols
                )
                if wb in ("CANCEL", "ABORT"):
                    return
                if wb == "TIMEOUT":
                    await channel.send(_TIMEOUT_MSG)
                    return

        # ── Finish: silent baseline (seeds any source backlog) + go live ──────
        await channel.send("⏳ Capturing your current applicants as a baseline…")
        try:
            baseline_count = await _finalize_and_enable(guild_id, sheet_id, tab, column_map)
        except Exception as e:  # noqa: BLE001
            logger.warning("[TRANSFER] baseline read failed for guild %s: %s", guild_id, e)
            await channel.send(
                f"⚠️ Saved your settings, but couldn't read the sheet for the baseline: "
                f"{config.describe_sheet_error(e)}\n"
                "The watcher is **not active yet** — re-run setup once the sheet is reachable."
            )
            return

        status_line = (
            "new applicants" if mode == _MODE_WATCH else "new applicants and status changes"
        )
        await channel.send(
            "🎉 **Transfer Management is live!**\n"
            f"Captured **{baseline_count}** current applicant(s) as your baseline, so no flood. "
            f"From now on, {status_line} post to <#{chan_id}>.\n\n"
            "Re-run setup any time to change your sheets, filters, templates, or extras."
        )
    finally:
        wizard_registry.unregister(user.id, cancel_event)


async def _finalize_and_enable(guild_id: int, sheet_id: str, tab: str, column_map: dict) -> int:
    """Pull any current source backlog into the tracked sheet *silently*, then
    bookmark every current row as the baseline (so existing applicants don't
    flood the channel), persist that state, and flip the watcher on. Returns
    the baseline applicant count.

    The source seed means a two-sheet (or form-fed) setup doesn't dump its
    whole matching backlog as new-applicant pings on the first poll — the rows
    land in the sheet here and the baseline read below bookmarks them. A no-op
    when no sources are enabled (own / watch modes)."""
    from datetime import datetime, timezone

    cfg = config.get_transfer_config(guild_id)
    header, rows = await asyncio.to_thread(transfer_sheets.read_sheet, sheet_id, tab)
    try:
        from transfer_cog import copy_sources

        copied = await copy_sources(cfg, header)
    except Exception as e:  # noqa: BLE001
        logger.warning("[TRANSFER] guild %s: go-live source seed failed: %s", guild_id, e)
        copied = 0
    if copied:
        header, rows = await asyncio.to_thread(transfer_sheets.read_sheet, sheet_id, tab)

    hidx = transfer.header_index(header)
    diff = transfer.compute_poll_diff(rows, hidx, column_map, prior_state={}, baseline=True)
    config.update_transfer_config_fields(
        guild_id,
        enabled=1,
        last_seen_state_json=json.dumps(diff.next_state),
        last_polled_at=datetime.now(timezone.utc).isoformat(),
    )
    return len(diff.next_state)

"""Transfer Management (#16) — the Setup Transfers wizard.

Reachable from both the `/setup` hub (🔁 Transfers button) and the
`/transfers` hub (⚙️ Setup Transfers). Premium-only. Lives in its own
module rather than bloating ``setup_cog.py`` (mirrors
``member_roster.run_member_roster_setup``); reuses the shared wizard
helpers (`ask_proceed_with_existing_config`, cancel registry, premium gate).

Build status: the spine is complete — sheet → column mapping → notification
channel → style → removal toggle → templates → silent baseline read that
flips the watcher live. Each step saves incrementally, so a bail-out mid-way
leaves a coherent (still-disabled) config and a re-run resumes. Still to
layer on (all re-entrant, all optional): the new-applicant filter builder,
the server-wide / intake-form sources, and decision write-back. The poll
loop that consumes this config lives in ``transfer_cog.py``.
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
            options=_header_options(self.headers, [self.sel_name] if self.sel_name else []),
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


# ── Step 3: notification channel ──────────────────────────────────────────────


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


# ── Step 4: notification style ────────────────────────────────────────────────


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


# ── Step 5: new-applicant filter builder ──────────────────────────────────────

# Comparison label → operator for the numeric-column control.
_FILTER_OPS = [("≥", ">="), ("≤", "<="), ("=", "=="), (">", ">"), ("<", "<")]


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
        await channel.send(f"**{col}** — comparison?", view=opv)
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

    if kind == "choice":
        mv = _FilterMultiView(owner_id, distinct)
        await channel.send(f"**{col}** — match which value(s)?", view=mv)
        await wizard_registry.wait_view_or_cancel(mv, cancel_event)
        if mv.cancelled:
            return "CANCEL"
        if not mv.confirmed:
            return "TIMEOUT"
        return {"column": col, "op": "in", "value": mv.values}

    # free text → contains
    valv = _FilterValueView(owner_id, prompt_title=col, prompt_label="Contains text")
    valv.message = await channel.send(
        f"**{col}** — match rows that *contain* what text?", view=valv
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
    for both the new-applicant notification filter and the source pull
    filter — the labels/intro adapt via the keyword args."""
    if intro is None:
        intro = (
            "**Step 5 — New-applicant filter**\n"
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


# ── Steps 8–9: optional source sheets (server-wide / intake form) ─────────────


class _SourceMapView(discord.ui.View):
    """A lighter mapping for a *source* sheet: just Name (required) + any
    Also-identity columns, used to dedup which rows get copied. Display /
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
            placeholder="Name column (required)",
            min_values=1,
            max_values=1,
            row=0,
            options=_header_options(self.headers, [self.sel_name] if self.sel_name else []),
        )
        name_sel.callback = self._on_name
        self.add_item(name_sel)

        id_opts = _header_options(self.headers, self.sel_identity)
        id_sel = discord.ui.Select(
            placeholder="Also-identity columns, e.g. Server (optional)",
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
        title="🔁 Source sheet — identity",
        color=discord.Color.blurple(),
        description=(
            "Pick the **Name** column (and any **Also-identity** like Server) so the bot can "
            "tell applicants apart and not copy the same person twice. The *whole* matching row "
            "gets copied into your sheet.\n\n" + transfer.summarize_column_map(column_map)
        ),
    )


async def _run_source_step(
    channel, guild_id, user_id, *, step_label, intro_text, prefix, current, cancel_event
):
    """Configure one optional source (``server_wide`` / ``alliance_form``).
    Returns ``"OK"`` / ``"SKIP"`` / ``"CANCEL"`` / ``"TIMEOUT"`` and saves the
    ``{prefix}_*`` config fields."""
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

    existing = (
        transfer.parse_column_map(current.get(f"{prefix}_column_map_json"))
        if current.get(f"{prefix}_sheet_id")
        else {}
    )
    initial = existing or transfer.suggest_column_map(s_header)
    map_view = _SourceMapView(owner_id=user_id, headers=s_header, initial_map=initial)
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
        s_header,
        s_rows,
        cancel_event,
        intro=(
            "**Which rows should the bot pull in?**\n"
            "For example: only rows where *Requested Landing Alliance* contains your tag."
        ),
        none_label="📥 Pull every row",
        build_label="🔎 Only rows matching a filter",
    )
    if filt == "CANCEL":
        return "CANCEL"
    if filt == "TIMEOUT":
        return "TIMEOUT"

    config.update_transfer_config_fields(
        guild_id,
        **{
            f"{prefix}_enabled": 1,
            f"{prefix}_sheet_id": s_id,
            f"{prefix}_sheet_tab": s_tab,
            f"{prefix}_column_map_json": json.dumps(src_map),
            f"{prefix}_filter_json": json.dumps(filt) if filt else "",
        },
    )
    await channel.send(
        f"✅ Connected. Rows matching **{transfer.describe_filter(filt)}** will be copied into "
        "your sheet (whole row, aligned to your columns)."
    )
    return "OK"


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
        sheet_id, tab, header, rows = sheet_view.result

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
        await channel.send(f"✅ **Mapping saved.**\n{transfer.summarize_column_map(column_map)}")

        # ── Step 3: notification channel ──────────────────────────────────────
        chan_view = _ChannelStepView(owner_id=user.id)
        chan_view.message = await channel.send(
            "**Step 3 — Notification channel**\n"
            "Where should new-applicant and status-change notices post? A dedicated "
            "recruiting channel works well.",
            view=chan_view,
        )
        await wizard_registry.wait_view_or_cancel(chan_view, cancel_event)
        if chan_view.cancelled:
            return
        if not chan_view.confirmed or not chan_view.selected_id:
            await channel.send(_TIMEOUT_MSG)
            return
        config.update_transfer_config_field(
            guild_id, "notification_channel_id", chan_view.selected_id
        )

        # ── Step 4: notification style ────────────────────────────────────────
        style_view = _StyleStepView(owner_id=user.id)
        await channel.send(
            "**Step 4 — How should notifications arrive?**\n"
            "• **A message per applicant** — richest; great for a dedicated channel where you "
            "watch people arrive.\n"
            "• **One digest** — a single batched message when several land in the same check "
            "(tidier on a busy day).",
            view=style_view,
        )
        await wizard_registry.wait_view_or_cancel(style_view, cancel_event)
        if style_view.cancelled:
            return
        if not style_view.confirmed or not style_view.style:
            await channel.send(_TIMEOUT_MSG)
            return
        config.update_transfer_config_field(guild_id, "notification_style", style_view.style)

        # ── Step 5: new-applicant filter ──────────────────────────────────────
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

        # ── Step 6: removal notices (opt-in) ──────────────────────────────────
        from setup_cog import YesNoView

        removal_view = YesNoView()
        await channel.send(
            "**Step 6 — Removal notices?**\n"
            "When someone who'd been marked (Confirmed / Declined / …) is *removed* from your "
            "sheet, want a heads-up? Pending rows that never had a status set never notify.",
            view=removal_view,
        )
        await wizard_registry.wait_view_or_cancel(removal_view, cancel_event)
        if removal_view.cancelled:
            return
        if removal_view.selected is None:
            await channel.send(_TIMEOUT_MSG)
            return
        config.update_transfer_config_field(
            guild_id, "notify_on_delete", 1 if removal_view.selected else 0
        )

        # ── Step 6: message templates ─────────────────────────────────────────
        from setup_cog import ask_keep_or_change
        from defaults import DEFAULT_TRANSFER_TEMPLATES

        await channel.send(
            "**Step 7 — In-game message templates**\n"
            "These are the messages you copy into game chat. Use `{name}` for the applicant, "
            "`{alliance_name}` for your alliance, or any display column as `{token}` "
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
                return  # cancelled / timed out (ask_keep_or_change posts its own notice)
            # Empty string == "use the default" (storage convention).
            stored = "" if chosen.strip() == default_body.strip() else chosen
            config.update_transfer_config_field(guild_id, f"template_{kind}", stored)

        # ── Step 8: server-wide pull (optional) ───────────────────────────────
        sw = await _run_source_step(
            channel,
            guild_id,
            user.id,
            step_label="Step 8 — Server-wide pull (optional)",
            intro_text=(
                "Pull applicants who fit from a server-wide sheet into your own, automatically "
                "(the highest-leverage piece — it catches people who asked for you). Skip if you "
                "only watch your own sheet."
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

        # ── Step 9: intake form (optional) ────────────────────────────────────
        fm = await _run_source_step(
            channel,
            guild_id,
            user.id,
            step_label="Step 9 — Intake form (optional)",
            intro_text=(
                "If your alliance runs its own Google Form, pull each submission into your sheet. "
                "Skip if you don't."
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

        # ── Step 10: decision write-back (optional; needs status columns) ─────
        status_cols = transfer.parse_column_map(
            config.get_transfer_config(guild_id).get("alliance_column_map_json")
        ).get("status", [])
        if status_cols:
            wb_choice = YesNoView()
            await channel.send(
                "**Step 10 — Decision write-back?**\n"
                f"Add buttons to each notification so you can mark an applicant "
                f"(**{', '.join(status_cols)}**) right from Discord — the bot writes it back to "
                "your sheet. Leave off to keep the bot read-only.",
                view=wb_choice,
            )
            await wizard_registry.wait_view_or_cancel(wb_choice, cancel_event)
            if wb_choice.cancelled:
                return
            if wb_choice.selected is None:
                await channel.send(_TIMEOUT_MSG)
                return
            if wb_choice.selected:
                wb_value = await ask_keep_or_change(
                    channel,
                    "**Write-back value**\n"
                    "What should the bot write when you click a Set button? Google Sheets "
                    "checkboxes use **TRUE** (so the box ticks); use your own word if your sheet "
                    "expects e.g. `Yes`.",
                    default="TRUE",
                    current=current.get("writeback_value", ""),
                    modal_title="Write-back value",
                    modal_label="Value to write",
                    cancel_event=cancel_event,
                )
                if wb_value is None:
                    return
                config.update_transfer_config_fields(
                    guild_id, writeback_enabled=1, writeback_value=wb_value or "TRUE"
                )
            else:
                config.update_transfer_config_field(guild_id, "writeback_enabled", 0)

        # ── Finish: silent baseline read + go live ────────────────────────────
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

        ch_mention = f"<#{chan_view.selected_id}>"
        await channel.send(
            "🎉 **Transfer Management is live!**\n"
            f"Captured **{baseline_count}** current applicant(s) as your baseline — no flood. "
            f"From now on, new applicants and status changes post to {ch_mention}.\n\n"
            "Optional extras you can add by re-running setup later: a new-applicant filter, "
            "server-wide / intake-form pulls, and decision write-back."
        )
    finally:
        wizard_registry.unregister(user.id, cancel_event)


async def _finalize_and_enable(guild_id: int, sheet_id: str, tab: str, column_map: dict) -> int:
    """Read the alliance sheet, bookmark every current row as the baseline
    (so existing applicants don't flood the channel), persist that state, and
    flip the watcher on. Returns the baseline applicant count."""
    from datetime import datetime, timezone

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

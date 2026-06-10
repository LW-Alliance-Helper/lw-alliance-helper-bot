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


# ── Paged column pickers ──────────────────────────────────────────────────────
#
# A select tops out at 25 options, but a recruiting / server-wide sheet can have
# far more columns. The pickers below page through the headers in 25-wide
# windows with ◀ ▶ buttons. Options are valued by *global* column index so a
# selection on one page stays addressable after a flip; multi-select state is
# kept as a set of global indices and merged per page (see _merge_page_selection)
# so picks on other pages survive.

_PER_PAGE = 25

# Inert one-option list a select is built with before _render() points it at the
# current page (a select needs ≥1 option at construction; "-1" maps to nothing).
_PLACEHOLDER_OPTS = [discord.SelectOption(label="…", value="-1")]


def _page_count(headers: list, per_page: int = _PER_PAGE) -> int:
    """How many 25-column pages it takes to show every header (at least 1)."""
    return max(1, (len(headers) + per_page - 1) // per_page)


def _page_index_set(headers: list, page: int, per_page: int = _PER_PAGE) -> set:
    """Global indices of the non-blank columns shown on ``page``."""
    start = page * per_page
    return {start + o for o, h in enumerate(headers[start : start + per_page]) if str(h).strip()}


def _page_options(headers: list, page: int, selected_idx, per_page: int = _PER_PAGE) -> list:
    """SelectOptions for one page, valued by *global* column index. Blank
    headers are skipped; ``selected_idx`` (global indices) drives ``default``.
    A page with no named columns falls back to one inert option (``"-1"``) so
    the select still has the ≥1 option Discord requires."""
    start = page * per_page
    opts = []
    for o, h in enumerate(headers[start : start + per_page]):
        text = str(h).strip()
        if not text:
            continue
        gi = start + o
        opts.append(
            discord.SelectOption(label=text[:100], value=str(gi), default=(gi in selected_idx))
        )
    if not opts:
        opts = [discord.SelectOption(label="(no named columns on this page)", value="-1")]
    return opts


def _selected_indices(values) -> set:
    """Parse a select's submitted values into a set of real global indices
    (dropping the ``-1`` inert-placeholder sentinel)."""
    out = set()
    for v in values:
        try:
            gi = int(v)
        except (TypeError, ValueError):
            continue
        if gi >= 0:
            out.add(gi)
    return out


def _merge_page_selection(old_idx, on_page: set, values) -> set:
    """Merge a multi-select's current-page picks into cross-page state: drop
    every index that lives on this page, then add the ones now selected — so
    picks made on other pages survive the flip."""
    return (set(old_idx) - on_page) | _selected_indices(values)


def _idx_for_headers(headers: list, wanted) -> list:
    """Global indices (first match) for a list of header texts, dropping any
    that no longer resolve. Seeds index-based selection from a saved
    header-name column map."""
    norm_to_idx: dict = {}
    for i, h in enumerate(headers):
        key = transfer._norm_header(h)
        if key and key not in norm_to_idx:
            norm_to_idx[key] = i
    out = []
    for w in wanted or []:
        i = norm_to_idx.get(transfer._norm_header(w))
        if i is not None:
            out.append(i)
    return out


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
        # Sentence-fitting noun phrase per role; `short` is for the inline summary.
        labels = {
            "intake": "the shared (intake) sheet",
            "alliance": "the sheet you watch" if mode == _MODE_WATCH else "your sheet",
        }
        short = {
            "intake": "Shared sheet",
            "alliance": "Watched sheet" if mode == _MODE_WATCH else "Your sheet",
        }
        read: dict = {}
        for role, sid, tab in sheets:
            label = labels.get(role, role)
            sid = transfer.extract_sheet_id(sid)
            try:
                header, rows = await asyncio.to_thread(transfer_sheets.read_sheet, sid, tab)
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(
                    f"⚠️ Couldn't read {label} (tab “{tab}”): {config.describe_sheet_error(e)}\n"
                    "Check the Sheet ID + tab and that the bot's service account has access, "
                    "then click the button again.",
                    ephemeral=True,
                )
                return
            if not header:
                await interaction.followup.send(
                    f"⚠️ {label[:1].upper() + label[1:]} has no header row (tab “{tab}”). "
                    "Put your column headers in row 1, then click the button again.",
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
                summary = " · ".join(f"{short.get(r, r)}: {len(read[r][2])} cols" for r in read)
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
    track changes on). Wide sheets page through their columns with ◀ ▶, and
    selections persist across pages (state is kept as global column indices)."""

    def __init__(self, *, owner_id: int, headers: list, initial_map: dict, include_status=True):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.all_headers = headers
        self.message: discord.Message | None = None
        self.saved = False
        self.include_status = include_status
        self.per_page = _PER_PAGE
        self.page = 0
        self.pages = _page_count(headers, self.per_page)

        # Selection state as *global* column indices (survives page flips).
        nm = _idx_for_headers(headers, [initial_map.get("name")] if initial_map.get("name") else [])
        self.name_idx = nm[0] if nm else None
        self.status_idx = set(_idx_for_headers(headers, initial_map.get("status") or []))
        self.display_idx = set(_idx_for_headers(headers, initial_map.get("display") or []))
        self.identity_idx = set(_idx_for_headers(headers, initial_map.get("identity_extra") or []))

        # Name is min_values=0 (so a flip to a page without it doesn't force a
        # wrong pick); it's validated as required on Save instead.
        self._name_sel = discord.ui.Select(
            placeholder="① Name column (required)",
            min_values=0,
            max_values=1,
            row=0,
            options=_PLACEHOLDER_OPTS,
        )
        self._name_sel.callback = self._on_name
        self.add_item(self._name_sel)

        row = 1
        self._status_sel = None
        if include_status:
            self._status_sel = self._make_multi(
                "② Status columns to watch (optional)", row, self._on_status
            )
            row += 1
            disp_num, id_num = "③", "④"
        else:
            disp_num, id_num = "②", "③"
        self._display_sel = self._make_multi(
            f"{disp_num} Columns to show in notices (optional)", row, self._on_display
        )
        row += 1
        self._identity_sel = self._make_multi(
            f"{id_num} Identity Fallback, e.g. Server (optional)", row, self._on_identity
        )

        self._prev = self._next = None
        if self.pages > 1:
            self._prev = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary, row=4
            )
            self._prev.callback = self._on_prev
            self.add_item(self._prev)
            self._next = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary, row=4
            )
            self._next.callback = self._on_next
            self.add_item(self._next)
        save = discord.ui.Button(label="✅ Save mapping", style=discord.ButtonStyle.success, row=4)
        save.callback = self._on_save
        self.add_item(save)

        self._render()

    def _make_multi(self, placeholder: str, row: int, callback):
        sel = discord.ui.Select(
            placeholder=placeholder, min_values=0, max_values=1, row=row, options=_PLACEHOLDER_OPTS
        )
        sel.callback = callback
        self.add_item(sel)
        return sel

    def _render(self):
        """Re-point every select at the current page's options + update nav."""
        self._name_sel.options = _page_options(
            self.all_headers, self.page, {self.name_idx} if self.name_idx is not None else set()
        )
        for sel, state in (
            (self._status_sel, self.status_idx),
            (self._display_sel, self.display_idx),
            (self._identity_sel, self.identity_idx),
        ):
            if sel is None:
                continue
            opts = _page_options(self.all_headers, self.page, state)
            sel.options = opts
            sel.max_values = max(1, len(opts))
        if self._prev is not None:
            self._prev.disabled = self.page <= 0
            self._next.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _on_name(self, interaction: discord.Interaction):
        picked = _selected_indices(interaction.data["values"])
        if picked:
            self.name_idx = sorted(picked)[0]
        elif self.name_idx in _page_index_set(self.all_headers, self.page, self.per_page):
            self.name_idx = None  # deliberately cleared on its own page
        await interaction.response.defer()

    async def _on_status(self, interaction: discord.Interaction):
        on_page = _page_index_set(self.all_headers, self.page, self.per_page)
        self.status_idx = _merge_page_selection(
            self.status_idx, on_page, interaction.data["values"]
        )
        await interaction.response.defer()

    async def _on_display(self, interaction: discord.Interaction):
        on_page = _page_index_set(self.all_headers, self.page, self.per_page)
        self.display_idx = _merge_page_selection(
            self.display_idx, on_page, interaction.data["values"]
        )
        await interaction.response.defer()

    async def _on_identity(self, interaction: discord.Interaction):
        on_page = _page_index_set(self.all_headers, self.page, self.per_page)
        self.identity_idx = _merge_page_selection(
            self.identity_idx, on_page, interaction.data["values"]
        )
        await interaction.response.defer()

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._render()
        await wizard_registry.safe_edit_response(interaction, view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page = min(self.pages - 1, self.page + 1)
        self._render()
        await wizard_registry.safe_edit_response(interaction, view=self)

    def _headers_for(self, idx_set) -> list:
        return [self.all_headers[i] for i in sorted(idx_set) if 0 <= i < len(self.all_headers)]

    def column_map(self) -> dict:
        name = (
            self.all_headers[self.name_idx]
            if (self.name_idx is not None and 0 <= self.name_idx < len(self.all_headers))
            else None
        )
        out: dict = {"name": name}
        if self.identity_idx:
            out["identity_extra"] = self._headers_for(self.identity_idx)
        if self.include_status and self.status_idx:
            out["status"] = self._headers_for(self.status_idx)
        if self.display_idx:
            out["display"] = self._headers_for(self.display_idx)
        return out

    async def _on_save(self, interaction: discord.Interaction):
        if self.name_idx is None:
            extra = " Use ◀ ▶ to find it if it's on another page." if self.pages > 1 else ""
            await interaction.response.send_message(
                f"⚠️ Pick a **Name** column. It's the one required field.{extra}", ephemeral=True
            )
            return
        self.saved = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


def _map_embed(
    column_map: dict, multipage: bool, include_status: bool = True, total: int = 0
) -> discord.Embed:
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
    if multipage:
        embed.set_footer(
            text=f"This sheet has {total} columns — use ◀ ▶ under the dropdowns to page "
            "through all of them. Your picks are kept across pages."
        )
    return embed


class _AdaptiveColumnMapView(discord.ui.View):
    """Wide-sheet column mapper. A hub (summary + one button per field) that
    morphs *in place* into a single focused, paged picker for one field at a
    time — so ◀ ▶ only ever moves the field you're editing, never all four at
    once (the jarring shared-pager problem). Selection state is global column
    indices, so it survives paging and switching fields. One message throughout,
    edited via ``safe_edit_response``."""

    _FIELDS = {
        "name": ("Name", "the column that identifies each applicant (required)"),
        "status": ("Status", "a change here posts a status-change notice"),
        "display": ("Display", "the columns shown in each notice"),
        "identity": ("Identity Fallback", "tells apart two people with the same name"),
    }

    def __init__(self, *, owner_id: int, headers: list, initial_map: dict, include_status=True):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.all_headers = headers
        self.include_status = include_status
        self.per_page = _PER_PAGE
        self.pages = _page_count(headers, self.per_page)
        self.message: discord.Message | None = None
        self.saved = False
        self.mode = "hub"  # "hub" | "name" | "status" | "display" | "identity"
        self.page = 0
        self._warn = ""

        nm = _idx_for_headers(headers, [initial_map.get("name")] if initial_map.get("name") else [])
        self.name_idx = nm[0] if nm else None
        self.status_idx = set(_idx_for_headers(headers, initial_map.get("status") or []))
        self.display_idx = set(_idx_for_headers(headers, initial_map.get("display") or []))
        self.identity_idx = set(_idx_for_headers(headers, initial_map.get("identity_extra") or []))

        self._render_hub()

    # ── state ────────────────────────────────────────────────────────────────
    def _field_order(self) -> list:
        fields = ["name"]
        if self.include_status:
            fields.append("status")
        fields += ["display", "identity"]
        return fields

    def _state_for(self, field) -> set:
        if field == "name":
            return {self.name_idx} if self.name_idx is not None else set()
        return {
            "status": self.status_idx,
            "display": self.display_idx,
            "identity": self.identity_idx,
        }[field]

    def _set_state(self, field, value):
        if field == "status":
            self.status_idx = value
        elif field == "display":
            self.display_idx = value
        elif field == "identity":
            self.identity_idx = value

    def _headers_for(self, idx_set) -> list:
        return [self.all_headers[i] for i in sorted(idx_set) if 0 <= i < len(self.all_headers)]

    def column_map(self) -> dict:
        name = (
            self.all_headers[self.name_idx]
            if (self.name_idx is not None and 0 <= self.name_idx < len(self.all_headers))
            else None
        )
        out: dict = {"name": name}
        if self.identity_idx:
            out["identity_extra"] = self._headers_for(self.identity_idx)
        if self.include_status and self.status_idx:
            out["status"] = self._headers_for(self.status_idx)
        if self.display_idx:
            out["display"] = self._headers_for(self.display_idx)
        return out

    # ── rendering ────────────────────────────────────────────────────────────
    def _render_hub(self):
        self.mode = "hub"
        self.clear_items()
        for field in self._field_order():
            mark = "✅" if self._state_for(field) else "▫️"
            btn = discord.ui.Button(
                label=f"{mark} {self._FIELDS[field][0]}", style=discord.ButtonStyle.secondary, row=0
            )
            btn.callback = self._make_edit(field)
            self.add_item(btn)
        save = discord.ui.Button(label="✅ Save mapping", style=discord.ButtonStyle.success, row=1)
        save.callback = self._on_save
        self.add_item(save)

    def _render_field(self, field):
        self.mode = field
        self.clear_items()
        is_name = field == "name"
        opts = _page_options(self.all_headers, self.page, self._state_for(field))
        sel = discord.ui.Select(
            placeholder=f"Pick the {self._FIELDS[field][0]} column" + ("" if is_name else "(s)"),
            min_values=0,
            max_values=1 if is_name else max(1, len(opts)),
            row=0,
            options=opts,
        )
        sel.callback = self._on_field_select
        self.add_item(sel)
        if self.pages > 1:
            prev = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary, row=1, disabled=self.page <= 0
            )
            prev.callback = self._on_prev
            self.add_item(prev)
            nxt = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=self.page >= self.pages - 1,
            )
            nxt.callback = self._on_next
            self.add_item(nxt)
        done = discord.ui.Button(label="✅ Done", style=discord.ButtonStyle.primary, row=1)
        done.callback = self._on_field_done
        self.add_item(done)

    def render_embed(self) -> discord.Embed:
        lines = [f"This sheet has **{len(self.all_headers)}** columns, so set one field at a time."]
        if self.mode == "hub":
            lines.append("Tap a field below to choose its column(s), then **Save mapping**.")
        else:
            label, desc = self._FIELDS[self.mode]
            lines.append(f"**Editing {label}:** {desc}.")
            if self.pages > 1:
                lines.append(
                    f"Page **{self.page + 1} of {self.pages}** — ◀ ▶ shows more columns. "
                    "**Done** goes back to the field list."
                )
        body = "\n".join(lines) + "\n\n" + transfer.summarize_column_map(self.column_map())
        if self._warn:
            body += f"\n\n{self._warn}"
        return discord.Embed(
            title="🔁 Map your columns", color=discord.Color.blurple(), description=body
        )

    # ── interactions ─────────────────────────────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    def _make_edit(self, field):
        async def _cb(interaction: discord.Interaction):
            self.page = 0
            self._warn = ""
            self._render_field(field)
            await wizard_registry.safe_edit_response(
                interaction, embed=self.render_embed(), view=self
            )

        return _cb

    async def _on_field_select(self, interaction: discord.Interaction):
        field = self.mode
        if field == "name":
            picked = _selected_indices(interaction.data["values"])
            if picked:
                self.name_idx = sorted(picked)[0]
            elif self.name_idx in _page_index_set(self.all_headers, self.page, self.per_page):
                self.name_idx = None
        else:
            on_page = _page_index_set(self.all_headers, self.page, self.per_page)
            self._set_state(
                field,
                _merge_page_selection(self._state_for(field), on_page, interaction.data["values"]),
            )
        await interaction.response.defer()

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._render_field(self.mode)
        await wizard_registry.safe_edit_response(interaction, embed=self.render_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page = min(self.pages - 1, self.page + 1)
        self._render_field(self.mode)
        await wizard_registry.safe_edit_response(interaction, embed=self.render_embed(), view=self)

    async def _on_field_done(self, interaction: discord.Interaction):
        self.page = 0
        self._render_hub()
        await wizard_registry.safe_edit_response(interaction, embed=self.render_embed(), view=self)

    async def _on_save(self, interaction: discord.Interaction):
        if self.name_idx is None:
            self._warn = (
                "⚠️ Pick a **Name** column first (open ▫️ Name) — it's the one required field."
            )
            await wizard_registry.safe_edit_response(
                interaction, embed=self.render_embed(), view=self
            )
            return
        self.saved = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, embed=self.render_embed(), view=self)
        self.stop()


async def _map_step(
    channel, guild_id, owner_id, header, saved_map, *, include_status, cancel_event
):
    """Run the column-mapping step against ``header``, save it to
    ``alliance_column_map_json``, and return the column map (or a
    ``"CANCEL"`` / ``"TIMEOUT"`` sentinel). Narrow sheets (≤25 columns) use the
    all-at-once view; wide sheets use the adaptive one-field-at-a-time view so
    paging never disturbs a field you aren't editing."""
    initial_map = saved_map or transfer.suggest_column_map(header)
    if not include_status:
        initial_map = {k: v for k, v in initial_map.items() if k != "status"}
    if _page_count(header) > 1:
        view = _AdaptiveColumnMapView(
            owner_id=owner_id,
            headers=header,
            initial_map=initial_map,
            include_status=include_status,
        )
        view.message = await channel.send(embed=view.render_embed(), view=view)
    else:
        view = _ColumnMapView(
            owner_id=owner_id,
            headers=header,
            initial_map=initial_map,
            include_status=include_status,
        )
        view.message = await channel.send(
            embed=_map_embed(initial_map, False, include_status=include_status, total=len(header)),
            view=view,
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
    """Single-select of the sheet's columns to filter on, paged for wide
    sheets (◀ ▶). Picking a column confirms and stops."""

    def __init__(self, owner_id: int, header: list):
        super().__init__(timeout=_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.all_headers = header
        self.column = None
        self.confirmed = False
        self.per_page = _PER_PAGE
        self.page = 0
        self.pages = _page_count(header, self.per_page)
        self._sel = discord.ui.Select(
            placeholder="Pick a column to filter on",
            min_values=0,
            max_values=1,
            options=_PLACEHOLDER_OPTS,
        )
        self._sel.callback = self._cb
        self.add_item(self._sel)
        self._prev = self._next = None
        if self.pages > 1:
            self._prev = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary, row=1
            )
            self._prev.callback = self._on_prev
            self.add_item(self._prev)
            self._next = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary, row=1
            )
            self._next.callback = self._on_next
            self.add_item(self._next)
        self._render()

    def _render(self):
        self._sel.options = _page_options(self.all_headers, self.page, set())
        if self._prev is not None:
            self._prev.disabled = self.page <= 0
            self._next.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _cb(self, interaction: discord.Interaction):
        picked = _selected_indices(interaction.data["values"])
        if not picked:  # the inert placeholder on an all-blank page
            await interaction.response.defer()
            return
        self.column = self.all_headers[sorted(picked)[0]]
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._render()
        await wizard_registry.safe_edit_response(interaction, view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page = min(self.pages - 1, self.page + 1)
        self._render()
        await wizard_registry.safe_edit_response(interaction, view=self)


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
        self.all_headers = headers
        self.saved = False
        self.per_page = _PER_PAGE
        self.page = 0
        self.pages = _page_count(headers, self.per_page)

        nm = _idx_for_headers(headers, [initial_map.get("name")] if initial_map.get("name") else [])
        self.name_idx = nm[0] if nm else None
        self.identity_idx = set(_idx_for_headers(headers, initial_map.get("identity_extra") or []))

        self._name_sel = discord.ui.Select(
            placeholder="① Name column (required)",
            min_values=0,
            max_values=1,
            row=0,
            options=_PLACEHOLDER_OPTS,
        )
        self._name_sel.callback = self._on_name
        self.add_item(self._name_sel)

        self._id_sel = discord.ui.Select(
            placeholder="② Identity Fallback, e.g. Server (optional)",
            min_values=0,
            max_values=1,
            row=1,
            options=_PLACEHOLDER_OPTS,
        )
        self._id_sel.callback = self._on_identity
        self.add_item(self._id_sel)

        self._prev = self._next = None
        if self.pages > 1:
            self._prev = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary, row=2
            )
            self._prev.callback = self._on_prev
            self.add_item(self._prev)
            self._next = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary, row=2
            )
            self._next.callback = self._on_next
            self.add_item(self._next)
        save = discord.ui.Button(label="✅ Save", style=discord.ButtonStyle.success, row=2)
        save.callback = self._on_save
        self.add_item(save)

        self._render()

    def _render(self):
        self._name_sel.options = _page_options(
            self.all_headers, self.page, {self.name_idx} if self.name_idx is not None else set()
        )
        opts = _page_options(self.all_headers, self.page, self.identity_idx)
        self._id_sel.options = opts
        self._id_sel.max_values = max(1, len(opts))
        if self._prev is not None:
            self._prev.disabled = self.page <= 0
            self._next.disabled = self.page >= self.pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _on_name(self, interaction: discord.Interaction):
        picked = _selected_indices(interaction.data["values"])
        if picked:
            self.name_idx = sorted(picked)[0]
        elif self.name_idx in _page_index_set(self.all_headers, self.page, self.per_page):
            self.name_idx = None
        await interaction.response.defer()

    async def _on_identity(self, interaction: discord.Interaction):
        on_page = _page_index_set(self.all_headers, self.page, self.per_page)
        self.identity_idx = _merge_page_selection(
            self.identity_idx, on_page, interaction.data["values"]
        )
        await interaction.response.defer()

    async def _on_prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._render()
        await wizard_registry.safe_edit_response(interaction, view=self)

    async def _on_next(self, interaction: discord.Interaction):
        self.page = min(self.pages - 1, self.page + 1)
        self._render()
        await wizard_registry.safe_edit_response(interaction, view=self)

    def column_map(self) -> dict:
        name = (
            self.all_headers[self.name_idx]
            if (self.name_idx is not None and 0 <= self.name_idx < len(self.all_headers))
            else None
        )
        out: dict = {"name": name}
        if self.identity_idx:
            out["identity_extra"] = [
                self.all_headers[i]
                for i in sorted(self.identity_idx)
                if 0 <= i < len(self.all_headers)
            ]
        return out

    async def _on_save(self, interaction: discord.Interaction):
        if self.name_idx is None:
            extra = " Use ◀ ▶ to find it if it's on another page." if self.pages > 1 else ""
            await interaction.response.send_message(
                "⚠️ Pick a **Name** column — it's required to avoid copying the same person "
                f"twice.{extra}",
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
    channel,
    guild_id,
    user_id,
    *,
    step_label,
    intro_text,
    prefix,
    current,
    cancel_event,
    required=False,
):
    """Offer one intake source (``server_wide`` / ``alliance_form``). When none
    is configured yet: Skip vs Connect. When one already exists (edit menu):
    Keep current / Replace / Remove (Remove hidden when ``required``). On
    Connect/Replace, enter a sheet then map + filter it. Returns ``"OK"``
    (added/replaced) / ``"SKIP"`` (skipped or removed) / ``"KEEP"`` (unchanged) /
    ``"CANCEL"`` / ``"TIMEOUT"`` and saves the ``{prefix}_*`` config."""
    already = bool(current.get(f"{prefix}_enabled"))
    if already:
        opts = [
            ("↩️ Keep current", "keep", discord.ButtonStyle.secondary),
            ("✏️ Replace", "connect", discord.ButtonStyle.primary),
        ]
        if not required:
            opts.append(("🗑️ Remove", "remove", discord.ButtonStyle.danger))
    else:
        opts = [
            ("⏭️ Skip", "skip", discord.ButtonStyle.secondary),
            ("➕ Connect a sheet", "connect", discord.ButtonStyle.primary),
        ]
    choice = _ButtonChoiceView(user_id, opts)
    await channel.send(f"**{step_label}**\n{intro_text}", view=choice)
    await wizard_registry.wait_view_or_cancel(choice, cancel_event)
    if choice.cancelled:
        return "CANCEL"
    if not choice.confirmed:
        return "TIMEOUT"
    if choice.value == "keep":
        await channel.send("↩️ Kept as-is.")
        return "KEEP"
    if choice.value == "skip":
        config.update_transfer_config_field(guild_id, f"{prefix}_enabled", 0)
        await channel.send("⏭️ Skipped.")
        return "SKIP"
    if choice.value == "remove":
        config.update_transfer_config_field(guild_id, f"{prefix}_enabled", 0)
        await channel.send("🗑️ Removed. The bot will stop copying from it.")
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


async def _step_writeback(channel, guild_id, owner_id, cancel_event, status_cols):
    from setup_cog import YesNoView

    view = YesNoView()
    await channel.send(
        "**Decision write-back?**\n"
        f"Add **Yes / No** buttons to each notification so you can mark an applicant "
        f"(**{', '.join(status_cols)}**) right from Discord. The bot ticks or unticks the matching "
        "checkbox on your sheet, so make those columns checkboxes in Google Sheets "
        "(Insert → Checkbox). Leave off to keep the bot read-only.",
        view=view,
    )
    await wizard_registry.wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return "CANCEL"
    if view.selected is None:
        return "TIMEOUT"
    config.update_transfer_config_field(guild_id, "writeback_enabled", 1 if view.selected else 0)
    return "OK"


# ── Wizard ────────────────────────────────────────────────────────────────────


async def run_transfer_setup(interaction: discord.Interaction, bot):
    """First-time setup walks the linear wizard; a re-entry on an already-
    configured guild opens the section edit menu instead, so leadership can
    change one thing without redoing the whole flow. The menu's 'Change sheets
    / setup type' option falls back through to the linear wizard."""
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
            if await _run_edit_menu(channel, guild_id, user, cancel_event) != "RESETUP":
                return
            # Full re-setup requested: refresh and fall through to the linear flow.
            current = config.get_transfer_config(guild_id)
            saved_map = transfer.parse_column_map(current.get("alliance_column_map_json"))

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
                wb = await _step_writeback(channel, guild_id, user.id, cancel_event, status_cols)
                if wb == "CANCEL":
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


# ── Re-entry edit menu ────────────────────────────────────────────────────────
#
# Re-running setup on a configured guild opens this section picker instead of
# re-walking the whole wizard. Each section, once clicked, still offers a
# "Keep current" escape (the gate shows the current value first — so it doubles
# as a quick "let me just look" view). The menu re-posts itself at the bottom
# after each edit so the actionable view stays the most-recent message.


class _EditMenuView(discord.ui.View):
    """Section picker. Each button records the chosen section in ``choice``;
    the menu loop runs that section's editor then re-posts. Buttons shown
    depend on the setup mode (watch hides Filter-as-notification, Intake, and
    Removal/write-back; source_to_own hides the standalone notification
    Filter — its filter lives on the intake source)."""

    def __init__(self, *, owner_id: int, mode: str):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.choice: str | None = None
        self.confirmed = False
        self.message: discord.Message | None = None

        specs = [
            ("🗂️ Column mapping", "mapping", 0),
            ("📢 Channel", "channel", 0),
            ("🎚️ Style", "style", 0),
        ]
        if mode in (_MODE_OWN, _MODE_WATCH):
            specs.append(("🔎 Filter", "filter", 1))
        if mode in (_MODE_SOURCE_TO_OWN, _MODE_OWN):
            specs.append(("📥 Intake sources", "intake", 1))
        specs.append(("✉️ Templates", "templates", 1))
        if mode != _MODE_WATCH:
            specs.append(("🗑️ Removal & write-back", "removal", 2))
        specs.append(("📑 Change sheets / setup type", "resetup", 2))
        for label, value, row in specs:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=row)
            btn.callback = self._make(value)
            self.add_item(btn)
        done = discord.ui.Button(label="✅ Done", style=discord.ButtonStyle.success, row=3)
        done.callback = self._make("done")
        self.add_item(done)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    def _make(self, value: str):
        async def _cb(interaction: discord.Interaction):
            self.choice = value
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(interaction, view=self)
            self.stop()

        return _cb


def _menu_embed(cfg: dict, mode: str) -> discord.Embed:
    column_map = transfer.parse_column_map(cfg.get("alliance_column_map_json"))
    sid = cfg.get("alliance_sheet_id") or ""
    chan = cfg.get("notification_channel_id") or 0
    style = (
        "a message per applicant"
        if (cfg.get("notification_style") or "each") == "each"
        else "a digest"
    )
    embed = discord.Embed(
        title="💎 Transfer Management — what do you want to change?",
        color=discord.Color.blurple(),
        description=(
            "Pick a section to edit. After clicking in you can still **Keep current** if you only "
            "wanted to look. Hit **Done** when you're finished."
        ),
    )
    embed.add_field(name="Setup type", value=_MODE_LABELS.get(mode, "—"), inline=False)
    embed.add_field(
        name="Tracked sheet",
        value=(
            f"`{sid[:18]}…` · tab **{cfg.get('alliance_sheet_tab') or '?'}**"
            if sid
            else "*not set*"
        ),
        inline=False,
    )
    embed.add_field(name="Columns", value=transfer.summarize_column_map(column_map), inline=False)
    if mode in (_MODE_OWN, _MODE_WATCH):
        embed.add_field(
            name="Filter",
            value=transfer.describe_filter(
                transfer.parse_filter(cfg.get("notification_filter_json"))
            ),
            inline=False,
        )
    extras = []
    if cfg.get("server_wide_enabled"):
        extras.append("shared-sheet pull ✅")
    if cfg.get("alliance_form_enabled"):
        extras.append("form pull ✅")
    if mode != _MODE_WATCH:
        extras.append(f"removal {'on' if cfg.get('notify_on_delete') else 'off'}")
        extras.append(f"write-back {'on' if cfg.get('writeback_enabled') else 'off'}")
    if extras:
        embed.add_field(name="Extras", value=" · ".join(extras), inline=False)
    embed.add_field(
        name="Notifications",
        value=f"{f'<#{chan}>' if chan else '*no channel set*'} · {style}",
        inline=False,
    )
    return embed


async def _run_edit_menu(channel, guild_id, user, cancel_event) -> str:
    """Loop the section picker. Returns ``"RESETUP"`` to ask the caller to run
    the full linear wizard (Change sheets / setup type), else ``"DONE"``."""
    msg = None
    while True:
        cfg = config.get_transfer_config(guild_id)
        mode = cfg.get("setup_mode") or ""
        view = _EditMenuView(owner_id=user.id, mode=mode)
        # Re-post at the bottom so the menu stays the most-recent message.
        if msg is not None:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        msg = await channel.send(embed=_menu_embed(cfg, mode), view=view)
        view.message = msg
        await wizard_registry.wait_view_or_cancel(view, cancel_event)
        if view.cancelled:
            return "DONE"
        if not view.confirmed or view.choice in (None, "done"):
            try:
                await msg.edit(view=None)
            except discord.HTTPException:
                pass
            if view.choice == "done":
                await channel.send("✅ Done. Your transfer setup is saved.")
            else:
                await channel.send(_TIMEOUT_MSG)
            return "DONE"
        if view.choice == "resetup":
            return "RESETUP"
        status = await _edit_section(channel, guild_id, user, cfg, cancel_event, view.choice)
        if status == "CANCEL":
            return "DONE"
        if status == "TIMEOUT":
            await channel.send(_TIMEOUT_MSG)
            return "DONE"
        # OK / KEEP → loop and re-post the menu.


async def _edit_gate(channel, owner_id, title, current_summary, cancel_event) -> str:
    """Show the current value with Change / Keep current. Returns ``"change"``
    / ``"KEEP"`` / ``"CANCEL"`` / ``"TIMEOUT"``."""
    view = _ButtonChoiceView(
        owner_id,
        [
            ("✏️ Change it", "change", discord.ButtonStyle.primary),
            ("↩️ Keep current", "keep", discord.ButtonStyle.secondary),
        ],
    )
    await channel.send(f"**{title}**\nCurrent: {current_summary}", view=view)
    await wizard_registry.wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return "CANCEL"
    if not view.confirmed:
        return "TIMEOUT"
    return "change" if view.value == "change" else "KEEP"


async def _edit_section(channel, guild_id, user, cfg, cancel_event, section) -> str:
    """Dispatch one section editor. Returns ``"OK"`` / ``"KEEP"`` / ``"CANCEL"``
    / ``"TIMEOUT"``."""
    handlers = {
        "mapping": _edit_mapping,
        "channel": _edit_channel,
        "style": _edit_style,
        "filter": _edit_filter,
        "intake": _edit_intake,
        "templates": _edit_templates,
        "removal": _edit_removal,
    }
    handler = handlers.get(section)
    if handler is None:
        return "OK"
    return await handler(channel, guild_id, user, cfg, cancel_event)


async def _edit_mapping(channel, guild_id, user, cfg, cancel_event) -> str:
    saved_map = transfer.parse_column_map(cfg.get("alliance_column_map_json"))
    g = await _edit_gate(
        channel, user.id, "Column mapping", transfer.summarize_column_map(saved_map), cancel_event
    )
    if g != "change":
        return g
    mode = cfg.get("setup_mode") or ""
    sheet_id = (cfg.get("alliance_sheet_id") or "").strip()
    tab = (cfg.get("alliance_sheet_tab") or "").strip()
    try:
        header, _rows = await asyncio.to_thread(transfer_sheets.read_sheet, sheet_id, tab)
    except Exception as e:  # noqa: BLE001
        await channel.send(f"⚠️ Couldn't read your sheet: {config.describe_sheet_error(e)}")
        return "OK"
    cm = await _map_step(
        channel,
        guild_id,
        user.id,
        header,
        saved_map,
        include_status=(mode != _MODE_WATCH),
        cancel_event=cancel_event,
    )
    if cm in ("CANCEL", "TIMEOUT"):
        return cm
    # Re-snapshot silently so adding a status column doesn't fire a flood of
    # "status changed" notices for everyone who already has a value in it.
    await channel.send("⏳ Re-syncing your applicants with the new columns…")
    try:
        await _finalize_and_enable(guild_id, sheet_id, tab, cm)
    except Exception as e:  # noqa: BLE001
        await channel.send(
            f"⚠️ Mapping saved, but the re-sync failed: {config.describe_sheet_error(e)}"
        )
        return "OK"
    await channel.send("✅ Column mapping updated.")
    return "OK"


async def _edit_channel(channel, guild_id, user, cfg, cancel_event) -> str:
    chan = cfg.get("notification_channel_id") or 0
    g = await _edit_gate(
        channel,
        user.id,
        "Notification channel",
        f"<#{chan}>" if chan else "*none set*",
        cancel_event,
    )
    if g != "change":
        return g
    st, chan_id = await _step_channel(channel, user.id, cancel_event)
    if st in ("CANCEL", "TIMEOUT"):
        return st
    config.update_transfer_config_field(guild_id, "notification_channel_id", chan_id)
    await channel.send(f"✅ Notices will post to <#{chan_id}>.")
    return "OK"


async def _edit_style(channel, guild_id, user, cfg, cancel_event) -> str:
    cur = (
        "a message per applicant"
        if (cfg.get("notification_style") or "each") == "each"
        else "a digest"
    )
    g = await _edit_gate(channel, user.id, "Notification style", cur, cancel_event)
    if g != "change":
        return g
    st, style = await _step_style(channel, user.id, cancel_event)
    if st in ("CANCEL", "TIMEOUT"):
        return st
    config.update_transfer_config_field(guild_id, "notification_style", style)
    await channel.send("✅ Notification style updated.")
    return "OK"


async def _edit_filter(channel, guild_id, user, cfg, cancel_event) -> str:
    mode = cfg.get("setup_mode") or ""
    cur = transfer.describe_filter(transfer.parse_filter(cfg.get("notification_filter_json")))
    g = await _edit_gate(channel, user.id, "Notification filter", cur, cancel_event)
    if g != "change":
        return g
    sheet_id = (cfg.get("alliance_sheet_id") or "").strip()
    tab = (cfg.get("alliance_sheet_tab") or "").strip()
    try:
        header, rows = await asyncio.to_thread(transfer_sheets.read_sheet, sheet_id, tab)
    except Exception as e:  # noqa: BLE001
        await channel.send(f"⚠️ Couldn't read your sheet: {config.describe_sheet_error(e)}")
        return "OK"
    if mode == _MODE_WATCH:
        filt = await _build_filter(
            channel,
            user.id,
            header,
            rows,
            cancel_event,
            intro=(
                "**Which applicants should ping you?**\n"
                "This is a shared sheet, so it may hold applicants for other alliances too. "
                "Get pinged for every new row, or only the ones matching a filter?"
            ),
            none_label="✅ Every new applicant on the sheet",
            build_label="🔎 Only ones matching a filter",
        )
    else:
        filt = await _build_filter(channel, user.id, header, rows, cancel_event)
    if filt in ("CANCEL", "TIMEOUT"):
        return filt
    config.update_transfer_config_field(
        guild_id, "notification_filter_json", json.dumps(filt) if filt else ""
    )
    await channel.send(f"✅ Filter: {transfer.describe_filter(filt)}")
    return "OK"


async def _edit_intake(channel, guild_id, user, cfg, cancel_event) -> str:
    mode = cfg.get("setup_mode") or ""
    changed = False
    if mode == _MODE_SOURCE_TO_OWN:
        r = await _run_source_step(
            channel,
            guild_id,
            user.id,
            step_label="Your shared (intake) sheet",
            intro_text=(
                "The read-only sheet applicants come in on. The bot copies matching rows from it "
                "into your sheet."
            ),
            prefix="server_wide",
            current=cfg,
            cancel_event=cancel_event,
            required=True,
        )
        if r in ("CANCEL", "TIMEOUT"):
            return r
        changed = changed or r == "OK"
        cfg = config.get_transfer_config(guild_id)
    elif mode == _MODE_OWN:
        r = await _run_source_step(
            channel,
            guild_id,
            user.id,
            step_label="Pull from a shared sheet (optional)",
            intro_text=(
                "Pull matching applicants in from a server-wide / shared sheet (read-only: the bot "
                "copies from it, never edits it). Skip if you only track your own sheet."
            ),
            prefix="server_wide",
            current=cfg,
            cancel_event=cancel_event,
        )
        if r in ("CANCEL", "TIMEOUT"):
            return r
        changed = changed or r == "OK"
        cfg = config.get_transfer_config(guild_id)

    rf = await _run_source_step(
        channel,
        guild_id,
        user.id,
        step_label="Your own form (optional)",
        intro_text=(
            "If your alliance runs its own Google Form, the bot can copy each new response in. "
            "Skip it if your form already writes straight into this sheet — the bot will see those "
            "rows on its own."
        ),
        prefix="alliance_form",
        current=cfg,
        cancel_event=cancel_event,
    )
    if rf in ("CANCEL", "TIMEOUT"):
        return rf
    changed = changed or rf == "OK"

    if changed:
        cfg = config.get_transfer_config(guild_id)
        sheet_id = (cfg.get("alliance_sheet_id") or "").strip()
        tab = (cfg.get("alliance_sheet_tab") or "").strip()
        cm = transfer.parse_column_map(cfg.get("alliance_column_map_json"))
        await channel.send("⏳ Pulling matching applicants in and re-syncing…")
        try:
            await _finalize_and_enable(guild_id, sheet_id, tab, cm)
        except Exception as e:  # noqa: BLE001
            await channel.send(
                f"⚠️ Sources saved, but the re-sync failed: {config.describe_sheet_error(e)}"
            )
            return "OK"
        await channel.send("✅ Intake sources updated.")
    return "OK"


async def _edit_templates(channel, guild_id, user, cfg, cancel_event) -> str:
    # Each template prompt already offers Keep current / Define your own, so it
    # carries its own per-template escape.
    if await _step_templates(channel, guild_id, user.id, cfg, cancel_event) != "OK":
        return "CANCEL"  # ask_keep_or_change posted its own cancel/timeout notice
    await channel.send("✅ Templates updated.")
    return "OK"


async def _edit_removal(channel, guild_id, user, cfg, cancel_event) -> str:
    rmv = "on" if cfg.get("notify_on_delete") else "off"
    wbk = "on" if cfg.get("writeback_enabled") else "off"
    g = await _edit_gate(
        channel,
        user.id,
        "Removal notices & write-back",
        f"removal {rmv}, write-back {wbk}",
        cancel_event,
    )
    if g != "change":
        return g
    rm = await _step_removal(channel, guild_id, user.id, cancel_event)
    if rm in ("CANCEL", "TIMEOUT"):
        return rm
    status_cols = transfer.parse_column_map(cfg.get("alliance_column_map_json")).get("status", [])
    if status_cols:
        wb = await _step_writeback(channel, guild_id, user.id, cancel_event, status_cols)
        if wb == "CANCEL":
            return "CANCEL"
        if wb == "TIMEOUT":
            return "TIMEOUT"
    else:
        await channel.send(
            "ℹ️ Write-back needs a Status column — add one in **Column mapping** first."
        )
    await channel.send("✅ Removal & write-back updated.")
    return "OK"

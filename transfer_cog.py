"""Transfer Management (#16) — the poll loop + notifications.

A per-minute background loop walks every guild with the watcher enabled,
polls each one whose interval has elapsed, diffs the sheet against the
last-seen state (``transfer.compute_poll_diff``), and posts new-applicant /
status-change / removal notices to the configured channel. Premium is
re-checked at poll time, so a lapsed subscriber's watcher goes quiet without
its row being deleted. A clean tick stamps a heartbeat so the #227 outage
catch-up can tell the loop was alive.

Notification action buttons (full details, draft a message) live on each
notice. They're non-persistent (timeout + ``expire_view_message`` cleanup) —
for acting on older applicants, the `/transfers` hub viewer is the durable
surface.

Optional server-wide / intake-form sources are pulled in at the top of each
poll: matching, not-yet-copied rows are aligned to the alliance sheet's
columns and appended, then the sheet is re-read so they surface as new
applicants the same poll. Decision write-back attaches once the wizard
configures it.

When a poll can't read a sheet the alliance controls (a renamed or deleted
tab, a deleted spreadsheet, revoked service-account access), the watcher used
to log, capture to Sentry, and go quiet — leaving the feature dead for days
with nobody told (#413). Those errors now post one leadership-channel notice
naming what broke and how to fix it, deduplicated by
``transfer.sheet_error_signature`` with a daily re-nudge, surfaced on the
`/transfers` hub, and cleared with a recovery line on the first clean read.
They're also classified through ``config.is_user_config_sheet_error`` so an
alliance's own Sheet misconfiguration no longer pages Sentry (the #285 / #286
treatment, which this loop never adopted).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import premium
import transfer
import transfer_sheets
import wizard_registry

try:
    import sentry_sdk
except Exception:  # pragma: no cover - sentry optional in some envs
    sentry_sdk = None

logger = logging.getLogger(__name__)

# Safety cap on per-applicant messages in one check, so a recruiter pasting a
# huge block of rows can't fire hundreds of messages even on the 'each' style.
_MAX_EACH = 25
_NOTICE_TIMEOUT = 21600  # 6h — act-now window; older applicants via the hub

_TEMPLATE_BTN = {
    "apply_invitation": "📩 Apply message",
    "confirm_request": "📩 Confirm message",
    "decline": "📩 Decline message",
}


def _capture(e: Exception) -> None:
    if sentry_sdk is not None:
        try:
            sentry_sdk.capture_exception(e)
        except Exception:
            pass


def _capture_unless_alliance_owned(e: Exception, gid, where: str) -> None:
    """Log a sheet failure, and page Sentry only if it's actually a bot bug.

    A failure the *alliance* owns (missing tab, deleted or unshared
    spreadsheet, rate limit) is logged with a diagnosis and not captured:
    capturing them buries real bugs under one alliance's renamed tab
    (#285 / #286, and #413 for this loop). Anything else still pages.
    """
    logger.warning(
        "[TRANSFER] guild %s: %s: %s", gid, where, config.describe_sheet_error(e, guild_id=gid)
    )
    if not config.is_user_config_sheet_error(e):
        _capture(e)


# ── Stuck-watcher notices (#413) ──────────────────────────────────────────────

_STUCK_TITLE = "⚠️ Transfer watch is stuck"
_RECOVERED_TITLE = "✅ Transfer watch is working again"

# Problem kinds worth telling the alliance about: durable, and theirs to fix.
MISSING_TAB = "missing_tab"
MISSING_SHEET = "missing_sheet"
NO_ACCESS = "no_access"


def sheet_problem_kind(e: Exception) -> str | None:
    """Which alliance-fixable problem this exception represents, or ``None``.

    ``None`` covers everything the alliance can't act on: a rate limit (429,
    which clears itself and would be a false alarm), a transient 5xx, or a bug
    in the bot. Those still log and skip; they just don't raise an alarm in the
    leadership channel.

    Kept separate from ``config.is_user_config_sheet_error`` on purpose: that
    answers "should Sentry care", which includes 429. This answers "should the
    alliance be told", which does not.
    """
    import gspread

    if isinstance(e, gspread.exceptions.WorksheetNotFound):
        return MISSING_TAB
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return MISSING_SHEET
    if isinstance(e, gspread.exceptions.APIError):
        status = None
        resp = getattr(e, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
        status = status or getattr(e, "code", None)
        if status == 404:
            return MISSING_SHEET
        if status == 403:
            return NO_ACCESS
    return None


def _problem_reason(kind: str, tab: str) -> str:
    """Plain-language "what's wrong" line for a notice.

    Written for leadership, not for a log: ``config.describe_sheet_error`` is
    the log-side diagnosis and points at the wrong command for this feature.
    """
    if kind == MISSING_TAB:
        named = f"a tab named `{tab}`" if tab else "the tab I was told to watch"
        return (
            f"That spreadsheet no longer has {named}. It was most likely renamed, "
            "or the tab was deleted."
        )
    if kind == MISSING_SHEET:
        return (
            "I can't open that spreadsheet at all. It may have been deleted, moved to a "
            "different account, or the sheet link saved in setup is wrong."
        )
    if kind == NO_ACCESS:
        return (
            "I don't have permission to open that spreadsheet any more. Its sharing settings "
            "were most likely changed."
        )
    return "I couldn't read that spreadsheet."


def _existing_tabs_hint(sheet_id: str) -> str:
    """A "the tabs on that spreadsheet are currently X, Y, Z" line, or ``""``.

    The overwhelmingly common cause of a stuck watcher is a renamed tab, and
    the fix is obvious the moment you can see the real names. Best-effort: if
    even this read fails (the whole spreadsheet is gone, access revoked) the
    notice just omits the hint rather than failing to send.
    """
    try:
        names = transfer_sheets.list_tab_names(sheet_id)
    except Exception:
        return ""
    if not names:
        return ""
    return ", ".join(f"`{n}`" for n in names[:20])


def _fix_instruction(kind: str) -> str:
    """The "How to fix it" line, matched to the problem.

    A permissions failure isn't fixed by re-picking the sheet, so pointing at
    setup for a 403 would send leadership down the wrong path.
    """
    from transfers_hub import SETUP_TRANSFERS_BTN, TRANSFERS_HUB_CMD

    keep_checking = (
        " I'll keep checking on every poll and post here as soon as I can read it again."
    )
    if kind == NO_ACCESS:
        return (
            "In Google Sheets, use **Share** to give the bot's service account Editor access "
            f"to that spreadsheet. If you'd rather point at a different sheet, run "
            f"{TRANSFERS_HUB_CMD} and click **{SETUP_TRANSFERS_BTN}**." + keep_checking
        )
    if kind == MISSING_TAB:
        return (
            f"Either rename the tab back, or run {TRANSFERS_HUB_CMD}, click "
            f"**{SETUP_TRANSFERS_BTN}**, and pick the tab's new name." + keep_checking
        )
    return (
        f"Run {TRANSFERS_HUB_CMD}, click **{SETUP_TRANSFERS_BTN}**, and re-pick the "
        "spreadsheet." + keep_checking
    )


def _stuck_embed(scope: str, kind: str, reason: str, tabs_hint: str) -> discord.Embed:
    """The leadership-channel notice for a watcher blocked on a sheet problem.

    Names the sheet in the alliance's own terms, says what's wrong in plain
    language, and gives the fix that actually applies.
    """
    which = transfer.SHEET_SCOPE_LABELS.get(scope, "one of your transfer sheets")
    embed = discord.Embed(
        title=_STUCK_TITLE,
        description=(
            f"I can't read {which}, so transfer notifications have stopped. "
            "New applicants aren't being picked up until this is sorted out."
        ),
        color=discord.Color.red(),
    )
    embed.add_field(name="What's wrong", value=reason[:1024], inline=False)
    if tabs_hint:
        embed.add_field(
            name="Tabs on that spreadsheet right now",
            value=tabs_hint[:1024],
            inline=False,
        )
    embed.add_field(name="How to fix it", value=_fix_instruction(kind)[:1024], inline=False)
    return embed


def _leadership_channel(bot, gid: int):
    """The guild's configured leadership channel, or ``None``.

    Sheet problems go here rather than to the transfer notification channel:
    they're an admin task for whoever runs setup, not a recruiting notice.
    """
    cfg = config.get_config(gid)
    channel_id = getattr(cfg, "leadership_channel_id", 0) or 0 if cfg else 0
    if not channel_id:
        return None
    return bot.get_channel(channel_id)


async def note_sheet_problem(
    bot, gid: int, scope: str, tab: str, sheet_id: str, e: Exception, now: datetime
):
    """Record an alliance-fixable sheet failure and tell leadership, once.

    Called from the poll loop only: the wizard and the hub's Check now already
    report read failures inline, where the user is looking. No-op for problems
    the alliance can't act on (see :func:`sheet_problem_kind`).

    Stores the problem signature so a failure that repeats every poll posts
    once and then goes quiet, re-nudging daily until it's fixed. A failed send
    still records the signature, so a broken leadership channel can't turn this
    into a post-attempt-every-poll loop. ``now`` is the poll's clock, passed in
    so the quiet window is measured against the same instant the poll stamped.
    """
    kind = sheet_problem_kind(e)
    if kind is None:
        return
    reason = _problem_reason(kind, tab)
    signature = transfer.sheet_error_signature(scope, kind, tab)
    cfg = config.get_transfer_config(gid)
    notify = transfer.should_notify_sheet_error(
        cfg.get("sheet_error_signature") or "",
        cfg.get("sheet_error_notified_at") or "",
        signature,
        now,
    )
    if not notify:
        # Same problem, still inside its quiet window. Keep the stored reason
        # fresh (the hub renders it) but don't post again.
        config.update_transfer_config_fields(
            gid, sheet_error_signature=signature, sheet_error_detail=reason
        )
        return

    channel = _leadership_channel(bot, gid)
    if channel is None:
        logger.info(
            "[TRANSFER] guild %s: watcher stuck (%s) but no resolvable leadership channel; "
            "the /transfers hub will still show it",
            gid,
            signature,
        )
    else:
        # Only worth a round-trip when a rename is the likely cause; if the
        # whole spreadsheet is unreachable, listing its tabs would fail too.
        tabs_hint = ""
        if kind == MISSING_TAB and sheet_id:
            tabs_hint = await asyncio.to_thread(_existing_tabs_hint, sheet_id)
        try:
            await channel.send(embed=_stuck_embed(scope, kind, reason, tabs_hint))
        except (discord.Forbidden, discord.HTTPException) as send_err:
            logger.warning(
                "[TRANSFER] guild %s: could not post stuck-watcher notice: %s", gid, send_err
            )

    config.update_transfer_config_fields(
        gid,
        sheet_error_signature=signature,
        sheet_error_detail=reason,
        sheet_error_notified_at=now.isoformat(),
    )


async def clear_sheet_problem(bot, gid: int, cfg: dict) -> None:
    """Clear a recorded sheet problem after a clean poll, and say so.

    The recovery line matters: the alliance was told the watcher was dead, went
    and changed something, and needs to know whether it worked. No-op when
    nothing was recorded, which is the overwhelmingly common case, so a healthy
    guild's poll costs one dict lookup.
    """
    signature = cfg.get("sheet_error_signature") or ""
    if not signature:
        return
    which = transfer.SHEET_SCOPE_LABELS.get(
        transfer.sheet_error_scope(signature), "your transfer sheets"
    )
    channel = _leadership_channel(bot, gid)
    if channel is not None:
        try:
            await channel.send(
                embed=discord.Embed(
                    title=_RECOVERED_TITLE,
                    description=(
                        f"I can read {which} again. Back to watching for new applicants "
                        "and status changes."
                    ),
                    color=discord.Color.green(),
                )
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.warning("[TRANSFER] guild %s: could not post recovery notice: %s", gid, e)
    config.update_transfer_config_fields(
        gid, sheet_error_signature="", sheet_error_detail="", sheet_error_notified_at=""
    )


def _display_status_value(value) -> str:
    """User-facing text for a status cell: a checkbox/boolean cell (``TRUE`` /
    ``FALSE``) shows as Yes / No, any other text passes through unchanged, and a
    blank shows as ``(blank)``. Keeps TRUE/FALSE out of leadership-facing copy
    while the bot still writes the literal booleans the checkbox needs."""
    if value is None:
        return "(blank)"
    s = str(value).strip()
    low = s.lower()
    if low == "true":
        return "Yes"
    if low == "false":
        return "No"
    return s or "(blank)"


# ── Embeds ────────────────────────────────────────────────────────────────────


def _new_applicant_embed(name: str, display_pairs: list) -> discord.Embed:
    embed = discord.Embed(
        title=f"📥 New transfer applicant: {name}"[:256], color=discord.Color.green()
    )
    body = "\n".join(f"**{h}:** {v}" for h, v in display_pairs)
    embed.description = body[:4000] if body else "*(no display columns configured)*"
    return embed


def _status_change_embed(name: str, changes: list) -> discord.Embed:
    embed = discord.Embed(title=f"🔔 {name}: status changed"[:256], color=discord.Color.blue())
    lines = [
        f"**{field}** has changed from {_display_status_value(old)} to {_display_status_value(new)}"
        for field, old, new in changes
    ]
    embed.description = "\n".join(lines)[:4000]
    return embed


def _removal_embed(name: str, snapshot: dict) -> discord.Embed:
    embed = discord.Embed(title=f"🗑️ {name} removed from the sheet"[:256], color=discord.Color.red())
    last = ", ".join(
        f"{k}: {_display_status_value(v)}" for k, v in (snapshot or {}).items() if str(v).strip()
    )
    embed.description = f"They'd been marked: {last}." if last else "Removed from your sheet."
    return embed


def _full_details_embed(name: str, header: list, row: list) -> discord.Embed:
    """Every column of the sheet row, one field per line (Decision J)."""
    embed = discord.Embed(title=f"📄 {name}: full record"[:256], color=discord.Color.greyple())
    lines = []
    for i, h in enumerate(header):
        if not str(h).strip():
            continue
        if i < len(row):
            cell = row[i]
            val = cell.strip() if isinstance(cell, str) else str(cell)
        else:
            val = ""
        lines.append(f"**{h}:** {val or '·'}")
    embed.description = "\n".join(lines)[:4000]
    return embed


# ── Notice view (full details + draft-a-message) ──────────────────────────────


class _WriteConfirmView(discord.ui.View):
    """Ephemeral decision prompt → write a value to the decision's column on the
    alliance sheet. Buttons follow the decision's shape: a **yesno** decision
    shows Yes / No (writing ``TRUE`` / ``FALSE`` so a checkbox toggles), a
    **pickone** decision shows one button per option (writing that option). The
    user never sees TRUE/FALSE. The row is re-found by identity at click time,
    since it may have moved since the notice posted."""

    def __init__(self, *, name: str, decision: dict, writeback: dict):
        super().__init__(timeout=120)
        self.name = name
        self.status_col = decision["column"]
        self.wb = writeback
        if decision.get("kind") == "pickone" and decision.get("options"):
            for opt in decision["options"][:20]:
                btn = discord.ui.Button(label=str(opt)[:80], style=discord.ButtonStyle.primary)
                btn.callback = self._make(str(opt), str(opt))
                self.add_item(btn)
        else:
            yes = discord.ui.Button(label="✅ Yes", style=discord.ButtonStyle.success)
            yes.callback = self._make("TRUE", "Yes")
            self.add_item(yes)
            no = discord.ui.Button(label="❌ No", style=discord.ButtonStyle.danger)
            no.callback = self._make("FALSE", "No")
            self.add_item(no)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content="Cancelled. Nothing written.", view=None)

    def _make(self, raw_value: str, label: str):
        async def _cb(interaction: discord.Interaction):
            await self._write(interaction, raw_value, label)

        return _cb

    async def _write(self, interaction: discord.Interaction, raw_value: str, label: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        wb = self.wb
        try:
            header, rows = await asyncio.to_thread(
                transfer_sheets.read_sheet, wb["sheet_id"], wb["tab"]
            )
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(
                f"⚠️ Couldn't reach the sheet: {config.describe_sheet_error(e)}", ephemeral=True
            )
            return
        hidx = transfer.header_index(header)
        idx = transfer.find_row_index(rows, hidx, wb["column_map"], wb["hash"])
        if idx is None:
            await interaction.followup.send(
                f"⚠️ Couldn't find **{self.name}** on the sheet anymore (row moved or removed).",
                ephemeral=True,
            )
            return
        col_idx = hidx.get(transfer.norm_header(self.status_col))
        if col_idx is None:
            await interaction.followup.send(
                f"⚠️ The **{self.status_col}** column isn't on the sheet anymore.", ephemeral=True
            )
            return
        try:
            await asyncio.to_thread(
                transfer_sheets.write_cell, wb["sheet_id"], wb["tab"], idx + 2, col_idx, raw_value
            )
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(
                f"⚠️ Couldn't write to the sheet: {config.describe_sheet_error(e)} "
                "(the bot's service account needs edit access).",
                ephemeral=True,
            )
            _capture(e)
            return
        await interaction.followup.send(
            f"✅ Set **{self.status_col}** to **{label}** for **{self.name}**.", ephemeral=True
        )


class _NoticeView(discord.ui.View):
    def __init__(
        self, *, guild_id, name, header, row, display_pairs, template_kinds, writeback=None
    ):
        super().__init__(timeout=_NOTICE_TIMEOUT)
        self.guild_id = guild_id
        self.name = name
        self.header = header
        self.row = row
        self.display_pairs = display_pairs
        self.writeback = writeback
        self.message: discord.Message | None = None

        # Row 0: the bot's own actions (full details + draft-a-message).
        details = discord.ui.Button(
            label="📄 Full details", style=discord.ButtonStyle.secondary, row=0
        )
        details.callback = self._full_details
        self.add_item(details)
        for kind in template_kinds:
            btn = discord.ui.Button(
                label=_TEMPLATE_BTN.get(kind, "📩 Message"),
                style=discord.ButtonStyle.primary,
                row=0,
            )
            btn.callback = self._make_template_cb(kind)
            self.add_item(btn)
        # Row 1: decision write-back — one button per decision (capped to a row).
        # Clicking it prompts for the decision's values (Yes/No or pick-one).
        if writeback:
            for decision in (writeback.get("decisions") or [])[:5]:
                btn = discord.ui.Button(
                    label=f"✏️ Set {decision['column']}"[:80],
                    style=discord.ButtonStyle.secondary,
                    row=1,
                )
                btn.callback = self._make_writeback_cb(decision)
                self.add_item(btn)

    async def on_timeout(self) -> None:
        await wizard_registry.expire_view_message(self.message, command_hint="`/transfers`")

    async def _full_details(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=_full_details_embed(self.name, self.header, self.row), ephemeral=True
        )

    def _make_template_cb(self, kind: str):
        async def _cb(interaction: discord.Interaction):
            cfg = config.get_transfer_config(self.guild_id)
            body = transfer.resolve_template(cfg, kind)
            guild = interaction.guild
            context = {"name": self.name, "alliance_name": guild.name if guild else ""}
            for header, value in self.display_pairs:
                context[transfer.field_token(header)] = value
            rendered = transfer.render_transfer_template(body, **context)
            await interaction.response.send_message(
                f"📋 Copy this into game chat:\n>>> {rendered}", ephemeral=True
            )

        return _cb

    def _make_writeback_cb(self, decision: dict):
        async def _cb(interaction: discord.Interaction):
            view = _WriteConfirmView(name=self.name, decision=decision, writeback=self.writeback)
            if decision.get("kind") == "pickone":
                prompt = f"Set **{decision['column']}** for **{self.name}** to which value?"
            else:
                prompt = f"Set **{decision['column']}** for **{self.name}** to Yes or No?"
            await interaction.response.send_message(prompt, view=view, ephemeral=True)

        return _cb


# ── Source copy (shared by the poll loop and setup go-live) ──────────────────


async def copy_sources(cfg: dict, target_header: list) -> dict:
    """Copy filter-matching, not-yet-copied rows from the optional intake
    sources (``server_wide`` = a shared/server-wide sheet, ``alliance_form`` =
    the alliance's own form responses) into the alliance's tracking sheet, each
    aligned to its column order. Dedup hashes persist in ``copied_state_json``
    so a row is copied once. With blank-cell enrichment on (#9), people already
    on the list are topped up instead of re-appended.

    Called once per poll by the loop, at go-live by the setup wizard, and by the
    `/transfers` "Check now" button. The bot only ever appends to the alliance's
    *own* sheet.

    Returns a diagnostic report::

        {"copied": int, "enriched": int, "sources": [
            {"prefix", "read", "matched", "already_pulled",
             "skipped_on_sheet", "copied", "enriched", "error", "exc"}]}

    ``exc`` carries the read exception (``None`` when the source read worked) so
    the poll loop can raise a stuck-watcher notice for it (#413). Callers that
    show their result inline (the wizard, the hub's Check now) use ``error`` and
    ignore it.
    """
    gid = cfg["guild_id"]
    alliance_id = (cfg.get("alliance_sheet_id") or "").strip()
    alliance_tab = (cfg.get("alliance_sheet_tab") or "").strip()
    report: dict = {"copied": 0, "enriched": 0, "sources": []}
    if not alliance_id or not alliance_tab:
        return report
    try:
        copied_set = set(json.loads(cfg.get("copied_state_json") or "[]"))
    except (ValueError, TypeError):
        copied_set = set()

    # Always read the alliance rows once. Their identities dedup the pull
    # against what's *actually* on the sheet — so people already on the list are
    # never appended as duplicates, and a copied-state reset (re-setup) re-pulls
    # cleanly without doubling anyone. Blank-cell enrichment (#9, opt-in) reuses
    # the same read.
    enrich = bool(cfg.get("source_enrich_blanks"))
    target_map = transfer.parse_column_map(cfg.get("alliance_column_map_json"))
    target_rows = None
    if target_map.get("name"):
        try:
            _th, target_rows = await asyncio.to_thread(
                transfer_sheets.read_sheet, alliance_id, alliance_tab
            )
        except Exception as e:  # noqa: BLE001
            _capture_unless_alliance_owned(e, gid, "alliance read failed")
            target_rows = None

    target_hidx = transfer.header_index(target_header)
    existing_ids: set = set()
    if target_rows is not None and target_map.get("name"):
        for trow in target_rows:
            tid = transfer.row_identity(trow, target_hidx, target_map)
            if tid:
                existing_ids.add(tid)

    state_changed = False
    for prefix in ("server_wide", "alliance_form"):
        if not cfg.get(f"{prefix}_enabled"):
            continue
        src = {
            "prefix": prefix,
            "read": 0,
            "matched": 0,
            "already_pulled": 0,
            "skipped_on_sheet": 0,
            "copied": 0,
            "enriched": 0,
            "error": None,
            "exc": None,
        }
        report["sources"].append(src)
        s_id = (cfg.get(f"{prefix}_sheet_id") or "").strip()
        s_tab = (cfg.get(f"{prefix}_sheet_tab") or "").strip()
        s_map = transfer.parse_column_map(cfg.get(f"{prefix}_column_map_json"))
        if not s_id or not s_tab:
            src["error"] = "sheet/tab not configured"
            continue
        if not s_map.get("name"):
            src["error"] = "no Name column mapped on the source"
            continue
        s_filter = transfer.parse_filter(cfg.get(f"{prefix}_filter_json"))
        try:
            s_header, s_rows = await asyncio.to_thread(transfer_sheets.read_sheet, s_id, s_tab)
        except Exception as e:  # noqa: BLE001
            src["error"] = config.describe_sheet_error(e, tab=s_tab)
            src["exc"] = e
            _capture_unless_alliance_owned(e, gid, f"{prefix} source read failed")
            continue
        s_hidx = transfer.header_index(s_header)
        s_copy_map = s_map.get("copy_map") if isinstance(s_map, dict) else None

        to_copy, sel = transfer.classify_source_rows(
            s_rows, s_hidx, s_map, filter_obj=s_filter, already_copied=copied_set
        )
        src["read"] = sel["read"]
        src["matched"] = sel["matched"]
        src["already_pulled"] = sel["already_pulled"]
        # Already on the sheet? Don't append a duplicate (enriched below if on).
        if existing_ids:
            before = len(to_copy)
            to_copy = [
                r for r in to_copy if transfer.row_identity(r, s_hidx, s_map) not in existing_ids
            ]
            src["skipped_on_sheet"] = before - len(to_copy)
        if to_copy:
            aligned = [transfer.align_row(s_header, r, target_header, s_copy_map) for r in to_copy]
            try:
                await asyncio.to_thread(
                    transfer_sheets.append_rows, alliance_id, alliance_tab, aligned
                )
            except Exception as e:  # noqa: BLE001
                # Don't mark these copied, so we retry them next poll.
                src["error"] = f"append failed: {config.describe_sheet_error(e, tab=alliance_tab)}"
                _capture_unless_alliance_owned(e, gid, "append to alliance sheet failed")
            else:
                src["copied"] = len(aligned)
                report["copied"] += len(aligned)
                for r in to_copy:
                    rid = transfer.row_identity(r, s_hidx, s_map)
                    if rid:
                        copied_set.add(rid)
                        state_changed = True

        # Fill blank cells in people already on the list from this source (#9).
        if enrich and target_rows is not None:
            try:
                fills = transfer.plan_blank_fill(
                    target_header,
                    target_rows,
                    target_map,
                    s_header,
                    s_rows,
                    s_map,
                    copy_map=s_copy_map,
                )
                if fills:
                    await asyncio.to_thread(
                        transfer_sheets.update_cells, alliance_id, alliance_tab, fills
                    )
                    src["enriched"] = len(fills)
                    report["enriched"] += len(fills)
                    # Reflect the writes in our in-memory copy so a second source
                    # doesn't re-plan the same cells (and sees them as filled).
                    for r_num, c_idx, val in fills:
                        ri = r_num - 2
                        if 0 <= ri < len(target_rows):
                            row = target_rows[ri]
                            while len(row) <= c_idx:
                                row.append("")
                            row[c_idx] = val
            except Exception as e:  # noqa: BLE001
                _capture_unless_alliance_owned(e, gid, "enrich write failed")

    if state_changed:
        config.update_transfer_config_field(
            gid, "copied_state_json", json.dumps(sorted(copied_set))
        )
    return report


# ── The cog ───────────────────────────────────────────────────────────────────


class TransferCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    @app_commands.command(name="transfers", description="Open the Transfer Management hub")
    @app_commands.guild_only()
    async def transfers(self, interaction: discord.Interaction):
        from transfers_hub import handle_transfers_hub

        await handle_transfers_hub(self.bot, interaction)

    @tasks.loop(minutes=1)
    async def poll(self):
        try:
            guilds = config.get_transfer_enabled_guilds()
        except Exception as e:
            logger.warning("[TRANSFER] could not list enabled guilds: %s", e)
            _capture(e)
            return
        now = datetime.now(timezone.utc)
        for cfg in guilds:
            try:
                await self._poll_guild(cfg, now)
            except Exception as e:
                logger.warning("[TRANSFER] poll error for guild %s: %s", cfg.get("guild_id"), e)
                _capture(e)
        config.stamp_loop_heartbeat("transfer_poll")

    @poll.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    async def _poll_guild(self, cfg: dict, now: datetime) -> None:
        gid = cfg["guild_id"]
        if not transfer.poll_is_due(
            cfg.get("last_polled_at"), cfg.get("poll_frequency_minutes") or 30, now
        ):
            return
        if not await premium.is_premium(gid, bot=self.bot):
            return  # lapsed subscriber — go quiet, don't delete the config

        sheet_id = (cfg.get("alliance_sheet_id") or "").strip()
        tab = (cfg.get("alliance_sheet_tab") or "").strip()
        column_map = transfer.parse_column_map(cfg.get("alliance_column_map_json"))
        if not sheet_id or not tab or not column_map.get("name"):
            return

        filter_obj = transfer.parse_filter(cfg.get("notification_filter_json"))
        try:
            prior_state = json.loads(cfg.get("last_seen_state_json") or "{}")
            if not isinstance(prior_state, dict):
                prior_state = {}
        except (ValueError, TypeError):
            prior_state = {}

        now_iso = now.isoformat()
        try:
            header, data_rows = await asyncio.to_thread(transfer_sheets.read_sheet, sheet_id, tab)
        except Exception as e:  # noqa: BLE001
            # Back off to the configured interval rather than retrying a broken
            # sheet every minute (and keep the seen-state intact). Tell
            # leadership: this is where the feature used to die silently (#413).
            config.update_transfer_config_field(gid, "last_polled_at", now_iso)
            _capture_unless_alliance_owned(e, gid, "sheet read failed")
            await note_sheet_problem(self.bot, gid, "alliance", tab, sheet_id, e, now)
            return

        # Optional source pulls (server-wide / intake form): copy matching whole
        # rows into the alliance sheet, aligned to its columns and deduped across
        # polls, then re-read so the copied rows surface as new applicants now.
        source_problem = None
        try:
            src_report = await copy_sources(cfg, header)
            copied = src_report["copied"]
            # First broken source wins the one notice slot; a second one gets
            # reported after this one is fixed. Two simultaneously-broken
            # sources is rare, and one clear alert beats two competing ones.
            source_problem = next(
                ((s["prefix"], s["exc"]) for s in src_report["sources"] if s.get("exc")), None
            )
        except Exception as e:  # noqa: BLE001
            _capture_unless_alliance_owned(e, gid, "source copy failed")
            copied = 0
        if copied:
            try:
                header, data_rows = await asyncio.to_thread(
                    transfer_sheets.read_sheet, sheet_id, tab
                )
            except Exception as e:  # noqa: BLE001
                _capture_unless_alliance_owned(e, gid, "re-read after copy failed")

        # The alliance sheet read cleanly. Either a source is still broken, or
        # everything works and any recorded problem is over.
        if source_problem is not None:
            prefix, exc = source_problem
            await note_sheet_problem(
                self.bot,
                gid,
                prefix,
                (cfg.get(f"{prefix}_sheet_tab") or "").strip(),
                (cfg.get(f"{prefix}_sheet_id") or "").strip(),
                exc,
                now,
            )
        else:
            await clear_sheet_problem(self.bot, gid, cfg)

        hidx = transfer.header_index(header)
        diff = transfer.compute_poll_diff(
            data_rows, hidx, column_map, prior_state=prior_state, filter_obj=filter_obj
        )

        channel = self.bot.get_channel(cfg.get("notification_channel_id") or 0)
        if channel is None:
            # Advance the clock so we don't hammer, but keep the seen-state so
            # pending notices fire once the channel resolves again.
            config.update_transfer_config_field(gid, "last_polled_at", now_iso)
            logger.info(
                "[TRANSFER] guild %s: notification channel unresolvable; skipping post", gid
            )
            return

        wb_base = None
        decisions = transfer.decisions_for(column_map)
        if cfg.get("writeback_enabled") and decisions:
            wb_base = {
                "sheet_id": sheet_id,
                "tab": tab,
                "column_map": column_map,
                "decisions": decisions,
            }

        await self._post(
            channel,
            gid,
            header,
            hidx,
            column_map,
            diff,
            cfg.get("notification_style") or "each",
            bool(cfg.get("notify_on_delete")),
            wb_base,
        )
        config.update_transfer_config_fields(
            gid,
            last_seen_state_json=json.dumps(diff.next_state),
            last_polled_at=now_iso,
        )

    async def check_now(self, cfg: dict) -> dict:
        """Run a full check immediately (ignoring the poll interval) for the
        `/transfers` "Check now" button. Pulls from sources, posts any
        new/changed/removed notices, and returns a breakdown of what happened
        so the user can see exactly where rows are or aren't coming through."""
        gid = cfg["guild_id"]
        sheet_id = (cfg.get("alliance_sheet_id") or "").strip()
        tab = (cfg.get("alliance_sheet_tab") or "").strip()
        column_map = transfer.parse_column_map(cfg.get("alliance_column_map_json"))
        if not sheet_id or not tab or not column_map.get("name"):
            return {"error": "Not fully set up yet (a sheet and a Name column are required)."}

        filter_obj = transfer.parse_filter(cfg.get("notification_filter_json"))
        try:
            prior_state = json.loads(cfg.get("last_seen_state_json") or "{}")
            if not isinstance(prior_state, dict):
                prior_state = {}
        except (ValueError, TypeError):
            prior_state = {}

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            header, data_rows = await asyncio.to_thread(transfer_sheets.read_sheet, sheet_id, tab)
        except Exception as e:  # noqa: BLE001
            # No stuck-watcher notice from here: the caller is looking at the
            # result, so the inline error is the notification.
            _capture_unless_alliance_owned(e, gid, "check-now sheet read failed")
            return {"error": f"Couldn't read your sheet: {config.describe_sheet_error(e, tab=tab)}"}

        # Reading cleanly here is as good a recovery signal as a poll: someone
        # who just fixed their sheet clicks Check now to confirm it.
        await clear_sheet_problem(self.bot, gid, cfg)

        try:
            src_report = await copy_sources(cfg, header)
        except Exception as e:  # noqa: BLE001
            _capture_unless_alliance_owned(e, gid, "check-now source copy failed")
            src_report = {"copied": 0, "enriched": 0, "sources": []}
        if src_report.get("copied"):
            try:
                header, data_rows = await asyncio.to_thread(
                    transfer_sheets.read_sheet, sheet_id, tab
                )
            except Exception as e:  # noqa: BLE001
                _capture_unless_alliance_owned(e, gid, "check-now re-read after copy failed")

        hidx = transfer.header_index(header)
        diff = transfer.compute_poll_diff(
            data_rows, hidx, column_map, prior_state=prior_state, filter_obj=filter_obj
        )
        report = {
            "copied": src_report.get("copied", 0),
            "enriched": src_report.get("enriched", 0),
            "sources": src_report.get("sources", []),
            "new": len(diff.new_applicants),
            "status": len(diff.status_changes),
            "removed": len(diff.deletions) if cfg.get("notify_on_delete") else 0,
            "applicants_on_sheet": len(diff.next_state),
            "posted": False,
        }

        channel = self.bot.get_channel(cfg.get("notification_channel_id") or 0)
        if channel is None:
            report["error"] = "Your notification channel can't be found — check it still exists."
            config.update_transfer_config_field(gid, "last_polled_at", now_iso)
            return report

        wb_base = None
        decisions = transfer.decisions_for(column_map)
        if cfg.get("writeback_enabled") and decisions:
            wb_base = {
                "sheet_id": sheet_id,
                "tab": tab,
                "column_map": column_map,
                "decisions": decisions,
            }
        await self._post(
            channel,
            gid,
            header,
            hidx,
            column_map,
            diff,
            cfg.get("notification_style") or "each",
            bool(cfg.get("notify_on_delete")),
            wb_base,
        )
        report["posted"] = True
        config.update_transfer_config_fields(
            gid,
            last_seen_state_json=json.dumps(diff.next_state),
            last_polled_at=now_iso,
        )
        return report

    async def _post(
        self, channel, gid, header, hidx, column_map, diff, style, notify_on_delete, wb_base=None
    ):
        display_headers = column_map.get("display", []) or []
        name_header = column_map.get("name")
        deletions = list(diff.deletions) if notify_on_delete else []

        if style == "digest":
            await self._post_digest(channel, hidx, name_header, display_headers, diff, deletions)
            return

        posted = 0
        for na in diff.new_applicants:
            if posted >= _MAX_EACH:
                await channel.send(
                    f"… and **{len(diff.new_applicants) - posted}** more new applicants this "
                    "check. (Switch to the digest style in setup if this is common.)"
                )
                break
            name = transfer.cell_for(na.row, hidx, name_header) or "(unknown)"
            pairs = transfer.display_fields(na.row, hidx, display_headers)
            view = _NoticeView(
                guild_id=gid,
                name=name,
                header=header,
                row=na.row,
                display_pairs=pairs,
                template_kinds=["apply_invitation"],
                writeback=({**wb_base, "hash": na.hash} if wb_base else None),
            )
            view.message = await channel.send(embed=_new_applicant_embed(name, pairs), view=view)
            posted += 1

        for sc in diff.status_changes:
            name = transfer.cell_for(sc.row, hidx, name_header) or "(unknown)"
            pairs = transfer.display_fields(sc.row, hidx, display_headers)
            view = _NoticeView(
                guild_id=gid,
                name=name,
                header=header,
                row=sc.row,
                display_pairs=pairs,
                template_kinds=["confirm_request", "decline"],
                writeback=({**wb_base, "hash": sc.hash} if wb_base else None),
            )
            view.message = await channel.send(
                embed=_status_change_embed(name, sc.changes), view=view
            )

        for d in deletions:
            await channel.send(embed=_removal_embed(d.name or "(unknown)", d.snapshot))

    async def _post_digest(self, channel, hidx, name_header, display_headers, diff, deletions):
        if not (diff.new_applicants or diff.status_changes or deletions):
            return
        embed = discord.Embed(title="📥 Transfer update", color=discord.Color.green())

        if diff.new_applicants:
            lines = []
            for na in diff.new_applicants[:25]:
                name = transfer.cell_for(na.row, hidx, name_header) or "(unknown)"
                pairs = transfer.display_fields(na.row, hidx, display_headers)
                summary = " · ".join(str(v) for _, v in pairs[:3])
                lines.append(f"• **{name}**" + (f": {summary}" if summary else ""))
            if len(diff.new_applicants) > 25:
                lines.append(f"… +{len(diff.new_applicants) - 25} more")
            embed.add_field(
                name=f"New applicants ({len(diff.new_applicants)})",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        if diff.status_changes:
            lines = []
            for sc in diff.status_changes[:25]:
                name = transfer.cell_for(sc.row, hidx, name_header) or "(unknown)"
                chg = ", ".join(f"{f}: {_display_status_value(n)}" for f, _o, n in sc.changes)
                lines.append(f"• **{name}**: {chg}")
            embed.add_field(
                name=f"Status changes ({len(diff.status_changes)})",
                value="\n".join(lines)[:1024],
                inline=False,
            )

        if deletions:
            names = ", ".join(d.name or "(unknown)" for d in deletions[:25])
            embed.add_field(name=f"Removed ({len(deletions)})", value=names[:1024], inline=False)

        await channel.send(embed=embed)


async def setup(bot):
    await bot.add_cog(TransferCog(bot))

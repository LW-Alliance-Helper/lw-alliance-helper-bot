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
        col_idx = hidx.get(transfer._norm_header(self.status_col))
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
             "skipped_on_sheet", "copied", "enriched", "error"}]}
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
            logger.warning("[TRANSFER] guild %s: alliance read failed: %s", gid, e)
            _capture(e)
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
            src["error"] = config.describe_sheet_error(e)
            logger.warning("[TRANSFER] guild %s: %s source read failed: %s", gid, prefix, e)
            _capture(e)
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
                src["error"] = f"append failed: {config.describe_sheet_error(e)}"
                logger.warning("[TRANSFER] guild %s: append to alliance sheet failed: %s", gid, e)
                _capture(e)
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
                logger.warning("[TRANSFER] guild %s: enrich write failed: %s", gid, e)
                _capture(e)

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
            # sheet every minute (and keep the seen-state intact).
            config.update_transfer_config_field(gid, "last_polled_at", now_iso)
            logger.warning("[TRANSFER] guild %s: sheet read failed: %s", gid, e)
            _capture(e)
            return

        # Optional source pulls (server-wide / intake form): copy matching whole
        # rows into the alliance sheet, aligned to its columns and deduped across
        # polls, then re-read so the copied rows surface as new applicants now.
        try:
            copied = (await copy_sources(cfg, header))["copied"]
        except Exception as e:  # noqa: BLE001
            logger.warning("[TRANSFER] guild %s: source copy failed: %s", gid, e)
            _capture(e)
            copied = 0
        if copied:
            try:
                header, data_rows = await asyncio.to_thread(
                    transfer_sheets.read_sheet, sheet_id, tab
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("[TRANSFER] guild %s: re-read after copy failed: %s", gid, e)
                _capture(e)

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
            _capture(e)
            return {"error": f"Couldn't read your sheet: {config.describe_sheet_error(e)}"}

        try:
            src_report = await copy_sources(cfg, header)
        except Exception as e:  # noqa: BLE001
            _capture(e)
            src_report = {"copied": 0, "enriched": 0, "sources": []}
        if src_report.get("copied"):
            try:
                header, data_rows = await asyncio.to_thread(
                    transfer_sheets.read_sheet, sheet_id, tab
                )
            except Exception as e:  # noqa: BLE001
                _capture(e)

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

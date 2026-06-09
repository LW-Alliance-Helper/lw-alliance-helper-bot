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

Source-copy (server-wide / form pulls) and decision write-back attach here
once the wizard configures them; this slice covers the alliance-sheet watch.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import discord
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


# ── Embeds ────────────────────────────────────────────────────────────────────


def _new_applicant_embed(name: str, display_pairs: list) -> discord.Embed:
    embed = discord.Embed(
        title=f"📥 New transfer applicant: {name}"[:256], color=discord.Color.green()
    )
    body = "\n".join(f"**{h}:** {v}" for h, v in display_pairs)
    embed.description = body[:4000] if body else "*(no display columns configured)*"
    return embed


def _status_change_embed(name: str, changes: list) -> discord.Embed:
    embed = discord.Embed(title=f"🔔 {name} — status changed"[:256], color=discord.Color.blue())
    lines = [
        f"**{field}:** {(old or '(blank)')} → {(new or '(blank)')}" for field, old, new in changes
    ]
    embed.description = "\n".join(lines)[:4000]
    return embed


def _removal_embed(name: str, snapshot: dict) -> discord.Embed:
    embed = discord.Embed(title=f"🗑️ {name} removed from the sheet"[:256], color=discord.Color.red())
    last = ", ".join(f"{k}: {v}" for k, v in (snapshot or {}).items() if str(v).strip())
    embed.description = f"They'd been marked — {last}." if last else "Removed from your sheet."
    return embed


def _full_details_embed(name: str, header: list, row: list) -> discord.Embed:
    """Every column of the sheet row, one field per line (Decision J)."""
    embed = discord.Embed(title=f"📄 {name} — full record"[:256], color=discord.Color.greyple())
    lines = []
    for i, h in enumerate(header):
        if not str(h).strip():
            continue
        if i < len(row):
            cell = row[i]
            val = cell.strip() if isinstance(cell, str) else str(cell)
        else:
            val = ""
        lines.append(f"**{h}:** {val or '—'}")
    embed.description = "\n".join(lines)[:4000]
    return embed


# ── Notice view (full details + draft-a-message) ──────────────────────────────


class _NoticeView(discord.ui.View):
    def __init__(self, *, guild_id, name, header, row, display_pairs, template_kinds):
        super().__init__(timeout=_NOTICE_TIMEOUT)
        self.guild_id = guild_id
        self.name = name
        self.header = header
        self.row = row
        self.display_pairs = display_pairs
        self.message: discord.Message | None = None

        details = discord.ui.Button(label="📄 Full details", style=discord.ButtonStyle.secondary)
        details.callback = self._full_details
        self.add_item(details)
        for kind in template_kinds:
            btn = discord.ui.Button(
                label=_TEMPLATE_BTN.get(kind, "📩 Message"), style=discord.ButtonStyle.primary
            )
            btn.callback = self._make_template_cb(kind)
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


# ── The cog ───────────────────────────────────────────────────────────────────


class TransferCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

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
            cfg.get("last_polled_at"), cfg.get("poll_frequency_minutes") or 60, now
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

        await self._post(
            channel,
            gid,
            header,
            hidx,
            column_map,
            diff,
            cfg.get("notification_style") or "each",
            bool(cfg.get("notify_on_delete")),
        )
        config.update_transfer_config_fields(
            gid,
            last_seen_state_json=json.dumps(diff.next_state),
            last_polled_at=now_iso,
        )

    async def _post(self, channel, gid, header, hidx, column_map, diff, style, notify_on_delete):
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
                lines.append(f"• **{name}**" + (f" — {summary}" if summary else ""))
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
                chg = ", ".join(f"{f}: {n or '(blank)'}" for f, _o, n in sc.changes)
                lines.append(f"• **{name}** — {chg}")
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

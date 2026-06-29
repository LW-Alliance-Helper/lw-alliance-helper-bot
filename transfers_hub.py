"""Transfer Management (#16) — the `/transfers` hub (front door).

One command opens a Premium-gated hub: a status embed + buttons that
dispatch into Setup Transfers (the wizard) and a read-only "current
applicants" view of the watched sheet. Modelled on `train_hub.py`. The whole
feature is Premium, so a free-tier `/transfers` renders the upsell rather
than a half-open hub (Decision B).
"""

from __future__ import annotations

import asyncio
import logging

import discord

import config
import premium
import transfer
import transfer_sheets
import wizard_registry

logger = logging.getLogger(__name__)

TRANSFERS_HUB_CMD = "/transfers"
_DENY_NOT_OWNER = "⛔ Only the person who opened this hub can use these buttons."

# Mirrors transfer_setup._MODE_LABELS (kept local to avoid importing the wizard
# module at hub-load time; the wizard is imported lazily on the Setup button).
_MODE_LABELS = {
    "source_to_own": "A shared sheet that populates my own sheet",
    "own": "My own sheet",
    "watch": "A shared sheet I watch",
}


# ── Embeds ────────────────────────────────────────────────────────────────────


def _hub_embed(cfg: dict, configured: bool) -> discord.Embed:
    embed = discord.Embed(title="🔁 Transfer Management", color=discord.Color.blurple())
    if not configured:
        embed.description = (
            "Not set up yet. Point the bot at your recruiting sheet and it'll watch for new "
            "applicants and status changes, draft your in-game messages, and (optionally) pull "
            "matching players from a server-wide sheet.\n\nClick **⚙️ Setup Transfers** to start."
        )
        return embed

    column_map = transfer.parse_column_map(cfg.get("alliance_column_map_json"))
    enabled = bool(cfg.get("enabled"))
    chan = cfg.get("notification_channel_id") or 0
    freq = cfg.get("poll_frequency_minutes") or 30
    style = (
        "a message per applicant"
        if (cfg.get("notification_style") or "each") == "each"
        else "a digest"
    )
    embed.description = (
        f"{'✅ **Active**' if enabled else '⏸️ **Set up but not active**'}. Watching "
        f"**{cfg.get('alliance_sheet_tab') or '?'}** every {freq} min, posting to "
        f"{f'<#{chan}>' if chan else '*no channel set*'} as {style}."
    )
    mode_label = _MODE_LABELS.get(cfg.get("setup_mode") or "")
    if mode_label:
        embed.add_field(name="Setup type", value=mode_label, inline=False)
    embed.add_field(name="Columns", value=transfer.summarize_column_map(column_map), inline=False)
    extras = []
    if cfg.get("server_wide_enabled"):
        extras.append("shared-sheet pull ✅")
    if cfg.get("alliance_form_enabled"):
        extras.append("form pull ✅")
    if cfg.get("notify_on_delete"):
        extras.append("removal notices ✅")
    if extras:
        embed.add_field(name="Extras", value=" · ".join(extras), inline=False)
    embed.set_footer(text="Buttons below")
    return embed


def _check_report_embed(report: dict) -> discord.Embed:
    """Render the 🔄 Check now breakdown: per-source pull counts + notices posted,
    so leadership can see exactly where applicants are (or aren't) coming through."""
    if report.get("error"):
        return discord.Embed(
            title="🔄 Check now", description=f"⚠️ {report['error']}", color=discord.Color.red()
        )
    embed = discord.Embed(title="🔄 Check now — results", color=discord.Color.blurple())
    lines = []
    for s in report.get("sources", []):
        name = "Shared sheet" if s.get("prefix") == "server_wide" else "Intake form"
        if s.get("error"):
            lines.append(f"**{name}:** ⚠️ {s['error']}")
            continue
        extra = f" · {s['enriched']} cell(s) filled" if s.get("enriched") else ""
        lines.append(
            f"**{name}:** read {s.get('read', 0)} · {s.get('matched', 0)} matched filter · "
            f"{s.get('already_pulled', 0)} already pulled · "
            f"{s.get('skipped_on_sheet', 0)} already on sheet · **{s.get('copied', 0)} copied**{extra}"
        )
    if not lines:
        lines.append("No source sheets connected (the shared-sheet pull is off).")
    embed.add_field(name="Pull from sources", value="\n".join(lines)[:1024], inline=False)

    posted = []
    if report.get("new"):
        posted.append(f"{report['new']} new-applicant")
    if report.get("status"):
        posted.append(f"{report['status']} status-change")
    if report.get("removed"):
        posted.append(f"{report['removed']} removal")
    embed.add_field(
        name="Notices posted" if report.get("posted") else "Notices",
        value=(", ".join(posted) + " notice(s)") if posted else "Nothing new to post.",
        inline=False,
    )
    embed.set_footer(
        text=f"{report.get('applicants_on_sheet', 0)} applicant(s) currently on your sheet"
    )
    return embed


def _applicants_embed(header, rows, hidx, name_header, display_headers) -> discord.Embed:
    embed = discord.Embed(title="📋 Current applicants", color=discord.Color.blurple())
    count = 0
    lines = []
    for row in rows:
        name = transfer.cell_for(row, hidx, name_header)
        if not name:
            continue
        count += 1
        if count <= 25:
            pairs = transfer.display_fields(row, hidx, display_headers)
            summary = " · ".join(str(v) for _h, v in pairs[:3])
            lines.append(f"**{name}**" + (f": {summary}" if summary else ""))
    if not count:
        embed.description = "*No applicants on the sheet right now.*"
        return embed
    body = "\n".join(lines)
    if count > 25:
        body += f"\n\n*…and {count - 25} more. Full data lives in your sheet.*"
    embed.description = body[:4000]
    embed.set_footer(text=f"{count} applicant(s) on the sheet")
    return embed


# ── Hub view ──────────────────────────────────────────────────────────────────


class _TransfersHubView(discord.ui.View):
    def __init__(self, bot, guild_id: int, owner_id: int, *, configured: bool):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.message: discord.Message | None = None

        if configured:
            view_btn = discord.ui.Button(
                label="📋 View applicants", style=discord.ButtonStyle.primary
            )
            view_btn.callback = self._view_applicants
            self.add_item(view_btn)
            check_btn = discord.ui.Button(label="🔄 Check now", style=discord.ButtonStyle.primary)
            check_btn.callback = self._check_now
            self.add_item(check_btn)
            setup_btn = discord.ui.Button(
                label="⚙️ Setup Transfers", style=discord.ButtonStyle.secondary
            )
        else:
            setup_btn = discord.ui.Button(
                label="⚙️ Setup Transfers", style=discord.ButtonStyle.primary
            )
        setup_btn.callback = self._setup
        self.add_item(setup_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        await wizard_registry.expire_view_message(
            self.message, command_hint=f"`{TRANSFERS_HUB_CMD}`"
        )

    async def _setup(self, interaction: discord.Interaction):
        from transfer_setup import _launch_transfer_setup

        await _launch_transfer_setup(interaction, self.bot)

    async def _check_now(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cog = self.bot.get_cog("TransferCog")
        if cog is None:
            await interaction.followup.send(
                "⚠️ The transfer watcher isn't running right now. Try again in a moment.",
                ephemeral=True,
            )
            return
        cfg = config.get_transfer_config(self.guild_id)
        try:
            report = await cog.check_now(cfg)
        except Exception as e:  # noqa: BLE001
            logger.warning("[TRANSFER] check-now failed for guild %s: %s", self.guild_id, e)
            await interaction.followup.send(
                f"⚠️ Check failed: {config.describe_sheet_error(e)}", ephemeral=True
            )
            return
        await interaction.followup.send(embed=_check_report_embed(report), ephemeral=True)

    async def _view_applicants(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_transfer_config(self.guild_id)
        sheet_id = (cfg.get("alliance_sheet_id") or "").strip()
        tab = (cfg.get("alliance_sheet_tab") or "").strip()
        column_map = transfer.parse_column_map(cfg.get("alliance_column_map_json"))
        if not sheet_id or not tab or not column_map.get("name"):
            await interaction.followup.send(
                "⚠️ No transfer sheet configured yet. Run **⚙️ Setup Transfers** first.",
                ephemeral=True,
            )
            return
        try:
            header, rows = await asyncio.to_thread(transfer_sheets.read_sheet, sheet_id, tab)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(
                f"⚠️ Couldn't read your sheet: {config.describe_sheet_error(e)}", ephemeral=True
            )
            return
        hidx = transfer.header_index(header)
        embed = _applicants_embed(
            header, rows, hidx, column_map.get("name"), column_map.get("display", []) or []
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ── Entry point ───────────────────────────────────────────────────────────────


async def handle_transfers_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/transfers`. Leadership + Premium gated; the
    whole feature is Premium, so free tier sees the upsell."""
    from setup_cog import _has_leadership_or_admin

    if not _has_leadership_or_admin(interaction):
        from config import get_config

        cfg = get_config(interaction.guild_id)
        role = (cfg.leadership_role_name if cfg else None) or "Leadership"
        await interaction.response.send_message(
            f"⛔ You need the **{role}** role (or admin) to use Transfer Management.",
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

    cfg = config.get_transfer_config(interaction.guild_id)
    configured = bool((cfg.get("alliance_sheet_id") or "").strip())
    embed = _hub_embed(cfg, configured)
    view = _TransfersHubView(bot, interaction.guild_id, interaction.user.id, configured=configured)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()

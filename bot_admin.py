"""
bot_admin.py — the owner-only `/admin` diagnostic toolkit (#372, split out
of bot.py once it passed ~2000 lines).

Bot-operational metadata + cleanup. Gated by `bot.is_owner` so they're only
usable by whoever owns the Discord application (or its team), not by guild
admins. These read from `guild_install_metadata` (populated in
on_guild_join / on_ready, still in bot.py) and `guild_configs` to make it
possible to identify an alliance from a logged `guild_id` and to action a
data-removal request without a Railway shell session. Slash commands take
guild IDs as strings -- snowflakes can exceed JavaScript's safe-integer
range.

Discord has no "application-owner-only visibility" tier -- the
`bot.is_owner` check only blocks *execution*, not *discoverability*. To
keep these commands out of the autocomplete picker in every alliance,
registration is scoped to the guilds listed in `BOT_ADMIN_GUILD_IDS`
(comma-separated env var; same parsing as `PREMIUM_BYPASS_GUILD_IDS`). When
the env var is unset (local dev) the commands fall back to global
registration.

Reads `bot` (the live Bot instance), `ET`, and `_try_assign_verified` via
`bot_state` -- NOT `from bot import ...`. Railway runs `python bot.py`
directly, which loads it as `__main__`; a plain `from bot import X` inside
a module bot.py itself imports re-executes bot.py a *second* time under
the name "bot" (since `sys.modules` has no "bot" entry yet when running as
__main__), which crashes -- see bot_state.py's module docstring and the
comment above the three `bot_state.X = ...` assignments in bot.py.
Registers `admin_group` on the tree itself at import time
(bot.tree.add_command(...) at the bottom of this file), so bot.py's own
bottom-of-file import is the only wiring it needs.
"""

import asyncio
import io
import json
import os
from datetime import datetime, date, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import discord
from discord import app_commands

from config import (
    get_config,
    get_app_setting,
    set_app_setting,
    get_guild_install_metadata,
    delete_guild_install_metadata,
)
import support_join_watch
import bot_state

bot = bot_state.bot
ET = bot_state.ET
_try_assign_verified = bot_state.try_assign_verified

# ── Owner-only diagnostic commands ─────────────────────────────────────────────
#
# Bot-operational metadata + cleanup. Gated by `bot.is_owner` so they're only
# usable by whoever owns the Discord application (or its team), not by guild
# admins. These read from `guild_install_metadata` (populated in
# on_guild_join / on_ready) and `guild_configs` to make it possible to
# identify an alliance from a logged `guild_id` and to action a data-removal
# request without a Railway shell session. Slash commands take guild IDs as
# strings — snowflakes can exceed JavaScript's safe-integer range.
#
# Discord has no "application-owner-only visibility" tier — the `bot.is_owner`
# check only blocks *execution*, not *discoverability*. To keep these
# commands out of the autocomplete picker in every alliance, registration is
# scoped to the guilds listed in `BOT_ADMIN_GUILD_IDS` (comma-separated env
# var; same parsing as `PREMIUM_BYPASS_GUILD_IDS`). When the env var is
# unset (local dev) the commands fall back to global registration so the
# developer doesn't have to think about it — production should always set
# the var.


def _admin_guild_ids() -> tuple[int, ...]:
    """Parse `BOT_ADMIN_GUILD_IDS` (comma-separated guild IDs). Returns an
    empty tuple if the env var is unset / blank, which means the admin
    commands register globally — intended for local dev only.
    """
    raw = os.environ.get("BOT_ADMIN_GUILD_IDS", "").strip()
    if not raw:
        return ()
    out: list[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if piece.isdigit():
            out.append(int(piece))
    return tuple(out)


_ADMIN_GUILD_IDS = _admin_guild_ids()
_admin_command_kwargs: dict = (
    {"guilds": [discord.Object(id=gid) for gid in _ADMIN_GUILD_IDS]} if _ADMIN_GUILD_IDS else {}
)
if not _ADMIN_GUILD_IDS:
    print(
        "[INFO] BOT_ADMIN_GUILD_IDS unset — owner-only admin commands "
        "will register globally. Set this in production to scope them "
        "to specific guilds."
    )
else:
    print(f"[INFO] Owner-only admin commands restricted to guild(s): {_ADMIN_GUILD_IDS}")


async def _require_bot_owner(interaction: discord.Interaction) -> bool:
    """Send an ephemeral reject if the caller isn't the application owner."""
    if await bot.is_owner(interaction.user):
        return True
    await interaction.response.send_message(
        "⛔ This command is restricted to the bot owner.", ephemeral=True
    )
    return False


def _parse_guild_id(raw: str) -> int | None:
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return None


# /admin command group — owner-only, scoped to BOT_ADMIN_GUILD_IDS.
# Defined as a module-level Group rather than via a cog because the
# bookkeeping (env-var guild scoping, bot.is_owner check) needs the
# already-instantiated `bot` here in bot.py. Registered on the tree
# at the bottom of this admin section so the @admin_group.command
# decorators below can attach to it.
admin_group = app_commands.Group(
    name="admin",
    description="(Bot owner only) Support + data-removal utilities",
)


@admin_group.command(
    name="overview",
    description="(Bot owner only) Fleet snapshot — total guilds, Premium counts, recent installs, stragglers",
)
async def admin_overview_slash(interaction: discord.Interaction):
    if not await _require_bot_owner(interaction):
        return

    from config import _get_conn  # noqa: PLC0415 — module-level imports already loaded

    with _get_conn() as conn:
        total_guilds = conn.execute("SELECT COUNT(*) FROM guild_install_metadata").fetchone()[0]
        with_setup_complete = conn.execute(
            "SELECT COUNT(*) FROM guild_configs WHERE setup_complete = 1"
        ).fetchone()[0]
        premium_assignments = conn.execute("SELECT COUNT(*) FROM premium_assignments").fetchone()[0]
        # Recent installs: last 7 days. Use ISO timestamp comparison
        # (TEXT-sorted, ISO-8601 is lexicographically ordered).
        cutoff_recent = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        recent_rows = conn.execute(
            "SELECT guild_id, guild_name, installed_at FROM guild_install_metadata "
            "WHERE installed_at >= ? ORDER BY installed_at DESC LIMIT 10",
            (cutoff_recent,),
        ).fetchall()
        # Stale stragglers: no on_ready ping in 14+ days.
        cutoff_stale = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        stale_rows = conn.execute(
            "SELECT guild_id, guild_name, last_seen_at FROM guild_install_metadata "
            "WHERE last_seen_at < ? ORDER BY last_seen_at ASC LIMIT 10",
            (cutoff_stale,),
        ).fetchall()

    embed = discord.Embed(
        title="🛠️ Admin Overview",
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Fleet",
        value=(
            f"**Installed guilds:** {total_guilds}\n"
            f"**Completed setup:** {with_setup_complete}\n"
            f"**Premium assignments:** {premium_assignments}"
        ),
        inline=False,
    )
    if recent_rows:
        lines = [
            f"• **{r['guild_name'] or '(unnamed)'}** (`{r['guild_id']}`) — {r['installed_at'][:10]}"
            for r in recent_rows
        ]
        embed.add_field(
            name=f"Recent installs (last 7 days, top {len(recent_rows)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name="Recent installs (last 7 days)",
            value="*(none)*",
            inline=False,
        )
    if stale_rows:
        lines = [
            f"• **{r['guild_name'] or '(unnamed)'}** (`{r['guild_id']}`) — last seen {r['last_seen_at'][:10]}"
            for r in stale_rows
        ]
        embed.add_field(
            name=f"No on_ready in 14+ days (top {len(stale_rows)})",
            value="\n".join(lines)[:1024],
            inline=False,
        )
    embed.set_footer(
        text="Use /admin guild_info <id> to drill into one guild, "
        "or /admin forget_guild <id> to remove install metadata for a data-removal request."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@admin_group.command(
    name="guild_info",
    description="(Bot owner only) Look up stored metadata + config for a guild_id.",
)
@app_commands.describe(guild_id="Discord guild ID — paste from log line / Sentry tag")
async def admin_guild_info_slash(interaction: discord.Interaction, guild_id: str):
    if not await _require_bot_owner(interaction):
        return

    gid = _parse_guild_id(guild_id)
    if gid is None:
        await interaction.response.send_message(
            f"⚠️ `{guild_id}` isn't a valid integer guild ID.", ephemeral=True
        )
        return

    meta = get_guild_install_metadata(gid)
    cfg = get_config(gid)

    if meta is None and cfg is None:
        await interaction.response.send_message(
            f"ℹ️ No record found for guild `{gid}`. The bot may not be in it, "
            "or it joined before metadata tracking shipped and hasn't reconnected since.",
            ephemeral=True,
        )
        return

    title = (meta["guild_name"] if meta else None) or f"Guild {gid}"
    embed = discord.Embed(title=f"🔎 {title}", color=discord.Color.blurple())
    embed.add_field(name="Guild ID", value=f"`{gid}`", inline=False)

    if meta is not None:
        owner_line = (
            f"<@{meta['owner_id']}> (`{meta['owner_id']}`)" if meta["owner_id"] else "*unknown*"
        )
        embed.add_field(name="Owner", value=owner_line, inline=False)
        if meta["installer_user_id"]:
            embed.add_field(
                name="Installer",
                value=f"<@{meta['installer_user_id']}> (`{meta['installer_user_id']}`)",
                inline=False,
            )
        else:
            embed.add_field(
                name="Installer",
                value="*not captured (joined before metadata tracking, or audit log unavailable)*",
                inline=False,
            )
        embed.add_field(name="First seen", value=meta["installed_at"], inline=True)
        embed.add_field(name="Last seen", value=meta["last_seen_at"], inline=True)
    else:
        embed.add_field(
            name="Install metadata",
            value="*missing — guild has a config row but no metadata record yet (will appear on next reconnect)*",
            inline=False,
        )

    if cfg is not None:
        embed.add_field(
            name="Setup complete", value="✅" if cfg.setup_complete else "❌", inline=True
        )
        embed.add_field(name="Timezone", value=cfg.timezone or "*not set*", inline=True)
        embed.add_field(
            name="Leadership role", value=cfg.leadership_role_name or "*not set*", inline=False
        )
        sheet_id = (cfg.spreadsheet_id or "").strip()
        if sheet_id:
            sheet_link = f"[`{sheet_id}`](https://docs.google.com/spreadsheets/d/{sheet_id})"
            embed.add_field(name="Sheet", value=sheet_link, inline=False)
        else:
            embed.add_field(name="Sheet", value="*not configured*", inline=False)
    else:
        embed.add_field(
            name="Configuration",
            value="*no `guild_configs` row — bot is installed but `/setup` was never completed*",
            inline=False,
        )

    await interaction.response.send_message(embed=embed, ephemeral=True)


class _ForgetGuildConfirm(discord.ui.View):
    """Two-button confirm for /admin forget_guild. Auto-cancels on timeout."""

    def __init__(self, guild_id: int, owner_id: int):
        super().__init__(timeout=60)
        self._guild_id = guild_id
        self._owner_id = owner_id
        self._handled = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "⛔ Only the bot owner who started this can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🗑️ Delete metadata row", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        self._handled = True
        for item in self.children:
            item.disabled = True
        deleted = delete_guild_install_metadata(self._guild_id)
        msg = (
            f"✅ Cleared install metadata for `{self._guild_id}`."
            if deleted
            else f"ℹ️ No metadata row for `{self._guild_id}` (already absent)."
        )
        await inter.response.edit_message(content=msg, view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        self._handled = True
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(
            content=f"❌ Cancelled — `{self._guild_id}` metadata left intact.",
            view=self,
        )
        self.stop()


@admin_group.command(
    name="forget_guild",
    description="(Bot owner only) Delete the install-metadata row for a guild_id (data-removal request).",
)
@app_commands.describe(guild_id="Discord guild ID to forget")
async def admin_forget_guild_slash(interaction: discord.Interaction, guild_id: str):
    if not await _require_bot_owner(interaction):
        return

    gid = _parse_guild_id(guild_id)
    if gid is None:
        await interaction.response.send_message(
            f"⚠️ `{guild_id}` isn't a valid integer guild ID.", ephemeral=True
        )
        return

    meta = get_guild_install_metadata(gid)
    if meta is None:
        await interaction.response.send_message(
            f"ℹ️ No metadata row for `{gid}` — nothing to delete.", ephemeral=True
        )
        return

    name = meta.get("guild_name") or "(unnamed)"
    view = _ForgetGuildConfirm(guild_id=gid, owner_id=interaction.user.id)
    await interaction.response.send_message(
        f"⚠️ About to delete the install-metadata row for **{name}** (`{gid}`). "
        f"`guild_configs` and other tables are untouched — clear those separately "
        f"if the request covers full config wipe. Confirm?",
        view=view,
        ephemeral=True,
    )


# The other half of `forget_guild` (#517). That one clears a guild's install
# metadata row; this is the route for a person, which nothing had before --
# `remove_premium_assignment` was the only user-keyed delete anywhere in the
# tree and it exists for subscription management.
#
# Preview first, then confirm, because this runs delete statements against
# production on somebody else's say-so and the counts are the only thing that
# says the ID was right. The preview and the run share every predicate (see
# `config.purge_user_data`), so the preview cannot promise something the run
# does not do.
#
# The receipt is the post-run counts rather than the preview's. Nothing stops a
# sign-up landing between the two, and what gets written back to the person has
# to be what happened, not what was going to.


def _parse_user_id(raw: str) -> int | None:
    """Same parse as `_parse_guild_id`, named for what it reads. Snowflakes
    arrive as strings because they exceed JavaScript's safe-integer range."""
    return _parse_guild_id(raw)


def _removal_lines(counts: dict) -> str:
    """One line per table, widest first. Nothing renders as an em dash rather
    than an empty string, which Discord rejects as a field value."""
    if not counts:
        return "—"
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return "\n".join(f"`{table}` {n}" for table, n in ordered)


def _removal_embed(
    uid: int,
    label: str,
    config_result: dict,
    cd_result: dict,
    cd_error: str | None,
) -> discord.Embed:
    """One removal report. Same shape for the preview and the receipt, because
    the operator reads the second against the first."""
    deleted = dict(config_result["deleted"])
    deleted.update(cd_result.get("deleted", {}))
    scrubbed = dict(config_result["scrubbed"])
    scrubbed.update(cd_result.get("scrubbed", {}))
    applied = bool(config_result.get("applied"))
    n_deleted = sum(deleted.values())
    n_scrubbed = sum(scrubbed.values())

    embed = discord.Embed(
        title="Data removal" if applied else "Data removal preview",
        description=f"**{label}**\n`{uid}`" if label else f"`{uid}`",
        color=discord.Color.red() if applied else discord.Color.orange(),
    )
    embed.add_field(
        name=f"{'Deleted' if applied else 'To delete'} ({n_deleted})",
        value=_removal_lines(deleted),
        inline=True,
    )
    embed.add_field(
        name=f"{'Scrubbed' if applied else 'To scrub'} ({n_scrubbed})",
        value=_removal_lines(scrubbed),
        inline=True,
    )
    if cd_error:
        embed.add_field(name="Champion Duel database", value=f"Not reached: `{cd_error}`")
    if not (n_deleted or n_scrubbed or cd_error):
        embed.set_footer(text="Nothing held under this ID in either database.")
    elif "guild_install_metadata" in scrubbed:
        # Worth saying every time it applies: on_ready rewrites owner_id for
        # every guild the bot is in, so that column comes back on the next boot
        # for as long as the install stands.
        embed.set_footer(text="owner_id returns on the next boot while the bot is in that guild.")
    return embed


def _run_user_removal(uid: int, *, apply: bool) -> tuple[dict, dict, str | None]:
    """Both databases, one call. Returns `(config_result, cd_result, cd_error)`.

    The Champion Duel half is caught separately so a failure there cannot leave
    the operator believing the config half did not run either. A removal that
    half-happened and reported nothing is the worst outcome available here.
    """
    import config  # noqa: PLC0415

    config_result = config.purge_user_data(uid, apply=apply)

    import champion_duel_db as cd_db  # noqa: PLC0415

    try:
        cd_result = cd_db.purge_user_data(uid, apply=apply)
    except Exception as exc:
        return config_result, {"deleted": {}, "scrubbed": {}, "applied": apply}, str(exc)
    return config_result, cd_result, None


class _ForgetUserConfirm(discord.ui.View):
    """Two-button confirm for /admin forget_user. Auto-cancels on timeout."""

    def __init__(self, user_id: int, owner_id: int, label: str):
        super().__init__(timeout=120)
        self._user_id = user_id
        self._owner_id = owner_id
        self._label = label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "⛔ Only the bot owner who started this can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🗑️ Run removal", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        config_result, cd_result, cd_error = _run_user_removal(self._user_id, apply=True)

        # Premium is cached per guild and per user and the assignment row may
        # have just gone. Clearing the whole cache is blunt, but a removal
        # request is a rare owner action and a stale premium read is not worth
        # the precision.
        import premium  # noqa: PLC0415

        premium.clear_cache()

        await inter.response.edit_message(
            content=f"🗑️ Ran data removal for `{self._user_id}`.",
            embed=_removal_embed(self._user_id, self._label, config_result, cd_result, cd_error),
            view=self,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(
            content=f"❌ Cancelled — nothing removed for `{self._user_id}`.",
            embed=None,
            view=self,
        )
        self.stop()


@admin_group.command(
    name="forget_user",
    description="(Bot owner only) Action a data-removal request for a Discord user ID.",
)
@app_commands.describe(user_id="Discord user ID making the removal request")
async def admin_forget_user_slash(interaction: discord.Interaction, user_id: str):
    """Preview, then run, a data removal for one person across both databases.

    Champion Duel *player* records are out of scope by the #499 decision. This
    reaches people with Discord identities only, which is what the privacy
    policy's removal promise is about.
    """
    if not await _require_bot_owner(interaction):
        return

    uid = _parse_user_id(user_id)
    if uid is None:
        await interaction.response.send_message(
            f"⚠️ `{user_id}` isn't a valid integer user ID.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    # Worth one REST call: the operator is about to delete rows on the strength
    # of an ID somebody typed, and a name is the only check available that the
    # ID is the person who asked.
    user = bot.get_user(uid)
    if user is None:
        try:
            user = await bot.fetch_user(uid)
        except Exception:
            # Anything at all. The name is a check on the ID, not part of the
            # removal, and a lookup that fails must not be what stops a request
            # from being actioned.
            user = None
    label = user.name if user is not None else ""

    config_result, cd_result, cd_error = _run_user_removal(uid, apply=False)
    embed = _removal_embed(uid, label, config_result, cd_result, cd_error)
    total = (
        sum(config_result["deleted"].values())
        + sum(config_result["scrubbed"].values())
        + sum(cd_result.get("deleted", {}).values())
        + sum(cd_result.get("scrubbed", {}).values())
    )
    if not total:
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    view = _ForgetUserConfirm(user_id=uid, owner_id=interaction.user.id, label=label)
    await interaction.followup.send(
        content=(
            "⚠️ Deletes are permanent and scrubs cannot be undone. "
            "`/admin forget_guild` is the separate route for a guild's install record."
        ),
        embed=embed,
        view=view,
        ephemeral=True,
    )


@admin_group.command(
    name="shiny_servers",
    description="(Bot owner only) Dump stored shiny_task_servers rows for a server-number range.",
)
@app_commands.describe(
    min_server="Lowest server number to include (inclusive)",
    max_server="Highest server number to include (inclusive)",
)
async def admin_shiny_servers_slash(
    interaction: discord.Interaction, min_server: int, max_server: int
):
    """Spot-check the frozen shiny_task_servers snapshot against the source.
    Owner-only debug tool: lists each stored server's creation_date, whether
    it's shiny on the current in-game day, and flags rows missing from the
    range — so a drift between the snapshot and reality is visible at a glance.
    """
    if not await _require_bot_owner(interaction):
        return

    if min_server > max_server:
        min_server, max_server = max_server, min_server

    from config import _get_conn, server_date_for  # noqa: PLC0415
    from shiny_tasks import is_shiny_today  # noqa: PLC0415

    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT server_number, creation_date, last_seen_at "
            "FROM shiny_task_servers WHERE server_number BETWEEN ? AND ? "
            "ORDER BY server_number",
            (min_server, max_server),
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            f"ℹ️ No `shiny_task_servers` rows stored between **{min_server}** and **{max_server}**.",
            ephemeral=True,
        )
        return

    # "Today" = the Last War in-game (server, UTC-2) date — the same date the
    # live post loop uses (see config.server_date_for) — so the Shiny? column
    # matches what the bot would announce right now and is directly checkable
    # against the source's "Shiny Tasks" column.
    server_today = server_date_for(datetime.now(timezone.utc))

    header = f"{'Server':>6}  {'Created':<10}  {'Shiny?':<6}  {'Last seen':<10}"
    table = [header, "-" * len(header)]
    shiny_nums: list[int] = []
    for r in rows:
        cd = (r["creation_date"] or "")[:10]
        try:
            is_shiny = is_shiny_today(date.fromisoformat(cd), server_today)
        except ValueError:
            is_shiny = False
        if is_shiny:
            shiny_nums.append(r["server_number"])
        table.append(
            f"{r['server_number']:>6}  {cd:<10}  {'yes' if is_shiny else 'no':<6}  "
            f"{(r['last_seen_at'] or '')[:10]:<10}"
        )

    present = {r["server_number"] for r in rows}
    missing = [n for n in range(min_server, max_server + 1) if n not in present]

    summary = (
        f"**Shiny snapshot {min_server}–{max_server}** · in-game date "
        f"`{server_today.isoformat()}`\n"
        f"{len(rows)} stored, {len(missing)} missing in range · "
        f"{len(shiny_nums)} shiny today"
    )
    detail = (
        f"Shiny today: {', '.join(map(str, shiny_nums)) or '(none)'}\n"
        f"Missing rows: {', '.join(map(str, missing)) or '(none)'}\n\n" + "\n".join(table)
    )

    full = f"{summary}\n```\n{detail}\n```"
    if len(full) <= 1900:
        await interaction.response.send_message(full, ephemeral=True)
    else:
        import io  # noqa: PLC0415

        fp = io.BytesIO(detail.encode("utf-8"))
        await interaction.response.send_message(
            content=f"{summary}\n*(full table attached)*",
            file=discord.File(fp, filename=f"shiny_servers_{min_server}_{max_server}.txt"),
            ephemeral=True,
        )


@admin_group.command(
    name="shiny_import",
    description="(Bot owner only) Bulk-replace the shiny server snapshot from an attached JSON export.",
)
@app_commands.describe(
    file="JSON array of server records (id + timestamp + region) captured from the source.",
)
async def admin_shiny_import_slash(interaction: discord.Interaction, file: discord.Attachment):
    """One-shot correction of the frozen shiny_task_servers snapshot (#331).
    Parses the attached JSON, derives every creation date in server time
    (UTC-2) so they match the source, and upserts the whole set — fixing
    drifted dates and adding new servers in a single push. Servers absent from
    the file keep their rows but age out of posts via the 30-day soft-delete
    filter (same as the old refresh).
    """
    if not await _require_bot_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)

    from config import upsert_shiny_task_servers  # noqa: PLC0415
    from shiny_tasks import parse_server_records_json  # noqa: PLC0415

    try:
        raw = await file.read()
        rows = parse_server_records_json(raw.decode("utf-8"))
    except UnicodeDecodeError:
        await interaction.followup.send(
            f"⚠️ `{file.filename}` isn't UTF-8 text — attach the raw JSON export.",
            ephemeral=True,
        )
        return
    except ValueError as e:  # includes json.JSONDecodeError
        await interaction.followup.send(
            f"⚠️ Couldn't parse `{file.filename}` as JSON: {e}", ephemeral=True
        )
        return

    if not rows:
        await interaction.followup.send(
            "⚠️ Parsed 0 usable server records — the file should be a JSON array "
            "of objects each with an `id` and a `timestamp`.",
            ephemeral=True,
        )
        return

    n = upsert_shiny_task_servers(rows, seen_at=datetime.now(timezone.utc).isoformat())
    lo, hi = min(r[0] for r in rows), max(r[0] for r in rows)
    await interaction.followup.send(
        f"✅ Imported **{n}** server rows (ids {lo}–{hi}). Creation dates derived "
        f"in server time (UTC-2). Spot-check with `/admin shiny_servers`.",
        ephemeral=True,
    )


@admin_group.command(
    name="shiny_set",
    description="(Bot owner only) Add or correct one server's creation date in the shiny snapshot.",
)
@app_commands.describe(
    server="Server number (e.g. 2286)",
    creation_date="Creation date in YYYY-MM-DD (server time) — match the date the source shows",
    region="Region label (optional; defaults to global)",
)
async def admin_shiny_set_slash(
    interaction: discord.Interaction,
    server: int,
    creation_date: str,
    region: str = "global",
):
    """Add a newly-launched server, or correct one row, in the shiny snapshot.
    Single-row upsert — leaves every other server untouched."""
    if not await _require_bot_owner(interaction):
        return
    try:
        d = date.fromisoformat(creation_date.strip())
    except ValueError:
        await interaction.response.send_message(
            f"⚠️ `{creation_date}` isn't a valid date — use `YYYY-MM-DD` "
            "(the creation date the source shows for that server).",
            ephemeral=True,
        )
        return

    from config import upsert_shiny_task_servers  # noqa: PLC0415

    reg = region.strip() or "global"
    upsert_shiny_task_servers(
        [(server, d.isoformat(), reg)],
        seen_at=datetime.now(timezone.utc).isoformat(),
    )
    await interaction.response.send_message(
        f"✅ Set server **{server}** → creation date `{d.isoformat()}` (region {reg}).",
        ephemeral=True,
    )


@admin_group.command(
    name="shiny_dump",
    description="(Bot owner only) Dump a guild's raw shiny_tasks_config row + live channel resolution.",
)
@app_commands.describe(guild_id="Discord guild ID")
async def admin_shiny_dump_slash(interaction: discord.Interaction, guild_id: str):
    """Raw `guild_shiny_tasks_config` row for one guild — including
    `last_posted_date`, which the friendly `/setup` view never shows — plus
    whether `bot.get_channel` currently resolves the configured channel.
    Read-only, for chasing silent no-posts where the per-minute loop logged
    nothing (a clean send and a dedup-skip both produce zero log output)."""
    if not await _require_bot_owner(interaction):
        return
    gid = _parse_guild_id(guild_id)
    if gid is None:
        await interaction.response.send_message(
            f"⚠️ `{guild_id}` isn't a valid integer guild ID.", ephemeral=True
        )
        return

    from config import get_config, get_shiny_tasks_config  # noqa: PLC0415

    cfg = get_shiny_tasks_config(gid)
    channel_id = cfg.get("channel_id") or 0
    channel = bot.get_channel(channel_id)
    guild = bot.get_guild(gid)
    base_cfg = get_config(gid)

    try:
        tz = ZoneInfo(base_cfg.timezone or "America/New_York") if base_cfg else ET
    except Exception:  # noqa: BLE001
        tz = ET
    guild_now_str = datetime.now(tz=tz).isoformat(timespec="seconds")

    lines = [f"# Shiny Tasks dump — guild {gid}", ""]
    for k in sorted(cfg):
        lines.append(f"{k} = {cfg[k]!r}")
    lines.append("")
    lines.append(f"channel {channel_id} resolves via bot.get_channel: {channel!r}")
    lines.append(f"guild cached: {guild is not None}")
    lines.append(f"configured timezone: {base_cfg.timezone if base_cfg else '(no base config)'}")
    lines.append(f"guild-local now: {guild_now_str}")

    await interaction.response.send_message("```\n" + "\n".join(lines) + "\n```", ephemeral=True)


@admin_group.command(
    name="shiny_reset",
    description="(Bot owner only) Clear a guild's last_posted_date so today's post is eligible again.",
)
@app_commands.describe(guild_id="Discord guild ID")
async def admin_shiny_reset_slash(interaction: discord.Interaction, guild_id: str):
    """Debug/support tool: clears `last_posted_date` so the per-minute loop
    treats the guild as not-yet-posted today, without waiting for the
    calendar to roll over. Does not itself post anything."""
    if not await _require_bot_owner(interaction):
        return
    gid = _parse_guild_id(guild_id)
    if gid is None:
        await interaction.response.send_message(
            f"⚠️ `{guild_id}` isn't a valid integer guild ID.", ephemeral=True
        )
        return

    from config import mark_shiny_tasks_posted  # noqa: PLC0415

    mark_shiny_tasks_posted(gid, "")
    await interaction.response.send_message(
        f"✅ Cleared `last_posted_date` for guild **{gid}** — eligible on the next matching tick.",
        ephemeral=True,
    )


@admin_group.command(
    name="shiny_reset_all",
    description="(Bot owner only) Clear last_posted_date for every enabled Shiny Tasks guild.",
)
async def admin_shiny_reset_all_slash(interaction: discord.Interaction):
    """Bulk version of /admin shiny_reset. Every enabled guild silently
    got a no-op 'no shinies today' mark every night from 2026-07-17
    onward (the frozen-snapshot staleness bug fixed in e85688f), so all
    of them are sitting on a stale last_posted_date, not just whichever
    guild happened to be under investigation."""
    if not await _require_bot_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)

    from config import list_shiny_enabled_guild_ids, mark_shiny_tasks_posted  # noqa: PLC0415

    gids = list_shiny_enabled_guild_ids()
    for gid in gids:
        mark_shiny_tasks_posted(gid, "")
    await interaction.followup.send(
        f"✅ Cleared `last_posted_date` for **{len(gids)}** enabled guild(s) — "
        "all eligible on the next matching tick.",
        ephemeral=True,
    )


@admin_group.command(
    name="transfer_dump",
    description="(Bot owner only) Dump a guild's full Transfer Management setup + a live sheet probe.",
)
@app_commands.describe(guild_id="Discord guild ID")
async def admin_transfer_dump_slash(interaction: discord.Interaction, guild_id: str):
    """Everything the Transfer Management feature has saved for a guild, plus a
    live read of each configured sheet showing how the name column and filter
    actually resolve against the real headers and how many rows match. Sent as
    a file so nothing is truncated. Read-only — changes no state."""
    if not await _require_bot_owner(interaction):
        return
    gid = _parse_guild_id(guild_id)
    if gid is None:
        await interaction.response.send_message(
            f"⚠️ `{guild_id}` isn't a valid integer guild ID.", ephemeral=True
        )
        return
    await interaction.response.defer(ephemeral=True, thinking=True)

    import io  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    import transfer  # noqa: PLC0415
    import transfer_sheets  # noqa: PLC0415
    from config import get_transfer_config  # noqa: PLC0415

    cfg = get_transfer_config(gid)
    out: list[str] = [f"# Transfer Management dump — guild {gid}", ""]

    out.append("## Raw config")
    for k in sorted(cfg):
        out.append(f"{k} = {cfg[k]!r}")
    out.append("")

    def _dump_map(label: str, raw):
        m = transfer.parse_column_map(raw)
        out.append(f"## {label} column map")
        out.append(_json.dumps(m, indent=2, ensure_ascii=False))
        return m

    a_map = _dump_map("alliance", cfg.get("alliance_column_map_json"))
    sw_map = _dump_map("server_wide", cfg.get("server_wide_column_map_json"))
    form_map = _dump_map("alliance_form", cfg.get("alliance_form_column_map_json"))

    for label, raw in (
        ("notification", cfg.get("notification_filter_json")),
        ("server_wide", cfg.get("server_wide_filter_json")),
        ("alliance_form", cfg.get("alliance_form_filter_json")),
    ):
        f = transfer.parse_filter(raw)
        out.append(f"## {label} filter: {transfer.describe_filter(f)}")
        out.append(_json.dumps(f, indent=2, ensure_ascii=False))

    for label, key in (
        ("copied_state", "copied_state_json"),
        ("last_seen_state", "last_seen_state_json"),
    ):
        try:
            blob = _json.loads(cfg.get(key) or ("[]" if "copied" in key else "{}"))
            out.append(f"## {label}: {len(blob)} entries")
        except Exception as e:  # noqa: BLE001
            out.append(f"## {label}: unparseable ({e})")
    out.append("")

    async def _probe(label, sid, tab, colmap, filt):
        out.append(f"## LIVE probe — {label}: sheet={sid!r} tab={tab!r}")
        if not sid or not tab:
            out.append("(not configured)\n")
            return
        try:
            header, rows = await asyncio.to_thread(transfer_sheets.read_sheet, sid, tab)
        except Exception as e:  # noqa: BLE001
            from config import describe_sheet_error  # noqa: PLC0415

            out.append(f"READ FAILED: {describe_sheet_error(e)}\n")
            return
        hidx = transfer.header_index(header)
        out.append(f"header ({len(header)} cols): {header}")
        out.append(f"data rows: {len(rows)}")
        nm = colmap.get("name")
        out.append(f"name col {nm!r} → index {hidx.get(transfer.norm_header(nm)) if nm else None}")
        cmap = colmap.get("copy_map")
        if cmap:
            out.append(f"copy_map (target←source): {_json.dumps(cmap, ensure_ascii=False)}")
        if filt:
            matched = sum(1 for r in rows if transfer.evaluate_filter(filt, r, hidx))
            out.append(
                f"filter '{transfer.describe_filter(filt)}' → {matched}/{len(rows)} rows match"
            )
            for clause in filt.get("and", []):
                col = clause.get("column")
                idx = hidx.get(transfer.norm_header(col))
                samples = [transfer.cell_for(r, hidx, col) for r in rows[:8]]
                out.append(f"  clause column {col!r} → index {idx}; first values: {samples}")
        out.append("")

    await _probe(
        "alliance sheet",
        (cfg.get("alliance_sheet_id") or "").strip(),
        (cfg.get("alliance_sheet_tab") or "").strip(),
        a_map,
        transfer.parse_filter(cfg.get("notification_filter_json")),
    )
    if cfg.get("server_wide_enabled"):
        await _probe(
            "server_wide source",
            (cfg.get("server_wide_sheet_id") or "").strip(),
            (cfg.get("server_wide_sheet_tab") or "").strip(),
            sw_map,
            transfer.parse_filter(cfg.get("server_wide_filter_json")),
        )
    if cfg.get("alliance_form_enabled"):
        await _probe(
            "alliance_form source",
            (cfg.get("alliance_form_sheet_id") or "").strip(),
            (cfg.get("alliance_form_sheet_tab") or "").strip(),
            form_map,
            transfer.parse_filter(cfg.get("alliance_form_filter_json")),
        )

    text = "\n".join(str(x) for x in out)
    buf = io.BytesIO(text.encode("utf-8"))
    await interaction.followup.send(
        f"🔁 Transfer dump for guild `{gid}` ({len(text)} chars).",
        file=discord.File(buf, filename=f"transfer_dump_{gid}.txt"),
        ephemeral=True,
    )


# /admin verify is one overloaded command rather than a subcommand group so a
# bare `/admin verify` (no options) can run the scan — Discord doesn't allow a
# subcommand *group* to be invoked on its own. Passing `channel:` and/or `role:`
# configures those; `disable:` turns one (or both) off; no options = scan.
@admin_group.command(
    name="verify",
    description="(Bot owner only) Set the join-watch channel / Verified role, or (no options) run a scan.",
)
@app_commands.describe(
    channel="Set the hidden channel that receives join notices (its server = the watched server).",
    role="Set the role to auto-assign to joiners who share a bot-installed server.",
    disable="Turn off the watch channel, the Verified role, or both.",
)
async def admin_verify_slash(
    interaction: discord.Interaction,
    channel: discord.TextChannel | None = None,
    role: discord.Role | None = None,
    disable: Literal["channel", "role", "both"] | None = None,
):
    """Consolidated join-watch + auto-verify control. With options it updates
    config (stored in app_settings); with no options it runs the member scan
    (and, if a Verified role is set, backfills that role onto everyone who
    qualifies). See support_join_watch for the cross-guild logic.
    """
    if not await _require_bot_owner(interaction):
        return

    changes: list[str] = []

    # Disable first, so an explicit set in the same invocation still wins.
    if disable in ("channel", "both"):
        set_app_setting(support_join_watch.WATCH_CHANNEL_SETTING, None)
        changes.append("🔕 Join watch **disabled** — no join notices will post.")
    if disable in ("role", "both"):
        set_app_setting(support_join_watch.VERIFIED_ROLE_SETTING, None)
        changes.append("🔕 Auto-verify **disabled** — no role will be assigned.")

    if channel is not None:
        # Verify the bot can post there before saving, so a silent Forbidden at
        # join time doesn't go unnoticed.
        perms = channel.permissions_for(channel.guild.me)
        if not (perms.view_channel and perms.send_messages):
            await interaction.response.send_message(
                f"⚠️ I can't post in {channel.mention} — grant me **View Channel** + "
                f"**Send Messages** there first, then re-run this.",
                ephemeral=True,
            )
            return
        set_app_setting(support_join_watch.WATCH_CHANNEL_SETTING, str(channel.id))
        changes.append(
            f"📍 Watch channel set to {channel.mention}. New members of "
            f"**{channel.guild.name}** will be reported there."
        )

    if role is not None:
        if role.is_default():
            await interaction.response.send_message(
                "⚠️ `@everyone` can't be used as the Verified role.", ephemeral=True
            )
            return
        me = role.guild.me
        blocker = support_join_watch.verified_role_blocker(
            has_manage_roles=me.guild_permissions.manage_roles,
            bot_top_position=me.top_role.position,
            role_position=role.position,
            role_managed=role.managed,
        )
        set_app_setting(support_join_watch.VERIFIED_ROLE_SETTING, str(role.id))
        line = f"🏷️ Verified role set to **{role.name}**."
        if blocker:
            line += f"\n   ⚠️ Heads up: {blocker} — I can't assign it until that's fixed."
        changes.append(line)

    if changes:
        await interaction.response.send_message("\n".join(changes), ephemeral=True)
        return

    # No options → run the member scan (with Verified backfill if configured).
    await _run_verify_scan(interaction)


async def _run_verify_scan(interaction: discord.Interaction):
    """Backfill of the join watch: run the shared-server check against every
    current member of the watch channel's guild, assigning the Verified role
    (if configured) to everyone who qualifies. Sends a summary plus a full
    per-member breakdown as an attached file, zero-overlap members first.
    """
    raw = get_app_setting(support_join_watch.WATCH_CHANNEL_SETTING)
    if not raw:
        await interaction.response.send_message(
            "⚠️ No watch channel set. Configure one first with "
            "`/admin verify channel:#your-channel`.",
            ephemeral=True,
        )
        return

    channel = bot.get_channel(int(raw)) if raw.isdigit() else None
    if not isinstance(channel, discord.TextChannel):
        await interaction.response.send_message(
            "⚠️ The saved watch channel no longer resolves — re-set it with "
            "`/admin verify channel:#your-channel`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    guild = channel.guild
    raw_role = get_app_setting(support_join_watch.VERIFIED_ROLE_SETTING)
    role = guild.get_role(int(raw_role)) if raw_role and raw_role.isdigit() else None
    role_missing = bool(raw_role) and role is None

    members = [m for m in guild.members if not m.bot]
    none_lines: list[str] = []
    some_lines: list[str] = []
    assigned = already = failed = 0
    fail_reasons: dict[str, int] = {}

    for m in sorted(members, key=lambda m: str(m).lower()):
        shared = support_join_watch.shared_bot_guilds(bot, m.id, guild.id)
        if not shared:
            none_lines.append(f"{m} (ID {m.id})")
            continue
        tag = ""
        if role is not None:
            ok, note = await _try_assign_verified(m, role)
            if ok and note == "Assigned":
                assigned += 1
                tag = "  [+Verified]"
            elif ok:
                already += 1
                tag = "  [already Verified]"
            else:
                failed += 1
                fail_reasons[note] = fail_reasons.get(note, 0) + 1
                tag = f"  [FAILED: {note}]"
        some_lines.append(f"{m} (ID {m.id}) → {', '.join(g.name for g in shared)}{tag}")

    report = [
        f"Support-server member scan — {guild.name} (ID {guild.id})",
        f"Total non-bot members: {len(members)}",
        f"In NO other bot-installed server: {len(none_lines)}",
        f"In at least one bot-installed server: {len(some_lines)}",
    ]
    if role is not None:
        report.append(
            f"Verified role '{role.name}': {assigned} newly assigned, "
            f"{already} already had it, {failed} failed"
        )
        if fail_reasons:
            for reason, count in sorted(fail_reasons.items(), key=lambda kv: -kv[1]):
                report.append(f"    failure ({count}): {reason}")
    elif role_missing:
        report.append("Verified role: configured id no longer resolves (skipped assignment)")
    else:
        report.append("Verified role: not configured (report only)")
    report += [
        "",
        "== In NO other bot-installed server ==",
        *(none_lines or ["(none)"]),
        "",
        "== In at least one bot-installed server ==",
        *(some_lines or ["(none)"]),
    ]
    text = "\n".join(report)

    import io  # noqa: PLC0415

    verify_summary = ""
    if role is not None:
        verify_summary = f" · Verified: +{assigned} new, {already} existing" + (
            f", {failed} failed" if failed else ""
        )
    buf = io.BytesIO(text.encode("utf-8"))
    await interaction.followup.send(
        f"🔎 Scanned **{len(members)}** members of **{guild.name}** — "
        f"**{len(none_lines)}** in no other bot server, "
        f"**{len(some_lines)}** with overlap{verify_summary}. Full breakdown attached.",
        file=discord.File(buf, filename=f"member_scan_{guild.id}.txt"),
        ephemeral=True,
    )


@admin_group.command(
    name="changelog",
    description="(Bot owner only) Check changelog posting, preview a release's post, or re-post it.",
)
@app_commands.describe(
    version="Preview the post for this version instead of showing status.",
    repost="Post the current version's entry again, even if it already went out.",
)
async def admin_changelog_slash(
    interaction: discord.Interaction,
    version: str | None = None,
    repost: bool = False,
):
    """Inspect the support-server changelog posts (#92).

    The destination is `CHANGELOG_CHANNEL_ID` in Railway, and the post
    text lives in `docs/DISCORD_CHANGELOG.md` and ships with the bot, so
    there's nothing to configure here. This is for seeing where things
    stand and previewing a post before a release goes out.
    """
    if not await _require_bot_owner(interaction):
        return

    import changelog_post
    from bot import __version__

    if version:
        block = changelog_post.load_block(version)
        if block is None:
            await interaction.response.send_message(
                f"⚠️ No usable block for **{version}** in `docs/DISCORD_CHANGELOG.md`.",
                ephemeral=True,
            )
        elif changelog_post.is_no_post(block):
            await interaction.response.send_message(
                f"🔕 **{version}** is marked `{changelog_post.NO_POST_MARKER}` — it won't post.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"**{version}** would post:\n\n{block}", ephemeral=True
            )
        return

    if repost:
        await interaction.response.defer(ephemeral=True)
        result = await changelog_post.maybe_post_changelog(bot, __version__, force=True)
        await interaction.followup.send(f"📜 {result}", ephemeral=True)
        return

    # Permissions are checked live rather than trusted from when the
    # channel was configured — the same reasoning as config_health, since
    # a role edit months later is exactly how this goes quiet.
    channel_id = changelog_post.configured_channel_id()
    lines = [
        "📜 **Release changelog**",
        f"Running: **{__version__}**",
        f"Last posted: **{get_app_setting(changelog_post.LAST_VERSION_SETTING) or 'nothing yet'}**",
    ]

    if not channel_id:
        lines.append(f"Channel: *not set* — set `{changelog_post.CHANNEL_ENV_VAR}` in Railway")
    else:
        ch = bot.get_channel(channel_id)
        if ch is None:
            lines.append(f"Channel: `{channel_id}` ⚠️ not found, or I can't see it")
        else:
            perms = ch.permissions_for(ch.guild.me)
            # Editing its own earlier message is how a burst of releases
            # stays one entry, so Read Message History matters as much as
            # posting does.
            missing = [
                name
                for name, ok in (
                    ("View Channel", perms.view_channel),
                    ("Send Messages", perms.send_messages),
                    ("Read Message History", perms.read_message_history),
                )
                if not ok
            ]
            lines.append(
                f"Channel: {ch.mention}"
                + (f" ⚠️ missing {', '.join(missing)}" if missing else " ✅")
            )

    block = changelog_post.load_block(__version__)
    if block is None:
        lines.append(f"This version: ⚠️ no usable block in `{changelog_post.CHANGELOG_PATH.name}`")
    elif changelog_post.is_no_post(block):
        lines.append(f"This version: marked `{changelog_post.NO_POST_MARKER}`")

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@admin_group.command(
    name="champion_duel_import",
    description="(Bot owner only) Load the Champion Duel roster and scouting from an attached JSON.",
)
@app_commands.describe(
    file="payload.json from the simulator's `push_to_bot.py --out payload.json`.",
    round="Which round this draw is for. Defaults to whatever the payload says.",
)
# Spelled out rather than built from `cd_db.STAGE_LABELS`: this decorator runs
# at import time and `champion_duel_db` is imported inside the callback. A test
# asserts the two stay identical, so the duplication cannot drift silently --
# which is what caught these when the labels moved to the game's spelling.
@app_commands.choices(
    round=[
        app_commands.Choice(name="Qualifiers", value="qualifiers"),
        app_commands.Choice(name="Semi-finals", value="semifinals"),
        app_commands.Choice(name="Knockout Stage", value="knockouts"),
    ]
)
async def admin_champion_duel_import_slash(
    interaction: discord.Interaction,
    file: discord.Attachment,
    round: app_commands.Choice[str] | None = None,
):
    """Import the roster from an attachment rather than over HTTP.

    The same payload `POST /admin/import` takes, applied by the same data-layer
    functions, arriving a different way — and the difference is the whole point.
    The HTTP path needs a public host, a service key, and a shell with the
    simulator checked out. This needs a file and the surface the operator is
    already in, and `_require_bot_owner` is a stronger gate than the service key
    it replaces.

    The route stays: Map Manager will want it later, and it is what the web app
    would use. This is a second door to one room, not a second room.

    Nothing about the data is parsed here. Reading the workbooks, fitting the
    THP ratios and resolving renames all stay in the simulator, where the corpus
    that validates them lives — the bot receives a finished payload and applies
    it. That is the same reason the engine is a pinned package rather than a
    port.
    """
    if not await _require_bot_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)

    import champion_duel_db as cd_db  # noqa: PLC0415

    try:
        raw = await file.read()
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError:
        await interaction.followup.send(
            f"⚠️ `{file.filename}` isn't UTF-8 text — attach the JSON from `push_to_bot.py --out`.",
            ephemeral=True,
        )
        return
    except ValueError as e:
        await interaction.followup.send(
            f"⚠️ Couldn't parse `{file.filename}` as JSON: {e}", ephemeral=True
        )
        return

    if not isinstance(payload, dict) or not any(
        isinstance(payload.get(k), list) for k in ("registrants", "squads", "orders", "profiles")
    ):
        await interaction.followup.send(
            "⚠️ That JSON has none of `registrants`, `squads`, `orders` or `profiles`. "
            "Generate it with `push_to_bot.py --out payload.json`.",
            ephemeral=True,
        )
        return

    actor = {
        "discord_user_id": str(interaction.user.id),
        "discord_name": interaction.user.display_name,
        "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
    }

    # Which round this draw is for: the picker on the command, then the
    # payload's own declaration, then none. Deliberately no fallback to
    # qualifiers -- guessing qualifiers on a semifinal draw overwrites the
    # qualifier groups, which is the failure rounds exist to prevent. Importing
    # without a round just adds the players, which is always recoverable.
    stage = (round.value if round else None) or payload.get("stage") or None
    if stage:
        try:
            stage = cd_db._stage(stage)
        except ValueError:
            await interaction.followup.send(
                f"⚠️ `{stage}` isn't a round I know. Pick one on the command, or fix "
                f"the payload's `stage`.",
                ephemeral=True,
            )
            return

    # Which grouping this draw belongs to. The payload's Participating Warzone
    # line settles it outright; without one the importer falls back to matching
    # a registrant's warzone against the groupings we hold, which is what every
    # payload written before groupings existed relies on.
    #
    # A grouping block also *completes* a row: it fills in a start date nobody
    # had read yet and any of the sixteen that fielded no players, neither of
    # which the registrant list can supply.
    grouping = payload.get("grouping") if isinstance(payload.get("grouping"), dict) else None
    grouping_id = None
    if grouping:
        try:
            resolved = await asyncio.to_thread(
                cd_db.ensure_grouping,
                grouping.get("warzones") or [],
                grouping.get("started_on"),
            )
        except ValueError as e:
            await interaction.followup.send(
                f"⚠️ The payload's `grouping` block has no usable warzones: {e}", ephemeral=True
            )
            return
        grouping_id = resolved["id"]
        lines_prefix = f"Grouping `#{grouping_id}`, **{len(resolved['warzones'])}** warzones" + (
            f", started {resolved['started_on']}" if resolved.get("started_on") else ""
        )
    else:
        lines_prefix = None

    # Dependency order: squads and orders both resolve against registrant rows.
    lines, problems = [], []
    results = {}
    if lines_prefix:
        lines.append(lines_prefix)
    if isinstance(payload.get("registrants"), list):
        result = results["registrants"] = await asyncio.to_thread(
            cd_db.import_registrants,
            payload["registrants"],
            stage=stage,
            grouping_id=grouping_id,
            started_on=(grouping or {}).get("started_on"),
        )
        lines.append(f"**{result['total']}** registrants ({result['inserted']} new)")
    if isinstance(payload.get("squads"), list):
        result = results["squads"] = await asyncio.to_thread(
            cd_db.import_squads, payload["squads"], actor=actor
        )
        lines.append(
            f"**{result['applied']}** squad rows"
            + (
                f", {result['kept_observed']} existing values kept"
                if result["kept_observed"]
                else ""
            )
            + (f", {result['skipped']} skipped" if result["skipped"] else "")
        )
        problems += result["problems"]
    if isinstance(payload.get("orders"), list):
        result = results["orders"] = await asyncio.to_thread(
            cd_db.import_orders, payload["orders"], actor=actor
        )
        lines.append(
            f"**{result['applied']}** deployment orders across {result['players']} players"
            + (f", {result['skipped']} skipped" if result["skipped"] else "")
        )
        problems += result["problems"]
    if isinstance(payload.get("profiles"), list):
        result = results["profiles"] = await asyncio.to_thread(
            cd_db.import_profiles, payload["profiles"], actor=actor
        )
        # Coverage, not a total. The model runs without these and falls back to
        # the population, so what this number says is how much of the field is
        # measured rather than assumed.
        lines.append(
            f"**{result['applied']}** player profiles"
            + (f", {result['cleared']} cleared" if result["cleared"] else "")
            + (f", {result['skipped']} skipped" if result["skipped"] else "")
        )
        problems += result["problems"]

    # Logged after the sections rather than before, so the row records what
    # actually landed. An import that skipped everything is exactly the run
    # somebody comes asking about.
    await asyncio.to_thread(
        cd_db.record_import,
        door="discord",
        results=results,
        grouping_id=grouping_id,
        stage=stage,
        actor=actor,
    )

    groups = await asyncio.to_thread(cd_db.get_groups)
    # Names the round it used, so a wrong pick is visible now rather than after
    # the next import compounds it. "no round" is stated rather than left
    # blank: it is a real outcome, not a missing word.
    where = f"**{cd_db.STAGE_LABELS[stage]}** " if stage else ""
    tail = (
        ""
        if stage
        else "\n\nNo round recorded, so this only added players. "
        "Re-run with a round to place them in one."
    )
    summary = "✅ Imported {}from `{}`:\n{}\n\n{} group(s), {} registrants now loaded.{}".format(
        where,
        file.filename,
        "\n".join(f"• {line}" for line in lines),
        len(groups),
        sum(g["registrants"] for g in groups),
        tail,
    )
    if problems:
        # Attached rather than inlined: a roster refresh can produce hundreds,
        # and truncating them into an embed hides the ones nobody has seen yet.
        fp = io.BytesIO("\n".join(problems).encode("utf-8"))
        await interaction.followup.send(
            f"{summary}\n\n⚠️ **{len(problems)}** row(s) didn't land — see attached.",
            file=discord.File(fp, filename="champion_duel_import_problems.txt"),
            ephemeral=True,
        )
        return
    await interaction.followup.send(summary, ephemeral=True)


# ── Champion Duel: resolving grouping conflicts ───────────────────────────────
#
# Two groupings inside one event window claiming the same warzone is a
# contradiction: a warzone is drawn into exactly one set of Participating
# Warzones per Champion Duel. The member-facing half of this shipped first --
# `champion_duel_hub._report_conflict` shows both lists, offers to fix the
# caller's own, and for the other one tells them to reach us on the Community
# Server. That exit had nothing behind it until this.
#
# The bot does not merge on its own, and that is deliberate rather than
# unfinished. A conflict only arises from a wrong claim, an alliance cannot
# undo one, and deciding which community's entry was the mistake is the kind of
# opinion `UX.md` principle 6 says the bot does not have. Detect it, keep it
# off member surfaces, give the operator a merge.


def _conflict_summary(cd_db, pair: dict) -> str:
    """One conflict as a block an operator can decide from.

    Both warzone lists in full, because naming the shared number says one of
    them is wrong without showing which. Counts alongside, because the whole
    decision is which to keep and that turns on which holds real data.
    """
    lines = []
    for side, grouping in (("A", pair["a"]), ("B", pair["b"])):
        counts = pair[f"{side.lower()}_counts"]
        started = grouping.get("started_on") or "no date recorded"
        lines.append(
            f"**{side} · #{grouping['id']}** started {started}, origin `{grouping['origin']}`\n"
            f"  {counts['players']} players · {counts['groups']} groups · "
            f"{counts['results']} results · {counts['guilds']} guilds pinned\n"
            f"  {', '.join(grouping['warzones'])}"
        )
    return f"shared: **{', '.join(pair['shared'])}**\n" + "\n".join(lines)


class _MergeGroupingsView(discord.ui.View):
    """Pick a conflict, then pick which side survives.

    Two steps because the second is irreversible. The direction buttons only
    appear once a pair is selected and are labelled with the id that survives,
    so nobody can press one before seeing what it would fold away -- the same
    reason `champion_duel_hub._RevertAnyway` is a button on the conflict rather
    than a `force` parameter set in advance.
    """

    def __init__(self, *, user_id: int, pairs: list[dict], cd_db):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.pairs = pairs
        self.cd_db = cd_db
        self.chosen: dict | None = None
        self.message: discord.Message | None = None
        self._build()

    def _build(self):
        self.clear_items()
        select = discord.ui.Select(
            placeholder="Which conflict?",
            options=[
                discord.SelectOption(
                    label=f"#{p['a']['id']} vs #{p['b']['id']}",
                    value=str(i),
                    description=f"shares {', '.join(p['shared'])[:80]}",
                    default=self.chosen is p,
                )
                for i, p in enumerate(self.pairs[:25])
            ],
            row=0,
        )
        select.callback = self._on_pick
        self.add_item(select)

        if self.chosen:
            for side, other in (("a", "b"), ("b", "a")):
                keep = self.chosen[side]
                drop = self.chosen[other]
                button = discord.ui.Button(
                    label=f"Keep #{keep['id']}, fold in #{drop['id']}",
                    style=discord.ButtonStyle.danger,
                    row=1,
                )
                button.callback = self._merge_into(keep["id"], drop["id"])
                self.add_item(button)

    def _merge_into(self, keep_id: int, drop_id: int):
        async def callback(inter: discord.Interaction):
            await inter.response.defer(ephemeral=True, thinking=True)
            try:
                moved = await asyncio.to_thread(
                    self.cd_db.merge_groupings,
                    drop_id,
                    keep_id,
                    actor=str(inter.user.id),
                )
            except self.cd_db.MergeRefused as exc:
                await self._retire(inter, f"Not merged: {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                # Two operators on one conflict list is what produces this: the
                # existence check runs before the write, so a merge that landed
                # in between surfaces as an integrity error rather than a
                # refusal. Either way the operator needs a reply, because a
                # deferred interaction with no follow-up is a spinner forever.
                print(f"[CHAMPION_DUEL] merge {drop_id} into {keep_id} failed: {exc!r}")
                await self._retire(
                    inter, f"Merge failed and nothing was changed: `{type(exc).__name__}: {exc}`"
                )
                return
            await self._retire(
                inter,
                f"Merged #{drop_id} into #{keep_id}. "
                f"{moved['players']} players moved, {moved['filled']} filled in gaps, "
                f"{moved['unchanged']} already complete in #{keep_id}, "
                f"{moved['groups']} groups touched, {moved['guilds']} guilds repointed"
                + (f", {moved['unpinned']} unpinned" if moved["unpinned"] else "")
                + f". #{drop_id} is gone, and its warzones went with it"
                + (
                    f" ({', '.join(moved['dropped_warzones'])})"
                    if moved["dropped_warzones"]
                    else ""
                )
                + ". Run the command again for what is left.",
            )

        return callback

    async def _retire(self, inter: discord.Interaction, text: str):
        """Report, and stop the buttons looking live.

        The pairs this view holds are stale the moment a merge lands, and a
        danger button that still renders is one that fails with Discord's bare
        "This interaction failed" when pressed.
        """
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        await inter.followup.send(text[:2000], ephemeral=True)
        self.stop()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message("That isn't yours to press.", ephemeral=True)
            return False
        return True

    async def _on_pick(self, inter: discord.Interaction):
        self.chosen = self.pairs[int(inter.data["values"][0])]
        self._build()
        await inter.response.edit_message(
            content=(
                "**Champion Duel grouping conflicts**\n\n"
                + _conflict_summary(self.cd_db, self.chosen)
                + "\n\nMerging is not revertable. The one you keep wins every "
                "placement they both hold; the other only fills gaps."
            )[:2000],
            view=self,
        )


@admin_group.command(
    name="champion_duel_conflicts",
    description="(Bot owner only) Groupings claiming the same warzone, and a merge.",
)
async def admin_champion_duel_conflicts_slash(interaction: discord.Interaction):
    """What is broken right now, and the only surface that can fix it.

    `overlapping_groupings` answers the question one member's entry asks, at
    the moment they ask it. This sweeps what is already stored, which is the
    operator's question and nobody else's: a conflict reported last Tuesday is
    still there and nothing else lists it.
    """
    if not await _require_bot_owner(interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)

    import champion_duel_db as cd_db  # noqa: PLC0415

    pairs = await asyncio.to_thread(cd_db.find_grouping_conflicts)
    if not pairs:
        await interaction.followup.send(
            "No grouping conflicts. Every warzone is in one set of "
            "Participating Warzones per Champion Duel.",
            ephemeral=True,
        )
        return

    view = _MergeGroupingsView(user_id=interaction.user.id, pairs=pairs, cd_db=cd_db)
    body = "\n\n".join(_conflict_summary(cd_db, p) for p in pairs[:5])
    more = f"\n\n{len(pairs) - 5} more not shown." if len(pairs) > 5 else ""
    await interaction.followup.send(
        f"**{len(pairs)} grouping conflict(s)**\n\n{body}{more}"[:2000],
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


# Register the /admin Group on the tree once every subcommand has been
# attached above. The Group-level guilds= kwarg propagates to all its
# subcommands, so `BOT_ADMIN_GUILD_IDS` scoping still hides the
# entire group from every non-admin guild's slash picker.
bot.tree.add_command(admin_group, **_admin_command_kwargs)

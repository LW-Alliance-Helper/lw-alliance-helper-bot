"""
member_roster.py — Member Roster Sync (Premium-only feature).

Iterates a guild's members (filtered by the configured member role if set)
and writes them to a designated sheet tab. Other premium features
(birthday DMs, train DMs, survey-reminder DMs, auto-mention) read this
sheet to look up Discord IDs by display name.

Sheet structure (column indices configurable per guild):
  Discord ID | Name | Display Name | Joined | Roles

Plus an auto-created `Is this user in Discord?` column the bot maintains
with Yes/No values per row + a Google Sheets data validation dropdown.
The storm readers prefer this column over the legacy `not_on_discord`
column when present — the alliance no longer has to flag non-Discord
members manually for the storm flow's officer-view bucketing to be
accurate.

Commands:
  /setup_members  — admin wizard to configure tab/columns/role filter
  /sync_members   — manually run a full sync now
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import premium
from config import (
    get_config, get_member_roster_config, save_member_roster_config,
    update_roster_last_synced, get_member_roster_sheet, get_spreadsheet,
)


logger = logging.getLogger(__name__)


# Header for the bot-maintained presence column. Storm readers also
# search this exact string — keep them in sync.
DISCORD_FLAG_COLUMN_HEADER = "Is this user in Discord?"


# ── Sync logic (pure-ish; takes a guild and config) ───────────────────────────

def _format_joined(member: discord.Member) -> str:
    if member.joined_at is None:
        return ""
    return member.joined_at.strftime("%Y-%m-%d")


def _format_roles(member: discord.Member) -> str:
    """Comma-separated list of role names, excluding @everyone."""
    names = [r.name for r in member.roles if r.name != "@everyone"]
    return ", ".join(sorted(names))


def _eligible(member: discord.Member, role_filter_id: int) -> bool:
    """True if the member should appear on the roster."""
    if member.bot:
        return False
    if role_filter_id == 0:
        return True
    return any(r.id == role_filter_id for r in member.roles)


def _bot_managed_cols(cfg: dict) -> dict[int, tuple[str, callable]]:
    """The column indices the bot owns and overwrites on every sync,
    keyed by index. Anything outside this set on the Sheet is alliance-
    owned data (custom power columns, the `not_on_discord` flag, etc.)
    and must be preserved across sync calls."""
    return {
        cfg["discord_id_col"]: ("Discord ID",   lambda m: str(m.id)),
        cfg["name_col"]:       ("Name",         lambda m: m.name),
        cfg["display_col"]:    ("Display Name", lambda m: m.display_name),
        cfg["joined_col"]:     ("Joined",       _format_joined),
        cfg["roles_col"]:      ("Roles",        lambda m: _format_roles(m)),
    }


def _build_roster_rows(guild: discord.Guild, cfg: dict) -> list[list[str]]:
    """
    Build the rows that will be written to the sheet, including a header row.
    Column ordering matches cfg's *_col indices; gaps are filled with empty
    strings so each row is at least max(col_index)+1 cells wide.
    """
    role_filter = cfg.get("role_filter_id", 0)
    cols = _bot_managed_cols(cfg)
    width = max(cols.keys()) + 1

    header_row = [""] * width
    for idx, (label, _) in cols.items():
        header_row[idx] = label

    member_rows: list[list[str]] = []
    members = sorted(
        (m for m in guild.members if _eligible(m, role_filter)),
        key=lambda m: m.display_name.lower(),
    )
    for m in members:
        row = [""] * width
        for idx, (_, value_fn) in cols.items():
            row[idx] = value_fn(m)
        member_rows.append(row)

    return [header_row, *member_rows]


def _merge_with_existing(
    new_rows: list[list[str]], existing: list[list[str]], cfg: dict,
) -> list[list[str]]:
    """Merge bot-managed columns from `new_rows` with alliance-owned
    columns preserved from `existing`.

    Strategy:
      * The bot writes the columns named in `_bot_managed_cols`. Every
        OTHER column is alliance data (custom Power column, the
        `not_on_discord` flag, an annotation column, etc.) and is
        preserved as-is per Discord-ID match.
      * The header row keeps the bot's labels for managed columns and
        the alliance's labels for everything else.
      * Members who left the alliance — and whose data therefore drops
        out of `new_rows` — also lose their custom-column data. That's
        the correct behaviour: leaving the alliance means their row
        is gone.
      * New members joining get blank cells in custom columns; the
        alliance can fill them in.
    """
    if not new_rows:
        return new_rows

    bot_cols = set(_bot_managed_cols(cfg).keys())
    id_col   = cfg["discord_id_col"]

    new_header = new_rows[0]
    existing_header = existing[0] if existing else []
    width = max(len(new_header), len(existing_header))

    # Header — bot owns its columns, alliance keeps theirs.
    header = list(new_header) + [""] * (width - len(new_header))
    for i in range(width):
        if i in bot_cols:
            continue
        if i < len(existing_header) and existing_header[i]:
            header[i] = existing_header[i]

    # Index existing rows by Discord ID so we can copy per-member custom
    # columns over to the new data.
    existing_by_id: dict[str, list[str]] = {}
    for row in existing[1:] if existing else []:
        did = row[id_col] if id_col < len(row) else ""
        did = did.strip()
        if did:
            existing_by_id[did] = row

    merged_rows = [header]
    for new_row in new_rows[1:]:
        merged = list(new_row)
        if len(merged) < width:
            merged.extend([""] * (width - len(merged)))
        did = merged[id_col] if id_col < len(merged) else ""
        old = existing_by_id.get(did.strip())
        if old:
            for i in range(width):
                if i in bot_cols:
                    continue
                if i < len(old):
                    merged[i] = old[i]
        merged_rows.append(merged)

    return merged_rows


def write_roster(guild: discord.Guild, cfg: dict) -> int:
    """
    Rebuild the configured tab with a fresh roster while preserving
    alliance-owned columns (custom Power column, `not_on_discord`,
    etc.) per Discord-ID match.

    Returns the number of member rows written (excluding header).

    The caller is responsible for ensuring the guild's member cache is
    populated (via `await guild.chunk()`) before invoking this function;
    otherwise `guild.members` may only contain a handful of members that
    Discord has surfaced via interactions, and the resulting roster will
    be incomplete. `_warn_if_cache_looks_thin` logs a warning when the
    Discord-side member_count is much larger than the cached size, which
    catches missing-intent and missing-chunk cases at runtime.
    """
    _warn_if_cache_looks_thin(guild)
    new_rows = _build_roster_rows(guild, cfg)
    ws = get_member_roster_sheet(guild.id, cfg["tab_name"])
    try:
        existing = ws.get_all_values()
    except Exception:
        existing = []
    merged = _merge_with_existing(new_rows, existing, cfg)
    # Ensure the "Is this user in Discord?" column exists on the
    # sheet and is filled with bot-derived Yes/No values. Returns the
    # final column index so the data-validation request can target it.
    flag_col_idx = _ensure_discord_flag_column(merged, guild, cfg)
    ws.clear()
    if merged:
        ws.update("A1", merged, value_input_option="USER_ENTERED")
    # Apply the Yes/No dropdown data-validation rule on the new column.
    # Best-effort — a Sheets API failure here doesn't roll back the row
    # write (the column's values are correct either way).
    if flag_col_idx is not None and len(merged) > 1:
        try:
            _apply_discord_flag_validation(
                guild.id, ws, flag_col_idx, row_count=len(merged),
            )
        except Exception as e:
            logger.warning(
                "[ROSTER] data-validation rule write failed for guild=%s: %s",
                guild.id, e,
            )
    update_roster_last_synced(guild.id, datetime.now(timezone.utc).isoformat())
    return max(0, len(merged) - 1)


def _ensure_discord_flag_column(
    merged: list[list[str]], guild: discord.Guild, cfg: dict,
) -> int | None:
    """Ensure the bot-maintained presence column exists in `merged` and
    fill every member row with "Yes" or "No" based on live guild
    membership.

    Mutates `merged` in place. Returns the column index (0-based) so
    the caller can target the data-validation request, or None when
    `merged` is empty.

    Header lookup is case-insensitive against
    `DISCORD_FLAG_COLUMN_HEADER`. If the column doesn't exist yet, a
    new column is appended at the right edge — every existing row is
    extended with a blank cell that the value fill then overwrites.
    """
    if not merged:
        return None

    header = merged[0]
    target = DISCORD_FLAG_COLUMN_HEADER.strip().lower()
    flag_idx = -1
    for i, cell in enumerate(header):
        if str(cell or "").strip().lower() == target:
            flag_idx = i
            break

    if flag_idx < 0:
        # Append a new column at the right edge for every row.
        flag_idx = len(header)
        header.append(DISCORD_FLAG_COLUMN_HEADER)
        for row in merged[1:]:
            row.append("")
    else:
        header[flag_idx] = DISCORD_FLAG_COLUMN_HEADER  # canonical casing
        for row in merged[1:]:
            while len(row) <= flag_idx:
                row.append("")

    # Build a (non-bot) member ID lookup once. `int()` conversion is
    # done in the same pass so the per-row check is a set membership.
    live_ids: set[int] = set()
    for m in getattr(guild, "members", []) or []:
        if getattr(m, "bot", False):
            continue
        try:
            live_ids.add(int(m.id))
        except (TypeError, ValueError):
            continue

    id_col = cfg["discord_id_col"]
    for row in merged[1:]:
        discord_id = (row[id_col] if id_col < len(row) else "").strip()
        is_on_discord = False
        if discord_id.isdigit():
            try:
                is_on_discord = int(discord_id) in live_ids
            except (TypeError, ValueError):
                is_on_discord = False
        row[flag_idx] = "Yes" if is_on_discord else "No"

    return flag_idx


def _apply_discord_flag_validation(
    guild_id: int, ws, flag_col_idx: int, *, row_count: int,
) -> None:
    """Write a Yes/No-dropdown data validation rule on the presence
    column for every member row. Spans rows 2..row_count (skipping
    header). gspread surfaces this via the spreadsheet-level
    `batch_update`."""
    spreadsheet = get_spreadsheet(guild_id)
    if spreadsheet is None:
        return  # no Sheet configured at all — nothing to validate
    sheet_id_raw = getattr(ws, "id", None)
    if sheet_id_raw is None:
        return
    try:
        sheet_id_int = int(sheet_id_raw)
    except (TypeError, ValueError):
        # Worksheet's `.id` isn't numeric (test fake, malformed mock).
        # Silent skip — the row values are still correct on the Sheet.
        return
    request = {
        "requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId":          sheet_id_int,
                        "startRowIndex":    1,            # skip header
                        "endRowIndex":      row_count,    # exclusive
                        "startColumnIndex": flag_col_idx,
                        "endColumnIndex":   flag_col_idx + 1,
                    },
                    "rule": {
                        "condition": {
                            "type":   "ONE_OF_LIST",
                            "values": [
                                {"userEnteredValue": "Yes"},
                                {"userEnteredValue": "No"},
                            ],
                        },
                        "showCustomUi": True,
                        "strict":       True,
                        "inputMessage": (
                            "Auto-filled by the LW Alliance Helper bot. "
                            "Override to Yes/No if needed."
                        ),
                    },
                },
            },
        ],
    }
    spreadsheet.batch_update(request)


def _warn_if_cache_looks_thin(guild: discord.Guild) -> None:
    """If the cached member list is wildly smaller than Discord's reported
    guild size, the Server Members Intent probably isn't enabled (or the
    caller forgot to chunk). Log loudly so the symptom — "/sync_members
    wrote 0 rows" — has a breadcrumb pointing at the cause."""
    try:
        cached_count = len(guild.members)
        raw_total    = getattr(guild, "member_count", None)
        total_count  = int(raw_total) if isinstance(raw_total, int) else 0
    except (AttributeError, TypeError) as e:
        # Defensive: shouldn't happen with a real `discord.Guild`, but tests
        # and edge cases (None members list) shouldn't suppress real errors.
        print(f"[ROSTER] _warn_if_cache_looks_thin diagnostic failed for "
              f"guild {getattr(guild, 'id', '?')}: {e}")
        return
    if total_count > 1 and cached_count < max(2, total_count // 2):
        print(
            f"[ROSTER] Guild {guild.id}: only {cached_count}/{total_count} members "
            f"in cache. Enable the SERVER MEMBERS INTENT in the Discord Developer "
            f"Portal (Bot → Privileged Gateway Intents) — without it `guild.members` "
            f"can't see the full roster."
        )


async def _ensure_member_cache(guild: discord.Guild) -> None:
    """Force-load `guild.members` if it isn't already chunked.

    Safe to call on every sync — it short-circuits when the cache is
    already complete, and swallows the `ClientException` that fires when
    the members intent isn't actually granted (so the sync still attempts
    to run with whatever members are available, with the warning above
    explaining the partial result).
    """
    try:
        if not getattr(guild, "chunked", True):
            await guild.chunk()
    except discord.ClientException as e:
        # Raised when intents.members is False — i.e. the privileged intent
        # isn't enabled. Don't crash; let _warn_if_cache_looks_thin surface it.
        print(f"[ROSTER] guild.chunk() rejected for guild {guild.id}: {e}")
    except Exception as e:
        print(f"[ROSTER] guild.chunk() failed for guild {guild.id}: {e}")


# ── Cog ──────────────────────────────────────────────────────────────────────

class MemberRosterCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._auto_sync_if_enabled(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._auto_sync_if_enabled(member.guild)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Only resync if role membership changed (covers the role-filter case).
        if {r.id for r in before.roles} != {r.id for r in after.roles}:
            await self._auto_sync_if_enabled(after.guild)

    async def _auto_sync_if_enabled(self, guild: discord.Guild):
        if not await premium.is_premium(guild.id, bot=self.bot):
            return
        cfg = get_member_roster_config(guild.id)
        if not cfg.get("enabled") or not cfg.get("auto_sync"):
            return
        await _ensure_member_cache(guild)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, write_roster, guild, cfg,
            )
        except Exception as e:
            print(f"[ROSTER] Auto-sync failed for guild {guild.id}: {e}")
            # Auto-sync runs on every member-join/leave/role-change, so a
            # transient error gets re-tried naturally. But unexpected bugs
            # (template typos, schema drift) should land in Sentry instead
            # of only stdout — the channel post path already logs, this
            # surfaces non-Discord-API failures for triage.
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(e)
            except Exception:
                pass

    @app_commands.command(
        name="sync_members",
        description="💎 Manually rebuild the member roster sheet now",
    )
    async def sync_members(self, interaction: discord.Interaction):
        from setup_cog import _has_leadership_or_admin
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to sync the member roster.",
                ephemeral=True,
            )
            return

        if not await premium.is_premium(
            interaction.guild_id, interaction=interaction, bot=self.bot,
        ):
            await interaction.response.send_message(
                embed=premium.premium_locked_embed(
                    feature_label="Member Roster Sync",
                    description=(
                        "Member Roster Sync writes every member's Discord ID to "
                        "your sheet so other Premium features (birthday DMs, "
                        "train DMs, auto-mention, etc.) can find them. "
                        "Run `/upgrade` to unlock it."
                    ),
                ),
                view=premium.upgrade_view(),
                ephemeral=True,
            )
            return

        cfg = get_member_roster_config(interaction.guild_id)
        if not cfg.get("enabled"):
            await interaction.response.send_message(
                "⚙️ Member Roster Sync isn't configured yet. Run `/setup_members` first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        await _ensure_member_cache(guild)
        try:
            count = await asyncio.get_event_loop().run_in_executor(
                None, write_roster, guild, cfg,
            )
        except Exception as e:
            await interaction.followup.send(
                f"⚠️ Sync failed: {e}\nMake sure the bot has access to your sheet "
                f"and that the **{cfg['tab_name']}** tab can be written to.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Synced **{count}** members to the **{cfg['tab_name']}** tab.",
            ephemeral=True,
        )

    @app_commands.command(
        name="setup_members",
        description="💎 Configure Member Roster Sync (Premium)",
    )
    async def setup_members(self, interaction: discord.Interaction):
        from setup_cog import _has_leadership_or_admin, _check_wizard_can_run
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to configure the member roster.",
                ephemeral=True,
            )
            return

        # Premium gate before the channel-perms pre-check: a free user
        # trying this command should see the upsell, not a perms error.
        if not await premium.is_premium(
            interaction.guild_id, interaction=interaction, bot=self.bot,
        ):
            await interaction.response.send_message(
                embed=premium.premium_locked_embed(
                    feature_label="Member Roster Sync",
                    description=(
                        "Member Roster Sync is part of LW Alliance Helper Premium. "
                        "Run `/upgrade` to unlock it."
                    ),
                ),
                view=premium.upgrade_view(),
                ephemeral=True,
            )
            return

        if not await _check_wizard_can_run(interaction, "setup_members"):
            return

        await interaction.response.send_message(
            "⚙️ Starting Member Roster Sync setup — check the channel for prompts.",
            ephemeral=True,
        )
        await run_member_roster_setup(interaction, self.bot)


# ── Wizard ───────────────────────────────────────────────────────────────────

async def run_member_roster_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the member-roster sync tab."""
    import wizard_registry
    from wizard_registry import wait_view_or_cancel
    from setup_cog import (
        ask_keep_or_change, YesNoView, ask_proceed_with_existing_config,
    )
    from config import has_member_roster_config

    guild_id     = interaction.guild_id
    channel      = interaction.channel
    user         = interaction.user
    cancel_event = wizard_registry.register(user.id)

    current   = get_member_roster_config(guild_id)
    guild_cfg = get_config(guild_id)
    member_role_id = guild_cfg.member_role_id if guild_cfg else 0
    roster_already_configured = has_member_roster_config(guild_id)

    # ── If already configured, show summary and offer edit or cancel ─────────
    if roster_already_configured:
        rf_id = current.get("role_filter_id") or 0
        fields = [
            ("Roster Tab", current.get("tab_name") or "*not set*"),
            (
                "Role Filter",
                f"<@&{rf_id}>" if rf_id else "All non-bots",
            ),
            ("Auto-Sync", "✅ Enabled" if current.get("auto_sync") else "❌ Disabled"),
        ]
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="💎 Current Member Roster Sync Setup",
            description="Member Roster Sync is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Member Roster Sync is still active.",
        )
        if proceed is not True:
            wizard_registry.unregister(user.id, cancel_event)
            return

    await channel.send(
        "💎 **Member Roster Sync Setup**\n"
        "Configure how the bot writes your roster (Discord IDs + names) to a "
        "sheet tab. Other premium features look this up to send DMs and tag members."
    )

    # ── Step 1: Tab name ──────────────────────────────────────────────────────
    # Pass `current=` separately from `default=` so the helper renders
    # "Keep current: X" instead of "Use default: X" when a guild-saved
    # value is present.
    tab_name = await ask_keep_or_change(
        channel,
        "**Step 1 of 3 — Roster Tab**\n"
        "Which tab should the roster be written to?\n"
        "⚠️ *If the tab doesn't exist, the bot will create it automatically.*\n"
        "⚠️ *The tab will be **completely overwritten** on each sync.*",
        default="Member Roster",
        current=current.get("tab_name", ""),
        modal_title="Roster Tab Name",
        modal_label="Tab name",
        timeout_cmd="setup_members",
        cancel_event=cancel_event,
    )
    if tab_name is None:
        return

    # ── Step 2: Filter to member role only? ───────────────────────────────────
    filter_view = YesNoView()
    role_label  = f"<@&{member_role_id}>" if member_role_id else "the configured member role"
    await channel.send(
        f"**Step 2 of 3 — Filter by Member Role?**\n"
        f"Should the roster only include members who have {role_label}?\n"
        f"Pick **No** to include every (non-bot) member of the server.",
        view=filter_view,
    )
    await wait_view_or_cancel(filter_view, cancel_event)
    if filter_view.cancelled:
        return
    if filter_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_members` to start again.")
        return
    role_filter_id = member_role_id if filter_view.selected else 0

    # ── Step 3: Auto-sync on join/leave/role-change? ──────────────────────────
    auto_view = YesNoView()
    await channel.send(
        "**Step 3 of 3 — Auto-Sync?**\n"
        "Should the bot automatically re-sync when members join, leave, or "
        "change roles?\nPick **No** to only sync on `/sync_members`.",
        view=auto_view,
    )
    await wait_view_or_cancel(auto_view, cancel_event)
    if auto_view.cancelled:
        return
    if auto_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_members` to start again.")
        return
    auto_sync = 1 if auto_view.selected else 0

    save_member_roster_config(
        guild_id,
        enabled=1, tab_name=tab_name,
        role_filter_id=role_filter_id, auto_sync=auto_sync,
    )

    # ── Initial sync ──────────────────────────────────────────────────────────
    cfg   = get_member_roster_config(guild_id)
    guild = interaction.guild
    await _ensure_member_cache(guild)
    try:
        count = await asyncio.get_event_loop().run_in_executor(
            None, write_roster, guild, cfg,
        )
    except Exception as e:
        await channel.send(
            f"✅ Saved configuration but the initial sync failed: {e}\n"
            f"Try running `/sync_members` once you've fixed the issue."
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    embed = discord.Embed(
        title="✅ Member Roster Sync Configured",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Tab",          value=tab_name, inline=True)
    embed.add_field(
        name="Role Filter",
        value=f"<@&{role_filter_id}>" if role_filter_id else "All non-bots",
        inline=True,
    )
    embed.add_field(name="Auto-Sync",    value="Enabled" if auto_sync else "Disabled", inline=True)
    embed.add_field(name="Initial sync", value=f"**{count}** members written", inline=False)
    embed.set_footer(text="Run /sync_members to re-sync manually any time.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[ROSTER] Sync configured for guild {guild_id} ({count} members)")


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberRosterCog(bot))

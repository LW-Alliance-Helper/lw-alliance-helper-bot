"""
member_roster.py — Member Roster Sync (Premium-only feature).

Iterates a guild's members (filtered by the configured member role if set)
and writes them to a designated sheet tab. Other premium features
(birthday DMs, train DMs, survey-reminder DMs, auto-mention) read this
sheet to look up Discord IDs by display name.

Sheet structure (column indices configurable per guild):
  Discord ID | Name | Display Name | Joined | Roles

Commands:
  /setup_members  — admin wizard to configure tab/columns/role filter
  /sync_members   — manually run a full sync now
"""

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import premium
from config import (
    get_config, get_member_roster_config, save_member_roster_config,
    update_roster_last_synced, get_member_roster_sheet,
)


# ── Sync logic (pure-ish; takes a guild and config) ───────────────────────────

def _format_joined(member: discord.Member) -> str:
    if member.joined_at is None:
        return ""
    return member.joined_at.strftime("%Y-%m-%d")


def _format_roles(member: discord.Member, role_filter_id: int) -> str:
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


def _build_roster_rows(guild: discord.Guild, cfg: dict) -> list[list[str]]:
    """
    Build the rows that will be written to the sheet, including a header row.
    Column ordering matches cfg's *_col indices; gaps are filled with empty
    strings so each row is at least max(col_index)+1 cells wide.
    """
    role_filter = cfg.get("role_filter_id", 0)
    cols = {
        cfg["discord_id_col"]: ("Discord ID",   lambda m: str(m.id)),
        cfg["name_col"]:       ("Name",         lambda m: m.name),
        cfg["display_col"]:    ("Display Name", lambda m: m.display_name),
        cfg["joined_col"]:     ("Joined",       _format_joined),
        cfg["roles_col"]:      ("Roles",        lambda m: _format_roles(m, role_filter)),
    }
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


def write_roster(guild: discord.Guild, cfg: dict) -> int:
    """
    Replace the contents of the configured tab with a fresh roster.
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
    rows = _build_roster_rows(guild, cfg)
    ws = get_member_roster_sheet(guild.id, cfg["tab_name"])
    ws.clear()
    if rows:
        ws.update("A1", rows, value_input_option="USER_ENTERED")
    update_roster_last_synced(guild.id, datetime.now(timezone.utc).isoformat())
    return max(0, len(rows) - 1)


def _warn_if_cache_looks_thin(guild: discord.Guild) -> None:
    """If the cached member list is wildly smaller than Discord's reported
    guild size, the Server Members Intent probably isn't enabled (or the
    caller forgot to chunk). Log loudly so the symptom — "/sync_members
    wrote 0 rows" — has a breadcrumb pointing at the cause."""
    try:
        cached_count = len(guild.members)
        raw_total    = getattr(guild, "member_count", None)
        total_count  = int(raw_total) if isinstance(raw_total, int) else 0
    except Exception:
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
    from setup_cog import ask_keep_or_change, YesNoView

    guild_id     = interaction.guild_id
    channel      = interaction.channel
    user         = interaction.user
    cancel_event = wizard_registry.register(user.id)

    current   = get_member_roster_config(guild_id)
    guild_cfg = get_config(guild_id)
    member_role_id = guild_cfg.member_role_id if guild_cfg else 0

    await channel.send(
        "💎 **Member Roster Sync Setup**\n"
        "Configure how the bot writes your roster (Discord IDs + names) to a "
        "sheet tab. Other premium features look this up to send DMs and tag members."
    )

    # ── Step 1: Tab name ──────────────────────────────────────────────────────
    tab_name = await ask_keep_or_change(
        channel,
        "**Step 1 of 3 — Roster Tab**\n"
        "Which tab should the roster be written to?\n"
        "⚠️ *If the tab doesn't exist, the bot will create it automatically.*\n"
        "⚠️ *The tab will be **completely overwritten** on each sync.*",
        default=current.get("tab_name") or "Member Roster",
        modal_title="Roster Tab Name",
        modal_label="Tab name",
        timeout_cmd="setup_members",
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
    await filter_view.wait()
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
    await auto_view.wait()
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

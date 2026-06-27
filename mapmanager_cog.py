"""`/map_manager` slash commands — link a guild to the Map Manager web app (6C, #316).

Three admin-only subcommands under the ``/map_manager`` group:
  - ``setup``  — Premium-gated. Collects the server number + alliance tag via a
    modal, calls MM ``POST /api/internal/guild-links``, caches the resolved ids
    locally (``config.guild_alliance_mappings``), and replies with a sign-in
    link to the alliance's Map Manager page.
  - ``change`` — modify the server / alliance name on an existing link
    (``PATCH``). Not Premium-gated: fixing a link shouldn't require an active
    subscription.
  - ``unlink`` — disconnect this server (``DELETE``; MM keeps its row for
    audit, and so does the bot via a soft ``revoked_at``). Confirm-gated.

The bot never auto-creates a grouping: per the integration decision, a freshly
linked alliance finishes onboarding (picking its season grouping) inside Map
Manager. All MM I/O goes through ``mapmanager_client``; gating mirrors the rest
of the bot (``premium.feature_gate`` + a Discord-admin check). See
``docs/BOT_INTEGRATION_HANDOFF.md`` in the Map Manager repo for the contract.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import config
import mapmanager_client
import premium

try:
    import sentry_sdk
except Exception:  # pragma: no cover - sentry optional in some envs
    sentry_sdk = None


_NOT_CONFIGURED_MSG = (
    "⚠️ The Map Manager integration isn't switched on for this bot yet. "
    "If you're the bot operator, set `MAPMANAGER_API_URL` and `MAPMANAGER_API_KEY`, "
    "then try again."
)


def _capture(e: Exception) -> None:
    if sentry_sdk is not None:
        try:
            sentry_sdk.capture_exception(e)
        except Exception:
            pass


def _is_admin(interaction: discord.Interaction) -> bool:
    """True if the caller has Discord's administrator permission in this guild.

    Per the integration design, running ``/map_manager setup`` is the assertion
    that this guild represents the alliance — only a guild admin can make it, so
    a guild admin (not the bot's leadership role) is the gate here.
    """
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


async def _reject_non_admin(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "⛔ Only a Discord server admin can manage this server's Map Manager link.",
        ephemeral=True,
    )


def _setup_success_message(result: dict, alliance_name: str, server: int) -> str:
    name = result.get("alliance_name") or alliance_name
    srv = result.get("server") or server
    url = mapmanager_client.alliance_dashboard_url(result.get("alliance_id"))

    lines = [f"✅ Linked **{name}** (server {srv}) to Map Manager."]
    if result.get("alliance_created"):
        lines.append("A new alliance record was created in Map Manager for your guild.")
    if result.get("server_grouping_id") is None:
        lines.append(
            "One more step lives in Map Manager: the first time you open it, "
            "you'll be guided to pick your season grouping."
        )
    if url:
        lines.append(f"\nOpen it and sign in with Discord:\n{url}")
    else:
        lines.append("\nOpen Map Manager and sign in with Discord to see your alliance.")
    return "\n".join(lines)


def _persist_link(guild_id: int, result: dict, fallback_name: str, fallback_server: int) -> None:
    """Cache MM's resolved link locally. Prefers MM's authoritative values,
    falling back to what the user entered when a field is absent."""
    config.save_guild_alliance_mapping(
        guild_id=guild_id,
        alliance_name=result.get("alliance_name") or fallback_name,
        server=int(result.get("server") or fallback_server),
        mm_alliance_id=result.get("alliance_id") or "",
        mm_server_grouping_id=result.get("server_grouping_id"),
    )


async def _perform_link_call(
    interaction: discord.Interaction, server: int, alliance_name: str, *, change: bool
) -> None:
    """Shared modal-submit path: call MM (create or update), cache the result
    locally, and reply. Defers first (the round-trip can take a second or two)
    then follows up. A remote success followed by a local-save failure is
    surfaced as a "linked but couldn't save" retry hint, not a silent desync.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        if change:
            result = await mapmanager_client.update_guild_link(
                interaction.guild_id, server=server, alliance_name=alliance_name
            )
        else:
            result = await mapmanager_client.create_guild_link(
                guild_id=interaction.guild_id,
                server=server,
                alliance_name=alliance_name,
                requested_by_discord_id=interaction.user.id,
            )
    except mapmanager_client.MapManagerError as e:
        await interaction.followup.send(f"⚠️ {e.message}", ephemeral=True)
        return

    try:
        _persist_link(interaction.guild_id, result, alliance_name, server)
    except Exception as e:  # noqa: BLE001 — local save failed after a remote success
        _capture(e)
        verb = "change" if change else "setup"
        await interaction.followup.send(
            "⚠️ Map Manager accepted the link, but I couldn't save it on my side. "
            f"Run `/map_manager {verb}` again to retry.",
            ephemeral=True,
        )
        return

    if change:
        name = result.get("alliance_name") or alliance_name
        srv = result.get("server") or server
        await interaction.followup.send(
            f"✅ Updated your Map Manager link to **{name}** (server {srv}).", ephemeral=True
        )
    else:
        await interaction.followup.send(
            _setup_success_message(result, alliance_name, server), ephemeral=True
        )


# ── Modals ──────────────────────────────────────────────────────────────────────


def _validate_inputs(raw_server: str, alliance_name: str) -> tuple[int, str] | str:
    """Return ``(server_int, alliance_name)`` on success, or an error string."""
    raw_server = raw_server.strip()
    alliance_name = alliance_name.strip()
    if not raw_server.isdigit():
        return "⚠️ The server number must be digits only (for example, 738)."
    if not alliance_name:
        return "⚠️ The alliance tag / name can't be blank."
    return int(raw_server), alliance_name


class _SetupModal(discord.ui.Modal, title="Link Map Manager"):
    server = discord.ui.TextInput(
        label="Server number",
        placeholder="e.g. 738",
        required=True,
        max_length=10,
    )
    alliance = discord.ui.TextInput(
        label="Alliance tag or name",
        placeholder="e.g. Nox",
        required=True,
        max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        validated = _validate_inputs(self.server.value, self.alliance.value)
        if isinstance(validated, str):
            await interaction.response.send_message(validated, ephemeral=True)
            return
        server, alliance_name = validated
        await _perform_link_call(interaction, server, alliance_name, change=False)


class _ChangeModal(discord.ui.Modal, title="Update Map Manager link"):
    def __init__(self, current_server: int, current_alliance: str):
        super().__init__()
        self.server = discord.ui.TextInput(
            label="Server number",
            default=str(current_server),
            required=True,
            max_length=10,
        )
        self.alliance = discord.ui.TextInput(
            label="Alliance tag or name",
            default=current_alliance,
            required=True,
            max_length=64,
        )
        self.add_item(self.server)
        self.add_item(self.alliance)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        validated = _validate_inputs(self.server.value, self.alliance.value)
        if isinstance(validated, str):
            await interaction.response.send_message(validated, ephemeral=True)
            return
        server, alliance_name = validated
        await _perform_link_call(interaction, server, alliance_name, change=True)


# ── Unlink confirmation ─────────────────────────────────────────────────────────


class _UnlinkConfirm(discord.ui.View):
    """Two-button confirm for ``/map_manager unlink``. Ephemeral, so it follows
    the ``_ForgetGuildConfirm`` precedent (value-checked, short timeout, no
    channel-message cleanup)."""

    def __init__(self, *, guild_id: int, user_id: int, alliance_name: str):
        super().__init__(timeout=60)
        self._guild_id = guild_id
        self._user_id = user_id
        self._alliance_name = alliance_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._user_id:
            await interaction.response.send_message(
                "⛔ Only the admin who started this can confirm.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="🔌 Remove link", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Removing the link…", view=self)

        # Tell MM to revoke; tolerate MM being unreachable so the admin can still
        # disconnect locally (MM keeps its row regardless of our call).
        note = ""
        try:
            await mapmanager_client.delete_guild_link(self._guild_id)
        except mapmanager_client.MapManagerError as e:
            note = f"\n(Map Manager couldn't confirm the removal: {e.message})"

        config.revoke_guild_alliance_mapping(self._guild_id)
        await interaction.edit_original_response(
            content=f"✅ Removed the Map Manager link for **{self._alliance_name}**.{note}"
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="❌ Cancelled — the Map Manager link is unchanged.", view=self
        )
        self.stop()


# ── The cog ─────────────────────────────────────────────────────────────────────


class MapManagerCog(commands.Cog):
    map_manager = app_commands.Group(
        name="map_manager",
        description="Connect this server's alliance to the Map Manager web app",
        guild_only=True,
    )

    def __init__(self, bot):
        self.bot = bot

    @map_manager.command(
        name="setup",
        description="Link this server's alliance to the Map Manager web app (Premium)",
    )
    async def setup_cmd(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            await _reject_non_admin(interaction)
            return
        if not await premium.feature_gate(
            "map_manager", interaction.guild_id, interaction=interaction, bot=self.bot
        ):
            await interaction.response.send_message(
                embed=premium.premium_locked_embed(
                    feature_label="Map Manager integration",
                    description=(
                        "Linking your alliance to the Map Manager web app is part of "
                        f"{premium.PREMIUM_BRAND}. Run `/upgrade` to unlock it."
                    ),
                ),
                view=premium.upgrade_view(),
                ephemeral=True,
            )
            return
        if not mapmanager_client.is_configured():
            await interaction.response.send_message(_NOT_CONFIGURED_MSG, ephemeral=True)
            return
        await interaction.response.send_modal(_SetupModal())

    @map_manager.command(
        name="change",
        description="Change the server number or alliance name on your Map Manager link",
    )
    async def change_cmd(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            await _reject_non_admin(interaction)
            return
        if not mapmanager_client.is_configured():
            await interaction.response.send_message(_NOT_CONFIGURED_MSG, ephemeral=True)
            return
        mapping = config.get_guild_alliance_mapping(interaction.guild_id)
        if mapping is None:
            await interaction.response.send_message(
                "ℹ️ This server isn't linked to Map Manager yet. Run `/map_manager setup` first.",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            _ChangeModal(mapping["server"], mapping["alliance_name"])
        )

    @map_manager.command(
        name="unlink",
        description="Disconnect this server from Map Manager",
    )
    async def unlink_cmd(self, interaction: discord.Interaction):
        if not _is_admin(interaction):
            await _reject_non_admin(interaction)
            return
        mapping = config.get_guild_alliance_mapping(interaction.guild_id)
        if mapping is None:
            await interaction.response.send_message(
                "ℹ️ This server isn't linked to Map Manager, so there's nothing to remove.",
                ephemeral=True,
            )
            return
        view = _UnlinkConfirm(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            alliance_name=mapping["alliance_name"],
        )
        await interaction.response.send_message(
            f"⚠️ Remove the Map Manager link for **{mapping['alliance_name']}** "
            f"(server {mapping['server']})? Your alliance's data stays in Map Manager; "
            "this only disconnects this Discord server from it.",
            view=view,
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(MapManagerCog(bot))

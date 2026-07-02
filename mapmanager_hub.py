"""mapmanager_hub.py — the single `/map_manager` hub (embed + button grid, #338).

Replaces the `/map_manager setup|change|unlink` subcommands with one command that
opens a status hub, mirroring `train_hub.py` / the Events-hub pattern. The hub is
admin-gated (running it asserts this guild represents the alliance) and adapts to
the current link state:

- **Not linked** → 🔗 Link this server (the Premium-gated link action)
- **Linked**     → ✏️ Change link · 🔌 Unlink, plus a 🌐 link button to the
  alliance's Map Manager page when MM returned an id

All MM I/O goes through ``mapmanager_client``; the Premium gate (link only) +
the Discord-admin check mirror the rest of the bot. The modals + link helpers
live here — they're the flows the buttons drive; ``mapmanager_cog.py`` is just
the thin command that opens this hub. See ``docs/BOT_INTEGRATION_HANDOFF.md`` in
the Map Manager repo for the contract.
"""

from __future__ import annotations

from typing import Optional

import discord

import config
import mapmanager_client
import premium

try:
    import sentry_sdk
except Exception:  # pragma: no cover - sentry optional in some envs
    sentry_sdk = None


MAPMANAGER_HUB_TITLE = "🗺️ Map Manager"
MAPMANAGER_HUB_CMD = "/map_manager"

# Hub button labels (kept as constants per the HUB_BTN_* convention).
MM_HUB_BTN_LINK = "🔗 Link this server"
MM_HUB_BTN_CHANGE = "✏️ Change link"
MM_HUB_BTN_UNLINK = "🔌 Unlink"
MM_HUB_BTN_OPEN = "🌐 Open Map Manager"

_DENY_NOT_OWNER = "⛔ Only the person who opened this hub can use these buttons."

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

    Per the integration design, opening `/map_manager` is the assertion that this
    guild represents the alliance — only a guild admin can make it, so a guild
    admin (not the bot's leadership role) is the gate here.
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
        await interaction.followup.send(
            "⚠️ Map Manager accepted the link, but I couldn't save it on my side. "
            "Re-open `/map_manager` and try again.",
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
    """Two-button confirm for the hub's Unlink action. Ephemeral, so it follows
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


# ── Hub embed ────────────────────────────────────────────────────────────────────


def _build_mapmanager_hub_embed(guild_id: int, *, mapping: Optional[dict], configured: bool):
    """Hub embed showing the server's current Map Manager link (DB-only read)."""
    embed = discord.Embed(title=MAPMANAGER_HUB_TITLE, color=discord.Color.blurple())
    if mapping:
        name = mapping.get("alliance_name") or "your alliance"
        server = mapping.get("server")
        embed.description = (
            f"This server is linked to **{name}** (server {server}) on Map Manager.\n\n"
            "Use the buttons below to change the link details or disconnect."
        )
    else:
        embed.description = (
            "This server isn't linked to Map Manager yet.\n\n"
            "Click **Link this server** to connect your alliance to the Map Manager "
            "web app (Premium)."
        )
    if not configured:
        embed.add_field(
            name="⚠️ Not switched on",
            value=(
                "The integration isn't configured on this bot yet. If you're the "
                "operator, set `MAPMANAGER_API_URL` and `MAPMANAGER_API_KEY`."
            ),
            inline=False,
        )
    embed.set_footer(text="Map Manager hub · buttons below")
    return embed


# ── Hub view ─────────────────────────────────────────────────────────────────────


class _MapManagerHubView(discord.ui.View):
    """Hub button grid. Adapts to whether this server is already linked. Only the
    admin who opened the hub can use the buttons."""

    def __init__(
        self,
        bot,
        guild_id: int,
        owner_user_id: int,
        *,
        mapping: Optional[dict],
        configured: bool,
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.mapping = mapping
        self.configured = configured
        self.message: Optional[discord.Message] = None
        self._build_buttons()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=MAPMANAGER_HUB_CMD)

    def _add(self, label, style, row, cb):
        btn = discord.ui.Button(label=label[:80], style=style, row=row)
        btn.callback = cb
        self.add_item(btn)

    def _build_buttons(self):
        if self.mapping:
            self._add(MM_HUB_BTN_CHANGE, discord.ButtonStyle.primary, 0, self._on_change)
            self._add(MM_HUB_BTN_UNLINK, discord.ButtonStyle.danger, 0, self._on_unlink)
            url = mapmanager_client.alliance_dashboard_url(self.mapping.get("mm_alliance_id"))
            if url:
                self.add_item(
                    discord.ui.Button(
                        label=MM_HUB_BTN_OPEN, style=discord.ButtonStyle.link, url=url, row=1
                    )
                )
        else:
            self._add(MM_HUB_BTN_LINK, discord.ButtonStyle.success, 0, self._on_link)

    # ── callbacks ──────────────────────────────────────────────────────────────

    async def _on_link(self, inter: discord.Interaction):
        # Linking is the Premium-gated action (creating the alliance link).
        if not await premium.feature_gate(
            "map_manager", self.guild_id, interaction=inter, bot=self.bot
        ):
            await inter.response.send_message(
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
            await inter.response.send_message(_NOT_CONFIGURED_MSG, ephemeral=True)
            return
        await inter.response.send_modal(_SetupModal())

    async def _on_change(self, inter: discord.Interaction):
        if not mapmanager_client.is_configured():
            await inter.response.send_message(_NOT_CONFIGURED_MSG, ephemeral=True)
            return
        mapping = config.get_guild_alliance_mapping(self.guild_id)
        if mapping is None:
            await inter.response.send_message(
                "ℹ️ This server isn't linked to Map Manager yet.", ephemeral=True
            )
            return
        await inter.response.send_modal(_ChangeModal(mapping["server"], mapping["alliance_name"]))

    async def _on_unlink(self, inter: discord.Interaction):
        mapping = config.get_guild_alliance_mapping(self.guild_id)
        if mapping is None:
            await inter.response.send_message(
                "ℹ️ This server isn't linked to Map Manager, so there's nothing to remove.",
                ephemeral=True,
            )
            return
        view = _UnlinkConfirm(
            guild_id=self.guild_id,
            user_id=inter.user.id,
            alliance_name=mapping["alliance_name"],
        )
        await inter.response.send_message(
            f"⚠️ Remove the Map Manager link for **{mapping['alliance_name']}** "
            f"(server {mapping['server']})? Your alliance's data stays in Map Manager; "
            "this only disconnects this Discord server from it.",
            view=view,
            ephemeral=True,
        )


# ── Entry point ──────────────────────────────────────────────────────────────────


async def handle_mapmanager_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/map_manager` (and the `/setup` hub's Map Manager
    button). Admin-gated; opens the status hub."""
    if not _is_admin(interaction):
        await _reject_non_admin(interaction)
        return

    guild_id = interaction.guild_id
    configured = mapmanager_client.is_configured()
    mapping = config.get_guild_alliance_mapping(guild_id)
    embed = _build_mapmanager_hub_embed(guild_id, mapping=mapping, configured=configured)
    view = _MapManagerHubView(
        bot, guild_id, interaction.user.id, mapping=mapping, configured=configured
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()

"""
export_import_cog.py — Discord-side surface for /export_config and
/import_config (issue #42).

`/export_config` collects a serializable view of the guild's bot config
across the categories the leader selects, then DMs the result as a JSON
attachment. `/import_config` reverses the process: parse the attached
file, walk leadership through a remap wizard for every channel and role
referenced in the export, then write the imported config to the
destination guild's tables.

The data-layer logic (serialization, validation, applying to SQLite)
lives in ``config_export.py`` and is unit-testable without any Discord
dependencies. This cog is the wizard + slash-command glue.
"""

from __future__ import annotations

import io
from typing import Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

import config_export
import wizard_registry
from setup_cog import _has_leadership_or_admin


WIZARD_TIMEOUT = 600  # 10 minutes per prompt


# ── Lookup helpers ───────────────────────────────────────────────────────────


def _build_channel_lookup(guild: discord.Guild | None) -> Callable[[int], str]:
    """Return a `(channel_id) -> display name` function for the given guild.
    Falls back to a placeholder when the guild is unavailable or the
    channel/thread can't be found — almost always means the channel was
    deleted between when it was configured and when the export ran."""
    if guild is None:
        return lambda cid: "(unknown — guild unavailable)"

    def _lookup(channel_id: int) -> str:
        if not channel_id:
            return ""
        ch = guild.get_channel(channel_id)
        if ch is not None:
            return f"#{ch.name}"
        thread = guild.get_thread(channel_id)
        if thread is not None:
            return f"#{thread.name} (thread)"
        return "(unknown — likely deleted)"

    return _lookup


def _build_role_lookup(guild: discord.Guild | None) -> Callable[[int], str]:
    if guild is None:
        return lambda rid: "(unknown — guild unavailable)"

    def _lookup(role_id: int) -> str:
        if not role_id:
            return ""
        role = guild.get_role(role_id)
        if role is not None:
            return f"@{role.name}"
        return "(unknown — likely deleted)"

    return _lookup


# ── Multi-select category picker ─────────────────────────────────────────────


class CategoryPickerView(discord.ui.View):
    """Multi-select rendering the categories the source guild has data for.
    The leader picks any subset and clicks Confirm; categories the guild
    has no data for are silently filtered upstream so they never appear."""

    def __init__(self, available_keys: list[str]):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = False
        self.selected: list[str] = list(available_keys)

        options = [
            discord.SelectOption(
                label=config_export.CATEGORY_LABELS[k][:100],
                value=k,
                default=True,
            )
            for k in available_keys
        ]
        self._select = discord.ui.Select(
            placeholder="Pick categories to export (all selected by default)",
            options=options,
            min_values=1,
            max_values=len(options),
        )

        async def _on_select(inter: discord.Interaction):
            self.selected = list(self._select.values)
            await inter.response.defer()

        self._select.callback = _on_select
        self.add_item(self._select)

        confirm_btn = discord.ui.Button(label="✅ Confirm", style=discord.ButtonStyle.success)
        cancel_btn  = discord.ui.Button(label="❌ Cancel",  style=discord.ButtonStyle.danger)

        async def _on_confirm(inter: discord.Interaction):
            self.confirmed = True
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        async def _on_cancel(inter: discord.Interaction):
            self.confirmed = False
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        confirm_btn.callback = _on_confirm
        cancel_btn.callback  = _on_cancel
        self.add_item(confirm_btn)
        self.add_item(cancel_btn)


# ── Per-group remap prompt (channel or role) ─────────────────────────────────


class _ChannelRemapView(discord.ui.View):
    """One channel-remap prompt: pick a new channel/thread, keep current,
    or skip. The picked decision is stored on `.decision` as a tuple
    matching `config_export.RemapDecisions`."""

    def __init__(self, *, allow_keep_current: bool):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.decision: Optional[tuple] = None

        # Native channel select — covers text + threads.
        select = discord.ui.ChannelSelect(
            placeholder="Pick the new channel or thread…",
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.news,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.news_thread,
            ],
            min_values=1, max_values=1,
        )

        async def _on_pick(inter: discord.Interaction):
            picked = select.values[0]
            self.decision = ("set", int(picked.id))
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content=f"✅ Picked: <#{picked.id}>", view=self,
            )
            self.stop()

        select.callback = _on_pick
        self.add_item(select)

        if allow_keep_current:
            keep_btn = discord.ui.Button(label="Keep current", style=discord.ButtonStyle.secondary)

            async def _on_keep(inter: discord.Interaction):
                self.decision = ("keep_current",)
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content="↩️ Keeping current values.", view=self,
                )
                self.stop()

            keep_btn.callback = _on_keep
            self.add_item(keep_btn)

        skip_btn = discord.ui.Button(label="Skip (clear)", style=discord.ButtonStyle.secondary)

        async def _on_skip(inter: discord.Interaction):
            self.decision = ("skip",)
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content="⏭️ Skipped — affected fields will be cleared.", view=self,
            )
            self.stop()

        skip_btn.callback = _on_skip
        self.add_item(skip_btn)


class _RoleRemapView(discord.ui.View):
    """Role analogue of `_ChannelRemapView`."""

    def __init__(self, *, allow_keep_current: bool):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.decision: Optional[tuple] = None

        select = discord.ui.RoleSelect(
            placeholder="Pick the new role…",
            min_values=1, max_values=1,
        )

        async def _on_pick(inter: discord.Interaction):
            picked = select.values[0]
            self.decision = ("set", int(picked.id))
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content=f"✅ Picked: <@&{picked.id}>", view=self,
            )
            self.stop()

        select.callback = _on_pick
        self.add_item(select)

        if allow_keep_current:
            keep_btn = discord.ui.Button(label="Keep current", style=discord.ButtonStyle.secondary)

            async def _on_keep(inter: discord.Interaction):
                self.decision = ("keep_current",)
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content="↩️ Keeping current values.", view=self,
                )
                self.stop()

            keep_btn.callback = _on_keep
            self.add_item(keep_btn)

        skip_btn = discord.ui.Button(label="Skip (clear)", style=discord.ButtonStyle.secondary)

        async def _on_skip(inter: discord.Interaction):
            self.decision = ("skip",)
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content="⏭️ Skipped — affected fields will be cleared.", view=self,
            )
            self.stop()

        skip_btn.callback = _on_skip
        self.add_item(skip_btn)


# ── Sheet ID prompt ──────────────────────────────────────────────────────────


class _SheetIdView(discord.ui.View):
    """Three-to-four-button picker for the source guild's spreadsheet ID:
    use exported, keep current (only when current is non-empty), enter
    new (modal), or skip (clear)."""

    def __init__(self, exported_id: str, current_id: str):
        super().__init__(timeout=WIZARD_TIMEOUT)
        # decision is one of:
        #   exported_id (str) — write the exported ID
        #   current_id  (str) — keep the new guild's existing ID
        #   ""                — skip (clear, /setup needed afterwards)
        #   None              — pending / cancelled
        self.decision: Optional[str] = None

        use_exported_btn = discord.ui.Button(
            label="Use exported", style=discord.ButtonStyle.success,
        )
        async def _on_use_exported(inter: discord.Interaction):
            self.decision = exported_id
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content=f"✅ Will use exported sheet ID `{exported_id}`.",
                view=self,
            )
            self.stop()
        use_exported_btn.callback = _on_use_exported
        self.add_item(use_exported_btn)

        if current_id:
            keep_btn = discord.ui.Button(
                label="Keep current", style=discord.ButtonStyle.secondary,
            )
            async def _on_keep_current(inter: discord.Interaction):
                self.decision = current_id
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content=f"↩️ Keeping current sheet ID `{current_id}`.",
                    view=self,
                )
                self.stop()
            keep_btn.callback = _on_keep_current
            self.add_item(keep_btn)

        new_btn = discord.ui.Button(
            label="Enter a different ID", style=discord.ButtonStyle.primary,
        )
        async def _on_new(inter: discord.Interaction):
            class _SheetModal(discord.ui.Modal, title="Enter sheet ID"):
                _ti = discord.ui.TextInput(
                    label="Spreadsheet ID",
                    placeholder="e.g. 1abc...XYZ — long alphanumeric ID from the sheet URL",
                    required=True, max_length=200,
                )
                async def on_submit(self, modal_inter: discord.Interaction):
                    await modal_inter.response.defer()
                    self.stop()
            modal = _SheetModal()
            await inter.response.send_modal(modal)
            await modal.wait()
            value = (modal._ti.value or "").strip()
            if not value:
                self.decision = None
                self.stop()
                return
            self.decision = value
            for item in self.children: item.disabled = True
            try:
                await inter.edit_original_response(
                    content=f"✅ Will use sheet ID `{value}`.", view=self,
                )
            except Exception:
                pass
            self.stop()
        new_btn.callback = _on_new
        self.add_item(new_btn)

        skip_btn = discord.ui.Button(
            label="Skip (clear)", style=discord.ButtonStyle.danger,
        )
        async def _on_skip(inter: discord.Interaction):
            self.decision = ""
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content="⏭️ Sheet ID will be cleared — run `/setup` after import.",
                view=self,
            )
            self.stop()
        skip_btn.callback = _on_skip
        self.add_item(skip_btn)


# ── Cog ──────────────────────────────────────────────────────────────────────


class ExportImportCog(commands.Cog):
    """Slash commands for moving a guild's bot config between Discord
    servers (or backing up + restoring within the same server)."""

    def __init__(self, bot):
        self.bot = bot

    # ── /export_config ────────────────────────────────────────────────────

    @app_commands.command(
        name="export_config",
        description="Export your alliance's bot config to a JSON file you can re-import to another server",
    )
    async def export_config(self, interaction: discord.Interaction):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/export_config`.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        guild_id = interaction.guild_id
        user = interaction.user
        channel_lookup = _build_channel_lookup(guild)
        role_lookup    = _build_role_lookup(guild)

        available = config_export.collect_available_categories(
            guild_id,
            channel_lookup=channel_lookup,
            role_lookup=role_lookup,
        )
        if not available:
            await interaction.response.send_message(
                "ℹ️ Nothing to export — the guild has no saved config in any "
                "category yet. Run `/setup` first.",
                ephemeral=True,
            )
            return

        view = CategoryPickerView(available)
        await interaction.response.send_message(
            "📦 **Export Config**\n"
            "Pick the categories you want to export. Categories with no saved "
            "data are hidden. After confirming, the bot will DM you a JSON "
            "file that you (or another officer) can attach to `/import_config` "
            "in another server.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.confirmed:
            await interaction.followup.send("❌ Cancelled.", ephemeral=True)
            return

        export = config_export.build_export(
            guild_id,
            categories=view.selected,
            source_guild_name=(guild.name if guild else "Unknown"),
            exporter_user_id=user.id,
            channel_lookup=channel_lookup,
            role_lookup=role_lookup,
        )
        payload = config_export.serialize_to_json_bytes(export)
        filename = f"lw-alliance-helper-config-{guild_id}.json"

        # DM the file. Channel/role IDs in the JSON are guild-specific so
        # we deliver privately rather than posting in the channel.
        try:
            dm = await user.create_dm()
            await dm.send(
                "📦 **Your alliance's bot config export** — keep this file "
                "private (it contains your sheet ID and channel/role IDs). "
                "Attach it to `/import_config` in the destination server.",
                file=discord.File(io.BytesIO(payload), filename=filename),
            )
            await interaction.followup.send(
                f"✅ Sent. Check your DMs for **{filename}** "
                f"({len(view.selected)} categor"
                f"{'y' if len(view.selected) == 1 else 'ies'} included).",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ Couldn't DM you the file — you have DMs from server members "
                "disabled. Re-enable DMs for this server and run `/export_config` again.",
                ephemeral=True,
            )

    # ── /import_config ────────────────────────────────────────────────────

    @app_commands.command(
        name="import_config",
        description="Import a JSON config export from another server (or restore your own backup)",
    )
    @app_commands.describe(
        file="The JSON file produced by /export_config",
    )
    async def import_config(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
    ):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/import_config`.",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        guild_id = interaction.guild_id
        user = interaction.user

        cancel_event = wizard_registry.register(user.id)

        await interaction.response.defer(ephemeral=False)
        channel = interaction.channel

        # 1) Download + validate.
        try:
            file_bytes = await file.read()
        except Exception as e:
            await channel.send(f"⚠️ Couldn't read the attached file: {e}")
            wizard_registry.unregister(user.id, cancel_event)
            return

        try:
            parsed = config_export.parse_and_validate(file_bytes)
        except config_export.ImportValidationError as e:
            await channel.send(f"⚠️ Import failed validation:\n{e}")
            wizard_registry.unregister(user.id, cancel_event)
            return

        source_guild = parsed.get("source_guild") or {}
        source_guild_id   = int(source_guild.get("id") or 0)
        source_guild_name = source_guild.get("name") or "Unknown"
        cats = parsed.get("categories_present") or []
        same_guild = (source_guild_id == guild_id)
        warnings = []
        if parsed.get("unknown_keys"):
            warnings.append(
                "Ignored unknown keys (this bot is older than the exporter, "
                "or the file was hand-edited): "
                + ", ".join(parsed["unknown_keys"][:5])
                + ("…" if len(parsed["unknown_keys"]) > 5 else "")
            )

        # 2) Preview + confirm.
        cat_labels = [config_export.CATEGORY_LABELS[c] for c in cats]
        embed = discord.Embed(
            title="📥 Import Preview",
            description=(
                f"**Source server:** {source_guild_name} (`{source_guild_id}`)\n"
                f"**Schema version:** v{parsed.get('schema_version')}\n"
                f"**Exported at:** {parsed.get('exported_at', '?')}\n"
                + ("**Same-guild restore** — channel/role IDs will likely still "
                   "resolve, but the wizard still runs in case anything was "
                   "revamped.\n" if same_guild else "")
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Will import",
            value="\n".join(f"• {l}" for l in cat_labels) or "*nothing*",
            inline=False,
        )
        if warnings:
            embed.add_field(name="⚠️ Warnings",
                            value="\n".join(warnings),
                            inline=False)

        class _ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.confirmed = False

            @discord.ui.button(label="Continue → walk through remap",
                               style=discord.ButtonStyle.success)
            async def go(self, inter: discord.Interaction, button: discord.ui.Button):
                self.confirmed = True
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
            async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
                self.confirmed = False
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

        confirm = _ConfirmView()
        await channel.send(embed=embed, view=confirm)
        await wizard_registry.wait_view_or_cancel(confirm, cancel_event)
        if cancel_event.is_set() or not confirm.confirmed:
            await channel.send("❌ Import cancelled.")
            wizard_registry.unregister(user.id, cancel_event)
            return

        # 3) Sheet ID prompt.
        exported_sheet_id = ""
        try:
            exported_sheet_id = ((parsed.get("data") or {})
                                 .get("core") or {}).get("travels", {}).get("spreadsheet_id", "") or ""
        except AttributeError:
            exported_sheet_id = ""
        from config import get_config
        cur_cfg = get_config(guild_id)
        current_sheet_id = (cur_cfg.spreadsheet_id if cur_cfg else "") or ""

        sheet_decision: Optional[str] = None
        if exported_sheet_id or current_sheet_id:
            sheet_view = _SheetIdView(exported_sheet_id, current_sheet_id)
            await channel.send(
                f"📄 **Spreadsheet ID**\n"
                f"Source server used sheet ID: `{exported_sheet_id or '(empty)'}`\n"
                f"This server currently has: `{current_sheet_id or '(empty)'}`\n\n"
                f"Pick which sheet to use. If you keep the source guild's "
                f"sheet ID, remember to share that sheet with the bot's "
                f"service-account email (the same one your alliance uses).",
                view=sheet_view,
            )
            await wizard_registry.wait_view_or_cancel(sheet_view, cancel_event)
            if cancel_event.is_set() or sheet_view.decision is None:
                await channel.send("❌ Import cancelled.")
                wizard_registry.unregister(user.id, cancel_event)
                return
            sheet_decision = sheet_view.decision

        # 4) Walk remap groups.
        channel_groups, role_groups = config_export.discover_remap_groups(parsed)

        channel_decisions: dict[int, tuple] = {}
        role_decisions:    dict[int, tuple] = {}

        if channel_groups or role_groups:
            await channel.send(
                f"🔁 **Channel & role remap** — {len(channel_groups)} channel "
                f"slot(s) and {len(role_groups)} role slot(s) to map.\n"
                f"For each group, pick the new equivalent in this server, "
                f"keep the current values (no overwrite), or skip (clear)."
            )

        for grp in channel_groups:
            purposes = "\n".join(f"• {p}" for p in grp.purposes)
            prompt = (
                f"**Channel slot {len(channel_decisions) + 1} of {len(channel_groups)}**\n"
                f"Old channel: **{grp.source_name or '(unknown)'}** "
                f"(source ID `{grp.source_id}`)\n"
                f"Used for:\n{purposes}"
            )
            view = _ChannelRemapView(allow_keep_current=True)
            await channel.send(prompt, view=view)
            await wizard_registry.wait_view_or_cancel(view, cancel_event)
            if cancel_event.is_set():
                await channel.send("❌ Import cancelled.")
                wizard_registry.unregister(user.id, cancel_event)
                return
            if view.decision is None:
                await channel.send("⏰ Timed out. Run `/import_config` again with the same file.")
                wizard_registry.unregister(user.id, cancel_event)
                return
            channel_decisions[grp.source_id] = view.decision

        for grp in role_groups:
            purposes = "\n".join(f"• {p}" for p in grp.purposes)
            prompt = (
                f"**Role slot {len(role_decisions) + 1} of {len(role_groups)}**\n"
                f"Old role: **{grp.source_name or '(unknown)'}** "
                f"(source ID `{grp.source_id}`)\n"
                f"Used for:\n{purposes}"
            )
            view = _RoleRemapView(allow_keep_current=True)
            await channel.send(prompt, view=view)
            await wizard_registry.wait_view_or_cancel(view, cancel_event)
            if cancel_event.is_set():
                await channel.send("❌ Import cancelled.")
                wizard_registry.unregister(user.id, cancel_event)
                return
            if view.decision is None:
                await channel.send("⏰ Timed out. Run `/import_config` again with the same file.")
                wizard_registry.unregister(user.id, cancel_event)
                return
            role_decisions[grp.source_id] = view.decision

        # 5) Apply.
        decisions = config_export.RemapDecisions(
            channel_decisions=channel_decisions,
            role_decisions=role_decisions,
            spreadsheet_id=sheet_decision,
            same_guild=same_guild,
        )
        summary = config_export.apply_import(guild_id, parsed, decisions)

        # 6) Report.
        result_embed = discord.Embed(
            title="📥 Import Result",
            color=(discord.Color.green() if not summary["skipped"]
                   else discord.Color.orange()),
        )
        if summary["applied"]:
            applied_labels = "\n".join(
                f"• {config_export.CATEGORY_LABELS[k]}" for k in summary["applied"]
            )
            result_embed.add_field(name="Applied", value=applied_labels, inline=False)
        if summary["skipped"]:
            skipped_text = "\n".join(
                f"• **{s['category']}** — {s['reason']}\n  "
                f"  Re-run `/import_config` with a corrected file to retry "
                f"just this category."
                for s in summary["skipped"]
            )
            result_embed.add_field(
                name="❌ Skipped (failed)",
                value=skipped_text[:1024],
                inline=False,
            )
        if summary["warnings"]:
            result_embed.add_field(
                name="⚠️ Warnings",
                value="\n".join(f"• {w}" for w in summary["warnings"][:5])[:1024],
                inline=False,
            )
        result_embed.set_footer(text="Run /view_configuration to see the live state.")
        await channel.send(embed=result_embed)
        wizard_registry.unregister(user.id, cancel_event)


async def setup(bot):
    await bot.add_cog(ExportImportCog(bot))

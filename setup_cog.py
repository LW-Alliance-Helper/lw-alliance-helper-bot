"""
setup_cog.py — /setup wizard for new guilds

Walks a server admin through configuring the bot using Discord's native
role and channel select menus. All values are saved to the config database.

Commands:
  /setup        — Run the full setup wizard
  /setup_status — Show the current config for this server
  /setup_reset  — Clear this server's config and start over (admin only)
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from config import (
    get_config, get_or_create_config, save_config, update_config_field,
    GuildConfig,
)

WIZARD_TIMEOUT = 120  # 2 minutes per step


# ── Step views ─────────────────────────────────────────────────────────────────

class RoleSelectStep(discord.ui.View):
    def __init__(self, placeholder: str):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected_role = None
        self.confirmed     = False

        select = discord.ui.RoleSelect(placeholder=placeholder, min_values=1, max_values=1)
        async def _cb(interaction: discord.Interaction):
            self.selected_role = select.values[0]
            self.confirmed     = True
            select.disabled    = True
            await interaction.response.edit_message(
                content=f"✅ Selected: **{self.selected_role.name}**",
                view=self,
            )
            self.stop()
        select.callback = _cb
        self.add_item(select)


class ChannelSelectStep(discord.ui.View):
    def __init__(self, placeholder: str, channel_types=None):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected_channel = None
        self.confirmed        = False

        types  = channel_types or [discord.ChannelType.text]
        select = discord.ui.ChannelSelect(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            channel_types=types,
        )
        async def _cb(interaction: discord.Interaction):
            self.selected_channel = select.values[0]
            self.confirmed        = True
            select.disabled       = True
            await interaction.response.edit_message(
                content=f"✅ Selected: **{self.selected_channel.name}**",
                view=self,
            )
            self.stop()
        select.callback = _cb
        self.add_item(select)


class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = None

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class TextInputModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, placeholder: str = "", default: str = ""):
        super().__init__(title=title)
        self.value = None
        self.field = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            default=default,
            required=True,
            max_length=200,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        self.value = self.field.value.strip()
        await interaction.response.defer()
        self.stop()


class ModalLaunchView(discord.ui.View):
    """Button that opens a modal — used for text input steps."""
    def __init__(self, modal: TextInputModal):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.modal     = modal
        self.confirmed = False

    @discord.ui.button(label="✏️ Enter Value", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(self.modal)
        await self.modal.wait()
        self.confirmed = True
        button.disabled = True
        try:
            await interaction.message.edit(
                content=f"✅ Entered: **{self.modal.value}**",
                view=self,
            )
        except discord.HTTPException:
            pass
        self.stop()


# ── Setup wizard flow ──────────────────────────────────────────────────────────

async def run_setup(interaction: discord.Interaction, bot):
    guild_id = interaction.guild_id
    cfg      = get_or_create_config(guild_id)
    channel  = interaction.channel
    user     = interaction.user

    def check_interaction(i: discord.Interaction):
        return i.user == user and i.guild_id == guild_id

    async def send_step(content: str, view: discord.ui.View):
        msg = await channel.send(content, view=view)
        await view.wait()
        return view

    await channel.send(
        "⚙️ **Alliance Helper Setup**\n\n"
        "I'll walk you through configuring the bot for your server. "
        "This should take about 2 minutes.\n\n"
        "*You can run `/setup` again at any time to update your settings.*"
    )

    # ── Step 1: Member role ────────────────────────────────────────────────────
    await channel.send("**Step 1 of 9 — Member Role**\nSelect the role that all alliance members have:")
    v = RoleSelectStep("Select member role...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.member_role_name = v.selected_role.name
    cfg.member_role_id   = v.selected_role.id

    # ── Step 2: Leadership role ────────────────────────────────────────────────
    await channel.send("**Step 2 of 9 — Leadership Role**\nSelect the elevated role for alliance leadership:")
    v = RoleSelectStep("Select leadership role...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.leadership_role_name = v.selected_role.name

    # ── Step 3: Leadership channel ─────────────────────────────────────────────
    await channel.send(
        "**Step 3 of 9 — Leadership Channel**\n"
        "Select the private channel where leadership commands will be used:"
    )
    v = ChannelSelectStep("Select leadership channel...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.leadership_channel_id    = v.selected_channel.id
    cfg.leadership_category_id   = getattr(v.selected_channel, "category_id", 0) or 0

    # ── Step 4: Announcement channel ──────────────────────────────────────────
    await channel.send(
        "**Step 4 of 9 — Announcement Channel**\n"
        "Select the public channel where event announcements will be posted:"
    )
    v = ChannelSelectStep("Select announcement channel...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.announcement_channel_id = v.selected_channel.id

    # ── Step 5: Survey channel ─────────────────────────────────────────────────
    await channel.send(
        "**Step 5 of 9 — Survey Channel**\n"
        "Select the channel where the squad powers survey button will live:"
    )
    v = ChannelSelectStep("Select survey channel...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.survey_channel_id = v.selected_channel.id

    # ── Step 6: Survey notification channel ───────────────────────────────────
    await channel.send(
        "**Step 6 of 9 — Survey Notification Channel**\n"
        "Select the channel where leadership will see new survey submissions:"
    )
    v = ChannelSelectStep("Select survey notification channel...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.survey_notify_channel_id = v.selected_channel.id

    # ── Step 7: Storm log thread ───────────────────────────────────────────────
    await channel.send(
        "**Step 7 of 9 — Storm Log Thread**\n"
        "Select the thread where DS/CS participation logs will be posted:\n"
        "*(Create a thread in your leadership channel first if you haven't already)*"
    )
    v = ChannelSelectStep(
        "Select storm log thread...",
        channel_types=[discord.ChannelType.public_thread, discord.ChannelType.private_thread],
    )
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.storm_log_thread_id = v.selected_channel.id

    # ── Step 8: Google Sheet ID ────────────────────────────────────────────────
    await channel.send(
        "**Step 8 of 9 — Google Sheet ID**\n"
        "Enter your Google Sheet ID. This is the long string in your sheet's URL:\n"
        "`https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID`**`/edit`\n\n"
        f"Also make sure you've shared your sheet with **`sheet-connector@lw-alliance-helper.iam.gserviceaccount.com`** (Editor access)."
    )
    modal    = TextInputModal("Google Sheet ID", "Sheet ID", placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
    modal_v  = ModalLaunchView(modal)
    await channel.send("\u200b", view=modal_v)
    await modal_v.wait()
    if not modal_v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    import os
    # Store sheet ID as an environment variable override per guild
    # For now store in a simple way — future enhancement: per-guild sheet IDs
    # We'll save it in the config notes field for now and use os.environ
    sheet_id = modal.value

    # ── Step 9: Event anchor date ──────────────────────────────────────────────
    await channel.send(
        "**Step 9 of 9 — Event Cycle Anchor Date**\n"
        "Enter a date when your alliance events ran (used to calculate the 3-day cycle).\n"
        "Format: `YYYY-MM-DD` (e.g. `2026-03-30`)"
    )
    modal   = TextInputModal("Event Anchor Date", "Anchor date (YYYY-MM-DD)", placeholder="2026-03-30", default=cfg.anchor_date)
    modal_v = ModalLaunchView(modal)
    await channel.send("\u200b", view=modal_v)
    await modal_v.wait()
    if not modal_v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    try:
        from datetime import date
        date.fromisoformat(modal.value)
        cfg.anchor_date = modal.value
    except ValueError:
        await channel.send(f"⚠️ Invalid date format `{modal.value}` — using default `{cfg.anchor_date}`.")

    # ── Confirm and save ───────────────────────────────────────────────────────
    embed = discord.Embed(
        title="⚙️ Setup Summary",
        description="Please confirm these settings:",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Member Role",        value=cfg.member_role_name,                  inline=True)
    embed.add_field(name="Leadership Role",    value=cfg.leadership_role_name,              inline=True)
    embed.add_field(name="\u200b",             value="\u200b",                              inline=True)
    embed.add_field(name="Leadership Channel", value=f"<#{cfg.leadership_channel_id}>",    inline=True)
    embed.add_field(name="Announcements",      value=f"<#{cfg.announcement_channel_id}>",  inline=True)
    embed.add_field(name="Survey Channel",     value=f"<#{cfg.survey_channel_id}>",        inline=True)
    embed.add_field(name="Survey Notifs",      value=f"<#{cfg.survey_notify_channel_id}>", inline=True)
    embed.add_field(name="Storm Log Thread",   value=f"<#{cfg.storm_log_thread_id}>",      inline=True)
    embed.add_field(name="Anchor Date",        value=cfg.anchor_date,                      inline=True)
    embed.add_field(name="Sheet ID",           value=f"`{sheet_id[:20]}...`",              inline=False)

    confirm_view = ConfirmView()
    await channel.send(embed=embed, view=confirm_view)
    await confirm_view.wait()

    if not confirm_view.confirmed:
        await channel.send("❌ Setup cancelled. Run `/setup` to start again.")
        return

    # Save config — including the sheet ID in the database
    cfg.setup_complete  = True
    cfg.spreadsheet_id  = sheet_id
    save_config(cfg)

    # Also write to JSON file for backward compatibility
    import json
    from config import SHEETS_MAP_PATH
    try:
        with open(SHEETS_MAP_PATH) as f:
            sheet_map = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        sheet_map = {}
    sheet_map[str(guild_id)] = sheet_id
    os.makedirs(os.path.dirname(SHEETS_MAP_PATH), exist_ok=True)
    with open(SHEETS_MAP_PATH, "w") as f:
        json.dump(sheet_map, f)

    await channel.send(
        "✅ **Setup complete!** The bot is ready to use.\n\n"
        "**Next steps:**\n"
        "• Run `/postsurvey` to post the survey button in your survey channel\n"
        "• Run `/schedule_set` to add your first train schedule entries\n"
        "• Run `/setmembertab` to set your active member sheet tab\n"
        "• Use `/help` to see all available commands"
    )
    print(f"[SETUP] Guild {guild_id} setup complete")


# ── Cog ────────────────────────────────────────────────────────────────────────

class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Configure Alliance Helper for your server")
    async def setup(self, interaction: discord.Interaction):
        # Only admins can run setup
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup`.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "⚙️ Starting setup — check the channel for prompts!", ephemeral=True
        )
        await run_setup(interaction, self.bot)

    @app_commands.command(name="setup_status", description="Show the current bot configuration for this server")
    async def setup_status(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can view setup status.", ephemeral=True
            )
            return

        cfg = get_config(interaction.guild_id)
        if not cfg or not cfg.setup_complete:
            await interaction.response.send_message(
                "⚙️ This server hasn't been set up yet. Run `/setup` to get started.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="⚙️ Current Configuration",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Member Role",        value=cfg.member_role_name,                  inline=True)
        embed.add_field(name="Leadership Role",    value=cfg.leadership_role_name,              inline=True)
        embed.add_field(name="\u200b",             value="\u200b",                              inline=True)
        embed.add_field(name="Leadership Channel", value=f"<#{cfg.leadership_channel_id}>",    inline=True)
        embed.add_field(name="Announcements",      value=f"<#{cfg.announcement_channel_id}>",  inline=True)
        embed.add_field(name="Survey Channel",     value=f"<#{cfg.survey_channel_id}>",        inline=True)
        embed.add_field(name="Survey Notifs",      value=f"<#{cfg.survey_notify_channel_id}>", inline=True)
        embed.add_field(name="Storm Log Thread",   value=f"<#{cfg.storm_log_thread_id}>",      inline=True)
        embed.add_field(name="Member Tab",         value=cfg.tab_member_default,               inline=True)
        embed.add_field(name="Anchor Date",        value=cfg.anchor_date,                      inline=True)
        embed.add_field(name="Cycle Days",         value=str(cfg.cycle_days),                  inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="setup_reset", description="Clear this server's configuration and start over")
    async def setup_reset(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can reset the configuration.", ephemeral=True
            )
            return

        class ConfirmResetView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.confirmed = False

            @discord.ui.button(label="Yes, reset everything", style=discord.ButtonStyle.danger)
            async def confirm(self, inner: discord.Interaction, button: discord.ui.Button):
                self.confirmed = True
                await inner.response.defer()
                self.stop()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, inner: discord.Interaction, button: discord.ui.Button):
                await inner.response.defer()
                self.stop()

        view = ConfirmResetView()
        await interaction.response.send_message(
            "⚠️ Are you sure you want to reset the bot configuration for this server? "
            "This cannot be undone.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.confirmed:
            from config import save_config, GuildConfig
            save_config(GuildConfig(guild_id=interaction.guild_id))
            await interaction.followup.send(
                "✅ Configuration reset. Run `/setup` to configure the bot again.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

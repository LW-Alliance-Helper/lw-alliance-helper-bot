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


def _parse_12h_time(raw: str) -> str:
    """
    Parse a user-entered time like '10:15pm', '9am', '9:00 AM' into
    HH:MM 24h string for storage. Returns None if unparseable.
    """
    import re
    raw = raw.strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)$", raw)
    if not m:
        return None
    hour, minute, period = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if hour < 1 or hour > 12 or minute < 0 or minute > 59:
        return None
    if period == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return f"{hour:02d}:{minute:02d}"


def _parse_month_day(raw: str) -> str:
    """
    Parse 'Month Day' into YYYY-MM-DD using the most recent occurrence.
    Always looks backward — never assumes a future date.
    Examples (today = April 25 2026):
      'February 20' → 2026-02-20  (already passed this year)
      'December 3'  → 2025-12-03  (hasn't happened yet this year, so last year)
      'May 2'       → 2026-05-02  (upcoming this year, but within ~5 days so still this year)
    Rule: if the date this year is in the future beyond today, use last year.
    """
    import re
    from datetime import date, datetime
    raw = raw.strip()
    try:
        parsed = datetime.strptime(raw, "%B %d")
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%b %d")
        except ValueError:
            return None
    today     = date.today()
    this_year = date(today.year, parsed.month, parsed.day)
    last_year = date(today.year - 1, parsed.month, parsed.day)
    # Allow up to 31 days in the future (next upcoming event within a month)
    # Anything further out uses last year's date
    if (this_year - today).days > 31:
        return last_year.isoformat()
    return this_year.isoformat()


# ── Step views ─────────────────────────────────────────────────────────────────

class CreateRoleModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Create a New Role")
        self.role_name = None
        self.field = discord.ui.TextInput(
            label="Role name",
            placeholder="e.g. Member, Alliance Member, Leadership",
            required=True,
            max_length=100,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        self.role_name = self.field.value.strip()
        await interaction.response.defer()
        self.stop()


class RoleSelectStep(discord.ui.View):
    def __init__(self, placeholder: str):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected_role = None
        self.confirmed     = False

        select = discord.ui.RoleSelect(placeholder=placeholder, min_values=1, max_values=1, row=0)
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

    @discord.ui.button(label="➕ Create a new role", style=discord.ButtonStyle.secondary, row=1)
    async def create_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = CreateRoleModal()
        await interaction.response.send_modal(modal)
        await modal.wait()
        if not modal.role_name:
            return
        try:
            new_role = await interaction.guild.create_role(
                name=modal.role_name,
                reason=f"Created during Alliance Helper setup by {interaction.user.display_name}",
            )
            self.selected_role = new_role
            self.confirmed     = True
            for item in self.children:
                item.disabled = True
            await interaction.message.edit(
                content=f"✅ Created and selected new role: **{new_role.name}**",
                view=self,
            )
            self.stop()
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ I don't have permission to create roles. Please create the role manually first, then run `/setup` again.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"⚠️ Could not create role: {e}",
                ephemeral=True,
            )


class CreateChannelModal(discord.ui.Modal):
    def __init__(self, suggested_name: str = ""):
        super().__init__(title="Create a New Channel")
        self.channel_name = None
        self.field = discord.ui.TextInput(
            label="Channel name",
            placeholder=suggested_name or "e.g. announcements",
            default=suggested_name,
            required=True,
            max_length=100,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        self.channel_name = self.field.value.strip().lower().replace(" ", "-")
        await interaction.response.defer()
        self.stop()


class ChannelSelectStep(discord.ui.View):
    def __init__(self, placeholder: str, channel_types=None, suggested_name: str = "", allow_create: bool = True):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected_channel = None
        self.confirmed        = False
        self.suggested_name   = suggested_name
        self.allow_create     = allow_create

        types  = channel_types or [discord.ChannelType.text]
        select = discord.ui.ChannelSelect(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            channel_types=types,
            row=0,
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

        # Show create button for text channels only
        has_threads = channel_types and any(
            t in channel_types for t in [discord.ChannelType.public_thread, discord.ChannelType.private_thread]
        )
        if allow_create and not has_threads:
            create_btn = discord.ui.Button(
                label="➕ Create a new channel",
                style=discord.ButtonStyle.secondary,
                row=1,
            )
            async def _create_cb(interaction: discord.Interaction):
                modal = CreateChannelModal(suggested_name=self.suggested_name)
                await interaction.response.send_modal(modal)
                await modal.wait()
                if not modal.channel_name:
                    return
                try:
                    new_channel = await interaction.guild.create_text_channel(
                        name=modal.channel_name,
                        reason=f"Created during Alliance Helper setup by {interaction.user.display_name}",
                    )
                    self.selected_channel = new_channel
                    self.confirmed        = True
                    for item in self.children:
                        item.disabled = True
                    await interaction.message.edit(
                        content=f"✅ Created and selected: **#{new_channel.name}**",
                        view=self,
                    )
                    self.stop()
                except discord.Forbidden:
                    await interaction.followup.send(
                        "⚠️ I don't have permission to create channels. Please create it manually first, then run `/setup` again.",
                        ephemeral=True,
                    )
                except Exception as e:
                    await interaction.followup.send(
                        f"⚠️ Could not create channel: {e}",
                        ephemeral=True,
                    )
            create_btn.callback = _create_cb
            self.add_item(create_btn)


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
    v = ChannelSelectStep("Select leadership channel...", suggested_name="leadership")
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
    v = ChannelSelectStep("Select announcement channel...", suggested_name="announcements")
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
    v = ChannelSelectStep("Select survey channel...", suggested_name="squad-survey")
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
    v = ChannelSelectStep("Select survey notification channel...", suggested_name="survey-responses")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.survey_notify_channel_id = v.selected_channel.id

    # ── Step 7: Storm log channel ──────────────────────────────────────────────
    await channel.send(
        "**Step 7 of 9 — Storm Log Channel**\n"
        "Select the channel where DS/CS participation logs will be posted:"
    )
    v = ChannelSelectStep(
        "Select storm log channel...",
        suggested_name="storm-log",
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
        "Enter your Google Sheet ID — the long string from your sheet's URL:\n"
        "`https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID`**`/edit`"
    )
    modal   = TextInputModal("Google Sheet ID", "Sheet ID", placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms")
    modal_v = ModalLaunchView(modal)
    await channel.send("\u200b", view=modal_v)
    await modal_v.wait()
    if not modal_v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    sheet_id = modal.value

    # ── Step 9: Share sheet with service account ───────────────────────────────
    SERVICE_ACCOUNT_EMAIL = "sheet-connector@lw-alliance-helper.iam.gserviceaccount.com"
    sharing_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#sharing"

    share_embed = discord.Embed(
        title="**Step 9 of 9 — Share Your Google Sheet**",
        description=(
            "Before finishing, you need to give the bot access to your sheet.\n\n"
            "**Follow these steps:**\n"
            "1️⃣ Click the link below to open your sheet's sharing settings\n"
            "2️⃣ Click **Share** in the top right corner\n"
            "3️⃣ Paste the email address below into the share field\n"
            "4️⃣ Set permission to **Editor**\n"
            "5️⃣ Click **Send** — then come back here and confirm"
        ),
        color=discord.Color.yellow(),
    )
    share_embed.add_field(
        name="📋 Service Account Email (click to copy)",
        value=f"`{SERVICE_ACCOUNT_EMAIL}`",
        inline=False,
    )
    share_embed.add_field(
        name="🔗 Open Your Sheet",
        value=f"[Click here to open sharing settings]({sharing_url})",
        inline=False,
    )

    done_view = ConfirmView()
    done_view.children[0].label = "✅ I've shared the sheet"
    done_view.children[1].label = "❌ Cancel setup"

    await channel.send(embed=share_embed, view=done_view)
    await done_view.wait()

    if not done_view.confirmed:
        await channel.send("❌ Setup cancelled. Run `/setup` to start again.")
        return

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
    embed.add_field(name="Storm Log Channel",  value=f"<#{cfg.storm_log_thread_id}>",      inline=True)
    embed.add_field(name="Sheet ID",           value=f"`{sheet_id[:20]}...`",              inline=False)

    confirm_view = ConfirmView()
    await channel.send(embed=embed, view=confirm_view)
    await confirm_view.wait()

    if not confirm_view.confirmed:
        await channel.send("❌ Setup cancelled. Run `/setup` to start again.")
        return

    # Save config
    cfg.setup_complete  = True
    cfg.spreadsheet_id  = sheet_id
    save_config(cfg)

    await channel.send(
        "✅ **Setup complete!** The bot is ready to use.\n\n"
        "**Next steps:**\n"
        "• Run `/setup_events` to configure your event announcements\n"
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


    @app_commands.command(
        name="setup_events",
        description="Add or edit an event type for announcements (Marauder, Siege, etc.)"
    )
    async def setup_events(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup_events`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚙️ Starting event setup — check the channel for prompts!", ephemeral=True
        )
        await run_event_setup(interaction, self.bot)

    @app_commands.command(
        name="setup_events_list",
        description="View all configured event types for this server"
    )
    async def setup_events_list(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can view event configuration.", ephemeral=True
            )
            return
        from config import get_guild_events
        events = get_guild_events(interaction.guild_id, active_only=False)
        if not events:
            await interaction.response.send_message(
                "No events configured yet. Run `/setup_events` to add one.", ephemeral=True
            )
            return
        lines = []
        for e in events:
            status = "✅ Active" if e["active"] else "❌ Inactive"
            lines.append(
                f"**{e['name']}** (`{e['short_key']}`)\n"
                f"Time: {e['default_time']} {e['timezone']} | "
                f"Schedule: {e['schedule_type']} | {status}"
            )
        embed = discord.Embed(
            title="⚙️ Configured Events",
            description="\n\n".join(lines),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="setup_events_remove",
        description="Deactivate an event type"
    )
    @app_commands.describe(short_key="The short key of the event to remove (e.g. marauder)")
    async def setup_events_remove(self, interaction: discord.Interaction, short_key: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can remove events.", ephemeral=True
            )
            return
        from config import get_guild_event, delete_guild_event
        event = get_guild_event(interaction.guild_id, short_key)
        if not event:
            await interaction.response.send_message(
                f"⚠️ No event found with key `{short_key}`.", ephemeral=True
            )
            return
        delete_guild_event(interaction.guild_id, short_key)
        await interaction.response.send_message(
            f"✅ **{event['name']}** has been deactivated. Run `/setup_events` to re-add it.",
            ephemeral=True,
        )


# ── /setup events wizard ───────────────────────────────────────────────────────

# Common timezones for the selector
COMMON_TIMEZONES = [
    "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
    "America/Sao_Paulo", "Europe/London", "Europe/Paris", "Europe/Berlin",
    "Europe/Moscow", "Asia/Dubai", "Asia/Kolkata", "Asia/Bangkok",
    "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul", "Australia/Sydney",
    "Pacific/Auckland",
]

TIMEZONE_LABELS = {
    "America/New_York":   "ET — Eastern Time (US)",
    "America/Chicago":    "CT — Central Time (US)",
    "America/Denver":     "MT — Mountain Time (US)",
    "America/Los_Angeles":"PT — Pacific Time (US)",
    "America/Sao_Paulo":  "BRT — Brazil Time",
    "Europe/London":      "GMT/BST — London",
    "Europe/Paris":       "CET — Paris/Madrid",
    "Europe/Berlin":      "CET — Berlin/Amsterdam",
    "Europe/Moscow":      "MSK — Moscow",
    "Asia/Dubai":         "GST — Dubai",
    "Asia/Kolkata":       "IST — India",
    "Asia/Bangkok":       "ICT — Bangkok/Jakarta",
    "Asia/Shanghai":      "CST — China/Singapore",
    "Asia/Tokyo":         "JST — Japan",
    "Asia/Seoul":         "KST — Korea",
    "Australia/Sydney":   "AEST — Sydney",
    "Pacific/Auckland":   "NZST — New Zealand",
}


class TimezoneSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.selected  = None
        self.confirmed = False
        # Split into two selects since we have more than 25 options... actually 17, fits in one
        select = discord.ui.Select(
            placeholder="Select your timezone...",
            options=[
                discord.SelectOption(label=TIMEZONE_LABELS[tz], value=tz)
                for tz in COMMON_TIMEZONES
            ],
            row=0,
        )
        async def _cb(interaction: discord.Interaction):
            self.selected  = select.values[0]
            self.confirmed = True
            select.disabled = True
            await interaction.response.edit_message(
                content=f"✅ Timezone: **{TIMEZONE_LABELS[self.selected]}**",
                view=self,
            )
            self.stop()
        select.callback = _cb
        self.add_item(select)


class ScheduleTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.selected  = None

    @discord.ui.button(label="🔁 Repeating cycle", style=discord.ButtonStyle.primary)
    async def repeating(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "repeating"
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="✅ Schedule: **Repeating cycle**", view=self)
        self.stop()

    @discord.ui.button(label="📅 Add manually each time", style=discord.ButtonStyle.secondary)
    async def manual(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "manual"
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="✅ Schedule: **Manual (add per event)**", view=self)
        self.stop()


class YesNoView(discord.ui.View):
    def __init__(self, yes_label="Yes", no_label="No"):
        super().__init__(timeout=120)
        self.selected = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


async def run_event_setup(interaction: discord.Interaction, bot):
    """Walk an admin through setting up one event type."""
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user

    TOTAL_STEPS = 9

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        """Post a prompt, wait for reply. Both stay visible."""
        await channel.send(prompt)
        try:
            reply = await bot.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    async def ask_view(prompt: str, view: discord.ui.View):
        """Post a prompt with a view. Message stays visible, view disables on selection."""
        await channel.send(prompt, view=view)
        await view.wait()
        return view

    await channel.send(
        "⚙️ **Event Setup** — let's configure one event type.\n"
        "*You can run `/setup_events` again to add more events or update existing ones.*"
    )

    # ── Step 1: Event name ────────────────────────────────────────────────────
    name = await ask_text(
        f"**Step 1 of {TOTAL_STEPS} — Event name**\n"
        "What is this event called? (e.g. `Plague Marauder (AE)`, `Zombie Siege`)"
    )
    if not name:
        return

    # Auto-generate short key from name — users never need to see or set this
    import re as _re
    short_key = _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

    # ── Step 2: Timezone ──────────────────────────────────────────────────────
    tz_view = TimezoneSelectView()
    await ask_view(
        f"**Step 2 of {TOTAL_STEPS} — Your timezone**\nSelect the timezone your leadership uses for event times:",
        tz_view,
    )
    if not tz_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
        return
    timezone = tz_view.selected

    # ── Step 4: Default event time ────────────────────────────────────────────
    time_raw = await ask_text(
        f"**Step 3 of {TOTAL_STEPS} — Default event time**\n"
        f"What time does **{name}** usually start?\n"
        f"*(e.g. `10:15pm`, `9:00am`)*"
    )
    if not time_raw:
        return
    import re
    default_time = _parse_12h_time(time_raw)
    if not default_time:
        await channel.send("⚠️ Could not read that time. Try something like `10:15pm` or `9:00am`. Run `/setup_events` to try again.")
        return

    # ── Step 5: Schedule type ─────────────────────────────────────────────────
    sched_view = ScheduleTypeView()
    await ask_view(
        f"**Step 4 of {TOTAL_STEPS} — Schedule**\nDoes this event repeat on a fixed cycle, or do you add it manually each time?",
        sched_view,
    )
    if not sched_view.selected:
        await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
        return
    schedule_type = sched_view.selected

    anchor_date   = ""
    interval_days = 0
    if schedule_type == "repeating":
        anchor_raw = await ask_text(
            f"**Step 4a of {TOTAL_STEPS} — Anchor date**\n"
            "Enter a date when this event occurred (used to calculate the repeating cycle).\n"
            "Format: `YYYY-MM-DD` (e.g. `2026-03-30`)"
        )
        if not anchor_raw:
            return
        anchor_date = _parse_month_day(anchor_raw)
        if not anchor_date:
            await channel.send("⚠️ Could not read that date. Try something like `March 30` or `April 14`. Run `/setup_events` to try again.")
            return

        interval_raw = await ask_text(
            f"**Step 4b of {TOTAL_STEPS} — Cycle interval**\n"
            "How many days between each occurrence? (e.g. `3` for every 3 days)"
        )
        if not interval_raw:
            return
        try:
            interval_days = int(interval_raw)
        except ValueError:
            await channel.send("⚠️ Please enter a whole number. Run `/setup_events` to try again.")
            return

    # ── Step 6: Draft channel ─────────────────────────────────────────────────
    draft_ch_view = ChannelSelectStep("Select the channel for announcement drafts...", suggested_name="event-drafts")
    await ask_view(
        f"**Step 5 of {TOTAL_STEPS} — Draft channel**\n"
        "Which channel should the bot post the draft announcement for leadership to review?",
        draft_ch_view,
    )
    if not draft_ch_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
        return
    draft_channel_id = draft_ch_view.selected_channel.id

    # ── Step 7: Announcement channel ──────────────────────────────────────────
    ann_ch_view = ChannelSelectStep("Select the public announcement channel...", suggested_name="announcements")
    await ask_view(
        f"**Step 6 of {TOTAL_STEPS} — Announcement channel**\n"
        "Which channel should the final approved announcement be posted to?",
        ann_ch_view,
    )
    if not ann_ch_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
        return
    announcement_channel_id = ann_ch_view.selected_channel.id

    # ── Step 8: Draft time ────────────────────────────────────────────────────
    draft_time_raw = await ask_text(
        f"**Step 7 of {TOTAL_STEPS} — Draft posting time**\n"
        "What time should the bot post the draft for leadership to review? (24h format in your timezone)\n"
        "*(e.g. `12:00` for noon)*"
    )
    if not draft_time_raw:
        return
    draft_time = _parse_12h_time(draft_time_raw)
    if not draft_time:
        await channel.send("⚠️ Could not read that time. Try something like `12:00pm` or `9:00am`. Run `/setup_events` to try again.")
        return

    # ── Step 9: 5-minute warning ──────────────────────────────────────────────
    warn_view = YesNoView()
    await ask_view(
        f"**Step 8 of {TOTAL_STEPS} — 5-minute warning**\n"
        "Should the bot automatically post a 5-minute warning to the announcement channel before the event?",
        warn_view,
    )
    if warn_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
        return
    five_min_warning = 1 if warn_view.selected else 0

    # ── Step 10: Announcement blurb ───────────────────────────────────────────
    blurb_raw = await ask_text(
        f"**Step 9 of {TOTAL_STEPS} — Announcement blurb**\n"
        "Write the message that gets posted when this event fires. Use these placeholders:\n"
        "• `{time}` — event time in your timezone (e.g. `10:15pm ET`)\n"
        "• `{server_time}` — event time in Server Time (UTC)\n\n"
        "**Example:**\n"
        "`Plague Marauder (AE) at {time} ({server_time} Server Time). Make sure to have offline participation checked!`",
        max_chars=1000,
    )
    if not blurb_raw:
        return

    # ── Save ──────────────────────────────────────────────────────────────────
    from config import save_guild_event
    event = {
        "short_key":               short_key,
        "name":                    name,
        "timezone":                timezone,
        "default_time":            default_time,
        "announcement_blurb":      blurb_raw,
        "schedule_type":           schedule_type,
        "anchor_date":             anchor_date,
        "interval_days":           interval_days,
        "draft_channel_id":        draft_channel_id,
        "announcement_channel_id": announcement_channel_id,
        "draft_time":              draft_time,
        "five_min_warning":        five_min_warning,
        "active":                  1,
    }
    save_guild_event(guild_id, event)

    # ── Summary ───────────────────────────────────────────────────────────────
    tz_label   = TIMEZONE_LABELS.get(timezone, timezone)
    sched_desc = (
        f"Repeating every {interval_days} days from {anchor_date}"
        if schedule_type == "repeating"
        else "Manual"
    )
    embed = discord.Embed(
        title=f"✅ Event configured: {name}",
        color=discord.Color.green(),
    )
    embed.add_field(name="Short Key",     value=f"`{short_key}`",        inline=True)
    embed.add_field(name="Timezone",      value=tz_label,                inline=True)
    embed.add_field(name="Default Time",  value=default_time,            inline=True)
    embed.add_field(name="Schedule",      value=sched_desc,              inline=False)
    embed.add_field(name="Draft Channel", value=f"<#{draft_channel_id}>",        inline=True)
    embed.add_field(name="Announcements", value=f"<#{announcement_channel_id}>", inline=True)
    embed.add_field(name="Draft Time",    value=draft_time,              inline=True)
    embed.add_field(name="5-min Warning", value="Yes" if five_min_warning else "No", inline=True)
    embed.add_field(name="Blurb",         value=blurb_raw[:200],        inline=False)
    embed.set_footer(text="Run /setup_events again to add another event or update this one.")
    await channel.send(embed=embed)
    print(f"[SETUP] Event '{short_key}' saved for guild {guild_id}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

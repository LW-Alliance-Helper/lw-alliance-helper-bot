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
    await channel.send("**Step 1 of 10 — Member Role**\nSelect the role that all alliance members have:")
    v = RoleSelectStep("Select member role...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.member_role_name = v.selected_role.name
    cfg.member_role_id   = v.selected_role.id

    # ── Step 2: Leadership role ────────────────────────────────────────────────
    await channel.send("**Step 2 of 10 — Leadership Role**\nSelect the elevated role for alliance leadership:")
    v = RoleSelectStep("Select leadership role...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.leadership_role_name = v.selected_role.name

    # ── Step 3: Leadership channel ─────────────────────────────────────────────
    await channel.send(
        "**Step 3 of 10 — Leadership Channel**\n"
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
        "**Step 4 of 10 — Announcement Channel**\n"
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
        "**Step 5 of 10 — Survey Channel**\n"
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
        "**Step 6 of 10 — Survey Notification Channel**\n"
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
        "**Step 7 of 10 — Storm Log Channel**\n"
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

    # ── Step 8: Timezone ───────────────────────────────────────────────────────
    tz_view = TimezoneSelectView()
    await channel.send(
        "**Step 8 of 10 — Timezone**\n"
        "Select your alliance's timezone. This is used for displaying event times, "
        "Desert Storm/Canyon Storm times, and train reminders throughout the bot:"
    )
    await channel.send("\u200b", view=tz_view)
    await tz_view.wait()
    if not tz_view.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.timezone = tz_view.selected

    # ── Step 9: Google Sheet ID ────────────────────────────────────────────────
    await channel.send(
        "**Step 9 of 10 — Google Sheet ID**\n"
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

    # ── Step 10: Share sheet with service account ──────────────────────────────
    SERVICE_ACCOUNT_EMAIL = "sheet-connector@lw-alliance-helper.iam.gserviceaccount.com"
    sharing_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#sharing"

    share_embed = discord.Embed(
        title="**Step 10 of 10 — Share Your Google Sheet**",
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
    tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
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
    embed.add_field(name="Timezone",           value=tz_label,                             inline=True)
    embed.add_field(name="Sheet ID",           value=f"`{sheet_id[:20]}...`",              inline=False)

    confirm_view = ConfirmView()
    await channel.send(embed=embed, view=confirm_view)
    await confirm_view.wait()

    if not confirm_view.confirmed:
        await channel.send("❌ Setup cancelled. Run `/setup` to start again.")
        return

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


# ── Birthday setup wizard ──────────────────────────────────────────────────────

async def run_birthday_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring birthday tracking."""
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    TOTAL    = 5

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        try:
            reply = await bot.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    from config import get_birthday_config
    current = get_birthday_config(guild_id)

    await channel.send(
        "⚙️ **Birthday Tracking Setup**\n"
        "Configure where the bot reads birthday data from your Google Sheet.\n"
        "*(Tip: column A = 1, column B = 2, etc.)*"
    )

    # ── Step 1: Enabled? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        f"**Step 1 of {TOTAL} — Enable birthday tracking?**\n"
        "Should the bot automatically add member birthdays to the train schedule?",
        view=enabled_view,
    )
    await enabled_view.wait()
    if enabled_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
        return
    enabled = 1 if enabled_view.selected else 0

    if not enabled:
        from config import save_birthday_config
        save_birthday_config(
            guild_id,
            current.get("tab_name", ""),
            current.get("name_col", 4),
            current.get("birthday_col", 8),
            current.get("data_start_row", 10),
            current.get("lookahead_days", 14),
            enabled=0,
        )
        await channel.send("✅ Birthday tracking disabled.")
        return

    # ── Step 2: Sheet tab ──────────────────────────────────────────────────────
    tab_raw = await ask_text(
        f"**Step 2 of {TOTAL} — Sheet Tab**\n"
        f"Which tab in your Google Sheet contains your member roster with birthdays?\n"
        f"*(Current: `{current.get('tab_name') or 'Not set'}`)*"
    )
    if tab_raw is None:
        return
    tab_name = tab_raw.strip() or current.get("tab_name", "")

    # ── Step 3: Name column ────────────────────────────────────────────────────
    cur_name_col = current.get("name_col", 4) + 1
    name_col_raw = await ask_text(
        f"**Step 3 of {TOTAL} — Name Column**\n"
        f"Which column number contains the member's name? (A=1, B=2, C=3...)\n"
        f"*(Current: column {cur_name_col} = {chr(64 + cur_name_col)})*"
    )
    if name_col_raw is None:
        return
    try:
        name_col = int(name_col_raw.strip()) - 1
        if name_col < 0:
            raise ValueError
    except ValueError:
        await channel.send("⚠️ Please enter a column number like `5`. Run `/setup_birthdays` to try again.")
        return

    # ── Step 4: Birthday column ────────────────────────────────────────────────
    cur_bday_col = current.get("birthday_col", 8) + 1
    bday_col_raw = await ask_text(
        f"**Step 4 of {TOTAL} — Birthday Column**\n"
        f"Which column number contains the member's birthday?\n"
        f"*(Current: column {cur_bday_col} = {chr(64 + cur_bday_col)})*"
    )
    if bday_col_raw is None:
        return
    try:
        birthday_col = int(bday_col_raw.strip()) - 1
        if birthday_col < 0:
            raise ValueError
    except ValueError:
        await channel.send("⚠️ Please enter a column number like `9`. Run `/setup_birthdays` to try again.")
        return

    # ── Step 5: Lookahead days ─────────────────────────────────────────────────
    lookahead_raw = await ask_text(
        f"**Step 5 of {TOTAL} — Lookahead Days**\n"
        f"How many days in advance should birthdays be added to the train schedule?\n"
        f"*(Current: {current.get('lookahead_days', 14)} days — we recommend 14)*"
    )
    if lookahead_raw is None:
        return
    try:
        lookahead_days = int(lookahead_raw.strip())
        if lookahead_days < 1:
            raise ValueError
    except ValueError:
        await channel.send("⚠️ Please enter a number like `14`. Run `/setup_birthdays` to try again.")
        return

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_birthday_config
    save_birthday_config(guild_id, tab_name, name_col, birthday_col, 10, lookahead_days, enabled)

    embed = discord.Embed(title="✅ Birthday Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",       value=tab_name,                                       inline=True)
    embed.add_field(name="Name Column",     value=f"Column {name_col + 1} ({chr(64 + name_col + 1)})",     inline=True)
    embed.add_field(name="Birthday Column", value=f"Column {birthday_col + 1} ({chr(64 + birthday_col + 1)})", inline=True)
    embed.add_field(name="Lookahead",       value=f"{lookahead_days} days",                       inline=True)
    embed.set_footer(text="Run /setup_birthdays again to update these settings.")
    await channel.send(embed=embed)
    print(f"[SETUP] Birthday config saved for guild {guild_id}")


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
        name="setup_train",
        description="Configure the train schedule — tab, themes, tones, and prompt template"
    )
    async def setup_train(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup_train`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚙️ Starting train setup — check the channel for prompts!", ephemeral=True
        )
        await run_train_setup(interaction, self.bot)


async def run_train_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the train schedule."""
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    TOTAL    = 5

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 2000):
        await channel.send(prompt)
        try:
            reply = await bot.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    await channel.send(
        "⚙️ **Train Schedule Setup**\n"
        "*Configure how the train schedule works for your alliance.*"
    )

    # ── Step 1: Sheet tab name ─────────────────────────────────────────────────
    from config import get_train_config
    current = get_train_config(guild_id)

    tab_raw = await ask_text(
        f"**Step 1 of {TOTAL} — Schedule Sheet Tab**\n"
        f"What is the name of the tab in your Google Sheet that stores the train schedule?\n"
        f"*(Current: `{current['tab_name']}` — type it again to keep it, or enter a new name)*"
    )
    if tab_raw is None:
        return
    tab_name = tab_raw.strip() or current["tab_name"]

    # ── Step 2: Themes ─────────────────────────────────────────────────────────
    current_themes = ", ".join(current["themes"])
    themes_raw = await ask_text(
        f"**Step 2 of {TOTAL} — Themes**\n"
        f"Enter your available themes as a comma-separated list.\n"
        f"These appear as options when selecting a theme for a member's train day.\n\n"
        f"**Example themes:**\n"
        f"`Welcome to the Alliance, Birthday, Milestone, War / Performance, General Celebration, Contest / Raffle, Custom`\n\n"
        f"*(Current: `{current_themes}`)*"
    )
    if themes_raw is None:
        return
    themes = [t.strip() for t in themes_raw.split(",") if t.strip()] or current["themes"]

    # ── Step 3: Tones ──────────────────────────────────────────────────────────
    current_tones = ", ".join(current["tones"])
    tones_raw = await ask_text(
        f"**Step 3 of {TOTAL} — Tones**\n"
        f"Enter your available tones as a comma-separated list.\n"
        f"These let leadership adjust the style of the generated blurb.\n\n"
        f"**Example tones:**\n"
        f"`Default (match the theme), More casual, More intense, Funny, Serious, Cinematic / Dramatic`\n\n"
        f"*(Current: `{current_tones}`)*"
    )
    if tones_raw is None:
        return
    tones = [t.strip() for t in tones_raw.split(",") if t.strip()] or current["tones"]

    # ── Step 4: Default tone ───────────────────────────────────────────────────
    tones_display = "\n".join(f"{i+1}. {t}" for i, t in enumerate(tones))
    default_tone_raw = await ask_text(
        f"**Step 4 of {TOTAL} — Default Tone**\n"
        f"Which tone should be selected by default?\n"
        f"Type the exact tone name from your list:\n\n{tones_display}\n\n"
        f"*(Current: `{current['default_tone']}`)*"
    )
    if default_tone_raw is None:
        return
    default_tone = default_tone_raw.strip() if default_tone_raw.strip() in tones else tones[0]

    # ── Step 5: Prompt template ────────────────────────────────────────────────
    await channel.send(
        f"**Step 5 of {TOTAL} — Prompt Template**\n"
        "Paste your full ChatGPT prompt template. Use these placeholders:\n"
        "• `{name}` — the member's name\n"
        "• `{theme}` — the selected theme\n"
        "• `{tone}` — the selected tone\n"
        "• `{notes}` — any notes stored for this member\n\n"
        "**Example template:**\n"
        "```\n"
        "You are writing a short motivational alliance announcement blurb for a mobile strategy game.\n"
        "Keep it under 3 sentences. It should feel energetic and personal.\n\n"
        "Member name: {name}\n"
        "Theme: {theme}\n"
        "Tone: {tone}\n"
        "Notes: {notes}\n\n"
        "Write the blurb now:\n"
        "```\n"
        "*(Paste your template below — you have 5 minutes)*"
    )
    try:
        reply = await bot.wait_for("message", check=check, timeout=300)
    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
        return
    prompt_template = reply.content.strip()

    if not prompt_template:
        await channel.send("⚠️ No template entered. Run `/setup_train` to try again.")
        return

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_train_config
    save_train_config(guild_id, tab_name, themes, tones, prompt_template, default_tone)

    embed = discord.Embed(
        title="✅ Train Schedule Configured",
        color=discord.Color.green(),
    )
    embed.add_field(name="Sheet Tab",     value=tab_name,                   inline=True)
    embed.add_field(name="Default Tone",  value=default_tone,               inline=True)
    embed.add_field(name="Themes",        value=", ".join(themes),          inline=False)
    embed.add_field(name="Tones",         value=", ".join(tones),           inline=False)
    embed.add_field(name="Prompt Template", value=f"```{prompt_template[:200]}...```" if len(prompt_template) > 200 else f"```{prompt_template}```", inline=False)
    embed.set_footer(text="Run /setup_train again to update any of these settings.")
    await channel.send(embed=embed)
    print(f"[SETUP] Train config saved for guild {guild_id}")


    @app_commands.command(
        name="setup_survey",
        description="Configure the squad powers survey — questions, tabs, and intro message"
    )
    async def setup_survey(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup_survey`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚙️ Starting survey setup — check the channel for prompts!", ephemeral=True
        )
        await run_survey_setup(interaction, self.bot)


async def run_survey_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the squad powers survey."""
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 500):
        await channel.send(prompt)
        try:
            reply = await bot.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    from config import get_survey_config, save_survey_config, OGV_SURVEY_QUESTIONS
    current   = get_survey_config(guild_id)
    questions = list(current.get("questions") or OGV_SURVEY_QUESTIONS)

    await channel.send(
        "⚙️ **Survey Setup**\n"
        "Configure the questions your members answer when they submit their stats.\n\n"
        "**Example questions from Last War:**\n"
        "```\n"
        "1st Squad Power — text — e.g. 43.27 — max 5 chars\n"
        "1st Squad Type  — dropdown — Missile, Air, Tank\n"
        "2nd Squad Power — text — e.g. 43.27 — max 5 chars\n"
        "3rd Squad Power — text — e.g. 43.27 — max 5 chars\n"
        "Drone Level     — text — e.g. 243\n"
        "Gorilla Level   — text — e.g. 70\n"
        "Total Hero Power (THP) — text — e.g. 301 — max 3 chars\n"
        "Total Kills     — text — e.g. 55.40 — max 5 chars\n"
        "Profession      — dropdown — War Leader, Engineer\n"
        "```"
    )

    # ── Step 1: Squad Powers tab ───────────────────────────────────────────────
    tab_sp = await ask_text(
        f"**Step 1 — Squad Powers Tab**\n"
        f"Which tab stores member stats (updated on each submission)?\n"
        f"*(Current: `{current.get('tab_squad_powers', 'Squad Powers')}`)*"
    )
    if tab_sp is None:
        return
    tab_squad_powers = tab_sp.strip() or current.get("tab_squad_powers", "Squad Powers")

    # ── Step 2: Survey History tab ─────────────────────────────────────────────
    tab_hist = await ask_text(
        f"**Step 2 — Survey History Tab**\n"
        f"Which tab stores the full history of all submissions?\n"
        f"*(Current: `{current.get('tab_history', 'Survey History')}`)*"
    )
    if tab_hist is None:
        return
    tab_history = tab_hist.strip() or current.get("tab_history", "Survey History")

    # ── Step 3: Intro message ──────────────────────────────────────────────────
    intro_raw = await ask_text(
        "**Step 3 — Survey Intro Message**\n"
        "What message should appear on the survey button post in your survey channel?\n\n"
        "**Example:**\n"
        "*Please fill out this survey each week to help us track squad powers, "
        "balance teams, and prepare for season events!*"
    )
    if intro_raw is None:
        return
    intro_message = intro_raw.strip()

    # ── Step 4: Build questions ────────────────────────────────────────────────
    await channel.send(
        "**Step 4 — Survey Questions**\n"
        "Now let's define your questions. You'll add them one at a time.\n"
        f"Currently you have **{len(questions)} question(s)** configured.\n\n"
        "Type `keep` to keep the current questions, or `reset` to start fresh."
    )
    try:
        reply = await bot.wait_for("message", check=check, timeout=120)
    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return

    if reply.content.strip().lower() == "keep":
        pass  # keep existing questions
    else:
        questions = []
        await channel.send(
            "Let's add your questions. For each one I'll ask:\n"
            "1. The question label (e.g. `1st Squad Power`)\n"
            "2. The type: `text` or `dropdown`\n"
            "3. For text: an example hint and max characters\n"
            "4. For dropdown: the options (comma-separated)\n\n"
            "Type `done` when you've finished adding questions."
        )

        while True:
            q_label_raw = await ask_text(
                f"**Question {len(questions) + 1} — Label** (or type `done` to finish):"
            )
            if q_label_raw is None:
                return
            if q_label_raw.strip().lower() == "done":
                break

            q_label = q_label_raw.strip()
            q_key   = q_label.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")

            q_type_raw = await ask_text(
                f"**Question: {q_label}**\nType: `text` (user types a value) or `dropdown` (user picks from a list)?"
            )
            if q_type_raw is None:
                return
            q_type = "dropdown" if "drop" in q_type_raw.lower() else "text"

            if q_type == "text":
                hint_raw = await ask_text(
                    f"**{q_label} — Example hint** (shown to user, e.g. `e.g. 43.27`) or type `none`:"
                )
                if hint_raw is None:
                    return
                placeholder = "" if hint_raw.strip().lower() == "none" else hint_raw.strip()

                max_raw = await ask_text(
                    f"**{q_label} — Max characters** (e.g. `5`) or `0` for no limit:"
                )
                if max_raw is None:
                    return
                try:
                    max_chars = int(max_raw.strip())
                except ValueError:
                    max_chars = 0

                questions.append({
                    "key": q_key, "label": q_label, "type": "text",
                    "options": [], "placeholder": placeholder, "max_chars": max_chars,
                })

            else:
                opts_raw = await ask_text(
                    f"**{q_label} — Options** (comma-separated, e.g. `Option A, Option B, Option C`):"
                )
                if opts_raw is None:
                    return
                options = [o.strip() for o in opts_raw.split(",") if o.strip()]

                questions.append({
                    "key": q_key, "label": q_label, "type": "dropdown",
                    "options": options, "placeholder": f"Select {q_label}...", "max_chars": 0,
                })

            await channel.send(f"✅ Added: **{q_label}** ({q_type}) — {len(questions)} question(s) so far.")

    if not questions:
        await channel.send("⚠️ No questions defined. Run `/setup_survey` to try again.")
        return

    # ── Save ───────────────────────────────────────────────────────────────────
    save_survey_config(guild_id, tab_squad_powers, tab_history, questions, intro_message)

    q_summary = "\n".join(
        f"• **{q['label']}** — {q['type']}" +
        (f" ({', '.join(q['options'])})" if q['type'] == 'dropdown' else "")
        for q in questions
    )
    embed = discord.Embed(title="✅ Survey Configured", color=discord.Color.green())
    embed.add_field(name="Squad Powers Tab", value=tab_squad_powers, inline=True)
    embed.add_field(name="History Tab",      value=tab_history,      inline=True)
    embed.add_field(name="Questions",        value=q_summary[:1024], inline=False)
    embed.set_footer(text="Run /setup_survey again to update. Run /postsurvey to post the survey button.")
    await channel.send(embed=embed)
    print(f"[SETUP] Survey config saved for guild {guild_id} — {len(questions)} questions")
    async def setup_birthdays(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup_birthdays`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚙️ Starting birthday setup — check the channel for prompts!", ephemeral=True
        )
        await run_birthday_setup(interaction, self.bot)

    @app_commands.command(
        name="setup_desertstorm",
        description="Configure Desert Storm mail template and time options"
    )
    async def setup_desertstorm(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup_desertstorm`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚙️ Starting Desert Storm setup — check the channel for prompts!", ephemeral=True
        )
        await run_storm_setup(interaction, self.bot, "DS")

    @app_commands.command(
        name="setup_canyonstorm",
        description="Configure Canyon Storm mail template and time options"
    )
    async def setup_canyonstorm(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup_canyonstorm`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚙️ Starting Canyon Storm setup — check the channel for prompts!", ephemeral=True
        )
        await run_storm_setup(interaction, self.bot, "CS")


async def run_storm_setup(interaction: discord.Interaction, bot, event_type: str):
    """Shared setup wizard for Desert Storm and Canyon Storm."""
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    label    = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    cmd_name = "setup_desertstorm" if event_type == "DS" else "setup_canyonstorm"

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 2000):
        await channel.send(prompt)
        try:
            reply = await bot.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    from config import get_storm_config, get_config, GENERIC_DS_TEMPLATE, GENERIC_CS_TEMPLATE, get_storm_time_labels
    current   = get_storm_config(guild_id, event_type)
    guild_cfg = get_config(guild_id)
    timezone  = guild_cfg.timezone if guild_cfg and guild_cfg.timezone else "America/New_York"
    tz_label  = TIMEZONE_LABELS.get(timezone, timezone)

    # Pre-compute time labels from Server Time constants in guild's timezone
    time_labels = get_storm_time_labels(event_type, guild_id)

    await channel.send(
        f"⚙️ **{label} Setup**\n"
        f"*Times will be displayed in your configured timezone: **{tz_label}***"
    )

    # ── Step 1: Sheet tab ──────────────────────────────────────────────────────
    tab_raw = await ask_text(
        f"**Step 1 of 4 — Sheet Tab**\n"
        f"Which tab in your Google Sheet stores the {label} zone assignments?\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*\n"
        f"*(Current: `{current.get('tab_name', 'DS Assignments')}`)*"
    )
    if tab_raw is None:
        return
    tab_name = tab_raw.strip() or current.get("tab_name", "DS Assignments")

    # ── Step 2: Which teams? ───────────────────────────────────────────────────
    class TeamChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.selected = None

        @discord.ui.button(label="Team A & Team B", style=discord.ButtonStyle.primary)
        async def both(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.selected = "both"
            for item in self.children: item.disabled = True
            await interaction.response.edit_message(content="✅ Teams: **Team A & Team B**", view=self)
            self.stop()

        @discord.ui.button(label="Team A only", style=discord.ButtonStyle.secondary)
        async def a_only(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.selected = "A"
            for item in self.children: item.disabled = True
            await interaction.response.edit_message(content="✅ Teams: **Team A only**", view=self)
            self.stop()

        @discord.ui.button(label="Team B only", style=discord.ButtonStyle.secondary)
        async def b_only(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.selected = "B"
            for item in self.children: item.disabled = True
            await interaction.response.edit_message(content="✅ Teams: **Team B only**", view=self)
            self.stop()

    team_view = TeamChoiceView()
    await channel.send(f"**Step 2 of 4 — Which teams do you run for {label}?**", view=team_view)
    await team_view.wait()
    if not team_view.selected:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return
    teams = team_view.selected  # "both", "A", or "B"

    # ── Step 3: Mail template(s) ───────────────────────────────────────────────
    placeholder_info = (
        "• `{alliance_name}` — your alliance name\n"
        "• `{zones}` — zone assignments block\n"
        "• `{subs}` — sub pairs\n"
        "• `{time}` — event time (auto-filled when drafting)"
    ) if event_type == "DS" else (
        "• `{alliance_name}` — your alliance name\n"
        "• `{zones}` — zone assignments block\n"
        "• `{subs_list}` — substitute members\n"
        "• `{time}` — event time (auto-filled when drafting)"
    )
    example = GENERIC_DS_TEMPLATE if event_type == "DS" else GENERIC_CS_TEMPLATE

    time_display = " | ".join(f"{lbl}" for lbl, _ in time_labels)

    # Show what the time buttons will look like
    await channel.send(
        f"ℹ️ When drafting, your time buttons will show:\n"
        + "\n".join(f"  • **{lbl}**" for lbl, _ in time_labels)
    )

    if teams == "both":
        # Ask if one template for both or separate
        class SharedTemplateView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected = None

            @discord.ui.button(label="One template for both teams", style=discord.ButtonStyle.primary)
            async def shared(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.selected = "shared"
                for item in self.children: item.disabled = True
                await interaction.response.edit_message(content="✅ **One shared template** for Team A & B", view=self)
                self.stop()

            @discord.ui.button(label="Separate templates per team", style=discord.ButtonStyle.secondary)
            async def separate(self, interaction: discord.Interaction, button: discord.ui.Button):
                self.selected = "separate"
                for item in self.children: item.disabled = True
                await interaction.response.edit_message(content="✅ **Separate templates** for Team A & Team B", view=self)
                self.stop()

        shared_view = SharedTemplateView()
        await channel.send(
            "**Step 3 of 4 — Mail Template**\n"
            "Do you want one template that applies to both teams, or separate templates per team?",
            view=shared_view,
        )
        await shared_view.wait()
        if not shared_view.selected:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return

        await channel.send(
            f"Paste your mail template below. Use these placeholders:\n{placeholder_info}\n\n"
            f"**Example:**\n```\n{example}\n```\n*(5 minutes to respond)*"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return
        template_a = reply.content.strip()

        if shared_view.selected == "separate":
            await channel.send(
                f"Now paste the **Team B** template:\n*(5 minutes to respond)*"
            )
            try:
                reply_b = await bot.wait_for("message", check=check, timeout=300)
            except asyncio.TimeoutError:
                await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
                return
            template_b = reply_b.content.strip()
        else:
            template_b = template_a

    else:
        team_label = "Team A" if teams == "A" else "Team B"
        await channel.send(
            f"**Step 3 of 4 — Mail Template**\n"
            f"This template will apply to **{team_label}**.\n"
            f"Paste your template below. Use these placeholders:\n{placeholder_info}\n\n"
            f"**Example:**\n```\n{example}\n```\n*(5 minutes to respond)*"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=300)
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return
        template_a = reply.content.strip() if teams == "A" else ""
        template_b = reply.content.strip() if teams == "B" else ""

    if not template_a and not template_b:
        await channel.send(f"⚠️ No template entered. Run `/{cmd_name}` to try again.")
        return

    # ── Step 4: Default zone names ─────────────────────────────────────────────
    # (no step needed — zones use "Member Name" placeholder by default)

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_storm_config
    # Save team A template
    if template_a:
        save_storm_config(
            guild_id, f"{event_type}_A", tab_name, template_a,
            "", "", "", "", "", "", timezone,
        )
    # Save team B template
    if template_b:
        save_storm_config(
            guild_id, f"{event_type}_B", tab_name, template_b,
            "", "", "", "", "", "", timezone,
        )
    # Save base event type record
    save_storm_config(
        guild_id, event_type, tab_name, template_a or template_b,
        "", "", "", "", "", "", timezone,
    )

    embed = discord.Embed(title=f"✅ {label} Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",    value=tab_name, inline=True)
    embed.add_field(name="Timezone",     value=tz_label, inline=True)
    embed.add_field(name="Teams",        value={"both": "A & B", "A": "A only", "B": "B only"}[teams], inline=True)
    embed.add_field(name="Time Options", value="\n".join(f"• {lbl}" for lbl, _ in time_labels), inline=False)
    embed.add_field(name="Template A Preview", value=f"```{(template_a or '—')[:150]}{'...' if len(template_a or '') > 150 else ''}```", inline=False)
    if template_b and template_b != template_a:
        embed.add_field(name="Template B Preview", value=f"```{template_b[:150]}{'...' if len(template_b) > 150 else ''}```", inline=False)
    embed.set_footer(text=f"Run /{cmd_name} again to update.")
    await channel.send(embed=embed)
    print(f"[SETUP] {label} config saved for guild {guild_id}")


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

    # Pull timezone from guild config (set during /setup)
    from config import get_config
    guild_cfg = get_config(guild_id)
    timezone  = guild_cfg.timezone if guild_cfg and guild_cfg.timezone else "America/New_York"

    # ── Step 2: Default event time ────────────────────────────────────────────
    time_raw = await ask_text(
        f"**Step 2 of {TOTAL_STEPS} — Default event time**\n"
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
        f"**Step 3 of {TOTAL_STEPS} — Schedule**\nDoes this event repeat on a fixed cycle, or do you add it manually each time?",
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
            f"**Step 3a of {TOTAL_STEPS} — Anchor date**\n"
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
            f"**Step 3b of {TOTAL_STEPS} — Cycle interval**\n"
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
        f"**Step 4 of {TOTAL_STEPS} — Draft channel**\n"
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
        f"**Step 5 of {TOTAL_STEPS} — Announcement channel**\n"
        "Which channel should the final approved announcement be posted to?",
        ann_ch_view,
    )
    if not ann_ch_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
        return
    announcement_channel_id = ann_ch_view.selected_channel.id

    # ── Step 8: Draft time ────────────────────────────────────────────────────
    draft_time_raw = await ask_text(
        f"**Step 6 of {TOTAL_STEPS} — Draft posting time**\n"
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
        f"**Step 7 of {TOTAL_STEPS} — 5-minute warning**\n"
        "Should the bot automatically post a 5-minute warning to the announcement channel before the event?",
        warn_view,
    )
    if warn_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
        return
    five_min_warning = 1 if warn_view.selected else 0

    # ── Step 10: Announcement blurb ───────────────────────────────────────────
    blurb_raw = await ask_text(
        f"**Step 8 of {TOTAL_STEPS} — Announcement blurb**\n"
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
        "timezone":                timezone,  # from guild config
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

    class AddAnotherView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_another = False

        @discord.ui.button(label="➕ Add another event", style=discord.ButtonStyle.primary)
        async def add_another_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.add_another = True
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=self)
            self.stop()

        @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.secondary)
        async def done_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=self)
            self.stop()

    another_view = AddAnotherView()
    await channel.send(embed=embed, view=another_view)
    print(f"[SETUP] Event '{short_key}' saved for guild {guild_id}")

    await another_view.wait()
    if another_view.add_another:
        await run_event_setup(interaction, bot)


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

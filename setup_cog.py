"""
setup_cog.py — /setup_* wizards for new guilds

Walks a server admin through configuring the bot using Discord's native
role and channel select menus. All values are saved to the config database.

Holds /setup, /setup_reset, /view_configuration, and the per-feature
/setup_train, /setup_growth, /setup_birthdays, /setup_desertstorm,
/setup_canyonstorm, /setup_events, /setup_survey commands.
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


# ── Define Various Setup Commands ────────────────────────────────────────────────────────────────────────

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

    @app_commands.command(name="view_configuration", description="View all configured settings across every setup wizard")
    async def view_configuration(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can view configuration.", ephemeral=True
            )
            return

        cfg = get_config(interaction.guild_id)
        if not cfg or not cfg.setup_complete:
            await interaction.response.send_message(
                "⚙️ This server hasn't been set up yet. Run `/setup` to get started.",
                ephemeral=True,
            )
            return

        await _send_view_configuration(interaction, cfg)

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

    @app_commands.command(name="setup_train", description="Configure the train schedule — tab, themes, tones, and prompt template")
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

    @app_commands.command(name="setup_growth", description="Configure growth tracking — source tab, metrics, and snapshot frequency")
    async def setup_growth(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup_growth`.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "⚙️ Starting growth tracking setup — check the channel for prompts!", ephemeral=True
        )
        await run_growth_setup(interaction, self.bot)

    @app_commands.command(name="setup_birthdays", description="Configure birthday tracking — sheet tab, columns, and lookahead days")
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

    @app_commands.command(name="setup_desertstorm", description="Configure Desert Storm mail template and time options")
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

    @app_commands.command(name="setup_canyonstorm", description="Configure Canyon Storm mail template and time options")
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

    @app_commands.command(name="setup_events", description="Add or edit an event type for announcements (Marauder, Siege, etc.)")
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

    @app_commands.command(name="setup_survey", description="Configure survey — channels, sheet tabs, intro message, and questions")
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

# ── /Define Various Setup Commands ───────────────────────────────────────────────────────

# Common timezones for the selector
# ── Timezone configuration ─────────────────────────────────────────────────────
# Format: (tz_database_name, display_label)
# Labels show (UTC offset, timezone name, and example cities
# Note: offsets shown are standard time — DST-observing zones shift +1 in summer

TIMEZONE_OPTIONS = [
    ("Pacific/Honolulu",                  "(UTC-10) Hawaii (Honolulu)"),
    ("America/Anchorage",                 "(UTC-9) Alaska (Anchorage)"),
    ("America/Los_Angeles",               "(UTC-8) Pacific (Los Angeles, Seattle, Vancouver)"),
    ("America/Denver",                    "(UTC-7) Mountain (Denver, Phoenix, Calgary)"),
    ("America/Chicago",                   "(UTC-6) Central (Chicago, Dallas, Mexico City)"),
    ("America/New_York",                  "(UTC-5) Eastern (New York, Toronto, Miami)"),
    ("America/Sao_Paulo",                 "(UTC-3) Brazil (São Paulo, Rio de Janeiro)"),
    ("America/Argentina/Buenos_Aires",    "(UTC-3) Argentina (Buenos Aires)"),
    ("Atlantic/Azores",                   "(UTC-1) Azores"),
    ("Europe/London",                     "(UTC+0) GMT/BST (London, Dublin, Lisbon)"),
    ("Europe/Paris",                      "(UTC+1) Central European (Paris, Berlin, Rome)"),
    ("Europe/Helsinki",                   "(UTC+2) Eastern European (Helsinki, Athens, Cairo)"),
    ("Europe/Moscow",                     "(UTC+3) Moscow (Moscow, Istanbul, Riyadh)"),
    ("Asia/Dubai",                        "(UTC+4) Gulf (Dubai, Abu Dhabi)"),
    ("Asia/Karachi",                      "(UTC+5) Pakistan (Karachi, Islamabad)"),
    ("Asia/Kolkata",                      "(UTC+5:30) India (Mumbai, Delhi, Bangalore)"),
    ("Asia/Dhaka",                        "(UTC+6) Bangladesh (Dhaka)"),
    ("Asia/Bangkok",                      "(UTC+7) Indochina (Bangkok, Jakarta, Hanoi)"),
    ("Asia/Shanghai",                     "(UTC+8) China/Singapore (Shanghai, Beijing, Singapore)"),
    ("Asia/Tokyo",                        "(UTC+9) Japan/Korea (Tokyo, Seoul)"),
    ("Australia/Sydney",                  "(UTC+10) Eastern Australia (Sydney, Melbourne)"),
    ("Pacific/Auckland",                  "(UTC+12) New Zealand (Auckland, Wellington)"),
]

# Map from tz_database_name → display label
TIMEZONE_LABELS = {tz: label for tz, label in TIMEZONE_OPTIONS}


class TimezoneSelectView(discord.ui.View):
    """Single dropdown covering all supported timezones, ordered by (UTC offset."""
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected  = None
        self.confirmed = False

        select = discord.ui.Select(
            placeholder="Select your timezone...",
            options=[
                discord.SelectOption(label=label[:100], value=tz)
                for tz, label in TIMEZONE_OPTIONS
            ],
            row=0,
        )

        async def _cb(interaction: discord.Interaction):
            self.selected    = select.values[0]
            self.confirmed   = True
            select.disabled  = True
            label = TIMEZONE_LABELS.get(self.selected, self.selected)
            await interaction.response.edit_message(
                content=f"✅ Timezone: **{label}**", view=self
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


# ── /view_configuration helper ───────────────────────────────────────────────

async def _send_view_configuration(interaction: discord.Interaction, cfg) -> None:
    """Build and send a single embed summarising every wizard's configuration."""
    await interaction.response.defer(ephemeral=True)

    from config import (
        get_train_config, get_birthday_config, get_storm_config,
        get_survey_config, get_growth_config, get_guild_events,
    )
    guild_id = interaction.guild_id
    train    = get_train_config(guild_id)
    birthday = get_birthday_config(guild_id)
    ds       = get_storm_config(guild_id, "DS")
    cs       = get_storm_config(guild_id, "CS")
    survey   = get_survey_config(guild_id)
    growth   = get_growth_config(guild_id)
    events   = get_guild_events(guild_id, active_only=True)

    def _yn(v) -> str:
        return "✅ Configured" if v else "❌ Not configured"

    def _enabled(v) -> str:
        return "✅ Enabled" if v else "❌ Disabled"

    def _channel(v) -> str:
        return f"<#{v}>" if v else "*not set*"

    def _col_letter(idx) -> str:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return "*not set*"
        return chr(65 + idx) if 0 <= idx <= 25 else str(idx)

    embed = discord.Embed(
        title="⚙️ Current Configuration",
        description="All configured settings across the bot's setup wizards.",
        color=discord.Color.blurple(),
    )

    tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
    sheet_id_display = f"`{cfg.spreadsheet_id[:25]}...`" if cfg.spreadsheet_id else "*not set*"
    core_lines = [
        f"**Member Role:** {cfg.member_role_name}",
        f"**Leadership Role:** {cfg.leadership_role_name}",
        f"**Leadership Channel:** {_channel(cfg.leadership_channel_id)}",
        f"**Announcement Channel:** {_channel(cfg.announcement_channel_id)}",
        f"**Timezone:** {tz_label}",
        f"**Spreadsheet ID:** {sheet_id_display}",
        f"**Member Tab:** {cfg.tab_member_default}",
    ]
    embed.add_field(name="🛠️ Core", value="\n".join(core_lines)[:1024], inline=False)

    ev_lines = [
        f"**Draft Channel:** {_channel(cfg.event_draft_channel_id)}",
        f"**Announcement Channel:** {_channel(cfg.event_announce_channel_id)}",
        f"**Draft Time:** {cfg.event_draft_time}",
        f"**5-Min Warning:** {_enabled(cfg.event_five_min_warning)}",
    ]
    if events:
        ev_lines.append(f"**Events ({len(events)}):**")
        for e in events:
            ev_lines.append(
                f"• {e['name']} (`{e['short_key']}`) — {e['default_time']} {e['timezone']} · "
                f"blurb {_yn(e.get('announcement_blurb'))}"
            )
    else:
        ev_lines.append("**Events:** *none configured*")
    embed.add_field(name="📣 Events", value="\n".join(ev_lines)[:1024], inline=False)

    train_lines = [
        f"**Schedule Tab:** {train.get('tab_name', '*not set*')}",
        f"**Blurbs:** {_enabled(train.get('blurbs_enabled'))}",
    ]
    if train.get("blurbs_enabled"):
        themes = train.get("themes") or []
        tones  = train.get("tones")  or []
        train_lines.append(f"**Themes ({len(themes)}):** " + (", ".join(themes) if themes else "*none*"))
        train_lines.append(f"**Tones ({len(tones)}):** "  + (", ".join(tones)  if tones  else "*none*"))
        train_lines.append(f"**Default Tone:** {train.get('default_tone', '*not set*')}")
        train_lines.append(f"**Prompt Template:** {_yn(train.get('prompt_template'))}")
    train_lines.append(f"**Reminders:** {_enabled(train.get('reminders_enabled'))}")
    if train.get("reminders_enabled"):
        train_lines.append(f"**Reminder Channel:** {_channel(train.get('reminder_channel_id'))}")
        train_lines.append(f"**Reminder Time:** {train.get('reminder_time', '*not set*')}")
    embed.add_field(name="🚂 Train", value="\n".join(train_lines)[:1024], inline=False)

    b_lines = [
        f"**Enabled:** {_enabled(birthday.get('enabled'))}",
        f"**Source Tab:** {birthday.get('tab_name', '*not set*')}",
        f"**Name Column:** {_col_letter(birthday.get('name_col'))}",
        f"**Birthday Column:** {_col_letter(birthday.get('birthday_col'))}",
        f"**Discord ID Column:** "
        + (_col_letter(birthday.get('discord_id_col'))
           if birthday.get('discord_id_col', -1) >= 0 else "*not set*"),
        f"**Data Start Row:** {birthday.get('data_start_row', '*not set*')}",
        f"**Lookahead Days:** {birthday.get('lookahead_days', '*not set*')}",
        f"**Train Integration:** {_enabled(birthday.get('train_integration'))}",
        f"**Reminders:** {_enabled(birthday.get('reminders_enabled'))}",
    ]
    if birthday.get("reminders_enabled"):
        b_lines.append(f"**Reminder Channel:** {_channel(birthday.get('reminder_channel_id'))}")
        b_lines.append(f"**Reminder Time:** {birthday.get('reminder_time', '*not set*')}")
    embed.add_field(name="🎂 Birthdays", value="\n".join(b_lines)[:1024], inline=False)

    ds_lines = [
        f"**Sheet Tab:** {ds.get('tab_name', '*not set*')}",
        f"**Log Channel:** {_channel(cfg.ds_log_channel_id)}",
        f"**Time Option 1:** {ds.get('time_option_1_label') or '*not set*'} "
        f"({ds.get('time_option_1_local') or '?'} local / {ds.get('time_option_1_server') or '?'} server)",
        f"**Time Option 2:** {ds.get('time_option_2_label') or '*not set*'} "
        f"({ds.get('time_option_2_local') or '?'} local / {ds.get('time_option_2_server') or '?'} server)",
        f"**Mail Template:** {_yn(ds.get('mail_template'))}",
    ]
    embed.add_field(name="⚔️ Desert Storm", value="\n".join(ds_lines)[:1024], inline=False)

    cs_lines = [
        f"**Sheet Tab:** {cs.get('tab_name', '*not set*')}",
        f"**Log Channel:** {_channel(cfg.cs_log_channel_id)}",
        f"**Time Option 1:** {cs.get('time_option_1_label') or '*not set*'} "
        f"({cs.get('time_option_1_local') or '?'} local / {cs.get('time_option_1_server') or '?'} server)",
        f"**Time Option 2:** {cs.get('time_option_2_label') or '*not set*'} "
        f"({cs.get('time_option_2_local') or '?'} local / {cs.get('time_option_2_server') or '?'} server)",
        f"**Mail Template:** {_yn(cs.get('mail_template'))}",
    ]
    embed.add_field(name="🏜️ Canyon Storm", value="\n".join(cs_lines)[:1024], inline=False)

    s_lines = [
        f"**Survey Channel:** {_channel(cfg.survey_channel_id)}",
        f"**Notify Channel:** {_channel(cfg.survey_notify_channel_id)}",
        f"**Stats Tab:** {survey.get('tab_squad_powers', '*not set*')}",
        f"**History Tab:** {survey.get('tab_history', '*not set*')}",
        f"**Questions:** {len(survey.get('questions') or [])}",
        f"**Intro Message:** {_yn(survey.get('intro_message'))}",
    ]
    embed.add_field(name="📋 Survey", value="\n".join(s_lines)[:1024], inline=False)

    g_lines = [f"**Enabled:** {_enabled(growth.get('enabled'))}"]
    if growth.get("enabled"):
        metrics = growth.get("metrics") or []
        freq    = growth.get("snapshot_frequency", "monthly")
        sched   = (
            f"Monthly on day {growth.get('snapshot_day', 1)}"
            if freq == "monthly"
            else f"Every {growth.get('snapshot_interval', 30)} days"
        )
        g_lines += [
            f"**Source Tab:** {growth.get('tab_source', '*not set*')}",
            f"**Name Column:** {growth.get('name_col', '*not set*')}",
            f"**Data Start Row:** {growth.get('data_start_row', '*not set*')}",
            f"**Growth Tab:** {growth.get('tab_growth', '*not set*')}",
            f"**Snapshot Schedule:** {sched}",
            f"**Metrics ({len(metrics)}):** "
            + (", ".join(f"{m['label']} (col {m['col']})" for m in metrics) if metrics else "*none*"),
        ]
    embed.add_field(name="📈 Growth", value="\n".join(g_lines)[:1024], inline=False)

    embed.set_footer(text="Run any /setup_* command to update a section. /help shows all commands.")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Run Various Setups ───────────────────────────────────────────────────────

async def run_setup(interaction: discord.Interaction, bot):
    import wizard_registry
    guild_id = interaction.guild_id
    cfg      = get_or_create_config(guild_id)
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    # ── If already configured, show summary and offer edit or cancel ──────────
    if cfg.setup_complete:
        tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
        existing_embed = discord.Embed(
            title="⚙️ Current Core Setup",
            description="Your server is already configured. Would you like to edit these settings?",
            color=discord.Color.blurple(),
        )
        existing_embed.add_field(name="Member Role",        value=cfg.member_role_name,              inline=False)
        existing_embed.add_field(name="Leadership Role",    value=cfg.leadership_role_name,          inline=False)
        existing_embed.add_field(name="Leadership Channel", value=f"<#{cfg.leadership_channel_id}>", inline=False)
        existing_embed.add_field(name="Timezone",           value=tz_label,                          inline=False)
        existing_embed.add_field(name="Sheet ID",           value=f"`{cfg.spreadsheet_id[:20]}...`" if cfg.spreadsheet_id else "Not set", inline=False)

        class EditOrCancelView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.proceed = None

            @discord.ui.button(label="✏️ Edit settings", style=discord.ButtonStyle.primary)
            async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
                self.proceed = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✅ No changes needed", style=discord.ButtonStyle.secondary)
            async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
                self.proceed = False
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        eoc_view = EditOrCancelView()
        await channel.send(embed=existing_embed, view=eoc_view)
        await eoc_view.wait()
        if not eoc_view.proceed:
            await channel.send("✅ No changes made. Your existing setup is still active.")
            return

    await channel.send(
        "⚙️ **Alliance Helper Setup**\n\n"
        "I'll walk you through the core configuration for your server. "
        "This covers your roles, leadership channel, timezone and Google Sheet.\n\n"
        "*You can run `/setup` again at any time to update these settings.*"
    )

    # ── Step 1: Member role ────────────────────────────────────────────────────
    await channel.send("**Step 1 of 6 — Member Role**\nSelect the role that all alliance members have:")
    v = RoleSelectStep("Select member role...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.member_role_name = v.selected_role.name
    cfg.member_role_id   = v.selected_role.id

    # ── Step 2: Leadership role ────────────────────────────────────────────────
    await channel.send("**Step 2 of 6 — Leadership Role**\nSelect the elevated role for alliance leadership:")
    v = RoleSelectStep("Select leadership role...")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.leadership_role_name = v.selected_role.name

    # ── Step 3: Leadership channel ─────────────────────────────────────────────
    await channel.send(
        "**Step 3 of 6 — Leadership Channel**\n"
        "Select the private channel where leadership commands will be used:"
    )
    v = ChannelSelectStep("Select leadership channel...", suggested_name="leadership")
    await channel.send("\u200b", view=v)
    await v.wait()
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.leadership_channel_id  = v.selected_channel.id
    cfg.leadership_category_id = getattr(v.selected_channel, "category_id", 0) or 0

    # ── Step 4: Timezone ───────────────────────────────────────────────────────
    tz_view = TimezoneSelectView()
    await channel.send(
        "**Step 4 of 6 — Timezone**\n"
        "Select your alliance's timezone. This is used for displaying event times, "
        "Desert Storm/Canyon Storm times, and train reminders throughout the bot:"
    )
    await channel.send("\u200b", view=tz_view)
    await tz_view.wait()
    if not tz_view.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.timezone = tz_view.selected

    # ── Step 5: Google Sheet ID ────────────────────────────────────────────────
    await channel.send(
        "**Step 5 of 6 — Google Sheet ID**\n"
        "Enter your Google Sheet ID — the long string from your sheet's URL:\n"
        "`https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID`**`/edit`"
    )
    modal   = TextInputModal("Google Sheet ID", "Sheet ID", placeholder="Paste your Sheet ID here...")
    modal_v = ModalLaunchView(modal)
    await channel.send("\u200b", view=modal_v)
    await modal_v.wait()
    if not modal_v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    sheet_id = modal.value

    # ── Step 6: Share sheet ────────────────────────────────────────────────────
    SERVICE_ACCOUNT_EMAIL = "sheet-connector@lw-alliance-helper.iam.gserviceaccount.com"
    sharing_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#sharing"

    share_embed = discord.Embed(
        title="**Step 6 of 6 — Share Your Google Sheet**",
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
        description="Please confirm these settings before saving:",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Member Role",        value=cfg.member_role_name,              inline=False)
    embed.add_field(name="Leadership Role",    value=cfg.leadership_role_name,          inline=False)
    embed.add_field(name="Leadership Channel", value=f"<#{cfg.leadership_channel_id}>", inline=False)
    embed.add_field(name="Timezone",           value=tz_label,                          inline=False)
    embed.add_field(name="Sheet ID",           value=f"`{sheet_id[:20]}...`",           inline=False)

    confirm_view = ConfirmView()
    await channel.send(embed=embed, view=confirm_view)
    await confirm_view.wait()
    if not confirm_view.confirmed:
        await channel.send("❌ Setup cancelled. Run `/setup` to start again.")
        return

    cfg.setup_complete = True
    cfg.spreadsheet_id = sheet_id
    save_config(cfg)

    await channel.send(
        "✅ **Core setup complete!**\n\n"
        "Now configure the features you want to use. Run each of the commands below for any feature you'd like to enable:\n\n"
        "📣 `/setup_events` — Event announcements (Plague Marauder, Zombie Siege, etc.)\n"
        "🚂 `/setup_train` — Train schedule, blurb generation, and reminders\n"
        "🎂 `/setup_birthdays` — Birthday tracking and announcements\n"
        "⚔️ `/setup_desertstorm` — Desert Storm mail drafts and participation logs\n"
        "🏜️ `/setup_canyonstorm` — Canyon Storm mail drafts and participation logs\n"
        "📋 `/setup_survey` — Squad powers survey\n"
        "📈 `/setup_growth` — Growth tracking (snapshot your members' stats over time)\n\n"
        "You can set up as many or as few of these as you need. Use `/help` at any time to see all available commands."
    )
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Guild {guild_id} core setup complete")

async def run_growth_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring growth tracking."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    async def ask_keep_or_change(
        prompt: str,
        default: str,
        modal_title: str,
        modal_label: str,
    ) -> str | None:
        """Show a `Keep default / Change` view and return the chosen value."""

        class KeepOrChangeDefaultView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.value = None
                self.confirmed = False

            @discord.ui.button(
                label=f"✅ Keep default: {default}"[:80],
                style=discord.ButtonStyle.success,
            )
            async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
                self.value = default
                self.confirmed = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content=f"✅ Using **{default}**", view=self
                )
                self.stop()

            @discord.ui.button(label="✏️ Change", style=discord.ButtonStyle.secondary)
            async def change(self, inter: discord.Interaction, button: discord.ui.Button):
                modal = TextInputModal(modal_title, modal_label, default=default)
                await inter.response.send_modal(modal)
                await modal.wait()
                self.value = (modal.value or default).strip() or default
                self.confirmed = True
                for item in self.children: item.disabled = True
                try:
                    await inter.message.edit(
                        content=f"✅ Using **{self.value}**", view=self
                    )
                except discord.HTTPException:
                    pass
                self.stop()

        view = KeepOrChangeDefaultView()
        await channel.send(prompt, view=view)
        await view.wait()
        if not view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
            return None
        return view.value

    from config import get_growth_config, save_growth_config
    current = get_growth_config(guild_id)

    # Defaults: prefer previously saved value, otherwise hardcoded fallback
    DEFAULT_TAB_SOURCE     = current.get("tab_source")     or "Squad Powers"
    DEFAULT_DATA_START_ROW = current.get("data_start_row") or 2
    DEFAULT_NAME_COL       = current.get("name_col")       or "A"
    DEFAULT_TAB_GROWTH     = current.get("tab_growth")     or "Growth Tracking"
    DEFAULT_SNAPSHOT_DAY      = current.get("snapshot_day")      or 1
    DEFAULT_SNAPSHOT_INTERVAL = current.get("snapshot_interval") or 30

    await channel.send(
        "⚙️ **Growth Tracking Setup**\n"
        "Configure how the bot tracks your alliance's growth over time. "
        "Each month (or on your chosen schedule), the bot takes a snapshot of your members' stats "
        "and records them in your Google Sheet so you can track progress."
    )

    # ── Step 1: Enable? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        "**Step 1 of 7 — Enable growth tracking?**\n"
        "Should the bot automatically take snapshots of your members' stats on a schedule?",
        view=enabled_view,
    )
    await enabled_view.wait()
    if enabled_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
        return
    if not enabled_view.selected:
        save_growth_config(
            guild_id, enabled=0,
            tab_source=current.get("tab_source", ""),
            name_col=current.get("name_col", "A"),
            metrics=current.get("metrics", []),
            tab_growth=current.get("tab_growth", "Growth Tracking"),
            snapshot_frequency=current.get("snapshot_frequency", "monthly"),
            snapshot_day=current.get("snapshot_day", 1),
            snapshot_interval=current.get("snapshot_interval", 30),
            data_start_row=current.get("data_start_row", 2),
        )
        await channel.send("✅ Growth tracking disabled.")
        return

    # ── Step 2: Source tab ────────────────────────────────────────────────────
    tab_source = await ask_keep_or_change(
        "**Step 2 of 7 — Source Tab**\n"
        "Which tab in your Google Sheet contains your member data?\n"
        "⚠️ *Make sure this tab exists in your sheet.*",
        default=DEFAULT_TAB_SOURCE,
        modal_title="Source Tab",
        modal_label="Tab name",
    )
    if tab_source is None:
        return

    # ── Step 3: Data start row ────────────────────────────────────────────────
    start_raw = await ask_keep_or_change(
        "**Step 3 of 7 — Data Start Row**\n"
        "Which row does your member data start on? (Row 1 is usually the header)",
        default=str(DEFAULT_DATA_START_ROW),
        modal_title="Data Start Row",
        modal_label="Row number",
    )
    if start_raw is None:
        return
    try:
        data_start_row = int(str(start_raw).strip())
    except ValueError:
        await channel.send("⚠️ Please enter a row number like `2`. Run `/setup_growth` to try again.")
        return

    # ── Step 4: Name column ───────────────────────────────────────────────────
    name_raw = await ask_keep_or_change(
        "**Step 4 of 7 — Name Column**\n"
        "Which column contains the member's name?",
        default=DEFAULT_NAME_COL,
        modal_title="Name Column",
        modal_label="Column letter",
    )
    if name_raw is None:
        return
    name_col = name_raw.strip().upper()
    if len(name_col) != 1 or not name_col.isalpha():
        await channel.send("⚠️ Please enter a single column letter like `A`. Run `/setup_growth` to try again.")
        return

    # ── Step 5: Metrics ───────────────────────────────────────────────────────
    metrics = list(current.get("metrics", []))

    class MetricModal(discord.ui.Modal):
        def __init__(self, label_default: str = "", col_default: str = ""):
            super().__init__(title="Metric")
            self.label_value = None
            self.col_value = None
            self._label_input = discord.ui.TextInput(
                label="Label",
                placeholder="e.g. 1st Squad Power, THP, Total Kills",
                default=label_default,
                required=True,
                max_length=100,
            )
            self._col_input = discord.ui.TextInput(
                label="Column letter",
                placeholder="e.g. E",
                default=col_default,
                required=True,
                max_length=2,
            )
            self.add_item(self._label_input)
            self.add_item(self._col_input)

        async def on_submit(self, interaction: discord.Interaction):
            self.label_value = self._label_input.value.strip()
            self.col_value = self._col_input.value.strip().upper()
            await interaction.response.defer()
            self.stop()

    def _metrics_embed() -> discord.Embed:
        embed = discord.Embed(
            title="📊 Step 5 of 7 — Metrics to Track",
            description=(
                "Define which columns the bot should snapshot each period. "
                "Add as many as you want — for example a `1st Squad Power` column, `THP`, `Total Kills`, etc."
            ),
            color=discord.Color.blurple(),
        )
        if metrics:
            for m in metrics:
                embed.add_field(name=m["label"], value=f"Column {m['col']}", inline=False)
        else:
            embed.add_field(name="No metrics yet", value="Click **Add Metric** to begin.", inline=False)
        return embed

    while True:
        class MetricsActionView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.choice = None
                if not metrics:
                    self.edit_btn.disabled = True
                    self.delete_btn.disabled = True
                    self.done_btn.disabled = True

            @discord.ui.button(label="➕ Add Metric", style=discord.ButtonStyle.success, row=0)
            async def add_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                modal = MetricModal()
                await inter.response.send_modal(modal)
                await modal.wait()
                if modal.label_value and modal.col_value and modal.col_value.isalpha():
                    metrics.append({"label": modal.label_value, "col": modal.col_value})
                self.choice = "loop"
                self.stop()

            @discord.ui.button(label="✏️ Edit Metric", style=discord.ButtonStyle.primary, row=0)
            async def edit_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "edit"
                self.stop()
                await inter.response.defer()

            @discord.ui.button(label="🗑️ Delete Metric", style=discord.ButtonStyle.danger, row=0)
            async def delete_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "delete"
                self.stop()
                await inter.response.defer()

            @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.secondary, row=1)
            async def done_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "done"
                self.stop()
                await inter.response.defer()

        action_view = MetricsActionView()
        await channel.send(embed=_metrics_embed(), view=action_view)
        await action_view.wait()

        if action_view.choice is None:
            await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
            return
        if action_view.choice == "done":
            break
        if action_view.choice == "loop":
            continue

        if action_view.choice in ("edit", "delete") and not metrics:
            continue

        # Pick which metric to edit/delete
        class PickMetricView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.index = None
                options = [
                    discord.SelectOption(
                        label=m["label"][:100],
                        value=str(i),
                        description=f"Column {m['col']}",
                    )
                    for i, m in enumerate(metrics)
                ]
                self.select = discord.ui.Select(
                    placeholder="Choose a metric...",
                    options=options,
                    min_values=1, max_values=1,
                )
                self.select.callback = self._on_select
                self.add_item(self.select)

            async def _on_select(self, inter: discord.Interaction):
                self.index = int(self.select.values[0])
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        pick_view = PickMetricView()
        verb = "edit" if action_view.choice == "edit" else "delete"
        await channel.send(f"Which metric do you want to {verb}?", view=pick_view)
        await pick_view.wait()
        if pick_view.index is None:
            await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
            return

        if action_view.choice == "delete":
            removed = metrics.pop(pick_view.index)
            await channel.send(f"🗑️ Removed: **{removed['label']}** (column {removed['col']})")
            continue

        # Edit: open a modal pre-filled with the chosen metric
        existing = metrics[pick_view.index]

        class EditLaunchView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.modal = MetricModal(
                    label_default=existing["label"], col_default=existing["col"]
                )
                self.confirmed = False

            @discord.ui.button(label="✏️ Edit values", style=discord.ButtonStyle.primary)
            async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
                await inter.response.send_modal(self.modal)
                await self.modal.wait()
                self.confirmed = True
                self.stop()

        edit_launch = EditLaunchView()
        await channel.send(
            f"Editing **{existing['label']}** (column {existing['col']}). Click below to update.",
            view=edit_launch,
        )
        await edit_launch.wait()
        if edit_launch.modal.label_value and edit_launch.modal.col_value and edit_launch.modal.col_value.isalpha():
            metrics[pick_view.index] = {
                "label": edit_launch.modal.label_value,
                "col":   edit_launch.modal.col_value,
            }

    if not metrics:
        await channel.send("⚠️ No metrics defined. Run `/setup_growth` to try again.")
        return

    # ── Step 6: Growth tracking tab ───────────────────────────────────────────
    tab_growth = await ask_keep_or_change(
        "**Step 6 of 7 — Growth Tracking Tab**\n"
        "Which tab should snapshots be written to?\n"
        "⚠️ *If the tab doesn't exist, the bot will create it automatically.*",
        default=DEFAULT_TAB_GROWTH,
        modal_title="Growth Tracking Tab",
        modal_label="Tab name",
    )
    if tab_growth is None:
        return

    # ── Step 7: Snapshot frequency ────────────────────────────────────────────
    class FrequencyView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_TIMEOUT)
            self.selected = None

        @discord.ui.button(label="📅 Monthly (1st of each month)", style=discord.ButtonStyle.primary)
        async def monthly(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "monthly"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Frequency: **Monthly**", view=self)
            self.stop()

        @discord.ui.button(label="🔁 Custom interval (every X days)", style=discord.ButtonStyle.secondary)
        async def custom(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "interval"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(view=self)
            self.stop()

    freq_view = FrequencyView()
    await channel.send(
        "**Step 7 of 7 — Snapshot Frequency**\n"
        "How often should the bot take a snapshot?",
        view=freq_view,
    )
    await freq_view.wait()
    if not freq_view.selected:
        await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
        return

    snapshot_frequency = freq_view.selected
    snapshot_day       = DEFAULT_SNAPSHOT_DAY
    snapshot_interval  = DEFAULT_SNAPSHOT_INTERVAL

    if snapshot_frequency == "monthly":
        day_raw = await ask_keep_or_change(
            "**Step 7a of 7 — Snapshot Day**\n"
            "Which day of the month should the snapshot run? (1–28)",
            default=str(DEFAULT_SNAPSHOT_DAY),
            modal_title="Snapshot Day",
            modal_label="Day of month (1–28)",
        )
        if day_raw is None:
            return
        try:
            snapshot_day = max(1, min(28, int(str(day_raw).strip())))
        except ValueError:
            snapshot_day = DEFAULT_SNAPSHOT_DAY
    else:
        interval_raw = await ask_keep_or_change(
            "**Step 7a of 7 — Interval (days)**\n"
            "How many days between each snapshot?",
            default=str(DEFAULT_SNAPSHOT_INTERVAL),
            modal_title="Interval",
            modal_label="Days between snapshots",
        )
        if interval_raw is None:
            return
        try:
            snapshot_interval = max(1, int(str(interval_raw).strip()))
        except ValueError:
            snapshot_interval = DEFAULT_SNAPSHOT_INTERVAL

    # ── Save ───────────────────────────────────────────────────────────────────
    save_growth_config(
        guild_id, enabled=1,
        tab_source=tab_source, name_col=name_col,
        metrics=metrics, tab_growth=tab_growth,
        snapshot_frequency=snapshot_frequency,
        snapshot_day=snapshot_day,
        snapshot_interval=snapshot_interval,
        data_start_row=data_start_row,
    )

    freq_desc  = (
        f"Monthly on day {snapshot_day}"
        if snapshot_frequency == "monthly"
        else f"Every {snapshot_interval} days"
    )
    metrics_display = "\n".join(f"• **{m['label']}** — column {m['col']}" for m in metrics)

    embed = discord.Embed(title="✅ Growth Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Source Tab",        value=tab_source,           inline=False)
    embed.add_field(name="Name Column",       value=f"Column {name_col}", inline=False)
    embed.add_field(name="Data Start Row",    value=str(data_start_row),  inline=False)
    embed.add_field(name="Growth Tab",        value=tab_growth,           inline=False)
    embed.add_field(name="Snapshot Schedule", value=freq_desc,            inline=False)
    embed.add_field(name="Metrics",           value=metrics_display,      inline=False)
    embed.set_footer(text="Run /setup_growth again to update. Use /growth to take a manual snapshot.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Growth config saved for guild {guild_id}")

async def run_train_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the train schedule."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 2000):
        """Send prompt and wait for typed reply. Both stay visible."""
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=300),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    from config import get_train_config
    current = get_train_config(guild_id)

    await channel.send(
        "⚙️ **Train Schedule Setup**\n"
        "*Configure how the train schedule works for your alliance.*"
    )

    # ── Step 1: Sheet tab ──────────────────────────────────────────────────────
    current_tab = current["tab_name"]

    class TabConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.tab_name  = None
            self.confirmed = False

        @discord.ui.button(label=f"✅ Keep current tab", style=discord.ButtonStyle.success)
        async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
            self.tab_name  = current_tab
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(
                content=f"✅ Using sheet tab: **{current_tab}**", view=self
            )
            self.stop()

        @discord.ui.button(label="✏️ Enter a different tab name", style=discord.ButtonStyle.secondary)
        async def change(self, inter: discord.Interaction, button: discord.ui.Button):
            modal = TextInputModal("Sheet Tab Name", "Tab name", default=current_tab)
            await inter.response.send_modal(modal)
            await modal.wait()
            self.tab_name  = modal.value or current_tab
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            try:
                await inter.message.edit(
                    content=f"✅ Using sheet tab: **{self.tab_name}**", view=self
                )
            except discord.HTTPException:
                pass
            self.stop()

    tab_view = TabConfirmView()
    await channel.send(
        f"**Step 1 of 7 — Schedule Sheet Tab**\n"
        f"The train schedule is stored in the **`{current_tab}`** tab of your Google Sheet.\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*",
        view=tab_view,
    )
    await tab_view.wait()
    if not tab_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
        return
    tab_name = tab_view.tab_name

    # ── Step 2: Generate blurbs? ───────────────────────────────────────────────
    blurb_view = YesNoView()
    await channel.send(
        "**Step 2 of 7 — ChatGPT Blurb Generation**\n"
        "Would you like the bot to help generate a ChatGPT prompt each day when you assign a train?\n"
        "This lets you quickly produce a personalised announcement blurb for the member.\n"
        "*(You can always set this up later by running `/setup_train` again)*",
        view=blurb_view,
    )
    await blurb_view.wait()
    if blurb_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
        return
    blurbs_enabled = 1 if blurb_view.selected else 0

    themes        = current["themes"]
    tones         = current["tones"]
    default_tone  = current["default_tone"]
    prompt_template = current.get("prompt_template", "")

    if blurbs_enabled:
        # ── Step 3: Themes ─────────────────────────────────────────────────────
        current_themes = ", ".join(current["themes"])

        class KeepOrChangeView(discord.ui.View):
            def __init__(self, label: str):
                super().__init__(timeout=120)
                self.keep_current = None
                self._label = label

            @discord.ui.button(label="✅ Keep current", style=discord.ButtonStyle.success)
            async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
                self.keep_current = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content=f"✅ Keeping current {self._label}.", view=self
                )
                self.stop()

            @discord.ui.button(label="✏️ Enter new list", style=discord.ButtonStyle.secondary)
            async def change(self, inter: discord.Interaction, button: discord.ui.Button):
                self.keep_current = False
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        themes_keep_view = KeepOrChangeView("themes")
        await channel.send(
            f"**Step 3 of 7 — Themes**\n"
            f"These appear as options when selecting a theme for a member's train day.\n\n"
            f"**Current themes:**\n`{current_themes}`\n\n"
            f"**Example themes:**\n"
            f"`Welcome to the Alliance, Birthday, Milestone, War / Performance, General Celebration, Contest / Raffle, Custom`",
            view=themes_keep_view,
        )
        await themes_keep_view.wait()
        if themes_keep_view.keep_current is None:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return

        if not themes_keep_view.keep_current:
            themes_raw = await ask_text("Enter your themes as a comma-separated list:")
            if themes_raw is None:
                return
            themes = [t.strip() for t in themes_raw.split(",") if t.strip()] or current["themes"]

        # ── Step 4: Tones ──────────────────────────────────────────────────────
        current_tones = ", ".join(current["tones"])

        tones_keep_view = KeepOrChangeView("tones")
        await channel.send(
            f"**Step 4 of 7 — Tones**\n"
            f"These let leadership adjust the writing style of the generated blurb.\n\n"
            f"**Current tones:**\n`{current_tones}`\n\n"
            f"**Example tones:**\n"
            f"`Default (match the theme), More casual, More intense, Funny, Serious, Cinematic / Dramatic`",
            view=tones_keep_view,
        )
        await tones_keep_view.wait()
        if tones_keep_view.keep_current is None:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return

        if not tones_keep_view.keep_current:
            tones_raw = await ask_text("Enter your tones as a comma-separated list:")
            if tones_raw is None:
                return
            tones = [t.strip() for t in tones_raw.split(",") if t.strip()] or current["tones"]

        # ── Step 5: Default tone ───────────────────────────────────────────────
        class ToneDefaultView(discord.ui.View):
            def __init__(self, tone_list: list):
                super().__init__(timeout=120)
                self.selected = None
                select = discord.ui.Select(
                    placeholder="Select default tone...",
                    options=[discord.SelectOption(label=t, value=t) for t in tone_list],
                )
                async def _cb(inter: discord.Interaction):
                    self.selected = select.values[0]
                    select.disabled = True
                    await inter.response.edit_message(
                        content=f"✅ Default tone: **{self.selected}**", view=self
                    )
                    self.stop()
                select.callback = _cb
                self.add_item(select)

        tone_default_view = ToneDefaultView(tones)
        await channel.send(
            f"**Step 5 of 7 — Default Tone**\n"
            f"Which tone should be pre-selected by default?",
            view=tone_default_view,
        )
        await tone_default_view.wait()
        if not tone_default_view.selected:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return
        default_tone = tone_default_view.selected

        # ── Step 6: Prompt template ────────────────────────────────────────────
        class SkipTemplateView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.skipped = False

            @discord.ui.button(label="⏭️ Skip — keep existing template", style=discord.ButtonStyle.secondary)
            async def skip(self, inter: discord.Interaction, button: discord.ui.Button):
                self.skipped = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content="✅ Keeping existing prompt template.", view=self
                )
                self.stop()

        skip_view = SkipTemplateView()
        await channel.send(
            "**Step 6 of 7 — Prompt Template**\n"
            "Enter a template that you would write for ChatGPT to generate a blurb about the day's train. "
            "Use these placeholders:\n"
            "• `{name}` — the member's name\n"
            "• `{theme}` — the selected theme\n"
            "• `{tone}` — the selected tone\n"
            "• `{notes}` — any notes stored for this member\n\n"
            "**Example:**\n"
            "```\n"
            "You are writing a short motivational blurb for a Last War alliance announcement.\n"
            "Keep it under 3 sentences and make it feel personal and energetic.\n\n"
            "Member: {name}\n"
            "Theme: {theme}\n"
            "Tone: {tone}\n"
            "Notes: {notes}\n\n"
            "Write the blurb:\n"
            "```\n"
            "*This form will time out in 5 minutes. You can run `/setup_train` again to add your template if it times out.*",
            view=skip_view,
        )

        # Wait for either a typed message or the skip button
        done = asyncio.Event()
        new_template = None

        async def wait_for_message():
            nonlocal new_template
            try:
                reply = await bot.wait_for("message", check=check, timeout=300)
                new_template = reply.content.strip()
                done.set()
                skip_view.stop()
            except asyncio.TimeoutError:
                done.set()

        msg_task = asyncio.create_task(wait_for_message())
        await skip_view.wait()
        msg_task.cancel()

        if skip_view.skipped:
            pass  # keep existing template
        elif new_template:
            prompt_template = new_template
        else:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return

    # ── Step 7: Reminders ─────────────────────────────────────────────────────
    reminder_view = YesNoView()
    await channel.send(
        "**Step 7 of 7 — Train Reminders**\n"
        "Should the bot post a reminder to leadership when someone is assigned the train each day?",
        view=reminder_view,
    )
    await reminder_view.wait()
    if reminder_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
        return
    reminders_enabled  = 1 if reminder_view.selected else 0
    reminder_channel_id = 0
    reminder_time       = "22:00"

    if reminders_enabled:
        # ── Step 7a: Reminder channel ──────────────────────────────────────────
        reminder_ch_view = ChannelSelectStep(
            "Select the reminder channel...",
            suggested_name="leadership",
        )
        await channel.send(
            "**Step 7a of 7 — Reminder Channel**\n"
            "Which channel should the train reminder be posted to?",
            view=reminder_ch_view,
        )
        await reminder_ch_view.wait()
        if not reminder_ch_view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return
        reminder_channel_id = reminder_ch_view.selected_channel.id

        # ── Step 7b: Reminder time ─────────────────────────────────────────────
        from config import get_config
        guild_cfg = get_config(guild_id)
        tz_label  = TIMEZONE_LABELS.get(guild_cfg.timezone if guild_cfg else "America/New_York", "ET")
        time_raw  = await ask_text(
            f"**Step 7b of 7 — Reminder Time**\n"
            f"What time should the reminder fire? *(in your timezone: {tz_label})*\n"
            f"*(e.g. `10:00pm`, `9:00am`)*"
        )
        if time_raw is None:
            return
        parsed = _parse_12h_time(time_raw)
        if not parsed:
            await channel.send("⚠️ Could not read that time. Using `10:00pm` as default.")
            parsed = "22:00"
        reminder_time = parsed

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_train_config
    save_train_config(
        guild_id, tab_name, themes, tones, prompt_template, default_tone,
        blurbs_enabled=blurbs_enabled,
        reminders_enabled=reminders_enabled,
        reminder_channel_id=reminder_channel_id,
        reminder_time=reminder_time,
    )

    embed = discord.Embed(title="✅ Train Schedule Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",       value=tab_name,                        inline=True)
    embed.add_field(name="Blurb Generation",value="Enabled" if blurbs_enabled else "Disabled", inline=True)
    embed.add_field(name="Reminders",       value="Enabled" if reminders_enabled else "Disabled", inline=True)
    if reminders_enabled:
        embed.add_field(name="Reminder Channel", value=f"<#{reminder_channel_id}>", inline=True)
        embed.add_field(name="Reminder Time",    value=reminder_time,               inline=True)
    if blurbs_enabled:
        embed.add_field(name="Default Tone", value=default_tone,          inline=True)
        embed.add_field(name="Themes",       value=", ".join(themes),     inline=False)
        embed.add_field(name="Tones",        value=", ".join(tones),      inline=False)
        if prompt_template:
            preview = prompt_template[:200] + ("..." if len(prompt_template) > 200 else "")
            embed.add_field(name="Template Preview", value=f"```{preview}```", inline=False)
    embed.set_footer(text="Run /setup_train again to update any of these settings.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Train config saved for guild {guild_id}")

async def run_survey_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the squad powers survey."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    from config import get_survey_config, save_survey_config, OGV_SURVEY_QUESTIONS
    current   = get_survey_config(guild_id)
    questions = list(current.get("questions") or [])

    await channel.send(
        "⚙️ **Survey Setup**\n"
        "Configure the squad powers survey for your alliance."
    )

    # ── Helper: tab keep/change view ──────────────────────────────────────────
    def make_tab_view(current_tab: str) -> discord.ui.View:
        class TabView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.tab_name  = None
                self.confirmed = False

            @discord.ui.button(label="✅ Keep current tab", style=discord.ButtonStyle.success)
            async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
                self.tab_name  = current_tab
                self.confirmed = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content=f"✅ Using tab: **{current_tab}**", view=self
                )
                self.stop()

            @discord.ui.button(label="✏️ Enter a different tab name", style=discord.ButtonStyle.secondary)
            async def change(self, inter: discord.Interaction, button: discord.ui.Button):
                modal = TextInputModal("Tab Name", "Tab name", default=current_tab)
                await inter.response.send_modal(modal)
                await modal.wait()
                self.tab_name  = modal.value or current_tab
                self.confirmed = True
                for item in self.children: item.disabled = True
                try:
                    await inter.message.edit(
                        content=f"✅ Using tab: **{self.tab_name}**", view=self
                    )
                except discord.HTTPException:
                    pass
                self.stop()
        return TabView()

    # ── Step 1: Survey channel ─────────────────────────────────────────────────
    survey_ch_view = ChannelSelectStep(
        "Select the survey channel...", suggested_name="squad-survey"
    )
    await channel.send(
        "**Step 1 of 6 — Survey Channel**\n"
        "Select the channel where the survey button will be posted for members to access:",
        view=survey_ch_view,
    )
    await survey_ch_view.wait()
    if not survey_ch_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return
    survey_channel_id = survey_ch_view.selected_channel.id

    # ── Step 2: Survey notification channel ───────────────────────────────────
    notify_ch_view = ChannelSelectStep(
        "Select the survey notification channel...", suggested_name="survey-responses"
    )
    await channel.send(
        "**Step 2 of 6 — Survey Notification Channel**\n"
        "Select the channel where leadership will be notified when a member submits the survey:",
        view=notify_ch_view,
    )
    await notify_ch_view.wait()
    if not notify_ch_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return
    survey_notify_channel_id = notify_ch_view.selected_channel.id

    # ── Step 3: Squad Powers tab ───────────────────────────────────────────────
    current_sp_tab = current.get("tab_squad_powers", "Squad Powers")
    sp_view = make_tab_view(current_sp_tab)
    await channel.send(
        f"**Step 3 of 6 — Member Statistics Tab**\n"
        f"Which tab stores your members' statistics? We will update this sheet on each submission.\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*\n"
        f"*(Current: `{current_sp_tab}`)*",
        view=sp_view,
    )
    await sp_view.wait()
    if not sp_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return
    tab_squad_powers = sp_view.tab_name

    # ── Step 4: Survey History tab ─────────────────────────────────────────────
    current_hist_tab = current.get("tab_history", "Survey History")
    hist_view = make_tab_view(current_hist_tab)
    await channel.send(
        f"**Step 4 of 6 — Survey History Tab**\n"
        f"Which tab stores the full history of all submissions?\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*\n"
        f"*(Current: `{current_hist_tab}`)*",
        view=hist_view,
    )
    await hist_view.wait()
    if not hist_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return
    tab_history = hist_view.tab_name

    # ── Step 5: Intro message ──────────────────────────────────────────────────
    await channel.send(
        "**Step 5 of 6 — Survey Intro Message**\n"
        "When your survey is posted, what introductory message do you want your members to see "
        "before they take the survey?\n\n"
        "**Example:**\n"
        "*Please fill out this survey each week to help us track squad powers, "
        "balance our teams, and prepare for season events!*"
    )
    intro_reply = await wizard_registry.wait_or_cancel(
        bot.wait_for("message", check=check, timeout=300),
        cancel_event,
    )
    if intro_reply is None:
        if cancel_event.is_set():
            await channel.send("❌ Cancelled.")
        else:
            await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return
    intro_message = intro_reply.content.strip()

    # ── Step 6: Survey Questions ───────────────────────────────────────────────
    # Show default questions and ask keep/edit/scratch
    default_q_list = "\n".join(
        f"{i+1}. **{q['label']}** — {'dropdown: ' + ', '.join(q['options']) if q['type'] == 'dropdown' else 'text'}"
        for i, q in enumerate(OGV_SURVEY_QUESTIONS)
    )
    current_q_list = "\n".join(
        f"{i+1}. **{q['label']}** — {'dropdown: ' + ', '.join(q['options']) if q['type'] == 'dropdown' else 'text'}"
        for i, q in enumerate(questions)
    ) if questions else "*(no questions configured yet)*"

    class QuestionStartView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.choice = None

        @discord.ui.button(label="✅ Use default questions", style=discord.ButtonStyle.success)
        async def use_default(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "default"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Using default questions.", view=self)
            self.stop()

        @discord.ui.button(label="✏️ Edit current questions", style=discord.ButtonStyle.primary)
        async def edit_current(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "edit"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✏️ Entering edit mode...", view=self)
            self.stop()

        @discord.ui.button(label="🔄 Start from scratch", style=discord.ButtonStyle.secondary)
        async def start_scratch(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "scratch"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="🔄 Starting from scratch...", view=self)
            self.stop()

    q_start_view = QuestionStartView()
    await channel.send(
        "**Step 6 of 6 — Survey Questions**\n\n"
        f"**Default questions (Last War):**\n{default_q_list}\n\n"
        f"**Your current questions:**\n{current_q_list}\n\n"
        "Would you like to use the defaults, edit your current questions, or start from scratch?",
        view=q_start_view,
    )
    await q_start_view.wait()
    if not q_start_view.choice:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return

    if q_start_view.choice == "default":
        questions = list(OGV_SURVEY_QUESTIONS)

    elif q_start_view.choice in ("edit", "scratch"):
        if q_start_view.choice == "scratch":
            questions = []

        # ── Question builder loop ──────────────────────────────────────────────
        async def build_question_list():
            """Show current question list with Add and Finish buttons."""
            nonlocal questions

            while True:
                # Build display
                if questions:
                    q_display = "\n".join(
                        f"{i+1}. **{q['label']}** — "
                        + ('dropdown: ' + ', '.join(q['options']) if q['type'] == 'dropdown' else 'text')
                        + (f" *(help: {q['placeholder']})*" if q.get('placeholder') else "")
                        for i, q in enumerate(questions)
                    )
                else:
                    q_display = "*(no questions added yet)*"

                class QuestionListView(discord.ui.View):
                    def __init__(self, q_count: int):
                        super().__init__(timeout=300)
                        self.action     = None
                        self.edit_index = None
                        self.del_index  = None

                        if q_count > 0:
                            # Edit dropdown
                            edit_select = discord.ui.Select(
                                placeholder="✏️ Edit a question...",
                                options=[discord.SelectOption(label=f"Edit: {questions[i]['label']}", value=str(i))
                                         for i in range(q_count)],
                                row=0,
                            )
                            async def _edit_cb(inter: discord.Interaction):
                                self.action     = "edit"
                                self.edit_index = int(edit_select.values[0])
                                for item in self.children: item.disabled = True
                                await inter.response.edit_message(view=self)
                                self.stop()
                            edit_select.callback = _edit_cb
                            self.add_item(edit_select)

                            # Delete dropdown
                            del_select = discord.ui.Select(
                                placeholder="🗑️ Delete a question...",
                                options=[discord.SelectOption(label=f"Delete: {questions[i]['label']}", value=str(i))
                                         for i in range(q_count)],
                                row=1,
                            )
                            async def _del_cb(inter: discord.Interaction):
                                self.action    = "delete"
                                self.del_index = int(del_select.values[0])
                                for item in self.children: item.disabled = True
                                await inter.response.edit_message(view=self)
                                self.stop()
                            del_select.callback = _del_cb
                            self.add_item(del_select)

                    @discord.ui.button(label="➕ Add Question", style=discord.ButtonStyle.primary, row=2)
                    async def add_q(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.action = "add"
                        for item in self.children: item.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()

                    @discord.ui.button(label="✅ Finish Survey Setup", style=discord.ButtonStyle.success, row=2)
                    async def finish(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.action = "finish"
                        for item in self.children: item.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()

                list_view = QuestionListView(len(questions))
                await channel.send(
                    f"**Current Questions:**\n{q_display}",
                    view=list_view,
                )
                await list_view.wait()

                if not list_view.action:
                    await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                    return False

                if list_view.action == "finish":
                    return True

                elif list_view.action == "delete":
                    idx     = list_view.del_index
                    removed = questions.pop(idx)
                    await channel.send(f"🗑️ Removed: **{removed['label']}**")

                elif list_view.action in ("add", "edit"):
                    # ── Question builder ───────────────────────────────────────
                    if list_view.action == "edit":
                        idx      = list_view.edit_index
                        existing = questions[idx]
                        q_num    = f"Question {idx + 1}"
                    else:
                        existing = {}
                        q_num    = f"Question {len(questions) + 1}"

                    # Label
                    await channel.send(
                        f"**{q_num} — Label**\n"
                        f"What is the label for this question? (e.g. `1st Squad Power`, `Profession`)"
                        + (f"\n*(Current: `{existing.get('label', '')}`)*" if existing else "")
                    )
                    try:
                        label_reply = await bot.wait_for("message", check=check, timeout=120)
                        q_label     = label_reply.content.strip() or existing.get("label", "")
                    except asyncio.TimeoutError:
                        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                        return False

                    q_key = q_label.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")

                    # Type
                    class TypeView(discord.ui.View):
                        def __init__(self):
                            super().__init__(timeout=120)
                            self.selected = None
                            select = discord.ui.Select(
                                placeholder="Select answer type...",
                                options=[
                                    discord.SelectOption(label="Text — member types their answer", value="text"),
                                    discord.SelectOption(label="Dropdown — member selects from a list", value="dropdown"),
                                ],
                            )
                            async def _cb(inter: discord.Interaction):
                                self.selected = select.values[0]
                                select.disabled = True
                                await inter.response.edit_message(
                                    content=f"✅ Type: **{'Text' if self.selected == 'text' else 'Dropdown'}**",
                                    view=self,
                                )
                                self.stop()
                            select.callback = _cb
                            self.add_item(select)

                    type_view = TypeView()
                    current_type = existing.get("type", "text")
                    await channel.send(
                        f"**{q_num} — Answer Type**\n"
                        f"Does your member answer by typing or selecting from a dropdown list?"
                        + (f"\n*(Current: `{current_type}`)*" if existing else ""),
                        view=type_view,
                    )
                    await type_view.wait()
                    if not type_view.selected:
                        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                        return False
                    q_type = type_view.selected

                    # Help text
                    await channel.send(
                        f"**{q_num} — Help Text**\n"
                        f"Do you want to show help text for this question? "
                        f"This appears as a hint to help members answer correctly.\n"
                        f"*(e.g. `e.g. 43.27` or `What is your first squad's current power?`)*\n"
                        f"Type your help text, or type `none` to skip."
                        + (f"\n*(Current: `{existing.get('placeholder', 'none')}`)*" if existing else "")
                    )
                    try:
                        help_reply  = await bot.wait_for("message", check=check, timeout=120)
                        help_raw    = help_reply.content.strip()
                        placeholder = "" if help_raw.lower() == "none" else help_raw
                    except asyncio.TimeoutError:
                        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                        return False

                    # Dropdown options (if dropdown type)
                    options = []
                    if q_type == "dropdown":
                        cur_opts = ", ".join(existing.get("options", [])) if existing else ""
                        await channel.send(
                            f"**{q_num} — Dropdown Options**\n"
                            f"Enter the options you want your members to be able to select from, "
                            f"as comma-separated values. Maximum of 25 options.\n"
                            f"*(e.g. `Missile, Air, Tank`)*"
                            + (f"\n*(Current: `{cur_opts}`)*" if cur_opts else "")
                        )
                        try:
                            opts_reply = await bot.wait_for("message", check=check, timeout=120)
                            options    = [o.strip() for o in opts_reply.content.split(",") if o.strip()][:25]
                        except asyncio.TimeoutError:
                            await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                            return False

                    new_q = {
                        "key":         q_key,
                        "label":       q_label,
                        "type":        q_type,
                        "options":     options,
                        "placeholder": placeholder,
                        "max_chars":   0,
                    }

                    if list_view.action == "edit":
                        questions[list_view.edit_index] = new_q
                        await channel.send(f"✅ Updated: **{q_label}**")
                    else:
                        questions.append(new_q)
                        await channel.send(f"✅ Added: **{q_label}** — {len(questions)} question(s) so far.")

        result = await build_question_list()
        if not result:
            return

    if not questions:
        await channel.send("⚠️ No questions defined. Run `/setup_survey` to try again.")
        return

    # ── Save — including channel IDs ───────────────────────────────────────────
    save_survey_config(guild_id, tab_squad_powers, tab_history, questions, intro_message)

    # Also persist survey channels to guild_configs
    from config import update_config_field
    update_config_field(guild_id, "survey_channel_id",        survey_channel_id)
    update_config_field(guild_id, "survey_notify_channel_id", survey_notify_channel_id)

    q_summary = "\n".join(
        f"• **{q['label']}** — {q['type']}"
        + (f" ({', '.join(q['options'])})" if q['type'] == 'dropdown' else "")
        for q in questions
    )
    embed = discord.Embed(title="✅ Survey Configured", color=discord.Color.green())
    embed.add_field(name="Survey Channel",      value=f"<#{survey_channel_id}>",        inline=True)
    embed.add_field(name="Notification Channel",value=f"<#{survey_notify_channel_id}>", inline=True)
    embed.add_field(name="Stats Tab",           value=tab_squad_powers,                  inline=True)
    embed.add_field(name="History Tab",         value=tab_history,                       inline=True)
    embed.add_field(name="Questions",           value=q_summary[:1024],                  inline=False)
    embed.set_footer(text="Run /setup_survey again to update. Run /survey_post to post the survey button.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Survey config saved for guild {guild_id} — {len(questions)} questions")

async def run_storm_setup(interaction: discord.Interaction, bot, event_type: str):
    """Shared setup wizard for Desert Storm and Canyon Storm."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    label    = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    cmd_name = "setup_desertstorm" if event_type == "DS" else "setup_canyonstorm"
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 2000):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=300),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    from config import get_storm_config, get_config, GENERIC_DS_TEMPLATE, GENERIC_CS_TEMPLATE
    current   = get_storm_config(guild_id, event_type)
    guild_cfg = get_config(guild_id)
    timezone  = guild_cfg.timezone if guild_cfg and guild_cfg.timezone else "America/New_York"
    tz_label  = TIMEZONE_LABELS.get(timezone, timezone)

    # Default template and placeholders per event type
    if event_type == "DS":
        default_template  = GENERIC_DS_TEMPLATE
        placeholder_info  = (
            "• `{alliance_name}` — your alliance name\n"
            "• `{zones}` — zone assignments block\n"
            "• `{subs}` — substitute members\n"
            "• `{time}` — event time (auto-filled when drafting)"
        )
    else:
        default_template  = GENERIC_CS_TEMPLATE
        placeholder_info  = (
            "• `{alliance_name}` — your alliance name\n"
            "• `{zones}` — zone assignments block\n"
            "• `{subs}` — substitute members\n"
            "• `{time}` — event time (auto-filled when drafting)"
        )

    await channel.send(f"⚙️ **{label} Setup**")

    # ── Step 1: Sheet tab ──────────────────────────────────────────────────────
    current_tab = current.get("tab_name") or ("DS Assignments" if event_type == "DS" else "CS Assignments")

    class TabConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.tab_name  = None
            self.confirmed = False

        @discord.ui.button(label="✅ Keep current tab", style=discord.ButtonStyle.success)
        async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
            self.tab_name  = current_tab
            self.confirmed = True
            for item in self.children: item.disabled = True
            await inter.response.edit_message(
                content=f"✅ Using sheet tab: **{current_tab}**", view=self
            )
            self.stop()

        @discord.ui.button(label="✏️ Enter a different tab name", style=discord.ButtonStyle.secondary)
        async def change(self, inter: discord.Interaction, button: discord.ui.Button):
            modal = TextInputModal("Sheet Tab Name", "Tab name", default=current_tab)
            await inter.response.send_modal(modal)
            await modal.wait()
            self.tab_name  = modal.value or current_tab
            self.confirmed = True
            for item in self.children: item.disabled = True
            try:
                await inter.message.edit(
                    content=f"✅ Using sheet tab: **{self.tab_name}**", view=self
                )
            except discord.HTTPException:
                pass
            self.stop()

    tab_view = TabConfirmView()
    await channel.send(
        f"**Step 1 of 3 — Sheet Tab**\n"
        f"Which tab in your Google Sheet stores the {label} zone assignments?\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*\n"
        f"ℹ️ *The bot will manage the data structure of this tab automatically — "
        f"you don't need to set up any specific columns or formatting beforehand.*\n"
        f"*(Current: `{current_tab}`)*",
        view=tab_view,
    )
    await tab_view.wait()
    if not tab_view.confirmed:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return
    tab_name = tab_view.tab_name

    # ── Step 2: Which teams? ───────────────────────────────────────────────────
    class TeamChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.selected = None

        @discord.ui.button(label="Team A & Team B", style=discord.ButtonStyle.primary)
        async def both(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "both"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Teams: **Team A & Team B**", view=self)
            self.stop()

        @discord.ui.button(label="Team A only", style=discord.ButtonStyle.secondary)
        async def a_only(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "A"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Teams: **Team A only**", view=self)
            self.stop()

        @discord.ui.button(label="Team B only", style=discord.ButtonStyle.secondary)
        async def b_only(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "B"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Teams: **Team B only**", view=self)
            self.stop()

    team_view = TeamChoiceView()
    await channel.send(
        f"**Step 2 of 3 — Which teams do you run for {label}?**",
        view=team_view,
    )
    await team_view.wait()
    if not team_view.selected:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return
    teams = team_view.selected

    # ── Step 3: Storm log channel ─────────────────────────────────────────────
    log_ch_view = ChannelSelectStep(
        f"Select the {label} log channel...",
        suggested_name="storm-log",
    )
    await channel.send(
        f"**Step 3 of 4 — Storm Log Channel**\n"
        f"Select the channel where {label} participation logs will be posted:",
        view=log_ch_view,
    )
    await log_ch_view.wait()
    if not log_ch_view.confirmed:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return
    log_channel_id = log_ch_view.selected_channel.id

    # ── Step 4: Mail template(s) ───────────────────────────────────────────────

    async def get_template(team_label: str) -> str | None:
        """Get template for one team — show default with use/edit choice."""
        class TemplateChoiceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.use_default = None

            @discord.ui.button(label="✅ Use default template", style=discord.ButtonStyle.success)
            async def use_def(self, inter: discord.Interaction, button: discord.ui.Button):
                self.use_default = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content=f"✅ Using default template for {team_label}.", view=self
                )
                self.stop()

            @discord.ui.button(label="✏️ Edit template", style=discord.ButtonStyle.secondary)
            async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
                self.use_default = False
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        choice_view = TemplateChoiceView()
        await channel.send(
            f"**{label} Mail Template — {team_label}**\n"
            f"When you draft the mail each week, you will be able to select the time slot "
            f"when you are running that team's {label}.\n\n"
            f"Here is the default template:\n"
            f"```\n{default_template}\n```\n"
            f"Would you like to use this or edit it?",
            view=choice_view,
        )
        await choice_view.wait()
        if choice_view.use_default is None:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        if choice_view.use_default:
            return default_template

        # User wants to edit — show variables and ask for input
        await channel.send(
            f"Paste your custom template for **{team_label}**. "
            f"You can copy the default above and modify it, or write your own.\n\n"
            f"**Available placeholders:**\n{placeholder_info}\n\n"
            f"*This form will time out in 5 minutes. "
            f"You can run `/{cmd_name}` again if it times out.*"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=300)
            return reply.content.strip() or default_template
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None

    if teams == "both":
        # Ask if one template for both or separate
        class SharedTemplateView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected = None

            @discord.ui.button(label="One template for both teams", style=discord.ButtonStyle.primary)
            async def shared(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = "shared"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content="✅ **One shared template** for Team A & B", view=self
                )
                self.stop()

            @discord.ui.button(label="Separate templates per team", style=discord.ButtonStyle.secondary)
            async def separate(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = "separate"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content="✅ **Separate templates** for Team A & Team B", view=self
                )
                self.stop()

        shared_view = SharedTemplateView()
        await channel.send(
            "**Step 3 of 3 — Mail Template**\n"
            "Do you want one template that applies to both teams, or separate templates per team?",
            view=shared_view,
        )
        await shared_view.wait()
        if not shared_view.selected:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return

        template_a = await get_template("Team A & B" if shared_view.selected == "shared" else "Team A")
        if template_a is None:
            return
        if shared_view.selected == "separate":
            template_b = await get_template("Team B")
            if template_b is None:
                return
        else:
            template_b = template_a

    else:
        team_label = "Team A" if teams == "A" else "Team B"
        await channel.send(f"**Step 4 of 4 — Mail Template**")
        template = await get_template(team_label)
        if template is None:
            return
        template_a = template if teams == "A" else ""
        template_b = template if teams == "B" else ""

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_storm_config, update_config_field
    if template_a:
        save_storm_config(guild_id, f"{event_type}_A", tab_name, template_a,
                          "", "", "", "", "", "", timezone, log_channel_id)
    if template_b:
        save_storm_config(guild_id, f"{event_type}_B", tab_name, template_b,
                          "", "", "", "", "", "", timezone, log_channel_id)
    save_storm_config(guild_id, event_type, tab_name, template_a or template_b,
                      "", "", "", "", "", "", timezone, log_channel_id)

    # Persist the log channel to guild_configs so storm_log.py can read it
    if event_type == "DS":
        update_config_field(guild_id, "ds_log_channel_id", log_channel_id)
    else:
        update_config_field(guild_id, "cs_log_channel_id", log_channel_id)

    embed = discord.Embed(title=f"✅ {label} Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",    value=tab_name, inline=True)
    embed.add_field(name="Teams",        value={"both": "A & B", "A": "A only", "B": "B only"}[teams], inline=True)
    embed.add_field(name="Timezone",     value=tz_label, inline=True)
    embed.add_field(name="Log Channel",  value=f"<#{log_channel_id}>", inline=True)
    if template_a:
        embed.add_field(name="Template A Preview",
                        value=f"```{template_a[:150]}{'...' if len(template_a) > 150 else ''}```",
                        inline=False)
    if template_b and template_b != template_a:
        embed.add_field(name="Template B Preview",
                        value=f"```{template_b[:150]}{'...' if len(template_b) > 150 else ''}```",
                        inline=False)
    embed.set_footer(text=f"Run /{cmd_name} again to update.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] {label} config saved for guild {guild_id}")

async def run_event_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring event types."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=120),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    async def ask_view(prompt: str, view: discord.ui.View):
        await channel.send(prompt, view=view)
        await view.wait()
        return view

    from config import get_config, get_guild_events, save_guild_event, get_or_create_config, update_config_field
    import re as _re

    guild_cfg = get_config(guild_id) or get_or_create_config(guild_id)
    timezone  = guild_cfg.timezone if guild_cfg.timezone else "America/New_York"
    tz_label  = TIMEZONE_LABELS.get(timezone, timezone)
    events    = get_guild_events(guild_id, active_only=True)

    draft_channel_id    = guild_cfg.event_draft_channel_id or 0
    announce_channel_id = guild_cfg.event_announce_channel_id or 0
    draft_time          = guild_cfg.event_draft_time or "12:00"
    five_min_warning    = guild_cfg.event_five_min_warning if guild_cfg.event_five_min_warning is not None else 1

    # ── If already configured, show summary with action options ───────────────
    if draft_channel_id and events:
        summary_embed = discord.Embed(
            title="📣 Event Setup",
            description="Your events are already configured. What would you like to do?",
            color=discord.Color.blurple(),
        )
        summary_embed.add_field(name="Draft Channel",        value=f"<#{draft_channel_id}>",    inline=False)
        summary_embed.add_field(name="Announcement Channel", value=f"<#{announce_channel_id}>", inline=False)
        summary_embed.add_field(name="Draft Time",           value=draft_time,                  inline=False)
        summary_embed.add_field(name="5-min Warning",        value="Yes" if five_min_warning else "No", inline=False)
        ev_list = "\n".join(f"• **{e['name']}** — {e['default_time']} {tz_label}" for e in events)
        summary_embed.add_field(name="Events", value=ev_list, inline=False)

        class EventActionView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.choice = None

            @discord.ui.button(label="⚙️ Edit Event Settings", style=discord.ButtonStyle.primary, row=0)
            async def edit_settings(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "settings"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="➕ Add Event", style=discord.ButtonStyle.success, row=0)
            async def add_event(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "add"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✏️ Edit Event", style=discord.ButtonStyle.secondary, row=1)
            async def edit_event(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "edit"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="🗑️ Delete Event", style=discord.ButtonStyle.danger, row=1)
            async def delete_event(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "delete"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✅ No changes needed", style=discord.ButtonStyle.secondary, row=2)
            async def done(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "done"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        action_view = EventActionView()
        await channel.send(embed=summary_embed, view=action_view)
        await action_view.wait()

        if not action_view.choice or action_view.choice == "done":
            await channel.send("✅ No changes made.")
            return

        # Jump straight to event list for add/edit/delete
        # We already have all the settings values — skip the settings wizard
        # and fall through directly to the event list below
        if action_view.choice in ("add", "edit", "delete"):
            pass  # fall through to event list at end of function

        # Fall through to full settings wizard for "settings"
        elif action_view.choice == "settings":
            await channel.send("⚙️ Let's update your event settings...")

        skip_settings = action_view.choice in ("add", "edit", "delete")
    else:
        skip_settings = False

    if not skip_settings:
        await channel.send(
            "⚙️ **Event Setup**\n"
            "Configure your alliance events. All events share the same draft channel, "
            "announcement channel, draft time, and 5-minute warning setting."
        )

    # ── Steps 1-4: Channel/time settings (skipped if coming from action menu) ──
    if not skip_settings:
        current_draft_id = guild_cfg.event_draft_channel_id or 0
        draft_ch_view    = ChannelSelectStep("Select the draft channel...", suggested_name="event-drafts")
        await channel.send(
            "**Step 1 of 5 — Draft Channel**\n"
            "Which channel should the bot post event announcement drafts for leadership to review?\n"
            "*(This applies to all events)*",
            view=draft_ch_view,
        )
        await draft_ch_view.wait()
        if not draft_ch_view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return
        draft_channel_id = draft_ch_view.selected_channel.id

        current_ann_id = guild_cfg.event_announce_channel_id or 0
        ann_ch_view    = ChannelSelectStep("Select the announcement channel...", suggested_name="announcements")
        await channel.send(
            "**Step 2 of 5 — Announcement Channel**\n"
            "Which channel should approved announcements be posted to?\n"
            "*(This applies to all events)*",
            view=ann_ch_view,
        )
        await ann_ch_view.wait()
        if not ann_ch_view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return
        announce_channel_id = ann_ch_view.selected_channel.id

        tz_label       = TIMEZONE_LABELS.get(timezone, timezone)
        draft_time_raw = await ask_text(
            f"**Step 3 of 5 — Draft Posting Time**\n"
            f"What time should the bot post the draft each event day? *(in {tz_label})*\n"
            f"*(e.g. `12:00pm` for noon)*"
        )
        if not draft_time_raw:
            return
        draft_time = _parse_12h_time(draft_time_raw)
        if not draft_time:
            await channel.send("⚠️ Could not read that time. Try `12:00pm`. Run `/setup_events` to try again.")
            return

        warn_view = YesNoView()
        await channel.send(
            "**Step 4 of 5 — 5-Minute Warning**\n"
            "Should the bot automatically post a 5-minute warning before events?\n"
            "*(This applies to all events)*",
            view=warn_view,
        )
        await warn_view.wait()
        if warn_view.selected is None:
            await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return
        five_min_warning = 1 if warn_view.selected else 0

        update_config_field(guild_id, "event_draft_channel_id",    draft_channel_id)
        update_config_field(guild_id, "event_announce_channel_id", announce_channel_id)
        update_config_field(guild_id, "event_draft_time",          draft_time)
        update_config_field(guild_id, "event_five_min_warning",    five_min_warning)

    # ── Event list ────────────────────────────────────────────────────────────
    events = get_guild_events(guild_id, active_only=False)

    async def build_event_list():
        """Show event list with Add/Edit/Delete/Finish controls."""
        nonlocal events

        while True:
            events = get_guild_events(guild_id, active_only=False)

            if events:
                event_display = "\n".join(
                    f"{i+1}. **{e['name']}** — "
                    f"{'🔁 ' + str(e['interval_days']) + '-day cycle' if e['schedule_type'] == 'repeating' else '📅 Manual'} "
                    f"at {e['default_time']}"
                    + (" *(inactive)*" if not e['active'] else "")
                    for i, e in enumerate(events)
                )
            else:
                event_display = "*(no events configured yet)*"

            class EventListView(discord.ui.View):
                def __init__(self, event_list):
                    super().__init__(timeout=300)
                    self.action     = None
                    self.edit_key   = None
                    self.delete_key = None

                    if event_list:
                        edit_select = discord.ui.Select(
                            placeholder="✏️ Edit an event...",
                            options=[discord.SelectOption(
                                label=f"Edit: {e['name']}", value=e['short_key']
                            ) for e in event_list],
                            row=0,
                        )
                        async def _edit_cb(inter: discord.Interaction):
                            self.action   = "edit"
                            self.edit_key = edit_select.values[0]
                            for item in self.children: item.disabled = True
                            await inter.response.edit_message(view=self)
                            self.stop()
                        edit_select.callback = _edit_cb
                        self.add_item(edit_select)

                        del_select = discord.ui.Select(
                            placeholder="🗑️ Delete an event...",
                            options=[discord.SelectOption(
                                label=f"Delete: {e['name']}", value=e['short_key']
                            ) for e in event_list],
                            row=1,
                        )
                        async def _del_cb(inter: discord.Interaction):
                            self.action     = "delete"
                            self.delete_key = del_select.values[0]
                            for item in self.children: item.disabled = True
                            await inter.response.edit_message(view=self)
                            self.stop()
                        del_select.callback = _del_cb
                        self.add_item(del_select)

                @discord.ui.button(label="➕ Add Event", style=discord.ButtonStyle.primary, row=2)
                async def add_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                    self.action = "add"
                    for item in self.children: item.disabled = True
                    await inter.response.edit_message(view=self)
                    self.stop()

                @discord.ui.button(label="✅ Finish", style=discord.ButtonStyle.success, row=2)
                async def finish_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                    self.action = "finish"
                    for item in self.children: item.disabled = True
                    await inter.response.edit_message(view=self)
                    self.stop()

            list_view = EventListView(events)
            await channel.send(
                f"**Step 5 of 5 — Your Events:**\n{event_display}",
                view=list_view,
            )
            await list_view.wait()

            if not list_view.action:
                await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
                return False

            if list_view.action == "finish":
                return True

            elif list_view.action == "delete":
                from config import delete_guild_event, get_guild_event
                ev = get_guild_event(guild_id, list_view.delete_key)
                delete_guild_event(guild_id, list_view.delete_key)
                await channel.send(f"🗑️ Removed: **{ev['name'] if ev else list_view.delete_key}**")

            elif list_view.action in ("add", "edit"):
                existing = None
                if list_view.action == "edit":
                    from config import get_guild_event
                    existing = get_guild_event(guild_id, list_view.edit_key)

                # ── Event builder ──────────────────────────────────────────────
                # Name
                name_raw = await ask_text(
                    "**Event Name**\n"
                    "What is this event called? (e.g. `Plague Marauder (AE)`, `Zombie Siege`)"
                    + (f"\n*(Current: `{existing['name']}`)*" if existing else "")
                )
                if not name_raw:
                    return False
                name      = name_raw.strip() or (existing['name'] if existing else "")
                short_key = existing['short_key'] if existing else _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

                # Time
                cur_time  = existing['default_time'] if existing else ""
                time_raw  = await ask_text(
                    f"**{name} — Event Time**\n"
                    f"What time does this event usually start? *(in {tz_label})*\n"
                    f"*(e.g. `10:15pm`, `9:00am`)*"
                    + (f"\n*(Current: `{cur_time}`)*" if cur_time else "")
                )
                if not time_raw:
                    return False
                default_time = _parse_12h_time(time_raw)
                if not default_time:
                    await channel.send("⚠️ Could not read that time. Try `10:15pm`. Run `/setup_events` to try again.")
                    return False

                # Schedule
                sched_view = ScheduleTypeView()
                await channel.send(
                    f"**{name} — Schedule**\n"
                    "Does this event repeat on a fixed cycle, or do you add it manually each time?",
                    view=sched_view,
                )
                await sched_view.wait()
                if not sched_view.selected:
                    await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
                    return False
                schedule_type = sched_view.selected

                anchor_date   = existing.get('anchor_date', '') if existing else ''
                interval_days = existing.get('interval_days', 3) if existing else 3

                if schedule_type == "repeating":
                    anchor_raw = await ask_text(
                        f"**{name} — Anchor Date**\n"
                        "Enter a recent or upcoming date when this event occurs.\n"
                        "Type the month and day (e.g. `March 30`, `April 14`)"
                        + (f"\n*(Current: `{anchor_date}`)*" if anchor_date else "")
                    )
                    if not anchor_raw:
                        return False
                    parsed_anchor = _parse_month_day(anchor_raw)
                    if not parsed_anchor:
                        await channel.send("⚠️ Could not read that date. Try `March 30`. Run `/setup_events` to try again.")
                        return False
                    anchor_date = parsed_anchor

                    interval_raw = await ask_text(
                        f"**{name} — Cycle Interval**\n"
                        "How many days between each occurrence? (e.g. `3`)"
                        + (f"\n*(Current: `{interval_days}`)*" if interval_days else "")
                    )
                    if not interval_raw:
                        return False
                    try:
                        interval_days = int(interval_raw)
                    except ValueError:
                        await channel.send("⚠️ Please enter a whole number. Run `/setup_events` to try again.")
                        return False

                # Blurb
                cur_blurb    = existing.get('announcement_blurb', '') if existing else ''
                default_blurb = f"{name} at {{time}} ({{server_time}} Server Time)."

                class BlurbChoiceView(discord.ui.View):
                    def __init__(self, has_existing: bool):
                        super().__init__(timeout=120)
                        self.choice = None

                    @discord.ui.button(label="✅ Use default blurb", style=discord.ButtonStyle.success)
                    async def use_default(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.choice = "default"
                        for item in self.children: item.disabled = True
                        await inter.response.edit_message(
                            content=f"✅ Using default blurb:\n`{default_blurb}`", view=self
                        )
                        self.stop()

                    @discord.ui.button(label="✏️ Enter my own", style=discord.ButtonStyle.secondary)
                    async def enter_own(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.choice = "custom"
                        for item in self.children: item.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()

                blurb_view = BlurbChoiceView(has_existing=bool(cur_blurb))
                if cur_blurb:
                    keep_btn = discord.ui.Button(
                        label="⏭️ Keep existing", style=discord.ButtonStyle.secondary, row=1
                    )
                    async def _keep_cb(inter: discord.Interaction):
                        blurb_view.choice = "keep"
                        for item in blurb_view.children: item.disabled = True
                        await inter.response.edit_message(content="✅ Keeping existing blurb.", view=blurb_view)
                        blurb_view.stop()
                    keep_btn.callback = _keep_cb
                    blurb_view.add_item(keep_btn)

                blurb_msg = (
                    f"**{name} — Announcement Blurb**\n"
                    "This message gets posted when this event fires.\n"
                    "Use `{time}` for the event time in your timezone and `{server_time}` for Server Time.\n\n"
                    f"**Default:** `{default_blurb}`"
                )
                if cur_blurb:
                    blurb_msg += f"\n**Current:** `{cur_blurb[:100]}{'...' if len(cur_blurb) > 100 else ''}`"

                await channel.send(blurb_msg, view=blurb_view)
                await blurb_view.wait()
                if not blurb_view.choice:
                    await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
                    return False

                if blurb_view.choice == "default":
                    blurb = default_blurb
                elif blurb_view.choice == "keep":
                    blurb = cur_blurb
                else:
                    blurb_raw = await ask_text(
                        "Enter your announcement blurb:\n"
                        "*(Use `{time}` and `{server_time}` as placeholders)*",
                        max_chars=1000,
                    )
                    if blurb_raw is None:
                        return False
                    blurb = blurb_raw.strip() or default_blurb

                # Save event
                event = {
                    "short_key":               short_key,
                    "name":                    name,
                    "timezone":                timezone,
                    "default_time":            default_time,
                    "announcement_blurb":      blurb,
                    "schedule_type":           schedule_type,
                    "anchor_date":             anchor_date,
                    "interval_days":           interval_days,
                    "draft_channel_id":        draft_channel_id,
                    "announcement_channel_id": announce_channel_id,
                    "draft_time":              draft_time,
                    "five_min_warning":        five_min_warning,
                    "active":                  1,
                }
                save_guild_event(guild_id, event)
                action_word = "Updated" if existing else "Added"
                await channel.send(f"✅ {action_word}: **{name}**")

    result = await build_event_list()
    if not result:
        return

    # ── Summary ────────────────────────────────────────────────────────────────
    events   = get_guild_events(guild_id, active_only=True)
    tz_label = TIMEZONE_LABELS.get(timezone, timezone)

    embed = discord.Embed(title="✅ Events Configured", color=discord.Color.green())
    embed.add_field(name="Draft Channel",        value=f"<#{draft_channel_id}>",    inline=False)
    embed.add_field(name="Announcement Channel", value=f"<#{announce_channel_id}>", inline=False)
    embed.add_field(name="Draft Time",           value=draft_time,                  inline=False)
    embed.add_field(name="5-min Warning",        value="Yes" if five_min_warning else "No", inline=False)
    if events:
        ev_list = "\n".join(f"• **{e['name']}** — {e['default_time']} {tz_label}" for e in events)
        embed.add_field(name="Events", value=ev_list, inline=False)
    embed.set_footer(text="Run /setup_events again to add or edit events.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Events saved for guild {guild_id}")

async def run_birthday_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring birthday tracking."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=120),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    def col_letter_to_index(raw: str) -> int:
        """Convert 'A' or 'a' to 0, 'B' to 1, etc. Returns -1 if invalid."""
        raw = raw.strip().upper()
        if len(raw) == 1 and raw.isalpha():
            return ord(raw) - ord('A')
        return -1

    def index_to_letter(idx: int) -> str:
        return chr(ord('A') + idx) if idx >= 0 else "—"

    from config import get_birthday_config
    current = get_birthday_config(guild_id)

    await channel.send(
        "⚙️ **Birthday Tracking Setup**\n"
        "Configure how the bot tracks member birthdays."
    )

    # ── Step 1: Enable? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        "**Step 1 of 9 — Enable birthday tracking?**\n"
        "Should the bot track member birthdays from your Google Sheet?",
        view=enabled_view,
    )
    await enabled_view.wait()
    if enabled_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
        return
    if not enabled_view.selected:
        from config import save_birthday_config
        save_birthday_config(guild_id, enabled=0, **{k: v for k, v in current.items() if k not in ('guild_id', 'enabled')})
        await channel.send("✅ Birthday tracking disabled.")
        return

    # ── Step 2: Sheet tab ─────────────────────────────────────────────────────
    current_tab = current.get("tab_name") or "Birthdays"

    class TabConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.tab_name  = None
            self.confirmed = False

        @discord.ui.button(label="✅ Keep current tab", style=discord.ButtonStyle.success)
        async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
            self.tab_name  = current_tab
            self.confirmed = True
            for item in self.children: item.disabled = True
            await inter.response.edit_message(
                content=f"✅ Using sheet tab: **{current_tab}**", view=self
            )
            self.stop()

        @discord.ui.button(label="✏️ Enter a different tab name", style=discord.ButtonStyle.secondary)
        async def change(self, inter: discord.Interaction, button: discord.ui.Button):
            modal = TextInputModal("Sheet Tab Name", "Tab name", default=current_tab)
            await inter.response.send_modal(modal)
            await modal.wait()
            self.tab_name  = modal.value or current_tab
            self.confirmed = True
            for item in self.children: item.disabled = True
            try:
                await inter.message.edit(
                    content=f"✅ Using sheet tab: **{self.tab_name}**", view=self
                )
            except discord.HTTPException:
                pass
            self.stop()

    tab_view = TabConfirmView()
    await channel.send(
        f"**Step 2 of 9 — Sheet Tab**\n"
        f"Which tab in your Google Sheet contains birthday data?\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*\n"
        f"*(Current: `{current_tab}`)*",
        view=tab_view,
    )
    await tab_view.wait()
    if not tab_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
        return
    tab_name = tab_view.tab_name

    # ── Step 3: Name column ────────────────────────────────────────────────────
    cur_name_letter = index_to_letter(current.get("name_col", 0))
    name_col_raw = await ask_text(
        f"**Step 3 of 9 — Name Column**\n"
        f"Which column contains the member's name?\n"
        f"Type the column letter (e.g. `A`, `B`, `C`)\n"
        f"*(Current: column `{cur_name_letter}`)*"
    )
    if name_col_raw is None:
        return
    name_col = col_letter_to_index(name_col_raw)
    if name_col < 0:
        await channel.send("⚠️ Please enter a single column letter like `A`. Run `/setup_birthdays` to try again.")
        return

    # ── Step 4: Birthday column ────────────────────────────────────────────────
    cur_bday_letter = index_to_letter(current.get("birthday_col", 1))
    bday_col_raw = await ask_text(
        f"**Step 4 of 9 — Birthday Column**\n"
        f"Which column contains the member's birthday?\n"
        f"Type the column letter (e.g. `A`, `B`, `C`)\n"
        f"*(Current: column `{cur_bday_letter}`)*"
    )
    if bday_col_raw is None:
        return
    birthday_col = col_letter_to_index(bday_col_raw)
    if birthday_col < 0:
        await channel.send("⚠️ Please enter a single column letter like `B`. Run `/setup_birthdays` to try again.")
        return

    # ── Step 5: Train integration ─────────────────────────────────────────────
    train_view = YesNoView()
    await channel.send(
        "**Step 5 — Train Schedule Integration**\n"
        "Should the bot automatically add members to the train schedule on their birthday?",
        view=train_view,
    )
    await train_view.wait()
    if train_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
        return
    train_integration = 1 if train_view.selected else 0

    flexible_placement = 0
    lookahead_days     = 14

    if train_integration:
        # ── Step 6: Flexible placement ─────────────────────────────────────────
        class PlacementView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected = None

            @discord.ui.button(label="🎂 Birthday only", style=discord.ButtonStyle.primary)
            async def birthday_only(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 0
                for item in self.children: item.disabled = True
                await inter.response.edit_message(content="✅ Placement: **Birthday only**", view=self)
                self.stop()

            @discord.ui.button(label="📅 Assign nearby if taken", style=discord.ButtonStyle.secondary)
            async def flexible(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 1
                for item in self.children: item.disabled = True
                await inter.response.edit_message(content="✅ Placement: **Assign 1 day before or after if birthday is taken**", view=self)
                self.stop()

        placement_view = PlacementView()
        await channel.send(
            "**Step 7 of 9 — Birthday Placement**\n"
            "If the member's birthday is already taken on the train schedule, what should the bot do?",
            view=placement_view,
        )
        await placement_view.wait()
        if placement_view.selected is None:
            await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
            return
        flexible_placement = placement_view.selected

        # ── Step 7: Lookahead days ─────────────────────────────────────────────
        lookahead_raw = await ask_text(
            "**Step 8 of 9 — Lookahead Days**\n"
            "How many days in advance should birthdays be added to the train schedule?\n"
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

    # ── Step 8: Birthday reminders ─────────────────────────────────────────────
    remind_view = YesNoView()
    await channel.send(
        "**Step 9 of 9 — Birthday Reminders**\n"
        "Should the bot post a message in Discord on a member's birthday?\n"
        f"*(It will post: \"🎂 Today is **[name]**'s birthday!\")*",
        view=remind_view,
    )
    await remind_view.wait()
    if remind_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
        return
    reminders_enabled    = 1 if remind_view.selected else 0
    reminder_channel_id  = 0
    reminder_time        = "08:00"

    if reminders_enabled:
        # ── Step 8a: Reminder channel ──────────────────────────────────────────
        remind_ch_view = ChannelSelectStep(
            "Select the birthday announcement channel...",
            suggested_name="birthdays",
        )
        await channel.send(
            "**Step 9a of 9 — Birthday Announcement Channel**\n"
            "Which channel should birthday announcements be posted in?",
            view=remind_ch_view,
        )
        await remind_ch_view.wait()
        if not remind_ch_view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
            return
        reminder_channel_id = remind_ch_view.selected_channel.id

        # ── Step 8b: Reminder time ─────────────────────────────────────────────
        from config import get_config
        guild_cfg = get_config(guild_id)
        tz_label  = TIMEZONE_LABELS.get(guild_cfg.timezone if guild_cfg else "America/New_York", "your timezone")
        time_raw  = await ask_text(
            f"**Step 9b of 9 — Reminder Time**\n"
            f"What time should birthday announcements be posted? *(in {tz_label})*\n"
            f"*(e.g. `8:00am`, `12:00pm`)*"
        )
        if time_raw is None:
            return
        parsed = _parse_12h_time(time_raw)
        if not parsed:
            await channel.send("⚠️ Could not read that time. Using `8:00am` as default.")
            parsed = "08:00"
        reminder_time = parsed

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_birthday_config
    save_birthday_config(
        guild_id        = guild_id,
        tab_name        = tab_name,
        name_col        = name_col,
        birthday_col    = birthday_col,
        discord_id_col  = discord_id_col,
        data_start_row  = 2,
        enabled         = 1,
        train_integration   = train_integration,
        flexible_placement  = flexible_placement,
        lookahead_days      = lookahead_days,
        reminders_enabled   = reminders_enabled,
        reminder_channel_id = reminder_channel_id,
        reminder_time       = reminder_time,
    )

    embed = discord.Embed(title="✅ Birthday Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",           value=tab_name,                            inline=True)
    embed.add_field(name="Name Column",         value=f"Column {index_to_letter(name_col)}",     inline=True)
    embed.add_field(name="Birthday Column",     value=f"Column {index_to_letter(birthday_col)}", inline=True)
    embed.add_field(name="Discord ID Column",   value=f"Column {index_to_letter(discord_id_col)}" if discord_id_col >= 0 else "Not stored", inline=True)
    embed.add_field(name="Train Integration",   value="Enabled" if train_integration else "Disabled", inline=True)
    if train_integration:
        embed.add_field(name="Placement",       value="Flexible (±1 day)" if flexible_placement else "Birthday only", inline=True)
        embed.add_field(name="Lookahead",       value=f"{lookahead_days} days",           inline=True)
    embed.add_field(name="Reminders",           value="Enabled" if reminders_enabled else "Disabled", inline=True)
    if reminders_enabled:
        embed.add_field(name="Reminder Channel", value=f"<#{reminder_channel_id}>",       inline=True)
        embed.add_field(name="Reminder Time",    value=reminder_time,                     inline=True)
    embed.set_footer(text="Run /setup_birthdays again to update these settings.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Birthday config saved for guild {guild_id}")

async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

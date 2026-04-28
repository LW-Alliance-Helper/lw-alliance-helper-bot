"""
storm.py — Desert Storm mail generation

Commands (OGV Leadership only, leadership channel only):
  /desertstorm_draft — Generate a Desert Storm mail draft for Team A or Team B
  /canyonstorm_draft — Generate a Canyon Storm mail draft for Team A or Team B

Flow:
  1. Bot asks Team A or Team B
  2. Bot posts last saved assignments for that team as an editable template
  3. Leadership copies, edits, and pastes back
  4. Bot asks for the time (4PM or 9PM)
  5. Bot builds the full mail and posts a preview for approval
  6. On approval, saves the new assignments as next week's default for that team

Assignments are persisted in the DS Assignments tab of the Google Sheet.
Sheet structure:
  Section headers in col A: DS_A_ZONES, DS_A_SUBS, DS_B_ZONES, DS_B_SUBS
  Data rows follow each header (col A = zone/name, col B = members/sub)
"""

import asyncio
import json
import os
import discord
from discord import app_commands
from discord.ext import commands
from config import get_config
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")



WIZARD_TIMEOUT = 600  # 10 minutes


# ── Default assignments ────────────────────────────────────────────────────────

# Team A — starts empty, leadership fills on first use
DEFAULT_A_ZONES = {
    "Nuclear Silo":       "Member Name",
    "Oil Refinery I":     "Member Name",
    "Oil Refinery II":    "Member Name",
    "Science Hub":        "Member Name",
    "Info Center":        "Member Name",
    "Field Hospital I":   "Member Name",
    "Field Hospital II":  "Member Name",
    "Field Hospital III": "Member Name",
    "Field Hospital IV":  "Member Name",
    "Arsenal":            "Member Name",
    "Mercenary Factory":  "Member Name",
}
DEFAULT_A_SUBS = []

# Team B — starts with placeholders
DEFAULT_B_ZONES = {
    "Nuclear Silo":       "Member Name",
    "Oil Refinery I":     "Member Name",
    "Oil Refinery II":    "Member Name",
    "Science Hub":        "Member Name",
    "Info Center":        "Member Name",
    "Field Hospital I":   "Member Name",
    "Field Hospital II":  "Member Name",
    "Field Hospital III": "Member Name",
    "Field Hospital IV":  "Member Name",
    "Arsenal":            "Member Name",
    "Mercenary Factory":  "Member Name",
}
DEFAULT_B_SUBS = []
DEFAULTS = {
    "A": (DEFAULT_A_ZONES, DEFAULT_A_SUBS),
    "B": (DEFAULT_B_ZONES, DEFAULT_B_SUBS),
}



# ── Google Sheets persistence ──────────────────────────────────────────────────

def _get_spreadsheet(guild_id: int = None):
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        info  = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds    = Credentials.from_service_account_file(key_file, scopes=scopes)

    gc = gspread.authorize(creds)
    from config import get_spreadsheet_id
    sheet_id = get_spreadsheet_id(guild_id)
    return gc.open_by_key(sheet_id)


def load_ds_assignments(team: str) -> tuple[dict, list]:
    """
    Load saved DS assignments for the given team ("A" or "B").
    Falls back to defaults if nothing is saved yet.
    """
    zone_key = f"DS_{team}_ZONES"
    sub_key  = f"DS_{team}_SUBS"

    try:
        sh   = _get_spreadsheet()
        ws   = sh.worksheet(cfg.tab_ds_assignments if cfg else "DS Assignments")
        rows = ws.get_all_values()

        zones   = {}
        subs    = []
        section = None

        for row in rows:
            if not row or not row[0].strip():
                continue
            key = row[0].strip()

            if key == zone_key:
                section = "zones"
                continue
            if key == sub_key:
                section = "subs"
                continue
            # Stop reading this team's section when hitting another team's header
            if key.startswith("DS_") and key not in (zone_key, sub_key):
                section = None
                continue

            if section == "zones" and len(row) >= 2:
                zones[key] = row[1].strip()
            elif section == "subs" and len(row) >= 2:
                subs.append((row[0].strip(), row[1].strip()))

        if zones:
            print(f"[STORM] Loaded Team {team} assignments ({len(zones)} zones, {len(subs)} sub pairs)")
            return zones, subs
        else:
            print(f"[STORM] No saved Team {team} assignments — using defaults")
            default_zones, default_subs = DEFAULTS[team]
            return dict(default_zones), list(default_subs)

    except Exception as e:
        print(f"[STORM] Error loading Team {team} assignments: {e}")
        default_zones, default_subs = DEFAULTS[team]
        return dict(default_zones), list(default_subs)


def save_ds_assignments(team: str, zones: dict, subs: list):
    """
    Save DS assignments for one team without affecting the other team's data.
    Reads the full sheet, replaces this team's sections, and rewrites.
    """
    zone_key = f"DS_{team}_ZONES"
    sub_key  = f"DS_{team}_SUBS"
    other    = "B" if team == "A" else "A"
    other_zone_key = f"DS_{other}_ZONES"
    other_sub_key  = f"DS_{other}_SUBS"

    try:
        sh = _get_spreadsheet()
        ws = sh.worksheet(cfg.tab_ds_assignments if cfg else "DS Assignments")

        # Load the other team's current data so we don't lose it
        other_zones, other_subs = load_ds_assignments(other)

        # Rebuild the full sheet with both teams
        rows = []

        # Team A first, then Team B — alphabetical for consistency
        for t, t_zones, t_subs in [
            ("A", zones if team == "A" else other_zones,
                  subs  if team == "A" else other_subs),
            ("B", zones if team == "B" else other_zones,
                  subs  if team == "B" else other_subs),
        ]:
            rows.append([f"DS_{t}_ZONES", ""])
            for zone, members in t_zones.items():
                rows.append([zone, members])
            rows.append(["", ""])
            rows.append([f"DS_{t}_SUBS", ""])
            for starter, sub in t_subs:
                rows.append([starter, sub])
            rows.append(["", ""])  # blank separator between teams

        ws.clear()
        ws.update("A1", rows, value_input_option="USER_ENTERED")
        print(f"[STORM] Team {team} assignments saved ({len(zones)} zones, {len(subs)} sub pairs)")

    except Exception as e:
        print(f"[STORM] Error saving Team {team} assignments: {e}")


# ── Template builder & parser ──────────────────────────────────────────────────

def build_ds_template(zones: dict, subs: list) -> str:
    lines = ["ZONE ASSIGNMENTS"]
    for zone, members in zones.items():
        lines.append(f"{zone}: {members}")
    lines.append("")
    lines.append("SUB PAIRS (Starter - Sub)")
    for starter, sub in subs:
        lines.append(f"{starter} - {sub}")
    return "\n".join(lines)


def parse_ds_template(text: str) -> tuple[dict, list, list]:
    """Parse the edited template. Returns (zones, subs, errors)."""
    zones   = {}
    subs    = []
    errors  = []
    section = None

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper() == "ZONE ASSIGNMENTS":
            section = "zones"
            continue
        if line.upper().startswith("SUB PAIRS"):
            section = "subs"
            continue

        if section == "zones":
            if ":" in line:
                zone, _, members = line.partition(":")
                zones[zone.strip()] = members.strip()
            else:
                errors.append(f"Could not parse zone line: {line}")
        elif section == "subs":
            if " - " in line:
                parts = line.split(" - ", 1)
                subs.append((parts[0].strip(), parts[1].strip()))
            else:
                errors.append(f"Could not parse sub pair: {line}")

    return zones, subs, errors


# ── Mail builder ───────────────────────────────────────────────────────────────

def build_ds_mail(team: str, zones: dict, subs: list, time_key: str,
                  guild_id: int = None, template_name: str | None = None) -> str:
    """Build DS mail using a guild's stored template (named or default)."""
    from config import get_storm_config, get_storm_template
    cfg       = get_storm_config(guild_id, "DS") if guild_id else {}
    if guild_id:
        template = get_storm_template(guild_id, "DS", template_name)
    else:
        template = cfg.get("mail_template") or ""

    # Build time string from config time options
    if time_key == "1":
        local_time  = cfg.get("time_option_1_local", "")
        server_time = cfg.get("time_option_1_server", "")
    else:
        local_time  = cfg.get("time_option_2_local", "")
        server_time = cfg.get("time_option_2_server", "")
    time_str = f"{local_time} ({server_time})" if local_time else time_key

    zone_lines = []
    for zone, members in zones.items():
        zone_lines.append(f"**{zone}**")
        if isinstance(members, list):
            zone_lines.append("\n".join(str(m) for m in members))
        else:
            zone_lines.append(str(members))
        zone_lines.append("")
    zones_block = "\n".join(zone_lines).strip()

    if isinstance(subs, list) and subs:
        subs_block = "\n".join(str(s) for s in subs)
    elif subs:
        subs_block = str(subs)
    else:
        subs_block = "(none)"

    if template:
        return template.format(
            alliance_name="Alliance",
            zones=zones_block,
            subs=subs_block,
            time=time_str,
        )

    # Fallback plain format
    return "\n".join([
        "**Desert Storm**",
        "",
        "**Zone Assignments**",
        zones_block,
        "",
        "**Sub Pairs**",
        subs_block,
        "",
        f"**Time:** {time_str}",
    ])


# ── UI Views ───────────────────────────────────────────────────────────────────

class TeamSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None

    @discord.ui.button(label="Team A", style=discord.ButtonStyle.primary)
    async def pick_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "A"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Team B", style=discord.ButtonStyle.success)
    async def pick_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "B"
        await interaction.response.defer()
        self.stop()


class TimeSelectView(discord.ui.View):
    """Dynamic time select — buttons built from guild storm config."""
    def __init__(self, event_type: str = "DS", guild_id: int = None):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None
        from config import get_storm_config
        cfg = get_storm_config(guild_id, event_type) if guild_id else {}
        t1_label = cfg.get("time_option_1_label") or "Option 1"
        t2_label = cfg.get("time_option_2_label") or "Option 2"
        t1_local = cfg.get("time_option_1_local", "")
        t1_server = cfg.get("time_option_1_server", "")
        t2_local = cfg.get("time_option_2_local", "")
        t2_server = cfg.get("time_option_2_server", "")
        btn1_label = f"{t1_label}: {t1_local} ({t1_server})" if t1_local else t1_label
        btn2_label = f"{t2_label}: {t2_local} ({t2_server})" if t2_local else t2_label

        b1 = discord.ui.Button(label=btn1_label[:80], style=discord.ButtonStyle.secondary)
        b2 = discord.ui.Button(label=btn2_label[:80], style=discord.ButtonStyle.secondary)

        async def pick_1(interaction: discord.Interaction):
            self.selected = "1"
            await interaction.response.defer()
            self.stop()
        async def pick_2(interaction: discord.Interaction):
            self.selected = "2"
            await interaction.response.defer()
            self.stop()

        b1.callback = pick_1
        b2.callback = pick_2
        self.add_item(b1)
        self.add_item(b2)


class StormApprovalView(discord.ui.View):
    def __init__(self, bot, team: str, mail: str, zones: dict, subs: list, time_key: str):
        super().__init__(timeout=3600)
        self.bot      = bot
        self.team     = team
        self.mail     = mail
        self.zones    = zones
        self.subs     = subs
        self.time_key = time_key

    async def _disable(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="✅ Looks Good — Save & Copy", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        await asyncio.get_event_loop().run_in_executor(None, save_ds_assignments, self.team, self.zones, self.subs)
        channel = interaction.channel
        if channel:
            await channel.send(
                f"✅ **Desert Storm Team {self.team} mail — ready to copy:**\n"
                f"```\n{self.mail}\n```"
            )
        self.stop()

    @discord.ui.button(label="✏️ Edit & Redo", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        channel = interaction.channel
        if channel:
            template = build_ds_template(self.zones, self.subs)
            await channel.send(
                f"✏️ {interaction.user.mention} — copy and edit the block below, then paste it back:\n"
                f"```\n{template}\n```"
            )
            await run_ds_edit_step(
                self.bot, channel, interaction.user,
                self.team, self.zones, self.subs, self.time_key
            )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        await interaction.followup.send("❌ Draft cancelled.", ephemeral=True)
        self.stop()


# ── Core wizard flow ───────────────────────────────────────────────────────────

async def _pick_storm_template(bot, channel, guild_id: int | None, event_type: str):
    """
    For premium guilds with more than one saved storm mail template, prompt
    leadership to pick which template to use for this draft.

    Returns:
      * a template name string the caller should pass to build_*_mail, or
      * `None` to use the guild's default template, or
      * `False` if the picker timed out and the caller should bail.
    """
    if guild_id is None:
        return None
    import premium
    from config import get_storm_template_names
    if not await premium.is_premium(guild_id, bot=bot):
        return None
    names = get_storm_template_names(guild_id, event_type)
    if len(names) <= 1:
        return None

    class StormTemplatePickView(discord.ui.View):
        def __init__(self, options: list[str]):
            super().__init__(timeout=120)
            self.selected: str | None = None
            sel = discord.ui.Select(
                placeholder="Pick a saved template…",
                options=[discord.SelectOption(label=n[:100], value=n) for n in options],
            )
            async def _cb(inter):
                self.selected = sel.values[0]
                sel.disabled  = True
                await inter.response.edit_message(
                    content=f"✅ Template: **{self.selected}**", view=self,
                )
                self.stop()
            sel.callback = _cb
            self.add_item(sel)

    view = StormTemplatePickView(names)
    await channel.send(
        "💎 You have multiple saved templates. Pick one for this draft:",
        view=view,
    )
    await view.wait()
    if view.selected is None:
        await channel.send("⏰ Template picker timed out.")
        return False
    return view.selected


async def run_ds_edit_step(bot, channel, user, team: str, current_zones: dict,
                            current_subs: list, time_key: str = None):
    """Wait for edited template, parse it, optionally ask for time, then preview."""

    def check(m):
        return m.author == user and m.channel == channel

    # Ask for time if not already set
    if time_key is None:
        time_msg  = await channel.send("⏰ What time is Desert Storm this week?")
        time_view = TimeSelectView()
        await time_msg.edit(view=time_view)
        await time_view.wait()
        try:
            await time_msg.delete()
        except discord.HTTPException:
            pass
        if time_view.selected is None:
            await channel.send("⏰ Timed out. Use `/desertstorm_draft` to start again.")
            return
        time_key = time_view.selected

    # Wait for the pasted template
    prompt = await channel.send(
        f"📋 {user.mention} — paste your edited assignments below.\n"
        f"*(10 minutes to respond — type `cancel` to stop)*"
    )

    try:
        reply = await bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT)
    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out. Use `/desertstorm_draft` to start again.")
        try:
            await prompt.delete()
        except discord.HTTPException:
            pass
        return

    try:
        await prompt.delete()
        await reply.delete()
    except discord.HTTPException:
        pass

    if reply.content.strip().lower() == "cancel":
        await channel.send("❌ Draft cancelled.")
        return

    zones, subs, errors = parse_ds_template(reply.content)

    if not zones:
        await channel.send(
            "⚠️ Could not parse any zone assignments. "
            "Make sure the format matches the template and try `/desertstorm_draft` again."
        )
        return

    if errors:
        await channel.send(
            "⚠️ Some lines were skipped:\n" +
            "\n".join(f"• {e}" for e in errors)
        )

    # 💎 Premium: when multiple templates exist, ask which one to use.
    guild_id      = getattr(getattr(channel, "guild", None), "id", None)
    template_name = await _pick_storm_template(bot, channel, guild_id, "DS")
    if template_name is False:
        return  # picker timed out

    mail          = build_ds_mail(team, zones, subs, time_key,
                                  guild_id=guild_id, template_name=template_name)
    approval_view = StormApprovalView(
        bot=bot, team=team, mail=mail,
        zones=zones, subs=subs, time_key=time_key,
    )
    await channel.send(
        f"📬 **Desert Storm Team {team} mail preview:**\n\n{mail}\n\nDoes this look right?",
        view=approval_view,
    )


# ── Guards ─────────────────────────────────────────────────────────────────────

async def _guard(interaction: discord.Interaction) -> bool:
    cfg     = get_config(interaction.guild_id)
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        cat_id = cfg.leadership_category_id if cfg else 0
        in_channel = parent is not None and getattr(parent, "category_id", None) == cat_id
    else:
        cat_id = cfg.leadership_category_id if cfg else 0
        in_channel = getattr(channel, "category_id", None) == cat_id

    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(
            "⚙️ This bot hasn't been set up yet. Run `/setup` to get started.", ephemeral=True
        )
        return False
    if not in_channel:
        await interaction.response.send_message(
            "⛔ This command can only be used in the leadership channel.", ephemeral=True
        )
        return False
    if cfg.leadership_role_name not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.", ephemeral=True
        )
        return False
    return True


# ── Cog ────────────────────────────────────────────────────────────────────────

class StormCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="desertstorm_draft",
        description="Generate a Desert Storm mail draft for Team A or Team B",
    )
    async def desertstorm_draft(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("⚠️ Could not find the channel.", ephemeral=True)
            return

        # Step 1: Pick team
        team_msg  = await channel.send(
            f"🔥 **Desert Storm Draft** — started by {interaction.user.mention}\n\n"
            f"Which team are you drafting for?"
        )
        team_view = TeamSelectView()
        await team_msg.edit(view=team_view)
        await team_view.wait()
        try:
            await team_msg.delete()
        except discord.HTTPException:
            pass

        if team_view.selected is None:
            await channel.send("⏰ Timed out. Use `/desertstorm_draft` to start again.")
            await interaction.followup.send("⏰ Timed out.", ephemeral=True)
            return

        team = team_view.selected

        # Step 2: Load and post the template for that team (run in executor to avoid blocking)
        zones, subs = await asyncio.get_event_loop().run_in_executor(None, load_ds_assignments, team)
        template    = build_ds_template(zones, subs)

        await channel.send(
            f"🔥 **Desert Storm Team {team} Draft**\n\n"
            f"Copy the block below, make your changes, and paste it back. "
            f"Anything that hasn't changed can stay as-is.\n"
            f"```\n{template}\n```"
        )

        await interaction.followup.send(f"✅ Team {team} template posted.", ephemeral=True)

        # Step 3: Wait for edits, pick time, preview, approve
        await run_ds_edit_step(self.bot, channel, interaction.user, team, zones, subs)


    @app_commands.command(
        name="canyonstorm_draft",
        description="Generate a Canyon Storm mail draft for Team A or Team B",
    )
    async def canyonstorm_draft(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        if channel is None:
            await interaction.followup.send("⚠️ Could not find the channel.", ephemeral=True)
            return

        # Step 1: Pick team
        team_msg  = await channel.send(
            f"⚡ **Canyon Storm Draft** — started by {interaction.user.mention}\n\n"
            f"Which team are you drafting for?"
        )
        team_view = TeamSelectView()
        await team_msg.edit(view=team_view)
        await team_view.wait()
        try:
            await team_msg.delete()
        except discord.HTTPException:
            pass

        if team_view.selected is None:
            await channel.send("⏰ Timed out. Use `/canyonstorm_draft` to start again.")
            await interaction.followup.send("⏰ Timed out.", ephemeral=True)
            return

        team = team_view.selected

        # Step 2: Load and post the template
        zones    = await asyncio.get_event_loop().run_in_executor(None, load_cs_assignments, team)
        template = build_cs_template(zones)

        await channel.send(
            f"⚡ **Canyon Storm Team {team} Draft**\n\n"
            f"Copy the block below, make your changes, and paste it back. "
            f"Anything that hasn't changed can stay as-is.\n"
            f"```\n{template}\n```"
        )
        await interaction.followup.send(f"✅ Team {team} template posted.", ephemeral=True)

        # Step 3: Wait for edits, pick time, preview, approve
        await run_cs_edit_step(self.bot, channel, interaction.user, team, zones)


    @app_commands.command(
        name="desertstorm",
        description="Show the configured Desert Storm setup and current rosters",
    )
    async def desertstorm(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        await _show_storm_overview(interaction, "DS")

    @app_commands.command(
        name="canyonstorm",
        description="Show the configured Canyon Storm setup and current rosters",
    )
    async def canyonstorm(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        await _show_storm_overview(interaction, "CS")


async def _show_storm_overview(interaction: discord.Interaction, event_type: str):
    """Render the storm config + the current roster mail template for the given event type."""
    await interaction.response.defer(ephemeral=True)
    from config import get_storm_config, get_config

    label    = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    icon     = "⚔️" if event_type == "DS" else "🏜️"
    cmd_name = "desertstorm" if event_type == "DS" else "canyonstorm"
    setup_cmd = "setup_desertstorm" if event_type == "DS" else "setup_canyonstorm"

    cfg  = get_config(interaction.guild_id)
    scfg = get_storm_config(interaction.guild_id, event_type)
    log_channel_id = (cfg.ds_log_channel_id if event_type == "DS" else cfg.cs_log_channel_id) if cfg else 0

    embed = discord.Embed(
        title=f"{icon} {label}",
        color=discord.Color.dark_red() if event_type == "DS" else discord.Color.gold(),
    )
    embed.add_field(name="Sheet Tab",   value=scfg.get("tab_name", "*not set*"),                        inline=False)
    embed.add_field(name="Log Channel", value=f"<#{log_channel_id}>" if log_channel_id else "*not set*", inline=False)
    embed.add_field(
        name="Time Option 1",
        value=(
            f"{scfg.get('time_option_1_label') or '*not set*'} — "
            f"{scfg.get('time_option_1_local') or '?'} local / "
            f"{scfg.get('time_option_1_server') or '?'} server"
        ),
        inline=False,
    )
    embed.add_field(
        name="Time Option 2",
        value=(
            f"{scfg.get('time_option_2_label') or '*not set*'} — "
            f"{scfg.get('time_option_2_local') or '?'} local / "
            f"{scfg.get('time_option_2_server') or '?'} server"
        ),
        inline=False,
    )

    # Build the rendered mail template — same templating used by /[event]_draft
    try:
        if event_type == "DS":
            zones, subs = await asyncio.get_event_loop().run_in_executor(
                None, load_ds_assignments, "A"
            )
            template = build_ds_template(zones, subs)
        else:
            zones = await asyncio.get_event_loop().run_in_executor(
                None, load_cs_assignments, "A"
            )
            template = build_cs_template(zones)
        # Discord field value cap is 1024 chars
        preview = template[:1000] + ("\n…" if len(template) > 1000 else "")
        embed.add_field(name="Current Mail Template (Team A)", value=f"```\n{preview}\n```", inline=False)
    except Exception as e:
        embed.add_field(name="Current Mail Template", value=f"⚠️ Could not load: {e}", inline=False)

    embed.set_footer(text=f"Run /{setup_cmd} to update. Run /{cmd_name}_draft to generate a draft.")
    await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StormCog(bot))

# ══════════════════════════════════════════════════════════════════════════════
# CANYON STORM (CS)
# ══════════════════════════════════════════════════════════════════════════════



# ── CS Defaults ───────────────────────────────────────────────────────────────

DEFAULT_CS_B = {
    "s1_power_tower":    "Jon, Lionel, Ice, Sunshine",
    "s1_dc1":            "Gonza, Glick, Bobby",
    "s1_dc2":            "MG, Chuck, Kimberdog",
    "s1_sw1":            "Mer, Lito",
    "s1_sw2":            "Catie, Woozy",
    "s1_sw3":            "Dingo, Miss Goose",
    "s1_sw4":            "Anuedii, Drezy1",
    "s1_floaters":       "Toxic, Legit",
    "s2_ds1":            "Mer, Lito",
    "s2_ds2":            "Catie, Woozy",
    "s2_sf1":            "Dingo, Miss Goose",
    "s2_sf2":            "Anuedii, Drezy1",
    "s2_floaters":       "Toxic, Legit",
    "s3_virus_lab":      "Jon, Lionel, Ice, Sunshine",
    "s3_power_tower":    "MG, Gonza, Glick, Bobby",
    "s3_dc1":            "Toxic, Legit",
    "s3_dc2":            "Chuck, Kimberdog",
    "s3_ds1":            "Mer, Lito",
    "s3_ds2":            "Catie, Woozy",
    "s3_sf1":            "Dingo, Miss Goose",
    "s3_sf2":            "Anuedii, Drezy1",
    "s3_pop_pair1":      "Dingo & Mer and Aneudii & Legit",
}

DEFAULT_CS_A = {
    "s1_power_tower":    "Pink, TRC, Lunar, Blades",
    "s1_dc1":            "Corporal, Fosk, Monk",
    "s1_dc2":            "AD, Kale, Death",
    "s1_sw1":            "Loki, Loki BBG",
    "s1_sw2":            "DSP, Raven",
    "s1_sw3":            "Mrs. C, Landers",
    "s1_sw4":            "Joy, Chaos",
    "s1_floaters":       "Snacks, Arthur",
    "s2_ds1":            "Loki, Loki BBG",
    "s2_ds2":            "DSP, Raven",
    "s2_sf1":            "Mrs. C, Landers",
    "s2_sf2":            "Joy, Chaos",
    "s2_floaters":       "Snacks, Arthur",
    "s3_virus_lab":      "Pink, TRC, Lunar, Corporal",
    "s3_power_tower":    "Kale, AD, Blades, Fosk",
    "s3_dc1":            "Death, Monk",
    "s3_dc2":            "Snacks, Arthur",
    "s3_ds1":            "Loki, Loki BBG",
    "s3_ds2":            "DSP, Raven",
    "s3_sf1":            "Mrs. C, Landers",
    "s3_sf2":            "Joy, Chaos",
    "s3_pop_pair1":      "Arthur & Chaos and Raven & Loki",
}

CS_DEFAULTS = {"A": DEFAULT_CS_A, "B": DEFAULT_CS_B}

# ── CS Sheets persistence ──────────────────────────────────────────────────────

def load_cs_assignments(team: str, guild_id: int = None) -> dict:
    zone_key = f"CS_{team}_ZONES"
    try:
        from config import get_config
        cfg = get_config(guild_id)
        sh   = _get_spreadsheet(guild_id)
        ws   = sh.worksheet(cfg.tab_ds_assignments if cfg else "DS Assignments")
        rows = ws.get_all_values()
        zones   = {}
        section = None
        for row in rows:
            if not row or not row[0].strip():
                continue
            key = row[0].strip()
            if key == zone_key:
                section = "zones"
                continue
            if key.startswith("CS_") or key.startswith("DS_"):
                if key != zone_key:
                    section = None
                    continue
            if section == "zones" and len(row) >= 2:
                zones[key] = row[1].strip()
        if zones:
            print(f"[STORM] Loaded CS Team {team} assignments ({len(zones)} zones)")
            return zones
        else:
            print(f"[STORM] No saved CS Team {team} assignments — using defaults")
            return dict(CS_DEFAULTS[team])
    except Exception as e:
        print(f"[STORM] Error loading CS Team {team} assignments: {e}")
        return dict(CS_DEFAULTS[team])


def save_cs_assignments(team: str, zones: dict, guild_id: int = None):
    """Save CS assignments for one team without affecting DS or the other CS team."""
    try:
        sh = _get_spreadsheet(guild_id)
        ws = sh.worksheet(cfg.tab_ds_assignments if cfg else "DS Assignments")
        existing = ws.get_all_values()

        # Rebuild full sheet: preserve all DS and CS rows, replace this team's CS section
        other_cs = "B" if team == "A" else "A"
        other_cs_zones = load_cs_assignments(other_cs)
        ds_a_zones, ds_a_subs = load_ds_assignments("A")
        ds_b_zones, ds_b_subs = load_ds_assignments("B")

        rows = []
        for t, t_zones, t_subs in [("A", ds_a_zones, ds_a_subs), ("B", ds_b_zones, ds_b_subs)]:
            rows.append([f"DS_{t}_ZONES", ""])
            for z, m in t_zones.items():
                rows.append([z, m])
            rows.append(["", ""])
            rows.append([f"DS_{t}_SUBS", ""])
            for starter, sub in t_subs:
                rows.append([starter, sub])
            rows.append(["", ""])

        for t, t_zones in [("A", zones if team == "A" else other_cs_zones),
                            ("B", zones if team == "B" else other_cs_zones)]:
            rows.append([f"CS_{t}_ZONES", ""])
            for z, m in t_zones.items():
                rows.append([z, m])
            rows.append(["", ""])

        ws.clear()
        ws.update("A1", rows, value_input_option="USER_ENTERED")
        print(f"[STORM] CS Team {team} assignments saved ({len(zones)} zones)")
    except Exception as e:
        print(f"[STORM] Error saving CS Team {team} assignments: {e}")


# ── CS Template builder & parser ───────────────────────────────────────────────

def build_cs_template(z: dict) -> str:
    lines = [
        "STAGE 1",
        f"Power Tower: {z.get('s1_power_tower', '')}",
        f"Data Center 1: {z.get('s1_dc1', '')}",
        f"Data Center 2: {z.get('s1_dc2', '')}",
        f"Sample Warehouse 1: {z.get('s1_sw1', '')}",
        f"Sample Warehouse 2: {z.get('s1_sw2', '')}",
        f"Sample Warehouse 3: {z.get('s1_sw3', '')}",
        f"Sample Warehouse 4: {z.get('s1_sw4', '')}",
        f"Floaters: {z.get('s1_floaters', '')}",
        "",
        "STAGE 2",
        f"Defense System 1: {z.get('s2_ds1', '')}",
        f"Defense System 2: {z.get('s2_ds2', '')}",
        f"Serum Factory 1: {z.get('s2_sf1', '')}",
        f"Serum Factory 2: {z.get('s2_sf2', '')}",
        f"Floaters: {z.get('s2_floaters', '')}",
        "",
        "STAGE 3",
        f"Virus Lab: {z.get('s3_virus_lab', '')}",
        f"Power Tower: {z.get('s3_power_tower', '')}",
        f"Data Center 1: {z.get('s3_dc1', '')}",
        f"Data Center 2: {z.get('s3_dc2', '')}",
        f"Defense System 1: {z.get('s3_ds1', '')}",
        f"Defense System 2: {z.get('s3_ds2', '')}",
        f"Serum Factory 1: {z.get('s3_sf1', '')}",
        f"Serum Factory 2: {z.get('s3_sf2', '')}",
        f"Pop Pairs (last 30 sec): {z.get('s3_pop_pair1', '')}",
    ]
    return "\n".join(lines)


def parse_cs_template(text: str) -> tuple[dict, list]:
    zones  = {}
    errors = []
    stage  = None
    key_map = {
        "power tower":        {1: "s1_power_tower", 3: "s3_power_tower"},
        "data center 1":      {1: "s1_dc1",         3: "s3_dc1"},
        "data center 2":      {1: "s1_dc2",         3: "s3_dc2"},
        "sample warehouse 1": {1: "s1_sw1"},
        "sample warehouse 2": {1: "s1_sw2"},
        "sample warehouse 3": {1: "s1_sw3"},
        "sample warehouse 4": {1: "s1_sw4"},
        "floaters":           {1: "s1_floaters",    2: "s2_floaters"},
        "defense system 1":   {2: "s2_ds1",         3: "s3_ds1"},
        "defense system 2":   {2: "s2_ds2",         3: "s3_ds2"},
        "serum factory 1":    {2: "s2_sf1",         3: "s3_sf1"},
        "serum factory 2":    {2: "s2_sf2",         3: "s3_sf2"},
        "virus lab":          {3: "s3_virus_lab"},
        "pop pairs (last 30 sec)": {3: "s3_pop_pair1"},
    }
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper() == "STAGE 1":
            stage = 1; continue
        if line.upper() == "STAGE 2":
            stage = 2; continue
        if line.upper() == "STAGE 3":
            stage = 3; continue
        if ":" in line:
            label, _, value = line.partition(":")
            label_lower = label.strip().lower()
            if label_lower in key_map and stage in key_map[label_lower]:
                field = key_map[label_lower][stage]
                zones[field] = value.strip()
            else:
                errors.append(f"Unrecognized line in Stage {stage}: {line}")
        else:
            errors.append(f"Could not parse: {line}")
    return zones, errors


# ── CS Mail builder ────────────────────────────────────────────────────────────

def build_cs_mail(team: str, z: dict, time_key: str, guild_id: int = None,
                  template_name: str | None = None) -> str:
    """Build CS mail using a guild's stored template (named or default)."""
    from config import get_storm_config, get_storm_template
    cfg       = get_storm_config(guild_id, "CS") if guild_id else {}
    if guild_id:
        template = get_storm_template(guild_id, "CS", template_name)
    else:
        template = cfg.get("mail_template") or ""

    if time_key == "1":
        local_time  = cfg.get("time_option_1_local", "")
        server_time = cfg.get("time_option_1_server", "")
    else:
        local_time  = cfg.get("time_option_2_local", "")
        server_time = cfg.get("time_option_2_server", "")
    time_str = f"{local_time} ({server_time})" if local_time else time_key

    # Build zones block from the zone dict
    zone_lines = []
    for key, members in z.items():
        if members and members != "(open)":
            label = key.replace("_", " ").replace("s1 ", "").replace("s2 ", "").replace("s3 ", "").title()
            zone_lines.append(f"**{label}**")
            if isinstance(members, list):
                zone_lines.append("\n".join(str(m) for m in members))
            else:
                zone_lines.append(str(members))
            zone_lines.append("")
    zones_block = "\n".join(zone_lines).strip()

    if isinstance(z.get("s3_pop_pair1"), list):
        subs_block = "\n".join(str(s) for s in z["s3_pop_pair1"])
    else:
        subs_block = z.get("s3_pop_pair1", "(none)") or "(none)"

    if template:
        return template.format(
            alliance_name="Alliance",
            zones=zones_block,
            subs=subs_block,
            time=time_str,
        )

    return "\n".join([
        "**Canyon Storm**",
        "",
        "**Zone Assignments**",
        zones_block,
        "",
        "**Subs**",
        subs_block,
        "",
        f"**Time:** {time_str}",
    ])


# ── CS Approval view ───────────────────────────────────────────────────────────

class CSApprovalView(discord.ui.View):
    def __init__(self, bot, team: str, mail: str, zones: dict, time_key: str):
        super().__init__(timeout=3600)
        self.bot      = bot
        self.team     = team
        self.mail     = mail
        self.zones    = zones
        self.time_key = time_key

    async def _disable(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="✅ Looks Good — Save & Copy", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        await asyncio.get_event_loop().run_in_executor(None, save_cs_assignments, self.team, self.zones)
        channel = interaction.channel
        if channel:
            await channel.send(
                f"✅ **Canyon Storm Team {self.team} mail — ready to copy:**\n"
                f"```\n{self.mail}\n```"
            )
        self.stop()

    @discord.ui.button(label="✏️ Edit & Redo", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        channel = interaction.channel
        if channel:
            template = build_cs_template(self.zones)
            await channel.send(
                f"✏️ {interaction.user.mention} — copy and edit the block below, then paste it back:\n"
                f"```\n{template}\n```"
            )
            await run_cs_edit_step(self.bot, channel, interaction.user, self.team, self.zones, self.time_key)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        await interaction.followup.send("❌ Draft cancelled.", ephemeral=True)
        self.stop()


# ── CS Time selection ──────────────────────────────────────────────────────────

class CSTimeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None

    @discord.ui.button(label="10AM EST / 12:00 Server", style=discord.ButtonStyle.secondary)
    async def pick_10am(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "10am"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="9PM EST / 23:00 Server", style=discord.ButtonStyle.secondary)
    async def pick_9pm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "9pm"
        await interaction.response.defer()
        self.stop()


# ── CS Core wizard flow ────────────────────────────────────────────────────────

async def run_cs_edit_step(bot, channel, user, team: str, current_zones: dict, time_key: str = None):
    def check(m):
        return m.author == user and m.channel == channel

    if time_key is None:
        time_msg  = await channel.send("⏰ What time is Canyon Storm this week?")
        time_view = CSTimeSelectView()
        await time_msg.edit(view=time_view)
        await time_view.wait()
        try:
            await time_msg.delete()
        except discord.HTTPException:
            pass
        if time_view.selected is None:
            await channel.send("⏰ Timed out. Use `/canyonstorm_draft` to start again.")
            return
        time_key = time_view.selected

    prompt = await channel.send(
        f"📋 {user.mention} — paste your edited assignments below.\n"
        f"*(10 minutes to respond — type `cancel` to stop)*"
    )
    try:
        reply = await bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT)
    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out. Use `/canyonstorm_draft` to start again.")
        try:
            await prompt.delete()
        except discord.HTTPException:
            pass
        return

    try:
        await prompt.delete()
        await reply.delete()
    except discord.HTTPException:
        pass

    if reply.content.strip().lower() == "cancel":
        await channel.send("❌ Draft cancelled.")
        return

    zones, errors = parse_cs_template(reply.content)

    if not zones:
        await channel.send(
            "⚠️ Could not parse any assignments. "
            "Make sure the format matches the template and try `/canyonstorm_draft` again."
        )
        return

    if errors:
        await channel.send(
            "⚠️ Some lines were skipped:\n" + "\n".join(f"• {e}" for e in errors)
        )

    # 💎 Premium: when multiple templates exist, ask which one to use.
    guild_id      = getattr(getattr(channel, "guild", None), "id", None)
    template_name = await _pick_storm_template(bot, channel, guild_id, "CS")
    if template_name is False:
        return  # picker timed out

    mail          = build_cs_mail(team, zones, time_key,
                                  guild_id=guild_id, template_name=template_name)
    approval_view = CSApprovalView(bot=bot, team=team, mail=mail, zones=zones, time_key=time_key)
    await channel.send(
        f"📬 **Canyon Storm Team {team} mail preview:**\n\n{mail}\n\nDoes this look right?",
        view=approval_view,
    )

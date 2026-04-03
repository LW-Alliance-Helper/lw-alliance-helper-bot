"""
storm.py — Desert Storm mail generation

Commands (OGV Leadership only, leadership channel only):
  /draftds — Generate a Desert Storm mail draft for Team A or Team B

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
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

LEADERSHIP_CHANNEL_ID = 1488693874938482799
REQUIRED_ROLE_NAME    = "OGV Leadership"
DS_SHEET_NAME         = "DS Assignments"

GUILD_ID = 1266229297723605052
GUILD    = discord.Object(id=GUILD_ID)

WIZARD_TIMEOUT = 600  # 10 minutes


# ── Default assignments ────────────────────────────────────────────────────────

# Team A — starts empty, leadership fills on first use
DEFAULT_A_ZONES = {
    "Nuclear Silo":       "(open)",
    "Oil Refinery I":     "(open)",
    "Oil Refinery II":    "(open)",
    "Science Hub":        "(open)",
    "Info Center":        "(open)",
    "Field Hospital I":   "(open)",
    "Field Hospital II":  "(open)",
    "Field Hospital III": "(open)",
    "Field Hospital IV":  "(open)",
    "Arsenal":            "(open)",
    "Mercenary Factory":  "(open)",
}
DEFAULT_A_SUBS = []

# Team B — current assignments
DEFAULT_B_ZONES = {
    "Nuclear Silo":       "Jon, Spartan, Death",
    "Oil Refinery I":     "Death, Lito, Jc, Redneck, Chose",
    "Oil Refinery II":    "Jon, Legit, Mer, Spartan, BadFosk",
    "Science Hub":        "Kimber, Ice",
    "Info Center":        "MotherGoose, Toxic",
    "Field Hospital I":   "Lareyna, Rodrigo",
    "Field Hospital II":  "FieryOnion, Walrus",
    "Field Hospital III": "Bad Pew, Abe",
    "Field Hospital IV":  "(open)",
    "Arsenal":            "MotherGoose, FieryOnion",
    "Mercenary Factory":  "Ice, Walrus",
}
DEFAULT_B_SUBS = [
    ("Rodrigo",    "Adrian"),
    ("Redneck",    "Sylvia"),
    ("Kimber",     "Olddave"),
    ("Abe",        "cmath"),
    ("Lareyna",    "swaggy"),
    ("Toxic",      "Bryce"),
    ("Walrus",     "Drezy"),
    ("Chose",      "Missgoose"),
    ("FieryOnion", "Soka"),
    ("Bad Pew",    "Catie"),
]

DEFAULTS = {
    "A": (DEFAULT_A_ZONES, DEFAULT_A_SUBS),
    "B": (DEFAULT_B_ZONES, DEFAULT_B_SUBS),
}

DS_TIMES = {
    "4pm": ("4:00pm ET", "18:00 server"),
    "9pm": ("9:00pm ET", "01:00 server"),
}


# ── Google Sheets persistence ──────────────────────────────────────────────────

def _get_spreadsheet():
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
    return gc.open_by_key(os.getenv("SPREADSHEET_ID"))


def load_ds_assignments(team: str) -> tuple[dict, list]:
    """
    Load saved DS assignments for the given team ("A" or "B").
    Falls back to defaults if nothing is saved yet.
    """
    zone_key = f"DS_{team}_ZONES"
    sub_key  = f"DS_{team}_SUBS"

    try:
        sh   = _get_spreadsheet()
        ws   = sh.worksheet(DS_SHEET_NAME)
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
        ws = sh.worksheet(DS_SHEET_NAME)

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

def build_ds_mail(team: str, zones: dict, subs: list, time_key: str) -> str:
    est_time, server_time = DS_TIMES.get(time_key, DS_TIMES["4pm"])

    zone_lines = []
    for zone, members in zones.items():
        zone_lines.append(f"**{zone}**")
        zone_lines.append(members)
        zone_lines.append("")  # blank line between zones

    sub_lines = [f"{starter} - {sub}" for starter, sub in subs]
    sub_block = "\n".join(sub_lines) if sub_lines else "(no sub pairs set)"

    return "\n".join([
        "🔥 **OGV Warriors — Desert Storm**",
        "We've got a strong setup going into this. If we stay coordinated and flexible, we're in a great spot to control the map early and close strong.",
        "",
        "🎯 **Objective & Game Plan**",
        "We hit our zones fast, secure early control, and stay coordinated to hold momentum.",
        "* We move quickly at the start to lock in our zones",
        "* We hold position as a team, avoiding unnecessary solo fights",
        "* We call out pressure early so reinforcements can respond fast",
        "* If a zone is stable, we shift support to nearby teammates",
        "",
        "",
        "🏆 **Zone Assignments**",
        "",
        "\n".join(zone_lines),
        "🔄 **Subs & Coordination**",
        "* Subs check in with your starter before match start",
        "* Starters confirm your sub is ready before stepping out",
        "* DM > AC (keeps things from getting buried)",
        "* Rotate out when troops are empty or by ~15 min latest",
        "",
        "**Sub pairs:**",
        sub_block,
        "",
        "⚠️ **Key Focus**",
        "If we're behind with <10 min left, prioritize: Silo, Refineries, Arsenal, Mercenary Factory",
        "",
        "⏳ **Timing**",
        f"{est_time} ({server_time})",
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
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None

    @discord.ui.button(label="4PM EST / 1800 Server", style=discord.ButtonStyle.primary)
    async def pick_4pm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "4pm"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="9PM EST / 0100 Server", style=discord.ButtonStyle.secondary)
    async def pick_9pm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "9pm"
        await interaction.response.defer()
        self.stop()


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
        save_ds_assignments(self.team, self.zones, self.subs)
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
            await channel.send("⏰ Timed out. Use `/draftds` to start again.")
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
        await channel.send("⏰ Timed out. Use `/draftds` to start again.")
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
            "Make sure the format matches the template and try `/draftds` again."
        )
        return

    if errors:
        await channel.send(
            "⚠️ Some lines were skipped:\n" +
            "\n".join(f"• {e}" for e in errors)
        )

    mail          = build_ds_mail(team, zones, subs, time_key)
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
    # Accept commands in the leadership channel or any thread inside it
    in_channel = interaction.channel_id == LEADERSHIP_CHANNEL_ID
    if not in_channel:
        channel = interaction.channel
        if isinstance(channel, discord.Thread) and channel.parent_id == LEADERSHIP_CHANNEL_ID:
            in_channel = True
    if not in_channel:
        await interaction.response.send_message(
            "⛔ This command can only be used in the leadership channel.", ephemeral=True
        )
        return False
    if REQUIRED_ROLE_NAME not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{REQUIRED_ROLE_NAME}** role to use this command.", ephemeral=True
        )
        return False
    return True


# ── Cog ────────────────────────────────────────────────────────────────────────

class StormCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="draftds",
        description="Generate a Desert Storm mail draft for Team A or Team B",
    )
    @app_commands.guilds(GUILD)
    async def draftds(self, interaction: discord.Interaction):
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
            await channel.send("⏰ Timed out. Use `/draftds` to start again.")
            await interaction.followup.send("⏰ Timed out.", ephemeral=True)
            return

        team = team_view.selected

        # Step 2: Load and post the template for that team
        zones, subs = load_ds_assignments(team)
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


async def setup(bot: commands.Bot):
    await bot.add_cog(StormCog(bot))

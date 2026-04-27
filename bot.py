import asyncio
import discord
from discord import app_commands
from discord.ext import commands, tasks
import re
import os
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from scheduler import (
    run_scheduler, post_editor, get_event_datetimes, default_event_list,
    next_event_dates, is_friday,
)
from growth import run_growth_snapshot
from zoneinfo import ZoneInfo
from config import init_db, get_config, get_or_create_config

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

ET = ZoneInfo("America/New_York")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Guards ─────────────────────────────────────────────────────────────────────

def is_leadership(interaction: discord.Interaction) -> bool:
    cfg = get_config(interaction.guild_id)
    if not cfg:
        return False
    return cfg.leadership_role_name in [r.name for r in interaction.user.roles]


def in_leadership_channel(interaction: discord.Interaction) -> bool:
    """Accept commands in any channel or thread within the leadership category."""
    cfg = get_config(interaction.guild_id)
    if not cfg:
        return False
    channel = interaction.channel
    if isinstance(channel, discord.Thread):
        parent = channel.parent
        return parent is not None and getattr(parent, "category_id", None) == cfg.leadership_category_id
    return getattr(channel, "category_id", None) == cfg.leadership_category_id


async def guard(interaction: discord.Interaction) -> bool:
    """Check role and channel. Respond with an error and return False if either fails."""
    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(
            "⚙️ This bot hasn't been set up yet. Run `/setup` to get started.", ephemeral=True
        )
        return False
    if not in_leadership_channel(interaction):
        await interaction.response.send_message(
            "⛔ This command can only be used in the leadership channel.", ephemeral=True
        )
        return False
    if not is_leadership(interaction):
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.", ephemeral=True
        )
        return False
    return True


# ── Bot events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    # Initialise the config database and seed OGV defaults
    init_db()
    print(f"[INFO] Logged in as {bot.user} (ID: {bot.user.id})")

    # Load cogs — skip if already loaded (happens on reconnect)
    if "train" not in bot.extensions:
        await bot.load_extension("train")
        print(f"[INFO] Train cog loaded")
    if "storm" not in bot.extensions:
        await bot.load_extension("storm")
        print(f"[INFO] Storm cog loaded")
    if "storm_log" not in bot.extensions:
        await bot.load_extension("storm_log")
        print(f"[INFO] Log cog loaded")
    if "survey" not in bot.extensions:
        await bot.load_extension("survey")
        print(f"[INFO] Survey cog loaded")
    if "setup_cog" not in bot.extensions:
        await bot.load_extension("setup_cog")
        print(f"[INFO] Setup cog loaded")

    # Sync slash commands globally so they work in any server
    synced = await bot.tree.sync()
    print(f"[INFO] Synced {len(synced)} slash commands globally")

    # Only start background tasks once — they persist across reconnects
    if not hasattr(bot, "_tasks_started"):
        bot._tasks_started = True
        bot.loop.create_task(run_scheduler(bot))
        print(f"[INFO] Event scheduler started")
        bot.loop.create_task(_run_growth_on_startup())
        growth_task.start()
        print(f"[INFO] Growth tracker started")


async def _run_growth_on_startup():
    """Run the growth snapshot once on startup for the initial baseline."""
    await bot.wait_until_ready()
    try:
        print(f"[GROWTH] Running initial snapshot on startup")
        await asyncio.get_event_loop().run_in_executor(None, run_growth_snapshot)
    except Exception as e:
        import traceback
        print(f"[GROWTH] Error during startup snapshot: {e}")
        print(f"[GROWTH] Traceback:\n{traceback.format_exc()}")


@tasks.loop(hours=1)
async def growth_task():
    """Check every hour — run snapshots for guilds whose schedule is due."""
    from config import DB_PATH, get_growth_config
    import sqlite3
    now = datetime.now(tz=ET)

    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT guild_id FROM guild_configs WHERE setup_complete = 1"
            ).fetchall()
        guild_ids = [r[0] for r in rows]
    except Exception as e:
        print(f"[GROWTH] Could not read guild list: {e}")
        return

    for gid in guild_ids:
        gcfg = get_growth_config(gid)
        if not gcfg.get("enabled"):
            continue

        should_run = False
        freq = gcfg.get("snapshot_frequency", "monthly")

        if freq == "monthly":
            day = gcfg.get("snapshot_day", 1)
            if now.day == day and now.hour == 22:
                should_run = True
        elif freq == "interval":
            # Use a simple check: run at 10pm ET if today is a multiple of interval days
            # from a fixed epoch (Jan 1 2026)
            from datetime import date as _date
            epoch   = _date(2026, 1, 1)
            delta   = (_date.today() - epoch).days
            interval = gcfg.get("snapshot_interval", 30)
            if delta % interval == 0 and now.hour == 22:
                should_run = True

        if should_run:
            try:
                print(f"[GROWTH] Scheduled snapshot triggered for guild {gid}")
                from growth import _run_growth_snapshot_inner
                await asyncio.get_event_loop().run_in_executor(
                    None, _run_growth_snapshot_inner, gid
                )
            except Exception as e:
                print(f"[GROWTH] Error during scheduled snapshot for guild {gid}: {e}")


@growth_task.before_loop
async def before_growth_task():
    await bot.wait_until_ready()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)


@bot.tree.command(
    name="rungrowth",
    description="Manually run a growth snapshot for this server",
)
async def rungrowth_slash(interaction: discord.Interaction):
    if not await guard(interaction):
        return
    from config import get_growth_config
    gcfg = get_growth_config(interaction.guild_id)
    if not gcfg.get("enabled"):
        await interaction.response.send_message(
            "⚠️ Growth tracking isn't configured yet. Run `/setup_growth` to set it up.",
            ephemeral=True,
        )
        return
    await interaction.response.defer(ephemeral=True)
    try:
        from growth import _run_growth_snapshot_inner
        _run_growth_snapshot_inner(interaction.guild_id)
        await interaction.followup.send(
            f"✅ Growth snapshot complete — check the **{gcfg.get('tab_growth', 'Growth Tracking')}** tab.",
            ephemeral=True,
        )
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Growth snapshot failed: {e}",
            ephemeral=True,
        )


# ── /events command ────────────────────────────────────────────────────────────

@bot.tree.command(
    name="events",
    description="Open the event editor for today or a specific date",
)
@app_commands.describe(date="Optional date, e.g. 'April 5' or '4/5' (defaults to today)")
async def events_slash(interaction: discord.Interaction, date: str = None):
    if not await guard(interaction):
        return

    await interaction.response.defer(ephemeral=False)

    target_date = None
    current_year = datetime.now(tz=ET).year

    if date:
        # Numeric: 4/5 or 4/5/2026
        numeric = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?$", date.strip())
        if numeric:
            try:
                target_date = date_cls(
                    int(numeric.group(3)) if numeric.group(3) else current_year,
                    int(numeric.group(1)),
                    int(numeric.group(2)),
                )
            except ValueError:
                pass

        # Month name: April 5
        if not target_date:
            named = re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", date.strip(), re.IGNORECASE)
            if named:
                month_map = {
                    "january": 1, "february": 2, "march": 3, "april": 4,
                    "may": 5, "june": 6, "july": 7, "august": 8,
                    "september": 9, "october": 10, "november": 11, "december": 12,
                    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
                    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
                }
                month = month_map.get(named.group(1).lower())
                if month:
                    try:
                        target_date = date_cls(current_year, month, int(named.group(2)))
                    except ValueError:
                        pass

        if not target_date:
            await interaction.followup.send(
                f"⚠️ Could not parse date `{date}`. Try formats like `April 5` or `4/5`.",
                ephemeral=True,
            )
            return
    else:
        target_date = date_cls.today()

    upcoming   = next_event_dates(from_date=target_date, count=1)
    event_date = upcoming[0]
    days_diff  = (event_date - target_date).days

    if days_diff > 0:
        await interaction.followup.send(
            f"ℹ️ **{target_date.strftime('%B %-d')}** is not an event day. "
            f"Showing the next event date: **{event_date.strftime('%A, %B %-d')}**.",
            ephemeral=True,
        )

    marauder_dt, siege_dt = get_event_datetimes(event_date)
    event_list = default_event_list(marauder_dt, siege_dt)
    event_key  = f"event-{event_date.isoformat()}-manual"
    run_date   = marauder_dt.date()

    await post_editor(bot, event_list, event_key, run_date)
    print(f"[EVENTS] Manual event editor opened for {event_date} by {interaction.user}")


# ── /help command ──────────────────────────────────────────────────────────────

@bot.tree.command(
    name="help",
    description="Show all available bot commands",
)
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Alliance Helper — Commands",
        color=discord.Color.blurple(),
        description=(
            "All commands require the configured leadership role and must be used in the leadership channel.\n"
            "Run `/setup` first if you haven't configured the bot yet."
        ),
    )

    embed.add_field(
        name="⚙️ Core Setup",
        value=(
            "Configure the bot for your server. Start here before using any other features.\n"
            "`/setup` — Configure roles, leadership channel, timezone, and Google Sheet\n"
            "`/setup_status` — View current server configuration\n"
            "`/setup_reset` — Clear server configuration and start over"
        ),
        inline=False,
    )

    embed.add_field(
        name="📣 Event Announcements",
        value=(
            "Automate event scheduling for Plague Marauder, Zombie Siege, and any other recurring events. "
            "Drafts are posted to leadership for review before going public.\n"
            "`/setup_events` — Configure events, announcement channels, draft time, and 5-min warning\n"
            "`/events [date]` — Open the event editor for today or a specific date"
        ),
        inline=False,
    )

    embed.add_field(
        name="🚂 Train Schedule",
        value=(
            "Track who is assigned the alliance train each day and optionally generate a personalised "
            "ChatGPT prompt to write a blurb for that member's announcement.\n"
            "`/setup_train` — Configure the train tab, blurb generation, and reminders\n"
            "`/schedule` — View the current train schedule\n"
            "`/schedule_set` — Add or update schedule entries\n"
            "`/schedule_clear` — Clear the entire schedule\n"
            "`/trainprompt [date]` — Get the ChatGPT prompt for a scheduled member\n"
            "`/train_addbirthdays` — Manually run the birthday check now"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎂 Birthdays",
        value=(
            "Track member birthdays from your Google Sheet and optionally post announcements "
            "in Discord and assign members to the train schedule on their birthday.\n"
            "`/setup_birthdays` — Configure birthday tracking, train integration, and announcements"
        ),
        inline=False,
    )

    embed.add_field(
        name="⚔️ Desert Storm",
        value=(
            "Generate and manage Desert Storm team mail drafts and log participation each week.\n"
            "`/setup_desertstorm` — Configure teams, sheet tab, log channel, and mail template\n"
            "`/desertstorm_draft` — Generate a Desert Storm mail draft for Team A or B\n"
            "`/desertstorm_participation` — Log Desert Storm participation data\n"
            "`/desertstorm_log [date]` — View a Desert Storm log entry (defaults to today)"
        ),
        inline=False,
    )

    embed.add_field(
        name="🏜️ Canyon Storm",
        value=(
            "Generate and manage Canyon Storm team mail drafts and log participation each week.\n"
            "`/setup_canyonstorm` — Configure teams, sheet tab, log channel, and mail template\n"
            "`/canyonstorm_draft` — Generate a Canyon Storm mail draft for Team A or B\n"
            "`/canyonstorm_participation` — Log Canyon Storm participation data\n"
            "`/canyonstorm_log [date]` — View a Canyon Storm log entry (defaults to today)"
        ),
        inline=False,
    )

    embed.add_field(
        name="📋 Survey",
        value=(
            "Collect member statistics through a private Discord thread survey. "
            "Responses are saved directly to your Google Sheet.\n"
            "`/setup_survey` — Configure survey questions, channels, and sheet tabs\n"
            "`/survey_post` — Post (or repost) the survey button in the survey channel\n"
            "Members click Answer to open a private thread and submit their stats"
        ),
        inline=False,
    )

    embed.add_field(
        name="📈 Growth Tracking",
        value=(
            "Take periodic snapshots of your members' stats to track alliance growth over time. "
            "You define which metrics to track and how often — snapshots are saved to your Google Sheet.\n"

            "`/setup_growth` — Configure source tab, metrics to track, and snapshot schedule\n\n"
            "`/rungrowth` — Manually run a growth snapshot"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔧 Utilities",
        value="`/cancel` — Cancel your active train wizard or storm log session",
        inline=False,
    )

    embed.set_footer(text="Alliance Helper — Run /setup to get started")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Alias so date_cls doesn't conflict with the `date` parameter name in events_slash
date_cls = date

bot.run(DISCORD_TOKEN)

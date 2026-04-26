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
    """Check every hour — run the snapshot on the 1st of the month at 10pm ET."""
    now = datetime.now(tz=ET)
    if now.day == 1 and now.hour == 22 and now.minute < 60:
        try:
            print(f"[GROWTH] Monthly snapshot triggered for {now.strftime('%B %Y')}")
            await asyncio.get_event_loop().run_in_executor(None, run_growth_snapshot)
        except Exception as e:
            print(f"[GROWTH] Error during monthly snapshot: {e}")


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
    description="Manually run the monthly squad power growth snapshot",
)
async def rungrowth_slash(interaction: discord.Interaction):
    if not await guard(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    try:
        run_growth_snapshot()
        await interaction.followup.send(
            "✅ Growth snapshot complete — check the Growth Tracking tab.",
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
        description="Leadership commands require the configured leadership role and channel.",
    )

    embed.add_field(
        name="⚙️ Server Setup",
        value=(
            "`/setup` — Configure the bot for your server (roles, channels, sheet)\n"
            "`/setup_events` — Add or update an event type (Marauder, Siege, etc.)\n"
            "`/setup_events_list` — View all configured events\n"
            "`/setup_events_remove` — Deactivate an event\n"
            "`/setup_train` — Configure train schedule tab, themes, tones, and prompt\n"
            "`/setup_birthdays` — Configure birthday tracking\n"
            "`/setup_desertstorm` — Configure DS mail template and time options\n"
            "`/setup_canyonstorm` — Configure CS mail template and time options\n"
            "`/setup_survey` — Configure survey questions and sheet tabs\n"
            "`/setup_status` — View current server configuration\n"
            "`/setup_reset` — Clear server configuration and start over"
        ),
        inline=False,
    )

    embed.add_field(
        name="🚂 Train Schedule",
        value=(
            "`/schedule` — View the current train schedule\n"
            "`/schedule_set` — Add or update entries in the schedule\n"
            "`/schedule_clear` — Clear the entire schedule\n"
            "`/trainprompt [date]` — Retrieve a stored ChatGPT prompt\n"
            "`/setmembertab [tab]` — Set the active member sheet tab\n"
            "`/checkbirthdays` — Manually run the birthday check\n"
            "`/cancel` — Cancel your active wizard or log session"
        ),
        inline=False,
    )

    embed.add_field(
        name="⚔️ Storm Mails",
        value=(
            "`/draftds` — Generate a Desert Storm mail draft for Team A or B\n"
            "`/draftcs` — Generate a Canyon Storm mail draft for Team A or B\n"
            "Edit the pre-filled template, paste it back, preview, then approve to save"
        ),
        inline=False,
    )

    embed.add_field(
        name="📊 Storm Logging",
        value=(
            "`/logds` — Log Desert Storm participation data\n"
            "`/logcs` — Log Canyon Storm participation data\n"
            "`/viewlog [event] [date]` — View a full log entry for a specific date"
        ),
        inline=False,
    )

    embed.add_field(
        name="📈 Growth Tracking",
        value=(
            "`/rungrowth` — Manually run the monthly squad power snapshot\n"
            "Snapshots also run automatically on the 1st of each month"
        ),
        inline=False,
    )

    embed.add_field(
        name="📋 Squad Powers Survey",
        value=(
            "`/postsurvey` — Post (or repost) the survey button in the survey channel\n"
            "Members click Answer to open a private thread and submit their stats"
        ),
        inline=False,
    )

    embed.add_field(
        name="📣 Event Announcements",
        value=(
            "`/events [date]` — Open the event editor for today or a specific date\n"
            "Drafts are also automatically posted at your configured draft time on event days"
        ),
        inline=False,
    )

    embed.set_footer(text="Alliance Helper — lw-alliance-helper.iam.gserviceaccount.com")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Alias so date_cls doesn't conflict with the `date` parameter name in events_slash
date_cls = date

bot.run(DISCORD_TOKEN)

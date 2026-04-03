import discord
from discord import app_commands
from discord.ext import commands, tasks
import re
import os
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from sheets import find_and_update_or_create_row
from scheduler import (
    run_scheduler, post_editor, get_event_datetimes, default_event_list,
    next_event_dates, is_friday,
)
from growth import run_growth_snapshot
from zoneinfo import ZoneInfo

load_dotenv()

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
WATCHED_CHANNEL_ID = int(os.getenv("WATCHED_CHANNEL_ID"))

ET                    = ZoneInfo("America/New_York")
GUILD_ID              = 1266229297723605052
LEADERSHIP_CHANNEL_ID = 1488693874938482799
LEADERSHIP_ROLE_NAME  = "OGV Leadership"

GUILD = discord.Object(id=GUILD_ID)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Guards ─────────────────────────────────────────────────────────────────────

def is_leadership(interaction: discord.Interaction) -> bool:
    return LEADERSHIP_ROLE_NAME in [r.name for r in interaction.user.roles]

def in_leadership_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == LEADERSHIP_CHANNEL_ID

async def guard(interaction: discord.Interaction) -> bool:
    """Check role and channel. Respond with an error and return False if either fails."""
    if not in_leadership_channel(interaction):
        await interaction.response.send_message(
            "⛔ This command can only be used in the leadership channel.", ephemeral=True
        )
        return False
    if not is_leadership(interaction):
        await interaction.response.send_message(
            f"⛔ You need the **{LEADERSHIP_ROLE_NAME}** role to use this command.", ephemeral=True
        )
        return False
    return True


# ── Survey embed parsing ───────────────────────────────────────────────────────

def parse_survey_embeds(embeds):
    if not embeds or len(embeds) < 10:
        print(f"[WARN] Expected at least 10 embeds, got {len(embeds)}")
        return None

    main_description = embeds[0].description or ""
    print(f"[DEBUG] Main embed description:\n{main_description}\n")

    discord_id_match = re.search(r"<@!?(\d+)>", main_description)
    discord_id = discord_id_match.group(1) if discord_id_match else None

    name_match = re.search(r"\*\*Name\s*\*\*\s*\n(.+)", main_description)
    username = None
    if name_match:
        name_line = name_match.group(1).strip()
        username = re.sub(r"\s*<@!?\d+>", "", name_line).strip()

    if not discord_id:
        print("[ERROR] Could not extract Discord ID from embed description.")
        return None

    def get_field_value(embed_index):
        try:
            fields = embeds[embed_index].fields
            if fields:
                return fields[0].value.strip()
        except (IndexError, AttributeError):
            pass
        return None

    date_modified = datetime.now().strftime("%-m/%-d/%Y")

    data = {
        "Username":       username,
        "Discord ID":     discord_id,
        "1st Squad":      get_field_value(1),
        "1st Squad Type": get_field_value(2),
        "2nd Squad":      get_field_value(3),
        "2nd Squad Type": get_field_value(4),
        "3rd Squad":      get_field_value(5),
        "3rd Squad Type": get_field_value(6),
        "Gorilla Level":  get_field_value(8),
        "Drone Level":    get_field_value(9),
        "Date Modified":  date_modified,
    }

    print(f"[INFO] Parsed data: {data}")
    return data


# ── Bot events ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[INFO] Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[INFO] Watching channel ID: {WATCHED_CHANNEL_ID}")

    # Load the train cog
    await bot.load_extension("train")
    print(f"[INFO] Train cog loaded")

    # Sync slash commands to the guild
    bot.tree.copy_global_to(guild=GUILD)
    synced = await bot.tree.sync(guild=GUILD)
    print(f"[INFO] Synced {len(synced)} slash commands to guild {GUILD_ID}")

    # Start the event scheduler
    bot.loop.create_task(run_scheduler(bot))
    print(f"[INFO] Event scheduler started")

    # Run the growth snapshot immediately on startup (first deploy baseline)
    # and start the monthly scheduler
    bot.loop.create_task(_run_growth_on_startup())
    growth_task.start()
    print(f"[INFO] Growth tracker started")


async def _run_growth_on_startup():
    """Run the growth snapshot once on startup for the initial baseline."""
    await bot.wait_until_ready()
    try:
        print(f"[GROWTH] Running initial snapshot on startup")
        run_growth_snapshot()
    except Exception as e:
        print(f"[GROWTH] Error during startup snapshot: {e}")


@tasks.loop(hours=1)
async def growth_task():
    """Check every hour — run the snapshot on the 1st of the month at 10pm ET."""
    now = datetime.now(tz=ET)
    if now.day == 1 and now.hour == 22 and now.minute < 60:
        try:
            print(f"[GROWTH] Monthly snapshot triggered for {now.strftime('%B %Y')}")
            run_growth_snapshot()
        except Exception as e:
            print(f"[GROWTH] Error during monthly snapshot: {e}")


@growth_task.before_loop
async def before_growth_task():
    await bot.wait_until_ready()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.id == WATCHED_CHANNEL_ID:
        if message.author.bot and message.embeds:
            first_embed = message.embeds[0]
            if first_embed.title and "New Response" in first_embed.title:
                print(f"[INFO] Survey response detected from {message.author}")
                data = parse_survey_embeds(message.embeds)
                if not data:
                    print("[ERROR] Failed to parse embed data.")
                    return
                try:
                    result = find_and_update_or_create_row(data)
                    print(f"[INFO] Sheet update result: {result}")
                except Exception as e:
                    print(f"[ERROR] Failed to update Google Sheet: {e}")

    await bot.process_commands(message)


@bot.tree.command(
    name="rungrowth",
    description="Manually run the monthly squad power growth snapshot",
    guild=GUILD,
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
    guild=GUILD,
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
    guild=GUILD,
)
async def help_slash(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 OGV Bot Commands",
        color=discord.Color.blurple(),
        description="All commands are restricted to the leadership channel and require the OGV Leadership role.",
    )

    embed.add_field(
        name="🚂 Train Schedule",
        value=(
            "`/train` — Launch the blurb wizard to build a ChatGPT prompt\n"
            "`/schedule` — Input, view, or clear the train schedule\n"
            "`/trainschedule` — Quick view of the schedule\n"
            "`/trainprompt [date]` — Retrieve a stored ChatGPT prompt\n"
            "`/setbirthdays [tab]` — Set the member tab used for birthday lookups\n"
            "`/checkbirthdays` — Manually run the birthday check now\n"
            "`/cancel` — Cancel your active wizard session"
        ),
        inline=False,
    )

    embed.add_field(
        name="📈 Growth Tracking",
        value=(
            "`/rungrowth` — Manually run the monthly squad power snapshot\n"
            "Snapshots also run automatically on the 1st of each month at 10pm ET"
        ),
        inline=False,
    )

    embed.add_field(
        name="📣 Event Announcements",
        value=(
            "`/events [date]` — Open the event editor for today or a specific date\n"
            "Events are also automatically posted at noon on event days for review"
        ),
        inline=False,
    )

    embed.add_field(
        name="📋 How Announcements Work",
        value=(
            "At noon on event days the bot posts a draft to this channel.\n"
            "Use the editor to add/remove events and adjust times, then build the announcement.\n"
            "Approve it or edit before it posts publicly to Announcements."
        ),
        inline=False,
    )

    embed.add_field(
        name="🛡️ Scheduled Reminders",
        value=(
            "**10pm ET (reset)** — Birthday check runs + train day reminder if someone is scheduled\n"
            "**Noon on event days** — Event announcement draft posted for review\n"
            "**9:55pm ET Fridays** — Buster day shield reminder for approval\n"
            "**5 min before first event** — Auto-posted warning to Announcements"
        ),
        inline=False,
    )

    embed.set_footer(text="OGV Squad Powers Bot")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Alias so date_cls doesn't conflict with the `date` parameter name in events_slash
date_cls = date

bot.run(DISCORD_TOKEN)

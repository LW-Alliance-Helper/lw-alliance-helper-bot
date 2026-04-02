import discord
from discord.ext import commands
import re
import os
from datetime import datetime, date, timedelta
from dotenv import load_dotenv
from sheets import find_and_update_or_create_row
from scheduler import (
    run_scheduler, post_editor, get_event_datetimes, default_event_list,
    next_event_dates, is_friday, make_et_datetime, noon_dt_for,
    EVENT_LIBRARY, parse_time_str, format_et
)
from zoneinfo import ZoneInfo

load_dotenv()

DISCORD_TOKEN      = os.getenv("DISCORD_TOKEN")
WATCHED_CHANNEL_ID = int(os.getenv("WATCHED_CHANNEL_ID"))

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Survey embed parsing ───────────────────────────────────────────────────────

def parse_survey_embeds(embeds):
    """
    Parse the list of embeds from a Subo survey message.

    Embed structure (from Zapier payload analysis):
      embeds[0]  → Main info embed (description contains Name, Member since, Roles, etc.)
      embeds[1]  → Question #1 → 1st Squad Power
      embeds[2]  → Question #2 → 1st Squad Type
      embeds[3]  → Question #3 → 2nd Squad Power
      embeds[4]  → Question #4 → 2nd Squad Type
      embeds[5]  → Question #5 → 3rd Squad Power
      embeds[6]  → Question #6 → 3rd Squad Type
      embeds[7]  → Question #7 → Gorilla (skipped - no sheet column)
      embeds[8]  → Question #8 → Gorilla Level
      embeds[9]  → Question #9 → Drone Level
    """
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

    squad_1       = get_field_value(1)
    squad_1_type  = get_field_value(2)
    squad_2       = get_field_value(3)
    squad_2_type  = get_field_value(4)
    squad_3       = get_field_value(5)
    squad_3_type  = get_field_value(6)
    gorilla_level = get_field_value(8)
    drone_level   = get_field_value(9)

    date_modified = datetime.now().strftime("%-m/%-d/%Y")

    data = {
        "Username":       username,
        "Discord ID":     discord_id,
        "1st Squad":      squad_1,
        "1st Squad Type": squad_1_type,
        "2nd Squad":      squad_2,
        "2nd Squad Type": squad_2_type,
        "3rd Squad":      squad_3,
        "3rd Squad Type": squad_3_type,
        "Gorilla Level":  gorilla_level,
        "Drone Level":    drone_level,
        "Date Modified":  date_modified,
    }

    print(f"[INFO] Parsed data: {data}")
    return data


ET = ZoneInfo("America/New_York")
LEADERSHIP_CHANNEL_ID = 1488693874938482799
LEADERSHIP_ROLE_NAME  = "OGV Leadership"


# ── Discord events ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[INFO] Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[INFO] Watching channel ID: {WATCHED_CHANNEL_ID}")
    bot.loop.create_task(run_scheduler(bot))
    print(f"[INFO] Event scheduler started")
    await bot.load_extension("train")
    print(f"[INFO] Train wizard loaded")


@bot.command(name="events")
async def events_command(ctx: commands.Context, *, date_arg: str = None):
    """
    Pull up the event editor for today or a specified date.

    Usage:
      !events           — today's events
      !events April 5   — events for April 5
      !events 4/5       — same

    Only works in the leadership channel for OGV Leadership role members.
    """
    # Delete the command message
    try:
        await ctx.message.delete()
    except discord.HTTPException:
        pass

    # Guard: leadership channel only
    if ctx.channel.id != LEADERSHIP_CHANNEL_ID:
        return

    # Guard: OGV Leadership role only
    role_names = [r.name for r in ctx.author.roles]
    if LEADERSHIP_ROLE_NAME not in role_names:
        await ctx.channel.send(
            f"⛔ You need the **{LEADERSHIP_ROLE_NAME}** role to use this command.",
            delete_after=10,
        )
        return

    # Determine the target date
    target_date = None

    if date_arg:
        # Try to parse the supplied date using scheduler's parse helpers
        from scheduler import MONTH_MAP  # reuse the month map from train parsing
        import re as _re

        current_year = datetime.now(tz=ET).year

        # Numeric: 4/5 or 4/5/2026
        numeric = _re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}))?$", date_arg.strip())
        if numeric:
            try:
                target_date = date(
                    int(numeric.group(3)) if numeric.group(3) else current_year,
                    int(numeric.group(1)),
                    int(numeric.group(2)),
                )
            except ValueError:
                pass

        # Month name: April 5 or Apr 5
        if not target_date:
            named = _re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", date_arg.strip(), _re.IGNORECASE)
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
                        target_date = date(current_year, month, int(named.group(2)))
                    except ValueError:
                        pass

        if not target_date:
            await ctx.channel.send(
                f"⚠️ Could not parse date `{date_arg}`. Try formats like `April 5` or `4/5`.",
                delete_after=10,
            )
            return
    else:
        target_date = date.today()

    # Find the nearest event date on or after the target date
    upcoming = next_event_dates(from_date=target_date, count=1)
    event_date = upcoming[0]

    # Check if target date is actually an event date
    # If the nearest event is more than 2 days away, warn but still allow
    days_diff = (event_date - target_date).days
    if days_diff > 0:
        await ctx.channel.send(
            f"ℹ️ **{target_date.strftime('%B %-d')}** is not an event day. "
            f"Showing the next event date: **{event_date.strftime('%A, %B %-d')}**.",
            delete_after=10,
        )

    # Build the default event list for that date
    marauder_dt, siege_dt = get_event_datetimes(event_date)
    event_list = default_event_list(marauder_dt, siege_dt)
    event_key  = f"event-{event_date.isoformat()}-manual"
    run_date   = marauder_dt.date()

    await post_editor(bot, event_list, event_key, run_date)
    print(f"[EVENTS] Manual event editor opened for {event_date} by {ctx.author}")


@bot.event
async def on_message(message):
    # Ignore messages from this bot itself
    if message.author == bot.user:
        return

    # Survey channel handling
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


bot.run(DISCORD_TOKEN)

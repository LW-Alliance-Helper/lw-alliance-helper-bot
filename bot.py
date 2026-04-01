import discord
from discord.ext import commands
import re
import os
from datetime import datetime
from dotenv import load_dotenv
from sheets import find_and_update_or_create_row
from scheduler import run_scheduler

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


# ── Discord events ─────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"[INFO] Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"[INFO] Watching channel ID: {WATCHED_CHANNEL_ID}")
    bot.loop.create_task(run_scheduler(bot))
    print(f"[INFO] Event scheduler started")


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

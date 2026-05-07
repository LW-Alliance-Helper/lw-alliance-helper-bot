import asyncio
import discord
import sentry_sdk
from discord import app_commands
from discord.ext import commands, tasks
import re
import os
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv
from scheduler import (
    run_scheduler, post_editor, next_event_dates, is_friday,
)
from stats_publisher import publish_alliance_count
from zoneinfo import ZoneInfo
from config import init_db, get_config
import wizard_registry

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Semantic versioning per https://semver.org. Bump on each release; the
# CHANGELOG.md file is the human-readable record of what each version
# changed.
__version__ = "1.0.19"

# ── Sentry error reporting ───────────────────────────────────────────────────
#
# Initialised only if SENTRY_DSN is set in the environment so local dev runs
# without a DSN don't ship telemetry. Configuration choices:
#   * traces_sample_rate=0.0 — errors only, no performance traces.
#   * send_default_pii=False — no Discord user IDs / IPs in events.
#   * environment — read from $ENV (defaults to "production"); local dev
#     should set ENV=development to keep dev errors out of prod alerts.
# See docs/PREMIUM_SETUP.md / privacy.html "Data Sharing" for what data
# this sends.
_sentry_dsn = os.getenv("SENTRY_DSN")
if _sentry_dsn:
    sentry_sdk.init(
        dsn=_sentry_dsn,
        release=f"lw-alliance-helper@{__version__}",
        environment=os.getenv("ENV", "production"),
        traces_sample_rate=0.0,
        send_default_pii=False,
    )
    print(f"[INFO] Sentry initialised (env={os.getenv('ENV', 'production')}, release={__version__})")
else:
    print(f"[INFO] SENTRY_DSN not set — error reporting disabled")

ET = ZoneInfo("America/New_York")

intents = discord.Intents.default()
intents.message_content = True
# `Intents.default()` deliberately omits the privileged `members` intent,
# so this line opts back in. Without it `guild.members` only contains the
# handful of users Discord surfaces via interactions/typing/voice — which
# is why /sync_members was writing 0 rows even when the portal toggle was
# already on. The portal toggle is the prerequisite for this request to
# succeed; the gateway will refuse the connection at startup if the
# portal toggle is off.
#
# Also required for on_member_join / on_member_remove / on_member_update
# to fire — i.e. for Member Roster Sync's auto-resync to actually work.
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ── Welcome DM (sent to the inviter on every new guild add) ──────────────────

WELCOME_DM = (
    "👋 Thanks for adding **LW Alliance Helper** to **{guild_name}**!\n\n"
    "**Your alliance's data stays with you.** Power scores, growth, train history, "
    "rosters — all of it lives in **your own Google Sheet**, on the Google account "
    "you control. The bot helps to organize; you own the data.\n\n"
    "To get started, run **/setup** in your server's leadership channel. "
    "The wizard walks you through:\n"
    "• Member and leadership roles\n"
    "• The leadership channel\n"
    "• Your alliance's timezone\n"
    "• Sharing your Google Sheet with the bot\n\n"
    "After setup, run **/help** to see every available feature.\n\n"
    "💎 **Premium is a per-user subscription** — one $4.99/mo applies to "
    "**one server at a time**. Run `/upgrade` to subscribe; the bot pins "
    "your subscription to the server you ran it in. Use `/premium_assign` "
    "to move it later, or `/premium_status` to see where it's active.\n\n"
    "📖 Setup guide: <https://lw-alliance-helper.github.io/setup.html>\n"
    "📋 All commands: <https://lw-alliance-helper.github.io/commands.html>\n"
    "💎 Pricing & Premium: <https://lw-alliance-helper.github.io/pricing.html>\n\n"
    "🐛 Need help or found a bug? Open an issue at:\n"
    "<https://github.com/LW-Alliance-Helper/lw-alliance-helper.github.io/issues>"
)


async def _update_presence():
    """Set the bot's status to reflect the current guild count.

    Format: `Helping N LW Alliance(s)`. Called from on_ready,
    on_guild_join, and on_guild_remove so the count stays current.
    """
    count = len(bot.guilds)
    name  = f"Helping {count} LW Alliance{'s' if count != 1 else ''}"
    try:
        # CustomActivity renders the name as-is (no "Playing" / "Watching"
        # prefix). Falls back to a Watching activity if Discord rejects
        # CustomActivity for bots on this gateway version.
        try:
            activity = discord.CustomActivity(name=name)
            await bot.change_presence(activity=activity)
        except Exception:
            await bot.change_presence(activity=discord.Activity(
                type=discord.ActivityType.watching, name=name,
            ))
    except Exception as e:
        print(f"[PRESENCE] Could not update status: {e}")
        sentry_sdk.capture_exception(e)


# ── Guards ─────────────────────────────────────────────────────────────────────

def is_leadership(interaction: discord.Interaction) -> bool:
    cfg = get_config(interaction.guild_id)
    if not cfg:
        return False
    return cfg.leadership_role_name in [r.name for r in interaction.user.roles]


async def guard(interaction: discord.Interaction) -> bool:
    """Check setup-complete and leadership role. Respond with an error and return False if either fails."""
    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(
            "⚙️ This bot hasn't been set up yet. Run `/setup` to get started.", ephemeral=True
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
    # Initialise the config database (creates tables and applies pending migrations)
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
    if "donate" not in bot.extensions:
        await bot.load_extension("donate")
        print(f"[INFO] Donate cog loaded")
    if "member_roster" not in bot.extensions:
        await bot.load_extension("member_roster")
        print(f"[INFO] Member Roster cog loaded")

    # Sync slash commands globally so they work in any server
    synced = await bot.tree.sync()
    print(f"[INFO] Synced {len(synced)} slash commands globally")

    # Set the bot's presence to reflect the live guild count.
    await _update_presence()

    # Only start background tasks once — they persist across reconnects
    if not hasattr(bot, "_tasks_started"):
        bot._tasks_started = True
        bot.loop.create_task(run_scheduler(bot))
        print(f"[INFO] Event scheduler started")
        growth_task.start()
        print(f"[INFO] Growth tracker started")
        stats_publish_task.start()
        print(f"[INFO] Stats publisher started")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """When the bot is added to a new server, DM the inviter (or the owner
    if the inviter can't be determined) with a welcome / setup message,
    and refresh the presence count.
    """
    print(f"[GUILD] Joined {guild.name} (ID: {guild.id}) — {guild.member_count} members")

    # Try to identify the inviter via the audit log (requires View Audit Log
    # permission, which the bot's default role normally gets).
    inviter: discord.User | discord.Member | None = None
    try:
        async for entry in guild.audit_logs(action=discord.AuditLogAction.bot_add, limit=5):
            target = getattr(entry, "target", None)
            if target is not None and target.id == bot.user.id:
                inviter = entry.user
                break
    except (discord.Forbidden, discord.HTTPException) as e:
        print(f"[GUILD] Couldn't read audit log on join for {guild.name}: {e}")

    # Fall back to the guild owner if the inviter isn't available.
    target_user = inviter or guild.owner
    if target_user is None:
        print(f"[GUILD] No DM target found for {guild.name}; skipping welcome.")
    else:
        try:
            await target_user.send(WELCOME_DM.format(guild_name=guild.name))
            print(f"[GUILD] Welcome DM sent to {target_user} for {guild.name}")
        except discord.Forbidden:
            print(f"[GUILD] Can't DM {target_user} (DMs closed) for {guild.name}")
        except Exception as e:
            print(f"[GUILD] Welcome DM failed for {guild.name}: {e}")
            sentry_sdk.capture_exception(e)

    await _update_presence()


@bot.event
async def on_guild_remove(guild: discord.Guild):
    """Refresh the presence count when the bot is removed from a server."""
    print(f"[GUILD] Removed from {guild.name} (ID: {guild.id})")
    await _update_presence()


# ── Global slash-command error handler ──────────────────────────────────────
#
# discord.py only fires this for exceptions that aren't caught inside a
# command handler. Most commands have their own try/except around sheet
# I/O and similar; this catches the rest. We unwrap the inner exception
# from CommandInvokeError before reporting so Sentry groups errors by
# the actual cause, not by the wrapper.

ISSUE_TRACKER_URL = "https://github.com/LW-Alliance-Helper/lw-alliance-helper.github.io/issues"


def _format_command_error(error: BaseException, event_id: str | None) -> str:
    """Build a user-facing error message for an unhandled slash-command
    exception. Categorises common Discord errors so the message tells the
    user either how to fix it themselves, or what to put in a support
    ticket. The Sentry event id (when available) is included as a
    `Reference:` line so ticket reports correlate to dashboard events.
    """
    ref_line = f"\n\n**Reference:** `{event_id}`" if event_id else ""

    if isinstance(error, discord.Forbidden):
        # 50001 = Missing Access. The bot isn't in the channel's perms
        # overrides, or its role doesn't have access to the channel/category.
        if error.code == 50001:
            return (
                "⚠️ **I don't have access to this channel.**\n\n"
                "To fix this, either:\n"
                "• Edit this channel's permissions and grant my role **Send Messages**, **Embed Links**, "
                "and **View Channel**, or\n"
                "• Run this command from a channel where I can already post (your leadership channel is "
                "a good choice).\n\n"
                f"If this keeps happening, open an issue at <{ISSUE_TRACKER_URL}> and include the "
                f"reference below.{ref_line}"
            )
        # 50013 = Missing Permissions. Bot has access but lacks a specific perm.
        if error.code == 50013:
            return (
                "⚠️ **I'm missing a Discord permission needed to do that.**\n\n"
                "Make sure my role has these permissions in this channel:\n"
                "• Send Messages\n"
                "• Embed Links\n"
                "• View Channel\n"
                "• Read Message History\n\n"
                f"If this keeps happening, open an issue at <{ISSUE_TRACKER_URL}> and include the "
                f"reference below.{ref_line}"
            )
        # Other Forbidden — surface the code so support can correlate.
        return (
            "⚠️ **Discord blocked this action.** This usually means a permission or role-hierarchy "
            f"issue. Discord error code: `{error.code}`.\n\n"
            f"Open an issue at <{ISSUE_TRACKER_URL}> with the reference below.{ref_line}"
        )

    if isinstance(error, discord.NotFound):
        return (
            "⚠️ **Discord couldn't find something I needed** — usually a channel, role, or message "
            "that's been deleted since the bot was set up.\n\n"
            "Try running `/view_configuration` to check that all your configured channels and roles "
            "still exist.\n\n"
            f"If they look correct and this keeps happening, open an issue at <{ISSUE_TRACKER_URL}> "
            f"with the reference below.{ref_line}"
        )

    if isinstance(error, discord.HTTPException):
        return (
            "⚠️ **Discord's API returned an error.** This is usually transient — try the command "
            f"again in a moment.\n\nDiscord status: `{error.status}`, code: `{error.code}`.\n\n"
            f"If it keeps failing, open an issue at <{ISSUE_TRACKER_URL}> with the reference "
            f"below.{ref_line}"
        )

    # Generic catch-all — bot bug, not a user-actionable error.
    return (
        "⚠️ **Something went wrong running that command.** This looks like a bug on my side, not a "
        "configuration issue you can fix.\n\n"
        f"Please open an issue at <{ISSUE_TRACKER_URL}> with the reference below — it'll let me find "
        f"the exact error in my logs.{ref_line}"
    )


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    actual = getattr(error, "original", error)
    cmd_name = interaction.command.name if interaction.command else "?"
    print(f"[SLASH] Unhandled error in /{cmd_name}: {actual!r}")

    # Capture to Sentry and grab the event id so the user-facing message
    # can include a reference for ticket reports. capture_exception()
    # returns None if Sentry isn't initialised; the formatter handles that.
    event_id = sentry_sdk.capture_exception(actual)

    msg = _format_command_error(actual, event_id)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        # If even the error-reply fails (interaction expired, etc.) just
        # let it go — Sentry already has the original.
        pass


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
        sentry_sdk.capture_exception(e)
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
                sentry_sdk.capture_exception(e)


@growth_task.before_loop
async def before_growth_task():
    await bot.wait_until_ready()


# ── Alliance count publisher ─────────────────────────────────────────────────
#
# Once a day, push the live `len(bot.guilds)` to the website's
# assets/stats.json so the home-page badge stays current. The publisher
# itself decides whether to make a commit (skip if unchanged), and
# silently no-ops if STATS_GITHUB_TOKEN isn't set.

@tasks.loop(hours=24)
async def stats_publish_task():
    try:
        await publish_alliance_count(len(bot.guilds))
    except Exception as e:
        # publish_alliance_count is supposed to swallow its own errors,
        # but belt + suspenders — never let this loop die.
        print(f"[STATS] Publisher loop caught unexpected error: {e}")
        sentry_sdk.capture_exception(e)


@stats_publish_task.before_loop
async def before_stats_publish_task():
    await bot.wait_until_ready()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)


@bot.tree.command(
    name="growth",
    description="Show growth tracking status with options to run a snapshot or edit config",
)
async def growth_slash(interaction: discord.Interaction):
    if not await guard(interaction):
        return
    from config import get_growth_config
    guild_id = interaction.guild_id
    gcfg = get_growth_config(guild_id)

    metrics = gcfg.get("metrics") or []
    freq    = gcfg.get("snapshot_frequency", "monthly")
    sched   = (
        f"Monthly on day {gcfg.get('snapshot_day', 1)}"
        if freq == "monthly"
        else f"Every {gcfg.get('snapshot_interval', 30)} days"
    )
    enabled = bool(gcfg.get("enabled"))

    embed = discord.Embed(
        title="📈 Growth Tracking",
        color=discord.Color.green() if enabled else discord.Color.greyple(),
    )
    embed.add_field(name="Status",         value="✅ Enabled" if enabled else "❌ Disabled",      inline=False)
    embed.add_field(name="Source Tab",     value=gcfg.get("tab_source", "*not set*"),            inline=False)
    embed.add_field(name="Growth Tab",     value=gcfg.get("tab_growth", "*not set*"),            inline=False)
    embed.add_field(name="Snapshot",       value=sched,                                          inline=False)

    if enabled:
        from growth import compute_next_snapshot
        next_dt = compute_next_snapshot(gcfg)
        if next_dt is not None:
            ts = int(next_dt.timestamp())
            embed.add_field(
                name="Next Snapshot",
                value=f"<t:{ts}:F> (<t:{ts}:R>)",
                inline=False,
            )

    embed.add_field(
        name=f"Metrics ({len(metrics)})",
        value=("\n".join(f"• **{m['label']}** — column {m['col']}" for m in metrics) or "*none configured*")[:1024],
        inline=False,
    )

    class GrowthActionView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            if not enabled:
                self.run_now.disabled = True

        @discord.ui.button(label="📸 Run Snapshot Now", style=discord.ButtonStyle.success)
        async def run_now(self, inter: discord.Interaction, button: discord.ui.Button):
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            try:
                from growth import _run_growth_snapshot_inner
                _run_growth_snapshot_inner(guild_id)
                await inter.followup.send(
                    f"✅ Growth snapshot complete — check the **{gcfg.get('tab_growth', 'Growth Tracking')}** tab.",
                    ephemeral=True,
                )
            except Exception as e:
                await inter.followup.send(f"⚠️ Growth snapshot failed: {e}", ephemeral=True)
            self.stop()

        @discord.ui.button(label="⚙️ Edit Config", style=discord.ButtonStyle.primary)
        async def edit_config(self, inter: discord.Interaction, button: discord.ui.Button):
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            await inter.followup.send(
                "Run `/setup_growth` to update the growth tracking configuration.",
                ephemeral=True,
            )
            self.stop()

    await interaction.response.send_message(embed=embed, view=GrowthActionView(), ephemeral=True)


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

    # Per-guild event lookup. Reads `guild_events` rows for the calling
    # guild, groups them by (anchor_date, interval_days), finds the next
    # event date for each repeating group on or after `target_date`, and
    # builds an event_list from every event that fires on the soonest of
    # those dates.
    from collections import defaultdict
    from zoneinfo import ZoneInfo as _ZI
    from config import get_guild_events, get_config

    cfg    = get_config(interaction.guild_id)
    events = get_guild_events(interaction.guild_id, active_only=True)

    if not events:
        await interaction.followup.send(
            "ℹ️ No events configured. Run `/setup_events` to add some.",
            ephemeral=True,
        )
        return

    # Group repeating events by (anchor, interval); manual events skip the
    # editor — they have no recurrence to project forward from.
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for ev in events:
        if ev["schedule_type"] == "repeating" and ev["anchor_date"]:
            groups[(ev["anchor_date"], ev["interval_days"])].append(ev)

    if not groups:
        await interaction.followup.send(
            "ℹ️ No repeating events configured. The event editor only "
            "applies to events with a recurring schedule. Run `/setup_events` "
            "to add one, or add events directly to your manual schedule.",
            ephemeral=True,
        )
        return

    # Find the next occurrence date per group, then pick the soonest.
    next_per_group: list[tuple[date_cls, tuple[str, int]]] = []
    for key, _ in groups.items():
        anchor_str, interval = key
        try:
            anchor = date_cls.fromisoformat(anchor_str)
        except ValueError:
            continue
        upcoming = next_event_dates(from_date=target_date, count=1, anchor=anchor, cycle=interval)
        if upcoming:
            next_per_group.append((upcoming[0], key))

    if not next_per_group:
        await interaction.followup.send(
            "ℹ️ Couldn't compute the next event date — your repeating events "
            "have invalid anchor dates. Run `/setup_events` to fix.",
            ephemeral=True,
        )
        return

    next_per_group.sort(key=lambda x: x[0])
    event_date = next_per_group[0][0]
    days_diff  = (event_date - target_date).days

    if days_diff > 0:
        await interaction.followup.send(
            f"ℹ️ **{target_date:%B} {target_date.day}** is not an event day. "
            f"Showing the next event date: **{event_date:%A, %B} {event_date.day}**.",
            ephemeral=True,
        )

    # Build event_list from every event that fires on `event_date`. A guild
    # may have multiple cycle groups; each one's next-occurrence-after
    # event_date might or might not be event_date itself. We test each
    # group; only those whose next date IS event_date contribute events.
    event_list: list[dict] = []
    draft_channel_id     = 0
    announce_channel_id  = 0
    five_min_warn        = False

    for (anchor_str, interval), group_events in groups.items():
        try:
            anchor = date_cls.fromisoformat(anchor_str)
        except ValueError:
            continue
        upcoming = next_event_dates(from_date=event_date, count=1, anchor=anchor, cycle=interval)
        if not upcoming or upcoming[0] != event_date:
            continue
        for ev in group_events:
            try:
                ev_tz       = _ZI(ev["timezone"])
                t_h, t_m    = (int(p) for p in ev["default_time"].split(":")[:2])
                ev_dt       = datetime(event_date.year, event_date.month, event_date.day, t_h, t_m, tzinfo=ev_tz)
                event_list.append({
                    "key":   ev["short_key"],
                    "name":  ev["name"],
                    "dt":    ev_dt,
                    "blurb": ev["announcement_blurb"],
                })
                draft_channel_id    = ev["draft_channel_id"] or draft_channel_id
                announce_channel_id = ev["announcement_channel_id"] or announce_channel_id
                if ev["five_min_warning"]:
                    five_min_warn = True
            except Exception as e:
                print(f"[EVENTS] Error processing event {ev.get('short_key', '?')}: {e}")

    if not event_list:
        await interaction.followup.send(
            "⚠️ No events to show on the next event date — likely a bad timezone "
            "or default_time on one of your configured events. Run `/setup_events` "
            "to review.",
            ephemeral=True,
        )
        return

    event_list.sort(key=lambda x: x["dt"])
    event_key = f"event-{interaction.guild_id}-{event_date.isoformat()}-manual"

    await post_editor(
        bot, event_list, event_key, event_date,
        cfg=cfg,
        draft_channel_id=draft_channel_id,
        announcement_channel_id=announce_channel_id,
        five_min_warning=five_min_warn,
    )
    print(f"[EVENTS] Manual event editor opened for guild {interaction.guild_id} date {event_date} by {interaction.user}")


# ── /events_log command ───────────────────────────────────────────────────────

@bot.tree.command(
    name="events_log",
    description="Show recent approved event posts (window depends on your tier)",
)
async def events_log_slash(interaction: discord.Interaction):
    if not await guard(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    from config import get_config
    import premium
    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.leadership_channel_id:
        await interaction.followup.send(
            "⚠️ Leadership channel isn't configured. Run `/setup` to configure it.",
            ephemeral=True,
        )
        return

    leadership = bot.get_channel(cfg.leadership_channel_id)
    if leadership is None:
        await interaction.followup.send(
            "⚠️ Could not access the leadership channel.", ephemeral=True
        )
        return

    days   = await premium.get_limit("events_log_days", interaction.guild_id, interaction=interaction)
    days   = days or 30  # safety; LIMITS always returns int here
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    matches = []
    try:
        async for msg in leadership.history(after=cutoff, limit=500):
            if msg.author.id != bot.user.id:
                continue
            if msg.content.startswith("✅ **Approved by"):
                matches.append(msg)
    except discord.Forbidden:
        await interaction.followup.send(
            "⚠️ Bot does not have permission to read message history in the leadership channel.",
            ephemeral=True,
        )
        return

    matches.sort(key=lambda m: m.created_at, reverse=True)

    embed = discord.Embed(
        title=f"📣 Events Log — Past {days} Days",
        description=f"*Showing approved event posts from the past {days} days.*",
        color=discord.Color.blurple(),
    )

    if not matches:
        embed.add_field(name="No approvals found", value=f"*No event posts have been approved in the past {days} days.*", inline=False)
    else:
        lines = []
        for msg in matches[:25]:
            # First line is "✅ **Approved by NAME at H:MMpm et**"
            header = msg.content.split("\n", 1)[0]
            _ldt     = msg.created_at.astimezone(ET)
            _hr12    = _ldt.hour % 12 or 12
            local_dt = f"{_ldt:%a %b} {_ldt.day}, {_hr12}:{_ldt:%M%p} ET".replace("AM", "am").replace("PM", "pm")
            lines.append(f"• {header} *— logged {local_dt}*")
        embed.add_field(name=f"Approvals ({len(matches)})", value="\n".join(lines)[:1024], inline=False)

    if days < 30:
        embed.set_footer(text="Free tier: 7-day window. Upgrade to Premium for 30 days.")
    # Premium: no footer — the 30-day window is the full feature.
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── /help command ──────────────────────────────────────────────────────────────

@bot.tree.command(
    name="help",
    description="Show all available bot commands",
)
async def help_slash(interaction: discord.Interaction):
    import premium
    is_premium_flag = await premium.is_premium(
        interaction.guild_id, interaction=interaction, bot=bot,
    )
    tier_badge = "💎 Premium" if is_premium_flag else "Free tier"

    embed = discord.Embed(
        title=f"🤖 Alliance Helper — Commands  ·  {tier_badge}",
        color=discord.Color.gold() if is_premium_flag else discord.Color.blurple(),
        description=(
            "All commands require the configured leadership role and must be used in the leadership channel.\n"
            "Run `/setup` first if you haven't configured the bot yet.\n\n"
            "🗂️ **Your alliance's data lives in your own Google Sheet** — the bot helps to organize, "
            "you own the data. See [Privacy](https://lw-alliance-helper.github.io/privacy.html#where-your-data-lives) for details."
        ),
    )

    embed.add_field(
        name="⚙️ Core Setup",
        value=(
            "Configure the bot for your server. Start here before using any other features.\n"
            "`/setup` — Configure roles, leadership channel, timezone, and Google Sheet\n"
            "`/view_configuration` — View all configured settings across every wizard\n"
            "`/setup_reset` — Clear server configuration and start over"
        ),
        inline=False,
    )

    embed.add_field(
        name="📣 Event Announcements",
        value=(
            "Automate event scheduling for in-game events such as Plague Marauder and Zombie Siege. "
            "Drafts are posted to a leadership channel for review before being sent to the public announcement channel — both channels are configured during `/setup_events`.\n"
            "`/setup_events` — Configure events, announcement channels, draft time, and 5-min warning\n"
            "`/events [date]` — Open the event editor for today or a specific date\n"
            "`/events_log` — Show approved event posts (7d free / 30d premium)"
        ),
        inline=False,
    )

    embed.add_field(
        name="🚂 Train Schedule",
        value=(
            "Track who is assigned the alliance train each day and optionally generate a personalised "
            "ChatGPT prompt to write a blurb for that member's announcement.\n"
            "`/setup_train` — Configure the train tab, blurb generation, and reminders\n"
            "`/train` — View the schedule with Add / Update / Generate Prompt / Clear buttons\n"
            "`/train_log [date]` — Show recent prompt log entries (7d free / 30d premium)\n"
            "`/train_addbirthdays` — Manually run the birthday check now"
        ),
        inline=False,
    )

    embed.add_field(
        name="🎂 Birthdays",
        value=(
            "Track member birthdays from your Google Sheet and optionally post announcements "
            "in Discord and assign members to the train schedule on their birthday.\n"
            "`/setup_birthdays` — Configure birthday tracking, train integration, and announcements\n"
            "`/birthdays` — Show upcoming birthdays within your configured lookahead window (defaults to 14 days)"
        ),
        inline=False,
    )

    embed.add_field(
        name="⚔️ Desert Storm",
        value=(
            "Generate weekly Desert Storm team mail drafts and log participation each event. "
            "Setup Step 6 lets you turn on participation tracking and define exactly what you want to log — "
            "vote count, sit-outs, custom questions — using free types (text, yes/no, numeric, roster names) "
            "or 💎 Premium types (single-select, multi-select, date).\n"
            "`/setup_desertstorm` — Configure teams, log channel, post channel, mail template, participation\n"
            "`/desertstorm` — Show current rosters and the active mail template\n"
            "`/desertstorm_draft` — Walk through team → time → template, then preview & post the mail\n"
            "`/desertstorm_participation` — Run the configurable participation log for this week\n"
            "`/desertstorm_log [date]` — View a Desert Storm log entry (free: 4 most recent / premium: all)\n"
            "`/desertstorm_remind` — 💎 DM every roster member to participate in this week's DS"
        ),
        inline=False,
    )

    embed.add_field(
        name="🏜️ Canyon Storm",
        value=(
            "Generate weekly Canyon Storm team mail drafts and log participation each event. "
            "Same flow as Desert Storm — preview in leadership, post to a public channel, plus configurable "
            "participation tracking on Setup Step 6.\n"
            "`/setup_canyonstorm` — Configure teams, log channel, post channel, mail template, participation\n"
            "`/canyonstorm` — Show current rosters and the active mail template\n"
            "`/canyonstorm_draft` — Walk through team → time → template, then preview & post the mail\n"
            "`/canyonstorm_participation` — Run the configurable participation log for this week\n"
            "`/canyonstorm_log [date]` — View a Canyon Storm log entry (free: 4 most recent / premium: all)\n"
            "`/canyonstorm_remind` — 💎 DM every roster member to participate in this week's CS"
        ),
        inline=False,
    )

    embed.add_field(
        name="📋 Survey",
        value=(
            "Collect member statistics through a private Discord thread survey. "
            "Each member clicks the survey button, gets walked through your configured questions in their own thread, and their answers land in your Google Sheet automatically. Leadership sees a notification embed in the configured notify channel for every submission.\n"
            "`/setup_survey` — Configure the default survey (questions, channels, sheet tabs, intro)\n"
            "`/survey` — View configured survey(s). 💎 Premium gets **Add / Edit / Remove** buttons here for managing multiple surveys.\n"
            "`/survey_post` — Post (or repost) the answer button (Premium picks which survey)\n"
            "`/survey_remind` — Send now or set up scheduled reminders. Free tier posts to a channel; "
            "💎 Premium adds DM-via-roster delivery."
        ),
        inline=False,
    )

    embed.add_field(
        name="📈 Growth Tracking",
        value=(
            "Take periodic snapshots of your members' stats to track alliance growth over time. "
            "You define which metrics to track and how often — snapshots are saved to your Google Sheet.\n"
            "`/setup_growth` — Configure source tab, metrics to track, and snapshot schedule\n"
            "`/growth` — Show growth status with options to run a snapshot or edit config"
        ),
        inline=False,
    )

    embed.add_field(
        name="💎 Premium Features",
        value=(
            "Unlock with `/upgrade`. Premium adds member-aware features that build on top of the free tier:\n"
            "`/setup_members` — Configure the Member Roster Sync (writes Discord IDs to your sheet so other features can find members by name)\n"
            "`/sync_members` — Manually re-sync the member roster now\n"
            "Multiple named surveys — manage from `/survey` directly via Add / Edit / Remove buttons\n"
            "`/survey_remind` — Send DM reminders via Member Roster, or schedule recurring DM reminders per survey\n"
            "`/desertstorm_remind` — DM every roster member about this week's DS\n"
            "`/canyonstorm_remind` — DM every roster member about this week's CS\n"
            "*Plus: personal birthday DMs, train-assignment DMs, auto-mention members in train reminders, "
            "use threads as destinations, multi-template train and storm support, advanced survey/participation "
            "question types (single-select, multi-select, date), and more.*"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔧 Utilities",
        value=(
            "`/cancel` — Cancel any active wizard or log session and reset wizard state\n"
            "`/help` — Show this command list (always available)\n"
            "`/donate` — 💖 Show optional tip-jar links to support the bot's hosting\n"
            "`/upgrade` — 💎 Subscribe to Premium and pin it to this server\n"
            "`/premium_assign` — 💎 Move your Premium subscription to this server\n"
            "`/premium_status` — 💎 Show your subscription state and assigned server\n"
            "`/premium_unassign` — 💎 Release the pin without canceling the subscription"
        ),
        inline=False,
    )

    if is_premium_flag:
        embed.set_footer(text="💎 Premium is active. Thanks for supporting LW Alliance Helper!")
    else:
        embed.set_footer(text="Alliance Helper — Run /upgrade to unlock Premium features")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Alias so date_cls doesn't conflict with the `date` parameter name in events_slash
date_cls = date


# Guard the runtime entry so `import bot` doesn't try to start the bot
# (which is what tests need to do — they import this module to walk the
# tree-registered commands without booting the real Discord client).
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

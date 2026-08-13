import asyncio
import discord
import sentry_sdk
from discord import app_commands
from discord.ext import commands, tasks
import re
import os
from typing import Literal
from datetime import datetime, date, timedelta, timezone
from dotenv import load_dotenv
from scheduler import (
    run_scheduler,
    post_editor,
    next_event_dates,
    is_friday,
)
from stats_publisher import publish_alliance_count
from sentry_filter import before_send as sentry_before_send
import config_health
from zoneinfo import ZoneInfo
from config import (
    init_db,
    get_config,
    upsert_guild_install_metadata,
    get_guild_install_metadata,
    delete_guild_install_metadata,
    get_app_setting,
    set_app_setting,
)
import support_join_watch
import wizard_registry
from messages import BOT_NOT_IN_GUILD, NOT_SET_UP

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")

# Semantic versioning per https://semver.org. Bump on each release; the
# CHANGELOG.md file is the human-readable record of what each version
# changed.
__version__ = "1.9.0"

# ── Sentry error reporting ───────────────────────────────────────────────────
#
# Initialised only if SENTRY_DSN is set in the environment so local dev runs
# without a DSN don't ship telemetry. Configuration choices:
#   * traces_sample_rate=0.0 — errors only, no performance traces.
#   * send_default_pii=False — no Discord user IDs / IPs in events.
#   * environment — read from $ENV (defaults to "production"); local dev
#     should set ENV=development to keep dev errors out of prod alerts.
#   * before_send — drops upstream failures nobody can fix from the code
#     (see sentry_filter). Every high-priority event auto-files a GitHub
#     issue, so unfixable events cost backlog attention, not just inbox
#     space. Dropped events are still logged.
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
        before_send=sentry_before_send,
    )
    print(
        f"[INFO] Sentry initialised (env={os.getenv('ENV', 'production')}, release={__version__})"
    )
else:
    print("[INFO] SENTRY_DSN not set — error reporting disabled")

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

# `help_command=None` removes discord.py's built-in `!help` prefix command.
# This bot is slash-only (see `/help`); the default prefix helper only ever
# fired when someone typed `!help` in a server the bot shares with another
# `!`-prefix bot, and it crashed with Forbidden (50013) whenever the bot
# lacked send access in that channel (#333). Dropping it lets `!help` fall
# through to the CommandNotFound swallow in on_command_error.
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Background threads (Growth Breakdown auto-post, anything else off the
# event loop) need to schedule coroutines onto the bot's loop without
# touching `bot.loop` directly — discord.py 2.4+ raises when the latter
# is accessed from a non-async context. Module-level globals on this
# file don't work for that either: Railway runs `python bot.py`, so
# this file lives in `sys.modules` as `__main__`, but anything that
# does `import bot` gets a separate `bot` module copy. State has to
# live in a third module that's only ever imported, hence
# `bot_state.py`. See #87.
import bot_state

bot_state.bot = bot
bot_state.ET = ET


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
    "your subscription to the server you ran it in. Use `/premium assign` "
    "to move it later, or `/premium overview` to see where it's active.\n\n"
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
    name = f"Helping {count} LW Alliance{'s' if count != 1 else ''}"
    try:
        # CustomActivity renders the name as-is (no "Playing" / "Watching"
        # prefix). Falls back to a Watching activity if Discord rejects
        # CustomActivity for bots on this gateway version.
        try:
            activity = discord.CustomActivity(name=name)
            await bot.change_presence(activity=activity)
        except Exception:
            await bot.change_presence(
                activity=discord.Activity(
                    type=discord.ActivityType.watching,
                    name=name,
                )
            )
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
        await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
        return False
    if not is_leadership(interaction):
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.",
            ephemeral=True,
        )
        return False
    return True


# ── Install-scope guard ────────────────────────────────────────────────────────


async def reject_commands_only_install(interaction: discord.Interaction) -> bool:
    """Global tree check: refuse commands from guilds the bot never joined.

    A guild can install the app with `applications.commands` but without
    the `bot` scope, which registers every command while no bot member
    joins. Commands are invocable but nothing they do can succeed, and
    each gate downstream misreports the cause: `guard` sends NOT_SET_UP,
    telling leadership to run `/setup` for a bot that was never there.

    A resolvable `guild_id` with no cached guild is the signal. Every
    command carries `@app_commands.guild_only()` and none declares a
    user-install context, so the bot is a member of every guild it can
    legitimately be commanded from.

    Returning False makes discord.py flag the interaction failed and stop
    without raising, so this never reaches `on_app_command_error` and
    never doubles up on the reply.
    """
    # Before READY the guild cache is still filling and an empty lookup
    # proves nothing. Let those through rather than risk telling a
    # correctly-installed alliance that the bot isn't there.
    if not bot.is_ready():
        return True
    if interaction.guild_id is None or bot.get_guild(interaction.guild_id) is not None:
        return True

    print(
        f"[INSTALL] Command in guild {interaction.guild_id} with no bot member "
        f"(commands-only install)"
    )
    # Deliberately not Sentry-captured: this is an install-link problem,
    # not a bug, and every affected guild would report it on every use.
    try:
        await interaction.response.send_message(BOT_NOT_IN_GUILD, ephemeral=True)
    except discord.HTTPException:
        # Interaction already expired or Discord rejected the reply. The
        # command is refused either way.
        pass
    return False


# discord.py invokes this as `self.interaction_check(interaction)`, so an
# instance attribute shadows the no-op default and receives the
# interaction directly.
bot.tree.interaction_check = reject_commands_only_install


# ── Bot events ─────────────────────────────────────────────────────────────────


@bot.event
async def on_ready():
    # Capture the running loop into `bot_state.event_loop` so
    # background-thread callers can schedule coroutines onto it via
    # `asyncio.run_coroutine_threadsafe`. on_ready re-fires on
    # reconnect; just refresh the handle each time so a re-established
    # loop is always reflected.
    bot_state.event_loop = asyncio.get_running_loop()

    # Initialise the config database (creates tables and applies pending migrations)
    init_db()

    # Champion Duel keeps its own database file on the same volume — global
    # tournament data rather than per-guild config, with its own lifecycle
    # (it can be wiped between qualifiers and semifinals). Failure here must
    # not stop the bot: it is one feature, and everything else still works.
    try:
        import champion_duel_db

        champion_duel_db.init_db()
    except Exception as e:  # noqa: BLE001 - one feature must not block startup
        print(f"[CHAMPION_DUEL] Database init failed, feature degraded: {e}")

    print(f"[INFO] Logged in as {bot.user} (ID: {bot.user.id})")

    # Demo guild reset hook. Runs the seed against the configured demo guild
    # whenever SEED_DEMO_ON_BOOT=1 is set in the Railway env. Wipes user-added
    # events + extra surveys, then upserts the canonical demo config and
    # rewrites every demo Sheet tab. Idempotent — safe to fire repeatedly to
    # reset the demo back to a clean state after community members poke at it.
    # Leave this block in place; toggle SEED_DEMO_ON_BOOT to control firing.
    if os.getenv("SEED_DEMO_ON_BOOT") == "1":
        try:
            from scripts.seed_demo import seed_demo_guild_from_env

            await asyncio.to_thread(seed_demo_guild_from_env)
        except Exception as e:
            print(f"[SEED] Demo seed crashed: {type(e).__name__}: {e}")

    # Load cogs — skip if already loaded (happens on reconnect)
    if "train" not in bot.extensions:
        await bot.load_extension("train")
        print("[INFO] Train cog loaded")
    if "survey" not in bot.extensions:
        await bot.load_extension("survey")
        print("[INFO] Survey cog loaded")
    if "setup_cog" not in bot.extensions:
        await bot.load_extension("setup_cog")
        print("[INFO] Setup cog loaded")
    if "donate" not in bot.extensions:
        await bot.load_extension("donate")
        print("[INFO] Donate cog loaded")
    if "member_roster" not in bot.extensions:
        await bot.load_extension("member_roster")
        print("[INFO] Member Roster cog loaded")
    if "export_import_cog" not in bot.extensions:
        await bot.load_extension("export_import_cog")
        print("[INFO] Export/Import cog loaded")
    # Storm commands all live under `/desertstorm` and `/canyonstorm`
    # — one root cog registers both parent groups and dispatches into
    # the per-feature handler modules (storm.py, storm_log.py, etc.).
    if "storm_commands_root" not in bot.extensions:
        await bot.load_extension("storm_commands_root")
        print("[INFO] Storm commands root cog loaded")
    if "buddy_cog" not in bot.extensions:
        await bot.load_extension("buddy_cog")
        print("[INFO] Buddy cog loaded")
    if "member_stats" not in bot.extensions:
        await bot.load_extension("member_stats")
        print("[INFO] Member stats cog loaded")
    if "transfer_cog" not in bot.extensions:
        await bot.load_extension("transfer_cog")
        print("[INFO] Transfer cog loaded")
    if "alliance_duel_cog" not in bot.extensions:
        await bot.load_extension("alliance_duel_cog")
        print("[INFO] Alliance Duel (VS) cog loaded")
    # Champion Duel admin tools. Only ever visible to CHAMPION_DUEL_ADMIN_IDS,
    # and the commands guard on it themselves, so loading unconditionally is
    # safe — an empty env var means every subcommand refuses.
    if "champion_duel_cog" not in bot.extensions:
        await bot.load_extension("champion_duel_cog")
        print("[INFO] Champion Duel admin cog loaded")
    # Loaded after every feature cog, so each one has registered its
    # config_health subjects before the first notifier pass can render them.
    if "config_health_cog" not in bot.extensions:
        await bot.load_extension("config_health_cog")
        print("[INFO] Config health cog loaded")
    # The Map Manager command surfaces (`/map_manager` + the /setup button)
    # are gated behind MAP_MANAGER_COMMANDS_ENABLED so the integration can ship
    # to production with its HTTP endpoints live for testing while the commands
    # stay hidden until Map Manager is ready to reveal them (#316/#338). Not
    # loading the cog keeps the command group out of the tree, so the global
    # sync below never publishes it.
    from api_server import map_manager_commands_enabled

    if map_manager_commands_enabled():
        if "mapmanager_cog" not in bot.extensions:
            await bot.load_extension("mapmanager_cog")
            print("[INFO] Map Manager cog loaded")
    else:
        print("[INFO] Map Manager commands hidden (MAP_MANAGER_COMMANDS_ENABLED unset)")

    # Sync slash commands globally so they work in any server. Commands
    # decorated with `guilds=[...]` are excluded from the global sync;
    # they're pushed per-guild below.
    synced = await bot.tree.sync()
    print(f"[INFO] Synced {len(synced)} slash commands globally")

    # Push guild-restricted admin commands to each configured admin guild.
    # If `BOT_ADMIN_GUILD_IDS` is unset, _ADMIN_GUILD_IDS is empty and
    # this loop is a no-op (admin commands fell back to global registration
    # earlier — see the print at module import).
    for gid in _ADMIN_GUILD_IDS:
        try:
            synced_guild = await bot.tree.sync(guild=discord.Object(id=gid))
            print(f"[INFO] Synced {len(synced_guild)} admin command(s) to guild {gid}")
        except discord.HTTPException as e:
            print(f"[INFO] Could not sync admin commands to guild {gid}: {e}")
            sentry_sdk.capture_exception(e)

    # Set the bot's presence to reflect the live guild count.
    await _update_presence()

    # Backfill install metadata for every connected guild. on_guild_join
    # only fires for *new* installs, so without this pass the metadata
    # table stays empty for guilds the bot was already in before this
    # release shipped. Idempotent — the upsert preserves `installed_at`
    # and `installer_user_id` on rows that already have them. We don't
    # try to recover `installer_user_id` from the audit log here: it
    # would mean an API call per guild on every reconnect, and the audit
    # log only retains 45 days anyway.
    #
    # `current_version` is passed so that *first-ever* sightings stamp
    # the row with the running version — that way a fresh install never
    # triggers a "Welcome to vX.Y.Z" announcement on its very next boot.
    # Existing rows ignore the parameter and their `last_seen_version`
    # is preserved (the release-announce handler below owns updates).
    from release_announcements import maybe_post_release_announcement

    for g in bot.guilds:
        try:
            upsert_guild_install_metadata(
                guild_id=g.id,
                guild_name=g.name,
                owner_id=g.owner_id or 0,
                installer_user_id=None,
                current_version=__version__,
            )
        except Exception as e:
            print(f"[GUILD] Could not backfill metadata for {g.name} ({g.id}): {e}")
            sentry_sdk.capture_exception(e)
    print(f"[GUILD] Refreshed install metadata for {len(bot.guilds)} guild(s)")

    # Release-announcement check (#253). Runs after the metadata refresh
    # above so every guild has a `last_seen_version` to compare against.
    # The helper is self-contained per-guild — a single guild's failure
    # never aborts the rest. See `release_announcements.py` for the
    # major/minor comparison + opt-out gating.
    for g in bot.guilds:
        await maybe_post_release_announcement(g, bot, __version__)

    # Support-server #changelog post (#92). Deliberately here rather than
    # in CI: the release workflow runs on merge to main, before Railway
    # has finished deploying, so a post from there could announce a
    # release that then fails to deploy. Posting from the running bot
    # means the version it names is the version actually serving.
    #
    # `on_ready` fires again on every gateway reconnect and redeploy;
    # `maybe_post_changelog` dedupes on the stored version so this isn't
    # a repost each time.
    try:
        from changelog_post import maybe_post_changelog

        result = await maybe_post_changelog(bot, __version__)
        print(f"[CHANGELOG] {result}")
    except Exception as e:
        print(f"[CHANGELOG] Post check failed: {type(e).__name__}: {e}")
        sentry_sdk.capture_exception(e)

    # Re-register persistent storm sign-up Views so their buttons keep
    # working after a restart. Fed from `storm_registration_posts`; safely
    # a no-op until #124 starts writing to that table. See storm_signup_view.
    try:
        from storm_signup_view import register_persistent_signup_views

        register_persistent_signup_views(bot)
    except Exception as e:
        print(f"[STORM SIGNUP] Failed to re-register sign-up views: {e}")
        sentry_sdk.capture_exception(e)

    # Re-register persistent Profession Buddy System Views (#289) so the
    # one-click profession buttons keep working after a restart. Fed from
    # `guild_buddy_config` rows that have a posted self-service message.
    try:
        from buddy_ui import register_persistent_buddy_views

        register_persistent_buddy_views(bot)
    except Exception as e:
        print(f"[BUDDY] Failed to re-register buddy views: {e}")
        sentry_sdk.capture_exception(e)

    # Re-register persistent Alliance Duel (VS) score prompt Views (#405) so
    # yesterday's prompt is still clickable after a redeploy. Fed from
    # `vs_score_prompt_posts`.
    try:
        from alliance_duel_views import register_persistent_vs_views

        register_persistent_vs_views(bot)
    except Exception as e:
        print(f"[VS PROMPT] Failed to re-register score prompt views: {e}")
        sentry_sdk.capture_exception(e)

    # Refresh zone emoji IDs from the bot's own Application Emojis
    # (#177). Each environment (dev, prod) ships its own Discord
    # Application with its own emoji set; the bot reads them at boot
    # so source carries no per-env IDs and storm renders pick up new
    # icons automatically once `scripts/upload_storm_emojis.py` runs.
    # Failure (or no-emojis-yet) falls through to plain-text zone
    # names — never blocks startup.
    try:
        from storm_icons import refresh_zone_emoji_ids

        count = await refresh_zone_emoji_ids(bot)
        print(f"[STORM ICONS] Loaded {count} application emoji ID(s)")
    except Exception as e:
        print(f"[STORM ICONS] Refresh failed: {e}")
        sentry_sdk.capture_exception(e)

    # Only start background tasks once — they persist across reconnects
    if not hasattr(bot, "_tasks_started"):
        bot._tasks_started = True

        # Snapshot the loop heartbeats BEFORE the loops start and overwrite
        # them, so the outage catch-up scan (#227) sees the pre-restart state.
        # init_db() ran earlier in on_ready, so loop_heartbeat exists.
        import outage_catchup

        _catchup_snapshot = outage_catchup.snapshot_heartbeats()
        _catchup_at = datetime.now(timezone.utc)

        bot.loop.create_task(run_scheduler(bot))
        print("[INFO] Event scheduler started")
        growth_task.start()
        print("[INFO] Growth tracker started")
        stats_publish_task.start()
        print("[INFO] Stats publisher started")
        shiny_tasks_refresh_task.start()
        print("[INFO] Shiny tasks weekly refresh started")
        shiny_tasks_post_task.start()
        print("[INFO] Shiny tasks per-minute post loop started")
        try:
            from storm_signup_scheduler import start_storm_signup_scheduler

            start_storm_signup_scheduler(bot)
            print("[INFO] Storm sign-up scheduler started")
        except Exception as e:
            print(f"[STORM SCHEDULER] Failed to start: {e}")
            sentry_sdk.capture_exception(e)

        # Start the internal HTTP API server for the Map Manager integration
        # (#316). Starts when running as a Railway web service (PORT set) or
        # when MAPMANAGER_API_KEY is configured, so the port is bound for
        # Railway's routing + health check. Runs in-process alongside the
        # gateway client on 0.0.0.0:${PORT} — the /members lookup needs the
        # gateway member cache, so the API must NOT be split into a separate
        # service. Railway routes inbound HTTP to it via the `web` Procfile
        # process type. The runner handle is stashed on the bot so it outlives
        # this scope (and so a future shutdown hook can close it).
        try:
            from api_server import api_server_enabled, start_api_server

            if api_server_enabled():
                bot._api_runner = await start_api_server(bot)
                print(f"[API] Internal HTTP API server started on :{os.getenv('PORT', '8080')}")
            else:
                print(
                    "[API] No PORT (not a web deploy) and no MAPMANAGER_API_KEY — "
                    "internal HTTP API server disabled (local dev)"
                )
        except Exception as e:
            print(f"[API] Failed to start internal HTTP API server: {e}")
            sentry_sdk.capture_exception(e)

        # After a short settle delay (guild cache + channels ready), scan the
        # captured heartbeats for an outage and post catch-up digests (#227).
        async def _delayed_outage_catchup():
            await asyncio.sleep(outage_catchup.SETTLE_DELAY_SECONDS)
            try:
                await outage_catchup.run_catchup_scan(bot, _catchup_snapshot, _catchup_at)
            except Exception as e:
                print(f"[CATCHUP] Outage catch-up scan failed: {e}")
                sentry_sdk.capture_exception(e)

        bot.loop.create_task(_delayed_outage_catchup())
        print("[INFO] Outage catch-up scan scheduled")


@bot.event
async def on_guild_join(guild: discord.Guild):
    """When the bot is added to a new server, DM the inviter (or the owner
    if the inviter can't be determined) with a welcome / setup message,
    persist a small operational-metadata record for support triage, and
    refresh the presence count.
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

    # Persist the install metadata row. Owner is always present on guilds
    # the bot is in; inviter is best-effort. See `config.upsert_guild_install_metadata`
    # for the upsert semantics.
    try:
        upsert_guild_install_metadata(
            guild_id=guild.id,
            guild_name=guild.name,
            owner_id=guild.owner_id or 0,
            installer_user_id=inviter.id if inviter else None,
            current_version=__version__,
        )
    except Exception as e:
        print(f"[GUILD] Could not persist install metadata for {guild.name}: {e}")
        sentry_sdk.capture_exception(e)

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
    """Refresh the presence count when the bot is removed from a server,
    and drop the install metadata row so kicked guilds aren't retained.
    """
    print(f"[GUILD] Removed from {guild.name} (ID: {guild.id})")
    try:
        delete_guild_install_metadata(guild.id)
    except Exception as e:
        print(f"[GUILD] Could not clear install metadata for {guild.name}: {e}")
        sentry_sdk.capture_exception(e)
    await _update_presence()


# ── Support-server join watch ────────────────────────────────────────────────
#
# When someone joins the support server, post a hidden-channel notice listing
# which other bot-installed servers they're already in (owner triage aid for
# join-and-spam accounts). The "support server" is whichever guild owns the
# channel set via `/admin set_join_watch` — a single stored value, no env var.
# Registered as a `@bot.listen` (not `@bot.event`) so it coexists with the
# Member Roster cog's own on_member_join listener rather than replacing it.


@bot.listen("on_member_join")
async def support_join_watch_listener(member: discord.Member):
    raw = get_app_setting(support_join_watch.WATCH_CHANNEL_SETTING)
    if not raw:
        return
    try:
        channel_id = int(raw)
    except (TypeError, ValueError):
        return

    channel = bot.get_channel(channel_id)
    # Only act on joins to the *same* guild that owns the watch channel, and
    # only when it's a text channel we can post to. Skip bot joins.
    if (
        channel is None
        or not isinstance(channel, discord.TextChannel)
        or channel.guild.id != member.guild.id
        or member.bot
    ):
        return

    shared = support_join_watch.shared_bot_guilds(bot, member.id, member.guild.id)
    lines = [support_join_watch.format_join_notice(member, shared)]
    verify_line = await _verification_line(member, eligible=bool(shared))
    if verify_line:
        lines.append(verify_line)

    try:
        await channel.send(
            "\n".join(lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        print(
            f"[JOINWATCH] Missing permission to post join notice in "
            f"channel {channel_id} (guild {member.guild.id})"
        )
    except Exception as e:
        print(f"[JOINWATCH] Failed to post join notice: {e}")
        sentry_sdk.capture_exception(e)


async def _try_assign_verified(member: discord.Member, role: discord.Role) -> tuple[bool, str]:
    """Give `member` the Verified `role`. Returns (ok, note):
      - (True, "Assigned")    — role was added just now
      - (True, "Already had") — member already had it (no-op)
      - (False, <reason>)     — couldn't assign; reason is user-facing

    Checks the permission/hierarchy blocker first (a cheap, cache-only check)
    so a misconfigured server produces a clear reason rather than a raw
    Forbidden, then falls back to catching Forbidden/HTTP errors from the API.
    """
    if role in member.roles:
        return True, "Already had"

    me = member.guild.me
    blocker = support_join_watch.verified_role_blocker(
        has_manage_roles=me.guild_permissions.manage_roles,
        bot_top_position=me.top_role.position,
        role_position=role.position,
        role_managed=role.managed,
    )
    if blocker:
        return False, blocker

    try:
        await member.add_roles(role, reason="Auto-verified: shares a bot-installed server")
        return True, "Assigned"
    except discord.Forbidden:
        return False, "missing permission (Forbidden)"
    except discord.HTTPException as e:
        return False, f"Discord error ({e.status})"


bot_state.try_assign_verified = _try_assign_verified


async def _verification_line(member: discord.Member, *, eligible: bool) -> str | None:
    """Build the auto-verify status line appended to a join notice, or None when
    auto-verify isn't configured. `eligible` = the member shares ≥1 bot server.
    """
    raw_role = get_app_setting(support_join_watch.VERIFIED_ROLE_SETTING)
    if not raw_role:
        return None  # auto-verify off
    role = member.guild.get_role(int(raw_role)) if raw_role.isdigit() else None
    if role is None:
        return (
            "⚠️ Configured Verified role no longer exists — set a new one with "
            "`/admin verify role:@Role`."
        )
    if not eligible:
        return f"🔒 Not auto-verified (in no other bot-installed server); **{role.name}** withheld."

    ok, note = await _try_assign_verified(member, role)
    if ok:
        return f"✅ {note} **{role.name}**."
    return f"⚠️ Qualifies for **{role.name}** but I couldn't assign it: {note}."


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
            "Try running `/setup` and clicking **🗂️ View configuration** to check that all your configured channels and roles "
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
    from config import get_active_guild_configs, get_growth_config

    now = datetime.now(tz=ET)

    try:
        # Off the event loop (#366) — was a raw sqlite3.connect(DB_PATH)
        # directly in this coroutine; now routed through config's own
        # connection helper, shared with scheduler.py's main loop.
        active_configs = await asyncio.to_thread(get_active_guild_configs)
        guild_ids = [c.guild_id for c in active_configs]
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

            epoch = _date(2026, 1, 1)
            delta = (_date.today() - epoch).days
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
                from config import describe_sheet_error, is_user_config_sheet_error

                if is_user_config_sheet_error(e):
                    # Deleted sheet / revoked access / missing tab is the
                    # alliance's to fix — log it with guild context, but don't
                    # page Sentry (regressions of #285 / #286).
                    print(f"[GROWTH] Skipping guild {gid}: {describe_sheet_error(e, guild_id=gid)}")
                    sentry_sdk.add_breadcrumb(
                        category="growth",
                        message=describe_sheet_error(e, guild_id=gid),
                        level="info",
                    )
                else:
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


# ── Shiny Tasks scheduler loops ──────────────────────────────────────────────
#
# Two background loops drive the daily shiny-tasks announcement:
#
#   * `shiny_tasks_refresh_task` (weekly) keeps `shiny_task_servers`
#     current with new Last War launches and ages out servers absent
#     from cpt-hedge's table. Also seeds the table on first startup
#     when it's empty.
#
#   * `shiny_tasks_post_task` (per minute) walks every enabled guild
#     and posts the daily announcement when wall-clock time in the
#     guild's timezone matches the configured `post_time`.
#
# Both loops emit failures to Sentry but never raise — a transient
# Hedge outage or one misconfigured guild must not abort the loop for
# everyone else.


@tasks.loop(hours=24 * 7)
async def shiny_tasks_refresh_task():
    """Weekly: refresh `shiny_task_servers` from cpt-hedge.

    `tasks.loop` fires its body immediately on `.start()` and the 7-day
    interval is in-process only, so a fresh process (Railway redeploy)
    would re-fetch Hedge on every boot. Gate on the persistent
    `MAX(last_seen_at)` so we genuinely refresh at most once per week
    regardless of redeploy cadence.
    """
    try:
        from datetime import datetime, timedelta, timezone
        from config import get_last_shiny_refresh_at
        from shiny_tasks import refresh_servers

        last = get_last_shiny_refresh_at()
        if last is not None:
            age = datetime.now(tz=timezone.utc) - last
            if age < timedelta(days=7):
                hours = int(age.total_seconds() // 3600)
                print(f"[SHINY] Weekly refresh skipped — last run {age.days}d{hours % 24}h ago")
                return
        n = await refresh_servers()
        print(f"[SHINY] Weekly refresh upserted {n} server rows")
    except Exception as e:
        print(f"[SHINY] Weekly refresh failed: {e}")
        sentry_sdk.capture_exception(e)


@shiny_tasks_refresh_task.before_loop
async def before_shiny_tasks_refresh_task():
    await bot.wait_until_ready()
    # First-run seed: if the table is empty (fresh install), pull the
    # full set right away rather than waiting up to 7 days for the
    # first scheduled refresh. Wrapped in its own try so a Cloudflare
    # hiccup at startup doesn't crash the loop's launch.
    try:
        from config import count_shiny_task_servers
        from shiny_tasks import refresh_servers

        if count_shiny_task_servers() == 0:
            n = await refresh_servers()
            print(f"[SHINY] Initial seed upserted {n} server rows")
    except Exception as e:
        print(f"[SHINY] Initial seed failed: {e}")
        sentry_sdk.capture_exception(e)


# Guards the at-or-past retry (below) from re-attempting a guild that keeps
# failing (channel not resolvable, missing permission) on every single tick
# for the rest of the day. Keyed by guild_id, reset implicitly on restart —
# that's fine, a restart just means one immediate retry rather than a
# missed one. Not persisted; in-memory is enough since the only cost of
# losing it is one extra attempt.
_SHINY_RETRY_COOLDOWN = timedelta(minutes=5)
_shiny_last_attempt: dict[int, datetime] = {}

# The retry above is right — a permission fix should take effect the same day
# rather than tomorrow — but logging every attempt is not. A channel an
# alliance has stopped maintaining stays broken for days, so at one line per
# guild per retry a handful of them bury every other log this bot writes.
# Leadership already hears about it properly (the config-health digest, #379),
# so the log only owes the operator "this is still true", not a running count.
_SHINY_UNUSABLE_LOG_COOLDOWN = timedelta(hours=6)
_shiny_last_unusable_log: dict[int, datetime] = {}

# #379: the configured Shiny Tasks post channel, as a config-health subject.
# Registered here because this loop is the only thing that reads it, and
# bot.py's loops deliberately stayed in this module (#372).
SHINY_POST_CHANNEL_SUBJECT = "shiny.post_channel"

# Imported rather than duplicated so a rename of the hub button stays one line.
from setup_hub import HUB_BTN_SHINY as _HUB_BTN_SHINY  # noqa: E402

config_health.register(
    config_health.Subject(
        key=SHINY_POST_CHANNEL_SUBJECT,
        label="your Daily Shiny Tasks channel",
        fix_hub="/setup",
        fix_btn=_HUB_BTN_SHINY,
    )
)


@tasks.loop(minutes=1)
async def shiny_tasks_post_task():
    """Per-minute: walk enabled guilds, post if their configured
    post_time matches wall-clock now in their timezone."""
    from config import (
        get_config,
        get_shiny_tasks_config,
        get_shiny_task_servers_in_range,
        list_shiny_enabled_guild_ids,
        mark_shiny_tasks_posted,
        server_date_for,
        stamp_loop_heartbeat,
    )
    from shiny_tasks import build_announcement_for_guild

    try:
        enabled_ids = list_shiny_enabled_guild_ids()
    except Exception as e:
        print(f"[SHINY] Could not list enabled guilds: {e}")
        sentry_sdk.capture_exception(e)
        return

    for gid in enabled_ids:
        try:
            cfg = get_config(gid)
            scfg = get_shiny_tasks_config(gid)
            if not cfg or not scfg.get("enabled"):
                continue

            # Time match: HH:MM in the guild's configured timezone.
            try:
                guild_tz = ZoneInfo(cfg.timezone or "America/New_York")
            except Exception:
                guild_tz = ET
            guild_now = datetime.now(tz=guild_tz)
            try:
                hh, mm = scfg["post_time"].split(":")
                hh, mm = int(hh), int(mm)
            except (KeyError, ValueError, AttributeError):
                continue
            # At-or-past match rather than an exact hour/minute equality — an
            # exact match silently misses the entire day if this tick lands
            # even a minute late (e.g. the event loop was busy with another
            # guild's blocking Sheets call right at the target minute; the
            # `last_posted_date` dedup below already prevents a late tick
            # from double-posting once today's message has gone out).
            target_today = guild_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if guild_now < target_today:
                continue

            # Throttle retries for a guild that's overdue but keeps failing
            # (bad channel, missing permission) — without this, at-or-past
            # matching means it would retry every single tick for the rest
            # of the day instead of once every few minutes.
            last_attempt = _shiny_last_attempt.get(gid)
            if last_attempt is not None and (guild_now - last_attempt) < _SHINY_RETRY_COOLDOWN:
                continue
            _shiny_last_attempt[gid] = guild_now

            today_iso = guild_now.date().isoformat()
            if scfg.get("last_posted_date") == today_iso:
                # Already fired today — Railway restart inside the
                # configured minute, or the loop somehow ran twice.
                continue

            # #379: this is the branch that started the ticket. Several
            # alliances had their configured channel go unreachable in a
            # channel reorg, and the loop skipped them silently for days with
            # nothing but this print. Now it also records, so the config-health
            # digest tells leadership and the /setup screen shows it.
            channel = config_health.resolve_configured_channel(
                bot, gid, SHINY_POST_CHANNEL_SUBJECT, scfg.get("channel_id") or 0
            )
            if channel is None:
                last_log = _shiny_last_unusable_log.get(gid)
                if last_log is None or (guild_now - last_log) >= _SHINY_UNUSABLE_LOG_COOLDOWN:
                    _shiny_last_unusable_log[gid] = guild_now
                    print(
                        f"[SHINY] Channel {scfg.get('channel_id')} not usable "
                        f"for guild {gid} — skipping post (further attempts "
                        f"logged at most every {_SHINY_UNUSABLE_LOG_COOLDOWN})"
                    )
                continue
            _shiny_last_unusable_log.pop(gid, None)

            # Resolve the shiny cycle against the Last War in-game (server,
            # UTC-2) date, not the guild's local calendar date. The post can
            # fire after the in-game reset (00:00 server time, ~2h before local
            # midnight) — e.g. a 10:30pm local post — at which point the local
            # date still names the in-game day that just ended, so the cycle
            # would list yesterday's shiny servers (#330). Same class of bug as
            # the train "day behind" fix (#318). See config.server_date_for.
            shiny_today = server_date_for(guild_now)
            rows = get_shiny_task_servers_in_range(
                int(scfg.get("server_min") or 0),
                int(scfg.get("server_max") or 0),
            )
            body = build_announcement_for_guild(
                server_rows=rows,
                server_min=int(scfg.get("server_min") or 0),
                server_max=int(scfg.get("server_max") or 0),
                today=shiny_today,
                template=scfg.get("message_template") or "",
            )
            if body is None:
                # No shinies in range today — record the date anyway so
                # we don't recheck (cheaply) every minute for the rest
                # of the matched minute, and so a /view_configuration
                # reader can see the loop fired.
                print(
                    f"[SHINY] No shinies in range {scfg.get('server_min')}-"
                    f"{scfg.get('server_max')} for guild {gid} on {shiny_today} "
                    f"(local date {today_iso}) — marking posted without sending"
                )
                mark_shiny_tasks_posted(gid, today_iso)
                continue

            print(
                f"[SHINY] Sending post for guild {gid} to channel {channel.id} "
                f"({getattr(channel, 'name', '?')}), body_len={len(body)}"
            )
            try:
                sent = await channel.send(body)
                print(
                    f"[SHINY] Sent message id={sent.id} for guild {gid} "
                    f"at {sent.created_at.isoformat()} jump_url={sent.jump_url}"
                )
                mark_shiny_tasks_posted(gid, today_iso)
            except discord.Forbidden:
                print(
                    f"[SHINY] Missing send permission in channel "
                    f"{channel.id} ({getattr(channel, 'name', '?')}) "
                    f"for guild {gid}"
                )
                # The pre-flight said we could post, so the permission changed
                # between the check and the send, or an overwrite the cache
                # hadn't caught up on. Record it either way.
                config_health.record(
                    gid,
                    SHINY_POST_CHANNEL_SUBJECT,
                    config_health.CHANNEL_NO_SEND,
                    "",
                    discriminator=str(channel.id),
                )
            except discord.HTTPException as e:
                print(f"[SHINY] HTTP error posting for guild {gid}: {e}")
                sentry_sdk.capture_exception(e)
        except Exception as e:
            print(f"[SHINY] Per-minute loop error for guild {gid}: {e}")
            sentry_sdk.capture_exception(e)

    # Mark a clean tick so the outage catch-up scan (#227) can tell this
    # loop was alive up to now. Per-guild failures above are isolated and
    # don't count as an outage.
    stamp_loop_heartbeat("shiny_post")


@shiny_tasks_post_task.before_loop
async def before_shiny_tasks_post_task():
    await bot.wait_until_ready()


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    await bot.process_commands(message)


@bot.event
async def on_command_error(ctx, error):
    # This bot is slash-only — `command_prefix="!"` exists only because
    # `commands.Bot` requires one. Every `!something` typed in a server
    # the bot shares with Dyno/MEE6/etc. (which also use `!`) dispatches
    # here and, with no matching command, logs at ERROR by default —
    # filling Railway logs and Sentry with noise. Swallow CommandNotFound
    # specifically; re-raise anything else so a future prefix command's
    # bugs still surface.
    if isinstance(error, commands.CommandNotFound):
        return
    raise error


# ── /growth command group ─────────────────────────────────────────────────────
#
# `/growth overview` keeps the embed + action buttons leadership has
# always reached via bare `/growth`. The new `/growth breakdown` leaf
# surfaces the bucket-classification view that previously lived only
# behind the "📊 See most recent Breakdown" button on the overview
# embed — so officers can jump straight to it from the slash picker.

growth_group = app_commands.Group(
    name="growth",
    description="Member-growth snapshots and bucket breakdown",
)


@growth_group.command(
    name="overview",
    description="Growth tracking status; buttons for snapshot, breakdown, and config edit",
)
async def growth_slash(interaction: discord.Interaction):
    if not await guard(interaction):
        return
    from config import get_growth_config

    guild_id = interaction.guild_id
    gcfg = get_growth_config(guild_id)

    metrics = gcfg.get("metrics") or []
    freq = gcfg.get("snapshot_frequency", "monthly")
    sched = (
        f"Monthly on day {gcfg.get('snapshot_day', 1)}"
        if freq == "monthly"
        else f"Every {gcfg.get('snapshot_interval', 30)} days"
    )
    enabled = bool(gcfg.get("enabled"))

    embed = discord.Embed(
        title="📈 Growth Tracking",
        color=discord.Color.green() if enabled else discord.Color.greyple(),
    )
    embed.add_field(name="Status", value="✅ Enabled" if enabled else "❌ Disabled", inline=False)
    embed.add_field(name="Source Tab", value=gcfg.get("tab_source", "*not set*"), inline=False)
    embed.add_field(name="Growth Tab", value=gcfg.get("tab_growth", "*not set*"), inline=False)
    embed.add_field(name="Snapshot", value=sched, inline=False)

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
        value=(
            "\n".join(f"• **{m['label']}** — column {m['col']}" for m in metrics)
            or "*none configured*"
        )[:1024],
        inline=False,
    )

    class GrowthActionView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            if not enabled:
                self.run_now.disabled = True
                self.breakdown.disabled = True

        @discord.ui.button(label="📸 Run Snapshot Now", style=discord.ButtonStyle.success)
        async def run_now(self, inter: discord.Interaction, button: discord.ui.Button):
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            try:
                from growth import _run_growth_snapshot_inner

                await asyncio.to_thread(_run_growth_snapshot_inner, guild_id)
                await inter.followup.send(
                    f"✅ Growth snapshot complete — check the **{gcfg.get('tab_growth', 'Growth Tracking')}** tab.",
                    ephemeral=True,
                )
            except Exception as e:
                await inter.followup.send(f"⚠️ Growth snapshot failed: {e}", ephemeral=True)
            self.stop()

        @discord.ui.button(
            label="📊 See most recent Breakdown", style=discord.ButtonStyle.secondary
        )
        async def breakdown(self, inter: discord.Interaction, button: discord.ui.Button):
            # Read-only render. Don't disable sibling buttons and don't
            # `self.stop()` — leadership might want to follow the no-data
            # message's own advice and click **Run Snapshot Now**, or
            # re-click Breakdown after a snapshot completes. (#84)
            await inter.response.defer(ephemeral=True)
            try:
                from growth import read_latest_breakdown, format_breakdown_embed

                data = await asyncio.to_thread(read_latest_breakdown, guild_id)
            except Exception as e:
                await inter.followup.send(f"⚠️ Could not load breakdown: {e}", ephemeral=True)
                return
            if not data.get("has_data"):
                await inter.followup.send(
                    "📊 No breakdown data yet — click **📸 Run Snapshot Now** "
                    "above (or wait for the next scheduled snapshot). The "
                    "breakdown classifies each member's percent change between "
                    "snapshots, so it needs at least two snapshots' worth of "
                    "data before any classification can render.",
                    ephemeral=True,
                )
                return
            embed = format_breakdown_embed(
                metric_labels=data["metric_labels"],
                breakdown_summary=data["summary"],
                prev_period_label=data["prev_period_label"],
                curr_period_label=data["curr_period_label"],
                label_overrides=gcfg.get("breakdown_labels") or {},
            )
            await inter.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="⚙️ Edit Config", style=discord.ButtonStyle.primary)
        async def edit_config(self, inter: discord.Interaction, button: discord.ui.Button):
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            from setup_cog import (
                _has_leadership_or_admin,
                _check_wizard_can_run,
                run_growth_setup,
            )

            if not _has_leadership_or_admin(inter):
                await inter.followup.send(
                    "⛔ You need the leadership role (or admin) to edit growth tracking config.",
                    ephemeral=True,
                )
                self.stop()
                return
            if not await _check_wizard_can_run(inter, "setup_growth"):
                self.stop()
                return
            await inter.followup.send(
                "⚙️ Starting growth tracking setup — check the channel for prompts!",
                ephemeral=True,
            )
            self.stop()
            await run_growth_setup(inter, bot)

    await interaction.response.send_message(embed=embed, view=GrowthActionView(), ephemeral=True)


@growth_group.command(
    name="breakdown",
    description="Most-recent bucket breakdown (Increased / Steady / Low / None / Decline)",
)
async def growth_breakdown_slash(interaction: discord.Interaction):
    if not await guard(interaction):
        return
    from config import get_growth_config
    from growth import read_latest_breakdown, format_breakdown_embed

    guild_id = interaction.guild_id
    gcfg = get_growth_config(guild_id)

    await interaction.response.defer(ephemeral=True)
    try:
        data = await asyncio.to_thread(read_latest_breakdown, guild_id)
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Could not load breakdown: {e}",
            ephemeral=True,
        )
        return

    if not data.get("has_data"):
        await interaction.followup.send(
            "📊 No breakdown data yet. Run `/growth overview` and click "
            "**📸 Run Snapshot Now** (or wait for the next scheduled "
            "snapshot). The breakdown classifies each member's percent "
            "change between snapshots, so it needs at least two snapshots' "
            "worth of data before any classification can render.",
            ephemeral=True,
        )
        return

    embed = format_breakdown_embed(
        metric_labels=data["metric_labels"],
        breakdown_summary=data["summary"],
        prev_period_label=data["prev_period_label"],
        curr_period_label=data["curr_period_label"],
        label_overrides=gcfg.get("breakdown_labels") or {},
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


# Register the /growth Group on the tree once every subcommand has
# been attached above. Global registration.
bot.tree.add_command(growth_group)


# ── /events hub command ───────────────────────────────────────────────────────
#
# Single top-level command that opens an events hub (embed + button grid)
# the same way /desertstorm and /canyonstorm do for storms. Replaced the
# /events overview|show|log subcommand group plus the event-list management
# step of /setup → 📣 Events (#249). Every event flow now lives behind a
# hub button.


@bot.tree.command(
    name="events",
    description="Open the event-announcements hub for this alliance",
)
@app_commands.guild_only()
async def events_slash(interaction: discord.Interaction):
    from events_hub import handle_events_hub

    await handle_events_hub(bot, interaction)


# ── /help command ──────────────────────────────────────────────────────────────


@bot.tree.command(
    name="help",
    description="Show all available bot commands",
)
async def help_slash(interaction: discord.Interaction):
    import premium
    from help_content import build_overview_embed, HelpView

    is_premium_flag = await premium.is_premium(
        interaction.guild_id,
        interaction=interaction,
        bot=bot,
    )
    embed = build_overview_embed(is_premium_flag)
    view = HelpView(is_premium_flag, origin=interaction)
    await interaction.response.send_message(
        embed=embed,
        view=view,
        ephemeral=True,
    )


# The owner-only /admin diagnostic toolkit lives in its own module (#372) --
# imported here (after `bot`/`ET`/`_try_assign_verified` already exist in
# this module's namespace) so it can register itself on the tree. Also
# pulls in `_ADMIN_GUILD_IDS`, which on_ready's per-guild sync loop above
# needs (only resolved at call time, long after this import runs).
from bot_admin import _ADMIN_GUILD_IDS  # noqa: E402,F401


# Guard the runtime entry so `import bot` doesn't try to start the bot
# (which is what tests need to do — they import this module to walk the
# tree-registered commands without booting the real Discord client).
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)

"""alliance_duel_cog.py — registers the `/vs` command for the Alliance Duel
(VS) tracker (#402), and the clock-driven loop behind its scheduled posts
(#405).

`/vs` is a single flat hub command opening an embed plus button grid via
`alliance_duel_hub.handle_vs_hub`, not an `app_commands.Group` with named
subcommands: the design settles the hub pattern for a feature this stateful,
matching `/buddy`, `/train`, `/events`, `/transfers` and `/map_manager`.

The loop follows the `check_rotation` precedent in `train_cog.py`: a per-minute
tick, per-guild try/except so one misconfigured alliance cannot stop the rest,
DB-backed dedup, and a heartbeat stamped at the end of every clean pass so
`outage_catchup` can tell a redeploy from an outage. The member day-theme
reminder (#406) lands on the same tick.
"""

import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger(__name__)

#: The heartbeat this loop stamps. Mirrored in `outage_catchup.HEARTBEAT_LOOPS`.
VS_POSTS_HEARTBEAT = "vs_score_prompt"

_DEFAULT_TZ = "America/New_York"


class AllianceDuelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_vs_posts.start()

    def cog_unload(self):
        self.check_vs_posts.cancel()

    @app_commands.command(
        name="vs",
        description="Alliance Duel (VS): bracket, this week, and scouting for this alliance",
    )
    @app_commands.guild_only()
    async def vs(self, interaction: discord.Interaction):
        from alliance_duel_hub import handle_vs_hub

        await handle_vs_hub(self.bot, interaction)

    # ── Scheduled posts ───────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_vs_posts(self):
        """Per-minute tick driving the VS scheduled posts.

        Every surface is gated on its own opt-in, so a guild that wants one and
        not the other gets exactly that.

        **The two surfaces gate differently on the tracker itself.** The score
        prompt reads the sheet, so it needs the tracker set up and Premium. The
        member day-theme reminder (#406) reads nothing at all: it renders from
        the fixed day table, ships free, and is available to an alliance that
        has never opened the tracker. Requiring `enabled` for it would be
        gating a free surface behind a Premium one.
        """
        import config

        for guild in self.bot.guilds:
            try:
                cfg = config.get_config(guild.id)
                if not cfg or not cfg.setup_complete:
                    continue
                vs_cfg = config.get_vs_config(guild.id)
                try:
                    guild_tz = ZoneInfo(cfg.timezone or _DEFAULT_TZ)
                except ZoneInfoNotFoundError:
                    guild_tz = ZoneInfo(_DEFAULT_TZ)
                from datetime import datetime

                guild_now = datetime.now(tz=guild_tz)
                if vs_cfg.get("enabled"):
                    await self._maybe_post_score_prompt(guild, vs_cfg, guild_now)
                await self._maybe_post_day_theme(guild, vs_cfg, guild_now)
            except Exception as e:  # noqa: BLE001 - one guild must not sink the tick
                logger.exception("[VS POSTS] tick failed for guild %s: %s", guild.id, e)

        config.stamp_loop_heartbeat(VS_POSTS_HEARTBEAT)

    async def _maybe_post_score_prompt(self, guild, vs_cfg, guild_now) -> None:
        """Ask for the day that just ended, once, in the alliance's channel."""
        import config

        if not vs_cfg.get("score_prompt_enabled") or not vs_cfg.get("score_prompt_channel_id"):
            return
        hh, mm = _hm(vs_cfg.get("score_prompt_time"), "")
        if hh is None or guild_now.hour != hh or guild_now.minute != mm:
            return

        import alliance_duel as ad

        target = ad.completed_duel_day(guild_now)
        if target is None:
            return  # server Monday: yesterday was the rest day, nothing to ask
        day_date, day = target

        # Marked before the work, not after: one attempt per duel day, whatever
        # happens below. A retry loop against a broken sheet would re-post the
        # prompt every minute for the rest of the hour.
        if vs_cfg.get("last_score_prompt_fired") == day_date.isoformat():
            return
        config.save_vs_config(guild.id, last_score_prompt_fired=day_date.isoformat())

        await post_score_prompt(self.bot, guild, vs_cfg, day_date, day)

    async def _maybe_post_day_theme(self, guild, vs_cfg, guild_now) -> None:
        """Tell members what today rewards, once, in the alliance's channel.

        Today's day, not yesterday's: the score prompt asks about numbers that
        already exist, while this one is a call to action for the day that is
        running. Monday through Saturday, since Sunday scores nothing.
        """
        import config

        if not vs_cfg.get("day_theme_enabled") or not vs_cfg.get("day_theme_channel_id"):
            return
        hh, mm = _hm(vs_cfg.get("day_theme_time"), "")
        if hh is None or guild_now.hour != hh or guild_now.minute != mm:
            return

        import alliance_duel as ad

        today = ad.server_today(guild_now)
        day = ad.duel_day_for_date(today)
        if day is None:
            return  # Sunday, nothing to spend anything on

        if vs_cfg.get("last_day_theme_fired") == today.isoformat():
            return
        config.save_vs_config(guild.id, last_day_theme_fired=today.isoformat())

        await post_day_theme(self.bot, guild, vs_cfg, day)

    @check_vs_posts.before_loop
    async def before_check_vs_posts(self):
        await self.bot.wait_until_ready()


def _hm(value: str, default: str) -> tuple[int | None, int]:
    """Parse 'HH:MM' → (hour, minute), or (None, 0) when there is nothing
    usable. Unlike the train equivalent there is no fallback hour: the bot
    never picks a posting time on the alliance's behalf, so an unset or
    unparseable time means no post rather than a post at some hour nobody
    chose."""
    try:
        h, m = (value or default).split(":")
        h, m = int(h), int(m)
    except (ValueError, AttributeError):
        return None, 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None, 0
    return h, m


async def post_score_prompt(bot, guild, vs_cfg, day_date, day: int, *, force: bool = False) -> bool:
    """Post one day's score prompt. True when a message actually landed.

    Shared by the loop and by `outage_catchup`, so a prompt missed while the
    bot was down is recovered by the same code that posts it normally rather
    than by a second implementation that can drift from this one.

    Silent returns are all cases where there is genuinely nothing to ask:
    no league covers the day, the alliance is not identified yet, the score is
    already recorded, or the sheet cannot be read (already reported through
    `config_health` by the reader). `force` skips the already-recorded check,
    which the catch-up path does not want.
    """
    import alliance_duel as ad
    import alliance_duel_hub as ad_hub
    import alliance_duel_setup as ad_setup
    import alliance_duel_views as ad_views
    import config
    import config_health
    import premium

    if not await premium.feature_gate("alliance_duel_vs", guild.id, bot=bot):
        return False

    try:
        rows = await ad_hub.read_tab_once(guild.id, vs_cfg)
    except Exception as e:  # noqa: BLE001 - a bot bug, not the alliance's to fix
        logger.exception("[VS PROMPT] sheet read failed for guild %s: %s", guild.id, e)
        return False
    if not rows:
        return False

    state = ad_hub.HubState(guild.id, vs_cfg, rows)
    if state.own is None:
        return False

    # Resolved against the date of the day being asked about, not today: at
    # 1am on Monday the live week is still last week's, and on Sunday the
    # week resolves back to the Monday that started it.
    live = ad.resolve_live_week(rows, today=day_date)
    if live is None:
        return False  # between leagues, or the week was never recorded

    row = state.row_for(state.own, live.week)
    if not force and row is not None and row.day_scores.get(day) is not None:
        return False  # already logged from the hub or typed into the sheet

    channel = config_health.resolve_configured_channel(
        bot, guild.id, ad_setup.VS_POST_CHANNEL_SUBJECT, vs_cfg.get("score_prompt_channel_id")
    )
    if channel is None:
        logger.info(
            "[VS PROMPT] channel %s not usable for guild %s, prompt skipped",
            vs_cfg.get("score_prompt_channel_id"),
            guild.id,
        )
        return False

    opponent = state.own_match(live.week)
    view = ad_views.ScorePromptView(guild.id, live.week, day)
    try:
        message = await channel.send(
            embed=ad_views.score_prompt_embed(state, live.week, day, opponent), view=view
        )
    except discord.Forbidden:
        logger.info("[VS PROMPT] missing perms to post in %s for guild %s", channel.id, guild.id)
        return False

    config.record_vs_score_prompt_post(
        guild.id, channel.id, message.id, live.league, live.week, day, day_date.isoformat()
    )
    logger.info("[VS PROMPT] posted day %s prompt for guild %s (%s)", day, guild.id, day_date)
    return True


async def post_day_theme(bot, guild, vs_cfg, day: int) -> bool:
    """Post one day's member reminder. True when a message actually landed.

    Shared by the loop and by `outage_catchup`, for the same reason the score
    prompt's post path is: recovery that reimplements posting drifts from it.

    No Premium gate and no sheet read. This surface renders from the fixed day
    table alone, which is what makes it free, and it is the only VS surface a
    guild can run without the tracker set up.
    """
    import alliance_duel_setup as ad_setup
    import alliance_duel_views as ad_views
    import config_health

    channel = config_health.resolve_configured_channel(
        bot, guild.id, ad_setup.VS_POST_CHANNEL_SUBJECT, vs_cfg.get("day_theme_channel_id")
    )
    if channel is None:
        logger.info(
            "[VS DAY THEME] channel %s not usable for guild %s, reminder skipped",
            vs_cfg.get("day_theme_channel_id"),
            guild.id,
        )
        return False

    try:
        await channel.send(embed=ad_views.day_theme_embed(day, vs_cfg.get("day_theme_note") or ""))
    except discord.Forbidden:
        logger.info("[VS DAY THEME] missing perms to post in %s for guild %s", channel.id, guild.id)
        return False

    logger.info("[VS DAY THEME] posted day %s reminder for guild %s", day, guild.id)
    return True


async def setup(bot):
    await bot.add_cog(AllianceDuelCog(bot))

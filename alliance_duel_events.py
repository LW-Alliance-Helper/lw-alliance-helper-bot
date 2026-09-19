"""Alliance Duel (VS) — the posts that fire on data rather than on a clock (#409).

Three announcements, each independently opt-in, all sharing one channel:

- **Live clinch status**, when a day outcome lands. The clinch arithmetic
  applied to the week actually in progress, which is the most actionable thing
  the tracker can say mid-week and the input to a mid-week push or save call.
- **Opponent reveal**, when next week's rows are written and the matchup is
  known. Carries the head to head record and the projection, so nobody has to
  go and look them up.
- **Season recap**, when the last week of a league is recorded.

Two properties hold across all three, and both come from the same fact: these
fire off a *write*, and a write can happen twice. An officer correcting a
mistyped score re-saves the same day.

**Dedup is durable, per event.** `vs_event_posts` keys on (guild, kind, event
key) so a correction cannot repost, and neither can a redeploy. In-memory
dedup would fail on both.

**A failure here never breaks the write.** Every entry point swallows its own
errors: the score save is what the officer asked for, and an announcement they
opted into is not worth failing that over. The channel problem is recorded
through `config_health` so it still surfaces, in the place the alliance looks
for broken configuration.
"""

from __future__ import annotations

import logging

import discord

import alliance_duel as ad
import alliance_duel_analytics as an
import alliance_duel_setup as ad_setup
import config
import config_health

logger = logging.getLogger(__name__)

#: Dedup kinds, and the shape of each one's event key.
KIND_CLINCH = "clinch"  # league|week|day
KIND_REVEAL = "reveal"  # league|week
KIND_RECAP = "recap"  # league


def _league_key(league: ad.LeagueKey | None) -> str:
    return f"{league.season}|{league.tier}|{league.group}" if league else "?"


async def _post(bot, state, kind: str, event_key: str, embed: discord.Embed) -> bool:
    """Send one event post, once. True when a message actually landed."""
    if config.vs_event_already_posted(state.guild_id, kind, event_key):
        return False

    channel = config_health.resolve_configured_channel(
        bot,
        state.guild_id,
        ad_setup.VS_POST_CHANNEL_SUBJECT,
        state.cfg.get("event_posts_channel_id"),
    )
    if channel is None:
        logger.info(
            "[VS EVENTS] channel not usable for guild %s, %s post skipped", state.guild_id, kind
        )
        return False

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        logger.info("[VS EVENTS] missing perms in %s for guild %s", channel.id, state.guild_id)
        return False

    # Marked only after the message lands, so a permissions problem the
    # alliance then fixes does not cost them the post entirely.
    config.mark_vs_event_posted(state.guild_id, kind, event_key)
    return True


# ── Live clinch status ────────────────────────────────────────────────────────


def clinch_embed(state, week: int) -> discord.Embed | None:
    """Where the week stands and what would settle it.

    Returns None when the week says nothing worth posting: nothing recorded
    yet, or an opponent we cannot name. The clinch arithmetic itself lives in
    `alliance_duel.clinch_state`, so this surface and the score
    acknowledgement can never disagree about who is winning.
    """
    row = state.row_for(state.own, week) if state.own else None
    if row is None or not row.day_outcomes:
        return None

    clinch = ad.clinch_state(row.day_outcomes)
    opponent = row.opponent
    versus = f" against {state.display_name(opponent)}" if opponent else ""

    if clinch.clinched:
        embed = discord.Embed(
            title=f"🏆 Week {week} is ours",
            description=f"**{clinch.own_points}-{clinch.opponent_points}**{versus}.",
            color=discord.Color.green(),
        )
        return embed
    if clinch.lost:
        # Red is for broken configuration, not for bad news about a game. A
        # week that has gone is still just the week's state.
        embed = discord.Embed(
            title=f"Week {week} has gone",
            description=(
                f"**{clinch.own_points}-{clinch.opponent_points}**{versus}. "
                "The remaining days are still worth points for the season."
            ),
            color=discord.Color.blurple(),
        )
        return embed

    embed = discord.Embed(
        title=f"Week {week}: {clinch.own_points}-{clinch.opponent_points}",
        description=f"Still live{versus}. {clinch.points_needed} points to take the week.",
        color=discord.Color.blurple(),
    )
    remaining = [
        f"day {d} ({ad.DUEL_DAY_BY_NUMBER[d].theme}, {ad.DUEL_DAY_BY_NUMBER[d].points} pts)"
        for d in clinch.remaining_days
    ]
    if remaining:
        embed.add_field(name="Left to play", value=", ".join(remaining), inline=False)
    if clinch.clinching_days:
        days = ", ".join(f"day {d}" for d in clinch.clinching_days)
        embed.add_field(
            name="What settles it", value=f"Winning {days} clinches the week.", inline=False
        )
    return embed


async def after_day_recorded(bot, state, week: int, day: int) -> bool:
    """Post the live clinch status, once per day, if the alliance opted in."""
    if not state.cfg.get("clinch_status_enabled"):
        return False
    embed = clinch_embed(state, week)
    if embed is None:
        return False
    key = f"{_league_key(state.league)}|{week}|{day}"
    return await _post(bot, state, KIND_CLINCH, key, embed)


# ── Opponent reveal ───────────────────────────────────────────────────────────


def reveal_embed(state, week: int) -> discord.Embed | None:
    """Next week's matchup with everything already known about them.

    The scout report is the point: an opponent named without their record and
    their projection is an announcement that immediately sends someone to go
    and look, which is the work this was supposed to save.
    """
    opponent = state.own_match(week)
    if opponent is None or state.own is None:
        return None

    embed = discord.Embed(
        title=f"🆚 Week {week}: {state.display_name(opponent)}"[:256],
        color=discord.Color.blurple(),
    )

    # The week being revealed has not been played, so its own row must not
    # count as a meeting. Left in, a first-ever encounter reads as "0-0 across
    # 1 meeting", which is both wrong and exactly backwards: it looks like
    # history where there is none.
    prior = [r for r in state.rows if not (r.league == state.league and r.week == week)]
    history = ad.head_to_head(prior, state.own, opponent)
    if history:
        embed.add_field(
            name="Head to head",
            value=f"**{history.record}** across {len(history.meetings)} meeting(s).",
            inline=False,
        )
    else:
        embed.add_field(
            name="Head to head",
            value="You have not faced them before, or it was never recorded.",
            inline=False,
        )

    profile = an.day_profile(state.rows, opponent)
    if profile.weeks_recorded:
        strongest = profile.ranked(best_first=True, minimum=an.MIN_BASELINE_WEEKS)
        if strongest:
            best = strongest[0]
            embed.add_field(
                name="Where they are strong",
                value=(
                    f"Day {best.day}, {best.theme}: they take it "
                    f"{best.wins} of {best.played} recorded."
                ),
                inline=False,
            )

    jump = an.power_jump(state.rows, opponent)
    if jump and jump.is_material:
        embed.add_field(
            name="Since you last met",
            value=f"Their recorded power is {an.pct(jump.change, signed=True)}.",
            inline=False,
        )

    embed.set_footer(
        text=f"Open /vs for the full scout report on {state.display_name(opponent)}."[:2048]
    )
    return embed


async def after_pairing_known(bot, state, week: int) -> bool:
    """Post next week's matchup, once per week, if the alliance opted in."""
    if not state.cfg.get("opponent_reveal_enabled"):
        return False
    embed = reveal_embed(state, week)
    if embed is None:
        return False
    key = f"{_league_key(state.league)}|{week}"
    return await _post(bot, state, KIND_REVEAL, key, embed)


# ── Season recap ──────────────────────────────────────────────────────────────


def recap_embed(state) -> discord.Embed | None:
    """The league, closed out.

    Closure, and the thing that makes a season of manual entry visibly worth
    having done. Everything here is counted from rows the alliance typed.
    """
    if state.own is None or state.league is None:
        return None
    rows = [r for r in state.rows if r.league == state.league and r.alliance == state.own]
    if not rows:
        return None

    wins = sum(1 for r in rows if r.week_outcome == "W")
    losses = sum(1 for r in rows if r.week_outcome == "L")
    league = state.league
    embed = discord.Embed(
        title=f"🏆 {league.season} {league.tier} {league.group}: {wins}-{losses}"[:256],
        description="Your league, from what you recorded.",
        color=discord.Color.blurple(),
    )

    profile = an.day_profile(rows, state.own)
    best = profile.ranked(best_first=True)
    worst = profile.ranked(best_first=False)
    if best and worst and best[0].day != worst[0].day:
        embed.add_field(
            name="Your days",
            value=(
                f"Strongest: day {best[0].day}, {best[0].theme} "
                f"({best[0].wins}-{best[0].losses}).\n"
                f"Weakest: day {worst[0].day}, {worst[0].theme} "
                f"({worst[0].wins}-{worst[0].losses})."
            ),
            inline=False,
        )

    accuracy = an.pick_accuracy(rows, state.own)
    if accuracy.judged:
        embed.add_field(
            name="Your picked calls",
            value=(
                f"{accuracy.correct} of {accuracy.judged} correct. "
                "Weeks you declared a save are left out."
            ),
            inline=False,
        )

    seasons = an.season_trajectory(state.rows, state.own)
    if len(seasons) > 1:
        previous = seasons[-2]
        embed.add_field(
            name="Last season",
            value=f"{previous.league.season} {previous.league.tier}: {previous.record}.",
            inline=False,
        )
    return embed


async def after_league_complete(bot, state) -> bool:
    """Post the recap, once per league, if the alliance opted in."""
    if not state.cfg.get("season_recap_enabled"):
        return False
    if state.league is None or not ad.is_league_complete(state.rows, state.league):
        return False
    embed = recap_embed(state)
    if embed is None:
        return False
    return await _post(bot, state, KIND_RECAP, _league_key(state.league), embed)


# ── The single entry point the write paths call ───────────────────────────────


async def announce_after_write(bot, state, *, week: int | None = None, day: int | None = None):
    """Fire whatever the write just made true, never raising into the caller.

    One call at the end of a save rather than three, so a write path does not
    have to know which announcements exist. Errors are logged and swallowed:
    the officer asked for a score to be recorded, and it was.
    """
    if bot is None:
        return
    try:
        if week is not None and day is not None:
            await after_day_recorded(bot, state, week, day)
        if week is not None:
            await after_pairing_known(bot, state, week)
        await after_league_complete(bot, state)
    except Exception as e:  # noqa: BLE001 - an announcement must not fail a save
        logger.warning("[VS EVENTS] announcement failed for guild=%s: %s", state.guild_id, e)

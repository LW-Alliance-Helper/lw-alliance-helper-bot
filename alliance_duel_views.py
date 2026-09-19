"""Alliance Duel (VS) — the persistent views the scheduled posts carry (#405).

Everything in this module has to keep working with nothing held in memory. The
daily score prompt is posted by a background loop, sits in a leadership channel
overnight, and is clicked by an officer who was asleep when it landed, quite
possibly after a Railway redeploy in between. That rules out the ordinary hub
pattern, where a `View` closes over the loaded `HubState`.

So, per the `storm_signup_view.py` contract:

  * `timeout=None` on the view
  * a stable `custom_id` per button, carrying everything the click needs
  * re-registered at startup via `bot.add_view` from `vs_score_prompt_posts`

Custom-id schema:

    vsprompt:{guild_id}:{week}:{day}

The league is deliberately *not* in there. Its three parts are free text off
the alliance's own sheet ("Alliance Duel League S35", "Diamond Tier 12 - 2"),
which neither fits Discord's 100-character cap reliably nor round-trips through
a colon-separated encoding. It comes from `vs_score_prompt_posts` instead,
which is also the row that survives a restart.
"""

from __future__ import annotations

import logging

import discord

import alliance_duel as ad
import alliance_duel_entry as ad_entry
import alliance_duel_hub as ad_hub
import alliance_duel_setup as ad_setup
import config
import config_health
import messages
import premium
import setup_hub

logger = logging.getLogger(__name__)

#: The one action on the prompt. `{day}` and `{theme}` are filled per post: the
#: prompt names the day it is asking about, because it fires about the day that
#: has just ended and "today" would be a different day by the time an officer
#: reads it. Clamped to Discord's 80-character button cap at build time.
VS_BTN_LOG_DAY = "✏️ Log day {day}: {theme}"

#: Shown when a prompt is clicked after the league it belongs to has been
#: replaced. Writing anyway would file the score against the wrong bracket.
PROMPT_STALE_LEAGUE = (
    "⚠️ This prompt is from your **{league}** league, and your sheet has moved "
    "on to a newer one. I have not written anything, because the score would "
    "have landed on the wrong league's row. Run `{cmd}` and click **{btn}** to "
    "log against the league you are in now."
)


def make_custom_id(guild_id: int, week: int, day: int) -> str:
    """Stable encoding for the prompt's log button."""
    return f"vsprompt:{int(guild_id)}:{int(week)}:{int(day)}"


def parse_custom_id(custom_id: str) -> dict | None:
    """Inverse of `make_custom_id`. None on anything malformed, which the
    handler treats as a no-op rather than raising in a button callback."""
    parts = (custom_id or "").split(":")
    if len(parts) != 4 or parts[0] != "vsprompt":
        return None
    try:
        return {"guild_id": int(parts[1]), "week": int(parts[2]), "day": int(parts[3])}
    except ValueError:
        return None


# ── The prompt itself ─────────────────────────────────────────────────────────


def score_prompt_embed(
    state: ad_hub.HubState,
    week: int,
    day: int,
    opponent: ad.AllianceKey | None,
) -> discord.Embed:
    """Ask for one day's numbers, and say where the week stands without them.

    Leads with the matchup rather than the request, because an officer reading
    this at reset already knows they are being asked for scores. What they
    cannot see from the channel is which day closed and what it was worth.
    """
    duel_day = ad.DUEL_DAY_BY_NUMBER[day]
    who = state.display_name(opponent) if opponent else "your opponent"
    embed = discord.Embed(
        title=f"🔔 Day {day}: {duel_day.theme}",
        description=(
            f"You played **{who}** for **{duel_day.points}** "
            f"{'point' if duel_day.points == 1 else 'points'}. "
            f"What did the two of you score?"
        ),
        color=discord.Color.blurple(),
    )

    row = state.row_for(state.own, week)
    if row is not None and row.day_outcomes:
        clinch = ad.clinch_state(row.day_outcomes)
        if clinch.clinched:
            standing = f"**{clinch.own_points}-{clinch.opponent_points}**. The week is yours."
        elif clinch.lost:
            standing = f"**{clinch.own_points}-{clinch.opponent_points}**. The week has gone."
        else:
            standing = (
                f"**{clinch.own_points}-{clinch.opponent_points}**, {clinch.points_needed} to go."
            )
        embed.add_field(name="Before this day", value=standing, inline=False)

    embed.set_footer(text=f"Week {week}. Duel days follow server time.")
    return embed


class ScorePromptView(discord.ui.View):
    """The prompt's single button. Persistent, so it holds only integers.

    Everything else (the sheet snapshot, who the opponent is, whether the guild
    is still Premium) is resolved when the button is actually clicked, which is
    the only moment any of it is known to be current.
    """

    def __init__(self, guild_id: int, week: int, day: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.week = week
        self.day = day

        theme = ad.DUEL_DAY_BY_NUMBER[day].theme
        button = discord.ui.Button(
            label=VS_BTN_LOG_DAY.format(day=day, theme=theme)[:80],
            style=discord.ButtonStyle.primary,
            custom_id=make_custom_id(guild_id, week, day),
        )
        button.callback = self._log
        self.add_item(button)

    async def _log(self, interaction: discord.Interaction) -> None:
        from setup_cog import _has_leadership_or_admin

        # Posted in a channel the whole of leadership can see, so unlike a hub
        # view there is no single owner. Anyone who could run `/vs` can log.
        if not _has_leadership_or_admin(interaction):
            cfg = config.get_config(interaction.guild_id)
            role = (cfg.leadership_role_name if cfg else None) or "Leadership"
            await interaction.response.send_message(
                f"⛔ You need the **{role}** role (or admin) to log a duel day.",
                ephemeral=True,
            )
            return

        # Re-gated at click time, not just at post time. A guild whose Premium
        # lapsed overnight still has yesterday's prompt sitting in the channel.
        if not await premium.feature_gate(
            "alliance_duel_vs", interaction.guild_id, interaction=interaction
        ):
            await interaction.response.send_message(
                messages.PREMIUM_LOCKED_INLINE.format(feature="Alliance Duel (VS) tracker"),
                ephemeral=True,
            )
            return

        vs_cfg = config.get_vs_config(interaction.guild_id)
        if not vs_cfg.get("enabled"):
            await interaction.response.send_message(
                messages.FEATURE_NOT_CONFIGURED.format(
                    feature="Alliance Duel (VS)", wizard_btn=setup_hub.HUB_BTN_VS
                ),
                ephemeral=True,
            )
            return

        # A modal cannot follow a deferral, so the sheet read has to happen
        # first and the modal has to be the response. Reading the tab is a
        # second or two, inside the 3-second window; `ScoreModal` defers before
        # its own write, which is the slow half.
        try:
            rows = await ad_hub.read_tab_once(interaction.guild_id, vs_cfg)
        except Exception as e:  # noqa: BLE001 - a bot bug, not the alliance's
            logger.exception("[VS PROMPT] sheet read failed for guild %s", interaction.guild_id)
            await interaction.response.send_message(
                f"⚠️ Couldn't read your sheet: {config.describe_sheet_error(e)}", ephemeral=True
            )
            return

        if rows is None:
            problems = config_health.problems_for_subjects(
                interaction.guild_id, [ad_setup.VS_SHEET_SUBJECT]
            )
            detail = config_health.describe(problems[0]) if problems else "I couldn't read the tab."
            await interaction.response.send_message(
                f"⚠️ {detail}\n\nFix that and run `{ad_hub.VS_HUB_CMD}` to log the day.",
                ephemeral=True,
            )
            return

        state = ad_hub.HubState(interaction.guild_id, vs_cfg, rows)
        if state.own is None:
            await interaction.response.send_message(
                "⚠️ I no longer know which alliance is yours. Set it again in "
                f"{ad_setup.VS_SETUP_NAV}.",
                ephemeral=True,
            )
            return

        stale = self._stale_league(interaction, state)
        if stale is not None:
            await interaction.response.send_message(stale, ephemeral=True)
            return

        await interaction.response.send_modal(
            ad_entry.ScoreModal(state, self.week, self.day, state.own_match(self.week))
        )

    def _stale_league(self, interaction: discord.Interaction, state: ad_hub.HubState) -> str | None:
        """The refusal sentence when this prompt outlived its league, else None.

        The week number alone is ambiguous across leagues: every league has a
        week 2, and writing to "week 2" after the sheet moved on would file the
        score under the wrong bracket. `vs_score_prompt_posts` remembers which
        league the prompt was asking about; a prompt with no row left (aged out
        of the table) is let through, since by then the far likelier reading is
        a bot that lost its table, not a league that turned over.
        """
        message = getattr(interaction, "message", None)
        post = config.get_vs_score_prompt_post(message.id) if message else None
        if not post:
            return None
        posted_league = ad.LeagueKey.of(
            post.get("league_season"), post.get("league_tier"), post.get("league_group")
        )
        if posted_league is None or state.league is None or posted_league == state.league:
            return None
        return PROMPT_STALE_LEAGUE.format(
            league=posted_league.season,
            cmd=ad_hub.VS_HUB_CMD,
            btn=ad_entry.VS_BTN_LOG_SCORE,
        )


def register_persistent_vs_views(bot) -> int:
    """Re-attach a `ScorePromptView` for every recent prompt, at startup.

    Without this, discord.py has no view matching the button's custom_id after
    a restart and the click dies as "Interaction failed", with the officer
    given no reason and no idea the day went unrecorded.
    """
    registered = 0
    for post in config.get_recent_vs_score_prompt_posts():
        try:
            view = ScorePromptView(post["guild_id"], post["week"], post["duel_day"])
            bot.add_view(view, message_id=int(post["message_id"]))
            registered += 1
        except Exception as e:  # noqa: BLE001 - one bad row must not stop the rest
            logger.warning(
                "[VS PROMPT] failed to register view for guild=%s message=%s: %s",
                post.get("guild_id"),
                post.get("message_id"),
                e,
            )
    if registered:
        logger.info("[VS PROMPT] Re-registered %d score prompt view(s) on startup", registered)
    return registered


# ── The member day-theme reminder (#406) ──────────────────────────────────────


def day_theme_embed(day: int, note: str = "") -> discord.Embed:
    """What today's duel day rewards, for members.

    Free tier and member-facing, unlike everything else in this feature, and
    written for someone with no idea the bot exists: it names the day, says
    what to spend, and stops. It reads nothing from the sheet, so an alliance
    that has never touched the tracker can still switch it on.

    **No award values, ever.** The in-game board renders each player their own
    Tech-boosted figures, so there is no shared number that would be true for
    two members of the same alliance. Order survives that and is what the copy
    leans on. `alliance_duel.DAY_ACTIONS` carries the full reasoning.
    """
    duel_day = ad.DUEL_DAY_BY_NUMBER[day]
    points = duel_day.points
    embed = discord.Embed(
        title=f"🏆 Today is {duel_day.theme}",
        description=(
            f"Day {day} of the Alliance Duel week, worth **{points}** of the "
            f"week's {ad.WEEK_POINTS_TOTAL} league points."
        ),
        color=discord.Color.blurple(),
    )

    actions = ad.DAY_ACTIONS[day]
    embed.add_field(
        name="What scores today",
        value="\n".join(f"• {action}" for action in actions) + f"\n\n{ad.SCORES_EVERY_DAY}",
        inline=False,
    )
    if day == 6:
        embed.add_field(name="Worth knowing", value=ad.ENEMY_BUSTER_NOTE, inline=False)

    if note.strip():
        # Alliance-authored, in whatever language they write in, so it is
        # carried verbatim and only clamped. Named as leadership's words rather
        # than presented as the bot's.
        embed.add_field(name="From your leadership", value=note.strip()[:1024], inline=False)

    embed.set_footer(text="Biggest first. Spend what you have been saving.")
    return embed

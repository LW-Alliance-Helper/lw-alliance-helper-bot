"""Alliance Duel (VS) — views and modals (#402).

Right now this is the scout profile: pick an alliance, see what has been
recorded about them, what the model will and will not say about the matchup,
and the real history of every time you have met. The read buttons, the note
modal, the add/edit modals and the daily score entry land here in #404, which
is why the picker lives in its own module rather than inside the hub.

The scout profile is the payoff for the manual entry burden, because the
head-to-head half is **observed history rather than an estimate**. It is
rendered above the projection for that reason: a real 2-1 record against this
alliance is better evidence than anything three stats can infer, and putting
the inference first would invert that.

Every modal added here must `defer` before any sheet round-trip, per the
CLAUDE.md 1.1.7 rule.
"""

from __future__ import annotations

import logging

import discord

import alliance_duel as ad
import alliance_duel_analytics as an
import alliance_duel_setup as ad_setup
import messages

logger = logging.getLogger(__name__)

#: Discord caps a select at 25 options, and a bracket is 16, so a full league
#: fits in one. Guarded anyway: a sheet with several leagues in it can offer
#: more alliances than one select holds.
MAX_SELECT_OPTIONS = 25

#: A single pick with a little thought behind it, per the DESIGN.md timeout
#: tiers. Not the hub's 900: this view holds no work worth preserving, and a
#: stale picker is more confusing than an expired one.
PICKER_TIMEOUT = 180


# ── Scout profile ─────────────────────────────────────────────────────────────


def scout_embed(state, target: ad.AllianceKey) -> discord.Embed:
    """One alliance's profile: what is recorded, what has happened, what is guessed.

    In that order, deliberately. Recorded facts first, observed history second,
    the computed read last and clearly labelled, so the softest thing on the
    screen is also the last thing read.
    """
    profile = state.profiles.get(target)
    embed = discord.Embed(
        title=f"🔍 {state.display_name(target)}"[:256],
        color=discord.Color.blurple(),
    )

    embed.add_field(name="Recorded", value=_recorded_block(state, target, profile), inline=False)

    if state.own is not None and target != state.own:
        history = ad.head_to_head(state.rows, state.own, target)
        embed.add_field(name="Head to head", value=_history_block(state, history), inline=False)
        embed.add_field(name="Projection", value=_projection_block(state, target), inline=False)
    elif target == state.own:
        embed.set_footer(text="This is your alliance.")

    notes = (profile.notes if profile else "") or ""
    if notes.strip():
        embed.add_field(name="Notes", value=notes.strip()[:1024], inline=False)
    return embed


def _recorded_block(state, target: ad.AllianceKey, profile) -> str:
    """The raw latest-non-blank values, with how old they are.

    Age is shown rather than implied. Nobody re-scouts fifteen alliances
    weekly, so most of these cells were filled once, and a reader has to know
    whether they are looking at last week or last season.
    """
    if profile is None:
        return "Nothing recorded yet."

    power = f"{profile.power / 1_000_000:,.0f}M" if profile.power else ad_setup.NOT_ENTERED
    members = str(profile.members) if profile.members is not None else ad_setup.NOT_ENTERED
    gift = str(profile.gift_level) if profile.gift_level is not None else ad_setup.NOT_ENTERED
    lines = [f"Power {power} · {members} members · gift level {gift}"]

    age = ad.input_age_days(profile)
    if age is not None:
        lines.append(f"Last updated {age} day{'' if age == 1 else 's'} ago.")
    if not profile.is_tier_1:
        lines.append(
            "*Power, members and gift level are all needed before a matchup can be projected.*"
        )

    known = (profile.known_1_5 or "").strip()
    known_6 = (profile.known_6 or "").strip()
    if known:
        lines.append(f"Known days 1-5: **{known}**")
    if known_6:
        lines.append(f"Known day 6: **{known_6}**")

    trajectory = _trajectory_line(profile)
    if trajectory:
        lines.append(trajectory)
    return "\n".join(lines)[:1024]


def _trajectory_line(profile) -> str:
    """Power growth between the rows where power was filled in.

    Growth is the output of activity, so this measures mobilization without
    asking anyone to judge another alliance's activity from outside, which the
    design rejects doing. Reported as an observation, never as a verdict.
    """
    history = [(d, p) for d, p in profile.power_history if p]
    if len(history) < 2:
        return ""
    (first_date, first), (last_date, last) = history[0], history[-1]
    if not first:
        return ""
    days = (last_date - first_date).days
    if days <= 0:
        return ""
    change = round((last / first - 1) * 100)
    direction = "up" if change > 0 else ("down" if change < 0 else "flat")
    if direction == "flat":
        return f"Power flat across {days} days of recorded rows."
    return f"Power {direction} {abs(change)}% across {days} days of recorded rows."


def _history_block(state, history: ad.HeadToHead) -> str:
    """Every prior meeting, newest first, with the tier it happened in.

    Tier is shown per meeting rather than averaged away: a result earned a tier
    down is weaker evidence about today than one from the current bracket.
    Tier *movement* since the last meeting is called out separately because it
    is game-adjudicated, and therefore harder evidence than any proxy here.
    """
    if not history:
        return "You have never faced this alliance, or the meeting was never recorded."

    lines = [f"**{history.record}** across {len(history.meetings)} meeting(s)."]
    if history.unrecorded:
        lines[0] += f" {history.unrecorded} with no outcome recorded."

    current_tier = state.league.tier if state.league else ""
    movement = history.tier_movement(current_tier)
    if movement:
        previous, delta = movement
        lines.append(
            f"They were in **{previous}** when you last met and are in **{current_tier}** now, "
            f"so the game {'promoted' if delta > 0 else 'relegated'} them since."
        )

    for meeting in history.meetings[:5]:
        mine, theirs = meeting.score
        score = (
            f"{mine}-{theirs}" if mine is not None and theirs is not None else "score not recorded"
        )
        result = {"W": "won", "L": "lost"}.get(meeting.outcome or "", "unrecorded")
        lines.append(
            f"· {meeting.league.season} {meeting.tier}, week {meeting.week}: {result} {score}"
        )
    if len(history.meetings) > 5:
        lines.append(f"*…and {len(history.meetings) - 5} earlier meeting(s).*")
    return "\n".join(lines)[:1024]


def _projection_block(state, target: ad.AllianceKey) -> str:
    """The computed read, rendered from the model's own lines.

    The copy comes straight off `WeekProjection`, including the capacity-ceiling
    caveat, so the honesty rules cannot drift apart from the surface that prints
    them.
    """
    own_profile = state.profiles.get(state.own)
    target_profile = state.profiles.get(target)
    if own_profile is None or target_profile is None:
        return "Not enough recorded to project this matchup."

    row = state.row_for(state.own, state.week)
    picked = row.picked if row is not None and row.opponent == target else None
    projection = ad.project_week(own_profile, target_profile, picked=picked)
    return "\n".join(projection.lines)[:1024]


# ── Picker ────────────────────────────────────────────────────────────────────


class ScoutPickerView(discord.ui.View):
    """Choose an alliance to scout. Renders from the loaded snapshot only."""

    def __init__(self, state, owner_id: int):
        super().__init__(timeout=PICKER_TIMEOUT)
        self.state = state
        self.owner_id = owner_id

        options = _scout_options(state)
        # Acts on change rather than pairing with a confirm button. The
        # DESIGN.md preference for select-plus-confirm exists because a
        # mis-tap on a phone is otherwise unrecoverable, and here it is not:
        # this opens a read-only profile and the picker stays live to pick
        # again. A confirm step would cost a tap on every single look-up.
        select = discord.ui.Select(
            placeholder="Pick an alliance to scout",
            options=options,
            disabled=not options,
        )
        select.callback = self._picked
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(messages.DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _picked(self, interaction: discord.Interaction):
        import alliance_duel_entry as ad_entry

        raw = interaction.data["values"][0]
        tag, _, warzone = raw.partition("|")
        target = ad.AllianceKey(tag, warzone)
        # The profile carries its own write actions, so a read that prompts a
        # correction does not cost a trip back to the hub.
        view = (
            None
            if target == self.state.own
            else ad_entry.ScoutActionsView(self.state, target, self.owner_id)
        )
        await interaction.response.send_message(
            embed=scout_embed(self.state, target), view=view, ephemeral=True
        )


def _scout_options(state) -> list[discord.SelectOption]:
    """Alliances worth offering, most useful first.

    Ordered by how much the reader is likely to want them: this week's opponent
    first, then the rest of the bracket by ranking. Someone opening Scout mid-week
    is usually asking about the alliance they are currently playing.
    """
    rows = state.league_rows()
    rankings: dict[ad.AllianceKey, int] = {}
    for row in rows:
        if row.ranking is not None:
            rankings.setdefault(row.alliance, row.ranking)

    opponent = state.own_match(state.week)
    alliances = sorted(
        {r.alliance for r in rows},
        key=lambda a: (
            0 if a == opponent else (1 if a != state.own else 2),
            rankings.get(a, ad.BRACKET_SIZE + 1),
            a,
        ),
    )

    options = []
    for alliance in alliances[:MAX_SELECT_OPTIONS]:
        label = state.display_name(alliance)[:100]
        if alliance == opponent:
            description = "This week's opponent"
        elif alliance == state.own:
            description = "Your alliance"
        else:
            ranking = rankings.get(alliance)
            description = f"Ranking {ranking}" if ranking else "No ranking recorded"
        options.append(
            discord.SelectOption(
                label=label,
                value=f"{alliance.tag}|{alliance.warzone}",
                description=description[:100],
            )
        )
    return options


async def open_scout_picker(interaction: discord.Interaction, state) -> None:
    """Open the scout picker for the loaded league."""
    view = ScoutPickerView(state, interaction.user.id)
    if not _scout_options(state):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔍 Scout",
                description=(
                    "No alliances recorded for this league yet. Fill in the bracket off the "
                    f"in-game League screen, or open the column guide from {ad_setup.VS_SETUP_NAV}."
                ),
                color=discord.Color.blurple(),
            ),
            ephemeral=True,
        )
        return
    await interaction.response.send_message("Who do you want to scout?", view=view, ephemeral=True)


__all__ = ["scout_embed", "ScoutPickerView", "open_scout_picker"]


# ── Trends (#408) ─────────────────────────────────────────────────────────────

#: 🔍 is the catalog's "view trends" glyph, and deliberately not 📊, which
#: already names Growth Breakdown, nor 🔍, which this feature already spends on
#: Scout. The two magnifiers reading as a pair here is fine: scouting one
#: alliance and reading your own patterns are the same kind of act on different
#: subjects.
VS_BTN_TRENDS = "🔍 Trends"

#: Days below this are reported as a shape rather than a rate. One recorded
#: week is not a pattern, and rendering "you lose Age of Science 100% of the
#: time" off a single week is the failure mode this whole module is written
#: against.
MIN_DAYS_FOR_PATTERN = 3


def trends_embed(state) -> discord.Embed:
    """The guild's own patterns: which days it wins, how, and how reliably.

    Everything here needs no bracket, which is the point. The most actionable
    output in the feature ("you lose Age of Science in 8 of 12 weeks") is a
    resource instruction available to an alliance tracking only itself.
    """
    embed = discord.Embed(title=f"{VS_BTN_TRENDS} for your alliance", color=discord.Color.blurple())

    if state.own is None:
        embed.description = (
            "You have not told me which alliance is yours yet, so there is nothing "
            f"to read patterns from. Set it in {ad_setup.VS_SETUP_NAV}."
        )
        return embed

    rows = [r for r in state.rows if r.alliance == state.own]
    profile = an.day_profile(rows, state.own)

    # The day read is the headline but not the gate: an alliance that recorded
    # week outcomes and picked calls without logging individual days still has
    # a pick accuracy worth reading, and telling them "nothing recorded" would
    # hide data they typed in themselves.
    if profile.weeks_recorded:
        embed.description = _sample_line(profile)
        embed.add_field(name="Your days", value=_day_block(profile), inline=False)

    buster = profile.day_six()
    if buster and buster.played:
        embed.add_field(name="Enemy Buster", value=_buster_line(buster), inline=False)

    shape = an.margin_shape(state.rows, state.own)
    if shape.days_compared:
        embed.add_field(name="How close they are", value=_margin_line(shape), inline=False)

    diverged = an.divergences(state.rows, state.own)
    if diverged:
        embed.add_field(name="Worth remembering", value=_divergence_line(diverged), inline=False)

    seasons = an.season_trajectory(state.rows, state.own)
    if len(seasons) > 1:
        embed.add_field(name="Season by season", value=_season_line(seasons), inline=False)

    accuracy = an.pick_accuracy(state.rows, state.own)
    if accuracy.judged:
        embed.add_field(name="Your picked calls", value=_accuracy_block(accuracy), inline=False)

    if not embed.fields:
        embed.description = (
            "Nothing recorded yet. Log a few weeks of day outcomes and this "
            "fills in on its own: which days you take, which you lose, and "
            "whether your wins are comfortable or narrow."
        )
        return embed

    embed.set_footer(text="Everything here is counted from what you have logged.")
    return embed


def _sample_line(profile) -> str:
    weeks = an.sample_words(profile.weeks_recorded)
    return (
        f"Counted across {weeks} of recorded day outcomes. "
        "A day nobody logged is left out rather than counted as a loss."
    )


def _day_block(profile) -> str:
    """Every day, in day order, with the sample beside the rate.

    Day order rather than ranked order on purpose: the reader is deciding what
    to bank for a specific day of the week, so the list has to be scannable by
    day rather than sorted into a leaderboard.
    """
    lines = []
    for record in profile.days:
        if not record.played:
            lines.append(f"**Day {record.day}** {record.theme}: nothing recorded")
            continue
        line = (
            f"**Day {record.day}** {record.theme}: "
            f"{record.wins}-{record.losses} ({an.pct(record.win_rate)})"
        )
        if record.played < MIN_DAYS_FOR_PATTERN:
            line += " *(too few to call a pattern)*"
        lines.append(line)
    return "\n".join(lines)[:1024]


def _buster_line(record) -> str:
    """The one honest combat read that exists.

    No formula can predict Enemy Buster, and this is not one: it is what has
    already happened, which is a better basis for the 4-point call than any
    model would have been.
    """
    line = (
        f"You take it {record.wins} weeks in {record.played} ({an.pct(record.win_rate)}). "
        "Nothing predicts this day, so your own record against it is the read."
    )
    if record.played < MIN_DAYS_FOR_PATTERN:
        line += " Too few weeks to lean on yet."
    return line


def _margin_line(shape) -> str:
    parts = [
        f"Across {an.sample_words(shape.days_compared, 'day')} where both scores are logged, "
        f"the middle margin is {an.pct(shape.median, signed=True)}."
    ]
    if shape.close_days:
        parts.append(f"{shape.close_days} were inside 10%.")
    if shape.blowouts:
        parts.append(f"{shape.blowouts} were 50% or wider.")
    return " ".join(parts)


def _divergence_line(diverged) -> str:
    """The weeks that get misremembered.

    Losing a week while outscoring the opponent is a distribution problem, not
    bad luck, and naming it is the difference between fixing it and shrugging.
    """
    lines = []
    for d in diverged[:3]:
        if d.outscored_and_lost:
            lines.append(f"**Week {d.week}**: you outscored them overall and still lost the week.")
        else:
            lines.append(f"**Week {d.week}**: they outscored you overall and you still took it.")
    lines.append("Days are won one at a time, so the totals and the result can disagree.")
    return "\n".join(lines)[:1024]


def _season_line(seasons) -> str:
    return "\n".join(
        f"**{s.league.season}** {s.league.tier} {s.league.group}: {s.record}" for s in seasons
    )[:1024]


def _accuracy_block(accuracy) -> str:
    """The rate, and immediately what it does not control for.

    A number this easy to over-read gets its caveat in the same field rather
    than in a footer: our own declared saves are out of it, but an opponent
    quietly saving is invisible from this side and always will be.
    """
    lines = [f"{accuracy.correct} of {accuracy.judged} called correctly ({an.pct(accuracy.rate)})."]
    if accuracy.unpicked:
        lines.append(f"{accuracy.unpicked} weeks in the sample carried no call.")
    if accuracy.excluded_saves:
        lines.append(
            f"{an.sample_words(accuracy.excluded_saves)} left out because you declared a save."
        )
    if accuracy.rests_on_assumption:
        lines.append(
            f"{an.sample_words(accuracy.rests_on_assumption)} were never declared either way, "
            "so they are counted as normal effort."
        )
    lines.append("This cannot see an opponent saving quietly, so read it as a floor.")
    return "\n".join(lines)[:1024]


def opponent_trends_embed(state, target: ad.AllianceKey) -> discord.Embed:
    """The same day read for an alliance you have faced.

    Narrower than your own on purpose. Their day outcomes exist only for weeks
    you played them, so this says less and has to say how much less.
    """
    embed = discord.Embed(
        title=f"{VS_BTN_TRENDS} for {state.display_name(target)}"[:256],
        color=discord.Color.blurple(),
    )
    profile = an.day_profile(state.rows, target)
    if profile.weeks_recorded == 0:
        embed.description = (
            "Nothing recorded for them yet. Their day outcomes only exist for "
            "weeks you played them and logged both sides."
        )
        return embed

    embed.description = (
        f"From {an.sample_words(profile.weeks_recorded)} you have logged against them."
    )
    embed.add_field(name="Their days", value=_day_block(profile), inline=False)

    shape = an.margin_shape(state.rows, state.own, target) if state.own else None
    if shape and shape.days_compared:
        embed.add_field(name="How close they are", value=_margin_line(shape), inline=False)

    jump = an.power_jump(state.rows, target)
    if jump and jump.is_material:
        embed.add_field(
            name="Since you last met",
            value=(
                f"Their recorded power is {an.pct(jump.change, signed=True)}, "
                f"{jump.previous:,} to {jump.latest:,}."
            ),
            inline=False,
        )
    return embed

"""Alliance Duel (VS) — the `/vs` hub (front door) (#402).

One flat command opening a stateful hub: a status embed plus a button grid,
matching `/buddy`, `/train`, `/events`, `/transfers` and `/map_manager`. Not an
`app_commands.Group` with named subcommands. This repo's convention for a
feature this stateful is the hub pattern, and the design doc settles it.

This module owns the hub embed, the button grid, and the two whole-bracket
reads (Bracket and This week). The per-alliance scout profile lives in
`alliance_duel_ui.py`, which is where its read buttons and note modal land in
#404.

**The sheet is read exactly once per `/vs` invocation.** 1.5.1 had to fix storm
screens blowing the Sheets read limit on quick click-through (#269), and this
hub has more buttons than those did. `handle_vs_hub` loads the tab, wraps it in
a `HubState`, and every view in the chain renders from that snapshot. No button
callback reads the sheet. Re-running `/vs` is the refresh, which is also the
only thing that re-reads.
"""

from __future__ import annotations

import asyncio
import logging

import discord

import alliance_duel as ad
import alliance_duel_entry as ad_entry
import alliance_duel_setup as ad_setup
import alliance_duel_ui as ad_ui
import config
import config_health
import messages
import premium
import wizard_registry

logger = logging.getLogger(__name__)

VS_HUB_CMD = "/vs"

#: Button labels. Named UI surfaces referenced from other modules' copy (the
#: sheet-problem notice and the /help category both point at them), so they
#: live as constants rather than duplicated literals.
#:
#: Emoji picked against the DESIGN.md catalog: 📇 is the bracket as a directory
#: of alliances, 🆚 is the game's own matchup mark, 🔍 is the catalog's look-up
#: verb, ⚙️ is its link-into-setup. Deliberately **not** 📊, which already names
#: Growth Breakdown, nor 📅, which already names the Events date views.
VS_BTN_BRACKET = "📇 Bracket"
VS_BTN_WEEK = "🆚 This week"
VS_BTN_SCOUT = "🔍 Scout"
VS_BTN_PATH = "🛣️ My path"
VS_BTN_SETUP = "⚙️ Sheet setup and check"

#: The path screen's own title. "My path" on the button that opens it, "Your
#: path" once open: the button is the reader picking a thing off a menu, the
#: screen is the bot handing it to them.
VS_PATH_TITLE = "🛣️ Your path"
VS_PATH_IF_WIN = "If you win"
VS_PATH_IF_LOSE = "If you lose"

#: What a line's claim rests on. Rendered as inline code, which is the closest
#: thing Discord has to a chip -- an embed body is one colour throughout, so a
#: label cannot be tinted and has to be shaped instead.
#:
#: **There is deliberately no label for a call somebody here made.** What gets
#: labelled is what the reader did not do themselves, so a bare line reads as
#: "one of us decided this" without a word spent saying so.
VS_LABEL_RECORDED = "Recorded result"
VS_LABEL_BOT = "Bot prediction"
VS_LABEL_NONE = "No prediction"

#: The two halves a blocked match falls into, headed by what clears it. Split
#: because they ask the reader for opposite things: one wants numbers off the
#: League screen, the other wants somebody to make a call.
VS_PATH_BLOCKED_SCOUTABLE = "Scout these first"
VS_PATH_BLOCKED_UNDECIDED = "Too close for me to predict"

#: The path footer: two counts, two actions, nothing else. A footer is grey
#: and small and gets skimmed, so it says what is outstanding and what
#: clears it, and leaves the payoff to the fork above -- the reader is
#: already looking at the weeks that would fill in.
#:
#: Either half can appear alone, so the second names "matches" when it
#: leads and elides the noun when it follows.
FOOTER_UNDECIDED = "{subject} your prediction."
FOOTER_SCOUTABLE = "{subject} power, members and gift level."

#: The not-entered glyph, shared with every other VS surface.
NOT_ENTERED = ad_setup.NOT_ENTERED


# ── Loaded state ──────────────────────────────────────────────────────────────


class HubState:
    """One `/vs` invocation's snapshot of the sheet, passed down the chain.

    Everything derived is computed once here rather than per button click:
    profiles, the live week, and the current league. A view that needs
    something not on here should take it as an argument, not go back to the
    sheet for it.
    """

    def __init__(self, guild_id: int, cfg: dict, rows: list[ad.AllianceWeek]):
        self.guild_id = guild_id
        self.cfg = cfg
        self.rows = rows
        self.tracking_mode = cfg.get("tracking_mode") or ad.MODE_FULL_BRACKET
        self.own = ad.AllianceKey.of(cfg.get("own_tag"), cfg.get("own_warzone"))
        self.profiles = ad.build_profiles(rows)
        self.live = ad.resolve_live_week(rows)
        self.league = self.live.league if self.live else ad.latest_league(rows)

    @property
    def full_bracket(self) -> bool:
        return self.tracking_mode == ad.MODE_FULL_BRACKET

    @property
    def week(self) -> int | None:
        return self.live.week if self.live else None

    def league_rows(self, week: int | None = None) -> list[ad.AllianceWeek]:
        """Rows for the current league, optionally narrowed to one week."""
        if self.league is None:
            return []
        return [
            r for r in self.rows if r.league == self.league and (week is None or r.week == week)
        ]

    def row_for(self, alliance: ad.AllianceKey, week: int | None) -> ad.AllianceWeek | None:
        for row in self.league_rows(week):
            if row.alliance == alliance:
                return row
        return None

    def display_name(self, alliance: ad.AllianceKey) -> str:
        """How an alliance reads on screen: its tag, as the game prints it.

        **No brackets.** The game shows `nWA`, so we do. They were decoration
        around an identifier that is already unambiguous, and on a path screen
        listing eight of them in a column they were eight rows of noise.

        Falls back to the normalised key so a row is never nameless. Clamped
        because the tag is an alliance-supplied cell, and this lands in embed
        titles (256) and select option labels (100).
        """
        for row in self.rows:
            if row.alliance == alliance and row.tag_display:
                return row.tag_display[:16]
        return alliance.tag.upper()[:16]

    def own_match(self, week: int | None) -> ad.AllianceKey | None:
        """Who the guild faces in `week`, from the recorded Opponent column."""
        if self.own is None:
            return None
        row = self.row_for(self.own, week)
        return row.opponent if row else None


# ── Embeds ────────────────────────────────────────────────────────────────────


def hub_embed(state: HubState) -> discord.Embed:
    """The front page: which league and week is live, and the guild's matchup.

    Leads with the matchup because that is what someone opening `/vs` mid-week
    is actually there for. League identity sits underneath as context.
    """
    embed = discord.Embed(title="🏆 Alliance Duel (VS)", color=discord.Color.blurple())

    if state.league is None:
        embed.description = (
            "Your tab is set up but has no league in it yet.\n\n"
            f"Fill in the bracket off the in-game League screen, or open "
            f"**{VS_BTN_SETUP}** for the column guide."
        )
        return embed

    league = state.league
    header = f"**{league.season} · {league.tier} {league.group}**".replace(" ·  ", " · ")
    if state.live:
        day = state.live.day
        when = f"Week {state.live.week} of {ad.LEAGUE_WEEKS}, " + (
            f"day {day} · {state.live.theme} ({ad.DUEL_DAY_BY_NUMBER[day].points} pts)"
            if day
            else "Sunday, no scoring today"
        )
    else:
        when = "No week running right now."
    embed.description = f"{header}\n{when}"

    embed.add_field(name="This week", value=_own_matchup_line(state), inline=False)

    if not state.full_bracket:
        embed.add_field(
            name="Tracking",
            value=(
                f"You are tracking {ad_setup.mode_label(state.tracking_mode)}. "
                f"The bracket views need all 16 alliances; you can widen at any "
                f"time from {ad_setup.VS_SETUP_NAV}."
            ),
            inline=False,
        )

    _add_sheet_problem_field(embed, state.guild_id)
    embed.set_footer(text="Your sheet is the source. Anything you type there wins.")
    return embed


def _own_matchup_line(state: HubState) -> str:
    """The guild's own matchup for the live week, with its running split."""
    if state.own is None:
        return (
            f"You have not told me which alliance is yours yet. Set it in {ad_setup.VS_SETUP_NAV}."
        )
    row = state.row_for(state.own, state.week)
    if row is None:
        return f"No row for {state.display_name(state.own)} in this week yet."

    opponent = row.opponent
    if opponent is None:
        return f"{state.display_name(state.own)}, with no opponent recorded for this week yet."

    clinch = ad.clinch_state(row.day_outcomes)
    line = f"{state.display_name(state.own)} vs {state.display_name(opponent)}"
    if clinch.own_points or clinch.opponent_points:
        line += f"\n**{clinch.own_points}-{clinch.opponent_points}** on league points"
        if clinch.clinched:
            line += ", already clinched."
        elif clinch.lost:
            line += ", already lost."
        elif clinch.clinching_days:
            days = ", ".join(
                f"day {d} ({ad.DUEL_DAY_BY_NUMBER[d].points} pts)" for d in clinch.clinching_days
            )
            line += f". Winning {days} clinches it."
        else:
            line += f". {clinch.points_needed} more to take the week."
    return line


def _add_sheet_problem_field(embed: discord.Embed, guild_id: int) -> None:
    """Surface a broken tab on the hub itself.

    The leadership-channel notice can be scrolled past, and the hub is where
    someone goes to ask "is this working?", so a recorded problem has to be
    visible right here (the #413 reasoning, generalised in #414/#379).
    """
    problems = config_health.problems_for_subjects(guild_id, [ad_setup.VS_SHEET_SUBJECT])
    if not problems:
        return
    embed.color = discord.Color.red()
    for problem in problems:
        embed.add_field(
            name=f"⚠️ I can't read {problem.label}"[:256],
            value=(
                f"{config_health.describe(problem)}\n\nEverything below is from the "
                f"last good read, so it may be out of date."
            ).strip()[:1024],
            inline=False,
        )


def bracket_embed(state: HubState, week: int | None = None) -> discord.Embed:
    """All sixteen alliances for the current league, with their raw data.

    Raw, not derived: this is the "what have we actually typed in" view, and a
    blank cell renders as unknown rather than as a zero or an estimate. Sorted
    by ranking, because that is the order the in-game League screen shows and the
    order someone copying off it will be reading in.
    """
    league = state.league
    embed = discord.Embed(
        title=VS_BTN_BRACKET,
        color=discord.Color.blurple(),
        description=(
            f"**{league.season} · {league.tier} {league.group}**"
            + (f" · week {week}" if week else "")
        ),
    )

    rows = sorted(
        state.league_rows(week),
        key=lambda r: (r.ranking if r.ranking is not None else ad.BRACKET_SIZE + 1, r.alliance),
    )
    if not rows:
        embed.description += "\n\n*No rows recorded for this week yet.*"
        return embed

    lines = []
    for row in rows:
        ranking = f"`{row.ranking:>2}`" if row.ranking is not None else "` ?`"
        power = f"{row.power / 1_000_000:,.0f}M" if row.power else NOT_ENTERED
        members = str(row.members) if row.members is not None else NOT_ENTERED
        gift = str(row.gift_level) if row.gift_level is not None else NOT_ENTERED
        mine = " ⬅️" if row.alliance == state.own else ""
        lines.append(
            f"{ranking} **{state.display_name(row.alliance)}** · {power} · "
            f"{members} members · gift {gift}{mine}"
        )
    embed.description += "\n\n" + "\n".join(lines)[:3800]
    embed.set_footer(
        text=f"{len(rows)} of {ad.BRACKET_SIZE} alliances · {NOT_ENTERED} means not entered"
    )
    return embed


def week_embed(state: HubState, week: int) -> discord.Embed:
    """This week's matchups, own first, each labelled with its status.

    Status is the design's user-facing ladder: a confirmed result, then Known,
    then Picked, then Estimated, then Unassessed. Unassessed renders plainly
    rather than being folded into something that sounds like a call.
    """
    embed = discord.Embed(
        title=f"{VS_BTN_WEEK.split()[0]} Week {week}",
        color=discord.Color.blurple(),
        description=f"**{state.league.season} · {state.league.tier} {state.league.group}**",
    )

    rows = state.league_rows(week)

    # `compute_week_pairing` weighs every prior week's result, so it takes the
    # whole league and not one week of it. Handed a single week it sees no
    # confirmed results at all, scores everyone zero, and falls back to ranking
    # order, which reproduces week 1's matchups for every week of the league.
    league_rows = state.league_rows()
    if ad.prior_week_decided(league_rows, week):
        pairing = ad.compute_week_pairing(league_rows, week)
    else:
        # The same guard `next_week_rows` holds: with the previous week
        # unrecorded the pairing is not merely unknown, it is confidently wrong.
        pairing = ad.BracketIncomplete(
            reason="undecided",
            detail=(
                f"Week {week - 1}'s results decide who plays who in week {week}. "
                f"Record them and this fills in."
            ),
            found=0,
        )
    if isinstance(pairing, ad.BracketIncomplete):
        matches = ad.matches_from_recorded_opponents(rows)
        if not matches:
            embed.description += f"\n\n*{pairing.detail}*"
            return embed
    else:
        matches = list(pairing.matches)

    matches.sort(key=lambda m: 0 if state.own in (m.a, m.b) else 1)
    estimate = ad.make_estimator(state.profiles)

    lines = []
    for match in matches:
        own_side = state.own in (match.a, match.b)
        left, right = (match.a, match.b)
        if own_side and right == state.own:
            left, right = right, left
        marker = "**" if own_side else ""
        lines.append(
            f"{marker}{state.display_name(left)} vs {state.display_name(right)}{marker}\n"
            f"  {_match_status(state, left, right, week, estimate)}"
        )
    embed.description += "\n\n" + "\n".join(lines)[:3800]
    return embed


def _match_status(state: HubState, left, right, week: int, estimate) -> str:
    """One line saying who is favoured and on what evidence."""
    left_row = state.row_for(left, week)
    if left_row is not None and left_row.won is not None:
        winner = left if left_row.won else right
        mine = left_row.week_score
        # The two halves of a week always total 13, so the opponent's half is
        # arithmetic rather than a second lookup.
        score = f" ({mine}-{ad.WEEK_POINTS_TOTAL - mine})" if mine is not None else ""
        return f"✅ {state.display_name(winner)} took it{score}"

    left_profile = state.profiles.get(left)
    right_profile = state.profiles.get(right)
    if left_profile is None or right_profile is None:
        return "Not assessed."

    picked = left_row.picked if left_row is not None else None
    projection = ad.project_week(left_profile, right_profile, picked=picked)
    if projection.status == ad.SOURCE_UNASSESSED:
        return "Not assessed. Add power, members and gift level for both."

    favoured = left if projection.outlook in (ad.OUTLOOK_EASY, ad.OUTLOOK_LIKELY) else right
    label = {
        ad.SOURCE_PICKED: "Picked",
        ad.SOURCE_KNOWN: "Known read",
        ad.SOURCE_ESTIMATED: "Estimated",
    }.get(projection.status, "Estimated")
    if projection.outlook == ad.OUTLOOK_TOSSUP:
        return f"{label}: too close to call."
    return f"{label}: {state.display_name(favoured)} favored ({projection.outlook})."


# ── My path (#403) ────────────────────────────────────────────────────────────
#
# Weekly re-pairing is deterministic, so the specific set of *other* matches
# that must resolve before your next opponent is known is computable in
# advance. It is not "the whole bracket": it is a small, named set.
#
# That is the whole reason this view exists twice over. It answers "who do we
# play next" and, when it cannot, it answers the more useful question of what
# would have to be true for it to know, which is a scouting list rather than an
# apology.


def path_embed(state: HubState) -> discord.Embed:
    """The guild's projected route through the league, week by week.

    Each step says how firmly it is known, because a confirmed result and a
    coin-flip estimate reaching the same conclusion are not the same claim and
    must not render as though they were.
    """
    embed = discord.Embed(title=VS_PATH_TITLE, color=discord.Color.blurple())

    if state.own is None:
        embed.description = (
            "You have not told me which alliance is yours yet, so there is no path "
            f"to work out. Set it in {ad_setup.VS_SETUP_NAV}."
        )
        return embed

    estimate = ad.make_estimator(state.profiles)
    settled = ad.project_own_path(state.own, state.league_rows(), estimate=estimate)
    if isinstance(settled, ad.BracketIncomplete):
        embed.description = settled.detail
        return embed

    week = state.week or 1
    fork = ad.first_open_week(settled, week)
    lines = [
        f"**{state.league.season} · {state.league.tier} {state.league.group}** · week {week} of {ad.LEAGUE_WEEKS}"
    ]
    lines += _played_block(state, settled, fork)
    if fork is not None:
        lines.append(_disclaimer(fork))
    embed.description = "\n".join(line for line in lines if line)

    # The fork is on the first week we do not already know, so each branch is
    # one `assume` away. Both are projected in full rather than described,
    # because "who do we play if we lose" is the question the screen exists to
    # answer.
    for outcome, heading in () if fork is None else (("W", VS_PATH_IF_WIN), ("L", VS_PATH_IF_LOSE)):
        branch = ad.project_own_path(
            state.own,
            state.league_rows(),
            estimate=estimate,
            assume={fork: (state.own, outcome)},
        )
        if isinstance(branch, ad.BracketIncomplete):
            continue
        ahead = [step for step in branch.steps if step.week > fork]
        if ahead:
            embed.add_field(
                name=heading,
                value="\n".join(_step_line(state, step) for step in ahead)[:1024],
                inline=False,
            )

    embed.add_field(name="Your record", value=_record_line(state, settled), inline=False)
    footer = _path_footer(state, settled)
    if footer:
        embed.set_footer(text=footer)
    return embed


def _path_footer(state: HubState, projection: ad.PathProjection) -> str:
    """What would have to happen for the unnamed weeks to fill in.

    Split by cause, because the two halves ask for different work and a single
    "enter them" would send somebody to the predictions screen to find a match
    the bot would happily have predicted for them off numbers nobody typed.
    """
    scoutable, undecided = _split_blockers(state, projection)
    if not scoutable and not undecided:
        return ""
    parts = []
    if undecided:
        parts.append(FOOTER_UNDECIDED.format(subject=_counted(len(undecided), lead=True)))
    if scoutable:
        parts.append(FOOTER_SCOUTABLE.format(subject=_counted(len(scoutable), lead=not undecided)))
    return " ".join(parts)


def _counted(n: int, *, lead: bool) -> str:
    """The subject of one footer half: "3 matches need", "5 need".

    The noun drops on a following clause -- "3 matches need your prediction.
    5 need power, members and gift level." -- but comes back when that clause
    leads, because then there is no antecedent to elide to.
    """
    verb = "needs" if n == 1 else "need"
    if not lead:
        return f"{n} {verb}"
    return f"{n} {'match' if n == 1 else 'matches'} {verb}"


def _disclaimer(week: int) -> str:
    """Names the weeks the caveat covers, because it does not cover all of them.

    Once a week is recorded it is not a prediction any more, and a blanket
    "these are predictions" over a screen whose top half is recorded results
    would be the bot disclaiming something it actually knows.
    """
    ahead = [w for w in range(week + 1, ad.LEAGUE_WEEKS + 1)]
    if not ahead:
        return ""
    if week == 1:
        subject = "These are predictions"
    elif len(ahead) == 1:
        subject = f"Week {ahead[0]} is a prediction"
    else:
        subject = f"Weeks {ahead[0]} and {ahead[-1]} are predictions"
    return (
        f"{subject} from what has been entered here. They are not a guarantee "
        "of how any match will go."
    )


def _played_block(state: HubState, projection: ad.PathProjection, week: int | None) -> list[str]:
    """The weeks up to and including this one, which are not predictions.

    A recorded week says what happened and stops being a projection; the week
    being played says so rather than claiming a result it cannot have.
    """
    lines = []
    for step in projection.steps:
        if week is not None and step.week > week:
            break
        if step.opponent is None:
            continue
        if step.week == week:
            # With recorded weeks above it, this line has to carry its number
            # to sit in the same column as them, and then needs "Playing now"
            # to say why it has no result. Alone at the top of the screen the
            # number says nothing the reader does not know, and "this week"
            # carries the same fact in the label.
            label = _source_label(step.source, bare_when_confirmed=True)
            if lines:
                lines.append(f"**Week {step.week}:** {state.display_name(step.opponent)}{label}")
                if step.outcome_source != ad.SOURCE_CONFIRMED:
                    lines.append("Playing now.")
                    continue
            else:
                lines.append(f"**This week:** {state.display_name(step.opponent)}{label}")
                if step.outcome_source != ad.SOURCE_CONFIRMED:
                    continue
        else:
            lines.append(
                f"**Week {step.week}:** {state.display_name(step.opponent)}"
                + _source_label(step.source, bare_when_confirmed=True)
            )
        row = state.row_for(state.own, step.week)
        split = _week_split(row)
        verdict = "Won" if step.outcome == "W" else "Lost"
        lines.append(f"Result: {verdict}{split} `{VS_LABEL_RECORDED}`")
    return lines


def _week_split(row: ad.AllianceWeek | None) -> str:
    """The week score as the game prints it, or nothing when it wasn't typed.

    Every week adds to 13, so one side's score gives both.
    """
    if row is None or row.week_score is None:
        return ""
    return f" {row.week_score} to {ad.WEEK_POINTS_TOTAL - row.week_score}"


def _step_line(state: HubState, step: ad.PathStep) -> str:
    """One week of a branch, labelled by what the claim rests on.

    A week nobody has called is narrowed to the alliances that could fill it
    rather than left blank, because "one of four" is a scouting job and "not
    worked out yet" is a dead end.
    """
    if step.opponent is None:
        count = len(step.candidates)
        who = f"one of {count} alliances" if count else "not worked out yet"
        return f"**Week {step.week}:** {who} `{VS_LABEL_NONE}`"
    return f"**Week {step.week}:** {state.display_name(step.opponent)}" + _source_label(
        step.source, bare_when_confirmed=False
    )


def _source_label(source: str | None, *, bare_when_confirmed: bool) -> str:
    """The evidence chip for an opponent's identity, or nothing.

    `bare_when_confirmed` is for the played block, where the line underneath
    already says `Recorded result` against the score. Labelling it twice in two
    lines reads as a stutter; leaving the *unconfirmed* case bare reads as a
    fact, which is what this exists to stop.

    Picked and Known stay bare either way: no label means somebody in the
    alliance entered it, which is the rule the whole label set follows.
    """
    if source == ad.SOURCE_ESTIMATED:
        return f" `{VS_LABEL_BOT}`"
    if source == ad.SOURCE_CONFIRMED:
        return "" if bare_when_confirmed else f" `{VS_LABEL_RECORDED}`"
    return ""


def _record_line(state: HubState, projection: ad.PathProjection) -> str:
    """Wins and losses actually recorded. A week nobody typed in is neither."""
    wins = losses = 0
    for step in projection.steps:
        if step.outcome_source != ad.SOURCE_CONFIRMED:
            continue
        if step.outcome == "W":
            wins += 1
        elif step.outcome == "L":
            losses += 1
    return (
        f"{wins} {'win' if wins == 1 else 'wins'}, {losses} {'loss' if losses == 1 else 'losses'}"
    )


#: How each evidence source reads in the path. Plain words, because the reader
#: is being asked to judge how much to trust the step, and "SOURCE_ESTIMATED"
#: tells them nothing about that.
_SOURCE_WORDS = {
    ad.SOURCE_CONFIRMED: "recorded result",
    ad.SOURCE_PICKED: "your picked call",
    ad.SOURCE_KNOWN: "your Known read",
    ad.SOURCE_ESTIMATED: "estimated from stats",
}


def path_preview_embed(state: HubState, outcome: str) -> discord.Embed:
    """One branch of the fork, expanded as far as the bracket allows.

    The path screen can only say "one of four alliances", because it has two
    branches to fit and no room. This names the four, says which matches
    decide between them, and splits those by what would actually clear them.

    Both blocks live here rather than on the path screen for a second reason:
    they are per-branch. A match that gates the winning route may not gate the
    losing one, and a combined list would send someone to scout for a route
    they are not on.
    """
    heading = VS_PATH_IF_WIN if outcome == "W" else VS_PATH_IF_LOSE
    embed = discord.Embed(
        title=f"{VS_PATH_TITLE.split()[0]} {heading}", color=discord.Color.blurple()
    )

    week = state.week or 1
    projection = ad.project_own_path(
        state.own,
        state.league_rows(),
        estimate=ad.make_estimator(state.profiles),
        assume={week: (state.own, outcome)},
    )
    if isinstance(projection, ad.BracketIncomplete):
        embed.description = projection.detail
        return embed

    ahead = [step for step in projection.steps if step.week > week]
    embed.description = (
        f"**{state.league.season} · {state.league.tier} {state.league.group}**\n"
        + "\n".join(_preview_line(state, step) for step in ahead)
    )

    scoutable, undecided = _split_blockers(state, projection)
    if scoutable:
        embed.add_field(
            name=VS_PATH_BLOCKED_SCOUTABLE,
            value=_priority_block(state, scoutable),
            inline=False,
        )
    if undecided:
        embed.add_field(
            name=VS_PATH_BLOCKED_UNDECIDED,
            value=_undecided_block(state, undecided),
            inline=False,
        )
    return embed


def _preview_line(state: HubState, step: ad.PathStep) -> str:
    """One week of the branch, with the candidates named rather than counted.

    Counting them is the path screen's job, where two branches have to fit at
    once. Here there is room for the answer itself.
    """
    if step.opponent is not None:
        return _step_line(state, step)
    if not step.candidates:
        return f"**Week {step.week}:** not worked out yet. `{VS_LABEL_NONE}`"
    named = ", ".join(state.display_name(a) for a in step.candidates[:8])
    if len(step.candidates) > 8:
        named += f" *…and {len(step.candidates) - 8} more*"
    return f"**Week {step.week}:** one of {named} `{VS_LABEL_NONE}`"


def _split_blockers(
    state: HubState, projection: ad.PathProjection
) -> tuple[list[ad.Match], list[ad.Match]]:
    """The blocked matches, in two piles: scoutable, and down to a human.

    They want opposite things from the reader, so they can never share a list.
    Sending someone to scout an alliance whose power, members and gift level
    are all already recorded points them at the one action that cannot help.
    """
    scoutable: list[ad.Match] = []
    undecided: list[ad.Match] = []
    for match in projection.blocked_on:
        cause = ad.blocker_cause(match, state.profiles)
        if cause == ad.BLOCKED_MISSING_INPUTS:
            scoutable.append(match)
        else:
            undecided.append(match)
    return scoutable, undecided


def _priority_block(state: HubState, matches: list[ad.Match]) -> str:
    """The scouting list, which is the payoff for the manual entry burden.

    **Match-shaped, not alliance-shaped.** Naming the match is what turns a
    dead end into a task -- it says which fixture in the league is holding the
    path up, and the reader can see it on their own League screen. A bare list
    of alliances loses that and reads like homework.

    Each line asks for the inputs that match is actually short of rather than
    all three, so an alliance missing only a gift level is asked for a gift
    level.
    """
    lines = []
    for match in matches[:6]:
        lines.append(
            f"· {state.display_name(match.a)} vs {state.display_name(match.b)} "
            f"(week {match.week}): {_wanted(state, match)}"
        )
    if len(matches) > 6:
        lines.append(f"*…and {len(matches) - 6} more.*")
    return "\n".join(lines)[:1024]


def _wanted(state: HubState, match: ad.Match) -> str:
    """What one blocked match is short of, said the shortest true way.

    Both sides short of the same things collapses to "both need ..." rather
    than repeating the list twice, which is the common case on a league nobody
    has scouted yet.
    """
    short = {
        side: ad.missing_inputs(state.profiles.get(side))
        for side in (match.a, match.b)
        if ad.missing_inputs(state.profiles.get(side))
    }
    if len(short) == 2 and len(set(short.values())) == 1:
        return f"both need {_english_list(next(iter(short.values())))}"
    return "; ".join(
        f"{state.display_name(side)} needs {_english_list(wanted)}"
        for side, wanted in short.items()
    )


def _undecided_block(state: HubState, matches: list[ad.Match]) -> str:
    """The matches the model will not predict, which no scouting fixes.

    Every input already exists; the three metrics point in directions that
    cancel. So this list asks for a judgement rather than a number, and says
    nothing about what is missing, because nothing is.
    """
    lines = [
        f"· {state.display_name(match.a)} vs {state.display_name(match.b)} (week {match.week})"
        for match in matches[:6]
    ]
    if len(matches) > 6:
        lines.append(f"*…and {len(matches) - 6} more.*")
    return "\n".join(lines)[:1024]


def _english_list(items: tuple[str, ...]) -> str:
    """`a`, `a and b`, `a, b and c` — lowercased, since these are column names
    landing mid-sentence."""
    words = [item.lower() for item in items]
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


# ── Hub view ──────────────────────────────────────────────────────────────────


#: The path screen's own controls. The two previews sit on their own row above
#: the writes, matching the hub's reads-above-writes rule: a mis-tap on a phone
#: should land on something read-only.
VS_BTN_PREVIEW_WIN = "Preview winning path"
VS_BTN_PREVIEW_LOSE = "Preview losing path"


class VSPathView(discord.ui.View):
    """The controls under the path. Renders from `state`, never re-reads."""

    def __init__(self, state: HubState, owner_id: int):
        super().__init__(timeout=900)
        self.state = state
        self.owner_id = owner_id
        self.message: discord.Message | None = None

        for label, outcome in ((VS_BTN_PREVIEW_WIN, "W"), (VS_BTN_PREVIEW_LOSE, "L")):
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=0)
            button.callback = self._preview(outcome)
            self.add_item(button)

        # Scout hangs off the path because the path is where someone finds out
        # an alliance decides their week and knows nothing about them. Without
        # it they would have to go back to `/vs` and start again.
        scout = discord.ui.Button(label=VS_BTN_SCOUT, style=discord.ButtonStyle.secondary, row=0)
        scout.callback = self._scout
        self.add_item(scout)

        # Row 1 is the write row, matching the hub: reads above, writes below,
        # so a mis-tap on a phone lands on something read-only.
        predict = discord.ui.Button(
            label=ad_entry.VS_BTN_PREDICT_WEEK,
            style=discord.ButtonStyle.secondary,
            disabled=not ad_entry.week_matches(state, state.week or 1),
            row=1,
        )
        predict.callback = self._predict
        self.add_item(predict)

        results = discord.ui.Button(
            label=ad_entry.VS_BTN_RESULTS_WEEK,
            style=discord.ButtonStyle.secondary,
            disabled=state.own is None or state.week is None,
            row=1,
        )
        results.callback = self._results
        self.add_item(results)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(messages.DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        await wizard_registry.expire_view_message(self.message, command_hint=f"`{VS_HUB_CMD}`")

    def _preview(self, outcome: str):
        async def _open(interaction: discord.Interaction):
            await interaction.response.send_message(
                embed=path_preview_embed(self.state, outcome), ephemeral=True
            )

        return _open

    async def _scout(self, interaction: discord.Interaction):
        await ad_ui.open_scout_picker(interaction, self.state)

    async def _predict(self, interaction: discord.Interaction):
        week = self.state.week or 1
        view = ad_entry.PredictionsView(self.state, week, interaction.user.id, interaction.guild)
        await interaction.response.send_message(
            embed=ad_entry.predictions_embed(
                self.state, week, {}, interaction.guild, interaction.user.id
            ),
            view=view,
            ephemeral=True,
        )
        # The view edits this message on save and strips it on timeout, and it
        # can do neither without the handle. Same pattern as the hub's own.
        view.message = await interaction.original_response()

    async def _results(self, interaction: discord.Interaction):
        week = self.state.week or 1
        view = ResultsView(self.state, week, interaction.user.id)
        await interaction.response.send_message(
            embed=ad_entry.results_embed(self.state, week), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()


class ResultsView(discord.ui.View):
    """Screen 3's controls. Lives here rather than in `alliance_duel_entry`
    because Back re-renders the path, and entry cannot import the hub."""

    def __init__(self, state: HubState, week: int, owner_id: int):
        super().__init__(timeout=900)
        self.state = state
        self.week = week
        self.owner_id = owner_id
        self.message: discord.Message | None = None

        day = discord.ui.Button(
            label=ad_entry.VS_BTN_DAY_SCORES,
            style=discord.ButtonStyle.primary,
            disabled=state.own is None,
            row=0,
        )
        day.callback = self._day_scores
        self.add_item(day)

        others = discord.ui.Button(
            label=ad_entry.VS_BTN_OTHER_RESULTS,
            style=discord.ButtonStyle.secondary,
            disabled=not ad_entry.all_week_matches(state, week),
            row=0,
        )
        others.callback = self._other_results
        self.add_item(others)

        back = discord.ui.Button(
            label=ad_entry.VS_BTN_BACK_TO_PATH, style=discord.ButtonStyle.secondary, row=0
        )
        back.callback = self._back
        self.add_item(back)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(messages.DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        await wizard_registry.expire_view_message(self.message, command_hint=f"`{VS_HUB_CMD}`")

    async def _day_scores(self, interaction: discord.Interaction):
        view = ad_entry.DayPickerView(self.state, self.week, interaction.user.id, view=self)
        await interaction.response.send_message(
            ad_entry.VS_DAY_PICK_PROMPT, view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def _other_results(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            ad_entry.OtherResultsModal(self.state, self.week, view=self)
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Re-render after a write. The modal defers with `thinking=True`, so
        `edit_original_response` would edit that placeholder rather than the
        screen -- the view's own message is the one that has to change."""
        if self.message is None:
            return
        try:
            await self.message.edit(embed=ad_entry.results_embed(self.state, self.week), view=self)
        except discord.HTTPException:
            pass

    async def _back(self, interaction: discord.Interaction):
        view = VSPathView(self.state, self.owner_id)
        await interaction.response.edit_message(embed=path_embed(self.state), view=view)
        view.message = self.message
        self.stop()


class VSHubView(discord.ui.View):
    """The button grid. Renders from `state` and never re-reads the sheet."""

    def __init__(self, bot, state: HubState, owner_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.state = state
        self.owner_id = owner_id
        self.message: discord.Message | None = None

        has_league = state.league is not None
        bracket = discord.ui.Button(
            label=VS_BTN_BRACKET,
            style=discord.ButtonStyle.secondary,
            disabled=not has_league,
            row=0,
        )
        bracket.callback = self._bracket
        self.add_item(bracket)

        # The one recommended action on this surface, so the only `primary`.
        # Mid-week, the week's own matchups are what /vs was opened for.
        week = discord.ui.Button(
            label=VS_BTN_WEEK,
            style=discord.ButtonStyle.primary,
            disabled=not (has_league and state.week),
            row=0,
        )
        week.callback = self._week
        self.add_item(week)

        scout = discord.ui.Button(
            label=VS_BTN_SCOUT,
            style=discord.ButtonStyle.secondary,
            disabled=not has_league,
            row=0,
        )
        scout.callback = self._scout
        self.add_item(scout)

        path = discord.ui.Button(
            label=VS_BTN_PATH,
            style=discord.ButtonStyle.secondary,
            disabled=not has_league,
            row=0,
        )
        path.callback = self._path
        self.add_item(path)

        # Trends (#408) reads only the guild's own rows, so unlike its
        # neighbours it works in own-alliance tracking mode and needs no
        # league: an alliance that logged two weeks and nothing else still has
        # patterns worth reading.
        trends = discord.ui.Button(
            label=ad_ui.VS_BTN_TRENDS,
            style=discord.ButtonStyle.secondary,
            disabled=state.own is None,
            row=0,
        )
        trends.callback = self._trends
        self.add_item(trends)

        # Row 1 is the write row. Reads above, writes below, so a mis-tap on a
        # phone lands on something read-only rather than something that saves.
        log = discord.ui.Button(
            label=ad_entry.VS_BTN_LOG_SCORE,
            style=discord.ButtonStyle.secondary,
            disabled=not (state.own and ad_entry.target_day(state)),
            row=1,
        )
        log.callback = self._log_score
        self.add_item(log)

        add = discord.ui.Button(
            label=ad_entry.VS_BTN_ADD_ALLIANCE,
            style=discord.ButtonStyle.secondary,
            disabled=not has_league,
            row=1,
        )
        add.callback = self._add_alliance
        self.add_item(add)

        # Push or save (#407). Needs a live week to declare anything about, so
        # between leagues it renders disabled rather than opening a view that
        # has no week to write to.
        declare = discord.ui.Button(
            label=ad_entry.VS_BTN_DECLARE,
            style=discord.ButtonStyle.secondary,
            disabled=not (state.own and state.week),
            row=1,
        )
        declare.callback = self._declare
        self.add_item(declare)

        # Shown only when pressing it would actually write rows, per the
        # "every control can change something" rule. Between leagues, or
        # mid-week, there is nothing to advance and the button is absent
        # rather than present and inert.
        self.next_week = ad_entry.pending_next_week(state)
        if self.next_week is not None:
            advance = discord.ui.Button(
                label=ad_entry.VS_BTN_NEXT_WEEK, style=discord.ButtonStyle.secondary, row=1
            )
            advance.callback = self._next_week
            self.add_item(advance)

        # Same slot, same rule, and the two can never both apply: advancing
        # needs a week to advance from, and starting a league needs there to be
        # no live one. With nothing recorded at all this is the only control on
        # the hub that does anything, so it is `primary` in that state.
        elif ad_entry.pending_new_league(state):
            fresh = discord.ui.Button(
                label=ad_entry.VS_BTN_NEW_LEAGUE,
                style=(
                    discord.ButtonStyle.primary
                    if state.league is None
                    else discord.ButtonStyle.secondary
                ),
                row=1,
            )
            fresh.callback = self._new_league
            self.add_item(fresh)

        setup = discord.ui.Button(label=VS_BTN_SETUP, style=discord.ButtonStyle.secondary, row=1)
        setup.callback = self._setup
        self.add_item(setup)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(messages.DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        await wizard_registry.expire_view_message(self.message, command_hint=f"`{VS_HUB_CMD}`")

    async def _bracket(self, interaction: discord.Interaction):
        if not self.state.full_bracket:
            await interaction.response.send_message(
                embed=ad_setup.upsell_embed(
                    ad.BracketIncomplete(
                        reason="own_alliance_mode",
                        detail=(
                            "The bracket view shows all 16 alliances, and you are "
                            "tracking just your own."
                        ),
                    )
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=bracket_embed(self.state, self.state.week), ephemeral=True
        )

    async def _week(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=week_embed(self.state, self.state.week), ephemeral=True
        )

    async def _scout(self, interaction: discord.Interaction):
        from alliance_duel_ui import open_scout_picker

        await open_scout_picker(interaction, self.state)

    async def _path(self, interaction: discord.Interaction):
        if not self.state.full_bracket:
            await interaction.response.send_message(
                embed=ad_setup.upsell_embed(
                    ad.BracketIncomplete(
                        reason="own_alliance_mode",
                        detail=(
                            "Working out your path needs every alliance in the bracket, "
                            "and you are tracking just your own."
                        ),
                    )
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=path_embed(self.state),
            view=VSPathView(self.state, interaction.user.id),
            ephemeral=True,
        )

    async def _log_score(self, interaction: discord.Interaction):
        target = ad_entry.target_day(self.state)
        if target is None:
            await interaction.response.send_message(
                "⚠️ No duel week is running right now, so there is no day to log a "
                f"score against. Add this league's Week Dates, or open **{VS_BTN_SETUP}** "
                "for the column guide.",
                ephemeral=True,
            )
            return
        week, day = target
        await interaction.response.send_modal(
            ad_entry.ScoreModal(self.state, week, day, self.state.own_match(week))
        )

    async def _add_alliance(self, interaction: discord.Interaction):
        week = self.state.week or 1
        await interaction.response.send_modal(ad_entry.AllianceModal(self.state, week))

    async def _trends(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=ad_ui.trends_embed(self.state), ephemeral=True
        )

    async def _declare(self, interaction: discord.Interaction):
        week = self.state.week
        if week is None:
            await interaction.response.send_message(
                "⚠️ No duel week is running right now, so there is nothing to declare "
                f"yet. Add this league's Week Dates, or open **{VS_BTN_SETUP}** for the "
                "column guide.",
                ephemeral=True,
            )
            return
        view = ad_entry.DeclarationView(self.state, week, interaction.user.id)
        await interaction.response.send_message(
            embed=ad_entry.declaration_embed(self.state, week), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()

    async def _next_week(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        ok, detail = await ad_entry.generate_next_week(
            self.state, self.next_week, bot=interaction.client
        )
        await interaction.followup.send(f"{'✅' if ok else '⚠️'} {detail}", ephemeral=True)

    async def _new_league(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ad_entry.NewLeagueModal(self.state))

    async def _setup(self, interaction: discord.Interaction):
        from alliance_duel_wizard import run_vs_setup

        await run_vs_setup(interaction, self.bot)


# ── Entry point ───────────────────────────────────────────────────────────────


async def read_tab_once(guild_id: int, vs_cfg: dict):
    """Read the guild's VS tab off the event loop.

    Shared by `/vs` and by the daily score prompt's buttons (#405), which need
    the same snapshot hours after the prompt was posted and cannot hold one in
    memory across a restart. Returns None when the tab could not be read, which
    `load_rows` has already recorded through `config_health`.
    """
    return await asyncio.to_thread(
        ad_setup.load_rows, guild_id, vs_cfg.get("tab_name") or "Alliance Duel (VS)"
    )


async def handle_vs_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/vs`. Leadership plus Premium gated.

    The whole tracker is Premium, so free tier gets the upsell rather than a
    half-open hub. Everything derived from the sheet is what the alliance is
    paying for; they typed the raw values themselves.
    """
    from setup_cog import _has_leadership_or_admin

    if not _has_leadership_or_admin(interaction):
        cfg = config.get_config(interaction.guild_id)
        role = (cfg.leadership_role_name if cfg else None) or "Leadership"
        await interaction.response.send_message(
            f"⛔ You need the **{role}** role (or admin) to use the Alliance Duel tracker.",
            ephemeral=True,
        )
        return

    if not await premium.feature_gate(
        "alliance_duel_vs", interaction.guild_id, interaction=interaction, bot=bot
    ):
        await interaction.response.send_message(
            embed=premium.premium_locked_embed(
                feature_label="Alliance Duel (VS) tracker",
                description=(
                    "The VS tracker turns the league data you type into your sheet into a "
                    "readable bracket, a per-week projection, your path through the bracket, "
                    "and your record against every alliance you have faced. It's part of "
                    "LW Alliance Helper Premium. Run `/upgrade` to unlock it."
                ),
            ),
            view=premium.upgrade_view(),
            ephemeral=True,
        )
        return

    vs_cfg = config.get_vs_config(interaction.guild_id)
    if not vs_cfg.get("enabled"):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🏆 Alliance Duel (VS)",
                description=(
                    f"Not set up yet.\n\n{ad_setup.VS_WHAT_IT_IS} I will create "
                    f"the tab for you.\n\nStart at {ad_setup.VS_SETUP_NAV}."
                ),
                color=discord.Color.blurple(),
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    # The one sheet read. Everything below renders from this snapshot (#269).
    try:
        rows = await read_tab_once(interaction.guild_id, vs_cfg)
    except Exception as e:  # noqa: BLE001 - a bot bug, not the alliance's to fix
        logger.exception("[VS] hub load failed for guild %s", interaction.guild_id)
        await interaction.followup.send(
            f"⚠️ Couldn't read your sheet: {config.describe_sheet_error(e)}", ephemeral=True
        )
        return

    if rows is None:
        problems = config_health.problems_for_subjects(
            interaction.guild_id, [ad_setup.VS_SHEET_SUBJECT]
        )
        detail = config_health.describe(problems[0]) if problems else "I couldn't read the tab."
        await interaction.followup.send(
            embed=discord.Embed(
                title="🏆 Alliance Duel (VS)",
                description=f"⚠️ {detail}\n\nFix that and run `{VS_HUB_CMD}` again.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    state = HubState(interaction.guild_id, vs_cfg, rows)
    view = VSHubView(bot, state, interaction.user.id)
    await interaction.followup.send(embed=hub_embed(state), view=view, ephemeral=True)
    view.message = await interaction.original_response()

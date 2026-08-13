"""Alliance Duel (VS) — derived reads over the accumulated data (#408).

Everything here is computed on demand and nothing is stored. The rows already
carry day scores, day outcomes, week outcomes, power, tier and intent, so every
answer in this module is a different way of looking at the same tab.

Discord-free, like `alliance_duel.py`, so all of it is unit-testable against
literal rows. The embeds that render it live in `alliance_duel_ui.py`.

Three rules run through the whole module, and they are the reason several of
these functions return less than they easily could.

**Report the observation, never the verdict.** "Both sides scored about 40%
below their season averages" is a fact. "They saved" is an invented rating
about another alliance, which the design forbids outright. Every function here
returns numbers and counts; naming what they mean is left to the reader.

**Say how thin the evidence is, in the same breath as the finding.** "You lose
Age of Science 8 weeks in 12" and "you lost it once, in the only week recorded"
are different claims and must not render identically. Every result carries its
sample size, and the surfaces print it.

**Unrecorded is never a loss.** A blank Day Outcome means nobody typed it in.
It is excluded from both halves of every rate rather than counted against the
alliance.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Iterable, Sequence

import alliance_duel as ad


# ── Day profiles ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DayRecord:
    """One duel day's record for one alliance, across every recorded week."""

    day: int
    wins: int
    losses: int

    @property
    def played(self) -> int:
        return self.wins + self.losses

    @property
    def win_rate(self) -> float | None:
        """Wins as a share of *recorded* days, or None with nothing recorded.

        None rather than 0.0, because a day nobody has logged is not a day the
        alliance loses every time, and a float that reads 0% would say exactly
        that.
        """
        return self.wins / self.played if self.played else None

    @property
    def theme(self) -> str:
        return ad.DUEL_DAY_BY_NUMBER[self.day].theme


@dataclass(frozen=True)
class DayProfile:
    """How an alliance does on each of the six days.

    The most actionable output in the feature, and the one that needs no
    bracket at all: "you lose Age of Science in 8 of 12 weeks" is a resource
    instruction (bank research speedups), where a win probability is a vibe.
    """

    alliance: ad.AllianceKey
    days: tuple[DayRecord, ...] = ()

    @property
    def weeks_recorded(self) -> int:
        """Weeks contributing at least one recorded day outcome."""
        return max((d.played for d in self.days), default=0)

    def ranked(self, *, best_first: bool, minimum: int = 1) -> tuple[DayRecord, ...]:
        """Days ordered by win rate, skipping any with too little recorded.

        Ties break on the larger sample, so a day settled over eight weeks
        outranks the same rate over two.
        """
        eligible = [d for d in self.days if d.played >= minimum and d.win_rate is not None]
        return tuple(
            sorted(
                eligible,
                key=lambda d: (d.win_rate, d.played),
                reverse=best_first,
            )
        )

    def day_six(self) -> DayRecord | None:
        """Enemy Buster's record, which is the closest thing to a combat read
        that exists. No formula can predict day 6, but "we take it 7 weeks in
        10" is itself the answer, and a better basis for the 4-point call than
        any model would have been."""
        for record in self.days:
            if record.day == 6:
                return record
        return None


def day_profile(rows: Iterable[ad.AllianceWeek], alliance: ad.AllianceKey) -> DayProfile:
    """Aggregate `alliance`'s day outcomes across every row it appears in."""
    wins = {day: 0 for day in ad.DUEL_DAY_BY_NUMBER}
    losses = {day: 0 for day in ad.DUEL_DAY_BY_NUMBER}
    for row in rows:
        if row.alliance != alliance:
            continue
        for day, outcome in row.day_outcomes.items():
            if day not in wins:
                continue
            if outcome == "W":
                wins[day] += 1
            elif outcome == "L":
                losses[day] += 1
    return DayProfile(
        alliance=alliance,
        days=tuple(DayRecord(day, wins[day], losses[day]) for day in sorted(wins)),
    )


# ── Margins ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MarginShape:
    """How wide the day margins are, for weeks where both sides are recorded.

    Says more about headroom than a win rate does: winning four days by 2% and
    winning four by 60% are the same record and completely different positions.
    """

    days_compared: int = 0
    #: Signed share of the loser's score, positive when the alliance won, e.g.
    #: 0.25 for a 25% win. Newest first is not meaningful here, so these stay
    #: in day order for readability.
    margins: tuple[float, ...] = ()

    @property
    def median(self) -> float | None:
        if not self.margins:
            return None
        ordered = sorted(self.margins)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    @property
    def close_days(self) -> int:
        """Days decided by less than a tenth. The word "close" is defined here
        rather than in the copy, so every surface means the same thing by it."""
        return sum(1 for m in self.margins if abs(m) < CLOSE_MARGIN)

    @property
    def blowouts(self) -> int:
        return sum(1 for m in self.margins if abs(m) >= BLOWOUT_MARGIN)


#: What counts as close, and what counts as a blowout. Module-level so tuning
#: them later is a constant change rather than a rewrite, the same way the
#: prediction thresholds are held.
CLOSE_MARGIN = 0.10
BLOWOUT_MARGIN = 0.50


def margin_shape(
    rows: Iterable[ad.AllianceWeek], own: ad.AllianceKey, opponent: ad.AllianceKey | None = None
) -> MarginShape:
    """Margins across every day where both sides' scores are recorded.

    Only own matchups can produce this: both halves of a day score exist for
    weeks the guild played, and nowhere else. Passing `opponent` narrows it to
    one alliance.
    """
    by_league_week: dict[tuple, dict[ad.AllianceKey, ad.AllianceWeek]] = {}
    for row in rows:
        by_league_week.setdefault((row.league, row.week), {})[row.alliance] = row

    margins: list[float] = []
    for sides in by_league_week.values():
        mine = sides.get(own)
        if mine is None or mine.opponent is None:
            continue
        if opponent is not None and mine.opponent != opponent:
            continue
        theirs = sides.get(mine.opponent)
        if theirs is None:
            continue
        for day, ours in sorted(mine.day_scores.items()):
            other = theirs.day_scores.get(day)
            if not ours or not other:
                continue
            margins.append((ours - other) / max(ours, other))

    return MarginShape(days_compared=len(margins), margins=tuple(margins))


# ── Raw points against league points ──────────────────────────────────────────


@dataclass(frozen=True)
class Divergence:
    """A week whose raw scoring and whose league points disagree."""

    league: ad.LeagueKey
    week: int
    own_total: int
    opponent_total: int
    outcome: str

    @property
    def outscored_and_lost(self) -> bool:
        return self.outcome == "L" and self.own_total > self.opponent_total

    @property
    def outscored_and_won(self) -> bool:
        return self.outcome == "W" and self.own_total < self.opponent_total


def divergences(rows: Iterable[ad.AllianceWeek], own: ad.AllianceKey) -> tuple[Divergence, ...]:
    """Weeks where total points and the week result point different ways.

    A week can be lost while outscoring the opponent overall: win two days by a
    lot, lose four narrowly. Worth surfacing rather than leaving in the data,
    because "we outscored them and lost" is exactly the kind of week that gets
    misremembered as bad luck when it is actually a distribution problem.
    """
    by_league_week: dict[tuple, dict[ad.AllianceKey, ad.AllianceWeek]] = {}
    for row in rows:
        by_league_week.setdefault((row.league, row.week), {})[row.alliance] = row

    found: list[Divergence] = []
    for (league, week), sides in by_league_week.items():
        mine = sides.get(own)
        if mine is None or mine.week_outcome is None or mine.opponent is None:
            continue
        theirs = sides.get(mine.opponent)
        if theirs is None:
            continue
        shared = set(mine.day_scores) & set(theirs.day_scores)
        if not shared:
            continue
        mine_total = sum(mine.day_scores[d] for d in shared)
        theirs_total = sum(theirs.day_scores[d] for d in shared)
        found.append(Divergence(league, week, mine_total, theirs_total, mine.week_outcome))

    return tuple(d for d in found if d.outscored_and_lost or d.outscored_and_won)


# ── Engagement against a baseline ─────────────────────────────────────────────


@dataclass(frozen=True)
class EngagementRead:
    """One week's scoring level against each side's own normal.

    The observation, never the verdict. Close scores that are both well below
    what those two alliances usually post looks nothing like a close week at
    usual levels, and the difference is worth showing. Calling it a save is an
    invented rating about someone else's alliance and does not ship.
    """

    week: int
    own_share: float | None
    opponent_share: float | None
    baseline_weeks: int

    @property
    def has_baseline(self) -> bool:
        """Whether enough other weeks exist for the shares to mean anything."""
        return self.baseline_weeks >= MIN_BASELINE_WEEKS


#: Weeks of history needed before an engagement read says anything at all.
#: Below this the "baseline" is one or two weeks, and comparing a week against
#: itself plus one other is noise wearing a percentage sign.
MIN_BASELINE_WEEKS = 3


def engagement(
    rows: Iterable[ad.AllianceWeek], own: ad.AllianceKey, week: int
) -> EngagementRead | None:
    """How `week` compares with each side's own average across other weeks.

    Returns None when the week itself is not fully recorded. A read with too
    thin a baseline still comes back, carrying ``has_baseline = False``, so the
    caller can say "not enough history yet" rather than guessing why it got
    nothing.
    """
    rows = list(rows)
    by_week: dict[int, dict[ad.AllianceKey, ad.AllianceWeek]] = {}
    for row in rows:
        by_week.setdefault(row.week, {})[row.alliance] = row

    target = by_week.get(week, {})
    mine = target.get(own)
    if mine is None or mine.opponent is None:
        return None
    theirs = target.get(mine.opponent)
    if theirs is None:
        return None

    def _total(row: ad.AllianceWeek) -> int:
        return sum(v for v in row.day_scores.values() if v)

    def _baseline(alliance: ad.AllianceKey) -> tuple[float | None, int]:
        totals = [
            _total(sides[alliance])
            for w, sides in by_week.items()
            if w != week and alliance in sides and _total(sides[alliance])
        ]
        if not totals:
            return None, 0
        return sum(totals) / len(totals), len(totals)

    own_avg, own_weeks = _baseline(own)
    opp_avg, opp_weeks = _baseline(mine.opponent)
    own_total, opp_total = _total(mine), _total(theirs)

    return EngagementRead(
        week=week,
        own_share=(own_total / own_avg) if own_avg else None,
        opponent_share=(opp_total / opp_avg) if opp_avg else None,
        baseline_weeks=min(own_weeks, opp_weeks),
    )


# ── Power movement ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PowerJump:
    """How an alliance's recorded power has moved between two entries."""

    alliance: ad.AllianceKey
    previous: int
    latest: int
    weeks_between: int

    @property
    def change(self) -> float:
        return (self.latest - self.previous) / self.previous if self.previous else 0.0

    @property
    def is_material(self) -> bool:
        return abs(self.change) >= MATERIAL_POWER_MOVE


#: How much movement is worth mentioning. Power drifts upward constantly, so a
#: threshold keeps "up 1% since you last met" out of the surfaces entirely.
MATERIAL_POWER_MOVE = 0.10


def power_jump(rows: Iterable[ad.AllianceWeek], alliance: ad.AllianceKey) -> PowerJump | None:
    """The movement between the two most recent recorded power values.

    Falls out for free because power is snapshotted on every league-week row,
    so nobody has to re-scout to learn that an alliance grew. Returns None with
    fewer than two entries, which is the normal state early on.
    """
    entries = sorted(
        (row for row in rows if row.alliance == alliance and row.power),
        key=lambda r: (r.week_date or _dt.date.min, r.week),
    )
    if len(entries) < 2:
        return None
    previous, latest = entries[-2], entries[-1]
    return PowerJump(
        alliance=alliance,
        previous=previous.power,
        latest=latest.power,
        weeks_between=max(1, latest.week - previous.week),
    )


# ── Season trajectory ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SeasonRecord:
    """One league's record for the guild, with the tier it was earned in."""

    league: ad.LeagueKey
    wins: int
    losses: int

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}"

    @property
    def tier_rank(self) -> int | None:
        return ad.tier_rank(self.league.tier)


def season_trajectory(
    rows: Iterable[ad.AllianceWeek], own: ad.AllianceKey
) -> tuple[SeasonRecord, ...]:
    """The guild's record league by league, oldest first.

    Tier travels with each league rather than being flattened, because a 3-1 in
    Gold and a 3-1 in Diamond are not the same season and averaging them would
    hide the only movement that is game-adjudicated rather than inferred.
    """
    by_league: dict[ad.LeagueKey, list[ad.AllianceWeek]] = {}
    for row in rows:
        if row.alliance == own and row.league is not None:
            by_league.setdefault(row.league, []).append(row)

    records = [
        SeasonRecord(
            league=league,
            wins=sum(1 for r in weeks if r.week_outcome == "W"),
            losses=sum(1 for r in weeks if r.week_outcome == "L"),
        )
        for league, weeks in by_league.items()
    ]
    records = [r for r in records if r.wins or r.losses]
    records.sort(key=lambda r: (r.league.season, r.league.tier, r.league.group))
    return tuple(records)


# ── Pick accuracy ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PickAccuracy:
    """How often the guild's Picked calls matched the week that followed.

    Measured only over the sample :func:`alliance_duel.partition_by_intent`
    leaves in, so a week the alliance conceded on purpose never counts as a
    failed prediction.
    """

    correct: int = 0
    wrong: int = 0
    #: Weeks in the sample that carried no Picked call at all. Reported so the
    #: rate is read against how often anyone actually predicted, not against
    #: the season.
    unpicked: int = 0
    #: How much of the sample was undeclared rather than a stated push.
    rests_on_assumption: int = 0
    #: Weeks excluded because they were declared saves.
    excluded_saves: int = 0

    @property
    def judged(self) -> int:
        return self.correct + self.wrong

    @property
    def rate(self) -> float | None:
        return self.correct / self.judged if self.judged else None


def pick_accuracy(rows: Iterable[ad.AllianceWeek], own: ad.AllianceKey) -> PickAccuracy:
    """Score the guild's own Picked calls against what happened.

    **The sample is never clean, only cleaner**, and any surface rendering this
    has to say so: a declared push can still be contaminated by an opponent
    quietly saving, which is unobservable from our side. What the partition
    buys is the removal of the one contamination we *can* see, our own.
    """
    partition = ad.partition_by_intent(row for row in rows if row.alliance == own)

    correct = wrong = unpicked = 0
    for row in partition.sample:
        if row.picked is None:
            unpicked += 1
        elif row.picked == row.week_outcome:
            correct += 1
        else:
            wrong += 1

    return PickAccuracy(
        correct=correct,
        wrong=wrong,
        unpicked=unpicked,
        rests_on_assumption=partition.rests_on_assumption,
        excluded_saves=len(partition.excluded),
    )


# ── Formatting helpers shared by the surfaces ─────────────────────────────────


def pct(value: float | None, *, signed: bool = False) -> str:
    """A share as a whole-number percentage. `None` renders as the shared
    not-entered glyph rather than as 0%, which would be a different claim."""
    if value is None:
        return ad.NOT_ENTERED
    rendered = f"{abs(value) * 100:.0f}%"
    if signed:
        return f"+{rendered}" if value >= 0 else f"-{rendered}"
    return rendered


def sample_words(n: int, unit: str = "week") -> str:
    """ "in 8 weeks" / "in 1 week", for the sample size that has to travel with
    every finding in this module."""
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def sorted_by_week(rows: Sequence[ad.AllianceWeek]) -> list[ad.AllianceWeek]:
    return sorted(rows, key=lambda r: (r.week_date or _dt.date.min, r.week))

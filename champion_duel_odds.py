"""Odds of advancing out of one group.

A port of `bracket_simulator/simulate_semis.py` from `champion-duel-simulator`,
not new work. The shape is deliberately the same so the two can be compared: a
pairwise meeting matrix computed once, then a Monte Carlo over the round robin.
What changes is where the per-match probability comes from. The simulator has a
logistic on a z-scored power gap and no squad model at all; here every meeting
goes through the engine on real stored squads and deployment orders, which is
strictly more information about the two players actually meeting.

**A meeting is not a match.** Each pair in a semifinal group of 8 meets exactly
once and that meeting is a best-of-3 (Kevin, 2026-08-15). A Bo3 favours the
stronger player, so resolving one as a single match understates strong players
and overstates weak ones -- in a direction that looks entirely plausible, which
is why it went unnoticed in the workbooks for as long as it did. `best_of` is
passed explicitly on every call here and is a required argument throughout this
module. The engine defaults it to 1, which is the identity, so a caller that
forgets it gets a believable number at the wrong series length rather than an
error.

**Two tie-break rules, ranked from the same simulated win totals**, exactly as
the workbook does it. Seven meetings still tie often, and the in-game tie-break
for this stage is not documented anywhere we can check:

- ``p_advance`` breaks ties toward the stronger player. In-game standings are
  points-based and points track damage dealt, so the stronger player really is
  favoured to win a tie-break. This is the primary number.
- ``p_advance_coinflip`` breaks ties at random: the floor for a favourite and
  the ceiling for an underdog.

Reporting one number alone would state a precision we do not have. Surfaces
show the pair as a range wherever they differ.

Strength for the first rule is the player's own row-sum of the meeting matrix,
which is their expected wins against this group. The simulator uses a z-scored
power score instead, and that is not available here for a good reason: the
z-scores are taken against the full 1,600-player registration field, and
re-scoring within one group of 8 would shrink the standard deviation to that
group's spread and make every meeting look near-certain. The row-sum is the
prediction's own read on who is stronger, needs no field-wide scale, and is
already computed.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import champion_duel_predict as predict_lib

#: Trials per group. The workbook runs far more, but it scores 16 groups
#: offline while this runs inside a Discord interaction: 4,000 trials over 28
#: meetings settles a two-of-eight question well inside a percentage point,
#: which is finer than the copy ever renders.
TRIALS = 4000

#: How many of a semifinal group of 8 go through to the knockout bracket.
ADVANCE = 2


@dataclass
class OddsRow:
    """One player's chance of advancing, under both tie-break rules."""

    name: str
    p_advance: float
    p_advance_coinflip: float

    @property
    def is_range(self) -> bool:
        """Whether the two tie-break rules actually disagree for this player.

        Rounded to whole percentage points first, because that is what the
        surface renders: a pair that differs in the fourth decimal is one
        number as far as the reader is concerned, and showing it as a range
        would invent a disagreement.
        """
        return round(self.p_advance * 100) != round(self.p_advance_coinflip * 100)


@dataclass
class GroupOdds:
    """The whole group's odds, plus what could not be included and why."""

    rows: list[OddsRow] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    trials: int = TRIALS
    best_of: int = 3
    advance: int = ADVANCE


class NotEnoughPlayers(Exception):
    """Fewer than two players could be predicted, so there is nothing to run.

    Distinct from a group we hold incompletely: that is about how many names
    were recorded, this is about how many of them we can predict. A member hits
    the second far more often than the first, and the two need different copy.
    """


def _meeting_matrix(sides: list, best_of: int) -> list[list[float]]:
    """P(row beats column) over a best-of-`best_of` meeting, for every pair.

    Computed once and reused across every trial, which is what makes 4,000
    trials cheap: the engine runs 28 times, not 28 times 4,000.

    The lower triangle is filled as ``1 - p`` rather than by predicting the
    reverse fixture. That is exact at any odd series length, because
    ``series_win_prob(1 - p, n) == 1 - series_win_prob(p, n)``: a meeting has
    no draw, so the two sides' chances are complements before and after the
    series transform. It also halves the engine calls.
    """
    n = len(sides)
    probs = [[0.5] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            p = predict_lib.predict_pair(sides[i], sides[j], best_of=best_of)
            probs[i][j] = p
            probs[j][i] = 1.0 - p
    return probs


def _run(
    probs: list[list[float]], *, trials: int, advance: int, rng
) -> tuple[list[int], list[int]]:
    """Monte Carlo the round robin, counting advances under both tie-breaks.

    Both orderings come from the *same* simulated win totals rather than from
    two independent runs, so the pair brackets one answer instead of being two
    unrelated ones that happen to sit near each other.
    """
    n = len(probs)
    by_strength = [0] * n
    by_coinflip = [0] * n
    # Expected wins against this group, which is the tie-break's notion of
    # stronger. Constant across trials, so it is computed once.
    strength = [sum(probs[i][j] for j in range(n) if j != i) for i in range(n)]

    for _ in range(trials):
        wins = [0] * n
        for i in range(n):
            row = probs[i]
            for j in range(i + 1, n):
                if rng.random() < row[j]:
                    wins[i] += 1
                else:
                    wins[j] += 1
        ranked = sorted(range(n), key=lambda i: (wins[i], strength[i]), reverse=True)
        flipped = sorted(range(n), key=lambda i: (wins[i], rng.random()), reverse=True)
        for i in ranked[:advance]:
            by_strength[i] += 1
        for i in flipped[:advance]:
            by_coinflip[i] += 1
    return by_strength, by_coinflip


def group_advance_odds(
    members: list[dict],
    *,
    best_of: int,
    advance: int = ADVANCE,
    trials: int = TRIALS,
    seed: int | None = None,
) -> GroupOdds:
    """Everyone's chance of finishing in the top `advance` of their group.

    `members` are rows as `get_group_members` returns them. Anyone we cannot
    build a prediction for is left out and named in `skipped` rather than
    dropped silently or filled in with an average: a group scored over six of
    eight is not this group, and a surface that does not say so is claiming a
    completeness it does not have.

    `seed` fixes the RNG for tests. Live callers leave it None.
    """
    if not predict_lib.ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")

    sides = []
    names = []
    skipped = []
    for row in members:
        name = row.get("display_name") or ""
        try:
            sides.append(predict_lib.build_side(row))
            names.append(name)
        except predict_lib.NotEnoughData:
            skipped.append(name)

    if len(sides) < 2:
        raise NotEnoughPlayers(
            f"only {len(sides)} of {len(members)} players in this group can be predicted"
        )

    rng = random.Random(seed)
    probs = _meeting_matrix(sides, best_of)
    by_strength, by_coinflip = _run(probs, trials=trials, advance=advance, rng=rng)

    rows = [
        OddsRow(
            name=names[i],
            p_advance=by_strength[i] / trials,
            p_advance_coinflip=by_coinflip[i] / trials,
        )
        for i in range(len(sides))
    ]
    rows.sort(key=lambda r: r.p_advance, reverse=True)
    return GroupOdds(
        rows=rows,
        skipped=skipped,
        trials=trials,
        best_of=best_of,
        advance=advance,
    )

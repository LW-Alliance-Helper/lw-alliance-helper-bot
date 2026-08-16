"""Odds of advancing out of one semifinal group.

**The model lives in `champion_duel_engine`, not here.** This module is the join
between what the bot stores and the shape `semifinal.simulate_group` wants, and
nothing else. It holds no constants, no Monte Carlo and no ranking rule.

The version this replaces was a port: a pairwise meeting matrix, a round robin
Monte Carlo over it, and two tie-break rules. It answered the wrong question.
A semifinal round does not rank on meetings won -- it ranks on **points
accumulated across all 21 matches**, so a player can lose a meeting and still
gain on the group by taking two slots in it. The engine's own docstring is
blunt about not reaching across between the two models, and the old port was
reaching across without knowing it.

WHAT THE MODEL NEEDS, AND WHAT IT WILL NOT DO WITHOUT

- **Exactly eight players.** `_check` refuses anything else rather than
  absorbing it: with an odd count the circle method silently drops a player
  from every round, and past eight the day schedule runs out. Both produce
  numbers and neither produces the round being modelled. So an incomplete group
  gets no odds at all, which is a harder stop than the old port had and is the
  correct one.
- **THP for every one of them.** Everything else is optional.

WHAT IT WILL TAKE, AND SAMPLE WHAT IT LACKS

`build_player` derives three squad powers from THP (`squad(M) = a + b x THP(M)`,
fitted on the 41-player corpus) and samples types, mix and capacity. A
`profile` overrides any part of that with something we actually hold, per
field, so an alliance sharing everything and an alliance sharing only THP go
through one path:

- `shape`  -- the squad power ratios, from stored squad powers. Supplying only
  the second ratio is fine; the engine derives the third from it.
- `types`  -- the three squad types, when we hold all three.

`jitter` stays on. Without it the model treats an estimate as a measurement,
which the engine calls the single biggest source of false confidence in the old
workbook. These odds are supposed to include our uncertainty about what people
are actually fielding, not just match variance.
"""

from __future__ import annotations

from dataclasses import dataclass, field

try:
    from champion_duel_engine import semifinal

    ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - asserted through the degraded path
    ENGINE_AVAILABLE = False

#: Trials per group, and deliberately not the engine's 4,000 default.
#:
#: The old port precomputed a 28-entry matrix and then spent one RNG draw per
#: meeting, so trials were nearly free. This rebuilds all eight players every
#: trial and plays 28 meetings of 3 matches, each up to three slot resolutions.
#: Measured on a development machine, 4,000 trials is about 15 seconds of pure
#: Python, and pure Python holds the GIL: the interaction survives because it
#: runs in a thread, but the bot's event loop does not get a look in for that
#: whole time, on one container, for every press.
#:
#: 800 keeps a two-of-eight question inside a percentage point, which is finer
#: than the surface ever renders, at roughly three seconds. The number is a
#: latency budget rather than a statistical one and should move if either
#: changes.
TRIALS = 800

SLOTS = (1, 2, 3)


@dataclass
class OddsRow:
    """One player's odds, straight off the engine."""

    name: str
    advance: float
    win_group: float
    points_mean: float
    points_sd: float


@dataclass
class GroupOdds:
    rows: list[OddsRow] = field(default_factory=list)
    trials: int = TRIALS


class NotEnoughData(Exception):
    """The group cannot be modelled, with a reason a member can act on.

    Carries `missing_thp` so a surface can name who to go and look up, rather
    than saying the group is not ready and leaving the reader to work out which
    of the eight is the problem.
    """

    def __init__(self, message: str, *, missing_thp: list[str] | None = None):
        super().__init__(message)
        self.missing_thp = missing_thp or []


def _profile(member: dict) -> dict | None:
    """What we actually hold about this player's squads, in the engine's shape.

    Returns None when we hold nothing usable, which is the normal case and is
    exactly what `build_player` expects when it is going to sample instead.

    Three things about this mapping are easy to get wrong and produce a
    plausible number rather than an error.

    **Ordered by power, together.** The engine reads `shape` and `types` as
    biggest squad first; the bot's slots are lineup positions and carry no
    ordering. Sorting the powers and leaving the types alone puts every squad
    type on the wrong squad and turns every counter-triangle decision over.
    They are sorted as pairs here for that reason.

    **All three or nothing, for both.** A shape from squads 1 and 3 with 2
    missing would hand the engine the third ratio as if it were the second,
    modelling a player's second squad at roughly a third of their top when the
    corpus puts it near 0.9. Sampling the whole shape is far closer than that.

    **The ratios are base-to-base and the panel is not.** `build_player` strips
    the gorilla off the displayed top squad before applying the shape, warning
    that getting the order wrong "inflates squads 2 and 3 by the whole
    gorilla". The gorilla sits on the biggest squad in most lineups at about a
    tenth of it, so dividing raw panel figures understates a scouted player's
    lower squads by that much, and only scouted players -- everyone else's
    shape is drawn correctly. The top squad is de-gorilla'd here before the
    ratios are taken.
    """
    squads = {s["slot"]: s for s in (member.get("squads") or [])}
    out: dict = {}

    pairs = [
        (float(squads[s]["power"]), squads[s].get("squad_type"))
        for s in SLOTS
        if squads.get(s) and squads[s].get("power")
    ]
    if len(pairs) == len(SLOTS):
        pairs.sort(key=lambda p: p[0], reverse=True)
        powers = [p for p, _ in pairs]
        base_top = powers[0] * (1.0 - semifinal.GORILLA_FRACTION)
        if base_top > 0:
            out["shape"] = (powers[1] / base_top, powers[2] / base_top)

        types = [t for _, t in pairs]
        # Anything outside the engine's triangle would raise a KeyError deep
        # inside a trial rather than here, so it is dropped and sampled.
        if all(t in semifinal.TYPES for t in types):
            out["types"] = types

    return out or None


def group_advance_odds(
    members: list[dict],
    *,
    trials: int = TRIALS,
    seed: int = 42,
    jitter: bool = True,
) -> GroupOdds:
    """Everyone's chance of getting out of this group.

    `members` are rows as `get_group_scouting` returns them: the group listing
    plus each player's squads. THP rides on the registrant row.

    Raises `NotEnoughData` rather than scoring a partial group. The engine
    refuses anything but eight, and filling the gaps with invented players
    would produce a number about a group that does not exist.
    """
    if not ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")

    # Size before power, deliberately. A short group usually also has people we
    # hold no THP for, and telling someone to go and look up two players' power
    # when they are four names short points at the smaller job. The engine's
    # own constant rather than an 8 here: the group size is the model's, and a
    # copy of it in the bot is a copy that can go stale.
    if len(members) != semifinal.GROUP_SIZE:
        raise NotEnoughData(
            f"the group has {len(members)} players; the semifinal model is "
            f"calibrated on groups of {semifinal.GROUP_SIZE}"
        )

    missing = [(m.get("display_name") or "?") for m in members if not m.get("thp")]
    if missing:
        raise NotEnoughData(
            f"{len(missing)} of {len(members)} players have no Total Hero Power",
            missing_thp=missing,
        )

    # The engine keys everything on `name`, so two members sharing one would
    # collapse into a single simulated player: one vanishes from the results
    # and the other accumulates both their points. A semifinal group is drawn
    # from sixteen warzones and `registrants` is unique on (name, server)
    # rather than on name, so this is a live case rather than a hypothetical.
    # Keyed by row position, which is unique by construction, and mapped back
    # for display afterwards.
    display = {str(i): (m.get("display_name") or "?") for i, m in enumerate(members)}
    specs = [
        {"name": str(i), "thp": float(m["thp"]), "profile": _profile(m)}
        for i, m in enumerate(members)
    ]
    scored = semifinal.simulate_group(specs, trials=trials, seed=seed, jitter=jitter)

    rows = [
        OddsRow(
            name=display[key],
            advance=vals["advance"],
            win_group=vals["win_group"],
            points_mean=vals["points_mean"],
            points_sd=vals["points_sd"],
        )
        for key, vals in scored.items()
    ]
    rows.sort(key=lambda r: r.advance, reverse=True)
    return GroupOdds(rows=rows, trials=trials)

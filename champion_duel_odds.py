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
  numbers and neither produces the round being modelled.
- **Something to place each of them by**, which is a THP or any single squad
  power. Only a player with neither has nothing at all, and the engine raises
  rather than inventing a lineup.

WHAT IT TAKES, AND WHAT IT SAMPLES INSTEAD

Every field is optional and a partial reading is the normal case. Given powers
are used exactly; missing ones are filled from the shape fit and carry the
estimate residual, which a typed number does not. A reading whose rank cannot
be read off the input is placed by matching against the THP-estimated lineup,
so one box filled resolves correctly whether it is that player's strongest
squad or their weakest.

- `squads`   up to three boxes in LINEUP ORDER, each a raw power and a type,
             either of which may be missing. Never sorted here.
- `thp`      what anchors a partial reading. Sent whenever we hold it, even
             when all three squads are present.
- `level`    troop level 1-11, defaulting to 11. Only matters across
             mixed-level matchups: within a group where everyone is the same
             level, ranking is unaffected. Absent until the bot collects it.
- `profile`  what the boxes cannot carry, which is now only `mixed`.

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

    Carries `missing_thp` so a surface can name who is short, rather than
    saying the group is not ready and leaving the reader to work out which of
    the eight is the problem. The name is now narrower than the meaning: since
    1.5 it lists players with neither a THP nor any squad power, because either
    one places them.

    An empty `missing_thp` means the refusal was about group size instead.
    `build_odds_embed` branches its copy on exactly that, so the two must stay
    distinguishable.
    """

    def __init__(self, message: str, *, missing_thp: list[str] | None = None):
        super().__init__(message)
        self.missing_thp = missing_thp or []


def _squads(member: dict) -> list[dict]:
    """This player's three lineup boxes, exactly as they were entered.

    In SLOT order, never sorted. The engine sorts internally and hands back an
    `order` mapping so it can translate anything else indexed against these
    boxes -- `mixed` above all. Sorting here would silently break that
    translation while still producing a lineup.

    Powers go out raw, which is how `parse_power` stores them and what
    `measured_base` expects; it divides by a million itself.

    **Not de-gorilla'd.** A player reads squad power off the panel and it
    already includes the gorilla, so the engine strips it from the strongest
    reading. Deflating here would strip it twice and shrink the whole lineup,
    because the other two squads are derived from the top one. An earlier
    version of this file did exactly that.
    """
    squads = {s["slot"]: s for s in (member.get("squads") or [])}
    out = []
    for slot in SLOTS:
        entry = squads.get(slot) or {}
        # `estimated` rows are THP run through the fitted ratios, which
        # `push_to_bot` writes for nearly the whole field. Forwarding one as a
        # reading is the worst thing this module can do: `measured_base` uses a
        # given power EXACTLY, on the stated grounds that a number somebody
        # typed is not the THP fit being wrong, so the estimate residual never
        # applies. The engine would then hand back near-certainty for a group
        # nobody has looked at -- measured at 100/90/10/0 against a true
        # 85/68/28/14 -- under a footer promising that unseen squads are
        # sampled. The engine derives exactly these numbers itself, better,
        # because it keeps the uncertainty attached.
        if entry.get("source") == "estimated":
            entry = {}
        power = entry.get("power")
        squad_type = entry.get("squad_type")
        out.append(
            {
                "power": float(power) if power else None,
                "type": squad_type if squad_type in semifinal.TYPES else None,
            }
        )
    return out


def _profile(member: dict, squads: list[dict]) -> dict | None:
    """What we measured that the squad boxes cannot carry.

    Types and lineup shape both ride on `squads` now, so this is only `mixed`:
    which squads are 4-of-a-type rather than 5.

    **`mixed` means two different things depending on whether any power was
    read**, which is the sharpest edge in this contract. With at least one
    power the engine treats it as positions against the input boxes and
    translates through its own sort. With none, `measured_base` returns None,
    the THP path runs, and the same tuple is applied directly as POWER RANKS.
    We know which box a squad is, never which rank, so it is only sent on the
    first path. Sending it on the second would land a 3.3% penalty on whichever
    squads happened to sort into those positions.

    An empty tuple is a measurement -- "we looked and every squad is pure" --
    and is deliberately distinct from absent, so it is only sent when somebody
    actually answered.
    """
    mixed = member.get("mixed_squads")
    if mixed is None:
        return None
    mixed = tuple(mixed)
    # An empty answer carries no rank ambiguity: there are no positions to
    # translate, so it means the same thing on both paths and is always worth
    # sending. Dropping it would make the engine sample a mixed pair from the
    # population and put a 3.3% penalty on two squads of a player we were told
    # has none.
    if mixed and not any(s["power"] for s in squads):
        return None
    return {"mixed": mixed}


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

    # Keyed by row position rather than display name. The engine keys `pts`,
    # `seen` and its summary entirely on `name`, so two members sharing one
    # would collapse into a single simulated player: one vanishes from the
    # results and the other banks both their points. A semifinal group is drawn
    # from sixteen warzones and `registrants` is unique on (name, server)
    # rather than on name, so this is live rather than hypothetical. Mapped
    # back through `display` afterwards.
    missing = []
    display = {}
    specs = []
    for i, member in enumerate(members):
        key = str(i)
        display[key] = member.get("display_name") or "?"
        squads = _squads(member)
        thp = member.get("thp")
        # A player is buildable from THP, from any single squad power, or from
        # both. Only somebody with neither has nothing to place them by, and
        # the engine raises rather than inventing a lineup for them.
        if not thp and not any(s["power"] for s in squads):
            missing.append(display[key])
            continue
        spec = {"name": key, "squads": squads}
        # Sent even when the squads are complete. It is what anchors a partial
        # reading to a rank, and one squad power plus THP places a player
        # materially better than either alone.
        if thp:
            spec["thp"] = float(thp)
        # Validated here rather than passed through. `scoring.troop_value`
        # raises for anything outside 1-11, `build_odds_embed` catches only
        # NotEnoughData, and the interaction has already been deferred -- so a
        # bad level would leave a member watching a spinner that never
        # resolves. Out of range is dropped and the engine's default applies.
        level = member.get("troop_level")
        if isinstance(level, int) and 1 <= level <= 11:
            spec["level"] = level
        profile = _profile(member, squads)
        if profile:
            spec["profile"] = profile
        specs.append(spec)

    if missing:
        raise NotEnoughData(
            f"{len(missing)} of {len(members)} players have neither a Total Hero "
            f"Power nor a squad power",
            missing_thp=missing,
        )

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

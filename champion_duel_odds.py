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
- `profile`  what the boxes cannot carry: squad purity, and -- for a player
             the sighting corpus measured and `import_profiles` loaded -- their
             real type ordering, lineup shape and gorilla placement.

`jitter` stays on. Without it the model treats an estimate as a measurement,
which the engine calls the single biggest source of false confidence in the old
workbook. These odds are supposed to include our uncertainty about what people
are actually fielding, not just match variance.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field

try:
    from champion_duel_engine import qualifier, semifinal

    ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - asserted through the degraded path
    ENGINE_AVAILABLE = False

#: Per-round model, how many go through, and how many trials to spend.
#:
#: The two rounds are different models and the package docstring is explicit
#: that they must not reach across to each other. They also cost wildly
#: different amounts, which is why the trial counts are not shared.
#:
#: SEMIFINALS. 800 rather than the engine's 4,000 default. 8 players over 28
#: meetings is cheap per trial but not free, and 4,000 measured about fifteen
#: seconds of pure Python. 800 keeps a two-of-eight question inside a
#: percentage point, finer than the surface renders, at roughly three seconds.
#:
#: QUALIFIERS. 200, which IS the engine's default, because the question needs
#: it: a top-8-of-100 probability sits near 8%, where 50 trials carry about
#: four points of standard error and would render as noise. 100 players over
#: ~36 matches costs about **fourteen seconds**, and pure Python holds the GIL,
#: so the whole bot is unresponsive for that long on every press. That is the
#: known cost of this button and the reason it is worth watching: if qualifier
#: odds get real use, the fix is to run the simulation off the event loop's
#: process, not to cut the trials until the number stops meaning anything.
#: Rounds a model exists for, so a surface can offer the control only where
#: it will work. The knockouts are absent: a single-elimination field of 32 is
#: a different question and nothing models it yet.
STAGES_WITH_A_MODEL = ("qualifiers", "semifinals")

# One run at a time, and remember the last few answers.
#
# A qualifier group is 100 players over ~36 matches each, about fourteen
# seconds of pure Python. Pure Python holds the GIL, so that is fourteen
# seconds in which the bot serves nobody, not just the alliance that pressed
# it. Two cheap things bound that without moving the simulation off this
# process, which is the real fix and deliberately not this change:
#
#   The LOCK stops presses stacking. Without it three people pressing inside a
#   minute cost forty-two seconds of dead bot rather than fourteen.
#
#   The CACHE makes the cost per data change rather than per press. The inputs
#   are a whole recorded group and the model is seeded, so scoring one twice is
#   the same answer arrived at twice. The key is the spec list itself, so any
#   edit to any player misses and re-runs, and nothing has to remember to
#   invalidate it.
_RUN_LOCK = threading.Lock()
_CACHE: dict[str, "GroupOdds"] = {}
_CACHE_MAX = 32


def _models():
    """Bound lazily so the module still imports without the engine."""
    return {
        "qualifiers": {"module": qualifier, "trials": 200},
        "semifinals": {"module": semifinal, "trials": 800},
    }


#: Default for callers that do not name a round. Semifinals, because that is
#: the surface this was built for.
TRIALS = 800

SLOTS = (1, 2, 3)

#: Troop levels the game has. `scoring.troop_value` raises outside this, and
#: only 10 and 11 are measured -- the rest are carried down the same 6 x
#: (level + 1) step, which `scoring.MEASURED_LEVELS` will tell you.
MIN_LEVEL, MAX_LEVEL = 1, 11


@dataclass
class OddsRow:
    """One player's odds, straight off the engine."""

    name: str
    advance: float
    win_group: float
    points_mean: float
    points_sd: float
    #: Share of matches won. The qualifier model reports it; the semifinal one
    #: does not, because that round is scored on points across every match
    #: rather than on matches won, and a win rate there would invite exactly
    #: the misreading the footer exists to prevent.
    win_rate: float | None = None


@dataclass
class GroupOdds:
    rows: list[OddsRow] = field(default_factory=list)
    trials: int = TRIALS
    #: How many of the group go through. Two of eight in the semi-finals,
    #: eight of a hundred in the qualifiers, and the surface has to say which
    #: because the same percentage means a very different thing in each.
    advance: int = 2


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


def _purity(member: dict, imported: dict, powers: list) -> dict:
    """Which of this player's squads are 4-of-a-type, in the frame the engine
    will read them in.

    Two sources answer this and **they are indexed differently**, which is the
    sharpest edge in this whole contract:

    - The `mixed` flag on `squads` is what a MEMBER answered, box by box,
      looking at their own lineup screen. It is indexed by BOX.
    - The `mixed` list on an imported profile is what the sighting corpus
      MEASURED, and `player_profiles` fits it against squad power. It is
      indexed by POWER RANK.

    And the engine reads the same key in whichever of those two frames the rest
    of the input puts it in. With at least one power read, `measured_base`
    returns an `order` and `build_player` translates `mixed` from boxes to
    ranks through it. With none, the THP path runs and the tuple is applied
    directly as ranks. So each source is correct on exactly the path the other
    is wrong on, and sending the wrong one lands a 3.3% penalty on whichever
    squads happened to sort into those positions.

    **The member's answer wins where both are usable.** Kevin's ruling is that
    a member reading their own screen and a scout watching a battle are the
    same kind of evidence, so this is not a ranking of sources -- it is
    recency. The member is describing the lineup they hold now; the corpus
    fitted an ordering from fights that may be a round old, and squads get
    rebuilt between rounds.

    An empty answer -- "we looked and every squad is pure" -- names no
    positions at all, so it is true in both frames and always worth sending.
    Dropping it would make the engine sample a mixed pair from the population
    for a player we were explicitly told has none.
    """
    read = [i for i, power in enumerate(powers) if power]

    # Per squad in the database, because that is what a member answers: they
    # are looking at their lineup screen box by box. The engine wants the set
    # of positions, so it is assembled here.
    flags = {sq["slot"]: sq.get("mixed") for sq in (member.get("squads") or [])}
    answered = [flags.get(slot) for slot in SLOTS]
    # **Every box, not any box.** `mixed` is a set of positions and the engine
    # reads a box's absence from it as "pure", so a half-answered lineup would
    # assert purity about squads nobody was asked about -- collapsing the NULL
    # and 0 the `squads.mixed` column exists to keep apart. There is no way to
    # say "box 2 unknown" in this contract, so a partial answer falls through
    # to the corpus, which measured all three or none.
    if all(v is not None for v in answered):
        boxes = tuple(i for i, v in enumerate(answered) if v)
        # Box positions with nothing read would be applied as power ranks, so
        # that one case falls through to the corpus, which measured this in
        # exactly that frame.
        if read or not boxes:
            return {"mixed": boxes}

    imported_mixed = imported.get("mixed")
    if imported_mixed is not None:
        ranks = tuple(sorted({int(i) for i in imported_mixed}))
        if not ranks or not read:
            return {"mixed": ranks}
        if len(read) == 3:
            # Every box carries a power, so which rank each box holds is
            # readable off what we store -- the same sort `measured_base` is
            # about to do. Translating here is what lets a corpus measurement
            # reach a player whose squads somebody has since typed in.
            order = sorted(read, key=lambda box: -powers[box])
            return {"mixed": tuple(sorted(order[rank] for rank in ranks))}
        # One or two powers. Which rank a box holds is inferred by the engine
        # from the THP fit and is not knowable here, so there is no translation
        # to make and the measurement is dropped. A misplaced penalty is worse
        # than a sampled one -- the same call `build_player` makes internally.
        return {}

    # LEGACY, and only sendable where the engine's own "the bottom n" expansion
    # lands in the frame it is then applied in. That is the THP path. With a
    # power read, the engine would expand the count to power ranks and
    # `build_player` would go on to read those as boxes.
    count = imported.get("n_mixed")
    if count is not None and not read:
        return {"n_mixed": count}
    return {}


def _profile(member: dict, squads: list[dict]) -> dict | None:
    """What we measured about this player that the squad boxes cannot carry.

    Two things feed it: the per-box answers a member gave through the hub, and
    the profile `import_profiles` loaded from the sighting corpus. `_purity`
    owns the one key they both speak to; the rest come from the corpus alone,
    because nothing in the bot measures them.

    `types`, `shape` and `gorilla` are all POWER-RANK facts and need no
    translation, so they go out whenever we hold them. The engine reads each on
    the path it can use one: `types` on both, `shape` and `gorilla` only where
    nothing was read. Sending a key the engine will ignore costs nothing;
    withholding one costs a player their measurement.
    """
    imported = member.get("profile") or {}
    powers = [squad["power"] for squad in squads]

    out = {
        key: imported[key] for key in ("types", "shape", "gorilla") if imported.get(key) is not None
    }
    out.update(_purity(member, imported, powers))
    return out or None


def group_advance_odds(
    members: list[dict],
    *,
    stage: str = "semifinals",
    trials: int | None = None,
    seed: int = 42,
    jitter: bool = True,
) -> GroupOdds:
    """Everyone's chance of getting out of this group, for the round it is in.

    `members` are rows as `get_group_scouting` returns them.

    The two rounds are separate models with separate constants, and the engine
    is explicit that they must not be mixed. `stage` picks one; a round with no
    model raises rather than being scored by the other one.

    Each model refuses a group it cannot schedule rather than absorbing it, and
    the two refuse different things: the semifinals need exactly eight, the
    qualifiers need an even headcount. `NotEnoughData` carries no names in
    either case, which is how the surface tells a size problem from a data one.
    """
    if not ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")

    config = _models().get(stage)
    if config is None:
        raise NotEnoughData(f"there is no model for the {stage} round")
    model = config["module"]
    trials = config["trials"] if trials is None else trials

    # Group size first. A short group usually also has people we hold nothing
    # for, and telling someone to record a squad for two players when they are
    # forty names short points at the smaller job. The models own their own
    # sizes, so this asks them rather than keeping a copy.
    expected = getattr(model, "GROUP_SIZE", None)
    if stage == "semifinals" and len(members) != expected:
        raise NotEnoughData(
            f"the group has {len(members)} players; the semifinal model is "
            f"calibrated on groups of {expected}"
        )
    if stage == "qualifiers" and len(members) != expected:
        # The model itself would take any even count of four or more, but
        # top-8-of-40 is not top-8-of-100: scoring a partial group inflates
        # everyone's chances by however many rivals are missing, and it does it
        # silently, in the units the surface renders. So the whole group or
        # nothing.
        raise NotEnoughData(
            f"the group has {len(members)} players of {expected}; odds over a "
            f"partial group would count only the rivals we happen to hold"
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
        if isinstance(level, int) and MIN_LEVEL <= level <= MAX_LEVEL:
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

    # `display` is part of the key, not just `specs`. The specs are keyed by row
    # POSITION so two members sharing a name stay two players, which means the
    # names are nowhere in them -- and a correction that changes only a spelling
    # would hit a warm entry and hand back the old names against the new group.
    key = json.dumps([stage, trials, seed, jitter, specs, display], sort_keys=True, default=str)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    with _RUN_LOCK:
        # Re-checked inside the lock: several callers can queue on one cold
        # group, and without this each pays a full run in turn to arrive at the
        # answer the first one already has.
        cached = _CACHE.get(key)
        if cached is not None:
            return cached
        scored = model.simulate_group(specs, trials=trials, seed=seed, jitter=jitter)

    rows = [
        OddsRow(
            name=display[key],
            advance=vals["advance"],
            win_group=vals["win_group"],
            points_mean=vals["points_mean"],
            points_sd=vals["points_sd"],
            win_rate=vals.get("win_rate"),
        )
        for key, vals in scored.items()
    ]
    rows.sort(key=lambda r: r.advance, reverse=True)
    result = GroupOdds(rows=rows, trials=trials, advance=getattr(model, "ADVANCE", 2))

    # Oldest out first. Insertion order is enough for a handful of groups and
    # needs no timestamp to go stale.
    if len(_CACHE) >= _CACHE_MAX:
        del _CACHE[next(iter(_CACHE))]
    _CACHE[key] = result
    return result

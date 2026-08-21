"""What one player actually does, and what to field against them.

**The model lives in `champion_duel_engine`, not here.** Like
`champion_duel_odds.py`, this is a join: it turns stored sightings into engine
input, asks the engine the same exact question many times, and shapes the
answers. It prices nothing itself.

WHAT THIS ANSWERS THAT `Find a player` DOES NOT. `Find a player` reads back
what we hold — squads, THP, the most-seen order. This takes the next step and
is a *recommendation*: given what they are seen fielding, which of your six
deployment orders to set, what they can do about it, and how much the choice is
worth at all. That last one is the part with no equivalent anywhere else in the
product, and against an unscouted opponent at near parity it is the whole
product.

THE THREE QUESTIONS, IN THE ORDER THEY MATTER

1. *Is the deployment worth anything here?* Decided by how far apart the two
   players are, not by how much scouting we hold, which is the least intuitive
   thing on the surface. Above roughly a 10% Total Hero Power gap the counter
   triangle stops deciding matches and power decides them; below 5% the
   deployment is very nearly the whole match. `worth()` grades it off the
   measured envelope rather than off the gap, so the sentence can never
   contradict the grid printed under it.
2. *What do they do?* Observed, never modelled — their most-seen order, how
   much of their record it is, and whether they hold it inside a meeting.
   `read_habit()` and `grade_read()`.
3. *What should I set?* The engine, asked once per cell of the deployment grid.
   `intel()`.

THE RECOMMENDATION IS ONLY AS GOOD AS THEIR HABIT, so it is graded rather than
always given, and `none` is a real answer rather than a failure. A counter to a
lineup somebody abandons every match is worse than useless: it is a
confident-looking coin flip. In the semifinal corpus only 6 of 22 opponents
were genuinely counterable, so a surface that always answers would be
confidently wrong about a third of the time.

WHY THE TYPE ASSIGNMENT IS ENUMERATED RATHER THAN TRUSTED

This is the correctness point of the whole module. For a player nobody has
sighted, `push_to_bot` writes three `estimated` squads whose *types* are
literally `("Tank", "Missile", "Aircraft")` in strongest-first slot order —
a placeholder, not a measurement, and the engine's own note says "which of
their squads is the Tank is unknown for almost all of them".

`champion_duel_predict.build_side` forwards that placeholder to the engine as
fact. Two unscouted players therefore meet Tank-on-Tank, Missile-on-Missile,
Aircraft-on-Aircraft in every slot, no counter ever fires, and the exact
probability that comes back is a pure power comparison wearing a prediction's
clothes. Measured over 400 random pairings drawn from the real THP range:

    THP gap     what the single number hides
    0-2%        the truth spans 14% to 80% — a 66-point range
    2-5%        58 points
    5-10%       26 points
    10-20%      1.5 points
    20%+        nothing; power decides and the assignment is irrelevant

The single number is not *biased* — it lands within a point of the mean over
every assignment — it is simply silent about the range. So this module treats a
placeholder type as unknown and enumerates it, which is what turns "41.8%" into
"between 14% and 80%, and here is the order that gets you the top of it".

WHAT IS NOT ENUMERATED, AND WHY

Gorilla placement. The research envelope enumerated it too (6 types x 6 orders
x 3 gorilla = 108 deployments a side, 11,664 pairs), because gorilla placement
changes the *powers* and the research rig derives powers from THP. Doing that
here would mean reimplementing `semifinal.build_player` inside the bot, which
is precisely the rule that has already cost this project two rewrites. What we
store is what a player read off the panel, gorilla already included. So the
bot's envelope is 6 x 6 deployments a side, 1,296 pairs, and it understates the
true spread rather than overstating it — the conservative direction.

THE MATCH IS THE UNIT, NOT THE MEETING. Every probability here is `best_of=1`.
A meeting is three matches and you redeploy between them, so advice at Bo3
would price a decision the player gets to make three times as though they made
it once. `best_of` is passed explicitly for the same reason
`predict_pair` requires it.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import champion_duel_predict as predict_lib

try:
    from champion_duel_engine import engine as _engine

    ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - asserted through the degraded path
    ENGINE_AVAILABLE = False


#: The counter triangle, read off the engine rather than restated.
#:
#: `engine.BEATS` maps a type to the type it beats, so the counter to X is the
#: key whose value is X. The scouting prototype in the simulator hardcoded this
#: inverted map and it is exactly the kind of duplicate that goes stale in one
#: repo and not the other, so it is derived here and never typed out.
def _counters() -> dict[str, str]:
    return {beaten: beater for beater, beaten in _engine.BEATS.items()}


#: The six deployment orders, as index permutations over a player's three
#: squads. Same set the engine's own `ORDERS` holds; kept as a local so this
#: module still imports with no engine and the degraded path stays testable.
ORDERS = tuple(itertools.permutations((0, 1, 2)))

SLOTS = (1, 2, 3)

#: The three squad types, taken from the engine's own map so a fourth type in
#: the game reaches this module by upgrading the pin rather than by editing it.
#: Falls back to the literal only where the engine is absent, which is the
#: degraded path the surface refuses on anyway.
TYPES = tuple(_engine.BEATS) if ENGINE_AVAILABLE else ("Tank", "Missile", "Aircraft")

# ── Editorial thresholds ─────────────────────────────────────────────────────
#
# READ THIS BEFORE MOVING ANY NUMBER BELOW.
#
# These are NOT model constants and they must never become any. Nothing here
# reaches the engine, weights a probability or changes a number: each one
# decides which of three *sentences* a surface prints over counts the bot
# already stores. That is a UX judgement about when evidence is thin, which is
# the bot's to make, and it is the reason this block can live here without
# breaking the "no constants in a join module" rule that
# `champion_duel_odds.py` states and this module inherits.
#
# The read thresholds are carried over unchanged from the simulator prototype
# `semifinal_data/build_scout_page.py`, which set them against the mechanism
# capture — 408 matches over 136 meetings. They are stated in one block so a
# later session can retune them against more sightings without reading the
# whole file.

#: A read is `strong` when they repeat one order at least this often...
STRONG_SHARE = 0.50
#: ...change it inside a meeting no more than this often...
STRONG_CHANGE = 0.34
#: ...and there are at least this many recorded orders behind both figures.
STRONG_SEEN = 6

#: A read is `lean` on a visible favourite with less behind it.
LEAN_SHARE = 0.40
LEAN_SEEN = 4

#: Where the deployment stops being the match, in points of measured envelope
#: spread rather than in power gap. Set where the corpus bands fall: the
#: envelope spans about 66 points under a 2% Total Hero Power gap and 26 under
#: a 10% one, which is where a counter stops overturning power in the recorded
#: rounds. Grading the computed quantity rather than the proxy means the
#: sentence can never contradict the grid printed under it.
WORTH_DECIDES_SPREAD = 0.40
WORTH_SWINGS_SPREAD = 0.10

#: Where *your own* choice stops being worth ranking, in points of spread
#: across your six orders.
#:
#: DISTINCT FROM `WORTH_*`, AND THE DIFFERENCE IS THE POINT. The envelope
#: grades the whole range the match could take, and most of that range is
#: their unknown type assignment, which is nobody's decision. This grades the
#: part the reader actually chooses. The two come apart exactly where the
#: honesty problem is: against an unscouted opponent at parity the envelope is
#: 98 points and the six orders are within 3 of each other, so a surface
#: grading only the envelope prints "the deployment is the match" over a
#: ranking that is a coin flip six ways.
#:
#: Measured on `notes/intel_previews/render_states.py`, which drives the
#: shipping path rather than a reimplementation:
#:
#:     their types recorded, graded read      62 to 95 points
#:     their types recorded, no read          13 points
#:     one of their three types recorded       4 points
#:     none of their types recorded            3 points
#:     power gap decides it                    0 points
#:
#: Set between the last two clusters. It is a judgement about when a ranking is
#: worth printing, not a measurement of anything, and it is the newest and
#: least evidenced number in this block: eleven fixture states are not a
#: corpus. Retune it against sightings when there are some.
CHOICE_SPREAD = 0.10

#: Grades. Strings rather than an enum so a surface can use them as dict keys
#: into its own copy without importing anything from here.
STRONG, LEAN, NONE = "strong", "lean", "none"
WORTH_DECIDES, WORTH_SWINGS, WORTH_SETTLED = "decides", "swings", "settled"


class NotEnoughData(Exception):
    """Neither side can be built into a line-up, so there is nothing to grid.

    Deliberately the same shape as `champion_duel_predict.NotEnoughData` and
    raised for the same reason — a player with a slot missing a type or a power
    cannot be deployed at all — so a surface that already handles one handles
    both.
    """

    def __init__(self, name: str, missing: list[int]):
        super().__init__(f"{name} is missing squad data for slot(s) {missing}")
        self.name = name
        self.missing = missing


# ── What they do ─────────────────────────────────────────────────────────────


@dataclass
class Habit:
    """What this player has been *observed* deploying. Nothing modelled.

    `change_rate` is the one figure that is weaker here than in the simulator
    prototype, and the difference is worth stating rather than papering over.
    The prototype measured a change *between consecutive matches of a meeting*,
    because `mechanism_entered.csv` carries a match number. `order_history`
    does not: it carries an opponent and a date, and nothing says which match
    of the meeting a row came from.

    So this is not the prototype's figure with a wider error bar, it is a
    different quantity: **how often a meeting we watched more than once
    contained a change.** Named that way in the copy rather than dressed as a
    transition rate.

    IT IS NOT A LOWER BOUND ON THE TRANSITION RATE, WHICH AN EARLIER VERSION OF
    THIS DOCSTRING CLAIMED. A meeting is three matches and therefore two
    transitions, and one distinct second order trips the whole meeting, so on a
    fully recorded meeting this runs *at or above* the transition rate rather
    than below it (A,B,B is a rate of 0.5 and scores 1.0 here). The claim was
    also the load-bearing half of a safety argument that does not hold: see
    `meetings_multi`.

    `meetings_multi` is the denominator, and it is the correction. A meeting
    with one recorded line-up cannot show a change however often the player
    changed, so counting it as an unchanged meeting is exactly the
    "we cannot tell" read as "they never change" that `grade_read` refuses
    elsewhere. Six meetings sighted once each used to score `change_rate` 0.0
    and earn a `strong` read saying they hold one line-up, on a record that
    never watched a single change happen. Now they score `None` and grade
    `lean`.
    """

    top: tuple[str, ...]
    seen: int
    total: int
    distinct: int
    meetings: int = 0
    meetings_changed: int = 0
    #: Meetings with more than one recorded line-up, which are the only ones
    #: that could have shown a change. The denominator of `change_rate`.
    meetings_multi: int = 0

    @property
    def share(self) -> float:
        """How much of their record the most-seen order is."""
        return self.seen / self.total if self.total else 0.0

    @property
    def change_rate(self) -> float | None:
        """How often a meeting we watched more than once contained a change.

        None where no meeting has two recorded line-ups, which is the normal
        case on today's corpus. It means "we cannot tell", and a surface must
        not render that as zero.
        """
        if not self.meetings_multi:
            return None
        return self.meetings_changed / self.meetings_multi


def read_habit(player: dict) -> Habit | None:
    """This player's deployment habit, from `order_history` rows.

    Takes the rows `db.get_player(..., include_scouting=True)` attaches rather
    than calling `db.most_common_order`, because that helper answers only the
    first two of the four questions here and a second query for the rest would
    read the same rows twice.

    Repeats are the signal and are kept: somebody seen five times leading
    Missile and once leading Tank reads 5:1, which is exactly the weighting
    `predict_matchup` consumes.
    """
    rows = player.get("orders") or []
    if not rows:
        return None

    counts: dict[tuple[str, ...], int] = {}
    # A meeting is one opponent on one date. Rows with no opponent cannot be
    # grouped into a meeting at all and are counted for the share but not for
    # the change rate, which is why the two have different denominators.
    #
    # Rows as well as distinct orders, because the two answer different
    # questions: distinct orders say whether they changed, and the row count
    # says whether we would have seen it if they had.
    meetings: dict[tuple, dict] = {}
    for row in rows:
        order = (row.get("slot1"), row.get("slot2"), row.get("slot3"))
        if not all(order):
            continue
        counts[order] = counts.get(order, 0) + 1
        opponent = row.get("opponent")
        if opponent:
            meeting = meetings.setdefault(
                (opponent, row.get("observed_at")), {"rows": 0, "orders": set()}
            )
            meeting["rows"] += 1
            meeting["orders"].add(order)

    if not counts:
        return None
    top, seen = max(counts.items(), key=lambda kv: kv[1])
    return Habit(
        top=top,
        seen=seen,
        total=sum(counts.values()),
        distinct=len(counts),
        meetings=len(meetings),
        meetings_multi=sum(1 for rows in meetings.values() if rows["rows"] > 1),
        meetings_changed=sum(1 for rows in meetings.values() if len(rows["orders"]) > 1),
    )


def grade_read(habit: Habit | None) -> str:
    """`strong` / `lean` / `none` — how much their habit is worth countering.

    `none` is an answer, not a failure. It says there is no repeat worth
    countering, which is a useful thing to be told and the opposite of what a
    surface that always produces a recommendation would tell you.
    """
    if habit is None or habit.total < LEAN_SEEN:
        return NONE
    change = habit.change_rate
    if (
        habit.share >= STRONG_SHARE
        and habit.total >= STRONG_SEEN
        # An unmeasurable change rate does not earn `strong`. The prototype had
        # a transition rate for every player it graded; here it is often None,
        # and treating "we cannot tell" as "they never change" is the one way
        # this grading could overclaim.
        and change is not None
        and change <= STRONG_CHANGE
    ):
        return STRONG
    if habit.share >= LEAN_SHARE:
        return LEAN
    return NONE


def worth(spread: float) -> str:
    """Whether the deployment choice is worth anything in this matchup.

    **Graded on the measured spread, not inferred from the power gap.** The
    gap is the mechanism and the spread is the answer, and only one of the two
    is computed from the players actually in front of us. An earlier draft
    graded on the gap and could be talked into printing "power decides this
    one" directly above a grid where it plainly did not — squad powers and
    total hero power are different columns and they can disagree, most obviously
    when a line-up was scouted a round ago.

    The thresholds are set where the corpus bands fall, so the two agree wherever
    the data is coherent. Over 400 pairings drawn from the real THP range, the
    envelope spans 66 points under a 2% gap, 58 under 5%, 26 under 10%, 1.5
    under 20% and nothing at all past that — which is the same finding the
    corpus states the other way round, that below 5% a counter decides matches
    and above 10% one has never won in 39 attempts.
    """
    if spread >= WORTH_DECIDES_SPREAD:
        return WORTH_DECIDES
    if spread >= WORTH_SWINGS_SPREAD:
        return WORTH_SWINGS
    return WORTH_SETTLED


# ── The grid ─────────────────────────────────────────────────────────────────


def _recorded_types(player: dict) -> set[int]:
    """The slot indices whose squad *type* is a sighting rather than a fill-in.

    `push_to_bot` writes `source='estimated'` rows carrying Tank/Missile/
    Aircraft in strongest-first slot order for everybody with no sighting,
    which is ~97% of the roster. The power on those rows is a fit and is
    labelled as one; the type is not a fit at all, it is a placeholder, and it
    is the field this module refuses to take on trust.

    Every other source is a real answer about type. `observed` came off a
    battle report and `edited` came off the player's own line-up screen, and
    Kevin's ruling is that those are the same kind of evidence — somebody read
    a screen — so they rank the same here.

    Returned per slot rather than as a flag because the mixed case is live: a
    member who fills in one box through the hub leaves the other two
    `estimated`, and knowing one type constrains the other two to a pair rather
    than telling us nothing. Read off the raw rows, since `SideInput` counts
    sources without keeping them per slot.
    """
    recorded = set()
    for squad in player.get("squads") or []:
        slot = squad.get("slot")
        if squad.get("source") != "estimated" and squad.get("squad_type") and slot in SLOTS:
            recorded.add(SLOTS.index(slot))
    return recorded


def _natural(side: predict_lib.SideInput) -> list[tuple[float, str]]:
    return [(side.player[f"sq{s}_power"], side.player[f"sq{s}_type"]) for s in SLOTS]


def _assignments(side: predict_lib.SideInput, recorded: set[int]) -> list[tuple[str, ...]]:
    """Which types this player's three squads could be, given what was seen.

    All three recorded is one assignment, none recorded is all six, and one
    recorded is the two that agree with it — knowing the Tank halves the space
    rather than leaving it whole, which is the whole reason a member filling in
    a single box is worth asking for.

    A recorded set that does not describe a one-of-each line-up (about 4% of
    players field two of a type) yields nothing consistent, and the natural
    assignment is used rather than an empty grid.
    """
    natural = [squad_type for _, squad_type in _natural(side)]
    if len(recorded) == len(SLOTS):
        return [tuple(natural)]
    consistent = [
        types
        for types in itertools.permutations(TYPES)
        if all(types[i] == natural[i] for i in recorded)
    ]
    return consistent or [tuple(natural)]


def _deployments(side: predict_lib.SideInput, recorded: set[int]) -> list[list[tuple]]:
    """Every line-up this player could put on the field, as engine orders.

    The six arrangements of their three squads, times however many type
    assignments survive `_assignments` — 6 where the line-up was sighted, 36
    where nothing was, 12 where one box is known.

    Powers stay welded to their slot through the type permutation: what is
    unknown is which type each squad *is*, not what it weighs.
    """
    powers = [power for power, _ in _natural(side)]
    return [
        [(powers[i], types[i]) for i in order]
        for types in _assignments(side, recorded)
        for order in ORDERS
    ]


@dataclass
class Option:
    """One of *your* deployment orders, and what it is worth against theirs.

    Named by the type sequence rather than by squad position, because that is
    the thing a player sets and reads on their own screen: they know which of
    their squads is the Tank, and "field Tank first" is an instruction they can
    follow where "field your second-strongest first" is not.

    That is also why options only exist when *your* types are recorded. With a
    placeholder assignment on your own side every type sequence is reachable by
    relabelling, so the six rows come out identical — a real symmetry, not a
    rounding artefact, and printing it would offer six choices that are one
    choice.
    """

    order: tuple[str, ...]
    worst: float
    mean: float
    best: float


@dataclass
class Envelope:
    """The range every deployment either side could set, and its middle.

    This is the figure with no equivalent anywhere else in the product, and at
    near parity it *is* the product: a single calibrated number cannot carry
    "somewhere between 14% and 80%, depending entirely on what the two of you
    set". `mean` is here to be set against the 1v1 card, which it tracks
    closely — the card is not biased, it is silent.

    **Not a rival probability.** Weighting every configuration equally is the
    wrong prior, and quoting it as a better estimate would be a worse claim
    than the one it criticises: 63% of real deployments are strongest-first,
    and the whole point of the behaviour model is that choices are not uniform.
    What this carries is the range of the possible and the shape of it, which
    is exactly what one calibrated number cannot.
    """

    floor: float
    ceiling: float
    mean: float
    combinations: int

    @property
    def spread(self) -> float:
        return self.ceiling - self.floor


@dataclass
class Intel:
    """The whole answer, in the order a surface should render it."""

    them: predict_lib.SideInput
    you: predict_lib.SideInput | None
    #: Total Hero Power gap as a share of the larger player, and the grade that
    #: follows from it. `None` where either side has no THP on file, in which
    #: case a surface must not grade the worth at all rather than guessing.
    gap: float | None
    worth: str | None
    habit: Habit | None
    read: str
    #: How many of each side's three squad *types* are recorded rather than
    #: filled in. A count and not a flag, because one recorded box is a real
    #: state between the other two: it halves the arrangements that survive,
    #: which is the whole argument for asking a member to fill one in.
    #: Surfaces have to say which of the three states they are in — a reader
    #: who thinks we know their line-up will not expect the spread that comes
    #: back when we do not.
    their_types_recorded: int = 0
    your_types_recorded: int = 0
    #: The type sequence that counters the order they most often show, from the
    #: triangle alone. Present whenever there is a graded read on a player
    #: whose types are recorded, and it needs nothing at all about you — which
    #: is why it survives where `recommended` cannot be computed.
    counter_types: tuple[str, ...] | None = None
    #: Your six orders, best first. Empty where your own types are a placeholder.
    options: list[Option] = field(default_factory=list)
    recommended: Option | None = None
    #: What they can do about it: the order of theirs that costs your
    #: recommended deployment the most, and what it leaves you at. Absent where
    #: their types are a placeholder, because naming one arrangement out of
    #: thirty-six as "their best reply" would dress a guess as a finding.
    their_best_reply: tuple[str, ...] | None = None
    p_if_they_hold: float | None = None
    p_if_they_switch: float | None = None
    envelope: Envelope | None = None

    @property
    def their_types_known(self) -> bool:
        return self.their_types_recorded == len(SLOTS)

    @property
    def your_types_known(self) -> bool:
        return self.your_types_recorded == len(SLOTS)

    @property
    def counterable(self) -> bool:
        return self.read in (STRONG, LEAN)

    @property
    def needs_your_squads(self) -> bool:
        """A recommendation is reachable but for one thing you can record."""
        return self.you is not None and not self.your_types_known

    @property
    def choice_spread(self) -> float | None:
        """How far apart your own six orders are, in probability.

        `None` where there are no options to spread, which is a different
        answer from zero and must not render as one.
        """
        if not self.options:
            return None
        means = [option.mean for option in self.options]
        return max(means) - min(means)

    @property
    def choice_matters(self) -> bool:
        """Whether your six orders are far enough apart to be worth ranking.

        The refusal this drives is the one Kevin asked for: with nothing
        recorded about the opponent the six means come out within a few points
        of each other, and printing the top one as a recommendation sells a
        coin flip as a plan. Graded on the computed spread rather than on
        "did we scout them", for the same reason `worth` is graded on the
        envelope rather than on the power gap: the sentence can then never
        contradict the numbers under it.
        """
        spread = self.choice_spread
        return spread is not None and spread >= CHOICE_SPREAD


def _grid(you: predict_lib.SideInput, them: predict_lib.SideInput, yours, theirs, best_of):
    """P(you win one match) for every cell. Exact, never sampled.

    `predict_matchup` averages over the cross product of the order lists it is
    handed, so one order a side IS one cell, and the whole grid is one call per
    cell at roughly 50 microseconds each — 36 cells where both sides are
    recorded, 1,296 where neither is.
    """
    return [
        [
            _engine.predict_matchup(
                you.player, them.player, orders_a=[mine], orders_b=[theirs_one], best_of=best_of
            )
            for theirs_one in theirs
        ]
        for mine in yours
    ]


def intel(them: dict, you: dict | None = None, *, best_of: int = 1) -> Intel:
    """What they do, what to set against it, and what the choice is worth.

    `them` is required and `you` is not, which is the shape of the question
    people actually ask. With one player you get the observed habit and the
    counter to the order they most often show — the simulator prototype's whole
    output, and it needs nothing about you because the counter triangle does
    not. With both you also get the grid: what your squads make of theirs, what
    they can do about it, and the envelope.

    `best_of` is 1 and should stay 1. The unit of a deployment decision is a
    MATCH: a meeting is three of them and you redeploy in between, so pricing
    the advice at Bo3 would charge a decision to a series the player gets to
    remake twice. It is a parameter only so a caller can be explicit, exactly
    as `predict_pair` requires one.
    """
    if not ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")

    them_side = predict_lib.build_side(them)
    habit = read_habit(them)
    read = grade_read(habit)
    their_recorded = _recorded_types(them)
    their_types_known = len(their_recorded) == len(SLOTS)

    counter = _counters()
    counter_types = None
    if habit and read != NONE and their_types_known:
        counter_types = tuple(counter[t] for t in habit.top)

    if you is None:
        return Intel(
            them=them_side,
            you=None,
            gap=None,
            worth=None,
            habit=habit,
            read=read,
            their_types_recorded=len(their_recorded),
            counter_types=counter_types,
        )

    you_side = predict_lib.build_side(you)
    your_recorded = _recorded_types(you)
    your_types_known = len(your_recorded) == len(SLOTS)

    gap = None
    thp_a, thp_b = you.get("thp"), them.get("thp")
    if thp_a and thp_b:
        gap = abs(float(thp_a) - float(thp_b)) / max(float(thp_a), float(thp_b))

    yours = _deployments(you_side, your_recorded)
    theirs = _deployments(them_side, their_recorded)
    grid = _grid(you_side, them_side, yours, theirs, best_of)

    flat = [p for row in grid for p in row]
    envelope = Envelope(
        floor=min(flat), ceiling=max(flat), mean=sum(flat) / len(flat), combinations=len(flat)
    )

    result = Intel(
        them=them_side,
        you=you_side,
        gap=gap,
        worth=worth(envelope.spread),
        habit=habit,
        read=read,
        their_types_recorded=len(their_recorded),
        your_types_recorded=len(your_recorded),
        counter_types=counter_types,
        envelope=envelope,
    )
    if not your_types_known:
        # Every type sequence is reachable by relabelling your own squads, so
        # the six rows are one row six times. Say nothing rather than offer a
        # choice that is not one; `needs_your_squads` is what a surface asks.
        return result

    # Which of their deployments the advice is priced against. A graded read on
    # a player whose types are recorded narrows thirty-six arrangements to the
    # one they keep showing, and that narrowing is the entire value of having
    # scouted them.
    focus = theirs
    if counter_types is not None:
        powers = {squad_type: power for power, squad_type in _natural(them_side)}
        if all(t in powers for t in habit.top):
            focus = [[(powers[t], t) for t in habit.top]]
    focused = grid if focus is theirs else _grid(you_side, them_side, yours, focus, best_of)

    for mine, row in zip(yours, focused):
        result.options.append(
            Option(
                order=tuple(squad_type for _, squad_type in mine),
                worst=min(row),
                mean=sum(row) / len(row),
                best=max(row),
            )
        )
    # Ranked on the mean, not on the floor, in both branches. A maximin pick
    # would be right against an opponent choosing to hurt you, and they are
    # not: deployments are set blind and simultaneously, so they are no more
    # aiming at you than you are at them. Where the read narrowed their side to
    # one line-up the mean IS that line-up, so one rule serves both.
    result.options.sort(key=lambda o: o.mean, reverse=True)
    result.recommended = result.options[0]

    if their_types_known:
        # What they can do about it, over the six orders they could actually
        # set — not over `focus`, which is one line-up when the read is good.
        # "If they hold, this; if they break habit, that" is the whole risk
        # statement, and it is most worth making exactly when the read is best.
        mine = next(d for d in yours if tuple(t for _, t in d) == result.recommended.order)
        their_orders = _deployments(them_side, set(range(len(SLOTS))))
        row = _grid(you_side, them_side, [mine], their_orders, best_of)[0]
        worst_at = min(range(len(row)), key=row.__getitem__)
        result.their_best_reply = tuple(t for _, t in their_orders[worst_at])
        result.p_if_they_switch = row[worst_at]
        result.p_if_they_hold = result.recommended.mean

    return result

"""Odds of getting through a round: two group joins and one bracket join.

**The model lives in `champion_duel_engine`, not here.** This module is the join
between what the bot stores and the shape the engine wants, and nothing else. It
holds no constants, no Monte Carlo and no ranking rule.

TWO ENTRY POINTS, BECAUSE THERE ARE TWO QUESTIONS.

`group_advance_odds` scores a group of eight — the semi-finals, ranked on
points accumulated across all 21 matches. `bracket_odds`
scores the knockout field of 32, which is not a group at all: a player's path
depends on who else wins, the engine entry point is `simulate_bracket` rather
than `simulate_group`, it returns a tuple rather than a dict, and there are no
points in that round to rank on. Keeping them apart is why neither has to carry
a column the other invented; `STAGES_WITH_A_MODEL` is what a surface asks.

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
import multiprocessing
import os
import threading
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field

try:
    from champion_duel_engine import semifinal

    ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - asserted through the degraded path
    ENGINE_AVAILABLE = False

# **Imported separately, and this is not tidiness.** `knockout` arrived in
# engine 1.12.0 and `semifinal` has been there since 1.0. Putting both on one
# import line makes an older pin raise `ImportError` for the whole statement,
# which the handler above turns into ENGINE_AVAILABLE = False -- so a bot
# running any engine before 1.12.0 would lose the semi-final odds it has always
# had, silently, reported as "the engine is not installed".
#
# That is not hypothetical. The pin has sat behind the newest tag for most of
# this feature's life and moves in its own merge, so the two are routinely out
# of step. A round the engine cannot model is simply absent from
# `STAGES_WITH_A_MODEL`, which is what that tuple is for.
try:
    from champion_duel_engine import knockout

    KNOCKOUT_AVAILABLE = True
except ImportError:  # pragma: no cover - engines before 1.12.0
    KNOCKOUT_AVAILABLE = False

#: Per-round model, how many go through, and how many trials to spend. One
#: round is in here, and the dict shape is kept rather than flattened because
#: a trial count is a property of a model, not of this module.
#:
#: SEMIFINALS. 800 rather than the engine's 4,000 default. 8 players over 28
#: meetings is cheap per trial but not free, and 4,000 measured about fifteen
#: seconds of pure Python. 800 keeps a two-of-eight question inside a
#: percentage point, finer than the surface renders, at roughly three seconds.
#: Rounds a model exists for, so a surface can offer the control only where
#: it will work.
#:
#: The knockouts joined this list on 2026-08-20. The sentence that used to sit
#: here -- "a single-elimination field of 32 is a different question and
#: nothing models it yet" -- was true when it was written and stopped being
#: true when `knockout.py` landed in engine 1.12.0. The first half of it is
#: still right and is why `bracket_odds` is a separate function from
#: `group_advance_odds`: it IS a different question, it just has a model now.
#:
#: The qualifiers left this list on 2026-08-21, and the reason was reachability
#: rather than value. Odds need a hero power or a squad power for every player
#: in the group, a qualifier group is 100, and there is no mechanism by which
#: 100 of them arrive: the bulk import is bot-owner-only and the paste would be
#: a hundred lines. So the control sat in the round people first meet the
#: feature and refused every press. Recording a qualifier group is untouched
#: and stays free; only the model wiring came out.
STAGES_WITH_A_MODEL = ("semifinals",) + (("knockouts",) if KNOCKOUT_AVAILABLE else ())

#: Bracket trials, and the pairwise-matrix trials underneath them. Separate
#: numbers because they cost wildly different amounts: the bracket sampler is
#: free and the matrix is the entire bill. See `bracket_odds` for the measured
#: table behind these two, and for why the matrix moved from 60 to 250 when
#: the surface went from two rungs to five.
#: The sampler moved 20,000 -> 200,000 on 2026-08-24, and it is the cheapest
#: accuracy this feature has ever bought: about five seconds on a sixty-second
#: run, because the sampler is ~1% of the bill and the matrix is the other 99%.
#:
#: Measured effect on run-to-run drift, at MATRIX_TRIALS = 250:
#:
#:     rung        mean drift        worst        rows moving >= 1pp
#:     Top 16    0.97 -> 0.60pp   1.84 -> 1.00pp     10/32 -> 0/32
#:     Top 8     0.77 -> 0.61pp   2.59 -> 2.06pp      9/32 -> 8/32
#:     Top 4     0.65 -> 0.56pp   3.11 -> 3.09pp      8/32 -> 9/32
#:     Champion  0.46 -> 0.45pp   4.27 -> 4.16pp      4/32 -> 5/32
#:
#: IT FIXES TOP 16 OUTRIGHT AND DOES ESSENTIALLY NOTHING BELOW IT, and that is
#: a diagnosis rather than a disappointment. Top 16 is one round of sampling;
#: Champion means winning five in a row, so matrix error compounds through
#: every round and cannot be sampled away. (Top 4 and Champion nudging the
#: wrong way is five-seed noise -- read the mean column.)
#:
#: CORRECTION TO #511, which is load-bearing there. It records the sampler's
#: noise as "~0.35pp at 20,000 trials" and treats it as a floor. Measured in
#: isolation with the matrices held fixed it is 0.72pp mean and 1.56pp worst on
#: Top 16 -- about twice what is recorded -- and it is not a floor at all. It
#: was a five-second dial nobody had tried, because the sampler was filed under
#: "free, therefore not worth touching".
BRACKET_TRIALS = 200_000

#: DO NOT RAISE THIS TO MATCH. The deeper rungs are matrix-bound, matrix noise
#: falls as the square root of trials, and moving them meaningfully is 4x the
#: run. Once a sweeper is paying for runs instead of a member waiting on one,
#: that becomes affordable -- but it is a separate decision with its own
#: measurement, not a rider on the sampler change.
MATRIX_TRIALS = 250

# One run at a time, and remember the last few answers.
#
# The most expensive thing this bot does is a knockout bracket: a minute and a
# bit of pure Python at `MATRIX_TRIALS` 250, measured at 63.5 s and 72.7 s on
# the two machines it has been timed on, up from roughly a quarter of that at
# 60. Pure Python holds the GIL, so that is a minute in which the bot serves
# nobody, not just the alliance that pressed it. A semi-final group is nearer
# three seconds. Both come through here, and two cheap things bound them
# without moving the simulation off this process, which is the real fix and
# deliberately not this change:
#
#   The LOCK stops presses stacking. Without it three people pressing inside
#   the same minute cost three minutes of dead bot rather than one.
#
#   The CACHE makes the cost per data change rather than per press. The inputs
#   are a whole recorded group and the model is seeded, so scoring one twice is
#   the same answer arrived at twice. The key is the spec list itself, so any
#   edit to any player misses and re-runs, and nothing has to remember to
#   invalidate it.
_RUN_LOCK = threading.Lock()

#: Guards the cache dict and nothing else. Deliberately NOT `_RUN_LOCK`: that
#: one is held for the whole simulation -- up to a minute and a half -- so a
#: lookup taking it would put every warm press in the queue behind a cold run
#: it does not need. This is held for the length of a dict operation.
_CACHE_LOCK = threading.Lock()

#: Least-recently-USED, and it used to be first-in-first-out. The difference is
#: not academic: one grouping is 16 semifinal groups plus one bracket, so 17
#: entries, and TWO LIVE GROUPINGS OVERFLOWED A CAP OF 32. Every overflow
#: evicted in insertion order, which is reliably the entry about to be needed
#: again, and each one turned a warm press back into a cold run.
#:
#: 144 is eight groupings with slack. A whole grouping's worth of entries
#: measured 117 KB on 2026-08-24, so eight is under 1 MB against a ~474 MB
#: baseline -- which is why the fix here is "raise it" rather than "tune it".
_CACHE: "OrderedDict[str, GroupOdds | BracketOdds]" = OrderedDict()
_CACHE_MAX = 144


def _cache_get(key: str):
    """Read an entry and mark it most-recently-used."""
    with _CACHE_LOCK:
        try:
            value = _CACHE[key]
        except KeyError:
            return None
        _CACHE.move_to_end(key)
        return value


def _cache_put(key: str, value) -> None:
    """Store an entry, evicting least-recently-used until the cap holds."""
    with _CACHE_LOCK:
        _CACHE[key] = value
        _CACHE.move_to_end(key)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)


#: Whether the simulation runs in a throwaway subprocess. On by default; set
#: CHAMPION_DUEL_ODDS_SUBPROCESS=0 to force every run back onto the calling
#: thread, which is also what the fallback below does on its own when a pool
#: cannot start.
USE_SUBPROCESS = os.environ.get("CHAMPION_DUEL_ODDS_SUBPROCESS", "1") != "0"

#: How long to wait on a child before abandoning it. See `_run_off_process`:
#: this is a wedge-breaker, not a performance bound, and it sits far above any
#: real run and at the point past which nobody is still waiting for an answer.
JOB_TIMEOUT_SECONDS = 900


def _simulate_group_job(stage: str, specs: list[dict], trials: int, seed: int, jitter: bool):
    """The group simulation, as the child runs it. Module level so it pickles.

    Returns the engine's own dict, keyed by SPEC NAME -- which is a row
    position, not a display name. The mapping back to people stays in the
    parent, because the child is never told who anybody is.
    """
    return _models()[stage]["module"].simulate_group(specs, trials=trials, seed=seed, jitter=jitter)


def _simulate_bracket_job(
    specs: list[dict], trials: int, seed: int, matrix_trials: int, jitter: bool
):
    """The bracket simulation, as the child runs it.

    `simulate_bracket` hands back the two 32x32 meeting matrices alongside the
    result. They are dropped HERE rather than in the parent: otherwise they get
    pickled back through a pipe on every run, and nothing has ever consumed
    them.
    """
    scored, _matrices = knockout.simulate_bracket(
        specs, trials=trials, seed=seed, matrix_trials=matrix_trials, jitter=jitter
    )
    return scored


def _run_off_process(fn, *args):
    """Run `fn(*args)` in a throwaway subprocess, or on this thread if it cannot.

    THE ENTIRE POINT IS THE GIL, and the mechanism is worth being exact about.
    The engine is stdlib-only by design, so a run is pure Python and holds the
    GIL for its whole duration -- 60 to 90 seconds for a bracket. The event loop
    survives that, because every caller reaches this through
    `asyncio.to_thread`. Everything else behind that thread boundary does not: a
    database read measured 1.1 ms idle against ~1,500 ms during a run, and all
    274 of the bot's database reads live there. IN EVERY GUILD, because one
    process serves all of them.

    Moving the work to a child fixes that by arithmetic rather than by tuning.
    The calling thread blocks on a pipe, and blocking on a pipe releases the
    GIL. That is the whole trick.

    SPAWN, NOT FORK. This process has threads, sockets and an open SQLite
    handle, and a forked child inherits every one of them.

    ONE JOB PER PROCESS, TORN DOWN AFTER IT. #509's finding that memory is 83%
    of the hosting bill and CPU is 1% still holds, so a resident worker is the
    wrong trade. Per-job spawning is cheap enough to obey that rule.

    THE CHILD RE-IMPORTS THE MAIN MODULE, which under Railway is `bot.py`, so a
    child costs 52 MB and 0.9 s rather than the 17 MB and 0.09 s this module
    would cost alone. That is taken deliberately: it is transient, one child at
    a time, against a ~474 MB baseline, and about $0.00002 per run. Verified
    safe in the way that matters -- importing `bot.py` opens no SQLite
    connections, and `bot.run()` is guarded by `if __name__ == "__main__"`, which
    a spawned child does not satisfy (it imports the parent's main module as
    `__mp_main__`). It does print two [INFO] lines per child, which show up in
    the logs once per run.

    IT IS NOT, HOWEVER, THREAD-FREE, and an earlier version of this comment said
    it was. `sentry_sdk.init` runs at `bot.py` module scope whenever SENTRY_DSN
    is set, so a production child builds a Sentry client with a transport thread
    and an atexit flush before it does any work. Harmless in practice -- the job
    is pure engine code, and an exception in it is pickled back to the parent and
    raised there rather than captured in the child -- but it is real startup cost
    per run, and it is the kind of claim that gets built on. Guarding that init
    the same way `bot.run()` is guarded would remove it; that is a change to the
    whole bot's error reporting and belongs to its own decision, not to this
    one.
    Buying the leaner child back means a worker module and a JSON protocol over
    stdin/stdout instead of `ProcessPoolExecutor` -- real extra code for
    something that is not costing anything. Revisit if the log noise or the
    memory ever becomes a problem.

    A HUNG CHILD IS NOT A DEAD ONE. `BrokenProcessPool` covers a child that
    died; a child that simply never finishes raises nothing, and this is called
    with `_RUN_LOCK` held -- so without a timeout one wedged process would stop
    every odds press and every sweep in the bot, permanently and silently. The
    in-thread path this replaced could not fail that way, so the timeout arrives
    with the subprocess rather than being a general precaution. It is set far
    above any real run (a bracket measured ~50 s, and the plan's worst case is
    90 s) and below the point where an answer could still be delivered: Discord
    drops an interaction token at 15 minutes, so a run still going at that point
    has nobody left to hand it to.

    WHAT FALLS BACK AND WHAT DOES NOT. A pool that cannot start, or a child that
    dies, degrades to running on this thread: slow for everyone, which is what
    the bot did before this change, rather than broken. An exception raised by
    `fn` ITSELF is not a pool failure and propagates untouched -- retrying it
    here would run the expensive thing twice to arrive at the same exception.
    """
    if not USE_SUBPROCESS:
        return fn(*args)

    timed_out = False
    try:
        executor = ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        )
    except Exception as exc:  # noqa: BLE001 - degrade rather than fail the press
        print(f"[CHAMPION_DUEL] odds subprocess unavailable, running in-thread: {exc}")
        return fn(*args)

    try:
        try:
            future = executor.submit(fn, *args)
        except Exception as exc:  # noqa: BLE001 - same degradation, at submit
            print(f"[CHAMPION_DUEL] odds subprocess submit failed, running in-thread: {exc}")
            return fn(*args)
        try:
            return future.result(timeout=JOB_TIMEOUT_SECONDS)
        except BrokenProcessPool as exc:
            print(f"[CHAMPION_DUEL] odds subprocess died, running in-thread: {exc}")
            return fn(*args)
        except TimeoutError:
            # Deliberately NOT a fallback. We have already spent the timeout;
            # spending another full run on this thread would hold the lock for
            # longer still, and whatever is wrong with the child is not fixed by
            # asking the parent to do the same work.
            timed_out = True
            print(
                f"[CHAMPION_DUEL] odds subprocess still running after "
                f"{JOB_TIMEOUT_SECONDS}s; abandoning it"
            )
            raise
    finally:
        # `wait=True` on a hung child would block here forever, which is the
        # exact wedge the timeout exists to prevent. Let it go instead: the
        # process is abandoned rather than reaped, and the next run gets a
        # fresh one.
        executor.shutdown(wait=not timed_out, cancel_futures=True)


def _models():
    """The GROUP models, bound lazily so the module still imports with no engine.

    **The knockouts are deliberately not in here**, and the first version of
    this change put them in. `group_advance_odds` looks a stage up in this dict
    and scores anything it finds, so registering the bracket here made it
    accept a round with no `simulate_group`, no points and no group -- reaching
    an `AttributeError` several steps later instead of the refusal that used to
    be immediate. Membership of this dict IS the guarantee, so the guarantee is
    kept by leaving it out rather than by a comment saying not to.

    `STAGES_WITH_A_MODEL` is the union and is what a surface asks.
    """
    return {
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
    #: The engine's own key for this row, which is a ROW POSITION rather than a
    #: name -- the specs are keyed by position so two members sharing a display
    #: name stay two players. Carried out so a caller storing these can map them
    #: back to registrant ids and re-render under whatever the names are later.
    #: None on anything built before this was added.
    key: str | None = None


@dataclass
class GroupOdds:
    rows: list[OddsRow] = field(default_factory=list)
    trials: int = TRIALS
    #: How many of the group go through: two of eight. Still read off the
    #: model rather than fixed here, and still rendered by the surface rather
    #: than assumed by it, because a top-2 percentage and a top-8 percentage
    #: are different claims wearing the same units.
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


def _specs(members: list[dict]) -> tuple[list[dict], dict, list[str]]:
    """Every member as one engine spec, plus the names to map back through.

    Lifted out of `group_advance_odds` when the knockouts arrived, because both
    joins have to build a spec exactly the same way and the one thing that must
    not drift between two rounds' surfaces is what the bot decided to tell the
    engine about a player. The refusals differ per round and stay with their
    own function; this is only the shaping.
    """
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

    return specs, display, missing


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

    Every round is its own model with its own constants, and the engine is
    explicit that they must not be mixed. `stage` picks one; a round with no
    model raises rather than being scored by another round's.

    The model refuses a group it cannot schedule rather than absorbing it: the
    semifinals need exactly eight. `NotEnoughData` carries no names in that
    case, which is how the surface tells a size problem from a data one.
    """
    if not ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")

    config = _models().get(stage)
    if config is None:
        # The knockouts get their own sentence. Since engine 1.12.0 they DO
        # have a model, so "there is no model for the knockouts round" became
        # false while staying the thing this function had to say -- and a
        # refusal that gives a wrong reason is how a caller ends up building
        # the wrong fix. They are refused here because they are not a group,
        # not because nothing can score them.
        if stage == "knockouts":
            raise NotEnoughData(
                "the knockouts are a bracket rather than a group, so they are "
                "scored by bracket_odds; there is no group model for them"
            )
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
    specs, display, missing = _specs(members)

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
    cached = _cache_get(key)
    if cached is not None:
        return cached

    with _RUN_LOCK:
        # Re-checked inside the lock: several callers can queue on one cold
        # group, and without this each pays a full run in turn to arrive at the
        # answer the first one already has.
        cached = _cache_get(key)
        if cached is not None:
            return cached
        # Off this process. The lock still admits one run at a time; what
        # changed is that the run no longer holds the GIL of the process
        # serving every other guild. See `_run_off_process`.
        scored = _run_off_process(_simulate_group_job, stage, specs, trials, seed, jitter)

    rows = [
        OddsRow(
            name=display[key_],
            advance=vals["advance"],
            win_group=vals["win_group"],
            points_mean=vals["points_mean"],
            points_sd=vals["points_sd"],
            key=key_,
        )
        for key_, vals in scored.items()
    ]
    rows.sort(key=lambda r: r.advance, reverse=True)
    result = GroupOdds(rows=rows, trials=trials, advance=getattr(model, "ADVANCE", 2))

    _cache_put(key, result)
    return result


@dataclass
class BracketRow:
    """One player's knockout odds. A different shape from `OddsRow`, on purpose.

    A group round is scored on points and ranks eight players against each
    other, so one number describes a player and `OddsRow` carries it. A bracket
    does not work that way: a player's path depends on who else wins, so what
    exists is a chance of reaching each round and nothing that behaves like a
    points total. Forcing this into `OddsRow` would mean inventing a
    `points_mean` for a round that has no points.
    """

    name: str
    #: Keyed by `knockout.ROUND_NAMES` — last32, last16, last8, last4, final,
    #: champion — plus `third` and `podium`. Every round is carried rather than
    #: the one a surface currently renders, because which of them "advancing"
    #: means in a bracket is an open product question and this join must not
    #: settle it by only computing one.
    reach: dict[str, float]
    #: The engine's own key for this row, which is a ROW POSITION rather than a
    #: name -- the specs are keyed by position so two members sharing a display
    #: name stay two players. Carried out so a caller storing these can map them
    #: back to registrant ids and re-render under whatever the names are later.
    #: None on anything built before this was added.
    key: str | None = None

    @property
    def champion(self) -> float:
        return self.reach.get("champion", 0.0)

    @property
    def podium(self) -> float:
        return self.reach.get("podium", 0.0)


@dataclass
class BracketOdds:
    rows: list[BracketRow] = field(default_factory=list)
    trials: int = BRACKET_TRIALS
    matrix_trials: int = MATRIX_TRIALS
    #: The draw was not supplied, so every trial reshuffled it. That answers
    #: "how does this player do across the draws that could happen" rather than
    #: "how do they do in the draw they got", and a surface has to say which —
    #: they are different claims and only one of them is available before the
    #: bracket is published.
    draw_known: bool = False


def bracket_odds(
    members: list[dict],
    *,
    trials: int | None = None,
    matrix_trials: int | None = None,
    seed: int = 42,
    jitter: bool = True,
) -> BracketOdds:
    """How far each of the 32 gets, over a reshuffled draw.

    The second join, and it is a second one rather than a branch inside
    `group_advance_odds` because almost nothing carries over. The engine entry
    point is `simulate_bracket` rather than `simulate_group`; it returns a
    tuple rather than a dict; the field is 32 rather than 8 or 100; there are
    no points to rank on; and the expensive part is a pairwise matrix that the
    group models do not have at all.

    WHAT IT COSTS, AND WHY THE DEFAULT IS WHAT IT IS. The bracket simulation
    itself is free — 20,000 trials over the matrices measured 0.44 seconds. All
    of the cost is `meeting_matrix`, which is 496 pairs simulated twice, once
    at Bo3 and once at Bo5, because a player's path runs through both series
    lengths. Measured 2026-08-20:

        matrix trials   both matrices   error against a 600-trial reference
                 60         13.0 s      last-16 mean 0.89pp, worst 3.50pp
                100         21.9 s      mean 0.79pp, worst 2.59pp
                150         39.0 s      mean 0.66pp, worst 2.22pp
                250         63.5 s      mean 0.56pp, worst 1.51pp
                600        133.0 s      (the reference)

    Re-measured on 2026-08-23 on a different machine, over the same 32-field:
    19.5 s at 60 and 72.7 s at 250. The table above is kept as it was rather
    than overwritten with those, because the errors in its right-hand column
    were measured on that machine too and only the timings would be moving.
    Read it for the shape — the cost is superlinear in trials and the error is
    not — and take a minute and a bit as the figure for the run itself.

    250 IS BOUGHT BY THE SMALL RUNGS, NOT BY THE ACCURACY IN THE ABSTRACT. The
    error falls far more slowly than the cost, and most of what was left at 250
    used to be the bracket sampler's own noise rather than the matrix's. On the two-rung table that
    argument carried and 60 was the default. The bracket surface now prints
    Top 4 and Champion, which sit in low single digits for most of a thirty-two
    field, and 60's worst-case 3.50pp is larger than the figure it would sit
    under. 250 takes that to 1.51pp. Signed off 2026-08-22.

    THAT SENTENCE WAS TRUE AT 20,000 SAMPLER TRIALS AND IS NOT ANY MORE.
    `BRACKET_TRIALS` moved to 200,000 on 2026-08-24 for about five seconds, and
    the "0.35pp floor" it rested on -- taken from #511 -- was itself wrong:
    measured in isolation the sampler contributed 0.72pp mean and 1.56pp worst
    on Top 16, and it was a dial rather than a floor. See the table on
    `BRACKET_TRIALS` for what turning it up bought and, more importantly, for
    what it did not: the deeper rungs are matrix-bound and barely moved, so the
    argument for 250 stands on exactly the rungs it always did.

    THE TABLE LOST A RUNG AFTER THAT AND THIS DID NOT MOVE WITH IT. 250 was
    signed off alongside five rungs; `podium` came off the table on 2026-08-23
    and Top 3 was one of the small figures the number was bought for. Whether
    four rungs need less is a real question and it is being costed separately
    — deliberately not answered here by dropping the constant to whatever
    happens to look sufficient, which would be retuning a calibrated number off
    a change that was about width rather than about error.

    **This is now the most expensive thing the bot does, by a wide margin and
    with nothing above it to shelter under.** 60 used to be chosen to sit
    inside the qualifier run's fourteen seconds, which was the existing worst
    case; qualifier odds came out on 2026-08-21 and took that budget with them,
    so there is no longer a ceiling to fit under and this run sets it. At about
    seventy seconds cold, and pure Python holding the GIL throughout, that is
    over a minute of one core inside a single-process bot.

    WHAT THE LOCK AND THE CACHE BUY IS FREQUENCY, NOT DURATION, and it is worth
    being exact about that because the note this replaced was not. `_RUN_LOCK`
    admits one run and `_CACHE` answers every press until the field changes, so
    a room full of people pressing ONE field costs one run rather than twenty
    -- that is what the re-check inside the lock is for, here and at
    `group_advance_odds`. Neither shortens the run itself, and it is the
    duration that the rest of the process has to survive.

    The event loop survives it. Every caller reaches this through
    `asyncio.to_thread` (`champion_duel_hub._on_odds`), so the loop is slowed
    by GIL contention rather than blocked, and discord.py's gateway heartbeat
    -- 60 s of silence before it force-closes, the library default, which
    `bot.py` does not override -- keeps being answered on 5 ms switch
    intervals. That was a 5x margin at 60 trials and is not one now, so it is
    written down rather than left to be rediscovered: a caller that ever runs
    this ON the loop disconnects the bot.

    WHAT IS NOT COVERED BY ANY OF THAT IS THE QUEUE BEHIND THE LOCK, and this
    is the part the four-rung change put a number on. `_RUN_LOCK` is one lock
    for the whole process and both models take it, so DISTINCT cold fields do
    not share a run: they serialize. Two alliances pressing on two different
    knockout fields is two minutes for the second one, and a semi-final group
    -- three seconds of work -- waits behind a bracket for the whole minute.
    Each waiting press also holds a thread of the default `to_thread`
    executor, so the queue is not free to the rest of the bot either. Far
    enough down a queue and `followup.send` outlives Discord's 15-minute
    interaction token, which is a spinner that never resolves rather than an
    error anybody can act on.

    Nothing here is a reason to move the constant, which is signed off, and
    four rungs are being costed on their own. It is written down because the
    argument for 250 was made against the event loop alone, and this is the
    limit that binds first. What would settle it rather than reason about it
    is the deferred fix, moving the simulation off this process (#511).

    The noise that buys is worth stating rather than burying, because it is
    still larger than the group models'. The table above is what was measured:
    at 250 a last-16 figure sits 0.56pp from the reference on average and
    1.51pp from it at worst. A surface rendering these to the nearest percent
    is still rendering a little finer than the number is measured, and the
    rungs the table added past the placeholder two are the small ones, where a
    point and a half is proportionally the most.
    """
    if not ENGINE_AVAILABLE or not KNOCKOUT_AVAILABLE:
        raise RuntimeError(
            "the installed champion-duel-engine has no knockout model; it arrived in 1.12.0"
        )

    trials = BRACKET_TRIALS if trials is None else trials
    matrix_trials = MATRIX_TRIALS if matrix_trials is None else matrix_trials

    expected = knockout.BRACKET_SIZE
    if len(members) != expected:
        raise NotEnoughData(
            f"the knockouts are a field of {expected} and we hold "
            f"{len(members)}; odds over a partial bracket would count only the "
            f"rivals we happen to have"
        )

    specs, display, missing = _specs(members)
    if missing:
        raise NotEnoughData(
            f"{len(missing)} of {len(members)} players have neither a Total Hero "
            f"Power nor a squad power",
            missing_thp=missing,
        )

    key = json.dumps(
        ["knockouts", trials, matrix_trials, seed, jitter, specs, display],
        sort_keys=True,
        default=str,
    )
    cached = _cache_get(key)
    if cached is not None:
        return cached

    with _RUN_LOCK:
        cached = _cache_get(key)
        if cached is not None:
            return cached
        # Off this process, and the matrices never cross back. `simulate_bracket`
        # returns them so a caller can reuse them; nothing here does, because the
        # cache already makes the cost per data change rather than per press, and
        # holding two 32x32 matrices per cached entry would grow this cache by
        # more than the answers in it. `_simulate_bracket_job` drops them in the
        # child, which now also keeps them off the pipe.
        scored = _run_off_process(_simulate_bracket_job, specs, trials, seed, matrix_trials, jitter)

    rows = [
        BracketRow(name=display[key_], reach=dict(reach), key=key_)
        for key_, reach in scored.items()
    ]
    # Sorted on the title first, because it is the one thing every player in a
    # bracket is unambiguously playing for -- any other lead key would be this
    # join answering "which round does advancing mean", which is a product
    # question and not its to settle.
    #
    # Then out through the earlier rounds as tie-breaks, and that cascade is
    # not decoration. Most of a 32-field has a title chance that rounds to
    # zero, so on the title alone the bottom two thirds of the table come back
    # in whatever order the engine's dict happened to be in -- a 358M player
    # below a 278M one, which reads as a broken table rather than as a tie.
    rows.sort(
        key=lambda row: tuple(
            row.reach.get(name, 0.0) for name in ("champion", "final", "last4", "last8", "last16")
        ),
        reverse=True,
    )
    result = BracketOdds(rows=rows, trials=trials, matrix_trials=matrix_trials, draw_known=False)

    _cache_put(key, result)
    return result

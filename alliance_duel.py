"""Alliance Duel (VS) — Discord-free data layer and pure algorithms (#400).

The core of the ``/vs`` tracker: the fixed game constant tables, the row
dataclasses, sheet read/upsert, league/week/day resolution, and the two
pairing functions. It imports nothing from ``discord`` so it stays trivially
unit-testable, following ``transfer.py``'s Discord-free-core precedent.

The Discord-facing wiring (the ``/vs`` hub, its views and modals, the
scheduled posts) lives in ``alliance_duel_cog.py`` / ``alliance_duel_hub.py``
/ ``alliance_duel_ui.py`` and calls into here.

Design ground rules (see ``notes/DESIGN_alliance_duel_vs.md`` — that doc is
ground truth for this feature):

- **One row per alliance per league-week.** Sixteen alliances times four
  weeks is 64 rows per league, and everything the feature stores lives on
  that grain, which is why it collapses to a single tab.
- **League identity is game-supplied** — season (``S35``), tier (Diamond /
  Gold / Silver, ordered) and group (``12 - 2``). Not date-derived. Many
  brackets run in parallel per season, and promotion/relegation moves
  alliances between tiers, so tier travels with every record.
- **Seed is per league, not per alliance.** It is constant across that
  league's four weeks and is only the tie-break anchor. What reshuffles
  weekly is the weighted score ranking.
- **Header-name column addressing**, reusing ``transfer.py``'s helpers.
  Users reorder and insert columns.
- **Never clobber human edits.** :func:`plan_upsert` locates a row by
  :class:`RowKey` and touches only that row's own cells, appending when
  absent. Same rule member sync learned in 1.4.2 (#262).
- **Weekday resolves on server time, never guild-local.** Every "which day
  is it" decision goes through :func:`server_today` /
  ``config.server_date_for``. A guild in UTC+10 sees Monday locally while it
  is still Sunday on server time, which would misfile a whole day of scores.
  CLAUDE.md flags this as a bug class that has already recurred three times
  (#330 / #318).
- **Two tracking modes** (#448). An alliance may track only its own rows
  rather than the full 16-alliance bracket, and that is a supported shape,
  not incomplete data. The two pairing functions genuinely need the bracket,
  so they report :class:`BracketIncomplete` rather than raising or inventing
  a pairing — callers turn that into an upsell, not an error.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Mapping, Sequence

import transfer
from storm_date_helpers import parse_event_date

logger = logging.getLogger(__name__)


# ── Fixed game constants ──────────────────────────────────────────────────────
#
# Day themes and the Monday-to-Saturday schedule are fixed always, so they live
# as a module-level table like the canonical DS/CS zone names, not as guild
# config. Spot-check them each season in case an update shifts them.
#
# `points` below is **league points** — the 1/2/2/2/2/4 a day contributes to
# the week's 13. Those are structural and identical for every player.
#
# Do not add a table of per-action *award* points here (what the board pays for
# a speedup minute, a kill, a trade truck). The in-game board shows each player
# their own **already-boosted** figures, scaled by a per-player Alliance Duel
# tech tree — a maxed account reads roughly 3x what an un-teched one reads on
# the same board on the same day. So there is no base value to hardcode and no
# universal number to print; a figure copied off one screenshot is wrong for
# nearly everyone else. `decided_by` is prose for exactly that reason. Day
# boards also differ per theme. See the design doc's "What the board actually
# shows".


@dataclass(frozen=True)
class DuelDay:
    """One of the six scoring days of a duel week."""

    number: int  # 1-6
    weekday: int  # Python weekday(): Monday=0 … Sunday=6
    points: int  # league points the day is worth
    theme: str  # in-game day name
    decided_by: str  # what actually decides it, for the member reminder


DUEL_DAYS: tuple[DuelDay, ...] = (
    DuelDay(1, 0, 1, "Radar Training", "Radar tasks and stamina, per-member capped"),
    DuelDay(2, 1, 2, "Base Expansion", "Construction speedup stockpiles"),
    DuelDay(3, 2, 2, "Age of Science", "Research speedup stockpiles"),
    DuelDay(4, 3, 2, "Train Heroes", "Hero / weapon / awakening shard stockpiles"),
    DuelDay(5, 4, 2, "Total Mobilization", "Training speedups and troop volume"),
    DuelDay(6, 5, 4, "Enemy Buster", "Combat"),
)

DUEL_DAY_BY_NUMBER: dict[int, DuelDay] = {d.number: d for d in DUEL_DAYS}
DUEL_DAY_BY_WEEKDAY: dict[int, DuelDay] = {d.weekday: d for d in DUEL_DAYS}

#: What actually scores on each day's board, biggest first (#406).
#:
#: **Names only, never award values, and this is a hard rule rather than a
#: style preference.** The in-game board renders each player their *own*
#: already-boosted figures, scaled by a per-player Alliance Duel tech tree that
#: runs to +150% on all non-purchased points plus +50% per category. There is
#: no base value visible anywhere and no shared number that would be true for
#: two members of the same alliance, so any figure printed here is wrong for
#: nearly everyone reading it. See the design doc, "Observed board values".
#:
#: What survives that is **order**, because every category caps at the same
#: boost, so the ratios between actions hold at any level of Tech. The lists
#: are therefore ordered by observed weight and the copy may lead with the
#: first entry, which is why "spend your research speedups" is safe to say
#: where "research speedups are 150 a minute" is not.
#:
#: Captured 2026-08-08 off a fully-researched account, one board per day. Each
#: day shows a **different** action set rather than one list re-priced, so no
#: day's list may be generalised to another: Radar Tasks score on days 1, 3 and
#: 5 rather than only on Radar Training, and day 6 carries day 2's two
#: big-ticket actions on top of combat.
DAY_ACTIONS: dict[int, tuple[str, ...]] = {
    1: (
        "Radar Tasks",
        "UR hero shards",
        "Drone Parts",
        "chip chests with premium chip material",
        "stamina",
        "gathering food, iron and coins",
    ),
    2: (
        "UR Trade Trucks",
        "UR Secret Tasks",
        "Armament Cores",
        "survivor recruitment",
        "construction speedups",
        "building power",
    ),
    3: (
        "Drone Component Chests, the higher the level the better",
        "Radar Tasks",
        "Valor Badges",
        "research speedups",
        "tech power",
    ),
    4: (
        "UR hero shards",
        "exclusive weapon shards",
        "awakening shards",
        "SSR hero shards",
        "hero recruitment",
        "skill medals",
    ),
    5: (
        "Overlord Bond Badges",
        "Radar Tasks",
        "Overlord Promotion Shards",
        "Overlord Training Certificates and Guidebooks",
        "construction, research and training speedups",
        "training units, the higher the level the better",
    ),
    6: (
        "killing rival alliance units, which score most per kill",
        "UR Trade Trucks",
        "UR Secret Tasks",
        "every kind of speedup, including healing",
    ),
}

#: True on every board, worth saying once rather than repeating per day. The
#: one category the Tech boost explicitly excludes ("All Points
#: (non-purchased)"), so it is also the one thing that does not grow with an
#: account. No value here either, for the same reason as above.
SCORES_EVERY_DAY = "Diamond purchases score on every day."

#: Day 6 only, and the one thing on that board members get wrong. Losing units
#: scores too, so a fight that goes badly is not a wasted fight.
ENEMY_BUSTER_NOTE = "Units you lose score as well as units you kill."

#: Total league points in a week, and the majority needed to take it.
WEEK_POINTS_TOTAL = 13
WEEK_POINTS_MAJORITY = 7

#: Points available across the five grind days (1-5) and on Enemy Buster.
GRIND_POINTS_TOTAL = sum(d.points for d in DUEL_DAYS if d.number <= 5)  # 9
ENEMY_BUSTER_POINTS = DUEL_DAY_BY_NUMBER[6].points  # 4

#: A league is four weeks of sixteen alliances.
LEAGUE_WEEKS = 4
BRACKET_SIZE = 16

#: Pairing weights by week — week 1 result is worth 8, week 2 is 4, and so on.
#: Each week's weight exceeds the sum of every later weight (8 > 4+2+1), which
#: is what makes the week-1 winner/loser cohort split permanent and lets
#: :func:`project_own_path` walk the bracket lineage directly.
WEEK_WEIGHTS: tuple[int, ...] = (8, 4, 2, 1)

#: Tiers in ascending competitive order. Promotion and relegation are real, so
#: tier is not decoration: it qualifies every historical record.
TIER_ORDER: tuple[str, ...] = ("Silver", "Gold", "Diamond")

#: Ordered vocabulary for the human "Known" read, weakest first. Used to
#: compare two alliances when neither has a confirmed result or an explicit
#: Picked call. Values the user types that aren't in here simply don't rank,
#: which falls through to the computed estimate rather than guessing.
KNOWN_SCALE: tuple[str, ...] = ("very weak", "weak", "average", "strong", "very strong")

_KNOWN_ALIASES: dict[str, str] = {
    "vw": "very weak",
    "w": "weak",
    "avg": "average",
    "even": "average",
    "mid": "average",
    "s": "strong",
    "vs": "very strong",
    "very-weak": "very weak",
    "very-strong": "very strong",
}

#: Shown wherever a value has not been entered. Most cells on this tab are
#: legitimately blank (nobody re-scouts fifteen alliances weekly), so one glyph
#: shared by every VS surface makes a half-filled bracket read as a shape
#: rather than as noise. A zero would be a lie and an empty string would look
#: like a rendering bug.
#:
#: A question mark rather than a dash, for two reasons: it says *unknown* where
#: a dash could be read as *none*, and an em dash in user-facing copy is
#: against the house style everywhere else on these surfaces.
#:
#: Lives here rather than in `alliance_duel_setup` (#408) so the analytics
#: layer can render a missing value without importing a module that imports
#: discord. `alliance_duel_setup.NOT_ENTERED` re-exports it, so every existing
#: call site is unchanged.
NOT_ENTERED = "?"

#: Values accepted in the Intent column. Held out of / partitioned in the
#: backtest — see the design doc's four-case table.
INTENT_PUSH = "push"
INTENT_SAVE = "save"
INTENT_NONE = "none"
INTENTS: tuple[str, ...] = (INTENT_PUSH, INTENT_SAVE, INTENT_NONE)

#: Tracking modes (#448). Asked at setup, stored on ``guild_vs_config``.
MODE_OWN_ALLIANCE = "own_alliance"
MODE_FULL_BRACKET = "full_bracket"
TRACKING_MODES: tuple[str, ...] = (MODE_OWN_ALLIANCE, MODE_FULL_BRACKET)


def tier_rank(tier) -> int | None:
    """Competitive rank of a tier name — Diamond outranks Gold outranks Silver.

    Returns ``None`` for an unrecognised value so callers can render it
    verbatim rather than silently sorting it to the bottom."""
    if not tier:
        return None
    key = str(tier).strip().casefold()
    key = re.sub(r"\s*tier\s*$", "", key).strip()  # "Diamond Tier" → "diamond"
    for i, name in enumerate(TIER_ORDER):
        if name.casefold() == key:
            return i
    return None


def known_rank(value) -> int | None:
    """Rank of a Known read on :data:`KNOWN_SCALE`, or ``None`` if it doesn't
    match the vocabulary. Unrecognised free text is deliberately not coerced —
    an unranked read falls through to the computed estimate instead of being
    guessed at."""
    if not value:
        return None
    key = re.sub(r"\s+", " ", str(value).strip().casefold())
    key = _KNOWN_ALIASES.get(key, key)
    for i, name in enumerate(KNOWN_SCALE):
        if name == key:
            return i
    return None


# ── Column names ──────────────────────────────────────────────────────────────
#
# Addressed by header *name* via `transfer.header_index` / `transfer.cell_for`,
# never by fixed position. These constants are the names the bot writes when it
# seeds the tab; a user who renames a header breaks only that column's binding.

COL_SEASON = "Season"
COL_TIER = "Tier"
COL_GROUP = "Group"
COL_WEEK_DATE = "Week Date"
COL_WEEK = "Week"
COL_SEED = "Seed"
COL_TAG = "Tag"
COL_WARZONE = "Warzone"
COL_NAME = "Name"
COL_POWER = "Power"
COL_MEMBERS = "Members"
COL_GIFT_LEVEL = "Gift Level"
COL_OPPONENT_TAG = "Opponent Tag"
COL_OPPONENT_WARZONE = "Opponent Warzone"
COL_WEEK_SCORE = "Week Score"
COL_WEEK_OUTCOME = "Week Outcome"
COL_KNOWN_1_5 = "Known Days 1-5"
COL_KNOWN_6 = "Known Day 6"
COL_PICKED = "Picked"
COL_PICKED_BY = "Picked By"
COL_INTENT = "Intent"
COL_NOTES = "Notes"


def day_score_col(day: int) -> str:
    return f"Day {day} Score"


def day_outcome_col(day: int) -> str:
    return f"Day {day} Outcome"


#: Header row the bot seeds the tab with, in order. Users may reorder or insert
#: columns afterwards; everything resolves by name.
SHEET_COLUMNS: tuple[str, ...] = (
    COL_SEASON,
    COL_TIER,
    COL_GROUP,
    COL_WEEK_DATE,
    COL_WEEK,
    COL_SEED,
    COL_TAG,
    COL_WARZONE,
    COL_NAME,
    COL_POWER,
    COL_MEMBERS,
    COL_GIFT_LEVEL,
    COL_OPPONENT_TAG,
    COL_OPPONENT_WARZONE,
    *(day_score_col(d) for d in range(1, 7)),
    *(day_outcome_col(d) for d in range(1, 7)),
    COL_WEEK_SCORE,
    COL_WEEK_OUTCOME,
    COL_KNOWN_1_5,
    COL_KNOWN_6,
    COL_PICKED,
    COL_PICKED_BY,
    COL_INTENT,
    COL_NOTES,
)

#: Columns whose value persists across rows for the same alliance — the
#: latest non-blank one wins. Nobody re-scouts 15 alliances weekly, so most of
#: these cells stay empty and the ones that do get filled become the
#: power-trajectory history for free.
PERSISTENT_COLUMNS: tuple[str, ...] = (
    COL_NAME,
    COL_POWER,
    COL_MEMBERS,
    COL_GIFT_LEVEL,
    COL_KNOWN_1_5,
    COL_KNOWN_6,
    COL_NOTES,
)


# ── Value coercion ────────────────────────────────────────────────────────────


def _parse_magnitude(value, magnitude: str | None) -> int | None:
    """Shared entry to ``survey``'s magnitude-aware shorthand parser.

    Reused rather than reimplemented so the same ``301`` / ``300m`` / ``1.2b``
    / ``304,743,912`` shapes members already type into surveys work here too
    (CLAUDE.md 1.1.5). Blank is a real state throughout this feature, so it
    returns ``None`` rather than coercing to ``0``.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    from survey import parse_magnitude_input

    return parse_magnitude_input(s, magnitude)


def parse_int(value) -> int | None:
    """Tolerant integer read of a plain count cell (week, seed, members, gift
    level). No implicit magnitude — a bare ``12`` means twelve."""
    return _parse_magnitude(value, None)


def parse_power(value) -> int | None:
    """Power read, magnitude-aware. A bare ``301`` on a power field means
    301,000,000 — nobody types nine digits by hand — while a value at or above
    1,000,000 is treated as already-raw. Matches the survey convention members
    are already used to."""
    return _parse_magnitude(value, "M")


def parse_score(value) -> int | None:
    """Day / week raw-score read.

    Suffix shorthand works (``6.4m`` → 6,400,000), but unlike :func:`parse_power`
    a bare number is taken **literally**, and that is deliberate.

    An established alliance posts 500m to 5b on a day, which makes it tempting
    to read a bare ``500`` as 500 million the way a power field would. Don't:
    an **early-game** alliance can legitimately post ``0``, ``1000`` or
    ``230000`` on a day, so there is no floor below which a small number is
    safely assumed to be shorthand. Scaling it would silently multiply a real
    score by a million, which is worse than making a big-alliance user type a
    unit. Anything ambiguous is caught by "Check my sheet" (#399) rather than
    guessed at here.
    """
    return _parse_magnitude(value, None)


_WIN_TOKENS = {"w", "win", "won", "wins", "1", "y", "yes", "true", "v"}
_LOSS_TOKENS = {"l", "loss", "lost", "lose", "0", "n", "no", "false", "x"}


def parse_outcome(value) -> str | None:
    """Read a W/L cell into ``"W"`` / ``"L"``, or ``None`` when blank or
    unrecognised. Deliberately permissive about what leadership types into a
    spreadsheet, and deliberately silent about anything else — a typo is caught
    by "Check my sheet" (#399), not coerced into a result here."""
    if value is None:
        return None
    s = str(value).strip().casefold()
    if not s:
        return None
    if s in _WIN_TOKENS:
        return "W"
    if s in _LOSS_TOKENS:
        return "L"
    return None


def parse_intent(value) -> str | None:
    """Read the Intent column into one of :data:`INTENTS`, or ``None``."""
    if value is None:
        return None
    s = str(value).strip().casefold()
    if not s:
        return None
    if s.startswith("push"):
        return INTENT_PUSH
    if s.startswith(("save", "saving", "saved")):
        return INTENT_SAVE
    if s in ("none", "no", "-", "n/a", "na", "unstated"):
        return INTENT_NONE
    return None


def parse_week_date(value, *, today: _dt.date | None = None) -> _dt.date | None:
    """Read a Week Date cell into a ``date``.

    The bot writes ISO, so the explicit-year shapes below cover everything it
    produces plus the formats a user is likely to type. A year-less value
    (``7/27``) is ambiguous for a tracker that records history as well as the
    live league; it falls through to ``storm_date_helpers.parse_event_date``,
    which resolves it *forward*. Documented rather than fixed, because the
    unambiguous fix is for the bot to keep writing the year.
    """
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    s_norm = re.sub(r"\s*([-/.])\s*", r"\1", s.replace(",", " ")).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y"):
        try:
            return _dt.datetime.strptime(s_norm, fmt).date()
        except ValueError:
            pass
    return parse_event_date(s, today=today)


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, order=True)
class AllianceKey:
    """An alliance's dedup identity: tag plus warzone.

    **Warzone, not "server".** It is the game's own word for a world
    (players say "server" colloquially, but ``server`` is reserved in this
    product for the *Discord* server, and letting the two senses share a word
    would put one term's opposite meaning on every VS surface). See the
    glossary in ``UX.md``.

    Both parts are normalised (trimmed, casefolded, ``#`` stripped from the
    tag) so ``[ABC] 1234`` and ``abc/1234`` are the same alliance. The display
    forms live on :class:`AllianceWeek`.
    """

    tag: str
    warzone: str

    @staticmethod
    def of(tag, warzone) -> "AllianceKey | None":
        t = re.sub(r"[\[\]#\s]", "", str(tag or "")).casefold()
        w = re.sub(r"[^0-9a-z]", "", str(warzone or "").casefold())
        if not t or not w:
            return None
        return AllianceKey(t, w)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"[{self.tag.upper()}] {self.warzone}"


@dataclass(frozen=True, order=True)
class LeagueKey:
    """A single 16-alliance bracket: season, tier and group.

    Game-supplied identity from the league start screen ("Alliance Duel League
    S35", "Diamond Tier 12 - 2"), not derived from dates. Many brackets run in
    parallel each season, so all three parts are needed.
    """

    season: str
    tier: str
    group: str

    @staticmethod
    def of(season, tier, group) -> "LeagueKey | None":
        se = re.sub(r"\s+", " ", str(season or "").strip())
        ti = re.sub(r"\s+", " ", str(tier or "").strip())
        gr = re.sub(r"\s+", " ", str(group or "").strip())
        if not se:
            return None
        return LeagueKey(se, ti, gr)

    @property
    def rank(self) -> int | None:
        """Competitive rank of this league's tier, for qualifying history."""
        return tier_rank(self.tier)

    def __str__(self) -> str:  # pragma: no cover - display only
        parts = [self.season]
        if self.tier:
            parts.append(self.tier)
        if self.group:
            parts.append(self.group)
        return " ".join(parts)


@dataclass(frozen=True, order=True)
class RowKey:
    """What locates a row for upsert: league, week, and alliance.

    The bot reads, finds this key, updates only that row's cells, and appends
    when absent. It never rewrites the tab wholesale, so hand edits survive.
    """

    league: LeagueKey
    week: int
    alliance: AllianceKey


@dataclass
class AllianceWeek:
    """One alliance's row for one league-week — the grain of the whole feature."""

    league: LeagueKey
    week: int
    alliance: AllianceKey

    week_date: _dt.date | None = None
    seed: int | None = None
    name: str = ""
    tag_display: str = ""
    warzone_display: str = ""

    power: int | None = None
    members: int | None = None
    gift_level: int | None = None

    opponent: AllianceKey | None = None

    day_scores: dict[int, int] = field(default_factory=dict)
    day_outcomes: dict[int, str] = field(default_factory=dict)
    week_score: int | None = None
    week_outcome: str | None = None

    known_1_5: str = ""
    known_6: str = ""
    picked: str | None = None
    picked_by: str = ""
    intent: str | None = None
    notes: str = ""

    #: 1-based sheet row this came from, or ``None`` for a row not yet written.
    row_number: int | None = None

    @property
    def key(self) -> RowKey:
        return RowKey(self.league, self.week, self.alliance)

    @property
    def won(self) -> bool | None:
        """Whether this alliance won the week, from the confirmed Week Outcome.
        ``None`` means unrecorded, which is a real state — never a loss."""
        if self.week_outcome == "W":
            return True
        if self.week_outcome == "L":
            return False
        return None

    def day_points(self, day: int) -> int:
        """League points this alliance took on `day` (0 if lost or unrecorded)."""
        return DUEL_DAY_BY_NUMBER[day].points if self.day_outcomes.get(day) == "W" else 0

    @property
    def day_points_total(self) -> int:
        """League points summed from the recorded Day Outcomes. When all six are
        present this must equal Week Score — a free validation check (#399)."""
        return sum(self.day_points(d) for d in range(1, 7))

    @property
    def has_all_day_outcomes(self) -> bool:
        return all(self.day_outcomes.get(d) in ("W", "L") for d in range(1, 7))

    @property
    def is_tier_1(self) -> bool:
        """All three prediction inputs present, which gates whether an estimate
        can be computed at all."""
        return self.power is not None and self.members is not None and self.gift_level is not None


@dataclass(frozen=True)
class AllianceProfile:
    """The merged, latest-non-blank view of one alliance across every row.

    Power / Members / Gift Level / Known / Notes persist: nobody re-scouts 15
    alliances weekly, so the newest non-blank cell wins and the ones that were
    filled become the power-trajectory history for free.
    """

    alliance: AllianceKey
    name: str = ""
    power: int | None = None
    members: int | None = None
    gift_level: int | None = None
    known_1_5: str = ""
    known_6: str = ""
    notes: str = ""

    #: When each persisted value was last filled in, so staleness can widen the
    #: confidence band rather than being invisible.
    as_of: dict[str, _dt.date] = field(default_factory=dict)

    #: (date, power) pairs, oldest first — the power trajectory. Growth is the
    #: output of activity, so this measures mobilization without asking anyone
    #: to judge it.
    power_history: tuple[tuple[_dt.date, int], ...] = ()

    @property
    def is_tier_1(self) -> bool:
        return self.power is not None and self.members is not None and self.gift_level is not None


# ── League / week / day resolution ────────────────────────────────────────────


def server_today(now: _dt.datetime | None = None) -> _dt.date:
    """Today's date on **server time** (UTC-2, no DST).

    Every "which duel day is it" decision goes through here. A guild in UTC+10
    sees Monday locally while it is still Sunday on server time, which would
    misfile a whole day of scores. CLAUDE.md flags server-vs-guild-local date
    resolution as a bug class that has already recurred three times (#330 /
    #318), so there is no guild-local variant of this function on purpose.
    """
    import config

    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    elif now.tzinfo is None:
        raise ValueError("server_today() needs a timezone-aware datetime")
    return config.server_date_for(now)


def duel_day_for_date(d: _dt.date) -> int | None:
    """Which duel day (1-6) a date falls on, or ``None`` for Sunday (off).

    `d` must already be a **server** date — see :func:`server_today`.
    """
    day = DUEL_DAY_BY_WEEKDAY.get(d.weekday())
    return day.number if day else None


def completed_duel_day(now: _dt.datetime | None = None) -> tuple[_dt.date, int] | None:
    """The (server date, duel day) that has just finished as of ``now``.

    What the daily score prompt (#405) asks about. It is deliberately *not*
    :func:`alliance_duel_entry.target_day`, which names the day currently
    running: a prompt asks for numbers that already exist, and the day in
    progress has none yet.

    Resolving it as "the server day before the current server day" also
    produces the Tuesday-through-Sunday schedule for free, with no weekday
    table. Server Monday's previous day is Sunday, which is not a duel day, so
    Monday returns ``None`` and nothing is asked. That holds whatever clock
    time the alliance picked, which a guild-local weekday check would not: at
    9am local the server date has not rolled over and the day being asked about
    is still running, while at 11pm local it already has.
    """
    yesterday = server_today(now) - _dt.timedelta(days=1)
    day = duel_day_for_date(yesterday)
    return (yesterday, day) if day is not None else None


def week_monday(d: _dt.date) -> _dt.date:
    """The Monday that starts `d`'s duel week.

    Sunday resolves to the Monday six days *back*, not the one the next day:
    Sunday is the rest day at the **end** of a duel week, so Sunday's score
    prompt covers that week's Saturday Enemy Buster. That falls straight out of
    ``weekday()`` treating Monday as 0 and Sunday as 6.
    """
    return d - _dt.timedelta(days=d.weekday())


@dataclass(frozen=True)
class LiveWeek:
    """Which league-week and duel day a date lands in, resolved against the
    Week Dates already recorded on the sheet."""

    league: LeagueKey
    week: int
    week_date: _dt.date
    day: int | None  # None on Sunday

    @property
    def is_rest_day(self) -> bool:
        return self.day is None

    @property
    def theme(self) -> str:
        return DUEL_DAY_BY_NUMBER[self.day].theme if self.day else ""


def resolve_live_week(
    rows: Iterable[AllianceWeek], today: _dt.date | None = None
) -> LiveWeek | None:
    """Which league-week is live on `today`, from the Week Dates on the sheet.

    Because the schedule is rigid (days 1-6 are Monday to Saturday, Sunday
    always off), today's date alone tells the bot which week and duel day is
    live — so "log today's score" needs two numbers and nothing else.

    `today` must be a **server** date; defaults to :func:`server_today`.
    Returns ``None`` when no recorded week covers today, which is the normal
    state between leagues.
    """
    if today is None:
        today = server_today()
    monday = week_monday(today)
    for row in rows:
        if row.week_date and week_monday(row.week_date) == monday:
            return LiveWeek(row.league, row.week, monday, duel_day_for_date(today))
    return None


def week_date_for(league_start: _dt.date, week: int) -> _dt.date:
    """The Monday of `week` (1-4) in a league that started on `league_start`."""
    return week_monday(league_start) + _dt.timedelta(weeks=week - 1)


def is_league_complete(rows: Iterable[AllianceWeek], league: LeagueKey) -> bool:
    """Whether every week of `league` has a recorded outcome — the trigger for
    the next-league rollover prompt."""
    weeks = {r.week for r in rows if r.league == league and r.week_outcome}
    return all(w in weeks for w in range(1, LEAGUE_WEEKS + 1))


# ── Sheet I/O ─────────────────────────────────────────────────────────────────


def parse_rows(values: Sequence[Sequence], *, today: _dt.date | None = None) -> list[AllianceWeek]:
    """Parse a worksheet's raw ``get_all_values()`` grid into rows.

    Row 1 is the header; columns resolve by name via ``transfer.header_index``
    so an inserted or moved column doesn't break the mapping. Rows missing a
    league or alliance identity are skipped — a blank spacer row between
    leagues is normal in a hand-maintained sheet, not an error.
    """
    if not values:
        return []
    header = list(values[0])
    hidx = transfer.header_index(header)
    out: list[AllianceWeek] = []

    for offset, raw in enumerate(values[1:], start=2):
        row = list(raw)

        def cell(name):
            return transfer.cell_for(row, hidx, name)

        league = LeagueKey.of(cell(COL_SEASON), cell(COL_TIER), cell(COL_GROUP))
        alliance = AllianceKey.of(cell(COL_TAG), cell(COL_WARZONE))
        week = parse_int(cell(COL_WEEK))
        if league is None or alliance is None or week is None:
            continue

        day_scores: dict[int, int] = {}
        day_outcomes: dict[int, str] = {}
        for d in range(1, 7):
            score = parse_score(cell(day_score_col(d)))
            if score is not None:
                day_scores[d] = score
            outcome = parse_outcome(cell(day_outcome_col(d)))
            if outcome is not None:
                day_outcomes[d] = outcome

        out.append(
            AllianceWeek(
                league=league,
                week=week,
                alliance=alliance,
                week_date=parse_week_date(cell(COL_WEEK_DATE), today=today),
                seed=parse_int(cell(COL_SEED)),
                name=cell(COL_NAME) or "",
                tag_display=cell(COL_TAG) or "",
                warzone_display=cell(COL_WARZONE) or "",
                power=parse_power(cell(COL_POWER)),
                members=parse_int(cell(COL_MEMBERS)),
                gift_level=parse_int(cell(COL_GIFT_LEVEL)),
                opponent=AllianceKey.of(cell(COL_OPPONENT_TAG), cell(COL_OPPONENT_WARZONE)),
                day_scores=day_scores,
                day_outcomes=day_outcomes,
                week_score=parse_int(cell(COL_WEEK_SCORE)),
                week_outcome=parse_outcome(cell(COL_WEEK_OUTCOME)),
                known_1_5=cell(COL_KNOWN_1_5) or "",
                known_6=cell(COL_KNOWN_6) or "",
                picked=parse_outcome(cell(COL_PICKED)),
                picked_by=cell(COL_PICKED_BY) or "",
                intent=parse_intent(cell(COL_INTENT)),
                notes=cell(COL_NOTES) or "",
                row_number=offset,
            )
        )
    return out


def row_values(row: AllianceWeek) -> dict[str, str]:
    """The cell values a row writes, keyed by column header name.

    Only non-empty values appear. That is what makes the upsert non-clobbering:
    a field the bot has nothing to say about is absent from this dict and so is
    never written, leaving whatever the user typed in place.
    """
    out: dict[str, str] = {
        COL_SEASON: row.league.season,
        COL_TIER: row.league.tier,
        COL_GROUP: row.league.group,
        COL_WEEK: str(row.week),
        COL_TAG: row.tag_display or row.alliance.tag.upper(),
        COL_WARZONE: row.warzone_display or row.alliance.warzone,
    }
    if row.week_date:
        out[COL_WEEK_DATE] = row.week_date.isoformat()
    if row.seed is not None:
        out[COL_SEED] = str(row.seed)
    if row.name:
        out[COL_NAME] = row.name
    if row.power is not None:
        out[COL_POWER] = str(row.power)
    if row.members is not None:
        out[COL_MEMBERS] = str(row.members)
    if row.gift_level is not None:
        out[COL_GIFT_LEVEL] = str(row.gift_level)
    if row.opponent is not None:
        out[COL_OPPONENT_TAG] = row.opponent.tag.upper()
        out[COL_OPPONENT_WARZONE] = row.opponent.warzone
    for d in range(1, 7):
        if d in row.day_scores:
            out[day_score_col(d)] = str(row.day_scores[d])
        if d in row.day_outcomes:
            out[day_outcome_col(d)] = row.day_outcomes[d]
    if row.week_score is not None:
        out[COL_WEEK_SCORE] = str(row.week_score)
    if row.week_outcome:
        out[COL_WEEK_OUTCOME] = row.week_outcome
    if row.known_1_5:
        out[COL_KNOWN_1_5] = row.known_1_5
    if row.known_6:
        out[COL_KNOWN_6] = row.known_6
    if row.picked:
        out[COL_PICKED] = row.picked
    if row.picked_by:
        out[COL_PICKED_BY] = row.picked_by
    if row.intent:
        out[COL_INTENT] = row.intent
    if row.notes:
        out[COL_NOTES] = row.notes
    return out


@dataclass(frozen=True)
class CellUpdate:
    """One cell the upsert will write, in A1 notation."""

    a1: str
    value: str


@dataclass(frozen=True)
class UpsertPlan:
    """What :func:`plan_upsert` decided to do, before anything is written.

    Pure output, so the non-clobbering guarantee is unit-testable without a
    worksheet: `updates` only ever names cells belonging to a matched row's own
    columns, and `appends` are whole new rows built against the live header.
    """

    updates: tuple[CellUpdate, ...] = ()
    appends: tuple[tuple[str, ...], ...] = ()
    #: Column names the caller wanted to write that the sheet has no header
    #: for. Surfaced rather than swallowed — a renamed header silently dropping
    #: writes is exactly the failure this addressing scheme exists to avoid.
    unmapped_columns: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.updates and not self.appends


def plan_upsert(
    values: Sequence[Sequence], rows: Iterable[AllianceWeek], *, today: _dt.date | None = None
) -> UpsertPlan:
    """Plan an upsert of `rows` against a worksheet's current `values`.

    Locates each row by :class:`RowKey` — (Season, Tier, Group, Week, Tag,
    Warzone) and updates only that row's own cells, appending when absent. A
    neighbouring hand-edited row is never touched, and within a matched row
    only the columns the caller actually has a value for are written. Same rule
    member sync learned in 1.4.2 (#262).
    """
    header = list(values[0]) if values else list(SHEET_COLUMNS)
    hidx = transfer.header_index(header)
    existing = {r.key: r for r in parse_rows(values, today=today)}

    updates: list[CellUpdate] = []
    appends: list[tuple[str, ...]] = []
    unmapped: list[str] = []
    next_row = len(values) + 1 if values else 2

    for row in rows:
        payload = row_values(row)
        for name in payload:
            if transfer.norm_header(name) not in hidx and name not in unmapped:
                unmapped.append(name)

        match = existing.get(row.key)
        if match is not None and match.row_number:
            for name, value in payload.items():
                idx = hidx.get(transfer.norm_header(name))
                if idx is None:
                    continue
                a1 = f"{transfer.col_index_to_letter(idx)}{match.row_number}"
                updates.append(CellUpdate(a1, value))
        else:
            line = [""] * len(header)
            for name, value in payload.items():
                idx = hidx.get(transfer.norm_header(name))
                if idx is not None and idx < len(line):
                    line[idx] = value
            appends.append(tuple(line))
            # Keep the key resolvable so two writes of the same row in one plan
            # don't append it twice.
            existing[row.key] = replace(row, row_number=next_row)
            next_row += 1

    return UpsertPlan(tuple(updates), tuple(appends), tuple(unmapped))


def apply_upsert(worksheet, plan: UpsertPlan) -> None:
    """Execute an :class:`UpsertPlan` against a gspread worksheet.

    Batched deliberately: 1.0.19 blew the 60/min Sheets write quota by calling
    ``append_row`` inside a loop, so cell updates go out as one
    ``batch_update`` and new rows as one ``append_rows``.
    """
    if plan.updates:
        worksheet.batch_update(
            [{"range": u.a1, "values": [[u.value]]} for u in plan.updates],
            value_input_option="USER_ENTERED",
        )
    if plan.appends:
        worksheet.append_rows(
            [list(r) for r in plan.appends],
            value_input_option="USER_ENTERED",
        )


def _row_sort_key(row: AllianceWeek) -> tuple:
    """Chronological order for latest-non-blank resolution: by week date when
    present, else by season/week so undated rows still order sensibly."""
    return (
        row.week_date or _dt.date.min,
        row.league.season,
        row.week,
    )


def build_profile(rows: Iterable[AllianceWeek], alliance: AllianceKey) -> AllianceProfile:
    """Merge every row for `alliance` into its latest-non-blank profile.

    Newest non-blank cell wins per persisted field, and the date each value was
    filled in is retained so staleness can widen the confidence band instead of
    being invisible (the core false-confidence failure mode).
    """
    mine = sorted((r for r in rows if r.alliance == alliance), key=_row_sort_key)
    profile = {
        "name": "",
        "power": None,
        "members": None,
        "gift_level": None,
        "known_1_5": "",
        "known_6": "",
        "notes": "",
    }
    as_of: dict[str, _dt.date] = {}
    history: list[tuple[_dt.date, int]] = []

    for row in mine:
        stamp = row.week_date
        for attr in profile:
            value = getattr(row, attr)
            if value in (None, ""):
                continue
            profile[attr] = value
            if stamp:
                as_of[attr] = stamp
        if row.power is not None and stamp:
            history.append((stamp, row.power))

    return AllianceProfile(
        alliance=alliance,
        as_of=as_of,
        power_history=tuple(history),
        **profile,
    )


def build_profiles(rows: Iterable[AllianceWeek]) -> dict[AllianceKey, AllianceProfile]:
    """Latest-non-blank profile for every alliance appearing in `rows`."""
    rows = list(rows)
    return {key: build_profile(rows, key) for key in {r.alliance for r in rows}}


# ── Head to head ──────────────────────────────────────────────────────────────
#
# The main payoff for the manual entry burden, because it is **real observed
# history** rather than anything this module estimates. Rows already carry tag,
# warzone, opponent, season and tier, so every prior meeting is recoverable
# without a second tab.
#
# Tier travels with each meeting rather than being flattened away. Promotion
# and relegation are real, so a result earned a tier down is weaker evidence
# about a current matchup than one earned in the present bracket, and the
# reader should see that distinction instead of having it quietly averaged in.


@dataclass(frozen=True)
class Meeting:
    """One prior match between two alliances, seen from the guild's side."""

    league: LeagueKey
    week: int
    week_date: _dt.date | None
    #: The guild's own row for that league-week.
    own: AllianceWeek
    #: The opponent's row, when it exists. Absent in own-alliance tracking
    #: mode (#448), where the pairing is recorded only on the guild's side.
    opponent: AllianceWeek | None = None

    @property
    def outcome(self) -> str | None:
        """``W`` / ``L`` from the guild's side, or ``None`` if unrecorded."""
        return self.own.week_outcome

    @property
    def score(self) -> tuple[int | None, int | None]:
        """The week's league-point split, guild's half first.

        The opponent's half is inferred from 13 when their row is absent,
        which is arithmetic rather than a guess: the two halves of a week
        always total :data:`WEEK_POINTS_TOTAL`.
        """
        mine = self.own.week_score
        theirs = self.opponent.week_score if self.opponent is not None else None
        if theirs is None and mine is not None:
            theirs = WEEK_POINTS_TOTAL - mine
        return mine, theirs

    @property
    def tier(self) -> str:
        return self.league.tier


@dataclass(frozen=True)
class HeadToHead:
    """Every recorded meeting between the guild and one other alliance."""

    own: AllianceKey
    opponent: AllianceKey
    #: Newest first, because the most recent meeting is the most relevant.
    meetings: tuple[Meeting, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.meetings)

    @property
    def wins(self) -> int:
        return sum(1 for m in self.meetings if m.outcome == "W")

    @property
    def losses(self) -> int:
        return sum(1 for m in self.meetings if m.outcome == "L")

    @property
    def unrecorded(self) -> int:
        """Meetings whose Week Outcome was never filled in. A real state, and
        never counted as a loss."""
        return sum(1 for m in self.meetings if m.outcome is None)

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}"

    def tier_movement(self, current_tier: str) -> tuple[str, int] | None:
        """How the opponent's competitive level has moved since you last met.

        Returns ``(previous_tier, delta)`` where a positive delta means they
        have come *up* since. This is a **game-adjudicated** signal and harder
        evidence than any proxy this module computes: an alliance you last met
        in Silver that now shares your Diamond bracket was promoted on real
        performance.

        ``None`` when there is no prior meeting, or when either tier is not on
        :data:`TIER_ORDER` — an unrecognised tier is rendered verbatim rather
        than silently ranked.
        """
        if not self.meetings:
            return None
        previous = self.meetings[0].tier
        now, then = tier_rank(current_tier), tier_rank(previous)
        if now is None or then is None or now == then:
            return None
        return previous, now - then


def head_to_head(
    rows: Iterable[AllianceWeek], own: AllianceKey, opponent: AllianceKey
) -> HeadToHead:
    """Recover every prior meeting between `own` and `opponent`, newest first.

    A meeting counts when *either* side's row names the other as its Opponent,
    so a week recorded from one side only still shows up. The guild's own row
    has to exist, though: it carries the outcome and the day scores, and a
    meeting rendered from the opponent's row alone would be a match the guild
    has no record of playing.
    """
    by_league_week: dict[tuple, dict[AllianceKey, AllianceWeek]] = {}
    for row in rows:
        by_league_week.setdefault((row.league, row.week), {})[row.alliance] = row

    meetings: list[Meeting] = []
    for (league, week), sides in by_league_week.items():
        mine, theirs = sides.get(own), sides.get(opponent)
        if mine is None:
            continue
        paired = mine.opponent == opponent or (theirs is not None and theirs.opponent == own)
        if paired:
            meetings.append(Meeting(league, week, mine.week_date, mine, theirs))

    meetings.sort(key=lambda m: _row_sort_key(m.own), reverse=True)
    return HeadToHead(own, opponent, tuple(meetings))


# ── Pairing ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Match:
    """One week's matchup between two alliances."""

    week: int
    a: AllianceKey
    b: AllianceKey

    def other(self, side: AllianceKey) -> AllianceKey | None:
        if side == self.a:
            return self.b
        if side == self.b:
            return self.a
        return None

    def __contains__(self, side) -> bool:
        return side in (self.a, self.b)


@dataclass(frozen=True)
class Standing:
    """An alliance's position going into a week."""

    alliance: AllianceKey
    score: int  # weighted score over confirmed results only
    seed: int | None
    wins: int
    losses: int


@dataclass(frozen=True)
class BracketIncomplete:
    """Why a bracket-dependent computation could not run (#448).

    Distinguishes "you chose not to track this" from "this data is missing".
    Those are different messages, and only the second one should prompt
    action — a bracket view firing an error at a deliberately own-alliance
    sheet is the tracker arguing with its user.
    """

    reason: str  # "own_alliance_mode" | "roster_size" | "missing_seeds"
    detail: str
    found: int = 0
    expected: int = BRACKET_SIZE

    @property
    def is_choice(self) -> bool:
        """True when this reflects a tracking-mode decision rather than a gap
        in the data. Callers show the upsell, once, rather than an error."""
        return self.reason == "own_alliance_mode"


@dataclass(frozen=True)
class WeekPairing:
    """The computed pairing for one week."""

    week: int
    matches: tuple[Match, ...] = ()
    standings: tuple[Standing, ...] = ()
    #: Matches the algorithm had to pair as a rematch because every remaining
    #: opponent had already been faced. Rare in a clean 16-alliance bracket;
    #: worth surfacing because it usually means the results are wrong.
    rematches: tuple[Match, ...] = ()

    def match_for(self, alliance: AllianceKey) -> Match | None:
        for m in self.matches:
            if alliance in m:
                return m
        return None


def weighted_score(rows: Iterable[AllianceWeek], alliance: AllianceKey, upto_week: int) -> int:
    """Weighted score going into `upto_week`, over **confirmed results only**.

    Week 1 is worth 8, week 2 is 4, week 3 is 2, week 4 is 1. Unrecorded weeks
    contribute nothing — an alliance whose result nobody entered is not treated
    as having lost.
    """
    total = 0
    for row in rows:
        if row.alliance != alliance or row.week >= upto_week:
            continue
        if row.won and 1 <= row.week <= len(WEEK_WEIGHTS):
            total += WEEK_WEIGHTS[row.week - 1]
    return total


def _prior_opponents(rows: Iterable[AllianceWeek], before_week: int) -> dict[AllianceKey, set]:
    """Who each alliance has already faced, from the confirmed Opponent columns
    of weeks before `before_week`. Both directions recorded, so a pairing only
    one side wrote still counts as a meeting."""
    met: dict[AllianceKey, set] = {}
    for row in rows:
        if row.week >= before_week or row.opponent is None:
            continue
        met.setdefault(row.alliance, set()).add(row.opponent)
        met.setdefault(row.opponent, set()).add(row.alliance)
    return met


def compute_week_pairing(
    alliances: Iterable[AllianceWeek], week: int
) -> WeekPairing | BracketIncomplete:
    """The literal spec pairing algorithm for one week of one league.

    Weighted score ``[8, 4, 2, 1]`` over confirmed results only, sorted
    descending with **seed as the tie-break**, then a greedy adjacent-pair walk
    down that order. When the next alliance in the order has already been
    faced, the walk skips ahead to the first one that hasn't; if every
    remaining candidate has been faced, it falls back to the adjacent one and
    records the rematch.

    Powers the confirmed bracket, standings, and the skeleton-row opponents the
    user only has to correct rather than type.

    `alliances` is every row of a **single league** (all weeks). Returns
    :class:`BracketIncomplete` rather than raising when there aren't sixteen
    distinct alliances — in own-alliance tracking mode (#448) that is a
    deliberate choice, not an error.
    """
    rows = list(alliances)
    seeds: dict[AllianceKey, int | None] = {}
    for row in rows:
        # First sighting registers the alliance; a later non-blank seed fills a
        # blank one, since seed is constant across a league's four weeks and the
        # user may only have typed it on the week-1 rows.
        if row.alliance not in seeds or (seeds[row.alliance] is None and row.seed is not None):
            seeds[row.alliance] = row.seed

    if len(seeds) < BRACKET_SIZE:
        return BracketIncomplete(
            reason="roster_size",
            detail=(
                f"Pairing needs all {BRACKET_SIZE} alliances in the bracket; "
                f"{len(seeds)} are recorded."
            ),
            found=len(seeds),
        )

    standings = tuple(
        sorted(
            (
                Standing(
                    alliance=key,
                    score=weighted_score(rows, key, week),
                    seed=seed,
                    wins=sum(1 for r in rows if r.alliance == key and r.week < week and r.won),
                    losses=sum(
                        1 for r in rows if r.alliance == key and r.week < week and r.won is False
                    ),
                )
                for key, seed in seeds.items()
            ),
            key=lambda s: (
                -s.score,
                s.seed if s.seed is not None else BRACKET_SIZE + 1,
                s.alliance,
            ),
        )
    )

    met = _prior_opponents(rows, week)
    remaining = list(standings)
    matches: list[Match] = []
    rematches: list[Match] = []

    while len(remaining) >= 2:
        a = remaining.pop(0)
        pick = next(
            (i for i, b in enumerate(remaining) if b.alliance not in met.get(a.alliance, ())),
            None,
        )
        is_rematch = pick is None
        b = remaining.pop(0 if is_rematch else pick)
        match = Match(week, a.alliance, b.alliance)
        matches.append(match)
        if is_rematch:
            rematches.append(match)

    return WeekPairing(week, tuple(matches), standings, tuple(rematches))


def pairing_disagreements(
    alliances: Iterable[AllianceWeek], week: int
) -> tuple[tuple[AllianceKey, AllianceKey | None, AllianceKey], ...]:
    """Where the recorded Opponent columns disagree with the computed pairing.

    The production half of the design's two cross-checks: real-world divergence
    surfaces instead of silently corrupting projections. Returns
    ``(alliance, recorded_opponent, predicted_opponent)`` triples.
    """
    rows = list(alliances)
    pairing = compute_week_pairing(rows, week)
    if isinstance(pairing, BracketIncomplete):
        return ()
    out = []
    for row in rows:
        if row.week != week or row.opponent is None:
            continue
        match = pairing.match_for(row.alliance)
        predicted = match.other(row.alliance) if match else None
        if predicted is not None and predicted != row.opponent:
            out.append((row.alliance, row.opponent, predicted))
    return tuple(out)


# ── Path projection ───────────────────────────────────────────────────────────

#: How a match in the projected path was decided, best evidence first.
SOURCE_CONFIRMED = "confirmed"
SOURCE_PICKED = "picked"
SOURCE_KNOWN = "known"
SOURCE_ESTIMATED = "estimated"
#: Not a path-projection source: the prediction model (#401) uses the same
#: vocabulary for what a matchup's call rests on, and needs a name for the
#: state where nothing at all has been recorded. Unassessed is a real state
#: and renders plainly, never folded into a confident-sounding label.
SOURCE_UNASSESSED = "unassessed"
#: Also not evidence: the caller asked "what if this week went this way?" and
#: the walk answered under that assumption (#407). Kept distinct from every
#: other source so a hypothetical can never render as a projection.
SOURCE_ASSUMED = "assumed"


@dataclass(frozen=True)
class MatchResolution:
    winner: AllianceKey
    loser: AllianceKey
    source: str


@dataclass(frozen=True)
class PathStep:
    """One week of the projected path."""

    week: int
    opponent: AllianceKey | None
    #: How the opponent's identity was arrived at — the weakest link in the
    #: chain of matches that had to resolve to name them.
    source: str | None
    #: How the target's own match that week resolved, once the opponent is
    #: known. ``None`` while the match itself is unresolved.
    outcome: str | None = None
    outcome_source: str | None = None


@dataclass(frozen=True)
class PathProjection:
    """The projected path through the bracket for one alliance."""

    target: AllianceKey
    steps: tuple[PathStep, ...] = ()
    #: Matches that must resolve before the path can continue, in the order
    #: they gate it. This doubles as the scouting priority list: not "go scout
    #: 15 alliances" but "these three determine your next two opponents."
    blocked_on: tuple[Match, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked_on)

    @property
    def scouting_priority(self) -> tuple[AllianceKey, ...]:
        """Alliances worth scouting next, in gating order, deduplicated."""
        out: list[AllianceKey] = []
        for match in self.blocked_on:
            for side in (match.a, match.b):
                if side != self.target and side not in out:
                    out.append(side)
        return tuple(out)


#: An estimator supplied by the prediction model (#401): given the two sides
#: and the week, return the projected winner, or ``None`` when it can't call
#: it. Kept as a callback so this module stays free of the voting model.
Estimator = Callable[[AllianceKey, AllianceKey, int], "AllianceKey | None"]


def project_own_path(
    target: AllianceKey,
    alliances: Iterable[AllianceWeek],
    upto_week: int = LEAGUE_WEEKS,
    *,
    estimate: Estimator | None = None,
    assume: Mapping[int, tuple[AllianceKey, str]] | None = None,
) -> PathProjection | BracketIncomplete:
    """Walk the bracket lineage to project `target`'s path through the league.

    Because each week's weight exceeds the sum of all later weights
    (8 > 4+2+1), the week-1 winner/loser split is **permanent**: the two
    cohorts never re-merge, and each resolves as its own seed-preserving
    single-elimination bracket. So an alliance's future opponents are
    computable in advance rather than guessed at — walking the lineage is
    equivalent to re-running :func:`compute_week_pairing` every week, which the
    randomized cross-check in the unit tests asserts directly.

    Each required match resolves through: confirmed result → an explicit Picked
    call → a Known read that ranks both sides differently → the `estimate`
    callback (#401) → **blocked**. A blocked match is named rather than
    collapsed into "not enough data", because naming it is what turns the
    projection into a scouting priority list.

    `assume` maps a week to ``(alliance, "W"|"L")`` and forces that outcome
    ahead of every other source, which is what makes "if we save this week, who
    do we end up facing?" answerable (#407). Because week 1 carries more weight
    than every later week combined, a save there is not a neutral resource
    decision: it moves the alliance into the lower cohort permanently, and the
    lineage walk is what turns that from a warning into a named list of
    opponents. Steps resolved this way carry :data:`SOURCE_ASSUMED`, so a
    hypothetical can never be rendered as a projection.

    Returns :class:`BracketIncomplete` when the roster isn't a full seeded
    bracket — in own-alliance tracking mode (#448) that is a choice, not an
    error, and the caller shows the upsell rather than a failure.
    """
    rows = list(alliances)

    seeded = _seeded_bracket(rows)
    if isinstance(seeded, BracketIncomplete):
        return seeded
    if target not in seeded:
        return BracketIncomplete(
            reason="roster_size",
            detail="The tracked alliance doesn't appear in this league's bracket.",
            found=len(seeded),
        )

    seed_index = seeded.index(target)
    blocked: list[Match] = []
    resolver = _MatchResolver(rows, estimate, blocked, assume=assume)
    memo: dict[tuple, tuple[AllianceKey | None, str]] = {}

    def occupant(week: int, path: tuple[str, ...], pos: int) -> tuple[AllianceKey | None, str]:
        """Who sits at `pos` in the cohort reached by `path` at `week`, and how
        strongly that identity is established.

        Week 1's cohort is the seeded bracket itself. Every later cohort is
        formed from its parent by taking the winners (or losers) of the
        parent's adjacent-pair matches *in place*, which preserves seed order —
        the winner of match i always outranks the winner of match i+1, because
        their whole feeder brackets do. That is why walking the lineage and
        re-sorting by weighted score land on the same pairing.

        The returned source is the **weakest** link across the whole subtree
        that had to resolve to name this occupant. An opponent reached through
        one estimated match is an estimated opponent however many confirmed
        results sit alongside it.
        """
        if (week, path, pos) in memo:
            return memo[(week, path, pos)]
        if week <= 1:
            result = (seeded[pos] if 0 <= pos < len(seeded) else None, SOURCE_CONFIRMED)
        else:
            a, src_a = occupant(week - 1, path[:-1], 2 * pos)
            b, src_b = occupant(week - 1, path[:-1], 2 * pos + 1)
            if a is None or b is None:
                result = (None, SOURCE_ESTIMATED)
            else:
                res = resolver.resolve(week - 1, a, b)
                if res is None:
                    result = (None, SOURCE_ESTIMATED)
                else:
                    who = res.winner if path[-1] == "W" else res.loser
                    result = (who, _weakest_source(src_a, src_b, res.source))
        memo[(week, path, pos)] = result
        return result

    steps: list[PathStep] = []
    path: tuple[str, ...] = ()

    for week in range(1, min(upto_week, LEAGUE_WEEKS) + 1):
        pos = seed_index >> (week - 1)
        opponent, source = occupant(week, path, pos ^ 1)
        if opponent is None:
            steps.append(PathStep(week, None, None))
            break

        own = resolver.resolve(week, target, opponent)
        if own is None:
            steps.append(PathStep(week, opponent, source))
            break

        steps.append(
            PathStep(
                week=week,
                opponent=opponent,
                source=source,
                outcome="W" if own.winner == target else "L",
                outcome_source=own.source,
            )
        )
        path += ("W" if own.winner == target else "L",)

    return PathProjection(target, tuple(steps), tuple(blocked))


def _seeded_bracket(rows: Sequence[AllianceWeek]) -> list[AllianceKey] | BracketIncomplete:
    """The league's sixteen alliances in seed order, or why they aren't."""
    seeds: dict[AllianceKey, int] = {}
    seen: set[AllianceKey] = set()
    for row in rows:
        seen.add(row.alliance)
        if row.seed is not None:
            seeds[row.alliance] = row.seed

    if len(seen) < BRACKET_SIZE:
        return BracketIncomplete(
            reason="roster_size",
            detail=(
                f"A projected path needs all {BRACKET_SIZE} alliances in the bracket; "
                f"{len(seen)} are recorded."
            ),
            found=len(seen),
        )
    missing = seen - set(seeds)
    if missing:
        return BracketIncomplete(
            reason="missing_seeds",
            detail=f"{len(missing)} alliance(s) have no seed recorded.",
            found=len(seeds),
        )
    if sorted(seeds.values()) != list(range(1, BRACKET_SIZE + 1)):
        return BracketIncomplete(
            reason="missing_seeds",
            detail=f"Seeds within a league must be 1-{BRACKET_SIZE} and unique.",
            found=len(seeds),
        )
    return sorted(seeds, key=lambda k: seeds[k])


#: Evidence strength, best first. Used to take the weakest link across a chain
#: of matches — see :func:`_weakest_source`.
_SOURCE_RANK = {SOURCE_CONFIRMED: 0, SOURCE_PICKED: 1, SOURCE_KNOWN: 2, SOURCE_ESTIMATED: 3}


def _weakest_source(*sources: str) -> str:
    """The weakest evidence among `sources`. A conclusion is only as strong as
    the flimsiest step that produced it."""
    return max(sources, key=lambda s: _SOURCE_RANK.get(s, len(_SOURCE_RANK)))


class _MatchResolver:
    """Resolves a single match through the evidence chain, recording blockers.

    Kept as a class rather than a closure so the per-match cache, the blocked
    list and the precomputed Known profiles are shared across the whole
    lineage walk — the same match is reached from several branches, and
    rebuilding profiles per visit would rescan every row each time.
    """

    def __init__(
        self,
        rows: Sequence[AllianceWeek],
        estimate: Estimator | None,
        blocked: list[Match],
        *,
        assume: Mapping[int, tuple[AllianceKey, str]] | None = None,
    ) -> None:
        self._estimate = estimate
        self._blocked = blocked
        self._assume = dict(assume or {})
        self._cache: dict[tuple, MatchResolution | None] = {}
        self._profiles = build_profiles(rows)
        self._by_week_alliance: dict[tuple[int, AllianceKey], list[AllianceWeek]] = {}
        for row in rows:
            self._by_week_alliance.setdefault((row.week, row.alliance), []).append(row)

    def resolve(self, week: int, a: AllianceKey, b: AllianceKey) -> MatchResolution | None:
        cache_key = (week, *sorted((a, b)))
        if cache_key not in self._cache:
            result = self._resolve_uncached(week, a, b)
            self._cache[cache_key] = result
            if result is None:
                match = Match(week, a, b)
                if match not in self._blocked:
                    self._blocked.append(match)
        return self._cache[cache_key]

    def _rows_for(self, week: int, side: AllianceKey) -> list[AllianceWeek]:
        return self._by_week_alliance.get((week, side), [])

    def _resolve_uncached(
        self, week: int, a: AllianceKey, b: AllianceKey
    ) -> MatchResolution | None:
        # 0. An outcome the caller assumed for this week (#407), which is how
        #    "if we save this week, who do we end up facing?" gets answered. It
        #    outranks even a confirmed result, because the question is
        #    explicitly counterfactual: the caller knows what the sheet says.
        #    Only ever applies to the alliance the assumption names.
        assumed = self._assume.get(week)
        if assumed is not None:
            side, outcome = assumed
            if side in (a, b):
                other = b if side == a else a
                return (
                    MatchResolution(side, other, SOURCE_ASSUMED)
                    if outcome == "W"
                    else MatchResolution(other, side, SOURCE_ASSUMED)
                )

        # 1. Confirmed result. Trust either side's row; a disagreement between
        #    the two is a validation finding (#399), not something to average.
        for side, other in ((a, b), (b, a)):
            for row in self._rows_for(week, side):
                if row.won is None:
                    continue
                if row.opponent is not None and row.opponent != other:
                    continue
                return (
                    MatchResolution(side, other, SOURCE_CONFIRMED)
                    if row.won
                    else MatchResolution(other, side, SOURCE_CONFIRMED)
                )

        # 2. An explicit Picked call on this match, from either side's row.
        for side, other in ((a, b), (b, a)):
            for row in self._rows_for(week, side):
                if row.picked is None:
                    continue
                if row.opponent is not None and row.opponent != other:
                    continue
                return (
                    MatchResolution(side, other, SOURCE_PICKED)
                    if row.picked == "W"
                    else MatchResolution(other, side, SOURCE_PICKED)
                )

        # 3. A standing Known read that ranks the two sides differently. Equal
        #    or unranked reads fall through rather than breaking the tie by
        #    fiat — Known and Picked take priority over the computed estimate,
        #    but only where they actually say something.
        ranks = {}
        for side in (a, b):
            profile = self._profiles.get(side)
            ranks[side] = known_rank(profile.known_1_5) if profile else None
        if ranks[a] is not None and ranks[b] is not None and ranks[a] != ranks[b]:
            winner = a if ranks[a] > ranks[b] else b
            loser = b if winner == a else a
            return MatchResolution(winner, loser, SOURCE_KNOWN)

        # 4. The prediction model (#401), if one was supplied.
        if self._estimate is not None:
            projected = self._estimate(a, b, week)
            if projected in (a, b):
                return MatchResolution(projected, b if projected == a else a, SOURCE_ESTIMATED)

        return None


# ── Prediction model (#401) ───────────────────────────────────────────────────
#
# Fills the :data:`Estimator` seam above. Three inputs, compared independently,
# with confidence coming from how much they agree rather than from a blended
# score. There is no data to calibrate weights against (32 matches per league,
# only four involving the guild), so any 0.5/0.25/0.25 would be invented, and a
# weighted sum hides genuine disagreement by averaging it away where voting
# surfaces it. The components are also printable — "Power +34%, gifts +3
# levels, members even" is something an R5 can act on where a composite 0.73 is
# not.
#
# **The output is a capacity ceiling, not a prediction, and every surface has to
# say so.** Two things it structurally cannot see, and neither is a defect to be
# fixed later:
#
# - **Mobilization.** Nearly every high-value action on these boards is a "did
#   you bother" action rather than an "are you strong" action: hoarded speedups,
#   radar tasks, stamina, banked shards. Alliances hold these regardless; the
#   question is whether members spend them.
# - **Duel tech.** The per-player Alliance Duel tech tree multiplies what an
#   action awards, so a maxed account reads roughly three times an un-teched one
#   on the same board on the same day. Two alliances identical on power, members
#   and gift level can therefore differ about threefold in points per stockpile
#   spent, and **none of the three Tier 1 inputs can see it.** Purchased points
#   are excluded from the largest boost, which dilutes gift level specifically —
#   the same spend buys relatively less as an account matures.
#
# Both are unmeasured in *every* matchup, so neither can differentiate one
# alliance from another: they set how loudly the model is allowed to speak, not
# which way it leans. Concretely, the tech multiplier is why :data:`OUTLOOK_EASY`
# demands unanimous agreement across three independent inputs rather than one
# large power margin. Against an unmeasured threefold multiplier a 30% power
# edge on its own is not evidence of much, whereas three inputs agreeing at
# least suggests an alliance that is further along generally.
#
# The observed proxies — points per member, power trajectory, both sides' day
# scores — measure realised output, so the tech multiplier is already inside
# them without anyone having to know it exists. They belong to the analytics
# surfaces rather than here, and they are the reason this model is deliberately
# small.
#
# Thresholds and bands ship as module-level constants so tuning them once real
# results accumulate is a constant change rather than a rewrite. Read them as
# conservative guesses: saying "easy win" and losing under-mobilizes the
# alliance, which is worse than saying toss-up and winning comfortably.

METRIC_POWER = "power"
METRIC_GIFT_LEVEL = "gift_level"
METRIC_MEMBERS = "members"

#: The three Tier 1 inputs, in the order the components line prints them —
#: power first because it is the primary input, members last because total power
#: already contains member count (total = average x count), so members is the
#: minor of the two independent inputs and average power is derived, never voted.
PREDICTION_METRICS: tuple[str, ...] = (METRIC_POWER, METRIC_GIFT_LEVEL, METRIC_MEMBERS)

#: What each metric is called in the components line.
METRIC_LABELS: dict[str, str] = {
    METRIC_POWER: "power",
    METRIC_GIFT_LEVEL: "gifts",
    METRIC_MEMBERS: "members",
}

#: Ratio thresholds for the two count-like inputs. A lean is measured on the
#: larger-over-smaller ratio so the classification is direction-symmetric.
POWER_SLIGHT_RATIO = 1.10
POWER_STRONG_RATIO = 1.30
MEMBERS_SLIGHT_RATIO = 1.10
MEMBERS_STRONG_RATIO = 1.25

#: Gift level is a level, so it compares by difference rather than by ratio.
GIFT_SLIGHT_LEVELS = 2
GIFT_STRONG_LEVELS = 5

#: Inputs at least this old stop locking days and cost the top confidence rung.
#: A league is four weeks, so this is roughly "these numbers predate the league
#: they are being used to project". A confident call from months-old power is
#: the core false-confidence failure mode.
STALE_AFTER_DAYS = 35

#: How far apart one metric puts the two alliances.
LEAN_EVEN = "even"
LEAN_SLIGHT = "slight"
LEAN_STRONG = "strong"

#: How much the three metrics agree. Named from the reader's side, not the
#: model's: `toss-up` is a real answer, not a failure to produce one.
CONFIDENCE_CONFIDENT = "confident"
CONFIDENCE_MODERATE = "moderate"
CONFIDENCE_TOSSUP = "toss-up"

#: The headline label, symmetric about a toss-up. `easy` / `hard` are the same
#: call seen from the two sides, and both need a computed margin behind them.
OUTLOOK_EASY = "easy"
OUTLOOK_LIKELY = "likely"
OUTLOOK_TOSSUP = "toss-up"
OUTLOOK_UNLIKELY = "unlikely"
OUTLOOK_HARD = "hard"
#: Not a rung on that ladder. Unassessed is a real state and renders plainly,
#: never folded into a confident-sounding label.
OUTLOOK_UNASSESSED = "unassessed"

#: Where a projected grind day sits. A day is only called locked on a strong
#: lean; a slight one stays contested, per the asymmetric error cost.
BUCKET_FAVORED_YOU = "favored_you"
BUCKET_CONTESTED = "contested"
BUCKET_FAVORED_THEM = "favored_them"

#: Which metric decides each grind day. Days 2-5 are stockpile days and total
#: alliance power is roughly total accumulated development, which is roughly
#: total stockpile. Day 1's actions are per-member capped, making it the one
#: headcount day. Day 4 is the shard day, where gift level's spend propensity is
#: strongest, so it reads gift level alongside power.
#:
#: **Day 6 is absent on purpose.** FSP is not visible in game and hero-kit
#: counters can flip a modest stat edge into total dominance, so Enemy Buster
#: gets a human combat read or nothing at all.
DAY_METRICS: dict[int, tuple[str, ...]] = {
    1: (METRIC_MEMBERS,),
    2: (METRIC_POWER,),
    3: (METRIC_POWER,),
    4: (METRIC_POWER, METRIC_GIFT_LEVEL),
    5: (METRIC_POWER,),
}

#: Days a strong power lean is **not** enough to call on its own.
#:
#: Days 2 and 3 need somewhere to spend the speedups, and a fully maxed account
#: cannot dump construction speedups at all. Those four points therefore favour
#: actively growing accounts over maxed ones, which makes the power-to-score
#: relationship sub-linear and possibly non-monotonic exactly where it costs
#: most — and power is the only input that can see those days at all. So they
#: are called only when a second metric leans the same way, and the nuance
#: itself is left to a human Known read, which captures it better than any
#: formula would. Days 1 and 4 need no corroboration because their input maps
#: onto the day cleanly: day 1's actions are per-member capped and day 4 is
#: banked shards, neither of which has a dump-capacity ceiling.
DAYS_NEEDING_CORROBORATION: frozenset[int] = frozenset({2, 3})

#: Grind points that clinch the week before Enemy Buster, and the mirror below
#: which the opponent has clinched it. Take 7+ of 9 and day 6 is irrelevant;
#: take 2 or fewer and it is irrelevant the other way. Everything between is
#: decided on Saturday, which is where most real weeks land.
GRIND_CLINCH_POINTS = WEEK_POINTS_MAJORITY  # 7 of 9
GRIND_CONCEDE_POINTS = GRIND_POINTS_TOTAL - GRIND_CLINCH_POINTS  # 2 of 9

CLINCH_BEFORE_ENEMY_BUSTER = "clinched"
CLINCH_DAY_SIX_DECIDES = "day_six_decides"
CLINCH_CONCEDED = "conceded"
CLINCH_OPEN = "open"

#: Printed under every projection. The one thing a reader must not take away is
#: that this is a forecast of what will happen.
CAPACITY_CEILING_NOTE = (
    "This is a capacity ceiling, not a prediction. It reads stockpiles off "
    "power, members and gift level. It cannot see whether members actually "
    "spend them, nor how far either alliance has pushed its Duel tech, which "
    "on its own can swing points per stockpile spent about threefold."
)


@dataclass(frozen=True)
class MetricLean:
    """How far one metric separates the two alliances, and in whose favour."""

    metric: str
    direction: int  # +1 own, -1 opponent, 0 even
    strength: str  # LEAN_EVEN / LEAN_SLIGHT / LEAN_STRONG
    own: int | None = None
    opponent: int | None = None

    @property
    def label(self) -> str:
        """This metric's share of the components line, e.g. ``power +34%``."""
        name = METRIC_LABELS.get(self.metric, self.metric)
        if self.strength == LEAN_EVEN or self.own is None or self.opponent is None:
            return f"{name} even"
        if self.metric == METRIC_GIFT_LEVEL:
            # Levels compare by difference, and gift level 0 is a real value —
            # never a missing one — so this branch tests for None, not falsiness.
            diff = self.own - self.opponent
            return f"{name} {diff:+d} level{'' if abs(diff) == 1 else 's'}"
        if not self.opponent:
            return f"{name} even"
        return f"{name} {round((self.own / self.opponent - 1) * 100):+d}%"


@dataclass(frozen=True)
class AgreementVote:
    """The three metrics' independent verdicts, and how much they agree."""

    leans: tuple[MetricLean, ...]
    direction: int  # +1 own, -1 opponent, 0 no call
    confidence: str
    #: Age of the oldest of the six input values, or ``None`` when no row
    #: carried a Week Date to stamp them with.
    age_days: int | None = None
    stale: bool = False

    @property
    def components(self) -> str:
        """The per-metric breakdown, which is the point of voting over blending.

        An R5 can bank research speedups off "power +34%, gifts +3 levels,
        members even"; a composite score of 0.73 tells them nothing.
        """
        text = ", ".join(lean.label for lean in self.leans)
        return text[:1].upper() + text[1:]

    def lean_for(self, metric: str) -> MetricLean | None:
        for lean in self.leans:
            if lean.metric == metric:
                return lean
        return None


@dataclass(frozen=True)
class DayProjection:
    """One grind day, projected against the metric that decides it."""

    day: int
    points: int
    bucket: str
    #: The metrics consulted, so the surface can say why rather than just what.
    decided_by: tuple[str, ...] = ()

    @property
    def theme(self) -> str:
        return DUEL_DAY_BY_NUMBER[self.day].theme


@dataclass(frozen=True)
class WeekProjection:
    """What can honestly be said about one matchup before it is played.

    Deliberately not a single verdict. Each grind day is projected separately
    and reported as a range, which answers "can we clinch before Enemy Buster"
    — the question leadership actually has — where a label like "likely to win"
    never does. It also makes the no-combat-estimate rule a natural part of the
    output rather than an awkward caveat bolted on the end.
    """

    own: AllianceKey
    opponent: AllianceKey
    #: Which evidence the call rests on: :data:`SOURCE_PICKED`,
    #: :data:`SOURCE_KNOWN`, :data:`SOURCE_ESTIMATED` or
    #: :data:`SOURCE_UNASSESSED`.
    status: str
    outlook: str
    vote: AgreementVote | None = None
    days: tuple[DayProjection, ...] = ()
    #: The opponent's standing Known Day 6 read, verbatim. Never computed.
    combat_read: str = ""
    #: True when the human read that decided `status` points the other way from
    #: the computed vote. Surfaced rather than resolved silently.
    overridden: bool = False

    # ── Grind-point arithmetic ────────────────────────────────────────────

    @property
    def locked_own(self) -> int:
        return sum(d.points for d in self.days if d.bucket == BUCKET_FAVORED_YOU)

    @property
    def locked_them(self) -> int:
        return sum(d.points for d in self.days if d.bucket == BUCKET_FAVORED_THEM)

    @property
    def contested(self) -> int:
        """Grind points not called either way. With nothing projected at all,
        every grind point is contested, which is the honest starting range."""
        if not self.days:
            return GRIND_POINTS_TOTAL
        return sum(d.points for d in self.days if d.bucket == BUCKET_CONTESTED)

    @property
    def low(self) -> int:
        """Grind points if every contested day goes against you."""
        return self.locked_own

    @property
    def high(self) -> int:
        """Grind points if every contested day goes your way."""
        return self.locked_own + self.contested

    @property
    def clinch(self) -> str:
        """Whether Enemy Buster can still decide the week on this projection."""
        return clinch_outlook(self.low, self.high)

    @property
    def has_combat_read(self) -> bool:
        return bool(self.combat_read.strip())

    # ── Rendering ─────────────────────────────────────────────────────────

    @property
    def grind_line(self) -> str:
        if not self.days or self.vote is None:
            return (
                "Not projected. An estimate needs power, members and gift level "
                "recorded for both alliances."
            )
        return (
            f"You {self.locked_own} locked, {self.contested} contested, "
            f"them {self.locked_them} locked. Projected {self.low} to {self.high} "
            f"of {GRIND_POINTS_TOTAL}. {self.vote.components}."
        )

    @property
    def buster_line(self) -> str:
        if self.has_combat_read:
            return f"Read on this alliance: {self.combat_read.strip()}."
        return "No combat read on this alliance yet."

    @property
    def disagreement_line(self) -> str:
        """Said out loud when the human read points away from the numbers.

        Not resolved silently. A read that contradicts the recorded stats is
        usually the most interesting thing on the screen — either the numbers
        are stale, or somebody has seen something the numbers cannot show, and
        both are worth a second look before Monday.
        """
        if not self.overridden or self.vote is None:
            return ""
        favoured = "you" if self.vote.direction > 0 else "them"
        source = "picked call" if self.status == SOURCE_PICKED else "Known read"
        return (
            f"The {source} takes priority here and points the other way: the "
            f"recorded numbers favour {favoured}. The per-day split is the "
            "numbers' view, not the read's."
        )

    @property
    def verdict_line(self) -> str:
        state = self.clinch
        if state == CLINCH_BEFORE_ENEMY_BUSTER:
            return (
                f"Projected to clinch before Enemy Buster: {self.low} of "
                f"{GRIND_POINTS_TOTAL} at worst, and {GRIND_CLINCH_POINTS} takes the week."
            )
        if state == CLINCH_CONCEDED:
            return (
                f"Projected at most {self.high} of {GRIND_POINTS_TOTAL} on the grind days, "
                "so Enemy Buster cannot save the week."
            )
        need = f"You need {GRIND_CLINCH_POINTS} of {GRIND_POINTS_TOTAL} to clinch before Saturday."
        if state == CLINCH_DAY_SIX_DECIDES:
            need = "Enemy Buster decides this week on the projection."
        return need if self.has_combat_read else f"{need} Get a read."

    @property
    def caveat_line(self) -> str:
        """The honesty rules, rendered. Staleness and the unseeable Duel tech
        multiplier are the two reasons this is a ceiling rather than a call."""
        note = CAPACITY_CEILING_NOTE
        if self.vote is not None and self.vote.stale and self.vote.age_days is not None:
            note += (
                f" The numbers behind it are up to {self.vote.age_days} days old, "
                "so the band is widened and no day is called locked."
            )
        return note

    @property
    def lines(self) -> tuple[str, ...]:
        """Grind days, Enemy Buster, verdict, caveat — in reading order, with
        the disagreement note included only when there is one."""
        return tuple(
            line
            for line in (
                self.grind_line,
                self.buster_line,
                self.disagreement_line,
                self.verdict_line,
                self.caveat_line,
            )
            if line
        )


def _ratio_lean(
    metric: str, own: int | None, opponent: int | None, slight: float, strong: float
) -> MetricLean:
    """Classify a count-like metric on the larger-over-smaller ratio."""
    if not own or not opponent or own <= 0 or opponent <= 0:
        return MetricLean(metric, 0, LEAN_EVEN, own, opponent)
    direction = 1 if own >= opponent else -1
    magnitude = own / opponent if direction > 0 else opponent / own
    if magnitude >= strong:
        return MetricLean(metric, direction, LEAN_STRONG, own, opponent)
    if magnitude >= slight:
        return MetricLean(metric, direction, LEAN_SLIGHT, own, opponent)
    return MetricLean(metric, 0, LEAN_EVEN, own, opponent)


def _level_lean(
    metric: str, own: int | None, opponent: int | None, slight: int, strong: int
) -> MetricLean:
    """Classify a level-like metric on the plain difference."""
    if own is None or opponent is None:
        return MetricLean(metric, 0, LEAN_EVEN, own, opponent)
    diff = own - opponent
    if abs(diff) >= strong:
        return MetricLean(metric, 1 if diff > 0 else -1, LEAN_STRONG, own, opponent)
    if abs(diff) >= slight:
        return MetricLean(metric, 1 if diff > 0 else -1, LEAN_SLIGHT, own, opponent)
    return MetricLean(metric, 0, LEAN_EVEN, own, opponent)


def _tally(leans: Sequence[MetricLean]) -> tuple[str, int]:
    """Agreement voting: confidence comes from how much the metrics agree.

    All three agreeing with two or more strong is confident; a majority leaning
    one way with nothing contradicting is moderate; any metric contradicting
    another, or all near even, is a toss-up.
    """
    directions = {lean.direction for lean in leans if lean.direction}
    if len(directions) != 1:
        return CONFIDENCE_TOSSUP, 0
    direction = directions.pop()
    unanimous = all(lean.direction == direction for lean in leans)
    strong = sum(1 for lean in leans if lean.strength == LEAN_STRONG)
    if unanimous and strong >= 2:
        return CONFIDENCE_CONFIDENT, direction
    return CONFIDENCE_MODERATE, direction


def input_age_days(*profiles: AllianceProfile, today: _dt.date | None = None) -> int | None:
    """Age of the oldest prediction input across `profiles`, in days.

    ``None`` when nothing carried a Week Date to stamp it with — unknown age is
    reported as unknown rather than assumed fresh or assumed stale.
    """
    today = today or server_today()
    stamps = [p.as_of[m] for p in profiles for m in PREDICTION_METRICS if m in p.as_of]
    if not stamps:
        return None
    return max(0, (today - min(stamps)).days)


def assess(
    own: AllianceProfile, opponent: AllianceProfile, *, today: _dt.date | None = None
) -> AgreementVote | None:
    """Vote the three metrics against each other, from `own`'s side.

    Returns ``None`` when either side is short of Tier 1 — all three of power,
    members and gift level present. That gate is the whole reason Tier 1 exists,
    and a missing input is never treated as a zero.

    Staleness costs the top rung: you cannot be *confident* on numbers that
    predate the league. It does not erase a lean, because a large gap measured
    months ago is still evidence of a gap; what it stops the model doing is
    locking days, which :func:`project_grind_days` handles by widening the band.
    """
    if not (own.is_tier_1 and opponent.is_tier_1):
        return None

    leans = (
        _ratio_lean(
            METRIC_POWER, own.power, opponent.power, POWER_SLIGHT_RATIO, POWER_STRONG_RATIO
        ),
        _level_lean(
            METRIC_GIFT_LEVEL,
            own.gift_level,
            opponent.gift_level,
            GIFT_SLIGHT_LEVELS,
            GIFT_STRONG_LEVELS,
        ),
        _ratio_lean(
            METRIC_MEMBERS,
            own.members,
            opponent.members,
            MEMBERS_SLIGHT_RATIO,
            MEMBERS_STRONG_RATIO,
        ),
    )
    confidence, direction = _tally(leans)
    age = input_age_days(own, opponent, today=today)
    stale = age is not None and age >= STALE_AFTER_DAYS
    if stale and confidence == CONFIDENCE_CONFIDENT:
        confidence = CONFIDENCE_MODERATE
    return AgreementVote(leans, direction, confidence, age_days=age, stale=stale)


def project_grind_days(vote: AgreementVote) -> tuple[DayProjection, ...]:
    """Project days 1-5 separately, each against the metric that decides it.

    Days are bucketed **independently**, which is the whole reason for voting
    rather than blending: when power favours one alliance and headcount the
    other, the honest output is that they take different days, not an averaged
    verdict that hides the disagreement. So a matchup whose headline is a
    toss-up can still show four points leaning one way and one the other, and
    that split is more actionable than the headline is.

    A day is called only on a *strong* lean. A slight one stays contested, per
    the asymmetric error cost: saying "easy win" and losing under-mobilizes the
    alliance, which is worse than saying toss-up and winning comfortably. Stale
    inputs call nothing at all, which is how data age widens the band rather
    than quietly going unnoticed. Days 2 and 3 additionally need a second metric
    agreeing — see :data:`DAYS_NEEDING_CORROBORATION`.
    """
    out: list[DayProjection] = []
    for day in sorted(DAY_METRICS):
        metrics = DAY_METRICS[day]
        leans = [lean for lean in (vote.lean_for(m) for m in metrics) if lean is not None]
        directions = {lean.direction for lean in leans if lean.direction}
        bucket = BUCKET_CONTESTED
        if len(directions) == 1 and not vote.stale:
            direction = directions.pop()
            called = any(
                lean.strength == LEAN_STRONG and lean.direction == direction for lean in leans
            )
            if called and day in DAYS_NEEDING_CORROBORATION:
                called = any(
                    lean.direction == direction and lean.metric not in metrics
                    for lean in vote.leans
                )
            if called:
                bucket = BUCKET_FAVORED_YOU if direction > 0 else BUCKET_FAVORED_THEM
        out.append(DayProjection(day, DUEL_DAY_BY_NUMBER[day].points, bucket, metrics))
    return tuple(out)


def clinch_outlook(low: int, high: int) -> str:
    """Classify a projected grind-point range against the clinch thresholds.

    Scoped to the nine grind points, not the week's thirteen, because that is
    the question the projection answers: can this be settled before a day no
    formula can model. Live mid-week status is the same threshold on the other
    scale and lives on :class:`ClinchState`.
    """
    if low >= GRIND_CLINCH_POINTS:
        return CLINCH_BEFORE_ENEMY_BUSTER
    if high <= GRIND_CONCEDE_POINTS:
        return CLINCH_CONCEDED
    if low > GRIND_CONCEDE_POINTS and high < GRIND_CLINCH_POINTS:
        return CLINCH_DAY_SIX_DECIDES
    return CLINCH_OPEN


def _outlook(confidence: str, direction: int) -> str:
    if direction == 0 or confidence == CONFIDENCE_TOSSUP:
        return OUTLOOK_TOSSUP
    if confidence == CONFIDENCE_CONFIDENT:
        return OUTLOOK_EASY if direction > 0 else OUTLOOK_HARD
    return OUTLOOK_LIKELY if direction > 0 else OUTLOOK_UNLIKELY


def project_week(
    own: AllianceProfile,
    opponent: AllianceProfile,
    *,
    picked: str | None = None,
    today: _dt.date | None = None,
) -> WeekProjection:
    """Everything the tracker can honestly say about one matchup.

    The human reads win on disagreement: an explicit Picked call for this week,
    then a standing Known read that ranks the two sides differently, then the
    computed estimate, then nothing. A human read that the computation does not
    corroborate is **capped at likely / unlikely**, because ``easy`` requires a
    computed margin and a read on its own supplies direction without one.

    Note the per-day projection is still rendered underneath a human override.
    The two answer different questions — who wins, versus which days are close
    enough to be worth banking for — and hiding the second because someone
    typed "strong" would throw away the more actionable half.
    """
    vote = assess(own, opponent, today=today)
    days = project_grind_days(vote) if vote is not None else ()

    human_direction: int | None = None
    status: str | None = None
    if picked in ("W", "L"):
        human_direction = 1 if picked == "W" else -1
        status = SOURCE_PICKED
    else:
        mine, theirs = known_rank(own.known_1_5), known_rank(opponent.known_1_5)
        if mine is not None and theirs is not None and mine != theirs:
            human_direction = 1 if mine > theirs else -1
            status = SOURCE_KNOWN

    overridden = False
    if human_direction is not None and status is not None:
        if vote is not None and vote.direction == human_direction:
            outlook = _outlook(vote.confidence, vote.direction)
        else:
            outlook = OUTLOOK_LIKELY if human_direction > 0 else OUTLOOK_UNLIKELY
            overridden = vote is not None and vote.direction not in (0, human_direction)
    elif vote is not None:
        status = SOURCE_ESTIMATED
        outlook = _outlook(vote.confidence, vote.direction)
    else:
        status = SOURCE_UNASSESSED
        outlook = OUTLOOK_UNASSESSED

    return WeekProjection(
        own=own.alliance,
        opponent=opponent.alliance,
        status=status,
        outlook=outlook,
        vote=vote,
        days=days,
        combat_read=opponent.known_6,
        overridden=overridden,
    )


def make_estimator(
    source: Iterable[AllianceWeek] | dict[AllianceKey, AllianceProfile],
    *,
    today: _dt.date | None = None,
) -> Estimator:
    """Build the callback :func:`project_own_path` takes.

    Returns the projected winner, or ``None`` when the model declines to call
    the match: either side short of Tier 1, or the three metrics failing to
    agree on a direction. **Declining is the useful behaviour**, not a gap — an
    uncalled match becomes a named blocker on the path, which is exactly the
    "these three alliances determine your next two opponents" scouting list.
    Filling it with a coin flip would trade that list for false precision.

    The week is not consulted: profiles are latest-non-blank across every row,
    so there is one current reading of an alliance rather than a per-week one.
    """
    profiles = source if isinstance(source, dict) else build_profiles(source)

    def estimate(a: AllianceKey, b: AllianceKey, _week: int) -> AllianceKey | None:
        side_a, side_b = profiles.get(a), profiles.get(b)
        if side_a is None or side_b is None:
            return None
        vote = assess(side_a, side_b, today=today)
        if vote is None or vote.direction == 0:
            return None
        return a if vote.direction > 0 else b

    return estimate


@dataclass(frozen=True)
class ClinchState:
    """Where a week stands on the outcomes recorded so far.

    The same 7-of-13 threshold the projection uses, applied live instead of in
    advance. Once a day's outcome lands the bot knows the running split and what
    remains, which is the single most actionable thing it can say mid-week:
    "you're 5-3 up with day 5 (2 pts) and Enemy Buster (4 pts) left; winning day
    5 clinches it."
    """

    own_points: int
    opponent_points: int
    remaining_points: int
    #: Days with no outcome recorded yet, in order.
    remaining_days: tuple[int, ...] = ()
    #: Remaining days that would clinch the week on their own if won.
    clinching_days: tuple[int, ...] = ()

    @property
    def points_needed(self) -> int:
        return max(0, WEEK_POINTS_MAJORITY - self.own_points)

    @property
    def clinched(self) -> bool:
        return self.own_points >= WEEK_POINTS_MAJORITY

    @property
    def lost(self) -> bool:
        return self.opponent_points >= WEEK_POINTS_MAJORITY

    @property
    def decided(self) -> bool:
        """True once no arrangement of the remaining days can change the week."""
        return self.clinched or self.lost


def clinch_state(day_outcomes: Mapping[int, str]) -> ClinchState:
    """Live clinch arithmetic from one alliance's recorded Day Outcomes.

    `day_outcomes` maps duel day to ``W`` / ``L`` from that alliance's side, as
    stored on :class:`AllianceWeek`. Unrecorded days are genuinely unrecorded —
    never counted as a loss.
    """
    own = 0
    opponent = 0
    remaining: list[int] = []
    for day in sorted(DUEL_DAY_BY_NUMBER):
        points = DUEL_DAY_BY_NUMBER[day].points
        outcome = day_outcomes.get(day)
        if outcome == "W":
            own += points
        elif outcome == "L":
            opponent += points
        else:
            remaining.append(day)

    clinching = tuple(
        day for day in remaining if own + DUEL_DAY_BY_NUMBER[day].points >= WEEK_POINTS_MAJORITY
    )
    return ClinchState(
        own_points=own,
        opponent_points=opponent,
        remaining_points=sum(DUEL_DAY_BY_NUMBER[d].points for d in remaining),
        remaining_days=tuple(remaining),
        clinching_days=clinching,
    )


# ── Intent partition (#407) ───────────────────────────────────────────────────


@dataclass(frozen=True)
class IntentPartition:
    """The guild's own weeks, split by what its declaration does to a backtest.

    Intent is a **confounder the accuracy sample partitions on, not a flag that
    excludes rows wholesale**, and the difference matters: an alliance can
    declare a push and still lose. Four cases, three destinations.

    - **Push and lost** is the cleanest calibration signal available: full
      effort on our side and the call was still wrong. It belongs in the
      sample, weighted no differently.
    - **Push and won** is ordinary confirmation, also in the sample.
    - **Undeclared** is assumed to be normal effort. In the sample, but kept
      separately so a reader can see how much of the sample rests on an
      assumption rather than a statement.
    - **Save** comes out of the accuracy number entirely. A deliberate loss is
      not a failed prediction, and counting it as one would silently poison the
      only calibration data this feature will ever have.

    ``saved_and_won`` is the case worth surfacing rather than merely dropping:
    conceding a week and winning it anyway means either the opponent was far
    weaker than modelled or the save was never actually executed, and both say
    something. It is a subset of ``excluded``, not a fourth bucket.

    **The sample is never clean, only cleaner.** A declared push can still be
    contaminated by an opponent quietly saving, which is unobservable from our
    side until several weeks of both sides' day scores exist. Any surface
    reporting accuracy off this has to say so rather than presenting a number
    as though effort were controlled.
    """

    declared_push: tuple[AllianceWeek, ...] = ()
    undeclared: tuple[AllianceWeek, ...] = ()
    excluded: tuple[AllianceWeek, ...] = ()
    saved_and_won: tuple[AllianceWeek, ...] = ()

    @property
    def sample(self) -> tuple[AllianceWeek, ...]:
        """Every week accuracy may be measured against, in week order."""
        return tuple(sorted(self.declared_push + self.undeclared, key=lambda r: r.week))

    @property
    def rests_on_assumption(self) -> int:
        """How much of the sample is undeclared rather than stated. Reported
        alongside any accuracy figure, because "6 of 8 correct" reads very
        differently when 7 of the 8 were never declared either way."""
        return len(self.undeclared)


def partition_by_intent(rows: Iterable[AllianceWeek]) -> IntentPartition:
    """Split `rows` into the buckets :class:`IntentPartition` documents.

    Rows with no recorded outcome are dropped from every bucket: a week that
    has not happened yet is not evidence for or against anything.
    """
    push: list[AllianceWeek] = []
    none: list[AllianceWeek] = []
    excluded: list[AllianceWeek] = []
    saved_won: list[AllianceWeek] = []

    for row in rows:
        if row.week_outcome is None:
            continue
        intent = row.intent or INTENT_NONE
        if intent == INTENT_SAVE:
            excluded.append(row)
            if row.week_outcome == "W":
                saved_won.append(row)
        elif intent == INTENT_PUSH:
            push.append(row)
        else:
            none.append(row)

    return IntentPartition(
        declared_push=tuple(push),
        undeclared=tuple(none),
        excluded=tuple(excluded),
        saved_and_won=tuple(saved_won),
    )


# ── Validation ("Check my sheet") ─────────────────────────────────────────────
#
# Manual entry with no external source makes errors inevitable and silent, so
# validation is a feature rather than a nicety. Every finding names the row and
# the column, because "something is wrong somewhere in 64 rows" is not
# actionable.
#
# Rules 4, 5 and 6 assume a full 16-alliance bracket and run **only in
# full-bracket mode** (#448). Fired against an own-alliance sheet they would
# flag a deliberate choice as an error, which is the opposite of what the
# validation is for.

#: How much a finding should worry the reader. `error` is a contradiction in
#: the data; `warning` is a value that looks wrong but might not be.
SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    """One validation problem, addressed to a specific cell."""

    rule: int
    severity: str
    message: str
    #: 1-based sheet row, when the finding belongs to one. Cross-row findings
    #: name the row the reader should look at first.
    row_number: int | None = None
    column: str = ""
    alliance: AllianceKey | None = None

    @property
    def where(self) -> str:
        """Human-readable location, for the report embed."""
        parts = []
        if self.row_number:
            parts.append(f"row {self.row_number}")
        if self.column:
            parts.append(f"column {self.column}")
        return ", ".join(parts)


def _league_weeks(rows: Sequence[AllianceWeek]) -> dict[tuple, list[AllianceWeek]]:
    """Group rows by (league, week)."""
    out: dict[tuple, list[AllianceWeek]] = {}
    for row in rows:
        out.setdefault((row.league, row.week), []).append(row)
    return out


def _check_week_score_pairs(group: Sequence[AllianceWeek]) -> list[Finding]:
    """Rule 1: a matchup's two Week Scores must total 13."""
    out: list[Finding] = []
    seen: set = set()
    by_alliance = {r.alliance: r for r in group}
    for row in group:
        if row.opponent is None or row.week_score is None:
            continue
        pair = tuple(sorted((row.alliance, row.opponent)))
        if pair in seen:
            continue
        other = by_alliance.get(row.opponent)
        if other is None or other.week_score is None:
            continue
        seen.add(pair)
        total = row.week_score + other.week_score
        if total != WEEK_POINTS_TOTAL:
            out.append(
                Finding(
                    rule=1,
                    severity=SEVERITY_ERROR,
                    message=(
                        f"Week Scores for this matchup add up to {total}, not "
                        f"{WEEK_POINTS_TOTAL} ({row.week_score} + {other.week_score})."
                    ),
                    row_number=row.row_number,
                    column=COL_WEEK_SCORE,
                    alliance=row.alliance,
                )
            )
    return out


def _check_day_outcomes_sum(row: AllianceWeek) -> list[Finding]:
    """Rule 2: six recorded Day Outcomes must total the Week Score."""
    if not row.has_all_day_outcomes or row.week_score is None:
        return []
    if row.day_points_total == row.week_score:
        return []
    return [
        Finding(
            rule=2,
            severity=SEVERITY_ERROR,
            message=(
                f"The six Day Outcomes add up to {row.day_points_total} league points, "
                f"but Week Score says {row.week_score}."
            ),
            row_number=row.row_number,
            column=COL_WEEK_SCORE,
            alliance=row.alliance,
        )
    ]


def _check_outcome_agrees(row: AllianceWeek) -> list[Finding]:
    """Rule 3: Week Outcome must agree with Week Score."""
    if row.week_outcome is None or row.week_score is None:
        return []
    expected = "W" if row.week_score * 2 > WEEK_POINTS_TOTAL else "L"
    if row.week_outcome == expected:
        return []
    return [
        Finding(
            rule=3,
            severity=SEVERITY_ERROR,
            message=(
                f"Week Outcome says {row.week_outcome}, but a Week Score of "
                f"{row.week_score} of {WEEK_POINTS_TOTAL} is a {expected}."
            ),
            row_number=row.row_number,
            column=COL_WEEK_OUTCOME,
            alliance=row.alliance,
        )
    ]


def _check_reciprocal_opponents(group: Sequence[AllianceWeek]) -> list[Finding]:
    """Rule 4 (full bracket only): opponent references must be mutual."""
    out: list[Finding] = []
    by_alliance = {r.alliance: r for r in group}
    for row in group:
        if row.opponent is None:
            continue
        other = by_alliance.get(row.opponent)
        if other is None:
            out.append(
                Finding(
                    rule=4,
                    severity=SEVERITY_ERROR,
                    message="This row's opponent has no row of their own this week.",
                    row_number=row.row_number,
                    column=COL_OPPONENT_TAG,
                    alliance=row.alliance,
                )
            )
        elif other.opponent is not None and other.opponent != row.alliance:
            out.append(
                Finding(
                    rule=4,
                    severity=SEVERITY_ERROR,
                    message=(
                        "Opponents disagree: this row faces that alliance, but their "
                        "row names someone else."
                    ),
                    row_number=row.row_number,
                    column=COL_OPPONENT_TAG,
                    alliance=row.alliance,
                )
            )
    return out


def _check_seeds(rows: Sequence[AllianceWeek], league: LeagueKey) -> list[Finding]:
    """Rule 5 (full bracket only): seeds in a league are 1-16 and unique."""
    seeds: dict[AllianceKey, int] = {}
    first_row: dict[AllianceKey, AllianceWeek] = {}
    for row in rows:
        if row.league != league or row.seed is None:
            continue
        seeds.setdefault(row.alliance, row.seed)
        first_row.setdefault(row.alliance, row)

    out: list[Finding] = []
    counts: dict[int, list[AllianceKey]] = {}
    for alliance, seed in seeds.items():
        counts.setdefault(seed, []).append(alliance)
        if not 1 <= seed <= BRACKET_SIZE:
            out.append(
                Finding(
                    rule=5,
                    severity=SEVERITY_ERROR,
                    message=f"Seed {seed} is outside 1-{BRACKET_SIZE}.",
                    row_number=first_row[alliance].row_number,
                    column=COL_SEED,
                    alliance=alliance,
                )
            )
    for seed, holders in counts.items():
        if len(holders) > 1:
            for alliance in holders:
                out.append(
                    Finding(
                        rule=5,
                        severity=SEVERITY_ERROR,
                        message=(
                            f"Seed {seed} is used by {len(holders)} alliances in this league."
                        ),
                        row_number=first_row[alliance].row_number,
                        column=COL_SEED,
                        alliance=alliance,
                    )
                )
    return out


def _check_own_alliance_present(
    rows: Sequence[AllianceWeek], league: LeagueKey, own: AllianceKey
) -> list[Finding]:
    """Rule 6 (full bracket only): the configured own alliance appears."""
    if any(r.league == league and r.alliance == own for r in rows):
        return []
    return [
        Finding(
            rule=6,
            severity=SEVERITY_WARNING,
            message=(
                f"Your alliance doesn't appear anywhere in league {league}. "
                "Check the Tag and Warzone on those rows match your setup."
            ),
            column=COL_TAG,
            alliance=own,
        )
    ]


def _check_picked_agreement(group: Sequence[AllianceWeek]) -> list[Finding]:
    """Rule 7: Picked calls on both sides of a matchup must agree."""
    out: list[Finding] = []
    seen: set = set()
    by_alliance = {r.alliance: r for r in group}
    for row in group:
        if row.opponent is None or row.picked is None:
            continue
        other = by_alliance.get(row.opponent)
        if other is None or other.picked is None:
            continue
        pair = tuple(sorted((row.alliance, row.opponent)))
        if pair in seen:
            continue
        seen.add(pair)
        # Both sides picked to win, or both picked to lose: one is wrong.
        if row.picked == other.picked:
            verb = "win" if row.picked == "W" else "lose"
            out.append(
                Finding(
                    rule=7,
                    severity=SEVERITY_WARNING,
                    message=(
                        f"Both sides of this matchup are picked to {verb}. One of them is wrong."
                    ),
                    row_number=row.row_number,
                    column=COL_PICKED,
                    alliance=row.alliance,
                )
            )
    return out


#: How far below an alliance's own typical day score a value has to sit before
#: rule 8 asks about it. Deliberately loose: the check exists to catch a
#: missing unit (a `500` that meant `500m`), not to police a bad day.
SUSPECT_SCORE_RATIO = 1_000

#: Recorded day scores needed before rule 8 will call anything unusual. Below
#: this there is no baseline, only a guess.
SUSPECT_SCORE_MIN_SAMPLES = 3


def _check_day_score_magnitude(rows: Sequence[AllianceWeek]) -> list[Finding]:
    """Rule 8: a day score wildly out of line with that alliance's own others.

    Compares each alliance against **its own** recorded scores, never against
    an absolute floor. Day scores are read literally (see :func:`parse_score`),
    so the mistake worth catching is an established alliance typing `500` when
    they meant `500m`. An early-game alliance posting small numbers
    consistently is telling the truth, and a hardcoded plausibility threshold
    would nag exactly the alliances least able to tell it was wrong.

    Phrased as a question. It never refuses the value.
    """
    out: list[Finding] = []
    by_alliance: dict[AllianceKey, list[AllianceWeek]] = {}
    for row in rows:
        if row.day_scores:
            by_alliance.setdefault(row.alliance, []).append(row)

    for alliance, own_rows in by_alliance.items():
        scores = [s for r in own_rows for s in r.day_scores.values() if s > 0]
        if len(scores) < SUSPECT_SCORE_MIN_SAMPLES:
            continue
        typical = sorted(scores)[len(scores) // 2]
        if typical <= 0:
            continue
        for row in own_rows:
            for day, score in sorted(row.day_scores.items()):
                if score > 0 and typical // score >= SUSPECT_SCORE_RATIO:
                    out.append(
                        Finding(
                            rule=8,
                            severity=SEVERITY_WARNING,
                            message=(
                                f"Day {day} Score of {score:,} is far below this "
                                f"alliance's usual (about {typical:,}). "
                                f"Did you mean {score:,}m?"
                            ),
                            row_number=row.row_number,
                            column=day_score_col(day),
                            alliance=alliance,
                        )
                    )
    return out


def validate(
    rows: Iterable[AllianceWeek],
    *,
    tracking_mode: str = MODE_FULL_BRACKET,
    own_alliance: AllianceKey | None = None,
) -> list[Finding]:
    """Run every applicable check over a parsed sheet.

    `tracking_mode` decides whether the three bracket-shaped rules run. In
    own-alliance mode (#448) rules 4, 5 and 6 are skipped entirely, because a
    sheet holding only your own rows is a supported shape rather than a
    half-finished one, and reporting it as broken would be the tracker arguing
    with a choice its user made deliberately.

    Findings come back ordered by rule then row, so the report reads in sheet
    order rather than in whatever order the checks happened to run.
    """
    rows = list(rows)
    full_bracket = tracking_mode != MODE_OWN_ALLIANCE
    out: list[Finding] = []

    for row in rows:
        out += _check_day_outcomes_sum(row)
        out += _check_outcome_agrees(row)

    for _key, group in _league_weeks(rows).items():
        out += _check_week_score_pairs(group)
        out += _check_picked_agreement(group)
        if full_bracket:
            out += _check_reciprocal_opponents(group)

    if full_bracket:
        for league in {r.league for r in rows}:
            out += _check_seeds(rows, league)
            if own_alliance is not None:
                out += _check_own_alliance_present(rows, league, own_alliance)

    out += _check_day_score_magnitude(rows)

    return sorted(out, key=lambda f: (f.rule, f.row_number or 0, f.column))


# ── Skeleton rows ─────────────────────────────────────────────────────────────


def skeleton_rows(
    league: LeagueKey,
    week: int,
    week_date: _dt.date,
    alliances: Sequence[tuple[AllianceKey, int | None]],
    *,
    tracking_mode: str = MODE_FULL_BRACKET,
    own_alliance: AllianceKey | None = None,
) -> list[AllianceWeek]:
    """Empty rows stamped with league identity, week and seed, ready to fill.

    Setup is one sitting: the League screen shows all 16 alliances and their
    seeds at league start, so the bot writes the rows and the user fills tag,
    warzone and seed straight off that screen.

    **Branches on tracking mode** (#448). Full-bracket mode writes a row per
    alliance given. Own-alliance mode writes only the configured own
    alliance's rows, because that alliance chose not to track the other
    fifteen and generating them anyway would be the bot overriding the choice.
    """
    if tracking_mode == MODE_OWN_ALLIANCE:
        if own_alliance is None:
            return []
        alliances = [(key, seed) for key, seed in alliances if key == own_alliance]

    return [
        AllianceWeek(
            league=league,
            week=week,
            alliance=key,
            week_date=week_date,
            seed=seed,
            tag_display=key.tag.upper(),
            warzone_display=key.warzone,
        )
        for key, seed in alliances
    ]


def next_week_rows(rows: Iterable[AllianceWeek], week: int) -> list[AllianceWeek]:
    """Rows for the week after `week`, carried forward with predicted opponents.

    Once a week's outcomes are in, the next week's pairing follows from the
    rules, so the only thing left for a human to type is what actually
    happened. Season, tier, group, seed, tag and warzone come forward from the
    week just played; the Opponent column is filled with the **prediction**.

    Writing the prediction rather than leaving it blank is deliberate. If the
    game paired differently and the officer corrects it, that correction is
    itself the signal that the pairing algorithm needs a look, which a blank
    column would never produce.

    Returns ``[]`` when the pairing cannot be computed, which is the normal
    state before that week's results are recorded.
    """
    rows = list(rows)
    if week >= LEAGUE_WEEKS:
        return []

    # The prior week has to be *decided*, not merely present. With no outcomes
    # recorded, the weighted score is zero for everyone and the pairing falls
    # back to seed order, which would confidently reproduce week 1's matchups
    # for week 2. Sixteen rows carrying a wrong opponent are worse than none:
    # the whole reason the prediction is written is that a correction means
    # something, and it means nothing if the prediction was never informed.
    played = [r for r in rows if r.week == week]
    if not played or any(r.week_outcome is None for r in played):
        return []

    pairing = compute_week_pairing(rows, week + 1)
    if isinstance(pairing, BracketIncomplete):
        return []

    opponents: dict[AllianceKey, AllianceKey] = {}
    for match in pairing.matches:
        opponents[match.a] = match.b
        opponents[match.b] = match.a

    source = {r.alliance: r for r in played}
    previous_date = next((r.week_date for r in source.values() if r.week_date), None)
    week_date = previous_date + _dt.timedelta(weeks=1) if previous_date else None

    out = []
    for alliance, row in sorted(source.items(), key=lambda kv: kv[1].seed or BRACKET_SIZE + 1):
        out.append(
            AllianceWeek(
                league=row.league,
                week=week + 1,
                alliance=alliance,
                week_date=week_date,
                seed=row.seed,
                tag_display=row.tag_display,
                warzone_display=row.warzone_display,
                opponent=opponents.get(alliance),
            )
        )
    return out


def latest_league(rows: Iterable[AllianceWeek]) -> LeagueKey | None:
    """The league the alliance is currently in, by newest recorded Week Date.

    Falls back to the last league seen when no row carries a date, so a
    part-filled sheet still resolves to something rather than nothing.
    """
    rows = list(rows)
    dated = [r for r in rows if r.week_date]
    if dated:
        return max(dated, key=lambda r: r.week_date).league
    return rows[-1].league if rows else None


def missing_bracket_rows(
    rows: Iterable[AllianceWeek], league: LeagueKey
) -> dict[int, tuple[int, _dt.date | None]]:
    """How many rows short of a full bracket each recorded week of `league` is.

    Returns ``{week: (rows_needed, week_date)}``, skipping weeks that are
    already full. Only weeks that already exist are counted: an alliance that
    has recorded two weeks is not asking for four.

    This is what makes switching from own-alliance to full-bracket tracking
    mid-league cheap (#448). The bot cannot know who the other fifteen
    alliances are, so the rows it offers to add are blank placeholders already
    stamped with season, tier, group, week and date. What that saves is
    retyping the league identity fifteen times per week, not the scouting.
    """
    rows = list(rows)
    out: dict[int, tuple[int, _dt.date | None]] = {}
    weeks = sorted({r.week for r in rows if r.league == league})
    for week in weeks:
        in_week = [r for r in rows if r.league == league and r.week == week]
        have = len({r.alliance for r in in_week})
        if have >= BRACKET_SIZE:
            continue
        stamp = next((r.week_date for r in in_week if r.week_date), None)
        out[week] = (BRACKET_SIZE - have, stamp)
    return out


def blank_bracket_values(
    header: Sequence[str],
    league: LeagueKey,
    week: int,
    week_date: _dt.date | None,
) -> list[str]:
    """One blank placeholder row, stamped with league identity and week only.

    Deliberately carries no Tag or Warzone: those come off the in-game bracket
    screen and the bot has no way to know them. Written against the live
    header so a reordered or extended sheet still lines up.
    """
    line = [""] * len(header)
    hidx = transfer.header_index(list(header))
    stamped = {
        COL_SEASON: league.season,
        COL_TIER: league.tier,
        COL_GROUP: league.group,
        COL_WEEK: str(week),
    }
    if week_date:
        stamped[COL_WEEK_DATE] = week_date.isoformat()
    for name, value in stamped.items():
        idx = hidx.get(transfer.norm_header(name))
        if idx is not None and idx < len(line):
            line[idx] = value
    return line


def week_one_pairing_from_seeds(
    alliances: Sequence[tuple[AllianceKey, int | None]],
) -> dict[AllianceKey, AllianceKey]:
    """Week 1 opponents, straight off the seeds: (1,2)(3,4)…(15,16).

    Week 1 needs no results to pair, so the bot fills Opponent at setup rather
    than waiting for a result. Returns the mapping both ways. Alliances with no
    seed are left out rather than guessed at.
    """
    seeded = sorted(((k, s) for k, s in alliances if s is not None), key=lambda p: p[1])
    out: dict[AllianceKey, AllianceKey] = {}
    for i in range(0, len(seeded) - 1, 2):
        a, b = seeded[i][0], seeded[i + 1][0]
        out[a] = b
        out[b] = a
    return out

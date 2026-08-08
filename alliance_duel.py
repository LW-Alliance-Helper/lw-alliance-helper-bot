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
from typing import Callable, Iterable, Sequence

import transfer
from storm_date_helpers import parse_event_date

logger = logging.getLogger(__name__)


# ── Fixed game constants ──────────────────────────────────────────────────────
#
# Day themes and the Monday-to-Saturday schedule are fixed always, so they live
# as a module-level table like the canonical DS/CS zone names, not as guild
# config. Point values should be spot-checked each season in case an update
# shifts them.


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
COL_SERVER = "Server"
COL_NAME = "Name"
COL_POWER = "Power"
COL_MEMBERS = "Members"
COL_GIFT_LEVEL = "Gift Level"
COL_OPPONENT_TAG = "Opponent Tag"
COL_OPPONENT_SERVER = "Opponent Server"
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
    COL_SERVER,
    COL_NAME,
    COL_POWER,
    COL_MEMBERS,
    COL_GIFT_LEVEL,
    COL_OPPONENT_TAG,
    COL_OPPONENT_SERVER,
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
    a bare number is taken **literally**. Duel scores span orders of magnitude —
    a day-1 radar total and a day-2 speedup dump aren't in the same range — so
    there's no safe implicit magnitude to apply, and silently reading ``301`` as
    301 million would poison the day profiles.
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
    """An alliance's dedup identity: tag plus server.

    Both are normalised (trimmed, casefolded, ``#`` stripped from the tag) so
    ``[ABC] 1234`` and ``abc/1234`` are the same alliance. The display forms
    live on :class:`AllianceWeek`.
    """

    tag: str
    server: str

    @staticmethod
    def of(tag, server) -> "AllianceKey | None":
        t = re.sub(r"[\[\]#\s]", "", str(tag or "")).casefold()
        s = re.sub(r"[^0-9a-z]", "", str(server or "").casefold())
        if not t or not s:
            return None
        return AllianceKey(t, s)

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"[{self.tag.upper()}] {self.server}"


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
    server_display: str = ""

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
        alliance = AllianceKey.of(cell(COL_TAG), cell(COL_SERVER))
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
                server_display=cell(COL_SERVER) or "",
                power=parse_power(cell(COL_POWER)),
                members=parse_int(cell(COL_MEMBERS)),
                gift_level=parse_int(cell(COL_GIFT_LEVEL)),
                opponent=AllianceKey.of(cell(COL_OPPONENT_TAG), cell(COL_OPPONENT_SERVER)),
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
        COL_SERVER: row.server_display or row.alliance.server,
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
        out[COL_OPPONENT_SERVER] = row.opponent.server
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
    Server) — and updates only that row's own cells, appending when absent. A
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
    resolver = _MatchResolver(rows, estimate, blocked)
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
    ) -> None:
        self._estimate = estimate
        self._blocked = blocked
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

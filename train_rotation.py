"""
train_rotation.py — Train Conductor Rotation (issue #55).

Two layers live here:

1. **Pure logic** — the selection algorithm, day-rule / member-rule
   application, rotation counting, and weekly-draft generation. These take
   already-loaded data structures (lists of dataclasses, member-name pools)
   so they unit-test without touching Google Sheets or Discord.

2. **Sheet I/O** — load / save for the three alliance-owned tabs that back
   the feature (Train History, Train Member Rules, Train Day Rules). The
   write strategy mirrors storm_strategy.py: read every row, keep the ones
   that don't belong to what we're rewriting, append the new rows. The
   gspread client is reused via `config.get_spreadsheet` /
   `config.get_or_create_worksheet`.

Free tier — there is no premium gating anywhere in this module. Rotation
tracking is baseline alliance management; see #55's tier-strategy note.

The selection algorithm is deliberately **deterministic**: the `random`
day rule means "pick the next person by rotation fairness", not a coin
flip. Equal rotation counts break by oldest last-driven date, then by name,
so the same inputs always produce the same draft (which is what makes it
testable and what makes the weekly draft reproducible if regenerated).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

# ── Day-of-week ──────────────────────────────────────────────────────────────
# Index matches datetime.date.weekday(): 0 = Monday … 6 = Sunday. Stored in the
# Sheet as the weekday NAME for human readability, parsed back via this list.
WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]


def weekday_index(name: str) -> int | None:
    """Map a weekday name (any case, full or 3-letter) to its 0-6 index."""
    n = (name or "").strip().lower()
    for i, full in enumerate(WEEKDAY_NAMES):
        if full.lower() == n or full.lower()[:3] == n[:3] and n:
            return i
    return None


# ── Day rule types ───────────────────────────────────────────────────────────
# `auto` (was "random") is the fair-rotation pick — it's an automatic action,
# not a random draw, hence the name. `manual` means the bot doesn't pick at all;
# leadership assigns the day themselves (and is prompted to).
RULE_AUTO = "auto"
RULE_LEADERSHIP = "leadership"
RULE_MANUAL = "manual"
RULE_BIRTHDAY = "birthday"
RULE_VS = "vs"
RULE_CONTEST = "contest"
RULE_EVENT = "event"
RULE_SPECIFIC = "specific_member"

# Rule types an alliance can pick per day in the preset editor. `birthday` is
# deliberately NOT here — birthday handling is global (override / disabled),
# derived from the Birthday setup, not a per-day rule. RULE_BIRTHDAY survives as
# a constant for the reason tag + the override pre-pass's DraftDay.rule_type.
DAY_RULE_TYPES = [
    RULE_AUTO,
    RULE_LEADERSHIP,
    RULE_MANUAL,
    RULE_VS,
    RULE_CONTEST,
    RULE_EVENT,
    RULE_SPECIFIC,
]

# Human labels for embeds / select menus. Includes `birthday` for rendering the
# reason/rule on draft rows even though it isn't a selectable day rule.
RULE_LABELS = {
    RULE_AUTO: "Auto (fair rotation)",
    RULE_LEADERSHIP: "Leadership role",
    RULE_MANUAL: "Manual (you assign)",
    RULE_BIRTHDAY: "Birthday",
    RULE_VS: "VS (you assign)",
    RULE_CONTEST: "Contest (you assign)",
    RULE_EVENT: "Event (you assign)",
    RULE_SPECIFIC: "Specific member",
}

# ── Auto vs manual is derived, not stored ────────────────────────────────────
# There is no separate strategy axis (dropped per Kevin). Whether a day
# auto-picks or "needs picking" falls out of the rule type + whether a pool
# resolves:
#   - auto        → automatic fair rotation over the full roster
#   - leadership  → auto from the leadership role (needs picking if unset/empty)
#   - manual      → never auto-picks; leadership assigns it day-of (prompted)
#   - vs/contest/event → auto from an assigned role if one exists, else MANUAL
#   - specific_member  → the pinned member (fixed)
# These rules need an assigned role to auto-pick; without one they're manual.
ROLE_REQUIRED_RULES = {RULE_VS, RULE_CONTEST, RULE_EVENT}

# Rule types that are manual by design — the bot never auto-picks; leadership
# assigns the day (and the daily confirmation prompts them). vs/contest/event
# fall in here only when no role is assigned (handled in select_conductor).
MANUAL_RULES = {RULE_MANUAL, RULE_VS, RULE_CONTEST, RULE_EVENT}


# ── Reason taxonomy (fixed list, v1 — see #55) ───────────────────────────────
# Tags written to Train History so the rotation count can exclude bonus /
# contextual assignments (a birthday or welcome train shouldn't count against
# the recipient's fairness rotation).
REASONS = [
    "vs",
    "contest",
    "birthday",
    "welcome",
    "leadership",
    "auto",
    "manual",
    "event",
    "other",
]

# Counted toward rotation by default. `birthday`, `welcome`, `event` are
# excluded — they're bonus/contextual, not a fair-rotation turn. Alliances can
# override the counted set in the setup wizard.
DEFAULT_COUNTED_REASONS = ["vs", "contest", "leadership", "auto", "manual", "other"]
DEFAULT_NON_COUNTED_REASONS = ["birthday", "welcome", "event"]

# Map a day rule type → the reason recorded when that rule picks someone.
RULE_TO_REASON = {
    RULE_AUTO: "auto",
    RULE_LEADERSHIP: "leadership",
    RULE_MANUAL: "manual",
    RULE_BIRTHDAY: "birthday",
    RULE_VS: "vs",
    RULE_CONTEST: "contest",
    RULE_EVENT: "event",
    # A pinned recurring slot is not a rotation pick; record it as `other`
    # (counted by default, so a member pinned every Wednesday isn't also
    # over-picked on auto days).
    RULE_SPECIFIC: "other",
}

# ── Train History status values ──────────────────────────────────────────────
# A day is either still in the draft (scheduled) or confirmed + announced
# (posted). There's no "skipped" status: to not run a train, leave the day
# manual and don't confirm the prompt (Option A, Kevin).
STATUS_SCHEDULED = "scheduled"  # in the current week's draft, not yet confirmed
STATUS_POSTED = "posted"  # confirmed and announced publicly

# ── Member rule types ────────────────────────────────────────────────────────
MEMBER_RULE_OPT_OUT = "opt_out"  # never include in selection
MEMBER_RULE_SKIP_UNTIL = "skip_until"  # exclude until the date in `value`
MEMBER_RULE_TYPES = [MEMBER_RULE_OPT_OUT, MEMBER_RULE_SKIP_UNTIL]

# ── Birthday integration modes ───────────────────────────────────────────────
# Two modes ship in v1 (Kevin, #55): `override` (ANY birthday outranks the
# rotation, placed on/around the day) and `disabled` (birthdays don't drive
# trains — the default, since birthdays can be configured for announcements
# without enabling them for trains). A `dedicated_day` mode was considered and
# deferred unless an alliance asks for it.
BIRTHDAY_OVERRIDE = "override"  # birthday preempts whatever rule is on that day
BIRTHDAY_DISABLED = "disabled"  # birthdays don't drive train selection (default)
BIRTHDAY_MODES = [BIRTHDAY_OVERRIDE, BIRTHDAY_DISABLED]

# ── Sheet headers ────────────────────────────────────────────────────────────
HISTORY_HEADER = ["Date", "Member", "Reason", "Status", "Posted At", "Notes"]
MEMBER_RULES_HEADER = ["Member", "Rule Type", "Value", "Notes"]
DAY_RULES_HEADER = [
    "Preset Name",
    "Day of Week",
    "Rule Type",
    "Specific Member",
    "Notes",
]

DEFAULT_PRESET_NAME = "Standard Week"

# Rendered in the weekly draft / daily reminder wherever a day has no
# auto-resolved conductor (manual rule, exhausted pool, or no roster). Copy is
# verbatim per Kevin — leadership must pick by hand for that day.
NEEDS_PICKING_LABEL = "⚠️ Requires selection. Assign manually."


def parse_counted_reasons(raw: str | None) -> set[str]:
    """Parse the stored comma-separated counted-reasons string into a set.
    Empty / unset → the default counted set."""
    if not raw or not raw.strip():
        return set(DEFAULT_COUNTED_REASONS)
    parsed = {r.strip().lower() for r in raw.split(",") if r.strip()}
    return parsed or set(DEFAULT_COUNTED_REASONS)


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class DayRule:
    """One day's rule inside a schedule preset."""

    weekday: int  # 0 = Monday … 6 = Sunday
    rule_type: str = RULE_AUTO
    specific_member: str = ""
    notes: str = ""


@dataclass
class SchedulePreset:
    """A named weekly pattern: exactly one DayRule per weekday."""

    name: str
    days: dict[int, DayRule] = field(default_factory=dict)

    @classmethod
    def default(cls, name: str = DEFAULT_PRESET_NAME) -> "SchedulePreset":
        """A fresh preset with every day `random`."""
        return cls(name=name, days={wd: DayRule(weekday=wd) for wd in range(7)})

    def rule_for(self, weekday: int) -> DayRule:
        """The rule for a weekday, defaulting to random if unset."""
        return self.days.get(weekday) or DayRule(weekday=weekday)


@dataclass
class HistoryRow:
    """One row of the Train History tab."""

    date: str  # ISO YYYY-MM-DD
    member: str
    reason: str
    status: str
    posted_at: str = ""
    notes: str = ""


@dataclass
class MemberRule:
    """One row of the Train Member Rules tab."""

    member: str
    rule_type: str
    value: str = ""
    notes: str = ""


@dataclass
class DraftDay:
    """One generated day in a weekly draft (not yet persisted)."""

    date: str  # ISO YYYY-MM-DD
    weekday: int
    rule_type: str
    member: str | None  # None → needs picking / no eligible conductor
    reason: str
    needs_picking: bool = False
    note: str = ""  # surfaced in the draft embed, e.g. "birthday 🎂"


# ── Small helpers ────────────────────────────────────────────────────────────


def _norm(name: str) -> str:
    """Normalise a member name for case/whitespace-insensitive matching.

    History rows and roster pools come from different surfaces (a hand-typed
    Sheet cell vs. a synced display name), so all count / dedup lookups key on
    the normalised form while the original display string is preserved for
    output."""
    return (name or "").strip().lower()


def _truthy(value: str, *, default: bool) -> bool:
    """Interpret a Sheet cell as a boolean. Empty → `default`."""
    v = (value or "").strip().lower()
    if v == "":
        return default
    return v not in ("false", "0", "no", "n", "off")


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except (ValueError, AttributeError):
        return None


# ── Rotation counting ────────────────────────────────────────────────────────


def rotation_counts(history: list[HistoryRow], counted_reasons: set[str]) -> dict[str, int]:
    """How many counted, posted trains each member has driven.

    Keyed by normalised name. Only `posted` rows whose reason is in
    `counted_reasons` count — that's what excludes birthday / welcome / event
    trains from the fairness rotation."""
    counts: dict[str, int] = {}
    for row in history:
        if row.status != STATUS_POSTED:
            continue
        if row.reason not in counted_reasons:
            continue
        key = _norm(row.member)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def last_driven_dates(history: list[HistoryRow]) -> dict[str, str]:
    """Most recent `posted` date per member (any reason), keyed by norm name.

    Uses ALL posted rows — including non-counted reasons — for the tie-break,
    because a member who drove a birthday train yesterday has still *driven*
    recently and shouldn't be first in line again over someone who hasn't."""
    last: dict[str, str] = {}
    for row in history:
        if row.status != STATUS_POSTED:
            continue
        key = _norm(row.member)
        if not key or not row.date:
            continue
        if key not in last or row.date > last[key]:
            last[key] = row.date
    return last


# ── Member-rule application ──────────────────────────────────────────────────


def is_blocked_by_member_rule(member: str, member_rules: list[MemberRule], today: date) -> bool:
    """True if a member-level rule excludes this member from selection today.

    `opt_out` excludes always (unless the Value cell explicitly reads false).
    `skip_until` excludes while `today` is before the stored date — the member
    becomes eligible again on the skip_until date itself."""
    key = _norm(member)
    for r in member_rules:
        if _norm(r.member) != key:
            continue
        if r.rule_type == MEMBER_RULE_OPT_OUT and _truthy(r.value, default=True):
            return True
        if r.rule_type == MEMBER_RULE_SKIP_UNTIL:
            until = _parse_iso(r.value)
            if until and today < until:
                return True
    return False


# ── Core selection ───────────────────────────────────────────────────────────


def _lowest_rotation(
    candidates: list[str],
    history: list[HistoryRow],
    counted_reasons: set[str],
) -> str:
    """Pick the fairest candidate: lowest rotation count, ties broken by oldest
    last-driven date, then by name for determinism. Never-driven members sort
    first (count 0, empty last-driven date)."""
    counts = rotation_counts(history, counted_reasons)
    last = last_driven_dates(history)

    def sort_key(m: str):
        key = _norm(m)
        # last.get → "" for never-driven, which sorts before any ISO date.
        return (counts.get(key, 0), last.get(key, ""), key)

    return min(candidates, key=sort_key)


def select_conductor(
    day_rule: DayRule,
    *,
    target_date: date,
    eligible_pool: list[str],
    role_pools: dict[str, list[str]],
    member_rules: list[MemberRule],
    history: list[HistoryRow],
    counted_reasons: set[str],
    already_scheduled: set[str],
) -> tuple[str | None, str, bool]:
    """Resolve a single day's conductor.

    Returns `(member_or_None, reason, needs_picking)`:
      - `member` is None when the rule resolves to no pool (manual), the pool
        is empty after filtering, or a `specific_member` pin is missing.
      - `reason` is the Train History reason tag for the pick.
      - `needs_picking` is True whenever leadership must choose by hand.

    Pool resolution (#55 — auto vs manual is derived, no strategy axis):
      - `random` → the full roster (`eligible_pool`).
      - `leadership` → `role_pools["leadership"]` (the leadership role, defaulting
        to the alliance's main leadership role); no resolvable role → manual.
      - `vs` / `contest` / `event` → their assigned role in `role_pools` if one
        is set; with no role assigned the day is MANUAL (leadership picks).
      - `specific_member` → the pinned member.

    Birthdays are handled by the weekly-draft pre-pass, not here.
    `already_scheduled` is the set of normalised names already placed earlier
    in the same week — the no-duplicates-this-week rule."""
    # specific_member is a pinned slot — always that member. A missing pin
    # surfaces as "needs picking".
    if day_rule.rule_type == RULE_SPECIFIC:
        pinned = (day_rule.specific_member or "").strip()
        if pinned:
            return pinned, RULE_TO_REASON[RULE_SPECIFIC], False
        return None, RULE_TO_REASON[RULE_SPECIFIC], True

    reason = RULE_TO_REASON.get(day_rule.rule_type, "auto")
    rt = day_rule.rule_type

    if rt == RULE_MANUAL:
        # Always manual — leadership assigns the day themselves (prompted).
        return None, reason, True
    elif rt == RULE_LEADERSHIP:
        # Leadership requires a resolvable leadership role pool; empty → manual.
        pool = list(role_pools.get(RULE_LEADERSHIP, []))
    elif rt in ROLE_REQUIRED_RULES:
        # vs / contest / event: a role must be assigned to auto-pick; with no
        # role the day is manual (leadership picks by hand).
        if rt not in role_pools:
            return None, reason, True
        pool = list(role_pools[rt])
    else:
        # auto (and any unknown rule) → full roster, fair rotation.
        pool = list(eligible_pool)

    # Filter opted-out / skip_until members — these are hard exclusions.
    eligible = [m for m in pool if not is_blocked_by_member_rule(m, member_rules, target_date)]

    # No-duplicates-this-week is a SOFT rule: prefer members not yet placed this
    # week, but if the pool is exhausted (more days than people), wrap around and
    # reuse the pool rather than leave the day unfilled. The day only "needs
    # picking" when the eligible pool is genuinely empty (everyone opted out, or
    # an empty roster/role).
    fresh = [m for m in eligible if _norm(m) not in already_scheduled]
    candidates = fresh or eligible
    if not candidates:
        return None, reason, True

    return _lowest_rotation(candidates, history, counted_reasons), reason, False


# ── Weekly draft generation ──────────────────────────────────────────────────


def _place_birthdays(
    week_isos: list[str],
    birthdays_on_date: dict[str, list[str]],
    occupied: dict[str, str],
    already: set[str],
) -> dict[str, DraftDay]:
    """Override-mode birthday pre-pass.

    Places each birthday member on their exact day, or the day before / day
    after when the exact day is already taken (by a `specific_member` pin or an
    earlier-placed birthday) — mirroring the existing birthday→train placement.
    ANY birthday outranks the rotation. A member already placed (e.g. a pinned
    member who also has a birthday) is skipped so nobody is booked twice.

    Processed in (date, name) order for determinism. Mutates `occupied` and
    `already`; returns the birthday DraftDays keyed by ISO date."""
    week_set = set(week_isos)
    # Flatten to (iso, member), de-duplicated by member, sorted deterministically.
    bdays: list[tuple[str, str]] = []
    seen: set[str] = set()
    for iso in week_isos:
        for member in sorted(birthdays_on_date.get(iso, []), key=_norm):
            if _norm(member) not in seen:
                seen.add(_norm(member))
                bdays.append((iso, member))
    bdays.sort(key=lambda t: (t[0], _norm(t[1])))

    out: dict[str, DraftDay] = {}
    for iso, member in bdays:
        if _norm(member) in already:
            continue  # already pinned elsewhere this week — don't double-book
        target = date.fromisoformat(iso)
        for delta in (0, -1, 1):  # on the day, then day before, then day after
            cand = (target + timedelta(days=delta)).isoformat()
            if cand in week_set and cand not in occupied:
                note = "birthday 🎂"
                if delta == -1:
                    note = "birthday 🎂 (day before)"
                elif delta == 1:
                    note = "birthday 🎂 (day after)"
                out[cand] = DraftDay(
                    date=cand,
                    weekday=date.fromisoformat(cand).weekday(),
                    rule_type=RULE_BIRTHDAY,
                    member=member,
                    reason="birthday",
                    needs_picking=False,
                    note=note,
                )
                occupied[cand] = member
                already.add(_norm(member))
                break
        # No free slot in the ±1 window → this member just isn't on the train
        # this week. The birthday announcement feature still shouts them out.
    return out


def generate_week_draft(
    preset: SchedulePreset,
    week_start: date,
    *,
    eligible_pool: list[str],
    role_pools: dict[str, list[str]] | None = None,
    member_rules: list[MemberRule],
    history: list[HistoryRow],
    counted_reasons: set[str],
    birthday_mode: str = BIRTHDAY_DISABLED,
    birthdays_on_date: dict[str, list[str]] | None = None,
) -> list[DraftDay]:
    """Generate a 7-day draft (Mon→Sun) from a preset and current state.

    `week_start` must be the Monday of the target week. Order of resolution:

      1. `specific_member` pins are placed first — they're fixed assignments
         that birthdays route around (treated as "taken").
      2. In `override` birthday mode, birthdays are placed on / around their day
         (see `_place_birthdays`); they outrank the rotation and don't count
         toward fairness, and the member is excluded from the rotation fill so
         they're never on the schedule twice in one week.
      3. Every remaining day is filled by the rotation algorithm.

    `birthdays_on_date` maps ISO date → member names with a birthday that day;
    only consulted in `override` mode."""
    birthdays_on_date = birthdays_on_date or {}
    role_pools = role_pools or {}
    counted = set(counted_reasons)
    week_isos = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]

    results: dict[str, DraftDay] = {}
    occupied: dict[str, str] = {}  # iso → member (for birthday around-the-day)
    already: set[str] = set()  # normalised names placed this week (no-dupes)

    # 1. specific_member pins — fixed, placed before birthdays so they're "taken".
    for iso in week_isos:
        wd = date.fromisoformat(iso).weekday()
        rule = preset.rule_for(wd)
        if rule.rule_type == RULE_SPECIFIC and (rule.specific_member or "").strip():
            pinned = rule.specific_member.strip()
            results[iso] = DraftDay(
                date=iso,
                weekday=wd,
                rule_type=RULE_SPECIFIC,
                member=pinned,
                reason=RULE_TO_REASON[RULE_SPECIFIC],
                needs_picking=False,
            )
            occupied[iso] = pinned
            already.add(_norm(pinned))

    # 2. Birthday pre-pass (override mode only).
    if birthday_mode == BIRTHDAY_OVERRIDE:
        results.update(_place_birthdays(week_isos, birthdays_on_date, occupied, already))

    # 3. Rotation fill for every still-empty day.
    for iso in week_isos:
        if iso in results:
            continue
        d = date.fromisoformat(iso)
        rule = preset.rule_for(d.weekday())
        member, reason, needs = select_conductor(
            rule,
            target_date=d,
            eligible_pool=eligible_pool,
            role_pools=role_pools,
            member_rules=member_rules,
            history=history,
            counted_reasons=counted,
            already_scheduled=already,
        )
        if member:
            already.add(_norm(member))
        results[iso] = DraftDay(
            date=iso,
            weekday=d.weekday(),
            rule_type=rule.rule_type,
            member=member,
            reason=reason,
            needs_picking=needs,
            note="" if member else "needs picking",
        )

    return [results[iso] for iso in week_isos]


def reroll_day(
    draft_day: DraftDay,
    *,
    eligible_pool: list[str],
    role_pools: dict[str, list[str]] | None = None,
    member_rules: list[MemberRule],
    history: list[HistoryRow],
    counted_reasons: set[str],
    other_scheduled: set[str],
    target_date: date,
) -> tuple[str | None, str, bool]:
    """Suggest the next fair conductor for a single day (the draft's [⏭️ Next]
    button). Picks from the rule's role if one is scoped (leadership / vs /
    contest / event with a role), otherwise from the full roster — so even a
    no-role "manual" day still gets a lowest-rotation starting suggestion.
    `other_scheduled` is the rest of the week's placements minus this day."""
    role_pools = role_pools or {}
    rt = draft_day.rule_type
    if rt == RULE_LEADERSHIP and role_pools.get(RULE_LEADERSHIP):
        pool = list(role_pools[RULE_LEADERSHIP])
    elif rt in ROLE_REQUIRED_RULES and role_pools.get(rt):
        pool = list(role_pools[rt])
    else:
        pool = list(eligible_pool)
    reason = RULE_TO_REASON.get(rt, "auto")
    eligible = [m for m in pool if not is_blocked_by_member_rule(m, member_rules, target_date)]
    # Soft no-duplicates: prefer someone not elsewhere this week, but wrap around
    # the pool when it's exhausted rather than refuse to suggest anyone.
    fresh = [m for m in eligible if _norm(m) not in other_scheduled]
    candidates = fresh or eligible
    if not candidates:
        return None, reason, True
    return _lowest_rotation(candidates, history, counted_reasons), reason, False


def leaderboard(history: list[HistoryRow], counted_reasons: set[str]) -> list[tuple[str, int, str]]:
    """Build a leaderboard: (member, count, last_driven_iso) sorted by count
    desc then most-recent. Member names are the display form from history."""
    counted = set(counted_reasons)
    counts: dict[str, int] = {}
    last: dict[str, str] = {}
    display: dict[str, str] = {}  # norm → first-seen display name
    for row in history:
        if row.status != STATUS_POSTED:
            continue
        key = _norm(row.member)
        if not key:
            continue
        display.setdefault(key, row.member)
        if row.reason in counted:
            counts[key] = counts.get(key, 0) + 1
        if row.date and (key not in last or row.date > last[key]):
            last[key] = row.date
    rows = [(display[k], counts.get(k, 0), last.get(k, "")) for k in display]
    rows.sort(key=lambda t: (-t[1], t[2] or "", t[0].lower()))
    return rows


def member_tally(
    roster_names: list[str],
    history: list[HistoryRow],
    counted_reasons: set[str],
    member_rules: list[MemberRule],
    today: date,
) -> list[tuple[str, int, str]]:
    """By-member fairness rows: (display_name, counted_train_count, last_iso).

    The universe is everyone who has actually driven (appears in posted history,
    via `leaderboard`) UNION every currently-eligible roster member. The roster
    union is what lets a member who has driven *zero* times — and therefore has
    no history rows at all — still surface (count 0, last ""), which is the whole
    point of the "fewest trains" view. Opted-out / skip_until members are omitted
    *unless* they already appear in history (they did drive, so the record of it
    stays honest). Unsorted; callers pick a sort via `sort_tally`."""
    rows = list(leaderboard(history, counted_reasons))
    seen = {_norm(name) for (name, _c, _l) in rows}
    for name in roster_names:
        key = _norm(name)
        if not key or key in seen:
            continue
        if is_blocked_by_member_rule(name, member_rules, today):
            continue
        rows.append((name, 0, ""))
        seen.add(key)
    return rows


# Sort keys for the by-member tally. Python's sort is stable, so each sort is
# built by applying the least-significant key first and the primary key last.
TALLY_SORT_MOST = "most"
TALLY_SORT_FEWEST = "fewest"
TALLY_SORT_LONGEST_SINCE = "longest_since"
TALLY_SORT_NAME = "name"


def sort_tally(rows: list[tuple[str, int, str]], key: str) -> list[tuple[str, int, str]]:
    """Return a sorted copy of a `member_tally` list for the given sort key.

    An empty last-driven date ("") sorts as "longest ago / never", so never-driven
    members rise to the top of both `fewest` and `longest_since`."""
    out = sorted(rows, key=lambda t: t[0].lower())  # tertiary: name A->Z
    if key == TALLY_SORT_FEWEST:
        out.sort(key=lambda t: t[2] or "")  # secondary: oldest/never first
        out.sort(key=lambda t: t[1])  # primary: fewest trains first
    elif key == TALLY_SORT_LONGEST_SINCE:
        out.sort(key=lambda t: t[1])  # secondary: fewest trains first
        out.sort(key=lambda t: t[2] or "")  # primary: oldest/never first
    elif key == TALLY_SORT_NAME:
        pass  # name A->Z already applied
    else:  # TALLY_SORT_MOST (default)
        out.sort(key=lambda t: t[2] or "", reverse=True)  # secondary: most recent first
        out.sort(key=lambda t: t[1], reverse=True)  # primary: most trains first
    return out


def sort_posted(posted: list[HistoryRow], *, newest_first: bool = True) -> list[HistoryRow]:
    """Chronological sort of posted history rows for the by-date log."""
    return sorted(posted, key=lambda h: h.date or "", reverse=newest_first)


# ══════════════════════════════════════════════════════════════════════════════
# Sheet I/O
# ══════════════════════════════════════════════════════════════════════════════
#
# All three tabs live in the alliance's own Google Sheet. The client + the
# get-or-create helper are reused from config so the rotation tabs behave like
# every other bot-managed tab (auto-created with a header row on first use).


def _open_tab(guild_id: int, tab_name: str, header: list[str]):
    """Return the worksheet for `tab_name`, creating it with `header` if absent.
    Returns None when the guild has no Sheet configured or gspread errored."""
    import config

    if not tab_name:
        return None
    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        print(f"[TRAIN ROTATION] get_spreadsheet failed for guild {guild_id}: {e}")
        return None
    if sh is None:
        return None
    try:
        return config.get_or_create_worksheet(
            sh,
            tab_name,
            header_row=header,
            rows=2000,
            cols=max(8, len(header)),
        )
    except Exception as e:
        print(f"[TRAIN ROTATION] open/create tab {tab_name!r} failed for guild {guild_id}: {e}")
        return None


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if len(row) > idx else ""


# ── Eligible-member roster ───────────────────────────────────────────────────
#
# The selection pool comes from the alliance's Member Roster tab (the same
# source storm uses) — decided with Kevin. The Display Name column is preferred,
# falling back to the Name column when Display Name is blank (the 1.4.3 roster
# name-fallback pattern). No roster configured → empty pool → every auto day
# renders NEEDS_PICKING_LABEL, which is the graceful degradation we want.


def load_roster_members(guild_id: int) -> list[dict]:
    """Read the Member Roster tab → [{"name": str, "discord_id": str}, ...].

    Returns [] when member-roster config is missing or the Sheet read fails.
    Name resolution prefers Display Name, falls back to Name when blank."""
    import config

    try:
        rcfg = config.get_member_roster_config(guild_id)
    except Exception as e:
        print(f"[TRAIN ROTATION] roster-config read failed for guild {guild_id}: {e}")
        return []

    tab_name = rcfg.get("tab_name") or "Member Roster"
    name_col = int(rcfg.get("name_col", 1))
    display_col = int(rcfg.get("display_col", 2))
    id_col = int(rcfg.get("discord_id_col", 0))

    try:
        ws = config.get_member_roster_sheet(guild_id, tab_name)
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] roster read failed for guild {guild_id}: {e}")
        return []

    out: list[dict] = []
    for row in values[1:]:  # row 1 is the header
        display = _cell(row, display_col)
        name = display or _cell(row, name_col)
        if not name:
            continue
        out.append({"name": name, "discord_id": _cell(row, id_col)})
    return out


def roster_names(roster: list[dict]) -> list[str]:
    """The full eligible pool — every roster member's name."""
    return [m["name"] for m in roster if m.get("name")]


def role_pool_from_roster(roster: list[dict], role_discord_ids: set[str]) -> list[str]:
    """Roster members whose Discord ID carries a given role.

    Resolved against the roster (not raw Discord display names) so the names
    stay consistent with what's written to Train History. Used to build the
    per-rule-type candidate pools (leadership / vs / contest / event roles)."""
    ids = {str(i) for i in role_discord_ids}
    return [m["name"] for m in roster if m.get("name") and str(m.get("discord_id") or "") in ids]


# ── Train History ────────────────────────────────────────────────────────────


def load_history(guild_id: int, tab_name: str) -> list[HistoryRow]:
    """Load every Train History row. Returns [] on any read failure (the
    callers degrade gracefully — an empty history just means everyone starts
    at rotation count zero)."""
    ws = _open_tab(guild_id, tab_name, HISTORY_HEADER)
    if ws is None:
        return []
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] load_history read failed for guild {guild_id}: {e}")
        return []
    out: list[HistoryRow] = []
    for row in values[1:]:
        if not row or not _cell(row, 0):
            continue
        out.append(
            HistoryRow(
                date=_cell(row, 0),
                member=_cell(row, 1),
                reason=_cell(row, 2).lower(),
                status=_cell(row, 3).lower(),
                posted_at=_cell(row, 4),
                notes=_cell(row, 5),
            )
        )
    return out


def _history_to_row(h: HistoryRow) -> list[str]:
    return [h.date, h.member, h.reason, h.status, h.posted_at, h.notes]


def write_draft_rows(guild_id: int, tab_name: str, draft: list[DraftDay]) -> bool:
    """Persist a weekly draft as `scheduled` history rows.

    Replaces any existing rows whose date falls inside the draft's date set
    (so re-running the draft for the same week overwrites cleanly) and keeps
    every other row. Days that still need picking are written with an empty
    member so the draft viewer can render the ⚠️ marker after a restart."""
    ws = _open_tab(guild_id, tab_name, HISTORY_HEADER)
    if ws is None:
        return False
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] write_draft read-back failed for guild {guild_id}: {e}")
        return False

    draft_dates = {dd.date for dd in draft}
    kept = [row for row in values[1:] if _cell(row, 0) and _cell(row, 0) not in draft_dates]
    new_rows = [
        _history_to_row(
            HistoryRow(
                date=dd.date,
                member=dd.member or "",
                reason=dd.reason,
                status=STATUS_SCHEDULED,
                posted_at="",
                notes=dd.note,
            )
        )
        for dd in draft
    ]
    return _rewrite(ws, HISTORY_HEADER, kept + new_rows, guild_id, tab_name)


def set_day_status(
    guild_id: int,
    tab_name: str,
    date_iso: str,
    *,
    member: str,
    reason: str,
    status: str,
    posted_at: str = "",
    notes: str = "",
) -> bool:
    """Upsert the single history row for `date_iso` (one conductor per date).

    Used by the daily-confirmation surface: confirm → posted, skip → skipped,
    pick / next → update the scheduled member. Finds the row by date and
    overwrites it; appends if no row exists yet."""
    ws = _open_tab(guild_id, tab_name, HISTORY_HEADER)
    if ws is None:
        return False
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] set_day_status read-back failed for guild {guild_id}: {e}")
        return False

    new_row = _history_to_row(
        HistoryRow(
            date=date_iso,
            member=member,
            reason=reason,
            status=status,
            posted_at=posted_at,
            notes=notes,
        )
    )
    body = values[1:]
    replaced = False
    out: list[list[str]] = []
    for row in body:
        if not _cell(row, 0):
            continue
        if _cell(row, 0) == date_iso and not replaced:
            out.append(new_row)
            replaced = True
        else:
            out.append(row)
    if not replaced:
        out.append(new_row)
    return _rewrite(ws, HISTORY_HEADER, out, guild_id, tab_name)


# ── Train Member Rules ───────────────────────────────────────────────────────


def load_member_rules(guild_id: int, tab_name: str) -> list[MemberRule]:
    ws = _open_tab(guild_id, tab_name, MEMBER_RULES_HEADER)
    if ws is None:
        return []
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] load_member_rules failed for guild {guild_id}: {e}")
        return []
    out: list[MemberRule] = []
    for row in values[1:]:
        if not row or not _cell(row, 0):
            continue
        out.append(
            MemberRule(
                member=_cell(row, 0),
                rule_type=_cell(row, 1).lower(),
                value=_cell(row, 2),
                notes=_cell(row, 3),
            )
        )
    return out


def set_member_rule(
    guild_id: int,
    tab_name: str,
    member: str,
    rule_type: str,
    value: str = "",
    notes: str = "",
) -> bool:
    """Upsert a member rule keyed by (member, rule_type)."""
    ws = _open_tab(guild_id, tab_name, MEMBER_RULES_HEADER)
    if ws is None:
        return False
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] set_member_rule read-back failed for guild {guild_id}: {e}")
        return False
    target = (_norm(member), rule_type.lower())
    new_row = [member, rule_type.lower(), value, notes]
    out: list[list[str]] = []
    replaced = False
    for row in values[1:]:
        if not _cell(row, 0):
            continue
        if (_norm(_cell(row, 0)), _cell(row, 1).lower()) == target and not replaced:
            out.append(new_row)
            replaced = True
        else:
            out.append(row)
    if not replaced:
        out.append(new_row)
    return _rewrite(ws, MEMBER_RULES_HEADER, out, guild_id, tab_name)


def clear_member_rule(
    guild_id: int, tab_name: str, member: str, rule_type: str | None = None
) -> bool:
    """Remove a member's rule(s). `rule_type=None` clears all rules for them."""
    ws = _open_tab(guild_id, tab_name, MEMBER_RULES_HEADER)
    if ws is None:
        return False
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] clear_member_rule read-back failed for guild {guild_id}: {e}")
        return False
    key = _norm(member)
    rt = rule_type.lower() if rule_type else None
    out = [
        row
        for row in values[1:]
        if _cell(row, 0)
        and not (_norm(_cell(row, 0)) == key and (rt is None or _cell(row, 1).lower() == rt))
    ]
    return _rewrite(ws, MEMBER_RULES_HEADER, out, guild_id, tab_name)


# ── Train Day Rules (multi-preset) ───────────────────────────────────────────


def list_presets(guild_id: int, tab_name: str) -> list[str]:
    """Preset names defined in the Day Rules tab, in first-seen order."""
    ws = _open_tab(guild_id, tab_name, DAY_RULES_HEADER)
    if ws is None:
        return []
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] list_presets failed for guild {guild_id}: {e}")
        return []
    seen: dict[str, None] = {}
    for row in values[1:]:
        name = _cell(row, 0)
        if name and name not in seen:
            seen[name] = None
    return list(seen)


def load_preset(guild_id: int, tab_name: str, name: str) -> SchedulePreset | None:
    """Load a named preset. Returns None if it doesn't exist. Missing weekdays
    are backfilled with random/auto so the preset always has all 7 days."""
    ws = _open_tab(guild_id, tab_name, DAY_RULES_HEADER)
    if ws is None:
        return None
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] load_preset failed for guild {guild_id}: {e}")
        return None
    days: dict[int, DayRule] = {}
    found = False
    for row in values[1:]:
        if _norm(_cell(row, 0)) != _norm(name):
            continue
        found = True
        wd = weekday_index(_cell(row, 1))
        if wd is None:
            continue
        rule_type = _cell(row, 2).lower() or RULE_AUTO
        days[wd] = DayRule(
            weekday=wd,
            rule_type=rule_type,
            specific_member=_cell(row, 3),
            notes=_cell(row, 4),
        )
    if not found:
        return None
    for wd in range(7):
        days.setdefault(wd, DayRule(weekday=wd))
    return SchedulePreset(name=name, days=days)


def save_preset(guild_id: int, tab_name: str, preset: SchedulePreset) -> bool:
    """Persist a preset, replacing every existing row for its name (mirrors the
    storm strategy write strategy). Writes all 7 weekday rows."""
    ws = _open_tab(guild_id, tab_name, DAY_RULES_HEADER)
    if ws is None:
        return False
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] save_preset read-back failed for guild {guild_id}: {e}")
        return False
    kept = [
        row for row in values[1:] if _cell(row, 0) and _norm(_cell(row, 0)) != _norm(preset.name)
    ]
    new_rows = []
    for wd in range(7):
        r = preset.rule_for(wd)
        new_rows.append(
            [
                preset.name,
                WEEKDAY_NAMES[wd],
                r.rule_type,
                r.specific_member,
                r.notes,
            ]
        )
    return _rewrite(ws, DAY_RULES_HEADER, kept + new_rows, guild_id, tab_name)


def delete_preset(guild_id: int, tab_name: str, name: str) -> bool:
    """Remove every row for a preset name."""
    ws = _open_tab(guild_id, tab_name, DAY_RULES_HEADER)
    if ws is None:
        return False
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[TRAIN ROTATION] delete_preset read-back failed for guild {guild_id}: {e}")
        return False
    kept = [row for row in values[1:] if _cell(row, 0) and _norm(_cell(row, 0)) != _norm(name)]
    return _rewrite(ws, DAY_RULES_HEADER, kept, guild_id, tab_name)


# ── Shared rewrite ───────────────────────────────────────────────────────────


def _rewrite(
    ws, header: list[str], body_rows: list[list[str]], guild_id: int, tab_name: str
) -> bool:
    """Clear the tab below the header and write `body_rows` in one batch.

    Single `update` after one `batch_clear` keeps us well under the Sheets
    60-writes/min quota even when a draft rewrites the whole history block."""
    try:
        ws.batch_clear([f"A2:{_col_letter(len(header))}{len(body_rows) + 5000}"])
        if body_rows:
            ws.update(
                "A2",
                body_rows,
                value_input_option="USER_ENTERED",
            )
        return True
    except Exception as e:
        print(f"[TRAIN ROTATION] rewrite of {tab_name!r} failed for guild {guild_id}: {e}")
        return False


def _col_letter(n: int) -> str:
    """1-based column index → spreadsheet letter (1→A, 26→Z, 27→AA)."""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out

"""
Storm date helpers (#145 / #146).

Two roles:
  1. Render ISO event dates as human-readable strings across every
     storm surface (embed titles, button labels, officer copy).
  2. Permissively parse leadership-typed dates at slash-command
     boundaries, plus per-command inference when `event_date` is
     omitted entirely.

Display layer always works off the stored ISO string; only the
rendered surface changes. Persistent View `custom_id`s and SQLite
keys still use ISO so storage round-trips unaffected.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ── Display formatters ──────────────────────────────────────────────────────


def format_event_date(event_date: str) -> str:
    """Long form for embed titles: ``Sunday, May 18, 2026``.

    Falls back to the raw input when unparseable so a Sheet typo or
    legacy non-ISO row still renders something instead of blanking the
    title.
    """
    d = _coerce_iso(event_date)
    if d is None:
        return (event_date or "").strip()
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"


def format_event_date_compact(event_date: str) -> str:
    """Compact form for inline references and button labels:
    ``Sun May 18``. No year — used in surfaces where the ISO date is
    near at hand for tiebreaking.
    """
    d = _coerce_iso(event_date)
    if d is None:
        return (event_date or "").strip()
    return f"{d.strftime('%a')} {d.strftime('%b')} {d.day}"


def _coerce_iso(event_date: str) -> Optional[_dt.date]:
    """Parse a stored ISO event_date or return None. Display helpers
    only — does NOT run the permissive parser, since the display layer
    should fail-soft to the raw string rather than re-interpret a
    typo at render time."""
    if not event_date:
        return None
    s = event_date.strip()
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s)
    except ValueError:
        return None


# ── Permissive parser ──────────────────────────────────────────────────────

_WEEKDAY_MAP = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "weds": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "thur": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

# Fully-qualified formats (year present). Order matters — try ISO first
# because it's the canonical storage form and the most-common officer input.
_FORMATS_WITH_YEAR = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m.%d.%Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
)

# Year-less formats. Year is inferred as "next occurrence on or after today"
# so a member typing "May 18" near the end of May 18 still gets May 18 of
# THIS year, but typing "Jan 5" in November rolls to next year.
_FORMATS_NO_YEAR = (
    "%m/%d",
    "%m-%d",
    "%m.%d",
    "%B %d",
    "%b %d",
    "%d %B",
    "%d %b",
)

_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", re.IGNORECASE)


def parse_event_date(
    raw: str,
    *,
    today: Optional[_dt.date] = None,
) -> Optional[_dt.date]:
    """Permissive parse of an officer-typed event date.

    Accepts:
      * ISO ``2026-05-18``, ``2026/05/18``, ``2026.05.18``
      * US slash/dash ``5/18/2026``, ``5-18``, ``05/18``
      * Long month names ``May 18``, ``May 18, 2026``, ``18 May``
      * Abbreviated ``May 18``, ``5 May``
      * Ordinal suffix tolerant: ``May 18th``, ``18th May``
      * Tokens: ``today``, ``tomorrow``
      * Weekday names (with optional ``next`` prefix): ``Sunday``,
        ``next sunday`` — bare weekday treats "today is that weekday"
        as next week, not this week, since storms are pre-scheduled.

    Returns None on unparseable input — caller surfaces the error,
    suggests a canonical format.
    """
    if not raw:
        return None
    today = today or _dt.date.today()
    s = raw.strip().rstrip(",.;:")
    if not s:
        return None

    lower = s.lower()
    if lower == "today":
        return today
    if lower in ("tomorrow", "tmrw", "tmw"):
        return today + _dt.timedelta(days=1)
    if lower == "yesterday":
        return today - _dt.timedelta(days=1)

    # Weekday tokens — single name, or "next <weekday>" / "this <weekday>".
    parts = lower.split()
    if len(parts) == 1 and parts[0] in _WEEKDAY_MAP:
        return _next_weekday(today, _WEEKDAY_MAP[parts[0]], same_day_rolls=True)
    if len(parts) == 2 and parts[0] in ("next", "this") and parts[1] in _WEEKDAY_MAP:
        return _next_weekday(
            today,
            _WEEKDAY_MAP[parts[1]],
            same_day_rolls=(parts[0] == "next"),
        )

    # Strip ordinal suffixes ("18th" -> "18", "1st" -> "1") and collapse
    # commas-as-separators so "May 18, 2026" parses through "%B %d %Y".
    s_norm = _ORDINAL_RE.sub(r"\1", s).replace(",", " ")
    s_norm = re.sub(r"\s+", " ", s_norm).strip()

    for fmt in _FORMATS_WITH_YEAR:
        try:
            return _dt.datetime.strptime(s_norm, fmt).date()
        except ValueError:
            pass

    for fmt in _FORMATS_NO_YEAR:
        try:
            parsed = _dt.datetime.strptime(s_norm, fmt).date()
        except ValueError:
            continue
        # `strptime` defaults the missing year to 1900; rebuild with
        # the inferred year ("on or after today, this year, else next").
        try:
            candidate = parsed.replace(year=today.year)
        except ValueError:
            # Feb 29 in a non-leap current year — try next year instead.
            try:
                return parsed.replace(year=today.year + 1)
            except ValueError:
                continue
        if candidate < today:
            try:
                candidate = parsed.replace(year=today.year + 1)
            except ValueError:
                continue
        return candidate

    return None


def parse_event_date_iso(
    raw: str,
    *,
    today: Optional[_dt.date] = None,
) -> Optional[str]:
    """Convenience: parse and return the ISO string, or None.

    Slash-command boundaries store and pass dates as ISO strings — this
    avoids every caller round-tripping through `.isoformat()`."""
    d = parse_event_date(raw, today=today)
    return d.isoformat() if d else None


def _next_weekday(
    today: _dt.date,
    target_dow: int,
    *,
    same_day_rolls: bool,
) -> _dt.date:
    """Next occurrence of `target_dow` (0=Mon..6=Sun). When today IS the
    target weekday, `same_day_rolls=True` advances a full week (the
    convention for storms — leadership scheduling a fresh DS on the
    day of a DS means *next* DS, not today's event)."""
    days_ahead = (target_dow - today.weekday()) % 7
    if days_ahead == 0 and same_day_rolls:
        days_ahead = 7
    return today + _dt.timedelta(days=days_ahead)


# ── Inference (per-command default when officer omits event_date) ──────────


# Event day-of-week is game-defined per Rule H (#164):
# DS runs Friday (weekday 4); CS runs Thursday (weekday 3).
# `next_event_date` returns the next such weekday regardless of
# guild config — alliances can't shift the in-game schedule.
_FIXED_EVENT_DOW = {"DS": 4, "CS": 3}


def next_event_date(
    guild_id: int,
    event_type: str,
    *,
    today: Optional[_dt.date] = None,
) -> str:
    """ISO event date the officer most likely meant when they omitted
    `event_date` on a pre-event command (`post_signup`, `signups` under
    the storm parent groups).

    Returns the next Friday (DS) / Thursday (CS) on or after `today`.
    `guild_id` is accepted for signature stability but ignored — the
    event day is game-defined, not per-alliance.
    """
    del guild_id  # signature-only; event day is fixed per Rule H.
    today = today or _dt.date.today()
    dow = _FIXED_EVENT_DOW.get(event_type.upper(), 6)
    return _next_weekday(today, dow, same_day_rolls=True).isoformat()


# ── Last-updated timestamp parser (#255) ────────────────────────────────────
#
# Parses the `Date Modified` / `Last Updated` column on whatever sheet
# the alliance configured for the stale-power DM nudge. Tolerant by
# design because the column could be written by:
#   * our own survey (`survey.update_squad_powers` writes m/d/yyyy UTC)
#   * a manually-maintained column (any format the officer types)
#   * a different bot (ISO 8601 likely; could be anything)
#
# US/EU date ambiguity is resolved per-column, not per-cell: we scan
# the column once and if any value has its first slash component > 12
# the whole column locks to DD/MM. Otherwise we default to MM/DD
# (matching our survey's output). A column with mixed formats falls
# back to MM/DD silently — that's an alliance-side data-quality
# problem we don't surface here.
#
# Unparseable cells return None. The click-handler treats None as
# "don't DM" — punishing members for an alliance-side formatting
# mismatch would be worse than missing a few stale-power nudges.

# Formats with explicit year. ISO first because it's unambiguous.
_LU_FORMATS_ISO = (
    "%Y-%m-%d",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y.%m.%d",
)

_LU_FORMATS_MDY = (
    "%m/%d/%Y",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%m-%d-%Y",
    "%m.%d.%Y",
)

_LU_FORMATS_DMY = (
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y",
    "%d.%m.%Y",
)

_LU_FORMATS_LONG_MONTH = (
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
)

# Two-digit-year variants. Year < 70 → 2000s, else 1900s (Python's
# default strptime behaviour). Alliances using 2-digit years for
# storm data is rare but a 3rd-party bot might.
_LU_FORMATS_MDY_SHORT = ("%m/%d/%y", "%m-%d-%y", "%m.%d.%y")
_LU_FORMATS_DMY_SHORT = ("%d/%m/%y", "%d-%m-%y", "%d.%m.%y")


def _strip_tz_suffix(raw: str) -> str:
    """Strip trailing 'UTC', 'GMT', or 'Z' so strptime can parse the
    bare datetime. We don't try to honour the tz — last_updated is
    rendered against today's local date with day-granular comparison,
    so tz drift is at worst one day off, which is well inside the
    stale-days threshold's noise floor."""
    s = raw.strip()
    s = re.sub(r"\s*\bUTC\b\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\bGMT\b\s*$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*\+\d{2}:?\d{2}$", "", s)
    s = re.sub(r"Z$", "", s)
    return s.strip()


def _looks_iso(raw: str) -> bool:
    """ISO 8601 starts with `YYYY-` — four digits then a dash."""
    return bool(re.match(r"^\d{4}-\d{1,2}-\d{1,2}", raw.strip()))


def detect_last_updated_dmy_first(cells: list[str]) -> bool:
    """Return True if the column appears to be DD/MM/YYYY, False otherwise.

    Heuristic: if any slash/dash/dot-separated value has its first
    component > 12, the column can only be DD/MM. Otherwise we
    default to MM/DD (matches our survey's output). ISO 8601 values
    (`YYYY-MM-DD`) are skipped — they're unambiguous and don't
    contribute to the M-vs-D vote either way.
    """
    sep_re = re.compile(r"^\s*(\d{1,4})[/\-.](\d{1,4})[/\-.]")
    for cell in cells:
        if not cell:
            continue
        s = cell.strip()
        if _looks_iso(s):
            continue
        m = sep_re.match(s)
        if not m:
            continue
        try:
            first = int(m.group(1))
        except ValueError:
            continue
        if first > 12:
            return True
    return False


def parse_last_updated(
    raw: str,
    *,
    dmy_first: bool = False,
) -> Optional[_dt.date]:
    """Parse a single Last-Updated cell into a date, or return None.

    `dmy_first` is the column-level format flag returned by
    `detect_last_updated_dmy_first`. ISO 8601 values bypass the flag
    (they're unambiguous). Long-month formats bypass the flag too.

    Returns a `datetime.date` — time-of-day and timezone are discarded
    on purpose; staleness comparison is day-granular.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = _strip_tz_suffix(s)
    if not s:
        return None

    # ISO 8601 first (unambiguous, most-likely-to-succeed for 3rd-party
    # bot exports). `date.fromisoformat` handles plain YYYY-MM-DD;
    # strptime handles the datetime variants.
    if _looks_iso(s):
        try:
            return _dt.date.fromisoformat(s[:10])
        except ValueError:
            pass
        for fmt in _LU_FORMATS_ISO:
            try:
                return _dt.datetime.strptime(s, fmt).date()
            except ValueError:
                continue

    # Long-month formats (`May 24 2026`) — unambiguous regardless of
    # locale, so we try them before the M/D vs D/M branch.
    for fmt in _LU_FORMATS_LONG_MONTH:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    # Numeric M/D vs D/M branch. Try the preferred order first, fall
    # back to the other so a single mis-keyed row in an otherwise
    # consistent column still parses if it happens to be unambiguous.
    primary = _LU_FORMATS_DMY if dmy_first else _LU_FORMATS_MDY
    primary_short = _LU_FORMATS_DMY_SHORT if dmy_first else _LU_FORMATS_MDY_SHORT
    secondary = _LU_FORMATS_MDY if dmy_first else _LU_FORMATS_DMY
    secondary_short = _LU_FORMATS_MDY_SHORT if dmy_first else _LU_FORMATS_DMY_SHORT
    for fmt in primary + primary_short + secondary + secondary_short:
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue

    return None


def most_recent_event_date(
    guild_id: int,
    event_type: str,
    *,
    today: Optional[_dt.date] = None,
) -> Optional[str]:
    """ISO event date for the most-recent past event (today inclusive),
    pulled from `storm_registration_posts`. Returns None when no posts
    have been recorded yet for this guild + event type.

    Used by the `attendance` subcommand when the officer omits `event_date` —
    attendance is recorded after-the-fact, so the right default is the
    last event that actually happened.
    """
    today = today or _dt.date.today()
    try:
        import config

        with config._get_conn() as conn:
            row = conn.execute(
                "SELECT event_date FROM storm_registration_posts "
                "WHERE guild_id = ? AND event_type = ? AND event_date <= ? "
                "ORDER BY event_date DESC LIMIT 1",
                (int(guild_id), event_type, today.isoformat()),
            ).fetchone()
    except Exception as e:
        logger.debug(
            "[STORM DATE] most_recent_event_date query failed for guild=%s event=%s: %s",
            guild_id,
            event_type,
            e,
        )
        return None
    if not row:
        return None
    return row[0]

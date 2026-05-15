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
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "thur": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Fully-qualified formats (year present). Order matters — try ISO first
# because it's the canonical storage form and the most-common officer input.
_FORMATS_WITH_YEAR = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d",
    "%m/%d/%Y", "%m-%d-%Y", "%m.%d.%Y",
    "%B %d %Y", "%b %d %Y",
    "%d %B %Y", "%d %b %Y",
)

# Year-less formats. Year is inferred as "next occurrence on or after today"
# so a member typing "May 18" near the end of May 18 still gets May 18 of
# THIS year, but typing "Jan 5" in November rolls to next year.
_FORMATS_NO_YEAR = (
    "%m/%d", "%m-%d", "%m.%d",
    "%B %d", "%b %d",
    "%d %B", "%d %b",
)

_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", re.IGNORECASE)


def parse_event_date(
    raw: str, *, today: Optional[_dt.date] = None,
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
            today, _WEEKDAY_MAP[parts[1]],
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
    raw: str, *, today: Optional[_dt.date] = None,
) -> Optional[str]:
    """Convenience: parse and return the ISO string, or None.

    Slash-command boundaries store and pass dates as ISO strings — this
    avoids every caller round-tripping through `.isoformat()`."""
    d = parse_event_date(raw, today=today)
    return d.isoformat() if d else None


def _next_weekday(
    today: _dt.date, target_dow: int, *, same_day_rolls: bool,
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


def next_event_date(
    guild_id: int, event_type: str, *,
    today: Optional[_dt.date] = None,
) -> str:
    """ISO event date the officer most likely meant when they omitted
    `event_date` on a pre-event command (`post_signup`, `signups` under
    the storm parent groups).

    Inference order:
      1. Structured-flow `event_day_of_week` if configured — the
         alliance has already told us when they run this event type.
      2. Convention fallback: next Sunday (matches the pre-existing
         `storm_officer_view._next_event_date` behaviour).
    """
    today = today or _dt.date.today()
    dow = -1
    try:
        import config
        cfg = config.get_structured_storm_config(int(guild_id), event_type)
        raw = cfg.get("event_day_of_week", -1)
        dow = int(raw) if raw is not None else -1
    except Exception as e:
        logger.debug(
            "[STORM DATE] next_event_date config lookup failed for "
            "guild=%s event=%s: %s",
            guild_id, event_type, e,
        )
        dow = -1
    if dow < 0 or dow > 6:
        dow = 6  # Sunday fallback — the historical default
    return _next_weekday(today, dow, same_day_rolls=True).isoformat()


def most_recent_event_date(
    guild_id: int, event_type: str, *,
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
            "[STORM DATE] most_recent_event_date query failed for "
            "guild=%s event=%s: %s",
            guild_id, event_type, e,
        )
        return None
    if not row:
        return None
    return row[0]

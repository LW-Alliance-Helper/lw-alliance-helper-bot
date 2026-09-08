"""
Time helpers — one home for "what day is it, and in whose calendar?".

**The rule: the server (in-game) day is the canonical internal calendar.
Use `server_today()` unless you have a specific, stated reason not to.**

Last War runs on UTC-2, so the in-game day rolls over at 02:00 UTC. The
bot exists to schedule in-game activity — events, the train rotation,
shiny servers, storm — so the game's day is the unit all of that should
compute in. Translate to a guild's local clock at *display* time; do not
compute in local time and hope it lines up.

The one justified exception is **data that is human-calendar by nature**,
where the value on the other side of the comparison has no timezone at
all. Birthdays are the current (and only) example: a birthday is a bare
month/day fact about a person, not about the game. Matching it against a
server day would compare a human calendar value to a game calendar one,
and for an alliance far from UTC-2 that lands the announcement on the
wrong local date — a Tokyo guild's 8am reminder on October 12th local is
still server-day October 11, so the announcement fires a day late in the
timezone the humans reading it actually live in. Those sites use
`local_today` and must say why in a comment.

Bare `date.today()` is the thing this module exists to replace. It is
dangerous precisely because it answers *without being asked which
calendar* — it hands back the container's day (UTC on Railway), which is
nobody's calendar in particular, and it looks correct in local testing
where every calendar agrees.

**If you already hold a correctly-zoned datetime, just call `.date()` on
it.** Do not route through this module for the sake of it — `now.date()`
where `now = datetime.now(tz=guild_tz)` is already unambiguous, and
wrapping it would only add indirection. These helpers are for the cases
where you have a timezone but no instant, or no timezone at all.

Known incidents this module exists to prevent:
  - The train rotation ran a full day behind at the reset boundary
    (#318), and the shiny server list did the same (#330 / #331).
  - The `/events` hub resolved "today" off the container clock while
    the schedules it drives are keyed to other calendars.

Imports stdlib only, deliberately: everything in the bot can depend on
this module without risking an import cycle.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Last War's in-game clock. The game defines this, not us — it is a
# fixed offset with no DST. This is the single source of truth for the
# offset; nothing else in the bot should redeclare it.
SERVER_TZ_OFFSET_HOURS = -2
SERVER_TZ = timezone(timedelta(hours=SERVER_TZ_OFFSET_HOURS))

# The bot's own default/legacy timezone, used where no per-guild or
# per-event timezone is configured.
ET = ZoneInfo("America/New_York")


def server_date_for(dt: datetime) -> date:
    """The Last War in-game (server) calendar date at instant `dt`.

    The in-game day rolls over at 00:00 Server Time, which is 02:00 UTC.
    So for the ~2 hours after UTC midnight, UTC has already moved to the
    next calendar date while the in-game day has not — and a guild's own
    *evening* is typically already the next in-game day.

    Date-keyed in-game schedules resolve "today" against this, not the
    guild's local calendar date; otherwise an announcement firing at the
    reset names the in-game day that just ended (#318).

    `dt` must be timezone-aware.
    """
    return dt.astimezone(SERVER_TZ).date()


def server_today() -> date:
    """Today's Last War in-game (server) date, right now.

    **This is the default.** Anything the game drives — event cycles, the
    train rotation, shiny servers, storm — computes in this calendar.

    The convenience form of `server_date_for`, which callers kept
    hand-rolling as `server_date_for(datetime.now(timezone.utc))`.
    """
    return server_date_for(datetime.now(timezone.utc))


def local_today(tz: ZoneInfo | timezone | None = None) -> date:
    """Today's calendar date in `tz` (default: ET) — **the exception.**

    Only for data that is human-calendar by nature, where the other side
    of the comparison carries no timezone: a birthday's bare month/day,
    for instance. Everything the game drives wants `server_today`
    instead; see this module's docstring for why mixing the two lands
    announcements on the wrong local date.

    If you reach for this, say why in a comment at the call site.
    """
    return datetime.now(tz=tz or ET).date()


def next_clock_time(hour: int, minute: int, tz: ZoneInfo | timezone | None = None) -> datetime:
    """The next real-world instant `hour:minute` occurs in `tz` (default: ET).

    A leader typing a time into an editor means "the next time the clock
    reads this" — not a specific calendar date. Building that datetime by
    combining the typed time with a separately-tracked date makes the
    result only as correct as whatever set that date; when it drifted, the
    typed time inherited the wrong day with no way for anyone to notice.

    Anchoring to the real clock instead makes a typed time self-correcting:
    it always lands on the next occurrence of that clock reading, whatever
    the surrounding view thinks it is building for.
    """
    now = datetime.now(tz=tz or ET)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate

"""Leadership Duties (#687): the Discord-free core.

A duty is a **standing** job a leadership team splits between its members
("Member Disputes/Concerns", "Daily Schedule"). It never finishes, so there
is no status, due date or "done" anywhere in this module. What leadership
needs from the bot is a record of who holds what, a count of how that load
is spread, reminders that follow the duty when it changes hands, and a way
for members to reach the holders.

This module owns the shapes and the arithmetic; `duties_db` stores them and
the Discord surfaces render them. Nothing here imports discord or touches
the database, so every rule below is unit-testable on plain values.

The design walkthrough that settled every rule here is recorded on the issue.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Callable, Iterable

import template_render

#: The `premium.PREMIUM_FEATURES` name the whole feature is gated on.
PREMIUM_FEATURE = "leadership_duties"

# ── Holders ──────────────────────────────────────────────────────────────────

SLOT_PRIMARY = "primary"
SLOT_BACKUP = "backup"
SLOTS = (SLOT_PRIMARY, SLOT_BACKUP)

#: The user id stored for an open slot: a position leadership means to fill
#: but hasn't. Kept in the holder list rather than as a separate count so a
#: holder leaving turns their own slot open in place (the order leadership
#: chose survives), and so "Primary: 2 of 3 filled" needs no second field.
OPEN = 0

#: The id stored for "anyone in leadership": the Leadership role from
#: `/setup`, as one of a slot's choices beside named people ("Bravo,
#: Charlie, anyone in leadership"). Either slot can hold it. Never a real
#: user id, so it is never DMed, counted in the workload, or vacated.
ANYONE = -1


@dataclass(frozen=True)
class Duty:
    id: int
    guild_id: int
    name: str
    #: The category's name, filled in on read for display. What is stored
    #: is `category_id`; categories are a managed list, not free text.
    category: str = ""
    description: str = ""
    #: Discord user ids in the order leadership set them; `OPEN` is an open
    #: position and `ANYONE` is "anyone in leadership".
    primaries: tuple[int, ...] = ()
    backups: tuple[int, ...] = ()
    #: The `/events` Pause idea: the duty stays listed, marked paused, its
    #: reminders stop and every setting is kept.
    paused: bool = False
    #: Members get a button for this duty on the contact panel.
    contact_enabled: bool = False
    sort_order: int = 0
    #: 0 = no category.
    category_id: int = 0

    def slot(self, kind: str) -> tuple[int, ...]:
        if kind == SLOT_PRIMARY:
            return self.primaries
        if kind == SLOT_BACKUP:
            return self.backups
        raise ValueError(f"unknown slot kind {kind!r}")

    def holders(self, kind: str) -> tuple[int, ...]:
        """The people named in `kind`: no open positions, no "anyone"."""
        return tuple(uid for uid in self.slot(kind) if uid not in (OPEN, ANYONE))

    def has_anyone(self, kind: str) -> bool:
        """Whether "anyone in leadership" is one of `kind`'s choices."""
        return ANYONE in self.slot(kind)

    def open_count(self, kind: str) -> int:
        return sum(1 for uid in self.slot(kind) if uid == OPEN)

    def all_holders(self) -> tuple[int, ...]:
        """Everyone named on the duty, primaries first, each once."""
        seen: dict[int, None] = {}
        for uid in self.holders(SLOT_PRIMARY) + self.holders(SLOT_BACKUP):
            seen.setdefault(uid, None)
        return tuple(seen)

    def any_anyone(self) -> bool:
        return self.has_anyone(SLOT_PRIMARY) or self.has_anyone(SLOT_BACKUP)

    def holds(self, user_id: int) -> bool:
        return user_id not in (OPEN, ANYONE) and user_id in self.all_holders()


def vacate(duty: Duty, user_id: int) -> Duty:
    """`duty` with every slot `user_id` held turned open, in place.

    A holder leaving the server leaves the job behind, not a shorter list:
    the slot stays where leadership put it and shows as open until someone
    is picked. Returns `duty` unchanged when they held nothing on it.
    """
    if not duty.holds(user_id):
        return duty
    return replace(
        duty,
        primaries=tuple(OPEN if uid == user_id else uid for uid in duty.primaries),
        backups=tuple(OPEN if uid == user_id else uid for uid in duty.backups),
    )


def held_by(duties: Iterable[Duty], user_id: int) -> tuple[list[Duty], list[Duty]]:
    """Everything `user_id` is primary on and everything they back up, the
    "my duties" view that supports aligning a new leader in person.

    Paused duties are included: holding a paused duty is still holding it.
    A duty that names them in both slots is listed under primary only, since
    that is the one they are actually carrying.
    """
    primary: list[Duty] = []
    backup: list[Duty] = []
    for duty in duties:
        if user_id in duty.holders(SLOT_PRIMARY):
            primary.append(duty)
        elif user_id in duty.holders(SLOT_BACKUP):
            backup.append(duty)
    return primary, backup


def by_category(duties: Iterable[Duty]) -> list[tuple[str, list[Duty]]]:
    """Duties grouped for display, categories in the order their first duty
    appears and duties in `sort_order` within each. The caller sorts the
    input the way leadership arranged it; this only groups."""
    groups: dict[str, list[Duty]] = {}
    for duty in duties:
        groups.setdefault(duty.category, []).append(duty)
    return [(cat, sorted(items, key=lambda d: d.sort_order)) for cat, items in groups.items()]


# ── Workload ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LeaderLoad:
    """One leader's line in the workload view.

    Counts are totals held, with the paused ones broken out, so the view can
    say `Primary: 3 (1 paused)`: three held, two being carried right now.
    """

    user_id: int
    primary: int = 0
    primary_paused: int = 0
    backup: int = 0
    backup_paused: int = 0
    #: False for someone who holds a duty but isn't in the Leadership role.
    #: They still appear, since a load nobody can see is the problem this
    #: view exists to catch.
    in_leadership: bool = True

    @property
    def primary_active(self) -> int:
        return self.primary - self.primary_paused

    @property
    def backup_active(self) -> int:
        return self.backup - self.backup_paused


def workload(duties: Iterable[Duty], leadership_ids: Iterable[int]) -> list[LeaderLoad]:
    """Every member of the Leadership role, plus anyone else holding a duty,
    with their counts.

    Leaders who hold nothing are listed with zeros: the bot knows the role,
    so it can show the imbalance a hand-built chart can't. "Anyone in
    leadership" backups count toward nobody. Order is the role's order
    as given, then non-leadership holders in the order they first appear;
    the renderer decides the display order.
    """
    counts: dict[int, dict[str, int]] = {}
    order: list[int] = []

    def _entry(uid: int) -> dict[str, int]:
        if uid not in counts:
            counts[uid] = {"primary": 0, "primary_paused": 0, "backup": 0, "backup_paused": 0}
            order.append(uid)
        return counts[uid]

    leaders = [uid for uid in leadership_ids if uid != OPEN]
    for uid in leaders:
        _entry(uid)

    for duty in duties:
        for kind in SLOTS:
            # A person listed twice in one slot still holds the duty once.
            for uid in dict.fromkeys(duty.holders(kind)):
                entry = _entry(uid)
                entry[kind] += 1
                if duty.paused:
                    entry[f"{kind}_paused"] += 1

    leader_set = set(leaders)
    return [
        LeaderLoad(user_id=uid, in_leadership=uid in leader_set, **counts[uid]) for uid in order
    ]


@dataclass(frozen=True)
class OpenSlot:
    duty: Duty
    kind: str
    count: int


def open_slots(duties: Iterable[Duty]) -> list[OpenSlot]:
    """Every duty with an unfilled position, for the section leadership uses
    to decide who to bring on next. Paused duties are included (the renderer
    marks them); the position still needs filling when the duty returns."""
    out: list[OpenSlot] = []
    for duty in duties:
        for kind in SLOTS:
            n = duty.open_count(kind)
            if n:
                out.append(OpenSlot(duty=duty, kind=kind, count=n))
    return out


# ── Reminders ────────────────────────────────────────────────────────────────

#: Every N days from an anchor date, the `/events` shape.
SCHEDULE_INTERVAL = "interval"
#: Chosen weekdays at a time, the storm sign-up shape.
SCHEDULE_WEEKDAYS = "weekdays"
SCHEDULE_TYPES = (SCHEDULE_INTERVAL, SCHEDULE_WEEKDAYS)

SEND_PRIMARIES = "primaries"
SEND_BACKUPS = "backups"
SEND_BOTH = "both"
SEND_CHANNEL = "channel"
#: A thread as the destination, the bot's 📢 / 🧵 pair. Delivered exactly
#: like a channel post; kept separate so the editor knows which picker to
#: show and the list says which it is.
SEND_THREAD = "thread"
SEND_TARGETS = (SEND_PRIMARIES, SEND_BACKUPS, SEND_BOTH, SEND_CHANNEL, SEND_THREAD)
POST_TARGETS = (SEND_CHANNEL, SEND_THREAD)


@dataclass(frozen=True)
class DutyReminder:
    """One reminder on one duty. A duty can carry several.

    There are deliberately no generic defaults: leadership switches a
    reminder on and writes exactly what it says.
    """

    id: int
    guild_id: int
    duty_id: int
    message: str
    schedule_type: str
    #: None only on a draft the officer hasn't given a time yet; a stored
    #: reminder always has one.
    at: time | None
    anchor_date: date | None = None
    interval_days: int = 1
    #: Monday = 0, as `date.weekday()`.
    weekdays: frozenset[int] = field(default_factory=frozenset)
    send_to: str = SEND_PRIMARIES
    #: The channel or thread for a post, and where the "anyone in
    #: leadership" half of a DM reminder goes. 0 = not set.
    channel_id: int = 0
    #: Ping whoever the reminder is for when it posts: the named holders,
    #: and the Leadership role where "anyone in leadership" is a choice.
    ping_holders: bool = False
    enabled: bool = True
    #: The occurrence date this last fired for, in the guild's calendar: the
    #: DB-backed "did this already fire today" the rest of the bot uses,
    #: never an in-memory set (#89).
    last_fired_on: date | None = None


def occurs_on(reminder: DutyReminder, day: date) -> bool:
    """Whether `reminder`'s schedule lands on `day`."""
    if reminder.schedule_type == SCHEDULE_WEEKDAYS:
        return day.weekday() in reminder.weekdays
    if reminder.schedule_type == SCHEDULE_INTERVAL:
        if reminder.anchor_date is None or reminder.interval_days < 1:
            return False
        delta = (day - reminder.anchor_date).days
        # Same arithmetic as `scheduler.next_event_dates`: the anchor and
        # every interval after it, never the days before the anchor.
        return delta >= 0 and delta % reminder.interval_days == 0
    return False


def due_occurrence(reminder: DutyReminder, now_local: datetime) -> date | None:
    """The occurrence `reminder` should fire for at `now_local`, or None.

    `now_local` is the current time in the guild's timezone. Fires at or
    past the set time rather than on an exact minute, so a tick that runs
    late doesn't skip the day (#365); `last_fired_on` stops the ticks after
    it from firing again. That includes the first tick after a restart, so a
    reminder due during a short outage goes out as soon as the bot is back.
    Only today's occurrence is considered: one whose whole day passed while
    the bot was down is the outage catch-up digest's to offer, not this
    loop's to send a day late.
    """
    if not reminder.enabled or reminder.at is None:
        return None
    today = now_local.date()
    if not occurs_on(reminder, today):
        return None
    if now_local.time().replace(second=0, microsecond=0) < reminder.at:
        return None
    if reminder.last_fired_on is not None and reminder.last_fired_on >= today:
        return None
    return today


def should_fire(duty: Duty, reminder: DutyReminder, now_local: datetime) -> date | None:
    """`due_occurrence`, with the duty's own state applied: a paused duty's
    reminders stop, and one for another duty never fires here."""
    if duty.paused or reminder.duty_id != duty.id:
        return None
    return due_occurrence(reminder, now_local)


@dataclass(frozen=True)
class Delivery:
    """Where one firing of a reminder goes."""

    dm_user_ids: tuple[int, ...] = ()
    #: 0 = no post.
    channel_id: int = 0
    #: Holders to mention in the post.
    ping_user_ids: tuple[int, ...] = ()
    #: Mention the Leadership role in the post: "anyone in leadership" is one
    #: of the choices the reminder is for, and it asked to ping.
    ping_leadership: bool = False


def plan_delivery(duty: Duty, reminder: DutyReminder, *, leadership_channel_id: int) -> Delivery:
    """Resolve a reminder's destination against the duty's current holders.

    Reads the holders at fire time, so a reminder follows the duty when it
    changes hands. Named holders are DMed; where "anyone in leadership" is
    one of a wanted slot's choices, that half goes to a post (the
    reminder's own channel, else the leadership channel) and never to every
    leader's DMs.
    """
    if reminder.send_to in POST_TARGETS:
        # A post is for everyone on the duty, so the ping is too, which for
        # "anyone in leadership" means the role.
        return Delivery(
            channel_id=reminder.channel_id,
            ping_user_ids=duty.all_holders() if reminder.ping_holders else (),
            ping_leadership=reminder.ping_holders and duty.any_anyone(),
        )

    wanted = []
    if reminder.send_to in (SEND_PRIMARIES, SEND_BOTH):
        wanted.append(SLOT_PRIMARY)
    if reminder.send_to in (SEND_BACKUPS, SEND_BOTH):
        wanted.append(SLOT_BACKUP)

    dms: dict[int, None] = {}
    anyone = False
    for kind in wanted:
        dms.update(dict.fromkeys(duty.holders(kind)))
        anyone = anyone or duty.has_anyone(kind)
    return Delivery(
        dm_user_ids=tuple(dms),
        channel_id=(reminder.channel_id or leadership_channel_id) if anyone else 0,
        ping_leadership=anyone and reminder.ping_holders,
    )


#: What leadership can write into a reminder, filled at send time so the
#: text doesn't go stale when a duty changes hands. The names are
#: provisional until the copy sign-off.
PLACEHOLDERS = ("duty", "primary", "backup")


def render_reminder(
    reminder: DutyReminder,
    duty: Duty,
    *,
    name_of: Callable[[int], str],
    anyone_in_leadership: str,
) -> str:
    """The reminder's text with the holders filled in.

    `name_of` turns a user id into however this destination shows a person
    (a mention in a channel, a display name in a DM). `anyone_in_leadership`
    is how "anyone in leadership" reads when it is one of a slot's choices.
    Open slots are left out; a slot with nobody in it renders as nothing.
    """

    def who(kind: str) -> str:
        names = [name_of(uid) for uid in duty.holders(kind)]
        if duty.has_anyone(kind):
            names.append(anyone_in_leadership)
        return ", ".join(names)

    primary, backup = who(SLOT_PRIMARY), who(SLOT_BACKUP)
    return template_render.render(reminder.message, duty=duty.name, primary=primary, backup=backup)

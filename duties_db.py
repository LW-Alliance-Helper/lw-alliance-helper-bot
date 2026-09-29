"""Leadership Duties (#687): storage.

**In `guild_configs.db`, not a file of its own.** The duty list is how one
server's leadership is set up, the same footing as its event schedule, so
it lives beside the rest of that server's configuration and goes with it
when the bot is removed (`config._GUILD_REMOVAL_DELETES`, whose schema test
fails for any `guild_id` table it doesn't name). The VS and Champion Duel
stores are separate files because their rows outlive a server; these must
not. No Sheet copy for now: the tickets already live in Discord as archived
threads, and a read-only tab can follow with the readable-tabs work (#668).

**Its own module, not more of `config.py`.** The tables are created from
`config.init_db` (via `create_tables`) so a fresh database and an upgraded
one come out identical, but the queries live here. Every connection comes
from `config._get_conn()` looked up at call time, so `/admin db_timings`
sees these calls and the `temp_db` / `seeded_db` fixtures reach them
without patching anything here.

Everything is synchronous. Reads are on-loop by convention; a write inside
a `tasks.loop` body goes through `asyncio.to_thread` (see CLAUDE.md).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, time

import config
from duties import ANYONE, OPEN, SLOT_BACKUP, SLOT_PRIMARY, Duty, DutyReminder


class DuplicateDutyName(ValueError):
    """Another duty in this server already has this name (case-insensitive).

    Names are how members pick a duty on the panel and how a ticket thread
    is titled, so two with the same name would be two buttons nobody can
    tell apart."""


class DuplicateCategoryName(ValueError):
    """Another category in this server already has this name
    (case-insensitive). A category is picked from a list by name."""


#: One select's worth: the duty modal offers categories as a dropdown.
MAX_CATEGORIES = 25


def create_tables(conn: sqlite3.Connection) -> None:
    """Called from `config.init_db`. Every statement is idempotent."""
    # One row per duty. `sort_order` is leadership's arrangement within the
    # hub and panel; ties fall back to id (creation order).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_duties (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id                 INTEGER NOT NULL,
            name                     TEXT    NOT NULL COLLATE NOCASE,
            category_id              INTEGER NOT NULL DEFAULT 0,
            description              TEXT    NOT NULL DEFAULT '',
            paused                   INTEGER NOT NULL DEFAULT 0,
            contact_enabled          INTEGER NOT NULL DEFAULT 0,
            sort_order               INTEGER NOT NULL DEFAULT 0,
            UNIQUE (guild_id, name)
        )
    """)
    # Categories are a managed list, picked from a dropdown, so a typo can't
    # split one group into two. Deleting one leaves its duties uncategorized.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_duty_categories (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id   INTEGER NOT NULL,
            name       TEXT    NOT NULL COLLATE NOCASE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            UNIQUE (guild_id, name)
        )
    """)
    # One row per slot. `user_id = 0` is an open slot (`duties.OPEN`) and
    # `-1` is "anyone in leadership" (`duties.ANYONE`), so a
    # holder leaving turns their row open in place and the position keeps
    # its order. `guild_id` is repeated here so the server removal and the
    # guild-scoped-table schema test reach it without a join.
    #
    # `vacated_user_id` / `vacated_name` say who left a slot open by leaving
    # the server, so the notice can still name them once they're gone. Any
    # save of the duty rewrites its rows without them: leadership has been
    # back to that duty, which is what the notice was asking for.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_duty_holders (
            guild_id        INTEGER NOT NULL,
            duty_id         INTEGER NOT NULL,
            slot            TEXT    NOT NULL,
            position        INTEGER NOT NULL,
            user_id         INTEGER NOT NULL DEFAULT 0,
            vacated_user_id INTEGER NOT NULL DEFAULT 0,
            vacated_name    TEXT    NOT NULL DEFAULT '',
            PRIMARY KEY (duty_id, slot, position)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_duty_holders_user "
        "ON guild_duty_holders (guild_id, user_id)"
    )
    # One row per reminder; a duty can have several. `weekdays` is a
    # comma-separated list of `date.weekday()` numbers. `last_fired_on` is
    # the occurrence date in the guild's calendar, the DB-backed dedup the
    # rest of the bot uses (#89).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_duty_reminders (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id      INTEGER NOT NULL,
            duty_id       INTEGER NOT NULL,
            message       TEXT    NOT NULL DEFAULT '',
            schedule_type TEXT    NOT NULL,
            at_time       TEXT    NOT NULL,
            anchor_date   TEXT    NOT NULL DEFAULT '',
            weekdays      TEXT    NOT NULL DEFAULT '',
            send_to       TEXT    NOT NULL DEFAULT 'primaries',
            channel_id    INTEGER NOT NULL DEFAULT 0,
            ping_holders  INTEGER NOT NULL DEFAULT 0,
            enabled       INTEGER NOT NULL DEFAULT 1,
            last_fired_on TEXT    NOT NULL DEFAULT ''
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_guild_duty_reminders_duty "
        "ON guild_duty_reminders (guild_id, duty_id)"
    )
    # Server-wide settings: where the contact panel is posted (and which
    # message it is, so the bot can edit it when a duty changes hands) and
    # which channel ticket threads open under.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS guild_duties_config (
            guild_id          INTEGER PRIMARY KEY,
            panel_channel_id  INTEGER NOT NULL DEFAULT 0,
            panel_message_id  INTEGER NOT NULL DEFAULT 0,
            ticket_channel_id INTEGER NOT NULL DEFAULT 0
        )
    """)


# ── Duties ───────────────────────────────────────────────────────────────────


def _holders_by_duty(conn: sqlite3.Connection, guild_id: int) -> dict[int, dict[str, list[int]]]:
    out: dict[int, dict[str, list[int]]] = {}
    rows = conn.execute(
        "SELECT duty_id, slot, user_id FROM guild_duty_holders "
        "WHERE guild_id = ? ORDER BY duty_id, slot, position",
        (guild_id,),
    ).fetchall()
    for row in rows:
        slots = out.setdefault(row["duty_id"], {SLOT_PRIMARY: [], SLOT_BACKUP: []})
        slots.setdefault(row["slot"], []).append(int(row["user_id"]))
    return out


def _duty_from_row(row: sqlite3.Row, holders: dict[str, list[int]] | None) -> Duty:
    holders = holders or {}
    return Duty(
        id=row["id"],
        guild_id=row["guild_id"],
        name=row["name"],
        category=row["category_name"] or "",
        category_id=row["category_id"] if row["category_name"] is not None else 0,
        description=row["description"],
        primaries=tuple(holders.get(SLOT_PRIMARY, ())),
        backups=tuple(holders.get(SLOT_BACKUP, ())),
        paused=bool(row["paused"]),
        contact_enabled=bool(row["contact_enabled"]),
        sort_order=row["sort_order"],
    )


_DUTY_SELECT = (
    "SELECT d.*, c.name AS category_name, c.sort_order AS category_order "
    "FROM guild_duties d LEFT JOIN guild_duty_categories c "
    "ON c.id = d.category_id AND c.guild_id = d.guild_id "
)


def list_duties(guild_id: int) -> list[Duty]:
    """Every duty in the server: grouped by category in the categories'
    order, uncategorized last, and in leadership's order within each."""
    with config._get_conn() as conn:
        rows = conn.execute(
            _DUTY_SELECT + "WHERE d.guild_id = ? "
            "ORDER BY c.id IS NULL, c.sort_order, c.id, d.sort_order, d.id",
            (guild_id,),
        ).fetchall()
        holders = _holders_by_duty(conn, guild_id)
    return [_duty_from_row(r, holders.get(r["id"])) for r in rows]


def get_duty(guild_id: int, duty_id: int) -> Duty | None:
    with config._get_conn() as conn:
        row = conn.execute(
            _DUTY_SELECT + "WHERE d.guild_id = ? AND d.id = ?", (guild_id, duty_id)
        ).fetchone()
        if row is None:
            return None
        holders = _holders_by_duty(conn, guild_id).get(duty_id)
    return _duty_from_row(row, holders)


def _write_holders(conn: sqlite3.Connection, duty: Duty, duty_id: int) -> None:
    conn.execute("DELETE FROM guild_duty_holders WHERE duty_id = ?", (duty_id,))
    for slot, ids in ((SLOT_PRIMARY, duty.primaries), (SLOT_BACKUP, duty.backups)):
        for position, uid in enumerate(ids):
            conn.execute(
                "INSERT INTO guild_duty_holders (guild_id, duty_id, slot, position, user_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (duty.guild_id, duty_id, slot, position, int(uid)),
            )


def save_duty(duty: Duty) -> int:
    """Insert (`duty.id == 0`) or update a duty and its holders, all in one
    transaction. Returns the duty's id.

    Raises `DuplicateDutyName` if another duty in the server has the name.
    """
    name = duty.name.strip()
    if not name:
        raise ValueError("a duty needs a name")
    values = (
        name,
        int(duty.category_id),
        duty.description.strip(),
        int(duty.paused),
        int(duty.contact_enabled),
        int(duty.sort_order),
    )
    try:
        with config._get_conn() as conn:
            if duty.id:
                cur = conn.execute(
                    "UPDATE guild_duties SET name = ?, category_id = ?, description = ?, "
                    "paused = ?, contact_enabled = ?, "
                    "sort_order = ? WHERE guild_id = ? AND id = ?",
                    (*values, duty.guild_id, duty.id),
                )
                if cur.rowcount == 0:
                    raise LookupError(f"no duty {duty.id} in guild {duty.guild_id}")
                duty_id = duty.id
            else:
                cur = conn.execute(
                    "INSERT INTO guild_duties (name, category_id, description, "
                    "paused, contact_enabled, sort_order, guild_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*values, duty.guild_id),
                )
                duty_id = int(cur.lastrowid)
            _write_holders(conn, duty, duty_id)
            conn.commit()
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc):
            raise DuplicateDutyName(name) from exc
        raise
    return duty_id


# ── Categories ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Category:
    id: int
    guild_id: int
    name: str
    sort_order: int = 0
    #: How many duties are in it, filled by `list_categories`.
    duty_count: int = 0


def list_categories(guild_id: int) -> list[Category]:
    with config._get_conn() as conn:
        rows = conn.execute(
            "SELECT c.*, (SELECT COUNT(*) FROM guild_duties d "
            "  WHERE d.guild_id = c.guild_id AND d.category_id = c.id) AS n "
            "FROM guild_duty_categories c WHERE c.guild_id = ? ORDER BY c.sort_order, c.id",
            (guild_id,),
        ).fetchall()
    return [Category(r["id"], r["guild_id"], r["name"], r["sort_order"], r["n"]) for r in rows]


def save_category(guild_id: int, name: str, category_id: int = 0) -> int:
    """Add (`category_id == 0`) or rename a category. Returns its id.

    Raises `DuplicateCategoryName`, `ValueError` for a blank name, and
    `LookupError` past `MAX_CATEGORIES` or for a missing id."""
    name = name.strip()
    if not name:
        raise ValueError("a category needs a name")
    try:
        with config._get_conn() as conn:
            if category_id:
                cur = conn.execute(
                    "UPDATE guild_duty_categories SET name = ? WHERE guild_id = ? AND id = ?",
                    (name, guild_id, category_id),
                )
                if cur.rowcount == 0:
                    raise LookupError(f"no category {category_id} in guild {guild_id}")
                new_id = category_id
            else:
                count, top = conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(sort_order), -1) "
                    "FROM guild_duty_categories WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
                if count >= MAX_CATEGORIES:
                    raise LookupError("category limit reached")
                cur = conn.execute(
                    "INSERT INTO guild_duty_categories (guild_id, name, sort_order) "
                    "VALUES (?, ?, ?)",
                    (guild_id, name, top + 1),
                )
                new_id = int(cur.lastrowid)
            conn.commit()
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc):
            raise DuplicateCategoryName(name) from exc
        raise
    return new_id


def delete_category(guild_id: int, category_id: int) -> int | None:
    """Delete a category. Its duties stay, uncategorized. Returns how many
    duties that touched, or None if there was no such category."""
    with config._get_conn() as conn:
        n = conn.execute(
            "DELETE FROM guild_duty_categories WHERE guild_id = ? AND id = ?",
            (guild_id, category_id),
        ).rowcount
        if not n:
            return None
        moved = conn.execute(
            "UPDATE guild_duties SET category_id = 0 WHERE guild_id = ? AND category_id = ?",
            (guild_id, category_id),
        ).rowcount
        conn.commit()
    return moved


def set_duty_paused(guild_id: int, duty_id: int, paused: bool) -> bool:
    """Pause or resume one duty. Every other setting is kept. Returns False
    if there is no such duty."""
    with config._get_conn() as conn:
        n = conn.execute(
            "UPDATE guild_duties SET paused = ? WHERE guild_id = ? AND id = ?",
            (int(paused), guild_id, duty_id),
        ).rowcount
        conn.commit()
    return n > 0


def delete_duty(guild_id: int, duty_id: int) -> bool:
    """Delete a duty with its holders and reminders. Returns False if there
    was no such duty. `config._get_conn` doesn't enable foreign keys, so the
    children are deleted here rather than by cascade."""
    with config._get_conn() as conn:
        n = conn.execute(
            "DELETE FROM guild_duties WHERE guild_id = ? AND id = ?", (guild_id, duty_id)
        ).rowcount
        if n:
            conn.execute(
                "DELETE FROM guild_duty_holders WHERE guild_id = ? AND duty_id = ?",
                (guild_id, duty_id),
            )
            conn.execute(
                "DELETE FROM guild_duty_reminders WHERE guild_id = ? AND duty_id = ?",
                (guild_id, duty_id),
            )
        conn.commit()
    return n > 0


def vacate_holder(guild_id: int, user_id: int, name: str = "") -> list[str]:
    """Turn every slot `user_id` held in this server open, in place, noting
    who left it (`name` is how they were known, since they can't be looked
    up once gone). Returns the names of the duties that changed. An empty
    list means they held nothing."""
    if user_id in (OPEN, ANYONE):
        return []
    with config._get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT d.name, d.sort_order, d.id FROM guild_duty_holders h "
            "JOIN guild_duties d ON d.id = h.duty_id "
            "WHERE h.guild_id = ? AND h.user_id = ? ORDER BY d.sort_order, d.id",
            (guild_id, user_id),
        ).fetchall()
        if rows:
            conn.execute(
                "UPDATE guild_duty_holders SET user_id = ?, vacated_user_id = ?, "
                "vacated_name = ? WHERE guild_id = ? AND user_id = ?",
                (OPEN, user_id, name, guild_id, user_id),
            )
            conn.commit()
    return [r["name"] for r in rows]


@dataclass(frozen=True)
class Departure:
    """Someone who left the server holding duties nobody has revisited yet."""

    user_id: int
    name: str
    duty_names: tuple[str, ...]


def departures(guild_id: int) -> list[Departure]:
    """Everyone whose leaving still has an open slot nobody has been back
    to, in the order they appear on the duty list."""
    with config._get_conn() as conn:
        rows = conn.execute(
            "SELECT h.vacated_user_id, h.vacated_name, d.name FROM guild_duty_holders h "
            "JOIN guild_duties d ON d.id = h.duty_id "
            "WHERE h.guild_id = ? AND h.vacated_user_id <> 0 "
            "ORDER BY d.sort_order, d.id, h.slot, h.position",
            (guild_id,),
        ).fetchall()
    people: dict[int, tuple[str, list[str]]] = {}
    for row in rows:
        name, duties_ = people.setdefault(row["vacated_user_id"], (row["vacated_name"], []))
        if row["name"] not in duties_:
            duties_.append(row["name"])
    return [Departure(uid, name, tuple(ds)) for uid, (name, ds) in people.items()]


# ── Reminders ────────────────────────────────────────────────────────────────


def _reminder_from_row(row: sqlite3.Row) -> DutyReminder:
    hh, mm = (int(p) for p in row["at_time"].split(":", 1))
    weekdays = frozenset(int(d) for d in row["weekdays"].split(",") if d.strip())
    return DutyReminder(
        id=row["id"],
        guild_id=row["guild_id"],
        duty_id=row["duty_id"],
        message=row["message"],
        schedule_type=row["schedule_type"],
        at=time(hh, mm),
        anchor_date=date.fromisoformat(row["anchor_date"]) if row["anchor_date"] else None,
        weekdays=weekdays,
        send_to=row["send_to"],
        channel_id=row["channel_id"],
        ping_holders=bool(row["ping_holders"]),
        enabled=bool(row["enabled"]),
        last_fired_on=date.fromisoformat(row["last_fired_on"]) if row["last_fired_on"] else None,
    )


def list_reminders(guild_id: int, duty_id: int | None = None) -> list[DutyReminder]:
    """A server's reminders, or one duty's, oldest first."""
    sql = "SELECT * FROM guild_duty_reminders WHERE guild_id = ?"
    params: tuple = (guild_id,)
    if duty_id is not None:
        sql += " AND duty_id = ?"
        params += (duty_id,)
    with config._get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY id", params).fetchall()
    return [_reminder_from_row(r) for r in rows]


def enabled_reminders() -> list[DutyReminder]:
    """Every switched-on reminder in every server, for the reminder loop.
    Pause and Premium are the loop's to check, against the duty and the
    server at fire time."""
    with config._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM guild_duty_reminders WHERE enabled = 1 ORDER BY guild_id, id"
        ).fetchall()
    return [_reminder_from_row(r) for r in rows]


def save_reminder(reminder: DutyReminder) -> int:
    """Insert (`reminder.id == 0`) or update a reminder. Returns its id.

    Saving never touches `last_fired_on`: editing the text or time of a
    reminder that already fired today must not make it fire again."""
    if reminder.at is None:
        raise ValueError("a reminder needs a time before it can be saved")
    values = (
        reminder.message,
        reminder.schedule_type,
        reminder.at.strftime("%H:%M"),
        reminder.anchor_date.isoformat() if reminder.anchor_date else "",
        ",".join(str(d) for d in sorted(reminder.weekdays)),
        reminder.send_to,
        int(reminder.channel_id),
        int(reminder.ping_holders),
        int(reminder.enabled),
    )
    with config._get_conn() as conn:
        if reminder.id:
            cur = conn.execute(
                "UPDATE guild_duty_reminders SET message = ?, schedule_type = ?, at_time = ?, "
                "anchor_date = ?, weekdays = ?, send_to = ?, channel_id = ?, "
                "ping_holders = ?, enabled = ? WHERE guild_id = ? AND id = ?",
                (*values, reminder.guild_id, reminder.id),
            )
            if cur.rowcount == 0:
                raise LookupError(f"no reminder {reminder.id} in guild {reminder.guild_id}")
            rid = reminder.id
        else:
            cur = conn.execute(
                "INSERT INTO guild_duty_reminders (message, schedule_type, at_time, anchor_date, "
                "weekdays, send_to, channel_id, ping_holders, enabled, "
                "guild_id, duty_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, reminder.guild_id, reminder.duty_id),
            )
            rid = int(cur.lastrowid)
        conn.commit()
    return rid


def delete_reminder(guild_id: int, reminder_id: int) -> bool:
    with config._get_conn() as conn:
        n = conn.execute(
            "DELETE FROM guild_duty_reminders WHERE guild_id = ? AND id = ?",
            (guild_id, reminder_id),
        ).rowcount
        conn.commit()
    return n > 0


def mark_reminder_fired(guild_id: int, reminder_id: int, occurrence: date) -> bool:
    """Record that `reminder_id` fired for `occurrence`. A loop-tick write,
    so the loop calls it through `asyncio.to_thread`.

    Conditional on the stored date being earlier, so two overlapping ticks
    can't both claim the same occurrence: the one that gets `True` sends.
    """
    stamp = occurrence.isoformat()
    with config._get_conn() as conn:
        n = conn.execute(
            "UPDATE guild_duty_reminders SET last_fired_on = ? "
            "WHERE guild_id = ? AND id = ? AND last_fired_on < ?",
            (stamp, guild_id, reminder_id, stamp),
        ).rowcount
        conn.commit()
    return n > 0


# ── Server settings ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DutiesSettings:
    guild_id: int
    panel_channel_id: int = 0
    panel_message_id: int = 0
    ticket_channel_id: int = 0


def get_settings(guild_id: int) -> DutiesSettings:
    with config._get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_duties_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()
    if row is None:
        return DutiesSettings(guild_id=guild_id)
    return DutiesSettings(
        guild_id=guild_id,
        panel_channel_id=row["panel_channel_id"],
        panel_message_id=row["panel_message_id"],
        ticket_channel_id=row["ticket_channel_id"],
    )


def save_settings(settings: DutiesSettings) -> None:
    with config._get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_duties_config "
            "(guild_id, panel_channel_id, panel_message_id, ticket_channel_id) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "panel_channel_id = excluded.panel_channel_id, "
            "panel_message_id = excluded.panel_message_id, "
            "ticket_channel_id = excluded.ticket_channel_id",
            (
                settings.guild_id,
                int(settings.panel_channel_id),
                int(settings.panel_message_id),
                int(settings.ticket_channel_id),
            ),
        )
        conn.commit()

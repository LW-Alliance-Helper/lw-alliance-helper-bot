"""Champion Duel data layer — its own SQLite file on the Railway volume.

Separate from `config.py`'s `guild_configs.db` on purpose. That database is
per-guild and private; this one is global tournament data contributed across
alliances and servers, with its own lifecycle — it can be wiped between
qualifiers and semifinals without touching a single alliance's configuration.

**Identity is (name, server), never name alone.** Last War names are not unique
across servers, so keying on the normalized name would merge two different
players the moment a second server contributed, and silently pool their
scouting. There is no way to unmerge that afterwards. The `registrants` table
therefore has a surrogate id with `UNIQUE (player_key, server)`, and squads,
orders and edits all hang off that id.

Everything here is **synchronous**. `ruff.toml` selects ASYNC, but its own
comment notes that only catches stdlib-level blocking calls — it does not know
sqlite3 blocks. Callers must wrap these in `asyncio.to_thread`, or a query
stalls the Discord gateway heartbeat for the whole process (#366).

Identity normalization is imported from `champion_duel_engine` rather than
reimplemented: the simulator keys its scouting by the same function, and a
second copy that drifted would file corrections under a key the simulator never
looks up — applying to nobody and raising nothing.

Attribution stores the raw Discord snowflake so this ports into Map Manager's
Alliance section later without a translation layer.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

try:
    from champion_duel_engine.names import normalize_name

    NAMES_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the degraded-mode path
    NAMES_AVAILABLE = False

    def normalize_name(name):
        """Refuses rather than inventing a second rule.

        A near-miss normalization is worse than none: it files edits under keys
        the simulator cannot find. Callers check NAMES_AVAILABLE and 503.
        """
        raise RuntimeError("champion_duel_engine is not installed")


DB_PATH = os.getenv("CHAMPION_DUEL_DB_PATH", "/app/data/champion_duel.sqlite3")

SESSION_TTL = timedelta(days=30)
AUTH_CODE_TTL = timedelta(seconds=60)

VALID_SOURCES = ("observed", "estimated", "edited")
# How a registrant row came to exist. `self_reported` is the community path --
# someone entered an opponent we had never heard of -- and must stay
# distinguishable from an official import for exactly the same reason
# squads.source exists: an assumption must never read like a verified fact.
VALID_ORIGINS = ("imported", "self_reported", "edited")
VALID_TYPES = ("Tank", "Missile", "Aircraft")


class AmbiguousPlayer(Exception):
    """That name exists on more than one server.

    Carries the candidates so a caller can ask which, rather than picking one
    and quietly attaching a sighting to the wrong person.
    """

    def __init__(self, name, candidates):
        super().__init__(f"{name!r} matches {len(candidates)} servers")
        self.name = name
        self.candidates = candidates


def _now() -> str:
    """UTC ISO-8601, stored as TEXT so it sorts lexicographically — which is
    what the admin date-range export filters on."""
    return datetime.now(timezone.utc).isoformat()


def _hash(token: str) -> str:
    """Tokens are stored hashed. This repo is public and the volume is
    snapshottable; neither should ever yield a usable credential."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _server(value) -> str | None:
    """Servers are digits in game but arrive as text from a modal."""
    if value is None:
        return None
    s = str(value).strip().lstrip("#")
    return s or None


def _group(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip().upper()
    return s or None


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _drop_pre_identity_tables(conn) -> bool:
    """Clear tables built before identity moved to (name, server).

    The first shape keyed `registrants` on `player_key` alone, and hung squads,
    orders and edits off that key. The current one has a surrogate `id` with
    UNIQUE (player_key, server), because two servers can field the same name and
    keying on the name alone silently merges them.

    `CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so
    a database created under the old shape stayed on it and every insert failed
    with "table registrants has no column named origin". That is what happened
    on dev: the volume was wiped while the pre-identity code was deployed, so
    the tables were rebuilt old and the schema commit that followed could not
    touch them.

    ALTER TABLE cannot fix it — the primary key changes and three tables change
    what they reference — so the old tables are dropped and recreated empty.

    **Safe only because nothing has ever successfully imported.** No import has
    completed against the old shape (it cannot), and this feature has never been
    on production. Guarded on the marker rather than on any failure, so it can
    only fire against that one obsolete layout: the day real rows exist they are
    in the new shape, and this stops matching.

    `sessions` and `auth_codes` are untouched. They reference no player.
    """
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "registrants" not in tables:
        return False
    columns = {r[1] for r in conn.execute("PRAGMA table_info(registrants)")}
    if "id" in columns:
        return False

    for name in ("edits", "order_history", "squads", "registrants"):
        conn.execute(f"DROP TABLE IF EXISTS {name}")
    print(
        "[CHAMPION_DUEL] dropped pre-identity tables (registrants keyed on name "
        "alone); they are recreated empty and the roster needs re-importing"
    )
    return True


def init_db() -> None:
    """Create tables if absent and apply pending migrations.

    Same shape as `config.init_db`: each ALTER in its own try/except so a re-run
    is harmless, and the CREATE TABLE above it stays in sync for fresh files.
    """
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with _get_conn() as conn:
        # Before any CREATE: the statements below are all IF NOT EXISTS and so
        # cannot correct a table that exists in the wrong shape.
        _drop_pre_identity_tables(conn)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS registrants (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                player_key   TEXT NOT NULL,
                display_name TEXT NOT NULL,
                server       TEXT,
                grp          TEXT,
                alliance     TEXT,
                rank         INTEGER,
                thp          REAL,
                fsp          REAL,
                seeded       INTEGER NOT NULL DEFAULT 0,
                origin       TEXT NOT NULL DEFAULT 'imported',
                added_by     TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                UNIQUE (player_key, server)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS squads (
                registrant_id INTEGER NOT NULL,
                slot          INTEGER NOT NULL,
                squad_type    TEXT,
                power         REAL,
                source        TEXT NOT NULL,
                observed_at   TEXT,
                updated_at    TEXT NOT NULL,
                updated_by    TEXT,
                PRIMARY KEY (registrant_id, slot),
                FOREIGN KEY (registrant_id) REFERENCES registrants(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_history (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                registrant_id INTEGER NOT NULL,
                slot1         TEXT NOT NULL,
                slot2         TEXT NOT NULL,
                slot3         TEXT NOT NULL,
                opponent      TEXT,
                observed_at   TEXT,
                source        TEXT NOT NULL,
                created_at    TEXT NOT NULL,
                created_by    TEXT,
                FOREIGN KEY (registrant_id) REFERENCES registrants(id) ON DELETE CASCADE
            )
        """)
        # Append-only. A revert never updates or deletes a row here; it writes a
        # new one carrying revert_of, so the history stays the whole truth.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS edits (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                target           TEXT NOT NULL,
                registrant_id    INTEGER NOT NULL,
                slot             INTEGER,
                field            TEXT,
                old_value        TEXT,
                new_value        TEXT,
                actor_discord_id TEXT NOT NULL,
                actor_name       TEXT,
                actor_guild_id   TEXT,
                created_at       TEXT NOT NULL,
                revert_of        INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash         TEXT PRIMARY KEY,
                discord_user_id    TEXT NOT NULL,
                discord_name       TEXT,
                can_write          INTEGER NOT NULL DEFAULT 0,
                writer_guild_id    TEXT,
                premium_checked_at TEXT,
                created_at         TEXT NOT NULL,
                expires_at         TEXT NOT NULL,
                last_used_at       TEXT,
                revoked_at         TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_codes (
                code_hash       TEXT PRIMARY KEY,
                discord_user_id TEXT NOT NULL,
                discord_name    TEXT,
                can_write       INTEGER NOT NULL DEFAULT 0,
                writer_guild_id TEXT,
                created_at      TEXT NOT NULL,
                expires_at      TEXT NOT NULL,
                used_at         TEXT
            )
        """)
        for stmt in (
            "CREATE INDEX IF NOT EXISTS ix_reg_group ON registrants(grp)",
            "CREATE INDEX IF NOT EXISTS ix_reg_key ON registrants(player_key)",
            "CREATE INDEX IF NOT EXISTS ix_reg_server ON registrants(server)",
            "CREATE INDEX IF NOT EXISTS ix_edits_created ON edits(created_at)",
            "CREATE INDEX IF NOT EXISTS ix_edits_reg ON edits(registrant_id)",
            "CREATE INDEX IF NOT EXISTS ix_edits_actor ON edits(actor_discord_id)",
            "CREATE INDEX IF NOT EXISTS ix_orders_reg ON order_history(registrant_id)",
            "CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(discord_user_id)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:  # pragma: no cover
                print(f"[CHAMPION_DUEL] index skipped: {exc}")


# ── Registrants ───────────────────────────────────────────────────────────────


def find_registrants(name: str, server=None) -> list[dict]:
    """Every registrant matching a name, optionally narrowed to one server."""
    key = normalize_name(name)
    sql = "SELECT * FROM registrants WHERE player_key = ?"
    params: list = [key]
    server = _server(server)
    if server:
        sql += " AND server = ?"
        params.append(server)
    sql += " ORDER BY server IS NULL, server"
    with _get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def resolve_registrant(name: str, server=None) -> dict:
    """Exactly one registrant, or an error that says which problem it is.

    Raises LookupError when nobody matches and AmbiguousPlayer when several do.
    Never picks a winner: attaching a sighting to the wrong player is not
    recoverable, and the caller is in a position to ask.
    """
    matches = find_registrants(name, server)
    if not matches:
        raise LookupError(f"no registrant matches {name!r}")
    if len(matches) > 1:
        raise AmbiguousPlayer(name, matches)
    return matches[0]


def upsert_registrant(
    name, *, server=None, grp=None, origin="self_reported", actor=None, **fields
) -> dict:
    """Create or update one registrant, keyed on (name, server).

    `origin` records how the row came to exist. A self-reported opponent must
    stay distinguishable from an official import, for the same reason
    squads.source exists — otherwise a guess hardens into a fact nobody can
    trace back.

    An existing row is never downgraded: an imported registrant stays
    `imported` even when someone later re-enters them by hand.
    """
    if origin not in VALID_ORIGINS:
        raise ValueError(f"origin must be one of {VALID_ORIGINS}")
    display = str(name).strip()
    if not display:
        raise ValueError("name is required")

    key = normalize_name(display)
    server = _server(server)
    grp = _group(grp)
    now = _now()
    actor_id = (actor or {}).get("discord_user_id")

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM registrants WHERE player_key = ? AND server IS ?", (key, server)
        ).fetchone()
        if row is None:
            cur = conn.execute(
                """
                INSERT INTO registrants
                    (player_key, display_name, server, grp, alliance, rank, thp,
                     fsp, seeded, origin, added_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    display,
                    server,
                    grp,
                    fields.get("alliance"),
                    fields.get("rank"),
                    fields.get("thp"),
                    fields.get("fsp"),
                    1 if fields.get("seeded") else 0,
                    origin,
                    actor_id,
                    now,
                    now,
                ),
            )
            new_id = cur.lastrowid
        else:
            new_id = row["id"]
            # Only fill what the caller actually supplied; a modal that leaves
            # alliance blank must not erase an imported value.
            sets, params = ["display_name = ?", "updated_at = ?"], [display, now]
            if grp:
                sets.append("grp = ?")
                params.append(grp)
            for col in ("alliance", "rank", "thp", "fsp"):
                if fields.get(col) is not None:
                    sets.append(f"{col} = ?")
                    params.append(fields[col])
            if row["origin"] == "self_reported" and origin == "imported":
                sets.append("origin = ?")
                params.append("imported")
            params.append(new_id)
            conn.execute(f"UPDATE registrants SET {', '.join(sets)} WHERE id = ?", params)

        return dict(conn.execute("SELECT * FROM registrants WHERE id = ?", (new_id,)).fetchone())


def import_registrants(rows: list[dict]) -> dict:
    """Bulk-load an official roster. Never touches scouting."""
    inserted = updated = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        before = find_registrants(name, row.get("server"))
        upsert_registrant(
            name,
            server=row.get("server"),
            grp=row.get("group"),
            origin="imported",
            alliance=row.get("alliance"),
            rank=row.get("rank"),
            thp=row.get("thp"),
            fsp=row.get("fsp"),
            seeded=row.get("seeded"),
        )
        if before:
            updated += 1
        else:
            inserted += 1
    return {"inserted": inserted, "updated": updated, "total": inserted + updated}


def get_groups() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT grp, COUNT(*) AS n FROM registrants "
            "WHERE grp IS NOT NULL AND grp != '' GROUP BY grp ORDER BY grp"
        ).fetchall()
    return [{"group": r["grp"], "registrants": r["n"]} for r in rows]


def get_roster(group=None, include_scouting: bool = False) -> list[dict]:
    """Registrants, optionally with their squads.

    `include_scouting` is False for anonymous callers: the registrant list is a
    public LWS export, but squad composition and deployment orders are our own
    scouting.
    """
    sql = "SELECT * FROM registrants"
    params: tuple = ()
    if group:
        sql += " WHERE grp = ?"
        params = (_group(group),)
    sql += " ORDER BY grp, rank, display_name"

    with _get_conn() as conn:
        players = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if include_scouting and players:
            ids = {p["id"] for p in players}
            by_id: dict[int, list] = {i: [] for i in ids}
            for r in conn.execute("SELECT * FROM squads ORDER BY registrant_id, slot").fetchall():
                if r["registrant_id"] in by_id:
                    by_id[r["registrant_id"]].append(dict(r))
            for p in players:
                p["squads"] = by_id.get(p["id"], [])
    return players


def get_player(name, server=None, include_scouting: bool = False) -> dict | None:
    """One player with their scouting, or None. Raises AmbiguousPlayer when the
    name exists on several servers and none was given."""
    try:
        player = resolve_registrant(name, server)
    except LookupError:
        return None
    if include_scouting:
        with _get_conn() as conn:
            player["squads"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM squads WHERE registrant_id = ? ORDER BY slot", (player["id"],)
                ).fetchall()
            ]
            player["orders"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM order_history WHERE registrant_id = ? "
                    "ORDER BY COALESCE(observed_at, created_at) DESC",
                    (player["id"],),
                ).fetchall()
            ]
    return player


def most_common_order(registrant_id: int) -> dict | None:
    """The order this player is seen in most often, and how sure that is.

    Repeats are the signal: someone seen five times leading Missile and once
    leading Tank should read 5:1, which is exactly what `predict_matchup`
    consumes when sampling observed orders.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT slot1, slot2, slot3, COUNT(*) AS n FROM order_history "
            "WHERE registrant_id = ? GROUP BY slot1, slot2, slot3 ORDER BY n DESC",
            (registrant_id,),
        ).fetchall()
    if not rows:
        return None
    total = sum(r["n"] for r in rows)
    top = rows[0]
    return {
        "order": [top["slot1"], top["slot2"], top["slot3"]],
        "seen": top["n"],
        "total": total,
        "distinct": len(rows),
    }


# ── Writes (each one audited) ─────────────────────────────────────────────────


def _record_edit(conn, *, target, registrant_id, slot, field, old, new, actor, revert_of=None):
    cur = conn.execute(
        """
        INSERT INTO edits (target, registrant_id, slot, field, old_value, new_value,
                           actor_discord_id, actor_name, actor_guild_id,
                           created_at, revert_of)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target,
            registrant_id,
            slot,
            field,
            None if old is None else str(old),
            None if new is None else str(new),
            actor["discord_user_id"],
            actor.get("discord_name"),
            actor.get("guild_id"),
            _now(),
            revert_of,
        ),
    )
    return cur.lastrowid


def set_squad(registrant_id, slot, squad_type=None, power=None, *, actor, source="edited"):
    """Set one squad slot. Each changed field becomes its own edit row, so
    reverting a wrong type does not also revert a correct power entered in the
    same request."""
    if slot not in (1, 2, 3):
        raise ValueError("slot must be 1, 2 or 3")
    if squad_type is not None and squad_type not in VALID_TYPES:
        raise ValueError(f"squad_type must be one of {VALID_TYPES}")
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}")

    edit_ids = []
    with _get_conn() as conn:
        if not conn.execute("SELECT 1 FROM registrants WHERE id = ?", (registrant_id,)).fetchone():
            raise LookupError(f"no registrant {registrant_id}")

        row = conn.execute(
            "SELECT * FROM squads WHERE registrant_id = ? AND slot = ?", (registrant_id, slot)
        ).fetchone()
        old_type = row["squad_type"] if row else None
        old_power = row["power"] if row else None
        new_type = old_type if squad_type is None else squad_type
        new_power = old_power if power is None else float(power)

        conn.execute(
            """
            INSERT INTO squads (registrant_id, slot, squad_type, power, source,
                                updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(registrant_id, slot) DO UPDATE SET
                squad_type = excluded.squad_type,
                power      = excluded.power,
                source     = excluded.source,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (registrant_id, slot, new_type, new_power, source, _now(), actor["discord_user_id"]),
        )
        if squad_type is not None and old_type != new_type:
            edit_ids.append(
                _record_edit(
                    conn,
                    target="squad",
                    registrant_id=registrant_id,
                    slot=slot,
                    field="squad_type",
                    old=old_type,
                    new=new_type,
                    actor=actor,
                )
            )
        if power is not None and old_power != new_power:
            edit_ids.append(
                _record_edit(
                    conn,
                    target="squad",
                    registrant_id=registrant_id,
                    slot=slot,
                    field="power",
                    old=old_power,
                    new=new_power,
                    actor=actor,
                )
            )
    return {"registrant_id": registrant_id, "slot": slot, "edit_ids": edit_ids}


def add_order(registrant_id, slots, *, actor, opponent=None, observed_at=None, source="observed"):
    """Record a deployment order actually seen. Appends; repeats are meaningful.

    Every lineup observed to date runs exactly one Tank, one Missile and one
    Aircraft, so an order is a permutation of the three. A repeat would mean
    either a game change or a typo, and both deserve a refusal rather than a
    silent record.
    """
    if len(slots) != 3 or any(s not in VALID_TYPES for s in slots):
        raise ValueError(f"slots must be three of {VALID_TYPES}")
    if len(set(slots)) != 3:
        raise ValueError("a deployment order uses each squad type once")

    with _get_conn() as conn:
        if not conn.execute("SELECT 1 FROM registrants WHERE id = ?", (registrant_id,)).fetchone():
            raise LookupError(f"no registrant {registrant_id}")
        cur = conn.execute(
            """
            INSERT INTO order_history
                (registrant_id, slot1, slot2, slot3, opponent, observed_at,
                 source, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                registrant_id,
                slots[0],
                slots[1],
                slots[2],
                opponent,
                observed_at,
                source,
                _now(),
                actor["discord_user_id"],
            ),
        )
        order_id = cur.lastrowid
        edit_id = _record_edit(
            conn,
            target="order",
            registrant_id=registrant_id,
            slot=None,
            field="order",
            old=None,
            new="/".join(slots),
            actor=actor,
        )
    return {"registrant_id": registrant_id, "order_id": order_id, "edit_ids": [edit_id]}


# ── Bulk import of scouting ───────────────────────────────────────────────────
#
# Imports are the baseline, not an edit, so none of this writes to `edits` --
# the same position `import_registrants` already takes. A roster load would
# otherwise put hundreds of rows into the audit trail on every run and bury the
# corrections a human actually made, which is the one thing that log is for.


def _resolve_for_import(name, server):
    """Registrant id for one import row, or a reason it can't be used.

    Bulk work reports and continues rather than raising. One misspelled name in
    a 400-row roster must not abandon the other 399, and the caller needs the
    list of what didn't land -- silently importing 399 of 400 is worse than
    either extreme.
    """
    try:
        return resolve_registrant(name, server)["id"], None
    except AmbiguousPlayer as exc:
        servers = ", ".join(str(c["server"]) for c in exc.candidates)
        return None, f"{name!r} is on several servers ({servers}) — give one"
    except LookupError:
        return None, f"no registrant matches {name!r}"


def import_squads(rows: list[dict], *, actor) -> dict:
    """Seed squad values in bulk.

    **An import never downgrades.** A slot already carrying an `observed`
    sighting or an `edited` correction keeps it when an `estimated` value
    arrives for the same slot -- that is the whole reason `squads.source`
    exists, and re-running an import after a scout has corrected something
    must not undo their work.

    Estimates are computed by the caller rather than here: the THP ratios are
    fitted against the sighting corpus, which lives in the simulator, and a
    second copy of a calibrated constant is exactly what this project keeps
    getting bitten by.
    """
    applied = skipped = protected = 0
    problems: list[str] = []
    now = _now()
    actor_id = (actor or {}).get("discord_user_id")

    with _get_conn() as conn:
        for row in rows:
            registrant_id, problem = _resolve_for_import(row.get("name"), row.get("server"))
            if problem:
                problems.append(problem)
                skipped += 1
                continue

            slot = row.get("slot")
            squad_type = row.get("type")
            source = row.get("source") or "estimated"
            if slot not in (1, 2, 3) or squad_type not in VALID_TYPES:
                problems.append(f"{row.get('name')!r} slot {slot!r}/{squad_type!r} is not valid")
                skipped += 1
                continue
            if source not in VALID_SOURCES:
                problems.append(f"{row.get('name')!r} has source {source!r}")
                skipped += 1
                continue
            try:
                power = float(row.get("power"))
            except (TypeError, ValueError):
                problems.append(f"{row.get('name')!r} slot {slot} has no usable power")
                skipped += 1
                continue

            existing = conn.execute(
                "SELECT source FROM squads WHERE registrant_id = ? AND slot = ?",
                (registrant_id, slot),
            ).fetchone()
            if existing and source == "estimated" and existing["source"] in ("observed", "edited"):
                protected += 1
                continue

            conn.execute(
                """
                INSERT INTO squads (registrant_id, slot, squad_type, power, source,
                                    observed_at, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(registrant_id, slot) DO UPDATE SET
                    squad_type  = excluded.squad_type,
                    power       = excluded.power,
                    source      = excluded.source,
                    observed_at = excluded.observed_at,
                    updated_at  = excluded.updated_at,
                    updated_by  = excluded.updated_by
                """,
                (
                    registrant_id,
                    slot,
                    squad_type,
                    power,
                    source,
                    row.get("observed_at"),
                    now,
                    actor_id,
                ),
            )
            applied += 1

    return {
        "applied": applied,
        "skipped": skipped,
        "kept_observed": protected,
        "problems": problems[:50],
    }


def import_orders(rows: list[dict], *, actor) -> dict:
    """Load scouted deployment orders, replacing the previous import.

    **Idempotent on purpose, and this is the subtle part.** Repeats in
    `order_history` are the weight -- a player seen five times in one order and
    once in another samples 5:1, which *is* the prediction's read on what they
    will have set when the two meet. Appending on every run would double every
    weight and skew every prediction downstream, silently and permanently,
    because nothing about the resulting numbers looks wrong.

    So imported rows carry `source='imported'` and a re-import deletes and
    replaces them -- but only for the players named in this payload, and only
    the imported ones. A sighting someone entered through the hub is
    `source='observed'` and survives untouched; it is not ours to discard.
    """
    applied = skipped = 0
    problems: list[str] = []
    now = _now()
    actor_id = (actor or {}).get("discord_user_id")

    prepared: dict[int, list[dict]] = {}
    for row in rows:
        registrant_id, problem = _resolve_for_import(row.get("name"), row.get("server"))
        if problem:
            problems.append(problem)
            skipped += 1
            continue
        slots = list(row.get("slots") or [])
        if len(slots) != 3 or any(s not in VALID_TYPES for s in slots) or len(set(slots)) != 3:
            problems.append(f"{row.get('name')!r} order {slots!r} is not a permutation")
            skipped += 1
            continue
        prepared.setdefault(registrant_id, []).append({**row, "slots": slots})

    with _get_conn() as conn:
        for registrant_id, orders in prepared.items():
            conn.execute(
                "DELETE FROM order_history WHERE registrant_id = ? AND source = 'imported'",
                (registrant_id,),
            )
            for row in orders:
                slots = row["slots"]
                conn.execute(
                    """
                    INSERT INTO order_history
                        (registrant_id, slot1, slot2, slot3, opponent, observed_at,
                         source, created_at, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, 'imported', ?, ?)
                    """,
                    (
                        registrant_id,
                        slots[0],
                        slots[1],
                        slots[2],
                        row.get("opponent"),
                        row.get("observed_at"),
                        now,
                        actor_id,
                    ),
                )
                applied += 1

    return {
        "applied": applied,
        "skipped": skipped,
        "players": len(prepared),
        "problems": problems[:50],
    }


# ── Audit + revert ────────────────────────────────────────────────────────────


def list_edits(*, since=None, until=None, player=None, server=None, actor=None, limit=50, offset=0):
    """Newest first. `since`/`until` are ISO-8601 and compare as text."""
    sql = (
        "SELECT e.*, r.display_name, r.server FROM edits e "
        "LEFT JOIN registrants r ON r.id = e.registrant_id WHERE 1=1"
    )
    where: list[str] = []
    params: list = []
    if since:
        where.append(" AND e.created_at >= ?")
        params.append(since)
    if until:
        where.append(" AND e.created_at <= ?")
        params.append(until)
    if player:
        ids = [p["id"] for p in find_registrants(player, server)]
        if not ids:
            return {"edits": [], "total": 0}
        where.append(f" AND e.registrant_id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if actor:
        where.append(" AND e.actor_discord_id = ?")
        params.append(str(actor))

    clause = "".join(where)
    with _get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                sql + clause + " ORDER BY e.id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        ]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM edits e WHERE 1=1" + clause, params
        ).fetchone()["n"]
    return {"edits": rows, "total": total}


class RevertConflict(Exception):
    """The value moved on after the edit being reverted.

    Carries the current value so the caller can show what it found instead of a
    bare failure — an admin needs to see the newer correction before deciding
    whether to stamp on it.
    """

    def __init__(self, current, expected):
        super().__init__(f"value is now {current!r}, expected {expected!r}")
        self.current = current
        self.expected = expected


def revert_edit(edit_id: int, *, actor, force: bool = False) -> dict:
    """Restore the value an edit replaced, as a new append-only edit.

    Optimistically checked: if the field changed again since, this raises
    RevertConflict rather than clobbering the newer correction. Two scouts
    entering sightings for one player at once is normal, and the later entry is
    usually the better information.
    """
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
        if row is None:
            raise LookupError(f"no edit {edit_id}")
        if row["target"] != "squad":
            raise ValueError("only squad edits can be reverted")

        reg_id, slot, field = row["registrant_id"], row["slot"], row["field"]
        # `field` reaches an UPDATE by name. It can only ever be one of ours,
        # but whitelisting means a corrupt audit row can't become arbitrary SQL.
        if field not in ("squad_type", "power"):
            raise ValueError(f"unrevertable field {field!r}")

        current_row = conn.execute(
            "SELECT * FROM squads WHERE registrant_id = ? AND slot = ?", (reg_id, slot)
        ).fetchone()
        current = None if current_row is None else current_row[field]

        if not force and (current is None) != (row["new_value"] is None):
            raise RevertConflict(current, row["new_value"])
        if not force and current is not None and str(current) != str(row["new_value"]):
            raise RevertConflict(current, row["new_value"])

        restored = row["old_value"]
        restored_typed = (
            None if restored is None else (float(restored) if field == "power" else restored)
        )
        conn.execute(
            f"UPDATE squads SET {field} = ?, updated_at = ?, updated_by = ?, "  # noqa: S608
            "source = 'edited' WHERE registrant_id = ? AND slot = ?",
            (restored_typed, _now(), actor["discord_user_id"], reg_id, slot),
        )
        new_id = _record_edit(
            conn,
            target="squad",
            registrant_id=reg_id,
            slot=slot,
            field=field,
            old=current,
            new=restored,
            actor=actor,
            revert_of=edit_id,
        )
    return {"edit_id": new_id, "reverted": edit_id, "restored_to": restored}


def export_edits(start: str, end: str) -> list[dict]:
    """Every edit in a date range, oldest first — the spreadsheet view.

    Oldest-first because it reads as a narrative of what happened.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT e.*, r.display_name, r.server, r.grp FROM edits e "
            "LEFT JOIN registrants r ON r.id = e.registrant_id "
            "WHERE e.created_at >= ? AND e.created_at <= ? ORDER BY e.id ASC",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


def contributor_summary(limit: int = 25) -> list[dict]:
    """Who has contributed what. The contributor graph IS the user base — no
    separate table needed to know which servers have people entering data."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT actor_discord_id, actor_name, COUNT(*) AS edits, "
            "MAX(created_at) AS last_seen FROM edits "
            "GROUP BY actor_discord_id ORDER BY edits DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Sessions ──────────────────────────────────────────────────────────────────


def create_session(discord_user_id, discord_name=None, can_write=False, writer_guild_id=None):
    """Mint a session. Returns the plaintext token exactly once — only its hash
    is stored, so it cannot be recovered from the volume afterwards."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sessions (token_hash, discord_user_id, discord_name,
                                  can_write, writer_guild_id, premium_checked_at,
                                  created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _hash(token),
                str(discord_user_id),
                discord_name,
                1 if can_write else 0,
                None if writer_guild_id is None else str(writer_guild_id),
                now.isoformat(),
                now.isoformat(),
                (now + SESSION_TTL).isoformat(),
            ),
        )
    return token


def get_session(token: str) -> dict | None:
    if not token:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM sessions WHERE token_hash = ?", (_hash(token),)
        ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        if row["expires_at"] <= _now():
            return None
        conn.execute(
            "UPDATE sessions SET last_used_at = ? WHERE token_hash = ?",
            (_now(), row["token_hash"]),
        )
    return dict(row)


def update_session_premium(token, can_write, writer_guild_id=None):
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET can_write = ?, writer_guild_id = ?, "
            "premium_checked_at = ? WHERE token_hash = ?",
            (
                1 if can_write else 0,
                None if writer_guild_id is None else str(writer_guild_id),
                _now(),
                _hash(token),
            ),
        )


def revoke_session(token: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token_hash = ?", (_now(), _hash(token))
        )


def purge_expired() -> int:
    with _get_conn() as conn:
        n = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),)).rowcount
        conn.execute("DELETE FROM auth_codes WHERE expires_at <= ?", (_now(),))
    return n


# ── OAuth hand-off codes ──────────────────────────────────────────────────────


def create_auth_code(discord_user_id, discord_name=None, can_write=False, writer_guild_id=None):
    """One-time code the browser carries back from the OAuth callback.

    Holds the resolved identity, not a session token: the session is minted at
    redemption, which is what lets `sessions` store only a hash.
    """
    code = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO auth_codes (code_hash, discord_user_id, discord_name,
                                    can_write, writer_guild_id, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _hash(code),
                str(discord_user_id),
                discord_name,
                1 if can_write else 0,
                None if writer_guild_id is None else str(writer_guild_id),
                now.isoformat(),
                (now + AUTH_CODE_TTL).isoformat(),
            ),
        )
    return code


def consume_auth_code(code: str) -> dict | None:
    """Redeem a code for the identity behind it, once. Unknown, expired and
    already-used all answer the same, so a caller cannot probe which codes
    existed."""
    if not code:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM auth_codes WHERE code_hash = ?", (_hash(code),)
        ).fetchone()
        if row is None or row["used_at"] is not None or row["expires_at"] <= _now():
            return None
        conn.execute(
            "UPDATE auth_codes SET used_at = ? WHERE code_hash = ?", (_now(), row["code_hash"])
        )
    return {
        "discord_user_id": row["discord_user_id"],
        "discord_name": row["discord_name"],
        "can_write": bool(row["can_write"]),
        "writer_guild_id": row["writer_guild_id"],
    }

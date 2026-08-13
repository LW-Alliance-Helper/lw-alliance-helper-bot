"""Champion Duel data layer — its own SQLite file on the Railway volume.

Separate from `config.py`'s `guild_configs.db` on purpose. That database is
per-guild and private; this one is global tournament data shared across every
alliance that contributes to it. They also have different lifecycles — a
Champion Duel file can be wiped when qualifiers roll into semifinals without
touching a single alliance's configuration. Future siblings
(`warzone_duel.sqlite3`, `alliance_vs_duel.sqlite3`) follow the same shape:
three separate events whose rows physically cannot join to each other.

Everything here is **synchronous**. `ruff.toml` selects ASYNC, but its own
comment notes that only catches stdlib-level blocking calls — it does not know
sqlite3 blocks. Callers in the aiohttp handlers must wrap these in
`asyncio.to_thread`, or a query stalls the Discord gateway heartbeat for the
whole process. That is what #366 swept up.

Identity is `champion_duel_engine.names.normalize_name`, imported rather than
reimplemented: the simulator keys its scouting by the same function, and a
second implementation that drifted would file corrections under a key the
simulator never looks up — applying to nobody and raising nothing.

Attribution stores the raw Discord snowflake, never a bot-local user id, so
this ports into Map Manager's Alliance section later without a translation
layer (`findByDiscordUserId` already resolves exactly that). `actor_guild_id`
is the join key to MM's `discord_guild_links`.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

# Identity comes from the installed engine package. Optional, like the engine
# itself: a failed install must degrade this feature, never break bot startup.
try:
    from champion_duel_engine.names import normalize_name

    NAMES_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the degraded-mode path
    NAMES_AVAILABLE = False

    def normalize_name(name):
        """Fallback that refuses to guess rather than inventing a second rule.

        A near-miss normalization is worse than none: it would silently file
        edits under keys the simulator cannot find. Callers check
        NAMES_AVAILABLE and return 503 instead.
        """
        raise RuntimeError("champion_duel_engine is not installed")


DB_PATH = os.getenv("CHAMPION_DUEL_DB_PATH", "/app/data/champion_duel.sqlite3")

# Session lifetime. Long enough that a scout is not re-authing mid-event,
# short enough that a stolen token is not indefinite.
SESSION_TTL = timedelta(days=30)

# The OAuth hand-off code is single-use and short-lived; it exists only to keep
# the session token out of the redirect URL, browser history and any log.
AUTH_CODE_TTL = timedelta(seconds=60)

VALID_SOURCES = ("observed", "estimated", "edited")
VALID_TYPES = ("Tank", "Missile", "Aircraft")


def _now() -> str:
    """UTC ISO-8601. Stored as TEXT so it sorts lexicographically, which is
    what the admin date-range export filters on."""
    return datetime.now(timezone.utc).isoformat()


def _hash(token: str) -> str:
    """Tokens are stored hashed. This repo is public and the volume is
    snapshottable; neither should ever yield a usable credential."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # WAL lets the admin export read while a scout writes. Single process, but
    # aiohttp handlers run concurrently in threads.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if absent and apply pending migrations.

    Same shape as `config.init_db`: each ALTER in its own try/except so a
    re-run is harmless, and the CREATE TABLE above it stays in sync for fresh
    databases.
    """
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS registrants (
                player_key   TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                grp          TEXT,
                rank         INTEGER,
                server       TEXT,
                alliance     TEXT,
                thp          REAL,
                fsp          REAL,
                seeded       INTEGER NOT NULL DEFAULT 0,
                updated_at   TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS squads (
                player_key  TEXT NOT NULL,
                slot        INTEGER NOT NULL,
                squad_type  TEXT,
                power       REAL,
                source      TEXT NOT NULL,
                observed_at TEXT,
                updated_at  TEXT NOT NULL,
                updated_by  TEXT,
                PRIMARY KEY (player_key, slot)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS order_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                player_key  TEXT NOT NULL,
                slot1       TEXT NOT NULL,
                slot2       TEXT NOT NULL,
                slot3       TEXT NOT NULL,
                opponent    TEXT,
                observed_at TEXT,
                source      TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                created_by  TEXT
            )
        """)
        # Append-only. A revert never updates or deletes a row here; it writes
        # a new one carrying revert_of, so the history stays the whole truth.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS edits (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                target           TEXT NOT NULL,
                player_key       TEXT NOT NULL,
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
        # Carries identity, not a token -- see create_auth_code for why that
        # is what keeps a live credential off the volume entirely.
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
            "CREATE INDEX IF NOT EXISTS ix_edits_created ON edits(created_at)",
            "CREATE INDEX IF NOT EXISTS ix_edits_player ON edits(player_key)",
            "CREATE INDEX IF NOT EXISTS ix_edits_actor ON edits(actor_discord_id)",
            "CREATE INDEX IF NOT EXISTS ix_orders_player ON order_history(player_key)",
            "CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(discord_user_id)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:  # pragma: no cover
                print(f"[CHAMPION_DUEL] index skipped: {exc}")

        # Migration block. Add ALTER TABLE entries here, each in its own
        # try/except, and update the CREATE TABLE above to match.
        for table, column, ddl in ():  # none yet
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                print(f"[CHAMPION_DUEL] Added {column} to {table}")
            except sqlite3.OperationalError:
                pass


# ── Registrants ───────────────────────────────────────────────────────────────


def import_registrants(rows: list[dict]) -> dict:
    """Bulk-load the roster. Upsert by player key; never touches scouting.

    Returns counts so the caller can report what actually changed rather than
    claiming success. A re-import after a roster refresh is expected and safe.
    """
    inserted = updated = 0
    now = _now()
    with _get_conn() as conn:
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            key = normalize_name(name)
            existing = conn.execute(
                "SELECT 1 FROM registrants WHERE player_key = ?", (key,)
            ).fetchone()
            conn.execute(
                """
                INSERT INTO registrants
                    (player_key, display_name, grp, rank, server, alliance,
                     thp, fsp, seeded, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    grp          = excluded.grp,
                    rank         = excluded.rank,
                    server       = excluded.server,
                    alliance     = excluded.alliance,
                    thp          = excluded.thp,
                    fsp          = excluded.fsp,
                    seeded       = excluded.seeded,
                    updated_at   = excluded.updated_at
                """,
                (
                    key,
                    name,
                    row.get("group"),
                    row.get("rank"),
                    row.get("server"),
                    row.get("alliance"),
                    row.get("thp"),
                    row.get("fsp"),
                    1 if row.get("seeded") else 0,
                    now,
                ),
            )
            if existing:
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


def get_roster(group: str | None = None, include_scouting: bool = False) -> list[dict]:
    """Registrants, optionally with their squads.

    `include_scouting` is False for anonymous callers. Squad composition and
    deployment orders are our own scouting, not public LWS data — the roster
    itself can be read by anyone, what we know about their lineups cannot.
    """
    sql = "SELECT * FROM registrants"
    params: tuple = ()
    if group:
        sql += " WHERE grp = ?"
        params = (group,)
    sql += " ORDER BY grp, rank"

    with _get_conn() as conn:
        players = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if include_scouting and players:
            keys = {p["player_key"] for p in players}
            by_key: dict[str, list] = {k: [] for k in keys}
            for r in conn.execute("SELECT * FROM squads ORDER BY player_key, slot").fetchall():
                if r["player_key"] in by_key:
                    by_key[r["player_key"]].append(dict(r))
            for p in players:
                p["squads"] = by_key.get(p["player_key"], [])
    return players


def get_player(name: str, include_scouting: bool = False) -> dict | None:
    key = normalize_name(name)
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM registrants WHERE player_key = ?", (key,)).fetchone()
        if row is None:
            return None
        player = dict(row)
        if include_scouting:
            player["squads"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM squads WHERE player_key = ? ORDER BY slot", (key,)
                ).fetchall()
            ]
            player["orders"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM order_history WHERE player_key = ? "
                    "ORDER BY COALESCE(observed_at, created_at) DESC",
                    (key,),
                ).fetchall()
            ]
    return player


# ── Writes (each one audited) ─────────────────────────────────────────────────


def _record_edit(conn, *, target, player_key, slot, field, old, new, actor, revert_of=None):
    cur = conn.execute(
        """
        INSERT INTO edits (target, player_key, slot, field, old_value, new_value,
                           actor_discord_id, actor_name, actor_guild_id,
                           created_at, revert_of)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target,
            player_key,
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


def set_squad(name, slot, squad_type=None, power=None, *, actor, source="edited"):
    """Correct one squad slot. Returns the edit ids written.

    Only the fields supplied are touched, and each field change is its own edit
    row — so an admin reverting "they set the type wrong" does not also revert
    a correct power entered in the same request.
    """
    if slot not in (1, 2, 3):
        raise ValueError("slot must be 1, 2 or 3")
    if squad_type is not None and squad_type not in VALID_TYPES:
        raise ValueError(f"squad_type must be one of {VALID_TYPES}")
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}")

    key = normalize_name(name)
    edit_ids = []
    with _get_conn() as conn:
        if not conn.execute("SELECT 1 FROM registrants WHERE player_key = ?", (key,)).fetchone():
            raise LookupError(f"no registrant matches {name!r}")

        row = conn.execute(
            "SELECT * FROM squads WHERE player_key = ? AND slot = ?", (key, slot)
        ).fetchone()
        old_type = row["squad_type"] if row else None
        old_power = row["power"] if row else None

        new_type = old_type if squad_type is None else squad_type
        new_power = old_power if power is None else float(power)

        conn.execute(
            """
            INSERT INTO squads (player_key, slot, squad_type, power, source,
                                updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_key, slot) DO UPDATE SET
                squad_type = excluded.squad_type,
                power      = excluded.power,
                source     = excluded.source,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (key, slot, new_type, new_power, source, _now(), actor["discord_user_id"]),
        )
        if squad_type is not None and old_type != new_type:
            edit_ids.append(
                _record_edit(
                    conn,
                    target="squad",
                    player_key=key,
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
                    player_key=key,
                    slot=slot,
                    field="power",
                    old=old_power,
                    new=new_power,
                    actor=actor,
                )
            )
    return {"player_key": key, "slot": slot, "edit_ids": edit_ids}


def add_order(name, slots, *, actor, opponent=None, observed_at=None, source="observed"):
    """Record a deployment order actually seen. Repeats are meaningful.

    A player seen five times in one order and once in another should be
    sampled 5:1, so this appends rather than replacing — the frequency is the
    signal `predict_matchup` consumes.
    """
    if len(slots) != 3 or any(s not in VALID_TYPES for s in slots):
        raise ValueError(f"slots must be three of {VALID_TYPES}")

    key = normalize_name(name)
    with _get_conn() as conn:
        if not conn.execute("SELECT 1 FROM registrants WHERE player_key = ?", (key,)).fetchone():
            raise LookupError(f"no registrant matches {name!r}")
        cur = conn.execute(
            """
            INSERT INTO order_history
                (player_key, slot1, slot2, slot3, opponent, observed_at,
                 source, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
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
            player_key=key,
            slot=None,
            field="order",
            old=None,
            new="/".join(slots),
            actor=actor,
        )
    return {"player_key": key, "order_id": order_id, "edit_ids": [edit_id]}


# ── Audit + revert ────────────────────────────────────────────────────────────


def list_edits(*, since=None, until=None, player=None, actor=None, limit=50, offset=0):
    """Newest first. `since`/`until` are ISO-8601; both are inclusive of the
    day boundary the caller passes, which is why timestamps are stored as
    sortable text."""
    sql = "SELECT * FROM edits WHERE 1=1"
    params: list = []
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    if until:
        sql += " AND created_at <= ?"
        params.append(until)
    if player:
        sql += " AND player_key = ?"
        params.append(normalize_name(player))
    if actor:
        sql += " AND actor_discord_id = ?"
        params.append(str(actor))
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with _get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM edits WHERE 1=1"
            + (" AND created_at >= ?" if since else "")
            + (" AND created_at <= ?" if until else "")
            + (" AND player_key = ?" if player else "")
            + (" AND actor_discord_id = ?" if actor else ""),
            params[:-2],
        ).fetchone()["n"]
    return {"edits": rows, "total": total}


class RevertConflict(Exception):
    """The value moved on after the edit being reverted.

    Carries the current value so the caller can show what it found instead of
    a bare failure — an admin needs to see the newer correction before
    deciding whether to stamp on it.
    """

    def __init__(self, current, expected):
        super().__init__(f"value is now {current!r}, expected {expected!r}")
        self.current = current
        self.expected = expected


def revert_edit(edit_id: int, *, actor, force: bool = False) -> dict:
    """Restore the value an edit replaced, as a new append-only edit.

    Optimistically checked: if the field has changed again since, this raises
    RevertConflict rather than silently clobbering the newer correction. That
    matters because two scouts can be entering sightings for the same player at
    once, and the later one is usually the better information.

    Order edits are not revertable this way — an order is an appended
    observation, not a replaced value, so there is nothing to restore. Deleting
    a bad sighting is a separate operation and deliberately not folded in here.
    """
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
        if row is None:
            raise LookupError(f"no edit {edit_id}")
        if row["target"] != "squad":
            raise ValueError("only squad edits can be reverted")

        key, slot, field = row["player_key"], row["slot"], row["field"]
        # `field` reaches an UPDATE by name. It can only ever be one of ours,
        # but whitelisting means a corrupt or hand-edited audit row can't turn
        # into arbitrary SQL.
        if field not in ("squad_type", "power"):
            raise ValueError(f"unrevertable field {field!r}")
        current_row = conn.execute(
            "SELECT * FROM squads WHERE player_key = ? AND slot = ?", (key, slot)
        ).fetchone()
        current = None if current_row is None else current_row[field]

        # Compare as text: the column is typed but the audit row is not.
        if not force and (current is None) != (row["new_value"] is None):
            raise RevertConflict(current, row["new_value"])
        if not force and current is not None and str(current) != str(row["new_value"]):
            raise RevertConflict(current, row["new_value"])

        restored = row["old_value"]
        if field == "power":
            restored_typed = None if restored is None else float(restored)
        else:
            restored_typed = restored

        conn.execute(
            f"UPDATE squads SET {field} = ?, updated_at = ?, updated_by = ?, "  # noqa: S608
            "source = 'edited' WHERE player_key = ? AND slot = ?",
            (restored_typed, _now(), actor["discord_user_id"], key, slot),
        )
        new_id = _record_edit(
            conn,
            target="squad",
            player_key=key,
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

    Browsing a long history in Discord is worse than a spreadsheet, so this is
    the escape hatch rather than an afterthought. Oldest-first because it reads
    as a narrative of what happened.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT e.*, r.display_name FROM edits e "
            "LEFT JOIN registrants r ON r.player_key = e.player_key "
            "WHERE e.created_at >= ? AND e.created_at <= ? ORDER BY e.id ASC",
            (start, end),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Sessions ──────────────────────────────────────────────────────────────────


def create_session(discord_user_id, discord_name=None, can_write=False, writer_guild_id=None):
    """Mint a session. Returns the plaintext token exactly once — only its
    hash is stored, so it cannot be recovered from the volume afterwards."""
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
    """Resolve a session token, or None if unknown, expired or revoked."""
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
    """Refresh the cached premium verdict on a session.

    `premium.is_premium` has its own 5-minute cache, but re-scanning every
    guild the bot is in on every write would be wasteful; this stamps the
    answer so the scan runs on a slower cadence.
    """
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
    """Drop dead sessions and spent hand-off codes. Cheap; run on a loop."""
    with _get_conn() as conn:
        n = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),)).rowcount
        conn.execute("DELETE FROM auth_codes WHERE expires_at <= ?", (_now(),))
    return n


# ── OAuth hand-off codes ──────────────────────────────────────────────────────


def create_auth_code(discord_user_id, discord_name=None, can_write=False, writer_guild_id=None):
    """One-time code the browser carries back from the OAuth callback.

    It holds the *resolved identity*, not a session token. The session is not
    minted until the code is redeemed, which is what lets `sessions` store only
    a hash: if the callback minted the token up front, redeeming a code would
    have to hand back a plaintext token that no longer exists anywhere, and the
    only way to make that work would be storing the live token on the volume.

    The code exists at all so the token never rides in the redirect URL, where
    it would land in browser history, the Referer header and any proxy log.
    Single-use, one minute.
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
    """Redeem a code for the identity behind it, once.

    None if unknown, expired or already used — all three answer the same, so a
    caller cannot probe which codes ever existed.
    """
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

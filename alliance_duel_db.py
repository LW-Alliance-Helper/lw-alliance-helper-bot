"""VS score storage — its own SQLite file on the Railway volume (#544).

The missing member of the `alliance_duel_*` family. Every other feature owns a
`<feature>_*` module set and Champion Duel's includes `champion_duel_db`; VS
had ten modules and no data layer, because until now every score lived in the
alliance's own Google Sheet and nowhere else.

**Why a third database rather than a table in one of the two.** `config.py`'s
`guild_configs.db` is per-guild and private, which is the wrong footing: these
rows are keyed to the in-game alliance and must outlive the Discord server that
recorded them. `champion_duel.sqlite3` has the right footing — global game
records contributed across alliances and servers — but it is a tournament with
groupings, rounds and stages, and a league season is a different grain
entirely. Kevin's call, 2026-09-04, after all three were priced with nothing
yet shipped to `main`.

**Keyed to the alliance (tag + warzone), never to the guild.** Two reasons and
the second is load-bearing:

- A knowledge base that dies when a guild churns is not a knowledge base. Keyed
  to the alliance, what one alliance records about another outlives their
  Discord server.
- Discord's Developer Policy asks that data specific to a server be deleted
  when the bot is removed from it. Guild-keyed scores would be exactly that.
  Alliance-keyed scores are game-world records, which is the footing Champion
  Duel already stands on.

**The sheet still wins.** This is a second copy for the benefit of alliances
that never played each other, not the source of truth. The alliance's own tab
remains the entry surface and remains authoritative for its own view; nothing
here is read back over a row the guild can see in its own sheet.

**On uninstall: scrub the attribution, keep the scores.** Three columns say who
recorded a row; a removal clears those three and leaves the numbers, because
the numbers are what the game showed and the other fifteen alliances in the
bracket contributed to the same league. After the scrub there is no
server-specific data left in a row: a tag, a warzone, and numbers.

Everything here is **synchronous**, the same as `champion_duel_db`. Callers must
wrap these in `asyncio.to_thread` or a query stalls the Discord gateway
heartbeat for the whole process (#366).
"""

import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("ALLIANCE_DUEL_DB_PATH", "/app/data/alliance_duel.sqlite3")

#: Days in a duel week. Kept as a literal rather than imported from
#: `alliance_duel` so the schema does not move if the feature's constant does:
#: a stored row is a record of what the game ran, not of what we currently
#: believe a week looks like.
DAYS_IN_WEEK = 6

VALID_OUTCOMES = ("W", "L")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if absent. Same shape as the other two data layers.

    Every statement is `IF NOT EXISTS` and every migration goes in its own
    try/except below, so a re-run is harmless and a fresh file and an upgraded
    one end up identical.
    """
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with _get_conn() as conn:
        # One row per alliance per league-week: the grain of the whole feature,
        # and the same grain as `alliance_duel.AllianceWeek`.
        #
        # `tag` and `warzone` are the normalised comparison key, not the display
        # form. `AllianceKey.of` is the one definition of that normalisation and
        # `_key_parts` below delegates to it rather than restating it -- two
        # copies is exactly how one surface starts disagreeing with another
        # about whether two rows are the same alliance.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alliance_weeks (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                tag              TEXT    NOT NULL,
                warzone          TEXT    NOT NULL,
                season           TEXT    NOT NULL,
                tier             TEXT    NOT NULL DEFAULT '',
                grp              TEXT    NOT NULL DEFAULT '',
                week             INTEGER NOT NULL,
                week_date        TEXT,
                ranking          INTEGER,
                tag_display      TEXT,
                warzone_display  TEXT,
                power            INTEGER,
                members          INTEGER,
                gift_level       INTEGER,
                opponent_tag     TEXT,
                opponent_warzone TEXT,
                week_score       INTEGER,
                week_outcome     TEXT,
                actor_discord_id TEXT,
                actor_name       TEXT,
                actor_guild_id   TEXT,
                created_at       TEXT    NOT NULL,
                updated_at       TEXT    NOT NULL,
                UNIQUE (tag, warzone, season, tier, grp, week)
            )
        """)

        # Day scores hang off the week rather than widening it by twelve
        # columns. A day is a real observation with its own presence: "day 3 is
        # recorded and day 4 is not" is a state the sheet can be in and the
        # screens already render, and twelve nullable columns say that far
        # worse than six optional rows do.
        #
        # CASCADE is the safety net, not the removal path. Guild removal
        # *scrubs* `alliance_weeks` and keeps the row, so nothing here is
        # deleted by a server dropping the bot.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS alliance_week_days (
                week_id INTEGER NOT NULL REFERENCES alliance_weeks(id) ON DELETE CASCADE,
                day     INTEGER NOT NULL,
                score   INTEGER,
                outcome TEXT,
                PRIMARY KEY (week_id, day)
            )
        """)

        # Reads are "everything about this alliance" and "everything in this
        # league", in that order of frequency: scouting an opponent is the
        # question this table exists to answer.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alliance_weeks_alliance "
            "ON alliance_weeks (tag, warzone)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alliance_weeks_league "
            "ON alliance_weeks (season, tier, grp, week)"
        )
        # The removal sweep walks this column across every row.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_alliance_weeks_actor_guild "
            "ON alliance_weeks (actor_guild_id)"
        )
        conn.commit()


# ── Identity ──────────────────────────────────────────────────────────────────


def _key_parts(alliance) -> tuple[str, str] | None:
    """`(tag, warzone)` for storage, normalised exactly as the feature does.

    Delegates to `alliance_duel.AllianceKey.of` rather than restating the rule.
    Imported inside the function because this module is a leaf: `alliance_duel`
    must be free to import it later without a cycle.
    """
    import alliance_duel as ad

    if alliance is None:
        return None
    key = alliance if isinstance(alliance, ad.AllianceKey) else ad.AllianceKey.of(*alliance)
    if key is None:
        return None
    return key.tag, key.warzone


def _league_parts(league) -> tuple[str, str, str] | None:
    """`(season, tier, group)` for storage.

    A season is required and the other two are not: `LeagueKey.of` already
    treats a missing tier or group as an empty string rather than a failure,
    because the League screen does not always show all three.
    """
    if league is None or not getattr(league, "season", ""):
        return None
    return league.season, league.tier or "", league.group or ""


# ── Writing ───────────────────────────────────────────────────────────────────


def record_weeks(rows, *, actor=None) -> dict:
    """Store what these rows say, without ever losing what is already stored.

    Returns `{"written": n, "skipped": n}`. A row is skipped rather than raised
    on when it carries no usable identity: this runs behind an entry surface
    that has already refused what it could, and a single unreadable row must
    not cost the officer the fifteen good ones beside it.

    **Nothing here overwrites a value with a blank.** The sheet is the entry
    surface and it is the authority: a field the caller has nothing to say
    about arrives as `None`, and a `None` leaves whatever is stored in place.
    That is the same non-clobbering rule `alliance_duel.row_values` applies to
    the sheet, for the same reason -- a screen that happens not to know a number
    must never erase somebody else's record of it.

    `actor` carries the three attribution columns. It is optional because a
    backfill has no author, and an unattributed row is a perfectly good record:
    the attribution is what a removal takes away, so a row must be able to live
    without it.
    """
    written = 0
    skipped = 0
    stamp = _now()
    aid, aname, aguild = _actor_columns(actor)

    with _get_conn() as conn:
        for row in rows:
            key = _key_parts(getattr(row, "alliance", None))
            league = _league_parts(getattr(row, "league", None))
            week = getattr(row, "week", None)
            if key is None or league is None or not week:
                skipped += 1
                continue

            tag, warzone = key
            season, tier, grp = league
            where = (tag, warzone, season, tier, grp, int(week))

            existing = conn.execute(
                "SELECT id FROM alliance_weeks WHERE tag = ? AND warzone = ? "
                "AND season = ? AND tier = ? AND grp = ? AND week = ?",
                where,
            ).fetchone()

            values = _week_columns(row)
            # Column by column, not all three together. A save that knows the
            # guild but not the person -- which is most of them -- would
            # otherwise NULL a name somebody else's save had recorded, and that
            # is the one rule this module exists to keep.
            for column, value in (
                ("actor_discord_id", aid),
                ("actor_name", aname),
                ("actor_guild_id", aguild),
            ):
                if value is not None:
                    values[column] = value

            if existing is None:
                columns = (
                    ["tag", "warzone", "season", "tier", "grp", "week"]
                    + list(values)
                    + ["created_at", "updated_at"]
                )
                placeholders = ", ".join("?" for _ in columns)
                conn.execute(
                    f"INSERT INTO alliance_weeks ({', '.join(columns)}) "  # noqa: S608
                    f"VALUES ({placeholders})",
                    [*where, *values.values(), stamp, stamp],
                )
                week_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                week_id = existing["id"]
                if values:
                    assignments = ", ".join(f"{c} = ?" for c in values)
                    conn.execute(
                        f"UPDATE alliance_weeks SET {assignments}, updated_at = ? "  # noqa: S608
                        "WHERE id = ?",
                        [*values.values(), stamp, week_id],
                    )

            _write_days(conn, week_id, row)
            written += 1
        conn.commit()

    return {"written": written, "skipped": skipped}


def _actor_columns(actor) -> tuple[str | None, str | None, str | None]:
    """The three attribution columns, from whatever the caller had.

    Accepts a mapping or an object, because the entry surfaces hold a Discord
    interaction and a backfill holds nothing at all.
    """
    if actor is None:
        return None, None, None
    get = actor.get if isinstance(actor, dict) else lambda k, d=None: getattr(actor, k, d)
    discord_id = get("discord_user_id") or get("discord_id") or get("id")
    name = get("discord_name") or get("name") or get("display_name")
    guild_id = get("guild_id")
    return (
        str(discord_id) if discord_id is not None else None,
        str(name) if name is not None else None,
        str(guild_id) if guild_id is not None else None,
    )


def _week_columns(row) -> dict:
    """The week-level columns this row actually has something to say about.

    A `None` is left out entirely rather than written, which is what makes the
    upsert non-clobbering. Ranking and the display forms are included because
    they are identity rather than measurement, and an alliance that renamed
    itself should read as its current name everywhere.
    """
    out: dict = {}
    opponent = _key_parts(getattr(row, "opponent", None))

    candidates = {
        "week_date": getattr(getattr(row, "week_date", None), "isoformat", lambda: None)(),
        "ranking": getattr(row, "ranking", None),
        "tag_display": getattr(row, "tag_display", "") or None,
        "warzone_display": getattr(row, "warzone_display", "") or None,
        "power": getattr(row, "power", None),
        "members": getattr(row, "members", None),
        "gift_level": getattr(row, "gift_level", None),
        "opponent_tag": opponent[0] if opponent else None,
        "opponent_warzone": opponent[1] if opponent else None,
        "week_score": getattr(row, "week_score", None),
        "week_outcome": getattr(row, "week_outcome", None) or None,
    }
    for column, value in candidates.items():
        if value is not None:
            out[column] = value
    return out


def _write_days(conn, week_id: int, row) -> None:
    """Upsert the days this row knows about, and only those.

    A day the caller says nothing about keeps whatever is stored, for the same
    reason the week columns do. Score and outcome are written independently
    because the screens record them independently: a day score arrives the
    evening it is played, and the outcome can arrive with it or a day later.
    """
    scores = getattr(row, "day_scores", None) or {}
    outcomes = getattr(row, "day_outcomes", None) or {}
    for day in range(1, DAYS_IN_WEEK + 1):
        score = scores.get(day)
        outcome = outcomes.get(day)
        if score is None and outcome is None:
            continue
        conn.execute(
            "INSERT INTO alliance_week_days (week_id, day, score, outcome) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(week_id, day) DO UPDATE SET "
            "  score = COALESCE(excluded.score, alliance_week_days.score), "
            "  outcome = COALESCE(excluded.outcome, alliance_week_days.outcome)",
            (week_id, day, score, outcome if outcome in VALID_OUTCOMES else None),
        )


# ── Reading ───────────────────────────────────────────────────────────────────


def weeks_for_alliance(alliance, *, season: str | None = None) -> list[dict]:
    """Everything stored about one alliance, oldest league-week first.

    This is the question the table exists to answer: an alliance about to face
    somebody they have never played can now read what fifteen other alliances
    recorded about them.
    """
    key = _key_parts(alliance)
    if key is None:
        return []
    sql = "SELECT * FROM alliance_weeks WHERE tag = ? AND warzone = ?"
    params: list = list(key)
    if season:
        sql += " AND season = ?"
        params.append(season)
    sql += " ORDER BY season, tier, grp, week"
    return _with_days(sql, params)


def weeks_for_league(league, *, week: int | None = None) -> list[dict]:
    """Every alliance's row for one league, for filling a bracket from what
    other alliances already recorded."""
    parts = _league_parts(league)
    if parts is None:
        return []
    sql = "SELECT * FROM alliance_weeks WHERE season = ? AND tier = ? AND grp = ?"
    params: list = list(parts)
    if week:
        sql += " AND week = ?"
        params.append(int(week))
    sql += " ORDER BY week, ranking"
    return _with_days(sql, params)


def _with_days(sql: str, params: list) -> list[dict]:
    """Run a week query and attach each row's days.

    One extra query for the whole result rather than one per row: a league is
    sixteen alliances times four weeks, and sixty-four round trips to answer one
    screen is how a hub starts feeling slow.
    """
    with _get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        placeholders = ", ".join("?" for _ in ids)
        days = conn.execute(
            f"SELECT * FROM alliance_week_days WHERE week_id IN ({placeholders}) "  # noqa: S608
            "ORDER BY week_id, day",
            ids,
        ).fetchall()

    by_week: dict[int, dict] = {}
    for day in days:
        entry = by_week.setdefault(day["week_id"], {"day_scores": {}, "day_outcomes": {}})
        if day["score"] is not None:
            entry["day_scores"][day["day"]] = day["score"]
        if day["outcome"] is not None:
            entry["day_outcomes"][day["day"]] = day["outcome"]

    for row in rows:
        found = by_week.get(row["id"], {})
        row["day_scores"] = found.get("day_scores", {})
        row["day_outcomes"] = found.get("day_outcomes", {})
    return rows


# ── Guild removal (#543) ──────────────────────────────────────────────────────
#
# Nothing here is deleted. A score is a reading of what the game showed sixteen
# alliances, and fifteen of them are not this server's to remove; the three
# columns saying who typed it in are. That is the same call `champion_duel_db`
# makes about a reading, and this file follows it rather than inventing a
# second rule.
#
# Consequence, and it is what makes this cheap: after the scrub there is no
# server-specific data left in a row, so the `GUILD_DELETE` obligation is met
# by scrubbing rather than deleting, and the history survives.

_GUILD_REMOVAL_DELETES: tuple[tuple[str, str], ...] = ()

_GUILD_REMOVAL_SCRUBS: tuple[tuple[str, str, str], ...] = (
    (
        "alliance_weeks",
        "actor_discord_id = NULL, actor_name = NULL, actor_guild_id = NULL",
        "actor_guild_id = :gid",
    ),
)


def _run_spec(out: dict, params: dict, deletes, scrubs, *, apply: bool) -> dict:
    """Walk a delete spec and a scrub spec, counting or doing.

    One implementation for both removals, so the guild path and the personal
    path cannot drift into answering differently. `apply=False` runs the same
    predicates and counts instead: a preview that ran a different query from
    the run would be worth less than no preview at all.
    """
    with _get_conn() as conn:
        for table, where in deletes:
            if apply:
                n = conn.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount  # noqa: S608
            else:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}",  # noqa: S608
                    params,
                ).fetchone()[0]
            if n:
                out["deleted"][table] = n

        for table, sets, where in scrubs:
            if apply:
                n = conn.execute(
                    f"UPDATE {table} SET {sets} WHERE {where}",  # noqa: S608
                    params,
                ).rowcount
            else:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}",  # noqa: S608
                    params,
                ).fetchone()[0]
            if n:
                out["scrubbed"][table] = n
        if apply:
            conn.commit()
    return out


# A person's own removal (#517), which is a different rule from a server's.
# A guild removal takes the three attribution columns because the *server* is
# what is leaving. A personal removal takes only the two that name a human: the
# guild id is a fact about which server recorded a league result, and it is not
# this person's to take with them.
#
# The reading itself stays either way. Sixteen alliances played that league.
_REMOVAL_DELETES: tuple[tuple[str, str], ...] = ()

_REMOVAL_SCRUBS: tuple[tuple[str, str, str], ...] = (
    (
        "alliance_weeks",
        "actor_discord_id = NULL, actor_name = NULL",
        "actor_discord_id = :sid",
    ),
)


def purge_user_data(discord_user_id, *, apply: bool = False) -> dict:
    """Remove one person from the VS scores.

    Same shape and same return as the guild purge and as both other databases'
    personal removals, including the `apply=False` dry run: a preview that ran
    a different query from the run would be worth less than no preview at all.
    """
    sid = str(discord_user_id).strip()
    out: dict = {"deleted": {}, "scrubbed": {}, "applied": bool(apply)}
    if not sid:
        return out
    return _run_spec(out, {"sid": sid}, _REMOVAL_DELETES, _REMOVAL_SCRUBS, apply=apply)


def purge_guild_data(guild_id: int, *, apply: bool = False) -> dict:
    """Remove one server's traces from the VS scores.

    Same shape and same return as the other two purges, including the
    `apply=False` dry run, and it walks the specs through the same `_run_spec`
    the personal removal does.
    """
    gid = str(int(guild_id))
    out: dict = {"deleted": {}, "scrubbed": {}, "applied": bool(apply)}
    return _run_spec(out, {"gid": gid}, _GUILD_REMOVAL_DELETES, _GUILD_REMOVAL_SCRUBS, apply=apply)

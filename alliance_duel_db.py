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
    """Create tables if absent, and bring a first-shape store forward (#658).

    Every statement is `IF NOT EXISTS`, so a re-run is harmless and a fresh
    file and an upgraded one end up identical. The one migration that cannot
    be an `ALTER` is the move to league identity: SQLite cannot drop a table's
    `UNIQUE` constraint, so a store still keyed on the label is rebuilt once,
    merging what the old key had split, and then grouped into leagues.
    """
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with _get_conn() as conn:
        if _LABEL_KEYED_MARKER in _table_sql(conn, "alliance_weeks"):
            _rebuild_label_keyed(conn)
        _create_tables(conn)
        _create_indexes(conn)
        conn.commit()


#: The constraint only the first shape of the table had: one row per alliance
#: per *label* per week. Its presence is what says the rebuild has not run.
_LABEL_KEYED_MARKER = "UNIQUE (tag, warzone, season, tier, grp, week)"

#: How far apart two weeks of one league can be. A league is four
#: consecutive weeks, so its first and last Mondays are 21 days apart. Two
#: rows under one label further apart than this are two leagues: the game
#: hands the same label to a later set of warzones (#658).
LEAGUE_SPAN_DAYS = 21


def _table_sql(conn, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return (row[0] or "") if row else ""


def _create_tables(conn) -> None:
    # One row per alliance per duel week: the grain of the whole feature, and
    # the same grain as `alliance_duel.AllianceWeek`.
    #
    # **Keyed on the alliance and the week's Monday, not on the league's
    # label** (#658). An alliance is in exactly one league in any week, so
    # that pair names one real observation however each server spelled the
    # league, and a corrected label moves the row rather than splitting it.
    # The label is a detail on the row, latest write wins, like power. The
    # key lives in two partial unique indexes (`_create_indexes`) because a
    # row typed straight into a Sheet can arrive without a date, and those
    # keep the label-based key they always had.
    #
    # `league_id` is which real bracket the row belongs to, and the only
    # thing reads join on (`_link`). `tag` and `warzone` are the normalised
    # comparison key, not the display form. `AllianceKey.of` is the one
    # definition of that normalisation and `_key_parts` below delegates to it
    # rather than restating it -- two copies is exactly how one surface starts
    # disagreeing with another about whether two rows are the same alliance.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alliance_weeks (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            league_id        INTEGER,
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
            updated_at       TEXT    NOT NULL
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


def _create_indexes(conn) -> None:
    # The row key (#658): alliance and week date, or for an undated row the
    # alliance and its label and week number.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_alliance_weeks_dated "
        "ON alliance_weeks (tag, warzone, week_date) WHERE week_date IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_alliance_weeks_undated "
        "ON alliance_weeks (tag, warzone, season, tier, grp, week) WHERE week_date IS NULL"
    )
    # Reads are "everything about this alliance" and "everything in this
    # league", in that order of frequency: scouting an opponent is the
    # question this table exists to answer.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alliance_weeks_alliance ON alliance_weeks (tag, warzone)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alliance_weeks_league_id ON alliance_weeks (league_id)"
    )
    # The label index serves the linking pass, which looks for rows sharing
    # a label within one league's span, and the opponent index its reverse
    # pairing lookup.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alliance_weeks_label "
        "ON alliance_weeks (season, tier, grp, week_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alliance_weeks_opponent "
        "ON alliance_weeks (opponent_tag, opponent_warzone, week_date)"
    )
    # The removal sweep walks this column across every row.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_alliance_weeks_actor_guild "
        "ON alliance_weeks (actor_guild_id)"
    )


def _rebuild_label_keyed(conn) -> None:
    """Move a first-shape store onto the alliance-and-date key, once (#658).

    The old rows are replayed oldest first through the same merge a live
    write uses, so where the label key had split one alliance-week in two
    (two servers' spellings, a corrected season), the later reading wins
    field by field, a blank never erases, and the first server to record the
    row keeps the attribution. Their days follow them. Then every row is
    grouped into its league.

    All in one transaction with foreign keys off, which is the documented way
    to rebuild a SQLite table: an interruption leaves the old tables in place
    and the next start runs it again.
    """
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN")
        for index in (
            "idx_alliance_weeks_alliance",
            "idx_alliance_weeks_league",
            "idx_alliance_weeks_actor_guild",
        ):
            conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.execute("ALTER TABLE alliance_weeks RENAME TO alliance_weeks_label_keyed")
        conn.execute("ALTER TABLE alliance_week_days RENAME TO alliance_week_days_label_keyed")
        _create_tables(conn)
        _create_indexes(conn)

        days: dict[int, list] = {}
        for day in conn.execute("SELECT * FROM alliance_week_days_label_keyed"):
            days.setdefault(day["week_id"], []).append(day)

        moved = []
        for old in conn.execute(
            "SELECT * FROM alliance_weeks_label_keyed ORDER BY updated_at, id"
        ).fetchall():
            record = dict(old)
            league = _league_parts(_Label(record["season"], record["tier"], record["grp"]))
            if league is None:
                continue
            record["season"], record["tier"], record["grp"] = league
            week_id = _merge_record(conn, record)
            for day in days.get(old["id"], []):
                _upsert_day(conn, week_id, day["day"], day["score"], day["outcome"])
            moved.append(week_id)

        conn.execute("DROP TABLE alliance_week_days_label_keyed")
        conn.execute("DROP TABLE alliance_weeks_label_keyed")
        _link(conn, sorted(set(moved)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


class _Label:
    """Three strings standing in for a `LeagueKey` where only the parts exist."""

    def __init__(self, season, tier, group):
        self.season, self.tier, self.group = season, tier, group


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
    """`(season, tier, group)` for storage, standardized (#658).

    A season is required and the other two are not: `LeagueKey.of` already
    treats a missing tier or group as an empty string rather than a failure,
    because the League screen does not always show all three. Standardized
    here as well as on `LeagueKey`, so a caller holding bare parts cannot
    store a second spelling.
    """
    import vs_labels

    if league is None or not getattr(league, "season", ""):
        return None
    return (
        vs_labels.standard_season(league.season),
        vs_labels.standard_tier(league.tier or ""),
        vs_labels.standard_group(league.group or ""),
    )


def _locate(conn, tag, warzone, season, tier, grp, week, week_date):
    """The stored row this observation belongs to, or None.

    Dated: the alliance's row for that Monday. Failing that, the same
    alliance-week under the same label with a different date, which is a
    corrected date rather than a new row, and the caller moves it. Undated:
    the label-keyed row, or a dated one for the same alliance-week under this
    label, which an undated write only fills in.
    """
    if week_date:
        found = conn.execute(
            "SELECT * FROM alliance_weeks WHERE tag = ? AND warzone = ? AND week_date = ?",
            (tag, warzone, week_date),
        ).fetchone()
        if found is not None:
            return found
    return conn.execute(
        "SELECT * FROM alliance_weeks WHERE tag = ? AND warzone = ? AND season = ? "
        "AND tier = ? AND grp = ? AND week = ? ORDER BY week_date IS NULL DESC LIMIT 1",
        (tag, warzone, season, tier, grp, int(week)),
    ).fetchone()


_ATTRIBUTION = ("actor_discord_id", "actor_name", "actor_guild_id")

#: Columns whose change can move a row into a different league, so a write
#: that changes one of them has to be linked again (`_link`).
_LINKING = ("season", "tier", "grp", "week_date", "opponent_tag", "opponent_warzone")


def _merge_record(conn, record: dict) -> int:
    """Insert or merge one row's worth of columns. Returns the row id.

    The rebuild's merge: every non-null value overwrites, attribution is kept
    from whichever row had it first, and the timestamps span both.
    """
    identity = (
        record["tag"],
        record["warzone"],
        record["season"],
        record["tier"],
        record["grp"],
        record["week"],
        record.get("week_date"),
    )
    existing = _locate(conn, *identity)
    skip = {"id", "league_id", "created_at", "updated_at", *_ATTRIBUTION}
    values = {c: v for c, v in record.items() if c not in skip and v is not None}
    if existing is None:
        for column in _ATTRIBUTION:
            if record.get(column) is not None:
                values[column] = record[column]
        columns = [*values, "created_at", "updated_at"]
        conn.execute(
            f"INSERT INTO alliance_weeks ({', '.join(columns)}) "  # noqa: S608
            f"VALUES ({', '.join('?' for _ in columns)})",
            [*values.values(), record["created_at"], record["updated_at"]],
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    for column in _ATTRIBUTION:
        if existing[column] is None and record.get(column) is not None:
            values[column] = record[column]
    values = {c: v for c, v in values.items() if existing[c] != v}
    created = min(existing["created_at"], record["created_at"])
    updated = max(existing["updated_at"], record["updated_at"])
    assignments = ", ".join(f"{c} = ?" for c in values)
    conn.execute(
        f"UPDATE alliance_weeks SET {assignments + ', ' if assignments else ''}"  # noqa: S608
        "created_at = ?, updated_at = ? WHERE id = ?",
        [*values.values(), created, updated, existing["id"]],
    )
    return existing["id"]


# ── Leagues (#658) ────────────────────────────────────────────────────────────
#
# A league label is not an identity: the game hands "S36 Diamond 12-1" to a
# later set of warzones a month on, and nobody on this side can rule out two
# brackets carrying it at once. So which real bracket a row belongs to is
# worked out from what the rows themselves say, and recorded as `league_id`.
# Two rows are the same league when:
#
# - they are the **same alliance** under one label within one league's span
#   (its four weeks);
# - **one server recorded them** under one label within one league's span (a
#   new league's sixteen skeleton rows are one bracket before anyone plays);
# - a **reported pairing** joins them: ABC says it played DEF that week, and
#   DEF has a row that week, whatever label either server used.
#
# Grouping spreads through those links. An undated row has no span, so it
# joins whatever its own server recorded under that label, or stays alone:
# never guessed into somebody else's league. Kevin, 28 Sep.


def _dates_around(week_date: str) -> tuple[str, str]:
    import datetime as _dt

    day = _dt.date.fromisoformat(week_date)
    span = _dt.timedelta(days=LEAGUE_SPAN_DAYS)
    return (day - span).isoformat(), (day + span).isoformat()


def _neighbours(conn, row) -> set[int]:
    """League ids of every row this one is linked to."""
    label = (row["season"], row["tier"], row["grp"])
    found: list = []
    if row["week_date"]:
        try:
            low, high = _dates_around(row["week_date"])
        except ValueError:
            low = high = None
        if low is not None:
            found += conn.execute(
                "SELECT league_id FROM alliance_weeks WHERE tag = ? AND warzone = ? "
                "AND season = ? AND tier = ? AND grp = ? AND week_date BETWEEN ? AND ?",
                (row["tag"], row["warzone"], *label, low, high),
            ).fetchall()
            if row["actor_guild_id"] is not None:
                found += conn.execute(
                    "SELECT league_id FROM alliance_weeks WHERE actor_guild_id = ? "
                    "AND season = ? AND tier = ? AND grp = ? AND week_date BETWEEN ? AND ?",
                    (row["actor_guild_id"], *label, low, high),
                ).fetchall()
        if row["opponent_tag"]:
            found += conn.execute(
                "SELECT league_id FROM alliance_weeks WHERE tag = ? AND warzone = ? "
                "AND week_date = ?",
                (row["opponent_tag"], row["opponent_warzone"], row["week_date"]),
            ).fetchall()
        found += conn.execute(
            "SELECT league_id FROM alliance_weeks WHERE opponent_tag = ? "
            "AND opponent_warzone = ? AND week_date = ?",
            (row["tag"], row["warzone"], row["week_date"]),
        ).fetchall()
    elif row["actor_guild_id"] is not None:
        found += conn.execute(
            "SELECT league_id FROM alliance_weeks WHERE actor_guild_id = ? "
            "AND season = ? AND tier = ? AND grp = ? AND id != ?",
            (row["actor_guild_id"], *label, row["id"]),
        ).fetchall()
    return {r[0] for r in found if r[0] is not None}


def _link(conn, ids) -> None:
    """Put each of these rows in its league, merging leagues it joins.

    Incremental: a row finds the leagues of everything it is linked to, and
    all of them become the lowest of their ids. Order does not matter for the
    result: a row linked to one not yet placed takes its own league, and the
    other joins it when its turn comes.
    """
    for row_id in ids:
        row = conn.execute("SELECT * FROM alliance_weeks WHERE id = ?", (row_id,)).fetchone()
        if row is None:
            continue
        leagues = _neighbours(conn, row)
        if row["league_id"] is not None:
            leagues.add(row["league_id"])
        target = min(leagues) if leagues else row["id"]
        conn.execute("UPDATE alliance_weeks SET league_id = ? WHERE id = ?", (target, row_id))
        for other in leagues - {target}:
            conn.execute(
                "UPDATE alliance_weeks SET league_id = ? WHERE league_id = ?", (target, other)
            )


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

    **The label is a detail, like power** (#658). A row is found by alliance
    and week date, so a server writing the same alliance-week under another
    spelling, or after correcting its season, updates the row rather than
    adding a second one, and the latest label wins.

    `actor` carries the three attribution columns. It is optional because a
    backfill has no author, and an unattributed row is a perfectly good record:
    the attribution is what a removal takes away, so a row must be able to live
    without it.
    """
    written = 0
    skipped = 0
    stamp = _now()
    aid, aname, aguild = _actor_columns(actor)
    relink: list[int] = []

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
            values = _week_columns(row)
            values.update(season=season, tier=tier, grp=grp, week=int(week))
            existing = _locate(
                conn, tag, warzone, season, tier, grp, int(week), values.get("week_date")
            )

            # Column by column, not all three together. A save that knows the
            # guild but not the person -- which is most of them -- would
            # otherwise NULL a name somebody else's save had recorded, and that
            # is the one rule this module exists to keep.
            #
            # **And never over another server's attribution.** These rows are
            # shared: sixteen alliances are in a bracket and any of their guilds
            # may hold a row about the same alliance-week. Letting the last
            # writer take the columns meant a guild merely opening `/vs` stamped
            # its own id over the guild that actually recorded it, and that
            # guild's removal then scrubbed nothing -- leaving a Discord id and
            # a display name behind permanently, which is the exact obligation
            # #543 exists to meet.
            #
            # So: claim an unattributed row, enrich our own, never touch anyone
            # else's.
            held = existing["actor_guild_id"] if existing is not None else None
            if held is None or aguild is None or held == aguild:
                for column, value in (
                    ("actor_discord_id", aid),
                    ("actor_name", aname),
                    ("actor_guild_id", aguild),
                ):
                    if value is not None:
                        values[column] = value

            if existing is None:
                columns = ["tag", "warzone", *values, "created_at", "updated_at"]
                conn.execute(
                    f"INSERT INTO alliance_weeks ({', '.join(columns)}) "  # noqa: S608
                    f"VALUES ({', '.join('?' for _ in columns)})",
                    [tag, warzone, *values.values(), stamp, stamp],
                )
                week_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                relink.append(week_id)
            else:
                week_id = existing["id"]
                # Drop anything already stored with this value. A repeat pass
                # over a whole tab is the common case -- every hub load runs the
                # backfill -- and an UPDATE that changes nothing still takes the
                # write lock for the length of the transaction.
                values = {c: v for c, v in values.items() if existing[c] != v}
                if values:
                    assignments = ", ".join(f"{c} = ?" for c in values)
                    conn.execute(
                        f"UPDATE alliance_weeks SET {assignments}, updated_at = ? "  # noqa: S608
                        "WHERE id = ?",
                        [*values.values(), stamp, week_id],
                    )
                # Only a change that can move the row between leagues costs a
                # linking pass, which keeps the every-hub-load backfill cheap.
                if existing["league_id"] is None or any(c in values for c in _LINKING):
                    relink.append(week_id)
                elif "actor_guild_id" in values:
                    relink.append(week_id)

            _write_days(conn, week_id, row)
            written += 1
        _link(conn, relink)
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
        _upsert_day(conn, week_id, day, score, outcome)


def _upsert_day(conn, week_id: int, day: int, score, outcome) -> None:
    """One day, filling in rather than blanking what is already stored."""
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


def weeks_for_bracket(pairs) -> list[dict]:
    """Every stored row in the league these alliance-weeks are in (#658).

    `pairs` is `(alliance, week_date)` for the rows a server already holds
    for its current league. Found by who is in the league and when, never by
    its label: the label is reused by the game, and joining on it would pool
    a bracket from a month ago, or a parallel one, into this one. Works the
    first time a server opens `/vs` too, before its own rows are stored,
    because other servers' rows for the same alliances and weeks carry the
    league id.
    """
    wanted = []
    for alliance, week_date in pairs:
        key = _key_parts(alliance)
        stamp = getattr(week_date, "isoformat", lambda: week_date)()
        if key is not None and stamp:
            wanted.append((*key, stamp))
    if not wanted:
        return []
    with _get_conn() as conn:
        leagues = set()
        for tag, warzone, stamp in wanted:
            found = conn.execute(
                "SELECT league_id FROM alliance_weeks WHERE tag = ? AND warzone = ? "
                "AND week_date = ?",
                (tag, warzone, stamp),
            ).fetchone()
            if found is not None and found[0] is not None:
                leagues.add(found[0])
    if not leagues:
        return []
    placeholders = ", ".join("?" for _ in leagues)
    return _with_days(
        f"SELECT * FROM alliance_weeks WHERE league_id IN ({placeholders}) "  # noqa: S608
        "ORDER BY week, ranking",
        sorted(leagues),
    )


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


def _to_row(record: dict):
    """One stored record as the `AllianceWeek` every VS surface already reads.

    The screens are built on that dataclass and on `build_profiles`, so handing
    them the same type is what lets shared knowledge reach a card without every
    renderer learning a second shape.
    """
    import alliance_duel as ad
    import datetime as _dt

    week_date = None
    if record.get("week_date"):
        try:
            week_date = _dt.date.fromisoformat(record["week_date"])
        except ValueError:
            week_date = None

    opponent = None
    if record.get("opponent_tag"):
        opponent = ad.AllianceKey.of(record["opponent_tag"], record.get("opponent_warzone"))

    return ad.AllianceWeek(
        league=ad.LeagueKey(record["season"], record["tier"], record["grp"]),
        week=record["week"],
        alliance=ad.AllianceKey(record["tag"], record["warzone"]),
        week_date=week_date,
        ranking=record.get("ranking"),
        tag_display=record.get("tag_display") or "",
        warzone_display=record.get("warzone_display") or "",
        power=record.get("power"),
        members=record.get("members"),
        gift_level=record.get("gift_level"),
        opponent=opponent,
        day_scores=dict(record.get("day_scores") or {}),
        day_outcomes=dict(record.get("day_outcomes") or {}),
        week_score=record.get("week_score"),
        week_outcome=record.get("week_outcome"),
    )


def rows_for_alliance(alliance, *, season: str | None = None) -> list:
    """`weeks_for_alliance`, as `AllianceWeek` objects."""
    return [_to_row(r) for r in weeks_for_alliance(alliance, season=season)]


def rows_for_bracket(pairs) -> list:
    """`weeks_for_bracket`, as `AllianceWeek` objects.

    **Never mix these into the rows a write reads.** `_row_for_write` builds
    what goes back to the guild's own tab, and a shared row reaching it would
    copy another alliance's record into this alliance's sheet as if they had
    typed it. They are kept on their own attribute for exactly that reason.
    """
    return [_to_row(r) for r in weeks_for_bracket(pairs)]


# ── Removing an alliance from a league (#651) ─────────────────────────────────
#
# Both are scoped to **this server's own copies**. A row another server
# recorded is that server's reading of the game, and an alliance that typed a
# tag wrong has no business deleting it; the removal cleans their league, not
# anybody else's. `actor_guild_id` is the column the uninstall scrub already
# trusts for exactly this question.


def remove_alliance_from_league(alliance, league, *, guild_id, week_dates=()) -> int:
    """Delete this server's rows for `alliance` in that league. Returns the count.

    The league's weeks by date, which is how rows are keyed (#658), and by
    label for any this server stored undated or under that label. Days go
    with them through the foreign key's CASCADE.
    """
    key = _key_parts(alliance)
    parts = _league_parts(league)
    if key is None or parts is None or guild_id is None:
        return 0
    stamps = [getattr(d, "isoformat", lambda d=d: d)() for d in week_dates if d]
    by_date = ""
    if stamps:
        by_date = f" OR week_date IN ({', '.join('?' for _ in stamps)})"
    with _get_conn() as conn:
        n = conn.execute(
            "DELETE FROM alliance_weeks WHERE tag = ? AND warzone = ? AND actor_guild_id = ? "  # noqa: S608
            f"AND ((season = ? AND tier = ? AND grp = ?){by_date})",
            (*key, str(guild_id), *parts, *stamps),
        ).rowcount
        conn.commit()
    return n


def clear_opponent(pairs, league, opponent, *, guild_id) -> int:
    """Blank the opponent on this server's rows that still name `opponent`.

    `pairs` is `(alliance, week, week_date)` for each row to clear: found by
    date when there is one, as rows are keyed (#658), else by label and week.
    The opponent is matched as well as the row, so a row already re-paired
    keeps its new one.
    """
    parts = _league_parts(league)
    theirs = _key_parts(opponent)
    if parts is None or theirs is None or guild_id is None:
        return 0
    n = 0
    with _get_conn() as conn:
        for alliance, week, week_date in pairs:
            key = _key_parts(alliance)
            if key is None or not week:
                continue
            stamp = getattr(week_date, "isoformat", lambda: week_date)()
            if stamp:
                where = "week_date = ?"
                params = (stamp,)
            else:
                where = "week_date IS NULL AND season = ? AND tier = ? AND grp = ? AND week = ?"
                params = (*parts, int(week))
            n += conn.execute(
                "UPDATE alliance_weeks SET opponent_tag = NULL, opponent_warzone = NULL, "  # noqa: S608
                f"updated_at = ? WHERE tag = ? AND warzone = ? AND {where} "
                "AND opponent_tag = ? AND opponent_warzone = ? AND actor_guild_id = ?",
                (_now(), *key, *params, *theirs, str(guild_id)),
            ).rowcount
        conn.commit()
    return n


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

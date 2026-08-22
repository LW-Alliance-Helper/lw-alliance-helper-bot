"""Champion Duel data layer â€” its own SQLite file on the Railway volume.

Separate from `config.py`'s `guild_configs.db` on purpose. That database is
per-guild and private; this one is global tournament data contributed across
alliances and servers, with its own lifecycle â€” it can be wiped between
qualifiers and semifinals without touching a single alliance's configuration.

**Identity is (name, server), never name alone.** Last War names are not unique
across servers, so keying on the normalized name would merge two different
players the moment a second server contributed, and silently pool their
scouting. There is no way to unmerge that afterwards. The `registrants` table
therefore has a surrogate id with `UNIQUE (player_key, server)`, and squads,
orders and edits all hang off that id.

Everything here is **synchronous**. `ruff.toml` selects ASYNC, but its own
comment notes that only catches stdlib-level blocking calls â€” it does not know
sqlite3 blocks. Callers must wrap these in `asyncio.to_thread`, or a query
stalls the Discord gateway heartbeat for the whole process (#366).

Identity normalization is imported from `champion_duel_engine` rather than
reimplemented: the simulator keys its scouting by the same function, and a
second copy that drifted would file corrections under a key the simulator never
looks up â€” applying to nobody and raising nothing.

Attribution stores the raw Discord snowflake so this ports into Map Manager's
Alliance section later without a translation layer.
"""

from __future__ import annotations

import difflib
import hashlib
import itertools
import os
import re
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

# The rounds that carry groups, in the order they are played. Order is
# load-bearing: a player's furthest round is the last of these they appear in.
STAGES = ("qualifiers", "semifinals", "knockouts")

# What the game calls each of the eight phases, on the Match Overview box a
# member reads the start date off. Verified against a screenshot 2026-08-15,
# spelling and hyphenation included: the game writes "Semi-finals", not
# "Semifinals", and "Knockout Stage", not "Knockouts".
#
# **This is the only place a round or phase is named.** `STAGE_LABELS` below is
# derived from it, so a name changes in one spot rather than in two tables that
# then disagree on one surface.
PHASE_LABELS = {
    "signup": "Sign-up stage",
    "signup_detail": "Sign-up Detail",
    "qualifiers": "Qualifiers",
    "qualifier_detail": "Qualifier Detail",
    "semifinals": "Semi-finals",
    "semifinal_detail": "Semi-final Detail",
    "knockouts": "Knockout Stage",
    "results": "Results",
}

# The three phases that carry groups, under the same names. Derived rather than
# restated: the rounds *are* phases, so two tables would be two places to update
# and one place to forget -- which is how the hub came to show "Semi-finals" on
# its phase line and "Semifinals" on a player card at the same time.
STAGE_LABELS = {stage: PHASE_LABELS[stage] for stage in STAGES}

# What a knockout placement means, as the match the player went out in.
#
# A 32-player single-elimination bracket is rigid, so the finishing position
# *is* the exit round and nothing extra has to be stored: 17th to 32nd lost
# their first match, 9th to 16th the round of 16, 5th to 8th the quarter-final.
# The top four reached the semi-finals, and the third-place match is what
# separates 3rd from 4th -- it needs no column of its own.
#
# Knockout rounds deliberately never become `stage` values. Nothing in the
# schema has to hold both a Semi-finals phase and a semifinal match, and these
# names exist only as display copy derived from a number.
# **Say how far they got, never where they lost** (Kevin, 2026-08-15). Thirty
# of the 32 go out somewhere, and a bracket that tells each of them which match
# ended their run is a scoreboard nobody asked us to keep. The same fact framed
# forwards is the one a member would repeat to their alliance.
KNOCKOUT_RESULTS = (
    (1, 1, "1st"),
    (2, 2, "2nd"),
    (3, 3, "3rd"),
    # Sentence case, except that the stage names keep their capital: "made it
    # to top 16" can be read as finishing 16th, where "Top 16" is plainly the
    # name of a round. The capital is doing work, not decoration.
    (4, 4, "Made it to Top 4"),
    (5, 8, "Made it to Quarter-finals"),
    (9, 16, "Made it to Top 16"),
    (17, 32, "Made it to Top 32"),
)


def knockout_result(placement) -> str | None:
    """How a knockout placement reads, or None if it is not one of the 32.

    None rather than a guess: a placement outside the bracket is a typo or a
    format we have not seen, and inventing a round for it would state a fact
    about a match nobody played.
    """
    if placement is None:
        return None
    try:
        place = int(placement)
    except (TypeError, ValueError):
        return None
    for first, last, result in KNOCKOUT_RESULTS:
        if first <= place <= last:
            return result
    return None


# The event's whole timeline, as day offsets from the grouping's start date:
# (key, first day, day it ends). Read off the in-game Match Overview box, which
# is also where a member reads the start date we ask them for.
#
# Only three of the eight carry groups. The rest still matter -- `qualifier_
# detail` is the window in which the semifinal draw becomes visible in game, so
# it is when there is something new to ask for -- and a phase nobody can act on
# is still the honest answer to "what is happening right now".
#
# **The durations are fixed; only the start date moves.** Confirmed by Kevin
# 2026-08-15, and not from one sighting: this timeline has held for several
# seasons, through season 6. That is what makes this an offset table rather
# than a record of one event -- a start date is the only thing anyone ever has
# to enter, and every window for every grouping follows from it.
#
# The whole feature rests on that. `current_phase`, `phase_window` and
# `is_finished` answer for a grouping with nothing loaded, which is every
# grouping but the one that was imported, and they can only do that because the
# shape is structural.
#
# The Knockout Stage is one phase and the game does not break the final out of
# it, so there is no ninth row here. That is a statement about the *timeline*
# only: the final is a longer series than the meetings before it (Bo5 against
# Bo3), it just does not get its own window on the Match Overview box.
#
# Nothing in this module cares. A placement is a placement whether it took
# three games or five, so `KNOCKOUT_RESULTS` is unaffected. Series lengths
# matter to the simulator, where they change a probability -- semifinal and
# knockout meetings are Bo3, the final is Bo5, and a qualifier meeting is a
# single match. `champion-duel-simulator/CONTEXT.md` is the authority.
#
# Verified end to end against the Match Overview box, both halves, 2026-08-15:
#
#   1 Sign-up stage     8/4~8/9      5 Semi-finals        8/17~8/21
#   2 Sign-up Detail    8/9~8/10     6 Semi-final Detail  8/21~8/24
#   3 Qualifiers        8/10~8/14    7 Knockout Stage     8/24~8/29
#   4 Qualifier Detail  8/14~8/17    8 Results            8/29~8/31
#
# `test_the_whole_timeline_matches_the_game` pins that against this table, so a
# transcription slip cannot survive a test run.
PHASES = (
    ("signup", 0, 5),
    ("signup_detail", 5, 6),
    ("qualifiers", 6, 10),
    ("qualifier_detail", 10, 13),
    ("semifinals", 13, 17),
    ("semifinal_detail", 17, 20),
    ("knockouts", 20, 25),
    ("results", 25, 27),
)

# How long a whole Champion Duel runs, from the first day of sign-up.
EVENT_DAYS = PHASES[-1][2]

# A grouping is exactly this many warzones. The game shows them as one line
# ("Participating Warzone: #773, #800, ...") and the set is the grouping's
# identity -- the order the game lists them in is arbitrary.
GROUPING_SIZE = 16

# How big a group is when complete, per round. Not a column: it is a property of
# the event's format, and storing it would let a typo claim a group of 8 is
# full at 6. Knockouts are one field of 32 rather than lettered groups.
GROUP_SIZE = {"qualifiers": 100, "semifinals": 8, "knockouts": 32}

# The lettered groups inside a round. Sixteen either way: the qualifiers split
# 1,600 players into groups of 100, and the semifinals split the 128 advancers
# into groups of 8. Knockouts are one field of 32 and carry no letter at all.
#
# **A to P, confirmed by Kevin 2026-08-15**, for both the qualifiers and the
# semifinals. A letter outside this set is still storable -- `_group` takes any
# letter -- so an import is never blocked by the picker's bounds.
GROUP_LABELS = tuple(chr(ord("A") + i) for i in range(16))

# Which entry a recording writes. A group is recorded twice over its life --
# once at the draw, once at the standings -- and they are different numbers for
# the same player and round, so they are different columns. Writing one must
# never destroy the other; that is the same failure `groups` exists to stop.
RECORDINGS = ("draw", "final")


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
    """UTC ISO-8601, stored as TEXT so it sorts lexicographically â€” which is
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


def _text(value) -> str | None:
    """A Discord snowflake as TEXT, matching how `edits` already stores them.

    Guild and user ids arrive as int from discord.py and as str from the API, and
    a column holding both compares equal to neither reliably.
    """
    if value is None:
        return None
    s = str(value).strip()
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

    ALTER TABLE cannot fix it â€” the primary key changes and three tables change
    what they reference â€” so the old tables are dropped and recreated empty.

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
                troop_level  INTEGER,
                seeded       INTEGER NOT NULL DEFAULT 0,
                origin       TEXT NOT NULL DEFAULT 'imported',
                added_by     TEXT,
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL,
                UNIQUE (player_key, server)
            )
        """)
        # A Champion Duel grouping: the 16 warzones drawn together. Timing and
        # structure are per grouping, not global -- the numbering blocks in the
        # game UI are 128 wide and a block splits into 8 groupings, so nothing
        # about one warzone number tells you which fifteen others it is paired
        # with. About 50 alliances use this bot and the imported grouping covers
        # roughly two of them.
        #
        # `started_on` is the first day of sign-up, read off the in-game Match
        # Overview. Everything about when a round runs derives from it (PHASES),
        # which is what lets a grouping with no draw loaded still answer "what
        # is happening now" -- the state every grouping but one is in.
        #
        # Nullable, because an import can establish that a grouping exists
        # without anyone having read its dates yet. A grouping with no start
        # date simply cannot answer timeline questions, and every timeline
        # helper returns None for it, which is the truth rather than a guess.
        #
        # `created_by_discord_id` is audit only and is never read to resolve
        # anything. A person changes alliance and migrates warzone; a guild's
        # warzone is the durable fact. Same split as `edits.actor_discord_id`.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groupings (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                started_on            TEXT,
                origin                TEXT    NOT NULL DEFAULT 'member',
                created_by_guild_id   TEXT,
                created_by_discord_id TEXT,
                created_at            TEXT    NOT NULL,
                updated_at            TEXT    NOT NULL
            )
        """)
        # The set is the grouping's identity. TEXT to join `registrants.server`,
        # which is TEXT because a server arrives from a modal.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS grouping_warzones (
                grouping_id INTEGER NOT NULL,
                warzone     TEXT    NOT NULL,
                source      TEXT    NOT NULL DEFAULT 'claim',
                PRIMARY KEY (grouping_id, warzone),
                FOREIGN KEY (grouping_id) REFERENCES groupings(id) ON DELETE CASCADE
            )
        """)
        # A lettered set inside one round of one grouping. `id` is the identity,
        # not the letter: two groupings both have a Group D and they are not the
        # same eight people. Before this, a group letter was a bare TEXT meaning
        # the same thing everywhere, so an officer in warzone 1500 recording an
        # opponent as "Group D" landed them in the imported grouping's Group D.
        #
        # `label` is NULL for knockouts, which are one field of 32 rather than
        # lettered groups.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                grouping_id         INTEGER NOT NULL,
                stage               TEXT    NOT NULL,
                label               TEXT,
                created_by_guild_id TEXT,
                created_at          TEXT    NOT NULL,
                updated_at          TEXT    NOT NULL,
                UNIQUE (grouping_id, stage, label),
                FOREIGN KEY (grouping_id) REFERENCES groupings(id) ON DELETE CASCADE
            )
        """)
        # Stage hangs off the group, not off the member: carrying both is how a
        # semifinal write could reach a qualifier row.
        #
        # `seed_rank` and `rank` are separate because they are different numbers
        # for the same player and round. Every player has a rank from the moment
        # a group is drawn (the seed position) and a different one after it is
        # played. For knockouts `seed_rank` is the bracket position 1..32, which
        # is given rather than derived -- the game reorders when it places them
        # and the rule is not known -- and `rank` is the final placement, which
        # in a rigid 32-bracket is also the exit round.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS group_members (
                group_id      INTEGER NOT NULL,
                registrant_id INTEGER NOT NULL,
                seed_rank     INTEGER,
                rank          INTEGER,
                score         INTEGER,
                created_at    TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL,
                PRIMARY KEY (group_id, registrant_id),
                FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
                FOREIGN KEY (registrant_id) REFERENCES registrants(id) ON DELETE CASCADE
            )
        """)
        # A guild's warzone, not its grouping. A warzone is durable; a grouping
        # changes every Champion Duel. Resolving grouping-by-warzone on each read
        # means next season's grouping starts working for every guild in it the
        # moment one person enters it, with no re-pinning and no expiry prompt.
        #
        # `confirmed_grouping_id` closes the silent case: an alliance that moves
        # warzone still resolves, because the old number still exists and still
        # gets drawn into somebody's grouping. Confirm once per Champion Duel
        # rather than trusting it forever.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_warzone (
                guild_id              TEXT PRIMARY KEY,
                warzone               TEXT NOT NULL,
                set_by_discord_id     TEXT,
                confirmed_grouping_id INTEGER,
                created_at            TEXT NOT NULL,
                updated_at            TEXT NOT NULL
            )
        """)
        # Superseded by `groups` / `group_members`, which add the grouping
        # dimension this table had no room for. Kept unread for one release so
        # the copy below can be checked against real data before the table goes;
        # dropping it in the same release that copies it leaves no way back.
        #
        # `registrants.grp` and `registrants.rank` stay too, and stay dead.
        # Dropping columns in SQLite rewrites the table, which is not worth
        # doing to a live volume for two fields nothing reads.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS registrant_stages (
                registrant_id INTEGER NOT NULL,
                stage         TEXT    NOT NULL,
                grp           TEXT,
                rank          INTEGER,
                created_at    TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL,
                PRIMARY KEY (registrant_id, stage),
                FOREIGN KEY (registrant_id) REFERENCES registrants(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS squads (
                registrant_id INTEGER NOT NULL,
                slot          INTEGER NOT NULL,
                squad_type    TEXT,
                power         REAL,
                -- 1 when this squad is 4-of-a-type rather than 5, 0 when
                -- somebody looked and it is pure, NULL when nobody has said.
                -- The three are genuinely different: the engine samples a
                -- mixed pair from the population for a player it has not been
                -- told about, and treats a measured "none" as a measurement.
                -- Collapsing NULL and 0 would put a 3.3% penalty on two squads
                -- of every player nobody has scouted.
                mixed         INTEGER,
                source        TEXT NOT NULL,
                observed_at   TEXT,
                updated_at    TEXT NOT NULL,
                updated_by    TEXT,
                PRIMARY KEY (registrant_id, slot),
                FOREIGN KEY (registrant_id) REFERENCES registrants(id) ON DELETE CASCADE
            )
        """)
        # What the sighting corpus MEASURED about a player, as opposed to what
        # somebody saw them field once. `champion_duel_engine.semifinal` takes
        # these as an argument and draws from a population distribution for
        # anyone absent, so a missing row costs accuracy rather than breaking a
        # prediction -- which is why they are their own table rather than
        # columns on `squads`. A profile is about the player; a squad row is
        # about one box on their lineup screen.
        #
        # **Every position here is a POWER RANK, 0 = biggest squad.** The
        # `mixed` flag on `squads` is indexed by BOX. The two are different
        # frames and translating between them needs all three powers, which is
        # why `champion_duel_odds._profile` -- not this table -- owns the
        # merge. Storing them apart is what keeps that translation visible.
        #
        # No `source` column: every row is imported by definition. A member's
        # own answer about their squads lands on `squads.mixed`, which is the
        # other half of the merge and carries its own provenance already.
        #
        # NULL means never measured, which is NOT a measured zero -- `mixed`
        # is '' when somebody looked and every squad is pure, and the engine
        # treats those two differently on purpose.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS registrant_profiles (
                registrant_id INTEGER PRIMARY KEY,
                -- "Aircraft,Tank,Missile" -- biggest squad first
                types         TEXT,
                -- lineup shape (r21, r31); either may be NULL if only the
                -- other was read
                shape_r21     REAL,
                shape_r31     REAL,
                -- which squads are 4-of-a-type, as power ranks: '0,1' is the
                -- two biggest, '' is "we looked and all three are pure"
                mixed         TEXT,
                -- LEGACY: how many are 4-of-a-type, when nothing said which.
                -- Only ever >= 1; a measured zero normalises to mixed = ''.
                n_mixed       INTEGER,
                -- which power rank the gorilla starts on, 0-2
                gorilla       INTEGER,
                updated_at    TEXT NOT NULL,
                updated_by    TEXT,
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
        # One row per import. Never one per value.
        #
        # **Deliberately not `edits`.** That was the open question and this is
        # the answer. `edits` is a per-value audit trail with a revert
        # attached, and its whole job is finding the handful of corrections a
        # human made. A roster load is a different kind of event: one file,
        # hundreds of rows, one actor, one moment, and nothing in it is
        # revertable row by row. Folding imports into `edits` would bury every
        # real correction under them and rank whoever ran the import above
        # every scout in `contributor_summary`. So
        # `test_an_import_writes_no_edit_rows` stands, and this table carries
        # what that rule leaves unrecorded.
        #
        # What it is FOR is also different, and drove the shape: Kevin wants a
        # population we can track. That question is answered by counts per
        # import, not by values.
        #
        # `grouping_id` carries no foreign key on purpose. A merge moves rows
        # between groupings and can retire the source, and a log entry records
        # what was true when it ran rather than following the schema forward.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS import_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                grouping_id      INTEGER,
                stage            TEXT,
                registrants      INTEGER NOT NULL DEFAULT 0,
                squads           INTEGER NOT NULL DEFAULT 0,
                orders           INTEGER NOT NULL DEFAULT 0,
                profiles         INTEGER NOT NULL DEFAULT 0,
                cleared          INTEGER NOT NULL DEFAULT 0,
                skipped          INTEGER NOT NULL DEFAULT 0,
                -- which door it came through: 'discord' or 'api'
                door             TEXT    NOT NULL,
                actor_discord_id TEXT,
                actor_name       TEXT,
                actor_guild_id   TEXT,
                created_at       TEXT    NOT NULL
            )
        """)
        # Every time we held a value, somebody offered a different one, and a
        # person was asked which is right.
        #
        # `edits` cannot carry this. An edit row is a change, and the answer
        # worth recording most is the one where NOTHING changed: somebody
        # challenged what we hold and a person confirmed it. Writing that as an
        # edit with old == new would put a no-op in the revert history and make
        # `contributor_summary` count a confirmation as a correction.
        #
        # One row per disputed field, all sharing one decision, because the
        # question put to the member is about the entry as a whole: here are
        # the two, which is right. Splitting the question per field is what
        # turns a correction into an interrogation.
        #
        # `edit_id` is set only when the offered value won, and links the call
        # to the change it caused so `⏪ Revert an edit` and this table tell one
        # story rather than two.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS disagreements (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                -- same vocabulary as edits.target
                target           TEXT    NOT NULL,
                registrant_id    INTEGER NOT NULL,
                slot             INTEGER,
                field            TEXT    NOT NULL,
                held_value       TEXT,
                offered_value    TEXT,
                -- 'held' or 'offered'
                chose            TEXT    NOT NULL,
                edit_id          INTEGER,
                actor_discord_id TEXT    NOT NULL,
                actor_name       TEXT,
                actor_guild_id   TEXT,
                created_at       TEXT    NOT NULL,
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
            "CREATE INDEX IF NOT EXISTS ix_stages_stage ON registrant_stages(stage, grp)",
            "CREATE INDEX IF NOT EXISTS ix_gw_warzone ON grouping_warzones(warzone)",
            "CREATE INDEX IF NOT EXISTS ix_groups_lookup ON groups(grouping_id, stage, label)",
            "CREATE INDEX IF NOT EXISTS ix_gm_registrant ON group_members(registrant_id)",
        ):
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:  # pragma: no cover
                print(f"[CHAMPION_DUEL] index skipped: {exc}")

        # Columns added for the engine's 1.5 player spec. Each ALTER in its own
        # try/except so a re-run is harmless, matching `config.init_db`; the
        # CREATE TABLE statements above carry the same columns for fresh files.
        #
        # Both are NULLable with no default, deliberately. The engine
        # distinguishes "not measured" from "measured and zero": an absent
        # `mixed` makes it sample a mixed pair from the population, where a
        # recorded 0 says somebody looked and every squad is pure. A DEFAULT 0
        # would turn every unscouted player into a measurement and put a 3.3%
        # purity penalty on squads nobody has ever seen.
        for _table, _column, _decl in (
            ("registrants", "troop_level", "INTEGER"),
            ("squads", "mixed", "INTEGER"),
        ):
            try:
                conn.execute(f"ALTER TABLE {_table} ADD COLUMN {_column} {_decl}")
                print(f"[CHAMPION_DUEL] added {_table}.{_column}")
            except sqlite3.OperationalError:
                pass

        # One-time backfill: whatever `registrants` holds today is qualifier
        # data, because qualifiers are the only round that has ever been
        # imported. Guarded on the qualifiers rows being absent rather than on
        # the table being empty, so a later round already loaded does not stop
        # it, and re-running never overwrites a corrected group.
        conn.execute(
            """
            INSERT INTO registrant_stages
                (registrant_id, stage, grp, rank, created_at, updated_at)
            SELECT r.id, 'qualifiers', r.grp, r.rank, r.created_at, r.updated_at
            FROM registrants r
            WHERE (r.grp IS NOT NULL AND r.grp != '') OR r.rank IS NOT NULL
            ON CONFLICT(registrant_id, stage) DO NOTHING
            """
        )

        _migrate_stages_to_groupings(conn)


# The imported grouping's sign-up date, from its in-game Match Overview. There
# is nowhere to derive this from -- the roster payload carries no dates -- and
# it is only ever applied to the one grouping that predates groupings existing.
_IMPORTED_STARTED_ON = "2026-08-04"


def _migrate_stages_to_groupings(conn) -> None:
    """Move the pre-grouping draw into a real grouping. Runs once.

    Everything imported so far belongs to one grouping, because a grouping is
    what the importer had no concept of. So this creates that grouping, seeds it
    from the warzones its own registrants are on, and copies each
    `registrant_stages` row into a group under it.

    Two things it deliberately does not do:

    **The warzones come from imported registrants only.** Self-reported rows
    already carry foreign warzones -- someone in another grouping recording an
    opponent -- and seeding from every registrant would pull other alliances'
    numbers into this grouping and make them resolve to it forever.

    **Placements on self-reported players are dropped, not migrated.** A group
    letter typed by an officer in another grouping is the exact collision this
    schema exists to stop; it names a group in a grouping we do not have. The
    registrant is kept, the placement is not. The count is printed rather than
    swallowed, because more than a handful means something else happened.
    """
    already = conn.execute("SELECT 1 FROM groupings WHERE origin = 'imported'").fetchone()
    if already:
        return
    warzones = [
        r["server"]
        for r in conn.execute(
            "SELECT DISTINCT server FROM registrants "
            "WHERE origin = 'imported' AND server IS NOT NULL AND server != '' "
            "ORDER BY server"
        ).fetchall()
    ]
    if not warzones:
        return

    now = _now()
    cur = conn.execute(
        "INSERT INTO groupings (started_on, origin, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (_IMPORTED_STARTED_ON, "imported", now, now),
    )
    grouping_id = cur.lastrowid
    conn.executemany(
        "INSERT OR IGNORE INTO grouping_warzones (grouping_id, warzone, source) VALUES (?, ?, ?)",
        [(grouping_id, w, "import") for w in warzones],
    )
    if len(warzones) != GROUPING_SIZE:
        # Not fatal: the roster may be partially loaded. But a grouping is
        # sixteen warzones, so anything else is worth seeing in the logs rather
        # than discovering when a lookup misses.
        print(
            f"[CHAMPION_DUEL] migrated grouping has {len(warzones)} warzones, "
            f"expected {GROUPING_SIZE}: {', '.join(warzones)}"
        )

    rows = conn.execute(
        """
        SELECT s.stage, s.grp, s.rank, s.registrant_id, s.created_at, s.updated_at
        FROM registrant_stages s
        JOIN registrants r ON r.id = s.registrant_id
        WHERE r.origin = 'imported' AND s.grp IS NOT NULL AND s.grp != ''
        """
    ).fetchall()
    groups: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row["stage"], row["grp"])
        if key not in groups:
            cur = conn.execute(
                "INSERT INTO groups (grouping_id, stage, label, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (grouping_id, row["stage"], row["grp"], now, now),
            )
            groups[key] = cur.lastrowid
        # The old `rank` is a finishing position, not a seed: it came from a
        # standings export. So it lands in `rank` and `seed_rank` stays empty
        # rather than being invented.
        conn.execute(
            "INSERT OR IGNORE INTO group_members "
            "(group_id, registrant_id, rank, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (groups[key], row["registrant_id"], row["rank"], row["created_at"], row["updated_at"]),
        )

    orphans = conn.execute(
        """
        SELECT COUNT(*) AS n FROM registrant_stages s
        JOIN registrants r ON r.id = s.registrant_id
        WHERE r.origin != 'imported' AND s.grp IS NOT NULL AND s.grp != ''
        """
    ).fetchone()["n"]
    print(
        f"[CHAMPION_DUEL] grouping {grouping_id}: {len(warzones)} warzones, "
        f"{len(groups)} groups, {len(rows)} placements migrated"
        + (f"; {orphans} self-reported placement(s) left behind" if orphans else "")
    )


# â”€â”€ Groupings â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def parse_warzones(text, *, unique: bool = True) -> list[str]:
    """The Participating Warzone line, as a sorted set of warzone numbers.

    The game renders it `#773 , #800 , #744 , ...` and the order it lists them
    in is arbitrary, so this returns them sorted: the *set* is the grouping's
    identity, and two people typing the same sixteen in different orders must
    produce the same grouping.

    Deliberately lenient about separators. Someone is copying sixteen numbers
    off a phone screen, and rejecting their line because they used spaces
    instead of commas would be a validation failure with nothing wrong behind
    it. Anything non-numeric simply is not a warzone.

    `unique=False` keeps repeats, in the order they were typed. Only validation
    wants that: sixteen numbers with one typed twice dedupe to sixteen and would
    otherwise be accepted as a complete grouping that is short one warzone.
    """
    out: list[str] = []
    for chunk in str(text or "").replace("#", " ").replace(",", " ").split():
        digits = chunk.strip()
        if digits.isdigit():
            out.append(str(int(digits)))
    if not unique:
        return out
    return sorted(set(out), key=int)


# ── Reading the numbers out of a pasted line ──────────────────────────────────
#
# A line is `name, warzone, rank, thp, score`, and its four numbers cannot be
# found by counting commas: people type `2,308` for a warzone, `1,103` for a
# rank and `327,159,292` for a hero power, and every one of those is a comma
# *inside* a number rather than between two of them. So the tokens after the
# name are partitioned into contiguous groups every valid way and the readings
# are scored. There are only a handful per line; this is not a search.
#
# The bands below were measured on 2026-08-21, against the 1,600-row qualifier
# register and the 128-player semi-final field:
#
#     total hero power   164,288,841 .. 499,230,216
#     duel score              73,728 ..  48,303,042
#     rank                          1 .. 100
#     warzone                     677 .. 804
#
# Two different jobs come out of that, and conflating them is how a parser like
# this goes wrong. The MIN/MAX pairs say what is *possible*, and are deliberately
# far wider than anything measured: refusing a number for being unlike the
# register is the phone-field pattern, and being errored for typing a number the
# way you naturally type it is miserable to be on the wrong end of. The LIKELY
# values say what is *typical*, and are consulted only where structure has
# already left more than one reading standing.
#
# `_LIKELY_POWER_FLOOR` is the one that earns its keep. Hero power and duel
# score do not overlap and are not close -- the largest score on record is 3.4x
# below the smallest hero power -- and that gap is the only thing separating a
# four-number line typed in the old `name, warzone, rank, score` order from one
# typed in the new order with the score left off.

#: How well one number sits in one field. Graded rather than boolean because
#: `_readings` compares whole readings against each other and needs to say that
#: one is better, not just that both are allowed.
_IMPOSSIBLE, _POSSIBLE, _TYPICAL, _KNOWN, _OURS = 0, 2, 4, 6, 8

_WARZONE_MAX = 9_999
#: Every warzone in every grouping we hold is three digits, and the only four
#: digit one on record is a test case. One and two digit warzones exist in the
#: game, so they are possible; they are just not what a Champion Duel line
#: normally names, and saying so is what keeps `2,308` from reading as warzone
#: 2 followed by rank 308 when neither prior is available.
_WARZONE_TYPICAL_DIGITS = (3, 4)
# Every real placement is a position inside a group, and the largest group the
# game draws is 100. This ceiling is not measured, then: it is the smallest
# round number that still admits a four-digit grouped rank like `1,103`, which
# is a shape that must not be assumed away. Above it a "rank" is a partition
# that cut in the wrong place, and saying so is what makes a line with no
# readable answer flag rather than guess.
_RANK_MAX = 1_999
_LIKELY_RANK_MAX = 200
_THP_MIN, _THP_MAX = 1_000_000, 10_000_000_000
_SCORE_MIN, _SCORE_MAX = 1_000, 1_000_000_000
_LIKELY_POWER_FLOOR = 100_000_000

#: A real line cannot need more tokens than this: two for a grouped warzone,
#: two for a rank past a thousand, four each for a hero power and a score. Past
#: it, whatever was pasted is not a placement line, and enumerating its
#: partitions is work with nothing at the end of it.
_MAX_NUMBER_TOKENS = 12

#: `325.8M` is how the game writes a power and `84,600,000` is how a spreadsheet
#: writes it. Both are the same number and neither is the user's mistake to fix.
_SUFFIX_MULTIPLIERS = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

#: A slot the user deliberately left empty, as in `AlphaOne, , 3`. That is not
#: a malformed line, it is somebody saying they do not know the warzone but do
#: know the rank, and the empty comma is the only thing holding the rank in its
#: own position. So it stays a token and takes a field rather than being swept
#: up: dropping it would slide the rank into the warzone slot.
_ABSENT = object()

_PLAIN_NUMBER = re.compile(r"0|[1-9][0-9]*")
_SUFFIXED_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?[kmbKMB]")
_GROUP_LEAD = re.compile(r"[1-9][0-9]{0,2}")
_GROUP_TAIL = re.compile(r"[0-9]{3}")

#: Which slot a token that is not a number at all was standing in, so a flagged
#: line can name the field it choked on rather than shrug at the whole line.
_FIELD_AT_POSITION = ("bad_server", "bad_rank", "bad_thp", "bad_score")

_LINE_FIELDS = (
    ("server",),
    ("server", "rank"),
    ("server", "rank", "thp"),
    # The old four-column format, `name, warzone, rank, score`, which is still
    # in the fingers of everyone who used this before hero power was asked for.
    # Kept as a reading rather than migrated away from: their score is a score,
    # and filing it as a hero power would put a number an order of magnitude too
    # small into the one field the model cannot run without.
    ("server", "rank", "score"),
    ("server", "rank", "thp", "score"),
)


def _unpadded(token: str) -> str:
    """`05` is a rank of 5 and `0738` is warzone 738. `000` is not a zero.

    A leading zero is only meaningful on a token that could be continuing the
    number in front of it, and a continuation is exactly three digits. So the
    padding comes off everything else, which is what the old parser did through
    `int()` and `parse_warzones`, and stays on the three-digit case, where
    dropping it would let `33,500,000` read as a number followed by a zero.
    """
    if len(token) == 3 or not token.isdigit():
        return token
    return str(int(token))


def _group_value(tokens: list[str]) -> float | None:
    """One contiguous run of tokens as a single number, or None if it is not one.

    A **single token** is a plain integer of any length. `2308` and `325800000`
    are both fine, and this is the rule that is easy to get wrong: an earlier
    prototype required a 1-3 digit lead and rejected every ungrouped long
    number, which broke exactly the people who type plainly while still passing
    a test suite built only from grouped examples.

    **Several tokens** are one comma-grouped number, so the lead is 1-3 digits
    with no leading zero and every continuation is exactly three. That is the
    rule which forces the cut in `327,159,292,33,500,000`: `33` cannot continue
    a group, so there is only one place the first number can end.
    """
    if len(tokens) == 1:
        token = tokens[0]
        if not token:
            return _ABSENT
        if _PLAIN_NUMBER.fullmatch(token):
            return float(token)
        if _SUFFIXED_NUMBER.fullmatch(token):
            # Rounded, and this is not cosmetic. `8.2 * 1_000_000` is
            # 8199999.999999999 in binary floating point, which is not an
            # integer -- so a score written `8.2M` failed the whole-number
            # check below and took the rest of its line down with it, and a
            # power written `4.1M` reached the database as 4099999.9999999995.
            # Neither number has a fractional part in the world it came from.
            return float(round(float(token[:-1]) * _SUFFIX_MULTIPLIERS[token[-1].lower()]))
        return None
    if not _GROUP_LEAD.fullmatch(tokens[0]):
        return None
    if not all(_GROUP_TAIL.fullmatch(token) for token in tokens[1:]):
        return None
    return float(int("".join(tokens)))


def _fits(field: str, value: float, *, warzone, known) -> int:
    """How well one number sits in one field, on the scale above."""
    if field == "server":
        if value != int(value) or not 1 <= value <= _WARZONE_MAX:
            return _IMPOSSIBLE
        # Most lines an alliance pastes are its own warzone, and every line it
        # can legitimately paste names one of the grouping's sixteen. Between
        # them, `2,308` stops being ambiguous before plausibility is reached.
        text = str(int(value))
        if warzone and text == str(warzone):
            return _OURS
        if text in known:
            return _KNOWN
        return _TYPICAL if len(text) in _WARZONE_TYPICAL_DIGITS else _POSSIBLE
    if field == "rank":
        if value != int(value) or not 1 <= value <= _RANK_MAX:
            return _IMPOSSIBLE
        return _TYPICAL if value <= _LIKELY_RANK_MAX else _POSSIBLE
    if field == "thp":
        if not _THP_MIN <= value <= _THP_MAX:
            return _IMPOSSIBLE
        return _TYPICAL if value >= _LIKELY_POWER_FLOOR else _POSSIBLE
    # Zero is a real score and belongs to somebody who did not play, so it is
    # admitted on its own rather than by dropping the floor. Points arrive in
    # chunks -- a victory alone is 300,000 -- so there is nothing between zero
    # and the floor for the floor to be refusing, and keeping it up is what
    # makes a line whose numbers have no readable split flag instead of guess.
    if value != int(value) or not (value == 0 or _SCORE_MIN <= value <= _SCORE_MAX):
        return _IMPOSSIBLE
    if value == 0:
        return _POSSIBLE
    return _TYPICAL if value < _LIKELY_POWER_FLOOR else _POSSIBLE


#: Scaled so a reading is scored on how well its fields fit ON AVERAGE, in
#: whole numbers so two readings can tie exactly. 12 is the common multiple of
#: the one to four fields a line can fill.
#:
#: **Averaged, not summed, and that is the whole point.** Summing pays a
#: reading for filling more fields, so `Kestrel, 2,308` scored better read as
#: warzone 2 plus rank 308 than as the warzone 2308 it plainly is, purely
#: because the wrong answer used two fields and the right one used one. Every
#: reading consumes all the tokens either way, so the number of fields is not
#: evidence about anything.
_FIT_SCALE = 12


def _readings(tokens: list[str], *, warzone, known) -> list[tuple[int, dict]]:
    """Every way this line's numbers read, best first."""
    scored: list[tuple[int, dict]] = []
    for fields in _LINE_FIELDS:
        if len(fields) > len(tokens):
            continue
        for cuts in itertools.combinations(range(1, len(tokens)), len(fields) - 1):
            edges = (0, *cuts, len(tokens))
            values: dict[str, float] = {}
            total = 0
            for field, start, end in zip(fields, edges, edges[1:]):
                value = _group_value(tokens[start:end])
                if value is None:
                    break
                if value is _ABSENT:
                    # Counted, not judged. A slot left empty says nothing about
                    # whether this partition is the right one, so it takes the
                    # neutral grade rather than tipping the reading either way.
                    values[field], total = None, total + _POSSIBLE
                    continue
                fit = _fits(field, value, warzone=warzone, known=known)
                if not fit:
                    break
                values[field], total = value, total + fit
            else:
                # A hero power all but always exceeds a duel score. On the one
                # line where both are present and both are readable either way,
                # that is the last thing left to tell them apart.
                thp, score = values.get("thp"), values.get("score")
                if thp is not None and score is not None and thp > score:
                    total += 1
                scored.append((total * (_FIT_SCALE // len(fields)), values))
    scored.sort(key=lambda reading: -reading[0])
    return scored


def parse_placement_line(line: str, *, warzone=None, known_warzones=None) -> dict:
    """One pasted line of a group listing: `name, warzone, rank, thp, score`.

    Left to right as the in-game Duel card reads, so somebody copying it out is
    transcribing rather than translating. Only the name is required; a line that
    stops early simply carries less.

    Returns a dict with `name`, `alliance`, `server`, `rank`, `thp`, `score` and
    `problem`. **`problem` is a flag, not an exception**: a line that cannot be
    read has to reach the reconcile view and be shown, because silently mangling
    one row of a paste of eight is the failure mode that gets noticed a week
    later.

    **No format is imposed on the user.** Every one of these parses, and the
    first two differ only in whether the warzone carries its own separator:

        pincatboiiii,2308,225,10,200,000,436,873
        pincatboiiii,2,308,225,10,200,000,436,873
        Kevin,738,5,327,159,292,33,500,000
        Deep,738,1,103,327,159,292,33,500,000          (a rank past a thousand)
        Name,738,5,325800000,33500000                  (no separators at all)
        [OGV]Kestrel,738,1,325.8M,33,500,000
        Name<TAB>738<TAB>5<TAB>327,159,292<TAB>33,500,000
        Wren,744,25                                    (stops early)

    Hero power is **fourth, before score**, which is what lets the score keep
    the tail of the line. The commas inside these numbers are why the old
    split-on-the-first-three-commas approach could not simply be extended by
    one: see `_readings`, and the band comment above it, for how the four
    numbers are actually found.

    `warzone` is the guild's own pinned warzone and `known_warzones` are the
    ones we hold anybody on. Both are priors, not filters: a line naming a
    warzone we have never seen still parses, it just stops being the thing that
    settles an otherwise tied reading.
    """
    raw = (line or "").strip()
    if not raw:
        return {"raw": raw, "problem": "blank"}

    # Tab and newline are separators alongside the comma. Anyone pasting out of
    # a spreadsheet has tabs available, a tab cannot collide with a digit group
    # the way a comma does, and it costs nothing to anyone typing by hand.
    flat = raw.replace("\t", ",").replace("\r", ",").replace("\n", ",")
    name, _, rest = flat.partition(",")
    name = name.strip()

    out = {
        "raw": raw,
        "name": name,
        "alliance": None,
        "server": None,
        "rank": None,
        "thp": None,
        "score": None,
        "problem": None,
    }
    if not name:
        out["problem"] = "no_name"
        return out

    # The tag arrives prefixed to the name, the way the card prints it.
    # `normalize_name` already ignores it for matching, so this is only about
    # keeping it rather than throwing it away: it is the one field on the line
    # we would otherwise have to ask for separately.
    if name.startswith("[") and "]" in name:
        tag, _, remainder = name[1:].partition("]")
        if remainder.strip():
            out["alliance"], out["name"] = tag.strip() or None, remainder.strip()

    # `#` is how the game prints a warzone. A space inside a number is how a
    # good part of the world writes a thousands separator, and the parser this
    # replaces already stripped them out of the score. A trailing comma is not
    # a statement about anything, so it comes off; an empty slot with something
    # after it IS one, and stays. See `_ABSENT`.
    tokens = [token.replace("#", "").replace(" ", "").strip() for token in rest.split(",")]
    tokens = [_unpadded(token) for token in tokens]
    while tokens and not tokens[-1]:
        tokens.pop()
    if not tokens:
        return out

    # A name containing a comma lands its second half in the warzone slot, and
    # that is not recoverable here. Reported by the slot the unreadable token
    # was standing in, so the reconcile view can name the field it choked on
    # rather than shrug at the line -- but only where the tokens in front of it
    # pin that slot down. A three-digit token before it might be a continuation
    # of the number before THAT, in which case its position is one field to the
    # right of where it looks, and a message naming the wrong field is worse
    # than one naming none.
    for position, token in enumerate(tokens):
        if not token:
            continue
        if not (
            _PLAIN_NUMBER.fullmatch(token)
            or _SUFFIXED_NUMBER.fullmatch(token)
            or _GROUP_TAIL.fullmatch(token)
        ):
            grouped = any(_GROUP_TAIL.fullmatch(earlier) for earlier in tokens[1:position])
            out["problem"] = (
                "bad_numbers"
                if grouped
                else _FIELD_AT_POSITION[min(position, len(_FIELD_AT_POSITION) - 1)]
            )
            return out

    if len(tokens) > _MAX_NUMBER_TOKENS:
        out["problem"] = "bad_numbers"
        return out

    readings = _readings(tokens, warzone=warzone, known=set(known_warzones or ()))
    if not readings:
        out["problem"] = "bad_numbers"
        return out

    # A line whose every token is three digits can have two readings that fit
    # equally well, and no correct answer is available for it. Saying so is the
    # honest move: the parser does not have to be perfect, only honest about
    # when it is not, and the reconcile view is already built to put a flagged
    # line in front of a human.
    best, values = readings[0]
    if any(total == best and other != values for total, other in readings[1:]):
        out["problem"] = "bad_numbers"
        return out

    if values.get("server") is not None:
        out["server"] = str(int(values["server"]))
    if values.get("rank") is not None:
        out["rank"] = int(values["rank"])
    if values.get("thp") is not None:
        out["thp"] = values["thp"]
    if values.get("score") is not None:
        out["score"] = int(values["score"])
    return out


def parse_placement_lines(text: str, *, warzone=None, known_warzones=None) -> list[dict]:
    """Every non-blank line of a paste, parsed. Blank lines are dropped rather
    than flagged: a trailing newline is not a mistake anyone made."""
    return [
        parsed
        for parsed in (
            parse_placement_line(line, warzone=warzone, known_warzones=known_warzones)
            for line in str(text or "").splitlines()
        )
        if parsed.get("problem") != "blank"
    ]


def create_grouping(
    warzones,
    started_on: str | None = None,
    *,
    origin: str = "member",
    guild_id=None,
    discord_id=None,
) -> dict:
    """A new Champion Duel grouping: its 16 warzones and when it started.

    Callers validate the count and that the caller's own warzone is in the set;
    this stores whatever it is handed, because the admin path legitimately loads
    a grouping the operator is not in.
    """
    zones = (
        parse_warzones(warzones)
        if isinstance(warzones, str)
        else [_server(w) for w in warzones if _server(w)]
    )
    zones = sorted(set(z for z in zones if z), key=int)
    if not zones:
        raise ValueError("a grouping needs at least one warzone")
    now = _now()
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO groupings "
            "(started_on, origin, created_by_guild_id, created_by_discord_id, "
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (started_on, origin, _text(guild_id), _text(discord_id), now, now),
        )
        grouping_id = cur.lastrowid
        conn.executemany(
            "INSERT OR IGNORE INTO grouping_warzones (grouping_id, warzone, source) "
            "VALUES (?, ?, ?)",
            [(grouping_id, z, "import" if origin == "imported" else "claim") for z in zones],
        )
    return get_grouping(grouping_id)


def ensure_grouping(
    warzones, started_on=None, *, origin="imported", guild_id=None, discord_id=None
) -> dict:
    """The grouping these warzones belong to, creating or completing it.

    Matches on **any** shared warzone, the same rule `_grouping_for_payload`
    uses: a semifinal payload carries the same sixteen as its qualifier draw,
    and requiring an exact set match would fork a second grouping over one
    event every time.

    Completes a row rather than duplicating it. An import can establish a
    grouping exists before anyone has read its dates, and the migration seeds
    warzones from the registrants it can see -- which is fewer than sixteen if
    a warzone fielded nobody. A later payload carrying the Participating
    Warzone line fills both gaps in. Neither is destructive: an existing start
    date is left alone, and warzones are added rather than replaced.
    """
    zones = (
        parse_warzones(warzones)
        if isinstance(warzones, str)
        else [z for z in (_server(w) for w in warzones) if z]
    )
    if not zones:
        raise ValueError("a grouping needs at least one warzone")

    found = next((g for g in (find_grouping_by_warzone(z) for z in zones) if g), None)
    if found is None:
        return create_grouping(
            zones, started_on, origin=origin, guild_id=guild_id, discord_id=discord_id
        )

    missing = sorted(set(zones) - set(found["warzones"]), key=int)
    fills_date = bool(started_on) and not found.get("started_on")
    if missing or fills_date:
        with _get_conn() as conn:
            if fills_date:
                conn.execute(
                    "UPDATE groupings SET started_on = ?, updated_at = ? WHERE id = ?",
                    (started_on, _now(), found["id"]),
                )
            conn.executemany(
                "INSERT OR IGNORE INTO grouping_warzones (grouping_id, warzone, source) "
                "VALUES (?, ?, ?)",
                [(found["id"], z, "import") for z in missing],
            )
        found = get_grouping(found["id"])
    return found


def get_grouping(grouping_id) -> dict | None:
    """One grouping with its warzones, or None."""
    if grouping_id is None:
        return None
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM groupings WHERE id = ?", (grouping_id,)).fetchone()
        if row is None:
            return None
        zones = [
            r["warzone"]
            for r in conn.execute(
                "SELECT warzone FROM grouping_warzones WHERE grouping_id = ?", (grouping_id,)
            ).fetchall()
        ]
    grouping = dict(row)
    grouping["warzones"] = sorted(zones, key=int)
    return grouping


def list_groupings() -> list[dict]:
    """Every grouping, newest start first."""
    with _get_conn() as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM groupings ORDER BY started_on DESC, id DESC"
            ).fetchall()
        ]
    return [g for g in (get_grouping(i) for i in ids) if g]


def find_grouping_by_warzone(warzone) -> dict | None:
    """The grouping containing this warzone, or None.

    A warzone is in at most one grouping per Champion Duel, which is what makes
    a single number enough to resolve someone. Where several match -- two
    member-made groupings over the same draw, which only a wrong claim produces
    -- the most recently started wins, because the older one is a finished
    event and this is the live question.
    """
    zone = _server(warzone)
    if not zone:
        return None
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT g.id FROM groupings g "
            "JOIN grouping_warzones w ON w.grouping_id = g.id "
            "WHERE w.warzone = ? ORDER BY g.started_on DESC, g.id DESC LIMIT 1",
            (zone,),
        ).fetchone()
    return get_grouping(row["id"]) if row else None


def overlapping_groupings(warzones, started_on=None) -> list[tuple[dict, str]]:
    """Groupings running at the same time that already hold one of these
    warzones, each paired with the lowest warzone they share.

    A warzone cannot be in two groupings of one Champion Duel, so an overlap
    that is not the whole set is a contradiction: one of the two entries is
    wrong and the surface has to stop rather than fork a second grouping over
    the same draw. An *exact* set match is not a contradiction at all, and the
    caller joins it instead -- that is two people entering the same sixteen.

    Groupings more than a whole event apart are different Champion Duels and
    share warzones by design, so they are not conflicts. Where either side has
    no start date, the overlap stands: we cannot show the two are separate
    events, and a false stop costs one message where a false pass costs a
    grouping nobody can untangle.
    """
    zones = set(
        parse_warzones(warzones)
        if isinstance(warzones, str)
        else [z for z in (_server(w) for w in warzones) if z]
    )
    start = _started({"started_on": started_on})
    out: list[tuple[dict, str]] = []
    for grouping in list_groupings():
        shared = sorted(zones & set(grouping["warzones"]), key=int)
        if not shared:
            continue
        other = _started(grouping)
        if start and other and abs((start - other).days) >= EVENT_DAYS:
            continue
        out.append((grouping, shared[0]))
    return out


def find_grouping_conflicts() -> list[dict]:
    """Every pair of groupings that claim a warzone they cannot both have.

    `overlapping_groupings` answers the question one member's entry asks: does
    what I just typed collide with anything. This answers the operator's: what
    is broken right now. Same rule, swept over what is already stored rather
    than over a candidate.

    A conflict is two groupings inside one event window sharing at least one
    warzone but not the whole set. An exact match is two people entering the
    same sixteen, which is agreement rather than a contradiction, and the
    entry path already joins them instead.

    Each pair carries the counts, because the operator's whole decision is
    which one to keep and that turns on which holds real data. A grouping with
    a roster and results is almost never the one to fold away.
    """
    groupings = list_groupings()
    out: list[dict] = []
    for i, first in enumerate(groupings):
        for second in groupings[i + 1 :]:
            zones_a, zones_b = set(first["warzones"]), set(second["warzones"])
            shared = sorted(zones_a & zones_b, key=int)
            if not shared or zones_a == zones_b:
                continue
            start_a, start_b = _started(first), _started(second)
            if start_a and start_b and abs((start_a - start_b).days) >= EVENT_DAYS:
                continue
            out.append(
                {
                    "a": first,
                    "b": second,
                    "shared": shared,
                    "a_counts": grouping_counts(first["id"]),
                    "b_counts": grouping_counts(second["id"]),
                }
            )
    return out


def grouping_counts(grouping_id: int) -> dict:
    """How much this grouping actually holds, for deciding whether to keep it.

    Players rather than rows: a registrant in three rounds is one person, and
    an operator weighing two groupings against each other is counting people.
    """
    with _get_conn() as conn:
        groups = conn.execute(
            "SELECT COUNT(*) AS n FROM groups WHERE grouping_id = ?", (grouping_id,)
        ).fetchone()["n"]
        players = conn.execute(
            "SELECT COUNT(DISTINCT m.registrant_id) AS n FROM group_members m "
            "JOIN groups g ON g.id = m.group_id WHERE g.grouping_id = ?",
            (grouping_id,),
        ).fetchone()["n"]
        results = conn.execute(
            "SELECT COUNT(*) AS n FROM group_members m JOIN groups g ON g.id = m.group_id "
            "WHERE g.grouping_id = ? AND m.rank IS NOT NULL",
            (grouping_id,),
        ).fetchone()["n"]
        guilds = conn.execute(
            "SELECT COUNT(*) AS n FROM guild_warzone WHERE confirmed_grouping_id = ?",
            (grouping_id,),
        ).fetchone()["n"]
    return {"groups": groups, "players": players, "results": results, "guilds": guilds}


class MergeRefused(Exception):
    """The merge was not attempted, because the two are not a conflict."""


def merge_groupings(source_id: int, target_id: int, *, actor=None) -> dict:
    """Fold `source` into `target` and delete it. Not revertable.

    The bot never does this on its own. Two member-made groupings claiming one
    warzone only arises from a wrong claim, an alliance cannot undo it, and
    deciding which community's entry was the mistake is the kind of opinion
    `UX.md` principle 6 says the bot does not have. So this exists and only
    `/admin` reaches it.

    **The kept grouping's warzone list is the truth and is not touched.** An
    earlier version unioned the two sets, which was wrong twice over: a
    Champion Duel is exactly `GROUPING_SIZE` warzones, so the union produced a
    31-warzone grouping that no member surface can render and the entry path
    would reject; and it glued the mistaken claim's warzones permanently onto
    the survivor, so the alliance actually drawn into them would conflict all
    over again the moment they entered their real set. The whole premise of a
    conflict is that one of the two lists is wrong. Folding it in keeps the
    wrong answer.

    **The target wins any value it already holds, and the source fills its
    gaps.** Where both hold a placement for one player in one round, each field
    is taken from the target when it has one and from the source otherwise --
    the same COALESCE rule `set_placement` uses, and for the same reason. Doing
    it row-at-a-time instead loses real data: a target holding only the draw and
    a source holding the standings would have thrown the standings away, and
    this cannot be undone.

    Returns what actually moved, because "merged" on its own is not something
    an operator can check.
    """
    if source_id == target_id:
        raise MergeRefused("a grouping cannot be merged into itself")
    source, target = get_grouping(source_id), get_grouping(target_id)
    if source is None or target is None:
        raise MergeRefused("one of those groupings no longer exists")

    now = _now()
    moved = {
        "groups": 0,
        "players": 0,
        "filled": 0,
        "unchanged": 0,
        "guilds": 0,
        "unpinned": 0,
        "dropped_warzones": sorted(set(source["warzones"]) - set(target["warzones"]), key=int),
    }
    with _get_conn() as conn:
        groups = conn.execute("SELECT * FROM groups WHERE grouping_id = ?", (source_id,)).fetchall()
        for group in groups:
            dest_row = conn.execute(
                "SELECT id FROM groups WHERE grouping_id = ? AND stage = ? AND label IS ? "
                "ORDER BY id LIMIT 1",
                (target_id, group["stage"], group["label"]),
            ).fetchone()
            if dest_row is None:
                conn.execute(
                    "INSERT INTO groups "
                    "(grouping_id, stage, label, created_by_guild_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        target_id,
                        group["stage"],
                        group["label"],
                        group["created_by_guild_id"],
                        now,
                        now,
                    ),
                )
                dest_row = conn.execute(
                    "SELECT id FROM groups WHERE grouping_id = ? AND stage = ? AND label IS ? "
                    "ORDER BY id LIMIT 1",
                    (target_id, group["stage"], group["label"]),
                ).fetchone()
            dest = dest_row["id"]
            moved["groups"] += 1
            for member in conn.execute(
                "SELECT * FROM group_members WHERE group_id = ?", (group["id"],)
            ).fetchall():
                existing = conn.execute(
                    "SELECT * FROM group_members WHERE group_id = ? AND registrant_id = ?",
                    (dest, member["registrant_id"]),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO group_members "
                        "(group_id, registrant_id, seed_rank, rank, score, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            dest,
                            member["registrant_id"],
                            member["seed_rank"],
                            member["rank"],
                            member["score"],
                            now,
                            now,
                        ),
                    )
                    moved["players"] += 1
                    continue
                gaps = {
                    field: member[field]
                    for field in ("seed_rank", "rank", "score")
                    if existing[field] is None and member[field] is not None
                }
                if not gaps:
                    moved["unchanged"] += 1
                    continue
                sets = ", ".join(f"{field} = ?" for field in gaps)
                conn.execute(
                    f"UPDATE group_members SET {sets}, updated_at = ? "
                    "WHERE group_id = ? AND registrant_id = ?",
                    (*gaps.values(), now, dest, member["registrant_id"]),
                )
                moved["filled"] += 1

        # A guild pinned to the grouping that is about to vanish would resolve
        # to nothing. Repoint the ones the survivor actually contains; for the
        # rest, clear the pin rather than point them at a Champion Duel their
        # warzone is not in. An unpinned guild re-resolves by warzone on the
        # next read, which is the self-healing path this column exists beside.
        kept_zones = set(target["warzones"])
        for row in conn.execute(
            "SELECT guild_id, warzone FROM guild_warzone WHERE confirmed_grouping_id = ?",
            (source_id,),
        ).fetchall():
            if row["warzone"] in kept_zones:
                conn.execute(
                    "UPDATE guild_warzone SET confirmed_grouping_id = ?, updated_at = ? "
                    "WHERE guild_id = ?",
                    (target_id, now, row["guild_id"]),
                )
                moved["guilds"] += 1
            else:
                conn.execute(
                    "UPDATE guild_warzone SET confirmed_grouping_id = NULL, updated_at = ? "
                    "WHERE guild_id = ?",
                    (now, row["guild_id"]),
                )
                moved["unpinned"] += 1

        # CASCADE takes this grouping's own warzones, groups and members with
        # it. Everything worth keeping has already been copied across.
        conn.execute("DELETE FROM groupings WHERE id = ?", (source_id,))
        conn.execute("UPDATE groupings SET updated_at = ? WHERE id = ?", (now, target_id))

    print(f"[CHAMPION_DUEL] merged grouping {source_id} into {target_id} by {actor}: {moved}")
    return moved


def groupings_for_warzone(warzone) -> list[dict]:
    """Every grouping this warzone has ever been drawn into, newest start first.

    `find_grouping_by_warzone` answers "which one now" and is right for
    resolving a guild. This answers "which ones were there", which is what a
    surface recording results needs: a Champion Duel runs once a season, the
    same warzone is drawn into a new grouping each time, and somebody entering
    a result has to be able to say which one it belongs to.

    Empty for a warzone we hold nothing for, which is the normal state before
    anyone has entered a grouping.
    """
    zone = _server(warzone)
    if not zone:
        return []
    with _get_conn() as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT g.id FROM groupings g "
                "JOIN grouping_warzones w ON w.grouping_id = g.id "
                "WHERE w.warzone = ? ORDER BY g.started_on DESC, g.id DESC",
                (zone,),
            ).fetchall()
        ]
    return [g for g in (get_grouping(i) for i in ids) if g]


def default_grouping_id() -> int | None:
    """The only grouping, when there is exactly one.

    **Transitional.** Before groupings existed there was one draw and every
    caller assumed it; this keeps those callers correct while that stays true,
    and returns None the moment a second grouping is added rather than guessing
    which one someone meant. Every caller that can know its guild should resolve
    properly instead -- see `resolve_grouping_for_guild`.
    """
    with _get_conn() as conn:
        rows = conn.execute("SELECT id FROM groupings LIMIT 2").fetchall()
    return rows[0]["id"] if len(rows) == 1 else None


def set_guild_warzone(guild_id, warzone, *, discord_id=None, confirmed_grouping_id=None) -> dict:
    """Remember which warzone a guild plays on.

    The guild's warzone rather than its grouping: a warzone is durable and a
    grouping changes every Champion Duel, so storing the warzone means next
    season resolves itself as soon as somebody enters the new sixteen.
    """
    zone = _server(warzone)
    if not zone:
        raise ValueError("a warzone is required")
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO guild_warzone
                (guild_id, warzone, set_by_discord_id, confirmed_grouping_id,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                warzone               = excluded.warzone,
                set_by_discord_id     = excluded.set_by_discord_id,
                confirmed_grouping_id = excluded.confirmed_grouping_id,
                updated_at            = excluded.updated_at
            """,
            (_text(guild_id), zone, _text(discord_id), confirmed_grouping_id, now, now),
        )
    return get_guild_warzone(guild_id)


def get_guild_warzone(guild_id) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_warzone WHERE guild_id = ?", (_text(guild_id),)
        ).fetchone()
    return dict(row) if row else None


def resolve_grouping_for_guild(guild_id, *, fallback_warzone=None) -> dict | None:
    """Which grouping this guild is in, or None to ask them.

    Order: the guild's own answer, then whatever the caller could infer, then
    nothing. An officer's answer beats an inference always.

    `fallback_warzone` is passed in rather than read here so this module stays
    off `config.py`'s database -- the Map Manager link lives in `guild_configs
    .db`, and reaching across would tie global tournament data to per-guild
    config in exactly the way keeping them in separate files avoids. The hub
    passes `config.get_guild_alliance_mapping(...)["server"]`, which is an
    INTEGER there and TEXT here; `_server` reconciles that.

    Returns None rather than guessing when the warzone is in no grouping we
    hold. That is the normal state for a new alliance: their grouping does not
    exist until somebody enters it.
    """
    pinned = get_guild_warzone(guild_id)
    warzone = (pinned or {}).get("warzone") or _server(fallback_warzone)
    if not warzone:
        return None
    return find_grouping_by_warzone(warzone)


def needs_warzone_confirmation(guild_id, grouping_id) -> bool:
    """Has this guild confirmed its warzone against this Champion Duel yet?

    An alliance that moves warzone still resolves, silently and wrongly: the old
    number keeps existing and keeps getting drawn into somebody's grouping. So
    the answer is re-confirmed once per grouping rather than trusted forever.
    Once per Champion Duel, never on a repeat visit.
    """
    pinned = get_guild_warzone(guild_id)
    if not pinned or grouping_id is None:
        return False
    return pinned.get("confirmed_grouping_id") != grouping_id


# â”€â”€ Timeline â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _server_today():
    """Today's in-game date. Imported from `config` rather than restated.

    Local import: `config` is a large module and this one is deliberately
    independent of it, but duplicating a timezone constant is how two copies of
    a number drift apart.
    """
    from config import server_date_for

    return server_date_for(datetime.now(timezone.utc))


def _started(grouping):
    """The grouping's start date, or None when nobody has entered one."""
    from datetime import date as _date

    started = (grouping or {}).get("started_on")
    if not started:
        return None
    try:
        return _date.fromisoformat(str(started)[:10])
    except ValueError:  # pragma: no cover - a hand-edited row
        return None


def current_phase(grouping_id=None) -> str | None:
    """Which of the eight phases this grouping is in, by the calendar.

    Derived rather than set by an operator, for the reason the round always was:
    a toggle is one more thing to forget at exactly the moment the event moves
    on. What changed is the source. It used to be "the furthest round we hold a
    draw for", which cannot answer anything for a grouping with nothing loaded
    -- and that is every grouping but the one that was imported.

    Returns None before the start date or after the event has finished.
    """
    grouping = get_grouping(grouping_id if grouping_id is not None else default_grouping_id())
    started = _started(grouping)
    if started is None:
        return None
    day = (_server_today() - started).days
    if day < 0:
        return None
    for key, first, end in PHASES:
        if first <= day < end:
            return key
    return None


def current_stage(grouping_id=None) -> str | None:
    """The round this grouping is playing, or the one it just played.

    A Detail window is not a round, but it is the window in which the round
    before it is what everyone is still talking about and the next draw becomes
    visible. So it reports the round just finished rather than nothing.

    Where the grouping has **no dates at all**, falls back to the furthest round
    we hold a draw for -- the rule this used before there was a timeline. That
    is only for a grouping nobody has entered a start date for; a grouping whose
    calendar says "sign-up, nothing has been played" gets that answer, not a
    guess from stale data.
    """
    grouping = get_grouping(grouping_id if grouping_id is not None else default_grouping_id())
    if _started(grouping) is None:
        return furthest_stage_held(grouping["id"] if grouping else None)
    phase = current_phase(grouping_id)
    if phase is None:
        return None
    mapping = {
        "signup": None,
        "signup_detail": None,
        "qualifiers": "qualifiers",
        "qualifier_detail": "qualifiers",
        "semifinals": "semifinals",
        "semifinal_detail": "semifinals",
        "knockouts": "knockouts",
        "results": "knockouts",
    }
    return mapping.get(phase)


def furthest_stage_held(grouping_id=None) -> str | None:
    """The last round this grouping holds any group for.

    The rule `current_stage` used before the timeline existed, kept as its
    fallback rather than deleted. A grouping whose dates nobody has entered can
    still say something true about itself, and "the furthest round we have a
    draw for" is true â€” it just cannot see a round that has started and has no
    draw loaded, which is why it stopped being the primary answer.
    """
    grouping_id = grouping_id if grouping_id is not None else default_grouping_id()
    if grouping_id is None:
        return None
    with _get_conn() as conn:
        held = {
            r["stage"]
            for r in conn.execute(
                "SELECT DISTINCT stage FROM groups WHERE grouping_id = ?", (grouping_id,)
            ).fetchall()
        }
    for stage in reversed(STAGES):
        if stage in held:
            return stage
    return None


def recorded_stages(grouping_id=None) -> list[str]:
    """Which rounds this grouping actually has groups for, in playing order.

    `current_stage` answers "which round is running", which is the right
    default while one is. It is the wrong question for a Champion Duel that has
    finished, where there is no current round and every round is worth looking
    back at. A surface offering history needs the set, not the tip.

    Ordered by `STAGES` rather than by what SQLite returns, so the picker reads
    in the order the rounds were played rather than alphabetically, which would
    put the knockouts before the qualifiers.
    """
    grouping_id = grouping_id if grouping_id is not None else default_grouping_id()
    if grouping_id is None:
        return []
    with _get_conn() as conn:
        held = {
            r["stage"]
            for r in conn.execute(
                "SELECT DISTINCT stage FROM groups WHERE grouping_id = ?", (grouping_id,)
            ).fetchall()
        }
    return [stage for stage in STAGES if stage in held]


def is_finished(grouping_id=None) -> bool:
    """Past the last day. The hub shows results and offers the next grouping."""
    grouping = get_grouping(grouping_id if grouping_id is not None else default_grouping_id())
    started = _started(grouping)
    if started is None:
        return False
    return (_server_today() - started).days >= EVENT_DAYS


def phase_window(grouping_id, phase: str) -> tuple:
    """(first day, day it ends) for one phase of one grouping, as dates."""
    from datetime import timedelta as _td

    grouping = get_grouping(grouping_id if grouping_id is not None else default_grouping_id())
    started = _started(grouping)
    if started is None:
        return (None, None)
    for key, first, end in PHASES:
        if key == phase:
            return (started + _td(days=first), started + _td(days=end))
    return (None, None)


# â”€â”€ Rounds â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _stage(value) -> str:
    """A round name, normalised, or a ValueError naming the valid ones."""
    stage = str(value or "").strip().lower()
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {STAGES}")
    return stage


def get_or_create_group(grouping_id, stage: str, label=None, *, guild_id=None) -> dict:
    """The group for one round of one grouping, creating it if new.

    `label` is the letter the game shows, or None for knockouts, which are a
    single field of 32 rather than lettered groups. The letter is not the
    identity -- `groups.id` is -- so two groupings' Group D never meet.

    **Selects before inserting rather than relying on `INSERT OR IGNORE`.**
    The UNIQUE index over (grouping_id, stage, label) does not constrain the
    knockouts at all, because their label is NULL and SQLite treats every NULL
    as distinct in a unique index. So the insert never collided there and every
    call created another knockout row, with an unordered read afterwards
    deciding which of them a placement landed in. Lettered rounds were always
    fine, which is why nothing noticed: the knockouts are the one round with no
    letter.
    """
    stage = _stage(stage)
    label = _group(label)
    if grouping_id is None:
        raise ValueError("a group belongs to a grouping")
    now = _now()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM groups WHERE grouping_id = ? AND stage = ? AND label IS ? "
            "ORDER BY id LIMIT 1",
            (grouping_id, stage, label),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO groups "
                "(grouping_id, stage, label, created_by_guild_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (grouping_id, stage, label, _text(guild_id), now, now),
            )
            row = conn.execute(
                "SELECT * FROM groups WHERE grouping_id = ? AND stage = ? AND label IS ? "
                "ORDER BY id LIMIT 1",
                (grouping_id, stage, label),
            ).fetchone()
    return dict(row)


def set_placement(
    group_id: int,
    registrant_id: int,
    *,
    seed_rank=None,
    rank=None,
    score=None,
    recording: str | None = None,
) -> dict:
    """Put one player in one group, or update where they finished.

    `recording` says which entry this is: `draw` writes `seed_rank`, `final`
    writes `rank`. They are different numbers for the same player and round --
    the seed position and where they actually finished -- so writing one must
    never blank the other. Passing neither leaves both alone and just records
    membership, which is what adding a player to a group you are tracking does.

    Omitted values are left as they are rather than overwritten with NULL: a
    second entry that only knows the standings must not erase the draw.
    """
    if recording == "draw":
        seed_rank, rank = (seed_rank if seed_rank is not None else rank), None
    elif recording == "final":
        rank = rank if rank is not None else None
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO group_members
                (group_id, registrant_id, seed_rank, rank, score, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(group_id, registrant_id) DO UPDATE SET
                seed_rank  = COALESCE(excluded.seed_rank, group_members.seed_rank),
                rank       = COALESCE(excluded.rank,      group_members.rank),
                score      = COALESCE(excluded.score,     group_members.score),
                updated_at = excluded.updated_at
            """,
            (group_id, registrant_id, seed_rank, rank, score, now, now),
        )
        row = conn.execute(
            "SELECT * FROM group_members WHERE group_id = ? AND registrant_id = ?",
            (group_id, registrant_id),
        ).fetchone()
    return dict(row)


def get_group_members(group_id: int) -> list[dict]:
    """Everyone in one group, in finishing order where it is known.

    `troop_level` rides along with `thp` because `group_advance_odds` reads it
    off these rows. It was collected, stored and read, and never selected here,
    so every player reached the engine at the default level and the dropdown
    that gathers it could not have changed a number once. Same shape as the
    `thp` gap this query had before it, and the same reason it survived: the
    odds tests build their member dicts by hand, so nothing that passes them
    ever goes through this SELECT.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT m.*, r.display_name, r.server, r.alliance, r.thp, r.fsp, r.troop_level
            FROM group_members m JOIN registrants r ON r.id = m.registrant_id
            WHERE m.group_id = ?
            ORDER BY COALESCE(m.rank, m.seed_rank, 9999), r.display_name
            """,
            (group_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def set_stage(registrant_id: int, stage: str, *, grp=None, rank=None, grouping_id=None) -> dict:
    """Place one registrant in one round of one grouping.

    Separate from `upsert_registrant` on purpose. A player's name, server and
    alliance are facts about the person; their group and rank are facts about a
    round of a grouping, and the bug this exists to prevent was exactly the two
    being written through one code path.

    `grouping_id` defaults to the only grouping there is, which keeps the
    callers written before groupings existed correct while that stays true. Once
    a second grouping exists an unresolved caller writes nothing rather than
    guessing, because guessing puts a player in a stranger's Group D.
    """
    grouping_id = grouping_id if grouping_id is not None else default_grouping_id()
    if grouping_id is None:
        raise ValueError("no grouping resolved; a group letter needs one to belong to")
    group = get_or_create_group(grouping_id, stage, grp)
    return set_placement(group["id"], registrant_id, rank=rank)


def get_stages(registrant_id: int, grouping_id=None) -> dict[str, dict]:
    """Every round this registrant is in, keyed by round, in playing order.

    Shape is unchanged from when rounds lived on their own table: each value
    carries `grp` and `rank`, which is what the card, the API and the roster
    export already read. They do not need to know a group is now a row.

    Scoped to one grouping when given. Without one it returns every round the
    player appears in anywhere, which is right for a player card -- a registrant
    only ever plays in one grouping per Champion Duel.
    """
    sql = """
        SELECT g.stage, g.label AS grp, g.grouping_id, g.id AS group_id,
               gr.started_on AS grouping_started_on,
               m.seed_rank, m.rank, m.score, m.created_at, m.updated_at
        FROM group_members m
             JOIN groups g ON g.id = m.group_id
             LEFT JOIN groupings gr ON gr.id = g.grouping_id
        WHERE m.registrant_id = ?
    """
    params: tuple = (registrant_id,)
    if grouping_id is not None:
        sql += " AND g.grouping_id = ?"
        params += (grouping_id,)
    with _get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    by_stage = {r["stage"]: dict(r) for r in rows}
    return {stage: by_stage[stage] for stage in STAGES if stage in by_stage}


def stage_for_display(registrant_id: int, grouping_id=None) -> dict | None:
    """The round to name on this player's card, or None to fall back.

    The rule (Kevin, #495): show the round currently running, but only if this
    player is actually in it. Someone knocked out in the qualifiers is not part
    of the semifinal story, and captioning their card with the live round would
    say they are still in it.
    """
    stages = get_stages(registrant_id, grouping_id)
    if not stages:
        return None
    # The player's own grouping decides which round is running. Two groupings
    # run on their own calendars, so "the semifinals" is a date for one of them
    # and not the other.
    owner = grouping_id if grouping_id is not None else next(iter(stages.values()))["grouping_id"]
    stage = current_stage(owner)
    if stage is None:
        return None
    return stages.get(stage)


# â”€â”€ Registrants â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


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


def suggest_registrants(name: str, server=None, limit: int = 5) -> list[dict]:
    """Registrants whose name is close to `name`, best first.

    **Never used to resolve anything.** `normalize_name` refuses to fuzzy-match
    on purpose â€” two names differing by one character can be two real players,
    and attaching a sighting to the wrong one is unrecoverable. That rule is
    about resolving silently. Offering candidates for a human to pick from is
    the opposite: it makes the ambiguity visible, which is what
    `AmbiguousPlayer` already does when a name is on several servers.

    Scored rather than filtered, because the two common misses are different
    shapes. A truncation ("pinkcatbo") is a prefix of the real name; a partial
    ("zaddy") is a substring of it, often not at the start. Sequence similarity
    alone ranks the first well and the second badly, so both are scored
    explicitly and similarity only breaks ties.

    `server` narrows when given, but a miss falls back to every server: getting
    the server wrong is at least as likely as getting the name wrong, and a
    suggestion list that hides the right player is worse than a long one.
    """
    query = normalize_name(name)
    if not query:
        return []

    sql = "SELECT id, player_key, display_name, server, grp FROM registrants"
    params: list = []
    server = _server(server)
    if server:
        sql += " WHERE server = ?"
        params.append(server)

    with _get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if server and not rows:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, player_key, display_name, server, grp FROM registrants"
                ).fetchall()
            ]

    scored = []
    for row in rows:
        key = row["player_key"] or ""
        if key.startswith(query) or query.startswith(key):
            score = 3.0
        elif query in key or key in query:
            score = 2.0
        else:
            score = difflib.SequenceMatcher(None, query, key).ratio()
            if score < 0.6:
                continue
        # Similarity breaks ties inside a band, so a closer prefix outranks a
        # longer one rather than the order being arbitrary.
        scored.append((score, difflib.SequenceMatcher(None, query, key).ratio(), row))

    scored.sort(key=lambda item: (-item[0], -item[1], item[2]["display_name"]))
    return [row for _, _, row in scored[:limit]]


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
    squads.source exists â€” otherwise a guess hardens into a fact nobody can
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
                     fsp, troop_level, seeded, origin, added_by, created_at,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    fields.get("troop_level"),
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
            for col in ("alliance", "rank", "thp", "fsp", "troop_level"):
                if fields.get(col) is not None:
                    sets.append(f"{col} = ?")
                    params.append(fields[col])
            if row["origin"] == "self_reported" and origin == "imported":
                sets.append("origin = ?")
                params.append("imported")
            params.append(new_id)
            conn.execute(f"UPDATE registrants SET {', '.join(sets)} WHERE id = ?", params)

        return dict(conn.execute("SELECT * FROM registrants WHERE id = ?", (new_id,)).fetchone())


def _grouping_for_payload(rows: list[dict], *, started_on=None) -> int:
    """Which grouping a roster payload belongs to, creating it if it is new.

    A payload names its own warzones: every registrant in it carries one, and a
    warzone belongs to at most one grouping. So the payload identifies its
    grouping without anyone declaring it, which is how the first import into an
    empty database gets somewhere to put a group letter.

    Matching on *any* warzone rather than the exact set on purpose. A semifinal
    payload carries the same sixteen warzones as its qualifier draw but only
    128 of the players, and a partial re-import carries fewer still; requiring
    the sets to match would fork a second grouping over the same event every
    time. A warzone that already belongs to a grouping settles it.
    """
    zones = sorted({z for z in (_server(r.get("server")) for r in rows) if z}, key=int)
    for zone in zones:
        found = find_grouping_by_warzone(zone)
        if found:
            return found["id"]
    if not zones:
        # Nothing to identify it by. The only remaining honest answer is the
        # grouping there is, if there is exactly one.
        only = default_grouping_id()
        if only is None:
            raise ValueError(
                "a round needs a grouping, and this payload carries no warzone to find one by"
            )
        return only
    return create_grouping(zones, started_on, origin="imported")["id"]


def set_registrant_thp(registrant_id: int, thp) -> None:
    """Fill in a Total Hero Power on a registrant we already hold.

    `upsert_registrant` is the wrong door for this and cannot be used: it is
    keyed on (name, server), and a pasted line that matched a player by name
    alone has an id without necessarily having the warzone that row is filed
    under. Upserting on half an identity would make a second row rather than
    update the first.

    Overwrites rather than asking which is right. Hero power climbs as heroes
    level, so a newer reading supersedes an older one instead of contradicting
    it, and the single-player path has always worked this way. The disagreement
    flow exists for squads, where two people can genuinely be describing
    different things.
    """
    if thp is None:
        return
    with _get_conn() as conn:
        conn.execute(
            "UPDATE registrants SET thp = ?, updated_at = ? WHERE id = ?",
            (float(thp), _now(), registrant_id),
        )


def import_registrants(
    rows: list[dict], *, stage: str | None = None, grouping_id=None, started_on=None
) -> dict:
    """Bulk-load a roster, optionally placing it in a round. Never touches
    scouting.

    `stage` says which round this draw is for. **Without one, no round is
    written at all** and this is just players being added: names, servers,
    alliances and THP, with no claim about where they are in the tournament.

    That is deliberately not a default of qualifiers. A payload whose round we
    cannot establish is exactly the case where guessing is expensive â€” guess
    qualifiers on a semifinal draw and it overwrites the qualifier groups,
    which is the failure this whole table exists to prevent (#495). No round is
    always recoverable; the wrong round is not.

    `grouping_id` says which Champion Duel it belongs to, and the same argument
    applies one level up: a group letter means nothing without one, and the
    wrong one files 1600 players into another alliance's tournament. Defaults to
    the only grouping there is, and refuses once that stops being unambiguous.

    A row's group and rank are written to that round of that grouping only, so
    loading the semifinal draw leaves every qualifier group intact.
    """
    stage = _stage(stage) if stage else None
    if stage and grouping_id is None:
        grouping_id = _grouping_for_payload(rows, started_on=started_on)
    inserted = updated = placed = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        before = find_registrants(name, row.get("server"))
        # The player's own facts. `grp` and `rank` are deliberately absent:
        # they belong to the round, and passing them here is what the stage
        # table exists to stop.
        player = upsert_registrant(
            name,
            server=row.get("server"),
            origin="imported",
            alliance=row.get("alliance"),
            thp=row.get("thp"),
            fsp=row.get("fsp"),
            seeded=row.get("seeded"),
        )
        # No group for this round means not in it. A semifinal payload carries
        # the whole roster so scouting still resolves against every player, but
        # only the 128 advancers have a semifinal group, and writing the other
        # 1472 an empty semifinal row would say they all qualified.
        if stage and row.get("group"):
            set_stage(
                player["id"],
                stage,
                grp=row.get("group"),
                rank=row.get("rank"),
                grouping_id=grouping_id,
            )
            placed += 1
        if before:
            updated += 1
        else:
            inserted += 1
    return {
        "stage": stage,
        "grouping_id": grouping_id,
        "placed": placed,
        "inserted": inserted,
        "updated": updated,
        "total": inserted + updated,
    }


def get_groups(stage: str | None = None, grouping_id=None) -> list[dict]:
    """Member counts per group, for one round of one grouping.

    Defaults to the round currently running, which is what "which groups are
    there" means to someone asking during the event, and to the only grouping
    there is. Scoped rather than global: a count spanning every grouping
    describes several tournaments at once and belongs to none of them.
    """
    grouping_id = grouping_id if grouping_id is not None else default_grouping_id()
    if grouping_id is None:
        return []
    stage = _stage(stage) if stage else current_stage(grouping_id)
    if stage is None:
        return []
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT g.label AS grp, COUNT(m.registrant_id) AS n
            FROM groups g LEFT JOIN group_members m ON m.group_id = g.id
            WHERE g.grouping_id = ? AND g.stage = ? AND g.label IS NOT NULL AND g.label != ''
            GROUP BY g.label ORDER BY g.label
            """,
            (grouping_id, stage),
        ).fetchall()
    return [{"group": r["grp"], "registrants": r["n"]} for r in rows]


def get_servers(grouping_id=None) -> list[dict]:
    """Registrant and scouting counts per warzone, busiest first.

    Groups tell a member which bracket they are in; warzones tell them whether
    this is about anyone they know. Both counts, because they answer different
    questions: a warzone can be fully rostered and still have nobody we have
    watched deploy, and only the second gap is worth contributing to.

    `grouping_id` narrows to that grouping's warzones, which is what every
    member-facing count wants: a total spanning every grouping describes several
    tournaments at once and belongs to none of them. **It reports every one of
    the grouping's sixteen**, including those we hold nobody from, because "we
    have nothing for your warzone" is the answer that invites a contribution and
    an omitted row is one the reader has to notice is missing.

    Global with no `grouping_id`, which is the honest answer before we know who
    is asking, and the only thing the hub can say to an alliance it cannot place.

    This is a report of the warzones we hold, not a list of the ones we accept.
    `upsert_registrant` takes any warzone string, so a self-reported opponent can
    introduce one that was never imported -- and will then appear here with one
    registrant. Callers must not treat the result as a whitelist.
    """
    counts = """
        SELECT r.server AS server,
               COUNT(DISTINCT r.id) AS registrants,
               COUNT(DISTINCT CASE WHEN s.source = 'observed' THEN r.id END) AS scouted
        FROM registrants r
        LEFT JOIN squads s ON s.registrant_id = r.id
        WHERE r.server IS NOT NULL AND r.server != ''
        GROUP BY r.server
        ORDER BY registrants DESC, server
    """
    if grouping_id is None:
        with _get_conn() as conn:
            return [dict(r) for r in conn.execute(counts).fetchall()]

    grouping = get_grouping(grouping_id)
    if grouping is None:
        return []
    with _get_conn() as conn:
        held = {r["server"]: dict(r) for r in conn.execute(counts).fetchall()}
    return [
        held.get(zone, {"server": zone, "registrants": 0, "scouted": 0})
        for zone in grouping["warzones"]
    ]


def get_roster(
    group=None, include_scouting: bool = False, stage: str | None = None, grouping_id=None
) -> list[dict]:
    """Registrants, optionally with their squads.

    `include_scouting` is False for anonymous callers: the registrant list is a
    public LWS export, but squad composition and deployment orders are our own
    scouting.

    `group` filters within one round of one grouping, defaulting to the round
    currently running. A group letter is only meaningful inside both: "group D"
    in the semifinals is a different set of people from "group D" in the
    qualifiers, and a different set again in somebody else's grouping.
    """
    grouping_id = grouping_id if grouping_id is not None else default_grouping_id()
    stage = _stage(stage) if stage else current_stage(grouping_id)
    sql = "SELECT r.* FROM registrants r"
    params: tuple = ()
    if group:
        # Joined through `groups` rather than filtered on `registrants.grp`,
        # which is legacy and no longer written. See #495.
        sql += (
            " JOIN group_members m ON m.registrant_id = r.id"
            " JOIN groups g ON g.id = m.group_id"
            " WHERE g.grouping_id = ? AND g.stage = ? AND g.label = ?"
        )
        params = (grouping_id, stage or "qualifiers", _group(group))
    sql += " ORDER BY r.display_name"

    with _get_conn() as conn:
        players = [dict(r) for r in conn.execute(sql, params).fetchall()]
    for player in players:
        attach_stages(player, grouping_id)
    players.sort(key=lambda p: (p.get("grp") or "", p.get("rank") or 0, p["display_name"]))

    with _get_conn() as conn:
        if include_scouting and players:
            ids = {p["id"] for p in players}
            by_id: dict[int, list] = {i: [] for i in ids}
            for r in conn.execute("SELECT * FROM squads ORDER BY registrant_id, slot").fetchall():
                if r["registrant_id"] in by_id:
                    by_id[r["registrant_id"]].append(dict(r))
            for p in players:
                p["squads"] = by_id.get(p["id"], [])
    return players


def attach_stages(player: dict, grouping_id=None) -> dict:
    """Fill a registrant's round data, in place.

    Adds `stages` (every round, in playing order) and points `grp` / `rank` at
    the furthest round the player is actually in. Those two keys are the ones
    every existing caller already reads, so filling them here keeps the embed,
    the API and the roster export working off round data without each of them
    having to know where a group lives.

    The furthest round rather than the running one: a player knocked out in the
    qualifiers should still show the group they went out of, not a blank where
    the semifinal they are not in would go.

    Also sets `grouping_id`, so a surface showing a player from outside the
    caller's own grouping can say which one a bare group letter belongs to.
    """
    stages = get_stages(player["id"], grouping_id)
    player["stages"] = stages
    if stages:
        stage = list(stages)[-1]
        player["stage"] = stage
        player["grp"] = stages[stage]["grp"]
        player["rank"] = stages[stage]["rank"]
        player["grouping_id"] = stages[stage]["grouping_id"]
    else:
        player["stage"] = None
        player["grp"] = None
        player["rank"] = None
        player["grouping_id"] = None
    return player


def _profile_from_row(row) -> dict:
    """One stored profile in the shape `champion_duel_engine.semifinal` reads.

    Positions are POWER RANKS here, which is the frame the engine documents and
    the frame the corpus measured them in. A caller pairing this with squad
    boxes has a translation to do; see `champion_duel_odds._profile`.

    `mixed` wins over `n_mixed`, the same way the engine picks between them, so
    a legacy count never reaches a model that has been told which squads.
    """
    out: dict = {}
    if row["types"]:
        out["types"] = row["types"].split(",")
    if row["shape_r21"] is not None or row["shape_r31"] is not None:
        out["shape"] = [row["shape_r21"], row["shape_r31"]]
    if row["mixed"] is not None:
        # '' is a measurement -- "we looked and every squad is pure" -- and has
        # to survive as an empty list rather than collapsing to absent.
        out["mixed"] = [int(i) for i in row["mixed"].split(",") if i]
    elif row["n_mixed"] is not None:
        out["n_mixed"] = row["n_mixed"]
    if row["gorilla"] is not None:
        out["gorilla"] = row["gorilla"]
    return out


def get_profiles(registrant_ids) -> dict[int, dict]:
    """`{registrant_id: profile}` for whichever of these have one measured.

    Absent from the mapping means never measured, which is what the engine
    answers by drawing from the population. A row of nulls would say something
    quite different, so `import_profiles` refuses to write one.
    """
    ids = list(registrant_ids)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM registrant_profiles WHERE registrant_id IN ({marks})",
            tuple(ids),
        ).fetchall()
    return {r["registrant_id"]: _profile_from_row(r) for r in rows}


def get_group_scouting(group_id: int) -> list[dict]:
    """One group's members with their squads and orders, in finishing order.

    `get_group_members` answers "who is in this group", which is what a member
    reading their group wants. This answers "what can we predict about them",
    which is what the odds need, and it is a different query rather than a flag
    on the first one: the group listing is read on every open and would be
    paying for scouting nobody asked for.

    Bulk rather than `get_player` per member. A group of 8 is 8 registrants, and
    a query each for squads and orders per player is 16 round trips inside a
    Discord interaction to answer something two queries cover.

    Rows keep everything `get_group_members` returns, so a caller can render the
    group and score it from one read. `id` is set to the registrant id, because
    that is the key `build_side` and the squad lookups expect, and a group
    membership row's own primary key would silently match nothing.

    The imported profile rides along for the same reason `thp` does: the odds
    read it, and a query that returns everything except the one field the model
    wants is a feature that cannot work in production and still passes its
    tests.
    """
    members = get_group_members(group_id)
    if not members:
        return []
    ids = [m["registrant_id"] for m in members]
    marks = ",".join("?" for _ in ids)
    squads: dict[int, list] = {i: [] for i in ids}
    orders: dict[int, list] = {i: [] for i in ids}
    profiles = get_profiles(ids)
    with _get_conn() as conn:
        for r in conn.execute(
            f"SELECT * FROM squads WHERE registrant_id IN ({marks}) ORDER BY registrant_id, slot",
            tuple(ids),
        ).fetchall():
            squads[r["registrant_id"]].append(dict(r))
        for r in conn.execute(
            f"SELECT * FROM order_history WHERE registrant_id IN ({marks}) "
            "ORDER BY registrant_id, COALESCE(observed_at, created_at) DESC",
            tuple(ids),
        ).fetchall():
            orders[r["registrant_id"]].append(dict(r))
    for m in members:
        rid = m["registrant_id"]
        m["id"] = rid
        m["squads"] = squads.get(rid, [])
        m["orders"] = orders.get(rid, [])
        m["profile"] = profiles.get(rid)
    return members


def get_player(name, server=None, include_scouting: bool = False) -> dict | None:
    """One player with their scouting, or None. Raises AmbiguousPlayer when the
    name exists on several servers and none was given."""
    try:
        player = resolve_registrant(name, server)
    except LookupError:
        return None
    attach_stages(player)
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
        player["profile"] = get_profiles([player["id"]]).get(player["id"])
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


# â”€â”€ Writes (each one audited) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


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


# ── When a second person says something different ─────────────────────────────
#
# Kevin's design: if someone is entering data we already have, surface what we
# have, show them the two pieces, and ask which is correct. Write a history of
# those calls.
#
# The whole difficulty is in deciding when NOT to ask. Two people entering the
# same correct value is the common case and has to pass in silence; a surface
# that questions every re-entry is one nobody enters anything into twice.

#: Squad fields worth arbitrating. Same names as the columns and as
#: `set_squad`'s edit rows, so a call, an edit and a column never disagree
#: about what a field is called.
SQUAD_FIELDS = ("squad_type", "power", "mixed")


def _same_value(field: str, held, offered) -> bool:
    """Whether these two say the same thing to the person who would be asked.

    A power is compared at whole units, which is the precision every surface
    renders it at. `parse_power` deliberately accepts `64.6M` and `64,600,000`
    as the same reading, and in binary floating point they are not: 64.6 * 1e6
    lands a fraction above 64600000.0. Exact comparison would put those two up
    side by side, rendered identically, and ask a member which is right.
    """
    if field == "power":
        return round(float(held)) == round(float(offered))
    return held == offered


def compare_squad(registrant_id: int, slot: int, *, actor=None, **offered) -> list[dict]:
    """Which of these offered squad values contradict one we already hold.

    Empty means write it: either it agrees, or it is the first thing anybody
    has said about that field. Callers pass only the fields the member filled
    in; an omitted one is not an assertion and is never compared.

    **An estimate is never worth arbitrating.** `push_to_bot` writes an
    `estimated` row for nearly the whole field, so treating those as something
    we hold would make the very first real reading of almost every player
    trigger a question. The bot's own guess giving way to somebody reading the
    screen is the system working, not a disagreement.

    **Nor is somebody correcting their own entry.** If the value we hold was
    last written by this same person, their newer reading is simply better and
    asking them to arbitrate against themselves is noise.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM squads WHERE registrant_id = ? AND slot = ?",
            (registrant_id, slot),
        ).fetchone()
    if row is None or row["source"] == "estimated":
        return []
    if actor and row["updated_by"] and row["updated_by"] == actor.get("discord_user_id"):
        return []

    out = []
    for field in SQUAD_FIELDS:
        new = offered.get(field)
        held = row[field]
        if new is None or held is None or _same_value(field, held, new):
            continue
        out.append({"field": field, "held": held, "offered": new})
    return out


def record_disagreement(registrant_id: int, *, target, slot, rows, chose, actor, edits=None):
    """Log one "which of these is right" call, one row per disputed field.

    `chose` is 'held' or 'offered' and is the same for every row: the member
    answered one question about the entry, not one per field.

    Recorded whichever way it went. The call where the member confirmed what we
    already hold changes nothing and is the more interesting half of the
    history: it is the only evidence that a stored value has been challenged
    and survived.

    `edits` is `set_squad`'s field-to-edit-id map. Linked by FIELD rather than
    by position: one entry can write an edit for a field nobody disputed, so
    the two lists are not the same length and pairing them by index would hang
    a call off the wrong change.
    """
    if chose not in ("held", "offered"):
        raise ValueError("chose must be 'held' or 'offered'")
    now = _now()
    edits = edits or {}
    with _get_conn() as conn:
        for row in rows:
            conn.execute(
                """
                INSERT INTO disagreements
                    (target, registrant_id, slot, field, held_value, offered_value,
                     chose, edit_id, actor_discord_id, actor_name, actor_guild_id,
                     created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    target,
                    registrant_id,
                    slot,
                    row["field"],
                    None if row["held"] is None else str(row["held"]),
                    None if row["offered"] is None else str(row["offered"]),
                    chose,
                    edits.get(row["field"]) if chose == "offered" else None,
                    actor["discord_user_id"],
                    actor.get("discord_name"),
                    actor.get("guild_id"),
                    now,
                ),
            )
    return len(rows)


def list_disagreements(*, registrant_id=None, actor=None, limit: int = 50, offset: int = 0):
    """Newest first. The history of every call, not only the ones that changed
    something."""
    sql = (
        "SELECT d.*, r.display_name, r.server FROM disagreements d "
        "LEFT JOIN registrants r ON r.id = d.registrant_id WHERE 1=1"
    )
    where: list[str] = []
    params: list = []
    if registrant_id is not None:
        where.append(" AND d.registrant_id = ?")
        params.append(registrant_id)
    if actor:
        where.append(" AND d.actor_discord_id = ?")
        params.append(str(actor))

    clause = "".join(where)
    with _get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                sql + clause + " ORDER BY d.id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        ]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM disagreements d WHERE 1=1" + clause, params
        ).fetchone()["n"]
    return {"disagreements": rows, "total": total}


def set_squad(
    registrant_id, slot, squad_type=None, power=None, *, actor, source="edited", mixed=None
):
    """Set one squad slot. Each changed field becomes its own edit row, so
    reverting a wrong type does not also revert a correct power entered in the
    same request.

    `mixed` is 1 when this squad is 4-of-a-type, 0 when somebody looked and it
    is pure, and None for "not asked". The three are genuinely different to
    the model: it samples a mixed pair from the population for a squad nobody
    has reported, and treats a recorded 0 as a measurement. So None leaves the
    stored value alone rather than clearing it, exactly like the other two
    fields.
    """
    if slot not in (1, 2, 3):
        raise ValueError("slot must be 1, 2 or 3")
    if squad_type is not None and squad_type not in VALID_TYPES:
        raise ValueError(f"squad_type must be one of {VALID_TYPES}")
    if source not in VALID_SOURCES:
        raise ValueError(f"source must be one of {VALID_SOURCES}")

    # Keyed by field as well as listed, so a caller that has to link one edit
    # to one field can, without depending on the order they were written in.
    edits: dict[str, int] = {}
    edit_ids = []
    with _get_conn() as conn:
        if not conn.execute("SELECT 1 FROM registrants WHERE id = ?", (registrant_id,)).fetchone():
            raise LookupError(f"no registrant {registrant_id}")

        row = conn.execute(
            "SELECT * FROM squads WHERE registrant_id = ? AND slot = ?", (registrant_id, slot)
        ).fetchone()
        old_type = row["squad_type"] if row else None
        old_power = row["power"] if row else None
        old_mixed = row["mixed"] if row else None
        new_type = old_type if squad_type is None else squad_type
        new_power = old_power if power is None else float(power)
        new_mixed = old_mixed if mixed is None else int(bool(mixed))

        conn.execute(
            """
            INSERT INTO squads (registrant_id, slot, squad_type, power, mixed, source,
                                updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(registrant_id, slot) DO UPDATE SET
                squad_type = excluded.squad_type,
                power      = excluded.power,
                mixed      = excluded.mixed,
                source     = excluded.source,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (
                registrant_id,
                slot,
                new_type,
                new_power,
                new_mixed,
                source,
                _now(),
                actor["discord_user_id"],
            ),
        )
        for field, given, old, new in (
            ("squad_type", squad_type, old_type, new_type),
            ("power", power, old_power, new_power),
            ("mixed", mixed, old_mixed, new_mixed),
        ):
            if given is None or old == new:
                continue
            edits[field] = _record_edit(
                conn,
                target="squad",
                registrant_id=registrant_id,
                slot=slot,
                field=field,
                old=old,
                new=new,
                actor=actor,
            )
            edit_ids.append(edits[field])
    return {
        "registrant_id": registrant_id,
        "slot": slot,
        "edit_ids": edit_ids,
        "edits": edits,
    }


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


# â”€â”€ Bulk import of scouting â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
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
        return None, f"{name!r} is on several servers ({servers}) â€” give one"
    except LookupError:
        return None, f"no registrant matches {name!r}"


def _import_would_downgrade(existing: str, incoming: str) -> bool:
    """Whether an imported value must give way to what is already stored.

    Two separate rules, and conflating them is what let a re-import quietly
    revert a hand correction:

    - **`edited` outranks everything an import carries.** A person looked at
      the game and typed what they saw. An import knows nothing that beats
      that, so even a fresh `observed` capture leaves it alone. Reverting a
      correction is what the edit log and `âª Revert an edit` are for, on
      purpose and attributed, rather than a side effect of loading a file.
    - **`observed` gives way only to another sighting.** An estimate is
      derived from total hero power; a sighting is someone reading the
      screen. A newer sighting may legitimately replace an older one.

    The bug this replaces guarded on `incoming == "estimated"` alone, so an
    imported `observed` row overwrote a correction and the import reported
    nothing kept.
    """
    if existing == "edited":
        return True
    return existing == "observed" and incoming == "estimated"


def import_squads(rows: list[dict], *, actor) -> dict:
    """Seed squad values in bulk.

    **An import never downgrades.** A slot carrying an `edited` correction
    keeps it whatever arrives; a slot carrying an `observed` sighting keeps it
    against an `estimated` value. That is the whole reason `squads.source`
    exists, and re-running an import after a scout has corrected something
    must not undo their work. See `_import_would_downgrade`.

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
            if existing and _import_would_downgrade(existing["source"], source):
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


def record_import(*, door, results, grouping_id=None, stage=None, actor=None) -> int:
    """Log one import, from whichever door it came through.

    `results` is the per-section dict each importer returns, keyed by section:
    `{"registrants": {...}, "squads": {...}}`. Sections the payload left out
    are simply absent and count zero, which is the truth about that run.

    Kept even when everything was skipped. An import that landed nothing is
    exactly the run somebody will come asking about, and a log that only
    records successes cannot answer them.
    """
    counts = {section: 0 for section in ("registrants", "squads", "orders", "profiles")}
    cleared = skipped = 0
    for section, result in (results or {}).items():
        if not isinstance(result, dict):
            continue
        if section in counts:
            # `import_registrants` reports a `total`; the rest report what they
            # wrote. Both answer "how much of this section landed", which is
            # the only question this column is asked.
            counts[section] = result.get("applied", result.get("total", 0)) or 0
        cleared += result.get("cleared", 0) or 0
        skipped += result.get("skipped", 0) or 0

    actor = actor or {}
    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO import_log
                (grouping_id, stage, registrants, squads, orders, profiles, cleared,
                 skipped, door, actor_discord_id, actor_name, actor_guild_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grouping_id,
                stage,
                counts["registrants"],
                counts["squads"],
                counts["orders"],
                counts["profiles"],
                cleared,
                skipped,
                door,
                actor.get("discord_user_id"),
                actor.get("discord_name"),
                actor.get("guild_id"),
                _now(),
            ),
        )
    return cur.lastrowid


def list_imports(*, grouping_id=None, limit: int = 50, offset: int = 0) -> dict:
    """Newest first. Who has loaded what, and when."""
    where, params = "", []
    if grouping_id is not None:
        where = " AND grouping_id = ?"
        params.append(grouping_id)
    with _get_conn() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM import_log WHERE 1=1" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        ]
        total = conn.execute(
            "SELECT COUNT(*) AS n FROM import_log WHERE 1=1" + where, params
        ).fetchone()["n"]
    return {"imports": rows, "total": total}


def _as_rank(value) -> int:
    """One squad position, 0-2, or a ValueError saying why it is not one.

    Bools are refused outright. `int(True)` is 1 and would land a purity
    penalty on the second-biggest squad of anyone whose profile carried a flag
    where a position belongs.
    """
    if isinstance(value, bool):
        raise ValueError(f"{value!r} is not a squad position")
    rank = int(value)
    if not 0 <= rank <= 2:
        raise ValueError(f"{value!r} is not a squad position (0-2)")
    return rank


#: Every profile key this version understands. Anything else is ignored where
#: there is a real measurement beside it, and refused where there is not.
PROFILE_KEYS = frozenset({"types", "shape", "mixed", "n_mixed", "gorilla"})


def _profile_columns(profile) -> tuple[dict | None, str | None]:
    """One imported profile as storable columns, or a reason it is unusable.

    **Validated here rather than passed through, and that is the whole point of
    the function.** The engine reads a profile deep inside a trial: `int(i)` on
    a `mixed` entry, `tuple(profile["types"])` indexed three deep in `lineup`.
    A bad value there raises after the interaction has been deferred, and
    `build_odds_embed` catches only `NotEnoughData` -- so the member watches a
    spinner that never resolves and nothing says why. Refusing the row at the
    door costs that one player their profile and nothing else.

    Unknown keys are ignored rather than refused: a newer simulator may fit
    something this version has no column for, and dropping the row over it
    would throw away the measurements we do understand.

    **But a row of nothing-we-recognise is not a row of nothing.** An empty
    result tells `import_profiles` to retract, and combining that with the rule
    above would turn one producer-side key rename into a mass deletion of every
    profile we hold. So a payload that measured something, in words this
    version cannot read, is refused rather than obeyed.
    """
    if not isinstance(profile, dict):
        return None, "profile is not an object"
    cols: dict = {
        "types": None,
        "shape_r21": None,
        "shape_r31": None,
        "mixed": None,
        "n_mixed": None,
        "gorilla": None,
    }

    types = profile.get("types")
    if types is not None:
        # Exactly three. `lineup` indexes this list once per slot, so a short
        # one is an IndexError inside a trial rather than a partial reading.
        if not isinstance(types, (list, tuple)) or len(types) != 3:
            return None, f"types {types!r} is not three squads"
        if any(t not in VALID_TYPES for t in types):
            return None, f"types {types!r} names something outside {VALID_TYPES}"
        cols["types"] = ",".join(types)

    shape = profile.get("shape")
    if shape is not None:
        if not isinstance(shape, (list, tuple)) or len(shape) != 2:
            return None, f"shape {shape!r} is not (r21, r31)"
        ratios: list = []
        for ratio in shape:
            if ratio is None:
                ratios.append(None)
                continue
            try:
                value = float(ratio)
            except (TypeError, ValueError):
                return None, f"shape {shape!r} is not numeric"
            # Both ratios are against the biggest squad, which cannot be
            # outranked by definition. Above 1 is a transcription slip, and it
            # would not raise -- the engine sorts, so the lineup silently comes
            # out in a different order from the one the profile describes.
            if not 0 < value <= 1:
                return None, f"shape ratio {ratio!r} is outside (0, 1]"
            ratios.append(value)
        # Squad 3 cannot outrank squad 2, which is what a transposed pair
        # would say. `shape_from_power` clamps its own output the same way;
        # `_profile_shape` does not clamp a given one, and the engine's later
        # sort would reorder the lineup without reordering `types`.
        if ratios[0] is not None and ratios[1] is not None and ratios[1] > ratios[0]:
            return None, f"shape {shape!r} has squad 3 above squad 2"
        cols["shape_r21"], cols["shape_r31"] = ratios

    mixed = profile.get("mixed")
    if mixed is not None:
        if isinstance(mixed, (str, bytes)) or not isinstance(mixed, (list, tuple, set)):
            return None, f"mixed {mixed!r} is not a list of positions"
        try:
            ranks = sorted({_as_rank(i) for i in mixed})
        except (TypeError, ValueError) as exc:
            return None, f"mixed {mixed!r}: {exc}"
        cols["mixed"] = ",".join(str(rank) for rank in ranks)

    # Only read when nothing said WHICH squads. The engine makes the same
    # choice between the two, and storing both would leave the row ambiguous
    # about which a later reader should believe.
    n_mixed = profile.get("n_mixed")
    if n_mixed is not None and cols["mixed"] is None:
        if isinstance(n_mixed, bool):
            return None, f"n_mixed {n_mixed!r} is not a count"
        try:
            count = int(n_mixed)
        except (TypeError, ValueError):
            return None, f"n_mixed {n_mixed!r} is not a count"
        if count < 0:
            return None, f"n_mixed {n_mixed!r} is negative"
        # A measured zero says "we looked and every squad is pure", which is
        # exactly what an empty `mixed` says -- and unlike a count it names no
        # positions, so it stays true however the reading turned out.
        # Normalised now so the legacy column only ever means "n of them and we
        # cannot say which", which is the case that needs handling with care.
        if count:
            cols["n_mixed"] = count
        else:
            cols["mixed"] = ""

    gorilla = profile.get("gorilla")
    if gorilla is not None:
        try:
            cols["gorilla"] = _as_rank(gorilla)
        except (TypeError, ValueError) as exc:
            return None, f"gorilla {gorilla!r}: {exc}"

    if all(value is None for value in cols.values()) and set(profile) - PROFILE_KEYS:
        return None, f"nothing this version can read in {sorted(set(profile) - PROFILE_KEYS)}"
    # An all-null result is not an error. It is a payload saying "here is what
    # we hold for this player" and holding nothing, which is a retraction --
    # `import_profiles` deletes on it rather than storing a row of nulls.
    return cols, None


def import_profiles(rows: list[dict], *, actor) -> dict:
    """Load the per-player measurements the semifinal model takes as input.

    Each row is `{name, server, profile}` -- the block `push_to_bot.py` has
    been sending since the 1.5 contract landed and nothing here read.

    **A profile is replaced whole, not merged key by key, and a payload row
    measuring nothing deletes it.** Each import is a re-fit of the entire
    corpus, so a measurement that has dropped out of the fit -- a sighting
    reclassified, a fight re-read -- has to drop out of the row with it.
    Merging would keep a retracted measurement alive with nothing able to clear
    it, which is the shape of bug that shows up only as odds that are quietly
    wrong forever.

    **The retraction only reaches players the payload names.** A player who
    drops out of the fit altogether is simply absent, and absent has to keep
    meaning "this payload says nothing about them" -- a block that cleared
    every profile it did not mention would let one alliance's import wipe
    another's, which is the destructive shape the import gate exists to
    contain. So the producer retracts by *sending* an empty profile. Today
    `player_profiles.all_profiles()` omits those instead, which leaves that one
    case uncovered end to end; the missing half is in the simulator.

    Replacing whole is the opposite of `import_squads`, and deliberately so: a
    squad row may carry a correction somebody typed, and an import must never
    walk over it. Nobody hand-enters a profile. What a member does enter is the
    per-box `mixed` flag on `squads`, which this never touches -- the two meet
    at read time in `champion_duel_odds._profile`, where the member's answer
    wins.
    """
    applied = cleared = skipped = 0
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

            cols, problem = _profile_columns(row.get("profile"))
            if problem:
                problems.append(f"{row.get('name')!r} profile: {problem}")
                skipped += 1
                continue

            if not any(value is not None for value in cols.values()):
                # Nothing measurable in the row, so nothing measured -- and a
                # profile we can no longer justify has to go, not sit there
                # feeding a retracted measurement to every future run. Counted
                # separately: clearing 400 profiles is a very different event
                # from loading 400 and the summary must not read the same.
                cleared += conn.execute(
                    "DELETE FROM registrant_profiles WHERE registrant_id = ?",
                    (registrant_id,),
                ).rowcount
                continue

            conn.execute(
                """
                INSERT INTO registrant_profiles
                    (registrant_id, types, shape_r21, shape_r31, mixed, n_mixed,
                     gorilla, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(registrant_id) DO UPDATE SET
                    types      = excluded.types,
                    shape_r21  = excluded.shape_r21,
                    shape_r31  = excluded.shape_r31,
                    mixed      = excluded.mixed,
                    n_mixed    = excluded.n_mixed,
                    gorilla    = excluded.gorilla,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (
                    registrant_id,
                    cols["types"],
                    cols["shape_r21"],
                    cols["shape_r31"],
                    cols["mixed"],
                    cols["n_mixed"],
                    cols["gorilla"],
                    now,
                    actor_id,
                ),
            )
            applied += 1

    return {
        "applied": applied,
        "cleared": cleared,
        "skipped": skipped,
        "problems": problems[:50],
    }


# â”€â”€ Audit + revert â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


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
    bare failure â€” an admin needs to see the newer correction before deciding
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
    """Every edit in a date range, oldest first â€” the spreadsheet view.

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
    """Who has contributed what. The contributor graph IS the user base â€” no
    separate table needed to know which servers have people entering data."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT actor_discord_id, actor_name, COUNT(*) AS edits, "
            "MAX(created_at) AS last_seen FROM edits "
            "GROUP BY actor_discord_id ORDER BY edits DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# â”€â”€ Sessions â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def create_session(discord_user_id, discord_name=None, can_write=False, writer_guild_id=None):
    """Mint a session. Returns the plaintext token exactly once â€” only its hash
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


# â”€â”€ OAuth hand-off codes â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


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


# ── Data removal ────────────────────────────────────────────────────────────
#
# Actioning a removal request from someone with a Discord identity (#517).
# Champion Duel *player* records are out of scope by the #499 decision: there is
# no player-keyed route here and there should not be one. A player row is about
# a name the game shows everyone, and this is about the people who used the bot.
#
# Two shapes, and what a table gets is decided by what its rows are.
#
#   * A record a person WROTE keeps its contribution and loses its attribution.
#     The reading is information the game already shows anyone, and removing it
#     would take it from the alliances it was contributed for.
#   * A record ABOUT a person goes whole. Nothing survives scrubbing a row whose
#     entire content is "this person signed in".
#
# `sessions` and `auth_codes` are the second kind. Everything else here is the
# first.
#
# The scrub sentinel is NULL wherever the column allows it, because NULL is
# already this schema's "nobody said" and every reader of these columns treats
# it that way. `edits.actor_discord_id` and `disagreements.actor_discord_id` are
# NOT NULL, so they take '' instead -- the readers treat both as falsy, and
# rewriting two live tables to relax a constraint is a worse trade than one
# inconsistent sentinel.
#
# Every scrub predicate is disjoint from every delete predicate, so a preview
# and the run it previews cannot disagree about a row: nothing is counted by one
# pass and removed by another.

_REMOVAL_DELETES: tuple[tuple[str, str], ...] = (
    ("sessions", "discord_user_id = :sid"),
    ("auth_codes", "discord_user_id = :sid"),
)

_REMOVAL_SCRUBS: tuple[tuple[str, str, str], ...] = (
    (
        "edits",
        "actor_discord_id = '', actor_name = NULL, actor_guild_id = NULL",
        "actor_discord_id = :sid",
    ),
    (
        "disagreements",
        "actor_discord_id = '', actor_name = NULL, actor_guild_id = NULL",
        "actor_discord_id = :sid",
    ),
    (
        "import_log",
        "actor_discord_id = NULL, actor_name = NULL, actor_guild_id = NULL",
        "actor_discord_id = :sid",
    ),
    ("registrants", "added_by = NULL", "added_by = :sid"),
    ("squads", "updated_by = NULL", "updated_by = :sid"),
    ("registrant_profiles", "updated_by = NULL", "updated_by = :sid"),
    ("order_history", "created_by = NULL", "created_by = :sid"),
    (
        "groupings",
        "created_by_discord_id = NULL, created_by_guild_id = NULL",
        "created_by_discord_id = :sid",
    ),
    ("guild_warzone", "set_by_discord_id = NULL", "set_by_discord_id = :sid"),
)


def purge_user_data(discord_user_id, *, apply: bool = False) -> dict:
    """Remove one person from the Champion Duel database.

    With `apply=False` (the default) this counts what a run would touch and
    changes nothing, so the same call can render a preview and then do the work.
    Both paths walk the same two spec tables above and share every predicate --
    a preview that ran a different query from the run would be worth less than
    no preview at all.

    Returns `{"deleted": {table: rows}, "scrubbed": {table: rows},
    "applied": bool}`, with tables that matched nothing left out. A removal
    nobody can audit is a removal nobody can trust, so the counts are the point
    rather than a debugging aid.
    """
    sid = str(discord_user_id).strip()
    out: dict = {"deleted": {}, "scrubbed": {}, "applied": bool(apply)}
    if not sid:
        return out
    params = {"sid": sid}
    with _get_conn() as conn:
        for table, where in _REMOVAL_DELETES:
            if apply:
                n = conn.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount  # noqa: S608
            else:
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}",  # noqa: S608
                    params,
                ).fetchone()[0]
            if n:
                out["deleted"][table] = n
        for table, sets, where in _REMOVAL_SCRUBS:
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
    return out

"""Odds that have already been worked out, held against the data they came from.

WHY THIS EXISTS. `champion_duel_odds` caches in memory, keyed on the specs, and
that cache is genuinely good: it needs no invalidation, because a changed group
simply misses. What it cannot do is survive a deploy, and it cannot be filled by
anything other than somebody waiting for it. Session 2 puts `Odds of advancing`
on the landing rather than three clicks down, which turns a rare deliberate
press into the default state of the bot -- so the answer has to be sitting there
before anyone asks.

THE CORRECTNESS MECHANISM IS THE HASH, NOT A FLAG. Every read recomputes the
fingerprint from live rows and compares it to the one the stored answer was
computed under. Nothing has to remember to invalidate anything: a write path
that forgets this module exists still cannot serve a stale number, because the
staleness is derived rather than announced. That is the one property the
in-memory cache has that most precompute designs lose, and it is deliberately
kept. There is no dirty flag anywhere in this file, and adding one would be a
regression rather than an optimisation.

THREE THINGS ARE IN THE KEY THAT THE IN-MEMORY CACHE NEVER NEEDED, because that
cache dies with the process and a table does not:

  * `champion_duel_engine.__version__`. This is the one that will bite. A deploy
    that moves the pin clears an in-memory cache for free. A table survives it,
    and would then serve last version's numbers under the new model -- silently,
    in the units the surface renders.
  * `matrix_trials` and the bracket `trials`, neither of which is in the group
    key today.
  * PAYLOAD_SCHEMA, so changing what gets stored invalidates old rows rather
    than half-reading them.

AND ONE THING IS DELIBERATELY *NOT* IN THE KEY: display names. The in-memory
cache includes them, on the sound reasoning that a spelling correction must not
hand back old names against a new group. Here the answer is stored against
registrant ids and the names are re-attached on every read, so a rename
re-renders rather than re-running. The model never sees a name -- specs go in
keyed by position -- so a rename cannot change a number, and paying 60 seconds
of CPU for one would be paying it for nothing.

WHAT THE VOLUME ALLOWS. The Railway volume is a thin-provisioned ZFS zvol: it
allocates blocks on write and never gives them back, and `fstrim` does not work
on it (`CLAUDE.md`). So rows are updated IN PLACE, keyed on the group, and there
is no history table of past recomputes and no VACUUM. Kevin's rule, stated
directly: hold whatever ran until another measurement is run against that piece
of information, then replace it.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import champion_duel_db as db
import champion_duel_odds as odds

#: Bumped whenever the shape of a stored payload changes. It is part of the
#: fingerprint, so a bump invalidates every row rather than letting an old one
#: be half-read under the new reader.
PAYLOAD_SCHEMA = 1

#: How long a group must go unwritten before the sweeper will run it. A member
#: filling three squad boxes writes three times and an officer reconciling a
#: group writes eight; without a quiet window, one editing session becomes eight
#: runs of the same group. This delays nobody, because the press path never
#: waits on the sweeper.
QUIET_MINUTES = 5


#: Rounds this sweeper will do, which is every round that HAS a model rather
#: than a list kept in step with one by hand. Read at call time, because
#: `STAGES_WITH_A_MODEL` is itself built from what the pinned engine can do: the
#: knockouts arrived in 1.12.0, and an older pin must not have its bracket
#: queued, fingerprinted and then failed once a minute forever.
#:
#: The qualifiers have no model and so are absent automatically. That is a
#: product decision as well as a cost one (2026-08-24): a qualifier group is 100
#: players, the model needs a power for every one of them, and precompute makes
#: runs cheap rather than data complete. What that round gets instead is the
#: neighbours view, which needs no simulation at all and lands in the IA work,
#: not here.
def swept_stages() -> tuple:
    return tuple(odds.STAGES_WITH_A_MODEL)


class NoModel(Exception):
    """This round has no model, so there is nothing to precompute."""


@dataclass
class Stored:
    """An answer that was worked out earlier, and how much to trust it.

    `state` is one of:

      fresh   -- the fingerprint matches. This is exactly what a run right now
                 would produce, and a surface should show it with no timestamp
                 and no caveat.
      stale   -- the same people, different data. Showable, but only with an
                 "as of" line: the numbers are about the right group and were
                 true when they were computed.
      missing -- nothing stored, or a different SET OF PEOPLE. Not a weaker
                 kind of stale: in a group of eight, replacing one rival moves
                 every row, so an answer computed against a different field is
                 wrong rather than old and must never be shown.
    """

    state: str
    odds: object | None = None
    computed_at: str | None = None
    refusal: str | None = None

    @property
    def showable(self) -> bool:
        return self.state in ("fresh", "stale") and self.odds is not None


def init_store() -> None:
    """Create the table. Safe to re-run, and called from `on_ready` beside
    `champion_duel_db.init_db`.

    Lives in the Champion Duel database rather than a file of its own: it is
    global tournament data with the same lifecycle as everything around it, and
    it can be wiped between rounds without consequence -- every row in here is
    derivable from the rows next to it.
    """
    with db._get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS odds_runs (
                -- One row per group, replaced in place. No history: the volume
                -- never gives blocks back, and a past recompute has no reader.
                group_id       INTEGER PRIMARY KEY,
                stage          TEXT    NOT NULL,
                -- What the answer was computed against. Recomputed from live
                -- rows on every read; if it differs, the answer is stale.
                fingerprint    TEXT    NOT NULL,
                -- Sorted registrant ids, compared BEFORE the fingerprint.
                -- Different people is a different question, not an older answer.
                member_ids     TEXT    NOT NULL,
                -- JSON, keyed by registrant id rather than by name, so a rename
                -- re-renders instead of re-running.
                payload        TEXT,
                -- Why the group could not be modelled, when payload is NULL.
                -- Stored so the sweeper stops picking it up every minute: the
                -- refusal is cheap but it is not free, and it cannot change
                -- until the data does -- at which point the fingerprint moves
                -- and it is queued again on its own.
                refusal        TEXT,
                computed_at    TEXT    NOT NULL,
                run_seconds    REAL,
                -- Stamped on read. The sweeper works in last-viewed order,
                -- because most groups are never looked at.
                last_viewed_at TEXT,
                FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
            )
        """)
        try:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_odds_runs_sweep ON odds_runs(last_viewed_at DESC)"
            )
        except sqlite3.OperationalError as exc:  # pragma: no cover
            print(f"[CHAMPION_DUEL] odds_runs index skipped: {exc}")


# ── the fingerprint ──────────────────────────────────────────────────────────


def _engine_version() -> str:
    """The pinned engine, or a marker that is deliberately never a version.

    An absent engine still gets a fingerprint rather than an exception, so a bot
    running without one reads the table as entirely stale instead of failing on
    every lookup.
    """
    try:
        import champion_duel_engine

        return str(getattr(champion_duel_engine, "__version__", "unknown"))
    except Exception:  # noqa: BLE001 - degraded, not broken
        return "absent"


def _trial_counts(stage: str) -> dict:
    """The trial counts that will actually be spent on this round.

    Read off the module rather than copied, so raising `BRACKET_TRIALS` or
    `MATRIX_TRIALS` invalidates every stored bracket by itself. That is the
    behaviour that was missing: neither number is in the in-memory key today,
    so a constant could move and warm answers computed under the old one would
    keep being served.
    """
    if stage not in odds.STAGES_WITH_A_MODEL:
        raise NoModel(f"there is no model for the {stage} round")
    if stage == "knockouts":
        return {"trials": odds.BRACKET_TRIALS, "matrix_trials": odds.MATRIX_TRIALS}
    config = odds._models().get(stage)
    if config is None:
        raise NoModel(f"there is no model for the {stage} round")
    return {"trials": config["trials"]}


def fingerprint(members: list[dict], *, stage: str, seed: int = 42, jitter: bool = True) -> tuple:
    """What this group's answer depends on, and who is in it.

    Returns `(fingerprint, member_ids)`. They are separate on purpose and
    compared in that order reversed -- ids first, because a different set of
    people is a different question rather than a staler answer.

    ORDER-INDEPENDENT BY CONSTRUCTION. The specs are paired with their
    registrant ids and sorted by id before hashing, so the same eight people
    with the same data fingerprint the same however the query happened to sort
    them. Without that, `get_group_scouting` returning "in finishing order"
    would re-run the whole group every time a rank was recorded, which is a
    change the model's inputs never saw.
    """
    specs, _display, _missing = odds._specs(members)
    # `_specs` names each spec by its position in `members`, and skips anyone
    # with nothing to place them by -- so a spec's name indexes back into the
    # list it was built from. That is the only bridge between an engine result
    # and a person, and it is why the pairing happens here rather than being
    # reconstructed later.
    ids = [m.get("id") for m in members]
    if any(i is None for i in ids):
        # `get_group_scouting` sets `id` to the registrant id; `get_group_members`
        # does not set it at all, and carries `registrant_id` instead. Passing the
        # second is a mistake worth naming, because without this the sort below
        # falls through to comparing two dicts and raises `TypeError` -- which in
        # `due()` takes down the whole tick rather than the one group.
        raise NoModel("these rows carry no registrant id; use get_group_scouting")
    paired = sorted(
        (
            (ids[int(spec["name"])], {k: v for k, v in spec.items() if k != "name"})
            for spec in specs
        ),
        # Explicitly on the id. Without a key, two rows with equal ids would make
        # Python compare the spec dicts beside them, which raises.
        key=lambda pair: pair[0],
    )
    material = {
        "schema": PAYLOAD_SCHEMA,
        "engine": _engine_version(),
        "stage": stage,
        "seed": seed,
        "jitter": jitter,
        "specs": paired,
        **_trial_counts(stage),
    }
    blob = json.dumps(material, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), sorted(i for i in ids if i is not None)


# ── payloads ─────────────────────────────────────────────────────────────────


def _to_payload(result, members: list[dict]) -> dict:
    """The answer, keyed by registrant id.

    Rows come off the engine keyed by row POSITION (`OddsRow.key`), which is
    meaningless once the query that produced it has been forgotten. Translating
    to a registrant id here is what lets the names be re-attached later, and it
    is why `key` was added to those dataclasses.
    """
    ids = [m.get("id") for m in members]

    def rid(row):
        return ids[int(row.key)] if row.key is not None else None

    if isinstance(result, odds.BracketOdds):
        return {
            "kind": "bracket",
            "trials": result.trials,
            "matrix_trials": result.matrix_trials,
            "draw_known": result.draw_known,
            "rows": [
                {"id": rid(r), "reach": {k: round(v, 6) for k, v in r.reach.items()}}
                for r in result.rows
            ],
        }
    return {
        "kind": "group",
        "trials": result.trials,
        "advance": result.advance,
        "rows": [
            {
                "id": rid(r),
                "advance": round(r.advance, 6),
                "win_group": round(r.win_group, 6),
                "points_mean": round(r.points_mean, 4),
                "points_sd": round(r.points_sd, 4),
            }
            for r in result.rows
        ],
    }


def _from_payload(payload: dict, members: list[dict]):
    """Rebuild the answer, under whatever the names are NOW.

    A row whose registrant is no longer in the group is dropped rather than
    rendered nameless. That case is already handled a level up -- a changed
    member set reads as `missing` and never reaches here -- so this is the
    belt to that braces, and it fails by showing less rather than by showing a
    stranger.
    """
    names = {m.get("id"): (m.get("display_name") or "?") for m in members}
    # The row POSITION each registrant sits at in `members`, which is what
    # `OddsRow.key` and `BracketRow.key` mean off the engine (`_specs` keys the
    # specs `str(i)`). Rebuilding without it left every stored row keyless, so
    # a caller could re-render the group and could NOT find one named player in
    # it -- which is the whole reason `key` was added to those dataclasses.
    # Names cannot stand in: two members of a group can share a display name,
    # and keying by position is precisely what stops that collapsing them.
    at = {m.get("id"): str(i) for i, m in enumerate(members)}

    if payload.get("kind") == "bracket":
        rows = [
            odds.BracketRow(name=names[r["id"]], reach=dict(r["reach"]), key=at[r["id"]])
            for r in payload["rows"]
            if r["id"] in names
        ]
        return odds.BracketOdds(
            rows=rows,
            trials=payload["trials"],
            matrix_trials=payload["matrix_trials"],
            draw_known=payload.get("draw_known", False),
        )

    rows = [
        odds.OddsRow(
            name=names[r["id"]],
            advance=r["advance"],
            win_group=r["win_group"],
            points_mean=r["points_mean"],
            points_sd=r["points_sd"],
            key=at[r["id"]],
        )
        for r in payload["rows"]
        if r["id"] in names
    ]
    return odds.GroupOdds(rows=rows, trials=payload["trials"], advance=payload["advance"])


# ── reading ──────────────────────────────────────────────────────────────────


def lookup(group_id: int, members: list[dict], *, stage: str, mark_viewed: bool = True) -> Stored:
    """What we hold for this group, and whether it is still true.

    Stamps `last_viewed_at` by default, because a lookup IS a view -- that is
    what orders the sweeper, and taking the stamp here means no surface has to
    remember to report one.
    """
    try:
        current, ids = fingerprint(members, stage=stage)
    except NoModel:
        return Stored(state="missing")

    with db._get_conn() as conn:
        row = conn.execute("SELECT * FROM odds_runs WHERE group_id = ?", (group_id,)).fetchone()
        if mark_viewed:
            # Inserted when there is no row yet, not just updated. A group nobody
            # has computed is exactly the one somebody is most likely to be
            # waiting on, and if only computed groups could record a reader then
            # `due()` would sort the group being pressed right now BEHIND every
            # stale-but-computed group in the tournament -- the precise inversion
            # of what sweeping in last-viewed order is for.
            #
            # The marker carries an empty fingerprint and member set, which match
            # nothing, so it reads as `missing` and is due on the next tick.
            conn.execute(
                """
                INSERT INTO odds_runs
                    (group_id, stage, fingerprint, member_ids, payload, computed_at,
                     last_viewed_at)
                VALUES (?, ?, '', '', NULL, ?, ?)
                ON CONFLICT(group_id) DO UPDATE SET last_viewed_at = excluded.last_viewed_at
                """,
                (group_id, stage, db._now(), db._now()),
            )

    if row is None:
        return Stored(state="missing")

    # Ids first. Same people with different numbers is an older answer about
    # you; different people is an answer about somebody else's group.
    if row["member_ids"] != json.dumps(ids):
        return Stored(state="missing")

    if row["payload"] is None:
        # A stored refusal. Only meaningful while the data it refused is still
        # the data we hold -- past that it is queued again on its own.
        if row["fingerprint"] == current:
            return Stored(state="missing", refusal=row["refusal"])
        return Stored(state="missing")

    try:
        result = _from_payload(json.loads(row["payload"]), members)
    except Exception as exc:  # noqa: BLE001 - a bad row must not break a press
        print(f"[CHAMPION_DUEL] stored odds for group {group_id} unreadable: {exc}")
        return Stored(state="missing")

    return Stored(
        state="fresh" if row["fingerprint"] == current else "stale",
        odds=result,
        computed_at=row["computed_at"],
    )


# ── writing ──────────────────────────────────────────────────────────────────


def store(group_id: int, members: list[dict], result, *, stage: str, run_seconds=None) -> None:
    """Replace this group's row. In place, and there is only ever one."""
    current, ids = fingerprint(members, stage=stage)
    with db._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO odds_runs
                (group_id, stage, fingerprint, member_ids, payload, refusal,
                 computed_at, run_seconds)
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                stage = excluded.stage,
                fingerprint = excluded.fingerprint,
                member_ids = excluded.member_ids,
                payload = excluded.payload,
                refusal = NULL,
                computed_at = excluded.computed_at,
                run_seconds = excluded.run_seconds
            """,
            (
                group_id,
                stage,
                current,
                json.dumps(ids),
                json.dumps(_to_payload(result, members)),
                db._now(),
                run_seconds,
            ),
        )


def store_refusal(group_id: int, members: list[dict], reason: str, *, stage: str) -> None:
    """Record that this group cannot be modelled as it currently stands.

    The plan this came from says a refusal "costs nothing and never needs
    storing". It costs nothing to PRODUCE -- both odds functions refuse before
    they simulate anything -- but not storing it means the sweeper picks the
    same unmodellable group every single tick and refuses it again forever,
    never reaching the group behind it. Storing it against the fingerprint
    keeps it out of the queue only until its data changes, which is exactly as
    long as the refusal is true.
    """
    try:
        current, ids = fingerprint(members, stage=stage)
    except NoModel:
        return
    with db._get_conn() as conn:
        conn.execute(
            """
            INSERT INTO odds_runs
                (group_id, stage, fingerprint, member_ids, payload, refusal, computed_at)
            VALUES (?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                stage = excluded.stage,
                fingerprint = excluded.fingerprint,
                member_ids = excluded.member_ids,
                payload = NULL,
                refusal = excluded.refusal,
                computed_at = excluded.computed_at,
                run_seconds = NULL
            """,
            (group_id, stage, current, json.dumps(ids), str(reason)[:500], db._now()),
        )


# ── the sweeper ──────────────────────────────────────────────────────────────


#: How many groupings back the sweeper looks. Two, because two can be live at
#: once -- one event finishing as the next is drawn -- and past that a grouping
#: is history nobody is pressing. Every tick fingerprints every group it can
#: see, so this is what stops the scan growing with the number of Champion Duels
#: the bot has ever recorded.
GROUPINGS_SWEPT = 2


def _all_groups() -> list[dict]:
    """Every group in a round that has a model, newest grouping first.

    Queried here rather than through `db.get_groups`, which filters out rows
    with a NULL label -- and the knockout field is exactly that: one unlettered
    field of 32, the most expensive thing in the tournament and the single row
    this sweeper most needs to see.
    """
    stages = swept_stages()
    if not stages:
        return []
    marks = ",".join("?" for _ in stages)
    with db._get_conn() as conn:
        recent = [
            r["grouping_id"]
            for r in conn.execute(
                "SELECT DISTINCT grouping_id FROM groups ORDER BY grouping_id DESC LIMIT ?",
                (GROUPINGS_SWEPT,),
            ).fetchall()
        ]
        if not recent:
            return []
        gmarks = ",".join("?" for _ in recent)
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT id, grouping_id, stage, label FROM groups "
                f"WHERE stage IN ({marks}) AND grouping_id IN ({gmarks}) "
                "ORDER BY grouping_id DESC, stage, label",
                (*stages, *recent),
            ).fetchall()
        ]


def _last_write_at(group_id: int) -> str | None:
    """When anything the odds read was last written, across every table.

    DERIVED RATHER THAN STAMPED, and that is the point. A dirty flag has to be
    set by every write path that could matter, and the one that forgets is the
    one nobody finds; a MAX over the timestamps those paths already keep cannot
    be forgotten. It is also what makes the debounce honest -- it is the real
    last write, not the last write somebody remembered to announce.
    """
    with db._get_conn() as conn:
        row = conn.execute(
            """
            SELECT MAX(t) AS t FROM (
                SELECT MAX(updated_at) AS t FROM groups        WHERE id = :g
                UNION ALL
                SELECT MAX(updated_at)      FROM group_members WHERE group_id = :g
                UNION ALL
                SELECT MAX(r.updated_at)    FROM registrants r
                    JOIN group_members m ON m.registrant_id = r.id WHERE m.group_id = :g
                UNION ALL
                SELECT MAX(s.updated_at)    FROM squads s
                    JOIN group_members m ON m.registrant_id = s.registrant_id
                    WHERE m.group_id = :g
                UNION ALL
                SELECT MAX(p.updated_at)    FROM registrant_profiles p
                    JOIN group_members m ON m.registrant_id = p.registrant_id
                    WHERE m.group_id = :g
                UNION ALL
                SELECT MAX(COALESCE(o.observed_at, o.created_at)) FROM order_history o
                    JOIN group_members m ON m.registrant_id = o.registrant_id
                    WHERE m.group_id = :g
            )
            """,
            {"g": group_id},
        ).fetchone()
    return row["t"] if row else None


def due(*, now=None) -> list[dict]:
    """Groups whose stored answer no longer matches their data, worth doing now.

    Ordered by when they were last LOOKED AT, most recent first, rather than by
    how long they have been wrong. Most groups in a tournament are never opened
    by anybody, and spending the tick on the group somebody read this morning is
    the difference between a warm surface and a cold one.

    A group still inside its quiet window is skipped rather than dropped -- it
    comes back on a later tick, once whoever is editing it has stopped.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=QUIET_MINUTES)).isoformat()

    with db._get_conn() as conn:
        held = {
            r["group_id"]: dict(r)
            for r in conn.execute(
                "SELECT group_id, fingerprint, member_ids, last_viewed_at FROM odds_runs"
            ).fetchall()
        }

    out = []
    for group in _all_groups():
        members = db.get_group_scouting(group["id"])
        if not members:
            continue
        try:
            current, ids = fingerprint(members, stage=group["stage"])
        except NoModel:
            continue
        row = held.get(group["id"])
        if row and row["fingerprint"] == current and row["member_ids"] == json.dumps(ids):
            continue  # already true, whether it holds numbers or a refusal
        written = _last_write_at(group["id"])
        if written and written > cutoff:
            continue  # somebody is still editing; let them finish
        out.append(
            {
                "group_id": group["id"],
                "stage": group["stage"],
                "label": group["label"],
                "members": members,
                "last_viewed_at": (row or {}).get("last_viewed_at"),
            }
        )

    # Never viewed sorts last: "" is below every ISO timestamp.
    out.sort(key=lambda c: c["last_viewed_at"] or "", reverse=True)
    return out


def compute(members: list[dict], *, stage: str):
    """Run the round's model. Refusals come back as `NotEnoughData`.

    A thin dispatch and deliberately nothing more. Both functions it calls
    already refuse a group they cannot schedule BEFORE they simulate anything,
    so the sweeper learns a group is unmodellable for free rather than by
    re-deriving the rules here and drifting from them.
    """
    if stage == "knockouts":
        return odds.bracket_odds(members)
    return odds.group_advance_odds(members, stage=stage)


def run_one(candidate: dict) -> str:
    """Do one group and write the result down. Returns what happened.

    Blocking, and called from a thread -- the simulation itself is in a
    subprocess (`champion_duel_odds._run_off_process`), so the minute it takes
    is a minute this process spends blocked on a pipe rather than holding the
    GIL against every other guild.

    Returns one of `stored`, `refused`, `deferred` or `failed`.
    """
    import time

    group_id, stage, members = candidate["group_id"], candidate["stage"], candidate["members"]

    # YIELD TO ANYBODY WHO IS ACTUALLY ASKING. `champion_duel_odds._RUN_LOCK` is
    # one lock for the whole process and both models take it, so a sweep that
    # started first makes a member's press wait behind it -- up to ninety seconds
    # for a bracket, which is the opposite of what a precompute is for.
    #
    # Checked rather than acquired, because `compute` takes that same lock a
    # moment later and it is not reentrant. That makes this advisory: a press
    # landing in the microseconds after the check still queues, and a press
    # landing DURING this run queues for the rest of it. Closing that properly
    # means either a second lock so sweeps and presses do not contend, or making
    # a press able to take over a run in flight -- both are decisions for
    # whoever wires the press path, and neither is this loop's to make. Skipping
    # costs nothing: the group is still due and the next tick is a minute away.
    if odds._RUN_LOCK.locked():
        return "deferred"

    t0 = time.perf_counter()
    try:
        result = compute(members, stage=stage)
    except odds.NotEnoughData as exc:
        store_refusal(group_id, members, str(exc), stage=stage)
        return "refused"
    except Exception as exc:  # noqa: BLE001 - one group must not stop the sweep
        # WRITTEN DOWN, not just logged, and this is the difference between one
        # bad group and a dead sweeper. The loop above always takes the head of
        # the queue, so a group that fails deterministically -- a knockout field
        # on an engine with no bracket model raises RuntimeError rather than
        # NotEnoughData, and sorts first -- would be retried every minute
        # forever and the sixteen groups behind it would never be reached.
        # Recording it against the fingerprint keeps it out of the queue for
        # exactly as long as the data that failed is still the data we hold.
        print(f"[CHAMPION_DUEL] odds sweep failed for group {group_id}: {exc}")
        store_refusal(group_id, members, f"{type(exc).__name__}: {exc}", stage=stage)
        return "failed"
    store(group_id, members, result, stage=stage, run_seconds=time.perf_counter() - t0)
    return "stored"

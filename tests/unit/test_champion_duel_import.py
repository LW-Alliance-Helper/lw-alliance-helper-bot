"""Bulk import of registrants, squads and deployment orders.

The two rules worth testing hard are the ones whose failure is *silent*: an
import that quietly replaces a scout's observation with an estimate, and one
that doubles a deployment order's weight every time it runs. Neither produces
anything that looks wrong afterwards — the numbers just drift.
"""

from __future__ import annotations

import pytest

import champion_duel_db as db

ACTOR = {"discord_user_id": "111", "discord_name": "Kevin", "guild_id": "999"}


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    db.import_registrants(
        [
            {"name": "AlphaOne", "group": "M", "rank": 1, "server": "738"},
            {"name": "BetaTwo", "group": "M", "rank": 2, "server": "738"},
        ]
    )
    return None


# The exact registrants shape shipped before identity moved to (name, server).
# Dev's volume rebuilt itself as this while the pre-identity code was deployed,
# and every CREATE TABLE IF NOT EXISTS after that was a no-op against it.
PRE_IDENTITY_SCHEMA = """
CREATE TABLE registrants (
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
);
CREATE TABLE squads (player_key TEXT NOT NULL, slot INTEGER NOT NULL, squad_type TEXT);
CREATE TABLE order_history (id INTEGER PRIMARY KEY, player_key TEXT NOT NULL);
CREATE TABLE edits (id INTEGER PRIMARY KEY, player_key TEXT);
"""


def test_a_pre_identity_database_is_rebuilt(tmp_path, monkeypatch):
    """The failure this reproduces is "table registrants has no column named
    origin", which is what dev hit on every import.

    ALTER TABLE cannot fix it: the primary key changes and three tables change
    what they reference. So the obsolete tables are dropped and recreated, which
    is safe only because no import has ever completed against that shape.
    """
    import sqlite3

    path = str(tmp_path / "champion_duel.sqlite3")
    monkeypatch.setattr(db, "DB_PATH", path)
    old = sqlite3.connect(path)
    old.executescript(PRE_IDENTITY_SCHEMA)
    old.execute(
        "INSERT INTO registrants (player_key, display_name, updated_at) VALUES (?,?,?)",
        ("alphaone", "AlphaOne", "2026-08-12T00:00:00+00:00"),
    )
    old.commit()
    old.close()

    with pytest.raises(sqlite3.OperationalError, match="origin"):
        db.import_registrants([{"name": "AlphaOne", "group": "M", "server": "738"}])

    db.init_db()

    result = db.import_registrants([{"name": "AlphaOne", "group": "M", "server": "738"}])
    assert result["inserted"] == 1
    with db._get_conn() as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(registrants)")}
    assert {"id", "origin", "added_by"} <= columns


def test_init_db_leaves_a_current_database_alone(cd_db):
    """The rebuild is keyed on the obsolete marker, not on any failure — so it
    stops matching the moment real rows exist in the current shape."""
    db.import_squads(_squad_rows("AlphaOne"), actor=ACTOR)
    db.init_db()
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    assert player is not None and len(player["squads"]) == 3


def _squad_rows(name, server="738", source="estimated", powers=(40, 30, 20)):
    return [
        {"name": name, "server": server, "slot": slot, "type": t, "power": p, "source": source}
        for slot, (t, p) in enumerate(zip(("Tank", "Missile", "Aircraft"), powers), start=1)
    ]


def _slot(name, slot, server="738"):
    player = db.get_player(name, server=server, include_scouting=True)
    return next(s for s in player["squads"] if s["slot"] == slot)


# ── Squads: an import never downgrades ────────────────────────────────────────


def test_an_estimate_never_overwrites_an_observation(cd_db):
    """The whole reason `squads.source` exists. Re-running an import after a
    scout corrected something must not undo their work."""
    db.import_squads(
        [
            {
                "name": "AlphaOne",
                "server": "738",
                "slot": 1,
                "type": "Tank",
                "power": 41_000_000,
                "source": "observed",
            }
        ],
        actor=ACTOR,
    )
    result = db.import_squads(
        [
            {
                "name": "AlphaOne",
                "server": "738",
                "slot": 1,
                "type": "Missile",
                "power": 9_000_000,
                "source": "estimated",
            }
        ],
        actor=ACTOR,
    )

    kept = _slot("AlphaOne", 1)
    assert kept["source"] == "observed"
    assert kept["squad_type"] == "Tank"
    assert kept["power"] == 41_000_000
    assert result["kept_observed"] == 1


def test_an_estimate_never_overwrites_a_correction(cd_db):
    """`edited` is a human's deliberate fix and outranks a guess too."""
    rid = db.resolve_registrant("AlphaOne", server="738")["id"]
    db.set_squad(rid, 1, squad_type="Aircraft", power=33_000_000, actor=ACTOR, source="edited")
    db.import_squads(_squad_rows("AlphaOne"), actor=ACTOR)
    assert _slot("AlphaOne", 1)["source"] == "edited"


def test_an_imported_sighting_never_overwrites_a_correction(cd_db):
    """The case that shipped broken: the guard only checked for an incoming
    `estimated`, so an imported `observed` row silently reverted a member's
    correction on the next re-import and the summary reported nothing kept.

    A person read the game and typed what they saw. Undoing that is what the
    edit log and `⏪ Revert an edit` are for, deliberately and attributed, not
    a side effect of loading a file.
    """
    rid = db.resolve_registrant("AlphaOne", server="738")["id"]
    db.set_squad(rid, 2, squad_type="Tank", power=81_900_000, actor=ACTOR, source="edited")

    result = db.import_squads(_squad_rows("AlphaOne", source="observed"), actor=ACTOR)

    kept = _slot("AlphaOne", 2)
    assert kept["source"] == "edited"
    assert kept["squad_type"] == "Tank"
    assert kept["power"] == 81_900_000
    assert result["kept_observed"] == 1, "and the import says so rather than staying silent"


def test_an_observation_does_overwrite_an_estimate(cd_db):
    """The protection runs one way only — new sightings are the point."""
    db.import_squads(_squad_rows("AlphaOne", source="estimated"), actor=ACTOR)
    db.import_squads(
        [
            {
                "name": "AlphaOne",
                "server": "738",
                "slot": 1,
                "type": "Tank",
                "power": 41_000_000,
                "source": "observed",
            }
        ],
        actor=ACTOR,
    )
    assert _slot("AlphaOne", 1)["source"] == "observed"


def test_estimates_fill_an_empty_roster(cd_db):
    """97% of registrants have never been seen, and a player with a missing
    slot cannot be predicted at all — so this is what makes the hub non-empty
    on day one, not a nicety."""
    result = db.import_squads(_squad_rows("AlphaOne") + _squad_rows("BetaTwo"), actor=ACTOR)
    assert result["applied"] == 6
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    assert len(player["squads"]) == 3


# ── Orders: re-import must not double the weights ─────────────────────────────


def test_reimporting_does_not_double_the_weights(cd_db):
    """Repeats are the weight: a player seen 5:1 samples 5:1. Appending on
    every run would skew every prediction downstream, silently, because
    nothing about the resulting numbers looks wrong."""
    rows = [
        {"name": "AlphaOne", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]},
        {"name": "AlphaOne", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]},
        {"name": "AlphaOne", "server": "738", "slots": ["Aircraft", "Tank", "Missile"]},
    ]
    db.import_orders(rows, actor=ACTOR)
    first = db.most_common_order(db.resolve_registrant("AlphaOne", server="738")["id"])

    db.import_orders(rows, actor=ACTOR)
    second = db.most_common_order(db.resolve_registrant("AlphaOne", server="738")["id"])

    assert first == second
    assert second["total"] == 3
    assert second["seen"] == 2


def test_a_reimport_leaves_hub_entered_sightings_alone(cd_db):
    """Someone else's contribution is not ours to discard."""
    rid = db.resolve_registrant("AlphaOne", server="738")["id"]
    db.add_order(rid, ["Missile", "Aircraft", "Tank"], actor=ACTOR)  # source='observed'
    db.import_orders(
        [{"name": "AlphaOne", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]}],
        actor=ACTOR,
    )
    db.import_orders(
        [{"name": "AlphaOne", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]}],
        actor=ACTOR,
    )

    top = db.most_common_order(rid)
    assert top["total"] == 2, "one imported plus the hand-entered one"
    orders = db.get_player("AlphaOne", server="738", include_scouting=True)["orders"]
    assert {o["source"] for o in orders} == {"imported", "observed"}


def test_a_reimport_only_touches_players_in_the_payload(cd_db):
    """A partial import must not wipe everyone else's history."""
    db.import_orders(
        [
            {"name": "AlphaOne", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]},
            {"name": "BetaTwo", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]},
        ],
        actor=ACTOR,
    )
    db.import_orders(
        [{"name": "AlphaOne", "server": "738", "slots": ["Aircraft", "Tank", "Missile"]}],
        actor=ACTOR,
    )
    assert db.most_common_order(db.resolve_registrant("BetaTwo", server="738")["id"])["total"] == 1


# ── Bad rows are reported, not fatal ──────────────────────────────────────────


def test_one_bad_row_does_not_abandon_the_rest(cd_db):
    """Silently importing 399 of 400 is worse than either extreme, so the
    caller gets the list of what didn't land."""
    result = db.import_squads(
        _squad_rows("AlphaOne")
        + [{"name": "WhoDis", "server": "738", "slot": 1, "type": "Tank", "power": 1}],
        actor=ACTOR,
    )
    assert result["applied"] == 3
    assert result["skipped"] == 1
    assert any("WhoDis" in p for p in result["problems"])


def test_an_ambiguous_name_is_refused_with_the_servers(cd_db):
    """Attaching a sighting to the wrong player is unrecoverable, so a bulk
    load refuses the row and names the choice rather than picking."""
    db.import_registrants([{"name": "AlphaOne", "group": "N", "rank": 9, "server": "1042"}])
    result = db.import_squads(
        [{"name": "AlphaOne", "slot": 1, "type": "Tank", "power": 1_000, "source": "observed"}],
        actor=ACTOR,
    )
    assert result["applied"] == 0
    assert "738" in result["problems"][0] and "1042" in result["problems"][0]


@pytest.mark.parametrize(
    "bad",
    [
        {"slots": ["Tank", "Tank", "Aircraft"]},
        {"slots": ["Tank", "Missile"]},
        {"slots": ["Tank", "Missile", "Submarine"]},
    ],
)
def test_an_order_that_is_not_a_permutation_is_refused(cd_db, bad):
    """Every lineup runs one of each type, so a repeat is a typo, not data."""
    result = db.import_orders([{"name": "AlphaOne", "server": "738", **bad}], actor=ACTOR)
    assert result["applied"] == 0
    assert result["skipped"] == 1


# ── Imports stay out of the audit trail ───────────────────────────────────────


def test_an_import_writes_no_edit_rows(cd_db):
    """Imports are the baseline, not an edit — the position `import_registrants`
    already took. Hundreds of rows per run would bury the corrections a human
    actually made, which is the one thing that log is for."""
    before = db.list_edits()["total"]
    db.import_squads(_squad_rows("AlphaOne"), actor=ACTOR)
    db.import_orders(
        [{"name": "AlphaOne", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]}],
        actor=ACTOR,
    )
    assert db.list_edits()["total"] == before

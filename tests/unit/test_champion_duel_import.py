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
        ],
        stage="qualifiers",
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

    What the import trips over first has moved -- a payload now resolves its
    grouping before it writes anything, so the missing `groupings` table is
    reached before the missing `origin` column. Either is the same fact: this
    file predates the code and nothing may be written into it until `init_db`
    has run. The assertion is that it refuses, not which table it names.
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

    with pytest.raises(sqlite3.OperationalError, match="origin|groupings"):
        db.import_registrants(
            [{"name": "AlphaOne", "group": "M", "server": "738"}], stage="qualifiers"
        )

    db.init_db()

    result = db.import_registrants(
        [{"name": "AlphaOne", "group": "M", "server": "738"}], stage="qualifiers"
    )
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
    db.import_registrants(
        [{"name": "AlphaOne", "group": "N", "rank": 9, "server": "1042"}], stage="qualifiers"
    )
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
    db.import_profiles([_profile_row("AlphaOne")], actor=ACTOR)
    assert db.list_edits()["total"] == before


# ── The import log ────────────────────────────────────────────────────────────
#
# The open question was whether an import log is new storage or a change to the
# rule above. It is new storage, and the test above still stands.
#
# `edits` is a per-value trail with a revert attached, and its job is finding
# the handful of corrections a human made. An import is one file, hundreds of
# rows, one actor, one moment, and nothing in it is revertable row by row.


def test_an_import_is_logged_once_with_what_landed(cd_db):
    """One row per import, never one per value. The question it is there to
    answer is who has loaded what, which counts answer and values do not."""
    entry = db.record_import(
        door="discord",
        results={
            "registrants": {"total": 2, "inserted": 2},
            "squads": {"applied": 6, "skipped": 1, "problems": []},
            "profiles": {"applied": 1, "cleared": 2, "skipped": 0, "problems": []},
        },
        grouping_id=db.default_grouping_id(),
        stage="qualifiers",
        actor=ACTOR,
    )

    logged = db.list_imports()
    assert logged["total"] == 1
    row = logged["imports"][0]
    assert row["id"] == entry
    assert (row["registrants"], row["squads"], row["orders"], row["profiles"]) == (2, 6, 0, 1)
    assert row["cleared"] == 2 and row["skipped"] == 1
    assert row["door"] == "discord" and row["stage"] == "qualifiers"
    assert row["actor_discord_id"] == ACTOR["discord_user_id"]


def test_an_import_that_landed_nothing_is_still_logged(cd_db):
    """Exactly the run somebody comes asking about. A log that only records
    successes cannot answer them."""
    db.record_import(
        door="api",
        results={"squads": {"applied": 0, "skipped": 400, "problems": []}},
        actor=ACTOR,
    )

    row = db.list_imports()["imports"][0]
    assert row["squads"] == 0 and row["skipped"] == 400


def test_the_log_is_readable_per_grouping(cd_db):
    """About 50 alliances use this bot and one grouping covers roughly two of
    them, so "who has loaded what" is a question about one Champion Duel."""
    mine = db.default_grouping_id()
    db.record_import(door="discord", results={}, grouping_id=mine, actor=ACTOR)
    db.record_import(door="discord", results={}, grouping_id=mine + 999, actor=ACTOR)

    assert db.list_imports(grouping_id=mine)["total"] == 1
    assert db.list_imports()["total"] == 2


# ── Player profiles ───────────────────────────────────────────────────────────
#
# The block `push_to_bot.py` has been emitting since the 1.5 contract landed and
# nothing here read, so every row of it hit the floor. What it carries cannot be
# derived from what the bot stores: `player_profiles` fits against the whole
# sightings corpus, and the bot only ever has whatever somebody typed in.


def _profile_row(name, server="738", **profile):
    base = {"types": ["Aircraft", "Tank", "Missile"], "shape": [0.94, 0.87], "mixed": [0, 1]}
    return {"name": name, "server": server, "profile": {**base, **profile}}


def test_a_profile_round_trips_in_the_shape_the_engine_reads(cd_db):
    """Positions are POWER RANKS on the way in and on the way out. The frame is
    the whole contract -- `champion_duel_odds._profile` is what translates."""
    result = db.import_profiles([_profile_row("AlphaOne", gorilla=0)], actor=ACTOR)

    assert result == {"applied": 1, "cleared": 0, "skipped": 0, "problems": []}
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    assert player["profile"] == {
        "types": ["Aircraft", "Tank", "Missile"],
        "shape": [0.94, 0.87],
        "mixed": [0, 1],
        "gorilla": 0,
    }


def test_a_player_with_nothing_measured_has_no_profile_at_all(cd_db):
    """Absent is what makes the engine draw from the population. A row of nulls
    would say something quite different, so it is never written."""
    assert db.get_player("BetaTwo", server="738", include_scouting=True)["profile"] is None
    assert db.get_profiles([]) == {}


def test_a_measured_none_survives_as_a_measurement(cd_db):
    """ "We looked and every squad is pure" is not "nobody looked". Collapsing
    the two puts a 3.3% penalty on two squads of every unscouted player."""
    db.import_profiles(
        [{"name": "AlphaOne", "server": "738", "profile": {"mixed": []}}], actor=ACTOR
    )

    assert db.get_player("AlphaOne", server="738", include_scouting=True)["profile"] == {
        "mixed": []
    }


def test_a_legacy_count_of_zero_becomes_an_empty_mixed(cd_db):
    """`n_mixed: 0` and `mixed: []` say the same thing -- every squad is pure --
    but only one of them names no positions. Normalising at the door leaves the
    legacy column meaning "n of them and we cannot say which", which is the
    case the read side has to be careful with."""
    db.import_profiles(
        [{"name": "AlphaOne", "server": "738", "profile": {"n_mixed": 0}}], actor=ACTOR
    )

    profile = db.get_player("AlphaOne", server="738", include_scouting=True)["profile"]
    assert profile == {"mixed": []}


def test_positions_win_over_a_legacy_count(cd_db):
    """The engine prefers `mixed` and reads a bare count as "the bottom n",
    which the corpus says is usually wrong. Storing both would leave the row
    ambiguous about which a later reader should believe."""
    db.import_profiles(
        [{"name": "AlphaOne", "server": "738", "profile": {"mixed": [0], "n_mixed": 2}}],
        actor=ACTOR,
    )

    assert db.get_player("AlphaOne", server="738", include_scouting=True)["profile"] == {
        "mixed": [0]
    }


def test_a_reimport_replaces_the_profile_whole(cd_db):
    """Every import is a re-fit of the entire corpus, so a measurement that has
    dropped out of the fit has to drop out of the row. Merging key by key would
    keep a retracted measurement alive with nothing able to clear it."""
    db.import_profiles([_profile_row("AlphaOne", gorilla=2)], actor=ACTOR)
    db.import_profiles(
        [{"name": "AlphaOne", "server": "738", "profile": {"types": ["Tank", "Tank", "Tank"]}}],
        actor=ACTOR,
    )

    assert db.get_player("AlphaOne", server="738", include_scouting=True)["profile"] == {
        "types": ["Tank", "Tank", "Tank"]
    }


@pytest.mark.parametrize(
    "profile",
    [
        {"types": ["Tank", "Missile"]},
        {"types": ["Tank", "Missile", "Submarine"]},
        {"shape": [0.9]},
        {"shape": [1.4, 0.8]},
        {"shape": [0.0, 0.8]},
        {"shape": ["big", 0.8]},
        {"shape": [0.7, 0.9]},
        {"mixed": [0, 5]},
        {"mixed": ["first"]},
        {"mixed": 2},
        {"gorilla": 7},
        {"gorilla": None, "n_mixed": -1},
        {"n_mixed": "two"},
    ],
)
def test_a_profile_the_engine_would_choke_on_is_refused_at_the_door(cd_db, profile):
    """The engine reads a profile deep inside a trial, long after the
    interaction has been deferred, and `build_odds_embed` catches only
    `NotEnoughData` -- so a bad value there leaves a member watching a spinner
    that never resolves. Refusing the row costs that player their profile and
    nothing else."""
    result = db.import_profiles(
        [{"name": "AlphaOne", "server": "738", "profile": profile}], actor=ACTOR
    )

    assert result["applied"] == 0 and result["skipped"] == 1
    assert "AlphaOne" in result["problems"][0]
    assert db.get_player("AlphaOne", server="738", include_scouting=True)["profile"] is None


def test_a_profile_that_measures_nothing_retracts_the_one_we_hold(cd_db):
    """Replacing whole only reaches a player the payload names, so this is the
    producer's one way of saying "we can no longer justify what you hold".
    Without it a retracted measurement sits there feeding every future run, and
    nothing in the system can ever clear it."""
    db.import_profiles([_profile_row("AlphaOne", gorilla=1)], actor=ACTOR)

    result = db.import_profiles([{"name": "AlphaOne", "server": "738", "profile": {}}], actor=ACTOR)

    # Cleared, not applied: wiping 400 profiles is a different event from
    # loading 400 and the operator's summary must not read the same.
    assert result["applied"] == 0 and result["cleared"] == 1 and result["skipped"] == 0
    assert db.get_player("AlphaOne", server="738", include_scouting=True)["profile"] is None


def test_retracting_a_profile_nobody_holds_is_not_an_error(cd_db):
    """Every import is the whole corpus, so most rows in a retracting payload
    have nothing to retract. Counting those as failures would bury the ones
    that did clear something."""
    result = db.import_profiles([{"name": "AlphaOne", "server": "738", "profile": {}}], actor=ACTOR)

    assert result == {"applied": 0, "cleared": 0, "skipped": 0, "problems": []}


def test_a_profile_of_only_unreadable_keys_does_not_retract(cd_db):
    """The two rules either side of this would otherwise combine badly: unknown
    keys are ignored, and an empty profile retracts. A producer-side key rename
    would then read as "we measured nothing about anybody" and delete the lot.

    A payload that measured something, in words this version cannot read, is
    refused and says so."""
    db.import_profiles([_profile_row("AlphaOne", gorilla=1)], actor=ACTOR)

    result = db.import_profiles(
        [{"name": "AlphaOne", "server": "738", "profile": {"nerve": 0.4}}], actor=ACTOR
    )

    assert result["cleared"] == 0 and result["skipped"] == 1
    assert "nerve" in result["problems"][0]
    assert db.get_player("AlphaOne", server="738", include_scouting=True)["profile"] is not None


def test_a_bool_is_not_a_position(cd_db):
    """`int(True)` is 1, so an unguarded flag where a position belongs would
    land the purity penalty on the second-biggest squad."""
    result = db.import_profiles(
        [{"name": "AlphaOne", "server": "738", "profile": {"gorilla": True}}], actor=ACTOR
    )

    assert result["skipped"] == 1


def test_a_key_this_version_has_no_column_for_is_ignored_not_fatal(cd_db):
    """A newer simulator may fit something we cannot store yet. Dropping the
    row over it would throw away the measurements we do understand."""
    result = db.import_profiles(
        [{"name": "AlphaOne", "server": "738", "profile": {"mixed": [1], "nerve": 0.4}}],
        actor=ACTOR,
    )

    assert result["applied"] == 1
    assert db.get_player("AlphaOne", server="738", include_scouting=True)["profile"] == {
        "mixed": [1]
    }


def test_one_bad_profile_does_not_abandon_the_rest(cd_db):
    result = db.import_profiles(
        [_profile_row("AlphaOne"), _profile_row("WhoDis"), _profile_row("BetaTwo")], actor=ACTOR
    )

    assert result["applied"] == 2 and result["skipped"] == 1
    assert any("WhoDis" in p for p in result["problems"])


def test_the_odds_query_carries_the_troop_level(cd_db):
    """Collected, stored, read by the odds, and never selected: every player
    reached the engine at the default level, so the dropdown that gathers this
    could not have changed a number once. The odds tests build their member
    dicts by hand, which is why nothing that passes them caught it."""
    # Through `upsert_registrant`, which is the only thing that writes it: a
    # troop level is read off a member's own screen, not off a roster file.
    db.upsert_registrant("AlphaOne", server="738", troop_level=9, actor=ACTOR)
    group = db.get_or_create_group(db.default_grouping_id(), "qualifiers", "M")

    rows = db.get_group_scouting(group["id"])

    assert {r["display_name"]: r["troop_level"] for r in rows}["AlphaOne"] == 9


def test_the_odds_query_carries_the_profile(cd_db):
    """`get_group_scouting` is what the odds read. A query that returns
    everything except the one field the model wants is a feature that cannot
    work in production and still passes its tests -- which is exactly what
    happened to `thp` on this query once already."""
    db.import_profiles([_profile_row("AlphaOne")], actor=ACTOR)
    group = db.get_or_create_group(db.default_grouping_id(), "qualifiers", "M")

    rows = db.get_group_scouting(group["id"])

    by_name = {r["display_name"]: r for r in rows}
    assert by_name["AlphaOne"]["profile"]["types"] == ["Aircraft", "Tank", "Missile"]
    assert by_name["BetaTwo"]["profile"] is None

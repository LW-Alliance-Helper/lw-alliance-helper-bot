"""Turning stored scouting into engine input.

The join between "squads by slot, sightings as type permutations" and "the
engine wants (power, type) tuples" is where a wrong number would be produced
confidently, so it gets its own tests rather than riding on the hub's.
"""

from __future__ import annotations

import pytest

import champion_duel_db as db
import champion_duel_predict as cdp

KEV = {"discord_user_id": "111", "discord_name": "Kevin", "guild_id": "999"}


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    db.import_registrants([{"name": "AlphaOne", "group": "M", "rank": 1, "server": "738"}])
    return db.resolve_registrant("AlphaOne", server="738")["id"]


def _squads(rid, source="observed"):
    for slot, (squad_type, power) in enumerate(
        zip(("Tank", "Missile", "Aircraft"), (40_000_000, 30_000_000, 20_000_000)), start=1
    ):
        db.set_squad(rid, slot, squad_type=squad_type, power=power, actor=KEV, source=source)


def test_missing_slot_refuses_rather_than_guessing(cd_db):
    """The engine handed a None returns a confident-looking number for a
    matchup nobody can field."""
    db.set_squad(cd_db, 1, squad_type="Tank", power=1_000, actor=KEV)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    with pytest.raises(cdp.NotEnoughData) as exc:
        cdp.build_side(player)
    assert exc.value.missing == [2, 3]


def test_a_power_of_none_is_missing_too(cd_db):
    """A type with no power is not a usable squad, even though the row exists."""
    for slot, squad_type in ((1, "Tank"), (2, "Missile"), (3, "Aircraft")):
        db.set_squad(cd_db, slot, squad_type=squad_type, actor=KEV)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    with pytest.raises(cdp.NotEnoughData):
        cdp.build_side(player)


def test_sightings_become_line_ups_by_looking_the_power_back_up(cd_db):
    _squads(cd_db)
    db.add_order(cd_db, ["Missile", "Aircraft", "Tank"], actor=KEV)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)

    side = cdp.build_side(player)
    assert side.orders == [
        [(30_000_000, "Missile"), (20_000_000, "Aircraft"), (40_000_000, "Tank")]
    ]


def test_repeated_sightings_are_kept(cd_db):
    """A player seen five times in one order and once in another should weigh
    5:1 — that ratio is the prediction's read on what they'll have set."""
    _squads(cd_db)
    for _ in range(3):
        db.add_order(cd_db, ["Tank", "Missile", "Aircraft"], actor=KEV)
    db.add_order(cd_db, ["Aircraft", "Missile", "Tank"], actor=KEV)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)

    side = cdp.build_side(player)
    assert len(side.orders) == 4
    assert side.sightings == 4


def test_a_sighting_naming_a_squad_they_no_longer_field_is_dropped(cd_db):
    """Stale or mistyped, both are better represented by one fewer order than
    by an invented power."""
    _squads(cd_db)
    db.add_order(cd_db, ["Tank", "Missile", "Aircraft"], actor=KEV)
    # They swap the Aircraft out for a second Tank-type squad after the sighting.
    db.set_squad(cd_db, 3, squad_type="Missile", power=15_000_000, actor=KEV)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)

    side = cdp.build_side(player)
    assert side.orders == []


def test_natural_order_is_the_slot_order(cd_db):
    _squads(cd_db)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    side = cdp.build_side(player)
    assert side.player["sq1_type"] == "Tank"
    assert side.player["sq3_power"] == 20_000_000


def test_confidence_separates_a_scouted_number_from_a_guessed_one(cd_db):
    """A 61% from six estimates is not the same claim as a 61% from six
    observations, and a surface that renders them identically is lying."""
    _squads(cd_db, source="estimated")
    db.import_registrants([{"name": "BetaTwo", "group": "M", "rank": 2, "server": "738"}])
    other = db.resolve_registrant("BetaTwo", server="738")["id"]
    _squads(other, source="estimated")

    a = db.get_player("AlphaOne", server="738", include_scouting=True)
    b = db.get_player("BetaTwo", server="738", include_scouting=True)
    guessed = cdp.predict(a, b)
    assert guessed.confidence() == "low"

    _squads(cd_db, source="observed")
    _squads(other, source="observed")
    db.add_order(cd_db, ["Tank", "Missile", "Aircraft"], actor=KEV)
    db.add_order(other, ["Aircraft", "Missile", "Tank"], actor=KEV)
    scouted = cdp.predict(
        db.get_player("AlphaOne", server="738", include_scouting=True),
        db.get_player("BetaTwo", server="738", include_scouting=True),
    )
    assert scouted.confidence() == "high"


def test_prediction_is_symmetric(cd_db):
    """P(A beats B) and P(B beats A) have to sum to 1, or one of the two
    renderings of the same match is wrong."""
    _squads(cd_db)
    db.import_registrants([{"name": "BetaTwo", "group": "M", "rank": 2, "server": "738"}])
    other = db.resolve_registrant("BetaTwo", server="738")["id"]
    for slot, (squad_type, power) in enumerate(
        zip(("Missile", "Aircraft", "Tank"), (35_000_000, 25_000_000, 15_000_000)), start=1
    ):
        db.set_squad(other, slot, squad_type=squad_type, power=power, actor=KEV)

    a = db.get_player("AlphaOne", server="738", include_scouting=True)
    b = db.get_player("BetaTwo", server="738", include_scouting=True)
    forward = cdp.predict(a, b)
    backward = cdp.predict(b, a)
    assert forward.p_a == pytest.approx(backward.p_b, abs=1e-9)
    assert forward.p_a + forward.p_b == pytest.approx(1.0)


def test_the_engine_constants_ride_along(cd_db):
    """A caller comparing two predictions has to be able to tell a model change
    from a data change."""
    _squads(cd_db)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    result = cdp.predict(player, player)
    assert "K" in result.engine
    # A player against themselves is a coin flip, which is a cheap sanity check
    # that nothing in the shaping favours side A.
    assert result.p_a == pytest.approx(0.5, abs=1e-9)

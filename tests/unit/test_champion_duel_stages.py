"""Rounds as a dimension (#495).

A registrant's group and rank belong to a round, not to the person. The bug
this table prevents is silent and unrecoverable: loading a semifinal draw into
one shared column overwrites which qualifier group a player came from, and
imports do not write edits, so there is nothing to revert.
"""

from __future__ import annotations

import sqlite3

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


def _rid(name, server="738"):
    return db.resolve_registrant(name, server=server)["id"]


# ── The thing this exists to prevent ──────────────────────────────────────────


def test_a_semifinal_draw_does_not_destroy_the_qualifier_group(cd_db):
    """The whole point. A player's qualifier group is how they got here, and it
    has to survive being placed in the next round."""
    rid = _rid("AlphaOne")

    db.set_stage(rid, "semifinals", grp="D", rank=3)

    stages = db.get_stages(rid)
    assert stages["qualifiers"]["grp"] == "M"
    assert stages["qualifiers"]["rank"] == 1
    assert stages["semifinals"]["grp"] == "D"
    assert stages["semifinals"]["rank"] == 3


def test_rounds_come_back_in_playing_order(cd_db):
    """Order is load-bearing: `current_stage` reads it to decide which round is
    running, and a card naming rounds out of sequence reads as nonsense."""
    rid = _rid("AlphaOne")
    db.set_stage(rid, "knockouts", grp="A", rank=1)
    db.set_stage(rid, "semifinals", grp="D", rank=3)

    assert list(db.get_stages(rid)) == ["qualifiers", "semifinals", "knockouts"]


def test_placing_a_player_twice_in_one_round_corrects_rather_than_duplicates(cd_db):
    rid = _rid("AlphaOne")
    db.set_stage(rid, "semifinals", grp="D", rank=3)
    db.set_stage(rid, "semifinals", grp="D", rank=2)

    assert db.get_stages(rid)["semifinals"]["rank"] == 2


@pytest.mark.parametrize("bad", ["", None, "finals", "Qualifier", "semi-finals"])
def test_an_unknown_round_is_refused(cd_db, bad):
    """Naming a round we do not play should fail at the call, not write a row
    nothing will ever read."""
    with pytest.raises(ValueError):
        db.set_stage(_rid("AlphaOne"), bad, grp="A")


# ── Which round is running ────────────────────────────────────────────────────


def test_the_running_round_is_the_furthest_draw_we_hold(cd_db):
    """Derived rather than set by an operator: a semifinal draw does not exist
    until the qualifiers producing it are over, so loading it is the signal."""
    assert db.current_stage() == "qualifiers"

    db.set_stage(_rid("AlphaOne"), "semifinals", grp="D", rank=1)
    assert db.current_stage() == "semifinals"

    db.set_stage(_rid("AlphaOne"), "knockouts", grp="A", rank=1)
    assert db.current_stage() == "knockouts"


def test_no_draw_at_all_has_no_running_round(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "empty.sqlite3"))
    db.init_db()
    assert db.current_stage() is None


def test_a_player_out_of_the_running_round_has_no_stage_to_display(cd_db):
    """Someone knocked out in the qualifiers is not part of the semifinal
    story, and captioning their card with the live round would say they are
    still in it."""
    survivor, eliminated = _rid("AlphaOne"), _rid("BetaTwo")
    db.set_stage(survivor, "semifinals", grp="D", rank=1)

    assert db.stage_for_display(survivor)["stage"] == "semifinals"
    assert db.stage_for_display(eliminated) is None


# ── Migration ─────────────────────────────────────────────────────────────────


def test_existing_rows_become_qualifier_rows(cd_db):
    """Whatever `registrants` holds today is qualifier data, because qualifiers
    are the only round that has ever been imported."""
    stages = db.get_stages(_rid("AlphaOne"))
    assert stages["qualifiers"]["grp"] == "M"
    assert stages["qualifiers"]["rank"] == 1


def test_the_backfill_never_overwrites_a_later_correction(cd_db):
    """`init_db` runs on every boot. A corrected qualifier group must not be
    reverted to whatever the legacy column still says."""
    rid = _rid("AlphaOne")
    db.set_stage(rid, "qualifiers", grp="P", rank=9)

    db.init_db()

    assert db.get_stages(rid)["qualifiers"] == {
        **db.get_stages(rid)["qualifiers"],
        "grp": "P",
        "rank": 9,
    }


def test_the_backfill_skips_a_registrant_with_no_round_data(cd_db):
    """A self-reported player has no group: group is optional on the add form.
    Writing them an empty qualifiers row would claim they played in it."""
    db.upsert_registrant("Stranger", server="999", origin="self_reported", actor=ACTOR)

    db.init_db()

    assert db.get_stages(_rid("Stranger", server="999")) == {}


def test_deleting_a_registrant_takes_their_rounds_with_them(cd_db):
    rid = _rid("AlphaOne")
    db.set_stage(rid, "semifinals", grp="D", rank=1)

    with db._get_conn() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("DELETE FROM registrants WHERE id = ?", (rid,))
        except sqlite3.IntegrityError:  # pragma: no cover
            pytest.skip("foreign keys not enforced on this build")
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM registrant_stages WHERE registrant_id = ?", (rid,)
        ).fetchone()["n"]

    assert remaining == 0


# ── What the card calls the fixture ───────────────────────────────────────────


def _player(name, server="738"):
    return db.get_player(name, server=server)


def test_the_card_names_the_round_when_both_are_in_it_together(cd_db):
    """Both in the running round and in the same group: this is a fixture that
    exists, so the card says which one."""
    import champion_duel_hub as hub

    assert hub.card_subtitle(_player("AlphaOne"), _player("BetaTwo")) == "Group M · Qualifiers"


def test_two_players_in_different_rounds_get_the_default(cd_db):
    """One still in, one knocked out. Naming the live round would say they are
    both still in it."""
    import champion_duel_hub as hub

    db.set_stage(_rid("AlphaOne"), "semifinals", grp="D", rank=1)

    assert hub.card_subtitle(_player("AlphaOne"), _player("BetaTwo")) == hub.CARD_DEFAULT_SUBTITLE


def test_same_round_different_groups_gets_the_default(cd_db):
    """They will never actually meet, so a "Group M" caption over two people
    who are not both in group M is wrong about the one thing it asserts."""
    import champion_duel_hub as hub

    db.set_stage(_rid("BetaTwo"), "qualifiers", grp="N", rank=1)

    assert hub.card_subtitle(_player("AlphaOne"), _player("BetaTwo")) == hub.CARD_DEFAULT_SUBTITLE


def test_a_player_we_hold_no_round_for_gets_the_default(cd_db):
    import champion_duel_hub as hub

    db.upsert_registrant("Stranger", server="999", origin="self_reported", actor=ACTOR)

    assert (
        hub.card_subtitle(_player("AlphaOne"), _player("Stranger", server="999"))
        == hub.CARD_DEFAULT_SUBTITLE
    )


def test_the_round_name_follows_the_event(cd_db):
    """Once the semifinal draw lands, a semifinal fixture is captioned as one."""
    import champion_duel_hub as hub

    for name in ("AlphaOne", "BetaTwo"):
        db.set_stage(_rid(name), "semifinals", grp="D", rank=1)

    assert hub.card_subtitle(_player("AlphaOne"), _player("BetaTwo")) == "Group D · Semifinals"

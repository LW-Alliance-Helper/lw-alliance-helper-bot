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


def started_so_today_is(phase: str) -> str:
    """A start date that puts today in `phase`.

    The round a grouping is playing comes from its calendar now, so a fixture
    has to give it one. Computed backwards from today rather than hardcoded, or
    every test in this module would start failing on a date nobody chose.
    """
    from datetime import timedelta

    first_day = {key: first for key, first, _ in db.PHASES}[phase]
    return (db._server_today() - timedelta(days=first_day)).isoformat()


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
        started_on=started_so_today_is("qualifiers"),
    )
    return None


def _restart(phase: str) -> None:
    """Move the grouping's start date so today lands in `phase`."""
    with db._get_conn() as conn:
        conn.execute("UPDATE groupings SET started_on = ?", (started_so_today_is(phase),))


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


def test_the_running_round_comes_from_the_calendar(cd_db):
    """Still derived rather than set by an operator, but from the grouping's own
    dates rather than from what we happen to hold.

    The old rule was "the furthest round any draw exists for", which cannot
    answer anything for a grouping with nothing loaded -- and that is every
    grouping except the one that was imported."""
    assert db.current_stage() == "qualifiers"

    _restart("semifinals")
    assert db.current_stage() == "semifinals"

    _restart("knockouts")
    assert db.current_stage() == "knockouts"


def test_a_detail_window_reports_the_round_just_played(cd_db):
    """A Detail window is not a round, but it is when the round before it is
    still what everyone is talking about and the next draw becomes visible."""
    _restart("qualifier_detail")
    assert db.current_phase() == "qualifier_detail"
    assert db.current_stage() == "qualifiers"


def test_the_round_is_known_before_any_draw_is_loaded(tmp_path, monkeypatch):
    """The state every grouping but one is in, and the reason the derivation
    moved to the calendar: an alliance that has entered nothing but their
    sixteen warzones can still be told the semifinals start on Monday."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "fresh.sqlite3"))
    db.init_db()
    grouping = db.create_grouping(["1500", "1501"], started_so_today_is("semifinals"))

    assert db.get_roster(grouping_id=grouping["id"]) == []
    assert db.current_stage(grouping["id"]) == "semifinals"


def test_sign_up_is_not_a_round(cd_db):
    """Nobody has played anything yet, so naming a round would be wrong."""
    _restart("signup")
    assert db.current_phase() == "signup"
    assert db.current_stage() is None


def test_a_grouping_with_no_dates_answers_nothing_rather_than_guessing(tmp_path, monkeypatch):
    """An import can establish a grouping exists before anyone reads its
    timeline off the Match Overview."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "undated.sqlite3"))
    db.init_db()
    grouping = db.create_grouping(["2000"], None)

    assert db.current_phase(grouping["id"]) is None
    assert db.current_stage(grouping["id"]) is None
    assert db.is_finished(grouping["id"]) is False


def test_no_grouping_at_all_has_no_running_round(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "empty.sqlite3"))
    db.init_db()
    assert db.current_stage() is None


def test_a_player_out_of_the_running_round_has_no_stage_to_display(cd_db):
    """Someone knocked out in the qualifiers is not part of the semifinal
    story, and captioning their card with the live round would say they are
    still in it."""
    survivor, eliminated = _rid("AlphaOne"), _rid("BetaTwo")
    db.set_stage(survivor, "semifinals", grp="D", rank=1)
    # Placing someone in a round is no longer what makes that round current.
    # The calendar is, so the clock has to move too.
    _restart("semifinals")

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
    _restart("semifinals")

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
    """Once the semifinals are running, a semifinal fixture is captioned as
    one."""
    import champion_duel_hub as hub

    for name in ("AlphaOne", "BetaTwo"):
        db.set_stage(_rid(name), "semifinals", grp="D", rank=1)
    _restart("semifinals")

    assert hub.card_subtitle(_player("AlphaOne"), _player("BetaTwo")) == "Group D · Semifinals"


# ── Importing without a round ─────────────────────────────────────────────────


def test_a_roster_with_no_round_adds_players_and_claims_nothing(tmp_path, monkeypatch):
    """Deliberately not a default of qualifiers. Guess qualifiers on a
    semifinal draw and it overwrites the qualifier groups, which is the failure
    rounds exist to prevent. No round is recoverable; the wrong round is not."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()

    result = db.import_registrants([{"name": "AlphaOne", "group": "M", "server": "738"}])

    assert result["total"] == 1
    assert result["stage"] is None
    player = db.get_player("AlphaOne", server="738")
    assert player is not None, "the player is added either way"
    assert player["stages"] == {}
    assert player["grp"] is None, "and we do not claim a group we cannot place"
    assert db.current_stage() is None


def test_a_roster_row_with_no_group_is_not_placed_in_the_round(cd_db):
    """A semifinal payload carries the whole roster so scouting still resolves
    against every player, but only the advancers have a semifinal group.
    Writing the rest an empty semifinal row would say they all qualified."""
    result = db.import_registrants(
        [
            {"name": "AlphaOne", "group": "D", "server": "738"},
            {"name": "BetaTwo", "server": "738"},
        ],
        stage="semifinals",
    )

    assert result["placed"] == 1, "only the one carrying a group"
    assert db.get_stages(_rid("AlphaOne"))["semifinals"]["grp"] == "D"
    assert "semifinals" not in db.get_stages(_rid("BetaTwo"))
    # And the one left out keeps the qualifier round they did play.
    assert db.get_stages(_rid("BetaTwo"))["qualifiers"]["grp"] == "M"


# ── Showing the pathway ───────────────────────────────────────────────────────


def test_the_card_shows_every_round_a_player_has_reached(cd_db):
    """ "Where are they in the duel pathway" is the question. One field
    hardcoded to Qualifiers stopped being true the day a draw landed."""
    import champion_duel_hub as hub

    db.set_stage(_rid("AlphaOne"), "semifinals", grp="D")

    embed = hub.build_player_embed(db.get_player("AlphaOne", server="738"), None)

    rounds = next(f.value for f in embed.fields if f.name == "Rounds")
    assert "**Qualifiers** · Group M · Rank 1" in rounds
    # A draw is not a result, so a round nobody has played carries no rank.
    assert "**Semifinals** · Group D" in rounds
    assert "Semifinals** · Group D · Rank" not in rounds
    assert rounds.index("Qualifiers") < rounds.index("Semifinals"), "oldest first"


def test_the_hub_says_which_round_is_running(cd_db):
    import champion_duel_hub as hub

    assert (
        "**Qualifiers** are running"
        in hub.build_hub_embed(servers=db.get_servers(), can_write=True).description
    )

    # The event moving on is what changes this, not a draw being loaded.
    _restart("semifinals")

    assert (
        "**Semifinals** are running"
        in hub.build_hub_embed(servers=db.get_servers(), can_write=True).description
    )

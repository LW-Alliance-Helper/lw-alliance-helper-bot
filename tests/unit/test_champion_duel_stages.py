"""Rounds as a dimension (#495).

A registrant's group and rank belong to a round, not to the person. The bug
this table prevents is silent and unrecoverable: loading a semifinal draw into
one shared column overwrites which qualifier group a player came from, and
imports do not write edits, so there is nothing to revert.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

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

    assert hub.card_subtitle(_player("AlphaOne"), _player("BetaTwo")) == "Group D · Semi-finals"


def test_a_round_is_named_in_exactly_one_place():
    """`STAGE_LABELS` is derived from `PHASE_LABELS`, not restated beside it.

    Two tables meant two places to update and one place to forget, and that is
    exactly what happened: the hub's phase line said "Semi-finals" while a
    player card said "Semifinals", on the same screen.
    """
    for stage in db.STAGES:
        assert db.STAGE_LABELS[stage] is db.PHASE_LABELS[stage], stage
    assert set(db.STAGE_LABELS) == set(db.STAGES)


def test_the_rounds_offered_to_a_reader_are_the_rounds_the_schema_has(cd_db):
    """`STAGES` is the one list of rounds, and the round picker is driven off
    it rather than off what we happen to hold.

    The companion to `test_a_round_is_named_in_exactly_one_place`: that one
    keeps a round from being *named* twice, this one keeps it from being
    *listed* twice. A surface with its own list goes wrong in both directions.
    A round in the schema and not in the picker is unreachable, which is what
    this change fixes; a round in the picker and not in the schema is a value
    nothing will store.

    Order is asserted as well as membership. `STAGES` is documented as
    load-bearing on order -- a player's furthest round is the last of these
    they appear in -- and a picker that reorders them shows a history running
    backwards.
    """
    import champion_duel_hub as hub

    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    db.get_or_create_group(grouping["id"], "semifinals", "H")

    view = hub._GroupView(
        user_id=1,
        groupings=[grouping],
        grouping=grouping,
        stages=db.recorded_stages(grouping["id"]),
        stage="semifinals",
        groups=[],
        label="H",
        members=[],
        can_odds=True,
    )

    assert [option.value for option in view._stage_options()] == list(db.STAGES)


# The Match Overview box for the 8/4 grouping, both halves, transcribed from
# screenshots on 2026-08-15. The dates move with the start date; the durations
# and the gaps between them do not, which is what makes one entered date enough
# to derive every window for every grouping.
OBSERVED_TIMELINE = (
    ("signup", "Sign-up stage", "8/4", "8/9"),
    ("signup_detail", "Sign-up Detail", "8/9", "8/10"),
    ("qualifiers", "Qualifiers", "8/10", "8/14"),
    ("qualifier_detail", "Qualifier Detail", "8/14", "8/17"),
    ("semifinals", "Semi-finals", "8/17", "8/21"),
    ("semifinal_detail", "Semi-final Detail", "8/21", "8/24"),
    ("knockouts", "Knockout Stage", "8/24", "8/29"),
    ("results", "Results", "8/29", "8/31"),
)


def test_the_whole_timeline_matches_the_game(cd_db):
    """All eight phases, against the box they were read off.

    Everything this feature states about dates comes out of one offset table
    and one entered start date, so a slip anywhere in the table is wrong on
    every surface at once and wrong for every grouping. Pinning it against the
    real thing is cheap; finding it from a member's complaint is not.
    """
    import champion_duel_hub as hub

    with db._get_conn() as conn:
        conn.execute("UPDATE groupings SET started_on = '2026-08-04'")
    grouping_id = db.list_groupings()[0]["id"]

    def short(value):
        return f"{value.month}/{value.day}"

    for key, label, first, last in OBSERVED_TIMELINE:
        starts, ends = db.phase_window(grouping_id, key)
        assert (short(starts), short(ends)) == (first, last), key
        assert db.PHASE_LABELS[key] == label, key

    # 8/4 plus the whole event lands on the last day the box shows.
    assert db.EVENT_DAYS == 27
    assert short(db.phase_window(grouping_id, "results")[1]) == "8/31"

    # And the phase line reads back in the game's own format.
    _restart("knockouts")
    assert hub.phase_line(db.list_groupings()[0]).startswith("**Knockout Stage** ")


def test_the_phases_run_end_to_end_with_no_gaps(cd_db):
    """ "The time between each is always the same" is zero: each phase begins the
    day the one before it ends. A gap would mean a day the hub could not name."""
    days = [(first, end) for _, first, end in db.PHASES]
    assert days[0][0] == 0
    for (_, ends), (next_first, _) in zip(days, days[1:]):
        assert ends == next_first


def test_the_labels_are_the_games_own_spelling(cd_db):
    """Verified against the Match Overview box, 2026-08-15. The hyphen in
    "Semi-finals" and the word "Stage" in "Knockout Stage" are the game's."""
    assert db.PHASE_LABELS["semifinals"] == "Semi-finals"
    assert db.PHASE_LABELS["knockouts"] == "Knockout Stage"
    assert db.PHASE_LABELS["signup"] == "Sign-up stage"
    assert db.PHASE_LABELS["qualifier_detail"] == "Qualifier Detail"
    # Every phase in the timeline has a name, and nothing else does.
    assert set(db.PHASE_LABELS) == {key for key, _, _ in db.PHASES}


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

    rounds = next(f.value for f in embed.fields if f.name == hub.FIELD_STAGES)
    assert "**Qualifiers** · Group M · Rank 1" in rounds
    # A draw is not a result, so a round nobody has played carries no rank.
    assert "**Semi-finals** · Group D" in rounds
    assert "Semi-finals** · Group D · Rank" not in rounds
    assert rounds.index("Qualifiers") < rounds.index("Semi-finals"), "oldest first"


def test_the_hub_says_where_the_event_is_and_what_is_next(cd_db):
    """The phase rather than the round, because a Detail window is not a round
    and is still the honest answer to "what is happening right now". Both halves
    on one line: the second is what a member actually opens this to find out."""
    import champion_duel_hub as hub

    _restart("qualifiers")
    grouping = db.list_groupings()[0]

    line = hub.phase_line(grouping)
    assert line.startswith("**Qualifiers** ")
    assert "then **Qualifier Detail**" in line

    # The event moving on is what changes this, not a draw being loaded.
    _restart("semifinal_detail")
    grouping = db.list_groupings()[0]

    line = hub.phase_line(grouping)
    assert line.startswith("**Semi-final Detail** ")
    assert "then **Knockout Stage**" in line


def test_the_phase_line_lays_out_dates_the_way_the_game_does(cd_db):
    """Name then range, so each half is one row of the Match Overview box.

    The layout is borrowed; the punctuation is not. The game writes `8/10~8/14`
    because its UI uses a CJK-origin tilde throughout, and a tilde is not how a
    range is written in the English copy around it.
    """
    import champion_duel_hub as hub

    started = date.fromisoformat(started_so_today_is("qualifiers"))
    with db._get_conn() as conn:
        conn.execute("UPDATE groupings SET started_on = ?", (started.isoformat(),))

    line = hub.phase_line(db.list_groupings()[0])

    qualifiers = started + timedelta(days=6)
    detail = started + timedelta(days=10)
    ends = started + timedelta(days=13)
    assert f"**Qualifiers** {qualifiers.month}/{qualifiers.day}-{detail.month}/{detail.day}" in line
    assert f"**Qualifier Detail** {detail.month}/{detail.day}-{ends.month}/{ends.day}" in line
    assert "~" not in line
    # No year: every date here is inside one 27-day event, and the box the
    # member is comparing against has no year on it either.
    assert str(started.year) not in line


def test_the_last_phase_has_nothing_after_it(cd_db):
    import champion_duel_hub as hub

    _restart("results")

    assert hub.phase_line(db.list_groupings()[0]).startswith("**Results** ")
    assert "then" not in hub.phase_line(db.list_groupings()[0])


def test_a_grouping_with_no_dates_says_nothing_about_the_calendar(cd_db):
    """A line derived from a date we do not have would be a guess presented as
    a schedule."""
    import champion_duel_hub as hub

    with db._get_conn() as conn:
        conn.execute("UPDATE groupings SET started_on = NULL")

    assert hub.phase_line(db.list_groupings()[0]) == ""


# ── Which round the roster reports (#519) ─────────────────────────────────────


def test_a_stage_filtered_roster_reports_the_round_it_filtered_on(cd_db):
    """The window that shipped broken, and that unit tests did not catch.

    `get_roster` scoped its *filter* by round, but every row's `grp` came from
    the furthest round that player was in. So a semifinal draw landing while
    the qualifiers were still running made one response contradict itself:
    filtered into group M, reporting group D. `rank` flipped with it, so the
    Rank column beside it was a semifinal seeding under a qualifier heading.
    """
    db.set_stage(_rid("AlphaOne"), "semifinals", grp="D", rank=3)

    roster = {p["display_name"]: p for p in db.get_roster(group="M")}

    assert set(roster) == {"AlphaOne", "BetaTwo"}, "both are still in qualifier group M"
    assert roster["AlphaOne"]["grp"] == "M", "the round the caller asked about"
    assert roster["AlphaOne"]["rank"] == 1, "and that round's rank, not the semifinal seeding"
    assert roster["AlphaOne"]["stage"] == "qualifiers"
    # The later round is not lost, only not what `grp` surfaces.
    assert roster["AlphaOne"]["stages"]["semifinals"]["grp"] == "D"


def test_an_explicit_stage_scopes_the_report_as_well_as_the_filter(cd_db):
    """The same rule when the caller names the round instead of inheriting it.
    Asking for the qualifiers during the semifinals has to answer about the
    qualifiers all the way down."""
    db.set_stage(_rid("AlphaOne"), "semifinals", grp="D", rank=3)
    _restart("semifinals")

    alpha = db.get_roster(group="M", stage="qualifiers")[0]

    assert (alpha["grp"], alpha["rank"], alpha["stage"]) == ("M", 1, "qualifiers")


def test_the_player_card_still_reports_the_furthest_round(cd_db):
    """The other half of the same rule, and why this is an argument one caller
    passes rather than a change to `attach_stages` outright.

    A card answers "where is this player in the pathway", so it names the
    furthest round they have reached. Someone knocked out in the qualifiers
    should still show the group they went out of.
    """
    db.set_stage(_rid("AlphaOne"), "semifinals", grp="D", rank=3)

    player = _player("AlphaOne")

    assert (player["grp"], player["rank"], player["stage"]) == ("D", 3, "semifinals")


def test_the_unfiltered_roster_is_scoped_too_and_takes_the_blanks(cd_db):
    """The decision on #519's open question, pinned here because it is a choice
    and not a derivation.

    `get_roster` resolves a round whether or not a group was named, so the
    unfiltered read is about that round as well. A player who is not in it
    reports no group rather than a letter from a round nobody asked for. A
    blank is honest; a letter from the wrong round is not, and exempting this
    path would put the bug back where it is hardest to notice. `stages` still
    carries every round for any caller wanting the fuller picture.
    """
    db.set_stage(_rid("AlphaOne"), "semifinals", grp="D", rank=1)
    _restart("semifinals")

    roster = {p["display_name"]: p for p in db.get_roster()}

    assert roster["AlphaOne"]["grp"] == "D", "in the round being read"
    assert roster["BetaTwo"]["grp"] is None, "knocked out in the qualifiers"
    assert roster["BetaTwo"]["rank"] is None
    assert roster["BetaTwo"]["stage"] is None, "and `stage` says so rather than claiming the round"
    assert roster["BetaTwo"]["stages"]["qualifiers"]["grp"] == "M", "not lost, just not surfaced"


def test_a_player_out_of_the_running_round_sorts_after_the_players_in_it(cd_db):
    """The sort needed deciding with it: every newly blank `grp` collapses to
    "" under the old key and lands ahead of Group A. A roster is read for the
    round it is about, so the people no longer in it go last."""
    db.import_registrants(
        [{"name": "AaronZero", "group": "A", "rank": 1, "server": "738"}], stage="qualifiers"
    )
    db.set_stage(_rid("AlphaOne"), "semifinals", grp="D", rank=1)
    _restart("semifinals")

    order = [p["display_name"] for p in db.get_roster()]

    assert order == ["AlphaOne", "AaronZero", "BetaTwo"], (
        "the one still in the semifinals first, then the two who are not, by name"
    )


def test_a_roster_with_no_running_round_falls_back_to_the_furthest(cd_db):
    """Sign-up names no round, so there is nothing to scope to and the rows
    answer the way a player card does. Blanking them instead would report an
    empty draw while the qualifier draw is loaded and perfectly readable."""
    _restart("signup")

    assert db.current_stage() is None
    assert {p["display_name"]: p["grp"] for p in db.get_roster()} == {
        "AlphaOne": "M",
        "BetaTwo": "M",
    }


def test_the_knockout_roster_still_says_who_is_in_it(cd_db):
    """The round with no letter, and the one this nearly broke.

    The knockouts are a single field of 32, so every row in them carries `grp`
    None -- the same value as someone who went out in the qualifiers. `stage`
    is what separates the two, and the sort has to key on that: keying on the
    letter put the eliminated ahead of the survivors in the one round where the
    letter says nothing at all.
    """
    db.set_stage(_rid("AlphaOne"), "knockouts", rank=1)
    _restart("knockouts")

    roster = db.get_roster()
    by_name = {p["display_name"]: p for p in roster}

    assert by_name["AlphaOne"]["stage"] == "knockouts", "still in, with no letter to show for it"
    assert by_name["AlphaOne"]["grp"] is None
    assert by_name["AlphaOne"]["rank"] == 1, "the placement is the whole story here"
    assert by_name["BetaTwo"]["stage"] is None, "out, and the response says which is which"
    assert [p["display_name"] for p in roster] == ["AlphaOne", "BetaTwo"], "still in, first"


def test_a_round_we_hold_no_draw_for_reports_nothing_rather_than_the_last_one(cd_db):
    """The window at every round transition: the calendar has rolled into the
    semifinals and nobody has imported the draw yet.

    The rest of the API is already silent here -- `get_groups` returns nothing
    and a group-filtered roster returns nobody, because there is no semifinal
    group to be in yet. The unfiltered roster says the same rather than handing
    back qualifier letters under a semifinal heading, which is the shape #519
    was about. Falling back to the furthest round we hold would make this the
    one endpoint of the three claiming letters for a round the other two say we
    have nothing for. `stages` still carries the qualifiers for anyone who
    needs them.
    """
    _restart("semifinals")

    assert db.current_stage() == "semifinals"
    assert db.get_groups() == [], "the rest of the API is already silent here"
    assert db.get_roster(group="M") == [], "and so is the filtered read"

    roster = db.get_roster()

    assert [p["grp"] for p in roster] == [None, None]
    assert [p["stage"] for p in roster] == [None, None]
    assert all(p["stages"]["qualifiers"]["grp"] == "M" for p in roster), "not lost, just not shown"

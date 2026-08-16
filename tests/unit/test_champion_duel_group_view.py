"""The group view: who am I facing, and are these seeds or results.

`get_group_members` had no caller outside its own definition and the tests. An
officer could paste a semifinal group of eight, reconcile it, save it, and then
no surface would show them those eight names.

The failure this guards is quiet rather than loud. `seed_rank` and `rank` are
different facts about the same eight people, and a column of numbers that
switches meaning between the draw and the standings reads as correct either
way. That is why the two columns exist at all, so the view has to say which one
it is showing.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import champion_duel_db as db
import champion_duel_hub as hub


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def started_so_today_is(phase: str) -> str:
    first_day = {key: first for key, first, _ in db.PHASES}[phase]
    return (db._server_today() - timedelta(days=first_day)).isoformat()


def _group_of(cd_db_unused, names_ranks, *, stage="semifinals"):
    """A grouping, a group, and members placed in it.

    `names_ranks` is (name, seed_rank, rank), any of the last two None.
    """
    grouping = db.create_grouping(["738"], started_so_today_is(stage), origin="member")
    group = db.get_or_create_group(grouping["id"], stage, "H")
    for name, seed, rank in names_ranks:
        reg = db.upsert_registrant(name, server="738", alliance="OGV")
        db.set_placement(group["id"], reg["id"], seed_rank=seed, rank=rank)
    return grouping, group


def test_a_group_recorded_at_the_draw_says_the_numbers_are_seeds(cd_db):
    """No results exist yet, so the numbers are draw positions and the surface
    says so rather than letting them read as finishing places."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None), ("Beta", 2, None)])
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(members=members, stage="semifinals", label="H", grouping=grouping)

    assert hub._rank_basis(members) == "seeds"
    assert "seed positions" in embed.description
    assert "No results are recorded yet" in embed.description


def test_a_finished_group_does_not_call_its_results_seeds(cd_db):
    """The same eight people, the other moment. Nothing here may say seed."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, 2), ("Beta", 2, 1)])
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(members=members, stage="semifinals", label="H", grouping=grouping)

    assert hub._rank_basis(members) == "results"
    assert "seed" not in embed.description.lower()
    assert "standings" in embed.description


def test_a_half_recorded_group_marks_the_seeds_per_row(cd_db):
    """The header cannot be true for everybody at once here, so the mark moves
    onto the rows that need it and stays off the ones that do not."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, 1), ("Beta", 2, None)])
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(members=members, stage="semifinals", label="H", grouping=grouping)

    assert hub._rank_basis(members) == "mixed"
    lines = {line.split("**")[1]: line for line in embed.description.splitlines() if "**" in line}
    assert "*(seed)*" in lines["Beta"]
    assert "*(seed)*" not in lines["Alpha"]


def test_an_incomplete_group_still_answers_who_am_i_facing(cd_db):
    """Seven names are worth showing. Withholding them until somebody supplies
    the eighth answers nobody's question."""
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 8)])
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(members=members, stage="semifinals", label="H", grouping=grouping)

    assert "P1" in embed.description
    field = next(f for f in embed.fields if f.name == "Not the whole group")
    assert "7 players" in field.value
    assert "8" in field.value


def test_a_complete_group_does_not_nag_about_completeness(cd_db):
    """Eight of eight has nothing to say, and saying it anyway trains people to
    ignore the line that matters when it is short."""
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(members=members, stage="semifinals", label="H", grouping=grouping)

    assert not [f for f in embed.fields if f.name == "Not the whole group"]


def test_the_group_is_named_the_way_the_game_names_it(cd_db):
    """The game writes `Semi-final Grouping: Group H`, so "Group H" is the
    phrase a member arrives already holding."""
    assert hub._group_title("semifinals", "H") == "Group H"


def test_the_knockouts_have_no_letter_and_are_not_given_one(cd_db):
    """One field of 32. `get_groups` drops it for exactly this reason, so a
    title invented from a blank label would be a group that does not exist."""
    assert hub._group_title("knockouts", None) == db.STAGE_LABELS["knockouts"]


def test_a_knockout_placement_says_how_far_they_got(cd_db):
    """Thirty of the 32 go out somewhere. Naming each exit is a scoreboard
    nobody asked us to keep, so the same fact is framed forwards."""
    grouping, group = _group_of(cd_db, [("Alpha", None, 12)], stage="knockouts")
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(members=members, stage="knockouts", label=None, grouping=grouping)

    assert "Made it to Top 16" in embed.description


def test_the_view_never_says_grouping_for_our_own_concept(cd_db):
    """The game's Grouping is the group of 8. Ours is named by its start date.

    Checked on the rendered surface rather than on `_grouping_name` alone,
    because this is the one view where both meanings are on screen together and
    it is the place the collision would actually reach a member.
    """
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(members=members, stage="semifinals", label="H", grouping=grouping)

    rendered = f"{embed.title}\n{embed.description}\n" + "\n".join(
        f"{f.name}\n{f.value}" for f in embed.fields
    )
    assert "rouping" not in rendered
    assert "Group H" in rendered


def test_an_empty_group_says_so_and_names_the_way_in(cd_db):
    """Every dead end carries its exit."""
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")

    embed = hub.build_group_embed(members=[], stage="semifinals", label="H", grouping=grouping)

    assert "do not have anyone recorded" in embed.description
    assert hub._btn_words(hub.CD_BTN_RECORD) in embed.description

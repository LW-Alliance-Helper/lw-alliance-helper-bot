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
    phrase a member arrives already holding.

    The round leads, because a group letter means nothing without it: Group H
    in the semi-finals is a different eight people from Group H in the
    qualifiers. Carrying both in the title lets the description open on the one
    thing neither can say, which is which Champion Duel this is.
    """
    assert hub._group_title("semifinals", "H") == "Semi-finals - Group H"


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


def test_the_description_opens_on_which_champion_duel_this_is(cd_db):
    """The title carries the round and the letter, so the description leads on
    the one fact neither of them can: which Champion Duel."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(members=members, stage="semifinals", label="H", grouping=grouping)

    assert embed.description.startswith("This Champion Duel started ")
    assert embed.title == "Semi-finals - Group H"


def test_an_undated_champion_duel_says_nothing_rather_than_leaving_a_blank(cd_db):
    """An import can establish one before anyone has read its dates, and a
    sentence with a hole where the date goes is worse than no sentence."""
    grouping = db.create_grouping(["738"], None, origin="imported")
    group = db.get_or_create_group(grouping["id"], "semifinals", "H")
    reg = db.upsert_registrant("Alpha", server="738")
    db.set_placement(group["id"], reg["id"], seed_rank=1)

    embed = hub.build_group_embed(
        members=db.get_group_members(group["id"]),
        stage="semifinals",
        label="H",
        grouping=grouping,
    )

    assert "This Champion Duel started" not in embed.description
    assert embed.description.startswith("These are seed positions")


def test_a_finished_champion_duel_still_offers_every_round_it_played(cd_db):
    """`current_stage` answers which round is running, which is the wrong
    question once there is no running round. Someone knocked out is looking
    backwards and every round they played is worth reaching."""
    grouping = db.create_grouping(["738"], started_so_today_is("knockouts"), origin="member")
    for stage in ("qualifiers", "semifinals", "knockouts"):
        db.get_or_create_group(grouping["id"], stage, "H" if stage != "knockouts" else None)

    assert db.recorded_stages(grouping["id"]) == ["qualifiers", "semifinals", "knockouts"]


def test_the_rounds_are_offered_in_playing_order_not_alphabetical(cd_db):
    """Alphabetical would put the knockouts before the qualifiers, which is
    backwards for a history."""
    grouping = db.create_grouping(["738"], started_so_today_is("knockouts"), origin="member")
    for stage in ("knockouts", "qualifiers", "semifinals"):
        db.get_or_create_group(grouping["id"], stage, "A")

    assert db.recorded_stages(grouping["id"]) == list(db.STAGES)


def test_a_round_we_hold_nothing_for_is_not_offered(cd_db):
    """The picker lists what exists, not what the calendar says should."""
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    db.get_or_create_group(grouping["id"], "semifinals", "H")

    assert db.recorded_stages(grouping["id"]) == ["semifinals"]


def test_a_short_group_is_told_to_add_players_not_to_look_up_power(cd_db):
    """Two of eight is not this group, and the model refuses it outright rather
    than scoring six invented players.

    A short group almost always also has people we hold no power for, so the
    order of the two checks is the whole point: pointing someone at two
    players' Total Hero Power when they are six names short is the smaller job
    and the wrong one.
    """
    grouping, group = _group_of(cd_db, [("Alpha", 1, None), ("Beta", 2, None)])
    scouted = db.get_group_scouting(group["id"])

    embed = hub.build_odds_embed(scouted, "semifinals", "H", grouping)

    assert "2 players" in embed.description and "8" in embed.description
    assert hub._btn_words(hub.CD_BTN_RECORD) in embed.description
    assert "Total Hero Power" not in embed.description


def test_a_full_group_missing_power_names_who_to_look_up(cd_db):
    """The other refusal. Here the names are all there and the gap is real, so
    the surface says which two people to go and check.

    On 1.5 this also gained a real exit. The model used to need a Total Hero
    Power, which only the roster import writes, so the copy had to say nobody
    here could fix it. Any single squad power now places a player, and
    `Correct a squad` writes one.
    """
    rows = [(f"P{i}", i, None) for i in range(1, 9)]
    grouping, group = _group_of(cd_db, rows)
    for member in db.get_group_members(group["id"]):
        if member["display_name"] in ("P3", "P6"):
            continue
        db.upsert_registrant(member["display_name"], server="738", thp=300_000_000)
    scouted = db.get_group_scouting(group["id"])

    embed = hub.build_odds_embed(scouted, "semifinals", "H", grouping)

    assert "neither a Total Hero Power nor a single squad power" in embed.description
    assert "**P3**" in embed.description and "**P6**" in embed.description
    # Deliberately names no button. `Correct a squad` writes the missing value
    # but sits on a player's own card, two surfaces away, and renders locked
    # without Premium. A signpost to a padlock is a worse dead end than none.
    assert hub._btn_words(hub.CD_BTN_SQUADS) not in embed.description
    # The negative half is the one that matters. Without it this passes when
    # every player is reported missing, which is exactly the breakage it is
    # written to rule out.
    assert "**P1**" not in embed.description
    assert "**P8**" not in embed.description


def test_the_odds_button_is_offered_wherever_there_is_a_model(cd_db):
    """The qualifiers and the semi-finals are separate models with separate
    constants, and both ship. The knockouts are a single-elimination field of
    32, which nothing models, so the button is absent there rather than present
    and refusing.

    Gated on `odds_lib.STAGES_WITH_A_MODEL` rather than a list here, so adding
    a model turns the control on in one place.
    """
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    members = db.get_group_members(group["id"])

    def labels(stage):
        view = _odds_view(grouping, members, can_odds=True, stage=stage)
        return [getattr(i, "label", None) for i in view.children]

    assert hub.CD_BTN_ODDS in labels("semifinals")
    assert hub.CD_BTN_ODDS in labels("qualifiers")
    assert hub.CD_BTN_ODDS not in labels("knockouts")


def _odds_view(grouping, members, *, can_odds, stage="semifinals"):
    return hub._GroupView(
        user_id=1,
        groupings=[grouping],
        grouping=grouping,
        stages=[stage],
        stage=stage,
        groups=[],
        label="H",
        members=members,
        can_odds=can_odds,
    )


def test_the_odds_are_the_one_gated_thing_and_it_locks_rather_than_hides(cd_db):
    """`DESIGN.md`'s Premium rule: a locked control renders disabled so the
    free tier can see the shape of the paid product. It reads well here because
    everything around it is free. An alliance sees their eight opponents, sees
    the button, and knows exactly what it would tell them."""
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    members = db.get_group_members(group["id"])

    locked = _odds_view(grouping, members, can_odds=False)

    button = next(b for b in locked.children if hub.CD_BTN_ODDS in (b.label or ""))
    assert button.disabled is True
    assert button.label.startswith("🔒")


def test_the_upsell_rides_on_the_embed_not_the_disabled_button(cd_db):
    """A disabled button cannot carry a reason, so the embed does. It names
    what the odds add over what this surface already gives away for nothing."""
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    members = db.get_group_members(group["id"])

    locked = hub.build_group_embed(
        members=members, stage="semifinals", label="H", grouping=grouping, can_odds=False
    )
    paid = hub.build_group_embed(
        members=members, stage="semifinals", label="H", grouping=grouping, can_odds=True
    )

    upsell = next(f for f in locked.fields if "🔒" in f.name)
    assert "free" in upsell.value, "it has to say what is not being taken away"
    assert "/upgrade" in upsell.value
    assert not any("🔒" in f.name for f in paid.fields)


def test_a_round_with_no_model_never_sells_odds_it_cannot_produce(cd_db):
    """The knockouts have no model, so the button is absent there. An upsell
    for it would be selling something no amount of paying reaches."""
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)], stage="knockouts")
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(
        members=members, stage="knockouts", label=None, grouping=grouping, can_odds=False
    )

    assert not any("🔒" in f.name for f in embed.fields)


async def test_an_entitlement_that_lapsed_while_the_group_was_open_is_caught(cd_db, monkeypatch):
    """The stale case that actually reaches this. A view built by a paying
    alliance has the button ENABLED, and it stays pressable for the 15 minutes
    the view lives against a 5 minute entitlement cache. Reading the flag
    captured at build time would let that through, so the gate is re-resolved
    on the press."""
    from unittest.mock import AsyncMock, MagicMock

    import premium

    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    members = db.get_group_members(group["id"])
    # Built while they were paying, pressed after they stopped.
    view = _odds_view(grouping, members, can_odds=True)
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=False))
    inter = MagicMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await view._on_odds(inter)

    embed = inter.followup.send.await_args.kwargs["embed"]
    assert "Premium" in embed.title


async def test_the_upsell_survives_having_no_subscribe_button(cd_db, monkeypatch):
    """`upgrade_view` returns None when no SKU is configured, and discord.py
    raises `TypeError` on a `view=None`. The embed's own "Run `/upgrade`" line
    carries it instead."""
    from unittest.mock import AsyncMock, MagicMock

    import premium

    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=False))
    monkeypatch.setattr(premium, "upgrade_view", lambda: None)
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    view = _odds_view(grouping, db.get_group_members(group["id"]), can_odds=True)
    inter = MagicMock()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()

    await view._on_odds(inter)

    assert "view" not in inter.followup.send.await_args.kwargs


def test_the_gate_flag_is_required_on_the_view(cd_db):
    """No default, so a future call site cannot ship the odds free by omission.
    `build_group_embed` keeps a default on purpose: omitting it there shows no
    upsell, where omitting it here hands out the paid thing."""
    import inspect

    parameter = inspect.signature(hub._GroupView.__init__).parameters["can_odds"]
    assert parameter.default is inspect.Parameter.empty


def test_a_full_group_with_power_actually_reaches_the_odds(cd_db):
    """The success path, end to end from the database.

    Everything else about the odds is tested against hand-built dicts, which is
    exactly how the surface came to be broken in a way no test saw:
    `get_group_scouting` did not select `registrants.thp`, so every real group
    was refused while every test passed. This one goes through the real query.
    """
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    for i in range(1, 9):
        db.upsert_registrant(f"P{i}", server="738", thp=300_000_000 + i * 9_000_000)
    scouted = db.get_group_scouting(group["id"])

    assert all(row["thp"] for row in scouted), "thp must survive the join"

    embed = hub.build_odds_embed(scouted, "semifinals", "H", grouping)

    assert "simulations of the round" in embed.description
    assert "**P8**" in embed.description
    assert "Total Hero Power" not in embed.description


# ── Putting one player in a group ─────────────────────────────────────────────


def test_setting_a_group_offers_only_the_rounds_that_have_letters(cd_db):
    """The knockouts are one field of 32 with no letter, so there would be
    nothing to pick. Both dropdowns come from the db's own constants rather
    than a list here, so a renamed round cannot leave the two disagreeing."""
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    reg = db.upsert_registrant("Alpha", server="738")

    modal = hub._PlaceInGroupModal(player=reg, grouping=grouping)

    rounds = [o.value for o in modal.stage.component.options]
    letters = [o.value for o in modal.group.component.options]
    assert rounds == ["qualifiers", "semifinals"]
    assert "knockouts" not in rounds
    assert letters == list(db.GROUP_LABELS)


def test_the_button_is_absent_without_a_champion_duel(cd_db):
    """A group letter is meaningless outside one, so there is nothing the
    control could set. Absent rather than disabled."""
    reg = db.upsert_registrant("Alpha", server="738")
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")

    without = hub.PlayerActionsView(player=reg, user_id=1, can_write=True, grouping=None)
    with_one = hub.PlayerActionsView(player=reg, user_id=1, can_write=True, grouping=grouping)

    assert hub.CD_BTN_PLACE not in [b.label for b in without.children]
    assert hub.CD_BTN_PLACE in [b.label for b in with_one.children]


def test_the_label_does_not_claim_they_are_being_registered(cd_db):
    """They registered for this Champion Duel in the game weeks ago. What is
    missing is where the draw put them, and a label saying otherwise describes
    an outcome the control does not have."""
    assert "register" not in hub.CD_BTN_PLACE.lower()
    assert "group" in hub.CD_BTN_PLACE.lower()

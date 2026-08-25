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

import discord
import pytest

import champion_duel_db as db
import champion_duel_hub as hub
import champion_duel_odds as odds_lib


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


def test_a_round_we_hold_nothing_for_is_not_reported_as_recorded(cd_db):
    """`recorded_stages` answers what our record holds, and that is all it has
    ever answered.

    It used to be the picker's option list too, which is what made a missing
    round and a missing picker the same screen. The picker is off `db.STAGES`
    now and this reading is unchanged: what it returns is the marks, not the
    options."""
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
    """Each round turns the control on as its model lands, and off if the model
    is taken away again.

    The knockouts arrived in engine 1.12.0 and were green-lit on 2026-08-19.
    Before that this test asserted the button was ABSENT there, on the grounds
    that nothing modelled a single-elimination field of 32 — true when written,
    and the sentence the wiring map records as wrong in eight places once
    `knockout.py` shipped.

    The qualifiers went the other way on 2026-08-21. Nothing about the model
    changed: odds need a power for every player in the group, a qualifier group
    is 100, and no path exists that delivers 100 of them, so the button was
    offered in the round people first meet the feature and refused every press.

    Still gated on `odds_lib.STAGES_WITH_A_MODEL` rather than on a list here,
    which is what lets an older pin keep the group round and simply not offer
    the bracket.
    """
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    members = db.get_group_members(group["id"])

    def labels(stage):
        view = _odds_view(grouping, members, can_odds=True, stage=stage)
        return [getattr(i, "label", None) for i in view.children]

    assert hub.CD_BTN_ODDS in labels("semifinals")
    assert hub.CD_BTN_ODDS not in labels("qualifiers")
    assert (hub.CD_BTN_ODDS in labels("knockouts")) == odds_lib.KNOCKOUT_AVAILABLE


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

    button = next(
        b for b in locked.children if hub.CD_BTN_ODDS in (getattr(b, "label", None) or "")
    )
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
    """The upsell follows the model, in both directions.

    A padlock on a round nothing can score sells something no amount of paying
    reaches, and an absent padlock on a round that CAN be scored hides the paid
    product from the tier it is aimed at. Both are decided by the same tuple,
    which is why this asserts against it rather than naming a round.
    """
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)], stage="knockouts")
    members = db.get_group_members(group["id"])

    embed = hub.build_group_embed(
        members=members, stage="knockouts", label=None, grouping=grouping, can_odds=False
    )

    locked = any("🔒" in f.name for f in embed.fields)
    assert locked == ("knockouts" in odds_lib.STAGES_WITH_A_MODEL)


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


# ── the round picker ─────────────────────────────────────────────────────────


def _picker(view, placeholder):
    """The select with this placeholder, or None. Buttons have no placeholder
    and a `getattr` default would evaluate one on them, so this is explicit."""
    for item in view.children:
        if isinstance(item, discord.ui.Select) and item.placeholder == placeholder:
            return item
    return None


def _view_of(grouping, *, stage, members, groups=(), label=None, stages=None, **kwargs):
    return hub._GroupView(
        user_id=1,
        groupings=[grouping],
        grouping=grouping,
        stages=list(db.recorded_stages(grouping["id"]) if stages is None else stages),
        stage=stage,
        groups=list(groups),
        label=label,
        members=list(members),
        can_odds=True,
        **kwargs,
    )


def test_every_round_the_game_plays_is_offered(cd_db):
    """The complaint that started this. Holding one round hid the picker
    entirely, so "no other round exists" and "the picker is missing" were the
    same screen and a member could not tell which they were looking at."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])
    members = db.get_group_members(group["id"])

    view = _view_of(grouping, stage="semifinals", members=members, label="H")

    assert db.recorded_stages(grouping["id"]) == ["semifinals"]
    picker = _picker(view, "Which round?")
    assert picker is not None
    assert [o.value for o in picker.options] == list(db.STAGES)


def test_the_rounds_are_offered_in_the_order_they_are_played(cd_db):
    """Alphabetical would put the knockouts first, which is backwards for a
    history and wrong for a calendar."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])

    view = _view_of(grouping, stage="semifinals", members=db.get_group_members(group["id"]))

    labels = [o.label for o in _picker(view, "Which round?").options]
    assert labels == [db.STAGE_LABELS[s] for s in db.STAGES]


def test_a_round_we_hold_nothing_for_is_marked_rather_than_hidden(cd_db):
    """The mark is text on the description line, not a color: `DESIGN.md` rule
    9 says a glyph has to work by shape, and this has to work for a screen
    reader too."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])

    view = _view_of(grouping, stage="semifinals", members=db.get_group_members(group["id"]))

    marks = {o.value: o.description for o in _picker(view, "Which round?").options}
    assert marks["semifinals"] is None
    assert marks["qualifiers"] == hub._STAGE_NOT_HELD
    assert marks["knockouts"] == hub._STAGE_NOT_HELD


def test_an_empty_round_invites_the_person_reading_it(cd_db):
    """Kevin's call, 2026-08-24: "You can add it", not "Anyone can add it".

    The picker offers every round the game plays, so a member can open one
    nobody has ever recorded. What they find there is the only invitation this
    feature makes, and it is made to them rather than announced to a room. The
    button under it is live for them, which is what makes the second person
    plural the wrong one.
    """
    grouping, _group = _group_of(cd_db, [])

    embed = hub.build_group_embed(members=[], stage="knockouts", label=None, grouping=grouping)

    assert "You can add it" in embed.description
    assert "Anyone" not in embed.description
    assert hub._btn_words(hub.CD_BTN_RECORD) in embed.description


def test_the_alliance_filter_offers_a_way_back_to_the_whole_list(cd_db):
    """`All alliances`, signed off 2026-08-24 over "Everyone".

    Pinned because the sentence case is the half that gets "corrected": it is
    `notes/DESIGN.md`'s Labels rule, not a typo, and Kevin's own words for it
    were Title Case.
    """
    rows = [(f"P{i}", i, None) for i in range(1, 30)]
    grouping, group = _group_of(cd_db, rows)
    for i, member in enumerate(db.get_group_members(group["id"])):
        db.upsert_registrant(member["display_name"], server="738", alliance="OGV" if i else "Kite")

    view = _view_of(grouping, stage="semifinals", members=db.get_group_members(group["id"]))

    unfiltered = _picker(view, "Which alliance?").options[0]
    assert unfiltered.label == "All alliances"
    assert unfiltered.value == hub._FILTER_ALL


def test_the_round_being_read_is_the_one_showing_as_chosen(cd_db):
    """A picker that offers three rounds and marks none of them as current
    leaves the reader guessing which one the list below it is."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])

    view = _view_of(grouping, stage="semifinals", members=db.get_group_members(group["id"]))

    chosen = [o.value for o in _picker(view, "Which round?").options if o.default]
    assert chosen == ["semifinals"]


def test_opening_a_round_we_hold_nothing_for_does_not_record_it(cd_db):
    """**Reading must not write.**

    `get_or_create_group` inserts, and the picker now reaches rounds nobody has
    recorded. Calling it on one would create the `groups` row that makes
    `recorded_stages` report the round as held, so the round would stop
    offering the contribution door it exists to offer, closed by somebody
    looking at it and with nothing to undo it.
    """
    grouping, _group = _group_of(cd_db, [("Alpha", 1, None)])
    recorded = db.recorded_stages(grouping["id"])

    assert hub._read_group(grouping["id"], "knockouts", None, recorded) == []
    assert db.recorded_stages(grouping["id"]) == recorded


def test_a_round_we_do_hold_still_reads_its_members(cd_db):
    """The negative half. Without it the guard above passes on a function that
    returns nothing for everything."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None), ("Beta", 2, None)])
    recorded = db.recorded_stages(grouping["id"])

    rows = hub._read_group(grouping["id"], "semifinals", "H", recorded)

    assert [r["display_name"] for r in rows] == ["Alpha", "Beta"]


def test_an_empty_round_says_so_and_offers_to_record_it(cd_db):
    """Kevin's own words for what the picker should do: a quick click to
    Knockouts shows we have nothing and prompts them to add it.

    The button matters as much as the sentence. Naming a control in prose was
    the whole offer before, and the control it named is on the hub message the
    reader scrolled past to get here.
    """
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])

    view = _view_of(grouping, stage="knockouts", members=[])
    embed = view._embed()

    assert "this round" in embed.description
    assert hub._btn_words(hub.CD_BTN_RECORD) in embed.description
    assert any(hub.CD_BTN_RECORD in (getattr(i, "label", None) or "") for i in view.children)


def test_an_empty_lettered_group_is_not_told_the_round_is_missing(cd_db):
    """Two shapes of nothing. A group we hold nobody for sits inside a round we
    do hold, and the reader picked that letter to get here."""
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    db.get_or_create_group(grouping["id"], "semifinals", "H")

    embed = hub.build_group_embed(members=[], stage="semifinals", label="H", grouping=grouping)

    assert "this group" in embed.description
    assert "this round" not in embed.description


def test_a_champion_duel_we_hold_nothing_for_opens_rather_than_refusing(cd_db):
    """It used to answer "no rounds are recorded" and stop, which put the
    flattest dead end in the feature exactly where the contribution was most
    wanted: an alliance that has just entered its Participating Warzones holds
    nothing by definition."""
    assert hub._opening_stage([], None) == db.STAGES[0]
    assert hub._opening_stage([], "semifinals") == "semifinals"
    assert hub._opening_stage(["qualifiers"], "semifinals") == "qualifiers"
    assert hub._opening_stage(["qualifiers", "semifinals"], "semifinals") == "semifinals"


# ── filtered lists ───────────────────────────────────────────────────────────


def _hundred(cd_db_unused, tags):
    """A qualifier group of len(tags) players, one per alliance tag. `None` is a
    player we hold no tag for."""
    grouping = db.create_grouping(["738"], started_so_today_is("qualifiers"), origin="member")
    group = db.get_or_create_group(grouping["id"], "qualifiers", "D")
    for i, tag in enumerate(tags, start=1):
        reg = db.upsert_registrant(f"P{i:03d}", server="738", alliance=tag)
        db.set_placement(group["id"], reg["id"], rank=i)
    return grouping, db.get_group_members(group["id"])


def test_a_long_list_comes_back_a_page_at_a_time(cd_db):
    grouping, members = _hundred(cd_db, ["Kite"] * 50 + ["Wren"] * 50)

    first = hub.build_group_embed(members=members, stage="qualifiers", label="D", grouping=grouping)
    second = hub.build_group_embed(
        members=members, stage="qualifiers", label="D", grouping=grouping, page=1
    )

    assert "**P001**" in first.description and "**P020**" in first.description
    assert "**P021**" not in first.description
    assert "**P021**" in second.description and "**P001**" not in second.description
    assert first.footer.text == "Showing 1 to 20 of 100 players."


def test_a_page_past_the_end_lands_on_the_last_one(cd_db):
    """The page can fall off the end when a filter changes under it, so the
    clamp is load-bearing rather than defensive."""
    grouping, members = _hundred(cd_db, ["Kite"] * 30)

    embed = hub.build_group_embed(
        members=members, stage="qualifiers", label="D", grouping=grouping, page=99
    )

    assert embed.footer.text == "Showing 21 to 30 of 30 players."


def test_the_filter_narrows_the_list_to_one_alliance(cd_db):
    """The 100 rows were never too long, they were unfiltered. Every one of
    them is in the reader's round and almost none is anybody they know."""
    grouping, members = _hundred(cd_db, ["Kite"] * 90 + ["OGV"] * 10)

    embed = hub.build_group_embed(
        members=members, stage="qualifiers", label="D", grouping=grouping, alliance="OGV"
    )

    assert "**P091**" in embed.description
    assert "**P001**" not in embed.description
    assert embed.footer.text == "Showing 10 players. Filtered from 100."


def test_the_filter_does_not_change_what_the_round_is_missing(cd_db):
    """Completeness is measured against what the round holds. Measuring it
    against a filtered count would report a gap the filter invented."""
    grouping, members = _hundred(cd_db, ["Kite"] * 90 + ["OGV"] * 10)

    embed = hub.build_group_embed(
        members=members, stage="qualifiers", label="D", grouping=grouping, alliance="OGV"
    )

    assert not [f for f in embed.fields if f.name == "Not the whole group"]


def test_a_filtered_group_that_is_short_still_says_so(cd_db):
    """The other half of the same rule, and the one that would break silently:
    ten of a hundred filtered to ten must not read as a complete group."""
    grouping, members = _hundred(cd_db, ["Kite"] * 30 + ["OGV"] * 10)

    embed = hub.build_group_embed(
        members=members, stage="qualifiers", label="D", grouping=grouping, alliance="OGV"
    )

    field = next(f for f in embed.fields if f.name == "Not the whole group")
    assert "40 players" in field.value and "100" in field.value


def test_a_short_group_gets_neither_a_filter_nor_a_pager(cd_db):
    """A semifinal group is eight players from up to eight alliances. A filter
    over it costs a row and saves nobody a scroll."""
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 9)])
    members = db.get_group_members(group["id"])

    view = _view_of(grouping, stage="semifinals", members=members, label="H")

    assert _picker(view, "Which alliance?") is None
    assert not [i for i in view.children if "Page " in (getattr(i, "label", None) or "")]
    # Eight of eight, so nothing to add either.
    assert not [i for i in view.children if hub.CD_BTN_RECORD in (getattr(i, "label", None) or "")]


def test_the_filter_appears_where_the_list_is_actually_long(cd_db):
    grouping, members = _hundred(cd_db, ["Kite"] * 60 + ["OGV"] * 40)

    view = _view_of(grouping, stage="qualifiers", members=members, label="D")

    picker = _picker(view, "Which alliance?")
    assert picker is not None
    assert [o.value for o in picker.options] == [hub._FILTER_ALL, "Kite", "OGV"]
    assert [o.description for o in picker.options] == [
        "100 players",
        "60 players",
        "40 players",
    ]


def test_the_alliances_are_offered_biggest_first(cd_db):
    """Which is what makes the cut below safe: what a 24-option select drops is
    the one- and two-player tail."""
    grouping, members = _hundred(cd_db, ["Kite"] * 21 + ["OGV"] * 60 + ["Wren"] * 19)

    view = _view_of(grouping, stage="qualifiers", members=members, label="D")

    assert [o.value for o in _picker(view, "Which alliance?").options][1:] == [
        "OGV",
        "Kite",
        "Wren",
    ]


def test_a_filter_that_cannot_list_every_alliance_says_how_many_it_dropped(cd_db):
    """A hundred players from sixteen warzones carry more alliances than a
    select holds. Dropping the tail silently reads as "your alliance is not in
    this group", which for a two-player alliance is exactly wrong."""
    tags = [f"A{i:02d}" for i in range(30)] * 2 + ["Big"] * 40
    grouping, members = _hundred(cd_db, tags)

    view = _view_of(grouping, stage="qualifiers", members=members, label="D")

    options = _picker(view, "Which alliance?").options
    assert len(options) == hub._ALLIANCES_SHOWN + 1
    assert "7 smaller alliances not listed" in options[0].description


def test_a_filter_set_on_an_alliance_the_cut_dropped_still_shows_as_chosen(cd_db):
    """Otherwise the select reads as unfiltered over a list that is filtered."""
    tags = [f"A{i:02d}" for i in range(30)] * 2 + ["Big"] * 40
    grouping, members = _hundred(cd_db, tags)

    view = _view_of(grouping, stage="qualifiers", members=members, label="D", alliance="A29")

    options = _picker(view, "Which alliance?").options
    assert [o.value for o in options if o.default] == ["A29"]
    assert len(options) == hub._ALLIANCES_SHOWN + 1


def test_players_we_hold_no_alliance_for_are_reachable_only_unfiltered(cd_db):
    """A blank tag is a gap in the record, not a group somebody is in. They are
    counted by nobody and are in the unfiltered list, which is where the person
    who could fill the gap will find them."""
    grouping, members = _hundred(cd_db, ["Kite"] * 25 + [None] * 5)

    assert hub._alliance_counts(members) == [("Kite", 25)]
    view = _view_of(grouping, stage="qualifiers", members=members, label="D")
    assert _picker(view, "Which alliance?") is None

    everyone = hub.build_group_embed(
        members=members, stage="qualifiers", label="D", grouping=grouping, page=1
    )
    assert "**P030**" in everyone.description


def test_a_filter_matching_nobody_says_so_rather_than_showing_an_empty_list(cd_db):
    """Unreachable through the view, which builds its options out of the
    alliances present. Written because the parameter is public."""
    grouping, members = _hundred(cd_db, ["Kite"] * 30)

    embed = hub.build_group_embed(
        members=members, stage="qualifiers", label="D", grouping=grouping, alliance="Nobody"
    )

    assert "**Nobody**" in embed.description
    assert "**P001**" not in embed.description


def test_the_seed_or_result_sentence_does_not_change_between_pages(cd_db):
    """`_rank_basis` reads the whole group, not the page. A header that rewords
    between page two and page three is the failure `UX.md` names for field
    names that move when the data thins."""
    grouping = db.create_grouping(["738"], started_so_today_is("qualifiers"), origin="member")
    group = db.get_or_create_group(grouping["id"], "qualifiers", "D")
    for i in range(1, 31):
        reg = db.upsert_registrant(f"P{i:03d}", server="738", alliance="Kite")
        db.set_placement(group["id"], reg["id"], seed_rank=i, rank=i if i <= 20 else None)
    members = db.get_group_members(group["id"])

    first = hub.build_group_embed(members=members, stage="qualifiers", label="D", grouping=grouping)
    second = hub.build_group_embed(
        members=members, stage="qualifiers", label="D", grouping=grouping, page=1
    )

    opening = first.description.split("\n\n")[0]
    assert opening == second.description.split("\n\n")[0]
    assert "Rows marked" in opening


def test_the_busiest_this_view_gets_still_fits_discords_grid(cd_db):
    """Five rows and twenty-five components is the hard limit, and the round
    picker now takes a row it used to hand back.

    Two shapes reach the ceiling from different directions and neither has a
    spare row, so both are pinned rather than reasoned about.

    A qualifier group is the widest: two Champion Duels, a round, several
    letters, an alliance filter and a three-button pager, which is exactly five
    rows. It carries no odds button, because the qualifiers stopped offering
    one on 2026-08-21 -- a qualifier group is a hundred players and no path
    delivers a power for all hundred.

    The knockouts are the busiest row: one field of 32, so no letters, and a
    model, so the odds button lands beside the pager. That is four buttons on
    one row against a limit of five.

    The record button cannot join either. It renders only where the group holds
    nobody, and an empty group has no alliances to filter and no second page.
    """
    grouping, members = _hundred(cd_db, ["Kite"] * 60 + ["OGV"] * 40)
    other = db.create_grouping(["744"], started_so_today_is("qualifiers"), origin="member")
    for letter in ("A", "B", "C"):
        db.get_or_create_group(grouping["id"], "qualifiers", letter)

    def grid(view):
        rows = [item.row for item in view.children]
        assert max(rows) <= 4, rows
        assert len(view.children) <= 25
        assert all(rows.count(r) <= 5 for r in set(rows)), rows
        return {getattr(i, "placeholder", None) or getattr(i, "label", None) for i in view.children}

    widest = grid(
        hub._GroupView(
            user_id=1,
            groupings=[grouping, other],
            grouping=grouping,
            stages=db.recorded_stages(grouping["id"]),
            stage="qualifiers",
            groups=db.get_groups("qualifiers", grouping["id"]),
            label="D",
            members=members,
            can_odds=True,
        )
    )
    assert {
        "Which Champion Duel?",
        "Which round?",
        "Which group?",
        "Which alliance?",
        "Page 1 / 5",
    } <= widest

    field = db.get_or_create_group(grouping["id"], "knockouts", None)
    for i, tag in enumerate(["Kite"] * 20 + ["OGV"] * 12, start=1):
        reg = db.upsert_registrant(f"K{i:03d}", server="738", alliance=tag)
        db.set_placement(field["id"], reg["id"], rank=i)

    busiest = grid(
        hub._GroupView(
            user_id=1,
            groupings=[grouping, other],
            grouping=grouping,
            stages=db.recorded_stages(grouping["id"]),
            stage="knockouts",
            groups=db.get_groups("knockouts", grouping["id"]),
            label=None,
            members=db.get_group_members(field["id"]),
            can_odds=True,
        )
    )
    assert {"Which Champion Duel?", "Which round?", "Which alliance?", "Page 1 / 2"} <= busiest
    assert ("Which group?" in busiest) is False
    assert (hub.CD_BTN_ODDS in busiest) == odds_lib.KNOCKOUT_AVAILABLE
    # A complete field, so no record button. Two of the 32 missing is what puts
    # five on one row: a three-button pager, the odds and the door.
    assert hub.CD_BTN_RECORD not in busiest

    db.set_placement(field["id"], db.upsert_registrant("K033", server="738")["id"], rank=33)
    short = [m for m in db.get_group_members(field["id"]) if m["display_name"] != "K001"][:30]
    fullest = grid(
        hub._GroupView(
            user_id=1,
            groupings=[grouping, other],
            grouping=grouping,
            stages=db.recorded_stages(grouping["id"]),
            stage="knockouts",
            groups=db.get_groups("knockouts", grouping["id"]),
            label=None,
            members=short,
            can_odds=True,
        )
    )
    assert hub.CD_BTN_RECORD in fullest


def test_a_filter_never_outlives_the_control_that_undoes_it(cd_db):
    """Found by `/code-review`, and it needed both halves of the surface to see.

    Filter a hundred-player qualifier group to one alliance, then move to a
    round holding eight. The eight are too few for the select to render, so the
    filter would have been narrowing the list with nothing on screen saying so
    and no way back to the whole group.
    """
    grouping, members = _hundred(cd_db, ["Kite"] * 60 + ["OGV"] * 40)
    semi = db.get_or_create_group(grouping["id"], "semifinals", "H")
    for i in range(1, 9):
        reg = db.upsert_registrant(f"S{i}", server="738", alliance="OGV" if i < 3 else "Kite")
        db.set_placement(semi["id"], reg["id"], rank=i)

    view = _view_of(grouping, stage="qualifiers", members=members, label="D", alliance="OGV")
    assert view.alliance == "OGV"

    view.stage = "semifinals"
    view.label = "H"
    view.members = db.get_group_members(semi["id"])
    view._build()

    assert _picker(view, "Which alliance?") is None
    assert view.alliance is None
    assert "**S8**" in view._embed().description


def test_a_filter_on_an_alliance_that_left_the_group_clears_itself(cd_db):
    """The other way the same trap opens: the control renders, but on an
    alliance nobody in the new list carries, so every row is filtered out."""
    grouping, members = _hundred(cd_db, ["Kite"] * 60 + ["OGV"] * 40)
    other, replacements = _hundred(cd_db, ["Wren"] * 50 + ["Kite"] * 50)

    view = _view_of(grouping, stage="qualifiers", members=members, label="D", alliance="OGV")
    view.members = replacements
    view._build()

    assert view.alliance is None
    assert _picker(view, "Which alliance?") is not None


def test_an_incomplete_group_offers_the_door_its_own_words_name(cd_db):
    """Found by `/code-review`. The completeness field says "anyone can add the
    rest with Record a group" and the button was on the empty state only, so
    the sentence pointed at a control on the hub message the reader had already
    scrolled past."""
    grouping, members = _hundred(cd_db, ["Kite"] * 40)

    view = _view_of(grouping, stage="qualifiers", members=members, label="D")
    embed = view._embed()

    assert "Not the whole group" in [f.name for f in embed.fields]
    assert any(hub.CD_BTN_RECORD in (getattr(i, "label", None) or "") for i in view.children)


def test_the_door_and_the_sentence_agree_about_a_complete_group(cd_db):
    """The negative half, and the one that would break silently: a hundred of a
    hundred says nothing and offers nothing."""
    grouping, members = _hundred(cd_db, ["Kite"] * 60 + ["OGV"] * 40)

    view = _view_of(grouping, stage="qualifiers", members=members, label="D")
    embed = view._embed()

    assert "Not the whole group" not in [f.name for f in embed.fields]
    assert not [i for i in view.children if hub.CD_BTN_RECORD in (getattr(i, "label", None) or "")]


def test_only_one_control_is_the_recommended_one(cd_db):
    """`notes/DESIGN.md`: at most one primary per view. The odds and the door
    can now share a row, so this is reachable rather than theoretical."""
    grouping, group = _group_of(cd_db, [(f"P{i}", i, None) for i in range(1, 8)])
    members = db.get_group_members(group["id"])

    view = _view_of(grouping, stage="semifinals", members=members, label="H")

    primaries = [
        getattr(i, "label", None)
        for i in view.children
        if getattr(i, "style", None) is discord.ButtonStyle.primary
    ]
    assert primaries == [hub.CD_BTN_ODDS]
    assert any(hub.CD_BTN_RECORD in (getattr(i, "label", None) or "") for i in view.children)


def test_the_empty_round_makes_recording_the_recommended_one(cd_db):
    """Where there is nothing else to do, the door is the recommendation."""
    grouping, _group = _group_of(cd_db, [("Alpha", 1, None)])

    view = _view_of(grouping, stage="knockouts", members=[])

    primaries = [
        getattr(i, "label", None)
        for i in view.children
        if getattr(i, "style", None) is discord.ButtonStyle.primary
    ]
    assert primaries == [hub.CD_BTN_RECORD]

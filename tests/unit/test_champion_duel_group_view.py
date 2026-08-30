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
from unittest.mock import patch

import discord
import pytest

import champion_duel_db as db
import champion_duel_hub as hub
import champion_duel_odds as odds_lib
import champion_duel_store as store_lib
import champion_duel_wording as words


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    # `🏰 Your alliance` reads stored odds, and `store_lib.lookup` degrades
    # rather than raising on a bad store -- so without the table these tests
    # would pass while silently exercising the no-odds branch every time.
    # Idempotent, and it is the pair `on_ready` calls.
    store_lib.init_store()
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

    trials = odds_lib._models()["semifinals"]["trials"]
    assert hub._ODDS_OVER.format(trials=trials) in embed.description
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
    picker = _picker(view, hub._PICK_STAGE)
    assert picker is not None
    assert [o.value for o in picker.options] == list(db.STAGES)


def test_the_rounds_are_offered_in_the_order_they_are_played(cd_db):
    """Alphabetical would put the knockouts first, which is backwards for a
    history and wrong for a calendar."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])

    view = _view_of(grouping, stage="semifinals", members=db.get_group_members(group["id"]))

    labels = [o.label for o in _picker(view, hub._PICK_STAGE).options]
    assert labels == [db.STAGE_LABELS[s] for s in db.STAGES]


def test_a_round_we_hold_nothing_for_is_marked_rather_than_hidden(cd_db):
    """The mark is text on the description line, not a color: `DESIGN.md` rule
    9 says a glyph has to work by shape, and this has to work for a screen
    reader too."""
    grouping, group = _group_of(cd_db, [("Alpha", 1, None)])

    view = _view_of(grouping, stage="semifinals", members=db.get_group_members(group["id"]))

    marks = {o.value: o.description for o in _picker(view, hub._PICK_STAGE).options}
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

    chosen = [o.value for o in _picker(view, hub._PICK_STAGE).options if o.default]
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

    assert hub._GROUP_NO_STAGE.format(record=hub._btn_words(hub.CD_BTN_RECORD)) in embed.description
    assert hub._btn_words(hub.CD_BTN_RECORD) in embed.description
    assert any(hub.CD_BTN_RECORD in (getattr(i, "label", None) or "") for i in view.children)


def test_an_empty_lettered_group_is_not_told_the_round_is_missing(cd_db):
    """Two shapes of nothing. A group we hold nobody for sits inside a round we
    do hold, and the reader picked that letter to get here."""
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    db.get_or_create_group(grouping["id"], "semifinals", "H")

    embed = hub.build_group_embed(members=[], stage="semifinals", label="H", grouping=grouping)

    assert "this group" in embed.description
    assert (
        hub._GROUP_NO_STAGE.format(record=hub._btn_words(hub.CD_BTN_RECORD))
        not in embed.description
    )


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
        hub._PICK_STAGE,
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
    assert {
        "Which Champion Duel?",
        hub._PICK_STAGE,
        "Which alliance?",
        "Page 1 / 2",
    } <= busiest
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


# ── `🏰 Your alliance` ────────────────────────────────────────────────────────
#
# Leadership's view of their own people, reading across every group. The tests
# that matter most here are the ones asserting the read is NOT a group filter:
# #536 shipped one that sits inside a single group, and treating that as having
# answered this question is the specific mistake `PLAN_champion_duel_ia.md`
# names.

KEV = {"discord_user_id": "111", "discord_name": "Kevin", "guild_id": "999"}
TYPES = ("Tank", "Missile", "Aircraft")
#: `push_to_bot.FALLBACK_RATIOS`. Squads are derived from THP for the ~97% of
#: the field with no sighting, so a fixture setting powers independently of THP
#: would grade the power gap off one column and the grid off another.
RATIOS = (0.338, 0.258, 0.238)


def _squads(registrant_id, thp, source="estimated"):
    for slot, squad_type, ratio in zip((1, 2, 3), TYPES, RATIOS):
        db.set_squad(
            registrant_id,
            slot,
            squad_type=squad_type,
            power=round(thp * ratio),
            source=source,
            actor=KEV,
        )


def _alliance_world(*, stage="semifinals"):
    """One Champion Duel over two warzones, with OGV spread across two groups.

    Deliberately mixed: two groups, an account nobody has placed, an account on
    another alliance, and an `[OGV]` on a warzone outside the event entirely.
    Every one of those is a case the read has to get right and none of them is
    visible from inside a single group.
    """
    grouping = db.create_grouping(["738", "900"], started_so_today_is(stage), origin="member")
    groups = {
        letter: db.get_or_create_group(grouping["id"], stage, letter) for letter in ("H", "C")
    }
    placed = {}
    for name, server, alliance, letter, rank in (
        ("Kestrel", "738", "OGV", "H", 1),
        ("Plover", "738", "OGV", "H", 3),
        ("Rival", "738", "Kite", "H", 2),
        ("Merlin", "0900", "OGV", "C", 2),
        ("Stranger", "900", "Kite", "C", 1),
    ):
        reg = db.upsert_registrant(name, server=server, alliance=alliance, thp=300_000_000)
        _squads(reg["id"], 300_000_000)
        db.set_placement(groups[letter]["id"], reg["id"], rank=rank)
        placed[name] = reg
    # Held, never placed. A leader needs to see these; a read keyed on group
    # membership would drop exactly the people they most need to notice.
    placed["Benched"] = db.upsert_registrant("Benched", server="738", alliance="OGV", thp=10)
    # The same three letters, another tournament. Tags are not unique across
    # the game, which is why the warzone scoping is doing real work.
    placed["Impostor"] = db.upsert_registrant("Impostor", server="1500", alliance="OGV", thp=10)
    return grouping, groups, placed


def _text_of(embed) -> str:
    """Everything the reader can see, as one string."""
    return "\n".join([embed.description or ""] + [f.value for f in embed.fields])


def _store_odds(group_id, *, stage="semifinals"):
    """A stored answer for one group, keyed by row position.

    Built rather than computed: this surface reads the store and never runs the
    model, so what is under test is the read-back rather than the engine. The
    ordering matches `get_group_scouting`, which is what `OddsRow.key` indexes.
    """
    members = db.get_group_scouting(group_id)
    rows = [
        odds_lib.OddsRow(
            name=m["display_name"],
            advance=0.9 - i * 0.1,
            win_group=0.5 - i * 0.05,
            points_mean=400 - i * 10,
            points_sd=12.0,
            key=str(i),
        )
        for i, m in enumerate(members)
    ]
    store_lib.store(group_id, members, odds_lib.GroupOdds(rows=rows), stage=stage)


def _two_full_groups():
    """Sixteen `[OGV]` accounts across two full semi-final groups.

    Deliberately unscouted -- `source='estimated'` is what ~97% of the roster
    carries -- so the reads take the wordiest branches and the widest grids,
    which is what makes this the right fixture for both the cost bound and the
    message-size one.
    """
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    for letter in ("A", "B"):
        group = db.get_or_create_group(grouping["id"], "semifinals", letter)
        for i in range(8):
            reg = db.upsert_registrant(
                f"{letter}{i}", server="738", alliance="OGV", thp=300_000_000
            )
            _squads(reg["id"], 300_000_000)
            db.set_placement(group["id"], reg["id"], rank=i + 1)
    return grouping, None


def _leader(registrant, user_id=1):
    db.claim_registrant(registrant["id"], user_id, discord_name="Leader", guild_id="999")
    return user_id


def test_the_alliance_is_read_across_groups_and_not_inside_one(cd_db):
    """The question is *where is my alliance*, and the answer spans the round.

    Kevin, 2026-08-24: *"If I'm looking for my alliance, I want to see everyone
    no matter what group they're in ... I would never go group first."* The
    filter that shipped in #536 answers "who from my alliance is in THIS
    group", so a read that could only see one group would be that filter again.
    """
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)

    assert state["state"] == "held"
    assert state["alliance"] == "OGV"
    names = [p["display_name"] for p in state["players"]]
    assert "Kestrel" in names and "Merlin" in names, "two groups, one alliance"
    assert "Rival" not in names and "Stranger" not in names


def test_whose_alliance_it_is_comes_from_the_claim(cd_db):
    """`guild_alliance_mappings` carries an `alliance_name`, but only for guilds
    linked to Map Manager, so it cannot be relied on. The claiming leader's own
    recorded account carries the tag, and that is a fact somebody read."""
    grouping, _groups, players = _alliance_world()
    other = hub.read_alliance(_leader(players["Rival"], user_id=2), grouping)

    assert other["alliance"] == "Kite"
    assert [p["display_name"] for p in other["players"]] == ["Stranger", "Rival"]


def test_an_account_outside_this_champion_duel_is_not_in_this_alliance(cd_db):
    """Alliance tags are three letters and are not unique across the game. The
    grouping's Participating Warzones are what say who is in this event."""
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)

    assert "Impostor" not in [p["display_name"] for p in state["players"]]


def test_a_padded_warzone_is_still_inside_its_own_champion_duel(cd_db):
    """`parse_warzones` canonicalizes a grouping through `str(int(...))` and a
    registrant added from a modal can hold `0900`. Compared as strings, that
    player is permanently outside their own event and nothing says so."""
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)

    merlin = next(p for p in state["players"] if p["display_name"] == "Merlin")
    assert merlin["server"] == "0900"
    assert db.warzone_key("0900") == db.warzone_key("900") == "900"


def test_the_listing_leads_on_whoever_got_deepest(cd_db):
    """The shape is the rounds, furthest first. For an alliance that rarely
    gets more than one player through, that section has one name in it and that
    name is the point -- which one flat sorted grid would bury."""
    grouping, groups, players = _alliance_world()
    quals = db.get_or_create_group(grouping["id"], "qualifiers", "D")
    early = db.upsert_registrant("Wader", server="738", alliance="OGV", thp=200_000_000)
    db.set_placement(quals["id"], early["id"], rank=40)

    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)
    embed = hub.build_alliance_embed(state, can_odds=False)
    named = [f.name for f in embed.fields if not f.name.startswith("🔒")]

    assert named[0] == db.STAGE_LABELS["semifinals"]
    assert named.index(db.STAGE_LABELS["qualifiers"]) < named.index(hub._ALLIANCE_UNPLACED)
    assert "Benched" in embed.fields[named.index(hub._ALLIANCE_UNPLACED)].value


def test_an_account_in_no_round_is_held_rather_than_missing(cd_db):
    """A blank round is a gap in our record, not a statement that somebody is
    not playing, so the field is named for the gap and carries the door.

    The door is its own field. Appended to the rows it was the first thing a
    1,024-character clamp cut, and it is the only exit this state has.
    """
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)
    embed = hub.build_alliance_embed(state, can_odds=False)

    names = [f.name for f in embed.fields]
    assert "Benched" in embed.fields[names.index(hub._ALLIANCE_UNPLACED)].value
    assert (
        hub._btn_words(hub.CD_BTN_RECORD)
        in embed.fields[names.index(hub._ALLIANCE_UNPLACED) + 1].value
    )


def test_nothing_here_tells_a_player_whether_they_are_still_in_it(cd_db):
    """Kevin struck exactly that from `🏅 Your standing` on 2026-08-25, and the
    plan says this session inherits it. The figures say how far somebody gets;
    a verdict would be narrating the game back at people playing it."""
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)
    embed = hub.build_alliance_embed(state, can_odds=True)

    text = " ".join([embed.description or ""] + [f.value for f in embed.fields]).lower()
    for narration in ("still in it", "long shot", "reward", "band", "you need"):
        assert narration not in text


def test_the_free_half_is_the_whole_screen_and_the_paid_half_is_the_tail(cd_db):
    """Free is what we recorded, paid is what we worked out. A free alliance
    sees every one of their people and where they are; what is locked is how
    far each one gets."""
    grouping, groups, players = _alliance_world()
    _store_odds(groups["H"]["id"])
    user_id = _leader(players["Kestrel"])

    free = hub.build_alliance_embed(hub.read_alliance(user_id, grouping), can_odds=False)
    paid = hub.build_alliance_embed(hub.read_alliance(user_id, grouping), can_odds=True)

    # Read off the round's own field rather than the whole embed: the upsell
    # names what it is selling, so it says "getting through" itself and an
    # assertion over everything on screen would pass on the wrong text.
    def rows(embed):
        return next(f.value for f in embed.fields if f.name == db.STAGE_LABELS["semifinals"])

    assert "Kestrel" in rows(free) and "Group H" in rows(free)
    assert "through" not in rows(free)
    assert f"🔒 {hub._ALLIANCE_LOCKED_FIELD}" in [f.name for f in free.fields]
    assert "through" in rows(paid)
    assert not [f for f in paid.fields if f.name.startswith("🔒")]


def test_a_stored_answer_is_read_back_by_position_rather_than_by_name(cd_db):
    """`OddsRow.key` is the index into the members list that produced the
    answer, which is what lets two players sharing a display name stay two
    players. This is also the bug #541 found in `_from_payload`, which was
    rebuilding rows without their key -- and this surface renders many."""
    grouping, groups, players = _alliance_world()
    _store_odds(groups["H"]["id"])
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)
    embed = hub.build_alliance_embed(state, can_odds=True)

    row = next(line for line in _text_of(embed).splitlines() if "Kestrel" in line)
    assert "through" in row and "win the group" in row


def test_a_group_with_nothing_stored_shows_the_rest_of_the_row(cd_db):
    """`missing` is never shown, and it must not cost the player their place in
    the listing: an answer computed against a different set of people is wrong
    rather than old, but where they are is still recorded and still free."""
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)
    embed = hub.build_alliance_embed(state, can_odds=True)

    assert "Kestrel" in _text_of(embed)
    assert "through" not in _text_of(embed)
    assert embed.footer.text is None


def test_a_round_with_no_model_is_never_scouted_for_odds(cd_db):
    """A qualifier group is 100 players, so `get_group_scouting` over one is
    four queries and a profile read for an answer that round has never had.
    `STAGES_WITH_A_MODEL` is read rather than a list kept in step by hand."""
    grouping = db.create_grouping(["738"], started_so_today_is("qualifiers"), origin="member")
    group = db.get_or_create_group(grouping["id"], "qualifiers", "D")
    lead = db.upsert_registrant("Kestrel", server="738", alliance="OGV", thp=300_000_000)
    db.set_placement(group["id"], lead["id"], rank=1)

    with patch.object(db, "get_group_scouting", side_effect=AssertionError("scouted")) as spy:
        state = hub.read_alliance(_leader(lead), grouping)
    assert spy.call_count == 0
    assert state["odds"] == {}


def test_the_two_alliance_reads_agree_about_what_one_alliance_is(cd_db):
    """The group filter and this read both answer "same alliance?", and a
    leader seeing twelve players in one and eleven in the other has no way to
    tell which is wrong. Both go through `db.alliance_tag`."""
    grouping, groups, players = _alliance_world()
    padded = db.upsert_registrant("Padded", server="738", alliance=" OGV ", thp=300_000_000)
    db.set_placement(groups["H"]["id"], padded["id"], rank=4)

    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)
    in_group = hub._by_alliance(db.get_group_members(groups["H"]["id"]), "OGV")

    assert "Padded" in [p["display_name"] for p in state["players"]]
    assert "Padded" in [m["display_name"] for m in in_group]


def test_a_leader_we_cannot_identify_is_offered_the_claim_rather_than_a_list(cd_db):
    """Whose alliance it is comes from the claim, so with no claim there is no
    question to answer -- and the exit is the flow that fixes it."""
    grouping, _groups, _players = _alliance_world()
    state = hub.read_alliance(4242, grouping)
    view = hub._AllianceView(
        user_id=4242, grouping=grouping, state=state, can_odds=True, can_intel=True, can_write=True
    )

    assert state["state"] == "unclaimed"
    assert hub._ALLIANCE_UNCLAIMED in hub.build_alliance_embed(state, can_odds=True).description
    assert [getattr(i, "label", None) for i in view.children] == [hub.CD_BTN_WHO_AM_I]


def test_a_claimed_account_with_no_tag_is_offered_the_field_that_is_missing(cd_db):
    """A blank tag is a gap in the record, not a claim that somebody is in no
    alliance -- `upsert_registrant` refuses to let a blank overwrite an
    imported value for exactly that reason."""
    grouping, _groups, _players = _alliance_world()
    bare = db.upsert_registrant("Tagless", server="738", thp=1)
    state = hub.read_alliance(_leader(bare, user_id=7), grouping)
    view = hub._AllianceView(
        user_id=7, grouping=grouping, state=state, can_odds=True, can_intel=True, can_write=True
    )

    assert state["state"] == "no_tag"
    assert "Tagless" in hub.build_alliance_embed(state, can_odds=True).description
    assert [getattr(i, "label", None) for i in view.children] == [hub.CD_BTN_EDIT_ME]


def test_the_no_tag_state_offers_the_control_that_is_about_the_reader(cd_db):
    """Kevin, 2026-08-26: the old exit here was `➕ Add a player`, and *"the
    label says you are adding a player when you are filling in one field about
    yourself."* Same modal, same write, opened on their own row.

    This state has no other door -- `_ALLIANCE_NO_TAG` lost the sentence that
    named the old control on the same day -- so `UX.md` principle 3 makes the
    button the exit rather than a convenience.
    """
    grouping, _groups, _players = _alliance_world()
    bare = db.upsert_registrant("Tagless", server="738", thp=1)
    state = hub.read_alliance(_leader(bare, user_id=7), grouping)
    view = hub._AllianceView(
        user_id=7, grouping=grouping, state=state, can_odds=True, can_intel=True, can_write=True
    )

    modal = hub._edit_me_modal(state["player"], can_write=True, grouping=grouping)

    assert [getattr(i, "label", None) for i in view.children] == [hub.CD_BTN_EDIT_ME]
    assert modal.name.default == "Tagless"
    assert modal.server.default == "738"


def test_the_edit_control_carries_every_field_we_hold_rather_than_the_two_it_needs(cd_db):
    """A member opening their own record to change one thing sees the other
    four as we hold them. A blank box beside a filled one reads as "we have
    nothing", which is the surface lying about its own record.
    """
    grouping, _groups, _players = _alliance_world()
    held = db.upsert_registrant(
        "Bracketless", server="900", alliance="ZZQ", thp=325_800_000, troop_level=8
    )
    state = hub.read_alliance(_leader(held, user_id=11), grouping)

    modal = hub._edit_me_modal(state["player"], can_write=True, grouping=grouping)
    chosen = [o.value for o in modal.troop_level.component.options if o.default]

    assert modal.alliance.default == "ZZQ"
    assert modal.thp.default == "325,800,000"
    assert chosen == ["8"]
    # The separator form round-trips exactly. `325.8M` re-enters as a number
    # rounded to one decimal place, so a member who changed nothing would have
    # their Total Hero Power moved by pressing save.
    assert hub.parse_power(modal.thp.default) == 325_800_000


def test_the_edit_control_is_titled_for_the_reader_not_for_adding_somebody(cd_db):
    """The modal a control opens says what the control said, which is the rule
    Kevin set on the claiming acknowledgements."""
    grouping, _groups, _players = _alliance_world()
    held = db.upsert_registrant("Nobodyhere", server="738", alliance="ZZQ", thp=1)
    state = hub.read_alliance(_leader(held, user_id=12), grouping)

    modal = hub._edit_me_modal(state["player"], can_write=True, grouping=grouping)

    assert modal.title == hub._EDIT_ME_TITLE
    assert modal.title != hub._AddPlayerModal(True).title


def test_a_prefilled_edit_does_not_leak_into_the_next_person_who_adds(cd_db):
    """`Modal._init_children` deepcopies each declared item onto the instance,
    and this is the test that says so out loud: a default set for one reader
    must not be sitting in the box for the next."""
    grouping, _groups, _players = _alliance_world()
    held = db.upsert_registrant("Zzqplayer", server="738", alliance="ZZQ", thp=1)
    state = hub.read_alliance(_leader(held, user_id=13), grouping)

    hub._edit_me_modal(state["player"], can_write=True, grouping=grouping)
    fresh = hub._AddPlayerModal(True, grouping=grouping)

    assert fresh.name.default is None
    assert fresh.alliance.default is None
    assert not [o for o in fresh.troop_level.component.options if o.default]


def test_an_empty_listing_is_about_the_reader_rather_than_their_alliance(cd_db):
    """Found by `/code-review`, and it needed the scoping rule to see.

    This read is scoped by the grouping's own warzones, so the reader is in
    their own result whenever they are in this Champion Duel -- which makes an
    empty list reachable ONLY when the reader is somewhere else. Answering that
    with "we do not hold anyone from OGV yet" over a `📥 Record a group` door
    is a claim about their alliance drawn from a fact about them, and the wrong
    thing to press either way. The fix is the claim, and it leads.
    """
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    db.create_grouping(["1500"], started_so_today_is("semifinals"), origin="member")
    lead = db.upsert_registrant("Kestrel", server="1500", alliance="OGV", thp=1)
    state = hub.read_alliance(_leader(lead), grouping, warzone="738")
    view = hub._AllianceView(
        user_id=1, grouping=grouping, state=state, can_odds=True, can_intel=True, can_write=True
    )

    assert state["players"] == []
    assert state["state"] == "elsewhere"
    description = hub.build_alliance_embed(state, can_odds=True).description
    assert "738" in description, "which Champion Duel this server is in"
    assert [getattr(i, "label", None) for i in view.children] == [
        hub.CD_BTN_WHO_AM_I,
        hub.CD_BTN_ADD,
        hub.CD_BTN_RECORD,
    ]
    assert view.children[0].style is discord.ButtonStyle.primary
    assert view.children[1].style is discord.ButtonStyle.secondary
    assert view.children[2].style is discord.ButtonStyle.secondary


def test_a_leader_who_switched_warzone_still_sees_the_alliance_they_left(cd_db):
    """`elsewhere` is a note, never a refusal. Their old alliance is still
    recorded and still theirs to look at, so the listing renders and the note
    only says which Champion Duel it is about -- the same call `read_standing`
    made about the same state."""
    grouping, _groups, players = _alliance_world()
    moved = db.upsert_registrant("Kestrel", server="1500", alliance="OGV", thp=1)
    state = hub.read_alliance(_leader(moved), grouping, warzone="738")

    assert state["state"] == "elsewhere"
    embed = hub.build_alliance_embed(state, can_odds=False)
    assert "Plover" in _text_of(embed), "their old alliance still renders"
    assert "738" in embed.description


def test_a_long_alliance_pages_at_twenty(cd_db):
    """Paging at 20 is this feature's fallback for any long listing, and it is
    applied to the flattened order rather than per round -- so a page is twenty
    players rather than twenty per section."""
    grouping = db.create_grouping(["738"], started_so_today_is("qualifiers"), origin="member")
    group = db.get_or_create_group(grouping["id"], "qualifiers", "D")
    for i in range(1, 26):
        reg = db.upsert_registrant(f"Bird{i:02d}", server="738", alliance="OGV", thp=1)
        db.set_placement(group["id"], reg["id"], rank=i)
    lead = db.upsert_registrant("Bird01", server="738")
    state = hub.read_alliance(_leader(lead), grouping)

    first = hub.build_alliance_embed(state, can_odds=False, page=0)
    second = hub.build_alliance_embed(state, can_odds=False, page=1)

    assert _text_of(first).count("**Bird") == hub.GROUP_PAGE_SIZE
    assert _text_of(second).count("**Bird") == 5
    assert "Showing 1 to 20 of 25 players" in first.footer.text
    view = hub._AllianceView(
        user_id=1, grouping=grouping, state=state, can_odds=False, can_intel=False, can_write=True
    )
    assert "Page 1 / 2" in [getattr(i, "label", None) for i in view.children]


def test_a_group_recorded_at_the_draw_shows_its_seeds_rather_than_dashes(cd_db):
    """Found by `/code-review`. `seed_rank` and `rank` are different facts, and
    between the draw and the standings every row has the first and none of the
    second -- which is the window "who will get into the semifinals" is read
    in. Reading only `rank` printed a dash for the whole alliance and sorted
    them alphabetically."""
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    group = db.get_or_create_group(grouping["id"], "semifinals", "H")
    for name, seed in (("Zephyr", 2), ("Albatross", 5), ("Kestrel", 1)):
        reg = db.upsert_registrant(name, server="738", alliance="OGV", thp=1)
        db.set_placement(group["id"], reg["id"], seed_rank=seed)

    state = hub.read_alliance(_leader(db.upsert_registrant("Kestrel", server="738")), grouping)
    embed = hub.build_alliance_embed(state, can_odds=False)
    listed = [line for line in _text_of(embed).splitlines() if "**" in line]

    assert [p["display_name"] for p in state["players"]] == ["Kestrel", "Zephyr", "Albatross"]
    assert listed[0].startswith("`1` **Kestrel** *(seed)*")
    assert "`-`" not in _text_of(embed)


def test_a_page_of_twenty_never_loses_its_tail_to_a_field_limit(cd_db):
    """Found by `/code-review`. A field value stops at 1,024 characters and
    twenty rows carrying two probabilities run to about 1,800, so the clamp
    every other call site uses would drop players the footer went on counting.
    """
    grouping = db.create_grouping(["738"], started_so_today_is("semifinals"), origin="member")
    group = db.get_or_create_group(grouping["id"], "semifinals", "H")
    for i in range(1, 21):
        reg = db.upsert_registrant(
            f"Longbirdname{i:02d}", server="738", alliance="OGV", thp=300_000_000
        )
        db.set_placement(group["id"], reg["id"], rank=i)
    _store_odds(group["id"])
    state = hub.read_alliance(
        _leader(db.upsert_registrant("Longbirdname01", server="738")), grouping
    )

    embed = hub.build_alliance_embed(state, can_odds=True)

    assert _text_of(embed).count("**Longbirdname") == 20, "every player on the page"
    assert all(len(f.value) <= hub.FIELD_LIMIT for f in embed.fields)
    assert len([f for f in embed.fields if "Longbirdname" in f.value]) > 1, "split, not clamped"


def test_a_free_guild_never_reads_the_store_at_all(cd_db):
    """Found by `/code-review`. The embed renders no odds without the
    entitlement, so reading them is a `get_group_scouting` per group for
    nothing -- and worse than nothing, because `lookup` stamps `last_viewed_at`
    and that is what orders the sweeper. A free guild paging this listing would
    push its own groups ahead of ones a paying guild is waiting on."""
    grouping, groups, players = _alliance_world()
    _store_odds(groups["H"]["id"])
    user_id = _leader(players["Kestrel"])

    with patch.object(db, "get_group_scouting", wraps=db.get_group_scouting) as spy:
        free = hub.read_alliance(user_id, grouping, with_odds=False)
    assert spy.call_count == 0
    assert free["odds"] == {}

    with patch.object(db, "get_group_scouting", wraps=db.get_group_scouting) as spy:
        paid = hub.read_alliance(user_id, grouping, with_odds=True)
    assert spy.call_count == 2, "one per group, not one per player"
    assert set(paid["odds"]) == {groups["H"]["id"]}, "group C has nothing stored"


def test_a_stale_answer_we_cannot_date_is_withheld_rather_than_shown_bare(cd_db):
    """Found by `/code-review`. The caveat is the CONDITION on showing a stale
    figure rather than a decoration over it, which is the rule
    `build_odds_embed` set. An unreadable timestamp costs the answer, not the
    line."""
    grouping, groups, players = _alliance_world()
    _store_odds(groups["H"]["id"])
    # Stale, and dated with something no one can parse. Both halves are needed:
    # a fresh answer carries no caveat, so only stale reaches the rule.
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE odds_runs SET fingerprint = 'moved', computed_at = 'whenever' WHERE group_id = ?",
            (groups["H"]["id"],),
        )

    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)
    embed = hub.build_alliance_embed(state, can_odds=True)

    assert groups["H"]["id"] not in state["odds"]
    assert "through" not in _text_of(embed)
    assert "Kestrel" in _text_of(embed), "and it costs them nothing else"


# ── Handing out the personal reads ───────────────────────────────────────────


def test_the_reads_cover_the_round_where_a_group_is_everyone_you_play(cd_db):
    """A semi-final group of 8 meets every other once, so the seven names
    beside somebody are exactly the seven they play. The qualifiers are a
    hundred who do not all meet and the knockouts are a bracket whose pairings
    nothing in the schema holds.

    The qualifier player here is the load-bearing half: without the round
    filter she gets a read against ninety-nine people she will never meet, and
    it is ninety-nine 1,296-cell grids to produce it.
    """
    grouping, _groups, players = _alliance_world()
    quals = db.get_or_create_group(grouping["id"], "qualifiers", "D")
    for i in range(4):
        early = db.upsert_registrant(f"Wader{i}", server="738", alliance="OGV", thp=200_000_000)
        _squads(early["id"], 200_000_000)
        db.set_placement(quals["id"], early["id"], rank=i + 1)

    state = hub.read_alliance(_leader(players["Kestrel"]), grouping, with_odds=False)
    result = hub.team_reads(state)

    assert "Wader0" in [p["display_name"] for p in state["players"]], "held, and listed"
    assert not [e for e in result["embeds"] if "Wader" in e.title], "and never read"

    assert db.ROUND_ROBIN_STAGES == ("semifinals",)
    assert result["stage"] == "semifinals"
    # Kestrel and Plover in group H, Merlin in group C. Benched has no round.
    assert [e.title for e in result["embeds"]] == [
        hub._READS_TITLE.format(player=name) for name in ("Kestrel", "Merlin", "Plover")
    ]


def test_one_read_carries_every_opponent_in_that_group(cd_db):
    """The mock is one player and their opponents, and the count comes off the
    group rather than off a schedule we do not hold."""
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping, with_odds=False)
    kestrel = next(e for e in hub.team_reads(state)["embeds"] if "Kestrel" in e.title)

    assert [f.name for f in kestrel.fields] == ["Rival", "Plover"]
    assert "2 opponents" in kestrel.description
    assert "remaining" not in kestrel.description.lower()


def test_a_read_never_prints_the_envelope_mean_as_the_odds(cd_db):
    """`champion_duel_intel` is explicit that weighting every configuration
    equally is the wrong prior and quoting the mean as an estimate would be a
    worse claim than the one it criticises. Where there is no recommendation to
    price, the range is the answer."""
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping, with_odds=False)
    kestrel = next(e for e in hub.team_reads(state)["embeds"] if "Kestrel" in e.title)
    block = next(f.value for f in kestrel.fields if f.name == "Rival")

    assert block.startswith("Runs from ")
    # The odds line names the player it is about, so its absence is what says
    # no single number was printed. Keyed off the constant rather than typed,
    # because Kevin owns this copy and it will move.
    assert hub._READ_ODDS.format(odds="", player="Kestrel").strip("* ") not in block


def test_a_settled_matchup_gives_the_figure_rather_than_a_range_of_one(cd_db):
    """Found by `/code-review`. `build_intel_embed` suppresses its range where
    the power gap decides the match, because there the envelope is "<1% to
    <1%" -- true, useless, and reading as a broken surface. Every line-up gives
    the same answer there, which is exactly when one number is honest."""
    grouping, groups, players = _alliance_world()
    giant = db.upsert_registrant("Giant", server="738", alliance="Kite", thp=900_000_000)
    _squads(giant["id"], 900_000_000)
    db.set_placement(groups["H"]["id"], giant["id"], rank=4)

    state = hub.read_alliance(_leader(players["Kestrel"]), grouping, with_odds=False)
    kestrel = next(e for e in hub.team_reads(state)["embeds"] if "Kestrel" in e.title)
    block = next(f.value for f in kestrel.fields if f.name == "Giant")

    assert "Runs from" not in block
    assert block.splitlines()[0].endswith("Kestrel wins")
    assert words.order_barely_matters(0.0).split(".")[0] in block


def test_an_opponent_we_cannot_build_stays_on_the_page(cd_db):
    """A leader handing this to a player needs to see that one of their
    meetings is unanswerable and which box fixes it. A list quietly six long in
    one group and seven in another says nothing at all."""
    grouping, groups, players = _alliance_world()
    blank = db.upsert_registrant("Ghost", server="738", alliance="Kite", thp=300_000_000)
    db.set_placement(groups["H"]["id"], blank["id"], rank=4)

    state = hub.read_alliance(_leader(players["Kestrel"]), grouping, with_odds=False)
    kestrel = next(e for e in hub.team_reads(state)["embeds"] if "Kestrel" in e.title)
    ghost = next(f.value for f in kestrel.fields if f.name == "Ghost")

    assert "Ghost" in [f.name for f in kestrel.fields]
    assert "1, 2, 3" in ghost


def test_a_player_we_cannot_build_is_said_once_rather_than_seven_times(cd_db):
    """Every one of their meetings would fail identically and for the same
    reason, so the read names the player instead of printing one fact seven
    times."""
    grouping, groups, players = _alliance_world()
    bare = db.upsert_registrant("Fledgling", server="738", alliance="OGV", thp=300_000_000)
    db.set_placement(groups["H"]["id"], bare["id"], rank=4)

    state = hub.read_alliance(_leader(players["Kestrel"]), grouping, with_odds=False)
    theirs = next(e for e in hub.team_reads(state)["embeds"] if "Fledgling" in e.title)

    assert theirs.fields == []
    assert "Fledgling" in theirs.description
    assert "1, 2, 3" in theirs.description


def test_the_batch_names_whoever_did_not_fit(cd_db):
    """One press is bounded so it cannot become several seconds of engine. A
    cut that is a count rather than names leaves a leader unable to go and get
    the people it dropped -- the same rule the alliance select's own cut line
    follows."""
    grouping, _big = _two_full_groups()
    lead = db.upsert_registrant("A0", server="738")
    state = hub.read_alliance(_leader(lead), grouping, with_odds=False)

    result = hub.team_reads(state, limit=3)

    assert result["shown"] == 3
    assert len(result["cut"]) == 13
    assert hub.READS_PER_PRESS == 10


def test_a_full_group_of_reads_fits_a_discord_message(cd_db):
    """The measurement `read_batches` is allowed to lean on.

    A single read must stay well inside the 6,000 characters Discord counts
    across a message, because an embed over that on its own cannot be sent at
    all and batching cannot save it. Seven opponents on the wordiest branches
    -- nothing scouted, so every block carries a range, the no-line-up sentence
    and the record-your-squads ask -- is the worst shape this produces.
    """
    grouping, _big = _two_full_groups()
    lead = db.upsert_registrant("A0", server="738")
    state = hub.read_alliance(_leader(lead), grouping, with_odds=False)
    biggest = max(hub.team_reads(state, limit=8)["embeds"], key=hub._embed_chars)

    assert len(biggest.fields) == 7, "a full semi-final group, less the player"
    assert hub._embed_chars(biggest) < hub.READS_CHAR_BUDGET / 2


def test_no_message_carries_more_embed_than_discord_counts(cd_db):
    """The cap people know about is ten embeds; the one that binds is 6,000
    characters across all of them. A read is about 2,500, so a team of five is
    several messages rather than one -- and nothing is dropped to make it fit.
    """
    grouping, _big = _two_full_groups()
    lead = db.upsert_registrant("A0", server="738")
    state = hub.read_alliance(_leader(lead), grouping, with_odds=False)
    embeds = hub.team_reads(state)["embeds"]

    batches = hub.read_batches(embeds)

    assert len(batches) > 1, "ten reads do not fit one message"
    assert sum(len(b) for b in batches) == len(embeds), "nothing dropped"
    for batch in batches:
        assert len(batch) <= hub.READS_EMBEDS_PER_MESSAGE
        assert sum(hub._embed_chars(e) for e in batch) <= hub.READS_CHAR_BUDGET


def test_one_oversized_read_is_sent_alone_rather_than_dropped(cd_db):
    """The reads are the deliverable. An embed nothing can batch with still
    goes out, because a batching rule that quietly lost one would be the worst
    version of the cut this surface refuses to make silently."""
    huge = discord.Embed(title="🎯 Kestrel", description="x" * (hub.READS_CHAR_BUDGET + 10))
    small = discord.Embed(title="🎯 Plover", description="y")

    batches = hub.read_batches([huge, small])

    assert [len(b) for b in batches] == [1, 1]
    assert batches[0][0] is huge


def test_a_read_says_it_is_one_match_rather_than_a_meeting(cd_db):
    """Every probability here is `best_of=1`: a meeting is three matches with a
    redeploy between them, and pricing the advice at Bo3 would charge a
    decision to a series the player gets to remake twice. It is also the figure
    the simulator measured as actively wrong."""
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping, with_odds=False)
    kestrel = next(e for e in hub.team_reads(state)["embeds"] if "Kestrel" in e.title)

    assert kestrel.footer.text == hub._READS_BASIS
    assert "one match" in kestrel.footer.text


def test_the_reads_are_private_until_somebody_posts_them(cd_db):
    """`PROPOSAL_champion_duel_ia.md` principle 5: an individual pulls it as an
    ephemeral and posting to a channel is a deliberate leadership act. Follows
    `SharePredictionView`, including holding the payload rather than rendering
    it twice."""
    embeds = [discord.Embed(title="🎯 Kestrel")]
    view = hub._ReadsShareView(embeds=embeds, user_id=1)

    assert [getattr(i, "label", None) for i in view.children] == [hub.CD_BTN_SHARE_READS]
    assert view.embeds is embeds


def test_the_reads_control_is_absent_where_no_round_carries_one(cd_db):
    """A padlock says "you could buy this"; absence says "this round does not
    work that way". The qualifiers are the second, and the two must not read as
    the same refusal."""
    grouping = db.create_grouping(["738"], started_so_today_is("qualifiers"), origin="member")
    group = db.get_or_create_group(grouping["id"], "qualifiers", "D")
    lead = db.upsert_registrant("Kestrel", server="738", alliance="OGV", thp=1)
    db.set_placement(group["id"], lead["id"], rank=1)
    state = hub.read_alliance(_leader(lead), grouping)

    labels = [
        getattr(i, "label", None)
        for i in hub._AllianceView(
            user_id=1,
            grouping=grouping,
            state=state,
            can_odds=True,
            can_intel=True,
            can_write=True,
        ).children
    ]
    assert not [label for label in labels if label and hub.CD_BTN_READS in label]


def test_the_reads_control_locks_rather_than_hides_where_it_could_run(cd_db):
    """`UX.md` principle 5. A free alliance in the semi-finals should see the
    shape of what it would be buying."""
    grouping, _groups, players = _alliance_world()
    state = hub.read_alliance(_leader(players["Kestrel"]), grouping)

    view = hub._AllianceView(
        user_id=1, grouping=grouping, state=state, can_odds=False, can_intel=False, can_write=True
    )
    locked = next(i for i in view.children if hub.CD_BTN_READS in (getattr(i, "label", None) or ""))

    assert locked.label.startswith("🔒")
    assert locked.disabled is True


def test_the_alliance_is_one_of_the_four_entries_and_the_group_is_not(cd_db):
    """Session 6 finished the split the plan describes.

    `🏅 Your group` sat beside `🏰 Your alliance` for exactly as long as
    the old control survived, which was until here: it is retired off the root
    and reached through the reader on `🏅 Your standing` instead.
    """
    grouping, _groups, _players = _alliance_world()
    view = hub.ChampionDuelHubView(
        user_id=1,
        is_admin=False,
        can_write=True,
        engine_ok=True,
        grouping=grouping,
        standing={"state": "held"},
    )
    front = [getattr(i, "label", None) for i in view.children if getattr(i, "row", None) == 0]

    assert hub.CD_BTN_ALLIANCE in front
    assert hub.CD_BTN_GROUP not in [getattr(i, "label", None) for i in view.children]


def test_the_alliance_control_is_absent_without_a_champion_duel(cd_db):
    """Same rule as `📥 Record a group` and `🔮 Today's picks`: with no
    grouping resolved there is no event to read an alliance out of, and the
    caller is being asked for their warzone instead."""
    view = hub.ChampionDuelHubView(
        user_id=1, is_admin=False, can_write=True, engine_ok=True, grouping=None
    )

    assert hub.CD_BTN_ALLIANCE not in [getattr(i, "label", None) for i in view.children]


def test_an_edit_is_acknowledged_as_an_update_not_as_a_refused_duplicate(cd_db):
    """Found by `/code-review`. The add flow's note fires on every edit — the
    member's own row always matches `find_registrants` — so it told somebody
    the write was declined when it landed."""
    grouping, _groups, _players = _alliance_world()
    held = db.upsert_registrant("Selfedit", server="738", alliance="ZZQ", thp=1)
    state = hub.read_alliance(_leader(held, user_id=21), grouping)
    modal = hub._edit_me_modal(state["player"], can_write=True, grouping=grouping)

    note = modal._note(state["player"], existing=True)

    assert note == hub._EDIT_ME_DONE.format(player=hub._label(state["player"]))
    assert "duplicate" not in note


def test_an_edit_that_lands_on_a_new_account_says_so_rather_than_orphaning_it(cd_db):
    """A registrant is keyed on (name, warzone) and cannot be renamed, and both
    are editable boxes. A member whose in-game name changed creates a second
    account and their claim stays on the first, so anything they entered lands
    on a row nobody holds. Found by `/code-review`."""
    grouping, _groups, _players = _alliance_world()
    held = db.upsert_registrant("Oldname", server="738", alliance="ZZQ", thp=1)
    state = hub.read_alliance(_leader(held, user_id=22), grouping)
    modal = hub._edit_me_modal(state["player"], can_write=True, grouping=grouping)

    renamed = db.upsert_registrant("Newname", server="738", alliance="ZZQ", thp=1)
    note = modal._note(renamed, existing=False)

    assert hub._label(renamed) in note
    assert hub._label(state["player"]) in note, "the account they still hold is named"
    assert db.get_claimed_registrant(22)["id"] == held["id"], "the claim did not follow"


def test_the_add_flow_keeps_its_own_two_notes(cd_db):
    """The edit path is the branch, not the replacement."""
    modal = hub._AddPlayerModal(True)
    player = db.upsert_registrant("Freshadd", server="738")

    assert "Added" in modal._note(player, existing=False)
    assert "duplicate" in modal._note(player, existing=True)

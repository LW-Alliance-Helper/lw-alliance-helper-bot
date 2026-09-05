"""`/champion_duel` hub — who sees which buttons, and what the flows behind
them do.

The modal and view bodies are exercised through their callbacks with a faked
interaction, following the repo's pattern of calling `task_name.coro(...)`
directly rather than standing up a gateway.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import champion_duel_claim as claim_lib
import champion_duel_db as db
import champion_duel_hub as hub
import champion_duel_odds as odds_lib
import champion_duel_store as store_lib

ADMIN_ID = 111
OUTSIDER_ID = 222

KEV = {"discord_user_id": str(ADMIN_ID), "discord_name": "Kevin", "guild_id": "999"}

# The game's clock, pinned. This file dates its groupings `2026-08-04` -- the
# real Champion Duel the feature was built against -- and several assertions
# read that date back as copy (`**8/4**`, `Started 8/4`), so the literal cannot
# simply be made relative. Left against the wall clock those dates age out from
# under the tests: on 2026-08-31 a 8/4 grouping passed `EVENT_DAYS`,
# `db.is_finished` turned True, and the hub started answering with
# `ChampionDuelFinishedView` -- which took `dev` red with nothing behind it.
# Pinning inside the window keeps every literal date meaning what it says.
CD_TODAY = date(2026, 8, 15)  # day 11 of 27 -- Qualifier Detail


@pytest.fixture(autouse=True)
def _server_clock(monkeypatch):
    """Both readings of "today", so the two never disagree mid-test."""
    monkeypatch.setattr(db, "_server_today", lambda: CD_TODAY)
    monkeypatch.setattr(hub, "_server_today", lambda: CD_TODAY)


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    path = str(tmp_path / "champion_duel.sqlite3")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.import_registrants(
        [
            {"name": "AlphaOne", "group": "M", "rank": 1, "server": "738"},
            {"name": "BetaTwo", "group": "M", "rank": 2, "server": "738"},
        ],
        stage="qualifiers",
    )
    return path


def _reg(name, server="738"):
    """Identity is (name, server), so scouting hangs off a registrant row rather
    than a bare name -- two servers can field the same name."""
    return db.resolve_registrant(name, server=server)["id"]


def _full_squads(registrant_id, powers=(40_000_000, 30_000_000, 20_000_000)):
    for slot, (squad_type, power) in enumerate(
        zip(("Tank", "Missile", "Aircraft"), powers), start=1
    ):
        db.set_squad(registrant_id, slot, squad_type=squad_type, power=power, actor=KEV)


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("CHAMPION_DUEL_ADMIN_IDS", str(ADMIN_ID))


def _interaction(user_id=ADMIN_ID):
    """A stand-in for discord.Interaction covering only what the hub touches."""
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.user.display_name = "Kevin"
    interaction.guild_id = 999
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.original_response = AsyncMock(return_value=MagicMock())
    interaction.edit_original_response = AsyncMock()
    interaction.channel.send = AsyncMock()
    return interaction


def _sent(interaction):
    """The text of the last followup, whether positional or keyword."""
    call = interaction.followup.send.call_args
    if call.args:
        return call.args[0]
    return call.kwargs.get("content") or ""


def _labels(view):
    return [item.label for item in view.children if hasattr(item, "label")]


# ── Who sees what ─────────────────────────────────────────────────────────────


def test_admin_buttons_are_absent_for_everyone_else():
    """Hidden rather than disabled, unlike the Premium rule.

    CHAMPION_DUEL_ADMIN_IDS is an operator env var, not a tier — a greyed-out
    'Revert an edit' would advertise a surface no amount of paying gets you.
    """
    view = hub.ChampionDuelHubView(
        user_id=OUTSIDER_ID, is_admin=False, can_write=True, engine_ok=True
    )
    labels = _labels(view)
    assert hub.CD_BTN_REVERT not in labels
    assert hub.CD_BTN_EDITS not in labels
    assert hub.CD_BTN_EXPORT not in labels
    assert hub.CD_BTN_PREDICT in labels


def test_admin_sees_the_operator_row():
    view = hub.ChampionDuelHubView(user_id=ADMIN_ID, is_admin=True, can_write=True, engine_ok=True)
    labels = _labels(view)
    assert hub.CD_BTN_EDITS in labels
    assert hub.CD_BTN_REVERT in labels
    assert hub.CD_BTN_EXPORT in labels


def test_write_buttons_lock_rather_than_vanish_on_the_free_tier():
    """Premium renders disabled, so the free tier sees the shape of the paid
    product (`notes/DESIGN.md`).

    Asserted where the control lives. Session 6 takes `➕ Add a player` off
    the root and leaves it at the miss, which is the surface the rule now has
    to hold on.
    """
    view = hub._MissView(can_write=False, user_id=OUTSIDER_ID, name="NobodyAtAll", server="738")
    locked = [b for b in view.children if hub.CD_BTN_ADD in (b.label or "")]
    assert locked, "the add button should still be on the miss"
    assert locked[0].disabled
    assert locked[0].label.startswith("🔒")


def test_the_write_actions_hang_off_a_player_not_the_hub():
    """Every flow used to open with "who?", so contributing three squad values
    and an order meant typing one name four times — and four chances at an
    ambiguous match. The hub now leads to a player; the writes act on them."""
    labels = [
        b.label
        for b in hub.ChampionDuelHubView(
            user_id=ADMIN_ID, is_admin=False, can_write=True, engine_ok=True
        ).children
    ]
    assert hub.CD_BTN_SQUADS not in labels
    assert hub.CD_BTN_ORDER not in labels
    assert hub.CD_BTN_FIND in labels
    # And nor is adding one, since session 6. It is at the miss that finding
    # produces, which is the point of need rather than a shelf on the root.
    assert hub.CD_BTN_ADD not in labels

    on_card = [
        b.label
        for b in hub.PlayerActionsView(
            player={"id": 1, "display_name": "AlphaOne", "server": "738"},
            user_id=ADMIN_ID,
            can_write=True,
        ).children
    ]
    # One squad control, not two. `✏️ Correct a squad` did the same job a slot
    # at a time and sat beside this one under the same glyph, which `DESIGN.md`
    # forbids across a choice set: two identical glyphs give the eye nothing to
    # navigate by, which is worse than bare.
    #
    # The claim is last and is not one of these: it says who the reader is
    # rather than what they saw, which is why it is never locked below.
    # `📖 Where to find these numbers` is on this card since session 6,
    # beside the two controls it explains and in front of neither.
    assert on_card == [
        hub.CD_BTN_SQUADS,
        hub.CD_BTN_ORDER,
        hub.CD_BTN_GUIDE,
        claim_lib.CLAIM_BTN,
    ]
    assert not hasattr(hub, "_SquadModal")


def test_the_player_card_locks_its_actions_on_the_free_tier():
    """Every *write* on the card, and only those.

    Two controls here are deliberately outside the rule. The claim, because
    contributing is a reading somebody made where a claim is a fact about the
    reader, so locking it would gate somebody out of their own record. And the
    capture guide, because it is documentation: somebody deciding whether the
    feature is worth paying for should be able to see what contributing
    involves, and withholding a picture of a game screen protects nothing.
    """
    view = hub.PlayerActionsView(
        player={"id": 1, "display_name": "AlphaOne", "server": "738"},
        user_id=OUTSIDER_ID,
        can_write=False,
    )
    free = {claim_lib.CLAIM_BTN, hub.CD_BTN_GUIDE}
    writes = [b for b in view.children if b.label not in free]
    assert writes, "the card lost its write actions"
    assert all(b.disabled for b in writes)
    assert all(b.label.startswith("🔒") for b in writes)


async def test_adding_a_player_marks_them_self_reported(cd_db):
    """The roster is who signed up, not everyone anyone will face. A row added
    from a sighting has to stay visibly distinguishable from an official
    import, exactly as squads.source does for estimates."""
    modal = hub._AddPlayerModal(can_write=True)
    modal.name._value = "Newcomer"
    modal.server._value = "1042"
    modal.alliance._value = "OGV"

    interaction = _interaction()
    await modal.on_submit(interaction)

    player = db.get_player("Newcomer", server="1042")
    assert player["origin"] == "self_reported"
    assert player["alliance"] == "OGV"
    assert player["added_by"] == str(ADMIN_ID)
    # Lands on the card with the write actions, not a bare confirmation.
    assert isinstance(interaction.followup.send.call_args.kwargs["view"], hub.PlayerActionsView)
    assert "Added" in _sent(interaction)


async def test_editing_your_own_name_renames_your_row_rather_than_adding_one(cd_db):
    """**End to end through `on_submit`, because the argument is the point.**
    `rename_id` is one keyword and deleting it restores the bug this fixes --
    a member whose in-game name changed got a second account, their claim
    stayed on the first, and everything they had just typed landed on a row
    nobody held.

    Kevin, 2026-08-30: *"if someone is EDITING their own information, it needs
    to update them."*"""
    held = db.get_player("AlphaOne", server="738")
    db.claim_registrant(held["id"], ADMIN_ID)

    modal = hub._edit_me_modal(held, can_write=True, grouping=None)
    modal.name._value = "AlphaOneRenamed"
    modal.server._value = "738"

    interaction = _interaction()
    await modal.on_submit(interaction)

    after = db.get_registrant(held["id"])
    assert after["display_name"] == "AlphaOneRenamed", "the row they own was renamed"
    assert db.get_player("AlphaOne", server="738") is None, "no row left under the old name"
    assert db.get_claimed_registrant(ADMIN_ID)["id"] == held["id"], "the claim followed"
    assert "Updated" in _sent(interaction)


async def test_a_rename_onto_somebody_elses_account_is_refused_and_writes_nothing(cd_db):
    """The one case a rename cannot be: the name and warzone submitted are
    already a DIFFERENT registrant. Two real records with their own squads and
    history, so choosing between them is the member's call -- and **nothing is
    written**, which is the assertion with teeth."""
    held = db.get_player("AlphaOne", server="738")
    other = db.get_player("BetaTwo", server="738")
    db.claim_registrant(held["id"], ADMIN_ID)

    modal = hub._edit_me_modal(held, can_write=True, grouping=None)
    modal.name._value = "BetaTwo"
    modal.server._value = "738"

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert db.get_registrant(held["id"])["display_name"] == "AlphaOne", "not renamed"
    assert db.get_registrant(other["id"])["display_name"] == "BetaTwo", "not overwritten"
    assert db.get_claimed_registrant(ADMIN_ID)["id"] == held["id"], "the claim did not move"
    sent = _sent(interaction)
    assert "BetaTwo" in sent, "the other account is named"
    assert claim_lib.CLAIM_BTN in sent, "and the way out is on the message"


async def test_a_claim_released_while_the_modal_sat_open_writes_nothing(cd_db):
    """**The snapshot window, and `/code-review` found it.** `_open_edit_me`
    reads the claim fresh, and then the modal sits open for as long as the
    member leaves it -- during which `ClaimResultView` can release or move the
    claim from another message. Without a re-read, `rename_id` renames, and
    clears fields on, an account they no longer hold.

    `champion_duel_claim._pressed` settles the identical window the identical
    way, which is why this needs no new copy."""
    held = db.get_player("AlphaOne", server="738")
    db.claim_registrant(held["id"], ADMIN_ID)
    modal = hub._edit_me_modal(held, can_write=True, grouping=None)
    modal.name._value = "AlphaOneRenamed"
    modal.server._value = "738"

    db.release_claim(ADMIN_ID)  # from another message, while the modal is open

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert db.get_registrant(held["id"])["display_name"] == "AlphaOne", "nothing was written"
    assert _sent(interaction) == claim_lib.CLAIM_NOT_LINKED


def test_a_group_letter_from_another_grouping_is_qualified_on_the_card(cd_db):
    """A letter is meaningful inside a grouping and nowhere else, so a bare
    "Group D" on a player from another draw reads as a claim it is not."""
    theirs = db.create_grouping(["1500", "1501"], "2026-08-04", origin="member")
    db.import_registrants([{"name": "Stranger", "server": "1500"}], grouping_id=theirs["id"])
    db.set_stage(
        db.resolve_registrant("Stranger", "1500")["id"],
        "semifinals",
        grp="D",
        grouping_id=theirs["id"],
    )
    mine = db.find_grouping_by_warzone("738")
    player = db.get_player("Stranger", server="1500")

    rounds = next(
        f.value
        for f in hub.build_player_embed(player, None, grouping=mine).fields
        if f.name == hub.FIELD_STAGES
    )
    assert "Group D (not your Champion Duel)" in rounds

    # Inside the caller's own grouping it stays bare, because there it is exact.
    theirs_view = next(
        f.value
        for f in hub.build_player_embed(player, None, grouping=theirs).fields
        if f.name == hub.FIELD_STAGES
    )
    assert "Group D" in theirs_view and "different Champion Duel" not in theirs_view


async def test_the_edit_flow_empties_a_box_the_member_cleared(cd_db):
    """Kevin settled on 2026-08-29 that `✏️ Edit my information` may clear a
    field. The modal opened with the tag we hold in the box, so deleting it is
    a member correcting a record they can see -- and this write used to be a
    silent no-op, which told them it had saved."""
    db.upsert_registrant("AlphaOne", server="738", alliance="OGV", thp=5, origin="imported")
    held = db.get_player("AlphaOne", server="738")
    # The claim is not decoration here. `_open_edit_me` refuses without one, so
    # a modal opened on an unclaimed row is a state the surface cannot reach --
    # and `on_submit` now re-reads the claim rather than trusting the snapshot
    # it opened on, so the test has to hold the account it is editing.
    db.claim_registrant(held["id"], ADMIN_ID)
    modal = hub._edit_me_modal(held, can_write=True, grouping=None)
    modal.name._value = "AlphaOne"
    modal.server._value = "738"
    modal.alliance._value = ""
    modal.thp._value = ""

    await modal.on_submit(_interaction())

    after = db.get_player("AlphaOne", server="738")
    assert after["alliance"] is None
    assert after["thp"] is None
    assert after["origin"] == "imported", "emptying a field is not a downgrade"


async def test_an_untouched_troop_level_survives_an_edit(cd_db):
    """**The one field an emptied box does NOT clear, and deliberately.** The
    other two are text boxes, which always submit their contents. This is a
    select, and an empty `values` means either "deselected" or "Discord did not
    echo the default back" -- the same payload for two different intentions.
    Guessing wrong wipes the troop level of every member who opens this screen
    to change something else, so it keeps the behaviour it had.

    Raised by `/code-review` as the one thing it could not settle. It cannot be
    settled from the payload, so this pins the safe answer instead."""
    db.upsert_registrant("AlphaOne", server="738", troop_level=9, origin="imported")
    held = db.get_player("AlphaOne", server="738")
    modal = hub._edit_me_modal(held, can_write=True, grouping=None)
    modal.name._value = "AlphaOne"
    modal.server._value = "738"
    modal.alliance._value = ""

    await modal.on_submit(_interaction())

    assert db.get_player("AlphaOne", server="738")["troop_level"] == 9


async def test_the_add_flow_still_leaves_an_imported_value_alone(cd_db):
    """**Unchanged, and this is the half `/code-review` protected.** Somebody
    adding a player they just met has no idea what we already hold, so a box
    they left alone is an omission rather than a statement."""
    db.upsert_registrant("AlphaOne", server="738", alliance="OGV", origin="imported")
    modal = hub._AddPlayerModal(can_write=True)
    modal.name._value = "AlphaOne"
    modal.server._value = "738"
    modal.alliance._value = ""

    await modal.on_submit(_interaction())

    assert db.get_player("AlphaOne", server="738")["alliance"] == "OGV"


async def test_an_edit_that_lands_on_another_account_leaves_its_fields_alone(cd_db):
    """Name and warzone are both editable, so a submission can land on a
    registrant this modal never showed anybody. The boxes were filled from
    somebody else's row, so a cleared one says nothing about this account."""
    db.upsert_registrant("BetaTwo", server="738", alliance="OGV", origin="imported")
    mine = db.get_player("AlphaOne", server="738")
    modal = hub._edit_me_modal(mine, can_write=True, grouping=None)
    modal.name._value = "BetaTwo"
    modal.server._value = "738"
    modal.alliance._value = ""

    await modal.on_submit(_interaction())

    assert db.get_player("BetaTwo", server="738")["alliance"] == "OGV"


async def test_adding_someone_we_already_have_opens_them(cd_db):
    """Not an error and not a duplicate — identity is (name, server), so this
    is the same person. Saying so beats a refusal the contributor has to
    interpret."""
    modal = hub._AddPlayerModal(can_write=True)
    modal.name._value = "AlphaOne"
    modal.server._value = "738"
    modal.alliance._value = ""

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert "already here" in _sent(interaction)
    # And the import origin survives being re-entered by hand.
    assert db.get_player("AlphaOne", server="738")["origin"] == "imported"


async def test_adding_without_a_server_is_refused(cd_db):
    """A self-reported player with no server is a row nobody can match against
    later, because identity is the two together."""
    modal = hub._AddPlayerModal(can_write=True)
    modal.name._value = "Nameless"
    modal.server._value = ""
    modal.alliance._value = ""

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert "name and a server" in _sent(interaction)
    assert db.find_registrants("Nameless") == []


async def test_a_typo_gets_a_did_you_mean(cd_db):
    """Suggesting is not resolving. normalize_name refuses to fuzzy-match
    because guessing which of two similar names a sighting belongs to is
    unrecoverable — but the person typing can tell instantly, and "no
    registrant matches" tells them nothing about which mistake they made."""
    modal = hub._FindPlayerModal(can_write=True)
    modal.name._value = "AlphaOn"  # truncated
    modal.server._value = "738"
    interaction = _interaction()
    await modal.on_submit(interaction)

    msg = _sent(interaction)
    assert "Did you mean" in msg and "AlphaOne" in msg and "738" in msg


def test_suggestions_catch_both_shapes_of_miss(cd_db):
    """A truncation is a prefix of the real name; a partial is a substring,
    often not at the start. Sequence similarity alone ranks the second badly,
    so both are scored explicitly."""
    db.import_registrants(
        [{"name": "Ultra Zaddy", "group": "M", "rank": 9, "server": "677"}], stage="qualifiers"
    )

    truncated = [c["display_name"] for c in db.suggest_registrants("AlphaOn")]
    assert "AlphaOne" in truncated

    partial = [c["display_name"] for c in db.suggest_registrants("zaddy")]
    assert "Ultra Zaddy" in partial


def test_a_wrong_server_still_finds_the_player(cd_db):
    """Getting the server wrong is at least as likely as getting the name
    wrong, so a miss falls back to every server rather than hiding them."""
    hits = db.suggest_registrants("AlphaOne", server="999")
    assert [c["display_name"] for c in hits] == ["AlphaOne"]


def test_nothing_close_suggests_nothing(cd_db):
    """No near match has to stay empty. A list of unrelated names reads as the
    bot having found something, which is worse than saying it hasn't."""
    assert db.suggest_registrants("qqqqqqzzzz") == []


async def test_a_missing_player_carries_the_add_button_not_a_route_back(cd_db):
    """A name we don't have is usually a real player we haven't met, so the
    miss carries its own exit. The exit is a button on this message: naming a
    button the user then has to go and find is only half of "every dead end
    carries its exit"."""
    modal = hub._FindPlayerModal(can_write=True)
    modal.name._value = "NobodyAtAll"
    modal.server._value = "738"
    interaction = _interaction()
    await modal.on_submit(interaction)

    view = interaction.followup.send.await_args.kwargs["view"]
    # And the guide beside it since session 6: the modal this opens asks for
    # three squad powers and their types, and the guide is where to read them.
    assert [b.label for b in view.children] == [hub.CD_BTN_ADD, hub.CD_BTN_GUIDE]
    assert not view.children[0].disabled


async def test_the_miss_carries_what_they_typed_into_the_add_modal(cd_db):
    """Someone who spelled it right and simply met a player we never imported
    should not type the name a second time."""
    view = hub._MissView(can_write=True, user_id=ADMIN_ID, name="NobodyAtAll", server="999")
    inter = _interaction()
    await view._on_add(inter)

    sent = inter.response.send_modal.await_args.args[0]
    assert sent.name.default == "NobodyAtAll"
    assert sent.server.default == "999"


async def test_a_locked_miss_still_shows_the_add_button(cd_db):
    """Premium controls render disabled rather than vanishing (DESIGN.md), so
    the free tier sees the shape of what contributing would give them."""
    view = hub._MissView(can_write=False, user_id=ADMIN_ID, name="Nobody", server=None)
    assert view.children[0].disabled
    assert "🔒" in view.children[0].label


async def test_the_squad_modal_does_not_ask_who_again(cd_db):
    """The player came from the card, so there is no second chance to mistype
    a name and no ambiguous match to resolve mid-flow."""
    player = db.get_player("AlphaOne", server="738")
    modal = hub._SquadDetailModal(player=player)
    labels = {getattr(i, "label", None) or getattr(i, "text", None) for i in modal.children}
    assert "Player name" not in labels
    assert {"Squad 1 power", "Squad 2 power", "Squad 3 power"} <= labels


async def test_recording_an_order_opens_the_picker_with_no_modal(cd_db):
    """The order flow asks one question. The modal that used to precede it
    collected who the player faced, which is not an input to anything: a
    prediction samples the order, not the opponent."""
    assert not hasattr(hub, "_OrderModal")

    player = db.get_player("AlphaOne", server="738")
    view = hub.PlayerActionsView(player=player, user_id=ADMIN_ID, can_write=True)
    inter = _interaction(user_id=ADMIN_ID)
    await view._on_order(inter)

    inter.response.send_modal.assert_not_awaited()
    sent = inter.followup.send.await_args
    assert isinstance(sent.kwargs["view"], hub._OrderSelectView)
    # Says what the data is for. Someone pressing this has no reason to know
    # why deployment order is the thing worth their time.
    assert "sharpens" in sent.args[0]


async def test_a_squad_correction_applies_to_the_card_player(cd_db):
    player = db.get_player("AlphaOne", server="738")
    modal = _detail_modal(player, powers=("84.6M", "", ""), types=0)

    interaction = _interaction()
    await modal.on_submit(interaction)

    squad = db.get_player("AlphaOne", server="738", include_scouting=True)["squads"][0]
    assert squad["squad_type"] == "Tank" and squad["power"] == 84_600_000
    assert "Recorded" in _sent(interaction)


# ── "Which of these is right?" ────────────────────────────────────────────────
#
# Kevin's design: surface what we already hold when somebody enters something
# different, show them the two pieces, and ask.
#
# Almost every test here is about NOT asking. Two people entering the same
# correct value is the common case, and a surface that questions every
# re-entry is one nobody enters anything into twice.

SCOUT = {"discord_user_id": "333", "discord_name": "Someone else", "guild_id": "999"}
SCOUT_ID = 333


def _order_starting_with(squad_type):
    """The dropdown index whose first box is this type, or None for no answer.

    The control is a whole-lineup answer, so a test that only cares about
    slot 1 still has to pick one of the six.
    """
    if not squad_type:
        return None
    return next(i for i, order in enumerate(hub._TYPE_ORDERS) if order[0] == squad_type)


async def _submit_correction(player, *, squad_type="", power="", user_id=SCOUT_ID):
    """One box entered through the squad screen, as a scout who is not KEV."""
    modal = _detail_modal(player, powers=(power, "", ""), types=_order_starting_with(squad_type))
    interaction = _interaction(user_id=user_id)
    await modal.on_submit(interaction)
    return interaction


def _view_of(interaction):
    return (interaction.followup.send.call_args.kwargs or {}).get("view")


def _first_sent(interaction):
    """The first followup, where `_sent` gives the last."""
    call = interaction.followup.send.call_args_list[0]
    if call.args:
        return call.args[0]
    return call.kwargs.get("content") or ""


def _slot_of(name, slot, server="738"):
    player = db.get_player(name, server=server, include_scouting=True)
    return next(s for s in player["squads"] if s["slot"] == slot)


def _squads_of(name, server="738"):
    return db.get_player(name, server=server, include_scouting=True)["squads"]


def _detail_modal(player, *, powers=("", "", ""), types=None, mixed=""):
    """`_SquadDetailModal` with its five components filled in.

    `types` is an index into `hub._TYPE_ORDERS`, or `hub._TYPE_ORDER_OTHER`,
    or None for "not answered".
    """
    modal = hub._SquadDetailModal(player=player)
    for box, value in zip((modal.squad1, modal.squad2, modal.squad3), powers):
        box._value = value
    modal.types.component._values = (
        [] if types is None else [types if isinstance(types, str) else str(types)]
    )
    modal.mixed.component._value = mixed
    return modal


async def test_agreeing_with_what_we_hold_passes_without_a_word(cd_db):
    """The common case by a distance. Two scouts reading the same panel must
    not be made to arbitrate between two identical numbers."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=84_600_000, actor=KEV, source="observed")

    interaction = await _submit_correction(
        db.get_player("AlphaOne", server="738"), squad_type="Tank", power="84.6M"
    )

    assert _view_of(interaction) is None
    assert db.list_disagreements()["total"] == 0


async def test_the_two_notations_for_one_number_do_not_contradict(cd_db):
    """`parse_power` accepts `64.6M` and `64,600,000` as the same reading, and
    in binary floating point they are not: 64.6 * 1e6 lands a fraction above.
    Compared exactly, the member is shown two numbers that render identically
    and asked which is right."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, power=64_600_000, actor=KEV, source="observed")

    interaction = await _submit_correction(db.get_player("AlphaOne", server="738"), power="64.6M")

    assert _view_of(interaction) is None
    assert db.list_disagreements()["total"] == 0


async def test_a_field_nobody_has_answered_is_not_a_disagreement(cd_db):
    """Nothing to contradict. The first thing anybody says about a field is
    new information, however much else we hold about that squad."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", actor=KEV, source="observed")

    interaction = await _submit_correction(db.get_player("AlphaOne", server="738"), power="84.6M")

    assert _view_of(interaction) is None
    assert _slot_of("AlphaOne", 1)["power"] == 84_600_000


async def test_an_estimate_is_never_worth_arbitrating(cd_db):
    """`push_to_bot` writes an estimate for nearly the whole field, so treating
    one as something we hold would put this question in front of the very first
    real reading of almost every player. The bot's own guess giving way to
    somebody reading the screen is the system working."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Missile", power=13_500_000, actor=KEV, source="estimated")

    interaction = await _submit_correction(
        db.get_player("AlphaOne", server="738"), squad_type="Tank", power="84.6M"
    )

    assert _view_of(interaction) is None
    assert _slot_of("AlphaOne", 1)["power"] == 84_600_000
    assert db.list_disagreements()["total"] == 0


async def test_correcting_your_own_entry_is_not_a_disagreement(cd_db):
    """Somebody's newer reading of their own squad is simply better. Asking
    them to arbitrate against themselves is noise."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=84_600_000, actor=KEV, source="observed")

    interaction = await _submit_correction(
        db.get_player("AlphaOne", server="738"), power="90.1M", user_id=ADMIN_ID
    )

    assert _view_of(interaction) is None
    assert _slot_of("AlphaOne", 1)["power"] == 90_100_000


async def test_a_real_contradiction_puts_both_pieces_up(cd_db):
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=84_600_000, actor=KEV, source="observed")

    interaction = await _submit_correction(db.get_player("AlphaOne", server="738"), power="90.1M")

    view = _view_of(interaction)
    assert isinstance(view, hub._DisagreementView)
    embed = interaction.followup.send.call_args.kwargs["embed"]
    body = " ".join(f"{f.name} {f.value}" for f in embed.fields)
    assert "84,600,000" in body and "90,100,000" in body
    # Nothing is written until somebody answers.
    assert _slot_of("AlphaOne", 1)["power"] == 84_600_000


async def test_the_two_buttons_are_bare(cd_db):
    """They differ by which value is right, which is a parameter rather than a
    kind, and `DESIGN.md` sends parameter sets out without glyphs rather than
    repeating one across the pair."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=84_600_000, actor=KEV, source="observed")
    interaction = await _submit_correction(db.get_player("AlphaOne", server="738"), power="90.1M")

    view = _view_of(interaction)

    labels = _labels(view)
    assert len(labels) == 2
    assert all(label[0].isalpha() for label in labels), labels
    # Neither is the recommended one. The bot has no view on which of two
    # people read the screen correctly.
    assert all(item.style is discord.ButtonStyle.secondary for item in view.children)


async def test_keeping_what_we_hold_changes_nothing_and_is_recorded(cd_db):
    """The half `edits` cannot carry. Nothing changed, and that a stored value
    was challenged and survived is the only thing that says so."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=84_600_000, actor=KEV, source="observed")
    interaction = await _submit_correction(db.get_player("AlphaOne", server="738"), power="90.1M")
    view = _view_of(interaction)
    before = db.list_edits()["total"]

    press = _interaction(user_id=SCOUT_ID)
    await view.keep.callback(press)

    assert _slot_of("AlphaOne", 1)["power"] == 84_600_000
    assert db.list_edits()["total"] == before, "keeping a value is not an edit"
    logged = db.list_disagreements()
    assert logged["total"] == 1
    row = logged["disagreements"][0]
    assert row["field"] == "power"
    assert row["held_value"] == "84600000.0" and row["offered_value"] == "90100000.0"
    assert row["chose"] == "held" and row["edit_id"] is None
    assert row["actor_discord_id"] == SCOUT["discord_user_id"]


async def test_taking_the_new_value_writes_it_and_links_the_edit(cd_db):
    """So `⏪ Revert an edit` and this history tell one story rather than two."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=84_600_000, actor=KEV, source="observed")
    interaction = await _submit_correction(db.get_player("AlphaOne", server="738"), power="90.1M")
    view = _view_of(interaction)

    await view.use_mine.callback(_interaction(user_id=SCOUT_ID))

    assert _slot_of("AlphaOne", 1)["power"] == 90_100_000
    row = db.list_disagreements()["disagreements"][0]
    assert row["chose"] == "offered"
    edit = next(e for e in db.list_edits()["edits"] if e["id"] == row["edit_id"])
    assert edit["field"] == "power" and edit["new_value"] == "90100000.0"


async def test_a_field_nobody_disputed_lands_before_the_question_is_asked(cd_db):
    """Somebody who reads a type we did not have and a power we did should not
    lose the type because they said our power was the right one, and should not
    lose it by being interrupted before answering either. There is nothing to
    arbitrate about a value nobody offered a different one for."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, power=84_600_000, actor=KEV, source="observed")

    interaction = await _submit_correction(
        db.get_player("AlphaOne", server="738"), squad_type="Aircraft", power="90.1M"
    )

    squad = _slot_of("AlphaOne", 1)
    assert squad["squad_type"] == "Aircraft", "saved without waiting for an answer"
    assert squad["power"] == 84_600_000, "and the disputed one is still ours until they answer"

    await _view_of(interaction).keep.callback(_interaction(user_id=SCOUT_ID))

    assert _slot_of("AlphaOne", 1)["power"] == 84_600_000


async def test_an_unanswered_question_retires_its_own_buttons(cd_db, monkeypatch):
    """A live-looking button on a dead view is a bug, not cosmetics: the member
    presses it, gets "Interaction failed", and never learns the question went
    unanswered."""
    import wizard_registry

    expired = AsyncMock()
    monkeypatch.setattr(wizard_registry, "expire_view_message", expired)
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=84_600_000, actor=KEV, source="observed")
    interaction = await _submit_correction(db.get_player("AlphaOne", server="738"), power="90.1M")

    await _view_of(interaction).on_timeout()

    expired.assert_awaited_once()


async def test_only_the_person_asked_can_answer(cd_db):
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=84_600_000, actor=KEV, source="observed")
    interaction = await _submit_correction(db.get_player("AlphaOne", server="738"), power="90.1M")
    view = _view_of(interaction)

    intruder = _interaction(user_id=OUTSIDER_ID)
    assert await view.interaction_check(intruder) is False
    assert _slot_of("AlphaOne", 1)["power"] == 84_600_000


async def test_recording_three_boxes_asks_once_not_three_times(cd_db):
    """A member filling in the whole lineup answered one thing. Asking per
    field is what turns a correction into an interrogation."""
    rid = _reg("AlphaOne")
    _full_squads(rid)
    modal = hub._SquadDetailModal(player=db.get_player("AlphaOne", server="738"))
    modal.squad1._value = "50M"
    modal.squad2._value = "40M"
    modal.squad3._value = "30M"
    modal.types.component._values = []
    modal.mixed.component._value = ""

    interaction = _interaction(user_id=SCOUT_ID)
    await modal.on_submit(interaction)

    assert interaction.followup.send.await_count == 1
    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert len(embed.fields) == 3, "three contradicted powers, still one question"


# ── "Other": two of the same type ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("Tank, Tank, Aircraft", ("Tank", "Tank", "Aircraft")),
        ("tank tank air", ("Tank", "Tank", "Aircraft")),
        ("T/M/A", ("Tank", "Missile", "Aircraft")),
        ("aircraft - aircraft - missile", ("Aircraft", "Aircraft", "Missile")),
    ],
)
def test_a_typed_squad_order_is_read_forgivingly(typed, expected):
    """This is the path for somebody already told the dropdown does not fit
    them. The three names share no first letter, which is what makes a single
    character enough to disambiguate."""
    assert hub._parse_type_order(typed) == expected


@pytest.mark.parametrize(
    "typed", ["Tank, Aircraft", "Tank, Tank, Tank, Tank", "", "blue red green"]
)
def test_an_unreadable_squad_order_is_refused_not_guessed(typed):
    """A wrong type is a wrong counter matchup on every prediction that player
    ever appears in, where a wrong power is a number slightly out."""
    assert hub._parse_type_order(typed) is None


async def test_other_saves_the_powers_then_asks_for_the_types(cd_db):
    """The powers are written before the question, so somebody who picks Other
    and then gets pulled away keeps everything except the types."""
    modal = _detail_modal(
        player=db.get_player("AlphaOne", server="738"),
        powers=("94.2M", "82M", "78M"),
        types=hub._TYPE_ORDER_OTHER,
    )
    interaction = _interaction()
    interaction.client.wait_for = AsyncMock(side_effect=asyncio.TimeoutError)

    await modal.on_submit(interaction)

    assert [s["power"] for s in _squads_of("AlphaOne")] == [94_200_000, 82_000_000, 78_000_000]
    assert all(s["squad_type"] is None for s in _squads_of("AlphaOne"))
    assert "What are their three squad types?" in _first_sent(interaction)


async def test_the_typed_order_lands_on_the_boxes(cd_db):
    """No second modal. The question is asked the way every free-text step in
    the setup wizards is asked, because Discord will not answer a modal with a
    modal and a button in between costs a press to answer one question."""
    modal = _detail_modal(
        player=db.get_player("AlphaOne", server="738"),
        powers=("94.2M", "82M", "78M"),
        types=hub._TYPE_ORDER_OTHER,
    )
    interaction = _interaction()
    reply = MagicMock()
    reply.content = "Tank, Tank, Aircraft"
    reply.delete = AsyncMock()
    interaction.client.wait_for = AsyncMock(return_value=reply)

    await modal.on_submit(interaction)

    assert [s["squad_type"] for s in _squads_of("AlphaOne")] == ["Tank", "Tank", "Aircraft"]
    # Their reply is a line with no visible prompt above it, since the question
    # was ephemeral and nobody can answer ephemerally.
    reply.delete.assert_awaited_once()


async def test_a_missing_delete_permission_does_not_lose_the_save(cd_db):
    """Tidying the channel needs Manage Messages, and not having it is not a
    reason to fail a save that has already happened."""
    modal = _detail_modal(
        player=db.get_player("AlphaOne", server="738"),
        powers=("94.2M", "", ""),
        types=hub._TYPE_ORDER_OTHER,
    )
    interaction = _interaction()
    reply = MagicMock()
    reply.content = "Tank, Tank, Aircraft"
    reply.delete = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    interaction.client.wait_for = AsyncMock(return_value=reply)

    await modal.on_submit(interaction)

    assert [s["squad_type"] for s in _squads_of("AlphaOne")] == ["Tank", "Tank", "Aircraft"]


async def test_an_unreadable_answer_costs_one_step_not_the_flow(cd_db):
    """`UX.md`: a validation failure gets a retry with an example before
    bailing out, and the bail-out names the route back in."""
    modal = _detail_modal(
        player=db.get_player("AlphaOne", server="738"),
        powers=("94.2M", "", ""),
        types=hub._TYPE_ORDER_OTHER,
    )
    interaction = _interaction()
    first, second = MagicMock(), MagicMock()
    first.content, second.content = "no idea", "Tank, Tank, Aircraft"
    first.delete = second.delete = AsyncMock()
    interaction.client.wait_for = AsyncMock(side_effect=[first, second])

    await modal.on_submit(interaction)

    assert [s["squad_type"] for s in _squads_of("AlphaOne")] == ["Tank", "Tank", "Aircraft"]
    assert any("couldn't read" in str(c) for c in interaction.followup.send.call_args_list)


async def test_giving_up_says_the_powers_are_safe_and_names_the_way_back(cd_db):
    modal = _detail_modal(
        player=db.get_player("AlphaOne", server="738"),
        powers=("94.2M", "", ""),
        types=hub._TYPE_ORDER_OTHER,
    )
    interaction = _interaction()
    bad = MagicMock()
    bad.content = "no idea"
    bad.delete = AsyncMock()
    interaction.client.wait_for = AsyncMock(return_value=bad)

    await modal.on_submit(interaction)

    msg = _sent(interaction)
    assert "powers are saved" in msg
    assert hub.CD_BTN_SQUADS in msg, "a dead end has to name its exit"
    assert _slot_of("AlphaOne", 1)["power"] == 94_200_000


async def test_a_blank_purity_box_says_none_are_mixed(cd_db):
    """Kevin's decision, 2026-08-17: the box is optional and blank says the
    same thing as typing "none". Somebody filling in this screen has the lineup
    in front of them, so silence about a mixed squad is an answer."""
    modal = hub._SquadDetailModal(player=db.get_player("AlphaOne", server="738"))
    modal.squad1._value = "94.2M"
    modal.squad2._value = ""
    modal.squad3._value = ""
    modal.types.component._values = []
    modal.mixed.component._value = ""

    await modal.on_submit(_interaction())

    assert [s["mixed"] for s in _squads_of("AlphaOne")] == [0, 0, 0]


async def test_an_empty_screen_is_not_a_measurement_that_everything_is_pure(cd_db):
    """Blank means none only once the member has told us something. Nobody
    looked at anything here, and without the guard it would write a purity
    measurement for all three boxes."""
    modal = hub._SquadDetailModal(player=db.get_player("AlphaOne", server="738"))
    for box in (modal.squad1, modal.squad2, modal.squad3):
        box._value = ""
    modal.types.component._values = []
    modal.mixed.component._value = ""

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert "Nothing to record" in _sent(interaction)
    assert _squads_of("AlphaOne") == []


async def test_saying_none_alone_is_still_worth_recording(cd_db):
    """Answering only the purity box is a real measurement: they looked, and
    every squad is pure."""
    modal = hub._SquadDetailModal(player=db.get_player("AlphaOne", server="738"))
    for box in (modal.squad1, modal.squad2, modal.squad3):
        box._value = ""
    modal.types.component._values = []
    modal.mixed.component._value = "none"

    await modal.on_submit(_interaction())

    assert [s["mixed"] for s in _squads_of("AlphaOne")] == [0, 0, 0]


def test_the_capture_guide_sits_with_the_flows_it_explains_and_never_locks():
    """Documentation, not a paid surface, and not a hub button either.

    Session 6 takes it off the root and attaches it to the two entry flows it
    explains: its two screens are the deployment order and one squad's power
    and type, which is exactly what the controls beside it here ask for. Never
    locked, because someone deciding whether to pay should be able to see what
    contributing involves and withholding a picture of a game screen protects
    nothing.
    """
    on_root = hub.ChampionDuelHubView(
        user_id=OUTSIDER_ID, is_admin=False, can_write=False, engine_ok=False
    )
    assert hub.CD_BTN_GUIDE not in [b.label for b in on_root.children]

    for view in (
        hub.PlayerActionsView(
            player={"id": 1, "display_name": "AlphaOne", "server": "738"},
            user_id=OUTSIDER_ID,
            can_write=False,
        ),
        hub._MissView(can_write=False, user_id=OUTSIDER_ID, name="NobodyAtAll", server="738"),
    ):
        guide = [b for b in view.children if b.label == hub.CD_BTN_GUIDE]
        assert guide, f"{type(view).__name__} lost the guide"
        assert guide[0].disabled is False


def test_the_guide_ships_both_annotated_screens():
    """A missing asset degrades to the words alone rather than failing the
    button — but on a complete deployment both should be there."""
    names = {f.filename for f in hub.guide_files()}
    assert names == set(hub.GUIDE_IMAGES)


def test_every_guide_image_carries_alt_text():
    """WCAG 2.2 AA 1.1.1. These images are entirely instructional, so without a
    description a screen-reader user gets nothing at all from the button."""
    for file in hub.guide_files():
        assert file.description, f"{file.filename} has no alt text"
        # Long enough to actually describe the markers, not "screenshot".
        assert len(file.description) > 120
        # Discord rejects an attachment description over 1024.
        assert len(file.description) <= 1024


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("84600000", 84_600_000),
        ("84,600,000", 84_600_000),
        ("84.6M", 84_600_000),
        ("84.6m", 84_600_000),
        ("84.6 M", 84_600_000),
        ("300K", 300_000),
        ("1.2B", 1_200_000_000),
    ],
)
def test_power_is_read_however_it_was_written(typed, expected):
    """The game shows 84.6M; a spreadsheet shows 84,600,000. Same number, and
    neither is the reader's mistake to correct."""
    assert hub.parse_power(typed) == expected


@pytest.mark.parametrize(
    "typed,expected",
    [
        ("81.9", 81_900_000),
        ("84.6", 84_600_000),
        ("100.4", 100_400_000),
        ("82", 82_000_000),
    ],
)
def test_a_bare_number_off_the_game_screen_means_millions(typed, expected):
    """The game prints `84.6M` and people drop the M copying it. Read
    literally it stored 81.9 and rendered "82", which looks like the bot
    ignored what they typed."""
    assert hub.parse_power(typed) == expected


@pytest.mark.parametrize("typed", ["84600000", "84,600,000", "1500000"])
def test_a_full_figure_is_left_alone(typed):
    """The inference only covers what the game displays. Anything already at
    squad-power scale is taken at face value."""
    assert hub.parse_power(typed) >= 1_000_000


@pytest.mark.parametrize("typed", ["", "   ", "lots", "84.6X", "-5", "0", "8.4.6M", None])
def test_unreadable_power_returns_none_rather_than_a_guess(typed):
    """A squad power silently wrong by 1000x produces a confident prediction
    for a line-up nobody can field."""
    assert hub.parse_power(typed) is None


def test_each_step_pairs_its_words_with_its_own_image():
    """A numbered list is useless if the thing it numbers is two screens away,
    and Discord stacks attachments after all the text — so one embed per step,
    each carrying its own picture."""
    embeds, files = hub.build_guide()
    assert len(embeds) == len(hub.GUIDE_SECTIONS)
    for embed, section in zip(embeds, hub.GUIDE_SECTIONS):
        assert embed.title == section["title"]
        assert embed.image.url == f"attachment://{section['image']}"
    assert {f.filename for f in files} == set(hub.GUIDE_IMAGES)
    # Consent is stated on the surface, not just in the commit that added it.
    assert "permission" in embeds[-1].footer.text


def test_the_instructions_survive_missing_images(monkeypatch):
    """The words are the guide; the pictures make it fast. A partial deployment
    loses the picture and keeps the instructions."""
    monkeypatch.setattr(hub, "_GUIDE_DIR", "/nonexistent/assets")
    embeds, files = hub.build_guide()
    assert files == []
    assert all(embed.image.url is None for embed in embeds)
    assert "The squad in Slot 1." in embeds[0].description


def test_the_guide_carries_no_words_in_the_images():
    """Text baked into a screenshot cannot be selected, translated, resized or
    read aloud. Every instruction lives in the embed instead, so each section
    has to actually have a body."""
    for section in hub.GUIDE_SECTIONS:
        assert section["body"].strip()
        assert "1." in section["body"]


async def test_the_guide_survives_missing_assets(monkeypatch):
    monkeypatch.setattr(hub, "_GUIDE_DIR", "/nonexistent/assets")
    assert hub.guide_files() == []


def test_predicting_is_disabled_without_the_engine():
    """A control that cannot change anything is worse than no control."""
    view = hub.ChampionDuelHubView(user_id=ADMIN_ID, is_admin=True, can_write=True, engine_ok=False)
    predict = [b for b in view.children if b.label == hub.CD_BTN_PREDICT]
    assert predict[0].disabled
    # The admin tools only touch SQLite, so they stay live.
    edits = [b for b in view.children if b.label == hub.CD_BTN_EDITS]
    assert not edits[0].disabled


async def test_only_the_opener_can_press_the_buttons():
    view = hub.ChampionDuelHubView(user_id=ADMIN_ID, is_admin=True, can_write=True, engine_ok=True)
    intruder = _interaction(user_id=OUTSIDER_ID)
    assert await view.interaction_check(intruder) is False
    intruder.response.send_message.assert_awaited_once()


def test_the_hub_never_sells_premium_on_contributing(cd_db):
    """Contributing is free and uncapped, so there is no upsell for it even on
    the path that used to render one. Free alliances are the collection engine:
    every sighting they enter sharpens the predictions paying alliances get, so
    gating this is the one split in the product that makes the paid tier
    worse."""
    embed = hub.build_hub_embed(servers=db.get_servers(), can_write=False)

    names = [f.name for f in embed.fields]
    assert not any("Premium" in n for n in names)
    assert "2" in embed.description  # both registrants counted


def test_hub_embed_lists_the_servers_bare_and_in_numeric_order(cd_db):
    """Bare numbers, one list, no per-server counts. A member is scanning for
    their own server; a count beside each one turns that into decoding."""
    db.upsert_registrant("StrangerFrom99", server="99", origin="self_reported", actor=KEV)
    db.upsert_registrant("StrangerFrom800", server="800", origin="self_reported", actor=KEV)

    embed = hub.build_hub_embed(servers=db.get_servers(), can_write=True)

    assert "99, 738, 800" in embed.description, "numeric, not lexicographic or by size"
    assert "(1)" not in embed.description and "(2)" not in embed.description


def test_hub_embed_counts_players_without_a_group(cd_db):
    """The total comes from servers, not groups. A self-reported player's group
    is optional, so counting groups would omit exactly the people the hub is
    asking members to add."""
    db.upsert_registrant("NoGroupHere", server="738", origin="self_reported", actor=KEV)

    embed = hub.build_hub_embed(servers=db.get_servers(), can_write=True)

    grouped = sum(g["registrants"] for g in db.get_groups())
    assert grouped == 2, "the added player has no group, so get_groups cannot see them"
    assert "**3**" in embed.description, "but the hub counts all three"


def test_hub_embed_invites_a_player_from_a_server_we_do_not_have(cd_db):
    """The listed servers are the ones we hold, not the ones we accept, and a
    member facing someone from an unimported server has to read the line as an
    invitation rather than a rejection.

    **It names a control that is on this surface.** The line named
    `➕ Add a player` until session 6 took that off the root, and prose naming
    a button the reader cannot see is the dead end `UX.md` principle 3 exists
    to stop. `🔍 Find a player` is the door now, and the miss it produces
    carries the add.
    """
    embed = hub.build_hub_embed(servers=db.get_servers(), can_write=True)
    on_root = [
        b.label
        for b in hub.ChampionDuelHubView(
            user_id=ADMIN_ID, is_admin=False, can_write=True, engine_ok=True
        ).children
    ]

    assert "Missing someone?" in embed.description
    # Named by its words: the button's leading emoji is a near-black glyph that
    # disappears against the embed background.
    assert "**Find a player**" in embed.description
    assert "🔍" not in embed.description
    assert hub.CD_BTN_FIND in on_root


def test_hub_embed_carries_no_source_legend(cd_db):
    """The 👁/≈/✏️ marks annotate squad powers, which only appear on a player's
    card. A legend on the hub is a key to a map the reader is not holding."""
    embed = hub.build_hub_embed(servers=db.get_servers(), can_write=True)

    assert embed.footer.text is None
    assert "observed" not in (embed.description or "")


def test_a_non_numeric_server_does_not_break_the_listing(cd_db):
    """Server is free text on the self-reported path, so the sort has to place
    something unparseable rather than raise while rendering the hub."""
    db.upsert_registrant("Mystery", server="abc", origin="self_reported", actor=KEV)

    embed = hub.build_hub_embed(servers=db.get_servers(), can_write=True)

    assert "738, abc" in embed.description, "digits first, the rest after"


def test_server_counts_separate_roster_from_scouting(cd_db):
    """Every server is in the roster; only some have anyone seen deploying.
    Reporting one number for both would hide the gap worth filling."""
    db.set_squad(_reg("AlphaOne"), 1, squad_type="Tank", power=1, actor=KEV, source="observed")
    rows = {r["server"]: r for r in db.get_servers()}
    assert rows["738"]["registrants"] == 2
    assert rows["738"]["scouted"] == 1


def test_a_self_reported_player_can_introduce_an_unimported_server(cd_db):
    """The roster is what was imported, not the set of servers we serve. Someone
    facing an opponent from a server nobody imported has to be able to enter
    them, so `get_servers` reports it afterwards rather than rejecting it."""
    assert "999" not in {r["server"] for r in db.get_servers()}

    db.upsert_registrant("StrangerFrom999", server="999", origin="self_reported", actor=KEV)

    rows = {r["server"]: r for r in db.get_servers()}
    assert rows["999"]["registrants"] == 1
    assert rows["999"]["scouted"] == 0, "added, not yet scouted -- the gap the hub asks about"


# ── Predict ───────────────────────────────────────────────────────────────────


async def test_predict_refuses_a_player_with_no_line_up(cd_db):
    """ "No squad data" and "no such player" need different copy: one is fixed
    by checking the spelling, the other by entering a sighting."""
    _full_squads(_reg("AlphaOne"))
    modal = hub._PredictModal()
    modal.player_a._value = "AlphaOne"
    modal.server_a._value = "738"
    modal.player_b._value = "BetaTwo"
    modal.server_b._value = "738"

    interaction = _interaction()
    await modal.on_submit(interaction)

    msg = _sent(interaction)
    assert "BetaTwo" in msg and "no squad recorded" in msg
    assert hub.CD_BTN_SQUADS in msg, "a dead end has to name its exit"
    # And the exit has to be reachable: correcting a squad starts from the
    # player's card now, so the hint routes through finding them first.
    assert hub.CD_BTN_FIND in msg


async def test_predict_renders_both_sides(cd_db):
    _full_squads(_reg("AlphaOne"), powers=(50_000_000, 40_000_000, 30_000_000))
    _full_squads(_reg("BetaTwo"), powers=(20_000_000, 15_000_000, 10_000_000))
    modal = hub._PredictModal()
    modal.player_a._value = "AlphaOne"
    modal.server_a._value = "738"
    modal.player_b._value = "BetaTwo"
    modal.server_b._value = "738"

    interaction = _interaction()
    await modal.on_submit(interaction)

    kwargs = interaction.followup.send.call_args.kwargs
    # The card is the answer; the caption is what survives a screen reader, a
    # failed image load, and Discord's own search.
    assert kwargs["file"].filename.endswith(".webp")
    caption = interaction.followup.send.call_args.args[0]
    assert "AlphaOne" in caption and "BetaTwo" in caption
    assert "%" in caption and hub.words.CONFIDENCE_LABEL in caption
    # The caption states the same numbers the card does, through the same
    # formatter. It used to build its own with `:.0%`, so a lopsided pairing
    # read "100%" in the text directly above a card saying ">99%".
    assert "100%" not in caption and "0%" not in caption


async def test_sharing_posts_the_card_to_the_channel(cd_db):
    """A followup to an ephemeral interaction is itself ephemeral, so the card
    has to go to the channel directly — the one thing this button exists for."""
    view = hub.SharePredictionView(
        png=b"not-really-an-image", caption="🆚 A 60% · B 40%", user_id=7
    )
    interaction = _interaction()
    interaction.channel.send = AsyncMock()

    await view.share.callback(interaction)

    interaction.channel.send.assert_awaited_once()
    posted = interaction.channel.send.call_args
    assert "60%" in posted.args[0]
    assert "<@7>" in posted.args[0], "a busy channel needs to know who shared it"
    assert posted.kwargs["file"].filename.endswith(".webp")
    # Spent, so it can't be double-posted.
    assert view.share.disabled is True


async def test_sharing_without_channel_permission_says_so(cd_db):
    """Never fail silently, and name the exit: the member can still save the
    image and post it themselves."""
    view = hub.SharePredictionView(png=b"x", caption="🆚 A 60% · B 40%", user_id=7)
    interaction = _interaction()
    interaction.channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))

    await view.share.callback(interaction)

    msg = _sent(interaction)
    assert "Send Messages" in msg and "Attach Files" in msg


async def test_a_failed_render_still_answers_the_question(cd_db, monkeypatch):
    """A render is fonts, an asset and Pillow. None of them are worth losing a
    correct prediction over, so it falls back to the embed."""
    _full_squads(_reg("AlphaOne"), powers=(50_000_000, 40_000_000, 30_000_000))
    _full_squads(_reg("BetaTwo"), powers=(20_000_000, 15_000_000, 10_000_000))

    def boom(*_a, **_kw):
        raise RuntimeError("no fonts on this box")

    monkeypatch.setattr(hub.champion_duel_image, "render", boom)

    modal = hub._PredictModal()
    modal.player_a._value = "AlphaOne"
    modal.server_a._value = "738"
    modal.player_b._value = "BetaTwo"
    modal.server_b._value = "738"

    interaction = _interaction()
    await modal.on_submit(interaction)

    kwargs = interaction.followup.send.call_args.kwargs
    assert "file" not in kwargs
    embed = kwargs["embed"]
    assert "AlphaOne" in embed.title and "BetaTwo" in embed.title
    assert any("Confidence" in f.name for f in embed.fields)


async def test_ambiguous_name_asks_which_server(cd_db):
    """Two servers can field the same name. Picking one would attach data to
    the wrong player, and that is not recoverable."""
    db.import_registrants(
        [{"name": "AlphaOne", "group": "N", "rank": 4, "server": "1042"}], stage="qualifiers"
    )
    modal = hub._FindPlayerModal(can_write=True)
    modal.name._value = "AlphaOne"
    modal.server._value = ""

    interaction = _interaction()
    await modal.on_submit(interaction)

    msg = _sent(interaction)
    assert "more than one server" in msg
    assert "738" in msg and "1042" in msg


async def test_lookup_of_an_unknown_name_says_what_to_check(cd_db):
    modal = hub._FindPlayerModal(can_write=True)
    modal.name._value = "NobodyAtAll"
    modal.server._value = ""
    interaction = _interaction()
    await modal.on_submit(interaction)
    assert "No registrant matches" in _sent(interaction)


def test_player_embed_states_the_basis_instead_of_glyphing_each_value(cd_db):
    """One sentence for the whole card, following the prediction card's footer,
    rather than a per-value key the reader has to learn and apply three times.
    DESIGN.md retired 👁️ in 2026-08-10 for reading clinical."""
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=1_000, actor=KEV, source="estimated")
    db.set_squad(rid, 2, squad_type="Missile", power=900, actor=KEV, source="observed")
    player = db.get_player("AlphaOne", server="738", include_scouting=True)

    embed = hub.build_player_embed(player, None)

    squads = next(f.value for f in embed.fields if f.name == "Squads")
    assert "👁" not in squads and "≈" not in squads and "✏️" not in squads
    assert "Some squad powers are estimated" in embed.footer.text


def test_the_basis_leads_with_the_weakest_input(cd_db):
    """A card carrying one estimate is qualified by that estimate, the same way
    `medium` confidence qualifies a prediction."""
    rid = _reg("AlphaOne")
    for slot, source in ((1, "observed"), (2, "observed"), (3, "estimated")):
        db.set_squad(rid, slot, squad_type="Tank", power=1_000, actor=KEV, source=source)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)

    assert "estimated" in hub.build_player_embed(player, None).footer.text


@pytest.mark.parametrize(
    ("seen", "total", "expected"),
    [
        (1, 1, "Their only recorded order"),
        (8, 8, "All 8 of their recorded orders"),
        (4, 8, "4 of their 8 recorded orders"),
        (2, 3, "2 of their 3 recorded orders"),
    ],
)
def test_the_order_share_reads_as_a_sentence_at_every_count(seen, total, expected):
    """ "Seen 1 of 1 sightings" was ungrammatical and circular: two counts that
    were the same number, answering a question nobody asked. The reader wants
    to know whether this is what the player always does."""
    assert hub._order_share(seen, total) == expected


def test_a_single_recorded_order_never_renders_as_one_of_one(cd_db):
    """The case that shipped wrong, end to end through the card."""
    rid = _reg("AlphaOne")
    db.add_order(rid, ("Tank", "Missile", "Aircraft"), actor=KEV)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)

    embed = hub.build_player_embed(player, db.most_common_order(rid))

    order_field = next(f.value for f in embed.fields if f.name == hub.FIELD_THEIRS)
    assert "1 of 1" not in order_field
    assert "Their only recorded order" in order_field


def test_the_card_leads_with_the_alliance_tag_and_holds_qualifiers_below(cd_db):
    """Squads and the order are what a member came for. Group and rank are
    qualifier history, so they sit under the answer rather than above it."""
    db.upsert_registrant("AlphaOne", server="738", alliance="DxL", actor=KEV)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)

    embed = hub.build_player_embed(player, None)

    assert embed.title.startswith("[DxL] ")
    assert "738" in embed.title
    names = [f.name for f in embed.fields]
    assert names.index("Squads") < names.index(hub.FIELD_STAGES)
    assert "Group" in next(f.value for f in embed.fields if f.name == hub.FIELD_STAGES)


# ── Record a line-up ──────────────────────────────────────────────────────────


def test_the_select_offers_every_permutation_and_only_those():
    """One select rather than three type pickers: three pickers can build
    'Tank, Tank, Missile', and the only thing left to do with that is reject
    it after the fact."""
    assert len(hub.ORDERS) == 6
    assert len({tuple(o) for o in hub.ORDERS}) == 6
    for order in hub.ORDERS:
        assert sorted(order) == sorted(db.VALID_TYPES)


async def test_confirming_the_select_records_the_sighting(cd_db):
    rid = _reg("AlphaOne")
    player = db.get_player("AlphaOne", server="738")
    view = hub._OrderSelectView(player=player, opponent="BetaTwo", user_id=ADMIN_ID)

    pick = _interaction()
    view.select._values = ["2"]  # Missile → Tank → Aircraft
    await view._on_select(pick)
    assert view.choice == ("Missile", "Tank", "Aircraft")
    assert view.confirm.disabled is False

    confirm = _interaction()
    await view._on_confirm(confirm)

    top = db.most_common_order(rid)
    assert top["order"] == ["Missile", "Tank", "Aircraft"]
    assert top["seen"] == 1
    assert "Recorded" in _sent(confirm)


async def test_confirm_is_dead_until_something_is_picked(cd_db):
    player = db.get_player("AlphaOne", server="738")
    view = hub._OrderSelectView(player=player, opponent=None, user_id=ADMIN_ID)
    assert view.confirm.disabled is True


# ── Admin flows, moved from the old subcommands ───────────────────────────────


async def test_export_produces_readable_csv(cd_db, admin_env):
    db.set_squad(_reg("AlphaOne"), 1, squad_type="Tank", power=1_000, actor=KEV)
    modal = hub._ExportModal()
    modal.start._value = "2000-01-01"
    modal.end._value = "2099-01-01"

    interaction = _interaction()
    await modal.on_submit(interaction)

    payload = interaction.followup.send.call_args.kwargs["file"].fp.read().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(payload)))
    assert rows, "export produced no rows"
    assert rows[0]["display_name"] == "AlphaOne"
    # The server is what distinguishes two players who share a name.
    assert rows[0]["server"] == "738"
    assert rows[0]["actor_discord_id"] == str(ADMIN_ID)


async def test_export_rejects_a_reversed_range(cd_db, admin_env):
    modal = hub._ExportModal()
    modal.start._value = "2026-08-12"
    modal.end._value = "2026-08-01"
    interaction = _interaction()
    await modal.on_submit(interaction)
    assert "after the end date" in _sent(interaction)
    assert "file" not in interaction.followup.send.call_args.kwargs


def test_end_date_covers_the_whole_day():
    """A same-day range must not come back empty.

    Timestamps compare as text, so an inclusive end has to be the day's last
    instant. Midnight would make an export of X to X silently return nothing,
    which reads as 'no edits that day' rather than 'your range had zero width'.
    """
    start = hub._parse_day("2026-08-12", end_of_day=False)
    end = hub._parse_day("2026-08-12", end_of_day=True)
    assert start < "2026-08-12T13:45:00+00:00" < end


@pytest.mark.parametrize("bad", ["12/08/2026", "not-a-date", "", "2026-13-45", None])
def test_bad_dates_rejected(bad):
    assert hub._parse_day(bad, end_of_day=False) is None


async def test_revert_conflict_offers_the_override_instead_of_clobbering(cd_db, admin_env):
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", actor=KEV)
    stale = db.set_squad(rid, 1, squad_type="Missile", actor=KEV)["edit_ids"][0]
    db.set_squad(rid, 1, squad_type="Aircraft", actor=KEV)

    interaction = _interaction()
    await hub._do_revert(interaction, stale, force=False)

    msg = _sent(interaction)
    assert "wasn't reverted" in msg and "Aircraft" in msg
    # The override is a button on the conflict, not a flag set before seeing it.
    view = interaction.followup.send.call_args.kwargs["view"]
    assert isinstance(view, hub._RevertAnyway)
    # Nothing on disk moved.
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    assert player["squads"][0]["squad_type"] == "Aircraft"


async def test_forced_revert_applies_and_appends(cd_db, admin_env):
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", actor=KEV)
    edit_id = db.set_squad(rid, 1, squad_type="Missile", actor=KEV)["edit_ids"][0]

    before = db.list_edits()["total"]
    interaction = _interaction()
    await hub._do_revert(interaction, edit_id, force=False)

    assert "Reverted" in _sent(interaction)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    assert player["squads"][0]["squad_type"] == "Tank"
    assert db.list_edits()["total"] == before + 1, "revert should append, never delete"


async def test_revert_of_an_unknown_edit(cd_db, admin_env):
    interaction = _interaction()
    await hub._do_revert(interaction, 99999, force=False)
    assert "No edit" in _sent(interaction)


async def test_revert_modal_rejects_a_non_number(cd_db, admin_env):
    modal = hub._RevertModal()
    modal.edit_id._value = "the tank one"
    interaction = _interaction()
    await modal.on_submit(interaction)
    assert hub.CD_BTN_EDITS in _sent(interaction), "a dead end has to name its exit"


async def test_edits_listing_is_capped(cd_db, admin_env):
    rid = _reg("AlphaOne")
    for i in range(30):
        db.set_squad(rid, 1, power=1000 + i, actor=KEV)

    interaction = _interaction()
    await hub._send_edits(interaction, limit=999)

    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert len(embed.description.splitlines()) <= hub.BROWSE_MAX
    # The footer has to point at the export, since that's the real browsing tool.
    assert hub.CD_BTN_EXPORT in embed.footer.text


def test_describe_renders_a_revert_marker():
    line = hub._describe(
        {
            "id": 7,
            "display_name": "AlphaOne",
            "server": "738",
            "slot": 1,
            "field": "squad_type",
            "old_value": "Tank",
            "new_value": "Missile",
            "actor_discord_id": "111",
            "created_at": "2026-08-12T10:00:00+00:00",
            "revert_of": 3,
            "target": "squad",
        }
    )
    assert "#7" in line and "revert of #3" in line and "<@111>" in line


def test_a_scrubbed_actor_reads_as_unknown_rather_than_as_an_empty_mention():
    """A data removal scrubs `actor_discord_id` and keeps the edit, so the
    history has to render a row whose actor is gone. Formatted unconditionally
    that produced `<@>`, which Discord does not resolve and which reads as a
    rendering bug rather than as a person who asked to be forgotten."""
    line = hub._describe(
        {
            "id": 7,
            "display_name": "AlphaOne",
            "server": "738",
            "slot": 1,
            "field": "squad_type",
            "old_value": "Tank",
            "new_value": "Missile",
            "actor_discord_id": None,
            "created_at": "2026-08-12T10:00:00+00:00",
            "target": "squad",
        }
    )
    assert "<@>" not in line
    # The same word this function already uses for a name it does not have.
    assert "(unknown)" in line


# ── Access ────────────────────────────────────────────────────────────────────


def test_unset_env_admits_nobody(monkeypatch):
    """A misconfigured deploy must close the surface, not open it."""
    monkeypatch.delenv("CHAMPION_DUEL_ADMIN_IDS", raising=False)
    assert hub._is_admin(ADMIN_ID) is False


def test_admin_env_admits_only_the_listed_ids(admin_env):
    assert hub._is_admin(ADMIN_ID) is True
    assert hub._is_admin(OUTSIDER_ID) is False


# ── Which grouping is this ────────────────────────────────────────────────────
#
# Champion Duel structure is per grouping, and everything merged before this
# assumed there was exactly one. The failure is silent rather than loud: an
# officer in warzone 1500 recording an opponent as "Group D" landed that player
# in the imported grouping's Group D, and nothing anywhere said so.
#
# These cover the hub learning which grouping it is talking about. The `cd_db`
# fixture imports two players on warzone 738, which creates one grouping holding
# that warzone and nothing else.

# A grouping's worth of warzones, none of them 738, so a test can build a
# second grouping that does not touch the fixture's.
SIXTEEN = [str(700 + i) for i in range(db.GROUPING_SIZE)]


@pytest.fixture
def no_mm_link(monkeypatch):
    """No Map Manager link, which is the state of most alliances."""
    monkeypatch.setattr(hub, "_mm_warzone", lambda guild_id: None)


def _view(interaction):
    return interaction.followup.send.call_args.kwargs.get("view")


def _embed(interaction):
    return interaction.followup.send.call_args.kwargs.get("embed")


def _button(view, label):
    return next(b for b in view.children if b.label == label)


async def test_an_alliance_we_cannot_place_is_asked_for_one_number(cd_db, no_mm_link):
    """The third hub state. A global count describing somebody else's tournament
    is what the other two would become without this."""
    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    view = _view(interaction)
    assert isinstance(view, hub.ChampionDuelOnboardingView)
    assert hub.CD_BTN_SET_WARZONE in _labels(view)
    assert "Which warzone" in _embed(interaction).description
    # Disabled rather than absent: it is the second half of one job, and a
    # surface with a single button on it reads as a dead end.
    assert _button(view, hub.CD_BTN_ADD_GROUPING).disabled


async def test_a_warzone_in_no_grouping_asks_for_the_grouping_not_the_number_again(
    cd_db, no_mm_link
):
    """They already answered. Asking again would be the surface failing to say
    what is actually missing."""
    db.set_guild_warzone("999", "1500")

    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    view = _view(interaction)
    assert isinstance(view, hub.ChampionDuelOnboardingView)
    assert "**1500**" in _embed(interaction).description
    assert not _button(view, hub.CD_BTN_ADD_GROUPING).disabled


async def test_a_map_manager_linked_alliance_is_never_asked(cd_db, monkeypatch):
    """`guild_alliance_mappings.server` is an INTEGER there and TEXT here, and
    that boundary is the kind of thing that silently matches nothing."""
    import config

    monkeypatch.setattr(
        config, "get_guild_alliance_mapping", lambda gid, include_revoked=False: {"server": 738}
    )

    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    assert isinstance(_view(interaction), hub.ChampionDuelHubView)
    assert db.get_guild_warzone("999") is None, "an inference is not a pin"


async def test_the_hub_outside_a_server_stays_global(cd_db, no_mm_link):
    """There is nowhere to remember a warzone for a DM and nothing to scope, so
    asking would be a question with no use for the answer."""
    interaction = _interaction()
    interaction.guild_id = None

    await hub._open_hub(interaction, can_write=False)

    assert isinstance(_view(interaction), hub.ChampionDuelHubView)


# ── Setting the warzone ───────────────────────────────────────────────────────


async def test_a_warzone_that_is_not_a_number_is_refused(cd_db, no_mm_link):
    modal = hub._WarzoneModal(can_write=True)
    modal.warzone._value = "our server"

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert "not a warzone number" in _sent(interaction)
    assert db.get_guild_warzone("999") is None


async def test_setting_a_warzone_lands_on_the_hub_it_unlocked(cd_db, no_mm_link):
    """Not an acknowledgement the user then has to leave."""
    modal = hub._WarzoneModal(can_write=True)
    modal.warzone._value = "#738"

    interaction = _interaction()
    await modal.on_submit(interaction)

    pinned = db.get_guild_warzone("999")
    assert pinned["warzone"] == "738"
    assert pinned["set_by_discord_id"] == str(ADMIN_ID), "audit only, never used to resolve"
    assert pinned["confirmed_grouping_id"] == db.default_grouping_id()
    assert isinstance(_view(interaction), hub.ChampionDuelHubView)
    assert "warzone **738**" in _sent(interaction)


async def test_changing_a_warzone_names_both_numbers_first(cd_db, no_mm_link):
    """One wrong change repoints every member of the server."""
    db.set_guild_warzone("999", "738", confirmed_grouping_id=db.default_grouping_id())
    modal = hub._WarzoneModal(can_write=True, current="738")
    modal.warzone._value = "1500"

    interaction = _interaction()
    await modal.on_submit(interaction)

    msg = _sent(interaction)
    assert "**738**" in msg and "**1500**" in msg
    assert db.get_guild_warzone("999")["warzone"] == "738", "not changed until confirmed"

    view = _view(interaction)
    assert isinstance(view, hub._ChangeWarzoneView)
    await view._on_yes(_interaction())
    assert db.get_guild_warzone("999")["warzone"] == "1500"


async def test_backing_out_of_a_change_says_what_survived(cd_db, no_mm_link):
    """A backpedal, not a cancelled flow: getting that backwards makes people
    think they lost the setting."""
    db.set_guild_warzone("999", "738", confirmed_grouping_id=db.default_grouping_id())
    view = hub._ChangeWarzoneView(user_id=ADMIN_ID, can_write=True, current="738", proposed="1500")

    interaction = _interaction()
    await view._on_no(interaction)

    said = interaction.response.edit_message.call_args.kwargs["content"]
    assert said.startswith("↩️") and "still on warzone **738**" in said
    assert db.get_guild_warzone("999")["warzone"] == "738"


def test_the_resolved_hub_carries_a_way_to_fix_a_wrong_warzone(cd_db):
    """Nothing else on the hub can fix it, and everything on it depends on it."""
    scoped = hub.ChampionDuelHubView(
        user_id=ADMIN_ID, is_admin=False, can_write=True, engine_ok=True, warzone="738"
    )
    assert hub.CD_BTN_CHANGE_WARZONE in _labels(scoped)

    # Nothing resolved from, nothing to change.
    unscoped = hub.ChampionDuelHubView(
        user_id=ADMIN_ID, is_admin=False, can_write=True, engine_ok=True
    )
    assert hub.CD_BTN_CHANGE_WARZONE not in _labels(unscoped)


# ── Confirming it against a new Champion Duel ─────────────────────────────────


async def test_a_new_champion_duel_reconfirms_the_warzone_once(cd_db, no_mm_link):
    """An alliance that moved warzone still resolves, silently and wrongly: the
    old number keeps being drawn into somebody's grouping."""
    db.set_guild_warzone("999", "738")

    first = _interaction()
    await hub._open_hub(first, can_write=True)
    view = _view(first)
    assert isinstance(view, hub._ConfirmWarzoneView)
    assert "**738**" in _embed(first).description

    yes = _interaction()
    await view._on_yes(yes)
    assert db.get_guild_warzone("999")["confirmed_grouping_id"] == db.default_grouping_id()
    assert isinstance(_view(yes), hub.ChampionDuelHubView)

    again = _interaction()
    await hub._open_hub(again, can_write=True)
    assert isinstance(_view(again), hub.ChampionDuelHubView), "never on a repeat visit"


# ── Adding a grouping ─────────────────────────────────────────────────────────


def test_a_start_date_goes_in_however_it_is_written():
    """The same permissive parser every other date surface uses. Nobody should
    have to learn a second date format for one modal."""
    today = date(2026, 8, 15)
    for written in ("2026-08-04", "2026/08/04", "2026.08.04", "8/4/2026", "Aug 4 2026"):
        assert hub.parse_start_date(written, today=today) == "2026-08-04", written


def test_a_date_with_no_year_takes_the_nearest_one():
    """`parse_event_date` infers a forward year, which is right for a storm being
    scheduled and wrong here: the Sign-up stage has already run by the time its
    date can be read off the Match Overview box."""
    today = date(2026, 8, 15)
    assert hub.parse_start_date("8/4", today=today) == "2026-08-04", "not next August"
    assert hub.parse_start_date("Aug 4", today=today) == "2026-08-04"
    # A year the user actually typed is never second-guessed.
    assert hub.parse_start_date("2027-08-04", today=today) == "2027-08-04"


def test_a_date_nobody_can_read_is_not_guessed_at():
    assert hub.parse_start_date("sometime last week") is None
    assert hub.parse_start_date("") is None


async def _add_grouping(warzones, *, warzone="700", started="2026-08-04"):
    modal = hub._AddGroupingModal(can_write=True, warzone=warzone)
    modal.warzones._value = warzones
    modal.started_on._value = started
    interaction = _interaction()
    await modal.on_submit(interaction)
    return interaction


async def test_an_unreadable_date_is_refused_before_anything_is_saved(cd_db, no_mm_link):
    """The shared rejection every date surface uses, so one failure does not
    read three different ways across the bot. The examples lean past, because
    the Sign-up stage has already run by the time its date can be read."""
    interaction = await _add_grouping(" ".join(SIXTEEN), started="sometime last week")

    said = _sent(interaction)
    assert said.startswith(
        hub.DATE_PARSE_REJECT.format(raw="sometime last week", examples=hub._START_DATE_EXAMPLES)
    )
    assert "Match Overview" in said, "plus the sentence that is this feature's own"
    assert len(db.list_groupings()) == 1


async def test_a_refusal_hands_back_the_sixteen_numbers(cd_db, no_mm_link):
    """A validation failure costs one step, not the whole flow. Without this,
    "try again" means retyping sixteen numbers to fix one of them."""
    typed = " ".join(SIXTEEN[:15])
    interaction = await _add_grouping(typed)

    view = _view(interaction)
    assert isinstance(view, hub._RetryGroupingView)

    reopened = _interaction()
    await view._on_retry(reopened)
    modal = reopened.response.send_modal.call_args.args[0]
    assert modal.warzones.default == typed
    assert modal.started_on.default == "2026-08-04"


async def test_fifteen_warzones_is_refused_naming_the_count(cd_db, no_mm_link):
    interaction = await _add_grouping(" ".join(SIXTEEN[:15]))

    assert "**15 warzones**" in _sent(interaction)
    assert len(db.list_groupings()) == 1


async def test_a_repeated_warzone_is_named_rather_than_quietly_deduped(cd_db, no_mm_link):
    """Sixteen numbers with one typed twice dedupe to sixteen, and would
    otherwise be accepted as a complete grouping that is short one warzone."""
    interaction = await _add_grouping(", ".join(SIXTEEN[:15] + [SIXTEEN[0]]))

    assert "**700** is in that list twice" in _sent(interaction)
    assert len(db.list_groupings()) == 1


async def test_a_grouping_without_your_own_warzone_is_refused(cd_db, no_mm_link):
    """One of the two answers is off and there is no way to tell which from
    here. Neither half of that is stated as the user's mistake."""
    interaction = await _add_grouping(" ".join(SIXTEEN), warzone="1500")

    said = _sent(interaction)
    assert "**1500**, is not in that list" in said
    assert "wrong" not in said, "an incorrect stored value is not a user error"
    assert len(db.list_groupings()) == 1


async def test_the_game_formatting_goes_in_as_it_is_read(cd_db, no_mm_link):
    """Copied off a phone screen. Rejecting it over a separator would be a
    validation failure with nothing wrong behind it."""
    interaction = await _add_grouping(" , ".join(f"#{z}" for z in SIXTEEN))

    made = db.find_grouping_by_warzone("700")
    assert made["warzones"] == SIXTEEN
    assert made["origin"] == "member"
    assert isinstance(_view(interaction), hub.ChampionDuelHubView)


async def test_the_confirmation_reads_back_every_warzone(cd_db, no_mm_link):
    """Sixteen numbers typed off a phone screen. Saying "16 warzones" back
    confirms the count and nothing about whether they are the right sixteen."""
    interaction = await _add_grouping(" ".join(SIXTEEN))

    said = _sent(interaction)
    assert "Added your Participating Warzones" in said and "**8/4**" in said
    for zone in SIXTEEN:
        assert zone in said


async def test_creating_a_grouping_pins_the_guild(cd_db, no_mm_link):
    """They just told us their sixteen. Asking which one they play on is asking
    for something we already have."""
    await _add_grouping(" ".join(SIXTEEN))

    pinned = db.get_guild_warzone("999")
    assert pinned["warzone"] == "700"
    assert pinned["confirmed_grouping_id"] == db.find_grouping_by_warzone("700")["id"]


async def test_recording_an_older_champion_duel_does_not_reask_the_warzone(cd_db, no_mm_link):
    """**A defect on its own terms.** `resolve_grouping_for_guild` takes the
    NEWEST grouping holding your warzone, and this used to write
    `confirmed_grouping_id` for whichever one was just entered. Enter a past
    Champion Duel of your own and the two disagree, so `needs_warzone_
    confirmation` fires and the hub throws you onto "is warzone 700 yours?" --
    a question this server answered when it onboarded.
    """
    live = db.create_grouping(SIXTEEN, "2026-08-04", origin="member")
    db.set_guild_warzone("999", "700", confirmed_grouping_id=live["id"])
    earlier = SIXTEEN[:8] + [str(800 + i) for i in range(8)]

    interaction = await _add_grouping(" ".join(earlier), started="2026-06-25")

    assert db.find_grouping_by_warzone("800")["started_on"] == "2026-06-25", "stored"
    assert db.get_guild_warzone("999")["confirmed_grouping_id"] == live["id"], (
        "still the Champion Duel the hub opens on"
    )
    assert not isinstance(_view(interaction), hub._ConfirmWarzoneView)


async def test_a_newer_champion_duel_still_confirms_the_warzone(cd_db, no_mm_link):
    """The other side of the same rule, and the behaviour that must not move:
    a server whose warzone was drawn into a new event does re-confirm, once."""
    old = db.create_grouping(SIXTEEN, "2026-06-25", origin="member")
    db.set_guild_warzone("999", "700", confirmed_grouping_id=old["id"])
    newer = SIXTEEN[:8] + [str(800 + i) for i in range(8)]

    await _add_grouping(" ".join(newer), started="2026-08-04")

    made = db.find_grouping_by_warzone("700")
    assert made["started_on"] == "2026-08-04", "the newest is what resolves"
    assert db.get_guild_warzone("999")["confirmed_grouping_id"] == made["id"]


async def test_an_exact_set_match_joins_rather_than_forking(cd_db, no_mm_link):
    """Two people entering the same sixteen is not a conflict, and the order the
    game lists them in is arbitrary."""
    existing = db.create_grouping(SIXTEEN, "2026-08-04", origin="member")

    interaction = await _add_grouping(" ".join(reversed(SIXTEEN)))

    assert len(db.list_groupings()) == 2, "the fixture's and this one, not three"
    assert db.get_guild_warzone("999")["confirmed_grouping_id"] == existing["id"]
    said = _sent(interaction)
    assert "already been entered" in said
    for zone in SIXTEEN:
        assert zone in said, "they did not enter this one, so they need to see it"


# ── A Champion Duel somebody sent you ─────────────────────────────────────────
#
# The other half of what `_AddGroupingModal` does. Onboarding asks which
# Champion Duel your alliance is in and pins the server to the answer; this
# records one you were sent, which has no reason to contain your warzone and
# must never re-point the server at itself.


async def _add_sent(warzones, *, warzone="700", started="2026-08-04"):
    """The hub-root form. It does not ask whose Champion Duel this is."""
    modal = hub._AddGroupingModal(can_write=True, warzone=warzone, onboarding=False)
    modal.warzones._value = warzones
    modal.started_on._value = started
    interaction = _interaction()
    await modal.on_submit(interaction)
    return interaction


async def test_a_champion_duel_you_are_not_in_is_recorded_rather_than_refused(cd_db, no_mm_link):
    """Kevin, 2026-08-31: *"what if I want to record another grouping because I
    got sent that information?"* The onboarding control refuses this by design,
    and the finished hub's copy has been offering it since 15 August."""
    theirs = [str(900 + i) for i in range(db.GROUPING_SIZE)]

    interaction = await _add_sent(" ".join(theirs), warzone="700")

    made = db.find_grouping_by_warzone("900")
    assert made is not None, "recorded, not refused"
    assert made["warzones"] == theirs
    assert "Recorded a Champion Duel" in _sent(interaction)


async def test_recording_one_you_were_sent_never_repoints_your_server(cd_db, no_mm_link):
    """The guard it drops is the one that stops a server being pinned to a
    Champion Duel it is not in, so the pin has to go with it."""
    db.set_guild_warzone("999", "700", confirmed_grouping_id=None)
    theirs = [str(900 + i) for i in range(db.GROUPING_SIZE)]

    await _add_sent(" ".join(theirs), warzone="700")

    pinned = db.get_guild_warzone("999")
    assert pinned["warzone"] == "700", "still their own"
    assert pinned["confirmed_grouping_id"] is None
    assert db.resolve_grouping_for_guild("999") is None, "not somebody else's event"


async def test_one_you_were_sent_is_still_reachable_afterwards(cd_db, no_mm_link):
    """Recording something and then being unable to find it again is the dead
    end this surface exists to close. Their sixteen hold none of ours, so the
    warzone lookup alone would never list it."""
    db.set_guild_warzone("999", "700")
    theirs = [str(900 + i) for i in range(db.GROUPING_SIZE)]
    await _add_sent(" ".join(theirs), warzone="700")

    assert db.groupings_for_warzone("700") == [], "not drawn into it, correctly"
    readable = db.groupings_readable_by("700", "999")
    assert [g["warzones"] for g in readable] == [theirs]


async def test_joining_one_already_entered_is_still_reachable(cd_db, no_mm_link):
    """**The common path, not the edge.** A Champion Duel somebody was sent has
    usually already been entered by the alliance that plays in it, so this hits
    the join rather than the create -- and the join is where nothing else
    records that this server can read it."""
    theirs = [str(900 + i) for i in range(db.GROUPING_SIZE)]
    already = db.create_grouping(theirs, "2026-08-04", origin="member", guild_id="777")
    db.set_guild_warzone("999", "700")

    interaction = await _add_sent(" ".join(theirs), warzone="700")

    assert "already been entered" in _sent(interaction)
    assert [g["id"] for g in db.groupings_readable_by("700", "999")] == [already["id"]]


async def test_the_acknowledgement_reports_what_happened_not_what_was_declared(cd_db, no_mm_link):
    """**Why the form does not ask whose Champion Duel this is.** Kevin,
    2026-08-31: *"we should not care who all it is - for all we know it could be
    theirs from a past Duel and we don't have a reason to need to know."*

    The only sense in which one is yours is that the hub now opens on it, and
    the entry already works that out to decide the pin. So the same form and
    the same answer produce both acknowledgements, off what was concluded.
    """
    db.set_guild_warzone("999", "700")
    mine = SIXTEEN[:8] + [str(800 + i) for i in range(8)]
    theirs = [str(900 + i) for i in range(db.GROUPING_SIZE)]

    ours = await _add_sent(" ".join(mine), warzone="700", started="2026-08-04")
    assert "Added your Participating Warzones" in _sent(ours), "it is the one we open on"
    assert db.get_guild_warzone("999")["confirmed_grouping_id"] is not None

    not_ours = await _add_sent(" ".join(theirs), warzone="700", started="2026-08-04")
    assert "Recorded a Champion Duel" in _sent(not_ours), "our warzone is not in it"


async def test_a_past_champion_duel_of_your_own_is_not_called_yours(cd_db, no_mm_link):
    """The case Kevin named. It holds your warzone and is still not the one you
    are playing, so it neither re-points the server nor claims to be yours."""
    live = db.create_grouping(SIXTEEN, "2026-08-04", origin="member")
    db.set_guild_warzone("999", "700", confirmed_grouping_id=live["id"])
    earlier = SIXTEEN[:8] + [str(800 + i) for i in range(8)]

    interaction = await _add_sent(" ".join(earlier), warzone="700", started="2026-06-25")

    assert "Recorded a Champion Duel" in _sent(interaction)
    assert db.get_guild_warzone("999")["confirmed_grouping_id"] == live["id"], "unmoved"


async def test_the_sixteen_are_still_checked_on_one_you_were_sent(cd_db, no_mm_link):
    """Only the warzone guard and the pin differ. A mistyped list is a grouping
    nobody can untangle whoever it belongs to."""
    theirs = [str(900 + i) for i in range(db.GROUPING_SIZE - 1)]

    interaction = await _add_sent(" ".join(theirs), warzone="700")

    assert "**15 warzones**" in _sent(interaction)
    assert db.find_grouping_by_warzone("900") is None, "nothing saved"


async def test_a_refusal_reopens_the_form_it_came_from(cd_db, no_mm_link):
    """Handing back the onboarding form would refuse the same entry again for
    not containing their warzone: a retry button that cannot succeed."""
    theirs = [str(900 + i) for i in range(db.GROUPING_SIZE - 1)]
    interaction = await _add_sent(" ".join(theirs), warzone="700")

    reopened = _interaction()
    await _view(interaction)._on_retry(reopened)

    modal = reopened.response.send_modal.call_args.args[0]
    assert modal.onboarding is False
    assert modal.title == hub.CD_ADD_SENT_TITLE


# ── Recording a group ─────────────────────────────────────────────────────────


def _save_button(view):
    """The reconcile view holds a Select as well as buttons, and a Select has no
    `label`, so this cannot filter on the attribute directly."""
    return next(b for b in view.children if getattr(b, "label", None) == hub.CD_BTN_SAVE_GROUP)


def _record_modal(cd_db, *, stage="semifinals", recording="final", group="D", players=""):
    """The modal as Discord hands it back.

    A select's `values` reads through `BaseSelect._values`, so a submitted
    choice is set there. That the picker defaulted to something is a different
    thing from the user having chosen it, which is why the defaults set in the
    constructor are not enough to drive these.
    """
    grouping = db.find_grouping_by_warzone("738")
    modal = hub._RecordGroupModal(can_write=True, grouping=grouping, stage=stage)
    modal.round_.component._values = [stage] if stage else []
    modal.recording.component._values = [recording] if recording else []
    modal.group.component._values = [group] if group else []
    modal.players.component._value = players
    return modal


async def test_a_pasted_group_lands_on_a_reconcile_rather_than_a_write(cd_db, no_mm_link):
    """Never a silent match. `AmbiguousPlayer` already carries its candidates so
    a caller can ask which; this is that precedent applied to a paste."""
    modal = _record_modal(cd_db, players="AlphaOne, 738, 3, 33,500,000\nWren, 744, 25")

    interaction = _interaction()
    await modal.on_submit(interaction)

    view = _view(interaction)
    assert isinstance(view, hub._ReconcileView)
    assert db.get_groups(stage="semifinals") == [], "nothing written yet"

    said = _embed(interaction).description
    assert "✅ **AlphaOne**" in said, "already on 738"
    assert "➕ **Wren**" in said and "new, will be added" in said


async def test_saving_writes_the_standings_and_adds_the_new_player(cd_db, no_mm_link):
    modal = _record_modal(cd_db, players="AlphaOne, 738, 3, 33,500,000\nWren, 744, 25")
    interaction = _interaction()
    await modal.on_submit(interaction)
    view = _view(interaction)

    await view._on_save(_interaction())

    mine = db.find_grouping_by_warzone("738")
    group = db.get_or_create_group(mine["id"], "semifinals", "D")
    rows = {r["display_name"]: r for r in db.get_group_members(group["id"])}
    assert rows["AlphaOne"]["rank"] == 3
    assert rows["AlphaOne"]["score"] == 33_500_000
    assert rows["Wren"]["rank"] == 25
    assert db.get_player("Wren", server="744")["origin"] == "self_reported"


async def test_a_pasted_hero_power_reaches_the_registrant(cd_db, no_mm_link):
    """The gap this field was added to close. `group_advance_odds` refuses a
    group where anybody has neither a power nor a squad, and before this the
    paste could not carry one: eight players meant eight separate modals."""
    modal = _record_modal(
        cd_db,
        players="AlphaOne, 738, 3, 327,159,292, 33,500,000\nWren, 744, 25, 325.8M",
    )
    interaction = _interaction()
    await modal.on_submit(interaction)

    # Shown before it is written, so a misread line is caught by the person who
    # pasted it rather than by the odds being quietly wrong a week later.
    said = _embed(interaction).description
    assert "327.2M" in said and "325.8M" in said

    await _view(interaction)._on_save(_interaction())

    assert db.get_player("AlphaOne", server="738")["thp"] == 327_159_292
    assert db.get_player("Wren", server="744")["thp"] == 325_800_000


async def test_the_draw_and_the_standings_do_not_overwrite_each_other(cd_db, no_mm_link):
    """Two columns exist for exactly this. A group is recorded twice over its
    life and the second entry must not destroy the first."""
    draw = _record_modal(cd_db, recording="draw", players="AlphaOne, 738, 5")
    first = _interaction()
    await draw.on_submit(first)
    await _view(first)._on_save(_interaction())

    final = _record_modal(cd_db, recording="final", players="AlphaOne, 738, 2, 40,000,000")
    second = _interaction()
    await final.on_submit(second)
    await _view(second)._on_save(_interaction())

    mine = db.find_grouping_by_warzone("738")
    row = db.get_group_members(db.get_or_create_group(mine["id"], "semifinals", "D")["id"])[0]
    assert row["seed_rank"] == 5, "the draw survived"
    assert row["rank"] == 2


async def test_save_stays_disabled_while_a_line_is_unresolved(cd_db, no_mm_link):
    """A control that would half-write a group should not look live."""
    db.import_registrants([{"name": "AlphaOne", "server": "800"}])
    modal = _record_modal(cd_db, players="AlphaOne, , 3")

    interaction = _interaction()
    await modal.on_submit(interaction)
    view = _view(interaction)

    assert _save_button(view).disabled, "AlphaOne is on 738 and 800"

    # Settling it turns the control on rather than leaving it inert.
    view.index = 0
    picked = _interaction()
    picked.data = {"values": [str(_reg("AlphaOne", "800"))]}
    await view._on_pick_candidate(picked)
    assert not _save_button(view).disabled


async def test_a_line_nobody_can_read_is_shown_not_dropped(cd_db, no_mm_link):
    """Silently mangling one row of a paste of eight is the failure that gets
    noticed a week later."""
    modal = _record_modal(cd_db, players="Smith, Jr, 738, 1\nAlphaOne, 738, 2")

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert "not a number" in _embed(interaction).description
    view = _view(interaction)
    assert _save_button(view).disabled

    # Skipping it saves the rest rather than losing the paste.
    view.index = 0
    await view._on_skip(_interaction())
    assert not _save_button(view).disabled
    await view._on_save(_interaction())

    mine = db.find_grouping_by_warzone("738")
    members = db.get_group_members(db.get_or_create_group(mine["id"], "semifinals", "D")["id"])
    assert [m["display_name"] for m in members] == ["AlphaOne"]


async def test_knockouts_take_no_group_letter(cd_db, no_mm_link):
    """One field of 32 rather than lettered groups, so a letter would be a claim
    about a structure the round does not have."""
    modal = _record_modal(cd_db, stage="knockouts", group="D", players="AlphaOne, 738, 1")

    interaction = _interaction()
    await modal.on_submit(interaction)
    await _view(interaction)._on_save(_interaction())

    mine = db.find_grouping_by_warzone("738")
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT label FROM groups WHERE grouping_id = ? AND stage = 'knockouts'",
            (mine["id"],),
        ).fetchall()
    assert [r["label"] for r in rows] == [None]


async def test_a_lettered_round_without_a_letter_is_refused(cd_db, no_mm_link):
    modal = _record_modal(cd_db, stage="semifinals", group=None, players="AlphaOne, 738, 1")

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert "needs a group" in _sent(interaction)


async def test_recording_asks_which_champion_duel_only_when_there_is_a_choice(cd_db, no_mm_link):
    """A warzone is drawn into a new grouping every season, so "the one running
    now" is only the right answer while there is one. The finished hub invites
    people to record past results, which is exactly when it is not."""
    mine = db.find_grouping_by_warzone("738")

    only = hub._RecordGroupModal(can_write=True, grouping=mine, groupings=[mine])
    assert "Which Champion Duel?" not in [c["label"] for c in only.to_components()]

    with db._get_conn() as conn:
        conn.execute("UPDATE groupings SET started_on = '2026-08-04' WHERE id = ?", (mine["id"],))
    mine = db.get_grouping(mine["id"])
    db.create_grouping(["738", "900"], "2026-06-02", origin="member")

    both = db.groupings_for_warzone("738")
    picker = hub._RecordGroupModal(can_write=True, grouping=mine, groupings=both)
    labels = [c["label"] for c in picker.to_components()]
    assert labels[0] == "Which Champion Duel?", "above Round, not appended after the paste"
    assert len(labels) == 5, "exactly the five-component cap"

    # Newest first, and the one the hub resolved to is what it opens on.
    options = picker.to_components()[0]["component"]["options"]
    assert [o["label"] for o in options] == ["Started 8/4", "Started 6/2"]
    assert options[0]["value"] == str(mine["id"])
    assert options[0]["default"] is True


def test_a_grouping_with_no_start_date_still_has_a_label(cd_db):
    """An import can establish one before anyone reads its dates off the Match
    Overview box, and an option with a blank where the date goes is worse than
    one that says the date is missing.

    Says "Champion Duel", never "Grouping". The game uses that word for the
    group of 8 ("Semi-final Grouping: Group H") and calls the 16 warzones
    Participating Warzones, so the only meaning a member has already learned
    for it is the one we do not mean. Corrected 2026-08-16; `UX.md` has the
    reasoning under Settled.
    """
    undated = db.find_grouping_by_warzone("738")

    assert undated["started_on"] is None
    label = hub._grouping_option_label(undated)
    assert label == f"Champion Duel {undated['id']} (no date recorded)"
    assert "rouping" not in label


async def test_a_result_files_against_the_champion_duel_that_was_picked(cd_db, no_mm_link):
    """The whole point of the picker. Without it a historical result lands in
    whichever grouping started most recently, which is a different event."""
    now = db.find_grouping_by_warzone("738")
    last_season = db.create_grouping(["738", "900"], "2026-06-02", origin="member")

    modal = hub._RecordGroupModal(
        can_write=True, grouping=now, groupings=db.groupings_for_warzone("738")
    )
    modal.champion_duel.component._values = [str(last_season["id"])]
    modal.round_.component._values = ["semifinals"]
    modal.recording.component._values = ["final"]
    modal.group.component._values = ["D"]
    modal.players.component._value = "AlphaOne, 738, 3"

    interaction = _interaction()
    await modal.on_submit(interaction)
    await _view(interaction)._on_save(_interaction())

    old_group = db.get_or_create_group(last_season["id"], "semifinals", "D")
    assert [m["display_name"] for m in db.get_group_members(old_group["id"])] == ["AlphaOne"]
    assert db.get_group_members(db.get_or_create_group(now["id"], "semifinals", "D")["id"]) == []


async def test_the_candidates_are_one_select_rather_than_a_button_each(cd_db, no_mm_link):
    """A button per candidate ate four of the five rows and still capped at 20.
    The warzone is the only thing telling two identical names apart, so it has
    to be readable, which is what the description line is for."""
    db.import_registrants([{"name": "AlphaOne", "server": "800"}])
    modal = _record_modal(cd_db, players="AlphaOne, , 3")

    interaction = _interaction()
    await modal.on_submit(interaction)
    view = _view(interaction)
    view.index = 0
    view._build()

    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert len(selects) == 1
    assert {o.description for o in selects[0].options} == {"Warzone 738", "Warzone 800"}
    assert not [c for c in view.children if getattr(c, "label", None) == "AlphaOne"]


def test_a_knockout_placement_is_the_round_they_went_out_in():
    """A 32-bracket is rigid, so the finishing position carries the exit round
    and nothing extra has to be stored."""
    assert db.knockout_result(11) == "Made it to Top 16"
    assert db.knockout_result(1) == "1st"
    assert db.knockout_result(2) == "2nd"
    # The third-place match is what separates these two, and needs no column.
    assert db.knockout_result(3) == "3rd"
    assert db.knockout_result(4) == "Made it to Top 4"
    assert db.knockout_result(8) == "Made it to Quarter-finals"
    assert db.knockout_result(32) == "Made it to Top 32"


def test_a_placement_outside_the_bracket_invents_no_round():
    """A typo, or a format we have not seen. Naming a round for it would state
    a fact about a match nobody played."""
    assert db.knockout_result(33) is None
    assert db.knockout_result(0) is None
    assert db.knockout_result(None) is None
    assert db.knockout_result("first") is None


async def test_the_thirty_two_seeds_round_trip_in_the_order_given(cd_db, no_mm_link):
    """The game reorders the 32 when it places them and the rule is unknown, so
    a person reads the bracket top to bottom and the order they type is the
    order stored. Deriving it would be inventing one."""
    names = [f"Seed{i}" for i in range(1, 33)]
    paste = "\n".join(f"{name}, 738, {i}" for i, name in enumerate(names, start=1))
    modal = _record_modal(cd_db, stage="knockouts", recording="draw", group=None, players=paste)

    interaction = _interaction()
    await modal.on_submit(interaction)
    await _view(interaction)._on_save(_interaction())

    mine = db.find_grouping_by_warzone("738")
    group = db.get_or_create_group(mine["id"], "knockouts", None)
    members = db.get_group_members(group["id"])
    assert len(members) == 32
    by_seed = {m["seed_rank"]: m["display_name"] for m in members}
    assert [by_seed[i] for i in range(1, 33)] == names
    assert all(m["rank"] is None for m in members), "a draw is not a result"


async def test_the_knockout_reconcile_shows_the_exit_round(cd_db, no_mm_link):
    """What a reader can actually check against what they watched. A bare 11
    is not."""
    modal = _record_modal(
        cd_db, stage="knockouts", recording="final", group=None, players="AlphaOne, 738, 11"
    )

    interaction = _interaction()
    await modal.on_submit(interaction)

    assert "Made it to Top 16" in _embed(interaction).description


def test_the_player_card_reads_a_knockout_placement_as_a_round(cd_db):
    mine = db.find_grouping_by_warzone("738")
    db.set_stage(_reg("AlphaOne"), "knockouts", rank=11, grouping_id=mine["id"])

    embed = hub.build_player_embed(db.get_player("AlphaOne", server="738"), None)

    rounds = next(f.value for f in embed.fields if f.name == hub.FIELD_STAGES)
    assert "**Knockout Stage** · Made it to Top 16" in rounds
    assert "Rank 11" not in rounds, "the placement replaces the bare rank, not sits beside it"


async def test_a_partial_group_does_not_read_as_something_missing(cd_db, no_mm_link):
    """Eight names against a hundred-player qualifier group is the normal case,
    not a truncation."""
    modal = _record_modal(cd_db, stage="qualifiers", group="M", players="AlphaOne, 738, 22")

    interaction = _interaction()
    await modal.on_submit(interaction)

    footer = _embed(interaction).footer.text
    assert "Recording 1 player for Final Standings." in footer
    assert "you can at any time" in footer


# ── Scoped to their grouping ──────────────────────────────────────────────────


def test_counts_do_not_span_groupings(cd_db):
    """A figure covering every grouping describes several tournaments at once,
    and to the alliance reading it, most of it is somebody else's."""
    theirs = db.create_grouping(["1500", "1501"], "2026-08-04", origin="member")
    db.import_registrants(
        [{"name": "Stranger", "server": "1500"}, {"name": "Other", "server": "1501"}],
        grouping_id=theirs["id"],
    )
    mine = db.find_grouping_by_warzone("738")

    scoped = db.get_servers(mine["id"])
    assert {s["server"] for s in scoped} == {"738"}
    assert sum(s["registrants"] for s in scoped) == 2, "not the other grouping's two"

    assert sum(s["registrants"] for s in db.get_servers()) == 4, "global is still global"


def test_a_grouping_reports_warzones_it_holds_nobody_from(cd_db):
    """ "We have nothing for your warzone" is the answer that invites a
    contribution. An omitted row is one the reader has to notice is missing."""
    mine = db.find_grouping_by_warzone("738")
    with db._get_conn() as conn:
        conn.execute(
            "INSERT INTO grouping_warzones (grouping_id, warzone, source) VALUES (?, ?, ?)",
            (mine["id"], "999", "claim"),
        )

    rows = {s["server"]: s for s in db.get_servers(mine["id"])}
    assert rows["999"]["registrants"] == 0
    assert rows["738"]["registrants"] == 2


def test_the_scoped_hub_says_whose_players_these_are(cd_db):
    mine = db.find_grouping_by_warzone("738")

    embed = hub.build_hub_embed(
        servers=db.get_servers(mine["id"]), can_write=True, grouping=mine, warzone="738"
    )

    assert "**2** players in your Champion Duel" in embed.description
    assert "**1 warzone**" in embed.description, "agrees with its count"
    assert "server" not in embed.description, "the game says warzone"


def test_a_grouping_we_hold_nothing_for_says_so_rather_than_nothing(cd_db):
    """The state of every grouping but the imported one. The calendar still
    works, and the gap is exactly what a contribution fills."""
    theirs = db.create_grouping(["1500", "1501"], "2026-08-04", origin="member")

    embed = hub.build_hub_embed(
        servers=db.get_servers(theirs["id"]), can_write=True, grouping=theirs, warzone="1500"
    )

    assert "do not have any players for your Champion Duel" in embed.description
    assert "warzone **1500**" in embed.description


# ── Finished ──────────────────────────────────────────────────────────────────


def _finish_the_champion_duel(warzone="738"):
    """Push the only grouping past its last day, and pin the guild to it."""
    from datetime import timedelta

    db.set_guild_warzone("999", warzone, confirmed_grouping_id=db.default_grouping_id())
    over = (db._server_today() - timedelta(days=db.EVENT_DAYS + 1)).isoformat()
    with db._get_conn() as conn:
        conn.execute("UPDATE groupings SET started_on = ?", (over,))


async def test_a_finished_champion_duel_keeps_its_results_and_offers_the_next(cd_db, no_mm_link):
    _finish_the_champion_duel()

    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    view = _view(interaction)
    said = _embed(interaction).description
    assert "has finished" in said
    assert "**738**" in said, "whose Champion Duel this was"
    # The offer has to survive the gap before the next draw is visible in game,
    # so it states the condition rather than an instruction nobody can act on.
    assert "as soon as the draw is visible in game" in said
    # Recording past results is the other half: the data is still worth having
    # once the event is over, and that is not obvious without being told.
    assert "record past Champion Duel results" in said
    assert hub.CD_BTN_ADD_CD in _labels(view)
    # Predict and Find are global and useful between events.
    #
    # **Both, and the first one is why this line is spelled out.** Folding the
    # standalone finished view into the hub dropped `CD_BTN_PREDICT` here,
    # because a finished Champion Duel still has a grouping and the control was
    # drawn only where there was none. This assertion was weakened to
    # `CD_BTN_FIND` to match, under a comment that still said Predict, which is
    # how it survived a review and two CI runs.
    assert hub.CD_BTN_FIND in _labels(view)
    assert hub.CD_BTN_PREDICT in _labels(view)


async def test_the_finished_hub_is_the_hub(cd_db, no_mm_link):
    """**The regression this whole change exists for.** The finished state used
    to be a second view written 2026-08-15 and never touched again, so every
    surface built after that date went into the live hub and not into it. A
    member opening the hub between events got a fortnight-old shape, and between
    events is where this feature sits by default.

    Asserted as one class rather than as a list of labels, because the list is
    what went stale: a sixth control added below has to appear here without
    anybody remembering to come back and add it.
    """
    _finish_the_champion_duel()

    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    view = _view(interaction)
    assert isinstance(view, hub.ChampionDuelHubView)
    assert view.finished is True
    labels = _labels(view)
    # The four entries, which is the whole of what the fork was missing. The
    # identity control renders as the unclaimed half here, which is the reader
    # we cannot place -- the pair is one control, not two.
    assert hub.CD_BTN_WHO_AM_I in labels
    assert hub.CD_BTN_PICKS in labels
    assert hub.CD_BTN_ALLIANCE in labels
    # Either half of the Premium pair. Which one renders is the entitlement's
    # business and CI runs both lanes; what this test is about is that the one
    # entry needing no Champion Duel at all was missing from the fork.
    assert {hub.CD_BTN_INTEL, f"🔒 {hub.CD_BTN_INTEL}"} & set(labels), (
        "locked or live, never absent"
    )


async def test_one_control_enters_a_champion_duel_of_either_kind(cd_db, no_mm_link):
    """Two jobs, one control, because `notes/DESIGN.md` rule 7 says so: entering
    your own sixteen and entering a set you were sent are the same act, so both
    wanted the same glyph and neither had one free. The form asks whose."""
    _finish_the_champion_duel()

    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    labels = _labels(_view(interaction))
    assert hub.CD_BTN_ADD_CD in labels
    assert hub.CD_BTN_ADD_GROUPING not in labels, "that one is onboarding's"


async def test_adding_a_champion_duel_does_not_wait_for_yours_to_end(cd_db, no_mm_link):
    """Nothing about being sent a Champion Duel is tied to your own being over.
    Gating it on `finished` rebuilds a smaller version of the problem this
    change fixes: a thing you can only do in one state, for no visible reason,
    and a set somebody sends you mid-event is the freshest data we can get."""
    db.set_guild_warzone("999", "738", confirmed_grouping_id=db.default_grouping_id())

    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    view = _view(interaction)
    assert view.finished is False, "mid-event"
    assert hub.CD_BTN_ADD_CD in _labels(view)


async def test_only_one_control_is_primary_on_a_finished_hub(cd_db, no_mm_link):
    """`notes/DESIGN.md`: at most one primary per view, and row 0 spends it on
    the identity control. The standalone finished view had `Add your
    Participating Warzones` primary, which would be a second one here."""
    _finish_the_champion_duel()

    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    view = _view(interaction)
    primaries = [c.label for c in view.children if c.style is discord.ButtonStyle.primary]
    assert primaries == [hub.CD_BTN_WHO_AM_I], primaries


async def test_a_conflict_shows_both_lists_so_the_reader_can_tell_which_is_off(cd_db, no_mm_link):
    """Naming the shared warzone says one of the two lists has a mistake without
    showing the other one, which leaves the reader no way to work out which."""
    db.create_grouping(SIXTEEN, "2026-08-04", origin="member")
    mine = ["800"] + SIXTEEN[:15]

    interaction = await _add_grouping(", ".join(mine), warzone="800")

    embed = _embed(interaction)
    assert "700" in embed.title
    assert hub._warzone_list(sorted(mine, key=int)) in embed.description
    assert hub._warzone_list(SIXTEEN) in embed.description
    assert len(db.list_groupings()) == 2, "nothing was saved"


async def test_a_conflict_carries_both_ways_out(cd_db, no_mm_link):
    """Only the reader can tell which list is wrong. Theirs is one button away;
    the other belongs to somebody else, so that half is a route to us."""
    db.create_grouping(SIXTEEN, "2026-08-04", origin="member")

    interaction = await _add_grouping(", ".join(["800"] + SIXTEEN[:15]), warzone="800")

    view = _view(interaction)
    assert isinstance(view, hub._RetryGroupingView)
    fields = " ".join(f.value for f in _embed(interaction).fields)
    assert "Edit and try again" in fields
    assert hub.COMMUNITY_SERVER_NAME in fields
    # One tap, not a URL to read and copy. This is a phone surface.
    assert hub.COMMUNITY_SERVER_URL in [b.url for b in view.children if b.url]


async def test_a_refusal_the_caller_can_fix_does_not_send_them_to_us(cd_db, no_mm_link):
    """A miscounted list is entirely theirs to fix. A control leading somewhere
    with nothing to do there is the same waste as one that changes nothing."""
    interaction = await _add_grouping(" ".join(SIXTEEN[:15]))

    view = _view(interaction)
    assert _labels(view) == [hub.CD_BTN_RETRY_GROUPING]


# The group letter came off the add-a-player screen on 2026-08-16, when Total
# Hero Power and troop level took its place: five components is the cap and the
# model cannot run without one of the two new fields, where a letter is round
# data the record and reconcile flows already collect properly and in a
# grouping-scoped way.
#
# Three tests went with it. They covered a real bug -- a letter is meaningless
# outside a grouping, and writing one against the globally-running round put an
# officer in warzone 1500's opponent into the imported grouping's Group D. That
# rule still holds everywhere a letter IS written: the record and reconcile
# paths go through `get_or_create_group` with an explicit grouping id, so they
# cannot reach another Champion Duel's Group D at all. What is gone is the
# ability to name a group while adding a stranger you just met.


# ── `🏅 Your standing` ────────────────────────────────────────────────────────
#
# The hub used to open on eight buttons and a player count, which is the
# feature describing itself. These pin it opening on the person.
#
# WHAT WOULD HAVE FAILED BEFORE: most of this pins behaviour rather than
# catching a regression, said plainly rather than implied -- `read_standing`,
# `build_standing_embed` and `standing_opener` did not exist.
#
# TWO ARE REGRESSION TESTS against source that shipped, and both were confirmed
# to fail with `champion_duel_store.py` reverted to #533:
# `test_a_stored_answer_can_still_be_matched_to_one_player` and
# `test_the_standing_reads_the_readers_row_and_not_the_favorites`.
# `_from_payload` rebuilt every row without its `key`, so a stored answer could
# be re-rendered under current names and could NOT be searched for one player.
#
# `test_two_players_sharing_a_name_stay_two_players` is NOT one of them: it
# builds its answer directly, so it covers `_my_odds_row` rather than the
# store, and it passes either way.

STANDING_WARZONES = [str(700 + i) for i in range(16)]


@pytest.fixture
def standing_db(tmp_path, monkeypatch):
    """A semifinal group of eight, built through the real writes.

    Deliberately not the `cd_db` fixture above: that one holds two qualifier
    registrants, and everything about a standing needs a round with a model
    behind it.
    """
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    store_lib.init_store()
    grouping = db.ensure_grouping(STANDING_WARZONES, "2026-08-04")
    ids = []
    for i in range(8):
        row = db.upsert_registrant(
            name=f"P{i:02d}",
            server=STANDING_WARZONES[i],
            alliance="OGV",
            thp=(480 - i * 9) * 1_000_000,
        )
        db.set_stage(row["id"], "semifinals", grp="A", grouping_id=grouping["id"])
        ids.append(row["id"])
    group = db.get_or_create_group(grouping["id"], "semifinals", "A")
    return {"grouping": grouping, "group_id": group["id"], "ids": ids}


def _claim(standing_db, which=3, user_id=ADMIN_ID):
    """Put the reader on one of the eight, and hand back that registrant id."""
    rid = standing_db["ids"][which]
    db.claim_registrant(rid, str(user_id), discord_name="Kevin", guild_id="999")
    return rid


def _answer(members, *, advance=None):
    """An answer shaped like the engine's, without paying for a simulation.

    `key` is the row POSITION, which is what the engine keys on and what every
    caller maps back through. Ordered strongest first, so `members[0]` is the
    favorite and the reader is deliberately not it.
    """
    return odds_lib.GroupOdds(
        rows=[
            odds_lib.OddsRow(
                name=m.get("display_name") or "?",
                advance=(0.9 - i * 0.12) if advance is None else advance[i],
                win_group=max(0.0, 0.5 - i * 0.06),
                points_mean=100 - i * 3,
                points_sd=5,
                key=str(i),
            )
            for i, m in enumerate(members)
        ],
        trials=800,
        advance=2,
    )


def _remember(standing_db, *, advance=None):
    """Put a fresh answer in the store, the way the sweeper would."""
    members = db.get_group_scouting(standing_db["group_id"])
    store_lib.store(
        standing_db["group_id"], members, _answer(members, advance=advance), stage="semifinals"
    )
    return members


def _standing_of(standing_db, user_id=ADMIN_ID):
    return hub.read_standing(user_id, standing_db["grouping"])


def _field(embed, name):
    for field in embed.fields:
        if field.name == name:
            return field.value
    return None


# ── the three states ─────────────────────────────────────────────────────────


def test_the_hub_opens_on_the_person_rather_than_on_the_roster(standing_db):
    """The complaint the whole rethink came from: eight buttons and no content."""
    _claim(standing_db)
    embed = hub.build_hub_embed(
        servers=[],
        can_write=True,
        grouping=standing_db["grouping"],
        standing=_standing_of(standing_db),
    )

    first = embed.description.split("\n")[0]
    assert "P03" in first, f"the hub does not open on the reader; it opens on {first!r}"
    assert "Group A" in first
    assert first.index("P03") < first.index("Group A"), (
        "the round is ahead of the person, which is the ordering this session exists to reverse"
    )


def test_the_hub_says_so_when_it_does_not_know_who_is_reading(standing_db):
    embed = hub.build_hub_embed(
        servers=[],
        can_write=True,
        grouping=standing_db["grouping"],
        standing=_standing_of(standing_db),
    )
    assert hub._STANDING_UNCLAIMED in embed.description


def _elsewhere(name="Faraway", server="1500", *, rank=4):
    """A claimed account in a Champion Duel that is not this server's.

    Given its own grouping and a real round, because that is the case that
    matters: the reader has a standing, and it is simply somewhere else.
    """
    other_grouping = db.ensure_grouping([server, "1501"], "2026-08-04")
    row = db.upsert_registrant(name=name, server=server, alliance="OGV", thp=400_000_000)
    db.set_stage(row["id"], "semifinals", grp="B", grouping_id=other_grouping["id"])
    group = db.get_or_create_group(other_grouping["id"], "semifinals", "B")
    db.set_placement(group["id"], row["id"], rank=rank, score=30_000_000)
    db.claim_registrant(row["id"], str(ADMIN_ID), discord_name="Kevin", guild_id="999")
    return row


def test_an_account_in_another_champion_duel_still_gets_its_standing(standing_db):
    """The reader has a round. It is simply not this server's."""
    _elsewhere()
    state = _standing_of(standing_db)

    assert state["state"] == "elsewhere"
    assert state["stage"] == "semifinals", (
        "scoping the read to the guild's grouping blanked a round we hold in full"
    )
    assert state["row"]["rank"] == 4

    opener = hub.standing_opener(state)
    assert "Faraway" in opener and "Rank 4" in opener
    assert hub._STANDING_ELSEWHERE.split("**")[-1].strip() in opener


def test_the_elsewhere_note_names_this_servers_warzone_and_not_the_players(standing_db):
    """Kevin's sentence, 2026-08-25, and the number in it is the guild's.

    The reader knows their own warzone -- `_label` prints it three words
    earlier. What they cannot see is which Champion Duel the Discord they are
    standing in belongs to, so that is the number the sentence names.
    """
    _elsewhere()
    state = hub.read_standing(ADMIN_ID, standing_db["grouping"], warzone=STANDING_WARZONES[0])

    assert state["state"] == "elsewhere"
    note = hub._elsewhere_note(state["player"], state["warzone"])
    assert f"({STANDING_WARZONES[0]})" in note, "the note did not name this server's warzone"
    assert "742" not in note, "the note named the player's own warzone instead"
    assert note.endswith(".")


def test_the_elsewhere_note_drops_the_parenthetical_rather_than_printing_an_empty_one():
    """A caller with no warzone -- a DM, or a guild that never resolved one."""
    player = {"display_name": "Faraway", "server": "742"}
    assert hub._elsewhere_note(player, None) == (
        hub._STANDING_ELSEWHERE.format(player="Faraway") + "."
    )
    assert "()" not in hub._elsewhere_note(player, "")


def test_last_events_round_is_not_rendered_as_this_events_standing(standing_db):
    """A warzone is drawn into a new grouping every event.

    `attach_stages` reports the furthest round in STAGES order across every
    grouping the account is in, so a player who reached the semifinals last
    event and is in a qualifier group now had last event's group, rank, kill
    score and stored odds rendered as their current standing, with no note
    saying so. Fails against the first version of `read_standing`.
    """
    warzone = STANDING_WARZONES[4]
    # A grouping over warzones this one does not hold, so `ensure_grouping`
    # creates a second rather than merging: it matches on ANY shared warzone.
    last = db.ensure_grouping(["1600", "1601"], "2026-06-01")
    assert last["id"] != standing_db["grouping"]["id"]
    row = db.upsert_registrant(name="Veteran", server=warzone, alliance="OGV", thp=400_000_000)

    # Last event: the semifinals, which is the furthest round they ever reached.
    db.set_stage(row["id"], "semifinals", grp="C", grouping_id=last["id"])
    old_group = db.get_or_create_group(last["id"], "semifinals", "C")
    db.set_placement(old_group["id"], row["id"], rank=1, score=99_000_000)

    # This event: only the qualifiers so far.
    db.set_stage(
        row["id"], "qualifiers", grp="D", grouping_id=standing_db["grouping"]["id"], rank=52
    )
    this_group = db.get_or_create_group(standing_db["grouping"]["id"], "qualifiers", "D")
    db.set_placement(this_group["id"], row["id"], rank=52, score=21_000_000)

    db.claim_registrant(row["id"], str(ADMIN_ID), discord_name="Kevin", guild_id="999")
    state = _standing_of(standing_db)

    assert state["state"] == "held"
    assert state["stage"] == "qualifiers", (
        f"last event's {state['stage']} was rendered as this event's standing"
    )
    assert state["row"]["rank"] == 52
    assert state["row"]["grouping_id"] == standing_db["grouping"]["id"]

    recorded = _field(hub.build_standing_embed(state, can_odds=False), hub._STANDING_RECORDED)
    assert "99,000,000" not in recorded, "last event's kill score is on this event's standing"
    assert "21,000,000" in recorded


def test_the_note_about_another_champion_duel_is_not_a_prompt(standing_db):
    """The failure the guild-change detector was rejected for, one level up.

    A claim is one per Discord account and every guild resolves its own
    Champion Duel, so the community server and any second alliance's server
    reach this state routinely. Telling those readers to move a correct claim
    is the noisy proxy this surface exists to avoid.
    """
    _elsewhere()
    state = _standing_of(standing_db)
    note = hub._elsewhere_note(state["player"])

    for word in ("switch", "moved", "transfer", "update your", "tell us"):
        assert word not in note.lower(), f"the note presumes a warzone switch: {word!r}"

    view = hub.ChampionDuelHubView(
        user_id=ADMIN_ID,
        is_admin=False,
        can_write=True,
        engine_ok=True,
        grouping=standing_db["grouping"],
        standing=state,
    )
    assert hub.CD_BTN_STANDING in _labels(view), (
        "a reader with a standing elsewhere was sent back to the claim invite"
    )


def test_the_way_to_change_accounts_rides_on_every_standing(standing_db):
    """The warzone-switch answer, and it detects nothing.

    Claiming a new account moves the claim, so one permanently reachable
    control is the whole update path.
    """
    _claim(standing_db)
    view = hub.ChampionDuelHubView(
        user_id=ADMIN_ID,
        is_admin=False,
        can_write=True,
        engine_ok=True,
        grouping=standing_db["grouping"],
        standing=_standing_of(standing_db),
    )
    interaction = _interaction()
    with patch("premium.feature_gate", new=AsyncMock(return_value=False)):
        asyncio.run(view._on_standing(interaction))

    sent = interaction.followup.send.call_args.kwargs
    assert hub.CD_BTN_WHO_AM_I in _labels(sent["view"]), (
        "a standing with no way to point it at a different account"
    )


def test_a_warzone_written_with_a_leading_zero_is_still_the_same_warzone(standing_db):
    """`parse_warzones` canonicalizes through int; `_server` does not."""
    row = db.upsert_registrant(name="Padded", server="0" + STANDING_WARZONES[0], thp=400_000_000)
    db.set_stage(row["id"], "semifinals", grp="A", grouping_id=standing_db["grouping"]["id"])
    db.claim_registrant(row["id"], str(ADMIN_ID), discord_name="Kevin", guild_id="999")

    assert _standing_of(standing_db)["state"] == "held", (
        "a leading zero put a player permanently outside their own Champion Duel"
    )


def test_the_landing_is_untouched_for_a_caller_with_no_champion_duel():
    """A DM, or a guild we cannot place. Both fall through to the old hub."""
    embed = hub.build_hub_embed(servers=[], can_write=True, standing=None)
    assert hub._STANDING_UNCLAIMED not in embed.description
    assert embed.description.startswith("No roster is loaded yet.")


# ── the control ──────────────────────────────────────────────────────────────


def test_the_control_says_which_of_the_two_things_it_does(standing_db):
    """`DESIGN.md`: the label describes the control, not the outcome."""
    unknown = hub.ChampionDuelHubView(
        user_id=ADMIN_ID,
        is_admin=False,
        can_write=True,
        engine_ok=True,
        grouping=standing_db["grouping"],
        standing=_standing_of(standing_db),
    )
    assert hub.CD_BTN_WHO_AM_I in _labels(unknown)
    assert hub.CD_BTN_STANDING not in _labels(unknown)

    _claim(standing_db)
    known = hub.ChampionDuelHubView(
        user_id=ADMIN_ID,
        is_admin=False,
        can_write=True,
        engine_ok=True,
        grouping=standing_db["grouping"],
        standing=_standing_of(standing_db),
    )
    assert hub.CD_BTN_STANDING in _labels(known)
    assert hub.CD_BTN_WHO_AM_I not in _labels(known)


def test_the_control_is_absent_with_no_champion_duel_resolved():
    """Same rule as `Your group`: no grouping, no round to stand in."""
    view = hub.ChampionDuelHubView(
        user_id=ADMIN_ID, is_admin=False, can_write=True, engine_ok=True, grouping=None
    )
    assert hub.CD_BTN_STANDING not in _labels(view)
    assert hub.CD_BTN_WHO_AM_I not in _labels(view)


def test_the_identity_control_leads_the_grid(standing_db):
    """Principle 1 is identity first, and a row reads left to right."""
    _claim(standing_db)
    view = hub.ChampionDuelHubView(
        user_id=ADMIN_ID,
        is_admin=False,
        can_write=True,
        engine_ok=True,
        grouping=standing_db["grouping"],
        standing=_standing_of(standing_db),
    )
    assert _labels(view)[0] == hub.CD_BTN_STANDING


def test_the_standing_re_reads_the_claim_rather_than_trusting_its_own_button(standing_db):
    """The hub lives fifteen minutes and `ClaimResultView` can release inside it."""
    _claim(standing_db)
    view = hub.ChampionDuelHubView(
        user_id=ADMIN_ID,
        is_admin=False,
        can_write=True,
        engine_ok=True,
        grouping=standing_db["grouping"],
        standing=_standing_of(standing_db),
    )
    db.release_claim(str(ADMIN_ID))

    interaction = _interaction()
    asyncio.run(view._on_standing(interaction))

    assert hub._STANDING_UNCLAIMED in _sent(interaction), (
        "a released claim still rendered a standing off the captured read"
    )


# ── free is what we recorded ─────────────────────────────────────────────────


def test_the_free_half_carries_the_rank_the_score_and_when_they_were_read(standing_db):
    """Rank AND kill score: the round is scored on the score and ranked on it."""
    rid = _claim(standing_db)
    # `set_placement`, not `set_stage`: a kill score is a fact about how a round
    # finished and only the placement write carries it.
    db.set_placement(standing_db["group_id"], rid, rank=3, score=41_200_000)

    embed = hub.build_standing_embed(_standing_of(standing_db), can_odds=False)
    recorded = _field(embed, hub._STANDING_RECORDED)
    assert "Rank **3**" in recorded
    assert "41,200,000" in recorded
    assert "<t:" in recorded, "nothing says when these were read"
    assert hub._STANDING_READ_AT.split("{")[0].strip() in recorded
    # `-# ` is Discord's subtext and only renders at the start of a line, which
    # is the whole point of asking for this line smaller than the ones above.
    assert "\n-# Updated" in recorded, "the timestamp lost its subtext marker"


def test_the_kill_score_reward_tiers_never_appear(standing_db):
    """Kevin's, twice in the plan: they are participation, not a ladder."""
    _claim(standing_db)
    _remember(standing_db)
    embed = hub.build_standing_embed(_standing_of(standing_db), can_odds=True)
    rendered = (embed.description or "") + " ".join(f.value for f in embed.fields)
    assert "4M" not in rendered and "4,000,000" not in rendered


# ── paid is what we worked out ───────────────────────────────────────────────


def test_the_paid_half_renders_locked_rather_than_hidden(standing_db):
    """`UX.md` principle 5: the free tier sees the shape of the paid product."""
    _claim(standing_db)
    _remember(standing_db)
    embed = hub.build_standing_embed(_standing_of(standing_db), can_odds=False)
    locked = [f for f in embed.fields if f.name.startswith("🔒")]
    assert locked, f"the paid half vanished on the free tier: {[f.name for f in embed.fields]}"
    assert hub._STANDING_WORKED_OUT in locked[0].name
    assert hub._STANDING_LOCKED.split("{")[0].strip() in locked[0].value


def test_the_qualifiers_say_they_have_no_model(standing_db):
    """Qualifier odds came out of the bot on 2026-08-21. Recording did not."""
    row = db.upsert_registrant(name="OnlyQuals", server=STANDING_WARZONES[3], thp=300_000_000)
    db.set_stage(
        row["id"], "qualifiers", grp="D", grouping_id=standing_db["grouping"]["id"], rank=14
    )
    db.claim_registrant(row["id"], str(ADMIN_ID), discord_name="Kevin", guild_id="999")

    state = _standing_of(standing_db)
    assert state["stage"] == "qualifiers"
    embed = hub.build_standing_embed(state, can_odds=True)
    assert hub._STANDING_NO_MODEL.split("{")[0].strip() in _field(embed, hub._STANDING_WORKED_OUT)


def test_a_missing_stored_answer_shows_no_odds_at_all(standing_db):
    """`missing` also means a DIFFERENT SET OF PEOPLE, which is wrong not old."""
    _claim(standing_db)
    state = _standing_of(standing_db)
    assert state["stored"].state == "missing"
    assert hub._standing_worked_out(state) is None


def test_a_missing_answer_is_said_out_loud_rather_than_left_blank(standing_db):
    """`UX.md` principle 2. A paying alliance must not lose the paid half in silence.

    THIS TEST USED TO ASSERT AN EXIT TOO, and that assertion came out on
    2026-08-25 rather than being made to pass. `_STANDING_NOT_WORKED_OUT` named
    the route to `🔮 Odds of advancing`; Kevin dropped the navigation because
    `PLAN_champion_duel_ia.md` session 6 moves that control onto this very
    surface, so the sentence was about to start pointing at itself.

    Until session 6 lands the field is a statement with no exit on the message,
    which is a real gap and is recorded as one on the pull request. It is not
    guarded here, because a test asserting the gap would have to be deleted
    again the moment the gap closes.
    """
    _claim(standing_db)
    embed = hub.build_standing_embed(_standing_of(standing_db), can_odds=True)
    worked = _field(embed, hub._STANDING_WORKED_OUT)
    assert worked is not None, "the paid half vanished with nothing said"
    assert hub._STANDING_NOT_WORKED_OUT.split("{")[0].strip() in worked
    assert embed.footer.text is None, "a caveat was rendered over an answer that is not there"


def test_the_standing_and_the_odds_surface_share_one_caveat(standing_db):
    """Two literals saying the same thing in slightly different words is how copy drifts."""
    _claim(standing_db)
    _remember(standing_db)
    embed = hub.build_standing_embed(_standing_of(standing_db), can_odds=True)
    assert embed.footer.text == hub._ODDS_BASIS


def test_nothing_is_computed_when_a_standing_is_opened(standing_db, monkeypatch):
    """The hard rule. A bracket is 60-90s of GIL, and this is the LANDING."""

    def _boom(*a, **k):  # pragma: no cover - the point is that it never runs
        raise AssertionError("the standing computed odds on open")

    monkeypatch.setattr(odds_lib, "group_advance_odds", _boom)
    monkeypatch.setattr(odds_lib, "bracket_odds", _boom)

    _claim(standing_db)
    state = _standing_of(standing_db)
    hub.build_standing_embed(state, can_odds=True)


def _viewed_at(standing_db):
    with db._get_conn() as conn:
        found = conn.execute(
            "SELECT last_viewed_at FROM odds_runs WHERE group_id = ?",
            (standing_db["group_id"],),
        ).fetchone()
    return found["last_viewed_at"] if found else None


def test_opening_the_hub_does_not_jump_the_sweeper_queue(standing_db):
    """`due()` orders most-recently-viewed first, and a landing is not a press.

    The landing renders a name, a round and a rank. It does not read odds at
    all, so it cannot stamp -- and it does not pay for a scouting read and a
    fingerprint rebuild on every `/champion_duel` in every guild either.
    """
    _claim(standing_db)
    _remember(standing_db)
    before = _viewed_at(standing_db)

    landing = hub.read_standing(ADMIN_ID, standing_db["grouping"], with_odds=False)

    assert "stored" not in landing and "members" not in landing, (
        "the landing paid for an answer it does not render"
    )
    assert _viewed_at(standing_db) == before, (
        "opening the hub stamped last_viewed_at, which puts this group at the "
        "head of the sweeper's queue on a surface nobody pressed"
    )


def test_opening_your_own_standing_does_join_the_sweeper_queue(standing_db):
    """A press is a press. Without this, a group with no stored answer never queues."""
    _claim(standing_db)
    _remember(standing_db)
    with db._get_conn() as conn:
        conn.execute("UPDATE odds_runs SET last_viewed_at = NULL")

    _standing_of(standing_db)
    assert _viewed_at(standing_db) is not None, (
        "pressing `Your standing` never records a view, so this group stays "
        "behind every group somebody pressed the odds on"
    )


# ── the reader's own row ─────────────────────────────────────────────────────


def test_a_stored_answer_can_still_be_matched_to_one_player(standing_db):
    """#533's store rebuilt every row keyless, so no caller could find a player.

    Fails against `champion_duel_store._from_payload` as it shipped: it
    re-rendered the names and dropped `key`, which is the one field that maps a
    row back to a registrant.
    """
    rid = _claim(standing_db, which=5)
    members = _remember(standing_db)
    state = _standing_of(standing_db)

    row = hub._my_odds_row(state["stored"].odds, members, rid)
    assert row is not None, "a stored answer cannot be matched to the reader"
    assert row.name == "P05"


def test_two_players_sharing_a_name_stay_two_players(standing_db):
    """Position, never the display name. It is why `key` exists."""
    rid = _claim(standing_db, which=5)
    db.upsert_registrant(name="P05", server=STANDING_WARZONES[1], thp=400_000_000)
    members = _remember(standing_db)
    # Force the collision the engine's positional keying is there to survive.
    for member in members:
        member["display_name"] = "P05"
    answer = _answer(members)

    row = hub._my_odds_row(answer, members, rid)
    assert row is not None
    assert row.key == str(next(i for i, m in enumerate(members) if m["id"] == rid))


def test_the_standing_reads_the_readers_row_and_not_the_favorites(standing_db):
    """A group answer is sorted strongest first; the reader is rarely first."""
    _claim(standing_db, which=6)
    _remember(standing_db)
    embed = hub.build_standing_embed(_standing_of(standing_db), can_odds=True)
    worked = _field(embed, hub._STANDING_WORKED_OUT)
    assert "90%" not in worked, "the favorite's odds were rendered as the reader's"
    assert "Projected finish **7**" in worked


# ── the verdict, and why there is not one ────────────────────────────


def test_the_worked_out_half_states_the_numbers_and_passes_no_judgement(standing_db):
    """Kevin struck the verdict and the reward band on 2026-08-25.

    Four tests stood here: two on the verdict either side of a 10% cut, and two
    on `_band_for`. All four went with the constants. This one replaces them,
    because a deletion nobody guards is a deletion somebody rebuilds -- and the
    rule is wider than the strings, so it is asserted against the rendered
    field rather than against any one of them.
    """
    _claim(standing_db, which=0)
    _remember(standing_db)
    worked = _field(
        hub.build_standing_embed(_standing_of(standing_db), can_odds=True),
        hub._STANDING_WORKED_OUT,
    )
    assert hub._STANDING_THROUGH in worked
    assert "Projected finish" in worked
    for narration in ("in the running", "long shot", "band", "reward", "wins still pay"):
        assert narration not in worked.lower(), (
            f"the bot is narrating the game back at the player: {narration!r}"
        )


# ── staleness ────────────────────────────────────────────────────────────────


def test_a_stale_answer_is_only_ever_shown_under_its_timestamp(standing_db):
    """The rule `build_odds_embed` set: the caveat is the condition, not a decoration."""
    _claim(standing_db)
    members = _remember(standing_db)
    state = _standing_of(standing_db)
    stale = store_lib.Stored(
        state="stale", odds=_answer(members), computed_at="2026-08-20T10:00:00+00:00"
    )
    text = hub._standing_worked_out({**state, "stored": stale})
    assert text is not None
    assert hub._ODDS_AS_OF.split("{")[0].strip() in text


def test_a_stale_answer_with_an_unreadable_stamp_costs_the_answer(standing_db):
    """Without this, old numbers render exactly like current ones."""
    _claim(standing_db)
    members = _remember(standing_db)
    state = _standing_of(standing_db)
    stale = store_lib.Stored(state="stale", odds=_answer(members), computed_at="not a date")
    assert hub._standing_worked_out({**state, "stored": stale}) is None


def test_a_fresh_answer_never_carries_a_timestamp(standing_db):
    _claim(standing_db)
    _remember(standing_db)
    state = _standing_of(standing_db)
    assert state["stored"].state == "fresh"
    assert hub._ODDS_AS_OF.split("{")[0].strip() not in hub._standing_worked_out(state)


# ── failing softly ───────────────────────────────────────────────────────────


def test_a_store_that_raises_costs_the_odds_and_not_the_hub(standing_db, monkeypatch):
    """This is the landing. A store fault must not be every guild's `/champion_duel`."""
    _claim(standing_db)

    def _boom(*a, **k):
        raise RuntimeError("table is half-migrated")

    monkeypatch.setattr(store_lib, "lookup", _boom)
    state = _standing_of(standing_db)
    assert state["state"] == "held"
    assert state.get("stored") is None
    hub.build_standing_embed(state, can_odds=True)


def test_a_claimed_account_with_no_round_recorded_says_so(standing_db):
    """Not an error. A Champion Duel before its semifinals has no group to stand in."""
    loose = db.upsert_registrant(name="Fresh", server=STANDING_WARZONES[2], thp=300_000_000)
    db.claim_registrant(loose["id"], str(ADMIN_ID), discord_name="Kevin", guild_id="999")
    state = _standing_of(standing_db)

    assert state["state"] == "held", (
        "a claimed account with no round yet was mistaken for a claim on another "
        "Champion Duel and invited to move"
    )
    embed = hub.build_standing_embed(state, can_odds=True)
    assert hub._STANDING_NO_ROUND.split("{")[0].strip() in _field(embed, hub._STANDING_WORKED_OUT)


def test_a_projected_finish_separates_players_the_printed_odds_cannot(standing_db):
    """`words.probability` caps at `>99%`, so three leaders shared one rung.

    Fails against the first version of `_projected_place`, which counted off
    the printed advance probability: all three of these are `>99%` and all
    three were told they finish 1st of 8.
    """
    _claim(standing_db, which=2)
    members = _remember(standing_db, advance=[0.999, 0.998, 0.997, 0.4, 0.3, 0.2, 0.1, 0.05])
    state = _standing_of(standing_db)
    result = state["stored"].odds

    places = [hub._projected_place(result, r) for r in result.rows]
    assert len(set(places)) == len(places), (
        f"players the model separates share a projected finish: {sorted(places)}"
    )

    mine = hub._my_odds_row(result, members, state["player"]["id"])
    assert hub._projected_place(result, mine) == 3


def test_a_round_the_engine_cannot_run_is_not_reported_as_having_no_model(standing_db, monkeypatch):
    """`STAGES_WITH_A_MODEL` drops the knockouts on a lagging engine pin.

    Reading absence from that tuple as "this round has no model" turns an
    operator problem into a permanent-sounding claim about the round, with a
    second sentence about kill score that a 32-bracket is not ranked on.
    """
    _claim(standing_db)
    state = _standing_of(standing_db)
    monkeypatch.setattr(odds_lib, "STAGES_WITH_A_MODEL", ())

    embed = hub.build_standing_embed(state, can_odds=True)
    worked = _field(embed, hub._STANDING_WORKED_OUT)
    assert worked == hub._ENGINE_MISSING
    assert hub._STANDING_NO_MODEL.split("{")[0].strip() not in worked


def test_nothing_claims_a_reading_nobody_took(standing_db):
    """`set_placement` stamps `updated_at` on a bare membership write too."""
    rid = _claim(standing_db)
    db.set_placement(standing_db["group_id"], rid)  # membership only, no rank, no score
    embed = hub.build_standing_embed(_standing_of(standing_db), can_odds=False)
    recorded = _field(embed, hub._STANDING_RECORDED)
    assert hub._STANDING_READ_AT.split("{")[0].strip() not in recorded, (
        "the surface says when a rank was read, over a row that holds no rank"
    )


def test_the_standing_offers_the_edit_control_only_where_a_claim_is_held(standing_db):
    """`_StandingClaimView` is both the unclaimed landing and the footer of a
    standing that has one. On the landing there is no "my" to edit."""
    _claim(standing_db)
    standing = hub.read_standing(ADMIN_ID, standing_db["grouping"], with_odds=False)

    held = hub._StandingClaimView(
        user_id=ADMIN_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=standing["player"],
    )
    landing = hub._StandingClaimView(
        user_id=ADMIN_ID, can_write=True, grouping=standing_db["grouping"]
    )

    # `🏅 Your group` rides with the claim pair: it is a promise about the
    # reader, so it renders wherever we know who they are and nowhere else.
    # No odds beside it here, because this view was built with no standing to
    # say which group they are in.
    assert [getattr(i, "label", None) for i in held.children] == [
        hub.CD_BTN_GROUP,
        hub.CD_BTN_WHO_AM_I,
        hub.CD_BTN_EDIT_ME,
    ]
    assert [getattr(i, "label", None) for i in landing.children] == [hub.CD_BTN_WHO_AM_I]


def test_the_edit_control_locks_rather_than_hides_where_the_reader_cannot_write(standing_db):
    """A locked control renders disabled rather than hidden, so the shape of
    the product is visible. `can_write` is not the Premium gate -- contributing
    has been free since 2026-08-17 -- and this follows `➕ Add a player`'s own
    treatment of it either way."""
    _claim(standing_db)
    standing = hub.read_standing(ADMIN_ID, standing_db["grouping"], with_odds=False)

    view = hub._StandingClaimView(
        user_id=ADMIN_ID,
        can_write=False,
        grouping=standing_db["grouping"],
        player=standing["player"],
    )
    edit = [i for i in view.children if hub.CD_BTN_EDIT_ME in (getattr(i, "label", None) or "")]

    assert len(edit) == 1
    assert edit[0].disabled
    assert edit[0].label.startswith("🔒")


async def test_the_edit_control_reads_the_claim_when_it_is_pressed(standing_db):
    """Found by `/code-review`. Both views carrying this button live ten and
    fifteen minutes, and `ClaimResultView` can release a claim from another
    message inside that window. A snapshot taken when the message was sent
    would prefill an account the reader gave up, and then write to it.
    """
    first = _claim(standing_db, which=3)
    view = hub._StandingClaimView(
        user_id=ADMIN_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=db.get_claimed_registrant(ADMIN_ID),
    )

    moved = _claim(standing_db, which=6)
    inter = _interaction()
    await view._on_edit_me(inter)

    assert moved != first
    opened = inter.response.send_modal.await_args.args[0]
    assert opened.name.default == db.get_registrant(moved)["display_name"]
    inter.response.send_message.assert_not_awaited()


async def test_the_edit_control_refuses_where_the_claim_has_gone(standing_db):
    """Released while the hub sat on screen. The refusal is the claim module's
    own, not a second wording of it."""
    rid = _claim(standing_db)
    view = hub._StandingClaimView(
        user_id=ADMIN_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=db.get_claimed_registrant(ADMIN_ID),
    )

    db.release_claim(str(ADMIN_ID))
    inter = _interaction()
    await view._on_edit_me(inter)

    assert rid
    inter.response.send_modal.assert_not_awaited()
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.await_args.args[0] == claim_lib.CLAIM_NOT_LINKED


# ── The root, after session 6 ─────────────────────────────────────────────────
#
# `notes/PLAN_champion_duel_ia.md`, *Session 6 - The hub, and the rename*. The
# assertions here are that file's table, read as a grid.


def _root(grouping=None, **kw):
    kw.setdefault("user_id", ADMIN_ID)
    kw.setdefault("is_admin", False)
    kw.setdefault("can_write", True)
    kw.setdefault("engine_ok", True)
    # Entitled by default, so the four entries read as their four labels. The
    # padlock is a Premium question and has its own tests.
    kw.setdefault("can_intel", True)
    return hub.ChampionDuelHubView(grouping=grouping, **kw)


def _row(view, n):
    return [i.label for i in view.children if getattr(i, "row", None) == n]


def test_the_front_row_is_the_four_questions_and_nothing_else():
    """Eight controls become four entries plus settings.

    The four are the four questions `PROPOSAL_champion_duel_ia.md` traced, in
    the order it asks them, and they are the whole of the front row. Kevin
    opened this hub, could not find the most valuable thing on it, and asked
    for the information architecture to be revisited: the fix is that the four
    things anybody comes here for are the first four things they see.
    """
    view = _root(grouping={"id": 1})

    assert _row(view, 0) == [
        hub.CD_BTN_WHO_AM_I,
        hub.CD_BTN_INTEL,
        hub.CD_BTN_PICKS,
        hub.CD_BTN_ALLIANCE,
    ]


def test_the_second_row_is_looking_someone_up_contributing_and_the_settings():
    """Demoted, not deleted. Finding a player is how you reach an opponent and
    is the gap-fill door; recording a group is batch contribution; changing the
    warzone is the settings half of "four entries plus settings"."""
    view = _root(grouping={"id": 1}, warzone="738", standing={"state": "held"})

    assert _row(view, 1) == [
        hub.CD_BTN_FIND,
        hub.CD_BTN_RECORD,
        hub.CD_BTN_CHANGE_WARZONE,
    ]


def test_the_group_listing_stays_on_the_root_until_the_reader_can_reach_it():
    """Retiring a door is not the same act as taking a surface away.

    `\U0001f3c5 Your group` is retired because you get to your own group by getting
    to yourself first, and that is true the moment we know who somebody is:
    `\U0001f3c5 Your standing` carries it, opened on their own letter. Before then
    there is no standing to reach it from, and the group listing is a free read
    carrying the round picker, the alliance filter and the door to recording a
    round we hold nothing for. So the old control survives exactly as long as
    it is the only one.
    """
    unknown = _root(grouping={"id": 1}, warzone="738")
    known = _root(grouping={"id": 1}, warzone="738", standing={"state": "held"})

    assert hub.CD_BTN_GROUP in _row(unknown, 1)
    assert hub.CD_BTN_GROUP not in _labels(known)
    # And never a front-row entry either way.
    assert hub.CD_BTN_GROUP not in _row(unknown, 0)


async def test_the_root_group_door_opens_what_it_always_opened(monkeypatch):
    """No stage and no letter. We cannot place this reader in the event, so it
    opens the round the guild is playing rather than guessing at a group."""
    view = _root(grouping={"id": 1}, warzone="738")
    press = next(b for b in view.children if b.label == hub.CD_BTN_GROUP)

    seen = {}

    async def _fake(interaction, **kw):
        seen.update(kw)

    monkeypatch.setattr(hub, "send_group_view", _fake)
    await press.callback(_interaction())

    assert seen["grouping"] == {"id": 1}
    assert seen["warzone"] == "738"
    assert "stage" not in seen and "label" not in seen


@pytest.mark.parametrize("standing", [None, {"state": "held"}])
def test_no_row_is_over_discords_five(standing):
    """Five buttons a row is Discord's cap and the old grid was at it, which is
    why the picks control shipped alone on a row of its own. Both readers,
    because the unknown one carries an extra control on row 1."""
    view = _root(grouping={"id": 1}, warzone="738", is_admin=True, standing=standing)

    for n in range(5):
        assert len(_row(view, n)) <= 5, f"row {n} is over Discord's five"


def test_the_operator_row_moves_up_with_everything_else():
    """Row 2 rather than row 3, because row 2 emptied when the picks control
    joined the front row. Still hidden entirely from everybody else.

    Row 3 arrived later and stayed below it on purpose: a row that changes
    position by state is the muscle-memory cost `notes/DESIGN.md` warns about,
    and this one would move for the one person who has row 2.
    """
    view = _root(grouping={"id": 1}, is_admin=True)

    assert _row(view, 2) == [hub.CD_BTN_EDITS, hub.CD_BTN_REVERT, hub.CD_BTN_EXPORT]
    assert _row(view, 3) == [hub.CD_BTN_ADD_CD], "below the operator's, never above"


@pytest.mark.parametrize("standing", [None, {"state": "held"}])
def test_no_two_controls_on_the_grid_share_a_glyph(standing):
    """`notes/DESIGN.md` emoji rule 7: never repeat one glyph across a choice
    set, because two identical marks side by side give the eye nothing to
    navigate by.

    This grid has two claims on \U0001f3c5 now, `\U0001f3c5 Your standing` and
    `\U0001f3c5 Your group`, and they are never drawn together: knowing who the
    reader is is exactly what swaps one for the other.
    """
    view = _root(grouping={"id": 1}, warzone="738", is_admin=True, standing=standing)
    glyphs = [(b.label or "").replace("\U0001f512 ", "").split(" ", 1)[0] for b in view.children]

    assert len(glyphs) == len(set(glyphs)), glyphs


def test_the_identity_pair_still_swaps_on_the_front_row():
    """A button reading "your standing" is a promise to somebody we cannot pick
    out of a hundred rows, so the label follows the claim."""
    known = _root(grouping={"id": 1}, standing={"state": "held"})

    assert _row(known, 0)[0] == hub.CD_BTN_STANDING


def test_every_control_the_old_root_carried_is_still_reachable(standing_db):
    """Nothing is deleted. The plan moves doors, and this walks each one to the
    surface it moved to, because "eight buttons become four" is only true if
    the other four still open something."""
    view = _root(
        grouping=standing_db["grouping"],
        warzone=STANDING_WARZONES[0],
        standing={"state": "held"},
    )
    root = _labels(view)

    assert hub.CD_BTN_PREDICT not in root
    assert hub.CD_BTN_ADD not in root
    assert hub.CD_BTN_GUIDE not in root
    assert hub.CD_BTN_GROUP not in root

    # Predicting one match: on the card that absorbed it.
    picks = hub._PicksView(
        user_id=ADMIN_ID,
        guild_id=999,
        state=hub.read_picks(999, standing_db["grouping"]),
    )
    assert hub.CD_BTN_PREDICT in _labels(picks)

    # Adding a player, and the capture guide: at the miss that finding one
    # produces.
    miss = hub._MissView(can_write=True, user_id=ADMIN_ID, name="Nobody", server="738")
    assert {hub.CD_BTN_ADD, hub.CD_BTN_GUIDE} <= set(_labels(miss))

    # The group: through the reader, on their own standing.
    standing = hub._StandingClaimView(
        user_id=ADMIN_ID, can_write=True, grouping=standing_db["grouping"], player={"id": 1}
    )
    assert hub.CD_BTN_GROUP in _labels(standing)


def test_the_one_off_prediction_is_offered_where_the_card_cannot_be():
    """Predicting one match is absorbed by the day's card and stays reachable
    there for a one-off. A caller with no Champion Duel resolved has no card to
    absorb it (a DM never gets one), and predicting two players who have never
    met is exactly what that caller came for."""
    assert hub.CD_BTN_PREDICT in _labels(_root(grouping=None))
    assert hub.CD_BTN_PREDICT not in _labels(_root(grouping={"id": 1}))


def test_the_days_card_is_a_read_and_does_not_lock():
    """It is one of the four entries and the surface behind it draws its own
    write controls locked. Gating the door would deny the read to keep back the
    write, which is the opposite of the Premium rule."""
    view = _root(grouping={"id": 1}, can_write=False)
    picks = next(b for b in view.children if hub.CD_BTN_PICKS in (b.label or ""))

    assert picks.label == hub.CD_BTN_PICKS
    assert picks.disabled is False


# ── The odds, on the standing ─────────────────────────────────────────────────


def test_the_standing_carries_the_odds_press(standing_db):
    """The move the plan calls for, and the thing that closes a dead end.

    `_STANDING_NOT_WORKED_OUT` says we hold no projection for the reader's
    group. Until this the message carried one button and it was the claim, so
    there was nothing on screen that could produce one. The press computes it.
    """
    _claim(standing_db)
    standing = hub.read_standing(ADMIN_ID, standing_db["grouping"], with_odds=False)
    view = hub._StandingClaimView(
        user_id=ADMIN_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=standing["player"],
        standing=standing,
        can_odds=True,
    )

    odds = next(b for b in view.children if hub.CD_BTN_ODDS in (b.label or ""))
    assert odds.disabled is False
    assert odds.style is discord.ButtonStyle.primary
    # One primary per view (`notes/DESIGN.md`). The claim gives it up here: on
    # a standing it is the identity footer rather than the point of the message.
    assert [b.style for b in view.children].count(discord.ButtonStyle.primary) == 1


def test_the_odds_lock_rather_than_vanish_on_a_standing(standing_db):
    """The Premium rule, on the surface the embed above already applies it to
    with its own locked odds field."""
    _claim(standing_db)
    standing = hub.read_standing(ADMIN_ID, standing_db["grouping"], with_odds=False)
    view = hub._StandingClaimView(
        user_id=ADMIN_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=standing["player"],
        standing=standing,
        can_odds=False,
    )

    odds = next(b for b in view.children if hub.CD_BTN_ODDS in (b.label or ""))
    assert odds.disabled is True
    assert odds.label.startswith("🔒")


def test_the_landing_is_still_one_button_and_it_is_the_claim(standing_db):
    """Both new controls are promises to somebody we cannot pick out of a
    hundred rows, so neither renders until we know who the reader is."""
    landing = hub._StandingClaimView(
        user_id=ADMIN_ID, can_write=True, grouping=standing_db["grouping"]
    )

    assert _labels(landing) == [hub.CD_BTN_WHO_AM_I]
    assert landing.children[0].style is discord.ButtonStyle.primary


async def test_the_odds_press_runs_over_the_readers_own_group(standing_db, monkeypatch):
    """Not the guild's first group. The reader reached this through themselves,
    so the answer has to be about the group they are standing in."""
    _claim(standing_db)
    standing = hub.read_standing(ADMIN_ID, standing_db["grouping"], with_odds=False)
    view = hub._StandingClaimView(
        user_id=ADMIN_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=standing["player"],
        standing=standing,
        can_odds=True,
    )

    seen = {}

    async def _fake(interaction, *, grouping, stage, label):
        seen.update(grouping=grouping, stage=stage, label=label)

    monkeypatch.setattr(hub, "send_group_odds", _fake)
    await view._on_odds(_interaction())

    assert seen["grouping"]["id"] == standing_db["grouping"]["id"]
    assert seen["stage"] == "semifinals"
    assert seen["label"] == "A"


async def test_the_group_press_opens_on_the_readers_own_letter(standing_db, monkeypatch):
    """The case the plan says stops being group-first once you reach it through
    yourself: you never pick from a list."""
    _claim(standing_db)
    standing = hub.read_standing(ADMIN_ID, standing_db["grouping"], with_odds=False)
    view = hub._StandingClaimView(
        user_id=ADMIN_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=standing["player"],
        standing=standing,
        warzone=STANDING_WARZONES[0],
    )

    seen = {}

    async def _fake(interaction, **kw):
        seen.update(kw)

    monkeypatch.setattr(hub, "send_group_view", _fake)
    await view._on_group(_interaction())

    assert seen["stage"] == "semifinals"
    assert seen["label"] == "A"
    assert seen["grouping"]["id"] == standing_db["grouping"]["id"]


async def test_the_group_press_follows_an_account_in_another_champion_duel(standing_db):
    """`elsewhere`. Opening the guild's tournament would show somebody a group
    they are not in, under a heading about their own standing."""
    other = db.ensure_grouping([str(2000 + i) for i in range(16)], "2026-08-04")
    mine = db.upsert_registrant(name="Away", server="2000", alliance="OGV", thp=1)
    db.set_stage(mine["id"], "semifinals", grp="C", grouping_id=other["id"])
    db.claim_registrant(mine["id"], str(OUTSIDER_ID), discord_name="Away", guild_id="999")

    standing = hub.read_standing(OUTSIDER_ID, standing_db["grouping"], with_odds=False)
    view = hub._StandingClaimView(
        user_id=OUTSIDER_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=standing["player"],
        standing=standing,
    )

    assert standing["state"] == "elsewhere"
    assert (await view._their_grouping())["id"] == other["id"]


async def test_the_group_press_takes_the_readers_warzone_out_of_this_server(
    standing_db, monkeypatch
):
    """The parsing prior follows the group, not the guild.

    `warzone` is only what the record modal on that surface uses to decide
    which number on a pasted line is the warzone. This guild's number is the
    wrong prior for a paste out of a Champion Duel this guild is not in.
    """
    other = db.ensure_grouping([str(3000 + i) for i in range(16)], "2026-08-04")
    mine = db.upsert_registrant(name="Faraway", server="3000", alliance="ZZQ", thp=1)
    db.set_stage(mine["id"], "semifinals", grp="D", grouping_id=other["id"])
    db.claim_registrant(mine["id"], str(OUTSIDER_ID), discord_name="Faraway", guild_id="999")
    standing = hub.read_standing(
        OUTSIDER_ID, standing_db["grouping"], warzone=STANDING_WARZONES[0], with_odds=False
    )
    view = hub._StandingClaimView(
        user_id=OUTSIDER_ID,
        can_write=True,
        grouping=standing_db["grouping"],
        player=standing["player"],
        standing=standing,
        warzone=STANDING_WARZONES[0],
    )

    seen = {}

    async def _fake(interaction, **kw):
        seen.update(kw)

    monkeypatch.setattr(hub, "send_group_view", _fake)
    await view._on_group(_interaction(OUTSIDER_ID))

    assert seen["grouping"]["id"] == other["id"]
    assert seen["warzone"] == "3000"
    assert seen["label"] == "D"


async def test_the_group_view_opens_on_a_letter_it_is_given(standing_db):
    """A hint rather than an instruction, both ways: a letter we no longer hold
    falls back to what the surface would have opened on anyway, because a stale
    pointer must not strand a live view."""
    inter = _interaction()

    await hub.send_group_view(
        inter,
        grouping=standing_db["grouping"],
        warzone=STANDING_WARZONES[0],
        user_id=ADMIN_ID,
        stage="semifinals",
        label="Z",
    )

    view = inter.followup.send.await_args.kwargs["view"]
    assert view.label == "A", "an unheld letter falls back rather than stranding the view"

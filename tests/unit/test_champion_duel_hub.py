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
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

import champion_duel_db as db
import champion_duel_hub as hub

ADMIN_ID = 111
OUTSIDER_ID = 222

KEV = {"discord_user_id": str(ADMIN_ID), "discord_name": "Kevin", "guild_id": "999"}


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
    product (`notes/DESIGN.md`)."""
    view = hub.ChampionDuelHubView(
        user_id=OUTSIDER_ID, is_admin=False, can_write=False, engine_ok=True
    )
    locked = [b for b in view.children if hub.CD_BTN_ADD in (b.label or "")]
    assert locked, "the add button should still be on the grid"
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
    assert {hub.CD_BTN_FIND, hub.CD_BTN_ADD} <= set(labels)

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
    assert on_card == [hub.CD_BTN_SQUADS, hub.CD_BTN_ORDER]
    assert not hasattr(hub, "_SquadModal")


def test_the_player_card_locks_its_actions_on_the_free_tier():
    view = hub.PlayerActionsView(
        player={"id": 1, "display_name": "AlphaOne", "server": "738"},
        user_id=OUTSIDER_ID,
        can_write=False,
    )
    assert all(b.disabled for b in view.children)
    assert all(b.label.startswith("🔒") for b in view.children)


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
        if f.name == "Rounds"
    )
    assert "Group D (not your Champion Duel)" in rounds

    # Inside the caller's own grouping it stays bare, because there it is exact.
    theirs_view = next(
        f.value
        for f in hub.build_player_embed(player, None, grouping=theirs).fields
        if f.name == "Rounds"
    )
    assert "Group D" in theirs_view and "different Champion Duel" not in theirs_view


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
    assert [b.label for b in view.children] == [hub.CD_BTN_ADD]
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


def test_the_capture_guide_is_never_locked():
    """Documentation, not a paid surface. Someone deciding whether to pay
    should be able to see what contributing involves, and withholding a picture
    of a game screen protects nothing."""
    view = hub.ChampionDuelHubView(
        user_id=OUTSIDER_ID, is_admin=False, can_write=False, engine_ok=False
    )
    guide = [b for b in view.children if b.label == hub.CD_BTN_GUIDE]
    assert guide, "the guide button should be on the grid"
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
    invitation rather than a rejection."""
    embed = hub.build_hub_embed(servers=db.get_servers(), can_write=True)

    assert "don't have data from your warzone" in embed.description
    # Named by its words: the button's leading emoji is a near-black glyph that
    # disappears against the embed background.
    assert "**Add a player**" in embed.description
    assert "➕" not in embed.description


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

    order_field = next(f.value for f in embed.fields if f.name == "Most common order")
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
    assert names.index("Squads") < names.index("Rounds")
    assert "Group" in next(f.value for f in embed.fields if f.name == "Rounds")


# ── Record an order ───────────────────────────────────────────────────────────


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

    rounds = next(f.value for f in embed.fields if f.name == "Rounds")
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


async def test_a_finished_champion_duel_keeps_its_results_and_offers_the_next(cd_db, no_mm_link):
    from datetime import timedelta

    db.set_guild_warzone("999", "738", confirmed_grouping_id=db.default_grouping_id())
    over = (db._server_today() - timedelta(days=db.EVENT_DAYS + 1)).isoformat()
    with db._get_conn() as conn:
        conn.execute("UPDATE groupings SET started_on = ?", (over,))

    interaction = _interaction()
    await hub._open_hub(interaction, can_write=True)

    view = _view(interaction)
    assert isinstance(view, hub.ChampionDuelFinishedView)
    said = _embed(interaction).description
    assert "has finished" in said
    assert "**738**" in said, "whose Champion Duel this was"
    # The offer has to survive the gap before the next draw is visible in game,
    # so it states the condition rather than an instruction nobody can act on.
    assert "When the next Champion Duel happens" in said
    # Recording past results is the other half: the data is still worth having
    # once the event is over, and that is not obvious without being told.
    assert "record past Champion Duel results" in said
    assert hub.CD_BTN_ADD_GROUPING in _labels(view)
    # Predict and Find are global and useful between events.
    assert hub.CD_BTN_PREDICT in _labels(view)


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

"""`/champion_duel` hub — who sees which buttons, and what the flows behind
them do.

The modal and view bodies are exercised through their callbacks with a faked
interaction, following the repo's pattern of calling `task_name.coro(...)`
directly rather than standing up a gateway.
"""

from __future__ import annotations

import csv
import io
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
        ]
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
    assert hub.CD_BTN_SQUAD not in labels
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
    assert on_card == [hub.CD_BTN_SQUAD, hub.CD_BTN_ORDER]


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
    modal.group._value = "N"
    modal.alliance._value = "OGV"

    interaction = _interaction()
    await modal.on_submit(interaction)

    player = db.get_player("Newcomer", server="1042")
    assert player["origin"] == "self_reported"
    assert player["grp"] == "N" and player["alliance"] == "OGV"
    assert player["added_by"] == str(ADMIN_ID)
    # Lands on the card with the write actions, not a bare confirmation.
    assert isinstance(interaction.followup.send.call_args.kwargs["view"], hub.PlayerActionsView)
    assert "Added" in _sent(interaction)


async def test_adding_someone_we_already_have_opens_them(cd_db):
    """Not an error and not a duplicate — identity is (name, server), so this
    is the same person. Saying so beats a refusal the contributor has to
    interpret."""
    modal = hub._AddPlayerModal(can_write=True)
    modal.name._value = "AlphaOne"
    modal.server._value = "738"
    modal.group._value = ""
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
    modal.group._value = ""
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
    db.import_registrants([{"name": "Ultra Zaddy", "group": "M", "rank": 9, "server": "677"}])

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


async def test_a_missing_player_points_at_adding_them(cd_db):
    """A name we don't have is usually a real player we haven't met, so the
    miss carries its own exit."""
    modal = hub._FindPlayerModal(can_write=True)
    modal.name._value = "NobodyAtAll"
    modal.server._value = ""
    interaction = _interaction()
    await modal.on_submit(interaction)
    # By its words: ➕ is near-black against a Discord message background, so
    # naming the button in prose drops the icon and lets bold do the work.
    assert hub._btn_words(hub.CD_BTN_ADD) in _sent(interaction)
    assert "➕" not in _sent(interaction)


async def test_the_write_modals_do_not_ask_who_again(cd_db):
    """The player came from the card, so the squad modal is three fields and
    the order modal is one — and neither can fail on an ambiguous name."""
    player = db.get_player("AlphaOne", server="738")
    squad_fields = {i.label for i in hub._SquadModal(player).children}
    assert "Player name" not in squad_fields
    assert squad_fields == {"Slot (1, 2 or 3)", "Squad type", "Power"}

    order_fields = {i.label for i in hub._OrderModal(player).children}
    assert order_fields == {"Who they faced"}


async def test_a_squad_correction_applies_to_the_card_player(cd_db):
    player = db.get_player("AlphaOne", server="738")
    modal = hub._SquadModal(player)
    modal.slot._value = "1"
    modal.squad_type._value = "Tank"
    modal.power._value = "84.6M"

    interaction = _interaction()
    await modal.on_submit(interaction)

    squad = db.get_player("AlphaOne", server="738", include_scouting=True)["squads"][0]
    assert squad["squad_type"] == "Tank" and squad["power"] == 84_600_000
    assert "84,600,000" in _sent(interaction)


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


def test_hub_embed_names_the_gate_it_applied(cd_db):
    embed = hub.build_hub_embed(servers=db.get_servers(), can_write=False)
    names = [f.name for f in embed.fields]
    assert any("Premium" in n for n in names)
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

    assert "don't have data from your server" in embed.description
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
    assert hub.CD_BTN_SQUAD in msg, "a dead end has to name its exit"
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
    db.import_registrants([{"name": "AlphaOne", "group": "N", "rank": 4, "server": "1042"}])
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


def test_player_embed_marks_estimates_apart_from_sightings(cd_db):
    rid = _reg("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=1_000, actor=KEV, source="estimated")
    db.set_squad(rid, 2, squad_type="Missile", power=900, actor=KEV, source="observed")
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    embed = hub.build_player_embed(player, None)
    squads = next(f.value for f in embed.fields if f.name == "Squads")
    assert hub._SOURCE_MARK["estimated"] in squads
    assert hub._SOURCE_MARK["observed"] in squads


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

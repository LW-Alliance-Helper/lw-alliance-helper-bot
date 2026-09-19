"""Claiming: the link between a Discord account and a recorded account.

A registrant is an **account**, not a person. Accounts change hands, so a claim
is a present-tense statement that moves when the person moves it, and almost
every test here is really about that one sentence: there is no history, no
merge, no transfer detection, and no release when somebody leaves a server.

The player names are invented. This repository is public and roster data never
enters it, not even as a fixture.
"""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

import champion_duel_claim as claim_lib
import champion_duel_db as db
import champion_duel_hub as hub

ALEX = "4001"
SAM = "4002"


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def _player(name, server="738", **fields):
    return db.upsert_registrant(name, server=server, origin="imported", **fields)


def _interaction(user_id=int(ALEX)):
    """A stand-in for discord.Interaction covering only what claiming touches."""
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.user.display_name = "Someone"
    interaction.guild_id = 999
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    interaction.original_response = AsyncMock(return_value=MagicMock())
    return interaction


def _sent(interaction):
    call = interaction.followup.send.call_args
    if call.args:
        return call.args[0]
    return call.kwargs.get("content") or ""


def _view(interaction):
    return interaction.followup.send.call_args.kwargs.get("view")


def _labels(view):
    return [item.label for item in view.children if getattr(item, "label", None)]


# ── The link itself ───────────────────────────────────────────────────────────


def test_a_claim_links_one_discord_account_to_one_recorded_account(cd_db):
    kestrel = _player("Kestrel")

    result = db.claim_registrant(kestrel["id"], ALEX, discord_name="Alex")

    assert result["changed"] is True
    assert result["moved_from"] is None
    assert result["claim"]["registrant_id"] == kestrel["id"]
    assert result["claim"]["discord_user_id"] == ALEX
    assert db.get_claim(kestrel["id"])["discord_user_id"] == ALEX


def test_the_claimed_account_is_reachable_from_the_discord_id(cd_db):
    """Session 2 opens the hub on the person, so the lookup that matters is
    from the caller to their row rather than the other way around."""
    kestrel = _player("Kestrel", thp=300_000_000.0)
    db.claim_registrant(kestrel["id"], ALEX)

    mine = db.get_claimed_registrant(ALEX)

    assert mine["display_name"] == "Kestrel"
    assert mine["thp"] == 300_000_000.0
    assert mine["claim"]["discord_user_id"] == ALEX


def test_nobody_claimed_is_none_rather_than_an_error(cd_db):
    assert db.get_claimed_registrant(SAM) is None
    assert db.get_claimed_registrant("") is None
    assert db.get_claim(_player("Kestrel")["id"]) is None


def test_two_people_hold_two_different_accounts(cd_db):
    kestrel = _player("Kestrel")
    harrier = _player("Harrier")

    db.claim_registrant(kestrel["id"], ALEX)
    db.claim_registrant(harrier["id"], SAM)

    assert db.get_claimed_registrant(ALEX)["display_name"] == "Kestrel"
    assert db.get_claimed_registrant(SAM)["display_name"] == "Harrier"


def test_claiming_something_that_does_not_exist_is_a_lookup_error(cd_db):
    """Distinct from the refusal on purpose. One is "not here", the other is
    "here and somebody else's", and they take different exits."""
    with pytest.raises(LookupError):
        db.claim_registrant(9999, ALEX)


def test_a_claim_needs_a_discord_id(cd_db):
    kestrel = _player("Kestrel")
    with pytest.raises(ValueError):
        db.claim_registrant(kestrel["id"], "   ")


# ── A second claim is refused ─────────────────────────────────────────────────


def test_a_second_person_cannot_take_a_held_account(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)

    with pytest.raises(db.ClaimRefused) as exc:
        db.claim_registrant(kestrel["id"], SAM)

    assert exc.value.registrant_id == kestrel["id"]
    assert exc.value.holder["discord_user_id"] == ALEX
    # And the refusal changed nothing.
    assert db.get_claim(kestrel["id"])["discord_user_id"] == ALEX
    assert db.get_claimed_registrant(SAM) is None


def test_pressing_your_own_claim_again_reports_no_change(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX, discord_name="Alex")

    again = db.claim_registrant(kestrel["id"], ALEX, discord_name="Alex Renamed")

    assert again["changed"] is False
    assert again["moved_from"] is None
    # The Discord display name still follows them: people rename, and freezing
    # the audit trail at whatever it said the first time helps nobody.
    assert again["claim"]["discord_name"] == "Alex Renamed"
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM registrant_claims").fetchone()[0] == 1


# ── Accounts change hands ─────────────────────────────────────────────────────


def test_moving_a_claim_repoints_the_same_row_and_frees_the_old_account(cd_db):
    """A warzone transfer, an alliance move and buying a stronger account are
    one mechanism, and this is it. The account left behind must be free, since
    somebody else may now be playing it."""
    old = _player("Kestrel", server="738")
    new = _player("Kestrel", server="1500")

    db.claim_registrant(old["id"], ALEX)
    moved = db.claim_registrant(new["id"], ALEX)

    assert moved["changed"] is True
    assert moved["moved_from"] == old["id"]
    assert db.get_claim(old["id"]) is None
    assert db.get_claimed_registrant(ALEX)["server"] == "1500"
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM registrant_claims").fetchone()[0] == 1


def test_a_moved_claim_keeps_when_it_was_first_made(cd_db):
    """An update rather than a delete and an insert, so "claimed since" is not
    reset by a transfer and there is no window where the person holds nothing."""
    old = _player("Kestrel", server="738")
    new = _player("Kestrel", server="1500")

    first = db.claim_registrant(old["id"], ALEX)["claim"]
    moved = db.claim_registrant(new["id"], ALEX)["claim"]

    assert moved["id"] == first["id"]
    assert moved["created_at"] == first["created_at"]


def test_the_account_somebody_left_can_be_claimed_by_whoever_plays_it_now(cd_db):
    old = _player("Kestrel", server="738")
    new = _player("Kestrel", server="1500")
    db.claim_registrant(old["id"], ALEX)
    db.claim_registrant(new["id"], ALEX)

    db.claim_registrant(old["id"], SAM)

    assert db.get_claim(old["id"])["discord_user_id"] == SAM


def test_releasing_frees_the_account_and_says_what_it_freed(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)

    released = db.release_claim(ALEX)

    assert released["registrant_id"] == kestrel["id"]
    assert db.get_claim(kestrel["id"]) is None
    assert db.get_claimed_registrant(ALEX) is None
    # And anyone can take it now.
    db.claim_registrant(kestrel["id"], SAM)
    assert db.get_claim(kestrel["id"])["discord_user_id"] == SAM


def test_releasing_nothing_is_not_an_error(cd_db):
    assert db.release_claim(ALEX) is None
    assert db.release_claim("") is None


def test_a_claim_never_writes_to_the_player_record(cd_db):
    """The record is a tournament account contributed across alliances. Saying
    who plays it is not an edit to it."""
    kestrel = _player("Kestrel", alliance="OGV", thp=250_000_000.0)
    before = db.get_registrant(kestrel["id"])

    db.claim_registrant(kestrel["id"], ALEX)
    db.release_claim(ALEX)

    assert db.get_registrant(kestrel["id"]) == before
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0


# ── The constraints actually bite ─────────────────────────────────────────────
#
# SQLite treats NULLs in a UNIQUE as distinct, so a nullable column enforces
# nothing. Both columns are NOT NULL for that reason and these two tests are
# what stops a later migration quietly making one nullable again.


def test_the_schema_refuses_two_claims_on_one_account(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)

    with pytest.raises(sqlite3.IntegrityError), db._get_conn() as conn:
        conn.execute(
            "INSERT INTO registrant_claims "
            "(registrant_id, discord_user_id, created_at, updated_at) VALUES (?,?,?,?)",
            (kestrel["id"], SAM, "2026-08-25T00:00:00Z", "2026-08-25T00:00:00Z"),
        )


def test_the_schema_refuses_two_claims_by_one_person(cd_db):
    kestrel = _player("Kestrel")
    harrier = _player("Harrier")
    db.claim_registrant(kestrel["id"], ALEX)

    with pytest.raises(sqlite3.IntegrityError), db._get_conn() as conn:
        conn.execute(
            "INSERT INTO registrant_claims "
            "(registrant_id, discord_user_id, created_at, updated_at) VALUES (?,?,?,?)",
            (harrier["id"], ALEX, "2026-08-25T00:00:00Z", "2026-08-25T00:00:00Z"),
        )


def test_deleting_a_player_takes_their_claim_with_it(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)

    with db._get_conn() as conn:
        conn.execute("DELETE FROM registrants WHERE id = ?", (kestrel["id"],))

    assert db.get_claimed_registrant(ALEX) is None


def test_claims_for_reads_a_hundred_rows_in_one_query(cd_db):
    """The alliance and group listings render up to a hundred players, and a
    per-row lookup there is a hundred round trips for a marker beside a name."""
    players = [_player(f"Player{n:03d}") for n in range(5)]
    db.claim_registrant(players[1]["id"], ALEX)
    db.claim_registrant(players[3]["id"], SAM)

    found = db.claims_for([p["id"] for p in players])

    assert set(found) == {players[1]["id"], players[3]["id"]}
    assert found[players[1]["id"]]["discord_user_id"] == ALEX
    assert db.claims_for([]) == {}


# ── Removal takes the link, and only the link ─────────────────────────────────


def test_forgetting_a_person_deletes_their_claim(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)

    result = db.purge_user_data(ALEX, apply=True)

    assert result["deleted"]["registrant_claims"] == 1
    assert db.get_claim(kestrel["id"]) is None


def test_forgetting_a_person_leaves_the_account_they_played(cd_db):
    """#499: Champion Duel player records are out of scope for a removal. The
    account is a tournament entrant other alliances contributed readings on."""
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)

    db.purge_user_data(ALEX, apply=True)

    assert db.get_registrant(kestrel["id"])["display_name"] == "Kestrel"


def test_forgetting_one_person_leaves_everybody_elses_claim(cd_db):
    kestrel = _player("Kestrel")
    harrier = _player("Harrier")
    db.claim_registrant(kestrel["id"], ALEX)
    db.claim_registrant(harrier["id"], SAM)

    db.purge_user_data(ALEX, apply=True)

    assert db.get_claim(harrier["id"])["discord_user_id"] == SAM


def test_the_preview_counts_the_claim_without_removing_it(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)

    preview = db.purge_user_data(ALEX, apply=False)

    assert preview["deleted"]["registrant_claims"] == 1
    assert preview["applied"] is False
    assert db.get_claim(kestrel["id"]) is not None


def test_a_claim_is_deleted_rather_than_scrubbed(cd_db):
    """Every other Discord id in this database is attribution on a reading, and
    the reading outlives its author. A claim is nothing but the person, and a
    scrubbed one would hold the account against nobody forever."""
    assert "registrant_claims" in {t for t, _ in db._REMOVAL_DELETES}
    assert "registrant_claims" not in {t for t, _, _ in db._REMOVAL_SCRUBS}


# ── The control on a player card ──────────────────────────────────────────────


def test_a_player_card_offers_the_claim(cd_db):
    kestrel = _player("Kestrel")
    view = hub.PlayerActionsView(
        player=kestrel, user_id=int(ALEX), can_write=True, claim=db.get_claim(kestrel["id"])
    )
    assert claim_lib.CLAIM_BTN in _labels(view)


def test_your_own_row_offers_the_release_instead(cd_db):
    """`notes/DESIGN.md`: the label says what the control does. "This is my
    account" on a row that is already yours describes a press that cannot
    happen."""
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)

    view = hub.PlayerActionsView(
        player=kestrel, user_id=int(ALEX), can_write=True, claim=db.get_claim(kestrel["id"])
    )

    assert claim_lib.CLAIM_RELEASE_BTN in _labels(view)
    assert claim_lib.CLAIM_BTN not in _labels(view)


def test_somebody_elses_row_still_offers_the_claim(cd_db):
    """Hiding it would leave a person who really did take over that account
    with nothing to press. The refusal is what names the route to support."""
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], SAM)

    view = hub.PlayerActionsView(
        player=kestrel, user_id=int(ALEX), can_write=True, claim=db.get_claim(kestrel["id"])
    )

    assert claim_lib.CLAIM_BTN in _labels(view)


def test_the_claim_is_never_locked(cd_db):
    """Contributing is free and this is not even a contribution: it says who
    the reader is, not what they saw."""
    kestrel = _player("Kestrel")
    view = hub.PlayerActionsView(player=kestrel, user_id=int(ALEX), can_write=False, claim=None)

    claim_button = next(i for i in view.children if i.label == claim_lib.CLAIM_BTN)
    assert claim_button.disabled is False
    # Every other control on the card wears the padlock on that tier.
    assert any(str(i.label).startswith("🔒") for i in view.children)


def test_pressing_it_claims_and_offers_the_way_back_out(cd_db):
    kestrel = _player("Kestrel")
    view = hub.PlayerActionsView(player=kestrel, user_id=int(ALEX), can_write=True, claim=None)
    button = next(i for i in view.children if i.label == claim_lib.CLAIM_BTN)
    interaction = _interaction()

    asyncio.run(button.callback(interaction))

    assert "Kestrel" in _sent(interaction)
    assert db.get_claim(kestrel["id"])["discord_user_id"] == ALEX
    assert claim_lib.CLAIM_RELEASE_BTN in _labels(_view(interaction))


def test_pressing_the_release_gives_the_account_up(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)
    view = hub.PlayerActionsView(
        player=kestrel, user_id=int(ALEX), can_write=True, claim=db.get_claim(kestrel["id"])
    )
    button = next(i for i in view.children if i.label == claim_lib.CLAIM_RELEASE_BTN)
    interaction = _interaction()

    asyncio.run(button.callback(interaction))

    assert "Kestrel" in _sent(interaction)
    assert db.get_claim(kestrel["id"]) is None


def test_releasing_a_claim_that_is_already_gone_says_so(cd_db):
    """A stale message can still be pressed after the claim moved elsewhere.

    One of the two states behind `CLAIM_NOT_LINKED`, which Kevin collapsed into
    a single sentence on 2026-08-25. The other is
    `test_a_stale_release_button_does_not_give_up_a_different_account`: the two
    read the same to whoever pressed the button and are still two branches."""
    kestrel = _player("Kestrel")
    interaction = _interaction()

    asyncio.run(claim_lib.release(interaction))

    assert _sent(interaction) == claim_lib.CLAIM_NOT_LINKED
    assert db.get_claim(kestrel["id"]) is None


def test_the_refusal_points_at_support_and_names_no_holder(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], SAM, discord_name="Sam")
    interaction = _interaction()

    asyncio.run(claim_lib.claim(interaction, kestrel))

    message = _sent(interaction)
    assert "Kestrel" in message
    assert hub.COMMUNITY_SERVER_NAME in message
    # Who holds it is for support to look up, not for anybody who can guess a
    # name to read off a refusal.
    assert SAM not in message
    assert "Sam" not in message
    assert hub.COMMUNITY_SERVER_URL in [b.url for b in _view(interaction).children if b.url]


def test_a_moved_claim_frees_the_old_account_without_naming_it(cd_db):
    """`CLAIM_MOVED` carried a second sentence saying the account they left is
    free, and Kevin cut it on 2026-08-25. The release itself is not copy and
    still happens, so the assertion is on the record rather than on the
    wording: what went is the telling."""
    old = _player("Kestrel", server="738")
    new = _player("Harrier", server="1500")
    db.claim_registrant(old["id"], ALEX)
    interaction = _interaction()

    asyncio.run(claim_lib.claim(interaction, new))

    assert _sent(interaction) == claim_lib.CLAIM_MOVED.format(player="Harrier (#1500)")
    assert db.get_claim(old["id"]) is None
    assert db.get_claim(new["id"])["discord_user_id"] == ALEX


def test_claiming_your_own_row_twice_reports_no_change(cd_db):
    kestrel = _player("Kestrel")
    db.claim_registrant(kestrel["id"], ALEX)
    interaction = _interaction()

    asyncio.run(claim_lib.claim(interaction, kestrel))

    assert _sent(interaction) == claim_lib.CLAIM_ALREADY_YOURS.format(player="Kestrel (#738)")


def test_a_row_that_vanished_between_the_card_and_the_press(cd_db):
    kestrel = _player("Kestrel")
    with db._get_conn() as conn:
        conn.execute("DELETE FROM registrants WHERE id = ?", (kestrel["id"],))
    interaction = _interaction()

    asyncio.run(claim_lib.claim(interaction, kestrel))

    assert "Kestrel" in _sent(interaction)
    assert db.get_claimed_registrant(ALEX) is None


# ── The words the control and the reply share ─────────────────────────────────


def test_the_button_and_its_acknowledgements_say_the_same_thing():
    """Signed off 2026-08-25 as a pair, which is the rule Kevin set on the head
    to head modal: pressing the control and reading what comes back use the
    same words. Rewording one of them means rewording the other."""
    assert claim_lib.CLAIM_BTN.endswith("This is my account")
    for acknowledgement in (
        claim_lib.CLAIM_DONE,
        claim_lib.CLAIM_MOVED,
        claim_lib.CLAIM_ALREADY_YOURS,
    ):
        assert "your account" in acknowledgement

    assert claim_lib.CLAIM_RELEASE_BTN.endswith("This is no longer my account")
    assert "is no longer your account" in claim_lib.CLAIM_RELEASED


# ── Picking yourself out by name ──────────────────────────────────────────────


def test_the_modal_asks_for_a_name_and_a_warzone_and_nothing_else(cd_db):
    """Total Hero Power and an alliance tag are what a claimed account needs to
    be useful, which is a different gate. Requiring them here would leave
    somebody who cannot answer stuck outside their own standing."""
    modal = claim_lib.ClaimModal()
    labels = [getattr(i, "label", None) or getattr(i, "text", None) for i in modal.children]
    assert labels == [claim_lib.CLAIM_FIELD_NAME, claim_lib.CLAIM_FIELD_SERVER]


def test_the_modal_needs_both_halves_of_the_identity(cd_db):
    modal = claim_lib.ClaimModal()
    modal.name._value = "Kestrel"
    modal.server._value = ""
    interaction = _interaction()

    asyncio.run(modal.on_submit(interaction))

    assert _sent(interaction) == claim_lib.CLAIM_NEEDS_BOTH


def test_the_modal_claims_the_player_it_resolves(cd_db):
    kestrel = _player("Kestrel")
    modal = claim_lib.ClaimModal()
    modal.name._value = "Kestrel"
    modal.server._value = "738"
    interaction = _interaction()

    asyncio.run(modal.on_submit(interaction))

    assert db.get_claim(kestrel["id"])["discord_user_id"] == ALEX


def test_a_name_we_do_not_hold_takes_the_same_exit_find_does(cd_db):
    """Not a second wording of the same miss. Somebody genuinely absent adds
    themselves and lands on their own card, where the button is waiting."""
    _player("Kestrel")
    modal = claim_lib.ClaimModal()
    modal.name._value = "Nobody"
    modal.server._value = "738"
    interaction = _interaction()

    asyncio.run(modal.on_submit(interaction))

    assert isinstance(_view(interaction), hub._MissView)
    assert hub.CD_BTN_ADD in _labels(_view(interaction))


# ── What the plan asked for and the schema already had ────────────────────────
#
# The plan's session 1 lists "enforce unique in-game names" as a schema-level
# change that has to come first. It is already there and has been since
# identity moved to (name, server); these pin the behaviour so the next reader
# does not add a second constraint on top of it.


def test_identity_is_name_and_warzone_together(cd_db):
    with db._get_conn() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='registrants'"
        ).fetchone()["sql"]
    assert "UNIQUE (player_key, server)" in sql


def test_the_same_name_on_two_warzones_is_two_players(cd_db):
    a = _player("Kestrel", server="738")
    b = _player("Kestrel", server="1500")
    assert a["id"] != b["id"]


def test_a_name_with_no_warzone_still_cannot_duplicate(cd_db):
    """The UNIQUE cannot catch this one: SQLite treats NULL servers as
    distinct, so two rows would both be legal. `upsert_registrant` closes it in
    Python with `server IS ?`, and this is what says so."""
    first = db.upsert_registrant("Kestrel", server=None)
    second = db.upsert_registrant("Kestrel", server=None)

    assert first["id"] == second["id"]
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM registrants").fetchone()[0] == 1


# ── Two presses at once ───────────────────────────────────────────────────────
#
# The decision is read-then-write across three SELECTs and one write, and the
# reads are outside a transaction. Two presses landing together both read
# "nobody holds this" and both reach the INSERT, so the loser hits a UNIQUE.
# It must come back as the refusal this feature is built on, never as a raw
# IntegrityError: `champion_duel_claim.claim` catches ClaimRefused and
# NoSuchRegistrant, so anything else leaves the member on a spinner forever.


def _race(monkeypatch, interloper):
    """Make the first pass lose: somebody else's claim lands, then the write
    fails the way SQLite would fail it."""
    real = db._claim_once
    state = {"first": True}

    def _wrapper(conn, registrant_id, sid, **kwargs):
        if state["first"]:
            state["first"] = False
            interloper()
            raise sqlite3.IntegrityError("UNIQUE constraint failed")
        return real(conn, registrant_id, sid, **kwargs)

    monkeypatch.setattr(db, "_claim_once", _wrapper)


def test_a_lost_race_comes_back_as_the_refusal_not_a_crash(cd_db, monkeypatch):
    kestrel = _player("Kestrel")
    _race(monkeypatch, lambda: db.claim_registrant(kestrel["id"], SAM))

    with pytest.raises(db.ClaimRefused):
        db.claim_registrant(kestrel["id"], ALEX)

    assert db.get_claim(kestrel["id"])["discord_user_id"] == SAM


def test_racing_against_yourself_settles_as_no_change(cd_db, monkeypatch):
    """A double press. The second pass sees the first one's row and reports
    what is true rather than inventing a change."""
    kestrel = _player("Kestrel")
    _race(monkeypatch, lambda: db.claim_registrant(kestrel["id"], ALEX))

    result = db.claim_registrant(kestrel["id"], ALEX)

    assert result["changed"] is False
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM registrant_claims").fetchone()[0] == 1


def test_a_second_failure_is_not_swallowed(cd_db, monkeypatch):
    """One retry, not a loop. A UNIQUE that survives a settled read is a bug
    and has to reach Sentry rather than be absorbed."""
    kestrel = _player("Kestrel")

    def _always_fails(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(db, "_claim_once", _always_fails)

    with pytest.raises(sqlite3.IntegrityError):
        db.claim_registrant(kestrel["id"], ALEX)


def test_a_missing_account_raises_its_own_class(cd_db):
    """`KeyError` and `IndexError` are `LookupError` too, so the surface has to
    be able to catch this exact condition rather than the base class."""
    assert issubclass(db.NoSuchRegistrant, LookupError)
    with pytest.raises(db.NoSuchRegistrant):
        db.claim_registrant(9999, ALEX)


# ── A card that has gone stale ────────────────────────────────────────────────


def test_a_stale_release_button_does_not_give_up_a_different_account(cd_db):
    """The card lives ten minutes. Releasing is keyed on the caller, so acting
    on their current claim would surrender an account this card never named."""
    kestrel = _player("Kestrel")
    harrier = _player("Harrier")
    db.claim_registrant(kestrel["id"], ALEX)
    view = hub.PlayerActionsView(
        player=kestrel, user_id=int(ALEX), can_write=True, claim=db.get_claim(kestrel["id"])
    )
    button = next(i for i in view.children if i.label == claim_lib.CLAIM_RELEASE_BTN)

    # Meanwhile, in another message, they move to a different account.
    db.claim_registrant(harrier["id"], ALEX)
    interaction = _interaction()
    asyncio.run(button.callback(interaction))

    assert _sent(interaction) == claim_lib.CLAIM_NOT_LINKED
    # And nothing moved: they still hold Harrier, and Kestrel is still free.
    assert db.get_claimed_registrant(ALEX)["display_name"] == "Harrier"
    assert db.get_claim(kestrel["id"]) is None


def test_a_stale_claim_button_reports_rather_than_releasing(cd_db):
    """The mirror image: drawn while the account was free, pressed after they
    took it. A button saying "this is my account" must never hand it back."""
    kestrel = _player("Kestrel")
    view = hub.PlayerActionsView(player=kestrel, user_id=int(ALEX), can_write=True, claim=None)
    button = next(i for i in view.children if i.label == claim_lib.CLAIM_BTN)

    db.claim_registrant(kestrel["id"], ALEX)
    interaction = _interaction()
    asyncio.run(button.callback(interaction))

    assert _sent(interaction) == claim_lib.CLAIM_ALREADY_YOURS.format(player="Kestrel (#738)")
    assert db.get_claim(kestrel["id"])["discord_user_id"] == ALEX


# ── The read side session 2 and 4 build on ────────────────────────────────────


def test_the_claimed_account_carries_its_rounds_like_any_other_player(cd_db):
    """Shaped like `get_player`, not like a bare row. A standing surface reads
    `stages` and `grp`, and a shape that looks like a player everywhere else in
    the file must not be the one that is missing them."""
    db.import_registrants(
        [{"name": "Kestrel", "group": "M", "rank": 4, "server": "738"}],
        stage="qualifiers",
    )
    kestrel = db.resolve_registrant("Kestrel", server="738")
    db.claim_registrant(kestrel["id"], ALEX)

    mine = db.get_claimed_registrant(ALEX)

    assert "stages" in mine
    assert mine["stages"]["qualifiers"]["grp"] == "M"

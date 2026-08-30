"""The day's picks: the slate the schema holds and the card it turns into.

Two things can be wrong here rather than merely awkward. The stored slate can
lose half a card when somebody re-edits it, or show the same meeting twice, or
grow without a bound on a volume that never gives blocks back. And the scored
slate can quietly answer a different question than the one the reader is
betting on: `predict_pair` takes `best_of` with no default precisely because a
number at the wrong series length is still a plausible number, and a slate
applies that argument twenty times from one place.

**A slate is not a group's card.** It is a set of meetings somebody chose, for
a day, identified by the guild that built it. Most of what these tests pin is
what survived that change and what deliberately did not.

Players here are invented. The bot repo is public and no roster name goes in it.
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

import champion_duel_db as db
import champion_duel_picks as picks
import champion_duel_predict as cdp

GUILD = "999"
OTHER_GUILD = "1000"

ACTOR = {"discord_user_id": "111", "discord_name": "Willow", "guild_id": GUILD}
OTHER = {"discord_user_id": "222", "discord_name": "Ash", "guild_id": GUILD}

NAMES = ("Ravenshade", "NightOwl", "Ironclad", "Vesper", "Kestrel", "Basalt")

DAY = "2026-08-25"


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def _field(names=NAMES, *, scouted=None, stage="semifinals", label="M", started_on=None):
    """A field of invented players, most of them scouted.

    They are still imported into a group, because that is how a roster arrives
    and because the stage helpers read one. The slate no longer does: what it
    needs is registrants and their squads.

    `scouted` is how many of them have squads; the rest are the players a card
    cannot predict, which is a case this surface has to render rather than
    refuse.
    """
    scouted = len(names) if scouted is None else scouted
    db.import_registrants(
        [
            {"name": name, "group": label, "rank": i + 1, "server": "738", "thp": 90_000_000}
            for i, name in enumerate(names)
        ],
        stage=stage,
        started_on=started_on,
    )
    grouping_id = db.default_grouping_id()
    group = db.get_or_create_group(grouping_id, stage, label)
    for i, name in enumerate(names):
        rid = db.resolve_registrant(name, server="738")["id"]
        db.set_placement(group["id"], rid, seed_rank=i + 1)
        if i < scouted:
            powers = (34_000_000 - i * 900_000, 31_000_000 - i * 700_000, 27_000_000 - i * 500_000)
            for slot, (squad_type, power) in enumerate(
                zip(("Tank", "Missile", "Aircraft"), powers), start=1
            ):
                db.set_squad(
                    rid, slot, squad_type=squad_type, power=power, actor=ACTOR, source="observed"
                )
    return group


def _ids(*names):
    return [db.resolve_registrant(name, server="738")["id"] for name in names]


# ── What the schema will accept ───────────────────────────────────────────────


def test_a_slate_holds_the_meetings_it_was_given_in_order(cd_db):
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    slate = db.set_slate(GUILD, DAY, [(a, b), (c, d)], actor=ACTOR)

    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(a, b), (c, d)]
    assert [m["position"] for m in slate["meetings"]] == [1, 2]
    assert slate["play_on"] == DAY
    assert slate["guild_id"] == GUILD
    assert slate["card_no"] == 1


def test_rebuilding_the_same_card_replaces_it_rather_than_adding_one(cd_db):
    """One card per guild per day per number, updated in place.

    The volume is a thin-provisioned zvol that never gives blocks back, so a
    second edit has to overwrite. More than that: two rows for one card would
    leave the reader's `Today's picks` picking one of them arbitrarily.
    """
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b), (c, d)], actor=ACTOR)
    again = db.set_slate(GUILD, DAY, [(c, d)], actor=OTHER)

    assert len(again["meetings"]) == 1
    assert db.slate_days(GUILD) == [
        {
            "play_on": DAY,
            "card_no": 1,
            "stage": None,
            "updated_at": again["updated_at"],
            "updated_by": "222",
            "meetings": 1,
        }
    ]
    # The first author is kept: the card was created once and edited once, and
    # collapsing those to one name would credit the edit with the whole thing.
    assert again["created_by"] == "111"


def test_a_twenty_first_meeting_goes_on_a_second_card_rather_than_off_the_end(cd_db):
    """Kevin, 2026-08-27: overflow makes another card, it never drops a row.

    That is what retires caption row-dropping by construction rather than by
    rule, and it is the reason the key carries a card number at all.
    """
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    first = db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    second = db.set_slate(GUILD, DAY, [(c, d)], card_no=2, actor=ACTOR)

    assert first["id"] != second["id"]
    assert [(m["a_id"], m["b_id"]) for m in db.get_slate(GUILD, DAY)["meetings"]] == [(a, b)]
    assert [(m["a_id"], m["b_id"]) for m in db.get_slate(GUILD, DAY, card_no=2)["meetings"]] == [
        (c, d)
    ]
    assert [(r["play_on"], r["card_no"]) for r in db.slate_days(GUILD)] == [(DAY, 1), (DAY, 2)]


def test_the_number_of_cards_a_day_can_carry_is_bounded(cd_db):
    """Not rationing -- a runaway guard, on a volume that never shrinks.

    The number comes from the game: 128 players play at most 64 meetings a day
    (Kevin, 2026-08-28), so `MAX_CARDS_PER_DAY` cards of `MAX_PICKS` hold the
    whole field. Refused rather than clamped, because writing card 9 as card 4
    would silently overwrite a card somebody built.
    """
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    assert db.MAX_CARDS_PER_DAY * db.MAX_PICKS >= 64

    with pytest.raises(ValueError, match=f"1..{db.MAX_CARDS_PER_DAY}"):
        db.set_slate(GUILD, DAY, [(a, b)], card_no=db.MAX_CARDS_PER_DAY + 1, actor=ACTOR)
    with pytest.raises(ValueError, match=f"1..{db.MAX_CARDS_PER_DAY}"):
        db.set_slate(GUILD, DAY, [(a, b)], card_no=0, actor=ACTOR)


def test_the_same_meeting_cannot_appear_on_two_of_the_days_cards(cd_db):
    """A reader seeing one meeting twice is the same mistake whether the repeat
    is on this card or the next one, and it names which card to go and fix."""
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    with pytest.raises(ValueError, match="already on card 1"):
        db.set_slate(GUILD, DAY, [(c, d), (b, a)], card_no=2, actor=ACTOR)


def test_rebuilding_a_card_does_not_trip_over_its_own_meetings(cd_db):
    """The cross-card check excludes the card being written. Without that, no
    card could ever be saved twice."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    again = db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    assert len(again["meetings"]) == 1


def test_another_guild_may_card_the_same_meeting(cd_db):
    """A behaviour change worth pinning: two guilds in one grouping used to
    share a slate, because the key was the group. They each build their own
    now, and the same meeting is a legitimate pick for both."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    theirs = db.set_slate(OTHER_GUILD, DAY, [(a, b)], actor=ACTOR)

    assert len(theirs["meetings"]) == 1
    assert db.get_slate(GUILD, DAY)["id"] != theirs["id"]


def test_a_slate_with_no_guild_is_refused(cd_db):
    """`guild_id` is NOT NULL for a reason. SQLite counts every NULL in a
    unique index as distinct, so a nameless slate would sit outside the
    constraint that bounds this table -- the same trap `get_or_create_group`
    documents for knockout labels."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    for missing in (None, "", "   "):
        with pytest.raises(ValueError, match="belongs to a guild"):
            db.set_slate(missing, DAY, [(a, b)], actor=ACTOR)


def test_the_same_pair_the_other_way_round_is_the_same_meeting(cd_db):
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match="twice"):
        db.set_slate(GUILD, DAY, [(a, b), (b, a)], actor=ACTOR)


def test_a_player_cannot_meet_themselves(cd_db):
    _field()
    (a,) = _ids("Ravenshade")
    with pytest.raises(ValueError, match="two different players"):
        db.set_slate(GUILD, DAY, [(a, a)], actor=ACTOR)


def test_a_card_stops_at_the_cap(cd_db):
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match=str(db.MAX_PICKS)):
        db.set_slate(GUILD, DAY, [(a, b)] * (db.MAX_PICKS + 1), actor=ACTOR)


def test_an_empty_card_is_refused_rather_than_written(cd_db):
    """Clearing is `delete_slate`. A stored slate with no meetings would render
    as a card that says nothing, which is not what removing every pick means."""
    _field()
    with pytest.raises(ValueError, match="delete_slate"):
        db.set_slate(GUILD, DAY, [], actor=ACTOR)


@pytest.mark.parametrize("bad", ["25/08/2026", "tomorrow", "2026-13-01", ""])
def test_a_day_that_is_not_a_date_never_reaches_the_table(cd_db, bad):
    """`play_on` is what a reader asks for by name. A row filed under
    `25/08/2026` is a row nobody will ever find again."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match="ISO date"):
        db.set_slate(GUILD, bad, [(a, b)], actor=ACTOR)


def test_a_day_with_no_card_reads_as_nothing_rather_than_as_empty(cd_db):
    _field()
    assert db.get_slate(GUILD, DAY) is None
    assert picks.build(GUILD, DAY) is None


def test_deleting_a_card_leaves_the_days_other_cards_alone(cd_db):
    """A renumber would move a card somebody is looking at while they are
    looking at it, and an empty card 1 costs nothing."""
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    db.set_slate(GUILD, DAY, [(c, d)], card_no=2, actor=ACTOR)

    assert db.delete_slate(GUILD, DAY) is True
    assert db.delete_slate(GUILD, DAY) is False
    assert db.get_slate(GUILD, DAY) is None
    assert [(m["a_id"], m["b_id"]) for m in db.get_slate(GUILD, DAY, card_no=2)["meetings"]] == [
        (c, d)
    ]
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pick_meetings").fetchone()[0] == 1


def test_removing_a_player_takes_only_their_meetings(cd_db):
    """A card outlives one of its players. The rest of it still renders."""
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b), (c, d)], actor=ACTOR)
    with db._get_conn() as conn:
        conn.execute("DELETE FROM registrants WHERE id = ?", (a,))

    slate = db.get_slate(GUILD, DAY)
    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(c, d)]


# ── Who may be on a card ──────────────────────────────────────────────────────


def test_two_players_from_different_groups_may_meet(cd_db):
    """The rule that changed, and it changed because the old one is no longer
    askable. A slate is picked out of a field of 128 that mixes warzones, and
    at the knockouts there is no lettered group at all. What stops an
    impossible pair is the entry flow filtering Player 2 to who Player 1 can
    meet -- and where the bracket is unknown, nothing does.
    """
    _field(NAMES[:4])
    _field(("Stranger", "Passerby"), stage="semifinals", label="N")
    mine, theirs = _ids("Ravenshade", "Stranger")

    slate = db.set_slate(GUILD, DAY, [(mine, theirs)], actor=ACTOR)
    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(mine, theirs)]


def test_a_registrant_that_does_not_exist_is_refused(cd_db):
    """The realistic way this happens is a dropdown drawn before somebody was
    deleted. Both players must exist, which is the whole membership rule now.
    """
    _field()
    (mine,) = _ids("Ravenshade")
    with pytest.raises(ValueError, match="no registrant 9999"):
        db.set_slate(GUILD, DAY, [(mine, 9999)], actor=ACTOR)


def test_a_repeated_pair_is_refused_by_name_not_by_id(cd_db):
    """A refusal naming a surrogate id is not something the person holding the
    dropdown can act on."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match="Ravenshade and NightOwl are on this card twice"):
        db.set_slate(GUILD, DAY, [(a, b), (a, b)], actor=ACTOR)


# ── Being forgotten ───────────────────────────────────────────────────────────


def test_a_card_they_built_keeps_its_meetings_and_loses_their_name(cd_db):
    """The meetings are a fixture that gets played. Who wrote them down is a
    fact about a person, and that is the half a removal takes."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    preview = db.purge_user_data("111")
    assert preview["scrubbed"]["pick_slates"] == 1
    db.purge_user_data("111", apply=True)

    slate = db.get_slate(GUILD, DAY)
    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(a, b)]
    assert slate["created_by"] is None
    assert slate["updated_by"] is None


def test_a_card_somebody_else_built_is_left_alone(cd_db):
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(GUILD, DAY, [(a, b)], actor=OTHER)

    db.purge_user_data("111", apply=True)
    assert db.get_slate(GUILD, DAY)["created_by"] == "222"


def test_forgetting_one_author_leaves_the_others_attribution(cd_db):
    """A card one person built and another edited holds two different people.

    The removal predicate matches a row where EITHER column is the person being
    removed, so a flat clear of both columns takes the second person's name off
    a card as a side effect of removing the first. This is the only entry in
    the removal spec whose two halves can name different people.
    """
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    db.set_slate(GUILD, DAY, [(a, b)], actor=OTHER)

    db.purge_user_data("111", apply=True)
    slate = db.get_slate(GUILD, DAY)
    assert slate["created_by"] is None
    assert slate["updated_by"] == "222", "the editor lost their name to somebody else's removal"


# ── Scoring one ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("stage", ["semifinals", "knockouts"])
def test_every_meeting_is_scored_as_one_match_whatever_the_stage(cd_db, stage):
    """The card used to score semifinal and knockout rows as the Bo3 the game
    actually plays them as. Measured on 310 real results that is worse, not
    better: a Bo3 amplifies the favourite by 0.4pp while `series_win_prob`
    amplifies by 8.4pp, and Brier goes 0.1010 -> 0.1052.

    So the stage decides nothing about the number, and this pins that rather
    than the old rule. The Bo3 comparison is asserted on so the pin is real: a
    row scored at the wrong length is still a plausible-looking number.
    """
    _field(stage=stage, label=None if stage == "knockouts" else "M")
    a, b = _ids("Ravenshade", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b)], stage=stage, actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    sides = [
        cdp.build_side(db.get_player(n, server="738", include_scouting=True))
        for n in ("Ravenshade", "Vesper")
    ]
    assert slate.picks[0].p_a == pytest.approx(cdp.predict_pair(*sides, best_of=1))
    assert slate.picks[0].p_a != pytest.approx(cdp.predict_pair(*sides, best_of=3))


def test_nothing_named_best_of_survives_on_a_slate(cd_db):
    """`BEST_OF` mapped a stage to a series length and was wrong at every entry
    it held. Removed rather than corrected, because the card never renders a
    qualifier either (Kevin: qualifiers have no prediction betting)."""
    assert not hasattr(picks, "BEST_OF")
    assert not hasattr(picks.Slate(guild_id=GUILD, play_on=DAY), "best_of")


def test_a_row_we_cannot_predict_stays_on_the_card_and_names_who(cd_db):
    """It is the most useful row on the card: it names two players nobody has
    scouted, to the alliance about to read it. Dropping it hides the gap."""
    _field(scouted=4)
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Kestrel", "Basalt")
    db.set_slate(GUILD, DAY, [(a, b), (c, d)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert [p.predicted for p in slate.picks] == [True, False]
    assert slate.picks[1].missing == ("Kestrel", "Basalt")
    assert slate.picks[1].p_a is None
    assert slate.picks[1].confidence() is None
    # The names survive, which is the whole point of keeping the row.
    assert (slate.picks[1].a_name, slate.picks[1].b_name) == ("Kestrel", "Basalt")


def test_one_unscouted_side_is_enough_to_stop_a_row(cd_db):
    _field(scouted=5)
    a, b = _ids("Ravenshade", "Basalt")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert slate.picks[0].predicted is False
    assert slate.picks[0].missing == ("Basalt",)


def test_a_player_on_two_rows_is_built_once(cd_db):
    """A player meets two people on a two-meeting day. Two `SideInput` objects
    for one person could drift apart, and building one reads the database."""
    _field()
    a, b, c = _ids("Ravenshade", "NightOwl", "Ironclad")
    db.set_slate(GUILD, DAY, [(a, b), (a, c)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert slate.picks[0].prediction.a is slate.picks[1].prediction.a


def test_a_slate_comes_back_in_the_cards_order_not_the_makers(cd_db):
    """The defect this pairing closes. `render_slate` draws strongest pick
    first, so a text half that walked the entry order described a card whose
    rows were somewhere else -- and session A took the numerals off the image,
    so there was nothing on it to disagree with.

    The lopsided meeting is entered second on purpose: with the two orders
    identical this would pass without the sort existing."""
    _field(("Ravenshade", "NightOwl", "Goliath", "Pebble"))
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Goliath", "Pebble")
    with db._get_conn() as conn:
        conn.execute("UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (d,))
    db.set_slate(GUILD, DAY, [(a, b), (c, d)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert [p.a_name for p in slate.picks] == ["Goliath", "Ravenshade"]
    strongest = [max(p.p_a, p.p_b) for p in slate.picks]
    assert strongest == sorted(strongest, reverse=True)
    # The entry order is still on the row, and is still the truth about where
    # the meeting sits in storage. It is simply not what anything renders.
    assert [p.entry_position for p in slate.picks] == [2, 1]


def test_a_row_nobody_can_predict_sorts_to_the_end(cd_db):
    """It stays on the card -- it names two players nobody has scouted, to the
    alliance about to read it -- but it cannot be ranked among rows that carry
    a number, and the useful end of a card is the top."""
    _field(scouted=2)
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(c, d), (a, b)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert [p.predicted for p in slate.picks] == [True, False]


def test_the_card_and_the_text_beside_it_are_one_order(cd_db):
    """`render_slate` sorts by the same key, and `sorted` is stable, so
    re-sorting a slate that already carries this order changes nothing. That
    is what makes row three here row three on the image."""
    _field(scouted=4)
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(c, d), (a, b)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert picks.card_order(slate.picks) == slate.picks


def test_a_slate_can_be_scored_before_it_is_saved(cd_db):
    """So the person choosing meetings sees the card they are building. Both
    paths score the same way, which is what stops a preview disagreeing with
    what gets saved."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    preview = picks.assemble(GUILD, DAY, [(a, b)])

    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    saved = picks.build(GUILD, DAY)
    assert preview.picks[0].p_a == saved.picks[0].p_a


def test_a_preview_refuses_the_same_oversized_card_the_save_would(cd_db):
    """Otherwise a twenty-one-meeting preview renders a card the person is then
    told they cannot keep, which is the disagreement between preview and save
    that `assemble` exists to prevent."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match=str(db.MAX_PICKS)):
        picks.assemble(GUILD, DAY, [(a, b)] * (db.MAX_PICKS + 1))


def test_a_card_is_scored_against_the_players_on_it_not_against_a_group(cd_db):
    """The read that made a slate a group's card. Two players from different
    groups are both scored, which a group-scoped read could not do."""
    _field(NAMES[:4])
    _field(("Stranger", "Passerby"), stage="semifinals", label="N")
    mine, theirs = _ids("Ravenshade", "Stranger")
    db.set_slate(GUILD, DAY, [(mine, theirs)], actor=ACTOR)

    slate = picks.build(GUILD, DAY)
    assert slate.picks[0].predicted is True
    assert (slate.picks[0].a_name, slate.picks[0].b_name) == ("Ravenshade", "Stranger")


# ── Naming the two sides ──────────────────────────────────────────────────────


def test_two_players_sharing_a_name_are_told_apart_by_server(cd_db):
    """Names are unique per server, not across them, and a card draws from
    several. Two rows both reading `Ravenshade` is the one way this card can be
    actively misleading."""
    _field()
    db.import_registrants(
        [{"name": "Ravenshade", "group": "M", "rank": 9, "server": "912", "thp": 90_000_000}],
        stage="semifinals",
    )
    twin = db.resolve_registrant("Ravenshade", server="912")["id"]
    for slot, (squad_type, power) in enumerate(
        zip(("Tank", "Missile", "Aircraft"), (30_000_000, 28_000_000, 25_000_000)), start=1
    ):
        db.set_squad(twin, slot, squad_type=squad_type, power=power, actor=ACTOR, source="observed")

    a, b, c = _ids("Ravenshade", "NightOwl", "Ironclad")
    db.set_slate(GUILD, DAY, [(a, b), (twin, c)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    # Order-independent: the slate comes back in the card's order rather than
    # the order the meetings were entered in, so the twins are looked up by
    # who they are beside rather than by where they landed.
    labels = {pick.b_label: pick.a_label for pick in slate.picks}
    assert labels["NightOwl"] == "Ravenshade #738"
    assert labels["Ironclad"] == "Ravenshade #912"
    # And nobody else pays for it: the suffix costs a third of the width a name
    # has to fit into.
    assert set(labels) == {"NightOwl", "Ironclad"}


def test_the_same_player_on_two_rows_gains_no_suffix(cd_db):
    _field()
    a, b, c = _ids("Ravenshade", "NightOwl", "Ironclad")
    db.set_slate(GUILD, DAY, [(a, b), (a, c)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert slate.picks[0].a_label == "Ravenshade"
    assert slate.picks[1].a_label == "Ravenshade"


def test_a_name_with_markdown_in_it_reads_the_same_in_both_places(cd_db):
    """The caption bolds names and the card draws them plain, so an unescaped
    asterisk puts one player under two different names in one message."""
    _field(("Rav**en", "NightOwl"))
    a, b = _ids("Rav**en", "NightOwl")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert slate.picks[0].a_label == "Rav**en", "the label itself is what the card draws"
    assert "**Rav\\*\\*en**" in "\n".join(picks.text_rows(slate))


def test_a_side_with_no_name_says_so_the_same_way_on_both_surfaces(cd_db):
    """Only reachable from a preview or from a player deleted after the card
    was saved, since `set_slate` refuses to write one. Set on the label rather
    than at each surface, so the card and the caption cannot describe the same
    gap two different ways."""
    _field()
    (a,) = _ids("Ravenshade")
    slate = picks.assemble(GUILD, DAY, [(a, 9999)])

    assert slate.picks[0].b_label == picks.CARD_UNKNOWN
    assert picks.CARD_UNKNOWN in "\n".join(picks.text_rows(slate))


# ── What the card is of ───────────────────────────────────────────────────────


def test_the_subject_is_the_stage_and_a_calendar_date(cd_db):
    """Kevin, 2026-08-27: *"Just put the round, something like 'Semi-finals
    Predictions' and leave it at that. Simple."* -- plus the date, as the
    in-game calendar date he asked for. No group letter and no day number.
    """
    slate = picks.Slate(guild_id=GUILD, play_on=DAY, stage="semifinals")

    assert slate.date_label() == "Tue Aug 25"
    assert slate.subject() == "Semi-finals · Tue Aug 25"
    assert "Group" not in slate.subject()
    assert "Day" not in slate.subject()


def test_the_weekday_leads_the_date_everywhere_the_label_goes(cd_db):
    """Kevin, 2026-08-29: *"We should add the day of the week here"*, asked on
    the day picker. It is built in `date_label` rather than at the picker
    because the CARD reads off the same method, and one day spelled two ways on
    two surfaces is what this method exists to prevent. **So this is a visible
    change to something already on a member's screen.**"""
    assert picks.Slate(guild_id=GUILD, play_on="2026-08-29").date_label() == "Sat Aug 29"
    assert picks.Slate(guild_id=GUILD, play_on="2026-09-01").date_label() == "Tue Sep 1"


def test_a_day_that_did_not_split_says_nothing_about_card_numbers(cd_db):
    """A lone card reads exactly as it did before any of this: one card is not
    "1 of 1", it is just the card."""
    slate = picks.Slate(guild_id=GUILD, play_on=DAY, stage="semifinals")

    assert slate.subject() == "Semi-finals · Tue Aug 25"
    assert "Card" not in slate.subject()


def test_every_card_of_a_split_says_which_one_it_is_and_how_many(cd_db):
    """Kevin, 2026-08-29: *"If it has to be 2 cards, we need to tell them each
    one. So it would be '# of #'. It would only show when cards > 1."*

    **Card 1 is the half this fixes.** The marker used to appear on card 2 and
    up, so the card most likely to be read on its own carried nothing saying it
    was half of a pair."""
    first = picks.Slate(guild_id=GUILD, play_on=DAY, stage="semifinals", card_total=2)
    second = picks.Slate(guild_id=GUILD, play_on=DAY, stage="semifinals", card_no=2, card_total=2)

    assert first.subject() == "Semi-finals · Tue Aug 25 · Card 1 of 2"
    assert second.subject() == "Semi-finals · Tue Aug 25 · Card 2 of 2"


def test_a_total_that_cannot_be_true_is_clamped_rather_than_printed(cd_db):
    """`Card 3 of 2` tells a reader cards exist that do not, which is worse
    than no marker at all. A total read before another card arrived, or one a
    caller never filled in, is raised to the card in hand."""
    stale = picks.Slate(guild_id=GUILD, play_on=DAY, stage="semifinals", card_no=3, card_total=2)

    assert stale.subject() == "Semi-finals · Tue Aug 25 · Card 3 of 3"


def test_the_days_total_comes_back_with_the_card_it_describes(cd_db):
    """A slate row knows its own number and nothing about its siblings, so the
    total is read beside it on the same connection rather than asked for
    separately."""
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b)], stage="semifinals", actor=ACTOR)

    assert db.get_slate(GUILD, DAY)["card_total"] == 1
    assert picks.build(GUILD, DAY).subject() == "Semi-finals · Tue Aug 25"

    db.set_slate(GUILD, DAY, [(c, d)], card_no=2, stage="semifinals", actor=ACTOR)

    assert db.get_slate(GUILD, DAY)["card_total"] == 2
    assert picks.build(GUILD, DAY).subject() == "Semi-finals · Tue Aug 25 · Card 1 of 2"
    assert picks.build(GUILD, DAY, card_no=2).subject() == "Semi-finals · Tue Aug 25 · Card 2 of 2"


def test_the_total_is_the_highest_card_number_rather_than_how_many_there_are(cd_db):
    """A day with a gap in it. Emptying card 2 while 1 and 3 exist deletes it,
    and a COUNT would then head card 3 `Card 3 of 2` -- the impossible marker
    `subject` clamps against, arriving from the other side. It has to agree
    with `champion_duel_hub._cards_on_day`, which reads the same way."""
    _field()
    a, b, c, d, e, f = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper", "Kestrel", "Basalt")
    for card, pair in ((1, (a, b)), (2, (c, d)), (3, (e, f))):
        db.set_slate(GUILD, DAY, [pair], card_no=card, stage="semifinals", actor=ACTOR)
    db.delete_slate(GUILD, DAY, card_no=2)

    assert db.get_slate(GUILD, DAY)["card_total"] == 3
    assert picks.build(GUILD, DAY).subject().endswith("Card 1 of 3")
    assert picks.build(GUILD, DAY, card_no=3).subject().endswith("Card 3 of 3")


def test_a_preview_refuses_the_same_card_number_the_save_would(cd_db):
    """The other half of the preview/save agreement `assemble` exists for: a
    card 5 that renders and is then refused on save is the same defect as an
    oversized one that renders and is then refused."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match=f"1..{db.MAX_CARDS_PER_DAY}"):
        picks.assemble(GUILD, DAY, [(a, b)], card_no=db.MAX_CARDS_PER_DAY + 1)


def test_the_knockouts_read_the_same_way_as_every_other_stage(cd_db):
    """They used to be the exception, because they are one field of 32 with no
    letter and the subject line led with a group. Nothing leads with a group
    now, so there is no exception left."""
    slate = picks.Slate(guild_id=GUILD, play_on=DAY, stage="knockouts")

    assert slate.subject() == "Knockout Stage · Tue Aug 25"


def test_without_a_stage_the_date_alone_is_better_than_a_guess(cd_db):
    """A guild whose warzone is in no grouping we hold has no stage to stamp,
    which is the normal state of a new alliance."""
    slate = picks.Slate(guild_id=GUILD, play_on=DAY)

    assert slate.subject() == "Tue Aug 25"


def test_the_stage_is_stamped_from_the_guilds_grouping(cd_db, monkeypatch):
    """One less thing for the maker to tap. Semi-finals open on day 13 of a
    grouping, so a grouping that began on the 11th is in them on the 25th."""
    _field(started_on="2026-08-11")
    db.set_guild_warzone(GUILD, "738")
    a, b = _ids("Ravenshade", "NightOwl")
    monkeypatch.setattr(db, "_server_today", lambda: date(2026, 8, 25))

    saved = db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    assert saved["stage"] == "semifinals"
    assert picks.build(GUILD, DAY).subject() == "Semi-finals · Tue Aug 25"


def test_the_stamped_stage_survives_the_event_moving_on(cd_db, monkeypatch):
    """The reason it is a column rather than a read. Derived at render time, a
    semifinal card reopened during the knockouts would relabel itself -- the
    same failure the day number had, printing `Day 38` on a four-day round."""
    _field(started_on="2026-08-11")
    db.set_guild_warzone(GUILD, "738")
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    monkeypatch.setattr(db, "_server_today", lambda: date(2026, 8, 25))
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    # Knockouts open on day 20, which is 31 Aug for this grouping.
    monkeypatch.setattr(db, "_server_today", lambda: date(2026, 9, 1))
    assert db.current_stage(db.default_grouping_id()) == "knockouts"

    rebuilt = db.set_slate(GUILD, DAY, [(a, b), (c, d)], actor=OTHER)
    assert rebuilt["stage"] == "semifinals"
    assert picks.build(GUILD, DAY).subject() == "Semi-finals · Tue Aug 25"


def test_a_stage_named_outright_beats_the_one_the_calendar_would_stamp(cd_db, monkeypatch):
    """The maker is reading a screen headed with the stage. Where they say so,
    that wins."""
    _field(started_on="2026-08-11")
    db.set_guild_warzone(GUILD, "738")
    a, b = _ids("Ravenshade", "NightOwl")
    monkeypatch.setattr(db, "_server_today", lambda: date(2026, 8, 25))

    saved = db.set_slate(GUILD, DAY, [(a, b)], stage="knockouts", actor=ACTOR)
    assert saved["stage"] == "knockouts"


def test_todays_card_reads_the_games_clock(cd_db, monkeypatch):
    """The card is prepared the evening before, so the day it is FOR and the
    day it was built are different days."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")
    monkeypatch.setattr(db, "_server_today", lambda: date(2026, 8, 25))
    assert picks.todays(GUILD) is None

    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    assert len(picks.todays(GUILD).picks) == 1


# ── The text half ─────────────────────────────────────────────────────────────


def test_the_text_half_carries_every_row(cd_db):
    """It is what survives a screen reader, a failed image load and Discord's
    own search, and on a slate that matters more than on one prediction:
    there are up to twenty numbers in the picture."""
    _field(scouted=4)
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Kestrel", "Basalt")
    db.set_slate(GUILD, DAY, [(a, b), (c, d)], stage="semifinals", actor=ACTOR)
    rows = picks.text_rows(picks.build(GUILD, DAY))

    assert len(rows) == 2
    text = "\n".join(rows)
    assert "**Ravenshade**" in text and "**NightOwl**" in text
    assert "no squads recorded" in text


def test_the_text_half_carries_no_row_numbers(cd_db):
    """The defect this session closes. The rows used to open `1.`, `2.` off the
    entry order while the card drew them strongest first, and session A had
    already taken the numerals off the image -- so the number named a row that
    was nowhere. Both halves follow the card's order now, and neither counts."""
    _field(scouted=6)
    ids = _ids(*NAMES)
    db.set_slate(GUILD, DAY, [(ids[0], ids[1]), (ids[2], ids[3])], actor=ACTOR)
    rows = picks.text_rows(picks.build(GUILD, DAY))

    assert not any(row.startswith(("1.", "2.")) for row in rows)
    for template in (picks.TEXT_ROW, picks.TEXT_ROW_UNPREDICTED):
        assert "{i}" not in template


def test_the_text_half_is_in_the_same_order_as_the_card(cd_db):
    """One order, arrived at once. The image re-sorts by the same key, which is
    a no-op on a list that already carries it, so row three here is row three
    on the picture."""
    _field(scouted=6)
    ids = _ids(*NAMES)
    db.set_slate(GUILD, DAY, [(ids[4], ids[5]), (ids[0], ids[1])], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert picks.card_order(slate.picks) == slate.picks
    # The rows come out in that order, strongest pick at the top.
    strongest = [max(p.p_a, p.p_b) for p in slate.picks]
    assert strongest == sorted(strongest, reverse=True)
    assert picks.text_rows(slate) == picks.text_rows(
        picks.Slate(guild_id=GUILD, play_on=DAY, picks=picks.card_order(slate.picks))
    )


def test_the_text_half_never_rounds_a_certainty_into_existence(cd_db):
    """The one claim the card exists to refuse. The text used to format its
    own percentage and said 100% above a card that said >99%."""
    _field(("Goliath", "Pebble"))
    a, b = _ids("Goliath", "Pebble")
    with db._get_conn() as conn:
        conn.execute("UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (b,))
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    text = "\n".join(picks.text_rows(picks.build(GUILD, DAY)))
    assert ">99%" in text
    assert "100%" not in text


def test_a_full_card_of_long_names_still_carries_every_row(cd_db):
    """**Nothing is dropped, and nothing can be.** The clamp this replaces
    dropped whole rows off the end and said so, which put a meeting on the
    image that was not in the text. Twenty rows is the hard cap and an embed
    description holds 4,096 characters, so the worst case is under half of it
    and there is nothing left for a truncation rule to do.

    These names are 67 characters, more than three times the game's own
    20-character limit, so the measurement below is well past the worst a real
    card can carry."""
    long_names = tuple("Player" + "o" * 60 + str(i) for i in range(7))
    _field(long_names)
    ids = _ids(*long_names)
    pairs = list(itertools.combinations(ids, 2))[: db.MAX_PICKS]
    db.set_slate(GUILD, DAY, pairs, actor=ACTOR)
    rows = picks.text_rows(picks.build(GUILD, DAY))

    assert len(rows) == db.MAX_PICKS
    assert len("\n".join(rows)) < 4096
    assert not hasattr(picks, "CAPTION_TRUNCATED")
    assert not hasattr(picks, "CAPTION_LIMIT")


def test_the_coin_flip_line_is_said_only_on_a_card_that_has_one(cd_db):
    """Kevin, 2026-08-28: *"we can do a cap but should likely add a line of
    text in the embed itself that those are truly a coin flip."* The image
    still prints `PICK 50%`, because `p_a >= p_b` names a side and suppressing
    the cap would drop the pick from the one row where naming a side is the
    whole task. The honesty lives in the text beside it."""
    _field(("Mirror", "Image"))
    a, b = _ids("Mirror", "Image")
    with db._get_conn() as conn:
        for slot, power in enumerate((30_000_000, 28_000_000, 25_000_000), start=1):
            conn.execute(
                "UPDATE squads SET power = ? WHERE registrant_id IN (?, ?) AND slot = ?",
                (power, a, b, slot),
            )
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert picks.is_coin_flip(slate.picks[0]), "two identical line-ups are a tie-break"
    assert picks.has_coin_flip(slate)


def test_a_card_with_no_tie_break_on_it_says_nothing_about_coin_flips(cd_db):
    """A caveat on a card the reader can see has no 50% row would be explaining
    something that is not there."""
    _field(("Goliath", "Pebble"))
    a, b = _ids("Goliath", "Pebble")
    with db._get_conn() as conn:
        conn.execute("UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (b,))
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    assert not picks.has_coin_flip(picks.build(GUILD, DAY))


def test_a_row_with_no_prediction_is_not_a_coin_flip(cd_db):
    """It is a row we have nothing to say about, not a row we say 50% about."""
    _field(scouted=0)
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert not slate.picks[0].predicted
    assert not picks.is_coin_flip(slate.picks[0])
    assert not picks.has_coin_flip(slate)


def test_the_alt_text_points_at_the_rows_and_fits_discords_cap(cd_db):
    """Discord caps an attachment description at 1,024, and twenty decorated
    names run past that. The rows are in the text on the same message either
    way, so the description names the card and points at them."""
    long_names = tuple("Player" + "o" * 60 + str(i) for i in range(7))
    _field(long_names)
    ids = _ids(*long_names)
    db.set_slate(
        GUILD,
        DAY,
        list(itertools.combinations(ids, 2))[: db.MAX_PICKS],
        stage="semifinals",
        actor=ACTOR,
    )
    slate = picks.build(GUILD, DAY)
    alt = picks.alt_text(slate)

    assert len(alt) <= picks.ALT_LIMIT
    assert alt == "Champion Duel picks card image. All details are in this message."
    assert long_names[0] not in alt


def test_every_string_this_module_renders_is_free_of_em_dashes(cd_db):
    """`notes/UX.md` reaches all of them. **The card strings are not exempt**:
    the exemption on record is written as *the card, not the module*, and the
    card it covers is the VS card, whose copy Kevin actually workshopped."""
    for template in (
        picks.TEXT_ROW,
        picks.TEXT_ROW_UNPREDICTED,
        picks.TEXT_COIN_FLIP,
        picks.TEXT_ALT,
        picks.CARD_TITLE,
        picks.CARD_FOOTER,
        picks.CARD_UNKNOWN,
        picks.CARD_NUMBER,
        picks.PICKS_TITLE,
    ):
        assert "—" not in template


# ── Re-keying the table off the group ─────────────────────────────────────────
#
# The migration is the shape `/code-review` catches, and two of the three tests
# below pin things that were measured on SQLite 3.45.1 rather than assumed.


def _rewind_to_group_keyed_slates():
    """Put `pick_slates` back the way it shipped, so the migration has work.

    Foreign keys off for the drop, for the reason the migration documents:
    dropping a parent table with them on runs an implicit DELETE first, which
    fires `pick_meetings`' cascade.
    """
    conn = db._get_conn()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DROP TABLE pick_slates")
    conn.execute("""
        CREATE TABLE pick_slates (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id   INTEGER NOT NULL,
            play_on    TEXT    NOT NULL,
            created_at TEXT    NOT NULL,
            created_by TEXT,
            updated_at TEXT    NOT NULL,
            updated_by TEXT,
            UNIQUE (group_id, play_on),
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
        )
    """)
    conn.commit()
    conn.close()


def _old_slate(group_id, play_on, meetings, *, slate_id):
    now = db._now()
    with db._get_conn() as conn:
        conn.execute(
            "INSERT INTO pick_slates (id, group_id, play_on, created_at, created_by, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slate_id, group_id, play_on, now, "111", now, "111"),
        )
        conn.executemany(
            "INSERT INTO pick_meetings (slate_id, position, a_id, b_id) VALUES (?, ?, ?, ?)",
            [(slate_id, i, a, b) for i, (a, b) in enumerate(meetings, start=1)],
        )


def test_a_group_keyed_slate_is_re_keyed_to_the_guild_that_built_it(cd_db):
    """The group supplied ownership by proxy: `groups.created_by_guild_id`, or
    the grouping's. Dropping `group_id` removes that route, so the guild is
    carried across before it goes."""
    group = _field()
    with db._get_conn() as conn:
        conn.execute("UPDATE groups SET created_by_guild_id = ? WHERE id = ?", (GUILD, group["id"]))
    a, b = _ids("Ravenshade", "NightOwl")
    _rewind_to_group_keyed_slates()
    _old_slate(group["id"], DAY, [(a, b)], slate_id=7)

    db._migrate_slates_off_groups()

    slate = db.get_slate(GUILD, DAY)
    assert slate["id"] == 7, "the id is preserved or every meeting loses its slate"
    assert slate["card_no"] == 1
    assert slate["stage"] == "semifinals", "the group's own stage is what to stamp"
    assert slate["created_by"] == "111"
    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(a, b)]
    # Idempotent: the second call must find nothing to do.
    db._migrate_slates_off_groups()
    assert db.get_slate(GUILD, DAY)["id"] == 7


def test_the_meetings_foreign_key_still_points_at_the_rebuilt_table(cd_db):
    """The trap this migration is written around, measured rather than assumed.

    `ALTER TABLE pick_slates RENAME TO ...` rewrites `pick_meetings`' REFERENCES
    clause to the new name, whatever `legacy_alter_table` and `foreign_keys`
    say. Renaming the old table out of the way and dropping it would leave
    every later insert failing with *no such table*, and the cascade pointing
    at nothing -- which no test of the migrated rows would notice.
    """
    group = _field()
    with db._get_conn() as conn:
        conn.execute("UPDATE groups SET created_by_guild_id = ? WHERE id = ?", (GUILD, group["id"]))
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    _rewind_to_group_keyed_slates()
    _old_slate(group["id"], DAY, [(a, b)], slate_id=7)

    db._migrate_slates_off_groups()

    with db._get_conn() as conn:
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'pick_meetings'").fetchone()
        assert "pick_slates" in sql[0] and "rebuilt" not in sql[0]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    # The card still takes a write, and the cascade still fires.
    db.set_slate(GUILD, DAY, [(a, b), (c, d)], actor=ACTOR)
    assert len(db.get_slate(GUILD, DAY)["meetings"]) == 2
    db.delete_slate(GUILD, DAY)
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pick_meetings").fetchone()[0] == 0


def test_a_guilds_second_card_for_a_day_is_renumbered_rather_than_dropped(cd_db):
    """The old UNIQUE was per group, so a guild tracking two groups held two
    slates for one evening quite legitimately. The new shape has the numbers
    spare, and deleting the second would take a card somebody built and its
    meetings with it to save a number that is already there.
    """
    first = _field(NAMES[:4])
    second = _field(("Stranger", "Passerby"), stage="semifinals", label="N")
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE groups SET created_by_guild_id = ? WHERE id IN (?, ?)",
            (GUILD, first["id"], second["id"]),
        )
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Stranger", "Passerby")
    _rewind_to_group_keyed_slates()
    _old_slate(first["id"], DAY, [(a, b)], slate_id=7)
    _old_slate(second["id"], DAY, [(c, d)], slate_id=8)

    db._migrate_slates_off_groups()

    # Oldest first, so card 1 is the one that was card 1 before.
    assert db.get_slate(GUILD, DAY)["id"] == 7
    assert db.get_slate(GUILD, DAY, card_no=2)["id"] == 8
    assert [(m["a_id"], m["b_id"]) for m in db.get_slate(GUILD, DAY, card_no=2)["meetings"]] == [
        (c, d)
    ]


def test_a_slate_whose_guild_cannot_be_found_is_dropped_with_its_meetings(cd_db):
    """`guild_id` is NOT NULL, so there is no row to write for a slate nobody
    owns -- and a NULL would sit outside the UNIQUE that bounds this table.
    Such a slate was already unreachable: nothing can ask for a card without
    knowing whose it is. The meetings go with it rather than being left
    pointing at an id nothing holds.
    """
    group = _field()
    with db._get_conn() as conn:
        conn.execute("UPDATE groups SET created_by_guild_id = NULL WHERE id = ?", (group["id"],))
        conn.execute("UPDATE groupings SET created_by_guild_id = NULL")
    a, b = _ids("Ravenshade", "NightOwl")
    _rewind_to_group_keyed_slates()
    _old_slate(group["id"], DAY, [(a, b)], slate_id=7)

    db._migrate_slates_off_groups()

    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pick_slates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM pick_meetings").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


# ── Appending one meeting, without losing anybody else's ──────────────────────
#
# `db.add_to_slate` exists because the surface used to read a card and rewrite
# it whole, on two connections. These pin the behaviour that made it worth
# writing, and the refusals it inherits word for word from `set_slate`.


def test_appending_keeps_what_is_already_on_the_card(cd_db):
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    assert db.add_to_slate(GUILD, DAY, (c, d), actor=OTHER) == 1
    slate = db.get_slate(GUILD, DAY)

    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(a, b), (c, d)]
    assert [m["position"] for m in slate["meetings"]] == [1, 2]


def test_an_append_does_not_lose_a_meeting_written_under_it(cd_db):
    """The bug this closes, played out.

    A view reads the card, somebody else adds to it, and then the view writes
    what it read back. `set_slate` is a full replace, so the second write used
    to delete the first with no error to catch. Appending cannot: it never
    holds a snapshot of the card to write back.
    """
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    e, f = _ids("Kestrel", "Basalt")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    stale = db.get_slate(GUILD, DAY)
    db.add_to_slate(GUILD, DAY, (c, d), actor=OTHER)
    db.add_to_slate(GUILD, DAY, (e, f), actor=ACTOR)

    assert len(stale["meetings"]) == 1
    assert len(db.get_slate(GUILD, DAY)["meetings"]) == 3


def test_an_append_opens_the_next_card_once_one_is_full(cd_db):
    """`MAX_PICKS` is legibility rather than storage, so overflow rolls onto the
    next card rather than being refused."""
    names = tuple(f"Filler{i:02d}" for i in range(db.MAX_PICKS * 2 + 2))
    _field(names)
    ids = _ids(*names)
    pairs = list(zip(ids[::2], ids[1::2]))

    landed = [db.add_to_slate(GUILD, DAY, pair, actor=ACTOR) for pair in pairs]

    assert landed[: db.MAX_PICKS] == [1] * db.MAX_PICKS
    assert landed[db.MAX_PICKS] == 2
    assert len(db.get_slate(GUILD, DAY)["meetings"]) == db.MAX_PICKS


def test_an_append_wraps_to_a_card_with_room_rather_than_calling_the_day_full(cd_db):
    """A full card only ever means a full day. A reader who opened the last
    card and filled it still has room on card 1."""
    names = tuple(f"Filler{i:02d}" for i in range(4))
    _field(names)
    a, b, c, d = _ids(*names)
    db.set_slate(GUILD, DAY, [(a, b)] * 1, card_no=1, actor=ACTOR)

    assert db.add_to_slate(GUILD, DAY, (c, d), card_no=db.MAX_CARDS_PER_DAY, actor=ACTOR) == (
        db.MAX_CARDS_PER_DAY
    )


def test_a_full_day_reports_itself_rather_than_raising(cd_db):
    """None, which the surface turns into the "every card is full" notice. A
    raise here would read to the reader as something having gone wrong."""
    per_card = db.MAX_PICKS
    total = per_card * db.MAX_CARDS_PER_DAY
    names = tuple(f"Filler{i:03d}" for i in range(total * 2 + 2))
    _field(names)
    ids = _ids(*names)
    pairs = list(zip(ids[::2], ids[1::2]))
    for pair in pairs[:total]:
        assert db.add_to_slate(GUILD, DAY, pair, actor=ACTOR) is not None

    assert db.add_to_slate(GUILD, DAY, pairs[total], actor=ACTOR) is None


def test_an_append_refuses_a_pair_already_carded_anywhere_that_day(cd_db):
    """`set_slate`'s rule and `set_slate`'s words. A reader seeing one meeting
    twice is the same mistake whichever card it is on."""
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    db.set_slate(GUILD, DAY, [(c, d)], card_no=2, actor=ACTOR)

    with pytest.raises(ValueError, match="already on card 2"):
        db.add_to_slate(GUILD, DAY, (d, c), actor=ACTOR)


def test_an_append_refuses_a_player_meeting_themselves(cd_db):
    _field()
    (a,) = _ids("Ravenshade")

    with pytest.raises(ValueError, match="two different players"):
        db.add_to_slate(GUILD, DAY, (a, a), actor=ACTOR)


def test_an_append_refuses_a_registrant_we_do_not_hold(cd_db):
    _field()
    (a,) = _ids("Ravenshade")

    with pytest.raises(ValueError, match="no registrant"):
        db.add_to_slate(GUILD, DAY, (a, 99999), actor=ACTOR)


def test_an_append_with_no_guild_is_refused(cd_db):
    _field()
    a, b = _ids("Ravenshade", "NightOwl")

    for missing in (None, "", "   "):
        with pytest.raises(ValueError, match="belongs to a guild"):
            db.add_to_slate(missing, DAY, (a, b), actor=ACTOR)


def test_a_refused_append_writes_nothing(cd_db):
    """The transaction rolls back, so a refusal never leaves an empty card
    behind: "nobody has built tomorrow's card yet" and "the card is empty" are
    different things to say and only one of them is ever true."""
    _field()
    a, b = _ids("Ravenshade", "NightOwl")

    with pytest.raises(ValueError):
        db.add_to_slate(GUILD, DAY, (a, 99999), actor=ACTOR)

    assert db.get_slate(GUILD, DAY) is None


def test_an_append_stamps_the_stage_on_creation_and_leaves_it_alone_after(cd_db):
    """A card re-rendered after the event moved on must still say which round
    it was for, which is `set_slate`'s rule."""
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(a, b)], stage="knockouts", actor=ACTOR)

    db.add_to_slate(GUILD, DAY, (c, d), actor=ACTOR)

    assert db.get_slate(GUILD, DAY)["stage"] == "knockouts"

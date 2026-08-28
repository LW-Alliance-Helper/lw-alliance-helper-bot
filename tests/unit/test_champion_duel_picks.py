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


def test_positions_are_the_order_they_were_picked_in(cd_db):
    _field()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(GUILD, DAY, [(c, d), (a, b)], actor=ACTOR)
    slate = picks.build(GUILD, DAY)

    assert [p.position for p in slate.picks] == [1, 2]
    assert [p.a_name for p in slate.picks] == ["Ironclad", "Ravenshade"]


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

    assert slate.picks[0].a_label == "Ravenshade #738"
    assert slate.picks[1].a_label == "Ravenshade #912"
    # And nobody else pays for it: the suffix costs a third of the width a name
    # has to fit into.
    assert slate.picks[0].b_label == "NightOwl"


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
    assert "**Rav\\*\\*en**" in picks.caption(slate)


def test_a_side_with_no_name_says_so_the_same_way_on_both_surfaces(cd_db):
    """Only reachable from a preview or from a player deleted after the card
    was saved, since `set_slate` refuses to write one. Set on the label rather
    than at each surface, so the card and the caption cannot describe the same
    gap two different ways."""
    _field()
    (a,) = _ids("Ravenshade")
    slate = picks.assemble(GUILD, DAY, [(a, 9999)])

    assert slate.picks[0].b_label == picks.CARD_UNKNOWN
    assert picks.CARD_UNKNOWN in picks.caption(slate)


# ── What the card is of ───────────────────────────────────────────────────────


def test_the_subject_is_the_stage_and_a_calendar_date(cd_db):
    """Kevin, 2026-08-27: *"Just put the round, something like 'Semi-finals
    Predictions' and leave it at that. Simple."* -- plus the date, as the
    in-game calendar date he asked for. No group letter and no day number.

    Placeholder copy pending sign-off, so what is pinned here is the shape.
    """
    slate = picks.Slate(guild_id=GUILD, play_on=DAY, stage="semifinals")

    assert slate.date_label() == "Aug 25"
    assert slate.subject() == "Semi-finals · Aug 25"
    assert "Group" not in slate.subject()
    assert "Day" not in slate.subject()


def test_the_knockouts_read_the_same_way_as_every_other_stage(cd_db):
    """They used to be the exception, because they are one field of 32 with no
    letter and the subject line led with a group. Nothing leads with a group
    now, so there is no exception left."""
    slate = picks.Slate(guild_id=GUILD, play_on=DAY, stage="knockouts")

    assert slate.subject() == "Knockout Stage · Aug 25"


def test_without_a_stage_the_date_alone_is_better_than_a_guess(cd_db):
    """A guild whose warzone is in no grouping we hold has no stage to stamp,
    which is the normal state of a new alliance."""
    slate = picks.Slate(guild_id=GUILD, play_on=DAY)

    assert slate.subject() == "Aug 25"


def test_the_stage_is_stamped_from_the_guilds_grouping(cd_db, monkeypatch):
    """One less thing for the maker to tap. Semi-finals open on day 13 of a
    grouping, so a grouping that began on the 11th is in them on the 25th."""
    _field(started_on="2026-08-11")
    db.set_guild_warzone(GUILD, "738")
    a, b = _ids("Ravenshade", "NightOwl")
    monkeypatch.setattr(db, "_server_today", lambda: date(2026, 8, 25))

    saved = db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)
    assert saved["stage"] == "semifinals"
    assert picks.build(GUILD, DAY).subject() == "Semi-finals · Aug 25"


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
    assert picks.build(GUILD, DAY).subject() == "Semi-finals · Aug 25"


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


# ── The caption ───────────────────────────────────────────────────────────────


def test_the_caption_carries_every_row(cd_db):
    """It is what survives a screen reader, a failed image load and Discord's
    own search, and on a slate that matters more than on one prediction:
    there are up to twenty numbers in the picture."""
    _field(scouted=4)
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Kestrel", "Basalt")
    db.set_slate(GUILD, DAY, [(a, b), (c, d)], stage="semifinals", actor=ACTOR)
    text = picks.caption(picks.build(GUILD, DAY))

    assert text.startswith(picks.PICKS_TITLE)
    assert "Semi-finals · Aug 25" in text
    assert "**Ravenshade**" in text and "**NightOwl**" in text
    assert "no squads recorded" in text
    assert len(text.splitlines()) == 3


def test_the_caption_never_rounds_a_certainty_into_existence(cd_db):
    """The one claim the card exists to refuse. The caption used to format its
    own percentage and said 100% above a card that said >99%."""
    _field(("Goliath", "Pebble"))
    a, b = _ids("Goliath", "Pebble")
    with db._get_conn() as conn:
        conn.execute("UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (b,))
    db.set_slate(GUILD, DAY, [(a, b)], actor=ACTOR)

    text = picks.caption(picks.build(GUILD, DAY))
    assert ">99%" in text
    assert "100%" not in text


def test_a_caption_too_long_for_discord_drops_whole_rows_and_says_so(cd_db):
    """Clamped by dropping rows off the end rather than by cutting mid-line: a
    caption that stopped mid-row would read as a card with fewer meetings on it
    than it has."""
    long_names = tuple("Player" + "o" * 60 + str(i) for i in range(7))
    _field(long_names)
    ids = _ids(*long_names)
    pairs = list(itertools.combinations(ids, 2))[: db.MAX_PICKS]
    db.set_slate(GUILD, DAY, pairs, actor=ACTOR)
    text = picks.caption(picks.build(GUILD, DAY))

    assert len(text) <= picks.CAPTION_LIMIT
    assert text.endswith(picks.CAPTION_TRUNCATED)
    # Whole lines, so the last row shown is a complete one.
    assert not text.splitlines()[-2].endswith("**")


def test_the_caption_is_free_of_em_dashes(cd_db):
    """It is a Discord message, so `notes/UX.md` reaches it. Copy rendered into
    the card image is workshopped and exempt; this is not."""
    for template in (picks.CAPTION_ROW, picks.CAPTION_ROW_UNPREDICTED, picks.CAPTION_TRUNCATED):
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

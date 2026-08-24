"""The day's picks: the slate the schema holds and the card it turns into.

Two things can be wrong here rather than merely awkward. The stored slate can
say two players meet who are not both in the group, or say it twice, or lose
half a card when somebody re-edits it. And the scored slate can quietly answer
a different question than the one the round is played over: `predict_pair` takes
`best_of` with no default precisely because a Bo3 scored as a Bo1 is a plausible
number at the wrong series length, and a slate applies that argument seventeen
times from one place.

Players here are invented. The bot repo is public and no roster name goes in it.
"""

from __future__ import annotations

import itertools

import pytest

import champion_duel_db as db
import champion_duel_picks as picks
import champion_duel_predict as cdp

ACTOR = {"discord_user_id": "111", "discord_name": "Willow", "guild_id": "999"}
OTHER = {"discord_user_id": "222", "discord_name": "Ash", "guild_id": "999"}

NAMES = ("Ravenshade", "NightOwl", "Ironclad", "Vesper", "Kestrel", "Basalt")


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def _group(names=NAMES, *, scouted=None, stage="semifinals", label="M", started_on=None):
    """A group of invented players, most of them scouted.

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
    group = _group()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    slate = db.set_slate(group["id"], "2026-08-25", [(a, b), (c, d)], actor=ACTOR)

    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(a, b), (c, d)]
    assert [m["position"] for m in slate["meetings"]] == [1, 2]
    assert slate["play_on"] == "2026-08-25"


def test_rebuilding_the_same_evening_replaces_the_card_rather_than_adding_one(cd_db):
    """One slate per group per day, updated in place.

    The volume is a thin-provisioned zvol that never gives blocks back, so a
    second edit has to overwrite. More than that: two slates for one day would
    leave the reader's `Today's picks` picking one of them arbitrarily.
    """
    group = _group()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(group["id"], "2026-08-25", [(a, b), (c, d)], actor=ACTOR)
    again = db.set_slate(group["id"], "2026-08-25", [(c, d)], actor=OTHER)

    assert len(again["meetings"]) == 1
    assert db.slate_days(group["id"]) == [
        {
            "play_on": "2026-08-25",
            "updated_at": again["updated_at"],
            "updated_by": "222",
            "meetings": 1,
        }
    ]
    # The first author is kept: the card was created once and edited once, and
    # collapsing those to one name would credit the edit with the whole thing.
    assert again["created_by"] == "111"


def test_a_player_from_outside_the_group_is_refused(cd_db):
    """Meetings are picked out of a group we hold, never typed. A pair from
    outside it would put two names on a card headed by a group neither is in.

    What the refusal says is asserted further down, with the rest of what
    `/code-review` caught.
    """
    group = _group(NAMES[:4])
    _group(("Stranger", "Passerby"), stage="semifinals", label="N")
    inside = _ids("Ravenshade")[0]
    outside = _ids("Stranger")[0]

    with pytest.raises(ValueError, match="not in this group"):
        db.set_slate(group["id"], "2026-08-25", [(inside, outside)], actor=ACTOR)


def test_the_same_pair_the_other_way_round_is_the_same_meeting(cd_db):
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match="twice"):
        db.set_slate(group["id"], "2026-08-25", [(a, b), (b, a)], actor=ACTOR)


def test_a_player_cannot_meet_themselves(cd_db):
    group = _group()
    (a,) = _ids("Ravenshade")
    with pytest.raises(ValueError, match="two different players"):
        db.set_slate(group["id"], "2026-08-25", [(a, a)], actor=ACTOR)


def test_a_card_stops_at_the_cap(cd_db):
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match=str(db.MAX_PICKS)):
        db.set_slate(group["id"], "2026-08-25", [(a, b)] * (db.MAX_PICKS + 1), actor=ACTOR)


def test_an_empty_card_is_refused_rather_than_written(cd_db):
    """Clearing is `delete_slate`. A stored slate with no meetings would render
    as a card that says nothing, which is not what removing every pick means."""
    group = _group()
    with pytest.raises(ValueError, match="delete_slate"):
        db.set_slate(group["id"], "2026-08-25", [], actor=ACTOR)


@pytest.mark.parametrize("bad", ["25/08/2026", "tomorrow", "2026-13-01", ""])
def test_a_day_that_is_not_a_date_never_reaches_the_table(cd_db, bad):
    """`play_on` is what a reader asks for by name. A row filed under
    `25/08/2026` is a row nobody will ever find again."""
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match="ISO date"):
        db.set_slate(group["id"], bad, [(a, b)], actor=ACTOR)


def test_a_day_with_no_card_reads_as_nothing_rather_than_as_empty(cd_db):
    group = _group()
    assert db.get_slate(group["id"], "2026-08-25") is None
    assert picks.build(group["id"], "2026-08-25") is None


def test_deleting_a_card_takes_its_meetings_with_it(cd_db):
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)

    assert db.delete_slate(group["id"], "2026-08-25") is True
    assert db.delete_slate(group["id"], "2026-08-25") is False
    with db._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pick_meetings").fetchone()[0] == 0


def test_removing_a_player_takes_only_their_meetings(cd_db):
    """A card outlives one of its players. The rest of it still renders."""
    group = _group()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(group["id"], "2026-08-25", [(a, b), (c, d)], actor=ACTOR)
    with db._get_conn() as conn:
        conn.execute("DELETE FROM registrants WHERE id = ?", (a,))

    slate = db.get_slate(group["id"], "2026-08-25")
    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(c, d)]


# ── Being forgotten ───────────────────────────────────────────────────────────


def test_a_card_they_built_keeps_its_meetings_and_loses_their_name(cd_db):
    """The meetings are a fixture the group played. Who wrote them down is a
    fact about a person, and that is the half a removal takes."""
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)

    preview = db.purge_user_data("111")
    assert preview["scrubbed"]["pick_slates"] == 1
    db.purge_user_data("111", apply=True)

    slate = db.get_slate(group["id"], "2026-08-25")
    assert [(m["a_id"], m["b_id"]) for m in slate["meetings"]] == [(a, b)]
    assert slate["created_by"] is None
    assert slate["updated_by"] is None


def test_a_card_somebody_else_built_is_left_alone(cd_db):
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=OTHER)

    db.purge_user_data("111", apply=True)
    assert db.get_slate(group["id"], "2026-08-25")["created_by"] == "222"


# ── Scoring one ───────────────────────────────────────────────────────────────


def test_every_meeting_is_scored_at_the_length_the_round_is_played_over(cd_db):
    """`predict_pair` takes `best_of` with no default because a Bo3 scored as a
    Bo1 is a plausible number at the wrong series length. A slate applies that
    argument to every row from one place, so it is worth pinning that the round
    is what decides it."""
    group = _group()
    a, b = _ids("Ravenshade", "Vesper")
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)
    slate = picks.build(group["id"], "2026-08-25")

    assert slate.best_of == 3
    pick = slate.picks[0]
    sides = [
        cdp.build_side(db.get_player(n, server="738", include_scouting=True))
        for n in ("Ravenshade", "Vesper")
    ]
    assert pick.p_a == pytest.approx(cdp.predict_pair(*sides, best_of=3))
    # Not the same number as a single match, which is what makes the argument
    # worth asserting on rather than assuming.
    assert pick.p_a != pytest.approx(cdp.predict_pair(*sides, best_of=1))


def test_a_qualifier_slate_is_a_single_match(cd_db):
    group = _group(stage="qualifiers", label="A")
    a, b = _ids("Ravenshade", "Vesper")
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)
    assert picks.build(group["id"], "2026-08-25").best_of == 1


def test_a_row_we_cannot_predict_stays_on_the_card_and_names_who(cd_db):
    """It is the most useful row on the card: it names two players nobody has
    scouted, to the alliance about to read it. Dropping it hides the gap."""
    group = _group(scouted=4)
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Kestrel", "Basalt")
    db.set_slate(group["id"], "2026-08-25", [(a, b), (c, d)], actor=ACTOR)
    slate = picks.build(group["id"], "2026-08-25")

    assert [p.predicted for p in slate.picks] == [True, False]
    assert slate.picks[1].missing == ("Kestrel", "Basalt")
    assert slate.picks[1].p_a is None
    assert slate.picks[1].confidence() is None
    # The names survive, which is the whole point of keeping the row.
    assert (slate.picks[1].a_name, slate.picks[1].b_name) == ("Kestrel", "Basalt")


def test_one_unscouted_side_is_enough_to_stop_a_row(cd_db):
    group = _group(scouted=5)
    a, b = _ids("Ravenshade", "Basalt")
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)
    slate = picks.build(group["id"], "2026-08-25")

    assert slate.picks[0].predicted is False
    assert slate.picks[0].missing == ("Basalt",)


def test_a_player_on_two_rows_is_built_once(cd_db):
    """A player meets two people on a two-meeting day. Two `SideInput` objects
    for one person could drift apart, and building one reads the database."""
    group = _group()
    a, b, c = _ids("Ravenshade", "NightOwl", "Ironclad")
    db.set_slate(group["id"], "2026-08-25", [(a, b), (a, c)], actor=ACTOR)
    slate = picks.build(group["id"], "2026-08-25")

    assert slate.picks[0].prediction.a is slate.picks[1].prediction.a


def test_positions_are_the_order_they_were_picked_in(cd_db):
    group = _group()
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    db.set_slate(group["id"], "2026-08-25", [(c, d), (a, b)], actor=ACTOR)
    slate = picks.build(group["id"], "2026-08-25")

    assert [p.position for p in slate.picks] == [1, 2]
    assert [p.a_name for p in slate.picks] == ["Ironclad", "Ravenshade"]


def test_a_slate_can_be_scored_before_it_is_saved(cd_db):
    """So the person choosing meetings sees the card they are building. Both
    paths score the same way, which is what stops a preview disagreeing with
    what gets saved."""
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    preview = picks.assemble(db.get_group(group["id"]), "2026-08-25", [(a, b)])

    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)
    saved = picks.build(group["id"], "2026-08-25")
    assert preview.picks[0].p_a == saved.picks[0].p_a


# ── Naming the two sides ──────────────────────────────────────────────────────


def test_two_players_sharing_a_name_are_told_apart_by_server(cd_db):
    """Names are unique per server, not across them, and a group draws from
    several. Two rows both reading `Ravenshade` is the one way this card can be
    actively misleading."""
    group = _group()
    db.import_registrants(
        [{"name": "Ravenshade", "group": "M", "rank": 9, "server": "912", "thp": 90_000_000}],
        stage="semifinals",
    )
    twin = db.resolve_registrant("Ravenshade", server="912")["id"]
    db.set_placement(group["id"], twin, seed_rank=9)
    for slot, (squad_type, power) in enumerate(
        zip(("Tank", "Missile", "Aircraft"), (30_000_000, 28_000_000, 25_000_000)), start=1
    ):
        db.set_squad(twin, slot, squad_type=squad_type, power=power, actor=ACTOR, source="observed")

    a, b, c = _ids("Ravenshade", "NightOwl", "Ironclad")
    db.set_slate(group["id"], "2026-08-25", [(a, b), (twin, c)], actor=ACTOR)
    slate = picks.build(group["id"], "2026-08-25")

    assert slate.picks[0].a_label == "Ravenshade #738"
    assert slate.picks[1].a_label == "Ravenshade #912"
    # And nobody else pays for it: the suffix costs a third of the width a name
    # has to fit into.
    assert slate.picks[0].b_label == "NightOwl"


def test_the_same_player_on_two_rows_gains_no_suffix(cd_db):
    group = _group()
    a, b, c = _ids("Ravenshade", "NightOwl", "Ironclad")
    db.set_slate(group["id"], "2026-08-25", [(a, b), (a, c)], actor=ACTOR)
    slate = picks.build(group["id"], "2026-08-25")

    assert slate.picks[0].a_label == "Ravenshade"
    assert slate.picks[1].a_label == "Ravenshade"


# ── What the card is of ───────────────────────────────────────────────────────


def test_the_subject_names_the_group_the_round_and_the_day(cd_db):
    """Off the grouping's own calendar rather than a stored day number, which
    would be a second copy of the game's schedule that could disagree with it.
    Semifinals open on day 13 of a grouping, so the 14th day of one that began
    on the 11th is its second."""
    group = _group(started_on="2026-08-11")
    slate = picks.assemble(db.get_group(group["id"]), "2026-08-25", [])

    assert slate.day_number() == 2
    assert slate.subject() == "Group M · Semi-finals · Day 2"


def test_without_a_start_date_the_day_is_left_off_rather_than_guessed(cd_db):
    group = _group()
    slate = picks.assemble(db.get_group(group["id"]), "2026-08-25", [])

    assert slate.day_number() is None
    assert slate.subject() == "Group M · Semi-finals"


def test_the_knockouts_open_on_the_round_because_they_have_no_letter(cd_db):
    """One field of 32 with no letter, which is why `label` is NULL there."""
    db.import_registrants(
        [{"name": name, "rank": i + 1, "server": "738"} for i, name in enumerate(NAMES)],
        stage="knockouts",
    )
    group = db.get_or_create_group(db.default_grouping_id(), "knockouts", None)
    slate = picks.assemble(group, "2026-08-25", [])

    assert slate.subject() == "Knockout Stage"


def test_todays_card_reads_the_games_clock(cd_db, monkeypatch):
    """The card is prepared the evening before, so the day it is FOR and the
    day it was built are different days."""
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    from datetime import date

    monkeypatch.setattr(db, "_server_today", lambda: date(2026, 8, 25))
    assert picks.todays(group["id"]) is None

    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)
    assert len(picks.todays(group["id"]).picks) == 1


# ── The caption ───────────────────────────────────────────────────────────────


def test_the_caption_carries_every_row(cd_db):
    """It is what survives a screen reader, a failed image load and Discord's
    own search, and on a slate that matters more than on one prediction:
    there are up to seventeen numbers in the picture."""
    group = _group(scouted=4)
    a, b, c, d = _ids("Ravenshade", "NightOwl", "Kestrel", "Basalt")
    db.set_slate(group["id"], "2026-08-25", [(a, b), (c, d)], actor=ACTOR)
    text = picks.caption(picks.build(group["id"], "2026-08-25"))

    assert text.startswith(picks.PICKS_TITLE)
    assert "Group M · Semi-finals" in text
    assert "**Ravenshade**" in text and "**NightOwl**" in text
    assert "no squads recorded" in text
    assert len(text.splitlines()) == 3


def test_the_caption_never_rounds_a_certainty_into_existence(cd_db):
    """The one claim the card exists to refuse. The caption used to format its
    own percentage and said 100% above a card that said >99%."""
    group = _group(("Goliath", "Pebble"))
    a, b = _ids("Goliath", "Pebble")
    with db._get_conn() as conn:
        conn.execute("UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (b,))
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)

    text = picks.caption(picks.build(group["id"], "2026-08-25"))
    assert ">99%" in text
    assert "100%" not in text


def test_a_caption_too_long_for_discord_drops_whole_rows_and_says_so(cd_db):
    """Clamped by dropping rows off the end rather than by cutting mid-line: a
    caption that stopped mid-row would read as a card with fewer meetings on it
    than it has."""
    long_names = tuple("Player" + "o" * 60 + str(i) for i in range(6))
    group = _group(long_names)
    ids = _ids(*long_names)
    pairs = list(itertools.combinations(ids, 2))[: db.MAX_PICKS]
    db.set_slate(group["id"], "2026-08-25", pairs, actor=ACTOR)
    text = picks.caption(picks.build(group["id"], "2026-08-25"))

    assert len(text) <= picks.CAPTION_LIMIT
    assert text.endswith(picks.CAPTION_TRUNCATED)
    # Whole lines, so the last row shown is a complete one.
    assert not text.splitlines()[-2].endswith("**")


def test_the_caption_is_free_of_em_dashes(cd_db):
    """It is a Discord message, so `notes/UX.md` reaches it. Copy rendered into
    the card image is workshopped and exempt; this is not."""
    for template in (picks.CAPTION_ROW, picks.CAPTION_ROW_UNPREDICTED, picks.CAPTION_TRUNCATED):
        assert "—" not in template


# ── What /code-review caught ──────────────────────────────────────────────────


def test_forgetting_one_author_leaves_the_others_attribution(cd_db):
    """A card one person built and another edited holds two different people.

    The removal predicate matches a row where EITHER column is the person being
    removed, so a flat clear of both columns takes the second person's name off
    a card as a side effect of removing the first. This is the only entry in
    the removal spec whose two halves can name different people.
    """
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=OTHER)

    db.purge_user_data("111", apply=True)
    slate = db.get_slate(group["id"], "2026-08-25")
    assert slate["created_by"] is None
    assert slate["updated_by"] == "222", "the editor lost their name to somebody else's removal"


def test_a_player_outside_the_group_is_refused_by_name_not_by_id(cd_db):
    """The realistic way this happens is a dropdown drawn before somebody was
    moved out of the group, and a refusal naming a surrogate id is not
    something the person holding that dropdown can act on."""
    group = _group(NAMES[:4])
    _group(("Stranger", "Passerby"), label="N")
    inside = _ids("Ravenshade")[0]
    outside = _ids("Stranger")[0]

    with pytest.raises(ValueError, match="Stranger is not in this group"):
        db.set_slate(group["id"], "2026-08-25", [(inside, outside)], actor=ACTOR)


def test_a_registrant_that_no_longer_exists_falls_back_to_its_id(cd_db):
    """There is genuinely no name left to give, and the id is still enough to
    tell two refusals apart in a log."""
    group = _group()
    (inside,) = _ids("Ravenshade")
    with pytest.raises(ValueError, match="registrant 9999 is not in this group"):
        db.set_slate(group["id"], "2026-08-25", [(inside, 9999)], actor=ACTOR)


def test_a_day_outside_the_round_is_left_off_rather_than_counted(cd_db):
    """A slate outlives its grouping, which is the normal state of last
    season's data. `Day 38` on a four-day round is worse than no day at all."""
    group = _group(started_on="2026-08-11")
    inside = picks.assemble(db.get_group(group["id"]), "2026-08-25", [])
    after = picks.assemble(db.get_group(group["id"]), "2026-09-20", [])
    before = picks.assemble(db.get_group(group["id"]), "2026-08-01", [])

    assert inside.day_number() == 2
    assert after.day_number() is None
    assert before.day_number() is None
    assert after.subject() == "Group M · Semi-finals"


def test_a_preview_refuses_the_same_oversized_card_the_save_would(cd_db):
    """Otherwise an eighteen-meeting preview renders a card the person is then
    told they cannot keep, which is the disagreement between preview and save
    that `assemble` exists to prevent."""
    group = _group()
    a, b = _ids("Ravenshade", "NightOwl")
    with pytest.raises(ValueError, match=str(db.MAX_PICKS)):
        picks.assemble(db.get_group(group["id"]), "2026-08-25", [(a, b)] * (db.MAX_PICKS + 1))


def test_a_name_with_markdown_in_it_reads_the_same_in_both_places(cd_db):
    """The caption bolds names and the card draws them plain, so an unescaped
    asterisk puts one player under two different names in one message."""
    group = _group(("Rav**en", "NightOwl"))
    a, b = _ids("Rav**en", "NightOwl")
    db.set_slate(group["id"], "2026-08-25", [(a, b)], actor=ACTOR)
    slate = picks.build(group["id"], "2026-08-25")

    assert slate.picks[0].a_label == "Rav**en", "the label itself is what the card draws"
    assert "**Rav\\*\\*en**" in picks.caption(slate)


def test_a_side_with_no_name_says_so_the_same_way_on_both_surfaces(cd_db):
    """Only reachable from a preview, since `set_slate` refuses a player who is
    not in the group. Set on the label rather than at each surface, so the card
    and the caption cannot describe the same gap two different ways."""
    group = _group()
    (a,) = _ids("Ravenshade")
    slate = picks.assemble(db.get_group(group["id"]), "2026-08-25", [(a, 9999)])

    assert slate.picks[0].b_label == picks.CARD_UNKNOWN
    assert picks.CARD_UNKNOWN in picks.caption(slate)

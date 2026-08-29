"""The picks entry flow: three taps, and nothing anybody has to type.

`db.set_slate` had no caller outside its own tests. The card rendered, the data
shape held it, and no member could reach either -- so what this covers is the
door, and the two things that can be quietly wrong behind it.

**Player 2 is the validation.** `set_slate`'s only membership rule is that both
players exist, and its docstring says why: *"What actually stops an impossible
pair is the entry flow filtering Player 2 to who Player 1 can meet."* A filter
that offered the wrong seven names would write meetings that cannot happen, and
nothing downstream would notice -- the card would render them and predict them.

**The knockout fold is derived and never enforced.** Seed *i* meets seed 33 - i
in the round of 32, measured 16 of 16 on one event. One event is not a rule, so
these tests pin that the partner is offered first and that everybody else is
still offered too.

Every name here is obviously invented. The bot repo is public and no roster name
goes in it.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import discord
import pytest

import champion_duel_db as db
import champion_duel_hub as hub
import champion_duel_picks as picks
from tests.conftest import make_mock_interaction

GUILD = "999"
USER = 4242
ACTOR = {"discord_user_id": str(USER), "discord_name": "Tester", "guild_id": GUILD}

#: Thirty-two obviously invented players, which is a knockout field.
NATO = (
    "Alfa Bravo Charlie Delta Echo Foxtrot Golf Hotel India Juliett Kilo Lima Mike November "
    "Oscar Papa Quebec Romeo Sierra Tango Uniform Victor Whiskey Xray Yankee Zulu Anvil "
    "Beacon Cinder Dune Ember Flint"
).split()


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def started_so_today_is(phase: str) -> str:
    first_day = {key: first for key, first, _ in db.PHASES}[phase]
    return (db._server_today() - timedelta(days=first_day)).isoformat()


def _grouping(stage="semifinals", warzones=("738", "800")):
    grouping = db.create_grouping(list(warzones), started_so_today_is(stage), origin="member")
    db.set_guild_warzone(GUILD, warzones[0])
    return grouping


def _place(grouping, stage, label, players):
    """`players` is (name, warzone, seed_rank, rank)."""
    group = db.get_or_create_group(grouping["id"], stage, label)
    for name, warzone, seed, rank in players:
        reg = db.upsert_registrant(name, server=warzone, alliance="ABC")
        db.set_placement(group["id"], reg["id"], seed_rank=seed, rank=rank)
    return group


def _semifinal_field(grouping):
    """Two groups of four, drawn from two warzones, which is the shape that
    matters: a group mixes warzones, so the warzone select cannot supply Player
    2's filter and the group has to."""
    _place(
        grouping,
        "semifinals",
        "A",
        [
            ("Alfa", "738", 1, None),
            ("Bravo", "800", 2, None),
            ("Charlie", "738", 3, None),
            ("Delta", "800", 4, None),
        ],
    )
    _place(
        grouping,
        "semifinals",
        "B",
        [
            ("Echo", "738", 1, None),
            ("Foxtrot", "800", 2, None),
            ("Golf", "738", 3, None),
            ("Hotel", "800", 4, None),
        ],
    )


def _knockout_field(grouping, *, played=0):
    """The unlettered field of 32, seeded 1..32.

    `played` knocks that many of them out, which is what takes the field past
    its first round and turns the fold off.
    """
    rows = []
    for i, name in enumerate(NATO, start=1):
        warzone = "738" if i % 2 else "800"
        rank = (33 - i) if i > len(NATO) - played else None
        rows.append((name, warzone, i, rank))
    return _place(grouping, "knockouts", None, rows)


def _rid(name):
    return db.resolve_registrant(name)["id"]


def _by_name(field, name):
    return next(m for m in field if m["display_name"] == name)


# ── Reading the field ─────────────────────────────────────────────────────────


def test_the_field_is_every_group_of_the_round_with_its_letter_on_each_row(cd_db):
    """The three selects read off one list, and the semi-final filter needs the
    letter that `get_group_members` does not select."""
    grouping = _grouping()
    _semifinal_field(grouping)

    field = hub._pick_field(grouping["id"], "semifinals", db.recorded_stages(grouping["id"]))

    assert len(field) == 8
    assert {m["grp"] for m in field} == {"A", "B"}
    assert _by_name(field, "Alfa")["grp"] == "A"
    assert _by_name(field, "Echo")["grp"] == "B"


def test_the_knockout_field_is_read_although_it_has_no_letter(cd_db):
    """`get_groups` drops NULL labels, which is every knockout row. An empty
    answer there means the unlettered field, not an empty round."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping)

    field = hub._pick_field(grouping["id"], "knockouts", db.recorded_stages(grouping["id"]))

    assert len(field) == 32
    assert {m["grp"] for m in field} == {None}


def test_reading_a_round_we_hold_nothing_for_creates_nothing(cd_db):
    """`get_or_create_group` inserts. Creating the row for a round nobody has
    recorded would make `recorded_stages` report it as held, which closes the
    contribution door by looking at it."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping)

    assert hub._pick_field(grouping["id"], "semifinals", db.recorded_stages(grouping["id"])) == []
    assert db.recorded_stages(grouping["id"]) == ["knockouts"]


def test_the_warzones_come_from_the_field_rather_than_from_sixteen(cd_db):
    """Kevin named the growth himself: sixteen today, and nothing says it cannot
    increase. The select shows what the field actually holds."""
    grouping = _grouping()
    _semifinal_field(grouping)
    field = hub._pick_field(grouping["id"], "semifinals", db.recorded_stages(grouping["id"]))

    assert hub._warzone_counts(field) == [("738", 4), ("800", 4)]


def test_two_spellings_of_one_warzone_are_one_option(cd_db):
    """A registrant added through a modal can hold `0738` where the grouping
    holds `738`. Two options for one warzone would each look incomplete."""
    grouping = _grouping()
    _place(grouping, "semifinals", "A", [("Alfa", "738", 1, None), ("Bravo", "0738", 2, None)])
    field = hub._pick_field(grouping["id"], "semifinals", db.recorded_stages(grouping["id"]))

    assert hub._warzone_counts(field) == [("738", 2)]
    assert len(hub._in_warzone(field, "0738")) == 2


# ── Who Player 1 can meet ─────────────────────────────────────────────────────


def test_at_the_semifinals_player_two_is_the_rest_of_player_ones_group(cd_db):
    """Eight players meet each other once over the round, so the rest of the
    group is exactly the opponent list. The other group is not offered, however
    much warzone they share."""
    grouping = _grouping()
    _semifinal_field(grouping)
    field = hub._pick_field(grouping["id"], "semifinals", db.recorded_stages(grouping["id"]))

    opponents = hub._pick_opponents(field, _by_name(field, "Alfa"), "semifinals")

    assert [m["display_name"] for m in opponents] == ["Bravo", "Charlie", "Delta"]


def test_the_warzone_cut_does_not_reach_player_two(cd_db):
    """Groups mix warzones, so Player 1's own warzone says nothing about who
    they meet. Alfa is on 738 and two of their three opponents are not."""
    grouping = _grouping()
    _semifinal_field(grouping)
    field = hub._pick_field(grouping["id"], "semifinals", db.recorded_stages(grouping["id"]))

    opponents = hub._pick_opponents(field, _by_name(field, "Alfa"), "semifinals")

    assert {m["server"] for m in opponents} == {"738", "800"}


def test_a_player_never_meets_themselves(cd_db):
    grouping = _grouping()
    _semifinal_field(grouping)
    field = hub._pick_field(grouping["id"], "semifinals", db.recorded_stages(grouping["id"]))

    for stage_field, player in ((field, _by_name(field, "Alfa")),):
        assert player["registrant_id"] not in {
            m["registrant_id"] for m in hub._pick_opponents(stage_field, player, "semifinals")
        }


# ── The knockout fold ─────────────────────────────────────────────────────────


def test_the_round_of_32_pairing_is_a_fold_for_every_seed(cd_db):
    """Seed *i* meets seed 33 - i. Measured 16 of 16 against the round 3
    capture, and this pins that every one of the sixteen resolves rather than
    only the one somebody happened to try."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping)
    field = hub._pick_field(grouping["id"], "knockouts", db.recorded_stages(grouping["id"]))

    for member in field:
        partner = hub._fold_partner(field, member, "knockouts")
        assert partner is not None
        assert member["seed_rank"] + partner["seed_rank"] == 33


def test_the_derived_partner_is_offered_first_and_everyone_else_still_is(cd_db):
    """One event is not a rule. The fold is a preselection, so the other thirty
    are still in the list for the day the game changes it."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping)
    field = hub._pick_field(grouping["id"], "knockouts", db.recorded_stages(grouping["id"]))
    alfa = _by_name(field, "Alfa")

    opponents = hub._pick_opponents(field, alfa, "knockouts")

    assert opponents[0]["seed_rank"] == 32
    assert len(opponents) == 31


def test_once_a_knockout_result_is_recorded_nothing_is_derived(cd_db):
    """A recorded `rank` is an exit round, so the field is past its first round
    and the fold no longer describes it. Nothing is guessed in its place."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping, played=16)
    field = hub._pick_field(grouping["id"], "knockouts", db.recorded_stages(grouping["id"]))
    alfa = _by_name(field, "Alfa")

    assert hub._fold_partner(field, alfa, "knockouts") is None
    opponents = hub._pick_opponents(field, alfa, "knockouts")
    assert len(opponents) == 15
    assert all(m["rank"] is None for m in opponents)


def test_the_fold_never_reaches_the_semifinals(cd_db):
    """Three rounds, three formats. A semi-final seed is a draw position inside
    a group of eight and folding it would pair people who never meet."""
    grouping = _grouping()
    _semifinal_field(grouping)
    field = hub._pick_field(grouping["id"], "semifinals", db.recorded_stages(grouping["id"]))

    assert hub._fold_partner(field, _by_name(field, "Alfa"), "semifinals") is None


# ── What the surface reads ────────────────────────────────────────────────────


def test_a_guild_with_no_champion_duel_gets_the_control_that_fixes_it(cd_db):
    state = hub.read_picks(GUILD, None)

    assert state["state"] == "no_grouping"
    embed = hub.build_picks_embed(state)
    assert hub._btn_words(hub.CD_BTN_ADD_GROUPING) in embed.description


def test_the_qualifiers_are_not_a_round_this_card_covers(cd_db):
    """The game runs no prediction market on them, so there is nothing to card
    and the surface says so rather than offering an empty field."""
    grouping = _grouping("qualifiers")
    _place(grouping, "qualifiers", "A", [("Alfa", "738", 1, None)])

    state = hub.read_picks(GUILD, grouping)

    assert state["state"] == "no_stage"
    assert "qualifiers" in hub.build_picks_embed(state).description


def test_a_round_with_no_draw_recorded_points_at_recording_one(cd_db):
    grouping = _grouping("knockouts")

    state = hub.read_picks(GUILD, grouping)

    assert state["state"] == "no_field"
    assert hub._btn_words(hub.CD_BTN_RECORD) in hub.build_picks_embed(state).description


def test_the_round_is_the_one_the_card_was_stamped_with(cd_db):
    """A card built at the semi-finals and edited during the knockouts is still
    a semi-final card. Reading the knockout field for it would offer players its
    own rows cannot contain."""
    grouping = _grouping("knockouts")
    _semifinal_field(grouping)
    _knockout_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], stage="semifinals", actor=ACTOR)

    state = hub.read_picks(GUILD, grouping, play_on=day)

    assert state["stage"] == "semifinals"
    assert len(state["field"]) == 8


def test_a_carded_player_the_field_does_not_hold_still_has_a_name(cd_db):
    """The row stays on the card either way. A blank beside a meeting somebody
    chose is worse than a name read out of the registrant table.

    Reached here by carding somebody nobody has placed in a group, which is what
    an opponent added through the hub is until their group is recorded."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    alfa = _rid("Alfa")
    stray = db.upsert_registrant("Nomad", server="738", alliance="ABC")["id"]
    db.set_slate(GUILD, day, [(alfa, stray)], actor=ACTOR)

    state = hub.read_picks(GUILD, grouping, play_on=day)

    assert stray not in {m["registrant_id"] for m in state["field"]}
    assert state["names"][stray]["display_name"] == "Nomad"
    assert "**Nomad**" in hub.build_picks_embed(state).fields[0].value


def test_a_day_that_is_not_a_date_shows_today_rather_than_failing(cd_db):
    """The only producer is the day select, so this is a forged payload. Raising
    inside a callback would show the reader "Interaction failed"."""
    assert hub._pick_day("25/08/2026") == db.server_today().isoformat()
    assert hub._pick_day(None) == db.server_today().isoformat()
    assert hub._pick_day("2026-08-25") == "2026-08-25"


# ── The view ──────────────────────────────────────────────────────────────────


def _view(grouping, *, play_on=None, card_no=1):
    state = hub.read_picks(GUILD, grouping, play_on=play_on, card_no=card_no)
    return hub._PicksView(user_id=USER, guild_id=GUILD, state=state)


def _components(view, kind):
    return [item for item in view.children if isinstance(item, kind)]


def _select_by_placeholder(view, needle):
    return next(
        item
        for item in view.children
        if isinstance(item, discord.ui.Select) and needle in (item.placeholder or "")
    )


async def _pick(view, select, value):
    inter = make_mock_interaction(user_id=USER)
    inter.data = {"values": [str(value)]}
    await select.callback(inter)
    return inter


async def _press(view, label):
    button = next(
        item
        for item in view.children
        if isinstance(item, discord.ui.Button) and item.label == label
    )
    inter = make_mock_interaction(user_id=USER)
    inter.data = {}
    await button.callback(inter)
    return inter


async def test_three_taps_put_a_meeting_on_the_card(cd_db):
    """Warzone, Player 1, Player 2, and it is written. Nothing typed, nothing
    pasted, nothing reproduced."""
    grouping = _grouping()
    _semifinal_field(grouping)
    view = _view(grouping)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P2), _rid("Charlie"))
    await _press(view, hub.CD_BTN_PICKS_SAVE)

    stored = db.get_slate(GUILD, db.server_today().isoformat())
    assert [(m["a_id"], m["b_id"]) for m in stored["meetings"]] == [(_rid("Alfa"), _rid("Charlie"))]


async def test_player_one_at_the_round_of_32_preselects_the_bracket_partner(cd_db):
    """This is the round where an unfiltered Player 2 would have been 31 options
    against a cap of 25. The tap that chooses Player 1 answers Player 2 too, so
    the meeting can be added without a third one."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping)
    view = _view(grouping)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))

    assert view.p2 == _rid("Flint")
    save = next(
        item
        for item in view.children
        if isinstance(item, discord.ui.Button) and item.label == hub.CD_BTN_PICKS_SAVE
    )
    assert not save.disabled


async def test_the_preselected_partner_can_be_overridden(cd_db):
    """One event, 16 of 16. A hard validation would block legitimate entry the
    day the game changes the rule."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping)
    view = _view(grouping)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P2), _rid("Bravo"))
    await _press(view, hub.CD_BTN_PICKS_SAVE)

    stored = db.get_slate(GUILD, db.server_today().isoformat())
    assert [(m["a_id"], m["b_id"]) for m in stored["meetings"]] == [(_rid("Alfa"), _rid("Bravo"))]


async def test_no_select_ever_offers_more_than_discord_will_carry(cd_db):
    """Thirty-two players in one warzone is more than a select holds, and a name
    a cut drops cannot be entered at all. Paging is what stops that."""
    grouping = _grouping("knockouts", warzones=("738",))
    _place(
        grouping,
        "knockouts",
        None,
        [(name, "738", i, None) for i, name in enumerate(NATO, start=1)],
    )
    view = _view(grouping)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")

    by_name = sorted(NATO, key=str.lower)
    players = _select_by_placeholder(view, hub._PICKS_PICK_P1)
    assert len(players.options) == hub._PICK_OPTIONS
    assert "page 1 of 2" in players.placeholder
    assert [o.label for o in players.options] == by_name[: hub._PICK_OPTIONS]

    await _press(view, "Next ▶")
    players = _select_by_placeholder(view, hub._PICKS_PICK_P1)
    assert [o.label for o in players.options] == by_name[hub._PICK_OPTIONS :]


async def test_the_view_never_exceeds_discords_five_rows(cd_db):
    """Warzone, Player 1, Player 2, a pager and the buttons is exactly five, and
    a select takes a whole row."""
    grouping = _grouping("knockouts", warzones=("738",))
    _place(
        grouping,
        "knockouts",
        None,
        [(name, "738", i, None) for i, name in enumerate(NATO, start=1)],
    )
    view = _view(grouping)
    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))

    assert {item.row for item in view.children} <= {0, 1, 2, 3, 4}
    for row in range(5):
        assert len([item for item in view.children if item.row == row]) <= 5


async def test_the_twenty_first_meeting_opens_a_second_card(cd_db):
    """Twenty is what stays legible on the image, not what the data can hold.
    Overflow opens a card and never drops a row."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping)
    day = db.server_today().isoformat()
    ids = [_rid(name) for name in NATO]
    # Twenty distinct meetings out of a field of 32. A player meets two people
    # on a two-meeting day, so the same name on two rows is the normal case.
    db.set_slate(
        GUILD,
        day,
        [(ids[i + 4], ids[i + 5]) for i in range(db.MAX_PICKS)],
        actor=ACTOR,
    )
    view = _view(grouping, play_on=day)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P2), _rid("Delta"))
    await _press(view, hub.CD_BTN_PICKS_SAVE)

    assert len(db.get_slate(GUILD, day, card_no=1)["meetings"]) == db.MAX_PICKS
    assert len(db.get_slate(GUILD, day, card_no=2)["meetings"]) == 1
    assert view.state["card_no"] == 2


async def test_taking_the_last_meeting_off_deletes_the_card(cd_db):
    """`set_slate` refuses an empty list by design: "no card for tomorrow yet"
    and "a card with nothing on it" are different things to say."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], actor=ACTOR)
    view = _view(grouping, play_on=day)

    remove = _select_by_placeholder(view, hub._PICKS_PICK_REMOVE)
    # The option names the two players rather than the row's place on the card.
    assert remove.options[0].value == f"{_rid('Alfa')}:{_rid('Bravo')}"
    await _pick(view, remove, remove.options[0].value)

    assert db.get_slate(GUILD, day) is None


async def test_a_pair_already_carded_is_marked_rather_than_hidden(cd_db):
    """Dropping it would read as "these two cannot meet", which is the one thing
    this select is otherwise saying."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], actor=ACTOR)
    view = _view(grouping, play_on=day)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))

    options = _select_by_placeholder(view, hub._PICKS_PICK_P2).options
    marked = next(o for o in options if o.label == "Bravo")
    assert marked.description == hub._PICKS_TAKEN.format(n=1)


async def test_the_same_pair_the_other_way_round_is_refused_by_the_write(cd_db):
    """The selects mark what a read already knew about. `set_slate` is the
    authority, and it catches the pair somebody carded while this was on
    screen."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    view = _view(grouping, play_on=day)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P2), _rid("Bravo"))
    # Somebody else cards the same meeting the other way round in the meantime.
    db.set_slate(GUILD, day, [(_rid("Bravo"), _rid("Alfa"))], card_no=2, actor=ACTOR)
    inter = await _press(view, hub.CD_BTN_PICKS_SAVE)

    assert db.get_slate(GUILD, day, card_no=1) is None
    notice = inter.followup.send.call_args.args[0]
    assert notice.startswith("⚠️")
    assert "card 2" in notice


async def test_moving_to_another_day_clears_the_meeting_being_built(cd_db):
    """Every axis below the one that moved is re-resolved rather than patched. A
    Player 1 chosen for tomorrow means nothing once the day changes."""
    grouping = _grouping()
    _semifinal_field(grouping)
    view = _view(grouping)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))
    await _press(view, hub.CD_BTN_PICKS_BACK)
    tomorrow = (db.server_today() + timedelta(days=1)).isoformat()
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_DAY), tomorrow)

    assert view.state["play_on"] == tomorrow
    assert (view.warzone, view.p1, view.p2) == (None, None, None)


async def test_the_card_screen_lists_what_is_on_it(cd_db):
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(
        GUILD,
        day,
        [(_rid("Alfa"), _rid("Bravo")), (_rid("Charlie"), _rid("Delta"))],
        actor=ACTOR,
    )
    view = _view(grouping, play_on=day)

    field = view._embed().fields[0]
    assert field.name == "2 meetings"
    assert "**Alfa** vs **Bravo**" in field.value
    assert "**Charlie** vs **Delta**" in field.value


def test_the_bench_never_prints_the_prediction(cd_db):
    """The card is what says who wins. Two places to read one number off is two
    answers to one question, and only one of them gets shared."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], actor=ACTOR)

    embed = hub.build_picks_embed(hub.read_picks(GUILD, grouping, play_on=day))

    rendered = (embed.description or "") + "".join(f.value for f in embed.fields)
    assert "%" not in rendered


def test_the_subject_is_the_one_the_card_prints(cd_db):
    """Built through `Slate` rather than formatted here, so the bench and the
    card cannot disagree about which day and which card this is."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], card_no=2, actor=ACTOR)

    state = hub.read_picks(GUILD, grouping, play_on=day, card_no=2)
    slate = picks.Slate(guild_id=GUILD, play_on=day, stage=state["stage"], card_no=2)

    assert hub.build_picks_embed(state).title.endswith(slate.subject())


async def test_choosing_player_one_leaves_their_page_where_it_was(cd_db):
    """The pager moves on to Player 2 once Player 1 is chosen, and the Player 1
    select has to keep showing the page they were chosen from. Snapping it back
    to page 1 puts the people they were choosing between out of reach with
    nothing on screen to say so."""
    grouping = _grouping("knockouts", warzones=("738",))
    _place(
        grouping,
        "knockouts",
        None,
        [(name, "738", i, None) for i, name in enumerate(NATO, start=1)],
    )
    view = _view(grouping)
    by_name = sorted(NATO, key=str.lower)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _press(view, "Next \u25b6")
    await _pick(
        view,
        _select_by_placeholder(view, hub._PICKS_PICK_P1),
        _rid(by_name[hub._PICK_OPTIONS]),
    )

    players = _select_by_placeholder(view, hub._PICKS_PICK_P1)
    assert [o.label for o in players.options] == by_name[hub._PICK_OPTIONS :]
    assert view.pages["player"] == 1
    assert view.pages["opponent"] == 0


async def test_a_group_we_hold_one_player_of_carries_the_door_out(cd_db):
    """Every dead end carries its exit. A select with nothing in it and no
    sentence saying why is the flattest screen this feature can produce."""
    grouping = _grouping()
    _place(grouping, "semifinals", "A", [("Alfa", "738", 1, None)])
    view = _view(grouping)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))

    assert not [
        s
        for s in view.children
        if isinstance(s, discord.ui.Select) and hub._PICKS_PICK_P2 in (s.placeholder or "")
    ]
    assert hub._btn_words(hub.CD_BTN_RECORD) in view._embed().description


async def test_a_finished_knockout_field_has_nothing_to_pick_and_says_so(cd_db):
    """Everybody carries a placement once the bracket is over, so nobody is
    still in. The warzone select is not drawn empty, which Discord refuses."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping, played=len(NATO))
    view = _view(grouping)

    await _press(view, hub.CD_BTN_PICKS_ADD)

    assert not [s for s in view.children if isinstance(s, discord.ui.Select)]
    assert hub._PICKS_NOBODY_LEFT.split("{")[0] in view._embed().description


def test_a_reader_who_may_not_write_is_offered_no_write(cd_db):
    """A select cannot be drawn disabled the way a button can, so the only
    honest treatment is not to offer it."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], actor=ACTOR)
    state = hub.read_picks(GUILD, grouping, play_on=day)

    view = hub._PicksView(user_id=USER, guild_id=GUILD, state=state, can_write=False)

    assert not [
        s
        for s in view.children
        if isinstance(s, discord.ui.Select) and hub._PICKS_PICK_REMOVE in (s.placeholder or "")
    ]
    assert all(
        b.disabled
        for b in view.children
        if isinstance(b, discord.ui.Button) and b.label == hub.CD_BTN_PICKS_ADD
    )


async def test_adding_a_meeting_does_not_read_the_field_again(cd_db):
    """Two queries a group is a dozen a card at the semi-finals, and a
    twenty-meeting card is twenty of those reads for a list that cannot have
    changed between two taps."""
    grouping = _grouping()
    _semifinal_field(grouping)
    view = _view(grouping)

    with patch.object(hub, "_pick_field", wraps=hub._pick_field) as read:
        await _press(view, hub.CD_BTN_PICKS_ADD)
        await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
        await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))
        await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P2), _rid("Bravo"))
        await _press(view, hub.CD_BTN_PICKS_SAVE)

    assert read.call_count == 0
    assert len(view.state["field"]) == 8


async def test_a_card_stamped_for_another_round_reads_that_rounds_field(cd_db):
    """The field handed back in is dropped the moment the round it was read for
    stops being the round this card is for."""
    grouping = _grouping("knockouts")
    _semifinal_field(grouping)
    _knockout_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], stage="semifinals", actor=ACTOR)
    view = _view(grouping, play_on=day)
    assert view.state["stage"] == "semifinals"

    tomorrow = (db.server_today() + timedelta(days=1)).isoformat()
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_DAY), tomorrow)

    assert view.state["stage"] == "knockouts"
    assert len(view.state["field"]) == 32


# ── What `/code-review` found ─────────────────────────────────────────────────


async def test_adding_does_not_delete_a_meeting_somebody_else_added(cd_db):
    """`set_slate` is a full replace, so writing the card back off a snapshot
    this view read minutes ago deletes everything anybody else added in
    between, silently and with no error to catch."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], actor=ACTOR)
    view = _view(grouping, play_on=day)

    # A second officer cards a meeting while this view sits on screen.
    db.set_slate(
        GUILD,
        day,
        [(_rid("Alfa"), _rid("Bravo")), (_rid("Echo"), _rid("Foxtrot"))],
        actor=ACTOR,
    )

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Charlie"))
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P2), _rid("Delta"))
    await _press(view, hub.CD_BTN_PICKS_SAVE)

    stored = db.get_slate(GUILD, day)["meetings"]
    assert {frozenset((m["a_id"], m["b_id"])) for m in stored} == {
        frozenset((_rid("Alfa"), _rid("Bravo"))),
        frozenset((_rid("Echo"), _rid("Foxtrot"))),
        frozenset((_rid("Charlie"), _rid("Delta"))),
    }


async def test_removing_one_meeting_takes_off_that_meeting_and_no_other(cd_db):
    """Positions shift the moment anybody else edits the card, so a removal
    keyed on one takes off whatever moved into that slot instead."""
    grouping = _grouping()
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], actor=ACTOR)
    view = _view(grouping, play_on=day)
    remove = _select_by_placeholder(view, hub._PICKS_PICK_REMOVE)

    # Somebody else puts a meeting in front of it, so row 1 is now theirs.
    db.set_slate(
        GUILD,
        day,
        [(_rid("Echo"), _rid("Foxtrot")), (_rid("Alfa"), _rid("Bravo"))],
        actor=ACTOR,
    )
    await _pick(view, remove, remove.options[0].value)

    stored = db.get_slate(GUILD, day)["meetings"]
    assert [(m["a_id"], m["b_id"]) for m in stored] == [(_rid("Echo"), _rid("Foxtrot"))]


async def test_a_round_we_hold_nothing_for_still_offers_the_other_days(cd_db):
    """A reader can have a card for another day, and the day picker is the only
    way back to it. Without it, moving onto an unrecorded round strands a live
    view with no controls at all."""
    grouping = _grouping("knockouts")
    _semifinal_field(grouping)
    day = db.server_today().isoformat()
    tomorrow = (db.server_today() + timedelta(days=1)).isoformat()
    db.set_slate(GUILD, day, [(_rid("Alfa"), _rid("Bravo"))], stage="semifinals", actor=ACTOR)
    view = _view(grouping, play_on=day)

    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_DAY), tomorrow)

    assert view.state["state"] == "no_field"
    back = _select_by_placeholder(view, hub._PICKS_PICK_DAY)
    assert day in {o.value for o in back.options}
    await _pick(view, back, day)
    assert view.state["state"] == "ready"


async def test_a_full_card_rolls_onto_one_with_room_wherever_it_is(cd_db):
    """ "The card is full" may only ever mean the whole day is. A full last card
    reporting a full day while card 1 has room after a removal is a refusal
    that is not true."""
    grouping = _grouping("knockouts")
    _knockout_field(grouping)
    day = db.server_today().isoformat()
    ids = [_rid(name) for name in NATO]
    # Cards 2, 3 and 4 filled with sixty distinct meetings. A different step
    # per card, so no pair repeats and `set_slate` never refuses the fixture.
    for card, step in zip(range(2, db.MAX_CARDS_PER_DAY + 1), (1, 2, 3)):
        db.set_slate(
            GUILD,
            day,
            [(ids[i], ids[i + step]) for i in range(db.MAX_PICKS)],
            card_no=card,
            actor=ACTOR,
        )
    view = _view(grouping, play_on=day, card_no=db.MAX_CARDS_PER_DAY)

    await _press(view, hub.CD_BTN_PICKS_ADD)
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_WARZONE), "738")
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P1), _rid("Alfa"))
    await _pick(view, _select_by_placeholder(view, hub._PICKS_PICK_P2), _rid("Foxtrot"))
    inter = await _press(view, hub.CD_BTN_PICKS_SAVE)

    assert view.state["card_no"] == 1
    assert [(m["a_id"], m["b_id"]) for m in db.get_slate(GUILD, day)["meetings"]] == [
        (_rid("Alfa"), _rid("Foxtrot"))
    ]
    assert "card 1" in inter.followup.send.call_args.args[0]


def test_a_full_card_of_long_names_keeps_every_row(cd_db):
    """A field value stops at 1,024 characters and a card carries twenty rows of
    two names. A clamp would drop the tail while the heading went on counting
    them, which is the silent cut this feature refuses to make anywhere else."""
    grouping = _grouping("knockouts")
    long_names = [f"{name} {'x' * 40}" for name in NATO]
    _place(
        grouping,
        "knockouts",
        None,
        [(name, "738", i, None) for i, name in enumerate(long_names, start=1)],
    )
    day = db.server_today().isoformat()
    ids = [db.resolve_registrant(name)["id"] for name in long_names]
    db.set_slate(
        GUILD,
        day,
        [(ids[i], ids[i + 1]) for i in range(db.MAX_PICKS)],
        actor=ACTOR,
    )

    embed = hub.build_picks_embed(hub.read_picks(GUILD, grouping, play_on=day))

    rendered = "".join(f.value for f in embed.fields)
    assert embed.fields[0].name == f"{db.MAX_PICKS} meetings"
    for name in long_names[: db.MAX_PICKS + 1]:
        assert name in rendered
    assert all(len(f.value) <= 1024 for f in embed.fields)

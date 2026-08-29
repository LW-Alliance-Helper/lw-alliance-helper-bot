"""The picks surface: the card, the rows beside it, and the way to the channel.

Everything before this session built a card nobody could see. `render_slate`
drew one, the entry flow filled one in, and there was no path that put either in
front of a reader -- `champion_duel_picks.caption` had no caller outside its own
tests.

**What these cover is the one rule that shapes the whole surface.** Kevin,
2026-08-28: *"we cannot have things just on an image that are not also in
text."* So the image and the rows are one message, the rows cannot be dropped,
and a card that will not draw still goes out as text.

Every name here is obviously invented. The bot repo is public and no roster name
goes in it.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import discord
import pytest

import champion_duel_db as db
import champion_duel_hub as hub
import champion_duel_picks as picks
from tests.conftest import make_mock_interaction

GUILD = "999"
USER = 4242
ACTOR = {"discord_user_id": str(USER), "discord_name": "Tester", "guild_id": GUILD}

#: Eight obviously invented players, which is one semi-final group.
NAMES = ("Alfa", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel")

CARD = b"not really a webp"


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def _started_so_today_is(phase: str) -> str:
    first_day = {key: first for key, first, _ in db.PHASES}[phase]
    return (db._server_today() - timedelta(days=first_day)).isoformat()


def _field(names=NAMES, *, scouted=None, powers=None):
    """A group of invented players, most of them with squads on record.

    `scouted` is how many carry squads; the rest are the rows a card cannot
    predict, which this surface renders rather than refuses.
    """
    scouted = len(names) if scouted is None else scouted
    grouping = db.create_grouping(["738"], _started_so_today_is("semifinals"), origin="member")
    db.set_guild_warzone(GUILD, "738")
    group = db.get_or_create_group(grouping["id"], "semifinals", "A")
    for i, name in enumerate(names):
        reg = db.upsert_registrant(name, server="738", alliance="ABC")
        db.set_placement(group["id"], reg["id"], seed_rank=i + 1)
        if i >= scouted:
            continue
        trio = powers or (
            34_000_000 - i * 900_000,
            31_000_000 - i * 700_000,
            27_000_000 - i * 500_000,
        )
        for slot, (squad_type, power) in enumerate(
            zip(("Tank", "Missile", "Aircraft"), trio), start=1
        ):
            db.set_squad(
                reg["id"], slot, squad_type=squad_type, power=power, actor=ACTOR, source="observed"
            )
    return grouping


def _rid(name: str) -> int:
    return db.resolve_registrant(name, server="738")["id"]


def _card(*pairs, day=None):
    day = day or db.server_today().isoformat()
    db.set_slate(GUILD, day, [(_rid(a), _rid(b)) for a, b in pairs], actor=ACTOR)
    return day


def _view(grouping, *, play_on=None, can_write=True):
    state = hub.read_picks(GUILD, grouping, play_on=play_on)
    return hub._PicksView(user_id=USER, guild_id=GUILD, state=state, can_write=can_write)


def _button(view, label):
    return next(
        (
            item
            for item in view.children
            if isinstance(item, discord.ui.Button) and item.label == label
        ),
        None,
    )


def _select(view, placeholder):
    return next(
        item
        for item in view.children
        if isinstance(item, discord.ui.Select) and placeholder in (item.placeholder or "")
    )


async def _press(view, label):
    button = _button(view, label)
    assert button is not None, f"no {label!r} button on this view"
    inter = make_mock_interaction(user_id=USER)
    inter.data = {}
    await button.callback(inter)
    return inter


async def _show(view):
    """Press `Show the card` with the renderer stubbed, and hand back the send."""
    with patch.object(hub.champion_duel_image, "render_slate", return_value=CARD):
        inter = await _press(view, hub.CD_BTN_PICKS_SHOW)
    return inter.followup.send.call_args


# ── The door ──────────────────────────────────────────────────────────────────


async def test_the_card_can_be_looked_at_only_once_there_is_one(cd_db):
    """`render_slate` refuses an empty card, so a button offering to draw one
    would be a control that always fails."""
    grouping = _field()
    assert _button(_view(grouping), hub.CD_BTN_PICKS_SHOW) is None

    _card(("Alfa", "Bravo"))
    assert _button(_view(grouping), hub.CD_BTN_PICKS_SHOW) is not None


async def test_looking_at_the_card_is_not_a_write(cd_db):
    """Anybody who can open the bench can read what is on it. The write gate
    reaches the controls that change the card, and this one does not."""
    grouping = _field()
    _card(("Alfa", "Bravo"))
    view = _view(grouping, can_write=False)

    assert _button(view, hub.CD_BTN_PICKS_ADD).disabled
    assert not _button(view, hub.CD_BTN_PICKS_SHOW).disabled


# ── One message, and it carries both halves ───────────────────────────────────


async def test_the_image_and_every_row_arrive_together(cd_db):
    """The rule the whole surface is shaped by: nothing on the image that is
    not also in text, on the same message, so there is no state in which the
    drawing has arrived and the rows have not."""
    grouping = _field()
    _card(("Alfa", "Bravo"), ("Charlie", "Delta"))
    call = await _show(_view(grouping))

    assert call.kwargs["file"].filename == "champion_duel_picks.webp"
    embed = call.kwargs["embed"]
    rendered = "\n".join(f.value for f in embed.fields)
    for name in ("Alfa", "Bravo", "Charlie", "Delta"):
        assert f"**{name}**" in rendered
    assert call.kwargs["ephemeral"] is True


async def test_the_rows_are_in_the_cards_order_and_carry_no_numerals(cd_db):
    """The defect this session closes. The text used to number its rows off the
    order the maker entered them in while the image drew them strongest pick
    first, and session A had taken the numerals off the image."""
    grouping = _field(("Alfa", "Bravo", "Goliath", "Pebble"))
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (_rid("Pebble"),)
        )
    _card(("Alfa", "Bravo"), ("Goliath", "Pebble"))
    call = await _show(_view(grouping))

    rows = "\n".join(f.value for f in call.kwargs["embed"].fields).splitlines()
    assert rows[0].startswith("**Goliath**"), "the lopsided meeting was entered second"
    assert not any(row.startswith(("1.", "2.")) for row in rows)


async def test_a_full_card_puts_every_row_in_the_text(cd_db):
    """Twenty rows against an embed description's 4,096 characters, so the
    guarantee is structural. `CAPTION_TRUNCATED` and the row-dropping behind it
    are deleted rather than reworded."""
    names = tuple(f"Player{'o' * 30}{i}" for i in range(7))
    grouping = _field(names)
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :]][: db.MAX_PICKS]
    _card(*pairs)
    call = await _show(_view(grouping))

    embed = call.kwargs["embed"]
    assert sum(len(f.value.splitlines()) for f in embed.fields) == db.MAX_PICKS
    assert all(len(f.value) <= 1024 for f in embed.fields), "a field stops at 1,024"
    assert embed.fields[0].name == f"{db.MAX_PICKS} meetings"


async def test_the_image_carries_a_description_that_points_at_the_rows(cd_db):
    """Discord caps an attachment description at 1,024 and twenty decorated
    names run past it, so the alt text names the card rather than repeating it.
    The rows are on the same message either way."""
    grouping = _field()
    _card(("Alfa", "Bravo"))
    call = await _show(_view(grouping))

    description = call.kwargs["file"].description
    assert len(description) <= picks.ALT_LIMIT
    assert "Alfa" not in description


async def test_the_shared_card_names_the_stage_the_bench_does(cd_db):
    """A card can be stored with no stage on it -- a guild whose Champion Duel
    was resolved off the warzone fallback rather than pinned. The screen falls
    back to the round the grouping is playing, and the card that gets posted
    has to say the same thing: a bench headed `Semi-finals · Aug 30` above a
    shared card headed `Aug 30` is two answers to one question. Found by
    `/code-review`."""
    grouping = _field()
    day = _card(("Alfa", "Bravo"))
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE pick_slates SET stage = NULL WHERE guild_id = ? AND play_on = ?", (GUILD, day)
        )
    view = _view(grouping)

    bench = hub.build_picks_embed(view.state).title
    call = await _show(view)

    assert "Semi-finals" in bench
    assert call.kwargs["embed"].title == bench


async def test_nothing_in_this_feature_numbers_a_meeting(cd_db):
    """Three surfaces, one rule: a meeting is identified by its place in the
    list. The card has carried no numerals since Kevin took them off, the text
    beside it dropped its own, and the bench would otherwise have been the last
    one counting -- in a third order, because it lists meetings as they were
    entered and the card draws them strongest pick first."""
    grouping = _field(("Alfa", "Bravo", "Goliath", "Pebble"))
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (_rid("Pebble"),)
        )
    _card(("Alfa", "Bravo"), ("Goliath", "Pebble"))
    view = _view(grouping)

    listed = "\n".join(f.value for f in hub.build_picks_embed(view.state).fields)
    options = [o.label for o in _select(view, hub._PICKS_PICK_REMOVE).options]

    assert not any(line.strip().startswith(("`1`", "1.", "1 ")) for line in listed.splitlines())
    assert not any(label.startswith(("1.", "2.")) for label in options)
    # The bench still lists them as they were entered, and that is fine now
    # that nothing invites the reader to map a number onto the card.
    assert listed.splitlines()[0].startswith("**Alfa**")
    assert (await _show(view)).kwargs["embed"].fields[0].value.startswith("**Goliath**")


async def test_a_full_card_of_long_names_stays_inside_an_embed(cd_db):
    """Discord's binding limit on a message is 6,000 characters across the
    whole embed, not the description's 4,096 -- the rows are fields. Sixty
    characters is three times what the game lets a name be."""
    names = tuple(f"Player{'o' * 53}{i}" for i in range(7))
    grouping = _field(names)
    pairs = [(a, b) for i, a in enumerate(names) for b in names[i + 1 :]][: db.MAX_PICKS]
    _card(*pairs)
    embed = (await _show(_view(grouping))).kwargs["embed"]

    assert len(embed) < 6000, "Discord refuses the whole message past this"
    assert sum(len(f.value.splitlines()) for f in embed.fields) == db.MAX_PICKS


# ── The coin flip ─────────────────────────────────────────────────────────────


async def test_a_tie_broken_row_says_so_in_the_text_beside_the_image(cd_db):
    """Kevin, 2026-08-28: *"we can do a cap but should likely add a line of text
    in the embed itself that those are truly a coin flip."* The image still
    prints `PICK 50%`: `p_a >= p_b` names a side, and suppressing the cap would
    drop the pick from the one row where naming a side is the whole task."""
    grouping = _field(("Mirror", "Image"), powers=(30_000_000, 28_000_000, 25_000_000))
    _card(("Mirror", "Image"))
    call = await _show(_view(grouping))

    assert call.kwargs["embed"].description == picks.TEXT_COIN_FLIP


async def test_a_card_with_nothing_at_fifty_percent_says_nothing_about_them(cd_db):
    """A caveat about 50% rows on a card that has none would be explaining
    something the reader cannot see."""
    grouping = _field(("Goliath", "Pebble"))
    with db._get_conn() as conn:
        conn.execute(
            "UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (_rid("Pebble"),)
        )
    _card(("Goliath", "Pebble"))
    call = await _show(_view(grouping))

    assert not call.kwargs["embed"].description


# ── When something is missing ─────────────────────────────────────────────────


async def test_a_card_that_will_not_draw_still_sends_its_rows(cd_db):
    """The fallback is silent because the rows are identical either way and the
    embed carries every one of them. A failed render costs the picture and
    nothing else."""
    grouping = _field()
    _card(("Alfa", "Bravo"))
    view = _view(grouping)

    with patch.object(hub.champion_duel_image, "render_slate", side_effect=OSError("no artwork")):
        inter = await _press(view, hub.CD_BTN_PICKS_SHOW)

    call = inter.followup.send.call_args
    assert "file" not in call.kwargs
    assert "**Alfa**" in "\n".join(f.value for f in call.kwargs["embed"].fields)


async def test_a_card_emptied_underneath_the_reader_says_so_rather_than_failing(cd_db):
    """This view lives fifteen minutes and two officers can build one evening's
    card, so the meetings are re-read before they are drawn."""
    grouping = _field()
    day = _card(("Alfa", "Bravo"))
    view = _view(grouping)
    db.delete_slate(GUILD, day, card_no=1)

    inter = await _press(view, hub.CD_BTN_PICKS_SHOW)

    assert inter.followup.send.call_args.args[0] == hub._PICKS_NO_CARD


async def test_no_engine_is_reported_as_the_operator_problem_it_is(cd_db):
    """Rather than a card that quietly never appears."""
    grouping = _field()
    _card(("Alfa", "Bravo"))
    view = _view(grouping)

    with patch.object(picks, "assemble", side_effect=RuntimeError("engine is not installed")):
        inter = await _press(view, hub.CD_BTN_PICKS_SHOW)

    assert inter.followup.send.call_args.args[0] == hub._ENGINE_MISSING


# ── To the channel ────────────────────────────────────────────────────────────


async def test_sharing_posts_the_image_and_the_rows_together(cd_db):
    """Private by default: the maker pulls the card as an ephemeral and chooses
    to post it. An image posted without its rows is the thing this surface
    exists to refuse, so they go together there too."""
    embed = discord.Embed(title="a card")
    view = hub._SlateShareView(png=CARD, embed=embed, alt="a description", user_id=USER)
    inter = make_mock_interaction(user_id=USER)

    await view.share.callback(inter)

    call = inter.channel.send.call_args
    assert call.kwargs["embed"] is embed
    assert call.kwargs["file"].description == "a description"
    assert f"<@{USER}>" in call.args[0]
    assert view.share.disabled, "one share, and the button says so afterwards"


async def test_a_card_that_did_not_draw_is_still_shareable_as_text(cd_db):
    """The rows are the substance."""
    view = hub._SlateShareView(png=None, embed=discord.Embed(title="a card"), alt="x", user_id=USER)
    inter = make_mock_interaction(user_id=USER)

    await view.share.callback(inter)

    assert "file" not in inter.channel.send.call_args.kwargs


async def test_a_channel_the_bot_cannot_post_in_says_which_permissions(cd_db):
    """And says it once: the same sentence every share button in this file
    uses, because it is the same refusal about the same two permissions."""
    view = hub._SlateShareView(png=CARD, embed=discord.Embed(), alt="x", user_id=USER)
    inter = make_mock_interaction(user_id=USER)
    inter.channel.send = AsyncMock(side_effect=discord.Forbidden(AsyncMock(status=403), "nope"))

    await view.share.callback(inter)

    assert inter.followup.send.call_args.args[0] == hub._SHARE_DENIED


async def test_the_shared_card_is_the_one_that_was_read(cd_db):
    """Held bytes rather than a second render. A card that changes between
    being read and being shared is worse than the memory."""
    grouping = _field()
    _card(("Alfa", "Bravo"))
    view = _view(grouping)

    with patch.object(hub.champion_duel_image, "render_slate", return_value=CARD) as render:
        inter = await _press(view, hub.CD_BTN_PICKS_SHOW)
    share = inter.followup.send.call_args.kwargs["view"]

    with patch.object(hub.champion_duel_image, "render_slate", return_value=b"different") as again:
        await share.share.callback(make_mock_interaction(user_id=USER))

    assert render.call_count == 1
    assert again.call_count == 0
    assert share.png == CARD

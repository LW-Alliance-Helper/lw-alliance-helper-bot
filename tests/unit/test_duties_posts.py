"""Leadership Duties: the posted messages (#706, #707) and the routine all
three posts share."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import config_health
import duties as d
import duties_copy as c
import duties_db as db
import duties_panel
import duties_posts as posts
from tests.conftest import TEST_GUILD_ID

G = TEST_GUILD_ID
CH = 600
A, B, C = 101, 102, 103


def _duty(**kw):
    kw.setdefault("name", "Disputes")
    kw.setdefault("primaries", (A,))
    kw.setdefault("backups", (B,))
    return db.save_duty(d.Duty(id=0, guild_id=G, **kw))


def _channel(*, sent_id=66):
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(id=sent_id))
    partial = MagicMock()
    partial.edit = AsyncMock()
    partial.delete = AsyncMock()
    channel.get_partial_message = MagicMock(return_value=partial)
    return channel


def _bot_with(channel):
    bot = MagicMock()
    bot.get_channel = lambda cid: channel if cid == CH else None
    return bot


# ── The full list in the leadership channel (#706) ──────────────────────────


async def test_sharing_the_full_list_records_where_it_is(seeded_db):
    _duty()
    channel = _channel()

    message = await posts.post(_bot_with(channel), G, posts.ROSTER, CH)

    assert message.id == 66
    settings = db.get_settings(G)
    assert (settings.roster_channel_id, settings.roster_message_id) == (CH, 66)
    kwargs = channel.send.await_args.kwargs
    assert kwargs["embeds"][0].title == c.ROSTER_TITLE
    assert kwargs["allowed_mentions"].users is False
    (button,) = kwargs["view"].children
    assert (button.label, button.custom_id) == (c.BTN_MY_DUTIES, "duties_roster_mine")
    assert kwargs["view"].timeout is None


async def test_sharing_again_replaces_the_old_list(seeded_db):
    _duty()
    db.update_settings(G, roster_channel_id=CH, roster_message_id=55)
    channel = _channel()

    await posts.post(_bot_with(channel), G, posts.ROSTER, CH)

    channel.get_partial_message.assert_called_once_with(55)
    channel.get_partial_message.return_value.delete.assert_awaited_once()
    assert db.get_settings(G).roster_message_id == 66


async def test_one_post_never_resets_another(seeded_db):
    """Each post saves only its own two fields: posting the contact buttons
    must not forget where the full list is."""
    _duty(contact_enabled=True)
    db.update_settings(
        G, roster_channel_id=CH, roster_message_id=55, shared_channel_id=CH, shared_message_id=77
    )

    await duties_panel.post_panel(_bot_with(_channel(sent_id=88)), G, CH)

    settings = db.get_settings(G)
    assert settings.panel_message_id == 88
    assert (settings.roster_message_id, settings.shared_message_id) == (55, 77)


async def test_a_list_someone_deleted_is_forgotten_not_reported(seeded_db):
    db.update_settings(G, roster_channel_id=CH, roster_message_id=55)
    channel = _channel()
    channel.get_partial_message.return_value.edit = AsyncMock(
        side_effect=discord.NotFound(MagicMock(status=404), "gone")
    )

    assert not await posts.refresh(_bot_with(channel), G, posts.ROSTER)
    assert db.get_settings(G).roster_message_id == 0
    assert config_health.problems(G) == []


async def test_a_list_in_a_channel_the_bot_lost_is_reported(seeded_db):
    db.update_settings(G, roster_channel_id=CH, roster_message_id=55)

    assert not await posts.refresh(_bot_with(None), G, posts.ROSTER)

    (problem,) = config_health.problems(G)
    assert problem.subject == posts.ROSTER.subject
    assert problem.kind == config_health.CHANNEL_GONE


async def test_every_change_refreshes_all_three_posts(seeded_db):
    with patch("duties_posts.refresh", AsyncMock()) as refresh:
        await posts.refresh_all(MagicMock(), G)
    kinds = [call.args[2] for call in refresh.await_args_list]
    assert kinds == [duties_panel.CONTACT, posts.ROSTER, posts.SHARED]


def test_the_my_duties_button_survives_a_redeploy():
    bot = MagicMock()
    posts.register_persistent_items(bot)
    (view,) = bot.add_view.call_args.args
    assert isinstance(view, posts.RosterView)


# ── Who the My duties button answers ─────────────────────────────────────────

LEADERSHIP = MagicMock(name="leadership role")


def _member(uid, *roles):
    return MagicMock(id=uid, roles=list(roles))


def test_anyone_with_the_leadership_role_can_see_their_duties():
    assert posts.can_see_own_duties(_member(C, LEADERSHIP), LEADERSHIP, [])


def test_someone_assigned_without_the_role_can_still_see_theirs():
    duties = [d.Duty(id=1, guild_id=G, name="X", backups=(C,))]
    assert posts.can_see_own_duties(_member(C), LEADERSHIP, duties)
    assert posts.can_see_own_duties(_member(C), None, duties)


def test_everyone_else_is_turned_away():
    duties = [d.Duty(id=1, guild_id=G, name="X", primaries=(A, d.ANYONE))]
    assert not posts.can_see_own_duties(_member(C), LEADERSHIP, duties)
    assert not posts.can_see_own_duties(_member(C), None, duties)


def _click(user):
    inter = MagicMock()
    inter.guild = MagicMock(id=G)
    inter.user = user
    inter.response.send_message = AsyncMock()
    return inter


async def test_the_button_shows_the_clicker_their_own_duties(seeded_db):
    _duty(name="Disputes", primaries=(A,))
    _duty(name="Schedule", primaries=(B,), backups=(A,))
    inter = _click(_member(A))
    with patch("duties_hub.leadership_role", return_value=None):
        await posts.show_my_duties(inter)
    kwargs = inter.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    fields = {f.name: f.value for f in kwargs["embed"].fields}
    assert "Disputes" in fields[c.MY_PRIMARY_FIELD]
    assert "Schedule" in fields[c.MY_BACKUP_FIELD]


async def test_the_button_refuses_someone_outside_leadership(seeded_db):
    _duty(primaries=(A,))
    inter = _click(_member(C))
    with patch("duties_hub.leadership_role", return_value=LEADERSHIP):
        await posts.show_my_duties(inter)
    inter.response.send_message.assert_awaited_once_with(c.ROSTER_DENIED, ephemeral=True)


# ── The curated list shared with members (#707) ─────────────────────────────


async def test_the_curated_list_shows_only_what_was_picked_and_is_running(seeded_db):
    shown = _duty(name="VS Scheduling")
    _duty(name="Disputes")
    paused = _duty(name="Season Planning", paused=True)
    db.set_shared(G, [shown, paused])
    channel = _channel()

    await posts.post(_bot_with(channel), G, posts.SHARED, CH)

    kwargs = channel.send.await_args.kwargs
    text = "\n".join(e.description or "" for e in kwargs["embeds"])
    assert "VS Scheduling" in text
    assert "Disputes" not in text and "Season Planning" not in text
    assert "view" not in kwargs  # no buttons on this one
    settings = db.get_settings(G)
    assert (settings.shared_channel_id, settings.shared_message_id) == (CH, 66)


@pytest.mark.parametrize("backups", [True, False])
async def test_the_curated_list_names_backups_only_when_asked(seeded_db, backups):
    db.set_shared(G, [_duty()])
    db.update_settings(G, shared_backups=backups)
    channel = _channel()

    await posts.post(_bot_with(channel), G, posts.SHARED, CH)

    text = channel.send.await_args.kwargs["embeds"][0].description
    assert (f"<@{B}>" in text) is backups
    assert f"<@{A}>" in text

"""Leadership Duties (#687): the contact buttons, and the threads they open."""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import config_health
import duties as d
import duties_copy as c
import duties_db as db
import duties_health as dh
import duties_panel as p
from tests.conftest import TEST_GUILD_ID

G = TEST_GUILD_ID
PANEL_CH = 600
A, B = 101, 102
MEMBER = 900


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    p._recent.clear()
    p._in_flight.clear()
    yield
    p._recent.clear()
    p._in_flight.clear()


def _perms(**kw):
    base = dict(
        view_channel=True,
        send_messages=True,
        create_private_threads=True,
        send_messages_in_threads=True,
    )
    base.update(kw)
    return MagicMock(**base)


def _text_channel(*, bot_perms=None, member_view=True):
    ch = MagicMock(spec=discord.TextChannel)
    ch.id = PANEL_CH

    def _perms_for(who):
        if getattr(who, "is_bot_me", False):
            return bot_perms or _perms()
        return _perms(view_channel=member_view)

    ch.permissions_for = MagicMock(side_effect=_perms_for)
    thread = MagicMock()
    thread.id = 4242
    thread.mention = "<#4242>"
    thread.add_user = AsyncMock()
    thread.send = AsyncMock()
    ch.create_thread = AsyncMock(return_value=thread)
    ch.thread = thread
    return ch


def _interaction(channel, *, member_display="Tester"):
    guild = MagicMock()
    guild.id = G
    guild.me = MagicMock(is_bot_me=True)
    guild.get_member = lambda uid: MagicMock() if uid in (A, B, MEMBER) else None
    guild.get_channel = lambda cid: channel if cid == PANEL_CH else None
    inter = MagicMock()
    inter.guild = guild
    inter.guild_id = G
    inter.channel_id = PANEL_CH
    inter.user = MagicMock(id=MEMBER, display_name=member_display, is_bot_me=False)
    inter.response = MagicMock()
    inter.response.defer = AsyncMock()
    inter.response.is_done = MagicMock(return_value=True)
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    inter.client = MagicMock()
    return inter


def _replies(inter):
    return [call.args[0] for call in inter.followup.send.await_args_list]


def _duty(**kw):
    kw.setdefault("name", "Disputes")
    kw.setdefault("primaries", (A,))
    kw.setdefault("backups", (B,))
    kw.setdefault("contact_enabled", True)
    return db.save_duty(d.Duty(id=0, guild_id=G, **kw))


def _settings(**kw):
    db.save_settings(db.DutiesSettings(guild_id=G, panel_channel_id=PANEL_CH, **kw))


@pytest.fixture
def premium_on():
    with patch("premium.feature_gate", AsyncMock(return_value=True)):
        yield


# ── Opening a thread ─────────────────────────────────────────────────────────


async def test_a_click_opens_a_private_thread_with_the_holders(seeded_db, premium_on):
    duty_id = _duty()
    _settings()
    channel = _text_channel()
    inter = _interaction(channel)

    await p.open_ticket(inter, duty_id)

    inter.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    kwargs = channel.create_thread.await_args.kwargs
    assert kwargs["type"] == discord.ChannelType.private_thread
    assert kwargs["invitable"] is False
    assert kwargs["name"].startswith("Disputes · Tester · ")
    added = [call.args[0].id for call in channel.thread.add_user.await_args_list]
    assert added == [MEMBER, A, B]
    opener = channel.thread.send.await_args
    assert opener.args[0].startswith(f"<@{MEMBER}> opened this about **Disputes**")
    assert isinstance(opener.kwargs["view"], p.TicketCloseView)
    assert _replies(inter) == [c.TICKET_READY.format(thread="<#4242>")]


async def test_a_second_click_gets_the_same_thread(seeded_db, premium_on):
    duty_id = _duty()
    _settings()
    channel = _text_channel()
    await p.open_ticket(_interaction(channel), duty_id)
    again = _interaction(channel)
    await p.open_ticket(again, duty_id)
    assert channel.create_thread.await_count == 1
    assert _replies(again) == [c.TICKET_RECENT.format(duty="Disputes", thread="<#4242>")]


async def test_without_premium_the_member_is_told_it_isnt_available(seeded_db):
    duty_id = _duty()
    channel = _text_channel()
    inter = _interaction(channel)
    with patch("premium.feature_gate", AsyncMock(return_value=False)):
        await p.open_ticket(inter, duty_id)
    assert _replies(inter) == [c.TICKET_UNAVAILABLE]
    channel.create_thread.assert_not_awaited()


@pytest.mark.parametrize("change", [{"paused": True}, {"contact_enabled": False}])
async def test_a_duty_no_longer_taking_messages_says_so_and_fixes_the_post(
    seeded_db, premium_on, change
):
    duty_id = _duty(**change)
    inter = _interaction(_text_channel())
    with patch("duties_panel.refresh_panel", AsyncMock()) as refresh:
        await p.open_ticket(inter, duty_id)
    assert _replies(inter) == [c.TICKET_DUTY_UNAVAILABLE.format(duty="Disputes")]
    refresh.assert_awaited_once()


async def test_a_duty_with_nobody_holding_it(seeded_db, premium_on):
    duty_id = _duty(primaries=(d.OPEN,), backups=())
    inter = _interaction(_text_channel())
    await p.open_ticket(inter, duty_id)
    assert _replies(inter) == [c.TICKET_NOBODY.format(duty="Disputes")]


@pytest.mark.parametrize(
    ("channel_kw", "kind"),
    [
        ({"bot_perms": _perms(create_private_threads=False)}, config_health.NO_THREADS),
        ({"bot_perms": _perms(view_channel=False)}, config_health.CHANNEL_NO_VIEW),
        ({"member_view": False}, config_health.MEMBERS_CANT_VIEW),
    ],
)
async def test_a_ticket_channel_that_wont_work_is_recorded(seeded_db, premium_on, channel_kw, kind):
    duty_id = _duty()
    _settings()
    channel = _text_channel(**channel_kw)
    inter = _interaction(channel)
    await p.open_ticket(inter, duty_id)
    channel.create_thread.assert_not_awaited()
    assert _replies(inter) == [c.TICKET_FAILED]
    (problem,) = config_health.problems(G)
    assert (problem.subject, problem.kind) == (dh.TICKET_CHANNEL, kind)


async def test_discord_refusing_the_thread_is_recorded(seeded_db, premium_on):
    duty_id = _duty()
    _settings()
    channel = _text_channel()
    channel.create_thread.side_effect = discord.Forbidden(MagicMock(status=403), "no")
    inter = _interaction(channel)
    await p.open_ticket(inter, duty_id)
    assert _replies(inter) == [c.TICKET_FAILED]
    assert config_health.problems(G)[0].kind == config_health.NO_THREADS


async def test_a_holder_who_cant_be_added_doesnt_cost_the_member_their_thread(
    seeded_db, premium_on
):
    duty_id = _duty()
    _settings()
    channel = _text_channel()
    channel.thread.add_user.side_effect = [
        None,
        discord.HTTPException(MagicMock(status=400), "x"),
        None,
    ]
    inter = _interaction(channel)
    await p.open_ticket(inter, duty_id)
    assert _replies(inter) == [c.TICKET_READY.format(thread="<#4242>")]


# ── Closing ──────────────────────────────────────────────────────────────────


def _close_inter(is_leader: bool):
    inter = MagicMock()
    inter.channel = MagicMock(spec=discord.Thread)
    inter.channel.edit = AsyncMock()
    inter.user = MagicMock(mention="<@5>")
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    return inter


async def test_close_archives_without_locking():
    view = p.TicketCloseView()
    inter = _close_inter(True)
    with patch("storm_permissions.is_leader_or_admin", return_value=True):
        await view.close.callback(inter)
    inter.response.send_message.assert_awaited_once()
    assert inter.response.send_message.await_args.args[0] == c.THREAD_CLOSED.format(who="<@5>")
    inter.channel.edit.assert_awaited_once_with(archived=True, locked=False)


async def test_only_leadership_can_close():
    view = p.TicketCloseView()
    inter = _close_inter(False)
    with patch("storm_permissions.is_leader_or_admin", return_value=False):
        await view.close.callback(inter)
    inter.response.send_message.assert_awaited_once_with(c.CLOSE_DENIED, ephemeral=True)
    inter.channel.edit.assert_not_awaited()


def test_the_close_button_and_contact_buttons_are_persistent():
    assert p.TicketCloseView().timeout is None
    assert p.TicketCloseView().children[0].custom_id == "duties_ticket_close"
    view = p.build_panel_view(
        [
            d.Duty(id=7, guild_id=G, name="Disputes", contact_enabled=True),
            d.Duty(id=8, guild_id=G, name="Paused", contact_enabled=True, paused=True),
        ]
    )
    assert view.timeout is None
    assert [(b.item.label, b.custom_id) for b in view.children] == [("Disputes", "duty_contact:7")]


# ── The posted message ───────────────────────────────────────────────────────


def _bot_with(channel):
    bot = MagicMock()
    bot.get_channel = lambda cid: channel if cid == PANEL_CH else None
    return bot


async def test_refresh_edits_the_posted_message(seeded_db):
    _duty()
    _settings(panel_message_id=55)
    channel = MagicMock()
    partial = MagicMock()
    partial.edit = AsyncMock()
    channel.get_partial_message = MagicMock(return_value=partial)

    assert await p.refresh_panel(_bot_with(channel), G)

    channel.get_partial_message.assert_called_once_with(55)
    kwargs = partial.edit.await_args.kwargs
    assert kwargs["embed"].fields[0].name == "Disputes"
    assert kwargs["allowed_mentions"].users is False


async def test_refresh_forgets_a_message_someone_deleted(seeded_db):
    _settings(panel_message_id=55)
    channel = MagicMock()
    partial = MagicMock()
    partial.edit = AsyncMock(side_effect=discord.NotFound(MagicMock(status=404), "gone"))
    channel.get_partial_message = MagicMock(return_value=partial)

    assert not await p.refresh_panel(_bot_with(channel), G)
    assert db.get_settings(G).panel_message_id == 0
    assert config_health.problems(G) == []


async def test_refresh_without_a_post_does_nothing(seeded_db):
    assert not await p.refresh_panel(_bot_with(None), G)


async def test_posting_again_replaces_the_old_message(seeded_db):
    _duty()
    _settings(panel_message_id=55, ticket_channel_id=77)
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock(id=66))
    old = MagicMock()
    old.delete = AsyncMock()
    channel.get_partial_message = MagicMock(return_value=old)

    message = await p.post_panel(_bot_with(channel), G, PANEL_CH)

    assert message.id == 66
    channel.get_partial_message.assert_called_once_with(55)
    old.delete.assert_awaited_once()
    assert db.get_settings(G) == db.DutiesSettings(
        guild_id=G, panel_channel_id=PANEL_CH, panel_message_id=66, ticket_channel_id=77
    )


async def test_posting_where_the_bot_cant_is_recorded(seeded_db):
    channel = MagicMock()
    channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "no"))
    assert await p.post_panel(_bot_with(channel), G, PANEL_CH) is None
    assert config_health.problems(G)[0].kind == config_health.CHANNEL_NO_SEND

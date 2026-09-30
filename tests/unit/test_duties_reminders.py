"""Leadership Duties (#687): the reminder loop and a holder leaving."""

from datetime import date, datetime, time, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import config
import config_health
import duties as d
import duties_db as db
import duties_health as dh
import duties_reminders as rem
from tests.conftest import TEST_GUILD_ID

G = TEST_GUILD_ID
LEAD_CH = 111111111111111111  # seeded leadership channel
POST_CH = 555
A, B, C = 101, 102, 103

# 21:00 in New York (the seeded timezone) on Monday 28 Sep 2026.
MONDAY_9PM = datetime(2026, 9, 29, 1, 0, tzinfo=timezone.utc)


def _member(uid, name, *, dm_fails=False):
    m = MagicMock()
    m.id = uid
    m.display_name = name
    m.mention = f"<@{uid}>"
    m.send = AsyncMock(
        side_effect=discord.Forbidden(MagicMock(status=403), "no DMs") if dm_fails else None
    )
    return m


def _channel(cid, guild):
    ch = MagicMock()
    ch.id = cid
    ch.guild = guild
    ch.send = AsyncMock()
    perms = MagicMock(view_channel=True, send_messages=True)
    ch.permissions_for = MagicMock(return_value=perms)
    return ch


class World:
    """A server with three leaders, a leadership channel and a post channel."""

    def __init__(self, *, dm_fails=()):
        self.guild = MagicMock()
        self.guild.id = G
        self.guild.name = "Test Server"
        self.guild.me = MagicMock()
        self.members = {
            uid: _member(uid, name, dm_fails=uid in dm_fails)
            for uid, name in ((A, "Alpha"), (B, "Bravo"), (C, "Charlie"))
        }
        self.guild.get_member = lambda uid: self.members.get(uid)
        role = MagicMock()
        role.mention = "<@&777>"
        self.guild.get_role = lambda rid: role
        self.guild.roles = [role]
        self.role = role
        self.channels = {
            LEAD_CH: _channel(LEAD_CH, self.guild),
            POST_CH: _channel(POST_CH, self.guild),
        }
        self.bot = MagicMock()
        self.bot.get_guild = lambda gid: self.guild if gid == G else None
        self.bot.get_channel = lambda cid: self.channels.get(cid)


@pytest.fixture
def premium_on():
    with patch("premium.feature_gate", AsyncMock(return_value=True)) as gate:
        yield gate


@pytest.fixture
def leadership_role_set(seeded_db):
    cfg = config.get_config(G)
    cfg.leadership_role_id = 777
    config.save_config(cfg)


def _duty(**kw):
    kw.setdefault("name", "Daily Schedule")
    return db.save_duty(d.Duty(id=0, guild_id=G, **kw))


def _reminder(duty_id, **kw):
    kw.setdefault("message", "{primary}, post today's schedule")
    kw.setdefault("schedule_type", d.SCHEDULE_WEEKDAYS)
    kw.setdefault("weekdays", frozenset({0}))
    kw.setdefault("at", time(21, 0))
    return db.save_reminder(d.DutyReminder(id=0, guild_id=G, duty_id=duty_id, **kw))


async def test_a_due_reminder_dms_the_primaries_once(seeded_db, premium_on):
    w = World()
    _reminder(_duty(primaries=(A, B)))

    assert await rem.run_reminder_tick(w.bot, MONDAY_9PM) == 1
    assert await rem.run_reminder_tick(w.bot, MONDAY_9PM) == 0  # claimed

    w.members[A].send.assert_awaited_once_with(
        "🔔 **Daily Schedule** reminder from **Test Server**\nAlpha, Bravo, post today's schedule"
    )
    w.members[B].send.assert_awaited_once()
    w.members[C].send.assert_not_awaited()
    assert db.list_reminders(G)[0].last_fired_on == date(2026, 9, 28)


async def test_nothing_is_due_before_the_time(seeded_db, premium_on):
    w = World()
    _reminder(_duty(primaries=(A,)))
    early = datetime(2026, 9, 29, 0, 59, tzinfo=timezone.utc)
    assert await rem.run_reminder_tick(w.bot, early) == 0
    premium_on.assert_not_awaited()  # nothing due, no Premium lookup


async def test_a_paused_duty_sends_nothing(seeded_db, premium_on):
    w = World()
    _reminder(_duty(primaries=(A,), paused=True))
    assert await rem.run_reminder_tick(w.bot, MONDAY_9PM) == 0
    w.members[A].send.assert_not_awaited()


async def test_without_premium_the_occurrence_is_used_up_not_sent(seeded_db):
    w = World()
    _reminder(_duty(primaries=(A,)))
    with patch("premium.feature_gate", AsyncMock(return_value=False)):
        assert await rem.run_reminder_tick(w.bot, MONDAY_9PM) == 0
    w.members[A].send.assert_not_awaited()
    # Resubscribing later the same day waits for the next time.
    assert db.list_reminders(G)[0].last_fired_on == date(2026, 9, 28)


async def test_an_undeliverable_dm_falls_back_to_the_leadership_channel(seeded_db, premium_on):
    w = World(dm_fails={B})
    _reminder(_duty(primaries=(A, B)))

    await rem.run_reminder_tick(w.bot, MONDAY_9PM)

    lead = w.channels[LEAD_CH]
    lead.send.assert_awaited_once()
    content = lead.send.await_args.args[0]
    assert content.startswith(f"🔔 I couldn't DM <@{B}> this **Daily Schedule** reminder")
    allowed = lead.send.await_args.kwargs["allowed_mentions"]
    assert [u.id for u in allowed.users] == [B]


async def test_a_channel_post_pings_only_when_asked(seeded_db, premium_on):
    w = World()
    duty_id = _duty(primaries=(A,), backups=(B,))
    _reminder(duty_id, send_to=d.SEND_CHANNEL, channel_id=POST_CH, ping_holders=False)

    await rem.run_reminder_tick(w.bot, MONDAY_9PM)

    post = w.channels[POST_CH].send
    content = post.await_args.args[0]
    assert content == f"🔔 **Daily Schedule**\n<@{A}>, post today's schedule"
    assert post.await_args.kwargs["allowed_mentions"].users == []


async def test_a_channel_post_with_ping(seeded_db, premium_on):
    w = World()
    duty_id = _duty(primaries=(A,), backups=(B,))
    _reminder(duty_id, send_to=d.SEND_CHANNEL, channel_id=POST_CH, ping_holders=True)

    await rem.run_reminder_tick(w.bot, MONDAY_9PM)

    post = w.channels[POST_CH].send
    assert post.await_args.args[0].startswith(f"<@{A}> <@{B}>\n")
    assert [u.id for u in post.await_args.kwargs["allowed_mentions"].users] == [A, B]


async def test_anyone_in_leadership_posts_in_the_leadership_channel(
    leadership_role_set, premium_on
):
    w = World()
    duty_id = _duty(primaries=(A,), backups=(C, d.ANYONE))
    _reminder(duty_id, send_to=d.SEND_BOTH, ping_holders=True)

    await rem.run_reminder_tick(w.bot, MONDAY_9PM)

    w.members[A].send.assert_awaited_once()
    w.members[C].send.assert_awaited_once()
    post = w.channels[LEAD_CH].send
    assert post.await_args.args[0].startswith("<@&777>\n")
    assert post.await_args.kwargs["allowed_mentions"].roles == [w.role]


async def test_a_channel_the_bot_cannot_see_is_recorded(seeded_db, premium_on):
    w = World()
    del w.channels[POST_CH]
    _reminder(_duty(primaries=(A,)), send_to=d.SEND_CHANNEL, channel_id=POST_CH)

    await rem.run_reminder_tick(w.bot, MONDAY_9PM)

    (problem,) = config_health.problems(G)
    assert (problem.subject, problem.kind) == (
        dh.reminder_channel_subject(POST_CH),
        config_health.CHANNEL_GONE,
    )
    assert problem.label == config_health.get_subject(dh.REMINDER_CHANNEL).label


async def test_one_broken_server_does_not_stop_the_others(seeded_db, premium_on):
    w = World()
    _reminder(_duty(primaries=(A,)))
    with patch("duties_reminders.deliver", AsyncMock(side_effect=RuntimeError("boom"))):
        assert await rem.run_reminder_tick(w.bot, MONDAY_9PM) == 0  # logged, not raised


async def test_a_holder_leaving_opens_their_positions_and_posts_a_notice(seeded_db):
    w = World()
    duty_id = _duty(primaries=(A, B))
    leaving = w.members[B]
    leaving.guild = w.guild
    with patch("duties_panel.refresh_panel", AsyncMock()) as refresh:
        names = await rem.handle_departure(w.bot, leaving)
    assert names == ["Daily Schedule"]
    assert db.get_duty(G, duty_id).primaries == (A, d.OPEN)
    (problem,) = config_health.problems(G)
    assert problem.kind == config_health.HOLDER_LEFT
    refresh.assert_awaited_once()


async def test_someone_holding_nothing_leaving_changes_nothing(seeded_db):
    w = World()
    _duty(primaries=(A,))
    leaving = w.members[C]
    leaving.guild = w.guild
    with patch("duties_panel.refresh_panel", AsyncMock()) as refresh:
        assert await rem.handle_departure(w.bot, leaving) == []
    refresh.assert_not_awaited()
    assert config_health.problems(G) == []


async def test_a_working_channel_does_not_clear_a_broken_one(seeded_db, premium_on):
    w = World()
    duty_id = _duty(primaries=(A,))
    _reminder(duty_id, send_to=d.SEND_CHANNEL, channel_id=POST_CH)
    _reminder(duty_id, send_to=d.SEND_CHANNEL, channel_id=999)  # doesn't exist

    await rem.run_reminder_tick(w.bot, MONDAY_9PM)

    (problem,) = config_health.problems(G)
    assert problem.subject == dh.reminder_channel_subject(999)

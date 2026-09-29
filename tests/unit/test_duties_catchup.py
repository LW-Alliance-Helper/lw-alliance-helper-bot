"""Leadership Duties (#687): reminders whose whole day passed during an
outage, offered in the catch-up digest."""

from datetime import date, datetime, time, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import config
import duties as d
import duties_db as db
import outage_catchup as oc
from tests.conftest import TEST_GUILD_ID

G = TEST_GUILD_ID
A = 101

# Down from Sunday 27 Sep 20:00 to Monday 28 Sep 12:00, New York time.
WINDOW = oc.OutageWindow(
    start=datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc),
    end=datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc),
)


def _guild():
    guild = MagicMock()
    guild.id = G
    return guild


def _setup(**kw):
    duty_id = db.save_duty(d.Duty(id=0, guild_id=G, name="Daily Schedule", primaries=(A,)))
    kw.setdefault("message", "go")
    kw.setdefault("schedule_type", d.SCHEDULE_WEEKDAYS)
    kw.setdefault("at", time(21, 0))
    db.save_reminder(d.DutyReminder(id=0, guild_id=G, duty_id=duty_id, **kw))
    return duty_id


@pytest.fixture
def premium_on():
    with patch("premium.feature_gate", AsyncMock(return_value=True)):
        yield


async def test_yesterdays_missed_reminder_is_offered(seeded_db, premium_on):
    _setup(weekdays=frozenset({6}))  # Sunday 21:00, inside the window
    items = await oc.scan_duty_reminders(MagicMock(), _guild(), config.get_config(G), WINDOW)
    (item,) = items
    assert item.title == "Leadership Duties reminder: Daily Schedule"
    assert item.destination == "sent as DMs to the holders"
    assert item.scheduled_local.date() == date(2026, 9, 27)


async def test_todays_is_left_to_the_live_loop(seeded_db, premium_on):
    _setup(weekdays=frozenset({0}), at=time(8, 0))  # Monday 08:00, inside the window
    assert await oc.scan_duty_reminders(MagicMock(), _guild(), config.get_config(G), WINDOW) == []


async def test_one_that_already_went_out_is_not_offered(seeded_db, premium_on):
    _setup(weekdays=frozenset({6}))
    rid = db.list_reminders(G)[0].id
    db.mark_reminder_fired(G, rid, date(2026, 9, 27))
    assert await oc.scan_duty_reminders(MagicMock(), _guild(), config.get_config(G), WINDOW) == []


async def test_nothing_is_offered_without_premium(seeded_db):
    _setup(weekdays=frozenset({6}))
    with patch("premium.feature_gate", AsyncMock(return_value=False)):
        items = await oc.scan_duty_reminders(MagicMock(), _guild(), config.get_config(G), WINDOW)
    assert items == []


async def test_sending_it_delivers_and_marks_it(seeded_db, premium_on):
    _setup(weekdays=frozenset({6}))
    (item,) = await oc.scan_duty_reminders(MagicMock(), _guild(), config.get_config(G), WINDOW)
    with patch("duties_reminders.deliver", AsyncMock(return_value=True)) as deliver:
        assert await item.fire()
    deliver.assert_awaited_once()
    assert db.list_reminders(G)[0].last_fired_on == date(2026, 9, 27)


async def test_a_duty_paused_since_the_digest_is_not_sent(seeded_db, premium_on):
    duty_id = _setup(weekdays=frozenset({6}))
    (item,) = await oc.scan_duty_reminders(MagicMock(), _guild(), config.get_config(G), WINDOW)
    db.set_duty_paused(G, duty_id, True)
    with patch("duties_reminders.deliver", AsyncMock(return_value=True)) as deliver:
        assert not await item.fire()
    deliver.assert_not_awaited()


def test_the_loop_is_an_outage_signal():
    assert "duty_reminder" in oc.HEARTBEAT_LOOPS
    assert oc.scan_duty_reminders in oc.SURFACE_ADAPTERS

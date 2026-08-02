"""
Tests for the #363 fix: scheduler.py's 5-minute-warning queue must survive a
restart instead of being silently dropped.

Covers two layers:
  * config.py's save/load/delete_pending_warning round-trip (including the
    `dt` datetime field inside each event dict, which needs special
    serialization since json.dumps can't handle it directly).
  * scheduler.run_scheduler's startup recovery: an overdue warning fires
    immediately instead of vanishing; a still-upcoming one is restored into
    the in-memory `pending_warnings` dict for the normal trigger loop to
    pick up.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

ET = ZoneInfo("America/New_York")

GUILD_ID = 12345
LEADERSHIP_CHAN_ID = 1111
ANNOUNCEMENT_CHAN_ID = 2222


def _make_cfg():
    cfg = MagicMock()
    cfg.guild_id = GUILD_ID
    cfg.leadership_channel_id = LEADERSHIP_CHAN_ID
    cfg.announcement_channel_id = ANNOUNCEMENT_CHAN_ID
    return cfg


def _event_list():
    return [
        {
            "key": "marauder",
            "name": "Marauder",
            "dt": datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            "blurb": "Marauder at {time}.",
        }
    ]


# ── config.py persistence round-trip ──────────────────────────────────────────


class TestPendingWarningPersistence:
    def test_save_and_load_round_trips_including_event_dt(self, temp_db):
        import config

        warn_dt = datetime(2026, 5, 15, 21, 55, tzinfo=ET)
        config.save_pending_warning("evt-1", GUILD_ID, warn_dt, _event_list())

        restored = config.load_pending_warnings()

        assert "evt-1" in restored
        got_warn_dt, got_events, got_guild_id = restored["evt-1"]
        assert got_warn_dt == warn_dt
        assert got_guild_id == GUILD_ID
        assert got_events[0]["key"] == "marauder"
        assert got_events[0]["dt"] == datetime(2026, 5, 15, 22, 0, tzinfo=ET)

    def test_save_is_idempotent_upsert(self, temp_db):
        """Re-saving the same event_key overwrites rather than duplicating."""
        import config

        warn_dt = datetime(2026, 5, 15, 21, 55, tzinfo=ET)
        config.save_pending_warning("evt-1", GUILD_ID, warn_dt, _event_list())
        later = warn_dt + timedelta(minutes=1)
        config.save_pending_warning("evt-1", GUILD_ID, later, _event_list())

        restored = config.load_pending_warnings()
        assert len(restored) == 1
        assert restored["evt-1"][0] == later

    def test_delete_removes_the_row(self, temp_db):
        import config

        config.save_pending_warning(
            "evt-1", GUILD_ID, datetime(2026, 5, 15, 21, 55, tzinfo=ET), _event_list()
        )
        config.delete_pending_warning("evt-1")

        assert config.load_pending_warnings() == {}

    def test_delete_of_unknown_key_does_not_raise(self, temp_db):
        import config

        config.delete_pending_warning("never-existed")  # should not raise


# ── run_scheduler startup recovery ────────────────────────────────────────────


class TestRunSchedulerRestartRecovery:
    @pytest.mark.asyncio
    async def test_overdue_warning_fires_immediately_on_startup(self, temp_db):
        """A warning whose fire time already passed while the bot was down
        must fire on the next startup, not vanish silently."""
        import config
        from scheduler import run_scheduler, pending_warnings

        pending_warnings.clear()

        overdue = datetime.now(tz=ET) - timedelta(minutes=10)
        config.save_pending_warning("evt-overdue", GUILD_ID, overdue, _event_list())

        announcements = AsyncMock()
        announcements.send = AsyncMock()
        leadership = AsyncMock()
        leadership.send = AsyncMock()
        bot = MagicMock()
        bot.wait_until_ready = AsyncMock()
        bot.is_closed = MagicMock(return_value=True)  # skip the main loop body
        bot.get_channel = MagicMock(
            side_effect=lambda cid: {
                ANNOUNCEMENT_CHAN_ID: announcements,
                LEADERSHIP_CHAN_ID: leadership,
            }.get(cid)
        )

        with patch("scheduler.get_config", return_value=_make_cfg()):
            await run_scheduler(bot)

        announcements.send.assert_awaited_once()
        assert "evt-overdue" not in pending_warnings
        assert config.load_pending_warnings() == {}

    @pytest.mark.asyncio
    async def test_future_warning_is_restored_not_fired(self, temp_db):
        """A warning still ahead of `now` is folded back into the in-memory
        dict for the normal trigger loop — not fired early."""
        import config
        from scheduler import run_scheduler, pending_warnings

        pending_warnings.clear()

        upcoming = datetime.now(tz=ET) + timedelta(hours=1)
        config.save_pending_warning("evt-future", GUILD_ID, upcoming, _event_list())

        bot = MagicMock()
        bot.wait_until_ready = AsyncMock()
        bot.is_closed = MagicMock(return_value=True)
        bot.get_channel = MagicMock(return_value=None)

        with patch("scheduler.get_config", return_value=_make_cfg()):
            await run_scheduler(bot)

        assert "evt-future" in pending_warnings
        assert pending_warnings["evt-future"][0] == upcoming
        # Still persisted — only cleared once it actually fires.
        assert "evt-future" in config.load_pending_warnings()

    @pytest.mark.asyncio
    async def test_missing_guild_config_drops_stale_warning(self, temp_db):
        """If the guild's config is gone by the time we recover (guild
        left, config wiped), don't retry forever — drop the stale row
        instead of leaving it stuck."""
        import config
        from scheduler import run_scheduler, pending_warnings

        pending_warnings.clear()

        overdue = datetime.now(tz=ET) - timedelta(minutes=10)
        config.save_pending_warning("evt-orphan", 99999, overdue, _event_list())

        bot = MagicMock()
        bot.wait_until_ready = AsyncMock()
        bot.is_closed = MagicMock(return_value=True)

        with patch("scheduler.get_config", return_value=None):
            await run_scheduler(bot)

        assert "evt-orphan" not in pending_warnings
        assert config.load_pending_warnings() == {}

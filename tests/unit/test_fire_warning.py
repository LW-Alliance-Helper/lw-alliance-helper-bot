"""
Tests for `scheduler.fire_warning` — the 5-minutes-before-event auto-post
that goes to the announcements channel and stamps a confirmation in the
leadership channel. Audit gap #2.

Lifecycle the suite covers:
  * Build: ApprovalView's "Send As-Is" inserts an entry into
    `pending_warnings` 5 minutes before the first event time
    (already covered by test_approval_view.py — referenced here for
    context).
  * Fire: `fire_warning(bot, event_key, event_list, cfg)` posts to
    announcements + leadership and removes the entry from the dict.
  * Edge cases: missing announcements channel → no crash, no leadership
    stamp; missing leadership channel → announcement still posts; key
    cleared from `pending_warnings` even if leadership stamp fails.

A regression here is silent: members miss the 5-min warning and
nobody notices until someone asks why no one was online.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
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


def _make_bot(channels: dict):
    bot = MagicMock()
    bot.get_channel = MagicMock(side_effect=lambda cid: channels.get(cid))
    return bot


def _event_list_with_blurb(blurb: str | None = None):
    """A minimal event_list. The marauder key has a hardcoded warning
    in scheduler.build_warning_message; passing a custom key + blurb
    exercises the generic 'Re-use the configured announcement blurb'
    path."""
    return [
        {
            "key": "test_event",
            "name": "Test Event",
            "dt": datetime(2026, 5, 15, 22, 0, tzinfo=ET),
            "blurb": blurb or "Test Event at {time} ({server_time}).",
        }
    ]


# ── Happy path ───────────────────────────────────────────────────────────────


class TestFireWarningHappyPath:
    @pytest.mark.asyncio
    async def test_posts_warning_to_announcements_channel(self, temp_db):
        from scheduler import fire_warning, pending_warnings

        pending_warnings["evt-1"] = (
            datetime(2026, 5, 15, 21, 55, tzinfo=ET),
            _event_list_with_blurb(),
            GUILD_ID,
        )

        announcements = AsyncMock()
        announcements.send = AsyncMock()
        leadership = AsyncMock()
        leadership.send = AsyncMock()
        bot = _make_bot(
            {
                ANNOUNCEMENT_CHAN_ID: announcements,
                LEADERSHIP_CHAN_ID: leadership,
            }
        )

        await fire_warning(bot, "evt-1", _event_list_with_blurb(), cfg=_make_cfg())

        # Announcement got posted; the body contains the 5-minute swap
        # ('5 minutes' replaces {time}).
        assert announcements.send.await_count == 1
        body = announcements.send.await_args.args[0]
        assert "5 minutes" in body, f"Warning body should mention '5 minutes': {body!r}"

    @pytest.mark.asyncio
    async def test_stamps_leadership_with_auto_post_confirmation(self, temp_db):
        from scheduler import fire_warning, pending_warnings

        pending_warnings.clear()

        announcements = AsyncMock()
        announcements.send = AsyncMock()
        leadership = AsyncMock()
        leadership.send = AsyncMock()
        bot = _make_bot(
            {
                ANNOUNCEMENT_CHAN_ID: announcements,
                LEADERSHIP_CHAN_ID: leadership,
            }
        )

        await fire_warning(bot, "evt-2", _event_list_with_blurb(), cfg=_make_cfg())

        assert leadership.send.await_count == 1
        stamp = leadership.send.await_args.args[0]
        assert "5-minute warning" in stamp.lower()
        assert "auto-posted" in stamp.lower()

    @pytest.mark.asyncio
    async def test_pending_entry_is_cleared_after_fire(self, temp_db):
        """pending_warnings is checked every scheduler tick; if we don't
        clear the entry the warning will fire again on the next tick
        and members get spammed."""
        from scheduler import fire_warning, pending_warnings

        pending_warnings["evt-3"] = (
            datetime(2026, 5, 15, 21, 55, tzinfo=ET),
            _event_list_with_blurb(),
            GUILD_ID,
        )
        bot = _make_bot(
            {
                ANNOUNCEMENT_CHAN_ID: AsyncMock(send=AsyncMock()),
                LEADERSHIP_CHAN_ID: AsyncMock(send=AsyncMock()),
            }
        )

        await fire_warning(bot, "evt-3", _event_list_with_blurb(), cfg=_make_cfg())

        assert "evt-3" not in pending_warnings


# ── Missing-channel edge cases ───────────────────────────────────────────────


class TestFireWarningMissingChannels:
    @pytest.mark.asyncio
    async def test_returns_quietly_when_announcements_channel_missing(self, temp_db):
        """If the announcement channel was deleted between scheduling
        and firing, fire_warning bails out before posting anything —
        no crash, no half-fired warning."""
        from scheduler import fire_warning, pending_warnings

        pending_warnings.clear()

        leadership = AsyncMock()
        leadership.send = AsyncMock()
        bot = _make_bot({LEADERSHIP_CHAN_ID: leadership})  # announcements absent

        # Should not raise.
        await fire_warning(bot, "evt-x", _event_list_with_blurb(), cfg=_make_cfg())

        leadership.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_announcement_still_fires_when_leadership_channel_missing(self, temp_db):
        """If announcements is fine but the leadership stamp channel is
        gone, the public warning still posts. Members are the priority."""
        from scheduler import fire_warning, pending_warnings

        pending_warnings.clear()

        announcements = AsyncMock()
        announcements.send = AsyncMock()
        bot = _make_bot({ANNOUNCEMENT_CHAN_ID: announcements})  # leadership absent

        await fire_warning(bot, "evt-y", _event_list_with_blurb(), cfg=_make_cfg())

        announcements.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_op_when_cfg_is_none(self):
        """Defensive: a None cfg should not crash the loop."""
        from scheduler import fire_warning

        bot = _make_bot({})
        # Should not raise.
        await fire_warning(bot, "evt-z", _event_list_with_blurb(), cfg=None)


# ── Warning message content ──────────────────────────────────────────────────


class TestFireWarningMessageContent:
    """Verify the body uses the right per-event warning text rather
    than a generic placeholder."""

    @pytest.mark.asyncio
    async def test_body_uses_event_blurb_with_time_replaced(self, temp_db):
        from scheduler import fire_warning, pending_warnings

        pending_warnings.clear()

        announcements = AsyncMock()
        announcements.send = AsyncMock()
        bot = _make_bot(
            {
                ANNOUNCEMENT_CHAN_ID: announcements,
                LEADERSHIP_CHAN_ID: AsyncMock(send=AsyncMock()),
            }
        )

        # The warning re-uses the announcement blurb with {time} swapped
        # to "5 minutes" (see scheduler.build_warning_message).
        evt_list = _event_list_with_blurb(blurb="Cool Raid at {time} ({server_time}). Get ready!")

        await fire_warning(bot, "evt-content", evt_list, cfg=_make_cfg())

        body = announcements.send.await_args.args[0]
        assert "Cool Raid at 5 minutes" in body
        assert "Get ready!" in body
        # The placeholder must have been replaced — leftover braces
        # mean .format() didn't run.
        assert "{time}" not in body

    @pytest.mark.asyncio
    async def test_empty_event_list_falls_back_to_generic_warning(self, temp_db):
        """build_warning_message guards against an empty list; a
        generic 'Event starting in 5 minutes!' fires instead of a crash."""
        from scheduler import fire_warning, pending_warnings

        pending_warnings.clear()

        announcements = AsyncMock()
        announcements.send = AsyncMock()
        bot = _make_bot(
            {
                ANNOUNCEMENT_CHAN_ID: announcements,
                LEADERSHIP_CHAN_ID: AsyncMock(send=AsyncMock()),
            }
        )

        await fire_warning(bot, "evt-empty", [], cfg=_make_cfg())

        body = announcements.send.await_args.args[0]
        assert "5 minutes" in body

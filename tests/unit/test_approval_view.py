"""
Tests for `scheduler.ApprovalView` — the leadership-side approval UI for
event-announcement drafts. Two button branches cover the entire lifetime
of a draft:

  * **✅ Send As-Is** — posts the unchanged draft to the announcements
    channel, stamps a confirmation in the leadership channel, and
    schedules a 5-minute warning if the draft has events on it.

  * **✏️ Edit & Send** — drops the draft into the leadership channel as
    a copy-paste block, waits for the editor's revised message (5-min
    timeout), then posts a fresh ApprovalView seeded with the revised
    text. The user can re-approve or re-edit again.

The Edit & Send path is what these tests exercise — coverage gap from
the audit. send_as_is is also lightly covered here so we know the
shield-vs-event branching of `_post_to_announcements` doesn't drift.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# scheduler.py imports a bunch of bot-adjacent modules; needs a fake token.
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

ET = ZoneInfo("America/New_York")

GUILD_ID            = 12345
LEADERSHIP_CHAN_ID  = 1111
ANNOUNCEMENT_CHAN_ID = 2222


def _make_cfg():
    cfg = MagicMock()
    cfg.guild_id                 = GUILD_ID
    cfg.leadership_channel_id    = LEADERSHIP_CHAN_ID
    cfg.announcement_channel_id  = ANNOUNCEMENT_CHAN_ID
    return cfg


def _make_bot(channels: dict):
    """Build a mock bot whose `get_channel(id)` returns the matching mock
    channel for that id (or None if not in the dict)."""
    bot = MagicMock()
    bot.get_channel = MagicMock(side_effect=lambda cid: channels.get(cid))
    bot.wait_for    = AsyncMock()
    return bot


def _make_interaction(user_display: str = "Editor"):
    interaction = MagicMock()
    interaction.user             = MagicMock()
    interaction.user.id          = 9001
    interaction.user.display_name = user_display
    interaction.user.mention     = f"<@{interaction.user.id}>"
    interaction.message          = MagicMock()
    interaction.message.edit     = AsyncMock()
    interaction.response         = MagicMock()
    interaction.response.defer   = AsyncMock()
    interaction.followup         = MagicMock()
    interaction.followup.send    = AsyncMock()
    return interaction


def _make_event_list():
    """Two-event list — first event 5 minutes after a fixed `dt`."""
    return [
        {"key": "marauder", "name": "Marauder", "dt": datetime(2026, 5, 15, 22, 0, tzinfo=ET), "blurb": "Marauder at {time}."},
        {"key": "siege",    "name": "Siege",    "dt": datetime(2026, 5, 15, 22, 30, tzinfo=ET), "blurb": "Siege at {time}."},
    ]


# ── Send As-Is ────────────────────────────────────────────────────────────────

class TestSendAsIs:
    """Posts the draft to announcements, stamps confirmation in
    leadership, and schedules a 5-min warning when an event_list exists."""

    @pytest.mark.asyncio
    async def test_posts_draft_to_announcements_channel(self):
        from scheduler import ApprovalView, pending_warnings
        pending_warnings.clear()

        cfg            = _make_cfg()
        announcements  = AsyncMock()
        announcements.send = AsyncMock()
        leadership     = AsyncMock()
        leadership.send = AsyncMock()
        bot = _make_bot({
            ANNOUNCEMENT_CHAN_ID: announcements,
            LEADERSHIP_CHAN_ID:   leadership,
        })

        view = ApprovalView(
            bot=bot, draft_message="Hello @members!",
            event_key="event-123", event_list=_make_event_list(),
            is_shield=False, guild_id=GUILD_ID,
        )
        interaction = _make_interaction(user_display="LeadAlice")

        with patch("config.get_config", return_value=cfg):
            await view.send_as_is.callback(interaction)

        # Announcement got posted.
        announcements.send.assert_called_once_with("Hello @members!")
        # Leadership got an "Approved by ..." stamp.
        assert leadership.send.await_count == 1
        stamp_text = leadership.send.await_args.args[0]
        assert "Approved by LeadAlice" in stamp_text
        assert "Hello @members!" in stamp_text

    @pytest.mark.asyncio
    async def test_schedules_5min_warning_when_event_list_present(self):
        from scheduler import ApprovalView, pending_warnings
        pending_warnings.clear()

        cfg            = _make_cfg()
        announcements  = AsyncMock()
        announcements.send = AsyncMock()
        bot = _make_bot({ANNOUNCEMENT_CHAN_ID: announcements, LEADERSHIP_CHAN_ID: AsyncMock()})

        view = ApprovalView(
            bot=bot, draft_message="msg",
            event_key="event-123", event_list=_make_event_list(),
            is_shield=False, guild_id=GUILD_ID,
        )

        with patch("config.get_config", return_value=cfg):
            await view.send_as_is.callback(_make_interaction())

        assert "event-123" in pending_warnings, \
            f"5-min warning should be scheduled. pending={pending_warnings}"
        warn_dt, stored_list, stored_gid = pending_warnings["event-123"]
        # 5 minutes before the first event (22:00 → 21:55)
        assert warn_dt.hour == 21 and warn_dt.minute == 55
        assert stored_gid == GUILD_ID

    @pytest.mark.asyncio
    async def test_shield_reminder_does_not_schedule_warning(self):
        """Friday shield reminders aren't followed by an event, so no
        5-minute warning should ever be queued for them."""
        from scheduler import ApprovalView, pending_warnings
        pending_warnings.clear()

        cfg = _make_cfg()
        announcements = AsyncMock(); announcements.send = AsyncMock()
        bot = _make_bot({ANNOUNCEMENT_CHAN_ID: announcements, LEADERSHIP_CHAN_ID: AsyncMock()})

        view = ApprovalView(
            bot=bot, draft_message="Shield up!",
            event_key="shield-123", event_list=[],
            is_shield=True, guild_id=GUILD_ID,
        )

        with patch("config.get_config", return_value=cfg):
            await view.send_as_is.callback(_make_interaction())

        assert "shield-123" not in pending_warnings


# ── Edit & Send ───────────────────────────────────────────────────────────────

class TestEditAndSend:
    """Audit gap: leadership's revisions go through Edit & Send.
    A regression here silently drops their edits."""

    @pytest.mark.asyncio
    async def test_revised_text_creates_new_approval_view(self):
        """Happy path: editor submits revised text → bot posts a new
        ApprovalView seeded with that text."""
        from scheduler import ApprovalView

        cfg          = _make_cfg()
        leadership   = AsyncMock()
        leadership.send = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        bot = _make_bot({LEADERSHIP_CHAN_ID: leadership})

        # The user's revised message arriving in the leadership channel.
        revised_msg              = MagicMock()
        revised_msg.author       = MagicMock()
        revised_msg.author.id    = 9001
        revised_msg.channel      = MagicMock()
        revised_msg.channel.id   = LEADERSHIP_CHAN_ID
        revised_msg.content      = "Revised announcement text"
        revised_msg.delete       = AsyncMock()
        bot.wait_for = AsyncMock(return_value=revised_msg)

        view = ApprovalView(
            bot=bot, draft_message="Original draft",
            event_key="event-123", event_list=_make_event_list(),
            is_shield=False, guild_id=GUILD_ID,
        )
        interaction = _make_interaction(user_display="LeadBob")

        with patch("config.get_config", return_value=cfg):
            await view.edit_and_send.callback(interaction)

        # Leadership channel saw two posts: the prompt + the revised draft.
        # (The original prompt is sent first, the revised draft second.)
        assert leadership.send.await_count == 2

        # The second send is the revised draft, attached to a new ApprovalView.
        revised_post = leadership.send.await_args_list[1]
        body = revised_post.args[0]
        assert "Revised announcement text" in body
        assert "LeadBob" in body
        new_view = revised_post.kwargs["view"]
        from scheduler import ApprovalView as AV
        assert isinstance(new_view, AV)
        assert new_view.draft_message == "Revised announcement text"

    @pytest.mark.asyncio
    async def test_revised_view_carries_event_list_and_guild(self):
        """The new ApprovalView must inherit event_list, event_key,
        is_shield, and guild_id — otherwise the eventual Send As-Is
        won't be able to schedule the 5-minute warning correctly."""
        from scheduler import ApprovalView

        cfg          = _make_cfg()
        leadership   = AsyncMock(); leadership.send = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        bot          = _make_bot({LEADERSHIP_CHAN_ID: leadership})

        revised_msg              = MagicMock()
        revised_msg.author       = MagicMock(); revised_msg.author.id = 9001
        revised_msg.channel      = MagicMock(); revised_msg.channel.id = LEADERSHIP_CHAN_ID
        revised_msg.content      = "edit text"
        revised_msg.delete       = AsyncMock()
        bot.wait_for = AsyncMock(return_value=revised_msg)

        original_event_list = _make_event_list()
        view = ApprovalView(
            bot=bot, draft_message="orig",
            event_key="event-key-77", event_list=original_event_list,
            is_shield=False, guild_id=GUILD_ID,
        )

        with patch("config.get_config", return_value=cfg):
            await view.edit_and_send.callback(_make_interaction())

        new_view = leadership.send.await_args_list[1].kwargs["view"]
        assert new_view.event_key  == "event-key-77"
        assert new_view.event_list == original_event_list
        assert new_view.is_shield  is False
        assert new_view.guild_id   == GUILD_ID

    @pytest.mark.asyncio
    async def test_timeout_posts_friendly_message_and_skips_resend(self):
        """5-minute timeout on the editor's reply → bot posts a
        timeout note. No new ApprovalView is created."""
        from scheduler import ApprovalView

        cfg          = _make_cfg()
        leadership   = AsyncMock(); leadership.send = AsyncMock(return_value=MagicMock(delete=AsyncMock()))
        bot          = _make_bot({LEADERSHIP_CHAN_ID: leadership})

        bot.wait_for = AsyncMock(side_effect=asyncio.TimeoutError())

        view = ApprovalView(
            bot=bot, draft_message="orig",
            event_key="event-key", event_list=_make_event_list(),
            is_shield=False, guild_id=GUILD_ID,
        )
        interaction = _make_interaction(user_display="LeadDana")

        with patch("config.get_config", return_value=cfg):
            await view.edit_and_send.callback(interaction)

        # Two sends: the prompt + the timeout note. No third send (no resend).
        assert leadership.send.await_count == 2
        timeout_text = leadership.send.await_args_list[1].args[0]
        assert "timed out" in timeout_text.lower()
        assert "LeadDana" in timeout_text or "@9001" in timeout_text

        # Confirm no ApprovalView was attached to the second send (the
        # timeout note shouldn't have a view).
        timeout_call = leadership.send.await_args_list[1]
        assert "view" not in timeout_call.kwargs

    @pytest.mark.asyncio
    async def test_no_op_when_leadership_channel_is_missing(self):
        """If get_channel returns None for leadership, the handler
        should bail without raising."""
        from scheduler import ApprovalView

        cfg = _make_cfg()
        bot = _make_bot({})  # nothing wired up

        view = ApprovalView(
            bot=bot, draft_message="orig",
            event_key="event-key", event_list=_make_event_list(),
            is_shield=False, guild_id=GUILD_ID,
        )

        with patch("config.get_config", return_value=cfg):
            # Should not raise.
            await view.edit_and_send.callback(_make_interaction())

        # bot.wait_for should NOT have been called (we bailed before that).
        bot.wait_for.assert_not_called()

    @pytest.mark.asyncio
    async def test_revised_view_message_captured_for_timeout(self):
        """The new ApprovalView posted from Edit & Send must have its
        `message` attribute set to the sent draft. Without this, when
        the new view eventually times out, on_timeout has nothing to
        edit and the buttons stay live but unresponsive."""
        from scheduler import ApprovalView

        cfg          = _make_cfg()
        revised_sent = MagicMock(name="revised_msg")
        revised_sent.delete = AsyncMock()
        leadership   = AsyncMock()
        # First send (the prompt) returns a deletable; second send (the
        # revised draft + new view) returns revised_sent. Use a list-style
        # side effect so each await returns the next value.
        leadership.send = AsyncMock(side_effect=[
            MagicMock(delete=AsyncMock()),
            revised_sent,
        ])
        bot = _make_bot({LEADERSHIP_CHAN_ID: leadership})

        revised_msg              = MagicMock()
        revised_msg.author       = MagicMock(); revised_msg.author.id = 9001
        revised_msg.channel      = MagicMock(); revised_msg.channel.id = LEADERSHIP_CHAN_ID
        revised_msg.content      = "edited body"
        revised_msg.delete       = AsyncMock()
        bot.wait_for = AsyncMock(return_value=revised_msg)

        view = ApprovalView(
            bot=bot, draft_message="orig",
            event_key="event-key", event_list=_make_event_list(),
            is_shield=False, guild_id=GUILD_ID,
        )

        with patch("config.get_config", return_value=cfg):
            await view.edit_and_send.callback(_make_interaction())

        new_view = leadership.send.await_args_list[1].kwargs["view"]
        assert new_view.message is revised_sent, \
            "Revised ApprovalView must carry the sent message ref so on_timeout can edit it"


# ── on_timeout: dead buttons must be cleaned up ──────────────────────────────

class TestApprovalViewOnTimeout:
    """When the 1-hour ApprovalView timeout expires, the buttons must be
    physically removed from the message and a 'use /events' notice
    appended. Otherwise leadership clicks expired buttons and gets the
    unhelpful 'Interaction failed' toast."""

    @pytest.mark.asyncio
    async def test_strips_buttons_and_posts_use_events_notice(self):
        from scheduler import ApprovalView

        view = ApprovalView(
            bot=MagicMock(), draft_message="m",
            event_key="k", event_list=[], is_shield=False, guild_id=GUILD_ID,
        )
        view.message = MagicMock()
        view.message.content = "📣 Announcement draft body"
        view.message.edit    = AsyncMock()

        await view.on_timeout()

        view.message.edit.assert_awaited_once()
        kwargs = view.message.edit.await_args.kwargs
        assert kwargs["view"] is None
        assert "/events" in kwargs["content"]
        assert "timed out" in kwargs["content"].lower()
        # Original content is preserved.
        assert "Announcement draft body" in kwargs["content"]

    @pytest.mark.asyncio
    async def test_no_op_when_message_never_captured(self):
        """A view with no message ref (constructed in a test, or send
        failed) must not raise on timeout."""
        from scheduler import ApprovalView

        view = ApprovalView(
            bot=MagicMock(), draft_message="m",
            event_key="k", event_list=[], is_shield=False, guild_id=GUILD_ID,
        )
        # view.message left as None by default.
        await view.on_timeout()  # should not raise


class TestEventEditorOnTimeout:
    """Same contract for the event-editor view. The editor sits in the
    leadership channel for an hour after the daily draft. If buttons
    aren't stripped, leadership comes back next day and clicks dead
    buttons."""

    @pytest.mark.asyncio
    async def test_strips_buttons_and_posts_use_events_notice(self):
        from datetime import date as date_cls
        from scheduler import EventEditorView

        view = EventEditorView(
            bot=MagicMock(), event_list=[], event_key="ek",
            run_date=date_cls(2026, 5, 15), guild_id=GUILD_ID,
        )
        view.message = MagicMock()
        view.message.content = "📣 **Event Editor** body"
        view.message.edit    = AsyncMock()

        await view.on_timeout()

        view.message.edit.assert_awaited_once()
        kwargs = view.message.edit.await_args.kwargs
        assert kwargs["view"] is None
        assert "/events" in kwargs["content"]
        assert "Event Editor" in kwargs["content"]


# ── post_editor / Build Announcement: must wire view.message ─────────────────

class TestSentMessageWiring:
    """post_editor and Build Announcement both send a View-bearing
    message. They must capture the returned message into view.message
    so the eventual on_timeout has something to edit."""

    @pytest.mark.asyncio
    async def test_post_editor_assigns_view_message(self):
        from datetime import date as date_cls
        from scheduler import post_editor

        sent = MagicMock(name="sent")
        channel = AsyncMock(); channel.send = AsyncMock(return_value=sent)
        bot = _make_bot({LEADERSHIP_CHAN_ID: channel})
        cfg = _make_cfg()

        await post_editor(
            bot, event_list=[], event_key="ek",
            run_date=date_cls(2026, 5, 15),
            cfg=cfg, draft_channel_id=LEADERSHIP_CHAN_ID,
            announcement_channel_id=ANNOUNCEMENT_CHAN_ID,
            five_min_warning=False,
        )

        # The view passed to channel.send must now carry message=sent.
        kwargs = channel.send.await_args.kwargs
        view   = kwargs["view"]
        assert view.message is sent

    @pytest.mark.asyncio
    async def test_build_announcement_assigns_approval_view_message(self):
        """When EventEditorView's Build Announcement button posts the
        ApprovalView, the new view's `message` must point at the sent
        draft so its 1-hour timeout can clean itself up."""
        from datetime import date as date_cls
        from scheduler import EventEditorView, ApprovalView

        sent = MagicMock(name="approval_msg")
        channel = AsyncMock(); channel.send = AsyncMock(return_value=sent)
        bot = _make_bot({LEADERSHIP_CHAN_ID: channel})
        cfg = _make_cfg()
        cfg.role_mention = "@everyone"

        view = EventEditorView(
            bot=bot, event_list=_make_event_list(), event_key="ek",
            run_date=date_cls(2026, 5, 15), guild_id=GUILD_ID,
        )
        # Need a message to satisfy the existing "disable buttons" edit.
        editor_msg = MagicMock(); editor_msg.edit = AsyncMock()
        interaction = _make_interaction()
        interaction.message = editor_msg

        with patch("config.get_config", return_value=cfg):
            await view.build_announcement_btn.callback(interaction)

        # ApprovalView was posted to the leadership channel.
        approval_view = channel.send.await_args.kwargs["view"]
        assert isinstance(approval_view, ApprovalView)
        assert approval_view.message is sent

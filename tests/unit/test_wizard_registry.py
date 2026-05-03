"""
Tests for `wizard_registry.wait_view_or_cancel` — the helper that races
a discord.ui.View's wait() against a wizard's cancel event so /cancel
actually stops view-based wizard steps instead of letting them sit
until the view's own timeout (which then posts a misleading "Timed
out" message).
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import AsyncMock, MagicMock

from wizard_registry import (
    register, unregister, cancel_user, wait_view_or_cancel,
    expire_view_message,
)


class _FakeView:
    """Minimal stand-in for discord.ui.View that exposes the same wait()
    semantics needed by wait_view_or_cancel: an asyncio-friendly wait()
    that completes when stop() is called or `confirm()` is awaited."""

    def __init__(self):
        self._stop_event = asyncio.Event()
        self.confirmed   = False
        # Note: cancelled is set by wait_view_or_cancel itself.

    async def wait(self):
        await self._stop_event.wait()

    def stop(self):
        self._stop_event.set()

    def confirm(self):
        """Simulate the user clicking through the view normally."""
        self.confirmed = True
        self._stop_event.set()


# ── wait_view_or_cancel ───────────────────────────────────────────────────────

class TestWaitViewOrCancel:

    @pytest.mark.asyncio
    async def test_returns_normally_when_view_confirmed(self):
        """View confirmed before cancel → cancelled stays False."""
        view  = _FakeView()
        event = asyncio.Event()
        # Confirm the view after a short delay, simulating the user
        # clicking a button.
        async def _confirm():
            await asyncio.sleep(0.01)
            view.confirm()
        asyncio.create_task(_confirm())
        await wait_view_or_cancel(view, event)
        assert view.confirmed is True
        assert view.cancelled is False

    @pytest.mark.asyncio
    async def test_cancel_event_sets_cancelled_and_stops_view(self):
        """If cancel fires first, view.cancelled becomes True AND the
        view is stopped so its own wait() unblocks."""
        view  = _FakeView()
        event = asyncio.Event()
        async def _cancel():
            await asyncio.sleep(0.01)
            event.set()
        asyncio.create_task(_cancel())
        await wait_view_or_cancel(view, event)
        assert view.cancelled is True
        assert view.confirmed is False
        # Internal _stop_event should be set (we called view.stop()).
        assert view._stop_event.is_set()

    @pytest.mark.asyncio
    async def test_no_cancel_event_falls_back_to_plain_wait(self):
        """Defensive default: passing None should not crash; cancelled
        stays False and the view runs to completion as if wait() were
        called bare."""
        view = _FakeView()
        async def _confirm():
            await asyncio.sleep(0.01)
            view.confirm()
        asyncio.create_task(_confirm())
        await wait_view_or_cancel(view, None)
        assert view.confirmed is True
        assert view.cancelled is False

    @pytest.mark.asyncio
    async def test_cancelled_attribute_initialised_even_on_normal_path(self):
        """Wizards check `if view.cancelled: return`; the attribute
        must exist after every call, not just the cancel path."""
        view  = _FakeView()
        event = asyncio.Event()
        async def _confirm():
            await asyncio.sleep(0.01)
            view.confirm()
        asyncio.create_task(_confirm())
        await wait_view_or_cancel(view, event)
        # The wizard is allowed to read view.cancelled — it must exist.
        assert hasattr(view, "cancelled")
        assert view.cancelled is False


# ── End-to-end with cancel_user ───────────────────────────────────────────────

class TestCancelUserDrivesViewWait:
    """Verify the full flow: register cancel event → start view wait →
    cancel_user fires → view.cancelled is True."""

    @pytest.mark.asyncio
    async def test_cancel_user_triggers_view_cancel(self):
        user_id = 12345
        cancel_event = register(user_id)
        try:
            view = _FakeView()
            async def _cancel_after_delay():
                await asyncio.sleep(0.01)
                cancel_user(user_id)
            asyncio.create_task(_cancel_after_delay())
            await wait_view_or_cancel(view, cancel_event)
            assert view.cancelled is True
        finally:
            # cancel_user already cleared the registry, so unregister is
            # a no-op here. Safe to call regardless.
            unregister(user_id, cancel_event)


# ── expire_view_message ───────────────────────────────────────────────────────

class TestExpireViewMessage:
    """When a View's timeout fires, expire_view_message strips the
    buttons from the original Discord message and appends a notice
    telling the user how to re-open the flow. Without this helper the
    expired message keeps rendering active-looking buttons that fail
    silently with 'Interaction failed' on click."""

    @pytest.mark.asyncio
    async def test_strips_view_and_appends_notice_with_command(self):
        msg = MagicMock()
        msg.content = "📣 Original draft body"
        msg.edit    = AsyncMock()

        await expire_view_message(msg, command_hint="/events")

        msg.edit.assert_awaited_once()
        kwargs = msg.edit.await_args.kwargs
        # View is removed entirely — buttons gone.
        assert kwargs["view"] is None
        # Original content preserved + timeout notice appended with command.
        assert kwargs["content"].startswith("📣 Original draft body")
        assert "timed out" in kwargs["content"].lower()
        assert "/events" in kwargs["content"]

    @pytest.mark.asyncio
    async def test_omits_command_suffix_when_hint_empty(self):
        msg = MagicMock()
        msg.content = "draft"
        msg.edit    = AsyncMock()

        await expire_view_message(msg)  # no command_hint

        body = msg.edit.await_args.kwargs["content"]
        assert "timed out" in body.lower()
        assert "re-initiate" not in body.lower()

    @pytest.mark.asyncio
    async def test_idempotent_when_notice_already_present(self):
        """Running twice on the same message must not double-append the
        notice. Defends against on_timeout being called twice (e.g. an
        explicit stop + the timer firing concurrently)."""
        msg = MagicMock()
        msg.edit = AsyncMock()
        # Simulate first run already happened.
        msg.content = "draft\n\n⏰ *The actions for this have timed out. Use `/events` to re-initiate.*"

        await expire_view_message(msg, command_hint="/events")

        msg.edit.assert_not_called()

    @pytest.mark.asyncio
    async def test_none_message_is_a_noop(self):
        """A View constructed in tests (or never sent) carries
        message=None. The helper must tolerate this without raising."""
        await expire_view_message(None, command_hint="/events")  # no-op

    @pytest.mark.asyncio
    async def test_swallows_edit_errors(self):
        """If the original message was deleted between draft post and
        timeout, message.edit raises NotFound. The helper must swallow
        it so the bot's task loop doesn't crash."""
        msg = MagicMock()
        msg.content = "draft"
        msg.edit    = AsyncMock(side_effect=RuntimeError("message gone"))

        # Should not raise.
        await expire_view_message(msg, command_hint="/events")
        msg.edit.assert_awaited_once()

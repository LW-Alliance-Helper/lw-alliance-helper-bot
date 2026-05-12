"""
Tests for `setup_cog.ask_proceed_with_existing_config` — the shared
helper that opens every `/setup_*` wizard with a summary of saved
config + Edit / No-changes buttons.

Extracted from `run_setup`'s inline summary block in #94 so the rest
of the per-wizard sub-tasks of #80 can reuse it. Three outcome paths
the wizards depend on:

  * `True`  — Edit clicked; caller proceeds into the step-by-step flow
  * `False` — No-changes clicked; helper already posted the
    confirmation; caller returns
  * `None`  — `/cancel` or timeout; caller returns silently
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


def _capture_view_and_embed(channel_mock):
    """Pull (view, embed) out of the most recent channel.send call."""
    last = channel_mock.send.call_args
    return last.kwargs.get("view"), last.kwargs.get("embed")


def _make_channel():
    channel = MagicMock()
    channel.send = AsyncMock()
    return channel


# ── Edit path ────────────────────────────────────────────────────────────────

class TestEditPath:

    @pytest.mark.asyncio
    async def test_edit_returns_true(self):
        from setup_cog import ask_proceed_with_existing_config
        channel = _make_channel()

        async def _click_edit(view, ev):
            # Simulate leadership clicking Edit before the view's wait()
            # returns. The helper just inspects view.proceed afterward.
            view.proceed = True
            view.cancelled = False

        with patch("setup_cog.wait_view_or_cancel", _click_edit):
            result = await ask_proceed_with_existing_config(
                channel,
                title="⚙️ Current setup",
                description="desc",
                fields=[("Role", "Member"), ("Channel", "#leadership")],
                cancel_event=None,
            )
        assert result is True
        # No second `channel.send` for the no-changes message.
        assert channel.send.call_count == 1


# ── No-changes path ──────────────────────────────────────────────────────────

class TestNoChangesPath:

    @pytest.mark.asyncio
    async def test_no_changes_returns_false_and_posts_message(self):
        from setup_cog import ask_proceed_with_existing_config
        channel = _make_channel()

        async def _click_no(view, ev):
            view.proceed = False
            view.cancelled = False

        with patch("setup_cog.wait_view_or_cancel", _click_no):
            result = await ask_proceed_with_existing_config(
                channel,
                title="⚙️ Current setup",
                description="desc",
                fields=[("Role", "Member")],
                cancel_event=None,
            )
        assert result is False
        # First send = the summary embed; second = no-changes confirmation.
        assert channel.send.call_count == 2
        confirm_call = channel.send.call_args_list[1]
        confirm_text = confirm_call.args[0] if confirm_call.args else ""
        assert "No changes" in confirm_text

    @pytest.mark.asyncio
    async def test_no_changes_message_is_customizable(self):
        from setup_cog import ask_proceed_with_existing_config
        channel = _make_channel()

        async def _click_no(view, ev):
            view.proceed = False
            view.cancelled = False

        custom = "✅ Survey kept as is."
        with patch("setup_cog.wait_view_or_cancel", _click_no):
            await ask_proceed_with_existing_config(
                channel,
                title="t", description="d",
                fields=[("F", "V")],
                cancel_event=None,
                no_changes_message=custom,
            )
        assert channel.send.call_args_list[1].args[0] == custom


# ── Cancel / timeout path ────────────────────────────────────────────────────

class TestCancelAndTimeoutReturnNone:
    """Both /cancel and timeout map to None so the caller's single
    `if X is None: return` check covers both."""

    @pytest.mark.asyncio
    async def test_cancel_returns_none(self):
        from setup_cog import ask_proceed_with_existing_config
        channel = _make_channel()

        async def _cancel(view, ev):
            view.cancelled = True

        with patch("setup_cog.wait_view_or_cancel", _cancel):
            result = await ask_proceed_with_existing_config(
                channel,
                title="t", description="d",
                fields=[("F", "V")],
                cancel_event=None,
            )
        assert result is None
        # Helper does not post a "cancelled" message — /cancel itself acks.
        assert channel.send.call_count == 1

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        from setup_cog import ask_proceed_with_existing_config
        channel = _make_channel()

        async def _timeout(view, ev):
            # Neither cancelled nor proceed-confirmed.
            view.cancelled = False
            # view.proceed stays None.

        with patch("setup_cog.wait_view_or_cancel", _timeout):
            result = await ask_proceed_with_existing_config(
                channel,
                title="t", description="d",
                fields=[("F", "V")],
                cancel_event=None,
            )
        assert result is None


# ── Embed rendering ──────────────────────────────────────────────────────────

class TestEmbedRendering:

    @pytest.mark.asyncio
    async def test_fields_render_as_embed_fields(self):
        from setup_cog import ask_proceed_with_existing_config
        channel = _make_channel()

        async def _click_edit(view, ev):
            view.proceed = True
            view.cancelled = False

        with patch("setup_cog.wait_view_or_cancel", _click_edit):
            await ask_proceed_with_existing_config(
                channel,
                title="⚙️ Current Train",
                description="Edit?",
                fields=[
                    ("Reminder Channel", "<#123>"),
                    ("Reminder Time",    "9:00am"),
                    ("Blurbs",           "Enabled"),
                ],
                cancel_event=None,
            )
        view, embed = _capture_view_and_embed(channel)
        assert embed is not None
        assert embed.title == "⚙️ Current Train"
        # All three fields appear.
        names  = [f.name  for f in embed.fields]
        values = [f.value for f in embed.fields]
        assert names == ["Reminder Channel", "Reminder Time", "Blurbs"]
        assert values == ["<#123>", "9:00am", "Enabled"]

    @pytest.mark.asyncio
    async def test_view_has_edit_and_no_changes_buttons(self):
        from setup_cog import ask_proceed_with_existing_config
        channel = _make_channel()

        async def _bail(view, ev):
            view.cancelled = True

        with patch("setup_cog.wait_view_or_cancel", _bail):
            await ask_proceed_with_existing_config(
                channel,
                title="t", description="d",
                fields=[("F", "V")],
                cancel_event=None,
            )
        view, _ = _capture_view_and_embed(channel)
        labels = [c.label for c in view.children]
        assert any("Edit" in lbl for lbl in labels)
        assert any("No changes" in lbl for lbl in labels)

"""
Tests for `setup_cog.ask_disable_with_clear` — friendlier response when
leadership picks No on an enable-toggle wizard step.

Two shapes the helper produces:

  * `had_prior_config=False` — first-time disable, nothing saved to
    lose. Helper posts a bare "✅ disabled" message, no button.
  * `had_prior_config=True`  — saved config is preserved, helper tells
    leadership how to restore it AND offers a Clear button that wipes
    the row via the caller's `clear_fn`.

The Clear button must handle both sync and async `clear_fn`, and must
not crash if `clear_fn` raises (just surface the error in-place).
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


def _make_channel():
    channel = MagicMock()
    channel.send = AsyncMock(return_value=MagicMock())
    return channel


# ── had_prior_config=False ───────────────────────────────────────────────────


class TestFirstTimeDisable:
    @pytest.mark.asyncio
    async def test_no_button_when_no_prior_config(self):
        from setup_cog import ask_disable_with_clear

        channel = _make_channel()
        clear_called = MagicMock()

        await ask_disable_with_clear(
            channel,
            feature_label="Shiny Tasks announcement",
            setup_command="setup_shiny_tasks",
            had_prior_config=False,
            clear_fn=clear_called,
            cancel_event=None,
        )
        # Single send, no view.
        assert channel.send.call_count == 1
        call = channel.send.call_args
        assert call.args[0] == "✅ Shiny Tasks announcement disabled."
        assert "view" not in call.kwargs
        # clear_fn was NOT invoked (the button doesn't render).
        clear_called.assert_not_called()


# ── had_prior_config=True ────────────────────────────────────────────────────


class TestHadPriorConfig:
    @pytest.mark.asyncio
    async def test_message_mentions_restore_and_offers_clear_button(self):
        from setup_cog import ask_disable_with_clear

        channel = _make_channel()

        async def _bail(view, ev):
            view.cancelled = True

        with patch("setup_cog.wait_view_or_cancel", _bail):
            await ask_disable_with_clear(
                channel,
                feature_label="Shiny Tasks announcement",
                setup_command="setup_shiny_tasks",
                had_prior_config=True,
                clear_fn=MagicMock(),
                cancel_event=None,
            )
        call = channel.send.call_args
        body = call.args[0]
        assert "disabled" in body.lower()
        assert "saved" in body.lower()
        assert "/setup_shiny_tasks" in body
        # View was attached and has one Clear button.
        view = call.kwargs["view"]
        labels = [c.label for c in view.children]
        assert any("Clear my saved configuration" in lbl for lbl in labels)

    @pytest.mark.asyncio
    async def test_clicking_clear_invokes_sync_clear_fn(self):
        from setup_cog import ask_disable_with_clear

        channel = _make_channel()
        clear_called = MagicMock()

        async def _bail(view, ev):
            view.cancelled = True

        with patch("setup_cog.wait_view_or_cancel", _bail):
            await ask_disable_with_clear(
                channel,
                feature_label="Shiny Tasks",
                setup_command="setup_shiny_tasks",
                had_prior_config=True,
                clear_fn=clear_called,
                cancel_event=None,
            )
        view = channel.send.call_args.kwargs["view"]
        clear_btn = next(
            c for c in view.children if "Clear my saved configuration" in (c.label or "")
        )
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await clear_btn.callback(inter)

        clear_called.assert_called_once()
        assert view.cleared is True
        # View self-stopped.
        assert view.is_finished()

    @pytest.mark.asyncio
    async def test_clicking_clear_invokes_async_clear_fn(self):
        from setup_cog import ask_disable_with_clear

        channel = _make_channel()
        clear_called = AsyncMock()

        async def _bail(view, ev):
            view.cancelled = True

        with patch("setup_cog.wait_view_or_cancel", _bail):
            await ask_disable_with_clear(
                channel,
                feature_label="Shiny Tasks",
                setup_command="setup_shiny_tasks",
                had_prior_config=True,
                clear_fn=clear_called,
                cancel_event=None,
            )
        view = channel.send.call_args.kwargs["view"]
        clear_btn = next(
            c for c in view.children if "Clear my saved configuration" in (c.label or "")
        )
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await clear_btn.callback(inter)

        clear_called.assert_awaited_once()
        assert view.cleared is True

    @pytest.mark.asyncio
    async def test_clear_fn_exception_surfaces_in_place(self):
        """If clear_fn raises (DB locked, etc.) the helper should not
        crash — it should surface the error in the original message and
        leave the view stopped so the dead button doesn't linger."""
        from setup_cog import ask_disable_with_clear

        channel = _make_channel()

        def _boom():
            raise RuntimeError("DB locked")

        async def _bail(view, ev):
            view.cancelled = True

        with patch("setup_cog.wait_view_or_cancel", _bail):
            await ask_disable_with_clear(
                channel,
                feature_label="Shiny Tasks",
                setup_command="setup_shiny_tasks",
                had_prior_config=True,
                clear_fn=_boom,
                cancel_event=None,
            )
        view = channel.send.call_args.kwargs["view"]
        clear_btn = next(
            c for c in view.children if "Clear my saved configuration" in (c.label or "")
        )
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        # Must NOT raise.
        await clear_btn.callback(inter)

        # cleared stays False since the wipe failed.
        assert view.cleared is False
        # View stopped so the button can't be clicked again into the same error.
        assert view.is_finished()
        # The edit was called with content that mentions the error.
        edit_kwargs = inter.response.edit_message.call_args.kwargs
        assert "DB locked" in edit_kwargs.get("content", "")

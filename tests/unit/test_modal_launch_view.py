"""
Tests for `setup_cog.ModalLaunchView` — the button-that-opens-a-modal
view used by every free-text wizard step (Sheet ID, etc.). #106 added
an optional Keep-current button alongside the existing Enter Value so
leadership doesn't have to re-paste a 44-character Sheet ID on every
re-run.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


def _labels(view) -> list[str]:
    return [c.label for c in view.children if getattr(c, "label", None)]


def _make_modal():
    """Return a real TextInputModal — Discord doesn't care if we never
    submit it; we just need somewhere for `modal.value` to live."""
    from setup_cog import TextInputModal
    return TextInputModal("title", "label", placeholder="ph")


# ── Default (no current_value) path ──────────────────────────────────────────

class TestDefaultRendering:
    """No `current_value` → the view renders just the Enter Value
    button, same as the pre-#106 behavior."""

    def test_no_current_value_renders_enter_only(self):
        from setup_cog import ModalLaunchView
        view = ModalLaunchView(_make_modal())
        labels = _labels(view)
        assert any("Enter Value" in lbl for lbl in labels)
        assert not any("Keep current" in lbl for lbl in labels)

    def test_empty_current_value_does_not_render_keep(self):
        """Wizards may pass `current_value=cfg.spreadsheet_id` where
        the field is empty on a not-yet-configured guild. Empty string
        should be treated the same as None — no keep button."""
        from setup_cog import ModalLaunchView
        view = ModalLaunchView(_make_modal(), current_value="")
        assert not any("Keep current" in lbl for lbl in _labels(view))


# ── Keep-current path (#106) ─────────────────────────────────────────────────

class TestKeepCurrent:

    def test_current_value_renders_keep_button(self):
        from setup_cog import ModalLaunchView
        view = ModalLaunchView(_make_modal(), current_value="some-saved-id")
        labels = _labels(view)
        assert any("Keep current" in lbl and "some-saved-id" in lbl
                   for lbl in labels)
        # Enter Value still present so leadership can type a new value.
        assert any("Enter Value" in lbl for lbl in labels)

    def test_current_display_overrides_label_for_long_values(self):
        """44-char Sheet IDs blow past Discord's 80-char button label
        cap once "✅ Keep current: " is prepended. Wizards truncate
        the display via the `current_display` kwarg."""
        from setup_cog import ModalLaunchView
        long_id = "1G21IGB5wyh79NpKEdgJabcdefghijklmnopqrstuv"
        view = ModalLaunchView(
            _make_modal(),
            current_value=long_id,
            current_display=f"{long_id[:25]}…",
        )
        keep_btn = next(c for c in view.children
                        if isinstance(c, discord.ui.Button)
                        and "Keep current" in (c.label or ""))
        # Truncated display in the label, not the full id.
        assert long_id not in keep_btn.label
        assert long_id[:25] in keep_btn.label
        assert len(keep_btn.label) <= 80

    @pytest.mark.asyncio
    async def test_clicking_keep_sets_modal_value_and_stops(self):
        """The whole point: callers read `modal.value` after
        `view.wait()`. Keep-current sets `modal.value` directly so the
        caller's read works without knowing the modal was skipped."""
        from setup_cog import ModalLaunchView
        modal = _make_modal()
        view  = ModalLaunchView(modal, current_value="saved-sheet-id")
        keep_btn = next(c for c in view.children
                        if isinstance(c, discord.ui.Button)
                        and "Keep current" in (c.label or ""))
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await keep_btn.callback(inter)
        assert modal.value     == "saved-sheet-id"
        assert view.confirmed  is True
        assert view.is_finished()

"""
Tests for `setup_cog.TimezoneSelectView` — the timezone picker used in
`/setup` Step 4. Added in #106 as part of the Keep-current polish so
leadership doesn't have to re-pick their timezone on every wizard
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


# ── Default (no current) path ────────────────────────────────────────────────


class TestDefaultRendering:
    """When `current` isn't passed, the view should look exactly like
    the pre-#106 version: just the timezone select on row 0."""

    def test_no_current_renders_select_only(self):
        from setup_cog import TimezoneSelectView

        view = TimezoneSelectView()
        labels = _labels(view)
        assert not any("Keep current" in lbl for lbl in labels)
        # Single select.
        selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
        assert len(selects) == 1
        # Select sits on row 0 (no keep button to displace it).
        assert selects[0].row == 0

    def test_unknown_timezone_does_not_render_keep(self):
        """If `current` doesn't match a known timezone (e.g. stale value
        from a different bot version), don't render a misleading
        Keep-current button — fall back to the plain picker."""
        from setup_cog import TimezoneSelectView

        view = TimezoneSelectView(current="Mars/Olympus_Mons")
        labels = _labels(view)
        assert not any("Keep current" in lbl for lbl in labels)


# ── Keep-current path (#106) ─────────────────────────────────────────────────


class TestKeepCurrent:
    def test_known_current_renders_keep_button(self):
        from setup_cog import TimezoneSelectView, TIMEZONE_LABELS

        # Use whatever the bot considers "America/New_York" to label
        # (matches the prod default).
        view = TimezoneSelectView(current="America/New_York")
        labels = _labels(view)
        expected_label_fragment = TIMEZONE_LABELS["America/New_York"]
        assert any("Keep current" in lbl and expected_label_fragment in lbl for lbl in labels)

    def test_keep_button_is_on_row_zero_select_shifts_down(self):
        from setup_cog import TimezoneSelectView

        view = TimezoneSelectView(current="America/New_York")
        keep_btn = next(c for c in view.children if isinstance(c, discord.ui.Button))
        select = next(c for c in view.children if isinstance(c, discord.ui.Select))
        assert keep_btn.row == 0
        assert select.row == 1

    @pytest.mark.asyncio
    async def test_clicking_keep_returns_current_timezone(self):
        from setup_cog import TimezoneSelectView

        view = TimezoneSelectView(current="America/New_York")
        keep_btn = next(c for c in view.children if isinstance(c, discord.ui.Button))
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await keep_btn.callback(inter)
        assert view.selected == "America/New_York"
        assert view.confirmed is True

    def test_long_label_clipped_to_80_chars(self):
        from setup_cog import TimezoneSelectView, TIMEZONE_LABELS

        view = TimezoneSelectView(current="America/New_York")
        keep_btn = next(c for c in view.children if isinstance(c, discord.ui.Button))
        assert len(keep_btn.label) <= 80

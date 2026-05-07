"""
Tests for the new button-driven ChannelSelectStep flow added to work
around Discord's ChannelSelect dropping thread results when text-channel
types are also in the picker.

Three rendering modes to cover:

  * **Initial** — guild has pickable threads → view shows two primary
    buttons ("📢 Channel" and "🧵 Thread"). No selects yet.
  * **Channel mode** — clicked the Channel button → view shows a
    `ChannelSelect` (text-only) plus a secondary "🧵 Pick a thread
    instead" switch button.
  * **Thread mode** — clicked the Thread button → view shows a manual
    `Select` populated from `guild.threads`, plus a secondary "📢 Pick
    a channel instead" switch button.

Plus the legacy fallback paths:

  * No `guild` passed → single ChannelSelect with all four channel
    types (back-compat for the existing ``test_premium.py`` assertions).
  * Guild passed but **zero** pickable threads → single ChannelSelect
    (text-only); the button flow would just be confusing if the only
    button that actually works is "Channel".
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, AsyncMock

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# bot.py needs DISCORD_TOKEN to import.
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_thread(name: str, parent_name: str, parent_id: int, *,
                 archived: bool = False, locked: bool = False,
                 thread_id: int = None) -> MagicMock:
    """Build a Thread-like mock with the attributes ChannelSelectStep
    inspects: name, archived, locked, parent (with .name), parent_id,
    id, and a permissions_for that returns a perm allowing thread post.
    """
    thread = MagicMock(spec=discord.Thread)
    thread.id        = thread_id or hash(name) & 0x7FFFFFFF
    thread.name      = name
    thread.archived  = archived
    thread.locked    = locked
    thread.parent_id = parent_id
    parent = MagicMock()
    parent.name      = parent_name
    parent.id        = parent_id
    thread.parent    = parent
    perms = MagicMock()
    perms.send_messages_in_threads = True
    thread.permissions_for = MagicMock(return_value=perms)
    return thread


def _make_guild(threads: list[MagicMock], guild_id: int = 999) -> MagicMock:
    """Build a Guild-like mock with .threads, .me, and .id."""
    guild = MagicMock(spec=discord.Guild)
    guild.id      = guild_id
    guild.threads = threads
    guild.me      = MagicMock()
    return guild


def _labels(view) -> list[str]:
    """Pull labels off children, including buttons and select placeholders."""
    out = []
    for child in view.children:
        label = getattr(child, "label", None)
        if label is not None:
            out.append(label)
    return out


def _placeholders(view) -> list[str]:
    """Pull placeholders off Select-like children."""
    out = []
    for child in view.children:
        ph = getattr(child, "placeholder", None)
        if ph is not None:
            out.append(ph)
    return out


# ── Initial-state (button choice) tests ───────────────────────────────────────

class TestInitialButtonChoice:
    """When include_threads=True AND a guild is passed AND there's at
    least one pickable thread, the view starts with two primary buttons."""

    def test_initial_view_has_two_buttons_and_no_selects(self):
        from setup_cog import ChannelSelectStep
        guild = _make_guild([_make_thread("t1", "general", 100)])
        view  = ChannelSelectStep("placeholder", include_threads=True, guild=guild)

        labels = _labels(view)
        assert "📢 Channel" in labels
        assert "🧵 Thread" in labels
        # No selects yet — buttons only.
        assert all(not isinstance(c, (discord.ui.ChannelSelect, discord.ui.Select))
                   for c in view.children)

    def test_initial_buttons_are_primary_style(self):
        from setup_cog import ChannelSelectStep
        guild = _make_guild([_make_thread("t1", "general", 100)])
        view  = ChannelSelectStep("placeholder", include_threads=True, guild=guild)
        for child in view.children:
            if isinstance(child, discord.ui.Button):
                assert child.style == discord.ButtonStyle.primary


# ── Channel-mode (after clicking 📢 Channel) tests ────────────────────────────

class TestChannelMode:
    """After the user clicks the Channel button, the view should swap to
    a ChannelSelect (text-only) plus a 'switch to thread' button."""

    @pytest.mark.asyncio
    async def test_clicking_channel_shows_channelselect_and_switch(self):
        from setup_cog import ChannelSelectStep
        guild = _make_guild([_make_thread("t1", "general", 100)])
        view  = ChannelSelectStep("placeholder", include_threads=True, guild=guild)

        # Find the Channel button and fire its callback.
        channel_btn = next(c for c in view.children
                           if getattr(c, "label", "") == "📢 Channel")
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await channel_btn.callback(inter)

        # ChannelSelect now present, plus a switch-to-thread button.
        has_channel_select = any(isinstance(c, discord.ui.ChannelSelect)
                                 for c in view.children)
        assert has_channel_select
        assert "🧵 Pick a thread instead" in _labels(view)

    @pytest.mark.asyncio
    async def test_channel_mode_uses_text_only_types(self):
        """The whole point of the split: when we have threads to offer
        elsewhere, the ChannelSelect carries text-only types so Discord's
        mixed-type filtering bug doesn't bite."""
        from setup_cog import ChannelSelectStep
        guild = _make_guild([_make_thread("t1", "general", 100)])
        view  = ChannelSelectStep("placeholder", include_threads=True, guild=guild)
        channel_btn = next(c for c in view.children
                           if getattr(c, "label", "") == "📢 Channel")
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await channel_btn.callback(inter)

        select = next(c for c in view.children
                      if isinstance(c, discord.ui.ChannelSelect))
        assert select.channel_types == [discord.ChannelType.text]


# ── Thread-mode (after clicking 🧵 Thread) tests ──────────────────────────────

class TestThreadMode:

    @pytest.mark.asyncio
    async def test_clicking_thread_shows_thread_select_and_switch(self):
        from setup_cog import ChannelSelectStep
        guild = _make_guild([
            _make_thread("Inner Alliance Rewards & Train", "r4-chat", 100),
            _make_thread("DS/CS Mail",                     "r4-chat", 100),
        ])
        view  = ChannelSelectStep("placeholder", include_threads=True, guild=guild)

        thread_btn = next(c for c in view.children
                          if getattr(c, "label", "") == "🧵 Thread")
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await thread_btn.callback(inter)

        # A non-ChannelSelect Select is now present, plus the switch button.
        thread_select = next(
            (c for c in view.children
             if isinstance(c, discord.ui.Select)
             and not isinstance(c, discord.ui.ChannelSelect)),
            None,
        )
        assert thread_select is not None
        assert "📢 Pick a channel instead" in _labels(view)

        # Both threads should appear as options.
        option_labels = [opt.label for opt in thread_select.options]
        assert any("Inner Alliance Rewards & Train" in l for l in option_labels)
        assert any("DS/CS Mail" in l for l in option_labels)
        # Labels include the parent channel for context.
        assert all("(in #r4-chat)" in l for l in option_labels)

    @pytest.mark.asyncio
    async def test_thread_select_caps_at_25_options(self):
        """Discord's Select component maxes out at 25 options."""
        from setup_cog import ChannelSelectStep
        many = [_make_thread(f"thread-{i:02d}", "general", 100, thread_id=i)
                for i in range(40)]
        guild = _make_guild(many)
        view  = ChannelSelectStep("placeholder", include_threads=True, guild=guild)

        thread_btn = next(c for c in view.children
                          if getattr(c, "label", "") == "🧵 Thread")
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await thread_btn.callback(inter)

        thread_select = next(
            c for c in view.children
            if isinstance(c, discord.ui.Select)
            and not isinstance(c, discord.ui.ChannelSelect)
        )
        assert len(thread_select.options) == 25


# ── Switching back and forth ──────────────────────────────────────────────────

class TestSwitching:
    """The user can change their mind: pick Channel, then click 'Pick a
    thread instead' to swap to the thread select, and vice versa."""

    @pytest.mark.asyncio
    async def test_channel_then_switch_to_thread(self):
        from setup_cog import ChannelSelectStep
        guild = _make_guild([_make_thread("t1", "general", 100)])
        view  = ChannelSelectStep("placeholder", include_threads=True, guild=guild)

        # Click Channel.
        ch_btn = next(c for c in view.children
                      if getattr(c, "label", "") == "📢 Channel")
        inter1 = MagicMock(); inter1.response.edit_message = AsyncMock()
        await ch_btn.callback(inter1)

        # Now click the switch button.
        switch_btn = next(c for c in view.children
                          if getattr(c, "label", "") == "🧵 Pick a thread instead")
        inter2 = MagicMock(); inter2.response.edit_message = AsyncMock()
        await switch_btn.callback(inter2)

        # Now in thread mode: thread Select + "Pick a channel instead".
        has_thread_select = any(
            isinstance(c, discord.ui.Select)
            and not isinstance(c, discord.ui.ChannelSelect)
            for c in view.children
        )
        assert has_thread_select
        assert "📢 Pick a channel instead" in _labels(view)
        # And the channel Select is gone.
        assert not any(isinstance(c, discord.ui.ChannelSelect) for c in view.children)


# ── Fallback paths (back-compat) ──────────────────────────────────────────────

class TestFallbackPaths:

    def test_no_guild_passes_through_to_single_channelselect(self):
        """Existing test in test_premium.py (test_include_threads_adds_three_thread_types)
        instantiates without a guild — it should still get the legacy
        single-ChannelSelect with thread types in the channel_types list."""
        from setup_cog import ChannelSelectStep
        view = ChannelSelectStep("placeholder", include_threads=True)
        # Single ChannelSelect, no buttons.
        selects = [c for c in view.children
                   if isinstance(c, discord.ui.ChannelSelect)]
        assert len(selects) == 1
        # Legacy: thread types in the channel_types list.
        assert discord.ChannelType.public_thread in selects[0].channel_types
        assert discord.ChannelType.private_thread in selects[0].channel_types
        # No buttons (other than possibly the create-channel one).
        button_labels = [c.label for c in view.children
                         if isinstance(c, discord.ui.Button)]
        assert "📢 Channel" not in button_labels
        assert "🧵 Thread" not in button_labels

    def test_guild_with_zero_pickable_threads_skips_button_flow(self):
        """When a guild has no usable threads, don't bother with the
        button picker — it'd just be confusing."""
        from setup_cog import ChannelSelectStep
        guild = _make_guild([])  # empty
        view  = ChannelSelectStep("placeholder", include_threads=True, guild=guild)
        button_labels = [c.label for c in view.children
                         if isinstance(c, discord.ui.Button)]
        assert "📢 Channel" not in button_labels
        assert "🧵 Thread" not in button_labels
        # ChannelSelect rendered straight away.
        assert any(isinstance(c, discord.ui.ChannelSelect) for c in view.children)

    def test_archived_threads_filtered_out(self):
        from setup_cog import ChannelSelectStep
        guild = _make_guild([
            _make_thread("active",   "general", 100, archived=False),
            _make_thread("archived", "general", 100, archived=True),
            _make_thread("locked",   "general", 100, locked=True),
        ])
        # Pickable threads collector should keep only the active one.
        threads = ChannelSelectStep._collect_pickable_threads(guild)
        names = [t.name for t in threads]
        assert "active"   in names
        assert "archived" not in names
        assert "locked"   not in names

    def test_create_button_appears_in_simple_text_only_path(self):
        from setup_cog import ChannelSelectStep
        view = ChannelSelectStep("placeholder", include_threads=False, allow_create=True)
        labels = _labels(view)
        assert "➕ Create a new channel" in labels


# ── Create-channel button visibility (#48) ────────────────────────────────────

class TestCreateButtonAlwaysVisible:
    """Pre-1.1.0 the create-channel button was hidden whenever the wizard
    took a Premium path (button-driven Channel/Thread choice, or thread
    types in the picker). It should be visible everywhere a channel select
    is shown so admins can create the leadership channel mid-wizard."""

    def test_create_button_visible_when_no_pickable_threads(self):
        """Premium guild, but no pickable threads → channel select renders
        with thread types in the picker. Create button should still appear."""
        from setup_cog import ChannelSelectStep
        guild = _make_guild([])  # premium-flagged, but no threads
        view  = ChannelSelectStep(
            "placeholder", include_threads=True, guild=guild, allow_create=True,
        )
        assert "➕ Create a new channel" in _labels(view)

    @pytest.mark.asyncio
    async def test_create_button_visible_after_clicking_channel(self):
        """Premium guild with pickable threads → button-driven flow.
        After the user picks Channel, the create button should be present."""
        from setup_cog import ChannelSelectStep
        guild = _make_guild([_make_thread("t1", "general", 100)])
        view  = ChannelSelectStep(
            "placeholder", include_threads=True, guild=guild, allow_create=True,
        )

        ch_btn = next(c for c in view.children
                      if getattr(c, "label", "") == "📢 Channel")
        inter = MagicMock(); inter.response.edit_message = AsyncMock()
        await ch_btn.callback(inter)

        assert "➕ Create a new channel" in _labels(view)

    def test_allow_create_false_suppresses_button(self):
        """Sanity: callers can still opt out via allow_create=False."""
        from setup_cog import ChannelSelectStep
        view = ChannelSelectStep(
            "placeholder", include_threads=False, allow_create=False,
        )
        assert "➕ Create a new channel" not in _labels(view)

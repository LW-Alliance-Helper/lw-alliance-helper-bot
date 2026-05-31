"""
Tests for `setup_cog.ask_keep_or_change` — the helper that drives every
"keep this value or define your own" step in the /setup_* wizards.

The bug this protects against: the function used to take a single
`default=` parameter that callers pre-resolved to "saved-or-default".
The button label always said "✅ Use default: <value>" — even when the
value was actually a previously-saved guild value, not the bot's
hardcoded baseline. OGV admins running /setup_birthdays saw their saved
"Season 5 - Off-Season" tab rendered as "Use default: Season 5 -
Off-Season" — confusingly labelled.

The fix splits hardcoded `default=` from optional `current=`, with the
function picking the right label automatically:

  * `current` is None or `current == default` → original 2-button
    layout: "✅ Use default: X" / "✏️ Define my own". `keep` returns
    `default`.
  * `current` is set AND differs from `default` → 3-button layout:
    "✅ Keep current: <current>" / "↩️ Use default: <default>" /
    "✏️ Define my own". The "Keep current" button returns `current`,
    "Use default" returns `default`.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog imports premium → bot.py-adjacent; needs a fake DISCORD_TOKEN.
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


def _capture_view(channel_mock):
    """Pull the View object out of the most recent `channel.send(...)` call."""
    last = channel_mock.send.call_args
    return last.kwargs.get("view") if last is not None else None


async def _fake_cancel(view, ev):
    """Stand-in for wait_view_or_cancel that bails immediately so the
    function under test returns without waiting on real button input."""
    view.cancelled = True


async def _invoke(*, default, current=None):
    """Render the helper's view once and return (result, view) so each
    test can assert on the rendered button labels."""
    from setup_cog import ask_keep_or_change

    channel = MagicMock()
    channel.send = AsyncMock()

    with patch("setup_cog.wait_view_or_cancel", _fake_cancel):
        result = await ask_keep_or_change(
            channel,
            "Some prompt",
            default=default,
            current=current,
            modal_title="t",
            modal_label="l",
        )

    return result, _capture_view(channel)


# ── Two-button layout (original behavior, no `current` provided) ──────────────


class TestTwoButtonLayoutWhenNoDistinctCurrent:
    @pytest.mark.asyncio
    async def test_button_label_says_use_default_when_current_is_none(self):
        _, view = await _invoke(default="Birthdays")
        labels = [item.label for item in view.children]
        assert any("Use default" in lbl and "Birthdays" in lbl for lbl in labels)
        assert not any("Keep current" in lbl for lbl in labels)
        # Two buttons total: the keep + the "Define my own".
        assert len(view.children) == 2

    @pytest.mark.asyncio
    async def test_button_label_says_keep_current_when_current_equals_default(self):
        """Saved value matches the hardcoded default → the primary
        button still labels as 'Keep current: X', not 'Use default: X'.
        Effect is identical (both return the same value), but the
        wording matches the leadership's mental model when re-running
        the wizard: 'I want to keep what's saved'.

        Surfaced as feedback after the initial #80 rollout: leadership
        running /setup_growth on a guild that accepted all defaults
        the first time saw only 'Use default' buttons and worried
        clicking them would wipe their config. Use-default also gone
        from this case — there's nothing distinct to revert to."""
        _, view = await _invoke(default="Birthdays", current="Birthdays")
        labels = [item.label for item in view.children]
        assert any("Keep current" in lbl and "Birthdays" in lbl for lbl in labels)
        assert not any("Use default" in lbl for lbl in labels)
        # Still two buttons total — Keep + Define my own — because
        # there's no distinct default to revert to.
        assert len(view.children) == 2

    @pytest.mark.asyncio
    async def test_button_label_treats_empty_current_as_none(self):
        """Wizards pass `current=current.get('X', '')` so missing keys
        produce an empty string. That should also fall back to the
        2-button layout, not show 'Keep current: '."""
        _, view = await _invoke(default="Birthdays", current="")
        labels = [item.label for item in view.children]
        assert any("Use default" in lbl for lbl in labels)
        assert not any("Keep current" in lbl for lbl in labels)


# ── Three-button layout (`current` differs from `default`) ────────────────────


class TestThreeButtonLayoutWhenCurrentDiffersFromDefault:
    """The OGV regression: if the guild has previously saved a tab name
    like 'Season 5 - Off-Season', the wizard must label it as 'Keep
    current' so admins don't think the bot ships with that value as
    its hardcoded default."""

    @pytest.mark.asyncio
    async def test_three_buttons_when_current_set_and_different(self):
        _, view = await _invoke(default="Birthdays", current="Season 5 - Off-Season")
        # Keep current + Use default + Define my own
        assert len(view.children) == 3

    @pytest.mark.asyncio
    async def test_keep_current_label_uses_current_value(self):
        _, view = await _invoke(default="Birthdays", current="Season 5 - Off-Season")
        labels = [item.label for item in view.children]
        assert any("Keep current" in lbl and "Season 5 - Off-Season" in lbl for lbl in labels)

    @pytest.mark.asyncio
    async def test_revert_to_default_button_uses_default_value(self):
        """Even with a saved value, the user can revert to the hardcoded
        default in one click without typing it manually."""
        _, view = await _invoke(default="Birthdays", current="Season 5 - Off-Season")
        labels = [item.label for item in view.children]
        # The revert button uses ↩️ rather than ✅ to differentiate it.
        assert any("Use default" in lbl and "Birthdays" in lbl for lbl in labels)

    @pytest.mark.asyncio
    async def test_define_my_own_button_present_in_both_layouts(self):
        for current in (None, "Season 5 - Off-Season"):
            _, view = await _invoke(default="Birthdays", current=current)
            labels = [item.label for item in view.children]
            assert any("Define my own" in lbl for lbl in labels)

    @pytest.mark.asyncio
    async def test_long_current_value_clipped_to_80_chars(self):
        """Discord enforces an 80-char button label cap. The helper
        truncates internally so a long saved value can't crash the
        view rendering."""
        long_current = "X" * 200
        _, view = await _invoke(default="Birthdays", current=long_current)
        for item in view.children:
            assert len(item.label) <= 80


# ── Function still returns None on cancel ────────────────────────────────────


class TestCancelStillReturnsNoneAcrossLayouts:
    """The `current` plumbing must not change the cancel-path contract:
    every wizard's `if X is None: return` path depends on this."""

    @pytest.mark.asyncio
    async def test_returns_none_on_cancel_two_button(self):
        result, _ = await _invoke(default="Birthdays")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_cancel_three_button(self):
        result, _ = await _invoke(default="Birthdays", current="Season 5 - Off-Season")
        assert result is None

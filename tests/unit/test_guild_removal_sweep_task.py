"""
Tests for `bot.guild_removal_sweep_task`'s handling of `result["freed_premium"]`
(#573) — the part that runs outside `config.sweep_guild_removals` because
config.py has no premium cache and no Discord client to DM with.

The sweep itself (which guilds get purged, when a pin is actually released
in the database) is covered end to end in `test_guild_removal.py`. These
tests patch `config.sweep_guild_removals` to a canned result and check only
the cache-invalidation + DM half.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _sweep_result(freed_premium=None):
    return {
        "guilds": list((freed_premium or {}).keys()),
        "config": {"deleted": {}, "scrubbed": {}},
        "champion_duel": {"deleted": {}, "scrubbed": {}},
        "alliance_duel": {"deleted": {}, "scrubbed": {}},
        "rejoined": [],
        "failed": [],
        "freed_premium": freed_premium or {},
        "applied": True,
    }


@pytest.mark.asyncio
async def test_no_freed_pins_touches_nothing():
    import bot

    with patch("config.sweep_guild_removals", return_value=_sweep_result()):
        with patch("premium._cache_invalidate_guild") as mock_invalidate:
            await bot.guild_removal_sweep_task.coro()

    mock_invalidate.assert_not_called()


@pytest.mark.asyncio
async def test_an_active_subscriber_is_dmed_and_the_cache_is_cleared():
    import bot

    fake_cog = MagicMock()
    fake_cog.dm_premium_pin_released = AsyncMock()

    with (
        patch("config.sweep_guild_removals", return_value=_sweep_result({555: 111})),
        patch("premium._cache_invalidate_guild") as mock_invalidate,
        patch("premium.user_has_active_subscription", AsyncMock(return_value=True)),
        patch.object(bot.bot, "get_cog", return_value=fake_cog),
    ):
        await bot.guild_removal_sweep_task.coro()

    mock_invalidate.assert_called_once_with(555)
    fake_cog.dm_premium_pin_released.assert_awaited_once_with(111, 555)


@pytest.mark.asyncio
async def test_a_lapsed_subscriber_is_not_dmed_but_the_cache_still_clears():
    """The pin is released either way -- the DM is a courtesy for someone
    still paying, not a condition of the release itself."""
    import bot

    fake_cog = MagicMock()
    fake_cog.dm_premium_pin_released = AsyncMock()

    with (
        patch("config.sweep_guild_removals", return_value=_sweep_result({555: 111})),
        patch("premium._cache_invalidate_guild") as mock_invalidate,
        patch("premium.user_has_active_subscription", AsyncMock(return_value=False)),
        patch.object(bot.bot, "get_cog", return_value=fake_cog),
    ):
        await bot.guild_removal_sweep_task.coro()

    mock_invalidate.assert_called_once_with(555)
    fake_cog.dm_premium_pin_released.assert_not_awaited()


@pytest.mark.asyncio
async def test_multiple_freed_guilds_are_each_handled():
    import bot

    fake_cog = MagicMock()
    fake_cog.dm_premium_pin_released = AsyncMock()

    with (
        patch("config.sweep_guild_removals", return_value=_sweep_result({555: 111, 777: 222})),
        patch("premium._cache_invalidate_guild") as mock_invalidate,
        patch("premium.user_has_active_subscription", AsyncMock(return_value=True)),
        patch.object(bot.bot, "get_cog", return_value=fake_cog),
    ):
        await bot.guild_removal_sweep_task.coro()

    assert mock_invalidate.call_count == 2
    assert fake_cog.dm_premium_pin_released.await_count == 2


@pytest.mark.asyncio
async def test_a_dm_failure_does_not_crash_the_loop():
    import bot

    fake_cog = MagicMock()
    fake_cog.dm_premium_pin_released = AsyncMock(side_effect=RuntimeError("DMs closed"))

    with (
        patch("config.sweep_guild_removals", return_value=_sweep_result({555: 111})),
        patch("premium._cache_invalidate_guild"),
        patch("premium.user_has_active_subscription", AsyncMock(return_value=True)),
        patch.object(bot.bot, "get_cog", return_value=fake_cog),
    ):
        await bot.guild_removal_sweep_task.coro()  # must not raise


@pytest.mark.asyncio
async def test_donate_cog_not_loaded_skips_the_dm_quietly():
    import bot

    with (
        patch("config.sweep_guild_removals", return_value=_sweep_result({555: 111})),
        patch("premium._cache_invalidate_guild") as mock_invalidate,
        patch("premium.user_has_active_subscription", AsyncMock(return_value=True)),
        patch.object(bot.bot, "get_cog", return_value=None),
    ):
        await bot.guild_removal_sweep_task.coro()  # must not raise

    mock_invalidate.assert_called_once_with(555)

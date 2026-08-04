"""The clock behind config_health (#414 / #379).

Thin by design: the pass itself is covered in test_config_health.py. What
matters here is that the loop can't take the bot down with it, and that it
reports its liveness the way every other clock-driven loop does.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import config_health_cog  # noqa: E402


def _cog():
    cog = config_health_cog.ConfigHealthCog.__new__(config_health_cog.ConfigHealthCog)
    cog.bot = MagicMock()
    return cog


class TestNotifyLoop:
    @pytest.mark.asyncio
    async def test_runs_the_pass_and_stamps_a_heartbeat(self):
        cog = _cog()
        stamp = MagicMock()
        with (
            patch("config_health.run_notifier_pass", AsyncMock(return_value=2)) as run,
            patch("config.stamp_loop_heartbeat", stamp),
        ):
            await type(cog).notify.coro(cog)

        run.assert_awaited_once_with(cog.bot)
        stamp.assert_called_once_with("config_health")

    @pytest.mark.asyncio
    async def test_a_failing_pass_does_not_raise_into_the_loop(self):
        """tasks.loop stops permanently on an unhandled exception, which would
        silently disable every guild's notices until the next restart."""
        cog = _cog()
        stamp = MagicMock()
        with (
            patch("config_health.run_notifier_pass", AsyncMock(side_effect=RuntimeError("boom"))),
            patch("config.stamp_loop_heartbeat", stamp),
        ):
            await type(cog).notify.coro(cog)

        # No heartbeat either: a pass that blew up did not run cleanly.
        stamp.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_quiet_pass_still_stamps(self):
        cog = _cog()
        stamp = MagicMock()
        with (
            patch("config_health.run_notifier_pass", AsyncMock(return_value=0)),
            patch("config.stamp_loop_heartbeat", stamp),
        ):
            await type(cog).notify.coro(cog)

        stamp.assert_called_once_with("config_health")


def test_interval_matches_the_documented_cadence():
    """15 minutes was the settled decision; the docstring and the loop must not
    drift apart."""
    assert config_health_cog.PASS_INTERVAL_MINUTES == 15


if __name__ == "__main__":
    pytest.main([__file__])

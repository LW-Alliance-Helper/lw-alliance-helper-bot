"""
Coverage for `setup_hub._SetupHubView.btn_root_wizard` (#582).

#319 first guarded this one button against a lost-access Forbidden/NotFound
mid-wizard, but the button itself had no test — the crash this ticket
tracks (#582's Sentry issue) hit a *different* launcher (`_launch_event_setup`)
that never got the same guard. This file exercises the shared
`wizard_registry.guard_wizard_launch` this button now uses, since every
other launcher's coverage lives with its own wizard's tests.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, make_mock_interaction


def _make_view(is_premium: bool = True):
    from setup_hub import _SetupHubView

    bot = AsyncMock()
    return _SetupHubView(bot, TEST_GUILD_ID, owner_user_id=123456789, is_premium=is_premium)


class TestBtnRootWizard:
    @pytest.mark.asyncio
    async def test_lost_access_mid_wizard_is_absorbed_not_raised(self, seeded_db):
        """A Forbidden(50001) raised partway through `run_setup` (the wizard
        losing channel access mid-flow) must not propagate to discord.py's
        view-error handler — the shared guard (#582) absorbs it."""
        interaction = make_mock_interaction()
        view = _make_view()

        resp = MagicMock()
        resp.status = 403
        forbidden = discord.Forbidden(resp, {"message": "Missing Access", "code": 50001})

        with (
            patch("setup_cog._check_wizard_can_run", new=AsyncMock(return_value=True)),
            patch("setup_cog.run_setup", new=AsyncMock(side_effect=forbidden)),
        ):
            # Must not raise.
            await view.btn_root_wizard.callback(interaction)

        interaction.followup.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_perms_check_failing_never_reaches_the_wizard(self, seeded_db):
        interaction = make_mock_interaction()
        view = _make_view()

        with (
            patch("setup_cog._check_wizard_can_run", new=AsyncMock(return_value=False)),
            patch("setup_cog.run_setup", new=AsyncMock()) as mock_run,
        ):
            await view.btn_root_wizard.callback(interaction)

        mock_run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_happy_path_runs_the_wizard(self, seeded_db):
        interaction = make_mock_interaction()
        view = _make_view()

        with (
            patch("setup_cog._check_wizard_can_run", new=AsyncMock(return_value=True)),
            patch("setup_cog.run_setup", new=AsyncMock()) as mock_run,
        ):
            await view.btn_root_wizard.callback(interaction)

        mock_run.assert_awaited_once()

"""Tests for the thin /map_manager command (hub redesign, #338).

The command just opens the Map Manager hub; the hub logic, modals, helpers, and
gating are covered in test_mapmanager_hub.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import mapmanager_cog
import mapmanager_hub
from tests.conftest import make_mock_interaction


def _cog():
    return mapmanager_cog.MapManagerCog(AsyncMock())


async def test_command_opens_hub(monkeypatch):
    cog = _cog()
    handler = AsyncMock()
    monkeypatch.setattr(mapmanager_hub, "handle_mapmanager_hub", handler)
    interaction = make_mock_interaction(is_admin=True)
    await cog.map_manager.callback(cog, interaction)
    handler.assert_awaited_once_with(cog.bot, interaction)

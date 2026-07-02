"""Tests for the MAP_MANAGER_COMMANDS_ENABLED visibility flag (#316 / #338).

The Map Manager integration ships to production with its HTTP endpoints live
(gated separately by MAPMANAGER_API_KEY) but the user-facing surfaces hidden
until Map Manager is ready to reveal them. This flag gates two things:

- the `/map_manager` cog load in bot.py (so the command group never enters the
  tree and the global sync doesn't publish it), and
- the Map Manager button + feature row in the `/setup` hub.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from api_server import map_manager_commands_enabled


class TestFlagParsing:
    def test_default_off(self, monkeypatch):
        monkeypatch.delenv("MAP_MANAGER_COMMANDS_ENABLED", raising=False)
        assert map_manager_commands_enabled() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "On", " on "])
    def test_truthy_values_enable(self, monkeypatch, val):
        monkeypatch.setenv("MAP_MANAGER_COMMANDS_ENABLED", val)
        assert map_manager_commands_enabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "  ", "enabled?"])
    def test_other_values_stay_off(self, monkeypatch, val):
        monkeypatch.setenv("MAP_MANAGER_COMMANDS_ENABLED", val)
        assert map_manager_commands_enabled() is False


class TestSetupHubButtonGating:
    """The Map Manager button is dropped from the hub view unless the flag is
    on. When on, it's present (and Premium-gated as before)."""

    def _make_view(self):
        from setup_hub import _SetupHubView

        with patch("config.get_config", return_value=None):
            return _SetupHubView(MagicMock(), 123, 456, is_premium=True)

    def test_button_absent_when_flag_off(self, monkeypatch):
        monkeypatch.delenv("MAP_MANAGER_COMMANDS_ENABLED", raising=False)
        view = self._make_view()
        labels = [getattr(c, "label", None) for c in view.children]
        assert "🗺️ Map Manager" not in labels

    def test_button_present_when_flag_on(self, monkeypatch):
        monkeypatch.setenv("MAP_MANAGER_COMMANDS_ENABLED", "1")
        view = self._make_view()
        labels = [getattr(c, "label", None) for c in view.children]
        assert "🗺️ Map Manager" in labels

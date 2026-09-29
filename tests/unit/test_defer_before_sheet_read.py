"""Buttons that read the Sheet acknowledge the click first (#677).

Discord gives a click three seconds to be answered. A handler that reads the
Sheet before answering leaves the officer on "The application did not
respond" whenever Google is slow. Each case here stubs the Sheet read to
record whether the defer had already happened when it ran.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

GUILD_ID = 1234


def _interaction():
    inter = MagicMock()
    inter.guild_id = GUILD_ID
    inter.user.id = 42
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock(
        side_effect=lambda **_: inter.response.is_done.configure_mock(return_value=True)
    )
    inter.followup.send = AsyncMock(return_value=MagicMock())
    inter.original_response = AsyncMock(return_value=MagicMock())
    return inter


def _read_after_defer(inter, calls, result):
    """A stand-in Sheet read that notes whether the click was answered first."""

    def _read(*_a, **_k):
        calls.append(inter.response.defer.await_count)
        return result

    return _read


@pytest.mark.parametrize("method", ["_edit", "_set_active", "_delete"])
async def test_train_preset_buttons_defer_first(method):
    import train_hub

    inter = _interaction()
    calls: list[int] = []
    view = train_hub.PresetsManageView(MagicMock(), GUILD_ID, 42, "Day Rules")
    with (
        patch.object(train_hub.tr, "list_presets", new=_read_after_defer(inter, calls, [])),
        patch.object(train_hub.PresetsManageView, "_active", return_value="Standard Week"),
    ):
        await getattr(view, method)(inter)
    assert calls == [1]
    inter.response.send_message.assert_not_called()
    assert inter.followup.send.await_args.kwargs.get("ephemeral") is True


async def test_train_member_rule_remove_defers_first():
    import train_hub

    inter = _interaction()
    calls: list[int] = []
    view = train_hub.MemberRulesManageView(MagicMock(), GUILD_ID, 42, "Member Rules")
    with patch.object(train_hub.tr, "load_member_rules", new=_read_after_defer(inter, calls, [])):
        await view._remove_rule(inter)
    assert calls == [1]
    inter.response.send_message.assert_not_called()


@pytest.mark.parametrize("button", ["update", "generate"])
async def test_train_schedule_buttons_defer_first(button):
    import train_ui

    inter = _interaction()
    calls: list[int] = []
    view = train_ui.TrainActionView(MagicMock(), GUILD_ID, blurbs_enabled=True)
    with patch.object(train_ui, "load_schedule", new=_read_after_defer(inter, calls, {})):
        await getattr(view, button).callback(inter)
    assert calls == [1]
    inter.response.send_message.assert_not_called()


async def test_storm_strategy_list_defers_first():
    import storm_strategy_ui

    inter = _interaction()
    calls: list[int] = []
    with (
        patch.object(storm_strategy_ui, "_deny_if_not_leader", new=AsyncMock(return_value=True)),
        patch.object(storm_strategy_ui.ss, "list_presets", new=_read_after_defer(inter, calls, [])),
    ):
        await storm_strategy_ui.open_strategy_list(inter, "DS")
    assert calls == [1]
    # The list stays public, as it was before the defer.
    inter.response.defer.assert_awaited_once_with(thinking=True)
    inter.response.send_message.assert_not_called()


async def test_storm_member_rule_list_defers_first():
    import storm_member_rules

    inter = _interaction()
    calls: list[int] = []
    with (
        patch.object(storm_member_rules, "_deny_if_not_leader", new=AsyncMock(return_value=True)),
        patch.object(storm_member_rules, "list_rules", new=_read_after_defer(inter, calls, [])),
    ):
        await storm_member_rules.open_member_rule_list(inter, "DS")
    assert calls == [1]
    inter.response.defer.assert_awaited_once_with(thinking=True)
    inter.response.send_message.assert_not_called()

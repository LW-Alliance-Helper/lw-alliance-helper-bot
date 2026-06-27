"""Tests for the /map_manager cog (6C, #316).

Covers command-level gating (admin / Premium / configured), the pure helpers,
and the shared ``_perform_link_call`` MM-call → persist → reply path. Discord
I/O is mocked; MM and premium are patched so no network or entitlement lookups
run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import config
import mapmanager_client
import mapmanager_cog
import premium
from tests.conftest import make_mock_interaction


def _cog():
    return mapmanager_cog.MapManagerCog(AsyncMock())


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_validate_inputs_ok():
    assert mapmanager_cog._validate_inputs(" 738 ", " Nox ") == (738, "Nox")


def test_validate_inputs_bad_server():
    out = mapmanager_cog._validate_inputs("73a", "Nox")
    assert isinstance(out, str) and "server number" in out.lower()


def test_validate_inputs_blank_alliance():
    out = mapmanager_cog._validate_inputs("738", "   ")
    assert isinstance(out, str)


def test_setup_success_message_new_alliance_no_grouping(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_URL", "https://mm.example")
    msg = mapmanager_cog._setup_success_message(
        {
            "alliance_name": "Nox",
            "server": 738,
            "alliance_id": "A1",
            "alliance_created": True,
            "server_grouping_id": None,
        },
        "Nox",
        738,
    )
    assert "https://mm.example/alliance/A1" in msg
    assert "new alliance record" in msg.lower()
    assert "season grouping" in msg.lower()


def test_setup_success_message_existing_alliance_with_grouping(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_URL", "https://mm.example")
    msg = mapmanager_cog._setup_success_message(
        {
            "alliance_name": "Nox",
            "server": 738,
            "alliance_id": "A1",
            "alliance_created": False,
            "server_grouping_id": "G1",
        },
        "Nox",
        738,
    )
    assert "new alliance record" not in msg.lower()
    assert "season grouping" not in msg.lower()


# ── /map_manager setup gating ─────────────────────────────────────────────────


async def test_setup_rejects_non_admin(monkeypatch):
    cog = _cog()
    gate = AsyncMock()
    monkeypatch.setattr(premium, "feature_gate", gate)
    interaction = make_mock_interaction(is_admin=False)
    await cog.setup_cmd.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_once()
    gate.assert_not_called()  # bailed before the premium check
    interaction.response.send_modal.assert_not_called()


async def test_setup_premium_locked(monkeypatch):
    cog = _cog()
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=False))
    interaction = make_mock_interaction(is_admin=True)
    await cog.setup_cmd.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_once()
    interaction.response.send_modal.assert_not_called()


async def test_setup_not_configured(monkeypatch):
    cog = _cog()
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=True))
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: False)
    interaction = make_mock_interaction(is_admin=True)
    await cog.setup_cmd.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_once()
    assert "isn't switched on" in interaction.response.send_message.call_args.args[0]
    interaction.response.send_modal.assert_not_called()


async def test_setup_opens_modal(monkeypatch):
    cog = _cog()
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=True))
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: True)
    interaction = make_mock_interaction(is_admin=True)
    await cog.setup_cmd.callback(cog, interaction)
    interaction.response.send_modal.assert_awaited_once()


# ── /map_manager change gating ────────────────────────────────────────────────


async def test_change_requires_existing_link(temp_db, monkeypatch):
    cog = _cog()
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: True)
    interaction = make_mock_interaction(is_admin=True)
    await cog.change_cmd.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_once()
    interaction.response.send_modal.assert_not_called()


async def test_change_opens_modal(temp_db, monkeypatch):
    cog = _cog()
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: True)
    interaction = make_mock_interaction(is_admin=True)
    config.save_guild_alliance_mapping(interaction.guild_id, "Nox", 738, "A1", "G1")
    await cog.change_cmd.callback(cog, interaction)
    interaction.response.send_modal.assert_awaited_once()


# ── /map_manager unlink ───────────────────────────────────────────────────────


async def test_unlink_nothing_to_remove(temp_db):
    cog = _cog()
    interaction = make_mock_interaction(is_admin=True)
    await cog.unlink_cmd.callback(cog, interaction)
    interaction.response.send_message.assert_awaited_once()
    assert "nothing to remove" in interaction.response.send_message.call_args.args[0].lower()


async def test_unlink_sends_confirm_view(temp_db):
    cog = _cog()
    interaction = make_mock_interaction(is_admin=True)
    config.save_guild_alliance_mapping(interaction.guild_id, "Nox", 738, "A1", "G1")
    await cog.unlink_cmd.callback(cog, interaction)
    view = interaction.response.send_message.call_args.kwargs.get("view")
    assert isinstance(view, mapmanager_cog._UnlinkConfirm)


# ── _perform_link_call ────────────────────────────────────────────────────────


async def test_perform_link_setup_persists_and_replies(temp_db, monkeypatch):
    interaction = make_mock_interaction(is_admin=True)
    result = {
        "alliance_name": "Nox",
        "server": 738,
        "alliance_id": "A1",
        "alliance_created": True,
        "server_grouping_id": None,
    }
    monkeypatch.setattr(mapmanager_client, "create_guild_link", AsyncMock(return_value=result))
    monkeypatch.setenv("MAPMANAGER_API_URL", "https://mm.example")

    await mapmanager_cog._perform_link_call(interaction, 738, "Nox", change=False)

    interaction.response.defer.assert_awaited_once()
    interaction.followup.send.assert_awaited_once()
    saved = config.get_guild_alliance_mapping(interaction.guild_id)
    assert saved["mm_alliance_id"] == "A1"
    assert saved["server"] == 738
    assert saved["mm_server_grouping_id"] is None


async def test_perform_link_surfaces_mm_error_and_persists_nothing(temp_db, monkeypatch):
    interaction = make_mock_interaction(is_admin=True)
    monkeypatch.setattr(
        mapmanager_client,
        "create_guild_link",
        AsyncMock(side_effect=mapmanager_client.MapManagerError(400, "bad input")),
    )
    await mapmanager_cog._perform_link_call(interaction, 738, "Nox", change=False)
    interaction.followup.send.assert_awaited_once()
    assert "bad input" in interaction.followup.send.call_args.args[0]
    assert config.get_guild_alliance_mapping(interaction.guild_id) is None


async def test_perform_link_change_updates_mapping(temp_db, monkeypatch):
    interaction = make_mock_interaction(is_admin=True)
    config.save_guild_alliance_mapping(interaction.guild_id, "Nox", 738, "A1", "G1")
    result = {
        "alliance_name": "Renamed",
        "server": 740,
        "alliance_id": "A1",
        "server_grouping_id": "G1",
    }
    monkeypatch.setattr(mapmanager_client, "update_guild_link", AsyncMock(return_value=result))

    await mapmanager_cog._perform_link_call(interaction, 740, "Renamed", change=True)

    saved = config.get_guild_alliance_mapping(interaction.guild_id)
    assert saved["alliance_name"] == "Renamed"
    assert saved["server"] == 740

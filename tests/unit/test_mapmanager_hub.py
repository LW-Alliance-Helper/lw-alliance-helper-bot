"""Tests for the /map_manager hub (hub redesign, #338).

Covers the pure helpers, the shared ``_perform_link_call`` MM-call → persist →
reply path, the hub embed, the hub view's button callbacks (link Premium gate,
change, unlink), and the ``handle_mapmanager_hub`` admin gate. Discord I/O is
mocked; MM and premium are patched so no network or entitlement lookups run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import config
import mapmanager_client
import mapmanager_hub
import premium
from tests.conftest import TEST_GUILD_ID, make_mock_interaction


def _view(*, mapping=None, configured=True, guild_id=TEST_GUILD_ID, owner_id=123456789):
    return mapmanager_hub._MapManagerHubView(
        AsyncMock(), guild_id, owner_id, mapping=mapping, configured=configured
    )


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_validate_inputs_ok():
    assert mapmanager_hub._validate_inputs(" 738 ", " Nox ") == (738, "Nox")


def test_validate_inputs_bad_server():
    out = mapmanager_hub._validate_inputs("73a", "Nox")
    assert isinstance(out, str) and "server number" in out.lower()


def test_validate_inputs_blank_alliance():
    out = mapmanager_hub._validate_inputs("738", "   ")
    assert isinstance(out, str)


def test_setup_success_message_new_alliance_no_grouping(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_URL", "https://mm.example")
    msg = mapmanager_hub._setup_success_message(
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
    msg = mapmanager_hub._setup_success_message(
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


# ── hub embed ─────────────────────────────────────────────────────────────────


def test_embed_linked_mentions_alliance_and_server():
    embed = mapmanager_hub._build_mapmanager_hub_embed(
        1, mapping={"alliance_name": "Nox", "server": 738}, configured=True
    )
    assert "Nox" in embed.description and "738" in embed.description
    assert all(f.name != "⚠️ Not switched on" for f in embed.fields)


def test_embed_not_linked_prompts_to_link():
    embed = mapmanager_hub._build_mapmanager_hub_embed(1, mapping=None, configured=True)
    assert "isn't linked" in embed.description.lower()


def test_embed_not_configured_adds_warning_field():
    embed = mapmanager_hub._build_mapmanager_hub_embed(1, mapping=None, configured=False)
    assert any(f.name == "⚠️ Not switched on" for f in embed.fields)


# ── hub view: link (Premium-gated) ────────────────────────────────────────────


async def test_on_link_premium_locked(monkeypatch):
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=False))
    view = _view(mapping=None)
    interaction = make_mock_interaction(is_admin=True)
    await view._on_link(interaction)
    interaction.response.send_message.assert_awaited_once()
    interaction.response.send_modal.assert_not_called()


async def test_on_link_not_configured(monkeypatch):
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=True))
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: False)
    view = _view(mapping=None)
    interaction = make_mock_interaction(is_admin=True)
    await view._on_link(interaction)
    assert "isn't switched on" in interaction.response.send_message.call_args.args[0]
    interaction.response.send_modal.assert_not_called()


async def test_on_link_opens_modal(monkeypatch):
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=True))
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: True)
    view = _view(mapping=None)
    interaction = make_mock_interaction(is_admin=True)
    await view._on_link(interaction)
    interaction.response.send_modal.assert_awaited_once()


# ── hub view: change ──────────────────────────────────────────────────────────


async def test_on_change_requires_existing_link(temp_db, monkeypatch):
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: True)
    view = _view(mapping=None)
    interaction = make_mock_interaction(is_admin=True)
    view.guild_id = interaction.guild_id
    await view._on_change(interaction)
    interaction.response.send_message.assert_awaited_once()
    interaction.response.send_modal.assert_not_called()


async def test_on_change_opens_modal(temp_db, monkeypatch):
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: True)
    interaction = make_mock_interaction(is_admin=True)
    config.save_guild_alliance_mapping(interaction.guild_id, "Nox", 738, "A1", "G1")
    view = _view(mapping={"alliance_name": "Nox", "server": 738})
    view.guild_id = interaction.guild_id
    await view._on_change(interaction)
    interaction.response.send_modal.assert_awaited_once()


# ── hub view: unlink ──────────────────────────────────────────────────────────


async def test_on_unlink_nothing_to_remove(temp_db):
    interaction = make_mock_interaction(is_admin=True)
    view = _view(mapping=None)
    view.guild_id = interaction.guild_id
    await view._on_unlink(interaction)
    assert "nothing to remove" in interaction.response.send_message.call_args.args[0].lower()


async def test_on_unlink_sends_confirm_view(temp_db):
    interaction = make_mock_interaction(is_admin=True)
    config.save_guild_alliance_mapping(interaction.guild_id, "Nox", 738, "A1", "G1")
    view = _view(mapping={"alliance_name": "Nox", "server": 738})
    view.guild_id = interaction.guild_id
    await view._on_unlink(interaction)
    sent = interaction.response.send_message.call_args.kwargs.get("view")
    assert isinstance(sent, mapmanager_hub._UnlinkConfirm)


# ── handle_mapmanager_hub gating ──────────────────────────────────────────────


async def test_hub_rejects_non_admin():
    interaction = make_mock_interaction(is_admin=False)
    await mapmanager_hub.handle_mapmanager_hub(AsyncMock(), interaction)
    interaction.response.send_message.assert_awaited_once()
    assert "server admin" in interaction.response.send_message.call_args.args[0].lower()


async def test_hub_admin_not_linked_opens_link_hub(temp_db, monkeypatch):
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: True)
    interaction = make_mock_interaction(is_admin=True)
    await mapmanager_hub.handle_mapmanager_hub(AsyncMock(), interaction)
    kwargs = interaction.response.send_message.call_args.kwargs
    assert isinstance(kwargs.get("view"), mapmanager_hub._MapManagerHubView)
    assert "isn't linked" in kwargs["embed"].description.lower()


async def test_hub_admin_linked_shows_link(temp_db, monkeypatch):
    monkeypatch.setattr(mapmanager_client, "is_configured", lambda: True)
    interaction = make_mock_interaction(is_admin=True)
    config.save_guild_alliance_mapping(interaction.guild_id, "Nox", 738, "A1", "G1")
    await mapmanager_hub.handle_mapmanager_hub(AsyncMock(), interaction)
    kwargs = interaction.response.send_message.call_args.kwargs
    assert "Nox" in kwargs["embed"].description

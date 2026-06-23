"""Tests for the bot → Map Manager API client (6C, #316).

Status→behavior mapping is checked by patching ``_request``; one end-to-end case
runs against a fake MM ``TestServer`` to prove the real round-trip (Bearer
header, string-encoded snowflakes, JSON parsing).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import mapmanager_client as mm


# ── config / helpers ──────────────────────────────────────────────────────────


def test_is_configured(monkeypatch):
    monkeypatch.delenv("MAPMANAGER_API_URL", raising=False)
    monkeypatch.delenv("MAPMANAGER_API_KEY", raising=False)
    assert mm.is_configured() is False
    monkeypatch.setenv("MAPMANAGER_API_URL", "https://mm.example")
    assert mm.is_configured() is False
    monkeypatch.setenv("MAPMANAGER_API_KEY", "k")
    assert mm.is_configured() is True


async def test_request_raises_when_unconfigured(monkeypatch):
    monkeypatch.delenv("MAPMANAGER_API_URL", raising=False)
    monkeypatch.delenv("MAPMANAGER_API_KEY", raising=False)
    with pytest.raises(mm.MapManagerNotConfigured):
        await mm.get_guild_link(123)


def test_alliance_dashboard_url(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_URL", "https://mm.example/")
    assert mm.alliance_dashboard_url("A1") == "https://mm.example/alliance/A1"
    assert mm.alliance_dashboard_url(None) is None
    monkeypatch.delenv("MAPMANAGER_API_URL", raising=False)
    assert mm.alliance_dashboard_url("A1") is None


# ── status → behavior mapping (patch _request) ────────────────────────────────


async def test_create_returns_body_on_201(monkeypatch):
    monkeypatch.setattr(mm, "_request", AsyncMock(return_value=(201, {"alliance_id": "A1"})))
    assert await mm.create_guild_link(1, 738, "Nox", 2) == {"alliance_id": "A1"}


async def test_create_raises_on_400(monkeypatch):
    monkeypatch.setattr(mm, "_request", AsyncMock(return_value=(400, {"error": "bad_input"})))
    with pytest.raises(mm.MapManagerError) as ei:
        await mm.create_guild_link(1, 738, "Nox", 2)
    assert ei.value.status == 400
    assert ei.value.code == "bad_input"


async def test_get_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(mm, "_request", AsyncMock(return_value=(404, None)))
    assert await mm.get_guild_link(1) is None


async def test_get_returns_body_on_200(monkeypatch):
    monkeypatch.setattr(mm, "_request", AsyncMock(return_value=(200, {"link_id": "L1"})))
    assert (await mm.get_guild_link(1))["link_id"] == "L1"


async def test_update_requires_a_field(monkeypatch):
    req = AsyncMock()
    monkeypatch.setattr(mm, "_request", req)
    with pytest.raises(mm.MapManagerError):
        await mm.update_guild_link(1)
    req.assert_not_called()  # never reaches the network


async def test_update_returns_body_on_200(monkeypatch):
    monkeypatch.setattr(mm, "_request", AsyncMock(return_value=(200, {"server": 740})))
    assert (await mm.update_guild_link(1, server=740))["server"] == 740


async def test_delete_returns_none_on_404(monkeypatch):
    monkeypatch.setattr(mm, "_request", AsyncMock(return_value=(404, None)))
    assert await mm.delete_guild_link(1) is None


async def test_delete_returns_body_on_200(monkeypatch):
    monkeypatch.setattr(mm, "_request", AsyncMock(return_value=(200, {"revoked": True})))
    assert (await mm.delete_guild_link(1))["revoked"] is True


# ── end-to-end round trip against a fake MM ───────────────────────────────────


async def test_create_round_trip_sends_auth_and_string_ids(monkeypatch):
    captured: dict = {}

    async def _handler(request):
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = await request.json()
        body = captured["body"]
        return web.json_response(
            {
                "link_id": "L1",
                "guild_id": body["guild_id"],
                "alliance_id": "A1",
                "alliance_name": body["alliance_name"],
                "server": body["server"],
                "server_grouping_id": None,
                "alliance_created": True,
                "linked_at": "2026-06-23T00:00:00Z",
            },
            status=201,
        )

    app = web.Application()
    app.router.add_post("/api/internal/guild-links", _handler)
    server = TestServer(app)
    await server.start_server()
    try:
        monkeypatch.setenv("MAPMANAGER_API_URL", str(server.make_url("/")).rstrip("/"))
        monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")
        out = await mm.create_guild_link(
            guild_id=123, server=738, alliance_name="Nox", requested_by_discord_id=456
        )
    finally:
        await server.close()

    assert captured["auth"] == "Bearer testkey"
    assert captured["body"]["guild_id"] == "123"  # snowflake sent as a string
    assert captured["body"]["requested_by_discord_id"] == "456"
    assert captured["body"]["server"] == 738  # numeric stays numeric
    assert out["alliance_id"] == "A1"
    assert out["alliance_created"] is True

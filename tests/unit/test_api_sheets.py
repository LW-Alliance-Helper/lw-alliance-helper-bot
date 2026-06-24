"""Tests for the sheet endpoints (6D, #316).

`GET /sheet/growth` is implemented (generic metric pass-through); roster and the
two storm-history routes are still 501 stubs. All require the Bearer key.
"""

from __future__ import annotations

import growth
import pytest
from aiohttp.test_utils import TestClient, TestServer

from api_server import build_app
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}

STUB_ROUTES = [
    ("GET", f"/api/guilds/{TEST_GUILD_ID}/sheet/roster"),
    ("GET", f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-history"),
    ("POST", f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-history"),
]


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


# ── still-stubbed endpoints ───────────────────────────────────────────────────


@pytest.mark.parametrize("method,path", STUB_ROUTES)
async def test_stub_endpoints_return_501(method, path):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.request(method, path, headers=AUTH)
        assert r.status == 501
        body = await r.json()
        assert body["error"] == "not_implemented"
        assert body["target_shape"]


@pytest.mark.parametrize("method,path", STUB_ROUTES)
async def test_stub_endpoints_require_auth(method, path):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.request(method, path)
        assert r.status == 401


# ── growth (implemented) ──────────────────────────────────────────────────────


async def test_growth_returns_series(monkeypatch):
    series = {
        "metrics": ["Power"],
        "snapshots": [{"date": "2026-01-01", "members": 2, "values": {"Power": 3000000}}],
    }
    monkeypatch.setattr(growth, "read_growth_series", lambda gid: series)
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/growth", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == series


async def test_growth_passes_guild_id(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        growth, "read_growth_series", lambda gid: captured.update(gid=gid) or {"metrics": []}
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/growth", headers=AUTH)
    assert captured["gid"] == TEST_GUILD_ID


async def test_growth_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/growth")
        assert r.status == 401


async def test_growth_bad_guild_id():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get("/api/guilds/not-a-number/sheet/growth", headers=AUTH)
        assert r.status == 400

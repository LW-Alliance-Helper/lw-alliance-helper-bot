"""Tests for the sheet-read endpoint stubs (6D, #316).

These are intentionally 501 for now (see api/routes/sheets.py). The tests pin
the current contract: auth is still enforced, and a credentialed call gets a
structured ``not_implemented`` 501 rather than a 404/500. When the endpoints are
implemented against MM's Phase 7 loaders, these assertions are the first thing
to update.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from api_server import build_app
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}

ROUTES = [
    ("GET", f"/api/guilds/{TEST_GUILD_ID}/sheet/roster"),
    ("GET", f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-history"),
    ("POST", f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-history"),
    ("GET", f"/api/guilds/{TEST_GUILD_ID}/sheet/growth"),
]


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


@pytest.mark.parametrize("method,path", ROUTES)
async def test_sheet_endpoints_return_501(method, path):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.request(method, path, headers=AUTH)
        assert r.status == 501
        body = await r.json()
        assert body["error"] == "not_implemented"
        assert body["target_shape"]  # documents the eventual shape


@pytest.mark.parametrize("method,path", ROUTES)
async def test_sheet_endpoints_require_auth(method, path):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.request(method, path)
        assert r.status == 401

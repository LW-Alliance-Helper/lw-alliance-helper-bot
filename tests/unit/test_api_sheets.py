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


# ── per-member growth history (Phase 8) ───────────────────────────────────────


async def test_member_history_resolves_names_and_returns_series(monkeypatch):
    import member_roster

    monkeypatch.setattr(
        member_roster,
        "read_roster_members",
        lambda gid: [
            {"discord_id": "123", "name": "ada", "display_name": "Ada", "joined_at": None}
        ],
    )
    captured = {}
    history = {"metrics": {"Power": [{"at": "2026-01-01", "value": 1000000}]}}
    monkeypatch.setattr(
        growth,
        "read_member_history",
        lambda gid, name_keys: captured.update(keys=name_keys) or history,
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/123/history", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == history
    assert captured["keys"] == {"ada"}  # display + username both lowercase to "ada"


async def test_member_history_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/123/history")
        assert r.status == 401


async def test_member_history_bad_guild_id():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get("/api/guilds/xx/members/123/history", headers=AUTH)
        assert r.status == 400


# ── zone-rules (Phase 8, display-only strategy guideline) ─────────────────────


def test_zone_rules_for_extracts_configured_floors(monkeypatch):
    from types import SimpleNamespace

    import storm_strategy

    monkeypatch.setattr(storm_strategy, "list_presets", lambda gid, et: ["Default"])
    zones = [
        storm_strategy.ZoneRow(zone="Nuclear Silo", min_power_a=1500000, min_power_b=1200000),
        storm_strategy.ZoneRow(zone="Arsenal", min_power_a=0, min_power_b=0),  # no floor -> omit
        storm_strategy.ZoneRow(zone="Science Hub", min_power_a=0, min_power_b=800000),  # one team
    ]
    monkeypatch.setattr(
        storm_strategy, "load_preset", lambda gid, et, name: SimpleNamespace(zones=zones)
    )
    # PHASE8 §4 pinned shape: { zone, min_power } only (min_power = max of the
    # per-team floors); Arsenal has no floor so it's omitted.
    assert storm_strategy.zone_rules_for(123, "DS") == [
        {"zone": "Nuclear Silo", "min_power": 1500000},
        {"zone": "Science Hub", "min_power": 800000},
    ]


def test_zone_rules_for_empty_when_no_presets(monkeypatch):
    import storm_strategy

    monkeypatch.setattr(storm_strategy, "list_presets", lambda gid, et: [])
    assert storm_strategy.zone_rules_for(123, "DS") == []


async def test_zone_rules_endpoint_returns_rules(monkeypatch):
    import storm_strategy

    rules = [{"zone": "Nuclear Silo", "min_power": 1500000}]
    monkeypatch.setattr(storm_strategy, "zone_rules_for", lambda gid, et: rules)
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/zone-rules?event_type=ds", headers=AUTH
        )
        assert r.status == 200
        assert (await r.json()) == {"rules": rules}


async def test_zone_rules_bad_event_type():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/zone-rules?event_type=xx", headers=AUTH
        )
        assert r.status == 400


async def test_zone_rules_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/storm/zone-rules?event_type=ds")
        assert r.status == 401

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


# ── named strategies + zone-rules (PHASE8 §4, display-only planner overlay) ───


def test_list_strategies_maps_preset_names(monkeypatch):
    import storm_strategy

    monkeypatch.setattr(storm_strategy, "list_presets", lambda gid, et: ["Standard", "Aggressive"])
    # The bot keys presets by name, so id == name.
    assert storm_strategy.list_strategies(123, "DS") == [
        {"id": "Standard", "name": "Standard"},
        {"id": "Aggressive", "name": "Aggressive"},
    ]


def test_list_strategies_empty(monkeypatch):
    import storm_strategy

    monkeypatch.setattr(storm_strategy, "list_presets", lambda gid, et: [])
    assert storm_strategy.list_strategies(123, "DS") == []


def test_zone_rules_for_returns_full_rule_shape(monkeypatch):
    from types import SimpleNamespace

    import storm_strategy

    monkeypatch.setattr(storm_strategy, "list_presets", lambda gid, et: ["Default"])
    zones = [
        storm_strategy.ZoneRow(
            zone="Nuclear Silo",
            min_power_a=1500000,
            min_power_b=1200000,
            max_players=4,
            priority=1,
        ),
        storm_strategy.ZoneRow(zone="Arsenal"),  # no rule at all -> omitted
        # phase-aware zone: flat max/priority are 0, fall back to phase fields.
        storm_strategy.ZoneRow(
            zone="Science Hub",
            min_power_b=800000,
            max_phase1=3,
            priority_phase2=2,
        ),
    ]
    monkeypatch.setattr(
        storm_strategy, "load_preset", lambda gid, et, name: SimpleNamespace(zones=zones)
    )
    assert storm_strategy.zone_rules_for(123, "DS") == [
        {
            "zone": "Nuclear Silo",
            "min_a": 1500000,
            "min_b": 1200000,
            "min_players": 0,
            "max_players": 4,
            "priority": 1,
        },
        {
            "zone": "Science Hub",
            "min_a": 0,
            "min_b": 800000,
            "min_players": 0,
            "max_players": 3,  # fell back to max_phase1
            "priority": 2,  # fell back to priority_phase2
        },
    ]


def test_zone_rules_for_resolves_strategy_id(monkeypatch):
    from types import SimpleNamespace

    import storm_strategy

    captured = {}

    def fake_load(gid, et, name):
        captured["name"] = name
        return SimpleNamespace(zones=[])

    # strategy_id given -> load that preset directly, never touch list_presets.
    monkeypatch.setattr(
        storm_strategy, "list_presets", lambda gid, et: (_ for _ in ()).throw(AssertionError())
    )
    monkeypatch.setattr(storm_strategy, "load_preset", fake_load)
    storm_strategy.zone_rules_for(123, "DS", "Aggressive")
    assert captured["name"] == "Aggressive"


def test_zone_rules_for_empty_when_no_presets(monkeypatch):
    import storm_strategy

    monkeypatch.setattr(storm_strategy, "list_presets", lambda gid, et: [])
    assert storm_strategy.zone_rules_for(123, "DS") == []


async def test_strategies_endpoint_returns_list(monkeypatch):
    import storm_strategy

    strategies = [{"id": "Standard", "name": "Standard"}]
    monkeypatch.setattr(storm_strategy, "list_strategies", lambda gid, et: strategies)
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/strategies?event_type=ds", headers=AUTH
        )
        assert r.status == 200
        assert (await r.json()) == {"strategies": strategies}


async def test_strategies_bad_event_type():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/strategies?event_type=xx", headers=AUTH
        )
        assert r.status == 400


async def test_strategies_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/storm/strategies?event_type=ds")
        assert r.status == 401


async def test_zone_rules_endpoint_passes_strategy_id(monkeypatch):
    import storm_strategy

    captured = {}
    rules = [
        {
            "zone": "Nuclear Silo",
            "min_a": 1500000,
            "min_b": 0,
            "min_players": 0,
            "max_players": 4,
            "priority": 1,
        }
    ]
    monkeypatch.setattr(
        storm_strategy,
        "zone_rules_for",
        lambda gid, et, sid: captured.update(gid=gid, et=et, sid=sid) or rules,
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/zone-rules?event_type=ds&strategy_id=Aggressive",
            headers=AUTH,
        )
        assert r.status == 200
        assert (await r.json()) == {"rules": rules}
    assert captured == {"gid": TEST_GUILD_ID, "et": "DS", "sid": "Aggressive"}


async def test_zone_rules_endpoint_strategy_id_optional(monkeypatch):
    import storm_strategy

    captured = {}
    monkeypatch.setattr(
        storm_strategy, "zone_rules_for", lambda gid, et, sid: captured.update(sid=sid) or []
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/zone-rules?event_type=ds", headers=AUTH
        )
    assert captured["sid"] is None


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

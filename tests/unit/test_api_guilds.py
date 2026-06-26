"""End-to-end tests for the guild endpoints MM calls (6D, #316).

Runs the real aiohttp app via ``TestServer`` so routing, the Bearer gate, and
match-info parsing are all exercised. ``link`` reads config + premium; ``members``
reads the (faked) gateway cache and maps roles to MM tiers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import config
import premium
from api_server import build_app
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


def _member(display_name, role_names):
    return SimpleNamespace(
        display_name=display_name,
        roles=[SimpleNamespace(name=n) for n in role_names],
    )


def _bot_with_member(guild_id, member):
    guild = SimpleNamespace(get_member=lambda uid: member)
    return SimpleNamespace(get_guild=lambda gid: guild if gid == guild_id else None)


# ── /link ─────────────────────────────────────────────────────────────────────


async def test_link_401_without_key():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/link")
        assert r.status == 401


async def test_link_400_on_bad_guild_id(temp_db):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get("/api/guilds/not-a-number/link", headers=AUTH)
        assert r.status == 400


async def test_link_404_when_unlinked(temp_db):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/link", headers=AUTH)
        assert r.status == 404
        assert (await r.json())["error"] == "not_linked"


async def test_link_200_with_mapping(seeded_db, monkeypatch):
    config.save_guild_alliance_mapping(TEST_GUILD_ID, "Nox", 738, "A1", "G1")
    monkeypatch.setenv("FORCE_PREMIUM", "1")  # premium=True without entitlement lookups
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/link", headers=AUTH)
        assert r.status == 200
        body = await r.json()
        assert body["guild_id"] == str(TEST_GUILD_ID)
        assert body["alliance_id"] == "A1"
        assert body["alliance_name"] == "Nox"
        assert body["server"] == 738
        assert body["server_grouping_id"] == "G1"
        assert body["sheet_id"]  # seeded_db sets a spreadsheet_id
        assert body["premium"] is True


async def test_link_premium_false_without_subscription(seeded_db, monkeypatch):
    config.save_guild_alliance_mapping(TEST_GUILD_ID, "Nox", 738, "A1", None)
    monkeypatch.delenv("FORCE_PREMIUM", raising=False)
    monkeypatch.delenv("PREMIUM_BYPASS_GUILD_IDS", raising=False)
    premium.clear_cache()
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/link", headers=AUTH)
        body = await r.json()
        assert body["premium"] is False
        assert body["server_grouping_id"] is None


# ── /members ──────────────────────────────────────────────────────────────────


async def test_member_in_guild_maps_both_tiers(seeded_db):
    member = _member("Coriander", ["Leadership", "Member", "Unrelated"])
    bot = _bot_with_member(TEST_GUILD_ID, member)
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/123", headers=AUTH)
        assert r.status == 200
        body = await r.json()
        assert body["in_guild"] is True
        assert body["display_name"] == "Coriander"
        assert set(body["roles"]) == {"leadership", "member"}


async def test_member_only_member_role(seeded_db):
    bot = _bot_with_member(TEST_GUILD_ID, _member("Basil", ["Member"]))
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/123", headers=AUTH)
        assert (await r.json())["roles"] == ["member"]


async def test_member_not_in_guild(seeded_db):
    guild = SimpleNamespace(get_member=lambda uid: None)
    bot = SimpleNamespace(get_guild=lambda gid: guild)
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/123", headers=AUTH)
        body = await r.json()
        assert body == {"in_guild": False, "roles": [], "display_name": None}


async def test_member_bot_not_in_guild(seeded_db):
    bot = SimpleNamespace(get_guild=lambda gid: None)
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/123", headers=AUTH)
        assert (await r.json())["in_guild"] is False


async def test_member_bad_user_id(seeded_db):
    bot = _bot_with_member(TEST_GUILD_ID, _member("X", []))
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/not-a-number", headers=AUTH)
        assert r.status == 400


# ── /config (Settings: verify bot setup) ──────────────────────────────────────


async def test_config_returns_roles_and_tabs(seeded_db):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/config", headers=AUTH)
        assert r.status == 200
        body = await r.json()
    assert body["roles"] == [
        {"tier": "leadership", "discordRoles": ["Leadership"]},
        {"tier": "member", "discordRoles": ["Member"]},
    ]
    features = {t["feature"] for t in body["tabs"]}
    assert {"Member Roster", "Growth", "Desert Storm roster", "Canyon Storm roster"} <= features


async def test_config_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/config")
        assert r.status == 401


# ── /storm/votes (planner candidate roster) ───────────────────────────────────


async def test_storm_votes_maps_rows_and_resolves_names(monkeypatch):
    monkeypatch.setattr(
        config,
        "get_storm_signups",
        lambda gid, et, date: [
            {"target_member_id": "123", "vote": "a"},
            {"target_member_id": "456", "vote": "cannot"},  # not in gateway cache
            {"target_member_id": "", "vote": "either"},  # no id
        ],
    )
    guild = SimpleNamespace(
        get_member=lambda uid: SimpleNamespace(display_name="Ada") if uid == 123 else None
    )
    bot = SimpleNamespace(get_guild=lambda gid: guild)
    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/votes?event_type=ds&date=2026-06-27", headers=AUTH
        )
        assert r.status == 200
        body = await r.json()
    assert body == [
        {"member_name": "Ada", "discord_id": "123", "vote": "a"},
        {"member_name": "", "discord_id": "456", "vote": "cannot"},
        {"member_name": "", "discord_id": None, "vote": "either"},
    ]


async def test_storm_votes_bad_event_type():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/votes?event_type=xx&date=2026-06-27", headers=AUTH
        )
        assert r.status == 400


async def test_storm_votes_bad_date():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/votes?event_type=ds&date=nope", headers=AUTH
        )
        assert r.status == 400


async def test_storm_votes_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/votes?event_type=ds&date=2026-06-27"
        )
        assert r.status == 401

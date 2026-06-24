"""Tests for GET /sheet/roster and its parser (6D, #316).

`parse_roster_rows` is the pure sheet→identity mapping; the endpoint adds tier
roles (gateway), the power map (growth metrics), and storm attendance, then
serves an ETag with If-None-Match support.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import growth
import member_roster
import member_stats
from api_server import build_app
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}
RCFG = {"discord_id_col": 0, "name_col": 1, "display_col": 2, "joined_col": 3}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


@pytest.fixture(autouse=True)
def _no_real_sheets(monkeypatch):
    """Default the three enrichment readers to empty so endpoint tests never hit
    a real sheet; individual tests override as needed."""
    monkeypatch.setattr(member_roster, "read_roster_members", lambda gid: [])
    monkeypatch.setattr(growth, "read_member_power_map", lambda gid: {})
    monkeypatch.setattr(member_stats, "read_storm_attendance_map", lambda gid: {})


def _member(role_names):
    return SimpleNamespace(display_name="X", roles=[SimpleNamespace(name=n) for n in role_names])


# ── parse_roster_rows (pure) ──────────────────────────────────────────────────


def test_parse_empty_and_header_only():
    assert member_roster.parse_roster_rows(RCFG, []) == []
    assert (
        member_roster.parse_roster_rows(RCFG, [["Discord ID", "Name", "Display", "Joined"]]) == []
    )


def test_parse_identity_rows():
    values = [
        ["Discord ID", "Name", "Display", "Joined"],
        ["123", "ada", "Ada", "2026-01-15"],
        ["", "manual", "", ""],  # hand-typed non-Discord member
        ["notanid", "bo", "Bo", ""],  # non-numeric id → None
    ]
    out = member_roster.parse_roster_rows(RCFG, values)
    assert out[0] == {
        "discord_id": "123",
        "name": "ada",
        "display_name": "Ada",
        "joined_at": "2026-01-15",
    }
    assert out[1] == {
        "discord_id": None,
        "name": "manual",
        "display_name": None,
        "joined_at": None,
    }
    assert out[2]["discord_id"] is None and out[2]["display_name"] == "Bo"


def test_parse_skips_rows_without_name_or_display():
    values = [["Discord ID", "Name", "Display", "Joined"], ["123", "", "", ""]]
    assert member_roster.parse_roster_rows(RCFG, values) == []


# ── endpoint ──────────────────────────────────────────────────────────────────


async def test_roster_returns_members_with_enrichment(seeded_db, monkeypatch):
    monkeypatch.setattr(
        member_roster,
        "read_roster_members",
        lambda gid: [
            {"discord_id": "123", "name": "ada", "display_name": "Ada", "joined_at": "2026-01-15"},
            {"discord_id": None, "name": "manual", "display_name": None, "joined_at": None},
        ],
    )
    monkeypatch.setattr(
        growth,
        "read_member_power_map",
        lambda gid: {"ada": {"Total Hero": 1000000, "1st Squad": 500000}},
    )
    monkeypatch.setattr(member_stats, "read_storm_attendance_map", lambda gid: {"ada": 92})

    member = _member(["Leadership", "Member"])
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 123 else None)
    bot = SimpleNamespace(get_guild=lambda gid: guild)

    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/roster", headers=AUTH)
        assert r.status == 200
        assert r.headers.get("ETag")
        body = await r.json()

    ada = body[0]
    assert ada["name"] == "ada"
    assert ada["joined_at"] == "2026-01-15"
    assert set(ada["roles"]) == {"leadership", "member"}
    assert ada["power"] == {"Total Hero": 1000000, "1st Squad": 500000}
    assert ada["attendance"] == 92
    # hero_level / last_seen were dropped from the schema
    assert "hero_level" not in ada and "last_seen" not in ada

    manual = body[1]
    assert manual["discord_id"] is None
    assert manual["roles"] == []
    assert manual["power"] == {}  # no growth data → empty map
    assert manual["attendance"] is None


async def test_roster_empty_when_unconfigured(seeded_db):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/roster", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == []


async def test_roster_conditional_get_304(seeded_db):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        first = await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/roster", headers=AUTH)
        etag = first.headers["ETag"]
        again = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/sheet/roster",
            headers={**AUTH, "If-None-Match": etag},
        )
        assert again.status == 304


async def test_roster_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/roster")
        assert r.status == 401


async def test_roster_bad_guild_id():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get("/api/guilds/not-a-number/sheet/roster", headers=AUTH)
        assert r.status == 400

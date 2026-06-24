"""Tests for GET /sheet/roster and its parser (6D, #316).

`parse_roster_rows` is the pure sheet→identity mapping; the endpoint adds tier
roles from the (faked) gateway, nulls the stat fields, and serves an ETag with
If-None-Match support.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

import member_roster
from api_server import build_app
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}
RCFG = {"discord_id_col": 0, "name_col": 1, "display_col": 2, "joined_col": 3}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


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


async def test_roster_returns_members_with_tiers(seeded_db, monkeypatch):
    monkeypatch.setattr(
        member_roster,
        "read_roster_members",
        lambda gid: [
            {"discord_id": "123", "name": "ada", "display_name": "Ada", "joined_at": "2026-01-15"},
            {"discord_id": None, "name": "manual", "display_name": None, "joined_at": None},
        ],
    )
    member = _member(["Leadership", "Member"])
    guild = SimpleNamespace(get_member=lambda uid: member if uid == 123 else None)
    bot = SimpleNamespace(get_guild=lambda gid: guild)

    async with TestClient(TestServer(build_app(bot=bot))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/roster", headers=AUTH)
        assert r.status == 200
        assert r.headers.get("ETag")
        body = await r.json()

    assert body[0]["name"] == "ada"
    assert body[0]["joined_at"] == "2026-01-15"
    assert set(body[0]["roles"]) == {"leadership", "member"}
    # stat fields are null pending enrichment
    for field in ("power", "attendance", "hero_level", "last_seen"):
        assert body[0][field] is None
    # non-Discord member: no tier roles
    assert body[1]["discord_id"] is None
    assert body[1]["roles"] == []


async def test_roster_empty_when_unconfigured(seeded_db, monkeypatch):
    monkeypatch.setattr(member_roster, "read_roster_members", lambda gid: [])
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/sheet/roster", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == []


async def test_roster_conditional_get_304(seeded_db, monkeypatch):
    monkeypatch.setattr(member_roster, "read_roster_members", lambda gid: [])
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

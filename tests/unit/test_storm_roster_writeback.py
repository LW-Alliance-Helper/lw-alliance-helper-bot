"""Tests for the storm-roster write-back (#316, handoff §6.1).

The pure row mapping (`assignment_to_fields` / `build_rows`) and the
`POST /sheet/storm-roster` endpoint (auth, body validation, dispatch). The
gspread write itself is mocked at the `write_mm_storm_roster` boundary.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

import storm_roster_writeback as wb
from api_server import build_app
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}
# Power index keyed by discord_id (common case) or roster name.
POWER = {"123": {"power": 5000000}, "Ada": {"power": 4000000}, "Bo": {"power": None}}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


# ── pure mapping ──────────────────────────────────────────────────────────────


def test_cs_primary_fields():
    a = {
        "member_name": "Ada",
        "discord_id": "123",
        "team": "A",
        "role": "primary",
        "zone": "Power Tower",
        "stage": 1,
    }
    assert wb.assignment_to_fields("2026-06-27", a, POWER) == {
        "Event Date": "2026-06-27",
        "Team": "A",
        "Stage": "1",
        "Zone": "Power Tower",
        "Member": "Ada",
        "Role": "primary",
        "Power at Assignment": "5000000",  # by discord_id
        "Discord ID": "123",
    }


def test_sub_omits_zone_and_stage():
    a = {"member_name": "Bo", "discord_id": None, "team": "B", "role": "sub"}
    f = wb.assignment_to_fields("2026-06-27", a, POWER)
    assert f["Zone"] == "" and f["Stage"] == "" and f["Role"] == "sub"
    assert f["Discord ID"] == ""
    assert f["Power at Assignment"] == ""  # Bo's power is None -> blank


def test_ds_primary_has_no_stage():
    a = {
        "member_name": "Ada",
        "discord_id": "123",
        "team": "A",
        "role": "primary",
        "zone": "Arsenal",
    }
    assert wb.assignment_to_fields("2026-06-27", a, POWER)["Stage"] == ""


def test_power_by_name_when_no_discord_id():
    a = {
        "member_name": "Ada",
        "discord_id": None,
        "team": "A",
        "role": "primary",
        "zone": "Arsenal",
    }
    assert wb.assignment_to_fields("2026-06-27", a, POWER)["Power at Assignment"] == "4000000"


def test_build_rows_aligns_to_header_and_blanks_unknown():
    header = ["Member", "Team", "Zone", "Discord ID", "Override Below Minimum"]
    a = {
        "member_name": "Ada",
        "discord_id": "123",
        "team": "A",
        "role": "primary",
        "zone": "Arsenal",
    }
    # arbitrary header order honored; the unknown officer column is left blank
    assert wb.build_rows(header, "2026-06-27", [a], POWER) == [["Ada", "A", "Arsenal", "123", ""]]


# ── endpoint ──────────────────────────────────────────────────────────────────


def _valid_body():
    return {
        "event_type": "cs",
        "event_date": "2026-06-27",
        "assignments": [
            {
                "member_name": "Ada",
                "discord_id": "123",
                "team": "A",
                "role": "primary",
                "zone": "Power Tower",
                "stage": 1,
            },
            {"member_name": "Bo", "discord_id": None, "team": "B", "role": "sub"},
        ],
    }


async def test_endpoint_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-roster", json=_valid_body())
        assert r.status == 401


async def test_endpoint_bad_guild_id():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post("/api/guilds/xx/sheet/storm-roster", headers=AUTH, json=_valid_body())
        assert r.status == 400


async def test_endpoint_bad_json():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(
            f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-roster", headers=AUTH, data="not json"
        )
        assert r.status == 400


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b.update(event_type="xx"),
        lambda b: b.update(event_date="2026/06/27"),
        lambda b: b.update(assignments="nope"),
    ],
)
async def test_endpoint_validation_400(mutate):
    body = _valid_body()
    mutate(body)
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(
            f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-roster", headers=AUTH, json=body
        )
        assert r.status == 400


async def test_endpoint_success(monkeypatch):
    monkeypatch.setattr(wb, "write_mm_storm_roster", lambda *a, **k: {"written": True, "rows": 2})
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(
            f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-roster", headers=AUTH, json=_valid_body()
        )
        assert r.status == 200
        assert (await r.json()) == {"written": True, "rows": 2}


async def test_endpoint_skipped_when_date_has_data(monkeypatch):
    monkeypatch.setattr(
        wb,
        "write_mm_storm_roster",
        lambda *a, **k: {"written": False, "rows": 0, "skipped_reason": "date_has_data"},
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(
            f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-roster", headers=AUTH, json=_valid_body()
        )
        body = await r.json()
        assert body["written"] is False and body["skipped_reason"] == "date_has_data"


async def test_endpoint_passes_overwrite_flag(monkeypatch):
    captured = {}

    def _fake(guild_id, event_type, event_date, assignments, *, overwrite=False):
        captured.update(overwrite=overwrite, event_type=event_type, n=len(assignments))
        return {"written": True, "rows": len(assignments)}

    monkeypatch.setattr(wb, "write_mm_storm_roster", _fake)
    body = _valid_body()
    body["overwrite"] = True
    async with TestClient(TestServer(build_app(bot=None))) as client:
        await client.post(
            f"/api/guilds/{TEST_GUILD_ID}/sheet/storm-roster", headers=AUTH, json=body
        )
    assert captured == {"overwrite": True, "event_type": "cs", "n": 2}

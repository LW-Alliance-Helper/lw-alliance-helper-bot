"""Tests for the OCR write-back endpoints (handoff §6.2 / §6.3, #316).

`POST /sheet/roster` (member-add) and `POST /sheet/power` (power upsert) merge
parsed-screenshot data into the Sheet without clobbering. Covers the merge logic
directly (fake worksheet) plus the route shape (auth / validation / passthrough).
"""

from __future__ import annotations

import growth
import member_roster
import pytest
from aiohttp.test_utils import TestClient, TestServer
from types import SimpleNamespace

from api_server import build_app
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}

ROSTER_HEADER = [
    "Discord ID",
    "Name",
    "Display Name",
    "Joined",
    "Roles",
    "Is this user in Discord?",
]


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


class FakeWS:
    """A gspread-worksheet stand-in capturing writes."""

    def __init__(self, values):
        self.values = [list(r) for r in values]
        self.appended: list[list] = []
        self.updates: list[dict] = []
        self.header_updates: list[tuple] = []

    def get_all_values(self):
        return [list(r) for r in self.values]

    def append_rows(self, rows, value_input_option=None):
        for r in rows:
            self.appended.append(list(r))
            self.values.append(list(r))

    def update(self, rng, vals, value_input_option=None):
        self.header_updates.append((rng, [list(v) for v in vals]))
        if rng == "A1":
            if self.values:
                self.values[0] = list(vals[0])
            else:
                self.values = [list(vals[0])]

    def batch_update(self, updates, value_input_option=None):
        self.updates.extend(updates)


# ── member_roster.add_ocr_members (§6.3) ──────────────────────────────────────


def _roster_cfg(**over):
    cfg = {
        "enabled": 1,
        "tab_name": "Member Roster",
        "discord_id_col": 0,
        "name_col": 1,
        "display_col": 2,
        "joined_col": 3,
        "roles_col": 4,
    }
    cfg.update(over)
    return cfg


def test_add_ocr_members_appends_new_skips_existing(monkeypatch):
    import config

    ws = FakeWS([ROSTER_HEADER, ["111", "Ada", "Ada", "2026-01-01", "Member", "Yes"]])
    monkeypatch.setattr(config, "get_member_roster_config", lambda gid: _roster_cfg())
    monkeypatch.setattr(config, "get_member_roster_sheet", lambda gid, tab: ws)

    result = member_roster.add_ocr_members(
        TEST_GUILD_ID,
        [{"name": "Ada", "discord_id": None}, {"name": "Bo", "discord_id": None}],
    )
    # Ada already on the roster (by name) -> skipped; Bo appended as a non-Discord
    # row (name in Name + Display Name, presence "No").
    assert result == {"written": True, "rows": 1}
    assert ws.appended == [["", "Bo", "Bo", "", "", "No"]]


def test_add_ocr_members_skips_by_discord_id(monkeypatch):
    import config

    ws = FakeWS([ROSTER_HEADER, ["111", "Ada", "Ada", "", "", "Yes"]])
    monkeypatch.setattr(config, "get_member_roster_config", lambda gid: _roster_cfg())
    monkeypatch.setattr(config, "get_member_roster_sheet", lambda gid, tab: ws)

    # Same person, different name on the screenshot, but MM has the id -> skip.
    result = member_roster.add_ocr_members(TEST_GUILD_ID, [{"name": "Ada2", "discord_id": "111"}])
    assert result == {"written": False, "rows": 0}
    assert ws.appended == []


def test_add_ocr_members_dedupes_within_request(monkeypatch):
    import config

    ws = FakeWS([ROSTER_HEADER])
    monkeypatch.setattr(config, "get_member_roster_config", lambda gid: _roster_cfg())
    monkeypatch.setattr(config, "get_member_roster_sheet", lambda gid, tab: ws)

    result = member_roster.add_ocr_members(
        TEST_GUILD_ID,
        [{"name": "Bo", "discord_id": None}, {"name": "bo", "discord_id": None}],
    )
    assert result == {"written": True, "rows": 1}
    assert ws.appended == [["", "Bo", "Bo", "", "", "No"]]


def test_add_ocr_members_disabled_is_noop(monkeypatch):
    import config

    ws = FakeWS([ROSTER_HEADER])
    monkeypatch.setattr(config, "get_member_roster_config", lambda gid: _roster_cfg(enabled=0))
    monkeypatch.setattr(config, "get_member_roster_sheet", lambda gid, tab: ws)

    assert member_roster.add_ocr_members(TEST_GUILD_ID, [{"name": "Bo", "discord_id": None}]) == {
        "written": False,
        "rows": 0,
    }
    assert ws.appended == []


def test_add_ocr_members_empty_is_noop():
    assert member_roster.add_ocr_members(TEST_GUILD_ID, []) == {"written": False, "rows": 0}


# ── growth.upsert_member_power (§6.2) ─────────────────────────────────────────


def _growth_cfg(**over):
    cfg = {
        "enabled": 1,
        "tab_growth": "Growth Tracking",
        "metrics": [{"col": "B", "label": "Power"}, {"col": "C", "label": "THP"}],
    }
    cfg.update(over)
    return cfg


def _patch_growth(monkeypatch, ws, cfg):
    import config

    monkeypatch.setattr(config, "get_growth_config", lambda gid: cfg)
    monkeypatch.setattr(
        growth, "_get_spreadsheet", lambda gid: SimpleNamespace(worksheet=lambda t: ws)
    )


def test_upsert_power_updates_existing_and_appends(monkeypatch):
    ws = FakeWS([["Name", "Power (Jun 2026)"], ["Ada", "100"], ["Bo", "200"]])
    _patch_growth(monkeypatch, ws, _growth_cfg())

    result = growth.upsert_member_power(
        TEST_GUILD_ID,
        [
            {"name": "Ada", "discord_id": None, "values": {"Power": 150, "Junk": 9}},
            {"name": "Cara", "discord_id": None, "values": {"Power": 50}},
        ],
        period_label="Jun 2026",
    )
    assert result == {"written": True, "rows": 2}
    # Ada (row 2) updated, Cara appended at row 4. "Junk" (not configured) ignored,
    # THP column (not sent) never added.
    assert {"range": "B2", "values": [[150]]} in ws.updates
    assert {"range": "B4", "values": [[50]]} in ws.updates
    assert ws.appended == [["Cara", ""]]
    assert all("THP" not in h for h in ws.values[0])


def test_upsert_power_creates_period_column_when_missing(monkeypatch):
    ws = FakeWS([["Name"]])
    _patch_growth(monkeypatch, ws, _growth_cfg())

    result = growth.upsert_member_power(
        TEST_GUILD_ID,
        [{"name": "Ada", "discord_id": None, "values": {"Power": 100}}],
        period_label="Jun 2026",
    )
    assert result == {"written": True, "rows": 1}
    assert ("A1", [["Name", "Power (Jun 2026)"]]) in ws.header_updates
    assert ws.appended == [["Ada", ""]]
    assert {"range": "B2", "values": [[100]]} in ws.updates


def test_upsert_power_not_configured_is_noop(monkeypatch):
    ws = FakeWS([["Name"]])
    _patch_growth(monkeypatch, ws, _growth_cfg(metrics=[], tab_growth=""))

    assert growth.upsert_member_power(
        TEST_GUILD_ID, [{"name": "Ada", "values": {"Power": 1}}], period_label="Jun 2026"
    ) == {"written": False, "rows": 0}


def test_upsert_power_no_configured_labels_is_noop(monkeypatch):
    ws = FakeWS([["Name", "Power (Jun 2026)"], ["Ada", "100"]])
    _patch_growth(monkeypatch, ws, _growth_cfg())

    assert growth.upsert_member_power(
        TEST_GUILD_ID, [{"name": "Ada", "values": {"Junk": 1}}], period_label="Jun 2026"
    ) == {"written": False, "rows": 0}
    assert ws.updates == []


# ── route shape: POST /sheet/roster + /sheet/power ────────────────────────────


async def test_roster_add_route_passes_through(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        member_roster,
        "add_ocr_members",
        lambda gid, members: (
            captured.update(gid=gid, members=members) or {"written": True, "rows": 2}
        ),
    )
    body = {"members": [{"name": "Ada", "discord_id": None}, {"name": "Bo", "discord_id": None}]}
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(f"/api/guilds/{TEST_GUILD_ID}/sheet/roster", headers=AUTH, json=body)
        assert r.status == 200
        assert (await r.json()) == {"written": True, "rows": 2}
    assert captured["gid"] == TEST_GUILD_ID
    assert captured["members"] == body["members"]


async def test_power_route_passes_through(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        growth,
        "upsert_member_power",
        lambda gid, members: (
            captured.update(gid=gid, members=members) or {"written": True, "rows": 1}
        ),
    )
    body = {"members": [{"name": "Ada", "discord_id": None, "values": {"Power": 100}}]}
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(f"/api/guilds/{TEST_GUILD_ID}/sheet/power", headers=AUTH, json=body)
        assert r.status == 200
        assert (await r.json()) == {"written": True, "rows": 1}
    assert captured["members"] == body["members"]


@pytest.mark.parametrize("path", ["sheet/roster", "sheet/power"])
async def test_writeback_requires_auth(path):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(f"/api/guilds/{TEST_GUILD_ID}/{path}", json={"members": []})
        assert r.status == 401


@pytest.mark.parametrize("path", ["sheet/roster", "sheet/power"])
async def test_writeback_bad_body(path):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(f"/api/guilds/{TEST_GUILD_ID}/{path}", headers=AUTH, json={"x": 1})
        assert r.status == 400
        assert (await r.json())["error"] == "bad_members"


@pytest.mark.parametrize("path", ["sheet/roster", "sheet/power"])
async def test_writeback_bad_guild_id(path):
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.post(f"/api/guilds/xx/{path}", headers=AUTH, json={"members": []})
        assert r.status == 400

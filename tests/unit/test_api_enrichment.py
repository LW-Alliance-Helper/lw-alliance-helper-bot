"""Tests for the enrichment read endpoints (#316).

Surface existing bot features on MM's alliance pages: growth breakdown buckets
(`GET /growth/breakdown`), the full member profile (`GET /members/{id}/stats`),
and storm participation trends (`GET /storm/trends`). Covers the data functions
plus the route shape (auth / validation / passthrough).
"""

from __future__ import annotations

import growth
import member_stats
import pytest
import storm_trends
from aiohttp.test_utils import TestClient, TestServer

from api_server import build_app
from member_stats import Target
from tests.conftest import TEST_GUILD_ID

AUTH = {"Authorization": "Bearer testkey"}


@pytest.fixture(autouse=True)
def _api_key(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "testkey")


# ── growth breakdown ──────────────────────────────────────────────────────────


async def test_breakdown_passes_through(monkeypatch):
    data = {
        "has_data": True,
        "prev_period_label": "Apr 2026",
        "curr_period_label": "May 2026",
        "metric_labels": ["Power"],
        "summary": {"Power": {"increased": ["Ada"], "decline": ["Bo"]}},
    }
    captured = {}
    monkeypatch.setattr(
        growth, "read_latest_breakdown", lambda gid: captured.update(gid=gid) or data
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/growth/breakdown", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == data
    assert captured["gid"] == TEST_GUILD_ID


async def test_breakdown_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/growth/breakdown")
        assert r.status == 401


# ── member profile ────────────────────────────────────────────────────────────


def test_build_member_profile_assembles_sections(monkeypatch):
    target = Target(name="Ada", discord_id=111, joined="2026-01-01")
    monkeypatch.setattr(member_stats, "_power_values", lambda gid, t: {"Power": 100})
    monkeypatch.setattr(
        member_stats,
        "_storm_profile",
        lambda gid, t, *, leadership_view: {"ds": {"attendance": {"pct": 80}}},
    )
    monkeypatch.setattr(member_stats, "_survey_profile", lambda gid, t: [{"survey_name": "S"}])
    monkeypatch.setattr(member_stats, "_train_profile", lambda gid, t: {"conductor_count": 3})

    profile = member_stats.build_member_profile(TEST_GUILD_ID, target, leadership_view=True)
    assert profile["member"] == {
        "name": "Ada",
        "discord_id": "111",
        "joined_at": "2026-01-01",
        "is_manual": False,
    }
    assert profile["power"] == {"Power": 100}
    assert profile["storm"] == {"ds": {"attendance": {"pct": 80}}}
    assert profile["surveys"] == [{"survey_name": "S"}]
    assert profile["train"] == {"conductor_count": 3}


def test_build_member_profile_hides_train_for_member_view(monkeypatch):
    target = Target(name="Ada", discord_id=111, joined="")
    monkeypatch.setattr(member_stats, "_power_values", lambda gid, t: {})
    monkeypatch.setattr(member_stats, "_storm_profile", lambda gid, t, *, leadership_view: {})
    monkeypatch.setattr(member_stats, "_survey_profile", lambda gid, t: [])
    monkeypatch.setattr(member_stats, "_train_profile", lambda gid, t: {"conductor_count": 3})

    profile = member_stats.build_member_profile(TEST_GUILD_ID, target, leadership_view=False)
    assert "train" not in profile
    assert profile["member"]["joined_at"] is None


async def test_member_profile_route_passes_through(monkeypatch):
    target = Target(name="Ada", discord_id=111, joined="")
    profile = {"member": {"name": "Ada"}, "power": {"Power": 1}}
    monkeypatch.setattr(
        member_stats, "resolve_profile_target", lambda gid, did, member=None: target
    )
    monkeypatch.setattr(member_stats, "build_member_profile", lambda gid, t: profile)
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/111/stats", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == profile


async def test_member_profile_404_when_unknown(monkeypatch):
    monkeypatch.setattr(member_stats, "resolve_profile_target", lambda gid, did, member=None: None)
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/111/stats", headers=AUTH)
        assert r.status == 404
        assert (await r.json())["error"] == "member_not_found"


async def test_member_profile_bad_member_id():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/abc/stats", headers=AUTH)
        assert r.status == 400


async def test_member_profile_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/111/stats")
        assert r.status == 401


# ── storm trends ──────────────────────────────────────────────────────────────


def test_member_attendance_summary_computes_rates(monkeypatch):
    import storm_log

    dates = ["2026-05-15", "2026-05-08", "2026-05-01"]
    by_member = {
        "Ada": {"2026-05-15": "yes", "2026-05-08": "yes", "2026-05-01": "no"},
        "Bo": {"2026-05-15": "no", "2026-05-08": "", "2026-05-01": "no"},
    }
    monkeypatch.setattr(
        storm_log, "read_member_log_window", lambda gid, et, lb, qk: (dates, by_member)
    )
    out = storm_trends.member_attendance_summary(TEST_GUILD_ID, "DS", 8)
    assert out["event_type"] == "ds"
    assert out["total_events"] == 3
    # Ada attended 2/3 (67%), Bo 0/3 (0%); sorted by pct desc.
    assert out["members"][0] == {
        "member": "Ada",
        "attended": 2,
        "tracked": 3,
        "attendance_pct": 67,
        "last_attended": "2026-05-15",
    }
    assert out["members"][1]["member"] == "Bo"
    assert out["members"][1]["attendance_pct"] == 0
    assert out["members"][1]["last_attended"] is None


def test_member_attendance_summary_degrades_on_read_error(monkeypatch):
    import storm_log

    def boom(*a, **k):
        raise RuntimeError("sheet down")

    monkeypatch.setattr(storm_log, "read_member_log_window", boom)
    out = storm_trends.member_attendance_summary(TEST_GUILD_ID, "CS", 8)
    assert out == {
        "event_type": "cs",
        "lookback_events": 8,
        "total_events": 0,
        "members": [],
    }


async def test_storm_trends_route_passes_through(monkeypatch):
    data = {"event_type": "ds", "lookback_events": 12, "total_events": 3, "members": []}
    captured = {}
    monkeypatch.setattr(
        storm_trends,
        "member_attendance_summary",
        lambda gid, et, lb: captured.update(gid=gid, et=et, lb=lb) or data,
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/trends?event_type=ds&lookback=5", headers=AUTH
        )
        assert r.status == 200
        assert (await r.json()) == data
    assert captured == {"gid": TEST_GUILD_ID, "et": "DS", "lb": 5}


async def test_storm_trends_clamps_lookback(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        storm_trends,
        "member_attendance_summary",
        lambda gid, et, lb: captured.update(lb=lb) or {"members": []},
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/trends?event_type=cs&lookback=999", headers=AUTH
        )
    assert captured["lb"] == 50  # clamped to max


async def test_storm_trends_bad_event_type():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/storm/trends?event_type=xx", headers=AUTH
        )
        assert r.status == 400


async def test_storm_trends_requires_auth():
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/storm/trends?event_type=ds")
        assert r.status == 401

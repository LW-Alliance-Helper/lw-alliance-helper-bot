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


def _patch_growth_tab(monkeypatch, rows):
    from types import SimpleNamespace

    import config

    gcfg = {
        "metrics": [{"col": "B", "label": "Power"}],
        "tab_growth": "Growth Tracking",
        "breakdown_thresholds": {},
    }
    monkeypatch.setattr(config, "get_growth_config", lambda gid: gcfg)
    ws = SimpleNamespace(get_all_values=lambda: [list(r) for r in rows])
    monkeypatch.setattr(
        growth, "_get_spreadsheet", lambda gid: SimpleNamespace(worksheet=lambda t: ws)
    )


def test_breakdown_for_range_classifies(monkeypatch):
    rows = [
        ["Name", "Power (Apr 2026)", "Power (May 2026)", "Power (Jun 2026)"],
        ["Ada", "1000000", "1100000", "1300000"],  # +30% Apr->Jun -> increased
        ["Bo", "1000000", "1000000", "950000"],  # -5% -> decline
        ["Cy", "1000000", "1050000", "1080000"],  # +8% -> low
    ]
    _patch_growth_tab(monkeypatch, rows)
    out = growth.breakdown_for_range(TEST_GUILD_ID, "Apr 2026", "Jun 2026")
    assert out["has_data"] is True
    assert out["prev_period_label"] == "Apr 2026"
    assert out["curr_period_label"] == "Jun 2026"
    assert out["metric_labels"] == ["Power"]
    assert out["summary"] == {
        "Power": {"increased": ["Ada"], "steady": [], "low": ["Cy"], "none": [], "decline": ["Bo"]}
    }


def test_breakdown_for_range_unknown_period_returns_empty(monkeypatch):
    rows = [["Name", "Power (Apr 2026)", "Power (Jun 2026)"], ["Ada", "1000000", "1300000"]]
    _patch_growth_tab(monkeypatch, rows)
    out = growth.breakdown_for_range(TEST_GUILD_ID, "Jan 2025", "Feb 2025")
    assert out["has_data"] is False
    assert out["summary"] == {}


async def test_breakdown_route_uses_range_when_from_to(monkeypatch):
    captured = {}
    ranged = {
        "has_data": True,
        "prev_period_label": "Apr 2026",
        "curr_period_label": "Jun 2026",
        "metric_labels": ["Power"],
        "summary": {},
    }
    monkeypatch.setattr(
        growth, "breakdown_for_range", lambda gid, f, t: captured.update(f=f, t=t) or ranged
    )
    monkeypatch.setattr(growth, "read_latest_breakdown", lambda gid: {"unexpected": True})
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/growth/breakdown?from=Apr%202026&to=Jun%202026",
            headers=AUTH,
        )
        assert r.status == 200
        assert (await r.json()) == ranged
    assert captured == {"f": "Apr 2026", "t": "Jun 2026"}


async def test_breakdown_route_falls_back_on_unknown_period(monkeypatch):
    latest = {
        "has_data": True,
        "prev_period_label": "May 2026",
        "curr_period_label": "Jun 2026",
        "metric_labels": [],
        "summary": {},
    }
    monkeypatch.setattr(growth, "breakdown_for_range", lambda gid, f, t: {"has_data": False})
    monkeypatch.setattr(growth, "read_latest_breakdown", lambda gid: latest)
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/growth/breakdown?from=Bad&to=Range", headers=AUTH
        )
        assert r.status == 200
        assert (await r.json()) == latest


# ── member profile ────────────────────────────────────────────────────────────


def test_build_member_profile_assembles_sections(monkeypatch):
    target = Target(name="Ada", discord_id=111, joined="2026-01-01")
    monkeypatch.setattr(member_stats, "_power_values", lambda gid, t: {"Power": 100})
    monkeypatch.setattr(
        member_stats,
        "_storm_profile",
        lambda gid, t, *, leadership_view, lookback=None: {"ds": {"attendance": {"pct": 80}}},
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
    monkeypatch.setattr(
        member_stats, "_storm_profile", lambda gid, t, *, leadership_view, lookback=None: {}
    )
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
    monkeypatch.setattr(member_stats, "build_member_profile", lambda gid, t, lookback=None: profile)
    async with TestClient(TestServer(build_app(bot=None))) as client:
        r = await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/111/stats", headers=AUTH)
        assert r.status == 200
        assert (await r.json()) == profile


async def test_member_profile_lookback_clamped_and_passed(monkeypatch):
    target = Target(name="Ada", discord_id=111, joined="")
    captured = {}
    monkeypatch.setattr(
        member_stats, "resolve_profile_target", lambda gid, did, member=None: target
    )
    monkeypatch.setattr(
        member_stats,
        "build_member_profile",
        lambda gid, t, lookback=None: captured.update(lookback=lookback) or {"member": {}},
    )
    async with TestClient(TestServer(build_app(bot=None))) as client:
        # In range -> passed through.
        await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/111/stats?lookback=8", headers=AUTH)
        assert captured["lookback"] == 8
        # Over the cap -> clamped to 50.
        await client.get(
            f"/api/guilds/{TEST_GUILD_ID}/members/111/stats?lookback=999", headers=AUTH
        )
        assert captured["lookback"] == 50
        # Omitted -> None (the bot's default window).
        await client.get(f"/api/guilds/{TEST_GUILD_ID}/members/111/stats", headers=AUTH)
        assert captured["lookback"] is None


def test_storm_profile_threads_lookback_to_readers(monkeypatch):
    target = Target(name="Ada", discord_id=111, joined="")
    seen = {}
    # update() returns None, so each reader reports "no data" and _storm_profile
    # skips unpacking — we only care that the lookback reached every reader.
    monkeypatch.setattr(
        member_stats,
        "_storm_signups_for_member",
        lambda gid, et, did, lb=None: seen.update(signup=lb),
    )
    monkeypatch.setattr(
        member_stats,
        "_storm_attendance_for_member",
        lambda gid, et, name, lb=None: seen.update(attendance=lb),
    )
    monkeypatch.setattr(
        member_stats,
        "_storm_placement_for_member",
        lambda gid, et, did, lb=None: seen.update(placement=lb),
    )
    member_stats._storm_profile(TEST_GUILD_ID, target, leadership_view=True, lookback=10)
    assert seen == {"signup": 10, "attendance": 10, "placement": 10}


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

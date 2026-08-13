"""Champion Duel API tests — access boundaries, degraded modes, revert safety.

Runs the real aiohttp app on an ephemeral loopback port, like
``test_api_server.py``, so the full request → auth → handler → SQLite path is
exercised rather than handlers in isolation.

Every player here is invented. This repo is public and the real Champion Duel
roster and scouting must never land in it, not even as a fixture.
"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

import champion_duel_db as db
from api_server import build_app

P = "/champion-duel/v1"

KEV = {"discord_user_id": "111", "discord_name": "Kevin", "guild_id": "999"}


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    """A throwaway Champion Duel database.

    Must not touch ``config.DB_PATH`` — this feature owns a separate file, and
    a test that crossed the two would hide exactly the coupling the split
    exists to prevent.
    """
    path = str(tmp_path / "champion_duel.sqlite3")
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setenv("CHAMPION_DUEL_DB_PATH", path)
    db.init_db()
    db.import_registrants(
        [
            {
                "name": "[TST]AlphaOne",
                "group": "M",
                "rank": 1,
                "server": "738",
                "alliance": "TST",
                "thp": 300_000_000,
                "fsp": 80.0,
            },
            {"name": "Beta Two", "group": "M", "rank": 2, "server": "738", "alliance": "TST"},
        ]
    )
    return path


@pytest.fixture
async def client(cd_db, monkeypatch):
    monkeypatch.delenv("CHAMPION_DUEL_APP_ORIGIN", raising=False)
    monkeypatch.setenv("CHAMPION_DUEL_APP_URL", "https://example.test/champion-duel-predictor.html")
    monkeypatch.setenv("CHAMPION_DUEL_ADMIN_IDS", "111")
    async with TestClient(TestServer(build_app(None))) as c:
        yield c


def _alpha():
    """The AlphaOne registrant id. Identity is (name, server) now, so scouting
    hangs off a row rather than a bare name."""
    return db.resolve_registrant("AlphaOne", server="738")["id"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


async def _session(can_write=False, user="111", name="Kevin"):
    return db.create_session(user, name, can_write=can_write, writer_guild_id="999")


# ── Open routes ───────────────────────────────────────────────────────────────


async def test_health_reports_capabilities(client):
    body = await (await client.get(f"{P}/health")).json()
    assert body["status"] == "ok"
    # Engine and identity fail independently; health must distinguish them.
    assert "engine" in body and "identity" in body


async def test_groups_open(client):
    body = await (await client.get(f"{P}/groups")).json()
    assert body["groups"] == [{"group": "M", "registrants": 2}]


async def test_anonymous_roster_excludes_scouting(client, cd_db):
    """The registrant list is public LWS data; our scouting is not."""
    db.set_squad(_alpha(), 1, squad_type="Tank", power=1_000, actor=KEV)
    body = await (await client.get(f"{P}/roster?group=M")).json()
    assert body["scouting_included"] is False
    assert all("squads" not in p for p in body["roster"])


async def test_authenticated_roster_includes_scouting(client, cd_db):
    db.set_squad(_alpha(), 1, squad_type="Tank", power=1_000, actor=KEV)
    token = await _session()
    body = await (await client.get(f"{P}/roster?group=M", headers=_auth(token))).json()
    assert body["scouting_included"] is True
    assert any(p.get("squads") for p in body["roster"])


async def test_player_requires_login(client):
    assert (await client.get(f"{P}/player/AlphaOne")).status == 401


# ── Writes ────────────────────────────────────────────────────────────────────


async def test_write_rejected_without_session(client):
    resp = await client.patch(f"{P}/player/AlphaOne/squads", json={"slot": 1, "type": "Tank"})
    assert resp.status == 401


async def test_write_rejected_without_premium(client):
    """Logged in but not Premium is 403, not 401 — different problems, and the
    app shows different things for each."""
    token = await _session(can_write=False)
    resp = await client.patch(
        f"{P}/player/AlphaOne/squads", json={"slot": 1, "type": "Tank"}, headers=_auth(token)
    )
    assert resp.status == 403
    assert (await resp.json())["error"] == "premium_required"


async def test_premium_write_is_attributed(client, cd_db):
    token = await _session(can_write=True)
    resp = await client.patch(
        f"{P}/player/AlphaOne/squads",
        json={"slot": 1, "type": "Tank", "power": 5_000},
        headers=_auth(token),
    )
    assert resp.status == 200
    edits = db.list_edits()["edits"]
    # Attribution comes from the session, never the request body.
    assert all(e["actor_discord_id"] == "111" for e in edits)


async def test_unknown_player_is_refused(client):
    token = await _session(can_write=True)
    resp = await client.patch(
        f"{P}/player/NoSuchPlayer/squads", json={"slot": 1, "type": "Tank"}, headers=_auth(token)
    )
    assert resp.status == 404


async def test_bad_squad_type_refused(client):
    token = await _session(can_write=True)
    resp = await client.patch(
        f"{P}/player/AlphaOne/squads", json={"slot": 1, "type": "Battleship"}, headers=_auth(token)
    )
    assert resp.status == 400


# ── Admin ─────────────────────────────────────────────────────────────────────


async def test_admin_routes_reject_non_admin(client):
    token = await _session(can_write=True, user="222", name="Mer")
    assert (await client.get(f"{P}/admin/edits", headers=_auth(token))).status == 403


async def test_admin_can_list_edits(client, cd_db):
    db.set_squad(_alpha(), 1, squad_type="Tank", actor=KEV)
    token = await _session(user="111")
    body = await (await client.get(f"{P}/admin/edits", headers=_auth(token))).json()
    assert body["total"] >= 1


async def test_stale_revert_returns_409_not_a_clobber(client, cd_db):
    """Two scouts editing one player is normal; the later value usually wins."""
    db.set_squad(_alpha(), 1, squad_type="Tank", actor=KEV)
    first = db.set_squad(_alpha(), 1, squad_type="Missile", actor=KEV)["edit_ids"][0]
    db.set_squad(_alpha(), 1, squad_type="Aircraft", actor=KEV)

    token = await _session(user="111")
    resp = await client.post(f"{P}/admin/edits/{first}/revert", headers=_auth(token))
    assert resp.status == 409
    body = await resp.json()
    assert body["current"] == "Aircraft"

    forced = await client.post(f"{P}/admin/edits/{first}/revert?force=1", headers=_auth(token))
    assert forced.status == 200


async def test_import_never_downgrades_observed_scouting(client, cd_db):
    """A roster refresh must not turn a real sighting back into an estimate."""
    db.set_squad(_alpha(), 1, squad_type="Tank", power=9_999, actor=KEV, source="observed")
    token = await _session(user="111")
    resp = await client.post(
        f"{P}/admin/import",
        json={"registrants": [{"name": "AlphaOne", "group": "M", "thp": 1}]},
        headers=_auth(token),
    )
    assert resp.status == 200
    squad = db.get_player("AlphaOne", server="738", include_scouting=True)["squads"][0]
    assert squad["source"] == "observed" and squad["power"] == 9_999


async def test_import_seeds_roster_squads_and_orders_in_one_call(client, cd_db):
    """The three are one operation: a roster with no squads cannot be
    predicted at all, and orders are meaningless without the registrants they
    hang off. Splitting them would make a half-applied import the normal
    outcome of a dropped connection."""
    token = await _session(user="111")
    resp = await client.post(
        f"{P}/admin/import",
        json={
            "registrants": [{"name": "AlphaOne", "group": "M", "server": "738", "thp": 100}],
            "squads": [
                {
                    "name": "AlphaOne",
                    "server": "738",
                    "slot": slot,
                    "type": t,
                    "power": p,
                    "source": "estimated",
                }
                for slot, (t, p) in enumerate(
                    zip(("Tank", "Missile", "Aircraft"), (40, 30, 20)), start=1
                )
            ],
            "orders": [
                {"name": "AlphaOne", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]}
            ],
        },
        headers=_auth(token),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["squads"]["applied"] == 3
    assert body["orders"]["applied"] == 1

    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    assert len(player["squads"]) == 3
    assert len(player["orders"]) == 1


async def test_import_rejects_a_payload_with_nothing_in_it(client, cd_db):
    """An empty body is a client mistake, not a no-op success."""
    token = await _session(user="111")
    resp = await client.post(f"{P}/admin/import", json={}, headers=_auth(token))
    assert resp.status == 400


async def test_import_rejects_a_non_list_section(client, cd_db):
    token = await _session(user="111")
    resp = await client.post(
        f"{P}/admin/import", json={"squads": {"name": "AlphaOne"}}, headers=_auth(token)
    )
    assert resp.status == 400
    assert "squads" in (await resp.json())["detail"]


# ── Predict ───────────────────────────────────────────────────────────────────


async def test_predict_validates_input(client):
    resp = await client.post(f"{P}/predict", json={"a": [], "b": []})
    # 400 when the engine is present, 503 when it isn't; never a 500, and never
    # a confident number for a matchup nobody asked about.
    assert resp.status in (400, 503)


async def test_predict_degrades_cleanly_without_engine(client, monkeypatch):
    from api.routes import champion_duel as cd

    monkeypatch.setattr(cd, "ENGINE_AVAILABLE", False)
    resp = await client.post(f"{P}/predict", json={"a": [], "b": []})
    assert resp.status == 503
    assert (await resp.json())["error"] == "engine_unavailable"


# ── CORS ──────────────────────────────────────────────────────────────────────


async def test_cors_only_for_the_configured_origin(client):
    ok = await client.get(f"{P}/groups", headers={"Origin": "https://example.test"})
    assert ok.headers.get("Access-Control-Allow-Origin") == "https://example.test"

    other = await client.get(f"{P}/groups", headers={"Origin": "https://evil.test"})
    assert "Access-Control-Allow-Origin" not in other.headers


async def test_map_manager_routes_stay_non_cors(client):
    """The Map Manager contract is server-to-server with a shared secret.
    Making it browser-reachable would widen its exposure for nothing."""
    resp = await client.get("/api/guilds/1/link", headers={"Origin": "https://example.test"})
    assert "Access-Control-Allow-Origin" not in resp.headers


async def test_preflight_advertises_the_write_methods(client):
    resp = await client.options(
        f"{P}/player/AlphaOne/squads", headers={"Origin": "https://example.test"}
    )
    assert resp.status == 204
    assert "PATCH" in resp.headers["Access-Control-Allow-Methods"]


# ── App URL vs origin ─────────────────────────────────────────────────────────


def test_cors_origin_is_derived_from_the_page_url(monkeypatch):
    """A browser sends Origin with no path.

    Comparing it against the full page URL would reject every cross-origin
    call, so the origin is derived rather than configured twice.
    """
    from api import champion_duel_auth as auth

    monkeypatch.delenv("CHAMPION_DUEL_APP_ORIGIN", raising=False)
    monkeypatch.setenv("CHAMPION_DUEL_APP_URL", "https://host.test/champion-duel-predictor.html")
    assert auth.app_origin() == "https://host.test"
    assert auth.app_url().endswith("/champion-duel-predictor.html")


def test_explicit_origin_overrides_and_loses_a_trailing_slash(monkeypatch):
    from api import champion_duel_auth as auth

    monkeypatch.setenv("CHAMPION_DUEL_APP_URL", "https://host.test/app.html")
    monkeypatch.setenv("CHAMPION_DUEL_APP_ORIGIN", "https://other.test/")
    # A trailing slash would never match a browser's Origin header.
    assert auth.app_origin() == "https://other.test"


def test_login_bounce_targets_the_page_not_the_site_root(monkeypatch):
    """The site root is a landing page with no code-redeeming script.

    Bouncing there would strand the one-time code in the URL and read as
    login silently doing nothing.
    """
    from api import champion_duel_auth as auth

    monkeypatch.delenv("CHAMPION_DUEL_APP_ORIGIN", raising=False)
    monkeypatch.setenv("CHAMPION_DUEL_APP_URL", "https://host.test/champion-duel-predictor.html")
    assert auth.app_url() != auth.app_origin()


# ── OAuth config ──────────────────────────────────────────────────────────────


def test_client_id_derives_from_the_running_bot(monkeypatch):
    """Dev and prod are separate Discord apps.

    A hand-copied client id can belong to the wrong one and fails in a way
    that looks like a code bug; the bot's own application id always belongs to
    the app that service is logged in as.
    """
    from unittest.mock import MagicMock

    from api import champion_duel_auth as auth

    monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
    bot = MagicMock()
    bot.application_id = 123456789
    assert auth.client_id(bot) == "123456789"
    assert "DISCORD_CLIENT_ID" not in auth.missing_oauth_env(bot)


def test_explicit_client_id_wins(monkeypatch):
    from unittest.mock import MagicMock

    from api import champion_duel_auth as auth

    monkeypatch.setenv("DISCORD_CLIENT_ID", "999")
    bot = MagicMock()
    bot.application_id = 123
    assert auth.client_id(bot) == "999"


def test_missing_oauth_names_what_is_absent(monkeypatch):
    """A bare oauth_unconfigured makes the deployer check several variables
    across two systems with no idea which one is missing."""
    from api import champion_duel_auth as auth

    monkeypatch.delenv("DISCORD_CLIENT_ID", raising=False)
    monkeypatch.delenv("DISCORD_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("CHAMPION_DUEL_REDIRECT_URI", raising=False)
    missing = auth.missing_oauth_env(None)
    assert missing == [
        "DISCORD_CLIENT_ID",
        "DISCORD_CLIENT_SECRET",
        "CHAMPION_DUEL_REDIRECT_URI",
    ]
    assert auth.oauth_configured(None) is False


async def test_login_503_lists_the_missing_names(client, monkeypatch):
    monkeypatch.delenv("DISCORD_CLIENT_SECRET", raising=False)
    resp = await client.get(f"{P}/auth/login", allow_redirects=False)
    assert resp.status == 503
    body = await resp.json()
    assert body["error"] == "oauth_unconfigured"
    assert "DISCORD_CLIENT_SECRET" in body["missing"]


async def test_health_reports_oauth_state(client):
    body = await (await client.get(f"{P}/health")).json()
    assert "oauth" in body and "oauth_missing" in body
    assert body["admins_configured"] == 1


# ── Sessions ──────────────────────────────────────────────────────────────────


async def test_me_reports_capabilities(client):
    token = await _session(can_write=True, user="111")
    body = await (await client.get(f"{P}/auth/me", headers=_auth(token))).json()
    assert body["authenticated"] is True
    assert body["can_write"] is True and body["can_admin"] is True


async def test_logout_revokes(client):
    token = await _session(can_write=True)
    assert (await client.post(f"{P}/auth/logout", headers=_auth(token))).status == 200
    resp = await client.patch(
        f"{P}/player/AlphaOne/squads", json={"slot": 1, "type": "Tank"}, headers=_auth(token)
    )
    assert resp.status == 401, "revoked session still wrote"

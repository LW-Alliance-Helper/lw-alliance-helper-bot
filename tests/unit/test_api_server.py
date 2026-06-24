"""Unit + end-to-end tests for the bot's internal HTTP API scaffold (6B, #316).

Covers the service-key gate (``api.auth.requires_api_key``), the uncredentialed
``/healthz`` endpoint, and the ``api_server`` assembly/gating helpers. The
end-to-end cases run a real aiohttp ``TestServer`` on an ephemeral loopback
port so the full request → auth → handler path is exercised, not just the
handler in isolation.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from api.auth import requires_api_key
from api.routes.healthz import healthz
from api_server import BOT_KEY, DEFAULT_PORT, api_server_enabled, build_app, _port


# ── Helpers ───────────────────────────────────────────────────────────────────


@requires_api_key
async def _protected(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


# ── Auth decorator (unit, no socket) ──────────────────────────────────────────


async def test_auth_rejects_missing_header(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "secret")
    resp = await _protected(make_mocked_request("GET", "/x"))
    assert resp.status == 401


async def test_auth_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "secret")
    req = make_mocked_request("GET", "/x", headers={"Authorization": "Bearer nope"})
    resp = await _protected(req)
    assert resp.status == 401


async def test_auth_rejects_wrong_scheme(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "secret")
    req = make_mocked_request("GET", "/x", headers={"Authorization": "Basic secret"})
    resp = await _protected(req)
    assert resp.status == 401


async def test_auth_rejects_empty_bearer(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "secret")
    req = make_mocked_request("GET", "/x", headers={"Authorization": "Bearer "})
    resp = await _protected(req)
    assert resp.status == 401


async def test_auth_accepts_valid_token(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "secret")
    req = make_mocked_request("GET", "/x", headers={"Authorization": "Bearer secret"})
    resp = await _protected(req)
    assert resp.status == 200


async def test_auth_unconfigured_returns_503(monkeypatch):
    # No key set: must NOT collapse to "any key works" — it's a server-side
    # misconfiguration, so 503 even when a token is presented.
    monkeypatch.delenv("MAPMANAGER_API_KEY", raising=False)
    req = make_mocked_request("GET", "/x", headers={"Authorization": "Bearer anything"})
    resp = await _protected(req)
    assert resp.status == 503


async def test_auth_blank_key_treated_as_unset(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "   ")
    req = make_mocked_request("GET", "/x", headers={"Authorization": "Bearer x"})
    resp = await _protected(req)
    assert resp.status == 503


# ── healthz (unit) ────────────────────────────────────────────────────────────


async def test_healthz_returns_ok():
    resp = await healthz(make_mocked_request("GET", "/healthz"))
    assert resp.status == 200


# ── api_server helpers ────────────────────────────────────────────────────────


def test_api_server_enabled_reflects_key_or_port(monkeypatch):
    monkeypatch.delenv("MAPMANAGER_API_KEY", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    assert api_server_enabled() is False
    # The integration key enables it.
    monkeypatch.setenv("MAPMANAGER_API_KEY", "k")
    assert api_server_enabled() is True
    # A web deploy (Railway sets PORT) binds even before the key is set, so the
    # port is up for routing + the health check.
    monkeypatch.delenv("MAPMANAGER_API_KEY", raising=False)
    monkeypatch.setenv("PORT", "8080")
    assert api_server_enabled() is True
    # Neither set (local dev) binds nothing.
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setenv("MAPMANAGER_API_KEY", "  ")
    assert api_server_enabled() is False


def test_port_parsing(monkeypatch):
    monkeypatch.delenv("PORT", raising=False)
    assert _port() == DEFAULT_PORT
    monkeypatch.setenv("PORT", "5005")
    assert _port() == 5005
    monkeypatch.setenv("PORT", "not-a-number")
    assert _port() == DEFAULT_PORT


def test_build_app_registers_healthz():
    app = build_app(bot=None)
    paths = {resource.canonical for resource in app.router.resources()}
    assert "/healthz" in paths


def test_build_app_stashes_bot():
    sentinel = object()
    app = build_app(bot=sentinel)
    assert app[BOT_KEY] is sentinel


# ── End-to-end through a real TestServer ──────────────────────────────────────


async def test_healthz_end_to_end():
    app = build_app(bot=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
        body = await resp.json()
        assert body == {"status": "ok"}


async def test_protected_route_end_to_end(monkeypatch):
    monkeypatch.setenv("MAPMANAGER_API_KEY", "topsecret")
    app = web.Application()
    app.router.add_get("/protected", _protected)
    async with TestClient(TestServer(app)) as client:
        unauthed = await client.get("/protected")
        assert unauthed.status == 401

        authed = await client.get("/protected", headers={"Authorization": "Bearer topsecret"})
        assert authed.status == 200
        assert (await authed.json()) == {"ok": True}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))

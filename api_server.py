"""In-process HTTP API server for the Map Manager ↔ bot integration (#316).

The bot runs a small aiohttp application alongside its gateway client so Map
Manager (MM) can read alliance data (roster / storm history / growth) and look
up guild links over HTTP. ``start_api_server(bot)`` is called once from
``bot.on_ready`` and returns the running ``AppRunner`` so the caller keeps a
handle (and so tests can start it against an ephemeral port).

Opt-in: the server only starts when ``MAPMANAGER_API_KEY`` is set, mirroring how
Sentry only initialises with a DSN. Local dev and CI don't bind a port unless
they configure the integration. Bound to ``0.0.0.0:${PORT}`` (default 8080) so
Railway's router can reach it.

Auth: every credentialed route uses ``api.auth.requires_api_key``; ``/healthz``
is uncredentialed. Routes are registered in ``build_app`` — the 6D guild/sheet
endpoints attach there as they land. See the Map Manager repo's
``docs/BOT_INTEGRATION_HANDOFF.md`` for the cross-service contract.

Env vars:
  - ``MAPMANAGER_API_KEY`` — the shared per-environment service key. Gates both
    whether the server starts and the Bearer check on every credentialed route.
  - ``MAPMANAGER_API_URL``  — MM's base URL, used by the *outbound* client the
    ``/map_manager`` commands call (6C); not consumed here.
  - ``PORT``                — the port to bind (default 8080).
"""

from __future__ import annotations

import os

from aiohttp import web

from api.routes.healthz import healthz

DEFAULT_PORT = 8080

# Typed application key for the gateway client stashed on the app. Route
# handlers reach the discord.py bot (member lookups, sheet config) via
# ``request.app[BOT_KEY]`` rather than a module global. Using a ``web.AppKey``
# (instead of a bare string) is aiohttp 3.9+'s recommended, warning-free form.
BOT_KEY: web.AppKey = web.AppKey("bot", object)


def api_server_enabled() -> bool:
    """True when the inbound API server should start.

    Gated on the service key so an environment that hasn't configured the
    integration never binds a port (matches the Sentry-DSN opt-in pattern).
    """
    return bool(os.getenv("MAPMANAGER_API_KEY", "").strip())


def _port() -> int:
    """The port to bind, from ``PORT`` (default 8080); falls back to the
    default on a non-numeric value rather than crashing startup."""
    raw = os.getenv("PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_PORT


def build_app(bot=None) -> web.Application:
    """Construct the aiohttp application and register routes.

    ``bot`` is stashed on the app (``app["bot"]``) so route handlers can reach
    the gateway cache (member lookups, sheet config, etc.) without a module
    global. Passing None is supported for route-shape tests that don't need the
    gateway.
    """
    app = web.Application()
    app[BOT_KEY] = bot
    app.router.add_get("/healthz", healthz)
    return app


async def start_api_server(bot=None) -> web.AppRunner:
    """Start the HTTP server on ``0.0.0.0:${PORT}`` and return the AppRunner.

    Always starts when invoked — callers gate on ``api_server_enabled()`` first
    (tests call this directly against an ephemeral port). The returned runner
    is the shutdown handle; keep it alive (the bot stashes it on the client).
    """
    app = build_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=_port())
    await site.start()
    return runner

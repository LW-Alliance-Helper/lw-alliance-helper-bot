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

from api import BOT_KEY
from api.routes.discord_actions import get_guild_channels, post_image
from api.routes.guilds import (
    get_guild_config,
    get_guild_link,
    get_guild_member,
    get_storm_votes,
)
from api.routes.healthz import healthz
from api.routes.sheets import (
    get_growth_breakdown,
    get_member_history,
    get_member_profile,
    get_storm_trends,
    get_zone_rules,
    sheet_growth,
    sheet_power_upsert,
    sheet_roster,
    sheet_roster_add,
    sheet_storm_history_append,
    sheet_storm_history_get,
    sheet_storm_roster,
)

DEFAULT_PORT = 8080


def api_server_enabled() -> bool:
    """True when the inbound API server should start.

    Starts when the integration is configured (``MAPMANAGER_API_KEY``) OR when
    running as a Railway web service (Railway sets ``PORT``) — so the process
    binds the port for Railway's routing + health check even before the key is
    set. The uncredentialed ``/healthz`` answers the health check; credentialed
    routes return 503 until the key is configured. Local dev (neither var set)
    binds nothing. Pairs with the ``web`` Procfile process type; a Railway
    ``worker`` gets no inbound HTTP routing, so the server would be unreachable.
    """
    return bool(os.getenv("MAPMANAGER_API_KEY", "").strip() or os.getenv("PORT", "").strip())


def _port() -> int:
    """The port to bind, from ``PORT`` (default 8080); falls back to the
    default on a non-numeric value rather than crashing startup."""
    raw = os.getenv("PORT", "").strip()
    if raw.isdigit():
        return int(raw)
    return DEFAULT_PORT


def build_app(bot=None) -> web.Application:
    """Construct the aiohttp application and register routes.

    ``bot`` is stashed on the app under ``BOT_KEY`` so route handlers can reach
    the gateway cache (member lookups, sheet config, etc.) via
    ``request.app[BOT_KEY]`` without a module global. Passing None is supported
    for route-shape tests that don't need the gateway.
    """
    # Body-size limit raised from aiohttp's 1 MB default for the post-image
    # endpoint (a base64 PNG can be several MB); 16 MB covers Discord's 8 MB
    # attachment ceiling plus base64 overhead.
    app = web.Application(client_max_size=16 * 1024 * 1024)
    app[BOT_KEY] = bot
    app.router.add_get("/healthz", healthz)

    # Guild lookups MM calls (6D). `link` resolves a guild to its alliance +
    # sheet + premium state; `members` is the gateway-cache lookup MM uses to
    # derive a signed-in user's bot-linked tier.
    app.router.add_get("/api/guilds/{guild_id}/link", get_guild_link)
    app.router.add_get("/api/guilds/{guild_id}/members/{discord_user_id}", get_guild_member)
    # Settings "verify your bot setup" (read-only config) + planner sign-up votes.
    app.router.add_get("/api/guilds/{guild_id}/config", get_guild_config)
    app.router.add_get("/api/guilds/{guild_id}/storm/votes", get_storm_votes)

    # Sheet-backed reads. roster + growth are implemented; storm-history is a
    # 501 stub (needs OCR-supplied match outcomes) — see api/routes/sheets.py.
    app.router.add_get("/api/guilds/{guild_id}/sheet/roster", sheet_roster)
    app.router.add_get("/api/guilds/{guild_id}/sheet/storm-history", sheet_storm_history_get)
    app.router.add_post("/api/guilds/{guild_id}/sheet/storm-history", sheet_storm_history_append)
    app.router.add_get("/api/guilds/{guild_id}/sheet/growth", sheet_growth)
    # Storm-roster write-back (handoff §6.1): MM's rebuilt planner posts a
    # finished event roster; the bot writes it to rosters_tab if the date is empty.
    app.router.add_post("/api/guilds/{guild_id}/sheet/storm-roster", sheet_storm_roster)

    # Phase 8 "Post to Discord" + per-member history (PHASE8_DISCORD_HANDOFF.md).
    app.router.add_get("/api/guilds/{guild_id}/channels", get_guild_channels)
    app.router.add_post("/api/guilds/{guild_id}/post-image", post_image)
    app.router.add_get(
        "/api/guilds/{guild_id}/members/{discord_user_id}/history", get_member_history
    )
    app.router.add_get("/api/guilds/{guild_id}/storm/zone-rules", get_zone_rules)

    # OCR write-backs (handoff §6.2 / §6.3): MM posts parsed screenshot data; the
    # bot merges into the Sheet without clobbering.
    app.router.add_post("/api/guilds/{guild_id}/sheet/roster", sheet_roster_add)
    app.router.add_post("/api/guilds/{guild_id}/sheet/power", sheet_power_upsert)

    # Enrichment reads (surface bot features on MM's alliance pages): growth
    # breakdown buckets, the full member profile, and storm participation trends.
    app.router.add_get("/api/guilds/{guild_id}/growth/breakdown", get_growth_breakdown)
    app.router.add_get("/api/guilds/{guild_id}/members/{discord_user_id}/stats", get_member_profile)
    app.router.add_get("/api/guilds/{guild_id}/storm/trends", get_storm_trends)
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

"""Uncredentialed liveness endpoint for the bot's internal HTTP API (#316).

Returns 200 with a tiny JSON body and requires **no** service key — this is
what Railway's health check and any uptime monitor hit, so gating it behind the
key would defeat the purpose. It intentionally does no work (no DB, no gateway
lookup) so a green ``/healthz`` means "the HTTP server is accepting requests",
nothing more.
"""

from __future__ import annotations

from aiohttp import web


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})

"""The bot's internal HTTP API package (Map Manager ↔ bot integration, #316).

The bot historically exposed no HTTP surface. This package stands up a small
aiohttp application that Map Manager calls (guild-link lookups and roster /
storm / growth reads) and that answers an uncredentialed health check.

Layout:
  - ``api.auth``          — ``requires_api_key`` service-key gate (Bearer token).
  - ``api.routes.healthz``— uncredentialed liveness endpoint.
  - ``api.routes.guilds`` / ``api.routes.sheets`` — the credentialed read
    endpoints Map Manager consumes (added in 6D).

The application itself is assembled in the top-level ``api_server`` module and
started once from ``bot.on_ready``. See ``docs/BOT_INTEGRATION_HANDOFF.md`` in
the Map Manager repo for the cross-service contract.
"""

from aiohttp import web

# Typed application key for the gateway client stashed on the app. Route handlers
# reach the discord.py bot (member lookups, sheet config) via
# ``request.app[BOT_KEY]`` rather than a module global. Defined here (not in
# ``api_server``) so route modules can import it without an import cycle back
# through the app assembler. Using a ``web.AppKey`` is aiohttp 3.9+'s
# recommended, warning-free form.
BOT_KEY: web.AppKey = web.AppKey("bot", object)

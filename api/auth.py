"""Service-key auth for the bot's internal HTTP API (#316).

Every credentialed call between Map Manager (MM) and the bot presents the
per-environment service key as ``Authorization: Bearer <token>``. The bot
stores that token in the ``MAPMANAGER_API_KEY`` env var; MM presents the same
token on its inbound calls (the same key is used in both directions per
``docs/BOT_INTEGRATION_HANDOFF.md``). ``requires_api_key`` wraps an aiohttp
handler and rejects anything without a matching token.

The compare is constant-time (``hmac.compare_digest``) so the validity of a
guess can't be inferred from response timing. When ``MAPMANAGER_API_KEY`` is
unset the gate returns 503 (service misconfigured) rather than allowing the
call — "no key configured" must never collapse into "any key works".
"""

from __future__ import annotations

import hmac
import os
from functools import wraps

from aiohttp import web


def _expected_key() -> str | None:
    """The configured service key, or None when unset/blank."""
    key = os.getenv("MAPMANAGER_API_KEY", "").strip()
    return key or None


def _extract_bearer(request: web.Request) -> str | None:
    """Pull the token out of an ``Authorization: Bearer <token>`` header, or
    None when the header is missing or malformed."""
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    token = header[len(prefix) :].strip()
    return token or None


def requires_api_key(handler):
    """Decorator gating an aiohttp handler behind the service key.

    - 503 ``service_unconfigured`` if ``MAPMANAGER_API_KEY`` is unset (the
      server is normally only started when it *is* set, so this is a defensive
      guard against a misconfigured request).
    - 401 ``unauthorized`` when the Bearer token is missing or doesn't match.
    - otherwise the wrapped handler runs.
    """

    @wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        expected = _expected_key()
        if expected is None:
            return web.json_response({"error": "service_unconfigured"}, status=503)
        token = _extract_bearer(request)
        if token is None or not hmac.compare_digest(token, expected):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    return wrapper

"""Outbound HTTP client: bot → Map Manager internal API (6C, #316).

The ``/map_manager`` wizards call Map Manager (MM) to create / read / update /
revoke a guild's alliance link. MM owns the alliance and grouping records; the
bot just drives the link lifecycle and caches the resolved ids locally
(``config.guild_alliance_mappings``).

Config comes from two env vars (the same key is used in both directions, see
``docs/BOT_INTEGRATION_HANDOFF.md``):
  - ``MAPMANAGER_API_URL`` — MM's base URL (e.g. ``https://app-lastwar...``).
  - ``MAPMANAGER_API_KEY`` — the per-environment service key, sent as
    ``Authorization: Bearer <key>``.

Each call opens a short-lived ``aiohttp.ClientSession`` (wizard calls are
low-frequency, so a shared session isn't worth its lifecycle management) with a
10s timeout. Failures raise ``MapManagerError`` so the wizard can show a clear
message; a missing env config raises ``MapManagerNotConfigured``. The endpoint
contract (paths, request/response shapes, status codes) lives in the handoff
doc; this module is the thin typed wrapper over it.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Optional

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=10)


class MapManagerError(Exception):
    """A Map Manager call failed (non-success status, or the service was
    unreachable / timed out). ``status`` is the HTTP status (0 when the call
    never completed); ``code`` is MM's machine-readable error code when present.
    """

    def __init__(self, status: int, message: str, *, code: Optional[str] = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class MapManagerNotConfigured(MapManagerError):
    """``MAPMANAGER_API_URL`` and/or ``MAPMANAGER_API_KEY`` are not set, so the
    integration can't be used. Surfaced to the user as "ask the operator to
    configure the integration", not a transient error they can retry away."""

    def __init__(self) -> None:
        super().__init__(
            0,
            "The Map Manager integration isn't configured on this bot "
            "(MAPMANAGER_API_URL / MAPMANAGER_API_KEY are unset).",
            code="not_configured",
        )


def _base_url() -> Optional[str]:
    url = os.getenv("MAPMANAGER_API_URL", "").strip().rstrip("/")
    return url or None


def _api_key() -> Optional[str]:
    key = os.getenv("MAPMANAGER_API_KEY", "").strip()
    return key or None


def is_configured() -> bool:
    """True when both env vars are set, so the wizard can decide whether to
    even start (and give a clear up-front message if not)."""
    return bool(_base_url() and _api_key())


def alliance_dashboard_url(alliance_id: Optional[str]) -> Optional[str]:
    """Best-effort deep link to an alliance's Map Manager page, or None when the
    base URL or id is missing. The path mirrors MM's documented
    ``alliance.$allianceId`` route; the alliance surfaces themselves are MM
    Phase 7, so until they ship this may land on the sign-in / Alliance Hub."""
    base = _base_url()
    if not base or not alliance_id:
        return None
    return f"{base}/alliance/{alliance_id}"


def _require_config() -> tuple[str, str]:
    base, key = _base_url(), _api_key()
    if not base or not key:
        raise MapManagerNotConfigured()
    return base, key


def _safe_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _error_from(status: int, data: Any) -> MapManagerError:
    code = None
    message = None
    if isinstance(data, dict):
        code = data.get("error") or data.get("code")
        message = data.get("message") or data.get("error")
    return MapManagerError(status, message or f"Map Manager returned HTTP {status}.", code=code)


async def _request(
    method: str,
    path: str,
    *,
    json_body: Optional[dict] = None,
    headers: Optional[dict] = None,
) -> tuple[int, Any]:
    """Make one request to MM and return ``(status, parsed_json_or_None)``.

    Raises ``MapManagerNotConfigured`` if env is missing, or ``MapManagerError``
    with status 0 if MM is unreachable / times out. Non-success HTTP statuses
    are NOT raised here — callers decide which statuses are expected (e.g. 404
    is "no link", not an error).
    """
    base, key = _require_config()
    all_headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    if headers:
        all_headers.update(headers)
    url = f"{base}{path}"
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.request(method, url, json=json_body, headers=all_headers) as resp:
                return resp.status, _safe_json(await resp.text())
    except asyncio.TimeoutError as e:
        raise MapManagerError(0, "Map Manager timed out. Try again in a moment.") from e
    except aiohttp.ClientError as e:
        raise MapManagerError(0, f"Couldn't reach Map Manager: {e}") from e


# ── Guild-link lifecycle ───────────────────────────────────────────────────────


async def create_guild_link(
    guild_id: int,
    server: int,
    alliance_name: str,
    requested_by_discord_id: int,
) -> dict:
    """`POST /api/internal/guild-links` — link a guild to an alliance.

    Returns MM's 201 body (``link_id``, ``alliance_id``, ``server_grouping_id``
    which may be null, ``alliance_created`` bool, etc.). Re-running replaces the
    guild's active link on MM's side. Raises ``MapManagerError`` on 400 / 401 /
    any non-201.
    """
    status, data = await _request(
        "POST",
        "/api/internal/guild-links",
        json_body={
            # Snowflakes are sent as strings: they exceed JS safe-integer range.
            "guild_id": str(guild_id),
            "server": server,
            "alliance_name": alliance_name,
            "requested_by_discord_id": str(requested_by_discord_id),
        },
    )
    if status == 201:
        return data if isinstance(data, dict) else {}
    raise _error_from(status, data)


async def get_guild_link(guild_id: int) -> Optional[dict]:
    """`GET /api/internal/guild-links/:guildId` — read the active link, or None
    (404 means no active link). Raises on other non-200 statuses."""
    status, data = await _request("GET", f"/api/internal/guild-links/{guild_id}")
    if status == 200:
        return data if isinstance(data, dict) else {}
    if status == 404:
        return None
    raise _error_from(status, data)


async def update_guild_link(
    guild_id: int,
    *,
    server: Optional[int] = None,
    alliance_name: Optional[str] = None,
) -> dict:
    """`PATCH /api/internal/guild-links/:guildId` — change the server and/or
    alliance name. At least one field must be provided. Returns MM's updated
    body. Raises on 400 (neither field) / 404 (no active link) / other non-200.
    """
    body: dict = {}
    if server is not None:
        body["server"] = server
    if alliance_name is not None:
        body["alliance_name"] = alliance_name
    if not body:
        raise MapManagerError(0, "Nothing to change: provide a new server or alliance name.")
    status, data = await _request("PATCH", f"/api/internal/guild-links/{guild_id}", json_body=body)
    if status == 200:
        return data if isinstance(data, dict) else {}
    raise _error_from(status, data)


async def delete_guild_link(guild_id: int) -> Optional[dict]:
    """`DELETE /api/internal/guild-links/:guildId` — revoke the link (MM keeps
    the row for audit). Returns MM's body, or None when there was no active link
    to revoke (404). Raises on other non-200 statuses."""
    status, data = await _request("DELETE", f"/api/internal/guild-links/{guild_id}")
    if status == 200:
        return data if isinstance(data, dict) else {}
    if status == 404:
        return None
    raise _error_from(status, data)

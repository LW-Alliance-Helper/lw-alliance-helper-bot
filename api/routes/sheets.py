"""Credentialed sheet-read endpoints Map Manager calls (6D, #316).

These serve the Google-Sheet-backed alliance data MM renders on its alliance
surfaces, plus the OCR write-back append.

Status:
  - **`GET /sheet/growth` — implemented.** Generic pass-through of the alliance's
    configured growth metrics (``growth.read_growth_series``).
  - **`GET /sheet/roster` — implemented.** Identity + tier roles, plus ``power``
    (the alliance's configured growth metrics at their latest snapshot, as a
    label→number map) and ``attendance`` (combined storm attendance %, matching
    ``/member_stats``). hero_level / last_seen are dropped. Carries an ``ETag``
    and honors ``If-None-Match`` (304); power/attendance join by member name.
  - **`GET /sheet/storm-history` — 501 stub.** Needs match-outcome data the bot
    doesn't store yet (it tracks participation/attendance, not results — those
    arrive via the OCR write-back).
  - **`POST /sheet/storm-history` — 501 stub** (OCR append is Phase 8).

The gspread reads are blocking and the API server shares the gateway's event
loop, so reads run via ``asyncio.to_thread``. Target shapes live in the Map
Manager repo's ``docs/BOT_INTEGRATION_HANDOFF.md`` §3.
"""

from __future__ import annotations

import asyncio
import hashlib
import json

from aiohttp import web

from api import BOT_KEY
from api.auth import requires_api_key

_NOT_IMPLEMENTED_DETAIL = (
    "Sheet-backed endpoint not implemented yet. See api/routes/sheets.py for why."
)


def _parse_guild_id(request: web.Request) -> int | None:
    try:
        return int(request.match_info["guild_id"])
    except (KeyError, ValueError):
        return None


def _not_implemented(resource: str, target_shape: str) -> web.Response:
    return web.json_response(
        {
            "error": "not_implemented",
            "resource": resource,
            "target_shape": target_shape,
            "detail": _NOT_IMPLEMENTED_DETAIL,
        },
        status=501,
    )


def _etag_response(payload, request: web.Request) -> web.Response:
    """Serialize ``payload`` with a strong ETag (sha256 of the body) and honor
    ``If-None-Match`` with a 304 so MM can cache the roster cheaply."""
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.sha256(body.encode()).hexdigest()[:32] + '"'
    if request.headers.get("If-None-Match") == etag:
        return web.Response(status=304, headers={"ETag": etag})
    return web.json_response(payload, headers={"ETag": etag})


@requires_api_key
async def sheet_growth(request: web.Request) -> web.Response:
    """Alliance growth metrics over time, as MM's generic
    ``{ metrics, snapshots: [{ date, members, values }] }``. Returns an empty
    series (not 404/500) when growth isn't configured, so MM shows its empty
    state."""
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    import growth

    # gspread is blocking; keep it off the event loop the API shares with the
    # gateway client.
    series = await asyncio.to_thread(growth.read_growth_series, guild_id)
    return web.json_response(series)


@requires_api_key
async def sheet_roster(request: web.Request) -> web.Response:
    """The alliance roster as MM's ``RosterMember[]`` (+ ETag).

    Identity (name / discord_id / display_name / joined_at) comes from the Member
    Roster tab; ``roles`` (leadership/member tiers) resolves live from the
    gateway; ``power`` is the alliance's configured growth metrics at their
    latest snapshot (Total Hero / 1st Squad / ... as a label→number map); and
    ``attendance`` is the combined storm attendance % (matching ``/member_stats``).
    Empty list when no roster is configured; power/attendance are best-effort by
    member name.
    """
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)

    import config
    import growth
    import member_roster
    import member_stats
    from api.routes.guilds import _tier_roles

    # Three blocking gspread reads — run them concurrently off the event loop.
    rows, power_map, attendance_map = await asyncio.gather(
        asyncio.to_thread(member_roster.read_roster_members, guild_id),
        asyncio.to_thread(growth.read_member_power_map, guild_id),
        asyncio.to_thread(member_stats.read_storm_attendance_map, guild_id),
    )

    cfg = config.get_config(guild_id)
    bot = request.app[BOT_KEY]
    guild = bot.get_guild(guild_id) if bot is not None else None

    members = []
    for r in rows:
        discord_id = r.get("discord_id")
        roles: list[str] = []
        if discord_id and guild is not None:
            member = guild.get_member(int(discord_id))
            if member is not None:
                roles = _tier_roles(member, cfg)

        # Power / attendance join by name, preferring the display name (the key
        # the growth + participation sheets use), falling back to the username.
        name_keys = [k.strip().lower() for k in (r.get("display_name"), r.get("name")) if k]
        power: dict = {}
        attendance = None
        for k in name_keys:
            if not power:
                power = power_map.get(k, {})
            if attendance is None:
                attendance = attendance_map.get(k)

        members.append(
            {
                "discord_id": discord_id,
                "name": r["name"],
                "display_name": r.get("display_name"),
                "joined_at": r.get("joined_at"),
                "roles": roles,
                "power": power,
                "attendance": attendance,
            }
        )

    return _etag_response(members, request)


@requires_api_key
async def sheet_storm_history_get(request: web.Request) -> web.Response:
    # Blocked on data, not effort: the bot stores participation/attendance, not
    # match outcomes (opponent/result/score). Implement once OCR write-back
    # populates those. Honor ?limit&offset, cap limit at 500.
    return _not_implemented(
        "storm-history",
        "{ items: StormRecord[], total_count } where StormRecord = "
        "{ id?, date, opponent, result: 'win'|'loss', score, notes }",
    )


@requires_api_key
async def sheet_storm_history_append(request: web.Request) -> web.Response:
    # OCR write-back (append a parsed storm row to the storm-log sheet) is
    # Phase 8; stubbed per the handoff.
    return _not_implemented("storm-history append", "{ appended: true }")

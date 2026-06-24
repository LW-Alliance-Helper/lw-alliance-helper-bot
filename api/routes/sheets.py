"""Credentialed sheet-read endpoints Map Manager calls (6D, #316).

These serve the Google-Sheet-backed alliance data MM renders on its alliance
surfaces, plus the OCR write-back append.

Status:
  - **`GET /sheet/growth` — implemented.** A generic pass-through of the
    alliance's *configured* growth metrics (MM renders them generically), shaped
    by ``growth.read_growth_series``.
  - **roster / storm-history reads — 501 stubs.** Roster needs a small cross-tab
    join to MM's fixed schema; storm-history needs match-outcome data the bot
    doesn't store yet (it tracks participation/attendance, not results — those
    arrive via the OCR write-back). Each returns a structured 501 documenting
    its target shape until that's wired.
  - **`POST /sheet/storm-history` — 501 stub** (OCR append is Phase 8).

The gspread reads are blocking and the API server shares the gateway's event
loop, so reads run via ``asyncio.to_thread``. Target shapes for the stubs live
in the Map Manager repo's ``docs/BOT_INTEGRATION_HANDOFF.md`` §3.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

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
    # Target: 200 with an ETag (hash of the gspread payload) and rows of
    # {discord_id, name, display_name, joined_at, roles, power, attendance,
    #  hero_level, last_seen} — the roster tab joined with the related sheets.
    return _not_implemented(
        "roster",
        "RosterMember[]: { discord_id, name, display_name, joined_at, roles, "
        "power, attendance, hero_level, last_seen } (+ ETag header)",
    )


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

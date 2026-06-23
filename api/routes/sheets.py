"""Credentialed sheet-read endpoints Map Manager calls (6D, #316).

These serve the Google-Sheet-backed alliance data (roster, storm history,
growth) that MM renders on its Phase 7 alliance surfaces, plus the OCR
write-back append.

**Status: deliberately stubbed (501).** Two reasons, both about not building the
wrong shape before its consumer exists:

  1. The GET response shapes are coupled to MM's alliance loaders, which are MM
     Phase 7 (``kevins-new-features.md`` §1.2–1.4) and not built yet. Until that
     contract is locked there's nothing to validate a concrete shape against.
  2. The interesting fields (power, attendance, hero level, last seen) are
     consolidated server-side from several sheets the bot already maintains
     (roster + storm attendance + growth + survey), keyed by member identity —
     a real consolidation feature, and the source columns vary per alliance.

So each handler returns a structured 501 documenting its target shape; wiring
them to the bot's existing sheet helpers (``config.read_member_roster_values``,
``storm_history``, ``growth.read_latest_breakdown``) is a focused follow-up once
MM's loader contract is in hand. The POST append is an explicit stub per the
handoff (OCR write-back is Phase 8). Auth still applies, so MM gets a clean 501
(not a 404/500) while it builds against these.
"""

from __future__ import annotations

from aiohttp import web

from api.auth import requires_api_key

_NOT_IMPLEMENTED_DETAIL = (
    "Sheet-backed endpoint not implemented yet. It lands once Map Manager's "
    "Phase 7 alliance loaders lock the response shape; see api/routes/sheets.py."
)


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
async def sheet_roster(request: web.Request) -> web.Response:
    # Target: 200 with an ETag (hash of the gspread payload) and rows of
    # {discord_id, name, display_name, joined_at, roles, power, attendance,
    #  hero_level, last_seen} — consolidated from the Member Roster tab plus the
    # related storm/growth sheets the bot maintains.
    return _not_implemented(
        "roster",
        "{ etag, members: [{ discord_id, name, display_name, joined_at, roles, "
        "power, attendance, hero_level, last_seen }] }",
    )


@requires_api_key
async def sheet_storm_history_get(request: web.Request) -> web.Response:
    # Target: 200 with paginated rows + total_count; honor ?limit&offset, cap
    # limit at 500.
    return _not_implemented(
        "storm-history",
        "{ storms: [{ date, opponent, result, score, attackers, defenders, "
        "duration, notes }], total_count }",
    )


@requires_api_key
async def sheet_growth(request: web.Request) -> web.Response:
    # Target: 200 with weekly growth snapshots as a time series.
    return _not_implemented(
        "growth",
        "{ snapshots: [{ week, total_power, active_members, ... }] }",
    )


@requires_api_key
async def sheet_storm_history_append(request: web.Request) -> web.Response:
    # OCR write-back (append a parsed storm row to the storm-log sheet) is
    # Phase 8; stubbed per the handoff.
    return _not_implemented("storm-history append", "{ appended: true }")

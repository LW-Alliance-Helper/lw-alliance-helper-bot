"""Champion Duel prediction API — `/champion-duel/v1/*`.

The prefix is never bare ``duel``: Champion Duel, Warzone Duel and Alliance VS
Duel are three different events and ``alliance_duel.py`` already owns the third.
Versioned from day one because Map Manager will consume this later and should
not break when the shape moves.

**The engine runs here, in Python, on purpose.** The web app posts a matchup
and gets a probability back from the same ``engine.py`` the backtest validates,
rather than a JavaScript port carrying a second copy of every calibrated
constant. A second copy is exactly what left ``analyze_counter_grades`` fitting
at K=123 for days after the engine had moved to 59.

The engine import is **optional**. A failed ``pip install`` of the pinned
package must degrade this one feature, never break a production bot serving
paying alliances — so ``/predict`` answers 503 while every other route, which
only touches SQLite, keeps working.

Every DB call goes through ``asyncio.to_thread``. ruff's ASYNC rules do not see
sqlite3 as blocking (it isn't stdlib-level I/O to them), so this is the #366
class of bug that still needs a human: a synchronous query here stalls the
Discord gateway heartbeat for the whole process.
"""

from __future__ import annotations

import asyncio

from aiohttp import web

import champion_duel_db as db
from api import BOT_KEY
from api.champion_duel_auth import (
    admin_ids,
    app_url,
    identify,
    json_response,
    missing_oauth_env,
    oauth_configured,
    requires_admin,
    requires_session,
    requires_writer,
)

try:
    from champion_duel_engine import constants, exact_match_prob

    ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - the degraded path is asserted in tests
    ENGINE_AVAILABLE = False


async def health(request: web.Request) -> web.Response:
    """Liveness plus what this deploy can actually do.

    Reports the engine and identity packages separately because they fail
    independently and the difference decides whether predictions or writes are
    the thing that's broken.
    """
    return json_response(
        {
            "status": "ok",
            "engine": ENGINE_AVAILABLE,
            "identity": db.NAMES_AVAILABLE,
            # Whether login can run, and what is missing if it can't. Checking
            # this should not require starting a redirect to Discord and
            # reading the error on the way back.
            "oauth": oauth_configured(request.app.get(BOT_KEY)),
            "oauth_missing": missing_oauth_env(request.app.get(BOT_KEY)),
            "app_url_set": bool(app_url()),
            "admins_configured": len(admin_ids()),
            "constants": constants() if ENGINE_AVAILABLE else None,
        },
        request,
    )


async def groups(request: web.Request) -> web.Response:
    return json_response({"groups": await asyncio.to_thread(db.get_groups)}, request)


async def roster(request: web.Request) -> web.Response:
    """Registrants, with scouting only for authenticated callers.

    The registrant list is an LWS export anyone can pull. Squad composition and
    deployment orders are our own scouting and the Predict & Win edge, so they
    require a login even though the roster around them does not.
    """
    actor = await identify(request)
    group = request.query.get("group")
    players = await asyncio.to_thread(db.get_roster, group, actor is not None)
    return json_response({"roster": players, "scouting_included": actor is not None}, request)


@requires_session
async def player(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if not db.NAMES_AVAILABLE:
        return json_response({"error": "identity_unavailable"}, request, status=503)
    found = await asyncio.to_thread(db.get_player, name, True)
    if found is None:
        return json_response({"error": "not_found"}, request, status=404)
    return json_response(found, request)


async def predict(request: web.Request) -> web.Response:
    """Exact P(A wins) for one match. Pure computation, touches no state.

    Returns the calibrated constants alongside the probability so a caller
    comparing two predictions can tell a model change from a data change.
    Read live from the engine rather than captured at import, because the
    backtest refits by assigning to the module's globals.
    """
    if not ENGINE_AVAILABLE:
        return json_response(
            {"error": "engine_unavailable", "detail": "champion-duel-engine is not installed."},
            request,
            status=503,
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error
        return json_response({"error": "bad_request"}, request, status=400)

    try:
        squads_a = _squads(body.get("a"))
        squads_b = _squads(body.get("b"))
    except ValueError as exc:
        return json_response({"error": "bad_request", "detail": str(exc)}, request, status=400)

    p_a = exact_match_prob(squads_a, squads_b)
    return json_response({"p_a": p_a, "p_b": 1.0 - p_a, "engine": constants()}, request)


def _squads(raw):
    """Validate one side into the [(power, type), ...] shape the engine wants.

    Strict rather than forgiving: a silently-coerced squad produces a
    confident-looking probability for a matchup nobody asked about, which is
    worse than an error when real wagers ride on the number.
    """
    if not isinstance(raw, list) or len(raw) != 3:
        raise ValueError("each side needs exactly 3 squads")
    out = []
    for i, sq in enumerate(raw, start=1):
        if not isinstance(sq, dict):
            raise ValueError(f"squad {i} must be an object")
        squad_type = sq.get("type")
        if squad_type not in db.VALID_TYPES:
            raise ValueError(f"squad {i} type must be one of {db.VALID_TYPES}")
        try:
            power = float(sq.get("power"))
        except (TypeError, ValueError):
            raise ValueError(f"squad {i} power must be a number") from None
        if power <= 0:
            raise ValueError(f"squad {i} power must be positive")
        out.append((power, squad_type))
    return out


# ── Writes (Premium) ──────────────────────────────────────────────────────────


@requires_writer
async def patch_squads(request: web.Request) -> web.Response:
    """Correct one squad slot. Each changed field becomes its own edit row."""
    if not db.NAMES_AVAILABLE:
        return json_response({"error": "identity_unavailable"}, request, status=503)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return json_response({"error": "bad_request"}, request, status=400)

    try:
        result = await asyncio.to_thread(
            db.set_squad,
            request.match_info["name"],
            int(body.get("slot", 0)),
            body.get("type"),
            body.get("power"),
            actor=request["cd_actor"],
            source=body.get("source", "edited"),
        )
    except LookupError as exc:
        return json_response({"error": "not_found", "detail": str(exc)}, request, status=404)
    except (ValueError, TypeError) as exc:
        return json_response({"error": "bad_request", "detail": str(exc)}, request, status=400)
    return json_response(result, request)


@requires_writer
async def post_order(request: web.Request) -> web.Response:
    """Record a deployment order actually seen. Repeats are kept: a player seen
    five times in one order and once in another must sample 5:1."""
    if not db.NAMES_AVAILABLE:
        return json_response({"error": "identity_unavailable"}, request, status=503)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return json_response({"error": "bad_request"}, request, status=400)

    try:
        result = await asyncio.to_thread(
            db.add_order,
            request.match_info["name"],
            list(body.get("slots") or []),
            actor=request["cd_actor"],
            opponent=body.get("opponent"),
            observed_at=body.get("observed_at"),
        )
    except LookupError as exc:
        return json_response({"error": "not_found", "detail": str(exc)}, request, status=404)
    except (ValueError, TypeError) as exc:
        return json_response({"error": "bad_request", "detail": str(exc)}, request, status=400)
    return json_response(result, request)


# ── Admin ─────────────────────────────────────────────────────────────────────


@requires_admin
async def admin_import(request: web.Request) -> web.Response:
    """Bulk-load the roster. Never touches scouting — an import must not
    downgrade an observed squad back to an estimate."""
    if not db.NAMES_AVAILABLE:
        return json_response({"error": "identity_unavailable"}, request, status=503)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return json_response({"error": "bad_request"}, request, status=400)

    rows = body.get("registrants")
    if not isinstance(rows, list):
        return json_response(
            {"error": "bad_request", "detail": "registrants must be a list"}, request, status=400
        )
    return json_response(await asyncio.to_thread(db.import_registrants, rows), request)


@requires_admin
async def admin_edits(request: web.Request) -> web.Response:
    q = request.query
    try:
        limit = min(int(q.get("limit", 50)), 500)
        offset = int(q.get("offset", 0))
    except ValueError:
        return json_response({"error": "bad_request"}, request, status=400)
    result = await asyncio.to_thread(
        db.list_edits,
        since=q.get("since"),
        until=q.get("until"),
        player=q.get("player"),
        actor=q.get("actor"),
        limit=limit,
        offset=offset,
    )
    return json_response(result, request)


@requires_admin
async def admin_revert(request: web.Request) -> web.Response:
    """Restore a prior value, refusing if it has since moved on.

    409 rather than a silent overwrite: two scouts entering sightings for one
    player at the same time is normal, and the later entry is usually the
    better information. The response carries what it found so the admin can
    decide, and `force` is how they say yes.
    """
    try:
        edit_id = int(request.match_info["edit_id"])
    except ValueError:
        return json_response({"error": "bad_request"}, request, status=400)

    force = request.query.get("force") == "1"
    try:
        result = await asyncio.to_thread(
            db.revert_edit, edit_id, actor=request["cd_actor"], force=force
        )
    except db.RevertConflict as exc:
        return json_response(
            {
                "error": "conflict",
                "detail": str(exc),
                "current": exc.current,
                "expected": exc.expected,
            },
            request,
            status=409,
        )
    except LookupError as exc:
        return json_response({"error": "not_found", "detail": str(exc)}, request, status=404)
    except ValueError as exc:
        return json_response({"error": "bad_request", "detail": str(exc)}, request, status=400)
    return json_response(result, request)

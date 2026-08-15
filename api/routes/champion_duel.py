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


def _grouping_id(request: web.Request):
    """The `grouping` query parameter, or None for every grouping.

    None rather than a default: this API has anonymous readers and no guild to
    resolve from, so there is nobody whose grouping it could mean. A caller that
    wants one scopes explicitly.
    """
    raw = request.query.get("grouping")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def groupings(request: web.Request) -> web.Response:
    """Every grouping and its warzones.

    The index a caller needs before it can scope anything else: a warzone is
    the only handle most consumers have, and this is what turns one into an id.
    """
    return json_response({"groupings": await asyncio.to_thread(db.list_groupings)}, request)


async def groups(request: web.Request) -> web.Response:
    return json_response(
        {
            "groups": await asyncio.to_thread(
                db.get_groups, request.query.get("stage"), _grouping_id(request)
            )
        },
        request,
    )


async def roster(request: web.Request) -> web.Response:
    """Registrants, with scouting only for authenticated callers.

    The registrant list is an LWS export anyone can pull. Squad composition and
    deployment orders are our own scouting and the Predict & Win edge, so they
    require a login even though the roster around them does not.

    `grouping` and `stage` scope the read. A group letter is only meaningful
    inside both -- "group D" in the semifinals is a different set of people from
    "group D" in the qualifiers, and a different set again in another grouping.
    """
    actor = await identify(request)
    players = await asyncio.to_thread(
        db.get_roster,
        request.query.get("group"),
        actor is not None,
        request.query.get("stage"),
        _grouping_id(request),
    )
    return json_response({"roster": players, "scouting_included": actor is not None}, request)


def _ambiguous(exc, request):
    """409 listing the candidates. Names repeat across servers, so picking one
    would attach a sighting to the wrong player -- unrecoverable, and the
    caller is in a position to ask."""
    return json_response(
        {
            "error": "ambiguous_player",
            "detail": str(exc),
            "candidates": [
                {
                    "id": c["id"],
                    "server": c["server"],
                    "group": c["grp"],
                    "display_name": c["display_name"],
                }
                for c in exc.candidates
            ],
        },
        request,
        status=409,
    )


@requires_session
async def player(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    if not db.NAMES_AVAILABLE:
        return json_response({"error": "identity_unavailable"}, request, status=503)
    try:
        found = await asyncio.to_thread(db.get_player, name, request.query.get("server"), True)
    except db.AmbiguousPlayer as exc:
        return _ambiguous(exc, request)
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
async def post_player(request: web.Request) -> web.Response:
    """Add a player the roster doesn't have.

    The imported roster is who signed up, not everyone anyone will meet: names
    change mid-event and an opponent can sit outside whatever was last
    imported. Without this the write routes are limited to correcting numbers
    on players we already knew, which is a much smaller thing than the access
    model was designed around — writes are Premium precisely because the
    dataset is only worth anything if more people contribute.

    `origin='self_reported'` is what keeps that safe. A community-added row
    stays distinguishable from an official import everywhere it is shown, and a
    later import upgrades it rather than duplicating it.

    Idempotent on (name, server): re-posting someone returns their existing row
    rather than erroring, because two people entering the same opponent is the
    normal case, not a conflict.
    """
    if not db.NAMES_AVAILABLE:
        return json_response({"error": "identity_unavailable"}, request, status=503)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return json_response({"error": "bad_request"}, request, status=400)

    name = (body.get("name") or "").strip()
    server = str(body.get("server") or "").strip()
    if not name or not server:
        return json_response(
            {
                "error": "bad_request",
                "detail": "name and server are both required — identity is the pair",
            },
            request,
            status=400,
        )

    try:
        player = await asyncio.to_thread(
            db.upsert_registrant,
            name,
            server=server,
            grp=body.get("group"),
            alliance=body.get("alliance"),
            origin="self_reported",
            actor=request["cd_actor"],
        )
    except (ValueError, TypeError) as exc:
        return json_response({"error": "bad_request", "detail": str(exc)}, request, status=400)

    # A group belongs to the round of a grouping, not to the player. Only
    # written when one was supplied, so a caller who omits it does not assert
    # that this player is in the current round.
    #
    # The grouping comes from the payload or from the player's own warzone. A
    # bare letter with neither has nothing to belong to, and writing it against
    # whatever round happens to be running is what put one alliance's opponent
    # in another alliance's Group D.
    group = (body.get("group") or "").strip()
    group_note = None
    if group:
        grouping_id = _grouping_id(request) or body.get("grouping")
        if grouping_id is None:
            found = await asyncio.to_thread(db.find_grouping_by_warzone, server)
            grouping_id = found["id"] if found else None
        stage = await asyncio.to_thread(db.current_stage, grouping_id) if grouping_id else None
        if stage is None:
            # Not a 4xx. The player is a real fact and is already written, so
            # failing the whole call would leave a row behind and report an
            # error -- the caller would have no way to tell what landed. The
            # letter is the only part we cannot place, and the response says so.
            group_note = (
                "no grouping resolved for this warzone, so the group letter was not "
                "recorded; pass `grouping` to place it"
            )
        else:
            await asyncio.to_thread(
                db.set_stage, player["id"], stage, grp=group, grouping_id=grouping_id
            )
            player = await asyncio.to_thread(db.get_player, name, server)
    if group_note:
        player = {**player, "group_recorded": False, "group_note": group_note}
    return json_response(player, request)


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
        registrant = await asyncio.to_thread(
            db.resolve_registrant, request.match_info["name"], body.get("server")
        )
        result = await asyncio.to_thread(
            db.set_squad,
            registrant["id"],
            int(body.get("slot", 0)),
            body.get("type"),
            body.get("power"),
            actor=request["cd_actor"],
            source=body.get("source", "edited"),
        )
    except db.AmbiguousPlayer as exc:
        return _ambiguous(exc, request)
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
        registrant = await asyncio.to_thread(
            db.resolve_registrant, request.match_info["name"], body.get("server")
        )
        result = await asyncio.to_thread(
            db.add_order,
            registrant["id"],
            list(body.get("slots") or []),
            actor=request["cd_actor"],
            opponent=body.get("opponent"),
            observed_at=body.get("observed_at"),
        )
    except db.AmbiguousPlayer as exc:
        return _ambiguous(exc, request)
    except LookupError as exc:
        return json_response({"error": "not_found", "detail": str(exc)}, request, status=404)
    except (ValueError, TypeError) as exc:
        return json_response({"error": "bad_request", "detail": str(exc)}, request, status=400)
    return json_response(result, request)


# ── Admin ─────────────────────────────────────────────────────────────────────


@requires_admin
async def admin_import(request: web.Request) -> web.Response:
    """Bulk-load the roster, and optionally the scouting that goes with it.

    One call rather than three, because the three are one operation: a roster
    with no squads cannot be predicted at all (97% of registrants have never
    been seen, so almost every row needs its estimate to be useful), and orders
    are meaningless without the registrants they hang off. Splitting them would
    make a half-applied import the normal outcome of a dropped connection.

    Applied in dependency order for the same reason — squads and orders both
    resolve against registrant rows, so those have to exist first.

    An import must not downgrade an observed squad back to an estimate, and
    must not double a deployment order's weight when it is re-run. Both rules
    live in the data layer; see `import_squads` / `import_orders`.
    """
    if not db.NAMES_AVAILABLE:
        return json_response({"error": "identity_unavailable"}, request, status=503)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return json_response({"error": "bad_request"}, request, status=400)

    for field in ("registrants", "squads", "orders"):
        if field in body and not isinstance(body[field], list):
            return json_response(
                {"error": "bad_request", "detail": f"{field} must be a list"}, request, status=400
            )
    if "registrants" not in body and "squads" not in body and "orders" not in body:
        return json_response(
            {"error": "bad_request", "detail": "nothing to import"}, request, status=400
        )

    actor = request["cd_actor"]
    result = {}
    if body.get("registrants") is not None:
        grouping = body.get("grouping") if isinstance(body.get("grouping"), dict) else None
        grouping_id = None
        if grouping and grouping.get("warzones"):
            grouping_id = (
                await asyncio.to_thread(
                    db.ensure_grouping, grouping["warzones"], grouping.get("started_on")
                )
            )["id"]
        result["registrants"] = await asyncio.to_thread(
            db.import_registrants,
            body["registrants"],
            stage=body.get("stage"),
            grouping_id=grouping_id,
            started_on=(grouping or {}).get("started_on"),
        )
    if body.get("squads") is not None:
        result["squads"] = await asyncio.to_thread(db.import_squads, body["squads"], actor=actor)
    if body.get("orders") is not None:
        result["orders"] = await asyncio.to_thread(db.import_orders, body["orders"], actor=actor)
    return json_response(result, request)


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

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
    doesn't store (it tracks participation/attendance, not results). By design,
    storm results live only in MM (OCR), not the Sheet — so this stays a stub.
  - **`POST /sheet/storm-history` — 501 stub** (storm results are MM-only).
  - **`POST /sheet/roster` — implemented.** OCR member-add (§6.3): merge-adds
    parsed names, preserving existing + non-Discord rows.
  - **`POST /sheet/power` — implemented.** OCR power upsert (§6.2): per-metric
    upsert into the current period's growth columns; never clobbers.
  - **`GET /growth/breakdown`, `GET /members/{id}/stats`, `GET /storm/trends` —
    implemented.** Enrichment reads surfacing bot features on MM's pages.

The gspread reads are blocking and the API server shares the gateway's event
loop, so reads run via ``asyncio.to_thread``. Target shapes live in the Map
Manager repo's ``docs/BOT_INTEGRATION_HANDOFF.md`` §3.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

from aiohttp import web

from api import BOT_KEY
from api.auth import requires_api_key

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

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


@requires_api_key
async def sheet_storm_roster(request: web.Request) -> web.Response:
    """Write an MM-built storm plan to the bot's `rosters_tab` (handoff §6.1).

    Body `{ event_type: "ds"|"cs", event_date: "YYYY-MM-DD", assignments: [...],
    overwrite?: bool }`. Writes one row per assignment only when that
    (event_type, date) has no rows yet (or `overwrite`), filling Power from the
    roster. Returns `{ written, rows, skipped_reason? }`.
    """
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON
        return web.json_response({"error": "bad_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "bad_body"}, status=400)

    event_type = str(body.get("event_type", "")).lower()
    event_date = str(body.get("event_date", ""))
    assignments = body.get("assignments")
    if event_type not in ("ds", "cs"):
        return web.json_response({"error": "bad_event_type"}, status=400)
    if not _DATE_RE.match(event_date):
        return web.json_response({"error": "bad_event_date"}, status=400)
    if not isinstance(assignments, list):
        return web.json_response({"error": "bad_assignments"}, status=400)

    import storm_roster_writeback

    result = await asyncio.to_thread(
        storm_roster_writeback.write_mm_storm_roster,
        guild_id,
        event_type,
        event_date,
        assignments,
        overwrite=bool(body.get("overwrite", False)),
    )
    return web.json_response(result)


@requires_api_key
async def get_member_history(request: web.Request) -> web.Response:
    """Per-member growth history (Phase 8). Resolves the Discord id to the
    member's roster + gateway names, then reads their per-metric growth series.
    Returns the metric-keyed shape `{ "metrics": { label: [{ at, value }] } }`.
    """
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    discord_id = request.match_info.get("discord_user_id", "")

    import growth
    import member_roster

    name_keys: set[str] = set()
    # Roster names — the keys the growth sheet is most likely keyed by.
    roster = await asyncio.to_thread(member_roster.read_roster_members, guild_id)
    for r in roster:
        if r.get("discord_id") == discord_id:
            for k in (r.get("display_name"), r.get("name")):
                if k:
                    name_keys.add(k.strip().lower())
            break
    # Gateway names too (covers a member missing from the roster sheet).
    bot = request.app[BOT_KEY]
    guild = bot.get_guild(guild_id) if bot is not None else None
    member = guild.get_member(int(discord_id)) if guild and discord_id.isdigit() else None
    if member is not None:
        name_keys.add(member.display_name.strip().lower())
        name_keys.add(member.name.strip().lower())

    history = await asyncio.to_thread(growth.read_member_history, guild_id, name_keys)
    return web.json_response(history)


@requires_api_key
async def get_storm_strategies(request: web.Request) -> web.Response:
    """Named strategies for MM's planner dropdown (PHASE8 §4). ``?event_type=ds|cs``.
    Returns ``{ strategies: [{ id, name }] }`` (the bot keys presets by name, so
    id == name); empty list when none are configured."""
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    event_type = (request.query.get("event_type") or "").lower()
    if event_type not in ("ds", "cs"):
        return web.json_response({"error": "bad_event_type"}, status=400)

    import storm_strategy

    strategies = await asyncio.to_thread(
        storm_strategy.list_strategies, guild_id, event_type.upper()
    )
    return web.json_response({"strategies": strategies})


@requires_api_key
async def get_zone_rules(request: web.Request) -> web.Response:
    """Per-zone rules for the strategy MM picked (PHASE8 §4, display only).
    ``?event_type=ds|cs&strategy_id=<id>`` (``strategy_id`` optional — omitted
    falls back to the first strategy). Returns ``{ rules: [{ zone, min_a, min_b,
    min_players, max_players, priority }] }``; empty when nothing is configured.
    ``min_players`` is always 0 (the bot has no per-zone minimum player count)."""
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    event_type = (request.query.get("event_type") or "").lower()
    if event_type not in ("ds", "cs"):
        return web.json_response({"error": "bad_event_type"}, status=400)
    strategy_id = request.query.get("strategy_id") or None

    import storm_strategy

    rules = await asyncio.to_thread(
        storm_strategy.zone_rules_for, guild_id, event_type.upper(), strategy_id
    )
    return web.json_response({"rules": rules})


# ── OCR write-backs (handoff §6.2 / §6.3) ─────────────────────────────────────
# MM parses a screenshot, the officer confirms, and MM posts only what it's
# adding/changing. The bot merges into the Sheet (the source of truth) and never
# clobbers existing data. Both return `{ written, rows }`.


@requires_api_key
async def sheet_roster_add(request: web.Request) -> web.Response:
    """OCR member-add (§6.3). Body `{ members: [{ name, discord_id }] }`
    (`discord_id` is null for OCR'd names). Merge-adds names not already on the
    roster, preserving existing + hand-typed non-Discord rows; identity stays
    Discord-sync-owned. Returns `{ written, rows }`."""
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON
        return web.json_response({"error": "bad_json"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("members"), list):
        return web.json_response({"error": "bad_members"}, status=400)

    import member_roster

    result = await asyncio.to_thread(member_roster.add_ocr_members, guild_id, body["members"])
    return web.json_response(result)


@requires_api_key
async def sheet_power_upsert(request: web.Request) -> web.Response:
    """OCR power upsert (§6.2). Body `{ members: [{ name, discord_id, values: {
    label: number } }] }`. Upserts each sent metric (labels = configured growth
    metrics) into the current period's column; never clobbers metrics/columns MM
    didn't send. Returns `{ written, rows }`."""
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON
        return web.json_response({"error": "bad_json"}, status=400)
    if not isinstance(body, dict) or not isinstance(body.get("members"), list):
        return web.json_response({"error": "bad_members"}, status=400)

    import growth

    result = await asyncio.to_thread(growth.upsert_member_power, guild_id, body["members"])
    return web.json_response(result)


# ── Enrichment reads (surface bot features on MM's alliance pages, #316) ──────


@requires_api_key
async def get_growth_breakdown(request: web.Request) -> web.Response:
    """Per-member growth buckets for MM's Growth page. Returns `{ has_data,
    prev_period_label, curr_period_label, metric_labels, summary: { metric: {
    bucket: [names] } } }` — present-but-empty before the first transition. With
    `?from={Mon YYYY}&to={Mon YYYY}` (MM's Compare picker) it classifies that
    specific range; an unknown period falls back to the latest transition rather
    than erroring."""
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    from_period = request.query.get("from")
    to_period = request.query.get("to")

    import growth

    if from_period and to_period:
        data = await asyncio.to_thread(growth.breakdown_for_range, guild_id, from_period, to_period)
        if not data.get("has_data"):
            # Unknown period(s): fall back to the latest transition, not a 500.
            data = await asyncio.to_thread(growth.read_latest_breakdown, guild_id)
    else:
        data = await asyncio.to_thread(growth.read_latest_breakdown, guild_id)
    return web.json_response(data)


@requires_api_key
async def get_member_profile(request: web.Request) -> web.Response:
    """Consolidated member profile (the JSON behind `/member_stats`): identity,
    power, storm participation, train, surveys. `?lookback=N` (clamped 1..50)
    scopes the storm counts to the last N events; omitted = the bot's default
    window. 404 when the member is unknown to both the roster and the gateway."""
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    discord_id = request.match_info.get("discord_user_id", "")
    if not discord_id.isdigit():
        return web.json_response({"error": "bad_member_id"}, status=400)

    lookback = None
    raw_lookback = request.query.get("lookback")
    if raw_lookback is not None:
        try:
            lookback = max(1, min(int(raw_lookback), 50))
        except (TypeError, ValueError):
            lookback = None

    import member_stats

    # Gateway lookup must happen on the event loop; the resolved member object is
    # then read off-thread (cached attrs only).
    bot = request.app[BOT_KEY]
    guild = bot.get_guild(guild_id) if bot is not None else None
    member = guild.get_member(int(discord_id)) if guild is not None else None

    target = await asyncio.to_thread(
        member_stats.resolve_profile_target, guild_id, int(discord_id), member=member
    )
    if target is None:
        return web.json_response({"error": "member_not_found"}, status=404)
    profile = await asyncio.to_thread(
        member_stats.build_member_profile, guild_id, target, lookback=lookback
    )
    return web.json_response(profile)


@requires_api_key
async def get_storm_trends(request: web.Request) -> web.Response:
    """Per-member storm attendance trends for MM's Storms page.
    `?event_type=ds|cs&lookback=N` (N clamped 1..50, default 12). Returns
    `{ event_type, lookback_events, total_events, members: [...] }`."""
    guild_id = _parse_guild_id(request)
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    event_type = (request.query.get("event_type") or "").lower()
    if event_type not in ("ds", "cs"):
        return web.json_response({"error": "bad_event_type"}, status=400)
    try:
        lookback = int(request.query.get("lookback", 12))
    except (TypeError, ValueError):
        lookback = 12
    lookback = max(1, min(lookback, 50))

    import storm_trends

    data = await asyncio.to_thread(
        storm_trends.member_attendance_summary, guild_id, event_type.upper(), lookback
    )
    return web.json_response(data)

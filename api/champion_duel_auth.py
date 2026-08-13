"""Discord OAuth + access gates for the Champion Duel API.

Deliberately a *second*, parallel gate rather than a change to
``api.auth.requires_api_key``. That one is a shared service key for the Map
Manager server-to-server contract; this one authenticates individual humans in
a browser. Widening the Map Manager gate to cover browsers would make a
server-to-server secret browser-reachable, so the two stay separate and are
revoked independently.

**Why the bot does the OAuth exchange.** Discord's token endpoint requires
``client_secret``; there is no public-client/PKCE-only flow. So the browser
never completes the exchange -- it hands the bot a code, the bot swaps it
server-side, and the browser gets a session token the bot minted itself.

**Two kinds of caller.** Writes accept either a user session (a human logged in
through the flow below) or a trusted service asserting who acted:

    Authorization: Bearer <session token>              -- today, the web app
    Authorization: Bearer <service key>                -- later, Map Manager
    X-Acting-User: <discord user id>

The second path exists so folding this into Map Manager's Alliance section is a
move rather than a rewrite: MM authenticates its own users with its own
sessions, then calls these routes with its service key. ``actor_discord_id``
stays truthful either way, which is the whole reason writes are attributed at
all.

Env vars:
  - ``DISCORD_CLIENT_ID`` / ``DISCORD_CLIENT_SECRET`` -- the Discord app. Shared
    with Map Manager on purpose: same app means the same user ids, so ported
    edits already match MM's user rows.
  - ``CHAMPION_DUEL_REDIRECT_URI``  -- must match a registered redirect exactly.
  - ``CHAMPION_DUEL_APP_URL``       -- the predictor *page*, e.g.
    ``https://host/champion-duel-predictor.html``. The post-login bounce goes
    here, and the CORS origin is derived from it so the two cannot drift.
  - ``CHAMPION_DUEL_APP_ORIGIN``    -- optional override for the CORS origin
    alone, if the page is served from somewhere the bot doesn't redirect to.
  - ``CHAMPION_DUEL_ADMIN_IDS``     -- comma-separated Discord user ids that may
    browse, revert and export the edit history.
  - ``CHAMPION_DUEL_SERVICE_KEY``   -- optional; the on-behalf-of path above.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
from functools import wraps
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

import champion_duel_db as db
from api import BOT_KEY

DISCORD_API = "https://discord.com/api/v10"
STATE_COOKIE = "cd_oauth_state"

# `identify` and nothing else. The premium check reads guild membership from
# the gateway cache the bot already holds, so asking for the `guilds` scope
# would put an extra permission on the consent screen and buy nothing.
OAUTH_SCOPE = "identify"


def _env(name: str) -> str | None:
    return (os.getenv(name, "") or "").strip() or None


OAUTH_ENV = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "CHAMPION_DUEL_REDIRECT_URI")


def missing_oauth_env() -> list[str]:
    """Which OAuth variables are unset.

    Reported by name in the 503, because "oauth_unconfigured" alone sends
    whoever is deploying to check three variables by hand across two systems.
    Names are not secrets; the values never appear.
    """
    return [n for n in OAUTH_ENV if not _env(n)]


def oauth_configured() -> bool:
    """True when the login flow can actually run.

    Checked before advertising login rather than failing mid-redirect, so a
    half-configured deploy says so instead of bouncing a user to Discord and
    dying on the way back.
    """
    return not missing_oauth_env()


def admin_ids() -> frozenset[str]:
    """Discord user ids allowed to browse/revert/export edits.

    Comma-separated, same parsing as ``BOT_ADMIN_GUILD_IDS``. Unset means no
    admins -- the surface closes rather than opening to everyone, because the
    failure mode of the alternative is unrecoverable.
    """
    raw = os.getenv("CHAMPION_DUEL_ADMIN_IDS", "") or ""
    return frozenset(p.strip() for p in raw.split(",") if p.strip().isdigit())


def app_url() -> str | None:
    """The predictor page itself, e.g. https://host/champion-duel-predictor.html

    This is where the callback bounces the browser back to, and it must be the
    *page*, not the site root. The app is published alongside the dashboards,
    so the root is a landing page that knows nothing about the one-time code --
    sending users there would strand the code in the URL of a page with no
    script to redeem it, and read as login silently doing nothing.
    """
    return _env("CHAMPION_DUEL_APP_URL")


def app_origin() -> str | None:
    """The origin for CORS: scheme://host, no path.

    Derived from CHAMPION_DUEL_APP_URL so the two cannot drift -- a browser
    sends `Origin` without a path, and comparing it against a full page URL
    would reject every cross-origin call. CHAMPION_DUEL_APP_ORIGIN still
    overrides it for the case where the page is served from somewhere the
    bot doesn't redirect to.
    """
    explicit = _env("CHAMPION_DUEL_APP_ORIGIN")
    if explicit:
        return explicit.rstrip("/")
    url = app_url()
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}"


# ── CORS ──────────────────────────────────────────────────────────────────────


def apply_cors(response: web.StreamResponse, request: web.Request) -> web.StreamResponse:
    """Attach CORS headers, but only for the configured app origin.

    Explicit origin rather than ``*``: the write routes carry an Authorization
    header, and ``*`` is invalid for credentialed requests anyway. Applied only
    to ``/champion-duel/*`` -- the Map Manager routes are a server-to-server
    contract and must not become browser-reachable.
    """
    origin = request.headers.get("Origin")
    allowed = app_origin()
    if allowed and origin == allowed:
        response.headers["Access-Control-Allow-Origin"] = allowed
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Vary"] = "Origin"
    return response


async def preflight(request: web.Request) -> web.Response:
    resp = web.Response(status=204)
    apply_cors(resp, request)
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-Acting-User"
    resp.headers["Access-Control-Max-Age"] = "600"
    return resp


def json_response(data, request, status=200) -> web.Response:
    resp = web.json_response(data, status=status)
    apply_cors(resp, request)
    return resp


# ── The login flow ────────────────────────────────────────────────────────────


async def login(request: web.Request) -> web.StreamResponse:
    """302 to Discord's consent screen, with CSRF state in a cookie."""
    if not oauth_configured():
        return json_response(
            {
                "error": "oauth_unconfigured",
                "missing": missing_oauth_env(),
                "detail": "These environment variables are unset on the bot service.",
            },
            request,
            status=503,
        )

    state = secrets.token_urlsafe(24)
    params = {
        "client_id": _env("DISCORD_CLIENT_ID"),
        "redirect_uri": _env("CHAMPION_DUEL_REDIRECT_URI"),
        "response_type": "code",
        "scope": OAUTH_SCOPE,
        "state": state,
        "prompt": "none",
    }
    url = f"{DISCORD_API}/oauth2/authorize?" + "&".join(
        f"{k}={aiohttp.helpers.quote(str(v), safe='')}" for k, v in params.items()
    )
    resp = web.HTTPFound(url)
    # SameSite=Lax, not Strict: the cookie has to survive Discord's top-level
    # redirect back here, and Strict would drop it and break every login.
    resp.set_cookie(
        STATE_COOKIE, state, max_age=600, httponly=True, secure=True, samesite="Lax", path="/"
    )
    return resp


async def _exchange_code(code: str) -> dict:
    data = {
        "client_id": _env("DISCORD_CLIENT_ID"),
        "client_secret": _env("DISCORD_CLIENT_SECRET"),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _env("CHAMPION_DUEL_REDIRECT_URI"),
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{DISCORD_API}/oauth2/token", data=data) as r:
            if r.status != 200:
                raise web.HTTPBadRequest(reason="token_exchange_failed")
            return await r.json()


async def _fetch_profile(access_token: str) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        ) as r:
            if r.status != 200:
                raise web.HTTPBadRequest(reason="profile_fetch_failed")
            return await r.json()


async def resolve_writer_context(bot, discord_user_id: str) -> tuple[bool, str | None]:
    """Can this user write, and on whose behalf?

    The single place that decides write access, so swapping the rule is a
    one-function change. Today: membership of any guild with an active Premium
    entitlement. Under Map Manager later this comes from MM's alliance context
    (roster ∩ leadership tier → canManage) instead, and no route handler moves.

    Premium is deliberately the gate rather than a hand-maintained allowlist:
    if an alliance pays, it decides who on its team is trusted to enter data,
    and the dataset is only worth anything if more people contribute sightings.
    Every write is attributed and revertable, so the blast radius is bounded.
    """
    if bot is None:
        return False, None
    try:
        import premium
    except ImportError:  # pragma: no cover
        return False, None

    uid = int(discord_user_id)
    for guild in getattr(bot, "guilds", []):
        member = guild.get_member(uid)
        if member is None:
            continue
        try:
            # feature_gate rather than is_premium: it is the canonical gate and
            # it raises on an unregistered name, so a premium feature cannot
            # quietly ship ungated.
            if await premium.feature_gate("champion_duel_write", guild.id, bot=bot):
                return True, str(guild.id)
        except Exception as exc:  # noqa: BLE001 - a premium lookup must not 500 a login
            print(f"[CHAMPION_DUEL] premium check failed for guild {guild.id}: {exc}")
    return False, None


async def callback(request: web.Request) -> web.StreamResponse:
    """Discord redirects here. Validate, exchange, mint a session, bounce back.

    The browser leaves with a *one-time code*, never the session token: a token
    in a redirect URL lands in browser history, the Referer header and any
    proxy log along the way.
    """
    if not oauth_configured():
        return json_response(
            {
                "error": "oauth_unconfigured",
                "missing": missing_oauth_env(),
                "detail": "These environment variables are unset on the bot service.",
            },
            request,
            status=503,
        )

    code = request.query.get("code")
    state = request.query.get("state")
    expected = request.cookies.get(STATE_COOKIE)

    # Back to the predictor page, not the site root -- only that page carries
    # the script that redeems the one-time code.
    back = app_url() or app_origin() or "/"
    if request.query.get("error"):
        return web.HTTPFound(f"{back}#error=discord_denied")
    if not code or not state or not expected or not hmac.compare_digest(state, expected):
        return web.HTTPFound(f"{back}#error=bad_state")

    try:
        token_data = await _exchange_code(code)
        profile = await _fetch_profile(token_data["access_token"])
    except web.HTTPException:
        return web.HTTPFound(f"{back}#error=discord_failed")

    user_id = str(profile["id"])
    name = profile.get("global_name") or profile.get("username") or user_id
    can_write, guild_id = await resolve_writer_context(request.app.get(BOT_KEY), user_id)

    # The session is minted at redemption, not here -- the code carries the
    # identity instead. That is what allows `sessions` to hold only a hash.
    handoff = await asyncio.to_thread(db.create_auth_code, user_id, name, can_write, guild_id)

    resp = web.HTTPFound(f"{back}#code={handoff}")
    resp.del_cookie(STATE_COOKIE, path="/")
    return resp


async def exchange(request: web.Request) -> web.Response:
    """Redeem the one-time code for a session token, over POST.

    This is where the session is actually created, so the plaintext token
    exists exactly once -- in this response -- and only its hash is persisted.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a 500
        return json_response({"error": "bad_request"}, request, status=400)

    code = (body or {}).get("code")
    if not code:
        return json_response({"error": "bad_request"}, request, status=400)

    identity = await asyncio.to_thread(db.consume_auth_code, code)
    if identity is None:
        # Unknown, expired and already-used answer the same on purpose.
        return json_response({"error": "invalid_code"}, request, status=401)

    token = await asyncio.to_thread(
        db.create_session,
        identity["discord_user_id"],
        identity["discord_name"],
        identity["can_write"],
        identity["writer_guild_id"],
    )
    return json_response(
        {
            "token": token,
            "discord_user_id": identity["discord_user_id"],
            "discord_name": identity["discord_name"],
            "can_write": identity["can_write"],
            "can_admin": identity["discord_user_id"] in admin_ids(),
        },
        request,
    )


# ── Gates ─────────────────────────────────────────────────────────────────────


def _bearer(request: web.Request) -> str | None:
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return header[len("Bearer ") :].strip() or None


async def identify(request: web.Request) -> dict | None:
    """Resolve the caller: a user session, or a trusted service acting for one.

    Returns an actor dict shaped for ``champion_duel_db`` (it only ever needs
    a Discord id, a display name and a guild), or None when unauthenticated.
    """
    token = _bearer(request)
    if not token:
        return None

    service_key = _env("CHAMPION_DUEL_SERVICE_KEY")
    acting = request.headers.get("X-Acting-User", "").strip()
    if service_key and acting.isdigit() and hmac.compare_digest(token, service_key):
        can_write, guild_id = await resolve_writer_context(request.app.get(BOT_KEY), acting)
        return {
            "discord_user_id": acting,
            "discord_name": None,
            "guild_id": guild_id,
            "can_write": can_write,
            "via": "service",
        }

    session = await asyncio.to_thread(db.get_session, token)
    if session is None:
        return None
    return {
        "discord_user_id": session["discord_user_id"],
        "discord_name": session["discord_name"],
        "guild_id": session["writer_guild_id"],
        "can_write": bool(session["can_write"]),
        "via": "session",
        "_token": token,
    }


def requires_session(handler):
    """401 unless the caller is authenticated. Scouting reads use this."""

    @wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        actor = await identify(request)
        if actor is None:
            return json_response({"error": "unauthorized"}, request, status=401)
        request["cd_actor"] = actor
        return await handler(request)

    return wrapper


def requires_writer(handler):
    """403 unless the caller belongs to a Premium guild.

    Distinct from 401: "log in" and "your alliance needs Premium" are different
    problems and the app shows different things for each.
    """

    @wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        actor = await identify(request)
        if actor is None:
            return json_response({"error": "unauthorized"}, request, status=401)
        if not actor.get("can_write"):
            return json_response(
                {"error": "premium_required", "detail": "Writing needs a Premium alliance."},
                request,
                status=403,
            )
        request["cd_actor"] = actor
        return await handler(request)

    return wrapper


def requires_admin(handler):
    """403 unless the caller's Discord id is in CHAMPION_DUEL_ADMIN_IDS."""

    @wraps(handler)
    async def wrapper(request: web.Request) -> web.StreamResponse:
        actor = await identify(request)
        if actor is None:
            return json_response({"error": "unauthorized"}, request, status=401)
        if actor["discord_user_id"] not in admin_ids():
            return json_response({"error": "forbidden"}, request, status=403)
        request["cd_actor"] = actor
        return await handler(request)

    return wrapper


async def me(request: web.Request) -> web.Response:
    """Who am I and what may I do -- the app renders its UI from this."""
    actor = await identify(request)
    if actor is None:
        return json_response(
            {"authenticated": False, "login_available": oauth_configured()}, request
        )
    return json_response(
        {
            "authenticated": True,
            "discord_user_id": actor["discord_user_id"],
            "discord_name": actor.get("discord_name"),
            "can_write": bool(actor.get("can_write")),
            "can_admin": actor["discord_user_id"] in admin_ids(),
            "guild_id": actor.get("guild_id"),
        },
        request,
    )


async def logout(request: web.Request) -> web.Response:
    actor = await identify(request)
    if actor and actor.get("_token"):
        await asyncio.to_thread(db.revoke_session, actor["_token"])
    return json_response({"ok": True}, request)

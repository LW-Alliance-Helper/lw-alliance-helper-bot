"""Discord gateway-action endpoints MM calls (Phase 8 "Post to Discord", #316).

Two Bearer-gated actions that use the bot's live gateway connection:
  - ``GET /api/guilds/{guild_id}/channels`` — the text channels the bot can post
    in (Send Messages + Attach Files), so MM's channel picker can offer them.
  - ``POST /api/guilds/{guild_id}/post-image`` — post a base64-encoded PNG (the
    map MM rendered) to a channel as an attachment, with an optional caption.
    Returns the posted message URL.

Both run in the bot's event loop (the API server is in-process), so the Discord
calls are awaited directly. See ``docs/PHASE8_DISCORD_HANDOFF.md`` in the Map
Manager repo for the contract.

Image transport decision: **base64 in the JSON body** (matches the other JSON
endpoints). The app raises its body-size limit to accommodate it (see
``api_server.build_app``). Multipart would also work; this picked base64 so MM's
client stays JSON-only.
"""

from __future__ import annotations

import base64
import binascii
from io import BytesIO
from typing import Optional

import discord
from aiohttp import web

from api import BOT_KEY
from api.auth import requires_api_key


def _parse_int(request: web.Request, key: str) -> Optional[int]:
    try:
        return int(request.match_info[key])
    except (KeyError, ValueError):
        return None


@requires_api_key
async def get_guild_channels(request: web.Request) -> web.Response:
    """Text channels the bot can post + attach in, in Discord (category +
    position) order. Empty list when the bot isn't in the guild."""
    guild_id = _parse_int(request, "guild_id")
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)

    bot = request.app[BOT_KEY]
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is None:
        return web.json_response({"channels": []})

    me = guild.me
    channels = []
    for ch in guild.text_channels:  # discord.py returns these in display order
        perms = ch.permissions_for(me)
        if perms.send_messages and perms.attach_files:
            channels.append({"id": str(ch.id), "name": ch.name})
    return web.json_response({"channels": channels})


@requires_api_key
async def post_image(request: web.Request) -> web.Response:
    """Post a base64 PNG to a channel as an attachment, with an optional caption.

    Body: ``{ channel_id, filename, image_base64, message? }``. Returns
    ``{ posted: true, message_url }``; on any failure ``{ posted: false, error }``
    with a 4xx so MM can surface the reason.
    """
    guild_id = _parse_int(request, "guild_id")
    if guild_id is None:
        return web.json_response({"posted": False, "error": "bad_guild_id"}, status=400)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed JSON / wrong content-type
        return web.json_response({"posted": False, "error": "bad_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"posted": False, "error": "bad_body"}, status=400)

    try:
        channel_id = int(body.get("channel_id"))
    except (TypeError, ValueError):
        return web.json_response({"posted": False, "error": "bad_channel_id"}, status=400)
    image_b64 = body.get("image_base64")
    if not isinstance(image_b64, str) or not image_b64.strip():
        return web.json_response({"posted": False, "error": "missing_image"}, status=400)
    try:
        image_bytes = base64.b64decode(image_b64, validate=True)
    except (binascii.Error, ValueError):
        return web.json_response({"posted": False, "error": "bad_base64"}, status=400)
    filename = (str(body.get("filename") or "").strip()) or "storm-map.png"
    message = body.get("message") or None

    bot = request.app[BOT_KEY]
    guild = bot.get_guild(guild_id) if bot is not None else None
    if guild is None:
        return web.json_response({"posted": False, "error": "guild_not_found"}, status=404)
    channel = guild.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.TextChannel):
        return web.json_response({"posted": False, "error": "channel_not_found"}, status=404)
    perms = channel.permissions_for(guild.me)
    if not (perms.send_messages and perms.attach_files):
        return web.json_response({"posted": False, "error": "missing_permission"}, status=403)

    try:
        file = discord.File(BytesIO(image_bytes), filename=filename)
        sent = await channel.send(content=message, file=file)
    except discord.HTTPException as e:
        # Size limit, rate limit, transient Discord error, etc.
        return web.json_response({"posted": False, "error": f"discord_error: {e}"}, status=400)

    return web.json_response({"posted": True, "message_url": sent.jump_url})

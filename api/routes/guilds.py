"""Credentialed guild endpoints Map Manager calls (6D, #316).

Two Bearer-gated reads:
  - ``GET /api/guilds/{guild_id}/link`` — the guild's alliance link (cached
    locally by the ``/map_manager`` wizards) plus the configured Google Sheet id
    and the guild's live Premium state. 404 when the guild has no active link.
  - ``GET /api/guilds/{guild_id}/members/{discord_user_id}`` — a gateway-cache
    lookup of a member's presence and tier roles (``leadership`` / ``member``,
    derived from the guild's configured role names). This is what MM consults to
    resolve a signed-in user's ``bot_linked_leader`` / ``bot_linked_member``
    tier, so it's the endpoint MM's permission seam is gated on.

Both reach the discord.py client and the config DB; neither touches Google
Sheets (those are the ``sheets`` module).
"""

from __future__ import annotations

from typing import Optional

from aiohttp import web

import config
import premium
from api import BOT_KEY
from api.auth import requires_api_key


def _parse_int(request: web.Request, key: str) -> Optional[int]:
    try:
        return int(request.match_info[key])
    except (KeyError, ValueError):
        return None


def _tier_roles(member, cfg) -> list[str]:
    """Map a member's Discord roles to MM permission tiers using the guild's
    configured leadership / member role names. Returns the subset of
    ``["leadership", "member"]`` the member holds (leadership implies the
    edit-capable tier; a member may hold both)."""
    if cfg is None:
        return []
    role_names = {r.name for r in getattr(member, "roles", [])}
    out: list[str] = []
    if cfg.leadership_role_name and cfg.leadership_role_name in role_names:
        out.append("leadership")
    if cfg.member_role_name and cfg.member_role_name in role_names:
        out.append("member")
    return out


@requires_api_key
async def get_guild_link(request: web.Request) -> web.Response:
    guild_id = _parse_int(request, "guild_id")
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)

    mapping = config.get_guild_alliance_mapping(guild_id)
    if mapping is None:
        return web.json_response({"error": "not_linked"}, status=404)

    cfg = config.get_config(guild_id)
    sheet_id = (cfg.spreadsheet_id if cfg else "") or ""
    bot = request.app[BOT_KEY]
    premium_flag = await premium.is_premium(guild_id, bot=bot)

    return web.json_response(
        {
            "guild_id": str(guild_id),
            "alliance_id": mapping["mm_alliance_id"],
            "alliance_name": mapping["alliance_name"],
            "server": mapping["server"],
            "server_grouping_id": mapping["mm_server_grouping_id"],
            "sheet_id": sheet_id,
            "premium": premium_flag,
        }
    )


@requires_api_key
async def get_guild_member(request: web.Request) -> web.Response:
    guild_id = _parse_int(request, "guild_id")
    if guild_id is None:
        return web.json_response({"error": "bad_guild_id"}, status=400)
    user_id = _parse_int(request, "discord_user_id")
    if user_id is None:
        return web.json_response({"error": "bad_user_id"}, status=400)

    bot = request.app[BOT_KEY]
    guild = bot.get_guild(guild_id) if bot is not None else None
    member = guild.get_member(user_id) if guild is not None else None

    # Not-in-guild (bot absent, gateway cache cold, or member not present) is a
    # 200 with in_guild=false, not a 404 — MM degrades the tier gracefully
    # rather than erroring.
    if member is None:
        return web.json_response({"in_guild": False, "roles": [], "display_name": None})

    cfg = config.get_config(guild_id)
    return web.json_response(
        {
            "in_guild": True,
            "roles": _tier_roles(member, cfg),
            "display_name": member.display_name,
        }
    )

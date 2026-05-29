"""
storm_permissions.py — shared permission + premium gating for storm cogs.

Centralizes two checks that the structured-flow storm cogs all need:

1. `is_leader_or_admin(interaction)` — admin OR configured leadership
   role. Mirrors `setup_cog._has_leadership_or_admin` semantics but
   without dragging the wizard-laden setup_cog module into every cog's
   import graph.

2. `ensure_premium_structured(interaction, event_type, ...)` — verifies
   the guild has Premium AND has opted into the structured flow for
   this event type. Sends the right ephemeral message on failure
   (upgrade prompt for non-premium; setup pointer for non-opted-in).

The first round of structured-flow cogs each invented their own
permission check that read a non-existent `leader_role_id` attribute
(the correct field is `leadership_role_name`), silently locking out
non-admin officers with the leadership role. The premium gate was
also inconsistent — some cogs checked one half, some neither. Routing
everything through this module fixes both classes of bug in one place
and gives future cogs a single canonical entry point.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

import discord

logger = logging.getLogger(__name__)


def is_leader_or_admin(interaction: discord.Interaction) -> bool:
    """True if the invoking user is a server administrator OR has the
    configured leadership role.

    Returns False in DMs (no `Member` object, no guild config) so callers
    don't have to special-case the off-guild path.
    """
    from config import get_config

    member = interaction.user
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.administrator:
        return True
    cfg = get_config(interaction.guild_id) if interaction.guild_id else None
    if cfg and cfg.leadership_role_name:
        if cfg.leadership_role_name in [r.name for r in member.roles]:
            return True
    return False


async def deny_non_leader(interaction: discord.Interaction) -> None:
    """Send the standard ephemeral denial to non-admin / non-leader users.

    Survives the case where the interaction has already been deferred —
    falls back to `followup.send` automatically.
    """
    message = "⛔ You need the leadership role (or admin) to run this command."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def ensure_premium_structured(
    interaction: discord.Interaction,
    event_type: str,
    *,
    bot: Optional[discord.Client] = None,
    feature_label: Optional[str] = None,
) -> Tuple[bool, Optional[dict]]:
    """Verify Premium + structured-flow opt-in for this event type.

    Returns `(ok, structured_cfg)`. On failure, sends the right
    ephemeral message and returns `(False, None)`. Callers should
    `return` immediately on a False result.

    Handles both pre-defer and post-defer states automatically.
    """
    import premium
    from config import get_structured_storm_config

    from setup_hub import STORM_SETUP_NAV

    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    setup_cmd = STORM_SETUP_NAV[event_type]

    async def _say(message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    if not interaction.guild_id:
        await _say("⚠️ This command must be used inside a server.")
        return False, None

    if not await premium.is_premium(interaction.guild_id, interaction=interaction, bot=bot):
        await _say(
            f"🔒 {feature_label or 'The structured storm flow'} is a "
            f"💎 Premium feature. Run `/upgrade` to unlock it."
        )
        return False, None

    structured = get_structured_storm_config(interaction.guild_id, event_type)
    if not structured.get("structured_flow_enabled"):
        await _say(
            f"⚠️ The structured roster flow isn't enabled for {label}. "
            f"Run `{setup_cmd}` and turn on **Structured Roster Flow** first."
        )
        return False, None

    return True, structured

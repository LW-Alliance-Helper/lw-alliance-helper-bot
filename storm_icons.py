"""
Storm zone emoji icons (#158 + #177).

Tiny in-game map icons that prefix zone names across every storm
surface — sign-up post mail body, officer view, roster builder embed,
attendance view, history detail, preset editor. Reading a roster
visually maps to the in-game map instead of forcing leadership to
translate zone names in their head.

Discord plumbing uses Application Emojis (bot-owned, no guild
dependency). Two-step setup per environment:

1. Upload the PNGs once: `DISCORD_TOKEN=<bot> py scripts/upload_storm_emojis.py`
2. Bot reads the resulting `{name: id}` map from its own Application
   at startup (`refresh_zone_emoji_ids` in `on_ready`).

That's it. No source carries IDs; dev and prod each resolve their own
emoji set from their respective bot tokens. Adding a new icon = drop
PNG into `assets/storm_icons/<event>/`, re-run the upload script, no
code change.

Renderers call `zone_emoji_prefix(zone_name)` and either get back
`"<:icon:id> "` (when the icon is registered) or `""` (when it isn't
— including during the boot window before on_ready fires, when the
fetch failed, or when no emojis are uploaded yet). Missing icons
silently fall through to plain text — no broken markup, no log spam.
"""

from __future__ import annotations

import logging
import re as _re

logger = logging.getLogger(__name__)


# Populated at bot startup by `refresh_zone_emoji_ids(bot)` from the
# bot's Discord Application Emojis. Stays empty until then —
# `zone_emoji_prefix()` returns "" in that window, which is the same
# no-op default the empty stub shipped with. Per-environment lookup
# is automatic: each bot's token resolves to its own Application,
# which holds its own emoji set.
#
# Keyed by stem (numerals + Roman numeral suffixes stripped) so one
# emoji name can serve many in-game zone variants:
#   "Field Hospital I/II/III/IV" → `field_hospital`
#   "Data Center 1/2"            → `data_center`
ZONE_EMOJI_IDS: dict[str, int] = {}


async def refresh_zone_emoji_ids(bot) -> int:
    """Populate `ZONE_EMOJI_IDS` from the bot's Application Emojis.
    Returns the number of mapped entries.

    Call from `on_ready` so each reconnect picks up any newly-
    uploaded emojis without a process restart. Idempotent — the
    dict is cleared + rebuilt each call.

    Failure path (network blip, Discord 5xx, token rotated): logs a
    warning and leaves the dict untouched. Storm renders keep using
    the last-known-good IDs from the prior call; if there was no
    prior call, they fall through to plain text. Either way the bot
    stays up.

    The `bot` parameter is typed as `Any` (left unannotated) so this
    module stays free of `discord.py` imports — the renderer modules
    that read `ZONE_EMOJI_IDS` import it without pulling Discord
    types in transitively.
    """
    try:
        emojis = await bot.fetch_application_emojis()
    except Exception as e:
        logger.warning(
            "[STORM ICONS] fetch_application_emojis failed: %s. "
            "Zone icons keep using whatever IDs were registered "
            "previously (or fall through to plain text if none).",
            e,
        )
        return 0
    ZONE_EMOJI_IDS.clear()
    for emoji in emojis:
        # Emoji names mirror the stem shape produced by
        # `_stem_from_filename` in scripts/upload_storm_emojis.py
        # ("Field Hospital.png" → `field_hospital`). The lookup side
        # (`_zone_stem`) produces the same shape, so they match
        # directly without any normalisation here.
        ZONE_EMOJI_IDS[emoji.name] = emoji.id
    if not ZONE_EMOJI_IDS:
        logger.warning(
            "[STORM ICONS] no application emojis registered for this bot. "
            "Run scripts/upload_storm_emojis.py to upload them; storm "
            "renders will fall through to plain text until then."
        )
    return len(ZONE_EMOJI_IDS)


# Roman + Arabic numerals stripped from the END of a zone name when
# normalizing to its stem. Compiled inline so the helper stays cheap
# at render time. The leading space is required so we don't strip the
# `2` out of "Data Center" but DO strip it out of "Data Center 2".

_NUMERAL_STRIP_RE = _re.compile(
    r"\s+(?:I{1,3}|IV|V|VI{0,3}|IX|X|[1-9])\s*$",
    flags=_re.IGNORECASE,
)


def _zone_stem(zone_name: str) -> str:
    """Normalize a zone name to its emoji-lookup stem.

    Strips trailing Roman / Arabic numerals so `Field Hospital II` and
    `Data Center 2` resolve to the same stem as their unnumbered base.
    Lower-cases and replaces spaces with underscores so the result
    matches Discord's emoji-name shape.
    """
    if not zone_name:
        return ""
    stripped = _NUMERAL_STRIP_RE.sub("", zone_name).strip()
    return stripped.lower().replace(" ", "_")


def zone_emoji_prefix(zone_name: str) -> str:
    """Return `'<:icon:id> '` for the zone's icon, or `''` if no icon
    is registered for that zone's stem (or `ZONE_EMOJI_IDS` is empty).

    The trailing space is part of the return value — callers can do
    `f"{zone_emoji_prefix(zone)}{zone}"` and get the right spacing
    whether the icon is present or not.
    """
    if not ZONE_EMOJI_IDS:
        return ""
    stem = _zone_stem(zone_name)
    emoji_id = ZONE_EMOJI_IDS.get(stem)
    if not emoji_id:
        return ""
    return f"<:{stem}:{emoji_id}> "

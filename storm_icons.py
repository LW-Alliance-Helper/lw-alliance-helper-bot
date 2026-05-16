"""
Storm zone emoji icons (#158).

Tiny in-game map icons that prefix zone names across every storm
surface — sign-up post mail body, officer view, roster builder embed,
attendance view, history detail, preset editor. Reading a roster
visually maps to the in-game map instead of forcing leadership to
translate zone names in their head.

Discord plumbing uses Application Emojis (bot-owned, no guild
dependency). Upload once via `scripts/upload_storm_emojis.py`, paste
the resulting `{name: id}` dict into `ZONE_EMOJI_IDS` below.

This module is intentionally pure — no `discord.py` imports, no
network. Renderers call `zone_emoji_prefix(zone_name)` and either get
back `"<:icon:id> "` (when the icon is registered) or `""` (when it
isn't). Missing icons silently fall through to plain text — no broken
markup, no log spam, ships cleanly before every icon is drawn.
"""

from __future__ import annotations

# Application emoji IDs keyed by zone STEM (numerals + Roman numeral
# suffixes stripped). One stem can map many in-game zone names:
#   "Field Hospital I/II/III/IV" all use the same `field_hospital` icon.
#   "Data Center 1/2"            both use the same `data_center` icon.
#
# Empty dict = no icons uploaded yet. `zone_emoji_prefix` returns ""
# for every zone in that case and renderers fall through to plain
# text. Run `scripts/upload_storm_emojis.py` to populate.
ZONE_EMOJI_IDS: dict[str, int] = {
    # DS canonical stems
    # "nuclear_silo":      ...,
    # "oil_refinery":      ...,
    # "science_hub":       ...,
    # "info_center":       ...,
    # "field_hospital":    ...,
    # "arsenal":           ...,            # icon TBD
    # "mercenary_factory": ...,            # icon TBD
    # CS canonical stems
    # "power_tower":       ...,
    # "data_center":       ...,
    # "sample_warehouse":  ...,
    # "floaters":          ...,            # icon TBD
    # "defense_system":    ...,
    # "serum_factory":     ...,
    # "virus_lab":         ...,
}

# Roman + Arabic numerals stripped from the END of a zone name when
# normalizing to its stem. Compiled inline so the helper stays cheap
# at render time. The leading space is required so we don't strip the
# `2` out of "Data Center" but DO strip it out of "Data Center 2".
import re as _re

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

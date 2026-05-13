"""
Pillow-based PNG render of a storm roster (#132).

Contract:

    storm_renderer.render(roster: RosterData) -> bytes  # PNG

Used by the manual roster builder (free tier) when leadership clicks
`[🖼️ Render image]`, and by the structured-flow Approve & Post
finalisation (Premium) as a file attachment alongside the mail.

The implementation is intentionally simple: a vertical text layout
on a white canvas with the default Pillow font, sized to fit the
roster. No backgrounds, no fancy graphics — those are Map Manager
territory and stay out of scope per the [#54](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/54) "Out of scope" list.

The contract is stable enough that the Pillow backend can be swapped
for a Map Manager API call later without touching any callsite.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RosterZone:
    """One zone in the rendered roster."""
    name: str
    max_players: int
    members: list[str] = field(default_factory=list)


@dataclass
class RosterData:
    """Everything the renderer needs to produce a PNG."""
    title: str                              # e.g. "Desert Storm — Standard — Team A — 2026-05-18"
    zones: list[RosterZone]
    subs: list[str] = field(default_factory=list)
    special_roles: dict[str, list[str]] = field(default_factory=dict)
    # Optional paired-sub mapping: {primary_name: sub_name}. Rendered
    # alongside the primary line when present.
    paired_subs: dict[str, str] = field(default_factory=dict)


# Layout constants — kept readable + isolated so a future redesign
# can tweak without hunting through the render code.
_PADDING_X      = 24
_PADDING_Y      = 24
_TITLE_GAP      = 18
_SECTION_GAP    = 14
_LINE_GAP       = 6
_BG_COLOR       = (255, 255, 255)   # white
_TEXT_COLOR     = (32, 32, 32)
_MUTED_COLOR    = (110, 110, 110)
_TITLE_COLOR    = (16, 16, 16)
_ZONE_COLOR     = (30, 60, 110)     # blue-ish heading
_WIDTH          = 720               # px


def render(roster: RosterData) -> bytes:
    """Render the roster to a PNG byte-string.

    Raises `RuntimeError` if Pillow isn't installed — caller should
    catch and fall back to text-only output. Returning bytes (rather
    than a Pillow Image) keeps the contract independent of the
    rendering library so a Map Manager swap is trivial.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise RuntimeError(
            "Pillow isn't installed — `/storm_*` image render unavailable. "
            "Add `Pillow>=10.0.0` to requirements.txt."
        ) from e

    # Default font; Pillow ships a tiny bitmap font that's enough for
    # readable English text. Truetype lookups are explicitly avoided —
    # Railway containers might not have any system fonts installed.
    title_font = ImageFont.load_default(size=18) if _supports_size() else ImageFont.load_default()
    heading_font = ImageFont.load_default(size=14) if _supports_size() else ImageFont.load_default()
    body_font = ImageFont.load_default(size=12) if _supports_size() else ImageFont.load_default()

    # First pass: compute height by walking the layout dry.
    height = _measure_height(roster, title_font, heading_font, body_font)
    img = Image.new("RGB", (_WIDTH, height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    y = _PADDING_Y

    # Title
    draw.text(
        (_PADDING_X, y), roster.title, fill=_TITLE_COLOR, font=title_font,
    )
    y += _line_height(title_font) + _TITLE_GAP

    # Zones
    for zone in roster.zones:
        count = len(zone.members)
        heading = f"{zone.name}  ({count}/{zone.max_players})"
        draw.text(
            (_PADDING_X, y), heading, fill=_ZONE_COLOR, font=heading_font,
        )
        y += _line_height(heading_font) + _LINE_GAP
        if not zone.members:
            draw.text(
                (_PADDING_X + 16, y), "(empty)",
                fill=_MUTED_COLOR, font=body_font,
            )
            y += _line_height(body_font) + _LINE_GAP
        else:
            for name in zone.members:
                sub = roster.paired_subs.get(name)
                line = f"• {name}" + (f"   ↳ sub: {sub}" if sub else "")
                draw.text(
                    (_PADDING_X + 16, y), line,
                    fill=_TEXT_COLOR, font=body_font,
                )
                y += _line_height(body_font) + _LINE_GAP
        y += _SECTION_GAP - _LINE_GAP

    # Subs
    if roster.subs:
        draw.text(
            (_PADDING_X, y), f"Subs ({len(roster.subs)})",
            fill=_ZONE_COLOR, font=heading_font,
        )
        y += _line_height(heading_font) + _LINE_GAP
        for name in roster.subs:
            draw.text(
                (_PADDING_X + 16, y), f"• {name}",
                fill=_TEXT_COLOR, font=body_font,
            )
            y += _line_height(body_font) + _LINE_GAP
        y += _SECTION_GAP - _LINE_GAP

    # Special roles
    if roster.special_roles:
        for role_name, names in roster.special_roles.items():
            if not names:
                continue
            heading = f"{role_name.title()}: {', '.join(names)}"
            draw.text(
                (_PADDING_X, y), heading,
                fill=_TEXT_COLOR, font=body_font,
            )
            y += _line_height(body_font) + _LINE_GAP

    # Serialize.
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── Layout helpers ───────────────────────────────────────────────────────────


def _supports_size() -> bool:
    """Pillow 10+ supports `size=` on `load_default()`. Older versions
    raise TypeError. Probing once at module init keeps the render path
    fast."""
    try:
        from PIL import ImageFont
        ImageFont.load_default(size=10)
        return True
    except (TypeError, ImportError):
        return False


def _line_height(font) -> int:
    """Approximate the line height for a font. Falls back to a sane
    default if `getbbox` is unavailable (very old Pillow)."""
    try:
        # bbox of "Ag" (ascender + descender characters) for a stable
        # vertical extent.
        bbox = font.getbbox("Ag")
        return bbox[3] - bbox[1] + 2
    except AttributeError:
        return 14


def _measure_height(
    roster: RosterData,
    title_font, heading_font, body_font,
) -> int:
    """Walk the same layout the renderer will, summing y-extent so the
    canvas is sized correctly. Returns at least 200 px for the empty
    case so the resulting PNG is never visually weird."""
    y = _PADDING_Y + _line_height(title_font) + _TITLE_GAP
    for zone in roster.zones:
        y += _line_height(heading_font) + _LINE_GAP
        if not zone.members:
            y += _line_height(body_font) + _LINE_GAP
        else:
            y += (len(zone.members)) * (_line_height(body_font) + _LINE_GAP)
        y += _SECTION_GAP - _LINE_GAP
    if roster.subs:
        y += _line_height(heading_font) + _LINE_GAP
        y += len(roster.subs) * (_line_height(body_font) + _LINE_GAP)
        y += _SECTION_GAP - _LINE_GAP
    if roster.special_roles:
        for role, names in roster.special_roles.items():
            if names:
                y += _line_height(body_font) + _LINE_GAP
    y += _PADDING_Y
    return max(200, y)


# ── Conversion helper from a RosterBuilderSession ────────────────────────────


def roster_from_session(session) -> RosterData:
    """Build a `RosterData` from a `RosterBuilderSession` for the
    builder's `[🖼️ Render image]` button. Lives in the renderer rather
    than the builder so `storm_roster_builder` doesn't import Pillow
    at module-load time — Pillow's import path is heavy and the
    free-tier builder shouldn't pay the cost unless image render is
    actually used.
    """
    event_label = "Desert Storm" if session.event_type == "DS" else "Canyon Storm"
    team_suffix = ""
    if session.event_type == "DS" and session.team:
        team_suffix = f" — Team {session.team}"
    elif session.preset.faction and session.preset.faction != "Either":
        team_suffix = f" — {session.preset.faction}"
    date_suffix = f" — {session.event_date}" if session.event_date else ""

    zones: list[RosterZone] = []
    paired_subs: dict[str, str] = {}
    for z in session.preset.zones:
        names: list[str] = []
        for key in session.assignments.get(z.zone, []):
            m = session.members.get(key)
            if not m:
                continue
            primary_name = m["name"]
            names.append(primary_name)
            if session.is_paired:
                sub_key = session.paired_subs.get(key)
                if sub_key:
                    sub_m = session.members.get(sub_key)
                    if sub_m:
                        paired_subs[primary_name] = sub_m["name"]
        zones.append(RosterZone(
            name=z.zone, max_players=int(z.max_players), members=names,
        ))

    subs = [
        session.members[k]["name"] for k in session.subs
        if k in session.members
    ]

    return RosterData(
        title=f"{event_label} — {session.preset.name}{team_suffix}{date_suffix}",
        zones=zones, subs=subs, paired_subs=paired_subs,
    )

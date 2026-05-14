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
    # Per-member metadata for rich rendering. Map keyed on `name` so
    # the renderer doesn't have to plumb a richer data structure
    # through every layout helper.
    #
    # `powers[name]` → human-readable power string ("412M") or "" /
    # missing to omit. The audit found the screenshot artifact dropped
    # the power readout the embed shows, so officers couldn't
    # cross-check power-by-zone from the image.
    #
    # `overrides` → set of member names assigned below the zone floor.
    # The rosters_tab `Override Below Floor` column captures the same
    # data; without surfacing it in the PNG, post-event review loses a
    # decision an officer made at build time.
    powers: dict[str, str] = field(default_factory=dict)
    overrides: set[str] = field(default_factory=set)


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

    # Font lookup tiers:
    #   1. DejaVuSans TTF if available on the host (covers Latin-1
    #      Supplement, Greek, Cyrillic, Korean, CJK depending on the
    #      installed variant) — common on Linux/Railway containers.
    #   2. Pillow's bundled default with size= (Pillow >= 10).
    #   3. Pillow's bundled default at native (tiny ASCII bitmap).
    # The bundled default is 1bpp ASCII — member names with emoji or
    # CJK characters render as `?` boxes through that path, so the
    # TTF lookup is what makes the PNG actually usable for non-English
    # alliances.
    title_font   = _best_font(ImageFont, size=18)
    heading_font = _best_font(ImageFont, size=14)
    body_font    = _best_font(ImageFont, size=12)

    # First pass: compute height by walking the layout dry.
    height = _measure_height(roster, title_font, heading_font, body_font)
    img = Image.new("RGB", (_WIDTH, height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    y = _PADDING_Y

    # Title — word-wrapped to the canvas width so long preset names /
    # paired-mode labels don't overflow the right edge. Without this,
    # "Desert Storm — <Long Preset Name> — Team A — 2026-05-18" got
    # truncated visually in the rendered PNG.
    title_lines = _wrap_text(
        roster.title, title_font, _WIDTH - 2 * _PADDING_X,
    )
    for line in title_lines:
        draw.text(
            (_PADDING_X, y), line, fill=_TITLE_COLOR, font=title_font,
        )
        y += _line_height(title_font)
    y += _TITLE_GAP

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
                power_str = roster.powers.get(name) or ""
                is_override = name in roster.overrides
                line = f"• {name}"
                if power_str:
                    line += f" ({power_str})"
                if is_override:
                    line += "  ⚠ override"
                if sub:
                    line += f"   ↳ sub: {sub}"
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


# Cached at first-render time so the probe doesn't run on every click.
# Lazy (not module-import) so the renderer module still loads without
# Pillow installed — the actual `render()` call raises the helpful
# RuntimeError.
_SUPPORTS_SIZE: Optional[bool] = None
_TTF_PATH: Optional[str] = None
_TTF_LOOKED_UP: bool = False

# TTF candidates probed once. Order matters — pick the most likely to
# be present on the host. DejaVu ships with most Linux distros and is
# what Pillow's default load_default falls back to on a fresh container.
_TTF_CANDIDATES = (
    "DejaVuSans.ttf",
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


def _supports_size() -> bool:
    """Pillow 10+ supports `size=` on `load_default()`. Older versions
    raise TypeError. Cached after first call so the render path doesn't
    re-probe every click."""
    global _SUPPORTS_SIZE
    if _SUPPORTS_SIZE is not None:
        return _SUPPORTS_SIZE
    try:
        from PIL import ImageFont
        ImageFont.load_default(size=10)
        _SUPPORTS_SIZE = True
    except (TypeError, ImportError):
        _SUPPORTS_SIZE = False
    return _SUPPORTS_SIZE


def _best_font(image_font_module, *, size: int):
    """Return the best font Pillow can resolve at `size`:
      1. TTF (one of `_TTF_CANDIDATES`) — covers Unicode for non-English
         alliances and emoji-in-member-names.
      2. `load_default(size=size)` — Pillow's tiny ASCII bitmap, scaled.
      3. `load_default()` — the same bitmap at native size (very old
         Pillow).

    The TTF path is cached after first call. If no TTF resolves on the
    host, every render still works (degraded), so we don't hard-fail.
    """
    global _TTF_LOOKED_UP, _TTF_PATH
    if not _TTF_LOOKED_UP:
        _TTF_LOOKED_UP = True
        for candidate in _TTF_CANDIDATES:
            try:
                image_font_module.truetype(candidate, size=size)
                _TTF_PATH = candidate
                break
            except (OSError, IOError):
                continue
    if _TTF_PATH:
        try:
            return image_font_module.truetype(_TTF_PATH, size=size)
        except (OSError, IOError):
            # Race: TTF disappeared between probe and use. Fall through.
            pass
    if _supports_size():
        return image_font_module.load_default(size=size)
    return image_font_module.load_default()


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


def _text_width(font, text: str) -> int:
    """Pixel width of `text` rendered in `font`. Approximates 8 px per
    character on very old Pillow that lacks `getbbox`."""
    if not text:
        return 0
    try:
        bbox = font.getbbox(text)
        return bbox[2] - bbox[0]
    except AttributeError:
        return len(text) * 8


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    """Word-wrap `text` so every line fits within `max_width` pixels.
    A token wider than `max_width` on its own (extremely long member
    name or one-word title) is broken character by character — never
    silently truncated. Always returns at least one line."""
    if not text:
        return [""]
    if _text_width(font, text) <= max_width:
        return [text]
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if _text_width(font, candidate) <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
            current = ""
        if _text_width(font, word) <= max_width:
            current = word
            continue
        # Token alone overflows — char-wrap it.
        buf = ""
        for ch in word:
            if _text_width(font, buf + ch) <= max_width:
                buf += ch
            else:
                if buf:
                    lines.append(buf)
                buf = ch
        current = buf
    if current:
        lines.append(current)
    return lines or [text]


def _measure_height(
    roster: RosterData,
    title_font, heading_font, body_font,
) -> int:
    """Walk the same layout the renderer will, summing y-extent so the
    canvas is sized correctly. Returns at least 200 px for the empty
    case so the resulting PNG is never visually weird."""
    title_lines = _wrap_text(
        roster.title, title_font, _WIDTH - 2 * _PADDING_X,
    )
    y = (
        _PADDING_Y
        + len(title_lines) * _line_height(title_font)
        + _TITLE_GAP
    )
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
    if session.event_date:
        from storm_date_helpers import format_event_date
        date_suffix = f" — {format_event_date(session.event_date)}"
    else:
        date_suffix = ""

    zones: list[RosterZone] = []
    paired_subs: dict[str, str] = {}
    powers: dict[str, str] = {}
    overrides: set[str] = set()

    def _format_power(p) -> str:
        if p is None:
            return "power unknown"
        try:
            from storm_strategy import format_power
            return format_power(int(p))
        except (TypeError, ValueError, ImportError):
            return str(p)

    # Walk every phase the preset declares. Flat presets resolve to a
    # single Phase-1 pass with the legacy data shape. Phase-aware presets
    # emit one zone block per (phase, zone) so the rendered PNG surfaces
    # the full migration line-up — earlier code looked at
    # `session.assignments` only and silently dropped Phase 2 / Phase 3.
    is_phased = session.is_phase_aware

    def _build_member_block(zone_name: str, phase: int) -> list[str]:
        names: list[str] = []
        assignments = session.assignments_for_phase(phase)
        pairings = session.paired_subs_for_phase(phase)
        overrides_set = session.below_floor_overrides_for_phase(phase)
        for key in assignments.get(zone_name, []):
            m = session.members.get(key)
            if not m:
                continue
            primary_name = m["name"]
            names.append(primary_name)
            powers[primary_name] = _format_power(m.get("power"))
            if key in overrides_set:
                overrides.add(primary_name)
            if session.is_paired:
                sub_key = pairings.get(key)
                if sub_key:
                    sub_m = session.members.get(sub_key)
                    if sub_m:
                        sub_name = sub_m["name"]
                        # Key the paired-sub map by primary name; if a
                        # primary is paired with different subs across
                        # phases, the latest phase wins in this dict.
                        # The phase-tagged zone-block name surfaces the
                        # per-phase context for the reader regardless.
                        paired_subs[primary_name] = sub_name
                        powers[sub_name] = _format_power(sub_m.get("power"))
                        if sub_key in overrides_set:
                            overrides.add(sub_name)
        return names

    for z in session.preset.zones:
        if is_phased:
            for phase in session.iter_phases():
                cap = int(z.max_for_phase(phase))
                names = _build_member_block(z.zone, phase)
                # Skip zones with no capacity in this phase AND no
                # assignments — empty Phase-1 center zones would
                # otherwise clutter the rendered image with empty rows.
                if cap == 0 and not names:
                    continue
                zones.append(RosterZone(
                    name=f"Phase {phase} — {z.zone}",
                    max_players=cap,
                    members=names,
                ))
        else:
            names = _build_member_block(z.zone, 1)
            zones.append(RosterZone(
                name=z.zone, max_players=int(z.max_players), members=names,
            ))

    subs = [
        session.members[k]["name"] for k in session.subs
        if k in session.members
    ]
    # Power readout for overflow subs too — leadership reading the
    # image can see why these members are in the sub block.
    for k in session.subs:
        m = session.members.get(k)
        if m is not None:
            powers[m["name"]] = _format_power(m.get("power"))

    return RosterData(
        title=f"{event_label} — {session.preset.name}{team_suffix}{date_suffix}",
        zones=zones, subs=subs, paired_subs=paired_subs,
        powers=powers, overrides=overrides,
    )

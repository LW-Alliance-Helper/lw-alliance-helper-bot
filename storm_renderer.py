"""
Pillow-based map render of a storm roster (#140 — replaces the text-
canvas v1 from #132).

Contract:

    storm_renderer.render(roster: RosterData) -> bytes  # PNG

Triggered by `[🖼️ Render image]` in the roster builder (available in
both free + structured/Premium modes). Approve & Post itself does
NOT auto-attach the PNG — the rendered image is its own artifact for
sharing with the wider alliance, separate from the leadership-facing
mail body.

The new renderer is event-type aware. Desert Storm uses the diamond
layout around Nuclear Silo with vertical spawn strips at the left and
right edges; Canyon Storm uses a wider 3-stage layout with a single
Rulebringers (blue) band at the top and split Dawnbreakers (red)
bands at the bottom. The shared `RosterData` carries enough structure
(event_type, phase_count, per-zone phase info) for the renderer to
dispatch without the caller needing to know which layout is in play.

Coordinates were extracted from the SVG mocks (`ds_layout.svg`,
`CS_layout.svg`) the alliance lead authored — every box position
matches the design 1:1, scaled by `SCALE` for crisper output.

Phase-aware zones render with `Stage N:` headers inside their
member-list pill. A member migrating across phases (e.g. Alice plays
Info Center in Phase 1, Nuclear Silo in Phase 2) shows up in two
different zone blocks, one per phase. Member-list font auto-shrinks
when content overflows the box.
"""

from __future__ import annotations

import io
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Data shapes ──────────────────────────────────────────────────────


@dataclass
class RosterZone:
    """One zone-phase slot in the rendered roster.

    Phase-aware presets emit one `RosterZone` per (zone, phase) so the
    PNG carries every phase's roster. Flat presets emit one block per
    zone with `phase = 0`. The renderer groups by `canonical_zone` to
    place each zone's icon + text pill at its layout slot, then
    stacks the per-phase member lists inside the text pill.
    """
    name: str                   # display name; may include "Stage N — " prefix
    max_players: int
    members: list[str] = field(default_factory=list)
    phase: int = 0              # 0 = flat preset, 1/2/3 = phase-aware
    canonical_zone: str = ""    # base zone name (no phase prefix) for icon lookup


@dataclass
class RosterData:
    """Everything the renderer needs to produce a PNG.

    Structured fields (`event_type`, `phase_count`, `team_label`,
    `event_date_label`, `preset_name`) drive the map dispatch and the
    header line. The map renderer is deliberately a shareable artifact
    for the wider alliance — it carries zone names, member names, and
    paired-sub formatting only. Per-member power readouts, below-floor
    override markers, and special-role footers are NOT drawn (those
    are leadership-internal signals surfaced in the builder embed and
    mail body, not the public artifact).
    """
    title: str                                      # legacy back-compat
    zones: list[RosterZone]
    subs: list[str] = field(default_factory=list)
    paired_subs: dict[str, str] = field(default_factory=dict)
    event_type: str = "DS"                          # "DS" or "CS"
    preset_name: str = ""                           # surfaces under event name
    team_label: str = ""                            # "Team A" / "Rulebringers" / ""
    event_date_label: str = ""                      # human-readable date string
    phase_count: int = 0                            # 0 = flat, 2 or 3 = phase-aware


# ── Asset paths ──────────────────────────────────────────────────────


_HERE = os.path.dirname(os.path.abspath(__file__))
_INTER_REGULAR = os.path.join(_HERE, "assets", "fonts", "Inter-Regular.ttf")
_INTER_BOLD = os.path.join(_HERE, "assets", "fonts", "Inter-Bold.ttf")
_ICONS_DS_DIR = os.path.join(_HERE, "assets", "storm_icons", "ds")
_ICONS_CS_DIR = os.path.join(_HERE, "assets", "storm_icons", "cs")

# Icon filename per canonical zone. A `None` entry falls back to the
# grey placeholder circle, intended for any future zone that ships
# before its icon does. All DS slots currently have art.
_DS_ICON_FILES: dict[str, Optional[str]] = {
    "Nuclear Silo":         "Nuclear Silo.png",
    "Oil Refinery I":       "Oil Refinery.png",
    "Oil Refinery II":      "Oil Refinery.png",
    "Science Hub":          "Science Hub.png",
    "Info Center":          "Info Center.png",
    "Field Hospital I":     "Field Hospital.png",
    "Field Hospital II":    "Field Hospital.png",
    "Field Hospital III":   "Field Hospital.png",
    "Field Hospital IV":    "Field Hospital.png",
    "Arsenal":              "Arsenal.png",
    "Mercenary Factory":    "Mercenary Factory.png",
}
_CS_ICON_FILES: dict[str, Optional[str]] = {
    "Power Tower":          "Power Tower.png",
    "Data Center 1":        "Data Center.png",
    "Data Center 2":        "Data Center.png",
    "Defense System 1":     "Defense System.png",
    "Defense System 2":     "Defense System.png",
    "Serum Factory 1":      "Serum Factory.png",
    "Serum Factory 2":      "Serum Factory.png",
    "Sample Warehouse 1":   "Sample Warehouse.png",
    "Sample Warehouse 2":   "Sample Warehouse.png",
    "Sample Warehouse 3":   "Sample Warehouse.png",
    "Sample Warehouse 4":   "Sample Warehouse.png",
    "Virus Lab":            "Virus Lab.png",
}


# ── Layout primitives ───────────────────────────────────────────────


# SVG render scale. 2× produces a sharp image at typical Discord
# zoom; bigger still fits within Discord's 8 MB attachment limit for
# typical rosters.
SCALE = 2


@dataclass(frozen=True)
class Box:
    """Pixel-coordinate box in SVG units. The renderer multiplies by
    SCALE when committing to canvas."""
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class ZoneLayout:
    """One zone's three layout pills + icon position."""
    title: Box
    text: Box
    icon: Box


@dataclass(frozen=True)
class EventLayout:
    """Per-event-type layout: canvas size, backgrounds, header, spawn
    zones, per-zone slots, subs section."""
    svg_w: float
    svg_h: float
    header: Box
    bg_main: Box
    bg_subs: Box
    # Spawn rectangles. DS = two vertical strips; CS = one blue band
    # top + two red bands bottom. Each rect carries an RGBA fill.
    spawn_rects: list[tuple[Box, tuple[int, int, int, int]]]
    zones: dict[str, ZoneLayout]                # canonical_zone → layout
    subs_title: Box
    subs_text_flat: Box
    subs_text_pairs: Box
    subs_pair_left_x: float
    subs_pair_right_x: float
    pairs_header_offset_y: float
    pairs_underline_offset_y: float
    pairs_row1_offset_y: float
    pairs_row_step: float
    pairs_divider_x0: float
    pairs_divider_x1: float


# ── DS layout ────────────────────────────────────────────────────────


_DS_LAYOUT = EventLayout(
    svg_w=1107.60, svg_h=764.26,
    header=Box(0, 0, 1107.60, 48.00),
    bg_main=Box(1.30, 46.75, 920.03, 715.24),
    bg_subs=Box(921.27, 46.71, 184.79, 715.24),
    spawn_rects=[
        # DS spawn squares — narrow vertical strips at left/right edges.
        # Game-defined colours: Team A blue, Team B red.
        (Box(0, 289.84, 38.33, 213.57),       (92, 124, 199, 255)),    # blue
        (Box(883.01, 287.32, 38.33, 213.57),  (208, 102, 99, 255)),    # red
    ],
    zones={
        "Info Center": ZoneLayout(
            title=Box(48.39, 85.63, 213.64, 16.79),
            text=Box(48.38, 106.20, 127.34, 136.60),
            icon=Box(175.08, 102.42, 96, 96),
        ),
        "Oil Refinery I": ZoneLayout(
            title=Box(58.31, 252.84, 223.34, 16.79),
            text=Box(154.31, 273.41, 127.34, 136.60),
            icon=Box(57.47, 269.52, 96, 96),
        ),
        "Field Hospital I": ZoneLayout(
            title=Box(58.31, 420.84, 223.34, 16.79),
            text=Box(154.31, 441.41, 127.34, 136.60),
            icon=Box(58.31, 437.63, 96, 96),
        ),
        "Field Hospital II": ZoneLayout(
            title=Box(51.19, 589.63, 219.91, 16.79),
            text=Box(52.01, 610.20, 127.34, 136.60),
            icon=Box(178.70, 606.42, 96, 96),
        ),
        "Arsenal": ZoneLayout(
            title=Box(399.22, 77.35, 127.34, 16.79),
            text=Box(399.22, 189.94, 127.34, 74.05),
            icon=Box(413.21, 94.13, 96, 96),
        ),
        "Nuclear Silo": ZoneLayout(
            title=Box(400.19, 329.72, 127.34, 16.79),
            text=Box(399.22, 434.32, 127.34, 74.05),
            icon=Box(415.57, 341.69, 96, 96),
        ),
        "Mercenary Factory": ZoneLayout(
            title=Box(399.22, 563.90, 128.31, 16.79),
            text=Box(399.22, 676.49, 127.34, 74.05),
            icon=Box(413.21, 580.68, 96, 96),
        ),
        "Field Hospital IV": ZoneLayout(
            title=Box(658.94, 86.16, 223.34, 16.79),
            text=Box(754.94, 106.73, 127.34, 136.60),
            icon=Box(658.95, 102.95, 96, 96),
        ),
        "Field Hospital III": ZoneLayout(
            title=Box(640.77, 252.09, 223.34, 16.79),
            text=Box(641.59, 272.65, 127.34, 136.60),
            icon=Box(768.29, 268.87, 96, 96),
        ),
        "Oil Refinery II": ZoneLayout(
            title=Box(644.13, 420.91, 223.34, 16.79),
            text=Box(644.12, 441.48, 127.34, 136.60),
            icon=Box(771.46, 437.63, 96, 96),
        ),
        "Science Hub": ZoneLayout(
            title=Box(658.94, 589.74, 223.34, 16.79),
            text=Box(754.94, 610.31, 127.34, 136.60),
            icon=Box(658.94, 606.42, 96, 96),
        ),
    },
    subs_title=Box(930.68, 78.16, 167.59, 16.79),
    subs_text_flat=Box(930.69, 106.73, 167.59, 150.61),
    subs_text_pairs=Box(930.69, 106.73, 167.59, 369.32),
    subs_pair_left_x=946.03,
    subs_pair_right_x=1018.60,
    pairs_header_offset_y=287.82 - 266.73,       # ≈ 21.09
    pairs_underline_offset_y=308.75 - 266.73,    # ≈ 42.02
    pairs_row1_offset_y=320.24 - 266.73,         # ≈ 53.51
    pairs_row_step=32.0,
    pairs_divider_x0=940.59,
    pairs_divider_x1=1086.72,
)


# ── CS layout ────────────────────────────────────────────────────────


_CS_LAYOUT = EventLayout(
    svg_w=1235.67, svg_h=1045.44,
    header=Box(0, 0, 1235.67, 48.00),
    bg_main=Box(1.30, 46.77, 1049.07, 996.44),
    bg_subs=Box(1050.26, 47.62, 184.79, 996.44),
    spawn_rects=[
        # CS spawn bands — game-defined factions.
        # Rulebringers (blue) — single horizontal band at top.
        (Box(343.01, 47.48, 349.54, 38.33),    (92, 124, 199, 255)),
        # Dawnbreakers (red) — split into two horizontal bands at bottom.
        (Box(117.97, 1004.88, 392.00, 38.33),  (208, 102, 99, 255)),
        (Box(550.35, 1004.88, 374.27, 38.33),  (208, 102, 99, 255)),
    ],
    zones={
        # Top row
        "Data Center 1": ZoneLayout(
            title=Box(246.62, 100.23, 223.34, 16.79),
            text=Box(342.62, 120.80, 127.34, 188.82),
            icon=Box(246.62, 116.89, 96, 96),
        ),
        "Data Center 2": ZoneLayout(
            title=Box(574.38, 100.23, 223.34, 16.79),
            text=Box(574.38, 120.80, 127.34, 188.82),
            icon=Box(700.91, 116.89, 96, 96),
        ),
        # Mid-upper
        "Serum Factory 1": ZoneLayout(
            title=Box(22.24, 313.41, 223.34, 16.79),
            text=Box(22.24, 333.97, 127.34, 166.65),
            icon=Box(150.62, 331.22, 96, 96),
        ),
        "Defense System 1": ZoneLayout(
            title=Box(797.71, 313.41, 223.34, 16.79),
            text=Box(893.71, 333.97, 127.34, 166.65),
            icon=Box(797.71, 331.22, 96, 96),
        ),
        "Power Tower": ZoneLayout(
            title=Box(417.83, 332.24, 223.34, 16.79),
            text=Box(514.63, 354.94, 127.34, 188.82),
            icon=Box(416.05, 348.75, 96, 96),
        ),
        # Mid-lower
        "Defense System 2": ZoneLayout(
            title=Box(22.24, 537.41, 223.34, 16.79),
            text=Box(22.24, 557.83, 127.34, 166.65),
            icon=Box(150.62, 555.22, 96, 96),
        ),
        "Serum Factory 2": ZoneLayout(
            title=Box(797.71, 537.41, 223.34, 16.79),
            text=Box(893.71, 557.83, 127.34, 166.65),
            icon=Box(797.71, 555.22, 96, 96),
        ),
        "Virus Lab": ZoneLayout(
            title=Box(417.83, 576.87, 223.34, 16.79),
            text=Box(417.83, 597.29, 127.34, 88.66),
            icon=Box(545.16, 593.62, 96, 96),
        ),
        # Bottom row
        "Sample Warehouse 1": ZoneLayout(
            title=Box(21.97, 763.69, 223.34, 16.79),
            text=Box(117.97, 784.26, 127.34, 188.82),
            icon=Box(21.97, 781.68, 96, 96),
        ),
        "Sample Warehouse 2": ZoneLayout(
            title=Box(285.97, 763.69, 223.34, 16.79),
            text=Box(381.97, 784.26, 127.34, 188.82),
            icon=Box(285.97, 781.68, 96, 96),
        ),
        "Sample Warehouse 3": ZoneLayout(
            title=Box(549.97, 763.69, 223.34, 16.79),
            text=Box(549.97, 784.26, 127.34, 188.82),
            icon=Box(677.31, 781.68, 96, 96),
        ),
        "Sample Warehouse 4": ZoneLayout(
            title=Box(797.97, 763.69, 223.34, 16.79),
            text=Box(797.97, 784.26, 127.34, 188.82),
            icon=Box(925.31, 781.68, 96, 96),
        ),
    },
    subs_title=Box(1059.68, 63.08, 167.59, 16.79),
    subs_text_flat=Box(1059.69, 91.64, 167.59, 150.61),
    subs_text_pairs=Box(1059.69, 91.64, 167.59, 369.32),
    subs_pair_left_x=1075.03,
    subs_pair_right_x=1147.59,
    # CS pairs offsets relative to a "flat-position" pairs box at
    # y=91.64. The SVG mock has the pairs version at y=251.64 in the
    # subs column; the renderer collapses them so whichever variant
    # renders sits right under the title pill.
    pairs_header_offset_y=272.74 - 251.64,
    pairs_underline_offset_y=290.05 - 251.64,
    pairs_row1_offset_y=305.05 - 251.64,
    pairs_row_step=32.0,
    pairs_divider_x0=1069.45,
    pairs_divider_x1=1215.58,
)


_LAYOUTS: dict[str, EventLayout] = {
    "DS": _DS_LAYOUT,
    "CS": _CS_LAYOUT,
}


# ── Colours ──────────────────────────────────────────────────────────


_SAND = (218, 178, 130, 255)
_HEADER_FILL = (67, 67, 67, 255)
_HEADER_TEXT = (245, 245, 245, 255)
_PILL_FILL = (255, 255, 255, 126)
_PILL_OUTLINE = (0, 0, 0, 255)
_SPAWN_OUTLINE = (40, 40, 40, 255)
_PLACEHOLDER_FILL = (217, 217, 217, 255)
_PLACEHOLDER_OUTLINE = (0, 0, 0, 255)
_TEXT_DARK = (20, 20, 20, 255)
_TEXT_MUTED = (60, 60, 60, 255)
_PAIRS_UNDERLINE_COLOR = (158, 158, 158, 255)
_PAIRS_DIVIDER_COLOR = (153, 153, 153, 255)
_PAIRS_UNDERLINE_WIDTH_SVG = 3
_PAIRS_DIVIDER_WIDTH_SVG = 2


# ── Font sizing ──────────────────────────────────────────────────────


# Locked sizes per the alliance lead's design spec (10 pt labels +
# members, 14 pt header). Converted from typographic points to SCALEd
# pixels via the 96-DPI web convention. Label size bumped from 8 to 10
# post-#222 once inline ` + sub <name>` dropped from per-zone labels —
# bare primary names stay under the in-game 20-char username max, so
# the pill has the horizontal room for the larger font. The shrink
# fallback in `_pick_member_fonts` still handles long sheet-alias
# names that exceed the in-game limit.
_LABEL_PT = 10
_HEADER_PT = 14

# Subs panel table font (paired mode). The right-side `Subs` pill
# renders two side-by-side columns (`Primary` | `Sub`), and each
# column is ~half the panel width. At 10 pt bold a 20-char in-game
# username fills the column with no gap, so adjacent rows like
# `Bobby1269` `KayyyShawty` end up touching. 8 pt keeps two 20-char
# names cleanly separated in the table while preserving the panel
# header at the larger size for hierarchy. Only applies inside
# `_draw_subs_section` — zone-pill labels stay at `_LABEL_PT`.
_SUBS_TABLE_PT = 8


def _pt_to_px(pt: float) -> int:
    return int(round(pt * 96 / 72 * SCALE))


# ── Public API ───────────────────────────────────────────────────────


def render(roster: RosterData) -> bytes:
    """Render the roster to a PNG byte-string.

    Raises `RuntimeError` if Pillow isn't installed — caller should
    catch and fall back to text-only output.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise RuntimeError(
            "Pillow isn't installed — `/desertstorm` and `/canyonstorm` "
            "image render unavailable. Add `Pillow>=10.0.0` to requirements.txt."
        ) from e

    layout = _LAYOUTS.get(roster.event_type.upper(), _DS_LAYOUT)
    canvas_w = int(round(layout.svg_w * SCALE))
    canvas_h = int(round(layout.svg_h * SCALE))
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (0, 0, 0, 0))

    # 1. Backgrounds: main play area + subs column. Subs column
    #    carries a 1 px black stroke whose left edge is the vertical
    #    separator between the map and the subs section.
    bg_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg_layer)
    bg_draw.rectangle(_s_box(layout.bg_main), fill=_SAND)
    bg_draw.rectangle(_s_box(layout.bg_subs), fill=_SAND,
                      outline=(0, 0, 0, 255), width=max(1, SCALE // 2))
    canvas.alpha_composite(bg_layer)

    # 2. Header bar with event / team / date.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_header(d, layout, roster)
    canvas.alpha_composite(layer)

    # 3. Spawn zones.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for spawn_box, color in layout.spawn_rects:
        d.rectangle(_s_box(spawn_box), fill=color,
                    outline=_SPAWN_OUTLINE, width=max(1, SCALE // 2))
    canvas.alpha_composite(layer)

    # 4. Zones. Group input zones by `canonical_zone` so each layout
    #    slot renders once with every phase's members stacked inside.
    grouped: dict[str, list[RosterZone]] = {}
    for z in roster.zones:
        key = z.canonical_zone or z.name
        grouped.setdefault(key, []).append(z)
    icon_files = (_DS_ICON_FILES if roster.event_type.upper() == "DS"
                  else _CS_ICON_FILES)
    icons_dir = (_ICONS_DS_DIR if roster.event_type.upper() == "DS"
                 else _ICONS_CS_DIR)

    for canonical, phase_blocks in grouped.items():
        zlayout = layout.zones.get(canonical)
        if zlayout is None:
            # Non-canonical zone name — skip silently. Pre-#152
            # presets with typo zones would otherwise crash here.
            logger.debug("render: skipping unknown zone %r for %s",
                         canonical, roster.event_type)
            continue
        _draw_zone(canvas, zlayout, canonical, phase_blocks,
                   icon_files, icons_dir, roster)

    # 5. Subs section — picks flat or pairs variant on data shape.
    _draw_subs_section(canvas, layout, roster)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ── Drawing helpers ──────────────────────────────────────────────────


def _s(v: float) -> int:
    return int(round(v * SCALE))


def _s_box(b: Box) -> tuple[int, int, int, int]:
    return _s(b.x), _s(b.y), _s(b.x + b.w), _s(b.y + b.h)


def _try_font(size: int, bold: bool = False):
    """Inter is the project font (bundled at `assets/fonts/`). Falls
    back to DejaVu / Arial if the bundled files aren't present —
    keeps `render()` non-fatal in environments where assets weren't
    copied (e.g. partial deployments)."""
    from PIL import ImageFont
    candidates = [_INTER_BOLD if bold else _INTER_REGULAR]
    candidates.extend([
        "arialbd.ttf" if bold else "arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ])
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_header(draw, layout: EventLayout, roster: RosterData) -> None:
    """Header strip: charcoal bar with left / center / right text.
    Left text combines the event name with the preset name when
    present so the rendered image is self-identifying when alliances
    save them for records."""
    canvas_w = int(round(layout.svg_w * SCALE))
    draw.rectangle((0, 0, canvas_w, _s(layout.header.h)), fill=_HEADER_FILL)
    font = _try_font(_pt_to_px(_HEADER_PT), bold=True)
    pad_x = _s(15)
    text_y = _s(layout.header.h / 2) - int(font.size * 0.55)

    event_full = "Desert Storm" if roster.event_type.upper() == "DS" else "Canyon Storm"
    if roster.preset_name:
        left_text = f"{event_full} — {roster.preset_name}"
    else:
        left_text = event_full
    draw.text((pad_x, text_y), left_text, fill=_HEADER_TEXT, font=font)

    if roster.team_label:
        tw = draw.textlength(roster.team_label, font=font)
        draw.text(((canvas_w - tw) / 2, text_y),
                  roster.team_label, fill=_HEADER_TEXT, font=font)

    if roster.event_date_label:
        tw = draw.textlength(roster.event_date_label, font=font)
        draw.text((canvas_w - pad_x - tw, text_y),
                  roster.event_date_label, fill=_HEADER_TEXT, font=font)


def _draw_pill(draw, b: Box, radius_svg: float) -> None:
    x0, y0, x1, y1 = _s_box(b)
    r = int(round(radius_svg * SCALE))
    draw.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=_PILL_FILL)
    draw.rounded_rectangle((x0, y0, x1, y1), radius=r,
                           outline=_PILL_OUTLINE, width=max(1, SCALE // 2))


def _draw_centered_text(draw, b: Box, text: str, font, fill) -> None:
    x0, y0, x1, y1 = _s_box(b)
    tw = draw.textlength(text, font=font)
    tx = (x0 + x1) / 2 - tw / 2
    ty = (y0 + y1) / 2 - font.size * 0.55
    draw.text((tx, ty), text, fill=fill, font=font)


def _draw_icon(canvas, draw, zlayout: ZoneLayout, canonical: str,
               icon_files: dict, icons_dir: str) -> None:
    """Place the zone icon at its layout position. Falls back to a
    grey placeholder circle if the icon file is missing — a defensive
    guard for any future zone that ships before its art does."""
    from PIL import Image
    x0, y0, x1, y1 = _s_box(zlayout.icon)
    icon_name = icon_files.get(canonical)
    if icon_name:
        path = os.path.join(icons_dir, icon_name)
        try:
            icon = Image.open(path).convert("RGBA")
            icon = icon.resize((x1 - x0, y1 - y0), Image.LANCZOS)
            canvas.alpha_composite(icon, (x0, y0))
            return
        except (OSError, IOError) as e:
            logger.debug("render: icon load failed for %s (%s) — placeholder",
                         canonical, e)
    draw.ellipse((x0, y0, x1, y1),
                 fill=_PLACEHOLDER_FILL, outline=_PLACEHOLDER_OUTLINE,
                 width=max(1, SCALE // 2))


def _measure_member_block(phase_blocks: list[RosterZone],
                          fh, fm, line_gap: int, block_gap: int) -> int:
    """Vertical space the member list will consume at the given font
    sizes. Used by `_pick_member_fonts` to auto-shrink content that
    overflows the text pill."""
    h = 0
    first = True
    for block in phase_blocks:
        if not block.members and block.phase == 0:
            continue
        if not first:
            h += block_gap
        first = False
        if block.phase >= 1:
            h += fh.size + line_gap
        h += len(block.members) * (fm.size + line_gap)
    return h


def _pick_member_fonts(phase_blocks: list[RosterZone], max_h: int):
    """Pick bold-header + regular-name fonts that fit `max_h`. Starts
    at the locked 8-pt size and shrinks until content fits."""
    base = _pt_to_px(_LABEL_PT)
    for shrink in (0, 2, 4, 6, 8, 10):
        sz = max(10, base - shrink)
        fh = _try_font(sz, bold=True)
        fm = _try_font(sz, bold=False)
        if _measure_member_block(phase_blocks, fh, fm, 4, 8) <= max_h:
            return fh, fm
    return _try_font(10, bold=True), _try_font(10)


def _draw_member_block(draw, b: Box, phase_blocks: list[RosterZone],
                       paired_subs: dict[str, str], is_paired: bool) -> None:
    """Render the member list inside a zone's text pill. Phase-aware
    blocks get a bold `Stage N:` header; flat blocks render the
    member list directly.

    Post-#222: zone pills render bare primary names. Pairings live in
    the right-side `Subs` panel (Primary / Sub table) so inline
    ` + sub Bob` was redundant and was the only thing pushing labels
    past the pill width. `paired_subs` / `is_paired` are kept in the
    signature so callers don't need to change, but they're unused.
    """
    del paired_subs, is_paired  # rendered in the Subs panel instead.
    x0, y0, x1, y1 = _s_box(b)
    pad = max(8, _s(6))
    py = max(6, _s(5))
    avail_h = (y1 - y0) - 2 * py
    line_gap = max(2, _s(2))
    block_gap = max(4, _s(4))
    fh, fm = _pick_member_fonts(phase_blocks, avail_h)
    cy = y0 + py
    indent = max(8, _s(6))
    first = True
    for block in sorted(phase_blocks, key=lambda z: z.phase):
        if not block.members and block.phase == 0:
            continue
        if not first:
            cy += block_gap
        first = False
        if block.phase >= 1:
            draw.text((x0 + pad, cy),
                      f"Stage {block.phase}:",
                      fill=_TEXT_DARK, font=fh)
            cy += fh.size + line_gap
        for name in block.members:
            draw.text((x0 + pad + indent, cy),
                      name, fill=_TEXT_MUTED, font=fm)
            cy += fm.size + line_gap


def _draw_zone(canvas, zlayout: ZoneLayout, canonical: str,
               phase_blocks: list[RosterZone],
               icon_files: dict, icons_dir: str,
               roster: RosterData) -> None:
    """Render the three pills + icon for one canonical zone slot.
    `phase_blocks` is the list of `RosterZone` entries for this zone
    (one per phase for phase-aware presets, one total for flat)."""
    from PIL import Image, ImageDraw
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_pill(d, zlayout.title, radius_svg=zlayout.title.h / 2)
    _draw_pill(d, zlayout.text,
               radius_svg=min(zlayout.text.w, zlayout.text.h) / 9)
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_centered_text(d, zlayout.title, canonical,
                        _try_font(_pt_to_px(_LABEL_PT), bold=True), _TEXT_DARK)
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_icon(canvas, d, zlayout, canonical, icon_files, icons_dir)
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    is_paired = bool(roster.paired_subs)
    _draw_member_block(d, zlayout.text, phase_blocks,
                       roster.paired_subs, is_paired)
    canvas.alpha_composite(layer)


def _draw_subs_section(canvas, layout: EventLayout,
                       roster: RosterData) -> None:
    """Subs column on the right. Chooses the pairs variant when
    `paired_subs` carries entries; flat list otherwise."""
    from PIL import Image, ImageDraw
    use_pairs = bool(roster.paired_subs)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_pill(d, layout.subs_title, radius_svg=layout.subs_title.h / 2)
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_centered_text(d, layout.subs_title, "Subs",
                        _try_font(_pt_to_px(_LABEL_PT), bold=True), _TEXT_DARK)
    canvas.alpha_composite(layer)

    content_box = layout.subs_text_pairs if use_pairs else layout.subs_text_flat
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_pill(d, content_box,
               radius_svg=min(content_box.w, content_box.h) / 9)
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad_x = max(8, _s(6))
    pad_y = max(6, _s(6))
    line_gap = max(2, _s(3))
    # Paired mode uses _SUBS_TABLE_PT so two 20-char usernames fit
    # side-by-side in the Primary / Sub columns. Flat mode uses the
    # default label size since the single column is the full panel
    # width.
    pairs_font_px = _pt_to_px(_SUBS_TABLE_PT)
    flat_font_px = _pt_to_px(_LABEL_PT)
    if use_pairs:
        fm = _try_font(pairs_font_px, bold=False)
        fm_bold = _try_font(pairs_font_px, bold=True)
    else:
        fm = _try_font(flat_font_px, bold=False)
        fm_bold = _try_font(flat_font_px, bold=True)
    x0, y0, x1, y1 = _s_box(content_box)
    cy = y0 + pad_y

    if use_pairs:
        # Table: "Primary" / "Sub" headers, thick underline, one row per
        # pair with a thin divider between rows.
        box_top = content_box.y
        primary_x = _s(layout.subs_pair_left_x)
        sub_x = _s(layout.subs_pair_right_x)
        header_y = _s(box_top + layout.pairs_header_offset_y)
        underline_y = _s(box_top + layout.pairs_underline_offset_y)
        row1_y = _s(box_top + layout.pairs_row1_offset_y)
        row_step_px = _s(layout.pairs_row_step)

        d.text((primary_x, header_y), "Primary",
               fill=_TEXT_DARK, font=fm_bold)
        d.text((sub_x, header_y), "Sub",
               fill=_TEXT_DARK, font=fm_bold)
        d.line(
            (_s(layout.pairs_divider_x0), underline_y,
             _s(layout.pairs_divider_x1), underline_y),
            fill=_PAIRS_UNDERLINE_COLOR,
            width=max(1, _s(_PAIRS_UNDERLINE_WIDTH_SVG)),
        )
        pairs_list = list(roster.paired_subs.items())
        for i, (primary, sub) in enumerate(pairs_list):
            row_y = row1_y + i * row_step_px
            if row_y + fm.size > y1 - pad_y:
                break
            d.text((primary_x, row_y), primary,
                   fill=_TEXT_DARK, font=fm)
            d.text((sub_x, row_y), sub,
                   fill=_TEXT_DARK, font=fm)
            if i < len(pairs_list) - 1:
                div_y = row_y + _s(layout.pairs_row_step * 0.625)
                if div_y < y1 - 4:
                    d.line(
                        (_s(layout.pairs_divider_x0), div_y,
                         _s(layout.pairs_divider_x1), div_y),
                        fill=_PAIRS_DIVIDER_COLOR,
                        width=max(1, _s(_PAIRS_DIVIDER_WIDTH_SVG)),
                    )
    else:
        # Flat list — one sub per row.
        for name in roster.subs:
            if cy + fm.size > y1 - pad_y:
                break
            d.text((x0 + pad_x + 4, cy), name, fill=_TEXT_DARK, font=fm)
            cy += fm.size + line_gap

    canvas.alpha_composite(layer)


# ── Conversion helper from a RosterBuilderSession ────────────────────


def roster_from_session(session) -> RosterData:
    """Build a `RosterData` from a `RosterBuilderSession` for the
    builder's `[🖼️ Render image]` button. Lives in the renderer rather
    than the builder so `storm_roster_builder` doesn't import Pillow
    at module-load time — Pillow's import path is heavy and the
    free-tier builder shouldn't pay the cost unless image render is
    actually used.
    """
    event_full = "Desert Storm" if session.event_type == "DS" else "Canyon Storm"
    team_label = ""
    team_suffix = ""
    if session.team:
        # Two-team events (DS or CS with teams=both) suffix with the team.
        team_label = f"Team {session.team}"
        team_suffix = f" — {team_label}"
    elif session.preset.faction and session.preset.faction != "Either":
        team_label = session.preset.faction
        team_suffix = f" — {team_label}"

    event_date_label = ""
    date_suffix = ""
    if session.event_date:
        from storm_date_helpers import format_event_date
        event_date_label = format_event_date(session.event_date)
        date_suffix = f" — {event_date_label}"

    zones: list[RosterZone] = []
    paired_subs: dict[str, str] = {}

    is_phased = session.is_phase_aware

    def _build_member_block(zone_name: str, phase: int) -> list[str]:
        names: list[str] = []
        assignments = session.assignments_for_phase(phase)
        pairings = session.paired_subs_for_phase(phase)
        for key in assignments.get(zone_name, []):
            m = session.members.get(key)
            if not m:
                continue
            primary_name = m["name"]
            names.append(primary_name)
            if session.is_paired:
                sub_key = pairings.get(key)
                if sub_key:
                    sub_m = session.members.get(sub_key)
                    if sub_m:
                        paired_subs[primary_name] = sub_m["name"]
        return names

    for z in session.preset.zones:
        if is_phased:
            for phase in session.iter_phases():
                cap = int(z.max_for_phase(phase))
                names = _build_member_block(z.zone, phase)
                # Skip empty phase blocks for zones that don't
                # participate in this phase (Phase-1 center zones in
                # DS, Phase-1/Phase-2 Virus Lab in CS).
                if cap == 0 and not names:
                    continue
                zones.append(RosterZone(
                    name=f"Stage {phase} — {z.zone}",
                    max_players=cap,
                    members=names,
                    phase=phase,
                    canonical_zone=z.zone,
                ))
        else:
            names = _build_member_block(z.zone, 1)
            zones.append(RosterZone(
                name=z.zone, max_players=int(z.max_players),
                members=names, phase=0, canonical_zone=z.zone,
            ))

    subs = [
        session.members[k]["name"] for k in session.subs
        if k in session.members
    ]

    return RosterData(
        title=f"{event_full} — {session.preset.name}{team_suffix}{date_suffix}",
        zones=zones, subs=subs, paired_subs=paired_subs,
        event_type=session.event_type,
        preset_name=session.preset.name,
        team_label=team_label,
        event_date_label=event_date_label,
        phase_count=int(getattr(session.preset, "phase_count", 0) or 0),
    )

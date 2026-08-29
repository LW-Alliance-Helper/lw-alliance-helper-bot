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

import functools
import io
import logging
import os
import unicodedata
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

    name: str  # display name; may include "Stage N — " prefix
    max_players: int
    members: list[str] = field(default_factory=list)
    phase: int = 0  # 0 = flat preset, 1/2/3 = phase-aware
    canonical_zone: str = ""  # base zone name (no phase prefix) for icon lookup


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

    title: str  # legacy back-compat
    zones: list[RosterZone]
    subs: list[str] = field(default_factory=list)
    paired_subs: dict[str, str] = field(default_factory=dict)
    event_type: str = "DS"  # "DS" or "CS"
    preset_name: str = ""  # surfaces under event name
    team_label: str = ""  # "Team A" / "Rulebringers" / ""
    event_date_label: str = ""  # human-readable date string
    phase_count: int = 0  # 0 = flat, 2 or 3 = phase-aware
    # Populated by `render()` after the slot-flow layout runs (#227):
    # any names that couldn't fit inside their zone's pill go here so
    # the caller can warn the officer in a post-Approve ephemeral.
    overflow: list = field(default_factory=list)


# ── Asset paths ──────────────────────────────────────────────────────


_HERE = os.path.dirname(os.path.abspath(__file__))
_BUNDLED_FONT_DIR = os.path.join(_HERE, "assets", "fonts")
_INTER_REGULAR = os.path.join(_BUNDLED_FONT_DIR, "Inter-Regular.ttf")
_INTER_BOLD = os.path.join(_BUNDLED_FONT_DIR, "Inter-Bold.ttf")
_NOTO_CJK_REGULAR = os.path.join(_BUNDLED_FONT_DIR, "NotoSansCJKsc-Regular.otf")
_NOTO_ARABIC_REGULAR = os.path.join(_BUNDLED_FONT_DIR, "NotoSansArabic-Regular.ttf")

# Where an *installed* family lands. `fonts-noto-core` unpacks into the
# first entry (see `nixpacks.toml` / `railpack.json`); DejaVu is what the
# base image already carries and is what `_try_font` has always fallen
# back to. Deliberately Linux-only: a developer's Windows font folder
# would make a local render disagree with the container's.
_SYSTEM_FONT_DIRS = (
    "/usr/share/fonts/truetype/noto",
    "/usr/share/fonts/opentype/noto",
    "/usr/share/fonts/truetype/dejavu",
)


@dataclass(frozen=True)
class _FontFamily:
    """One entry in `_FONT_STACK`.

    `regular` / `bold` are file *names*, resolved against the bundled
    asset folder first and the container's font directories second, so
    a bundled file always wins over an installed one of the same name.
    An empty `bold` means the family ships Regular only and a bold
    request gets the Regular weight.
    """

    key: str
    regular: tuple[str, ...]
    bold: tuple[str, ...]
    bundled: bool  # committed to this repo, so absence is a broken deploy
    covers: str  # what it is here for, printed by `log_font_coverage`


# The fallback order, and the order is the load-bearing part.
#
# 1. Inter first: it is the project face and the only one with a Bold
#    weight bundled, so every name it CAN draw looks like the rest of
#    the card.
# 2. Noto Sans second, ahead of every script-specific face. It is what
#    closes the largest gap by a distance — Latin Extended-A (Turkish
#    `ı ğ ş`, Polish `ł ą`, Czech `č`, Romanian `ă`, ~45,000 players),
#    Ukrainian `і`, Macedonian `Ѕ`, Greek, and the small-caps /
#    phonetic / modifier letters players style their names with — which
#    between them accounted for nearly all of the 232 names in the
#    roster measurement that no bundled font could draw (115 Latin
#    extended and decorative, 85 Phonetic Extensions, 37 modifier
#    letters, categories that overlap). Not exotic alphabets.
#    Put it below the CJK or Arabic faces and a Turkish name renders
#    correctly in a completely different typeface from the name beside
#    it.
# 3. Then the scripts, heaviest player population first, so the common
#    case touches the fewest files.
# 4. DejaVu last: broad, already present, and the least like Inter.
_FONT_STACK: tuple[_FontFamily, ...] = (
    _FontFamily("inter", ("Inter-Regular.ttf",), ("Inter-Bold.ttf",), True, "Latin"),
    _FontFamily(
        "noto",
        ("NotoSans-Regular.ttf",),
        ("NotoSans-Bold.ttf",),
        False,
        "Latin Extended, Cyrillic, Greek, Vietnamese, phonetic + modifier letters",
    ),
    _FontFamily(
        "cjk",
        ("NotoSansCJKsc-Regular.otf",),
        (),
        True,
        "Japanese, Korean, Han, Vietnamese",
    ),
    _FontFamily(
        "arabic",
        ("NotoSansArabic-Regular.ttf",),
        ("NotoSansArabic-Bold.ttf",),
        True,
        "Arabic",
    ),
    _FontFamily("thai", ("NotoSansThai-Regular.ttf",), ("NotoSansThai-Bold.ttf",), False, "Thai"),
    _FontFamily(
        "devanagari",
        ("NotoSansDevanagari-Regular.ttf",),
        ("NotoSansDevanagari-Bold.ttf",),
        False,
        "Devanagari (Hindi, Nepali, Marathi)",
    ),
    _FontFamily(
        "tamil", ("NotoSansTamil-Regular.ttf",), ("NotoSansTamil-Bold.ttf",), False, "Tamil"
    ),
    _FontFamily(
        "hebrew", ("NotoSansHebrew-Regular.ttf",), ("NotoSansHebrew-Bold.ttf",), False, "Hebrew"
    ),
    _FontFamily(
        "georgian",
        ("NotoSansGeorgian-Regular.ttf",),
        ("NotoSansGeorgian-Bold.ttf",),
        False,
        "Georgian",
    ),
    _FontFamily(
        "bengali", ("NotoSansBengali-Regular.ttf",), ("NotoSansBengali-Bold.ttf",), False, "Bengali"
    ),
    _FontFamily(
        "khmer", ("NotoSansKhmer-Regular.ttf",), ("NotoSansKhmer-Bold.ttf",), False, "Khmer"
    ),
    _FontFamily(
        "myanmar", ("NotoSansMyanmar-Regular.ttf",), ("NotoSansMyanmar-Bold.ttf",), False, "Myanmar"
    ),
    _FontFamily("lao", ("NotoSansLao-Regular.ttf",), ("NotoSansLao-Bold.ttf",), False, "Lao"),
    _FontFamily(
        "armenian",
        ("NotoSansArmenian-Regular.ttf",),
        ("NotoSansArmenian-Bold.ttf",),
        False,
        "Armenian",
    ),
    _FontFamily(
        "sinhala", ("NotoSansSinhala-Regular.ttf",), ("NotoSansSinhala-Bold.ttf",), False, "Sinhala"
    ),
    _FontFamily(
        "dejavu",
        ("DejaVuSans.ttf",),
        ("DejaVuSans-Bold.ttf",),
        False,
        "broad last resort",
    ),
)


_ICONS_DS_DIR = os.path.join(_HERE, "assets", "storm_icons", "ds")
_ICONS_CS_DIR = os.path.join(_HERE, "assets", "storm_icons", "cs")

# Icon filename per canonical zone. A `None` entry falls back to the
# grey placeholder circle, intended for any future zone that ships
# before its icon does. All DS slots currently have art.
_DS_ICON_FILES: dict[str, Optional[str]] = {
    "Nuclear Silo": "Nuclear Silo.png",
    "Oil Refinery I": "Oil Refinery.png",
    "Oil Refinery II": "Oil Refinery.png",
    "Science Hub": "Science Hub.png",
    "Info Center": "Info Center.png",
    "Field Hospital I": "Field Hospital.png",
    "Field Hospital II": "Field Hospital.png",
    "Field Hospital III": "Field Hospital.png",
    "Field Hospital IV": "Field Hospital.png",
    "Arsenal": "Arsenal.png",
    "Mercenary Factory": "Mercenary Factory.png",
}
_CS_ICON_FILES: dict[str, Optional[str]] = {
    "Power Tower": "Power Tower.png",
    "Data Center 1": "Data Center.png",
    "Data Center 2": "Data Center.png",
    "Defense System 1": "Defense System.png",
    "Defense System 2": "Defense System.png",
    "Serum Factory 1": "Serum Factory.png",
    "Serum Factory 2": "Serum Factory.png",
    "Sample Warehouse 1": "Sample Warehouse.png",
    "Sample Warehouse 2": "Sample Warehouse.png",
    "Sample Warehouse 3": "Sample Warehouse.png",
    "Sample Warehouse 4": "Sample Warehouse.png",
    "Virus Lab": "Virus Lab.png",
}


# ── Layout primitives ───────────────────────────────────────────────


# SVG render scale. 2× produces a sharp image at typical Discord
# zoom; bigger still fits within Discord's 8 MB attachment limit for
# typical rosters.
SCALE = 2


# Locked sizes per the alliance lead's design spec. Converted from
# typographic points to SCALEd pixels via the 96-DPI web convention.
#
# Post-#227 redesign: every label inside a zone pill (title pill text
# + member text) is 8 pt. The new pill-rendering engine packs names
# into slot grids sized to fit 20-char in-game names at 8 pt; no font
# auto-shrink anywhere. The canvas header (event / team / date)
# stays at 14 pt bold.
_LABEL_PT = 8
_HEADER_PT = 14

# Subs panel table font (paired mode). Same 8 pt as label text now
# that label and table both target the 20-char username max at the
# same point size. Kept as a separate constant so the table can shrink
# independently if the subs panel layout ever changes.
_SUBS_TABLE_PT = 8

# Long-name threshold. Names with `> _LONG_NAME_CHARS` rendered glyph
# count are treated as "wide" and take a full row inside their pill,
# absorbing the other column slots in that row. Slot widths are sized
# to fit this many characters at `_LABEL_PT`.
_LONG_NAME_CHARS = 20

# Max rows allowed inside a central-column zone pill (DS Arsenal /
# Nuclear Silo / Mercenary Factory). Outer pills grow vertically until
# they collide with the next zone; central pills cap here so the icon
# above stays visible.
_CENTRAL_MAX_ROWS = 10

# Max content rows for outer zones (DS left / right columns, all CS
# zones). Was 7 (pre-#228 dev review picked it for a 2-stage layout
# with tight vertical spacing). Bumped to 10 on 2026-05-23 after the
# DS / CS canvas-height bumps gave each row ~30 SVG more vertical
# headroom, and CS 3-stage rosters with 4 names per stage need
# more content rows before tipping into overflow. 10 rows at ~20
# SVG/row + 3 stage headers + padding ≈ 270 SVG, which still fits
# comfortably under the ~360 SVG vertical lane on the bumped
# layouts.
_OUTER_MAX_ROWS = 10


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
    """One zone's three layout pills + icon position.

    Post-#227: the `text` Box is treated as the *anchor* for the
    member-list pill (top-left corner + maximum width). Actual pill
    height is computed at render time from the content via the
    `max_cols` slot grid plus the optional `max_rows` cap. Pills grow
    downward beyond `text.h` when content requires it; `text.h` is
    used only as the *minimum* pill height so empty zones still render
    a visible pill.

    `max_cols` is the number of name slots packed horizontally inside
    the pill. Outer DS zones (left/right map columns) use 2 cols;
    central DS zones (Arsenal / Nuclear Silo / Mercenary Factory)
    use 3. CS uses 2 across the board.

    `max_rows` caps the vertical row count for central zones (7 per
    the alliance lead's spec) so the pill can't outgrow the space
    below the icon. Outer zones leave this as `None` and accept
    however many rows the content needs.
    """

    title: Box
    text: Box
    icon: Box
    max_cols: int = 2
    max_rows: Optional[int] = None
    # Override the auto-detected pill extend direction. Used by CS
    # PT/VL where the pill sits to the side of the icon but should
    # nonetheless extend in BOTH directions to use all neighbour-
    # budget rather than only the side away from the icon. Values:
    # "left" / "right" / "both" / None (auto-detect).
    extend_direction: Optional[str] = None


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
    zones: dict[str, ZoneLayout]  # canonical_zone → layout
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
    # Canvas height bumped from 764 → 884 SVG (+120) per tester
    # feedback that Oil Refinery II's pill overlapped Science Hub's
    # icon when both stages had 5-6 names. Each of the four outer
    # rows is shifted down by ~30 SVG, central rows track halfway
    # between them — pills now have ~90 SVG of breathing room
    # between the bottom of the row-3 pill and the row-4 icon
    # (previously ~30 SVG, which clipped at typical roster size).
    svg_w=1107.60,
    svg_h=884.26,
    header=Box(0, 0, 1107.60, 48.00),
    bg_main=Box(1.30, 46.75, 920.03, 835.24),
    bg_subs=Box(921.27, 46.71, 184.79, 835.24),
    spawn_rects=[
        # DS spawn squares — narrow vertical strips at left/right edges.
        # Game-defined colours: Team A blue, Team B red. Centered
        # vertically with the new taller canvas (+60 SVG).
        (Box(0, 349.84, 38.33, 213.57), (92, 124, 199, 204)),  # blue
        (Box(883.01, 347.32, 38.33, 213.57), (208, 102, 99, 204)),  # red
    ],
    zones={
        "Info Center": ZoneLayout(
            title=Box(48.39, 85.63, 223.34, 16.79),
            text=Box(48.38, 106.20, 127.34, 60.00),
            icon=Box(175.08, 102.42, 96, 96),
        ),
        "Oil Refinery I": ZoneLayout(
            title=Box(58.31, 282.84, 223.34, 16.79),
            text=Box(154.31, 303.41, 127.34, 60.00),
            icon=Box(57.47, 299.52, 96, 96),
        ),
        "Field Hospital I": ZoneLayout(
            title=Box(58.31, 480.84, 223.34, 16.79),
            text=Box(154.31, 501.41, 127.34, 60.00),
            icon=Box(58.31, 497.63, 96, 96),
        ),
        "Field Hospital II": ZoneLayout(
            title=Box(51.19, 679.63, 223.34, 16.79),
            text=Box(52.01, 700.20, 127.34, 60.00),
            icon=Box(178.70, 696.42, 96, 96),
        ),
        # Central DS zones use 3 column slots (vs 2 for outer) and cap
        # at 7 vertical rows so the pill can't outgrow the space below
        # the icon. Pill width widened to 200 SVG units so 3 slots fit
        # at 8 pt + the long-name absorption case.
        "Arsenal": ZoneLayout(
            title=Box(362.89, 77.35, 200.00, 16.79),
            text=Box(362.89, 189.94, 200.00, 60.00),
            icon=Box(413.21, 94.13, 96, 96),
            max_cols=3,
            max_rows=_CENTRAL_MAX_ROWS,
        ),
        "Nuclear Silo": ZoneLayout(
            title=Box(362.89, 374.72, 200.00, 16.79),
            text=Box(362.89, 479.32, 200.00, 60.00),
            icon=Box(415.57, 386.69, 96, 96),
            max_cols=3,
            max_rows=_CENTRAL_MAX_ROWS,
        ),
        "Mercenary Factory": ZoneLayout(
            title=Box(362.89, 653.90, 200.00, 16.79),
            text=Box(362.89, 766.49, 200.00, 60.00),
            icon=Box(413.21, 670.68, 96, 96),
            max_cols=3,
            max_rows=_CENTRAL_MAX_ROWS,
        ),
        "Field Hospital IV": ZoneLayout(
            title=Box(658.94, 86.16, 223.34, 16.79),
            text=Box(754.94, 106.73, 127.34, 60.00),
            icon=Box(658.95, 102.95, 96, 96),
        ),
        "Field Hospital III": ZoneLayout(
            title=Box(640.77, 282.09, 223.34, 16.79),
            text=Box(641.59, 302.65, 127.34, 60.00),
            icon=Box(768.29, 298.87, 96, 96),
        ),
        "Oil Refinery II": ZoneLayout(
            title=Box(644.13, 480.91, 223.34, 16.79),
            text=Box(644.12, 501.48, 127.34, 60.00),
            icon=Box(771.46, 497.63, 96, 96),
        ),
        "Science Hub": ZoneLayout(
            title=Box(658.94, 679.74, 223.34, 16.79),
            text=Box(754.94, 700.31, 127.34, 60.00),
            icon=Box(658.94, 696.42, 96, 96),
        ),
    },
    subs_title=Box(930.68, 78.16, 167.59, 16.79),
    # Subs section grows with the taller canvas so the pair table has
    # room to render long pairings without bumping into the logo.
    subs_text_flat=Box(930.69, 106.73, 167.59, 270.61),
    subs_text_pairs=Box(930.69, 106.73, 167.59, 489.32),
    subs_pair_left_x=946.03,
    subs_pair_right_x=1018.60,
    pairs_header_offset_y=287.82 - 266.73,  # ≈ 21.09
    pairs_underline_offset_y=308.75 - 266.73,  # ≈ 42.02
    pairs_row1_offset_y=320.24 - 266.73,  # ≈ 53.51
    pairs_row_step=32.0,
    pairs_divider_x0=940.59,
    pairs_divider_x1=1086.72,
)


# ── CS layout ────────────────────────────────────────────────────────


_CS_LAYOUT = EventLayout(
    # Canvas restructured 2026-05-23 (round 2): tester report on the
    # CS preview — SW row was crowding the bottom spawn band and
    # adjacent SW pills collided when long names landed in them.
    # Three changes:
    #   1. SW row: icon ABOVE pill (was beside); pill spans the full
    #      lane width so 2-col rosters fit without wrap, eliminating
    #      the SW1↔SW2 / SW3↔SW4 pill-collision case.
    #   2. SF / DS edge zones pulled in 30 SVG from canvas edges so
    #      long-name pills extend toward the centre without clipping
    #      off the canvas.
    #   3. Canvas height bumped to 1240 SVG (+212 from prior 1028)
    #      so 3-stage CS rosters with paired subs have headroom AND
    #      so SW pills don't crowd the bottom spawn band — tester
    #      asked for "more spacing between text boxes and spawn
    #      zones" on the SW row specifically.
    svg_w=1235.67,
    svg_h=1240.00,
    header=Box(0, 0, 1235.67, 48.00),
    bg_main=Box(1.30, 46.77, 1049.07, 1191.00),
    bg_subs=Box(1050.26, 47.62, 184.79, 1191.00),
    spawn_rects=[
        # CS spawn bands — game-defined factions.
        # Rulebringers (blue) — single horizontal band at top.
        (Box(343.01, 47.48, 349.54, 38.33), (92, 124, 199, 204)),
        # Dawnbreakers (red) — split into two horizontal bands at
        # the very bottom. SW pills end ~y=1100 with content; the
        # spawn band sits at y=1199 with ~99 SVG of breathing room.
        (Box(117.97, 1199.00, 392.00, 38.33), (208, 102, 99, 204)),
        (Box(550.35, 1199.00, 374.27, 38.33), (208, 102, 99, 204)),
    ],
    zones={
        # Top row — DCs symmetric around the BLUE SPAWN BAND
        # (centre x≈518) per tester ask 2026-05-23 round 8: the
        # subs sidebar pushes the canvas math-centre to ~525, but
        # the visual balance point is the spawn band above the
        # zones, not the math centre. Each DC is 297 SVG wide
        # (icon 96 + gap 1 + pill 200) with a 92 SVG gap.
        #   DC1: 175-472 (icon 175-271, pill 272-472)
        #   DC2: 564-861 (pill 564-764, icon 765-861)
        # DC1.center (323.5) and DC2.center (712.5) are both 194.5
        # SVG from the spawn-band centre at x≈518.
        # `max_rows=8` caps the pill height (DC.text.y=120 to
        # SF1.title.y=343 = 223 SVG of vertical room; 8 content
        # rows + 3 stage headers + padding ≈ 210 SVG fits with a
        # ~13 SVG safety buffer before the pill bottom touches the
        # SF1 title box).
        "Data Center 1": ZoneLayout(
            title=Box(175.00, 100.23, 297.00, 16.79),
            text=Box(272.00, 120.80, 200.00, 188.82),
            icon=Box(175.00, 116.89, 96, 96),
            max_rows=8,
        ),
        "Data Center 2": ZoneLayout(
            title=Box(564.00, 100.23, 297.00, 16.79),
            text=Box(564.00, 120.80, 200.00, 188.82),
            icon=Box(765.00, 116.89, 96, 96),
            max_rows=8,
        ),
        # Mid-upper — round 5 tester feedback 2026-05-23: SF/DS pills
        # were too narrow. Layout now pulls SF/DS inward and widens
        # their pills from 127 → 150 SVG, while PT shifts left and VL
        # shifts right so PT/VL icons sit closer to their adjacent
        # outer-zone icons (more horizontal room for text boxes,
        # tighter icon clustering).
        #
        # Horizontal map (mid-upper):
        #   SF1: 84-332 (pill 84-234, icon 236-332)
        #   PT:  386-684 (icon 386-482, pill 484-684)
        #   DS2: 742-990 (icon 742-838, pill 840-990)
        "Serum Factory 1": ZoneLayout(
            title=Box(84.00, 343.41, 247.34, 16.79),
            text=Box(84.00, 363.97, 150.00, 166.65),
            icon=Box(236.00, 361.22, 96, 96),
        ),
        # Canonical CS map placement: Defense System I sits on the
        # LEFT side and Defense System II on the RIGHT (tester report
        # 2026-05-21). Pre-2026-05-21 the names were swapped relative
        # to the in-game map, which surfaced as the wrong building name
        # next to each pill on the rendered PNG.
        "Defense System 2": ZoneLayout(
            title=Box(742.00, 343.41, 247.34, 16.79),
            text=Box(840.00, 363.97, 150.00, 166.65),
            icon=Box(742.00, 361.22, 96, 96),
        ),
        # Central zones: PT shifts left, pill keeps 200 SVG width.
        # `extend_direction="both"` so the pill consumes both the
        # icon-side and pill-side neighbour budgets when long names
        # need extra room.
        "Power Tower": ZoneLayout(
            title=Box(386.00, 362.24, 298.00, 16.79),
            text=Box(484.00, 384.94, 200.00, 188.82),
            icon=Box(386.00, 378.75, 96, 96),
            extend_direction="both",
        ),
        # Mid-lower — matches SF row horizontal positions.
        "Defense System 1": ZoneLayout(
            title=Box(84.00, 597.41, 247.34, 16.79),
            text=Box(84.00, 617.83, 150.00, 166.65),
            icon=Box(236.00, 615.22, 96, 96),
        ),
        "Serum Factory 2": ZoneLayout(
            title=Box(742.00, 597.41, 247.34, 16.79),
            text=Box(840.00, 617.83, 150.00, 166.65),
            icon=Box(742.00, 615.22, 96, 96),
        ),
        # VL shifts right (mirror of PT shifting left). Pill at left
        # of icon, both also marked `extend_direction="both"`.
        "Virus Lab": ZoneLayout(
            title=Box(366.00, 636.87, 298.00, 16.79),
            text=Box(366.00, 657.29, 200.00, 88.66),
            icon=Box(568.00, 653.62, 96, 96),
            extend_direction="both",
        ),
        # Bottom row — RESTRUCTURED 2026-05-23: icon ABOVE pill,
        # pill fills the full lane width. Each SW lane is 224 SVG
        # wide so a 2-column roster (4 names per stage) fits
        # comfortably without wrap. Icon centred horizontally in
        # the lane below the title.
        #
        # Per-lane vertical stack:
        #   y=853  title (h≈17)
        #   y=873  icon (h=96, x = lane.x + (lane.w - 96)/2)
        #   y=975  pill text (full lane width, grows down)
        "Sample Warehouse 1": ZoneLayout(
            title=Box(21.97, 853.69, 223.34, 16.79),
            text=Box(21.97, 975.69, 223.34, 60.00),
            icon=Box(85.64, 873.69, 96, 96),
        ),
        "Sample Warehouse 2": ZoneLayout(
            title=Box(285.97, 853.69, 223.34, 16.79),
            text=Box(285.97, 975.69, 223.34, 60.00),
            icon=Box(349.64, 873.69, 96, 96),
        ),
        "Sample Warehouse 3": ZoneLayout(
            title=Box(549.97, 853.69, 223.34, 16.79),
            text=Box(549.97, 975.69, 223.34, 60.00),
            icon=Box(613.64, 873.69, 96, 96),
        ),
        "Sample Warehouse 4": ZoneLayout(
            title=Box(797.97, 853.69, 223.34, 16.79),
            text=Box(797.97, 975.69, 223.34, 60.00),
            icon=Box(861.64, 873.69, 96, 96),
        ),
    },
    subs_title=Box(1059.68, 63.08, 167.59, 16.79),
    # Subs section grows with the taller canvas so the pair table
    # has room for 30-row paired-subs renders without bumping into
    # the logo at the bottom of the sidebar.
    subs_text_flat=Box(1059.69, 91.64, 167.59, 450.00),
    subs_text_pairs=Box(1059.69, 91.64, 167.59, 669.00),
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
# CS background base. Deeper sage per the alliance lead's #227 dev
# review feedback ("more of a sage / slightly deeper green than the
# grass green you have now"). The PIL noise overlay adds subtle
# variation on top so the canvas reads as terrain rather than flat
# colour.
_JUNGLE = (102, 122, 88, 255)
_HEADER_FILL = (67, 67, 67, 255)
_HEADER_TEXT = (245, 245, 245, 255)
# Pill fill opacity: 80% (alpha 204/255) post-#227 dev review. The
# original ~49% read fine over the flat sand / sage fills but became
# muddy over the painted background images. 80% keeps the slight
# glassy / paper-over-terrain look while leaving the 8 pt text
# readable.
_PILL_FILL = (255, 255, 255, 204)
_PILL_OUTLINE = (0, 0, 0, 255)
_SPAWN_OUTLINE = (40, 40, 40, 255)
_PLACEHOLDER_FILL = (217, 217, 217, 255)
_PLACEHOLDER_OUTLINE = (0, 0, 0, 255)
_TEXT_DARK = (20, 20, 20, 255)
_TEXT_MUTED = (60, 60, 60, 255)

# Background noise grain amplitude (per-channel ±). Larger = more
# texture, smaller = closer to a solid fill. 16 reads as gentle
# graininess at SCALE=2 without making the labels harder to read.
_NOISE_AMPLITUDE = 16
_PAIRS_UNDERLINE_COLOR = (158, 158, 158, 255)
_PAIRS_DIVIDER_COLOR = (153, 153, 153, 255)
_PAIRS_UNDERLINE_WIDTH_SVG = 3
_PAIRS_DIVIDER_WIDTH_SVG = 2


# ── Font sizing ──────────────────────────────────────────────────────


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
    #    separator between the map and the subs section. DS uses a
    #    sand palette; CS uses a sage / khaki green for the jungle
    #    look the alliance lead asked for (#227). Both get a PIL noise
    #    overlay so the canvas reads as terrain rather than flat
    #    colour.
    is_cs = roster.event_type.upper() == "CS"
    bg_color = _JUNGLE if is_cs else _SAND
    bg_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg_layer)
    bg_draw.rectangle(_s_box(layout.bg_main), fill=bg_color)
    bg_draw.rectangle(_s_box(layout.bg_subs), fill=bg_color)
    canvas.alpha_composite(bg_layer)
    # Layer the painted background image over the flat fill (#227).
    # Procedural noise is the fallback if the asset is missing.
    if not _apply_background_image(canvas, layout, is_cs):
        _apply_background_noise(canvas, layout, bg_color)
    # Re-stroke the sidebar outline AFTER the background so the
    # divider between the map and the sidebar reads clearly.
    border_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    border_draw = ImageDraw.Draw(border_layer)
    border_draw.rectangle(_s_box(layout.bg_subs), outline=(0, 0, 0, 255), width=max(2, SCALE))
    canvas.alpha_composite(border_layer)

    # 2. Header bar with event / team / date.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_header(d, layout, roster)
    canvas.alpha_composite(layer)

    # 3. Spawn zones.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for spawn_box, color in layout.spawn_rects:
        d.rectangle(_s_box(spawn_box), fill=color, outline=_SPAWN_OUTLINE, width=max(1, SCALE // 2))
    canvas.alpha_composite(layer)

    # 4. Zones. Group input zones by `canonical_zone` so each layout
    #    slot renders once with every phase's members stacked inside.
    grouped: dict[str, list[RosterZone]] = {}
    for z in roster.zones:
        key = z.canonical_zone or z.name
        grouped.setdefault(key, []).append(z)
    icon_files = _DS_ICON_FILES if roster.event_type.upper() == "DS" else _CS_ICON_FILES
    icons_dir = _ICONS_DS_DIR if roster.event_type.upper() == "DS" else _ICONS_CS_DIR

    overflow: list[_OverflowEntry] = []
    for canonical, phase_blocks in grouped.items():
        zlayout = layout.zones.get(canonical)
        if zlayout is None:
            # Non-canonical zone name — skip silently. Pre-#152
            # presets with typo zones would otherwise crash here.
            logger.debug("render: skipping unknown zone %r for %s", canonical, roster.event_type)
            continue
        _draw_zone(
            canvas,
            zlayout,
            canonical,
            phase_blocks,
            icon_files,
            icons_dir,
            roster,
            overflow,
            layout=layout,
        )

    # 5. Subs section — picks flat or pairs variant on data shape.
    _draw_subs_section(canvas, layout, roster)

    # 6. Attribution logo at the bottom of the subs sidebar (#227).
    _draw_attribution_logo(canvas, layout)

    # Stash the overflow list on the roster so callers
    # (`_finalize_structured_roster`) can surface a warning ephemeral
    # naming members who didn't fit the slot grid. Render itself never
    # blocks on overflow; the PNG is still produced with as many names
    # as fit.
    roster.overflow = overflow

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


# ── Drawing helpers ──────────────────────────────────────────────────


def _s(v: float) -> int:
    return int(round(v * SCALE))


def _s_box(b: Box) -> tuple[int, int, int, int]:
    return _s(b.x), _s(b.y), _s(b.x + b.w), _s(b.y + b.h)


# ── Fonts ────────────────────────────────────────────────────────────
#
# One name, one font. `_font_for_text` returns the first family in
# `_FONT_STACK` that can draw the WHOLE string, so everything that
# measures a name — shrink-to-fit, wrap, ellipsis, the shared row
# height — keeps working against a single face.
#
# Coverage is asked of the font FILE rather than inferred from the
# codepoint. Guessing by script range is what shipped Cyrillic to a
# Latin-subset Inter: the ranges only listed the scripts somebody had
# already been bitten by, so every script nobody had hit yet silently
# claimed to be covered.
#
# The question "can this file draw this character" is answered the only
# way it can be: rasterise the character, rasterise a private-use
# codepoint no font assigns, and compare. Identical bitmaps mean the
# font drew its .notdef box — the tofu the reader would have seen.


# Coverage is a property of the file, not of the size, so probe at one
# small size and reuse the answer for every size the card asks for.
_FONT_PROBE_PX = 16

# Plane-15 private use. Nothing assigns it, so whatever a font draws
# for it IS that font's .notdef glyph.
_NOTDEF_SENTINEL = "\U000f0000"


@functools.lru_cache(maxsize=64)
def _font_file(name: str) -> Optional[str]:
    """Absolute path to `name`, bundled copy first, or `None`.

    Cached: the answer is a property of the container, and a font that
    was not in the image at boot will not appear in it later.
    """
    for directory in (_BUNDLED_FONT_DIR, *_SYSTEM_FONT_DIRS):
        path = os.path.join(directory, name)
        if os.path.exists(path):
            return path
    return None


def _open_font(path: str, size: int):
    """`ImageFont.truetype(path, size)`, or `None` if it will not open."""
    from PIL import ImageFont

    try:
        return ImageFont.truetype(path, size)
    except (OSError, IOError):
        return None


# Bounded, and small, for the same reason as `_FONT_CACHE_ENTRIES`
# below. Holding one probe face per family sounds free and is not:
# a name containing a character NO family draws — an emoji is the
# everyday case — walks the entire stack looking for the font that
# draws the most of it, and an unbounded cache would then pin all
# sixteen faces, the 16 MB CJK file among them, for the life of the
# process. Measured at about 5 MB, permanently, off one emoji in one
# player's name.
#
# Four is enough because the ANSWERS are cached, not just the faces:
# `_font_covers_char` remembers every (file, character) it has been
# asked, so in the steady state a probe font is not needed at all. It
# is the cold walk that reloads, and that costs a few milliseconds
# once.
_PROBE_FONT_ENTRIES = 4


@functools.lru_cache(maxsize=_PROBE_FONT_ENTRIES)
def _probe_font(path: str):
    """The instance of `path` that coverage is measured with."""
    return _open_font(path, _FONT_PROBE_PX)


# Small on purpose, and the number is a memory decision rather than a
# taste one. A held FreeType face is not free — measured on the bundled
# files, each additional SIZE of Noto Sans CJK costs about 1.2 MB
# resident (Inter, 0.12 MB), because the glyph cache is per size. On
# Railway memory is around 83% of the hosting bill against roughly 1%
# for CPU, so an unbounded font cache is the version of this that costs
# money: `champion_duel_image._fit` alone walks 28 sizes looking for one
# that fits, and would pin every one of them.
#
# What actually repeats is one size drawn across many rows of the same
# card — a roster of forty names at one size, in two or three faces. A
# cache of eight covers that and bounds the worst case at roughly 10 MB.
# The shrink-to-fit walk gets no reuse from a larger cache anyway: it
# steps down through sizes and never revisits one.
_FONT_CACHE_ENTRIES = 8


@functools.lru_cache(maxsize=_FONT_CACHE_ENTRIES)
def _load_font(path: str, size: int):
    """`ImageFont.truetype(path, size)`, or `None` if it will not open."""
    return _open_font(path, size)


@functools.lru_cache(maxsize=32)
def _notdef_bitmap(path: str) -> Optional[bytes]:
    font = _probe_font(path)
    if font is None:
        return None
    try:
        return bytes(font.getmask(_NOTDEF_SENTINEL))
    except Exception:  # noqa: BLE001 - an unprobeable font is just unusable
        return None


@functools.lru_cache(maxsize=8192)
def _font_covers_char(path: str, ch: str) -> bool:
    """Whether the file at `path` has a real glyph for `ch`.

    Zero-width formatting characters (ZWJ, variation selectors) are
    counted as covered: they have no glyph to draw in any font, so
    holding them against a face would reject every font for a name that
    merely contains one.
    """
    if unicodedata.category(ch) in ("Cf", "Cc"):
        return True
    notdef = _notdef_bitmap(path)
    if notdef is None:
        return False
    font = _probe_font(path)
    try:
        return bytes(font.getmask(ch)) != notdef
    except Exception:  # noqa: BLE001 - unrenderable is uncovered
        return False


def _family_font_file(family: _FontFamily, bold: bool) -> Optional[str]:
    """The file to use for `family`, preferring Bold when asked and the
    family has one. A family that ships Regular only answers a bold
    request with its Regular weight rather than nothing — the same
    trade `_font_for_text` has always made for the bundled Noto files.
    """
    names = (*family.bold, *family.regular) if bold else family.regular
    for name in names:
        path = _font_file(name)
        if path is not None:
            return path
    return None


def _family_for_text(text: str, *, bold: bool = False) -> tuple[Optional[str], Optional[str]]:
    """`(family key, font path)` for the first family in `_FONT_STACK`
    that draws every character of `text`.

    When nothing draws all of it, falls back to whichever family draws
    the MOST of it — the name is on the card beside a caption that
    carries it exactly, so half a name read in the right typeface beats
    a full row of boxes. Ties go to the earlier family, which keeps the
    house face winning wherever it can.
    """
    if not text:
        return None, None

    best_key: Optional[str] = None
    best_path: Optional[str] = None
    best_drawn = -1
    chars = set(text)
    # Characters NO family drew, as opposed to ones the winning family
    # happened to miss. The two mean different things and the log below
    # says different things about them.
    nobody: Optional[set[str]] = None

    for family in _FONT_STACK:
        path = _family_font_file(family, bold)
        if path is None:
            continue
        missing = {ch for ch in chars if not _font_covers_char(path, ch)}
        if not missing:
            return family.key, path
        nobody = missing if nobody is None else (nobody & missing)
        # Probe the distinct characters, but score the whole string:
        # a name that is six Hangul syllables and two Arabic letters
        # should land on the font that draws the six.
        drawn = sum(1 for ch in text if ch not in missing)
        if drawn > best_drawn:
            best_key, best_path, best_drawn = family.key, path, drawn

    if best_key is not None:
        unseen = _codepoints(nobody or ())
        boxes = _codepoints(ch for ch in chars if not _font_covers_char(best_path, ch))
        _log_undrawable(unseen, boxes)
    return best_key, best_path


def _codepoints(chars) -> str:
    """`U+0E18,U+0E19` — the log's currency.

    Codepoints rather than the name itself: this goes to a log file, the
    name belongs to a player, and the codepoint is the half that says
    which font is missing.
    """
    return ",".join(sorted({f"U+{ord(ch):04X}" for ch in chars}))


@functools.lru_cache(maxsize=256)
def _log_undrawable(unseen: str, boxes: str) -> None:
    """Once per distinct gap, not once per render — a roster redrawn on
    every edit would otherwise log the same thing dozens of times an
    evening.

    Two different failures, said differently, because only one of them
    has a fix an operator can act on:

    `unseen` is codepoints NO family in the stack drew. That is a
    missing font package and naming it is the whole point of the line.

    `unseen` empty means some font drew every character, just never one
    font all of them — a name mixing Arabic with Hangul, say. That is
    the case per-character fallback was measured against and rejected
    for; no package fixes it, so the line must not ask for one.
    """
    if unseen:
        logger.info(
            "render: no installed font draws %s — drew the rest of the name. "
            "Add the family covering it to the deploy image.",
            unseen,
        )
    else:
        logger.info(
            "render: no single font draws this whole name — drew the longest run "
            "it could; %s will show as boxes. Every font it needs is installed.",
            boxes,
        )


def _try_font(size: int, bold: bool = False):
    """The project face at `size`. Inter is bundled at
    `assets/fonts/`; falls back to DejaVu / Arial when the bundled
    files aren't present, so `render()` stays non-fatal in an
    environment the assets didn't reach.

    For anything a *player* typed, use `_font_for_text` instead. Inter
    is a 66 KB Latin subset — it has three characters of Latin
    Extended-A and no Greek or Cyrillic at all — and this function does
    not check coverage.
    """
    from PIL import ImageFont

    candidates = [
        _INTER_BOLD if bold else _INTER_REGULAR,
        "arialbd.ttf" if bold else "arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        font = _load_font(path, size)
        if font is not None:
            return font
    return ImageFont.load_default()


def log_font_coverage() -> list[str]:
    """Print which font families the running container actually has,
    and return the keys of the ones it does not.

    Called once at boot (`bot.py`'s `on_ready`). The coverage half of
    this fix is an *installed* package rather than a committed file, and
    an environment change fails silently: a build that quietly drops
    `fonts-noto-core` brings the empty boxes straight back with no
    error anywhere. This is the line that says so, at the moment it
    becomes true rather than weeks later off a screenshot.

    Resolves paths only — `os.path.exists`, no font loading — so the
    16 MB CJK file is still not read until a name needs it.
    """
    missing: list[str] = []
    for family in _FONT_STACK:
        path = _family_font_file(family, bold=False)
        if path is not None:
            continue
        missing.append(family.key)
        if family.bundled:
            print(
                f"[FONTS] MISSING bundled font {family.regular[0]} ({family.covers}) — "
                "the deploy did not carry assets/fonts/. Names in that script will "
                "render as empty boxes."
            )
    if missing:
        installed_missing = [k for k in missing if not _family_of(k).bundled]
        if installed_missing:
            print(
                f"[FONTS] {len(installed_missing)} font families not installed: "
                f"{', '.join(installed_missing)}. Names in those scripts render "
                "partially. Expected `fonts-noto-core` in the deploy image."
            )
    else:
        print(f"[FONTS] all {len(_FONT_STACK)} font families present.")
    return missing


def _family_of(key: str) -> _FontFamily:
    for family in _FONT_STACK:
        if family.key == key:
            return family
    raise KeyError(key)


def _wrap_name_to_lines(
    name: str,
    font,
    max_width_px: int,
) -> list[str]:
    """Wrap a long member name to fit within `max_width_px` per line.

    Strategy (#236 follow-up 2026-05-23):

    1. If the name already fits, return it as one line.
    2. If the name has spaces, greedy word-wrap (no hyphen — the
       natural word boundary signals the break).
    3. If a single token still exceeds the budget (e.g. camelCase
       handles with no spaces), hard-break with a hyphen at the end
       of the broken line so readers know the name continues.
       Examples: `dominicsteele99` → `["dominicstee-", "le99"]`.
    4. Anti-orphan: if the natural hard-break would leave a 1-2 char
       tail, prefer a balanced midpoint split — keeps both lines
       substantive.

    Never truncates. Usernames like `dominicsteele99` vs
    `dominicsteele01` differ only in the suffix; trimming would lose
    the disambiguating identifier.
    """
    if not name:
        return [""]
    try:
        if font.getlength(name) <= max_width_px:
            return [name]
    except (AttributeError, TypeError):
        # `getlength` missing on the PIL default font fallback; treat
        # the whole name as fitting (no wrap) to avoid crashing render.
        return [name]

    # Word-wrap when the name has spaces. Greedy: keep adding words
    # to the current line until the next one wouldn't fit, then start
    # a new line. No hyphen — the space IS the natural break signal.
    if " " in name:
        words = name.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = (current + " " + word).strip()
            try:
                fits = font.getlength(candidate) <= max_width_px
            except (AttributeError, TypeError):
                fits = True
            if fits:
                current = candidate
                continue
            if current:
                lines.append(current)
            # The new word alone might still overflow — recurse to
            # hard-break it (which will hyphenate appropriately).
            try:
                if font.getlength(word) > max_width_px:
                    lines.extend(_wrap_name_to_lines(word, font, max_width_px))
                    current = ""
                else:
                    current = word
            except (AttributeError, TypeError):
                current = word
        if current:
            lines.append(current)
        return lines or [name]

    # No spaces — hard-break with a hyphen. Find the longest prefix
    # such that `prefix + "-"` fits in the budget. Recurse on the
    # remainder. Hyphen tells the reader the name continues on the
    # next line (common typographic convention).
    lo, hi = 1, len(name) - 1  # always need at least 1 char left for the tail
    if hi < lo:
        return [name]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        try:
            width = font.getlength(name[:mid] + "-")
        except (AttributeError, TypeError):
            return [name]
        if width <= max_width_px:
            lo = mid
        else:
            hi = mid - 1
    prefix_len = max(1, lo)

    # Anti-orphan: if the natural break would leave a 1-3 char tail
    # (e.g. "LokisBabyGirl" → "LokisBabyG-" + "irl"), prefer a
    # balanced midpoint split so the wrap reads cleanly. Both halves
    # must fit (head with hyphen, tail without). Threshold is 3 so
    # "irl"-style 3-letter tails redistribute too — feels less like
    # a chopped suffix.
    tail_len_natural = len(name) - prefix_len
    if tail_len_natural <= 3 and len(name) >= 6:
        bal_mid = len(name) // 2
        head_bal = name[:bal_mid]
        tail_bal = name[bal_mid:]
        try:
            head_fits = font.getlength(head_bal + "-") <= max_width_px
            tail_fits = font.getlength(tail_bal) <= max_width_px
        except (AttributeError, TypeError):
            head_fits = tail_fits = False
        if head_fits and tail_fits:
            return [head_bal + "-", tail_bal]

    head = name[:prefix_len] + "-"
    tail = name[prefix_len:]
    if not tail:
        # Defensive — shouldn't happen since we ensured 1+ char tail
        # via the binary-search bound.
        return [name]
    return [head] + _wrap_name_to_lines(tail, font, max_width_px)


def _font_for_text(text: str, size: int, *, bold: bool = False):
    """Pick the one font that can draw `text` (#236).

    Walks `_FONT_STACK` in order and returns the first family whose
    file covers every character of the string — Inter for a Latin
    name, Noto Sans for a Turkish or Ukrainian one, the CJK file for
    Korean, and so on. Coverage is measured against the file, not
    guessed from the codepoint; see the section header above for why
    that distinction is the whole bug.

    Returns ONE font, deliberately. Per-character fallback was measured
    against all 3,689 names in the roster and bought ten more of them
    than this does, in exchange for changing every place a name is
    measured. So the contract here is unchanged and no caller needs
    touching — `champion_duel_image.py` imports this function and
    should not have to know any of the above.

    Bold is best-effort: a family that ships Regular only (the bundled
    CJK file) answers `bold=True` with its Regular weight, because
    CJK/Arabic bold is rare in the bot's output and the Bold weights
    would each double the asset.
    """
    _key, path = _family_for_text(text, bold=bold)
    if path is None:
        return _try_font(size, bold=bold)
    font = _load_font(path, size)
    if font is None:
        # Resolved a moment ago and will not open now — a truncated
        # copy, or a file pulled out from under a running container.
        return _try_font(size, bold=bold)
    return font


def _draw_header(draw, layout: EventLayout, roster: RosterData) -> None:
    """Header strip: charcoal bar with left / center / right text.
    Left text combines the event name with the preset name when
    present so the rendered image is self-identifying when alliances
    save them for records.

    Every string here is routed through `_font_for_text` rather than
    `_try_font`: the preset name and the team label are typed by an
    officer, so they are player text and can be in any script. Each is
    measured with the same font it is drawn with, so picking per
    string is safe.
    """
    canvas_w = int(round(layout.svg_w * SCALE))
    draw.rectangle((0, 0, canvas_w, _s(layout.header.h)), fill=_HEADER_FILL)
    size = _pt_to_px(_HEADER_PT)
    pad_x = _s(15)
    text_y = _s(layout.header.h / 2) - int(size * 0.55)

    event_full = "Desert Storm" if roster.event_type.upper() == "DS" else "Canyon Storm"
    if roster.preset_name:
        left_text = f"{event_full} — {roster.preset_name}"
    else:
        left_text = event_full
    draw.text(
        (pad_x, text_y),
        left_text,
        fill=_HEADER_TEXT,
        font=_font_for_text(left_text, size, bold=True),
    )

    if roster.team_label:
        font = _font_for_text(roster.team_label, size, bold=True)
        tw = draw.textlength(roster.team_label, font=font)
        draw.text(((canvas_w - tw) / 2, text_y), roster.team_label, fill=_HEADER_TEXT, font=font)

    if roster.event_date_label:
        font = _font_for_text(roster.event_date_label, size, bold=True)
        tw = draw.textlength(roster.event_date_label, font=font)
        draw.text(
            (canvas_w - pad_x - tw, text_y), roster.event_date_label, fill=_HEADER_TEXT, font=font
        )


def _draw_pill(draw, b: Box, radius_svg: float) -> None:
    x0, y0, x1, y1 = _s_box(b)
    r = int(round(radius_svg * SCALE))
    draw.rounded_rectangle((x0, y0, x1, y1), radius=r, fill=_PILL_FILL)
    draw.rounded_rectangle(
        (x0, y0, x1, y1), radius=r, outline=_PILL_OUTLINE, width=max(1, SCALE // 2)
    )


def _draw_centered_text(draw, b: Box, text: str, font, fill) -> None:
    x0, y0, x1, y1 = _s_box(b)
    tw = draw.textlength(text, font=font)
    tx = (x0 + x1) / 2 - tw / 2
    ty = (y0 + y1) / 2 - font.size * 0.55
    draw.text((tx, ty), text, fill=fill, font=font)


def _draw_icon(
    canvas, draw, zlayout: ZoneLayout, canonical: str, icon_files: dict, icons_dir: str
) -> None:
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
            logger.debug("render: icon load failed for %s (%s) — placeholder", canonical, e)
    draw.ellipse(
        (x0, y0, x1, y1),
        fill=_PLACEHOLDER_FILL,
        outline=_PLACEHOLDER_OUTLINE,
        width=max(1, SCALE // 2),
    )


# ── Slot-flow member layout (#227) ──────────────────────────────────
#
# Names default to a 1-column vertical stack. The pill grows downward
# until it hits the zone's `max_rows` content cap (central pills are
# fixed at 7; outer pills get a computed cap from the layout's
# available vertical space). When 1-col can't fit, the algorithm
# falls back to 2-col packing (outer) or 3-col packing (central). At
# 2 cols and above, slot widths shrink and 20-char in-game names
# become "long," taking a full row alone. Stage headers don't count
# toward the row budget; only content rows (member rows / empty
# rows) do.


@dataclass
class _OverflowEntry:
    """One member that couldn't fit inside a zone's slot grid (capped
    at `max_rows` for central zones). The renderer collects these so
    the caller (`_finalize_structured_roster`) can warn the officer
    after Approve-with-image lands the post."""

    canonical_zone: str
    phase: int
    name: str


# Padding inside the text pill. Top/bottom is slightly larger than
# left/right so the pill has visual breathing room without the
# horizontal slot grid feeling cramped.
# Pill padding matches the design source (#235): 14 px left + right,
# 12 px top + bottom. At SCALE=2 that's 7 SVG horizontal, 6 SVG
# vertical.
_PILL_PAD_X_SVG = 7.0
_PILL_PAD_Y_SVG = 6.0
_PILL_LINE_GAP_SVG = 2.0
_PILL_HEADER_GAP_SVG = 3.0  # gap between a Stage header and its first row
_PILL_STAGE_GAP_SVG = 6.0  # gap between end of Stage 1's rows and Stage 2's header
_PILL_NAME_INDENT_SVG = 4.0  # left indent for member rows (under the Stage header)
# Horizontal gap between adjacent column slots in multi-col layouts.
# Without this, packed names like "Member 2 Member 9" run together at
# 8 pt (#227 dev review).
_PILL_COL_GAP_SVG = 12.0


def _pill_extend_direction(zlayout) -> str:
    """Pick the direction the text pill should grow when it needs to
    widen to fit a long-name row. Centrals (pill below icon) grow
    both directions; outer zones grow away from the icon (so the icon
    stays visible).

      icon center > pill center  → icon is to the right → grow LEFT
      icon center < pill center  → icon is to the left  → grow RIGHT
      centers aligned (central)  → grow BOTH directions

    `ZoneLayout.extend_direction` overrides the auto-detection.
    """
    if zlayout.extend_direction in ("left", "right", "both"):
        return zlayout.extend_direction
    icon_cx = zlayout.icon.x + zlayout.icon.w / 2
    pill_cx = zlayout.text.x + zlayout.text.w / 2
    if abs(icon_cx - pill_cx) < 5.0:
        return "both"
    return "left" if icon_cx > pill_cx else "right"


def _safe_pill_extension_px(zlayout, layout) -> tuple[int, int]:
    """Return `(left_budget_px, right_budget_px)` — how far the pill
    can grow leftward / rightward from its default `text` box before
    hitting (a) the canvas edge less a fixed margin, or (b) the
    midpoint to the nearest neighbour zone in the same horizontal
    band.

    Without this clamp, long-name auto-extension pushed Serum Factory
    pills off the CS canvas edge and would make Sample Warehouse
    pills collide with their neighbours' pills/icons when populated
    densely with long names.

    `_CANVAS_MARGIN_SVG` keeps a visible buffer between the pill and
    the canvas border so the Defense System / Serum Factory pills
    never read as "touching the edge" (tester ask 2026-05-23).

    "Same band" = icon-centre y within `_BAND_EPSILON_SVG` units —
    keeps the constraint scoped to the row so a top-row zone doesn't
    constrain a bottom-row zone vertically.
    """
    # The bounding region for the main zone area is `bg_main` — the
    # subs sidebar sits to the right of it and must never be invaded
    # by a zone's pill extension (tester ask 2026-05-23 round 4:
    # SF2/DS2 pills were overflowing into the subs area on long-name
    # auto-extension because the helper used the FULL canvas width
    # as the right bound instead of bg_main).
    bg_left_px = _s(layout.bg_main.x)
    bg_right_px = _s(layout.bg_main.x) + _s(layout.bg_main.w)
    canvas_margin_px = _s(_CANVAS_MARGIN_SVG)
    pill_left_px = _s(zlayout.text.x)
    pill_right_px = _s(zlayout.text.x) + _s(zlayout.text.w)
    icon_cy = zlayout.icon.y + zlayout.icon.h / 2

    # Inset from the bg_main bounds so even maximally-extended pills
    # carry a visible margin to the canvas border AND never crash
    # into the subs sidebar.
    left_neighbour_right_px = bg_left_px + canvas_margin_px
    right_neighbour_left_px = bg_right_px - canvas_margin_px

    # Same-zone icon clamp — the pill must never grow past its OWN
    # icon. PT's icon sits at x=386-482 with the pill at x=484-684;
    # without this clamp, the asymmetric "both" extension on long
    # names pushed the pill leftward to ~x=412, overlapping the
    # icon at x=482. Mirror for VL whose icon is right of the pill.
    own_icon_left_px = _s(zlayout.icon.x)
    own_icon_right_px = _s(zlayout.icon.x) + _s(zlayout.icon.w)
    # 2-SVG gap between pill and icon so they're visibly distinct.
    icon_gap_px = _s(2.0)
    if own_icon_right_px <= pill_left_px:
        # Icon is to the LEFT of the pill — clamp left extension to
        # icon right edge + gap so the pill never overlaps it.
        left_neighbour_right_px = max(
            left_neighbour_right_px,
            own_icon_right_px + icon_gap_px,
        )
    if own_icon_left_px >= pill_right_px:
        # Icon is to the RIGHT of the pill — clamp right extension.
        right_neighbour_left_px = min(
            right_neighbour_left_px,
            own_icon_left_px - icon_gap_px,
        )

    # Half of the inter-pill gap so each side's budget keeps a
    # visible buffer to the neighbour even at max auto-extension.
    half_pill_gap_px = _s(_PILL_NEIGHBOUR_GAP_SVG) // 2

    for other in layout.zones.values():
        if other is zlayout:
            continue
        other_icon_cy = other.icon.y + other.icon.h / 2
        # Same row check.
        if abs(other_icon_cy - icon_cy) > _BAND_EPSILON_SVG:
            continue
        # Outer extents of the neighbour (whichever is further out:
        # icon or pill).
        other_right_svg = max(
            other.text.x + other.text.w,
            other.icon.x + other.icon.w,
        )
        other_left_svg = min(other.text.x, other.icon.x)
        other_right_px = _s(other_right_svg)
        other_left_px = _s(other_left_svg)
        # Constrain in the direction the other zone sits. Take the
        # midpoint as the *natural* budget boundary, then add half
        # the inter-pill gap so neither zone's max extension touches
        # the other.
        if other_right_px <= pill_left_px:
            # Neighbour is to our LEFT.
            mid_px = (other_right_px + pill_left_px) // 2
            left_neighbour_right_px = max(
                left_neighbour_right_px,
                mid_px + half_pill_gap_px,
            )
        elif other_left_px >= pill_right_px:
            # Neighbour is to our RIGHT.
            mid_px = (other_left_px + pill_right_px) // 2
            right_neighbour_left_px = min(
                right_neighbour_left_px,
                mid_px - half_pill_gap_px,
            )

    left_budget_px = max(0, pill_left_px - left_neighbour_right_px)
    right_budget_px = max(0, right_neighbour_left_px - pill_right_px)
    return left_budget_px, right_budget_px


# Two icon-centre y values within this distance count as the same
# horizontal band for the safe-extension calculation. ~120 SVG is
# wider than any single zone's icon (96 SVG) but narrower than the
# row spacing (~190 SVG), so it scopes the constraint cleanly to
# the row.
_BAND_EPSILON_SVG = 120.0

# Minimum visible buffer between the outer edge of any auto-extended
# pill and the canvas border. Tester ask 2026-05-23 (round 3): keep
# 24 SVG of padding on the edges so zones never touch the canvas
# border. Matches the static title placement for SF1/SF2/DS1/DS2 —
# both default position AND auto-extension respect this margin.
_CANVAS_MARGIN_SVG = 24.0

# Minimum visible gap between two adjacent zones' pills when both
# auto-extend toward each other (tester ask 2026-05-23 round 6:
# both Data Center pills extending into the centre line touched at
# the midpoint, reading as a single merged block). Half the gap is
# subtracted from each side's budget at the midpoint clamp.
_PILL_NEIGHBOUR_GAP_SVG = 20.0


def _max_line_width_px(
    lines: list[dict], font_regular, font_bold, slot_width_px: int, col_gap_px: int
) -> int:
    """Maximum rendered pixel width across all lines in a flow layout.
    Used to size the pill horizontally so long-row names + multi-col
    rows fit without clipping (#227 dev review)."""
    pad_x = _s(_PILL_PAD_X_SVG)
    indent = _s(_PILL_NAME_INDENT_SVG)
    overhead = 2 * pad_x + indent  # padding on both sides + name indent
    max_inner = 0
    for line in lines:
        if line["type"] == "header":
            inner = font_bold.getbbox(line["text"])[2]
        elif line["type"] == "row":
            items = line["items"]
            if not items:
                continue
            cols = len(items)
            # Each name's actual rendered width inside its slot, plus
            # inter-column gaps.
            max_name = max(font_regular.getbbox(n)[2] for n in items)
            # A row uses slot_width per item, but the actual name
            # could be narrower or wider than the slot. The pill needs
            # room for the widest case.
            slot_or_name = max(slot_width_px, max_name)
            inner = cols * slot_or_name + (cols - 1) * col_gap_px
        elif line["type"] == "long":
            inner = font_regular.getbbox(line["name"])[2]
        else:  # empty
            inner = font_regular.getbbox("(empty)")[2]
        if inner > max_inner:
            max_inner = inner
    return max_inner + overhead


def _attempt_flow_at(
    phase_blocks: list[RosterZone],
    font_regular,
    pill_content_width_px: int,
    cols: int,
    max_rows: int,
    canonical_zone: str,
    wrap_max_px: int | None = None,
) -> tuple[list[dict], list[_OverflowEntry]]:
    """Attempt the slot layout at `cols` columns. Returns the lines +
    any members that didn't fit within `max_rows` content rows.

    Stage headers don't count toward `max_rows`. A "content row" is a
    `row` (one or more short names), a `long` row (one 20-char-plus
    name absorbing the row), or an `empty` placeholder.

    The "long" threshold is character-count based (`_LONG_NAME_CHARS`,
    typically 20 to match the in-game username max) rather than pixel
    width. Pixel-width detection at 8 pt occasionally mis-classifies
    9-char names whose rendered glyphs exceed the per-slot pixel
    budget — even though the user's design treats them as short and
    accepts a few pixels of overlap into the inter-column gap.
    """
    lines: list[dict] = []
    overflow: list[_OverflowEntry] = []
    content_rows = 0
    blocks_sorted = sorted(phase_blocks, key=lambda b: b.phase)

    # Stage-visibility rule: find the first phase the zone opens
    # (cap > 0). Stages BEFORE that are deliberately closed
    # (Defense Systems / Serum Factories open at Stage 2; Virus Lab
    # at Stage 3) and must NOT render as Stage headers — officers
    # don't want ghost stages for buildings that aren't part of the
    # strategy yet. Stages from first-open onward render either
    # member names or `(empty)` so deliberate gaps are visible.
    # If no phase opens (Field Hospitals III/IV with cap=0
    # everywhere), keep every block so the canonical slot still
    # draws as a fully-closed zone with Stage headers + `(empty)`,
    # instead of looking like a broken render.
    first_open_phase: int | None = None
    for _b in blocks_sorted:
        if _b.phase >= 1 and _b.max_players > 0:
            first_open_phase = _b.phase
            break

    def _budget_ok() -> bool:
        return content_rows < max_rows

    for block in blocks_sorted:
        if not block.members and block.phase == 0:
            continue
        # Hide phases before the zone opens — same rule as the
        # mail builder and the summary embed, so all three surfaces
        # stay consistent.
        if first_open_phase is not None and block.phase >= 1 and block.phase < first_open_phase:
            continue

        # Stage header always renders (when present), but doesn't tick
        # the content-row budget.
        if block.phase >= 1:
            lines.append({"type": "header", "text": f"Stage {block.phase}:"})

        if not block.members:
            if _budget_ok():
                lines.append({"type": "empty"})
                content_rows += 1
            continue

        # When `wrap_max_px` is set (pill clamped to canvas-safe
        # bounds), each row slot has a known width budget. Names whose
        # rendered pixel width exceeds the slot get force-promoted to
        # "long" rows and hyphen-wrapped via `_wrap_name_to_lines` so
        # they stay readable instead of spilling off the canvas.
        slot_width_px = wrap_max_px

        current_row: list[str] = []
        for raw_name in block.members:
            # In-game LW usernames are hard-capped at 20 chars. Sheet
            # aliases can exceed that; we truncate so the pill never
            # needs to grow beyond the 20-char box width
            # (#227 dev review feedback).
            name = raw_name[:_LONG_NAME_CHARS]
            # At 1 col every name takes the full row anyway (the col
            # IS the row), so the threshold only matters at 2+ cols.
            is_long = cols > 1 and len(name) >= _LONG_NAME_CHARS
            # Pixel-width check: when the pill is clamped, any name
            # that overflows the slot must wrap. Force to "long" so
            # the wrap pieces stack vertically in their own rows.
            if slot_width_px is not None and not is_long:
                try:
                    if font_regular.getlength(name) > slot_width_px:
                        is_long = True
                except (AttributeError, TypeError):
                    pass
            if is_long:
                if current_row:
                    if not _budget_ok():
                        for n in current_row:
                            overflow.append(
                                _OverflowEntry(
                                    canonical_zone,
                                    block.phase,
                                    n,
                                )
                            )
                        current_row = []
                    else:
                        lines.append({"type": "row", "items": current_row})
                        content_rows += 1
                        current_row = []
                # Hyphen-wrap when constrained — the pill won't grow,
                # so multi-char names need to break across rows.
                if wrap_max_px is not None:
                    # 1-col long rows can use the full pill width;
                    # multi-col promoted-long rows are bounded by
                    # the slot width.
                    wrap_budget = pill_content_width_px if cols == 1 else wrap_max_px
                    # Wrap in the font that will DRAW the name, not in
                    # Inter. A Cyrillic name measured in a face that has
                    # no Cyrillic is measured against its .notdef box.
                    pieces = _wrap_name_to_lines(
                        name,
                        _font_for_text(name, font_regular.size),
                        wrap_budget,
                    )
                else:
                    pieces = [name]
                for piece in pieces:
                    if not _budget_ok():
                        overflow.append(
                            _OverflowEntry(
                                canonical_zone,
                                block.phase,
                                piece,
                            )
                        )
                        continue
                    # `whole` is the unwrapped name, carried so the
                    # font is picked from it rather than from the
                    # fragment. "Alexey-Волмирев" hyphen-breaks into
                    # "Alexey-" and "Волмирев", and picking per
                    # fragment would set two adjacent lines of one name
                    # in two different typefaces.
                    lines.append({"type": "long", "name": piece, "whole": name})
                    content_rows += 1
            else:
                current_row.append(name)
                if len(current_row) >= cols:
                    if not _budget_ok():
                        for n in current_row:
                            overflow.append(
                                _OverflowEntry(
                                    canonical_zone,
                                    block.phase,
                                    n,
                                )
                            )
                        current_row = []
                    else:
                        lines.append({"type": "row", "items": current_row})
                        content_rows += 1
                        current_row = []
        if current_row:
            if not _budget_ok():
                for n in current_row:
                    overflow.append(
                        _OverflowEntry(
                            canonical_zone,
                            block.phase,
                            n,
                        )
                    )
            else:
                lines.append({"type": "row", "items": current_row})
                content_rows += 1
    return lines, overflow


def _build_flow_lines(
    phase_blocks: list[RosterZone],
    font_regular,
    pill_content_width_px: int,
    max_cols: int,
    max_rows: int,
    canonical_zone: str,
    overflow: list[_OverflowEntry],
    wrap_max_px: int | None = None,
) -> list[dict]:
    """Try the slot layout at 1 col first. If it overflows `max_rows`,
    re-try at 2 cols, then `max_cols` cols. Take the first column
    count where no overflow occurs; if even `max_cols` cols can't fit
    everyone, take the max-cols attempt and accumulate its overflow.
    This implements the alliance lead's "columns are the fallback"
    rule from #227's design review.

    `wrap_max_px` (when set) signals that the caller has clamped the
    pill width to canvas-safe bounds and any name exceeding the slot
    width must hyphen-wrap rather than overflow."""
    best_lines: list[dict] = []
    best_overflow: list[_OverflowEntry] = []
    for cols in range(1, max_cols + 1):
        lines, attempt_overflow = _attempt_flow_at(
            phase_blocks,
            font_regular,
            pill_content_width_px=pill_content_width_px,
            cols=cols,
            max_rows=max_rows,
            canonical_zone=canonical_zone,
            wrap_max_px=wrap_max_px,
        )
        if not attempt_overflow:
            return lines
        best_lines, best_overflow = lines, attempt_overflow
    overflow.extend(best_overflow)
    return best_lines


def _font_row_height(font) -> int:
    """Actual rendered row height in pixels for `font`. PIL's
    `font.size` returns the em-square size but doesn't include the
    descender, so rows in fallback fonts (Noto CJK SC has a ~7 px
    descender at 16 px em) overflow a pill sized by em alone (#236
    follow-up). `getmetrics()` returns (ascent, descent) measured
    from the baseline; their sum is the actual top-to-bottom extent
    a single line of text occupies."""
    try:
        ascent, descent = font.getmetrics()
        return ascent + descent
    except (AttributeError, OSError):
        # Some PIL fallback fonts (e.g. `load_default`) don't expose
        # getmetrics. Use the em size as a safe approximation.
        return getattr(font, "size", 0) or 12


def _row_height_for_line(line: dict, font_regular, font_bold) -> int:
    """Row height for a single `_build_flow_lines` entry, accounting
    for any fallback font that a name in the row would use. Picks the
    max font height across every name in the row so CJK / Arabic
    fallback rows don't clip into the next line or the pill bottom."""
    if line["type"] == "header":
        return _font_row_height(font_bold)
    if line["type"] == "row":
        # Multi-name row. Walk each name through `_font_for_text` so
        # mixed-script rows (e.g. `Alice 김민준 Bob`) take the tallest
        # font's row height.
        max_h = _font_row_height(font_regular)
        for name in line.get("items", []):
            f = _font_for_text(name, font_regular.size)
            max_h = max(max_h, _font_row_height(f))
        return max_h
    if line["type"] == "long":
        # `whole` when present: a hyphen-wrapped fragment is a piece of
        # one name and must be measured in that name's font, or the two
        # halves get different row heights.
        name = line.get("whole") or line.get("name", "")
        f = _font_for_text(name, font_regular.size)
        return max(_font_row_height(font_regular), _font_row_height(f))
    # empty
    return _font_row_height(font_regular)


def _pill_height_px(lines: list[dict], font_regular, font_bold) -> int:
    """Compute the pixel height the pill needs to draw `lines` without
    clipping. Mirrors `_draw_flow_lines` exactly so the pill background
    and the drawn content stay in sync. Per-line row heights account
    for the actual font that will be used (#236 follow-up: CJK and
    Arabic fallback fonts have larger ascent + descent than Inter at
    the same em size)."""
    if not lines:
        return _s(_PILL_PAD_Y_SVG) * 2
    line_gap = _s(_PILL_LINE_GAP_SVG)
    header_gap = _s(_PILL_HEADER_GAP_SVG)
    stage_gap = _s(_PILL_STAGE_GAP_SVG)
    pad_y = _s(_PILL_PAD_Y_SVG)
    h = pad_y
    prev_type = None
    for line in lines:
        if line["type"] == "header":
            if prev_type is not None:
                h += stage_gap
        else:  # row | long | empty
            if prev_type == "header":
                h += header_gap
            elif prev_type is not None:
                h += line_gap
        h += _row_height_for_line(line, font_regular, font_bold)
        prev_type = line["type"]
    h += pad_y
    return h


def _draw_flow_lines(
    draw, anchor: Box, lines: list[dict], font_regular, font_bold, slot_width_px: int
) -> None:
    """Render the slot-flow `lines` into the pill anchored at
    `anchor`. Assumes the pill background was already drawn at the
    height returned by `_pill_height_px`. `slot_width_px` is the per-
    name horizontal slot used to place columns in multi-col rows."""
    x0, y0, _x1, _y1 = _s_box(anchor)
    pad_x = _s(_PILL_PAD_X_SVG)
    pad_y = _s(_PILL_PAD_Y_SVG)
    line_gap = _s(_PILL_LINE_GAP_SVG)
    header_gap = _s(_PILL_HEADER_GAP_SVG)
    stage_gap = _s(_PILL_STAGE_GAP_SVG)
    indent = _s(_PILL_NAME_INDENT_SVG)
    col_gap = _s(_PILL_COL_GAP_SVG)
    cy = y0 + pad_y
    prev_type = None
    for line in lines:
        # Pre-advance the inter-line gap before this line.
        if line["type"] == "header":
            if prev_type is not None:
                cy += stage_gap
        else:
            if prev_type == "header":
                cy += header_gap
            elif prev_type is not None:
                cy += line_gap

        # Draw this line's content at `cy` (top of the row).
        if line["type"] == "header":
            draw.text((x0 + pad_x, cy), line["text"], fill=_TEXT_DARK, font=font_bold)
        elif line["type"] == "row":
            items = line["items"]
            for idx, name in enumerate(items):
                # Each column slot starts at `slot_width_px + col_gap`
                # after the previous, so packed names have breathing
                # room rather than running together at 8 pt.
                x = x0 + pad_x + indent + idx * (slot_width_px + col_gap)
                # Per-name font picker so CJK / Arabic player names
                # render with their fallback fonts (#236) instead of
                # the Inter `.notdef` tofu box.
                name_font = _font_for_text(name, font_regular.size)
                draw.text((x, cy), name, fill=_TEXT_MUTED, font=name_font)
        elif line["type"] == "long":
            # From `whole` rather than the fragment: see the note where
            # the line is built.
            name_font = _font_for_text(line.get("whole") or line["name"], font_regular.size)
            draw.text((x0 + pad_x + indent, cy), line["name"], fill=_TEXT_MUTED, font=name_font)
        elif line["type"] == "empty":
            draw.text((x0 + pad_x + indent, cy), "(empty)", fill=_TEXT_MUTED, font=font_regular)

        # Advance by the actual row height (accounts for fallback-font
        # ascent + descent) so the next line lands below the descenders
        # of this one instead of overlapping (#236 follow-up).
        cy += _row_height_for_line(line, font_regular, font_bold)
        prev_type = line["type"]


def _draw_zone(
    canvas,
    zlayout: ZoneLayout,
    canonical: str,
    phase_blocks: list[RosterZone],
    icon_files: dict,
    icons_dir: str,
    roster: RosterData,
    overflow: list[_OverflowEntry],
    layout: "EventLayout" = None,
) -> None:
    """Render the three pills + icon for one canonical zone slot.

    `phase_blocks` is the list of `RosterZone` entries for this zone
    (one per phase for phase-aware presets, one total for flat). The
    text pill grows downward from `zlayout.text.y` to fit the slot-flow
    layout's actual line count (#227). Names that don't fit within
    the zone's `max_rows` cap go into `overflow` so the caller can
    warn the officer.
    """
    from PIL import Image, ImageDraw

    font_regular = _try_font(_pt_to_px(_LABEL_PT), bold=False)
    font_bold = _try_font(_pt_to_px(_LABEL_PT), bold=True)

    # Slot grid math: the pill content area is `text.w` minus
    # left/right padding minus the per-name indent. `_build_flow_lines`
    # tries 1-col first (where the slot fills the full content area)
    # and falls back to multi-col only when a 1-col layout overflows
    # the row budget. The chosen column count drives the per-name slot
    # width passed to `_draw_flow_lines`.
    pad_x = _s(_PILL_PAD_X_SVG)
    indent = _s(_PILL_NAME_INDENT_SVG)
    pill_content_width_px = _s(zlayout.text.w) - 2 * pad_x - indent

    # Outer zones use `_OUTER_MAX_ROWS` (derived from the typical
    # vertical headroom between adjacent zones in the layout grid).
    # Central zones cap at `max_rows` per the spec (7). Headers don't
    # count toward the cap; only content rows do.
    nominal_max_rows = zlayout.max_rows if zlayout.max_rows is not None else _OUTER_MAX_ROWS

    # #236 follow-up: the `nominal_max_rows` ceiling was sized assuming
    # Inter-sized rows (~18 px at 8 pt). CJK / Arabic fallback fonts
    # render at ~24 px per row at the same em, so a 7-row CJK pill is
    # ~50 px taller than a 7-row Inter pill and overlaps the next
    # zone's label below it. Scale the cap down so the pixel height
    # ceiling stays roughly constant across scripts. Overflow members
    # drop into the existing post-Approve ephemeral warning path.
    inter_row_h = _font_row_height(font_regular)
    actual_max_row_h = inter_row_h
    for block in phase_blocks:
        for name in block.members or []:
            actual_max_row_h = max(
                actual_max_row_h,
                _font_row_height(_font_for_text(name, font_regular.size)),
            )
    if actual_max_row_h > inter_row_h and nominal_max_rows > 0:
        max_rows = max(
            1,
            int(nominal_max_rows * inter_row_h / actual_max_row_h),
        )
    else:
        max_rows = nominal_max_rows

    lines = _build_flow_lines(
        phase_blocks,
        font_regular,
        pill_content_width_px=pill_content_width_px,
        max_cols=zlayout.max_cols,
        max_rows=max_rows,
        canonical_zone=canonical,
        overflow=overflow,
    )

    # Determine the column count actually used so `_draw_flow_lines`
    # can position name slots correctly. Inspect the lines: if any
    # `row` has more than 1 item, we're in multi-col; otherwise we
    # rendered everything as 1-col.
    cols_used = 1
    for line in lines:
        if line["type"] == "row" and len(line["items"]) > cols_used:
            cols_used = len(line["items"])
    col_gap_px = _s(_PILL_COL_GAP_SVG) if cols_used > 1 else 0
    total_gap_px = col_gap_px * (cols_used - 1)
    slot_width_px = max(1, (pill_content_width_px - total_gap_px) // cols_used)

    # #227 dev review: when a long-row name (or wide multi-col row) is
    # present, scale the pill out to the side so the name fits without
    # clipping. Centrals scale symmetrically; outer zones scale away
    # from their icon (so the icon stays visible).
    required_pill_w_px = _max_line_width_px(
        lines,
        font_regular,
        font_bold,
        slot_width_px,
        col_gap_px,
    )
    default_pill_w_px = _s(zlayout.text.w)

    # Safe-extent clamp (2026-05-23 tester ask): the pill grows away
    # from the icon to fit long-row names, but unconstrained that
    # extension pushed Serum Factory pills off the CS canvas edge
    # and would crash adjacent Sample Warehouse pills into each
    # other. Compute how far each direction can safely grow, then
    # clamp `required_pill_w_px` to that ceiling. When the requested
    # width exceeds the budget, we re-flow the lines with a
    # `wrap_max_px` constraint so long names hyphen-wrap inside
    # the pill instead of spilling out.
    if layout is not None:
        left_budget_px, right_budget_px = _safe_pill_extension_px(
            zlayout,
            layout,
        )
    else:
        # Defensive fallback — no layout context means we trust the
        # caller's bounds.
        left_budget_px = right_budget_px = float("inf")

    direction = _pill_extend_direction(zlayout)
    if direction == "right":
        max_extension_px = right_budget_px
    elif direction == "left":
        max_extension_px = left_budget_px
    else:  # both
        max_extension_px = left_budget_px + right_budget_px

    max_safe_pill_w_px = default_pill_w_px + max_extension_px

    if required_pill_w_px > default_pill_w_px:
        # Pill wants to grow. Cap at the safe-extent ceiling and
        # flag a re-flow when the clamp bites.
        if required_pill_w_px > max_safe_pill_w_px:
            pill_w_px = max_safe_pill_w_px
            needs_reflow = True
        else:
            pill_w_px = required_pill_w_px
            needs_reflow = False
        extend_used_px = pill_w_px - default_pill_w_px
        if direction == "right":
            pill_x_px = _s(zlayout.text.x)
        elif direction == "left":
            pill_x_px = _s(zlayout.text.x) - extend_used_px
        else:  # both — split asymmetrically across the per-side
            # budgets so a zone with lopsided neighbour-room (e.g.
            # CS Power Tower: 118 SVG left, 29 SVG right) actually
            # uses what's available on each side instead of halving
            # the extension and crashing the lighter-budget side.
            if max_extension_px > 0 and (left_budget_px > 0 or right_budget_px > 0):
                left_share_px = min(
                    left_budget_px,
                    int(extend_used_px * left_budget_px / max_extension_px),
                )
            else:
                left_share_px = extend_used_px // 2
            pill_x_px = _s(zlayout.text.x) - left_share_px
        # Re-derive slot_width with the (possibly clamped) pill so
        # the row layout gets the right horizontal room.
        pill_content_width_px = pill_w_px - 2 * pad_x - indent
        slot_width_px = max(
            1,
            (pill_content_width_px - total_gap_px) // cols_used,
        )
        if needs_reflow:
            # Re-build lines with the slot constraint so long names
            # hyphen-wrap inside the cell rather than overflowing.
            lines = _build_flow_lines(
                phase_blocks,
                font_regular,
                pill_content_width_px=pill_content_width_px,
                max_cols=zlayout.max_cols,
                max_rows=max_rows,
                canonical_zone=canonical,
                overflow=overflow,
                wrap_max_px=slot_width_px,
            )
            # Recompute cols_used + slot_width since the re-flow may
            # have picked a different column count under constraint.
            cols_used = 1
            for line in lines:
                if line["type"] == "row" and len(line["items"]) > cols_used:
                    cols_used = len(line["items"])
            total_gap_px = col_gap_px * (cols_used - 1)
            slot_width_px = max(
                1,
                (pill_content_width_px - total_gap_px) // cols_used,
            )
    else:
        pill_x_px = _s(zlayout.text.x)
        pill_w_px = default_pill_w_px

    # The text-pill Box we hand to the renderer is dynamic post-#227:
    # `text.x` and `text.w` reflect the resized pill so the drawing
    # code below paints at the right coordinates.
    text_box_dynamic = Box(
        x=pill_x_px / SCALE,
        y=zlayout.text.y,
        w=pill_w_px / SCALE,
        h=zlayout.text.h,
    )
    pill_h_px = _pill_height_px(lines, font_regular, font_bold)
    # Minimum pill height: just enough room for one line of text +
    # vertical padding. Lets the pill collapse tight to its content for
    # 1-2 name zones (matching the alliance lead's design) while
    # keeping zero-content pills visually present.
    min_pill_h_px = 2 * _s(_PILL_PAD_Y_SVG) + font_regular.size
    pill_h_px = max(pill_h_px, min_pill_h_px)

    # Pill background sized to the computed content height +
    # post-#227 dynamic content width. We can't reuse `_draw_pill`
    # directly because that takes a Box in SVG units; build the
    # rendered box from the dynamic anchor + pixel height.
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_pill(d, zlayout.title, radius_svg=zlayout.title.h / 2)
    text_x0 = pill_x_px
    text_y0 = _s(zlayout.text.y)
    text_x1 = text_x0 + pill_w_px
    text_y1 = text_y0 + pill_h_px
    r = _s(min(text_box_dynamic.w, zlayout.text.h) / 9)
    d.rounded_rectangle(
        (text_x0, text_y0, text_x1, text_y1),
        radius=r,
        fill=_PILL_FILL,
    )
    d.rounded_rectangle(
        (text_x0, text_y0, text_x1, text_y1),
        radius=r,
        outline=_PILL_OUTLINE,
        width=max(1, SCALE // 2),
    )
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_centered_text(d, zlayout.title, canonical, font_bold, _TEXT_DARK)
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_icon(canvas, d, zlayout, canonical, icon_files, icons_dir)
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    _draw_flow_lines(
        d,
        text_box_dynamic,
        lines,
        font_regular,
        font_bold,
        slot_width_px=slot_width_px,
    )
    canvas.alpha_composite(layer)


def _draw_subs_section(canvas, layout: EventLayout, roster: RosterData) -> None:
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
    _draw_centered_text(
        d, layout.subs_title, "Subs", _try_font(_pt_to_px(_LABEL_PT), bold=True), _TEXT_DARK
    )
    canvas.alpha_composite(layer)

    content_box = layout.subs_text_pairs if use_pairs else layout.subs_text_flat
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
    # Subs panel padding (#235 + 2026-05-21 follow-up):
    # - 14 px left + right (design source) — `pad_x`
    # - 12 px bottom (design source) — `pad_y_bottom`
    # - 18 px top — `pad_y_top`. The design source's 12 px top looked
    #   too tight under the "Subs" header pill once the renderer was
    #   accommodating real font metrics; paired-mode top padding from
    #   the layout (~42 px before the Primary/Sub header) sets the
    #   visual benchmark, so flat-mode steps up a notch from design
    #   to feel less cramped.
    pad_x = max(8, _s(7))
    pad_y_top = max(9, _s(9))
    pad_y_bottom = max(6, _s(6))
    # Paired mode only uses pad_y for the bottom-clipping check; flat
    # mode uses pad_y_top + pad_y_bottom asymmetrically.
    pad_y = pad_y_bottom
    line_gap = max(2, _s(3))
    x0, y0, x1, _y1_full = _s_box(content_box)

    # #228 follow-up: shrink the subs pill vertically when it isn't
    # completely full, matching the dynamic-height behaviour of every
    # other pill on the canvas. Pairs mode height comes from the
    # last-row offset + a fixed 12 px tail (design source's bottom
    # padding); flat mode comes from N rows of font + line gap + top
    # and bottom padding.
    # Pre-compute the per-row top positions for paired mode so the
    # pill height calculation and the draw loop agree on placement,
    # even when CJK / Arabic names step rows further apart than the
    # layout's nominal `pairs_row_step` (#236 follow-up).
    # Pair-row metadata: parallel lists of row tops + wrapped name
    # lines per pair. Computed once here so the pill-height calc and
    # the draw loop agree even when CJK / long-name wrapping pushes
    # rows further apart than the layout's nominal `pairs_row_step`
    # (#236 follow-up + 2026-05-23 wrap fix).
    pairs_row_tops: list[int] = []
    pairs_wrapped: list[tuple[list[str], list[str], int]] = []
    # 2026-05-23 simplification: the divider line endpoints
    # (`pairs_divider_x0` and `pairs_divider_x1`) are the canonical
    # left/right borders for the paired-subs text area. Every text
    # edge sits 12 px inside the borders, and the two columns get
    # 12 px between them. Predictable layout, doesn't depend on
    # other layout constants drifting.
    _PAD_PX = 12
    inner_left_px = _s(layout.pairs_divider_x0) + _PAD_PX
    inner_right_px = _s(layout.pairs_divider_x1) - _PAD_PX
    inner_width_px = max(_PAD_PX, inner_right_px - inner_left_px)
    col_width_px = max(_s(8), (inner_width_px - _PAD_PX) // 2)
    primary_col_w_px = col_width_px
    sub_col_w_px = col_width_px
    # Override the layout's column-anchor x values so text draws at
    # the canonical 12-px-inside-divider positions instead of the
    # historical SVG-derived anchors.
    derived_primary_x_px = inner_left_px
    derived_sub_x_px = inner_left_px + col_width_px + _PAD_PX
    if use_pairs:
        pairs_count = len(roster.paired_subs)
        if pairs_count == 0:
            # Just header + underline; small tail.
            content_h_px = _s(layout.pairs_underline_offset_y) + _s(10)
        else:
            box_top_svg = content_box.y
            row1_y_px = _s(box_top_svg + layout.pairs_row1_offset_y)
            nominal_step_px = _s(layout.pairs_row_step)
            line_step_px = _font_row_height(fm)
            cy = row1_y_px
            last_row_h = line_step_px
            for primary, sub in roster.paired_subs.items():
                pairs_row_tops.append(cy)
                primary_font = _font_for_text(primary, fm.size)
                sub_font = _font_for_text(sub, fm.size)
                # Wrap each name to its column budget. Long names
                # split across multiple lines instead of running into
                # the next column or past the divider.
                primary_lines = _wrap_name_to_lines(
                    primary,
                    primary_font,
                    primary_col_w_px,
                )
                sub_lines = _wrap_name_to_lines(
                    sub,
                    sub_font,
                    sub_col_w_px,
                )
                # Per-line height = the per-font row height (covers
                # CJK descenders); row height = N lines × per-line
                # height for the side with more lines.
                primary_line_h = _font_row_height(primary_font)
                sub_line_h = _font_row_height(sub_font)
                row_lines = max(len(primary_lines), len(sub_lines))
                last_row_h = max(
                    primary_line_h * len(primary_lines),
                    sub_line_h * len(sub_lines),
                    line_step_px,
                )
                pairs_wrapped.append((primary_lines, sub_lines, row_lines))
                # 2026-05-23 simplification: 12 px above + 12 px below
                # each row's text. Dividers between rows sit midway,
                # so each row gets 12 px clearance from the adjacent
                # divider. `last_row_h + _PAD_PX * 2` is the minimum
                # row step; nominal layout step caps the minimum when
                # text height is small (Latin single-line) so rows
                # don't bunch up tighter than the design intent.
                cy += max(nominal_step_px, last_row_h + _PAD_PX * 2)
            last_row_top = pairs_row_tops[-1]
            # Pill bottom = last row top + last row's actual height +
            # 12 px bottom padding (the canonical 12 px rule).
            content_h_px = (last_row_top - _s(box_top_svg)) + last_row_h + _PAD_PX
    else:
        flat_count = len([s for s in roster.subs])
        if flat_count == 0:
            content_h_px = pad_y_top + pad_y_bottom + _font_row_height(fm)
        else:
            # Take the max row height across every sub name so a CJK
            # row anywhere in the list grows the pill instead of
            # clipping into the bottom (#236 follow-up).
            max_row_h = max(
                _font_row_height(fm),
                *(_font_row_height(_font_for_text(name, fm.size)) for name in roster.subs),
            )
            content_h_px = pad_y_top + flat_count * (max_row_h + line_gap) - line_gap + pad_y_bottom
    # Never grow beyond the layout's default; only shrink.
    content_h_px = min(content_h_px, _y1_full - y0)
    y1 = y0 + content_h_px

    # Draw the pill background at the computed height (manual rounded
    # rectangle instead of `_draw_pill` so we can pin the height).
    radius_px = _s(min(content_box.w, content_box.h) / 9)
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.rounded_rectangle(
        (x0, y0, x1, y1),
        radius=radius_px,
        fill=_PILL_FILL,
    )
    d.rounded_rectangle(
        (x0, y0, x1, y1),
        radius=radius_px,
        outline=_PILL_OUTLINE,
        width=max(1, SCALE // 2),
    )
    canvas.alpha_composite(layer)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    # Flat mode starts content at the asymmetric top padding; paired
    # mode uses layout-defined offsets and ignores `cy`.
    cy = y0 + pad_y_top

    if use_pairs:
        # Table: "Primary" / "Sub" headers, thick underline, one row per
        # pair with a thin divider between rows. Row positions come
        # from `pairs_row_tops` computed above so the rows step by the
        # max font height when CJK / Arabic names appear (#236
        # follow-up).
        box_top = content_box.y
        # 2026-05-23 simplification: column x positions derived from
        # the divider boundaries with 12 px text padding, not from
        # the historical `subs_pair_*_x` SVG anchors. Keeps text
        # anchored to a predictable, divider-aware coordinate system.
        primary_x = derived_primary_x_px
        sub_x = derived_sub_x_px
        header_y = _s(box_top + layout.pairs_header_offset_y)
        underline_y = _s(box_top + layout.pairs_underline_offset_y)

        d.text((primary_x, header_y), "Primary", fill=_TEXT_DARK, font=fm_bold)
        d.text((sub_x, header_y), "Sub", fill=_TEXT_DARK, font=fm_bold)
        d.line(
            (_s(layout.pairs_divider_x0), underline_y, _s(layout.pairs_divider_x1), underline_y),
            fill=_PAIRS_UNDERLINE_COLOR,
            width=max(1, _s(_PAIRS_UNDERLINE_WIDTH_SVG)),
        )
        pairs_list = list(roster.paired_subs.items())
        for i, (primary, sub) in enumerate(pairs_list):
            row_y = pairs_row_tops[i]
            primary_font = _font_for_text(primary, fm.size)
            sub_font = _font_for_text(sub, fm.size)
            # Pre-computed wrap lines from the geometry pass above
            # (2026-05-23): each side may be 1+ lines depending on
            # name length vs column width. Hard-break preserves every
            # character (no truncation — usernames like
            # `dominicsteele99` vs `dominicsteele01` would lose their
            # disambiguating suffix if we trimmed).
            primary_lines, sub_lines, _row_lines = pairs_wrapped[i]
            primary_line_h = _font_row_height(primary_font)
            sub_line_h = _font_row_height(sub_font)
            row_h = max(
                primary_line_h * len(primary_lines),
                sub_line_h * len(sub_lines),
                _font_row_height(fm),
            )
            if row_y + row_h > y1 - pad_y:
                break
            for li, line in enumerate(primary_lines):
                d.text(
                    (primary_x, row_y + li * primary_line_h),
                    line,
                    fill=_TEXT_DARK,
                    font=primary_font,
                )
            for li, line in enumerate(sub_lines):
                d.text(
                    (sub_x, row_y + li * sub_line_h),
                    line,
                    fill=_TEXT_DARK,
                    font=sub_font,
                )
            if i < len(pairs_list) - 1:
                # Place the divider midway between this row's bottom
                # and the next row's top so it stays centered
                # regardless of variable wrap-driven row heights.
                next_top = pairs_row_tops[i + 1]
                div_y = (row_y + row_h + next_top) // 2
                if div_y < y1 - 4:
                    d.line(
                        (_s(layout.pairs_divider_x0), div_y, _s(layout.pairs_divider_x1), div_y),
                        fill=_PAIRS_DIVIDER_COLOR,
                        width=max(1, _s(_PAIRS_DIVIDER_WIDTH_SVG)),
                    )
    else:
        # Flat list — one sub per row.
        for name in roster.subs:
            # Per-name font picker for CJK / Arabic fallback (#236).
            name_font = _font_for_text(name, fm.size)
            row_h = _font_row_height(name_font)
            if cy + row_h > y1 - pad_y:
                break
            d.text((x0 + pad_x + 4, cy), name, fill=_TEXT_DARK, font=name_font)
            cy += row_h + line_gap

    canvas.alpha_composite(layer)


# ── Background noise + attribution logo (#227) ──────────────────────


# Noise generation parameters. Post-#227 dev review: switched from
# tiled noise (visibly repeating) to canvas-sized noise generated
# once at low resolution then upscaled with bilinear blur for an
# organic, non-repeating terrain feel. Two octaves stacked produce
# broader light/dark patches alongside the finer grain so the canvas
# reads as ground rather than uniform noise.
_NOISE_COARSE_BLOCK_PX = 32  # ~32 px per coarse block at SCALE 2
_NOISE_FINE_BLOCK_PX = 8  # ~8 px per fine grain block
_NOISE_COARSE_WEIGHT = 0.65  # tone-shift contribution
_NOISE_FINE_WEIGHT = 0.35  # grain contribution


def _apply_background_noise(
    canvas, layout: EventLayout, base_rgba: tuple[int, int, int, int]
) -> None:
    """Layer a deterministic two-octave noise pattern over the main +
    subs background fills so the canvas reads as terrain rather than
    a flat colour wash. Noise is generated *once at canvas size* (no
    tiling) so there's no visible repetition. Two octaves stack: a
    coarse octave for broad tonal variation, plus a fine octave for
    grain texture. Both are generated at low resolution and upscaled
    bilinearly to produce soft organic blobs instead of sharp blocks.
    Seeded RNG keeps the output stable across runs."""
    import random
    from PIL import Image, ImageDraw as _ImageDraw

    canvas_w, canvas_h = canvas.size
    rng = random.Random(0x57_4F_52_4D)  # "STOR" — stable per render
    amp = _NOISE_AMPLITUDE
    br, bg, bb, _ba = base_rgba

    def _make_octave(block_px: int, weight: float) -> Image.Image:
        """Generate one noise octave at canvas size by sampling a low-
        resolution grid and bilinear-upscaling it."""
        lo_w = max(2, canvas_w // block_px + 1)
        lo_h = max(2, canvas_h // block_px + 1)
        lo = Image.new("RGBA", (lo_w, lo_h), (0, 0, 0, 0))
        lo_px = lo.load()
        for y in range(lo_h):
            for x in range(lo_w):
                dr = rng.triangular(-amp, amp, 0) * weight
                dg = rng.triangular(-amp, amp, 0) * weight
                db = rng.triangular(-amp, amp, 0) * weight
                r = max(0, min(255, int(br + dr)))
                g = max(0, min(255, int(bg + dg)))
                b = max(0, min(255, int(bb + db)))
                lo_px[x, y] = (r, g, b, 255)
        return lo.resize((canvas_w, canvas_h), Image.BILINEAR)

    coarse = _make_octave(_NOISE_COARSE_BLOCK_PX, _NOISE_COARSE_WEIGHT)
    fine = _make_octave(_NOISE_FINE_BLOCK_PX, _NOISE_FINE_WEIGHT)
    # Blend the two octaves additively. Each octave alone reads as
    # gentle haze; together they look like uneven dirt / dappled
    # vegetation.
    noise = Image.blend(coarse, fine, 0.5)

    # Mask the noise to only cover the two background rectangles so
    # it doesn't bleed onto the header bar / spawn rectangles.
    mask = Image.new("L", canvas.size, 0)
    md = _ImageDraw.Draw(mask)
    md.rectangle(_s_box(layout.bg_main), fill=255)
    md.rectangle(_s_box(layout.bg_subs), fill=255)
    canvas.paste(noise, (0, 0), mask)


_LOGO_PATH = os.path.join(_HERE, "assets", "branding", "lw-alliance-helper-logo.png")
_DS_BACKGROUND_PATH = os.path.join(_HERE, "assets", "backgrounds", "ds_background.png")
_CS_BACKGROUND_PATH = os.path.join(_HERE, "assets", "backgrounds", "cs_background.png")


def _apply_background_image(canvas, layout: EventLayout, is_cs: bool) -> bool:
    """Composite the painted DS / CS background image over the main +
    subs background rectangles. Returns True on success; False if the
    asset is missing (caller falls back to procedural noise).

    The painted image is upscaled with LANCZOS to the canvas dimensions
    so the noise texture, rocks, and other terrain detail stay crisp.
    A mask restricts the composite to just the background rectangles
    so the painted scene doesn't bleed onto the header bar or spawn
    bands."""
    from PIL import Image, ImageDraw as _ImageDraw

    path = _CS_BACKGROUND_PATH if is_cs else _DS_BACKGROUND_PATH
    if not os.path.isfile(path):
        logger.debug("render: background asset missing at %s", path)
        return False
    try:
        bg = Image.open(path).convert("RGBA")
    except (OSError, IOError) as e:
        logger.debug("render: background load failed (%s)", e)
        return False
    bg = bg.resize(canvas.size, Image.LANCZOS)
    # Restrict the paste to the actual background rectangles via a
    # mask so the painted scene doesn't paint over the header bar or
    # spawn rectangles.
    mask = Image.new("L", canvas.size, 0)
    md = _ImageDraw.Draw(mask)
    md.rectangle(_s_box(layout.bg_main), fill=255)
    md.rectangle(_s_box(layout.bg_subs), fill=255)
    canvas.paste(bg, (0, 0), mask)
    return True


def _draw_attribution_logo(canvas, layout: EventLayout) -> None:
    """Drop the bot's logo into the bottom of the subs sidebar (#227).

    Logo scales to fit the rectangle between the bottom of the
    `subs_text_pairs` Box and the bottom of `bg_subs`, butting up
    against the side / bottom edges of the sidebar. If the logo file
    is missing (partial deployments, asset not copied) the slot is
    left empty rather than crashing the render."""
    from PIL import Image

    if not os.path.isfile(_LOGO_PATH):
        logger.debug("render: logo asset missing at %s; skipping", _LOGO_PATH)
        return
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA")
    except (OSError, IOError) as e:
        logger.debug("render: logo load failed (%s); skipping", e)
        return

    # Slot: bottom of `subs_text_pairs` to bottom of `bg_subs`,
    # full width of the sidebar.
    pairs_bottom = layout.subs_text_pairs.y + layout.subs_text_pairs.h
    bg_bottom = layout.bg_subs.y + layout.bg_subs.h
    slot_top_y = pairs_bottom + 8.0  # small gap below the pair table
    slot_left_x = layout.bg_subs.x
    slot_right_x = layout.bg_subs.x + layout.bg_subs.w
    slot_w_svg = slot_right_x - slot_left_x
    slot_h_svg = bg_bottom - slot_top_y
    if slot_h_svg <= 0 or slot_w_svg <= 0:
        return

    # Cap the logo to the sidebar width so it fits the column without
    # bleeding into the map area. The sidebar is the binding dimension.
    target_w_px = _s(slot_w_svg)
    target_h_px = _s(slot_h_svg)
    # Scale to fit while preserving aspect ratio.
    logo_w, logo_h = logo.size
    scale = min(target_w_px / logo_w, target_h_px / logo_h)
    new_w = max(1, int(round(logo_w * scale)))
    new_h = max(1, int(round(logo_h * scale)))
    logo = logo.resize((new_w, new_h), Image.LANCZOS)
    # Anchor at the bottom of the slot, horizontally centred. Bottom
    # alignment keeps the logo flush with the sidebar edge per the
    # alliance lead's design review ("butt up against the edges").
    paste_x = _s(slot_left_x) + (target_w_px - new_w) // 2
    paste_y = _s(slot_top_y) + (target_h_px - new_h)
    canvas.alpha_composite(logo, (paste_x, paste_y))


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
                # Closed phase blocks (cap=0) still appear in
                # roster.zones so the canonical layout slot draws as
                # an empty pill (#226). Officers use cap=0 to
                # communicate "this zone is intentionally unassigned";
                # the visual handed to members needs to show every
                # zone slot, even the closed ones.
                zones.append(
                    RosterZone(
                        name=f"Stage {phase} — {z.zone}",
                        max_players=cap,
                        members=names,
                        phase=phase,
                        canonical_zone=z.zone,
                    )
                )
        else:
            names = _build_member_block(z.zone, 1)
            zones.append(
                RosterZone(
                    name=z.zone,
                    max_players=int(z.max_players),
                    members=names,
                    phase=0,
                    canonical_zone=z.zone,
                )
            )

    subs = [session.members[k]["name"] for k in session.subs if k in session.members]

    return RosterData(
        title=f"{event_full} — {session.preset.name}{team_suffix}{date_suffix}",
        zones=zones,
        subs=subs,
        paired_subs=paired_subs,
        event_type=session.event_type,
        preset_name=session.preset.name,
        team_label=team_label,
        event_date_label=event_date_label,
        phase_count=int(getattr(session.preset, "phase_count", 0) or 0),
    )

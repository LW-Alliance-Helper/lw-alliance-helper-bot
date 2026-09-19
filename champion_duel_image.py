"""The Champion Duel prediction cards.

**Two cards live here.** `render` draws one matchup on the designed VS card and
is what the rest of this docstring is about. `render_slate` draws a whole day's
picks as a list of matchups, and it works differently in one important way: its
height is not fixed, so it has no single static template. It assembles a
header, a repeated row band and a footer instead, on one of two templates --
one column of up to ten meetings, two columns of up to twenty. Its own section
at the foot of this file carries the reasoning.

The embed carries the same numbers. This exists because an embed is not what
gets forwarded into an alliance chat, and sharing is how a prediction earns the
sightings that sharpen the next one — the dataset is only worth anything if
more people contribute, so the output has to be worth passing on.

**This module composites; it does not draw a card.** The artwork —  frames, the
VS burst, the squad containers, the header and footer framing — is a finished
asset in `assets/champion_duel/`, and every box the bot writes into is a
coordinate in `lw_alliance_helper_vs_claude_layout.json`. Boxes are read from
that file rather than hard-coded so a design revision is a file swap, which is
how these arrive. `lw_alliance_helper_vs_claude_handoff.md` beside it is the
authority on sizes, colours and the render order below.

**The template contains an empty progress track only.** The fill and the
divider are drawn here, from the actual probability. Nothing about the result
is baked into the artwork.

**Blue and red are positional, not judgemental.** In the game they mark you and
your opponent; here they mark the first and second name typed, because the bot
does not know which of the two the reader is rooting for. That distinction is
why the winning side is never rendered in green — `notes/DESIGN.md` is explicit
that green means good *for the alliance reading it*, and a scouting tool
predicting two strangers' match has no such side. The template bakes
blue-left/red-right, which says the same thing. What separates the two sides is
the probability itself, at a size nothing else on the card competes with.

Fonts and the script-fallback rules come from `storm_renderer`, imported rather
than reimplemented: Champion Duel names routinely carry non-Latin scripts, and
that module already knows which file renders them.
"""

from __future__ import annotations

import io
import json
import os

from PIL import Image, ImageChops, ImageColor, ImageDraw, ImageFilter

import champion_duel_picks as picks_lib
import champion_duel_predict as predict_lib
import champion_duel_wording as words
from storm_renderer import _font_for_text

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS = os.path.join(_HERE, "assets", "champion_duel")
_LAYOUT_PATH = os.path.join(_ASSETS, "lw_alliance_helper_vs_claude_layout.json")
_LOGO_PATH = os.path.join(_HERE, "assets", "branding", "lw-alliance-helper-logo.png")

with open(_LAYOUT_PATH, encoding="utf-8") as _fh:
    LAYOUT = json.load(_fh)

# The layout names its own background, so swapping the artwork is still one
# file edit even when the new one is a different format.
_TEMPLATE_PATH = os.path.join(_ASSETS, LAYOUT["static_template"])

# The day's picks card has its own layout and NO static template, because its
# height is not fixed -- see `render_slate`.
_PICKS_LAYOUT_PATH = os.path.join(_ASSETS, "lw_alliance_helper_picks_layout.json")

with open(_PICKS_LAYOUT_PATH, encoding="utf-8") as _fh:
    PICKS = json.load(_fh)

# The template's own pixels. Text is drawn at this size and never on a scaled
# copy: shrinking the artwork first would put every coordinate in the layout
# half a pixel out and soften the type for nothing.
W = LAYOUT["canvas"]["width"]
H = LAYOUT["canvas"]["height"]

# From the handoff's colour rules. Near-white for almost everything, gold kept
# back for the one thing worth emphasising (the divider), and the two accents
# used only where a row belongs to a side.
TEXT = (247, 248, 255)
MUTED = (201, 201, 218)
LEFT_ACCENT = (97, 196, 255)
RIGHT_ACCENT = (255, 119, 123)
SHADOW = (4, 5, 14)

# Progress-bar gradients, also from the handoff.
BLUE_OUTER = (21, 159, 246)
BLUE_INNER = (33, 106, 234)
RED_INNER = (215, 38, 54)
RED_OUTER = (239, 52, 42)
GOLD_LIGHT = (255, 217, 106)
GOLD_DARK = (226, 154, 24)

# The bar is the one place Pillow's own primitives show: rounded caps and a
# thin divider both alias badly at native size. Drawing the track at 4x and
# downsampling once is the same trick `storm_renderer` uses on whole canvases,
# applied to the only region that needs it.
_BAR_SCALE = 4


_template_cache: Image.Image | None = None


def _template() -> Image.Image:
    """The background, loaded once and copied per render.

    A missing template is not survivable here and is not meant to be: it is a
    committed asset, and a card drawn without it would be a different card
    rather than a degraded one. `champion_duel_hub._send_prediction` catches
    the failure and sends the embed, which carries the same numbers.
    """
    global _template_cache
    if _template_cache is None:
        _template_cache = Image.open(_TEMPLATE_PATH).convert("RGBA")
    return _template_cache.copy()


# ── Text ──────────────────────────────────────────────────────────────────────


def _fit(draw, text: str, max_w: int, *, start: int, minimum: int, bold: bool = False):
    """Largest font from `start` down that fits `text` into `max_w`.

    Shrinking beats truncating wherever it can: names are user-supplied and the
    name is the one thing on the card a reader uses to confirm the prediction
    is about who they think it is.
    """
    for size in range(start, minimum - 1, -1):
        font = _font_for_text(text, size, bold=bold)
        if draw.textlength(text, font=font) <= max_w:
            return font
    return _font_for_text(text, minimum, bold=bold)


def _place(draw, box: dict, text: str, font, *, align: str = "center", metric: str = "line"):
    """Where to start drawing so `text` sits inside `box`.

    Vertical placement measures a fixed reference glyph rather than the text
    itself, so a row reading "Tank" and one reading "Missile 31.5M" share a
    baseline instead of each centring on its own ink. `metric="ink"` opts out,
    for the big percentage where the glyphs *are* the block.

    **The reference is cap height, not cap-to-descender.** Reserving descender
    space under every string centres the ones that have a descender and leaves
    the ones that don't sitting 2-3px high — which is most of the card, since
    names, squad types and the title rarely have one. Centring the cap band
    instead puts the baseline in the same place either way and lets the
    occasional descender hang, which is what reads as level.
    """
    ink = draw.textbbox((0, 0), text, font=font)
    ref = ink if metric == "ink" else draw.textbbox((0, 0), "H", font=font)
    if align == "left":
        x = box["x"] - ink[0]
    elif align == "right":
        x = box["x"] + box["w"] - ink[2]
    else:
        x = box["x"] + (box["w"] - (ink[2] - ink[0])) / 2 - ink[0]
    y = box["y"] + (box["h"] - (ref[3] - ref[1])) / 2 - ref[1]
    return round(x), round(y)


def _inset(box: dict, pad: int) -> dict:
    """`box` pulled in on both sides.

    Left- and right-aligned text would otherwise start on the container's own
    border, which the template draws as a lit stroke — the glyphs end up
    touching it.
    """
    return {"x": box["x"] + pad, "y": box["y"], "w": box["w"] - 2 * pad, "h": box["h"]}


def _text(draw, box: dict, text: str, font, fill, *, align="center", metric="line", stroke=0):
    """One string, placed in its box over a dark shadow.

    The background is busy neon; a 2px offset is enough to hold small text off
    it. Heavier strokes are reserved for the percentage, which is large enough
    to carry one without the letterforms thickening into each other.
    """
    if not text:
        return
    x, y = _place(draw, box, text, font, align=align, metric=metric)
    draw.text((x + 2, y + 2), text, font=font, fill=SHADOW, stroke_width=stroke, stroke_fill=SHADOW)
    draw.text((x, y), text, font=font, fill=fill, stroke_width=stroke, stroke_fill=SHADOW)


def _ellipsized(draw, head: str, tail: str, font, max_w: int) -> str:
    """`head` trimmed until `head + tail` fits, with `tail` kept whole.

    Only ever reached by a name that is still too wide at the minimum size.
    The server suffix survives the trim because it is what disambiguates two
    players who chose the same name.
    """
    if draw.textlength(head + tail, font=font) <= max_w:
        return head + tail
    for cut in range(len(head) - 1, 0, -1):
        candidate = head[:cut].rstrip() + "…" + tail
        if draw.textlength(candidate, font=font) <= max_w:
            return candidate
    return "…" + tail


def _name(draw, box: dict, side) -> None:
    """The competitor's name, with their server."""
    head = side.name or "(unknown)"
    tail = f" #{side.server}" if side.server else ""
    font = _fit(draw, head + tail, box["w"] - 24, start=28, minimum=18, bold=True)
    _text(draw, box, _ellipsized(draw, head, tail, font, box["w"] - 24), font, TEXT)


def _shared_pct_font(draw, left: str, right: str, boxes):
    """One size for both percentages.

    They are read against each other, so a "9%" set larger than a ">99%" purely
    because it has fewer digits would misstate the gap before the reader has
    got to the numbers.
    """
    for size in range(116, 60, -2):
        if all(
            draw.textlength(text, font=_font_for_text(text, size, bold=True)) <= box["w"] - 24
            for text, box in zip((left, right), boxes)
        ):
            return size
    return 60


# ── The card ──────────────────────────────────────────────────────────────────


def _shared_status_font(draw, left: str, right: str, boxes):
    """One size for both status lines.

    They sit level with each other across the card, so a short one set larger
    than a long one reads as a difference in importance rather than in length.
    """
    for size in range(18, 13, -1):
        if all(
            draw.textlength(text, font=_font_for_text(text, size)) <= box["w"] - 16
            for text, box in zip((left, right), boxes)
        ):
            return size
    return 14


def _side(draw, boxes: dict, side, prob: float, pct_size: int, status_size: int, *, accent) -> None:
    """One competitor: name, probability, line-up, and what it is built on."""
    _name(draw, boxes["name"], side)

    pct = words.probability(prob)
    _text(
        draw,
        boxes["win_probability"],
        pct,
        _font_for_text(pct, pct_size, bold=True),
        TEXT,
        metric="ink",
        stroke=2,
    )
    _text(draw, boxes["to_win"], "to win", _font_for_text("to win", 19, bold=True), MUTED)

    # The line-up in the order the prediction assumed they will deploy in --
    # NOT the natural slot order, when the two differ. Order decides which
    # squad meets which, and the counter triangle means it can outweigh power,
    # so showing one order beside a number computed from another is how a
    # reader talks themselves into distrusting a correct prediction.
    lineup, _from_sightings = side.likely_order()
    index_font = _font_for_text("1", 21, bold=True)
    for i, ((power, squad_type), row) in enumerate(zip(lineup, boxes["squad_rows"]), start=1):
        _text(draw, row["icon"], str(i), index_font, accent)
        # Type left, power right in the same cell, so the powers scan as a
        # column and the two sides' line-ups can be compared down the card.
        cell = _inset(row["text"], 16)
        _text(
            draw,
            cell,
            squad_type,
            _fit(draw, squad_type, cell["w"] // 2, start=23, minimum=16, bold=True),
            TEXT,
            align="left",
        )
        power_text = f"{power / 1_000_000:.1f}M"
        _text(
            draw, cell, power_text, _font_for_text(power_text, 23, bold=True), TEXT, align="right"
        )

    box = boxes["status"]
    status = words.lineup_summary(side)
    font = _font_for_text(status, status_size)
    _text(draw, box, _ellipsized(draw, status, "", font, box["w"] - 16), font, MUTED)


def _odds_bar(canvas, p_a: float) -> None:
    """The VS card's bar, over the empty track the template supplies.

    The two sides each state a percentage; this is what makes the *gap* legible
    at a glance, which is the thing a reader actually wants and the thing two
    separate numbers are worst at conveying.
    """
    _split_bar(canvas, LAYOUT["dynamic_progress_track"], p_a)


def _split_bar(canvas, track: dict, p_a: float) -> None:
    """One bar, split where the odds split, over any track a layout names.

    The whole fill is computed rather than drawn in the artwork, which is what
    lets one routine serve both cards: the VS card's single full-width track
    and the picks card's seventeen short ones differ only in the box. The
    divider is clamped a radius in from each end so a lopsided prediction still
    reads as a bar with rounded caps rather than as a shape with one corner cut
    off.
    """
    x, y, w, h, radius = (track[k] for k in ("x", "y", "w", "h", "radius"))
    s = _BAR_SCALE
    bw, bh = w * s, h * s

    split = round(w * max(0.0, min(1.0, p_a)))
    split = min(max(split, radius), w - radius) * s

    row = bytearray()
    for px in range(bw):
        if px < split:
            a, b, t = BLUE_OUTER, BLUE_INNER, px / max(split, 1)
        else:
            a, b, t = RED_INNER, RED_OUTER, (px - split) / max(bw - split, 1)
        row.extend(round(c0 + (c1 - c0) * t) for c0, c1 in zip(a, b))
    layer = Image.frombytes("RGB", (bw, 1), bytes(row)).resize((bw, bh)).convert("RGBA")

    # A little bloom under the divider, so the seam reads as lit rather than as
    # a scratch across the fill. Kept to the divider: blooming the fills too
    # would wash the gradient out.
    half = max(5 * s // 2, 1)
    glow = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    ImageDraw.Draw(glow).rectangle(
        [split - half * 3, 0, split + half * 3, bh], fill=GOLD_LIGHT + (120,)
    )
    layer = Image.alpha_composite(layer, glow.filter(ImageFilter.GaussianBlur(radius=4 * s)))

    gold = bytearray()
    for py in range(bh):
        t = py / max(bh - 1, 1)
        gold.extend(round(c0 + (c1 - c0) * t) for c0, c1 in zip(GOLD_LIGHT, GOLD_DARK))
    divider = Image.frombytes("RGB", (1, bh), bytes(gold)).resize((half * 2, bh))
    layer.paste(divider, (split - half, 0))

    # Clip everything to the rounded track in one go. The fill covers the whole
    # rectangle, so the mask *is* the track's shape.
    mask = Image.new("L", (bw, bh), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, bw - 1, bh - 1], radius=radius * s, fill=255)
    layer.putalpha(mask)

    canvas.alpha_composite(layer.resize((w, h), Image.LANCZOS), (x, y))


def _header(canvas, draw, subtitle: str | None) -> None:
    """Event title, round metadata, and the badge.

    `subtitle` is whatever the caller knows about the fixture. Left blank when
    it knows nothing: an invented round is worse than an empty box, and the
    stage is not something the bot can derive (#488).
    """
    header = LAYOUT["header"]
    title = "CHAMPION DUEL"
    _text(draw, header["event_title"], title, _font_for_text(title, 34, bold=True), TEXT)
    if subtitle:
        box = header["round_metadata"]
        font = _fit(draw, subtitle, box["w"] - 24, start=24, minimum=16, bold=True)
        _text(draw, box, _ellipsized(draw, subtitle, "", font, box["w"] - 24), font, MUTED)
    _logo(canvas, header["logo_badge"])


def _footer(draw, result) -> None:
    """What the number is worth, in the reader's words rather than a score.

    It spans the full width under the bar because it qualifies the whole card,
    not one side of it. Named as a prediction confidence rather than bare
    "Confidence:", which on a card carrying two probabilities could be read as
    a confidence in one of the players.
    """
    box = LAYOUT["footer"]["confidence_summary"]
    text = words.confidence_line(result)
    font = _fit(draw, text, box["w"] - 32, start=21, minimum=15)
    _text(draw, box, _ellipsized(draw, text, "", font, box["w"] - 32), font, MUTED)


def _clear(canvas, clear: dict) -> None:
    """Paint a region of the template back to background.

    The artwork draws a badge frame in the top-right corner, and it is the one
    container on the card the logo cannot sit in: it is 103×88 and the logo is
    square, so the mark could only go in cropped (which takes the antenna off
    the robot and cuts "HELPER" in half) or letterboxed against the frame's
    walls. Removing the frame and standing the logo on the background costs
    nothing — the rounded corners keep the badge read.

    The left and bottom edges are feathered because those are the two that meet
    other artwork: the header bar's glow and the red card's, both of which
    bleed into this rectangle and neither of which should end in a straight
    line. The top and right edges are the canvas, where there is nothing to
    blend into.
    """
    w, h = clear["w"], clear["h"]
    patch = Image.new("RGBA", (w, h), ImageColor.getrgb(clear["fill"]) + (255,))

    feather = clear.get("feather", 0)
    if feather:
        mask = Image.new("L", (w, h), 255)
        px = mask.load()
        for i in range(feather):
            alpha = round(255 * (i + 1) / (feather + 1))
            for y in range(h):
                px[i, y] = min(px[i, y], alpha)
            for x in range(w):
                px[x, h - 1 - i] = min(px[x, h - 1 - i], alpha)
        patch.putalpha(mask)

    canvas.alpha_composite(patch, (clear["x"], clear["y"]))


def _logo(canvas, box: dict) -> None:
    """Attribution in the top-right corner, matching the storm render's
    convention.

    Square, standing on the background rather than inside a frame — see
    `_clear`, which removes the one the template draws. Sized to the header
    bar's height and aligned right with the red card, so the corner reads as
    part of the same row rather than an ornament floating beside it.

    Corners are rounded at 4x like the odds bar; Pillow leaves them stepped at
    this size otherwise.

    A missing or unreadable asset is skipped rather than raised: the render
    must not fail over branding, and the template's own frame is left in place
    when that happens rather than clearing a hole where the logo would go.
    """
    if not os.path.isfile(_LOGO_PATH):
        return
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA")
    except Exception:  # noqa: BLE001 - a missing logo must not fail the render
        return

    if box.get("clear"):
        _clear(canvas, box["clear"])

    scale = min(box["w"] / logo.width, box["h"] / logo.height)
    w = max(round(logo.width * scale), 1)
    h = max(round(logo.height * scale), 1)
    s = _BAR_SCALE
    big = logo.resize((w * s, h * s), Image.LANCZOS)

    mask = Image.new("L", big.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, big.width - 1, big.height - 1], radius=box.get("radius", 0) * s, fill=255
    )
    # Multiplied into the logo's own alpha rather than replacing it, so a
    # future logo with transparency keeps it.
    big.putalpha(ImageChops.multiply(big.getchannel("A"), mask))

    canvas.alpha_composite(
        big.resize((w, h), Image.LANCZOS),
        (box["x"] + (box["w"] - w) // 2, box["y"] + (box["h"] - h) // 2),
    )


def render(result: predict_lib.Prediction, *, subtitle: str | None = None) -> bytes:
    """The prediction as a WebP image, composited over the template.

    `subtitle` is the round metadata — "Group M · Semifinal" — which the game
    puts at the top of its own screen. Optional because the bot does not know
    the schedule; a caller that does can pass it.

    **WebP rather than the PNG the spec asks for (§10).** The card is mostly a
    photographic neon background, which PNG encodes badly: the same image is
    1302 KB as PNG and 259 KB at WebP q=95, for a mean error of 1.26/255 that
    nothing on this artwork shows. Downsampling was the other way to get the
    size down and is worse — scaling to 1280px wide saves a quarter and costs
    the resolution permanently, where the format change saves four fifths and
    costs nothing. Discord renders WebP inline on every client and re-encodes
    uploads to it for previews regardless, so this is the format it wanted.
    """
    canvas = _template()
    draw = ImageDraw.Draw(canvas)
    left, right = LAYOUT["left"], LAYOUT["right"]

    _header(canvas, draw, subtitle)
    # Both sides' percentages and both status lines are sized together, so
    # neither pair differs in size for a reason the reader would misread.
    pct_size = _shared_pct_font(
        draw,
        words.probability(result.p_a),
        words.probability(result.p_b),
        (left["win_probability"], right["win_probability"]),
    )
    status_size = _shared_status_font(
        draw,
        words.lineup_summary(result.a),
        words.lineup_summary(result.b),
        (left["status"], right["status"]),
    )
    _side(draw, left, result.a, result.p_a, pct_size, status_size, accent=LEFT_ACCENT)
    _side(draw, right, result.b, result.p_b, pct_size, status_size, accent=RIGHT_ACCENT)
    _odds_bar(canvas, result.p_a)
    _footer(draw, result)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="WEBP", quality=95, method=6)
    return buf.getvalue()


# ── The day's picks ───────────────────────────────────────────────────────────
#
# A second card, and a different kind of one. The VS card composites one
# finished template; this one assembles bands, because **a slate's height is not
# fixed**: it carries one to twenty meetings and grows with them.
#
# **Two templates, not one elastic card.** Kevin, 27 Aug: *"I think having a
# different design for 1 column of 10 vs 2 columns of 10 is better so we know
# which we need."* So `single` is one column of up to ten and `wide` is two
# columns of up to twenty; height is data, the column count is the template,
# and the switch is automatic on row count. The columns balance rather than
# fill -- eleven rows is six and five, never ten and one, which would read as a
# mistake every time -- and a short column ends early rather than stretching its
# rows, because one row pitch on every card is what stops two rows on the same
# card being different sizes.
#
# **What a row carries is one percentage, on the picked side, and nothing else.**
# No row number, no confidence, no second percentage, no basis label. The number
# itself is the caller's; nothing about which one is decided here.
#
# Everything else follows the VS card deliberately: the same fonts through
# `storm_renderer`, the same `words.probability` refusal to round a certainty
# into existence, and the same positional blue-left / red-right.


def _picks_colour(key: str) -> tuple:
    return ImageColor.getrgb(PICKS["palette"][key])


# A placeholder, and the only card string that does not live in
# `champion_duel_picks.py` beside `CARD_TITLE` and the rest. It is here because
# the cap is new artwork and no module owned the word yet, and this session
# does not open that one. **Move it there with the others when the copy
# sign-off lands** -- this is not meant to become a second home for card copy.
CARD_PICK_LABEL = "PICK"


_GEOM = PICKS["geometry"]
_ROWS_TOP = _GEOM["header_h"] - _GEOM["header_overlap"]


def picks_template(rows: int) -> dict:
    """Which of the two templates `rows` meetings are drawn on.

    One column up to ten, two columns to twenty. Above twenty the card is not
    drawn at all: overflow makes a second slate, it never drops a row. That is
    what retires `CAPTION_TRUNCATED` by construction rather than by rule, and
    it is why this raises instead of clamping.
    """
    if rows < 1:
        raise ValueError("a picks card needs at least one meeting")
    if rows > _GEOM["max_rows"]:
        raise ValueError(
            f"a picks card carries at most {_GEOM['max_rows']} meetings, not {rows}; "
            "a twenty-first meeting is a second slate"
        )
    single = PICKS["templates"]["single"]
    return single if rows <= single["rows_per_column"] else PICKS["templates"]["wide"]


def _column_split(rows: int, template: dict) -> list[int]:
    """How many rows each column carries.

    The columns balance rather than fill, and an odd count puts the extra row
    on the left. Kevin, correcting the wireframe: *"just do it as 6 and 5 and
    don't squish the 6, let there be an empty space under the 5."*
    """
    columns = len(template["columns"])
    per = [rows // columns] * columns
    for i in range(rows % columns):
        per[i] += 1
    return per


def picks_height(rows: int) -> int:
    """How tall a card with `rows` meetings comes out.

    Public because the layout tests assert against it and a surface sizing an
    upload wants it without rendering first. It is the tallest column that sets
    the height, so eleven rows in two columns is a six-row card.
    """
    template = picks_template(rows)
    tallest = max(_column_split(rows, template))
    return _last_bar_bottom(tallest) + _GEOM["trailing"] + _GEOM["footer_h"]


def _last_bar_bottom(rows_in_column: int) -> int:
    """The bottom edge of the lowest bar on the card.

    Everything under the rows is measured from the bar rather than from the row
    band, because the band is taller than its pitch and its lower half is
    transparent. The bar is the ink.
    """
    return _ROWS_TOP + _GEOM["bar"]["y"] + (rows_in_column - 1) * _GEOM["pitch"] + _GEOM["bar"]["h"]


def picks_size(rows: int) -> tuple[int, int]:
    """The finished card's pixel size, without rendering it."""
    return picks_template(rows)["width"], picks_height(rows)


_art_cache: dict[str, Image.Image | None] = {}


def _art(name: str, *, required: bool = False) -> Image.Image | None:
    """One piece of the artwork, loaded once and copied per use.

    **A decorative piece that will not load is treated as absent rather than
    raised on**, which is the opposite of the VS card's rule about its
    template. The difference is what the failure costs: that card cannot be
    drawn at all without its background, where the header, footer and row band
    only make this one look plainer than intended.

    **`required=True` inverts that, and the PICK cap is the one piece that
    asks for it.** The cap is not decoration -- it is the only thing on a row
    that says who to back and by how much, and a row drawn without it is
    indistinguishable from a row nobody could predict. That is a card making a
    false statement rather than a plain one, so it fails instead, and
    `champion_duel_hub._send_prediction` falls back to the embed, which carries
    the same numbers.

    Unreadable counts the same as missing: `os.path.isfile` says a path exists,
    not that Pillow can decode what is at it.

    **Nothing is resized here.** Every piece is committed at the size it is
    drawn at, so the 4x downscale off the masters is paid once by
    `scripts/build_champion_duel_picks_assets.py` rather than on every render.
    """
    if name not in _art_cache:
        try:
            _art_cache[name] = Image.open(os.path.join(_ASSETS, name)).convert("RGBA")
        except Exception:  # noqa: BLE001 - a decorative piece must not fail the render
            _art_cache[name] = None
    art = _art_cache[name]
    if art is None:
        if required:
            raise FileNotFoundError(f"the picks card cannot be drawn without {name}")
        return None
    return art.copy()


def _at(box: dict, dx: int, dy: int) -> dict:
    """A row box moved onto the row and column it is being drawn for.

    Row boxes are stored once, relative to the row band's own corner rather
    than to the card's. There is only one row shape, and a layout that repeated
    it twenty times would be twenty places for a revision to half-land.
    """
    return {**box, "x": box["x"] + dx, "y": box["y"] + dy}


def _mirrored(box: dict, width: int) -> dict:
    """`box` reflected across the vertical centre of a `width`-wide space.

    The PICK cap's artwork points one way and is mirrored for the other side,
    so its text boxes have to be mirrored with it or the type lands on the
    chamfer.
    """
    return {**box, "x": width - (box["x"] + box["w"])}


def _shared_name_size(draw, labels, max_w: int) -> int:
    """One font size for every name on the card.

    The same reasoning as `_shared_pct_font`, over a longer list: names are
    read down two columns, and one set larger than its neighbours because it
    happens to be shorter reads as emphasis rather than as length.

    **Each label is measured in the font it will actually be drawn in**, which
    is not the same file for all of them: `_font_for_text` picks by script, and
    Champion Duel names routinely carry Korean and Arabic. Measuring them all
    against one Latin font sizes the card off a width the CJK face does not
    have -- and, worse, invites a caller to reuse that font object for every
    row, which draws those names as empty boxes. That is what the first version
    of this did.

    The floor is 17 because the game's 20-character name limit does not bound
    full-width scripts: twenty Korean or Chinese glyphs run about twice as wide
    as twenty Latin ones, and Kevin's own reference card carries several. Below
    the floor `_ellipsized` takes over.
    """
    for size in range(30, 17, -1):
        if all(
            draw.textlength(label, font=_font_for_text(label, size, bold=True)) <= max_w
            for label in labels
        ):
            return size
    return 17


def _picks_header(canvas, draw, slate, template: dict) -> None:
    """The title, what the card is of, and the badge."""
    header = template["header"]
    art = _art(header["art"])
    if art is not None:
        canvas.alpha_composite(art, (0, 0))

    box = header["event_title"]
    title = picks_lib.CARD_TITLE
    font = _fit(draw, title, box["w"] - 24, start=34, minimum=22, bold=True)
    _text(draw, box, title, font, TEXT, align=box["align"])

    # The words are the slate's, not this module's: what the subject line says
    # -- the stage and the date, with no group in it -- belongs to whoever owns
    # `Slate`. This box is geometry only.
    box = header["subject"]
    subject = slate.subject()
    font = _fit(draw, subject, box["w"] - 24, start=26, minimum=17, bold=True)
    _text(
        draw,
        box,
        _ellipsized(draw, subject, "", font, box["w"] - 24),
        font,
        MUTED,
        align=box["align"],
    )

    # No frame is painted out here. Unlike the VS template, this artwork leaves
    # the top-right corner clear -- Kevin composited the mark onto the magenta
    # glow himself and accepted it.
    _logo(canvas, header["logo_badge"])


def _pick_cap(percentage: str, *, mirror: bool):
    """The gold PICK cap, with its two lines set on it.

    **Set at 4x and downsampled by the caller.** The cap occupies 103x58 on the
    bar and carries two lines with the lower one the bigger, which is too tight
    to set directly; the artwork is stored at 412x232 so the type goes on
    there. It is the same supersample the odds bar and the logo already use,
    applied to the one region of this card that needs it.

    **The two zones take different text colours, and that is a decision rather
    than a preference.** Kevin, 28 Aug: *"I left space in the cap for it to say
    'PICK' in the upper gold section and the percentage to be larger in the
    dark lower section."* The ground flips between them -- median relative
    luminance 0.44 above the split and 0.014 below it -- so one colour would
    fail on one of the two.

    **Each line carries a halo in the other zone's tone**, because neither zone
    is flat: both run a diagonal highlight across them, and a fill chosen
    against the median disappears where that highlight crosses a glyph. The
    halo is a stroke rather than the offset shadow `_text` draws, because an
    offset one reads as a drop shadow on a plate this small.
    """
    spec = PICKS["cap"]
    # Required, unlike every other piece: see `_art`. A row that loses its cap
    # loses the pick and the number with it, and reads as a meeting nobody
    # could call rather than as a card missing some polish.
    art = _art(spec["art"], required=True)
    if mirror:
        art = art.transpose(Image.FLIP_LEFT_RIGHT)
    width = spec["draw"][0]
    draw = ImageDraw.Draw(art)

    for key, text, fill, halo, start, minimum in (
        (
            "label",
            CARD_PICK_LABEL,
            _picks_colour("cap_label"),
            _picks_colour("cap_label_halo"),
            46,
            30,
        ),
        (
            "percentage",
            percentage,
            _picks_colour("cap_percentage"),
            _picks_colour("cap_percentage_halo"),
            96,
            56,
        ),
    ):
        box = _mirrored(spec[key], width) if mirror else spec[key]
        font = _fit(draw, text, box["w"], start=start, minimum=minimum, bold=True)
        x, y = _place(draw, box, text, font, align=box["align"], metric="ink")
        draw.text((x, y), text, font=font, fill=fill, stroke_width=5, stroke_fill=halo)
    return art


def _picks_row(canvas, draw, pick, origin: tuple[int, int], name_size: int) -> None:
    """One meeting: the band, two names, and a cap on the picked side.

    **A row nobody can predict keeps both names and carries no cap.** It is
    still the most useful row on the card -- it names two players nobody has
    scouted, to the alliance about to read it -- so it is drawn rather than
    dropped. What it does not do is claim a pick: the cap *is* the claim, and
    an absent one says so more honestly than a word across the middle, where
    this artwork has its starburst anyway.

    **The name box is reserved on both bars whether or not either carries a
    cap.** Kevin, 28 Aug: *"I want us to always put the name only in the space
    where the pick will not go. This way no matter what we always have the same
    space for a name."* Without that the picked player's name -- the one that
    matters most -- would be the first to ellipsize.
    """
    dx, dy = origin
    row = PICKS["row"]

    band = _art(_GEOM["row_band"]["art"])
    if band is not None:
        canvas.alpha_composite(band, (dx, dy))

    for side, box_key in (("a", "name_a"), ("b", "name_b")):
        box = _at(row[box_key], dx, dy)
        label = getattr(pick, f"{side}_label") or picks_lib.CARD_UNKNOWN
        # One size for the card, but the font itself is chosen per name: these
        # names carry Korean and Arabic, and a Latin face renders those as
        # empty boxes.
        font = _font_for_text(label, name_size, bold=True)
        _text(
            draw,
            box,
            _ellipsized(draw, label, "", font, box["w"] - 16),
            font,
            TEXT,
            align=box["align"],
        )

    if not pick.predicted:
        return

    # Blue left, red right are positional and never judgemental -- the game's
    # own convention and the VS card's rule. Which of the two the cap lands on
    # is the only thing on the row that says who to back.
    picked = "a" if pick.p_a >= pick.p_b else "b"
    cap = _pick_cap(
        words.probability(pick.p_a if picked == "a" else pick.p_b),
        mirror=picked == "b",
    )
    box = _at(row[f"cap_{picked}"], dx, dy)
    canvas.alpha_composite(cap.resize((box["w"], box["h"]), Image.LANCZOS), (box["x"], box["y"]))


def _picks_footer(canvas, draw, top: int, template: dict) -> None:
    """The one line between this card and the game's own betting market."""
    footer = template["footer"]
    art = _art(footer["art"])
    if art is not None:
        canvas.alpha_composite(art, (0, top))

    box = _at(footer["summary"], 0, top)
    text = picks_lib.CARD_FOOTER
    font = _fit(draw, text, box["w"] - 32, start=17, minimum=12)
    _text(
        draw,
        box,
        _ellipsized(draw, text, "", font, box["w"] - 32),
        font,
        MUTED,
        align=box["align"],
    )


def render_slate(slate) -> bytes:
    """The day's picks as a WebP image.

    **The canvas grows with the slate**, which is the one structural difference
    from `render`. Three meetings and twenty are the same card at two widths
    and many heights, so the header and footer are bands rather than regions of
    a fixed picture and the row band repeats. Nothing is scaled: as on the VS
    card, every coordinate is drawn at the size the layout states.

    **Rows are drawn strongest pick first.** Kevin, 27 Aug: *"I think let's go
    with strongest pick first. To me it doesn't really matter the order."* So
    the order on the card is the card's own rather than the order the maker
    entered the meetings in, and a row nobody can predict sorts to the end. In
    two columns the reading order is the left column then the right, and
    nothing on the card states that: the row numerals were removed deliberately
    and Kevin does not mind the order.

    **WebP at q=95, the same as the VS card, and this is a change.** The old
    picks card encoded lossless, correctly: it was flat colour and type, which
    is the best case for lossless and the worst for lossy ringing. It is now
    the same photographic neon artwork the VS card is, and the measurement
    followed the picture rather than the precedent. On the twenty-row card
    lossless is **854 KB against 430 KB at q=95**, for a mean error of
    1.26/255 -- the identical figure the VS card accepted -- and q=95 is also
    the *faster* encode, 0.84s against 1.34s. Checked where it would show
    first: the PICK cap's small type on gold, magnified 5x, is
    indistinguishable between the two.
    """
    template = picks_template(len(slate.picks))

    # `sorted` is stable, so meetings we cannot predict keep the order they
    # arrived in rather than being shuffled among themselves.
    picks = sorted(
        slate.picks,
        key=lambda p: (p.predicted, max(p.p_a, p.p_b) if p.predicted else 0.0),
        reverse=True,
    )

    canvas = Image.new(
        "RGBA",
        (template["width"], picks_height(len(picks))),
        _picks_colour("background") + (255,),
    )
    draw = ImageDraw.Draw(canvas)

    # The header goes down before any row. The row band is transparent above
    # its bar and the stack is lifted into the header's dead space, so the
    # first row's starburst has to bleed over the header rather than under it.
    _picks_header(canvas, draw, slate, template)

    row = PICKS["row"]
    name_size = _shared_name_size(
        draw,
        [getattr(p, f"{s}_label") or picks_lib.CARD_UNKNOWN for p in picks for s in ("a", "b")],
        row["name_a"]["w"] - 16,
    )

    split = _column_split(len(picks), template)
    at = 0
    for column_x, count in zip(template["columns"], split):
        for i in range(count):
            _picks_row(
                canvas, draw, picks[at], (column_x, _ROWS_TOP + i * _GEOM["pitch"]), name_size
            )
            at += 1

    _picks_footer(canvas, draw, _last_bar_bottom(max(split)) + _GEOM["trailing"], template)

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="WEBP", quality=95, method=6)
    return buf.getvalue()

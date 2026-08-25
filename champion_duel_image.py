"""The Champion Duel prediction cards.

**Two cards live here.** `render` draws one matchup on the designed VS card and
is what the rest of this docstring is about. `render_slate` draws a whole day's
picks as a list of matchups, and it works differently in one important way: its
height is not fixed, so it has no static template and paints its own bands
instead. Its own section at the foot of this file carries the reasoning.

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
# A second card, and a different kind of one. The VS card composites a finished
# template; this one draws its own bands, because **a slate's height is not
# fixed**: a card carries five to seventeen meetings and grows with them, so
# there is no single image the artwork could arrive as. What it can arrive as is
# three bands — a header, one row, a footer — which `bands` in the layout names
# and `_band_art` composites the moment the files exist. Until then the bands
# are painted from `palette`, in the VS card's own colours so the two read as
# one product.
#
# Everything else follows the VS card deliberately: the same fonts through
# `storm_renderer`, the same `words.probability` refusal to round a certainty
# into existence, the same positional blue-left / red-right, and the same bar
# routine over a track the layout names.


def _picks_colour(key: str) -> tuple:
    return ImageColor.getrgb(PICKS["palette"][key])


def picks_height(rows: int) -> int:
    """How tall a card with `rows` meetings comes out.

    Public because the layout tests assert against it and a surface sizing an
    upload wants it without rendering first.
    """
    return PICKS["header"]["h"] + rows * PICKS["row"]["pitch"] + PICKS["footer"]["h"]


def _vertical_gradient(w: int, h: int, top: tuple, bottom: tuple):
    """One column of colour, stretched.

    Cheaper than filling per pixel, and the same trick `_split_bar` uses along
    the other axis.
    """
    column = bytearray()
    for py in range(h):
        t = py / max(h - 1, 1)
        column.extend(round(c0 + (c1 - c0) * t) for c0, c1 in zip(top, bottom))
    return Image.frombytes("RGB", (1, h), bytes(column)).resize((w, h)).convert("RGBA")


def _band_art(key: str, w: int, h: int):
    """The finished artwork for one band, or None while there is none.

    A named file that is not on disk is treated as absent rather than raised
    on, which is the opposite of the VS card's rule about its template. The
    difference is what the failure costs: that card cannot be drawn at all
    without its background, where this one is drawn either way and a missing
    band is a card that looks plainer than intended.
    """
    name = (PICKS.get("bands") or {}).get(key)
    if not name:
        return None
    path = os.path.join(_ASSETS, name)
    if not os.path.isfile(path):
        return None
    return Image.open(path).convert("RGBA").resize((w, h), Image.LANCZOS)


def _plate(box: dict, fill: tuple, border: tuple):
    """One row's plate, drawn once and pasted per row.

    Every row is the same shape, so the 4x supersample that keeps a 12px corner
    from stepping is paid once for the card rather than once for each of
    seventeen rows.
    """
    s = _BAR_SCALE
    w, h = box["w"], box["h"]
    layer = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        [0, 0, w * s - 1, h * s - 1],
        radius=box["radius"] * s,
        fill=fill + (255,),
        outline=border + (255,),
        width=s,
    )
    return layer.resize((w, h), Image.LANCZOS)


def _at(box: dict, dy: int) -> dict:
    """A row box moved onto the row it is being drawn for.

    Row boxes are stored once, with `y` measured from the top of the row band
    rather than from the top of the card. There is only one row shape, and a
    layout that repeated it seventeen times would be seventeen places for a
    revision to half-land.
    """
    return {**box, "y": box["y"] + dy}


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
    """
    for size in range(30, 18, -1):
        if all(
            draw.textlength(label, font=_font_for_text(label, size, bold=True)) <= max_w
            for label in labels
        ):
            return size
    return 19


def _picks_header(canvas, draw, slate, width: int) -> None:
    """Title, what the card is of, the column heading, and the badge."""
    header = PICKS["header"]
    art = _band_art("header", width, header["h"])
    if art is not None:
        canvas.alpha_composite(art, (0, 0))

    box = header["event_title"]
    title = picks_lib.CARD_TITLE
    _text(draw, box, title, _font_for_text(title, 34, bold=True), TEXT, align=box["align"])

    box = header["round_metadata"]
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

    # Two lines rather than one, because the column is as wide as a word and a
    # bare "CONFIDENCE" over a card of paired probabilities reads as confidence
    # in one of the players.
    box = header["confidence_heading"]
    heading_font = _font_for_text("H", 15, bold=True)
    for i, line in enumerate(picks_lib.CARD_CONFIDENCE_HEADING):
        _text(
            draw,
            _at(box, i * box["line_height"]),
            line,
            heading_font,
            MUTED,
            align=box["align"],
        )

    rule = header["rule"]
    draw.rectangle(
        [rule["x"], rule["y"], rule["x"] + rule["w"] - 1, rule["y"] + rule["h"] - 1],
        fill=_picks_colour("rule"),
    )
    _logo(canvas, header["logo_badge"])


def _picks_row(canvas, draw, pick, top: int, name_size: int, fonts) -> None:
    """One meeting.

    A row we cannot predict keeps both names and says so across the middle
    instead of carrying two percentages and a bar. It is the most useful row on
    the card — it names two players nobody has scouted, to the alliance about
    to read it — so it is drawn rather than dropped.
    """
    row = PICKS["row"]
    _text(draw, _at(row["index"], top), str(pick.position), fonts["index"], MUTED)

    for side, box_key in (("a", "name_a"), ("b", "name_b")):
        box = _at(row[box_key], top)
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
        box = _at(row["no_prediction"], top)
        _text(draw, box, picks_lib.CARD_NO_PREDICTION, fonts["absent"], MUTED, align=box["align"])
        return

    for prob, box_key in ((pick.p_a, "probability_a"), (pick.p_b, "probability_b")):
        box = _at(row[box_key], top)
        text = words.probability(prob)
        _text(draw, box, text, _font_for_text(text, 26, bold=True), TEXT, align=box["align"])

    _split_bar(canvas, _at(row["track"], top), pick.p_a)

    box = _at(row["confidence"], top)
    level = pick.confidence().capitalize()
    _text(draw, box, level, fonts["confidence"], MUTED, align=box["align"])


def _picks_footer(canvas, draw, top: int, width: int) -> None:
    """The one line between this card and the game's own betting market."""
    footer = PICKS["footer"]
    art = _band_art("footer", width, footer["h"])
    if art is not None:
        canvas.alpha_composite(art, (0, top))

    box = _at(footer["summary"], top)
    text = picks_lib.CARD_FOOTER
    font = _fit(draw, text, box["w"] - 32, start=21, minimum=15)
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
    from `render`. Five meetings and seventeen meetings are the same card at
    two heights, so the header and footer are bands rather than regions of a
    fixed picture and the row band repeats. Nothing is scaled: as on the VS
    card, every coordinate is drawn at the size the layout states.

    **WebP, but LOSSLESS where the VS card is q=95.** The two settings are
    right for two different pictures. That card is mostly a photographic neon
    background, where lossy WebP is four fifths smaller at an error nothing on
    the artwork shows. This one is flat colour and type, which is the worst
    case for lossy ringing and the best case for lossless: measured on a
    seventeen-row card, lossless is **79 KB against 132 KB at q=95** and exact
    rather than approximate. It costs about half a second more to encode, and
    the bytes are read far more often than they are written.
    """
    if not slate.picks:
        raise ValueError("a picks card needs at least one meeting")

    width = PICKS["canvas"]["width"]
    height = picks_height(len(slate.picks))
    canvas = _vertical_gradient(
        width, height, _picks_colour("background_top"), _picks_colour("background_bottom")
    )
    draw = ImageDraw.Draw(canvas)
    row = PICKS["row"]

    _picks_header(canvas, draw, slate, width)

    name_size = _shared_name_size(
        draw,
        [
            getattr(p, f"{s}_label") or picks_lib.CARD_UNKNOWN
            for p in slate.picks
            for s in ("a", "b")
        ],
        row["name_a"]["w"] - 16,
    )
    fonts = {
        "index": _font_for_text("1", 22, bold=True),
        "confidence": _font_for_text("H", 19),
        "absent": _font_for_text("H", 20),
    }
    plates = (
        _plate(row["plate"], _picks_colour("row_plate"), _picks_colour("row_border")),
        _plate(row["plate"], _picks_colour("row_plate_alt"), _picks_colour("row_border")),
    )
    band = _band_art("row", width, row["pitch"])

    top = PICKS["header"]["h"]
    for i, pick in enumerate(slate.picks):
        y = top + i * row["pitch"]
        if band is not None:
            canvas.alpha_composite(band, (0, y))
        else:
            canvas.alpha_composite(plates[i % 2], (row["plate"]["x"], y + row["plate"]["y"]))
        _picks_row(canvas, draw, pick, y, name_size, fonts)

    _picks_footer(canvas, draw, top + len(slate.picks) * row["pitch"], width)

    buf = io.BytesIO()
    # `method=4` rather than 6: at lossless the two produce the same picture
    # and 6 is the slower search. It is 4 here and 6 on the VS card because
    # only one of them is paying that search for a smaller file.
    canvas.convert("RGB").save(buf, format="WEBP", lossless=True, method=4)
    return buf.getvalue()

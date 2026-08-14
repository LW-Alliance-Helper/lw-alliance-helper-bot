"""The Champion Duel prediction, rendered on the designed VS card.

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

from PIL import Image, ImageChops, ImageDraw, ImageFilter

import champion_duel_predict as predict_lib
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

_CONFIDENCE_COPY = {
    "high": "Built on observed squads and recorded sightings",
    "medium": "Part of this line-up is estimated from total hero power",
    "low": "Both line-ups are estimates — neither player has been seen deploying",
}

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

    Vertical placement measures a fixed reference string rather than the text
    itself, so a row reading "Tank" and one reading "Missile 31.5M" share a
    baseline instead of each centring on its own ink. `metric="ink"` opts out,
    for the big percentage where the glyphs *are* the block.
    """
    ink = draw.textbbox((0, 0), text, font=font)
    ref = ink if metric == "ink" else draw.textbbox((0, 0), "Hg", font=font)
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


def _pct(prob: float) -> str:
    """A probability as text, refusing to round certainty into existence.

    `f"{0.9999:.0%}"` is "100%", which claims the match cannot be lost. The
    engine is decisive — a 35% power edge puts it past 0.999 — so this is the
    common case for a lopsided pairing, not an edge case. Upsets happen, and a
    card that says 100% before one is a card nobody trusts afterwards.
    """
    if prob >= 0.995:
        return ">99%"
    if prob <= 0.005:
        return "<1%"
    return f"{prob:.0%}"


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


def _status(side) -> str:
    """What the line-up beside it is built on.

    Says which of the two orders is on screen, so the reader never has to guess
    whether they are looking at a sighting or a default.
    """
    text = f"{side.observed_squads}/3 seen · "
    return text + (
        f"their order in {side.sightings} sighting{'s' if side.sightings != 1 else ''}"
        if side.likely_order()[1]
        else "never seen deploying — assuming strongest first"
    )


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

    pct = _pct(prob)
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
    status = _status(side)
    font = _font_for_text(status, status_size)
    _text(draw, box, _ellipsized(draw, status, "", font, box["w"] - 16), font, MUTED)


def _odds_bar(canvas, p_a: float) -> None:
    """One bar, split where the odds split.

    The two sides each state a percentage; this is what makes the *gap* legible
    at a glance, which is the thing a reader actually wants and the thing two
    separate numbers are worst at conveying.

    The template supplies an empty track and nothing else — the whole fill is
    computed here. The divider is clamped a radius in from each end so a
    lopsided prediction still reads as a bar with rounded caps rather than as
    a shape with one corner cut off.
    """
    track = LAYOUT["dynamic_progress_track"]
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


def _footer(draw, confidence: str) -> None:
    """What the number is worth, in the reader's words rather than a score.

    It spans the full width under the bar because it qualifies the whole card,
    not one side of it.
    """
    box = LAYOUT["footer"]["confidence_summary"]
    text = f"Confidence: {confidence} · {_CONFIDENCE_COPY[confidence]}"
    font = _fit(draw, text, box["w"] - 32, start=21, minimum=15)
    _text(draw, box, _ellipsized(draw, text, "", font, box["w"] - 32), font, MUTED)


def _logo(canvas, box: dict) -> None:
    """Attribution in the header badge, matching the storm render's convention.

    **Fitted to the frame, not filled.** The badge is slightly wider than it is
    tall (103×88) and the logo is square, so filling it edge to edge means
    either stretching the mark or cropping 7% off the top and bottom — which
    takes the antenna off the robot and cuts "HELPER" in half. It runs the full
    height of the frame instead, centred, leaving a little of the frame's own
    dark either side.

    Corners are rounded to the frame's radius so it reads as seated in the
    badge rather than laid over it, and rounded at 4x like the odds bar —
    Pillow leaves them stepped at this size otherwise.

    A missing or unreadable asset is skipped rather than raised: the render
    must not fail over branding.
    """
    if not os.path.isfile(_LOGO_PATH):
        return
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA")
    except Exception:  # noqa: BLE001 - a missing logo must not fail the render
        return

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
        _pct(result.p_a),
        _pct(result.p_b),
        (left["win_probability"], right["win_probability"]),
    )
    status_size = _shared_status_font(
        draw, _status(result.a), _status(result.b), (left["status"], right["status"])
    )
    _side(draw, left, result.a, result.p_a, pct_size, status_size, accent=LEFT_ACCENT)
    _side(draw, right, result.b, result.p_b, pct_size, status_size, accent=RIGHT_ACCENT)
    _odds_bar(canvas, result.p_a)
    _footer(draw, result.confidence())

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="WEBP", quality=95, method=6)
    return buf.getvalue()

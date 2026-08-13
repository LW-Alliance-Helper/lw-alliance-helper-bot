"""The Champion Duel prediction, rendered as a shareable card.

The embed carries the same numbers. This exists because an embed is not what
gets forwarded into an alliance chat, and sharing is how a prediction earns the
sightings that sharpen the next one — the dataset is only worth anything if
more people contribute, so the output has to be worth passing on.

**The layout borrows the game's own matchup screen** (`notes/DESIGN.md`, emoji
rule 5, applied to a whole surface rather than one glyph): two cards angled in
toward a gold VS, blue on the left and red on the right, name plates as pills,
stats stacked beneath. Players arrive having already learned that screen, so
reading ours costs them nothing.

**Blue and red are positional, not judgemental.** In the game they mark you and
your opponent; here they mark the first and second name typed, because the bot
does not know which of the two the reader is rooting for. That distinction is
why the winning side is never rendered in green — `notes/DESIGN.md` is explicit
that green means good *for the alliance reading it*, and a scouting tool
predicting two strangers' match has no such side. What separates the two cards
is the probability itself, at a size nothing else on the card competes with.

Fonts and the script-fallback rules come from `storm_renderer`, imported rather
than reimplemented: Champion Duel names routinely carry non-Latin scripts, and
that module already knows which file renders them.
"""

from __future__ import annotations

import io
import os

from PIL import Image, ImageDraw

import champion_duel_predict as predict_lib
from storm_renderer import _font_for_text

# Rendered at 2x and downsampled, which is how `storm_renderer` gets clean
# edges out of Pillow's non-antialiased primitives.
SCALE = 2
W, H = 1200, 700

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOGO_PATH = os.path.join(_HERE, "assets", "branding", "lw-alliance-helper-logo.png")

# Pulled from the game's own matchup screen so the card reads as continuous
# with it. The background is the deep navy behind the VS; the two card colours
# are its blue and red panels.
INK = (18, 26, 48)
INK_LIGHT = (30, 42, 74)
BLUE = (58, 124, 214)
BLUE_DEEP = (32, 78, 148)
RED = (214, 76, 66)
RED_DEEP = (148, 42, 38)
GOLD = (255, 198, 62)
WHITE = (255, 255, 255)
MUTED = (168, 182, 212)
PLATE = (12, 18, 36)


def _s(v: float) -> int:
    return int(round(v * SCALE))


def _fit(draw, text: str, size: int, max_w: int, *, bold: bool = False):
    """Largest font at or below `size` that fits `text` into `max_w`.

    Player names are user-supplied and some are very long. Shrinking beats
    truncating here: the name is the one thing on the card a reader uses to
    confirm the prediction is about who they think it is.
    """
    for candidate in range(size, 11, -2):
        font = _font_for_text(text, _s(candidate), bold=bold)
        if draw.textlength(text, font=font) <= _s(max_w):
            return font
    return _font_for_text(text, _s(12), bold=bold)


def _centered(draw, cx: int, y: int, text: str, font, fill):
    draw.text((cx - draw.textlength(text, font=font) / 2, y), text, font=font, fill=fill)


def _card(canvas, x: int, y: int, w: int, h: int, top, bottom, *, lean: int):
    """One side's panel, leaning toward the centre like the game's do.

    Drawn as its own RGBA layer so the parallelogram can be alpha-composited
    with a soft edge rather than left with Pillow's hard polygon border.
    """
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0, x1, y1 = _s(x), _s(y), _s(x + w), _s(y + h)
    skew = _s(lean)
    d.polygon([(x0 + skew, y0), (x1 + skew, y0), (x1, y1), (x0, y1)], fill=top)
    # A band across the lower third, which is what gives the game's cards their
    # sense of depth without needing a real gradient.
    band = y0 + int((y1 - y0) * 0.62)
    ratio = (band - y0) / (y1 - y0)
    inset = int(skew * (1 - ratio))
    d.polygon([(x0 + inset, band), (x1 + inset, band), (x1, y1), (x0, y1)], fill=bottom)
    canvas.alpha_composite(layer)


def _plate(draw, cx: int, y: int, w: int, h: int, text: str, font):
    """The game's name plate: a dark pill with a gold hairline."""
    x0, x1 = _s(cx - w // 2), _s(cx + w // 2)
    draw.rounded_rectangle(
        [x0, _s(y), x1, _s(y + h)], radius=_s(h // 2), fill=PLATE, outline=GOLD, width=_s(1.5)
    )
    _centered(draw, _s(cx), _s(y + h * 0.22), text, font, WHITE)


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


def _side(canvas, draw, side, prob: float, *, left: bool):
    """One competitor: plate, probability, line-up, and what it's built on."""
    cx = 300 if left else 900
    name = side.name or "(unknown)"
    label = f"{name}  #{side.server}" if side.server else name

    plate_font = _fit(draw, label, 29, 320, bold=True)
    _plate(draw, cx, 162, 380, 52, label, plate_font)

    # The probability is the card's subject, so nothing else competes with it.
    pct = _pct(prob)
    _centered(draw, _s(cx), _s(232), pct, _font_for_text(pct, _s(88), bold=True), WHITE)
    _centered(draw, _s(cx), _s(338), "to win", _font_for_text("x", _s(22)), MUTED)

    # The line-up in the order the prediction assumed they will deploy in --
    # NOT the natural slot order, when the two differ. Order decides which
    # squad meets which, and the counter triangle means it can outweigh power,
    # so showing one order beside a number computed from another is how a
    # reader talks themselves into distrusting a correct prediction.
    lineup, from_sightings = side.likely_order()
    row_font = _font_for_text("x", _s(26), bold=True)
    mark_font = _font_for_text("x", _s(17))
    for i, (power, squad_type) in enumerate(lineup):
        y = 396 + i * 46
        draw.text((_s(cx - 150), _s(y)), f"{i + 1}", font=mark_font, fill=MUTED)
        draw.text((_s(cx - 128), _s(y - 4)), squad_type, font=row_font, fill=WHITE)
        power_text = f"{power / 1_000_000:.1f}M"
        draw.text(
            (_s(cx + 150) - draw.textlength(power_text, font=row_font), _s(y - 4)),
            power_text,
            font=row_font,
            fill=WHITE,
        )

    # Say which of the two orders is on screen, so the reader never has to
    # guess whether they are looking at a sighting or a default.
    tail = f"{side.observed_squads}/3 seen · "
    tail += (
        f"their order in {side.sightings} sighting{'s' if side.sightings != 1 else ''}"
        if from_sightings
        else "never seen deploying — assuming strongest first"
    )
    _centered(draw, _s(cx), _s(548), tail, _fit(draw, tail, 19, 420), MUTED)


def _vs(canvas, draw):
    """The gold VS. Straight from the game — it is the single strongest cue
    that this card is about a Champion Duel match and not a leaderboard."""
    text = "VS"
    font = _font_for_text(text, _s(76), bold=True)
    cx, y = _s(600), _s(268)
    width = draw.textlength(text, font=font)
    # A cheap outline: the glyph in deep ink, offset in eight directions, with
    # the gold laid over it. Pillow's stroke_width does the same job but bloats
    # the letterform at this size.
    for dx in (-3, 0, 3):
        for dy in (-3, 0, 3):
            if dx or dy:
                draw.text((cx - width / 2 + dx, y + dy), text, font=font, fill=INK)
    draw.text((cx - width / 2, y), text, font=font, fill=GOLD)


def _odds_bar(draw, p_a: float):
    """One bar, split where the odds split.

    The two cards each state a percentage; this is what makes the *gap* legible
    at a glance, which is the thing a reader actually wants and the thing two
    separate numbers are worst at conveying.
    """
    x0, x1, y, h = 120, 1080, 604, 24
    split = x0 + int((x1 - x0) * max(0.0, min(1.0, p_a)))
    draw.rounded_rectangle([_s(x0), _s(y), _s(x1), _s(y + h)], radius=_s(h / 2), fill=RED_DEEP)
    if split > x0:
        draw.rounded_rectangle([_s(x0), _s(y), _s(split), _s(y + h)], radius=_s(h / 2), fill=BLUE)
    draw.line([(_s(split), _s(y - 4)), (_s(split), _s(y + h + 4))], fill=WHITE, width=_s(2))


def _header(canvas, draw, subtitle: str | None):
    """Title bar. The logo sits at the right end and the subtitle stops short
    of it -- the stage string is caller-supplied and long enough to collide."""
    draw.rectangle([0, 0, _s(W), _s(88)], fill=INK_LIGHT)
    draw.text(
        (_s(48), _s(26)), "CHAMPION DUEL", font=_font_for_text("C", _s(34), bold=True), fill=WHITE
    )
    logo_w = _logo(canvas)
    if subtitle:
        right = _s(W - 40) - logo_w
        font = _fit(draw, subtitle, 24, (right / SCALE) - 380)
        draw.text(
            (right - draw.textlength(subtitle, font=font), _s(34)), subtitle, font=font, fill=MUTED
        )
    draw.line([(0, _s(88)), (_s(W), _s(88))], fill=GOLD, width=_s(2))


def _footer(canvas, draw, confidence: str):
    """What the number is worth, in the reader's words rather than a score.

    It sits under the bar rather than beside a percentage because it qualifies
    the whole card, not one side of it.
    """
    note = {
        "high": "Built on observed squads and recorded sightings",
        "medium": "Part of this line-up is estimated from total hero power",
        "low": "Both line-ups are estimates — neither player has been seen deploying",
    }[confidence]
    text = f"Confidence: {confidence} · {note}"
    _centered(draw, _s(600), _s(652), text, _font_for_text(text, _s(19)), MUTED)


def _logo(canvas) -> int:
    """Attribution in the header, matching the storm render's convention.

    Returns the width it occupied so the subtitle can stop short of it, or 0
    when the asset is missing — the render must not fail over branding.
    """
    if not os.path.isfile(_LOGO_PATH):
        return 0
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA")
    except Exception:  # noqa: BLE001 - a missing logo must not fail the render
        return 0
    target_h = _s(44)
    logo = logo.resize((int(logo.width * target_h / logo.height), target_h), Image.LANCZOS)
    canvas.alpha_composite(logo, (_s(W) - logo.width - _s(28), _s(22)))
    return logo.width + _s(28)


def render(result: predict_lib.Prediction, *, subtitle: str | None = None) -> bytes:
    """The prediction as a PNG.

    `subtitle` is for the stage — "Group M · Qualifier Day 4/4" — which the
    game puts at the top of its own screen. Left optional because the bot does
    not know the schedule; a caller that does can pass it.
    """
    canvas = Image.new("RGBA", (_s(W), _s(H)), INK)
    draw = ImageDraw.Draw(canvas)

    _header(canvas, draw, subtitle)
    # Lean the cards toward each other, as the game's do, with a wide enough
    # centre channel for the VS to sit in rather than on top of them.
    _card(canvas, 56, 116, 484, 464, BLUE, BLUE_DEEP, lean=34)
    _card(canvas, 660, 116, 484, 464, RED, RED_DEEP, lean=-34)

    draw = ImageDraw.Draw(canvas)
    _side(canvas, draw, result.a, result.p_a, left=True)
    _side(canvas, draw, result.b, result.p_b, left=False)
    _vs(canvas, draw)
    _odds_bar(draw, result.p_a)
    _footer(canvas, draw, result.confidence())

    canvas = canvas.convert("RGB").resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

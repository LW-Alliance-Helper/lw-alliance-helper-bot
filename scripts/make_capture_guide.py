"""Build the annotated 'where do I find this number' images for `/champion_duel`.

The write flows ask for three values — a squad's deployment position, its type,
and its power — and none of them are labelled that way in game. Someone who has
never entered data has to be shown the screen, not told the field name. This is
the same thing `qualifier_data/capture_guide/` does for the simulator's manual
entry process, aimed at the two fields the hub actually asks for.

**Sources are consented captures.** Both players shown (`pinkcatboi` and
`PlumpNSupple`, both #738 OGV) gave permission for their battle report to be
used here. That matters because this repo is public: nothing about Champion
Duel roster or scouting data goes in it otherwise, and these images are an
exception granted per-person rather than a relaxation of the rule.

The sources themselves are not committed — only the annotated output, which is
what the bot ships. Re-run with `--src` pointing at the originals to rebuild.

Annotation approach is lifted from the simulator's `make_annotations_v2.py`:
a gutter down the left with numbered discs, a legend banner across the top, and
fractional bounding boxes. Fractions are measured against these two 626x1379
captures specifically and do not transfer to a different aspect ratio — the
simulator's own guide had to re-measure every box when its source changed.
"""

from __future__ import annotations

import argparse
import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(HERE, "assets", "champion_duel")

# Both marker colours have to clear WCAG 2.2 AA's 3:1 for graphical objects
# against TWO very different grounds: the game's near-white panels, and the dark
# banner the legend discs sit on. The first pass used gold, which is 1.8:1 on
# white -- it looked fine in isolation and vanished against the screenshot.
# `check_contrast()` below asserts both directions on every build rather than
# leaving it to the eye.
MARK = (240, 56, 79)  # the value the field is asking for
CTX = (77, 141, 247)  # context: which side, which slot
HALO = (10, 13, 18)  # drawn under every marker so mid-tone areas separate too
BG = (17, 19, 26)
PAPER = (255, 255, 255)
GUTTER = 54

# The Windows title bar carries nothing and costs vertical space on a phone,
# where this is read. Trimmed along with the dead area under the game panel.
CROP_TOP = 32
CROP_BOTTOM = 40


def font(size: int, bold: bool = True):
    for name in (("arialbd.ttf" if bold else "arial.ttf"), "segoeuib.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _luminance(rgb) -> float:
    channels = []
    for value in rgb:
        c = value / 255
        channels.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = channels
    return 0.2126 * r + 0.0722 * b + 0.7152 * g


def contrast(a, b) -> float:
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def check_contrast() -> None:
    """Fail the build rather than ship markers nobody can see.

    3:1 is WCAG 2.2 AA for non-text graphical objects (1.4.11). Each marker is
    checked against the game's white panels and against the dark legend banner,
    because it appears on both.
    """
    failures = []
    for name, colour in (("MARK", MARK), ("CTX", CTX)):
        for ground_name, ground in (("paper", PAPER), ("banner", BG)):
            ratio = contrast(colour, ground)
            status = "ok " if ratio >= 3.0 else "FAIL"
            print(f"  {status} {name} on {ground_name}: {ratio:.2f}:1")
            if ratio < 3.0:
                failures.append(f"{name} on {ground_name} is {ratio:.2f}:1, needs 3:1")
    if failures:
        raise SystemExit("contrast check failed:\n  " + "\n  ".join(failures))


def annotate(src_path: str, out_name: str, boxes: list) -> str:
    """A cropped screenshot with numbered markers. No words baked in.

    The first version burned a title, a subtitle and a legend into the picture.
    That put the entire explanation in the one format nobody can select,
    translate, resize, or hear — and it read like a developer annotating a
    ticket. The words live in the Discord message now; the image only has to
    say *where*, and the numbers key it to the text.

    Boxes are fractions of the ORIGINAL capture, not of the cropped result. The
    crop is converted here so changing it cannot silently move every marker,
    which is how an earlier build put a box over the score bar instead of the
    player names.
    """
    image = Image.open(src_path).convert("RGB")
    original_h = image.height
    image = image.crop((0, CROP_TOP, image.width, original_h - CROP_BOTTOM))
    w, h = image.size

    def rescale(frac):
        top = (frac[1] * original_h - CROP_TOP) / h
        bottom = (frac[3] * original_h - CROP_TOP) / h
        return (frac[0], top, frac[2], bottom)

    canvas = Image.new("RGB", (w + GUTTER, h), BG)
    canvas.paste(image, (GUTTER, 0))
    draw = ImageDraw.Draw(canvas)

    for i, (frac, colour) in enumerate(boxes, start=1):
        frac = rescale(frac)
        x0, x1 = int(frac[0] * w) + GUTTER, int(frac[2] * w) + GUTTER
        y0, y1 = int(frac[1] * h), int(frac[3] * h)
        # Dark halo first, colour over it. The measured ratios hold against
        # white and against the gutter, but the screenshot also carries mid-tone
        # blues and pinks; the halo guarantees an edge everywhere rather than
        # only where the maths was checked.
        for t in (5, 6):
            draw.rectangle([x0 - t, y0 - t, x1 + t, y1 + t], outline=HALO)
        for t in range(5):
            draw.rectangle([x0 - t, y0 - t, x1 + t, y1 + t], outline=colour)

        r, cx = 20, GUTTER // 2
        cy = max(r + 2, min(h - r - 2, y0 + min(30, (y1 - y0) // 2)))
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
        bb = draw.textbbox((0, 0), str(i), font=font(24))
        draw.text(
            (cx - (bb[2] - bb[0]) / 2, cy - (bb[3] - bb[1]) / 2 - 3),
            str(i),
            fill=(20, 22, 30),
            font=font(24),
        )
        draw.line([cx + r + 2, cy, x0 - 7, cy], fill=colour, width=3)

    os.makedirs(OUT_DIR, exist_ok=True)
    out_path = os.path.join(OUT_DIR, out_name)
    canvas.save(out_path, optimize=True)
    return out_path


# Measured against the two 626x1379 captures. Re-measure for any other source.
#
# The order is the ROUND 1 panels, not the Overview rows above them. Overview is
# a score summary; Round 1 is where the squads themselves are. That also matches
# how the engine models a match — round 1 is all three slot pairings fought in
# parallel, slot 1 against slot 1 — so the three panels are the three slots in
# the order they were sent.
ORDER_BOXES = [
    ((0.03, 0.470, 0.97, 0.600), MARK),
    ((0.03, 0.606, 0.97, 0.736), MARK),
    ((0.03, 0.744, 0.97, 0.872), MARK),
]

# Numbered down the page, not by importance. A reader works top to bottom and a
# marker order that disagrees with reading order makes them hunt for "1".
SQUAD_BOXES = [
    ((0.02, 0.070, 0.98, 0.180), CTX),
    ((0.02, 0.245, 0.98, 0.283), MARK),
    ((0.10, 0.560, 0.46, 0.600), MARK),
]

# Alt text lives in `champion_duel_hub.py` beside the message that pairs with
# each image, not here. The instructions are Discord text now, so alt text has
# the narrower job of saying where each numbered marker sits — and it belongs
# next to the words it complements rather than in the build script.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_src = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
    parser.add_argument("--src", default=default_src, help="folder holding the two captures")
    parser.add_argument("--order", default="Screenshot 2026-08-13 203443.png")
    parser.add_argument("--squad", default="Screenshot 2026-08-13 203503.png")
    args = parser.parse_args()

    print("contrast (WCAG 2.2 AA needs 3:1 for graphical objects):")
    check_contrast()

    written = [
        annotate(os.path.join(args.src, args.order), "guide_order.png", ORDER_BOXES),
        annotate(os.path.join(args.src, args.squad), "guide_squad.png", SQUAD_BOXES),
    ]
    for path in written:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

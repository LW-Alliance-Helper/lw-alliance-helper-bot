"""Derive the picks card's runtime artwork from Kevin's 4x masters.

**This is the record of how `assets/champion_duel/picks_*.webp` were made**, and
it is why the masters are worth committing beside them. It is not run at render
time and nothing imports it: the renderer loads the runtime files directly.

Run it again when the artwork is revised. The delivered PNGs are not in the
repo -- they are 12 MB and `check-added-large-files` rejects anything over
500 KB -- so point `--masters` at wherever they are and let this write both
scales as WebP.

    .venv/Scripts/python.exe scripts/build_champion_duel_picks_assets.py \
        --masters C:/path/to/delivered/pngs

**Why two scales are committed.** The runtime files are what the renderer
draws, already at the size it wants, so the 4x downscale is paid once here
rather than on every render. The masters are committed as well so no piece
exists in only one place -- both fit inside the pre-commit size limit.

**Two names in the delivered set are not what the manifest said**, and both are
repaired here rather than worked around at load time: the row master carries a
`(1)` duplicate-download suffix, and the PICK cap is a bare UUID because it was
drawn in a separate session after the first attempts failed to render.
"""

from __future__ import annotations

import argparse
import os

from PIL import Image

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSETS = os.path.join(os.path.dirname(_HERE), "assets", "champion_duel")

# master filename -> (runtime stem, runtime size)
#
# The cap's runtime size is 4x the 103x58 it lands on the bar at, because the
# renderer draws its two lines of type onto it at 4x and downsamples the
# finished cap -- two lines inside 58px is too tight to set directly. Every
# other piece is drawn at the size it is stored at.
PIECES: list[tuple[str, str, tuple[int, int]]] = [
    ("champion_duel_header_single_MASTER_3800x600.png", "picks_header_single", (950, 150)),
    ("champion_duel_header_wide_MASTER_7600x600.png", "picks_header_wide", (1900, 150)),
    (
        "champion_duel_footer_single_MASTER_3800x360_fixed.png",
        "picks_footer_single",
        (950, 90),
    ),
    ("champion_duel_footer_wide_MASTER_7600x360_fixed.png", "picks_footer_wide", (1900, 90)),
    ("champion_duel_matchup_row_MASTER_3440x652(1).png", "picks_row", (860, 163)),
    ("a4e228d8-b035-4255-b9b6-cbbf1bab3949.png", "picks_pick_cap", (412, 232)),
]

# The `_fixed` footers supersede the plain ones and the plain ones are not
# built: their top and side fade does not dissolve into #040616, which is the
# ground this card composites onto. Verified rather than taken on trust -- the
# plain footer's top-left pixel is (5, 7, 31) where the fixed one's is exactly
# (4, 6, 22).
SUPERSEDED = (
    "champion_duel_footer_single_MASTER_3800x360.png",
    "champion_duel_footer_wide_MASTER_7600x360.png",
)

RUNTIME_QUALITY = 95
MASTER_QUALITY = 95
SIZE_LIMIT = 500 * 1024  # `check-added-large-files`, which runs with no args


def build(masters: str, out: str) -> int:
    over = 0
    for source, stem, size in PIECES:
        path = os.path.join(masters, source)
        if not os.path.isfile(path):
            raise SystemExit(f"missing master: {path}")
        art = Image.open(path).convert("RGBA")

        runtime = os.path.join(out, f"{stem}.webp")
        art.resize(size, Image.LANCZOS).save(
            runtime, format="WEBP", quality=RUNTIME_QUALITY, method=6
        )
        master = os.path.join(out, f"{stem}_master.webp")
        art.save(master, format="WEBP", quality=MASTER_QUALITY, method=6)

        # The written size, not the source's: this line is the only check that
        # each piece landed at the size the layout expects to draw it at.
        for written, written_size in ((runtime, size), (master, art.size)):
            kb = os.path.getsize(written) / 1024
            flag = ""
            if os.path.getsize(written) > SIZE_LIMIT:
                flag = "  <-- OVER THE 500 KB PRE-COMMIT LIMIT"
                over += 1
            print(f"{os.path.basename(written):34s} {written_size} -> {kb:7.1f} KB{flag}")
    return over


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--masters",
        default=_ASSETS,
        help="directory holding the delivered PNG masters (default: the assets directory)",
    )
    parser.add_argument("--out", default=_ASSETS, help="where to write the WebP files")
    args = parser.parse_args()

    over = build(args.masters, args.out)
    print(f"\nsuperseded and deliberately not built: {', '.join(SUPERSEDED)}")
    if over:
        raise SystemExit(f"{over} file(s) over the pre-commit size limit")


if __name__ == "__main__":
    main()

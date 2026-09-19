"""Render sample picks cards to disk, without Discord.

Reading the renderer is not how the VS card's last defects were found; looking
at a card was -- its whole right-hand column sat 13-17px off its panel and
every row sat high, and both were found in a render. This is the same tool for
the picks card, and it covers the cases most likely to break: the two
templates, the column split that leaves a short column, the clamp that stops a
row claiming a match cannot be lost, and names in scripts a Latin face cannot
draw.

    python scripts/preview_champion_duel_picks.py [outdir]

Defaults to `notes/preview_picks/`, which is gitignored. Open the `index.html`
it prints. Nothing here ships.

**The slates are stand-ins rather than real ones**, which is deliberate and is
the one thing to know before editing this file. `scripts/preview_champion_duel_card.py`
builds its cases through the database because a VS card needs squads, orders
and a real prediction. A picks row needs four fields, and going through the
database instead would tie this preview to a schema that is being rewritten
underneath it -- and would make it impossible to ask for a twenty-row card
without fabricating forty players. `_Pick` and `_Slate` below are therefore the
renderer's actual contract, written out; if `render_slate` ever wants more than
these, they are the first place to add it.

**Every name here is obviously invented.** No real player name goes in the
repo -- it is public -- and the decorated ones are built to exercise the font
fallback and the ellipsis, not to resemble anyone.
"""

from __future__ import annotations

import html
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import champion_duel_image as img  # noqa: E402


class _Pick:
    """One meeting, as `render_slate` reads it."""

    def __init__(self, a_label: str, b_label: str, p_a: float | None):
        self.a_label = a_label
        self.b_label = b_label
        self.predicted = p_a is not None
        self.p_a = p_a
        self.p_b = None if p_a is None else 1.0 - p_a


class _Slate:
    def __init__(self, subject: str, picks: list[_Pick]):
        self.picks = picks
        self._subject = subject

    def subject(self) -> str:
        return self._subject


# Mock names, in the proposal's own convention plus a few built to stress the
# type. `Kilo|||||` is the pipe padding in-game names carry; the Hangul and Han
# ones are full-width, which is what the 20-character limit does NOT bound.
PLAIN = [
    "Alpha",
    "Bravo",
    "Charlie",
    "Delta",
    "Echo",
    "Foxtrot",
    "Golf",
    "Hotel",
    "India",
    "Juliett",
    "Kilo",
    "Lima",
    "Mike",
    "November",
    "Oscar",
    "Papa",
    "Quebec",
    "Romeo",
    "Sierra",
    "Tango",
    "Uniform",
    "Victor",
    "Whiskey",
    "Xray",
    "Yankee",
    "Zulu",
    "Anvil",
    "Basalt",
    "Cinder",
    "Drift",
    "Ember",
    "Flint",
    "Gale",
    "Harrow",
    "Ingot",
    "Jetty",
    "Kettle",
    "Lumen",
    "Marrow",
    "Nettle",
]


def _ladder(n: int, subject: str) -> _Slate:
    """`n` meetings with probabilities spread across the useful band."""
    picks = []
    for i in range(n):
        p = 0.52 + 0.46 * (i / max(n - 1, 1))
        # Alternate which side is the favourite, so both cap placements and
        # both mirrorings appear on every card.
        picks.append(
            _Pick(PLAIN[2 * i % len(PLAIN)], PLAIN[(2 * i + 1) % len(PLAIN)], p if i % 2 else 1 - p)
        )
    return _Slate(subject, picks)


CASES: dict[str, tuple[str, _Slate]] = {
    "two_rows": (
        "The shortest card worth sending. Single template. Both cap placements "
        "appear: far left for blue, far right for red, flush on the bar's outer "
        "terminal and exactly the bar's height.",
        _ladder(2, "Semi-finals Predictions · Aug 18"),
    ),
    "ten_rows": (
        "The single template at its limit -- 950 x 1414, a 1:1.5 portrait. That "
        "is normal for this card and not a sliver; it is recorded here so nobody "
        "is surprised by it.",
        _ladder(10, "Semi-finals Predictions · Aug 18"),
    ),
    "eleven_rows": (
        "The switch to two columns, and the gap it leaves. Six rows on the left, "
        "five on the right, one row pitch throughout and empty ground under the "
        "right column's last row -- NOT five and six squeezed to the same height.",
        _ladder(11, "Semi-finals Predictions · Aug 19"),
    ),
    "twenty_rows": (
        "The hard cap. A twenty-first meeting is a second slate, never a dropped "
        "row -- ask for 21 and the renderer raises.",
        _ladder(20, "Knockout Stage Predictions · Aug 24"),
    ),
    "clamped": (
        "No row may claim a match cannot be lost. The top two must read >99%, not "
        "100%. The last row has no prediction at all: it keeps both names and "
        "carries no cap, and it sorts to the end.",
        _Slate(
            "Knockout Stage Predictions · Aug 24",
            [
                _Pick("Goliath", "Pebble", 0.9997),
                _Pick("Anvil", "Feather", 0.0003),
                _Pick("Even", "Steven", 0.5),
                _Pick("Unscouted", "Unknown", None),
            ],
        ),
    ),
    "hard_names": (
        "Full-width scripts, pipe padding, and a name long enough to ellipsize. "
        "The 20-character in-game limit bounds Latin names comfortably and does "
        "not bound these: twenty Hangul or Han glyphs run about twice as wide. "
        "Every name on the card shares one size, and each is drawn in the face "
        "its own script needs. ⚠️ ROW 1 RIGHT RENDERS AS EMPTY BOXES, and that "
        "is a real defect this preview exists to show. It is not in this "
        "renderer: the bundled Inter carries no Cyrillic at all, and "
        "`storm_renderer._font_for_text` routes Cyrillic to it anyway on the "
        "strength of a docstring that says Cyrillic and Greek 'stay on Inter'. "
        "See the PR body for what is and is not fixable by routing.",
        _Slate(
            "Semi-finals Predictions · Aug 18",
            [
                _Pick("가나다라마바사아자차", "Bravo", 0.71),
                _Pick("Alpha", "山水火風雷電空時光影", 0.38),
                _Pick("Kilo|||||", "ЅіеггаVісtоr", 0.86),
                _Pick("AVeryLongInventedName", "Тапgоでこぼこ道", 0.63),
            ],
        ),
    ),
}


_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Champion Duel picks card preview</title>
<style>
 body {{ background:#040616; color:#c9c9da; font:15px/1.5 system-ui,sans-serif; margin:0 auto;
        max-width:1960px; padding:32px; }}
 h1 {{ color:#f7f8ff; font-size:22px; }}
 section {{ margin:40px 0; border-top:1px solid #2a2a44; padding-top:20px; }}
 h2 {{ color:#f7f8ff; font-size:17px; margin:0 0 6px; }}
 p {{ margin:0 0 14px; max-width:70ch; }}
 img {{ max-width:100%; height:auto; display:block; }}
 .size {{ color:#8a8aa0; font-size:13px; margin-top:8px; }}
</style>
<h1>Champion Duel &mdash; the day&rsquo;s picks</h1>
<p>Rendered by <code>scripts/preview_champion_duel_picks.py</code>. Mock names only.</p>
{sections}
"""

_SECTION = """<section>
  <h2>{name}</h2>
  <p>{note}</p>
  <img src="{file}" alt="{name}">
  <div class="size">{file} &middot; {w}&times;{h} &middot; {kb:.0f} KB</div>
</section>"""


def main() -> None:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "notes", "preview_picks")
    os.makedirs(outdir, exist_ok=True)

    sections = []
    for name, (note, slate) in CASES.items():
        card = img.render_slate(slate)
        width, height = img.picks_size(len(slate.picks))
        filename = f"{name}.webp"
        with open(os.path.join(outdir, filename), "wb") as fh:
            fh.write(card)
        sections.append(
            _SECTION.format(
                name=html.escape(name),
                note=html.escape(note),
                file=filename,
                w=width,
                h=height,
                kb=len(card) / 1024,
            )
        )
        print(f"  {filename:16s} {width}x{height}  {len(card) / 1024:6.0f} KB")

    index = os.path.join(outdir, "index.html")
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(_PAGE.format(sections="\n".join(sections)))
    print(f"\nOpen: {index}")


if __name__ == "__main__":
    main()

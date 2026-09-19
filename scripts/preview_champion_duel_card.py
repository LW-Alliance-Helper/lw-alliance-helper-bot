"""Render sample prediction cards to disk, without Discord.

Reading the renderer is not how its last defects were found; looking at a card
was. This builds a throwaway database, fabricates a few matchups that stress
the parts most likely to break — a lopsided prediction, a name in Hangul, a
name long enough to need ellipsizing, a side never seen deploying — and writes
each one out beside an `index.html` that shows them all at once.

    python scripts/preview_champion_duel_card.py [outdir]

Defaults to `notes/preview_cards/`, which is gitignored. Open the `index.html`
it prints. Nothing here ships.
"""

from __future__ import annotations

import html
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import champion_duel_db as db  # noqa: E402
import champion_duel_image as img  # noqa: E402
import champion_duel_predict as cdp  # noqa: E402

ACTOR = {"discord_user_id": "0", "discord_name": "preview", "guild_id": "0"}
TYPES = ("Tank", "Missile", "Aircraft")


def _player(name, server, powers, *, orders=(), source="observed"):
    db.import_registrants([{"name": name, "group": "M", "rank": 1, "server": server}])
    rid = db.resolve_registrant(name, server=server)["id"]
    for slot, (squad_type, power) in enumerate(zip(TYPES, powers), start=1):
        db.set_squad(rid, slot, squad_type=squad_type, power=power, actor=ACTOR, source=source)
    for order in orders:
        db.add_order(rid, list(order), actor=ACTOR)
    return db.get_player(name, server=server, include_scouting=True)


CASES = {
    "typical": (
        "The wireframe's own example — hold this one against the design.",
        lambda: (
            _player(
                "RavenShade",
                "738",
                (34_800_000, 31_500_000, 27_200_000),
                orders=[("Missile", "Tank", "Aircraft")],
            ),
            _player(
                "NightOwl",
                "738",
                (33_000_000, 31_000_000, 25_000_000),
                orders=[("Tank", "Missile", "Aircraft")],
            ),
            "Group M · Semifinal",
        ),
    ),
    "lopsided": (
        "Must read >99% / <1%, never 100% / 0%. The divider stops short of the "
        "cap so the bar keeps both rounded ends.",
        lambda: (
            _player(
                "Goliath",
                "101",
                (52_000_000, 49_000_000, 47_000_000),
                orders=[("Tank", "Missile", "Aircraft")],
            ),
            _player("Pebble", "101", (14_000_000, 12_000_000, 11_000_000)),
            "Group A · Quarterfinal",
        ),
    ),
    "hard_names": (
        "Hangul on the left (bundled Noto fallback), a name long enough to "
        "ellipsize on the right — the server suffix survives the trim. No "
        "round metadata, so the header's right box is deliberately empty.",
        lambda: (
            _player(
                "MangowhiskY 망고",
                "1042",
                (28_000_000, 26_500_000, 24_100_000),
                orders=[("Aircraft", "Tank", "Missile")],
            ),
            _player(
                "[VERYLONGALLIANCE] TheLongestNameOnTheServer",
                "902",
                (27_800_000, 26_900_000, 23_500_000),
            ),
            None,
        ),
    ),
    "unseen": (
        "Neither side ever seen deploying: 'assuming strongest first' on both "
        "cards, low confidence in the footer, and a near-even bar.",
        lambda: (
            _player("Alpha", "555", (30_000_000, 29_000_000, 28_000_000), source="estimated"),
            _player("Beta", "555", (29_500_000, 29_000_000, 28_500_000), source="estimated"),
            "Group C",
        ),
    ),
}

_PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Champion Duel card preview</title>
<style>
  body {{ margin: 0; padding: 40px; background: #0a0b14; color: #c9c9da;
         font: 16px/1.5 system-ui, sans-serif; }}
  h1 {{ color: #f7f8ff; font-size: 22px; margin: 0 0 4px; }}
  .meta {{ color: #7d7f96; font-size: 14px; margin-bottom: 36px; }}
  section {{ max-width: 1619px; margin: 0 auto 48px; }}
  h2 {{ color: #ffd35b; font-size: 17px; margin: 0 0 4px; }}
  p {{ margin: 0 0 12px; max-width: 70ch; }}
  img {{ width: 100%; height: auto; display: block; border-radius: 10px; }}
  .size {{ color: #7d7f96; font-size: 13px; margin-top: 8px; }}
</style>
<h1>Champion Duel prediction card</h1>
<div class="meta">Rendered at native 1619&times;971 &middot; scaled to fit your
window here, so open an image directly to judge sharpness.</div>
{sections}
"""

_SECTION = """<section>
  <h2>{name}</h2>
  <p>{note}</p>
  <img src="{file}" alt="{name}">
  <div class="size">{file} &middot; {kb:.0f} KB</div>
</section>"""


def main() -> None:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(here, "notes", "preview_cards")
    os.makedirs(outdir, exist_ok=True)

    # One scratch database for the whole run. Every case uses distinct names,
    # so they can share it -- and the module keeps its connection open, which
    # on Windows means a per-case temp directory cannot be cleaned up.
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "champion_duel.sqlite3")
    db.init_db()

    sections = []
    for name, (note, build) in CASES.items():
        a, b, subtitle = build()
        card = img.render(cdp.predict(a, b), subtitle=subtitle)
        filename = f"{name}.webp"
        with open(os.path.join(outdir, filename), "wb") as fh:
            fh.write(card)
        sections.append(
            _SECTION.format(
                name=html.escape(name), note=html.escape(note), file=filename, kb=len(card) / 1024
            )
        )
        print(f"  {filename}  {len(card) / 1024:.0f} KB")

    index = os.path.join(outdir, "index.html")
    with open(index, "w", encoding="utf-8") as fh:
        fh.write(_PAGE.format(sections="\n".join(sections)))
    print(f"\nOpen: {index}")


if __name__ == "__main__":
    main()

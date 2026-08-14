"""Render sample prediction cards to disk, without Discord.

Reading the renderer is not how its last defects were found; looking at a card
was. This builds a throwaway database, fabricates a few matchups that stress
the parts most likely to break — a lopsided prediction, a name in Hangul, a
name long enough to need ellipsizing, a side never seen deploying — and writes
each one out as a PNG.

    python scripts/preview_champion_duel_card.py [outdir]

Nothing here ships; it exists so a layout revision can be checked against the
debug overlay in `assets/champion_duel/`.
"""

from __future__ import annotations

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
    # The wireframe's own example, so the render can be held against it.
    "typical": lambda: (
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
    # The case that must never print 100% / 0%.
    "lopsided": lambda: (
        _player(
            "Goliath",
            "101",
            (52_000_000, 49_000_000, 47_000_000),
            orders=[("Tank", "Missile", "Aircraft")],
        ),
        _player("Pebble", "101", (14_000_000, 12_000_000, 11_000_000)),
        "Group A · Quarterfinal",
    ),
    # Non-Latin and overlong names, and no subtitle at all.
    "hard_names": lambda: (
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
    # Neither side ever seen deploying: the low-confidence footer and the
    # "assuming strongest first" status on both cards.
    "unseen": lambda: (
        _player("Alpha", "555", (30_000_000, 29_000_000, 28_000_000), source="estimated"),
        _player("Beta", "555", (29_500_000, 29_000_000, 28_500_000), source="estimated"),
        "Group C",
    ),
}


def main() -> None:
    outdir = sys.argv[1] if len(sys.argv) > 1 else "preview_cards"
    os.makedirs(outdir, exist_ok=True)

    # One scratch database for the whole run. Every case uses distinct names,
    # so they can share it -- and the module keeps its connection open, which
    # on Windows means a per-case temp directory cannot be cleaned up.
    db.DB_PATH = os.path.join(tempfile.mkdtemp(), "champion_duel.sqlite3")
    db.init_db()

    for name, build in CASES.items():
        a, b, subtitle = build()
        png = img.render(cdp.predict(a, b), subtitle=subtitle)
        path = os.path.join(outdir, f"{name}.png")
        with open(path, "wb") as fh:
            fh.write(png)
        print(f"{path}  {len(png) / 1024:.0f} KB")


if __name__ == "__main__":
    main()

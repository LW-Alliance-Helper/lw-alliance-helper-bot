"""Build the annotated screens for the VS "Start a new league" guide (#655).

Starting a league asks for every alliance's tag and warzone, and the warzone
is three taps away from the League screen. The guide shows those screens with
numbered markers, the same way the Champion Duel capture guide does, and
shares its drawing code (`make_capture_guide.annotate`) rather than copying it.

**Real game screens, not redacted** (Kevin, 29 Sep): the guide points at the
very things a redaction would hide, and screens like these are public already.
The sources are not committed, only the annotated output the bot ships.

Measured against four 626x1365 captures. Re-measure for any other source.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from make_capture_guide import CTX, MARK, annotate, check_contrast  # noqa: E402

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(HERE, "assets", "alliance_duel")

# Numbered down the page, as a reader works. Fractions of the original capture.
LEAGUE_BOXES = [
    ((0.03, 0.130, 0.78, 0.168), MARK),  # the season, "Alliance Duel League S37"
    ((0.04, 0.299, 0.34, 0.322), MARK),  # tier and group, "Diamond Tier 12 - 4"
    ((0.04, 0.401, 0.55, 0.917), MARK),  # every tag, in ranking order
]
ALLIANCE_BOXES = [
    ((0.35, 0.262, 0.81, 0.285), MARK),  # the tag
    ((0.35, 0.318, 0.91, 0.404), CTX),  # power, gift level, members: optional
    ((0.20, 0.643, 0.37, 0.709), MARK),  # Members
]
MEMBERS_BOXES = [
    ((0.30, 0.106, 0.68, 0.232), MARK),  # any member; the leader is the easy one
]
PROFILE_BOXES = [
    ((0.47, 0.346, 0.97, 0.391), MARK),  # the warzone, "#723"
]

SOURCES = (
    ("1-league-screen.png", "vs_guide_league.png", LEAGUE_BOXES),
    ("2-alliance-page.png", "vs_guide_alliance.png", ALLIANCE_BOXES),
    ("3-member-list.png", "vs_guide_members.png", MEMBERS_BOXES),
    ("4-player-profile.png", "vs_guide_profile.png", PROFILE_BOXES),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True, help="folder holding the four captures")
    parser.add_argument("--out", default=OUT_DIR, help="where the annotated images go")
    args = parser.parse_args()

    print("contrast (WCAG 2.2 AA needs 3:1 for graphical objects):")
    check_contrast()
    for source, name, boxes in SOURCES:
        path = annotate(os.path.join(args.src, source), name, boxes, out_dir=args.out)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

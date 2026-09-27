"""Invented values for placeholders and worked examples (#654).

Copy that shows an example alliance, warzone or player takes it from here, so
a new surface has nothing to invent and nothing real to reach for. Examples
used to borrow other alliances' tags, warzone numbers and player names from
real leagues and groupings, and every alliance using the bot saw them.

The owner's own alliance and warzone may appear; the owner consents to that.
Everything else is invented. `scripts/quality/invented_examples.py` finds a
user-visible example that doesn't draw from this set, and
`tests/unit/test_invented_examples.py` runs it.
"""

from __future__ import annotations

#: The owner's own alliance and warzone, the only real values allowed.
OWN_TAG = "OGV"
OWN_WARZONE = 738

#: Other alliances. Obviously placeholders, the way the sign-off pages write them.
TAG = "ABC"
TAGS = ("ABC", "XYZ", "DEF")

#: Other warzones. Every number from 1 to 2308 is a real warzone, so the
#: invented ones are patterned, and a list of them can't be mistaken for a
#: real grouping.
WARZONE = 999
WARZONES = (999, 998, 997)
#: The bare placeholder in a box that asks for the alliance's own warzone:
#: a digit run, so it reads as "type your number here", not as an example.
WARZONE_PLACEHOLDER = 1234
WARZONE_LIST = (101, 102, 103)

#: Players. Already invented before this set existed.
PLAYERS = ("Kestrel", "Wren")

#: A Discord ID in the shape Discord uses, belonging to nobody. gitleaks reads
#: any 18-digit number beside the word Discord as a client ID; this one is the
#: digits 1 to 9 twice.
DISCORD_ID = "123456789012345678"  # gitleaks:allow

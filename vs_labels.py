"""How a VS League's label is written down, whoever typed it (#658).

The game names a league "Alliance Duel League S37" and "Diamond Tier 12-1".
Officers type the season and group by hand, into the new-league form or
straight into their Sheet, so the same league arrived as `S37`, `s37`, `37`
and `Season 37`, and as `12-1`, `12 - 1` and `12 – 1`. Each spelling read as
a different league: two servers in one bracket never saw each other's data in
the shared store, and a server's own rows split in two after a correction.

So every label is standardized on the way in, to the form the game shows:

- **Season:** `S` and the number. Anything that is not a number with an
  optional `S` or `Season` in front is kept as typed rather than guessed at.
- **Group:** no spaces around the dash, and any dash-like character is a
  hyphen.
- **Tier:** one of the game's tier names, capitalized as the game writes it,
  when it matches one; otherwise as typed.

A leaf module with no imports of its own, because `config` needs it to bring
the stored score prompts and event keys forward, and `config` cannot import
the feature module.
"""

from __future__ import annotations

import re

#: The game's tiers, as the League screen writes them. `alliance_duel`
#: orders them; this only needs the spelling.
TIERS: tuple[str, ...] = ("Silver", "Gold", "Diamond")

_SEASON = re.compile(r"^(?:s(?:eason)?\s*)?0*(\d+)$", re.IGNORECASE)
_DASH = re.compile(r"\s*[-‐‑‒–—−]\s*")


def _squash(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def standard_season(text) -> str:
    """`S37`, from `S37`, `s37`, `37`, `S 37`, `Season 37` or `S037`."""
    squashed = _squash(text)
    match = _SEASON.match(squashed)
    return f"S{match.group(1)}" if match else squashed


def standard_tier(text) -> str:
    """`Diamond`, from any capitalization of it."""
    squashed = _squash(text)
    for tier in TIERS:
        if squashed.casefold() == tier.casefold():
            return tier
    return squashed


def standard_group(text) -> str:
    """`12-1`, from `12 - 1`, `12 – 1` or `12-1`."""
    return _DASH.sub("-", _squash(text))


def standard_key(key: str) -> str:
    """A stored `season|tier|group[|...]` event key, standardized in place.

    `config.vs_league_key` builds these and `alliance_duel_events` appends the
    week and day, so only the first three parts are a label.
    """
    parts = key.split("|")
    if len(parts) < 3:
        return key
    parts[0] = standard_season(parts[0])
    parts[1] = standard_tier(parts[1])
    parts[2] = standard_group(parts[2])
    return "|".join(parts)

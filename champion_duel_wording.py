"""Every user-facing sentence a Champion Duel prediction can produce.

One module because a prediction is shown on three surfaces — the rendered card,
the embed it falls back to, and the message caption posted beside both — and a
reader can see two of them at once. When the wording lived on each surface the
three drifted: the card said "3/3 seen", the embed said "3/3 observed", and the
caption rounded a 0.9999 to "100%" directly above a card that said ">99%",
which is the one claim the card exists to refuse.

Copy here is approved by Kevin rather than chosen by whoever touched the file
last. Change the strings in this module and every surface follows; add a string
here rather than writing one at a call site.

**The vocabulary is the game's, not the database's.** "3/3 seen · their order in
1 sighting" describes rows and source flags — how the data happens to be kept.
A player sizing up an opponent wants to know whether the line-up on screen is
what that opponent usually does, and how much of the number is real.
"""

from __future__ import annotations

from champion_duel_predict import SLOTS

CONFIDENCE_LABEL = "Outcome Prediction Confidence"

#: What the prediction was built from, chosen from what a card actually has
#: rather than from its confidence level. The level is decided on both players'
#: counts added together, so "Medium" alone cannot say whether one player is
#: well known and the other is guesswork, or whether both are half known.
#:
#: "Recorded" means the bot holds a real figure for that squad's power, whether
#: it came from the sighting corpus or from someone typing it into the hub.
#: What it is set against is a power derived from the player's Total Hero
#: Power, which is the only case where the number is inferred rather than known.
EVIDENCE_COPY = {
    "both": "built from recorded squad power for both players",
    "some": "squad power partly recorded, partly estimated from Total Hero Power",
    "neither": "no squad power recorded, estimated from Total Hero Power",
}


def probability(prob: float) -> str:
    """A probability as text, refusing to round certainty into existence.

    `f"{0.9999:.0%}"` is "100%", which claims the match cannot be lost. The
    engine is decisive — a 35% power edge puts it past 0.999 — so this is the
    common case for a lopsided pairing, not an edge case. Upsets happen, and a
    prediction that said 100% before one is a prediction nobody trusts
    afterwards.

    Every surface uses this. The caption and the embed used to format their own
    and both said "100%", which put the disclaimer on the card and the
    overclaim in the text beside it.
    """
    if prob >= 0.995:
        return ">99%"
    if prob <= 0.005:
        return "<1%"
    return f"{prob:.0%}"


def lineup_summary(side) -> str:
    """Where the line-up shown for one player came from.

    Whether the reader is looking at an order this player was actually seen
    using or at the default, which is the difference between a line-up that is
    evidence and one that is an assumption.
    """
    if not side.likely_order()[1]:
        return "Lineup not recorded — assuming strongest first"
    plural = "" if side.sightings == 1 else "s"
    return f"Typical lineup in {side.sightings} observed battle{plural}"


def evidence(a, b) -> str:
    """Which `EVIDENCE_COPY` line describes this pair.

    Only three cases, because "one player recorded, the other estimated" and
    "both partly recorded" are the same thing to a reader — some of this is
    real and some of it is inferred — and splitting them bought precision
    nobody asked for.
    """
    recorded = [side.recorded_squads for side in (a, b)]
    if all(n == len(SLOTS) for n in recorded):
        return "both"
    if all(n == 0 for n in recorded):
        return "neither"
    return "some"


def confidence_line(result) -> str:
    """The footer: how much the number is worth, and what it was built on.

    Named as a prediction confidence rather than a bare "Confidence:", which on
    a card carrying two probabilities could be read as confidence in one of the
    players.
    """
    level = result.confidence().capitalize()
    return f"{CONFIDENCE_LABEL}: {level} — {EVIDENCE_COPY[evidence(result.a, result.b)]}"

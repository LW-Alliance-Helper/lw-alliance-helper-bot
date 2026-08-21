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


# ── The intel surface ────────────────────────────────────────────────────────
#
# ⚠️ DRAFT — NOT SIGNED OFF. Every string below this line is written to the
# standing copy rules but has not been through Kevin. Variants considered are
# enumerated on the intel preview page rather than left as a comment here.
#
# THIS BLOCK IS EMBED COPY AND THE UX.md RULES REACH IT. Nothing below here is
# rendered into the shared VS card image, which is the part of this module that
# is workshopped and exempt. Everything above the marker feeds
# `champion_duel_image.py`; everything below it only ever reaches a Discord
# embed, which is the interface. So the no-em-dash rule applies here and does
# not apply up there, and the two halves of this file legitimately look
# different. Do not "fix" the top half to match. Kevin confirmed the boundary
# 2026-08-20.
#
# THE FOUR RULES THAT LAND HARDEST ON THIS SURFACE
#
# 1. Never imply the model picks winners better than a power sort. It does not:
#    sorting by total hero power calls 87.9% of meetings against the
#    simulation's 84.4%. Everything this feature sells is calibration and
#    conditional structure ("here is what changes if you set this instead of
#    that"), never "here is who wins".
# 2. Never round a probability into a certainty. `probability()` owns that, and
#    `rate()` makes the same refusal about shares of a record.
# 3. ONE LINE-UP, ONE WORD. The thing a player sets is a **line-up**, hyphenated,
#    and it is the only word for it on this surface. Not "order", not
#    "deployment", not "arrangement" except where the copy is literally counting
#    the arrangements they could be fielding. The stored table is still
#    `order_history` and the button is still "Record an order"; those are
#    internal and a label, and issue #512 tracks the rest.
# 4. VOICE: "I" JUDGES, "WE" RECORDS. Kevin's rule, 2026-08-20. The bot says
#    "I" when it is reasoning, refusing or recommending, and "we" when it means
#    the shared record the alliance has built. "I can't recommend a line-up" and
#    "the 3 meetings we watched" are both correct and they are correct for
#    different reasons.
#
# AND ONE THAT IS NEW HERE. The recommendation is advice a member will act on
# inside a game they can lose, so where the evidence is thin the copy says so
# in the same breath as the advice, not in a footnote under it. `none` is a
# sentence, not an empty state, and "I can't tell you" is an answer.

#: How much a line-up is worth at this power gap. The least intuitive thing on
#: the surface: a reader assumes more scouting means a better read, and the gap
#: decides it instead, so it leads rather than qualifies.
#:
#: THE KEYS ARE ABOUT LEVERAGE, NOT MAGNITUDE, AND THAT IS DELIBERATE. They were
#: `high` / `some` / `little`, which put `high` in this vocabulary and in the
#: prediction card's confidence, and `some` in this one and in `EVIDENCE_COPY`.
#: Nothing broke, because none of these four vocabularies prints its grade
#: except confidence, but a word meaning two things is a word that eventually
#: reaches a sentence meaning the wrong one. `test_no_two_grade_vocabularies_
#: share_a_word` is what actually holds the line; the rename is what makes the
#: names read true. Kevin's call, 2026-08-20.
WORTH_COPY = {
    "decides": (
        "**At this gap the line-up is the match.** Power is close enough that "
        "what the two of you set decides it."
    ),
    "swings": ("**At this gap the line-up still swings it**, but power is starting to take over."),
    "settled": (
        "**At this gap power decides and the read is worth very little.** "
        "Across the recorded rounds, no counter has overturned a gap this "
        "size in 39 attempts."
    ),
}

#: The verdict on their habit, and nothing else.
#:
#: IT USED TO RESTATE THE MEASUREMENT AND NOW IT DOES NOT. "They repeat one
#: line-up and hold it" sat directly under a sentence saying they had used it
#: in all 6 recorded line-ups and never been seen changing it, so the field
#: made the same point twice in weaker words the second time. Kevin's rewrite
#: of the first embed is what exposed it. What is left is the only part that
#: was ever a judgement rather than a count: how much to trust it.
#:
#: This is also why there is no longer a separate `lean_unmeasured`. The two
#: ways to reach a `lean` read are different findings, and the difference now
#: lives in the measured sentence above ("they changed it inside 2 of 3
#: meetings" against "whether they change it is unknown"), where it is a
#: number rather than an adjective.
READ_COPY = {
    "strong": "This is the most reliable read here.",
    "lean": "Treat this as a lean, not a plan.",
    "none": (
        "No line-up they repeat often enough to counter. Field your own best "
        "line-up rather than guessing at theirs."
    ),
}

#: Kept apart from `READ_COPY["none"]` because they are different answers.
#: "They move around too much to counter" is a finding about the player;
#: "nobody has watched them" is a finding about us, and it is the one with
#: something the reader can do about it.
NOTHING_SEEN = (
    "We have no line-up recorded for this player yet, so there is nothing to "
    "read. Anyone who has faced them can add one from their card."
)

#: What the answer is built on, said once at the bottom in the reader's words
#: rather than as a glyph key. Mirrors `EVIDENCE_COPY` on the prediction card.
#:
#: "SQUAD TYPES", NEVER "LINE-UP", AND THIS IS THE ONE PLACE THAT MATTERS. Two
#: different things are unknown about an opponent and this footer speaks to
#: only one of them: which of their squads is the Tank is separate from which
#: line-up they set. A footer saying we have seen their line-up above a section
#: saying we have none recorded is the surface contradicting itself, which is
#: exactly what "one line-up, one word" would cause here if it were applied
#: without thinking.
INTEL_BASIS = {
    "seen": "Their squad types are what someone saw in game.",
    "partly": (
        "Some of their squad types are recorded and the rest are not, so every "
        "arrangement they could be was checked."
    ),
    "unknown": (
        "We have not seen which of this player's squads is which type, so "
        "every arrangement they could be was checked."
    ),
}

#: THE REFUSAL, AND THE POINT OF THE WHOLE NO-DATA REWORK.
#:
#: One opener for both reasons, under the heading the surface always uses.
#: Kevin's call, 2026-08-20: keeping the heading stable is what builds
#: cohesion, so the refusal goes in the body rather than changing the label
#: above it. Every state answers the same question in the same place, and
#: sometimes the answer is that there is no answer.
#:
#: First person, because this is the bot judging rather than the record
#: reporting. "Matchup" rather than "pairing": `alliance_duel` already ships
#: "matchup" in user copy and "pairing" appears only in docstrings.
CANNOT_RECOMMEND = "**I can't recommend a specific line-up for this matchup.**"

#: Why, where the six come out level. Carries the measured spread for the same
#: reason `order_barely_matters` does: "I cannot tell you" is a claim the reader
#: is entitled to check.
CANNOT_RECOMMEND_FLAT = (
    "Across all six line-ups you could set, the outcome moves by {measured}, "
    "so the one at the top would not be a recommendation. It would be a coin "
    "flip with a number on it."
)

#: And what is missing, named off the record rather than in general terms, so
#: the reader knows which press fixes it. Only their squad types: the other
#: thing that can be missing is a line-up, and `NOTHING_SEEN` already says that
#: and already names the press, in the section immediately above this one.
CANNOT_RECOMMEND_WHY = (
    "We have not recorded which of their squads is the Tank, which is the "
    "Missile and which is the Aircraft, so every arrangement they could field "
    "was counted and your six line-ups came out level."
)

#: The other reason, and the only one the reader can fix about themselves.
NEEDS_YOUR_SQUADS = (
    "I don't know which of your squads is which type, so every line-up you "
    "could set looks the same from here. Record your own squads and this "
    "becomes a recommendation. {path}"
)

#: The ask, and the honest version of the sales pitch. Arithmetic rather than a
#: promise: recording types cuts what they could be fielding from 36
#: arrangements to 12 to 6, and that is certain, where "this becomes a
#: recommendation" is not.
WHAT_WOULD_HELP = (
    "Anyone who has seen their line-up screen can record it. One squad type "
    "cuts what they could be fielding from 36 arrangements to 12, and all "
    "three cuts it to 6. {path}"
)

#: Kevin, on review: recording squads is worth nothing in this matchup and is
#: still worth collecting. The old surface suppressed the ask entirely here,
#: which optimised for the answer on screen and threw away the contribution.
#:
#: Deliberately NOT `NEEDS_YOUR_SQUADS`. "Record your squads and this becomes a
#: recommendation" is false at a 45% power gap, and a prompt that overpromises
#: once is a prompt nobody presses twice. Two different asks, two sentences.
SQUADS_WORTH_RECORDING_ANYWAY = (
    "**Recording squads will not change this answer.** It changes the next "
    "ones: their odds of advancing, their bracket odds, and every prediction "
    "about them in the next Champion Duel. {path}"
)

#: The honest framing of the envelope, and the sentence that keeps it from
#: being read as a rival probability. It is a range, not an estimate.
ENVELOPE_NOTE = (
    "That range is what the two line-ups are worth, not a second prediction. "
    "It weighs every arrangement equally and real players do not."
)

#: The prototype's third note, kept because it is true and because it is the
#: one line on the surface that costs us something to say.
THEY_READ_YOU = (
    "They can read you the same way. Anything you keep repeating is this visible from their side."
)


def points(spread: float) -> str:
    """A spread in points, floored at "under a point" rather than rounded to 0.

    A rounded zero says the choice provably cannot matter, which is a stronger
    claim than the measurement supports and the same overreach `probability()`
    exists to refuse at the other end of the scale.
    """
    scaled = spread * 100
    return "under a point" if scaled < 1 else f"about {scaled:.0f} points"


def rate(value: float) -> str | None:
    """A share of a record as a percentage, or None where it reads as always.

    `probability()` refuses to round a probability into a certainty. This is
    the same refusal for the other kind of number on this surface: a share of
    what has been recorded. "They play this 100% of the time" is arithmetically
    true off six sightings and reads as a claim about the player rather than
    about the record, and it is the one figure here somebody would act on.

    `None` means "say it in words instead", not "say nothing" (see
    `habit_line`). Tested against the rendered string rather than the value,
    because 199 of 200 formats as 100% and is the same overclaim.
    """
    text = f"{value:.0%}"
    return None if text in ("0%", "100%") else text


def worth_line(grade: str | None) -> str:
    """How much a line-up is worth here, or nothing where there is no gap."""
    return WORTH_COPY.get(grade or "", "")


def read_line(grade: str) -> str:
    return READ_COPY[grade]


def order_barely_matters(spread: float) -> str:
    """The whole of the recommendation where power is what decides it.

    Carries the measured number rather than only the verdict, because "it does
    not matter" is a claim a reader is entitled to check, and because the same
    sentence at three points and at thirty would be two different pieces of
    advice wearing one set of words.
    """
    return (
        f"**Set whatever you normally would.** Across every line-up either of "
        f"you could put out, the outcome moves by {points(spread)}. The power "
        f"gap decides this one, not the line-up."
    )


def _recorded_meetings(habit) -> str:
    """The meetings, where every one of them could have shown a change.

    Only reached when `meetings` and `meetings_multi` agree, which is the
    common case and the one Kevin wrote: "our 3 recorded meetings" is then
    exact, because every meeting on file has more than one line-up recorded
    from it. Where they disagree the copy takes a different shape entirely.
    See `habit_line`.
    """
    n = habit.meetings_multi
    return f"our {'one' if n == 1 else n} recorded meeting{'' if n == 1 else 's'}"


def habit_line(habit) -> str:
    """What they are observed to set, as rates rather than as counts.

    Kevin, on review: "this person plays XYZ order XX% of the time and they
    move squads around XX% of the time", which is the language of the OGV
    scouting page ("Usually deploys", "Changes it up") and of the three fights
    his own team members were given. The counts stay beside the rate, because a
    rate off six sightings and a rate off sixty are not the same claim and the
    percentage alone hides which one this is.

    THE LINE-UP ITSELF IS NOT IN HERE. It goes on its own line above, unbolded,
    against the bolded recommendation below it: their observed thing is plain
    and the reader's action is emphasised. Kevin's layout, and it survives being
    scanned in a way the run-on sentence did not.

    NO FIGURE HERE EVER RENDERS AS 0% OR 100%, which is `rate()`'s job and the
    same refusal `probability()` makes about the other kind of number. Every one
    of those cases is said in words instead, because they are the cases somebody
    acts on: "all 6 line-ups we have recorded" is a statement about the record,
    and "100% of the time" is heard as a statement about the player.
    """
    # A fragment rather than a sentence, and the same three cases the Find a
    # player card already phrases. "They have used it in all 6 line-ups we
    # have recorded" was circular: the thing being counted and the thing being
    # counted in are both line-ups.
    share = rate(habit.share)
    if habit.total == 1:
        used = "The only line-up on record for them."
    elif share is None:
        # "All 2 line-ups" is what a template does to a count of two.
        used = (
            "Both line-ups on record for them."
            if habit.total == 2
            else f"All {habit.total} line-ups on record for them."
        )
    else:
        used = (
            f"{habit.seen} of the {habit.total} line-ups on record for them, {share} of the time."
        )

    # `is not None`, never a truth test. A measured zero is the strongest thing
    # this figure can say, and a falsy check drops exactly that case.
    change_rate = habit.change_rate
    if change_rate is None:
        changed = (
            "Whether they change it inside a meeting is unknown: we have never "
            "recorded two line-ups from one meeting."
        )
    elif habit.meetings == habit.meetings_multi:
        # Every meeting on file was recorded more than once, so the count is
        # the whole record and needs no qualifier. Kevin's wording.
        meetings = _recorded_meetings(habit)
        if not habit.meetings_changed:
            changed = f"They have never been seen changing it in {meetings}."
        elif habit.meetings_multi == 1:
            # "every one of the one meeting" is what a template does to a count
            # of one, and there is no rate off a single meeting anyway.
            changed = f"They changed it inside {meetings}."
        elif rate(change_rate) is None:
            changed = f"They changed it inside every one of {meetings}."
        else:
            changed = (
                f"They changed it inside {habit.meetings_changed} of {meetings}, "
                f"{rate(change_rate)} of the time."
            )
    else:
        # Some meetings have only one line-up on file and could never have
        # shown a change, so the rate is computed over fewer meetings than we
        # have. SAY THE LIMIT, NOT THE MECHANISM. Kevin's call, 2026-08-20: an
        # earlier draft said "in the 3 meetings we watched more than once",
        # which is accurate and asks the reader to work out why a meeting seen
        # once is useless. "We can only tell for 3 of their 6" says the thing
        # they actually need, which is that the number is thinner than it looks.
        seen = "one" if habit.meetings_multi == 1 else habit.meetings_multi
        tail = f", though we can only tell for {seen} of their {habit.meetings} meetings."
        if not habit.meetings_changed:
            changed = "They have never been seen changing it inside a meeting" + tail
        elif rate(change_rate) is None:
            # "every time" rather than 100%, which `rate` refuses.
            changed = "They changed it inside a meeting every time" + tail
        else:
            changed = f"They changed it inside a meeting {rate(change_rate)} of the time" + tail
    return f"{used} {changed}"


def intel_basis(result) -> str:
    """Which `INTEL_BASIS` line describes what this answer was built on."""
    if result.their_types_known:
        return INTEL_BASIS["seen"]
    if result.their_types_recorded:
        return INTEL_BASIS["partly"]
    return INTEL_BASIS["unknown"]

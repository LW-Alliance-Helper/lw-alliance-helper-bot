"""The day's picks: a slate of meetings, each one predicted.

WHAT THIS IS FOR. Everything else in Champion Duel answers a question about a
*player*. This answers the one people asked for unprompted, daily, and which
until now was assembled by hand and posted as an image: **who should I pick
today.** The game runs a betting market on individual meetings, the multipliers
are on the reader's own screen, and what they lack is a read on who actually
wins each one.

WHY A SLATE IS ITS OWN TYPE. `champion_duel_predict` shapes ONE matchup, and a
slate is not a list of those: the sides are shared between rows (a player meets
two people on a two-meeting day), the whole card is scored against one field in
one read, and a row we cannot predict has to stay on the card rather than being
dropped from it. So the join belongs here, above the data layer and below
whichever surface asks, exactly as `champion_duel_predict` sits between them for
a single matchup.

A SLATE IS NOT A GROUP'S CARD. It is a set of meetings somebody chose, for a
day. The group was never a property of the thing -- the field of 128 mixes
warzones, so a semifinal group is not one warzone's eight, and at the knockouts
there is no lettered group at all. What identifies a slate is the guild that
built it, the day it is for, and which of that day's cards it is.

**A row we cannot predict is not an error, it is the most useful row on the
card.** It names two players nobody has scouted, on a surface the alliance is
about to read. Dropping it would hide the gap; keeping it turns the card into
the contribution prompt the information-architecture work asks every surface to
carry at the point of failure.

**THE ODDS ARE COMPUTED, NOT LOOKED UP -- and that is not the mistake it looks
like.** `champion_duel_store` holds precomputed odds, and the brief for this
work said to read them. It holds the wrong ones: a stored row is a
`simulate_group` result, which answers *who gets out of this group* over 800
simulated groups and costs a minute of GIL-holding Python. A meeting is
`predict_matchup`, which is analytic -- measured at 0.17 ms a call with observed
orders, so a full twenty-row card is under 4 ms of engine time. Reading the
store here would be slower, and would answer a different question in the same
units.

**No multiplier field.** The game buckets its meetings x4/x3/x2/x1 and the
person building a card ships the profitable ones. Those numbers are already in
front of the reader and a second copy of a game concept goes stale, so all we
hold is which meetings somebody picked.

COPY. Every string this module renders is a placeholder awaiting Kevin's
sign-off except `PICKS_TITLE`, which is approved. They live here rather than in
`champion_duel_wording.py` only because that file is contested while the
information-architecture sessions run; **they belong there** once they are
signed off, under the same one-module rule -- a slate has the same three
surfaces a single prediction does (the card, the text beside it, and the bench
somebody assembles it on) and the same way of drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import champion_duel_db as db
import champion_duel_predict as predict_lib

# ── Copy ──────────────────────────────────────────────────────────────────────
#
# ✅ SIGNED OFF by Kevin on 2026-08-29, off the picks sign-off page --
# <https://claude.ai/code/artifact/6cd70358-2103-4708-83f5-9684ddd4f098> --
# except `CARD_FOOTER`, `CARD_NUMBER` and `TEXT_ALT`, which are marked where
# they sit. **The reasoning below is kept and the variants are spent.**
#
# **NOTHING HERE IS EXEMPT FROM `notes/UX.md`, and this module used to say it
# was.** The exemption on record is written as *the card, not the module*, and
# the card it covers is the **VS** card, whose copy Kevin actually workshopped.
# No string below was. So every one of them takes the same rules as any other
# surface: US English, no em dashes, `stage` rather than `round`, and the "I
# acts, we holds" voice.

#: Approved 2026-08-24. The one string here that is settled.
PICKS_TITLE = "🔮 Today's picks"

#: The card's own title, taken from the VS card verbatim so the two read as one
#: product rather than as two tools that both happen to draw on black.
CARD_TITLE = "CHAMPION DUEL"

# `CARD_CONFIDENCE_HEADING` and `CARD_NO_PREDICTION` were here and are gone.
# The redesigned card has no confidence column and writes nothing across a row
# it cannot predict -- the absent PICK cap is what says so -- so both had been
# without a renderer since that landed, and a string nothing draws is a string
# nobody can sign off. Deleted rather than left for the copy page to carry.

#: The whole card's footer. It exists to hold one line between us and the
#: game's own betting market, which is a different thing sitting on the same
#: screen: the multipliers there are the game's, these numbers are ours.
# ⚠️ STILL OPEN. Kevin answered this one with a question or a change rather
# than a string, and it is being walked through rather than guessed at.
#
# Kevin, 29 Aug: *"I thought I had changed this before. We should say 'This
# is purely our predictions and do not represent the in-game odds of match
# outcomes.'"* **Two things to settle before it goes on the card**: the
# sentence does not agree with itself (*This is ... and do not represent*).
#
# **The length is fine and was measured rather than worried about**: at 87
# characters it fits both templates without ellipsizing, shrinking the footer
# from 17px to 15px on the single-column card and not at all on the wide one.
# The grammar-corrected wording is 89 and costs one more pixel. **Held on the
# wording only.**
CARD_FOOTER = "Our prediction for each meeting. Not the game's odds."

#: A side with no name at all, which is a meeting naming a registrant that no
#: longer exists. `set_slate` refuses to write one, so it is only reachable
#: from a preview or from a player deleted after the card was saved -- but it
#: is set on the label rather than at each surface, so the card and the text
#: beside it cannot say two different things about the same gap. The VS card's `_name`
#: already reads this way for the same reason.
CARD_UNKNOWN = "(unknown)"

#: Which of the day's cards this is, appended to the subject on card 2 and up.
#: Never on card 1: a day that did not overflow twenty meetings reads the way
#: it always did, and only a day that split has anything to disambiguate.
# ⚠️ STILL OPEN. Kevin answered this one with a question or a change rather
# than a string, and it is being walked through rather than guessed at.
#
# Kevin, 29 Aug: *"If it has to be 2 cards, we need to tell them each one. So
# it would be '# of #'. It would only show when cards > 1."* **That is not a
# wording change**: a slate knows its own number and not how many the day
# has, and the marker currently appears on card 2 and up rather than on both
# halves of a split. The total has to be read and carried. **Held.**
CARD_NUMBER = "Card {n}"

#: The text half's row lines, one per meeting, in the same order the image
#: draws them.
#:
#: **NO ROW NUMBER, and its removal is the fix for a real defect.** These used
#: to open `{i}.` off `Pick.position`, which is the order the maker entered the
#: meetings in, while the image drew them strongest pick first -- so the text
#: said "4." about a meeting that was not fourth on the card, and session A had
#: taken the numerals off the image, so there was nothing there to disagree
#: with. Both halves now follow the card's order (`card_order`), and with no
#: numeral on either of them there is nothing left to number differently.
TEXT_ROW = "**{a}** {p_a} · **{b}** {p_b} ({confidence})"
TEXT_ROW_UNPREDICTED = "**{a}** and **{b}**: no squads recorded"

#: Said once, and only on a card that has one. A row the tie-break decided
#: still prints `PICK 50%` on the image, because `p_a >= p_b` names a side and
#: suppressing the cap would drop the pick from the one row where naming a side
#: is the whole task (Kevin, 2026-08-28: *"we can do a cap but should likely
#: add a line of text in the embed itself that those are truly a coin flip."*).
#: The honesty lives here beside it, which is the same division the
#: accessibility rule already set: the image carries the pick, the text carries
#: what the image cannot fit.
TEXT_COIN_FLIP = (
    "Where a row says 50%, treat the pick as a coin toss. "
    "The model has nothing to separate those two."
)
#
# Kevin took the variant that speaks to the reader about what to do rather
# than to us about what the model did. **It says nothing about a tie-break**,
# which was deliberate on his part: the reader does not need to know which
# side `p_a >= p_b` landed on to know not to trust the side named.

#: The image's own description, which Discord caps at 1,024 characters.
#:
#: **It points at the rows rather than repeating them**, and that is a
#: deliberate call rather than a shortcut. Twenty rows of decorated names run
#: past 1,024 often enough that repeating them would sometimes truncate --
#: which is the silent cut this whole surface exists to refuse -- and the rows
#: are already carried in full by the text beside the image, on the same
#: message, where a screen reader reaches them without the attachment at all.
#: A description that is always complete beats one that is usually complete.
# ⚠️ STILL OPEN. Kevin answered this one with a question or a change rather
# than a string, and it is being walked through rather than guessed at.
#
# Kevin, 29 Aug: *"Is the entire context of the image in the message with it?
# If it is, then we should just mark the image as decorative so there is no
# alt text and a screenreader skips it."* The answer to his question is yes,
# and it is the guarantee this whole surface is built on. **Held on whether
# Discord can express `decorative` at all** -- an attachment either carries a
# description or it does not, and what a client announces without one is not
# silence.
TEXT_ALT = (
    "Champion Duel picks for {subject}, drawn as an image. "
    "Every meeting on it is written out in the text beside it."
)


# ── The type ──────────────────────────────────────────────────────────────────


@dataclass
class Pick:
    """One meeting on the card, predicted or not.

    `prediction` is None exactly when a side has no usable line-up, and
    `missing` names who. Both are kept rather than collapsed to a flag: the
    surface that renders the gap wants the name to offer a way to fill it.
    """

    #: Where this meeting sits in `pick_meetings`, which is the order the maker
    #: entered it in. **It is not the row's place on the card**, and the old
    #: name for it -- `position` -- read as though it were: the text half
    #: numbered its rows off this while the image drew them strongest pick
    #: first. Nothing renders it now. It is kept because it is the row's
    #: identity in storage, and named so that nobody prints it as a row number
    #: again.
    entry_position: int
    a_name: str
    b_name: str
    a_server: str | None = None
    b_server: str | None = None
    prediction: predict_lib.Prediction | None = None
    missing: tuple[str, ...] = ()
    #: The name as it should be shown, which is the display name plus the
    #: server where and only where two players on this card share a name. See
    #: `_label_sides`.
    a_label: str = ""
    b_label: str = ""

    @property
    def predicted(self) -> bool:
        return self.prediction is not None

    @property
    def p_a(self) -> float | None:
        return None if self.prediction is None else self.prediction.p_a

    @property
    def p_b(self) -> float | None:
        return None if self.prediction is None else self.prediction.p_b

    def confidence(self) -> str | None:
        """`high` / `medium` / `low`, or None where there is no prediction."""
        return None if self.prediction is None else self.prediction.confidence()


@dataclass
class Slate:
    """One guild's card for one day."""

    guild_id: str
    play_on: str
    stage: str = ""
    card_no: int = 1
    picks: list[Pick] = field(default_factory=list)
    updated_at: str | None = None
    updated_by: str | None = None

    def date_label(self) -> str:
        """The day the meetings are played, as a calendar date.

        A calendar date rather than a day number, because that is what the
        game's own Predict screen prints and what the maker is reading while
        they build the card (Kevin, 2026-08-27). A day number needed the
        grouping's calendar, which most groupings do not have, and read as
        `Day 38` on a four-day round whenever a slate outlived its grouping.

        No year: the card is read the day before it is played, and a year on it
        would be the only part nobody needs.
        """
        try:
            played = date.fromisoformat(self.play_on)
        except ValueError:  # pragma: no cover - `play_on` is validated on write
            return self.play_on
        return f"{played.strftime('%b')} {played.day}"

    def subject(self) -> str:
        """What this card is of: the stage, and the day it is for.

        «Semi-finals Predictions · Aug 18», and the group is gone from it --
        no group letter and no day number replace it (Kevin, 2026-08-27:
        *"Just put the round, something like 'Semi-finals Predictions' and
        leave it at that. Simple."*). Recorded as a placeholder pending
        sign-off, not as settled copy; `stage` is the word a member reads
        rather than `round` (#545).

        The date alone where no stage was stamped, which is a guild whose
        grouping has no calendar. That is better than a guess, and it is the
        only half of this line the card can always fill in.

        **Card 2 says so, and card 1 does not.** A day that overflowed twenty
        meetings produces two cards, and two cards headed identically are two
        cards a reader cannot tell apart -- in a channel, in a search, or in
        the sentence somebody writes under one of them. The marker is what
        makes "card 2" sayable at all. Off on card 1, because a day that did
        not overflow should read exactly as it did before.
        """
        stage_label = db.STAGE_LABELS.get(self.stage, self.stage)
        card = CARD_NUMBER.format(n=self.card_no) if self.card_no > 1 else ""
        parts = [p for p in (stage_label, self.date_label(), card) if p]
        return " · ".join(parts)


# ── Building one ──────────────────────────────────────────────────────────────


def _sides(registrant_ids) -> tuple[dict, dict]:
    """Everyone on the card shaped for the engine, in one read.

    Returns `{registrant_id: SideInput}` for the ones we can predict, and the
    registrant rows for everyone, so a row can still name a player we could not
    build a side for.

    Built per player rather than per meeting. A player meets two people on a
    two-meeting day, and building a side twice would read the same squads twice
    and, worse, produce two objects a caller could drift apart.

    Read by registrant rather than by group. A slate draws from a field of 128
    that mixes warzones, and once the knockouts start there is no lettered
    group to read at all.
    """
    sides: dict[int, predict_lib.SideInput] = {}
    names: dict[int, dict] = {}
    for player in db.get_scouting(registrant_ids):
        rid = player["registrant_id"]
        names[rid] = player
        try:
            sides[rid] = predict_lib.build_side(player)
        except predict_lib.NotEnoughData:
            continue
    return sides, names


def _label_sides(picks: list[Pick]) -> None:
    """Fill in `a_label` / `b_label`, adding a server only where one is needed.

    Names are unique per server, not across them, and a card draws from
    several. Two rows reading `Ravenshade` for two different people is the one
    way this card can be actively misleading, so the server goes on where a
    name is shared -- and stays off everywhere else, because the suffix costs a
    third of the width the name has to fit into and buys nothing when there is
    nobody to confuse them with.

    Compared on the name alone, not on (name, server): the same player on both
    sides of two rows is not an ambiguity, and a name that appears twice for
    the same person would otherwise gain a suffix nobody needs.
    """
    seen: dict[str, set] = {}
    for pick in picks:
        for name, server in ((pick.a_name, pick.a_server), (pick.b_name, pick.b_server)):
            seen.setdefault(name, set()).add(server)
    for pick in picks:
        for side in ("a", "b"):
            name = getattr(pick, f"{side}_name")
            server = getattr(pick, f"{side}_server")
            ambiguous = len(seen.get(name, ())) > 1
            if not name:
                label = CARD_UNKNOWN
            elif ambiguous and server:
                label = f"{name} #{server}"
            else:
                label = name
            setattr(pick, f"{side}_label", label)


def card_order(picks: list[Pick]) -> list[Pick]:
    """The order every surface shows a slate in: strongest pick first.

    **This is the card's own order, and it is now the slate's.** Kevin, 27
    Aug: *"I think let's go with strongest pick first. To me it doesn't really
    matter the order."* A row nobody can predict sorts to the end, because a
    row with no number cannot be ranked among rows that have one and the useful
    end of the card is the top.

    **`champion_duel_image.render_slate` applies this same key to the list it
    is handed.** Sorting an already-sorted list by the same key is a no-op --
    `sorted` is stable -- so the image draws exactly what this returns and the
    two halves of the message cannot disagree about which row is first. That
    duplication is the reason the sort lives here rather than only there:
    whoever next opens the renderer should replace its sort with a call to
    this. Until then the invariant is that the two keys are identical.

    Stable, so meetings we cannot predict keep the order they were entered in
    rather than being shuffled among themselves.
    """
    return sorted(
        picks,
        key=lambda p: (p.predicted, max(p.p_a, p.p_b) if p.predicted else 0.0),
        reverse=True,
    )


def is_coin_flip(pick: Pick) -> bool:
    """Whether this row's pick is a tie-break rather than a read.

    Measured off the string the surfaces actually print rather than off a
    threshold of its own: a row is a coin flip exactly when the percentage on
    it reads `50%`. `champion_duel_wording.probability` rounds to the whole
    percent, so anything from 49.5 to 50.5 lands there, and any other rule
    would make the text disagree with the number beside it about which rows
    `TEXT_COIN_FLIP` is talking about.
    """
    from champion_duel_wording import probability

    if not pick.predicted:
        return False
    return probability(max(pick.p_a, pick.p_b)) == "50%"


def assemble(
    guild_id,
    play_on: str,
    pairs,
    *,
    stage: str = "",
    card_no: int = 1,
    updated_at=None,
    updated_by=None,
) -> Slate:
    """Score a list of `(a_id, b_id)` meetings into a slate.

    Split out of `build` so a surface can show the card somebody is part way
    through choosing without writing it down first. Both paths score the same
    way, which is what stops a preview from disagreeing with what gets saved.

    `champion_duel_engine.constants()` is read once for the whole slate rather
    than once a row. It is the same dictionary every time, and hanging a fresh
    copy off twenty `Prediction`s would be twenty reads of the same pin.
    """
    if not predict_lib.ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")
    from champion_duel_engine import constants

    pairs = [(int(a), int(b)) for a, b in pairs]
    # The same cap `set_slate` enforces, so a preview refuses exactly where the
    # save would. Without it a twenty-one-meeting preview renders a card the
    # person is then told they cannot keep, which is the disagreement between
    # preview and save this function exists to prevent.
    if len(pairs) > db.MAX_PICKS:
        raise ValueError(f"a card carries at most {db.MAX_PICKS} meetings, not {len(pairs)}")
    # And the same card number, for the same reason: a preview of card 5 that
    # renders and is then refused at save is the disagreement the cap above
    # exists to prevent. The bound is read off `db` rather than restated, so
    # the two cannot drift.
    if not 1 <= card_no <= db.MAX_CARDS_PER_DAY:
        raise ValueError(f"card_no must be 1..{db.MAX_CARDS_PER_DAY}, not {card_no}")

    slate = Slate(
        guild_id=str(guild_id or ""),
        play_on=play_on,
        stage=stage or "",
        card_no=card_no,
        updated_at=updated_at,
        updated_by=updated_by,
    )
    sides, players = _sides({rid for pair in pairs for rid in pair})
    engine = constants()

    for position, (a_id, b_id) in enumerate(pairs, start=1):
        a_row, b_row = players.get(a_id), players.get(b_id)
        pick = Pick(
            entry_position=position,
            a_name=(a_row or {}).get("display_name") or "",
            b_name=(b_row or {}).get("display_name") or "",
            a_server=(a_row or {}).get("server"),
            b_server=(b_row or {}).get("server"),
        )
        a, b = sides.get(a_id), sides.get(b_id)
        if a is None or b is None:
            pick.missing = tuple(
                name for side, name in ((a, pick.a_name), (b, pick.b_name)) if side is None and name
            )
        else:
            pick.prediction = predict_lib.Prediction(
                # **One match, not the meeting, and the stage does not change
                # it.** The card used to score semifinal and knockout rows at
                # Bo3 because that is how the game plays them. Measured on 310
                # real results that makes the card worse, not better: a Bo3 in
                # this game amplifies the favourite by 0.4pp while the engine's
                # `series_win_prob` amplifies by 8.4pp, because it assumes
                # independent matches where the real scoreline distribution is
                # bimodal (chi-square 358.3 on 3 df). Brier went 0.1010 ->
                # 0.1052 and log loss 0.3892 -> 0.5599. Source:
                # `champion-duel-simulator`,
                # `semifinal_data/FINDING_matchup_model_stage.md`.
                #
                # So 1 here is the number the results support, not a default
                # nobody set, and it is the same 1 the intel surface passes for
                # its own reason. **Do not make this depend on the stage
                # again.**
                p_a=predict_lib.predict_pair(a, b, best_of=1),
                a=a,
                b=b,
                engine=engine,
            )
        slate.picks.append(pick)
    _label_sides(slate.picks)
    # Sorted here rather than at each surface, so the card, the text beside it
    # and anything else that walks `slate.picks` are one order by construction.
    slate.picks = card_order(slate.picks)
    return slate


def build(guild_id, play_on, *, card_no: int = 1) -> Slate | None:
    """One of a guild's stored cards for one day, scored. None if there is none.

    None rather than an empty slate, because "nobody has built tomorrow's card
    yet" and "the card is empty" are different things to say to a reader and
    only one of them is true.
    """
    stored = db.get_slate(guild_id, play_on, card_no=card_no)
    if stored is None:
        return None
    return assemble(
        stored["guild_id"],
        stored["play_on"],
        [(m["a_id"], m["b_id"]) for m in stored["meetings"]],
        stage=stored.get("stage") or "",
        card_no=stored["card_no"],
        updated_at=stored.get("updated_at"),
        updated_by=stored.get("updated_by"),
    )


def todays(guild_id, *, card_no: int = 1) -> Slate | None:
    """Today's card, on the game's clock rather than the reader's.

    The card is prepared the evening before, so the day it is FOR and the day
    it was built are different days. This asks for the one it is for, and
    `db.server_today` is the same reading of the clock the slate was dated by
    -- a second reading is how two answers to "today" drift apart.
    """
    return build(guild_id, db.server_today().isoformat(), card_no=card_no)


# ── The text half ─────────────────────────────────────────────
#
# **THE ROWS CANNOT OVERFLOW, AND THAT IS STRUCTURAL NOW.** This used to be a
# 2,000-character message clamped by dropping rows off the end and saying so,
# which meant the image could carry a meeting the text did not. Kevin,
# 2026-08-28: *"we cannot have things just on an image that are not also in
# text."*
#
# Two things closed that by construction rather than by rule. A card carries at
# most `db.MAX_PICKS` meetings, which is twenty; and the text half is an embed
# description, which holds 4,096 characters. Twenty rows of two names, two
# percentages and a confidence word run to roughly 1,100-1,500. So there is
# nothing left for a truncation rule to do, and `CAPTION_TRUNCATED` and the
# row-dropping behind it are deleted rather than reworded.
#
# The rows still go in through `champion_duel_hub._add_listing`, because an
# embed FIELD stops at 1,024 even though its description does not.


def text_rows(slate: Slate) -> list[str]:
    """Every meeting on the card, as text, in the order the card draws them.

    The image carries this visually. These lines are what survive a screen
    reader, a failed image load, and Discord's own search, none of which can
    read a WebP -- and on a slate that matters more than it does on a single
    prediction, because there are up to twenty numbers in the picture.

    **Names are escaped.** They are bolded here and drawn plain on the card, so
    a player called `Rav**en` would otherwise appear under two different names
    in one message: Discord eats the asterisks in the text and the card keeps
    them. `discord.utils.escape_markdown` is the same call the hub makes at
    every other site that bolds a player. Imported inside the function because
    this module has no other reason to know about Discord, and a caller that
    only wants a slate should not pay for the library.
    """
    from discord.utils import escape_markdown

    from champion_duel_wording import probability

    lines = []
    for pick in slate.picks:
        a, b = escape_markdown(pick.a_label), escape_markdown(pick.b_label)
        if pick.predicted:
            lines.append(
                TEXT_ROW.format(
                    a=a,
                    b=b,
                    p_a=probability(pick.p_a),
                    p_b=probability(pick.p_b),
                    confidence=pick.confidence().capitalize(),
                )
            )
        else:
            lines.append(TEXT_ROW_UNPREDICTED.format(a=a, b=b))
    return lines


def has_coin_flip(slate: Slate) -> bool:
    """Whether any row on this card is a tie-break, so the line is worth saying.

    Only where there is one. A card with no 50% row that carried the caveat
    anyway would be explaining a thing the reader cannot see, and the surfaces
    in this feature say a caveat where it applies rather than everywhere.
    """
    return any(is_coin_flip(pick) for pick in slate.picks)


#: Discord's cap on an attachment description.
ALT_LIMIT = 1024


def alt_text(slate: Slate) -> str:
    """The image's description, for a reader who cannot see it.

    Points at the rows rather than repeating them -- see `TEXT_ALT`, which
    carries the reasoning. Clamped rather than trusted: the subject is short
    and bounded, but a description Discord refuses would fail the whole send.
    """
    return TEXT_ALT.format(subject=slate.subject())[:ALT_LIMIT]

"""The day's picks: a slate of meetings, each one predicted.

WHAT THIS IS FOR. Everything else in Champion Duel answers a question about a
*player*. This answers the one people asked for unprompted, daily, and which
until now was assembled by hand and posted as an image: **who should I pick
today.** The game runs a betting market on individual meetings, the multipliers
are on the reader's own screen, and what they lack is a read on who actually
wins each one.

WHY A SLATE IS ITS OWN TYPE. `champion_duel_predict` shapes ONE matchup, and a
slate is not a list of those: the sides are shared between rows (a player meets
two people on a two-meeting day), the round decides the series length for every
row at once, and a row we cannot predict has to stay on the card rather than
being dropped from it. So the join belongs here, above the data layer and below
whichever surface asks, exactly as `champion_duel_predict` sits between them for
a single matchup.

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
orders, so a full seventeen-row card is under 3 ms of engine time. Reading the
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
surfaces a single prediction does (the card, the caption beside it, and the
embed it falls back to) and the same way of drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import champion_duel_db as db
import champion_duel_predict as predict_lib

#: The series length each round is played over, which decides every row on a
#: card at once. A qualifier meeting is a single match; semifinal and knockout
#: meetings are a Bo3 (Kevin, 2026-08-15/16).
#:
#: **The two finals are a Bo5 and are not represented here.** A knockout slate
#: cannot tell which of its meetings is the final without the bracket position,
#: so both are scored as the Bo3 the other fourteen knockout meetings are. That
#: understates a favourite's edge on exactly two meetings out of 31, and the
#: alternative -- guessing which pair is the final -- would misstate it on more.
#: Named as a gap rather than left as a rounding.
BEST_OF = {"qualifiers": 1, "semifinals": 3, "knockouts": 3}

#: Discord's message-content ceiling. The caption is clamped to it rather than
#: being allowed to fail the send: a card that arrives with a short caption is
#: worth more than one that does not arrive.
CAPTION_LIMIT = 2000


# ── Copy ──────────────────────────────────────────────────────────────────────
#
# ⚠️ AWAITING SIGN-OFF, everything below except `PICKS_TITLE`.
#
# The card strings and the caption strings are held apart because two different
# rules reach them. Copy rendered INTO the card image is workshopped and exempt
# from `notes/UX.md`; the caption is a Discord message and is not, so it takes
# US English, no em dashes, and the "I acts, we holds" voice.

#: Approved 2026-08-24. The one string here that is settled.
PICKS_TITLE = "🔮 Today's picks"

#: The card's own title, taken from the VS card verbatim so the two read as one
#: product rather than as two tools that both happen to draw on black.
CARD_TITLE = "CHAMPION DUEL"

#: The confidence column's heading, over two lines because the column is as
#: wide as a word. Two lines rather than a bare "CONFIDENCE" for the reason
#: `champion_duel_wording.CONFIDENCE_LABEL` is not bare either: on a card
#: carrying two probabilities a lone "Confidence" reads as confidence in one of
#: the players.
CARD_CONFIDENCE_HEADING = ("PREDICTION", "CONFIDENCE")

#: What a row says where a side has no usable squads. It replaces the two
#: percentages and the bar, and the two names stay.
CARD_NO_PREDICTION = "Squads not recorded"

#: The whole card's footer. It exists to hold one line between us and the
#: game's own betting market, which is a different thing sitting on the same
#: screen: the multipliers there are the game's, these numbers are ours.
CARD_FOOTER = "Our prediction for each meeting. Not the game's odds."

#: The caption's row lines. `{i}` is the row's place on the card, so a reader
#: can say "number four" and be understood.
CAPTION_ROW = "{i}. **{a}** {p_a} · **{b}** {p_b} ({confidence})"
CAPTION_ROW_UNPREDICTED = "{i}. **{a}** and **{b}**: no squads recorded"
CAPTION_TRUNCATED = "Some rows are on the card only."


# ── The type ──────────────────────────────────────────────────────────────────


@dataclass
class Pick:
    """One meeting on the card, predicted or not.

    `prediction` is None exactly when a side has no usable line-up, and
    `missing` names who. Both are kept rather than collapsed to a flag: the
    surface that renders the gap wants the name to offer a way to fill it.
    """

    position: int
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
    """One group's card for one day."""

    group: dict
    play_on: str
    picks: list[Pick] = field(default_factory=list)
    updated_at: str | None = None
    updated_by: str | None = None

    @property
    def stage(self) -> str:
        return self.group.get("stage") or ""

    @property
    def best_of(self) -> int:
        return BEST_OF.get(self.stage, 1)

    def day_number(self) -> int | None:
        """Which day of the round this is, or None where nobody entered dates.

        Derived from the grouping's own calendar rather than from a column,
        because the calendar is what the game shows and a stored day number
        would be a second copy of it that could disagree. None where the
        grouping has no start date, which is every grouping until somebody
        enters one -- and a missing day is left off the card rather than
        guessed at.

        **None outside the round's own window too.** `phase_window` gives both
        ends and both are load-bearing: a semifinal card dated a month past the
        semifinals would otherwise read `Day 38` on a round that is four days
        long, which is a worse thing to print than nothing. That happens
        whenever a slate outlives its grouping, which is the normal state of
        last season's data.
        """
        first, end = db.phase_window(self.group.get("grouping_id"), self.stage)
        if first is None:
            return None
        try:
            played = date.fromisoformat(self.play_on)
        except ValueError:  # pragma: no cover - `play_on` is validated on write
            return None
        if played < first or played >= end:
            return None
        return (played - first).days + 1

    def subject(self) -> str:
        """What this card is of, in the words the rest of the feature uses.

        `Group M · Semi-finals · Day 2`. The knockouts are one field of 32 with
        no letter, so they open on the round name instead -- the same rule the
        hub's own group headings follow.
        """
        label = self.group.get("label")
        stage_label = db.STAGE_LABELS.get(self.stage, self.stage)
        parts = [f"Group {label} · {stage_label}"] if label else [stage_label]
        day = self.day_number()
        if day is not None:
            parts.append(f"Day {day}")
        return " · ".join(parts)


# ── Building one ──────────────────────────────────────────────────────────────


def _sides(group_id: int) -> tuple[dict, dict]:
    """Every member of a group shaped for the engine, in one read.

    Returns `{registrant_id: SideInput}` for the ones we can predict, and the
    member rows for everyone, so a row can still name a player we could not
    build a side for.

    Built per player rather than per meeting. A player meets two people on a
    two-meeting day, and building a side twice would read the same squads twice
    and, worse, produce two objects a caller could drift apart.
    """
    sides: dict[int, predict_lib.SideInput] = {}
    names: dict[int, dict] = {}
    for member in db.get_group_scouting(group_id):
        rid = member["registrant_id"]
        names[rid] = member
        try:
            sides[rid] = predict_lib.build_side(member)
        except predict_lib.NotEnoughData:
            continue
    return sides, names


def _label_sides(picks: list[Pick]) -> None:
    """Fill in `a_label` / `b_label`, adding a server only where one is needed.

    Names are unique per server, not across them, and a group draws from
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
            setattr(pick, f"{side}_label", f"{name} #{server}" if ambiguous and server else name)


def assemble(group: dict, play_on: str, pairs, *, updated_at=None, updated_by=None) -> Slate:
    """Score a list of `(a_id, b_id)` meetings into a slate.

    Split out of `build` so a surface can show the card somebody is part way
    through choosing without writing it down first. Both paths score the same
    way, which is what stops a preview from disagreeing with what gets saved.

    `champion_duel_engine.constants()` is read once for the whole slate rather
    than once a row. It is the same dictionary every time, and hanging a fresh
    copy off seventeen `Prediction`s would be seventeen reads of the same pin.
    """
    if not predict_lib.ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")
    from champion_duel_engine import constants

    pairs = list(pairs)
    # The same cap `set_slate` enforces, so a preview refuses exactly where the
    # save would. Without it an eighteen-meeting preview renders a card the
    # person is then told they cannot keep, which is the disagreement between
    # preview and save this function exists to prevent.
    if len(pairs) > db.MAX_PICKS:
        raise ValueError(f"a card carries at most {db.MAX_PICKS} meetings, not {len(pairs)}")

    slate = Slate(
        group=group,
        play_on=play_on,
        updated_at=updated_at,
        updated_by=updated_by,
    )
    sides, members = _sides(group["id"])
    engine = constants()

    for position, (a_id, b_id) in enumerate(pairs, start=1):
        a_row, b_row = members.get(a_id), members.get(b_id)
        pick = Pick(
            position=position,
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
                p_a=predict_lib.predict_pair(a, b, best_of=slate.best_of),
                a=a,
                b=b,
                engine=engine,
            )
        slate.picks.append(pick)
    _label_sides(slate.picks)
    return slate


def build(group_id: int, play_on) -> Slate | None:
    """The stored card for one group and one day, scored. None if there is none.

    None rather than an empty slate, because "nobody has built tomorrow's card
    yet" and "the card is empty" are different things to say to a reader and
    only one of them is true.
    """
    stored = db.get_slate(group_id, play_on)
    if stored is None:
        return None
    group = db.get_group(group_id)
    if group is None:  # pragma: no cover - the slate cascades with the group
        return None
    return assemble(
        group,
        stored["play_on"],
        [(m["a_id"], m["b_id"]) for m in stored["meetings"]],
        updated_at=stored.get("updated_at"),
        updated_by=stored.get("updated_by"),
    )


def todays(group_id: int) -> Slate | None:
    """Today's card, on the game's clock rather than the reader's.

    The card is prepared the evening before, so the day it is FOR and the day
    it was built are different days. This asks for the one it is for.
    """
    return build(group_id, db.server_today().isoformat())


# ── The caption ───────────────────────────────────────────────────────────────


def caption(slate: Slate) -> str:
    """The whole card as text, for beside the image.

    The card carries this visually. The line is what survives a screen reader,
    a failed image load, and Discord's own search, none of which can read a
    WebP -- and on a slate that matters more than it does on a single
    prediction, because there are up to seventeen numbers in the picture.

    Clamped to Discord's message ceiling by dropping whole rows off the end and
    saying so. A caption that silently stops mid-row would read as a card with
    fewer meetings on it than it has.

    **Names are escaped.** They are bolded here and drawn plain on the card, so
    a player called `Rav**en` would otherwise appear under two different names
    in one message: Discord eats the asterisks in the caption and the card
    keeps them. `discord.utils.escape_markdown` is the same call the hub makes
    at every other site that bolds a player. Imported inside the function
    because this module has no other reason to know about Discord, and a
    caller that only wants a slate should not pay for the library.
    """
    from discord.utils import escape_markdown

    from champion_duel_wording import probability

    head = f"{PICKS_TITLE}: {slate.subject()}"
    lines = [head]
    dropped = False
    for pick in slate.picks:
        a, b = escape_markdown(pick.a_label), escape_markdown(pick.b_label)
        if pick.predicted:
            line = CAPTION_ROW.format(
                i=pick.position,
                a=a,
                b=b,
                p_a=probability(pick.p_a),
                p_b=probability(pick.p_b),
                confidence=pick.confidence().capitalize(),
            )
        else:
            line = CAPTION_ROW_UNPREDICTED.format(i=pick.position, a=a, b=b)
        # +1 for the newline this line would add, and room for the notice that
        # replaces whatever does not fit.
        room = CAPTION_LIMIT - len("\n".join(lines)) - len(CAPTION_TRUNCATED) - 2
        if len(line) + 1 > room:
            dropped = True
            break
        lines.append(line)
    if dropped:
        lines.append(CAPTION_TRUNCATED)
    return "\n".join(lines)[:CAPTION_LIMIT]

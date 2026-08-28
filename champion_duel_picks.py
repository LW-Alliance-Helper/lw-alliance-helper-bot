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
surfaces a single prediction does (the card, the caption beside it, and the
embed it falls back to) and the same way of drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import champion_duel_db as db
import champion_duel_predict as predict_lib

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

#: A side with no name at all, which is a meeting naming a registrant that no
#: longer exists. `set_slate` refuses to write one, so it is only reachable
#: from a preview or from a player deleted after the card was saved -- but it
#: is set on the label rather than at each surface, so the card and the caption
#: cannot say two different things about the same gap. The VS card's `_name`
#: already reads this way for the same reason.
CARD_UNKNOWN = "(unknown)"

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
        """
        stage_label = db.STAGE_LABELS.get(self.stage, self.stage)
        parts = [p for p in (stage_label, self.date_label()) if p]
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


# ── The caption ───────────────────────────────────────────────────────────────


def caption(slate: Slate) -> str:
    """The whole card as text, for beside the image.

    The card carries this visually. The line is what survives a screen reader,
    a failed image load, and Discord's own search, none of which can read a
    WebP -- and on a slate that matters more than it does on a single
    prediction, because there are up to twenty numbers in the picture.

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

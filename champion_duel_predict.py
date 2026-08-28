"""Turning stored scouting into an engine prediction.

The engine takes powers and types. The database stores squads by slot and
sightings as type permutations. This module is the join between them, and it
lives apart from both on purpose: `champion_duel_db.py` is data access and the
engine is a pinned package, so the shaping that belongs to neither would
otherwise get copied into whichever surface asked first. The Discord hub calls
it today; a roster-driven `/predict` route is the same call.

**A prediction carries how much it is worth.** `Prediction.confidence` reports
what the number was actually built from — how many of the six squad values were
observed rather than estimated, and how many sightings backed the deployment
orders. A 61% built from six estimates and no sightings is not the same claim
as a 61% built from six observations and eleven sightings, and a surface that
renders them identically is telling the reader something untrue.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

try:
    from champion_duel_engine import predict_matchup

    ENGINE_AVAILABLE = True
except ImportError:  # pragma: no cover - asserted through the degraded path
    ENGINE_AVAILABLE = False

SLOTS = (1, 2, 3)


class NotEnoughData(Exception):
    """That player has no usable squad line-up.

    Distinct from "no such player": the registrant exists, we simply have
    nothing to predict with. The two need different copy, because one is fixed
    by checking the spelling and the other by entering a sighting.
    """

    def __init__(self, name: str, missing: list[int]):
        super().__init__(f"{name} is missing squad data for slot(s) {missing}")
        self.name = name
        self.missing = missing


@dataclass
class SideInput:
    """One side, in the shape the engine wants, plus where it came from."""

    name: str
    server: str | None
    player: dict
    orders: list[list[tuple[float, str]]]
    observed_squads: int
    sightings: int
    estimated_squads: int = 0
    #: Squads whose power is a real figure rather than derived from total hero
    #: power — `observed` from the sighting corpus plus `edited`, which is what
    #: the hub writes when someone types a squad in. Both mean "we hold a
    #: number for this"; only `estimated` does not, and that is the distinction
    #: a prediction's worth turns on. Counting `edited` as neither, as this
    #: used to, made every squad the community entered count against the
    #: confidence of the prediction it improved.
    recorded_squads: int = 0

    def likely_order(self) -> tuple[list[tuple[float, str]], bool]:
        """The line-up to *show*, and whether it came from sightings.

        Every surface that renders a prediction has to answer this, and getting
        it wrong is not cosmetic. The prediction averages over each side's
        recorded orders, but a card can only draw one — and drawing the natural
        slot order next to a probability computed from a different order
        invites the reader to work out why the number looks wrong and reach a
        conclusion that is also wrong. Deployment order decides which squad
        meets which, and the counter triangle means order can outweigh power.

        So: the most-seen order where there are sightings, and the natural
        strongest-first order where there are none — which is exactly what the
        prediction used in each case.

        **A tie resolves to the most recent sighting.** `orders` arrives newest
        first (`get_player` sorts on observed_at), and `Counter.most_common`
        breaks ties by first insertion — so a player seen once in each of two
        orders shows the one they were last seen in. That is the right guess:
        the question a prediction answers is which order they will have set
        when the two meet, and the newer observation is the better evidence.
        Only the display collapses to one order; the prediction itself still
        averages over every recorded order, tie or not.
        """
        natural = [(self.player[f"sq{s}_power"], self.player[f"sq{s}_type"]) for s in SLOTS]
        if not self.orders:
            return natural, False
        counts = Counter(tuple(squad_type for _, squad_type in order) for order in self.orders)
        powers = {squad_type: power for power, squad_type in natural}
        return [(powers[t], t) for t in counts.most_common(1)[0][0]], True


@dataclass
class Prediction:
    p_a: float
    a: SideInput
    b: SideInput
    engine: dict = field(default_factory=dict)

    @property
    def p_b(self) -> float:
        return 1.0 - self.p_a

    @property
    def favored(self) -> SideInput:
        return self.a if self.p_a >= 0.5 else self.b

    def confidence(self) -> str:
        """`high` / `medium` / `low`, from what the number was built on.

        Deliberately three coarse buckets rather than a second computed
        percentage. A precise-looking confidence figure would invite exactly
        the misreading it exists to prevent — it is not calibrated against
        anything, unlike `p_a`, which the backtest validates at 48/49.
        """
        recorded = self.a.recorded_squads + self.b.recorded_squads
        sightings = self.a.sightings + self.b.sightings
        if recorded >= 6 and sightings >= 2:
            return "high"
        if recorded >= 3 or sightings >= 1:
            return "medium"
        return "low"


def _type_to_power(squads: list[dict]) -> dict[str, float]:
    """Map each squad type to the power the player fields it at.

    Every lineup observed to date runs one Tank, one Missile and one Aircraft,
    which is what makes a sighting recordable as a bare type permutation at
    all. Where that holds, type is enough to recover the power.
    """
    return {
        s["squad_type"]: s["power"]
        for s in squads
        if s.get("squad_type") and s.get("power") is not None
    }


def build_side(player: dict) -> SideInput:
    """Shape one scouted player into engine input.

    Raises NotEnoughData when a slot has no type or no power — the engine would
    otherwise be handed a None and return a confident-looking number for a
    matchup nobody can actually field.
    """
    squads = {s["slot"]: s for s in (player.get("squads") or [])}
    missing = [
        slot
        for slot in SLOTS
        if not squads.get(slot)
        or not squads[slot].get("squad_type")
        or squads[slot].get("power") is None
    ]
    if missing:
        raise NotEnoughData(player.get("display_name") or "that player", missing)

    natural = {}
    for slot in SLOTS:
        natural[f"sq{slot}_power"] = float(squads[slot]["power"])
        natural[f"sq{slot}_type"] = squads[slot]["squad_type"]

    # Sightings become concrete line-ups by looking each type's power back up.
    # A sighting naming a type this player has no squad of is dropped rather
    # than guessed at: it is either stale (they swapped a squad out) or a typo,
    # and both are better represented by having one fewer order than by an
    # invented power.
    powers = _type_to_power(squads.values())
    orders: list[list[tuple[float, str]]] = []
    for row in player.get("orders") or []:
        types = [row.get("slot1"), row.get("slot2"), row.get("slot3")]
        if all(t in powers for t in types):
            orders.append([(powers[t], t) for t in types])

    observed = sum(1 for slot in SLOTS if squads[slot].get("source") == "observed")
    estimated = sum(1 for slot in SLOTS if squads[slot].get("source") == "estimated")
    recorded = sum(1 for slot in SLOTS if squads[slot].get("source") in ("observed", "edited"))
    return SideInput(
        name=player.get("display_name") or "",
        server=player.get("server"),
        player=natural,
        orders=orders,
        observed_squads=observed,
        estimated_squads=estimated,
        recorded_squads=recorded,
        sightings=len(orders),
    )


def predict(player_a: dict, player_b: dict) -> Prediction:
    """P(A wins), averaged exactly over both sides' observed deployment orders.

    Repeats in a side's sightings are kept, so a player seen five times in one
    order and once in another weighs 5:1 — that ratio *is* the prediction's
    read on what they will have set when the two meet. A side with no sightings
    falls back to its natural strongest-first order, which is what 63% of
    observed line-ups actually are.
    """
    if not ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")
    from champion_duel_engine import constants

    a = build_side(player_a)
    b = build_side(player_b)
    # **1 is correct and nobody should "fix" it.** It reads like an oversight
    # -- the VS card renders semifinal and knockout meetings, and the game
    # plays those as a Bo3 -- so this comment exists to stop the next session
    # changing it. The unit here is one match, and scoring the meeting as a
    # series is measurably worse rather than merely different: on 310 real
    # results a Bo3 amplifies the favourite by 0.4pp while `series_win_prob`
    # amplifies by 8.4pp, because it assumes independent matches where the real
    # scoreline distribution is bimodal (chi-square 358.3 on 3 df). Brier goes
    # 0.1010 -> 0.1052 and log loss 0.3892 -> 0.5599. Source:
    # `champion-duel-simulator`, `semifinal_data/FINDING_matchup_model_stage.md`.
    p_a = predict_pair(a, b, best_of=1)
    return Prediction(p_a=p_a, a=a, b=b, engine=constants())


def predict_pair(a: SideInput, b: SideInput, *, best_of: int) -> float:
    """P(A beats B) over a best-of-`best_of` meeting, from two built sides.

    Split out of `predict` for callers that score many pairs from the same
    players: building a side reads the database, and a group of 8 has 28 pairs
    over 8 sides. It also takes `best_of`, which `predict` does not need.

    **`best_of` is required and has no default here.** The engine defaults it
    to 1, and 1 is the identity transform, so a caller that forgets it gets a
    plausible number at the wrong series length instead of an error. That is
    the exact defect this argument exists to fix, and a default would reinstate
    it one layer up. `predict` passes 1 explicitly for the same reason.

    **Every caller in this repo passes 1, and that is the measured answer
    rather than an unset default.** The game does play a qualifier meeting as
    one match, semifinal and knockout meetings as a Bo3, and both finals as a
    Bo5 (Kevin, 2026-08-15/16) -- so passing the real series length looks like
    the more careful choice. It is not. Scored on 310 real results, a Bo3 in
    this game amplifies the favourite by 0.4pp while this engine's
    `series_win_prob` amplifies by 8.4pp, because it assumes independent
    matches where the real scoreline distribution is bimodal (chi-square 358.3
    on 3 df). Brier gets worse (0.1010 -> 0.1052) and log loss much worse
    (0.3892 -> 0.5599). Source: `champion-duel-simulator`,
    `semifinal_data/FINDING_matchup_model_stage.md`.

    So the argument stays required, because a silent 1 and a chosen 1 are
    different things to a reader of the call site -- but the number to choose
    is 1 until the engine's series model is refitted.
    """
    if not ENGINE_AVAILABLE:
        raise RuntimeError("champion-duel-engine is not installed")
    return predict_matchup(
        a.player,
        b.player,
        orders_a=a.orders or None,
        orders_b=b.orders or None,
        best_of=best_of,
    )

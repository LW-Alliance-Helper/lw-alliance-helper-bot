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
        observed = self.a.observed_squads + self.b.observed_squads
        sightings = self.a.sightings + self.b.sightings
        if observed >= 6 and sightings >= 2:
            return "high"
        if observed >= 3 or sightings >= 1:
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
    return SideInput(
        name=player.get("display_name") or "",
        server=player.get("server"),
        player=natural,
        orders=orders,
        observed_squads=observed,
        estimated_squads=estimated,
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
    p_a = predict_matchup(
        a.player,
        b.player,
        orders_a=a.orders or None,
        orders_b=b.orders or None,
    )
    return Prediction(p_a=p_a, a=a, b=b, engine=constants())

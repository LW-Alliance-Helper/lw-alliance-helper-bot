"""Odds of advancing out of a semifinal group.

The model is `champion_duel_engine.semifinal` and none of it is tested here.
This file tests the join: that what the bot stores arrives in the shape the
engine wants, that partial scouting is passed through rather than discarded,
and that the two refusals stay distinguishable.

The version this replaces tested a round robin of best-of-3 meetings that the
bot implemented itself. That was the wrong model twice over: the round ranks on
points accumulated across all 21 matches rather than on meetings won, and the
implementation had no business being in the bot at all.
"""

from __future__ import annotations

import pytest

import champion_duel_odds as odds

try:
    from champion_duel_engine import semifinal
except ImportError:  # pragma: no cover
    semifinal = None

pytestmark = pytest.mark.skipif(
    not odds.ENGINE_AVAILABLE, reason="champion-duel-engine is not installed"
)

TYPES = ("Tank", "Missile", "Aircraft")


def _member(name, thp, *, powers=None, types=None):
    """One group member as `get_group_scouting` returns them."""
    squads = []
    for slot in (1, 2, 3):
        power = powers[slot - 1] if powers else None
        squad_type = types[slot - 1] if types else None
        if power is None and squad_type is None:
            continue
        squads.append({"slot": slot, "power": power, "squad_type": squad_type})
    return {"display_name": name, "thp": thp, "squads": squads, "orders": []}


def _group(n=8, base=300_000_000, step=8_000_000, **kw):
    return [_member(f"P{i}", base + i * step, **kw) for i in range(n)]


def test_a_group_of_eight_gets_odds_for_all_eight():
    result = odds.group_advance_odds(_group(), trials=200)

    assert len(result.rows) == 8
    assert {r.name for r in result.rows} == {f"P{i}" for i in range(8)}


def test_the_advance_chances_sum_to_the_two_slots_that_exist():
    """Two of eight go through every trial, so the column total is fixed no
    matter how the odds fall. This is the check that catches a broken join
    regardless of what the individual numbers look like."""
    result = odds.group_advance_odds(_group(), trials=400)

    assert sum(r.advance for r in result.rows) == pytest.approx(2.0, abs=0.02)
    assert sum(r.win_group for r in result.rows) == pytest.approx(1.0, abs=0.02)


def test_thp_alone_is_enough():
    """The commonest case by far, and the one Kevin named: an alliance that
    shares only Total Hero Power still gets odds. The engine derives squads
    from THP and samples what it has not been told."""
    result = odds.group_advance_odds(_group(), trials=200)

    assert len(result.rows) == 8
    assert all(0.0 <= r.advance <= 1.0 for r in result.rows)
    assert sum(r.advance for r in result.rows) == pytest.approx(2.0, abs=0.05)


def test_a_stronger_player_is_favoured():
    """A sanity check on the join rather than on the model. If members were
    being passed in with the wrong THP, or the mapping back to names slipped,
    this is what would notice."""
    result = odds.group_advance_odds(_group(), trials=400)

    assert result.rows[0].name == "P7"
    assert result.rows[0].advance > result.rows[-1].advance


# ── What we hold gets passed through ──────────────────────────────────────────


def test_the_shape_is_taken_against_the_squad_with_the_gorilla_removed():
    """`build_player` strips the gorilla off the displayed top squad before
    applying the shape, and warns that getting the order wrong inflates squads
    2 and 3 by the whole gorilla. Dividing raw panel figures understates a
    scouted player's lower squads by about a tenth, and only scouted players:
    everyone else's shape is sampled correctly.

    Computed from the engine's own constant rather than hardcoded, so a refit
    moves the expectation with it instead of breaking this.
    """
    member = _member("A", 300_000_000, powers=[40.0, 100.0, 60.0])

    profile = odds._profile(member)

    base_top = 100.0 * (1.0 - semifinal.GORILLA_FRACTION)
    assert profile["shape"] == pytest.approx((60.0 / base_top, 40.0 / base_top))
    assert profile["shape"][0] > 0.6  # raw division would give exactly 0.6


def test_the_types_are_ordered_with_the_powers_not_by_slot():
    """The engine reads `shape` and `types` as biggest squad first. The bot's
    slots are lineup positions with no ordering, so sorting the powers and
    leaving the types alone would put every type on the wrong squad and turn
    every counter-triangle decision over."""
    member = _member("A", 3e8, powers=[40.0, 100.0, 60.0], types=["Tank", "Missile", "Aircraft"])

    profile = odds._profile(member)

    assert profile["types"] == ["Missile", "Aircraft", "Tank"]


def test_a_partial_shape_is_sampled_rather_than_misread():
    """Slots 1 and 3 recorded with 2 missing would hand the engine the third
    ratio as if it were the second, modelling a second squad at a third of the
    top where the corpus puts it near nine tenths. Sampling the whole shape is
    far closer than that."""
    member = _member("A", 300_000_000, powers=[100.0, None, 40.0])

    assert odds._profile(member) is None


def test_a_type_outside_the_triangle_is_dropped_not_passed_on():
    """Anything the engine's `BEATS` does not know raises a KeyError deep
    inside a trial, after the interaction has been deferred. Dropping it here
    costs one player's types and keeps the group scoreable."""
    member = _member("A", 3e8, powers=[100.0, 60.0, 40.0], types=["Tank", "Navy", "Aircraft"])

    profile = odds._profile(member)

    assert "types" not in profile
    assert "shape" in profile


def test_two_players_with_one_name_stay_two_players():
    """The engine keys everything on `name`, so a collision collapses them into
    one simulated entity: one vanishes and the other banks both their points.
    A semifinal group is drawn from sixteen warzones and registrants are unique
    on (name, server), so this is live rather than hypothetical."""
    members = _group(n=6) + [
        _member("Dup", 300_000_000),
        _member("Dup", 380_000_000),
    ]

    result = odds.group_advance_odds(members, trials=200)

    assert len(result.rows) == 8
    assert [r.name for r in result.rows].count("Dup") == 2


def test_a_player_we_hold_nothing_about_gets_no_profile():
    """None is what `build_player` expects when it is going to sample, so an
    empty dict would be a lie about having looked."""
    assert odds._profile(_member("A", 300_000_000)) is None


# ── The two refusals ──────────────────────────────────────────────────────────


def test_a_group_that_is_not_eight_is_refused_not_absorbed():
    """With an odd count the circle method drops a player from every round and
    past eight the day schedule runs out. Both produce numbers; neither
    produces the round being modelled."""
    with pytest.raises(odds.NotEnoughData):
        odds.group_advance_odds(_group(n=7), trials=50)
    with pytest.raises(odds.NotEnoughData):
        odds.group_advance_odds(_group(n=9), trials=50)


def test_a_missing_total_hero_power_names_who_is_missing_it():
    """The surface has to say which two people to go and look up. "Not ready"
    leaves the reader to work out which of the eight is the problem."""
    members = _group()
    members[2]["thp"] = None
    members[5]["thp"] = None

    with pytest.raises(odds.NotEnoughData) as caught:
        odds.group_advance_odds(members, trials=50)

    assert caught.value.missing_thp == ["P2", "P5"]


def test_the_two_refusals_are_told_apart():
    """Add the missing players and look up two people's power are different
    jobs. A surface pointing at the wrong one is a dead end."""
    short = _group(n=6)
    no_power = _group()
    for m in no_power:
        m["thp"] = None

    with pytest.raises(odds.NotEnoughData) as size_error:
        odds.group_advance_odds(short, trials=50)
    with pytest.raises(odds.NotEnoughData) as power_error:
        odds.group_advance_odds(no_power, trials=50)

    assert size_error.value.missing_thp == []
    assert len(power_error.value.missing_thp) == 8


# ── Uncertainty is kept ───────────────────────────────────────────────────────


def test_jitter_is_on_by_default():
    """Without it the model treats an estimate as a measurement, which the
    engine calls the single biggest source of false confidence in the old
    workbook. Turning it off must be a deliberate argument, never a default.

    Checked by effect: the same field with jitter off is more decisive, because
    every player's squads land exactly on the central estimate.
    """
    members = _group()
    jittered = odds.group_advance_odds(members, trials=600, seed=5)
    central = odds.group_advance_odds(members, trials=600, seed=5, jitter=False)

    spread = max(r.advance for r in jittered.rows) - min(r.advance for r in jittered.rows)
    central_spread = max(r.advance for r in central.rows) - min(r.advance for r in central.rows)
    assert central_spread >= spread


def test_the_points_spread_comes_back_with_the_odds():
    """`points_sd` is how noisy the round is for that player. A surface that
    only ever shows the advance chance can still reach for it."""
    result = odds.group_advance_odds(_group(), trials=200)

    assert all(r.points_sd > 0 for r in result.rows)
    assert all(r.points_mean > 0 for r in result.rows)

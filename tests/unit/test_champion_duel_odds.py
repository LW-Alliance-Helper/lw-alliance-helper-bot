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


def test_a_group_that_is_not_eight_is_refused_not_absorbed():
    """With an odd count the circle method drops a player from every round and
    past eight the day schedule runs out. Both produce numbers; neither
    produces the round being modelled."""
    with pytest.raises(odds.NotEnoughData):
        odds.group_advance_odds(_group(n=7), trials=50)
    with pytest.raises(odds.NotEnoughData):
        odds.group_advance_odds(_group(n=9), trials=50)


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


# ── The 1.5 payload contract ──────────────────────────────────────────────────


def test_powers_go_out_as_displayed_never_pre_deflated():
    """The panel reading already includes the gorilla and the engine strips it
    from the strongest squad itself. Deflating here would strip it twice, and
    shrink the other two as well since they are derived from the top one. An
    earlier version of this module did exactly that."""
    member = _member("A", 3e8, powers=[78e6, 94e6, 82e6])

    squads = odds._squads(member)

    assert [s["power"] for s in squads] == [78e6, 94e6, 82e6]


def test_the_boxes_go_out_in_lineup_order_never_sorted():
    """The engine sorts internally and hands back an `order` mapping so it can
    translate anything indexed against these boxes. Sorting here breaks that
    translation while still producing a lineup, which is the silent kind."""
    member = _member("A", 3e8, powers=[78e6, 94e6, 82e6], types=list(TYPES))

    squads = odds._squads(member)

    assert [s["type"] for s in squads] == list(TYPES)
    assert squads[0]["power"] < squads[1]["power"]


def test_a_blank_box_goes_out_as_a_gap_not_a_zero():
    member = _member("A", 3e8, powers=[94e6, None, 82e6])

    squads = odds._squads(member)

    assert [s["power"] for s in squads] == [94e6, None, 82e6]


def test_mixed_is_only_sent_when_a_power_was_read():
    """`mixed` is box positions when any power is present and POWER RANKS when
    none is, because `measured_base` returns None and the THP path applies it
    directly. We know which box a squad is, never which rank, so sending it on
    the second path would land the penalty on whichever squads sorted there."""
    with_power = _member("A", 3e8, powers=[94e6, None, None])
    with_power["mixed_squads"] = [0, 2]
    no_power = _member("B", 3e8)
    no_power["mixed_squads"] = [0, 2]

    assert odds._profile(with_power, odds._squads(with_power)) == {"mixed": (0, 2)}
    assert odds._profile(no_power, odds._squads(no_power)) is None


def test_an_answered_none_is_distinct_from_never_asked():
    """An empty tuple is a measurement: we looked and every squad is pure."""
    answered = _member("A", 3e8, powers=[94e6, 82e6, 78e6])
    answered["mixed_squads"] = []
    never = _member("B", 3e8, powers=[94e6, 82e6, 78e6])

    assert odds._profile(answered, odds._squads(answered)) == {"mixed": ()}
    assert odds._profile(never, odds._squads(never)) is None


def test_a_squad_power_alone_is_enough_without_thp():
    """THP became optional in 1.5 when a reading is present. A player with one
    box filled and no THP is placed by the reading."""
    members = _group(n=7)
    lone = {
        "display_name": "NoThp",
        "thp": None,
        "orders": [],
        "squads": [{"slot": 1, "power": 90e6, "squad_type": "Tank"}],
    }

    result = odds.group_advance_odds(members + [lone], trials=200)

    assert len(result.rows) == 8


def test_a_player_with_neither_power_nor_thp_is_named():
    members = _group(n=7)
    empty = {"display_name": "Nothing", "thp": None, "squads": [], "orders": []}

    with pytest.raises(odds.NotEnoughData) as caught:
        odds.group_advance_odds(members + [empty], trials=50)

    assert caught.value.missing_thp == ["Nothing"]


def test_the_measured_route_reproduces_the_engine_sessions_check():
    """The regression check handed over with 1.5.0: pinkcatboi entered as
    94/82/78 resolves to base 84.7 / 82.0 / 78.0 with a 9.27 gorilla.

    It reproduces exactly, and it is worth having because the two ways of
    getting it wrong both still produce a lineup. Deflating the powers before
    sending strips the gorilla twice; sorting the boxes breaks the `order`
    mapping the engine uses to translate `mixed`.

    `mixed: ()` is required to reproduce it. Without a profile the engine
    samples a mixed pair from the population, which lands a 3.3% penalty on two
    squads and moves the second to 79.294.
    """
    import random

    measured = semifinal.build_player(
        "pinkcatboi",
        325.8e6,
        random.Random(1),
        profile={"mixed": ()},
        jitter=False,
        level=11,
        squads=[{"power": 94e6}, {"power": 82e6}, {"power": 78e6}],
    )

    assert measured["base"] == pytest.approx([84.73, 82.0, 78.0], abs=0.02)
    assert measured["gorilla"] == pytest.approx(9.27, abs=0.01)


def test_the_two_routes_agree_on_what_they_both_determine():
    """Top squad and gorilla come from the THP fit on either route, so they
    agree to well under a percent. The lower two do NOT: on the measured route
    they are the numbers somebody typed, and on the THP-only route they are the
    shape fit. Asserting whole-lineup agreement would be asserting that the fit
    reproduces one particular player, which is not a property of anything.
    """
    import random

    measured = semifinal.build_player(
        "pinkcatboi",
        325.8e6,
        random.Random(1),
        profile={"mixed": ()},
        jitter=False,
        level=11,
        squads=[{"power": 94e6}, {"power": 82e6}, {"power": 78e6}],
    )
    estimated = semifinal.build_player(
        "pinkcatboi", 325.8e6, random.Random(1), profile={"mixed": ()}, jitter=False
    )

    assert estimated["base"][0] == pytest.approx(measured["base"][0], rel=0.02)
    assert estimated["gorilla"] == pytest.approx(measured["gorilla"], rel=0.02)


def test_an_estimated_squad_is_never_forwarded_as_a_reading():
    """`push_to_bot` writes three `estimated` squads for nearly every
    registrant, derived from THP through the fitted ratios.

    `measured_base` uses a given power EXACTLY, on the stated grounds that a
    typed number is not the THP fit being wrong, so the estimate residual never
    applies to it. Forwarding an estimate as a reading therefore hands back
    near-certainty for a group nobody has looked at, under a footer promising
    that unseen squads are sampled. The engine derives the same numbers itself
    and keeps the uncertainty attached.
    """
    member = _member("A", 3e8, powers=[94e6, 82e6, 78e6])
    for squad in member["squads"]:
        squad["source"] = "estimated"

    assert odds._squads(member) == [{"power": None, "type": None}] * 3


def test_a_real_reading_is_forwarded():
    """The other half. `observed` and `edited` are both somebody looking."""
    for source in ("observed", "edited"):
        member = _member("A", 3e8, powers=[94e6, None, None])
        member["squads"][0]["source"] = source

        assert odds._squads(member)[0]["power"] == 94e6


def test_an_estimated_group_is_no_more_confident_than_thp_alone():
    """The property behind the two tests above, on the surface that shows it.

    Sending the estimated rows measured 100/90/10/0 where the honest answer is
    85/68/28/14. A group nobody has scouted must not read as near-certain.
    """
    estimated = _group()
    for member in estimated:
        for slot, ratio in zip((1, 2, 3), (0.338, 0.258, 0.238)):
            member["squads"].append(
                {
                    "slot": slot,
                    "power": member["thp"] * ratio,
                    "squad_type": None,
                    "source": "estimated",
                }
            )

    with_rows = odds.group_advance_odds(estimated, trials=400, seed=3)
    bare = odds.group_advance_odds(_group(), trials=400, seed=3)

    assert max(r.advance for r in with_rows.rows) == pytest.approx(
        max(r.advance for r in bare.rows), abs=0.05
    )


def test_a_type_outside_the_triangle_is_dropped_not_passed_on():
    """Anything the engine's `BEATS` does not know raises a KeyError inside a
    trial, after the interaction is deferred. `db.VALID_TYPES` and
    `semifinal.TYPES` are separate tuples in separate repos, so this guard is
    the only thing between the two drifting and a member seeing a dead spinner.
    """
    member = _member("A", 3e8, powers=[94e6, 82e6, 78e6], types=["Tank", "Navy", "Aircraft"])
    for squad in member["squads"]:
        squad["source"] = "observed"

    types = [s["type"] for s in odds._squads(member)]

    assert types == ["Tank", None, "Aircraft"]


def test_the_two_refusals_stay_distinguishable():
    """`build_odds_embed` branches its copy on whether `missing_thp` is empty.
    A size refusal that carried names would tell someone four players short to
    go and record a squad for two of them."""
    short = _group(n=6)
    nothing = [{"display_name": f"P{i}", "thp": None, "squads": [], "orders": []} for i in range(8)]

    with pytest.raises(odds.NotEnoughData) as size_error:
        odds.group_advance_odds(short, trials=50)
    with pytest.raises(odds.NotEnoughData) as data_error:
        odds.group_advance_odds(nothing, trials=50)

    assert size_error.value.missing_thp == []
    assert len(data_error.value.missing_thp) == 8

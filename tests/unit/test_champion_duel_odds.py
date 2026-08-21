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
    with_power = _member("A", 3e8, powers=[94e6, 82e6, 78e6])
    for squad, flag in zip(with_power["squads"], (1, 0, 1)):
        squad["source"] = "observed"
        squad["mixed"] = flag
    no_power = _member("B", 3e8)
    no_power["squads"] = [
        {"slot": s, "power": None, "squad_type": None, "mixed": f}
        for s, f in zip((1, 2, 3), (1, 0, 1))
    ]

    # Boxes 1 and 3, which is what the member typed, arriving as positions 0
    # and 2 -- the translation the engine's own summary calls the easiest
    # off-by-one in the payload.
    assert odds._profile(with_power, odds._squads(with_power)) == {"mixed": (0, 2)}
    assert odds._profile(no_power, odds._squads(no_power)) is None


def test_an_answered_none_is_distinct_from_never_asked():
    """An empty tuple is a measurement: we looked and every squad is pure."""
    answered = _member("A", 3e8, powers=[94e6, 82e6, 78e6])
    for squad in answered["squads"]:
        squad["source"] = "observed"
        squad["mixed"] = 0
    never = _member("B", 3e8, powers=[94e6, 82e6, 78e6])
    for squad in never["squads"]:
        squad["source"] = "observed"

    assert odds._profile(answered, odds._squads(answered)) == {"mixed": ()}
    assert odds._profile(never, odds._squads(never)) is None


# ── The imported profile, and the frame it arrives in ─────────────────────────
#
# `import_profiles` stores what the sighting corpus measured, indexed by POWER
# RANK. The `mixed` flag a member answers is indexed by BOX. The engine reads
# the same key in whichever frame the rest of the input puts it in, so each
# source is correct on exactly the path the other is wrong on.


def _measured(name, thp, *, powers=None, profile=None, flags=None):
    member = _member(name, thp, powers=powers, types=TYPES if powers else None)
    if powers is None:
        member["squads"] = [{"slot": s, "power": None, "squad_type": None} for s in (1, 2, 3)]
    for squad in member["squads"]:
        squad["source"] = "observed"
    if flags is not None:
        for squad, flag in zip(member["squads"], flags):
            squad["mixed"] = flag
    member["profile"] = profile
    return member


def test_what_only_the_corpus_measured_goes_out_untranslated():
    """`types`, `shape` and `gorilla` are all power-rank facts, so none of them
    needs the box translation `mixed` does. Nothing in the bot measures them at
    all -- before the import block was read, an unscouted player got a type
    ordering sampled from the population and a shape off the THP fit."""
    member = _measured(
        "A",
        3e8,
        profile={"types": ["Aircraft", "Tank", "Missile"], "shape": [0.94, 0.87], "gorilla": 1},
    )

    assert odds._profile(member, odds._squads(member)) == {
        "types": ["Aircraft", "Tank", "Missile"],
        "shape": [0.94, 0.87],
        "gorilla": 1,
    }


def test_corpus_positions_go_straight_out_when_nothing_was_read():
    """No power read means the THP path, where the engine applies the tuple
    directly as power ranks -- which is the frame the corpus measured it in."""
    member = _measured("A", 3e8, profile={"mixed": [0, 1]})

    assert odds._profile(member, odds._squads(member)) == {"mixed": (0, 1)}


def test_corpus_positions_are_translated_to_boxes_when_all_three_were_read():
    """With every box carrying a power, which rank each box holds is readable
    off what we store -- the same sort `measured_base` is about to do. Without
    the translation the engine would read power ranks as box positions and land
    the penalty on whichever squads sorted there."""
    # Boxes in lineup order 82 / 94 / 78, so power ranks run box1, box0, box2.
    member = _measured("A", 3e8, powers=[82e6, 94e6, 78e6], profile={"mixed": [0, 2]})

    # Ranks 0 and 2 are the 94M and the 78M, which are boxes 1 and 2.
    assert odds._profile(member, odds._squads(member))["mixed"] == (1, 2)


def test_corpus_positions_are_dropped_when_only_some_boxes_were_read():
    """Which rank a box holds is inferred by the engine from the THP fit and is
    not knowable here, so there is no translation to make. A misplaced 3.3%
    penalty is worse than a sampled one -- the call `build_player` makes
    internally for the same reason."""
    member = _measured("A", 3e8, powers=[82e6, None, None], profile={"mixed": [0, 2]})

    assert odds._profile(member, odds._squads(member)) is None


def test_a_corpus_none_is_safe_on_either_path():
    """An empty answer names no positions, so it is true in both frames."""
    read = _measured("A", 3e8, powers=[82e6, None, None], profile={"mixed": []})
    unread = _measured("B", 3e8, profile={"mixed": []})

    assert odds._profile(read, odds._squads(read)) == {"mixed": ()}
    assert odds._profile(unread, odds._squads(unread)) == {"mixed": ()}


def test_the_members_own_answer_wins_over_the_corpus():
    """Not a ranking of sources -- a member reading their screen and a scout
    watching a battle are the same kind of evidence. It is recency: the member
    is describing the lineup they hold now, and the corpus fitted an ordering
    from fights that may be a round old."""
    member = _measured(
        "A", 3e8, powers=[82e6, 94e6, 78e6], profile={"mixed": [0, 2]}, flags=(1, 0, 0)
    )

    # Box 1, which is what the member typed. The corpus would have said 1 and 2.
    assert odds._profile(member, odds._squads(member))["mixed"] == (0,)


def test_a_half_answered_lineup_does_not_assert_purity():
    """`mixed` is a set of positions and the engine reads a box's absence from
    it as pure, so a partial answer would claim a measurement about squads
    nobody was asked about. That is the NULL-versus-0 distinction the `squads`
    schema keeps apart, and there is no way to say "box 2 unknown" here."""
    member = _measured("A", 3e8, powers=[82e6, 94e6, 78e6], profile={"mixed": [0]})
    member["squads"][0]["mixed"] = 1  # box 1 answered, boxes 2 and 3 never asked

    # The corpus measured all three, so it answers instead of the partial.
    assert odds._profile(member, odds._squads(member))["mixed"] == (1,)


def test_the_corpus_answers_where_the_members_boxes_cannot():
    """A box answer with nothing read would be applied as power ranks, so that
    one case falls through to the corpus rather than being dropped."""
    member = _measured("A", 3e8, profile={"mixed": [1]}, flags=(1, 0, 1))

    assert odds._profile(member, odds._squads(member)) == {"mixed": (1,)}


def test_a_legacy_count_is_only_sent_on_the_path_it_expands_correctly_on():
    """The engine reads a bare count as "the bottom n" POWER RANKS. On the
    measured path `build_player` would then read that expansion as boxes, so
    the count is dropped rather than moved to the wrong squads."""
    unread = _measured("A", 3e8, profile={"n_mixed": 2})
    read = _measured("B", 3e8, powers=[82e6, 94e6, 78e6], profile={"n_mixed": 2})

    assert odds._profile(unread, odds._squads(unread)) == {"n_mixed": 2}
    assert odds._profile(read, odds._squads(read)) is None


def test_an_estimated_squad_leaves_the_corpus_positions_in_the_rank_frame():
    """`_squads` withholds an estimate from the engine, so a player whose only
    squads are THP-derived is on the THP path however many rows they have. The
    profile has to follow that, not the row count."""
    member = _measured("A", 3e8, powers=[82e6, 94e6, 78e6], profile={"mixed": [0, 2]})
    for squad in member["squads"]:
        squad["source"] = "estimated"

    assert odds._profile(member, odds._squads(member)) == {"mixed": (0, 2)}


def test_a_group_carrying_profiles_still_scores():
    """The end of the join: everything above assembles a dict the engine has to
    accept. A bad value in it raises inside a trial, after the interaction has
    been deferred, and the member watches a spinner that never resolves."""
    members = _group(n=8)
    for i, member in enumerate(members):
        member["profile"] = {
            "types": ["Aircraft", "Tank", "Missile"],
            "shape": [0.94, 0.87],
            "mixed": [2] if i % 2 else [],
            "gorilla": 0,
        }

    result = odds.group_advance_odds(members, trials=100)

    assert len(result.rows) == 8
    assert sum(r.advance for r in result.rows) == pytest.approx(2.0, abs=0.01)


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


def test_the_engine_sessions_canonical_snippet_holds():
    """The regression check as the engine session finally wrote it.

    Two things in it are worth keeping. `thp=None` on the measured route: three
    readings place a player with no Total Hero Power at all, which is what
    makes THP optional rather than merely tolerated. And `mixed: []` -- without
    it the engine samples a mixed pair from the population and the second squad
    moves to 79.294, so anyone quoting 82.0 without a profile is quoting a
    number the model did not produce.

    Their first summary reported the THP-only route as 84.4 / 81.6 / 78.1. It
    is 84.37 and then the shape fit; those two figures were the with-profile
    validation against the same player, and the route was mislabelled. Only the
    top squad and the gorilla are comparable across the two routes, which is
    what this asserts.
    """
    import random

    rng = random.Random(1)
    measured = semifinal.build_player(
        "pinkcatboi",
        None,
        rng,
        {"mixed": []},
        jitter=False,
        squads=[{"power": 94e6}, {"power": 82e6}, {"power": 78e6}],
    )
    assert measured["base"] == pytest.approx([84.732, 82.000, 78.000], abs=0.001)
    assert round(measured["gorilla"], 2) == 9.27

    # THE THP ROUTE MOVES WITH `ESTIMATE_LIFT`, so it is asserted against the
    # constant rather than against the number the constant produced.
    #
    # 84.37 and 9.23 are the pre-lift figures and they are what an engine
    # before 1.13.0 returns. That version multiplies the whole THP-derived
    # line-up by ESTIMATE_LIFT at the end of `build_player`'s THP path, which
    # takes the top squad to 87.61 -- exactly 84.37 x 1.0384. Pinned as a
    # literal, this test fails the moment the engine pin moves, and it fails
    # looking like a regression when what it has caught is a documented model
    # change on the route the constant exists to correct.
    #
    # The measured route above is deliberately NOT scaled. A caller who sends
    # real `squads` never touches the lift, which is the whole contract, so
    # those three numbers are the same on every engine and any drift in them
    # IS a regression.
    lift = getattr(semifinal, "ESTIMATE_LIFT", 1.0)
    estimated = semifinal.build_player("pinkcatboi", 325_800_000, rng, {"mixed": []}, jitter=False)
    assert round(estimated["base"][0], 2) == pytest.approx(round(84.37 * lift, 2), abs=0.02)
    assert round(estimated["gorilla"], 2) == pytest.approx(round(9.23 * lift, 2), abs=0.02)


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


# ── One group round, and the one that came out ────────────────────────────────


def test_the_semifinal_round_puts_two_of_eight_through():
    """Read off the model rather than fixed in the join, because the surface
    prints the number and a top-2 percentage is a different claim from a top-8
    one wearing the same units."""
    result = odds.group_advance_odds(_group(), stage="semifinals", trials=200)

    assert result.advance == 2
    assert len(result.rows) == 8


def test_the_qualifier_round_no_longer_has_a_model_to_be_scored_by():
    """Removed on 2026-08-21, and for reachability rather than value: odds need
    a hero power or a squad power for every player in the group, a qualifier
    group is 100, and there is no mechanism by which 100 of them arrive. The
    control sat in the round people first meet the feature and refused every
    press, which is worse than not offering it.

    Removed rather than hidden, so it cannot be confused with a live surface.
    Recording a qualifier group is untouched and is asserted elsewhere."""
    assert "qualifiers" not in odds.STAGES_WITH_A_MODEL
    assert "qualifiers" not in odds._models()

    with pytest.raises(odds.NotEnoughData) as caught:
        odds.group_advance_odds(_group(), stage="qualifiers", trials=5)

    assert "no model" in str(caught.value)


def test_a_round_with_no_model_is_refused_rather_than_scored_by_another():
    """The knockouts are a single-elimination field of 32. Scoring them with
    either group model would answer a different question convincingly.

    The refusal survived the arrival of `knockout.py` in engine 1.12.0; only
    its reason changed. It used to say "there is no model for the knockouts
    round", which stopped being true — and a refusal giving a reason that is
    false is how a caller ends up building the wrong fix. Membership of
    `_models()` is what enforces this: the knockouts are deliberately absent
    from it, because the first version of that change registered them there and
    `group_advance_odds` went on to accept a round with no `simulate_group` at
    all, reaching an AttributeError several steps past the refusal.
    """
    with pytest.raises(odds.NotEnoughData) as caught:
        odds.group_advance_odds(_group(), stage="knockouts", trials=5)

    message = str(caught.value)
    assert "no group model" in message
    assert "bracket_odds" in message


def test_a_round_that_genuinely_has_no_model_still_says_so_plainly():
    """The generic branch, kept apart from the knockouts' own sentence."""
    with pytest.raises(odds.NotEnoughData) as caught:
        odds.group_advance_odds(_group(), stage="quarterfinals", trials=5)

    assert "no model for the quarterfinals round" in str(caught.value)


def test_a_troop_level_outside_the_game_is_dropped_not_forwarded():
    """`scoring.troop_value` raises outside 1-11, `build_odds_embed` catches
    only NotEnoughData, and the interaction is already deferred -- so a bad
    level would leave a member watching a spinner that never resolves."""
    group = _group()
    group[0]["troop_level"] = 99
    group[1]["troop_level"] = 0
    group[2]["troop_level"] = 11

    result = odds.group_advance_odds(group, trials=100)

    assert len(result.rows) == 8


# ── The collection form's two new fields ──────────────────────────────────────


def test_the_mixed_boxes_a_member_typed_become_engine_positions():
    """A member reads their lineup screen and types `1,3`. The engine wants
    positions against those same boxes, so 1 and 3 become 0 and 2 -- the
    off-by-one the engine session called the easiest in the payload."""
    member = _member("A", 3e8, powers=[94e6, 82e6, 78e6])
    for squad, flag in zip(member["squads"], (1, 0, 1)):
        squad["source"] = "observed"
        squad["mixed"] = flag

    assert odds._profile(member, odds._squads(member)) == {"mixed": (0, 2)}


def test_a_mixed_answer_of_none_is_kept_as_a_measurement():
    """0 across the board says somebody looked and every squad is pure, which
    the engine treats as measured. Absent makes it sample a mixed pair."""
    answered = _member("A", 3e8, powers=[94e6, 82e6, 78e6])
    for squad in answered["squads"]:
        squad["source"] = "observed"
        squad["mixed"] = 0

    assert odds._profile(answered, odds._squads(answered)) == {"mixed": ()}


def test_the_troop_level_reaches_the_spec():
    """Levels only separate players in a mixed-level group, but they do, and
    the semifinal model scores on troop value."""
    group = _group()
    for member, level in zip(group, (11, 10, 11, 11, 9, 11, 11, 10)):
        member["troop_level"] = level

    result = odds.group_advance_odds(group, trials=200)

    assert len(result.rows) == 8


def test_two_runs_of_one_group_do_not_pay_twice():
    """The runs behind this cache are seconds of pure Python and pure Python
    holds the GIL, so the cost has to be per data change rather than per press.
    The key is the spec list, so any edit misses."""
    group = _group()
    first = odds.group_advance_odds(group, trials=200)
    second = odds.group_advance_odds(group, trials=200)

    assert first is second

    group[0]["thp"] = 999e6
    assert odds.group_advance_odds(group, trials=200) is not first

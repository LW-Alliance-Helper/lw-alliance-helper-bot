"""What a player does, and what to field against them.

Two things are worth testing here and they are different in kind. The first is
the join: a habit read off `order_history` rows, a grid built from the right
number of deployments, a recommendation that is the counter and not something
that merely looks like one. The second is the set of refusals — the cases where
this module has to decline to answer rather than produce a confident-looking
number, which is the whole reason the read is graded at all.

The refusals get the most tests, because every one of them is a place a future
change could quietly start answering.
"""

from __future__ import annotations

import pytest

import champion_duel_db as db
import champion_duel_intel as intel_lib

KEV = {"discord_user_id": "111", "discord_name": "Kevin", "guild_id": "999"}
TYPES = ("Tank", "Missile", "Aircraft")


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    db.import_registrants(
        [
            {"name": "AlphaOne", "group": "M", "rank": 1, "server": "738", "thp": 266_000_000},
            {"name": "BetaTwo", "group": "M", "rank": 2, "server": "738", "thp": 262_000_000},
            {"name": "Giant", "group": "M", "rank": 3, "server": "738", "thp": 480_000_000},
            {"name": "NoPower", "group": "M", "rank": 4, "server": "738"},
        ],
        stage="qualifiers",
    )
    return db


def _rid(name):
    return db.resolve_registrant(name, server="738")["id"]


def _squads(name, source="observed", types=TYPES, powers=(90_000_000, 68_000_000, 63_000_000)):
    rid = _rid(name)
    for slot, (squad_type, power) in enumerate(zip(types, powers), start=1):
        db.set_squad(rid, slot, squad_type=squad_type, power=power, actor=KEV, source=source)
    return rid


def _player(name):
    return db.get_player(name, server="738", include_scouting=True)


def _orders(name, orders, opponent=None, observed_at=None):
    rid = _rid(name)
    for order in orders:
        db.add_order(rid, list(order), actor=KEV, opponent=opponent, observed_at=observed_at)
    return rid


# ── The habit ────────────────────────────────────────────────────────────────


def test_no_sightings_is_no_habit_rather_than_an_empty_one(cd_db):
    """`None` and "seen zero times" are different claims and only one is true."""
    _squads("AlphaOne")
    assert intel_lib.read_habit(_player("AlphaOne")) is None


def test_repeats_are_the_signal_and_are_kept(cd_db):
    """Five-to-one is the read. Collapsing repeats would throw it away."""
    _squads("AlphaOne")
    _orders("AlphaOne", [("Missile", "Tank", "Aircraft")] * 5 + [("Tank", "Missile", "Aircraft")])

    habit = intel_lib.read_habit(_player("AlphaOne"))
    assert habit.top == ("Missile", "Tank", "Aircraft")
    assert (habit.seen, habit.total, habit.distinct) == (5, 6, 2)
    assert habit.share == pytest.approx(5 / 6)


def test_a_meeting_is_one_opponent_on_one_date(cd_db):
    """The change rate needs meetings, and a meeting is what groups the rows."""
    _squads("AlphaOne")
    _orders(
        "AlphaOne", [("Missile", "Tank", "Aircraft")] * 2, opponent="X", observed_at="2026-08-10"
    )
    _orders(
        "AlphaOne",
        [("Missile", "Tank", "Aircraft"), ("Tank", "Missile", "Aircraft")],
        opponent="Y",
        observed_at="2026-08-11",
    )

    habit = intel_lib.read_habit(_player("AlphaOne"))
    assert habit.meetings == 2
    assert habit.meetings_multi == 2
    assert habit.meetings_changed == 1
    assert habit.change_rate == pytest.approx(0.5)


def test_a_meeting_sighted_once_is_not_a_meeting_they_did_not_change_in(cd_db):
    """The denominator is meetings that could have shown a change.

    A meeting with one recorded line-up cannot show one however often the
    player changed, so counting it as unchanged is the "we cannot tell" read as
    "they never change" that `grade_read` refuses everywhere else. Six meetings
    sighted once each used to score a change rate of 0.0 and earn a `strong`
    read on a record that never watched a single change happen."""
    _squads("AlphaOne")
    for i in range(6):
        _orders(
            "AlphaOne",
            [("Missile", "Tank", "Aircraft")],
            opponent=f"opp{i}",
            observed_at=f"2026-08-1{i}",
        )

    habit = intel_lib.read_habit(_player("AlphaOne"))
    assert habit.meetings == 6, "the meetings are real and are still counted"
    assert habit.meetings_multi == 0, "none of them could have shown a change"
    assert habit.change_rate is None
    assert habit.share == 1.0 and habit.total >= intel_lib.STRONG_SEEN
    assert intel_lib.grade_read(habit) == intel_lib.LEAN


def test_an_unmeasurable_change_rate_is_none_and_never_zero(cd_db):
    """ "We cannot tell" and "they never change" must not render the same."""
    _squads("AlphaOne")
    _orders("AlphaOne", [("Missile", "Tank", "Aircraft")] * 6)

    assert intel_lib.read_habit(_player("AlphaOne")).change_rate is None


# ── The grade ────────────────────────────────────────────────────────────────


def test_a_thin_record_grades_none_however_consistent_it_looks(cd_db):
    """Three sightings all the same is not a habit, it is three sightings."""
    _squads("AlphaOne")
    _orders("AlphaOne", [("Missile", "Tank", "Aircraft")] * 3)

    assert intel_lib.grade_read(intel_lib.read_habit(_player("AlphaOne"))) == intel_lib.NONE


def test_an_unmeasurable_change_rate_cannot_earn_strong(cd_db):
    """The one way this grading could overclaim: treating unknown as never."""
    _squads("AlphaOne")
    _orders("AlphaOne", [("Missile", "Tank", "Aircraft")] * 8)

    habit = intel_lib.read_habit(_player("AlphaOne"))
    assert habit.share == 1.0 and habit.total >= intel_lib.STRONG_SEEN
    assert intel_lib.grade_read(habit) == intel_lib.LEAN


def test_a_repeated_order_they_hold_inside_meetings_grades_strong(cd_db):
    _squads("AlphaOne")
    for i, opponent in enumerate(("X", "Y", "Z")):
        _orders(
            "AlphaOne",
            [("Missile", "Tank", "Aircraft")] * 2,
            opponent=opponent,
            observed_at=f"2026-08-1{i}",
        )

    assert intel_lib.grade_read(intel_lib.read_habit(_player("AlphaOne"))) == intel_lib.STRONG


def test_no_habit_at_all_grades_none(cd_db):
    assert intel_lib.grade_read(None) == intel_lib.NONE


# ── Is a read worth having ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "spread,expected",
    [
        (0.98, intel_lib.WORTH_DECIDES),
        (0.40, intel_lib.WORTH_DECIDES),
        (0.399, intel_lib.WORTH_SWINGS),
        (0.10, intel_lib.WORTH_SWINGS),
        (0.099, intel_lib.WORTH_SETTLED),
        (0.0, intel_lib.WORTH_SETTLED),
    ],
)
def test_the_measured_spread_decides_whether_a_read_is_worth_anything(spread, expected):
    """Graded on what was computed for these two players, not inferred from a
    proxy. Squad power and total hero power are different columns and can
    disagree, so grading on the gap could print "power decides this one" over a
    grid where it plainly did not."""
    assert intel_lib.worth(spread) == expected


def test_the_grade_lands_where_the_corpus_bands_do(cd_db):
    """The thresholds are only defensible if they agree with the finding they
    came from: a counter decides matches below a 5% power gap and has never
    overturned one above 10%."""
    _squads("AlphaOne", source="observed")
    _squads("BetaTwo", source="observed")
    assert intel_lib.intel(_player("AlphaOne"), _player("BetaTwo")).worth == intel_lib.WORTH_DECIDES

    _squads("Giant", source="observed", powers=(162_000_000, 124_000_000, 114_000_000))
    assert intel_lib.intel(_player("Giant"), _player("BetaTwo")).worth == intel_lib.WORTH_SETTLED


# ── The grid ─────────────────────────────────────────────────────────────────


def test_a_placeholder_type_is_enumerated_rather_than_trusted(cd_db):
    """The correctness point of the module.

    `push_to_bot` writes Tank/Missile/Aircraft on every estimated row, which is
    a fill-in and not a sighting. Trusting it puts both players Tank-on-Tank in
    every slot, so no counter ever fires and an exact-looking probability comes
    back that is a power comparison.
    """
    _squads("AlphaOne", source="estimated")
    _squads("BetaTwo", source="observed")

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.their_types_known is False
    # six of their orders times six type assignments, against six of yours
    assert result.envelope.combinations == 6 * 36


def test_one_recorded_box_halves_their_space(cd_db):
    """Knowing which squad is the Tank constrains the other two to a pair.

    This is the argument for asking a member to fill in one box, so it is worth
    a test rather than a comment: partial knowledge has to actually narrow the
    grid or the ask is not worth making.
    """
    _squads("AlphaOne", source="estimated")
    _squads("BetaTwo", source="observed")
    db.set_squad(_rid("AlphaOne"), 1, squad_type="Missile", power=90_000_000, actor=KEV)

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.their_types_known is False
    assert result.envelope.combinations == 6 * 12


def test_the_recommendation_against_a_graded_read_is_the_counter(cd_db):
    """Slot for slot, from the engine's own triangle rather than a local copy."""
    _squads("AlphaOne", source="observed")
    _squads("BetaTwo", source="observed")
    for i, opponent in enumerate(("X", "Y", "Z")):
        _orders(
            "AlphaOne",
            [("Missile", "Tank", "Aircraft")] * 2,
            opponent=opponent,
            observed_at=f"2026-08-1{i}",
        )

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.read == intel_lib.STRONG
    assert result.counter_types == ("Tank", "Aircraft", "Missile")
    assert result.recommended.order == result.counter_types


def test_with_no_read_the_recommendation_ranks_on_the_mean_not_the_floor(cd_db):
    """They are not aiming at you. Deployments are set blind and at the same
    time, so a maximin pick would be answering a question nobody asked."""
    _squads("AlphaOne", source="observed")
    _squads("BetaTwo", source="observed")

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.read == intel_lib.NONE
    assert result.recommended is result.options[0]
    assert result.recommended.mean == max(o.mean for o in result.options)


def test_your_own_placeholder_types_offer_no_choice_at_all(cd_db):
    """Six identical rows are one row six times. Say nothing instead."""
    _squads("AlphaOne", source="observed")
    _squads("BetaTwo", source="estimated")

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.needs_your_squads is True
    assert result.options == []
    assert result.recommended is None
    # the envelope still answers "how much is the choice worth here"
    assert result.envelope is not None


def test_their_best_reply_is_over_the_orders_they_could_set(cd_db):
    """Most worth stating exactly when the read is best: a strong read prices
    the advice against one line-up, and the risk is that they break habit."""
    _squads("AlphaOne", source="observed")
    _squads("BetaTwo", source="observed")
    for i, opponent in enumerate(("X", "Y", "Z")):
        _orders(
            "AlphaOne",
            [("Missile", "Tank", "Aircraft")] * 2,
            opponent=opponent,
            observed_at=f"2026-08-1{i}",
        )

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.their_best_reply is not None
    assert result.p_if_they_switch < result.p_if_they_hold


def test_a_placeholder_line_up_names_no_best_reply(cd_db):
    """Naming one arrangement out of thirty-six would dress a guess as a
    finding."""
    _squads("AlphaOne", source="estimated")
    _squads("BetaTwo", source="observed")

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.their_best_reply is None
    assert result.p_if_they_switch is None


def test_the_second_player_is_required_rather_than_defaulted(cd_db):
    """The one-name shape is gone and it has to fail loudly.

    `you` used to default to `None` and return a partial answer. Leaving the
    default in place with the branch removed would have turned a reachable
    state into an AttributeError inside `build_side`, several frames from
    anything that names a player — so the signature refuses it and an explicit
    `None` is told what happened.
    """
    _squads("AlphaOne", source="observed")

    with pytest.raises(TypeError):
        intel_lib.intel(_player("AlphaOne"))

    with pytest.raises(ValueError, match="both players"):
        intel_lib.intel(_player("AlphaOne"), None)


def test_a_missing_thp_costs_the_gap_but_not_the_grade(cd_db):
    """Two different things, and only one of them needs a Total Hero Power.

    The gap is a fact about the two players and there is none to state without
    both figures. How much the deployment is worth is measured off the grid,
    which is built from squads — so it survives, and the surface can still
    answer its most important question for a player nobody has a THP for.
    """
    _squads("AlphaOne", source="observed")
    _squads("NoPower", source="observed")

    result = intel_lib.intel(_player("AlphaOne"), _player("NoPower"))
    assert result.gap is None
    assert result.worth in (
        intel_lib.WORTH_DECIDES,
        intel_lib.WORTH_SWINGS,
        intel_lib.WORTH_SETTLED,
    )


def test_a_player_with_a_missing_slot_refuses_like_a_prediction_does(cd_db):
    """Same refusal as `build_side`, so a surface handling one handles both."""
    rid = _rid("AlphaOne")
    db.set_squad(rid, 1, squad_type="Tank", power=90_000_000, actor=KEV)
    _squads("BetaTwo", source="observed")

    with pytest.raises(Exception) as exc:
        intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert "AlphaOne" in str(exc.value)


def test_the_counter_map_is_read_off_the_engine(cd_db):
    """Never restated locally. A duplicate goes stale in one repo, not both."""
    from champion_duel_engine import engine

    counters = intel_lib._counters()
    for beater, beaten in engine.BEATS.items():
        assert counters[beaten] == beater


def test_the_grid_is_priced_per_match_not_per_meeting(cd_db):
    """A meeting is three matches and you redeploy between them, so Bo3 would
    charge a decision to a series the player gets to remake twice."""
    _squads("AlphaOne", source="observed")
    _squads("BetaTwo", source="observed")

    one = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    three = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"), best_of=3)
    # Bo3 amplifies whoever is ahead in a cell, so the floor of the envelope
    # drops. Asserted on the floor rather than the mean, which two evenly
    # matched players hold at 0.5 by symmetry at any series length.
    assert three.envelope.floor < one.envelope.floor


# ── What your own choice is worth ────────────────────────────────────────────


def test_an_unscouted_opponent_leaves_your_six_orders_level(cd_db):
    """The measurement behind the refusal on the surface.

    With their types unrecorded every arrangement they could field is averaged,
    and averaging over thirty-six opponents is what flattens the ranking. The
    top row is real and it is not a decision."""
    _squads("AlphaOne", source="estimated")
    _squads("BetaTwo", source="observed")

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.choice_spread < intel_lib.CHOICE_SPREAD
    assert result.choice_matters is False


def test_a_scouted_opponent_with_a_read_makes_the_choice_worth_making(cd_db):
    """The other side of it, or the refusal would swallow the whole feature."""
    _squads("AlphaOne", source="observed")
    _squads("BetaTwo", source="observed")
    for i, opponent in enumerate(("X", "Y", "Z")):
        _orders(
            "AlphaOne",
            [("Missile", "Tank", "Aircraft")] * 2,
            opponent=opponent,
            observed_at=f"2026-08-1{i}",
        )

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.choice_matters is True
    assert result.choice_spread > 0.5


def test_no_options_is_no_spread_rather_than_a_spread_of_zero(cd_db):
    """`None` and "every order is worth the same" are different claims, and
    only one of them is true when your own types are a placeholder."""
    _squads("AlphaOne", source="observed")
    _squads("BetaTwo", source="estimated")

    result = intel_lib.intel(_player("AlphaOne"), _player("BetaTwo"))
    assert result.options == []
    assert result.choice_spread is None
    assert result.choice_matters is False

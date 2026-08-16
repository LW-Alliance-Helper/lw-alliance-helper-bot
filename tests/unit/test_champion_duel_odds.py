"""Odds of advancing out of one group.

The properties worth holding are the ones that would fail *plausibly*. A
series-length bug does not raise; it returns a believable number that is wrong
in a consistent direction, which is exactly how the workbooks carried one for
ten days. So these lean on invariants a wrong answer cannot satisfy: the
advance probabilities must sum to the number of slots, and a longer series must
never hurt the favourite.
"""

import pytest

import champion_duel_odds as odds
import champion_duel_predict as predict_lib

pytestmark = pytest.mark.skipif(
    not predict_lib.ENGINE_AVAILABLE, reason="champion-duel-engine is not installed"
)

TYPES = ("Tank", "Missile", "Air")


def _member(name, base):
    """One predictable player, `base` setting how strong they are."""
    return {
        "display_name": name,
        "server": "738",
        "squads": [
            {
                "slot": s + 1,
                "squad_type": TYPES[s],
                "power": base + s * 1_000_000,
                "source": "observed",
            }
            for s in range(3)
        ],
        "orders": [],
    }


def _group(n=8, gap=4_000_000):
    return [_member(f"P{i}", 30_000_000 + i * gap) for i in range(n)]


def test_the_advance_chances_sum_to_the_number_of_slots():
    """Two of eight go through, so the eight chances add to exactly two.

    This is the check that catches a broken simulation regardless of what the
    individual numbers look like. Any trial puts exactly `advance` players
    through, so the column total is fixed no matter how the odds fall.
    """
    result = odds.group_advance_odds(_group(), best_of=3, seed=1, trials=500)
    assert result.advance == 2
    assert sum(r.p_advance for r in result.rows) == pytest.approx(2.0)
    assert sum(r.p_advance_coinflip for r in result.rows) == pytest.approx(2.0)


def test_a_longer_series_does_not_hurt_the_favourite():
    """A Bo3 favours the stronger player. That is the whole point of it.

    Run off one seed so the two differ only by series length. If `best_of` ever
    stops reaching the engine, this is what notices: the numbers stay
    plausible and stop responding to the argument.
    """
    close = _group(gap=600_000)
    short = odds.group_advance_odds(close, best_of=1, seed=7, trials=1500)
    long = odds.group_advance_odds(close, best_of=3, seed=7, trials=1500)
    assert max(r.p_advance for r in long.rows) >= max(r.p_advance for r in short.rows)


def test_best_of_is_required_rather_than_defaulted():
    """The engine defaults it to 1, which is the identity and so a silent bug.

    Every layer above the engine takes it positionally or by keyword with no
    default, so a forgotten series length is a TypeError rather than a
    believable number at the wrong length.
    """
    with pytest.raises(TypeError):
        odds.group_advance_odds(_group())
    with pytest.raises(TypeError):
        predict_lib.predict_pair(object(), object())


def test_a_player_we_cannot_predict_is_named_not_dropped():
    """Six of eight is not this group, and the surface has to be able to say so."""
    group = _group()[:-1] + [
        {"display_name": "NoSquads", "server": "738", "squads": [], "orders": []}
    ]
    result = odds.group_advance_odds(group, best_of=3, seed=1, trials=200)
    assert result.skipped == ["NoSquads"]
    assert len(result.rows) == 7
    assert "NoSquads" not in {r.name for r in result.rows}


def test_a_group_we_can_barely_predict_refuses_rather_than_guessing():
    """One predictable player has nobody to meet. That is not a low number, it
    is no answer, and the two must not render the same."""
    with pytest.raises(odds.NotEnoughPlayers):
        odds.group_advance_odds(_group(n=1), best_of=3)


def test_the_two_tiebreak_rules_come_from_one_run():
    """Both orderings rank the same simulated win totals, so they bracket one
    answer rather than being two unrelated runs that sit near each other.

    With a wide power gap nothing ties, so the pair must agree exactly. If they
    were independently simulated they would drift apart by sampling noise.
    """
    result = odds.group_advance_odds(_group(gap=8_000_000), best_of=3, seed=3, trials=400)
    for row in result.rows:
        assert row.p_advance == pytest.approx(row.p_advance_coinflip, abs=0.02)


def test_a_range_is_only_reported_when_the_rules_actually_disagree():
    """Rounded to what the surface renders, so a fourth-decimal difference is
    one number and not an invented disagreement."""
    same = odds.OddsRow(name="A", p_advance=0.5001, p_advance_coinflip=0.5002)
    differ = odds.OddsRow(name="B", p_advance=0.61, p_advance_coinflip=0.55)
    assert not same.is_range
    assert differ.is_range

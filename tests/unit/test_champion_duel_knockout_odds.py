"""The knockout bracket join, and the table over it.

⚠️ MOST OF THIS FILE SKIPS ON THE CURRENT PIN. `knockout.py` arrived in engine
1.12.0 and `dev` is pinned at v1.5, so `KNOCKOUT_AVAILABLE` is False until the
pin moves. The tests that do NOT skip are the ones that matter most before it
does: that an engine without the module degrades to the two group rounds
instead of losing all three.

The expensive tests are marked. A cold bracket is about thirteen seconds — the
matrices are 496 pairs simulated at two series lengths — so the ones that need
a real run share a single module-scoped result rather than each paying for one.
"""

from __future__ import annotations

import pytest

import champion_duel_hub as hub
import champion_duel_odds as odds

TYPES = ("Tank", "Missile", "Aircraft")
#: `push_to_bot.FALLBACK_RATIOS`, so a fixture player is shaped like a real
#: unscouted registrant rather than like something only a test would produce.
RATIOS = (0.338, 0.258, 0.238)

needs_knockout = pytest.mark.skipif(
    not odds.KNOCKOUT_AVAILABLE,
    reason="the installed champion-duel-engine has no knockout model (1.12.0+)",
)


def _member(name: str, thp_m: float) -> dict:
    thp = thp_m * 1_000_000
    return {
        "display_name": name,
        "thp": thp,
        "squads": [
            {
                "slot": slot,
                "squad_type": squad_type,
                "power": round(thp * ratio),
                "source": "estimated",
            }
            for slot, (squad_type, ratio) in enumerate(zip(TYPES, RATIOS), start=1)
        ],
    }


def _field(n: int = 32) -> list[dict]:
    """A spread field, top to bottom, over the range the real event runs in."""
    return [_member(f"P{i:02d}", 480 - i * 7) for i in range(n)]


@pytest.fixture(scope="module")
def scored():
    if not odds.KNOCKOUT_AVAILABLE:
        pytest.skip("no knockout model on the installed engine")
    return odds.bracket_odds(_field())


# ── the degraded path, which is the live one until the pin moves ─────────────


def test_an_engine_without_the_bracket_keeps_the_group_round():
    """The reason `knockout` is imported on its own line.

    On one import statement an older pin raises for both names, the handler
    sets ENGINE_AVAILABLE False, and a bot that has always had semi-final odds
    loses them — reported as "the engine is not installed", which is both wrong
    and unactionable.
    """
    assert "semifinals" in odds.STAGES_WITH_A_MODEL
    if not odds.KNOCKOUT_AVAILABLE:
        assert odds.ENGINE_AVAILABLE, "the group models must survive a missing bracket"
        assert "knockouts" not in odds.STAGES_WITH_A_MODEL


def test_a_round_the_engine_cannot_model_is_never_offered():
    """`STAGES_WITH_A_MODEL` is what a surface asks before drawing the control,
    so a round missing from it is a button that is never rendered rather than
    an error a member reaches."""
    assert ("knockouts" in odds.STAGES_WITH_A_MODEL) == odds.KNOCKOUT_AVAILABLE


def test_the_bracket_is_never_reachable_through_the_group_join():
    """`_models()` is the GROUP models, and membership of it is the guarantee.

    `group_advance_odds` looks a stage up in that dict and scores whatever it
    finds, so a bracket registered there is a round it will accept with no
    `simulate_group`, no points and no group — reaching an AttributeError
    several steps past where the refusal used to be. The first version of this
    change registered it there for tidiness and did exactly that.

    Asserted unconditionally rather than against `KNOCKOUT_AVAILABLE`: this has
    to hold whether or not the pinned engine has the bracket, because the
    danger is the engine that DOES have it.
    """
    assert "knockouts" not in odds._models()
    assert set(odds._models()) == {"semifinals"}


def test_asking_for_a_bracket_without_a_model_says_which_version_added_it():
    if odds.KNOCKOUT_AVAILABLE:
        pytest.skip("the engine on this machine has the model")
    with pytest.raises(RuntimeError) as exc:
        odds.bracket_odds(_field())
    assert "1.12.0" in str(exc.value)


# ── refusals, which cost nothing to check ───────────────────────────────────


@needs_knockout
@pytest.mark.parametrize("n", [8, 16, 31, 33])
def test_a_field_that_is_not_thirty_two_is_refused(n):
    """Not absorbed. `simulate_bracket` pairs slots two at a time and a wrong
    count produces a number for a tournament nobody played."""
    with pytest.raises(odds.NotEnoughData) as exc:
        odds.bracket_odds(_field(33)[:n])
    assert not exc.value.missing_thp, "a size refusal must stay distinguishable"


@needs_knockout
def test_a_player_with_nothing_to_place_them_by_is_named():
    """The two refusals carry different copy, so they have to stay apart: one
    is fixed by recording a squad and the other by adding players."""
    field = _field()
    field[7] = {"display_name": "Ghost", "thp": None, "squads": []}
    with pytest.raises(odds.NotEnoughData) as exc:
        odds.bracket_odds(field)
    assert exc.value.missing_thp == ["Ghost"]


# ── the shape of the answer ─────────────────────────────────────────────────


@needs_knockout
def test_every_round_is_computed_not_just_the_one_rendered(scored):
    """Which round "advancing" means in a bracket is an open product question,
    and this join must not settle it by only computing one answer."""
    for row in scored.rows:
        assert set(row.reach) >= {
            "last32",
            "last16",
            "last8",
            "last4",
            "final",
            "champion",
            "third",
            "podium",
        }


@needs_knockout
@pytest.mark.parametrize(
    "round_name,seats",
    [("last32", 32), ("last16", 16), ("last8", 8), ("last4", 4), ("final", 2), ("champion", 1)],
)
def test_each_round_hands_out_exactly_its_own_number_of_places(scored, round_name, seats):
    """The identity that catches a bracket wired wrongly: however the paths
    branch, the reach probabilities across the field have to sum to the seats
    in that round. A double-advance or a dropped slot breaks it immediately."""
    assert sum(row.reach[round_name] for row in scored.rows) == pytest.approx(seats, abs=0.02)


@needs_knockout
def test_a_podium_is_three_places_and_counts_the_third_place_match(scored):
    assert sum(row.reach["podium"] for row in scored.rows) == pytest.approx(3, abs=0.02)


@needs_knockout
def test_nobody_reaches_a_later_round_more_often_than_an_earlier_one(scored):
    for row in scored.rows:
        reach = row.reach
        assert (
            reach["last32"]
            >= reach["last16"]
            >= reach["last8"]
            >= reach["last4"]
            >= reach["final"]
            >= reach["champion"]
        ), row.name


@needs_knockout
def test_neither_printed_column_ever_goes_backwards(scored):
    """Most of a 32-field shares a title chance under half a percent, so what
    orders the middle of this table is the other column — and a column that
    climbs as the eye goes down reads as a sorting bug whatever the numbers
    underneath it are doing."""
    embed = hub.build_bracket_embed(scored, None)
    rendered = [line for line in embed.description.split("\n") if line.startswith("`")]
    assert len(rendered) > 8, "the fixture should fill the table or this proves nothing"

    def columns(line):
        left, right = line.split("`")[1], line.split("`")[3]
        return tuple(
            0.0 if "<" in cell else float(cell.strip().rstrip("%")) for cell in (left, right)
        )

    pairs = [columns(line) for line in rendered]
    assert [p[1] for p in pairs] == sorted((p[1] for p in pairs), reverse=True)
    for before, after in zip(pairs, pairs[1:]):
        if before[1] == after[1]:
            assert before[0] >= after[0], (before, after)


@needs_knockout
def test_the_join_ranks_on_the_title_and_cascades_out_from_there(scored):
    """The canonical order, kept apart from the table's. A caller reading every
    round wants one defensible ranking; the table wants the columns it prints.
    Ranking on the title is the neutral choice — it is the one thing every
    player in a bracket is unambiguously playing for, so it does not quietly
    answer "which round does advancing mean"."""
    titles = [row.champion for row in scored.rows]
    assert titles == sorted(titles, reverse=True)


@needs_knockout
def test_a_second_press_does_not_pay_for_the_matrices_again(scored):
    """Thirteen seconds of pure Python holds the GIL, so the bot serves nobody
    while it runs. The cache makes that a cost per data change rather than per
    press."""
    import time

    started = time.perf_counter()
    again = odds.bracket_odds(_field())
    assert time.perf_counter() - started < 1.0
    assert again is scored


# ── the table ───────────────────────────────────────────────────────────────


@needs_knockout
def test_no_player_is_told_the_title_is_out_of_reach(scored):
    """`0%` in a field of thirty-two is not an edge case, it is most of the
    table, and it makes a claim the simulation did not."""
    description = hub.build_bracket_embed(scored, None).description
    assert "0%" not in description.replace("10%", "").replace("20%", "").replace("30%", "")
    assert "<1%" in description


@needs_knockout
def test_the_table_says_the_draw_is_not_the_one_that_will_happen(scored):
    """The single most misreadable thing on this surface: with no published
    draw the model reshuffles every trial, so these are odds across the
    brackets that could happen. A reader taking them for the real draw will be
    badly wrong about one specific player."""
    footer = hub.build_bracket_embed(scored, None).footer.text
    assert "redraws it" in footer or "redraw" in footer


@needs_knockout
def test_the_table_names_the_rounds_as_reaching_them_never_as_going_out(scored):
    """Thirty of the thirty-two are eliminated somewhere, and a surface naming
    each exit is a scoreboard nobody asked for."""
    embed = hub.build_bracket_embed(scored, None)
    text = f"{embed.description} {embed.footer.text}".lower()
    for word in ("knocked out", "eliminated", "goes out", "loses in"):
        assert word not in text


@needs_knockout
def test_the_bracket_goes_through_its_own_builder_not_the_group_one(scored):
    """A group embed would print "the chance of finishing in the top 2 and
    going through" over a field of 32, which is a sentence about a round that
    is not being played."""
    embed = hub.build_odds_embed(_field(), "knockouts", None, None)
    assert "top" not in (embed.description or "").lower().split("second")[0]
    assert "bracket" in (embed.description or "").lower()

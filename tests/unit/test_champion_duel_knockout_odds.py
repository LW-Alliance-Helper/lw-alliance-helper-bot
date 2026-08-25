"""The knockout bracket join, and the table over it.

⚠️ MOST OF THIS FILE SKIPS ON THE CURRENT PIN. `knockout.py` arrived in engine
1.12.0 and `dev` is pinned at v1.5, so `KNOCKOUT_AVAILABLE` is False until the
pin moves. The tests that do NOT skip are the ones that matter most before it
does: that an engine without the module degrades to the two group rounds
instead of losing all three.

The expensive tests are marked. A cold bracket is a minute and a bit at the
shipped `MATRIX_TRIALS` of 250 — the matrices are 496 pairs simulated at two
series lengths — so the ones that need a real run share a single module-scoped
result rather than each paying for one. That is past `pytest.ini`'s 30-second
default, which is what `bracket_test` is for.
"""

from __future__ import annotations

import re

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


def bracket_test(func):
    """Skip without the model, and allow for the one cold run.

    `pytest.ini` sets a 30-second default and the cold bracket is about sixty,
    so a test that pays for the `scored` fixture needs the allowance — fixture
    setup counts against the timeout of whichever test triggers it.

    ON EVERY CONSUMER, NOT ON THE ONE THAT PAYS TODAY. `scored` is
    module-scoped, so exactly one of these builds it and the rest get it free;
    which one that is depends on the order they sit in the file, and a test
    reordered into first place should not start timing out for it.

    The refusals below are deliberately NOT decorated with this: they raise on
    field size or missing power before anything is simulated, and they are the
    tests that would stop being cheap without anyone noticing.
    """
    return needs_knockout(pytest.mark.timeout(240)(func))


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


@bracket_test
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


@bracket_test
@pytest.mark.parametrize(
    "round_name,seats",
    [("last32", 32), ("last16", 16), ("last8", 8), ("last4", 4), ("final", 2), ("champion", 1)],
)
def test_each_round_hands_out_exactly_its_own_number_of_places(scored, round_name, seats):
    """The identity that catches a bracket wired wrongly: however the paths
    branch, the reach probabilities across the field have to sum to the seats
    in that round. A double-advance or a dropped slot breaks it immediately."""
    assert sum(row.reach[round_name] for row in scored.rows) == pytest.approx(seats, abs=0.02)


@bracket_test
def test_a_podium_is_three_places_and_counts_the_third_place_match(scored):
    assert sum(row.reach["podium"] for row in scored.rows) == pytest.approx(3, abs=0.02)


@bracket_test
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


@bracket_test
def test_no_printed_rung_ever_goes_backwards(scored):
    """Most of a 32-field shares a title chance under half a percent, so what
    orders the middle of this list is the rungs beneath it — and a figure that
    climbs as the eye goes down reads as a sorting bug whatever the numbers
    underneath it are doing.

    The guard is that every rung on screen is in the sort key, most significant
    first, and that the key is the figure as PRINTED. Read the ladder back to
    front and the list has to come out in descending order: no rung may climb
    unless a more significant one visibly fell first. Nothing the reader cannot
    see is allowed to order this list.

    Sorting on the raw floats fails this. `probability()` floors a long tail
    into `<1%`, so most of a thirty-two field ties on screen at the title while
    differing in the fourth decimal, and Top 4 then climbs under two rungs the
    reader can see are equal."""
    ladders = _ladders(hub.build_bracket_embed(scored, None))
    assert len(ladders) > 8, "the fixture should fill the list or this proves nothing"
    keys = [tuple(reversed(ladder)) for ladder in ladders]
    assert keys == sorted(keys, reverse=True)


@bracket_test
def test_the_order_is_the_printed_figure_and_cannot_drift_from_it(scored):
    """`_printed_rank` is what keeps the visible order the real one, and it is
    read off `probability()` rather than repeating its thresholds — so a change
    to where the `<1%` floor or the `>99%` ceiling sits moves both together."""
    for prob in (0.0, 0.0049, 0.005, 0.0051, 0.02, 0.125, 0.5, 0.9949, 0.995, 1.0):
        text = hub.words.probability(prob)
        rank = hub._printed_rank(prob)
        assert (rank == 0.0) == (text == "<1%"), (prob, text, rank)
        assert (rank == 100.0) == (text == ">99%"), (prob, text, rank)
        if text not in ("<1%", ">99%"):
            assert f"{rank:.0f}%" == text, (prob, text, rank)


@bracket_test
def test_every_rung_kevin_picked_is_printed_with_its_label(scored):
    """Four rungs, not the placeholder two and not the five that shipped for a
    day, and each label travels with its own number — which is the whole reason
    a wrap is harmless here."""
    embed = hub.build_bracket_embed(scored, None)
    ladder = [ln for ln in embed.description.split("\n") if " · " in ln][0]
    assert [cell.rsplit(" ", 1)[0] for cell in ladder.split(" · ")] == list(
        hub.BRACKET_RUNGS.values()
    )
    assert list(hub.BRACKET_RUNGS) == ["last16", "last8", "last4", "champion"]


@bracket_test
def test_the_podium_is_computed_and_deliberately_not_printed(scored):
    """`podium` came off the table on 2026-08-23 and stayed in the join.

    It duplicated its neighbours in both directions: within 2 to 6 points of
    Top 4 at the head of the table, and `<1%` beside an already-`<1%` Champion
    from the thirteenth row down. That is a display call and nothing more, so
    the figure is still computed — a later surface asking "who finishes on the
    podium" must not find the answer thrown away to save a column."""
    assert "podium" not in hub.BRACKET_RUNGS
    assert all("podium" in row.reach for row in scored.rows)
    assert "Top 3" not in hub.build_bracket_embed(scored, None).description


@bracket_test
def test_the_whole_field_fits_the_description_cap(scored):
    """Thirty-two players at two lines each, against Discord's 4,096. It fits
    with room to spare, and a member who cannot find their own name on the
    surface built for finding it has been given nothing."""
    embed = hub.build_bracket_embed(scored, None)
    assert len(embed.description) <= 4096
    assert len(_ladders(embed)) == len(scored.rows)
    assert "below them" not in embed.description


@bracket_test
def test_a_field_too_wide_to_fit_is_counted_rather_than_cut_silently(scored):
    """The guard behind the line above. Names are member-supplied and an
    over-long description is truncated by Discord mid-figure rather than by us,
    so rows come off the bottom until it fits and the count is carried."""
    import dataclasses

    wide = dataclasses.replace(
        scored, rows=[dataclasses.replace(r, name="W" * 200) for r in scored.rows]
    )
    embed = hub.build_bracket_embed(wide, None)
    assert len(embed.description) <= 4096
    dropped = len(wide.rows) - len(_ladders(embed))
    assert dropped > 0
    assert f"and **{dropped} players** below them." in embed.description


@bracket_test
def test_the_join_ranks_on_the_title_and_cascades_out_from_there(scored):
    """The canonical order, kept apart from the table's. A caller reading every
    round wants one defensible ranking; the table wants the columns it prints.
    Ranking on the title is the neutral choice — it is the one thing every
    player in a bracket is unambiguously playing for, so it does not quietly
    answer "which round does advancing mean"."""
    titles = [row.champion for row in scored.rows]
    assert titles == sorted(titles, reverse=True)


@bracket_test
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


def _ladders(embed):
    """Every rendered ladder, as tuples of numbers in printed order.

    `probability()` prints two words rather than figures at the ends of the
    scale, and both carry an order: `<1%` is below every printed figure and
    `>99%` is above every one, so they map inside their own bands rather than
    to 0 and 100.
    """
    out = []
    for line in embed.description.split("\n"):
        if " · " not in line:
            continue
        figures = []
        for cell in line.split(" · "):
            text = cell.rsplit(" ", 1)[1]
            figures.append(
                0.5 if text == "<1%" else 99.5 if text == ">99%" else float(text.rstrip("%"))
            )
        out.append(tuple(figures))
    return out


@bracket_test
def test_no_player_is_told_a_rung_is_out_of_reach(scored):
    """`0%` in a field of thirty-two is not an edge case, it is most of what
    is printed once the ladder goes five rungs deep, and it makes a claim the
    simulation did not.

    Matched on a word boundary rather than by stripping the known figures out
    first: every one of "40%" through "90%" ends in `0%`, so the old
    subtract-the-cases spelling would have started passing by accident the
    moment a third rung was printed."""
    description = hub.build_bracket_embed(scored, None).description
    assert re.search(r"\b0%", description) is None, description
    assert "<1%" in description


@bracket_test
def test_the_table_says_the_seeding_is_not_the_one_that_will_happen(scored):
    """The single most misreadable thing on this surface: with no published
    seeding the model reshuffles every trial, so these are odds across the
    brackets that could happen. A reader taking them for the real bracket will
    be badly wrong about one specific player."""
    footer = hub.build_bracket_embed(scored, None).footer.text
    assert "Seeding isn't set yet" in footer
    assert "a different bracket" in footer
    assert f"{scored.trials:,}" in footer


@bracket_test
def test_the_bracket_surface_says_seeding_and_never_draw(scored):
    """`_RECORDING_LABELS` calls it **Initial Seed** where a member records
    one, so a footer calling the same thing "the draw" gives the bot two words
    for one object — which is how somebody ends up believing they are two
    objects."""
    embed = hub.build_bracket_embed(scored, None)
    text = f"{embed.description} {embed.footer.text}".lower()
    assert "draw" not in text
    assert hub._RECORDING_LABELS["draw"] == "Initial Seed"


@bracket_test
def test_the_explainer_says_what_the_surface_is_and_says_odds(scored):
    """ "What are these? Where did these come from?" — the copy never said this
    was a 32-player knockout bracket, so "Top 8" had nothing to attach to.

    And it takes the same shape as the group explainer one button away, which
    is "gives the odds of": people move between the two, and two surfaces
    describing the same kind of number two different ways is a difference a
    reader will try to find meaning in."""
    embed = hub.build_bracket_embed(scored, None)
    description = embed.description
    assert "32 players, single elimination" in description
    assert "gives the odds of" in description
    text = f"{description} {embed.footer.text}"
    assert "chance" not in text.lower()
    assert "—" not in text


@bracket_test
def test_the_table_names_the_rounds_as_reaching_them_never_as_going_out(scored):
    """Thirty of the thirty-two are eliminated somewhere, and a surface naming
    each exit is a scoreboard nobody asked for."""
    embed = hub.build_bracket_embed(scored, None)
    text = f"{embed.description} {embed.footer.text}".lower()
    for word in ("knocked out", "eliminated", "goes out", "loses in"):
        assert word not in text


@bracket_test
def test_the_bracket_goes_through_its_own_builder_not_the_group_one(scored):
    """A group embed would print "the odds of finishing in the top 2 and going
    through" over a field of 32, which is a sentence about a round that is not
    being played."""
    description = hub.build_odds_embed(_field(), "knockouts", None, None).description or ""
    assert description.startswith("The knockout bracket:")
    assert "going through" not in description
    assert "winning the group" not in description


# ── the stale caveat over a stored bracket ───────────────────────────────────
#
# No engine, deliberately. What is under test is the FITTING, not the model, so
# the rows are built by hand and this runs on any pin.


#: The real line, at the length it actually renders. Built from the constant
#: rather than copied, so it stays honest when Kevin settles the wording.
_AS_OF = hub._ODDS_AS_OF.format(when="<t:1787040000:R>")


def _wide_bracket(name_length: int) -> odds.BracketOdds:
    """A thirty-two field whose names are as long as the caller wants.

    A real field renders at about 2,200 characters against the 4,096 cap, so
    nothing about the fitting loop is exercised by one -- which is exactly how
    a caveat prepended to a finished description would pass every test in this
    file and then truncate a live bracket the first time somebody's alliance
    tag ran long.
    """
    return odds.BracketOdds(
        rows=[
            odds.BracketRow(
                name=f"P{i:02d}" + "x" * max(0, name_length - 3),
                reach={rung: 0.5 for rung in hub.BRACKET_RUNGS},
            )
            for i in range(32)
        ],
        trials=200_000,
        matrix_trials=250,
    )


def _bracket_the_caveat_tips_over() -> odds.BracketOdds:
    """A 32-field sized so all of it fits WITHOUT the caveat and not WITH it.

    Sized by search rather than by a number in the test, because the number
    depends on the length of copy nobody has signed off yet. Outside that
    window the test would pass against a prepend-and-truncate as well, which
    is the thing it exists to rule out.
    """
    for length in range(40, 140):
        field = _wide_bracket(length)
        plain = hub.build_bracket_embed(field, None).description
        if "below them" in plain:
            break
        if len(plain) + len(_AS_OF) + 2 > 4096:
            return field
    raise AssertionError(
        "no name length puts a 32-field in the window where the caveat is what "
        "tips it over the cap; the fitting loop or the rungs must have changed"
    )


def test_every_one_of_the_32_is_accounted_for_under_the_stale_line():
    """The caveat is counted before rows are dropped, not written over them.

    A bracket is read for one thing -- finding your own name in a field of
    thirty-two -- so a row that is neither printed nor counted is the failure
    that matters. `build_bracket_embed` drops whole rows off the bottom until
    the description fits and says how many went; a line prepended to the
    finished string instead would push it past 4,096 and Discord would take the
    tail, which is the sentence saying anybody was left out at all.
    """
    field = _bracket_the_caveat_tips_over()

    description = hub.build_bracket_embed(field, None, as_of=_AS_OF).description

    assert description.startswith(_AS_OF)
    assert len(description) <= 4096
    printed = sum(1 for row in field.rows if f"**{row.name}**" in description)
    # Both halves of a row, not just the name. A truncation lands mid-ladder
    # and leaves the name above it standing, so counting names alone would call
    # a player present who has no figures under them.
    ladders = len(re.findall(r"Top 16 \d+% . Top 8 \d+% . Top 4 \d+% . Champion \d+%", description))
    assert ladders == printed, (
        f"{printed} names are printed and {ladders} of them have a full ladder; "
        "somebody's figures were cut in half"
    )
    counted = re.search(r"and \*\*(\d+) players?\*\* below them\.", description)
    assert printed + (int(counted.group(1)) if counted else 0) == 32, (
        "the table lost players: they were neither printed nor counted, which "
        "means the description was truncated rather than fitted"
    )


def test_a_bracket_with_nothing_to_caveat_reads_exactly_as_it_did():
    """The common case is a fresh answer, and it must be untouched."""
    field = _wide_bracket(12)
    assert hub.build_bracket_embed(field, None, as_of=None).description == (
        hub.build_bracket_embed(field, None).description
    )

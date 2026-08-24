"""The intel button and the embed behind it.

Split from `test_champion_duel_intel.py`, which tests the join. This file tests
what a member is shown, and the tests that matter most are the ones asserting
what is *not* shown: the surface's job is to stop talking where the numbers stop
meaning anything, and every one of those stops is a place a later change could
quietly start talking again.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest

import champion_duel_db as db
import champion_duel_hub as hub
import champion_duel_intel as intel_lib
import champion_duel_wording as words
import premium

KEV = {"discord_user_id": "111", "discord_name": "Kevin", "guild_id": "999"}
TYPES = ("Tank", "Missile", "Aircraft")
#: `push_to_bot.FALLBACK_RATIOS` — squads are derived from THP for the ~97% of
#: the field with no sighting, so a fixture that sets powers independently of
#: THP would grade the power gap off one column and the grid off another.
RATIOS = (0.338, 0.258, 0.238)


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    db.import_registrants(
        [
            {"name": "Habitual", "group": "M", "rank": 1, "server": "738", "thp": 266_000_000},
            {"name": "Asker", "group": "M", "rank": 2, "server": "738", "thp": 262_000_000},
            {"name": "Unseen", "group": "M", "rank": 3, "server": "738", "thp": 264_000_000},
            {"name": "Giant", "group": "M", "rank": 4, "server": "738", "thp": 480_000_000},
            {"name": "Mixed", "group": "M", "rank": 5, "server": "738", "thp": 265_000_000},
            {"name": "Switcher", "group": "M", "rank": 6, "server": "738", "thp": 261_000_000},
        ],
        stage="qualifiers",
    )
    for name, source in (
        ("Habitual", "observed"),
        ("Asker", "observed"),
        ("Unseen", "estimated"),
        ("Giant", "observed"),
        ("Mixed", "estimated"),
        ("Switcher", "observed"),
    ):
        _squads(name, source)
    # One of Mixed's three squad types is a sighting and the other two are the
    # placeholder `push_to_bot` writes. That halves the arrangements they could
    # be fielding rather than leaving the space whole, which is the live case a
    # member creates by filling in one box through the hub.
    db.set_squad(
        _rid("Mixed"), 1, squad_type="Missile", power=round(265_000_000 * RATIOS[0]), actor=KEV
    )
    return db


def _rid(name):
    return db.resolve_registrant(name, server="738")["id"]


def _squads(name, source):
    thp = db.resolve_registrant(name, server="738")["thp"]
    for slot, (squad_type, ratio) in enumerate(zip(TYPES, RATIOS), start=1):
        db.set_squad(
            _rid(name),
            slot,
            squad_type=squad_type,
            power=round(thp * ratio),
            actor=KEV,
            source=source,
        )


def _habit(name="Habitual", order=("Missile", "Tank", "Aircraft"), meetings=3):
    for i in range(meetings):
        for _ in range(2):
            db.add_order(
                _rid(name),
                list(order),
                actor=KEV,
                opponent=f"opp{i}",
                observed_at=f"2026-08-1{i}",
            )


def _moves_around(name="Switcher"):
    """Six meetings, six different orders — a `none` read past `LEAN_SEEN`.

    A player with no orders at all renders `NOTHING_SEEN` and never reaches
    `READ_COPY["none"]`, so a test that means to cover the moves-around state
    has to seed one rather than name an unscouted fixture.
    """
    orders = (
        ("Missile", "Tank", "Aircraft"),
        ("Tank", "Aircraft", "Missile"),
        ("Aircraft", "Missile", "Tank"),
        ("Tank", "Missile", "Aircraft"),
        ("Aircraft", "Tank", "Missile"),
        ("Missile", "Aircraft", "Tank"),
    )
    for i, order in enumerate(orders):
        db.add_order(
            _rid(name), list(order), actor=KEV, opponent=f"opp{i}", observed_at=f"2026-08-1{i}"
        )


def _player(name):
    return db.get_player(name, server="738", include_scouting=True)


def _embed(them, you="Asker"):
    """Both sides, always. `you` defaults to a player rather than to `None`:
    the surface has no one-name shape any more, and a helper that could still
    build one would be the only place in the suite that could."""
    return hub.build_intel_embed(intel_lib.intel(_player(them), _player(you)))


def _field(embed, name):
    for field in embed.fields:
        if field.name == name:
            return field.value
    return None


# ── the button ───────────────────────────────────────────────────────────────


def test_the_intel_button_locks_rather_than_vanishes_on_the_free_tier():
    """Premium renders disabled so a free alliance sees the shape of the paid
    product. This one is hard to describe and easy to show, so hiding it would
    cost more than most."""
    view = hub.ChampionDuelHubView(
        user_id=1, is_admin=False, can_write=True, engine_ok=True, can_intel=False
    )
    locked = [b for b in view.children if hub.CD_BTN_INTEL in (b.label or "")]
    assert locked, "the intel button should still be on the grid"
    assert locked[0].disabled
    assert locked[0].label.startswith("🔒")


def test_a_paying_alliance_gets_it_unlocked():
    view = hub.ChampionDuelHubView(
        user_id=1, is_admin=False, can_write=True, engine_ok=True, can_intel=True
    )
    live = [b for b in view.children if b.label == hub.CD_BTN_INTEL]
    assert live and not live[0].disabled


def test_the_gate_defaults_closed():
    """A caller that forgets the flag renders the padlock rather than handing
    out the paid surface."""
    view = hub.ChampionDuelHubView(user_id=1, is_admin=False, can_write=True, engine_ok=True)
    locked = [b for b in view.children if hub.CD_BTN_INTEL in (b.label or "")]
    assert locked[0].disabled


def test_the_intel_button_shares_no_glyph_with_anything_beside_it():
    """`DESIGN.md` emoji rule: never repeat one glyph across a choice set, and
    two identical glyphs side by side give the eye nothing to navigate by."""
    view = hub.ChampionDuelHubView(
        user_id=1, is_admin=True, can_write=True, engine_ok=True, can_intel=True
    )
    glyphs = [(b.label or "").replace("🔒 ", "").split(" ", 1)[0] for b in view.children if b.label]
    assert len(glyphs) == len(set(glyphs)), glyphs


# ── the embed ────────────────────────────────────────────────────────────────


def test_a_strong_read_leads_with_the_counter_and_names_the_risk(cd_db):
    _habit()
    embed = _embed("Habitual", "Asker")

    what_to_set = _field(embed, hub.FIELD_YOURS)
    assert "Tank → Aircraft → Missile" in what_to_set
    assert "counters the line-up they show most often" in what_to_set
    # The whole risk statement, and it is most worth making when the read is
    # best: a strong read prices the advice against one line-up they might not
    # hold.
    assert "Their best answer to it" in what_to_set


def test_a_measured_zero_change_rate_is_stated_rather_than_dropped(cd_db):
    """The strongest thing that figure can say — watched across three meetings
    and never moved — and a falsy check would drop exactly that case."""
    _habit()
    field = _field(_embed("Habitual"), hub.FIELD_THEIRS)
    assert "never been seen changing it" in field
    # The denominator is named. Every meeting on file here has more than one
    # line-up recorded from it, so the short form is exact; where they differ
    # the copy says "we can only tell for 3 of their 6 meetings" instead. A
    # meeting sighted once could not have shown a change, so folding it in
    # would be "we never looked" printed as "they never changed".
    assert "our 3 recorded meetings" in field


def test_a_meeting_sighted_once_cannot_say_they_never_change(cd_db):
    """The overclaim the denominator fix exists to stop. Six meetings, one
    line-up recorded from each, all the same order: nothing in that record ever
    watched a change happen, so it cannot report that none did."""
    for i in range(6):
        db.add_order(
            _rid("Habitual"),
            ["Missile", "Tank", "Aircraft"],
            actor=KEV,
            opponent=f"opp{i}",
            observed_at=f"2026-08-1{i}",
        )
    field = _field(_embed("Habitual"), hub.FIELD_THEIRS)
    assert "never moved squads around" not in field
    assert "is unknown" in field
    # And it cannot earn the strongest read on the strength of it.
    assert words.READ_COPY["strong"] not in field


def test_nobody_watching_reads_differently_from_them_moving_around(cd_db):
    """Two different findings. One is about the player and one is about us, and
    only the second has something the reader can do about it."""
    seen_nothing = _field(_embed("Unseen", "Asker"), hub.FIELD_THEIRS)
    assert seen_nothing == words.NOTHING_SEEN.format(button=hub._btn_words(hub.CD_BTN_ORDER))
    assert "Anyone who has faced them can add one" in seen_nothing


def test_where_the_deployment_decides_nothing_the_surface_says_so_and_stops(cd_db):
    """Six rows of "<1%" under a heading saying the choice does not matter
    reads as a broken surface rather than as a finding."""
    embed = _embed("Giant", "Asker")

    assert "Set whatever you normally would" in _field(embed, hub.FIELD_YOURS)
    assert _field(embed, hub.FIELD_OTHERS) is None
    assert _field(embed, hub.FIELD_WORTH) is None


def test_it_never_tells_someone_to_record_squads_that_would_not_help(cd_db):
    """`needs_your_squads` is true here and acting on it would not change this
    answer, so the promise that it would must not be made.

    What replaces it is a different sentence, not silence. Kevin, on review:
    recording squads is worth nothing in the matchup you are doing now and it
    is still worth collecting for other rounds and the next Champion Duel. The
    old surface suppressed the ask entirely, which optimised for the answer on
    screen and threw the contribution away."""
    db.import_registrants(
        [{"name": "Nobody", "group": "M", "rank": 5, "server": "738", "thp": 262_000_000}],
        stage="qualifiers",
    )
    _squads("Nobody", "estimated")

    embed = _embed("Giant", "Nobody")
    what_to_set = _field(embed, hub.FIELD_YOURS)
    assert "Set whatever you normally would" in what_to_set
    # The false promise, which is what the name of this test is about.
    assert "becomes a recommendation" not in what_to_set

    anyway = _field(embed, hub.FIELD_ANYWAY)
    assert anyway is not None, "the collection ask is still worth making here"
    assert "won't change this answer" in anyway
    assert hub.CHAMPION_DUEL_HUB_CMD in anyway, "an ask with no exit is not an ask"


def test_your_own_placeholder_types_ask_for_squads_rather_than_ranking_nothing(cd_db):
    _habit()
    embed = _embed("Habitual", "Unseen")

    what_to_set = _field(embed, hub.FIELD_YOURS)
    assert "We don't have your squad types" in what_to_set
    # Every dead end carries its exit, and this one is a press away.
    assert hub.CHAMPION_DUEL_HUB_CMD in what_to_set
    assert _field(embed, hub.FIELD_OTHERS) is None
    # the envelope still answers "how much is this decision worth"
    assert _field(embed, hub.FIELD_WORTH) is not None


def test_an_unscouted_opponent_gets_a_refusal_rather_than_a_ranking(cd_db):
    """Kevin, on review: rather than give a false recommendation, be honest
    about what we can give them and give them a way to refine it.

    With nothing recorded about the opponent every arrangement they could field
    is averaged, and the six orders come back within a few points of each
    other. The top one is real and it is not advice."""
    embed = _embed("Unseen", "Asker")

    what_to_set = _field(embed, hub.FIELD_YOURS)
    assert "no recommendation to give" in what_to_set
    # Carrying the measurement, because "we cannot tell you" is a claim the
    # reader is entitled to check.
    assert "points" in what_to_set
    # And why, which only holds where their types really are missing.
    assert words.CANNOT_RECOMMEND_WHY in what_to_set
    # Six rows within three points of each other is the same broken-looking
    # surface that six rows of <1% was.
    assert _field(embed, hub.FIELD_OTHERS) is None
    # And it does not go quiet: the ask names the press that fixes it.
    fix = _field(embed, hub.FIELD_FIX)
    assert fix is not None and hub.CHAMPION_DUEL_HUB_CMD in fix
    assert "Anyone who has seen their line-up screen can record it" in fix


def test_the_refusal_does_not_repeat_what_the_section_above_it_just_said(cd_db):
    """`NOTHING_SEEN` already says nobody has recorded an order and already
    names the press. An embed that says it twice is not listening to itself."""
    embed = _embed("Unseen", "Asker")
    rendered = " ".join(f.value for f in embed.fields)
    assert rendered.count("Anyone who has faced them can add one") == 1
    assert rendered.count(hub.CHAMPION_DUEL_HUB_CMD) == 1


def test_a_recorded_opponent_still_gets_a_recommendation(cd_db):
    """The refusal is graded on the measured spread, not on "did we scout
    them", so it must not swallow the cases that do have an answer."""
    _habit()
    what_to_set = _field(_embed("Habitual", "Asker"), hub.FIELD_YOURS)
    assert "no recommendation to give" not in what_to_set
    assert "Tank → Aircraft → Missile" in what_to_set


def test_the_counter_order_is_computed_and_deliberately_not_rendered_here(cd_db):
    """The gap the one-name removal left, pinned so it stays a decision.

    `counter_types` needs nothing about you, so this state — your own types
    unknown — is the one place left where it has something unsaid. It is held
    back because printing it over "every line-up you could set looks the same
    from here" contradicts that sentence on screen, and reconciling the two
    needs copy that is Kevin's to write.

    Asserting on both halves so that restoring it is a deliberate edit to this
    test rather than a silent change in what a member is shown.
    """
    _habit()
    result = intel_lib.intel(_player("Habitual"), _player("Unseen"))
    assert result.counter_types == ("Tank", "Aircraft", "Missile")

    field = _field(hub.build_intel_embed(result), hub.FIELD_YOURS)
    assert "We don't have your squad types" in field
    assert "Tank → Aircraft → Missile" not in field


def test_the_lead_is_always_there_now_that_both_sides_are(cd_db):
    """The description used to be able to come out empty, because a one-name
    answer had no grid and so no grade to lead with. With both sides required
    there is always a grid, so `worth` is always a grade and every grade has a
    sentence — which is what makes the `or None` fallback under it dead."""
    _habit()
    embed = _embed("Habitual", "Asker")

    assert embed.description
    assert any(
        embed.description.startswith(line) or line in embed.description
        for line in words.WORTH_COPY.values()
    )


def test_no_probability_is_ever_rounded_into_a_certainty(cd_db):
    """Every number on this surface goes through `probability()`, which is what
    keeps a 0.4% from rendering as a match that cannot be won."""
    _habit()
    for them, you in (("Habitual", "Asker"), ("Unseen", "Asker"), ("Giant", "Asker")):
        embed = _embed(them, you)
        rendered = " ".join([embed.description or ""] + [f.value for f in embed.fields])
        percentages = re.findall(r"\d+(?:\.\d+)?%", rendered)
        assert percentages, f"{them} vs {you} rendered no numbers at all"
        # A bare 0% claims the match cannot be won and a bare 100% that it
        # cannot be lost. The power gap in the description is a measurement
        # rather than a probability, so it is excluded by name.
        gap = re.findall(r"gap \*\*(\d+(?:\.\d+)?%)\*\*", rendered)
        assert not ({"0%", "100%"} & (set(percentages) - set(gap)))


def test_the_footer_never_claims_a_line_up_nobody_recorded(cd_db):
    """ "We have seen their line-up" over "nobody has recorded their order" is
    the surface contradicting itself, which is why the footer says types."""
    embed = _embed("Unseen", "Asker")
    assert embed.footer.text == words.INTEL_BASIS["unknown"]
    # The whole point of the row: it speaks about squad types, never about a
    # line-up. A footer claiming we have seen their line-up, above a section
    # saying none is recorded, is the surface contradicting itself.
    assert "line-up" not in embed.footer.text


def test_a_favourite_nobody_has_watched_twice_is_not_a_favourite_they_move_off(cd_db):
    """Two ways to reach a `lean` read and they are different findings. One is
    about the player, one is about us, and only the second is fixable."""
    for i in range(6):
        db.add_order(
            _rid("Habitual"),
            ["Missile", "Tank", "Aircraft"],
            actor=KEV,
            opponent=f"opp{i}",
            observed_at=f"2026-08-1{i}",
        )
    field = _field(_embed("Habitual"), hub.FIELD_THEIRS)
    # The difference is a measurement now, not an adjective: the surface says
    # the rate cannot be computed rather than asserting they move off it.
    assert "Whether they change it inside a meeting is unknown" in field
    assert "never been seen changing it" not in field
    assert words.READ_COPY["lean"] in field


def test_one_sighting_is_not_reported_as_a_player_who_changes_it_often(cd_db):
    """`grade_read` returns `none` for two reasons and the copy speaks to one.

    Under `LEAN_SEEN` the grade means "nobody has watched them enough to tell",
    and `READ_COPY["none"]` says "they change it often", which is a claim about
    the player. Off a single sighting the field read *"The only line-up on
    record for this player. They change it often."* — the second sentence
    contradicting the first.

    Kevin, 2026-08-23: print nothing. Not a hedged verdict, which would still
    be read as a verdict. The line-up and what the record holds, and stop."""
    db.add_order(
        _rid("Habitual"),
        ["Missile", "Tank", "Aircraft"],
        actor=KEV,
        opponent="opp0",
        observed_at="2026-08-10",
    )
    result = intel_lib.intel(_player("Habitual"), _player("Asker"))
    assert result.read == intel_lib.NONE
    field = _field(hub.build_intel_embed(result), hub.FIELD_THEIRS)
    # What we hold, still said.
    assert "The only line-up on record for this player." in field
    assert "Missile → Tank → Aircraft" in field
    # And no verdict on top of it, in any wording.
    assert words.READ_COPY["none"] not in field
    assert "change it often" not in field


def test_a_player_who_really_does_move_around_is_still_told_about(cd_db):
    """The other half of the same branch, and the reason the fix is at the
    render site rather than in `grade_read`. Past `LEAN_SEEN` a `none` read is
    a finding — no repeat worth countering — and suppressing it there would
    throw away the useful half to fix the false one."""
    _moves_around("Switcher")
    result = intel_lib.intel(_player("Switcher"), _player("Asker"))
    assert result.read == intel_lib.NONE
    assert result.habit.total >= intel_lib.LEAN_SEEN
    assert words.READ_COPY["none"] in _field(hub.build_intel_embed(result), hub.FIELD_THEIRS)


def test_the_refusal_reads_as_english_under_a_point(cd_db):
    """`points()` floors at "under a point" rather than rounding a spread to
    zero, which the old frame rendered as "came out within under a point of
    each other". The frame bends to the formatter; the floor stays."""
    for spread in (0.004, 0.031):
        rendered = words.CANNOT_RECOMMEND_FLAT.format(measured=words.points(spread))
        assert "within under" not in rendered
        assert "of each other" not in rendered
    assert "came out under a point apart" in words.CANNOT_RECOMMEND_FLAT.format(
        measured=words.points(0.004)
    )
    assert "came out about 3 points apart" in words.CANNOT_RECOMMEND_FLAT.format(
        measured=words.points(0.031)
    )


def test_a_spread_of_one_point_is_one_point_and_not_one_points():
    """The seam the reworded frame put a spotlight on. Anything in [1.0, 1.5)
    rounds to one, and the count was pluralised unconditionally — so the
    sentence the reword exists to make read correctly rendered "came out about
    1 points apart" across a live band. The refusal only fires below a spread
    of `CHOICE_SPREAD`, ten points, so one point is well inside it.

    The floor is untouched: `points()` still refuses to round a spread to zero,
    which is a different guard and the reason this function exists."""
    assert words.points(0.0099) == "under a point"
    for spread in (0.010, 0.012, 0.0149):
        assert words.points(spread) == "about 1 point", spread
    for spread in (0.015, 0.021):
        assert words.points(spread) == "about 2 points", spread
    # And in both sentences it feeds, not just the one that was reworded.
    assert "moves by about 1 point." in words.order_barely_matters(0.012)
    assert "came out about 1 point apart" in words.CANNOT_RECOMMEND_FLAT.format(
        measured=words.points(0.012)
    )


def test_no_share_is_ever_rendered_as_always_or_never(cd_db):
    """`probability()` refuses to round a probability into a certainty. The
    other kind of number on this surface is a share of what has been recorded,
    and "100% of the time" off six sightings is heard as a claim about the
    player rather than about the record."""
    _habit()
    for i in range(3):
        db.add_order(
            _rid("Switcher"),
            ["Missile", "Tank", "Aircraft"],
            actor=KEV,
            opponent=f"opp{i}",
            observed_at=f"2026-08-1{i}",
        )
        db.add_order(
            _rid("Switcher"),
            ["Tank", "Missile", "Aircraft"],
            actor=KEV,
            opponent=f"opp{i}",
            observed_at=f"2026-08-1{i}",
        )
    for name in ("Habitual", "Switcher"):
        field = _field(_embed(name), hub.FIELD_THEIRS)
        # Whole tokens: "50%" contains "0%" and is a perfectly good rate.
        assert not ({"0%", "100%"} & set(re.findall(r"\d+%", field))), field


def test_this_surface_carries_no_em_dashes(cd_db):
    """`UX.md`: if a user can see it, no em dashes. This block of
    `champion_duel_wording.py` is embed copy and the rule reaches it. The card
    strings above the draft marker are workshopped and are the exception, which
    is why this asserts on the rendered embed rather than on the module."""
    _habit()
    # Seeded, not merely named. Was ("Habitual", None) — the one-name render
    # state, which no longer exists. A player who moves around takes its place
    # so the count of states covered does not quietly drop by one, and it only
    # covers anything if the orders exist: an unscouted fixture renders
    # `NOTHING_SEEN` and never reaches `READ_COPY["none"]`, which is the string
    # in this set most likely to acquire punctuation.
    _moves_around("Switcher")
    for them, you in (
        ("Habitual", "Asker"),
        ("Unseen", "Asker"),
        ("Habitual", "Unseen"),
        ("Giant", "Asker"),
        ("Switcher", "Asker"),
        ("Mixed", "Asker"),
    ):
        embed = _embed(them, you)
        text = " ".join(
            [embed.title or "", embed.description or "", embed.footer.text or ""]
            + [f"{f.name} {f.value}" for f in embed.fields]
        )
        assert "—" not in text, f"{them} vs {you}: {text}"


def test_the_envelope_is_never_offered_as_a_better_prediction(cd_db):
    """Uniform weighting over every configuration is the wrong prior, and
    quoting it against the card would be a worse claim than the one it
    criticises.

    THE GUARD MOVED INTO THE LABEL. It used to be a note under the range
    ("not a second prediction"), which was the copy defending a figure against
    a misreading. Kevin's call, 2026-08-22: name the two numbers and the
    misreading has nowhere to start. So what this test holds now is the label
    and the shape of the value — one range, no second figure presented as an
    answer."""
    _habit()
    embed = _embed("Habitual", "Asker")
    assert hub.FIELD_WORTH == "Best and worst case"
    value = _field(embed, hub.FIELD_WORTH)
    # A floor and a ceiling, and nothing that reads as a rival to the card's
    # own number.
    assert value.startswith("Across every line-up the two of you could set")
    assert len(re.findall(r"\d+(?:\.\d+)?%|[<>]\d+%", value)) == 2


# ── the vocabulary guard ─────────────────────────────────────────────────────


def test_nothing_seen_names_the_button_that_exists(cd_db):
    """The empty state has to name a button that is on the grid, or it sends a
    member looking for one that is not there. Taking `{button}` from the label
    rather than retyping it is what keeps that true through a rename.

    And it comes through `_btn_words`, so the near-black U+2795 never reaches
    the sentence: typed in, it renders as a gap and the reader is told to press
    "** Record a line-up**"."""
    rendered = _field(_embed("Unseen", "Asker"), hub.FIELD_THEIRS)
    assert hub._btn_words(hub.CD_BTN_ORDER) in rendered
    assert "➕" not in rendered


def test_no_two_grade_vocabularies_share_a_word():
    """Four three-tier grades exist across this feature and no word may sit in
    two of them.

    This is the guard, not the rename. The collision was real in the code and
    invisible on screen: only `confidence` prints its grade word, so `high`
    meaning "well evidenced" on a card and "the deployment decides it" on an
    intel page never met a reader. It would have, the first time somebody wrote
    a sentence with the grade in it. Before 2026-08-20 this test failed twice:
    `high` in confidence and worth, `some` in evidence and worth."""
    vocabularies = {
        "prediction confidence": {"high", "medium", "low"},
        "prediction evidence": set(words.EVIDENCE_COPY),
        "intel worth": set(words.WORTH_COPY),
        "intel read": {intel_lib.STRONG, intel_lib.LEAN, intel_lib.NONE},
    }
    for name, vocabulary in vocabularies.items():
        for other_name, other in vocabularies.items():
            if name >= other_name:
                continue
            shared = vocabulary & other
            assert not shared, f"{name} and {other_name} both use {shared}"


def test_the_intel_grades_only_select_a_sentence(cd_db):
    """A grade is a dict key here and never a word on the surface.

    What the reader gets is the sentence the grade chose, verbatim from
    `champion_duel_wording`. If a grade ever starts being formatted into copy
    instead, this stops holding and the rename above stops being enough."""
    _habit()
    for them, you in (("Habitual", "Asker"), ("Unseen", "Asker"), ("Giant", "Asker")):
        result = intel_lib.intel(_player(them), _player(you))
        embed = hub.build_intel_embed(result)
        assert words.WORTH_COPY[result.worth] in embed.description
        field = _field(embed, hub.FIELD_THEIRS)
        expected = (
            words.read_line(result.read)
            if result.habit
            else words.NOTHING_SEEN.format(button=hub._btn_words(hub.CD_BTN_ORDER))
        )
        assert expected in field


def test_a_thin_change_rate_says_the_limit_rather_than_the_mechanism(cd_db):
    """Where some meetings have only one line-up on file, the rate is computed
    over fewer meetings than we hold and the copy has to say so.

    Kevin's call, 2026-08-20: say the limit, not the mechanism. "The 3 meetings
    we watched more than once" is accurate and asks the reader to work out why
    a meeting seen once is useless. What they need is that the number is
    thinner than it looks."""
    # Three meetings with two line-ups each, then three sighted once.
    for i in range(3):
        for _ in range(2):
            db.add_order(
                _rid("Habitual"),
                ["Missile", "Tank", "Aircraft"],
                actor=KEV,
                opponent=f"twice{i}",
                observed_at=f"2026-08-1{i}",
            )
    for i in range(3):
        db.add_order(
            _rid("Habitual"),
            ["Missile", "Tank", "Aircraft"],
            actor=KEV,
            opponent=f"once{i}",
            observed_at=f"2026-08-2{i}",
        )

    habit = intel_lib.read_habit(_player("Habitual"))
    assert (habit.meetings, habit.meetings_multi) == (6, 3)

    field = _field(_embed("Habitual"), hub.FIELD_THEIRS)
    assert "we can only tell for 3 of their 6 meetings" in field
    # The short form claims we watched all six and must not appear.
    assert "our 6 recorded meetings" not in field
    assert "our 3 recorded meetings" not in field


def test_full_coverage_needs_no_qualifier(cd_db):
    """The common case, and the sentence Kevin wrote. Every meeting on file has
    more than one line-up recorded from it, so the count is the whole record."""
    _habit()
    field = _field(_embed("Habitual"), hub.FIELD_THEIRS)
    assert "in our 3 recorded meetings" in field
    assert "we can only tell for" not in field


# ── the modal ──────────────────────────────────────────────────────────────


def _interaction():
    interaction = MagicMock()
    interaction.user.id = 1
    interaction.guild_id = 999
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _sent(interaction):
    """The text of the last followup, whether positional or keyword."""
    call = interaction.followup.send.call_args
    return call.args[0] if call.args else call.kwargs.get("content", "")


def _modal(monkeypatch, **values):
    """The modal filled in, with the engine present and the paywall open."""
    monkeypatch.setattr(intel_lib, "ENGINE_AVAILABLE", True)
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=True))
    modal = hub._IntelModal()
    for field, value in values.items():
        getattr(modal, field)._value = value
    return modal


def test_the_second_name_is_required_on_the_control(cd_db):
    """Discord enforces this client-side and it is the first thing a member
    meets, so the flag itself is worth asserting."""
    modal = hub._IntelModal()
    assert modal.you.required
    assert modal.opponent.required
    # Read off the component payload rather than `.label`, which discord.py
    # deprecated in favour of `discord.ui.Label`.
    assert "optional" not in str(modal.you.to_component_dict()).lower()
    # The two servers stay optional together. Requiring one and not the other
    # would read as a difference between the sides that does not exist.
    assert not modal.opponent_server.required
    assert not modal.your_server.required


async def test_a_blank_second_name_is_told_what_to_do_rather_than_raising(cd_db, monkeypatch):
    """Discord will not send this and the handler is not entitled to assume
    Discord is the only thing that can. Without the check the blank reaches
    `_resolve`, which asks the roster for "" and answers "No registrant
    matches" — a true sentence about a question nobody asked."""
    modal = _modal(monkeypatch, opponent="Habitual", you="   ", opponent_server="", your_server="")
    interaction = _interaction()

    await modal.on_submit(interaction)

    told = _sent(interaction)
    assert told == hub._INTEL_NEEDS_BOTH
    assert "No registrant matches" not in told
    # It names the field rather than the rule, and it stops there. It used to
    # end "Open it again and fill in both names", which pointed at the hub
    # message far up the channel -- the dead end the retry button replaced.
    assert "both players" in told.lower()
    assert "open it again" not in told.lower()


async def test_a_blank_opponent_is_refused_on_the_same_terms(cd_db, monkeypatch):
    """The guard defends against a payload Discord did not send, and that
    threat does not distinguish the two fields. Covering only the one that
    changed would leave `opponent` reaching `_resolve("")` for the same
    "No registrant matches ****" the guard exists to prevent."""
    modal = _modal(monkeypatch, opponent="  ", you="Asker", opponent_server="", your_server="")
    interaction = _interaction()

    await modal.on_submit(interaction)

    told = _sent(interaction)
    assert told == hub._INTEL_NEEDS_BOTH
    assert "No registrant matches" not in told


async def test_a_mistyped_own_name_gets_the_did_you_mean_list(cd_db, monkeypatch):
    """The cost of requiring the second name is that a member has to know their
    own roster spelling, and that cost was accepted on the condition that
    getting it wrong is recoverable. Your side resolves through the same
    `_resolve` as theirs, so a near miss suggests rather than dead-ends."""
    _habit()
    modal = _modal(monkeypatch, opponent="Habitual", you="Askr", opponent_server="", your_server="")
    interaction = _interaction()

    await modal.on_submit(interaction)

    told = _sent(interaction)
    assert "Did you mean" in told
    assert "Asker" in told


# ── the way back in ──────────────────────────────────────────────────────────
#
# Four of the five ways `on_submit` can refuse used to end in an ephemeral
# message and nothing to press, and the member's only route back was to scroll
# up a busy channel to the hub message -- with everything they typed gone.
#
# Three of the four are covered here. The other two are refusals no edit can
# fix, and they are asserted to stay buttonless: offering to reopen the form
# would be promising something that would not happen.

SPACED = "A Girl Has A Name"


def _view(interaction):
    """The view on the last followup, or None if it went out without one."""
    return interaction.followup.send.call_args.kwargs.get("view")


def _retry_buttons(interaction):
    view = _view(interaction)
    if view is None:
        return []
    return [b for b in view.children if getattr(b, "label", None) == hub.CD_BTN_INTEL_RETRY]


async def _submit(monkeypatch, **values):
    """Fill the modal in, submit it, and hand back the interaction."""
    values.setdefault("opponent_server", "")
    values.setdefault("your_server", "")
    modal = _modal(monkeypatch, **values)
    interaction = _interaction()
    await modal.on_submit(interaction)
    return interaction


def _press(interaction, user_id=1):
    """Press the retry button and hand back the modal it opened."""
    (button,) = _retry_buttons(interaction)
    reopened = _interaction()
    reopened.user.id = user_id
    reopened.response.send_modal = AsyncMock()
    return button, reopened


@pytest.mark.parametrize(
    "values, because",
    [
        (
            {"opponent": "Habitual", "you": "   "},
            "a name missing -- the payload guard",
        ),
        (
            {"opponent": "Habitual", "you": "Askr"},
            "a name wrong -- the did-you-mean",
        ),
        (
            {"opponent": "Twinned", "you": "Asker"},
            "a name on two warzones -- the ambiguity",
        ),
    ],
)
async def test_every_fixable_refusal_offers_one_way_back(cd_db, monkeypatch, values, because):
    """The three states differ and the button does not. One label the member
    learns once beats three they read separately, so all three paths send the
    identical control -- and one, not a row of them."""
    db.import_registrants(
        [{"name": "Twinned", "group": "M", "rank": 7, "server": "1042", "thp": 200_000_000}],
        stage="qualifiers",
    )
    db.import_registrants(
        [{"name": "Twinned", "group": "M", "rank": 8, "server": "738", "thp": 210_000_000}],
        stage="qualifiers",
    )

    interaction = await _submit(monkeypatch, **values)

    view = _view(interaction)
    assert isinstance(view, hub._IntelRetryView), because
    assert len(view.children) == 1, "one control, not a row of them"
    assert view.children[0].label == hub.CD_BTN_INTEL_RETRY
    # An ephemeral that outlives its view leaves a button that does nothing.
    assert view.timeout


async def test_the_button_reopens_the_form_with_all_four_boxes_still_filled(cd_db, monkeypatch):
    """The prefill is the whole point of the retry on a misspelling: the near
    miss is sitting in the box, one character from being right, rather than
    retyped from memory alongside the "Did you mean" that named it."""
    interaction = await _submit(
        monkeypatch,
        opponent="Habitul",
        opponent_server="738",
        you="Asker",
        your_server="1042",
    )
    assert "Did you mean" in _sent(interaction)

    button, reopened = _press(interaction)
    await button.callback(reopened)

    modal = reopened.response.send_modal.call_args.args[0]
    assert isinstance(modal, hub._IntelModal)
    assert modal.opponent.default == "Habitul"
    assert modal.opponent_server.default == "738"
    assert modal.you.default == "Asker"
    assert modal.your_server.default == "1042"


async def test_a_prefill_cannot_reach_the_next_person_who_opens_the_form(cd_db, monkeypatch):
    """`Modal._init_children` deepcopies each declared item onto the instance,
    which is what makes setting a default on `self` safe. Asserted rather than
    trusted, because the comment saying so is the kind of thing that gets
    "cleaned up" by someone who assumes class attributes are shared."""
    interaction = await _submit(monkeypatch, opponent="Habitul", you="Asker")
    button, reopened = _press(interaction)
    await button.callback(reopened)

    assert hub._IntelModal().opponent.default is None
    assert hub._IntelModal().you.default is None


async def test_a_name_with_spaces_in_it_survives_the_round_trip(cd_db, monkeypatch):
    """`normalize_name` strips whitespace entirely, so a name that only exists
    with spaces in it is the one most likely to be quietly rewritten on the way
    back into the box. It comes back exactly as it was typed."""
    db.import_registrants(
        [{"name": SPACED, "group": "M", "rank": 9, "server": "738", "thp": 250_000_000}],
        stage="qualifiers",
    )
    _squads(SPACED, "observed")

    # Misspelled on the last word only, so the name stays multi-word.
    interaction = await _submit(monkeypatch, opponent="A Girl Has A Naem", you="Asker")
    button, reopened = _press(interaction)
    await button.callback(reopened)

    assert reopened.response.send_modal.call_args.args[0].opponent.default == "A Girl Has A Naem"

    # And the real spelling resolves rather than being lost to the stripping.
    got = await hub._resolve(SPACED, "738")
    assert not isinstance(got, str)
    assert got["display_name"] == SPACED


async def test_failing_twice_in_a_row_still_offers_the_way_out(cd_db, monkeypatch):
    """The member must not be one further mistake from being stranded again,
    and the second offer has to carry the second attempt's text rather than
    the first's -- otherwise the retry hands back a correction already made."""
    first = await _submit(monkeypatch, opponent="Habitul", you="Asker")
    button, reopened = _press(first)
    await button.callback(reopened)

    second = await _submit(monkeypatch, opponent="Habitua", you="Asker")

    assert len(_retry_buttons(second)) == 1
    again, third = _press(second)
    await again.callback(third)
    assert third.response.send_modal.call_args.args[0].opponent.default == "Habitua"


async def test_a_missing_engine_is_not_offered_a_form_to_edit(cd_db, monkeypatch):
    """Nothing the member can type installs an engine, so a button offering to
    reopen the form would be lying about what happens next."""
    monkeypatch.setattr(intel_lib, "ENGINE_AVAILABLE", False)
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=True))
    modal = hub._IntelModal()
    for field in ("opponent", "you", "opponent_server", "your_server"):
        getattr(modal, field)._value = "Habitual" if field in ("opponent", "you") else ""
    interaction = _interaction()

    await modal.on_submit(interaction)

    assert _sent(interaction) == hub._ENGINE_MISSING
    assert _retry_buttons(interaction) == []


async def test_a_missing_engine_reached_through_the_lookup_is_not_offered_one_either(
    cd_db, monkeypatch
):
    """`_resolve` returns `_ENGINE_MISSING` off `db.NAMES_AVAILABLE`, which is
    a different flag from the one `on_submit` checks. They come from the same
    package and in practice fail together, but a partial install reaches the
    refusal through the lookup rather than through the guard, and the button
    would be just as untrue there."""
    monkeypatch.setattr(db, "NAMES_AVAILABLE", False)
    interaction = await _submit(monkeypatch, opponent="Habitual", you="Asker")

    assert _sent(interaction) == hub._ENGINE_MISSING
    assert _retry_buttons(interaction) == []


async def test_the_paywall_is_not_offered_a_form_to_edit(cd_db, monkeypatch):
    """Same reason as the engine: editing the entry does not buy a
    subscription. Asserted as "no retry button" rather than "no view", because
    the upsell carries Discord's own premium button whenever a SKU is
    configured."""
    monkeypatch.setattr(intel_lib, "ENGINE_AVAILABLE", True)
    monkeypatch.setattr(premium, "feature_gate", AsyncMock(return_value=False))
    modal = hub._IntelModal()
    interaction = _interaction()

    await modal.on_submit(interaction)

    assert _retry_buttons(interaction) == []


async def test_a_success_carries_no_leftover_way_back(cd_db, monkeypatch):
    """The retry rides on the refusal, not on the surface. A control offering
    to re-edit an answer that arrived would be inviting a member to redo work
    that worked."""
    _habit()
    interaction = await _submit(monkeypatch, opponent="Habitual", you="Asker")

    assert interaction.followup.send.call_args.kwargs.get("embed") is not None
    assert _retry_buttons(interaction) == []

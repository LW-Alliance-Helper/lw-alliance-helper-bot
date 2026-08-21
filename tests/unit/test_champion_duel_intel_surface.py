"""The intel button and the embed behind it.

Split from `test_champion_duel_intel.py`, which tests the join. This file tests
what a member is shown, and the tests that matter most are the ones asserting
what is *not* shown: the surface's job is to stop talking where the numbers stop
meaning anything, and every one of those stops is a place a later change could
quietly start talking again.
"""

from __future__ import annotations

import re

import pytest

import champion_duel_db as db
import champion_duel_hub as hub
import champion_duel_intel as intel_lib
import champion_duel_wording as words

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


def _player(name):
    return db.get_player(name, server="738", include_scouting=True)


def _embed(them, you=None):
    return hub.build_intel_embed(intel_lib.intel(_player(them), _player(you) if you else None))


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
    assert seen_nothing == words.NOTHING_SEEN
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
    assert "will not change this answer" in anyway
    assert hub.CHAMPION_DUEL_HUB_CMD in anyway, "an ask with no exit is not an ask"


def test_your_own_placeholder_types_ask_for_squads_rather_than_ranking_nothing(cd_db):
    _habit()
    embed = _embed("Habitual", "Unseen")

    what_to_set = _field(embed, hub.FIELD_YOURS)
    assert "I don't know which of your squads is which type" in what_to_set
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
    assert words.CANNOT_RECOMMEND in what_to_set
    # Carrying the measurement, because "I cannot tell you" is a claim the
    # reader is entitled to check.
    assert "points" in what_to_set
    assert "coin flip" in what_to_set
    # Six rows within three points of each other is the same broken-looking
    # surface that six rows of <1% was.
    assert _field(embed, hub.FIELD_OTHERS) is None
    # And it does not go quiet: the ask names the press that fixes it.
    fix = _field(embed, hub.FIELD_FIX)
    assert fix is not None and hub.CHAMPION_DUEL_HUB_CMD in fix
    assert "36 arrangements to 12" in fix


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
    assert words.CANNOT_RECOMMEND not in what_to_set
    assert "Tank → Aircraft → Missile" in what_to_set


def test_one_name_still_answers_the_question_it_can(cd_db):
    """The counter triangle does not need to know what you field, so refusing
    here would decline a question we can fully answer."""
    _habit()
    embed = _embed("Habitual")

    assert "Tank → Aircraft → Missile" in _field(embed, hub.FIELD_YOURS)
    assert _field(embed, hub.FIELD_WORTH) is None
    assert embed.description is None


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
    assert "We have not seen which of this player's squads is which type" in embed.footer.text


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
    for them, you in (
        ("Habitual", "Asker"),
        ("Unseen", "Asker"),
        ("Habitual", "Unseen"),
        ("Giant", "Asker"),
        ("Habitual", None),
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
    criticises."""
    _habit()
    value = _field(_embed("Habitual", "Asker"), hub.FIELD_WORTH)
    assert "not a second prediction" in value


# ── the vocabulary guard ─────────────────────────────────────────────────────


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
        expected = words.read_line(result.read) if result.habit else words.NOTHING_SEEN
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

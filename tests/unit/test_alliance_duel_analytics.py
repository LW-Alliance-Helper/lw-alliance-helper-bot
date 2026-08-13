"""Unit tests for the VS analytics (#408).

The whole module is derived reads over rows that already exist, so the risk is
not that a number comes out wrong. It is that a number comes out *confident*:

- **A rate off one week is not a pattern**, and must not render like one.
- **Unrecorded is never a loss.** A blank Day Outcome is a day nobody logged,
  and counting it against the alliance would quietly invent a losing streak.
- **Observations, never verdicts.** "Both sides scored 40% below their
  averages" ships; "they saved" does not, because the bot does not rate other
  alliances.
- **Pick accuracy is partitioned, not filtered**, and says out loud what it
  cannot see.
"""

import datetime as _dt
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import alliance_duel as ad
import alliance_duel_analytics as an
import alliance_duel_hub as hub
import alliance_duel_ui as ad_ui
import config_health


LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
OLD_LEAGUE = ad.LeagueKey("S34", "Gold", "9 - 1")
MONDAY = _dt.date(2026, 8, 10)
OWN_TAG, OWN_WZ = "US", "1234"
OWN = ad.AllianceKey.of(OWN_TAG, OWN_WZ)
THEM = ad.AllianceKey.of("A02", OWN_WZ)


def _row(tag, week=1, league=LEAGUE, week_date=MONDAY, **kw):
    return ad.AllianceWeek(
        league=league,
        week=week,
        alliance=ad.AllianceKey.of(tag, OWN_WZ),
        week_date=week_date,
        tag_display=tag,
        **kw,
    )


def _matchup(week, own_scores, their_scores, outcomes=None, outcome=None, **own_extra):
    """One week with both sides recorded, which is the only shape that can
    produce a margin or an engagement read."""
    mine = _row(
        OWN_TAG,
        week=week,
        opponent=THEM,
        day_scores=dict(own_scores),
        day_outcomes=dict(outcomes or {}),
        week_outcome=outcome,
        **own_extra,
    )
    mirrored = {d: ("L" if o == "W" else "W") for d, o in (outcomes or {}).items()}
    theirs = _row(
        "A02",
        week=week,
        opponent=OWN,
        day_scores=dict(their_scores),
        day_outcomes=mirrored,
    )
    return [mine, theirs]


def _state(rows, **cfg_over):
    cfg = {
        "guild_id": 1,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": OWN_TAG,
        "own_warzone": OWN_WZ,
        "tracking_mode": ad.MODE_OWN_ALLIANCE,
    }
    cfg.update(cfg_over)
    return hub.HubState(1, cfg, rows)


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def _no_config_health_db(monkeypatch):
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


# ── Day profiles ──────────────────────────────────────────────────────────────


def test_a_day_nobody_logged_is_not_a_day_you_lose():
    profile = an.day_profile([_row(OWN_TAG, day_outcomes={1: "W"})], OWN)
    day_three = next(d for d in profile.days if d.day == 3)
    assert day_three.played == 0
    assert day_three.win_rate is None


def test_the_weakest_day_is_the_one_worth_banking_for():
    rows = [_row(OWN_TAG, week=w, day_outcomes={1: "W", 3: "L", 5: "W"}) for w in range(1, 5)]
    profile = an.day_profile(rows, OWN)
    worst = profile.ranked(best_first=False)[0]
    assert worst.day == 3
    assert worst.theme == "Age of Science"


def test_ties_break_towards_the_bigger_sample():
    """A 100% record over eight weeks is a stronger claim than the same rate
    over two, and the ordering has to say so."""
    rows = [_row(OWN_TAG, week=w, day_outcomes={1: "W"}) for w in range(1, 9)]
    rows += [_row(OWN_TAG, week=w, day_outcomes={2: "W"}) for w in range(1, 3)]
    profile = an.day_profile(rows, OWN)
    assert profile.ranked(best_first=True)[0].day == 1


def test_a_minimum_sample_can_be_demanded():
    rows = [_row(OWN_TAG, day_outcomes={1: "W", 2: "L"})]
    profile = an.day_profile(rows, OWN)
    assert profile.ranked(best_first=False, minimum=3) == ()


def test_day_six_is_singled_out_because_nothing_else_predicts_it():
    rows = [_row(OWN_TAG, week=w, day_outcomes={6: "W" if w < 4 else "L"}) for w in range(1, 5)]
    buster = an.day_profile(rows, OWN).day_six()
    assert (buster.wins, buster.losses) == (3, 1)


# ── Margins ───────────────────────────────────────────────────────────────────


def test_margins_need_both_sides_recorded():
    one_sided = [_row(OWN_TAG, opponent=THEM, day_scores={1: 100})]
    assert an.margin_shape(one_sided, OWN).days_compared == 0


def test_a_close_week_and_a_blowout_are_told_apart():
    rows = _matchup(1, {1: 102, 2: 300}, {1: 100, 2: 100})
    shape = an.margin_shape(rows, OWN)
    assert shape.close_days == 1
    assert shape.blowouts == 1


def test_a_losing_margin_is_negative():
    rows = _matchup(1, {1: 50}, {1: 100})
    assert an.margin_shape(rows, OWN).median < 0


def test_margins_can_be_narrowed_to_one_opponent():
    rows = _matchup(1, {1: 200}, {1: 100})
    rows += _matchup(2, {1: 100}, {1: 100})
    other = ad.AllianceKey.of("A09", OWN_WZ)
    assert an.margin_shape(rows, OWN, other).days_compared == 0
    assert an.margin_shape(rows, OWN, THEM).days_compared == 2


# ── Raw points against league points ──────────────────────────────────────────


def test_outscoring_them_and_losing_the_week_is_surfaced():
    """The week that gets misremembered as bad luck when it is really a
    distribution problem."""
    rows = _matchup(1, {1: 500, 2: 10, 3: 10}, {1: 100, 2: 20, 3: 20}, outcome="L")
    found = an.divergences(rows, OWN)
    assert len(found) == 1
    assert found[0].outscored_and_lost


def test_an_ordinary_week_is_not_flagged():
    rows = _matchup(1, {1: 500}, {1: 100}, outcome="W")
    assert an.divergences(rows, OWN) == ()


def test_winning_a_week_you_were_outscored_in_is_also_worth_knowing():
    rows = _matchup(1, {1: 10, 2: 10}, {1: 500, 2: 5}, outcome="W")
    found = an.divergences(rows, OWN)
    assert found and found[0].outscored_and_won


# ── Engagement ────────────────────────────────────────────────────────────────


def test_engagement_says_nothing_without_a_baseline():
    rows = _matchup(1, {1: 100}, {1: 100})
    read = an.engagement(rows, OWN, 1)
    assert read is not None
    assert read.has_baseline is False


def test_engagement_reports_the_share_of_each_sides_own_normal():
    rows = []
    for week in range(1, 5):
        rows += _matchup(week, {1: 1000}, {1: 1000})
    rows += _matchup(5, {1: 400}, {1: 500})
    read = an.engagement(rows, OWN, 5)
    assert read.has_baseline is True
    assert read.own_share == pytest.approx(0.4)
    assert read.opponent_share == pytest.approx(0.5)


def test_engagement_returns_an_observation_not_a_verdict():
    """No field on the result names a reason. Calling a week a save is an
    invented rating about another alliance."""
    fields = an.EngagementRead.__dataclass_fields__
    assert set(fields) == {"week", "own_share", "opponent_share", "baseline_weeks"}


# ── Power movement ────────────────────────────────────────────────────────────


def test_power_movement_falls_out_of_the_rows_already_recorded():
    rows = [
        _row("A02", week=1, power=100_000_000),
        _row("A02", week=3, power=118_000_000, week_date=MONDAY + _dt.timedelta(days=14)),
    ]
    jump = an.power_jump(rows, THEM)
    assert jump.change == pytest.approx(0.18)
    assert jump.is_material


def test_ordinary_drift_is_not_worth_mentioning():
    rows = [
        _row("A02", week=1, power=100_000_000),
        _row("A02", week=2, power=101_000_000, week_date=MONDAY + _dt.timedelta(days=7)),
    ]
    assert an.power_jump(rows, THEM).is_material is False


def test_one_recorded_power_is_not_a_trend():
    assert an.power_jump([_row("A02", power=100)], THEM) is None


# ── Season trajectory ─────────────────────────────────────────────────────────


def test_each_season_keeps_the_tier_it_was_earned_in():
    rows = [_row(OWN_TAG, week=w, league=OLD_LEAGUE, week_outcome="W") for w in range(1, 4)]
    rows += [_row(OWN_TAG, week=1, week_outcome="L")]
    seasons = an.season_trajectory(rows, OWN)
    assert [s.record for s in seasons] == ["3-0", "0-1"]
    assert [s.league.tier for s in seasons] == ["Gold", "Diamond"]


def test_a_league_with_nothing_recorded_is_left_out():
    assert an.season_trajectory([_row(OWN_TAG)], OWN) == ()


# ── Pick accuracy ─────────────────────────────────────────────────────────────


def test_a_declared_save_never_counts_as_a_failed_prediction():
    rows = [
        _row(OWN_TAG, week=1, picked="W", week_outcome="L", intent=ad.INTENT_SAVE),
        _row(OWN_TAG, week=2, picked="W", week_outcome="W", intent=ad.INTENT_PUSH),
    ]
    accuracy = an.pick_accuracy(rows, OWN)
    assert (accuracy.correct, accuracy.wrong) == (1, 0)
    assert accuracy.excluded_saves == 1


def test_a_push_that_lost_counts_against_the_rate():
    rows = [_row(OWN_TAG, week=1, picked="W", week_outcome="L", intent=ad.INTENT_PUSH)]
    accuracy = an.pick_accuracy(rows, OWN)
    assert (accuracy.correct, accuracy.wrong) == (0, 1)
    assert accuracy.rate == 0.0


def test_weeks_with_no_call_are_counted_separately_from_wrong_ones():
    rows = [
        _row(OWN_TAG, week=1, week_outcome="W"),
        _row(OWN_TAG, week=2, picked="W", week_outcome="W"),
    ]
    accuracy = an.pick_accuracy(rows, OWN)
    assert accuracy.unpicked == 1
    assert accuracy.judged == 1


def test_an_undeclared_sample_is_reported_as_an_assumption():
    rows = [_row(OWN_TAG, week=w, picked="W", week_outcome="W") for w in range(1, 4)]
    assert an.pick_accuracy(rows, OWN).rests_on_assumption == 3


def test_only_your_own_rows_feed_your_accuracy():
    rows = [_row("A02", week=1, picked="W", week_outcome="L")]
    assert an.pick_accuracy(rows, OWN).judged == 0


# ── The surface ───────────────────────────────────────────────────────────────


def test_trends_works_without_a_bracket():
    """The most actionable output in the feature needs no bracket data, which
    is what makes it worth having in own-alliance mode."""
    rows = [_row(OWN_TAG, week=w, day_outcomes={1: "W", 3: "L"}) for w in range(1, 5)]
    text = _text(ad_ui.trends_embed(_state(rows)))
    assert "Age of Science" in text
    assert "0-4" in text or "0%" in text


def test_a_thin_sample_is_labelled_rather_than_stated_as_a_pattern():
    rows = [_row(OWN_TAG, week=1, day_outcomes={3: "L"})]
    text = _text(ad_ui.trends_embed(_state(rows)))
    assert "too few" in text.lower()


def test_an_empty_tab_says_what_would_fill_it():
    text = _text(ad_ui.trends_embed(_state([_row(OWN_TAG)])))
    assert "Nothing recorded yet" in text
    assert "log" in text.lower()


def test_the_accuracy_block_says_what_it_cannot_see():
    """A number this easy to over-read carries its caveat in the same field."""
    rows = [
        _row(OWN_TAG, week=w, picked="W", week_outcome="W", intent=ad.INTENT_PUSH)
        for w in range(1, 4)
    ]
    text = _text(ad_ui.trends_embed(_state(rows)))
    assert "opponent saving quietly" in text


def test_the_sample_size_travels_with_the_finding():
    rows = [_row(OWN_TAG, week=w, day_outcomes={1: "W"}) for w in range(1, 6)]
    text = _text(ad_ui.trends_embed(_state(rows)))
    assert "5 weeks" in text


def test_opponent_trends_say_how_little_they_rest_on():
    rows = _matchup(1, {1: 100}, {1: 200}, outcomes={1: "L"})
    text = _text(ad_ui.opponent_trends_embed(_state(rows), THEM))
    assert "1 week" in text


def test_a_never_met_alliance_gets_a_reason_rather_than_an_empty_table():
    text = _text(ad_ui.opponent_trends_embed(_state([_row(OWN_TAG)]), THEM))
    assert "weeks you played them" in text


def test_the_trends_surfaces_carry_no_em_dashes():
    rows = [_row(OWN_TAG, week=w, day_outcomes={1: "W"}, week_outcome="W") for w in range(1, 4)]
    assert "—" not in _text(ad_ui.trends_embed(_state(rows)))
    assert "—" not in _text(ad_ui.opponent_trends_embed(_state(rows), THEM))


def test_a_missing_value_renders_as_the_shared_glyph_not_a_zero():
    assert an.pct(None) == ad.NOT_ENTERED
    assert an.pct(0.0) == "0%"

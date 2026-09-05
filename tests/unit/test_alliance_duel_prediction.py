"""Unit tests for the Alliance Duel (VS) prediction model (#401).

Pure functions over `AllianceProfile`s — no Discord, no Sheets, no fixtures.

What these are really guarding is the *honesty* of the output rather than its
arithmetic. The failure mode that matters is a confident-sounding call the data
cannot support, so most of the assertions below are about the model declining
to say things: no day called on a slight lean, nothing called at all on stale
inputs, no combat estimate ever, and `easy` reserved for unanimous strong
agreement.
"""

import datetime as _dt

import pytest

import alliance_duel as ad


TODAY = _dt.date(2026, 8, 12)
FRESH = _dt.date(2026, 8, 10)
ANCIENT = TODAY - _dt.timedelta(days=ad.STALE_AFTER_DAYS + 60)

#: Values chosen to sit unambiguously either side of each threshold, so tuning
#: a constant later fails a threshold test rather than silently reshaping every
#: other assertion in the file.
POWER_STRONG, POWER_SLIGHT, POWER_EVEN = 400_000_000, 340_000_000, 310_000_000
POWER_BASE = 300_000_000
MEMBERS_STRONG, MEMBERS_SLIGHT, MEMBERS_EVEN = 105, 88, 82
MEMBERS_BASE = 80
GIFT_STRONG, GIFT_SLIGHT, GIFT_EVEN = 22, 17, 16
GIFT_BASE = 15


def _profile(tag, power=POWER_BASE, members=MEMBERS_BASE, gift=GIFT_BASE, **kw):
    stamp = kw.pop("stamp", FRESH)
    as_of = {} if stamp is None else dict.fromkeys(ad.PREDICTION_METRICS, stamp)
    return ad.AllianceProfile(
        alliance=ad.AllianceKey.of(tag, "1234"),
        power=power,
        members=members,
        gift_level=gift,
        as_of=as_of,
        **kw,
    )


def _us(**kw):
    return _profile("US", **kw)


def _them(**kw):
    return _profile("EM", **kw)


def _buckets(projection):
    return {day.day: day.bucket for day in projection.days}


# ── Metric classification ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "power,expected",
    [
        (POWER_STRONG, ad.LEAN_STRONG),
        (POWER_SLIGHT, ad.LEAN_SLIGHT),
        (POWER_EVEN, ad.LEAN_EVEN),
        (POWER_BASE, ad.LEAN_EVEN),
    ],
)
def test_power_is_classified_strong_slight_or_even(power, expected):
    vote = ad.assess(_us(power=power), _them(), today=TODAY)
    assert vote.lean_for(ad.METRIC_POWER).strength == expected


def test_gift_level_compares_by_levels_not_by_ratio():
    # A level is not a count: +5 levels means the same thing at level 3 as at
    # level 30, where a ratio would call one enormous and the other noise.
    strong = ad.assess(_us(gift=GIFT_STRONG), _them(), today=TODAY)
    assert strong.lean_for(ad.METRIC_GIFT_LEVEL).strength == ad.LEAN_STRONG
    high = ad.assess(_us(gift=GIFT_BASE + 40), _them(gift=GIFT_BASE + 33), today=TODAY)
    assert high.lean_for(ad.METRIC_GIFT_LEVEL).strength == ad.LEAN_STRONG


def test_gift_level_zero_is_a_real_value_not_a_missing_one():
    vote = ad.assess(_us(gift=0), _them(gift=GIFT_BASE), today=TODAY)
    lean = vote.lean_for(ad.METRIC_GIFT_LEVEL)
    assert lean.direction == -1
    assert lean.strength == ad.LEAN_STRONG
    assert lean.label == "gifts -15 levels"


def test_classification_is_direction_symmetric():
    # Measured on the larger-over-smaller ratio, so swapping the sides has to
    # mirror the answer exactly rather than shifting the thresholds.
    ours = ad.assess(_us(power=POWER_STRONG), _them(), today=TODAY)
    theirs = ad.assess(_them(), _us(power=POWER_STRONG), today=TODAY)
    assert ours.lean_for(ad.METRIC_POWER).direction == 1
    assert theirs.lean_for(ad.METRIC_POWER).direction == -1
    assert ours.confidence == theirs.confidence
    assert ours.direction == -theirs.direction


def test_components_line_prints_what_an_r5_can_act_on():
    vote = ad.assess(
        _us(power=POWER_STRONG, gift=GIFT_SLIGHT, members=MEMBERS_BASE),
        _them(),
        today=TODAY,
    )
    assert vote.components == "Power +33%, gifts +2 levels, members even"


def test_a_missing_input_is_never_treated_as_a_zero():
    # Tier 1 requires all three. A blank Members cell means unknown, and
    # scoring it as zero would invent a landslide out of an empty cell.
    assert ad.assess(_us(members=None), _them(), today=TODAY) is None
    assert ad.assess(_us(), _them(power=None), today=TODAY) is None


# ── Agreement voting ──────────────────────────────────────────────────────────


def test_all_three_agreeing_with_two_strong_is_confident():
    vote = ad.assess(
        _us(power=POWER_STRONG, gift=GIFT_STRONG, members=MEMBERS_SLIGHT), _them(), today=TODAY
    )
    assert vote.direction == 1
    assert vote.confidence == ad.CONFIDENCE_CONFIDENT


def test_a_majority_with_nothing_contradicting_is_moderate():
    vote = ad.assess(_us(power=POWER_STRONG), _them(), today=TODAY)
    assert vote.direction == 1
    assert vote.confidence == ad.CONFIDENCE_MODERATE


def test_one_strong_metric_and_two_even_never_reaches_confident():
    # Confident needs the other two to agree, not merely to stay quiet.
    vote = ad.assess(_us(power=POWER_STRONG, gift=GIFT_EVEN), _them(), today=TODAY)
    assert vote.confidence == ad.CONFIDENCE_MODERATE


def test_any_metric_contradicting_another_is_a_toss_up():
    vote = ad.assess(_us(power=POWER_STRONG, members=MEMBERS_BASE - 30), _them(), today=TODAY)
    assert vote.direction == 0
    assert vote.confidence == ad.CONFIDENCE_TOSSUP


def test_all_near_even_is_a_toss_up():
    vote = ad.assess(
        _us(power=POWER_EVEN, members=MEMBERS_EVEN, gift=GIFT_EVEN), _them(), today=TODAY
    )
    assert vote.direction == 0
    assert vote.confidence == ad.CONFIDENCE_TOSSUP


def test_a_toss_up_is_an_answer_not_a_failure_to_produce_one():
    projection = ad.project_week(_us(power=POWER_EVEN), _them(), today=TODAY)
    assert projection.status == ad.SOURCE_ESTIMATED
    assert projection.outlook == ad.OUTLOOK_TOSSUP
    assert projection.outlook != ad.OUTLOOK_UNASSESSED


# ── Per-day projection ────────────────────────────────────────────────────────


def test_enemy_buster_is_never_projected():
    # FSP is not visible in game and hero-kit counters flip modest stat edges,
    # so day 6 gets a human read or nothing. 4 of the 13 points, deliberately
    # left to a person.
    assert 6 not in ad.DAY_METRICS
    projection = ad.project_week(_us(power=POWER_STRONG), _them(), today=TODAY)
    assert [day.day for day in projection.days] == [1, 2, 3, 4, 5]
    assert projection.buster_line == "No combat read on this alliance yet."


def test_the_day_six_read_is_reported_verbatim_and_never_computed():
    projection = ad.project_week(
        _us(power=POWER_STRONG), _them(known_6="loses fights, never shows up"), today=TODAY
    )
    assert "loses fights, never shows up" in projection.buster_line


def test_a_slight_lean_leaves_every_day_contested():
    # Asymmetric error cost: saying "easy win" and losing under-mobilizes the
    # alliance, which is worse than saying toss-up and winning comfortably.
    projection = ad.project_week(
        _us(power=POWER_SLIGHT, members=MEMBERS_SLIGHT, gift=GIFT_SLIGHT), _them(), today=TODAY
    )
    assert set(_buckets(projection).values()) == {ad.BUCKET_CONTESTED}
    assert (projection.low, projection.high) == (0, ad.GRIND_POINTS_TOTAL)


def test_days_split_both_ways_when_the_metrics_disagree():
    """The reason for voting rather than blending, shown at day level.

    Power favours us and headcount favours them, so they take different days.
    A weighted composite would average that into one number and hide it; the
    per-day split is the actionable half even though the headline is a toss-up.
    """
    projection = ad.project_week(
        _us(power=POWER_STRONG, members=60), _them(members=MEMBERS_STRONG), today=TODAY
    )
    buckets = _buckets(projection)
    assert buckets[1] == ad.BUCKET_FAVORED_THEM  # per-member capped actions
    assert buckets[4] == ad.BUCKET_FAVORED_YOU  # banked shards
    assert buckets[5] == ad.BUCKET_FAVORED_YOU
    assert projection.outlook == ad.OUTLOOK_TOSSUP
    assert (projection.locked_own, projection.contested, projection.locked_them) == (4, 4, 1)
    assert (projection.low, projection.high) == (4, 8)


def test_days_two_and_three_need_a_second_metric_to_agree():
    """Power is the only input that sees the speedup days, and it overstates
    them: a fully maxed account cannot dump construction speedups at all. So a
    strong power lean alone leaves them contested."""
    alone = ad.project_week(_us(power=POWER_STRONG), _them(), today=TODAY)
    assert _buckets(alone)[2] == ad.BUCKET_CONTESTED
    assert _buckets(alone)[3] == ad.BUCKET_CONTESTED
    assert _buckets(alone)[5] == ad.BUCKET_FAVORED_YOU  # no such ceiling on day 5

    corroborated = ad.project_week(_us(power=POWER_STRONG, gift=GIFT_SLIGHT), _them(), today=TODAY)
    assert _buckets(corroborated)[2] == ad.BUCKET_FAVORED_YOU
    assert _buckets(corroborated)[3] == ad.BUCKET_FAVORED_YOU


def test_the_range_is_locked_points_to_locked_plus_contested():
    projection = ad.project_week(_us(power=POWER_STRONG), _them(), today=TODAY)
    assert projection.low == projection.locked_own
    assert projection.high == projection.locked_own + projection.contested
    assert projection.locked_own + projection.contested + projection.locked_them == (
        ad.GRIND_POINTS_TOTAL
    )


def test_the_grind_line_reads_like_the_design_doc():
    projection = ad.project_week(_us(power=POWER_STRONG), _them(), today=TODAY)
    assert projection.grind_line == (
        "You 4 locked, 5 contested, them 0 locked. Projected 4 to 9 of 9. "
        "Power +33%, gifts even, members even."
    )


# ── Staleness ─────────────────────────────────────────────────────────────────


def test_staleness_costs_the_top_confidence_rung():
    inputs = dict(power=POWER_STRONG, gift=GIFT_STRONG, members=MEMBERS_SLIGHT)
    fresh = ad.assess(_us(**inputs), _them(), today=TODAY)
    stale = ad.assess(_us(stamp=ANCIENT, **inputs), _them(stamp=ANCIENT), today=TODAY)

    assert fresh.confidence == ad.CONFIDENCE_CONFIDENT
    assert stale.confidence == ad.CONFIDENCE_MODERATE
    assert stale.stale is True
    # The lean itself survives: a large gap measured months ago is still
    # evidence of a gap. What it loses is the right to sound certain.
    assert stale.direction == fresh.direction


def test_stale_inputs_widen_the_band_by_calling_no_day_at_all():
    inputs = dict(power=POWER_STRONG, gift=GIFT_STRONG, members=MEMBERS_SLIGHT)
    projection = ad.project_week(_us(stamp=ANCIENT, **inputs), _them(stamp=ANCIENT), today=TODAY)

    assert set(_buckets(projection).values()) == {ad.BUCKET_CONTESTED}
    assert (projection.low, projection.high) == (0, ad.GRIND_POINTS_TOTAL)
    assert projection.outlook != ad.OUTLOOK_EASY
    # And it is said out loud, because a confident call from months-old power
    # numbers is the core false-confidence failure mode.
    assert "days old" in projection.caveat_line


def test_one_stale_side_is_enough_to_widen_the_band():
    projection = ad.project_week(
        _us(power=POWER_STRONG, gift=GIFT_STRONG), _them(stamp=ANCIENT), today=TODAY
    )
    assert projection.vote.stale is True


def test_undated_inputs_report_unknown_age_rather_than_assuming_fresh():
    vote = ad.assess(_us(stamp=None), _them(stamp=None), today=TODAY)
    assert vote.age_days is None
    assert vote.stale is False


# ── The outlook ladder ────────────────────────────────────────────────────────


def test_easy_requires_unanimous_strong_agreement():
    easy = ad.project_week(
        _us(power=POWER_STRONG, gift=GIFT_STRONG, members=MEMBERS_SLIGHT), _them(), today=TODAY
    )
    assert easy.outlook == ad.OUTLOOK_EASY

    # One strong metric with the others silent is a lean, not a walkover. This
    # is the Duel-tech guard: against an unmeasured threefold multiplier on
    # points per stockpile spent, a big power edge on its own proves little.
    lean = ad.project_week(_us(power=POWER_STRONG), _them(), today=TODAY)
    assert lean.outlook == ad.OUTLOOK_LIKELY


def test_the_ladder_is_symmetric_about_a_toss_up():
    strong = dict(power=POWER_STRONG, gift=GIFT_STRONG, members=MEMBERS_SLIGHT)
    assert ad.project_week(_us(**strong), _them(), today=TODAY).outlook == ad.OUTLOOK_EASY
    assert ad.project_week(_them(), _us(**strong), today=TODAY).outlook == ad.OUTLOOK_HARD
    assert ad.project_week(_us(power=POWER_STRONG), _them(), today=TODAY).outlook == (
        ad.OUTLOOK_LIKELY
    )
    assert ad.project_week(_them(), _us(power=POWER_STRONG), today=TODAY).outlook == (
        ad.OUTLOOK_UNLIKELY
    )


def test_unassessed_renders_plainly_and_is_never_dressed_up():
    projection = ad.project_week(
        _us(), _profile("XX", power=None, members=None, gift=None), today=TODAY
    )
    assert projection.status == ad.SOURCE_UNASSESSED
    assert projection.outlook == ad.OUTLOOK_UNASSESSED
    assert projection.vote is None
    assert projection.days == ()
    assert "needs power, members and gift level" in projection.grind_line
    # With nothing projected, every grind point is genuinely still open.
    assert (projection.low, projection.high) == (0, ad.GRIND_POINTS_TOTAL)


def test_the_rendered_copy_carries_no_em_dashes():
    tier_0 = _profile("XX", power=None, members=None, gift=None)
    projections = (
        ad.project_week(_us(power=POWER_STRONG), _them(), today=TODAY),
        ad.project_week(_us(), tier_0, today=TODAY),
        ad.project_week(
            _us(power=POWER_STRONG, gift=GIFT_STRONG, known_1_5="weak"),
            _them(known_1_5="very strong", known_6="strong"),
            today=TODAY,
        ),
        ad.project_week(_us(stamp=ANCIENT, power=POWER_STRONG), _them(stamp=ANCIENT), today=TODAY),
    )
    for projection in projections:
        for line in projection.lines:
            assert "—" not in line, line
            assert "–" not in line, line


def test_every_projection_says_it_is_a_ceiling_rather_than_a_forecast():
    for projection in (
        ad.project_week(_us(power=POWER_STRONG), _them(), today=TODAY),
        ad.project_week(_us(), _profile("XX", power=None, members=None, gift=None), today=TODAY),
    ):
        assert ad.CAPACITY_CEILING_NOTE in projection.caveat_line
        assert projection.caveat_line in projection.lines


# ── Known and Picked take priority ────────────────────────────────────────────


def test_a_known_read_outranks_the_computed_estimate_on_disagreement():
    projection = ad.project_week(
        _us(power=POWER_STRONG, gift=GIFT_STRONG, known_1_5="weak"),
        _them(known_1_5="very strong"),
        today=TODAY,
    )
    assert projection.status == ad.SOURCE_KNOWN
    assert projection.outlook == ad.OUTLOOK_UNLIKELY


def test_a_read_the_numbers_contradict_is_said_out_loud():
    projection = ad.project_week(
        _us(power=POWER_STRONG, gift=GIFT_STRONG, known_1_5="weak"),
        _them(known_1_5="very strong"),
        today=TODAY,
    )
    assert projection.overridden is True
    assert "points the other way" in projection.disagreement_line
    assert projection.disagreement_line in projection.lines


def test_a_read_that_merely_agrees_raises_no_disagreement_note():
    projection = ad.project_week(
        _us(power=POWER_STRONG, gift=GIFT_STRONG, known_1_5="very strong"),
        _them(known_1_5="weak"),
        today=TODAY,
    )
    assert projection.overridden is False
    assert projection.disagreement_line == ""


def test_a_human_read_with_no_computed_margin_is_capped_at_likely():
    # Tier 0 opponent: the read supplies a direction but no margin, and "easy"
    # requires a margin.
    projection = ad.project_week(
        _us(known_1_5="very strong"),
        _profile("XX", power=None, members=None, gift=None, known_1_5="very weak"),
        today=TODAY,
    )
    assert projection.status == ad.SOURCE_KNOWN
    assert projection.outlook == ad.OUTLOOK_LIKELY


def test_a_corroborated_read_may_still_reach_easy():
    projection = ad.project_week(
        _us(power=POWER_STRONG, gift=GIFT_STRONG, members=MEMBERS_SLIGHT, known_1_5="very strong"),
        _them(known_1_5="weak"),
        today=TODAY,
    )
    assert projection.status == ad.SOURCE_KNOWN
    assert projection.outlook == ad.OUTLOOK_EASY


def test_a_picked_call_outranks_a_known_read():
    projection = ad.project_week(
        _us(known_1_5="very strong"), _them(known_1_5="weak"), picked="L", today=TODAY
    )
    assert projection.status == ad.SOURCE_PICKED
    assert projection.outlook == ad.OUTLOOK_UNLIKELY


def test_equal_or_unranked_reads_fall_through_to_the_computation():
    both_strong = ad.project_week(
        _us(power=POWER_STRONG, known_1_5="strong"), _them(known_1_5="strong"), today=TODAY
    )
    assert both_strong.status == ad.SOURCE_ESTIMATED

    free_text = ad.project_week(
        _us(power=POWER_STRONG, known_1_5="scary"), _them(known_1_5="dunno"), today=TODAY
    )
    assert free_text.status == ad.SOURCE_ESTIMATED


def test_the_per_day_split_still_renders_under_a_human_override():
    # The two answer different questions — who wins, versus which days are
    # close enough to be worth banking speedups for — so overriding the first
    # must not throw away the second.
    projection = ad.project_week(
        _us(power=POWER_STRONG, gift=GIFT_STRONG, known_1_5="weak"),
        _them(known_1_5="very strong"),
        today=TODAY,
    )
    assert projection.days
    assert _buckets(projection)[5] == ad.BUCKET_FAVORED_YOU


# ── Clinch arithmetic ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "low,high,expected",
    [
        (7, 9, ad.CLINCH_BEFORE_ENEMY_BUSTER),
        (9, 9, ad.CLINCH_BEFORE_ENEMY_BUSTER),
        (0, 2, ad.CLINCH_CONCEDED),
        (3, 6, ad.CLINCH_DAY_SIX_DECIDES),
        (4, 6, ad.CLINCH_DAY_SIX_DECIDES),
        (0, 9, ad.CLINCH_OPEN),
        (4, 8, ad.CLINCH_OPEN),
    ],
)
def test_clinch_outlook_classifies_the_projected_range(low, high, expected):
    # 7 of 9 clinches before Enemy Buster, 2 or fewer concedes it, and the band
    # between is where most real weeks land — which is why the one day no
    # formula can model is usually the deciding day.
    assert ad.clinch_outlook(low, high) == expected


def test_the_verdict_asks_for_a_combat_read_when_the_week_is_still_open():
    open_week = ad.project_week(_us(power=POWER_SLIGHT), _them(), today=TODAY)
    assert open_week.clinch == ad.CLINCH_OPEN
    assert open_week.verdict_line.endswith("Get a read.")

    with_read = ad.project_week(_us(power=POWER_SLIGHT), _them(known_6="weak"), today=TODAY)
    assert not with_read.verdict_line.endswith("Get a read.")


def test_live_clinch_state_answers_the_mid_week_question():
    # The design's worked sentence: 5-3 up with day 5 (2 pts) and Enemy Buster
    # (4 pts) left, and winning day 5 clinches it.
    state = ad.clinch_state({1: "W", 2: "W", 3: "L", 4: "W"})
    assert (state.own_points, state.opponent_points) == (5, 2)
    assert state.remaining_days == (5, 6)
    assert state.remaining_points == 6
    assert state.points_needed == 2
    assert state.clinching_days == (5, 6)
    assert state.decided is False


def test_an_unrecorded_day_is_never_counted_as_a_loss():
    state = ad.clinch_state({1: "W"})
    assert state.opponent_points == 0
    assert state.remaining_days == (2, 3, 4, 5, 6)


def test_clinch_state_knows_when_the_week_is_already_decided():
    won = ad.clinch_state({1: "W", 2: "W", 3: "W", 4: "W"})
    assert won.own_points == 7
    assert won.clinched is True
    assert won.decided is True

    lost = ad.clinch_state({2: "L", 3: "L", 4: "L", 5: "L"})
    assert lost.lost is True
    assert lost.decided is True


def test_enemy_buster_can_reverse_a_grind_day_advantage():
    # 3-2 on the grind days is fully reversed by losing day 6, which is the
    # whole reason the projection reports a range instead of a winner.
    state = ad.clinch_state({1: "W", 2: "W", 3: "L", 4: "L", 5: "L", 6: "L"})
    assert state.own_points == 3
    assert state.opponent_points == 10
    assert state.lost is True


# ── The estimator seam ────────────────────────────────────────────────────────


def _bracket_rows():
    """Sixteen ranked alliances, each clearly stronger than the next."""
    league = ad.LeagueKey("S35", "Diamond", "12 - 2")
    return [
        ad.AllianceWeek(
            league=league,
            week=week,
            alliance=ad.AllianceKey.of(f"AL{i:02d}", "1234"),
            ranking=i + 1,
            power=1_000_000 * (2 ** (ad.BRACKET_SIZE - i)),
            members=MEMBERS_BASE,
            gift_level=GIFT_BASE,
        )
        for week in (1, 2)
        for i in range(ad.BRACKET_SIZE)
    ]


def _play_week_one(rows):
    pairing = ad.compute_week_pairing(rows, 1)
    for match in pairing.matches:
        for side, other, outcome in ((match.a, match.b, "W"), (match.b, match.a, "L")):
            for row in rows:
                if row.week == 1 and row.alliance == side:
                    row.opponent = other
                    row.week_outcome = outcome


def test_the_estimator_declines_rather_than_flipping_a_coin():
    """Declining is the useful behaviour, not a gap.

    An uncalled match stays a named blocker on the path, which is what turns
    the projection into "these three alliances decide your next two opponents".
    Filling it with a guess would trade that list for false precision.
    """
    estimate = ad.make_estimator(
        {p.alliance: p for p in (_us(), _them(), _profile("XX", power=None))}, today=TODAY
    )
    a, b, x = (ad.AllianceKey.of(t, "1234") for t in ("US", "EM", "XX"))
    assert estimate(a, b, 1) is None  # identical stats — a toss-up
    assert estimate(a, x, 1) is None  # short of Tier 1
    assert estimate(a, ad.AllianceKey.of("ZZ", "1234"), 1) is None  # no profile at all


def test_the_estimator_calls_the_side_the_vote_favours():
    profiles = {p.alliance: p for p in (_us(power=POWER_STRONG), _them())}
    estimate = ad.make_estimator(profiles, today=TODAY)
    us, them = ad.AllianceKey.of("US", "1234"), ad.AllianceKey.of("EM", "1234")
    assert estimate(us, them, 1) == us
    assert estimate(them, us, 1) == us  # argument order must not decide it


def test_the_estimator_unblocks_a_path_that_results_alone_cannot_resolve():
    """The seam, end to end: `project_own_path` takes an Estimator, and this is
    the model supplying one."""
    rows = _bracket_rows()
    _play_week_one(rows)
    target = ad.AllianceKey.of("AL00", "1234")

    blocked = ad.project_own_path(target, rows, upto_week=2)
    assert blocked.is_blocked is True

    projected = ad.project_own_path(
        target, rows, upto_week=2, estimate=ad.make_estimator(rows, today=TODAY)
    )
    assert projected.is_blocked is False
    assert projected.steps[1].outcome_source == ad.SOURCE_ESTIMATED
    # Ranking 1 is the strongest alliance in the bracket, so the model calls it.
    assert projected.steps[1].outcome == "W"


def test_the_estimator_does_not_consult_the_week():
    # Profiles are latest-non-blank across every row, so there is one current
    # reading of an alliance rather than a per-week one.
    estimate = ad.make_estimator(
        {p.alliance: p for p in (_us(power=POWER_STRONG), _them())}, today=TODAY
    )
    us, them = ad.AllianceKey.of("US", "1234"), ad.AllianceKey.of("EM", "1234")
    assert {estimate(us, them, week) for week in range(1, ad.LEAGUE_WEEKS + 1)} == {us}

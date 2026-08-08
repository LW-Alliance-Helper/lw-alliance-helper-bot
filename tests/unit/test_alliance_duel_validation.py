"""Unit tests for Alliance Duel (VS) validation and skeleton rows (#399).

The validation suite is Discord-free and Sheets-free, so these run against
constructed rows with no mocks.

The load-bearing behaviour here isn't any individual rule, it's the mode gate:
rules 4, 5 and 6 assume a full 16-alliance bracket, and firing them at an
own-alliance sheet would report a deliberate choice as an error (#448).
"""

import datetime as _dt

import pytest

import alliance_duel as ad


LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
MONDAY = _dt.date(2026, 8, 3)


def _key(i: int) -> ad.AllianceKey:
    return ad.AllianceKey.of(f"AL{i:02d}", "1234")


def _row(week: int, i: int, seed: int | None = None, **kw) -> ad.AllianceWeek:
    kw.setdefault("row_number", 100 + i)
    return ad.AllianceWeek(league=LEAGUE, week=week, alliance=_key(i), seed=seed, **kw)


def _rules(findings) -> list[int]:
    return sorted({f.rule for f in findings})


def _matched_pair(week=1, a=0, b=1, **kw):
    """Two rows facing each other, so the pairwise rules have something to chew."""
    return [
        _row(week, a, a + 1, opponent=_key(b), **kw.get("a", {})),
        _row(week, b, b + 1, opponent=_key(a), **kw.get("b", {})),
    ]


# ── Rule 1: Week Scores total 13 ──────────────────────────────────────────────


def test_rule_1_flags_a_matchup_that_does_not_total_13():
    rows = _matched_pair(a=0, b=1)
    rows[0].week_score = 7
    rows[1].week_score = 7  # 14
    findings = ad.validate(rows)
    assert 1 in _rules(findings)
    finding = next(f for f in findings if f.rule == 1)
    assert finding.severity == ad.SEVERITY_ERROR
    assert "14" in finding.message
    assert finding.column == ad.COL_WEEK_SCORE
    assert finding.row_number == 100


def test_rule_1_accepts_a_correct_split():
    rows = _matched_pair()
    rows[0].week_score = 9
    rows[1].week_score = 4
    assert 1 not in _rules(ad.validate(rows))


def test_rule_1_reports_a_bad_pair_once_not_twice():
    rows = _matched_pair()
    rows[0].week_score = 10
    rows[1].week_score = 10
    assert len([f for f in ad.validate(rows) if f.rule == 1]) == 1


def test_rule_1_stays_quiet_when_only_one_side_is_filled_in():
    rows = _matched_pair()
    rows[0].week_score = 9
    assert 1 not in _rules(ad.validate(rows))


# ── Rule 2: Day Outcomes sum to Week Score ────────────────────────────────────


def test_rule_2_flags_day_outcomes_that_disagree_with_week_score():
    row = _row(
        1, 0, 1, week_score=13, day_outcomes={1: "W", 2: "W", 3: "L", 4: "W", 5: "W", 6: "L"}
    )
    findings = ad.validate([row])
    finding = next(f for f in findings if f.rule == 2)
    assert "7" in finding.message and "13" in finding.message
    assert finding.severity == ad.SEVERITY_ERROR


def test_rule_2_needs_all_six_days_before_it_says_anything():
    # A part-filled week is normal mid-week, not an error.
    row = _row(1, 0, 1, week_score=13, day_outcomes={1: "W", 2: "W"})
    assert 2 not in _rules(ad.validate([row]))


def test_rule_2_accepts_a_consistent_week():
    row = _row(1, 0, 1, week_score=7, day_outcomes={1: "W", 2: "W", 3: "L", 4: "W", 5: "W", 6: "L"})
    assert 2 not in _rules(ad.validate([row]))


# ── Rule 3: Week Outcome agrees with Week Score ───────────────────────────────


@pytest.mark.parametrize(
    "score,outcome,flagged",
    [(9, "W", False), (4, "L", False), (9, "L", True), (4, "W", True), (7, "W", False)],
)
def test_rule_3_outcome_must_match_score(score, outcome, flagged):
    row = _row(1, 0, 1, week_score=score, week_outcome=outcome)
    assert (3 in _rules(ad.validate([row]))) is flagged


def test_rule_3_treats_seven_of_thirteen_as_the_win_threshold():
    # 7 of 13 is the majority; 6 is not.
    assert 3 not in _rules(ad.validate([_row(1, 0, 1, week_score=7, week_outcome="W")]))
    assert 3 in _rules(ad.validate([_row(1, 0, 1, week_score=6, week_outcome="W")]))


# ── Rule 7: Picked calls agree ────────────────────────────────────────────────


def test_rule_7_flags_both_sides_picked_to_win():
    rows = _matched_pair()
    rows[0].picked = "W"
    rows[1].picked = "W"
    findings = [f for f in ad.validate(rows) if f.rule == 7]
    assert len(findings) == 1
    assert findings[0].severity == ad.SEVERITY_WARNING
    assert "win" in findings[0].message


def test_rule_7_accepts_opposing_picks():
    rows = _matched_pair()
    rows[0].picked = "W"
    rows[1].picked = "L"
    assert 7 not in _rules(ad.validate(rows))


# ── Rule 8: suspicious day score magnitude ────────────────────────────────────


def test_rule_8_asks_about_a_score_that_looks_like_a_missing_unit():
    rows = [
        _row(1, 0, 1, day_scores={1: 1_200_000_000, 2: 1_400_000_000, 3: 1_100_000_000}),
        _row(2, 0, 1, day_scores={1: 500}),  # meant 500m
    ]
    findings = [f for f in ad.validate(rows) if f.rule == 8]
    assert len(findings) == 1
    assert findings[0].severity == ad.SEVERITY_WARNING
    assert "500m" in findings[0].message
    assert findings[0].column == ad.day_score_col(1)


def test_rule_8_never_nags_an_early_game_alliance():
    # Small numbers, consistently. This alliance is telling the truth, and an
    # absolute floor would flag every row of it.
    rows = [
        _row(1, 0, 1, day_scores={1: 0, 2: 1_000, 3: 230_000}),
        _row(2, 0, 1, day_scores={1: 500, 2: 2_000}),
    ]
    assert 8 not in _rules(ad.validate(rows))


def test_rule_8_waits_for_a_baseline():
    # Two scores is not enough to call a third one odd.
    rows = [_row(1, 0, 1, day_scores={1: 1_000_000_000, 2: 500})]
    assert 8 not in _rules(ad.validate(rows))


def test_rule_8_leaves_an_ordinary_bad_day_alone():
    rows = [
        _row(1, 0, 1, day_scores={1: 1_000_000_000, 2: 900_000_000, 3: 1_100_000_000}),
        _row(2, 0, 1, day_scores={1: 300_000_000}),  # a poor day, not a typo
    ]
    assert 8 not in _rules(ad.validate(rows))


# ── The mode gate (#448) ──────────────────────────────────────────────────────


def _own_alliance_sheet():
    """What an own-alliance tracker's sheet actually looks like: your rows and
    the opponent you faced, no bracket, no seeds."""
    return [
        ad.AllianceWeek(
            league=LEAGUE,
            week=1,
            alliance=_key(0),
            opponent=_key(9),
            week_score=9,
            week_outcome="W",
            row_number=2,
        )
    ]


def test_own_alliance_mode_does_not_flag_a_deliberate_choice():
    rows = _own_alliance_sheet()
    findings = ad.validate(rows, tracking_mode=ad.MODE_OWN_ALLIANCE, own_alliance=_key(0))
    # Rules 4, 5 and 6 all assume a full bracket. None may fire here.
    assert not {4, 5, 6} & set(_rules(findings)), (
        "own-alliance mode must not report a supported shape as broken"
    )


def test_the_same_sheet_in_full_bracket_mode_does_flag_it():
    # The contrast is the point: identical data, different meaning, because
    # the mode says whether the missing 15 alliances were a choice.
    rows = _own_alliance_sheet()
    findings = ad.validate(rows, tracking_mode=ad.MODE_FULL_BRACKET, own_alliance=_key(0))
    assert 4 in _rules(findings), "a full-bracket sheet missing the opponent's row is an error"


def test_rule_4_flags_a_one_sided_opponent_reference():
    rows = _matched_pair()
    rows[1].opponent = _key(5)  # points somewhere else
    findings = [f for f in ad.validate(rows) if f.rule == 4]
    assert findings
    assert any("disagree" in f.message or "no row of their own" in f.message for f in findings)


def test_rule_5_flags_duplicate_and_out_of_range_seeds():
    rows = [_row(1, 0, 3), _row(1, 1, 3), _row(1, 2, 99)]
    findings = [f for f in ad.validate(rows) if f.rule == 5]
    messages = " ".join(f.message for f in findings)
    assert "used by 2 alliances" in messages
    assert f"outside 1-{ad.BRACKET_SIZE}" in messages


def test_rule_5_is_skipped_in_own_alliance_mode():
    rows = [_row(1, 0, 3), _row(1, 1, 3)]
    findings = ad.validate(rows, tracking_mode=ad.MODE_OWN_ALLIANCE, own_alliance=_key(0))
    assert 5 not in _rules(findings)


def test_rule_6_flags_a_league_your_alliance_is_missing_from():
    rows = [_row(1, 5, 6), _row(1, 6, 7)]
    findings = ad.validate(rows, own_alliance=_key(0))
    finding = next(f for f in findings if f.rule == 6)
    assert finding.severity == ad.SEVERITY_WARNING
    assert "doesn't appear" in finding.message


def test_rule_6_needs_a_configured_own_alliance():
    # Setup not finished: nothing to check against, so say nothing.
    rows = [_row(1, 5, 6)]
    assert 6 not in _rules(ad.validate(rows, own_alliance=None))


def test_rules_that_do_not_assume_a_bracket_still_run_in_own_alliance_mode():
    rows = _own_alliance_sheet()
    rows[0].week_score = 9
    rows[0].week_outcome = "L"  # contradicts the score
    findings = ad.validate(rows, tracking_mode=ad.MODE_OWN_ALLIANCE, own_alliance=_key(0))
    assert 3 in _rules(findings), "own-alliance mode still validates what it can"


# ── Report shape ──────────────────────────────────────────────────────────────


def test_findings_come_back_in_reading_order():
    rows = _matched_pair()
    rows[0].week_score = 10
    rows[1].week_score = 10
    rows[0].week_outcome = "L"
    findings = ad.validate(rows)
    assert [f.rule for f in findings] == sorted(f.rule for f in findings)


def test_a_finding_names_where_to_look():
    rows = [_row(1, 0, 1, week_score=9, week_outcome="L")]
    finding = ad.validate(rows)[0]
    assert finding.where == f"row 100, column {ad.COL_WEEK_OUTCOME}"


def test_a_clean_sheet_produces_nothing():
    rows = _matched_pair()
    rows[0].week_score, rows[0].week_outcome = 9, "W"
    rows[1].week_score, rows[1].week_outcome = 4, "L"
    assert ad.validate(rows, own_alliance=_key(0)) == []


# ── Skeleton rows ─────────────────────────────────────────────────────────────


def _bracket_entries():
    return [(_key(i), i + 1) for i in range(ad.BRACKET_SIZE)]


def test_skeleton_writes_the_whole_bracket_in_full_mode():
    rows = ad.skeleton_rows(LEAGUE, 1, MONDAY, _bracket_entries())
    assert len(rows) == ad.BRACKET_SIZE
    assert all(r.league == LEAGUE and r.week == 1 and r.week_date == MONDAY for r in rows)
    assert sorted(r.seed for r in rows) == list(range(1, ad.BRACKET_SIZE + 1))


def test_skeleton_writes_only_your_rows_in_own_alliance_mode():
    rows = ad.skeleton_rows(
        LEAGUE,
        1,
        MONDAY,
        _bracket_entries(),
        tracking_mode=ad.MODE_OWN_ALLIANCE,
        own_alliance=_key(3),
    )
    assert [r.alliance for r in rows] == [_key(3)]


def test_skeleton_in_own_alliance_mode_without_a_configured_alliance_writes_nothing():
    # Better to write nothing than to guess which of the 16 rows is theirs.
    rows = ad.skeleton_rows(
        LEAGUE, 1, MONDAY, _bracket_entries(), tracking_mode=ad.MODE_OWN_ALLIANCE
    )
    assert rows == []


def test_skeleton_rows_upsert_without_clobbering():
    # The skeleton must be safe to regenerate over a part-filled sheet.
    rows = ad.skeleton_rows(LEAGUE, 1, MONDAY, _bracket_entries()[:2])
    values = [list(ad.SHEET_COLUMNS)]
    plan = ad.plan_upsert(values, rows)
    assert len(plan.appends) == 2
    assert plan.updates == ()


# ── Week 1 pairing from seeds ─────────────────────────────────────────────────


def test_week_one_pairs_one_two_three_four():
    pairing = ad.week_one_pairing_from_seeds(_bracket_entries())
    assert pairing[_key(0)] == _key(1)
    assert pairing[_key(2)] == _key(3)
    assert pairing[_key(14)] == _key(15)
    assert len(pairing) == ad.BRACKET_SIZE


def test_week_one_pairing_agrees_with_compute_week_pairing():
    # Same answer as the real algorithm, which is what lets setup pre-fill
    # Opponent before any result exists.
    entries = _bracket_entries()
    from_seeds = ad.week_one_pairing_from_seeds(entries)
    computed = ad.compute_week_pairing(
        [ad.AllianceWeek(league=LEAGUE, week=1, alliance=k, seed=s) for k, s in entries], 1
    )
    for match in computed.matches:
        assert from_seeds[match.a] == match.b


def test_week_one_pairing_skips_unseeded_alliances():
    entries = [(_key(0), 1), (_key(1), None), (_key(2), 2)]
    pairing = ad.week_one_pairing_from_seeds(entries)
    assert pairing == {_key(0): _key(2), _key(2): _key(0)}

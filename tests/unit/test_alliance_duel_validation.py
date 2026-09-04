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


def _row(week: int, i: int, ranking: int | None = None, **kw) -> ad.AllianceWeek:
    kw.setdefault("row_number", 100 + i)
    return ad.AllianceWeek(league=LEAGUE, week=week, alliance=_key(i), ranking=ranking, **kw)


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
    the opponent you faced, no bracket, no rankings."""
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


def test_rule_5_flags_duplicate_and_out_of_range_rankings():
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
    assert sorted(r.ranking for r in rows) == list(range(1, ad.BRACKET_SIZE + 1))


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


# ── Week 1 pairing from rankings ─────────────────────────────────────────────────


def test_week_one_pairs_one_two_three_four():
    pairing = ad.week_one_pairing_from_rankings(_bracket_entries())
    assert pairing[_key(0)] == _key(1)
    assert pairing[_key(2)] == _key(3)
    assert pairing[_key(14)] == _key(15)
    assert len(pairing) == ad.BRACKET_SIZE


def test_week_one_pairing_agrees_with_compute_week_pairing():
    # Same answer as the real algorithm, which is what lets setup pre-fill
    # Opponent before any result exists.
    entries = _bracket_entries()
    from_rankings = ad.week_one_pairing_from_rankings(entries)
    computed = ad.compute_week_pairing(
        [ad.AllianceWeek(league=LEAGUE, week=1, alliance=k, ranking=s) for k, s in entries], 1
    )
    for match in computed.matches:
        assert from_rankings[match.a] == match.b


def test_week_one_pairing_skips_unranked_alliances():
    entries = [(_key(0), 1), (_key(1), None), (_key(2), 2)]
    pairing = ad.week_one_pairing_from_rankings(entries)
    assert pairing == {_key(0): _key(2), _key(2): _key(0)}


# ── Switching own-alliance to full bracket mid-league (#448) ──────────────────


def test_missing_bracket_rows_counts_only_weeks_that_exist():
    # An alliance two weeks into a league is not asking for four weeks of rows.
    rows = [_row(1, 0), _row(1, 9), _row(2, 0), _row(2, 9)]
    missing = ad.missing_bracket_rows(rows, LEAGUE)
    assert sorted(missing) == [1, 2]
    assert missing[1][0] == ad.BRACKET_SIZE - 2
    assert missing[2][0] == ad.BRACKET_SIZE - 2


def test_missing_bracket_rows_skips_weeks_already_full():
    rows = [_row(1, i, i + 1) for i in range(ad.BRACKET_SIZE)]
    rows += [_row(2, 0), _row(2, 9)]
    missing = ad.missing_bracket_rows(rows, LEAGUE)
    assert 1 not in missing, "a full week needs nothing"
    assert missing[2][0] == ad.BRACKET_SIZE - 2


def test_missing_bracket_rows_carries_the_week_date_forward():
    rows = [_row(1, 0, week_date=MONDAY), _row(1, 9)]
    assert ad.missing_bracket_rows(rows, LEAGUE)[1][1] == MONDAY


def test_a_full_bracket_needs_nothing():
    rows = [_row(1, i, i + 1) for i in range(ad.BRACKET_SIZE)]
    assert ad.missing_bracket_rows(rows, LEAGUE) == {}


def test_latest_league_follows_the_newest_week_date():
    other = ad.LeagueKey("S36", "Diamond", "12 - 2")
    rows = [
        _row(1, 0, week_date=MONDAY),
        ad.AllianceWeek(
            league=other, week=1, alliance=_key(0), week_date=MONDAY + _dt.timedelta(days=28)
        ),
    ]
    assert ad.latest_league(rows) == other


def test_latest_league_falls_back_when_nothing_is_dated():
    rows = [_row(1, 0)]
    assert ad.latest_league(rows) == LEAGUE
    assert ad.latest_league([]) is None


def test_blank_rows_stamp_the_league_but_never_invent_an_alliance():
    header = list(ad.SHEET_COLUMNS)
    line = ad.blank_bracket_values(header, LEAGUE, 2, MONDAY)
    assert line[header.index(ad.COL_SEASON)] == "S35"
    assert line[header.index(ad.COL_TIER)] == "Diamond"
    assert line[header.index(ad.COL_WEEK)] == "2"
    assert line[header.index(ad.COL_WEEK_DATE)] == MONDAY.isoformat()
    # The bot cannot know who the other fifteen are. Those come off the
    # in-game bracket screen.
    assert line[header.index(ad.COL_TAG)] == ""
    assert line[header.index(ad.COL_WARZONE)] == ""
    assert line[header.index(ad.COL_RANKING)] == ""


def test_blank_rows_follow_a_reordered_header():
    header = [ad.COL_WEEK, "My Own Column", ad.COL_SEASON, ad.COL_TIER, ad.COL_GROUP]
    line = ad.blank_bracket_values(header, LEAGUE, 3, None)
    assert line[0] == "3"
    assert line[2] == "S35"
    assert line[1] == "", "a user's own column is never written into"


def test_blank_rows_omit_a_date_that_is_not_known():
    header = list(ad.SHEET_COLUMNS)
    line = ad.blank_bracket_values(header, LEAGUE, 1, None)
    assert line[header.index(ad.COL_WEEK_DATE)] == ""


# ── Rule 9: a bracket holds sixteen, never more ───────────────────────────────


def _full_league(ranked=ad.BRACKET_SIZE):
    """Week 1 rows for a complete bracket, rankings 1..16."""
    return [_row(1, i, i + 1) for i in range(ranked)]


def test_a_full_bracket_of_sixteen_raises_no_roster_finding():
    assert 9 not in _rules(ad.validate(_full_league()))


def test_a_part_filled_bracket_is_not_reported_as_wrong():
    # Twelve typed so far is what entry looks like halfway through, and the
    # bracket and path surfaces already say the roster is short. Nagging here
    # would make "Check my sheet" argue with work in progress.
    assert 9 not in _rules(ad.validate(_full_league(12)))


def test_a_seventeenth_alliance_is_reported_once():
    # The failure this exists for: one tag entered two ways (a capital i and a
    # lowercase L read alike), which reaches the sheet through Discord with no
    # Ranking, and which rule 5 skips precisely because it has no Ranking.
    rows = _full_league() + [_row(1, 99)]
    findings = [f for f in ad.validate(rows) if f.rule == 9]
    assert len(findings) == 1
    assert "16 alliances and you have entered 17" in findings[0].message
    assert findings[0].severity == ad.SEVERITY_ERROR


def test_the_oversized_league_finding_names_nobody():
    """The bot can rank which row looks likeliest to be the extra. It cannot
    know, and pointing at one would be an opinion about which of the alliance's
    own entries is wrong (`UX.md` principle 6). The reader does the picking."""
    rows = _full_league() + [_row(1, 99)]
    findings = [f for f in ad.validate(rows) if f.rule == 9]
    assert findings[0].alliance is None
    assert findings[0].row_number is None
    for tag in ("AL99", "AL00"):
        assert tag not in findings[0].message


def test_the_unranked_intruder_is_invisible_to_the_ranking_rule():
    # Standing proof of why rule 9 had to exist rather than rule 5 being widened.
    rows = _full_league() + [_row(1, 99)]
    assert 5 not in _rules(ad.validate(rows))


def test_a_seventeenth_ranked_alliance_reports_the_count_once():
    # Seventeen alliances cannot hold sixteen distinct rankings, so rule 5 names
    # the collision and rule 9 only has to state the count.
    rows = _full_league() + [_row(1, 99, 4)]
    findings = [f for f in ad.validate(rows) if f.rule == 9]
    assert len(findings) == 1
    assert findings[0].alliance is None
    assert 5 in _rules(ad.validate(rows))


def test_the_roster_rule_does_not_run_in_own_alliance_mode():
    rows = _full_league() + [_row(1, 99)]
    findings = ad.validate(rows, tracking_mode=ad.MODE_OWN_ALLIANCE)
    assert 9 not in _rules(findings)


def test_one_league_being_oversized_does_not_flag_another():
    other = ad.LeagueKey("S36", "Diamond", "12 - 1")
    rows = _full_league() + [_row(1, 99)]
    rows += [
        ad.AllianceWeek(league=other, week=1, alliance=_key(i), ranking=i + 1) for i in range(4)
    ]
    findings = [f for f in ad.validate(rows) if f.rule == 9]
    assert len(findings) == 1
    assert "entered 17" in findings[0].message


# ── Reading a typed-in bracket ────────────────────────────────────────────────


def _bracket_text(n=ad.BRACKET_SIZE, start=0):
    return "\n".join(f"AL{i:02d} 1234" for i in range(start, start + n))


def test_a_pasted_bracket_takes_its_rankings_from_line_order():
    parse = ad.parse_bracket(_bracket_text())
    assert parse.ok
    assert [e.ranking for e in parse.entries] == list(range(1, ad.BRACKET_SIZE + 1))
    assert parse.entries[0].alliance == _key(0)


def test_the_shapes_an_officer_actually_pastes_all_read():
    parse = ad.parse_bracket(
        "[kTZ] 714\nIMI,685\nRudi\t716\n" + "\n".join(f"AL{i:02d} 1234" for i in range(13))
    )
    assert parse.ok, parse.problems
    assert [e.tag_display for e in parse.entries[:3]] == ["KTZ", "IMI", "RUDI"]


def test_blank_lines_between_alliances_are_not_rankings():
    parse = ad.parse_bracket("\n\n".join(f"AL{i:02d} 1234" for i in range(ad.BRACKET_SIZE)))
    assert parse.ok
    assert [e.ranking for e in parse.entries] == list(range(1, ad.BRACKET_SIZE + 1))


def test_a_stated_ranking_is_checked_against_its_position_not_trusted():
    # A paste that arrived out of order is the mistake worth catching; honouring
    # the number would hide it behind a bracket that looks fine.
    parse = ad.parse_bracket("1 AL00 1234\n3 AL01 1234\n" + _bracket_text(14, start=2))
    assert not parse.ok
    assert parse.problems == (
        "Line 2 is numbered 3. Lines are read in ranking order, so this "
        "one is ranking 2. Reorder them or drop the numbers.",
    )


def test_a_stated_ranking_that_agrees_with_its_position_is_accepted():
    text = "\n".join(f"{i + 1} AL{i:02d} 1234" for i in range(ad.BRACKET_SIZE))
    parse = ad.parse_bracket(text)
    assert parse.ok, parse.problems
    assert [e.ranking for e in parse.entries] == list(range(1, ad.BRACKET_SIZE + 1))


def test_a_line_missing_its_warzone_names_the_line():
    parse = ad.parse_bracket("AL00\n" + _bracket_text(15))
    assert not parse.ok
    assert "Line 1" in parse.problems[0]


def test_the_same_alliance_twice_names_both_lines():
    parse = ad.parse_bracket("AL00 1234\nAL00 1234\n" + _bracket_text(14, start=1))
    assert not parse.ok
    assert len(parse.problems) == 1
    assert "Line 2 repeats" in parse.problems[0]
    assert "line 1" in parse.problems[0]


def test_one_tag_in_two_warzones_is_two_alliances():
    # Tags are not unique across warzones, so this is legitimate, not a repeat.
    parse = ad.parse_bracket("AL00 1234\nAL00 5678\n" + _bracket_text(14, start=1))
    assert parse.ok, parse.problems


def test_a_short_bracket_is_counted_not_guessed_at():
    parse = ad.parse_bracket(_bracket_text(15))
    assert not parse.ok
    assert "15 alliances" in parse.problems[0]


def test_typos_are_reported_ahead_of_the_count_they_cause():
    # Fifteen good lines and one bad one is one mistake, and reporting it as
    # two ("...and the bracket is short") sends the reader looking for a
    # second problem that was never there.
    parse = ad.parse_bracket("AL00\n" + _bracket_text(15))
    assert len(parse.problems) == 1


def test_own_alliance_mode_asks_for_one_line():
    parse = ad.parse_bracket("AL00 1234", expect=1)
    assert parse.ok
    assert parse.entries[0].ranking == 1


# ── Power, gift level and members on the bracket lines ────────────────────────


def test_a_line_carries_power_gift_and_members_in_that_order():
    parse = ad.parse_bracket("AL00 1234 26853240157 25 100\n" + _bracket_text(15, start=1))
    assert parse.ok, parse.problems
    first = parse.entries[0]
    assert (first.power, first.gift_level, first.members) == (26853240157, 25, 100)


def test_the_trailing_fields_may_simply_stop():
    parse = ad.parse_bracket(
        "AL00 1234\nAL01 1234 30b\nAL02 1234 30b 25\n" + _bracket_text(13, start=3)
    )
    assert parse.ok, parse.problems
    a, b, c = parse.entries[:3]
    assert (a.power, a.gift_level, a.members) == (None, None, None)
    assert (b.power, b.gift_level, b.members) == (30_000_000_000, None, None)
    assert (c.power, c.gift_level, c.members) == (30_000_000_000, 25, None)


def test_power_on_a_bracket_line_reads_like_power_everywhere_else():
    # `301` is 301M by the survey convention, and a full in-game figure is
    # already raw. Same parser, so the two conventions cannot drift apart.
    parse = ad.parse_bracket("AL00 1234 301\nAL01 1234 26853240157\n" + _bracket_text(14, start=2))
    assert parse.ok, parse.problems
    assert parse.entries[0].power == 301_000_000
    assert parse.entries[1].power == 26853240157


def test_an_unreadable_extra_names_the_line_and_the_field():
    parse = ad.parse_bracket("AL00 1234 26.8b twenty-five\n" + _bracket_text(15, start=1))
    assert not parse.ok
    assert parse.problems == ("Line 1: I could not read `twenty-five` as gift level.",)


def test_a_sixth_field_is_refused_rather_than_ignored():
    parse = ad.parse_bracket("AL00 1234 26.8b 25 100 extra\n" + _bracket_text(15, start=1))
    assert not parse.ok
    assert "Line 1" in parse.problems[0]


def test_a_numeric_tag_is_not_mistaken_for_a_ranking_number():
    # `1 714 26.8b` is an alliance whose tag is "1", not line 1 of a numbered
    # paste. A tag carries at least one non-digit, which is what tells them apart.
    parse = ad.parse_bracket("1 714 26853240157\n" + _bracket_text(15, start=1))
    assert parse.ok, parse.problems
    assert parse.entries[0].alliance == ad.AllianceKey.of("1", "714")
    assert parse.entries[0].power == 26853240157


def test_a_numbered_line_still_reads_when_it_carries_the_extras():
    parse = ad.parse_bracket("1 AL00 1234 26.8b 25 100\n" + _bracket_text(15, start=1))
    assert parse.ok, parse.problems
    assert parse.entries[0].members == 100

"""Unit tests for the Alliance Duel (VS) core data layer and pairing (#400).

No Discord or Sheets mocks required — `alliance_duel.py` is a Discord-free
core, and the sheet-facing half is split into a pure `plan_upsert` (tested
here against literal grids) and a thin `apply_upsert` that only batches the
plan out to gspread.

The centrepiece is `test_projection_agrees_with_pairing_on_random_leagues`:
the design deliberately implements the bracket twice — once by re-sorting on
weighted score, once by walking the lineage — and this asserts they agree on
randomized fully-resolved leagues rather than trusting the derivation.
"""

import datetime as _dt
import random

import pytest

import alliance_duel as ad


LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")


def _key(i: int) -> ad.AllianceKey:
    return ad.AllianceKey.of(f"AL{i:02d}", "1234")


def _row(week: int, i: int, seed: int | None = None, **kw) -> ad.AllianceWeek:
    return ad.AllianceWeek(league=LEAGUE, week=week, alliance=_key(i), seed=seed, **kw)


# ── Fixed game constants ──────────────────────────────────────────────────────


def test_day_table_matches_the_game():
    assert [d.number for d in ad.DUEL_DAYS] == [1, 2, 3, 4, 5, 6]
    # Monday (0) through Saturday (5); Sunday is off and has no entry.
    assert [d.weekday for d in ad.DUEL_DAYS] == [0, 1, 2, 3, 4, 5]
    assert [d.points for d in ad.DUEL_DAYS] == [1, 2, 2, 2, 2, 4]
    assert [d.theme for d in ad.DUEL_DAYS] == [
        "Radar Training",
        "Base Expansion",
        "Age of Science",
        "Train Heroes",
        "Total Mobilization",
        "Enemy Buster",
    ]


def test_point_totals_drive_the_clinch_math():
    assert sum(d.points for d in ad.DUEL_DAYS) == ad.WEEK_POINTS_TOTAL == 13
    assert ad.WEEK_POINTS_MAJORITY == 7
    # The consequence the whole prediction design hangs on: grind days are 9,
    # Enemy Buster is 4, so a 3-2 grind advantage is reversed by losing day 6.
    assert ad.GRIND_POINTS_TOTAL == 9
    assert ad.ENEMY_BUSTER_POINTS == 4
    assert ad.GRIND_POINTS_TOTAL + ad.ENEMY_BUSTER_POINTS == ad.WEEK_POINTS_TOTAL


def test_week_weights_make_the_cohort_split_permanent():
    # Each week's weight must exceed the sum of every later weight — that is
    # what lets project_own_path walk the lineage instead of re-ranking.
    for i, weight in enumerate(ad.WEEK_WEIGHTS):
        assert weight > sum(ad.WEEK_WEIGHTS[i + 1 :])


@pytest.mark.parametrize(
    "tier,expected",
    [("Diamond", 2), ("Gold", 1), ("Silver", 0), ("diamond tier", 2), ("", None), ("Bronze", None)],
)
def test_tier_rank_orders_diamond_over_gold_over_silver(tier, expected):
    assert ad.tier_rank(tier) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("strong", 3), ("Very Strong", 4), ("avg", 2), ("weak", 1), ("", None), ("dunno", None)],
)
def test_known_rank_only_ranks_the_known_vocabulary(value, expected):
    assert ad.known_rank(value) == expected


# ── Date resolution (server time) ─────────────────────────────────────────────


def test_server_today_uses_server_time_not_utc():
    # 2026-08-10 01:00 UTC is still 2026-08-09 23:00 on server time (UTC-2).
    # A guild in UTC+10 would call this Monday and misfile a whole day of
    # scores — the #330 / #318 bug class.
    utc_monday = _dt.datetime(2026, 8, 10, 1, 0, tzinfo=_dt.timezone.utc)
    assert ad.server_today(utc_monday) == _dt.date(2026, 8, 9)
    assert ad.duel_day_for_date(ad.server_today(utc_monday)) is None  # Sunday, off


def test_server_today_rejects_a_naive_datetime():
    with pytest.raises(ValueError):
        ad.server_today(_dt.datetime(2026, 8, 10, 1, 0))


@pytest.mark.parametrize(
    "day,expected",
    [(3, 1), (4, 2), (5, 3), (6, 4), (7, 5), (8, 6), (9, None)],
)
def test_duel_day_for_date(day, expected):
    # 2026-08-03 is a Monday; 2026-08-09 the Sunday that closes the week.
    assert ad.duel_day_for_date(_dt.date(2026, 8, day)) == expected


def test_week_monday_puts_sunday_at_the_end_of_its_week():
    monday = _dt.date(2026, 8, 3)
    for offset in range(7):
        assert ad.week_monday(monday + _dt.timedelta(days=offset)) == monday
    # Sunday resolves back to that Monday, not forward to the next one, so
    # Sunday's prompt covers that week's Saturday Enemy Buster.
    assert ad.week_monday(_dt.date(2026, 8, 9)) == monday


def test_resolve_live_week_finds_the_week_covering_today():
    rows = [
        _row(1, 0, 1, week_date=_dt.date(2026, 8, 3)),
        _row(2, 0, 1, week_date=_dt.date(2026, 8, 10)),
    ]
    live = ad.resolve_live_week(rows, _dt.date(2026, 8, 12))  # Wednesday of week 2
    assert live is not None
    assert (live.week, live.day, live.theme) == (2, 3, "Age of Science")
    assert live.is_rest_day is False

    sunday = ad.resolve_live_week(rows, _dt.date(2026, 8, 16))
    assert sunday.week == 2 and sunday.is_rest_day

    assert ad.resolve_live_week(rows, _dt.date(2026, 9, 21)) is None  # between leagues


def test_week_date_for_and_league_completion():
    assert ad.week_date_for(_dt.date(2026, 8, 5), 3) == _dt.date(2026, 8, 17)
    rows = [_row(w, 0, 1, week_outcome="W") for w in range(1, 4)]
    assert ad.is_league_complete(rows, LEAGUE) is False
    rows.append(_row(4, 0, 1, week_outcome="L"))
    assert ad.is_league_complete(rows, LEAGUE) is True


# ── Value coercion ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("301", 301_000_000), ("300m", 300_000_000), ("1.2b", 1_200_000_000), ("", None)],
)
def test_parse_power_uses_the_survey_shorthand(raw, expected):
    assert ad.parse_power(raw) == expected


def test_parse_power_treats_a_full_number_as_raw():
    assert ad.parse_power("304,743,912") == 304_743_912


def test_parse_score_takes_bare_numbers_literally():
    # An established alliance posts 500m-5b on a day, which makes shorthand
    # tempting. It is still wrong: an early-game alliance legitimately posts
    # these, and scaling them would multiply a real score by a million.
    assert ad.parse_score("0") == 0
    assert ad.parse_score("1000") == 1_000
    assert ad.parse_score("230000") == 230_000
    # A big alliance types the unit or the full number instead.
    assert ad.parse_score("500m") == 500_000_000
    assert ad.parse_score("1.2b") == 1_200_000_000
    assert ad.parse_score("5,000,000,000") == 5_000_000_000


def test_power_and_scores_read_a_bare_number_differently():
    # The one place the two parsers must not converge: power keeps the survey
    # shorthand convention, scores do not.
    assert ad.parse_power("500") == 500_000_000
    assert ad.parse_score("500") == 500


@pytest.mark.parametrize("raw", ["W", "w", "win", "Won", "1", "yes"])
def test_parse_outcome_wins(raw):
    assert ad.parse_outcome(raw) == "W"


@pytest.mark.parametrize("raw", ["L", "loss", "lost", "0", "no"])
def test_parse_outcome_losses(raw):
    assert ad.parse_outcome(raw) == "L"


def test_parse_outcome_leaves_junk_alone():
    # A typo is a validation finding (#399), not something to coerce here.
    assert ad.parse_outcome("maybe") is None
    assert ad.parse_outcome("") is None


@pytest.mark.parametrize(
    "raw,expected",
    [("Push", "push"), ("saving", "save"), ("none", "none"), ("", None), ("???", None)],
)
def test_parse_intent(raw, expected):
    assert ad.parse_intent(raw) == expected


def test_parse_week_date_reads_what_the_bot_writes_and_what_users_type():
    assert ad.parse_week_date("2026-08-03") == _dt.date(2026, 8, 3)
    assert ad.parse_week_date("8/3/2026") == _dt.date(2026, 8, 3)
    assert ad.parse_week_date(_dt.date(2026, 8, 3)) == _dt.date(2026, 8, 3)
    assert ad.parse_week_date("") is None


def test_alliance_key_normalises_tag_and_server():
    assert _key(1) == ad.AllianceKey.of("[al01]", "1234")
    assert ad.AllianceKey.of("AL01", "#1234") == ad.AllianceKey.of("al01", "1234")
    assert ad.AllianceKey.of("", "1234") is None
    assert ad.AllianceKey.of("AL01", "") is None


def test_the_identity_column_is_warzone_not_server():
    """Pins the naming decision, because it is invisible until it isn't.

    Players say "server" colloquially, but `server` is reserved product-wide
    for the *Discord* server (UX.md glossary). Warzone is the game's own word
    for a world, so it carries no second meaning. Renaming the column back
    would put one term's opposite sense on every VS surface.
    """
    assert ad.COL_WARZONE == "Warzone"
    assert ad.COL_OPPONENT_WARZONE == "Opponent Warzone"
    assert "Server" not in ad.SHEET_COLUMNS
    assert not any("Server" in c for c in ad.SHEET_COLUMNS)


def test_server_time_resolution_is_untouched_by_the_warzone_rename():
    # The two senses of "server" live in one module. This asserts the
    # date-resolution half still means the game clock.
    utc = _dt.datetime(2026, 8, 10, 1, 0, tzinfo=_dt.timezone.utc)
    assert ad.server_today(utc) == _dt.date(2026, 8, 9)


def test_league_key_needs_a_season_and_ranks_its_tier():
    assert ad.LeagueKey.of("", "Diamond", "12 - 2") is None
    assert ad.LeagueKey.of("S35", "Diamond", "12 - 2").rank == 2


# ── Sheet I/O ─────────────────────────────────────────────────────────────────


def _grid(*extra_rows, headers=None):
    header = list(headers or ad.SHEET_COLUMNS)
    return [header, *extra_rows]


def _blank(headers=None):
    return [""] * len(list(headers or ad.SHEET_COLUMNS))


def _cell(row, header, value, headers=None):
    row[list(headers or ad.SHEET_COLUMNS).index(header)] = value
    return row


def test_parse_rows_addresses_columns_by_name_not_position():
    # Reordered, with an extra user column inserted in the middle.
    headers = [ad.COL_TAG, "My Own Notes", ad.COL_WARZONE, ad.COL_WEEK, ad.COL_SEASON, ad.COL_POWER]
    values = [headers, ["AL01", "ignore me", "1234", "2", "S35", "301"]]
    rows = ad.parse_rows(values)
    assert len(rows) == 1
    assert rows[0].alliance == _key(1)
    assert rows[0].week == 2
    assert rows[0].power == 301_000_000
    assert rows[0].row_number == 2


def _identified(week="1", tag="AL01", season="S35"):
    row = _blank()
    _cell(row, ad.COL_SEASON, season)
    _cell(row, ad.COL_TAG, tag)
    _cell(row, ad.COL_WARZONE, "1234")
    _cell(row, ad.COL_WEEK, week)
    return row


def test_parse_rows_skips_rows_without_an_identity():
    values = _grid(_blank(), _identified(), _identified(season=""))
    rows = ad.parse_rows(values)
    # The blank spacer row is normal in a hand-maintained sheet, not an error,
    # and neither is a row someone started but hasn't given a league yet.
    assert len(rows) == 1
    assert rows[0].row_number == 3


def test_parse_rows_reads_day_scores_and_outcomes():
    row = _blank()
    for header, value in (
        (ad.COL_SEASON, "S35"),
        (ad.COL_TAG, "AL01"),
        (ad.COL_WARZONE, "1234"),
        (ad.COL_WEEK, "1"),
        (ad.day_score_col(1), "12,500"),
        (ad.day_outcome_col(1), "W"),
        (ad.day_outcome_col(6), "L"),
        (ad.COL_WEEK_SCORE, "9"),
        (ad.COL_WEEK_OUTCOME, "W"),
    ):
        _cell(row, header, value)
    parsed = ad.parse_rows(_grid(row))[0]
    assert parsed.day_scores == {1: 12_500}
    assert parsed.day_outcomes == {1: "W", 6: "L"}
    assert parsed.week_score == 9
    assert parsed.won is True
    assert parsed.day_points(1) == 1 and parsed.day_points(6) == 0
    assert parsed.has_all_day_outcomes is False


def test_day_points_total_is_the_free_validation_check():
    row = _row(1, 0, 1, day_outcomes={1: "W", 2: "W", 3: "L", 4: "W", 5: "W", 6: "L"})
    assert row.has_all_day_outcomes is True
    assert row.day_points_total == 1 + 2 + 2 + 2  # 7
    assert row.day_points_total != 13


def test_plan_upsert_touches_only_the_matched_rows_own_cells():
    headers = list(ad.SHEET_COLUMNS)
    mine = _blank()
    for header, value in (
        (ad.COL_SEASON, "S35"),
        (ad.COL_TIER, "Diamond"),
        (ad.COL_GROUP, "12 - 2"),
        (ad.COL_WEEK, "1"),
        (ad.COL_TAG, "AL00"),
        (ad.COL_WARZONE, "1234"),
    ):
        _cell(mine, header, value)
    neighbour = list(mine)
    _cell(neighbour, ad.COL_TAG, "AL01")
    _cell(neighbour, ad.COL_NOTES, "hand-typed, must survive")
    values = _grid(mine, neighbour)

    plan = ad.plan_upsert(values, [_row(1, 0, 1, week_outcome="W")])

    assert plan.appends == ()
    written_rows = {u.a1.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for u in plan.updates}
    assert written_rows == {"2"}  # row 3 (the neighbour) is never addressed
    outcome_col = ad.transfer.col_index_to_letter(headers.index(ad.COL_WEEK_OUTCOME))
    assert ad.CellUpdate(f"{outcome_col}2", "W") in plan.updates


def test_plan_upsert_never_writes_a_column_the_caller_has_nothing_for():
    values = _grid()
    plan = ad.plan_upsert(values, [_row(1, 0, 1)])
    appended = plan.appends[0]
    headers = list(ad.SHEET_COLUMNS)
    # Nothing was said about Power or Notes, so those cells stay empty rather
    # than being written as blanks over whatever the user might have there.
    assert appended[headers.index(ad.COL_POWER)] == ""
    assert appended[headers.index(ad.COL_NOTES)] == ""
    assert appended[headers.index(ad.COL_SEED)] == "1"


def test_plan_upsert_appends_each_new_key_once():
    values = _grid()
    plan = ad.plan_upsert(values, [_row(1, 0, 1), _row(1, 0, 1, week_outcome="W")])
    assert len(plan.appends) == 1
    assert len(plan.updates) >= 1  # the second write lands on the appended row


def test_plan_upsert_surfaces_a_renamed_header_instead_of_dropping_writes():
    headers = [h for h in ad.SHEET_COLUMNS if h != ad.COL_WEEK_OUTCOME]
    plan = ad.plan_upsert(_grid(headers=headers), [_row(1, 0, 1, week_outcome="W")])
    assert ad.COL_WEEK_OUTCOME in plan.unmapped_columns


def test_apply_upsert_batches_rather_than_writing_per_cell():
    calls = {"batch": 0, "append": 0}

    class FakeWorksheet:
        def batch_update(self, data, **kw):
            calls["batch"] += 1
            assert len(data) > 1

        def append_rows(self, rows, **kw):
            calls["append"] += 1
            assert len(rows) == 2

    plan = ad.plan_upsert(_grid(), [_row(1, 0, 1), _row(1, 1, 2)])
    # Both are new, so this is one append call and no updates.
    ad.apply_upsert(FakeWorksheet(), plan)
    assert calls == {"batch": 0, "append": 1}


def test_build_profile_takes_the_latest_non_blank_value():
    rows = [
        _row(1, 0, 1, week_date=_dt.date(2026, 8, 3), power=300_000_000, members=98, gift_level=40),
        _row(2, 0, 1, week_date=_dt.date(2026, 8, 10)),  # nobody re-scouted
        _row(3, 0, 1, week_date=_dt.date(2026, 8, 17), power=330_000_000, known_1_5="strong"),
    ]
    profile = ad.build_profile(rows, _key(0))
    assert profile.power == 330_000_000
    assert profile.members == 98  # blank weeks don't erase it
    assert profile.gift_level == 40
    assert profile.known_1_5 == "strong"
    assert profile.is_tier_1 is True
    assert profile.as_of["power"] == _dt.date(2026, 8, 17)
    assert profile.as_of["members"] == _dt.date(2026, 8, 3)  # staleness stays visible
    assert profile.power_history == (
        (_dt.date(2026, 8, 3), 300_000_000),
        (_dt.date(2026, 8, 17), 330_000_000),
    )


# ── Pairing ───────────────────────────────────────────────────────────────────


def _full_bracket(weeks=1):
    """Sixteen seeded alliances with skeleton rows for `weeks` weeks."""
    return [_row(w, i, i + 1) for w in range(1, weeks + 1) for i in range(ad.BRACKET_SIZE)]


def test_week_one_pairs_straight_off_the_seeds():
    pairing = ad.compute_week_pairing(_full_bracket(), 1)
    assert isinstance(pairing, ad.WeekPairing)
    assert [(m.a, m.b) for m in pairing.matches] == [
        (_key(i), _key(i + 1)) for i in range(0, ad.BRACKET_SIZE, 2)
    ]
    assert pairing.rematches == ()


def test_weighted_score_counts_confirmed_wins_only():
    rows = [
        _row(1, 0, 1, week_outcome="W"),
        _row(2, 0, 1, week_outcome="L"),
        _row(3, 0, 1),  # unrecorded — not a loss
    ]
    assert ad.weighted_score(rows, _key(0), 4) == 8
    assert ad.weighted_score(rows, _key(0), 2) == 8
    assert ad.weighted_score(rows, _key(0), 1) == 0


def test_week_two_splits_winners_from_losers():
    rows = _full_bracket(weeks=2)
    for i, row in enumerate(r for r in rows if r.week == 1):
        row.week_outcome = "W" if i % 2 == 0 else "L"
        row.opponent = _key(i + 1 if i % 2 == 0 else i - 1)

    pairing = ad.compute_week_pairing(rows, 2)
    winners = {_key(i) for i in range(0, ad.BRACKET_SIZE, 2)}
    # Week 1's weight (8) exceeds every later week combined, so winners and
    # losers never meet again.
    for match in pairing.matches:
        assert (match.a in winners) == (match.b in winners)
    assert pairing.rematches == ()


def test_pairing_avoids_a_rematch_when_it_can():
    rows = _full_bracket(weeks=2)
    # Everyone tied at 0 going into week 2, but 1 and 2 already met in week 1,
    # so the adjacent-pair walk must skip ahead rather than rerun the match.
    for row in (r for r in rows if r.week == 1):
        if row.alliance in (_key(0), _key(1)):
            row.opponent = _key(1) if row.alliance == _key(0) else _key(0)

    pairing = ad.compute_week_pairing(rows, 2)
    first = pairing.matches[0]
    assert {first.a, first.b} == {_key(0), _key(2)}
    assert pairing.rematches == ()


def test_pairing_skips_across_cohorts_before_it_repeats_a_matchup():
    rows = _full_bracket(weeks=2)
    for row in (r for r in rows if r.week == 1):
        idx = int(row.alliance.tag[2:])
        row.opponent = _key(idx + 1 if idx % 2 == 0 else idx - 1)
        row.week_outcome = "W" if idx % 2 == 0 else "L"
    # Every winner has now also faced every other winner, so the top of the
    # order has no clean opponent left inside its own cohort.
    winners = [_key(i) for i in range(0, ad.BRACKET_SIZE, 2)]
    rows += [
        _row(1, int(w.tag[2:]), None, opponent=other)
        for w in winners
        for other in winners
        if other != w
    ]

    pairing = ad.compute_week_pairing(rows, 2)
    # Avoiding the rematch is preferred over keeping the cohorts clean, and
    # nothing is reported as a rematch because none actually happened.
    assert pairing.rematches == ()
    assert {pairing.matches[0].a, pairing.matches[0].b} == {_key(0), _key(3)}


def test_pairing_falls_back_to_a_rematch_and_says_so():
    rows = _full_bracket(weeks=2)
    # Everyone has already faced everyone, so the walk has nowhere clean to
    # go. It pairs adjacent anyway and reports the rematch rather than
    # dropping alliances out of the week.
    everyone = [_key(i) for i in range(ad.BRACKET_SIZE)]
    rows += [
        _row(1, i, None, opponent=other)
        for i in range(ad.BRACKET_SIZE)
        for other in everyone
        if other != _key(i)
    ]

    pairing = ad.compute_week_pairing(rows, 2)
    assert len(pairing.matches) == ad.BRACKET_SIZE // 2
    assert pairing.rematches == pairing.matches


def test_pairing_reports_an_incomplete_bracket_rather_than_guessing():
    result = ad.compute_week_pairing([_row(1, i, i + 1) for i in range(4)], 1)
    assert isinstance(result, ad.BracketIncomplete)
    assert result.reason == "roster_size"
    assert result.found == 4
    assert result.expected == 16
    # It is data-shaped, not a choice — the caller should prompt, not upsell.
    assert result.is_choice is False


def test_own_alliance_mode_is_a_choice_not_a_gap():
    incomplete = ad.BracketIncomplete(reason="own_alliance_mode", detail="tracking own alliance")
    assert incomplete.is_choice is True


def test_pairing_disagreements_surface_real_world_divergence():
    rows = _full_bracket()
    for row in rows:
        idx = int(row.alliance.tag[2:])
        row.opponent = _key(idx + 1 if idx % 2 == 0 else idx - 1)
    assert ad.pairing_disagreements(rows, 1) == ()

    # The game paired differently from the algorithm — that divergence is the
    # signal, and it must surface rather than silently corrupt projections.
    rows[0].opponent = _key(5)
    disagreements = ad.pairing_disagreements(rows, 1)
    assert (_key(0), _key(5), _key(1)) in disagreements


# ── Path projection ───────────────────────────────────────────────────────────


def _play_out(rows, week, winner_of):
    """Record `week`'s pairing and outcomes onto the rows in place."""
    pairing = ad.compute_week_pairing(rows, week)
    assert isinstance(pairing, ad.WeekPairing), pairing
    for match in pairing.matches:
        winner = winner_of(match)
        loser = match.other(winner)
        for side, opponent, outcome in ((winner, loser, "W"), (loser, winner, "L")):
            for row in rows:
                if row.week == week and row.alliance == side:
                    row.opponent = opponent
                    row.week_outcome = outcome
    return pairing


def test_projection_names_every_opponent_when_the_league_is_played_out():
    rows = _full_bracket(weeks=4)
    for week in range(1, 5):
        _play_out(rows, week, winner_of=lambda m: m.a)

    projection = ad.project_own_path(_key(3), rows)
    assert isinstance(projection, ad.PathProjection)
    assert projection.is_blocked is False
    assert [s.week for s in projection.steps] == [1, 2, 3, 4]
    assert all(s.opponent is not None for s in projection.steps)
    assert all(s.source == ad.SOURCE_CONFIRMED for s in projection.steps)
    assert all(s.outcome in ("W", "L") for s in projection.steps)


def test_projection_names_the_matches_blocking_it():
    rows = _full_bracket(weeks=4)
    _play_out(rows, 1, winner_of=lambda m: m.a)

    projection = ad.project_own_path(_key(0), rows)
    # Week 1 and 2 are knowable; week 2's opponent needs the sibling week-1
    # match, which is confirmed. Week 3 needs week-2 results that don't exist.
    assert projection.steps[0].opponent == _key(1)
    assert projection.steps[0].outcome == "W"
    assert projection.is_blocked is True
    # The blockers are named specific matches, not "not enough data".
    assert all(isinstance(m, ad.Match) for m in projection.blocked_on)
    assert projection.blocked_on[0].week == 2


def test_scouting_priority_is_the_blocking_set_without_yourself():
    rows = _full_bracket(weeks=4)
    _play_out(rows, 1, winner_of=lambda m: m.a)
    projection = ad.project_own_path(_key(0), rows)

    priority = projection.scouting_priority
    assert priority, "a blocked path must name who to scout"
    assert _key(0) not in priority
    assert len(priority) == len(set(priority))
    # This is the payoff: not "go scout 15 alliances" but a short ordered set.
    assert len(priority) < ad.BRACKET_SIZE


def test_a_picked_call_resolves_a_match_the_results_dont():
    rows = _full_bracket(weeks=2)
    _play_out(rows, 1, winner_of=lambda m: m.a)
    # Nobody has played week 2, but leadership called the sibling match.
    for row in rows:
        if row.week == 2 and row.alliance == _key(2):
            row.picked = "W"
            row.opponent = _key(0)

    projection = ad.project_own_path(_key(0), rows, upto_week=2)
    assert projection.steps[1].opponent == _key(2)
    assert projection.steps[1].outcome_source == ad.SOURCE_PICKED


def test_a_known_read_breaks_a_tie_the_results_cannot():
    rows = _full_bracket(weeks=2)
    _play_out(rows, 1, winner_of=lambda m: m.a)
    for row in rows:
        if row.alliance == _key(0):
            row.known_1_5 = "very strong"
        elif row.alliance == _key(2):
            row.known_1_5 = "weak"

    projection = ad.project_own_path(_key(0), rows, upto_week=2)
    assert projection.steps[1].outcome == "W"
    assert projection.steps[1].outcome_source == ad.SOURCE_KNOWN


def test_equal_known_reads_do_not_break_the_tie_by_fiat():
    rows = _full_bracket(weeks=2)
    _play_out(rows, 1, winner_of=lambda m: m.a)
    for row in rows:
        row.known_1_5 = "strong"

    projection = ad.project_own_path(_key(0), rows, upto_week=2)
    assert projection.steps[1].outcome is None
    assert projection.is_blocked is True


def test_the_estimator_is_the_last_resort_and_marks_the_step():
    rows = _full_bracket(weeks=2)
    _play_out(rows, 1, winner_of=lambda m: m.a)

    projection = ad.project_own_path(_key(0), rows, upto_week=2, estimate=lambda a, b, w: a)
    assert projection.is_blocked is False
    assert projection.steps[1].outcome_source == ad.SOURCE_ESTIMATED


def test_an_opponent_reached_through_an_estimate_is_reported_as_estimated():
    rows = _full_bracket(weeks=4)
    _play_out(rows, 1, winner_of=lambda m: m.a)
    projection = ad.project_own_path(
        _key(0), rows, upto_week=3, estimate=lambda a, b, w: sorted((a, b))[0]
    )
    # Week 3's opponent identity depends on estimated week-2 matches, so the
    # weakest link in that chain is what gets reported.
    assert projection.steps[2].source == ad.SOURCE_ESTIMATED


def test_projection_reports_an_incomplete_bracket_rather_than_raising():
    rows = [_row(1, i, i + 1) for i in range(3)]
    result = ad.project_own_path(_key(0), rows)
    assert isinstance(result, ad.BracketIncomplete)
    assert result.reason == "roster_size"


def test_projection_requires_unique_seeds_one_to_sixteen():
    rows = _full_bracket()
    rows[0].seed = 2  # duplicate
    result = ad.project_own_path(_key(1), rows)
    assert isinstance(result, ad.BracketIncomplete)
    assert result.reason == "missing_seeds"

    rows = _full_bracket()
    rows[5].seed = None
    result = ad.project_own_path(_key(1), rows)
    assert isinstance(result, ad.BracketIncomplete)
    assert result.reason == "missing_seeds"


# ── The cross-check ───────────────────────────────────────────────────────────


def _simulate_league(rng: random.Random) -> list[ad.AllianceWeek]:
    """A fully-resolved randomized league: seeds shuffled, winners random."""
    seeds = list(range(1, ad.BRACKET_SIZE + 1))
    rng.shuffle(seeds)
    rows: list[ad.AllianceWeek] = []
    for week in range(1, ad.LEAGUE_WEEKS + 1):
        rows += [_row(week, i, seeds[i]) for i in range(ad.BRACKET_SIZE)]
        _play_out(rows, week, winner_of=lambda m: rng.choice([m.a, m.b]))
    return rows


@pytest.mark.parametrize("trial", range(25))
def test_projection_agrees_with_pairing_on_random_leagues(trial):
    """The design's first cross-check, run for real.

    `compute_week_pairing` re-ranks on weighted score each week;
    `project_own_path` walks the bracket lineage. They are independent
    derivations of the same thing, so on fully-resolved data they must agree
    for every target and every week — otherwise the projection is quietly
    wrong and nobody would notice until an alliance scouted the wrong team.
    """
    rng = random.Random(trial)
    rows = _simulate_league(rng)

    expected = {}
    for week in range(1, ad.LEAGUE_WEEKS + 1):
        pairing = ad.compute_week_pairing(rows, week)
        assert isinstance(pairing, ad.WeekPairing)
        assert pairing.rematches == (), "a clean 16-alliance bracket needs no rematch"
        for match in pairing.matches:
            expected[(week, match.a)] = match.b
            expected[(week, match.b)] = match.a

    for i in range(ad.BRACKET_SIZE):
        target = _key(i)
        projection = ad.project_own_path(target, rows)
        assert isinstance(projection, ad.PathProjection)
        assert projection.is_blocked is False
        assert len(projection.steps) == ad.LEAGUE_WEEKS
        for step in projection.steps:
            assert step.opponent == expected[(step.week, target)], (
                f"trial {trial}: {target} week {step.week} — lineage says "
                f"{step.opponent}, re-ranking says {expected[(step.week, target)]}"
            )


@pytest.mark.parametrize("trial", range(10))
def test_recorded_opponents_match_the_algorithm_on_a_clean_league(trial):
    """The design's second cross-check: the production comparison of recorded
    Opponent columns against the predicted pairing finds nothing on data the
    algorithm itself produced."""
    rows = _simulate_league(random.Random(100 + trial))
    for week in range(1, ad.LEAGUE_WEEKS + 1):
        assert ad.pairing_disagreements(rows, week) == ()

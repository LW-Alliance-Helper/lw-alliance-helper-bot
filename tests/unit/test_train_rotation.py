"""
Unit tests for train_rotation.py — the pure selection-algorithm layer.

Covers rotation counting, member-rule filtering, single-day selection across
every day-rule type, the no-duplicates-this-week rule, the three birthday
modes, draft re-roll, and the leaderboard. Sheet I/O is tested separately in
test_train_rotation_sheets.py.

Reference week: 2026-06-01 is a Monday, so the generated week runs
Mon Jun 1 → Sun Jun 7.
"""

from datetime import date

import pytest

import train_rotation as tr
from train_rotation import (
    DayRule,
    HistoryRow,
    MemberRule,
    SchedulePreset,
    STATUS_POSTED,
    STATUS_SCHEDULED,
)

MONDAY = date(2026, 6, 1)
COUNTED = set(tr.DEFAULT_COUNTED_REASONS)


def _posted(member, d, reason="auto"):
    return HistoryRow(date=d, member=member, reason=reason, status=STATUS_POSTED)


# ── rotation_counts ──────────────────────────────────────────────────────────


def test_rotation_counts_only_posted_and_counted():
    history = [
        _posted("Alice", "2026-05-01", "auto"),
        _posted("Alice", "2026-05-08", "auto"),
        _posted("Alice", "2026-05-15", "birthday"),  # non-counted reason
        HistoryRow("2026-05-22", "Alice", "auto", STATUS_SCHEDULED),  # not posted
        _posted("Bob", "2026-05-02", "leadership"),
    ]
    counts = tr.rotation_counts(history, COUNTED)
    assert counts["alice"] == 2  # birthday + scheduled excluded
    assert counts["bob"] == 1


def test_rotation_counts_case_insensitive_member_key():
    history = [_posted("Alice", "2026-05-01"), _posted("alice", "2026-05-08")]
    assert tr.rotation_counts(history, COUNTED)["alice"] == 2


def test_last_driven_uses_all_posted_reasons():
    history = [
        _posted("Alice", "2026-05-01", "auto"),
        _posted("Alice", "2026-05-20", "birthday"),  # non-counted but still drove
    ]
    assert tr.last_driven_dates(history)["alice"] == "2026-05-20"


# ── member-rule filtering ────────────────────────────────────────────────────


def test_opt_out_blocks():
    rules = [MemberRule("Alice", tr.MEMBER_RULE_OPT_OUT, "")]
    assert tr.is_blocked_by_member_rule("Alice", rules, MONDAY) is True


def test_opt_out_explicit_false_does_not_block():
    rules = [MemberRule("Alice", tr.MEMBER_RULE_OPT_OUT, "FALSE")]
    assert tr.is_blocked_by_member_rule("Alice", rules, MONDAY) is False


def test_skip_until_blocks_before_date():
    rules = [MemberRule("Alice", tr.MEMBER_RULE_SKIP_UNTIL, "2026-06-15")]
    assert tr.is_blocked_by_member_rule("Alice", rules, MONDAY) is True


def test_skip_until_eligible_on_and_after_date():
    rules = [MemberRule("Alice", tr.MEMBER_RULE_SKIP_UNTIL, "2026-06-01")]
    # eligible on the date itself
    assert tr.is_blocked_by_member_rule("Alice", rules, MONDAY) is False
    assert tr.is_blocked_by_member_rule("Alice", rules, date(2026, 6, 2)) is False


def test_skip_until_garbage_value_does_not_block():
    rules = [MemberRule("Alice", tr.MEMBER_RULE_SKIP_UNTIL, "not-a-date")]
    assert tr.is_blocked_by_member_rule("Alice", rules, MONDAY) is False


# ── select_conductor: specific_member ────────────────────────────────────────


def test_specific_member_pinned():
    rule = DayRule(0, tr.RULE_SPECIFIC, specific_member="Zara")
    member, reason, needs = tr.select_conductor(
        rule,
        target_date=MONDAY,
        eligible_pool=["Alice"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member == "Zara"
    assert reason == "other"
    assert needs is False


def test_specific_member_missing_pin_needs_picking():
    rule = DayRule(0, tr.RULE_SPECIFIC, specific_member="")
    member, _, needs = tr.select_conductor(
        rule,
        target_date=MONDAY,
        eligible_pool=["Alice"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member is None
    assert needs is True


# ── select_conductor: rotation fairness ──────────────────────────────────────


def _select_auto(pool, history, already=None):
    return tr.select_conductor(
        DayRule(0, tr.RULE_AUTO),
        target_date=MONDAY,
        eligible_pool=pool,
        role_pools={},
        member_rules=[],
        history=history,
        counted_reasons=COUNTED,
        already_scheduled=already or set(),
    )


def test_auto_picks_lowest_rotation_count():
    history = [_posted("Alice", "2026-05-01"), _posted("Alice", "2026-05-08")]
    member, reason, needs = _select_auto(["Alice", "Bob"], history)
    assert member == "Bob"  # 0 vs 2
    assert reason == "auto"
    assert needs is False


def test_never_driven_sorts_first():
    history = [_posted("Alice", "2026-05-01")]  # Alice 1, Carol 0
    member, _, _ = _select_auto(["Alice", "Carol"], history)
    assert member == "Carol"


def test_tie_broken_by_oldest_last_driven():
    # Both count 1; Bob drove longer ago → Bob picked.
    history = [_posted("Alice", "2026-05-20"), _posted("Bob", "2026-05-01")]
    member, _, _ = _select_auto(["Alice", "Bob"], history)
    assert member == "Bob"


def test_tie_fully_equal_breaks_by_name_deterministically():
    member, _, _ = _select_auto(["Bravo", "Alpha"], [])
    assert member == "Alpha"  # deterministic, name tiebreak


def test_already_scheduled_excluded():
    member, _, _ = _select_auto(["Alice", "Bob"], [], already={"alice"})
    assert member == "Bob"


def test_pool_exhausted_wraps_to_repeat():
    # Alice is the only eligible member and already scheduled this week → wrap
    # around and reuse her rather than leave the day empty.
    member, _, needs = _select_auto(["Alice"], [], already={"alice"})
    assert member == "Alice"
    assert needs is False


def test_genuinely_empty_pool_needs_picking():
    # Everyone opted out → no eligible candidates at all → needs picking.
    rules = [MemberRule("Alice", tr.MEMBER_RULE_OPT_OUT, "")]
    member, _, needs = tr.select_conductor(
        DayRule(0, tr.RULE_AUTO),
        target_date=MONDAY,
        eligible_pool=["Alice"],
        role_pools={},
        member_rules=rules,
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member is None
    assert needs is True


def test_opt_out_member_excluded_from_selection():
    rules = [MemberRule("Bob", tr.MEMBER_RULE_OPT_OUT, "")]
    member, _, _ = tr.select_conductor(
        DayRule(0, tr.RULE_AUTO),
        target_date=MONDAY,
        eligible_pool=["Bob", "Carol"],
        role_pools={},
        member_rules=rules,
        history=[_posted("Carol", "2026-05-01")],  # Carol has higher count
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member == "Carol"  # Bob opted out despite count 0


# ── select_conductor: rule types ─────────────────────────────────────────────


def test_leadership_rule_uses_leadership_role_pool():
    # Leadership draws from role_pools["leadership"], not the full roster.
    member, reason, _ = tr.select_conductor(
        DayRule(0, tr.RULE_LEADERSHIP),
        target_date=MONDAY,
        eligible_pool=["Alice", "Bob"],
        role_pools={tr.RULE_LEADERSHIP: ["Officer"]},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member == "Officer"
    assert reason == "leadership"


def test_leadership_rule_no_role_needs_picking():
    # No leadership role resolved → needs picking (honest, vs. silently
    # picking a non-leader from the full roster).
    member, _, needs = tr.select_conductor(
        DayRule(0, tr.RULE_LEADERSHIP),
        target_date=MONDAY,
        eligible_pool=["Alice", "Bob"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member is None
    assert needs is True


def test_vs_rule_scoped_to_assigned_role():
    # A role assigned to vs scopes the pool to that role's members.
    member, reason, _ = tr.select_conductor(
        DayRule(0, tr.RULE_VS),
        target_date=MONDAY,
        eligible_pool=["Alice", "Bob"],
        role_pools={tr.RULE_VS: ["WarLead"]},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member == "WarLead"  # scoped to the vs role, not the full roster
    assert reason == "vs"


def test_vs_rule_no_role_is_manual():
    # vs / contest / event with no assigned role → manual (needs picking),
    # NOT a silent full-roster pick.
    member, reason, needs = tr.select_conductor(
        DayRule(0, tr.RULE_VS),
        target_date=MONDAY,
        eligible_pool=["Alice", "Bob"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member is None
    assert needs is True
    assert reason == "vs"


def test_manual_rule_always_needs_picking():
    # The Manual rule type never auto-picks, even with a full roster — the day
    # is assigned by hand (and the daily confirmation prompts).
    member, reason, needs = tr.select_conductor(
        DayRule(0, tr.RULE_MANUAL),
        target_date=MONDAY,
        eligible_pool=["Alice", "Bob"],
        role_pools={tr.RULE_MANUAL: ["X"]},  # even a stray role is ignored
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
    )
    assert member is None
    assert needs is True
    assert reason == "manual"


# ── generate_week_draft ──────────────────────────────────────────────────────


def _standard_preset():
    return SchedulePreset.default("Standard Week")


def test_draft_produces_seven_days_mon_to_sun():
    draft = tr.generate_week_draft(
        _standard_preset(),
        MONDAY,
        eligible_pool=["A", "B", "C", "D", "E", "F", "G"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
    )
    assert len(draft) == 7
    assert draft[0].date == "2026-06-01"
    assert draft[6].date == "2026-06-07"
    assert [d.weekday for d in draft] == [0, 1, 2, 3, 4, 5, 6]


def test_draft_no_duplicate_members_within_week():
    pool = ["A", "B", "C", "D", "E", "F", "G"]
    draft = tr.generate_week_draft(
        _standard_preset(),
        MONDAY,
        eligible_pool=pool,
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
    )
    picked = [d.member for d in draft if d.member]
    assert len(picked) == len(set(picked)) == 7


def test_draft_small_pool_wraps_around():
    # Pool smaller than the week → fill all 7 days, reusing the pool once it's
    # exhausted (no empty days). Everyone is used before anyone repeats.
    draft = tr.generate_week_draft(
        _standard_preset(),
        MONDAY,
        eligible_pool=["A", "B"],  # only 2 for 7 days
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
    )
    placed = [d.member for d in draft if d.member]
    assert len(placed) == 7  # every day filled, none left needs-picking
    assert set(placed) == {"A", "B"}  # only the two members, wrapping
    # The first two days use both before any repeat.
    assert {draft[0].member, draft[1].member} == {"A", "B"}


def test_draft_empty_pool_needs_picking():
    # A genuinely empty roster → every auto day needs picking (nothing to wrap).
    draft = tr.generate_week_draft(
        _standard_preset(),
        MONDAY,
        eligible_pool=[],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
    )
    assert all(d.member is None and d.needs_picking for d in draft)


def test_draft_override_birthday_preempts_day_rule():
    # Wednesday Jun 3 has a birthday; override mode should make Eve the
    # conductor that day with reason birthday, even though the preset rule
    # is auto.
    draft = tr.generate_week_draft(
        _standard_preset(),
        MONDAY,
        eligible_pool=["A", "B", "C", "D", "E", "F", "G"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        birthday_mode=tr.BIRTHDAY_OVERRIDE,
        birthdays_on_date={"2026-06-03": ["Eve"]},
    )
    wed = next(d for d in draft if d.date == "2026-06-03")
    assert wed.member == "Eve"
    assert wed.reason == "birthday"
    assert "🎂" in wed.note


def test_draft_disabled_mode_ignores_birthdays():
    # Eve has a birthday Wednesday but disabled mode must ignore birthdays
    # entirely — Wednesday gets an ordinary rotation pick (Eve isn't even in
    # the pool).
    draft = tr.generate_week_draft(
        _standard_preset(),
        MONDAY,
        eligible_pool=["A", "B", "C", "D", "E", "F", "G"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        birthday_mode=tr.BIRTHDAY_DISABLED,
        birthdays_on_date={"2026-06-03": ["Eve"]},
    )
    wed = next(d for d in draft if d.date == "2026-06-03")
    assert wed.member != "Eve"
    assert wed.reason == "auto"


def test_draft_override_two_birthdays_same_day_route_around():
    # Eve + Zoe both born Wednesday. Deterministic order (by name) puts Eve on
    # Wed; Zoe routes to the day before (Tue), since Wed is taken.
    draft = tr.generate_week_draft(
        _standard_preset(),
        MONDAY,
        eligible_pool=["A", "B", "C", "D", "E", "F", "G"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        birthday_mode=tr.BIRTHDAY_OVERRIDE,
        birthdays_on_date={"2026-06-03": ["Eve", "Zoe"]},
    )
    by_member = {d.member: d for d in draft if d.reason == "birthday"}
    assert by_member["Eve"].date == "2026-06-03"
    assert by_member["Zoe"].date == "2026-06-02"  # Wed taken → day before
    assert "day before" in by_member["Zoe"].note


def test_draft_override_birthday_routes_around_specific_pin():
    # Wednesday is pinned to Captain; Eve's birthday is Wednesday → the pin
    # holds and Eve routes to an adjacent day.
    preset = _standard_preset()
    preset.days[2] = DayRule(2, tr.RULE_SPECIFIC, specific_member="Captain")
    draft = tr.generate_week_draft(
        preset,
        MONDAY,
        eligible_pool=["A", "B", "C", "D", "E", "F", "G"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        birthday_mode=tr.BIRTHDAY_OVERRIDE,
        birthdays_on_date={"2026-06-03": ["Eve"]},
    )
    wed = next(d for d in draft if d.date == "2026-06-03")
    assert wed.member == "Captain"  # pin holds
    eve = next(d for d in draft if d.member == "Eve")
    assert eve.date == "2026-06-02"  # day before (Wed taken by the pin)
    assert eve.reason == "birthday"


def test_draft_override_birthday_person_not_double_booked():
    # Bob has a Wednesday birthday AND would be the top rotation pick (never
    # driven). He appears exactly once — as the birthday — never in rotation.
    draft = tr.generate_week_draft(
        _standard_preset(),
        MONDAY,
        eligible_pool=["Bob", "Cara", "Dan", "Eli", "Fay", "Gil", "Hue"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        birthday_mode=tr.BIRTHDAY_OVERRIDE,
        birthdays_on_date={"2026-06-03": ["Bob"]},
    )
    bob_days = [d for d in draft if d.member == "Bob"]
    assert len(bob_days) == 1
    assert bob_days[0].date == "2026-06-03"
    assert bob_days[0].reason == "birthday"


def test_draft_specific_member_pinned_each_week():
    preset = _standard_preset()
    preset.days[2] = DayRule(2, tr.RULE_SPECIFIC, specific_member="Captain")
    draft = tr.generate_week_draft(
        preset,
        MONDAY,
        eligible_pool=["A", "B", "C", "D", "E", "F", "G"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
    )
    wed = next(d for d in draft if d.date == "2026-06-03")
    assert wed.member == "Captain"


# ── reroll_day ───────────────────────────────────────────────────────────────


def test_reroll_manual_day_suggests_from_full_pool():
    dd = tr.DraftDay(
        date="2026-06-05",
        weekday=4,
        rule_type=tr.RULE_VS,
        member=None,
        reason="vs",
        needs_picking=True,
    )
    member, _, needs = tr.reroll_day(
        dd,
        eligible_pool=["Alice", "Bob"],
        role_pools={},
        member_rules=[],
        history=[_posted("Alice", "2026-05-01")],
        counted_reasons=COUNTED,
        other_scheduled=set(),
        target_date=date(2026, 6, 5),
    )
    assert member == "Bob"  # lowest rotation suggestion despite manual rule
    assert needs is False


# ── leaderboard ──────────────────────────────────────────────────────────────


def test_leaderboard_sorted_by_count_then_recency():
    history = [
        _posted("Alice", "2026-05-01"),
        _posted("Alice", "2026-05-08"),
        _posted("Bob", "2026-05-02"),
        _posted("Carol", "2026-05-20", "birthday"),  # non-counted
    ]
    board = tr.leaderboard(history, COUNTED)
    names = [row[0] for row in board]
    assert names[0] == "Alice"  # count 2
    assert names[1] == "Bob"  # count 1
    # Carol present with count 0 (drove a birthday train, not counted)
    carol = next(row for row in board if row[0] == "Carol")
    assert carol[1] == 0
    assert carol[2] == "2026-05-20"


# ── member_tally (roster-based, for the Assignment Logs "fewest" view) ─────────


class TestMemberTally:
    def test_never_driven_roster_member_present_at_zero(self):
        roster = ["Alice", "Bob", "Zoe"]
        history = [
            _posted("Alice", "2026-06-01"),
            _posted("Alice", "2026-06-02"),
            _posted("Bob", "2026-06-03", "vs"),
        ]
        rows = tr.member_tally(roster, history, COUNTED, [], date(2026, 6, 10))
        d = {n: c for n, c, _ in rows}
        assert d["Alice"] == 2
        assert d["Bob"] == 1
        assert d["Zoe"] == 0  # on roster, never drove → surfaces at 0 (no history row)

    def test_opted_out_without_history_is_omitted(self):
        roster = ["Alice", "Optout"]
        rules = [MemberRule("Optout", tr.MEMBER_RULE_OPT_OUT, "")]
        rows = tr.member_tally(roster, [], COUNTED, rules, date(2026, 6, 10))
        names = {n for n, _, _ in rows}
        assert "Alice" in names
        assert "Optout" not in names  # opted out + no history → not a "neglected" member

    def test_opted_out_with_history_stays(self):
        # Someone who drove and later opted out still appears (the record is honest).
        roster = ["Gone"]
        rules = [MemberRule("Gone", tr.MEMBER_RULE_OPT_OUT, "")]
        history = [_posted("Gone", "2026-05-01")]
        rows = tr.member_tally(roster, history, COUNTED, rules, date(2026, 6, 10))
        assert "Gone" in {n for n, _, _ in rows}

    def test_skipped_member_without_history_omitted(self):
        roster = ["Alice", "Skipped"]
        rules = [MemberRule("Skipped", tr.MEMBER_RULE_SKIP_UNTIL, "2026-12-01")]
        rows = tr.member_tally(roster, [], COUNTED, rules, date(2026, 6, 10))
        assert "Skipped" not in {n for n, _, _ in rows}


# ── sort_tally / sort_posted ───────────────────────────────────────────────────


class TestSortTally:
    def _rows(self):
        return [
            ("Alice", 5, "2026-06-08"),
            ("Bob", 1, "2026-06-01"),
            ("Zoe", 0, ""),  # never driven
            ("Yan", 0, "2026-03-01"),  # 0 counted but drove a non-counted train
        ]

    def test_most_puts_highest_count_first(self):
        out = tr.sort_tally(self._rows(), tr.TALLY_SORT_MOST)
        assert [n for n, _, _ in out] == ["Alice", "Bob", "Yan", "Zoe"]

    def test_fewest_puts_never_driven_first_within_zero(self):
        out = tr.sort_tally(self._rows(), tr.TALLY_SORT_FEWEST)
        assert out[0][0] == "Zoe"  # 0 + never sorts above 0 + has-a-date
        assert out[1][0] == "Yan"
        assert out[-1][0] == "Alice"  # most-driven trails

    def test_longest_since_treats_never_as_longest(self):
        out = tr.sort_tally(self._rows(), tr.TALLY_SORT_LONGEST_SINCE)
        assert out[0][0] == "Zoe"  # never driven → longest ago
        assert out[1][0] == "Yan"  # then the oldest real date

    def test_name_alpha(self):
        out = tr.sort_tally(self._rows(), tr.TALLY_SORT_NAME)
        assert [n for n, _, _ in out] == ["Alice", "Bob", "Yan", "Zoe"]


def test_sort_posted_direction():
    posted = [_posted("Bob", "2026-06-01"), _posted("Alice", "2026-06-08")]
    newest = tr.sort_posted(posted)
    assert newest[0].member == "Alice"  # 06-08 first
    oldest = tr.sort_posted(posted, newest_first=False)
    assert oldest[0].member == "Bob"  # 06-01 first

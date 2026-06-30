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


def test_rotation_counts_whole_sheet_is_fact_excludes_only_named_noncounted():
    # The whole sheet counts as fact: posted AND scheduled rows count. Only a
    # named non-counted reason (birthday) is excluded.
    history = [
        _posted("Alice", "2026-05-01", "auto"),
        _posted("Alice", "2026-05-08", "auto"),
        _posted("Alice", "2026-05-15", "birthday"),  # named non-counted reason → excluded
        HistoryRow("2026-05-22", "Alice", "auto", STATUS_SCHEDULED),  # scheduled still counts
        _posted("Bob", "2026-05-02", "leadership"),
    ]
    counts = tr.rotation_counts(history, COUNTED)
    assert counts["alice"] == 3  # 2 posted + 1 scheduled; birthday excluded
    assert counts["bob"] == 1


def test_rotation_counts_backfill_blank_reason_and_status_count():
    # Leadership back-fill: just Date + Member, no reason or status. Counts.
    history = [
        HistoryRow("2026-05-01", "Alice", "", ""),
        HistoryRow("2026-05-08", "Alice", "", ""),
        HistoryRow("2026-05-02", "Bob", "", ""),
    ]
    counts = tr.rotation_counts(history, COUNTED)
    assert counts["alice"] == 2
    assert counts["bob"] == 1


def test_rotation_counts_before_excludes_drafted_week_and_future():
    history = [
        HistoryRow("2026-05-25", "Alice", "", ""),  # before the drafted week → counts
        HistoryRow("2026-06-01", "Alice", "", ""),  # the drafted week itself → excluded
        HistoryRow("2026-06-10", "Alice", "", ""),  # future → excluded
    ]
    assert tr.rotation_counts(history, COUNTED, before=MONDAY)["alice"] == 1


def test_rotation_counts_case_insensitive_member_key():
    history = [_posted("Alice", "2026-05-01"), _posted("alice", "2026-05-08")]
    assert tr.rotation_counts(history, COUNTED)["alice"] == 2


def test_last_driven_uses_all_reasons():
    history = [
        _posted("Alice", "2026-05-01", "auto"),
        _posted("Alice", "2026-05-20", "birthday"),  # non-counted but still drove
    ]
    assert tr.last_driven_dates(history)["alice"] == "2026-05-20"


# ── identity: Discord-ID-first, name fallback ─────────────────────────────────


def test_canonicalize_history_maps_id_to_current_name():
    roster = [{"name": "NewName", "discord_id": "111"}]
    history = [HistoryRow("2026-05-01", "OldName", "auto", STATUS_POSTED, discord_id="111")]
    assert tr.canonicalize_history(history, roster)[0].member == "NewName"


def test_canonicalize_history_name_fallback_when_no_id():
    roster = [{"name": "NewName", "discord_id": "111"}]
    history = [HistoryRow("2026-05-01", "SomeName", "auto", STATUS_POSTED, discord_id="")]
    assert tr.canonicalize_history(history, roster)[0].member == "SomeName"


def test_renamed_member_record_unified_by_id():
    # A member who changed display name keeps one record via their Discord ID.
    roster = [{"name": "Current", "discord_id": "111"}]
    history = [
        HistoryRow("2026-05-01", "Old", "auto", STATUS_POSTED, discord_id="111"),
        HistoryRow("2026-05-08", "Current", "auto", STATUS_POSTED, discord_id="111"),
    ]
    canon = tr.canonicalize_history(history, roster)
    assert tr.rotation_counts(canon, COUNTED)["current"] == 2  # not split across names


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


def test_tie_fully_equal_is_stable_random_not_alphabetical():
    # Genuine dead heat (empty history). The pick is stable for a given day and
    # independent of candidate order, but NOT alphabetical.
    a = _select_auto(["Bravo", "Alpha"], [])[0]
    b = _select_auto(["Alpha", "Bravo"], [])[0]
    assert a == b  # same day → same pick regardless of incoming order
    assert a in {"Alpha", "Bravo"}
    assert a == "Bravo"  # 2026-06-01 resolves to Bravo — proves it's not name order


def test_tie_random_varies_across_days():
    # Seeded by the date, so different days resolve differently across a pool —
    # a new alliance gets a varied week, not the same alphabetical name daily.
    def pick(day):
        return tr.select_conductor(
            DayRule(0, tr.RULE_AUTO),
            target_date=date(2026, 6, day),
            eligible_pool=["Alpha", "Bravo"],
            role_pools={},
            member_rules=[],
            history=[],
            counted_reasons=COUNTED,
            already_scheduled=set(),
        )[0]

    assert {pick(d) for d in range(1, 15)} == {"Alpha", "Bravo"}


def test_already_scheduled_excluded():
    member, _, _ = _select_auto(["Alice", "Bob"], [], already={"alice"})
    assert member == "Bob"


def test_reroll_advances_off_current_member():
    # "Go to next person" must move off the current pick, even on a small tied
    # pool (the Leadership case) where the fairness pick is otherwise stable.
    role_pools = {tr.RULE_LEADERSHIP: ["Alice", "Bob"]}
    dd = tr.DraftDay("2026-06-01", 0, tr.RULE_LEADERSHIP, "Alice", "leadership")
    m1, _, _ = tr.reroll_day(
        dd,
        eligible_pool=["Alice", "Bob"],
        role_pools=role_pools,
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        other_scheduled=set(),
        target_date=MONDAY,
    )
    assert m1 == "Bob"  # advanced off Alice
    dd.member = "Bob"
    m2, _, _ = tr.reroll_day(
        dd,
        eligible_pool=["Alice", "Bob"],
        role_pools=role_pools,
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        other_scheduled=set(),
        target_date=MONDAY,
    )
    assert m2 == "Alice"  # advanced off Bob


def test_reroll_single_person_pool_keeps_them():
    # Only one eligible member → "next" stays on them (not needs-picking).
    dd = tr.DraftDay("2026-06-01", 0, tr.RULE_LEADERSHIP, "Solo", "leadership")
    member, _, needs = tr.reroll_day(
        dd,
        eligible_pool=["Solo"],
        role_pools={tr.RULE_LEADERSHIP: ["Solo"]},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        other_scheduled=set(),
        target_date=MONDAY,
    )
    assert member == "Solo"
    assert needs is False


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


# ── select_conductor: free tier (role rules disabled, #337) ──────────────────


def test_free_tier_leadership_falls_back_to_full_roster():
    # On free tier (role_rules_enabled False) a leadership day rotates the full
    # roster instead of "needs picking", recorded as an auto pick.
    member, reason, needs = tr.select_conductor(
        DayRule(0, tr.RULE_LEADERSHIP),
        target_date=MONDAY,
        eligible_pool=["Alice", "Bob"],
        role_pools={},  # no role pools built on free tier
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
        role_rules_enabled=False,
    )
    assert member in ("Alice", "Bob")
    assert needs is False
    assert reason == "auto"


def test_free_tier_vs_falls_back_to_full_roster():
    member, reason, needs = tr.select_conductor(
        DayRule(0, tr.RULE_VS),
        target_date=MONDAY,
        eligible_pool=["Alice", "Bob"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
        role_rules_enabled=False,
    )
    assert member in ("Alice", "Bob")
    assert needs is False
    assert reason == "auto"


def test_free_tier_ignores_role_pool_even_if_present():
    # Defensive: even if a role pool somehow exists, free tier rotates the full
    # roster and never honors the role scope.
    member, reason, _ = tr.select_conductor(
        DayRule(0, tr.RULE_LEADERSHIP),
        target_date=MONDAY,
        eligible_pool=["Alice", "Bob"],
        role_pools={tr.RULE_LEADERSHIP: ["Officer"]},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        already_scheduled=set(),
        role_rules_enabled=False,
    )
    assert member in ("Alice", "Bob")
    assert member != "Officer"
    assert reason == "auto"


def test_premium_default_still_scopes_roles():
    # Default (role_rules_enabled True) preserves Premium behavior: a leadership
    # day with no resolvable role still needs picking, not a full-roster pick.
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


def test_free_tier_reroll_role_day_uses_full_roster_as_auto():
    # reroll on a leadership day, free tier → picks from the full roster and
    # records the auto reason (matching the weekly draft).
    member, reason, needs = tr.reroll_day(
        tr.DraftDay(
            date="2026-06-01",
            weekday=0,
            rule_type=tr.RULE_LEADERSHIP,
            member="Officer",
            reason="leadership",
        ),
        eligible_pool=["Alice", "Bob"],
        role_pools={tr.RULE_LEADERSHIP: ["Officer"]},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        other_scheduled=set(),
        target_date=MONDAY,
        role_rules_enabled=False,
    )
    assert member in ("Alice", "Bob")
    assert member != "Officer"
    assert needs is False
    assert reason == "auto"


# ── generate_week_draft ──────────────────────────────────────────────────────


def _standard_preset():
    return SchedulePreset.default("Standard Week")


def _leadership_monday_preset():
    """Monday = leadership, every other day = auto."""
    days = {wd: DayRule(weekday=wd) for wd in range(7)}
    days[0] = DayRule(weekday=0, rule_type=tr.RULE_LEADERSHIP)
    return SchedulePreset(name="Lead Monday", days=days)


def test_free_tier_draft_fills_role_day_from_full_roster():
    # A leadership Monday on free tier (role_rules_enabled False) fills from the
    # full roster instead of needs-picking.
    draft = tr.generate_week_draft(
        _leadership_monday_preset(),
        MONDAY,
        eligible_pool=["Alice", "Bob", "Cara"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
        role_rules_enabled=False,
    )
    mon = next(d for d in draft if d.date == "2026-06-01")
    assert mon.member in ("Alice", "Bob", "Cara")
    assert mon.needs_picking is False
    assert mon.reason == "auto"


def test_premium_draft_role_day_needs_picking_without_role():
    # Same preset, Premium default, no role pool → the leadership day needs
    # picking (unchanged Premium behavior).
    draft = tr.generate_week_draft(
        _leadership_monday_preset(),
        MONDAY,
        eligible_pool=["Alice", "Bob", "Cara"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=COUNTED,
    )
    mon = next(d for d in draft if d.date == "2026-06-01")
    assert mon.member is None
    assert mon.needs_picking is True


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
    # needs-picking is tracked by the flag, not by a sentinel in the note column
    # (which is the human-reason field surfaced in the draft embed).
    assert all(d.note == "" for d in draft)


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

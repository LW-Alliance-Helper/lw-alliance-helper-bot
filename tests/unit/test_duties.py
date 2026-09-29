"""Leadership Duties (#687): the Discord-free core."""

from datetime import date, datetime, time

import pytest

import duties as d
import template_render

A, B, C, E = 101, 102, 103, 105  # leaders
OUTSIDER = 900  # holds a duty, not in the Leadership role


def duty(id=1, **kw) -> d.Duty:
    kw.setdefault("name", f"Duty {id}")
    return d.Duty(id=id, guild_id=1, **kw)


def reminder(**kw) -> d.DutyReminder:
    kw.setdefault("id", 1)
    kw.setdefault("guild_id", 1)
    kw.setdefault("duty_id", 1)
    kw.setdefault("message", "hi")
    kw.setdefault("schedule_type", d.SCHEDULE_WEEKDAYS)
    kw.setdefault("at", time(9, 0))
    return d.DutyReminder(**kw)


# ── Holders ──────────────────────────────────────────────────────────────────


def test_open_slots_are_positions_not_holders():
    x = duty(primaries=(A, d.OPEN), backups=(d.OPEN,))
    assert x.holders(d.SLOT_PRIMARY) == (A,)
    assert x.open_count(d.SLOT_PRIMARY) == 1
    assert x.holders(d.SLOT_BACKUP) == ()
    assert x.open_count(d.SLOT_BACKUP) == 1


def test_anyone_in_leadership_backup_hides_the_stored_backups():
    x = duty(primaries=(A,), backups=(B, d.OPEN), backup_anyone_leadership=True)
    assert x.holders(d.SLOT_BACKUP) == ()
    assert x.open_count(d.SLOT_BACKUP) == 0
    assert x.all_holders() == (A,)
    assert not x.holds(B)


def test_all_holders_lists_someone_in_both_slots_once():
    x = duty(primaries=(A, B), backups=(B, C))
    assert x.all_holders() == (A, B, C)


def test_unknown_slot_kind_is_an_error():
    with pytest.raises(ValueError):
        duty().slot("tertiary")


def test_vacate_opens_the_slot_in_place():
    x = duty(primaries=(A, B), backups=(C, B))
    out = d.vacate(x, B)
    assert out.primaries == (A, d.OPEN)
    assert out.backups == (C, d.OPEN)


def test_vacate_leaves_a_duty_they_dont_hold_alone():
    x = duty(primaries=(A,))
    assert d.vacate(x, B) is x


def test_vacate_never_matches_an_open_slot():
    x = duty(primaries=(d.OPEN, A))
    assert d.vacate(x, d.OPEN) is x


def test_held_by_splits_primary_and_backup_and_keeps_paused():
    one = duty(1, primaries=(A,))
    two = duty(2, backups=(A,), paused=True)
    three = duty(3, primaries=(A,), backups=(A,))
    four = duty(4, primaries=(B,))
    primary, backup = d.held_by([one, two, three, four], A)
    assert [x.id for x in primary] == [1, 3]
    assert [x.id for x in backup] == [2]


def test_by_category_groups_in_first_seen_order_and_sorts_within():
    xs = [
        duty(1, category="VS", sort_order=2),
        duty(2, category="Members", sort_order=0),
        duty(3, category="VS", sort_order=1),
    ]
    grouped = d.by_category(xs)
    assert [c for c, _ in grouped] == ["VS", "Members"]
    assert [x.id for x in grouped[0][1]] == [3, 1]


# ── Workload ─────────────────────────────────────────────────────────────────


def test_every_leader_appears_even_holding_nothing():
    loads = d.workload([duty(primaries=(A,))], [A, B])
    assert [(x.user_id, x.primary, x.backup) for x in loads] == [(A, 1, 0), (B, 0, 0)]


def test_paused_duties_count_and_are_broken_out():
    xs = [
        duty(1, primaries=(A,)),
        duty(2, primaries=(A,)),
        duty(3, primaries=(A,), paused=True),
        duty(4, backups=(A,), paused=True),
    ]
    (load,) = d.workload(xs, [A])
    assert (load.primary, load.primary_paused, load.primary_active) == (3, 1, 2)
    assert (load.backup, load.backup_paused, load.backup_active) == (1, 1, 0)


def test_anyone_in_leadership_backups_count_toward_nobody():
    xs = [duty(primaries=(A,), backups=(B,), backup_anyone_leadership=True)]
    loads = {x.user_id: x for x in d.workload(xs, [A, B])}
    assert loads[B].backup == 0


def test_a_holder_outside_leadership_still_appears_and_is_marked():
    loads = d.workload([duty(primaries=(OUTSIDER,))], [A])
    assert [(x.user_id, x.in_leadership, x.primary) for x in loads] == [
        (A, True, 0),
        (OUTSIDER, False, 1),
    ]


def test_open_slots_and_duplicate_listing_dont_inflate_counts():
    xs = [duty(primaries=(A, A, d.OPEN))]
    (load,) = d.workload(xs, [A])
    assert load.primary == 1


def test_open_slots_lists_each_duty_and_kind_with_count():
    xs = [
        duty(1, primaries=(A, d.OPEN, d.OPEN)),
        duty(2, primaries=(B,), backups=(d.OPEN,), paused=True),
        duty(3, primaries=(C,)),
    ]
    got = [(s.duty.id, s.kind, s.count) for s in d.open_slots(xs)]
    assert got == [(1, d.SLOT_PRIMARY, 2), (2, d.SLOT_BACKUP, 1)]


# ── Reminder schedules ───────────────────────────────────────────────────────


def test_weekday_schedule():
    r = reminder(weekdays=frozenset({0, 3}))  # Mon, Thu
    assert d.occurs_on(r, date(2026, 9, 28))  # Monday
    assert not d.occurs_on(r, date(2026, 9, 29))
    assert d.occurs_on(r, date(2026, 10, 1))  # Thursday


def test_interval_schedule_counts_from_the_anchor_and_not_before():
    r = reminder(schedule_type=d.SCHEDULE_INTERVAL, anchor_date=date(2026, 9, 1), interval_days=3)
    assert d.occurs_on(r, date(2026, 9, 1))
    assert not d.occurs_on(r, date(2026, 9, 2))
    assert d.occurs_on(r, date(2026, 9, 4))
    assert not d.occurs_on(r, date(2026, 8, 29))  # before the anchor


@pytest.mark.parametrize(
    "kw",
    [
        {"schedule_type": d.SCHEDULE_INTERVAL, "anchor_date": None},
        {"schedule_type": d.SCHEDULE_INTERVAL, "anchor_date": date(2026, 9, 1), "interval_days": 0},
        {"schedule_type": "monthly"},
    ],
)
def test_an_incomplete_schedule_never_fires(kw):
    assert not d.occurs_on(reminder(**kw), date(2026, 9, 1))


def _now(hh, mm, day=date(2026, 9, 28)):
    return datetime(day.year, day.month, day.day, hh, mm, 30)


def test_due_at_or_past_the_time_so_a_late_tick_still_fires():
    r = reminder(weekdays=frozenset({0}), at=time(9, 0))
    assert d.due_occurrence(r, _now(8, 59)) is None
    assert d.due_occurrence(r, _now(9, 0)) == date(2026, 9, 28)
    assert d.due_occurrence(r, _now(13, 5)) == date(2026, 9, 28)


def test_fires_once_per_occurrence():
    r = reminder(weekdays=frozenset({0}), last_fired_on=date(2026, 9, 28))
    assert d.due_occurrence(r, _now(10, 0)) is None
    r = reminder(weekdays=frozenset({0}), last_fired_on=date(2026, 9, 21))
    assert d.due_occurrence(r, _now(10, 0)) == date(2026, 9, 28)


def test_switched_off_or_off_day_never_fires():
    assert d.due_occurrence(reminder(weekdays=frozenset({0}), enabled=False), _now(10, 0)) is None
    assert d.due_occurrence(reminder(weekdays=frozenset({1})), _now(10, 0)) is None


def test_a_paused_duty_stops_its_reminders():
    r = reminder(weekdays=frozenset({0}))
    assert d.should_fire(duty(paused=True), r, _now(10, 0)) is None
    assert d.should_fire(duty(), r, _now(10, 0)) == date(2026, 9, 28)


def test_a_reminder_never_fires_against_another_duty():
    r = reminder(duty_id=2, weekdays=frozenset({0}))
    assert d.should_fire(duty(1), r, _now(10, 0)) is None


# ── Delivery ─────────────────────────────────────────────────────────────────

LEAD_CH = 555


def test_dm_primaries_reads_current_holders_and_skips_open_slots():
    x = duty(primaries=(A, d.OPEN), backups=(B,))
    got = d.plan_delivery(x, reminder(send_to=d.SEND_PRIMARIES), leadership_channel_id=LEAD_CH)
    assert got == d.Delivery(dm_user_ids=(A,))


def test_dm_both_sends_once_to_someone_in_both_slots():
    x = duty(primaries=(A, B), backups=(B, C))
    got = d.plan_delivery(x, reminder(send_to=d.SEND_BOTH), leadership_channel_id=LEAD_CH)
    assert got.dm_user_ids == (A, B, C)
    assert got.channel_id == 0


def test_anyone_in_leadership_backups_go_to_a_channel_not_every_dm():
    x = duty(primaries=(A,), backups=(B,), backup_anyone_leadership=True)
    got = d.plan_delivery(x, reminder(send_to=d.SEND_BOTH), leadership_channel_id=LEAD_CH)
    assert got.dm_user_ids == (A,)
    assert got.channel_id == LEAD_CH
    assert not got.ping_leadership

    own = d.plan_delivery(
        x,
        reminder(send_to=d.SEND_BACKUPS, channel_id=777, ping_holders=True),
        leadership_channel_id=LEAD_CH,
    )
    assert own == d.Delivery(channel_id=777, ping_leadership=True)


def test_channel_post_pings_holders_only_when_asked():
    x = duty(primaries=(A,), backups=(B,))
    quiet = d.plan_delivery(
        x, reminder(send_to=d.SEND_CHANNEL, channel_id=777), leadership_channel_id=LEAD_CH
    )
    assert quiet == d.Delivery(channel_id=777)
    loud = d.plan_delivery(
        x,
        reminder(send_to=d.SEND_CHANNEL, channel_id=777, ping_holders=True),
        leadership_channel_id=LEAD_CH,
    )
    assert loud == d.Delivery(channel_id=777, ping_user_ids=(A, B))


# ── Rendering ────────────────────────────────────────────────────────────────


def _name(uid):
    return f"<@{uid}>"


def test_render_names_current_holders_and_survives_typos():
    x = duty(name="Daily Schedule", primaries=(A, d.OPEN), backups=(B,))
    r = reminder(message="{duty}: {primary} (backup {backup}) {nme} {")
    out = d.render_reminder(r, x, name_of=_name, anyone_in_leadership="leadership")
    assert out == f"Daily Schedule: <@{A}> (backup <@{B}>) {{nme}} {{"


def test_render_anyone_in_leadership_backup():
    x = duty(primaries=(A,), backups=(B,), backup_anyone_leadership=True)
    r = reminder(message="{backup}")
    assert d.render_reminder(r, x, name_of=_name, anyone_in_leadership="LEAD") == "LEAD"


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        ("{a} and {b}", "1 and {b}"),
        ("{a:>5}", "    1"),
        ("{0} {a}", "{0} 1"),
        ("{a.x} {a}", "{a.x} 1"),
        ("}{a}", "}1"),
    ],
)
def test_template_render_never_raises(template, expected):
    assert template_render.render(template, a="1") == expected

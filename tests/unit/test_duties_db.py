"""Leadership Duties (#687): storage in guild_configs.db."""

from dataclasses import replace
from datetime import date, time

import pytest

import config
import duties as d
import duties_db as db

G = 1
OTHER_G = 2
A, B, C = 101, 102, 103


def new_duty(name="Disputes", guild_id=G, **kw) -> d.Duty:
    return d.Duty(id=0, guild_id=guild_id, name=name, **kw)


def new_reminder(duty_id, guild_id=G, **kw) -> d.DutyReminder:
    kw.setdefault("message", "check {duty}")
    kw.setdefault("schedule_type", d.SCHEDULE_WEEKDAYS)
    kw.setdefault("at", time(9, 30))
    kw.setdefault("weekdays", frozenset({0, 4}))
    return d.DutyReminder(id=0, guild_id=guild_id, duty_id=duty_id, **kw)


# ── Duties ───────────────────────────────────────────────────────────────────


def test_round_trip_keeps_every_field_and_slot_order(temp_db):
    duty_id = db.save_duty(
        new_duty(
            category="Members",
            description="Concerns between members",
            primaries=(B, d.OPEN, A),
            backups=(C,),
            contact_enabled=True,
            sort_order=3,
        )
    )
    got = db.get_duty(G, duty_id)
    assert got == d.Duty(
        id=duty_id,
        guild_id=G,
        name="Disputes",
        category="Members",
        description="Concerns between members",
        primaries=(B, d.OPEN, A),
        backups=(C,),
        contact_enabled=True,
        sort_order=3,
    )


def test_update_replaces_holders(temp_db):
    duty_id = db.save_duty(new_duty(primaries=(A, B), backups=(C,)))
    db.save_duty(replace(db.get_duty(G, duty_id), primaries=(C,), backups=()))
    got = db.get_duty(G, duty_id)
    assert got.primaries == (C,)
    assert got.backups == ()


def test_named_backups_survive_switching_to_anyone_in_leadership_and_back(temp_db):
    duty_id = db.save_duty(new_duty(backups=(B, C)))
    db.save_duty(replace(db.get_duty(G, duty_id), backup_anyone_leadership=True))
    assert db.get_duty(G, duty_id).holders(d.SLOT_BACKUP) == ()
    db.save_duty(replace(db.get_duty(G, duty_id), backup_anyone_leadership=False))
    assert db.get_duty(G, duty_id).holders(d.SLOT_BACKUP) == (B, C)


def test_names_are_unique_per_server_ignoring_case(temp_db):
    db.save_duty(new_duty("Disputes"))
    with pytest.raises(db.DuplicateDutyName):
        db.save_duty(new_duty("disputes"))
    # Another server may use the same name.
    db.save_duty(new_duty("Disputes", guild_id=OTHER_G))


def test_a_failed_duplicate_save_leaves_no_holder_rows(temp_db):
    db.save_duty(new_duty("Disputes"))
    with pytest.raises(db.DuplicateDutyName):
        db.save_duty(new_duty("DISPUTES", primaries=(A,)))
    with config._get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM guild_duty_holders").fetchone()[0]
    assert n == 0


def test_blank_name_is_refused(temp_db):
    with pytest.raises(ValueError):
        db.save_duty(new_duty("   "))


def test_updating_a_missing_duty_is_an_error(temp_db):
    with pytest.raises(LookupError):
        db.save_duty(replace(new_duty(), id=999))


def test_list_is_in_leadership_order_and_scoped_to_the_server(temp_db):
    db.save_duty(new_duty("Late", sort_order=5))
    db.save_duty(new_duty("Early", sort_order=1))
    db.save_duty(new_duty("Elsewhere", guild_id=OTHER_G))
    assert [x.name for x in db.list_duties(G)] == ["Early", "Late"]


def test_get_duty_from_another_server_is_none(temp_db):
    duty_id = db.save_duty(new_duty(guild_id=OTHER_G))
    assert db.get_duty(G, duty_id) is None


def test_pause_keeps_every_setting(temp_db):
    duty_id = db.save_duty(new_duty(primaries=(A,), contact_enabled=True))
    before = db.get_duty(G, duty_id)
    assert db.set_duty_paused(G, duty_id, True)
    assert db.get_duty(G, duty_id) == replace(before, paused=True)
    assert db.set_duty_paused(G, duty_id, False)
    assert db.get_duty(G, duty_id) == before
    assert not db.set_duty_paused(G, 999, True)


def test_delete_takes_holders_and_reminders_with_it(temp_db):
    keep = db.save_duty(new_duty("Keep", primaries=(A,)))
    gone = db.save_duty(new_duty("Gone", primaries=(A,)))
    db.save_reminder(new_reminder(keep))
    db.save_reminder(new_reminder(gone))
    assert db.delete_duty(G, gone)
    assert not db.delete_duty(G, gone)
    assert [x.name for x in db.list_duties(G)] == ["Keep"]
    assert [r.duty_id for r in db.list_reminders(G)] == [keep]
    with config._get_conn() as conn:
        rows = conn.execute("SELECT duty_id FROM guild_duty_holders").fetchall()
    assert {r["duty_id"] for r in rows} == {keep}


def test_vacate_holder_opens_their_slots_and_names_the_duties(temp_db):
    one = db.save_duty(new_duty("One", primaries=(A, B), sort_order=1))
    two = db.save_duty(new_duty("Two", backups=(B,), sort_order=0))
    db.save_duty(new_duty("Three", primaries=(C,)))
    elsewhere = db.save_duty(new_duty("One", guild_id=OTHER_G, primaries=(B,)))

    assert db.vacate_holder(G, B) == ["Two", "One"]
    assert db.get_duty(G, one).primaries == (A, d.OPEN)
    assert db.get_duty(G, two).backups == (d.OPEN,)
    assert db.get_duty(OTHER_G, elsewhere).primaries == (B,)
    assert db.vacate_holder(G, B) == []
    assert db.vacate_holder(G, d.OPEN) == []


# ── Reminders ────────────────────────────────────────────────────────────────


def test_reminder_round_trip(temp_db):
    duty_id = db.save_duty(new_duty())
    r = new_reminder(
        duty_id,
        schedule_type=d.SCHEDULE_INTERVAL,
        anchor_date=date(2026, 9, 1),
        interval_days=3,
        weekdays=frozenset(),
        send_to=d.SEND_CHANNEL,
        channel_id=777,
        ping_holders=True,
    )
    rid = db.save_reminder(r)
    assert db.list_reminders(G, duty_id) == [replace(r, id=rid)]


def test_weekday_reminder_round_trip(temp_db):
    duty_id = db.save_duty(new_duty())
    rid = db.save_reminder(new_reminder(duty_id, weekdays=frozenset({4, 0, 6})))
    (got,) = db.list_reminders(G)
    assert got.id == rid
    assert got.weekdays == frozenset({0, 4, 6})
    assert got.at == time(9, 30)
    assert got.anchor_date is None


def test_enabled_reminders_spans_servers_and_skips_switched_off(temp_db):
    a = db.save_duty(new_duty())
    b = db.save_duty(new_duty(guild_id=OTHER_G))
    db.save_reminder(new_reminder(a))
    db.save_reminder(new_reminder(a, enabled=False))
    db.save_reminder(new_reminder(b, guild_id=OTHER_G))
    assert [(r.guild_id, r.duty_id) for r in db.enabled_reminders()] == [(G, a), (OTHER_G, b)]


def test_fired_marker_claims_each_occurrence_once(temp_db):
    duty_id = db.save_duty(new_duty())
    rid = db.save_reminder(new_reminder(duty_id))
    day = date(2026, 9, 28)
    assert db.mark_reminder_fired(G, rid, day)
    assert not db.mark_reminder_fired(G, rid, day)  # a second tick loses the claim
    assert not db.mark_reminder_fired(G, rid, date(2026, 9, 21))  # never backwards
    assert db.list_reminders(G)[0].last_fired_on == day
    assert db.mark_reminder_fired(G, rid, date(2026, 10, 2))


def test_editing_a_reminder_keeps_its_fired_marker(temp_db):
    duty_id = db.save_duty(new_duty())
    rid = db.save_reminder(new_reminder(duty_id))
    db.mark_reminder_fired(G, rid, date(2026, 9, 28))
    db.save_reminder(replace(db.list_reminders(G)[0], message="edited", last_fired_on=None))
    got = db.list_reminders(G)[0]
    assert got.message == "edited"
    assert got.last_fired_on == date(2026, 9, 28)


def test_delete_reminder(temp_db):
    duty_id = db.save_duty(new_duty())
    rid = db.save_reminder(new_reminder(duty_id))
    assert not db.delete_reminder(OTHER_G, rid)
    assert db.delete_reminder(G, rid)
    assert db.list_reminders(G) == []


def test_updating_a_missing_reminder_is_an_error(temp_db):
    with pytest.raises(LookupError):
        db.save_reminder(replace(new_reminder(1), id=999))


# ── Settings ─────────────────────────────────────────────────────────────────


def test_settings_default_then_upsert(temp_db):
    assert db.get_settings(G) == db.DutiesSettings(guild_id=G)
    db.save_settings(db.DutiesSettings(guild_id=G, panel_channel_id=5, ticket_channel_id=6))
    db.save_settings(
        db.DutiesSettings(guild_id=G, panel_channel_id=5, panel_message_id=7, ticket_channel_id=6)
    )
    assert db.get_settings(G) == db.DutiesSettings(
        guild_id=G, panel_channel_id=5, panel_message_id=7, ticket_channel_id=6
    )


# ── Removal ──────────────────────────────────────────────────────────────────


def test_server_removal_takes_every_duty_row(temp_db):
    duty_id = db.save_duty(new_duty(primaries=(A,)))
    db.save_reminder(new_reminder(duty_id))
    db.save_settings(db.DutiesSettings(guild_id=G, panel_channel_id=5))
    kept = db.save_duty(new_duty(guild_id=OTHER_G, primaries=(A,)))

    result = config.purge_guild_data(G, apply=True)

    for table in ("guild_duties", "guild_duty_holders", "guild_duty_reminders"):
        assert result["deleted"].get(table) == 1, table
    assert result["deleted"].get("guild_duties_config") == 1
    assert db.list_duties(G) == []
    assert db.get_duty(OTHER_G, kept).primaries == (A,)


def test_a_person_purge_leaves_their_positions_open(temp_db):
    one = db.save_duty(new_duty("One", primaries=(A, B)))
    other = db.save_duty(new_duty("One", guild_id=OTHER_G, backups=(B,)))

    result = config.purge_user_data(B, apply=True)

    assert result["scrubbed"].get("guild_duty_holders") == 2
    assert db.get_duty(G, one).primaries == (A, d.OPEN)
    assert db.get_duty(OTHER_G, other).backups == (d.OPEN,)

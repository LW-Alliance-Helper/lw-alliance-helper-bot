"""Leadership Duties (#687): the departed-holder notice, and the
config_health pieces it needed (a notice kind, dismiss, set_detail)."""

from datetime import datetime, timedelta, timezone

import discord

import config
import config_health as ch
import duties as d
import duties_db as db
import duties_health as dh

G = 1
A, B = 101, 102


def _row(subject=dh.HOLDERS):
    with config._get_conn() as conn:
        return conn.execute(
            "SELECT * FROM guild_config_health WHERE guild_id = ? AND subject = ?", (G, subject)
        ).fetchone()


def _duty(name, **kw):
    return db.save_duty(d.Duty(id=0, guild_id=G, name=name, **kw))


def test_subjects_are_registered_with_their_fix():
    subject = ch.get_subject(dh.HOLDERS)
    assert subject.fix_hub == "/duties"
    assert subject.fix_btn == "✏️ Edit a duty"
    for key in (dh.REMINDER_CHANNEL, dh.PANEL_CHANNEL, dh.TICKET_CHANNEL):
        assert ch.get_subject(key).label != "part of your setup"


def test_a_departure_records_a_notice_naming_them(temp_db):
    _duty("Disputes", primaries=(A,))
    db.vacate_holder(G, A, "Alpha")
    dh.note_departures(G)
    row = _row()
    assert row["kind"] == ch.HOLDER_LEFT
    assert "**Alpha** left the server" in row["detail"]
    assert "**Disputes**" in row["detail"]


def test_someone_else_leaving_is_a_new_notice(temp_db):
    _duty("One", primaries=(A,))
    _duty("Two", primaries=(B,))
    db.vacate_holder(G, A, "Alpha")
    dh.note_departures(G)
    with config._get_conn() as conn:
        conn.execute("UPDATE guild_config_health SET notified_at = ?", ("2026-09-01T00:00:00",))
        conn.commit()
    db.vacate_holder(G, B, "Bravo")
    dh.note_departures(G)
    assert _row()["notified_at"] is None  # posts promptly, not after the quiet window


def test_dealing_with_part_of_it_narrows_the_text_without_a_new_post(temp_db):
    one = _duty("One", primaries=(A,))
    _duty("Two", primaries=(B,))
    db.vacate_holder(G, A, "Alpha")
    db.vacate_holder(G, B, "Bravo")
    dh.note_departures(G)
    with config._get_conn() as conn:
        conn.execute("UPDATE guild_config_health SET notified_at = ?", ("2026-09-01T00:00:00",))
        conn.commit()

    db.save_duty(db.get_duty(G, one))
    dh.sync_departures(G)

    row = _row()
    assert "Alpha" not in row["detail"] and "Bravo" in row["detail"]
    assert row["notified_at"] == "2026-09-01T00:00:00"


def test_dealing_with_all_of_it_ends_the_notice_with_no_recovery_line(temp_db):
    one = _duty("One", primaries=(A,))
    db.vacate_holder(G, A, "Alpha")
    dh.note_departures(G)
    with config._get_conn() as conn:
        conn.execute("UPDATE guild_config_health SET notified_at = ?", ("2026-09-01T00:00:00",))
        conn.commit()

    db.save_duty(db.get_duty(G, one))
    dh.sync_departures(G)

    assert _row() is None  # gone, not a tombstone waiting to say "working again"


def test_dismiss_and_set_detail_are_safe_without_a_row(temp_db):
    ch.dismiss(G, dh.HOLDERS)
    ch.set_detail(G, dh.HOLDERS, "x")
    assert _row() is None


def _problem(kind, subject="s"):
    return ch.Problem(G, subject, kind, "", "detail", "t", None, None)


def test_a_notice_only_digest_says_needs_a_look_in_orange():
    embed = ch.build_digest_embed([_problem(ch.HOLDER_LEFT)])
    assert embed.description == "Something in your setup needs a look."
    assert embed.color == discord.Color.orange()
    assert "again tomorrow" in embed.footer.text


def test_a_mixed_digest_leads_with_what_stopped():
    embed = ch.build_digest_embed([_problem(ch.CHANNEL_GONE, "a"), _problem(ch.HOLDER_LEFT, "b")])
    assert embed.description.startswith("Something I was told to use has stopped working")
    assert embed.description.endswith("1 other thing also needs a look.")
    assert embed.color == discord.Color.red()


def test_the_broken_only_digest_is_unchanged():
    embed = ch.build_digest_embed([_problem(ch.CHANNEL_GONE, "a"), _problem(ch.NO_ACCESS, "b")])
    assert embed.description == (
        "2 things I was told to use have stopped working, so the features "
        "that depend on them aren't running."
    )
    assert embed.color == discord.Color.red()


def test_a_person_purge_rewrites_the_notice_that_named_them(temp_db):
    _duty("One", primaries=(A,))
    _duty("Two", primaries=(B,))
    db.vacate_holder(G, A, "Alpha")
    db.vacate_holder(G, B, "Bravo")
    dh.note_departures(G)

    config.purge_user_data(A, apply=True)
    assert "Alpha" not in _row()["detail"] and "Bravo" in _row()["detail"]

    config.purge_user_data(B, apply=True)
    assert _row() is None


def test_a_per_channel_subject_takes_its_base_copy():
    assert (
        ch.get_subject(dh.reminder_channel_subject(55)).label
        == ch.get_subject(dh.REMINDER_CHANNEL).label
    )

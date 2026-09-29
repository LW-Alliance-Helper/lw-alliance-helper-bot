"""Leadership Duties (#687): what each surface says, from plain values."""

from datetime import date, datetime, time, timezone

import discord
import pytest

import duties as d
import duties_copy as c
import duties_render as r

A, B, C = 101, 102, 103
NAMES = {A: "Alpha", B: "Bravo", C: "Charlie"}


def name_of(uid):
    return NAMES.get(uid, f"#{uid}")


def duty(id=1, **kw):
    kw.setdefault("name", f"Duty {id}")
    return d.Duty(id=id, guild_id=1, **kw)


def reminder(**kw):
    kw.setdefault("id", 1)
    kw.setdefault("guild_id", 1)
    kw.setdefault("duty_id", 1)
    kw.setdefault("message", "hello")
    kw.setdefault("schedule_type", d.SCHEDULE_WEEKDAYS)
    kw.setdefault("at", time(21, 0))
    return d.DutyReminder(**kw)


# ── The copy itself ──────────────────────────────────────────────────────────


def _copy_strings():
    for name in dir(c):
        if name.startswith("__"):
            continue  # the module docstring is for us, not a user
        value = getattr(c, name)
        if isinstance(value, str):
            yield name, value
        elif isinstance(value, tuple):
            for i, item in enumerate(value):
                if isinstance(item, str):
                    yield f"{name}[{i}]", item


def test_no_em_dashes_in_anything_a_user_sees():
    """UX.md, Voice: if a user can see it, no em dashes."""
    offenders = [n for n, v in _copy_strings() if "—" in v]
    assert not offenders


def test_us_english():
    offenders = [n for n, v in _copy_strings() if "cancelled" in v.lower()]
    assert not offenders


def test_no_bare_slot_in_copy():
    """ "Slot" is a storm time slot in this product; a duty has positions."""
    offenders = [n for n, v in _copy_strings() if "slot" in v.lower()]
    assert not offenders


# ── Holders ──────────────────────────────────────────────────────────────────


def test_people_lists_names_then_open_positions():
    x = duty(primaries=(A, d.OPEN, B))
    assert r.people(x, d.SLOT_PRIMARY, name_of) == f"Alpha, Bravo, {c.OPEN_MARK}"


def test_people_says_nobody_and_anyone_in_leadership():
    x = duty(primaries=(), backups=(C, d.ANYONE, d.OPEN))
    assert r.people(x, d.SLOT_PRIMARY, name_of) == c.NOBODY
    assert r.people(x, d.SLOT_BACKUP, name_of) == (
        f"Charlie, {c.ANYONE_IN_LEADERSHIP}, {c.OPEN_MARK}"
    )


def test_duty_block_marks_paused():
    block = r.duty_block(duty(name="Disputes", primaries=(A,), paused=True), name_of)
    assert block.splitlines() == [
        f"**Disputes**{c.PAUSED_MARK}",
        "Primary: Alpha",
        f"Backup: {c.NOBODY}",
    ]


# ── Hub ──────────────────────────────────────────────────────────────────────


def test_hub_groups_by_category_with_headings():
    xs = [duty(1, category="VS"), duty(2, category=""), duty(3, category="VS")]
    (page,) = r.hub_pages(xs, name_of)
    assert page.index("### VS") < page.index("**Duty 1**") < page.index("**Duty 3**")
    assert f"### {c.HUB_NO_CATEGORY}" in page


def test_hub_pages_repeat_the_heading_on_a_continued_category():
    long = "x" * 900
    xs = [duty(i, name=f"D{i} {long}", category="Members") for i in range(1, 8)]
    pages = r.hub_pages(xs, name_of)
    assert len(pages) > 1
    assert all(p.startswith("### Members") for p in pages)
    assert all(len(p) <= 4000 for p in pages)


@pytest.mark.parametrize(
    ("state", "has", "expect"),
    [
        (r.STATE_FREE, False, c.HUB_FREE),
        (r.STATE_FREE, True, c.HUB_LAPSED),
        (r.STATE_LAPSED, True, c.HUB_LAPSED),
        (r.STATE_PREMIUM, False, c.HUB_EMPTY),
        (r.STATE_PREMIUM, True, c.HUB_INTRO),
    ],
)
def test_hub_intro_by_state(state, has, expect):
    xs = [duty(primaries=(A,), contact_enabled=True)] if has else []
    embed, pages = r.hub_embed(xs, name_of, state=state)
    assert expect in embed.description
    assert pages == 1
    assert embed.color == discord.Color.blurple()


def test_a_lapsed_hub_still_shows_the_whole_list():
    xs = [duty(1, name="Disputes", primaries=(A,))]
    embed, _ = r.hub_embed(xs, name_of, state=r.STATE_LAPSED)
    assert "**Disputes**" in embed.description


def test_hub_footer_counts():
    xs = [duty(1, contact_enabled=True), duty(2)]
    embed, _ = r.hub_embed(xs, name_of, state=r.STATE_PREMIUM)
    assert embed.footer.text == c.HUB_FOOTER.format(count=2, contact=1)


# ── Workload ─────────────────────────────────────────────────────────────────


def test_workload_is_in_name_order_not_load_order():
    loads = [
        d.LeaderLoad(user_id=C, primary=9),
        d.LeaderLoad(user_id=A, primary=0),
        d.LeaderLoad(user_id=B, primary=4),
    ]
    embed, _ = r.workload_embed(loads, [], name_of, sort_key=name_of, role_known=True)
    text = embed.description
    assert text.index("**Alpha**") < text.index("**Bravo**") < text.index("**Charlie**")
    assert embed.color == discord.Color.blurple()


def test_workload_block_counts_and_paused_note():
    load = d.LeaderLoad(user_id=A, primary=3, primary_paused=1, backup=2)
    assert r.workload_block(load, name_of).splitlines() == [
        "**Alpha**",
        "Primary: 3 (1 paused)",
        "Backup: 2",
    ]


def test_workload_marks_a_holder_outside_leadership_and_a_missing_role():
    loads = [d.LeaderLoad(user_id=A, primary=1, in_leadership=False)]
    embed, _ = r.workload_embed(loads, [], name_of, sort_key=name_of, role_known=False)
    assert f"**Alpha**{c.WORKLOAD_NOT_LEADERSHIP}" in embed.description
    assert c.WORKLOAD_NO_ROLE in embed.description


def test_open_positions_on_the_first_page_only():
    loads = [d.LeaderLoad(user_id=i) for i in range(30)]
    slots = [d.OpenSlot(duty=duty(name="Disputes", paused=True), kind=d.SLOT_BACKUP, count=2)]
    first, pages = r.workload_embed(loads, slots, name_of, sort_key=name_of, role_known=True)
    second, _ = r.workload_embed(loads, slots, name_of, sort_key=name_of, role_known=True, page=1)
    assert pages == 3
    assert [f.name for f in first.fields] == [c.WORKLOAD_OPEN_FIELD]
    assert first.fields[0].value == f"• **Disputes**{c.PAUSED_MARK}: 2 backups"
    assert second.fields == []


def test_no_open_positions_says_so():
    assert r.open_positions_text([]) == c.WORKLOAD_OPEN_NONE


# ── My duties ────────────────────────────────────────────────────────────────


def test_my_duties_keeps_both_field_names_when_one_is_empty():
    embed = r.my_duties_embed([duty(name="Disputes")], [])
    assert [(f.name, f.value) for f in embed.fields] == [
        (c.MY_PRIMARY_FIELD, "• **Disputes**"),
        (c.MY_BACKUP_FIELD, c.MY_EMPTY_FIELD),
    ]


def test_my_duties_none():
    assert r.my_duties_embed([], []).description == c.MY_NONE


# ── Reminders ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kw", "expect"),
    [
        ({"weekdays": frozenset({3, 0})}, "Mondays and Thursdays at 9:00pm"),
        ({"weekdays": frozenset(range(7))}, "Every day at 9:00pm"),
        ({"weekdays": frozenset()}, c.WHEN_UNSET),
        ({"schedule_type": d.SCHEDULE_DAILY}, "Every day at 9:00pm"),
        (
            {"schedule_type": d.SCHEDULE_EVERY_2_DAYS, "anchor_date": date(2026, 9, 1)},
            "Every other day from Sep 1 at 9:00pm",
        ),
        (
            {"schedule_type": d.SCHEDULE_EVERY_3_DAYS, "anchor_date": date(2026, 9, 1)},
            "Every 3 days from Sep 1 at 9:00pm",
        ),
        (
            {"schedule_type": d.SCHEDULE_WEEKLY, "weekdays": frozenset({2})},
            "Every Wednesday at 9:00pm",
        ),
        (
            {"schedule_type": d.SCHEDULE_EVERY_2_WEEKS, "anchor_date": date(2026, 9, 7)},
            "Every 2 weeks from Sep 7 at 9:00pm",
        ),
        (
            {"schedule_type": d.SCHEDULE_MONTHLY, "anchor_date": date(2026, 9, 1)},
            "Every month on the 1st at 9:00pm",
        ),
        (
            {"schedule_type": d.SCHEDULE_MONTHLY, "anchor_date": date(2026, 9, 22)},
            "Every month on the 22nd at 9:00pm",
        ),
        (
            {"schedule_type": d.SCHEDULE_MONTHLY, "anchor_date": date(2026, 8, 31)},
            "Every month on the 31st, or the last day of shorter months, at 9:00pm",
        ),
        ({"schedule_type": d.SCHEDULE_MONTHLY}, c.WHEN_UNSET),
        ({"at": None, "weekdays": frozenset({0})}, c.WHEN_UNSET),
    ],
)
def test_reminder_when(kw, expect):
    assert r.reminder_when(reminder(**kw), None) == expect


def test_reminder_dest():
    assert r.reminder_dest(reminder(send_to=d.SEND_BOTH)) == c.SEND_BOTH
    assert r.reminder_dest(reminder(send_to=d.SEND_CHANNEL, channel_id=7)) == "<#7>"
    assert r.reminder_dest(reminder(send_to=d.SEND_CHANNEL)) == c.DEST_NO_CHANNEL


def test_reminder_list_notes_a_paused_duty():
    embed = r.reminder_list_embed(duty(name="X", paused=True), [reminder()], None)
    assert c.REMINDERS_PAUSED_NOTE.format(name="X") in embed.description
    assert "> hello" in embed.description


def test_reminder_text_dm_names_the_server():
    x = duty(name="Daily Schedule", primaries=(A,))
    rem = reminder(message="{primary}, post it")
    dm = r.reminder_text(x, rem, name_of, server="Test Server", dm=True)
    assert dm == "🔔 **Daily Schedule** reminder from **Test Server**\nAlpha, post it"
    post = r.reminder_text(x, rem, name_of, server="Test Server", dm=False)
    assert post == "🔔 **Daily Schedule**\nAlpha, post it"


def test_reminder_text_is_clamped():
    rem = reminder(message="y" * 3000)
    assert len(r.reminder_text(duty(), rem, name_of, server="S", dm=True)) == 2000


def test_editor_notes_where_anyone_in_leadership_reminders_go():
    x = duty(backups=(d.ANYONE,))
    embed = r.reminder_editor_embed(
        x, reminder(send_to=d.SEND_BOTH), None, name_of, leadership_channel_id=55
    )
    assert c.REMINDER_ANYONE_NOTE.format(channel="<#55>") in embed.description
    assert c.REMINDER_PING_OFF in embed.description
    quiet = r.reminder_editor_embed(
        x, reminder(send_to=d.SEND_PRIMARIES), None, name_of, leadership_channel_id=55
    )
    assert c.REMINDER_ANYONE_NOTE.format(channel="<#55>") not in quiet.description


def test_thread_destination_wording():
    assert r.reminder_dest(reminder(send_to=d.SEND_THREAD)) == c.DEST_NO_THREAD
    assert r.reminder_dest(reminder(send_to=d.SEND_THREAD, channel_id=9)) == "<#9>"
    assert r.send_label(d.SEND_THREAD) == c.SEND_THREAD


def test_panel_names_anyone_in_leadership():
    x = duty(name="Disputes", primaries=(A, d.ANYONE), contact_enabled=True)
    (field,) = r.panel_embed([x], name_of).fields
    assert f"Handled by Alpha and {c.ANYONE_IN_LEADERSHIP}" in field.value


def test_fallback_names_who_it_was_for():
    text = r.fallback_text(duty(name="X"), ["<@1>", "<@2>"], "do it")
    assert text == "🔔 I couldn't DM <@1> and <@2> this **X** reminder, so here it is:\ndo it"


# ── Contact ──────────────────────────────────────────────────────────────────


def test_contact_warnings():
    check = r.ContactCheck(bot_can_thread=False, members_can_view=False, manage_threads=("@M",))
    warnings = r.contact_warnings(check, "#t")
    assert warnings == [
        c.WARN_BOT_THREADS.format(channel="#t"),
        c.WARN_MEMBERS_CANT_VIEW.format(channel="#t"),
        c.WARN_MANAGE_THREADS.format(channel="#t", who="@M"),
    ]
    assert r.contact_warnings(r.ContactCheck(members_can_view=None), "#t") == [
        c.WARN_NO_MEMBER_ROLE.format(channel="#t")
    ]
    assert r.contact_warnings(r.ContactCheck(), "#t") == []


def test_contact_settings_embed_colors_by_warnings():
    common = dict(panel_channel_id=5, ticket_channel_id=0, posted=True, contact_count=2)
    calm = r.contact_settings_embed(warnings=[], **common)
    assert calm.color == discord.Color.blurple()
    assert c.CONTACT_THREADS_LINE.format(channel=c.CONTACT_SAME_CHANNEL) in calm.description
    warned = r.contact_settings_embed(warnings=["⚠️ x"], **common)
    assert warned.color == discord.Color.orange()


def test_contact_duties_skip_paused_and_off_and_cap():
    xs = [duty(i, contact_enabled=True) for i in range(1, 30)]
    xs.append(duty(99, contact_enabled=True, paused=True))
    xs.append(duty(98))
    shown = r.contact_duties(xs)
    assert len(shown) == r.CONTACT_CAP
    assert all(x.id not in (98, 99) for x in shown)


def test_panel_embed():
    xs = [
        duty(
            1,
            name="Disputes",
            description="Trouble between members",
            primaries=(A,),
            backups=(B, C),
            contact_enabled=True,
        )
    ]
    embed = r.panel_embed(xs, name_of)
    assert embed.description == c.PANEL_INTRO
    (field,) = embed.fields
    assert field.name == "Disputes"
    assert field.value.splitlines() == [
        "Trouble between members",
        "Handled by Alpha",
        "Backup: Bravo and Charlie",
    ]
    assert r.panel_embed([], name_of).description == c.PANEL_EMPTY


def test_thread_name_is_in_server_time_and_fits():
    now = datetime(2026, 9, 27, 16, 5, tzinfo=timezone.utc)  # 14:05 server time
    assert r.thread_name("Disputes", "TestPlayer", now) == "Disputes · TestPlayer · 27 Sep 14:05"
    long = r.thread_name("Disputes", "N" * 200, now)
    assert len(long) == 100
    assert long.endswith("· 27 Sep 14:05")


def test_thread_opener_grammar():
    x = duty(name="Disputes")
    one = r.thread_opener(x, 9, [A])
    assert one.startswith(f"<@9> opened this about **Disputes**, which <@{A}> handles.")
    two = r.thread_opener(x, 9, [A, B])
    assert f"<@{A}> and <@{B}> handle." in two


def test_departure_detail():
    class Dep:
        def __init__(self, name, duty_names):
            self.name, self.duty_names = name, duty_names

    text = r.departure_detail([Dep("Zed", ("One",)), Dep("", ("Two", "Three"))])
    assert text.splitlines() == [
        "**Zed** left the server. Their duty **One** now has an open position.",
        "**Someone** left the server. Their duties **Two** and **Three** now have an open position.",
    ]

"""Leadership Duties (#687): the /duties hub and its screens, clicked."""

from dataclasses import replace
from datetime import date, time
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import duties as d
import duties_copy as c
import duties_db as db
import duties_hub as h
import duties_render as r
from tests.conftest import TEST_GUILD_ID

G = TEST_GUILD_ID
OWNER = 42
A, B, C = 101, 102, 103


def _inter():
    inter = MagicMock()
    inter.user = MagicMock(id=OWNER)
    inter.response = MagicMock()
    for name in ("edit_message", "send_message", "send_modal", "defer"):
        setattr(inter.response, name, AsyncMock())
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock()
    return inter


def _fake_hub():
    hub = MagicMock()
    hub.guild = MagicMock()
    hub.guild.id = G
    hub.guild.get_member = lambda uid: None
    hub.owner_id = OWNER
    hub.bot = MagicMock()
    hub.after_change = AsyncMock()
    return hub


def _buttons(view):
    return {x.label: x for x in view.children if isinstance(x, discord.ui.Button)}


# ── Pure helpers ─────────────────────────────────────────────────────────────


def test_merge_order_keeps_the_order_leadership_set():
    assert h.merge_order((A, B, d.OPEN), [C, B, A]) == (A, B, C)
    assert h.merge_order((A, B), [B]) == (B,)
    assert h.merge_order((), [C, A]) == (C, A)


@pytest.mark.parametrize(
    ("raw", "n"), [("3", 3), (" 1 ", 1), ("0", None), ("x", None), ("400", None)]
)
def test_parse_interval(raw, n):
    assert h.parse_interval(raw) == n


# ── The hub's controls by state ──────────────────────────────────────────────


def _hub(state, duties_list):
    guild = MagicMock()
    guild.id = G
    view = h.DutiesHubView(MagicMock(), guild, OWNER, state=state)
    with patch("duties_db.list_duties", return_value=duties_list):
        view.render()
    return view


def test_premium_hub_is_all_live():
    view = _hub(r.STATE_PREMIUM, [d.Duty(id=1, guild_id=G, name="X")])
    buttons = _buttons(view)
    assert all(not b.disabled for b in buttons.values())
    assert c.BTN_ADD in buttons and buttons[c.BTN_ADD].style == discord.ButtonStyle.primary


def test_empty_premium_hub_offers_only_what_can_change_something():
    buttons = _buttons(_hub(r.STATE_PREMIUM, []))
    live = {label for label, b in buttons.items() if not b.disabled}
    assert live == {c.BTN_ADD, c.BTN_CONTACT}


def test_a_lapsed_hub_is_read_only_but_can_still_be_viewed():
    buttons = _buttons(_hub(r.STATE_LAPSED, [d.Duty(id=1, guild_id=G, name="X")]))
    live = {label for label, b in buttons.items() if not b.disabled}
    assert live == {c.BTN_WORKLOAD, c.BTN_MY_DUTIES}
    # 💎 in front of the control's own glyph, not instead of it.
    assert f"💎 {c.BTN_ADD}" in buttons


def test_the_hub_pages_a_long_list():
    many = [d.Duty(id=i, guild_id=G, name=f"D{i} " + "x" * 400) for i in range(1, 30)]
    view = _hub(r.STATE_PREMIUM, many)
    assert "Page 1 / " in " ".join(_buttons(view))


# ── Duty editor ──────────────────────────────────────────────────────────────


def _editor(**kw):
    kw.setdefault("name", "Disputes")
    draft = d.Duty(id=kw.pop("id", 0), guild_id=G, **kw)
    return h.DutyEditorView(_fake_hub(), draft)


def _selects(view):
    return [x for x in view.children if isinstance(x, (discord.ui.Select, discord.ui.UserSelect))]


async def test_picking_holders_keeps_open_positions_and_skips_bots():
    view = _editor(primaries=(A, d.OPEN))
    primary = _selects(view)[0]
    primary._values = [
        MagicMock(id=B, bot=False),
        MagicMock(id=999, bot=True),
        MagicMock(id=A, bot=False),
    ]
    await primary.callback(_inter())
    assert view.draft.primaries == (A, B, d.OPEN)


async def test_setting_open_positions():
    view = _editor(primaries=(A,))
    open_primary = _selects(view)[2]
    open_primary._values = ["2"]
    await open_primary.callback(_inter())
    assert view.draft.primaries == (A, d.OPEN, d.OPEN)


async def test_anyone_in_leadership_disables_the_backup_pickers():
    view = _editor(backups=(B,))
    await _buttons(view)[c.BTN_BACKUP_ANYONE].callback(_inter())
    assert view.draft.backup_anyone_leadership
    selects = _selects(view)
    assert selects[1].disabled and selects[3].disabled
    assert selects[1].placeholder == c.EDITOR_PH_BACKUP_ANYONE
    assert c.BTN_BACKUP_NAMED in _buttons(view)


async def test_save_adds_the_duty_and_refreshes(temp_db):
    view = _editor(primaries=(A,), contact_enabled=True)
    inter = _inter()
    await _buttons(view)[c.BTN_SAVE].callback(inter)
    inter.response.edit_message.assert_awaited_once_with(
        content=c.ADDED.format(name="Disputes"), embed=None, view=None
    )
    view.hub.after_change.assert_awaited_once()
    (saved,) = db.list_duties(G)
    assert saved.primaries == (A,) and saved.contact_enabled


async def test_save_refuses_a_duplicate_name_and_keeps_the_draft(temp_db):
    db.save_duty(d.Duty(id=0, guild_id=G, name="disputes"))
    view = _editor()
    inter = _inter()
    await _buttons(view)[c.BTN_SAVE].callback(inter)
    inter.response.send_message.assert_awaited_once_with(
        c.DUPLICATE_NAME.format(name="Disputes"), ephemeral=True
    )
    assert not view.is_finished()


async def test_save_refuses_a_26th_contact_duty(temp_db):
    for i in range(r.CONTACT_CAP):
        db.save_duty(d.Duty(id=0, guild_id=G, name=f"D{i}", contact_enabled=True))
    view = _editor(contact_enabled=True)
    inter = _inter()
    await _buttons(view)[c.BTN_SAVE].callback(inter)
    inter.response.send_message.assert_awaited_once_with(
        c.CONTACT_FULL.format(cap=r.CONTACT_CAP), ephemeral=True
    )


async def test_saving_clears_a_departure_notice(temp_db):
    duty_id = db.save_duty(d.Duty(id=0, guild_id=G, name="Disputes", primaries=(A,)))
    db.vacate_holder(G, A, "Alpha")
    view = h.DutyEditorView(_fake_hub(), db.get_duty(G, duty_id))
    with patch("duties_health.sync_departures") as sync:
        await _buttons(view)[c.BTN_SAVE].callback(_inter())
    sync.assert_called_once_with(G)
    assert db.departures(G) == []


async def test_cancel_changes_nothing(temp_db):
    view = _editor()
    inter = _inter()
    await _buttons(view)[c.BTN_CANCEL].callback(inter)
    assert db.list_duties(G) == []
    assert inter.response.edit_message.await_args.kwargs["content"].startswith("↩️")


# ── Reminder editor ──────────────────────────────────────────────────────────


def _reminder_editor(duty_kw=None, **draft_kw):
    duty = d.Duty(id=1, guild_id=G, name="Daily Schedule", **(duty_kw or {}))
    parent = MagicMock()
    parent.hub = _fake_hub()
    parent.owner_id = OWNER
    parent.duty = duty
    parent.back = AsyncMock()
    draft_kw.setdefault("message", "")
    draft_kw.setdefault("schedule_type", d.SCHEDULE_WEEKDAYS)
    draft_kw.setdefault("at", None)
    draft = d.DutyReminder(id=0, guild_id=G, duty_id=1, **draft_kw)
    with patch("config.get_config", return_value=None):
        view = h.ReminderEditorView(parent, draft)
        view.render()
    return view


def test_a_blank_reminder_lists_what_it_needs():
    view = _reminder_editor(send_to=d.SEND_CHANNEL)
    assert view.problems() == [c.NEEDS_MESSAGE, c.NEEDS_TIME, c.NEEDS_DAYS, c.NEEDS_CHANNEL]
    interval = _reminder_editor(schedule_type=d.SCHEDULE_INTERVAL, message="x", at=time(9))
    assert interval.problems() == [c.NEEDS_INTERVAL]


def test_the_channel_picker_shows_only_when_it_is_used():
    def has_channel_select(view):
        return any(isinstance(x, discord.ui.ChannelSelect) for x in view.children)

    assert not has_channel_select(_reminder_editor(send_to=d.SEND_PRIMARIES))
    assert has_channel_select(_reminder_editor(send_to=d.SEND_CHANNEL))
    assert has_channel_select(
        _reminder_editor({"backup_anyone_leadership": True}, send_to=d.SEND_BACKUPS)
    )


async def test_save_returns_to_the_list(temp_db):
    view = _reminder_editor(message="hi", at=time(9), weekdays=frozenset({0}))
    inter = _inter()
    await _buttons(view)[c.BTN_SAVE].callback(inter)
    view.parent.back.assert_awaited_once_with(inter, c.REMINDER_SAVED.format(name="Daily Schedule"))
    assert len(db.list_reminders(G)) == 1


async def test_save_with_problems_says_what_and_saves_nothing(temp_db):
    view = _reminder_editor()
    inter = _inter()
    await _buttons(view)[c.BTN_SAVE].callback(inter)
    assert c.NEEDS_MESSAGE in inter.response.send_message.await_args.args[0]
    assert db.list_reminders(G) == []


async def test_the_modal_keeps_what_parsed_and_reports_the_rest():
    view = _reminder_editor(schedule_type=d.SCHEDULE_INTERVAL)
    with patch("config.get_config", return_value=None):
        modal = h.ReminderTextModal(view)
    modal.message_input._value = "{primary}, go"
    modal.time_input._value = "9pm"
    modal.every_input._value = "soon"
    modal.start_input._value = "2026-09-01"
    inter = _inter()
    with patch("config.get_config", return_value=None):
        await modal.on_submit(inter)
    assert view.draft.message == "{primary}, go"
    assert view.draft.at == time(21, 0)
    assert view.draft.anchor_date == date(2026, 9, 1)
    assert inter.followup.send.await_args.args[0] == c.EVERY_UNREADABLE.format(raw="soon")


async def test_a_weekday_reminder_modal_asks_only_for_message_and_time():
    view = _reminder_editor()
    modal = h.ReminderTextModal(view)
    assert len(modal.children) == 2


# ── Contact settings ─────────────────────────────────────────────────────────


def _role(mention, *, manage=False, admin=False, managed=False, default=False, view=True):
    role = MagicMock()
    role.mention = mention
    role.managed = managed
    role.is_default = MagicMock(return_value=default)
    role.perms = MagicMock(manage_threads=manage, administrator=admin, view_channel=view)
    return role


def test_contact_check_names_who_can_read_every_thread():
    member_role = _role("@Members")
    officers = _role("@Officers", manage=True)
    admins = _role("@Admins", manage=True, admin=True)
    bot_role = _role("@Bot", manage=True, managed=True)
    guild = MagicMock()
    guild.roles = [member_role, officers, admins, bot_role]
    guild.me = MagicMock()
    guild.get_role = lambda rid: member_role if rid == 5 else None
    channel = MagicMock(spec=discord.TextChannel)
    channel.overwrites = {}

    def perms_for(who):
        if who is guild.me:
            return MagicMock(
                view_channel=True, create_private_threads=True, send_messages_in_threads=True
            )
        return who.perms

    channel.permissions_for = MagicMock(side_effect=perms_for)
    cfg = MagicMock(member_role_id=5, member_role_name="Members")

    check = h.contact_check(guild, channel, cfg)

    assert check == r.ContactCheck(
        bot_can_thread=True, members_can_view=True, manage_threads=("@Officers",)
    )


# ── Entry point ──────────────────────────────────────────────────────────────


async def test_members_cannot_open_the_hub(seeded_db):
    inter = _inter()
    inter.guild_id = G
    with (
        patch("storm_permissions.is_leader_or_admin", return_value=False),
        patch("storm_permissions.deny_non_leader", AsyncMock()) as deny,
    ):
        await h.open_hub(MagicMock(), inter)
    deny.assert_awaited_once()


async def test_leadership_opens_the_hub(seeded_db):
    inter = _inter()
    inter.guild_id = G
    inter.guild = MagicMock()
    inter.guild.id = G
    inter.guild.chunked = False
    inter.original_response = AsyncMock(return_value=MagicMock())
    with (
        patch("storm_permissions.is_leader_or_admin", return_value=True),
        patch("premium.feature_gate", AsyncMock(return_value=True)),
    ):
        await h.open_hub(MagicMock(), inter)
    kwargs = inter.response.send_message.await_args.kwargs
    assert kwargs["ephemeral"] is True
    assert kwargs["embed"].title == c.HUB_TITLE
    assert isinstance(kwargs["view"], h.DutiesHubView)


def test_reconcile_opens_positions_of_people_already_gone(temp_db):
    duty_id = db.save_duty(d.Duty(id=0, guild_id=G, name="X", primaries=(A, B)))
    guild = MagicMock()
    guild.id = G
    guild.chunked = True
    guild.get_member = lambda uid: MagicMock() if uid == A else None
    bot = MagicMock()
    bot.get_user = lambda uid: MagicMock(display_name="Bravo")
    assert h.reconcile_departed(bot, guild)
    assert db.get_duty(G, duty_id).primaries == (A, d.OPEN)
    assert db.departures(G)[0].name == "Bravo"


def test_reconcile_never_guesses_on_a_partial_member_list(temp_db):
    duty_id = db.save_duty(d.Duty(id=0, guild_id=G, name="X", primaries=(A,)))
    guild = MagicMock()
    guild.id = G
    guild.chunked = False
    guild.get_member = lambda uid: None
    assert not h.reconcile_departed(MagicMock(), guild)
    assert db.get_duty(G, duty_id).primaries == (A,)

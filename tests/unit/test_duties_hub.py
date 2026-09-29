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
    inter.original_response = AsyncMock(return_value=MagicMock())
    return inter


def _fake_hub():
    hub = MagicMock()
    hub.guild = MagicMock()
    hub.guild.id = G
    hub.guild.get_member = lambda uid: None
    hub.guild.chunked = False  # no departure reconcile unless a test asks
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
    assert live == {c.BTN_ADD, c.BTN_CONTACT, c.BTN_CATEGORIES}


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
    assert view.draft.primaries == (A, d.OPEN, B)  # the open position keeps its place


async def test_setting_open_positions():
    view = _editor(primaries=(A,))
    open_primary = _selects(view)[2]
    open_primary._values = ["2"]
    await open_primary.callback(_inter())
    assert view.draft.primaries == (A, d.OPEN, d.OPEN)


def _leaders(*pairs):
    role = MagicMock()
    role.members = [MagicMock(id=uid, display_name=name, bot=False) for uid, name in pairs]
    return role


def _editor_with_leaders(role, **kw):
    kw.setdefault("name", "Disputes")
    hub = _fake_hub()
    # Charlie is in the server but not in the leadership role.
    hub.guild.get_member = lambda uid: MagicMock(display_name="Charlie") if uid == C else None
    with (
        patch("duties_hub.leadership_role", return_value=role),
        patch("config.get_config", return_value=None),
    ):
        return h.DutyEditorView(hub, d.Duty(id=0, guild_id=G, **kw))


async def test_holder_pickers_offer_leaders_and_anyone_in_leadership():
    role = _leaders((B, "Bravo"), (A, "Alpha"))
    view = _editor_with_leaders(role, primaries=(B,), backups=(d.ANYONE,))
    primary, backup = _selects(view)[:2]
    for select in (primary, backup):
        assert [o.label for o in select.options] == ["Alpha", "Bravo", c.EDITOR_ANYONE_OPTION]
    assert [o.default for o in primary.options] == [False, True, False]
    assert [o.default for o in backup.options] == [False, False, True]

    primary._values = [str(A), str(d.ANYONE)]
    with (
        patch("duties_hub.leadership_role", return_value=role),
        patch("config.get_config", return_value=None),
    ):
        await primary.callback(_inter())
    assert view.draft.primaries == (A, d.ANYONE)


def test_a_holder_outside_leadership_stays_offered():
    view = _editor_with_leaders(_leaders((A, "Alpha")), primaries=(C,))
    assert [o.value for o in _selects(view)[0].options] == [str(A), str(C), str(d.ANYONE)]


def test_without_a_leadership_role_the_user_picker_is_used():
    view = _editor_with_leaders(None, primaries=(A,))
    assert isinstance(_selects(view)[0], discord.ui.UserSelect)


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
    with (
        patch("config.get_config", return_value=None),
        patch("wizard_steps.ChannelSelectStep._collect_pickable_threads", return_value=[])
        if draft_kw.get("send_to") != d.SEND_THREAD
        else _nullctx(),
    ):
        view = h.ReminderEditorView(parent, draft)
        view.render()
    return view


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_blank_reminder_lists_what_it_needs():
    view = _reminder_editor(send_to=d.SEND_CHANNEL)
    assert view.problems() == [c.NEEDS_MESSAGE, c.NEEDS_TIME, c.NEEDS_DAYS, c.NEEDS_CHANNEL]
    for kind in d.NEEDS_START:
        dated = _reminder_editor(schedule_type=kind, message="x", at=time(9))
        assert dated.problems() == [c.NEEDS_START], kind
    weekly = _reminder_editor(schedule_type=d.SCHEDULE_WEEKLY, message="x", at=time(9))
    assert weekly.problems() == [c.NEEDS_WEEKDAY]
    daily = _reminder_editor(schedule_type=d.SCHEDULE_DAILY, message="x", at=time(9))
    assert daily.problems() == []


def test_the_schedule_picker_offers_the_seven_choices_in_order():
    view = _reminder_editor(schedule_type=d.SCHEDULE_DAILY)
    schedule = next(x for x in view.children if getattr(x, "placeholder", None) == c.SCHEDULE_PH)
    assert [o.label for o in schedule.options] == [
        "Daily",
        "Every other day",
        "Every 3 days",
        "Selected days only",
        "Weekly",
        "Every 2 weeks",
        "Every month",
    ]


def test_the_day_pickers_match_the_schedule():
    def pickers(view):
        return {getattr(x, "placeholder", None): x for x in view.children}

    many = pickers(_reminder_editor(schedule_type=d.SCHEDULE_WEEKDAYS))
    assert many[c.WEEKDAYS_PH].max_values == 7
    one = pickers(_reminder_editor(schedule_type=d.SCHEDULE_WEEKLY))
    assert one[c.WEEKLY_PH].max_values == 1
    none = pickers(_reminder_editor(schedule_type=d.SCHEDULE_MONTHLY))
    assert c.WEEKDAYS_PH not in none and c.WEEKLY_PH not in none


def test_the_channel_picker_shows_only_when_it_is_used():
    def has_channel_select(view):
        return any(isinstance(x, discord.ui.ChannelSelect) for x in view.children)

    assert not has_channel_select(_reminder_editor(send_to=d.SEND_PRIMARIES))
    assert has_channel_select(_reminder_editor(send_to=d.SEND_CHANNEL))
    assert has_channel_select(_reminder_editor({"backups": (d.ANYONE,)}, send_to=d.SEND_BACKUPS))
    assert not has_channel_select(
        _reminder_editor({"backups": (d.ANYONE,)}, send_to=d.SEND_PRIMARIES)
    )


def test_a_thread_reminder_offers_open_threads():
    thread = MagicMock()
    thread.id, thread.name = 77, "weekly-plans"
    thread.parent.name = "leadership"
    with patch("wizard_steps.ChannelSelectStep._collect_pickable_threads", return_value=[thread]):
        view = _reminder_editor(send_to=d.SEND_THREAD)
    (picker,) = [x for x in view.children if getattr(x, "placeholder", None) == c.THREAD_PH]
    assert [o.label for o in picker.options] == ["weekly-plans (in #leadership)"]
    assert view.problems()[-1] == c.NEEDS_THREAD


def test_no_open_threads_says_so():
    with patch("wizard_steps.ChannelSelectStep._collect_pickable_threads", return_value=[]):
        view = _reminder_editor(send_to=d.SEND_THREAD)
    (picker,) = [x for x in view.children if getattr(x, "placeholder", None) == c.THREAD_NONE_OPEN]
    assert picker.disabled


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
    view = _reminder_editor(schedule_type=d.SCHEDULE_EVERY_2_WEEKS)
    with patch("config.get_config", return_value=None):
        modal = h.ReminderTextModal(view)
    modal.message_input._value = "{primary}, go"
    modal.time_input._value = "9ish"
    modal.start_input._value = "2026-09-01"
    inter = _inter()
    with patch("config.get_config", return_value=None):
        await modal.on_submit(inter)
    assert view.draft.message == "{primary}, go"
    assert view.draft.at is None
    assert view.draft.anchor_date == date(2026, 9, 1)
    assert inter.followup.send.await_args.args[0] == c.TIME_UNREADABLE.format(raw="9ish")


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
    inter.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    kwargs = inter.followup.send.await_args.kwargs
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


# ── Review fixes ─────────────────────────────────────────────────────────────


def test_open_positions_keep_their_place_through_an_edit():
    assert h.place_holders((A, d.OPEN, B), (A, B, C), 1) == (A, d.OPEN, B, C)
    assert h.place_holders((A, d.OPEN, B), (B,), 1) == (d.OPEN, B)
    assert h.place_holders((A,), (A,), 2) == (A, d.OPEN, d.OPEN)
    assert h.place_holders((d.OPEN, d.OPEN, A), (A,), 1) == (d.OPEN, A)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9/1", date(2026, 9, 1)),  # recent past: this year
        ("12/1", date(2026, 12, 1)),  # coming up: this year, not last
        ("1/5", date(2027, 1, 5)),  # early next year is nearer than last January
        ("2025-12-01", date(2025, 12, 1)),  # an explicit year as typed
        ("nonsense", None),
    ],
)
def test_start_date_takes_the_nearest_year(raw, expected):
    assert h.parse_start_date(raw, date(2026, 9, 29)) == expected


async def test_a_new_reminder_for_a_time_already_passed_waits_for_tomorrow(seeded_db):
    duty_id = db.save_duty(d.Duty(id=0, guild_id=G, name="X", primaries=(A,)))
    rid = db.save_reminder(
        d.DutyReminder(
            id=0,
            guild_id=G,
            duty_id=duty_id,
            message="m",
            schedule_type=d.SCHEDULE_WEEKDAYS,
            at=time(0, 0),
            weekdays=frozenset(range(7)),
        )
    )
    h.skip_today_if_passed(G, rid)
    (saved,) = db.list_reminders(G)
    assert saved.last_fired_on is not None  # today's 00:00 counts as done


async def test_the_reminder_list_stops_when_it_hands_over_and_back_is_fresh(temp_db):
    duty_id = db.save_duty(d.Duty(id=0, guild_id=G, name="X"))
    hub = _fake_hub()
    view = h.ReminderListView(hub, db.get_duty(G, duty_id))
    with patch("config.get_config", return_value=None):
        view.render()
        inter = _inter()
        await _buttons(view)[c.BTN_REMINDER_ADD].callback(inter)
    assert view.is_finished()
    editor = inter.response.edit_message.await_args.kwargs["view"]
    assert isinstance(editor, h.ReminderEditorView)
    assert editor.message is inter.original_response.return_value

    back = _inter()
    with patch("config.get_config", return_value=None):
        await view.back(back, None)
    fresh = back.response.edit_message.await_args.kwargs["view"]
    assert isinstance(fresh, h.ReminderListView) and fresh is not view
    assert not fresh.is_finished()


async def test_saving_a_new_buttons_channel_moves_the_post(temp_db):
    db.save_settings(db.DutiesSettings(guild_id=G, panel_channel_id=600, panel_message_id=55))
    hub = _fake_hub()
    hub.guild.get_channel = lambda cid: None
    view = h.ContactSettingsView(hub)
    view.panel_channel_id = 700
    view.render()
    inter = _inter()
    inter.edit_original_response = AsyncMock()
    with patch("duties_panel.post_panel", AsyncMock(return_value=MagicMock())) as post:
        await _buttons(view)[c.BTN_SAVE].callback(inter)
    post.assert_awaited_once_with(hub.bot, G, 700)
    # The stored channel is still the old one when post_panel reads it, so it
    # can delete the old post there.
    assert db.get_settings(G).panel_channel_id == 600


async def test_saving_without_a_post_just_saves(temp_db):
    hub = _fake_hub()
    hub.guild.get_channel = lambda cid: None
    view = h.ContactSettingsView(hub)
    view.panel_channel_id = 700
    view.render()
    with patch("duties_panel.post_panel", AsyncMock()) as post:
        await _buttons(view)[c.BTN_SAVE].callback(_inter())
    post.assert_not_awaited()
    assert db.get_settings(G).panel_channel_id == 700


# ── Categories ───────────────────────────────────────────────────────────────


def test_the_duty_modal_offers_the_categories():
    cats = [db.Category(1, G, "Members"), db.Category(2, G, "VS")]
    duty = d.Duty(id=5, guild_id=G, name="X", category_id=2)
    modal = h.DutyDetailsModal(c.MODAL_EDIT_TITLE, duty, cats, AsyncMock())
    label = modal.category_label
    assert label.text == c.FIELD_CATEGORY
    assert [(o.label, o.default) for o in label.component.options] == [
        ("Members", False),
        ("VS", True),
    ]
    label.component._values = ["1"]
    assert modal.picked_category().name == "Members"
    label.component._values = []
    assert modal.picked_category() is None


def test_with_no_categories_the_modal_points_to_them():
    modal = h.DutyDetailsModal(c.MODAL_ADD_TITLE, None, [], AsyncMock())
    assert modal.category_label is None
    texts = [x.content for x in modal.children if isinstance(x, discord.ui.TextDisplay)]
    assert texts == [c.DUTY_NO_CATEGORIES]


async def test_the_category_manager_adds_and_refuses_duplicates(temp_db):
    hub = _fake_hub()
    hub.refresh = AsyncMock()
    view = h.CategoryManagerView(hub)
    view.render()
    inter = _inter()
    await _buttons(view)[c.BTN_CATEGORY_ADD].callback(inter)
    modal = inter.response.send_modal.await_args.args[0]
    modal.name_input._value = "Members"
    done = _inter()
    await modal.on_submit(done)
    assert done.response.edit_message.await_args.kwargs["content"] == c.CATEGORY_ADDED.format(
        name="Members"
    )
    assert [x.name for x in db.list_categories(G)] == ["Members"]

    again = _inter()
    await modal.on_submit(again)
    again.response.send_message.assert_awaited_once_with(
        c.CATEGORY_DUPLICATE.format(name="Members"), ephemeral=True
    )


def test_category_delete_confirm_says_where_duties_go():
    assert r.category_delete_confirm("VS", 2) == (
        f"🗑️ **VS** will be deleted. Its 2 duties move to **{c.HUB_NO_CATEGORY}**. "
        "This can't be undone."
    )
    assert r.category_delete_confirm("VS", 0) == "🗑️ **VS** will be deleted. This can't be undone."

"""Leadership Duties (#687): the `/duties` hub and everything opened from it.

Leadership only (an admin or the Leadership role, as every officer surface
accepts). Every screen is ephemeral and inherits `OwnedView`, so only the
officer who opened it can press anything and a timeout strips the buttons
and names the route back.

**Premium.** The whole feature is Premium. A server that has never had it
sees the hub's shape with every control marked 💎 and disabled (UX.md,
Principle 5). A server whose Premium ended sees its full list, read-only,
with the same controls disabled: nothing is deleted, and resubscribing
picks up where it left off. The viewing controls (workload, my duties) stay
live, since they change nothing.

**Screens reuse their message.** A picker becomes the editor it picked
for, the reminder list becomes the reminder editor and back, so an officer
works down one message rather than a stack of them. Every screen that
changes something refreshes the hub behind it and the posted contact
buttons.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, time

import discord

import config
import duties as d
import duties_copy as c
import duties_db
import duties_health
import duties_panel
import duties_render as r
from messages import CANCEL_BACKPEDAL_DEFAULT
from wizard_registry import OwnedView

logger = logging.getLogger(__name__)

PREMIUM_FEATURE = duties_panel.PREMIUM_FEATURE

#: People per slot. The design says one to three is typical; five leaves
#: room without letting one duty swallow the whole team.
MAX_HOLDERS = 5
MAX_OPEN = 3
NAME_MAX = 60
CATEGORY_MAX = 40
DESCRIPTION_MAX = 300
MESSAGE_MAX = 1500
_PER_PICKER_PAGE = 25


# ── Who is who ───────────────────────────────────────────────────────────────


def _role(guild: discord.Guild, role_id: int, role_name: str) -> discord.Role | None:
    role = guild.get_role(role_id) if role_id else None
    if role is None and role_name:
        role = discord.utils.get(guild.roles, name=role_name)
    return role


def leadership_role(guild: discord.Guild, cfg) -> discord.Role | None:
    if cfg is None:
        return None
    return _role(guild, cfg.leadership_role_id, cfg.leadership_role_name)


def member_role(guild: discord.Guild, cfg) -> discord.Role | None:
    if cfg is None:
        return None
    return _role(guild, cfg.member_role_id, cfg.member_role_name)


def display_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member is not None else str(user_id)


def reconcile_departed(bot, guild: discord.Guild) -> bool:
    """Open the positions of holders who left while the bot wasn't
    listening (`on_member_remove` never fires across a restart).

    Only on a fully chunked guild: a partial member cache would make a
    present leader look gone, and opening their positions on a guess would be
    far worse than a late notice. Returns True if anything changed.
    """
    if not getattr(guild, "chunked", False):
        return False
    changed = False
    seen: set[int] = set()
    for duty in duties_db.list_duties(guild.id):
        for uid in duty.primaries + duty.backups:
            if uid == d.OPEN or uid in seen:
                continue
            seen.add(uid)
            if guild.get_member(uid) is not None:
                continue
            user = bot.get_user(uid)
            name = getattr(user, "display_name", "") or getattr(user, "name", "") or ""
            if duties_db.vacate_holder(guild.id, uid, name):
                changed = True
    if changed:
        duties_health.note_departures(guild.id)
    return changed


async def premium_state(bot, guild_id: int, has_duties: bool) -> str:
    import premium

    if await premium.feature_gate(PREMIUM_FEATURE, guild_id, bot=bot):
        return r.STATE_PREMIUM
    return r.STATE_LAPSED if has_duties else r.STATE_FREE


# ── Small shared views ───────────────────────────────────────────────────────


async def hand_over(view: OwnedView, inter: discord.Interaction) -> None:
    """Give `view` the message `inter` just edited it onto.

    Fetched under `inter`'s own token rather than borrowed from the view
    that had the message before: that token is fresh, so the 15-minute edit
    window `ExpiringView` guards starts now, and the message comes back with
    its current text for the timeout notice to append to. The same shape as
    `alliance_duel_entry`'s builder hand-off.
    """
    try:
        view.message = await inter.original_response()
    except discord.HTTPException:
        pass  # the notice can't land; the view still works until it times out


class _ConfirmView(OwnedView):
    """Yes / Cancel for the one irreversible thing on a screen."""

    timeout_hint = c.HUB_ROUTE

    def __init__(self, owner_id: int, yes_label: str, on_yes, on_no):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.add_button(yes_label, discord.ButtonStyle.danger, on_yes)
        self.add_button(c.BTN_CANCEL, discord.ButtonStyle.secondary, on_no)


class DutyPickerView(OwnedView):
    """ "Which duty?", paged past 25 (DESIGN.md, Selects)."""

    timeout_hint = c.HUB_ROUTE

    def __init__(self, owner_id: int, duties: list[d.Duty], on_pick, *, page: int = 0):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.duties = duties
        self.on_pick = on_pick
        self.page = page
        self._build()

    def _build(self) -> None:
        self.clear_items()
        page_count = max(1, -(-len(self.duties) // _PER_PICKER_PAGE))
        self.page = max(0, min(self.page, page_count - 1))
        chunk = self.duties[self.page * _PER_PICKER_PAGE : (self.page + 1) * _PER_PICKER_PAGE]
        options = []
        for duty in chunk:
            note = duty.category
            if duty.paused:
                note = f"{note}{c.PAUSED_MARK}".strip()
            options.append(
                discord.SelectOption(
                    label=duty.name[:100], value=str(duty.id), description=note[:100] or None
                )
            )
        select = discord.ui.Select(placeholder=c.PICK_PLACEHOLDER, options=options, row=0)

        async def _picked(inter: discord.Interaction):
            self.stop()
            await self.on_pick(inter, int(select.values[0]), self)

        select.callback = _picked
        self.add_item(select)
        self.add_pagination_row(page=self.page, page_count=page_count, on_page=self._turn, row=1)

    async def _turn(self, inter: discord.Interaction, page: int) -> None:
        self.page = page
        self._build()
        await inter.response.edit_message(view=self)


class _PagedEmbedView(OwnedView):
    """A read-only embed with the standard paging row, for the workload
    view."""

    timeout_hint = c.HUB_ROUTE

    def __init__(self, owner_id: int, render, page_count: int):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.render = render
        self.page_count = page_count
        self._build(0)

    def _build(self, page: int) -> None:
        self.clear_items()
        self.add_pagination_row(page=page, page_count=self.page_count, on_page=self._turn)

    async def _turn(self, inter: discord.Interaction, page: int) -> None:
        embed, _ = self.render(page)
        self._build(page)
        await inter.response.edit_message(embed=embed, view=self)


# ── The hub ──────────────────────────────────────────────────────────────────


class DutiesHubView(OwnedView):
    timeout_hint = c.HUB_ROUTE

    def __init__(self, bot, guild: discord.Guild, owner_id: int, *, state: str):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild = guild
        self.owner_id = owner_id
        self.state = state
        self.page = 0
        self.duties: list[d.Duty] = []

    @property
    def live(self) -> bool:
        return self.state == r.STATE_PREMIUM

    def render(self) -> discord.Embed:
        """Reload the list, rebuild the controls, and return the embed."""
        self.duties = duties_db.list_duties(self.guild.id)
        embed, page_count = r.hub_embed(self.duties, r.mention, state=self.state, page=self.page)
        self.page = max(0, min(self.page, page_count - 1))
        self._build(page_count)
        return embed

    def _build(self, page_count: int) -> None:
        self.clear_items()
        live, has = self.live, bool(self.duties)

        def label(text: str) -> str:
            # 💎 goes in front of the control's own glyph on the tier that
            # doesn't have it, never in place of it (DESIGN.md, Premium).
            return text if live else f"💎 {text}"

        sec = discord.ButtonStyle.secondary
        self.add_button(
            label(c.BTN_ADD), discord.ButtonStyle.primary, self._add, row=0, disabled=not live
        )
        self.add_button(label(c.BTN_EDIT), sec, self._edit, row=0, disabled=not (live and has))
        self.add_button(label(c.BTN_PAUSE), sec, self._pause, row=0, disabled=not (live and has))
        self.add_button(label(c.BTN_DELETE), sec, self._delete, row=0, disabled=not (live and has))
        self.add_button(c.BTN_WORKLOAD, sec, self._workload, row=1, disabled=not has)
        self.add_button(c.BTN_MY_DUTIES, sec, self._my_duties, row=1, disabled=not has)
        self.add_button(
            label(c.BTN_REMINDERS), sec, self._reminders, row=1, disabled=not (live and has)
        )
        self.add_button(label(c.BTN_CONTACT), sec, self._contact, row=1, disabled=not live)
        self.add_pagination_row(page=self.page, page_count=page_count, on_page=self._turn, row=2)

    async def _turn(self, inter: discord.Interaction, page: int) -> None:
        self.page = page
        await inter.response.edit_message(embed=self.render(), view=self)

    async def refresh(self) -> None:
        """Redraw the hub after a change made on another screen."""
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.render(), view=self)
        except discord.HTTPException:
            pass  # the hub's token ran out; the next /duties shows the change

    async def after_change(self) -> None:
        await duties_panel.refresh_panel(self.bot, self.guild.id)
        await self.refresh()

    async def _pick(self, inter: discord.Interaction, on_pick) -> None:
        duties = duties_db.list_duties(self.guild.id)
        if not duties:
            await inter.response.send_message(c.PICK_NONE, ephemeral=True)
            return
        view = DutyPickerView(self.owner_id, duties, on_pick)
        await inter.response.send_message(c.PICK_PROMPT, view=view, ephemeral=True)
        view.message = await inter.original_response()

    # ── Add and edit ─────────────────────────────────────────────────────────

    async def _add(self, inter: discord.Interaction) -> None:
        async def _start(i: discord.Interaction, name: str, category: str, description: str):
            draft = d.Duty(
                id=0, guild_id=self.guild.id, name=name, category=category, description=description
            )
            editor = DutyEditorView(self, draft)
            await i.response.send_message(
                embed=r.editor_embed(draft, r.mention), view=editor, ephemeral=True
            )
            editor.message = await i.original_response()

        await inter.response.send_modal(DutyDetailsModal(c.MODAL_ADD_TITLE, None, _start))

    async def _edit(self, inter: discord.Interaction) -> None:
        async def _picked(i: discord.Interaction, duty_id: int, picker: DutyPickerView):
            duty = duties_db.get_duty(self.guild.id, duty_id)
            if duty is None:
                await i.response.edit_message(content=c.PICK_GONE, view=None)
                return
            editor = DutyEditorView(self, duty)
            await i.response.edit_message(
                content=None, embed=r.editor_embed(duty, r.mention), view=editor
            )
            await hand_over(editor, i)

        await self._pick(inter, _picked)

    # ── Pause, delete ────────────────────────────────────────────────────────

    async def _pause(self, inter: discord.Interaction) -> None:
        async def _picked(i: discord.Interaction, duty_id: int, picker: DutyPickerView):
            duty = duties_db.get_duty(self.guild.id, duty_id)
            if duty is None:
                await i.response.edit_message(content=c.PICK_GONE, view=None)
                return
            duties_db.set_duty_paused(self.guild.id, duty_id, not duty.paused)
            if duty.paused:  # resuming: today's passed times don't all fire at once
                for reminder in duties_db.list_reminders(self.guild.id, duty_id):
                    skip_today_if_passed(self.guild.id, reminder.id)
            ack = (c.RESUMED if duty.paused else c.PAUSED).format(name=duty.name)
            await i.response.edit_message(content=ack, view=None)
            await self.after_change()

        await self._pick(inter, _picked)

    async def _delete(self, inter: discord.Interaction) -> None:
        async def _picked(i: discord.Interaction, duty_id: int, picker: DutyPickerView):
            duty = duties_db.get_duty(self.guild.id, duty_id)
            if duty is None:
                await i.response.edit_message(content=c.PICK_GONE, view=None)
                return
            n = len(duties_db.list_reminders(self.guild.id, duty_id))
            reminders = (
                ""
                if n == 0
                else c.DELETE_CONFIRM_REMINDERS[0]
                if n == 1
                else c.DELETE_CONFIRM_REMINDERS[1].format(n=n)
            )

            async def _yes(i2: discord.Interaction):
                duties_db.delete_duty(self.guild.id, duty_id)
                duties_health.sync_departures(self.guild.id)
                await i2.response.edit_message(content=c.DELETED.format(name=duty.name), view=None)
                confirm.stop()
                await self.after_change()

            async def _no(i2: discord.Interaction):
                await i2.response.edit_message(content=CANCEL_BACKPEDAL_DEFAULT, view=None)
                confirm.stop()

            confirm = _ConfirmView(self.owner_id, c.BTN_DELETE_YES, _yes, _no)
            await i.response.edit_message(
                content=c.DELETE_CONFIRM.format(name=duty.name, reminders=reminders), view=confirm
            )
            await hand_over(confirm, i)

        await self._pick(inter, _picked)

    # ── Views onto the list ──────────────────────────────────────────────────

    async def _workload(self, inter: discord.Interaction) -> None:
        cfg = config.get_config(self.guild.id)
        role = leadership_role(self.guild, cfg)
        leader_ids = [m.id for m in role.members] if role is not None else []
        duties = duties_db.list_duties(self.guild.id)
        loads = d.workload(duties, leader_ids)
        slots = d.open_slots(duties)

        def _render(page: int):
            return r.workload_embed(
                loads,
                slots,
                r.mention,
                sort_key=lambda uid: display_name(self.guild, uid),
                role_known=role is not None,
                page=page,
            )

        embed, page_count = _render(0)
        if page_count > 1:
            view = _PagedEmbedView(self.owner_id, _render, page_count)
            await inter.response.send_message(embed=embed, view=view, ephemeral=True)
            view.message = await inter.original_response()
        else:
            await inter.response.send_message(embed=embed, ephemeral=True)

    async def _my_duties(self, inter: discord.Interaction) -> None:
        primary, backup = d.held_by(duties_db.list_duties(self.guild.id), inter.user.id)
        await inter.response.send_message(embed=r.my_duties_embed(primary, backup), ephemeral=True)

    # ── Reminders, contact ───────────────────────────────────────────────────

    async def _reminders(self, inter: discord.Interaction) -> None:
        async def _picked(i: discord.Interaction, duty_id: int, picker: DutyPickerView):
            duty = duties_db.get_duty(self.guild.id, duty_id)
            if duty is None:
                await i.response.edit_message(content=c.PICK_GONE, view=None)
                return
            view = ReminderListView(self, duty)
            await i.response.edit_message(content=None, embed=view.render(), view=view)
            await hand_over(view, i)

        await self._pick(inter, _picked)

    async def _contact(self, inter: discord.Interaction) -> None:
        view = ContactSettingsView(self)
        await inter.response.send_message(embed=view.render(), view=view, ephemeral=True)
        view.message = await inter.original_response()


# ── Duty editor ──────────────────────────────────────────────────────────────


class DutyDetailsModal(discord.ui.Modal):
    """Name, category and description: the three things a duty is called
    and what it covers. Used to start a new duty and to edit one."""

    def __init__(self, title: str, duty: d.Duty | None, on_submit_cb):
        super().__init__(title=title)
        self._cb = on_submit_cb
        self.name_input = discord.ui.TextInput(
            label=c.FIELD_NAME,
            placeholder=c.FIELD_NAME_PLACEHOLDER,
            default=duty.name if duty else None,
            max_length=NAME_MAX,
            required=True,
        )
        self.category_input = discord.ui.TextInput(
            label=c.FIELD_CATEGORY,
            placeholder=c.FIELD_CATEGORY_PLACEHOLDER,
            default=duty.category if duty else None,
            max_length=CATEGORY_MAX,
            required=False,
        )
        self.description_input = discord.ui.TextInput(
            label=c.FIELD_DESCRIPTION,
            placeholder=c.FIELD_DESCRIPTION_PLACEHOLDER,
            default=duty.description if duty else None,
            style=discord.TextStyle.paragraph,
            max_length=DESCRIPTION_MAX,
            required=False,
        )
        for item in (self.name_input, self.category_input, self.description_input):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = (self.name_input.value or "").strip()
        if not name:
            await interaction.response.send_message(c.NAME_REQUIRED, ephemeral=True)
            return
        await self._cb(
            interaction,
            name,
            (self.category_input.value or "").strip(),
            (self.description_input.value or "").strip(),
        )


def merge_order(old: tuple[int, ...], picked: list[int]) -> tuple[int, ...]:
    """Holders in the order leadership first set them: people kept from
    before stay where they were, newcomers go after. Discord's user picker
    doesn't promise an order, so it can't be trusted to keep one."""
    kept = [uid for uid in old if uid in picked and uid != d.OPEN]
    return tuple(kept + [uid for uid in picked if uid not in kept])


def place_holders(old: tuple[int, ...], named: tuple[int, ...], open_n: int) -> tuple[int, ...]:
    """The new slot list, keeping every position where it was.

    People still named and open positions still wanted stay in place;
    people removed drop out; newcomers and any extra open positions go at
    the end. A holder leaving opens their slot where it was (`duties.vacate`),
    and an unrelated edit must not then shuffle it to the bottom.
    """
    out: list[int] = []
    opens_left = open_n
    for uid in old:
        if uid == d.OPEN:
            if opens_left:
                out.append(d.OPEN)
                opens_left -= 1
        elif uid in named:
            out.append(uid)
    out += [uid for uid in named if uid not in out]
    out += [d.OPEN] * opens_left
    return tuple(out)


class DutyEditorView(OwnedView):
    """One duty, held as a draft until 💾 Save."""

    timeout_hint = c.HUB_ROUTE

    def __init__(self, hub: DutiesHubView, draft: d.Duty):
        super().__init__(timeout=600)
        self.hub = hub
        self.owner_id = hub.owner_id
        self.draft = draft
        self._build()

    def _named(self, kind: str) -> tuple[int, ...]:
        ids = self.draft.primaries if kind == d.SLOT_PRIMARY else self.draft.backups
        return tuple(uid for uid in ids if uid != d.OPEN)

    def _open(self, kind: str) -> int:
        ids = self.draft.primaries if kind == d.SLOT_PRIMARY else self.draft.backups
        return sum(1 for uid in ids if uid == d.OPEN)

    def _set(self, kind: str, named: tuple[int, ...], open_n: int) -> None:
        old = self.draft.primaries if kind == d.SLOT_PRIMARY else self.draft.backups
        field = "primaries" if kind == d.SLOT_PRIMARY else "backups"
        self.draft = replace(self.draft, **{field: place_holders(old, named, open_n)})

    def _build(self) -> None:
        self.clear_items()
        anyone = self.draft.backup_anyone_leadership
        for row, kind, placeholder in (
            (0, d.SLOT_PRIMARY, c.EDITOR_PH_PRIMARY),
            (1, d.SLOT_BACKUP, c.EDITOR_PH_BACKUP_ANYONE if anyone else c.EDITOR_PH_BACKUP),
        ):
            select = discord.ui.UserSelect(
                placeholder=placeholder,
                min_values=0,
                max_values=MAX_HOLDERS,
                default_values=[discord.Object(id=uid) for uid in self._named(kind)],
                disabled=kind == d.SLOT_BACKUP and anyone,
                row=row,
            )
            select.callback = self._holders_cb(kind, select)
            self.add_item(select)
        for row, kind, placeholder, word in (
            (2, d.SLOT_PRIMARY, c.EDITOR_PH_OPEN_PRIMARY, c.KIND_PRIMARY[0]),
            (3, d.SLOT_BACKUP, c.EDITOR_PH_OPEN_BACKUP, c.KIND_BACKUP[0]),
        ):
            current = self._open(kind)
            options = [
                discord.SelectOption(
                    label=r.open_option_label(n, word), value=str(n), default=n == current
                )
                for n in range(max(MAX_OPEN, current) + 1)
            ]
            select = discord.ui.Select(
                placeholder=placeholder,
                options=options,
                disabled=kind == d.SLOT_BACKUP and anyone,
                row=row,
            )
            select.callback = self._open_cb(kind, select)
            self.add_item(select)

        sec = discord.ButtonStyle.secondary
        self.add_button(c.BTN_NAME_AND_DESCRIPTION, sec, self._details, row=4)
        self.add_button(
            c.BTN_BACKUP_NAMED if anyone else c.BTN_BACKUP_ANYONE, sec, self._toggle_anyone, row=4
        )
        self.add_button(
            c.BTN_CONTACT_OFF if self.draft.contact_enabled else c.BTN_CONTACT_ON,
            sec,
            self._toggle_contact,
            row=4,
        )
        self.add_button(c.BTN_SAVE, discord.ButtonStyle.primary, self._save, row=4)
        self.add_button(c.BTN_CANCEL, sec, self._cancel, row=4)

    async def _redraw(self, inter: discord.Interaction) -> None:
        self._build()
        await inter.response.edit_message(embed=r.editor_embed(self.draft, r.mention), view=self)

    def _holders_cb(self, kind: str, select: discord.ui.UserSelect):
        async def _cb(inter: discord.Interaction):
            picked = [u.id for u in select.values if not getattr(u, "bot", False)]
            self._set(kind, merge_order(self._named(kind), picked), self._open(kind))
            await self._redraw(inter)

        return _cb

    def _open_cb(self, kind: str, select: discord.ui.Select):
        async def _cb(inter: discord.Interaction):
            self._set(kind, self._named(kind), int(select.values[0]))
            await self._redraw(inter)

        return _cb

    async def _details(self, inter: discord.Interaction) -> None:
        async def _apply(i: discord.Interaction, name: str, category: str, description: str):
            self.draft = replace(self.draft, name=name, category=category, description=description)
            await self._redraw(i)

        await inter.response.send_modal(DutyDetailsModal(c.MODAL_EDIT_TITLE, self.draft, _apply))

    async def _toggle_anyone(self, inter: discord.Interaction) -> None:
        self.draft = replace(
            self.draft, backup_anyone_leadership=not self.draft.backup_anyone_leadership
        )
        await self._redraw(inter)

    async def _toggle_contact(self, inter: discord.Interaction) -> None:
        self.draft = replace(self.draft, contact_enabled=not self.draft.contact_enabled)
        await self._redraw(inter)

    async def _save(self, inter: discord.Interaction) -> None:
        guild_id = self.hub.guild.id
        if self.draft.contact_enabled:
            others = [
                x
                for x in duties_db.list_duties(guild_id)
                if x.contact_enabled and x.id != self.draft.id
            ]
            if len(others) >= r.CONTACT_CAP:
                await inter.response.send_message(
                    c.CONTACT_FULL.format(cap=r.CONTACT_CAP), ephemeral=True
                )
                return
        is_new = not self.draft.id
        try:
            duty_id = duties_db.save_duty(self.draft)
        except duties_db.DuplicateDutyName:
            await inter.response.send_message(
                c.DUPLICATE_NAME.format(name=self.draft.name), ephemeral=True
            )
            return
        self.draft = replace(self.draft, id=duty_id)
        duties_health.sync_departures(guild_id)
        # The draft was loaded when the editor opened. Anyone it names who
        # has left since is opened again, with the notice, rather than saved
        # back as a holder.
        reconcile_departed(self.hub.bot, self.hub.guild)
        ack = (c.ADDED if is_new else c.SAVED).format(name=self.draft.name)
        self.stop()
        await inter.response.edit_message(content=ack, embed=None, view=None)
        await self.hub.after_change()

    async def _cancel(self, inter: discord.Interaction) -> None:
        self.stop()
        await inter.response.edit_message(content=CANCEL_BACKPEDAL_DEFAULT, embed=None, view=None)


# ── Reminders ────────────────────────────────────────────────────────────────


def skip_today_if_passed(guild_id: int, reminder_id: int) -> None:
    """Count today's occurrence as done when its time has already passed.

    The loop fires at or past the set time so a late tick or a restart
    doesn't lose a reminder. The same rule would otherwise send one the
    moment it is created, switched on, retimed earlier, or its duty resumed,
    in the afternoon, for a morning time. Setting a reminder up is not the
    reminder being missed.
    """
    from datetime import datetime

    from duties_reminders import guild_tz

    reminder = next((x for x in duties_db.list_reminders(guild_id) if x.id == reminder_id), None)
    if reminder is None:
        return
    now_local = datetime.now(guild_tz(config.get_config(guild_id)))
    occurrence = d.due_occurrence(reminder, now_local)
    if occurrence is not None:
        duties_db.mark_reminder_fired(guild_id, reminder_id, occurrence)


def _guild_tz_name(guild_id: int) -> str | None:
    cfg = config.get_config(guild_id)
    return cfg.timezone if cfg and cfg.timezone else None


class ReminderListView(OwnedView):
    """One duty's reminders: pick one, then act on it, or add another."""

    timeout_hint = c.HUB_ROUTE

    def __init__(self, hub: DutiesHubView, duty: d.Duty):
        super().__init__(timeout=600)
        self.hub = hub
        self.owner_id = hub.owner_id
        self.duty = duty
        self.selected: int | None = None
        self.reminders: list[d.DutyReminder] = []

    def render(self) -> discord.Embed:
        guild_id = self.hub.guild.id
        self.duty = duties_db.get_duty(guild_id, self.duty.id) or self.duty
        self.reminders = duties_db.list_reminders(guild_id, self.duty.id)
        ids = [x.id for x in self.reminders]
        if self.selected not in ids:
            self.selected = ids[0] if len(ids) == 1 else None
        self._build()
        return r.reminder_list_embed(self.duty, self.reminders, _guild_tz_name(guild_id))

    def _current(self) -> d.DutyReminder | None:
        return next((x for x in self.reminders if x.id == self.selected), None)

    def _build(self) -> None:
        self.clear_items()
        tz = _guild_tz_name(self.hub.guild.id)
        if self.reminders:
            options = [
                discord.SelectOption(
                    # A select can't render a channel mention, so the option
                    # says the kind of destination; the list above names it.
                    label=f"{n}. {r.reminder_when(x, tz)}"[:100],
                    description=r.send_label(x.send_to)[:100],
                    value=str(x.id),
                    default=x.id == self.selected,
                )
                for n, x in enumerate(self.reminders[:25], start=1)
            ]
            select = discord.ui.Select(
                placeholder=c.REMINDER_PICK_PLACEHOLDER, options=options, row=0
            )

            async def _pick(inter: discord.Interaction):
                self.selected = int(select.values[0])
                await inter.response.edit_message(embed=self.render(), view=self)

            select.callback = _pick
            self.add_item(select)
        current = self._current()
        sec = discord.ButtonStyle.secondary
        self.add_button(c.BTN_REMINDER_ADD, discord.ButtonStyle.primary, self._add, row=1)
        self.add_button(c.BTN_REMINDER_EDIT, sec, self._edit, row=1, disabled=current is None)
        self.add_button(
            c.BTN_REMINDER_OFF if current is None or current.enabled else c.BTN_REMINDER_ON,
            sec,
            self._toggle,
            row=1,
            disabled=current is None,
        )
        self.add_button(c.BTN_REMINDER_DELETE, sec, self._delete, row=1, disabled=current is None)

    async def back(self, inter: discord.Interaction, ack: str | None) -> None:
        """Return to the list, from the editor, a confirm, or a toggle.

        Always a fresh list view: this one stopped when it handed its message
        on (a stopped view is never listened to again), and a view left
        running would time out and strip whatever screen now holds the
        message.
        """
        self.stop()
        fresh = ReminderListView(self.hub, self.duty)
        fresh.selected = self.selected
        await inter.response.edit_message(content=ack, embed=fresh.render(), view=fresh)
        await hand_over(fresh, inter)

    def _open_editor(self, draft: d.DutyReminder) -> "ReminderEditorView":
        return ReminderEditorView(self, draft)

    async def _add(self, inter: discord.Interaction) -> None:
        draft = d.DutyReminder(
            id=0,
            guild_id=self.hub.guild.id,
            duty_id=self.duty.id,
            message="",
            schedule_type=d.SCHEDULE_WEEKDAYS,
            at=None,
        )
        editor = self._open_editor(draft)
        self.stop()
        self.stop()
        await inter.response.edit_message(content=None, embed=editor.render(), view=editor)
        await hand_over(editor, inter)  # stopped

    async def _edit(self, inter: discord.Interaction) -> None:
        current = self._current()
        if current is None:
            return
        editor = self._open_editor(current)
        await inter.response.edit_message(content=None, embed=editor.render(), view=editor)
        await hand_over(editor, inter)

    async def _toggle(self, inter: discord.Interaction) -> None:
        current = self._current()
        if current is None:
            return
        duties_db.save_reminder(replace(current, enabled=not current.enabled))
        if not current.enabled:
            skip_today_if_passed(self.hub.guild.id, current.id)
        ack = c.REMINDER_TURNED_OFF if current.enabled else c.REMINDER_TURNED_ON
        await self.back(inter, ack)

    async def _delete(self, inter: discord.Interaction) -> None:
        current = self._current()
        if current is None:
            return

        async def _yes(i: discord.Interaction):
            duties_db.delete_reminder(self.hub.guild.id, current.id)
            self.selected = None
            confirm.stop()
            await self.back(i, c.REMINDER_DELETED)

        async def _no(i: discord.Interaction):
            confirm.stop()
            await self.back(i, CANCEL_BACKPEDAL_DEFAULT)

        confirm = _ConfirmView(self.owner_id, c.BTN_DELETE_YES, _yes, _no)
        self.stop()
        tz = _guild_tz_name(self.hub.guild.id)
        await inter.response.edit_message(
            content=c.REMINDER_DELETE_CONFIRM.format(when=r.reminder_when(current, tz)),
            embed=None,
            view=confirm,
        )
        await hand_over(confirm, inter)


_SEND_ORDER = (d.SEND_PRIMARIES, d.SEND_BACKUPS, d.SEND_BOTH, d.SEND_CHANNEL)


class ReminderEditorView(OwnedView):
    """One reminder, held as a draft until 💾 Save.

    Selects carry the choices (when, which days, where it goes, which
    channel); the one modal carries what has to be typed (the message, the
    time, and for an every-few-days reminder how often and from when).
    """

    timeout_hint = c.HUB_ROUTE

    def __init__(self, parent: ReminderListView, draft: d.DutyReminder):
        super().__init__(timeout=600)
        self.parent = parent
        self.owner_id = parent.owner_id
        self.draft = draft
        guild_id = parent.hub.guild.id
        self.tz_name = _guild_tz_name(guild_id)
        cfg = config.get_config(guild_id)
        self.leadership_channel_id = (cfg.leadership_channel_id if cfg else 0) or 0

    @property
    def duty(self) -> d.Duty:
        return self.parent.duty

    def today(self) -> date:
        """Today in the server's own calendar, which the schedule runs on."""
        from datetime import datetime

        from duties_reminders import guild_tz

        return datetime.now(guild_tz(config.get_config(self.parent.hub.guild.id))).date()

    def _wants_channel(self) -> bool:
        if self.draft.send_to == d.SEND_CHANNEL:
            return True
        return self.duty.backup_anyone_leadership and self.draft.send_to in (
            d.SEND_BACKUPS,
            d.SEND_BOTH,
        )

    def render(self) -> discord.Embed:
        self._build()
        guild = self.parent.hub.guild
        return r.reminder_editor_embed(
            self.duty,
            self.draft,
            self.tz_name,
            lambda uid: display_name(guild, uid),
            leadership_channel_id=self.leadership_channel_id,
        )

    def _build(self) -> None:
        self.clear_items()
        weekdays_mode = self.draft.schedule_type == d.SCHEDULE_WEEKDAYS

        schedule = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label=c.SCHEDULE_WEEKDAYS, value=d.SCHEDULE_WEEKDAYS, default=weekdays_mode
                ),
                discord.SelectOption(
                    label=c.SCHEDULE_INTERVAL, value=d.SCHEDULE_INTERVAL, default=not weekdays_mode
                ),
            ],
            row=0,
        )

        async def _schedule(inter: discord.Interaction):
            self.draft = replace(self.draft, schedule_type=schedule.values[0])
            await self._redraw(inter)

        schedule.callback = _schedule
        self.add_item(schedule)

        if weekdays_mode:
            days = discord.ui.Select(
                placeholder=c.WEEKDAYS_PH,
                min_values=0,
                max_values=7,
                options=[
                    discord.SelectOption(label=name, value=str(i), default=i in self.draft.weekdays)
                    for i, name in enumerate(c.WEEKDAY_NAMES)
                ],
                row=1,
            )

            async def _days(inter: discord.Interaction):
                self.draft = replace(self.draft, weekdays=frozenset(int(v) for v in days.values))
                await self._redraw(inter)

            days.callback = _days
            self.add_item(days)

        send = discord.ui.Select(
            options=[
                discord.SelectOption(
                    label=r.send_label(key), value=key, default=key == self.draft.send_to
                )
                for key in _SEND_ORDER
            ],
            row=2,
        )

        async def _send(inter: discord.Interaction):
            self.draft = replace(self.draft, send_to=send.values[0])
            await self._redraw(inter)

        send.callback = _send
        self.add_item(send)

        if self._wants_channel():
            channel = discord.ui.ChannelSelect(
                placeholder=c.CHANNEL_PH,
                channel_types=[discord.ChannelType.text, discord.ChannelType.news],
                default_values=(
                    [discord.Object(id=self.draft.channel_id)] if self.draft.channel_id else []
                ),
                min_values=0,
                max_values=1,
                row=3,
            )

            async def _channel(inter: discord.Interaction):
                picked = channel.values[0].id if channel.values else 0
                self.draft = replace(self.draft, channel_id=picked)
                await self._redraw(inter)

            channel.callback = _channel
            self.add_item(channel)

        sec = discord.ButtonStyle.secondary
        self.add_button(c.BTN_MESSAGE_AND_TIME, sec, self._text, row=4)
        if self._wants_channel():
            self.add_button(
                c.BTN_PING_OFF if self.draft.ping_holders else c.BTN_PING_ON,
                sec,
                self._toggle_ping,
                row=4,
            )
        self.add_button(c.BTN_SAVE, discord.ButtonStyle.primary, self._save, row=4)
        self.add_button(c.BTN_CANCEL, sec, self._cancel, row=4)

    async def _redraw(self, inter: discord.Interaction) -> None:
        await inter.response.edit_message(embed=self.render(), view=self)

    async def _toggle_ping(self, inter: discord.Interaction) -> None:
        self.draft = replace(self.draft, ping_holders=not self.draft.ping_holders)
        await self._redraw(inter)

    async def _text(self, inter: discord.Interaction) -> None:
        await inter.response.send_modal(ReminderTextModal(self))

    def problems(self) -> list[str]:
        """What stops this draft being saved, in the order an officer would
        fix them."""
        out = []
        if not self.draft.message.strip():
            out.append(c.NEEDS_MESSAGE)
        if self.draft.at is None:
            out.append(c.NEEDS_TIME)
        if self.draft.schedule_type == d.SCHEDULE_WEEKDAYS and not self.draft.weekdays:
            out.append(c.NEEDS_DAYS)
        if self.draft.schedule_type == d.SCHEDULE_INTERVAL and (
            self.draft.anchor_date is None or self.draft.interval_days < 1
        ):
            out.append(c.NEEDS_INTERVAL)
        if self.draft.send_to == d.SEND_CHANNEL and not self.draft.channel_id:
            out.append(c.NEEDS_CHANNEL)
        return out

    async def _save(self, inter: discord.Interaction) -> None:
        problems = self.problems()
        if problems:
            await inter.response.send_message("\n".join(problems), ephemeral=True)
            return
        rid = duties_db.save_reminder(self.draft)
        skip_today_if_passed(self.draft.guild_id, rid)
        self.stop()
        await self.parent.back(inter, c.REMINDER_SAVED.format(name=self.duty.name))

    async def _cancel(self, inter: discord.Interaction) -> None:
        self.stop()
        await self.parent.back(inter, CANCEL_BACKPEDAL_DEFAULT)


def parse_start_date(raw: str, today: date) -> date | None:
    """An every-few-days reminder's start date, in whichever year puts it
    nearest `today`.

    A start can be in the past (lining up with a cycle already running) or
    the future (starting next month), so neither of the bot's two date
    readers fits: `parse_event_date` always looks ahead and
    `wizard_time._parse_month_day` looks back past 31 days. An explicit
    four-digit year is taken as typed.
    """
    import re

    from storm_date_helpers import parse_event_date

    if not raw or not raw.strip():
        return None
    parsed = parse_event_date(raw, today=today)
    if parsed is None:
        return None
    if re.search(r"\d{4}", raw):
        return parsed
    candidates = []
    for year in (today.year - 1, today.year, today.year + 1):
        try:
            candidates.append(parsed.replace(year=year))
        except ValueError:
            continue  # 29 Feb outside a leap year
    return min(candidates, key=lambda x: abs((x - today).days)) if candidates else parsed


def parse_interval(raw: str) -> int | None:
    try:
        n = int(str(raw).strip())
    except ValueError:
        return None
    return n if 1 <= n <= 365 else None


class ReminderTextModal(discord.ui.Modal):
    def __init__(self, editor: ReminderEditorView):
        super().__init__(title=c.MODAL_REMINDER_TITLE)
        self.editor = editor
        draft = editor.draft
        from wizard_time import _format_24h_to_12h

        tz_label = editor.tz_name or "your server's timezone"
        self.message_input = discord.ui.TextInput(
            label=c.FIELD_MESSAGE,
            placeholder=c.FIELD_MESSAGE_PLACEHOLDER,
            default=draft.message or None,
            style=discord.TextStyle.paragraph,
            max_length=MESSAGE_MAX,
            required=True,
        )
        self.time_input = discord.ui.TextInput(
            label=c.FIELD_TIME.format(tz=tz_label)[:45],
            placeholder=c.FIELD_TIME_PLACEHOLDER,
            default=_format_24h_to_12h(draft.at.strftime("%H:%M")) if draft.at else None,
            max_length=10,
            required=True,
        )
        self.add_item(self.message_input)
        self.add_item(self.time_input)
        self.every_input = self.start_input = None
        if draft.schedule_type == d.SCHEDULE_INTERVAL:
            self.every_input = discord.ui.TextInput(
                label=c.FIELD_EVERY,
                placeholder=c.FIELD_EVERY_PLACEHOLDER,
                default=str(draft.interval_days) if draft.anchor_date else None,
                max_length=3,
                required=True,
            )
            self.start_input = discord.ui.TextInput(
                label=c.FIELD_START,
                placeholder=c.FIELD_START_PLACEHOLDER,
                default=draft.anchor_date.isoformat() if draft.anchor_date else None,
                max_length=20,
                required=True,
            )
            self.add_item(self.every_input)
            self.add_item(self.start_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from config import parse_storm_signup_time

        editor = self.editor
        errors: list[str] = []
        changes: dict = {"message": (self.message_input.value or "").strip()}

        raw_time = (self.time_input.value or "").strip()
        hhmm = parse_storm_signup_time(raw_time)
        if hhmm is None:
            errors.append(c.TIME_UNREADABLE.format(raw=raw_time))
        else:
            hh, mm = (int(p) for p in hhmm.split(":"))
            changes["at"] = time(hh, mm)

        if self.every_input is not None:
            raw_every = (self.every_input.value or "").strip()
            every = parse_interval(raw_every)
            if every is None:
                errors.append(c.EVERY_UNREADABLE.format(raw=raw_every))
            else:
                changes["interval_days"] = every
            raw_start = (self.start_input.value or "").strip()
            start = parse_start_date(raw_start, editor.today())
            if start is None:
                errors.append(c.START_UNREADABLE.format(raw=raw_start))
            else:
                changes["anchor_date"] = start

        # What did parse is kept, so a typo in one field costs only that
        # field (UX.md, Wizards).
        editor.draft = replace(editor.draft, **changes)
        await interaction.response.edit_message(embed=editor.render(), view=editor)
        if errors:
            await interaction.followup.send("\n".join(errors), ephemeral=True)


# ── Contact settings ─────────────────────────────────────────────────────────


def contact_check(guild: discord.Guild, channel, cfg) -> r.ContactCheck:
    """What the contact settings screen should warn about for the channel
    threads open under. Read from cache: an officer is waiting, and every
    one of these is a permission lookup, not a request."""
    if not isinstance(channel, discord.TextChannel):
        return r.ContactCheck()
    mine = channel.permissions_for(guild.me)
    bot_ok = bool(
        mine.view_channel and mine.create_private_threads and mine.send_messages_in_threads
    )
    role = member_role(guild, cfg)
    members_ok = None if role is None else bool(channel.permissions_for(role).view_channel)

    who: list[str] = []
    for guild_role in guild.roles:
        if guild_role.managed:
            continue  # bots, including this one
        perms = channel.permissions_for(guild_role)
        # Server admins can read everything anyway; naming them on every
        # server would bury the one role worth noticing.
        if perms.manage_threads and not perms.administrator:
            who.append("@everyone" if guild_role.is_default() else guild_role.mention)
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Member) and overwrite.manage_threads:
            if not target.guild_permissions.administrator and not target.bot:
                who.append(target.mention)
    return r.ContactCheck(
        bot_can_thread=bot_ok, members_can_view=members_ok, manage_threads=tuple(who)
    )


class ContactSettingsView(OwnedView):
    """Where the contact buttons go, where threads open, and posting them."""

    timeout_hint = c.HUB_ROUTE

    def __init__(self, hub: DutiesHubView):
        super().__init__(timeout=600)
        self.hub = hub
        self.owner_id = hub.owner_id
        settings = duties_db.get_settings(hub.guild.id)
        self.panel_channel_id = settings.panel_channel_id
        self.ticket_channel_id = settings.ticket_channel_id

    def render(self) -> discord.Embed:
        guild = self.hub.guild
        settings = duties_db.get_settings(guild.id)
        effective = self.ticket_channel_id or self.panel_channel_id
        warnings: list[str] = []
        if effective:
            channel = guild.get_channel(effective)
            warnings = r.contact_warnings(
                contact_check(guild, channel, config.get_config(guild.id)), f"<#{effective}>"
            )
        contact_count = sum(1 for x in duties_db.list_duties(guild.id) if x.contact_enabled)
        self._build(posted=bool(settings.panel_message_id))
        return r.contact_settings_embed(
            panel_channel_id=self.panel_channel_id,
            ticket_channel_id=self.ticket_channel_id,
            posted=bool(settings.panel_message_id),
            contact_count=contact_count,
            warnings=warnings,
        )

    def _build(self, *, posted: bool) -> None:
        self.clear_items()
        for row, placeholder, current, attr in (
            (0, c.CONTACT_PH_BUTTONS, self.panel_channel_id, "panel_channel_id"),
            (1, c.CONTACT_PH_THREADS, self.ticket_channel_id, "ticket_channel_id"),
        ):
            select = discord.ui.ChannelSelect(
                placeholder=placeholder,
                channel_types=[discord.ChannelType.text],
                default_values=[discord.Object(id=current)] if current else [],
                min_values=0,
                max_values=1,
                row=row,
            )
            select.callback = self._channel_cb(select, attr)
            self.add_item(select)
        sec = discord.ButtonStyle.secondary
        self.add_button(
            c.BTN_THREADS_SAME, sec, self._same, row=2, disabled=not self.ticket_channel_id
        )
        self.add_button(c.BTN_SAVE, discord.ButtonStyle.primary, self._save, row=2)
        self.add_button(
            c.BTN_POST_AGAIN if posted else c.BTN_POST_BUTTONS,
            sec,
            self._post,
            row=2,
            disabled=not self.panel_channel_id,
        )

    def _channel_cb(self, select: discord.ui.ChannelSelect, attr: str):
        async def _cb(inter: discord.Interaction):
            setattr(self, attr, select.values[0].id if select.values else 0)
            await inter.response.edit_message(embed=self.render(), view=self)

        return _cb

    async def _same(self, inter: discord.Interaction) -> None:
        self.ticket_channel_id = 0
        await inter.response.edit_message(embed=self.render(), view=self)

    def _persist(self, *, panel_channel: bool) -> None:
        """Save the threads channel, and the buttons channel only when told.

        The stored buttons channel is where the posted message lives, and
        `duties_panel` finds the message by it. Changing it without moving
        the message would leave the old post behind, still clickable and
        never updated again, so only `post_panel` changes it once a post
        exists.
        """
        guild_id = self.hub.guild.id
        settings = duties_db.get_settings(guild_id)
        ticket = self.ticket_channel_id
        if ticket == self.panel_channel_id:
            ticket = 0  # "the same channel" is stored as unset, so it follows a move
        duties_db.save_settings(
            duties_db.DutiesSettings(
                guild_id=guild_id,
                panel_channel_id=self.panel_channel_id
                if panel_channel
                else settings.panel_channel_id,
                panel_message_id=settings.panel_message_id,
                ticket_channel_id=ticket,
            )
        )

    async def _save(self, inter: discord.Interaction) -> None:
        settings = duties_db.get_settings(self.hub.guild.id)
        moved = (
            bool(settings.panel_message_id) and self.panel_channel_id != settings.panel_channel_id
        )
        if moved and self.panel_channel_id:
            # The buttons are posted and their channel changed: saving moves
            # the post, which is what "Buttons posted in" now promises.
            await self._post(inter)
            return
        self._persist(panel_channel=True)
        await inter.response.edit_message(content=c.CONTACT_SAVED, embed=self.render(), view=self)

    async def _post(self, inter: discord.Interaction) -> None:
        if not self.panel_channel_id:
            await inter.response.send_message(c.CONTACT_PICK_CHANNEL_FIRST, ephemeral=True)
            return
        # `post_panel` reads the old channel and message before replacing
        # them, deletes the old post, and saves the new channel with it.
        self._persist(panel_channel=False)
        await inter.response.defer()
        channel = f"<#{self.panel_channel_id}>"
        message = await duties_panel.post_panel(
            self.hub.bot, self.hub.guild.id, self.panel_channel_id
        )
        content = (
            c.CONTACT_POSTED_ACK.format(channel=channel)
            if message is not None
            else c.CONTACT_POST_FAILED.format(channel=channel)
        )
        try:
            await inter.edit_original_response(content=content, embed=self.render(), view=self)
        except discord.HTTPException:
            pass


# ── Entry point ──────────────────────────────────────────────────────────────


async def open_hub(bot, interaction: discord.Interaction) -> None:
    """`/duties`. Leadership only."""
    from messages import NOT_SET_UP
    from storm_permissions import deny_non_leader, is_leader_or_admin

    cfg = config.get_config(interaction.guild_id)
    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
        return
    if not is_leader_or_admin(interaction):
        await deny_non_leader(interaction)
        return
    # Before anything that can wait on the network: the Premium lookup can
    # miss its cache and the post refresh is a request. Discord allows three
    # seconds for the first response.
    await interaction.response.defer(ephemeral=True, thinking=True)
    guild = interaction.guild
    if reconcile_departed(bot, guild):
        await duties_panel.refresh_panel(bot, guild.id)
    has = bool(duties_db.list_duties(guild.id))
    state = await premium_state(bot, guild.id, has)
    view = DutiesHubView(bot, guild, interaction.user.id, state=state)
    view.message = await interaction.followup.send(
        embed=view.render(), view=view, ephemeral=True, wait=True
    )

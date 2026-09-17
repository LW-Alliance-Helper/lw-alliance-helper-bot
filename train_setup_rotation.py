"""
The train wizard's Conductor Rotation step (#55 / #302): the toggle, the
roster source (free tab + column, or the synced Member Roster on Premium),
the draft channel and time (reused from reminders, or asked), the weekly
draft day, public posts, role-scoped days (Premium, #337), counted
reasons, the three Sheet tabs and the weekly pattern that opens the preset
editor.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611)
alongside `train_setup.py`: `setup_cog` imports `run_rotation_step` back as
`_run_train_rotation_step` and the two views under their old names; the
shared wizard pieces come from `wizard_steps` and `wizard_time`; this module
reaches into `setup_cog` at call time (`_setup()`) only for the column
letter helpers.

Shape: a `_Step` for the shared handles, one `_ask_*` function per lettered
sub-step writing into a `_Rotation` result, one `_Abort` exit that the entry
point turns into the `None` the wizard expects. The inline modals and views
are module classes under their old names.

Every prompt, label and saved field is unchanged; the rotation half of
`tests/integration/test_train_setup.py` holds this module to the old step.
"""

import asyncio
from dataclasses import dataclass, field

import discord

import premium
import wizard_registry
import wizard_steps
from messages import CANCEL_PLAIN, PREV_CHANNEL_GONE, WIZARD_TIMEOUT
from setup_hub import HUB_BTN_TRAIN
from wizard_registry import wait_view_or_cancel
from wizard_steps import WIZARD_STEP_TIMEOUT
from wizard_time import _format_time_with_tz, _parse_12h_time


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


TIMEOUT_MSG = WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN)
_DEF_HIST, _DEF_MR, _DEF_DR = "Train History", "Train Member Rules", "Train Day Rules"


# ── Shared handles ───────────────────────────────────────────────────────────


@dataclass
class _Step:
    bot: discord.Client
    interaction: discord.Interaction
    channel: discord.abc.Messageable
    user: discord.abc.User
    guild_id: int
    cancel_event: asyncio.Event
    guild_tz: str
    is_premium: bool = False

    def check(self, m) -> bool:
        return m.author == self.user and m.channel == self.channel

    async def timed_out(self) -> None:
        await self.channel.send(TIMEOUT_MSG)

    async def wait(self, view) -> None:
        await wait_view_or_cancel(view, self.cancel_event)
        if getattr(view, "cancelled", False):
            raise _Abort

    async def ask(self, text: str, view, attr: str):
        """Post `text` with `view`, wait, and return `view.<attr>`; a timeout
        (the attribute still `None`) posts the route back and raises."""
        await self.channel.send(text, view=view)
        await self.wait(view)
        value = getattr(view, attr)
        if value is None:
            await self.timed_out()
            raise _Abort
        return value

    async def ask_yes_no(self, text: str) -> bool:
        return bool(await self.ask(text, wizard_steps.YesNoView(), "selected"))

    async def ask_channel(
        self,
        picker_prompt: str,
        *,
        suggested_name: str,
        current_id: int,
        step_text: str,
        gone_label: str | None = None,
    ) -> int:
        view = wizard_steps.ChannelSelectStep(
            picker_prompt,
            suggested_name=suggested_name,
            guild=self.interaction.guild,
            current_id=current_id,
        )
        if gone_label is not None and view.is_current_stale:
            await self.channel.send(PREV_CHANNEL_GONE.format(channel_label=gone_label))
        await self.channel.send(step_text, view=view)
        await self.wait(view)
        if not view.confirmed:
            await self.timed_out()
            raise _Abort
        return view.selected_channel.id

    async def keep_or_change(self, prompt: str, **kw) -> str:
        picked = await wizard_steps.ask_keep_or_change(
            self.channel, prompt, timeout_cmd="setup_train", cancel_event=self.cancel_event, **kw
        )
        if picked is None:
            raise _Abort
        return picked


@dataclass
class _Rotation:
    reminder_channel_id: int = 0
    reminder_time: str = "22:00"
    weekly_draft_day: int = 6
    rotation_public_channel_id: int = 0
    rule_type_roles: dict = field(default_factory=dict)
    counted_reasons: str = ""
    history_tab: str = _DEF_HIST
    member_rules_tab: str = _DEF_MR
    day_rules_tab: str = _DEF_DR
    active_preset: str = ""
    open_editor: bool = False


# ── Views ────────────────────────────────────────────────────────────────────


class _WeekdaySelectView(discord.ui.View):
    """Mon-Sun picker for the weekly-draft day, with a Keep-current button."""

    def __init__(self, *, current: int | None = None):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.selected: int | None = None
        self.cancelled = False
        import train_rotation as tr

        if current is not None and 0 <= int(current) <= 6:
            keep = discord.ui.Button(
                label=f"Keep current: {tr.WEEKDAY_NAMES[int(current)]}",
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep(inter: discord.Interaction):
                self.selected = int(current)
                for c in self.children:
                    c.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content=f"✅ Draft day: **{tr.WEEKDAY_NAMES[int(current)]}**", view=self
                )
                self.stop()

            keep.callback = _keep
            self.add_item(keep)

        select = discord.ui.Select(
            placeholder="Pick the weekly draft day…",
            options=[
                discord.SelectOption(label=tr.WEEKDAY_NAMES[wd], value=str(wd)) for wd in range(7)
            ],
            row=1 if current is not None else 0,
        )

        async def _cb(inter: discord.Interaction):
            self.selected = int(select.values[0])
            for c in self.children:
                c.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content=f"✅ Draft day: **{tr.WEEKDAY_NAMES[self.selected]}**", view=self
            )
            self.stop()

        select.callback = _cb
        self.add_item(select)


class _RuleRoleAttachView(discord.ui.View):
    """Roles opt-in (#302): pick a rule type + a Discord role, hit "Attach role
    to this rule", repeat for as many as you like, then Done. Replaces the
    looped one-at-a-time picker that overwhelmed leadership. Attachments
    accumulate in `rule_type_roles`; `wait_view_or_cancel` flips `.cancelled`."""

    def __init__(self, rule_type_roles: dict):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        import train_rotation as tr

        self.rule_type_roles = dict(rule_type_roles)
        self.cancelled = False
        self.done = False
        self.message = None
        self._labels = tr.RULE_LABELS
        self._pending_rule: str | None = None
        self._pending_role = None  # (id, name)

        rule_opts = [
            discord.SelectOption(label=tr.RULE_LABELS[rt], value=rt)
            for rt in (tr.RULE_LEADERSHIP, tr.RULE_VS, tr.RULE_CONTEST, tr.RULE_EVENT)
        ]
        self._rule_sel = discord.ui.Select(
            placeholder="1) Pick a rule type…", options=rule_opts, row=0
        )
        self._rule_sel.callback = self._on_rule
        self.add_item(self._rule_sel)

        self._role_sel = discord.ui.RoleSelect(
            placeholder="2) Pick a role…", min_values=1, max_values=1, row=1
        )
        self._role_sel.callback = self._on_role
        self.add_item(self._role_sel)

        attach = discord.ui.Button(
            label="Attach role to this rule", style=discord.ButtonStyle.primary, row=2
        )
        attach.callback = self._on_attach
        self.add_item(attach)

        done = discord.ui.Button(label="✅ Done", style=discord.ButtonStyle.success, row=2)
        done.callback = self._on_done
        self.add_item(done)

    def summary(self) -> str:
        if not self.rule_type_roles:
            return "_No roles attached yet._"
        return "\n".join(
            f"• {self._labels.get(k, k)} → <@&{v}>" for k, v in self.rule_type_roles.items()
        )

    async def _on_rule(self, interaction: discord.Interaction):
        self._pending_rule = self._rule_sel.values[0]
        await interaction.response.defer()

    async def _on_role(self, interaction: discord.Interaction):
        role = self._role_sel.values[0]
        self._pending_role = (role.id, role.name)
        await interaction.response.defer()

    async def _on_attach(self, interaction: discord.Interaction):
        if not self._pending_rule or not self._pending_role:
            await interaction.response.send_message(
                "Pick both a rule type and a role first, then hit Attach.", ephemeral=True
            )
            return
        self.rule_type_roles[self._pending_rule] = self._pending_role[0]
        label = self._labels.get(self._pending_rule, self._pending_rule)
        role_name = self._pending_role[1]
        self._pending_rule = None
        self._pending_role = None
        await interaction.response.edit_message(
            content=(
                f"✅ **{label}** days will pull from **@{role_name}**.\n\n"
                f"**Attached so far:**\n{self.summary()}\n\n"
                "Attach another rule, or hit **Done**."
            ),
            view=self,
        )

    async def _on_done(self, interaction: discord.Interaction):
        self.done = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


class _RosterModal(discord.ui.Modal):
    """Free tier: the roster tab and its name column."""

    def __init__(self, *, cur_tab: str, cur_col_letter: str):
        super().__init__(title="Roster Source")
        self.out = None
        self._tab = discord.ui.TextInput(
            label="Tab name (your member list)", default=cur_tab, max_length=100
        )
        self._col = discord.ui.TextInput(
            label="Name column (a letter, e.g. B)", default=cur_col_letter, max_length=3
        )
        self.add_item(self._tab)
        self.add_item(self._col)

    async def on_submit(self, inter: discord.Interaction):
        tab = self._tab.value.strip() or "Member Roster"
        idx = _setup()._col_letter_to_index(self._col.value.strip())
        if idx < 0:
            idx = 1  # default to column B on an unreadable letter
        self.out = (tab, idx)
        await inter.response.defer()
        self.stop()


class _RosterView(discord.ui.View):
    """Set the roster tab + column through the modal, or keep the current."""

    def __init__(self, *, cur_tab: str, cur_col_letter: str):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.choice = None
        self.modal_out = None
        self.cancelled = False
        self._current = (cur_tab, cur_col_letter)

    @discord.ui.button(label="⚙️ Set roster tab + column", style=discord.ButtonStyle.primary)
    async def _set(self, inter: discord.Interaction, _btn: discord.ui.Button):
        cur_tab, cur_col_letter = self._current
        modal = _RosterModal(cur_tab=cur_tab, cur_col_letter=cur_col_letter)
        await inter.response.send_modal(modal)
        await modal.wait()
        # Dismissing the modal leaves the saved source untouched.
        self.choice = "set" if modal.out else "keep"
        self.modal_out = modal.out
        for c in self.children:
            c.disabled = True
        try:
            await inter.edit_original_response(view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Keep current", style=discord.ButtonStyle.secondary)
    async def _keep(self, inter: discord.Interaction, _btn: discord.ui.Button):
        self.choice = "keep"
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()


class _TabsModal(discord.ui.Modal):
    """The three rotation tabs."""

    def __init__(self, *, history_tab: str, member_rules_tab: str, day_rules_tab: str):
        super().__init__(title="Rotation Sheet Tabs")
        self.out = None
        self._h = discord.ui.TextInput(label="History tab", default=history_tab, max_length=100)
        self._m = discord.ui.TextInput(
            label="Member Rules tab", default=member_rules_tab, max_length=100
        )
        self._d = discord.ui.TextInput(label="Day Rules tab", default=day_rules_tab, max_length=100)
        for i in (self._h, self._m, self._d):
            self.add_item(i)

    async def on_submit(self, inter: discord.Interaction):
        self.out = (
            self._h.value.strip() or _DEF_HIST,
            self._m.value.strip() or _DEF_MR,
            self._d.value.strip() or _DEF_DR,
        )
        await inter.response.defer()
        self.stop()


class _TabsChoiceView(discord.ui.View):
    """Keep current (when custom tabs are saved) / Use default / Define my own."""

    def __init__(
        self, *, saved_custom: bool, history_tab: str, member_rules_tab: str, day_rules_tab: str
    ):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.choice = None
        self.cancelled = False
        self.modal_out = None
        self._tabs = (history_tab, member_rules_tab, day_rules_tab)
        if saved_custom:
            keep = discord.ui.Button(label="Keep current", style=discord.ButtonStyle.success)
            keep.callback = self._keep
            self.add_item(keep)

        use_def = discord.ui.Button(
            label="↩️ Use default" if saved_custom else "✅ Use default",
            style=discord.ButtonStyle.secondary if saved_custom else discord.ButtonStyle.success,
        )
        use_def.callback = self._use_default
        self.add_item(use_def)

        custom = discord.ui.Button(label="✏️ Define my own", style=discord.ButtonStyle.primary)
        custom.callback = self._custom
        self.add_item(custom)

    async def _finish(self, inter: discord.Interaction, choice: str) -> None:
        self.choice = choice
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    async def _keep(self, inter: discord.Interaction) -> None:
        await self._finish(inter, "keep")

    async def _use_default(self, inter: discord.Interaction) -> None:
        await self._finish(inter, "default")

    async def _custom(self, inter: discord.Interaction) -> None:
        h, m, d = self._tabs
        modal = _TabsModal(history_tab=h, member_rules_tab=m, day_rules_tab=d)
        await inter.response.send_modal(modal)
        await modal.wait()
        if modal.out:
            self.choice = "custom"
            self.modal_out = modal.out
        for c in self.children:
            c.disabled = True
        try:
            await inter.edit_original_response(view=self)
        except Exception:
            pass
        self.stop()


# ── The sub-steps ────────────────────────────────────────────────────────────


async def _ask_toggle(w: _Step, current: dict) -> bool:
    """Step 9. Returns False when rotation ends up off (turning a running
    rotation off is persisted and announced here)."""
    from config import update_train_config_field

    was_on = bool(current.get("rotation_enabled"))
    wants = await w.ask_yes_no(
        "**Step 9 of 9 — Conductor Rotation** *(optional)*\n"
        "Want the bot to auto-pick fair conductors from a weekly pattern? It drafts the "
        "upcoming week for leadership to review, then confirms (and optionally announces) "
        "each day's conductor, always rotating to whoever has driven least. You can "
        "override any day by hand."
        + ("\n\n*Rotation is currently on. Choose Yes to review or adjust it.*" if was_on else "")
    )
    if wants:
        return True
    if was_on:
        update_train_config_field(w.guild_id, "rotation_enabled", 0)
        await w.channel.send(
            "🚂 Conductor Rotation turned off. Your presets and member rules stay saved, "
            "so you can re-enable any time from `/setup` → 🚂 Train."
        )
    return False


async def _ask_roster_source(w: _Step) -> None:
    """Step 9a. Free tier points at a tab + a name column (names are all the
    rotation needs); Premium uses the synced Member Roster, which also
    unlocks the role-scoped day rules in Step 9f (#337)."""
    from config import get_member_roster_config, save_member_roster_config

    rcfg = get_member_roster_config(w.guild_id)
    if w.is_premium:
        if rcfg.get("enabled"):
            await w.channel.send(
                "**Step 9a of 9 — Roster Source** 💎\n"
                f"Using your synced **Member Roster** (tab **{rcfg.get('tab_name') or 'Member Roster'}**) "
                "as the conductor pool. Members stay current automatically, and role-scoped "
                "day rules are unlocked for you in a later step."
            )
        else:
            await w.channel.send(
                "**Step 9a of 9 — Roster Source** 💎\n"
                "Your **Member Roster** sync isn't set up yet. Run **Member Sync** from "
                "`/setup` to auto-populate it from Discord; that becomes the conductor pool and "
                "unlocks role-scoped day rules. Until then, rotation reads any names in a tab "
                "called **Member Roster**."
            )
        return

    # Free: point at a tab + name column. Saved as a sync-disabled roster
    # config (enabled=0), which load_roster_members reads name-only (#337).
    cur_tab = rcfg.get("tab_name") or "Member Roster"
    cur_col_letter = _setup()._col_index_to_letter(int(rcfg.get("name_col", 1)))
    view = _RosterView(cur_tab=cur_tab, cur_col_letter=cur_col_letter)
    choice = await w.ask(
        "**Step 9a of 9 — Roster Source**\n"
        "Who can be a conductor? Point me at the tab in your Google Sheet that lists your "
        "members and the column holding their names. The bot rotates fairly through that "
        "list. *(Names are all the free rotation needs, no Discord IDs.)*\n"
        f"Current: tab **{cur_tab}**, name column **{cur_col_letter}**.",
        view,
        "choice",
    )
    if choice == "set" and view.modal_out:
        tab, name_idx = view.modal_out
        save_member_roster_config(w.guild_id, enabled=0, tab_name=tab, name_col=name_idx)
        await w.channel.send(
            f"✅ Roster source set: tab **{tab}**, name column "
            f"**{_setup()._col_index_to_letter(name_idx)}**."
        )


async def _ask_channel_and_time(w: _Step, current: dict, r: _Rotation) -> None:
    """Steps 9b / 9c: reuse the reminder channel and time when set, ask for
    rotation's own when reminders are off."""
    from config import update_train_config_field

    r.reminder_channel_id = current.get("reminder_channel_id", 0) or 0
    r.reminder_time = current.get("reminder_time", "22:00") or "22:00"
    if r.reminder_channel_id:
        await w.channel.send(
            "📅 Rotation will post the weekly draft and daily confirmation in "
            f"<#{r.reminder_channel_id}> at **{_format_time_with_tz(r.reminder_time, w.guild_tz)}** "
            "(reusing your reminder channel and time)."
        )
        return
    r.reminder_channel_id = await w.ask_channel(
        "Select the rotation channel…",
        suggested_name="leadership",
        current_id=0,
        step_text=(
            "**Step 9b of 9 — Rotation Channel**\n"
            "Rotation posts a weekly draft and a daily confirmation. Which channel should "
            "they go in? *(Your train reminders are off, so this is just for rotation.)*"
        ),
    )
    tz_label = wizard_steps.TIMEZONE_LABELS.get(
        w.guild_tz if isinstance(w.guild_tz, str) else "America/New_York", "ET"
    )
    time_raw = await w.keep_or_change(
        "**Step 9c of 9 — Rotation Time**\n"
        "What time should the draft and daily confirmation fire? "
        f"*(in your timezone: {tz_label})*\n*(e.g. `10:00pm`, `9:00am`)*",
        default="10:00pm",
        current="",
        modal_title="Rotation Time",
        modal_label="Time",
    )
    parsed = _parse_12h_time(time_raw)
    if parsed:
        r.reminder_time = parsed
    elif len(time_raw) == 5 and time_raw[2] == ":" and time_raw.replace(":", "").isdigit():
        r.reminder_time = time_raw
    else:
        r.reminder_time = "22:00"
        await w.channel.send(
            "⚠️ Couldn't read that time, so I'll use 10:00pm. You can change it later "
            "from `/setup` → 🚂 Train."
        )
    # Persist so the rotation loops (and the summary) have the channel + time.
    update_train_config_field(w.guild_id, "reminder_channel_id", r.reminder_channel_id)
    update_train_config_field(w.guild_id, "reminder_time", r.reminder_time)


async def _ask_draft_day(w: _Step, current: dict, r: _Rotation) -> None:
    """Step 9d."""
    r.weekly_draft_day = await w.ask(
        "**Step 9d of 9 — Weekly Draft Day**\n"
        "Which day should the bot post the upcoming week's draft for leadership to review? "
        "*(Sunday is typical: it previews the week starting Monday. The draft posts at the "
        "time above.)*",
        _WeekdaySelectView(current=current.get("weekly_draft_day", 6)),
        "selected",
    )


async def _ask_public_posts(w: _Step, current: dict, r: _Rotation) -> None:
    """Step 9e (opt-in)."""
    wants = await w.ask_yes_no(
        "**Step 9e of 9 — Public Posts**\n"
        "When you confirm each day's conductor, should the bot also announce them publicly "
        "for the whole alliance to see? *(Selecting No will record the conductor with no "
        "public post.)*"
    )
    if not wants:
        return
    r.rotation_public_channel_id = await w.ask_channel(
        "Select the public announcement channel…",
        suggested_name="general",
        current_id=current.get("rotation_public_channel_id", 0) or 0,
        gone_label="public post",
        step_text="Which channel should confirmed conductors be announced in?",
    )


async def _ask_role_scoped_days(w: _Step, current: dict, r: _Rotation) -> None:
    """Step 9f (Premium, #337). Saved assignments are preserved so a
    re-subscribe restores them; the draft-time Premium gate makes role days
    fall back to the full roster while a guild is on free."""
    r.rule_type_roles = dict(current.get("rule_type_roles") or {})
    if not w.is_premium:
        await w.channel.send(
            "**Step 9f of 9 — Role-Scoped Days** 💎\n"
            "Leadership / VS / Contest / Event days that rotate only members in a specific "
            "Discord role are a Premium feature. On the free plan every day rotates your full "
            "roster fairly. Run `/upgrade` to unlock role-scoped days."
        )
        return
    wants = await w.ask_yes_no(
        "**Step 9f of 9 — Role-Scoped Days** *(optional)* 💎\n"
        "Day rules can be **leadership**, **vs**, **contest**, or **event** days. By default "
        "leadership days use your main leadership role and the rest are picked by hand. Want "
        "to tie any of these to a specific Discord role, so the bot only rotates members in "
        "that role on those days?"
    )
    if not wants:
        return
    attach_view = _RuleRoleAttachView(r.rule_type_roles)
    await w.channel.send(
        "Pick a rule type and a role, then **Attach role to this rule**. Repeat for as "
        "many as you like, then hit **Done**.\n\n"
        f"**Attached so far:**\n{attach_view.summary()}",
        view=attach_view,
    )
    await w.wait(attach_view)
    r.rule_type_roles = attach_view.rule_type_roles


async def _ask_counted_reasons(w: _Step, current: dict, r: _Rotation) -> None:
    """Step 9g."""
    import train_rotation as tr

    counted_raw = await w.keep_or_change(
        "**Step 9g of 9 — Counted Reasons** *(most alliances keep the default)*\n"
        "The rotation always picks whoever has driven the fewest *counted* trains, so "
        "everyone takes a fair turn. Each reason you count here adds to a member's tally "
        "when they drive for that reason. The default counts regular turns but leaves out "
        "**birthday**, **welcome**, and **event** drives, so a one-off or bonus drive "
        "doesn't push someone to the back of the line.\n"
        f"Comma-separated; valid: `{', '.join(tr.REASONS)}`.",
        default=", ".join(tr.DEFAULT_COUNTED_REASONS),
        current=current.get("counted_reasons") or "",
        modal_title="Counted Reasons",
        modal_label="Reasons (comma-separated)",
    )
    valid = {x.strip().lower() for x in counted_raw.split(",") if x.strip()} & set(tr.REASONS)
    r.counted_reasons = ",".join(sorted(valid)) if valid else ""


async def _ask_sheet_tabs(w: _Step, current: dict, r: _Rotation) -> None:
    """Step 9h: one question, Keep / Default / Define my own."""
    r.history_tab = current.get("history_tab") or _DEF_HIST
    r.member_rules_tab = current.get("member_rules_tab") or _DEF_MR
    r.day_rules_tab = current.get("day_rules_tab") or _DEF_DR
    saved_custom = (
        r.history_tab != _DEF_HIST or r.member_rules_tab != _DEF_MR or r.day_rules_tab != _DEF_DR
    )

    def _tab_line(label, val, default):
        return f"{label}: **{val}**" + (" (default)" if val == default else "")

    view = _TabsChoiceView(
        saved_custom=saved_custom,
        history_tab=r.history_tab,
        member_rules_tab=r.member_rules_tab,
        day_rules_tab=r.day_rules_tab,
    )
    choice = await w.ask(
        "**Step 9h of 9 — Sheet Tabs**\n"
        "Rotation reads and writes three tabs. The bot creates them automatically if they "
        "don't exist yet:\n"
        f"{_tab_line('History tab', r.history_tab, _DEF_HIST)}\n"
        f"{_tab_line('Member Rules', r.member_rules_tab, _DEF_MR)}\n"
        f"{_tab_line('Day Rules', r.day_rules_tab, _DEF_DR)}\n\n"
        "Keep these, or define your own?",
        view,
        "choice",
    )
    if choice == "default":
        r.history_tab, r.member_rules_tab, r.day_rules_tab = _DEF_HIST, _DEF_MR, _DEF_DR
    elif choice == "custom":
        r.history_tab, r.member_rules_tab, r.day_rules_tab = view.modal_out
    # "keep" leaves the loaded values as-is.


async def _ask_weekly_pattern(w: _Step, current: dict, r: _Rotation) -> None:
    """Step 9i: opt in to a preset and name it; the editor opens after the
    save."""
    import train_rotation as tr

    r.active_preset = current.get("active_schedule_preset") or tr.DEFAULT_PRESET_NAME
    wants = await w.ask_yes_no(
        "**Step 9i of 9 — Weekly Pattern** *(recommended)*\n"
        "A weekly pattern (preset) sets what *kind* of day each weekday is, e.g. Monday = "
        "auto rotation, Friday = VS, Sunday = leadership. The bot fills in conductors from "
        "the pattern each week. Want to set one up now?"
    )
    if not wants:
        return
    await w.channel.send(
        "What should we name this pattern? *(e.g. `Standard Week`, `Season 6`. Type a "
        "name, or `skip` to use the default.)*"
    )
    reply = await wizard_registry.wait_or_cancel(
        w.bot.wait_for("message", check=w.check, timeout=300), w.cancel_event
    )
    if reply is None:
        if w.cancel_event.is_set():
            await w.channel.send(CANCEL_PLAIN)
        else:
            await w.timed_out()
        raise _Abort
    name_in = reply.content.strip()[:80]
    if name_in and name_in.lower() != "skip":
        r.active_preset = name_in
    r.open_editor = True


# ── Save and summary ─────────────────────────────────────────────────────────


def _save(w: _Step, r: _Rotation) -> None:
    import json

    from config import save_train_rotation_config

    save_train_rotation_config(
        w.guild_id,
        rotation_enabled=1,
        history_tab=r.history_tab,
        member_rules_tab=r.member_rules_tab,
        day_rules_tab=r.day_rules_tab,
        rotation_public_channel_id=r.rotation_public_channel_id,
        weekly_draft_day=r.weekly_draft_day,
        rule_type_roles=json.dumps(r.rule_type_roles),
        counted_reasons=r.counted_reasons,
        active_schedule_preset=r.active_preset,
    )


def _summary(w: _Step, r: _Rotation) -> str:
    import train_rotation as tr
    from config import get_member_roster_config

    lines = [
        f"Draft + confirm: <#{r.reminder_channel_id}> at "
        f"{_format_time_with_tz(r.reminder_time, w.guild_tz)}",
        f"Weekly draft day: {tr.WEEKDAY_NAMES[int(r.weekly_draft_day)]}",
        "Public posts: "
        + (
            f"<#{r.rotation_public_channel_id}>"
            if r.rotation_public_channel_id
            else "off (record only)"
        ),
        f"Active preset: {r.active_preset}",
    ]
    # Roster source — Premium uses the synced Member Roster; free shows the tab +
    # name column they pointed us at (#337).
    rsrc = get_member_roster_config(w.guild_id)
    if w.is_premium and rsrc.get("enabled"):
        lines.insert(
            0, f"Roster: synced Member Roster (tab {rsrc.get('tab_name') or 'Member Roster'}) 💎"
        )
    else:
        lines.insert(
            0,
            f"Roster: tab {rsrc.get('tab_name') or 'Member Roster'}, name column "
            f"{_setup()._col_index_to_letter(int(rsrc.get('name_col', 1)))}",
        )
    # Role-scoped day assignments only function on Premium (#337).
    if w.is_premium and r.rule_type_roles:
        lines.append(
            "Rule-type roles: "
            + ", ".join(
                f"{tr.RULE_LABELS.get(k, k)} → <@&{v}>" for k, v in r.rule_type_roles.items()
            )
        )
    return "\n".join(lines)


async def _load_preset_for_editor(w: _Step, r: _Rotation):
    """The preset the editor opens on, or the default when the Sheet can't
    be read (rotation stays saved and on either way)."""
    import train_rotation as tr

    try:
        preset = await asyncio.get_event_loop().run_in_executor(
            None, tr.load_preset, w.guild_id, r.day_rules_tab, r.active_preset
        )
    except Exception as e:
        from config import describe_sheet_error

        await w.channel.send(
            "⚠️ Couldn't load your saved pattern to open the editor: "
            f"{describe_sheet_error(e, guild_id=w.guild_id, tab=r.day_rules_tab)}\n"
            "Rotation is still saved and on — fix your Sheet and reopen the editor "
            "any time from `/train` → 📅 Schedule presets."
        )
        preset = None
    return preset or tr.SchedulePreset.default(r.active_preset)


# ── Entry point ──────────────────────────────────────────────────────────────


async def run_rotation_step(*, bot, interaction, channel, user, guild_id, cancel_event, guild_tz):
    """Step 9 — Conductor Rotation (#302). Owns the on/off toggle and, when on,
    every rotation setting as its own lettered, gated sub-step (9a..9h) so it
    never floods the user. Reuses the reminder channel/time when set; asks for
    its own when reminders are off. Turning a previously-on rotation off
    disables it (presets + member rules stay saved).

    Returns None on cancel/timeout (caller bails), `{"enabled": False}` when
    rotation ends up off, or, when on, a dict with the summary string, whether
    to open the preset editor, the preset, and the day-rules tab — so the caller
    can post one combined summary and open the editor last (#302)."""
    from config import get_train_config

    current = get_train_config(guild_id)
    w = _Step(bot, interaction, channel, user, guild_id, cancel_event, guild_tz)
    # Role-scoped day rules are Premium (#337); resolve once so Step 9a (roster
    # source) and Step 9f (role-scoped days) can branch on it.
    w.is_premium = await premium.is_premium(interaction.guild_id, interaction=interaction, bot=bot)
    r = _Rotation()
    try:
        if not await _ask_toggle(w, current):
            return {"enabled": False}
        await _ask_roster_source(w)
        await _ask_channel_and_time(w, current, r)
        await _ask_draft_day(w, current, r)
        await _ask_public_posts(w, current, r)
        await _ask_role_scoped_days(w, current, r)
        await _ask_counted_reasons(w, current, r)
        await _ask_sheet_tabs(w, current, r)
        await _ask_weekly_pattern(w, current, r)
    except _Abort:
        return None

    _save(w, r)
    result = {
        "enabled": True,
        "summary": _summary(w, r),
        "open_editor": r.open_editor,
        "preset": None,
        "day_rules_tab": r.day_rules_tab,
    }
    if r.open_editor:
        result["preset"] = await _load_preset_for_editor(w, r)
    else:
        await channel.send(
            "ℹ️ No weekly pattern set yet, so the bot uses a default until you build one from "
            "`/train` → 📅 Schedule presets (you can set member rules there too)."
        )
    print(f"[SETUP] Train rotation enabled for guild {guild_id}")
    return result

"""
train_rotation_ui_draft.py — the weekly-draft surface of Train Conductor
Rotation (#55), split out of train_rotation_ui.py (#373).

WeeklyDraftView — the Sunday draft posted to leadership. A day picker plus
Next / Assign / Skip / Regenerate buttons act on the chosen day. (The issue
mocked per-day button rows; a 7-day × 3-button grid exceeds Discord's
5-action-row cap, so it's a day-select + shared buttons.)

Shared infrastructure (embeds, RotationState, the roster picker also used by
the daily-confirm surface, load/regenerate helpers) stays in
train_rotation_ui.py; referenced here as `ui.X` (not a bound import) so the
existing `patch.object(ui, ...)` / `patch("train_rotation_ui.X", ...)` test
hooks keep working regardless of which file actually defines the symbol.
"""

import asyncio
from datetime import date, timedelta

import discord

import wizard_registry
import train_rotation as tr
import train_rotation_ui as ui

REASON_MAX_LEN = 200  # keeps the reason sub-line tidy in the draft embed


class _ReasonModal(discord.ui.Modal, title="Reason for this day"):
    """Captures a free-text reason for why a member is the day's conductor
    (e.g. "Nominated for helping with General's Trials"). Pre-filled with the
    current reason so it edits as well as adds; blank clears it."""

    def __init__(self, view: "WeeklyDraftView", dd: tr.DraftDay):
        super().__init__()
        self._view = view
        self._dd = dd
        day_label = f"{date.fromisoformat(dd.date):%a %b} {date.fromisoformat(dd.date).day}"
        self.reason = discord.ui.TextInput(
            label=f"Why is {dd.member or 'this day'} on {day_label}?"[:45],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=REASON_MAX_LEN,
            default=(dd.note or "")[:REASON_MAX_LEN],
            placeholder="e.g. Nominated for helping with General's Trials",
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        self._dd.note = self.reason.value.strip()
        await interaction.response.defer()
        await asyncio.to_thread(self._view._persist_day, self._dd)
        self._view._rebuild()
        embed = ui.build_weekly_draft_embed(
            self._view.draft, self._view.week_start, self._view.preset_name
        )
        try:
            if self._view.message:
                await self._view.message.edit(embed=embed, view=self._view)
        except discord.HTTPException:
            pass


class WeeklyDraftView(discord.ui.View):
    """Leadership-facing weekly draft. A day picker + shared action buttons edit
    the selected day; edits write straight to the Train History `scheduled`
    rows (the draft IS the schedule — no approve step)."""

    def __init__(
        self, bot, guild_id: int, draft: list[tr.DraftDay], week_start: date, preset_name: str
    ):
        super().__init__(timeout=ui.EDITOR_TIMEOUT)
        self.bot = bot
        self.guild_id = guild_id
        self.draft = draft
        self.week_start = week_start
        self.preset_name = preset_name
        self.selected_iso: str | None = None
        self.message: discord.Message | None = None
        self._rebuild()

    def _by_iso(self, iso: str) -> tr.DraftDay:
        return next(d for d in self.draft if d.date == iso)

    def _rebuild(self):
        self.clear_items()
        # Week navigation (row 0) — pick which week to view / draft. Opening the
        # hub on a Sunday lands on the upcoming week (see the hub default), and
        # these let leadership step to any week from there.
        prev_btn = discord.ui.Button(
            label="◀ Previous week", style=discord.ButtonStyle.secondary, row=0
        )
        prev_btn.callback = self._on_prev_week
        self.add_item(prev_btn)
        next_btn = discord.ui.Button(
            label="Next week ▶", style=discord.ButtonStyle.secondary, row=0
        )
        next_btn.callback = self._on_next_week
        self.add_item(next_btn)

        day_select = discord.ui.Select(
            placeholder="📅 Pick a day to adjust…",
            row=1,
            options=[
                discord.SelectOption(
                    label=f"{date.fromisoformat(dd.date):%a %b} {date.fromisoformat(dd.date).day}",
                    value=dd.date,
                    description=ui._short(ui._conductor_cell(dd), 100),
                    default=self.selected_iso == dd.date,
                )
                for dd in self.draft
            ],
        )
        day_select.callback = self._on_day
        self.add_item(day_select)

        # Labels spell out exactly what happens to the picked day.
        for label, style, cb in [
            ("⏭️ Go to next person", discord.ButtonStyle.primary, self._on_next),
            ("✏️ Assign someone", discord.ButtonStyle.secondary, self._on_assign),
            ("✏️ Add reason", discord.ButtonStyle.secondary, self._on_add_reason),
            ("✏️ Set to manual", discord.ButtonStyle.secondary, self._on_set_manual),
            ("🔄 Re-draft the whole week", discord.ButtonStyle.danger, self._on_regen),
        ]:
            btn = discord.ui.Button(label=label, style=style, row=2)
            btn.callback = cb
            self.add_item(btn)

    async def _on_prev_week(self, interaction: discord.Interaction):
        await self._shift_week(interaction, -7)

    async def _on_next_week(self, interaction: discord.Interaction):
        await self._shift_week(interaction, 7)

    async def _shift_week(self, interaction: discord.Interaction, days: int):
        """Move the view to another week and reload that week's draft. Existing
        weeks reconstruct from their saved rows; an empty future week generates a
        fresh draft (load_week_draft handles both)."""
        if not ui._is_leader(interaction):
            await interaction.response.send_message(ui.DENY_NOT_LEADER, ephemeral=True)
            return
        await interaction.response.defer()
        self.week_start = self.week_start + timedelta(days=days)
        self.selected_iso = None
        self.draft = await ui.load_week_draft_async(self.bot, self.guild_id, self.week_start)
        await self._rerender(interaction)

    async def _guard_day(self, interaction: discord.Interaction) -> tr.DraftDay | None:
        if not ui._is_leader(interaction):
            await interaction.response.send_message(ui.DENY_NOT_LEADER, ephemeral=True)
            return None
        if not self.selected_iso:
            await interaction.response.send_message(
                "ℹ️ Pick a day from the dropdown first.", ephemeral=True
            )
            return None
        return self._by_iso(self.selected_iso)

    async def _rerender(self, interaction: discord.Interaction):
        self._rebuild()
        embed = ui.build_weekly_draft_embed(self.draft, self.week_start, self.preset_name)
        try:
            if interaction.response.is_done():
                if self.message:
                    await self.message.edit(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

    def _persist_day(self, dd: tr.DraftDay):
        from config import get_train_config

        tab = get_train_config(self.guild_id).get("history_tab") or ""
        tr.set_day_status(
            self.guild_id,
            tab,
            dd.date,
            member=dd.member or "",
            reason=dd.reason,
            status=tr.STATUS_SCHEDULED,
            notes=dd.note,
        )

    async def _on_day(self, interaction: discord.Interaction):
        if not ui._is_leader(interaction):
            await interaction.response.send_message(ui.DENY_NOT_LEADER, ephemeral=True)
            return
        self.selected_iso = interaction.data["values"][0]
        await self._rerender(interaction)

    async def _on_next(self, interaction: discord.Interaction):
        dd = await self._guard_day(interaction)
        if dd is None:
            return
        await interaction.response.defer()
        state = await ui.load_rotation_state_async(self.bot, self.guild_id)
        other = {tr._norm(d.member) for d in self.draft if d.member and d.date != dd.date}
        member, reason, needs = tr.reroll_day(
            dd,
            eligible_pool=state.eligible_pool,
            role_pools=state.role_pools,
            member_rules=state.member_rules,
            history=state.history,
            counted_reasons=state.counted_reasons,
            other_scheduled=other,
            target_date=date.fromisoformat(dd.date),
            role_rules_enabled=state.role_rules_enabled,
        )
        dd.member, dd.reason, dd.needs_picking = member, reason, needs
        # Rerolling replaces the conductor, so any reason typed for the old pick
        # no longer applies — clear it.
        dd.note = ""
        dd.discord_id = ui._id_for_name(state, member) if member else ""
        await asyncio.to_thread(self._persist_day, dd)
        await self._rerender(interaction)

    async def _on_assign(self, interaction: discord.Interaction):
        dd = await self._guard_day(interaction)
        if dd is None:
            return
        # thinking=True so the button shows it's working during the roster read.
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = await ui.load_rotation_state_async(self.bot, self.guild_id)
        names, scope = ui._assign_pool_for_day(state, dd.rule_type)
        full_names = tr.roster_names(state.roster)
        day_label = f"{date.fromisoformat(dd.date):%a %b} {date.fromisoformat(dd.date).day}"

        async def _commit(name: str):
            dd.member = name
            dd.reason = "manual"
            dd.needs_picking = False
            dd.note = ""
            dd.discord_id = ui._id_for_name(state, name)
            await asyncio.to_thread(self._persist_day, dd)
            self._rebuild()
            if self.message:
                try:
                    await self.message.edit(
                        embed=ui.build_weekly_draft_embed(
                            self.draft, self.week_start, self.preset_name
                        ),
                        view=self,
                    )
                except discord.HTTPException:
                    pass

        picker = ui._RosterPickerView(
            names,
            current=dd.member or "",
            prompt=f"Who drives **{day_label}**?",
            modal_title="Assign conductor",
            on_commit=_commit,
            full_names=full_names,
            scope=scope,
            full_scope="\nShowing the **full roster**.",
        )
        await interaction.followup.send(content=picker.content(), view=picker, ephemeral=True)

    async def _on_add_reason(self, interaction: discord.Interaction):
        # Opening a modal must be the interaction's first response, so _guard_day
        # (which never defers on the success path) has to run before the modal.
        dd = await self._guard_day(interaction)
        if dd is None:
            return
        await interaction.response.send_modal(_ReasonModal(self, dd))

    async def _on_set_manual(self, interaction: discord.Interaction):
        # Leave the day for leadership to assign on the day (they get prompted by
        # the daily confirmation). Shows "Manual assignment" in the draft.
        dd = await self._guard_day(interaction)
        if dd is None:
            return
        await interaction.response.defer()
        dd.member = None
        dd.reason = tr.RULE_MANUAL
        dd.needs_picking = True
        dd.note = ""
        dd.discord_id = ""
        await asyncio.to_thread(self._persist_day, dd)
        await self._rerender(interaction)

    async def _on_regen(self, interaction: discord.Interaction):
        # Re-drafting throws away every current pick (including ones set by
        # hand), so confirm first.
        if not ui._is_leader(interaction):
            await interaction.response.send_message(ui.DENY_NOT_LEADER, ephemeral=True)
            return
        confirm = discord.ui.View(timeout=60)
        yes = discord.ui.Button(label="🔄 Yes, re-draft", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="↩️ Keep current draft", style=discord.ButtonStyle.secondary)

        redrafting = {"on": False}

        async def _do(ci: discord.Interaction):
            if redrafting["on"]:
                # Already running from an earlier click — ack and ignore so
                # repeated clicks don't fire concurrent regenerates (which raced
                # the Sheet reads and could wipe day rules).
                try:
                    await ci.response.defer()
                except discord.HTTPException:
                    pass
                return
            redrafting["on"] = True
            # Disable the confirm buttons + show progress IMMEDIATELY (not a bare
            # defer) so the re-draft can't be clicked two or three more times.
            for c in confirm.children:
                c.disabled = True
            await ci.response.edit_message(content="🔄 Re-drafting the whole week…", view=confirm)
            try:
                self.draft = await ui.regenerate_week_async(
                    self.bot, self.guild_id, self.week_start
                )
            except Exception as e:
                print(f"[TRAIN ROTATION] re-draft failed for guild {self.guild_id}: {e}")
                redrafting["on"] = False
                try:
                    await ci.edit_original_response(
                        content="⚠️ Re-draft failed — please try again.", view=None
                    )
                except discord.HTTPException:
                    pass
                return
            self.selected_iso = None
            self._rebuild()
            embed = ui.build_weekly_draft_embed(self.draft, self.week_start, self.preset_name)
            # Refresh the live draft in place; if its message is gone or its token
            # expired, re-post a fresh editable draft so leadership can keep going.
            refreshed = False
            if self.message:
                try:
                    await self.message.edit(embed=embed, view=self)
                    refreshed = True
                except discord.HTTPException:
                    refreshed = False
            if not refreshed:
                try:
                    self.message = await ci.channel.send(embed=embed, view=self)
                    refreshed = True
                except discord.HTTPException:
                    pass
            # Remove the ephemeral confirm so it doesn't linger as a stale "nothing
            # happened" artifact — the refreshed draft above is the live surface.
            try:
                await ci.delete_original_response()
            except discord.HTTPException:
                pass
            if not refreshed:
                try:
                    await ci.followup.send(
                        "🔄 Re-drafted, but I couldn't refresh the draft here — reopen it "
                        "with `/train`.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass

        async def _cancel(ci: discord.Interaction):
            for c in confirm.children:
                c.disabled = True
            await ci.response.edit_message(content="↩️ Kept the current draft.", view=confirm)

        yes.callback = _do
        no.callback = _cancel
        confirm.add_item(yes)
        confirm.add_item(no)
        await interaction.response.send_message(
            "🔄 **Re-draft the whole week?** This replaces every conductor for this week with "
            "fresh fair rotation picks, including any you set by hand or marked as no-train.",
            view=confirm,
            ephemeral=True,
        )

    async def on_timeout(self):
        await wizard_registry.expire_view_message(self.message, command_hint="/train draft_week")

"""
train_rotation_ui_presets.py — the schedule-preset editor surface of Train
Conductor Rotation (#55), split out of train_rotation_ui.py (#373).

TrainPresetEditorView — a live, owner-locked editor for a schedule preset.
Pick a day from a dropdown, set its rule with another dropdown, Save. (The
issue mocked dropdowns *inside* a modal; Discord modals only hold text
inputs, so the editor is a single live message instead.)

Shared infrastructure (embeds, RotationState, the roster picker used by the
weekly-draft/daily-confirm surfaces too) stays in train_rotation_ui.py;
referenced here as `ui.X` (not a bound import) so the existing
`patch.object(ui, ...)` / `patch("train_rotation_ui.X", ...)` test hooks
keep working regardless of which file actually defines the symbol.
"""

import asyncio

import discord

import wizard_registry
import train_rotation as tr
import train_rotation_ui as ui


def _roster_member_names(guild_id: int) -> list[str]:
    """Distinct roster display names for the Specific-member picker. Empty when
    no roster is configured / readable (caller falls back to free text)."""
    out: list[str] = []
    seen: set[str] = set()
    for r in tr.load_roster_members(guild_id):
        name = (r.get("name") or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


class _SpecificMemberPickerView(discord.ui.View):
    """Roster-backed picker for a Specific-member day pin (#302). The dropdown
    only sets a *pending* choice (Discord won't fire the change event if you
    re-pick the already-selected member, so selection alone can't be the commit);
    **💾 Save** commits it, **Cancel** closes with no change, and **✏️ Type a
    name instead** handles anyone not on the roster. The pending choice defaults
    to the current pin, so confirming an unchanged member is a single Save."""

    PAGE = 25

    def __init__(
        self, editor: "TrainPresetEditorView", names: list[str], *, current: str, day_name: str
    ):
        super().__init__(timeout=180)
        self.editor = editor
        self.names = names
        self.day_name = day_name
        self.page = 0
        self.pending = current or ""
        self._build()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.names) + self.PAGE - 1) // self.PAGE)

    def content(self) -> str:
        base = f"Who drives the train every **{self.day_name}**?"
        if self.pending:
            return f"{base}\nSelected: **{self.pending}** — hit **💾 Save** to confirm."
        if self.names:
            return f"{base}\nPick a member, then **💾 Save**."
        return f"{base}\nNo roster is set up — use **✏️ Type a name instead**."

    def _build(self):
        self.clear_items()
        if self.names:
            start = self.page * self.PAGE
            page_names = self.names[start : start + self.PAGE]
            sel = discord.ui.Select(
                placeholder="Pick the member…",
                options=[
                    discord.SelectOption(label=n[:100], value=n[:100], default=(n == self.pending))
                    for n in page_names
                ],
                row=0,
            )
            sel.callback = self._on_select
            self.add_item(sel)
            if self.total_pages > 1:
                prev = discord.ui.Button(
                    label="◀ Prev",
                    style=discord.ButtonStyle.secondary,
                    disabled=self.page == 0,
                    row=1,
                )
                prev.callback = self._prev
                self.add_item(prev)
                nxt = discord.ui.Button(
                    label="Next ▶",
                    style=discord.ButtonStyle.secondary,
                    disabled=self.page >= self.total_pages - 1,
                    row=1,
                )
                nxt.callback = self._next
                self.add_item(nxt)
            save = discord.ui.Button(label="💾 Save", style=discord.ButtonStyle.success, row=2)
            save.callback = self._on_save
            self.add_item(save)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
        cancel.callback = self._on_cancel
        self.add_item(cancel)
        typ = discord.ui.Button(
            label="✏️ Type a name instead", style=discord.ButtonStyle.secondary, row=2
        )
        typ.callback = self._on_type
        self.add_item(typ)

    async def _on_select(self, interaction: discord.Interaction):
        self.pending = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _on_save(self, interaction: discord.Interaction):
        if not self.pending:
            await interaction.response.send_message(
                "Pick a member first, or use **✏️ Type a name instead**.", ephemeral=True
            )
            return
        name = self.pending
        for c in self.children:
            c.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(
                content=f"📌 Pinned **{name}** to drive every {self.day_name}.", view=None
            )
        except discord.HTTPException:
            pass
        await self.editor._apply_specific_member(name)

    async def _on_cancel(self, interaction: discord.Interaction):
        for c in self.children:
            c.disabled = True
        self.stop()
        try:
            await interaction.response.edit_message(
                content="Canceled. The specific member was left unchanged.", view=None
            )
        except discord.HTTPException:
            pass

    async def _on_type(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.send_modal(
            ui._MemberNameModal(
                f"Specific member for every {self.day_name}",
                self.editor._apply_from_modal,
                current=self.pending,
            )
        )

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)


class TrainPresetEditorView(discord.ui.View):
    """Owner-locked live editor for one schedule preset. State held in
    `self.preset`; persisted to the Day Rules tab on Save."""

    def __init__(self, guild_id: int, user_id: int, preset: tr.SchedulePreset, day_rules_tab: str):
        super().__init__(timeout=ui.EDITOR_TIMEOUT)
        self.guild_id = guild_id
        self.user_id = user_id
        self.preset = preset
        self.day_rules_tab = day_rules_tab
        self.dirty = False
        # True once any edit has been made this session — gates the "Done making
        # changes" exit so a fresh editor just shows "Cancel (no changes needed)".
        self.ever_changed = False
        self.editing_day: int | None = None
        self.message: discord.Message | None = None
        self._rebuild()

    # ── component assembly ────────────────────────────────────────────────────

    def _rebuild(self):
        self.clear_items()

        day_select = discord.ui.Select(
            placeholder="📅 Pick a day to edit…",
            options=[
                discord.SelectOption(
                    label=tr.WEEKDAY_NAMES[wd],
                    value=str(wd),
                    description=tr.RULE_LABELS.get(self.preset.rule_for(wd).rule_type, "")[:100],
                    default=self.editing_day == wd,
                )
                for wd in range(7)
            ],
        )
        day_select.callback = self._on_day
        self.add_item(day_select)

        if self.editing_day is not None:
            cur = self.preset.rule_for(self.editing_day)

            rule_select = discord.ui.Select(
                placeholder="Rule type…",
                options=[
                    discord.SelectOption(
                        label=tr.RULE_LABELS[rt],
                        value=rt,
                        default=cur.rule_type == rt,
                    )
                    for rt in tr.DAY_RULE_TYPES
                ],
            )
            rule_select.callback = self._on_rule
            self.add_item(rule_select)

        # Action buttons. "Set specific member" only shows when the selected
        # day's rule is Specific member (the only rule that takes a pin).
        if (
            self.editing_day is not None
            and self.preset.rule_for(self.editing_day).rule_type == tr.RULE_SPECIFIC
        ):
            pin_btn = discord.ui.Button(
                label="✏️ Set specific member", style=discord.ButtonStyle.secondary
            )
            pin_btn.callback = self._on_set_pin
            self.add_item(pin_btn)

        # Save never locks the editor: it commits and says "saved" but leaves
        # the controls live so leadership can keep tweaking. Exit paths depend
        # on state: Cancel before any edit, Abandon to drop unsaved edits, and
        # "Done making changes" once anything has been touched (it saves first
        # if there are unsaved edits, so nothing is lost). (#302)
        save_btn = discord.ui.Button(
            label="💾 Save preset", style=discord.ButtonStyle.success, disabled=not self.dirty
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

        if self.dirty:
            abandon_btn = discord.ui.Button(
                label="🗑️ Abandon (no changes will be saved)", style=discord.ButtonStyle.danger
            )
            abandon_btn.callback = self._on_abandon
            self.add_item(abandon_btn)

        if not self.ever_changed:
            cancel_btn = discord.ui.Button(
                label="Cancel (no changes needed)", style=discord.ButtonStyle.secondary
            )
            cancel_btn.callback = self._on_cancel
            self.add_item(cancel_btn)
        else:
            done_btn = discord.ui.Button(
                label="✅ Done making changes", style=discord.ButtonStyle.primary, row=2
            )
            done_btn.callback = self._on_done
            self.add_item(done_btn)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(ui.DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _rerender(self, interaction: discord.Interaction, *, content: str | None = None):
        self._rebuild()
        embed = ui.build_preset_editor_embed(self.preset, dirty=self.dirty)
        try:
            if interaction.response.is_done():
                if self.message:
                    await self.message.edit(content=content, embed=embed, view=self)
            else:
                await interaction.response.edit_message(content=content, embed=embed, view=self)
        except discord.HTTPException:
            pass

    # ── callbacks ─────────────────────────────────────────────────────────────

    async def _on_day(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.editing_day = int(interaction.data["values"][0])
        await self._rerender(interaction)

    async def _on_rule(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        rt = interaction.data["values"][0]
        rule = self.preset.rule_for(self.editing_day)
        rule.rule_type = rt
        if rt != tr.RULE_SPECIFIC:
            rule.specific_member = ""
        self.preset.days[self.editing_day] = rule
        self.dirty = True
        self.ever_changed = True
        await self._rerender(interaction)

    async def _on_set_pin(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        day_name = tr.WEEKDAY_NAMES[self.editing_day]
        cur = self.preset.rule_for(self.editing_day).specific_member
        # Defer first (the roster read is a Sheets round-trip), then offer a
        # picker built from the roster, with a type-a-name escape (#302).
        await interaction.response.defer(ephemeral=True)
        names = await asyncio.to_thread(_roster_member_names, self.guild_id)
        view = _SpecificMemberPickerView(self, names, current=cur, day_name=day_name)
        await interaction.followup.send(view.content(), view=view, ephemeral=True)

    async def _apply_specific_member(self, name: str):
        """Pin `name` to the day being edited and refresh the editor message.
        Called by the roster picker (no triggering interaction of its own)."""
        rule = self.preset.rule_for(self.editing_day)
        rule.specific_member = name
        self.preset.days[self.editing_day] = rule
        self.dirty = True
        self.ever_changed = True
        self._rebuild()
        if self.message:
            try:
                await self.message.edit(
                    embed=ui.build_preset_editor_embed(self.preset, dirty=self.dirty), view=self
                )
            except discord.HTTPException:
                pass

    async def _apply_from_modal(self, interaction: discord.Interaction, name: str):
        """Type-a-name path: ack the modal submit, then apply the pin."""
        try:
            await interaction.response.send_message(
                f"📌 Pinned **{name}** to drive every {tr.WEEKDAY_NAMES[self.editing_day]}.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        await self._apply_specific_member(name)

    async def _on_save(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        ok = await asyncio.to_thread(tr.save_preset, self.guild_id, self.day_rules_tab, self.preset)
        if ok:
            # Stay open so leadership can keep editing; just clear the dirty
            # flag and confirm. "Done making changes" is how they exit.
            self.dirty = False
            await self._rerender(
                interaction,
                content=f"✅ Saved **{self.preset.name}**. Keep editing, or hit "
                "**Done making changes** when you're finished.",
            )
        else:
            await interaction.followup.send(
                "⚠️ Couldn't save the preset. Check that your Google Sheet is configured "
                "and the bot has edit access.",
                ephemeral=True,
            )

    async def _close(self, interaction: discord.Interaction, content: str):
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(
                content=content,
                embed=ui.build_preset_editor_embed(self.preset, dirty=self.dirty),
                view=self,
            )
        except discord.HTTPException:
            pass
        self.stop()

    async def _on_abandon(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await self._close(interaction, "🗑️ Abandoned. Your unsaved changes were not saved.")

    async def _on_cancel(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await self._close(interaction, "👍 Closed. No changes were needed.")

    async def _on_done(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        # Save any pending edits first so "Done" never silently drops work.
        if self.dirty:
            await interaction.response.defer()
            ok = await asyncio.to_thread(
                tr.save_preset, self.guild_id, self.day_rules_tab, self.preset
            )
            if not ok:
                await interaction.followup.send(
                    "⚠️ Couldn't save your latest changes. Check the Google Sheet and try "
                    "Save preset again.",
                    ephemeral=True,
                )
                return
            self.dirty = False
            for item in self.children:
                item.disabled = True
            if self.message:
                try:
                    await self.message.edit(
                        content=f"✅ Saved **{self.preset.name}**. All set!",
                        embed=ui.build_preset_editor_embed(self.preset, dirty=False),
                        view=self,
                    )
                except discord.HTTPException:
                    pass
            self.stop()
        else:
            await self._close(interaction, f"✅ All set! **{self.preset.name}** is saved.")

    async def on_timeout(self):
        await wizard_registry.expire_view_message(
            self.message, command_hint="/train schedule_preset edit"
        )


async def open_preset_editor(
    interaction: discord.Interaction, preset: tr.SchedulePreset, day_rules_tab: str
):
    """Send a fresh editor as the interaction's initial response."""
    view = TrainPresetEditorView(interaction.guild_id, interaction.user.id, preset, day_rules_tab)
    embed = ui.build_preset_editor_embed(preset, dirty=False)
    await interaction.response.send_message(embed=embed, view=view)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        view.message = None
    return view


async def open_preset_editor_followup(
    interaction: discord.Interaction, preset: tr.SchedulePreset, day_rules_tab: str
):
    """Send a fresh editor as a followup (when the response was already used,
    e.g. from the setup wizard)."""
    view = TrainPresetEditorView(interaction.guild_id, interaction.user.id, preset, day_rules_tab)
    embed = ui.build_preset_editor_embed(preset, dirty=False)
    view.message = await interaction.followup.send(embed=embed, view=view)
    return view


async def post_preset_editor(
    channel, guild_id: int, user_id: int, preset: tr.SchedulePreset, day_rules_tab: str
):
    """Post a fresh editor straight to a channel.

    Used at the end of the setup wizard, which runs long enough that the
    original interaction token has likely expired — so the editor is sent with
    `channel.send` rather than an interaction response/followup."""
    view = TrainPresetEditorView(guild_id, user_id, preset, day_rules_tab)
    embed = ui.build_preset_editor_embed(preset, dirty=False)
    view.message = await channel.send(embed=embed, view=view)
    return view

"""
The wizard pieces every `/setup` flow is built from: the role and channel
pickers with their create-one modals, the confirm and yes/no views, the
text-input modal and its launcher, `ask_keep_or_change`, the timezone and
schedule-type pickers, and the keep-or-flip re-entry gate.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611,
step 1) under the CLAUDE.md rule that a helper used by a second feature
moves to a shared module: `transfer_setup`, `alliance_duel_wizard` and the
storm structured-flow step all reached into `setup_cog` for these.
`setup_cog` imports every name back, so `from setup_cog import X` and
`patch("setup_cog.X")` still resolve; a new caller imports from here.

Nothing here knows which feature is being set up. A view that does belongs
next to its wizard, not in this module.
"""

import discord

import wizard_registry
from config import get_config
from messages import GENERIC_CMD_TIMEOUT
from wizard_registry import wait_view_or_cancel

WIZARD_STEP_TIMEOUT = 120  # 2 minutes per step

# ── Step views ─────────────────────────────────────────────────────────────────


class CreateRoleModal(discord.ui.Modal):
    def __init__(self):
        super().__init__(title="Create a New Role")
        self.role_name = None
        self.field = discord.ui.TextInput(
            label="Role name",
            placeholder="e.g. Member, Alliance Member, Leadership",
            required=True,
            max_length=100,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        self.role_name = self.field.value.strip()
        await interaction.response.defer()
        self.stop()


class RoleSelectStep(discord.ui.View):
    """Picks a role for a wizard step.

    When `current_id` is set and resolves to a live role via
    `guild.get_role`, a "Keep current" button is rendered above the
    picker so leadership doesn't have to rediscover the saved value.
    When `current_id` is set but no longer resolves (role deleted),
    `is_current_stale` flips True so callers can post a warning above
    the view.
    """

    def __init__(
        self,
        placeholder: str,
        *,
        current_id: int | None = None,
        current_name: str | None = None,
        guild: discord.Guild | None = None,
    ):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.selected_role = None
        self.confirmed = False
        self._placeholder = placeholder

        self.current_id = current_id
        self._current_name = current_name
        self._current_role: discord.Role | None = None
        if current_id and guild is not None:
            try:
                resolved = guild.get_role(current_id)
            except Exception:
                resolved = None
            if resolved is not None:
                self._current_role = resolved

        # Name-based fallback. Two cases this matters for:
        #   * Migration window — old guilds stored `leadership_role` by
        #     name only, so the new `leadership_role_id` column is 0
        #     until the wizard re-saves. Without this fallback,
        #     re-running /setup on an old guild shows no Keep-current
        #     button for the leadership role.
        #   * Rename safety — if the id was wiped but the name still
        #     matches a live role, fall back rather than show a
        #     misleading "deleted" warning.
        if self._current_role is None and current_name and guild is not None:
            for role in getattr(guild, "roles", []) or []:
                if getattr(role, "name", None) == current_name:
                    self._current_role = role
                    break

        self._render()

    @property
    def is_current_stale(self) -> bool:
        """True iff a saved value was given (either id or name) but no
        live role could be resolved. Wizards inspect this to surface a
        one-line warning. `current_id = 0` is the schema sentinel for
        "not set"; an empty `current_name` is the equivalent for the
        name-only path."""
        has_saved = bool(self.current_id) or bool(self._current_name)
        return has_saved and self._current_role is None

    def _render(self) -> None:
        self.clear_items()
        # Keep-current button on row 0 when we have a resolved role.
        if self._current_role is not None:
            role = self._current_role
            keep_btn = discord.ui.Button(
                label=f"Keep current: @{role.name}"[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep_cb(inter: discord.Interaction):
                self.selected_role = role
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"✅ Keeping: **@{role.name}**",
                    view=self,
                )
                self.stop()

            keep_btn.callback = _keep_cb
            self.add_item(keep_btn)
            select_row = 1
            create_row = 2
        else:
            select_row = 0
            create_row = 1

        select = discord.ui.RoleSelect(
            placeholder=self._placeholder,
            min_values=1,
            max_values=1,
            row=select_row,
        )

        async def _select_cb(interaction: discord.Interaction):
            self.selected_role = select.values[0]
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                interaction,
                content=f"✅ Selected: **{self.selected_role.name}**",
                view=self,
            )
            self.stop()

        select.callback = _select_cb
        self.add_item(select)

        create_btn = discord.ui.Button(
            label="➕ Create a new role",
            style=discord.ButtonStyle.secondary,
            row=create_row,
        )

        async def _create_cb(interaction: discord.Interaction):
            modal = CreateRoleModal()
            await interaction.response.send_modal(modal)
            await modal.wait()
            if not modal.role_name:
                return
            try:
                new_role = await interaction.guild.create_role(
                    name=modal.role_name,
                    reason=f"Created during Alliance Helper setup by {interaction.user.display_name}",
                )
                self.selected_role = new_role
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                await interaction.message.edit(
                    content=f"✅ Created and selected new role: **{new_role.name}**",
                    view=self,
                )
                self.stop()
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ I don't have permission to create roles. Please create the role manually first, then run `/setup` again.",
                    ephemeral=True,
                )
            except Exception as e:
                await interaction.followup.send(
                    f"⚠️ Could not create role: {e}",
                    ephemeral=True,
                )

        create_btn.callback = _create_cb
        self.add_item(create_btn)


class CreateChannelModal(discord.ui.Modal):
    def __init__(self, suggested_name: str = ""):
        super().__init__(title="Create a New Channel")
        self.channel_name = None
        self.field = discord.ui.TextInput(
            label="Channel name",
            placeholder=suggested_name or "e.g. announcements",
            default=suggested_name,
            required=True,
            max_length=100,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        self.channel_name = self.field.value.strip().lower().replace(" ", "-")
        await interaction.response.defer()
        self.stop()


class ChannelSelectStep(discord.ui.View):
    """Picks a destination channel or thread for a wizard step.

    Usual flow when both options are available (Premium guild with active
    threads): start with two buttons — **📢 Channel** and **🧵 Thread** —
    and reveal the appropriate select after the user picks. After picking,
    a "Pick a {other} instead" button stays visible so the user can swap
    if they chose the wrong type.

    Used because Discord's native `ChannelSelect` silently drops thread
    results when text-channel types are also in the picker (confirmed
    via /admin_debug_channels in production). Splitting them into a
    button-then-select flow guarantees both work.

    Falls back to a single ChannelSelect when:
      * `include_threads=False`, or
      * a guild isn't passed, or
      * the guild has no pickable threads (active, non-archived, in a
        channel the bot can post in, not auto-generated).
    """

    def __init__(
        self,
        placeholder: str,
        channel_types=None,
        suggested_name: str = "",
        allow_create: bool = True,
        include_threads: bool = False,
        guild: discord.Guild | None = None,
        *,
        current_id: int | None = None,
        current_name: str | None = None,
    ):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.selected_channel = None
        self.confirmed = False
        self.suggested_name = suggested_name
        self.allow_create = allow_create

        # State carried across the button-driven flow.
        self._placeholder = placeholder
        self._explicit_types = channel_types
        self._include_threads = include_threads
        self._guild = guild
        self._thread_lookup: dict[str, discord.Thread] = {}
        self._pickable_threads: list[discord.Thread] = (
            self._collect_pickable_threads(guild) if (include_threads and guild is not None) else []
        )

        # Keep-current support. If the wizard passes `current_id`, resolve
        # it to a live channel/thread; when it still exists, render a
        # "Keep current" button on top of the picker so leadership doesn't
        # have to rediscover their saved value. When `current_id` is set
        # but no longer resolves (channel deleted), `is_current_stale`
        # flips True so the caller can post a warning above the view.
        self.current_id = current_id
        self._current_name = current_name
        self._current_channel: discord.abc.GuildChannel | discord.Thread | None = None
        if current_id and guild is not None:
            resolved = None
            if hasattr(guild, "get_channel"):
                resolved = guild.get_channel(current_id)
            if resolved is None and hasattr(guild, "get_thread"):
                try:
                    resolved = guild.get_thread(current_id)
                except Exception:
                    resolved = None
            if resolved is not None:
                self._current_channel = resolved

        # Decide initial state. If we have threads to offer, start with the
        # button-driven choice. Otherwise just show the channel select
        # straight away — same as the pre-fix behavior.
        if self._pickable_threads:
            self._render_initial_choice()
        else:
            self._render_channel_select(switchable=False)

    @property
    def is_current_stale(self) -> bool:
        """True iff `current_id` was given but no longer resolves to a live
        channel/thread. Wizards inspect this to decide whether to send a
        one-line warning above the picker. Treats `0` as "not set" since
        that's the schema sentinel for an unconfigured channel."""
        return bool(self.current_id) and self._current_channel is None

    def _maybe_add_keep_current(self, *, row: int) -> bool:
        """Prepend a 'Keep current' button when a saved channel still
        resolves. Returns True iff the button was added — callers use this
        to know whether to shift the next component down a row."""
        if self._current_channel is None:
            return False
        ch = self._current_channel
        if isinstance(ch, discord.Thread):
            parent = ch.parent.name if ch.parent else "?"
            display = f"🧵 {ch.name} (in #{parent})"
        else:
            display = f"#{ch.name}"
        keep_btn = discord.ui.Button(
            label=f"Keep current: {display}"[:80],
            style=discord.ButtonStyle.success,
            row=row,
        )

        async def _keep_cb(inter: discord.Interaction):
            self.selected_channel = self._current_channel
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Keeping: **{display}**",
                view=self,
            )
            self.stop()

        keep_btn.callback = _keep_cb
        self.add_item(keep_btn)
        return True

    # ── Initial state: two buttons ─────────────────────────────────────

    def _render_initial_choice(self) -> None:
        self.clear_items()
        # Keep-current sits on its own row so the Channel/Thread choice
        # still reads as a paired decision below it.
        keep_added = self._maybe_add_keep_current(row=0)
        button_row = 1 if keep_added else 0

        async def _on_channel(inter: discord.Interaction):
            self._render_channel_select(switchable=True)
            await wizard_registry.safe_edit_response(inter, view=self)

        async def _on_thread(inter: discord.Interaction):
            self._render_thread_select(switchable=True)
            await wizard_registry.safe_edit_response(inter, view=self)

        ch_btn = discord.ui.Button(
            label="📢 Channel",
            style=discord.ButtonStyle.primary,
            row=button_row,
        )
        ch_btn.callback = _on_channel
        self.add_item(ch_btn)

        th_btn = discord.ui.Button(
            label="🧵 Thread",
            style=discord.ButtonStyle.primary,
            row=button_row,
        )
        th_btn.callback = _on_thread
        self.add_item(th_btn)

    # ── Channel-select state ───────────────────────────────────────────

    def _channel_types_for_select(self) -> list[discord.ChannelType]:
        """Decide which channel_types to send to Discord's ChannelSelect.

        When we have pickable threads, we use **text-only** here because
        threads come from the manual Select in the other state. When we
        don't have a guild (e.g. unit-test path), we fall back to the
        old mixed-types list — which is what the existing tests assert.
        """
        if self._explicit_types:
            return list(self._explicit_types)
        if self._include_threads and not self._pickable_threads:
            return [
                discord.ChannelType.text,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.news_thread,
            ]
        return [discord.ChannelType.text]

    def _render_channel_select(self, *, switchable: bool) -> None:
        self.clear_items()
        keep_added = self._maybe_add_keep_current(row=0)
        select_row = 1 if keep_added else 0
        secondary_row = select_row + 1

        types = self._channel_types_for_select()
        select = discord.ui.ChannelSelect(
            placeholder=self._placeholder,
            min_values=1,
            max_values=1,
            channel_types=types,
            row=select_row,
        )

        async def _select_cb(inter: discord.Interaction):
            self.selected_channel = select.values[0]
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Selected: **{self.selected_channel.name}**",
                view=self,
            )
            self.stop()

        select.callback = _select_cb
        self.add_item(select)

        if switchable and self._pickable_threads:
            switch_btn = discord.ui.Button(
                label="🧵 Pick a thread instead",
                style=discord.ButtonStyle.secondary,
                row=secondary_row,
            )

            async def _switch(inter: discord.Interaction):
                self._render_thread_select(switchable=True)
                await wizard_registry.safe_edit_response(inter, view=self)

            switch_btn.callback = _switch
            self.add_item(switch_btn)

        if self.allow_create:
            self._add_create_button(row=secondary_row)

    # ── Thread-select state ────────────────────────────────────────────

    def _render_thread_select(self, *, switchable: bool) -> None:
        self.clear_items()
        self._thread_lookup.clear()
        keep_added = self._maybe_add_keep_current(row=0)
        select_row = 1 if keep_added else 0
        secondary_row = select_row + 1

        # Sort so the dropdown groups threads under their parent and is
        # alphabetised within each group — easier for the user to find.
        sorted_threads = sorted(
            self._pickable_threads,
            key=lambda t: ((t.parent.name if t.parent else "zzz"), t.name),
        )

        thread_select = discord.ui.Select(
            placeholder="Pick a thread...",
            min_values=1,
            max_values=1,
            row=select_row,
        )
        # Discord caps Select options at 25.
        for t in sorted_threads[:25]:
            parent_name = t.parent.name if t.parent else "?"
            label = f"{t.name} (in #{parent_name})"[:100]
            value = str(t.id)
            thread_select.add_option(label=label, value=value)
            self._thread_lookup[value] = t

        async def _select_cb(inter: discord.Interaction):
            picked = self._thread_lookup.get(thread_select.values[0])
            if picked is None:
                await inter.response.send_message(
                    "⚠️ Could not resolve that thread. Try again.",
                    ephemeral=True,
                )
                return
            self.selected_channel = picked
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            parent_name = picked.parent.name if picked.parent else "?"
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Selected thread: **{picked.name}** (in #{parent_name})",
                view=self,
            )
            self.stop()

        thread_select.callback = _select_cb
        self.add_item(thread_select)

        if switchable:
            switch_btn = discord.ui.Button(
                label="📢 Pick a channel instead",
                style=discord.ButtonStyle.secondary,
                row=secondary_row,
            )

            async def _switch(inter: discord.Interaction):
                self._render_channel_select(switchable=True)
                await wizard_registry.safe_edit_response(inter, view=self)

            switch_btn.callback = _switch
            self.add_item(switch_btn)

    # ── Create-channel button ──────────────────────────────────────────

    def _add_create_button(self, *, row: int) -> None:
        create_btn = discord.ui.Button(
            label="➕ Create a new channel",
            style=discord.ButtonStyle.secondary,
            row=row,
        )

        async def _create_cb(interaction: discord.Interaction):
            modal = CreateChannelModal(suggested_name=self.suggested_name)
            await interaction.response.send_modal(modal)
            await modal.wait()
            if not modal.channel_name:
                return
            try:
                new_channel = await interaction.guild.create_text_channel(
                    name=modal.channel_name,
                    reason=f"Created during Alliance Helper setup by {interaction.user.display_name}",
                )
                self.selected_channel = new_channel
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                await interaction.message.edit(
                    content=f"✅ Created and selected **#{new_channel.name}**.",
                    view=self,
                )
                self.stop()
            except discord.Forbidden:
                await interaction.followup.send(
                    "⚠️ I don't have permission to create channels. Please create it manually first, then run `/setup` again.",
                    ephemeral=True,
                )
            except Exception as e:
                await interaction.followup.send(
                    f"⚠️ Could not create channel: {e}",
                    ephemeral=True,
                )

        create_btn.callback = _create_cb
        self.add_item(create_btn)

    @staticmethod
    def _collect_pickable_threads(guild: discord.Guild) -> list[discord.Thread]:
        """Return active threads in `guild` that are reasonable destinations
        for an announcement / reminder. Filters out:
          * archived or locked threads (Discord wouldn't accept posts anyway)
          * threads under channels the bot can't post in (avoids picking a
            destination that will then fail)
          * survey-* threads under the configured survey channel — those
            are auto-generated per-user threads, not destinations.
        """
        # The survey-channel filter requires the guild's saved config. It's
        # a convenience filter; if the config DB isn't reachable (e.g. unit
        # tests that don't set up a temp DB) skip it rather than crash —
        # the filter is to declutter the dropdown, not load-bearing.
        survey_chan: int = 0
        try:
            cfg = get_config(guild.id)
            if cfg:
                survey_chan = cfg.survey_channel_id or 0
        except Exception:
            pass
        bot_member = guild.me
        results: list[discord.Thread] = []
        for t in guild.threads:
            if t.archived or t.locked:
                continue
            if t.parent is None:
                continue
            # Skip auto-generated survey threads on the survey channel.
            if survey_chan and t.parent_id == survey_chan:
                continue
            # Make sure the bot can post here.
            if bot_member is not None:
                try:
                    perms = t.permissions_for(bot_member)
                    if not perms.send_messages_in_threads:
                        continue
                except Exception:
                    pass
            results.append(t)
        return results


class ConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.confirmed = None

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


class TextInputModal(discord.ui.Modal):
    def __init__(self, title: str, label: str, placeholder: str = "", default: str = ""):
        super().__init__(title=title)
        self.value = None
        self.field = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            default=default,
            required=True,
            max_length=200,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        self.value = self.field.value.strip()
        await interaction.response.defer()
        self.stop()


class ModalLaunchView(discord.ui.View):
    """Button that opens a modal — used for text input steps.

    When `current_value` is passed, a Keep-current button is rendered
    alongside Enter Value. Clicking it sets `self.modal.value =
    current_value` and stops the view, so callers that read
    `modal.value` after `view.wait()` need no changes. `current_display`
    overrides the label text (useful for truncating long Sheet IDs).

    `on_keep_current` is for modals whose `value` is a read-only
    derived property (e.g. ``ServerRangeModal`` in `/setup` → 🌟 Shiny Tasks
    where the wizard reads `min_value` / `max_value` rather than a
    single `value`). When provided, the callable is invoked with the
    modal as its only argument *instead* of the default ``modal.value
    = current_value``, so the caller can populate whatever attributes
    the wizard's post-submit code actually reads.
    """

    def __init__(
        self,
        modal: TextInputModal,
        *,
        current_value: str | None = None,
        current_display: str | None = None,
        on_keep_current=None,
    ):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.modal = modal
        self.confirmed = False
        self._current_value = current_value
        self._current_display = current_display or current_value

        if current_value:
            keep_btn = discord.ui.Button(
                label=f"Keep current: {self._current_display}"[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep_cb(inter: discord.Interaction):
                if on_keep_current is not None:
                    on_keep_current(self.modal)
                else:
                    self.modal.value = current_value
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"✅ Keeping: **{self._current_display}**",
                    view=self,
                )
                self.stop()

            keep_btn.callback = _keep_cb
            self.add_item(keep_btn)

    @discord.ui.button(label="✏️ Enter Value", style=discord.ButtonStyle.primary)
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(self.modal)
        await self.modal.wait()
        self.confirmed = True
        button.disabled = True
        try:
            await interaction.message.edit(
                content=f"✅ Entered: **{self.modal.value}**",
                view=self,
            )
        except discord.HTTPException:
            pass
        self.stop()


async def ask_keep_or_change(
    channel,
    prompt: str,
    default: str,
    modal_title: str,
    modal_label: str,
    timeout_cmd: str | None = None,
    cancel_event=None,
    current: str | None = None,
) -> str | None:
    """Show a `Keep current / Use default / Define your own` view and
    return the chosen value.

    Rendering depends on what's been saved before:
      * No saved value (``current`` is None or empty): two-button
        layout **✅ Use default: {default}** / **✏️ Define my own**.
        The keep button returns ``default``.
      * Saved value matches the hardcoded default: two-button layout
        **Keep current: {current}** / **✏️ Define my own**. Labels
        as "Keep current" rather than "Use default" so leadership
        running the wizard a second time sees what's actually saved
        (the values are identical anyway, but the wording makes the
        Keep-current intent obvious — fixes the
        "is Use default going to wipe my settings?" anxiety).
      * Saved value differs from default: three-button layout
        **Keep current: {current}** / **↩️ Use default: {default}**
        / **✏️ Define my own**. Lets leadership revert to the
        hardcoded baseline in one click instead of typing it manually.

    The button labels include the value so the prompt body never has to
    repeat it. Returns None on timeout (and posts a timeout message
    referencing `timeout_cmd` if provided), or on /cancel (silently —
    the /cancel command itself acks the user).
    """
    has_saved = bool(current)
    has_distinct_current = has_saved and current != default
    pre_filled = current if has_saved else default

    class KeepOrChangeDefaultView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_STEP_TIMEOUT)
            self.value = None
            self.confirmed = False

            # Build buttons explicitly so we can vary the layout based on
            # whether anything is saved and whether it matches default.
            # Decorator-based buttons can't be conditionally added.
            keep_label = (
                f"Keep current: {current}"[:80] if has_saved else f"✅ Use default: {default}"[:80]
            )
            keep_btn = discord.ui.Button(label=keep_label, style=discord.ButtonStyle.success)

            async def _keep_cb(inter: discord.Interaction):
                chosen = current if has_saved else default
                self.value = chosen
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content=f"{prompt}\n\n✅ Using **{chosen}**", view=self
                )
                self.stop()

            keep_btn.callback = _keep_cb
            self.add_item(keep_btn)

            if has_distinct_current:
                revert_btn = discord.ui.Button(
                    label=f"↩️ Use default: {default}"[:80],
                    style=discord.ButtonStyle.secondary,
                )

                async def _revert_cb(inter: discord.Interaction):
                    self.value = default
                    self.confirmed = True
                    for item in self.children:
                        item.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=f"{prompt}\n\n✅ Reverted to default: **{default}**",
                        view=self,
                    )
                    self.stop()

                revert_btn.callback = _revert_cb
                self.add_item(revert_btn)

            change_btn = discord.ui.Button(
                label="✏️ Define my own",
                style=discord.ButtonStyle.secondary,
            )

            async def _change_cb(inter: discord.Interaction):
                modal = TextInputModal(modal_title, modal_label, default=pre_filled)
                await inter.response.send_modal(modal)
                await modal.wait()
                self.value = (modal.value or pre_filled).strip() or pre_filled
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                try:
                    await inter.message.edit(
                        content=f"{prompt}\n\n✅ Using **{self.value}**", view=self
                    )
                except discord.HTTPException:
                    pass
                self.stop()

            change_btn.callback = _change_cb
            self.add_item(change_btn)

    view = KeepOrChangeDefaultView()
    await channel.send(prompt, view=view)
    await wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return None
    if not view.confirmed:
        if timeout_cmd:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=timeout_cmd))
        return None
    return view.value


# ── Timezone configuration ─────────────────────────────────────────────────────
# Format: (tz_database_name, display_label)
# Labels show (UTC offset, timezone name, and example cities
# Note: offsets shown are standard time — DST-observing zones shift +1 in summer

TIMEZONE_OPTIONS = [
    ("Pacific/Honolulu", "(UTC-10) Hawaii (Honolulu)"),
    ("America/Anchorage", "(UTC-9) Alaska (Anchorage)"),
    ("America/Los_Angeles", "(UTC-8) Pacific (Los Angeles, Seattle, Vancouver)"),
    ("America/Denver", "(UTC-7) Mountain (Denver, Phoenix, Calgary)"),
    ("America/Chicago", "(UTC-6) Central (Chicago, Dallas, Mexico City)"),
    ("America/New_York", "(UTC-5) Eastern (New York, Toronto, Miami)"),
    ("America/Sao_Paulo", "(UTC-3) Brazil (São Paulo, Rio de Janeiro)"),
    ("America/Argentina/Buenos_Aires", "(UTC-3) Argentina (Buenos Aires)"),
    ("Atlantic/Azores", "(UTC-1) Azores"),
    ("Europe/London", "(UTC+0) GMT/BST (London, Dublin, Lisbon)"),
    ("Europe/Paris", "(UTC+1) Central European (Paris, Berlin, Rome)"),
    ("Europe/Helsinki", "(UTC+2) Eastern European (Helsinki, Athens, Cairo)"),
    ("Europe/Moscow", "(UTC+3) Moscow (Moscow, Istanbul, Riyadh)"),
    ("Asia/Dubai", "(UTC+4) Gulf (Dubai, Abu Dhabi)"),
    ("Asia/Karachi", "(UTC+5) Pakistan (Karachi, Islamabad)"),
    ("Asia/Kolkata", "(UTC+5:30) India (Mumbai, Delhi, Bangalore)"),
    ("Asia/Dhaka", "(UTC+6) Bangladesh (Dhaka)"),
    ("Asia/Bangkok", "(UTC+7) Indochina (Bangkok, Jakarta, Hanoi)"),
    ("Asia/Shanghai", "(UTC+8) China/Singapore (Shanghai, Beijing, Singapore)"),
    ("Asia/Tokyo", "(UTC+9) Japan/Korea (Tokyo, Seoul)"),
    ("Australia/Sydney", "(UTC+10) Eastern Australia (Sydney, Melbourne)"),
    ("Pacific/Auckland", "(UTC+12) New Zealand (Auckland, Wellington)"),
]

# Map from tz_database_name → display label
TIMEZONE_LABELS = {tz: label for tz, label in TIMEZONE_OPTIONS}


class TimezoneSelectView(discord.ui.View):
    """Single dropdown covering all supported timezones, ordered by (UTC offset.

    When `current` is passed and matches a known timezone option, the
    view prepends a green Keep-current button above the select so
    leadership doesn't have to re-pick their timezone on a re-run.
    """

    def __init__(self, *, current: str | None = None):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.selected = None
        self.confirmed = False
        self.current = current

        keep_added = False
        if current and current in TIMEZONE_LABELS:
            keep_label = TIMEZONE_LABELS[current]
            keep_btn = discord.ui.Button(
                label=f"Keep current: {keep_label}"[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep_cb(inter: discord.Interaction):
                self.selected = current
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"✅ Keeping timezone: **{keep_label}**",
                    view=self,
                )
                self.stop()

            keep_btn.callback = _keep_cb
            self.add_item(keep_btn)
            keep_added = True

        select = discord.ui.Select(
            placeholder="Select your timezone...",
            options=[
                discord.SelectOption(label=label[:100], value=tz) for tz, label in TIMEZONE_OPTIONS
            ],
            row=1 if keep_added else 0,
        )

        async def _cb(interaction: discord.Interaction):
            self.selected = select.values[0]
            self.confirmed = True
            select.disabled = True
            label = TIMEZONE_LABELS.get(self.selected, self.selected)
            await wizard_registry.safe_edit_response(
                interaction, content=f"✅ Timezone: **{label}**", view=self
            )
            self.stop()

        select.callback = _cb
        self.add_item(select)


class ScheduleTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.selected = None

    @discord.ui.button(label="🔁 Repeating cycle", style=discord.ButtonStyle.primary)
    async def repeating(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "repeating"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(
            interaction, content="✅ Schedule: **Repeating cycle**", view=self
        )
        self.stop()

    @discord.ui.button(label="✏️ Add manually each time", style=discord.ButtonStyle.secondary)
    async def manual(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "manual"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(
            interaction, content="✅ Schedule: **Manual (add per event)**", view=self
        )
        self.stop()


class YesNoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.selected = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = False
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


# ── Keep-or-flip Yes/No re-entry gate ───────────────────────────────────────
# Used by power-refresh DM step (and previously the Judicator role step,
# now dropped per Rule G / #167).


class _KeepOrFlipYesNoGate(discord.ui.View):
    """Re-entry gate for a yes/no wizard step that already has a saved
    value. Two buttons: Keep current (success) / Flip (secondary).
    Sets `self.value` to the resolved bool, or None on timeout."""

    def __init__(
        self,
        *,
        current_value: bool,
        keep_label_yes: str = "Keep current: Yes",
        keep_label_no: str = "Keep current: No",
        flip_label_yes: str = "↩️ Switch to: Yes",
        flip_label_no: str = "↩️ Switch to: No",
    ):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.value: bool | None = None
        self.cancelled = False

        keep_label = keep_label_yes if current_value else keep_label_no
        flip_label = flip_label_no if current_value else flip_label_yes

        keep_btn = discord.ui.Button(
            label=keep_label[:80],
            style=discord.ButtonStyle.success,
        )

        async def _keep_cb(inter: discord.Interaction):
            self.value = bool(current_value)
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Keeping **{'Yes' if self.value else 'No'}**",
                view=self,
            )
            self.stop()

        keep_btn.callback = _keep_cb
        self.add_item(keep_btn)

        flip_btn = discord.ui.Button(
            label=flip_label[:80],
            style=discord.ButtonStyle.secondary,
        )

        async def _flip_cb(inter: discord.Interaction):
            self.value = not bool(current_value)
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Switched to **{'Yes' if self.value else 'No'}**",
                view=self,
            )
            self.stop()

        flip_btn.callback = _flip_cb
        self.add_item(flip_btn)

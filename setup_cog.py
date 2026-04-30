"""
setup_cog.py — /setup_* wizards for new guilds

Walks a server admin through configuring the bot using Discord's native
role and channel select menus. All values are saved to the config database.

Holds /setup, /setup_reset, /view_configuration, and the per-feature
/setup_train, /setup_growth, /setup_birthdays, /setup_desertstorm,
/setup_canyonstorm, /setup_events, /setup_survey commands.
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from config import (
    get_config, get_or_create_config, save_config, update_config_field,
    GuildConfig,
)
import premium
from wizard_registry import wait_view_or_cancel

WIZARD_TIMEOUT = 120  # 2 minutes per step


def _parse_12h_time(raw: str) -> str:
    """
    Parse a user-entered time like '10:15pm', '9am', '9:00 AM' into
    HH:MM 24h string for storage. Returns None if unparseable.
    """
    import re
    raw = raw.strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)$", raw)
    if not m:
        return None
    hour, minute, period = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if hour < 1 or hour > 12 or minute < 0 or minute > 59:
        return None
    if period == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return f"{hour:02d}:{minute:02d}"


def _parse_month_day(raw: str) -> str:
    """
    Parse 'Month Day' into YYYY-MM-DD using the most recent occurrence.
    Always looks backward — never assumes a future date.
    Examples (today = April 25 2026):
      'February 20' → 2026-02-20  (already passed this year)
      'December 3'  → 2025-12-03  (hasn't happened yet this year, so last year)
      'May 2'       → 2026-05-02  (upcoming this year, but within ~5 days so still this year)
    Rule: if the date this year is in the future beyond today, use last year.
    """
    import re
    from datetime import date, datetime
    raw = raw.strip()
    try:
        parsed = datetime.strptime(raw, "%B %d")
    except ValueError:
        try:
            parsed = datetime.strptime(raw, "%b %d")
        except ValueError:
            return None
    today     = date.today()
    this_year = date(today.year, parsed.month, parsed.day)
    last_year = date(today.year - 1, parsed.month, parsed.day)
    # Allow up to 31 days in the future (next upcoming event within a month)
    # Anything further out uses last year's date
    if (this_year - today).days > 31:
        return last_year.isoformat()
    return this_year.isoformat()


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
    def __init__(self, placeholder: str):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected_role = None
        self.confirmed     = False

        select = discord.ui.RoleSelect(placeholder=placeholder, min_values=1, max_values=1, row=0)
        async def _cb(interaction: discord.Interaction):
            self.selected_role = select.values[0]
            self.confirmed     = True
            select.disabled    = True
            await interaction.response.edit_message(
                content=f"✅ Selected: **{self.selected_role.name}**",
                view=self,
            )
            self.stop()
        select.callback = _cb
        self.add_item(select)

    @discord.ui.button(label="➕ Create a new role", style=discord.ButtonStyle.secondary, row=1)
    async def create_role(self, interaction: discord.Interaction, button: discord.ui.Button):
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
            self.confirmed     = True
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
    ):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected_channel = None
        self.confirmed        = False
        self.suggested_name   = suggested_name
        self.allow_create     = allow_create

        # State carried across the button-driven flow.
        self._placeholder      = placeholder
        self._explicit_types   = channel_types
        self._include_threads  = include_threads
        self._guild            = guild
        self._thread_lookup: dict[str, discord.Thread] = {}
        self._pickable_threads: list[discord.Thread] = (
            self._collect_pickable_threads(guild)
            if (include_threads and guild is not None)
            else []
        )

        # Decide initial state. If we have threads to offer, start with the
        # button-driven choice. Otherwise just show the channel select
        # straight away — same as the pre-fix behavior.
        if self._pickable_threads:
            self._render_initial_choice()
        else:
            self._render_channel_select(switchable=False)

    # ── Initial state: two buttons ─────────────────────────────────────

    def _render_initial_choice(self) -> None:
        self.clear_items()

        async def _on_channel(inter: discord.Interaction):
            self._render_channel_select(switchable=True)
            await inter.response.edit_message(view=self)

        async def _on_thread(inter: discord.Interaction):
            self._render_thread_select(switchable=True)
            await inter.response.edit_message(view=self)

        ch_btn = discord.ui.Button(
            label="📢 Channel", style=discord.ButtonStyle.primary, row=0,
        )
        ch_btn.callback = _on_channel
        self.add_item(ch_btn)

        th_btn = discord.ui.Button(
            label="🧵 Thread", style=discord.ButtonStyle.primary, row=0,
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

        types = self._channel_types_for_select()
        select = discord.ui.ChannelSelect(
            placeholder=self._placeholder,
            min_values=1, max_values=1,
            channel_types=types, row=0,
        )

        async def _select_cb(inter: discord.Interaction):
            self.selected_channel = select.values[0]
            self.confirmed        = True
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(
                content=f"✅ Selected: **{self.selected_channel.name}**",
                view=self,
            )
            self.stop()
        select.callback = _select_cb
        self.add_item(select)

        if switchable and self._pickable_threads:
            switch_btn = discord.ui.Button(
                label="🧵 Pick a thread instead",
                style=discord.ButtonStyle.secondary, row=1,
            )
            async def _switch(inter: discord.Interaction):
                self._render_thread_select(switchable=True)
                await inter.response.edit_message(view=self)
            switch_btn.callback = _switch
            self.add_item(switch_btn)

        # The "Create a new channel" button is irrelevant in the
        # button-driven flow (the user would have picked Channel already
        # and is now on this state). Only show it on the simple, single-
        # ChannelSelect path — i.e., when the picker doesn't include
        # threads at all and we're not in the switchable variant.
        has_threads_in_picker = any(
            t in types for t in (
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.news_thread,
            )
        )
        if self.allow_create and not has_threads_in_picker and not switchable:
            self._add_create_button(row=1)

    # ── Thread-select state ────────────────────────────────────────────

    def _render_thread_select(self, *, switchable: bool) -> None:
        self.clear_items()
        self._thread_lookup.clear()

        # Sort so the dropdown groups threads under their parent and is
        # alphabetised within each group — easier for the user to find.
        sorted_threads = sorted(
            self._pickable_threads,
            key=lambda t: ((t.parent.name if t.parent else "zzz"), t.name),
        )

        thread_select = discord.ui.Select(
            placeholder="Pick a thread...",
            min_values=1, max_values=1, row=0,
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
            self.confirmed        = True
            for item in self.children:
                item.disabled = True
            parent_name = picked.parent.name if picked.parent else "?"
            await inter.response.edit_message(
                content=f"✅ Selected thread: **{picked.name}** (in #{parent_name})",
                view=self,
            )
            self.stop()
        thread_select.callback = _select_cb
        self.add_item(thread_select)

        if switchable:
            switch_btn = discord.ui.Button(
                label="📢 Pick a channel instead",
                style=discord.ButtonStyle.secondary, row=1,
            )
            async def _switch(inter: discord.Interaction):
                self._render_channel_select(switchable=True)
                await inter.response.edit_message(view=self)
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
                self.confirmed        = True
                for item in self.children:
                    item.disabled = True
                await interaction.message.edit(
                    content=f"✅ Created and selected: **#{new_channel.name}**",
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
        bot_member   = guild.me
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
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = None

    @discord.ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
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
    """Button that opens a modal — used for text input steps."""
    def __init__(self, modal: TextInputModal):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.modal     = modal
        self.confirmed = False

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
    """Show a `Use default / Define your own` view and return the chosen value.

    Two-vs-three button rendering:
      * If `current` is None or equals `default`, render the original
        two-button layout: **✅ Use default: {default}** / **✏️ Define
        my own**. The keep button returns `default`.
      * If `current` is provided AND differs from `default`, render
        three buttons: **✅ Keep current: {current}** / **↩️ Use
        default: {default}** / **✏️ Define my own**. This stops the
        wizard from labelling a previously-saved guild value as the
        "default" (which is misleading — "default" should mean the
        bot's hardcoded baseline, not whatever the guild last entered)
        while still letting the user revert to that baseline in one
        click instead of typing it manually.

    The button labels include the value so the prompt body never has to
    repeat it. Returns None on timeout (and posts a timeout message
    referencing `timeout_cmd` if provided), or on /cancel (silently —
    the /cancel command itself acks the user).
    """
    has_distinct_current = bool(current) and current != default
    pre_filled = current if has_distinct_current else default

    class KeepOrChangeDefaultView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_TIMEOUT)
            self.value     = None
            self.confirmed = False

            # Build buttons explicitly so we can vary the layout based on
            # whether `current` differs from `default`. Decorator-based
            # buttons can't be conditionally added.
            keep_label = (
                f"✅ Keep current: {current}"[:80]
                if has_distinct_current else
                f"✅ Use default: {default}"[:80]
            )
            keep_btn = discord.ui.Button(label=keep_label, style=discord.ButtonStyle.success)

            async def _keep_cb(inter: discord.Interaction):
                chosen         = current if has_distinct_current else default
                self.value     = chosen
                self.confirmed = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content=f"✅ Using **{chosen}**", view=self
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
                    self.value     = default
                    self.confirmed = True
                    for item in self.children: item.disabled = True
                    await inter.response.edit_message(
                        content=f"✅ Reverted to default: **{default}**", view=self
                    )
                    self.stop()
                revert_btn.callback = _revert_cb
                self.add_item(revert_btn)

            change_btn = discord.ui.Button(
                label="✏️ Define my own", style=discord.ButtonStyle.secondary,
            )

            async def _change_cb(inter: discord.Interaction):
                modal = TextInputModal(modal_title, modal_label, default=pre_filled)
                await inter.response.send_modal(modal)
                await modal.wait()
                self.value     = (modal.value or pre_filled).strip() or pre_filled
                self.confirmed = True
                for item in self.children: item.disabled = True
                try:
                    await inter.message.edit(
                        content=f"✅ Using **{self.value}**", view=self
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
            await channel.send(f"⏰ Timed out. Run `/{timeout_cmd}` to start again.")
        return None
    return view.value


async def _manage_train_templates(
    *, bot, channel, check, existing: list, default_name: str,
    cap: int | None, cancel_event,
):
    """
    Multi-template manager for /setup_train.

    Lets the user view, add, edit, delete, and re-pick the default for the
    guild's saved ChatGPT prompt templates. `cap` is the per-tier maximum
    (None = unlimited / premium).

    Returns (templates_list, default_template_name) — or (None, None) if
    the user timed out. Templates are stored as `[{"name", "template"}, ...]`.
    """
    import wizard_registry

    templates: list[dict] = list(existing) if existing else []
    if not templates:
        templates = [{"name": "Default", "template": ""}]

    # Default name must always reference an existing template.
    if not any(t.get("name") == default_name for t in templates):
        default_name = templates[0]["name"]

    while True:
        cap_label = "unlimited" if cap is None else str(cap)
        listing   = []
        for i, t in enumerate(templates):
            star = " ⭐" if t["name"] == default_name else ""
            preview = (t.get("template") or "").strip().split("\n")[0][:60]
            preview_suffix = f" — *{preview}*" if preview else " — *(empty)*"
            listing.append(f"`{i+1}.` **{t['name']}**{star}{preview_suffix}")

        embed = discord.Embed(
            title="**Step 6 of 7 — Prompt Templates**",
            description=(
                "Saved ChatGPT prompt templates. The default ⭐ is the one used "
                "by the blurb wizard unless a member's day overrides it.\n\n"
                + "\n".join(listing)
                + f"\n\n*Slot usage: **{len(templates)} of {cap_label}**.*"
            ),
            color=discord.Color.blurple(),
        )

        class TemplateListView(discord.ui.View):
            def __init__(self, count: int, at_cap: bool):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.action: str | None = None
                self.index: int | None  = None
                if at_cap:
                    self.add_btn.disabled = True
                if count <= 1:
                    self.delete_btn.disabled = True
                if count == 0:
                    self.edit_btn.disabled        = True
                    self.set_default_btn.disabled = True
                    self.done_btn.disabled        = True

            @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.success, row=0)
            async def add_btn(self, inter, button):
                self.action = "add"
                for c in self.children: c.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=0)
            async def edit_btn(self, inter, button):
                self.action = "edit"
                for c in self.children: c.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="⭐ Set Default", style=discord.ButtonStyle.secondary, row=0)
            async def set_default_btn(self, inter, button):
                self.action = "default"
                for c in self.children: c.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=1)
            async def delete_btn(self, inter, button):
                self.action = "delete"
                for c in self.children: c.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=1)
            async def done_btn(self, inter, button):
                self.action = "done"
                for c in self.children: c.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        list_view = TemplateListView(
            count=len(templates),
            at_cap=cap is not None and len(templates) >= cap,
        )
        await channel.send(embed=embed, view=list_view)
        await wait_view_or_cancel(list_view, cancel_event)
        if list_view.cancelled:
            return None, None

        if list_view.action is None:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return None, None

        if list_view.action == "done":
            return templates, default_name

        # ── Pick which template (for edit/default/delete) ─────────────────────
        picked_idx = None
        if list_view.action in ("edit", "default", "delete"):
            class PickView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=WIZARD_TIMEOUT)
                    self.idx = None
                    options = [
                        discord.SelectOption(label=t["name"][:100], value=str(i))
                        for i, t in enumerate(templates)
                    ]
                    sel = discord.ui.Select(placeholder="Pick a template…", options=options)
                    async def _cb(inter):
                        self.idx = int(sel.values[0])
                        for c in self.children: c.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()
                    sel.callback = _cb
                    self.add_item(sel)

            pick = PickView()
            await channel.send("Which template?", view=pick)
            await wait_view_or_cancel(pick, cancel_event)
            if pick.cancelled:
                return None, None
            if pick.idx is None:
                await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
                return None, None
            picked_idx = pick.idx

        if list_view.action == "delete":
            removed = templates.pop(picked_idx)
            if not templates:
                templates = [{"name": "Default", "template": ""}]
                default_name = "Default"
                await channel.send(
                    f"🗑️ Removed **{removed['name']}**. (Restored an empty Default — "
                    f"you need at least one template.)"
                )
            else:
                if removed["name"] == default_name:
                    default_name = templates[0]["name"]
                await channel.send(f"🗑️ Removed **{removed['name']}**.")
            continue

        if list_view.action == "default":
            default_name = templates[picked_idx]["name"]
            await channel.send(f"⭐ Default set to **{default_name}**.")
            continue

        # ── Add or Edit: collect a name + template body ────────────────────────
        existing_t = templates[picked_idx] if list_view.action == "edit" else None
        is_edit    = existing_t is not None

        await channel.send(
            f"**Template name** *(short label)*"
            + (f" — *editing* `{existing_t['name']}`" if is_edit else "")
            + "\nReply with a name (e.g. `Birthday`, `Welcome`, `Default`)."
            + " Reply `cancel` to abort."
        )
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=300),
            cancel_event,
        )
        if reply is None:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return None, None
        new_name = reply.content.strip()
        if new_name.lower() == "cancel" or not new_name:
            continue
        new_name = new_name[:50]
        # Reject duplicate names (except when editing the same entry).
        for j, t in enumerate(templates):
            if t["name"].lower() == new_name.lower() and not (is_edit and j == picked_idx):
                await channel.send(f"⚠️ A template named **{new_name}** already exists. Try a different name.")
                new_name = None
                break
        if new_name is None:
            continue

        await channel.send(
            "**Template body**\n"
            "Paste the full ChatGPT prompt. Use these placeholders:\n"
            "• `{name}` — the member's name\n"
            "• `{theme}` — the selected theme\n"
            "• `{tone}` — the selected tone\n"
            "• `{notes}` — any notes stored for this member\n"
            "*Reply `cancel` to abort, `keep` to keep the current body.*"
        )
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=600),
            cancel_event,
        )
        if reply is None:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return None, None
        body_raw = reply.content.strip()
        if body_raw.lower() == "cancel":
            continue
        if body_raw.lower() == "keep" and is_edit:
            new_body = existing_t["template"]
        else:
            new_body = body_raw

        if is_edit:
            old_name = existing_t["name"]
            templates[picked_idx] = {"name": new_name, "template": new_body}
            if default_name == old_name:
                default_name = new_name
            await channel.send(f"✅ Updated **{new_name}**.")
        else:
            templates.append({"name": new_name, "template": new_body})
            await channel.send(f"✅ Added **{new_name}** ({len(templates)} of {cap_label}).")


# ── Define Various Setup Commands ────────────────────────────────────────────────────────────────────────

def _has_leadership_or_admin(interaction: discord.Interaction) -> bool:
    """
    True if the invoking user is a server administrator OR has the
    configured leadership role. Used by per-feature /setup_* wizards so
    that day-to-day leadership members can configure features without
    needing full server-admin permissions.
    """
    if interaction.user.guild_permissions.administrator:
        return True
    cfg = get_config(interaction.guild_id)
    if cfg and cfg.leadership_role_name:
        if cfg.leadership_role_name in [r.name for r in interaction.user.roles]:
            return True
    return False


# Permissions required for the bot to drive a wizard end-to-end in a
# given channel: read the channel, send messages, embed (for setup
# summaries), and read history (so wait_for("message") works for the
# typed-reply steps).
_WIZARD_REQUIRED_PERMS = (
    "view_channel",
    "send_messages",
    "embed_links",
    "read_message_history",
)


def _missing_wizard_perms(interaction: discord.Interaction) -> list[str]:
    """Return the list of human-readable permission names the bot is
    missing in `interaction.channel`. Empty list = the bot can drive a
    wizard here. Used as a pre-flight check at the top of /setup_*
    commands so the user gets a clear "I need these permissions in
    this channel" error instead of the generic "Something went wrong"
    that the global error handler would otherwise show after the
    wizard's first channel.send fails with 403.
    """
    me = interaction.guild.me if interaction.guild else None
    if me is None:
        return []   # DM context — wizards aren't supported in DMs anyway
    channel = interaction.channel
    if channel is None:
        return []
    perms   = channel.permissions_for(me)
    missing = []
    for name in _WIZARD_REQUIRED_PERMS:
        if not getattr(perms, name, False):
            # Convert "send_messages" → "Send Messages" for the user-facing
            # message — Discord's UI uses Title Case.
            missing.append(name.replace("_", " ").title())
    return missing


async def _check_wizard_can_run(interaction: discord.Interaction, command_name: str) -> bool:
    """If the bot can run a wizard in the current channel, return True.
    Otherwise send a clear ephemeral message explaining what perms are
    missing (and how to fix), and return False. Call at the top of
    every /setup_* command.
    """
    missing = _missing_wizard_perms(interaction)
    if not missing:
        return True

    perm_lines = "\n".join(f"• **{p}**" for p in missing)
    channel_mention = (
        interaction.channel.mention if interaction.channel and hasattr(interaction.channel, "mention")
        else "this channel"
    )
    msg = (
        f"⚠️ **I can't run `/{command_name}` in {channel_mention}** — I'm missing the "
        f"following Discord permissions in this channel:\n\n"
        f"{perm_lines}\n\n"
        f"To fix this, either:\n"
        f"• Edit this channel's permissions and grant my role those permissions, or\n"
        f"• Run `/{command_name}` from a channel where I already have them (your leadership "
        f"channel is a good choice).\n\n"
        f"Once that's done, the wizard will work."
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        # If even this fails (e.g., the bot can't send ephemeral
        # responses for some reason), let the global error handler
        # take over — but log it so we know.
        print(f"[SETUP] Could not deliver permission-check message for /{command_name}")
    return False


class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="setup", description="Configure Alliance Helper for your server")
    async def setup(self, interaction: discord.Interaction):
        # Only admins can run setup
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can run `/setup`.", ephemeral=True
            )
            return

        if not await _check_wizard_can_run(interaction, "setup"):
            return

        await interaction.response.send_message(
            "⚙️ Starting setup — check the channel for prompts!", ephemeral=True
        )
        await run_setup(interaction, self.bot)

    @app_commands.command(name="view_configuration", description="View all configured settings across every setup wizard")
    async def view_configuration(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can view configuration.", ephemeral=True
            )
            return

        cfg = get_config(interaction.guild_id)
        if not cfg or not cfg.setup_complete:
            await interaction.response.send_message(
                "⚙️ This server hasn't been set up yet. Run `/setup` to get started.",
                ephemeral=True,
            )
            return

        await _send_view_configuration(interaction, cfg)

    @app_commands.command(name="setup_reset", description="Clear this server's configuration and start over")
    async def setup_reset(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Only server administrators can reset the configuration.", ephemeral=True
            )
            return

        class ConfirmResetView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.confirmed = False

            @discord.ui.button(label="Yes, reset everything", style=discord.ButtonStyle.danger)
            async def confirm(self, inner: discord.Interaction, button: discord.ui.Button):
                self.confirmed = True
                await inner.response.defer()
                self.stop()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def cancel(self, inner: discord.Interaction, button: discord.ui.Button):
                await inner.response.defer()
                self.stop()

        view = ConfirmResetView()
        await interaction.response.send_message(
            "⚠️ Are you sure you want to reset the bot configuration for this server? "
            "This cannot be undone.",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.confirmed:
            from config import save_config, GuildConfig
            save_config(GuildConfig(guild_id=interaction.guild_id))
            await interaction.followup.send(
                "✅ Configuration reset. Run `/setup` to configure the bot again.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "✅ Reset cancelled. Your configuration is still active and has not been reset.",
                ephemeral=True,
            )

    @app_commands.command(name="setup_train", description="Configure the train schedule — tab, themes, tones, and prompt template")
    async def setup_train(self, interaction: discord.Interaction):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/setup_train`.",
                ephemeral=True,
            )
            return
        if not await _check_wizard_can_run(interaction, "setup_train"):
            return
        await interaction.response.send_message(
            "⚙️ Starting train setup — check the channel for prompts!", ephemeral=True
        )
        await run_train_setup(interaction, self.bot)

    @app_commands.command(name="setup_growth", description="Configure growth tracking — source tab, metrics, and snapshot frequency")
    async def setup_growth(self, interaction: discord.Interaction):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/setup_growth`.",
                ephemeral=True,
            )
            return
        if not await _check_wizard_can_run(interaction, "setup_growth"):
            return
        await interaction.response.send_message(
            "⚙️ Starting growth tracking setup — check the channel for prompts!", ephemeral=True
        )
        await run_growth_setup(interaction, self.bot)

    @app_commands.command(name="setup_birthdays", description="Configure birthday tracking — sheet tab, columns, and lookahead days")
    async def setup_birthdays(self, interaction: discord.Interaction):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/setup_birthdays`.",
                ephemeral=True,
            )
            return
        if not await _check_wizard_can_run(interaction, "setup_birthdays"):
            return
        await interaction.response.send_message(
            "⚙️ Starting birthday setup — check the channel for prompts!", ephemeral=True
        )
        await run_birthday_setup(interaction, self.bot)

    @app_commands.command(name="setup_desertstorm", description="Configure Desert Storm mail template and time options")
    async def setup_desertstorm(self, interaction: discord.Interaction):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/setup_desertstorm`.",
                ephemeral=True,
            )
            return
        if not await _check_wizard_can_run(interaction, "setup_desertstorm"):
            return
        await interaction.response.send_message(
            "⚙️ Starting Desert Storm setup — check the channel for prompts!", ephemeral=True
        )
        await run_storm_setup(interaction, self.bot, "DS")

    @app_commands.command(name="setup_canyonstorm", description="Configure Canyon Storm mail template and time options")
    async def setup_canyonstorm(self, interaction: discord.Interaction):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/setup_canyonstorm`.",
                ephemeral=True,
            )
            return
        if not await _check_wizard_can_run(interaction, "setup_canyonstorm"):
            return
        await interaction.response.send_message(
            "⚙️ Starting Canyon Storm setup — check the channel for prompts!", ephemeral=True
        )
        await run_storm_setup(interaction, self.bot, "CS")

    @app_commands.command(name="setup_events", description="Add or edit an event type for announcements (Marauder, Siege, etc.)")
    async def setup_events(self, interaction: discord.Interaction):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/setup_events`.",
                ephemeral=True,
            )
            return
        if not await _check_wizard_can_run(interaction, "setup_events"):
            return
        await interaction.response.send_message(
            "⚙️ Starting event setup — check the channel for prompts!", ephemeral=True
        )
        await run_event_setup(interaction, self.bot)

    @app_commands.command(name="setup_survey", description="Configure the default survey — channels, tabs, intro, and questions")
    async def setup_survey(self, interaction: discord.Interaction):
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to run `/setup_survey`.",
                ephemeral=True,
            )
            return
        if not await _check_wizard_can_run(interaction, "setup_survey"):
            return
        await interaction.response.send_message(
            "⚙️ Starting survey setup — check the channel for prompts!", ephemeral=True
        )
        await run_survey_setup(interaction, self.bot)


# ── /Define Various Setup Commands ───────────────────────────────────────────────────────

# Common timezones for the selector
# ── Timezone configuration ─────────────────────────────────────────────────────
# Format: (tz_database_name, display_label)
# Labels show (UTC offset, timezone name, and example cities
# Note: offsets shown are standard time — DST-observing zones shift +1 in summer

TIMEZONE_OPTIONS = [
    ("Pacific/Honolulu",                  "(UTC-10) Hawaii (Honolulu)"),
    ("America/Anchorage",                 "(UTC-9) Alaska (Anchorage)"),
    ("America/Los_Angeles",               "(UTC-8) Pacific (Los Angeles, Seattle, Vancouver)"),
    ("America/Denver",                    "(UTC-7) Mountain (Denver, Phoenix, Calgary)"),
    ("America/Chicago",                   "(UTC-6) Central (Chicago, Dallas, Mexico City)"),
    ("America/New_York",                  "(UTC-5) Eastern (New York, Toronto, Miami)"),
    ("America/Sao_Paulo",                 "(UTC-3) Brazil (São Paulo, Rio de Janeiro)"),
    ("America/Argentina/Buenos_Aires",    "(UTC-3) Argentina (Buenos Aires)"),
    ("Atlantic/Azores",                   "(UTC-1) Azores"),
    ("Europe/London",                     "(UTC+0) GMT/BST (London, Dublin, Lisbon)"),
    ("Europe/Paris",                      "(UTC+1) Central European (Paris, Berlin, Rome)"),
    ("Europe/Helsinki",                   "(UTC+2) Eastern European (Helsinki, Athens, Cairo)"),
    ("Europe/Moscow",                     "(UTC+3) Moscow (Moscow, Istanbul, Riyadh)"),
    ("Asia/Dubai",                        "(UTC+4) Gulf (Dubai, Abu Dhabi)"),
    ("Asia/Karachi",                      "(UTC+5) Pakistan (Karachi, Islamabad)"),
    ("Asia/Kolkata",                      "(UTC+5:30) India (Mumbai, Delhi, Bangalore)"),
    ("Asia/Dhaka",                        "(UTC+6) Bangladesh (Dhaka)"),
    ("Asia/Bangkok",                      "(UTC+7) Indochina (Bangkok, Jakarta, Hanoi)"),
    ("Asia/Shanghai",                     "(UTC+8) China/Singapore (Shanghai, Beijing, Singapore)"),
    ("Asia/Tokyo",                        "(UTC+9) Japan/Korea (Tokyo, Seoul)"),
    ("Australia/Sydney",                  "(UTC+10) Eastern Australia (Sydney, Melbourne)"),
    ("Pacific/Auckland",                  "(UTC+12) New Zealand (Auckland, Wellington)"),
]

# Map from tz_database_name → display label
TIMEZONE_LABELS = {tz: label for tz, label in TIMEZONE_OPTIONS}


class TimezoneSelectView(discord.ui.View):
    """Single dropdown covering all supported timezones, ordered by (UTC offset."""
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected  = None
        self.confirmed = False

        select = discord.ui.Select(
            placeholder="Select your timezone...",
            options=[
                discord.SelectOption(label=label[:100], value=tz)
                for tz, label in TIMEZONE_OPTIONS
            ],
            row=0,
        )

        async def _cb(interaction: discord.Interaction):
            self.selected    = select.values[0]
            self.confirmed   = True
            select.disabled  = True
            label = TIMEZONE_LABELS.get(self.selected, self.selected)
            await interaction.response.edit_message(
                content=f"✅ Timezone: **{label}**", view=self
            )
            self.stop()

        select.callback = _cb
        self.add_item(select)


class ScheduleTypeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.selected  = None

    @discord.ui.button(label="🔁 Repeating cycle", style=discord.ButtonStyle.primary)
    async def repeating(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "repeating"
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="✅ Schedule: **Repeating cycle**", view=self)
        self.stop()

    @discord.ui.button(label="📅 Add manually each time", style=discord.ButtonStyle.secondary)
    async def manual(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "manual"
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="✅ Schedule: **Manual (add per event)**", view=self)
        self.stop()


class YesNoView(discord.ui.View):
    def __init__(self, yes_label="Yes", no_label="No"):
        super().__init__(timeout=120)
        self.selected = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = False
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


# ── /view_configuration helper ───────────────────────────────────────────────

async def _send_view_configuration(interaction: discord.Interaction, cfg) -> None:
    """Build and send a single embed summarising every wizard's configuration."""
    await interaction.response.defer(ephemeral=True)

    from config import (
        get_train_config, get_birthday_config, get_storm_config,
        get_survey_config, get_growth_config, get_guild_events,
    )
    guild_id = interaction.guild_id
    train    = get_train_config(guild_id)
    birthday = get_birthday_config(guild_id)
    ds       = get_storm_config(guild_id, "DS")
    cs       = get_storm_config(guild_id, "CS")
    survey   = get_survey_config(guild_id)
    growth   = get_growth_config(guild_id)
    events   = get_guild_events(guild_id, active_only=True)
    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction)

    def _yn(v) -> str:
        return "✅ Configured" if v else "❌ Not configured"

    def _enabled(v) -> str:
        return "✅ Enabled" if v else "❌ Disabled"

    def _channel(v) -> str:
        return f"<#{v}>" if v else "*not set*"

    def _col_letter(idx) -> str:
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            return "*not set*"
        return chr(65 + idx) if 0 <= idx <= 25 else str(idx)

    tier_badge = "💎 Premium" if is_premium_flag else "Free tier"
    embed = discord.Embed(
        title=f"⚙️ Current Configuration  ·  {tier_badge}",
        description="All configured settings across the bot's setup wizards.",
        color=discord.Color.gold() if is_premium_flag else discord.Color.blurple(),
    )

    tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
    sheet_id_display = f"`{cfg.spreadsheet_id[:25]}...`" if cfg.spreadsheet_id else "*not set*"
    core_lines = [
        f"**Tier:** {tier_badge}",
        f"**Member Role:** {cfg.member_role_name}",
        f"**Leadership Role:** {cfg.leadership_role_name}",
        f"**Leadership Channel:** {_channel(cfg.leadership_channel_id)}",
        f"**Announcement Channel:** {_channel(cfg.announcement_channel_id)}",
        f"**Timezone:** {tz_label}",
        f"**Spreadsheet ID:** {sheet_id_display}",
        f"**Member Tab:** {cfg.tab_member_default}",
    ]
    embed.add_field(name="🛠️ Core", value="\n".join(core_lines)[:1024], inline=False)

    ev_lines = [
        f"**Draft Channel:** {_channel(cfg.event_draft_channel_id)}",
        f"**Announcement Channel:** {_channel(cfg.event_announce_channel_id)}",
        f"**Draft Time:** {cfg.event_draft_time}",
        f"**5-Min Warning:** {_enabled(cfg.event_five_min_warning)}",
    ]
    if events:
        ev_lines.append(f"**Events ({len(events)}):**")
        for e in events:
            ev_lines.append(
                f"• {e['name']} (`{e['short_key']}`) — {e['default_time']} {e['timezone']} · "
                f"blurb {_yn(e.get('announcement_blurb'))}"
            )
    else:
        ev_lines.append("**Events:** *none configured*")
    embed.add_field(name="📣 Events", value="\n".join(ev_lines)[:1024], inline=False)

    train_lines = [
        f"**Schedule Tab:** {train.get('tab_name', '*not set*')}",
        f"**Blurbs:** {_enabled(train.get('blurbs_enabled'))}",
    ]
    if train.get("blurbs_enabled"):
        themes = train.get("themes") or []
        tones  = train.get("tones")  or []
        train_lines.append(f"**Themes ({len(themes)}):** " + (", ".join(themes) if themes else "*none*"))
        train_lines.append(f"**Tones ({len(tones)}):** "  + (", ".join(tones)  if tones  else "*none*"))
        train_lines.append(f"**Default Tone:** {train.get('default_tone', '*not set*')}")
        train_lines.append(f"**Prompt Template:** {_yn(train.get('prompt_template'))}")
    train_lines.append(f"**Reminders:** {_enabled(train.get('reminders_enabled'))}")
    if train.get("reminders_enabled"):
        train_lines.append(f"**Reminder Channel:** {_channel(train.get('reminder_channel_id'))}")
        train_lines.append(f"**Reminder Time:** {train.get('reminder_time', '*not set*')}")
    embed.add_field(name="🚂 Train", value="\n".join(train_lines)[:1024], inline=False)

    b_lines = [
        f"**Enabled:** {_enabled(birthday.get('enabled'))}",
        f"**Source Tab:** {birthday.get('tab_name', '*not set*')}",
        f"**Name Column:** {_col_letter(birthday.get('name_col'))}",
        f"**Birthday Column:** {_col_letter(birthday.get('birthday_col'))}",
        f"**Discord ID Column:** "
        + (_col_letter(birthday.get('discord_id_col'))
           if birthday.get('discord_id_col', -1) >= 0 else "*not set*"),
        f"**Data Start Row:** {birthday.get('data_start_row', '*not set*')}",
        f"**Lookahead Days:** {birthday.get('lookahead_days', '*not set*')}",
        f"**Train Integration:** {_enabled(birthday.get('train_integration'))}",
        f"**Reminders:** {_enabled(birthday.get('reminders_enabled'))}",
    ]
    if birthday.get("reminders_enabled"):
        b_lines.append(f"**Reminder Channel:** {_channel(birthday.get('reminder_channel_id'))}")
        b_lines.append(f"**Reminder Time:** {birthday.get('reminder_time', '*not set*')}")
    embed.add_field(name="🎂 Birthdays", value="\n".join(b_lines)[:1024], inline=False)

    ds_lines = [
        f"**Sheet Tab:** {ds.get('tab_name', '*not set*')}",
        f"**Log Channel:** {_channel(cfg.ds_log_channel_id)}",
        f"**Time Option 1:** {ds.get('time_option_1_label') or '*not set*'} "
        f"({ds.get('time_option_1_local') or '?'} local / {ds.get('time_option_1_server') or '?'} server)",
        f"**Time Option 2:** {ds.get('time_option_2_label') or '*not set*'} "
        f"({ds.get('time_option_2_local') or '?'} local / {ds.get('time_option_2_server') or '?'} server)",
        f"**Mail Template:** {_yn(ds.get('mail_template'))}",
    ]
    embed.add_field(name="⚔️ Desert Storm", value="\n".join(ds_lines)[:1024], inline=False)

    cs_lines = [
        f"**Sheet Tab:** {cs.get('tab_name', '*not set*')}",
        f"**Log Channel:** {_channel(cfg.cs_log_channel_id)}",
        f"**Time Option 1:** {cs.get('time_option_1_label') or '*not set*'} "
        f"({cs.get('time_option_1_local') or '?'} local / {cs.get('time_option_1_server') or '?'} server)",
        f"**Time Option 2:** {cs.get('time_option_2_label') or '*not set*'} "
        f"({cs.get('time_option_2_local') or '?'} local / {cs.get('time_option_2_server') or '?'} server)",
        f"**Mail Template:** {_yn(cs.get('mail_template'))}",
    ]
    embed.add_field(name="🏜️ Canyon Storm", value="\n".join(cs_lines)[:1024], inline=False)

    s_lines = [
        f"**Survey Channel:** {_channel(cfg.survey_channel_id)}",
        f"**Notify Channel:** {_channel(cfg.survey_notify_channel_id)}",
        f"**Stats Tab:** {survey.get('tab_squad_powers', '*not set*')}",
        f"**History Tab:** {survey.get('tab_history', '*not set*')}",
        f"**Questions:** {len(survey.get('questions') or [])}",
        f"**Intro Message:** {_yn(survey.get('intro_message'))}",
    ]
    embed.add_field(name="📋 Survey", value="\n".join(s_lines)[:1024], inline=False)

    g_lines = [f"**Enabled:** {_enabled(growth.get('enabled'))}"]
    if growth.get("enabled"):
        metrics = growth.get("metrics") or []
        freq    = growth.get("snapshot_frequency", "monthly")
        sched   = (
            f"Monthly on day {growth.get('snapshot_day', 1)}"
            if freq == "monthly"
            else f"Every {growth.get('snapshot_interval', 30)} days"
        )
        g_lines += [
            f"**Source Tab:** {growth.get('tab_source', '*not set*')}",
            f"**Name Column:** {growth.get('name_col', '*not set*')}",
            f"**Data Start Row:** {growth.get('data_start_row', '*not set*')}",
            f"**Growth Tab:** {growth.get('tab_growth', '*not set*')}",
            f"**Snapshot Schedule:** {sched}",
            f"**Metrics ({len(metrics)}):** "
            + (", ".join(f"{m['label']} (col {m['col']})" for m in metrics) if metrics else "*none*"),
        ]
    embed.add_field(name="📈 Growth", value="\n".join(g_lines)[:1024], inline=False)

    if is_premium_flag:
        embed.set_footer(text="💎 Premium is active. Run any /setup_* command to update a section.")
    else:
        embed.set_footer(text="Run /upgrade for Premium • /help for all commands • /setup_* to update a section")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Run Various Setups ───────────────────────────────────────────────────────

async def run_setup(interaction: discord.Interaction, bot):
    import wizard_registry
    guild_id = interaction.guild_id
    cfg      = get_or_create_config(guild_id)
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    # ── If already configured, show summary and offer edit or cancel ──────────
    if cfg.setup_complete:
        tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
        existing_embed = discord.Embed(
            title="⚙️ Current Core Setup",
            description="Your server is already configured. Would you like to edit these settings?",
            color=discord.Color.blurple(),
        )
        existing_embed.add_field(name="Member Role",        value=cfg.member_role_name,              inline=False)
        existing_embed.add_field(name="Leadership Role",    value=cfg.leadership_role_name,          inline=False)
        existing_embed.add_field(name="Leadership Channel", value=f"<#{cfg.leadership_channel_id}>", inline=False)
        existing_embed.add_field(name="Timezone",           value=tz_label,                          inline=False)
        existing_embed.add_field(name="Sheet ID",           value=f"`{cfg.spreadsheet_id[:20]}...`" if cfg.spreadsheet_id else "Not set", inline=False)

        class EditOrCancelView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.proceed = None

            @discord.ui.button(label="✏️ Edit settings", style=discord.ButtonStyle.primary)
            async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
                self.proceed = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✅ No changes needed", style=discord.ButtonStyle.secondary)
            async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
                self.proceed = False
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        eoc_view = EditOrCancelView()
        await channel.send(embed=existing_embed, view=eoc_view)
        await wait_view_or_cancel(eoc_view, cancel_event)
        if eoc_view.cancelled:
            return
        if not eoc_view.proceed:
            await channel.send("✅ No changes made. Your existing setup is still active.")
            return

    await channel.send(
        "⚙️ **Alliance Helper Setup**\n\n"
        "I'll walk you through the core configuration for your server. "
        "This covers your roles, leadership channel, timezone and Google Sheet.\n\n"
        "*You can run `/setup` again at any time to update these settings.*"
    )

    # ── Step 1: Member role ────────────────────────────────────────────────────
    await channel.send("**Step 1 of 6 — Member Role**\nSelect the role that all alliance members have:")
    v = RoleSelectStep("Select member role...")
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.member_role_name = v.selected_role.name
    cfg.member_role_id   = v.selected_role.id

    # ── Step 2: Leadership role ────────────────────────────────────────────────
    await channel.send("**Step 2 of 6 — Leadership Role**\nSelect the elevated role for alliance leadership:")
    v = RoleSelectStep("Select leadership role...")
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.leadership_role_name = v.selected_role.name

    # ── Step 3: Leadership channel ─────────────────────────────────────────────
    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction)
    await channel.send(
        "**Step 3 of 6 — Leadership Channel**\n"
        "Select the private channel where leadership commands will be used:"
    )
    v = ChannelSelectStep(
        "Select leadership channel...",
        suggested_name="leadership",
        include_threads=is_premium_flag,
        guild=interaction.guild,
    )
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.leadership_channel_id  = v.selected_channel.id
    cfg.leadership_category_id = getattr(v.selected_channel, "category_id", 0) or 0

    # ── Step 4: Timezone ───────────────────────────────────────────────────────
    tz_view = TimezoneSelectView()
    await channel.send(
        "**Step 4 of 6 — Timezone**\n"
        "Select your alliance's timezone. This is used for displaying event times, "
        "Desert Storm/Canyon Storm times, and train reminders throughout the bot:"
    )
    await channel.send("\u200b", view=tz_view)
    await wait_view_or_cancel(tz_view, cancel_event)
    if tz_view.cancelled:
        return
    if not tz_view.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    cfg.timezone = tz_view.selected

    # ── Step 5: Google Sheet ID ────────────────────────────────────────────────
    await channel.send(
        "**Step 5 of 6 — Google Sheet ID**\n"
        "Enter your Google Sheet ID — the long string from your sheet's URL:\n"
        "`https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID`**`/edit`"
    )
    modal   = TextInputModal("Google Sheet ID", "Sheet ID", placeholder="Paste your Sheet ID here...")
    modal_v = ModalLaunchView(modal)
    await channel.send("\u200b", view=modal_v)
    await wait_view_or_cancel(modal_v, cancel_event)
    if modal_v.cancelled:
        return
    if not modal_v.confirmed:
        await channel.send("⏰ Setup timed out. Run `/setup` to start again.")
        return
    sheet_id = modal.value

    # ── Step 6: Share sheet ────────────────────────────────────────────────────
    SERVICE_ACCOUNT_EMAIL = "sheet-connector@lw-alliance-helper.iam.gserviceaccount.com"
    sharing_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#sharing"

    share_embed = discord.Embed(
        title="**Step 6 of 6 — Share Your Google Sheet**",
        description=(
            "Before finishing, you need to give the bot access to your sheet.\n\n"
            "**Follow these steps:**\n"
            "1️⃣ Click the link below to open your sheet's sharing settings\n"
            "2️⃣ Click **Share** in the top right corner\n"
            "3️⃣ Paste the email address below into the share field\n"
            "4️⃣ Set permission to **Editor**\n"
            "5️⃣ Click **Send** — then come back here and confirm"
        ),
        color=discord.Color.yellow(),
    )
    share_embed.add_field(
        name="📋 Service Account Email (click to copy)",
        value=f"`{SERVICE_ACCOUNT_EMAIL}`",
        inline=False,
    )
    share_embed.add_field(
        name="🔗 Open Your Sheet",
        value=f"[Click here to open sharing settings]({sharing_url})",
        inline=False,
    )
    done_view = ConfirmView()
    done_view.children[0].label = "✅ I've shared the sheet"
    done_view.children[1].label = "❌ Cancel setup"
    await channel.send(embed=share_embed, view=done_view)
    await wait_view_or_cancel(done_view, cancel_event)
    if done_view.cancelled:
        return
    if not done_view.confirmed:
        await channel.send("❌ Setup cancelled. Run `/setup` to start again.")
        return

    # ── Confirm and save ───────────────────────────────────────────────────────
    tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
    embed = discord.Embed(
        title="✅ Final Review — Confirm to Save",
        description=(
            "All steps complete. Review your selections below and click "
            "**Confirm** to save your configuration, or **Cancel** to start over.\n"
            "*(This is the final review, not an additional step.)*"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Member Role",        value=cfg.member_role_name,              inline=False)
    embed.add_field(name="Leadership Role",    value=cfg.leadership_role_name,          inline=False)
    embed.add_field(name="Leadership Channel", value=f"<#{cfg.leadership_channel_id}>", inline=False)
    embed.add_field(name="Timezone",           value=tz_label,                          inline=False)
    embed.add_field(name="Sheet ID",           value=f"`{sheet_id[:20]}...`",           inline=False)

    confirm_view = ConfirmView()
    await channel.send(embed=embed, view=confirm_view)
    await wait_view_or_cancel(confirm_view, cancel_event)
    if confirm_view.cancelled:
        return
    if not confirm_view.confirmed:
        await channel.send("❌ Setup cancelled. Run `/setup` to start again.")
        return

    cfg.setup_complete = True
    cfg.spreadsheet_id = sheet_id
    save_config(cfg)

    await channel.send(
        "✅ **Core setup complete!**\n\n"
        "Now configure the features you want to use. Run each of the commands below for any feature you'd like to enable:\n\n"
        "📣 `/setup_events` — Event announcements (Plague Marauder, Zombie Siege, etc.)\n"
        "🚂 `/setup_train` — Train schedule, blurb generation, and reminders\n"
        "🎂 `/setup_birthdays` — Birthday tracking and announcements\n"
        "⚔️ `/setup_desertstorm` — Desert Storm mail drafts and participation logs\n"
        "🏜️ `/setup_canyonstorm` — Canyon Storm mail drafts and participation logs\n"
        "📋 `/setup_survey` — Squad powers survey\n"
        "📈 `/setup_growth` — Growth tracking (snapshot your members' stats over time)\n\n"
        "You can set up as many or as few of these as you need. Use `/help` at any time to see all available commands."
    )
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Guild {guild_id} core setup complete")

async def run_growth_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring growth tracking."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    from config import get_growth_config, save_growth_config
    current = get_growth_config(guild_id)

    # Hardcoded defaults — what the bot ships with. These are passed as
    # `default=` to ask_keep_or_change. The user's previously-saved value
    # (if any) is passed as `current=` so the wizard can label it
    # accurately ("Keep current: X" vs "Use default: Y") instead of
    # showing every saved value as the "default".
    DEFAULT_TAB_SOURCE        = "Squad Powers"
    DEFAULT_DATA_START_ROW    = 2
    DEFAULT_NAME_COL          = "A"
    DEFAULT_TAB_GROWTH        = "Growth Tracking"
    DEFAULT_SNAPSHOT_DAY      = 1
    DEFAULT_SNAPSHOT_INTERVAL = 30

    await channel.send(
        "⚙️ **Growth Tracking Setup**\n"
        "Configure how the bot tracks your alliance's growth over time. "
        "Each month (or on your chosen schedule), the bot takes a snapshot of your members' stats "
        "and records them in your Google Sheet so you can track progress."
    )

    # ── Step 1: Enable? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        "**Step 1 of 7 — Enable growth tracking?**\n"
        "Should the bot automatically take snapshots of your members' stats on a schedule?",
        view=enabled_view,
    )
    await wait_view_or_cancel(enabled_view, cancel_event)
    if enabled_view.cancelled:
        return
    if enabled_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
        return
    if not enabled_view.selected:
        save_growth_config(
            guild_id, enabled=0,
            tab_source=current.get("tab_source", ""),
            name_col=current.get("name_col", "A"),
            metrics=current.get("metrics", []),
            tab_growth=current.get("tab_growth", "Growth Tracking"),
            snapshot_frequency=current.get("snapshot_frequency", "monthly"),
            snapshot_day=current.get("snapshot_day", 1),
            snapshot_interval=current.get("snapshot_interval", 30),
            data_start_row=current.get("data_start_row", 2),
        )
        await channel.send("✅ Growth tracking disabled.")
        return

    # ── Step 2: Source tab ────────────────────────────────────────────────────
    tab_source = await ask_keep_or_change(
        channel,
        "**Step 2 of 7 — Source Tab**\n"
        "Which tab in your Google Sheet contains your member data?\n"
        "⚠️ *Make sure this tab exists in your sheet.*",
        default=DEFAULT_TAB_SOURCE,
        current=current.get("tab_source", ""),
        modal_title="Source Tab",
        modal_label="Tab name",
        timeout_cmd="setup_growth",
        cancel_event=cancel_event,
    )
    if tab_source is None:
        return

    # ── Step 3: Data start row ────────────────────────────────────────────────
    start_raw = await ask_keep_or_change(
        channel,
        "**Step 3 of 7 — Data Start Row**\n"
        "Which row does your member data start on? (Row 1 is usually the header)",
        default=str(DEFAULT_DATA_START_ROW),
        current=str(current.get("data_start_row") or ""),
        modal_title="Data Start Row",
        modal_label="Row number",
        timeout_cmd="setup_growth",
        cancel_event=cancel_event,
    )
    if start_raw is None:
        return
    try:
        data_start_row = int(str(start_raw).strip())
    except ValueError:
        await channel.send("⚠️ Please enter a row number like `2`. Run `/setup_growth` to try again.")
        return

    # ── Step 4: Name column ───────────────────────────────────────────────────
    name_raw = await ask_keep_or_change(
        channel,
        "**Step 4 of 7 — Name Column**\n"
        "Which column contains the member's name?",
        default=DEFAULT_NAME_COL,
        current=current.get("name_col", ""),
        modal_title="Name Column",
        modal_label="Column letter",
        timeout_cmd="setup_growth",
        cancel_event=cancel_event,
    )
    if name_raw is None:
        return
    name_col = name_raw.strip().upper()
    if len(name_col) != 1 or not name_col.isalpha():
        await channel.send("⚠️ Please enter a single column letter like `A`. Run `/setup_growth` to try again.")
        return

    # ── Step 5: Metrics ───────────────────────────────────────────────────────
    metrics = list(current.get("metrics", []))

    class MetricModal(discord.ui.Modal):
        def __init__(self, label_default: str = "", col_default: str = ""):
            super().__init__(title="Metric")
            self.label_value = None
            self.col_value = None
            self._label_input = discord.ui.TextInput(
                label="Label",
                placeholder="e.g. 1st Squad Power, THP, Total Kills",
                default=label_default,
                required=True,
                max_length=100,
            )
            self._col_input = discord.ui.TextInput(
                label="Column letter",
                placeholder="e.g. E",
                default=col_default,
                required=True,
                max_length=2,
            )
            self.add_item(self._label_input)
            self.add_item(self._col_input)

        async def on_submit(self, interaction: discord.Interaction):
            self.label_value = self._label_input.value.strip()
            self.col_value = self._col_input.value.strip().upper()
            await interaction.response.defer()
            self.stop()

    def _metrics_embed(cap: int | None = None) -> discord.Embed:
        embed = discord.Embed(
            title="📊 Step 5 of 7 — Metrics to Track",
            description=(
                "Define which columns the bot should snapshot each period. "
                "Add as many as you want — for example a `1st Squad Power` column, `THP`, `Total Kills`, etc."
            ),
            color=discord.Color.blurple(),
        )
        if metrics:
            for m in metrics:
                embed.add_field(name=m["label"], value=f"Column {m['col']}", inline=False)
        else:
            embed.add_field(name="No metrics yet", value="Click **Add Metric** to begin.", inline=False)
        if cap is not None:
            embed.set_footer(text=f"Free tier: {len(metrics)} of {cap} metrics used. Upgrade to Premium for unlimited.")
        return embed

    while True:
        # Free-tier cap on number of growth metrics
        metrics_cap = await premium.get_limit("growth_metrics", guild_id, interaction=interaction)
        at_metrics_cap = metrics_cap is not None and len(metrics) >= metrics_cap

        class MetricsActionView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.choice = None
                if not metrics:
                    self.edit_btn.disabled = True
                    self.delete_btn.disabled = True
                    self.done_btn.disabled = True
                if at_metrics_cap:
                    self.add_btn.disabled = True

            @discord.ui.button(label="➕ Add Metric", style=discord.ButtonStyle.success, row=0)
            async def add_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                modal = MetricModal()
                await inter.response.send_modal(modal)
                await modal.wait()
                if modal.label_value and modal.col_value and modal.col_value.isalpha():
                    metrics.append({"label": modal.label_value, "col": modal.col_value})
                self.choice = "loop"
                self.stop()

            @discord.ui.button(label="✏️ Edit Metric", style=discord.ButtonStyle.primary, row=0)
            async def edit_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "edit"
                self.stop()
                await inter.response.defer()

            @discord.ui.button(label="🗑️ Delete Metric", style=discord.ButtonStyle.danger, row=0)
            async def delete_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "delete"
                self.stop()
                await inter.response.defer()

            @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.secondary, row=1)
            async def done_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "done"
                self.stop()
                await inter.response.defer()

        action_view = MetricsActionView()
        await channel.send(embed=_metrics_embed(cap=metrics_cap), view=action_view)
        await wait_view_or_cancel(action_view, cancel_event)
        if action_view.cancelled:
            return

        if action_view.choice is None:
            await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
            return
        if action_view.choice == "done":
            break
        if action_view.choice == "loop":
            continue

        if action_view.choice in ("edit", "delete") and not metrics:
            continue

        # Pick which metric to edit/delete
        class PickMetricView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.index = None
                options = [
                    discord.SelectOption(
                        label=m["label"][:100],
                        value=str(i),
                        description=f"Column {m['col']}",
                    )
                    for i, m in enumerate(metrics)
                ]
                self.select = discord.ui.Select(
                    placeholder="Choose a metric...",
                    options=options,
                    min_values=1, max_values=1,
                )
                self.select.callback = self._on_select
                self.add_item(self.select)

            async def _on_select(self, inter: discord.Interaction):
                self.index = int(self.select.values[0])
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        pick_view = PickMetricView()
        verb = "edit" if action_view.choice == "edit" else "delete"
        await channel.send(f"Which metric do you want to {verb}?", view=pick_view)
        await wait_view_or_cancel(pick_view, cancel_event)
        if pick_view.cancelled:
            return
        if pick_view.index is None:
            await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
            return

        if action_view.choice == "delete":
            removed = metrics.pop(pick_view.index)
            await channel.send(f"🗑️ Removed: **{removed['label']}** (column {removed['col']})")
            continue

        # Edit: open a modal pre-filled with the chosen metric
        existing = metrics[pick_view.index]

        class EditLaunchView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.modal = MetricModal(
                    label_default=existing["label"], col_default=existing["col"]
                )
                self.confirmed = False

            @discord.ui.button(label="✏️ Edit values", style=discord.ButtonStyle.primary)
            async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
                await inter.response.send_modal(self.modal)
                await self.modal.wait()
                self.confirmed = True
                self.stop()

        edit_launch = EditLaunchView()
        await channel.send(
            f"Editing **{existing['label']}** (column {existing['col']}). Click below to update.",
            view=edit_launch,
        )
        await wait_view_or_cancel(edit_launch, cancel_event)
        if edit_launch.cancelled:
            return
        if edit_launch.modal.label_value and edit_launch.modal.col_value and edit_launch.modal.col_value.isalpha():
            metrics[pick_view.index] = {
                "label": edit_launch.modal.label_value,
                "col":   edit_launch.modal.col_value,
            }

    if not metrics:
        await channel.send("⚠️ No metrics defined. Run `/setup_growth` to try again.")
        return

    # ── Step 6: Growth tracking tab ───────────────────────────────────────────
    tab_growth = await ask_keep_or_change(
        channel,
        "**Step 6 of 7 — Growth Tracking Tab**\n"
        "Which tab should snapshots be written to?\n"
        "⚠️ *If the tab doesn't exist, the bot will create it automatically.*",
        default=DEFAULT_TAB_GROWTH,
        current=current.get("tab_growth", ""),
        modal_title="Growth Tracking Tab",
        modal_label="Tab name",
        timeout_cmd="setup_growth",
        cancel_event=cancel_event,
    )
    if tab_growth is None:
        return

    # ── Step 7: Snapshot frequency ────────────────────────────────────────────
    # Custom-interval frequency is a premium-only feature.
    custom_interval_unlocked = await premium.is_premium(guild_id, interaction=interaction)

    class FrequencyView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_TIMEOUT)
            self.selected = None
            if not custom_interval_unlocked:
                self.custom.disabled = True

        @discord.ui.button(label="📅 Monthly (1st of each month)", style=discord.ButtonStyle.primary)
        async def monthly(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "monthly"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Frequency: **Monthly**", view=self)
            self.stop()

        @discord.ui.button(label="🔁 Custom interval (every X days) 💎", style=discord.ButtonStyle.secondary)
        async def custom(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "interval"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(view=self)
            self.stop()

    freq_view = FrequencyView()
    freq_prompt = (
        "**Step 7 of 7 — Snapshot Frequency**\n"
        "How often should the bot take a snapshot?"
    )
    if not custom_interval_unlocked:
        freq_prompt += "\n*🔒 Custom interval is a Premium feature.*"
    await channel.send(
        freq_prompt,
        view=freq_view,
    )
    await wait_view_or_cancel(freq_view, cancel_event)
    if freq_view.cancelled:
        return
    if not freq_view.selected:
        await channel.send("⏰ Timed out. Run `/setup_growth` to start again.")
        return

    snapshot_frequency = freq_view.selected
    snapshot_day       = DEFAULT_SNAPSHOT_DAY
    snapshot_interval  = DEFAULT_SNAPSHOT_INTERVAL

    if snapshot_frequency == "monthly":
        day_raw = await ask_keep_or_change(
            channel,
            "**Step 7a of 7 — Snapshot Day**\n"
            "Which day of the month should the snapshot run? (1–28)",
            default=str(DEFAULT_SNAPSHOT_DAY),
            current=str(current.get("snapshot_day") or ""),
            modal_title="Snapshot Day",
            modal_label="Day of month (1–28)",
            timeout_cmd="setup_growth",
            cancel_event=cancel_event,
        )
        if day_raw is None:
            return
        try:
            snapshot_day = max(1, min(28, int(str(day_raw).strip())))
        except ValueError:
            snapshot_day = DEFAULT_SNAPSHOT_DAY
    else:
        interval_raw = await ask_keep_or_change(
            channel,
            "**Step 7a of 7 — Interval (days)**\n"
            "How many days between each snapshot?",
            default=str(DEFAULT_SNAPSHOT_INTERVAL),
            current=str(current.get("snapshot_interval") or ""),
            modal_title="Interval",
            modal_label="Days between snapshots",
            timeout_cmd="setup_growth",
            cancel_event=cancel_event,
        )
        if interval_raw is None:
            return
        try:
            snapshot_interval = max(1, int(str(interval_raw).strip()))
        except ValueError:
            snapshot_interval = DEFAULT_SNAPSHOT_INTERVAL

    # ── Save ───────────────────────────────────────────────────────────────────
    save_growth_config(
        guild_id, enabled=1,
        tab_source=tab_source, name_col=name_col,
        metrics=metrics, tab_growth=tab_growth,
        snapshot_frequency=snapshot_frequency,
        snapshot_day=snapshot_day,
        snapshot_interval=snapshot_interval,
        data_start_row=data_start_row,
    )

    freq_desc  = (
        f"Monthly on day {snapshot_day}"
        if snapshot_frequency == "monthly"
        else f"Every {snapshot_interval} days"
    )
    metrics_display = "\n".join(f"• **{m['label']}** — column {m['col']}" for m in metrics)

    # Compute when the very first snapshot will fire under this config so
    # the user isn't left guessing "OK now what?" when they pick a custom
    # interval — picking 14 days doesn't tell them whether the first
    # snapshot is today, tomorrow, or 14 days from now.
    from growth import compute_next_snapshot
    next_dt = compute_next_snapshot({
        "enabled": 1,
        "snapshot_frequency": snapshot_frequency,
        "snapshot_day": snapshot_day,
        "snapshot_interval": snapshot_interval,
    })
    if next_dt is not None:
        ts = int(next_dt.timestamp())
        # Discord renders <t:N:F> as a localized full date/time per viewer
        # and <t:N:R> as a relative "in 3 days" string — the combo gives
        # leadership a clear answer regardless of their personal timezone.
        next_value = (
            f"<t:{ts}:F> (<t:{ts}:R>)\n"
            f"*Want to start tracking from today instead? "
            f"Run `/growth` and click **📸 Run Snapshot Now**.*"
        )
    else:
        next_value = "*Could not compute — check `/growth` for status.*"

    embed = discord.Embed(title="✅ Growth Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Source Tab",        value=tab_source,           inline=False)
    embed.add_field(name="Name Column",       value=f"Column {name_col}", inline=False)
    embed.add_field(name="Data Start Row",    value=str(data_start_row),  inline=False)
    embed.add_field(name="Growth Tab",        value=tab_growth,           inline=False)
    embed.add_field(name="Snapshot Schedule", value=freq_desc,            inline=False)
    embed.add_field(name="Next Snapshot",     value=next_value,           inline=False)
    embed.add_field(name="Metrics",           value=metrics_display,      inline=False)
    embed.set_footer(text="Run /setup_growth again to update. Use /growth to take a manual snapshot.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Growth config saved for guild {guild_id}")

async def run_train_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the train schedule."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 2000):
        """Send prompt and wait for typed reply. Both stay visible."""
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=300),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    from config import get_train_config
    current = get_train_config(guild_id)

    await channel.send(
        "⚙️ **Train Schedule Setup**\n"
        "*Configure how the train schedule works for your alliance.*"
    )

    # ── Step 1: Sheet tab ──────────────────────────────────────────────────────
    tab_name = await ask_keep_or_change(
        channel,
        "**Step 1 of 8 — Schedule Sheet Tab**\n"
        "Which tab in your Google Sheet stores the train schedule?\n"
        "⚠️ *Make sure this tab exists in your sheet before continuing.*",
        default="Train Schedule",
        current=current.get("tab_name", ""),
        modal_title="Sheet Tab Name",
        modal_label="Tab name",
        timeout_cmd="setup_train",
        cancel_event=cancel_event,
    )
    if tab_name is None:
        return

    # ── Step 2: Generate blurbs? ───────────────────────────────────────────────
    blurb_view = YesNoView()
    await channel.send(
        "**Step 2 of 8 — ChatGPT Blurb Generation**\n"
        "Would you like the bot to help generate a ChatGPT prompt each day when you assign a train?\n"
        "This lets you quickly produce a personalised announcement blurb for the member.\n"
        "*(You can always set this up later by running `/setup_train` again)*",
        view=blurb_view,
    )
    await wait_view_or_cancel(blurb_view, cancel_event)
    if blurb_view.cancelled:
        return
    if blurb_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
        return
    blurbs_enabled = 1 if blurb_view.selected else 0
    if not blurbs_enabled:
        await channel.send(
            "ℹ️ *Skipping Steps 3–6 (themes, tones, default tone, prompt template) — "
            "blurb generation is off.*"
        )

    themes        = current["themes"]
    tones         = current["tones"]
    default_tone  = current["default_tone"]
    prompt_template = current.get("prompt_template", "")

    # Free-tier slot caps for themes / tones (None = unlimited).
    # Also used by the reminder-channel step to expose threads on premium.
    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction)
    themes_cap = await premium.get_limit("themes", guild_id, interaction=interaction)
    tones_cap  = await premium.get_limit("tones",  guild_id, interaction=interaction)

    def _trim(values: list[str], cap: int | None) -> tuple[list[str], bool]:
        """Trim list to cap. Returns (trimmed_list, was_truncated)."""
        if cap is None or len(values) <= cap:
            return values, False
        return values[:cap], True

    if blurbs_enabled:
        # ── Step 3: Themes ─────────────────────────────────────────────────────
        # Apply cap up-front so the "defaults" preview matches what'll actually save.
        cap_capped_themes, _ = _trim(list(current["themes"]), themes_cap)
        existing_themes      = ", ".join(cap_capped_themes)
        cap_note_themes      = (
            f"\n*Free tier: up to {themes_cap} themes. Upgrade for unlimited.*"
            if themes_cap is not None else ""
        )

        class KeepOrChangeView(discord.ui.View):
            def __init__(self, label: str):
                super().__init__(timeout=120)
                self.keep_existing = None
                self._label = label

            @discord.ui.button(label="✅ Use defaults", style=discord.ButtonStyle.success)
            async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
                self.keep_existing = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content=f"✅ Using defaults for {self._label}.", view=self
                )
                self.stop()

            @discord.ui.button(label="✏️ Define my own", style=discord.ButtonStyle.secondary)
            async def change(self, inter: discord.Interaction, button: discord.ui.Button):
                self.keep_existing = False
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        themes_keep_view = KeepOrChangeView("themes")
        await channel.send(
            f"**Step 3 of 8 — Themes**\n"
            f"These appear as options when selecting a theme for a member's train day.\n\n"
            f"**Defaults:**\n`{existing_themes}`"
            + cap_note_themes,
            view=themes_keep_view,
        )
        await wait_view_or_cancel(themes_keep_view, cancel_event)
        if themes_keep_view.cancelled:
            return
        if themes_keep_view.keep_existing is None:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return

        if themes_keep_view.keep_existing:
            themes = cap_capped_themes
        else:
            themes_raw = await ask_text("Enter your themes as a comma-separated list:")
            if themes_raw is None:
                return
            entered = [t.strip() for t in themes_raw.split(",") if t.strip()] or current["themes"]
            themes, truncated = _trim(entered, themes_cap)
            if truncated:
                await channel.send(
                    f"ℹ️ Free tier: only the first {themes_cap} themes were saved "
                    f"(`{', '.join(themes)}`). Upgrade to Premium to save more."
                )

        # ── Step 4: Tones ──────────────────────────────────────────────────────
        cap_capped_tones, _ = _trim(list(current["tones"]), tones_cap)
        existing_tones      = ", ".join(cap_capped_tones)
        cap_note_tones      = (
            f"\n*Free tier: up to {tones_cap} tones. Upgrade for unlimited.*"
            if tones_cap is not None else ""
        )

        tones_keep_view = KeepOrChangeView("tones")
        await channel.send(
            f"**Step 4 of 8 — Tones**\n"
            f"These let leadership adjust the writing style of the generated blurb.\n\n"
            f"**Defaults:**\n`{existing_tones}`"
            + cap_note_tones,
            view=tones_keep_view,
        )
        await wait_view_or_cancel(tones_keep_view, cancel_event)
        if tones_keep_view.cancelled:
            return
        if tones_keep_view.keep_existing is None:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return

        if tones_keep_view.keep_existing:
            tones = cap_capped_tones
        else:
            tones_raw = await ask_text("Enter your tones as a comma-separated list:")
            if tones_raw is None:
                return
            entered = [t.strip() for t in tones_raw.split(",") if t.strip()] or current["tones"]
            tones, truncated = _trim(entered, tones_cap)
            if truncated:
                await channel.send(
                    f"ℹ️ Free tier: only the first {tones_cap} tones were saved "
                    f"(`{', '.join(tones)}`). Upgrade to Premium to save more."
                )

        # ── Step 5: Default tone ───────────────────────────────────────────────
        class ToneDefaultView(discord.ui.View):
            def __init__(self, tone_list: list):
                super().__init__(timeout=120)
                self.selected = None
                select = discord.ui.Select(
                    placeholder="Select default tone...",
                    options=[discord.SelectOption(label=t, value=t) for t in tone_list],
                )
                async def _cb(inter: discord.Interaction):
                    self.selected = select.values[0]
                    select.disabled = True
                    await inter.response.edit_message(
                        content=f"✅ Default tone: **{self.selected}**", view=self
                    )
                    self.stop()
                select.callback = _cb
                self.add_item(select)

        tone_default_view = ToneDefaultView(tones)
        await channel.send(
            f"**Step 5 of 8 — Default Tone**\n"
            f"Which tone should be pre-selected by default?",
            view=tone_default_view,
        )
        await wait_view_or_cancel(tone_default_view, cancel_event)
        if tone_default_view.cancelled:
            return
        if not tone_default_view.selected:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return
        default_tone = tone_default_view.selected

        # ── Step 6: Prompt templates ───────────────────────────────────────────
        # Free tier keeps a single "Default" template; premium can save up to
        # `template_cap` named templates and pick which is the default.
        template_cap     = await premium.get_limit("train_templates", guild_id, interaction=interaction)
        existing_templates = list(current.get("templates") or [])
        if not existing_templates:
            existing_templates = [{"name": "Default", "template": prompt_template or ""}]
        default_template_name = current.get("default_template") or existing_templates[0]["name"]

        templates, default_template_name = await _manage_train_templates(
            bot=bot, channel=channel, check=check,
            existing=existing_templates, default_name=default_template_name,
            cap=template_cap, cancel_event=cancel_event,
        )
        if templates is None:
            return  # timed out / cancelled
        prompt_template = next(
            (t["template"] for t in templates if t.get("name") == default_template_name),
            templates[0]["template"] if templates else "",
        )

    # ── Step 7: Reminders ─────────────────────────────────────────────────────
    reminder_view = YesNoView()
    await channel.send(
        "**Step 7 of 8 — Train Reminders**\n"
        "Should the bot post a reminder to leadership when someone is assigned the train each day?",
        view=reminder_view,
    )
    await wait_view_or_cancel(reminder_view, cancel_event)
    if reminder_view.cancelled:
        return
    if reminder_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
        return
    reminders_enabled  = 1 if reminder_view.selected else 0
    reminder_channel_id = 0
    reminder_time       = "22:00"
    if not reminders_enabled:
        await channel.send(
            "ℹ️ *Skipping Steps 7a–7b (reminder channel and time) — train reminders are off.*"
        )

    if reminders_enabled:
        # ── Step 7a: Reminder channel ──────────────────────────────────────────
        reminder_ch_view = ChannelSelectStep(
            "Select the reminder channel...",
            suggested_name="leadership",
            include_threads=is_premium_flag,
            guild=interaction.guild,
        )
        await channel.send(
            "**Step 7a of 8 — Reminder Channel**\n"
            "Which channel should the train reminder be posted to?",
            view=reminder_ch_view,
        )
        await wait_view_or_cancel(reminder_ch_view, cancel_event)
        if reminder_ch_view.cancelled:
            return
        if not reminder_ch_view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_train` to start again.")
            return
        reminder_channel_id = reminder_ch_view.selected_channel.id

        # ── Step 7b: Reminder time ─────────────────────────────────────────────
        # Re-prompt up to 3 times on unparseable input rather than silently
        # falling back to a default — gives leadership a chance to correct
        # a typo without restarting the whole wizard.
        from config import get_config
        guild_cfg = get_config(guild_id)
        tz_label  = TIMEZONE_LABELS.get(guild_cfg.timezone if guild_cfg else "America/New_York", "ET")
        attempts_left = 3
        reminder_time = "22:00"
        while True:
            time_raw = await ask_keep_or_change(
                channel,
                f"**Step 7b of 8 — Reminder Time**\n"
                f"What time should the reminder fire? *(in your timezone: {tz_label})*\n"
                f"*(e.g. `10:00pm`, `9:00am`)*",
                default="10:00pm",
                current=current.get("reminder_time", ""),
                modal_title="Reminder Time",
                modal_label="Time",
                timeout_cmd="setup_train",
                cancel_event=cancel_event,
            )
            if time_raw is None:
                return
            parsed = _parse_12h_time(time_raw)
            if parsed:
                reminder_time = parsed
                break
            if (len(time_raw) == 5 and time_raw[2] == ":"
                    and time_raw.replace(":", "").isdigit()):
                reminder_time = time_raw  # already 24h
                break
            attempts_left -= 1
            if attempts_left <= 0:
                await channel.send(
                    "⚠️ Could not read that time after a few tries. "
                    "Run `/setup_train` to start over."
                )
                return
            await channel.send(
                f"⚠️ Could not read **`{time_raw}`** as a time. "
                f"Try `10:00pm`, `9:00am`, or `22:00`. Let's try once more."
            )

    # ── Step 8: Train DM body (💎 Premium) ────────────────────────────────────
    # Customisable body of the DM that fires alongside the channel
    # reminder when the assigned member is on Premium. Free guilds can
    # configure now — it just won't fire until they upgrade + sync the
    # member roster.
    train_dm_message = ""
    if reminders_enabled:
        from train_cog import DEFAULT_TRAIN_DM
        saved_train_dm = (current.get("dm_message") or "").strip()
        train_dm_input = await ask_keep_or_change(
            channel,
            "**Step 8 of 8 — Train DM Body (💎 Premium)**\n"
            "When the train reminder fires, the bot also DMs the assigned member directly. "
            "Free guilds can configure it now — it just won't fire until you have Premium "
            "+ Member Roster Sync.\n\n"
            "Use `{name}` as a placeholder for the member's name (optional).",
            default=DEFAULT_TRAIN_DM,
            current=saved_train_dm,
            modal_title="Train DM Body",
            modal_label="DM body (max 1000 chars)",
            timeout_cmd="setup_train",
            cancel_event=cancel_event,
        )
        if train_dm_input is None:
            return
        # Match the "Use default" UX everywhere else: keep the DB column
        # empty when the user picked the default, so future tweaks to the
        # hardcoded text reach existing alliances automatically.
        train_dm_message = "" if train_dm_input == DEFAULT_TRAIN_DM else train_dm_input

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_train_config
    save_kwargs = dict(
        blurbs_enabled=blurbs_enabled,
        reminders_enabled=reminders_enabled,
        reminder_channel_id=reminder_channel_id,
        reminder_time=reminder_time,
        dm_message=train_dm_message,
    )
    if blurbs_enabled:
        save_kwargs["templates"]        = templates
        save_kwargs["default_template"] = default_template_name
    save_train_config(
        guild_id, tab_name, themes, tones, prompt_template, default_tone,
        **save_kwargs,
    )

    embed = discord.Embed(title="✅ Train Schedule Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",       value=tab_name,                        inline=True)
    embed.add_field(name="Blurb Generation",value="Enabled" if blurbs_enabled else "Disabled", inline=True)
    embed.add_field(name="Reminders",       value="Enabled" if reminders_enabled else "Disabled", inline=True)
    if reminders_enabled:
        embed.add_field(name="Reminder Channel", value=f"<#{reminder_channel_id}>", inline=True)
        embed.add_field(name="Reminder Time",    value=reminder_time,               inline=True)
    if blurbs_enabled:
        embed.add_field(name="Default Tone", value=default_tone,          inline=True)
        embed.add_field(name="Themes",       value=", ".join(themes),     inline=False)
        embed.add_field(name="Tones",        value=", ".join(tones),      inline=False)
        template_count = len(templates) if 'templates' in locals() else 0
        if template_count > 0:
            template_names = ", ".join(t["name"] for t in templates)
            embed.add_field(
                name=f"Templates ({template_count})",
                value=f"`{template_names}` — default: **{default_template_name}**",
                inline=False,
            )
        if prompt_template:
            preview = prompt_template[:200] + ("..." if len(prompt_template) > 200 else "")
            embed.add_field(name="Default Template Preview", value=f"```{preview}```", inline=False)
    embed.set_footer(text="Run /setup_train again to update any of these settings.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Train config saved for guild {guild_id}")

async def run_create_new_extra_survey(interaction: discord.Interaction, bot):
    """
    Premium-only: ask leadership for a display name for a new extra survey,
    derive a unique slug for `survey_id`, then dispatch to
    `run_survey_setup` to walk through the standard wizard. Called by the
    `[➕ Add Survey]` button on `/survey` for premium guilds.
    """
    import re as _re
    from config import list_surveys

    channel = interaction.channel
    user    = interaction.user

    def check(m):
        return m.author == user and m.channel == channel

    await channel.send(
        "💎 **Add a Survey**\n"
        "Type a short display name for the new survey (e.g. `Off-Season Powers` "
        "or `Recruit Intake`). This is what leadership and members will see."
    )
    try:
        name_reply = await bot.wait_for("message", check=check, timeout=180)
    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out. Click **➕ Add Survey** on `/survey` again to retry.")
        return

    survey_name = (name_reply.content or "").strip()[:60]
    if not survey_name:
        await channel.send("⚠️ Empty name — aborting. Click **➕ Add Survey** on `/survey` to try again.")
        return

    extras = [s for s in list_surveys(interaction.guild_id)
              if (s.get("survey_id") or "default") != "default"]
    base_slug    = _re.sub(r"[^a-z0-9]+", "-", survey_name.lower()).strip("-") or "survey"
    existing_ids = {s.get("survey_id") for s in extras}
    survey_id    = base_slug
    suffix       = 2
    while survey_id in existing_ids:
        survey_id = f"{base_slug}-{suffix}"
        suffix   += 1

    await channel.send(
        f"✅ Creating new survey **{survey_name}** (id: `{survey_id}`).\n"
        f"Walking you through the same setup steps as `/setup_survey`…"
    )
    await run_survey_setup(
        interaction, bot,
        target_survey_id=survey_id,
        target_survey_name=survey_name,
    )


async def run_remove_extra_survey(interaction: discord.Interaction, bot):
    """
    Premium-only: show a picker of extra surveys and confirm-delete the
    chosen one. Called by the `[🗑️ Remove Survey]` button on `/survey`
    for premium guilds.
    """
    from config import list_surveys, delete_extra_survey

    extras = [s for s in list_surveys(interaction.guild_id)
              if (s.get("survey_id") or "default") != "default"]
    if not extras:
        await interaction.followup.send(
            "*You have no extra surveys to remove.* "
            "Click **➕ Add Survey** on `/survey` to add one.",
            ephemeral=True,
        )
        return

    class _ConfirmRemoveView(discord.ui.View):
        def __init__(self, target: dict):
            super().__init__(timeout=120)
            self.target = target

        @discord.ui.button(label="🗑️ Remove", style=discord.ButtonStyle.danger)
        async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
            ok = delete_extra_survey(interaction.guild_id, self.target["survey_id"])
            msg = (
                f"🗑️ Removed **{self.target.get('survey_name')}**."
                if ok else "⚠️ Could not remove that survey."
            )
            await inter.response.edit_message(content=msg, view=None)
            self.stop()

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
            await inter.response.edit_message(content="❌ Cancelled. No surveys removed.", view=None)
            self.stop()

    class _RemovePickView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            sel = discord.ui.Select(
                placeholder="Pick a survey to remove…",
                options=[
                    discord.SelectOption(
                        label=(s.get("survey_name") or s.get("survey_id"))[:100],
                        value=s.get("survey_id"),
                    ) for s in extras[:25]
                ],
            )
            async def _cb(inter: discord.Interaction):
                sid = sel.values[0]
                picked = next((s for s in extras if s.get("survey_id") == sid), None)
                sel.disabled = True
                name = picked.get("survey_name", sid) if picked else sid
                await inter.response.edit_message(
                    content=f"⚠️ Confirm: remove **{name}**?",
                    view=_ConfirmRemoveView(picked),
                )
                self.stop()
            sel.callback = _cb
            self.add_item(sel)

    await interaction.followup.send(
        "Pick which extra survey to remove:",
        view=_RemovePickView(),
        ephemeral=True,
    )


async def run_pick_survey_to_edit(interaction: discord.Interaction, bot):
    """
    Show a picker covering the default survey + all extras, then dispatch
    into `run_survey_setup` with the chosen target. Called by the
    `[✏️ Edit Survey]` button on `/survey` for premium guilds.
    """
    from config import list_surveys

    surveys = list_surveys(interaction.guild_id)

    class _EditPickView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            sel = discord.ui.Select(
                placeholder="Pick a survey to edit…",
                options=[
                    discord.SelectOption(
                        label=(s.get("survey_name") or s.get("survey_id"))[:100],
                        value=s.get("survey_id") or "default",
                    ) for s in surveys[:25]
                ],
            )
            async def _cb(inter: discord.Interaction):
                sid = sel.values[0]
                target = next((s for s in surveys
                               if (s.get("survey_id") or "default") == sid), None)
                sel.disabled = True
                name = (target.get("survey_name") if target else sid) or sid
                await inter.response.edit_message(content=f"✏️ Editing **{name}**…", view=self)
                self.stop()
                # Dispatch into the wizard. `target_survey_id=None` means
                # the default survey (run_survey_setup edits guild_survey_config).
                target_id = None if sid == "default" else sid
                await run_survey_setup(
                    interaction, bot,
                    target_survey_id=target_id,
                    target_survey_name=(target.get("survey_name") if target else None),
                )
            sel.callback = _cb
            self.add_item(sel)

    await interaction.followup.send(
        "Which survey would you like to edit?",
        view=_EditPickView(),
        ephemeral=True,
    )


async def run_survey_setup(interaction: discord.Interaction, bot,
                           target_survey_id: str | None = None,
                           target_survey_name: str | None = None):
    """
    Walk an admin through configuring a survey.

    `target_survey_id` controls *which* survey is being edited:
      • `None` (default) — edits the guild's main survey
        (`guild_survey_config` row).
      • Any other id  — edits or creates an entry in `guild_extra_surveys`,
        which premium guilds use for additional named surveys.
    """
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    from config import (
        get_survey_config, save_survey_config,
        get_survey, save_extra_survey,
    )
    from defaults import DEFAULT_SURVEY_QUESTIONS

    if target_survey_id is None:
        current = get_survey_config(guild_id)
        wizard_label = "Survey Setup"
    else:
        current = get_survey(guild_id, target_survey_id) or {}
        # Carry the existing name through so we can preserve it on save.
        if not target_survey_name:
            target_survey_name = current.get("survey_name") or target_survey_id
        wizard_label = f"Survey Setup — {target_survey_name}"
    questions = list(current.get("questions") or [])

    await channel.send(
        f"⚙️ **{wizard_label}**\n"
        "Configure the survey for your alliance."
    )

    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction)

    # ── Step 1: Survey channel ─────────────────────────────────────────────────
    survey_ch_view = ChannelSelectStep(
        "Select the survey channel...",
        suggested_name="squad-survey",
        include_threads=is_premium_flag,
        guild=interaction.guild,
    )
    await channel.send(
        "**Step 1 of 6 — Survey Channel**\n"
        "Select the channel where the survey button will be posted for members to access:",
        view=survey_ch_view,
    )
    await wait_view_or_cancel(survey_ch_view, cancel_event)
    if survey_ch_view.cancelled:
        return
    if not survey_ch_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return
    survey_channel_id = survey_ch_view.selected_channel.id

    # ── Step 2: Survey notification channel ───────────────────────────────────
    notify_ch_view = ChannelSelectStep(
        "Select the survey notification channel...",
        suggested_name="survey-responses",
        include_threads=is_premium_flag,
        guild=interaction.guild,
    )
    await channel.send(
        "**Step 2 of 6 — Survey Notification Channel**\n"
        "Select the channel where leadership will be notified when a member submits the survey:",
        view=notify_ch_view,
    )
    await wait_view_or_cancel(notify_ch_view, cancel_event)
    if notify_ch_view.cancelled:
        return
    if not notify_ch_view.confirmed:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return
    survey_notify_channel_id = notify_ch_view.selected_channel.id

    # ── Step 3: Squad Powers tab ───────────────────────────────────────────────
    tab_squad_powers = await ask_keep_or_change(
        channel,
        "**Step 3 of 6 — Member Statistics Tab**\n"
        "Which tab stores your members' statistics? We will update this sheet on each submission.\n"
        "⚠️ *Make sure this tab exists in your sheet before continuing.*",
        default="Squad Powers",
        current=current.get("tab_squad_powers", ""),
        modal_title="Member Statistics Tab",
        modal_label="Tab name",
        timeout_cmd="setup_survey",
        cancel_event=cancel_event,
    )
    if tab_squad_powers is None:
        return

    # ── Step 4: Survey History tab ─────────────────────────────────────────────
    tab_history = await ask_keep_or_change(
        channel,
        "**Step 4 of 6 — Survey History Tab**\n"
        "Which tab stores the full history of all submissions?\n"
        "⚠️ *Make sure this tab exists in your sheet before continuing.*",
        default="Survey History",
        current=current.get("tab_history", ""),
        modal_title="Survey History Tab",
        modal_label="Tab name",
        timeout_cmd="setup_survey",
        cancel_event=cancel_event,
    )
    if tab_history is None:
        return

    # ── Step 5: Intro message ──────────────────────────────────────────────────
    await channel.send(
        "**Step 5 of 6 — Survey Intro Message**\n"
        "When your survey is posted, what introductory message do you want your members to see "
        "before they take the survey?\n\n"
        "**Example:**\n"
        "*Please fill out this survey each week to help us track squad powers, "
        "balance our teams, and prepare for season events!*"
    )
    intro_reply = await wizard_registry.wait_or_cancel(
        bot.wait_for("message", check=check, timeout=300),
        cancel_event,
    )
    if intro_reply is None:
        if cancel_event.is_set():
            await channel.send("❌ Cancelled.")
        else:
            await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return
    intro_message = intro_reply.content.strip()

    # ── Step 6: Survey Questions ───────────────────────────────────────────────
    # Show default questions and ask keep/edit/scratch
    default_q_list = "\n".join(
        f"{i+1}. **{q['label']}** — {'dropdown: ' + ', '.join(q['options']) if q['type'] == 'dropdown' else 'text'}"
        for i, q in enumerate(DEFAULT_SURVEY_QUESTIONS)
    )
    existing_q_list = "\n".join(
        f"{i+1}. **{q['label']}** — {'dropdown: ' + ', '.join(q['options']) if q['type'] == 'dropdown' else 'text'}"
        for i, q in enumerate(questions)
    ) if questions else "*(no questions configured yet)*"

    class QuestionStartView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.choice = None

        @discord.ui.button(label="✅ Use default questions", style=discord.ButtonStyle.success)
        async def use_default(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "default"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Using default questions.", view=self)
            self.stop()

        @discord.ui.button(label="✏️ Edit existing questions", style=discord.ButtonStyle.primary)
        async def edit_existing(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "edit"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✏️ Entering edit mode...", view=self)
            self.stop()

        @discord.ui.button(label="🔄 Start from scratch", style=discord.ButtonStyle.secondary)
        async def start_scratch(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "scratch"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="🔄 Starting from scratch...", view=self)
            self.stop()

    q_start_view = QuestionStartView()
    await channel.send(
        "**Step 6 of 6 — Survey Questions**\n\n"
        f"**Default questions (Last War):**\n{default_q_list}\n\n"
        f"**Your existing questions:**\n{existing_q_list}\n\n"
        "Would you like to use the defaults, edit your existing questions, or start from scratch?",
        view=q_start_view,
    )
    await wait_view_or_cancel(q_start_view, cancel_event)
    if q_start_view.cancelled:
        return
    if not q_start_view.choice:
        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
        return

    if q_start_view.choice == "default":
        questions = list(DEFAULT_SURVEY_QUESTIONS)

    elif q_start_view.choice in ("edit", "scratch"):
        if q_start_view.choice == "scratch":
            questions = []

        # ── Question builder loop ──────────────────────────────────────────────
        async def build_question_list():
            """Show current question list with Add and Finish buttons."""
            nonlocal questions

            while True:
                # Build display
                if questions:
                    q_display = "\n".join(
                        f"{i+1}. **{q['label']}** — "
                        + ('dropdown: ' + ', '.join(q['options']) if q['type'] == 'dropdown' else 'text')
                        + (f" *(help: {q['placeholder']})*" if q.get('placeholder') else "")
                        for i, q in enumerate(questions)
                    )
                else:
                    q_display = "*(no questions added yet)*"

                class QuestionListView(discord.ui.View):
                    def __init__(self, q_count: int):
                        super().__init__(timeout=300)
                        self.action     = None
                        self.edit_index = None
                        self.del_index  = None

                        if q_count > 0:
                            # Edit dropdown
                            edit_select = discord.ui.Select(
                                placeholder="✏️ Edit a question...",
                                options=[discord.SelectOption(label=f"Edit: {questions[i]['label']}", value=str(i))
                                         for i in range(q_count)],
                                row=0,
                            )
                            async def _edit_cb(inter: discord.Interaction):
                                self.action     = "edit"
                                self.edit_index = int(edit_select.values[0])
                                for item in self.children: item.disabled = True
                                await inter.response.edit_message(view=self)
                                self.stop()
                            edit_select.callback = _edit_cb
                            self.add_item(edit_select)

                            # Delete dropdown
                            del_select = discord.ui.Select(
                                placeholder="🗑️ Delete a question...",
                                options=[discord.SelectOption(label=f"Delete: {questions[i]['label']}", value=str(i))
                                         for i in range(q_count)],
                                row=1,
                            )
                            async def _del_cb(inter: discord.Interaction):
                                self.action    = "delete"
                                self.del_index = int(del_select.values[0])
                                for item in self.children: item.disabled = True
                                await inter.response.edit_message(view=self)
                                self.stop()
                            del_select.callback = _del_cb
                            self.add_item(del_select)

                    @discord.ui.button(label="➕ Add Question", style=discord.ButtonStyle.primary, row=2)
                    async def add_q(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.action = "add"
                        for item in self.children: item.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()

                    @discord.ui.button(label="✅ Finish Survey Setup", style=discord.ButtonStyle.success, row=2)
                    async def finish(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.action = "finish"
                        for item in self.children: item.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()

                list_view = QuestionListView(len(questions))
                await channel.send(
                    f"**Survey Questions:**\n{q_display}",
                    view=list_view,
                )
                await wait_view_or_cancel(list_view, cancel_event)
                if list_view.cancelled:
                    return

                if not list_view.action:
                    await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                    return False

                if list_view.action == "finish":
                    return True

                elif list_view.action == "delete":
                    idx     = list_view.del_index
                    removed = questions.pop(idx)
                    await channel.send(f"🗑️ Removed: **{removed['label']}**")

                elif list_view.action in ("add", "edit"):
                    # ── Question builder ───────────────────────────────────────
                    if list_view.action == "edit":
                        idx      = list_view.edit_index
                        existing = questions[idx]
                        q_num    = f"Question {idx + 1}"
                    else:
                        # Free-tier cap on number of survey questions
                        q_cap = await premium.get_limit("survey_questions", guild_id, interaction=interaction)
                        if q_cap is not None and len(questions) >= q_cap:
                            await channel.send(embed=premium.limit_reached_embed(
                                feature_label="Survey Questions",
                                current=len(questions), cap=q_cap, plural_unit="questions",
                            ))
                            continue
                        existing = {}
                        q_num    = f"Question {len(questions) + 1}"

                    # Label
                    label_extra = f"\n*Existing label:* `{existing.get('label', '')}`" if existing else ""
                    await channel.send(
                        f"**{q_num} — Label**\n"
                        f"What is the label for this question? (e.g. `1st Squad Power`, `Profession`)"
                        + label_extra
                    )
                    try:
                        label_reply = await bot.wait_for("message", check=check, timeout=120)
                        q_label     = label_reply.content.strip() or existing.get("label", "")
                    except asyncio.TimeoutError:
                        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                        return False

                    q_key = q_label.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")

                    # Type — premium subscribers see three additional types.
                    is_premium_for_q = await premium.is_premium(guild_id, interaction=interaction)
                    type_options = [
                        discord.SelectOption(label="Text — member types their answer", value="text"),
                        discord.SelectOption(label="Dropdown — member selects from a list", value="dropdown"),
                    ]
                    if is_premium_for_q:
                        type_options += [
                            discord.SelectOption(label="💎 Numeric — number with min/max validation", value="numeric"),
                            discord.SelectOption(label="💎 Multi-select — pick multiple options",     value="multi_select"),
                            discord.SelectOption(label="💎 Date — formatted date entry",              value="date"),
                        ]
                    _type_pretty = {
                        "text": "Text", "dropdown": "Dropdown",
                        "numeric": "Numeric", "multi_select": "Multi-Select", "date": "Date",
                    }

                    class TypeView(discord.ui.View):
                        def __init__(self):
                            super().__init__(timeout=120)
                            self.selected = None
                            select = discord.ui.Select(
                                placeholder="Select answer type...",
                                options=type_options,
                            )
                            async def _cb(inter: discord.Interaction):
                                self.selected = select.values[0]
                                select.disabled = True
                                await inter.response.edit_message(
                                    content=f"✅ Type: **{_type_pretty.get(self.selected, self.selected)}**",
                                    view=self,
                                )
                                self.stop()
                            select.callback = _cb
                            self.add_item(select)

                    type_view    = TypeView()
                    existing_type = existing.get("type", "text")
                    type_extra   = f"\n*Existing type:* `{existing_type}`" if existing else ""
                    type_prompt  = (
                        f"**{q_num} — Answer Type**\n"
                        + ("Pick how members answer this question." if is_premium_for_q
                           else "Does your member answer by typing or selecting from a dropdown list?")
                        + type_extra
                    )
                    await channel.send(type_prompt, view=type_view)
                    await wait_view_or_cancel(type_view, cancel_event)
                    if type_view.cancelled:
                        return
                    if not type_view.selected:
                        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                        return False
                    q_type = type_view.selected

                    # Help text
                    help_extra = (
                        f"\n*Existing help text:* `{existing.get('placeholder') or 'none'}`"
                        if existing else ""
                    )
                    await channel.send(
                        f"**{q_num} — Help Text**\n"
                        f"Do you want to show help text for this question? "
                        f"This appears as a hint to help members answer correctly.\n"
                        f"*(e.g. `e.g. 43.27` or `What is your first squad's power?`)*\n"
                        f"Type your help text, or type `none` to skip."
                        + help_extra
                    )
                    try:
                        help_reply  = await bot.wait_for("message", check=check, timeout=120)
                        help_raw    = help_reply.content.strip()
                        placeholder = "" if help_raw.lower() == "none" else help_raw
                    except asyncio.TimeoutError:
                        await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                        return False

                    # Type-specific extras
                    options       = []
                    extra_meta    = {}    # numeric min/max, date format, etc.
                    if q_type in ("dropdown", "multi_select"):
                        existing_opts = ", ".join(existing.get("options", [])) if existing else ""
                        opts_extra    = f"\n*Existing options:* `{existing_opts}`" if existing_opts else ""
                        await channel.send(
                            f"**{q_num} — Options**\n"
                            f"Enter the options as comma-separated values. Maximum of 25.\n"
                            f"*(e.g. `Missile, Air, Tank`)*"
                            + opts_extra
                        )
                        try:
                            opts_reply = await bot.wait_for("message", check=check, timeout=120)
                            options    = [o.strip() for o in opts_reply.content.split(",") if o.strip()][:25]
                        except asyncio.TimeoutError:
                            await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                            return False

                    if q_type == "numeric":
                        await channel.send(
                            f"**{q_num} — Numeric Bounds** *(💎 Premium)*\n"
                            f"Reply with `min,max` (e.g. `0,100`), `min,` for only a minimum, "
                            f"`,max` for only a maximum, or `none` to skip both bounds."
                        )
                        try:
                            bounds_reply = await bot.wait_for("message", check=check, timeout=120)
                        except asyncio.TimeoutError:
                            await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                            return False
                        raw = bounds_reply.content.strip().lower()
                        if raw not in ("", "none"):
                            try:
                                lo_s, _, hi_s = raw.partition(",")
                                if lo_s.strip(): extra_meta["min"] = float(lo_s.strip())
                                if hi_s.strip(): extra_meta["max"] = float(hi_s.strip())
                            except ValueError:
                                await channel.send(
                                    "⚠️ Couldn't parse bounds. Run `/setup_survey` to try again."
                                )
                                return False

                    if q_type == "date":
                        existing_fmt = existing.get("date_format") or "%m/%d/%Y"
                        await channel.send(
                            f"**{q_num} — Date Format** *(💎 Premium)*\n"
                            f"Reply with a strptime-style format (e.g. `%m/%d/%Y`, `%Y-%m-%d`), "
                            f"or reply `default` for `%m/%d/%Y`."
                            + (f"\n*Existing format:* `{existing_fmt}`" if existing else "")
                        )
                        try:
                            fmt_reply = await bot.wait_for("message", check=check, timeout=120)
                        except asyncio.TimeoutError:
                            await channel.send("⏰ Timed out. Run `/setup_survey` to start again.")
                            return False
                        raw_fmt = fmt_reply.content.strip()
                        extra_meta["date_format"] = (
                            "%m/%d/%Y" if raw_fmt.lower() in ("", "default") else raw_fmt
                        )

                    new_q = {
                        "key":         q_key,
                        "label":       q_label,
                        "type":        q_type,
                        "options":     options,
                        "placeholder": placeholder,
                        "max_chars":   0,
                        **extra_meta,
                    }

                    if list_view.action == "edit":
                        questions[list_view.edit_index] = new_q
                        await channel.send(f"✅ Updated: **{q_label}**")
                    else:
                        questions.append(new_q)
                        await channel.send(f"✅ Added: **{q_label}** — {len(questions)} question(s) so far.")

        result = await build_question_list()
        if not result:
            return

    if not questions:
        await channel.send("⚠️ No questions defined. Run `/setup_survey` to try again.")
        return

    # ── Save — including channel IDs ───────────────────────────────────────────
    if target_survey_id is None:
        # Default survey: legacy single-row storage, plus the channel IDs go
        # to guild_configs so older code that reads them stays happy.
        save_survey_config(guild_id, tab_squad_powers, tab_history, questions, intro_message)
        from config import update_config_field
        update_config_field(guild_id, "survey_channel_id",        survey_channel_id)
        update_config_field(guild_id, "survey_notify_channel_id", survey_notify_channel_id)
        next_step_cmd = "/setup_survey"
    else:
        # Extra survey: save into guild_extra_surveys; preserve any custom
        # reminder body the leadership previously set.
        save_extra_survey(
            guild_id, target_survey_id,
            survey_name=target_survey_name or target_survey_id,
            tab_squad_powers=tab_squad_powers,
            tab_history=tab_history,
            questions=questions,
            intro_message=intro_message,
            survey_channel_id=survey_channel_id,
            notify_channel_id=survey_notify_channel_id,
            reminder_message=current.get("reminder_message", "") or "",
            reminder_enabled=int(current.get("reminder_enabled") or 0),
        )
        next_step_cmd = "/survey"  # premium UI surfaces edit/remove from there

    q_summary = "\n".join(
        f"• **{q['label']}** — {q['type']}"
        + (f" ({', '.join(q['options'])})" if q['type'] == 'dropdown' else "")
        for q in questions
    )
    title = (
        f"✅ Survey Configured — {target_survey_name}"
        if target_survey_id else "✅ Survey Configured"
    )
    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="Survey Channel",      value=f"<#{survey_channel_id}>",        inline=True)
    embed.add_field(name="Notification Channel",value=f"<#{survey_notify_channel_id}>", inline=True)
    embed.add_field(name="Stats Tab",           value=tab_squad_powers,                  inline=True)
    embed.add_field(name="History Tab",         value=tab_history,                       inline=True)
    embed.add_field(name="Questions",           value=q_summary[:1024],                  inline=False)
    embed.set_footer(
        text=f"Run {next_step_cmd} again to update. Run /survey_post to post the survey button."
    )
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Survey config saved for guild {guild_id} "
          f"(survey_id={target_survey_id or 'default'}) — {len(questions)} questions")

async def run_storm_setup(interaction: discord.Interaction, bot, event_type: str):
    """Shared setup wizard for Desert Storm and Canyon Storm."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    label    = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    cmd_name = "setup_desertstorm" if event_type == "DS" else "setup_canyonstorm"
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 2000):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=300),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    from config import get_storm_config, get_config
    from defaults import DEFAULT_DS_TEMPLATE, DEFAULT_CS_TEMPLATE
    current   = get_storm_config(guild_id, event_type)
    guild_cfg = get_config(guild_id)
    timezone  = guild_cfg.timezone if guild_cfg and guild_cfg.timezone else "America/New_York"
    tz_label  = TIMEZONE_LABELS.get(timezone, timezone)

    # Default template and placeholders per event type
    if event_type == "DS":
        default_template  = DEFAULT_DS_TEMPLATE
        placeholder_info  = (
            "• `{alliance_name}` — your alliance name\n"
            "• `{zones}` — zone assignments block\n"
            "• `{subs}` — substitute members\n"
            "• `{time}` — event time (auto-filled when drafting)"
        )
    else:
        default_template  = DEFAULT_CS_TEMPLATE
        placeholder_info  = (
            "• `{alliance_name}` — your alliance name\n"
            "• `{zones}` — zone assignments block\n"
            "• `{subs}` — substitute members\n"
            "• `{time}` — event time (auto-filled when drafting)"
        )

    await channel.send(f"⚙️ **{label} Setup**")

    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction)

    # ── Step 1: Sheet tab ──────────────────────────────────────────────────────
    hardcoded_tab = "DS Assignments" if event_type == "DS" else "CS Assignments"
    tab_name = await ask_keep_or_change(
        channel,
        f"**Step 1 of 6 — Sheet Tab**\n"
        f"Which tab in your Google Sheet stores the {label} zone assignments?\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*\n"
        f"ℹ️ *The bot will manage the data structure of this tab automatically — "
        f"you don't need to set up any specific columns or formatting beforehand.*",
        default=hardcoded_tab,
        current=current.get("tab_name", ""),
        modal_title="Sheet Tab Name",
        modal_label="Tab name",
        timeout_cmd=cmd_name,
        cancel_event=cancel_event,
    )
    if tab_name is None:
        return

    # ── Step 2: Which teams? ───────────────────────────────────────────────────
    class TeamChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.selected = None

        @discord.ui.button(label="Team A & Team B", style=discord.ButtonStyle.primary)
        async def both(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "both"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Teams: **Team A & Team B**", view=self)
            self.stop()

        @discord.ui.button(label="Team A only", style=discord.ButtonStyle.secondary)
        async def a_only(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "A"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Teams: **Team A only**", view=self)
            self.stop()

        @discord.ui.button(label="Team B only", style=discord.ButtonStyle.secondary)
        async def b_only(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "B"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(content="✅ Teams: **Team B only**", view=self)
            self.stop()

    team_view = TeamChoiceView()
    await channel.send(
        f"**Step 2 of 6 — Which teams do you run for {label}?**",
        view=team_view,
    )
    await wait_view_or_cancel(team_view, cancel_event)
    if team_view.cancelled:
        return
    if not team_view.selected:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return
    teams = team_view.selected

    # ── Step 3: Storm log channel ─────────────────────────────────────────────
    # Reused by /[event]_log lookups and by the participation flow when
    # leadership posts the participation summary.
    log_ch_view = ChannelSelectStep(
        f"Select the {label} log channel...",
        suggested_name="storm-log",
        include_threads=is_premium_flag,
        guild=interaction.guild,
    )
    await channel.send(
        f"**Step 3 of 6 — Storm Log Channel**\n"
        f"Select the channel where {label} participation/log summaries will be posted:",
        view=log_ch_view,
    )
    await wait_view_or_cancel(log_ch_view, cancel_event)
    if log_ch_view.cancelled:
        return
    if not log_ch_view.confirmed:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return
    log_channel_id = log_ch_view.selected_channel.id

    # ── Step 4: Post channel (where /[event]_draft posts the final mail) ─────
    post_ch_view = ChannelSelectStep(
        f"Select the {label} mail post channel...",
        suggested_name=f"{'desert' if event_type == 'DS' else 'canyon'}-storm",
        include_threads=is_premium_flag,
        guild=interaction.guild,
    )
    await channel.send(
        f"**Step 4 of 6 — Mail Post Channel**\n"
        f"When leadership clicks **Post & Copy** at the end of `/"
        f"{'desertstorm' if event_type == 'DS' else 'canyonstorm'}_draft`, "
        f"the finished mail will be posted to this channel:",
        view=post_ch_view,
    )
    await wait_view_or_cancel(post_ch_view, cancel_event)
    if post_ch_view.cancelled:
        return
    if not post_ch_view.confirmed:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return
    post_channel_id = post_ch_view.selected_channel.id

    # ── Step 5: Mail template(s) ───────────────────────────────────────────────

    async def get_template(team_label: str) -> str | None:
        """Get template for one team — show default with use/edit choice."""
        class TemplateChoiceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.use_default = None

            @discord.ui.button(label="✅ Use default template", style=discord.ButtonStyle.success)
            async def use_def(self, inter: discord.Interaction, button: discord.ui.Button):
                self.use_default = True
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content=f"✅ Using default template for {team_label}.", view=self
                )
                self.stop()

            @discord.ui.button(label="✏️ Edit template", style=discord.ButtonStyle.secondary)
            async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
                self.use_default = False
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        choice_view = TemplateChoiceView()
        await channel.send(
            f"**{label} Mail Template — {team_label}**\n"
            f"When you draft the mail each week, you will be able to select the time slot "
            f"when you are running that team's {label}.\n\n"
            f"Here is the default template:\n"
            f"```\n{default_template}\n```\n"
            f"Would you like to use this or edit it?",
            view=choice_view,
        )
        await wait_view_or_cancel(choice_view, cancel_event)
        if choice_view.cancelled:
            return
        if choice_view.use_default is None:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        if choice_view.use_default:
            return default_template

        # User wants to edit — show variables and ask for input
        await channel.send(
            f"Paste your custom template for **{team_label}**. "
            f"You can copy the default above and modify it, or write your own.\n\n"
            f"**Available placeholders:**\n{placeholder_info}\n\n"
            f"*This form will time out in 5 minutes. "
            f"You can run `/{cmd_name}` again if it times out.*"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=300)
            return reply.content.strip() or default_template
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None

    if teams == "both":
        # Ask if one template for both or separate
        class SharedTemplateView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected = None

            @discord.ui.button(label="One template for both teams", style=discord.ButtonStyle.primary)
            async def shared(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = "shared"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content="✅ **One shared template** for Team A & B", view=self
                )
                self.stop()

            @discord.ui.button(label="Separate templates per team", style=discord.ButtonStyle.secondary)
            async def separate(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = "separate"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(
                    content="✅ **Separate templates** for Team A & Team B", view=self
                )
                self.stop()

        shared_view = SharedTemplateView()
        await channel.send(
            "**Step 5 of 6 — Mail Template**\n"
            "Do you want one template that applies to both teams, or separate templates per team?",
            view=shared_view,
        )
        await wait_view_or_cancel(shared_view, cancel_event)
        if shared_view.cancelled:
            return
        if not shared_view.selected:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return

        template_a = await get_template("Team A & B" if shared_view.selected == "shared" else "Team A")
        if template_a is None:
            return
        if shared_view.selected == "separate":
            template_b = await get_template("Team B")
            if template_b is None:
                return
        else:
            template_b = template_a

    else:
        team_label = "Team A" if teams == "A" else "Team B"
        await channel.send("**Step 5 of 6 — Mail Template**")
        template = await get_template(team_label)
        if template is None:
            return
        template_a = template if teams == "A" else ""
        template_b = template if teams == "B" else ""

    # ── Step 6: Participation log tracking (optional) ─────────────────────────
    participation_cfg = await _run_storm_participation_step(
        channel, bot, user, cancel_event,
        guild_id=guild_id, event_type=event_type, label=label, cmd_name=cmd_name,
        is_premium_flag=is_premium_flag, current=current,
    )
    if participation_cfg is None:
        return  # cancelled / timed out

    # ── Step 7: Reminder DM body (💎 Premium) ─────────────────────────────────
    # The body of the DM that fires when leadership runs
    # /[event]_remind. Stored per (guild_id, event_type) so DS and CS
    # can have different copy. Free guilds can configure this now too —
    # it just won't fire until they upgrade.
    from storm_log import DEFAULT_STORM_REMINDER_DM
    default_remind_dm = DEFAULT_STORM_REMINDER_DM.format(label=label)
    saved_remind_dm   = (current.get("dm_reminder_message") or "").strip()
    remind_dm = await ask_keep_or_change(
        channel,
        f"**Step 7 of 7 — {label} Reminder DM (💎 Premium)**\n"
        f"When leadership runs `/{cmd_name.replace('setup_', '')}_remind`, the bot DMs every "
        f"roster member this message. Free guilds can configure it now — it just won't "
        f"fire until you have Premium + Member Roster Sync.\n\n"
        f"Use `{{name}}` as a placeholder for the member's roster name (optional).",
        default=default_remind_dm,
        current=saved_remind_dm,
        modal_title=f"{label} Reminder DM",
        modal_label="DM body (max 1000 chars)",
        timeout_cmd=cmd_name,
        cancel_event=cancel_event,
    )
    if remind_dm is None:
        return
    # Treat "use default" as empty in the DB — that way the hardcoded
    # default automatically picks up future tweaks without alliances
    # needing to re-run setup.
    dm_reminder_message = "" if remind_dm == default_remind_dm else remind_dm

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_storm_config, save_participation_config, update_config_field
    if template_a:
        save_storm_config(guild_id, f"{event_type}_A", tab_name, template_a,
                          "", "", "", "", "", "", timezone, log_channel_id,
                          post_channel_id=post_channel_id,
                          dm_reminder_message=dm_reminder_message)
    if template_b:
        save_storm_config(guild_id, f"{event_type}_B", tab_name, template_b,
                          "", "", "", "", "", "", timezone, log_channel_id,
                          post_channel_id=post_channel_id,
                          dm_reminder_message=dm_reminder_message)
    save_storm_config(guild_id, event_type, tab_name, template_a or template_b,
                      "", "", "", "", "", "", timezone, log_channel_id,
                      post_channel_id=post_channel_id,
                      dm_reminder_message=dm_reminder_message)

    # Persist the participation config to the (guild, event_type) row.
    save_participation_config(
        guild_id, event_type,
        enabled          = participation_cfg["enabled"],
        tab_name         = participation_cfg["tab_name"],
        questions        = participation_cfg["questions"],
        roster_tab       = participation_cfg["roster_tab"],
        roster_name_col  = participation_cfg["roster_name_col"],
        roster_alias_col = participation_cfg["roster_alias_col"],
        roster_start_row = participation_cfg["roster_start_row"],
    )

    # Persist the log channel to guild_configs so storm_log.py can read it
    if event_type == "DS":
        update_config_field(guild_id, "ds_log_channel_id", log_channel_id)
    else:
        update_config_field(guild_id, "cs_log_channel_id", log_channel_id)

    embed = discord.Embed(title=f"✅ {label} Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",    value=tab_name, inline=True)
    embed.add_field(name="Teams",        value={"both": "A & B", "A": "A only", "B": "B only"}[teams], inline=True)
    embed.add_field(name="Timezone",     value=tz_label, inline=True)
    embed.add_field(name="Log Channel",  value=f"<#{log_channel_id}>", inline=True)
    embed.add_field(name="Post Channel", value=f"<#{post_channel_id}>", inline=True)
    if participation_cfg["enabled"]:
        n_q = len(participation_cfg["questions"])
        embed.add_field(
            name="Participation Tracking",
            value=(f"✅ Enabled · {n_q} question(s) · Tab: "
                   f"`{participation_cfg['tab_name']}`"),
            inline=False,
        )
    else:
        embed.add_field(name="Participation Tracking", value="❌ Disabled", inline=False)
    if template_a:
        embed.add_field(name="Template A Preview",
                        value=f"```{template_a[:150]}{'...' if len(template_a) > 150 else ''}```",
                        inline=False)
    if template_b and template_b != template_a:
        embed.add_field(name="Template B Preview",
                        value=f"```{template_b[:150]}{'...' if len(template_b) > 150 else ''}```",
                        inline=False)
    embed.set_footer(text=f"Run /{cmd_name} again to update.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] {label} config saved for guild {guild_id}")


# ── Step 6 helper: participation tracking sub-flow (#20 rework) ───────────────

# Free-tier question types are universally available; Premium types are gated
# in the wizard. `roster_names` is unique to participation logs — it draws
# from the roster source the user configures here.
_PARTICIPATION_FREE_TYPES = ["text", "yes_no", "numeric", "roster_names"]
_PARTICIPATION_PREMIUM_TYPES = ["single_select", "multi_select", "date"]
_PARTICIPATION_TYPE_LABELS = {
    "text":          "Text — short typed answer",
    "yes_no":        "Yes / No",
    "numeric":       "Numeric — number with optional min/max",
    "roster_names":  "Roster names — pick or type member names",
    "single_select": "💎 Single-select dropdown",
    "multi_select":  "💎 Multi-select dropdown",
    "date":          "💎 Date (formatted entry)",
}


async def _run_storm_participation_step(
    channel, bot, user, cancel_event, *,
    guild_id: int, event_type: str, label: str, cmd_name: str,
    is_premium_flag: bool, current: dict,
) -> dict | None:
    """
    Step 6 of /setup_desertstorm and /setup_canyonstorm. Walks leadership
    through enabling/configuring participation log tracking. Returns a
    dict shaped like the one save_participation_config expects, or None
    if the user cancelled or timed out.
    """
    import wizard_registry
    from config import (
        get_participation_config, get_survey_config, get_birthday_config,
    )
    import premium

    cur_part = get_participation_config(guild_id, event_type)

    # ── 6.1 Enable? ────────────────────────────────────────────────────────────
    enable_view = YesNoView()
    await channel.send(
        f"**Step 6 of 7 — Participation Tracking**\n"
        f"Do you want to track {label} participation? Leadership runs "
        f"`/{cmd_name.replace('setup_', '')}_participation` after each event "
        f"to log who showed up, who sat out, etc.\n"
        f"You'll define the questions yourself, so the tracker matches how "
        f"your alliance runs the event.",
        view=enable_view,
    )
    await wait_view_or_cancel(enable_view, cancel_event)
    if enable_view.cancelled:
        return None
    if enable_view.selected is None:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return None

    if not enable_view.selected:
        # Disabled — keep the existing values around but mark off.
        return {
            "enabled":          0,
            "tab_name":         cur_part.get("tab_name") or "",
            "questions":        cur_part.get("questions") or [],
            "roster_tab":       cur_part.get("roster_tab") or "",
            "roster_name_col":  cur_part.get("roster_name_col") or 0,
            "roster_alias_col": cur_part.get("roster_alias_col") if cur_part.get("roster_alias_col") is not None else -1,
            "roster_start_row": cur_part.get("roster_start_row") or 2,
        }

    # ── 6.2 Sheet tab ──────────────────────────────────────────────────────────
    hardcoded_tab = "DS Participation Log" if event_type == "DS" else "CS Participation Log"
    tab_name = await ask_keep_or_change(
        channel,
        f"**Step 6.1 — Participation Sheet Tab**\n"
        f"Which tab should the bot write {label} participation rows to?\n"
        f"ℹ️ *The bot will create this tab automatically if it doesn't exist "
        f"and will manage the column structure based on the questions you define.*",
        default=hardcoded_tab,
        current=cur_part.get("tab_name", ""),
        modal_title="Participation Tab",
        modal_label="Tab name",
        timeout_cmd=cmd_name,
        cancel_event=cancel_event,
    )
    if tab_name is None:
        return None

    # ── 6.3 Roster source ──────────────────────────────────────────────────────
    # Smart "current" suggestion: prefer a previously-saved roster source
    # for this event type, else fall back to the survey stats tab if
    # configured, else birthday tab. The hardcoded default ("Squad
    # Powers") is the bot's baseline if none of those exist either.
    survey_cfg     = get_survey_config(guild_id) or {}
    birthday_cfg   = get_birthday_config(guild_id) or {}
    suggested_tab  = (
        cur_part.get("roster_tab")
        or survey_cfg.get("tab_squad_powers")
        or birthday_cfg.get("tab_name")
        or ""
    )
    roster_tab = await ask_keep_or_change(
        channel,
        f"**Step 6.2 — Roster Source: Sheet Tab**\n"
        f"Which tab in your sheet has the list of members? The bot reads "
        f"member names from here when you use a `Roster names` question.\n"
        f"*Tip: this is often the same tab you use for `/setup_survey` or "
        f"`/setup_birthdays`.*",
        default="Squad Powers",
        current=suggested_tab,
        modal_title="Roster Tab",
        modal_label="Tab name",
        timeout_cmd=cmd_name,
        cancel_event=cancel_event,
    )
    if roster_tab is None:
        return None

    saved_name_col_idx = cur_part.get("roster_name_col")
    raw_name_col = await ask_keep_or_change(
        channel,
        f"**Step 6.3 — Roster Source: Name Column**\n"
        f"Which column letter has the member name? (e.g. `A`, `B`, `E`)",
        default="A",
        current=(
            _col_index_to_letter(saved_name_col_idx)
            if isinstance(saved_name_col_idx, int) and saved_name_col_idx >= 0
            else ""
        ),
        modal_title="Name column",
        modal_label="Column letter",
        timeout_cmd=cmd_name,
        cancel_event=cancel_event,
    )
    if raw_name_col is None:
        return None
    roster_name_col = _col_letter_to_index(raw_name_col)
    if roster_name_col < 0:
        await channel.send(f"⚠️ `{raw_name_col}` isn't a valid column letter. Run `/{cmd_name}` to start again.")
        return None

    alias_view = YesNoView()
    await channel.send(
        "**Step 6.4 — Roster Source: Alias Column?**\n"
        "If you have other names or nicknames that you call your members in these "
        "mails, this helps resolve to their full name in your sheet automatically. "
        "Do you have an alias column?",
        view=alias_view,
    )
    await wait_view_or_cancel(alias_view, cancel_event)
    if alias_view.cancelled:
        return None
    if alias_view.selected is None:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return None

    roster_alias_col = -1
    if alias_view.selected:
        saved_alias = cur_part.get("roster_alias_col")
        # Hardcoded default = column right after the name column (a sensible
        # convention). Saved value (if any) is shown as "current".
        raw_alias = await ask_keep_or_change(
            channel,
            "**Alias Column**\nWhich column letter has the alias / nickname?",
            default=_col_index_to_letter(roster_name_col + 1),
            current=(
                _col_index_to_letter(saved_alias)
                if isinstance(saved_alias, int) and saved_alias >= 0
                else ""
            ),
            modal_title="Alias column",
            modal_label="Column letter",
            timeout_cmd=cmd_name,
            cancel_event=cancel_event,
        )
        if raw_alias is None:
            return None
        roster_alias_col = _col_letter_to_index(raw_alias)
        if roster_alias_col < 0:
            await channel.send(f"⚠️ `{raw_alias}` isn't a valid column letter. Run `/{cmd_name}` to start again.")
            return None

    raw_start = await ask_keep_or_change(
        channel,
        "**Step 6.5 — Roster Source: First Data Row**\n"
        "In your existing roster tab above, which row does the member data start on? "
        "Usually `2` if your sheet has a header row in row 1.",
        default="2",
        current=str(cur_part.get("roster_start_row") or ""),
        modal_title="Data start row",
        modal_label="Row number",
        timeout_cmd=cmd_name,
        cancel_event=cancel_event,
    )
    if raw_start is None:
        return None
    try:
        roster_start_row = max(1, int(raw_start.strip()))
    except ValueError:
        await channel.send(f"⚠️ `{raw_start}` isn't a number. Run `/{cmd_name}` to start again.")
        return None

    # ── 6.6 Questions builder ──────────────────────────────────────────────────
    questions = list(cur_part.get("questions") or [])
    cap = None if is_premium_flag else 3

    def _summarize() -> str:
        if not questions:
            return "*(no questions yet — every participation log will only ask for the date)*"
        lines = []
        for i, q in enumerate(questions, start=1):
            t = _PARTICIPATION_TYPE_LABELS.get(q.get("type"), q.get("type", "?"))
            lines.append(f"**{i}. {q.get('label', '?')}** — _{t}_")
        return "\n".join(lines)

    while True:
        class _BuilderView(discord.ui.View):
            def __init__(self, count: int):
                super().__init__(timeout=300)
                self.action: str | None = None
                self.edit_idx: int | None = None
                self.del_idx: int | None = None

                if count > 0:
                    edit_sel = discord.ui.Select(
                        placeholder="✏️ Edit a question…",
                        options=[discord.SelectOption(label=f"Edit: {q.get('label', '?')[:90]}", value=str(i))
                                 for i, q in enumerate(questions[:25])],
                        row=0,
                    )
                    async def _ec(inter: discord.Interaction):
                        self.action   = "edit"
                        self.edit_idx = int(edit_sel.values[0])
                        for c in self.children: c.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()
                    edit_sel.callback = _ec
                    self.add_item(edit_sel)

                    del_sel = discord.ui.Select(
                        placeholder="🗑️ Remove a question…",
                        options=[discord.SelectOption(label=f"Remove: {q.get('label', '?')[:90]}", value=str(i))
                                 for i, q in enumerate(questions[:25])],
                        row=1,
                    )
                    async def _dc(inter: discord.Interaction):
                        self.action  = "delete"
                        self.del_idx = int(del_sel.values[0])
                        for c in self.children: c.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()
                    del_sel.callback = _dc
                    self.add_item(del_sel)

            @discord.ui.button(label="➕ Add question", style=discord.ButtonStyle.primary, row=2)
            async def add_q(self, inter: discord.Interaction, button: discord.ui.Button):
                self.action = "add"
                for c in self.children: c.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=2)
            async def done(self, inter: discord.Interaction, button: discord.ui.Button):
                self.action = "done"
                for c in self.children: c.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        cap_note = (
            f"\n*Free tier limit: {cap} questions.*"
            if cap is not None else
            "\n💎 *Premium: unlimited questions and three extra question types.*"
        )
        view = _BuilderView(len(questions))
        await channel.send(
            f"**Step 6.6 — Participation Questions**\n"
            f"Each question becomes a column on your sheet and a step in the "
            f"`/{cmd_name.replace('setup_', '')}_participation` flow.\n"
            f"Examples: *Vote count*, *Sitting out*, *Did anyone show up late?*\n"
            f"{cap_note}\n\n{_summarize()}",
            view=view,
        )
        await wait_view_or_cancel(view, cancel_event)
        if view.cancelled:
            return
        if view.action is None:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        if view.action == "done":
            break
        if view.action == "delete":
            removed = questions.pop(view.del_idx)
            await channel.send(f"🗑️ Removed: **{removed.get('label')}**")
            continue
        if view.action in ("add", "edit"):
            if view.action == "add" and cap is not None and len(questions) >= cap:
                await channel.send(embed=premium.limit_reached_embed(
                    feature_label="Participation Questions",
                    current=len(questions), cap=cap, plural_unit="questions",
                ))
                continue
            existing = questions[view.edit_idx] if view.action == "edit" else None
            new_q = await _build_participation_question(
                channel, bot, user, cancel_event,
                cmd_name=cmd_name,
                is_premium_flag=is_premium_flag,
                existing=existing,
            )
            if new_q is None:
                return None
            if view.action == "edit":
                questions[view.edit_idx] = new_q
                await channel.send(f"✅ Updated: **{new_q['label']}**")
            else:
                questions.append(new_q)
                await channel.send(f"✅ Added: **{new_q['label']}** ({len(questions)} so far)")

    return {
        "enabled":          1,
        "tab_name":         tab_name,
        "questions":        questions,
        "roster_tab":       roster_tab,
        "roster_name_col":  roster_name_col,
        "roster_alias_col": roster_alias_col,
        "roster_start_row": roster_start_row,
    }


def _col_letter_to_index(letter: str) -> int:
    """A→0, B→1, ..., AA→26. Returns -1 on invalid input."""
    s = (letter or "").strip().upper()
    if not s or not all(c.isalpha() for c in s):
        return -1
    n = 0
    for c in s:
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


def _col_index_to_letter(idx: int) -> str:
    """0→A, 1→B, ..., 26→AA."""
    if idx < 0:
        return "A"
    out = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


async def _build_participation_question(
    channel, bot, user, cancel_event, *,
    cmd_name: str, is_premium_flag: bool, existing: dict | None,
) -> dict | None:
    """Add or edit a single participation question. Mirrors the survey
    question builder's shape but with participation-specific types."""

    def check(m):
        return m.author == user and m.channel == channel

    # Label
    label_extra = f"\n*Existing label:* `{existing.get('label', '')}`" if existing else ""
    await channel.send(
        f"**Question — Label**\n"
        f"What's the label for this question? (e.g. `Sitting Out`, `Vote Count`)" + label_extra
    )
    try:
        reply = await bot.wait_for("message", check=check, timeout=180)
    except asyncio.TimeoutError:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return None
    q_label = reply.content.strip() or (existing.get("label", "") if existing else "")
    if not q_label:
        await channel.send("⚠️ Empty label. Skipping this question.")
        return None
    q_key = (
        q_label.lower()
        .replace(" ", "_").replace("-", "_").replace("/", "_")
        .replace("(", "").replace(")", "")
    )

    # Type
    type_options: list[discord.SelectOption] = []
    for t in _PARTICIPATION_FREE_TYPES:
        type_options.append(discord.SelectOption(label=_PARTICIPATION_TYPE_LABELS[t], value=t))
    if is_premium_flag:
        for t in _PARTICIPATION_PREMIUM_TYPES:
            type_options.append(discord.SelectOption(label=_PARTICIPATION_TYPE_LABELS[t], value=t))

    class _TypeView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.selected: str | None = None
            sel = discord.ui.Select(placeholder="Pick the answer type…", options=type_options)
            async def _cb(inter):
                self.selected = sel.values[0]
                sel.disabled = True
                await inter.response.edit_message(
                    content=f"✅ Type: **{_PARTICIPATION_TYPE_LABELS.get(self.selected, self.selected)}**",
                    view=self,
                )
                self.stop()
            sel.callback = _cb
            self.add_item(sel)

    type_view = _TypeView()
    type_extra = f"\n*Existing type:* `{existing.get('type')}`" if existing else ""
    await channel.send(f"**Question — Answer Type**{type_extra}", view=type_view)
    await wait_view_or_cancel(type_view, cancel_event)
    if type_view.cancelled:
        return
    if type_view.selected is None:
        await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
        return None
    q_type = type_view.selected

    q: dict = {"key": q_key, "label": q_label, "type": q_type}

    # Type-specific extras
    if q_type == "numeric":
        await channel.send(
            "**Optional — bounds**\nReply with `min,max` (e.g. `0,500`) or "
            "type `none` for no bounds."
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        bounds_raw = reply.content.strip().lower()
        if bounds_raw not in ("", "none"):
            try:
                lo, hi = (s.strip() for s in bounds_raw.split(","))
                if lo:
                    q["min"] = float(lo) if "." in lo else int(lo)
                if hi:
                    q["max"] = float(hi) if "." in hi else int(hi)
            except Exception:
                await channel.send("⚠️ Couldn't parse those bounds — saving without min/max.")

    elif q_type in ("single_select", "multi_select"):
        await channel.send(
            "**Options** *(💎 Premium)*\nList the choices separated by commas.\n"
            "Example: `Win, Loss, Draw`"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=180)
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        opts = [o.strip() for o in reply.content.split(",") if o.strip()]
        if not opts:
            await channel.send("⚠️ No options provided. Skipping this question.")
            return None
        q["options"] = opts[:25]

    elif q_type == "date":
        await channel.send(
            "**Date format** *(💎 Premium)*\nEnter a `strptime`-style format "
            "(e.g. `%m/%d/%Y`) or reply `default` for `%m/%d/%Y`."
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run `/{cmd_name}` to start again.")
            return None
        fmt = reply.content.strip()
        q["date_format"] = "%m/%d/%Y" if fmt.lower() in ("", "default") else fmt

    return q


async def run_event_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring event types."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=120),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    async def ask_view(prompt: str, view: discord.ui.View):
        await channel.send(prompt, view=view)
        await wait_view_or_cancel(view, cancel_event)
        return view

    from config import get_config, get_guild_events, save_guild_event, get_or_create_config, update_config_field
    import re as _re

    guild_cfg = get_config(guild_id) or get_or_create_config(guild_id)
    timezone  = guild_cfg.timezone if guild_cfg.timezone else "America/New_York"
    tz_label  = TIMEZONE_LABELS.get(timezone, timezone)
    events    = get_guild_events(guild_id, active_only=True)

    draft_channel_id    = guild_cfg.event_draft_channel_id or 0
    announce_channel_id = guild_cfg.event_announce_channel_id or 0
    draft_time          = guild_cfg.event_draft_time or "12:00"
    five_min_warning    = guild_cfg.event_five_min_warning if guild_cfg.event_five_min_warning is not None else 1

    # ── If already configured, show summary with action options ───────────────
    if draft_channel_id and events:
        summary_embed = discord.Embed(
            title="📣 Event Setup",
            description="Your events are already configured. What would you like to do?",
            color=discord.Color.blurple(),
        )
        summary_embed.add_field(name="Draft Channel",        value=f"<#{draft_channel_id}>",    inline=False)
        summary_embed.add_field(name="Announcement Channel", value=f"<#{announce_channel_id}>", inline=False)
        summary_embed.add_field(name="Draft Time",           value=draft_time,                  inline=False)
        summary_embed.add_field(name="5-min Warning",        value="Yes" if five_min_warning else "No", inline=False)
        ev_list = "\n".join(f"• **{e['name']}** — {e['default_time']} {tz_label}" for e in events)
        summary_embed.add_field(name="Events", value=ev_list, inline=False)

        class EventActionView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.choice = None

            @discord.ui.button(label="⚙️ Edit Event Settings", style=discord.ButtonStyle.primary, row=0)
            async def edit_settings(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "settings"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="➕ Add Event", style=discord.ButtonStyle.success, row=0)
            async def add_event(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "add"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✏️ Edit Event", style=discord.ButtonStyle.secondary, row=1)
            async def edit_event(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "edit"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="🗑️ Delete Event", style=discord.ButtonStyle.danger, row=1)
            async def delete_event(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "delete"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

            @discord.ui.button(label="✅ No changes needed", style=discord.ButtonStyle.secondary, row=2)
            async def done(self, inter: discord.Interaction, button: discord.ui.Button):
                self.choice = "done"
                for item in self.children: item.disabled = True
                await inter.response.edit_message(view=self)
                self.stop()

        action_view = EventActionView()
        await channel.send(embed=summary_embed, view=action_view)
        await wait_view_or_cancel(action_view, cancel_event)
        if action_view.cancelled:
            return

        if not action_view.choice or action_view.choice == "done":
            await channel.send("✅ No changes made.")
            return

        # Jump straight to event list for add/edit/delete
        # We already have all the settings values — skip the settings wizard
        # and fall through directly to the event list below
        if action_view.choice in ("add", "edit", "delete"):
            pass  # fall through to event list at end of function

        # Fall through to full settings wizard for "settings"
        elif action_view.choice == "settings":
            await channel.send("⚙️ Let's update your event settings...")

        skip_settings = action_view.choice in ("add", "edit", "delete")
    else:
        skip_settings = False

    if not skip_settings:
        await channel.send(
            "⚙️ **Event Setup**\n"
            "Configure your alliance events. All events share the same draft channel, "
            "announcement channel, draft time, and 5-minute warning setting."
        )

    # ── Steps 1-4: Channel/time settings (skipped if coming from action menu) ──
    if not skip_settings:
        is_premium_flag  = await premium.is_premium(guild_id, interaction=interaction)
        current_draft_id = guild_cfg.event_draft_channel_id or 0
        draft_ch_view    = ChannelSelectStep(
            "Select the draft channel...",
            suggested_name="event-drafts",
            include_threads=is_premium_flag,
            guild=interaction.guild,
        )
        await channel.send(
            "**Step 1 of 5 — Draft Channel**\n"
            "Which channel should the bot post event announcement drafts for leadership to review?\n"
            "*(This applies to all events)*",
            view=draft_ch_view,
        )
        await wait_view_or_cancel(draft_ch_view, cancel_event)
        if draft_ch_view.cancelled:
            return
        if not draft_ch_view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return
        draft_channel_id = draft_ch_view.selected_channel.id

        current_ann_id = guild_cfg.event_announce_channel_id or 0
        ann_ch_view    = ChannelSelectStep(
            "Select the announcement channel...",
            suggested_name="announcements",
            include_threads=is_premium_flag,
            guild=interaction.guild,
        )
        await channel.send(
            "**Step 2 of 5 — Announcement Channel**\n"
            "Which channel should approved announcements be posted to?\n"
            "*(This applies to all events)*",
            view=ann_ch_view,
        )
        await wait_view_or_cancel(ann_ch_view, cancel_event)
        if ann_ch_view.cancelled:
            return
        if not ann_ch_view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return
        announce_channel_id = ann_ch_view.selected_channel.id

        tz_label       = TIMEZONE_LABELS.get(timezone, timezone)
        # `draft_time` is stored in 24h format ("12:00"); show it as-is in the
        # default button label, but accept either format from user input.
        # Re-prompt up to 3 times on unparseable input before bailing out.
        attempts_left = 3
        while True:
            draft_time_raw = await ask_keep_or_change(
                channel,
                f"**Step 3 of 5 — Draft Posting Time**\n"
                f"What time should the bot post the draft each event day? *(in {tz_label})*\n"
                f"*(e.g. `12:00pm` for noon)*",
                default="12:00",
                current=draft_time or "",
                modal_title="Draft Posting Time",
                modal_label="Time",
                timeout_cmd="setup_events",
                cancel_event=cancel_event,
            )
            if not draft_time_raw:
                return
            parsed_draft = _parse_12h_time(draft_time_raw)
            if parsed_draft:
                draft_time = parsed_draft
                break
            if (len(draft_time_raw) == 5 and draft_time_raw[2] == ":"
                    and draft_time_raw.replace(":", "").isdigit()):
                draft_time = draft_time_raw   # already 24h
                break
            attempts_left -= 1
            if attempts_left <= 0:
                await channel.send(
                    "⚠️ Could not read that time after a few tries. "
                    "Run `/setup_events` to start over."
                )
                return
            await channel.send(
                f"⚠️ Could not read **`{draft_time_raw}`** as a time. "
                f"Try `12:00pm`, `9:00am`, or `15:30`. Let's try once more."
            )

        warn_view = YesNoView()
        await channel.send(
            "**Step 4 of 5 — 5-Minute Warning**\n"
            "Should the bot automatically post a 5-minute warning before events?\n"
            "*(This applies to all events)*",
            view=warn_view,
        )
        await wait_view_or_cancel(warn_view, cancel_event)
        if warn_view.cancelled:
            return
        if warn_view.selected is None:
            await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
            return
        five_min_warning = 1 if warn_view.selected else 0

        update_config_field(guild_id, "event_draft_channel_id",    draft_channel_id)
        update_config_field(guild_id, "event_announce_channel_id", announce_channel_id)
        update_config_field(guild_id, "event_draft_time",          draft_time)
        update_config_field(guild_id, "event_five_min_warning",    five_min_warning)

    # ── Event list ────────────────────────────────────────────────────────────
    events = get_guild_events(guild_id, active_only=False)

    async def build_event_list():
        """Show event list with Add/Edit/Delete/Finish controls."""
        nonlocal events

        while True:
            events = get_guild_events(guild_id, active_only=False)

            if events:
                event_display = "\n".join(
                    f"{i+1}. **{e['name']}** — "
                    f"{'🔁 ' + str(e['interval_days']) + '-day cycle' if e['schedule_type'] == 'repeating' else '📅 Manual'} "
                    f"at {e['default_time']}"
                    + (" *(inactive)*" if not e['active'] else "")
                    for i, e in enumerate(events)
                )
            else:
                event_display = "*(no events configured yet)*"

            class EventListView(discord.ui.View):
                def __init__(self, event_list):
                    super().__init__(timeout=300)
                    self.action     = None
                    self.edit_key   = None
                    self.delete_key = None

                    if event_list:
                        edit_select = discord.ui.Select(
                            placeholder="✏️ Edit an event...",
                            options=[discord.SelectOption(
                                label=f"Edit: {e['name']}", value=e['short_key']
                            ) for e in event_list],
                            row=0,
                        )
                        async def _edit_cb(inter: discord.Interaction):
                            self.action   = "edit"
                            self.edit_key = edit_select.values[0]
                            for item in self.children: item.disabled = True
                            await inter.response.edit_message(view=self)
                            self.stop()
                        edit_select.callback = _edit_cb
                        self.add_item(edit_select)

                        del_select = discord.ui.Select(
                            placeholder="🗑️ Delete an event...",
                            options=[discord.SelectOption(
                                label=f"Delete: {e['name']}", value=e['short_key']
                            ) for e in event_list],
                            row=1,
                        )
                        async def _del_cb(inter: discord.Interaction):
                            self.action     = "delete"
                            self.delete_key = del_select.values[0]
                            for item in self.children: item.disabled = True
                            await inter.response.edit_message(view=self)
                            self.stop()
                        del_select.callback = _del_cb
                        self.add_item(del_select)

                @discord.ui.button(label="➕ Add Event", style=discord.ButtonStyle.primary, row=2)
                async def add_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                    self.action = "add"
                    for item in self.children: item.disabled = True
                    await inter.response.edit_message(view=self)
                    self.stop()

                @discord.ui.button(label="✅ Finish", style=discord.ButtonStyle.success, row=2)
                async def finish_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                    self.action = "finish"
                    for item in self.children: item.disabled = True
                    await inter.response.edit_message(view=self)
                    self.stop()

            list_view = EventListView(events)
            await channel.send(
                f"**Step 5 of 5 — Your Events:**\n{event_display}",
                view=list_view,
            )
            await wait_view_or_cancel(list_view, cancel_event)
            if list_view.cancelled:
                return

            if not list_view.action:
                await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
                return False

            if list_view.action == "finish":
                return True

            elif list_view.action == "delete":
                from config import delete_guild_event, get_guild_event
                ev = get_guild_event(guild_id, list_view.delete_key)
                delete_guild_event(guild_id, list_view.delete_key)
                await channel.send(f"🗑️ Removed: **{ev['name'] if ev else list_view.delete_key}**")

            elif list_view.action in ("add", "edit"):
                existing = None
                if list_view.action == "edit":
                    from config import get_guild_event
                    existing = get_guild_event(guild_id, list_view.edit_key)
                elif list_view.action == "add":
                    # Free-tier cap on number of events
                    cap = await premium.get_limit("events", guild_id, interaction=interaction)
                    if cap is not None and len(events) >= cap:
                        await channel.send(embed=premium.limit_reached_embed(
                            feature_label="Event Announcements",
                            current=len(events), cap=cap, plural_unit="events",
                        ))
                        continue

                # ── Event builder ──────────────────────────────────────────────
                # Name
                existing_name_extra = f"\n*Existing name:* `{existing['name']}`" if existing else ""
                name_raw = await ask_text(
                    "**Event Name**\n"
                    "What is this event called? (e.g. `Plague Marauder (AE)`, `Zombie Siege`)"
                    + existing_name_extra
                )
                if not name_raw:
                    return False
                name      = name_raw.strip() or (existing['name'] if existing else "")
                short_key = existing['short_key'] if existing else _re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

                # Time — re-prompt up to 3 times on unparseable input.
                existing_time = existing['default_time'] if existing else ""
                attempts_left = 3
                default_time  = None
                while True:
                    if existing_time:
                        # Editing an event — there's no hardcoded baseline
                        # for "event time" since it varies per event. Pass
                        # `current` AND `default` as the same existing
                        # value; the helper's `current == default` branch
                        # renders the standard two-button layout.
                        time_raw = await ask_keep_or_change(
                            channel,
                            f"**{name} — Event Time**\n"
                            f"What time does this event usually start? *(in {tz_label})*\n"
                            f"*(e.g. `10:15pm`, `9:00am`)*",
                            default=existing_time,
                            current=existing_time,
                            modal_title="Event Time",
                            modal_label="Time",
                            timeout_cmd="setup_events",
                            cancel_event=cancel_event,
                        )
                    else:
                        time_raw = await ask_text(
                            f"**{name} — Event Time**\n"
                            f"What time does this event usually start? *(in {tz_label})*\n"
                            f"*(e.g. `10:15pm`, `9:00am`)*"
                        )
                    if not time_raw:
                        return False
                    parsed = _parse_12h_time(time_raw)
                    if parsed:
                        default_time = parsed
                        break
                    if (len(time_raw) == 5 and time_raw[2] == ":"
                            and time_raw.replace(":", "").isdigit()):
                        default_time = time_raw   # already 24h
                        break
                    attempts_left -= 1
                    if attempts_left <= 0:
                        await channel.send(
                            "⚠️ Could not read that time after a few tries. "
                            "Run `/setup_events` to start over."
                        )
                        return False
                    await channel.send(
                        f"⚠️ Could not read **`{time_raw}`** as a time. "
                        f"Try `10:15pm`, `9:00am`, or `21:00`. Let's try once more."
                    )

                # Schedule
                sched_view = ScheduleTypeView()
                await channel.send(
                    f"**{name} — Schedule**\n"
                    "Does this event repeat on a fixed cycle, or do you add it manually each time?",
                    view=sched_view,
                )
                await wait_view_or_cancel(sched_view, cancel_event)
                if sched_view.cancelled:
                    return
                if not sched_view.selected:
                    await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
                    return False
                schedule_type = sched_view.selected

                anchor_date   = existing.get('anchor_date', '') if existing else ''
                interval_days = existing.get('interval_days', 3) if existing else 3

                if schedule_type == "repeating":
                    anchor_extra = f"\n*Existing anchor date:* `{anchor_date}`" if anchor_date else ""
                    anchor_raw   = await ask_text(
                        f"**{name} — Anchor Date**\n"
                        "Enter a recent or upcoming date when this event occurs.\n"
                        "Type the month and day (e.g. `March 30`, `April 14`)"
                        + anchor_extra
                    )
                    if not anchor_raw:
                        return False
                    parsed_anchor = _parse_month_day(anchor_raw)
                    if not parsed_anchor:
                        await channel.send("⚠️ Could not read that date. Try `March 30`. Run `/setup_events` to try again.")
                        return False
                    anchor_date = parsed_anchor

                    interval_raw = await ask_keep_or_change(
                        channel,
                        f"**{name} — Cycle Interval**\n"
                        "How many days between each occurrence? (e.g. `3`)",
                        default="3",
                        current=(
                            str(existing['interval_days'])
                            if existing and existing.get('interval_days')
                            else ""
                        ),
                        modal_title="Cycle Interval",
                        modal_label="Days between occurrences",
                        timeout_cmd="setup_events",
                        cancel_event=cancel_event,
                    )
                    if not interval_raw:
                        return False
                    try:
                        interval_days = int(interval_raw)
                    except ValueError:
                        await channel.send("⚠️ Please enter a whole number. Run `/setup_events` to try again.")
                        return False

                # Blurb
                cur_blurb    = existing.get('announcement_blurb', '') if existing else ''
                default_blurb = f"{name} at {{time}} ({{server_time}} Server Time)."

                class BlurbChoiceView(discord.ui.View):
                    def __init__(self, has_existing: bool):
                        super().__init__(timeout=120)
                        self.choice = None

                    @discord.ui.button(label="✅ Use default blurb", style=discord.ButtonStyle.success)
                    async def use_default(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.choice = "default"
                        for item in self.children: item.disabled = True
                        await inter.response.edit_message(
                            content=f"✅ Using default blurb:\n`{default_blurb}`", view=self
                        )
                        self.stop()

                    @discord.ui.button(label="✏️ Enter my own", style=discord.ButtonStyle.secondary)
                    async def enter_own(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.choice = "custom"
                        for item in self.children: item.disabled = True
                        await inter.response.edit_message(view=self)
                        self.stop()

                blurb_view = BlurbChoiceView(has_existing=bool(cur_blurb))
                if cur_blurb:
                    keep_btn = discord.ui.Button(
                        label="⏭️ Keep existing", style=discord.ButtonStyle.secondary, row=1
                    )
                    async def _keep_cb(inter: discord.Interaction):
                        blurb_view.choice = "keep"
                        for item in blurb_view.children: item.disabled = True
                        await inter.response.edit_message(content="✅ Keeping existing blurb.", view=blurb_view)
                        blurb_view.stop()
                    keep_btn.callback = _keep_cb
                    blurb_view.add_item(keep_btn)

                blurb_msg = (
                    f"**{name} — Announcement Blurb**\n"
                    "This message gets posted when this event fires.\n"
                    "Use `{time}` for the event time in your timezone and `{server_time}` for Server Time.\n\n"
                    f"**Default:** `{default_blurb}`"
                )
                if cur_blurb:
                    blurb_msg += f"\n**Existing:** `{cur_blurb[:100]}{'...' if len(cur_blurb) > 100 else ''}`"

                await channel.send(blurb_msg, view=blurb_view)
                await wait_view_or_cancel(blurb_view, cancel_event)
                if blurb_view.cancelled:
                    return
                if not blurb_view.choice:
                    await channel.send("⏰ Timed out. Run `/setup_events` to start again.")
                    return False

                if blurb_view.choice == "default":
                    blurb = default_blurb
                elif blurb_view.choice == "keep":
                    blurb = cur_blurb
                else:
                    blurb_raw = await ask_text(
                        "Enter your announcement blurb:\n"
                        "*(Use `{time}` and `{server_time}` as placeholders)*",
                        max_chars=1000,
                    )
                    if blurb_raw is None:
                        return False
                    blurb = blurb_raw.strip() or default_blurb

                # Save event
                event = {
                    "short_key":               short_key,
                    "name":                    name,
                    "timezone":                timezone,
                    "default_time":            default_time,
                    "announcement_blurb":      blurb,
                    "schedule_type":           schedule_type,
                    "anchor_date":             anchor_date,
                    "interval_days":           interval_days,
                    "draft_channel_id":        draft_channel_id,
                    "announcement_channel_id": announce_channel_id,
                    "draft_time":              draft_time,
                    "five_min_warning":        five_min_warning,
                    "active":                  1,
                }
                save_guild_event(guild_id, event)
                action_word = "Updated" if existing else "Added"
                await channel.send(f"✅ {action_word}: **{name}**")

    result = await build_event_list()
    if not result:
        return

    # ── Summary ────────────────────────────────────────────────────────────────
    events   = get_guild_events(guild_id, active_only=True)
    tz_label = TIMEZONE_LABELS.get(timezone, timezone)

    embed = discord.Embed(title="✅ Events Configured", color=discord.Color.green())
    embed.add_field(name="Draft Channel",        value=f"<#{draft_channel_id}>",    inline=False)
    embed.add_field(name="Announcement Channel", value=f"<#{announce_channel_id}>", inline=False)
    embed.add_field(name="Draft Time",           value=draft_time,                  inline=False)
    embed.add_field(name="5-min Warning",        value="Yes" if five_min_warning else "No", inline=False)
    if events:
        ev_list = "\n".join(f"• **{e['name']}** — {e['default_time']} {tz_label}" for e in events)
        embed.add_field(name="Events", value=ev_list, inline=False)
    embed.set_footer(text="Run /setup_events again to add or edit events.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Events saved for guild {guild_id}")

async def run_birthday_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring birthday tracking."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=120),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send("❌ Cancelled.")
            else:
                await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
            return None
        return reply.content.strip()[:max_chars]

    def col_letter_to_index(raw: str) -> int:
        """Convert 'A' or 'a' to 0, 'B' to 1, etc. Returns -1 if invalid."""
        raw = raw.strip().upper()
        if len(raw) == 1 and raw.isalpha():
            return ord(raw) - ord('A')
        return -1

    def index_to_letter(idx: int) -> str:
        return chr(ord('A') + idx) if idx >= 0 else "—"

    from config import get_birthday_config
    current = get_birthday_config(guild_id)

    await channel.send(
        "⚙️ **Birthday Tracking Setup**\n"
        "Configure how the bot tracks member birthdays."
    )

    # ── Step 1: Enable? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        "**Step 1 of 9 — Enable birthday tracking?**\n"
        "Should the bot track member birthdays from your Google Sheet?",
        view=enabled_view,
    )
    await wait_view_or_cancel(enabled_view, cancel_event)
    if enabled_view.cancelled:
        return
    if enabled_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
        return
    if not enabled_view.selected:
        from config import save_birthday_config
        save_birthday_config(guild_id, enabled=0, **{k: v for k, v in current.items() if k not in ('guild_id', 'enabled')})
        await channel.send("✅ Birthday tracking disabled.")
        return

    # ── Step 2: Sheet tab ─────────────────────────────────────────────────────
    tab_name = await ask_keep_or_change(
        channel,
        "**Step 2 of 9 — Sheet Tab**\n"
        "Which tab in your Google Sheet contains birthday data?\n"
        "⚠️ *Make sure this tab exists in your sheet before continuing.*",
        default="Birthdays",
        current=current.get("tab_name", ""),
        modal_title="Sheet Tab Name",
        modal_label="Tab name",
        timeout_cmd="setup_birthdays",
        cancel_event=cancel_event,
    )
    if tab_name is None:
        return

    # discord_id_col is no longer asked in the wizard — preserve any existing
    # value so save_birthday_config doesn't clobber it.
    discord_id_col = current.get("discord_id_col", -1)

    # ── Step 3: Name column ────────────────────────────────────────────────────
    saved_name_col = current.get("name_col")
    name_col_raw = await ask_keep_or_change(
        channel,
        "**Step 3 of 9 — Name Column**\n"
        "Which column contains the member's name?",
        default="A",
        current=(
            index_to_letter(saved_name_col)
            if isinstance(saved_name_col, int) and saved_name_col >= 0
            else ""
        ),
        modal_title="Name Column",
        modal_label="Column letter",
        timeout_cmd="setup_birthdays",
        cancel_event=cancel_event,
    )
    if name_col_raw is None:
        return
    name_col = col_letter_to_index(name_col_raw)
    if name_col < 0:
        await channel.send("⚠️ Please enter a single column letter like `A`. Run `/setup_birthdays` to try again.")
        return

    # ── Step 4: Birthday column ────────────────────────────────────────────────
    saved_bday_col = current.get("birthday_col")
    bday_col_raw = await ask_keep_or_change(
        channel,
        "**Step 4 of 9 — Birthday Column**\n"
        "Which column contains the member's birthday?",
        default="B",
        current=(
            index_to_letter(saved_bday_col)
            if isinstance(saved_bday_col, int) and saved_bday_col >= 0
            else ""
        ),
        modal_title="Birthday Column",
        modal_label="Column letter",
        timeout_cmd="setup_birthdays",
        cancel_event=cancel_event,
    )
    if bday_col_raw is None:
        return
    birthday_col = col_letter_to_index(bday_col_raw)
    if birthday_col < 0:
        await channel.send("⚠️ Please enter a single column letter like `B`. Run `/setup_birthdays` to try again.")
        return

    # ── Step 5: Train integration ─────────────────────────────────────────────
    train_view = YesNoView()
    await channel.send(
        "**Step 5 of 9 — Train Schedule Integration**\n"
        "Should the bot automatically add members to the train schedule on their birthday?",
        view=train_view,
    )
    await wait_view_or_cancel(train_view, cancel_event)
    if train_view.cancelled:
        return
    if train_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
        return
    train_integration = 1 if train_view.selected else 0

    flexible_placement = 0
    lookahead_days     = 14

    if not train_integration:
        await channel.send(
            "ℹ️ *Skipping Steps 6–7 (placement and lookahead) — train integration is off.*"
        )

    if train_integration:
        # ── Step 6: Flexible placement ─────────────────────────────────────────
        class PlacementView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected = None

            @discord.ui.button(label="🎂 Birthday only", style=discord.ButtonStyle.primary)
            async def birthday_only(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 0
                for item in self.children: item.disabled = True
                await inter.response.edit_message(content="✅ Placement: **Birthday only**", view=self)
                self.stop()

            @discord.ui.button(label="📅 Assign nearby if taken", style=discord.ButtonStyle.secondary)
            async def flexible(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 1
                for item in self.children: item.disabled = True
                await inter.response.edit_message(content="✅ Placement: **Assign 1 day before or after if birthday is taken**", view=self)
                self.stop()

        placement_view = PlacementView()
        await channel.send(
            "**Step 6 of 9 — Birthday Placement**\n"
            "If the member's birthday is already taken on the train schedule, what should the bot do?",
            view=placement_view,
        )
        await wait_view_or_cancel(placement_view, cancel_event)
        if placement_view.cancelled:
            return
        if placement_view.selected is None:
            await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
            return
        flexible_placement = placement_view.selected

        # ── Step 7: Lookahead days ─────────────────────────────────────────────
        lookahead_raw = await ask_keep_or_change(
            channel,
            "**Step 7 of 9 — Train Schedule Lookahead**\n"
            "Since you enabled train integration, how many days ahead of a "
            "member's birthday should the bot pre-populate them on the train "
            "schedule? This only applies to train-integration auto-placement; "
            "the birthday announcement itself always fires on the day.\n"
            "*(we recommend 14)*",
            default="14",
            current=str(current.get("lookahead_days") or ""),
            modal_title="Lookahead Days",
            modal_label="Number of days",
            timeout_cmd="setup_birthdays",
            cancel_event=cancel_event,
        )
        if lookahead_raw is None:
            return
        try:
            lookahead_days = int(str(lookahead_raw).strip())
            if lookahead_days < 1:
                raise ValueError
        except ValueError:
            await channel.send("⚠️ Please enter a number like `14`. Run `/setup_birthdays` to try again.")
            return

    # ── Step 8: Birthday reminders ─────────────────────────────────────────────
    remind_view = YesNoView()
    await channel.send(
        "**Step 8 of 9 — Birthday Reminders**\n"
        "Should the bot post a message in Discord on a member's birthday?\n"
        f"*(It will post: \"🎂 Today is **[name]**'s birthday!\")*",
        view=remind_view,
    )
    await wait_view_or_cancel(remind_view, cancel_event)
    if remind_view.cancelled:
        return
    if remind_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
        return
    reminders_enabled    = 1 if remind_view.selected else 0
    reminder_channel_id  = 0
    reminder_time        = "08:00"
    if not reminders_enabled:
        await channel.send(
            "ℹ️ *Skipping Steps 8a–8b (reminder channel and time) — birthday reminders are off.*"
        )

    if reminders_enabled:
        # ── Step 8a: Reminder channel ──────────────────────────────────────────
        is_premium_flag = await premium.is_premium(guild_id, interaction=interaction)
        remind_ch_view = ChannelSelectStep(
            "Select the birthday announcement channel...",
            suggested_name="birthdays",
            include_threads=is_premium_flag,
            guild=interaction.guild,
        )
        await channel.send(
            "**Step 8a of 9 — Birthday Announcement Channel**\n"
            "Which channel should birthday announcements be posted in?",
            view=remind_ch_view,
        )
        await wait_view_or_cancel(remind_ch_view, cancel_event)
        if remind_ch_view.cancelled:
            return
        if not remind_ch_view.confirmed:
            await channel.send("⏰ Timed out. Run `/setup_birthdays` to start again.")
            return
        reminder_channel_id = remind_ch_view.selected_channel.id

        # ── Step 8b: Reminder time ─────────────────────────────────────────────
        # Re-prompt up to 3 times on unparseable input rather than silently
        # falling back to a default.
        from config import get_config
        guild_cfg = get_config(guild_id)
        tz_label  = TIMEZONE_LABELS.get(guild_cfg.timezone if guild_cfg else "America/New_York", "your timezone")
        attempts_left = 3
        reminder_time = "08:00"
        while True:
            time_raw = await ask_keep_or_change(
                channel,
                f"**Step 8b of 9 — Reminder Time**\n"
                f"What time should birthday announcements be posted? *(in {tz_label})*\n"
                f"*(e.g. `8:00am`, `12:00pm`)*",
                default="8:00am",
                current=current.get("reminder_time", ""),
                modal_title="Reminder Time",
                modal_label="Time",
                timeout_cmd="setup_birthdays",
                cancel_event=cancel_event,
            )
            if time_raw is None:
                return
            parsed = _parse_12h_time(time_raw)
            if parsed:
                reminder_time = parsed
                break
            if (len(time_raw) == 5 and time_raw[2] == ":"
                    and time_raw.replace(":", "").isdigit()):
                reminder_time = time_raw  # already 24h
                break
            attempts_left -= 1
            if attempts_left <= 0:
                await channel.send(
                    "⚠️ Could not read that time after a few tries. "
                    "Run `/setup_birthdays` to start over."
                )
                return
            await channel.send(
                f"⚠️ Could not read **`{time_raw}`** as a time. "
                f"Try `8:00am`, `12:00pm`, or `08:00`. Let's try once more."
            )

    # ── Step 9: Birthday DM body (💎 Premium) ─────────────────────────────────
    # Customisable body of the per-member birthday DM that fires alongside
    # the channel announcement on Premium guilds. Free guilds can configure
    # now — it just won't fire until they have Premium + Member Roster Sync
    # AND a Discord ID column wired up in the birthday sheet.
    birthday_dm_message = ""
    if reminders_enabled:
        from train_cog import DEFAULT_BIRTHDAY_DM
        saved_birthday_dm = (current.get("dm_message") or "").strip()
        bday_dm_input = await ask_keep_or_change(
            channel,
            "**Step 9 of 9 — Birthday DM Body (💎 Premium)**\n"
            "When a birthday fires, the bot also DMs the member directly with a personal "
            "note. Free guilds can configure this now — it just won't fire until you have "
            "Premium + Member Roster Sync + a Discord ID column in your birthday sheet.\n\n"
            "Use `{name}` as a placeholder for the member's name.",
            default=DEFAULT_BIRTHDAY_DM,
            current=saved_birthday_dm,
            modal_title="Birthday DM Body",
            modal_label="DM body (max 1000 chars)",
            timeout_cmd="setup_birthdays",
            cancel_event=cancel_event,
        )
        if bday_dm_input is None:
            return
        birthday_dm_message = "" if bday_dm_input == DEFAULT_BIRTHDAY_DM else bday_dm_input

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_birthday_config
    save_birthday_config(
        guild_id        = guild_id,
        tab_name        = tab_name,
        name_col        = name_col,
        birthday_col    = birthday_col,
        discord_id_col  = discord_id_col,
        data_start_row  = 2,
        enabled         = 1,
        train_integration   = train_integration,
        flexible_placement  = flexible_placement,
        lookahead_days      = lookahead_days,
        reminders_enabled   = reminders_enabled,
        reminder_channel_id = reminder_channel_id,
        reminder_time       = reminder_time,
        dm_message          = birthday_dm_message,
    )

    embed = discord.Embed(title="✅ Birthday Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",           value=tab_name,                            inline=True)
    embed.add_field(name="Name Column",         value=f"Column {index_to_letter(name_col)}",     inline=True)
    embed.add_field(name="Birthday Column",     value=f"Column {index_to_letter(birthday_col)}", inline=True)
    embed.add_field(name="Discord ID Column",   value=f"Column {index_to_letter(discord_id_col)}" if discord_id_col >= 0 else "Not stored", inline=True)
    embed.add_field(name="Train Integration",   value="Enabled" if train_integration else "Disabled", inline=True)
    if train_integration:
        embed.add_field(name="Placement",       value="Flexible (±1 day)" if flexible_placement else "Birthday only", inline=True)
        embed.add_field(name="Lookahead",       value=f"{lookahead_days} days",           inline=True)
    embed.add_field(name="Reminders",           value="Enabled" if reminders_enabled else "Disabled", inline=True)
    if reminders_enabled:
        embed.add_field(name="Reminder Channel", value=f"<#{reminder_channel_id}>",       inline=True)
        embed.add_field(name="Reminder Time",    value=reminder_time,                     inline=True)
    embed.set_footer(text="Run /setup_birthdays again to update these settings.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Birthday config saved for guild {guild_id}")

async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

"""
setup_cog.py — /setup_* wizards for new guilds

Walks a server admin through configuring the bot using Discord's native
role and channel select menus. All values are saved to the config database.

Holds the `/setup` slash command (which opens the setup hub from
setup_hub.py) plus every per-feature wizard handler the hub
dispatches into. The 11 pre-#201 `/setup_*` slash commands
collapsed into hub buttons; their bodies are now module-level
`_launch_*_setup` helpers exposed at the bottom of this file.
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from config import (
    get_config, get_or_create_config, save_config, update_config_field,
    GuildConfig, normalize_spreadsheet_id,
)
import premium
import wizard_registry
from messages import (
    GENERIC_CMD_TIMEOUT,
    WIZARD_TIMEOUT,
)
from setup_hub import (
    HUB_BTN_BIRTHDAYS,
    HUB_BTN_BREAKDOWN,
    HUB_BTN_EVENTS,
    HUB_BTN_GROWTH,
    HUB_BTN_SHINY,
    HUB_BTN_SURVEY,
    HUB_BTN_TRAIN,
)
from storm_event_hub import (
    HUB_COMMAND,
    HUB_BTN_POST_SIGNUP,
    HUB_BTN_PRESETS,
    HUB_BTN_RULES,
)
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


def _format_24h_to_12h(raw: str) -> str:
    """Inverse of `_parse_12h_time`: render a stored 'HH:MM' 24-hour
    value as e.g. '9:00am' for display in wizards. Pass-through on
    empty / unparseable input so callers can pipe it through unchanged
    when no saved value is present. Used wherever a setup step shows
    a saved time back to leadership and the default it could revert
    to is in 12-hour form — otherwise the 'Keep current' and 'Use
    default' buttons sit side-by-side in mismatched formats."""
    if not raw or ":" not in raw:
        return raw or ""
    try:
        h_str, m_str = raw.split(":", 1)
        hour, minute = int(h_str), int(m_str)
    except ValueError:
        return raw
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return raw
    period = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d}{period}"


def _format_time_with_tz(time_str: str, tz_name: str | None) -> str:
    """Render a stored 'HH:MM' 24-hour time as e.g. '8:00am EDT' using
    the guild's configured timezone. Used everywhere a wizard summary
    or `/setup` → 🗂️ View configuration shows a saved time back to leadership —
    bare '08:00' leaves them guessing which timezone the reminder
    fires in.

    The tz abbreviation comes from `dt.tzname()` anchored on today's
    date, so the suffix reflects DST for the current date ('EST' in
    winter vs. 'EDT' in summer).

    Falls back gracefully:
      * empty / `*not set*` / non-time strings → returned unchanged,
        so callers can pipe sentinels through without a separate guard;
      * unparseable HH:MM → returned unchanged;
      * unknown tz_name → bare 12-hour form without a tz suffix.
    """
    if not time_str or ":" not in str(time_str):
        return time_str or ""
    try:
        h_str, m_str = str(time_str).split(":", 1)
        hour, minute = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        return time_str
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return time_str
    period = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    base   = f"{hour12}:{minute:02d}{period}"
    if not tz_name:
        return base
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        tz    = ZoneInfo(tz_name)
        today = datetime.now(tz=tz).date()
        dt    = datetime(today.year, today.month, today.day,
                         hour, minute, tzinfo=tz)
    except Exception:
        return base
    abbr = dt.tzname()
    return f"{base} {abbr}" if abbr else base


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
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected_role = None
        self.confirmed     = False
        self._placeholder  = placeholder

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
                label=f"✅ Keep current: @{role.name}"[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep_cb(inter: discord.Interaction):
                self.selected_role = role
                self.confirmed     = True
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
            min_values=1, max_values=1, row=select_row,
        )

        async def _select_cb(interaction: discord.Interaction):
            self.selected_role = select.values[0]
            self.confirmed     = True
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
            label=f"✅ Keep current: {display}"[:80],
            style=discord.ButtonStyle.success,
            row=row,
        )

        async def _keep_cb(inter: discord.Interaction):
            self.selected_channel = self._current_channel
            self.confirmed        = True
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
            label="📢 Channel", style=discord.ButtonStyle.primary, row=button_row,
        )
        ch_btn.callback = _on_channel
        self.add_item(ch_btn)

        th_btn = discord.ui.Button(
            label="🧵 Thread", style=discord.ButtonStyle.primary, row=button_row,
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
        select_row    = 1 if keep_added else 0
        secondary_row = select_row + 1

        types = self._channel_types_for_select()
        select = discord.ui.ChannelSelect(
            placeholder=self._placeholder,
            min_values=1, max_values=1,
            channel_types=types, row=select_row,
        )

        async def _select_cb(inter: discord.Interaction):
            self.selected_channel = select.values[0]
            self.confirmed        = True
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
                style=discord.ButtonStyle.secondary, row=secondary_row,
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
        select_row    = 1 if keep_added else 0
        secondary_row = select_row + 1

        # Sort so the dropdown groups threads under their parent and is
        # alphabetised within each group — easier for the user to find.
        sorted_threads = sorted(
            self._pickable_threads,
            key=lambda t: ((t.parent.name if t.parent else "zzz"), t.name),
        )

        thread_select = discord.ui.Select(
            placeholder="Pick a thread...",
            min_values=1, max_values=1, row=select_row,
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
                style=discord.ButtonStyle.secondary, row=secondary_row,
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
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.modal           = modal
        self.confirmed       = False
        self._current_value  = current_value
        self._current_display = current_display or current_value

        if current_value:
            keep_btn = discord.ui.Button(
                label=f"✅ Keep current: {self._current_display}"[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep_cb(inter: discord.Interaction):
                if on_keep_current is not None:
                    on_keep_current(self.modal)
                else:
                    self.modal.value = current_value
                self.confirmed   = True
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
        **✅ Keep current: {current}** / **✏️ Define my own**. Labels
        as "Keep current" rather than "Use default" so leadership
        running the wizard a second time sees what's actually saved
        (the values are identical anyway, but the wording makes the
        Keep-current intent obvious — fixes the
        "is Use default going to wipe my settings?" anxiety).
      * Saved value differs from default: three-button layout
        **✅ Keep current: {current}** / **↩️ Use default: {default}**
        / **✏️ Define my own**. Lets leadership revert to the
        hardcoded baseline in one click instead of typing it manually.

    The button labels include the value so the prompt body never has to
    repeat it. Returns None on timeout (and posts a timeout message
    referencing `timeout_cmd` if provided), or on /cancel (silently —
    the /cancel command itself acks the user).
    """
    has_saved            = bool(current)
    has_distinct_current = has_saved and current != default
    pre_filled           = current if has_saved else default

    class KeepOrChangeDefaultView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_TIMEOUT)
            self.value     = None
            self.confirmed = False

            # Build buttons explicitly so we can vary the layout based on
            # whether anything is saved and whether it matches default.
            # Decorator-based buttons can't be conditionally added.
            keep_label = (
                f"✅ Keep current: {current}"[:80]
                if has_saved else
                f"✅ Use default: {default}"[:80]
            )
            keep_btn = discord.ui.Button(label=keep_label, style=discord.ButtonStyle.success)

            async def _keep_cb(inter: discord.Interaction):
                chosen         = current if has_saved else default
                self.value     = chosen
                self.confirmed = True
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"{prompt}\n\n✅ Using **{chosen}**", view=self
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
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=f"{prompt}\n\n✅ Reverted to default: **{default}**", view=self
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


async def ask_proceed_with_existing_config(
    channel,
    *,
    title: str,
    description: str,
    fields: list[tuple[str, str]],
    cancel_event,
    no_changes_message: str = "✅ No changes made. Your existing configuration is still active.",
) -> bool | None:
    """Show an existing-config summary embed with Edit / No changes buttons.

    Each per-feature `/setup_*` calls this at the top so leadership sees
    what's saved without walking the whole wizard. Returns:

      * ``True``  — leadership clicked Edit. Caller should proceed into
        the wizard's step-by-step flow.
      * ``False`` — leadership clicked No changes. This helper has
        already posted ``no_changes_message``; caller should return.
      * ``None``  — `/cancel` or timeout. Caller should return silently
        (timeout case posts no message — keep parity with the other
        cancellable views in the wizard).

    `fields` is a list of ``(label, value)`` tuples rendered as
    embed fields, inline=False. Pass the same tuples that
    ``/setup` → 🗂️ View configuration` would render for that feature.
    """

    class EditOrCancelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.proceed = None

        @discord.ui.button(label="✏️ Edit settings", style=discord.ButtonStyle.primary)
        async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
            self.proceed = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✅ No changes needed", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
            self.proceed = False
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
    )
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)

    view = EditOrCancelView()
    await channel.send(embed=embed, view=view)
    await wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return None
    if view.proceed is None:
        # Timed out without an interaction.
        return None
    if not view.proceed:
        await channel.send(no_changes_message)
        return False
    return True


async def ask_disable_with_clear(
    channel,
    *,
    feature_label: str,
    setup_command: str,
    had_prior_config: bool,
    clear_fn,
    cancel_event,
) -> None:
    """Post the disable confirmation after leadership picks No on an
    enable-toggle wizard step.

    When ``had_prior_config`` is True, the message tells leadership their
    saved config is preserved and shows a Clear button that calls
    ``clear_fn()`` to wipe it. When False (first-time disable, nothing
    saved to lose), just posts the bare confirmation without the button.

    ``feature_label`` — friendly noun for the message body
    (e.g. "Shiny Tasks announcement").

    ``setup_command`` — slash navigation leadership should re-run to
    re-enable, sans the leading slash (e.g. "setup → 🌟 Shiny Tasks").
    Post-#201 every wizard lives behind a /setup hub button; pass the
    hub navigation hint here so the rendered message reads
    "Re-run `/setup → 🌟 Shiny Tasks` and pick Yes to restore."

    ``clear_fn`` — callable taking no arguments; runs synchronously
    or via ``await`` (the helper auto-detects). Should wipe the
    feature's saved config so a future re-enable starts clean. Each
    wizard supplies its own — typically a ``DELETE FROM <table>
    WHERE guild_id = ?`` since ``get_*_config`` already returns a
    default dict when the row is absent.
    """
    import inspect

    if not had_prior_config:
        await channel.send(f"✅ {feature_label} disabled.")
        return

    body = (
        f"✅ {feature_label} disabled. Your previous configuration is saved. "
        f"Re-run `/{setup_command}` and pick Yes to restore it instantly."
    )

    class ClearConfigView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            self.message: discord.Message | None = None
            self.cleared = False

        @discord.ui.button(
            label="🗑️ Clear my saved configuration",
            style=discord.ButtonStyle.danger,
        )
        async def clear(self, inter: discord.Interaction, button: discord.ui.Button):
            try:
                if inspect.iscoroutinefunction(clear_fn):
                    await clear_fn()
                else:
                    clear_fn()
            except Exception as e:
                await wizard_registry.safe_edit_response(
                    inter,
                    content=(
                        f"{body}\n\n⚠️ Could not clear configuration: {e}"
                    ),
                    view=None,
                )
                self.stop()
                return
            self.cleared = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ {feature_label} disabled and saved configuration cleared.",
                view=self,
            )
            self.stop()

        async def on_timeout(self):
            await wizard_registry.expire_view_message(
                self.message, command_hint=f"`/{setup_command}`",
            )

    view = ClearConfigView()
    view.message = await channel.send(body, view=view)
    await wait_view_or_cancel(view, cancel_event)


async def _manage_train_templates(
    *, bot, channel, check, existing: list, default_name: str,
    cap: int | None, cancel_event,
):
    """
    Multi-template manager for the train setup wizard.

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
            title="**Step 6 of 8 — Prompt Templates**",
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
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

            @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=0)
            async def edit_btn(self, inter, button):
                self.action = "edit"
                for c in self.children: c.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

            @discord.ui.button(label="⭐ Set Default", style=discord.ButtonStyle.secondary, row=0)
            async def set_default_btn(self, inter, button):
                self.action = "default"
                for c in self.children: c.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

            @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=1)
            async def delete_btn(self, inter, button):
                self.action = "delete"
                for c in self.children: c.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

            @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=1)
            async def done_btn(self, inter, button):
                self.action = "done"
                for c in self.children: c.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
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
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
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
                        await wizard_registry.safe_edit_response(inter, view=self)
                        self.stop()
                    sel.callback = _cb
                    self.add_item(sel)

            pick = PickView()
            await channel.send("Which template?", view=pick)
            await wait_view_or_cancel(pick, cancel_event)
            if pick.cancelled:
                return None, None
            if pick.idx is None:
                await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
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
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
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
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
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
    `/setup` and any setup-hub launcher that opens a wizard in the
    current channel.
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

    @app_commands.command(name="setup", description="Open the setup hub — foundations + every feature wizard, in one place")
    async def setup(self, interaction: discord.Interaction):
        from setup_hub import handle_setup_hub
        await handle_setup_hub(self.bot, interaction)


async def _send_ack(interaction: discord.Interaction, message: str) -> None:
    """Send an ephemeral ack via whichever path the interaction state
    allows. The launcher helpers below are reachable from two entry
    points — the /setup hub's slash command (fresh response slot) and
    the storm event hub's `⚙️ Open setup` button callback (response
    slot already consumed by `safe_edit_response` disabling the button
    row). `response.send_message` only works in the fresh case;
    `followup.send` only works after the response slot is consumed.
    Branch on `response.is_done()` so the helpers don't care which
    caller invoked them.
    """
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# Standalone launcher helpers — extracted from the pre-#201 per-feature
# `/setup_*` slash commands so the setup hub's button callbacks can
# dispatch into the existing wizard functions without re-instantiating
# the cog. Mirrors the `open_strategy_list` / `open_member_rule_list`
# pattern from the storm hub (#187).

async def _launch_train_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(interaction, "⛔ You need the leadership role (or admin) to open the train wizard.")
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting train setup — check the channel for prompts!")
    await run_train_setup(interaction, bot)


async def _launch_growth_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(interaction, "⛔ You need the leadership role (or admin) to open the growth wizard.")
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting growth tracking setup — check the channel for prompts!")
    await run_growth_setup(interaction, bot)


async def _launch_growth_breakdown_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(interaction, "⛔ You need the leadership role (or admin) to open the breakdown wizard.")
        return
    if not await premium.is_premium(interaction.guild_id, interaction=interaction):
        await _send_ack(
            interaction,
            "💎 Growth Breakdown configuration is a Premium feature. The "
            "**📊 See most recent Breakdown** button on `/growth overview` "
            "(and `/growth breakdown`) works on every tier — this wizard "
            "configures the auto-post and the customizable thresholds and "
            "labels. Run `/upgrade` to subscribe.",
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting Growth Breakdown setup — check the channel for prompts!")
    await run_growth_breakdown_setup(interaction, bot)


async def _launch_birthday_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(interaction, "⛔ You need the leadership role (or admin) to open the birthday wizard.")
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting birthday setup — check the channel for prompts!")
    await run_birthday_setup(interaction, bot)


async def _launch_storm_setup(interaction: discord.Interaction, bot, event_type: str) -> None:
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    if not _has_leadership_or_admin(interaction):
        await _send_ack(interaction, f"⛔ You need the leadership role (or admin) to open the {label} wizard.")
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, f"⚙️ Starting {label} setup — check the channel for prompts!")
    await run_storm_setup(interaction, bot, event_type)


async def _launch_event_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(interaction, "⛔ You need the leadership role (or admin) to open the event wizard.")
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting event setup — check the channel for prompts!")
    await run_event_setup(interaction, bot)


async def _launch_survey_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(interaction, "⛔ You need the leadership role (or admin) to open the survey wizard.")
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting survey setup — check the channel for prompts!")
    await run_survey_setup(interaction, bot)


async def _launch_shiny_tasks_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(interaction, "⛔ You need the leadership role (or admin) to open the shiny-tasks wizard.")
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting Shiny Tasks setup — check the channel for prompts!")
    await run_shiny_tasks_setup(interaction, bot)


async def _run_reset_flow(interaction: discord.Interaction) -> None:
    """Reset confirmation flow, extracted from the pre-#201
    `/setup` → 🗑️ Reset configuration slash command so the setup hub's `🗑️ Reset configuration`
    button can call it without round-tripping through a slash command."""
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "⛔ Only server administrators can reset the configuration.",
            ephemeral=True,
        )
        return

    class ConfirmResetView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.confirmed = False

        @discord.ui.button(label="Yes, reset everything", style=discord.ButtonStyle.danger)
        async def confirm(self, inner: discord.Interaction, _b: discord.ui.Button):
            self.confirmed = True
            await inner.response.defer()
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inner: discord.Interaction, _b: discord.ui.Button):
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
    """Single dropdown covering all supported timezones, ordered by (UTC offset.

    When `current` is passed and matches a known timezone option, the
    view prepends a green Keep-current button above the select so
    leadership doesn't have to re-pick their timezone on a re-run.
    """
    def __init__(self, *, current: str | None = None):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected  = None
        self.confirmed = False
        self.current   = current

        keep_added = False
        if current and current in TIMEZONE_LABELS:
            keep_label = TIMEZONE_LABELS[current]
            keep_btn = discord.ui.Button(
                label=f"✅ Keep current: {keep_label}"[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep_cb(inter: discord.Interaction):
                self.selected  = current
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content=f"✅ Keeping timezone: **{keep_label}**", view=self,
                )
                self.stop()
            keep_btn.callback = _keep_cb
            self.add_item(keep_btn)
            keep_added = True

        select = discord.ui.Select(
            placeholder="Select your timezone...",
            options=[
                discord.SelectOption(label=label[:100], value=tz)
                for tz, label in TIMEZONE_OPTIONS
            ],
            row=1 if keep_added else 0,
        )

        async def _cb(interaction: discord.Interaction):
            self.selected    = select.values[0]
            self.confirmed   = True
            select.disabled  = True
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
        self.selected  = None

    @discord.ui.button(label="🔁 Repeating cycle", style=discord.ButtonStyle.primary)
    async def repeating(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "repeating"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(
            interaction, content="✅ Schedule: **Repeating cycle**", view=self
        )
        self.stop()

    @discord.ui.button(label="📅 Add manually each time", style=discord.ButtonStyle.secondary)
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


# ── 🗂️ View configuration helper (used by the /setup hub button) ─────────────

async def _send_view_configuration(interaction: discord.Interaction, cfg) -> None:
    """Build and send a single embed summarising every wizard's configuration."""
    await interaction.response.defer(ephemeral=True)

    from config import (
        get_train_config, get_birthday_config, get_storm_config,
        get_survey_config, get_growth_config, get_guild_events,
        get_shiny_tasks_config,
    )
    guild_id = interaction.guild_id
    train    = get_train_config(guild_id)
    birthday = get_birthday_config(guild_id)
    ds       = get_storm_config(guild_id, "DS")
    cs       = get_storm_config(guild_id, "CS")
    survey   = get_survey_config(guild_id)
    growth   = get_growth_config(guild_id)
    shiny    = get_shiny_tasks_config(guild_id)
    events   = get_guild_events(guild_id, active_only=True)
    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)

    def _yn(v) -> str:
        return "✅ Configured" if v else "❌ Not configured"

    def _enabled(v) -> str:
        return "✅ Enabled" if v else "❌ Disabled"

    def _channel(v) -> str:
        return f"<#{v}>" if v else "*not set*"

    def _col_letter(idx) -> str:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return "*not set*"
        return _col_index_to_letter(i) if i >= 0 else "*not set*"

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
        f"**Draft Time:** {_format_time_with_tz(cfg.event_draft_time, cfg.timezone)}",
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
    embed.add_field(name=HUB_BTN_EVENTS, value="\n".join(ev_lines)[:1024], inline=False)

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
        train_lines.append(f"**Reminder Time:** {_format_time_with_tz(train.get('reminder_time'), cfg.timezone) or '*not set*'}")
    embed.add_field(name=HUB_BTN_TRAIN, value="\n".join(train_lines)[:1024], inline=False)

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
        b_lines.append(f"**Reminder Time:** {_format_time_with_tz(birthday.get('reminder_time'), cfg.timezone) or '*not set*'}")
    embed.add_field(name=HUB_BTN_BIRTHDAYS, value="\n".join(b_lines)[:1024], inline=False)

    from config import get_storm_slot_labels
    ds_slot_labels = get_storm_slot_labels("DS", interaction.guild_id)
    cs_slot_labels = get_storm_slot_labels("CS", interaction.guild_id)

    def _team_time_line(team_letter: str, idx, slot_lbls, setup_hint: str) -> str:
        """Render the Team A/B time line for the /setup config view.
        Falls back to a nudge toward the setup wizard when the alliance
        hasn't picked the slot yet (#251)."""
        if idx in (1, 2) and len(slot_lbls) >= idx:
            return f"**Team {team_letter} Time:** {slot_lbls[idx - 1]}"
        return f"**Team {team_letter} Time:** *not set — Step 3 of {setup_hint}*"

    ds_hint = "`/setup` → ⚔️ Desert Storm"
    cs_hint = "`/setup` → 🏜️ Canyon Storm"
    ds_lines = [
        f"**Sheet Tab:** {ds.get('tab_name', '*not set*')}",
        f"**Log Channel:** {_channel(cfg.ds_log_channel_id)}",
        _team_time_line("A", ds.get("team_a_slot_index"), ds_slot_labels, ds_hint),
        _team_time_line("B", ds.get("team_b_slot_index"), ds_slot_labels, ds_hint),
        f"**Mail Template:** {_yn(ds.get('mail_template'))}",
    ]
    embed.add_field(name="⚔️ Desert Storm", value="\n".join(ds_lines)[:1024], inline=False)

    cs_lines = [
        f"**Sheet Tab:** {cs.get('tab_name', '*not set*')}",
        f"**Log Channel:** {_channel(cfg.cs_log_channel_id)}",
        _team_time_line("A", cs.get("team_a_slot_index"), cs_slot_labels, cs_hint),
        _team_time_line("B", cs.get("team_b_slot_index"), cs_slot_labels, cs_hint),
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
    embed.add_field(name=HUB_BTN_GROWTH, value="\n".join(g_lines)[:1024], inline=False)

    st_lines = [f"**Enabled:** {_enabled(shiny.get('enabled'))}"]
    if shiny.get("enabled"):
        st_lines += [
            f"**Channel:** {_channel(shiny.get('channel_id'))}",
            f"**Post Time:** {_format_time_with_tz(shiny.get('post_time'), cfg.timezone) or '*not set*'}",
            f"**Server Range:** "
            f"{shiny.get('server_min') or '?'} – {shiny.get('server_max') or '?'}",
            f"**Custom Message:** {_yn(shiny.get('message_template'))}",
        ]
    embed.add_field(name="🌟 Shiny Tasks", value="\n".join(st_lines)[:1024], inline=False)

    if is_premium_flag:
        embed.set_footer(text="💎 Premium is active. Run /setup and click a section button to update it.")
    else:
        embed.set_footer(text="Run /upgrade for Premium • /help for all commands • /setup to update a section")
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
        sheet_display = (
            f"`{cfg.spreadsheet_id[:20]}...`" if cfg.spreadsheet_id else "Not set"
        )
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="⚙️ Current Core Setup",
            description="Your server is already configured. Would you like to edit these settings?",
            fields=[
                ("Member Role",        cfg.member_role_name),
                ("Leadership Role",    cfg.leadership_role_name),
                ("Leadership Channel", f"<#{cfg.leadership_channel_id}>"),
                ("Timezone",           tz_label),
                ("Sheet ID",           sheet_display),
            ],
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Your existing setup is still active.",
        )
        if proceed is not True:
            return

    await channel.send(
        "⚙️ **Alliance Helper Setup**\n\n"
        "I'll walk you through the core configuration for your server. "
        "This covers your roles, leadership channel, timezone and Google Sheet.\n\n"
        "*You can run `/setup` again at any time to update these settings.*"
    )

    # ── Step 1: Member role ────────────────────────────────────────────────────
    await channel.send("**Step 1 of 6 — Member Role**\nSelect the role that all alliance members have:")
    v = RoleSelectStep(
        "Select member role...",
        current_id=cfg.member_role_id,
        current_name=cfg.member_role_name,
        guild=interaction.guild,
    )
    if v.is_current_stale:
        await channel.send(
            f"⚠️ Your previously configured member role **{cfg.member_role_name}** "
            "no longer exists. Pick a new one below."
        )
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    cfg.member_role_name = v.selected_role.name
    cfg.member_role_id   = v.selected_role.id

    # ── Step 2: Leadership role ────────────────────────────────────────────────
    await channel.send("**Step 2 of 6 — Leadership Role**\nSelect the elevated role for alliance leadership:")
    v = RoleSelectStep(
        "Select leadership role...",
        current_id=cfg.leadership_role_id,
        current_name=cfg.leadership_role_name,
        guild=interaction.guild,
    )
    if v.is_current_stale:
        await channel.send(
            f"⚠️ Your previously configured leadership role **{cfg.leadership_role_name}** "
            "no longer exists. Pick a new one below."
        )
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    cfg.leadership_role_name = v.selected_role.name
    cfg.leadership_role_id   = v.selected_role.id

    # ── Step 3: Leadership channel ─────────────────────────────────────────────
    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)
    await channel.send(
        "**Step 3 of 6 — Leadership Channel**\n"
        "Pick the channel where I should post drafts, reminders, and approvals "
        "by default. Individual features (events, train reminders, etc.) can "
        "override this with their own channel later."
    )
    v = ChannelSelectStep(
        "Select leadership channel...",
        suggested_name="leadership",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=cfg.leadership_channel_id,
    )
    if v.is_current_stale:
        await channel.send(
            "⚠️ Your previously configured leadership channel no longer exists. "
            "Pick a new one below."
        )
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    cfg.leadership_channel_id = v.selected_channel.id

    # ── Step 4: Timezone ───────────────────────────────────────────────────────
    tz_view = TimezoneSelectView(current=cfg.timezone)
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
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    cfg.timezone = tz_view.selected

    # ── Step 5: Google Sheet ID ────────────────────────────────────────────────
    await channel.send(
        "**Step 5 of 6 — Google Sheet ID**\n"
        "Enter your Google Sheet ID — the long string from your sheet's URL:\n"
        "`https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID`**`/edit`\n"
        "*(You can paste the whole URL and I'll pull the ID out.)*"
    )
    modal   = TextInputModal("Google Sheet ID", "Sheet ID", placeholder="Paste your Sheet ID here...")
    # Truncate long sheet ids for the Keep-current button label —
    # full ids are ~44 chars and overflow Discord's 80-char cap once
    # the "✅ Keep current: " prefix is added.
    sheet_display = (
        f"{cfg.spreadsheet_id[:25]}…"
        if cfg.spreadsheet_id and len(cfg.spreadsheet_id) > 25
        else cfg.spreadsheet_id
    )
    modal_v = ModalLaunchView(
        modal,
        current_value=cfg.spreadsheet_id or None,
        current_display=sheet_display or None,
    )
    await channel.send("\u200b", view=modal_v)
    await wait_view_or_cancel(modal_v, cancel_event)
    if modal_v.cancelled:
        return
    if not modal_v.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    sheet_id = normalize_spreadsheet_id(modal.value)

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
        "Now configure whichever features you want to use. Run `/setup` "
        "again to re-open the hub — every feature wizard lives behind a "
        "labelled button:\n\n"
        "📣 **Events** — Event announcements (Plague Marauder, Zombie Siege, etc.)\n"
        "🚂 **Train** — Train schedule, blurb generation, and reminders\n"
        "🎂 **Birthdays** — Birthday tracking and announcements\n"
        "⚔️ **Desert Storm** — Mail drafts and participation logs\n"
        "🏜️ **Canyon Storm** — Mail drafts and participation logs\n"
        "📋 **Survey** — Squad powers survey\n"
        "📈 **Growth** — Growth tracking (snapshot your members' stats over time)\n"
        "🌟 **Shiny Tasks** — Daily announcement of today's shiny task servers for your Alliance\n\n"
        "Premium features (👥 Member Sync, 📋 Survey, 📊 Growth Breakdown) show as 💎-locked "
        "until you upgrade. Use `/help` any time to see every command."
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
                await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
            return None
        return reply.content.strip()[:max_chars]

    from config import (
        get_growth_config, save_growth_config,
        has_growth_config, clear_growth_config,
    )
    current = get_growth_config(guild_id)
    growth_already_configured = has_growth_config(guild_id)

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

    # ── If already enabled, show summary and offer edit or cancel ─────────────
    if growth_already_configured and current.get("enabled"):
        metrics_list = current.get("metrics") or []
        freq = current.get("snapshot_frequency", "monthly")
        if freq == "monthly":
            sched = f"Monthly on day {current.get('snapshot_day', 1)}"
        else:
            sched = f"Every {current.get('snapshot_interval', 30)} days"
        fields = [
            ("Source Tab",        current.get("tab_source") or "*not set*"),
            ("Name Column",       f"Column {current.get('name_col') or '*not set*'}"),
            ("Data Start Row",    str(current.get("data_start_row") or "*not set*")),
            ("Growth Tab",        current.get("tab_growth") or "*not set*"),
            ("Snapshot Schedule", sched),
            (
                f"Metrics ({len(metrics_list)})",
                "\n".join(f"• {m['label']} — column {m['col']}" for m in metrics_list)
                if metrics_list else "*none*",
            ),
        ]
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="📈 Current Growth Setup",
            description="Growth tracking is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Growth tracking is still active.",
        )
        if proceed is not True:
            return

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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
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
        await ask_disable_with_clear(
            channel,
            feature_label="Growth tracking",
            setup_command=f"setup → {HUB_BTN_GROWTH}",
            had_prior_config=growth_already_configured,
            clear_fn=lambda: clear_growth_config(guild_id),
            cancel_event=cancel_event,
        )
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
        await channel.send(f"⚠️ Please enter a row number like `2`. Run `/setup` → {HUB_BTN_GROWTH} to try again.")
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
        await channel.send(f"⚠️ Please enter a single column letter like `A`. Run `/setup` → {HUB_BTN_GROWTH} to try again.")
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
        metrics_cap = await premium.get_limit("growth_metrics", guild_id, interaction=interaction, bot=interaction.client)
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
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
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
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

        pick_view = PickMetricView()
        verb = "edit" if action_view.choice == "edit" else "delete"
        await channel.send(f"Which metric do you want to {verb}?", view=pick_view)
        await wait_view_or_cancel(pick_view, cancel_event)
        if pick_view.cancelled:
            return
        if pick_view.index is None:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
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
        await channel.send(f"⚠️ No metrics defined. Run `/setup` → {HUB_BTN_GROWTH} to try again.")
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
    custom_interval_unlocked = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)

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
            await wizard_registry.safe_edit_response(inter, content="✅ Frequency: **Monthly**", view=self)
            self.stop()

        @discord.ui.button(label="🔁 Custom interval (every X days) 💎", style=discord.ButtonStyle.secondary)
        async def custom(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "interval"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
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
            f"Run `/growth overview` and click **📸 Run Snapshot Now**.*"
        )
    else:
        next_value = "*Could not compute — check `/growth overview` for status.*"

    embed = discord.Embed(title="✅ Growth Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Source Tab",        value=tab_source,           inline=False)
    embed.add_field(name="Name Column",       value=f"Column {name_col}", inline=False)
    embed.add_field(name="Data Start Row",    value=str(data_start_row),  inline=False)
    embed.add_field(name="Growth Tab",        value=tab_growth,           inline=False)
    embed.add_field(name="Snapshot Schedule", value=freq_desc,            inline=False)
    embed.add_field(name="Next Snapshot",     value=next_value,           inline=False)
    embed.add_field(name="Metrics",           value=metrics_display,      inline=False)
    embed.set_footer(text=f"Run /setup and click {HUB_BTN_GROWTH} to update. Use /growth overview to take a manual snapshot.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Growth config saved for guild {guild_id}")


async def run_growth_breakdown_setup(interaction: discord.Interaction, bot):
    """Premium-only wizard for the Growth Breakdown auto-post + customization.

    The bucket-classification math itself ships free (`/growth breakdown`,
    and the **📊 See most recent Breakdown** button on `/growth overview`, both read the breakdown tab for any guild that's
    enabled growth tracking). This wizard configures the Premium layer:

      * sheet tab name for the breakdown
      * auto-post channel (fires after every snapshot, premium-gated at
        post time so a subscription lapse stops the alerts without any
        config change)
      * bucket filter (which buckets fire the auto-post — e.g. only
        Decline + None)
      * custom thresholds applied to every metric (global, not per-metric
        — per-metric customization is parked as a follow-up if alliances
        ask for it)
      * custom labels for each bucket
    """
    import wizard_registry
    from config import (
        get_growth_config, save_growth_breakdown_config,
        has_growth_breakdown_config,
    )
    from growth import DEFAULT_THRESHOLDS, DEFAULT_BUCKET_LABELS, BUCKET_ORDER

    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    current = get_growth_config(guild_id)
    if not current.get("enabled") or not current.get("metrics"):
        await channel.send(
            f"⚙️ Set up growth tracking first — run `/setup` → {HUB_BTN_GROWTH} and add at "
            f"least one metric, then come back to `/setup` → {HUB_BTN_BREAKDOWN} to "
            "configure the breakdown layer."
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    # ── If already configured, show summary and offer edit or cancel ─────────
    if has_growth_breakdown_config(guild_id):
        post_ch = current.get("breakdown_post_channel_id") or 0
        thresholds = current.get("breakdown_thresholds") or {}
        labels     = current.get("breakdown_labels") or {}
        bucket_filter = current.get("breakdown_bucket_filter") or []
        fields = [
            ("Breakdown Tab", current.get("tab_breakdown") or "Growth Breakdown"),
            ("Auto-Post Channel", f"<#{post_ch}>" if post_ch else "❌ Off"),
        ]
        if post_ch and bucket_filter:
            fields.append((
                "Bucket Filter",
                ", ".join(DEFAULT_BUCKET_LABELS.get(b, b) for b in bucket_filter),
            ))
        elif post_ch:
            fields.append(("Bucket Filter", "All buckets"))
        if thresholds:
            fields.append((
                "Custom Thresholds",
                f"Increased ≥ {thresholds.get('increased', 0):g}%, "
                f"Steady ≥ {thresholds.get('steady', 0):g}%, "
                f"Low ≥ {thresholds.get('low', 0):g}%, "
                f"None ≥ {thresholds.get('none', 0):g}%",
            ))
        if labels:
            fields.append((
                "Custom Labels",
                ", ".join(
                    f"{DEFAULT_BUCKET_LABELS[b]}→{labels[b]}"
                    for b in BUCKET_ORDER if labels.get(b)
                ) or "—",
            ))
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="📊 Current Growth Breakdown Setup",
            description="Growth breakdown is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Your breakdown setup is still active.",
        )
        if proceed is not True:
            return

    await channel.send(
        "📊 **Growth Breakdown Setup** (💎 Premium)\n"
        "Classifies each member's growth between snapshots into one of "
        "five buckets and (optionally) posts the summary to a channel "
        "after every snapshot."
    )

    # ── Step 1: Breakdown tab ─────────────────────────────────────────────
    tab_breakdown = await ask_keep_or_change(
        channel,
        "**Step 1 of 5 — Breakdown Tab**\n"
        "Which tab in your Google Sheet should the breakdown data live in? "
        "The bot creates it automatically if it doesn't exist yet.",
        default="Growth Breakdown",
        current=current.get("tab_breakdown") or "",
        modal_title="Breakdown Tab",
        modal_label="Tab name",
        timeout_cmd="setup_growth_breakdown",
        cancel_event=cancel_event,
    )
    if tab_breakdown is None:
        return

    # ── Step 2: Auto-post toggle + channel ────────────────────────────────
    autopost_view = YesNoView()
    await channel.send(
        "**Step 2 of 5 — Auto-Post After Snapshots?**\n"
        "Each time the bot finishes a snapshot, post the breakdown summary "
        "to a channel so leadership doesn't have to run `/growth breakdown` to see "
        "who's slowing down.",
        view=autopost_view,
    )
    await wait_view_or_cancel(autopost_view, cancel_event)
    if autopost_view.cancelled:
        return
    if autopost_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN))
        return

    post_channel_id = 0
    if autopost_view.selected:
        saved_post_ch = current.get("breakdown_post_channel_id") or 0
        post_ch_view = ChannelSelectStep(
            "Select the auto-post channel…",
            suggested_name="growth-breakdown",
            include_threads=True,
            guild=interaction.guild,
            current_id=saved_post_ch,
        )
        if post_ch_view.is_current_stale:
            await channel.send(
                "⚠️ Your previously configured breakdown channel no longer exists. "
                "Pick a new one below."
            )
        await channel.send(
            "**Auto-Post Channel**\n"
            "Where should the breakdown summaries land?",
            view=post_ch_view,
        )
        await wait_view_or_cancel(post_ch_view, cancel_event)
        if post_ch_view.cancelled:
            return
        if not post_ch_view.confirmed:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN))
            return
        post_channel_id = post_ch_view.selected_channel.id

    # ── Step 3: Bucket filter ─────────────────────────────────────────────
    # Surfaces only when auto-post is on — bucket filter doesn't apply to
    # the on-demand /growth button (which always shows every bucket).
    bucket_filter: list[str] = []
    if post_channel_id:
        saved_filter = current.get("breakdown_bucket_filter") or []

        class BucketFilterView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_TIMEOUT)
                self.selected: list[str] | None = None

                # Keep-current button on its own row when leadership
                # previously picked a filter. Sets `self.selected` to
                # the saved list and stops — no Select interaction
                # needed.
                if saved_filter:
                    saved_disp_short = ", ".join(
                        DEFAULT_BUCKET_LABELS.get(b, b) for b in saved_filter
                    )
                    keep_btn = discord.ui.Button(
                        label=f"✅ Keep current: {saved_disp_short}"[:80],
                        style=discord.ButtonStyle.success,
                        row=0,
                    )

                    async def _keep_cb(inter: discord.Interaction):
                        self.selected = list(saved_filter)
                        for item in self.children: item.disabled = True
                        await wizard_registry.safe_edit_response(inter, view=self)
                        self.stop()
                    keep_btn.callback = _keep_cb
                    self.add_item(keep_btn)

                opts = [
                    discord.SelectOption(
                        label=DEFAULT_BUCKET_LABELS[b],
                        value=b,
                        description=f"{b.title()} growth bucket",
                    )
                    for b in BUCKET_ORDER
                ]
                select = discord.ui.Select(
                    placeholder="Pick which buckets fire alerts (none = all)",
                    options=opts,
                    min_values=0,
                    max_values=len(BUCKET_ORDER),
                    row=1 if saved_filter else 0,
                )

                async def _select_cb(inter: discord.Interaction):
                    self.selected = list(select.values)
                    for item in self.children: item.disabled = True
                    await wizard_registry.safe_edit_response(inter, view=self)
                    self.stop()

                select.callback = _select_cb
                self.add_item(select)

                all_btn = discord.ui.Button(
                    label="Use all buckets",
                    style=discord.ButtonStyle.secondary,
                    row=2 if saved_filter else 1,
                )

                async def _all_cb(inter: discord.Interaction):
                    self.selected = []
                    for item in self.children: item.disabled = True
                    await wizard_registry.safe_edit_response(inter, view=self)
                    self.stop()

                all_btn.callback = _all_cb
                self.add_item(all_btn)

        bf_view = BucketFilterView()
        if saved_filter:
            saved_disp = ", ".join(
                DEFAULT_BUCKET_LABELS.get(b, b) for b in saved_filter
            )
            await channel.send(
                f"**Step 3 of 5 — Bucket Filter**\n"
                f"Currently alerting on: **{saved_disp}**. Pick a new set of "
                f"buckets, or hit **Use all buckets** to alert on every bucket.",
                view=bf_view,
            )
        else:
            await channel.send(
                "**Step 3 of 5 — Bucket Filter**\n"
                "Pick which buckets fire the auto-post — leave empty (or hit "
                "**Use all buckets**) to alert on every bucket.",
                view=bf_view,
            )
        await wait_view_or_cancel(bf_view, cancel_event)
        if bf_view.cancelled:
            return
        if bf_view.selected is None:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN))
            return
        bucket_filter = bf_view.selected

    # ── Step 4: Custom thresholds ─────────────────────────────────────────
    thresholds = dict(current.get("breakdown_thresholds") or {})

    class ThresholdsModal(discord.ui.Modal):
        def __init__(self):
            super().__init__(title="Custom Thresholds (%)")
            self.values_out: dict | None = None
            self._increased = discord.ui.TextInput(
                label="Increased ≥",
                placeholder="e.g. 20 — bucket lower bound, %",
                default=str(thresholds.get("increased", DEFAULT_THRESHOLDS["increased"])),
                required=True, max_length=6,
            )
            self._steady = discord.ui.TextInput(
                label="Steady ≥",
                placeholder="e.g. 10",
                default=str(thresholds.get("steady", DEFAULT_THRESHOLDS["steady"])),
                required=True, max_length=6,
            )
            self._low = discord.ui.TextInput(
                label="Low ≥",
                placeholder="e.g. 5",
                default=str(thresholds.get("low", DEFAULT_THRESHOLDS["low"])),
                required=True, max_length=6,
            )
            self._none = discord.ui.TextInput(
                label="None ≥",
                placeholder="0 — usually leave as 0. Decline is < 0.",
                default=str(thresholds.get("none", DEFAULT_THRESHOLDS["none"])),
                required=True, max_length=6,
            )
            for i in (self._increased, self._steady, self._low, self._none):
                self.add_item(i)

        async def on_submit(self, inter: discord.Interaction):
            try:
                self.values_out = {
                    "increased": float(self._increased.value),
                    "steady":    float(self._steady.value),
                    "low":       float(self._low.value),
                    "none":      float(self._none.value),
                }
            except (ValueError, TypeError):
                self.values_out = None
            await inter.response.defer()
            self.stop()

    class ThresholdsChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_TIMEOUT)
            self.choice = None

            # Keep-current button when leadership saved custom thresholds
            # on a previous run. Demoted "Use defaults" to a secondary
            # revert in that case so Keep current is the visually
            # primary action.
            if thresholds:
                for child in self.children:
                    if getattr(child, "label", None) == "✅ Use defaults":
                        child.label = "↩️ Use defaults"
                        child.style = discord.ButtonStyle.secondary
                        break
                keep_btn = discord.ui.Button(
                    label="✅ Keep current values",
                    style=discord.ButtonStyle.success,
                )

                async def _keep_cb(inter: discord.Interaction):
                    self.choice = "keep"
                    for item in self.children: item.disabled = True
                    await wizard_registry.safe_edit_response(inter, view=self)
                    self.stop()
                keep_btn.callback = _keep_cb
                self.add_item(keep_btn)

        @discord.ui.button(label="✅ Use defaults", style=discord.ButtonStyle.success)
        async def defaults_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "defaults"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✏️ Customize", style=discord.ButtonStyle.primary)
        async def customize_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            modal = ThresholdsModal()
            await inter.response.send_modal(modal)
            await modal.wait()
            self.choice = "customize" if modal.values_out else None
            self._modal_values = modal.values_out
            for item in self.children: item.disabled = True
            try:
                await inter.edit_original_response(view=self)
            except Exception:
                pass
            self.stop()

    t_view = ThresholdsChoiceView()
    t_view._modal_values = None
    await channel.send(
        "**Step 4 of 5 — Bucket Thresholds**\n"
        f"Defaults: Increased ≥ {DEFAULT_THRESHOLDS['increased']:.0f}%, "
        f"Steady ≥ {DEFAULT_THRESHOLDS['steady']:.0f}%, "
        f"Low ≥ {DEFAULT_THRESHOLDS['low']:.0f}%, "
        f"None ≥ {DEFAULT_THRESHOLDS['none']:.0f}%, "
        f"Decline < 0%.\n"
        "Customize for stricter (or looser) growth standards — applies to "
        "every metric. Per-metric thresholds are tracked as a follow-up.",
        view=t_view,
    )
    await wait_view_or_cancel(t_view, cancel_event)
    if t_view.cancelled:
        return
    if t_view.choice is None:
        await channel.send(
            f"⏰ Timed out or invalid thresholds. Run `/setup` → {HUB_BTN_BREAKDOWN} to start again."
        )
        return
    if t_view.choice == "defaults":
        thresholds = {}
    elif t_view.choice == "keep":
        # Already loaded from current at the top of this step — no
        # action needed; the wizard saves what's there.
        pass
    else:
        thresholds = t_view._modal_values

    # ── Step 5: Custom labels ─────────────────────────────────────────────
    labels = dict(current.get("breakdown_labels") or {})

    class LabelsModal(discord.ui.Modal):
        def __init__(self):
            super().__init__(title="Custom Bucket Labels")
            self.values_out: dict | None = None
            self._inputs = {}
            for b in BUCKET_ORDER:
                ti = discord.ui.TextInput(
                    label=DEFAULT_BUCKET_LABELS[b],
                    placeholder=f"e.g. '{DEFAULT_BUCKET_LABELS[b]}'",
                    default=str(labels.get(b, DEFAULT_BUCKET_LABELS[b])),
                    required=True, max_length=30,
                )
                self._inputs[b] = ti
                self.add_item(ti)

        async def on_submit(self, inter: discord.Interaction):
            self.values_out = {b: ti.value.strip() for b, ti in self._inputs.items()}
            await inter.response.defer()
            self.stop()

    class LabelsChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_TIMEOUT)
            self.choice = None

            # Mirror ThresholdsChoiceView: when leadership has saved
            # custom bucket labels, surface Keep current as the primary
            # action and demote Use defaults to a revert.
            if labels:
                for child in self.children:
                    if getattr(child, "label", None) == "✅ Use defaults":
                        child.label = "↩️ Use defaults"
                        child.style = discord.ButtonStyle.secondary
                        break
                keep_btn = discord.ui.Button(
                    label="✅ Keep current labels",
                    style=discord.ButtonStyle.success,
                )

                async def _keep_cb(inter: discord.Interaction):
                    self.choice = "keep"
                    for item in self.children: item.disabled = True
                    await wizard_registry.safe_edit_response(inter, view=self)
                    self.stop()
                keep_btn.callback = _keep_cb
                self.add_item(keep_btn)

        @discord.ui.button(label="✅ Use defaults", style=discord.ButtonStyle.success)
        async def defaults_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "defaults"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✏️ Customize", style=discord.ButtonStyle.primary)
        async def customize_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            modal = LabelsModal()
            await inter.response.send_modal(modal)
            await modal.wait()
            self.choice = "customize" if modal.values_out else None
            self._modal_values = modal.values_out
            for item in self.children: item.disabled = True
            try:
                await inter.edit_original_response(view=self)
            except Exception:
                pass
            self.stop()

    l_view = LabelsChoiceView()
    l_view._modal_values = None
    await channel.send(
        "**Step 5 of 5 — Bucket Labels**\n"
        f"Defaults: {', '.join(DEFAULT_BUCKET_LABELS[b] for b in BUCKET_ORDER)}.\n"
        "Rename buckets to match your alliance's voice (e.g. 'Crushing It', "
        "'Stalled', 'Going Backwards').",
        view=l_view,
    )
    await wait_view_or_cancel(l_view, cancel_event)
    if l_view.cancelled:
        return
    if l_view.choice is None:
        await channel.send(
            WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN)
        )
        return
    if l_view.choice == "defaults":
        labels = {}
    elif l_view.choice == "keep":
        # Already loaded from current at the top of this step.
        pass
    else:
        labels = l_view._modal_values

    # ── Save ──────────────────────────────────────────────────────────────
    saved_ok = save_growth_breakdown_config(
        guild_id,
        tab_breakdown=tab_breakdown,
        breakdown_thresholds=thresholds,
        breakdown_labels=labels,
        breakdown_post_channel_id=post_channel_id,
        breakdown_bucket_filter=bucket_filter,
    )
    if not saved_ok:
        await channel.send(
            f"⚠️ Couldn't save the breakdown config — make sure `/setup` → {HUB_BTN_GROWTH} "
            "has been run for this server first."
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    embed = discord.Embed(title="✅ Growth Breakdown Configured", color=discord.Color.green())
    embed.add_field(name="Breakdown Tab", value=tab_breakdown, inline=False)
    if post_channel_id:
        bf_text = (
            ", ".join(DEFAULT_BUCKET_LABELS.get(b, b) for b in bucket_filter)
            if bucket_filter else "All buckets"
        )
        embed.add_field(name="Auto-Post Channel", value=f"<#{post_channel_id}>", inline=False)
        embed.add_field(name="Bucket Filter",     value=bf_text,                inline=False)
    else:
        embed.add_field(name="Auto-Post", value="❌ Off — use `/growth breakdown` (or `/growth overview` → 📊 See most recent Breakdown) to view on demand.", inline=False)
    if thresholds:
        t_text = (
            f"Increased ≥ {thresholds['increased']:g}%, "
            f"Steady ≥ {thresholds['steady']:g}%, "
            f"Low ≥ {thresholds['low']:g}%, "
            f"None ≥ {thresholds['none']:g}%, Decline < 0%"
        )
        embed.add_field(name="Custom Thresholds", value=t_text, inline=False)
    else:
        embed.add_field(name="Thresholds", value="Defaults (Increased ≥ 20%, Steady ≥ 10%, Low ≥ 5%, None ≥ 0%, Decline < 0%)", inline=False)
    if labels:
        l_text = ", ".join(f"{DEFAULT_BUCKET_LABELS[b]}→{labels[b]}" for b in BUCKET_ORDER if labels.get(b))
        embed.add_field(name="Custom Labels", value=l_text or "—", inline=False)
    embed.set_footer(text="Run /setup and click 📊 Growth Breakdown to update.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Growth Breakdown config saved for guild {guild_id}")


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
                await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
            return None
        return reply.content.strip()[:max_chars]

    from config import get_train_config, has_train_config, get_config
    current = get_train_config(guild_id)
    train_already_configured = has_train_config(guild_id)
    guild_cfg = get_config(guild_id)
    guild_tz  = guild_cfg.timezone if guild_cfg else "America/New_York"

    # ── If already configured, show summary and offer edit or cancel ──────────
    if train_already_configured:
        themes = current.get("themes") or []
        tones  = current.get("tones") or []
        fields = [
            ("Schedule Tab", current.get("tab_name") or "*not set*"),
            ("Blurbs",       "✅ Enabled" if current.get("blurbs_enabled") else "❌ Disabled"),
        ]
        if current.get("blurbs_enabled"):
            fields.append(("Themes", ", ".join(themes) if themes else "*none*"))
            fields.append(("Tones",  ", ".join(tones)  if tones  else "*none*"))
            fields.append(("Default Tone", current.get("default_tone") or "*not set*"))
        fields.append(("Reminders", "✅ Enabled" if current.get("reminders_enabled") else "❌ Disabled"))
        if current.get("reminders_enabled"):
            rc = current.get("reminder_channel_id", 0) or 0
            fields.append(("Reminder Channel", f"<#{rc}>" if rc else "*not set*"))
            fields.append(("Reminder Time",    _format_time_with_tz(current.get("reminder_time"), guild_tz) or "*not set*"))
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="🚂 Current Train Setup",
            description="Your train schedule is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Your train setup is still active.",
        )
        if proceed is not True:
            return

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
        f"*(You can always set this up later by running `/setup` → {HUB_BTN_TRAIN} again)*",
        view=blurb_view,
    )
    await wait_view_or_cancel(blurb_view, cancel_event)
    if blurb_view.cancelled:
        return
    if blurb_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
        return
    blurbs_enabled = 1 if blurb_view.selected else 0
    if not blurbs_enabled:
        # If leadership had blurbs configured previously, their themes,
        # tones, default tone, and templates are kept in the DB so a
        # future re-enable starts where they left off. Tell them so they
        # don't worry about losing config.
        had_prior_blurbs = train_already_configured and current.get("blurbs_enabled")
        if had_prior_blurbs:
            await channel.send(
                "ℹ️ Blurb generation disabled. Your themes, tones, and templates "
                "remain saved — re-enable later to restore them."
            )
        else:
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
    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)
    themes_cap = await premium.get_limit("themes", guild_id, interaction=interaction, bot=interaction.client)
    tones_cap  = await premium.get_limit("tones",  guild_id, interaction=interaction, bot=interaction.client)

    def _trim(values: list[str], cap: int | None) -> tuple[list[str], bool]:
        """Trim list to cap. Returns (trimmed_list, was_truncated)."""
        if cap is None or len(values) <= cap:
            return values, False
        return values[:cap], True

    if blurbs_enabled:
        from defaults import DEFAULT_THEMES, DEFAULT_TONES

        async def _ask_csv_keep_or_change(
            *, step_label: str, label: str, current_list: list[str],
            default_list: list[str], cap: int | None,
        ) -> list[str] | None:
            """Wrapper around ask_keep_or_change for comma-separated lists.

            Renders the same 2- or 3-button layout used everywhere else
            (Keep current / Use default / Define my own), then parses the
            returned string back into a list and applies the free-tier cap.
            Returns None on timeout or cancel.
            """
            cap_capped_default, _ = _trim(list(default_list), cap)
            cap_capped_current, _ = _trim(list(current_list), cap)
            cap_note = (
                f"\n*Free tier: up to {cap} {label}. Upgrade for unlimited.*"
                if cap is not None else ""
            )

            chosen = await ask_keep_or_change(
                channel,
                f"{step_label}\n"
                f"These appear as options when selecting a {label[:-1] if label.endswith('s') else label} "
                f"for the train.{cap_note}",
                default=", ".join(cap_capped_default),
                current=", ".join(cap_capped_current),
                modal_title=label.title(),
                modal_label=f"{label.title()} (comma-separated)",
                timeout_cmd="setup_train",
                cancel_event=cancel_event,
            )
            if chosen is None:
                return None
            entered = [t.strip() for t in chosen.split(",") if t.strip()] or list(current_list)
            trimmed, truncated = _trim(entered, cap)
            if truncated:
                await channel.send(
                    f"ℹ️ Free tier: only the first {cap} {label} were saved "
                    f"(`{', '.join(trimmed)}`). Upgrade to Premium to save more."
                )
            return trimmed

        # ── Step 3: Themes ─────────────────────────────────────────────────────
        themes = await _ask_csv_keep_or_change(
            step_label="**Step 3 of 8 — Themes**",
            label="themes",
            current_list=current["themes"],
            default_list=DEFAULT_THEMES,
            cap=themes_cap,
        )
        if themes is None:
            return

        # ── Step 4: Tones ──────────────────────────────────────────────────────
        tones = await _ask_csv_keep_or_change(
            step_label="**Step 4 of 8 — Tones**",
            label="tones",
            current_list=current["tones"],
            default_list=DEFAULT_TONES,
            cap=tones_cap,
        )
        if tones is None:
            return

        # ── Step 5: Default tone ───────────────────────────────────────────────
        class ToneDefaultView(discord.ui.View):
            def __init__(self, tone_list: list, *, current: str | None = None):
                super().__init__(timeout=120)
                self.selected = None

                # Keep-current button on row 0 when the saved default
                # tone still appears in the current tone_list (it may
                # have been removed in Step 4).
                keep_added = False
                if current and current in tone_list:
                    keep_btn = discord.ui.Button(
                        label=f"✅ Keep current: {current}"[:80],
                        style=discord.ButtonStyle.success,
                        row=0,
                    )

                    async def _keep_cb(inter: discord.Interaction):
                        self.selected = current
                        for item in self.children:
                            item.disabled = True
                        await wizard_registry.safe_edit_response(
                            inter,
                            content=f"✅ Keeping default tone: **{current}**",
                            view=self,
                        )
                        self.stop()
                    keep_btn.callback = _keep_cb
                    self.add_item(keep_btn)
                    keep_added = True

                select = discord.ui.Select(
                    placeholder="Select default tone...",
                    options=[discord.SelectOption(label=t, value=t) for t in tone_list],
                    row=1 if keep_added else 0,
                )
                async def _cb(inter: discord.Interaction):
                    self.selected = select.values[0]
                    for item in self.children:
                        item.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=f"✅ Default tone: **{self.selected}**", view=self
                    )
                    self.stop()
                select.callback = _cb
                self.add_item(select)

        tone_default_view = ToneDefaultView(tones, current=current.get("default_tone"))
        await channel.send(
            f"**Step 5 of 8 — Default Tone**\n"
            f"Which tone should be pre-selected by default?",
            view=tone_default_view,
        )
        await wait_view_or_cancel(tone_default_view, cancel_event)
        if tone_default_view.cancelled:
            return
        if not tone_default_view.selected:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
            return
        default_tone = tone_default_view.selected

        # ── Step 6: Prompt templates ───────────────────────────────────────────
        # Free tier keeps a single "Default" template; premium can save up to
        # `template_cap` named templates and pick which is the default.
        template_cap     = await premium.get_limit("train_templates", guild_id, interaction=interaction, bot=interaction.client)
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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
        return
    reminders_enabled  = 1 if reminder_view.selected else 0
    reminder_channel_id = 0
    reminder_time       = "22:00"
    if not reminders_enabled:
        had_prior_reminders = train_already_configured and current.get("reminders_enabled")
        if had_prior_reminders:
            await channel.send(
                "ℹ️ Train reminders disabled. Your saved reminder channel and time "
                "remain saved — re-enable later to restore them."
            )
        else:
            await channel.send(
                "ℹ️ *Skipping Steps 7a–7b (reminder channel and time) — train reminders are off.*"
            )

    if reminders_enabled:
        # ── Step 7a: Reminder channel ──────────────────────────────────────────
        saved_reminder_ch = current.get("reminder_channel_id", 0) or 0
        reminder_ch_view = ChannelSelectStep(
            "Select the reminder channel...",
            suggested_name="leadership",
            include_threads=is_premium_flag,
            guild=interaction.guild,
            current_id=saved_reminder_ch,
        )
        if reminder_ch_view.is_current_stale:
            await channel.send(
                "⚠️ Your previously configured reminder channel no longer exists. "
                "Pick a new one below."
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
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN))
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
                # DB stores 24h ("22:00") — render as "10:00pm" so the
                # Keep-current and Use-default button labels don't sit
                # side-by-side in mismatched formats.
                current=_format_24h_to_12h(current.get("reminder_time", "")),
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
                    f"Run `/setup` → {HUB_BTN_TRAIN} to start over."
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
            "+ Member Sync.\n\n"
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
        embed.add_field(name="Reminder Time",    value=_format_time_with_tz(reminder_time, guild_tz), inline=True)
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
    embed.set_footer(text=f"Run /setup and click {HUB_BTN_TRAIN} to update any of these settings.")
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
        f"Walking you through the same setup steps as `/setup` → 📋 Survey…"
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
            await wizard_registry.safe_edit_response(inter, content=msg, view=None)
            self.stop()

        @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
            await wizard_registry.safe_edit_response(inter, content="❌ Cancelled. No surveys removed.", view=None)
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
                await wizard_registry.safe_edit_response(
                    inter,
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
                await wizard_registry.safe_edit_response(inter, content=f"✏️ Editing **{name}**…", view=self)
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
        has_survey_config, get_config,
    )
    from defaults import DEFAULT_SURVEY_QUESTIONS

    if target_survey_id is None:
        current = get_survey_config(guild_id)
        wizard_label = "Survey Setup"
        survey_already_configured = has_survey_config(guild_id)
        # Main survey: channel ids live on guild_configs (legacy storage),
        # not on guild_survey_config. Load them from there.
        guild_cfg = get_config(guild_id)
        saved_survey_ch = (guild_cfg.survey_channel_id if guild_cfg else 0) or 0
        saved_notify_ch = (guild_cfg.survey_notify_channel_id if guild_cfg else 0) or 0
    else:
        current = get_survey(guild_id, target_survey_id) or {}
        # Carry the existing name through so we can preserve it on save.
        if not target_survey_name:
            target_survey_name = current.get("survey_name") or target_survey_id
        wizard_label = f"Survey Setup — {target_survey_name}"
        survey_already_configured = bool(current)
        # Extra surveys: channel ids are stored alongside the survey row.
        saved_survey_ch = (current.get("survey_channel_id") or 0)
        saved_notify_ch = (current.get("notify_channel_id") or 0)
    questions = list(current.get("questions") or [])

    # ── If already configured, show summary and offer edit or cancel ─────────
    if survey_already_configured:
        q_count = len(questions)
        fields = [
            (
                "Survey Channel",
                f"<#{saved_survey_ch}>" if saved_survey_ch else "*not set*",
            ),
            (
                "Notification Channel",
                f"<#{saved_notify_ch}>" if saved_notify_ch else "*not set*",
            ),
            ("Stats Tab",   current.get("tab_squad_powers") or "*not set*"),
            ("History Tab", current.get("tab_history")      or "*not set*"),
            ("Questions",   f"{q_count} configured" if q_count else "*none*"),
        ]
        title = (
            f"📋 Current Survey Setup — {target_survey_name}"
            if target_survey_id else "📋 Current Survey Setup"
        )
        proceed = await ask_proceed_with_existing_config(
            channel,
            title=title,
            description="This survey is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Your survey setup is still active.",
        )
        if proceed is not True:
            return

    await channel.send(
        f"⚙️ **{wizard_label}**\n"
        "Configure the survey for your alliance."
    )

    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)

    # ── Step 1: Survey channel ─────────────────────────────────────────────────
    survey_ch_view = ChannelSelectStep(
        "Select the survey channel...",
        suggested_name="squad-survey",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=saved_survey_ch,
    )
    if survey_ch_view.is_current_stale:
        await channel.send(
            "⚠️ Your previously configured survey channel no longer exists. "
            "Pick a new one below."
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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
        return
    survey_channel_id = survey_ch_view.selected_channel.id

    # ── Step 2: Survey notification channel ───────────────────────────────────
    notify_ch_view = ChannelSelectStep(
        "Select the survey notification channel...",
        suggested_name="survey-responses",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=saved_notify_ch,
    )
    if notify_ch_view.is_current_stale:
        await channel.send(
            "⚠️ Your previously configured notification channel no longer exists. "
            "Pick a new one below."
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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
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
    # Intro message can be long-form / multi-line, so we use a free-text
    # reply via bot.wait_for rather than a modal. When a saved intro
    # already exists, show it back to leadership and offer Keep current
    # so they don't have to retype a paragraph just to tweak channels
    # or questions.
    saved_intro = (current.get("intro_message") or "").strip()
    intro_message: str | None = None

    if saved_intro:
        # Preview is truncated to keep the embed readable when leadership
        # has saved a long intro.
        preview = saved_intro if len(saved_intro) <= 500 else saved_intro[:500] + "…"

        class IntroChoiceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                # Use a distinct attribute name so this view doesn't
                # collide with QuestionStartView's `choice` field when
                # send-handlers in tests broadcast view overrides.
                self.intro_choice = None

            @discord.ui.button(label="✅ Keep current", style=discord.ButtonStyle.success)
            async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
                self.intro_choice = "keep"
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

            @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary)
            async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
                self.intro_choice = "edit"
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

        intro_view = IntroChoiceView()
        await channel.send(
            "**Step 5 of 6 — Survey Intro Message**\n"
            "Members see this introductory message before taking the survey.\n\n"
            f"**Currently saved:**\n>>> {preview}",
            view=intro_view,
        )
        await wait_view_or_cancel(intro_view, cancel_event)
        if intro_view.cancelled:
            return
        if intro_view.intro_choice == "keep":
            intro_message = saved_intro
        elif intro_view.intro_choice == "edit":
            await channel.send(
                "Type the new intro message below. Use the example from "
                "the previous step as a guide, or paste in your own."
            )
        else:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
            return

    if intro_message is None:
        if not saved_intro:
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
                await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
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
            await wizard_registry.safe_edit_response(inter, content="✅ Using default questions.", view=self)
            self.stop()

        @discord.ui.button(label="✏️ Edit existing questions", style=discord.ButtonStyle.primary)
        async def edit_existing(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "edit"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, content="✏️ Entering edit mode...", view=self)
            self.stop()

        @discord.ui.button(label="🔄 Start from scratch", style=discord.ButtonStyle.secondary)
        async def start_scratch(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "scratch"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(inter, content="🔄 Starting from scratch...", view=self)
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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
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
                                await wizard_registry.safe_edit_response(inter, view=self)
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
                                await wizard_registry.safe_edit_response(inter, view=self)
                                self.stop()
                            del_select.callback = _del_cb
                            self.add_item(del_select)

                    @discord.ui.button(label="➕ Add Question", style=discord.ButtonStyle.primary, row=2)
                    async def add_q(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.action = "add"
                        for item in self.children: item.disabled = True
                        await wizard_registry.safe_edit_response(inter, view=self)
                        self.stop()

                    @discord.ui.button(label="✅ Finish Survey Setup", style=discord.ButtonStyle.success, row=2)
                    async def finish(self, inter: discord.Interaction, button: discord.ui.Button):
                        self.action = "finish"
                        for item in self.children: item.disabled = True
                        await wizard_registry.safe_edit_response(inter, view=self)
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
                    await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
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
                        q_cap = await premium.get_limit("survey_questions", guild_id, interaction=interaction, bot=interaction.client)
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
                        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
                        return False

                    q_key = q_label.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")

                    # Type — Numeric is free; Multi-select / Date are Premium.
                    is_premium_for_q = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)
                    type_options = [
                        discord.SelectOption(label="Text — member types their answer", value="text"),
                        discord.SelectOption(label="Dropdown — member selects from a list", value="dropdown"),
                        discord.SelectOption(label="Numeric — number, with shorthand support", value="numeric"),
                    ]
                    if is_premium_for_q:
                        type_options += [
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
                                await wizard_registry.safe_edit_response(
                                    inter,
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
                        "Pick how members answer this question."
                        + type_extra
                    )
                    await channel.send(type_prompt, view=type_view)
                    await wait_view_or_cancel(type_view, cancel_event)
                    if type_view.cancelled:
                        return
                    if not type_view.selected:
                        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
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
                        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
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
                            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
                            return False

                    if q_type == "numeric":
                        # Magnitude — required for numeric. Tells the parser
                        # what bare shorthand like `301` means for this field.
                        mag_options = [
                            discord.SelectOption(
                                label="Exact number — type what you mean (e.g. drone level 150 stays 150)",
                                value="raw",
                            ),
                            discord.SelectOption(
                                label="Thousands (K) — 5 becomes 5,000",
                                value="K",
                            ),
                            discord.SelectOption(
                                label="Millions (M) — 301 becomes 301,000,000",
                                value="M",
                            ),
                            discord.SelectOption(
                                label="Billions (B) — 1.2 becomes 1,200,000,000",
                                value="B",
                            ),
                        ]

                        class MagnitudeView(discord.ui.View):
                            def __init__(self):
                                super().__init__(timeout=120)
                                self.selected = None
                                select = discord.ui.Select(
                                    placeholder="Select number scale...",
                                    options=mag_options,
                                )
                                async def _cb(inter: discord.Interaction):
                                    self.selected = select.values[0]
                                    select.disabled = True
                                    pretty = {"raw": "Exact number", "K": "Thousands (K)",
                                              "M": "Millions (M)", "B": "Billions (B)"}
                                    await wizard_registry.safe_edit_response(
                                        inter,
                                        content=f"✅ Scale: **{pretty.get(self.selected, self.selected)}**",
                                        view=self,
                                    )
                                    self.stop()
                                select.callback = _cb
                                self.add_item(select)

                        existing_mag = (existing.get("magnitude") if existing else "") or ""
                        mag_extra    = f"\n*Existing scale:* `{existing_mag or 'raw'}`" if existing else ""
                        mag_view     = MagnitudeView()
                        await channel.send(
                            f"**{q_num} — Number Scale**\n"
                            f"How big are these numbers typically? Picking a scale lets members type "
                            f"the natural shorthand (`301`) instead of the full value (`304,743,912`) — "
                            f"the bot accepts both either way."
                            + mag_extra,
                            view=mag_view,
                        )
                        await wait_view_or_cancel(mag_view, cancel_event)
                        if mag_view.cancelled:
                            return
                        if not mag_view.selected:
                            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
                            return False
                        extra_meta["magnitude"] = mag_view.selected

                        # Min/max bounds — Premium-only. Free tier sees a
                        # one-line teaser and we move on without bounds.
                        if is_premium_for_q:
                            await channel.send(
                                f"**{q_num} — Numeric Bounds** *(💎 Premium)*\n"
                                f"Reply with `min,max` (e.g. `0,100`), `min,` for only a minimum, "
                                f"`,max` for only a maximum, or `none` to skip both bounds.\n"
                                f"*Bounds are checked against the stored value after scaling.*"
                            )
                            try:
                                bounds_reply = await bot.wait_for("message", check=check, timeout=120)
                            except asyncio.TimeoutError:
                                await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
                                return False
                            raw = bounds_reply.content.strip().lower()
                            if raw not in ("", "none"):
                                try:
                                    lo_s, _, hi_s = raw.partition(",")
                                    if lo_s.strip(): extra_meta["min"] = float(lo_s.strip())
                                    if hi_s.strip(): extra_meta["max"] = float(hi_s.strip())
                                except ValueError:
                                    await channel.send(
                                        "⚠️ Couldn't parse bounds. Run `/setup` → 📋 Survey to try again."
                                    )
                                    return False
                        else:
                            await channel.send(
                                "💎 *Min/max bounds are a Premium feature — this question will accept any number.*"
                            )

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
                            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY))
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
        await channel.send("⚠️ No questions defined. Run `/setup` → 📋 Survey to try again.")
        return

    # ── Save — including channel IDs ───────────────────────────────────────────
    if target_survey_id is None:
        # Default survey: legacy single-row storage, plus the channel IDs go
        # to guild_configs so older code that reads them stays happy.
        save_survey_config(guild_id, tab_squad_powers, tab_history, questions, intro_message)
        from config import update_config_field
        update_config_field(guild_id, "survey_channel_id",        survey_channel_id)
        update_config_field(guild_id, "survey_notify_channel_id", survey_notify_channel_id)
        next_step_cmd = "/setup → 📋 Survey"
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
        text=f"Run {next_step_cmd} again to update. Run /survey post to post the survey button."
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
    # cmd_name is the user-facing hint shown after `Run /` in timeout /
    # footer messages throughout this wizard. The old `/setup_desertstorm`
    # and `/setup_canyonstorm` slash commands were consolidated under the
    # `/setup` hub (#201); the hint now points officers at the button
    # they actually need to click. For internal slug uses (channel-name
    # suggestion) helpers derive a separate `cmd_short` from `event_type`.
    storm_button = "⚔️ Desert Storm" if event_type == "DS" else "🏜️ Canyon Storm"
    cmd_name = f"setup → {storm_button}"
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
                await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        return reply.content.strip()[:max_chars]

    from config import (
        get_storm_config, get_config, has_storm_config,
        get_structured_storm_config,
    )
    from defaults import DEFAULT_DS_TEMPLATE, DEFAULT_CS_TEMPLATE
    current            = get_storm_config(guild_id, event_type)
    current_structured = get_structured_storm_config(guild_id, event_type)
    guild_cfg = get_config(guild_id)
    timezone  = guild_cfg.timezone if guild_cfg and guild_cfg.timezone else "America/New_York"
    tz_label  = TIMEZONE_LABELS.get(timezone, timezone)
    storm_already_configured = has_storm_config(guild_id, event_type)
    saved_log_ch = (
        guild_cfg.ds_log_channel_id if event_type == "DS"
        else guild_cfg.cs_log_channel_id
    ) if guild_cfg else 0
    saved_log_ch = saved_log_ch or 0
    saved_post_ch = current.get("post_channel_id") or 0

    # Per-team saved mail templates — drives Step 5 Keep-current (#231).
    # save_storm_config persists DS_A / DS_B (and CS_A / CS_B) rows per
    # team; the base DS / CS row mirrors whichever side has content. Read
    # the per-team rows directly so the wizard can distinguish "saved
    # custom" from "saved default" from "no row".
    saved_template_a = ""
    saved_template_b = ""
    if has_storm_config(guild_id, f"{event_type}_A"):
        saved_template_a = (
            get_storm_config(guild_id, f"{event_type}_A").get("mail_template") or ""
        ).strip()
    if has_storm_config(guild_id, f"{event_type}_B"):
        saved_template_b = (
            get_storm_config(guild_id, f"{event_type}_B").get("mail_template") or ""
        ).strip()

    # Default template and placeholders per event type
    if event_type == "DS":
        default_template  = DEFAULT_DS_TEMPLATE
        placeholder_info  = (
            "• `{alliance_name}`: your alliance name\n"
            "• `{zones}`: zone assignments block\n"
            "• `{subs}`: substitute members\n"
            "• `{time}`: event time (auto-filled when drafting)"
        )
    else:
        default_template  = DEFAULT_CS_TEMPLATE
        placeholder_info  = (
            "• `{alliance_name}`: your alliance name\n"
            "• `{zones}`: zone assignments block\n"
            "• `{subs}`: substitute members\n"
            "• `{time}`: event time (auto-filled when drafting)"
        )

    # ── If already configured, show summary and offer edit or cancel ─────────
    if storm_already_configured:
        templates = current.get("templates") or []
        structured_status = (
            "✅ Enabled" if current_structured.get("structured_flow_enabled")
            else "❌ Off (preset tabs available on free tier)"
        )
        fields = [
            ("Sheet Tab",    current.get("tab_name") or "*not set*"),
            ("Log Channel",  f"<#{saved_log_ch}>" if saved_log_ch else "*not set*"),
            ("Post Channel", f"<#{saved_post_ch}>" if saved_post_ch else "*not set*"),
            ("Timezone",     tz_label),
            (
                "Mail Templates",
                ", ".join(t["name"] for t in templates) if templates else "Default",
            ),
            (
                "Reminder DM",
                "Custom" if (current.get("dm_reminder_message") or "").strip() else "Default",
            ),
            ("Structured Roster Flow", structured_status),
        ]
        # CS reads `teams` like DS does (Rule A / #166), so surface the
        # field in the re-entry summary for both event types — officers
        # can see their current single-team / both-teams config.
        _summary_teams = {"both": "A & B", "A": "A only", "B": "B only"}.get(
            (current.get("teams") or "both"), "A & B",
        )
        fields.insert(1, ("Teams", _summary_teams))

        # Team time-slot mapping (#251). Surfaced so officers can see at
        # a glance whether the slots are set, and what they're set to,
        # without having to re-enter the wizard's Step 3.
        from config import get_storm_slot_labels as _gslot
        try:
            _slot_lbls = _gslot(event_type, guild_id)
        except Exception:
            _slot_lbls = []
        _team_summary = (current.get("teams") or "both").strip()
        _a_idx = current.get("team_a_slot_index")
        _b_idx = current.get("team_b_slot_index")

        def _slot_blurb(idx):
            if idx in (1, 2) and len(_slot_lbls) >= idx:
                return _slot_lbls[idx - 1]
            return "*not set*"

        if _team_summary == "A":
            _times_value = f"Team A: {_slot_blurb(_a_idx)}"
        elif _team_summary == "B":
            _times_value = f"Team B: {_slot_blurb(_b_idx)}"
        else:
            _times_value = (
                f"Team A: {_slot_blurb(_a_idx)} · "
                f"Team B: {_slot_blurb(_b_idx)}"
            )
        fields.insert(2, ("Team Times", _times_value))
        emoji = "⚔️" if event_type == "DS" else "🏜️"
        proceed = await ask_proceed_with_existing_config(
            channel,
            title=f"{emoji} Current {label} Setup",
            description=f"{label} is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message=f"✅ No changes made. Your {label} setup is still active.",
        )
        if proceed is not True:
            return

    await channel.send(f"⚙️ **{label} Setup**")

    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)

    # ── Step 1: Sheet tab ──────────────────────────────────────────────────────
    # When Member Sync is enabled, default to the alliance's Member
    # Sync tab name (typically "Member Roster") — that's the canonical
    # roster location for everything else in the bot, so suggesting the
    # same tab here keeps the alliance's mental model coherent. Falls
    # back to the legacy `DS Assignments` / `CS Assignments` default
    # when Member Sync isn't configured yet.
    from config import get_member_roster_config as _gmrc_step1
    _sync_cfg_step1 = _gmrc_step1(guild_id) if guild_id else {}
    if _sync_cfg_step1.get("enabled"):
        hardcoded_tab = _sync_cfg_step1.get("tab_name") or "Member Roster"
    else:
        hardcoded_tab = "DS Assignments" if event_type == "DS" else "CS Assignments"
    tab_name = await ask_keep_or_change(
        channel,
        f"**Step 1 of 9: Sheet Tab**\n"
        f"Which tab in your Google Sheet stores the {label} zone assignments?\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*\n"
        f"ℹ️ *The bot will manage the data structure of this tab automatically. "
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
    saved_teams_raw = (current.get("teams") or "both").strip()
    saved_teams = saved_teams_raw if saved_teams_raw in ("both", "A", "B") else "both"
    _team_blurb = {
        "both": "Team A & Team B",
        "A":    "Team A only",
        "B":    "Team B only",
    }

    # Capture the prompt so the button callbacks can preserve it in the
    # edited message — otherwise the question disappears the moment a
    # button is clicked and officers scrolling back to review what they
    # answered see only the bare confirmation line.
    team_prompt = (
        f"**Step 2 of 9: Which teams do you run for {label}?**"
        + (
            f"\nCurrent: **{_team_blurb[saved_teams]}**"
            if storm_already_configured else ""
        )
    )

    class TeamChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=120)
            self.selected = None

        @discord.ui.button(label="Team A & Team B", style=discord.ButtonStyle.primary)
        async def both(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "both"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"{team_prompt}\n\n✅ Teams: **Team A & Team B**",
                view=self,
            )
            self.stop()

        @discord.ui.button(label="Team A only", style=discord.ButtonStyle.secondary)
        async def a_only(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "A"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"{team_prompt}\n\n✅ Teams: **Team A only**",
                view=self,
            )
            self.stop()

        @discord.ui.button(label="Team B only", style=discord.ButtonStyle.secondary)
        async def b_only(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = "B"
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"{team_prompt}\n\n✅ Teams: **Team B only**",
                view=self,
            )
            self.stop()

        # Re-entry: keep the previously-saved choice without re-clicking.
        # Surface the actual saved selection on the button label (set in
        # post-construction below) so officers can see at a glance what
        # "Keep current" would preserve. Removed entirely when the
        # alliance has no saved value yet (fresh setup).
        @discord.ui.button(label="Keep current", style=discord.ButtonStyle.success)
        async def keep_current(self, inter: discord.Interaction, button: discord.ui.Button):
            self.selected = saved_teams
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=(
                    f"{team_prompt}\n\n"
                    f"✅ Teams: **{_team_blurb[saved_teams]}** (kept current)"
                ),
                view=self,
            )
            self.stop()

    team_view = TeamChoiceView()
    if storm_already_configured:
        # Surface the saved value on the Keep current button so the
        # officer can see what would be preserved without reading the
        # prompt — mirrors the convention used by `ask_keep_or_change`.
        team_view.keep_current.label = (
            f"✅ Keep current: {_team_blurb[saved_teams]}"[:80]
        )
    else:
        # Hide Keep current on fresh setup — there's no current value to keep.
        team_view.remove_item(team_view.keep_current)
    await channel.send(team_prompt, view=team_view)
    await wait_view_or_cancel(team_view, cancel_event)
    if team_view.cancelled:
        return
    if not team_view.selected:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
        return
    teams = team_view.selected

    # ── Step 3: Team time slots (#251) ────────────────────────────────────────
    # DS / CS each have two game-defined time slots; this step records
    # which slot each team the alliance runs is on. Both teams can pick
    # the same slot. Independent per event type. The mapping is the
    # default for every weekly sign-up; the officer can override it for
    # a single week when posting that week's sign-up.
    from config import get_storm_slot_labels
    slot_labels = get_storm_slot_labels(event_type, guild_id)
    saved_a_idx = current.get("team_a_slot_index")
    saved_b_idx = current.get("team_b_slot_index")

    async def pick_team_slot(team_letter: str, saved_idx):
        """Single-team slot picker. Returns 1 / 2 (selected), or None on
        cancel / timeout. `saved_idx` drives whether Keep current renders."""
        current_line = (
            f"\nCurrent: **{slot_labels[saved_idx - 1]}**"
            if saved_idx in (1, 2) else ""
        )
        # Capture the prompt so each button callback can echo it in the
        # edited message — keeps the original question visible when the
        # officer scrolls back to review what they chose, instead of
        # leaving only the bare confirmation line.
        slot_prompt = (
            f"Which time slot does **Team {team_letter}** run for {label}?"
            + current_line
        )

        class TeamSlotView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=180)
                self.selected = None

            @discord.ui.button(label=slot_labels[0], style=discord.ButtonStyle.primary)
            async def slot1(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 1
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=(
                        f"{slot_prompt}\n\n"
                        f"✅ Team {team_letter}: **{slot_labels[0]}**"
                    ),
                    view=self,
                )
                self.stop()

            @discord.ui.button(label=slot_labels[1], style=discord.ButtonStyle.primary)
            async def slot2(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 2
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=(
                        f"{slot_prompt}\n\n"
                        f"✅ Team {team_letter}: **{slot_labels[1]}**"
                    ),
                    view=self,
                )
                self.stop()

            @discord.ui.button(label="Keep current", style=discord.ButtonStyle.success)
            async def keep_current(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = saved_idx
                for item in self.children: item.disabled = True
                kept_label = slot_labels[saved_idx - 1] if saved_idx in (1, 2) else "—"
                await wizard_registry.safe_edit_response(
                    inter,
                    content=(
                        f"{slot_prompt}\n\n"
                        f"✅ Team {team_letter}: **{kept_label}** (kept current)"
                    ),
                    view=self,
                )
                self.stop()

        view = TeamSlotView()
        if saved_idx not in (1, 2):
            view.remove_item(view.keep_current)
        else:
            # Surface the saved slot on the Keep current button so the
            # officer can see what would be preserved without reading
            # back through the prompt — matches the convention used by
            # `ask_keep_or_change` elsewhere in the wizard.
            view.keep_current.label = (
                f"✅ Keep current: {slot_labels[saved_idx - 1]}"[:80]
            )
        await channel.send(slot_prompt, view=view)
        await wait_view_or_cancel(view, cancel_event)
        if view.cancelled:
            return None
        if not view.selected:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        return view.selected

    await channel.send(
        f"**Step 3 of 9: Team Time Slots**\n"
        f"Select the time when you typically run each {label} team. "
        f"You can override these for a single week when you send out the "
        f"sign up, if needed."
    )

    team_a_slot = None
    team_b_slot = None
    if teams in ("both", "A"):
        team_a_slot = await pick_team_slot("A", saved_a_idx)
        if team_a_slot is None:
            return
    if teams in ("both", "B"):
        team_b_slot = await pick_team_slot("B", saved_b_idx)
        if team_b_slot is None:
            return

    # ── Step 4: Storm log channel ─────────────────────────────────────────────
    # Reused by /[event]_log lookups and by the participation flow when
    # leadership posts the participation summary.
    log_ch_view = ChannelSelectStep(
        f"Select the {label} log channel...",
        suggested_name="storm-log",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=saved_log_ch,
    )
    if log_ch_view.is_current_stale:
        await channel.send(
            f"⚠️ Your previously configured {label} log channel no longer exists. "
            "Pick a new one below."
        )
    await channel.send(
        f"**Step 4 of 9: Storm Log Channel**\n"
        f"Select the channel where {label} participation/log summaries will be posted:",
        view=log_ch_view,
    )
    await wait_view_or_cancel(log_ch_view, cancel_event)
    if log_ch_view.cancelled:
        return
    if not log_ch_view.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
        return
    log_channel_id = log_ch_view.selected_channel.id

    # ── Step 4: Post channel (where 📄 Generate mail posts the final mail) ───
    post_ch_view = ChannelSelectStep(
        f"Select the {label} mail post channel...",
        suggested_name=f"{'desert' if event_type == 'DS' else 'canyon'}-storm",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=saved_post_ch,
    )
    if post_ch_view.is_current_stale:
        await channel.send(
            f"⚠️ Your previously configured {label} mail post channel no longer exists. "
            "Pick a new one below."
        )
    parent_cmd = "desertstorm" if event_type == "DS" else "canyonstorm"
    await channel.send(
        f"**Step 5 of 9: Mail Post Channel**\n"
        f"When leadership clicks **Post & Copy** at the end of "
        f"`/{parent_cmd}` → **📄 Generate mail**, the finished mail "
        f"will be posted to this channel:",
        view=post_ch_view,
    )
    await wait_view_or_cancel(post_ch_view, cancel_event)
    if post_ch_view.cancelled:
        return
    if not post_ch_view.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
        return
    post_channel_id = post_ch_view.selected_channel.id

    # ── Step 5: Mail template(s) ───────────────────────────────────────────────

    async def get_template(team_label: str, saved_template: str = "") -> str | None:
        """Get template for one team — show default with use/edit choice.

        When `saved_template` is a non-empty body that differs from the
        hardcoded default, render a 3-button view (Keep current / Use
        default / Edit) so re-entering officers can preserve their
        custom body. Pre-#231 the only options were Use default (which
        silently overwrote the saved custom) and Edit (which forced a
        re-paste from scratch).
        """
        saved_is_custom = bool(saved_template) and saved_template != default_template

        class TemplateChoiceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.outcome: str | None = None  # "keep" | "default" | "edit"

            # Re-entry only — only added when saved_is_custom.
            @discord.ui.button(label="✅ Keep current custom template", style=discord.ButtonStyle.success)
            async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
                self.outcome = "keep"
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"✅ Keeping your saved custom template for {team_label}.", view=self
                )
                self.stop()

            @discord.ui.button(label="↩️ Use default template", style=discord.ButtonStyle.secondary)
            async def use_def(self, inter: discord.Interaction, button: discord.ui.Button):
                self.outcome = "default"
                for item in self.children: item.disabled = True
                msg = (
                    f"✅ Reverted to default template for {team_label}."
                    if saved_is_custom else
                    f"✅ Using default template for {team_label}."
                )
                await wizard_registry.safe_edit_response(inter, content=msg, view=self)
                self.stop()

            @discord.ui.button(label="✏️ Edit template", style=discord.ButtonStyle.secondary)
            async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
                self.outcome = "edit"
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

        choice_view = TemplateChoiceView()
        if not saved_is_custom:
            # First-time / saved-equals-default — drop the Keep current
            # button and let the default-color Use default button act as
            # the success default like the pre-#231 view.
            choice_view.remove_item(choice_view.keep)
            choice_view.use_def.style = discord.ButtonStyle.success

        custom_block = (
            f"\n\nHere is your saved custom template:\n```\n{saved_template}\n```"
            if saved_is_custom else ""
        )
        question = (
            "Would you like to keep your custom template, revert to the default, or edit it?"
            if saved_is_custom else
            "Would you like to use this or edit it?"
        )
        await channel.send(
            f"**{label} Mail Template: {team_label}**\n"
            f"When you draft the mail each week, you will be able to select the time slot "
            f"when you are running that team's {label}.\n\n"
            f"Here is the default template:\n"
            f"```\n{default_template}\n```"
            f"{custom_block}\n\n"
            f"{question}",
            view=choice_view,
        )
        await wait_view_or_cancel(choice_view, cancel_event)
        if choice_view.cancelled:
            return
        if choice_view.outcome is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        if choice_view.outcome == "keep":
            return saved_template
        if choice_view.outcome == "default":
            return default_template

        # User wants to edit — show variables and ask for input. When a
        # custom body is saved, point them at it as the natural starting
        # point so they can copy-modify instead of typing from scratch.
        reference_label = "current custom" if saved_is_custom else "default"
        await channel.send(
            f"Paste your custom template for **{team_label}**. "
            f"You can copy the {reference_label} above and modify it, or write your own.\n\n"
            f"**Available placeholders:**\n{placeholder_info}\n\n"
            f"*This form will time out in 5 minutes. "
            f"You can run `/{cmd_name}` again if it times out.*"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=300)
            fallback = saved_template if saved_is_custom else default_template
            return reply.content.strip() or fallback
        except asyncio.TimeoutError:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None

    if teams == "both":
        # Derive saved shared-vs-separate from per-team rows so re-entry
        # offers Keep current instead of forcing officers to re-pick
        # (#231). Only resolvable when BOTH team rows have non-empty
        # bodies — switching from A-only / B-only to both is first-time
        # for the shared/separate decision.
        if saved_template_a and saved_template_b:
            saved_share_mode = (
                "shared" if saved_template_a == saved_template_b else "separate"
            )
        else:
            saved_share_mode = None

        class SharedTemplateView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected = None

            # Re-entry only — removed when saved_share_mode is None.
            @discord.ui.button(label="Keep current", style=discord.ButtonStyle.success)
            async def keep_current(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = saved_share_mode
                for item in self.children: item.disabled = True
                ack = (
                    "✅ Kept current: **One shared template** for Team A & B"
                    if saved_share_mode == "shared" else
                    "✅ Kept current: **Separate templates** for Team A & Team B"
                )
                await wizard_registry.safe_edit_response(inter, content=ack, view=self)
                self.stop()

            @discord.ui.button(label="One template for both teams", style=discord.ButtonStyle.primary)
            async def shared(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = "shared"
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content="✅ **One shared template** for Team A & B", view=self
                )
                self.stop()

            @discord.ui.button(label="Separate templates per team", style=discord.ButtonStyle.secondary)
            async def separate(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = "separate"
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content="✅ **Separate templates** for Team A & Team B", view=self
                )
                self.stop()

        shared_view = SharedTemplateView()
        if saved_share_mode is None:
            shared_view.remove_item(shared_view.keep_current)
        else:
            shared_view.keep_current.label = (
                "✅ Keep current: One shared template"
                if saved_share_mode == "shared" else
                "✅ Keep current: Separate templates"
            )
        prompt_lines = [
            "**Step 6 of 9: Mail Template**",
            "Do you want one template that applies to both teams, or separate templates per team?",
        ]
        if saved_share_mode is not None:
            prompt_lines.append(
                "Current: **"
                + ("One shared template" if saved_share_mode == "shared" else "Separate templates")
                + "**."
            )
        await channel.send("\n".join(prompt_lines), view=shared_view)
        await wait_view_or_cancel(shared_view, cancel_event)
        if shared_view.cancelled:
            return
        if not shared_view.selected:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return

        if shared_view.selected == "shared":
            # Shared mode — feed the saved A template (equal to B on a
            # prior shared save) as the Keep-current candidate. When the
            # prior save was separate, leave it blank so the user gets a
            # first-time template prompt for the new shared body.
            shared_saved = saved_template_a if saved_share_mode == "shared" else ""
            template_a = await get_template("Team A & B", saved_template=shared_saved)
            if template_a is None:
                return
            template_b = template_a
        else:
            template_a = await get_template("Team A", saved_template=saved_template_a)
            if template_a is None:
                return
            template_b = await get_template("Team B", saved_template=saved_template_b)
            if template_b is None:
                return

    else:
        team_label = "Team A" if teams == "A" else "Team B"
        # Single-team mode — only the saved row for the picked team is
        # relevant; the other side stays empty.
        saved_for_team = saved_template_a if teams == "A" else saved_template_b
        await channel.send("**Step 6 of 9: Mail Template**")
        template = await get_template(team_label, saved_template=saved_for_team)
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

    # ── Structured roster flow (#38 + #54) — Premium opt-in + preset tabs ────
    structured_cfg = await _run_structured_flow_setup_step(
        channel, bot, user, cancel_event,
        guild_id=guild_id, event_type=event_type, label=label, cmd_name=cmd_name,
        is_premium_flag=is_premium_flag,
        current=current, current_structured=current_structured,
        interaction_guild=interaction.guild,
    )
    if structured_cfg is None:
        return  # cancelled / timed out

    # ── Step 7: Reminder DM body (💎 Premium) ─────────────────────────────────
    # The body of the DM that fires when leadership clicks
    # 🔔 Send DM reminder to roster on the storm hub. Stored per
    # (guild_id, event_type) so DS and CS can have different copy. Free
    # guilds can configure this now too — it just won't fire until they
    # upgrade.
    from storm_log import DEFAULT_STORM_REMINDER_DM
    default_remind_dm = DEFAULT_STORM_REMINDER_DM.format(label=label)
    saved_remind_dm   = (current.get("dm_reminder_message") or "").strip()
    parent_cmd = "desertstorm" if event_type == "DS" else "canyonstorm"
    remind_dm = await ask_keep_or_change(
        channel,
        f"**Step 9 of 9: {label} Reminder DM (💎 Premium)**\n"
        f"When leadership clicks **🔔 Send DM reminder to roster** on "
        f"`/{parent_cmd}`, the bot DMs every roster member this message. "
        f"Free guilds can configure it now; it just won't fire until "
        f"you have Premium + Member Sync.\n\n"
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
    from config import (
        save_storm_config, save_participation_config, update_config_field,
        save_structured_storm_config, save_storm_team_slots,
    )
    # `teams` carries the wizard's Step 2 choice ('both' / 'A' / 'B') so
    # the strategy preset editor can hide Min Power inputs for a team the
    # alliance doesn't run (#148). CS rows store 'both' and ignore it.
    teams_persisted = teams if event_type == "DS" else "both"
    if template_a:
        save_storm_config(guild_id, f"{event_type}_A", tab_name, template_a,
                          timezone, log_channel_id,
                          post_channel_id=post_channel_id,
                          dm_reminder_message=dm_reminder_message,
                          teams=teams_persisted)
    if template_b:
        save_storm_config(guild_id, f"{event_type}_B", tab_name, template_b,
                          timezone, log_channel_id,
                          post_channel_id=post_channel_id,
                          dm_reminder_message=dm_reminder_message,
                          teams=teams_persisted)
    save_storm_config(guild_id, event_type, tab_name, template_a or template_b,
                      timezone, log_channel_id,
                      post_channel_id=post_channel_id,
                      dm_reminder_message=dm_reminder_message,
                      teams=teams_persisted)

    # Persist the per-team slot mapping (#251). Kept separate from
    # save_storm_config so this step can be re-run without re-typing the
    # rest of the config — same precedent as save_structured_storm_config.
    save_storm_team_slots(
        guild_id, event_type,
        team_a_slot_index=team_a_slot,
        team_b_slot_index=team_b_slot,
    )

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

    # Persist the structured-flow config (#38 + #54) against the (guild,
    # event_type) row save_storm_config just created/updated above. The
    # registration-post schedule is set in #124; left blank here.
    # Roster DM templates (#226 follow-up) — stored on the same
    # guild_storm_config row but written via a separate helper so
    # save_structured_storm_config's signature stays focused. Empty
    # strings persist as "fall back to the hardcoded default at send
    # time."
    from config import save_roster_dm_templates
    save_roster_dm_templates(
        guild_id, event_type,
        starter   =structured_cfg.get("roster_dm_starter_template", ""),
        paired_sub=structured_cfg.get("roster_dm_paired_sub_template", ""),
        pool_sub  =structured_cfg.get("roster_dm_pool_sub_template", ""),
    )

    save_structured_storm_config(
        guild_id, event_type,
        structured_flow_enabled=structured_cfg["structured_flow_enabled"],
        power_metric_column    =structured_cfg.get("power_metric_column", "B"),
        power_metric_tab       =structured_cfg.get("power_metric_tab", ""),
        power_match_column     =structured_cfg.get("power_match_column", ""),
        sub_mode               =structured_cfg["sub_mode"],
        signup_channel_id      =structured_cfg["signup_channel_id"],
        signup_schedule_cron   =structured_cfg.get("signup_schedule_cron", ""),
        signups_tab            =structured_cfg["signups_tab"],
        rosters_tab            =structured_cfg["rosters_tab"],
        attendance_tab         =structured_cfg["attendance_tab"],
        strategies_tab         =structured_cfg["strategies_tab"],
        member_rules_tab       =structured_cfg["member_rules_tab"],
        poll_day_of_week       =structured_cfg.get("poll_day_of_week", -1),
        signup_time            =structured_cfg.get("signup_time", ""),
        power_refresh_dm_enabled=bool(structured_cfg.get("power_refresh_dm_enabled", False)),
        power_last_updated_tab          =structured_cfg.get("power_last_updated_tab", ""),
        power_last_updated_column       =structured_cfg.get("power_last_updated_column", ""),
        power_last_updated_match_column =structured_cfg.get("power_last_updated_match_column", ""),
        power_refresh_stale_days        =int(structured_cfg.get("power_refresh_stale_days", 0)),
    )

    embed = discord.Embed(title=f"✅ {label} Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab",    value=tab_name, inline=True)
    embed.add_field(name="Teams",        value={"both": "A & B", "A": "A only", "B": "B only"}[teams], inline=True)
    # Team time-slot mapping (#251) — surfaced inline alongside the
    # other event-shape fields so officers can confirm their slot picks
    # made it through the wizard.
    def _slot_lbl(idx):
        if idx in (1, 2) and len(slot_labels) >= idx:
            return slot_labels[idx - 1]
        return "—"
    if teams == "A":
        embed.add_field(name="Team Times", value=f"A: {_slot_lbl(team_a_slot)}", inline=False)
    elif teams == "B":
        embed.add_field(name="Team Times", value=f"B: {_slot_lbl(team_b_slot)}", inline=False)
    else:
        embed.add_field(
            name="Team Times",
            value=f"A: {_slot_lbl(team_a_slot)} · B: {_slot_lbl(team_b_slot)}",
            inline=False,
        )
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
    if structured_cfg["structured_flow_enabled"]:
        _pwr_letter = structured_cfg.get("power_metric_column", "B")
        _pwr_tab = structured_cfg.get("power_metric_tab", "") or ""
        _pwr_match = structured_cfg.get("power_match_column", "") or ""
        if _pwr_tab:
            power_blurb = (
                f"Power source: `{_pwr_tab}` · column `{_pwr_letter}`"
                + (f" · matched by `{_pwr_match}`" if _pwr_match else "")
            )
        else:
            power_blurb = f"Power column: `{_pwr_letter}`"
        signup_blurb = (
            f" · Sign-up channel: <#{structured_cfg['signup_channel_id']}>"
            if structured_cfg["signup_channel_id"] else ""
        )
        embed.add_field(
            name="Structured Roster Flow",
            value=(
                f"✅ Enabled · {power_blurb} · "
                f"Sub mode: `{structured_cfg['sub_mode']}`"
                f"{signup_blurb}"
            ),
            inline=False,
        )
    else:
        embed.add_field(
            name="Structured Roster Flow",
            value=f"❌ Disabled · Preset tabs: `{structured_cfg['strategies_tab']}` / `{structured_cfg['member_rules_tab']}`",
            inline=False,
        )
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

    # ── Inline "post first sign-up" offer (#144) ─────────────────────────
    #
    # Fires only when the structured flow is opted in, a sign-up channel
    # is configured, and no sign-up post has been recorded yet for this
    # guild + event type. Whether auto-scheduling was configured or
    # skipped, this gives the alliance one fully-live sign-up post right
    # at the end of setup — the discovery surface #144 is closing.
    if (
        structured_cfg["structured_flow_enabled"]
        and structured_cfg.get("signup_channel_id")
    ):
        try:
            import config as _config
            with _config._get_conn() as conn:
                already_posted = conn.execute(
                    "SELECT 1 FROM storm_registration_posts "
                    "WHERE guild_id = ? AND event_type = ? LIMIT 1",
                    (guild_id, event_type),
                ).fetchone() is not None
        except Exception:
            already_posted = True  # err on the side of not nagging
        if not already_posted:
            parent = "desertstorm" if event_type == "DS" else "canyonstorm"
            post_offer = _InlinePostFirstSignupOffer(
                owner_id=user.id, bot=bot, guild_id=guild_id,
                event_type=event_type, parent=parent, label=label,
            )
            post_offer.message = await channel.send(
                f"📣 Want to post your first {label} sign-up now? "
                f"It'll land in <#{structured_cfg['signup_channel_id']}> "
                f"with vote buttons members can click. You can also wait "
                f"for the auto-schedule to post it (if you set one up) "
                f"or run `{HUB_COMMAND[event_type]}` and click "
                f"**{HUB_BTN_POST_SIGNUP}** later.",
                view=post_offer,
            )
            await wait_view_or_cancel(post_offer, cancel_event)

    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] {label} config saved for guild {guild_id}")


# ── Step 6 helper: participation tracking sub-flow (#20 rework) ───────────────

# Free-tier question types are universally available; Premium types are gated
# in the wizard. `roster_names` is unique to participation logs — it draws
# from the roster source the user configures here.
#
# #244: `roster_multi_select` and `derived_count` are new per-member capture
# types. They write to the new `DS Member Log` / `CS Member Log` Sheet tab
# (one row per (event_date, member)) so the Trends Viewer in #246 can
# aggregate across events. `roster_names` keeps working unchanged for
# alliances on the legacy free-text-list pattern.
_PARTICIPATION_FREE_TYPES = [
    "text", "yes_no", "numeric", "roster_names", "roster_multi_select",
]
_PARTICIPATION_PREMIUM_TYPES = [
    "single_select", "multi_select", "date", "derived_count",
]
_PARTICIPATION_TYPE_LABELS = {
    "text":          "Text: short typed answer",
    "yes_no":        "Yes / No",
    "numeric":       "Numeric: number with optional min/max",
    "roster_names":  "Roster names: pick or type member names",
    "roster_multi_select": "Roster multi-select: pick members from a dropdown",
    "single_select": "💎 Single-select dropdown",
    "multi_select":  "💎 Multi-select dropdown",
    "date":          "💎 Date (formatted entry)",
    "derived_count": "💎 Derived count: bot counts past events per member",
}

# #244: question types that produce *per-member* data (one flag per
# member per event) rather than *event-level* data (one answer per
# event). These get written to the new `DS Member Log` / `CS Member
# Log` Sheet tab instead of the existing participation log tab. Used
# by `storm_log.run_log_flow` to branch the write paths.
_PARTICIPATION_PER_MEMBER_TYPES = ("roster_multi_select", "derived_count")


async def _run_participation_preset_picker_step(
    channel, bot, user, cancel_event, *,
    cmd_name: str,
    is_premium_flag: bool,
    existing_questions: list[dict],
    cap: int | None,
) -> list[dict] | None:
    """#247 — multi-select preset picker for participation questions.

    Returns the list of question dicts the officer picked (already
    converted from preset shape via `defaults.preset_to_question`).
    Empty list when the officer skipped or no presets remain.
    `None` on timeout / cancel — caller propagates.

    Filters out presets whose key is already in `existing_questions`
    so re-running setup doesn't duplicate columns. When nothing is
    left to offer, the function returns `[]` without bothering the
    officer.
    """
    import wizard_registry
    from defaults import (
        storm_participation_presets, preset_to_question,
    )

    # Always show both tiers' presets so free-tier officers can see
    # what Premium would unlock. The free-tier set is the base; the
    # Premium-only set is appended with a 💎 prefix on the visible
    # label so officers can tell them apart at a glance.
    free_set = storm_participation_presets(is_premium=False)
    full_set = storm_participation_presets(is_premium=True)
    free_keys = {p["key"] for p in free_set}
    existing_keys = {q.get("key") for q in existing_questions if q.get("key")}
    # Available list = everything except already-configured. Premium
    # presets stay in the visible list for free-tier users so they
    # can see what's available; picking one fires the upsell ack.
    available = [p for p in full_set if p["key"] not in existing_keys]
    if not available:
        return []

    def _is_premium_only(p: dict) -> bool:
        return p["key"] not in free_keys

    # Default-check the presets marked `default_checked` in defaults.py
    # (currently just "Did this member show up?"). Premium-only ones
    # are never default-checked on free tier — defaulting to a 💎
    # preset would force the upsell ack just for hitting Add.
    default_checked = {
        p["key"] for p in available
        if p.get("default_checked")
        and (is_premium_flag or not _is_premium_only(p))
    }

    class _PresetPickerView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            self.action: str | None = None
            self.selected: set[str] = set(default_checked)
            self.cancelled = False

            options = []
            for p in available[:25]:  # Discord 25-option cap
                emoji = p.get("emoji", "")
                # Premium-only on free tier: 💎 prefix + a Premium hint
                # in the description so officers can't miss it.
                if _is_premium_only(p) and not is_premium_flag:
                    label = f"💎 {emoji} {p['label']}"[:100]
                    description = f"💎 Premium · {p.get('description', '')}"[:100]
                else:
                    label = f"{emoji} {p['label']}"[:100]
                    description = p.get("description", "")[:100]
                options.append(discord.SelectOption(
                    label=label,
                    value=p["key"],
                    description=description,
                    default=(p["key"] in default_checked),
                ))
            sel = discord.ui.Select(
                placeholder="Pick the preset questions you want…",
                options=options,
                min_values=0,
                max_values=min(len(options), 25),
                row=0,
            )

            async def _on_select(inter: discord.Interaction):
                self.selected = set(sel.values)
                await inter.response.defer()

            sel.callback = _on_select
            self.add_item(sel)

        @discord.ui.button(
            label="✅ Add picked presets",
            style=discord.ButtonStyle.success, row=1,
        )
        async def add(self, inter: discord.Interaction, _btn):
            self.action = "add"
            for c in self.children:
                c.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(
            label="↩️ Skip presets",
            style=discord.ButtonStyle.secondary, row=1,
        )
        async def skip(self, inter: discord.Interaction, _btn):
            self.action = "skip"
            for c in self.children:
                c.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

    prem_count_visible = sum(1 for p in available if _is_premium_only(p))
    tier_note = ""
    if is_premium_flag:
        if prem_count_visible > 0:
            tier_note = f"\n💎 *Includes {prem_count_visible} Premium preset(s).*"
    else:
        if prem_count_visible > 0:
            tier_note = (
                f"\n💎 *Presets marked with 💎 are Premium-only. Run "
                f"`/upgrade` to unlock {prem_count_visible} additional "
                f"preset(s).*"
            )

    view = _PresetPickerView()
    await channel.send(
        f"**Step 7.6: Use any preset questions?**\n"
        f"Pre-configured templates for the common participation "
        f"questions. Pick any you want and they'll land in your "
        f"question list ready to use. You can still customise them "
        f"later in the next step.{tier_note}",
        view=view,
    )
    await wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return None
    if view.action is None:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
        return None
    if view.action == "skip" or not view.selected:
        return []

    # Drop Premium-only picks on free tier (the picker shows them as a
    # teaser; selecting one surfaces the upsell). Officers can still
    # pick the free presets in the same submission.
    selected_in_order = [p for p in available if p["key"] in view.selected]
    if not is_premium_flag:
        premium_picks = [p for p in selected_in_order if _is_premium_only(p)]
        if premium_picks:
            labels = ", ".join(f"**{p['label']}**" for p in premium_picks)
            await channel.send(
                f"💎 *Skipped Premium-only preset(s):* {labels}. Run "
                f"`/upgrade` to unlock them, then re-run setup to add."
            )
            selected_in_order = [
                p for p in selected_in_order if not _is_premium_only(p)
            ]
        if not selected_in_order:
            return []

    # Enforce free-tier cap. Premium has no cap. Trim selections by
    # cap-already-spent so officers don't accidentally land past it.
    if cap is not None:
        room = max(0, cap - len(existing_questions))
        if room < len(selected_in_order):
            await channel.send(
                f"⚠️ Free tier caps participation questions at {cap}. "
                f"You picked {len(selected_in_order)} preset(s), but only "
                f"{room} fit. Added the first {room}; ignore or upgrade "
                f"for the rest."
            )
            selected_in_order = selected_in_order[:room]

    additions = [preset_to_question(p) for p in selected_in_order]

    # Dependency check: derived_count presets reference a source
    # question. Warn if the officer picked a derived_count without its
    # source AND the source isn't already configured. The question
    # still lands in the list — it'll just be inert until the source
    # exists.
    added_keys = {q["key"] for q in additions} | existing_keys
    warnings: list[str] = []
    for q in additions:
        src = q.get("source_question_key")
        if not src:
            continue
        if src in added_keys:
            continue
        # Find the source preset's friendly label, if any.
        src_label = src
        for p in storm_participation_presets(True):
            if p["key"] == src:
                src_label = p["label"]
                break
        warnings.append(
            f"⚠️ **{q['label']}** needs the **{src_label}** question "
            f"as its source. Pick that one too in the next step, or "
            f"this column will stay empty until the source is added."
        )
    if warnings:
        await channel.send("\n".join(warnings))

    summary = ", ".join(f"**{q['label']}**" for q in additions)
    await channel.send(f"✅ Added preset(s): {summary}")
    return additions


async def _run_storm_participation_step(
    channel, bot, user, cancel_event, *,
    guild_id: int, event_type: str, label: str, cmd_name: str,
    is_premium_flag: bool, current: dict,
) -> dict | None:
    """
    Step 6 of the storm setup wizard (DS + CS). Walks leadership
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
    # Treat any prior saved row (enabled true OR disabled with config
    # bits set) as "re-entry" so the Yes/No prompt offers Keep current
    # instead of forcing the officer to re-pick. A pristine row has
    # everything zero / empty.
    part_previously_saved = (
        bool(cur_part.get("enabled"))
        or bool(cur_part.get("tab_name"))
        or bool(cur_part.get("questions"))
    )

    # ── 6.1 Enable? ────────────────────────────────────────────────────────────
    parent_cmd = "desertstorm" if event_type == "DS" else "canyonstorm"
    enable_prompt = (
        f"**Step 7 of 9: Participation Tracking**\n"
        f"Do you want to track {label} participation? Leadership clicks "
        f"**📊 Fill out participation questions** on `/{parent_cmd}` "
        f"after each event to log who showed up, who sat out, etc.\n"
        f"You'll define the questions yourself, so the tracker matches how "
        f"your alliance runs the event."
    )
    if part_previously_saved:
        gate = _KeepOrFlipYesNoGate(current_value=bool(cur_part.get("enabled")))
        await channel.send(enable_prompt, view=gate)
        await wait_view_or_cancel(gate, cancel_event)
        if getattr(gate, "cancelled", False):
            return None
        if gate.value is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        enable_selected = bool(gate.value)
    else:
        enable_view = YesNoView()
        await channel.send(enable_prompt, view=enable_view)
        await wait_view_or_cancel(enable_view, cancel_event)
        if enable_view.cancelled:
            return None
        if enable_view.selected is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        enable_selected = bool(enable_view.selected)

    if not enable_selected:
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
        f"**Step 7.1: Participation Sheet Tab**\n"
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
        f"**Step 7.2: Roster Source: Sheet Tab**\n"
        f"Which tab in your sheet has the list of members? The bot reads "
        f"member names from here when you use a `Roster names` question.\n"
        f"*Tip: this is often the same tab you use for `/setup` → {HUB_BTN_SURVEY} or "
        f"`/setup` → {HUB_BTN_BIRTHDAYS}.*",
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
        f"**Step 7.3: Roster Source: Name Column**\n"
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

    # Re-entry: if the alliance previously configured a roster alias
    # column (saved as >= 0) OR explicitly opted out (-1) on a prior
    # save, surface the keep-or-flip gate instead of plain Yes/No.
    saved_alias_idx = cur_part.get("roster_alias_col")
    alias_was_previously_answered = (
        part_previously_saved
        and isinstance(saved_alias_idx, int)
    )
    alias_prompt = (
        "**Step 7.4: Roster Source: Alias Column?**\n"
        "If you have other names or nicknames that you call your members in these "
        "mails, this helps resolve to their full name in your sheet automatically. "
        "Do you have an alias column?"
    )
    if alias_was_previously_answered:
        alias_gate = _KeepOrFlipYesNoGate(
            current_value=(saved_alias_idx >= 0),
        )
        await channel.send(alias_prompt, view=alias_gate)
        await wait_view_or_cancel(alias_gate, cancel_event)
        if getattr(alias_gate, "cancelled", False):
            return None
        if alias_gate.value is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        alias_selected = bool(alias_gate.value)
    else:
        alias_view = YesNoView()
        await channel.send(alias_prompt, view=alias_view)
        await wait_view_or_cancel(alias_view, cancel_event)
        if alias_view.cancelled:
            return None
        if alias_view.selected is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        alias_selected = bool(alias_view.selected)

    roster_alias_col = -1
    if alias_selected:
        saved_alias = cur_part.get("roster_alias_col")
        # Default the alias column to Member Sync's `display_col` slot
        # when sync is enabled — that's where the bot writes the
        # Discord display name (the closest thing to an alias the bot
        # maintains). Otherwise fall back to the historic
        # "column right after the name column" convention.
        from config import get_member_roster_config as _gmrc_alias
        _sync_cfg_alias = _gmrc_alias(guild_id) if guild_id else {}
        if _sync_cfg_alias.get("enabled"):
            _alias_default_letter = _col_index_to_letter(
                int(_sync_cfg_alias.get("display_col", 2))
            )
        else:
            _alias_default_letter = _col_index_to_letter(roster_name_col + 1)
        raw_alias = await ask_keep_or_change(
            channel,
            "**Alias Column**\nWhich column letter has the alias / nickname?",
            default=_alias_default_letter,
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
        "**Step 7.5: Roster Source: First Data Row**\n"
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

    # ── 6.6 Preset picker (#247) ───────────────────────────────────────────────
    # Offer pre-configured question templates so officers don't have to
    # spell out the common shapes by hand. Free tier sees the 3 base
    # templates; Premium sees the 3 derived/auto-prefill ones too. The
    # picker filters out templates already present in the existing
    # questions list (re-entry case) — re-run setup then "add from
    # presets" without duplicating columns.
    questions = list(cur_part.get("questions") or [])
    cap = None if is_premium_flag else 3
    preset_additions = await _run_participation_preset_picker_step(
        channel, bot, user, cancel_event,
        cmd_name=cmd_name,
        is_premium_flag=is_premium_flag,
        existing_questions=questions,
        cap=cap,
    )
    if preset_additions is None:
        return None
    questions.extend(preset_additions)

    def _summarize() -> str:
        if not questions:
            return "*(no questions yet; every participation log will only ask for the date)*"
        lines = []
        for i, q in enumerate(questions, start=1):
            t = _PARTICIPATION_TYPE_LABELS.get(q.get("type"), q.get("type", "?"))
            lines.append(f"**{i}. {q.get('label', '?')}**: _{t}_")
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
                        await wizard_registry.safe_edit_response(inter, view=self)
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
                        await wizard_registry.safe_edit_response(inter, view=self)
                        self.stop()
                    del_sel.callback = _dc
                    self.add_item(del_sel)

            @discord.ui.button(label="➕ Add question", style=discord.ButtonStyle.primary, row=2)
            async def add_q(self, inter: discord.Interaction, button: discord.ui.Button):
                self.action = "add"
                for c in self.children: c.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

            @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=2)
            async def done(self, inter: discord.Interaction, button: discord.ui.Button):
                self.action = "done"
                for c in self.children: c.disabled = True
                await wizard_registry.safe_edit_response(inter, view=self)
                self.stop()

        cap_note = (
            f"\n*Free tier limit: {cap} questions.*"
            if cap is not None else
            "\n💎 *Premium: unlimited questions and three extra question types.*"
        )
        view = _BuilderView(len(questions))
        await channel.send(
            f"**Step 7.7: Participation Questions**\n"
            f"Each question becomes a column on your sheet and a step in "
            f"the **📊 Fill out participation questions** flow on "
            f"`/{parent_cmd}`.\n"
            f"Examples: *Vote count*, *Sitting out*, *Did anyone show up late?*\n"
            f"{cap_note}\n\n{_summarize()}",
            view=view,
        )
        await wait_view_or_cancel(view, cancel_event)
        if view.cancelled:
            return
        if view.action is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
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
                all_questions=questions,
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


# ── Auto-schedule sub-flow (#131) ────────────────────────────────────────────


_DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday",
              "Friday", "Saturday", "Sunday"]


async def _ask_signup_schedule(
    channel, bot, user, cancel_event, *,
    label: str, cmd_name: str,
    current_dow: int, current_time: str,
    tz_label: str = "",
    event_type: str = "DS",
) -> dict | None:
    """Two-step Premium sub-flow for the auto-scheduler config:
        * Poll day-of-week (dropdown; or "Skip auto-scheduling").
          Event day is game-defined (DS = Friday, CS = Thursday); the
          dropdown only shows poll days that sit between the previous
          event and the in-game roster lock.
        * Sign-up post time (HH:MM in guild timezone; modal). Required
          when a day is picked — Rule F / #163.

    Returns `{"dow": int, "time": str}`. `dow = -1` indicates the
    alliance explicitly opted out of auto-scheduling (manual
    `/<parent> post_signup` remains usable). Returns None on cancel
    or timeout — callers should propagate the None.
    """
    parent = "desertstorm" if event_type == "DS" else "canyonstorm"
    import wizard_registry

    # Per Rule H, valid poll days per event type sit between the day
    # AFTER the previous event and the day BEFORE the in-game roster
    # lock. Event days themselves are excluded (game-defined: DS=Fri,
    # CS=Thu) so same-day poll/event is impossible by construction.
    # 0=Monday..6=Sunday.
    if event_type == "DS":
        # DS event = Friday; roster locks Wednesday before reset.
        # Valid poll days: Sat, Sun, Mon, Tue, Wed.
        poll_options = [5, 6, 0, 1, 2]
        event_label = "Friday"
    else:
        # CS event = Thursday; roster locks Monday before reset.
        # Valid poll days: Fri, Sat, Sun, Mon.
        poll_options = [4, 5, 6, 0]
        event_label = "Thursday"

    # ── Step 1: poll day-of-week ──
    class _DowView(discord.ui.View):
        def __init__(self, current: int):
            super().__init__(timeout=300)
            self.selected: int | None = None
            self.cancelled = False

            # Keep-current button (#80 pattern). Re-selecting an already
            # default-marked dropdown option doesn't read as "save" to
            # leadership, so the picker also exposes an explicit
            # Keep-current affordance like every other /setup_* re-entry
            # surface. Label reflects whatever the saved value is: the
            # day name when a poll day was configured, "Skip
            # auto-scheduling" when the alliance opted out previously.
            if 0 <= current <= 6 and current in poll_options:
                keep_label = f"✅ Keep current: {_DOW_NAMES[current]}"
            else:
                keep_label = "✅ Keep current: Skip auto-scheduling"
            keep_btn = discord.ui.Button(
                label=keep_label[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _on_keep(inter: discord.Interaction):
                self.selected = current if (0 <= current <= 6 and current in poll_options) else -1
                for item in self.children: item.disabled = True
                if self.selected < 0:
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=(
                            f"✅ Auto-scheduling stays skipped. Post "
                            f"manually via `{HUB_COMMAND[event_type]}` → "
                            f"**{HUB_BTN_POST_SIGNUP}** when you're ready."
                        ),
                        view=self,
                    )
                else:
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=f"✅ Keeping poll day: **{_DOW_NAMES[self.selected]}**.",
                        view=self,
                    )
                self.stop()

            keep_btn.callback = _on_keep
            self.add_item(keep_btn)

            options = [
                discord.SelectOption(
                    label=_DOW_NAMES[i], value=str(i),
                    default=(i == current),
                )
                for i in poll_options
            ]
            options.append(discord.SelectOption(
                label="Skip auto-scheduling (post manually from the hub)",
                value="-1",
                default=(current < 0),
            ))
            sel = discord.ui.Select(
                placeholder="When should the bot post the sign-up poll?",
                min_values=1, max_values=1,
                options=options,
                row=1,
            )

            async def _on_pick(inter: discord.Interaction):
                try:
                    self.selected = int(sel.values[0])
                except ValueError:
                    self.selected = -1
                for item in self.children: item.disabled = True
                if self.selected < 0:
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=(
                            f"✅ Auto-scheduling skipped. Post manually "
                            f"via `{HUB_COMMAND[event_type]}` → "
                            f"**{HUB_BTN_POST_SIGNUP}** when you're ready."
                        ),
                        view=self,
                    )
                else:
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=f"✅ Poll day: **{_DOW_NAMES[self.selected]}**.",
                        view=self,
                    )
                self.stop()

            sel.callback = _on_pick
            self.add_item(sel)

    dow_view = _DowView(int(current_dow if current_dow is not None else -1))
    await channel.send(
        f"**Auto-Schedule: Poll Day (💎 Premium)**\n"
        f"**{label}** runs every **{event_label}** in-game. Which day "
        f"do you want the bot to post the sign-up poll? (The dropdown "
        f"shows only days that sit between the previous event and the "
        f"in-game roster lock.)",
        view=dow_view,
    )
    await wait_view_or_cancel(dow_view, cancel_event)
    if dow_view.cancelled:
        return None
    if dow_view.selected is None:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
        return None
    if dow_view.selected < 0:
        # Skipped — return cleared schedule.
        return {"dow": -1, "time": ""}

    # ── Step 3: sign-up time ──
    # The alliance opted into auto-scheduling at Step 1 (`dow >= 0`),
    # so the time field is required (#163 / Rule F). Empty submissions
    # surface a one-line re-prompt and the modal re-opens.
    #
    # Time copy follows the existing convention used by train / birthday
    # / shiny setup: 12-hour clock for display + parsing, with the
    # guild's local timezone surfaced inline. Storage stays 24-hour HH:MM
    # so the scheduler doesn't have to disambiguate at fire time.
    saved_12h = _format_24h_to_12h(current_time) if current_time else ""
    tz_hint = f" *(in your timezone: {tz_label})*" if tz_label else ""
    time_clean: str | None = None
    for attempt in range(3):
        time_picked = await ask_keep_or_change(
            channel,
            f"**Auto-Schedule: Sign-Up Post Time**\n"
            f"What time should the bot fire the sign-up post?{tz_hint}\n"
            f"*(e.g. `2:00pm`, `9:00am`, or 24-hour `14:00`)*",
            default="12:00pm",
            current=saved_12h,
            modal_title="Sign-Up Time",
            modal_label="e.g. 2:00pm",
            timeout_cmd=cmd_name,
            cancel_event=cancel_event,
        )
        if time_picked is None:
            return None
        raw = str(time_picked).strip()
        if raw:
            time_clean = _parse_12h_time(raw) or _normalise_hhmm(raw) or "12:00"
            break
        # Empty submission — surface a friendly nudge and re-prompt.
        await channel.send(
            "⚠️ A sign-up time is required when auto-scheduling is on. "
            "Pick a time (e.g. `12:00pm`) or use the default."
        )
    if time_clean is None:
        # Three blank attempts in a row — fall back to the default so
        # the wizard doesn't loop forever.
        time_clean = _parse_12h_time("12:00pm") or "12:00"

    return {
        "dow":  dow_view.selected,
        "time": time_clean,
    }


def _normalise_hhmm(raw: str) -> str | None:
    """Thin wrapper around `config.parse_storm_signup_time` kept under
    the wizard's old name so existing tests / imports keep working.
    The canonical implementation lives in `config.py` so the scheduler
    and the wizard can't drift on parsing rules."""
    from config import parse_storm_signup_time
    return parse_storm_signup_time(raw)


# ── Keep-or-flip Yes/No re-entry gate ───────────────────────────────────────
# Used by power-refresh DM step (and previously the Judicator role step,
# now dropped per Rule G / #167).


class _KeepOrFlipYesNoGate(discord.ui.View):
    """Re-entry gate for a yes/no wizard step that already has a saved
    value. Two buttons: Keep current (success) / Flip (secondary).
    Sets `self.value` to the resolved bool, or None on timeout."""

    def __init__(
        self, *,
        current_value: bool,
        keep_label_yes: str = "✅ Keep current: Yes",
        keep_label_no: str = "✅ Keep current: No",
        flip_label_yes: str = "↩️ Switch to: Yes",
        flip_label_no: str = "↩️ Switch to: No",
    ):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.value: bool | None = None
        self.cancelled = False

        keep_label = keep_label_yes if current_value else keep_label_no
        flip_label = flip_label_no if current_value else flip_label_yes

        keep_btn = discord.ui.Button(
            label=keep_label[:80], style=discord.ButtonStyle.success,
        )

        async def _keep_cb(inter: discord.Interaction):
            self.value = bool(current_value)
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Keeping **{'Yes' if self.value else 'No'}**",
                view=self,
            )
            self.stop()

        keep_btn.callback = _keep_cb
        self.add_item(keep_btn)

        flip_btn = discord.ui.Button(
            label=flip_label[:80], style=discord.ButtonStyle.secondary,
        )

        async def _flip_cb(inter: discord.Interaction):
            self.value = not bool(current_value)
            for item in self.children: item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Switched to **{'Yes' if self.value else 'No'}**",
                view=self,
            )
            self.stop()

        flip_btn.callback = _flip_cb
        self.add_item(flip_btn)


# ── Inline-create offers for the structured-flow setup wizard (#144) ─────────
#
# Each offer is a Yes/No view posted after the relevant tab name is saved.
# It only appears when the underlying table is empty — alliances running
# setup a second time see their saved values surface as defaults but don't
# get re-prompted to create the first row.


class _InlineCreatePresetOffer(discord.ui.View):
    """Posted after the Strategy Presets tab name is saved (and the
    alliance has zero presets). 'Create now' opens the same preset
    editor as `/<parent> strategy create`."""

    def __init__(self, *, owner_id: int, event_type: str, parent: str,
                 default_name: str = "Standard"):
        super().__init__(timeout=300)
        self.owner_id     = owner_id
        self.event_type   = event_type
        self.parent       = parent
        self.default_name = default_name
        self.choice: str | None = None
        self.message: discord.Message | None = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "Only the user running setup can pick.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="✨ Create my first preset now",
                       style=discord.ButtonStyle.primary)
    async def create_btn(self, inter: discord.Interaction, _btn):
        self.choice = "create"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        try:
            from storm_strategy import seed_default_preset, open_editor_followup
            buf = seed_default_preset(self.default_name, self.event_type)
            buf.dirty = True
            await open_editor_followup(inter, self.event_type, buf)
        except Exception as e:
            await inter.followup.send(
                f"⚠️ Couldn't open the preset editor inline: {e}. "
                f"Run `{HUB_COMMAND[self.event_type]}` and click "
                f"**{HUB_BTN_PRESETS}** to retry.",
                ephemeral=True,
            )
        self.stop()

    @discord.ui.button(label="Skip for now", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, inter: discord.Interaction, _btn):
        self.choice = "skip"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message
        await expire_view_message(
            self.message,
            command_hint=f"`{HUB_COMMAND[self.event_type]}` → **{HUB_BTN_PRESETS}**",
        )


class _InlineCreateMemberRuleOffer(discord.ui.View):
    """Posted after the Member Rules tab name is saved (and the alliance
    has zero rules). 'Add one now' opens a streamlined modal for a
    power-band rule — the most-common rule type. Per-member rules (which
    need a Discord member picker) remain available via the slash commands."""

    def __init__(self, *, owner_id: int, event_type: str, parent: str):
        super().__init__(timeout=300)
        self.owner_id   = owner_id
        self.event_type = event_type
        self.parent     = parent
        self.choice: str | None = None
        self.message: discord.Message | None = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "Only the user running setup can pick.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="✨ Add a power-band rule now",
                       style=discord.ButtonStyle.primary)
    async def create_btn(self, inter: discord.Interaction, _btn):
        self.choice = "create"
        for child in self.children:
            child.disabled = True
        # Disable the offer message via the bot-owned message handle so a
        # fast second click can't fire a duplicate picker. The picker
        # itself sends a fresh ephemeral with its own select + modal.
        try:
            from storm_member_rules import InlinePowerBandView
            picker = InlinePowerBandView(self.event_type, owner_id=inter.user.id)
            await inter.response.send_message(
                content=(
                    "Pick the zone the rule applies to, then click "
                    "**Set minimum power** to enter the threshold."
                ),
                view=picker, ephemeral=True,
            )
            picker.message = await inter.original_response()
        except Exception as e:
            await inter.response.send_message(
                f"⚠️ Couldn't open the rule picker: {e}. Run "
                f"`{HUB_COMMAND[self.event_type]}` and click "
                f"**{HUB_BTN_RULES}** to retry.",
                ephemeral=True,
            )
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()

    @discord.ui.button(label="Skip for now", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, inter: discord.Interaction, _btn):
        self.choice = "skip"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message
        await expire_view_message(
            self.message,
            command_hint=f"`{HUB_COMMAND[self.event_type]}` → **{HUB_BTN_RULES}**",
        )


class _InlinePostFirstSignupOffer(discord.ui.View):
    """Posted at the end of the storm setup wizard (DS / CS) when
    the structured flow is opted in, a sign-up channel is configured,
    and no sign-up post has been recorded yet. 'Post now' fires
    `post_registration` against the next configured event date."""

    def __init__(self, *, owner_id: int, bot, guild_id: int,
                 event_type: str, parent: str, label: str):
        super().__init__(timeout=300)
        self.owner_id   = owner_id
        self.bot        = bot
        self.guild_id   = guild_id
        self.event_type = event_type
        self.parent     = parent
        self.label      = label
        self.choice: str | None = None
        self.message: discord.Message | None = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "Only the user who ran setup can pick.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="📣 Post my first sign-up now",
                       style=discord.ButtonStyle.primary)
    async def post_btn(self, inter: discord.Interaction, _btn):
        self.choice = "post"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        try:
            from storm_date_helpers import next_event_date
            from storm_signup_post import post_registration, _format_post_result_message
            from config import get_structured_storm_config
            target_date = next_event_date(self.guild_id, self.event_type)
            structured  = get_structured_storm_config(self.guild_id, self.event_type)
            guild = self.bot.get_guild(self.guild_id)
            if guild is None:
                await inter.followup.send(
                    "⚠️ The bot can't see this guild right now. Try again "
                    f"via `{HUB_COMMAND[self.event_type]}` → "
                    f"**{HUB_BTN_POST_SIGNUP}**.",
                    ephemeral=True,
                )
                return
            result = await post_registration(
                self.bot, guild, self.event_type, target_date,
                structured=structured,
                # Leadership-triggered repost (#265): bypass the
                # once-per-event guard so the first-sign-up wizard
                # button can post even if an earlier post is still on
                # the channel.
                force=True,
            )
            await inter.followup.send(
                _format_post_result_message(self.event_type, target_date, result),
                ephemeral=True,
            )
        except Exception as e:
            await inter.followup.send(
                f"⚠️ Sign-up post failed: {e}. Run "
                f"`{HUB_COMMAND[self.event_type]}` and click "
                f"**{HUB_BTN_POST_SIGNUP}** to retry.",
                ephemeral=True,
            )
        self.stop()

    @discord.ui.button(label="Skip: I'll post later", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, inter: discord.Interaction, _btn):
        self.choice = "skip"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message
        await expire_view_message(
            self.message,
            command_hint=f"`{HUB_COMMAND[self.event_type]}` → **{HUB_BTN_POST_SIGNUP}**",
        )


# ── Structured storm flow setup sub-flow (#38 + #54) ─────────────────────────

async def _run_structured_flow_setup_step(
    channel, bot, user, cancel_event, *,
    guild_id: int, event_type: str, label: str, cmd_name: str,
    is_premium_flag: bool, current: dict, current_structured: dict,
    interaction_guild,
) -> dict | None:
    """
    Final block of the storm setup wizard (DS + CS). Walks
    leadership through enabling the structured roster flow (Premium, #38)
    and / or configuring the strategy preset + member rules tabs (free,
    #54). Returns a dict shaped like save_structured_storm_config's
    kwargs, or None if the user cancelled or timed out.

    Branching:
      * Premium + opted-in: full config (power column, sub mode, signup
        channel, all 5 tab names).
      * Premium without opt-in / free tier: preset library tab names only
        (strategies_tab + member_rules_tab). Other fields keep their
        current values so nothing gets cleared by accident.

    The registration-post schedule UI is deferred to the registration
    post sub-issue — this sub-flow leaves `signup_schedule_cron`
    untouched.
    """
    import wizard_registry

    # Build the result on top of the current saved values so that
    # opting *out* of the structured flow doesn't clear previously
    # configured fields (e.g. an alliance that opted out for one week
    # shouldn't lose its preset tab names).
    result = dict(current_structured)
    # Force-coerce to the keys save_structured_storm_config expects.
    result.setdefault("structured_flow_enabled", False)
    result.setdefault("power_metric_column", "B")
    result.setdefault("power_metric_tab", "")
    result.setdefault("power_match_column", "")
    result.setdefault("sub_mode", "pool")
    result.setdefault("signup_channel_id", 0)
    result.setdefault("signup_schedule_cron", "")
    for tab in ("signups_tab", "rosters_tab", "attendance_tab",
                "strategies_tab", "member_rules_tab"):
        result.setdefault(tab, "")
    result.setdefault("poll_day_of_week", -1)
    result.setdefault("signup_time", "")
    result.setdefault("power_refresh_dm_enabled", False)
    # Stale-power DM nudge (#255). All four fields default to off;
    # the wizard surfaces them as a follow-up step only when the
    # primary power-refresh DM toggle is on.
    result.setdefault("power_last_updated_tab", "")
    result.setdefault("power_last_updated_column", "")
    result.setdefault("power_last_updated_match_column", "")
    result.setdefault("power_refresh_stale_days", 0)
    # Roster DM templates (#226 follow-up). Persisted via
    # `save_roster_dm_templates`, distinct from
    # `save_structured_storm_config`, but stashed in the same result
    # dict so the wizard's re-entry surface and the save call site
    # can hand them off together.
    result.setdefault("roster_dm_starter_template", "")
    result.setdefault("roster_dm_paired_sub_template", "")
    result.setdefault("roster_dm_pool_sub_template", "")

    # Internal slug for channel-name suggestion (e.g. `desertstorm-signups`).
    # Derived from event_type directly so it stays a clean slug even
    # after cmd_name became a user-facing hint pointing at the `/setup`
    # hub button (#201).
    cmd_short = "desertstorm" if event_type == "DS" else "canyonstorm"

    # ── Premium opt-in question ────────────────────────────────────────────
    structured_opted_in = False
    if is_premium_flag:
        await channel.send(
            f"**Step 8 of 9: Structured Roster Flow (💎 Premium)**\n"
            f"The structured flow auto-posts a Discord sign-up poll, captures "
            f"votes per member, and gives leadership a roster builder that "
            f"filters members by power for each zone. Replaces the text-template "
            f"draft for {label} when enabled. You can leave this off and still "
            f"use the strategy preset library on the free tier."
        )
        # Re-entry: the alliance has a previously saved decision (either
        # explicit on or explicit off after running the wizard before),
        # so offer Keep-current / Flip rather than forcing a re-pick of
        # plain Yes/No. `get_structured_storm_config` always returns a
        # dict with `structured_flow_enabled`, so detect prior setup by
        # asking has_storm_config directly.
        from config import has_storm_config
        already_decided = has_storm_config(guild_id, event_type)
        if already_decided:
            structured_gate = _KeepOrFlipYesNoGate(
                current_value=bool(
                    current_structured.get("structured_flow_enabled")
                ),
            )
            await channel.send(
                f"Turn on the structured flow for {label}?",
                view=structured_gate,
            )
            await wait_view_or_cancel(structured_gate, cancel_event)
            if getattr(structured_gate, "cancelled", False):
                return None
            if structured_gate.value is None:
                await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
                return None
            structured_opted_in = bool(structured_gate.value)
        else:
            enable_view = YesNoView()
            await channel.send(
                f"Turn on the structured flow for {label}?",
                view=enable_view,
            )
            await wait_view_or_cancel(enable_view, cancel_event)
            if getattr(enable_view, "cancelled", False):
                return None
            if enable_view.selected is None:
                await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
                return None
            structured_opted_in = bool(enable_view.selected)
    result["structured_flow_enabled"] = structured_opted_in

    # ── Premium + opted-in: full config ────────────────────────────────────
    if structured_opted_in:
        # Power Data Source step. Replaces the old single-letter
        # "Power Metric Column" prompt with a tab + column + match-column
        # triple. The alliance can point storm at any tab: the Member
        # Roster (default), the bot's own Squad Powers tab if they use
        # the Survey, or a custom external tab. The match column
        # identifies each row — at read time the bot tries Discord ID
        # first when the cell looks like one, otherwise it falls back
        # to case-insensitive name match.
        from config import get_member_roster_config
        _roster_cfg_for_defaults = get_member_roster_config(guild_id)
        _sync_enabled = bool(_roster_cfg_for_defaults.get("enabled"))
        default_tab = (
            (_roster_cfg_for_defaults.get("tab_name") or "Member Roster")
            if _sync_enabled else "Member Roster"
        )
        if _sync_enabled:
            default_match_letter = _col_index_to_letter(
                int(_roster_cfg_for_defaults.get("discord_id_col", 0))
            )
        else:
            default_match_letter = "A"

        saved_tab = (result.get("power_metric_tab") or "").strip()
        saved_letter = (
            result.get("power_metric_column") or "B"
        ).strip().upper()
        if not (len(saved_letter) == 1 and "A" <= saved_letter <= "Z"):
            saved_letter = "B"
        saved_match = (result.get("power_match_column") or "").strip().upper()
        if not (len(saved_match) == 1 and "A" <= saved_match <= "Z"):
            saved_match = ""

        # `has_custom` = the alliance has saved values that differ from
        # the defaults. Drives the 2-button vs 3-button picker layout
        # below (same idiom `ask_keep_or_change` uses).
        effective_tab = saved_tab or default_tab
        effective_match = saved_match or default_match_letter
        has_custom = (
            saved_tab != ""
            or saved_letter != "B"
            or saved_match != ""
        )

        class _PowerDataSourceModal(discord.ui.Modal):
            def __init__(self):
                super().__init__(title="Power Data Source")
                self.confirmed = False
                self.tab_input = discord.ui.TextInput(
                    label="Power source tab",
                    placeholder="e.g. Member Roster, Squad Powers",
                    default=effective_tab,
                    required=True,
                    max_length=100,
                )
                self.col_input = discord.ui.TextInput(
                    label="Power column letter (A-Z)",
                    placeholder="e.g. B",
                    default=saved_letter,
                    required=True,
                    max_length=2,
                )
                self.match_input = discord.ui.TextInput(
                    label="Member-match column letter (A-Z)",
                    placeholder="e.g. B — prefer column with Discord IDs",
                    default=effective_match,
                    required=True,
                    max_length=2,
                )
                self.add_item(self.tab_input)
                self.add_item(self.col_input)
                self.add_item(self.match_input)

            async def on_submit(self, inter: discord.Interaction):
                self.confirmed = True
                await inter.response.defer()
                self.stop()

        class _PowerDataSourcePickerView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.outcome: str | None = None  # "default" | "keep" | "edit"
                self.modal: _PowerDataSourceModal | None = None

            @discord.ui.button(
                label="✅ Use defaults",
                style=discord.ButtonStyle.success,
            )
            async def use_default(
                self, inter: discord.Interaction, _btn: discord.ui.Button,
            ):
                self.outcome = "default"
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=(
                        f"✅ Using defaults: tab `{default_tab}`, "
                        f"power column `B`, matched by `{default_match_letter}`."
                    ),
                    view=self,
                )
                self.stop()

            @discord.ui.button(
                label="✅ Keep current",
                style=discord.ButtonStyle.success,
            )
            async def keep_current(
                self, inter: discord.Interaction, _btn: discord.ui.Button,
            ):
                self.outcome = "keep"
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=(
                        f"✅ Keeping current: tab `{effective_tab}`, "
                        f"power column `{saved_letter}`, matched by "
                        f"`{effective_match}`."
                    ),
                    view=self,
                )
                self.stop()

            @discord.ui.button(
                label="✏️ Define my own",
                style=discord.ButtonStyle.secondary,
            )
            async def define(
                self, inter: discord.Interaction, _btn: discord.ui.Button,
            ):
                self.modal = _PowerDataSourceModal()
                await inter.response.send_modal(self.modal)
                await self.modal.wait()
                self.outcome = "edit" if self.modal.confirmed else None
                for item in self.children:
                    item.disabled = True
                try:
                    if inter.message:
                        await inter.message.edit(view=self)
                except discord.HTTPException:
                    pass
                self.stop()

        picker = _PowerDataSourcePickerView()
        # Drop the Keep current button when the alliance hasn't
        # customised yet — promotes Use defaults to the only success
        # action. Drop Use defaults when the alliance HAS customised
        # so they don't accidentally wipe their saved values.
        if has_custom:
            picker.remove_item(picker.use_default)
            picker.keep_current.label = (
                f"✅ Keep current: {effective_tab} · {saved_letter} · "
                f"matched by {effective_match}"[:80]
            )
        else:
            picker.remove_item(picker.keep_current)
            picker.use_default.label = (
                f"✅ Use defaults: {default_tab} · B · matched by "
                f"{default_match_letter}"[:80]
            )

        sync_blurb = (
            f"\n\n_Member Sync is enabled, so we're suggesting tab "
            f"`{default_tab}` matched by column `{default_match_letter}` "
            f"(the bot's Discord ID slot)._"
            if _sync_enabled else
            "\n\n_Member Sync isn't enabled yet — the default tab "
            "name is just a placeholder; pick whichever tab actually "
            "has your power data._"
        )
        await channel.send(
            f"**Power Data Source**\n"
            f"Tell the bot which Google Sheet tab + column has each "
            f"member's power value. Storm uses this to gate zone "
            f"eligibility by power. You can keep power in the Member "
            f"Roster, a Survey tab, or any custom tab.\n\n"
            f"• **Tab**: the Sheet tab where power lives.\n"
            f"• **Power column**: the column with the actual power "
            f"value (e.g. `B`).\n"
            f"• **Member-match column**: the column the bot uses to "
            f"match rows to your alliance members. Cells that look "
            f"like Discord IDs match by ID; otherwise the bot matches "
            f"by name (case-insensitive). Preferably the column where "
            f"you have Discord IDs. You can also select a Name column "
            f"if you'd prefer, but matching may be less reliable than "
            f"an ID."
            f"{sync_blurb}",
            view=picker,
        )
        await wait_view_or_cancel(picker, cancel_event)
        if getattr(picker, "cancelled", False):
            return None
        if picker.outcome is None:
            await channel.send(
                GENERIC_CMD_TIMEOUT.format(cmd=cmd_name)
            )
            return None

        if picker.outcome == "default":
            # Persist empty tab + match so a future default change
            # propagates without re-running the wizard.
            result["power_metric_tab"] = ""
            result["power_metric_column"] = "B"
            result["power_match_column"] = ""
        elif picker.outcome == "keep":
            # Saved values already in `result` from the initial
            # current_structured spread — leave as-is.
            pass
        else:  # edit
            modal = picker.modal
            assert modal is not None
            tab_val = (modal.tab_input.value or "").strip()
            col_val = (modal.col_input.value or "B").strip().upper()
            match_val = (modal.match_input.value or "").strip().upper()
            if not (len(col_val) == 1 and "A" <= col_val <= "Z"):
                col_val = "B"
            if not (len(match_val) == 1 and "A" <= match_val <= "Z"):
                match_val = ""  # empty → fall back to discord_id_col
            # Store tab verbatim only when it differs from the Member
            # Roster tab; otherwise persist empty so the read path
            # falls through to the canonical default.
            member_roster_tab = (
                _roster_cfg_for_defaults.get("tab_name") or "Member Roster"
            )
            result["power_metric_tab"] = (
                tab_val if tab_val and tab_val != member_roster_tab else ""
            )
            result["power_metric_column"] = col_val
            result["power_match_column"] = match_val

        # Sub mode — Kevin's first-sweep _edited convention: the
        # green/default button reads `Use Default: <X>` on first run
        # (no saved value) or `Use Current: <X>` on re-entry. The
        # other button shows its descriptive label. Matches the
        # `ask_keep_or_change` pattern the rest of the setup flow
        # uses so officers learn one button-label idiom across the
        # whole wizard.
        _saved_sub_mode = result.get("sub_mode")
        _has_saved_sub_mode = _saved_sub_mode in ("pool", "paired")
        _effective_mode = _saved_sub_mode if _has_saved_sub_mode else "pool"

        class SubModeView(discord.ui.View):
            def __init__(self, current_mode: str, has_saved: bool):
                super().__init__(timeout=120)
                self.selected = None
                self.cancelled = False
                if current_mode == "pool":
                    pool_label = (
                        "Use Current: Pool" if has_saved else "Use Default: Pool"
                    )
                    pool_style = discord.ButtonStyle.success
                    paired_label = "Paired: primary↔sub pairs"
                    paired_style = discord.ButtonStyle.primary
                else:
                    pool_label = "Pool: flat sub list"
                    pool_style = discord.ButtonStyle.primary
                    paired_label = "Use Current: Paired"
                    paired_style = discord.ButtonStyle.success

                pool_btn = discord.ui.Button(label=pool_label, style=pool_style)
                paired_btn = discord.ui.Button(label=paired_label, style=paired_style)

                async def _pool(inter):
                    self.selected = "pool"
                    for item in self.children: item.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter, content="✅ Sub mode: Pool", view=self
                    )
                    self.stop()

                async def _paired(inter):
                    self.selected = "paired"
                    for item in self.children: item.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter, content="✅ Sub mode: Paired", view=self
                    )
                    self.stop()

                pool_btn.callback = _pool
                paired_btn.callback = _paired
                self.add_item(pool_btn)
                self.add_item(paired_btn)

        sub_view = SubModeView(_effective_mode, _has_saved_sub_mode)
        await channel.send(
            "**Sub Mode**\n"
            "How should subs be tracked when leadership builds a roster?\n"
            "• **Pool**: flat list of subs; any sub can cover any primary no-show.\n"
            "• **Paired**: each primary has a specific sub assigned in advance.",
            view=sub_view,
        )
        await wait_view_or_cancel(sub_view, cancel_event)
        if sub_view.cancelled:
            return None
        if sub_view.selected is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        result["sub_mode"] = sub_view.selected

        # Registration post channel
        signup_ch_view = ChannelSelectStep(
            f"Select the channel where {label} sign-up polls post...",
            suggested_name=f"{cmd_short.replace('_', '-')}-signups",
            include_threads=True,
            guild=interaction_guild,
            current_id=result.get("signup_channel_id") or 0,
        )
        if signup_ch_view.is_current_stale:
            await channel.send(
                f"⚠️ Your previously configured {label} sign-up channel no longer "
                "exists. Select a new channel."
            )
        parent_cmd = "desertstorm" if event_type == "DS" else "canyonstorm"
        await channel.send(
            f"**{label} Sign-Up Channel**\n"
            "The bot will auto-post a sign-up poll here each week. Members click "
            "buttons to register their availability.\n"
            f"You can open the officer view via `/{parent_cmd} signups`.",
            view=signup_ch_view,
        )
        await wait_view_or_cancel(signup_ch_view, cancel_event)
        if signup_ch_view.cancelled:
            return None
        if not signup_ch_view.confirmed:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        result["signup_channel_id"] = signup_ch_view.selected_channel.id

        # Auto-schedule (#131) — Day-of-week + lead days + time-of-day.
        # All three together drive the storm_signup_scheduler loop. The
        # alliance can skip all three (leave defaults) and continue
        # running `/<parent> post_signup` manually.
        # Resolve tz_label for the time-of-day prompt so the wizard
        # shows "in your timezone: ET (America/New_York)" alongside the
        # 12-hour example — matches the train / birthday / shiny
        # patterns already established in this file.
        from config import get_config
        guild_cfg = get_config(guild_id) if guild_id else None
        tz_str = (
            guild_cfg.timezone if guild_cfg and guild_cfg.timezone
            else "America/New_York"
        )
        tz_label = TIMEZONE_LABELS.get(tz_str, tz_str)
        sched_result = await _ask_signup_schedule(
            channel, bot, user, cancel_event,
            label=label, cmd_name=cmd_name,
            current_dow=result.get("poll_day_of_week", -1),
            current_time=result.get("signup_time", ""),
            tz_label=tz_label,
            event_type=event_type,
        )
        if sched_result is None:
            return None
        result["poll_day_of_week"] = sched_result["dow"]
        result["signup_time"]      = sched_result["time"]

        # Sign-ups / rosters / attendance tab names — Premium only
        for tab_key, label_text in (
            ("signups_tab",    "Sign-Ups"),
            ("rosters_tab",    "Rosters"),
            ("attendance_tab", "Attendance"),
        ):
            from config import default_structured_tab
            tab_default = default_structured_tab(event_type, tab_key)
            picked = await ask_keep_or_change(
                channel,
                f"**{label_text} Tab**\n"
                f"Which Google Sheet tab should store {label} "
                f"{label_text.lower()}? The bot creates and maintains "
                f"this tab.",
                default=tab_default,
                current=result.get(tab_key, ""),
                modal_title=f"{label_text} Tab Name",
                modal_label="Tab name",
                timeout_cmd=cmd_name,
                cancel_event=cancel_event,
            )
            if picked is None:
                return None
            result[tab_key] = picked

        # Power-refresh DM nudge (#138) — Premium-only. When on, the
        # signup-button handler DMs the voter if their power column
        # value isn't readable. Cooldown is one nudge per event_date
        # so members aren't pinged repeatedly.
        #
        # Keep-or-change branch on re-entry. If the alliance had the
        # structured flow enabled previously, the saved Yes/No is
        # surfaced as "Keep current" so the wizard doesn't force the
        # officer to re-pick on every re-run. First-time setup
        # (structured flow not previously enabled) still shows the
        # standard Yes/No view so the question is asked explicitly.
        prior_enabled = bool(current_structured.get("structured_flow_enabled"))
        if prior_enabled:
            current_yn = bool(current_structured.get("power_refresh_dm_enabled"))
            gate = _KeepOrFlipYesNoGate(
                current_value=current_yn,
                keep_label_yes="✅ Keep current: Yes",
                keep_label_no="✅ Keep current: No",
                flip_label_yes="↩️ Switch to: Yes",
                flip_label_no="↩️ Switch to: No",
            )
            await channel.send(
                f"**Power-Refresh DM (💎 Premium)**\n"
                f"When a member clicks a sign-up button for **{label}** and "
                f"their power value (Column "
                f"**{result.get('power_metric_column', 'B')}** on the roster "
                f"Sheet) is blank or unparseable, the bot can DM them a "
                f"one-line nudge to update it. Currently "
                f"**{'on' if current_yn else 'off'}**. Keep it or flip.",
                view=gate,
            )
            await wait_view_or_cancel(gate, cancel_event)
            if getattr(gate, "cancelled", False):
                return None
            if gate.value is None:
                await channel.send(
                    GENERIC_CMD_TIMEOUT.format(cmd=cmd_name)
                )
                return None
            result["power_refresh_dm_enabled"] = bool(gate.value)
        else:
            nudge_view = YesNoView()
            await channel.send(
                f"**Power-Refresh DM (💎 Premium)**\n"
                f"When a member clicks a sign-up button for **{label}** and "
                f"their power value (Column "
                f"**{result.get('power_metric_column', 'B')}** on the roster "
                f"Sheet) is blank or unparseable, should the bot DM them a "
                f"one-line nudge to update it? At most one DM per member "
                f"per event date.",
                view=nudge_view,
            )
            await wait_view_or_cancel(nudge_view, cancel_event)
            if getattr(nudge_view, "cancelled", False):
                return None
            if nudge_view.selected is None:
                await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
                return None
            result["power_refresh_dm_enabled"] = bool(nudge_view.selected)

        # ── Stale-power follow-up (#255) ─────────────────────────────────
        #
        # Only relevant when the master power-refresh toggle is on. Asks
        # whether the bot should also DM when the voter's power value is
        # older than N days, then captures the days threshold and the
        # source for the "last updated" timestamp (tab + column + match
        # column). When the Power Data Source is already pointed at the
        # bot's Squad Powers tab, we look up the `Date Modified` column
        # automatically and skip the source picker entirely — that's the
        # common case for survey-using alliances.
        if result.get("power_refresh_dm_enabled"):
            saved_stale_days = int(result.get("power_refresh_stale_days") or 0)
            saved_stale_on  = saved_stale_days > 0

            prior_stale = prior_enabled and saved_stale_on
            if prior_stale:
                gate = _KeepOrFlipYesNoGate(
                    current_value=True,
                    keep_label_yes="✅ Keep current: Yes",
                    keep_label_no="✅ Keep current: No",
                    flip_label_yes="↩️ Switch to: Yes",
                    flip_label_no="↩️ Switch to: No",
                )
                await channel.send(
                    f"**Stale-Power DM (💎 Premium)**\n"
                    f"On top of the missing-power nudge, should the bot "
                    f"also DM when a member's power value is older than "
                    f"a configured number of days? Currently **on** at "
                    f"**{saved_stale_days}** days.",
                    view=gate,
                )
                await wait_view_or_cancel(gate, cancel_event)
                if getattr(gate, "cancelled", False):
                    return None
                if gate.value is None:
                    await channel.send(
                        GENERIC_CMD_TIMEOUT.format(cmd=cmd_name)
                    )
                    return None
                stale_on = bool(gate.value)
            else:
                stale_view = YesNoView()
                blurb = (
                    f"Currently **off**."
                    if prior_enabled and not saved_stale_on
                    else "Currently **off**."
                )
                await channel.send(
                    f"**Stale-Power DM (💎 Premium)**\n"
                    f"On top of the missing-power nudge for **{label}**, "
                    f"should the bot also DM when a member's power value "
                    f"is older than a configured number of days? "
                    f"{blurb} At most one DM per member per event date "
                    f"(shared with the missing-power nudge).",
                    view=stale_view,
                )
                await wait_view_or_cancel(stale_view, cancel_event)
                if getattr(stale_view, "cancelled", False):
                    return None
                if stale_view.selected is None:
                    await channel.send(
                        GENERIC_CMD_TIMEOUT.format(cmd=cmd_name)
                    )
                    return None
                stale_on = bool(stale_view.selected)

            if not stale_on:
                # Persist as off — wipe any saved days + source fields
                # so re-enabling later starts from defaults rather than
                # a half-configured row.
                result["power_refresh_stale_days"] = 0
                result["power_last_updated_tab"] = ""
                result["power_last_updated_column"] = ""
                result["power_last_updated_match_column"] = ""
            else:
                # Days threshold modal. Re-prompt up to 3 times on
                # garbage input rather than silently defaulting — a
                # stale-days picker that quietly went back to 7 when
                # the officer typed "two weeks" would be confusing.
                effective_days = saved_stale_days if saved_stale_days > 0 else 7

                class _StaleDaysModal(discord.ui.Modal):
                    def __init__(self):
                        super().__init__(title="Stale-Power Days")
                        self.confirmed = False
                        self.days_input = discord.ui.TextInput(
                            label="Days before a power value is 'stale'",
                            placeholder="e.g. 7",
                            default=str(effective_days),
                            required=True,
                            max_length=4,
                        )
                        self.add_item(self.days_input)

                    async def on_submit(self, inter: discord.Interaction):
                        self.confirmed = True
                        await inter.response.defer()
                        self.stop()

                class _StaleDaysPickerView(discord.ui.View):
                    def __init__(self):
                        super().__init__(timeout=300)
                        self.outcome: str | None = None
                        self.modal: _StaleDaysModal | None = None

                    @discord.ui.button(
                        label=f"✅ Keep current: {effective_days} days",
                        style=discord.ButtonStyle.success,
                    )
                    async def keep(
                        self, inter: discord.Interaction,
                        _btn: discord.ui.Button,
                    ):
                        self.outcome = "keep"
                        for item in self.children:
                            item.disabled = True
                        await wizard_registry.safe_edit_response(
                            inter,
                            content=f"✅ Threshold: **{effective_days}** days.",
                            view=self,
                        )
                        self.stop()

                    @discord.ui.button(
                        label="✏️ Set days",
                        style=discord.ButtonStyle.secondary,
                    )
                    async def define(
                        self, inter: discord.Interaction,
                        _btn: discord.ui.Button,
                    ):
                        self.modal = _StaleDaysModal()
                        await inter.response.send_modal(self.modal)
                        await self.modal.wait()
                        self.outcome = "edit" if self.modal.confirmed else None
                        for item in self.children:
                            item.disabled = True
                        try:
                            if inter.message:
                                await inter.message.edit(view=self)
                        except discord.HTTPException:
                            pass
                        self.stop()

                attempts = 0
                while True:
                    days_picker = _StaleDaysPickerView()
                    if not saved_stale_on:
                        # No prior value — drop the Keep button and
                        # promote Set days. (Mirrors the Power Data
                        # Source picker's first-time-vs-re-entry idiom.)
                        days_picker.remove_item(days_picker.keep)
                        days_picker.define.label = (
                            f"✏️ Set days (default: 7)"
                        )
                    if attempts == 0:
                        await channel.send(
                            f"**Stale-Power Threshold (💎 Premium)**\n"
                            f"How many days old must a member's power value "
                            f"be before the bot DMs them? Recommended: **7**. "
                            f"Range: 1–365.",
                            view=days_picker,
                        )
                    else:
                        await channel.send(
                            f"⚠️ Couldn't parse that — try a whole number "
                            f"between 1 and 365.",
                            view=days_picker,
                        )
                    await wait_view_or_cancel(days_picker, cancel_event)
                    if getattr(days_picker, "cancelled", False):
                        return None
                    if days_picker.outcome is None:
                        await channel.send(
                            GENERIC_CMD_TIMEOUT.format(cmd=cmd_name)
                        )
                        return None

                    if days_picker.outcome == "keep":
                        result["power_refresh_stale_days"] = effective_days
                        break

                    # edit: parse the modal value.
                    modal = days_picker.modal
                    assert modal is not None
                    raw_days = (modal.days_input.value or "").strip()
                    try:
                        parsed_days = int(raw_days)
                    except ValueError:
                        parsed_days = -1
                    if 1 <= parsed_days <= 365:
                        result["power_refresh_stale_days"] = parsed_days
                        break
                    attempts += 1
                    if attempts >= 3:
                        await channel.send(
                            f"⚠️ Couldn't parse a stale-days threshold "
                            f"after 3 tries. Run `/{cmd_name}` to start "
                            f"again."
                        )
                        return None

                # Last-Updated Source. Survey shortcut: if the alliance
                # already pointed Power Data Source at the bot's Squad
                # Powers tab, the survey writes a `Date Modified` column
                # we can locate by header — skip the picker entirely.
                from config import get_survey_config, get_spreadsheet
                survey_cfg = get_survey_config(guild_id) if guild_id else {}
                survey_tab = (
                    survey_cfg.get("tab_squad_powers") or "Squad Powers"
                )
                # `power_metric_tab` is stored empty when it matches
                # Member Roster (read path falls back to default).
                # Treat the wizard-side `effective_tab` value the
                # officer just saw as the resolved tab name.
                pmt = (result.get("power_metric_tab") or "").strip()
                resolved_power_tab = pmt or default_tab
                survey_shortcut_applied = False
                if resolved_power_tab == survey_tab:
                    # Try to locate the `Date Modified` column by
                    # header. Off the event loop in case the sheet is
                    # slow / rate-limited.
                    try:
                        sh = get_spreadsheet(guild_id)
                        ws = sh.worksheet(survey_tab) if sh else None
                        header_row = (
                            await asyncio.to_thread(ws.row_values, 1)
                            if ws else []
                        )
                    except Exception as e:
                        print(
                            f"[SETUP] survey Date-Modified header lookup "
                            f"failed for guild={guild_id} tab={survey_tab!r}: "
                            f"{e}"
                        )
                        header_row = []
                    date_col_idx = -1
                    for i, cell in enumerate(header_row):
                        if cell.strip().lower() == "date modified":
                            date_col_idx = i
                            break
                    if date_col_idx >= 0:
                        date_col_letter = _col_index_to_letter(date_col_idx)
                        result["power_last_updated_tab"] = survey_tab
                        result["power_last_updated_column"] = date_col_letter
                        # Empty match column reuses power_match_column
                        # at read time (which itself falls back to the
                        # power tab's match column).
                        result["power_last_updated_match_column"] = ""
                        survey_shortcut_applied = True
                        await channel.send(
                            f"✅ Auto-detected the survey's **Date Modified** "
                            f"column (`{date_col_letter}`) on tab "
                            f"`{survey_tab}` — using that as the "
                            f"last-updated source."
                        )

                if not survey_shortcut_applied:
                    # Full picker — mirrors the Power Data Source idiom.
                    saved_lu_tab = (
                        result.get("power_last_updated_tab") or ""
                    ).strip()
                    saved_lu_col = (
                        result.get("power_last_updated_column") or ""
                    ).strip().upper()
                    if not (
                        len(saved_lu_col) == 1
                        and "A" <= saved_lu_col <= "Z"
                    ):
                        saved_lu_col = ""
                    saved_lu_match = (
                        result.get("power_last_updated_match_column") or ""
                    ).strip().upper()
                    if not (
                        len(saved_lu_match) == 1
                        and "A" <= saved_lu_match <= "Z"
                    ):
                        saved_lu_match = ""

                    # Pre-fill defaults from the Power Data Source so
                    # alliances whose power + timestamp live on the same
                    # tab can one-click accept. The match column
                    # defaults to the power's match column (which
                    # itself defaults to Member Roster's discord_id_col).
                    pdef_tab = resolved_power_tab
                    pdef_match = (
                        result.get("power_match_column")
                        or default_match_letter
                    )

                    lu_effective_tab = saved_lu_tab or pdef_tab
                    lu_effective_col = saved_lu_col or ""
                    lu_effective_match = saved_lu_match or pdef_match
                    lu_has_custom = bool(saved_lu_tab) or bool(saved_lu_col)

                    class _LastUpdatedSourceModal(discord.ui.Modal):
                        def __init__(self):
                            super().__init__(title="Last-Updated Source")
                            self.confirmed = False
                            self.tab_input = discord.ui.TextInput(
                                label="Last-updated tab",
                                placeholder="e.g. Squad Powers, Member Roster",
                                default=lu_effective_tab,
                                required=True,
                                max_length=100,
                            )
                            self.col_input = discord.ui.TextInput(
                                label="Last-updated column letter (A-Z)",
                                placeholder="e.g. N",
                                default=lu_effective_col,
                                required=True,
                                max_length=2,
                            )
                            self.match_input = discord.ui.TextInput(
                                label="Name-match column letter (A-Z, optional)",
                                placeholder=(
                                    "Leave blank to reuse Power match column"
                                ),
                                default=lu_effective_match,
                                required=False,
                                max_length=2,
                            )
                            self.add_item(self.tab_input)
                            self.add_item(self.col_input)
                            self.add_item(self.match_input)

                        async def on_submit(
                            self, inter: discord.Interaction,
                        ):
                            self.confirmed = True
                            await inter.response.defer()
                            self.stop()

                    class _LastUpdatedPickerView(discord.ui.View):
                        def __init__(self):
                            super().__init__(timeout=300)
                            self.outcome: str | None = None
                            self.modal: _LastUpdatedSourceModal | None = None

                        @discord.ui.button(
                            label="✅ Keep current",
                            style=discord.ButtonStyle.success,
                        )
                        async def keep(
                            self, inter: discord.Interaction,
                            _btn: discord.ui.Button,
                        ):
                            self.outcome = "keep"
                            for item in self.children:
                                item.disabled = True
                            await wizard_registry.safe_edit_response(
                                inter,
                                content=(
                                    f"✅ Keeping current: tab "
                                    f"`{lu_effective_tab}`, column "
                                    f"`{lu_effective_col or '?'}`."
                                ),
                                view=self,
                            )
                            self.stop()

                        @discord.ui.button(
                            label="✏️ Define source",
                            style=discord.ButtonStyle.secondary,
                        )
                        async def define(
                            self, inter: discord.Interaction,
                            _btn: discord.ui.Button,
                        ):
                            self.modal = _LastUpdatedSourceModal()
                            await inter.response.send_modal(self.modal)
                            await self.modal.wait()
                            self.outcome = (
                                "edit" if self.modal.confirmed else None
                            )
                            for item in self.children:
                                item.disabled = True
                            try:
                                if inter.message:
                                    await inter.message.edit(view=self)
                            except discord.HTTPException:
                                pass
                            self.stop()

                    lu_picker = _LastUpdatedPickerView()
                    if not lu_has_custom:
                        # First-time setup — drop Keep, promote Define.
                        lu_picker.remove_item(lu_picker.keep)
                        lu_picker.define.label = (
                            f"✏️ Set source: {pdef_tab[:30]}…"
                            if len(pdef_tab) > 30 else
                            f"✏️ Set source: {pdef_tab}"
                        )
                    else:
                        lu_picker.keep.label = (
                            f"✅ Keep: {lu_effective_tab} · "
                            f"{lu_effective_col}"[:80]
                        )

                    await channel.send(
                        f"**Last-Updated Source (💎 Premium)**\n"
                        f"Where on the Sheet does the bot find each "
                        f"member's last-updated timestamp? We support "
                        f"the bot's own Squad Power Survey, a manually-"
                        f"maintained column, or an export from a "
                        f"different bot.\n\n"
                        f"• **Tab**: the Sheet tab with the timestamp.\n"
                        f"• **Last-updated column**: the column with the "
                        f"timestamp values (e.g. `N`).\n"
                        f"• **Name-match column**: blank reuses the Power "
                        f"Data Source's match column. Same row-matching "
                        f"rules: Discord ID first, name fallback.\n\n"
                        f"_Date formats are auto-detected. MM/DD/YYYY, "
                        f"DD/MM/YYYY, ISO 8601, and `May 5, 2026`-style "
                        f"long-month all work. Rows whose timestamp "
                        f"doesn't parse are silently skipped._",
                        view=lu_picker,
                    )
                    await wait_view_or_cancel(lu_picker, cancel_event)
                    if getattr(lu_picker, "cancelled", False):
                        return None
                    if lu_picker.outcome is None:
                        await channel.send(
                            GENERIC_CMD_TIMEOUT.format(cmd=cmd_name)
                        )
                        return None

                    if lu_picker.outcome == "keep":
                        pass  # saved values already in result
                    else:  # edit
                        modal = lu_picker.modal
                        assert modal is not None
                        tab_val = (modal.tab_input.value or "").strip()
                        col_val = (modal.col_input.value or "").strip().upper()
                        match_val = (
                            modal.match_input.value or ""
                        ).strip().upper()
                        if not (len(col_val) == 1 and "A" <= col_val <= "Z"):
                            col_val = ""
                        if not (
                            len(match_val) == 1
                            and "A" <= match_val <= "Z"
                        ):
                            match_val = ""  # blank → reuse power match
                        result["power_last_updated_tab"] = tab_val
                        result["power_last_updated_column"] = col_val
                        result["power_last_updated_match_column"] = match_val
                        if not (tab_val and col_val):
                            # Officer cleared the source mid-edit — disable
                            # the stale check instead of leaving a half-
                            # configured row that silently fails at read.
                            await channel.send(
                                "⚠️ Tab + column are both required for the "
                                "stale check. Disabling for now — re-run "
                                f"`/{cmd_name}` to set them later."
                            )
                            result["power_refresh_stale_days"] = 0

        # ── Roster DM templates (#226 follow-up) ─────────────────────────
        #
        # The Approve & Post flow's `📨 DM rostered members` button
        # fans out per-member DMs using these three templates. We walk
        # each role (Starter / Paired Sub / Pool Sub) through the same
        # Use default / Keep current / Edit picker the mail template
        # step uses, so officers learn one idiom across the wizard.
        # Empty saved value = fall back to the hardcoded default at
        # send time, so a guild that never customises still gets sane
        # copy.
        from defaults import (
            DEFAULT_ROSTER_DM_STARTER,
            DEFAULT_ROSTER_DM_PAIRED_SUB,
            DEFAULT_ROSTER_DM_POOL_SUB,
        )
        from config import get_roster_dm_templates

        saved_dm_templates = (
            get_roster_dm_templates(guild_id, event_type)
            if guild_id else
            {"starter": "", "paired_sub": "", "pool_sub": ""}
        )

        dm_placeholder_info = (
            "• `{name}`: member's display name\n"
            "• `{event_label}`: `Desert Storm` / `Canyon Storm`\n"
            "• `{team_blurb}`: ` Team A` / ` Team B` / `` (leading "
            "space included for you)\n"
            "• `{date}`: event date (e.g. `Thursday, May 28, 2026`)\n"
            "• `{time}`: team time slot (e.g. `4pm EDT (18:00 server "
            "time)`)\n"
            "• `{assignments}`: per-stage assignments block (Starter "
            "+ Paired Sub only)"
        )

        async def _get_dm_template(
            role_label: str, default_template: str,
            saved_template: str,
        ) -> str | None:
            """Walk one DM template through the Use default / Keep
            current / Edit picker. Returns the chosen template body,
            empty string for "use default" (so the DB stays clean
            when the bot ships an updated default later), or None
            on cancel / timeout."""
            saved_is_custom = (
                bool(saved_template)
                and saved_template != default_template
            )

            class _DmTemplateChoiceView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=300)
                    self.outcome: str | None = None

                @discord.ui.button(
                    label="✅ Keep current custom template",
                    style=discord.ButtonStyle.success,
                )
                async def keep(
                    self, inter: discord.Interaction,
                    _btn: discord.ui.Button,
                ):
                    self.outcome = "keep"
                    for item in self.children:
                        item.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=(
                            f"✅ Keeping your saved {role_label} DM "
                            f"template."
                        ),
                        view=self,
                    )
                    self.stop()

                @discord.ui.button(
                    label="↩️ Use default template",
                    style=discord.ButtonStyle.secondary,
                )
                async def use_def(
                    self, inter: discord.Interaction,
                    _btn: discord.ui.Button,
                ):
                    self.outcome = "default"
                    for item in self.children:
                        item.disabled = True
                    msg = (
                        f"✅ Reverted to default {role_label} DM "
                        f"template."
                        if saved_is_custom else
                        f"✅ Using default {role_label} DM template."
                    )
                    await wizard_registry.safe_edit_response(
                        inter, content=msg, view=self,
                    )
                    self.stop()

                @discord.ui.button(
                    label="✏️ Edit template",
                    style=discord.ButtonStyle.secondary,
                )
                async def edit(
                    self, inter: discord.Interaction,
                    _btn: discord.ui.Button,
                ):
                    self.outcome = "edit"
                    for item in self.children:
                        item.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter, view=self,
                    )
                    self.stop()

            choice_view = _DmTemplateChoiceView()
            if not saved_is_custom:
                # First-time / saved-equals-default: drop the Keep
                # current button and promote Use default to the
                # success-style primary action.
                choice_view.remove_item(choice_view.keep)
                choice_view.use_def.style = discord.ButtonStyle.success

            custom_block = (
                f"\n\nHere is your saved custom template:\n```\n"
                f"{saved_template}\n```"
                if saved_is_custom else ""
            )
            question = (
                "Would you like to keep your custom template, revert "
                "to the default, or edit it?"
                if saved_is_custom else
                "Would you like to use this default or write your own?"
            )
            await channel.send(
                f"**Roster DM Template: {role_label}**\n"
                f"Sent when leadership clicks 📨 DM rostered members "
                f"after Approve & Post for {label}.\n\n"
                f"Here is the default template:\n"
                f"```\n{default_template}\n```"
                f"{custom_block}\n\n"
                f"{question}",
                view=choice_view,
            )
            await wait_view_or_cancel(choice_view, cancel_event)
            if getattr(choice_view, "cancelled", False):
                return None
            if choice_view.outcome is None:
                await channel.send(
                    GENERIC_CMD_TIMEOUT.format(cmd=cmd_name)
                )
                return None
            if choice_view.outcome == "keep":
                return saved_template
            if choice_view.outcome == "default":
                # Persist empty string so the bot's future default
                # updates land for this alliance automatically.
                return ""

            # Edit branch — pasted as a chat message so multi-line
            # templates work without fighting a 200-char modal.
            reference_label = (
                "current custom" if saved_is_custom else "default"
            )
            await channel.send(
                f"Paste your custom {role_label} DM template. "
                f"You can copy the {reference_label} above and modify "
                f"it, or write your own.\n\n"
                f"**Available placeholders:**\n{dm_placeholder_info}\n\n"
                f"*This form will time out in 5 minutes. "
                f"You can run `/{cmd_name}` again if it times out.*"
            )
            try:
                reply = await bot.wait_for(
                    "message", check=check, timeout=300,
                )
                fallback = (
                    saved_template if saved_is_custom
                    else default_template
                )
                return reply.content.strip() or fallback
            except asyncio.TimeoutError:
                await channel.send(
                    GENERIC_CMD_TIMEOUT.format(cmd=cmd_name)
                )
                return None

        await channel.send(
            f"📨 **Roster DM Templates** _(3 templates, one per role)_"
            f"\nNext we'll set up the three DMs the bot sends after "
            f"Approve & Post when leadership clicks 📨 DM rostered "
            f"members. Each role (Starter, Paired Sub, Pool Sub) gets "
            f"its own message. You can use the defaults or customise "
            f"each one."
        )

        starter_template = await _get_dm_template(
            "Starter", DEFAULT_ROSTER_DM_STARTER,
            saved_dm_templates.get("starter", ""),
        )
        if starter_template is None:
            return None
        result["roster_dm_starter_template"] = starter_template

        paired_template = await _get_dm_template(
            "Paired Sub", DEFAULT_ROSTER_DM_PAIRED_SUB,
            saved_dm_templates.get("paired_sub", ""),
        )
        if paired_template is None:
            return None
        result["roster_dm_paired_sub_template"] = paired_template

        pool_template = await _get_dm_template(
            "Pool Sub", DEFAULT_ROSTER_DM_POOL_SUB,
            saved_dm_templates.get("pool_sub", ""),
        )
        if pool_template is None:
            return None
        result["roster_dm_pool_sub_template"] = pool_template

    # ── Always-ask: preset library + member rules tab names (free + Premium) ──
    #
    # Strategy presets + member rules only drive the structured roster
    # builder, so we only walk through them when the alliance has opted
    # *in* to the structured flow. Otherwise we'd post Premium-only copy
    # to a free-tier alliance that just declined the offer above.
    # Each block (#144):
    #   1. Posts an explainer so officers know what the concept IS.
    #   2. Asks for the tab name via `ask_keep_or_change`.
    #   3. If the alliance has zero rows in that table, offers an inline
    #      "create your first one now" branch so the new concept is
    #      immediately reachable.
    if not structured_opted_in:
        return result

    parent = "desertstorm" if event_type == "DS" else "canyonstorm"
    from config import default_structured_tab

    # ── Strategy Presets ────────────────────────────────────────────────
    # Bullet-list explainer per Kevin's first-sweep _edited.md spec:
    # leadership sees a structured "what's in a preset" breakdown before
    # being asked to name a Sheet tab for it (#144).
    await channel.send(
        "**Strategy Presets**\n"
        "A strategy preset is a saved zone layout including:\n"
        "Maximum players per zone\n"
        "Optional power requirements\n"
        "Priority\n"
        "\n"
        f"When leadership builds a roster, they pick which preset to "
        f"apply. The bot uses the preset to gate eligibility and fill "
        f"out the team.\n"
        f"\n"
        f"Manage presets via `{HUB_COMMAND[event_type]}` → "
        f"**{HUB_BTN_PRESETS}**."
    )
    picked = await ask_keep_or_change(
        channel,
        f"**Strategy Presets Tab**\n"
        f"Which Google Sheet tab should store {label} strategy presets? "
        f"The bot creates and maintains this tab.",
        default=default_structured_tab(event_type, "strategies_tab"),
        current=result.get("strategies_tab", ""),
        modal_title="Strategy Presets Tab Name",
        modal_label="Tab name",
        timeout_cmd=cmd_name,
        cancel_event=cancel_event,
    )
    if picked is None:
        return None
    result["strategies_tab"] = picked

    # Inline-create offer (only when the alliance has no presets yet).
    # An unconfigured Sheet (or transient gspread failure) here means the
    # offer is shown unconditionally — that's the same behaviour the
    # `/<parent> strategy list` command falls back to, and it's the
    # less-bad failure mode (offer one extra time vs. miss the discovery
    # surface entirely for a guild that genuinely has no presets).
    try:
        import storm_strategy as ss
        existing_presets = await asyncio.to_thread(
            ss.list_presets, guild_id, event_type,
        )
    except Exception:
        existing_presets = []
    if not existing_presets:
        preset_offer = _InlineCreatePresetOffer(
            owner_id=user.id, event_type=event_type, parent=parent,
        )
        preset_offer.message = await channel.send(
            f"Want to create your first {label} preset now? You can also "
            f"do this later via `{HUB_COMMAND[event_type]}` → "
            f"**{HUB_BTN_PRESETS}**.",
            view=preset_offer,
        )
        await wait_view_or_cancel(preset_offer, cancel_event)
        if getattr(preset_offer, "cancelled", False):
            return None
        # Either choice is fine — proceed regardless. A timeout also
        # proceeds; the on_timeout hook strips the buttons.

    # ── Member Rules ────────────────────────────────────────────────────
    # Per Rule A / #166 both DS and CS support `teams=both/A/B`, so the
    # per-member rule list (and the example) is the same for both event
    # types. Kevin's first-sweep _edited spec uses a structured Power-
    # band + Per-member breakdown with explicit "Example:" lines.
    await channel.send(
        "**Member Rules**\n"
        "Member rules tell the roster builder how to treat individual "
        "members.\n"
        "\n"
        "There are two types of Member rules.\n"
        "• Power-band:\n"
        "     Example: `members ≥ 80M are eligible for Power Tower`\n"
        "     Primary rule type that reads against the power column "
        "you configured earlier.\n"
        "• Per-member:\n"
        "     Used for special cases, example: `Alice always plays on Team A`,\n"
        "\n"
        f"Add rules later via `{HUB_COMMAND[event_type]}` → "
        f"**{HUB_BTN_RULES}**."
    )
    picked = await ask_keep_or_change(
        channel,
        f"**Member Rules Tab**\n"
        f"Which Google Sheet tab should store {label} member rules? "
        f"The bot creates and maintains this tab.",
        default=default_structured_tab(event_type, "member_rules_tab"),
        current=result.get("member_rules_tab", ""),
        modal_title="Member Rules Tab Name",
        modal_label="Tab name",
        timeout_cmd=cmd_name,
        cancel_event=cancel_event,
    )
    if picked is None:
        return None
    result["member_rules_tab"] = picked

    try:
        import storm_member_rules as smr
        existing_rules = await asyncio.to_thread(
            smr.list_rules, guild_id, event_type,
        )
    except Exception:
        existing_rules = []
    if not existing_rules:
        rule_offer = _InlineCreateMemberRuleOffer(
            owner_id=user.id, event_type=event_type, parent=parent,
        )
        # Per Rule A / #166 both DS and CS support per-member rules.
        # Pointer text is identical for both event types.
        rule_offer.message = await channel.send(
            f"Want to add your first {label} rule now? The button opens "
            f"a quick modal for a power-band rule (the most common type); "
            f"per-member rules need a Discord member picker, so add those "
            f"later via `{HUB_COMMAND[event_type]}` → **{HUB_BTN_RULES}**.",
            view=rule_offer,
        )
        await wait_view_or_cancel(rule_offer, cancel_event)
        if getattr(rule_offer, "cancelled", False):
            return None

    return result


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
    all_questions: list[dict] | None = None,
) -> dict | None:
    """Add or edit a single participation question. Mirrors the survey
    question builder's shape but with participation-specific types.

    `all_questions` (#244): the full configured-so-far list. Lets the
    derived_count type's source picker enumerate existing
    roster_multi_select questions to point at.
    """

    def check(m):
        return m.author == user and m.channel == channel

    # Label
    label_extra = f"\n*Existing label:* `{existing.get('label', '')}`" if existing else ""
    await channel.send(
        f"**Question: Label**\n"
        f"What's the label for this question? (e.g. `Sitting Out`, `Vote Count`)" + label_extra
    )
    try:
        reply = await bot.wait_for("message", check=check, timeout=180)
    except asyncio.TimeoutError:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
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
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"✅ Type: **{_PARTICIPATION_TYPE_LABELS.get(self.selected, self.selected)}**",
                    view=self,
                )
                self.stop()
            sel.callback = _cb
            self.add_item(sel)

    type_view = _TypeView()
    type_extra = f"\n*Existing type:* `{existing.get('type')}`" if existing else ""
    await channel.send(f"**Question: Answer Type**{type_extra}", view=type_view)
    await wait_view_or_cancel(type_view, cancel_event)
    if type_view.cancelled:
        return
    if type_view.selected is None:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
        return None
    q_type = type_view.selected

    q: dict = {"key": q_key, "label": q_label, "type": q_type}

    # Type-specific extras
    if q_type == "numeric":
        await channel.send(
            "**Optional bounds**\nReply with `min,max` (e.g. `0,500`) or "
            "type `none` for no bounds."
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=120)
        except asyncio.TimeoutError:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
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
                await channel.send("⚠️ Couldn't parse those bounds. Saving without min/max.")

    elif q_type in ("single_select", "multi_select"):
        await channel.send(
            "**Options** *(💎 Premium)*\nList the choices separated by commas.\n"
            "Example: `Win, Loss, Draw`"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=180)
        except asyncio.TimeoutError:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
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
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        fmt = reply.content.strip()
        q["date_format"] = "%m/%d/%Y" if fmt.lower() in ("", "default") else fmt

    elif q_type == "roster_multi_select":
        # #244 — paginated multi-select against the alliance roster.
        # Optional auto-prefill source (Premium only). Free tier always
        # captures manually.
        if is_premium_flag:
            class _PrefillView(discord.ui.View):
                def __init__(self):
                    super().__init__(timeout=120)
                    self.selected: str | None = None
                    self.cancelled = False

                @discord.ui.button(
                    label="🗳️ Pre-fill from Discord poll signups",
                    style=discord.ButtonStyle.primary,
                )
                async def from_poll(self, inter, _btn):
                    self.selected = "discord_poll"
                    for c in self.children:
                        c.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter,
                        content="✅ Pre-fill source: **Discord poll signups**",
                        view=self,
                    )
                    self.stop()

                @discord.ui.button(
                    label="✋ Manual selection only",
                    style=discord.ButtonStyle.secondary,
                )
                async def manual(self, inter, _btn):
                    self.selected = ""
                    for c in self.children:
                        c.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter,
                        content="✅ Pre-fill source: **Manual selection only**",
                        view=self,
                    )
                    self.stop()

            pv = _PrefillView()
            await channel.send(
                "**Auto-prefill source (💎 Premium, optional)**\n"
                "Want the bot to pre-check members based on a signal? "
                "Officers can still toggle any member; pre-fills are a "
                "starting point.\n\n"
                "• **Discord poll signups**: pre-check members who "
                "voted to attend in the signup poll for this event "
                "(useful for the 'Who didn't vote?' shape — invert "
                "the answer at participation time).\n"
                "• **Manual only**: no pre-fill; officer picks from "
                "the full roster.",
                view=pv,
            )
            await wait_view_or_cancel(pv, cancel_event)
            if pv.cancelled or pv.selected is None:
                await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
                return None
            if pv.selected:
                q["prefill_source"] = pv.selected
        # Free tier: prefill_source not set; officer always picks manually.

    elif q_type == "derived_count":
        # #244 — Premium-only. The bot reads past Per-Member Log rows
        # for the source question and counts per member. Officer can
        # override at participation time.

        # Source question picker — must be a roster_multi_select.
        source_candidates = [
            other for other in (all_questions or [])
            if other.get("type") == "roster_multi_select"
        ]
        if not source_candidates:
            await channel.send(
                "⚠️ **Derived count needs a source question** of type "
                "`Roster multi-select` to read from. Add one of those "
                "first, then come back and add this derived count.\n"
                "Skipping this question for now."
            )
            return None

        class _SourceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected: str | None = None
                self.cancelled = False
                options = [
                    discord.SelectOption(
                        label=(s.get("label") or s.get("key", "?"))[:100],
                        value=s.get("key", ""),
                    )
                    for s in source_candidates[:25]
                ]
                sel = discord.ui.Select(
                    placeholder="Pick the source roster multi-select question…",
                    options=options,
                )

                async def _cb(inter):
                    self.selected = sel.values[0]
                    sel.disabled = True
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=f"✅ Source question: `{self.selected}`",
                        view=self,
                    )
                    self.stop()
                sel.callback = _cb
                self.add_item(sel)

        sv = _SourceView()
        await channel.send(
            "**Source question** *(💎 Premium)*\n"
            "Which roster multi-select question's history should this "
            "count read from?",
            view=sv,
        )
        await wait_view_or_cancel(sv, cancel_event)
        if sv.cancelled or sv.selected is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        q["source_question_key"] = sv.selected

        # Lookback window — number of past events to scan.
        attempts = 3
        lookback = 4
        while attempts > 0:
            raw = await wait_for_msg_simple(
                channel, bot, user, cancel_event,
                "**Lookback window** *(💎 Premium)*\n"
                "How many past captured events should the count cover? "
                "(e.g. `4` for 'past 4 events'). Default is 4.",
            )
            if raw is None:
                return None
            raw = raw.strip()
            if not raw:
                break
            try:
                lookback = max(1, int(raw))
                break
            except ValueError:
                attempts -= 1
                await channel.send(
                    f"⚠️ `{raw}` isn't a number. Please re-enter."
                )
        q["lookback_events"] = lookback

        # Show-during-log toggle.
        class _ShowDuringLogView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected: bool | None = None
                self.cancelled = False

            @discord.ui.button(
                label="📊 Show counts per member during the log",
                style=discord.ButtonStyle.primary,
            )
            async def yes_btn(self, inter, _btn):
                self.selected = True
                for c in self.children:
                    c.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content="✅ Show during the log: **Yes**", view=self,
                )
                self.stop()

            @discord.ui.button(
                label="🔕 Only show in the Trends Viewer",
                style=discord.ButtonStyle.secondary,
            )
            async def no_btn(self, inter, _btn):
                self.selected = False
                for c in self.children:
                    c.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content="✅ Show during the log: **No**", view=self,
                )
                self.stop()

        sd = _ShowDuringLogView()
        await channel.send(
            "**Show during participation log?** *(💎 Premium)*\n"
            "When officers run the log, should this count display per "
            "member next to their name? Off by default — the count "
            "still lives in the Trends Viewer either way.",
            view=sd,
        )
        await wait_view_or_cancel(sd, cancel_event)
        if sd.cancelled or sd.selected is None:
            await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
            return None
        q["show_during_log"] = bool(sd.selected)

    return q


async def wait_for_msg_simple(
    channel, bot, user, cancel_event, prompt: str,
    *, timeout: int = 120,
) -> str | None:
    """Minimal message-wait helper for inline use inside
    `_build_participation_question`. Returns the user's reply text
    or None on cancel/timeout."""

    def check(m):
        return m.author == user and m.channel == channel

    await channel.send(prompt)
    try:
        reply = await bot.wait_for("message", check=check, timeout=timeout)
        return reply.content
    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out.")
        return None


async def run_event_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the four shared event settings:
    draft channel, announcement channel, draft time, and 5-minute warning.
    Individual events (add/edit/delete) live on the /events hub (#249)."""
    import wizard_registry
    guild_id = interaction.guild_id
    channel  = interaction.channel
    user     = interaction.user
    cancel_event = wizard_registry.register(user.id)

    from config import get_config, get_or_create_config, update_config_field

    guild_cfg = get_config(guild_id) or get_or_create_config(guild_id)
    timezone  = guild_cfg.timezone if guild_cfg.timezone else "America/New_York"

    draft_channel_id    = guild_cfg.event_draft_channel_id or 0
    announce_channel_id = guild_cfg.event_announce_channel_id or 0
    draft_time          = guild_cfg.event_draft_time or "12:00"
    five_min_warning    = guild_cfg.event_five_min_warning if guild_cfg.event_five_min_warning is not None else 1

    # Post-#249: this wizard owns only the four shared event settings
    # (channels, draft time, 5-minute warning). Event creation, editing,
    # and deletion moved to the /events hub, so officers managing
    # individual events go there instead of crawling through this wizard.

    await channel.send(
        "⚙️ **Event Setup**\n"
        "Configure your alliance event channels and draft cadence. "
        "All events share these four settings. To add, edit, or remove "
        f"individual events, run `/events` after this wizard completes."
    )

    # ── Steps 1-4: Channel/time settings ──────────────────────────────────────
    is_premium_flag  = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)
    current_draft_id = guild_cfg.event_draft_channel_id or 0
    draft_ch_view    = ChannelSelectStep(
        "Select the draft channel...",
        suggested_name="event-drafts",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=current_draft_id,
    )
    if draft_ch_view.is_current_stale:
        await channel.send(
            "⚠️ Your previously configured draft channel no longer exists. "
            "Pick a new one below."
        )
    await channel.send(
        "**Step 1 of 4 — Draft Channel**\n"
        "Which channel should the bot post event announcement drafts for leadership to review?\n"
        "*(This applies to all events)*",
        view=draft_ch_view,
    )
    await wait_view_or_cancel(draft_ch_view, cancel_event)
    if draft_ch_view.cancelled:
        return
    if not draft_ch_view.confirmed:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_EVENTS))
        return
    draft_channel_id = draft_ch_view.selected_channel.id

    current_ann_id = guild_cfg.event_announce_channel_id or 0
    ann_ch_view    = ChannelSelectStep(
        "Select the announcement channel...",
        suggested_name="announcements",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=current_ann_id,
    )
    if ann_ch_view.is_current_stale:
        await channel.send(
            "⚠️ Your previously configured announcement channel no longer exists. "
            "Pick a new one below."
        )
    await channel.send(
        "**Step 2 of 4 — Announcement Channel**\n"
        "Which channel should approved announcements be posted to?\n"
        "*(This applies to all events)*",
        view=ann_ch_view,
    )
    await wait_view_or_cancel(ann_ch_view, cancel_event)
    if ann_ch_view.cancelled:
        return
    if not ann_ch_view.confirmed:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_EVENTS))
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
            f"**Step 3 of 4 — Draft Posting Time**\n"
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
                f"Run `/setup` → {HUB_BTN_EVENTS} to start over."
            )
            return
        await channel.send(
            f"⚠️ Could not read **`{draft_time_raw}`** as a time. "
            f"Try `12:00pm`, `9:00am`, or `15:30`. Let's try once more."
        )

    warn_view = YesNoView()
    await channel.send(
        "**Step 4 of 4 — 5-Minute Warning**\n"
        "Should the bot automatically post a 5-minute warning before events?\n"
        "*(This applies to all events)*",
        view=warn_view,
    )
    await wait_view_or_cancel(warn_view, cancel_event)
    if warn_view.cancelled:
        return
    if warn_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_EVENTS))
        return
    five_min_warning = 1 if warn_view.selected else 0

    update_config_field(guild_id, "event_draft_channel_id",    draft_channel_id)
    update_config_field(guild_id, "event_announce_channel_id", announce_channel_id)
    update_config_field(guild_id, "event_draft_time",          draft_time)
    update_config_field(guild_id, "event_five_min_warning",    five_min_warning)

    # ── Summary ────────────────────────────────────────────────────────────────
    embed = discord.Embed(title="✅ Event Settings Saved", color=discord.Color.green())
    embed.add_field(name="Draft Channel",        value=f"<#{draft_channel_id}>",    inline=False)
    embed.add_field(name="Announcement Channel", value=f"<#{announce_channel_id}>", inline=False)
    embed.add_field(name="Draft Time",           value=_format_time_with_tz(draft_time, timezone), inline=False)
    embed.add_field(name="5-min Warning",        value="Yes" if five_min_warning else "No", inline=False)
    embed.set_footer(text="Run /events to add, edit, or remove individual events.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Event settings saved for guild {guild_id}")

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
                await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
            return None
        return reply.content.strip()[:max_chars]

    from config import (
        get_birthday_config, has_birthday_config, clear_birthday_config,
        get_config,
    )
    current = get_birthday_config(guild_id)
    birthdays_already_configured = has_birthday_config(guild_id)
    guild_cfg = get_config(guild_id)
    guild_tz  = guild_cfg.timezone if guild_cfg else "America/New_York"

    # ── If already enabled, show summary and offer edit or cancel ─────────────
    if birthdays_already_configured and current.get("enabled"):
        rc = current.get("reminder_channel_id", 0) or 0
        fields = [
            ("Sheet Tab",          current.get("tab_name") or "*not set*"),
            ("Name Column",        _col_index_to_letter(current.get("name_col", 0))),
            ("Birthday Column",    _col_index_to_letter(current.get("birthday_col", 0))),
            ("Train Integration",  "✅ Enabled" if current.get("train_integration") else "❌ Disabled"),
        ]
        if current.get("train_integration"):
            fields.append((
                "Placement",
                "Flexible (±1 day)" if current.get("flexible_placement") else "Birthday only",
            ))
            fields.append(("Lookahead", f"{current.get('lookahead_days', 14)} days"))
        fields.append((
            "Reminders",
            "✅ Enabled" if current.get("reminders_enabled") else "❌ Disabled",
        ))
        if current.get("reminders_enabled"):
            fields.append(("Reminder Channel", f"<#{rc}>" if rc else "*not set*"))
            fields.append(("Reminder Time",    _format_time_with_tz(current.get("reminder_time"), guild_tz) or "*not set*"))
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="🎂 Current Birthday Setup",
            description="Birthday tracking is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Birthday tracking is still active.",
        )
        if proceed is not True:
            return

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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
        return
    if not enabled_view.selected:
        from config import save_birthday_config
        # `last_train_population_date` is operational state owned by the
        # train-auto-pop scheduler (see #89) — not a `save_birthday_config`
        # parameter. Strip it from the splat so the disable path still
        # works when the column exists on the loaded row.
        save_birthday_config(
            guild_id, enabled=0,
            **{k: v for k, v in current.items()
               if k not in ("guild_id", "enabled", "last_train_population_date")}
        )
        await ask_disable_with_clear(
            channel,
            feature_label="Birthday tracking",
            setup_command=f"setup → {HUB_BTN_BIRTHDAYS}",
            had_prior_config=birthdays_already_configured,
            clear_fn=lambda: clear_birthday_config(guild_id),
            cancel_event=cancel_event,
        )
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
            _col_index_to_letter(saved_name_col)
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
    name_col = _col_letter_to_index(name_col_raw)
    if name_col < 0:
        await channel.send(f"⚠️ Please enter a single column letter like `A`. Run `/setup` → {HUB_BTN_BIRTHDAYS} to try again.")
        return

    # ── Step 4: Birthday column ────────────────────────────────────────────────
    saved_bday_col = current.get("birthday_col")
    bday_col_raw = await ask_keep_or_change(
        channel,
        "**Step 4 of 9 — Birthday Column**\n"
        "Which column contains the member's birthday?\n"
        "ℹ️ *The bot accepts most date formats: `12/7`, `12-7`, `Dec 7`, "
        "`December 7`, `1990-12-07`, etc. Bare numeric dates like `7/12` are "
        "read as **M/D** (July 12) — use `Dec 7` if your alliance writes "
        "day-first.*",
        default="B",
        current=(
            _col_index_to_letter(saved_bday_col)
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
    birthday_col = _col_letter_to_index(bday_col_raw)
    if birthday_col < 0:
        await channel.send(f"⚠️ Please enter a single column letter like `B`. Run `/setup` → {HUB_BTN_BIRTHDAYS} to try again.")
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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
        return
    train_integration = 1 if train_view.selected else 0

    flexible_placement = 0
    lookahead_days     = 14

    if not train_integration:
        await channel.send(
            "ℹ️ *Skipping Steps 6–7 (placement and lookahead) — train integration is off.*"
        )

    if train_integration:
        await channel.send(
            "ℹ️ Heads up: birthdays auto-populate the train schedule **once per day** "
            "(on the bot's first tick after server-time midnight). If you need a "
            "birthday reflected on the schedule sooner, run `/train birthdays` "
            "to trigger the check on demand."
        )

        # ── Step 6: Flexible placement ─────────────────────────────────────────
        class PlacementView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected = None

            @discord.ui.button(label="🎂 Birthday only", style=discord.ButtonStyle.primary)
            async def birthday_only(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 0
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(inter, content="✅ Placement: **Birthday only**", view=self)
                self.stop()

            @discord.ui.button(label="📅 Assign nearby if taken", style=discord.ButtonStyle.secondary)
            async def flexible(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 1
                for item in self.children: item.disabled = True
                await wizard_registry.safe_edit_response(inter, content="✅ Placement: **Assign 1 day before or after if birthday is taken**", view=self)
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
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
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
            await channel.send(f"⚠️ Please enter a number like `14`. Run `/setup` → {HUB_BTN_BIRTHDAYS} to try again.")
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
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
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
        is_premium_flag = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)
        saved_remind_ch = current.get("reminder_channel_id", 0) or 0
        remind_ch_view = ChannelSelectStep(
            "Select the birthday announcement channel...",
            suggested_name="birthdays",
            include_threads=is_premium_flag,
            guild=interaction.guild,
            current_id=saved_remind_ch,
        )
        if remind_ch_view.is_current_stale:
            await channel.send(
                "⚠️ Your previously configured birthday channel no longer exists. "
                "Pick a new one below."
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
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
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
                # DB stores 24h ("08:00") — render as "8:00am" so the
                # Keep-current and Use-default button labels don't sit
                # side-by-side in mismatched formats.
                current=_format_24h_to_12h(current.get("reminder_time", "")),
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
                    f"Run `/setup` → {HUB_BTN_BIRTHDAYS} to start over."
                )
                return
            await channel.send(
                f"⚠️ Could not read **`{time_raw}`** as a time. "
                f"Try `8:00am`, `12:00pm`, or `08:00`. Let's try once more."
            )

    # ── Step 9: Birthday DM body (💎 Premium) ─────────────────────────────────
    # Customisable body of the per-member birthday DM that fires alongside
    # the channel announcement on Premium guilds. Free guilds can configure
    # now — it just won't fire until they have Premium + Member Sync
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
            "Premium + Member Sync + a Discord ID column in your birthday sheet.\n\n"
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
    embed.add_field(name="Name Column",         value=f"Column {_col_index_to_letter(name_col)}",     inline=True)
    embed.add_field(name="Birthday Column",     value=f"Column {_col_index_to_letter(birthday_col)}", inline=True)
    embed.add_field(name="Discord ID Column",   value=f"Column {_col_index_to_letter(discord_id_col)}" if discord_id_col >= 0 else "Not stored", inline=True)
    embed.add_field(name="Train Integration",   value="Enabled" if train_integration else "Disabled", inline=True)
    if train_integration:
        embed.add_field(name="Placement",       value="Flexible (±1 day)" if flexible_placement else "Birthday only", inline=True)
        embed.add_field(name="Lookahead",       value=f"{lookahead_days} days",           inline=True)
    embed.add_field(name="Reminders",           value="Enabled" if reminders_enabled else "Disabled", inline=True)
    if reminders_enabled:
        embed.add_field(name="Reminder Channel", value=f"<#{reminder_channel_id}>",       inline=True)
        embed.add_field(name="Reminder Time",    value=_format_time_with_tz(reminder_time, guild_tz), inline=True)
    embed.set_footer(text=f"Run /setup and click {HUB_BTN_BIRTHDAYS} to update these settings.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Birthday config saved for guild {guild_id}")

async def run_shiny_tasks_setup(interaction: discord.Interaction, bot):
    """Walk leadership through configuring the daily shiny-tasks
    announcement. Six steps: enable → channel → server range → post
    time → message template → confirm. Free for all tiers."""
    import wizard_registry
    from config import (
        get_config, get_shiny_tasks_config, save_shiny_tasks_config,
        has_shiny_tasks_config, clear_shiny_tasks_config,
    )
    from defaults import DEFAULT_SHINY_TASKS_MESSAGE

    guild_id     = interaction.guild_id
    channel      = interaction.channel
    user         = interaction.user
    cancel_event = wizard_registry.register(user.id)

    current  = get_shiny_tasks_config(guild_id)
    cfg      = get_config(guild_id)
    guild_tz = cfg.timezone if cfg else "America/New_York"
    tz_label = TIMEZONE_LABELS.get(guild_tz, "ET")
    shiny_already_configured = has_shiny_tasks_config(guild_id)

    # ── If already enabled, show summary and offer edit or cancel ─────────────
    if shiny_already_configured and current.get("enabled"):
        ch_id = current.get("channel_id", 0) or 0
        fields = [
            ("Channel",       f"<#{ch_id}>" if ch_id else "*not set*"),
            (
                "Server Range",
                f"{current.get('server_min') or '?'} – {current.get('server_max') or '?'}",
            ),
            (
                "Post Time",
                _format_time_with_tz(current.get("post_time"), guild_tz) or "*not set*",
            ),
            (
                "Message",
                "Custom" if (current.get("message_template") or "").strip() else "Default",
            ),
        ]
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="🌟 Current Shiny Tasks Setup",
            description="The daily shiny-tasks announcement is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. The daily announcement is still active.",
        )
        if proceed is not True:
            wizard_registry.unregister(user.id, cancel_event)
            return

    await channel.send(
        "🌟 **Daily Shiny Tasks Setup**\n"
        "Each day, the bot can post the list of Last War servers where "
        "shiny tasks are available, filtered to the servers your alliance "
        "can reach."
    )

    # ── Step 1: Enable? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        "**Step 1 of 6 — Enable daily shiny tasks announcement?**",
        view=enabled_view,
    )
    await wait_view_or_cancel(enabled_view, cancel_event)
    if enabled_view.cancelled:
        wizard_registry.unregister(user.id, cancel_event)
        return
    if enabled_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SHINY))
        wizard_registry.unregister(user.id, cancel_event)
        return
    if not enabled_view.selected:
        # Disable + persist the previously-saved range/channel/etc. so the
        # next Shiny Tasks setup wizard run can offer them back as "current".
        save_shiny_tasks_config(
            guild_id,
            enabled=0,
            channel_id=current.get("channel_id", 0),
            post_time=current.get("post_time", "09:00"),
            server_min=current.get("server_min", 0),
            server_max=current.get("server_max", 0),
            message_template=current.get("message_template", ""),
        )
        await ask_disable_with_clear(
            channel,
            feature_label="Shiny tasks announcement",
            setup_command="setup → 🌟 Shiny Tasks",
            had_prior_config=shiny_already_configured,
            clear_fn=lambda: clear_shiny_tasks_config(guild_id),
            cancel_event=cancel_event,
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    # ── Step 2: Channel ───────────────────────────────────────────────────────
    is_premium_flag = await premium.is_premium(guild_id, interaction=interaction, bot=interaction.client)
    await channel.send(
        "**Step 2 of 6 — Announcement Channel**\n"
        "Pick the channel where the daily shiny tasks post should be posted."
    )
    saved_channel_id = current.get("channel_id", 0) or 0
    ch_view = ChannelSelectStep(
        "Select the shiny tasks channel...",
        suggested_name="shiny-tasks",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=saved_channel_id,
    )
    if ch_view.is_current_stale:
        await channel.send(
            "⚠️ Your previously configured shiny tasks channel no longer exists. "
            "Pick a new one below."
        )
    await channel.send("​", view=ch_view)
    await wait_view_or_cancel(ch_view, cancel_event)
    if ch_view.cancelled:
        wizard_registry.unregister(user.id, cancel_event)
        return
    if not ch_view.confirmed:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SHINY))
        wizard_registry.unregister(user.id, cancel_event)
        return
    channel_id = ch_view.selected_channel.id

    # ── Step 3: Server range (min + max in one modal) ─────────────────────────
    class ServerRangeModal(discord.ui.Modal):
        def __init__(self, min_default: str = "", max_default: str = ""):
            super().__init__(title="Server Range")
            self.min_value = None
            self.max_value = None
            self._min = discord.ui.TextInput(
                label="Lowest reachable server number",
                placeholder="e.g. 677",
                default=min_default,
                required=True, max_length=5,
            )
            self._max = discord.ui.TextInput(
                label="Highest reachable server number",
                placeholder="e.g. 804",
                default=max_default,
                required=True, max_length=5,
            )
            self.add_item(self._min)
            self.add_item(self._max)

        @property
        def value(self) -> str:
            """Display string consumed by `ModalLaunchView` after submit.
            That view formats `✅ Entered: **{self.modal.value}**`, so
            this property has to exist on every modal it wraps — without
            it, the post-submit edit raises `AttributeError` and the
            wizard step appears to hang."""
            if self.min_value is None and self.max_value is None:
                return ""
            return f"{self.min_value or '?'} – {self.max_value or '?'}"

        async def on_submit(self, inter: discord.Interaction):
            self.min_value = self._min.value.strip()
            self.max_value = self._max.value.strip()
            await inter.response.defer()
            self.stop()

    saved_min = current.get("server_min") or 0
    saved_max = current.get("server_max") or 0
    range_prompt = (
        "**Step 3 of 6 — Server Range**\n"
        "Enter the lowest and highest server numbers your alliance can "
        "reach. Typically your transfer range."
    )
    range_attempts_left = 3
    server_min = server_max = None
    while True:
        range_modal = ServerRangeModal(
            min_default=str(saved_min) if saved_min else "",
            max_default=str(saved_max) if saved_max else "",
        )
        # ServerRangeModal's `value` is a read-only @property derived
        # from min_value + max_value, so ModalLaunchView's default
        # `modal.value = current_value` path won't work. Use the
        # on_keep_current callback to populate the underlying
        # attributes directly when leadership clicks Keep current.
        try:
            has_saved_range = int(saved_min) >= 1 and int(saved_max) >= int(saved_min)
        except (TypeError, ValueError):
            has_saved_range = False
        if has_saved_range:
            keep_min, keep_max = int(saved_min), int(saved_max)

            def _keep_range(modal, _min=keep_min, _max=keep_max):
                modal.min_value = str(_min)
                modal.max_value = str(_max)

            range_launcher = ModalLaunchView(
                range_modal,
                current_value=f"{keep_min} – {keep_max}",
                current_display=f"{keep_min} – {keep_max}",
                on_keep_current=_keep_range,
            )
        else:
            range_launcher = ModalLaunchView(range_modal)
        # Override the generic "Enter Value" button label so leadership
        # sees domain wording. Find by label rather than index because
        # the Keep-current button (when present) sits at children[0].
        for _child in range_launcher.children:
            if isinstance(_child, discord.ui.Button) and _child.label == "✏️ Enter Value":
                _child.label = "✏️ Enter Server Numbers"
                break
        await channel.send(range_prompt, view=range_launcher)
        await wait_view_or_cancel(range_launcher, cancel_event)
        if range_launcher.cancelled:
            wizard_registry.unregister(user.id, cancel_event)
            return
        if not range_launcher.confirmed:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SHINY))
            wizard_registry.unregister(user.id, cancel_event)
            return

        min_raw = (range_modal.min_value or "").strip()
        max_raw = (range_modal.max_value or "").strip()
        try:
            candidate_min = int(min_raw)
            candidate_max = int(max_raw)
            valid_numbers = True
        except (TypeError, ValueError):
            valid_numbers = False

        if valid_numbers and candidate_min >= 1 and candidate_min <= candidate_max:
            server_min = candidate_min
            server_max = candidate_max
            break

        range_attempts_left -= 1
        if range_attempts_left <= 0:
            await channel.send(
                "⚠️ Could not read those server numbers after a few tries. "
                "Run `/setup` → 🌟 Shiny Tasks to start over."
            )
            wizard_registry.unregister(user.id, cancel_event)
            return

        # Pre-fill the retry modal with whatever the user typed, so the
        # correction is one tap away instead of re-entering both fields.
        saved_min = min_raw
        saved_max = max_raw
        if not valid_numbers:
            await channel.send(
                f"⚠️ Could not read **`{min_raw}`** / **`{max_raw}`** as whole "
                f"numbers. Try something like `677` and `804`. Let's try once more."
            )
        else:
            await channel.send(
                f"⚠️ The lowest server (**`{min_raw}`**) must be ≥ 1 and "
                f"≤ the highest (**`{max_raw}`**). Let's try once more."
            )

    # ── Step 4: Post time ─────────────────────────────────────────────────────
    attempts_left = 3
    post_time = "09:00"
    while True:
        time_raw = await ask_keep_or_change(
            channel,
            f"**Step 4 of 6 — Post Time**\n"
            f"What time of day should the announcement post? "
            f"*(in your timezone: {tz_label})*\n"
            f"*(e.g. `9:00am`, `10:30am`, `9:00pm`)*",
            default="9:00am",
            # DB stores '09:00' (24h) — render as '9:00am' before showing
            # so 'Keep current' and 'Use default' don't sit side-by-side
            # in mismatched formats.
            current=_format_24h_to_12h(current.get("post_time", "")),
            modal_title="Post Time",
            modal_label="Time",
            timeout_cmd="setup_shiny_tasks",
            cancel_event=cancel_event,
        )
        if time_raw is None:
            wizard_registry.unregister(user.id, cancel_event)
            return
        parsed = _parse_12h_time(time_raw)
        if parsed:
            post_time = parsed
            break
        if (len(time_raw) == 5 and time_raw[2] == ":"
                and time_raw.replace(":", "").isdigit()):
            post_time = time_raw  # already 24h
            break
        attempts_left -= 1
        if attempts_left <= 0:
            await channel.send(
                "⚠️ Could not read that time after a few tries. "
                "Run `/setup` → 🌟 Shiny Tasks to start over."
            )
            wizard_registry.unregister(user.id, cancel_event)
            return
        await channel.send(
            f"⚠️ Could not read **`{time_raw}`** as a time. "
            f"Try `9:00am`, `10:30am`, or `09:00`. Let's try once more."
        )

    # ── Step 5: Message template ──────────────────────────────────────────────
    saved_template = (current.get("message_template") or "").strip()
    template_input = await ask_keep_or_change(
        channel,
        "**Step 5 of 6 — Announcement Message**\n"
        "Customize the announcement body, or use the default. "
        "Placeholders: `{servers}` and `{date}`.",
        default=DEFAULT_SHINY_TASKS_MESSAGE,
        current=saved_template or None,
        modal_title="Shiny Tasks Message",
        modal_label="Message body",
        timeout_cmd="setup_shiny_tasks",
        cancel_event=cancel_event,
    )
    if template_input is None:
        wizard_registry.unregister(user.id, cancel_event)
        return
    # Store empty string when the user picks "use default" so a future
    # change to DEFAULT_SHINY_TASKS_MESSAGE automatically propagates,
    # instead of freezing today's wording in the DB.
    message_template = (
        "" if template_input.strip() == DEFAULT_SHINY_TASKS_MESSAGE.strip()
        else template_input.strip()
    )

    # ── Step 6: Confirm + save ────────────────────────────────────────────────
    embed = discord.Embed(
        title="🌟 Shiny Tasks — Final Review",
        description="Confirm to save this configuration.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Status",         value="✅ Enabled",                       inline=True)
    embed.add_field(name="Channel",        value=f"<#{channel_id}>",                inline=True)
    embed.add_field(name="Server Range",   value=f"{server_min} – {server_max}",    inline=True)
    embed.add_field(name="Post Time",      value=_format_time_with_tz(post_time, guild_tz), inline=True)
    embed.add_field(
        name="Message",
        value=(message_template or DEFAULT_SHINY_TASKS_MESSAGE)[:1024],
        inline=False,
    )
    confirm_view = ConfirmView()
    await channel.send(embed=embed, view=confirm_view)
    await wait_view_or_cancel(confirm_view, cancel_event)
    if confirm_view.cancelled:
        wizard_registry.unregister(user.id, cancel_event)
        return
    if not confirm_view.confirmed:
        await channel.send("❌ Setup cancelled. Run `/setup` → 🌟 Shiny Tasks to start again.")
        wizard_registry.unregister(user.id, cancel_event)
        return

    save_shiny_tasks_config(
        guild_id,
        enabled=1,
        channel_id=channel_id,
        post_time=post_time,
        server_min=server_min,
        server_max=server_max,
        message_template=message_template,
    )

    _human = _format_time_with_tz(post_time, guild_tz) or post_time

    await channel.send(
        f"✅ Shiny-tasks announcement saved! The first post will fire at {_human}."
    )
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Shiny tasks config saved for guild {guild_id}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

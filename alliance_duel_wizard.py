"""Alliance Duel (VS) setup wizard (#399 / #448).

The interactive half of VS setup: ask which alliance is yours, ask which
shape they want to track, create the tab, and show the column guide.

`alliance_duel_setup.py` owns the embeds this renders, so everything here is
flow control. Two steps only, in the order the user thinks rather than the
order the schema stores: *who are you* then *what do you want to track*.

The mode question is step two on purpose. It is the one thing that cannot be
inferred (skeleton generation is a write, so the shape has to be known before
there is data to infer it from), and asking it after the alliance identity
means the upsell lands on someone who has already committed to setting this
up rather than on a cold open.
"""

from __future__ import annotations

import logging

import discord

import alliance_duel as ad
import alliance_duel_setup as ads
import config
from messages import CANCEL_BACKPEDAL_DEFAULT
from setup_hub import HUB_BTN_VS
from wizard_registry import expire_view_message, safe_edit_response

logger = logging.getLogger(__name__)

#: A wizard step involving typing or thought, per the DESIGN.md timeout tiers.
STEP_TIMEOUT = 300


class OwnAllianceModal(discord.ui.Modal, title="Your alliance"):
    """Step 1: which of the sixteen rows is yours.

    Stored once in config rather than as a repeated sheet column. Your
    alliance is otherwise just another row; this is what tells the bot which.
    """

    tag = discord.ui.TextInput(
        label="Alliance tag",
        placeholder="ABC",
        max_length=10,
        required=True,
    )
    warzone = discord.ui.TextInput(
        label="Warzone",
        placeholder="1234",
        max_length=10,
        required=True,
    )

    def __init__(self, parent: "VSSetupView") -> None:
        super().__init__()
        self._parent = parent
        current = parent.cfg
        if current.get("own_tag"):
            self.tag.default = current["own_tag"]
        if current.get("own_warzone"):
            self.warzone.default = current["own_warzone"]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Defer before any sheet or DB round-trip, per the 1.1.7 rule: a slow
        # call otherwise expires the 3-second initial-response token.
        await interaction.response.defer(ephemeral=True)

        key = ad.AllianceKey.of(str(self.tag), str(self.warzone))
        if key is None:
            await interaction.followup.send(
                "⚠️ I need both an alliance tag and a warzone. "
                f"Run `/setup` and click **{HUB_BTN_VS}** to start again.",
                ephemeral=True,
            )
            return

        config.save_vs_config(
            self._parent.guild_id,
            own_tag=str(self.tag).strip(),
            own_warzone=str(self.warzone).strip(),
        )
        self._parent.cfg = config.get_vs_config(self._parent.guild_id)
        await self._parent.show_mode_step(interaction)


class TrackingModeView(discord.ui.View):
    """Step 2: own alliance or the full bracket.

    Neither button is `primary`. The bracket is the option that unlocks more,
    but presenting it as recommended would be the bot leaning on a choice the
    design says belongs to the alliance, and own-alliance is a supported shape
    rather than a lesser one.
    """

    def __init__(self, parent: "VSSetupView") -> None:
        super().__init__(timeout=STEP_TIMEOUT)
        self._parent = parent
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        await expire_view_message(self.message, command_hint=ads.VS_SETUP_NAV)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._parent.owns(interaction)

    @discord.ui.button(label=ads.MODE_BTN_OWN, style=discord.ButtonStyle.secondary)
    async def btn_own(self, inter: discord.Interaction, _b: discord.ui.Button):
        await self._parent.finish(inter, ad.MODE_OWN_ALLIANCE)

    @discord.ui.button(label=ads.MODE_BTN_FULL, style=discord.ButtonStyle.secondary)
    async def btn_full(self, inter: discord.Interaction, _b: discord.ui.Button):
        await self._parent.finish(inter, ad.MODE_FULL_BRACKET)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def btn_cancel(self, inter: discord.Interaction, _b: discord.ui.Button):
        # Backpedal, not plain: the alliance identity saved in step 1 survives,
        # and telling them "Cancelled." flat would imply it did not.
        self.stop()
        await safe_edit_response(
            inter,
            content=(
                f"{CANCEL_BACKPEDAL_DEFAULT} Your alliance is still saved. "
                f"Run `/setup` and click **{HUB_BTN_VS}** to pick a tracking mode."
            ),
            embed=None,
            view=None,
        )


#: The two scheduled surfaces, as buttons on the setup panel.
#:
#: Emoji picked against the DESIGN.md catalog, and the pair is deliberately
#: **not** symmetrical. 🔔 is "an automated notification from a module the
#: alliance set up, arriving on its own", which is exactly the leadership score
#: prompt. The catalog then rules itself out for the other one: "an auto-post
#: to the whole alliance is not 🔕 when switched off. A scheduled summary
#: landing in a channel for everyone to read is a post, not a notification."
#: So the member reminder takes 📣, the announcing glyph, and its on/off
#: buttons go bare rather than borrowing a bell that would mean the wrong
#: thing.
VS_BTN_SCORE_PROMPT = "🔔 Daily score prompt"
VS_BTN_DAY_THEME = "📣 Day theme reminder"

VS_BTN_PROMPT_TIME = "🕒 Set the posting time"
VS_BTN_PROMPT_TIME_CHANGE = "🕒 Change the posting time"
VS_BTN_PROMPT_CHANNEL = "📢 Set the channel"
VS_BTN_PROMPT_CHANNEL_CHANGE = "📢 Change the channel"
VS_BTN_PROMPT_NOTE = "✏️ Add a note from leadership"
VS_BTN_PROMPT_NOTE_CHANGE = "✏️ Change the note from leadership"

#: On/off. The bell pair carries the score prompt, where a notification really
#: is being switched off; the member reminder gets bare labels, per the catalog
#: rule quoted above.
VS_BTN_PROMPT_ON = "🔔 Turn it on"
VS_BTN_PROMPT_OFF = "🔕 Turn it off"
#: Bare labels with a state glyph, not a bell: these are posts to a channel
#: rather than notifications, and three switches in one grid need their state
#: readable at a glance more than they need three different icons.
VS_BTN_EVENT_POSTS = "📣 Event posts"
EVENT_TOGGLES = (
    ("clinch_status_enabled", "Mid-week clinch status"),
    ("opponent_reveal_enabled", "Next opponent"),
    ("season_recap_enabled", "Season recap"),
)

VS_BTN_POST_ON = "Turn it on"
VS_BTN_POST_OFF = "Turn it off"


class ScheduledSurface:
    """One scheduled VS post, as the settings panel needs to know it.

    Two surfaces (#405, #406) with the same three settings and genuinely
    different copy, audiences and gating. Parameterising one panel keeps the
    interaction rules (disabled until it could fire, off preserves the values,
    one primary) in a single place, where two classes would drift the moment
    one of them gained a fourth control.
    """

    def __init__(
        self,
        key: str,
        *,
        button: str,
        channel_question: str,
        channel_label: str,
        suggested_channel: str,
        time_modal_title: str,
        on_label: str,
        off_label: str,
        has_note: bool = False,
        has_time: bool = True,
        toggles: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.key = key
        self.button = button
        self.channel_question = channel_question
        self.channel_label = channel_label
        self.suggested_channel = suggested_channel
        self.time_modal_title = time_modal_title
        self.on_label = on_label
        self.off_label = off_label
        self.has_note = has_note
        #: False for the event-driven posts (#409), which fire when data lands
        #: rather than on a clock. There is no time to ask for, so asking would
        #: be a control that cannot change anything.
        self.has_time = has_time
        #: `(config column, label)` for a surface that is several independent
        #: opt-ins sharing one channel, instead of a single on/off.
        self.toggles = toggles

    @property
    def enabled_col(self) -> str:
        return f"{self.key}_enabled"

    @property
    def time_col(self) -> str:
        return f"{self.key}_time"

    @property
    def channel_col(self) -> str:
        return f"{self.key}_channel_id"

    @property
    def note_col(self) -> str:
        return f"{self.key}_note"


SCORE_PROMPT_SURFACE = ScheduledSurface(
    "score_prompt",
    button=VS_BTN_SCORE_PROMPT,
    channel_question="Where should the daily score prompt be posted?",
    channel_label="score prompt",
    suggested_channel="leadership",
    time_modal_title="Daily score prompt time",
    on_label=VS_BTN_PROMPT_ON,
    off_label=VS_BTN_PROMPT_OFF,
)

EVENT_POSTS_SURFACE = ScheduledSurface(
    "event_posts",
    button=VS_BTN_EVENT_POSTS,
    channel_question="Which channel should these land in?",
    channel_label="event posts",
    suggested_channel="leadership",
    time_modal_title="",
    on_label="Turn it on",
    off_label="Turn it off",
    has_time=False,
    toggles=EVENT_TOGGLES,
)

DAY_THEME_SURFACE = ScheduledSurface(
    "day_theme",
    button=VS_BTN_DAY_THEME,
    channel_question="Which channel should the day theme reminder be posted in?",
    channel_label="day theme reminder",
    suggested_channel="general",
    time_modal_title="Day theme reminder time",
    on_label=VS_BTN_POST_ON,
    off_label=VS_BTN_POST_OFF,
    has_note=True,
)


class ScheduledPostSettingsView(discord.ui.View):
    """Time, channel, an optional standing note, and on/off.

    A settings panel rather than a stepped wizard, which is the shape these
    questions actually have: independent values an officer comes back to change
    one at a time, months after setup, quite possibly a different officer than
    the one who set them. A sequence would re-ask all of them to change one, and
    "Keep current" three times over is a worse version of a panel that simply
    shows what is saved.

    Turning a post off preserves its channel, time and note, matching every
    other scheduled surface in the bot: switching a post off is not the same as
    forgetting where it went.
    """

    def __init__(self, guild_id: int, owner_user_id: int, surface: ScheduledSurface) -> None:
        super().__init__(timeout=STEP_TIMEOUT)
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.surface = surface
        self.cfg = config.get_vs_config(guild_id)
        self.message: discord.Message | None = None
        self._render()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render(self) -> None:
        self.clear_items()
        surface = self.surface
        has_time = bool(self.cfg.get(surface.time_col)) if surface.has_time else True
        has_channel = bool(self.cfg.get(surface.channel_col))
        is_on = bool(self.cfg.get(surface.enabled_col))

        if surface.has_time:
            time_btn = discord.ui.Button(
                label=VS_BTN_PROMPT_TIME_CHANGE if has_time else VS_BTN_PROMPT_TIME,
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            time_btn.callback = self._set_time
            self.add_item(time_btn)

        channel_btn = discord.ui.Button(
            label=VS_BTN_PROMPT_CHANNEL_CHANGE if has_channel else VS_BTN_PROMPT_CHANNEL,
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        channel_btn.callback = self._set_channel
        self.add_item(channel_btn)

        if surface.has_note:
            has_note = bool(self.cfg.get(surface.note_col))
            note_btn = discord.ui.Button(
                label=VS_BTN_PROMPT_NOTE_CHANGE if has_note else VS_BTN_PROMPT_NOTE,
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            note_btn.callback = self._set_note
            self.add_item(note_btn)

        # Disabled rather than hidden until the surface could actually fire,
        # with the reason in the embed: switching on a post with no channel (or
        # no time, where one is asked for) saves a setting that never runs.
        ready = has_time and has_channel

        if surface.toggles:
            for column, label in surface.toggles:
                on = bool(self.cfg.get(column))
                button = discord.ui.Button(
                    label=f"{'✅' if on else '▫️'} {label}"[:80],
                    style=discord.ButtonStyle.secondary,
                    disabled=not on and not ready,
                    row=1,
                )
                button.callback = self._make_toggle(column)
                self.add_item(button)
            return

        toggle = discord.ui.Button(
            label=surface.off_label if is_on else surface.on_label,
            style=discord.ButtonStyle.secondary if is_on else discord.ButtonStyle.primary,
            disabled=not is_on and not ready,
            row=1,
        )
        toggle.callback = self._toggle
        self.add_item(toggle)

    def embed(self) -> discord.Embed:
        return ads.scheduled_post_embed(self.cfg, self.surface.key)

    async def _redraw(self, interaction: discord.Interaction) -> None:
        """Re-read config and redraw, so the panel always shows what is saved."""
        self.cfg = config.get_vs_config(self.guild_id)
        self._render()
        await safe_edit_response(interaction, embed=self.embed(), view=self)

    # ── Guards ────────────────────────────────────────────────────────────────

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        from messages import DENY_NOT_OWNER

        await interaction.response.send_message(DENY_NOT_OWNER, ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        await expire_view_message(self.message, command_hint=ads.VS_SETUP_NAV)

    # ── Actions ───────────────────────────────────────────────────────────────

    async def _set_time(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(PostTimeModal(self))

    async def _set_note(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LeadershipNoteModal(self))

    async def _set_channel(self, interaction: discord.Interaction) -> None:
        from setup_cog import ChannelSelectStep

        surface = self.surface
        picker = ChannelSelectStep(
            f"Select the channel for the {surface.channel_label}...",
            suggested_name=surface.suggested_channel,
            allow_create=False,
            guild=interaction.guild,
            current_id=self.cfg.get(surface.channel_col) or 0,
        )
        content = surface.channel_question
        if picker.is_current_stale:
            from messages import PREV_CHANNEL_GONE

            gone = PREV_CHANNEL_GONE.format(channel_label=surface.channel_label)
            content = gone + "\n\n" + content
        await interaction.response.send_message(content=content, view=picker, ephemeral=True)

        await picker.wait()
        if not picker.confirmed or picker.selected_channel is None:
            return  # timed out or cancelled; the panel above is still live

        config.save_vs_config(self.guild_id, **{surface.channel_col: picker.selected_channel.id})
        await self._redraw_message()

    async def _toggle(self, interaction: discord.Interaction) -> None:
        turning_on = not self.cfg.get(self.surface.enabled_col)
        config.save_vs_config(self.guild_id, **{self.surface.enabled_col: 1 if turning_on else 0})
        await self._redraw(interaction)

    def _make_toggle(self, column: str):
        """One switch among several sharing a channel (#409). Each is its own
        opt-in: an alliance that wants the mid-week clinch status and nothing
        else gets exactly that."""

        async def _callback(interaction: discord.Interaction) -> None:
            turning_on = not self.cfg.get(column)
            config.save_vs_config(self.guild_id, **{column: 1 if turning_on else 0})
            await self._redraw(interaction)

        return _callback

    async def _redraw_message(self) -> None:
        """Redraw without an interaction to answer, for the channel picker: its
        own interaction was spent inside `ChannelSelectStep`."""
        self.cfg = config.get_vs_config(self.guild_id)
        self._render()
        if self.message is not None:
            try:
                await self.message.edit(embed=self.embed(), view=self)
            except discord.HTTPException:
                pass


class PostTimeModal(discord.ui.Modal):
    """When to post. Read in the guild's own timezone, like every other
    scheduled post, because that is the clock an officer thinks in even though
    the duel day itself resolves on server time."""

    def __init__(self, panel: ScheduledPostSettingsView) -> None:
        super().__init__(title=panel.surface.time_modal_title[:45], timeout=STEP_TIMEOUT)
        self.panel = panel
        self.time = discord.ui.TextInput(
            label="Time of day",
            placeholder="9:00am, 10:15pm, or 22:00",
            default=panel.cfg.get(panel.surface.time_col) or "",
            required=True,
            max_length=16,
        )
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from messages import TIME_PARSE_RETRY
        from scheduler import parse_time_str

        parsed = parse_time_str(self.time.value or "")
        if parsed is None or not (0 <= parsed[0] <= 23 and 0 <= parsed[1] <= 59):
            await interaction.response.send_message(
                TIME_PARSE_RETRY.format(raw=self.time.value), ephemeral=True
            )
            return

        config.save_vs_config(
            self.panel.guild_id,
            **{self.panel.surface.time_col: f"{parsed[0]:02d}:{parsed[1]:02d}"},
        )
        await self.panel._redraw(interaction)


class LeadershipNoteModal(discord.ui.Modal, title="Note from leadership"):
    """A standing line carried on every day theme reminder.

    Blank means **clear it**, which is the opposite of the rule the sheet
    modals follow, and the placeholder says so. The difference is that this
    field is pre-filled with what is saved, so an empty box is a deletion the
    officer can see themselves making rather than a field they left alone.

    Whatever the alliance writes here is carried verbatim. They run in their
    own language and this is their words to their own members.
    """

    def __init__(self, panel: ScheduledPostSettingsView) -> None:
        super().__init__(timeout=STEP_TIMEOUT)
        self.panel = panel
        self.note = discord.ui.TextInput(
            label="Note",
            placeholder="Leave empty to remove the note",
            default=panel.cfg.get(panel.surface.note_col) or "",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        config.save_vs_config(
            self.panel.guild_id, **{self.panel.surface.note_col: (self.note.value or "").strip()}
        )
        await self.panel._redraw(interaction)


class VSSetupView(discord.ui.View):
    """Holds the wizard's state across its two steps."""

    def __init__(self, guild_id: int, owner_user_id: int) -> None:
        super().__init__(timeout=STEP_TIMEOUT)
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.cfg = config.get_vs_config(guild_id)
        self.message: discord.Message | None = None

    async def owns(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_user_id:
            return True
        from messages import DENY_NOT_OWNER

        await interaction.response.send_message(DENY_NOT_OWNER, ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        await expire_view_message(self.message, command_hint=ads.VS_SETUP_NAV)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.owns(interaction)

    @discord.ui.button(label="🏆 Set up Alliance Duel (VS)", style=discord.ButtonStyle.primary)
    async def btn_start(self, inter: discord.Interaction, _b: discord.ui.Button):
        await inter.response.send_modal(OwnAllianceModal(self))

    @discord.ui.button(label=VS_BTN_SCORE_PROMPT, style=discord.ButtonStyle.secondary)
    async def btn_score_prompt(self, inter: discord.Interaction, _b: discord.ui.Button):
        """The score prompt's settings. Disabled until the tracker itself is set
        up, because a prompt has nothing to ask about without a tab and an
        alliance identity."""
        await self._open_panel(inter, SCORE_PROMPT_SURFACE)

    @discord.ui.button(label=VS_BTN_DAY_THEME, style=discord.ButtonStyle.secondary)
    async def btn_day_theme(self, inter: discord.Interaction, _b: discord.ui.Button):
        """The member reminder's settings. Never disabled: this one reads
        nothing from the sheet and ships free, so it works for an alliance that
        has not set the tracker up and never will."""
        await self._open_panel(inter, DAY_THEME_SURFACE)

    @discord.ui.button(label=VS_BTN_EVENT_POSTS, style=discord.ButtonStyle.secondary, row=1)
    async def btn_event_posts(self, inter: discord.Interaction, _b: discord.ui.Button):
        """The three posts that fire when data lands (#409). Needs the tracker,
        since all three read the sheet."""
        await self._open_panel(inter, EVENT_POSTS_SURFACE)

    async def _open_panel(self, inter: discord.Interaction, surface: ScheduledSurface) -> None:
        panel = ScheduledPostSettingsView(self.guild_id, self.owner_user_id, surface)
        await inter.response.send_message(embed=panel.embed(), view=panel, ephemeral=True)
        panel.message = await inter.original_response()

    async def show_mode_step(self, interaction: discord.Interaction) -> None:
        view = TrackingModeView(self)
        await interaction.followup.send(embed=ads.tracking_mode_embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def finish(self, interaction: discord.Interaction, tracking_mode: str) -> None:
        """Save the mode, create the tab, and show the column guide."""
        await interaction.response.defer(ephemeral=True)
        was = self.cfg.get("tracking_mode")
        config.save_vs_config(self.guild_id, tracking_mode=tracking_mode, enabled=1)
        self.cfg = config.get_vs_config(self.guild_id)

        tab_name = self.cfg.get("tab_name") or "Alliance Duel (VS)"
        created = await _create_tab(self.guild_id, tab_name)
        if not created:
            await interaction.followup.send(
                "⚠️ I saved your settings, but could not reach your sheet to "
                f"create the **{tab_name}** tab. Check the bot still has access, "
                f"then run `/setup` and click **{HUB_BTN_VS}** to start again.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            content=(
                f"✅ Set up Alliance Duel (VS), tracking {ads.mode_label(tracking_mode)}. "
                f"Your **{tab_name}** tab is ready to fill in."
            ),
            embed=ads.column_guide_embed(tracking_mode),
            ephemeral=True,
        )

        # Widening mid-league leaves the sheet short of a full bracket. Offer
        # the rows rather than leaving fifteen per week to be typed by hand
        # (#448). Only on the widening direction: narrowing needs no rows, and
        # nothing is ever deleted.
        if was == ad.MODE_OWN_ALLIANCE and tracking_mode == ad.MODE_FULL_BRACKET:
            await self._offer_missing_rows(interaction, tab_name)

    async def _offer_missing_rows(self, interaction: discord.Interaction, tab_name: str) -> None:
        import asyncio

        rows = await asyncio.to_thread(ads.load_rows, self.guild_id, tab_name)
        if not rows:
            return  # nothing recorded yet, or the sheet is unreachable and already reported
        league = ad.latest_league(rows)
        if league is None:
            return
        missing = ad.missing_bracket_rows(rows, league)
        if not missing:
            return

        view = FillBracketView(self, league, missing, tab_name)
        await interaction.followup.send(
            embed=ads.fill_bracket_embed(league, missing), view=view, ephemeral=True
        )
        view.message = await interaction.original_response()


class FillBracketView(discord.ui.View):
    """Offers the blank rows, and never writes without being asked.

    Declining is a real answer: an alliance may want the fuller views without
    backfilling a league already half over.
    """

    def __init__(self, parent: "VSSetupView", league, missing, tab_name: str) -> None:
        super().__init__(timeout=STEP_TIMEOUT)
        self._parent = parent
        self._league = league
        self._missing = missing
        self._tab = tab_name
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        await expire_view_message(self.message, command_hint=ads.VS_SETUP_NAV)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self._parent.owns(interaction)

    @discord.ui.button(label="Add the rows", style=discord.ButtonStyle.primary)
    async def btn_add(self, inter: discord.Interaction, _b: discord.ui.Button):
        await inter.response.defer(ephemeral=True)
        self.stop()
        added = await _append_blank_rows(
            self._parent.guild_id, self._tab, self._league, self._missing
        )
        if added is None:
            await safe_edit_response(
                inter,
                content=(
                    "⚠️ Could not reach your sheet to add the rows. Your tracking "
                    f"mode is saved. Run `/setup` and click **{HUB_BTN_VS}** to try "
                    "the rows again."
                ),
                embed=None,
                view=None,
            )
            return
        await safe_edit_response(
            inter,
            content=(
                f"✅ Added {added} blank row{'s' if added != 1 else ''} to your "
                f"**{self._tab}** tab. Fill in the tag, warzone and ranking for each "
                "from the in-game League screen."
            ),
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Not now", style=discord.ButtonStyle.secondary)
    async def btn_skip(self, inter: discord.Interaction, _b: discord.ui.Button):
        self.stop()
        await safe_edit_response(
            inter,
            content=(
                "Left your sheet as it is. You can add the rows yourself, or run "
                f"`/setup` and click **{HUB_BTN_VS}** to be offered them again."
            ),
            embed=None,
            view=None,
        )


async def _append_blank_rows(guild_id: int, tab_name: str, league, missing) -> int | None:
    """Append the placeholder rows. Returns how many, or None if unreachable.

    Appends rather than upserting: a blank row has no Tag or Warzone, so it has
    no key to upsert on. Once the user fills identity in, later writes locate
    it normally.
    """
    import asyncio

    def _work():
        import config_health

        spreadsheet = config.get_spreadsheet(guild_id)
        worksheet = ads.ensure_tab(spreadsheet, tab_name)
        header = worksheet.row_values(1) or list(ad.SHEET_COLUMNS)
        lines = []
        for week, (count, stamp) in sorted(missing.items()):
            for _ in range(count):
                lines.append(ad.blank_bracket_values(header, league, week, stamp))
        if lines:
            worksheet.append_rows(lines, value_input_option="USER_ENTERED")
        config_health.clear(guild_id, ads.VS_SHEET_SUBJECT)
        return len(lines)

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001 - classified by config_health
        import config_health

        config_health.record_sheet_failure(guild_id, ads.VS_SHEET_SUBJECT, e, tab=tab_name)
        logger.warning(
            "[VS] could not append bracket rows for guild=%s: %s",
            guild_id,
            config.describe_sheet_error(e, guild_id=guild_id, tab=tab_name),
        )
        return None


async def _create_tab(guild_id: int, tab_name: str) -> bool:
    """Create the tab off the event loop. Returns False if the sheet is
    unreachable, which `load_rows` will already have reported through
    `config_health`."""
    import asyncio

    def _work():
        spreadsheet = config.get_spreadsheet(guild_id)
        ads.ensure_tab(spreadsheet, tab_name)
        return True

    try:
        return await asyncio.to_thread(_work)
    except Exception as e:  # noqa: BLE001 - classified by config_health
        import config_health

        config_health.record_sheet_failure(guild_id, ads.VS_SHEET_SUBJECT, e, tab=tab_name)
        logger.warning(
            "[VS] could not create tab for guild=%s: %s",
            guild_id,
            config.describe_sheet_error(e, guild_id=guild_id, tab=tab_name),
        )
        return False


async def run_vs_setup(interaction: discord.Interaction, bot=None) -> None:
    """Entry point from the `/setup` hub.

    Re-entry shows what is already configured rather than starting blank, so
    an officer who did not set this up can see the current state before
    changing it.
    """
    guild_id = interaction.guild_id
    cfg = config.get_vs_config(guild_id)

    if cfg.get("enabled"):
        described = (
            f"Tracking {ads.mode_label(cfg['tracking_mode'])} for "
            f"**[{cfg['own_tag'].upper()}] {cfg['own_warzone']}**."
        )
    else:
        described = "Not set up yet."

    embed = discord.Embed(
        title="🏆 Alliance Duel (VS)",
        description=(
            f"{described}\n\n"
            "Record your VS league in your sheet, and the bot reads it back "
            "as your bracket, your projected path, and your history against "
            "the alliances you have faced."
        ),
        color=discord.Color.blurple(),
    )
    view = VSSetupView(guild_id, interaction.user.id)
    if cfg.get("enabled"):
        view.btn_start.label = "✏️ Change alliance or mode"
        view.btn_start.style = discord.ButtonStyle.secondary
    else:
        # Nothing to prompt about until there is a tab and an alliance identity,
        # so the control is shown disabled rather than left live and inert.
        view.btn_score_prompt.disabled = True
        view.btn_event_posts.disabled = True

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()


__all__ = [
    "run_vs_setup",
    "VSSetupView",
    "TrackingModeView",
    "OwnAllianceModal",
    "ScheduledPostSettingsView",
    "ScheduledSurface",
    "SCORE_PROMPT_SURFACE",
    "DAY_THEME_SURFACE",
    "PostTimeModal",
    "LeadershipNoteModal",
]

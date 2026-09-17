"""
The storm setup wizard (Desert Storm and Canyon Storm): the re-entry
summary, the Sheet tab, which teams the alliance runs and their time
slots (#251), the log and mail post channels, the per-team mail templates
with Keep current on re-entry (#231), the participation step (#20) and
the structured-flow step (#38 + #54), the reminder DM, the save, the
summary embed and the first sign-up offer (#144).

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup_structured.py` set in round 1: `setup_cog`
imports `run_storm_setup` back under its old name, and this module reaches
into `setup_cog` at call time (`_setup()`) for the pieces the tests patch
there (`ChannelSelectStep`, `ask_keep_or_change`, the participation and
structured steps, the re-entry summary, the sign-up offer), so every
`patch("setup_cog.X")` keeps its target.

Shape:
* `_Wizard` carries the handles every question needs; `_Saved` is what
  the wizard read from the database before asking anything; `_Answers`
  is what the officer chose, handed from the questions to the save.
* Each step is one `_ask_*` function that posts its prompt, waits, and
  returns the answer.
* A cancelled or timed-out question raises `_Abort`; `run_storm_setup`
  turns that into the bare return the old function made at each exit.
* The four views the old function defined inline (`TeamChoiceView`,
  `TeamSlotView`, `TemplateChoiceView`, `SharedTemplateView`) are module
  classes under the same names, so the tests that answer them by
  attribute (`selected`, `outcome`) and by class name keep working.

Every prompt, button label, acknowledgement, saved row and embed field is
unchanged from the function this replaced;
`tests/integration/test_storm_setup.py` was written against that function
and holds this one to it.
"""

import asyncio
from dataclasses import dataclass, field

import discord

import premium
import wizard_registry
from messages import GENERIC_CMD_TIMEOUT, PREV_CHANNEL_GONE
from setup_hub import STORM_GLYPH
from storm_event_hub import HUB_BTN_POST_SIGNUP, HUB_COMMAND
from wizard_registry import wait_view_or_cancel
from wizard_steps import TIMEZONE_LABELS


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


_TEAM_BLURB = {
    "both": "Team A & Team B",
    "A": "Team A only",
    "B": "Team B only",
}
_TEAM_SHORT = {"both": "A & B", "A": "A only", "B": "B only"}


# ── Shared wizard handles ────────────────────────────────────────────────────


@dataclass
class _Wizard:
    interaction: discord.Interaction
    bot: discord.Client
    event_type: str
    channel: discord.abc.Messageable
    user: discord.abc.User
    guild_id: int
    label: str
    cmd_name: str
    cancel_event: asyncio.Event
    is_premium: bool = False

    @property
    def parent_cmd(self) -> str:
        return "desertstorm" if self.event_type == "DS" else "canyonstorm"

    def check(self, m) -> bool:
        return m.author == self.user and m.channel == self.channel

    async def wait(self, view) -> None:
        """Wait for `view`, raising `_Abort` if the officer cancelled."""
        await wait_view_or_cancel(view, self.cancel_event)
        if getattr(view, "cancelled", False):
            raise _Abort

    async def ask(self, text: str, view, attr: str):
        """Post `text` with `view`, wait, and return `view.<attr>`. A
        timeout (the attribute still unset) posts the route back and
        raises `_Abort`."""
        await self.channel.send(text, view=view)
        await self.wait(view)
        value = getattr(view, attr)
        if not value:
            await self.timed_out()
            raise _Abort
        return value

    async def timed_out(self) -> None:
        await self.channel.send(GENERIC_CMD_TIMEOUT.format(cmd=self.cmd_name))


@dataclass
class _Saved:
    """What the wizard read before asking anything."""

    current: dict
    current_structured: dict
    guild_cfg: object
    timezone: str
    tz_label: str
    already_configured: bool
    log_channel_id: int
    post_channel_id: int
    template_a: str
    template_b: str
    default_template: str
    placeholder_info: str


@dataclass
class _Answers:
    """What the officer chose, in the order the wizard asked."""

    tab_name: str = ""
    teams: str = "both"
    slot_labels: list = field(default_factory=list)
    team_a_slot: int | None = None
    team_b_slot: int | None = None
    log_channel_id: int = 0
    post_channel_id: int = 0
    template_a: str = ""
    template_b: str = ""
    participation: dict = field(default_factory=dict)
    structured: dict = field(default_factory=dict)
    dm_reminder_message: str = ""


# ── Views ────────────────────────────────────────────────────────────────────


class TeamChoiceView(discord.ui.View):
    """Step 2: which teams the alliance runs. Keep current is the leftmost
    button and carries the saved choice on its label; it is removed on a
    fresh setup, where there is nothing to keep."""

    def __init__(self, *, prompt: str, saved_teams: str, already_configured: bool):
        super().__init__(timeout=120)
        self.selected = None
        self.prompt = prompt
        self.saved_teams = saved_teams
        if already_configured:
            # Surface the saved value on the Keep current button so the
            # officer can see what would be preserved without reading the
            # prompt — mirrors the convention used by `ask_keep_or_change`.
            self.keep_current.label = f"Keep current: {_TEAM_BLURB[saved_teams]}"[:80]
        else:
            # Hide Keep current on fresh setup — there's no current value to keep.
            self.remove_item(self.keep_current)

    async def _pick(self, inter: discord.Interaction, selected: str, ack: str) -> None:
        self.selected = selected
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(
            inter, content=f"{self.prompt}\n\n{ack}", view=self
        )
        self.stop()

    # Re-entry: keep the previously-saved choice without re-clicking.
    # Defined first so it always renders as the leftmost button (the
    # Keep-current-is-always-first convention).
    @discord.ui.button(label="Keep current", style=discord.ButtonStyle.success)
    async def keep_current(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._pick(
            inter,
            self.saved_teams,
            f"✅ Teams: **{_TEAM_BLURB[self.saved_teams]}** (kept current)",
        )

    @discord.ui.button(label="Team A & Team B", style=discord.ButtonStyle.primary)
    async def both(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._pick(inter, "both", "✅ Teams: **Team A & Team B**")

    @discord.ui.button(label="Team A only", style=discord.ButtonStyle.secondary)
    async def a_only(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._pick(inter, "A", "✅ Teams: **Team A only**")

    @discord.ui.button(label="Team B only", style=discord.ButtonStyle.secondary)
    async def b_only(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._pick(inter, "B", "✅ Teams: **Team B only**")


class TeamSlotView(discord.ui.View):
    """Step 3: one team's time slot. The two slot buttons carry the
    game-defined labels; Keep current, leftmost, is present only when a
    slot is saved and carries that slot on its label."""

    def __init__(self, *, prompt: str, team_letter: str, slot_labels: list, saved_idx):
        super().__init__(timeout=180)
        self.selected = None
        self.prompt = prompt
        self.team_letter = team_letter
        self.slot_labels = slot_labels
        self.saved_idx = saved_idx
        if saved_idx in (1, 2):
            # Surface the saved slot on the Keep current button so the
            # officer can see what would be preserved without reading
            # back through the prompt — matches the convention used by
            # `ask_keep_or_change` elsewhere in the wizard.
            keep = discord.ui.Button(
                label=f"Keep current: {slot_labels[saved_idx - 1]}"[:80],
                style=discord.ButtonStyle.success,
            )
            keep.callback = self._keep_current
            self.add_item(keep)
        for idx in (1, 2):
            button = discord.ui.Button(
                label=slot_labels[idx - 1], style=discord.ButtonStyle.primary
            )
            button.callback = self._slot_picker(idx)
            self.add_item(button)

    async def _pick(self, inter: discord.Interaction, selected, ack: str) -> None:
        self.selected = selected
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(
            inter, content=f"{self.prompt}\n\n{ack}", view=self
        )
        self.stop()

    async def _keep_current(self, inter: discord.Interaction) -> None:
        kept_label = self.slot_labels[self.saved_idx - 1] if self.saved_idx in (1, 2) else "—"
        await self._pick(
            inter,
            self.saved_idx,
            f"✅ Team {self.team_letter}: **{kept_label}** (kept current)",
        )

    def _slot_picker(self, idx: int):
        async def pick(inter: discord.Interaction) -> None:
            await self._pick(
                inter, idx, f"✅ Team {self.team_letter}: **{self.slot_labels[idx - 1]}**"
            )

        return pick


class TemplateChoiceView(discord.ui.View):
    """One team's mail template: keep the saved custom body (re-entry
    only), use the default, or paste a new one."""

    def __init__(self, *, team_label: str, saved_is_custom: bool):
        super().__init__(timeout=300)
        self.outcome: str | None = None  # "keep" | "default" | "edit"
        self.team_label = team_label
        self.saved_is_custom = saved_is_custom
        if not saved_is_custom:
            # First-time / saved-equals-default — drop the Keep current
            # button and let the default-color Use default button act as
            # the success default like the pre-#231 view.
            self.remove_item(self.keep)
            self.use_def.style = discord.ButtonStyle.success

    def _disable(self) -> None:
        for item in self.children:
            item.disabled = True

    # Re-entry only — only added when saved_is_custom.
    @discord.ui.button(label="Keep current custom template", style=discord.ButtonStyle.success)
    async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
        self.outcome = "keep"
        self._disable()
        await wizard_registry.safe_edit_response(
            inter,
            content=f"✅ Keeping your saved custom template for {self.team_label}.",
            view=self,
        )
        self.stop()

    @discord.ui.button(label="↩️ Use default template", style=discord.ButtonStyle.secondary)
    async def use_def(self, inter: discord.Interaction, button: discord.ui.Button):
        self.outcome = "default"
        self._disable()
        msg = (
            f"✅ Reverted to default template for {self.team_label}."
            if self.saved_is_custom
            else f"✅ Using default template for {self.team_label}."
        )
        await wizard_registry.safe_edit_response(inter, content=msg, view=self)
        self.stop()

    @discord.ui.button(label="✏️ Edit template", style=discord.ButtonStyle.secondary)
    async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
        self.outcome = "edit"
        self._disable()
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()


class SharedTemplateView(discord.ui.View):
    """Both teams: one shared template or one per team. Keep current is
    present only when both saved rows have a body to compare."""

    def __init__(self, *, saved_share_mode: str | None):
        super().__init__(timeout=120)
        self.selected = None
        self.saved_share_mode = saved_share_mode
        if saved_share_mode is None:
            self.remove_item(self.keep_current)
        else:
            self.keep_current.label = (
                "Keep current: One shared template"
                if saved_share_mode == "shared"
                else "Keep current: Separate templates"
            )

    async def _pick(self, inter: discord.Interaction, selected: str, ack: str) -> None:
        self.selected = selected
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, content=ack, view=self)
        self.stop()

    # Re-entry only — removed when saved_share_mode is None.
    @discord.ui.button(label="Keep current", style=discord.ButtonStyle.success)
    async def keep_current(self, inter: discord.Interaction, button: discord.ui.Button):
        ack = (
            "✅ Kept current: **One shared template** for Team A & B"
            if self.saved_share_mode == "shared"
            else "✅ Kept current: **Separate templates** for Team A & Team B"
        )
        await self._pick(inter, self.saved_share_mode, ack)

    @discord.ui.button(label="One template for both teams", style=discord.ButtonStyle.primary)
    async def shared(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._pick(inter, "shared", "✅ **One shared template** for Team A & B")

    @discord.ui.button(label="Separate templates per team", style=discord.ButtonStyle.secondary)
    async def separate(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._pick(inter, "separate", "✅ **Separate templates** for Team A & Team B")


# ── What is saved ────────────────────────────────────────────────────────────


def _load_saved(w: _Wizard) -> _Saved:
    from config import (
        get_storm_config,
        get_config,
        has_storm_config,
        get_structured_storm_config,
    )
    from defaults import DEFAULT_DS_TEMPLATE, DEFAULT_CS_TEMPLATE

    current = get_storm_config(w.guild_id, w.event_type)
    current_structured = get_structured_storm_config(w.guild_id, w.event_type)
    guild_cfg = get_config(w.guild_id)
    timezone = guild_cfg.timezone if guild_cfg and guild_cfg.timezone else "America/New_York"
    saved_log_ch = (
        (guild_cfg.ds_log_channel_id if w.event_type == "DS" else guild_cfg.cs_log_channel_id)
        if guild_cfg
        else 0
    )

    # Per-team saved mail templates — drives Step 5 Keep-current (#231).
    # save_storm_config persists DS_A / DS_B (and CS_A / CS_B) rows per
    # team; the base DS / CS row mirrors whichever side has content. Read
    # the per-team rows directly so the wizard can distinguish "saved
    # custom" from "saved default" from "no row".
    saved_template_a = ""
    saved_template_b = ""
    if has_storm_config(w.guild_id, f"{w.event_type}_A"):
        saved_template_a = (
            get_storm_config(w.guild_id, f"{w.event_type}_A").get("mail_template") or ""
        ).strip()
    if has_storm_config(w.guild_id, f"{w.event_type}_B"):
        saved_template_b = (
            get_storm_config(w.guild_id, f"{w.event_type}_B").get("mail_template") or ""
        ).strip()

    # Default template and placeholders per event type
    if w.event_type == "DS":
        default_template = DEFAULT_DS_TEMPLATE
        placeholder_info = (
            "• `{alliance_name}`: your alliance name\n"
            "• `{zones}`: zone assignments block\n"
            "• `{subs}`: substitute members\n"
            "• `{time}`: event time (auto-filled when drafting)"
        )
    else:
        default_template = DEFAULT_CS_TEMPLATE
        placeholder_info = (
            "• `{alliance_name}`: your alliance name\n"
            "• `{zones}`: zone assignments block\n"
            "• `{subs}`: substitute members\n"
            "• `{time}`: event time (auto-filled when drafting)"
        )

    return _Saved(
        current=current,
        current_structured=current_structured,
        guild_cfg=guild_cfg,
        timezone=timezone,
        tz_label=TIMEZONE_LABELS.get(timezone, timezone),
        already_configured=has_storm_config(w.guild_id, w.event_type),
        log_channel_id=saved_log_ch or 0,
        post_channel_id=current.get("post_channel_id") or 0,
        template_a=saved_template_a,
        template_b=saved_template_b,
        default_template=default_template,
        placeholder_info=placeholder_info,
    )


# ── Re-entry summary ─────────────────────────────────────────────────────────


async def _confirm_reentry(w: _Wizard, s: _Saved) -> None:
    """On a configured guild, show the summary and offer edit or cancel;
    anything but Edit ends the wizard."""
    if not s.already_configured:
        return
    templates = s.current.get("templates") or []
    structured_status = (
        "✅ Enabled"
        if s.current_structured.get("structured_flow_enabled")
        else "❌ Off (preset tabs available on free tier)"
    )
    fields = [
        ("Sheet Tab", s.current.get("tab_name") or "*not set*"),
        ("Log Channel", f"<#{s.log_channel_id}>" if s.log_channel_id else "*not set*"),
        ("Post Channel", f"<#{s.post_channel_id}>" if s.post_channel_id else "*not set*"),
        ("Timezone", s.tz_label),
        (
            "Mail Templates",
            ", ".join(t["name"] for t in templates) if templates else "Default",
        ),
        (
            "Reminder DM",
            "Custom" if (s.current.get("dm_reminder_message") or "").strip() else "Default",
        ),
        ("Structured Roster Flow", structured_status),
    ]
    # CS reads `teams` like DS does (Rule A / #166), so surface the
    # field in the re-entry summary for both event types — officers
    # can see their current single-team / both-teams config.
    _summary_teams = _TEAM_SHORT.get((s.current.get("teams") or "both"), "A & B")
    fields.insert(1, ("Teams", _summary_teams))

    # Team time-slot mapping (#251). Surfaced so officers can see at
    # a glance whether the slots are set, and what they're set to,
    # without having to re-enter the wizard's Step 3.
    from config import get_storm_slot_labels as _gslot

    try:
        _slot_lbls = _gslot(w.event_type, w.guild_id)
    except Exception:
        _slot_lbls = []
    _team_summary = (s.current.get("teams") or "both").strip()
    _a_idx = s.current.get("team_a_slot_index")
    _b_idx = s.current.get("team_b_slot_index")

    def _slot_blurb(idx):
        if idx in (1, 2) and len(_slot_lbls) >= idx:
            return _slot_lbls[idx - 1]
        return "*not set*"

    if _team_summary == "A":
        _times_value = f"Team A: {_slot_blurb(_a_idx)}"
    elif _team_summary == "B":
        _times_value = f"Team B: {_slot_blurb(_b_idx)}"
    else:
        _times_value = f"Team A: {_slot_blurb(_a_idx)} · Team B: {_slot_blurb(_b_idx)}"
    fields.insert(2, ("Team Times", _times_value))
    emoji = STORM_GLYPH[w.event_type]
    proceed = await _setup().ask_proceed_with_existing_config(
        w.channel,
        title=f"{emoji} Current {w.label} Setup",
        description=f"{w.label} is already configured. Would you like to edit these settings?",
        fields=fields,
        cancel_event=w.cancel_event,
        no_changes_message=f"✅ No changes made. Your {w.label} setup is still active.",
    )
    if proceed is not True:
        raise _Abort


# ── The questions ────────────────────────────────────────────────────────────


async def _ask_tab(w: _Wizard, s: _Saved) -> str:
    """Step 1: the Sheet tab."""
    # When Member Sync is enabled, default to the alliance's Member
    # Sync tab name (typically "Member Roster") — that's the canonical
    # roster location for everything else in the bot, so suggesting the
    # same tab here keeps the alliance's mental model coherent. Falls
    # back to the legacy `DS Assignments` / `CS Assignments` default
    # when Member Sync isn't configured yet.
    from config import get_member_roster_config as _gmrc_step1

    _sync_cfg_step1 = _gmrc_step1(w.guild_id) if w.guild_id else {}
    if _sync_cfg_step1.get("enabled"):
        hardcoded_tab = _sync_cfg_step1.get("tab_name") or "Member Roster"
    else:
        hardcoded_tab = "DS Assignments" if w.event_type == "DS" else "CS Assignments"
    tab_name = await _setup().ask_keep_or_change(
        w.channel,
        f"**Step 1 of 9: Sheet Tab**\n"
        f"Which tab in your Google Sheet stores the {w.label} zone assignments?\n"
        f"⚠️ *Make sure this tab exists in your sheet before continuing.*\n"
        f"ℹ️ *The bot will manage the data structure of this tab automatically. "
        f"you don't need to set up any specific columns or formatting beforehand.*",
        default=hardcoded_tab,
        current=s.current.get("tab_name", ""),
        modal_title="Sheet Tab Name",
        modal_label="Tab name",
        timeout_cmd=w.cmd_name,
        cancel_event=w.cancel_event,
    )
    if tab_name is None:
        raise _Abort
    await _setup().warn_if_tab_claimed(
        w.channel, w.guild_id, tab_name, exclude_field="storm_tab_name"
    )
    return tab_name


async def _ask_teams(w: _Wizard, s: _Saved) -> str:
    """Step 2: which teams the alliance runs ('both' / 'A' / 'B')."""
    saved_teams_raw = (s.current.get("teams") or "both").strip()
    saved_teams = saved_teams_raw if saved_teams_raw in ("both", "A", "B") else "both"

    # Capture the prompt so the button callbacks can preserve it in the
    # edited message — otherwise the question disappears the moment a
    # button is clicked and officers scrolling back to review what they
    # answered see only the bare confirmation line.
    team_prompt = f"**Step 2 of 9: Which teams do you run for {w.label}?**" + (
        f"\nCurrent: **{_TEAM_BLURB[saved_teams]}**" if s.already_configured else ""
    )
    view = TeamChoiceView(
        prompt=team_prompt, saved_teams=saved_teams, already_configured=s.already_configured
    )
    return await w.ask(team_prompt, view, "selected")


async def _pick_team_slot(w: _Wizard, team_letter: str, saved_idx, slot_labels: list) -> int:
    """Single-team slot picker. Returns 1 / 2; `saved_idx` drives whether
    Keep current renders."""
    current_line = f"\nCurrent: **{slot_labels[saved_idx - 1]}**" if saved_idx in (1, 2) else ""
    # Capture the prompt so each button callback can echo it in the
    # edited message — keeps the original question visible when the
    # officer scrolls back to review what they chose, instead of
    # leaving only the bare confirmation line.
    slot_prompt = f"Which time slot does **Team {team_letter}** run for {w.label}?" + current_line
    view = TeamSlotView(
        prompt=slot_prompt, team_letter=team_letter, slot_labels=slot_labels, saved_idx=saved_idx
    )
    return await w.ask(slot_prompt, view, "selected")


async def _ask_team_slots(w: _Wizard, s: _Saved, teams: str, a: _Answers) -> None:
    """Step 3: each run team's time slot (#251), written into `a`."""
    # DS / CS each have two game-defined time slots; this step records
    # which slot each team the alliance runs is on. Both teams can pick
    # the same slot. Independent per event type. The mapping is the
    # default for every weekly sign-up; the officer can override it for
    # a single week when posting that week's sign-up.
    from config import get_storm_slot_labels

    a.slot_labels = get_storm_slot_labels(w.event_type, w.guild_id)
    await w.channel.send(
        f"**Step 3 of 9: Team Time Slots**\n"
        f"Select the time when you typically run each {w.label} team. "
        f"You can override these for a single week when you send out the "
        f"sign up, if needed."
    )
    if teams in ("both", "A"):
        a.team_a_slot = await _pick_team_slot(
            w, "A", s.current.get("team_a_slot_index"), a.slot_labels
        )
    if teams in ("both", "B"):
        a.team_b_slot = await _pick_team_slot(
            w, "B", s.current.get("team_b_slot_index"), a.slot_labels
        )


async def _ask_channel(
    w: _Wizard,
    *,
    picker_prompt: str,
    suggested_name: str,
    current_id: int,
    gone_label: str,
    step_text: str,
) -> int:
    """One channel picker step: the saved channel's absence is called
    out, the picker posted, and the picked channel's id returned."""
    view = _setup().ChannelSelectStep(
        picker_prompt,
        suggested_name=suggested_name,
        include_threads=w.is_premium,
        guild=w.interaction.guild,
        current_id=current_id,
    )
    if view.is_current_stale:
        await w.channel.send(PREV_CHANNEL_GONE.format(channel_label=gone_label))
    await w.channel.send(step_text, view=view)
    await w.wait(view)
    if not view.confirmed:
        await w.timed_out()
        raise _Abort
    return view.selected_channel.id


async def _ask_log_channel(w: _Wizard, s: _Saved) -> int:
    """Step 4: the storm log channel. Reused by /[event]_log lookups and
    by the participation flow when leadership posts the summary."""
    return await _ask_channel(
        w,
        picker_prompt=f"Select the {w.label} log channel...",
        suggested_name="storm-log",
        current_id=s.log_channel_id,
        gone_label=f"{w.label} log",
        step_text=(
            f"**Step 4 of 9: Storm Log Channel**\n"
            f"Select the channel where {w.label} participation/log summaries will be posted:"
        ),
    )


async def _ask_post_channel(w: _Wizard, s: _Saved) -> int:
    """Step 5: where 📄 Generate mail posts the final mail."""
    return await _ask_channel(
        w,
        picker_prompt=f"Select the {w.label} mail post channel...",
        suggested_name=f"{'desert' if w.event_type == 'DS' else 'canyon'}-storm",
        current_id=s.post_channel_id,
        gone_label=f"{w.label} mail post",
        step_text=(
            f"**Step 5 of 9: Mail Post Channel**\n"
            f"When leadership clicks **Post & Copy** at the end of "
            f"`/{w.parent_cmd}` → **📄 Generate mail**, the finished mail "
            f"will be posted to this channel:"
        ),
    )


async def _get_template(w: _Wizard, s: _Saved, team_label: str, saved_template: str = "") -> str:
    """Get template for one team — show default with use/edit choice.

    When `saved_template` is a non-empty body that differs from the
    hardcoded default, render a 3-button view (Keep current / Use
    default / Edit) so re-entering officers can preserve their
    custom body. Pre-#231 the only options were Use default (which
    silently overwrote the saved custom) and Edit (which forced a
    re-paste from scratch).
    """
    saved_is_custom = bool(saved_template) and saved_template != s.default_template
    choice_view = TemplateChoiceView(team_label=team_label, saved_is_custom=saved_is_custom)

    custom_block = (
        f"\n\nHere is your saved custom template:\n```\n{saved_template}\n```"
        if saved_is_custom
        else ""
    )
    question = (
        "Would you like to keep your custom template, revert to the default, or edit it?"
        if saved_is_custom
        else "Would you like to use this or edit it?"
    )
    outcome = await w.ask(
        f"**{w.label} Mail Template: {team_label}**\n"
        f"When you draft the mail each week, you will be able to select the time slot "
        f"when you are running that team's {w.label}.\n\n"
        f"Here is the default template:\n"
        f"```\n{s.default_template}\n```"
        f"{custom_block}\n\n"
        f"{question}",
        choice_view,
        "outcome",
    )
    if outcome == "keep":
        return saved_template
    if outcome == "default":
        return s.default_template

    # User wants to edit — show variables and ask for input. When a
    # custom body is saved, point them at it as the natural starting
    # point so they can copy-modify instead of typing from scratch.
    reference_label = "current custom" if saved_is_custom else "default"
    await w.channel.send(
        f"Paste your custom template for **{team_label}**. "
        f"You can copy the {reference_label} above and modify it, or write your own.\n\n"
        f"**Available placeholders:**\n{s.placeholder_info}\n\n"
        f"*This form will time out in 5 minutes. "
        f"You can run `/{w.cmd_name}` again if it times out.*"
    )
    try:
        reply = await w.bot.wait_for("message", check=w.check, timeout=300)
    except asyncio.TimeoutError:
        await w.timed_out()
        raise _Abort
    fallback = saved_template if saved_is_custom else s.default_template
    return reply.content.strip() or fallback


async def _ask_templates(w: _Wizard, s: _Saved, teams: str) -> tuple[str, str]:
    """Step 6: the mail template(s). Returns (template_a, template_b);
    the side of a team the alliance does not run is ''."""
    if teams != "both":
        team_label = "Team A" if teams == "A" else "Team B"
        # Single-team mode — only the saved row for the picked team is
        # relevant; the other side stays empty.
        saved_for_team = s.template_a if teams == "A" else s.template_b
        await w.channel.send("**Step 6 of 9: Mail Template**")
        template = await _get_template(w, s, team_label, saved_template=saved_for_team)
        return (template if teams == "A" else ""), (template if teams == "B" else "")

    # Derive saved shared-vs-separate from per-team rows so re-entry
    # offers Keep current instead of forcing officers to re-pick
    # (#231). Only resolvable when BOTH team rows have non-empty
    # bodies — switching from A-only / B-only to both is first-time
    # for the shared/separate decision.
    if s.template_a and s.template_b:
        saved_share_mode = "shared" if s.template_a == s.template_b else "separate"
    else:
        saved_share_mode = None

    shared_view = SharedTemplateView(saved_share_mode=saved_share_mode)
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
    mode = await w.ask("\n".join(prompt_lines), shared_view, "selected")

    if mode == "shared":
        # Shared mode — feed the saved A template (equal to B on a
        # prior shared save) as the Keep-current candidate. When the
        # prior save was separate, leave it blank so the user gets a
        # first-time template prompt for the new shared body.
        shared_saved = s.template_a if saved_share_mode == "shared" else ""
        template_a = await _get_template(w, s, "Team A & B", saved_template=shared_saved)
        return template_a, template_a
    template_a = await _get_template(w, s, "Team A", saved_template=s.template_a)
    template_b = await _get_template(w, s, "Team B", saved_template=s.template_b)
    return template_a, template_b


async def _ask_participation(w: _Wizard, s: _Saved) -> dict:
    """Step 7: participation log tracking (optional), its own sub-flow."""
    cfg = await _setup()._run_storm_participation_step(
        w.channel,
        w.bot,
        w.user,
        w.cancel_event,
        guild_id=w.guild_id,
        event_type=w.event_type,
        label=w.label,
        cmd_name=w.cmd_name,
        is_premium_flag=w.is_premium,
        current=s.current,
    )
    if cfg is None:
        raise _Abort  # cancelled / timed out
    return cfg


async def _ask_structured(w: _Wizard, s: _Saved) -> dict:
    """Step 8: the structured roster flow (#38 + #54), Premium opt-in
    plus preset tabs, its own sub-flow."""
    cfg = await _setup()._run_structured_flow_setup_step(
        w.channel,
        w.bot,
        w.user,
        w.cancel_event,
        guild_id=w.guild_id,
        event_type=w.event_type,
        label=w.label,
        cmd_name=w.cmd_name,
        is_premium_flag=w.is_premium,
        current=s.current,
        current_structured=s.current_structured,
        interaction_guild=w.interaction.guild,
    )
    if cfg is None:
        raise _Abort  # cancelled / timed out
    return cfg


async def _ask_reminder_dm(w: _Wizard, s: _Saved) -> str:
    """Step 9: the reminder DM body (💎 Premium). Returns '' when the
    officer keeps the default, so the hardcoded default picks up future
    tweaks without alliances re-running setup."""
    # The body of the DM that fires when leadership clicks
    # 📨 Send DM reminder to roster on the storm hub. Stored per
    # (guild_id, event_type) so DS and CS can have different copy. Free
    # guilds can configure this now too — it just won't fire until they
    # upgrade.
    from storm_log import DEFAULT_STORM_REMINDER_DM

    default_remind_dm = DEFAULT_STORM_REMINDER_DM.format(label=w.label)
    saved_remind_dm = (s.current.get("dm_reminder_message") or "").strip()
    remind_dm = await _setup().ask_keep_or_change(
        w.channel,
        f"**Step 9 of 9: {w.label} Reminder DM (💎 Premium)**\n"
        f"When leadership clicks **📨 Send DM reminder to roster** on "
        f"`/{w.parent_cmd}`, the bot DMs every roster member this message. "
        f"Free guilds can configure it now; it just won't fire until "
        f"you have Premium + Member Sync.\n\n"
        f"Use `{{name}}` as a placeholder for the member's roster name (optional).",
        default=default_remind_dm,
        current=saved_remind_dm,
        modal_title=f"{w.label} Reminder DM",
        modal_label="DM body (max 1000 chars)",
        timeout_cmd=w.cmd_name,
        cancel_event=w.cancel_event,
    )
    if remind_dm is None:
        raise _Abort
    return "" if remind_dm == default_remind_dm else remind_dm


# ── Save, summary, offer ─────────────────────────────────────────────────────


def _save(w: _Wizard, s: _Saved, a: _Answers) -> None:
    from config import (
        save_storm_config,
        save_participation_config,
        update_config_field,
        save_structured_storm_config,
        save_storm_team_slots,
        save_roster_dm_templates,
    )

    # `teams` carries the wizard's Step 2 choice ('both' / 'A' / 'B') so
    # the strategy preset editor can hide Min Power inputs for a team the
    # alliance doesn't run (#148). CS rows store 'both' and ignore it.
    teams_persisted = a.teams if w.event_type == "DS" else "both"
    for side, template in (("A", a.template_a), ("B", a.template_b)):
        if template:
            save_storm_config(
                w.guild_id,
                f"{w.event_type}_{side}",
                a.tab_name,
                template,
                s.timezone,
                a.log_channel_id,
                post_channel_id=a.post_channel_id,
                dm_reminder_message=a.dm_reminder_message,
                teams=teams_persisted,
            )
    save_storm_config(
        w.guild_id,
        w.event_type,
        a.tab_name,
        a.template_a or a.template_b,
        s.timezone,
        a.log_channel_id,
        post_channel_id=a.post_channel_id,
        dm_reminder_message=a.dm_reminder_message,
        teams=teams_persisted,
    )

    # Persist the per-team slot mapping (#251). Kept separate from
    # save_storm_config so this step can be re-run without re-typing the
    # rest of the config — same precedent as save_structured_storm_config.
    save_storm_team_slots(
        w.guild_id,
        w.event_type,
        team_a_slot_index=a.team_a_slot,
        team_b_slot_index=a.team_b_slot,
    )

    # Persist the participation config to the (guild, event_type) row.
    save_participation_config(
        w.guild_id,
        w.event_type,
        enabled=a.participation["enabled"],
        tab_name=a.participation["tab_name"],
        questions=a.participation["questions"],
        roster_tab=a.participation["roster_tab"],
        roster_name_col=a.participation["roster_name_col"],
        roster_alias_col=a.participation["roster_alias_col"],
        roster_start_row=a.participation["roster_start_row"],
    )

    # Persist the log channel to guild_configs so storm_log.py can read it
    if w.event_type == "DS":
        update_config_field(w.guild_id, "ds_log_channel_id", a.log_channel_id)
    else:
        update_config_field(w.guild_id, "cs_log_channel_id", a.log_channel_id)

    # Persist the structured-flow config (#38 + #54) against the (guild,
    # event_type) row save_storm_config just created/updated above. The
    # registration-post schedule is set in #124; left blank here.
    # Roster DM templates (#226 follow-up) — stored on the same
    # guild_storm_config row but written via a separate helper so
    # save_structured_storm_config's signature stays focused. Empty
    # strings persist as "fall back to the hardcoded default at send
    # time."
    sc = a.structured
    save_roster_dm_templates(
        w.guild_id,
        w.event_type,
        starter=sc.get("roster_dm_starter_template", ""),
        paired_sub=sc.get("roster_dm_paired_sub_template", ""),
        pool_sub=sc.get("roster_dm_pool_sub_template", ""),
    )
    save_structured_storm_config(
        w.guild_id,
        w.event_type,
        structured_flow_enabled=sc["structured_flow_enabled"],
        power_metric_column=sc.get("power_metric_column", "B"),
        power_metric_tab=sc.get("power_metric_tab", ""),
        power_match_column=sc.get("power_match_column", ""),
        sub_mode=sc["sub_mode"],
        signup_channel_id=sc["signup_channel_id"],
        signup_schedule_cron=sc.get("signup_schedule_cron", ""),
        signups_tab=sc["signups_tab"],
        rosters_tab=sc["rosters_tab"],
        attendance_tab=sc["attendance_tab"],
        strategies_tab=sc["strategies_tab"],
        member_rules_tab=sc["member_rules_tab"],
        poll_day_of_week=sc.get("poll_day_of_week", -1),
        signup_time=sc.get("signup_time", ""),
        power_refresh_dm_enabled=bool(sc.get("power_refresh_dm_enabled", False)),
        power_last_updated_tab=sc.get("power_last_updated_tab", ""),
        power_last_updated_column=sc.get("power_last_updated_column", ""),
        power_last_updated_match_column=sc.get("power_last_updated_match_column", ""),
        power_refresh_stale_days=int(sc.get("power_refresh_stale_days", 0)),
    )


def _summary_embed(w: _Wizard, s: _Saved, a: _Answers) -> discord.Embed:
    embed = discord.Embed(title=f"✅ {w.label} Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab", value=a.tab_name, inline=True)
    embed.add_field(name="Teams", value=_TEAM_SHORT[a.teams], inline=True)

    # Team time-slot mapping (#251) — surfaced inline alongside the
    # other event-shape fields so officers can confirm their slot picks
    # made it through the wizard.
    def _slot_lbl(idx):
        if idx in (1, 2) and len(a.slot_labels) >= idx:
            return a.slot_labels[idx - 1]
        return "—"

    if a.teams == "A":
        embed.add_field(name="Team Times", value=f"A: {_slot_lbl(a.team_a_slot)}", inline=False)
    elif a.teams == "B":
        embed.add_field(name="Team Times", value=f"B: {_slot_lbl(a.team_b_slot)}", inline=False)
    else:
        embed.add_field(
            name="Team Times",
            value=f"A: {_slot_lbl(a.team_a_slot)} · B: {_slot_lbl(a.team_b_slot)}",
            inline=False,
        )
    embed.add_field(name="Timezone", value=s.tz_label, inline=True)
    embed.add_field(name="Log Channel", value=f"<#{a.log_channel_id}>", inline=True)
    embed.add_field(name="Post Channel", value=f"<#{a.post_channel_id}>", inline=True)
    if a.participation["enabled"]:
        n_q = len(a.participation["questions"])
        embed.add_field(
            name="Participation Tracking",
            value=(f"✅ Enabled · {n_q} question(s) · Tab: `{a.participation['tab_name']}`"),
            inline=False,
        )
    else:
        embed.add_field(name="Participation Tracking", value="❌ Disabled", inline=False)
    sc = a.structured
    if sc["structured_flow_enabled"]:
        _pwr_letter = sc.get("power_metric_column", "B")
        _pwr_tab = sc.get("power_metric_tab", "") or ""
        _pwr_match = sc.get("power_match_column", "") or ""
        if _pwr_tab:
            power_blurb = f"Power source: `{_pwr_tab}` · column `{_pwr_letter}`" + (
                f" · matched by `{_pwr_match}`" if _pwr_match else ""
            )
        else:
            power_blurb = f"Power column: `{_pwr_letter}`"
        signup_blurb = (
            f" · Sign-up channel: <#{sc['signup_channel_id']}>" if sc["signup_channel_id"] else ""
        )
        embed.add_field(
            name="Structured Roster Flow",
            value=(f"✅ Enabled · {power_blurb} · Sub mode: `{sc['sub_mode']}`{signup_blurb}"),
            inline=False,
        )
    else:
        embed.add_field(
            name="Structured Roster Flow",
            value=f"❌ Disabled · Preset tabs: `{sc['strategies_tab']}` / `{sc['member_rules_tab']}`",
            inline=False,
        )
    if a.template_a:
        embed.add_field(
            name="Template A Preview",
            value=f"```{a.template_a[:150]}{'...' if len(a.template_a) > 150 else ''}```",
            inline=False,
        )
    if a.template_b and a.template_b != a.template_a:
        embed.add_field(
            name="Template B Preview",
            value=f"```{a.template_b[:150]}{'...' if len(a.template_b) > 150 else ''}```",
            inline=False,
        )
    embed.set_footer(text=f"Run /{w.cmd_name} again to update.")
    return embed


async def _offer_first_signup(w: _Wizard, a: _Answers) -> None:
    """The inline "post first sign-up" offer (#144).

    Fires only when the structured flow is opted in, a sign-up channel
    is configured, and no sign-up post has been recorded yet for this
    guild + event type. Whether auto-scheduling was configured or
    skipped, this gives the alliance one fully-live sign-up post right
    at the end of setup — the discovery surface #144 is closing.
    """
    sc = a.structured
    if not (sc["structured_flow_enabled"] and sc.get("signup_channel_id")):
        return
    try:
        import config as _config

        def _signup_already_posted() -> bool:
            with _config._get_conn() as conn:
                return (
                    conn.execute(
                        "SELECT 1 FROM storm_registration_posts "
                        "WHERE guild_id = ? AND event_type = ? LIMIT 1",
                        (w.guild_id, w.event_type),
                    ).fetchone()
                    is not None
                )

        already_posted = await asyncio.to_thread(_signup_already_posted)
    except Exception:
        already_posted = True  # err on the side of not nagging
    if already_posted:
        return
    post_offer = _setup()._InlinePostFirstSignupOffer(
        owner_id=w.user.id,
        bot=w.bot,
        guild_id=w.guild_id,
        event_type=w.event_type,
        parent=w.parent_cmd,
        label=w.label,
    )
    post_offer.message = await w.channel.send(
        f"📣 Want to post your first {w.label} sign-up now? "
        f"It'll land in <#{sc['signup_channel_id']}> "
        f"with vote buttons members can click. You can also wait "
        f"for the auto-schedule to post it (if you set one up) "
        f"or run `{HUB_COMMAND[w.event_type]}` and click "
        f"**{HUB_BTN_POST_SIGNUP}** later.",
        view=post_offer,
    )
    await wait_view_or_cancel(post_offer, w.cancel_event)


# ── Entry point ──────────────────────────────────────────────────────────────


async def run_storm_setup(interaction: discord.Interaction, bot, event_type: str):
    """Shared setup wizard for Desert Storm and Canyon Storm."""
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    # cmd_name is the user-facing hint shown after `Run /` in timeout /
    # footer messages throughout this wizard. The old `/setup_desertstorm`
    # and `/setup_canyonstorm` slash commands were consolidated under the
    # `/setup` hub (#201); the hint now points officers at the button
    # they actually need to click. For internal slug uses (channel-name
    # suggestion) helpers derive a separate slug from `event_type`.
    storm_button = "⚔️ Desert Storm" if event_type == "DS" else "🛡️ Canyon Storm"
    w = _Wizard(
        interaction=interaction,
        bot=bot,
        event_type=event_type,
        channel=interaction.channel,
        user=interaction.user,
        guild_id=interaction.guild_id,
        label=label,
        cmd_name=f"setup → {storm_button}",
        cancel_event=wizard_registry.register(interaction.user.id),
    )
    saved = _load_saved(w)
    a = _Answers()
    try:
        await _confirm_reentry(w, saved)
        await w.channel.send(f"⚙️ **{label} Setup**")
        w.is_premium = await premium.is_premium(
            w.guild_id, interaction=interaction, bot=interaction.client
        )
        a.tab_name = await _ask_tab(w, saved)
        a.teams = await _ask_teams(w, saved)
        await _ask_team_slots(w, saved, a.teams, a)
        a.log_channel_id = await _ask_log_channel(w, saved)
        a.post_channel_id = await _ask_post_channel(w, saved)
        a.template_a, a.template_b = await _ask_templates(w, saved, a.teams)
        a.participation = await _ask_participation(w, saved)
        a.structured = await _ask_structured(w, saved)
        a.dm_reminder_message = await _ask_reminder_dm(w, saved)
    except _Abort:
        return

    _save(w, saved, a)
    await w.channel.send(embed=_summary_embed(w, saved, a))
    await _offer_first_signup(w, a)

    wizard_registry.unregister(w.user.id, w.cancel_event)
    print(f"[SETUP] {label} config saved for guild {w.guild_id}")

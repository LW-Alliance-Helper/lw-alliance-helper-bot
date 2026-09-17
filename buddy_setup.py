"""
The Profession Buddy System wizard (#289): enable or keep, the buddy and
preset tabs, the opt-out column (#427), the roster filter (#428),
Engineer doubling, scarcity priority, the reliability ranking and its
source (#303), the leadership alerts channel and the buddy DM.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup.py` set: `setup_cog` imports `run_buddy_setup`
back under its old name for the hub, the launcher and the tests; the
shared wizard pieces come from `wizard_steps`; this module reaches into
`setup_cog` at call time (`_setup()`) only for what still lives there
(the re-entry summary, the disable-with-clear helper, the tab-claim
warning).

Shape:
* `_Wizard` carries the handles every question needs, plus `_Saved` (what
  was read before asking) and `_Answers` (what the officer chose).
* One `_ask_*` function per step; the reliability source's two inline
  classes are module classes under their old names.
* A cancelled or timed-out question raises `_Abort`; `run_buddy_setup`
  unregisters the cancel event and returns, the way the old function did
  on every exit but the two abandoned tab questions (which now do too).

Every prompt, label, acknowledgement, saved field and summary line is
unchanged from the function this replaced;
`tests/integration/test_buddy_setup.py` was written against it and holds
this module to it.
"""

import asyncio
from dataclasses import dataclass, field

import discord

import premium
import wizard_registry
import wizard_steps
from messages import PREV_CHANNEL_GONE
from wizard_registry import wait_view_or_cancel
from wizard_steps import WIZARD_STEP_TIMEOUT


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


TIMEOUT_MSG = "⏰ Setup timed out. Run `/setup` → 🤝 Buddy System to start again."
NAV = "setup → 🤝 Buddy System"
DEFAULT_REL_COL = "D"


# ── Shared handles ───────────────────────────────────────────────────────────


@dataclass
class _Wizard:
    interaction: discord.Interaction
    channel: discord.abc.Messageable
    user: discord.abc.User
    guild_id: int
    cancel_event: asyncio.Event

    async def wait(self, view) -> None:
        """Wait for `view`, raising `_Abort` silently if the officer cancelled."""
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
            await self.channel.send(TIMEOUT_MSG)
            raise _Abort
        return value

    async def ask_yes_no(self, text: str) -> bool:
        return bool(await self.ask(text, wizard_steps.YesNoView(), "selected"))

    async def ask_yes_no_lenient(self, text: str) -> bool:
        """The opt-out and roster questions never had a timeout branch: an
        unanswered view reads as No and the walk carries on."""
        view = wizard_steps.YesNoView()
        await self.channel.send(text, view=view)
        await self.wait(view)
        return bool(view.selected)

    async def keep_or_change(self, prompt: str, **kw) -> str:
        """One `ask_keep_or_change` question. The helper posts its own cancel
        and timeout notice; an abandoned one just raises."""
        picked = await wizard_steps.ask_keep_or_change(
            self.channel, prompt, timeout_cmd="setup_buddy", cancel_event=self.cancel_event, **kw
        )
        if picked is None:
            raise _Abort
        return picked


@dataclass
class _Saved:
    current: dict
    already_configured: bool
    profession_tab: str = ""
    profession_col_header: str = ""
    roster_cfg: dict = field(default_factory=dict)
    storm_ds: dict = field(default_factory=dict)


@dataclass
class _Answers:
    buddy_tab: str = ""
    preset_tab: str = ""
    include_col_header: str = ""
    roster_tab_name: str = ""
    roster_filter_enabled: int = 0
    engineer_doubling: int = 0
    scarcity_priority: str = "alphabetical"
    reliability_enabled: int = 0
    reliability_tab: str = ""
    reliability_column: str = ""
    notify_channel_id: int = 0
    dm_enabled: int = 0
    dm_template: str = ""


# ── Views ────────────────────────────────────────────────────────────────────


class _RelModal(discord.ui.Modal):
    """Tab and column for the reliability scores."""

    def __init__(self, *, tab_default: str, col_default: str):
        super().__init__(title="Reliability Source")
        self.out = None
        self._tab = discord.ui.TextInput(label="Tab name", default=tab_default, max_length=100)
        self._col = discord.ui.TextInput(
            label="Reliability score column (letter)", default=col_default, max_length=4
        )
        self.add_item(self._tab)
        self.add_item(self._col)

    async def on_submit(self, inter: discord.Interaction):
        self.out = (self._tab.value.strip(), self._col.value.strip().upper())
        await inter.response.defer()
        self.stop()


class _RelChoiceView(discord.ui.View):
    """Keep current (when a custom source is saved) / Use default / Define
    my own. `choice` is "keep", "default" or "custom" (with `modal_out`)."""

    def __init__(
        self,
        *,
        saved_custom: bool,
        current_is_default: bool,
        default_rel_tab: str,
        modal_tab: str,
        modal_col: str,
    ):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.choice = None
        self.modal_out = None
        self._modal_defaults = (modal_tab, modal_col)
        if saved_custom:
            keep = discord.ui.Button(label="Keep current", style=discord.ButtonStyle.success)
            keep.callback = self._keep
            self.add_item(keep)

        # Spell out the actual defaults when what's saved isn't already them.
        use_def_label = (
            f"↩️ Use default ({default_rel_tab}; {DEFAULT_REL_COL})"
            if saved_custom and not current_is_default
            else "✅ Use default"
        )
        use_def = discord.ui.Button(
            label=use_def_label[:80],
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
        tab_default, col_default = self._modal_defaults
        modal = _RelModal(tab_default=tab_default, col_default=col_default)
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


# ── Re-entry ─────────────────────────────────────────────────────────────────


async def _confirm_reentry(w: _Wizard, s: _Saved) -> None:
    """On an enabled system, show the summary and offer edit or cancel;
    anything but Edit ends the wizard."""
    current = s.current
    if not (s.already_configured and current.get("enabled")):
        return
    notify_id = current.get("notify_channel_id", 0) or 0
    fields = [
        ("Buddy Tab", current.get("buddy_tab") or "Buddy System"),
        ("Preset Tab", current.get("preset_tab") or "Buddy Presets"),
        (
            "Opt-out column",
            f"`{current.get('include_col_header')}`"
            if current.get("include_col_header")
            else "*not set — everyone on the profession tab is eligible*",
        ),
        (
            "Limited to your member roster",
            "✅ Yes" if current.get("roster_filter_enabled") else "❌ No",
        ),
        (
            "Two Engineers per War Leader",
            "✅ Yes" if current.get("engineer_doubling") else "❌ No",
        ),
        (
            "When Engineers are scarce",
            "Strongest War Leaders first"
            if current.get("scarcity_priority") == "strongest_first"
            else "Alphabetical",
        ),
        (
            "Rank Engineers by reliability",
            "✅ Yes" if current.get("reliability_enabled") else "❌ No",
        ),
        ("Leadership alerts", f"<#{notify_id}>" if notify_id else "*off*"),
        ("Buddy DMs", "✅ Yes" if current.get("dm_enabled") else "❌ No"),
    ]
    proceed = await _setup().ask_proceed_with_existing_config(
        w.channel,
        title="🤝 Current Buddy System Setup",
        description="The Profession Buddy System is already on. Would you like to edit these settings?",
        fields=fields,
        cancel_event=w.cancel_event,
        no_changes_message="✅ No changes made. The Buddy System is still active.",
    )
    if proceed is not True:
        raise _Abort


# ── The questions ────────────────────────────────────────────────────────────


async def _ask_enable(w: _Wizard, s: _Saved) -> None:
    """Step 1. A No turns the system off (offering to clear a prior
    config) and ends the wizard."""
    from config import update_buddy_config_field, clear_buddy_config

    if await w.ask_yes_no("**Step 1 of 10 — Turn on the Profession Buddy System?**"):
        return
    update_buddy_config_field(w.guild_id, "enabled", 0)
    await _setup().ask_disable_with_clear(
        w.channel,
        feature_label="Profession Buddy System",
        setup_command=NAV,
        had_prior_config=s.already_configured,
        clear_fn=lambda: clear_buddy_config(w.guild_id),
        cancel_event=w.cancel_event,
    )
    raise _Abort


async def _detect_profession_source(w: _Wizard, s: _Saved) -> None:
    """Profession is read from the Squad Powers survey question, when there
    is one; the wizard says which way it went."""
    from config import get_survey_config

    survey_cfg = get_survey_config(w.guild_id) or {}
    questions = survey_cfg.get("questions") or []
    prof_q = next(
        (q for q in questions if (q.get("key") or "").lower() == "profession"),
        None,
    )
    s.profession_tab = survey_cfg.get("tab_squad_powers") or "Squad Powers"
    if prof_q:
        s.profession_col_header = prof_q.get("label") or "Profession"
        await w.channel.send(
            f"✅ Found your **{s.profession_col_header}** survey question writing to the "
            f"**{s.profession_tab}** tab. The Buddy System will read professions from there."
        )
    else:
        s.profession_col_header = "Profession"
        await w.channel.send(
            "⚠️ I couldn't find a **Profession** question in your Squad Power Survey. "
            "Members won't be able to self-report War Leader / Engineer until you add a "
            "dropdown question with the key `profession` (options: War Leader, Engineer) "
            "via `/setup` → 📋 Survey. You can still finish this setup and pair people "
            "manually in the meantime."
        )


async def _ask_tabs(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 2 and 3: the buddy list tab and the presets tab."""
    a.buddy_tab = await w.keep_or_change(
        "**Step 2 of 10 — Buddy List Tab**\n"
        "Which tab in your Google Sheet should hold the buddy list? The bot owns "
        "this tab and rebuilds it (one row per War Leader, Engineers alongside).\n"
        "⚠️ *The bot will create it if it doesn't exist.*",
        default="Buddy System",
        current=s.current.get("buddy_tab", ""),
        modal_title="Buddy Tab Name",
        modal_label="Tab name",
    )
    await _setup().warn_if_tab_claimed(
        w.channel, w.guild_id, a.buddy_tab, exclude_field="buddy_tab"
    )

    # Presets live on the alliance's own sheet rather than in our database, so
    # where they live is theirs to name.
    a.preset_tab = await w.keep_or_change(
        "**Step 3 of 10 — Buddy Presets Tab**\n"
        "Which tab should hold your saved pairing presets? Saving a lineup writes it "
        "here, so your presets stay on your own sheet.\n"
        "⚠️ *The bot will create it if it doesn't exist.*",
        default="Buddy Presets",
        current=s.current.get("preset_tab", ""),
        modal_title="Buddy Presets Tab Name",
        modal_label="Tab name",
    )
    await _setup().warn_if_tab_claimed(
        w.channel, w.guild_id, a.preset_tab, exclude_field="preset_tab"
    )


async def _ask_opt_out_column(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 4 (#427). Optional. Blank keeps every row on the profession tab
    eligible, which is the pre-#427 behaviour and what every existing
    alliance gets on upgrade."""
    wants = await w.ask_yes_no_lenient(
        "**Step 4 of 10 — Leave people out of the buddy list?**\n"
        f"Some alliances keep members on the **{s.profession_tab}** tab after they leave, "
        "so the historical record stays intact. Others have members who just don't want "
        "a buddy.\n\n"
        f"Do you want a column on **{s.profession_tab}** that takes someone out of the buddy "
        "list without touching the rest of their row?"
    )
    if not wants:
        return
    header = await w.keep_or_change(
        "**Step 4a of 10 — Which column?**\n"
        f"Type the **header name** exactly as it appears in row 1 of **{s.profession_tab}** "
        "(not the column letter, so you can move it later without breaking anything).\n\n"
        "The bot leaves someone out when their cell reads **no**, **false**, **0**, "
        "**off**, **left** or **exclude**. Blank cells stay in the list, so you only "
        "have to fill it in for the people you're taking out.",
        default="In Buddy System",
        current=s.current.get("include_col_header", ""),
        modal_title="Opt-out Column Header",
        modal_label="Column header name",
    )
    a.include_col_header = (header or "").strip()


async def _ask_roster_filter(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 5 (#428). Shares the one roster config with Conductor Rotation,
    so `enabled` is what splits synced-Premium from hand-maintained free.
    Opt-in, and gated on the alliance actually having a roster configured."""
    from config import get_member_roster_config, has_member_roster_config

    roster_cfg = get_member_roster_config(w.guild_id)
    a.roster_tab_name = roster_cfg.get("tab_name") or "Member Roster"
    roster_synced = bool(roster_cfg.get("enabled"))

    if not has_member_roster_config(w.guild_id):
        await w.channel.send(
            "**Step 5 of 10 — Keep the list in step with your roster** *(skipped)*\n"
            "You haven't pointed the bot at a member roster yet. Set one up via `/setup` → "
            "👥 Member Sync (or 🚂 Train → Conductor Rotation) and re-run this wizard to have "
            "departures drop out of the buddy list on their own."
        )
        return
    if roster_synced:
        blurb = (
            f"Your **{a.roster_tab_name}** tab is synced from Discord 💎, so it drops people "
            "automatically when they leave. Point the buddy list at it and departures "
            "leave the pairing list on their own, with nothing to edit."
        )
    else:
        blurb = (
            f"Your **{a.roster_tab_name}** tab is one you maintain by hand. Point the buddy "
            "list at it and removing someone there also removes them here, so it's one "
            "edit in one place instead of two.\n"
            "💎 *With Member Sync, that tab keeps itself current and departures drop out "
            "with no edit at all.*"
        )
    wants = await w.ask_yes_no_lenient(
        f"**Step 5 of 10 — Keep the list in step with your roster?**\n{blurb}\n\n"
        "Only people on that tab would be eligible for a buddy."
    )
    a.roster_filter_enabled = 1 if wants else 0


async def _ask_engineer_doubling(w: _Wizard, a: _Answers) -> None:
    """Step 6."""
    wants = await w.ask_yes_no(
        "**Step 6 of 10 — Two Engineers per War Leader?**\n"
        "When you have more Engineers than War Leaders, should we allow War Leaders "
        "to have 2 Engineers paired with them?"
    )
    a.engineer_doubling = 1 if wants else 0


async def _ask_scarcity_priority(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 7. Strongest-first reads power from the alliance's existing Power
    Data Source (Member Sync roster, or the storm Power Data Source). If
    neither is set up there's no power to read, so the question is skipped
    and the order stays alphabetical rather than offering a setting that
    can't do anything (#289 test note 4)."""
    from config import get_member_roster_config, get_storm_config

    s.roster_cfg = get_member_roster_config(w.guild_id) or {}
    s.storm_ds = get_storm_config(w.guild_id, "DS") or {}
    power_source_available = (
        bool(s.roster_cfg.get("enabled"))
        or bool((s.storm_ds.get("power_metric_tab") or "").strip())
        or bool(s.storm_ds.get("structured_flow_enabled"))
    )
    if not power_source_available:
        await w.channel.send(
            "ℹ️ *Skipping the strongest-first option: pairing the strongest War Leaders "
            "first needs a Power data source. Set one up via Member Sync or Storm setup "
            "and re-run this wizard to enable it. Using alphabetical order for now.*"
        )
        return
    wants = await w.ask_yes_no(
        "**Step 7 of 10 — When Engineers are scarce**\n"
        "If you have more War Leaders than Engineers, should we prioritize your "
        "strongest War Leaders first? (Note that this will read from your existing "
        "Power data source if you have one set up.)"
    )
    a.scarcity_priority = "strongest_first" if wants else "alphabetical"


async def _ask_reliability(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 8 and 8a (#303). When on, the bot reads a 1-5 score the alliance
    maintains in their Sheet and orders Engineers most-reliable-first."""
    a.reliability_tab = s.current.get("reliability_tab", "") or ""
    a.reliability_column = s.current.get("reliability_column", "") or ""

    wants = await w.ask_yes_no(
        "**Step 8 of 10 — Rank Engineers by reliability?**\n"
        "Keep a 1-5 reliability score for your Engineers somewhere in your Google "
        "Sheet (higher = more dependable) and the bot will pair your most reliable "
        "Engineers with your top War Leaders. Leave this off to order Engineers "
        "alphabetically. You maintain the scores; the bot only reads them."
    )
    a.reliability_enabled = 1 if wants else 0
    if not a.reliability_enabled:
        return

    # Default tab mirrors the Power Data Source (then Member Roster); default
    # score column is a placeholder "D" so leadership picks their real column.
    # Matching reuses the Power Data Source match column (read_reliability_for_members).
    default_rel_tab = (
        (s.storm_ds.get("power_metric_tab") or "").strip()
        or (s.roster_cfg.get("tab_name") or "").strip()
        or "Member Roster"
    )
    saved_custom = bool(a.reliability_tab and a.reliability_column)
    # Fall back to the defaults for display when nothing is saved yet (mirrors
    # how the rotation Sheet Tabs step renders).
    disp_tab = a.reliability_tab or default_rel_tab
    disp_col = a.reliability_column or DEFAULT_REL_COL
    current_is_default = (disp_tab, disp_col) == (default_rel_tab, DEFAULT_REL_COL)

    def _rel_line(label, val, default):
        return f"{label}: **{val}**" + (" (default)" if val == default else "")

    view = _RelChoiceView(
        saved_custom=saved_custom,
        current_is_default=current_is_default,
        default_rel_tab=default_rel_tab,
        modal_tab=a.reliability_tab or default_rel_tab,
        modal_col=a.reliability_column or DEFAULT_REL_COL,
    )
    choice = await w.ask(
        "**Step 8a of 10 — Where are your reliability scores?**\n"
        "The bot reads each Engineer's 1-5 score from here (it never writes to it) "
        "and matches members the same way it reads power:\n"
        f"{_rel_line('Tab name', disp_tab, default_rel_tab)}\n"
        f"{_rel_line('Column', disp_col, DEFAULT_REL_COL)}\n\n"
        "Keep these, or define your own?",
        view,
        "choice",
    )
    if choice == "default":
        a.reliability_tab, a.reliability_column = default_rel_tab, DEFAULT_REL_COL
    elif choice == "custom":
        a.reliability_tab, a.reliability_column = view.modal_out
    # "keep" leaves the loaded values as-is.

    if not a.reliability_tab or not a.reliability_column:
        a.reliability_enabled = 0
        await w.channel.send(
            "⚠️ I need both a tab and a column letter to read reliability. Leaving it "
            "off for now — re-run setup to set it. Using alphabetical Engineer order."
        )


async def _ask_alerts_channel(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 9."""
    is_premium_flag = await premium.is_premium(
        w.guild_id, interaction=w.interaction, bot=w.interaction.client
    )
    wants = await w.ask_yes_no(
        "**Step 9 of 10 — Leadership alerts**\n"
        "When a member swaps profession and the bot re-pairs people, should it post a "
        "heads-up to a leadership channel?\n"
        "💎 Premium: these posts send only while Premium is active."
    )
    if not wants:
        return
    saved_notify = s.current.get("notify_channel_id", 0) or 0
    view = wizard_steps.ChannelSelectStep(
        "Select the leadership alerts channel...",
        suggested_name="leadership",
        include_threads=is_premium_flag,
        guild=w.interaction.guild,
        current_id=saved_notify,
    )
    if view.is_current_stale:
        await w.channel.send(PREV_CHANNEL_GONE.format(channel_label="leadership alerts"))
    await w.channel.send("​", view=view)
    await w.wait(view)
    if not view.confirmed:
        await w.channel.send(TIMEOUT_MSG)
        raise _Abort
    a.notify_channel_id = view.selected_channel.id


async def _ask_dms(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 10, and the DM body when DMs are on."""
    from defaults import DEFAULT_BUDDY_DM

    wants = await w.ask_yes_no(
        "**Step 10 of 10 — Buddy DMs**\n"
        "Should the bot DM members their buddy when it changes?\n"
        "💎 Premium: these DMs send only while Premium is active."
    )
    a.dm_enabled = 1 if wants else 0
    a.dm_template = s.current.get("dm_template", "") or ""
    if not a.dm_enabled:
        return
    dm_input = await w.keep_or_change(
        "**Buddy DM message**\n"
        "What should the buddy DM say? Placeholders: `{name}` (the recipient), "
        "`{buddy}` (their buddy), `{buddy_role}` (War Leader / Engineer).",
        default=DEFAULT_BUDDY_DM,
        current=s.current.get("dm_template", ""),
        modal_title="Buddy DM message",
        modal_label="DM body",
    )
    # Store empty when it matches the default, so the default can evolve.
    a.dm_template = "" if dm_input == DEFAULT_BUDDY_DM else dm_input


# ── Save and summary ─────────────────────────────────────────────────────────


def _save(w: _Wizard, s: _Saved, a: _Answers) -> None:
    from config import update_buddy_config_field

    for field_name, value in (
        ("enabled", 1),
        ("buddy_tab", a.buddy_tab),
        ("preset_tab", a.preset_tab),
        ("profession_tab", s.profession_tab),
        ("profession_col_header", s.profession_col_header),
        ("include_col_header", a.include_col_header),
        ("roster_filter_enabled", a.roster_filter_enabled),
        ("engineer_doubling", a.engineer_doubling),
        ("scarcity_priority", a.scarcity_priority),
        ("reliability_enabled", a.reliability_enabled),
        ("reliability_tab", a.reliability_tab),
        ("reliability_column", a.reliability_column),
        ("notify_channel_id", a.notify_channel_id),
        ("dm_enabled", a.dm_enabled),
        ("dm_template", a.dm_template),
    ):
        update_buddy_config_field(w.guild_id, field_name, value)


def _summary_embed(s: _Saved, a: _Answers) -> discord.Embed:
    summary = discord.Embed(
        title="🤝 Buddy System configured",
        color=discord.Color.green(),
        description=(
            f"**Buddy tab:** {a.buddy_tab}\n"
            f"**Preset tab:** {a.preset_tab}\n"
            f"**Opt-out column:** "
            f"{f'`{a.include_col_header}` on {s.profession_tab}' if a.include_col_header else 'not set'}\n"
            f"**Limited to your member roster:** "
            f"{f'✅ Yes ({a.roster_tab_name})' if a.roster_filter_enabled else '❌ No'}\n"
            f"**Two Engineers per War Leader:** {'✅ Yes' if a.engineer_doubling else '❌ No'}\n"
            f"**When Engineers are scarce:** "
            f"{'strongest first' if a.scarcity_priority == 'strongest_first' else 'alphabetical'}\n"
            f"**Rank Engineers by reliability:** "
            f"{f'✅ Yes (column {a.reliability_column} on {a.reliability_tab})' if a.reliability_enabled else '❌ No'}\n"
            f"**Leadership alerts:** {f'<#{a.notify_channel_id}>' if a.notify_channel_id else 'off'}\n"
            f"**Buddy DMs:** {'✅ Yes' if a.dm_enabled else '❌ No'}"
            f"{' (custom message)' if (a.dm_enabled and a.dm_template) else ''}"
        ),
    )
    summary.set_footer(text="Open /buddy to view the list, pair members, or auto-assign.")
    return summary


# ── Entry point ──────────────────────────────────────────────────────────────


async def run_buddy_setup(interaction: discord.Interaction, bot):
    """Walk leadership through configuring the Profession Buddy System (#289).

    Enable → buddy tab → Engineer doubling → scarcity priority → reliability
    ranking → leadership alerts channel → buddy DMs. Profession is detected from the Squad Powers
    survey question. Free to enable + manually pair; auto-assign, one-click
    profession buttons, alerts, and DMs are Premium at runtime."""
    from config import get_buddy_config, has_buddy_config

    w = _Wizard(
        interaction=interaction,
        channel=interaction.channel,
        user=interaction.user,
        guild_id=interaction.guild_id,
        cancel_event=wizard_registry.register(interaction.user.id),
    )
    s = _Saved(
        current=get_buddy_config(w.guild_id),
        already_configured=has_buddy_config(w.guild_id),
    )
    a = _Answers()
    try:
        await _confirm_reentry(w, s)
        await w.channel.send(
            "🤝 **Profession Buddy System Setup**\n"
            "Pair your War Leaders with Engineers so the daily buff Skill always has a home."
        )
        await _ask_enable(w, s)
        await _detect_profession_source(w, s)
        await _ask_tabs(w, s, a)
        await _ask_opt_out_column(w, s, a)
        await _ask_roster_filter(w, s, a)
        await _ask_engineer_doubling(w, a)
        await _ask_scarcity_priority(w, s, a)
        await _ask_reliability(w, s, a)
        await _ask_alerts_channel(w, s, a)
        await _ask_dms(w, s, a)
    except _Abort:
        wizard_registry.unregister(w.user.id, w.cancel_event)
        return

    _save(w, s, a)
    await w.channel.send(embed=_summary_embed(s, a))
    wizard_registry.unregister(w.user.id, w.cancel_event)
    print(f"[SETUP] Buddy System enabled for guild {w.guild_id}")

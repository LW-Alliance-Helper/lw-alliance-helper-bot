"""
The survey wizard (`run_survey_setup`): the re-entry summary, the survey
and notification channels, the survey's own pair of sheet tabs, the intro
message, the Step 6 question choice and the question builder, the save,
the header seeding and the confirmation embed with its Post / Edit
buttons. Also the flows around it that the `/survey` hub dispatches into:
adding a named survey (`run_create_new_extra_survey`), picking one to
edit (`run_pick_survey_to_edit`) and removing one
(`run_remove_extra_survey`).

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup.py` set: `setup_cog` imports the entry points
back under their old names for the hub, the launcher and the tests; the
shared wizard pieces come from `wizard_steps`; this module reaches into
`setup_cog` at call time (`_setup()`) only for what still lives there
(the re-entry summary, the tab-claim warning, the Premium answer types
and the free-tier note that names them).

Shape:
* `_Wizard` carries the handles every question needs, plus `_Saved` (what
  was read before asking) and `_Answers` (what the officer chose).
* One `_ask_*` function per step; the question builder is a loop over
  `QuestionListView` with one function per question part and the
  per-type extras dispatched from `_TYPE_EXTRAS`, the way the storm
  participation builder is laid out.
* The inline views are module classes under their old names.
* A cancelled or timed-out question raises `_Abort`; `run_survey_setup`
  unregisters the cancel event and returns, the way the old function did
  on its clean exits (it left the event registered on most aborts).

Every prompt, label, acknowledgement, saved field and summary line is
unchanged from the function this replaced;
`tests/integration/test_survey_setup.py` was written against it and holds
this module to it.
"""

import asyncio
import re
from dataclasses import dataclass, field

import discord

import premium
import wizard_registry
import wizard_steps
from messages import CANCEL_PLAIN, PREV_CHANNEL_GONE, WIZARD_TIMEOUT
from setup_hub import HUB_BTN_SURVEY
from wizard_registry import OwnedView, wait_view_or_cancel
from wizard_steps import WIZARD_STEP_TIMEOUT, TextInputModal


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


TIMEOUT_MSG = WIZARD_TIMEOUT.format(wizard=HUB_BTN_SURVEY)
RETRY_HINT = "Run `/setup` → 📋 Survey to try again."

_TYPE_PRETTY = {
    "text": "Text",
    "dropdown": "Dropdown",
    "numeric": "Numeric",
    "multi_select": "Multi-Select",
    "date": "Date",
}

_MAGNITUDE_PRETTY = {
    "raw": "Exact number",
    "K": "Thousands (K)",
    "M": "Millions (M)",
    "B": "Billions (B)",
}

_MAGNITUDE_OPTIONS = [
    ("Exact number — type what you mean (e.g. drone level 150 stays 150)", "raw"),
    ("Thousands (K) — 5 becomes 5,000", "K"),
    ("Millions (M) — 301 becomes 301,000,000", "M"),
    ("Billions (B) — 1.2 becomes 1,200,000,000", "B"),
]

_FREE_TYPE_OPTIONS = [
    ("🔤 Text — member types their answer", "text"),
    ("🔽 Dropdown — member selects from a list", "dropdown"),
    ("🔢 Numeric — number, with shorthand support", "numeric"),
]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "survey"


# ── Shared handles ───────────────────────────────────────────────────────────


@dataclass
class _Wizard:
    interaction: discord.Interaction
    bot: object
    channel: discord.abc.Messageable
    user: discord.abc.User
    guild_id: int
    cancel_event: asyncio.Event
    is_premium: bool = False

    def _check(self, m) -> bool:
        return m.author == self.user and m.channel == self.channel

    async def wait(self, view) -> None:
        """Wait for `view`, raising `_Abort` silently if the officer cancelled."""
        await wait_view_or_cancel(view, self.cancel_event)
        if getattr(view, "cancelled", False):
            raise _Abort

    async def ask(self, text: str, view, attr: str):
        """Post `text` with `view`, wait, and return `view.<attr>`; a timeout
        (the attribute still falsy) posts the route back and raises."""
        await self.channel.send(text, view=view)
        await self.wait(view)
        value = getattr(view, attr)
        if not value:
            await self.channel.send(TIMEOUT_MSG)
            raise _Abort
        return value

    async def reply(self, seconds: int) -> str:
        """One free-text reply from the officer. A timeout posts the route
        back and raises; `/cancel` is not raced here, as it never was in
        the builder."""
        try:
            msg = await self.bot.wait_for("message", check=self._check, timeout=seconds)
        except asyncio.TimeoutError:
            await self.channel.send(TIMEOUT_MSG)
            raise _Abort
        return msg.content.strip()

    async def reply_or_cancel(self, seconds: int) -> str:
        """The intro reply: raced against `/cancel`, which posts the plain
        cancel line; a timeout posts the route back."""
        msg = await wizard_registry.wait_or_cancel(
            self.bot.wait_for("message", check=self._check, timeout=seconds),
            self.cancel_event,
        )
        if msg is None:
            if self.cancel_event.is_set():
                await self.channel.send(CANCEL_PLAIN)
            else:
                await self.channel.send(TIMEOUT_MSG)
            raise _Abort
        return msg.content.strip()


@dataclass
class _Saved:
    target_survey_id: str | None
    target_survey_name: str | None
    current: dict
    already_configured: bool
    wizard_label: str
    saved_survey_ch: int
    saved_notify_ch: int
    template_key: str
    tpl: dict
    tpl_tab_responses: str
    tpl_tab_history: str
    claimed_tabs: dict
    questions: list


@dataclass
class _Answers:
    survey_channel_id: int = 0
    notify_channel_id: int = 0
    tab_squad_powers: str = ""
    tab_history: str = ""
    intro_message: str = ""
    questions: list = field(default_factory=list)


# ── Views ────────────────────────────────────────────────────────────────────


async def _disable_and_stop(view, inter: discord.Interaction, **edit) -> None:
    for item in view.children:
        item.disabled = True
    await wizard_registry.safe_edit_response(inter, view=view, **edit)
    view.stop()


class IntroChoiceView(discord.ui.View):
    """Step 5 when an intro is on offer: keep it, or type a new one."""

    def __init__(self):
        super().__init__(timeout=120)
        # Use a distinct attribute name so this view doesn't collide with
        # QuestionStartView's `choice` field when send-handlers in tests
        # broadcast view overrides.
        self.intro_choice = None

    @discord.ui.button(label="Keep current", style=discord.ButtonStyle.success)
    async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
        self.intro_choice = "keep"
        await _disable_and_stop(self, inter)

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary)
    async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
        self.intro_choice = "edit"
        await _disable_and_stop(self, inter)


class QuestionStartView(discord.ui.View):
    """Step 6: which question set to start from. Built explicitly rather
    than with decorators so the options can vary — decorator buttons
    can't be conditionally added."""

    def __init__(self, *, has_template_qs: bool, has_existing_qs: bool, template_name: str):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.choice = None

        if has_template_qs:
            self._add(
                "✅ Use these questions",
                discord.ButtonStyle.success,
                "template_asis",
                f"✅ Using the {template_name} questions.",
            )
            self._add(
                "✏️ Edit these questions",
                discord.ButtonStyle.primary,
                "template_edit",
                "✏️ Loading the template's questions so you can edit them...",
            )
        if has_existing_qs:
            # Both edit buttons can be on screen at once when a template
            # survey is re-run. The labels mirror the two headings in the
            # message body so it's clear which set each one opens.
            self._add(
                "✏️ Edit this survey's questions",
                discord.ButtonStyle.primary,
                "edit",
                "✏️ Entering edit mode...",
            )
        self._add(
            "♻️ Start from scratch",
            discord.ButtonStyle.secondary,
            "scratch",
            "🔄 Starting from scratch...",
        )

    def _add(self, label, style, choice, ack):
        btn = discord.ui.Button(label=label[:80], style=style)

        async def _cb(inter: discord.Interaction):
            self.choice = choice
            await _disable_and_stop(self, inter, content=ack)

        btn.callback = _cb
        self.add_item(btn)


class QuestionListView(discord.ui.View):
    """The builder's list: edit and delete pickers when there are
    questions, then Add and Finish."""

    def __init__(self, questions: list):
        super().__init__(timeout=300)
        self.action = None
        self.edit_index = None
        self.del_index = None

        if questions:
            edit_select = discord.ui.Select(
                placeholder="✏️ Edit a question...",
                options=[
                    discord.SelectOption(label=f"Edit: {q['label']}", value=str(i))
                    for i, q in enumerate(questions)
                ],
                row=0,
            )

            async def _edit_cb(inter: discord.Interaction):
                self.action = "edit"
                self.edit_index = int(edit_select.values[0])
                await _disable_and_stop(self, inter)

            edit_select.callback = _edit_cb
            self.add_item(edit_select)

            del_select = discord.ui.Select(
                placeholder="🗑️ Delete a question...",
                options=[
                    discord.SelectOption(label=f"Delete: {q['label']}", value=str(i))
                    for i, q in enumerate(questions)
                ],
                row=1,
            )

            async def _del_cb(inter: discord.Interaction):
                self.action = "delete"
                self.del_index = int(del_select.values[0])
                await _disable_and_stop(self, inter)

            del_select.callback = _del_cb
            self.add_item(del_select)

    @discord.ui.button(label="➕ Add Question", style=discord.ButtonStyle.primary, row=2)
    async def add_q(self, inter: discord.Interaction, button: discord.ui.Button):
        self.action = "add"
        await _disable_and_stop(self, inter)

    @discord.ui.button(label="✅ Finish Survey Setup", style=discord.ButtonStyle.success, row=2)
    async def finish(self, inter: discord.Interaction, button: discord.ui.Button):
        self.action = "finish"
        await _disable_and_stop(self, inter)


class TypeView(discord.ui.View):
    """The answer-type picker; the Premium types are offered only to
    Premium guilds."""

    def __init__(self, type_options: list[discord.SelectOption]):
        super().__init__(timeout=120)
        self.selected = None
        select = discord.ui.Select(placeholder="Select answer type...", options=type_options)

        async def _cb(inter: discord.Interaction):
            self.selected = select.values[0]
            select.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Type: **{_TYPE_PRETTY.get(self.selected, self.selected)}**",
                view=self,
            )
            self.stop()

        select.callback = _cb
        self.add_item(select)


class MagnitudeView(discord.ui.View):
    """A numeric question's scale: what bare shorthand like `301` means."""

    def __init__(self):
        super().__init__(timeout=120)
        self.selected = None
        select = discord.ui.Select(
            placeholder="Select number scale...",
            options=[
                discord.SelectOption(label=label, value=value)
                for label, value in _MAGNITUDE_OPTIONS
            ],
        )

        async def _cb(inter: discord.Interaction):
            self.selected = select.values[0]
            select.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Scale: **{_MAGNITUDE_PRETTY.get(self.selected, self.selected)}**",
                view=self,
            )
            self.stop()

        select.callback = _cb
        self.add_item(select)


# ── Loading ──────────────────────────────────────────────────────────────────


def _load(guild_id: int, target_survey_id, target_survey_name, template) -> _Saved:
    """Read what the wizard asks about before it asks: the survey row, its
    channels, the template it speaks in and the tab names it suggests."""
    from config import (
        get_survey_config,
        get_survey,
        has_survey_config,
        get_config,
        survey_tabs_in_use,
    )
    from defaults import derive_survey_tab_names, survey_template

    if target_survey_id is None:
        current = get_survey_config(guild_id)
        wizard_label = "Survey Setup"
        already_configured = has_survey_config(guild_id)
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
        already_configured = bool(current)
        # Extra surveys: channel ids are stored alongside the survey row.
        saved_survey_ch = current.get("survey_channel_id") or 0
        saved_notify_ch = current.get("notify_channel_id") or 0

    # An explicit template wins (creating a survey); otherwise fall back to
    # whatever this survey was built from (re-editing one).
    template_key = template or current.get("template") or "squad_power"
    tpl = survey_template(template_key)

    # Tab names this survey should suggest. A template supplies its own;
    # anything else derives both from the survey name so the suggestion
    # is unique to this survey and can't collide with another one's.
    tpl_tab_responses = tpl.get("tab_responses")
    tpl_tab_history = tpl.get("tab_history")
    if not (tpl_tab_responses and tpl_tab_history):
        derived_responses, derived_history = derive_survey_tab_names(target_survey_name or "Survey")
        tpl_tab_responses = tpl_tab_responses or derived_responses
        tpl_tab_history = tpl_tab_history or derived_history

    # A template's suggested tab names are shared by every guild using
    # that template, so the second squad-power survey in a guild would be
    # handed a name the first one already owns. Fall back to name-derived
    # suggestions rather than defaulting leadership into a rejection.
    claimed_tabs = survey_tabs_in_use(guild_id, exclude_survey_id=(target_survey_id or "default"))
    if tpl_tab_responses.casefold() in claimed_tabs or tpl_tab_history.casefold() in claimed_tabs:
        tpl_tab_responses, tpl_tab_history = derive_survey_tab_names(target_survey_name or "Survey")

    return _Saved(
        target_survey_id=target_survey_id,
        target_survey_name=target_survey_name,
        current=current,
        already_configured=already_configured,
        wizard_label=wizard_label,
        saved_survey_ch=saved_survey_ch,
        saved_notify_ch=saved_notify_ch,
        template_key=template_key,
        tpl=tpl,
        tpl_tab_responses=tpl_tab_responses,
        tpl_tab_history=tpl_tab_history,
        claimed_tabs=claimed_tabs,
        questions=list(current.get("questions") or []),
    )


# ── Steps ────────────────────────────────────────────────────────────────────


async def _confirm_reentry(w: _Wizard, s: _Saved) -> None:
    """If already configured, show the summary and offer edit or cancel."""
    if not s.already_configured:
        return
    q_count = len(s.questions)
    fields = [
        ("Survey Channel", f"<#{s.saved_survey_ch}>" if s.saved_survey_ch else "*not set*"),
        ("Notification Channel", f"<#{s.saved_notify_ch}>" if s.saved_notify_ch else "*not set*"),
        (s.tpl["responses_step_label"], s.current.get("tab_squad_powers") or "*not set*"),
        (s.tpl["history_step_label"], s.current.get("tab_history") or "*not set*"),
        ("Questions", f"{q_count} configured" if q_count else "*none*"),
    ]
    title = (
        f"📋 Current Survey Setup — {s.target_survey_name}"
        if s.target_survey_id
        else "📋 Current Survey Setup"
    )
    proceed = await _setup().ask_proceed_with_existing_config(
        w.channel,
        title=title,
        description="This survey is already configured. Would you like to edit these settings?",
        fields=fields,
        cancel_event=w.cancel_event,
        no_changes_message="✅ No changes made. Your survey setup is still active.",
    )
    if proceed is not True:
        raise _Abort


async def _ask_channel(
    w: _Wizard, *, step: str, label: str, placeholder: str, suggested: str, current_id: int
) -> int:
    view = wizard_steps.ChannelSelectStep(
        placeholder,
        suggested_name=suggested,
        include_threads=w.is_premium,
        guild=w.interaction.guild,
        current_id=current_id,
    )
    if view.is_current_stale:
        await w.channel.send(PREV_CHANNEL_GONE.format(channel_label=label))
    await w.channel.send(step, view=view)
    await w.wait(view)
    if not view.confirmed:
        await w.channel.send(TIMEOUT_MSG)
        raise _Abort
    return view.selected_channel.id


async def _ask_channels(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 1 and 2: the survey channel and the notification channel."""
    # Names offered by the "➕ Create a new channel" button. A named survey
    # suggests channels named after itself; the guild's main survey keeps
    # the names it has always suggested.
    if s.target_survey_id and s.target_survey_name:
        slug = _slug(s.target_survey_name)
        suggested_survey_channel = slug[:90]
        suggested_notify_channel = f"{slug[:80]}-responses"
    else:
        suggested_survey_channel = "squad-survey"
        suggested_notify_channel = "survey-responses"

    a.survey_channel_id = await _ask_channel(
        w,
        step=(
            "**Step 1 of 6 — Survey Channel**\n"
            "Select the channel where the survey button will be posted for members to access:"
        ),
        label="survey",
        placeholder="Select the survey channel...",
        suggested=suggested_survey_channel,
        current_id=s.saved_survey_ch,
    )
    a.notify_channel_id = await _ask_channel(
        w,
        step=(
            "**Step 2 of 6 — Survey Notification Channel**\n"
            "Select the channel where leadership will be notified when a member submits the survey:"
        ),
        label="notification",
        placeholder="Select the survey notification channel...",
        suggested=suggested_notify_channel,
        current_id=s.saved_notify_ch,
    )


async def _ask_tabs(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 3 and 4: this survey's own pair of tabs."""
    # Stated up front rather than per-step: leadership picking tab names is
    # the moment the one-pair-per-survey rule matters, and it's cheaper to
    # explain once than to reject a name and explain then.
    await w.channel.send(
        "**Next: your two sheet tabs**\n"
        "This survey needs its own pair of tabs, separate from any other "
        "survey's: one holding each member's current answers, one holding "
        "every submission over time. **I'll create them for you** if they "
        "aren't in your sheet yet."
    )
    exclude_survey_id = s.target_survey_id or "default"

    tab = await _ask_survey_tab(
        w.channel,
        prompt=f"**Step 3 of 6 — {s.tpl['responses_step_label']}**\n{s.tpl['responses_step_prompt']}",
        default=s.tpl_tab_responses,
        current=s.current.get("tab_squad_powers", ""),
        modal_title=s.tpl["responses_step_label"],
        cancel_event=w.cancel_event,
        guild_id=w.guild_id,
        claimed_tabs=s.claimed_tabs,
        also_claimed={},
        exclude_field="survey_tab",
        exclude_survey_id=exclude_survey_id,
    )
    if tab is None:
        raise _Abort
    a.tab_squad_powers = tab

    tab = await _ask_survey_tab(
        w.channel,
        prompt=f"**Step 4 of 6 — {s.tpl['history_step_label']}**\n{s.tpl['history_step_prompt']}",
        default=s.tpl_tab_history,
        current=s.current.get("tab_history", ""),
        modal_title=s.tpl["history_step_label"],
        cancel_event=w.cancel_event,
        guild_id=w.guild_id,
        claimed_tabs=s.claimed_tabs,
        also_claimed={a.tab_squad_powers.casefold(): s.target_survey_name or "this survey"},
        exclude_field="survey_tab",
        exclude_survey_id=exclude_survey_id,
    )
    if tab is None:
        raise _Abort
    a.tab_history = tab


async def _ask_intro(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 5: the intro message. Long-form, so it is a free-text reply
    rather than a modal; a saved or template intro is offered first with
    Keep current so leadership never retypes a paragraph to tweak a
    channel."""
    saved_intro = (s.current.get("intro_message") or "").strip()
    # A template ships a written intro, so a brand-new template survey gets
    # the same Keep/Edit choice as a re-run rather than a blank page.
    offered_intro = saved_intro or (s.tpl.get("intro_message") or "").strip()

    if offered_intro:
        # Preview is truncated to keep the embed readable when leadership
        # has saved a long intro.
        preview = offered_intro if len(offered_intro) <= 500 else offered_intro[:500] + "…"
        source_line = "**Currently saved:**" if saved_intro else "**The template suggests:**"
        choice = await w.ask(
            "**Step 5 of 6 — Survey Intro Message**\n"
            "Members see this introductory message before taking the survey.\n\n"
            f"{source_line}\n>>> {preview}",
            IntroChoiceView(),
            "intro_choice",
        )
        if choice == "keep":
            a.intro_message = offered_intro
            return
        await w.channel.send(
            "Type the new intro message below. Use the one above as a guide, or paste in your own."
        )
    else:
        await w.channel.send(
            "**Step 5 of 6 — Survey Intro Message**\n"
            "When your survey is posted, what introductory message do you want your members to see "
            "before they take the survey?\n\n"
            "**Example:**\n"
            f"*{s.tpl['intro_step_example']}*"
        )
    a.intro_message = await w.reply_or_cancel(300)


def _summarise(qs: list, *, with_help: bool = False) -> str:
    return "\n".join(
        f"{i + 1}. **{q['label']}** — "
        + (
            "dropdown: " + ", ".join(q["options"])
            if q["type"] == "dropdown"
            else q.get("type", "text")
        )
        + (f" *(help: {q['placeholder']})*" if with_help and q.get("placeholder") else "")
        for i, q in enumerate(qs)
    )


async def _ask_questions(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 6: which question set to start from, then the builder unless
    the template's questions are taken as they are.

    What's on offer depends on what exists: the template's question set
    (if it has one) and whatever this survey already had. A scratch
    survey being configured for the first time has neither, so there's
    nothing to choose between and it goes straight to the builder."""
    template_questions = list(s.tpl.get("questions") or [])
    has_template_qs = bool(template_questions)
    has_existing_qs = bool(s.questions)

    if not has_template_qs and not has_existing_qs:
        choice = "scratch"
    else:
        body = "**Step 6 of 6 — Survey Questions**\n\n"
        if has_template_qs:
            body += (
                f"**Questions from the {s.tpl['name']} template:**\n"
                f"{_summarise(template_questions)}\n\n"
            )
        if has_existing_qs:
            body += f"**This survey's current questions:**\n{_summarise(s.questions)}\n\n"
        body += "How would you like to set up this survey's questions?"
        choice = await w.ask(
            body,
            QuestionStartView(
                has_template_qs=has_template_qs,
                has_existing_qs=has_existing_qs,
                template_name=s.tpl["name"],
            ),
            "choice",
        )

    if choice == "template_asis":
        a.questions = list(template_questions)
        return
    if choice == "template_edit":
        a.questions = list(template_questions)
    elif choice == "scratch":
        a.questions = []
    else:
        a.questions = list(s.questions)
    await _run_builder_loop(w, a.questions)


# ── The question builder ─────────────────────────────────────────────────────


async def _run_builder_loop(w: _Wizard, questions: list) -> None:
    """Show the current list with Add and Finish until Finish. Edits the
    list in place."""
    while True:
        q_display = (
            _summarise(questions, with_help=True) if questions else "*(no questions added yet)*"
        )
        view = QuestionListView(questions)
        action = await w.ask(f"**Survey Questions:**\n{q_display}", view, "action")

        if action == "finish":
            return

        if action == "delete":
            removed = questions.pop(view.del_index)
            await w.channel.send(f"🗑️ Removed **{removed['label']}**.")
            continue

        if action == "edit":
            idx = view.edit_index
            existing = questions[idx]
            q_num = f"Question {idx + 1}"
        else:
            # Free-tier cap on number of survey questions
            q_cap = await premium.get_limit(
                "survey_questions", w.guild_id, interaction=w.interaction, bot=w.interaction.client
            )
            if q_cap is not None and len(questions) >= q_cap:
                await w.channel.send(
                    embed=premium.limit_reached_embed(
                        feature_label="Survey Questions",
                        current=len(questions),
                        cap=q_cap,
                        plural_unit="questions",
                    )
                )
                continue
            existing = {}
            q_num = f"Question {len(questions) + 1}"

        new_q = await _build_question(w, q_num, existing)
        if action == "edit":
            questions[idx] = new_q
            await w.channel.send(f"✅ Updated **{new_q['label']}**.")
        else:
            questions.append(new_q)
            await w.channel.send(
                f"✅ Added **{new_q['label']}** ({len(questions)} question(s) so far)."
            )


def _key_for(label: str) -> str:
    return label.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")


async def _ask_label(w: _Wizard, q_num: str, existing: dict) -> str:
    label_extra = f"\n*Existing label:* `{existing.get('label', '')}`" if existing else ""
    await w.channel.send(
        f"**{q_num} — Label**\n"
        f"What is the label for this question? (e.g. `Time Zone`, `Preferred Role`)" + label_extra
    )
    return (await w.reply(120)) or existing.get("label", "")


async def _ask_type(w: _Wizard, q_num: str, existing: dict) -> str:
    # Numeric is free; Multi-select / Date are Premium.
    type_options = [
        discord.SelectOption(label=label, value=value) for label, value in _FREE_TYPE_OPTIONS
    ]
    premium_types = _setup()._SURVEY_PREMIUM_TYPES
    if w.is_premium:
        type_options += [
            discord.SelectOption(label=label, value=value)
            for value, (label, _short) in premium_types.items()
        ]
    type_extra = f"\n*Existing type:* `{existing.get('type', 'text')}`" if existing else ""
    prompt = f"**{q_num} — Answer Type**\nPick how members answer this question." + type_extra
    if not w.is_premium:
        prompt += _setup()._locked_types_note(*(short for _label, short in premium_types.values()))
    return await w.ask(prompt, TypeView(type_options), "selected")


async def _ask_help_text(w: _Wizard, q_num: str, existing: dict) -> str:
    help_extra = (
        f"\n*Existing help text:* `{existing.get('placeholder') or 'none'}`" if existing else ""
    )
    await w.channel.send(
        f"**{q_num} — Help Text**\n"
        f"Do you want to show help text for this question? "
        f"This appears as a hint to help members answer correctly.\n"
        f"*(e.g. `Pick the closest match` or `Use your in-game name`)*\n"
        f"Type your help text, or type `none` to skip." + help_extra
    )
    raw = await w.reply(120)
    return "" if raw.lower() == "none" else raw


async def _extra_options(w: _Wizard, q_num: str, existing: dict, q: dict) -> None:
    existing_opts = ", ".join(existing.get("options", [])) if existing else ""
    opts_extra = f"\n*Existing options:* `{existing_opts}`" if existing_opts else ""
    await w.channel.send(
        f"**{q_num} — Options**\n"
        f"Enter the options as comma-separated values. Maximum of 25.\n"
        f"*(e.g. `Yes, No, Not sure`)*" + opts_extra
    )
    raw = await w.reply(120)
    q["options"] = [o.strip() for o in raw.split(",") if o.strip()][:25]


async def _extra_numeric(w: _Wizard, q_num: str, existing: dict, q: dict) -> None:
    # Magnitude — required for numeric. Tells the parser what bare
    # shorthand like `301` means for this field.
    existing_mag = (existing.get("magnitude") if existing else "") or ""
    mag_extra = f"\n*Existing scale:* `{existing_mag or 'raw'}`" if existing else ""
    q["magnitude"] = await w.ask(
        f"**{q_num} — Number Scale**\n"
        f"How big are these numbers typically? Picking a scale lets members type "
        f"the natural shorthand (`301`) instead of the full value (`304,743,912`) — "
        f"the bot accepts both either way." + mag_extra,
        MagnitudeView(),
        "selected",
    )

    # Min/max bounds — Premium-only. Free tier sees a one-line teaser and
    # we move on without bounds.
    if not w.is_premium:
        await w.channel.send(
            "💎 *Min/max bounds are a Premium feature — this question will accept any number.*"
        )
        return
    await w.channel.send(
        f"**{q_num} — Numeric Bounds** *(💎 Premium)*\n"
        f"Reply with `min,max` (e.g. `0,100`), `min,` for only a minimum, "
        f"`,max` for only a maximum, or `none` to skip both bounds.\n"
        f"*Bounds are checked against the stored value after scaling.*"
    )
    raw = (await w.reply(120)).lower()
    if raw in ("", "none"):
        return
    try:
        lo_s, _, hi_s = raw.partition(",")
        if lo_s.strip():
            q["min"] = float(lo_s.strip())
        if hi_s.strip():
            q["max"] = float(hi_s.strip())
    except ValueError:
        await w.channel.send(f"⚠️ Couldn't parse bounds. {RETRY_HINT}")
        raise _Abort


async def _extra_date(w: _Wizard, q_num: str, existing: dict, q: dict) -> None:
    existing_fmt = existing.get("date_format") or "%m/%d/%Y"
    await w.channel.send(
        f"**{q_num} — Date Format** *(💎 Premium)*\n"
        f"Reply with a strptime-style format (e.g. `%m/%d/%Y`, `%Y-%m-%d`), "
        f"or reply `default` for `%m/%d/%Y`."
        + (f"\n*Existing format:* `{existing_fmt}`" if existing else "")
    )
    raw_fmt = await w.reply(120)
    q["date_format"] = "%m/%d/%Y" if raw_fmt.lower() in ("", "default") else raw_fmt


_TYPE_EXTRAS = {
    "dropdown": _extra_options,
    "multi_select": _extra_options,
    "numeric": _extra_numeric,
    "date": _extra_date,
}


async def _build_question(w: _Wizard, q_num: str, existing: dict) -> dict:
    """Add or edit one question: label, type, help text, then the extras
    the type needs."""
    q_label = await _ask_label(w, q_num, existing)
    q_type = await _ask_type(w, q_num, existing)
    placeholder = await _ask_help_text(w, q_num, existing)
    q = {
        "key": _key_for(q_label),
        "label": q_label,
        "type": q_type,
        "options": [],
        "placeholder": placeholder,
        "max_chars": 0,
    }
    extra = _TYPE_EXTRAS.get(q_type)
    if extra is not None:
        await extra(w, q_num, existing, q)
    return q


# ── Save and summary ─────────────────────────────────────────────────────────


async def _save(w: _Wizard, s: _Saved, a: _Answers) -> str:
    """Write the survey and its channel ids; returns the command the
    footer names for updating it."""
    from config import save_survey_config, save_extra_survey, follow_survey_tab_rename

    if s.target_survey_id is None:
        # Default survey: legacy single-row storage, plus the channel IDs go
        # to guild_configs so older code that reads them stays happy.
        #
        # The Buddy System reads profession off this tab by name, from a
        # different wizard and a different column. Renaming it here used to
        # leave buddy pointing at a tab that no longer exists (#441).
        previous_tab = s.current.get("tab_squad_powers", "")
        if follow_survey_tab_rename(w.guild_id, previous_tab, a.tab_squad_powers):
            await w.channel.send(
                f"🔗 Your Buddy System read professions from **{previous_tab}**, "
                f"so I've pointed it at **{a.tab_squad_powers}** too."
            )
        save_survey_config(
            w.guild_id,
            a.tab_squad_powers,
            a.tab_history,
            a.questions,
            a.intro_message,
            s.template_key,
        )
        from config import update_config_field

        update_config_field(w.guild_id, "survey_channel_id", a.survey_channel_id)
        update_config_field(w.guild_id, "survey_notify_channel_id", a.notify_channel_id)
        return "/setup → 📋 Survey"

    # Extra survey: save into guild_extra_surveys; preserve any custom
    # reminder body the leadership previously set.
    save_extra_survey(
        w.guild_id,
        s.target_survey_id,
        survey_name=s.target_survey_name or s.target_survey_id,
        tab_squad_powers=a.tab_squad_powers,
        tab_history=a.tab_history,
        questions=a.questions,
        intro_message=a.intro_message,
        survey_channel_id=a.survey_channel_id,
        notify_channel_id=a.notify_channel_id,
        reminder_message=s.current.get("reminder_message", "") or "",
        reminder_enabled=int(s.current.get("reminder_enabled") or 0),
        template=s.template_key,
    )
    return "/survey"  # premium UI surfaces edit/remove from there


async def _seed_headers(w: _Wizard, a: _Answers) -> None:
    """Label both tabs now that the question list is final. Steps 3 and 4
    create the tabs but run before Step 6, so this is the first moment the
    headers are knowable. Leaving them blank until the first member
    submits invites an alliance to hand-enter data under no headers."""
    from survey import seed_survey_headers
    from config import describe_sheet_error

    try:
        seeded = await asyncio.to_thread(
            seed_survey_headers,
            w.guild_id,
            tab_responses=a.tab_squad_powers,
            tab_history=a.tab_history,
            questions=a.questions,
        )
    except Exception as e:
        print(f"[SETUP] Survey header seed failed guild={w.guild_id}: {describe_sheet_error(e)}")
        await w.channel.send(
            "⚠️ I couldn't add the column headers to your sheet just now. Your "
            "survey is saved, and I'll add them the first time a member submits."
        )
        return
    if seeded:
        await w.channel.send(
            "Added column headers to " + " and ".join(f"**{t}**" for t in seeded) + "."
        )


def _summary_embed(s: _Saved, a: _Answers, next_step_cmd: str) -> discord.Embed:
    from survey_hub import SURVEY_HUB_BTN_TRANSLATE

    q_summary = "\n".join(
        f"• **{q['label']}** — {q['type']}"
        + (f" ({', '.join(q['options'])})" if q["type"] == "dropdown" else "")
        for q in a.questions
    )
    title = (
        f"✅ Survey Configured — {s.target_survey_name}"
        if s.target_survey_id
        else "✅ Survey Configured"
    )
    embed = discord.Embed(title=title, color=discord.Color.green())
    embed.add_field(name="Survey Channel", value=f"<#{a.survey_channel_id}>", inline=True)
    embed.add_field(name="Notification Channel", value=f"<#{a.notify_channel_id}>", inline=True)
    embed.add_field(name=s.tpl["responses_step_label"], value=a.tab_squad_powers, inline=True)
    embed.add_field(name=s.tpl["history_step_label"], value=a.tab_history, inline=True)
    embed.add_field(name="Questions", value=q_summary[:1024], inline=False)
    embed.set_footer(
        text=(
            f"Run {next_step_cmd} again to update. Run /survey and click "
            f"{SURVEY_HUB_BTN_TRANSLATE} if your members don't all read the "
            f"language you write your questions in."
        )
    )
    return embed


# ── The wizard ───────────────────────────────────────────────────────────────


async def run_survey_setup(
    interaction: discord.Interaction,
    bot,
    target_survey_id: str | None = None,
    target_survey_name: str | None = None,
    template: str | None = None,
):
    """
    Walk an admin through configuring a survey.

    `target_survey_id` controls *which* survey is being edited:
      • `None` (default) — edits the guild's main survey
        (`guild_survey_config` row).
      • Any other id  — edits or creates an entry in `guild_extra_surveys`,
        which premium guilds use for additional named surveys.

    `template` names the entry in `defaults.SURVEY_TEMPLATES` this survey
    was built from. It decides the language every step speaks in, the tab
    names suggested, and the questions offered at Step 6. Passing None
    reads the template already saved on the survey, so re-running the
    wizard on a survey reads the way it did when it was created.
    """
    w = _Wizard(
        interaction=interaction,
        bot=bot,
        channel=interaction.channel,
        user=interaction.user,
        guild_id=interaction.guild_id,
        cancel_event=wizard_registry.register(interaction.user.id),
    )
    s = _load(w.guild_id, target_survey_id, target_survey_name, template)
    a = _Answers()
    try:
        await _confirm_reentry(w, s)
        await w.channel.send(f"⚙️ **{s.wizard_label}**\nConfigure the survey for your alliance.")
        w.is_premium = await premium.is_premium(
            w.guild_id, interaction=interaction, bot=interaction.client
        )
        await _ask_channels(w, s, a)
        await _ask_tabs(w, s, a)
        await _ask_intro(w, s, a)
        await _ask_questions(w, s, a)
        if not a.questions:
            await w.channel.send(f"⚠️ No questions defined. {RETRY_HINT}")
            raise _Abort
    except _Abort:
        wizard_registry.unregister(w.user.id, w.cancel_event)
        return

    next_step_cmd = await _save(w, s, a)
    await _seed_headers(w, a)
    confirm_view = SurveyConfiguredView(
        bot,
        guild_id=w.guild_id,
        survey_id=s.target_survey_id,
        survey_name=s.target_survey_name or "Default",
        template_key=s.template_key,
        owner_id=w.user.id,
    )
    confirm_view.message = await w.channel.send(
        embed=_summary_embed(s, a, next_step_cmd), view=confirm_view
    )
    wizard_registry.unregister(w.user.id, w.cancel_event)
    print(
        f"[SETUP] Survey config saved for guild {w.guild_id} "
        f"(survey_id={s.target_survey_id or 'default'}) — {len(a.questions)} questions"
    )


# ── The tab step's helpers ───────────────────────────────────────────────────


async def _ensure_survey_tab(channel, guild_id: int, tab_name: str) -> None:
    """Create `tab_name` in the guild's spreadsheet if it isn't there yet.

    Leadership shouldn't have to go and hand-create two tabs before the
    wizard will work — the storm structured-flow surfaces already create
    their own tabs, and a survey needing a fresh pair per survey is the
    place that needs it most.

    Reports what happened either way, so "we made this for you" is never
    silent. A sheet the alliance owns can be missing, unshared or renamed;
    that's their problem to fix and it must not end the wizard, so it
    degrades to the old "make sure this tab exists" instruction.
    """
    from config import get_spreadsheet, get_or_create_worksheet, describe_sheet_error

    def _work():
        sh = get_spreadsheet(guild_id)
        existed = True
        try:
            sh.worksheet(tab_name)
        except Exception:
            existed = False
        get_or_create_worksheet(sh, tab_name)
        return existed

    try:
        existed = await asyncio.to_thread(_work)
    except Exception as e:
        print(f"[SETUP] Survey tab ensure failed guild={guild_id}: {describe_sheet_error(e)}")
        await channel.send(
            f"⚠️ I couldn't check your Google Sheet just now, so I haven't created "
            f"**{tab_name}**. Setup will continue, but please make sure that tab "
            f"exists in your sheet before members start submitting."
        )
        return

    if existed:
        await channel.send(f"✅ Found **{tab_name}** in your sheet.")
    else:
        await channel.send(f"✅ Created **{tab_name}** in your sheet.")


async def _ask_survey_tab(
    channel,
    *,
    prompt: str,
    default: str,
    current: str,
    modal_title: str,
    cancel_event,
    guild_id: int,
    claimed_tabs: dict,
    also_claimed: dict,
    exclude_field: str = "",
    exclude_survey_id: str | None = None,
) -> str | None:
    """Ask for one of a survey's two tab names, then make sure it exists.

    Re-prompts on a name another survey already uses. Two surveys sharing
    a tab corrupts silently rather than erroring: `append_survey_history`
    only writes a header row when row 1 is empty, so the second survey's
    answers land positionally under the first survey's column names.

    `claimed_tabs` maps casefolded tab name → owning survey name for every
    *other* survey. `also_claimed` is the same shape for tabs picked
    earlier in this same wizard run, which stops a survey's own two tabs
    from being pointed at each other.
    """
    while True:
        tab = await wizard_steps.ask_keep_or_change(
            channel,
            prompt,
            default=default,
            current=current,
            modal_title=modal_title,
            modal_label="Tab name",
            timeout_cmd="setup_survey",
            cancel_event=cancel_event,
        )
        if tab is None:
            return None

        owner = claimed_tabs.get(tab.casefold()) or also_claimed.get(tab.casefold())
        if owner:
            await channel.send(
                f"⚠️ **{tab}** is already used by **{owner}**. Every survey needs its "
                f"own two tabs, because sharing one would write both surveys' answers "
                f"into the same columns. Please pick a different name."
            )
            # Don't re-offer the rejected name as the pre-filled default.
            current = ""
            continue

        # Another *survey* sharing this tab is always wrong, so it's
        # rejected above. Another feature is only probably wrong, so it
        # gets a warning and the step still proceeds (#441).
        await _setup().warn_if_tab_claimed(
            channel,
            guild_id,
            tab,
            exclude_field=exclude_field,
            exclude_survey_id=exclude_survey_id,
        )
        await _ensure_survey_tab(channel, guild_id, tab)
        return tab


# ── The confirmation embed's buttons ─────────────────────────────────────────


# The confirmation embed's buttons stay live well past a wizard step —
# leadership often reads the summary, checks their sheet, then posts.
SURVEY_CONFIRM_VIEW_TIMEOUT = 900  # 15 minutes


class SurveyConfiguredView(OwnedView):
    """Post or edit the survey straight off the wizard's confirmation embed.

    Those are the two things leadership almost always wants next, and
    both otherwise mean leaving, running `/survey`, and picking this
    survey out of a list they just came from.

    Restricted to whoever ran the wizard: the embed sits in a shared
    leadership channel, and these buttons act on a survey the clicker
    may not have configured. Anyone else gets there through `/survey`.
    """

    timeout_hint = "`/survey`"

    def __init__(
        self,
        bot,
        *,
        guild_id: int,
        survey_id: str | None,
        survey_name: str,
        template_key: str,
        owner_id: int,
    ):
        super().__init__(timeout=SURVEY_CONFIRM_VIEW_TIMEOUT)
        self.message = None
        self._bot = bot
        self._guild_id = guild_id
        self._survey_id = survey_id
        self._survey_name = survey_name
        self._template_key = template_key
        self.owner_id = owner_id

        from survey_hub import SURVEY_HUB_BTN_EDIT, SURVEY_HUB_BTN_POST

        post_btn = discord.ui.Button(label=SURVEY_HUB_BTN_POST, style=discord.ButtonStyle.success)
        post_btn.callback = self._on_post
        self.add_item(post_btn)

        edit_btn = discord.ui.Button(label=SURVEY_HUB_BTN_EDIT, style=discord.ButtonStyle.secondary)
        edit_btn.callback = self._on_edit
        self.add_item(edit_btn)

    async def _disable(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)

    async def _on_post(self, interaction: discord.Interaction):
        from config import get_survey
        from survey import post_survey_to_its_channel

        await self._disable(interaction)
        self.stop()
        survey = get_survey(self._guild_id, self._survey_id or "default")
        if survey is None:
            await interaction.followup.send(
                f"⚠️ **{self._survey_name}** is no longer configured.", ephemeral=True
            )
            return
        _ok, message = await post_survey_to_its_channel(self._bot, self._guild_id, survey)
        await interaction.followup.send(message, ephemeral=True)

    async def _on_edit(self, interaction: discord.Interaction):
        await self._disable(interaction)
        self.stop()
        await wizard_registry.guard_wizard_launch(
            run_survey_setup(
                interaction,
                self._bot,
                target_survey_id=self._survey_id,
                target_survey_name=self._survey_name if self._survey_id else None,
                template=self._template_key,
            ),
            interaction,
        )


# ── Adding a named survey ────────────────────────────────────────────────────


class StartChoiceView(discord.ui.View):
    """Template or scratch, for a new survey."""

    def __init__(self):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.start_choice = None

    @discord.ui.button(label="➕ Start from a template", style=discord.ButtonStyle.primary)
    async def from_template(self, inter: discord.Interaction, button: discord.ui.Button):
        self.start_choice = "template"
        await _disable_and_stop(self, inter)

    @discord.ui.button(label="✏️ Start from scratch", style=discord.ButtonStyle.secondary)
    async def from_scratch(self, inter: discord.Interaction, button: discord.ui.Button):
        self.start_choice = "scratch"
        await _disable_and_stop(self, inter)


class TemplatePickView(discord.ui.View):
    """The template picker, in `SURVEY_TEMPLATE_PICKER_ORDER`."""

    def __init__(self):
        from defaults import SURVEY_TEMPLATES, SURVEY_TEMPLATE_PICKER_ORDER

        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.selected = None
        select = discord.ui.Select(
            placeholder="Pick a template...",
            options=[
                discord.SelectOption(
                    label=SURVEY_TEMPLATES[k]["name"],
                    description=SURVEY_TEMPLATES[k]["description"][:100],
                    emoji=SURVEY_TEMPLATES[k]["emoji"],
                    value=k,
                )
                for k in SURVEY_TEMPLATE_PICKER_ORDER
            ],
        )

        async def _cb(inter: discord.Interaction):
            self.selected = select.values[0]
            select.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Template: **{SURVEY_TEMPLATES[self.selected]['name']}**",
                view=self,
            )
            self.stop()

        select.callback = _cb
        self.add_item(select)


class NameChoiceView(discord.ui.View):
    """A template's suggested name as a one-click default, or a modal."""

    def __init__(self, suggested: str):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.survey_name = None
        self.answered = False

        use_btn = discord.ui.Button(
            label=f"✅ Use this name: {suggested}"[:80],
            style=discord.ButtonStyle.success,
        )

        async def _use_cb(inter: discord.Interaction):
            self.survey_name = suggested
            self.answered = True
            await _disable_and_stop(self, inter, content=f"✅ Name: **{suggested}**")

        use_btn.callback = _use_cb
        self.add_item(use_btn)

        own_btn = discord.ui.Button(label="✏️ Name it myself", style=discord.ButtonStyle.secondary)

        async def _own_cb(inter: discord.Interaction):
            modal = TextInputModal(
                "Survey Name", "What should this survey be called?", default=suggested
            )
            await inter.response.send_modal(modal)
            await modal.wait()
            self.survey_name = (modal.value or suggested).strip()[:60] or suggested
            self.answered = True
            for item in self.children:
                item.disabled = True
            try:
                await inter.message.edit(content=f"✅ Name: **{self.survey_name}**", view=self)
            except discord.HTTPException:
                pass
            self.stop()

        own_btn.callback = _own_cb
        self.add_item(own_btn)


async def _ask_new_survey_template(channel, cancel_event) -> str | None:
    """Ask whether a new survey starts from a template or from scratch.

    Returns a template key from `defaults.SURVEY_TEMPLATES`, or None if
    leadership cancelled or let the step time out. The template drives
    the whole wizard afterwards: its language, its suggested tab names,
    and the questions it prefills.
    """
    from defaults import SURVEY_TEMPLATE_SCRATCH
    from survey_hub import SURVEY_HUB_BTN_ADD

    start_view = StartChoiceView()
    await channel.send(
        "💎 **Add a Survey**\n"
        "A survey is any set of questions you want to ask your members. Their "
        "latest answers go in one tab of your sheet, and every submission is "
        "kept in a second tab.\n\n"
        "Start from a ready-made template, or build your own questions from scratch?",
        view=start_view,
    )
    await wait_view_or_cancel(start_view, cancel_event)
    if start_view.cancelled:
        return None
    if not start_view.start_choice:
        await channel.send(WIZARD_TIMEOUT.format(wizard=SURVEY_HUB_BTN_ADD))
        return None

    if start_view.start_choice == "scratch":
        return SURVEY_TEMPLATE_SCRATCH

    pick_view = TemplatePickView()
    await channel.send(
        "**Pick a template**\n"
        "Each one comes with its questions already written. You can edit, "
        "add to, or remove any of them later in this wizard.",
        view=pick_view,
    )
    await wait_view_or_cancel(pick_view, cancel_event)
    if pick_view.cancelled:
        return None
    if not pick_view.selected:
        await channel.send(WIZARD_TIMEOUT.format(wizard=SURVEY_HUB_BTN_ADD))
        return None
    return pick_view.selected


async def _ask_new_survey_name(channel, bot, user, cancel_event, tpl: dict) -> str | None:
    """Ask what the new survey should be called.

    A template offers its own name as a one-click default; the scratch
    path asks for free text. Returns None on cancel or timeout.
    """
    from survey_hub import SURVEY_HUB_BTN_ADD

    suggested = tpl.get("suggested_survey_name")

    if suggested:
        name_view = NameChoiceView(suggested)
        await channel.send(
            "**Name your survey**\n"
            "This is what leadership and members will see when they pick or "
            "take this survey.",
            view=name_view,
        )
        await wait_view_or_cancel(name_view, cancel_event)
        if name_view.cancelled:
            return None
        if not name_view.answered:
            await channel.send(WIZARD_TIMEOUT.format(wizard=SURVEY_HUB_BTN_ADD))
            return None
        return name_view.survey_name

    def check(m):
        return m.author == user and m.channel == channel

    await channel.send(
        "**Name your survey**\n"
        "Type a short display name for it (e.g. `Alliance Feedback` or "
        "`New Member Intake`). This is what leadership and members will see."
    )
    name_reply = await wizard_registry.wait_or_cancel(
        bot.wait_for("message", check=check, timeout=180), cancel_event
    )
    if name_reply is None:
        if not cancel_event.is_set():
            await channel.send(WIZARD_TIMEOUT.format(wizard=SURVEY_HUB_BTN_ADD))
        return None

    survey_name = (name_reply.content or "").strip()[:60]
    if not survey_name:
        await channel.send(
            f"⚠️ That name was empty, so nothing was created. "
            f"Click **{SURVEY_HUB_BTN_ADD}** on `/survey` to try again."
        )
        return None
    return survey_name


async def run_create_new_extra_survey(interaction: discord.Interaction, bot):
    """
    Premium-only: ask whether the new survey starts from a template or from
    scratch, name it, derive a unique slug for `survey_id`, then dispatch to
    `run_survey_setup` to walk through the standard wizard. Called by the
    `[➕ Add Survey]` button on `/survey` for premium guilds.
    """
    from config import list_surveys
    from defaults import survey_template

    channel = interaction.channel
    user = interaction.user

    # The template and name steps run before `run_survey_setup` registers
    # its own cancel event, so they get one of their own — otherwise
    # /cancel does nothing until the wizard proper starts.
    cancel_event = wizard_registry.register(user.id)
    try:
        template_key = await _ask_new_survey_template(channel, cancel_event)
        if template_key is None:
            return
        tpl = survey_template(template_key)

        survey_name = await _ask_new_survey_name(channel, bot, user, cancel_event, tpl)
        if survey_name is None:
            return
    finally:
        # Released before dispatching so the wizard's own registration
        # is the only live one for this user.
        wizard_registry.unregister(user.id, cancel_event)

    extras = [
        s
        for s in list_surveys(interaction.guild_id)
        if (s.get("survey_id") or "default") != "default"
    ]
    base_slug = _slug(survey_name)
    existing_ids = {s.get("survey_id") for s in extras}
    survey_id = base_slug
    suffix = 2
    while survey_id in existing_ids:
        survey_id = f"{base_slug}-{suffix}"
        suffix += 1

    await channel.send(
        f"✅ Creating new survey **{survey_name}** (id: `{survey_id}`).\n"
        f"Walking you through the same setup steps as `/setup` → 📋 Survey…"
    )
    await run_survey_setup(
        interaction,
        bot,
        target_survey_id=survey_id,
        target_survey_name=survey_name,
        template=template_key,
    )


# ── Removing a named survey ──────────────────────────────────────────────────


class _ConfirmRemoveView(discord.ui.View):
    def __init__(self, guild_id: int, target: dict):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.target = target

    @discord.ui.button(label="🗑️ Remove", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        from config import delete_extra_survey

        ok = delete_extra_survey(self.guild_id, self.target["survey_id"])
        msg = (
            f"🗑️ Removed **{self.target.get('survey_name')}**."
            if ok
            else "⚠️ Could not remove that survey."
        )
        await wizard_registry.safe_edit_response(inter, content=msg, view=None)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        await wizard_registry.safe_edit_response(
            inter, content="❌ Canceled. No surveys removed.", view=None
        )
        self.stop()


class _RemovePickView(discord.ui.View):
    def __init__(self, guild_id: int, extras: list[dict]):
        super().__init__(timeout=120)
        sel = discord.ui.Select(
            placeholder="Pick a survey to remove…",
            options=[
                discord.SelectOption(
                    label=(s.get("survey_name") or s.get("survey_id"))[:100],
                    value=s.get("survey_id"),
                )
                for s in extras[:25]
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
                view=_ConfirmRemoveView(guild_id, picked),
            )
            self.stop()

        sel.callback = _cb
        self.add_item(sel)


async def run_remove_extra_survey(interaction: discord.Interaction, bot):
    """
    Premium-only: show a picker of extra surveys and confirm-delete the
    chosen one. Called by the `[🗑️ Remove Survey]` button on `/survey`
    for premium guilds.
    """
    from config import list_surveys

    extras = [
        s
        for s in list_surveys(interaction.guild_id)
        if (s.get("survey_id") or "default") != "default"
    ]
    if not extras:
        await interaction.followup.send(
            "*You have no extra surveys to remove.* "
            "Click **➕ Add Survey** on `/survey` to add one.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        "Pick which extra survey to remove:",
        view=_RemovePickView(interaction.guild_id, extras),
        ephemeral=True,
    )


# ── Picking a survey to edit ─────────────────────────────────────────────────


class _EditPickView(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, bot, surveys: list[dict]):
        super().__init__(timeout=180)
        sel = discord.ui.Select(
            placeholder="Pick a survey to edit…",
            options=[
                discord.SelectOption(
                    label=(s.get("survey_name") or s.get("survey_id"))[:100],
                    value=s.get("survey_id") or "default",
                )
                for s in surveys[:25]
            ],
        )

        async def _cb(inter: discord.Interaction):
            sid = sel.values[0]
            target = next((s for s in surveys if (s.get("survey_id") or "default") == sid), None)
            sel.disabled = True
            name = (target.get("survey_name") if target else sid) or sid
            await wizard_registry.safe_edit_response(
                inter, content=f"✏️ Editing **{name}**…", view=self
            )
            self.stop()
            # Dispatch into the wizard. `target_survey_id=None` means
            # the default survey (run_survey_setup edits guild_survey_config).
            target_id = None if sid == "default" else sid
            await wizard_registry.guard_wizard_launch(
                run_survey_setup(
                    interaction,
                    bot,
                    target_survey_id=target_id,
                    target_survey_name=(target.get("survey_name") if target else None),
                    # Keep speaking the language this survey was built in.
                    template=(target.get("template") if target else None),
                ),
                interaction,
            )

        sel.callback = _cb
        self.add_item(sel)


async def run_pick_survey_to_edit(interaction: discord.Interaction, bot):
    """
    Show a picker covering the default survey + all extras, then dispatch
    into `run_survey_setup` with the chosen target. Called by the
    `[✏️ Edit Survey]` button on `/survey` for premium guilds.
    """
    from config import list_surveys

    surveys = list_surveys(interaction.guild_id)
    await interaction.followup.send(
        "Which survey would you like to edit?",
        view=_EditPickView(interaction, bot, surveys),
        ephemeral=True,
    )

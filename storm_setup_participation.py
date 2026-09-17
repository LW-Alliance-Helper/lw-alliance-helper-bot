"""
The storm wizard's participation step (#20 rework): enable or keep,
the participation Sheet tab, the roster source (tab, name column, alias
column, first data row), the preset picker (#247), and the question
builder loop with its per-type extras (#244).

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup.py` and `storm_setup_structured.py` set:
`setup_cog` imports the three entry points back under their old names
(`_run_storm_participation_step`, `_run_participation_preset_picker_step`,
`_build_participation_question`) along with the type constants and
`wait_for_msg_simple`, so every caller and every `patch("setup_cog.X")`
keeps its target. The shared wizard pieces come from `wizard_steps`; this
module reaches into `setup_cog` at call time (`_setup()`) only for what
still lives there (the column-letter helpers, the tab-claim warning, the
locked-types note).

Shape:
* `_Walk` carries the handles every question needs and the three ways a
  question can end: an answer, a cancel (`_Abort`, silent) or a timeout
  (`_Abort` after the route back).
* One function per question or stage; the question builder dispatches
  its per-type extras from a table.
* The six inline views become module classes under their old names.

Every prompt, label, acknowledgement and returned dict is unchanged from
the functions this replaced; `tests/integration/test_storm_setup_participation.py`
was written against them and holds this module to it.
"""

import asyncio
from dataclasses import dataclass

import discord

import wizard_registry
import wizard_steps
from messages import GENERIC_CMD_TIMEOUT
from setup_hub import HUB_BTN_BIRTHDAYS, HUB_BTN_SURVEY
from wizard_registry import wait_view_or_cancel


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. Whatever notice the
    exit calls for has already been posted when this is raised."""


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
    "text",
    "yes_no",
    "numeric",
    "roster_names",
    "roster_multi_select",
]
_PARTICIPATION_PREMIUM_TYPES = [
    "single_select",
    "multi_select",
    "date",
    "derived_count",
]
_PARTICIPATION_TYPE_LABELS = {
    "text": "🔤 Text: short typed answer",
    "yes_no": "☑️ Yes / No",
    "numeric": "🔢 Numeric: number with optional min/max",
    "roster_names": "Roster names: pick or type member names",
    "roster_multi_select": "Roster multi-select: pick members from a dropdown",
    "single_select": "🔽 Single-select dropdown",
    "multi_select": "Multi-select dropdown",
    "date": "📅 Date (formatted entry)",
    "derived_count": "✨ Derived count: bot counts past events per member",
}

# Short forms of the Premium labels above, for the free-tier note on the
# type prompt. The full labels carry a trailing description that reads as
# clutter in a sentence; the mark has to match, and
# `test_participation_premium_short_names_keep_their_mark` asserts it does.
_PARTICIPATION_PREMIUM_TYPE_SHORT = {
    "single_select": "🔽 Single-select",
    "multi_select": "Multi-select",
    "date": "📅 Date",
    "derived_count": "✨ Derived count",
}

# #244: question types that produce *per-member* data (one flag per
# member per event) rather than *event-level* data (one answer per
# event). These get written to the new `DS Member Log` / `CS Member
# Log` Sheet tab instead of the existing participation log tab. Used
# by `storm_log.run_log_flow` to branch the write paths.
_PARTICIPATION_PER_MEMBER_TYPES = ("roster_multi_select", "derived_count")

#: The free tier's participation question cap. Premium has none.
_FREE_QUESTION_CAP = 3


# ── Shared handles ───────────────────────────────────────────────────────────


@dataclass
class _Walk:
    channel: discord.abc.Messageable
    bot: discord.Client
    user: discord.abc.User
    cancel_event: asyncio.Event
    cmd_name: str
    is_premium: bool

    def check(self, m) -> bool:
        return m.author == self.user and m.channel == self.channel

    async def timed_out(self) -> None:
        await self.channel.send(GENERIC_CMD_TIMEOUT.format(cmd=self.cmd_name))

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
            await self.timed_out()
            raise _Abort
        return value

    async def ask_strict(self, text: str, view, attr: str):
        """Like `ask`, but a cancel posts the route back too: the three
        Premium extras of the question builder have always answered a
        cancel and a timeout the same way."""
        await self.channel.send(text, view=view)
        await wait_view_or_cancel(view, self.cancel_event)
        value = getattr(view, attr)
        if getattr(view, "cancelled", False) or value is None:
            await self.timed_out()
            raise _Abort
        return value

    async def reply(self, seconds: int) -> str:
        """The officer's next typed message; a timeout posts the route back
        and raises."""
        try:
            reply = await self.bot.wait_for("message", check=self.check, timeout=seconds)
        except asyncio.TimeoutError:
            await self.timed_out()
            raise _Abort
        return reply.content

    async def keep_or_change(self, prompt: str, **kw) -> str:
        """One `ask_keep_or_change` question; the helper posts its own
        cancel / timeout notice, so an abandoned one just raises."""
        picked = await wizard_steps.ask_keep_or_change(
            self.channel, prompt, timeout_cmd=self.cmd_name, cancel_event=self.cancel_event, **kw
        )
        if picked is None:
            raise _Abort
        return picked


# ── Views ────────────────────────────────────────────────────────────────────


class _PresetPickerView(discord.ui.View):
    """The multi-select over the preset catalogue with Add / Skip."""

    def __init__(
        self, available: list[dict], *, default_checked: set, premium_only, is_premium: bool
    ):
        super().__init__(timeout=300)
        self.action: str | None = None
        self.selected: set[str] = set(default_checked)
        self.cancelled = False

        options = []
        for p in available[:25]:  # Discord 25-option cap
            emoji = p.get("emoji", "")
            # Premium-only on free tier: 💎 prefix + a Premium hint
            # in the description so officers can't miss it.
            if premium_only(p) and not is_premium:
                label = f"💎 {emoji} {p['label']}"[:100]
                description = f"💎 Premium · {p.get('description', '')}"[:100]
            else:
                label = f"{emoji} {p['label']}"[:100]
                description = p.get("description", "")[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=p["key"],
                    description=description,
                    default=(p["key"] in default_checked),
                )
            )
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

    async def _finish(self, inter: discord.Interaction, action: str) -> None:
        self.action = action
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    @discord.ui.button(label="✅ Add picked presets", style=discord.ButtonStyle.success, row=1)
    async def add(self, inter: discord.Interaction, _btn):
        await self._finish(inter, "add")

    @discord.ui.button(label="↩️ Skip presets", style=discord.ButtonStyle.secondary, row=1)
    async def skip(self, inter: discord.Interaction, _btn):
        await self._finish(inter, "skip")


class _BuilderView(discord.ui.View):
    """The question list's controls: edit and remove selects when there
    are questions, then Add and Done."""

    def __init__(self, questions: list[dict]):
        super().__init__(timeout=300)
        self.action: str | None = None
        self.edit_idx: int | None = None
        self.del_idx: int | None = None

        if questions:
            edit_sel = discord.ui.Select(
                placeholder="✏️ Edit a question…",
                options=[
                    discord.SelectOption(label=f"Edit: {q.get('label', '?')[:90]}", value=str(i))
                    for i, q in enumerate(questions[:25])
                ],
                row=0,
            )

            async def _ec(inter: discord.Interaction):
                self.edit_idx = int(edit_sel.values[0])
                await self._finish(inter, "edit")

            edit_sel.callback = _ec
            self.add_item(edit_sel)

            del_sel = discord.ui.Select(
                placeholder="🗑️ Remove a question…",
                options=[
                    discord.SelectOption(label=f"Remove: {q.get('label', '?')[:90]}", value=str(i))
                    for i, q in enumerate(questions[:25])
                ],
                row=1,
            )

            async def _dc(inter: discord.Interaction):
                self.del_idx = int(del_sel.values[0])
                await self._finish(inter, "delete")

            del_sel.callback = _dc
            self.add_item(del_sel)

    async def _finish(self, inter: discord.Interaction, action: str) -> None:
        self.action = action
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    @discord.ui.button(label="➕ Add question", style=discord.ButtonStyle.primary, row=2)
    async def add_q(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._finish(inter, "add")

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=2)
    async def done(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._finish(inter, "done")


class _TypeView(discord.ui.View):
    """The answer-type dropdown."""

    def __init__(self, type_options: list[discord.SelectOption]):
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


class _PrefillView(discord.ui.View):
    """Roster multi-select (Premium): pre-fill from the poll, or manual."""

    def __init__(self):
        super().__init__(timeout=120)
        self.selected: str | None = None
        self.cancelled = False

    async def _pick(self, inter, selected: str, ack: str) -> None:
        self.selected = selected
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, content=ack, view=self)
        self.stop()

    @discord.ui.button(
        label="🗳️ Pre-fill from Discord poll signups", style=discord.ButtonStyle.primary
    )
    async def from_poll(self, inter, _btn):
        await self._pick(inter, "discord_poll", "✅ Pre-fill source: **Discord poll signups**")

    @discord.ui.button(label="✏️ Manual selection only", style=discord.ButtonStyle.secondary)
    async def manual(self, inter, _btn):
        await self._pick(inter, "", "✅ Pre-fill source: **Manual selection only**")


class _SourceView(discord.ui.View):
    """Derived count (Premium): which roster multi-select question to count."""

    def __init__(self, source_candidates: list[dict]):
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


class _ShowDuringLogView(discord.ui.View):
    """Derived count (Premium): show the count during the log, or only in
    the Trends Viewer."""

    def __init__(self):
        super().__init__(timeout=120)
        self.selected: bool | None = None
        self.cancelled = False

    async def _pick(self, inter, selected: bool, ack: str) -> None:
        self.selected = selected
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, content=ack, view=self)
        self.stop()

    @discord.ui.button(
        label="📋 Show counts per member during the log", style=discord.ButtonStyle.primary
    )
    async def yes_btn(self, inter, _btn):
        await self._pick(inter, True, "✅ Show during the log: **Yes**")

    @discord.ui.button(
        label="🔍 Only show in the Trends Viewer", style=discord.ButtonStyle.secondary
    )
    async def no_btn(self, inter, _btn):
        await self._pick(inter, False, "✅ Show during the log: **No**")


# ── The preset picker (#247) ─────────────────────────────────────────────────


def _tier_note(prem_count_visible: int, is_premium: bool) -> str:
    if is_premium:
        if prem_count_visible > 0:
            return f"\n💎 *Includes {prem_count_visible} Premium preset(s).*"
        return ""
    if prem_count_visible > 0:
        return (
            f"\n💎 *Presets marked with 💎 are Premium-only. Run "
            f"`/upgrade` to unlock {prem_count_visible} additional "
            f"preset(s).*"
        )
    return ""


async def _drop_premium_picks(w: _Walk, picks: list[dict], premium_only) -> list[dict]:
    """Free tier: the picker shows Premium presets as a teaser; picking one
    surfaces the upsell and drops it. Free picks in the same submission
    stand."""
    premium_picks = [p for p in picks if premium_only(p)]
    if premium_picks:
        labels = ", ".join(f"**{p['label']}**" for p in premium_picks)
        await w.channel.send(
            f"💎 *Skipped Premium-only preset(s):* {labels}. Run "
            f"`/upgrade` to unlock them, then re-run setup to add."
        )
        picks = [p for p in picks if not premium_only(p)]
    return picks


async def _trim_to_cap(w: _Walk, picks: list[dict], *, cap: int, existing: int) -> list[dict]:
    """Free-tier cap, by the room already spent, so officers don't land
    past it by accident."""
    room = max(0, cap - existing)
    if room < len(picks):
        await w.channel.send(
            f"⚠️ Free tier caps participation questions at {cap}. "
            f"You picked {len(picks)} preset(s), but only "
            f"{room} fit. Added the first {room}; ignore or upgrade "
            f"for the rest."
        )
        picks = picks[:room]
    return picks


def _dependency_warnings(additions: list[dict], known_keys: set) -> list[str]:
    """A derived_count preset whose source question is neither picked nor
    configured still lands, but stays inert until the source exists."""
    from defaults import storm_participation_presets

    warnings: list[str] = []
    for q in additions:
        src = q.get("source_question_key")
        if not src or src in known_keys:
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
    return warnings


async def run_preset_picker_step(
    channel,
    bot,
    user,
    cancel_event,
    *,
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
    from defaults import storm_participation_presets, preset_to_question

    w = _Walk(channel, bot, user, cancel_event, cmd_name, is_premium_flag)

    # Always show both tiers' presets so free-tier officers can see
    # what Premium would unlock. The free-tier set is the base; the
    # Premium-only set is appended with a 💎 prefix on the visible
    # label so officers can tell them apart at a glance.
    free_keys = {p["key"] for p in storm_participation_presets(is_premium=False)}
    existing_keys = {q.get("key") for q in existing_questions if q.get("key")}
    available = [
        p for p in storm_participation_presets(is_premium=True) if p["key"] not in existing_keys
    ]
    if not available:
        return []

    def _is_premium_only(p: dict) -> bool:
        return p["key"] not in free_keys

    # Default-check the presets marked `default_checked` in defaults.py
    # (currently just "Did this member show up?"). Premium-only ones
    # are never default-checked on free tier — defaulting to a 💎
    # preset would force the upsell ack just for hitting Add.
    default_checked = {
        p["key"]
        for p in available
        if p.get("default_checked") and (is_premium_flag or not _is_premium_only(p))
    }
    view = _PresetPickerView(
        available,
        default_checked=default_checked,
        premium_only=_is_premium_only,
        is_premium=is_premium_flag,
    )
    prem_count_visible = sum(1 for p in available if _is_premium_only(p))
    try:
        action = await w.ask(
            f"**Step 7.6: Use any preset questions?**\n"
            f"Pre-configured templates for the common participation "
            f"questions. Pick any you want and they'll land in your "
            f"question list ready to use. You can still customize them "
            f"later in the next step.{_tier_note(prem_count_visible, is_premium_flag)}",
            view,
            "action",
        )
    except _Abort:
        return None
    if action == "skip" or not view.selected:
        return []

    picks = [p for p in available if p["key"] in view.selected]
    if not is_premium_flag:
        picks = await _drop_premium_picks(w, picks, _is_premium_only)
        if not picks:
            return []
    if cap is not None:
        picks = await _trim_to_cap(w, picks, cap=cap, existing=len(existing_questions))

    additions = [preset_to_question(p) for p in picks]
    warnings = _dependency_warnings(additions, {q["key"] for q in additions} | existing_keys)
    if warnings:
        await channel.send("\n".join(warnings))

    summary = ", ".join(f"**{q['label']}**" for q in additions)
    await channel.send(f"✅ Added preset(s): {summary}")
    return additions


# ── The question builder ─────────────────────────────────────────────────────


async def _ask_label(w: _Walk, existing: dict | None) -> str:
    label_extra = f"\n*Existing label:* `{existing.get('label', '')}`" if existing else ""
    await w.channel.send(
        "**Question: Label**\n"
        "What's the label for this question? (e.g. `Sitting Out`, `Vote Count`)" + label_extra
    )
    reply = await w.reply(180)
    q_label = reply.strip() or (existing.get("label", "") if existing else "")
    if not q_label:
        await w.channel.send("⚠️ Empty label. Skipping this question.")
        raise _Abort
    return q_label


def _key_for(label: str) -> str:
    return (
        label.lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
        .replace("(", "")
        .replace(")", "")
    )


async def _ask_type(w: _Walk, existing: dict | None) -> str:
    type_options: list[discord.SelectOption] = []
    for t in _PARTICIPATION_FREE_TYPES:
        type_options.append(discord.SelectOption(label=_PARTICIPATION_TYPE_LABELS[t], value=t))
    if w.is_premium:
        for t in _PARTICIPATION_PREMIUM_TYPES:
            type_options.append(discord.SelectOption(label=_PARTICIPATION_TYPE_LABELS[t], value=t))

    type_extra = f"\n*Existing type:* `{existing.get('type')}`" if existing else ""
    type_prompt = f"**Question: Answer Type**{type_extra}"
    if not w.is_premium:
        type_prompt += _setup()._locked_types_note(
            *(_PARTICIPATION_PREMIUM_TYPE_SHORT[t] for t in _PARTICIPATION_PREMIUM_TYPES)
        )
    return await w.ask(type_prompt, _TypeView(type_options), "selected")


async def _extra_numeric(w: _Walk, q: dict, all_questions: list[dict]) -> None:
    await w.channel.send(
        "**Optional bounds**\nReply with `min,max` (e.g. `0,500`) or type `none` for no bounds."
    )
    bounds_raw = (await w.reply(120)).strip().lower()
    if bounds_raw in ("", "none"):
        return
    try:
        lo, hi = (s.strip() for s in bounds_raw.split(","))
        if lo:
            q["min"] = float(lo) if "." in lo else int(lo)
        if hi:
            q["max"] = float(hi) if "." in hi else int(hi)
    except Exception:
        await w.channel.send("⚠️ Couldn't parse those bounds. Saving without min/max.")


async def _extra_select(w: _Walk, q: dict, all_questions: list[dict]) -> None:
    await w.channel.send(
        "**Options** *(💎 Premium)*\nList the choices separated by commas.\n"
        "Example: `Win, Loss, Draw`"
    )
    reply = await w.reply(180)
    opts = [o.strip() for o in reply.split(",") if o.strip()]
    if not opts:
        await w.channel.send("⚠️ No options provided. Skipping this question.")
        raise _Abort
    q["options"] = opts[:25]


async def _extra_date(w: _Walk, q: dict, all_questions: list[dict]) -> None:
    await w.channel.send(
        "**Date format** *(💎 Premium)*\nEnter a `strptime`-style format "
        "(e.g. `%m/%d/%Y`) or reply `default` for `%m/%d/%Y`."
    )
    fmt = (await w.reply(120)).strip()
    q["date_format"] = "%m/%d/%Y" if fmt.lower() in ("", "default") else fmt


async def _extra_roster_multi_select(w: _Walk, q: dict, all_questions: list[dict]) -> None:
    # #244 — paginated multi-select against the alliance roster.
    # Optional auto-prefill source (Premium only). Free tier always
    # captures manually.
    if not w.is_premium:
        return
    selected = await w.ask_strict(
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
        _PrefillView(),
        "selected",
    )
    if selected:
        q["prefill_source"] = selected


async def _extra_derived_count(w: _Walk, q: dict, all_questions: list[dict]) -> None:
    # #244 — Premium-only. The bot reads past Per-Member Log rows
    # for the source question and counts per member. Officer can
    # override at participation time.

    # Source question picker — must be a roster_multi_select.
    source_candidates = [
        other for other in (all_questions or []) if other.get("type") == "roster_multi_select"
    ]
    if not source_candidates:
        await w.channel.send(
            "⚠️ **Derived count needs a source question** of type "
            "`Roster multi-select` to read from. Add one of those "
            "first, then come back and add this derived count.\n"
            "Skipping this question for now."
        )
        raise _Abort
    q["source_question_key"] = await w.ask_strict(
        "**Source question** *(💎 Premium)*\n"
        "Which roster multi-select question's history should this "
        "count read from?",
        _SourceView(source_candidates),
        "selected",
    )

    # Lookback window — number of past events to scan.
    attempts = 3
    lookback = 4
    while attempts > 0:
        raw = await wait_for_msg_simple(
            w.channel,
            w.bot,
            w.user,
            w.cancel_event,
            "**Lookback window** *(💎 Premium)*\n"
            "How many past captured events should the count cover? "
            "(e.g. `4` for 'past 4 events'). Default is 4.",
        )
        if raw is None:
            raise _Abort
        raw = raw.strip()
        if not raw:
            break
        try:
            lookback = max(1, int(raw))
            break
        except ValueError:
            attempts -= 1
            await w.channel.send(f"⚠️ `{raw}` isn't a number. Please re-enter.")
    q["lookback_events"] = lookback

    q["show_during_log"] = bool(
        await w.ask_strict(
            "**Show during participation log?** *(💎 Premium)*\n"
            "When officers run the log, should this count display per "
            "member next to their name? Off by default — the count "
            "still lives in the Trends Viewer either way.",
            _ShowDuringLogView(),
            "selected",
        )
    )


_TYPE_EXTRAS = {
    "numeric": _extra_numeric,
    "single_select": _extra_select,
    "multi_select": _extra_select,
    "date": _extra_date,
    "roster_multi_select": _extra_roster_multi_select,
    "derived_count": _extra_derived_count,
}


async def build_participation_question(
    channel,
    bot,
    user,
    cancel_event,
    *,
    cmd_name: str,
    is_premium_flag: bool,
    existing: dict | None,
    all_questions: list[dict] | None = None,
) -> dict | None:
    """Add or edit a single participation question. Mirrors the survey
    question builder's shape but with participation-specific types.

    `all_questions` (#244): the full configured-so-far list. Lets the
    derived_count type's source picker enumerate existing
    roster_multi_select questions to point at.
    """
    w = _Walk(channel, bot, user, cancel_event, cmd_name, is_premium_flag)
    try:
        q_label = await _ask_label(w, existing)
        q_type = await _ask_type(w, existing)
        q: dict = {"key": _key_for(q_label), "label": q_label, "type": q_type}
        extra = _TYPE_EXTRAS.get(q_type)
        if extra is not None:
            await extra(w, q, all_questions or [])
    except _Abort:
        return None
    return q


async def wait_for_msg_simple(
    channel,
    bot,
    user,
    cancel_event,
    prompt: str,
    *,
    wait_seconds: int = 120,
) -> str | None:
    """Minimal message-wait helper for inline use inside
    `build_participation_question`. Returns the user's reply text
    or None on cancel/timeout."""

    def check(m):
        return m.author == user and m.channel == channel

    await channel.send(prompt)
    try:
        reply = await bot.wait_for("message", check=check, timeout=wait_seconds)
        return reply.content
    except asyncio.TimeoutError:
        await channel.send("⏰ Timed out.")
        return None


# ── The step ─────────────────────────────────────────────────────────────────


def _disabled_shape(cur_part: dict) -> dict:
    """Disabled — keep the existing values around but mark off."""
    return {
        "enabled": 0,
        "tab_name": cur_part.get("tab_name") or "",
        "questions": cur_part.get("questions") or [],
        "roster_tab": cur_part.get("roster_tab") or "",
        "roster_name_col": cur_part.get("roster_name_col") or 0,
        "roster_alias_col": cur_part.get("roster_alias_col")
        if cur_part.get("roster_alias_col") is not None
        else -1,
        "roster_start_row": cur_part.get("roster_start_row") or 2,
    }


async def _ask_yes_no(w: _Walk, prompt: str, *, current: bool | None) -> bool:
    """A yes/no question: the plain view on a fresh setup, the keep-or-flip
    gate when an answer was saved, so a re-run never forces a re-pick."""
    if current is None:
        return bool(await w.ask(prompt, wizard_steps.YesNoView(), "selected"))
    gate = wizard_steps._KeepOrFlipYesNoGate(current_value=current)
    return bool(await w.ask(prompt, gate, "value"))


async def _ask_enable(w: _Walk, cur_part: dict, *, label: str, parent_cmd: str) -> bool:
    # Treat any prior saved row (enabled true OR disabled with config
    # bits set) as "re-entry" so the Yes/No prompt offers Keep current
    # instead of forcing the officer to re-pick. A pristine row has
    # everything zero / empty.
    previously_saved = (
        bool(cur_part.get("enabled"))
        or bool(cur_part.get("tab_name"))
        or bool(cur_part.get("questions"))
    )
    return await _ask_yes_no(
        w,
        f"**Step 7 of 9: Participation Tracking**\n"
        f"Do you want to track {label} participation? Leadership clicks "
        f"**📊 Fill out participation questions** on `/{parent_cmd}` "
        f"after each event to log who showed up, who sat out, etc.\n"
        f"You'll define the questions yourself, so the tracker matches how "
        f"your alliance runs the event.",
        current=bool(cur_part.get("enabled")) if previously_saved else None,
    )


async def _ask_tab(w: _Walk, cur_part: dict, *, guild_id: int, event_type: str, label: str) -> str:
    hardcoded_tab = "DS Participation Log" if event_type == "DS" else "CS Participation Log"
    tab_name = await w.keep_or_change(
        f"**Step 7.1: Participation Sheet Tab**\n"
        f"Which tab should the bot write {label} participation rows to?\n"
        f"ℹ️ *The bot will create this tab automatically if it doesn't exist "
        f"and will manage the column structure based on the questions you define.*",
        default=hardcoded_tab,
        current=cur_part.get("tab_name", ""),
        modal_title="Participation Tab",
        modal_label="Tab name",
    )
    await _setup().warn_if_tab_claimed(
        w.channel, guild_id, tab_name, exclude_field="participation_tab_name"
    )
    return tab_name


async def _ask_column(
    w: _Walk, prompt: str, *, default: str, current: str, modal_title: str
) -> int:
    """A column-letter question, parsed; a letter that isn't one ends the step."""
    raw = await w.keep_or_change(
        prompt,
        default=default,
        current=current,
        modal_title=modal_title,
        modal_label="Column letter",
    )
    idx = _setup()._col_letter_to_index(raw)
    if idx < 0:
        await w.channel.send(
            f"⚠️ `{raw}` isn't a valid column letter. Run `/{w.cmd_name}` to start again."
        )
        raise _Abort
    return idx


def _letter_or_blank(idx) -> str:
    return _setup()._col_index_to_letter(idx) if isinstance(idx, int) and idx >= 0 else ""


async def _ask_roster_source(
    w: _Walk, cur_part: dict, *, guild_id: int
) -> tuple[str, int, int, int]:
    """Steps 7.2 to 7.5: the roster tab, its name column, the optional
    alias column and the first data row."""
    from config import get_survey_config, get_birthday_config

    # Smart "current" suggestion: prefer a previously-saved roster source
    # for this event type, else fall back to the survey stats tab if
    # configured, else birthday tab. The hardcoded default ("Squad
    # Powers") is the bot's baseline if none of those exist either.
    survey_cfg = get_survey_config(guild_id) or {}
    birthday_cfg = get_birthday_config(guild_id) or {}
    suggested_tab = (
        cur_part.get("roster_tab")
        or survey_cfg.get("tab_squad_powers")
        or birthday_cfg.get("tab_name")
        or ""
    )
    roster_tab = await w.keep_or_change(
        f"**Step 7.2: Roster Source: Sheet Tab**\n"
        f"Which tab in your sheet has the list of members? The bot reads "
        f"member names from here when you use a `Roster names` question.\n"
        f"*Tip: this is often the same tab you use for `/setup` → {HUB_BTN_SURVEY} or "
        f"`/setup` → {HUB_BTN_BIRTHDAYS}.*",
        default="Squad Powers",
        current=suggested_tab,
        modal_title="Roster Tab",
        modal_label="Tab name",
    )

    roster_name_col = await _ask_column(
        w,
        "**Step 7.3: Roster Source: Name Column**\n"
        "Which column letter has the member name? (e.g. `A`, `B`, `E`)",
        default="A",
        current=_letter_or_blank(cur_part.get("roster_name_col")),
        modal_title="Name column",
    )

    # Re-entry: if the alliance previously configured a roster alias
    # column (saved as >= 0) OR explicitly opted out (-1) on a prior
    # save, surface the keep-or-flip gate instead of plain Yes/No.
    previously_saved = (
        bool(cur_part.get("enabled"))
        or bool(cur_part.get("tab_name"))
        or bool(cur_part.get("questions"))
    )
    saved_alias_idx = cur_part.get("roster_alias_col")
    alias_answered = previously_saved and isinstance(saved_alias_idx, int)
    alias_selected = await _ask_yes_no(
        w,
        "**Step 7.4: Roster Source: Alias Column?**\n"
        "If you have other names or nicknames that you call your members in these "
        "mails, this helps resolve to their full name in your sheet automatically. "
        "Do you have an alias column?",
        current=(saved_alias_idx >= 0) if alias_answered else None,
    )

    roster_alias_col = -1
    if alias_selected:
        # Default the alias column to Member Sync's `display_col` slot
        # when sync is enabled — that's where the bot writes the
        # Discord display name (the closest thing to an alias the bot
        # maintains). Otherwise fall back to the historic
        # "column right after the name column" convention.
        from config import get_member_roster_config as _gmrc_alias

        sync_cfg = _gmrc_alias(guild_id) if guild_id else {}
        if sync_cfg.get("enabled"):
            alias_default = _setup()._col_index_to_letter(int(sync_cfg.get("display_col", 2)))
        else:
            alias_default = _setup()._col_index_to_letter(roster_name_col + 1)
        roster_alias_col = await _ask_column(
            w,
            "**Alias Column**\nWhich column letter has the alias / nickname?",
            default=alias_default,
            current=_letter_or_blank(saved_alias_idx),
            modal_title="Alias column",
        )

    raw_start = await w.keep_or_change(
        "**Step 7.5: Roster Source: First Data Row**\n"
        "In your existing roster tab above, which row does the member data start on? "
        "Usually `2` if your sheet has a header row in row 1.",
        default="2",
        current=str(cur_part.get("roster_start_row") or ""),
        modal_title="Data start row",
        modal_label="Row number",
    )
    try:
        roster_start_row = max(1, int(raw_start.strip()))
    except ValueError:
        await w.channel.send(f"⚠️ `{raw_start}` isn't a number. Run `/{w.cmd_name}` to start again.")
        raise _Abort
    return roster_tab, roster_name_col, roster_alias_col, roster_start_row


def _summarize(questions: list[dict]) -> str:
    if not questions:
        return "*(no questions yet; every participation log will only ask for the date)*"
    lines = []
    for i, q in enumerate(questions, start=1):
        t = _PARTICIPATION_TYPE_LABELS.get(q.get("type"), q.get("type", "?"))
        lines.append(f"**{i}. {q.get('label', '?')}**: _{t}_")
    return "\n".join(lines)


async def _run_builder_loop(
    w: _Walk, questions: list[dict], *, parent_cmd: str, cap: int | None
) -> None:
    """Step 7.7: add, edit and remove questions until Done. Edits the
    list in place."""
    import premium

    cap_note = (
        f"\n*Free tier limit: {cap} questions.*"
        if cap is not None
        else "\n💎 *Premium: unlimited questions and three extra question types.*"
    )
    while True:
        view = _BuilderView(questions)
        await w.channel.send(
            f"**Step 7.7: Participation Questions**\n"
            f"Each question becomes a column on your sheet and a step in "
            f"the **📊 Fill out participation questions** flow on "
            f"`/{parent_cmd}`.\n"
            f"Examples: *Vote count*, *Sitting out*, *Did anyone show up late?*\n"
            f"{cap_note}\n\n{_summarize(questions)}",
            view=view,
        )
        await w.wait(view)
        if view.action is None:
            await w.timed_out()
            raise _Abort
        if view.action == "done":
            return
        if view.action == "delete":
            removed = questions.pop(view.del_idx)
            await w.channel.send(f"🗑️ Removed **{removed.get('label')}**.")
            continue
        if view.action == "add" and cap is not None and len(questions) >= cap:
            await w.channel.send(
                embed=premium.limit_reached_embed(
                    feature_label="Participation Questions",
                    current=len(questions),
                    cap=cap,
                    plural_unit="questions",
                )
            )
            continue
        existing = questions[view.edit_idx] if view.action == "edit" else None
        new_q = await _setup()._build_participation_question(
            w.channel,
            w.bot,
            w.user,
            w.cancel_event,
            cmd_name=w.cmd_name,
            is_premium_flag=w.is_premium,
            existing=existing,
            all_questions=questions,
        )
        if new_q is None:
            raise _Abort
        if view.action == "edit":
            questions[view.edit_idx] = new_q
            await w.channel.send(f"✅ Updated **{new_q['label']}**.")
        else:
            questions.append(new_q)
            await w.channel.send(f"✅ Added **{new_q['label']}** ({len(questions)} so far).")


async def run_participation_step(
    channel,
    bot,
    user,
    cancel_event,
    *,
    guild_id: int,
    event_type: str,
    label: str,
    cmd_name: str,
    is_premium_flag: bool,
    current: dict,
) -> dict | None:
    """
    Step 6 of the storm setup wizard (DS + CS). Walks leadership
    through enabling/configuring participation log tracking. Returns a
    dict shaped like the one save_participation_config expects, or None
    if the user cancelled or timed out.
    """
    from config import get_participation_config

    w = _Walk(channel, bot, user, cancel_event, cmd_name, is_premium_flag)
    cur_part = get_participation_config(guild_id, event_type)
    parent_cmd = "desertstorm" if event_type == "DS" else "canyonstorm"
    try:
        if not await _ask_enable(w, cur_part, label=label, parent_cmd=parent_cmd):
            return _disabled_shape(cur_part)
        tab_name = await _ask_tab(
            w, cur_part, guild_id=guild_id, event_type=event_type, label=label
        )
        roster_tab, name_col, alias_col, start_row = await _ask_roster_source(
            w, cur_part, guild_id=guild_id
        )

        # ── Preset picker (#247) ──
        # Offer pre-configured question templates so officers don't have to
        # spell out the common shapes by hand. The picker filters out
        # templates already present in the existing questions list
        # (re-entry case) — re-run setup then "add from presets" without
        # duplicating columns.
        questions = list(cur_part.get("questions") or [])
        cap = None if is_premium_flag else _FREE_QUESTION_CAP
        additions = await _setup()._run_participation_preset_picker_step(
            channel,
            bot,
            user,
            cancel_event,
            cmd_name=cmd_name,
            is_premium_flag=is_premium_flag,
            existing_questions=questions,
            cap=cap,
        )
        if additions is None:
            raise _Abort
        questions.extend(additions)

        await _run_builder_loop(w, questions, parent_cmd=parent_cmd, cap=cap)
    except _Abort:
        return None

    return {
        "enabled": 1,
        "tab_name": tab_name,
        "questions": questions,
        "roster_tab": roster_tab,
        "roster_name_col": name_col,
        "roster_alias_col": alias_col,
        "roster_start_row": start_row,
    }

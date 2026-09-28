"""
The Shiny Tasks wizard (`run_shiny_tasks_setup`): the re-entry summary,
enable or disable (with the saved settings kept), the announcement
channel, the warzone group from the game's fixed list (#604), the post
time, the message template, the final review and the save. Free for all
tiers.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup.py` set: `setup_cog` imports
`run_shiny_tasks_setup` back under its old name for the hub, the
launcher and the tests; the shared wizard pieces come from
`wizard_steps` and the clock text from `wizard_time`; this module
reaches into `setup_cog` at call time (`_setup()`) only for the re-entry
summary and the disable-with-clear helper.

Step 3 was a two-field modal for the lowest and highest server number
until #604, when one alliance typed 1-2308 and its post outgrew what
Discord will send. It is a pick from the game's warzone groups now.
`tests/integration/test_shiny_tasks_setup.py` holds the prompts, labels,
acknowledgements, saved fields and summary lines.
"""

import asyncio
from dataclasses import dataclass

import discord

import config_health
import premium
import shiny_tasks
import wizard_registry
import wizard_steps
from messages import (
    CANCEL_PLAIN,
    PREV_CHANNEL_GONE,
    TIME_PARSE_GIVE_UP,
    TIME_PARSE_RETRY,
    WIZARD_TIMEOUT,
)
from setup_hub import HUB_BTN_SHINY
from wizard_registry import OwnedView, wait_view_or_cancel
from wizard_time import _format_24h_to_12h, _format_time_with_tz, _parse_12h_time


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


TIMEOUT_MSG = WIZARD_TIMEOUT.format(wizard=HUB_BTN_SHINY)
RECOVERY = f"`/setup` → {HUB_BTN_SHINY}"
DEFAULT_TZ = "America/New_York"


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

    async def keep_or_change(self, prompt: str, **kw) -> str:
        """One `ask_keep_or_change` question. The helper posts its own cancel
        and timeout notice; an abandoned one just raises."""
        picked = await wizard_steps.ask_keep_or_change(
            self.channel,
            prompt,
            timeout_cmd="setup_shiny_tasks",
            cancel_event=self.cancel_event,
            **kw,
        )
        if picked is None:
            raise _Abort
        return picked


@dataclass
class _Saved:
    current: dict
    already_configured: bool
    guild_tz: str


@dataclass
class _Answers:
    channel_id: int = 0
    server_min: int = 0
    server_max: int = 0
    post_time: str = "09:00"
    message_template: str = ""


# ── Views ────────────────────────────────────────────────────────────────────


def _span(lo, hi) -> str:
    """A range the way every Shiny surface shows it."""
    return f"{lo} – {hi}"


GROUP_STEP_TEXT = (
    "**Step 3 of 6 — Warzone Group**\n"
    "Pick your alliance's warzone group. The daily post lists the warzones in it "
    "that have shiny tasks that day."
)
GROUP_PLACEHOLDER = "Pick your warzone group..."
GROUP_CONFIRM = "✅ Use this group"


class WarzoneGroupView(OwnedView):
    """Step 3 (#604): the warzone group, from the game's fixed list.

    A select plus a confirm button rather than a select that acts on change:
    a mis-tap on a phone is otherwise unrecoverable (DESIGN.md, Selects).
    Keep current is offered when the saved range is exactly one of the
    groups; a narrower slice saved before the groups existed keeps posting
    until the step is re-run, and then a group is picked. No timeout hint:
    the wizard posts its own timeout line, as every other step does.
    """

    def __init__(self, owner_id: int, *, current: tuple[int, int] | None = None):
        super().__init__(timeout=wizard_steps.WIZARD_STEP_TIMEOUT)
        self.owner_id = owner_id
        self.current = current
        self.selected: tuple[int, int] | None = None
        self.confirmed = False
        self._pick: tuple[int, int] | None = None

        row = 0
        if current:
            self.add_button(
                f"Keep current: {_span(*current)}", discord.ButtonStyle.success, self._keep, row=0
            )
            row = 1
        self._select = discord.ui.Select(
            placeholder=GROUP_PLACEHOLDER,
            options=[
                discord.SelectOption(label=f"Warzones {_span(lo, hi)}", value=f"{lo}-{hi}")
                for lo, hi in shiny_tasks.WARZONE_GROUPS
            ],
            row=row,
        )
        self._select.callback = self._on_select
        self.add_item(self._select)
        self._confirm = self.add_button(
            GROUP_CONFIRM,
            discord.ButtonStyle.primary,
            self._on_confirm,
            row=row + 1,
            disabled=True,
        )

    def _finish(self) -> None:
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        self.stop()

    async def _keep(self, interaction: discord.Interaction):
        self.selected = self.current
        self._finish()
        await wizard_registry.safe_edit_response(
            interaction, content=f"✅ Keeping warzones: **{_span(*self.current)}**", view=self
        )

    async def _on_select(self, interaction: discord.Interaction):
        lo, hi = (int(n) for n in self._select.values[0].split("-"))
        self._pick = (lo, hi)
        for option in self._select.options:
            option.default = option.value == f"{lo}-{hi}"
        self._confirm.disabled = False
        await wizard_registry.safe_edit_response(interaction, view=self)

    async def _on_confirm(self, interaction: discord.Interaction):
        if self._pick is None:
            return
        self.selected = self._pick
        self._finish()
        await wizard_registry.safe_edit_response(
            interaction, content=f"✅ Warzone group: **{_span(*self.selected)}**", view=self
        )


# ── Steps ────────────────────────────────────────────────────────────────────


async def _confirm_reentry(w: _Wizard, s: _Saved) -> None:
    """If already enabled, show the summary and offer edit or cancel."""
    current = s.current
    if not (s.already_configured and current.get("enabled")):
        return
    ch_id = current.get("channel_id", 0) or 0
    fields = [
        ("Channel", f"<#{ch_id}>" if ch_id else "*not set*"),
        (
            "Warzones",
            f"{current.get('server_min') or '?'} – {current.get('server_max') or '?'}",
        ),
        ("Post Time", _format_time_with_tz(current.get("post_time"), s.guild_tz) or "*not set*"),
        ("Message", "Custom" if (current.get("message_template") or "").strip() else "Default"),
    ]
    proceed = await _setup().ask_proceed_with_existing_config(
        w.channel,
        title="🌟 Current Shiny Tasks Setup",
        description="The daily shiny-tasks announcement is already configured. Would you like to edit these settings?",
        fields=fields,
        cancel_event=w.cancel_event,
        no_changes_message="✅ No changes made. The daily announcement is still active.",
    )
    if proceed is not True:
        raise _Abort


async def _ask_enable(w: _Wizard, s: _Saved) -> None:
    """Step 1. A No saves the row disabled with the previously-saved
    range, channel and the rest kept, so the next run can offer them back
    as "current"; then the clear button, and the wizard ends."""
    enabled = await w.ask(
        "**Step 1 of 6 — Enable daily shiny tasks announcement?**",
        wizard_steps.YesNoView(),
        "selected",
    )
    if enabled:
        return
    from config import save_shiny_tasks_config, clear_shiny_tasks_config

    current = s.current
    save_shiny_tasks_config(
        w.guild_id,
        enabled=0,
        channel_id=current.get("channel_id", 0),
        post_time=current.get("post_time", "09:00"),
        server_min=current.get("server_min", 0),
        server_max=current.get("server_max", 0),
        message_template=current.get("message_template", ""),
    )
    await _setup().ask_disable_with_clear(
        w.channel,
        feature_label="Shiny tasks announcement",
        setup_command="setup → 🌟 Shiny Tasks",
        had_prior_config=s.already_configured,
        clear_fn=lambda: clear_shiny_tasks_config(w.guild_id),
        cancel_event=w.cancel_event,
    )
    raise _Abort


async def _ask_channel(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 2."""
    is_premium_flag = await premium.is_premium(
        w.guild_id, interaction=w.interaction, bot=w.interaction.client
    )
    await w.channel.send(
        "**Step 2 of 6 — Announcement Channel**\n"
        "Pick the channel where the daily shiny tasks post should be posted."
    )
    view = wizard_steps.ChannelSelectStep(
        "Select the shiny tasks channel...",
        suggested_name="shiny-tasks",
        include_threads=is_premium_flag,
        guild=w.interaction.guild,
        current_id=s.current.get("channel_id", 0) or 0,
    )
    if view.is_current_stale:
        await w.channel.send(PREV_CHANNEL_GONE.format(channel_label="shiny tasks"))
    await w.channel.send("​", view=view)
    await w.wait(view)
    if not view.confirmed:
        await w.channel.send(TIMEOUT_MSG)
        raise _Abort
    a.channel_id = view.selected_channel.id


async def _ask_warzone_group(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 3: pick a warzone group (#604)."""
    current = shiny_tasks.group_for_range(s.current.get("server_min"), s.current.get("server_max"))
    view = WarzoneGroupView(w.user.id, current=current)
    view.message = await w.channel.send(GROUP_STEP_TEXT, view=view)
    await w.wait(view)
    if not view.confirmed or view.selected is None:
        await w.channel.send(TIMEOUT_MSG)
        raise _Abort
    a.server_min, a.server_max = view.selected


async def _ask_post_time(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 4. Re-prompts up to 3 times on unparseable input rather than
    silently falling back to a default."""
    tz_label = wizard_steps.TIMEZONE_LABELS.get(s.guild_tz, "ET")
    attempts_left = 3
    while True:
        raw = await w.keep_or_change(
            f"**Step 4 of 6 — Post Time**\n"
            f"What time of day should the announcement post? "
            f"*(in your timezone: {tz_label})*\n"
            f"*(e.g. `9:00am`, `10:30am`, `9:00pm`)*",
            default="9:00am",
            # DB stores '09:00' (24h) — render as '9:00am' before showing
            # so 'Keep current' and 'Use default' don't sit side-by-side
            # in mismatched formats.
            current=_format_24h_to_12h(s.current.get("post_time", "")),
            modal_title="Post Time",
            modal_label="Time",
        )
        parsed = _parse_12h_time(raw)
        if parsed:
            a.post_time = parsed
            return
        if len(raw) == 5 and raw[2] == ":" and raw.replace(":", "").isdigit():
            a.post_time = raw  # already 24h
            return
        attempts_left -= 1
        if attempts_left <= 0:
            await w.channel.send(TIME_PARSE_GIVE_UP.format(recovery=RECOVERY))
            raise _Abort
        await w.channel.send(TIME_PARSE_RETRY.format(raw=raw))


async def _ask_message(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 5. Stores empty when the officer picks the default so a future
    change to `DEFAULT_SHINY_TASKS_MESSAGE` propagates instead of freezing
    today's wording in the DB."""
    from defaults import DEFAULT_SHINY_TASKS_MESSAGE

    saved = (s.current.get("message_template") or "").strip()
    picked = await w.keep_or_change(
        "**Step 5 of 6 — Announcement Message**\n"
        "Customize the announcement body, or use the default. "
        "Placeholders: `{warzones}` and `{date}`.",
        default=DEFAULT_SHINY_TASKS_MESSAGE,
        current=saved or None,
        modal_title="Shiny Tasks Message",
        modal_label="Message body",
    )
    a.message_template = (
        "" if picked.strip() == DEFAULT_SHINY_TASKS_MESSAGE.strip() else picked.strip()
    )


async def _confirm(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 6: the final review. A declined or timed-out review both post
    the cancel line (`not confirmed` covers None)."""
    from defaults import DEFAULT_SHINY_TASKS_MESSAGE

    embed = discord.Embed(
        title="🌟 Shiny Tasks — Final Review",
        description="Confirm to save this configuration.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Status", value="✅ Enabled", inline=True)
    embed.add_field(name="Channel", value=f"<#{a.channel_id}>", inline=True)
    embed.add_field(name="Warzones", value=_span(a.server_min, a.server_max), inline=True)
    embed.add_field(
        name="Post Time", value=_format_time_with_tz(a.post_time, s.guild_tz), inline=True
    )
    embed.add_field(
        name="Message",
        value=(a.message_template or DEFAULT_SHINY_TASKS_MESSAGE)[:1024],
        inline=False,
    )
    view = wizard_steps.ConfirmView()
    await w.channel.send(embed=embed, view=view)
    await w.wait(view)
    if not view.confirmed:
        await w.channel.send(f"{CANCEL_PLAIN} Run {RECOVERY} to start again.")
        raise _Abort


def _save(w: _Wizard, a: _Answers) -> None:
    from config import save_shiny_tasks_config

    save_shiny_tasks_config(
        w.guild_id,
        enabled=1,
        channel_id=a.channel_id,
        post_time=a.post_time,
        server_min=a.server_min,
        server_max=a.server_max,
        message_template=a.message_template,
    )
    # A group always fits in one post, so a too-wide notice is resolved now
    # rather than at the next post time (#604).
    config_health.clear(w.guild_id, shiny_tasks.SHINY_WARZONE_RANGE_SUBJECT)


# ── The wizard ───────────────────────────────────────────────────────────────


async def run_shiny_tasks_setup(interaction: discord.Interaction, bot):
    """Walk leadership through configuring the daily shiny-tasks
    announcement. Six steps: enable → channel → warzone group → post
    time → message template → confirm. Free for all tiers."""
    from config import get_config, get_shiny_tasks_config, has_shiny_tasks_config

    w = _Wizard(
        interaction=interaction,
        channel=interaction.channel,
        user=interaction.user,
        guild_id=interaction.guild_id,
        cancel_event=wizard_registry.register(interaction.user.id),
    )
    cfg = get_config(w.guild_id)
    s = _Saved(
        current=get_shiny_tasks_config(w.guild_id),
        already_configured=has_shiny_tasks_config(w.guild_id),
        guild_tz=cfg.timezone if cfg else DEFAULT_TZ,
    )
    a = _Answers()
    try:
        await _confirm_reentry(w, s)
        await w.channel.send(
            "🌟 **Daily Shiny Tasks Setup**\n"
            "Each day, the bot can post the list of Last War warzones where "
            "shiny tasks are available, filtered to your alliance's warzone "
            "group."
        )
        await _ask_enable(w, s)
        await _ask_channel(w, s, a)
        await _ask_warzone_group(w, s, a)
        await _ask_post_time(w, s, a)
        await _ask_message(w, s, a)
        await _confirm(w, s, a)
    except _Abort:
        wizard_registry.unregister(w.user.id, w.cancel_event)
        return

    _save(w, a)
    human = _format_time_with_tz(a.post_time, s.guild_tz) or a.post_time
    await w.channel.send(f"✅ Shiny-tasks announcement saved! The first post will fire at {human}.")
    wizard_registry.unregister(w.user.id, w.cancel_event)
    print(f"[SETUP] Shiny tasks config saved for guild {w.guild_id}")

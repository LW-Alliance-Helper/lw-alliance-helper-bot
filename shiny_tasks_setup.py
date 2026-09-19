"""
The Shiny Tasks wizard (`run_shiny_tasks_setup`): the re-entry summary,
enable or disable (with the saved settings kept), the announcement
channel, the server range through its two-field modal, the post time,
the message template, the final review and the save. Free for all tiers.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup.py` set: `setup_cog` imports
`run_shiny_tasks_setup` back under its old name for the hub, the
launcher and the tests; the shared wizard pieces come from
`wizard_steps` and the clock text from `wizard_time`; this module
reaches into `setup_cog` at call time (`_setup()`) only for the re-entry
summary and the disable-with-clear helper. `ServerRangeModal` is a
module class under its old name.

Every prompt, label, acknowledgement, saved field and summary line is
unchanged from the function this replaced;
`tests/integration/test_shiny_tasks_setup.py` was written against it and
holds this module to it.
"""

import asyncio
from dataclasses import dataclass

import discord

import premium
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
from wizard_registry import wait_view_or_cancel
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


class ServerRangeModal(discord.ui.Modal):
    """Step 3: the lowest and highest reachable server numbers in one modal."""

    def __init__(self, min_default: str = "", max_default: str = ""):
        super().__init__(title="Server Range")
        self.min_value = None
        self.max_value = None
        self._min = discord.ui.TextInput(
            label="Lowest reachable server number",
            placeholder="e.g. 677",
            default=min_default,
            required=True,
            max_length=5,
        )
        self._max = discord.ui.TextInput(
            label="Highest reachable server number",
            placeholder="e.g. 804",
            default=max_default,
            required=True,
            max_length=5,
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
            "Server Range",
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


def _range_launcher(saved_min, saved_max) -> wizard_steps.ModalLaunchView:
    """The server-range modal behind a launcher, with Keep current when a
    usable range is saved. `ServerRangeModal.value` is a read-only
    property derived from min_value + max_value, so the launcher's
    default `modal.value = current_value` path won't work; the
    `on_keep_current` callback populates the underlying attributes."""
    modal = ServerRangeModal(
        min_default=str(saved_min) if saved_min else "",
        max_default=str(saved_max) if saved_max else "",
    )
    try:
        has_saved_range = int(saved_min) >= 1 and int(saved_max) >= int(saved_min)
    except (TypeError, ValueError):
        has_saved_range = False
    if has_saved_range:
        keep_min, keep_max = int(saved_min), int(saved_max)

        def _keep_range(m, _min=keep_min, _max=keep_max):
            m.min_value = str(_min)
            m.max_value = str(_max)

        launcher = wizard_steps.ModalLaunchView(
            modal,
            current_value=f"{keep_min} – {keep_max}",
            current_display=f"{keep_min} – {keep_max}",
            on_keep_current=_keep_range,
        )
    else:
        launcher = wizard_steps.ModalLaunchView(modal)
    # Override the generic "Enter Value" button label so leadership sees
    # domain wording. Found by label because the Keep-current button
    # (when present) is not at a fixed index.
    for child in launcher.children:
        if isinstance(child, discord.ui.Button) and child.label == "✏️ Enter Value":
            child.label = "✏️ Enter Server Numbers"
            break
    return launcher


async def _ask_server_range(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 3. Re-prompts up to 3 times; the retry modal is pre-filled
    with whatever the officer typed, so the correction is one tap away
    instead of re-entering both fields."""
    saved_min = s.current.get("server_min") or 0
    saved_max = s.current.get("server_max") or 0
    attempts_left = 3
    while True:
        launcher = _range_launcher(saved_min, saved_max)
        await w.channel.send(
            "**Step 3 of 6 — Server Range**\n"
            "Enter the lowest and highest server numbers your alliance can "
            "reach. Typically your transfer range.",
            view=launcher,
        )
        await w.wait(launcher)
        if not launcher.confirmed:
            await w.channel.send(TIMEOUT_MSG)
            raise _Abort

        min_raw = (launcher.modal.min_value or "").strip()
        max_raw = (launcher.modal.max_value or "").strip()
        try:
            candidate_min = int(min_raw)
            candidate_max = int(max_raw)
            valid_numbers = True
        except (TypeError, ValueError):
            valid_numbers = False

        if valid_numbers and candidate_min >= 1 and candidate_min <= candidate_max:
            a.server_min = candidate_min
            a.server_max = candidate_max
            return

        attempts_left -= 1
        if attempts_left <= 0:
            await w.channel.send(
                "⚠️ Could not read those server numbers after a few tries. "
                "Run `/setup` → 🌟 Shiny Tasks to start over."
            )
            raise _Abort

        saved_min = min_raw
        saved_max = max_raw
        if not valid_numbers:
            await w.channel.send(
                f"⚠️ Could not read **`{min_raw}`** / **`{max_raw}`** as whole "
                f"numbers. Try something like `677` and `804`. Let's try once more."
            )
        else:
            await w.channel.send(
                f"⚠️ The lowest server (**`{min_raw}`**) must be ≥ 1 and "
                f"≤ the highest (**`{max_raw}`**). Let's try once more."
            )


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
        "Placeholders: `{servers}` and `{date}`.",
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
    embed.add_field(name="Server Range", value=f"{a.server_min} – {a.server_max}", inline=True)
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


# ── The wizard ───────────────────────────────────────────────────────────────


async def run_shiny_tasks_setup(interaction: discord.Interaction, bot):
    """Walk leadership through configuring the daily shiny-tasks
    announcement. Six steps: enable → channel → server range → post
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
            "Each day, the bot can post the list of Last War servers where "
            "shiny tasks are available, filtered to the servers your alliance "
            "can reach."
        )
        await _ask_enable(w, s)
        await _ask_channel(w, s, a)
        await _ask_server_range(w, s, a)
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

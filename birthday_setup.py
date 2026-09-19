"""
The birthday wizard (`run_birthday_setup`): the re-entry summary, enable
or disable (with the saved settings kept), the sheet tab and the name and
birthday columns, train integration with its placement and lookahead,
reminders with their channel, time and the Premium birthday DM, the save
and the confirmation embed.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup.py` set: `setup_cog` imports `run_birthday_setup`
back under its old name for the hub, the launcher and the tests; the
shared wizard pieces come from `wizard_steps` and the clock text from
`wizard_time`; this module reaches into `setup_cog` at call time
(`_setup()`) only for what still lives there (the re-entry summary, the
disable-with-clear helper, the tab-claim warning, the column-letter
helpers).

Shape:
* `_Wizard` carries the handles every question needs, plus `_Saved` (what
  was read before asking) and `_Answers` (what the officer chose).
* One `_ask_*` function per step; the placement view is a module class
  under its old name.
* A cancelled or timed-out question raises `_Abort`; `run_birthday_setup`
  unregisters the cancel event and returns, the way the old function did
  on its clean exits (it left the event registered on most aborts).

Every prompt, label, acknowledgement, saved field and summary line is
unchanged from the function this replaced;
`tests/integration/test_birthday_setup.py` was written against it and
holds this module to it.
"""

import asyncio
from dataclasses import dataclass

import discord

import premium
import wizard_registry
import wizard_steps
from messages import (
    INPUT_INVALID,
    PREV_CHANNEL_GONE,
    SETUP_POINTER_FOOTER,
    TIME_PARSE_GIVE_UP,
    TIME_PARSE_RETRY,
    WIZARD_TIMEOUT,
)
from setup_hub import HUB_BTN_BIRTHDAYS
from wizard_registry import wait_view_or_cancel
from wizard_time import _format_24h_to_12h, _format_time_with_tz, _parse_12h_time


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


TIMEOUT_MSG = WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS)
RECOVERY = f"`/setup` → {HUB_BTN_BIRTHDAYS}"
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

    async def ask_yes_no(self, text: str) -> bool:
        return bool(await self.ask(text, wizard_steps.YesNoView(), "selected"))

    async def keep_or_change(self, prompt: str, **kw) -> str:
        """One `ask_keep_or_change` question. The helper posts its own cancel
        and timeout notice; an abandoned one just raises."""
        picked = await wizard_steps.ask_keep_or_change(
            self.channel,
            prompt,
            timeout_cmd="setup_birthdays",
            cancel_event=self.cancel_event,
            **kw,
        )
        if picked is None:
            raise _Abort
        return picked

    async def invalid(self, *, type: str, example: str) -> None:
        await self.channel.send(INPUT_INVALID.format(type=type, example=example, recovery=RECOVERY))
        raise _Abort


@dataclass
class _Saved:
    current: dict
    already_configured: bool
    guild_tz: str


@dataclass
class _Answers:
    tab_name: str = ""
    name_col: int = 0
    birthday_col: int = 0
    discord_id_col: int = -1
    train_integration: int = 0
    flexible_placement: int = 0
    lookahead_days: int = 14
    reminders_enabled: int = 0
    reminder_channel_id: int = 0
    reminder_time: str = "08:00"
    dm_message: str = ""


# ── Views ────────────────────────────────────────────────────────────────────


class PlacementView(discord.ui.View):
    """Step 6: what to do when the birthday is already taken on the train."""

    def __init__(self):
        super().__init__(timeout=120)
        self.selected = None

    async def _pick(self, inter: discord.Interaction, value: int, ack: str) -> None:
        self.selected = value
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, content=ack, view=self)
        self.stop()

    @discord.ui.button(label="🎂 Birthday only", style=discord.ButtonStyle.primary)
    async def birthday_only(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._pick(inter, 0, "✅ Placement: **Birthday only**")

    @discord.ui.button(label="↔️ Assign nearby if taken", style=discord.ButtonStyle.secondary)
    async def flexible(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._pick(
            inter, 1, "✅ Placement: **Assign 1 day before or after if birthday is taken**"
        )


# ── Steps ────────────────────────────────────────────────────────────────────


def _letter(idx) -> str:
    """A saved column index as the letter Keep current shows; blank when
    nothing usable is saved."""
    if isinstance(idx, int) and idx >= 0:
        return _setup()._col_index_to_letter(idx)
    return ""


async def _confirm_reentry(w: _Wizard, s: _Saved) -> None:
    """If already enabled, show the summary and offer edit or cancel."""
    current = s.current
    if not (s.already_configured and current.get("enabled")):
        return
    col = _setup()._col_index_to_letter
    rc = current.get("reminder_channel_id", 0) or 0
    fields = [
        ("Sheet Tab", current.get("tab_name") or "*not set*"),
        ("Name Column", col(current.get("name_col", 0))),
        ("Birthday Column", col(current.get("birthday_col", 0))),
        ("Train Integration", "✅ Enabled" if current.get("train_integration") else "❌ Disabled"),
    ]
    if current.get("train_integration"):
        fields.append(
            (
                "Placement",
                "Flexible (±1 day)" if current.get("flexible_placement") else "Birthday only",
            )
        )
        fields.append(("Lookahead", f"{current.get('lookahead_days', 14)} days"))
    fields.append(
        ("Reminders", "✅ Enabled" if current.get("reminders_enabled") else "❌ Disabled")
    )
    if current.get("reminders_enabled"):
        fields.append(("Reminder Channel", f"<#{rc}>" if rc else "*not set*"))
        fields.append(
            (
                "Reminder Time",
                _format_time_with_tz(current.get("reminder_time"), s.guild_tz) or "*not set*",
            )
        )
    proceed = await _setup().ask_proceed_with_existing_config(
        w.channel,
        title="🎂 Current Birthday Setup",
        description="Birthday tracking is already configured. Would you like to edit these settings?",
        fields=fields,
        cancel_event=w.cancel_event,
        no_changes_message="✅ No changes made. Birthday tracking is still active.",
    )
    if proceed is not True:
        raise _Abort


async def _ask_enable(w: _Wizard, s: _Saved) -> None:
    """Step 1. A No saves the row disabled with every other setting kept,
    offers the clear button, and ends the wizard."""
    enabled = await w.ask_yes_no(
        "**Step 1 of 9 — Enable birthday tracking?**\n"
        "Should the bot track member birthdays from your Google Sheet?"
    )
    if enabled:
        return
    from config import save_birthday_config, clear_birthday_config

    # `last_train_population_date` and `ignored_conflicts` are operational
    # state owned by the train-auto-pop scheduler and the conflict alert
    # (see #89, #334) — not `save_birthday_config` parameters. Strip them
    # from the splat so the disable path still works when those columns
    # exist on the loaded row.
    save_birthday_config(
        w.guild_id,
        enabled=0,
        **{
            k: v
            for k, v in s.current.items()
            if k not in ("guild_id", "enabled", "last_train_population_date", "ignored_conflicts")
        },
    )
    await _setup().ask_disable_with_clear(
        w.channel,
        feature_label="Birthday tracking",
        setup_command=f"setup → {HUB_BTN_BIRTHDAYS}",
        had_prior_config=s.already_configured,
        clear_fn=lambda: clear_birthday_config(w.guild_id),
        cancel_event=w.cancel_event,
    )
    raise _Abort


async def _ask_tab(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 2."""
    a.tab_name = await w.keep_or_change(
        "**Step 2 of 9 — Sheet Tab**\n"
        "Which tab in your Google Sheet contains birthday data?\n"
        "⚠️ *Make sure this tab exists in your sheet before continuing.*",
        default="Birthdays",
        current=s.current.get("tab_name", ""),
        modal_title="Sheet Tab Name",
        modal_label="Tab name",
    )
    await _setup().warn_if_tab_claimed(
        w.channel, w.guild_id, a.tab_name, exclude_field="birthday_tab_name"
    )


async def _ask_columns(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 3 and 4: the name and birthday columns, as letters."""
    # discord_id_col is no longer asked in the wizard — preserve any existing
    # value so save_birthday_config doesn't clobber it.
    a.discord_id_col = s.current.get("discord_id_col", -1)
    to_index = _setup()._col_letter_to_index

    raw = await w.keep_or_change(
        "**Step 3 of 9 — Name Column**\nWhich column contains the member's name?",
        default="A",
        current=_letter(s.current.get("name_col")),
        modal_title="Name Column",
        modal_label="Column letter",
    )
    a.name_col = to_index(raw)
    if a.name_col < 0:
        await w.invalid(type="single column letter", example="A")

    raw = await w.keep_or_change(
        "**Step 4 of 9 — Birthday Column**\n"
        "Which column contains the member's birthday?\n"
        "ℹ️ *The bot accepts most date formats: `12/7`, `12-7`, `Dec 7`, "
        "`December 7`, `1990-12-07`, etc. Bare numeric dates like `7/12` are "
        "read as **M/D** (July 12) — use `Dec 7` if your alliance writes "
        "day-first.*",
        default="B",
        current=_letter(s.current.get("birthday_col")),
        modal_title="Birthday Column",
        modal_label="Column letter",
    )
    a.birthday_col = to_index(raw)
    if a.birthday_col < 0:
        await w.invalid(type="single column letter", example="B")


async def _ask_train_integration(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 5 to 7: train integration, then placement and lookahead when on."""
    on = await w.ask_yes_no(
        "**Step 5 of 9 — Train Schedule Integration**\n"
        "Should the bot automatically add members to the train schedule on their birthday?"
    )
    a.train_integration = 1 if on else 0
    if not on:
        await w.channel.send(
            "ℹ️ *Skipping Steps 6–7 (placement and lookahead) — train integration is off.*"
        )
        return

    await w.channel.send(
        "ℹ️ Heads up: birthdays auto-populate the train schedule **once per day** "
        "(on the bot's first tick after server-time midnight). If you need a "
        "birthday reflected on the schedule sooner, open `/train` and click "
        "**🎂 Run birthday check** to trigger the check on demand."
    )
    a.flexible_placement = await w.ask(
        "**Step 6 of 9 — Birthday Placement**\n"
        "If the member's birthday is already taken on the train schedule, what should the bot do?",
        PlacementView(),
        "selected",
    )
    raw = await w.keep_or_change(
        "**Step 7 of 9 — Train Schedule Lookahead**\n"
        "Since you enabled train integration, how many days ahead of a "
        "member's birthday should the bot pre-populate them on the train "
        "schedule? This only applies to train-integration auto-placement; "
        "the birthday announcement itself always fires on the day.\n"
        "*(we recommend 14)*",
        default="14",
        current=str(s.current.get("lookahead_days") or ""),
        modal_title="Lookahead Days",
        modal_label="Number of days",
    )
    try:
        a.lookahead_days = int(str(raw).strip())
        if a.lookahead_days < 1:
            raise ValueError
    except ValueError:
        await w.invalid(type="number", example="14")


async def _ask_reminders(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 8 to 9: reminders, then the channel, time and DM when on."""
    on = await w.ask_yes_no(
        "**Step 8 of 9 — Birthday Reminders**\n"
        "Should the bot post a message in Discord on a member's birthday?\n"
        '*(It will post: "🎂 Today is **[name]**\'s birthday!")*'
    )
    a.reminders_enabled = 1 if on else 0
    if not on:
        await w.channel.send(
            "ℹ️ *Skipping Steps 8a–8b (reminder channel and time) — birthday reminders are off.*"
        )
        return
    await _ask_reminder_channel(w, s, a)
    await _ask_reminder_time(w, s, a)
    await _ask_dm(w, s, a)


async def _ask_reminder_channel(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 8a."""
    is_premium_flag = await premium.is_premium(
        w.guild_id, interaction=w.interaction, bot=w.interaction.client
    )
    view = wizard_steps.ChannelSelectStep(
        "Select the birthday announcement channel...",
        suggested_name="birthdays",
        include_threads=is_premium_flag,
        guild=w.interaction.guild,
        current_id=s.current.get("reminder_channel_id", 0) or 0,
    )
    if view.is_current_stale:
        await w.channel.send(PREV_CHANNEL_GONE.format(channel_label="birthday"))
    await w.channel.send(
        "**Step 8a of 9 — Birthday Announcement Channel**\n"
        "Which channel should birthday announcements be posted in?",
        view=view,
    )
    await w.wait(view)
    if not view.confirmed:
        await w.channel.send(TIMEOUT_MSG)
        raise _Abort
    a.reminder_channel_id = view.selected_channel.id


async def _ask_reminder_time(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 8b. Re-prompts up to 3 times on unparseable input rather than
    silently falling back to a default."""
    tz_label = wizard_steps.TIMEZONE_LABELS.get(s.guild_tz, "your timezone")
    attempts_left = 3
    while True:
        raw = await w.keep_or_change(
            f"**Step 8b of 9 — Reminder Time**\n"
            f"What time should birthday announcements be posted? *(in {tz_label})*\n"
            f"*(e.g. `8:00am`, `12:00pm`)*",
            default="8:00am",
            # DB stores 24h ("08:00") — render as "8:00am" so the
            # Keep-current and Use-default button labels don't sit
            # side-by-side in mismatched formats.
            current=_format_24h_to_12h(s.current.get("reminder_time", "")),
            modal_title="Reminder Time",
            modal_label="Time",
        )
        parsed = _parse_12h_time(raw)
        if parsed:
            a.reminder_time = parsed
            return
        if len(raw) == 5 and raw[2] == ":" and raw.replace(":", "").isdigit():
            a.reminder_time = raw  # already 24h
            return
        attempts_left -= 1
        if attempts_left <= 0:
            await w.channel.send(TIME_PARSE_GIVE_UP.format(recovery=RECOVERY))
            raise _Abort
        await w.channel.send(TIME_PARSE_RETRY.format(raw=raw))


async def _ask_dm(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 9: the per-member birthday DM (💎 Premium). Free guilds can
    configure it now — it just won't fire until they have Premium + Member
    Sync AND a Discord ID column wired up in the birthday sheet."""
    from train_cog import DEFAULT_BIRTHDAY_DM

    picked = await w.keep_or_change(
        "**Step 9 of 9 — Birthday DM Body (💎 Premium)**\n"
        "When a birthday fires, the bot also DMs the member directly with a personal "
        "note. Free guilds can configure this now — it just won't fire until you have "
        "Premium + Member Sync + a Discord ID column in your birthday sheet.\n\n"
        "Use `{name}` as a placeholder for the member's name.",
        default=DEFAULT_BIRTHDAY_DM,
        current=(s.current.get("dm_message") or "").strip(),
        modal_title="Birthday DM Body",
        modal_label="DM body (max 1000 chars)",
    )
    a.dm_message = "" if picked == DEFAULT_BIRTHDAY_DM else picked


# ── Save and summary ─────────────────────────────────────────────────────────


def _save(w: _Wizard, a: _Answers) -> None:
    from config import save_birthday_config

    save_birthday_config(
        guild_id=w.guild_id,
        tab_name=a.tab_name,
        name_col=a.name_col,
        birthday_col=a.birthday_col,
        discord_id_col=a.discord_id_col,
        data_start_row=2,
        enabled=1,
        train_integration=a.train_integration,
        flexible_placement=a.flexible_placement,
        lookahead_days=a.lookahead_days,
        reminders_enabled=a.reminders_enabled,
        reminder_channel_id=a.reminder_channel_id,
        reminder_time=a.reminder_time,
        dm_message=a.dm_message,
    )


def _summary_embed(s: _Saved, a: _Answers) -> discord.Embed:
    col = _setup()._col_index_to_letter
    embed = discord.Embed(title="✅ Birthday Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab", value=a.tab_name, inline=True)
    embed.add_field(name="Name Column", value=f"Column {col(a.name_col)}", inline=True)
    embed.add_field(name="Birthday Column", value=f"Column {col(a.birthday_col)}", inline=True)
    embed.add_field(
        name="Discord ID Column",
        value=f"Column {col(a.discord_id_col)}" if a.discord_id_col >= 0 else "Not stored",
        inline=True,
    )
    embed.add_field(
        name="Train Integration",
        value="Enabled" if a.train_integration else "Disabled",
        inline=True,
    )
    if a.train_integration:
        embed.add_field(
            name="Placement",
            value="Flexible (±1 day)" if a.flexible_placement else "Birthday only",
            inline=True,
        )
        embed.add_field(name="Lookahead", value=f"{a.lookahead_days} days", inline=True)
    embed.add_field(
        name="Reminders", value="Enabled" if a.reminders_enabled else "Disabled", inline=True
    )
    if a.reminders_enabled:
        embed.add_field(name="Reminder Channel", value=f"<#{a.reminder_channel_id}>", inline=True)
        embed.add_field(
            name="Reminder Time",
            value=_format_time_with_tz(a.reminder_time, s.guild_tz),
            inline=True,
        )
    embed.set_footer(text=SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_BIRTHDAYS))
    return embed


# ── The wizard ───────────────────────────────────────────────────────────────


async def run_birthday_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring birthday tracking."""
    from config import get_birthday_config, has_birthday_config, get_config

    w = _Wizard(
        interaction=interaction,
        channel=interaction.channel,
        user=interaction.user,
        guild_id=interaction.guild_id,
        cancel_event=wizard_registry.register(interaction.user.id),
    )
    guild_cfg = get_config(w.guild_id)
    s = _Saved(
        current=get_birthday_config(w.guild_id),
        already_configured=has_birthday_config(w.guild_id),
        guild_tz=guild_cfg.timezone if guild_cfg else DEFAULT_TZ,
    )
    a = _Answers()
    try:
        await _confirm_reentry(w, s)
        await w.channel.send(
            "⚙️ **Birthday Tracking Setup**\nConfigure how the bot tracks member birthdays."
        )
        await _ask_enable(w, s)
        await _ask_tab(w, s, a)
        await _ask_columns(w, s, a)
        await _ask_train_integration(w, s, a)
        await _ask_reminders(w, s, a)
    except _Abort:
        wizard_registry.unregister(w.user.id, w.cancel_event)
        return

    _save(w, a)
    await w.channel.send(embed=_summary_embed(s, a))
    wizard_registry.unregister(w.user.id, w.cancel_event)
    print(f"[SETUP] Birthday config saved for guild {w.guild_id}")

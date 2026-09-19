"""
Structured-flow block of the storm setup wizard (Desert Storm and Canyon
Storm): the Premium opt-in for the structured roster flow (#38) and, once
opted in, the power data source, sub mode, sign-up channel and schedule,
Sheet tab names, power-refresh and stale-power DMs (#138, #255), roster DM
templates (#226) and the strategy preset / member rules library (#54, #144).

Split out of `setup_cog.py` in the #589 step 11 refactor, following the
1.8.0 precedent of `storm_strategy_ui.py` and `train_rotation_ui_*.py`: the
wizard lives in `storm_setup.run_storm_setup`, which calls
`run_structured_flow_step` as its eighth step. The shared wizard pieces
(the yes/no views, the channel picker, `ask_keep_or_change`) come from
`wizard_steps`; this module reaches back into `setup_cog` through
`_setup()` at call time only for what still lives there (the
column-letter helper, the inline-create offers), so
importing either module first works and every `patch("setup_cog.X")` on
those keeps its target. Tests patch the shared pieces on `wizard_steps`.

Shape:

* `_Wizard` carries the handles every question needs (channel, bot, user,
  cancel event, guild, event type, labels), replacing the twelve-parameter
  signature the old function had.
* Each question is one `_ask_*` function that posts its prompt, waits, and
  writes into the shared `result` dict.
* A cancelled or timed-out question raises `_Abort`; `run_structured_flow_step`
  turns that into the `None` the wizard expects. That replaces fourteen
  copies of the cancel / timeout / `return None` ladder.
* `_ChoiceView` is the one button-row view behind the power data source,
  sub mode, stale-days, last-updated source and DM template pickers, which
  were five inline copies of the same class.

Every prompt, button label and acknowledgement is unchanged from the
function this replaced; `tests/integration/test_storm_setup_structured.py`
was written against that function and holds this one to it.
"""

import asyncio
from dataclasses import dataclass
from typing import Callable

import discord

import wizard_registry
import wizard_steps
import wizard_time
from wizard_registry import wait_view_or_cancel
from messages import GENERIC_CMD_TIMEOUT
from storm_event_hub import HUB_COMMAND, HUB_BTN_PRESETS, HUB_BTN_RULES


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


# ── Shared wizard handles ────────────────────────────────────────────────────


@dataclass
class _Wizard:
    channel: discord.abc.Messageable
    bot: discord.Client
    user: discord.abc.User
    cancel_event: asyncio.Event
    guild_id: int
    event_type: str
    label: str
    cmd_name: str
    interaction_guild: discord.Guild | None

    @property
    def slug(self) -> str:
        """Internal slug for channel-name suggestions and hub pointers
        (`desertstorm` / `canyonstorm`). Derived from the event type so it
        stays clean after `cmd_name` became a `/setup` hub hint (#201)."""
        return "desertstorm" if self.event_type == "DS" else "canyonstorm"

    async def wait(self, view) -> None:
        """Wait for `view`, raising `_Abort` if the officer cancelled.
        A timeout is the caller's to interpret: some questions treat it
        as an answer (the inline-create offers), most as an exit."""
        await wait_view_or_cancel(view, self.cancel_event)
        if getattr(view, "cancelled", False):
            raise _Abort

    async def ask(self, text: str, view, attr: str):
        """Post `text` with `view`, wait, and return `view.<attr>`. A
        timeout (the attribute still `None`) posts the route back and
        raises `_Abort`."""
        await self.channel.send(text, view=view)
        await self.wait(view)
        value = getattr(view, attr)
        if value is None:
            await self.timed_out()
            raise _Abort
        return value

    async def timed_out(self) -> None:
        await self.channel.send(GENERIC_CMD_TIMEOUT.format(cmd=self.cmd_name))

    async def ask_yes_no(self, text: str, *, current: bool | None) -> bool:
        """A yes/no question. With no saved answer (`current is None`) it
        is the plain Yes / No view; with one, the Keep current / Switch
        gate, so a re-run never forces a re-pick."""
        if current is None:
            return bool(await self.ask(text, wizard_steps.YesNoView(), "selected"))
        gate = wizard_steps._KeepOrFlipYesNoGate(current_value=current)
        return bool(await self.ask(text, gate, "value"))

    async def ask_tab(self, text: str, *, key: str, result: dict, modal_title: str) -> None:
        """One Sheet-tab-name question through `ask_keep_or_change`,
        written straight into `result[key]`."""
        from config import default_structured_tab

        picked = await wizard_steps.ask_keep_or_change(
            self.channel,
            text,
            default=default_structured_tab(self.event_type, key),
            current=result.get(key, ""),
            modal_title=modal_title,
            modal_label="Tab name",
            timeout_cmd=self.cmd_name,
            cancel_event=self.cancel_event,
        )
        if picked is None:
            raise _Abort
        result[key] = picked


# ── Views ────────────────────────────────────────────────────────────────────


class _TextModal(discord.ui.Modal):
    """A modal of text inputs, each reachable afterwards as an attribute
    (`modal.tab_input.value`). `confirmed` flips on submit so a dismissed
    modal reads as no answer."""

    def __init__(self, title: str, fields: list[tuple[str, dict]]):
        super().__init__(title=title)
        self.confirmed = False
        for attr, kwargs in fields:
            item = discord.ui.TextInput(**kwargs)
            setattr(self, attr, item)
            self.add_item(item)

    async def on_submit(self, inter: discord.Interaction):
        self.confirmed = True
        await inter.response.defer()
        self.stop()


@dataclass(frozen=True)
class _Choice:
    """One button on a `_ChoiceView`. A choice either acknowledges in
    place (`ack` replaces the prompt text; `None` keeps it) or opens a
    modal, in which case the outcome only stands if the modal was
    submitted."""

    label: str
    outcome: str
    style: discord.ButtonStyle = discord.ButtonStyle.secondary
    ack: str | None = None
    modal: Callable[[], _TextModal] | None = None


class _ChoiceView(discord.ui.View):
    """A row of outcome buttons. `outcome` is the picked choice's name, or
    `None` on timeout or a dismissed modal; `modal` is the submitted modal
    when the pick opened one."""

    def __init__(self, choices: list[_Choice], *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.outcome: str | None = None
        self.modal: _TextModal | None = None
        for choice in choices:
            button = discord.ui.Button(label=choice.label[:80], style=choice.style)
            button.callback = self._picker(choice)
            self.add_item(button)

    def _picker(self, choice: _Choice):
        async def pick(inter: discord.Interaction):
            if choice.modal is not None:
                self.modal = choice.modal()
                await inter.response.send_modal(self.modal)
                await self.modal.wait()
                self.outcome = choice.outcome if self.modal.confirmed else None
                self._disable()
                try:
                    if inter.message:
                        await inter.message.edit(view=self)
                except discord.HTTPException:
                    pass
            else:
                self.outcome = choice.outcome
                self._disable()
                kwargs = {"view": self}
                if choice.ack is not None:
                    kwargs["content"] = choice.ack
                await wizard_registry.safe_edit_response(inter, **kwargs)
            self.stop()

        return pick

    def _disable(self) -> None:
        for item in self.children:
            item.disabled = True


# ── Pure helpers ─────────────────────────────────────────────────────────────


_RESULT_DEFAULTS = {
    "structured_flow_enabled": False,
    "power_metric_column": "B",
    "power_metric_tab": "",
    "power_match_column": "",
    "sub_mode": "pool",
    "signup_channel_id": 0,
    "signup_schedule_cron": "",
    "signups_tab": "",
    "rosters_tab": "",
    "attendance_tab": "",
    "strategies_tab": "",
    "member_rules_tab": "",
    "poll_day_of_week": -1,
    "signup_time": "",
    "power_refresh_dm_enabled": False,
    # Stale-power DM nudge (#255). All four default to off; the wizard
    # asks about them only when the power-refresh DM itself is on.
    "power_last_updated_tab": "",
    "power_last_updated_column": "",
    "power_last_updated_match_column": "",
    "power_refresh_stale_days": 0,
    # Roster DM templates (#226 follow-up). Persisted by
    # `save_roster_dm_templates`, not `save_structured_storm_config`, but
    # carried in the same dict so the re-entry surface and the save site
    # get them together.
    "roster_dm_starter_template": "",
    "roster_dm_paired_sub_template": "",
    "roster_dm_pool_sub_template": "",
}


def seed_result(current_structured: dict) -> dict:
    """The dict the step fills in, started from the saved values so that
    opting *out* of the structured flow clears nothing an alliance set
    before, with every key `save_structured_storm_config` expects present."""
    result = dict(current_structured)
    for key, value in _RESULT_DEFAULTS.items():
        result.setdefault(key, value)
    return result


def col_letter(value, fallback: str = "") -> str:
    """A single column letter A-Z, upper-cased, or `fallback`."""
    letter = (value or "").strip().upper()
    return letter if len(letter) == 1 and "A" <= letter <= "Z" else fallback


def parse_stale_days(raw: str) -> int | None:
    """A whole number of days in 1-365, or `None` for anything else."""
    try:
        days = int((raw or "").strip())
    except ValueError:
        return None
    return days if 1 <= days <= 365 else None


def find_header_column(header_row: list[str], name: str) -> int:
    """Index of the header cell equal to `name` (case-insensitive,
    trimmed), or -1."""
    for i, cell in enumerate(header_row):
        if cell.strip().lower() == name:
            return i
    return -1


# ── Power data source ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _PowerDefaults:
    """What the Power Data Source picker suggests. With Member Sync on,
    the roster tab matched by the bot's Discord ID column; otherwise a
    placeholder tab matched by column A."""

    tab: str
    match_letter: str
    member_roster_tab: str
    sync_enabled: bool


def _power_defaults(guild_id: int) -> _PowerDefaults:
    from config import get_member_roster_config

    cfg = get_member_roster_config(guild_id)
    sync_enabled = bool(cfg.get("enabled"))
    member_roster_tab = cfg.get("tab_name") or "Member Roster"
    if sync_enabled:
        match_letter = _setup()._col_index_to_letter(int(cfg.get("discord_id_col", 0)))
    else:
        match_letter = "A"
    return _PowerDefaults(
        tab=member_roster_tab if sync_enabled else "Member Roster",
        match_letter=match_letter,
        member_roster_tab=member_roster_tab,
        sync_enabled=sync_enabled,
    )


async def _ask_power_data_source(w: _Wizard, result: dict, d: _PowerDefaults) -> None:
    """Tab + power column + member-match column. The alliance can point
    storm at the Member Roster (default), the Survey's Squad Powers tab,
    or any custom tab; at read time a match cell that looks like a
    Discord ID matches by ID, anything else by name."""
    saved_tab = (result.get("power_metric_tab") or "").strip()
    saved_letter = col_letter(result.get("power_metric_column"), "B")
    saved_match = col_letter(result.get("power_match_column"))
    effective_tab = saved_tab or d.tab
    effective_match = saved_match or d.match_letter
    # Saved values that differ from the defaults drive the 2-button
    # layout: Keep current replaces Use defaults so a re-run can't wipe
    # them by accident (the `ask_keep_or_change` idiom).
    has_custom = saved_tab != "" or saved_letter != "B" or saved_match != ""

    def modal() -> _TextModal:
        return _TextModal(
            "Power Data Source",
            [
                (
                    "tab_input",
                    dict(
                        label="Power source tab",
                        placeholder="e.g. Member Roster, Squad Powers",
                        default=effective_tab,
                        required=True,
                        max_length=100,
                    ),
                ),
                (
                    "col_input",
                    dict(
                        label="Power column letter (A-Z)",
                        placeholder="e.g. B",
                        default=saved_letter,
                        required=True,
                        max_length=2,
                    ),
                ),
                (
                    "match_input",
                    dict(
                        label="Member-match column letter (A-Z)",
                        placeholder="e.g. B — prefer column with Discord IDs",
                        default=effective_match,
                        required=True,
                        max_length=2,
                    ),
                ),
            ],
        )

    if has_custom:
        first = _Choice(
            f"Keep current: {effective_tab} · {saved_letter} · matched by {effective_match}",
            "keep",
            discord.ButtonStyle.success,
            ack=(
                f"✅ Keeping current: tab `{effective_tab}`, "
                f"power column `{saved_letter}`, matched by "
                f"`{effective_match}`."
            ),
        )
    else:
        first = _Choice(
            f"✅ Use defaults: {d.tab} · B · matched by {d.match_letter}",
            "default",
            discord.ButtonStyle.success,
            ack=(
                f"✅ Using defaults: tab `{d.tab}`, "
                f"power column `B`, matched by `{d.match_letter}`."
            ),
        )
    picker = _ChoiceView([first, _Choice("✏️ Define my own", "edit", modal=modal)])

    sync_blurb = (
        f"\n\n_Member Sync is enabled, so we're suggesting tab "
        f"`{d.tab}` matched by column `{d.match_letter}` "
        f"(the bot's Discord ID slot)._"
        if d.sync_enabled
        else "\n\n_Member Sync isn't enabled yet — the default tab "
        "name is just a placeholder; pick whichever tab actually "
        "has your power data._"
    )
    outcome = await w.ask(
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
        picker,
        "outcome",
    )

    if outcome == "default":
        # Empty tab + match persist so a future default change lands
        # without re-running the wizard.
        result["power_metric_tab"] = ""
        result["power_metric_column"] = "B"
        result["power_match_column"] = ""
    elif outcome == "edit":
        m = picker.modal
        assert m is not None
        tab_val = (m.tab_input.value or "").strip()
        # The Member Roster tab is stored empty so the read path falls
        # through to the canonical default.
        result["power_metric_tab"] = tab_val if tab_val and tab_val != d.member_roster_tab else ""
        result["power_metric_column"] = col_letter(m.col_input.value or "B", "B")
        result["power_match_column"] = col_letter(m.match_input.value)
    # "keep": the saved values are already in `result`.


# ── Sub mode ─────────────────────────────────────────────────────────────────


async def _ask_sub_mode(w: _Wizard, result: dict) -> None:
    """Pool (flat sub list) or Paired (a named sub per primary). The
    green button reads `Use Default: Pool` on a first run and
    `Use Current: <mode>` on re-entry, the `ask_keep_or_change` idiom."""
    saved = result.get("sub_mode")
    has_saved = saved in ("pool", "paired")
    mode = saved if has_saved else "pool"
    if mode == "pool":
        choices = [
            _Choice(
                "Use Current: Pool" if has_saved else "Use Default: Pool",
                "pool",
                discord.ButtonStyle.success,
                ack="✅ Sub mode: Pool",
            ),
            _Choice(
                "Paired: primary↔sub pairs",
                "paired",
                discord.ButtonStyle.primary,
                ack="✅ Sub mode: Paired",
            ),
        ]
    else:
        choices = [
            _Choice(
                "Pool: flat sub list",
                "pool",
                discord.ButtonStyle.primary,
                ack="✅ Sub mode: Pool",
            ),
            _Choice(
                "Use Current: Paired",
                "paired",
                discord.ButtonStyle.success,
                ack="✅ Sub mode: Paired",
            ),
        ]
    result["sub_mode"] = await w.ask(
        "**Sub Mode**\n"
        "How should subs be tracked when leadership builds a roster?\n"
        "• **Pool**: flat list of subs; any sub can cover any primary no-show.\n"
        "• **Paired**: each primary has a specific sub assigned in advance.",
        _ChoiceView(choices, timeout=120),
        "outcome",
    )


# ── Sign-up channel + schedule ───────────────────────────────────────────────


async def _ask_signup_channel(w: _Wizard, result: dict) -> None:
    view = wizard_steps.ChannelSelectStep(
        f"Select the channel where {w.label} sign-up polls post...",
        suggested_name=f"{w.slug}-signups",
        include_threads=True,
        guild=w.interaction_guild,
        current_id=result.get("signup_channel_id") or 0,
    )
    if view.is_current_stale:
        await w.channel.send(
            f"⚠️ Your previously configured {w.label} sign-up channel no longer "
            "exists. Select a new channel."
        )
    await w.channel.send(
        f"**{w.label} Sign-Up Channel**\n"
        "The bot will auto-post a sign-up poll here each week. Members click "
        "buttons to register their availability.\n"
        f"You can open the officer view via `/{w.slug} signups`.",
        view=view,
    )
    await w.wait(view)
    if not view.confirmed:
        await w.timed_out()
        raise _Abort
    result["signup_channel_id"] = view.selected_channel.id


async def _ask_signup_schedule(w: _Wizard, result: dict) -> None:
    """Poll day + post time (#131), the pair that drives the sign-up
    scheduler loop. Skipping both leaves `/<parent> post_signup` manual.
    The time prompt names the guild timezone the way the train, birthday
    and shiny wizards do."""
    from config import get_config

    guild_cfg = get_config(w.guild_id) if w.guild_id else None
    tz_str = guild_cfg.timezone if guild_cfg and guild_cfg.timezone else "America/New_York"
    sched = await wizard_time._ask_signup_schedule(
        w.channel,
        w.bot,
        w.user,
        w.cancel_event,
        label=w.label,
        cmd_name=w.cmd_name,
        current_dow=result.get("poll_day_of_week", -1),
        current_time=result.get("signup_time", ""),
        tz_label=wizard_steps.TIMEZONE_LABELS.get(tz_str, tz_str),
        event_type=w.event_type,
    )
    if sched is None:
        raise _Abort
    result["poll_day_of_week"] = sched["dow"]
    result["signup_time"] = sched["time"]


async def _ask_premium_tabs(w: _Wizard, result: dict) -> None:
    for key, label_text in (
        ("signups_tab", "Sign-Ups"),
        ("rosters_tab", "Rosters"),
        ("attendance_tab", "Attendance"),
    ):
        await w.ask_tab(
            f"**{label_text} Tab**\n"
            f"Which Google Sheet tab should store {w.label} "
            f"{label_text.lower()}? The bot creates and maintains "
            f"this tab.",
            key=key,
            result=result,
            modal_title=f"{label_text} Tab Name",
        )


# ── Power-refresh DM + stale-power follow-up ─────────────────────────────────


async def _ask_power_refresh_dm(w: _Wizard, result: dict, *, prior_enabled: bool) -> None:
    """Whether the sign-up button DMs a voter whose power cell is blank
    or unreadable (#138); one nudge per member per event date. On
    re-entry with the structured flow previously on, the saved answer
    shows as Keep current rather than forcing a re-pick."""
    column = result.get("power_metric_column", "B")
    if prior_enabled:
        current = bool(result.get("power_refresh_dm_enabled"))
        result["power_refresh_dm_enabled"] = await w.ask_yes_no(
            f"**Power-Refresh DM (💎 Premium)**\n"
            f"When a member clicks a sign-up button for **{w.label}** and "
            f"their power value (Column "
            f"**{column}** on the roster "
            f"Sheet) is blank or unparseable, the bot can DM them a "
            f"one-line nudge to update it. Currently "
            f"**{'on' if current else 'off'}**. Keep it or flip.",
            current=current,
        )
    else:
        result["power_refresh_dm_enabled"] = await w.ask_yes_no(
            f"**Power-Refresh DM (💎 Premium)**\n"
            f"When a member clicks a sign-up button for **{w.label}** and "
            f"their power value (Column "
            f"**{column}** on the roster "
            f"Sheet) is blank or unparseable, should the bot DM them a "
            f"one-line nudge to update it? At most one DM per member "
            f"per event date.",
            current=None,
        )


async def _ask_stale_power(
    w: _Wizard, result: dict, d: _PowerDefaults, *, prior_enabled: bool
) -> None:
    """The stale-power follow-up (#255), asked only when the power-refresh
    DM is on: whether to also nudge when a power value is older than N
    days, then the threshold and where the last-updated timestamp lives."""
    saved_days = int(result.get("power_refresh_stale_days") or 0)
    saved_on = saved_days > 0

    if prior_enabled and saved_on:
        stale_on = await w.ask_yes_no(
            f"**Stale-Power DM (💎 Premium)**\n"
            f"On top of the missing-power nudge, should the bot "
            f"also DM when a member's power value is older than "
            f"a configured number of days? Currently **on** at "
            f"**{saved_days}** days.",
            current=True,
        )
    else:
        stale_on = await w.ask_yes_no(
            f"**Stale-Power DM (💎 Premium)**\n"
            f"On top of the missing-power nudge for **{w.label}**, "
            f"should the bot also DM when a member's power value "
            f"is older than a configured number of days? "
            f"Currently **off**. At most one DM per member per event date "
            f"(shared with the missing-power nudge).",
            current=None,
        )

    if not stale_on:
        # Off wipes the days and source fields so re-enabling later
        # starts from defaults rather than a half-configured row.
        result["power_refresh_stale_days"] = 0
        result["power_last_updated_tab"] = ""
        result["power_last_updated_column"] = ""
        result["power_last_updated_match_column"] = ""
        return

    await _ask_stale_days(w, result, saved_days=saved_days)
    if not await _detect_survey_date_modified(w, result, d):
        await _ask_last_updated_source(w, result, d)


async def _ask_stale_days(w: _Wizard, result: dict, *, saved_days: int) -> None:
    """The days threshold, re-prompted up to three times on input that
    is not a whole number in 1-365: a picker that quietly fell back to
    7 when the officer typed "two weeks" would be confusing."""
    effective_days = saved_days if saved_days > 0 else 7

    def modal() -> _TextModal:
        return _TextModal(
            "Stale-Power Days",
            [
                (
                    "days_input",
                    dict(
                        label="Days before a power value is 'stale'",
                        placeholder="e.g. 7",
                        default=str(effective_days),
                        required=True,
                        max_length=4,
                    ),
                )
            ],
        )

    choices = []
    if saved_days > 0:
        choices.append(
            _Choice(
                f"Keep current: {effective_days} days",
                "keep",
                discord.ButtonStyle.success,
                ack=f"✅ Threshold: **{effective_days}** days.",
            )
        )
        choices.append(_Choice("✏️ Set days", "edit", modal=modal))
    else:
        # No prior value: no Keep button, and Set days names the default
        # (the Power Data Source picker's first-time idiom).
        choices.append(_Choice("✏️ Set days (default: 7)", "edit", modal=modal))

    for attempt in range(3):
        picker = _ChoiceView(choices)
        prompt = (
            "**Stale-Power Threshold (💎 Premium)**\n"
            "How many days old must a member's power value "
            "be before the bot DMs them? Recommended: **7**. "
            "Range: 1–365."
            if attempt == 0
            else "⚠️ Couldn't parse that — try a whole number between 1 and 365."
        )
        outcome = await w.ask(prompt, picker, "outcome")
        if outcome == "keep":
            result["power_refresh_stale_days"] = effective_days
            return
        m = picker.modal
        assert m is not None
        days = parse_stale_days(m.days_input.value)
        if days is not None:
            result["power_refresh_stale_days"] = days
            return

    await w.channel.send(
        f"⚠️ Couldn't parse a stale-days threshold "
        f"after 3 tries. Run `/{w.cmd_name}` to start "
        f"again."
    )
    raise _Abort


def _resolved_power_tab(result: dict, d: _PowerDefaults) -> str:
    """`power_metric_tab` is stored empty when it is the Member Roster
    tab; this is the tab name the officer actually saw."""
    return (result.get("power_metric_tab") or "").strip() or d.tab


async def _detect_survey_date_modified(w: _Wizard, result: dict, d: _PowerDefaults) -> bool:
    """Survey shortcut: when the power source is the bot's own Squad
    Powers tab, the survey writes a `Date Modified` column that can be
    found by header, so the last-updated picker is skipped. Returns
    whether that applied."""
    from config import get_survey_config, get_spreadsheet

    survey_cfg = get_survey_config(w.guild_id) if w.guild_id else {}
    survey_tab = survey_cfg.get("tab_squad_powers") or "Squad Powers"
    if _resolved_power_tab(result, d) != survey_tab:
        return False

    def _read_survey_header():
        sh = get_spreadsheet(w.guild_id)
        ws = sh.worksheet(survey_tab) if sh else None
        return ws.row_values(1) if ws else []

    # Off the event loop in case the sheet is slow or rate-limited.
    try:
        header_row = await asyncio.to_thread(_read_survey_header)
    except Exception as e:
        print(
            f"[SETUP] survey Date-Modified header lookup "
            f"failed for guild={w.guild_id} tab={survey_tab!r}: "
            f"{e}"
        )
        header_row = []
    idx = find_header_column(header_row, "date modified")
    if idx < 0:
        return False

    letter = _setup()._col_index_to_letter(idx)
    result["power_last_updated_tab"] = survey_tab
    result["power_last_updated_column"] = letter
    # An empty match column reuses `power_match_column` at read time.
    result["power_last_updated_match_column"] = ""
    await w.channel.send(
        f"✅ Auto-detected the survey's **Date Modified** "
        f"column (`{letter}`) on tab "
        f"`{survey_tab}` — using that as the "
        f"last-updated source."
    )
    return True


async def _ask_last_updated_source(w: _Wizard, result: dict, d: _PowerDefaults) -> None:
    """Tab + column + optional match column for the last-updated
    timestamp, pre-filled from the Power Data Source so an alliance whose
    power and timestamp share a tab can accept in one click."""
    saved_tab = (result.get("power_last_updated_tab") or "").strip()
    saved_col = col_letter(result.get("power_last_updated_column"))
    saved_match = col_letter(result.get("power_last_updated_match_column"))
    default_tab = _resolved_power_tab(result, d)
    default_match = result.get("power_match_column") or d.match_letter
    effective_tab = saved_tab or default_tab
    effective_col = saved_col or ""
    effective_match = saved_match or default_match
    has_custom = bool(saved_tab) or bool(saved_col)

    def modal() -> _TextModal:
        return _TextModal(
            "Last-Updated Source",
            [
                (
                    "tab_input",
                    dict(
                        label="Last-updated tab",
                        placeholder="e.g. Squad Powers, Member Roster",
                        default=effective_tab,
                        required=True,
                        max_length=100,
                    ),
                ),
                (
                    "col_input",
                    dict(
                        label="Last-updated column letter (A-Z)",
                        placeholder="e.g. N",
                        default=effective_col,
                        required=True,
                        max_length=2,
                    ),
                ),
                (
                    "match_input",
                    dict(
                        label="Name-match column letter (A-Z, optional)",
                        placeholder=("Leave blank to reuse Power match column"),
                        default=effective_match,
                        required=False,
                        max_length=2,
                    ),
                ),
            ],
        )

    choices = []
    if has_custom:
        choices.append(
            _Choice(
                f"Keep: {effective_tab} · {effective_col}",
                "keep",
                discord.ButtonStyle.success,
                ack=(
                    f"✅ Keeping current: tab `{effective_tab}`, column `{effective_col or '?'}`."
                ),
            )
        )
        choices.append(_Choice("✏️ Define source", "edit", modal=modal))
    else:
        choices.append(
            _Choice(
                f"✏️ Set source: {default_tab[:30]}…"
                if len(default_tab) > 30
                else f"✏️ Set source: {default_tab}",
                "edit",
                modal=modal,
            )
        )
    picker = _ChoiceView(choices)

    outcome = await w.ask(
        "**Last-Updated Source (💎 Premium)**\n"
        "Where on the Sheet does the bot find each "
        "member's last-updated timestamp? We support "
        "the bot's own Squad Power Survey, a manually-"
        "maintained column, or an export from a "
        "different bot.\n\n"
        "• **Tab**: the Sheet tab with the timestamp.\n"
        "• **Last-updated column**: the column with the "
        "timestamp values (e.g. `N`).\n"
        "• **Name-match column**: blank reuses the Power "
        "Data Source's match column. Same row-matching "
        "rules: Discord ID first, name fallback.\n\n"
        "_Date formats are auto-detected. MM/DD/YYYY, "
        "DD/MM/YYYY, ISO 8601, and `May 5, 2026`-style "
        "long-month all work. Rows whose timestamp "
        "doesn't parse are silently skipped._",
        picker,
        "outcome",
    )
    if outcome == "keep":
        return  # saved values already in result

    m = picker.modal
    assert m is not None
    tab_val = (m.tab_input.value or "").strip()
    col_val = col_letter(m.col_input.value)
    result["power_last_updated_tab"] = tab_val
    result["power_last_updated_column"] = col_val
    result["power_last_updated_match_column"] = col_letter(m.match_input.value)
    if not (tab_val and col_val):
        # A cleared source disables the stale check rather than leaving
        # a half-configured row that fails silently at read time.
        await w.channel.send(
            "⚠️ Tab + column are both required for the "
            "stale check. Disabling for now — re-run "
            f"`/{w.cmd_name}` to set them later."
        )
        result["power_refresh_stale_days"] = 0


# ── Roster DM templates ──────────────────────────────────────────────────────


_DM_PLACEHOLDER_INFO = (
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


async def _ask_roster_dm_templates(w: _Wizard, result: dict) -> None:
    """The three DMs behind `📨 DM rostered members` (Starter, Paired
    Sub, Pool Sub), each through the mail template step's Use default /
    Keep current / Edit picker. An empty saved value means the hardcoded
    default at send time, so a guild that never customises still gets
    the bot's copy, updates included."""
    from defaults import (
        DEFAULT_ROSTER_DM_STARTER,
        DEFAULT_ROSTER_DM_PAIRED_SUB,
        DEFAULT_ROSTER_DM_POOL_SUB,
    )
    from config import get_roster_dm_templates

    saved = (
        get_roster_dm_templates(w.guild_id, w.event_type)
        if w.guild_id
        else {"starter": "", "paired_sub": "", "pool_sub": ""}
    )

    await w.channel.send(
        "📨 **Roster DM Templates** _(3 templates, one per role)_"
        "\nNext we'll set up the three DMs the bot sends after "
        "Approve & Post when leadership clicks 📨 DM rostered "
        "members. Each role (Starter, Paired Sub, Pool Sub) gets "
        "its own message. You can use the defaults or customize "
        "each one."
    )
    for key, role_label, default_template, saved_key in (
        ("roster_dm_starter_template", "Starter", DEFAULT_ROSTER_DM_STARTER, "starter"),
        ("roster_dm_paired_sub_template", "Paired Sub", DEFAULT_ROSTER_DM_PAIRED_SUB, "paired_sub"),
        ("roster_dm_pool_sub_template", "Pool Sub", DEFAULT_ROSTER_DM_POOL_SUB, "pool_sub"),
    ):
        result[key] = await _ask_dm_template(
            w, role_label, default_template, saved.get(saved_key, "")
        )


async def _ask_dm_template(
    w: _Wizard, role_label: str, default_template: str, saved_template: str
) -> str:
    """One DM template. Returns the chosen body, or the empty string for
    "use default" so the DB stays clean when the bot ships a new default."""
    saved_is_custom = bool(saved_template) and saved_template != default_template

    choices = []
    if saved_is_custom:
        choices.append(
            _Choice(
                "Keep current custom template",
                "keep",
                discord.ButtonStyle.success,
                ack=f"✅ Keeping your saved {role_label} DM template.",
            )
        )
    choices.append(
        _Choice(
            "↩️ Use default template",
            "default",
            # With nothing custom saved, Use default is the primary action.
            discord.ButtonStyle.secondary if saved_is_custom else discord.ButtonStyle.success,
            ack=(
                f"✅ Reverted to default {role_label} DM template."
                if saved_is_custom
                else f"✅ Using default {role_label} DM template."
            ),
        )
    )
    choices.append(_Choice("✏️ Edit template", "edit"))

    custom_block = (
        f"\n\nHere is your saved custom template:\n```\n{saved_template}\n```"
        if saved_is_custom
        else ""
    )
    question = (
        "Would you like to keep your custom template, revert to the default, or edit it?"
        if saved_is_custom
        else "Would you like to use this default or write your own?"
    )
    outcome = await w.ask(
        f"**Roster DM Template: {role_label}**\n"
        f"Sent when leadership clicks 📨 DM rostered members "
        f"after Approve & Post for {w.label}.\n\n"
        f"Here is the default template:\n"
        f"```\n{default_template}\n```"
        f"{custom_block}\n\n"
        f"{question}",
        _ChoiceView(choices),
        "outcome",
    )
    if outcome == "keep":
        return saved_template
    if outcome == "default":
        return ""

    # Edit: pasted as a chat message so multi-line templates work
    # without fighting a modal's length limit.
    reference_label = "current custom" if saved_is_custom else "default"
    await w.channel.send(
        f"Paste your custom {role_label} DM template. "
        f"You can copy the {reference_label} above and modify "
        f"it, or write your own.\n\n"
        f"**Available placeholders:**\n{_DM_PLACEHOLDER_INFO}\n\n"
        f"*This form will time out in 5 minutes. "
        f"You can run `/{w.cmd_name}` again if it times out.*"
    )

    def check(m):
        return m.author == w.user and m.channel == w.channel

    try:
        reply = await w.bot.wait_for("message", check=check, timeout=300)
    except asyncio.TimeoutError:
        await w.timed_out()
        raise _Abort
    fallback = saved_template if saved_is_custom else default_template
    return reply.content.strip() or fallback


# ── Preset library: strategy presets + member rules ──────────────────────────


async def _ask_preset_library(w: _Wizard, result: dict) -> None:
    """Tab names for strategy presets and member rules, each with an
    explainer first (#144) and, when the alliance has no rows yet, an
    inline offer to create the first one so the concept is reachable
    right away. Only asked once the alliance has opted in: the presets
    and rules drive the structured roster builder and nothing else."""
    hub = HUB_COMMAND[w.event_type]

    await w.channel.send(
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
        f"Manage presets via `{hub}` → "
        f"**{HUB_BTN_PRESETS}**."
    )
    await w.ask_tab(
        f"**Strategy Presets Tab**\n"
        f"Which Google Sheet tab should store {w.label} strategy presets? "
        f"The bot creates and maintains this tab.",
        key="strategies_tab",
        result=result,
        modal_title="Strategy Presets Tab Name",
    )
    # An unconfigured Sheet (or a transient gspread failure) shows the
    # offer unconditionally: one extra offer beats a guild with no
    # presets never seeing the surface, and `/<parent> strategy list`
    # falls back the same way.
    try:
        import storm_strategy as ss

        existing_presets = await asyncio.to_thread(ss.list_presets, w.guild_id, w.event_type)
    except Exception:
        existing_presets = []
    if not existing_presets:
        await _offer_first_row(
            w,
            _setup()._InlineCreatePresetOffer(
                owner_id=w.user.id, event_type=w.event_type, parent=w.slug
            ),
            f"Want to create your first {w.label} preset now? You can also "
            f"do this later via `{hub}` → "
            f"**{HUB_BTN_PRESETS}**.",
        )

    # Both DS and CS support `teams=both/A/B` (Rule A / #166), so the
    # rule list and example are the same for both event types.
    await w.channel.send(
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
        f"Add rules later via `{hub}` → "
        f"**{HUB_BTN_RULES}**."
    )
    await w.ask_tab(
        f"**Member Rules Tab**\n"
        f"Which Google Sheet tab should store {w.label} member rules? "
        f"The bot creates and maintains this tab.",
        key="member_rules_tab",
        result=result,
        modal_title="Member Rules Tab Name",
    )
    try:
        import storm_member_rules as smr

        existing_rules = await asyncio.to_thread(smr.list_rules, w.guild_id, w.event_type)
    except Exception:
        existing_rules = []
    if not existing_rules:
        await _offer_first_row(
            w,
            _setup()._InlineCreateMemberRuleOffer(
                owner_id=w.user.id, event_type=w.event_type, parent=w.slug
            ),
            f"Want to add your first {w.label} rule now? The button opens "
            f"a quick modal for a power-band rule (the most common type); "
            f"per-member rules need a Discord member picker, so add those "
            f"later via `{hub}` → **{HUB_BTN_RULES}**.",
        )


async def _offer_first_row(w: _Wizard, offer, text: str) -> None:
    """Post an inline-create offer and wait it out. Either choice, and a
    timeout, proceed; the view's own `on_timeout` strips the buttons.
    Only a cancel exits."""
    offer.message = await w.channel.send(text, view=offer)
    await w.wait(offer)


# ── Entry point ──────────────────────────────────────────────────────────────


async def run_structured_flow_step(
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
    current_structured: dict,
    interaction_guild,
) -> dict | None:
    """
    Final block of the storm setup wizard (DS + CS). Walks leadership
    through enabling the structured roster flow (Premium, #38) and, once
    enabled, everything it needs, ending with the strategy preset and
    member rules tabs (#54). Returns a dict shaped like
    `save_structured_storm_config`'s kwargs (plus the three roster DM
    templates), or None if the officer cancelled or a question timed out.

    Branching:
      * Premium + opted in: the full configuration.
      * Premium without opt-in, or free tier: nothing is asked and the
        saved values come back unchanged, so opting out clears nothing.

    The registration-post schedule is asked here (#131);
    `signup_schedule_cron` is left untouched.

    `current` (the plain storm config) is accepted for signature parity
    with the other wizard steps and unused.
    """
    w = _Wizard(
        channel=channel,
        bot=bot,
        user=user,
        cancel_event=cancel_event,
        guild_id=guild_id,
        event_type=event_type,
        label=label,
        cmd_name=cmd_name,
        interaction_guild=interaction_guild,
    )
    result = seed_result(current_structured)
    try:
        await _walk(w, result, current_structured, is_premium_flag=is_premium_flag)
    except _Abort:
        return None
    return result


async def _walk(w: _Wizard, result: dict, current_structured: dict, *, is_premium_flag: bool):
    opted_in = False
    if is_premium_flag:
        opted_in = await _ask_opt_in(w, current_structured)
    result["structured_flow_enabled"] = opted_in
    if not opted_in:
        return

    prior_enabled = bool(current_structured.get("structured_flow_enabled"))
    d = _power_defaults(w.guild_id)
    await _ask_power_data_source(w, result, d)
    await _ask_sub_mode(w, result)
    await _ask_signup_channel(w, result)
    await _ask_signup_schedule(w, result)
    await _ask_premium_tabs(w, result)
    await _ask_power_refresh_dm(w, result, prior_enabled=prior_enabled)
    if result.get("power_refresh_dm_enabled"):
        await _ask_stale_power(w, result, d, prior_enabled=prior_enabled)
    await _ask_roster_dm_templates(w, result)
    await _ask_preset_library(w, result)


async def _ask_opt_in(w: _Wizard, current_structured: dict) -> bool:
    """The Premium gate. An alliance that has run the wizard before has
    a saved decision either way, so it gets Keep current / Switch rather
    than a fresh Yes / No. `get_structured_storm_config` always returns
    `structured_flow_enabled`, so prior setup is asked of
    `has_storm_config` directly."""
    from config import has_storm_config

    await w.channel.send(
        f"**Step 8 of 9: Structured Roster Flow (💎 Premium)**\n"
        f"The structured flow auto-posts a Discord sign-up poll, captures "
        f"votes per member, and gives leadership a roster builder that "
        f"filters members by power for each zone. Replaces the text-template "
        f"draft for {w.label} when enabled. You can leave this off and still "
        f"use the strategy preset library on the free tier."
    )
    current = (
        bool(current_structured.get("structured_flow_enabled"))
        if has_storm_config(w.guild_id, w.event_type)
        else None
    )
    return await w.ask_yes_no(f"Turn on the structured flow for {w.label}?", current=current)

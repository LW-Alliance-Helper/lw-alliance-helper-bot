"""
The growth wizard (`run_growth_setup`): the re-entry summary, enable or
disable (with the saved settings kept), the source tab, the data start
row, the name column, the metrics editor (add through a modal, edit
through a launcher, delete through a picker, with the free-tier cap),
the growth tab, the snapshot frequency with its day or interval, the
save and the confirmation embed with the next snapshot's time.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup.py` set: `setup_cog` imports `run_growth_setup`
back under its old name for the hub, the launcher and the tests; the
shared wizard pieces come from `wizard_steps`; this module reaches into
`setup_cog` at call time (`_setup()`) only for the re-entry summary, the
disable-with-clear helper and the tab-claim warning. The five inline
views and the modal are module classes under their old names.

Every prompt, label, acknowledgement, saved field and summary line is
unchanged from the function this replaced;
`tests/integration/test_growth_setup.py` was written against it and
holds this module to it.
"""

import asyncio
from dataclasses import dataclass, field

import discord

import premium
import wizard_registry
import wizard_steps
from messages import INPUT_INVALID, SETUP_POINTER_FOOTER, TIER_COMPARISON, WIZARD_TIMEOUT
from setup_hub import HUB_BTN_GROWTH
from wizard_registry import wait_view_or_cancel
from wizard_steps import WIZARD_STEP_TIMEOUT


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


TIMEOUT_MSG = WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH)
RECOVERY = f"`/setup` → {HUB_BTN_GROWTH}"

# Hardcoded defaults — what the bot ships with. These are passed as
# `default=` to ask_keep_or_change. The user's previously-saved value
# (if any) is passed as `current=` so the wizard can label it
# accurately ("Keep current: X" vs "Use default: Y") instead of
# showing every saved value as the "default".
DEFAULT_TAB_SOURCE = "Squad Powers"
DEFAULT_DATA_START_ROW = 2
DEFAULT_NAME_COL = "A"
DEFAULT_TAB_GROWTH = "Growth Tracking"
DEFAULT_SNAPSHOT_DAY = 1
DEFAULT_SNAPSHOT_INTERVAL = 30


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

    async def ask(self, text: str, view, attr: str, *, embed=None):
        """Post `text` (or `embed`) with `view`, wait, and return
        `view.<attr>`; a timeout (the attribute still `None`) posts the
        route back and raises."""
        if embed is not None:
            await self.channel.send(embed=embed, view=view)
        else:
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
            self.channel, prompt, timeout_cmd="setup_growth", cancel_event=self.cancel_event, **kw
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


@dataclass
class _Answers:
    tab_source: str = ""
    data_start_row: int = DEFAULT_DATA_START_ROW
    name_col: str = DEFAULT_NAME_COL
    metrics: list = field(default_factory=list)
    tab_growth: str = ""
    snapshot_frequency: str = "monthly"
    snapshot_day: int = DEFAULT_SNAPSHOT_DAY
    snapshot_interval: int = DEFAULT_SNAPSHOT_INTERVAL


# ── Views ────────────────────────────────────────────────────────────────────


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

    @property
    def is_usable(self) -> bool:
        return bool(self.label_value and self.col_value and self.col_value.isalpha())

    async def on_submit(self, interaction: discord.Interaction):
        self.label_value = self._label_input.value.strip()
        self.col_value = self._col_input.value.strip().upper()
        await interaction.response.defer()
        self.stop()


class MetricsActionView(discord.ui.View):
    """Step 5's controls. Add opens the modal and appends to `metrics`
    itself; Edit, Delete and Done hand back to the loop."""

    def __init__(self, metrics: list, *, at_cap: bool):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.choice = None
        self._metrics = metrics
        if not metrics:
            self.edit_btn.disabled = True
            self.delete_btn.disabled = True
            self.done_btn.disabled = True
        if at_cap:
            self.add_btn.disabled = True

    @discord.ui.button(label="➕ Add Metric", style=discord.ButtonStyle.success, row=0)
    async def add_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        modal = MetricModal()
        await inter.response.send_modal(modal)
        await modal.wait()
        if modal.is_usable:
            self._metrics.append({"label": modal.label_value, "col": modal.col_value})
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


class PickMetricView(discord.ui.View):
    """Which metric to edit or delete."""

    def __init__(self, metrics: list):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.index = None
        self.select = discord.ui.Select(
            placeholder="Choose a metric...",
            options=[
                discord.SelectOption(
                    label=m["label"][:100], value=str(i), description=f"Column {m['col']}"
                )
                for i, m in enumerate(metrics)
            ],
            min_values=1,
            max_values=1,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, inter: discord.Interaction):
        self.index = int(self.select.values[0])
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()


class EditLaunchView(discord.ui.View):
    """Opens the metric modal pre-filled with the chosen metric."""

    def __init__(self, existing: dict):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.modal = MetricModal(label_default=existing["label"], col_default=existing["col"])
        self.confirmed = False

    @discord.ui.button(label="✏️ Edit values", style=discord.ButtonStyle.primary)
    async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(self.modal)
        await self.modal.wait()
        self.confirmed = True
        self.stop()


class FrequencyView(discord.ui.View):
    """Step 7: monthly, or a custom interval (💎 Premium)."""

    def __init__(self, *, custom_unlocked: bool):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.selected = None
        if not custom_unlocked:
            self.custom.disabled = True

    @discord.ui.button(label="📅 Monthly (1st of each month)", style=discord.ButtonStyle.primary)
    async def monthly(self, inter: discord.Interaction, button: discord.ui.Button):
        self.selected = "monthly"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(
            inter, content="✅ Frequency: **Monthly**", view=self
        )
        self.stop()

    @discord.ui.button(
        label="🔁 Custom interval (every X days) 💎", style=discord.ButtonStyle.secondary
    )
    async def custom(self, inter: discord.Interaction, button: discord.ui.Button):
        self.selected = "interval"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()


# ── Steps ────────────────────────────────────────────────────────────────────


def _schedule_text(current: dict) -> str:
    if current.get("snapshot_frequency", "monthly") == "monthly":
        return f"Monthly on day {current.get('snapshot_day', 1)}"
    return f"Every {current.get('snapshot_interval', 30)} days"


async def _confirm_reentry(w: _Wizard, s: _Saved) -> None:
    """If already enabled, show the summary and offer edit or cancel."""
    current = s.current
    if not (s.already_configured and current.get("enabled")):
        return
    metrics_list = current.get("metrics") or []
    fields = [
        ("Source Tab", current.get("tab_source") or "*not set*"),
        ("Name Column", f"Column {current.get('name_col') or '*not set*'}"),
        ("Data Start Row", str(current.get("data_start_row") or "*not set*")),
        ("Growth Tab", current.get("tab_growth") or "*not set*"),
        ("Snapshot Schedule", _schedule_text(current)),
        (
            f"Metrics ({len(metrics_list)})",
            "\n".join(f"• {m['label']} — column {m['col']}" for m in metrics_list)
            if metrics_list
            else "*none*",
        ),
    ]
    proceed = await _setup().ask_proceed_with_existing_config(
        w.channel,
        title="📈 Current Growth Setup",
        description="Growth tracking is already configured. Would you like to edit these settings?",
        fields=fields,
        cancel_event=w.cancel_event,
        no_changes_message="✅ No changes made. Growth tracking is still active.",
    )
    if proceed is not True:
        raise _Abort


async def _ask_enable(w: _Wizard, s: _Saved) -> None:
    """Step 1. A No saves the row disabled with every other setting kept,
    offers the clear button, and ends the wizard."""
    enabled = await w.ask(
        "**Step 1 of 7 — Enable growth tracking?**\n"
        "Should the bot automatically take snapshots of your members' stats on a schedule?",
        wizard_steps.YesNoView(),
        "selected",
    )
    if enabled:
        return
    from config import save_growth_config, clear_growth_config

    current = s.current
    save_growth_config(
        w.guild_id,
        enabled=0,
        tab_source=current.get("tab_source", ""),
        name_col=current.get("name_col", "A"),
        metrics=current.get("metrics", []),
        tab_growth=current.get("tab_growth", "Growth Tracking"),
        snapshot_frequency=current.get("snapshot_frequency", "monthly"),
        snapshot_day=current.get("snapshot_day", 1),
        snapshot_interval=current.get("snapshot_interval", 30),
        data_start_row=current.get("data_start_row", 2),
    )
    await _setup().ask_disable_with_clear(
        w.channel,
        feature_label="Growth tracking",
        setup_command=f"setup → {HUB_BTN_GROWTH}",
        had_prior_config=s.already_configured,
        clear_fn=lambda: clear_growth_config(w.guild_id),
        cancel_event=w.cancel_event,
    )
    raise _Abort


async def _ask_source(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 2 to 4: the source tab, the data start row, the name column."""
    a.tab_source = await w.keep_or_change(
        "**Step 2 of 7 — Source Tab**\n"
        "Which tab in your Google Sheet contains your member data?\n"
        "⚠️ *Make sure this tab exists in your sheet.*",
        default=DEFAULT_TAB_SOURCE,
        current=s.current.get("tab_source", ""),
        modal_title="Source Tab",
        modal_label="Tab name",
    )
    await _setup().warn_if_tab_claimed(
        w.channel, w.guild_id, a.tab_source, exclude_field="tab_source"
    )

    raw = await w.keep_or_change(
        "**Step 3 of 7 — Data Start Row**\n"
        "Which row does your member data start on? (Row 1 is usually the header)",
        default=str(DEFAULT_DATA_START_ROW),
        current=str(s.current.get("data_start_row") or ""),
        modal_title="Data Start Row",
        modal_label="Row number",
    )
    try:
        a.data_start_row = int(str(raw).strip())
    except ValueError:
        await w.invalid(type="row number", example="2")

    raw = await w.keep_or_change(
        "**Step 4 of 7 — Name Column**\nWhich column contains the member's name?",
        default=DEFAULT_NAME_COL,
        current=s.current.get("name_col", ""),
        modal_title="Name Column",
        modal_label="Column letter",
    )
    a.name_col = raw.strip().upper()
    if len(a.name_col) != 1 or not a.name_col.isalpha():
        await w.invalid(type="single column letter", example="A")


def _metrics_embed(metrics: list, cap: int | None) -> discord.Embed:
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
        embed.set_footer(
            text=TIER_COMPARISON.format(
                free_limit=f"{len(metrics)} of {cap} metrics used", premium_limit="unlimited"
            )
        )
    return embed


async def _run_metrics_editor(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 5: add, edit and delete metrics until Done. Edits `a.metrics`
    in place; the Add button appends through its own modal."""
    metrics = a.metrics
    while True:
        # Free-tier cap on number of growth metrics
        cap = await premium.get_limit(
            "growth_metrics", w.guild_id, interaction=w.interaction, bot=w.interaction.client
        )
        at_cap = cap is not None and len(metrics) >= cap
        choice = await w.ask(
            "",
            MetricsActionView(metrics, at_cap=at_cap),
            "choice",
            embed=_metrics_embed(metrics, cap),
        )
        if choice == "done":
            break
        if choice == "loop":
            continue
        if choice in ("edit", "delete") and not metrics:
            continue

        verb = "edit" if choice == "edit" else "delete"
        index = await w.ask(
            f"Which metric do you want to {verb}?", PickMetricView(metrics), "index"
        )

        if choice == "delete":
            removed = metrics.pop(index)
            await w.channel.send(f"🗑️ Removed **{removed['label']}** (column {removed['col']}).")
            continue

        existing = metrics[index]
        launch = EditLaunchView(existing)
        await w.channel.send(
            f"Editing **{existing['label']}** (column {existing['col']}). Click below to update.",
            view=launch,
        )
        await w.wait(launch)
        if launch.modal.is_usable:
            metrics[index] = {"label": launch.modal.label_value, "col": launch.modal.col_value}

    if not metrics:
        await w.channel.send(f"⚠️ No metrics defined. Run {RECOVERY} to try again.")
        raise _Abort


async def _ask_growth_tab(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 6."""
    a.tab_growth = await w.keep_or_change(
        "**Step 6 of 7 — Growth Tracking Tab**\n"
        "Which tab should snapshots be written to?\n"
        "⚠️ *If the tab doesn't exist, the bot will create it automatically.*",
        default=DEFAULT_TAB_GROWTH,
        current=s.current.get("tab_growth", ""),
        modal_title="Growth Tracking Tab",
        modal_label="Tab name",
    )
    await _setup().warn_if_tab_claimed(
        w.channel, w.guild_id, a.tab_growth, exclude_field="tab_growth"
    )


async def _ask_frequency(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 7 and 7a: the frequency, then its day or interval. A custom
    interval is Premium-only; an unparseable day or interval lands the
    default."""
    custom_unlocked = await premium.is_premium(
        w.guild_id, interaction=w.interaction, bot=w.interaction.client
    )
    prompt = "**Step 7 of 7 — Snapshot Frequency**\nHow often should the bot take a snapshot?"
    if not custom_unlocked:
        prompt += "\n*🔒 Custom interval is a Premium feature.*"
    a.snapshot_frequency = await w.ask(
        prompt, FrequencyView(custom_unlocked=custom_unlocked), "selected"
    )

    if a.snapshot_frequency == "monthly":
        raw = await w.keep_or_change(
            "**Step 7a of 7 — Snapshot Day**\n"
            "Which day of the month should the snapshot run? (1–28)",
            default=str(DEFAULT_SNAPSHOT_DAY),
            current=str(s.current.get("snapshot_day") or ""),
            modal_title="Snapshot Day",
            modal_label="Day of month (1–28)",
        )
        try:
            a.snapshot_day = max(1, min(28, int(str(raw).strip())))
        except ValueError:
            a.snapshot_day = DEFAULT_SNAPSHOT_DAY
    else:
        raw = await w.keep_or_change(
            "**Step 7a of 7 — Interval (days)**\nHow many days between each snapshot?",
            default=str(DEFAULT_SNAPSHOT_INTERVAL),
            current=str(s.current.get("snapshot_interval") or ""),
            modal_title="Interval",
            modal_label="Days between snapshots",
        )
        try:
            a.snapshot_interval = max(1, int(str(raw).strip()))
        except ValueError:
            a.snapshot_interval = DEFAULT_SNAPSHOT_INTERVAL


# ── Save and summary ─────────────────────────────────────────────────────────


def _save(w: _Wizard, a: _Answers) -> None:
    from config import save_growth_config

    save_growth_config(
        w.guild_id,
        enabled=1,
        tab_source=a.tab_source,
        name_col=a.name_col,
        metrics=a.metrics,
        tab_growth=a.tab_growth,
        snapshot_frequency=a.snapshot_frequency,
        snapshot_day=a.snapshot_day,
        snapshot_interval=a.snapshot_interval,
        data_start_row=a.data_start_row,
    )


def _next_snapshot_text(a: _Answers) -> str:
    """When the very first snapshot will fire under this config, so the
    user isn't left guessing "OK now what?" when they pick a custom
    interval — picking 14 days doesn't tell them whether the first
    snapshot is today, tomorrow, or 14 days from now."""
    from growth import compute_next_snapshot

    next_dt = compute_next_snapshot(
        {
            "enabled": 1,
            "snapshot_frequency": a.snapshot_frequency,
            "snapshot_day": a.snapshot_day,
            "snapshot_interval": a.snapshot_interval,
        }
    )
    if next_dt is None:
        return "*Could not compute — check `/growth overview` for status.*"
    ts = int(next_dt.timestamp())
    # Discord renders <t:N:F> as a localized full date/time per viewer
    # and <t:N:R> as a relative "in 3 days" string — the combo gives
    # leadership a clear answer regardless of their personal timezone.
    return (
        f"<t:{ts}:F> (<t:{ts}:R>)\n"
        f"*Want to start tracking from today instead? "
        f"Run `/growth overview` and click **📸 Run Snapshot Now**.*"
    )


def _summary_embed(a: _Answers) -> discord.Embed:
    freq_desc = (
        f"Monthly on day {a.snapshot_day}"
        if a.snapshot_frequency == "monthly"
        else f"Every {a.snapshot_interval} days"
    )
    metrics_display = "\n".join(f"• **{m['label']}** — column {m['col']}" for m in a.metrics)
    embed = discord.Embed(title="✅ Growth Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Source Tab", value=a.tab_source, inline=False)
    embed.add_field(name="Name Column", value=f"Column {a.name_col}", inline=False)
    embed.add_field(name="Data Start Row", value=str(a.data_start_row), inline=False)
    embed.add_field(name="Growth Tab", value=a.tab_growth, inline=False)
    embed.add_field(name="Snapshot Schedule", value=freq_desc, inline=False)
    embed.add_field(name="Next Snapshot", value=_next_snapshot_text(a), inline=False)
    embed.add_field(name="Metrics", value=metrics_display, inline=False)
    embed.set_footer(
        text=SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_GROWTH)
        + " Use /growth overview to take a manual snapshot.",
    )
    return embed


# ── The wizard ───────────────────────────────────────────────────────────────


async def run_growth_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring growth tracking."""
    from config import get_growth_config, has_growth_config

    w = _Wizard(
        interaction=interaction,
        channel=interaction.channel,
        user=interaction.user,
        guild_id=interaction.guild_id,
        cancel_event=wizard_registry.register(interaction.user.id),
    )
    s = _Saved(
        current=get_growth_config(w.guild_id),
        already_configured=has_growth_config(w.guild_id),
    )
    a = _Answers(metrics=list(s.current.get("metrics", [])))
    try:
        await _confirm_reentry(w, s)
        await w.channel.send(
            "⚙️ **Growth Tracking Setup**\n"
            "Configure how the bot tracks your alliance's growth over time. "
            "Each month (or on your chosen schedule), the bot takes a snapshot of your members' stats "
            "and records them in your Google Sheet so you can track progress."
        )
        await _ask_enable(w, s)
        await _ask_source(w, s, a)
        await _run_metrics_editor(w, s, a)
        await _ask_growth_tab(w, s, a)
        await _ask_frequency(w, s, a)
    except _Abort:
        wizard_registry.unregister(w.user.id, w.cancel_event)
        return

    _save(w, a)
    await w.channel.send(embed=_summary_embed(a))
    wizard_registry.unregister(w.user.id, w.cancel_event)
    print(f"[SETUP] Growth config saved for guild {w.guild_id}")

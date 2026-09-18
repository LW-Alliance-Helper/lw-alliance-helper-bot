"""
The Growth Breakdown wizard (`run_growth_breakdown_setup`, 💎 Premium):
the growth guard, the re-entry summary, the breakdown tab, the auto-post
toggle with its channel and bucket filter, the custom thresholds and
bucket labels, the save and the confirmation embed.

The bucket-classification math itself ships free (`/growth breakdown`,
and the **📊 See most recent Breakdown** button on `/growth overview`,
both read the breakdown tab for any guild that's enabled growth
tracking). This wizard configures the Premium layer: the sheet tab, the
auto-post channel (premium-gated at post time so a subscription lapse
stops the alerts without any config change), the bucket filter, custom
thresholds applied to every metric (global, not per-metric) and custom
labels for each bucket.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611),
in the shape `storm_setup.py` set: `setup_cog` imports the entry point
back under its old name for the hub, the launcher and the tests; the
shared wizard pieces come from `wizard_steps`; this module reaches into
`setup_cog` at call time (`_setup()`) only for the re-entry summary and
the tab-claim warning. The five inline views and modals are module
classes under their old names.

Every prompt, label, acknowledgement, saved field and summary line is
unchanged from the function this replaced;
`tests/integration/test_growth_breakdown_setup.py` was written against
it and holds this module to it.
"""

import asyncio
from dataclasses import dataclass, field

import discord

import wizard_registry
import wizard_steps
from messages import PREV_CHANNEL_GONE, SETUP_POINTER_FOOTER, WIZARD_TIMEOUT
from setup_hub import HUB_BTN_BREAKDOWN, HUB_BTN_GROWTH
from wizard_registry import wait_view_or_cancel
from wizard_steps import WIZARD_STEP_TIMEOUT


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


TIMEOUT_MSG = WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN)


def _bucket_names(buckets: list[str]) -> str:
    from growth import DEFAULT_BUCKET_LABELS

    return ", ".join(DEFAULT_BUCKET_LABELS.get(b, b) for b in buckets)


def _custom_labels_text(labels: dict) -> str:
    from growth import DEFAULT_BUCKET_LABELS, BUCKET_ORDER

    return (
        ", ".join(f"{DEFAULT_BUCKET_LABELS[b]}→{labels[b]}" for b in BUCKET_ORDER if labels.get(b))
        or "—"
    )


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

    async def ask(self, text: str, view, attr: str, *, timeout_msg: str = TIMEOUT_MSG):
        """Post `text` with `view`, wait, and return `view.<attr>`; a timeout
        (the attribute still `None`) posts the route back and raises."""
        await self.channel.send(text, view=view)
        await self.wait(view)
        value = getattr(view, attr)
        if value is None:
            await self.channel.send(timeout_msg)
            raise _Abort
        return value


@dataclass
class _Saved:
    current: dict
    already_configured: bool


@dataclass
class _Answers:
    tab_breakdown: str = ""
    post_channel_id: int = 0
    bucket_filter: list = field(default_factory=list)
    thresholds: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)


# ── Views ────────────────────────────────────────────────────────────────────


async def _disable_and_stop(view, inter: discord.Interaction) -> None:
    for item in view.children:
        item.disabled = True
    await wizard_registry.safe_edit_response(inter, view=view)
    view.stop()


class BucketFilterView(discord.ui.View):
    """Step 3: which buckets fire the auto-post. A Keep-current button
    sits on its own row when leadership previously picked a filter."""

    def __init__(self, saved_filter: list[str]):
        from growth import DEFAULT_BUCKET_LABELS, BUCKET_ORDER

        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.selected: list[str] | None = None

        if saved_filter:
            keep_btn = discord.ui.Button(
                label=f"Keep current: {_bucket_names(saved_filter)}"[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep_cb(inter: discord.Interaction):
                self.selected = list(saved_filter)
                await _disable_and_stop(self, inter)

            keep_btn.callback = _keep_cb
            self.add_item(keep_btn)

        select = discord.ui.Select(
            placeholder="Pick which buckets fire alerts (none = all)",
            options=[
                discord.SelectOption(
                    label=DEFAULT_BUCKET_LABELS[b],
                    value=b,
                    description=f"{b.title()} growth bucket",
                )
                for b in BUCKET_ORDER
            ],
            min_values=0,
            max_values=len(BUCKET_ORDER),
            row=1 if saved_filter else 0,
        )

        async def _select_cb(inter: discord.Interaction):
            self.selected = list(select.values)
            await _disable_and_stop(self, inter)

        select.callback = _select_cb
        self.add_item(select)

        all_btn = discord.ui.Button(
            label="Use all buckets",
            style=discord.ButtonStyle.secondary,
            row=2 if saved_filter else 1,
        )

        async def _all_cb(inter: discord.Interaction):
            self.selected = []
            await _disable_and_stop(self, inter)

        all_btn.callback = _all_cb
        self.add_item(all_btn)


class ThresholdsModal(discord.ui.Modal):
    def __init__(self, thresholds: dict):
        from growth import DEFAULT_THRESHOLDS

        super().__init__(title="Custom Thresholds (%)")
        self.values_out: dict | None = None
        self._increased = discord.ui.TextInput(
            label="Increased ≥",
            placeholder="e.g. 20 — bucket lower bound, %",
            default=str(thresholds.get("increased", DEFAULT_THRESHOLDS["increased"])),
            required=True,
            max_length=6,
        )
        self._steady = discord.ui.TextInput(
            label="Steady ≥",
            placeholder="e.g. 10",
            default=str(thresholds.get("steady", DEFAULT_THRESHOLDS["steady"])),
            required=True,
            max_length=6,
        )
        self._low = discord.ui.TextInput(
            label="Low ≥",
            placeholder="e.g. 5",
            default=str(thresholds.get("low", DEFAULT_THRESHOLDS["low"])),
            required=True,
            max_length=6,
        )
        self._none = discord.ui.TextInput(
            label="None ≥",
            placeholder="0 — usually leave as 0. Decline is < 0.",
            default=str(thresholds.get("none", DEFAULT_THRESHOLDS["none"])),
            required=True,
            max_length=6,
        )
        for i in (self._increased, self._steady, self._low, self._none):
            self.add_item(i)

    async def on_submit(self, inter: discord.Interaction):
        try:
            self.values_out = {
                "increased": float(self._increased.value),
                "steady": float(self._steady.value),
                "low": float(self._low.value),
                "none": float(self._none.value),
            }
        except (ValueError, TypeError):
            self.values_out = None
        await inter.response.defer()
        self.stop()


class LabelsModal(discord.ui.Modal):
    def __init__(self, labels: dict):
        from growth import DEFAULT_BUCKET_LABELS, BUCKET_ORDER

        super().__init__(title="Custom Bucket Labels")
        self.values_out: dict | None = None
        self._inputs = {}
        for b in BUCKET_ORDER:
            ti = discord.ui.TextInput(
                label=DEFAULT_BUCKET_LABELS[b],
                placeholder=f"e.g. '{DEFAULT_BUCKET_LABELS[b]}'",
                default=str(labels.get(b, DEFAULT_BUCKET_LABELS[b])),
                required=True,
                max_length=30,
            )
            self._inputs[b] = ti
            self.add_item(ti)

    async def on_submit(self, inter: discord.Interaction):
        self.values_out = {b: ti.value.strip() for b, ti in self._inputs.items()}
        await inter.response.defer()
        self.stop()


class _KeepDefaultsCustomizeView(discord.ui.View):
    """Keep current / Use defaults / Customize, with Customize opening a
    modal. Keep current renders first (leftmost), per the
    Keep-current-is-always-first convention. It only applies when
    leadership saved custom values on a previous run — drop it on first
    run, and otherwise demote "Use defaults" to a secondary revert so
    Keep current is the primary action."""

    keep_label = "Keep current"

    def __init__(self, saved: dict):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.choice = None
        self._modal_values = None
        self._saved = saved
        self.keep_btn.label = self.keep_label
        if saved:
            self.defaults_btn.label = "↩️ Use defaults"
            self.defaults_btn.style = discord.ButtonStyle.secondary
        else:
            self.remove_item(self.keep_btn)

    def _modal(self) -> discord.ui.Modal:
        raise NotImplementedError

    @discord.ui.button(label="Keep current", style=discord.ButtonStyle.success)
    async def keep_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "keep"
        await _disable_and_stop(self, inter)

    @discord.ui.button(label="✅ Use defaults", style=discord.ButtonStyle.success)
    async def defaults_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "defaults"
        await _disable_and_stop(self, inter)

    @discord.ui.button(label="✏️ Customize", style=discord.ButtonStyle.primary)
    async def customize_btn(self, inter: discord.Interaction, button: discord.ui.Button):
        modal = self._modal()
        await inter.response.send_modal(modal)
        await modal.wait()
        self.choice = "customize" if modal.values_out else None
        self._modal_values = modal.values_out
        for item in self.children:
            item.disabled = True
        try:
            await inter.edit_original_response(view=self)
        except Exception:
            pass
        self.stop()


class ThresholdsChoiceView(_KeepDefaultsCustomizeView):
    keep_label = "Keep current values"

    def _modal(self) -> discord.ui.Modal:
        return ThresholdsModal(self._saved)


class LabelsChoiceView(_KeepDefaultsCustomizeView):
    keep_label = "Keep current labels"

    def _modal(self) -> discord.ui.Modal:
        return LabelsModal(self._saved)


# ── Steps ────────────────────────────────────────────────────────────────────


async def _guard(w: _Wizard, s: _Saved) -> None:
    """Breakdown needs growth tracking enabled with at least one metric."""
    if s.current.get("enabled") and s.current.get("metrics"):
        return
    await w.channel.send(
        f"⚙️ Set up growth tracking first — run `/setup` → {HUB_BTN_GROWTH} and add at "
        f"least one metric, then come back to `/setup` → {HUB_BTN_BREAKDOWN} to "
        "configure the breakdown layer."
    )
    raise _Abort


async def _confirm_reentry(w: _Wizard, s: _Saved) -> None:
    """If already configured, show the summary and offer edit or cancel."""
    if not s.already_configured:
        return
    current = s.current
    post_ch = current.get("breakdown_post_channel_id") or 0
    thresholds = current.get("breakdown_thresholds") or {}
    labels = current.get("breakdown_labels") or {}
    bucket_filter = current.get("breakdown_bucket_filter") or []
    fields = [
        ("Breakdown Tab", current.get("tab_breakdown") or "Growth Breakdown"),
        ("Auto-Post Channel", f"<#{post_ch}>" if post_ch else "❌ Off"),
    ]
    if post_ch and bucket_filter:
        fields.append(("Bucket Filter", _bucket_names(bucket_filter)))
    elif post_ch:
        fields.append(("Bucket Filter", "All buckets"))
    if thresholds:
        fields.append(
            (
                "Custom Thresholds",
                f"Increased ≥ {thresholds.get('increased', 0):g}%, "
                f"Steady ≥ {thresholds.get('steady', 0):g}%, "
                f"Low ≥ {thresholds.get('low', 0):g}%, "
                f"None ≥ {thresholds.get('none', 0):g}%",
            )
        )
    if labels:
        fields.append(("Custom Labels", _custom_labels_text(labels)))
    proceed = await _setup().ask_proceed_with_existing_config(
        w.channel,
        title="📊 Current Growth Breakdown Setup",
        description="Growth breakdown is already configured. Would you like to edit these settings?",
        fields=fields,
        cancel_event=w.cancel_event,
        no_changes_message="✅ No changes made. Your breakdown setup is still active.",
    )
    if proceed is not True:
        raise _Abort


async def _ask_tab(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 1."""
    tab = await wizard_steps.ask_keep_or_change(
        w.channel,
        "**Step 1 of 5 — Breakdown Tab**\n"
        "Which tab in your Google Sheet should the breakdown data live in? "
        "The bot creates it automatically if it doesn't exist yet.",
        default="Growth Breakdown",
        current=s.current.get("tab_breakdown") or "",
        modal_title="Breakdown Tab",
        modal_label="Tab name",
        timeout_cmd="setup_growth_breakdown",
        cancel_event=w.cancel_event,
    )
    if tab is None:
        raise _Abort
    a.tab_breakdown = tab
    await _setup().warn_if_tab_claimed(w.channel, w.guild_id, tab, exclude_field="tab_breakdown")


async def _ask_auto_post(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 2 and 3: the auto-post toggle, its channel, and the bucket
    filter (which only applies to the auto-post; the on-demand `/growth`
    button always shows every bucket)."""
    on = await w.ask(
        "**Step 2 of 5 — Auto-Post After Snapshots?**\n"
        "Each time the bot finishes a snapshot, post the breakdown summary "
        "to a channel so leadership doesn't have to run `/growth breakdown` to see "
        "who's slowing down.",
        wizard_steps.YesNoView(),
        "selected",
    )
    if not on:
        return

    view = wizard_steps.ChannelSelectStep(
        "Select the auto-post channel…",
        suggested_name="growth-breakdown",
        include_threads=True,
        guild=w.interaction.guild,
        current_id=s.current.get("breakdown_post_channel_id") or 0,
    )
    if view.is_current_stale:
        await w.channel.send(PREV_CHANNEL_GONE.format(channel_label="breakdown"))
    await w.channel.send(
        "**Auto-Post Channel**\nWhere should the breakdown summaries land?", view=view
    )
    await w.wait(view)
    if not view.confirmed:
        await w.channel.send(TIMEOUT_MSG)
        raise _Abort
    a.post_channel_id = view.selected_channel.id

    saved_filter = s.current.get("breakdown_bucket_filter") or []
    if saved_filter:
        prompt = (
            f"**Step 3 of 5 — Bucket Filter**\n"
            f"Currently alerting on: **{_bucket_names(saved_filter)}**. Pick a new set of "
            f"buckets, or hit **Use all buckets** to alert on every bucket."
        )
    else:
        prompt = (
            "**Step 3 of 5 — Bucket Filter**\n"
            "Pick which buckets fire the auto-post — leave empty (or hit "
            "**Use all buckets**) to alert on every bucket."
        )
    a.bucket_filter = await w.ask(prompt, BucketFilterView(saved_filter), "selected")


async def _ask_thresholds(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 4."""
    from growth import DEFAULT_THRESHOLDS

    saved = dict(s.current.get("breakdown_thresholds") or {})
    view = ThresholdsChoiceView(saved)
    choice = await w.ask(
        "**Step 4 of 5 — Bucket Thresholds**\n"
        f"Defaults: Increased ≥ {DEFAULT_THRESHOLDS['increased']:.0f}%, "
        f"Steady ≥ {DEFAULT_THRESHOLDS['steady']:.0f}%, "
        f"Low ≥ {DEFAULT_THRESHOLDS['low']:.0f}%, "
        f"None ≥ {DEFAULT_THRESHOLDS['none']:.0f}%, "
        f"Decline < 0%.\n"
        "Customize for stricter (or looser) growth standards — applies to "
        "every metric. Per-metric thresholds are tracked as a follow-up.",
        view,
        "choice",
        timeout_msg=(
            f"⏰ Timed out or invalid thresholds. Run `/setup` → {HUB_BTN_BREAKDOWN} to start again."
        ),
    )
    if choice == "defaults":
        a.thresholds = {}
    elif choice == "keep":
        a.thresholds = saved
    else:
        a.thresholds = view._modal_values


async def _ask_labels(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 5."""
    from growth import DEFAULT_BUCKET_LABELS, BUCKET_ORDER

    saved = dict(s.current.get("breakdown_labels") or {})
    view = LabelsChoiceView(saved)
    choice = await w.ask(
        "**Step 5 of 5 — Bucket Labels**\n"
        f"Defaults: {', '.join(DEFAULT_BUCKET_LABELS[b] for b in BUCKET_ORDER)}.\n"
        "Rename buckets to match your alliance's voice (e.g. 'Crushing It', "
        "'Stalled', 'Going Backwards').",
        view,
        "choice",
    )
    if choice == "defaults":
        a.labels = {}
    elif choice == "keep":
        a.labels = saved
    else:
        a.labels = view._modal_values


# ── Save and summary ─────────────────────────────────────────────────────────


async def _save(w: _Wizard, a: _Answers) -> None:
    from config import save_growth_breakdown_config

    saved_ok = save_growth_breakdown_config(
        w.guild_id,
        tab_breakdown=a.tab_breakdown,
        breakdown_thresholds=a.thresholds,
        breakdown_labels=a.labels,
        breakdown_post_channel_id=a.post_channel_id,
        breakdown_bucket_filter=a.bucket_filter,
    )
    if not saved_ok:
        await w.channel.send(
            f"⚠️ Couldn't save the breakdown config — make sure `/setup` → {HUB_BTN_GROWTH} "
            "has been run for this server first."
        )
        raise _Abort


def _summary_embed(a: _Answers) -> discord.Embed:
    embed = discord.Embed(title="✅ Growth Breakdown Configured", color=discord.Color.green())
    embed.add_field(name="Breakdown Tab", value=a.tab_breakdown, inline=False)
    if a.post_channel_id:
        bf_text = _bucket_names(a.bucket_filter) if a.bucket_filter else "All buckets"
        embed.add_field(name="Auto-Post Channel", value=f"<#{a.post_channel_id}>", inline=False)
        embed.add_field(name="Bucket Filter", value=bf_text, inline=False)
    else:
        embed.add_field(
            name="Auto-Post",
            value="❌ Off — use `/growth breakdown` (or `/growth overview` → 📊 See most recent Breakdown) to view on demand.",
            inline=False,
        )
    if a.thresholds:
        t_text = (
            f"Increased ≥ {a.thresholds['increased']:g}%, "
            f"Steady ≥ {a.thresholds['steady']:g}%, "
            f"Low ≥ {a.thresholds['low']:g}%, "
            f"None ≥ {a.thresholds['none']:g}%, Decline < 0%"
        )
        embed.add_field(name="Custom Thresholds", value=t_text, inline=False)
    else:
        embed.add_field(
            name="Thresholds",
            value="Defaults (Increased ≥ 20%, Steady ≥ 10%, Low ≥ 5%, None ≥ 0%, Decline < 0%)",
            inline=False,
        )
    if a.labels:
        embed.add_field(name="Custom Labels", value=_custom_labels_text(a.labels), inline=False)
    embed.set_footer(text=SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_BREAKDOWN))
    return embed


# ── The wizard ───────────────────────────────────────────────────────────────


async def run_growth_breakdown_setup(interaction: discord.Interaction, bot):
    """Premium-only wizard for the Growth Breakdown auto-post + customization.
    See the module docstring for what it configures."""
    from config import get_growth_config, has_growth_breakdown_config

    w = _Wizard(
        interaction=interaction,
        channel=interaction.channel,
        user=interaction.user,
        guild_id=interaction.guild_id,
        cancel_event=wizard_registry.register(interaction.user.id),
    )
    s = _Saved(
        current=get_growth_config(w.guild_id),
        already_configured=has_growth_breakdown_config(w.guild_id),
    )
    a = _Answers()
    try:
        await _guard(w, s)
        await _confirm_reentry(w, s)
        await w.channel.send(
            "📊 **Growth Breakdown Setup** (💎 Premium)\n"
            "Classifies each member's growth between snapshots into one of "
            "five buckets and (optionally) posts the summary to a channel "
            "after every snapshot."
        )
        await _ask_tab(w, s, a)
        await _ask_auto_post(w, s, a)
        await _ask_thresholds(w, s, a)
        await _ask_labels(w, s, a)
        await _save(w, a)
    except _Abort:
        wizard_registry.unregister(w.user.id, w.cancel_event)
        return

    await w.channel.send(embed=_summary_embed(a))
    wizard_registry.unregister(w.user.id, w.cancel_event)
    print(f"[SETUP] Growth Breakdown config saved for guild {w.guild_id}")

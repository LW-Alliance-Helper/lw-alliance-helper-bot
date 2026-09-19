"""
The train schedule wizard: the re-entry summary, the schedule tab, blurb
generation with its themes, tones, default tone and prompt templates,
reminders with their channel, time and DM, the save and the summary, and
the hand-off to the Conductor Rotation step (`train_setup_rotation.py`).

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611) in
the `storm_setup.py` shape: `setup_cog` imports `run_train_setup` back under
its old name for the hub and the launcher, and `manage_train_templates` as
`_manage_train_templates`; the shared wizard pieces come from `wizard_steps`
and `wizard_time`; this module reaches into `setup_cog` at call time
(`_setup()`) only for the re-entry summary and the tab-claim warning, and
for the rotation step under the name the tests patch.

Shape: `_Wizard` for the handles, `_Saved` and `_Answers` for the state, one
`_ask_*` function per step, one `_Abort` exit. The template manager keeps
its loop and its two inline views become module classes under their old
names.

Every prompt, label, saved field and summary line is unchanged;
`tests/integration/test_train_setup.py` holds this module to the old
function.
"""

import asyncio
from dataclasses import dataclass, field

import discord

import premium
import wizard_registry
import wizard_steps
from messages import (
    PREV_CHANNEL_GONE,
    SETUP_POINTER_FOOTER,
    TIME_PARSE_GIVE_UP,
    TIME_PARSE_RETRY,
    WIZARD_TIMEOUT,
)
from setup_hub import HUB_BTN_TRAIN
from wizard_registry import wait_view_or_cancel
from wizard_steps import WIZARD_STEP_TIMEOUT
from wizard_time import _format_24h_to_12h, _format_time_with_tz, _parse_12h_time


def _setup():
    """The wizard module, resolved at call time (see the module docstring)."""
    import setup_cog

    return setup_cog


class _Abort(Exception):
    """The officer cancelled, or a question timed out. The timeout notice
    has already been posted by the time this is raised."""


TIMEOUT_MSG = WIZARD_TIMEOUT.format(wizard=HUB_BTN_TRAIN)
#: The route back the keep-or-change helper prints on its own timeout notice:
#: the hub button, like every other exit here. `/setup_train` went with #201.
NAV = f"setup → {HUB_BTN_TRAIN}"


# ── Shared handles ───────────────────────────────────────────────────────────


@dataclass
class _Wizard:
    interaction: discord.Interaction
    bot: discord.Client
    channel: discord.abc.Messageable
    user: discord.abc.User
    guild_id: int
    cancel_event: asyncio.Event
    guild_tz: str
    is_premium: bool = False

    def check(self, m) -> bool:
        return m.author == self.user and m.channel == self.channel

    async def timed_out(self) -> None:
        await self.channel.send(TIMEOUT_MSG)

    async def wait(self, view) -> None:
        await wait_view_or_cancel(view, self.cancel_event)
        if getattr(view, "cancelled", False):
            raise _Abort

    async def ask(self, text: str, view, attr: str):
        """Post `text` with `view`, wait, and return `view.<attr>`; a timeout
        (the attribute still unset) posts the route back and raises."""
        await self.channel.send(text, view=view)
        await self.wait(view)
        value = getattr(view, attr)
        if not value and value is not False:
            await self.timed_out()
            raise _Abort
        return value

    async def ask_yes_no(self, text: str) -> bool:
        return bool(await self.ask(text, wizard_steps.YesNoView(), "selected"))

    async def keep_or_change(self, prompt: str, **kw) -> str:
        picked = await wizard_steps.ask_keep_or_change(
            self.channel, prompt, timeout_cmd=NAV, cancel_event=self.cancel_event, **kw
        )
        if picked is None:
            raise _Abort
        return picked


@dataclass
class _Saved:
    current: dict
    already_configured: bool


@dataclass
class _Answers:
    tab_name: str = ""
    blurbs_enabled: int = 0
    themes: list = field(default_factory=list)
    tones: list = field(default_factory=list)
    default_tone: str = ""
    prompt_template: str = ""
    templates: list | None = None
    default_template_name: str = ""
    reminders_enabled: int = 0
    reminder_channel_id: int = 0
    reminder_time: str = "22:00"
    train_dm_message: str = ""


# ── Views ────────────────────────────────────────────────────────────────────


class ToneDefaultView(discord.ui.View):
    """Step 5: the default tone, with Keep current when the saved default is
    still one of the tones."""

    def __init__(self, tone_list: list, *, current: str | None = None):
        super().__init__(timeout=120)
        self.selected = None

        # Keep-current button on row 0 when the saved default
        # tone still appears in the current tone_list (it may
        # have been removed in Step 4).
        keep_added = False
        if current and current in tone_list:
            keep_btn = discord.ui.Button(
                label=f"Keep current: {current}"[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _keep_cb(inter: discord.Interaction):
                self.selected = current
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"✅ Keeping default tone: **{current}**",
                    view=self,
                )
                self.stop()

            keep_btn.callback = _keep_cb
            self.add_item(keep_btn)
            keep_added = True

        select = discord.ui.Select(
            placeholder="Select default tone...",
            options=[discord.SelectOption(label=t, value=t) for t in tone_list],
            row=1 if keep_added else 0,
        )

        async def _cb(inter: discord.Interaction):
            self.selected = select.values[0]
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content=f"✅ Default tone: **{self.selected}**", view=self
            )
            self.stop()

        select.callback = _cb
        self.add_item(select)


class TemplateListView(discord.ui.View):
    """The template manager's controls: Add / Edit / Set Default / Delete / Done."""

    def __init__(self, count: int, at_cap: bool):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.action: str | None = None
        self.index: int | None = None
        if at_cap:
            self.add_btn.disabled = True
        if count <= 1:
            self.delete_btn.disabled = True
        if count == 0:
            self.edit_btn.disabled = True
            self.set_default_btn.disabled = True
            self.done_btn.disabled = True

    async def _finish(self, inter, action: str) -> None:
        self.action = action
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.success, row=0)
    async def add_btn(self, inter, button):
        await self._finish(inter, "add")

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary, row=0)
    async def edit_btn(self, inter, button):
        await self._finish(inter, "edit")

    @discord.ui.button(label="⭐ Set Default", style=discord.ButtonStyle.secondary, row=0)
    async def set_default_btn(self, inter, button):
        await self._finish(inter, "default")

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger, row=1)
    async def delete_btn(self, inter, button):
        await self._finish(inter, "delete")

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=1)
    async def done_btn(self, inter, button):
        await self._finish(inter, "done")


class PickView(discord.ui.View):
    """Which template to edit, delete or make the default."""

    def __init__(self, templates: list[dict]):
        super().__init__(timeout=WIZARD_STEP_TIMEOUT)
        self.idx = None
        options = [
            discord.SelectOption(label=t["name"][:100], value=str(i))
            for i, t in enumerate(templates)
        ]
        sel = discord.ui.Select(placeholder="Pick a template…", options=options)

        async def _cb(inter):
            self.idx = int(sel.values[0])
            for c in self.children:
                c.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        sel.callback = _cb
        self.add_item(sel)


# ── The template manager ─────────────────────────────────────────────────────


def _template_embed(templates: list[dict], default_name: str, cap_label: str) -> discord.Embed:
    listing = []
    for i, t in enumerate(templates):
        star = " ⭐" if t["name"] == default_name else ""
        preview = (t.get("template") or "").strip().split("\n")[0][:60]
        preview_suffix = f" — *{preview}*" if preview else " — *(empty)*"
        listing.append(f"`{i + 1}.` **{t['name']}**{star}{preview_suffix}")
    return discord.Embed(
        title="**Step 6 of 9 — Prompt Templates**",
        description=(
            "Saved ChatGPT prompt templates. The default ⭐ is the one used "
            "by the blurb wizard unless a member's day overrides it.\n\n"
            + "\n".join(listing)
            + f"\n\n*Slot usage: **{len(templates)} of {cap_label}**.*"
        ),
        color=discord.Color.blurple(),
    )


async def manage_train_templates(
    *,
    bot,
    channel,
    check,
    existing: list,
    default_name: str,
    cap: int | None,
    cancel_event,
):
    """
    Multi-template manager for the train setup wizard.

    Lets the user view, add, edit, delete, and re-pick the default for the
    guild's saved ChatGPT prompt templates. `cap` is the per-tier maximum
    (None = unlimited / premium).

    Returns (templates_list, default_template_name) — or (None, None) if
    the user timed out. Templates are stored as `[{"name", "template"}, ...]`.
    """
    templates: list[dict] = list(existing) if existing else []
    if not templates:
        templates = [{"name": "Default", "template": ""}]

    # Default name must always reference an existing template.
    if not any(t.get("name") == default_name for t in templates):
        default_name = templates[0]["name"]

    async def _reply(seconds: int):
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=seconds), cancel_event
        )
        if reply is None:
            await channel.send(TIMEOUT_MSG)
        return reply

    while True:
        cap_label = "unlimited" if cap is None else str(cap)
        list_view = TemplateListView(
            count=len(templates),
            at_cap=cap is not None and len(templates) >= cap,
        )
        await channel.send(
            embed=_template_embed(templates, default_name, cap_label), view=list_view
        )
        await wait_view_or_cancel(list_view, cancel_event)
        if list_view.cancelled:
            return None, None
        if list_view.action is None:
            await channel.send(TIMEOUT_MSG)
            return None, None
        if list_view.action == "done":
            return templates, default_name

        # ── Pick which template (for edit/default/delete) ─────────────────────
        picked_idx = None
        if list_view.action in ("edit", "default", "delete"):
            pick = PickView(templates)
            await channel.send("Which template?", view=pick)
            await wait_view_or_cancel(pick, cancel_event)
            if pick.cancelled:
                return None, None
            if pick.idx is None:
                await channel.send(TIMEOUT_MSG)
                return None, None
            picked_idx = pick.idx

        if list_view.action == "delete":
            removed = templates.pop(picked_idx)
            if not templates:
                templates = [{"name": "Default", "template": ""}]
                default_name = "Default"
                await channel.send(
                    f"🗑️ Removed **{removed['name']}**. (Restored an empty Default — "
                    f"you need at least one template.)"
                )
            else:
                if removed["name"] == default_name:
                    default_name = templates[0]["name"]
                await channel.send(f"🗑️ Removed **{removed['name']}**.")
            continue

        if list_view.action == "default":
            default_name = templates[picked_idx]["name"]
            await channel.send(f"⭐ Default set to **{default_name}**.")
            continue

        # ── Add or Edit: collect a name + template body ────────────────────────
        existing_t = templates[picked_idx] if list_view.action == "edit" else None
        is_edit = existing_t is not None

        await channel.send(
            "**Template name** *(short label)*"
            + (f" — *editing* `{existing_t['name']}`" if is_edit else "")
            + "\nReply with a name (e.g. `Birthday`, `Welcome`, `Default`)."
            + " Reply `cancel` to abort."
        )
        reply = await _reply(300)
        if reply is None:
            return None, None
        new_name = reply.content.strip()
        if new_name.lower() == "cancel" or not new_name:
            continue
        new_name = new_name[:50]
        # Reject duplicate names (except when editing the same entry).
        for j, t in enumerate(templates):
            if t["name"].lower() == new_name.lower() and not (is_edit and j == picked_idx):
                await channel.send(
                    f"⚠️ A template named **{new_name}** already exists. Try a different name."
                )
                new_name = None
                break
        if new_name is None:
            continue

        await channel.send(
            "**Template body**\n"
            "Paste the full ChatGPT prompt. Use these placeholders:\n"
            "• `{name}` — the member's name\n"
            "• `{theme}` — the selected theme\n"
            "• `{tone}` — the selected tone\n"
            "• `{notes}` — any notes stored for this member\n"
            "*Reply `cancel` to abort, `keep` to keep the current body.*"
        )
        reply = await _reply(600)
        if reply is None:
            return None, None
        body_raw = reply.content.strip()
        if body_raw.lower() == "cancel":
            continue
        if body_raw.lower() == "keep" and is_edit:
            new_body = existing_t["template"]
        else:
            new_body = body_raw

        if is_edit:
            old_name = existing_t["name"]
            templates[picked_idx] = {"name": new_name, "template": new_body}
            if default_name == old_name:
                default_name = new_name
            await channel.send(f"✅ Updated **{new_name}**.")
        else:
            templates.append({"name": new_name, "template": new_body})
            await channel.send(f"✅ Added **{new_name}** ({len(templates)} of {cap_label}).")


# ── Re-entry ─────────────────────────────────────────────────────────────────


def _summary_fields(current: dict, guild_tz: str) -> list[tuple[str, str]]:
    themes = current.get("themes") or []
    tones = current.get("tones") or []
    fields = [
        ("Schedule Tab", current.get("tab_name") or "*not set*"),
        ("Blurbs", "✅ Enabled" if current.get("blurbs_enabled") else "❌ Disabled"),
    ]
    if current.get("blurbs_enabled"):
        fields.append(("Themes", ", ".join(themes) if themes else "*none*"))
        fields.append(("Tones", ", ".join(tones) if tones else "*none*"))
        fields.append(("Default Tone", current.get("default_tone") or "*not set*"))
    fields.append(
        ("Reminders", "✅ Enabled" if current.get("reminders_enabled") else "❌ Disabled")
    )
    if current.get("reminders_enabled"):
        rc = current.get("reminder_channel_id", 0) or 0
        fields.append(("Reminder Channel", f"<#{rc}>" if rc else "*not set*"))
        fields.append(
            (
                "Reminder Time",
                _format_time_with_tz(current.get("reminder_time"), guild_tz) or "*not set*",
            )
        )
    # Conductor Rotation (#55) — surfaced so the summary reflects it like
    # every other train setting (#302). DB-only reads, so no sheet round-trip.
    if current.get("rotation_enabled"):
        import train_rotation as _tr

        # 0 is Monday, so the fallback is for a missing value only, not a falsy one.
        _saved_wd = current.get("weekly_draft_day")
        _wd = 6 if _saved_wd is None else int(_saved_wd)
        _draft_day = _tr.WEEKDAY_NAMES[_wd] if 0 <= _wd < len(_tr.WEEKDAY_NAMES) else "?"
        _confirm = _format_time_with_tz(current.get("reminder_time"), guild_tz) or "not set"
        _pub = current.get("rotation_public_channel_id", 0) or 0
        fields.append(
            (
                "Conductor Rotation",
                "✅ Enabled\n"
                f"Weekly draft: {_draft_day} at {_confirm}\n"
                f"Public posts: {f'<#{_pub}>' if _pub else 'off (record only)'}\n"
                f"Active preset: {current.get('active_schedule_preset') or 'default'}",
            )
        )
    else:
        fields.append(("Conductor Rotation", "❌ Disabled"))
    return fields


async def _confirm_reentry(w: _Wizard, s: _Saved) -> None:
    if not s.already_configured:
        return
    proceed = await _setup().ask_proceed_with_existing_config(
        w.channel,
        title="🚂 Current Train Setup",
        description="Your train schedule is already configured. Would you like to edit these settings?",
        fields=_summary_fields(s.current, w.guild_tz),
        cancel_event=w.cancel_event,
        no_changes_message="✅ No changes made. Your train setup is still active.",
    )
    if proceed is not True:
        raise _Abort


# ── The questions ────────────────────────────────────────────────────────────


async def _ask_tab(w: _Wizard, s: _Saved, a: _Answers) -> None:
    a.tab_name = await w.keep_or_change(
        "**Step 1 of 9 — Schedule Sheet Tab**\n"
        "Which tab in your Google Sheet stores the train schedule?\n"
        "⚠️ *Make sure this tab exists in your sheet before continuing.*",
        default="Train Schedule",
        current=s.current.get("tab_name", ""),
        modal_title="Sheet Tab Name",
        modal_label="Tab name",
    )
    await _setup().warn_if_tab_claimed(
        w.channel, w.guild_id, a.tab_name, exclude_field="train_tab_name"
    )


async def _ask_blurbs(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 2, and when off, the note about what is skipped or kept."""
    wants = await w.ask_yes_no(
        "**Step 2 of 9 — ChatGPT Blurb Generation**\n"
        "Would you like the bot to help generate a ChatGPT prompt each day when you assign a train?\n"
        "This lets you quickly produce a personalized announcement blurb for the member.\n"
        f"*(You can always set this up later by running `/setup` → {HUB_BTN_TRAIN} again)*"
    )
    a.blurbs_enabled = 1 if wants else 0
    a.themes = s.current["themes"]
    a.tones = s.current["tones"]
    a.default_tone = s.current["default_tone"]
    a.prompt_template = s.current.get("prompt_template", "")
    if a.blurbs_enabled:
        return
    # If leadership had blurbs configured previously, their themes,
    # tones, default tone, and templates are kept in the DB so a
    # future re-enable starts where they left off. Tell them so they
    # don't worry about losing config.
    if s.already_configured and s.current.get("blurbs_enabled"):
        await w.channel.send(
            "ℹ️ Blurb generation disabled. Your themes, tones, and templates "
            "remain saved — re-enable later to restore them."
        )
    else:
        await w.channel.send(
            "ℹ️ *Skipping Steps 3–6 (themes, tones, default tone, prompt template) — "
            "blurb generation is off.*"
        )


def _trim(values: list[str], cap: int | None) -> tuple[list[str], bool]:
    """Trim list to cap. Returns (trimmed_list, was_truncated)."""
    if cap is None or len(values) <= cap:
        return values, False
    return values[:cap], True


async def _ask_csv(
    w: _Wizard,
    *,
    step_label: str,
    label: str,
    current_list: list[str],
    default_list: list[str],
    cap: int | None,
) -> list[str]:
    """One keep-or-change question over a comma-separated list, with the
    free-tier cap applied to the default, the current and the answer."""
    cap_capped_default, _ = _trim(list(default_list), cap)
    cap_capped_current, _ = _trim(list(current_list), cap)
    cap_note = (
        f"\n*Free tier: up to {cap} {label}. Upgrade for unlimited.*" if cap is not None else ""
    )
    chosen = await w.keep_or_change(
        f"{step_label}\n"
        f"These appear as options when selecting a {label[:-1] if label.endswith('s') else label} "
        f"for the train.{cap_note}",
        default=", ".join(cap_capped_default),
        current=", ".join(cap_capped_current),
        modal_title=label.title(),
        modal_label=f"{label.title()} (comma-separated)",
    )
    entered = [t.strip() for t in chosen.split(",") if t.strip()] or list(current_list)
    trimmed, truncated = _trim(entered, cap)
    if truncated:
        await w.channel.send(
            f"ℹ️ Free tier: only the first {cap} {label} were saved "
            f"(`{', '.join(trimmed)}`). Upgrade to Premium to save more."
        )
    return trimmed


async def _ask_blurb_details(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Steps 3 to 6: themes, tones, the default tone and the prompt templates."""
    from defaults import DEFAULT_THEMES, DEFAULT_TONES

    themes_cap = await premium.get_limit(
        "themes", w.guild_id, interaction=w.interaction, bot=w.interaction.client
    )
    tones_cap = await premium.get_limit(
        "tones", w.guild_id, interaction=w.interaction, bot=w.interaction.client
    )
    a.themes = await _ask_csv(
        w,
        step_label="**Step 3 of 9 — Themes**",
        label="themes",
        current_list=s.current["themes"],
        default_list=DEFAULT_THEMES,
        cap=themes_cap,
    )
    a.tones = await _ask_csv(
        w,
        step_label="**Step 4 of 9 — Tones**",
        label="tones",
        current_list=s.current["tones"],
        default_list=DEFAULT_TONES,
        cap=tones_cap,
    )
    a.default_tone = await w.ask(
        "**Step 5 of 9 — Default Tone**\nWhich tone should be pre-selected by default?",
        ToneDefaultView(a.tones, current=s.current.get("default_tone")),
        "selected",
    )

    # Free tier keeps a single "Default" template; premium can save up to
    # `template_cap` named templates and pick which is the default.
    template_cap = await premium.get_limit(
        "train_templates", w.guild_id, interaction=w.interaction, bot=w.interaction.client
    )
    existing_templates = list(s.current.get("templates") or [])
    if not existing_templates:
        existing_templates = [{"name": "Default", "template": a.prompt_template or ""}]
    default_template_name = s.current.get("default_template") or existing_templates[0]["name"]

    templates, default_template_name = await _setup()._manage_train_templates(
        bot=w.bot,
        channel=w.channel,
        check=w.check,
        existing=existing_templates,
        default_name=default_template_name,
        cap=template_cap,
        cancel_event=w.cancel_event,
    )
    if templates is None:
        raise _Abort  # timed out / cancelled
    a.templates = templates
    a.default_template_name = default_template_name
    a.prompt_template = next(
        (t["template"] for t in templates if t.get("name") == default_template_name),
        templates[0]["template"] if templates else "",
    )


async def _ask_reminders(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 7, and 7a / 7b when on. Rotation (Step 9) reuses the reminder
    channel and time when they're set, and asks for its own if reminders
    are off, so this step only collects them when the reminder is on."""
    wants = await w.ask_yes_no(
        "**Step 7 of 9 — Train Reminders**\n"
        "Should the bot post a reminder to leadership when someone is assigned the train each day?"
    )
    a.reminders_enabled = 1 if wants else 0
    a.reminder_channel_id = s.current.get("reminder_channel_id", 0) or 0
    a.reminder_time = s.current.get("reminder_time", "22:00") or "22:00"
    if not a.reminders_enabled:
        if s.already_configured and s.current.get("reminders_enabled"):
            await w.channel.send(
                "ℹ️ Train reminders disabled. Your saved reminder channel and time "
                "remain saved — re-enable later to restore them."
            )
        else:
            await w.channel.send(
                "ℹ️ *Skipping the reminder channel and time — train reminders are off.*"
            )
        return

    # ── Step 7a: Reminder channel ──
    view = wizard_steps.ChannelSelectStep(
        "Select the reminder channel...",
        suggested_name="leadership",
        include_threads=w.is_premium,
        guild=w.interaction.guild,
        current_id=a.reminder_channel_id,
    )
    if view.is_current_stale:
        await w.channel.send(PREV_CHANNEL_GONE.format(channel_label="reminder"))
    await w.channel.send(
        "**Step 7a of 9 — Reminder Channel**\n"
        "Which channel should the train reminder be posted to?",
        view=view,
    )
    await w.wait(view)
    if not view.confirmed:
        await w.timed_out()
        raise _Abort
    a.reminder_channel_id = view.selected_channel.id

    # ── Step 7b: Reminder time ──
    # Re-prompt up to 3 times on unparseable input rather than silently
    # falling back to a default — gives leadership a chance to correct
    # a typo without restarting the whole wizard.
    tz_label = wizard_steps.TIMEZONE_LABELS.get(w.guild_tz, "ET")
    attempts_left = 3
    a.reminder_time = "22:00"
    while True:
        time_raw = await w.keep_or_change(
            f"**Step 7b of 9 — Reminder Time**\n"
            f"What time should the reminder fire? *(in your timezone: {tz_label})*\n"
            f"*(e.g. `10:00pm`, `9:00am`)*",
            default="10:00pm",
            # DB stores 24h ("22:00") — render as "10:00pm" so the
            # Keep-current and Use-default button labels don't sit
            # side-by-side in mismatched formats.
            current=_format_24h_to_12h(s.current.get("reminder_time", "")),
            modal_title="Reminder Time",
            modal_label="Time",
        )
        parsed = _parse_12h_time(time_raw)
        if parsed:
            a.reminder_time = parsed
            return
        if len(time_raw) == 5 and time_raw[2] == ":" and time_raw.replace(":", "").isdigit():
            a.reminder_time = time_raw  # already 24h
            return
        attempts_left -= 1
        if attempts_left <= 0:
            await w.channel.send(TIME_PARSE_GIVE_UP.format(recovery=f"`/setup` → {HUB_BTN_TRAIN}"))
            raise _Abort
        await w.channel.send(TIME_PARSE_RETRY.format(raw=time_raw))


async def _ask_train_dm(w: _Wizard, s: _Saved, a: _Answers) -> None:
    """Step 8 (💎 Premium): the DM that fires alongside the channel reminder.
    Free guilds can configure it now; it just won't fire until they upgrade
    and sync the member roster."""
    from train_cog import DEFAULT_TRAIN_DM

    if not a.reminders_enabled:
        return
    saved_train_dm = (s.current.get("dm_message") or "").strip()
    train_dm_input = await w.keep_or_change(
        "**Step 8 of 9 — Train DM Body (💎 Premium)**\n"
        "When the train reminder fires, the bot also DMs the assigned member directly. "
        "Free guilds can configure it now — it just won't fire until you have Premium "
        "+ Member Sync.\n\n"
        "Use `{name}` as a placeholder for the member's name (optional).",
        default=DEFAULT_TRAIN_DM,
        current=saved_train_dm,
        modal_title="Train DM Body",
        modal_label="DM body (max 1000 chars)",
    )
    # Match the "Use default" UX everywhere else: keep the DB column
    # empty when the user picked the default, so future tweaks to the
    # hardcoded text reach existing alliances automatically.
    a.train_dm_message = "" if train_dm_input == DEFAULT_TRAIN_DM else train_dm_input


# ── Save and summary ─────────────────────────────────────────────────────────


def _save(w: _Wizard, a: _Answers) -> None:
    from config import save_train_config

    save_kwargs = dict(
        blurbs_enabled=a.blurbs_enabled,
        reminders_enabled=a.reminders_enabled,
        reminder_channel_id=a.reminder_channel_id,
        reminder_time=a.reminder_time,
        dm_message=a.train_dm_message,
    )
    if a.blurbs_enabled:
        save_kwargs["templates"] = a.templates
        save_kwargs["default_template"] = a.default_template_name
    save_train_config(
        w.guild_id,
        a.tab_name,
        a.themes,
        a.tones,
        a.prompt_template,
        a.default_tone,
        **save_kwargs,
    )


def _summary_embed(w: _Wizard, a: _Answers) -> discord.Embed:
    embed = discord.Embed(title="✅ Train Schedule Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab", value=a.tab_name, inline=True)
    embed.add_field(
        name="Blurb Generation", value="Enabled" if a.blurbs_enabled else "Disabled", inline=True
    )
    embed.add_field(
        name="Reminders", value="Enabled" if a.reminders_enabled else "Disabled", inline=True
    )
    if a.reminders_enabled:
        embed.add_field(name="Reminder Channel", value=f"<#{a.reminder_channel_id}>", inline=True)
        embed.add_field(
            name="Reminder Time",
            value=_format_time_with_tz(a.reminder_time, w.guild_tz),
            inline=True,
        )
    if a.blurbs_enabled:
        embed.add_field(name="Default Tone", value=a.default_tone, inline=True)
        embed.add_field(name="Themes", value=", ".join(a.themes), inline=False)
        embed.add_field(name="Tones", value=", ".join(a.tones), inline=False)
        template_count = len(a.templates) if a.templates is not None else 0
        if template_count > 0:
            template_names = ", ".join(t["name"] for t in a.templates)
            embed.add_field(
                name=f"Templates ({template_count})",
                value=f"`{template_names}` — default: **{a.default_template_name}**",
                inline=False,
            )
        if a.prompt_template:
            preview = a.prompt_template[:200] + ("..." if len(a.prompt_template) > 200 else "")
            embed.add_field(name="Default Template Preview", value=f"```{preview}```", inline=False)
    embed.set_footer(text=SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_TRAIN))
    return embed


# ── Entry point ──────────────────────────────────────────────────────────────


async def run_train_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the train schedule."""
    from config import get_train_config, has_train_config, get_config

    guild_cfg = get_config(interaction.guild_id)
    w = _Wizard(
        interaction=interaction,
        bot=bot,
        channel=interaction.channel,
        user=interaction.user,
        guild_id=interaction.guild_id,
        cancel_event=wizard_registry.register(interaction.user.id),
        guild_tz=guild_cfg.timezone if guild_cfg else "America/New_York",
    )
    s = _Saved(
        current=get_train_config(w.guild_id), already_configured=has_train_config(w.guild_id)
    )
    a = _Answers()
    try:
        await _confirm_reentry(w, s)
        await w.channel.send(
            "⚙️ **Train Schedule Setup**\n*Configure how the train schedule works for your alliance.*"
        )
        await _ask_tab(w, s, a)
        await _ask_blurbs(w, s, a)
        # Free-tier slot caps for themes / tones (None = unlimited).
        # Also used by the reminder-channel step to expose threads on premium.
        w.is_premium = await premium.is_premium(
            w.guild_id, interaction=interaction, bot=interaction.client
        )
        if a.blurbs_enabled:
            await _ask_blurb_details(w, s, a)
        await _ask_reminders(w, s, a)
        await _ask_train_dm(w, s, a)
    except _Abort:
        wizard_registry.unregister(w.user.id, w.cancel_event)
        return

    _save(w, a)
    embed = _summary_embed(w, a)

    # ── Step 9: Conductor Rotation (#55) ──
    # Run rotation BEFORE sending the summary so the embed reflects it, and so
    # the preset editor (if opened) is the last thing posted rather than buried
    # above this summary (#302).
    rot = await _setup()._run_train_rotation_step(
        bot=bot,
        interaction=interaction,
        channel=w.channel,
        user=w.user,
        guild_id=w.guild_id,
        cancel_event=w.cancel_event,
        guild_tz=w.guild_tz,
    )
    if rot is None:
        # Cancelled / timed out inside Step 9 — it already messaged the user.
        wizard_registry.unregister(w.user.id, w.cancel_event)
        return
    # The message-driven wizard is done; the preset editor below is a
    # self-contained component view, so release the wizard registration now.
    wizard_registry.unregister(w.user.id, w.cancel_event)
    embed.add_field(
        name="Conductor Rotation",
        value=rot["summary"] if rot.get("enabled") else "Disabled",
        inline=not rot.get("enabled"),
    )

    # When a preset editor is opening, it's the active surface — let leadership
    # finish (or close) it BEFORE the "Train Schedule Configured" summary lands,
    # so the summary doesn't pop up above the thing they're still editing (#302).
    if rot.get("open_editor"):
        import train_rotation_ui as ui

        await w.channel.send("Lay out your weekly pattern below, then 💾 Save preset:")
        editor_view = await ui.post_preset_editor(
            w.channel, w.guild_id, w.user.id, rot["preset"], rot["day_rules_tab"]
        )
        if editor_view is not None:
            await editor_view.wait()

    await w.channel.send(embed=embed)
    print(f"[SETUP] Train config saved for guild {w.guild_id}")

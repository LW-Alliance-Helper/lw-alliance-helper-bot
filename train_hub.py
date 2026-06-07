"""
train_hub.py — the single `/train` hub (embed + button grid) that replaced the
`/train overview|log|birthdays` subcommands and the Train Conductor Rotation
subcommands (#55). Modeled on `events_hub.py` (the lighter Events-hub pattern).

One command opens a hub that adapts to what the alliance has configured:

- **Rotation on** → 📋 This week's draft · 📊 Assignment logs ·
  📅 Schedule presets · 👤 Member rules · ⚙️ Open setup
- **Rotation off** → the legacy blurb surface: 📋 Schedule overview ·
  📜 Prompt log · 🎂 Run birthday check · ⚙️ Open setup

Every button dispatches into a flow that already exists (the preset editor,
the weekly draft view, the legacy TrainActionView, the setup wizard); the hub
is just the single front door. Preset + member-rule management — which used to
be slash subcommands — live here as button → view flows.
"""

import asyncio
import logging
from datetime import date as date_cls, datetime, timedelta
from typing import Optional

import discord

import train_rotation as tr
import train_rotation_ui as ui

logger = logging.getLogger(__name__)

TRAIN_HUB_TITLE = "🚂 Alliance Train"
TRAIN_HUB_CMD = "/train"

# Rotation surface
TRAIN_HUB_BTN_WEEK = "📋 This week's draft"
TRAIN_HUB_BTN_LOGS = "📊 Assignment logs"
TRAIN_HUB_BTN_PRESETS = "📅 Schedule presets"
TRAIN_HUB_BTN_MEMBER_RULES = "👤 Member rules"
# Legacy blurb surface
TRAIN_HUB_BTN_OVERVIEW = "📋 Schedule overview"
TRAIN_HUB_BTN_LOG = "📜 Prompt log"
TRAIN_HUB_BTN_BIRTHDAYS = "🎂 Run birthday check"
# Always
TRAIN_HUB_BTN_SETUP = "⚙️ Open setup"

_DENY_NOT_OWNER = "⛔ Only the person who opened this hub can use these buttons."


# ── Embed ─────────────────────────────────────────────────────────────────────


def _build_train_hub_embed(bot, guild_id: int) -> discord.Embed:
    """Hub embed showing the alliance's current train config. DB-only reads
    (config), so it's cheap to render on every open."""
    from config import get_config, get_train_config

    cfg = get_config(guild_id)
    tcfg = get_train_config(guild_id)
    rotation_on = bool(tcfg.get("rotation_enabled"))
    tz = cfg.timezone if cfg else None

    embed = discord.Embed(title=TRAIN_HUB_TITLE, color=discord.Color.gold())

    def _ch(cid):
        return f"<#{cid}>" if cid else "*not set*"

    def _time(hhmm):
        # Canonical "6:00pm EDT" renderer, shared with the setup summaries.
        from setup_cog import _format_time_with_tz

        return _format_time_with_tz(hhmm, tz)

    if rotation_on:
        embed.description = (
            "Conductor Rotation is **on**. Pick an action below. The weekly "
            "draft and daily confirmation also post automatically."
        )
        draft_day = tr.WEEKDAY_NAMES[int(tcfg.get("weekly_draft_day", 6))]
        rem_ch = tcfg.get("reminder_channel_id", 0) or (cfg.leadership_channel_id if cfg else 0)
        time_str = _time(tcfg.get("reminder_time", "22:00"))
        pub = tcfg.get("rotation_public_channel_id", 0) or 0
        lines = [
            f"**Active preset:** {tcfg.get('active_schedule_preset') or tr.DEFAULT_PRESET_NAME}",
            f"**Weekly draft:** {draft_day} at {time_str} → {_ch(rem_ch)}",
            f"**Daily confirmation:** {time_str} → {_ch(rem_ch)}",
            f"**Public posts:** {_ch(pub) if pub else 'off (record only)'}",
        ]
        embed.add_field(name="✅ Conductor Rotation", value="\n".join(lines), inline=False)
    else:
        embed.description = (
            "Manage your alliance's train schedule. Turn on **Conductor "
            "Rotation** from setup to have the bot draft fair conductors for you."
        )
        blurbs = "✅ on" if tcfg.get("blurbs_enabled") else "❌ off"
        reminders = tcfg.get("reminders_enabled")
        rem_ch = tcfg.get("reminder_channel_id", 0) or (cfg.leadership_channel_id if cfg else 0)
        lines = [
            f"**Blurb prompts:** {blurbs}",
            "**Daily reminder:** "
            + (
                f"{_time(tcfg.get('reminder_time', '22:00'))} → {_ch(rem_ch)}"
                if reminders
                else "❌ off"
            ),
            "**Conductor Rotation:** ❌ off",
        ]
        embed.add_field(name="Schedule", value="\n".join(lines), inline=False)

    embed.set_footer(text="Train hub · buttons below")
    return embed


# ── Hub view ──────────────────────────────────────────────────────────────────


class _TrainHubView(discord.ui.View):
    """Hub button grid, adapting to whether rotation is on."""

    def __init__(self, bot, guild_id: int, owner_user_id: int, *, rotation_on: bool):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.rotation_on = rotation_on
        self.message: Optional[discord.Message] = None
        self._build_buttons()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=TRAIN_HUB_CMD)

    def _add(self, label, style, row, cb):
        btn = discord.ui.Button(label=label[:80], style=style, row=row)
        btn.callback = cb
        self.add_item(btn)

    def _build_buttons(self):
        if self.rotation_on:
            self._add(TRAIN_HUB_BTN_WEEK, discord.ButtonStyle.primary, 0, self._on_week)
            self._add(TRAIN_HUB_BTN_LOGS, discord.ButtonStyle.secondary, 0, self._on_logs)
            self._add(TRAIN_HUB_BTN_PRESETS, discord.ButtonStyle.success, 1, self._on_presets)
            self._add(
                TRAIN_HUB_BTN_MEMBER_RULES, discord.ButtonStyle.success, 1, self._on_member_rules
            )
            self._add(TRAIN_HUB_BTN_SETUP, discord.ButtonStyle.secondary, 2, self._on_setup)
        else:
            self._add(TRAIN_HUB_BTN_OVERVIEW, discord.ButtonStyle.primary, 0, self._on_overview)
            self._add(TRAIN_HUB_BTN_LOG, discord.ButtonStyle.secondary, 0, self._on_log)
            self._add(TRAIN_HUB_BTN_BIRTHDAYS, discord.ButtonStyle.secondary, 0, self._on_birthdays)
            self._add(TRAIN_HUB_BTN_SETUP, discord.ButtonStyle.secondary, 1, self._on_setup)

    # ── rotation callbacks ────────────────────────────────────────────────────

    async def _on_week(self, inter: discord.Interaction):
        await _open_week_draft(self.bot, inter)

    async def _on_logs(self, inter: discord.Interaction):
        await _render_logs(self.bot, inter)

    async def _on_presets(self, inter: discord.Interaction):
        await _open_presets_manage(self.bot, inter)

    async def _on_member_rules(self, inter: discord.Interaction):
        await _open_member_rules_manage(self.bot, inter)

    # ── legacy callbacks ──────────────────────────────────────────────────────

    async def _on_overview(self, inter: discord.Interaction):
        await _open_overview(self.bot, inter)

    async def _on_log(self, inter: discord.Interaction):
        await _render_prompt_log(self.bot, inter)

    async def _on_birthdays(self, inter: discord.Interaction):
        await _run_birthday_check(self.bot, inter)

    async def _on_setup(self, inter: discord.Interaction):
        from setup_cog import run_train_setup

        # The wizard talks in-channel via channel.send; ack the button first.
        await inter.response.send_message("⚙️ Opening train setup below…", ephemeral=True)
        await run_train_setup(inter, self.bot)


# ── Rotation dispatch ─────────────────────────────────────────────────────────


async def _open_week_draft(bot, interaction: discord.Interaction):
    await interaction.response.defer()
    guild_id = interaction.guild_id
    from config import get_train_config

    tcfg = get_train_config(guild_id)
    today = ui._guild_today(bot, guild_id)
    # Default to the week leadership is most likely planning: the current week,
    # but the upcoming week once it's the configured draft day (#304).
    week_start = ui.default_draft_week(today, int(tcfg.get("weekly_draft_day", 6)))
    draft = await asyncio.to_thread(ui.load_week_draft, bot, guild_id, week_start)

    preset_name = tcfg.get("active_schedule_preset") or "Standard Week"
    view = ui.WeeklyDraftView(bot, guild_id, draft, week_start, preset_name)
    view.message = await interaction.followup.send(
        embed=ui.build_weekly_draft_embed(draft, week_start, preset_name), view=view
    )


async def _render_logs(bot, interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id
    # Full state gives the roster, so "fewest trains" can surface members who've
    # driven zero times and therefore have no history rows at all.
    state = await asyncio.to_thread(ui.load_rotation_state, bot, guild_id)
    today = ui._guild_today(bot, guild_id)
    tally = tr.member_tally(
        state.eligible_pool, state.history, state.counted_reasons, state.member_rules, today
    )
    posted = [h for h in state.history if h.status == tr.STATUS_POSTED]
    view = ui.AssignmentLogsView(interaction.user.id, tally, posted)
    view.message = await interaction.followup.send(
        embed=view.render_embed(), view=view, ephemeral=True
    )


# ── Legacy dispatch ───────────────────────────────────────────────────────────


async def _open_overview(bot, interaction: discord.Interaction):
    from config import get_train_config
    from train import load_schedule, load_blurb_log, build_train_view_embed
    from train_ui import TrainActionView

    await interaction.response.defer()
    guild_id = interaction.guild_id
    blurbs_on = bool(get_train_config(guild_id).get("blurbs_enabled", 1))
    schedule = await asyncio.to_thread(load_schedule, guild_id)
    blurb_log = await asyncio.to_thread(load_blurb_log, guild_id)
    embed = build_train_view_embed(schedule, blurb_log)
    view = TrainActionView(bot, guild_id, blurbs_on)
    await interaction.followup.send(embed=embed, view=view)


async def _render_prompt_log(bot, interaction: discord.Interaction):
    import premium
    from train import load_schedule

    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id
    schedule = await asyncio.to_thread(load_schedule, guild_id)
    window = (
        await premium.get_limit("train_log_days", guild_id, interaction=interaction, bot=bot) or 30
    )
    today = date_cls.today()
    cutoff = today - timedelta(days=window)
    recent = []
    for date_str, entry in schedule.items():
        try:
            d = date_cls.fromisoformat(date_str)
        except ValueError:
            continue
        if cutoff <= d <= today + timedelta(days=window):
            recent.append((d, entry))
    recent.sort(key=lambda t: t[0], reverse=True)
    embed = discord.Embed(title="🚂 Train Prompt Log", color=discord.Color.blurple())
    if not recent:
        embed.description = f"*No train entries in the past {window} days.*"
    else:
        lines = []
        for d, entry in recent[:20]:
            retrieved = "✅" if entry.get("prompt_retrieved") else "❌"
            name = entry.get("name") or "*unset*"
            theme = entry.get("theme") or ""
            bits = [f"**{d:%a %b} {d.day}**: {name}"]
            if theme:
                bits.append(theme)
            bits.append(f"prompt {retrieved}")
            lines.append("• " + " · ".join(bits))
        embed.description = "\n".join(lines)[:4000]
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _run_birthday_check(bot, interaction: discord.Interaction):
    from train import load_schedule, save_schedule, check_and_add_birthdays, BIRTHDAY_LOOKAHEAD

    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id
    schedule = await asyncio.to_thread(load_schedule, guild_id)
    before = len(schedule)
    updated, alerts = await asyncio.to_thread(check_and_add_birthdays, schedule, guild_id)
    added = len(updated) - before
    if added > 0 or alerts:
        await asyncio.to_thread(save_schedule, updated, guild_id)
    for alert in alerts:
        if interaction.channel:
            await interaction.channel.send(alert)
    if added > 0:
        await interaction.followup.send(
            f"✅ Birthday check complete. Added **{added}** entr{'y' if added == 1 else 'ies'}."
            + (f" ⚠️ {len(alerts)} conflict(s) posted above." if alerts else ""),
            ephemeral=True,
        )
    elif alerts:
        await interaction.followup.send(
            f"⚠️ {len(alerts)} conflict(s) posted above require manual action.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"✅ Birthday check complete. Nothing new within {BIRTHDAY_LOOKAHEAD} days.",
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# Schedule-preset management (the old /train schedule_preset … subcommands)
# ══════════════════════════════════════════════════════════════════════════════


class _PresetNameModal(discord.ui.Modal, title="Create schedule preset"):
    def __init__(self, on_name):
        super().__init__()
        self._on_name = on_name
        self.name = discord.ui.TextInput(
            label="Preset name", placeholder="e.g. VS Save Week", required=True, max_length=60
        )
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction):
        await self._on_name(interaction, self.name.value.strip())


class _PresetPickerView(discord.ui.View):
    """Dropdown of presets → callback with the chosen name. Used for edit /
    set-active / delete."""

    def __init__(self, names: list[str], owner_id: int, on_pick, *, exclude: str | None = None):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self._on_pick = on_pick
        opts = [
            discord.SelectOption(label=n[:100], value=n)
            for n in names
            if exclude is None or n != exclude
        ][:25]
        sel = discord.ui.Select(placeholder="Pick a preset…", options=opts)
        sel.callback = self._cb
        self._sel = sel
        self.add_item(sel)

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _cb(self, inter: discord.Interaction):
        self._sel.disabled = True
        await self._on_pick(inter, self._sel.values[0])
        self.stop()


class PresetsManageView(discord.ui.View):
    """Owner-locked preset management: list + Create / Edit / Set active / Delete."""

    def __init__(self, bot, guild_id: int, owner_id: int, day_rules_tab: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.day_rules_tab = day_rules_tab
        self.message: Optional[discord.Message] = None
        self._add("➕ Create", discord.ButtonStyle.success, self._create)
        self._add("✏️ Edit", discord.ButtonStyle.primary, self._edit)
        self._add("⭐ Set active", discord.ButtonStyle.secondary, self._set_active)
        self._add("🗑️ Delete", discord.ButtonStyle.danger, self._delete)

    def _add(self, label, style, cb):
        btn = discord.ui.Button(label=label, style=style)
        btn.callback = cb
        self.add_item(btn)

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    def _active(self) -> str:
        from config import get_train_config

        return (
            get_train_config(self.guild_id).get("active_schedule_preset") or tr.DEFAULT_PRESET_NAME
        )

    async def _create(self, inter: discord.Interaction):
        async def _make(i: discord.Interaction, name: str):
            existing = await asyncio.to_thread(tr.list_presets, self.guild_id, self.day_rules_tab)
            if any(n.lower() == name.lower() for n in existing):
                await i.response.send_message(
                    f"⚠️ A preset named **{name}** already exists.", ephemeral=True
                )
                return
            await i.response.defer()
            await ui.post_preset_editor(
                i.channel,
                self.guild_id,
                i.user.id,
                tr.SchedulePreset.default(name),
                self.day_rules_tab,
            )

        await inter.response.send_modal(_PresetNameModal(_make))

    async def _edit(self, inter: discord.Interaction):
        names = await asyncio.to_thread(tr.list_presets, self.guild_id, self.day_rules_tab)
        if not names:
            await inter.response.send_message(
                "ℹ️ No presets yet. Hit **➕ Create**.", ephemeral=True
            )
            return

        async def _pick(i: discord.Interaction, name: str):
            await i.response.defer()
            preset = await asyncio.to_thread(
                tr.load_preset, self.guild_id, self.day_rules_tab, name
            ) or tr.SchedulePreset.default(name)
            await ui.post_preset_editor(
                i.channel, self.guild_id, i.user.id, preset, self.day_rules_tab
            )

        await inter.response.send_message(
            "Pick a preset to edit:",
            view=_PresetPickerView(names, self.owner_id, _pick),
            ephemeral=True,
        )

    async def _set_active(self, inter: discord.Interaction):
        names = await asyncio.to_thread(tr.list_presets, self.guild_id, self.day_rules_tab)
        if not names:
            await inter.response.send_message("ℹ️ No presets yet.", ephemeral=True)
            return

        async def _pick(i: discord.Interaction, name: str):
            from config import update_train_config_field

            await asyncio.to_thread(
                update_train_config_field, self.guild_id, "active_schedule_preset", name
            )
            await i.response.send_message(
                f"⭐ **{name}** is now the active preset. The next weekly draft uses it.",
                ephemeral=True,
            )

        await inter.response.send_message(
            "Pick the preset to make active:",
            view=_PresetPickerView(names, self.owner_id, _pick),
            ephemeral=True,
        )

    async def _delete(self, inter: discord.Interaction):
        names = await asyncio.to_thread(tr.list_presets, self.guild_id, self.day_rules_tab)
        active = self._active()
        deletable = [n for n in names if n != active]
        if not deletable:
            await inter.response.send_message(
                "⚠️ Nothing to delete. You can't delete the active preset or your only one.",
                ephemeral=True,
            )
            return

        async def _pick(i: discord.Interaction, name: str):
            ok = await asyncio.to_thread(tr.delete_preset, self.guild_id, self.day_rules_tab, name)
            await i.response.send_message(
                f"🗑️ Deleted **{name}**." if ok else "⚠️ Couldn't delete that preset.",
                ephemeral=True,
            )

        await inter.response.send_message(
            "Pick a preset to delete (the active preset is excluded):",
            view=_PresetPickerView(deletable, self.owner_id, _pick),
            ephemeral=True,
        )


def _build_presets_embed(guild_id: int, day_rules_tab: str) -> discord.Embed:
    from config import get_train_config

    active = get_train_config(guild_id).get("active_schedule_preset") or tr.DEFAULT_PRESET_NAME
    names = tr.list_presets(guild_id, day_rules_tab)
    embed = discord.Embed(title="📅 Schedule Presets", color=discord.Color.gold())
    if not names:
        embed.description = "*No presets yet. Hit ➕ Create to make one.*"
    else:
        embed.description = "\n".join(
            (f"⭐ **{n}** *(active)*" if n == active else f"• {n}") for n in names
        )
    embed.set_footer(text="⭐ = active preset (drives the weekly draft)")
    return embed


async def _open_presets_manage(bot, interaction: discord.Interaction):
    from config import get_train_config

    tab = get_train_config(interaction.guild_id).get("day_rules_tab") or ""
    embed = await asyncio.to_thread(_build_presets_embed, interaction.guild_id, tab)
    view = PresetsManageView(bot, interaction.guild_id, interaction.user.id, tab)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()


# ══════════════════════════════════════════════════════════════════════════════
# Member-rule management (the old /train member_rule … subcommands)
# ══════════════════════════════════════════════════════════════════════════════


class _AddMemberRuleModal(discord.ui.Modal, title="Add a member rule"):
    """One modal covers both rule types: a blank date = opt out entirely; a
    date = skip the member until then."""

    def __init__(self, on_submit_cb):
        super().__init__()
        self._cb = on_submit_cb
        self.member = discord.ui.TextInput(
            label="Member name",
            placeholder="As it appears on your roster",
            required=True,
            max_length=80,
        )
        self.skip_until = discord.ui.TextInput(
            label="Skip until (blank = opt out entirely)",
            placeholder="2026-07-01, or leave blank to opt out entirely",
            required=False,
            max_length=10,
        )
        self.add_item(self.member)
        self.add_item(self.skip_until)

    async def on_submit(self, interaction: discord.Interaction):
        await self._cb(interaction, self.member.value.strip(), self.skip_until.value.strip())


class MemberRulesManageView(discord.ui.View):
    """Owner-locked member-rule management: Add / Remove."""

    def __init__(self, bot, guild_id: int, owner_id: int, tab: str):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.tab = tab
        self.message: Optional[discord.Message] = None
        add_btn = discord.ui.Button(label="➕ Add rule", style=discord.ButtonStyle.success)
        add_btn.callback = self._add_rule
        self.add_item(add_btn)
        rm_btn = discord.ui.Button(label="🗑️ Remove rule", style=discord.ButtonStyle.danger)
        rm_btn.callback = self._remove_rule
        self.add_item(rm_btn)

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _refresh_message(self):
        if self.message:
            try:
                await self.message.edit(
                    embed=await asyncio.to_thread(
                        _build_member_rules_embed, self.guild_id, self.tab
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass

    async def _add_rule(self, inter: discord.Interaction):
        async def _save(i: discord.Interaction, member: str, skip_until: str):
            if not member:
                await i.response.send_message("⚠️ Member name required.", ephemeral=True)
                return
            if skip_until:
                parsed = tr._parse_iso(skip_until)
                if parsed is None:
                    await i.response.send_message(
                        "⚠️ skip-until needs a date like `2026-07-01`.", ephemeral=True
                    )
                    return
                await asyncio.to_thread(
                    tr.set_member_rule,
                    self.guild_id,
                    self.tab,
                    member,
                    tr.MEMBER_RULE_SKIP_UNTIL,
                    parsed.isoformat(),
                    "",
                )
                msg = f"✅ **{member}** skipped until **{parsed.isoformat()}**."
            else:
                await asyncio.to_thread(
                    tr.set_member_rule,
                    self.guild_id,
                    self.tab,
                    member,
                    tr.MEMBER_RULE_OPT_OUT,
                    "",
                    "",
                )
                msg = f"✅ **{member}** opted out of the rotation."
            await i.response.send_message(msg, ephemeral=True)
            await self._refresh_message()

        await inter.response.send_modal(_AddMemberRuleModal(_save))

    async def _remove_rule(self, inter: discord.Interaction):
        rules = await asyncio.to_thread(tr.load_member_rules, self.guild_id, self.tab)
        members = []
        seen = set()
        for r in rules:
            key = r.member.strip().lower()
            if key and key not in seen:
                seen.add(key)
                members.append(r.member)
        if not members:
            await inter.response.send_message("ℹ️ No member rules to remove.", ephemeral=True)
            return
        opts = [discord.SelectOption(label=m[:100], value=m) for m in members[:25]]
        sel = discord.ui.Select(placeholder="Pick a member to clear…", options=opts)
        picker = discord.ui.View(timeout=120)
        owner = self.owner_id

        async def _on_pick(i: discord.Interaction):
            name = sel.values[0]
            await asyncio.to_thread(tr.clear_member_rule, self.guild_id, self.tab, name, None)
            sel.disabled = True
            await i.response.edit_message(
                content=f"🗑️ Cleared all rules for **{name}**.", view=picker
            )
            await self._refresh_message()

        async def _check(i):
            if i.user.id != owner:
                await i.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
                return False
            return True

        sel.callback = _on_pick
        picker.interaction_check = _check
        picker.add_item(sel)
        await inter.response.send_message("Pick a member to clear:", view=picker, ephemeral=True)


def _build_member_rules_embed(guild_id: int, tab: str) -> discord.Embed:
    rules = tr.load_member_rules(guild_id, tab)
    embed = discord.Embed(title="👤 Member Rotation Rules", color=discord.Color.gold())
    if not rules:
        embed.description = "*No member rules set. Everyone's in the rotation.*"
    else:
        lines = []
        for r in rules:
            if r.rule_type == tr.MEMBER_RULE_OPT_OUT:
                lines.append(f"• **{r.member}**: opted out")
            elif r.rule_type == tr.MEMBER_RULE_SKIP_UNTIL:
                lines.append(f"• **{r.member}**: skipped until {r.value}")
        embed.description = "\n".join(lines)[:4000]
    return embed


async def _open_member_rules_manage(bot, interaction: discord.Interaction):
    from config import get_train_config

    tab = get_train_config(interaction.guild_id).get("member_rules_tab") or ""
    embed = await asyncio.to_thread(_build_member_rules_embed, interaction.guild_id, tab)
    view = MemberRulesManageView(bot, interaction.guild_id, interaction.user.id, tab)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()


# ── Entry point ───────────────────────────────────────────────────────────────


async def handle_train_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/train`. Leadership-gated via train._guard
    (setup-complete + leadership role)."""
    from train import _guard

    if not await _guard(interaction):
        return

    rotation_on = False
    try:
        from config import get_train_config

        rotation_on = bool(get_train_config(interaction.guild_id).get("rotation_enabled"))
    except Exception:
        rotation_on = False

    embed = _build_train_hub_embed(bot, interaction.guild_id)
    view = _TrainHubView(bot, interaction.guild_id, interaction.user.id, rotation_on=rotation_on)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()

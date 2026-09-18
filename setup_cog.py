"""
setup_cog.py — /setup_* wizards for new guilds

Walks a server admin through configuring the bot using Discord's native
role and channel select menus. All values are saved to the config database.

Holds the `/setup` slash command (which opens the setup hub from
setup_hub.py) plus every per-feature wizard handler the hub
dispatches into. The 11 pre-#201 `/setup_*` slash commands
collapsed into hub buttons; their bodies are now module-level
`_launch_*_setup` helpers exposed at the bottom of this file.
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from config import (
    get_config,
    get_or_create_config,
    save_config,
    update_config_field,
    GuildConfig,
    normalize_spreadsheet_id,
)
import config_health
import premium
import wizard_registry
from wizard_registry import ExpiringView, OwnedView
from messages import (
    CANCEL_PLAIN,
    GENERIC_CMD_TIMEOUT,
    INPUT_INVALID,
    PREV_CHANNEL_GONE,
    SETUP_POINTER_FOOTER,
    TIER_COMPARISON,
    TIME_PARSE_GIVE_UP,
    TIME_PARSE_RETRY,
    WIZARD_TIMEOUT,
)
from setup_hub import (
    STORM_GLYPH,
    HUB_BTN_BIRTHDAYS,
    HUB_BTN_BREAKDOWN,
    HUB_BTN_EVENTS,
    HUB_BTN_GROWTH,
    HUB_BTN_SHINY,
    HUB_BTN_SURVEY,
    HUB_BTN_TRAIN,
)
from storm_event_hub import (
    HUB_COMMAND,
    HUB_BTN_POST_SIGNUP,
    HUB_BTN_PRESETS,
    HUB_BTN_RULES,
)
from storm_setup_structured import run_structured_flow_step as _run_structured_flow_setup_step

# The storm wizard itself lives in storm_setup.py (#611 round 2); imported back
# under its old name for setup_hub, the launcher and the tests.
from storm_setup import run_storm_setup

# The Buddy System wizard lives in buddy_setup.py (#611 round 2); imported back
# under its old name for the hub, the launcher and the tests.
from buddy_setup import run_buddy_setup

# The train wizard, its template manager and the Conductor Rotation step live
# in train_setup.py / train_setup_rotation.py (#611 round 2); imported back
# under the old names for the hub, the launcher and the tests.
from train_setup import run_train_setup
from train_setup import manage_train_templates as _manage_train_templates
from train_setup_rotation import (
    _RuleRoleAttachView,
    _WeekdaySelectView,
    run_rotation_step as _run_train_rotation_step,
)

# The survey wizard and the add / edit / remove flows around it live in
# survey_setup.py (#611 round 2); imported back under their old names for
# the survey hub, the launcher and the tests.
from survey_setup import (
    run_create_new_extra_survey,
    run_pick_survey_to_edit,
    run_remove_extra_survey,
    run_survey_setup,
)
from wizard_registry import wait_view_or_cancel

# View timeout duration (seconds) for each wizard step. Distinct from the
# imported `WIZARD_TIMEOUT` message template (messages.py) used by the
# `.format(wizard=...)` timeout notices — they must not share a name, or the
# int shadows the string and every timeout notice crashes (#290).
# Shared wizard pieces live in wizard_steps.py (#611 step 1). Imported back
# by name so every wizard here, every `from setup_cog import X` elsewhere and
# every `patch("setup_cog.X")` in the tests keep resolving.
from wizard_steps import (
    WIZARD_STEP_TIMEOUT,
    CreateRoleModal,
    RoleSelectStep,
    CreateChannelModal,
    ChannelSelectStep,
    ConfirmView,
    TextInputModal,
    ModalLaunchView,
    ask_keep_or_change,
    TIMEZONE_OPTIONS,
    TIMEZONE_LABELS,
    TimezoneSelectView,
    ScheduleTypeView,
    YesNoView,
    _KeepOrFlipYesNoGate,
)

# The time parsers and the sign-up schedule sub-flow live in wizard_time.py
# (#611 round 2); imported back by name for the same reasons.
from wizard_time import (
    _DOW_NAMES,
    _ask_signup_schedule,
    _format_24h_to_12h,
    _format_time_with_tz,
    _normalise_hhmm,
    _parse_12h_time,
    _parse_month_day,
)

# The participation step lives in storm_setup_participation.py (#611 round 2);
# imported back under the old names for the storm wizard and the tests.
from storm_setup_participation import (
    _PARTICIPATION_FREE_TYPES,
    _PARTICIPATION_PER_MEMBER_TYPES,
    _PARTICIPATION_PREMIUM_TYPE_SHORT,
    _PARTICIPATION_PREMIUM_TYPES,
    _PARTICIPATION_TYPE_LABELS,
    build_participation_question as _build_participation_question,
    run_participation_step as _run_storm_participation_step,
    run_preset_picker_step as _run_participation_preset_picker_step,
    wait_for_msg_simple,
)


# The Survey picker's Premium answer types, as `{value: (label, short)}`.
# The option and the free-tier note that names it read from one entry, so a
# re-marked type cannot end up advertised under its old glyph. The Desert /
# Canyon Storm participation picker keeps the same pairing in
# `_PARTICIPATION_TYPE_LABELS` / `_PARTICIPATION_PREMIUM_TYPE_SHORT`.
_SURVEY_PREMIUM_TYPES = {
    "multi_select": ("Multi-select — pick multiple options", "Multi-select"),
    "date": ("📅 Date — formatted date entry", "📅 Date"),
}


def _locked_types_note(*type_names: str) -> str:
    """Free-tier note naming the Premium answer types a picker leaves out.

    Appended to a question-type prompt when the guild is on the free tier.
    `DESIGN.md`'s Premium rule wants the free tier to see the shape of the
    paid product, and a select cannot show it the way a button does:
    `SelectOption` has no `disabled` parameter, so a shown option is a
    selectable one. Naming the types in the prompt is how that rule is
    honored on a select without making anything new pickable.

    🔒 rather than 💎: the State catalog separates them, and prose
    telling an officer they cannot have something yet is the locked case.
    This is a tier note on a prompt — the same shape as the growth
    wizard's frequency step — not an inline gate, so
    `messages.PREMIUM_LOCKED_INLINE` does not apply: nothing is refused
    here, because nothing was offered.

    Both pickers gate at least two types today. One is handled anyway,
    because the alternative is a note reading "types are Premium:  and X".
    """
    if not type_names:
        raise ValueError("a tier note has to name at least one type")
    if len(type_names) == 1:
        lead, names, obj = "One more answer type is", type_names[0], "it"
    else:
        lead = "More answer types are"
        names = ", ".join(type_names[:-1]) + f" and {type_names[-1]}"
        obj = "them"
    return f"\n*🔒 {lead} 💎 Premium: {names}. Run `/upgrade` to unlock {obj}.*"


async def warn_if_tab_claimed(
    channel,
    guild_id: int,
    tab: str,
    *,
    exclude_field: str,
    exclude_survey_id: str | None = None,
) -> bool:
    """Tell leadership when a tab they just named already belongs elsewhere.

    A warning, not a rejection: some overlaps are deliberate (pointing
    two features at one roster tab is a reasonable thing to want), and
    the bot can't tell those from a typo. What it can do is make sure
    nobody discovers it by finding one feature's columns written over
    another's.

    Returns True when a warning was posted, for callers that want to
    know. Never raises — a warning failing must not end a wizard.
    """
    try:
        from config import tabs_in_use

        owner = tabs_in_use(
            guild_id, exclude_field=exclude_field, exclude_survey_id=exclude_survey_id
        ).get((tab or "").casefold())
    except Exception as e:
        print(f"[SETUP] tab claim check failed guild={guild_id}: {type(e).__name__}: {e}")
        return False

    if not owner:
        return False

    await channel.send(
        f"⚠️ Heads up: **{tab}** is also {owner}. Two features writing to one "
        f"tab will overwrite each other's columns. That's fine if you meant "
        f"it, otherwise pick a different tab here or in that feature's setup."
    )
    return True


async def ask_proceed_with_existing_config(
    channel,
    *,
    title: str,
    description: str,
    fields: list[tuple[str, str]],
    cancel_event,
    no_changes_message: str = "✅ No changes made. Your existing configuration is still active.",
) -> bool | None:
    """Show an existing-config summary embed with Edit / No changes buttons.

    Each per-feature `/setup_*` calls this at the top so leadership sees
    what's saved without walking the whole wizard. Returns:

      * ``True``  — leadership clicked Edit. Caller should proceed into
        the wizard's step-by-step flow.
      * ``False`` — leadership clicked No changes. This helper has
        already posted ``no_changes_message``; caller should return.
      * ``None``  — `/cancel` or timeout. Caller should return silently
        (timeout case posts no message — keep parity with the other
        cancellable views in the wizard).

    `fields` is a list of ``(label, value)`` tuples rendered as
    embed fields, inline=False. Pass the same tuples that
    ``/setup` → 🗂️ View configuration` would render for that feature.
    """

    class EditOrCancelView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.proceed = None

        @discord.ui.button(label="✏️ Edit settings", style=discord.ButtonStyle.primary)
        async def edit(self, inter: discord.Interaction, button: discord.ui.Button):
            self.proceed = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✅ No changes needed", style=discord.ButtonStyle.secondary)
        async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
            self.proceed = False
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
    )
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)

    view = EditOrCancelView()
    await channel.send(embed=embed, view=view)
    await wait_view_or_cancel(view, cancel_event)
    if view.cancelled:
        return None
    if view.proceed is None:
        # Timed out without an interaction.
        return None
    if not view.proceed:
        await channel.send(no_changes_message)
        return False
    return True


async def ask_disable_with_clear(
    channel,
    *,
    feature_label: str,
    setup_command: str,
    had_prior_config: bool,
    clear_fn,
    cancel_event,
) -> None:
    """Post the disable confirmation after leadership picks No on an
    enable-toggle wizard step.

    When ``had_prior_config`` is True, the message tells leadership their
    saved config is preserved and shows a Clear button that calls
    ``clear_fn()`` to wipe it. When False (first-time disable, nothing
    saved to lose), just posts the bare confirmation without the button.

    ``feature_label`` — friendly noun for the message body
    (e.g. "Shiny Tasks announcement").

    ``setup_command`` — slash navigation leadership should re-run to
    re-enable, sans the leading slash (e.g. "setup → 🌟 Shiny Tasks").
    Post-#201 every wizard lives behind a /setup hub button; pass the
    hub navigation hint here so the rendered message reads
    "Re-run `/setup → 🌟 Shiny Tasks` and pick Yes to restore."

    ``clear_fn`` — callable taking no arguments; runs synchronously
    or via ``await`` (the helper auto-detects). Should wipe the
    feature's saved config so a future re-enable starts clean. Each
    wizard supplies its own — typically a ``DELETE FROM <table>
    WHERE guild_id = ?`` since ``get_*_config`` already returns a
    default dict when the row is absent.
    """
    import inspect

    if not had_prior_config:
        await channel.send(f"✅ {feature_label} disabled.")
        return

    body = (
        f"✅ {feature_label} disabled. Your previous configuration is saved. "
        f"Re-run `/{setup_command}` and pick Yes to restore it instantly."
    )

    class ClearConfigView(ExpiringView):
        timeout_hint = f"`/{setup_command}`"

        def __init__(self):
            super().__init__(timeout=300)
            self.message: discord.Message | None = None
            self.cleared = False

        @discord.ui.button(
            label="🗑️ Clear my saved configuration",
            style=discord.ButtonStyle.danger,
        )
        async def clear(self, inter: discord.Interaction, button: discord.ui.Button):
            try:
                if inspect.iscoroutinefunction(clear_fn):
                    await clear_fn()
                else:
                    clear_fn()
            except Exception as e:
                await wizard_registry.safe_edit_response(
                    inter,
                    content=(f"{body}\n\n⚠️ Could not clear configuration: {e}"),
                    view=None,
                )
                self.stop()
                return
            self.cleared = True
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ {feature_label} disabled and saved configuration cleared.",
                view=self,
            )
            self.stop()

    view = ClearConfigView()
    view.message = await channel.send(body, view=view)
    await wait_view_or_cancel(view, cancel_event)


# ── Define Various Setup Commands ────────────────────────────────────────────────────────────────────────


def _has_leadership_or_admin(interaction: discord.Interaction) -> bool:
    """
    True if the invoking user is a server administrator OR has the
    configured leadership role. Used by per-feature /setup_* wizards so
    that day-to-day leadership members can configure features without
    needing full server-admin permissions.
    """
    # In a DM, interaction.guild is None and interaction.user is a
    # discord.User with no guild_permissions — accessing it there was the
    # #271 crash. /setup carries @app_commands.guild_only() so it can't be
    # invoked in a DM, but this helper is also reached from in-guild hub
    # buttons / member sync, so guard on the guild being present.
    if interaction.guild is None:
        return False
    if interaction.user.guild_permissions.administrator:
        return True
    cfg = get_config(interaction.guild_id)
    if cfg and cfg.leadership_role_name:
        if cfg.leadership_role_name in [r.name for r in interaction.user.roles]:
            return True
    return False


# Permissions required for the bot to drive a wizard end-to-end in a
# given channel: read the channel, send messages, embed (for setup
# summaries), and read history (so wait_for("message") works for the
# typed-reply steps).
_WIZARD_REQUIRED_PERMS = (
    "view_channel",
    "send_messages",
    "embed_links",
    "read_message_history",
)


def _missing_wizard_perms(interaction: discord.Interaction) -> list[str]:
    """Return the list of human-readable permission names the bot is
    missing in `interaction.channel`. Empty list = the bot can drive a
    wizard here. Used as a pre-flight check at the top of /setup_*
    commands so the user gets a clear "I need these permissions in
    this channel" error instead of the generic "Something went wrong"
    that the global error handler would otherwise show after the
    wizard's first channel.send fails with 403.
    """
    me = interaction.guild.me if interaction.guild else None
    if me is None:
        return []  # DM context — wizards aren't supported in DMs anyway
    channel = interaction.channel
    if channel is None:
        return []
    perms = channel.permissions_for(me)
    missing = []
    for name in _WIZARD_REQUIRED_PERMS:
        if not getattr(perms, name, False):
            # Convert "send_messages" → "Send Messages" for the user-facing
            # message — Discord's UI uses Title Case.
            missing.append(name.replace("_", " ").title())
    return missing


async def _check_wizard_can_run(interaction: discord.Interaction, command_name: str) -> bool:
    """If the bot can run a wizard in the current channel, return True.
    Otherwise send a clear ephemeral message explaining what perms are
    missing (and how to fix), and return False. Call at the top of
    `/setup` and any setup-hub launcher that opens a wizard in the
    current channel.
    """
    missing = _missing_wizard_perms(interaction)
    if not missing:
        return True

    perm_lines = "\n".join(f"• **{p}**" for p in missing)
    channel_mention = (
        interaction.channel.mention
        if interaction.channel and hasattr(interaction.channel, "mention")
        else "this channel"
    )
    msg = (
        f"⚠️ **I can't run `/{command_name}` in {channel_mention}** — I'm missing the "
        f"following Discord permissions in this channel:\n\n"
        f"{perm_lines}\n\n"
        f"To fix this, either:\n"
        f"• Edit this channel's permissions and grant my role those permissions, or\n"
        f"• Run `/{command_name}` from a channel where I already have them (your leadership "
        f"channel is a good choice).\n\n"
        f"Once that's done, the wizard will work."
    )
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        # If even this fails (e.g., the bot can't send ephemeral
        # responses for some reason), let the global error handler
        # take over — but log it so we know.
        print(f"[SETUP] Could not deliver permission-check message for /{command_name}")
    return False


class SetupCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="setup",
        description="Open the setup hub — foundations + every feature wizard, in one place",
    )
    @app_commands.guild_only()
    async def setup(self, interaction: discord.Interaction):
        from setup_hub import handle_setup_hub

        await handle_setup_hub(self.bot, interaction)


async def _send_ack(interaction: discord.Interaction, message: str) -> None:
    """Send an ephemeral ack via whichever path the interaction state
    allows. The launcher helpers below are reachable from two entry
    points — the /setup hub's slash command (fresh response slot) and
    the storm event hub's `⚙️ Open setup` button callback (response
    slot already consumed by `safe_edit_response` disabling the button
    row). `response.send_message` only works in the fresh case;
    `followup.send` only works after the response slot is consumed.
    Branch on `response.is_done()` so the helpers don't care which
    caller invoked them.
    """
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


# Standalone launcher helpers — extracted from the pre-#201 per-feature
# `/setup_*` slash commands so the setup hub's button callbacks can
# dispatch into the existing wizard functions without re-instantiating
# the cog. Mirrors the `open_strategy_list` / `open_member_rule_list`
# pattern from the storm hub (#187).


async def _launch_train_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to open the train wizard."
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting train setup — check the channel for prompts!")
    await run_train_setup(interaction, bot)


async def _launch_buddy_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to open the buddy wizard."
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(
        interaction, "⚙️ Starting Profession Buddy System setup — check the channel for prompts!"
    )
    await run_buddy_setup(interaction, bot)


async def _launch_growth_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to open the growth wizard."
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(
        interaction, "⚙️ Starting growth tracking setup — check the channel for prompts!"
    )
    await run_growth_setup(interaction, bot)


async def _launch_growth_breakdown_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to open the breakdown wizard."
        )
        return
    if not await premium.is_premium(interaction.guild_id, interaction=interaction):
        await _send_ack(
            interaction,
            "💎 Growth Breakdown configuration is a Premium feature. The "
            "**📊 See most recent Breakdown** button on `/growth overview` "
            "(and `/growth breakdown`) works on every tier — this wizard "
            "configures the auto-post and the customizable thresholds and "
            "labels. Run `/upgrade` to subscribe.",
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(
        interaction, "⚙️ Starting Growth Breakdown setup — check the channel for prompts!"
    )
    await run_growth_breakdown_setup(interaction, bot)


async def _launch_birthday_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to open the birthday wizard."
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting birthday setup — check the channel for prompts!")
    await run_birthday_setup(interaction, bot)


async def _launch_storm_setup(interaction: discord.Interaction, bot, event_type: str) -> None:
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, f"⛔ You need the leadership role (or admin) to open the {label} wizard."
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, f"⚙️ Starting {label} setup — check the channel for prompts!")
    await run_storm_setup(interaction, bot, event_type)


async def _launch_event_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to open the event wizard."
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting event setup — check the channel for prompts!")
    await run_event_setup(interaction, bot)


async def _launch_survey_hub(interaction: discord.Interaction, bot) -> None:
    """
    Open the survey hub from the `/setup` hub's 📋 Survey button.

    Keeps that door's existing admin-or-leadership gate (the `/survey`
    command applies `survey._guard` instead), then hands off to the same
    hub surface both doors share.
    """
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to manage surveys."
        )
        return
    from survey_hub import handle_survey_hub

    await handle_survey_hub(bot, interaction)


async def _launch_shiny_tasks_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction,
            "⛔ You need the leadership role (or admin) to open the shiny-tasks wizard.",
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting Shiny Tasks setup — check the channel for prompts!")
    await run_shiny_tasks_setup(interaction, bot)


async def _run_reset_flow(interaction: discord.Interaction) -> None:
    """Reset confirmation flow, extracted from the pre-#201
    `/setup` → 🗑️ Reset configuration slash command so the setup hub's `🗑️ Reset configuration`
    button can call it without round-tripping through a slash command."""
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "⛔ Only server administrators can reset the configuration.",
            ephemeral=True,
        )
        return

    class ConfirmResetView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.confirmed = False

        @discord.ui.button(label="Yes, reset everything", style=discord.ButtonStyle.danger)
        async def confirm(self, inner: discord.Interaction, _b: discord.ui.Button):
            self.confirmed = True
            await inner.response.defer()
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, inner: discord.Interaction, _b: discord.ui.Button):
            await inner.response.defer()
            self.stop()

    view = ConfirmResetView()
    await interaction.response.send_message(
        "⚠️ Are you sure you want to reset the bot configuration for this server? "
        "This cannot be undone.",
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if view.confirmed:
        from config import save_config, GuildConfig

        save_config(GuildConfig(guild_id=interaction.guild_id))
        await interaction.followup.send(
            "✅ Configuration reset. Run `/setup` to configure the bot again.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "✅ Reset canceled. Your configuration is still active and has not been reset.",
            ephemeral=True,
        )


# ── /Define Various Setup Commands ───────────────────────────────────────────────────────

# Common timezones for the selector
# ── 🗂️ View configuration helper (used by the /setup hub button) ─────────────


async def _send_view_configuration(interaction: discord.Interaction, cfg) -> None:
    """Build and send a single embed summarising every wizard's configuration."""
    await interaction.response.defer(ephemeral=True)

    from config import (
        get_train_config,
        get_birthday_config,
        get_storm_config,
        get_survey_config,
        get_growth_config,
        get_guild_events,
        get_shiny_tasks_config,
    )

    guild_id = interaction.guild_id
    train = get_train_config(guild_id)
    birthday = get_birthday_config(guild_id)
    ds = get_storm_config(guild_id, "DS")
    cs = get_storm_config(guild_id, "CS")
    survey = get_survey_config(guild_id)
    growth = get_growth_config(guild_id)
    shiny = get_shiny_tasks_config(guild_id)
    events = get_guild_events(guild_id, active_only=True)
    is_premium_flag = await premium.is_premium(
        guild_id, interaction=interaction, bot=interaction.client
    )

    def _yn(v) -> str:
        return "✅ Configured" if v else "❌ Not configured"

    def _enabled(v) -> str:
        return "✅ Enabled" if v else "❌ Disabled"

    # #379: every channel line in this embed goes through _channel, so making
    # it health-aware covers all of them at once. Checked live rather than
    # read from stored rows: detection is reactive, so a channel that broke
    # since its loop last ran has no row yet, and this screen is exactly where
    # someone goes to ask "is my setup still good?".
    #
    # Cache-only, because this renders a dozen-plus channels in one embed and
    # the precise check costs a REST call each. The ⚠️ is a prompt to look, not
    # a diagnosis; the config-health digest carries the full explanation.
    unreachable_channels: list[int] = []

    def _channel(v) -> str:
        if not v:
            return "*not set*"
        if config_health.check_channel(interaction.client, v) is not None:
            unreachable_channels.append(v)
            return f"<#{v}> ⚠️"
        return f"<#{v}>"

    def _col_letter(idx) -> str:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return "*not set*"
        return _col_index_to_letter(i) if i >= 0 else "*not set*"

    tier_badge = "💎 Premium" if is_premium_flag else "Free tier"
    embed = discord.Embed(
        title=f"⚙️ Current Configuration  ·  {tier_badge}",
        description="All configured settings across the bot's setup wizards.",
        color=discord.Color.gold() if is_premium_flag else discord.Color.blurple(),
    )

    tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
    sheet_id_display = f"`{cfg.spreadsheet_id[:25]}...`" if cfg.spreadsheet_id else "*not set*"
    core_lines = [
        f"**Tier:** {tier_badge}",
        f"**Member Role:** {cfg.member_role_name}",
        f"**Leadership Role:** {cfg.leadership_role_name}",
        f"**Leadership Channel:** {_channel(cfg.leadership_channel_id)}",
        f"**Announcement Channel:** {_channel(cfg.announcement_channel_id)}",
        f"**Timezone:** {tz_label}",
        f"**Spreadsheet ID:** {sheet_id_display}",
        f"**Member Tab:** {cfg.tab_member_default}",
    ]
    embed.add_field(name="⚙️ Core", value="\n".join(core_lines)[:1024], inline=False)

    ev_lines = [
        f"**Draft Channel:** {_channel(cfg.event_draft_channel_id)}",
        f"**Announcement Channel:** {_channel(cfg.event_announce_channel_id)}",
        f"**Draft Time:** {_format_time_with_tz(cfg.event_draft_time, cfg.timezone)}",
    ]
    if events:
        ev_lines.append(f"**Events ({len(events)}):**")
        for e in events:
            ev_lines.append(
                f"• {e['name']} (`{e['short_key']}`) — {e['default_time']} {e['timezone']} · "
                f"blurb {_yn(e.get('announcement_blurb'))} · "
                f"warning {'custom' if e.get('warning_blurb') else 'default'}"
            )
    else:
        ev_lines.append("**Events:** *none configured*")
    embed.add_field(name=HUB_BTN_EVENTS, value="\n".join(ev_lines)[:1024], inline=False)

    train_lines = [
        f"**Schedule Tab:** {train.get('tab_name', '*not set*')}",
        f"**Blurbs:** {_enabled(train.get('blurbs_enabled'))}",
    ]
    if train.get("blurbs_enabled"):
        themes = train.get("themes") or []
        tones = train.get("tones") or []
        train_lines.append(
            f"**Themes ({len(themes)}):** " + (", ".join(themes) if themes else "*none*")
        )
        train_lines.append(
            f"**Tones ({len(tones)}):** " + (", ".join(tones) if tones else "*none*")
        )
        train_lines.append(f"**Default Tone:** {train.get('default_tone', '*not set*')}")
        train_lines.append(f"**Prompt Template:** {_yn(train.get('prompt_template'))}")
    train_lines.append(f"**Reminders:** {_enabled(train.get('reminders_enabled'))}")
    if train.get("reminders_enabled"):
        train_lines.append(f"**Reminder Channel:** {_channel(train.get('reminder_channel_id'))}")
        train_lines.append(
            f"**Reminder Time:** {_format_time_with_tz(train.get('reminder_time'), cfg.timezone) or '*not set*'}"
        )
    embed.add_field(name=HUB_BTN_TRAIN, value="\n".join(train_lines)[:1024], inline=False)

    b_lines = [
        f"**Enabled:** {_enabled(birthday.get('enabled'))}",
        f"**Source Tab:** {birthday.get('tab_name', '*not set*')}",
        f"**Name Column:** {_col_letter(birthday.get('name_col'))}",
        f"**Birthday Column:** {_col_letter(birthday.get('birthday_col'))}",
        "**Discord ID Column:** "
        + (
            _col_letter(birthday.get("discord_id_col"))
            if birthday.get("discord_id_col", -1) >= 0
            else "*not set*"
        ),
        f"**Data Start Row:** {birthday.get('data_start_row', '*not set*')}",
        f"**Lookahead Days:** {birthday.get('lookahead_days', '*not set*')}",
        f"**Train Integration:** {_enabled(birthday.get('train_integration'))}",
        f"**Reminders:** {_enabled(birthday.get('reminders_enabled'))}",
    ]
    if birthday.get("reminders_enabled"):
        b_lines.append(f"**Reminder Channel:** {_channel(birthday.get('reminder_channel_id'))}")
        b_lines.append(
            f"**Reminder Time:** {_format_time_with_tz(birthday.get('reminder_time'), cfg.timezone) or '*not set*'}"
        )
    embed.add_field(name=HUB_BTN_BIRTHDAYS, value="\n".join(b_lines)[:1024], inline=False)

    from config import get_storm_slot_labels

    ds_slot_labels = get_storm_slot_labels("DS", interaction.guild_id)
    cs_slot_labels = get_storm_slot_labels("CS", interaction.guild_id)

    def _team_time_line(team_letter: str, idx, slot_lbls, setup_hint: str) -> str:
        """Render the Team A/B time line for the /setup config view.
        Falls back to a nudge toward the setup wizard when the alliance
        hasn't picked the slot yet (#251)."""
        if idx in (1, 2) and len(slot_lbls) >= idx:
            return f"**Team {team_letter} Time:** {slot_lbls[idx - 1]}"
        return f"**Team {team_letter} Time:** *not set — Step 3 of {setup_hint}*"

    ds_hint = "`/setup` → ⚔️ Desert Storm"
    cs_hint = "`/setup` → 🛡️ Canyon Storm"
    ds_lines = [
        f"**Sheet Tab:** {ds.get('tab_name', '*not set*')}",
        f"**Log Channel:** {_channel(cfg.ds_log_channel_id)}",
        _team_time_line("A", ds.get("team_a_slot_index"), ds_slot_labels, ds_hint),
        _team_time_line("B", ds.get("team_b_slot_index"), ds_slot_labels, ds_hint),
        f"**Mail Template:** {_yn(ds.get('mail_template'))}",
    ]
    embed.add_field(name="⚔️ Desert Storm", value="\n".join(ds_lines)[:1024], inline=False)

    cs_lines = [
        f"**Sheet Tab:** {cs.get('tab_name', '*not set*')}",
        f"**Log Channel:** {_channel(cfg.cs_log_channel_id)}",
        _team_time_line("A", cs.get("team_a_slot_index"), cs_slot_labels, cs_hint),
        _team_time_line("B", cs.get("team_b_slot_index"), cs_slot_labels, cs_hint),
        f"**Mail Template:** {_yn(cs.get('mail_template'))}",
    ]
    embed.add_field(name="🛡️ Canyon Storm", value="\n".join(cs_lines)[:1024], inline=False)

    translate_bot = (
        f"<@{cfg.survey_translate_bot_id}>" if cfg.survey_translate_bot_id else "*not set*"
    )
    s_lines = [
        f"**Survey Channel:** {_channel(cfg.survey_channel_id)}",
        f"**Notify Channel:** {_channel(cfg.survey_notify_channel_id)}",
        f"**Stats Tab:** {survey.get('tab_squad_powers', '*not set*')}",
        f"**History Tab:** {survey.get('tab_history', '*not set*')}",
        f"**Questions:** {len(survey.get('questions') or [])}",
        f"**Intro Message:** {_yn(survey.get('intro_message'))}",
        f"**Translation Helper:** {translate_bot}",
    ]
    embed.add_field(name="📋 Survey", value="\n".join(s_lines)[:1024], inline=False)

    g_lines = [f"**Enabled:** {_enabled(growth.get('enabled'))}"]
    if growth.get("enabled"):
        metrics = growth.get("metrics") or []
        freq = growth.get("snapshot_frequency", "monthly")
        sched = (
            f"Monthly on day {growth.get('snapshot_day', 1)}"
            if freq == "monthly"
            else f"Every {growth.get('snapshot_interval', 30)} days"
        )
        g_lines += [
            f"**Source Tab:** {growth.get('tab_source', '*not set*')}",
            f"**Name Column:** {growth.get('name_col', '*not set*')}",
            f"**Data Start Row:** {growth.get('data_start_row', '*not set*')}",
            f"**Growth Tab:** {growth.get('tab_growth', '*not set*')}",
            f"**Snapshot Schedule:** {sched}",
            f"**Metrics ({len(metrics)}):** "
            + (
                ", ".join(f"{m['label']} (col {m['col']})" for m in metrics)
                if metrics
                else "*none*"
            ),
        ]
    embed.add_field(name=HUB_BTN_GROWTH, value="\n".join(g_lines)[:1024], inline=False)

    st_lines = [f"**Enabled:** {_enabled(shiny.get('enabled'))}"]
    if shiny.get("enabled"):
        st_lines += [
            f"**Channel:** {_channel(shiny.get('channel_id'))}",
            f"**Post Time:** {_format_time_with_tz(shiny.get('post_time'), cfg.timezone) or '*not set*'}",
            f"**Server Range:** "
            f"{shiny.get('server_min') or '?'} – {shiny.get('server_max') or '?'}",
            f"**Custom Message:** {_yn(shiny.get('message_template'))}",
        ]
    embed.add_field(name="🌟 Shiny Tasks", value="\n".join(st_lines)[:1024], inline=False)

    # Explain the ⚠️ once rather than per line, and say what it costs them.
    # A channel the bot can't reach is a feature that has silently stopped,
    # which is the whole point of #379.
    if unreachable_channels:
        count = len(unreachable_channels)
        embed.color = discord.Color.red()
        embed.add_field(
            name="⚠️ Channels I can't post in",
            value=(
                f"{count} channel{'s' if count != 1 else ''} above {'are' if count != 1 else 'is'} "
                f"marked ⚠️: either the channel was deleted, or my role lost **View Channel** or "
                f"**Send Messages** there. Anything scheduled to post {'there' if count == 1 else 'in them'} "
                f"is not running. Fix the channel permissions, or re-pick the channel in the "
                f"matching section above."
            )[:1024],
            inline=False,
        )

    if is_premium_flag:
        embed.set_footer(
            text="💎 Premium is active. Run /setup and click a section button to update it."
        )
    else:
        embed.set_footer(
            text="Run /upgrade for Premium • /help for all commands • /setup to update a section"
        )
    await interaction.followup.send(embed=embed, ephemeral=True)


# ── Run Various Setups ───────────────────────────────────────────────────────


async def run_setup(interaction: discord.Interaction, bot):
    import wizard_registry

    guild_id = interaction.guild_id
    cfg = get_or_create_config(guild_id)
    channel = interaction.channel
    user = interaction.user
    cancel_event = wizard_registry.register(user.id)

    # ── If already configured, show summary and offer edit or cancel ──────────
    if cfg.setup_complete:
        tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
        sheet_display = f"`{cfg.spreadsheet_id[:20]}...`" if cfg.spreadsheet_id else "Not set"
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="⚙️ Current Core Setup",
            description="Your server is already configured. Would you like to edit these settings?",
            fields=[
                ("Member Role", cfg.member_role_name),
                ("Leadership Role", cfg.leadership_role_name),
                ("Leadership Channel", f"<#{cfg.leadership_channel_id}>"),
                ("Timezone", tz_label),
                ("Sheet ID", sheet_display),
            ],
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Your existing setup is still active.",
        )
        if proceed is not True:
            return

    await channel.send(
        "⚙️ **Alliance Helper Setup**\n\n"
        "I'll walk you through the core configuration for your server. "
        "This covers your roles, leadership channel, timezone and Google Sheet.\n\n"
        "*You can run `/setup` again at any time to update these settings.*"
    )

    # ── Step 1: Member role ────────────────────────────────────────────────────
    await channel.send(
        "**Step 1 of 6 — Member Role**\nSelect the role that all alliance members have:"
    )
    v = RoleSelectStep(
        "Select member role...",
        current_id=cfg.member_role_id,
        current_name=cfg.member_role_name,
        guild=interaction.guild,
    )
    if v.is_current_stale:
        await channel.send(
            f"⚠️ Your previously configured member role **{cfg.member_role_name}** "
            "no longer exists. Pick a new one below."
        )
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    cfg.member_role_name = v.selected_role.name
    cfg.member_role_id = v.selected_role.id

    # ── Step 2: Leadership role ────────────────────────────────────────────────
    await channel.send(
        "**Step 2 of 6 — Leadership Role**\nSelect the elevated role for alliance leadership:"
    )
    v = RoleSelectStep(
        "Select leadership role...",
        current_id=cfg.leadership_role_id,
        current_name=cfg.leadership_role_name,
        guild=interaction.guild,
    )
    if v.is_current_stale:
        await channel.send(
            f"⚠️ Your previously configured leadership role **{cfg.leadership_role_name}** "
            "no longer exists. Pick a new one below."
        )
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    cfg.leadership_role_name = v.selected_role.name
    cfg.leadership_role_id = v.selected_role.id

    # ── Step 3: Leadership channel ─────────────────────────────────────────────
    is_premium_flag = await premium.is_premium(
        guild_id, interaction=interaction, bot=interaction.client
    )
    await channel.send(
        "**Step 3 of 6 — Leadership Channel**\n"
        "Pick the channel where I should post drafts, reminders, and approvals "
        "by default. Individual features (events, train reminders, etc.) can "
        "override this with their own channel later."
    )
    v = ChannelSelectStep(
        "Select leadership channel...",
        suggested_name="leadership",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=cfg.leadership_channel_id,
    )
    if v.is_current_stale:
        await channel.send(PREV_CHANNEL_GONE.format(channel_label="leadership"))
    await channel.send("\u200b", view=v)
    await wait_view_or_cancel(v, cancel_event)
    if v.cancelled:
        return
    if not v.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    cfg.leadership_channel_id = v.selected_channel.id

    # ── Step 4: Timezone ───────────────────────────────────────────────────────
    tz_view = TimezoneSelectView(current=cfg.timezone)
    await channel.send(
        "**Step 4 of 6 — Timezone**\n"
        "Select your alliance's timezone. This is used for displaying event times, "
        "Desert Storm/Canyon Storm times, and train reminders throughout the bot:"
    )
    await channel.send("\u200b", view=tz_view)
    await wait_view_or_cancel(tz_view, cancel_event)
    if tz_view.cancelled:
        return
    if not tz_view.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    cfg.timezone = tz_view.selected

    # ── Step 5: Google Sheet ID ────────────────────────────────────────────────
    await channel.send(
        "**Step 5 of 6 — Google Sheet ID**\n"
        "Enter your Google Sheet ID — the long string from your sheet's URL:\n"
        "`https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID`**`/edit`\n"
        "*(You can paste the whole URL and I'll pull the ID out.)*"
    )
    modal = TextInputModal("Google Sheet ID", "Sheet ID", placeholder="Paste your Sheet ID here...")
    # Truncate long sheet ids for the Keep-current button label —
    # full ids are ~44 chars and overflow Discord's 80-char cap once
    # the "Keep current: " prefix is added.
    sheet_display = (
        f"{cfg.spreadsheet_id[:25]}…"
        if cfg.spreadsheet_id and len(cfg.spreadsheet_id) > 25
        else cfg.spreadsheet_id
    )
    modal_v = ModalLaunchView(
        modal,
        current_value=cfg.spreadsheet_id or None,
        current_display=sheet_display or None,
    )
    await channel.send("\u200b", view=modal_v)
    await wait_view_or_cancel(modal_v, cancel_event)
    if modal_v.cancelled:
        return
    if not modal_v.confirmed:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd="setup"))
        return
    sheet_id = normalize_spreadsheet_id(modal.value)

    # ── Step 6: Share sheet ────────────────────────────────────────────────────
    SERVICE_ACCOUNT_EMAIL = "sheet-connector@lw-alliance-helper.iam.gserviceaccount.com"
    sharing_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit#sharing"

    share_embed = discord.Embed(
        title="**Step 6 of 6 — Share Your Google Sheet**",
        description=(
            "Before finishing, you need to give the bot access to your sheet.\n\n"
            "**Follow these steps:**\n"
            "1️⃣ Click the link below to open your sheet's sharing settings\n"
            "2️⃣ Click **Share** in the top right corner\n"
            "3️⃣ Paste the email address below into the share field\n"
            "4️⃣ Set permission to **Editor**\n"
            "5️⃣ Click **Send** — then come back here and confirm"
        ),
        color=discord.Color.yellow(),
    )
    share_embed.add_field(
        name="📋 Service Account Email (click to copy)",
        value=f"`{SERVICE_ACCOUNT_EMAIL}`",
        inline=False,
    )
    share_embed.add_field(
        name="🔗 Open Your Sheet",
        value=f"[Click here to open sharing settings]({sharing_url})",
        inline=False,
    )
    done_view = ConfirmView()
    done_view.children[0].label = "✅ I've shared the sheet"
    done_view.children[1].label = "❌ Cancel setup"
    await channel.send(embed=share_embed, view=done_view)
    await wait_view_or_cancel(done_view, cancel_event)
    if done_view.cancelled:
        return
    if not done_view.confirmed:
        await channel.send(f"{CANCEL_PLAIN} Run `/setup` to start again.")
        return

    # ── Confirm and save ───────────────────────────────────────────────────────
    tz_label = TIMEZONE_LABELS.get(cfg.timezone, cfg.timezone)
    embed = discord.Embed(
        title="✅ Final Review — Confirm to Save",
        description=(
            "All steps complete. Review your selections below and click "
            "**Confirm** to save your configuration, or **Cancel** to start over.\n"
            "*(This is the final review, not an additional step.)*"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Member Role", value=cfg.member_role_name, inline=False)
    embed.add_field(name="Leadership Role", value=cfg.leadership_role_name, inline=False)
    embed.add_field(
        name="Leadership Channel", value=f"<#{cfg.leadership_channel_id}>", inline=False
    )
    embed.add_field(name="Timezone", value=tz_label, inline=False)
    embed.add_field(name="Sheet ID", value=f"`{sheet_id[:20]}...`", inline=False)

    confirm_view = ConfirmView()
    await channel.send(embed=embed, view=confirm_view)
    await wait_view_or_cancel(confirm_view, cancel_event)
    if confirm_view.cancelled:
        return
    if not confirm_view.confirmed:
        await channel.send(f"{CANCEL_PLAIN} Run `/setup` to start again.")
        return

    cfg.setup_complete = True
    cfg.spreadsheet_id = sheet_id
    save_config(cfg)

    await channel.send(
        "✅ **Core setup complete!**\n\n"
        "Now configure whichever features you want to use. Run `/setup` "
        "again to re-open the hub — every feature wizard lives behind a "
        "labeled button:\n\n"
        "📣 **Events** — Event announcements (Plague Marauder, Zombie Siege, etc.)\n"
        "🚂 **Train** — Train schedule, blurb generation, and reminders\n"
        "🎂 **Birthdays** — Birthday tracking and announcements\n"
        "⚔️ **Desert Storm** — Mail drafts and participation logs\n"
        "🛡️ **Canyon Storm** — Mail drafts and participation logs\n"
        "📋 **Survey** — Squad powers survey\n"
        "📈 **Growth** — Growth tracking (snapshot your members' stats over time)\n"
        "🌟 **Shiny Tasks** — Daily announcement of today's shiny task servers for your Alliance\n\n"
        "Premium features (👥 Member Sync, 📋 Survey, 📊 Growth Breakdown) show as 💎-locked "
        "until you upgrade. Use `/help` any time to see every command."
    )
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Guild {guild_id} core setup complete")


async def run_growth_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring growth tracking."""
    import wizard_registry

    guild_id = interaction.guild_id
    channel = interaction.channel
    user = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=WIZARD_STEP_TIMEOUT),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send(CANCEL_PLAIN)
            else:
                await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
            return None
        return reply.content.strip()[:max_chars]

    from config import (
        get_growth_config,
        save_growth_config,
        has_growth_config,
        clear_growth_config,
    )

    current = get_growth_config(guild_id)
    growth_already_configured = has_growth_config(guild_id)

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

    # ── If already enabled, show summary and offer edit or cancel ─────────────
    if growth_already_configured and current.get("enabled"):
        metrics_list = current.get("metrics") or []
        freq = current.get("snapshot_frequency", "monthly")
        if freq == "monthly":
            sched = f"Monthly on day {current.get('snapshot_day', 1)}"
        else:
            sched = f"Every {current.get('snapshot_interval', 30)} days"
        fields = [
            ("Source Tab", current.get("tab_source") or "*not set*"),
            ("Name Column", f"Column {current.get('name_col') or '*not set*'}"),
            ("Data Start Row", str(current.get("data_start_row") or "*not set*")),
            ("Growth Tab", current.get("tab_growth") or "*not set*"),
            ("Snapshot Schedule", sched),
            (
                f"Metrics ({len(metrics_list)})",
                "\n".join(f"• {m['label']} — column {m['col']}" for m in metrics_list)
                if metrics_list
                else "*none*",
            ),
        ]
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="📈 Current Growth Setup",
            description="Growth tracking is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Growth tracking is still active.",
        )
        if proceed is not True:
            return

    await channel.send(
        "⚙️ **Growth Tracking Setup**\n"
        "Configure how the bot tracks your alliance's growth over time. "
        "Each month (or on your chosen schedule), the bot takes a snapshot of your members' stats "
        "and records them in your Google Sheet so you can track progress."
    )

    # ── Step 1: Enable? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        "**Step 1 of 7 — Enable growth tracking?**\n"
        "Should the bot automatically take snapshots of your members' stats on a schedule?",
        view=enabled_view,
    )
    await wait_view_or_cancel(enabled_view, cancel_event)
    if enabled_view.cancelled:
        return
    if enabled_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
        return
    if not enabled_view.selected:
        save_growth_config(
            guild_id,
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
        await ask_disable_with_clear(
            channel,
            feature_label="Growth tracking",
            setup_command=f"setup → {HUB_BTN_GROWTH}",
            had_prior_config=growth_already_configured,
            clear_fn=lambda: clear_growth_config(guild_id),
            cancel_event=cancel_event,
        )
        return

    # ── Step 2: Source tab ────────────────────────────────────────────────────
    tab_source = await ask_keep_or_change(
        channel,
        "**Step 2 of 7 — Source Tab**\n"
        "Which tab in your Google Sheet contains your member data?\n"
        "⚠️ *Make sure this tab exists in your sheet.*",
        default=DEFAULT_TAB_SOURCE,
        current=current.get("tab_source", ""),
        modal_title="Source Tab",
        modal_label="Tab name",
        timeout_cmd="setup_growth",
        cancel_event=cancel_event,
    )
    if tab_source is None:
        return
    await warn_if_tab_claimed(channel, guild_id, tab_source, exclude_field="tab_source")

    # ── Step 3: Data start row ────────────────────────────────────────────────
    start_raw = await ask_keep_or_change(
        channel,
        "**Step 3 of 7 — Data Start Row**\n"
        "Which row does your member data start on? (Row 1 is usually the header)",
        default=str(DEFAULT_DATA_START_ROW),
        current=str(current.get("data_start_row") or ""),
        modal_title="Data Start Row",
        modal_label="Row number",
        timeout_cmd="setup_growth",
        cancel_event=cancel_event,
    )
    if start_raw is None:
        return
    try:
        data_start_row = int(str(start_raw).strip())
    except ValueError:
        await channel.send(
            INPUT_INVALID.format(
                type="row number",
                example="2",
                recovery=f"`/setup` → {HUB_BTN_GROWTH}",
            )
        )
        return

    # ── Step 4: Name column ───────────────────────────────────────────────────
    name_raw = await ask_keep_or_change(
        channel,
        "**Step 4 of 7 — Name Column**\nWhich column contains the member's name?",
        default=DEFAULT_NAME_COL,
        current=current.get("name_col", ""),
        modal_title="Name Column",
        modal_label="Column letter",
        timeout_cmd="setup_growth",
        cancel_event=cancel_event,
    )
    if name_raw is None:
        return
    name_col = name_raw.strip().upper()
    if len(name_col) != 1 or not name_col.isalpha():
        await channel.send(
            INPUT_INVALID.format(
                type="single column letter",
                example="A",
                recovery=f"`/setup` → {HUB_BTN_GROWTH}",
            )
        )
        return

    # ── Step 5: Metrics ───────────────────────────────────────────────────────
    metrics = list(current.get("metrics", []))

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

        async def on_submit(self, interaction: discord.Interaction):
            self.label_value = self._label_input.value.strip()
            self.col_value = self._col_input.value.strip().upper()
            await interaction.response.defer()
            self.stop()

    def _metrics_embed(cap: int | None = None) -> discord.Embed:
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
            embed.add_field(
                name="No metrics yet", value="Click **Add Metric** to begin.", inline=False
            )
        if cap is not None:
            embed.set_footer(
                text=TIER_COMPARISON.format(
                    free_limit=f"{len(metrics)} of {cap} metrics used",
                    premium_limit="unlimited",
                )
            )
        return embed

    while True:
        # Free-tier cap on number of growth metrics
        metrics_cap = await premium.get_limit(
            "growth_metrics", guild_id, interaction=interaction, bot=interaction.client
        )
        at_metrics_cap = metrics_cap is not None and len(metrics) >= metrics_cap

        class MetricsActionView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_STEP_TIMEOUT)
                self.choice = None
                if not metrics:
                    self.edit_btn.disabled = True
                    self.delete_btn.disabled = True
                    self.done_btn.disabled = True
                if at_metrics_cap:
                    self.add_btn.disabled = True

            @discord.ui.button(label="➕ Add Metric", style=discord.ButtonStyle.success, row=0)
            async def add_btn(self, inter: discord.Interaction, button: discord.ui.Button):
                modal = MetricModal()
                await inter.response.send_modal(modal)
                await modal.wait()
                if modal.label_value and modal.col_value and modal.col_value.isalpha():
                    metrics.append({"label": modal.label_value, "col": modal.col_value})
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

        action_view = MetricsActionView()
        await channel.send(embed=_metrics_embed(cap=metrics_cap), view=action_view)
        await wait_view_or_cancel(action_view, cancel_event)
        if action_view.cancelled:
            return

        if action_view.choice is None:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
            return
        if action_view.choice == "done":
            break
        if action_view.choice == "loop":
            continue

        if action_view.choice in ("edit", "delete") and not metrics:
            continue

        # Pick which metric to edit/delete
        class PickMetricView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_STEP_TIMEOUT)
                self.index = None
                options = [
                    discord.SelectOption(
                        label=m["label"][:100],
                        value=str(i),
                        description=f"Column {m['col']}",
                    )
                    for i, m in enumerate(metrics)
                ]
                self.select = discord.ui.Select(
                    placeholder="Choose a metric...",
                    options=options,
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

        pick_view = PickMetricView()
        verb = "edit" if action_view.choice == "edit" else "delete"
        await channel.send(f"Which metric do you want to {verb}?", view=pick_view)
        await wait_view_or_cancel(pick_view, cancel_event)
        if pick_view.cancelled:
            return
        if pick_view.index is None:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
            return

        if action_view.choice == "delete":
            removed = metrics.pop(pick_view.index)
            await channel.send(f"🗑️ Removed **{removed['label']}** (column {removed['col']}).")
            continue

        # Edit: open a modal pre-filled with the chosen metric
        existing = metrics[pick_view.index]

        class EditLaunchView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_STEP_TIMEOUT)
                self.modal = MetricModal(
                    label_default=existing["label"], col_default=existing["col"]
                )
                self.confirmed = False

            @discord.ui.button(label="✏️ Edit values", style=discord.ButtonStyle.primary)
            async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
                await inter.response.send_modal(self.modal)
                await self.modal.wait()
                self.confirmed = True
                self.stop()

        edit_launch = EditLaunchView()
        await channel.send(
            f"Editing **{existing['label']}** (column {existing['col']}). Click below to update.",
            view=edit_launch,
        )
        await wait_view_or_cancel(edit_launch, cancel_event)
        if edit_launch.cancelled:
            return
        if (
            edit_launch.modal.label_value
            and edit_launch.modal.col_value
            and edit_launch.modal.col_value.isalpha()
        ):
            metrics[pick_view.index] = {
                "label": edit_launch.modal.label_value,
                "col": edit_launch.modal.col_value,
            }

    if not metrics:
        await channel.send(f"⚠️ No metrics defined. Run `/setup` → {HUB_BTN_GROWTH} to try again.")
        return

    # ── Step 6: Growth tracking tab ───────────────────────────────────────────
    tab_growth = await ask_keep_or_change(
        channel,
        "**Step 6 of 7 — Growth Tracking Tab**\n"
        "Which tab should snapshots be written to?\n"
        "⚠️ *If the tab doesn't exist, the bot will create it automatically.*",
        default=DEFAULT_TAB_GROWTH,
        current=current.get("tab_growth", ""),
        modal_title="Growth Tracking Tab",
        modal_label="Tab name",
        timeout_cmd="setup_growth",
        cancel_event=cancel_event,
    )
    if tab_growth is None:
        return
    await warn_if_tab_claimed(channel, guild_id, tab_growth, exclude_field="tab_growth")

    # ── Step 7: Snapshot frequency ────────────────────────────────────────────
    # Custom-interval frequency is a premium-only feature.
    custom_interval_unlocked = await premium.is_premium(
        guild_id, interaction=interaction, bot=interaction.client
    )

    class FrequencyView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_STEP_TIMEOUT)
            self.selected = None
            if not custom_interval_unlocked:
                self.custom.disabled = True

        @discord.ui.button(
            label="📅 Monthly (1st of each month)", style=discord.ButtonStyle.primary
        )
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

    freq_view = FrequencyView()
    freq_prompt = "**Step 7 of 7 — Snapshot Frequency**\nHow often should the bot take a snapshot?"
    if not custom_interval_unlocked:
        freq_prompt += "\n*🔒 Custom interval is a Premium feature.*"
    await channel.send(
        freq_prompt,
        view=freq_view,
    )
    await wait_view_or_cancel(freq_view, cancel_event)
    if freq_view.cancelled:
        return
    if not freq_view.selected:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_GROWTH))
        return

    snapshot_frequency = freq_view.selected
    snapshot_day = DEFAULT_SNAPSHOT_DAY
    snapshot_interval = DEFAULT_SNAPSHOT_INTERVAL

    if snapshot_frequency == "monthly":
        day_raw = await ask_keep_or_change(
            channel,
            "**Step 7a of 7 — Snapshot Day**\n"
            "Which day of the month should the snapshot run? (1–28)",
            default=str(DEFAULT_SNAPSHOT_DAY),
            current=str(current.get("snapshot_day") or ""),
            modal_title="Snapshot Day",
            modal_label="Day of month (1–28)",
            timeout_cmd="setup_growth",
            cancel_event=cancel_event,
        )
        if day_raw is None:
            return
        try:
            snapshot_day = max(1, min(28, int(str(day_raw).strip())))
        except ValueError:
            snapshot_day = DEFAULT_SNAPSHOT_DAY
    else:
        interval_raw = await ask_keep_or_change(
            channel,
            "**Step 7a of 7 — Interval (days)**\nHow many days between each snapshot?",
            default=str(DEFAULT_SNAPSHOT_INTERVAL),
            current=str(current.get("snapshot_interval") or ""),
            modal_title="Interval",
            modal_label="Days between snapshots",
            timeout_cmd="setup_growth",
            cancel_event=cancel_event,
        )
        if interval_raw is None:
            return
        try:
            snapshot_interval = max(1, int(str(interval_raw).strip()))
        except ValueError:
            snapshot_interval = DEFAULT_SNAPSHOT_INTERVAL

    # ── Save ───────────────────────────────────────────────────────────────────
    save_growth_config(
        guild_id,
        enabled=1,
        tab_source=tab_source,
        name_col=name_col,
        metrics=metrics,
        tab_growth=tab_growth,
        snapshot_frequency=snapshot_frequency,
        snapshot_day=snapshot_day,
        snapshot_interval=snapshot_interval,
        data_start_row=data_start_row,
    )

    freq_desc = (
        f"Monthly on day {snapshot_day}"
        if snapshot_frequency == "monthly"
        else f"Every {snapshot_interval} days"
    )
    metrics_display = "\n".join(f"• **{m['label']}** — column {m['col']}" for m in metrics)

    # Compute when the very first snapshot will fire under this config so
    # the user isn't left guessing "OK now what?" when they pick a custom
    # interval — picking 14 days doesn't tell them whether the first
    # snapshot is today, tomorrow, or 14 days from now.
    from growth import compute_next_snapshot

    next_dt = compute_next_snapshot(
        {
            "enabled": 1,
            "snapshot_frequency": snapshot_frequency,
            "snapshot_day": snapshot_day,
            "snapshot_interval": snapshot_interval,
        }
    )
    if next_dt is not None:
        ts = int(next_dt.timestamp())
        # Discord renders <t:N:F> as a localized full date/time per viewer
        # and <t:N:R> as a relative "in 3 days" string — the combo gives
        # leadership a clear answer regardless of their personal timezone.
        next_value = (
            f"<t:{ts}:F> (<t:{ts}:R>)\n"
            f"*Want to start tracking from today instead? "
            f"Run `/growth overview` and click **📸 Run Snapshot Now**.*"
        )
    else:
        next_value = "*Could not compute — check `/growth overview` for status.*"

    embed = discord.Embed(title="✅ Growth Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Source Tab", value=tab_source, inline=False)
    embed.add_field(name="Name Column", value=f"Column {name_col}", inline=False)
    embed.add_field(name="Data Start Row", value=str(data_start_row), inline=False)
    embed.add_field(name="Growth Tab", value=tab_growth, inline=False)
    embed.add_field(name="Snapshot Schedule", value=freq_desc, inline=False)
    embed.add_field(name="Next Snapshot", value=next_value, inline=False)
    embed.add_field(name="Metrics", value=metrics_display, inline=False)
    embed.set_footer(
        text=SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_GROWTH)
        + " Use /growth overview to take a manual snapshot.",
    )
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Growth config saved for guild {guild_id}")


async def run_growth_breakdown_setup(interaction: discord.Interaction, bot):
    """Premium-only wizard for the Growth Breakdown auto-post + customization.

    The bucket-classification math itself ships free (`/growth breakdown`,
    and the **📊 See most recent Breakdown** button on `/growth overview`, both read the breakdown tab for any guild that's
    enabled growth tracking). This wizard configures the Premium layer:

      * sheet tab name for the breakdown
      * auto-post channel (fires after every snapshot, premium-gated at
        post time so a subscription lapse stops the alerts without any
        config change)
      * bucket filter (which buckets fire the auto-post — e.g. only
        Decline + None)
      * custom thresholds applied to every metric (global, not per-metric
        — per-metric customization is parked as a follow-up if alliances
        ask for it)
      * custom labels for each bucket
    """
    import wizard_registry
    from config import (
        get_growth_config,
        save_growth_breakdown_config,
        has_growth_breakdown_config,
    )
    from growth import DEFAULT_THRESHOLDS, DEFAULT_BUCKET_LABELS, BUCKET_ORDER

    guild_id = interaction.guild_id
    channel = interaction.channel
    user = interaction.user
    cancel_event = wizard_registry.register(user.id)

    current = get_growth_config(guild_id)
    if not current.get("enabled") or not current.get("metrics"):
        await channel.send(
            f"⚙️ Set up growth tracking first — run `/setup` → {HUB_BTN_GROWTH} and add at "
            f"least one metric, then come back to `/setup` → {HUB_BTN_BREAKDOWN} to "
            "configure the breakdown layer."
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    # ── If already configured, show summary and offer edit or cancel ─────────
    if has_growth_breakdown_config(guild_id):
        post_ch = current.get("breakdown_post_channel_id") or 0
        thresholds = current.get("breakdown_thresholds") or {}
        labels = current.get("breakdown_labels") or {}
        bucket_filter = current.get("breakdown_bucket_filter") or []
        fields = [
            ("Breakdown Tab", current.get("tab_breakdown") or "Growth Breakdown"),
            ("Auto-Post Channel", f"<#{post_ch}>" if post_ch else "❌ Off"),
        ]
        if post_ch and bucket_filter:
            fields.append(
                (
                    "Bucket Filter",
                    ", ".join(DEFAULT_BUCKET_LABELS.get(b, b) for b in bucket_filter),
                )
            )
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
            fields.append(
                (
                    "Custom Labels",
                    ", ".join(
                        f"{DEFAULT_BUCKET_LABELS[b]}→{labels[b]}"
                        for b in BUCKET_ORDER
                        if labels.get(b)
                    )
                    or "—",
                )
            )
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="📊 Current Growth Breakdown Setup",
            description="Growth breakdown is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Your breakdown setup is still active.",
        )
        if proceed is not True:
            return

    await channel.send(
        "📊 **Growth Breakdown Setup** (💎 Premium)\n"
        "Classifies each member's growth between snapshots into one of "
        "five buckets and (optionally) posts the summary to a channel "
        "after every snapshot."
    )

    # ── Step 1: Breakdown tab ─────────────────────────────────────────────
    tab_breakdown = await ask_keep_or_change(
        channel,
        "**Step 1 of 5 — Breakdown Tab**\n"
        "Which tab in your Google Sheet should the breakdown data live in? "
        "The bot creates it automatically if it doesn't exist yet.",
        default="Growth Breakdown",
        current=current.get("tab_breakdown") or "",
        modal_title="Breakdown Tab",
        modal_label="Tab name",
        timeout_cmd="setup_growth_breakdown",
        cancel_event=cancel_event,
    )
    if tab_breakdown is None:
        return
    await warn_if_tab_claimed(channel, guild_id, tab_breakdown, exclude_field="tab_breakdown")

    # ── Step 2: Auto-post toggle + channel ────────────────────────────────
    autopost_view = YesNoView()
    await channel.send(
        "**Step 2 of 5 — Auto-Post After Snapshots?**\n"
        "Each time the bot finishes a snapshot, post the breakdown summary "
        "to a channel so leadership doesn't have to run `/growth breakdown` to see "
        "who's slowing down.",
        view=autopost_view,
    )
    await wait_view_or_cancel(autopost_view, cancel_event)
    if autopost_view.cancelled:
        return
    if autopost_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN))
        return

    post_channel_id = 0
    if autopost_view.selected:
        saved_post_ch = current.get("breakdown_post_channel_id") or 0
        post_ch_view = ChannelSelectStep(
            "Select the auto-post channel…",
            suggested_name="growth-breakdown",
            include_threads=True,
            guild=interaction.guild,
            current_id=saved_post_ch,
        )
        if post_ch_view.is_current_stale:
            await channel.send(PREV_CHANNEL_GONE.format(channel_label="breakdown"))
        await channel.send(
            "**Auto-Post Channel**\nWhere should the breakdown summaries land?",
            view=post_ch_view,
        )
        await wait_view_or_cancel(post_ch_view, cancel_event)
        if post_ch_view.cancelled:
            return
        if not post_ch_view.confirmed:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN))
            return
        post_channel_id = post_ch_view.selected_channel.id

    # ── Step 3: Bucket filter ─────────────────────────────────────────────
    # Surfaces only when auto-post is on — bucket filter doesn't apply to
    # the on-demand /growth button (which always shows every bucket).
    bucket_filter: list[str] = []
    if post_channel_id:
        saved_filter = current.get("breakdown_bucket_filter") or []

        class BucketFilterView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=WIZARD_STEP_TIMEOUT)
                self.selected: list[str] | None = None

                # Keep-current button on its own row when leadership
                # previously picked a filter. Sets `self.selected` to
                # the saved list and stops — no Select interaction
                # needed.
                if saved_filter:
                    saved_disp_short = ", ".join(
                        DEFAULT_BUCKET_LABELS.get(b, b) for b in saved_filter
                    )
                    keep_btn = discord.ui.Button(
                        label=f"Keep current: {saved_disp_short}"[:80],
                        style=discord.ButtonStyle.success,
                        row=0,
                    )

                    async def _keep_cb(inter: discord.Interaction):
                        self.selected = list(saved_filter)
                        for item in self.children:
                            item.disabled = True
                        await wizard_registry.safe_edit_response(inter, view=self)
                        self.stop()

                    keep_btn.callback = _keep_cb
                    self.add_item(keep_btn)

                opts = [
                    discord.SelectOption(
                        label=DEFAULT_BUCKET_LABELS[b],
                        value=b,
                        description=f"{b.title()} growth bucket",
                    )
                    for b in BUCKET_ORDER
                ]
                select = discord.ui.Select(
                    placeholder="Pick which buckets fire alerts (none = all)",
                    options=opts,
                    min_values=0,
                    max_values=len(BUCKET_ORDER),
                    row=1 if saved_filter else 0,
                )

                async def _select_cb(inter: discord.Interaction):
                    self.selected = list(select.values)
                    for item in self.children:
                        item.disabled = True
                    await wizard_registry.safe_edit_response(inter, view=self)
                    self.stop()

                select.callback = _select_cb
                self.add_item(select)

                all_btn = discord.ui.Button(
                    label="Use all buckets",
                    style=discord.ButtonStyle.secondary,
                    row=2 if saved_filter else 1,
                )

                async def _all_cb(inter: discord.Interaction):
                    self.selected = []
                    for item in self.children:
                        item.disabled = True
                    await wizard_registry.safe_edit_response(inter, view=self)
                    self.stop()

                all_btn.callback = _all_cb
                self.add_item(all_btn)

        bf_view = BucketFilterView()
        if saved_filter:
            saved_disp = ", ".join(DEFAULT_BUCKET_LABELS.get(b, b) for b in saved_filter)
            await channel.send(
                f"**Step 3 of 5 — Bucket Filter**\n"
                f"Currently alerting on: **{saved_disp}**. Pick a new set of "
                f"buckets, or hit **Use all buckets** to alert on every bucket.",
                view=bf_view,
            )
        else:
            await channel.send(
                "**Step 3 of 5 — Bucket Filter**\n"
                "Pick which buckets fire the auto-post — leave empty (or hit "
                "**Use all buckets**) to alert on every bucket.",
                view=bf_view,
            )
        await wait_view_or_cancel(bf_view, cancel_event)
        if bf_view.cancelled:
            return
        if bf_view.selected is None:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN))
            return
        bucket_filter = bf_view.selected

    # ── Step 4: Custom thresholds ─────────────────────────────────────────
    thresholds = dict(current.get("breakdown_thresholds") or {})

    class ThresholdsModal(discord.ui.Modal):
        def __init__(self):
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

    class ThresholdsChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_STEP_TIMEOUT)
            self.choice = None

            # Keep current renders first (leftmost), per the
            # Keep-current-is-always-first convention. It only applies
            # when leadership saved custom thresholds on a previous run —
            # drop it on first run, and otherwise demote "Use defaults"
            # to a secondary revert so Keep current is the primary action.
            if thresholds:
                self.defaults_btn.label = "↩️ Use defaults"
                self.defaults_btn.style = discord.ButtonStyle.secondary
            else:
                self.remove_item(self.keep_btn)

        @discord.ui.button(label="Keep current values", style=discord.ButtonStyle.success)
        async def keep_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "keep"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✅ Use defaults", style=discord.ButtonStyle.success)
        async def defaults_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "defaults"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✏️ Customize", style=discord.ButtonStyle.primary)
        async def customize_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            modal = ThresholdsModal()
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

    t_view = ThresholdsChoiceView()
    t_view._modal_values = None
    await channel.send(
        "**Step 4 of 5 — Bucket Thresholds**\n"
        f"Defaults: Increased ≥ {DEFAULT_THRESHOLDS['increased']:.0f}%, "
        f"Steady ≥ {DEFAULT_THRESHOLDS['steady']:.0f}%, "
        f"Low ≥ {DEFAULT_THRESHOLDS['low']:.0f}%, "
        f"None ≥ {DEFAULT_THRESHOLDS['none']:.0f}%, "
        f"Decline < 0%.\n"
        "Customize for stricter (or looser) growth standards — applies to "
        "every metric. Per-metric thresholds are tracked as a follow-up.",
        view=t_view,
    )
    await wait_view_or_cancel(t_view, cancel_event)
    if t_view.cancelled:
        return
    if t_view.choice is None:
        await channel.send(
            f"⏰ Timed out or invalid thresholds. Run `/setup` → {HUB_BTN_BREAKDOWN} to start again."
        )
        return
    if t_view.choice == "defaults":
        thresholds = {}
    elif t_view.choice == "keep":
        # Already loaded from current at the top of this step — no
        # action needed; the wizard saves what's there.
        pass
    else:
        thresholds = t_view._modal_values

    # ── Step 5: Custom labels ─────────────────────────────────────────────
    labels = dict(current.get("breakdown_labels") or {})

    class LabelsModal(discord.ui.Modal):
        def __init__(self):
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

    class LabelsChoiceView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=WIZARD_STEP_TIMEOUT)
            self.choice = None

            # Mirror ThresholdsChoiceView: Keep current renders first
            # (leftmost). It only applies when leadership saved custom
            # bucket labels on a previous run — drop it on first run, and
            # otherwise demote Use defaults to a secondary revert so Keep
            # current is the primary action.
            if labels:
                self.defaults_btn.label = "↩️ Use defaults"
                self.defaults_btn.style = discord.ButtonStyle.secondary
            else:
                self.remove_item(self.keep_btn)

        @discord.ui.button(label="Keep current labels", style=discord.ButtonStyle.success)
        async def keep_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "keep"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✅ Use defaults", style=discord.ButtonStyle.success)
        async def defaults_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            self.choice = "defaults"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        @discord.ui.button(label="✏️ Customize", style=discord.ButtonStyle.primary)
        async def customize_btn(self, inter: discord.Interaction, button: discord.ui.Button):
            modal = LabelsModal()
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

    l_view = LabelsChoiceView()
    l_view._modal_values = None
    await channel.send(
        "**Step 5 of 5 — Bucket Labels**\n"
        f"Defaults: {', '.join(DEFAULT_BUCKET_LABELS[b] for b in BUCKET_ORDER)}.\n"
        "Rename buckets to match your alliance's voice (e.g. 'Crushing It', "
        "'Stalled', 'Going Backwards').",
        view=l_view,
    )
    await wait_view_or_cancel(l_view, cancel_event)
    if l_view.cancelled:
        return
    if l_view.choice is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BREAKDOWN))
        return
    if l_view.choice == "defaults":
        labels = {}
    elif l_view.choice == "keep":
        # Already loaded from current at the top of this step.
        pass
    else:
        labels = l_view._modal_values

    # ── Save ──────────────────────────────────────────────────────────────
    saved_ok = save_growth_breakdown_config(
        guild_id,
        tab_breakdown=tab_breakdown,
        breakdown_thresholds=thresholds,
        breakdown_labels=labels,
        breakdown_post_channel_id=post_channel_id,
        breakdown_bucket_filter=bucket_filter,
    )
    if not saved_ok:
        await channel.send(
            f"⚠️ Couldn't save the breakdown config — make sure `/setup` → {HUB_BTN_GROWTH} "
            "has been run for this server first."
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    embed = discord.Embed(title="✅ Growth Breakdown Configured", color=discord.Color.green())
    embed.add_field(name="Breakdown Tab", value=tab_breakdown, inline=False)
    if post_channel_id:
        bf_text = (
            ", ".join(DEFAULT_BUCKET_LABELS.get(b, b) for b in bucket_filter)
            if bucket_filter
            else "All buckets"
        )
        embed.add_field(name="Auto-Post Channel", value=f"<#{post_channel_id}>", inline=False)
        embed.add_field(name="Bucket Filter", value=bf_text, inline=False)
    else:
        embed.add_field(
            name="Auto-Post",
            value="❌ Off — use `/growth breakdown` (or `/growth overview` → 📊 See most recent Breakdown) to view on demand.",
            inline=False,
        )
    if thresholds:
        t_text = (
            f"Increased ≥ {thresholds['increased']:g}%, "
            f"Steady ≥ {thresholds['steady']:g}%, "
            f"Low ≥ {thresholds['low']:g}%, "
            f"None ≥ {thresholds['none']:g}%, Decline < 0%"
        )
        embed.add_field(name="Custom Thresholds", value=t_text, inline=False)
    else:
        embed.add_field(
            name="Thresholds",
            value="Defaults (Increased ≥ 20%, Steady ≥ 10%, Low ≥ 5%, None ≥ 0%, Decline < 0%)",
            inline=False,
        )
    if labels:
        l_text = ", ".join(
            f"{DEFAULT_BUCKET_LABELS[b]}→{labels[b]}" for b in BUCKET_ORDER if labels.get(b)
        )
        embed.add_field(name="Custom Labels", value=l_text or "—", inline=False)
    embed.set_footer(text=SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_BREAKDOWN))
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Growth Breakdown config saved for guild {guild_id}")


# ── Inline-create offers for the structured-flow setup wizard (#144) ─────────
#
# Each offer is a Yes/No view posted after the relevant tab name is saved.
# It only appears when the underlying table is empty — alliances running
# setup a second time see their saved values surface as defaults but don't
# get re-prompted to create the first row.


class _InlineCreatePresetOffer(OwnedView):
    """Posted after the Strategy Presets tab name is saved (and the
    alliance has zero presets). 'Create now' opens the same preset
    editor as `/<parent> strategy create`."""

    @property
    def timeout_hint(self) -> str:
        return f"`{HUB_COMMAND[self.event_type]}` → **{HUB_BTN_PRESETS}**"

    def __init__(
        self, *, owner_id: int, event_type: str, parent: str, default_name: str = "Standard"
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.event_type = event_type
        self.parent = parent
        self.default_name = default_name
        self.choice: str | None = None
        self.message: discord.Message | None = None

    @discord.ui.button(label="➕ Create my first preset now", style=discord.ButtonStyle.primary)
    async def create_btn(self, inter: discord.Interaction, _btn):
        self.choice = "create"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        try:
            from storm_strategy import seed_default_preset
            from storm_strategy_ui import open_editor_followup

            buf = seed_default_preset(self.default_name, self.event_type)
            buf.dirty = True
            await open_editor_followup(inter, self.event_type, buf)
        except Exception as e:
            await inter.followup.send(
                f"⚠️ Couldn't open the preset editor inline: {e}. "
                f"Run `{HUB_COMMAND[self.event_type]}` and click "
                f"**{HUB_BTN_PRESETS}** to retry.",
                ephemeral=True,
            )
        self.stop()

    @discord.ui.button(label="Skip for now", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, inter: discord.Interaction, _btn):
        self.choice = "skip"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


class _InlineCreateMemberRuleOffer(OwnedView):
    """Posted after the Member Rules tab name is saved (and the alliance
    has zero rules). 'Add one now' opens a streamlined modal for a
    power-band rule — the most-common rule type. Per-member rules (which
    need a Discord member picker) remain available via the slash commands."""

    @property
    def timeout_hint(self) -> str:
        return f"`{HUB_COMMAND[self.event_type]}` → **{HUB_BTN_RULES}**"

    def __init__(self, *, owner_id: int, event_type: str, parent: str):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.event_type = event_type
        self.parent = parent
        self.choice: str | None = None
        self.message: discord.Message | None = None

    @discord.ui.button(label="➕ Add a power-band rule now", style=discord.ButtonStyle.primary)
    async def create_btn(self, inter: discord.Interaction, _btn):
        self.choice = "create"
        for child in self.children:
            child.disabled = True
        # Disable the offer message via the bot-owned message handle so a
        # fast second click can't fire a duplicate picker. The picker
        # itself sends a fresh ephemeral with its own select + modal.
        try:
            from storm_member_rules import InlinePowerBandView

            picker = InlinePowerBandView(self.event_type, owner_id=inter.user.id)
            await inter.response.send_message(
                content=(
                    "Pick the zone the rule applies to, then click "
                    "**Set minimum power** to enter the threshold."
                ),
                view=picker,
                ephemeral=True,
            )
            picker.message = await inter.original_response()
        except Exception as e:
            await inter.response.send_message(
                f"⚠️ Couldn't open the rule picker: {e}. Run "
                f"`{HUB_COMMAND[self.event_type]}` and click "
                f"**{HUB_BTN_RULES}** to retry.",
                ephemeral=True,
            )
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()

    @discord.ui.button(label="Skip for now", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, inter: discord.Interaction, _btn):
        self.choice = "skip"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


class _InlinePostFirstSignupOffer(OwnedView):
    """Posted at the end of the storm setup wizard (DS / CS) when
    the structured flow is opted in, a sign-up channel is configured,
    and no sign-up post has been recorded yet. 'Post now' fires
    `post_registration` against the next configured event date."""

    @property
    def timeout_hint(self) -> str:
        return f"`{HUB_COMMAND[self.event_type]}` → **{HUB_BTN_POST_SIGNUP}**"

    def __init__(
        self, *, owner_id: int, bot, guild_id: int, event_type: str, parent: str, label: str
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.bot = bot
        self.guild_id = guild_id
        self.event_type = event_type
        self.parent = parent
        self.label = label
        self.choice: str | None = None
        self.message: discord.Message | None = None

    @discord.ui.button(label="📣 Post my first sign-up now", style=discord.ButtonStyle.primary)
    async def post_btn(self, inter: discord.Interaction, _btn):
        self.choice = "post"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        try:
            from storm_date_helpers import next_event_date
            from storm_signup_post import post_registration, _format_post_result_message
            from config import get_structured_storm_config

            target_date = next_event_date(self.guild_id, self.event_type)
            structured = get_structured_storm_config(self.guild_id, self.event_type)
            guild = self.bot.get_guild(self.guild_id)
            if guild is None:
                await inter.followup.send(
                    "⚠️ The bot can't see this guild right now. Try again "
                    f"via `{HUB_COMMAND[self.event_type]}` → "
                    f"**{HUB_BTN_POST_SIGNUP}**.",
                    ephemeral=True,
                )
                return
            result = await post_registration(
                self.bot,
                guild,
                self.event_type,
                target_date,
                structured=structured,
                # Leadership-triggered repost (#265): bypass the
                # once-per-event guard so the first-sign-up wizard
                # button can post even if an earlier post is still on
                # the channel.
                force=True,
            )
            await inter.followup.send(
                _format_post_result_message(self.event_type, target_date, result),
                ephemeral=True,
            )
        except Exception as e:
            await inter.followup.send(
                f"⚠️ Sign-up post failed: {e}. Run "
                f"`{HUB_COMMAND[self.event_type]}` and click "
                f"**{HUB_BTN_POST_SIGNUP}** to retry.",
                ephemeral=True,
            )
        self.stop()

    @discord.ui.button(label="Skip: I'll post later", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, inter: discord.Interaction, _btn):
        self.choice = "skip"
        for child in self.children:
            child.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()


# ── Structured storm flow setup sub-flow (#38 + #54) ─────────────────────────


def _col_letter_to_index(letter: str) -> int:
    """A→0, B→1, ..., AA→26. Returns -1 on invalid input."""
    s = (letter or "").strip().upper()
    if not s or not all(c.isalpha() for c in s):
        return -1
    n = 0
    for c in s:
        n = n * 26 + (ord(c) - ord("A") + 1)
    return n - 1


def _col_index_to_letter(idx: int) -> str:
    """0→A, 1→B, ..., 26→AA."""
    if idx < 0:
        return "A"
    out = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


async def run_event_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the three shared event settings:
    draft channel, announcement channel and draft time.

    The 5-minute warning used to be a fourth. It is per event now (#566),
    asked by the events wizard, because one answer for every event at once
    was never what the scheduler read.

    Individual events (add/edit/delete) live on the /events hub (#249)."""
    import wizard_registry

    guild_id = interaction.guild_id
    channel = interaction.channel
    user = interaction.user
    cancel_event = wizard_registry.register(user.id)

    from config import get_config, get_or_create_config, update_config_field

    guild_cfg = get_config(guild_id) or get_or_create_config(guild_id)
    timezone = guild_cfg.timezone if guild_cfg.timezone else "America/New_York"

    draft_channel_id = guild_cfg.event_draft_channel_id or 0
    announce_channel_id = guild_cfg.event_announce_channel_id or 0
    draft_time = guild_cfg.event_draft_time or "12:00"

    # Post-#249: this wizard owns only the shared event settings (channels
    # and draft time). Event creation, editing and deletion moved to the
    # /events hub, so officers managing individual events go there instead
    # of crawling through this wizard. The 5-minute warning followed them
    # in #566: it is per event, so a server-wide question could not answer
    # it.

    await channel.send(
        "⚙️ **Event Setup**\n"
        "Configure your alliance event channels and draft cadence. "
        "All events share these four settings. To add, edit, or remove "
        "individual events, run `/events` after this wizard completes."
    )

    # ── Steps 1-4: Channel/time settings ──────────────────────────────────────
    is_premium_flag = await premium.is_premium(
        guild_id, interaction=interaction, bot=interaction.client
    )
    current_draft_id = guild_cfg.event_draft_channel_id or 0
    draft_ch_view = ChannelSelectStep(
        "Select the draft channel...",
        suggested_name="event-drafts",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=current_draft_id,
    )
    if draft_ch_view.is_current_stale:
        await channel.send(PREV_CHANNEL_GONE.format(channel_label="draft"))
    await channel.send(
        "**Step 1 of 3 — Draft Channel**\n"
        "Which channel should the bot post event announcement drafts for leadership to review?\n"
        "*(This applies to all events)*",
        view=draft_ch_view,
    )
    await wait_view_or_cancel(draft_ch_view, cancel_event)
    if draft_ch_view.cancelled:
        return
    if not draft_ch_view.confirmed:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_EVENTS))
        return
    draft_channel_id = draft_ch_view.selected_channel.id

    current_ann_id = guild_cfg.event_announce_channel_id or 0
    ann_ch_view = ChannelSelectStep(
        "Select the announcement channel...",
        suggested_name="announcements",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=current_ann_id,
    )
    if ann_ch_view.is_current_stale:
        await channel.send(PREV_CHANNEL_GONE.format(channel_label="announcement"))
    await channel.send(
        "**Step 2 of 3 — Announcement Channel**\n"
        "Which channel should approved announcements be posted to?\n"
        "*(This applies to all events)*",
        view=ann_ch_view,
    )
    await wait_view_or_cancel(ann_ch_view, cancel_event)
    if ann_ch_view.cancelled:
        return
    if not ann_ch_view.confirmed:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_EVENTS))
        return
    announce_channel_id = ann_ch_view.selected_channel.id

    tz_label = TIMEZONE_LABELS.get(timezone, timezone)
    # `draft_time` is stored in 24h format ("12:00"); show it as-is in the
    # default button label, but accept either format from user input.
    # Re-prompt up to 3 times on unparseable input before bailing out.
    attempts_left = 3
    while True:
        draft_time_raw = await ask_keep_or_change(
            channel,
            f"**Step 3 of 3 — Draft Posting Time**\n"
            f"What time should the bot post the draft each event day? *(in {tz_label})*\n"
            f"*(e.g. `12:00pm` for noon)*",
            default="12:00",
            current=draft_time or "",
            modal_title="Draft Posting Time",
            modal_label="Time",
            timeout_cmd="setup_events",
            cancel_event=cancel_event,
        )
        if not draft_time_raw:
            return
        parsed_draft = _parse_12h_time(draft_time_raw)
        if parsed_draft:
            draft_time = parsed_draft
            break
        if (
            len(draft_time_raw) == 5
            and draft_time_raw[2] == ":"
            and draft_time_raw.replace(":", "").isdigit()
        ):
            draft_time = draft_time_raw  # already 24h
            break
        attempts_left -= 1
        if attempts_left <= 0:
            await channel.send(
                TIME_PARSE_GIVE_UP.format(
                    recovery=f"`/setup` → {HUB_BTN_EVENTS}",
                )
            )
            return
        await channel.send(TIME_PARSE_RETRY.format(raw=draft_time_raw))

    # There is no 5-minute warning step here any more (#566). It asked one
    # question for every event at once, which is not how the warning works:
    # the scheduler reads each event's own `five_min_warning` column and
    # never the server field, so this step could only ever seed new events
    # while claiming more. The question now belongs to the event, and the
    # events wizard asks it per event.
    #
    # `guild_configs.event_five_min_warning` is left in place, unread and
    # unwritten. Dropping it would break importing a config exported before
    # this, for a column nothing consults.

    update_config_field(guild_id, "event_draft_channel_id", draft_channel_id)
    update_config_field(guild_id, "event_announce_channel_id", announce_channel_id)
    update_config_field(guild_id, "event_draft_time", draft_time)

    # ── Summary ────────────────────────────────────────────────────────────────
    embed = discord.Embed(title="✅ Event Settings Saved", color=discord.Color.green())
    embed.add_field(name="Draft Channel", value=f"<#{draft_channel_id}>", inline=False)
    embed.add_field(name="Announcement Channel", value=f"<#{announce_channel_id}>", inline=False)
    embed.add_field(
        name="Draft Time", value=_format_time_with_tz(draft_time, timezone), inline=False
    )
    embed.set_footer(text="Run /events to add, edit, or remove individual events.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Event settings saved for guild {guild_id}")


async def run_birthday_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring birthday tracking."""
    import wizard_registry

    guild_id = interaction.guild_id
    channel = interaction.channel
    user = interaction.user
    cancel_event = wizard_registry.register(user.id)

    def check(m):
        return m.author == user and m.channel == channel

    async def ask_text(prompt: str, max_chars: int = 200):
        await channel.send(prompt)
        reply = await wizard_registry.wait_or_cancel(
            bot.wait_for("message", check=check, timeout=120),
            cancel_event,
        )
        if reply is None:
            if cancel_event.is_set():
                await channel.send(CANCEL_PLAIN)
            else:
                await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
            return None
        return reply.content.strip()[:max_chars]

    from config import (
        get_birthday_config,
        has_birthday_config,
        clear_birthday_config,
        get_config,
    )

    current = get_birthday_config(guild_id)
    birthdays_already_configured = has_birthday_config(guild_id)
    guild_cfg = get_config(guild_id)
    guild_tz = guild_cfg.timezone if guild_cfg else "America/New_York"

    # ── If already enabled, show summary and offer edit or cancel ─────────────
    if birthdays_already_configured and current.get("enabled"):
        rc = current.get("reminder_channel_id", 0) or 0
        fields = [
            ("Sheet Tab", current.get("tab_name") or "*not set*"),
            ("Name Column", _col_index_to_letter(current.get("name_col", 0))),
            ("Birthday Column", _col_index_to_letter(current.get("birthday_col", 0))),
            (
                "Train Integration",
                "✅ Enabled" if current.get("train_integration") else "❌ Disabled",
            ),
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
            (
                "Reminders",
                "✅ Enabled" if current.get("reminders_enabled") else "❌ Disabled",
            )
        )
        if current.get("reminders_enabled"):
            fields.append(("Reminder Channel", f"<#{rc}>" if rc else "*not set*"))
            fields.append(
                (
                    "Reminder Time",
                    _format_time_with_tz(current.get("reminder_time"), guild_tz) or "*not set*",
                )
            )
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="🎂 Current Birthday Setup",
            description="Birthday tracking is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Birthday tracking is still active.",
        )
        if proceed is not True:
            return

    await channel.send(
        "⚙️ **Birthday Tracking Setup**\nConfigure how the bot tracks member birthdays."
    )

    # ── Step 1: Enable? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        "**Step 1 of 9 — Enable birthday tracking?**\n"
        "Should the bot track member birthdays from your Google Sheet?",
        view=enabled_view,
    )
    await wait_view_or_cancel(enabled_view, cancel_event)
    if enabled_view.cancelled:
        return
    if enabled_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
        return
    if not enabled_view.selected:
        from config import save_birthday_config

        # `last_train_population_date` and `ignored_conflicts` are operational
        # state owned by the train-auto-pop scheduler and the conflict alert
        # (see #89, #334) — not `save_birthday_config` parameters. Strip them
        # from the splat so the disable path still works when those columns
        # exist on the loaded row.
        save_birthday_config(
            guild_id,
            enabled=0,
            **{
                k: v
                for k, v in current.items()
                if k
                not in ("guild_id", "enabled", "last_train_population_date", "ignored_conflicts")
            },
        )
        await ask_disable_with_clear(
            channel,
            feature_label="Birthday tracking",
            setup_command=f"setup → {HUB_BTN_BIRTHDAYS}",
            had_prior_config=birthdays_already_configured,
            clear_fn=lambda: clear_birthday_config(guild_id),
            cancel_event=cancel_event,
        )
        return

    # ── Step 2: Sheet tab ─────────────────────────────────────────────────────
    tab_name = await ask_keep_or_change(
        channel,
        "**Step 2 of 9 — Sheet Tab**\n"
        "Which tab in your Google Sheet contains birthday data?\n"
        "⚠️ *Make sure this tab exists in your sheet before continuing.*",
        default="Birthdays",
        current=current.get("tab_name", ""),
        modal_title="Sheet Tab Name",
        modal_label="Tab name",
        timeout_cmd="setup_birthdays",
        cancel_event=cancel_event,
    )
    if tab_name is None:
        return
    await warn_if_tab_claimed(channel, guild_id, tab_name, exclude_field="birthday_tab_name")

    # discord_id_col is no longer asked in the wizard — preserve any existing
    # value so save_birthday_config doesn't clobber it.
    discord_id_col = current.get("discord_id_col", -1)

    # ── Step 3: Name column ────────────────────────────────────────────────────
    saved_name_col = current.get("name_col")
    name_col_raw = await ask_keep_or_change(
        channel,
        "**Step 3 of 9 — Name Column**\nWhich column contains the member's name?",
        default="A",
        current=(
            _col_index_to_letter(saved_name_col)
            if isinstance(saved_name_col, int) and saved_name_col >= 0
            else ""
        ),
        modal_title="Name Column",
        modal_label="Column letter",
        timeout_cmd="setup_birthdays",
        cancel_event=cancel_event,
    )
    if name_col_raw is None:
        return
    name_col = _col_letter_to_index(name_col_raw)
    if name_col < 0:
        await channel.send(
            INPUT_INVALID.format(
                type="single column letter",
                example="A",
                recovery=f"`/setup` → {HUB_BTN_BIRTHDAYS}",
            )
        )
        return

    # ── Step 4: Birthday column ────────────────────────────────────────────────
    saved_bday_col = current.get("birthday_col")
    bday_col_raw = await ask_keep_or_change(
        channel,
        "**Step 4 of 9 — Birthday Column**\n"
        "Which column contains the member's birthday?\n"
        "ℹ️ *The bot accepts most date formats: `12/7`, `12-7`, `Dec 7`, "
        "`December 7`, `1990-12-07`, etc. Bare numeric dates like `7/12` are "
        "read as **M/D** (July 12) — use `Dec 7` if your alliance writes "
        "day-first.*",
        default="B",
        current=(
            _col_index_to_letter(saved_bday_col)
            if isinstance(saved_bday_col, int) and saved_bday_col >= 0
            else ""
        ),
        modal_title="Birthday Column",
        modal_label="Column letter",
        timeout_cmd="setup_birthdays",
        cancel_event=cancel_event,
    )
    if bday_col_raw is None:
        return
    birthday_col = _col_letter_to_index(bday_col_raw)
    if birthday_col < 0:
        await channel.send(
            INPUT_INVALID.format(
                type="single column letter",
                example="B",
                recovery=f"`/setup` → {HUB_BTN_BIRTHDAYS}",
            )
        )
        return

    # ── Step 5: Train integration ─────────────────────────────────────────────
    train_view = YesNoView()
    await channel.send(
        "**Step 5 of 9 — Train Schedule Integration**\n"
        "Should the bot automatically add members to the train schedule on their birthday?",
        view=train_view,
    )
    await wait_view_or_cancel(train_view, cancel_event)
    if train_view.cancelled:
        return
    if train_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
        return
    train_integration = 1 if train_view.selected else 0

    flexible_placement = 0
    lookahead_days = 14

    if not train_integration:
        await channel.send(
            "ℹ️ *Skipping Steps 6–7 (placement and lookahead) — train integration is off.*"
        )

    if train_integration:
        await channel.send(
            "ℹ️ Heads up: birthdays auto-populate the train schedule **once per day** "
            "(on the bot's first tick after server-time midnight). If you need a "
            "birthday reflected on the schedule sooner, open `/train` and click "
            "**🎂 Run birthday check** to trigger the check on demand."
        )

        # ── Step 6: Flexible placement ─────────────────────────────────────────
        class PlacementView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=120)
                self.selected = None

            @discord.ui.button(label="🎂 Birthday only", style=discord.ButtonStyle.primary)
            async def birthday_only(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 0
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter, content="✅ Placement: **Birthday only**", view=self
                )
                self.stop()

            @discord.ui.button(
                label="↔️ Assign nearby if taken", style=discord.ButtonStyle.secondary
            )
            async def flexible(self, inter: discord.Interaction, button: discord.ui.Button):
                self.selected = 1
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content="✅ Placement: **Assign 1 day before or after if birthday is taken**",
                    view=self,
                )
                self.stop()

        placement_view = PlacementView()
        await channel.send(
            "**Step 6 of 9 — Birthday Placement**\n"
            "If the member's birthday is already taken on the train schedule, what should the bot do?",
            view=placement_view,
        )
        await wait_view_or_cancel(placement_view, cancel_event)
        if placement_view.cancelled:
            return
        if placement_view.selected is None:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
            return
        flexible_placement = placement_view.selected

        # ── Step 7: Lookahead days ─────────────────────────────────────────────
        lookahead_raw = await ask_keep_or_change(
            channel,
            "**Step 7 of 9 — Train Schedule Lookahead**\n"
            "Since you enabled train integration, how many days ahead of a "
            "member's birthday should the bot pre-populate them on the train "
            "schedule? This only applies to train-integration auto-placement; "
            "the birthday announcement itself always fires on the day.\n"
            "*(we recommend 14)*",
            default="14",
            current=str(current.get("lookahead_days") or ""),
            modal_title="Lookahead Days",
            modal_label="Number of days",
            timeout_cmd="setup_birthdays",
            cancel_event=cancel_event,
        )
        if lookahead_raw is None:
            return
        try:
            lookahead_days = int(str(lookahead_raw).strip())
            if lookahead_days < 1:
                raise ValueError
        except ValueError:
            await channel.send(
                INPUT_INVALID.format(
                    type="number",
                    example="14",
                    recovery=f"`/setup` → {HUB_BTN_BIRTHDAYS}",
                )
            )
            return

    # ── Step 8: Birthday reminders ─────────────────────────────────────────────
    remind_view = YesNoView()
    await channel.send(
        "**Step 8 of 9 — Birthday Reminders**\n"
        "Should the bot post a message in Discord on a member's birthday?\n"
        '*(It will post: "🎂 Today is **[name]**\'s birthday!")*',
        view=remind_view,
    )
    await wait_view_or_cancel(remind_view, cancel_event)
    if remind_view.cancelled:
        return
    if remind_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
        return
    reminders_enabled = 1 if remind_view.selected else 0
    reminder_channel_id = 0
    reminder_time = "08:00"
    if not reminders_enabled:
        await channel.send(
            "ℹ️ *Skipping Steps 8a–8b (reminder channel and time) — birthday reminders are off.*"
        )

    if reminders_enabled:
        # ── Step 8a: Reminder channel ──────────────────────────────────────────
        is_premium_flag = await premium.is_premium(
            guild_id, interaction=interaction, bot=interaction.client
        )
        saved_remind_ch = current.get("reminder_channel_id", 0) or 0
        remind_ch_view = ChannelSelectStep(
            "Select the birthday announcement channel...",
            suggested_name="birthdays",
            include_threads=is_premium_flag,
            guild=interaction.guild,
            current_id=saved_remind_ch,
        )
        if remind_ch_view.is_current_stale:
            await channel.send(PREV_CHANNEL_GONE.format(channel_label="birthday"))
        await channel.send(
            "**Step 8a of 9 — Birthday Announcement Channel**\n"
            "Which channel should birthday announcements be posted in?",
            view=remind_ch_view,
        )
        await wait_view_or_cancel(remind_ch_view, cancel_event)
        if remind_ch_view.cancelled:
            return
        if not remind_ch_view.confirmed:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_BIRTHDAYS))
            return
        reminder_channel_id = remind_ch_view.selected_channel.id

        # ── Step 8b: Reminder time ─────────────────────────────────────────────
        # Re-prompt up to 3 times on unparseable input rather than silently
        # falling back to a default.
        from config import get_config

        guild_cfg = get_config(guild_id)
        tz_label = TIMEZONE_LABELS.get(
            guild_cfg.timezone if guild_cfg else "America/New_York", "your timezone"
        )
        attempts_left = 3
        reminder_time = "08:00"
        while True:
            time_raw = await ask_keep_or_change(
                channel,
                f"**Step 8b of 9 — Reminder Time**\n"
                f"What time should birthday announcements be posted? *(in {tz_label})*\n"
                f"*(e.g. `8:00am`, `12:00pm`)*",
                default="8:00am",
                # DB stores 24h ("08:00") — render as "8:00am" so the
                # Keep-current and Use-default button labels don't sit
                # side-by-side in mismatched formats.
                current=_format_24h_to_12h(current.get("reminder_time", "")),
                modal_title="Reminder Time",
                modal_label="Time",
                timeout_cmd="setup_birthdays",
                cancel_event=cancel_event,
            )
            if time_raw is None:
                return
            parsed = _parse_12h_time(time_raw)
            if parsed:
                reminder_time = parsed
                break
            if len(time_raw) == 5 and time_raw[2] == ":" and time_raw.replace(":", "").isdigit():
                reminder_time = time_raw  # already 24h
                break
            attempts_left -= 1
            if attempts_left <= 0:
                await channel.send(
                    TIME_PARSE_GIVE_UP.format(
                        recovery=f"`/setup` → {HUB_BTN_BIRTHDAYS}",
                    )
                )
                return
            await channel.send(TIME_PARSE_RETRY.format(raw=time_raw))

    # ── Step 9: Birthday DM body (💎 Premium) ─────────────────────────────────
    # Customisable body of the per-member birthday DM that fires alongside
    # the channel announcement on Premium guilds. Free guilds can configure
    # now — it just won't fire until they have Premium + Member Sync
    # AND a Discord ID column wired up in the birthday sheet.
    birthday_dm_message = ""
    if reminders_enabled:
        from train_cog import DEFAULT_BIRTHDAY_DM

        saved_birthday_dm = (current.get("dm_message") or "").strip()
        bday_dm_input = await ask_keep_or_change(
            channel,
            "**Step 9 of 9 — Birthday DM Body (💎 Premium)**\n"
            "When a birthday fires, the bot also DMs the member directly with a personal "
            "note. Free guilds can configure this now — it just won't fire until you have "
            "Premium + Member Sync + a Discord ID column in your birthday sheet.\n\n"
            "Use `{name}` as a placeholder for the member's name.",
            default=DEFAULT_BIRTHDAY_DM,
            current=saved_birthday_dm,
            modal_title="Birthday DM Body",
            modal_label="DM body (max 1000 chars)",
            timeout_cmd="setup_birthdays",
            cancel_event=cancel_event,
        )
        if bday_dm_input is None:
            return
        birthday_dm_message = "" if bday_dm_input == DEFAULT_BIRTHDAY_DM else bday_dm_input

    # ── Save ───────────────────────────────────────────────────────────────────
    from config import save_birthday_config

    save_birthday_config(
        guild_id=guild_id,
        tab_name=tab_name,
        name_col=name_col,
        birthday_col=birthday_col,
        discord_id_col=discord_id_col,
        data_start_row=2,
        enabled=1,
        train_integration=train_integration,
        flexible_placement=flexible_placement,
        lookahead_days=lookahead_days,
        reminders_enabled=reminders_enabled,
        reminder_channel_id=reminder_channel_id,
        reminder_time=reminder_time,
        dm_message=birthday_dm_message,
    )

    embed = discord.Embed(title="✅ Birthday Tracking Configured", color=discord.Color.green())
    embed.add_field(name="Sheet Tab", value=tab_name, inline=True)
    embed.add_field(
        name="Name Column", value=f"Column {_col_index_to_letter(name_col)}", inline=True
    )
    embed.add_field(
        name="Birthday Column", value=f"Column {_col_index_to_letter(birthday_col)}", inline=True
    )
    embed.add_field(
        name="Discord ID Column",
        value=f"Column {_col_index_to_letter(discord_id_col)}"
        if discord_id_col >= 0
        else "Not stored",
        inline=True,
    )
    embed.add_field(
        name="Train Integration", value="Enabled" if train_integration else "Disabled", inline=True
    )
    if train_integration:
        embed.add_field(
            name="Placement",
            value="Flexible (±1 day)" if flexible_placement else "Birthday only",
            inline=True,
        )
        embed.add_field(name="Lookahead", value=f"{lookahead_days} days", inline=True)
    embed.add_field(
        name="Reminders", value="Enabled" if reminders_enabled else "Disabled", inline=True
    )
    if reminders_enabled:
        embed.add_field(name="Reminder Channel", value=f"<#{reminder_channel_id}>", inline=True)
        embed.add_field(
            name="Reminder Time", value=_format_time_with_tz(reminder_time, guild_tz), inline=True
        )
    embed.set_footer(text=SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_BIRTHDAYS))
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Birthday config saved for guild {guild_id}")


async def run_shiny_tasks_setup(interaction: discord.Interaction, bot):
    """Walk leadership through configuring the daily shiny-tasks
    announcement. Six steps: enable → channel → server range → post
    time → message template → confirm. Free for all tiers."""
    import wizard_registry
    from config import (
        get_config,
        get_shiny_tasks_config,
        save_shiny_tasks_config,
        has_shiny_tasks_config,
        clear_shiny_tasks_config,
    )
    from defaults import DEFAULT_SHINY_TASKS_MESSAGE

    guild_id = interaction.guild_id
    channel = interaction.channel
    user = interaction.user
    cancel_event = wizard_registry.register(user.id)

    current = get_shiny_tasks_config(guild_id)
    cfg = get_config(guild_id)
    guild_tz = cfg.timezone if cfg else "America/New_York"
    tz_label = TIMEZONE_LABELS.get(guild_tz, "ET")
    shiny_already_configured = has_shiny_tasks_config(guild_id)

    # ── If already enabled, show summary and offer edit or cancel ─────────────
    if shiny_already_configured and current.get("enabled"):
        ch_id = current.get("channel_id", 0) or 0
        fields = [
            ("Channel", f"<#{ch_id}>" if ch_id else "*not set*"),
            (
                "Server Range",
                f"{current.get('server_min') or '?'} – {current.get('server_max') or '?'}",
            ),
            (
                "Post Time",
                _format_time_with_tz(current.get("post_time"), guild_tz) or "*not set*",
            ),
            (
                "Message",
                "Custom" if (current.get("message_template") or "").strip() else "Default",
            ),
        ]
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="🌟 Current Shiny Tasks Setup",
            description="The daily shiny-tasks announcement is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. The daily announcement is still active.",
        )
        if proceed is not True:
            wizard_registry.unregister(user.id, cancel_event)
            return

    await channel.send(
        "🌟 **Daily Shiny Tasks Setup**\n"
        "Each day, the bot can post the list of Last War servers where "
        "shiny tasks are available, filtered to the servers your alliance "
        "can reach."
    )

    # ── Step 1: Enable? ───────────────────────────────────────────────────────
    enabled_view = YesNoView()
    await channel.send(
        "**Step 1 of 6 — Enable daily shiny tasks announcement?**",
        view=enabled_view,
    )
    await wait_view_or_cancel(enabled_view, cancel_event)
    if enabled_view.cancelled:
        wizard_registry.unregister(user.id, cancel_event)
        return
    if enabled_view.selected is None:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SHINY))
        wizard_registry.unregister(user.id, cancel_event)
        return
    if not enabled_view.selected:
        # Disable + persist the previously-saved range/channel/etc. so the
        # next Shiny Tasks setup wizard run can offer them back as "current".
        save_shiny_tasks_config(
            guild_id,
            enabled=0,
            channel_id=current.get("channel_id", 0),
            post_time=current.get("post_time", "09:00"),
            server_min=current.get("server_min", 0),
            server_max=current.get("server_max", 0),
            message_template=current.get("message_template", ""),
        )
        await ask_disable_with_clear(
            channel,
            feature_label="Shiny tasks announcement",
            setup_command="setup → 🌟 Shiny Tasks",
            had_prior_config=shiny_already_configured,
            clear_fn=lambda: clear_shiny_tasks_config(guild_id),
            cancel_event=cancel_event,
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    # ── Step 2: Channel ───────────────────────────────────────────────────────
    is_premium_flag = await premium.is_premium(
        guild_id, interaction=interaction, bot=interaction.client
    )
    await channel.send(
        "**Step 2 of 6 — Announcement Channel**\n"
        "Pick the channel where the daily shiny tasks post should be posted."
    )
    saved_channel_id = current.get("channel_id", 0) or 0
    ch_view = ChannelSelectStep(
        "Select the shiny tasks channel...",
        suggested_name="shiny-tasks",
        include_threads=is_premium_flag,
        guild=interaction.guild,
        current_id=saved_channel_id,
    )
    if ch_view.is_current_stale:
        await channel.send(PREV_CHANNEL_GONE.format(channel_label="shiny tasks"))
    await channel.send("​", view=ch_view)
    await wait_view_or_cancel(ch_view, cancel_event)
    if ch_view.cancelled:
        wizard_registry.unregister(user.id, cancel_event)
        return
    if not ch_view.confirmed:
        await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SHINY))
        wizard_registry.unregister(user.id, cancel_event)
        return
    channel_id = ch_view.selected_channel.id

    # ── Step 3: Server range (min + max in one modal) ─────────────────────────
    class ServerRangeModal(discord.ui.Modal):
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

    saved_min = current.get("server_min") or 0
    saved_max = current.get("server_max") or 0
    range_prompt = (
        "**Step 3 of 6 — Server Range**\n"
        "Enter the lowest and highest server numbers your alliance can "
        "reach. Typically your transfer range."
    )
    range_attempts_left = 3
    server_min = server_max = None
    while True:
        range_modal = ServerRangeModal(
            min_default=str(saved_min) if saved_min else "",
            max_default=str(saved_max) if saved_max else "",
        )
        # ServerRangeModal's `value` is a read-only @property derived
        # from min_value + max_value, so ModalLaunchView's default
        # `modal.value = current_value` path won't work. Use the
        # on_keep_current callback to populate the underlying
        # attributes directly when leadership clicks Keep current.
        try:
            has_saved_range = int(saved_min) >= 1 and int(saved_max) >= int(saved_min)
        except (TypeError, ValueError):
            has_saved_range = False
        if has_saved_range:
            keep_min, keep_max = int(saved_min), int(saved_max)

            def _keep_range(modal, _min=keep_min, _max=keep_max):
                modal.min_value = str(_min)
                modal.max_value = str(_max)

            range_launcher = ModalLaunchView(
                range_modal,
                current_value=f"{keep_min} – {keep_max}",
                current_display=f"{keep_min} – {keep_max}",
                on_keep_current=_keep_range,
            )
        else:
            range_launcher = ModalLaunchView(range_modal)
        # Override the generic "Enter Value" button label so leadership
        # sees domain wording. Find by label rather than index because
        # the Keep-current button (when present) sits at children[0].
        for _child in range_launcher.children:
            if isinstance(_child, discord.ui.Button) and _child.label == "✏️ Enter Value":
                _child.label = "✏️ Enter Server Numbers"
                break
        await channel.send(range_prompt, view=range_launcher)
        await wait_view_or_cancel(range_launcher, cancel_event)
        if range_launcher.cancelled:
            wizard_registry.unregister(user.id, cancel_event)
            return
        if not range_launcher.confirmed:
            await channel.send(WIZARD_TIMEOUT.format(wizard=HUB_BTN_SHINY))
            wizard_registry.unregister(user.id, cancel_event)
            return

        min_raw = (range_modal.min_value or "").strip()
        max_raw = (range_modal.max_value or "").strip()
        try:
            candidate_min = int(min_raw)
            candidate_max = int(max_raw)
            valid_numbers = True
        except (TypeError, ValueError):
            valid_numbers = False

        if valid_numbers and candidate_min >= 1 and candidate_min <= candidate_max:
            server_min = candidate_min
            server_max = candidate_max
            break

        range_attempts_left -= 1
        if range_attempts_left <= 0:
            await channel.send(
                "⚠️ Could not read those server numbers after a few tries. "
                "Run `/setup` → 🌟 Shiny Tasks to start over."
            )
            wizard_registry.unregister(user.id, cancel_event)
            return

        # Pre-fill the retry modal with whatever the user typed, so the
        # correction is one tap away instead of re-entering both fields.
        saved_min = min_raw
        saved_max = max_raw
        if not valid_numbers:
            await channel.send(
                f"⚠️ Could not read **`{min_raw}`** / **`{max_raw}`** as whole "
                f"numbers. Try something like `677` and `804`. Let's try once more."
            )
        else:
            await channel.send(
                f"⚠️ The lowest server (**`{min_raw}`**) must be ≥ 1 and "
                f"≤ the highest (**`{max_raw}`**). Let's try once more."
            )

    # ── Step 4: Post time ─────────────────────────────────────────────────────
    attempts_left = 3
    post_time = "09:00"
    while True:
        time_raw = await ask_keep_or_change(
            channel,
            f"**Step 4 of 6 — Post Time**\n"
            f"What time of day should the announcement post? "
            f"*(in your timezone: {tz_label})*\n"
            f"*(e.g. `9:00am`, `10:30am`, `9:00pm`)*",
            default="9:00am",
            # DB stores '09:00' (24h) — render as '9:00am' before showing
            # so 'Keep current' and 'Use default' don't sit side-by-side
            # in mismatched formats.
            current=_format_24h_to_12h(current.get("post_time", "")),
            modal_title="Post Time",
            modal_label="Time",
            timeout_cmd="setup_shiny_tasks",
            cancel_event=cancel_event,
        )
        if time_raw is None:
            wizard_registry.unregister(user.id, cancel_event)
            return
        parsed = _parse_12h_time(time_raw)
        if parsed:
            post_time = parsed
            break
        if len(time_raw) == 5 and time_raw[2] == ":" and time_raw.replace(":", "").isdigit():
            post_time = time_raw  # already 24h
            break
        attempts_left -= 1
        if attempts_left <= 0:
            await channel.send(
                TIME_PARSE_GIVE_UP.format(
                    recovery=f"`/setup` → {HUB_BTN_SHINY}",
                )
            )
            wizard_registry.unregister(user.id, cancel_event)
            return
        await channel.send(TIME_PARSE_RETRY.format(raw=time_raw))

    # ── Step 5: Message template ──────────────────────────────────────────────
    saved_template = (current.get("message_template") or "").strip()
    template_input = await ask_keep_or_change(
        channel,
        "**Step 5 of 6 — Announcement Message**\n"
        "Customize the announcement body, or use the default. "
        "Placeholders: `{servers}` and `{date}`.",
        default=DEFAULT_SHINY_TASKS_MESSAGE,
        current=saved_template or None,
        modal_title="Shiny Tasks Message",
        modal_label="Message body",
        timeout_cmd="setup_shiny_tasks",
        cancel_event=cancel_event,
    )
    if template_input is None:
        wizard_registry.unregister(user.id, cancel_event)
        return
    # Store empty string when the user picks "use default" so a future
    # change to DEFAULT_SHINY_TASKS_MESSAGE automatically propagates,
    # instead of freezing today's wording in the DB.
    message_template = (
        ""
        if template_input.strip() == DEFAULT_SHINY_TASKS_MESSAGE.strip()
        else template_input.strip()
    )

    # ── Step 6: Confirm + save ────────────────────────────────────────────────
    embed = discord.Embed(
        title="🌟 Shiny Tasks — Final Review",
        description="Confirm to save this configuration.",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Status", value="✅ Enabled", inline=True)
    embed.add_field(name="Channel", value=f"<#{channel_id}>", inline=True)
    embed.add_field(name="Server Range", value=f"{server_min} – {server_max}", inline=True)
    embed.add_field(name="Post Time", value=_format_time_with_tz(post_time, guild_tz), inline=True)
    embed.add_field(
        name="Message",
        value=(message_template or DEFAULT_SHINY_TASKS_MESSAGE)[:1024],
        inline=False,
    )
    confirm_view = ConfirmView()
    await channel.send(embed=embed, view=confirm_view)
    await wait_view_or_cancel(confirm_view, cancel_event)
    if confirm_view.cancelled:
        wizard_registry.unregister(user.id, cancel_event)
        return
    if not confirm_view.confirmed:
        await channel.send(f"{CANCEL_PLAIN} Run `/setup` → {HUB_BTN_SHINY} to start again.")
        wizard_registry.unregister(user.id, cancel_event)
        return

    save_shiny_tasks_config(
        guild_id,
        enabled=1,
        channel_id=channel_id,
        post_time=post_time,
        server_min=server_min,
        server_max=server_max,
        message_template=message_template,
    )

    _human = _format_time_with_tz(post_time, guild_tz) or post_time

    await channel.send(f"✅ Shiny-tasks announcement saved! The first post will fire at {_human}.")
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Shiny tasks config saved for guild {guild_id}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

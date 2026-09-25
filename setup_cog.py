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

# The birthday wizard lives in birthday_setup.py (#611 round 2); imported back
# under its old name for the hub, the launcher and the tests.
from birthday_setup import run_birthday_setup

# The Growth Breakdown wizard lives in growth_breakdown_setup.py (#611 round
# 2); imported back under its old name for the hub, the launcher and the tests.
from growth_breakdown_setup import run_growth_breakdown_setup

# The Shiny Tasks wizard lives in shiny_tasks_setup.py (#611 round 2); imported
# back under its old name for the hub, the launcher and the tests.
from shiny_tasks_setup import run_shiny_tasks_setup

# The growth wizard lives in growth_setup.py (#611 round 2); imported back
# under its old name for the hub, the launcher and the tests.
from growth_setup import run_growth_setup
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


def tab_claim_warning(
    guild_id: int,
    tab: str,
    *,
    exclude_field: str,
    exclude_survey_id: str | None = None,
) -> str | None:
    """The warning text if `tab` already belongs to another feature, else
    None. Extracted from `warn_if_tab_claimed` (#503) for wizards that
    cannot `channel.send` it themselves -- VS setup is an ephemeral panel
    driven by modals, so it sends this text through an interaction
    followup instead. Never raises; a lookup failure reads as "no claim".
    """
    try:
        from config import tabs_in_use

        owner = tabs_in_use(
            guild_id, exclude_field=exclude_field, exclude_survey_id=exclude_survey_id
        ).get((tab or "").casefold())
    except Exception as e:
        print(f"[SETUP] tab claim check failed guild={guild_id}: {type(e).__name__}: {e}")
        return None

    if not owner:
        return None

    return (
        f"⚠️ Heads up: **{tab}** is also {owner}. Two features writing to one "
        f"tab will overwrite each other's columns. That's fine if you meant "
        f"it, otherwise pick a different tab here or in that feature's setup."
    )


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
    message = tab_claim_warning(
        guild_id, tab, exclude_field=exclude_field, exclude_survey_id=exclude_survey_id
    )
    if message is None:
        return False
    await channel.send(message)
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
    await wizard_registry.guard_wizard_launch(run_train_setup(interaction, bot), interaction)


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
    await wizard_registry.guard_wizard_launch(run_buddy_setup(interaction, bot), interaction)


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
    await wizard_registry.guard_wizard_launch(run_growth_setup(interaction, bot), interaction)


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
    await wizard_registry.guard_wizard_launch(
        run_growth_breakdown_setup(interaction, bot), interaction
    )


async def _launch_birthday_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to open the birthday wizard."
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting birthday setup — check the channel for prompts!")
    await wizard_registry.guard_wizard_launch(run_birthday_setup(interaction, bot), interaction)


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
    await wizard_registry.guard_wizard_launch(
        run_storm_setup(interaction, bot, event_type), interaction
    )


async def _launch_event_setup(interaction: discord.Interaction, bot) -> None:
    if not _has_leadership_or_admin(interaction):
        await _send_ack(
            interaction, "⛔ You need the leadership role (or admin) to open the event wizard."
        )
        return
    if not await _check_wizard_can_run(interaction, "setup"):
        return
    await _send_ack(interaction, "⚙️ Starting event setup — check the channel for prompts!")
    await wizard_registry.guard_wizard_launch(run_event_setup(interaction, bot), interaction)


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
    await wizard_registry.guard_wizard_launch(run_shiny_tasks_setup(interaction, bot), interaction)


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
    # Wrapped in try/finally (#582): every early return below used to skip
    # the unregister call, and so did any exception (a lost-access Forbidden
    # mid-wizard, for instance) — leaking the cancel event for this user
    # until the process restarted.
    try:
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

        # ── Steps 1-4: Channel/time settings ──────────────────────────────────
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

        # ── Summary ────────────────────────────────────────────────────────────
        embed = discord.Embed(title="✅ Event Settings Saved", color=discord.Color.green())
        embed.add_field(name="Draft Channel", value=f"<#{draft_channel_id}>", inline=False)
        embed.add_field(
            name="Announcement Channel", value=f"<#{announce_channel_id}>", inline=False
        )
        embed.add_field(
            name="Draft Time", value=_format_time_with_tz(draft_time, timezone), inline=False
        )
        embed.set_footer(text="Run /events to add, edit, or remove individual events.")
        await channel.send(embed=embed)
        print(f"[SETUP] Event settings saved for guild {guild_id}")
    finally:
        wizard_registry.unregister(user.id, cancel_event)
    print(f"[SETUP] Event settings saved for guild {guild_id}")


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupCog(bot))

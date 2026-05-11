"""
Command coverage audit — phase 1 of the full-coverage suite.

Two goals:

  1. **Registration audit.** Every cog imports cleanly and exposes the
     exact set of slash commands we expect. If a command is renamed,
     removed, or accidentally un-decorated, this fails immediately.

  2. **Gate smoke tests.** Every slash command is callable end-to-end
     in its rejection path — i.e. when the caller lacks the required
     permission, when the bot isn't set up, when premium is required,
     etc. This catches:
       * import-time errors in command bodies
       * silent dead-end branches that never reply to the user
       * gate logic that crashes instead of replying

Workflow happy paths and wizard branches are covered in the other
`tests/integration/test_*.py` files; this file's job is breadth, not
depth.
"""
from __future__ import annotations

import asyncio
import sys
import os
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import (
    TEST_GUILD_ID,
    PREMIUM_TEST_GUILD_ID,
    make_mock_interaction,
    make_mock_user,
    make_mock_channel,
    make_mock_guild,
)


# ── Expected registered commands per cog ──────────────────────────────────────

EXPECTED_COG_COMMANDS = {
    "SetupCog": {
        "setup", "view_configuration", "setup_reset",
        "setup_train", "setup_growth", "setup_growth_breakdown",
        "setup_birthdays",
        "setup_desertstorm", "setup_canyonstorm",
        "setup_events", "setup_survey", "setup_shiny_tasks",
    },
    "StormCog": {
        "desertstorm_draft", "canyonstorm_draft",
        "desertstorm", "canyonstorm",
    },
    "LogCog": {
        "desertstorm_participation", "canyonstorm_participation",
        "desertstorm_log", "canyonstorm_log",
        "desertstorm_remind", "canyonstorm_remind",
    },
    "SurveyCog": {
        "survey_post", "survey", "survey_remind",
    },
    "TrainCog": {
        "train_addbirthdays", "birthdays", "train_log",
        "cancel", "train",
    },
    "MemberRosterCog": {
        "sync_members", "setup_members",
    },
    "DonateCog": {
        "donate", "upgrade",
        "premium_assign", "premium_status", "premium_unassign",
    },
}

# Module-level slash commands defined directly in bot.py (not on a cog).
EXPECTED_MODULE_COMMANDS = {"growth", "events", "events_log", "help"}


# ── Cog instantiation helpers ─────────────────────────────────────────────────

def _make_cog(cog_class):
    """Instantiate a cog with a MagicMock bot and a no-op task loop."""
    bot = MagicMock()
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    return cog_class(bot)


def _commands_on(cog) -> set[str]:
    """All slash command names registered on a cog instance."""
    out: set[str] = set()
    # Cogs in this codebase use @app_commands.command at class level; the
    # commands appear as `app_commands.Command` attributes. Search the
    # instance's class for them.
    import discord.app_commands as _ac
    for attr_name in dir(cog):
        attr = getattr(cog, attr_name, None)
        if isinstance(attr, _ac.Command):
            out.add(attr.name)
    return out


# ── 1. Registration audit ─────────────────────────────────────────────────────

class TestCogRegistration:
    """Every cog's expected slash commands are actually wired up."""

    def test_setup_cog_registers_expected_commands(self, seeded_db):
        from setup_cog import SetupCog
        cog = _make_cog(SetupCog)
        try:
            assert _commands_on(cog) == EXPECTED_COG_COMMANDS["SetupCog"]
        finally:
            # SetupCog doesn't start tasks; nothing to tear down
            pass

    def test_storm_cog_registers_expected_commands(self, seeded_db):
        from storm import StormCog
        cog = _make_cog(StormCog)
        assert _commands_on(cog) == EXPECTED_COG_COMMANDS["StormCog"]

    def test_log_cog_registers_expected_commands(self, seeded_db):
        from storm_log import LogCog
        cog = _make_cog(LogCog)
        assert _commands_on(cog) == EXPECTED_COG_COMMANDS["LogCog"]

    @pytest.mark.asyncio
    async def test_survey_cog_registers_expected_commands(self, seeded_db):
        # SurveyCog.__init__ starts a tasks.loop, which needs a running
        # event loop — hence async.
        from survey import SurveyCog
        cog = _make_cog(SurveyCog)
        try:
            assert _commands_on(cog) == EXPECTED_COG_COMMANDS["SurveyCog"]
        finally:
            try:
                cog.check_scheduled_reminders.cancel()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_train_cog_registers_expected_commands(self, seeded_db):
        from train_cog import TrainCog
        cog = _make_cog(TrainCog)
        try:
            assert _commands_on(cog) == EXPECTED_COG_COMMANDS["TrainCog"]
        finally:
            try:
                cog.check_reminder.cancel()
            except Exception:
                pass

    def test_member_roster_cog_registers_expected_commands(self, seeded_db):
        from member_roster import MemberRosterCog
        cog = _make_cog(MemberRosterCog)
        assert _commands_on(cog) == EXPECTED_COG_COMMANDS["MemberRosterCog"]

    def test_donate_cog_registers_expected_commands(self, seeded_db):
        from donate import DonateCog
        cog = _make_cog(DonateCog)
        assert _commands_on(cog) == EXPECTED_COG_COMMANDS["DonateCog"]

    def test_module_level_commands_registered_on_bot_tree(self, seeded_db):
        """bot.py defines a handful of commands directly on `bot.tree`
        rather than via a cog. Walk the registered command tree and
        confirm every expected command is present."""
        import bot as bot_module

        # CommandTree.get_commands() returns a list[Command] for the
        # global scope. Map by name and assert membership.
        registered = {c.name for c in bot_module.bot.tree.get_commands()}
        for name in EXPECTED_MODULE_COMMANDS:
            assert name in registered, (
                f"bot.py's command tree is missing /{name}. "
                f"Registered commands: {sorted(registered)}"
            )

    @pytest.mark.asyncio
    async def test_no_unexpected_extra_commands(self, seeded_db):
        """Catch the inverse: a command that exists on a cog but isn't in
        our expected set (e.g. someone added /foo without updating docs).
        Async because SurveyCog/TrainCog start tasks.loops at construction."""
        from setup_cog import SetupCog
        from storm import StormCog
        from storm_log import LogCog
        from survey import SurveyCog
        from train_cog import TrainCog
        from member_roster import MemberRosterCog
        from donate import DonateCog

        for cog_class in (SetupCog, StormCog, LogCog, SurveyCog,
                          TrainCog, MemberRosterCog, DonateCog):
            cog = _make_cog(cog_class)
            expected = EXPECTED_COG_COMMANDS[cog_class.__name__]
            actual   = _commands_on(cog)
            extra    = actual - expected
            try:
                assert not extra, (
                    f"{cog_class.__name__} has unexpected commands: {extra}. "
                    f"Either add them to EXPECTED_COG_COMMANDS or remove from the cog."
                )
            finally:
                for loop_name in ("check_scheduled_reminders", "check_reminder"):
                    try:
                        getattr(cog, loop_name).cancel()
                    except Exception:
                        pass


# ── 2. Gate smoke tests ───────────────────────────────────────────────────────

# Helper: build a non-leadership, non-admin interaction.
def _make_nonprivileged_interaction(guild_id=TEST_GUILD_ID):
    interaction = make_mock_interaction(guild_id=guild_id, is_admin=False)
    interaction.user.roles = []   # no leadership role
    return interaction


# Helper: build a fully-privileged interaction (admin + leadership role)
# on a fully-set-up guild.
def _make_leadership_interaction(guild_id=TEST_GUILD_ID):
    interaction = make_mock_interaction(guild_id=guild_id, is_admin=True)
    role = MagicMock()
    role.name = "Leadership"
    interaction.user.roles = [role]
    return interaction


# Capture helper — pulls the most recent send call's content/embed.
def _last_message(interaction):
    for sender in (interaction.response.send_message,
                   interaction.followup.send):
        if sender.call_args:
            args, kwargs = sender.call_args
            content = args[0] if args else kwargs.get("content") or ""
            embed   = kwargs.get("embed")
            return (content, embed)
    return ("", None)


# ── /setup_* commands: leadership-or-admin gate ──────────────────────────────

class TestSetupStarCommandsGateNonAdmins:
    """Each /setup_* command rejects non-admin, non-leadership users."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "setup_train", "setup_growth", "setup_birthdays",
        "setup_desertstorm", "setup_canyonstorm",
        "setup_events", "setup_survey", "setup_shiny_tasks",
    ])
    async def test_rejects_non_privileged_caller(self, seeded_db, command_name):
        from setup_cog import SetupCog
        cog = _make_cog(SetupCog)

        interaction = _make_nonprivileged_interaction()
        cmd = getattr(cog, command_name)
        await cmd.callback(cog, interaction)

        content, _ = _last_message(interaction)
        lowered = (content or "").lower()
        # Either the leadership wording or the admin wording is acceptable
        assert "leadership" in lowered or "admin" in lowered, (
            f"/{command_name} should reject non-privileged caller, got: {content!r}"
        )


class TestSetupAndResetGateNonAdmins:
    """/setup, /setup_reset, /view_configuration require admin only."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "setup", "setup_reset", "view_configuration",
    ])
    async def test_rejects_non_admin(self, seeded_db, command_name):
        from setup_cog import SetupCog
        cog = _make_cog(SetupCog)

        interaction = make_mock_interaction(is_admin=False)
        cmd = getattr(cog, command_name)
        await cmd.callback(cog, interaction)

        content, _ = _last_message(interaction)
        assert "admin" in (content or "").lower()


# ── /sync_members + /setup_members ───────────────────────────────────────────

class TestMemberRosterCommandsGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", ["sync_members", "setup_members"])
    async def test_rejects_non_privileged(self, seeded_db, command_name):
        import premium
        premium.clear_cache()

        from member_roster import MemberRosterCog
        cog = _make_cog(MemberRosterCog)

        interaction = _make_nonprivileged_interaction()
        cmd = getattr(cog, command_name)
        await cmd.callback(cog, interaction)

        content, _ = _last_message(interaction)
        lowered = (content or "").lower()
        assert "leadership" in lowered or "admin" in lowered

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    @pytest.mark.parametrize("command_name", ["sync_members", "setup_members"])
    async def test_premium_locked_for_free_admin(self, seeded_db, command_name):
        """An admin on a free guild gets the premium-locked embed."""
        import premium
        premium.clear_cache()

        from member_roster import MemberRosterCog
        cog = _make_cog(MemberRosterCog)

        interaction = make_mock_interaction(is_admin=True)
        # Free guild — no entitlements
        interaction.entitlements = []
        cmd = getattr(cog, command_name)
        await cmd.callback(cog, interaction)

        _, embed = _last_message(interaction)
        assert embed is not None, (
            f"/{command_name} on free tier should show the premium-locked embed"
        )
        assert "Premium" in (embed.title or "")


# ── Storm + LogCog command gates (leadership role) ──────────────────────────

class TestStormCommandsGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "desertstorm_draft", "canyonstorm_draft",
        "desertstorm", "canyonstorm",
    ])
    async def test_rejects_caller_without_leadership_role(self, seeded_db, command_name):
        from storm import StormCog
        cog = _make_cog(StormCog)

        interaction = make_mock_interaction()
        interaction.user.roles = []   # no leadership role

        cmd = getattr(cog, command_name)
        await cmd.callback(cog, interaction)

        content, _ = _last_message(interaction)
        assert "leadership" in (content or "").lower()


class TestLogCommandsGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "desertstorm_participation", "canyonstorm_participation",
        "desertstorm_log", "canyonstorm_log",
        "desertstorm_remind", "canyonstorm_remind",
    ])
    async def test_rejects_caller_without_leadership_role(self, seeded_db, command_name):
        from storm_log import LogCog
        cog = _make_cog(LogCog)

        interaction = make_mock_interaction()
        interaction.user.roles = []   # no leadership role

        cmd = getattr(cog, command_name)
        # /[event]_log takes a date arg; the gate runs first, so any
        # extra positional doesn't matter — but the callback signature
        # does. Pass `None` for the optional kwargs.
        try:
            await cmd.callback(cog, interaction)
        except TypeError:
            # date-taking commands need a second arg
            await cmd.callback(cog, interaction, None)

        content, _ = _last_message(interaction)
        assert "leadership" in (content or "").lower()


# ── Survey commands ───────────────────────────────────────────────────────────

class TestSurveyCommandsGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "survey_post", "survey", "survey_remind",
    ])
    async def test_rejects_caller_without_leadership_role(self, seeded_db, command_name):
        from survey import SurveyCog
        cog = _make_cog(SurveyCog)
        try:
            interaction = make_mock_interaction()
            interaction.user.roles = []   # no leadership role

            cmd = getattr(cog, command_name)
            await cmd.callback(cog, interaction)

            content, _ = _last_message(interaction)
            assert "leadership" in (content or "").lower()
        finally:
            try:
                cog.check_scheduled_reminders.cancel()
            except Exception:
                pass


# ── Train commands ────────────────────────────────────────────────────────────

class TestTrainCommandsGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "train", "train_log", "train_addbirthdays", "birthdays",
    ])
    async def test_rejects_caller_without_leadership_role(self, seeded_db, command_name):
        from train_cog import TrainCog
        cog = _make_cog(TrainCog)
        try:
            interaction = make_mock_interaction()
            interaction.user.roles = []   # no leadership role

            cmd = getattr(cog, command_name)
            try:
                await cmd.callback(cog, interaction)
            except TypeError:
                await cmd.callback(cog, interaction, None)  # /train_log [date]

            content, _ = _last_message(interaction)
            assert "leadership" in (content or "").lower()
        finally:
            try:
                cog.check_reminder.cancel()
            except Exception:
                pass


# ── Donate / Upgrade are unguarded (anyone can run them) ─────────────────────

class TestUnguardedCommandsRespond:
    """/donate and /upgrade work for anyone; they should reply."""

    @pytest.mark.asyncio
    async def test_donate_replies(self, seeded_db):
        from donate import DonateCog
        cog = _make_cog(DonateCog)
        interaction = make_mock_interaction(is_admin=False)
        interaction.entitlements = []

        await cog.donate.callback(cog, interaction)
        # Just assert something was sent — the body is config-dependent.
        assert interaction.response.send_message.called

    @pytest.mark.asyncio
    async def test_upgrade_replies(self, seeded_db):
        import premium
        premium.clear_cache()

        from donate import DonateCog
        cog = _make_cog(DonateCog)
        interaction = make_mock_interaction(is_admin=False)
        interaction.entitlements = []

        await cog.upgrade.callback(cog, interaction)
        # Either the premium-active embed or the upsell embed; both are valid.
        _, embed = _last_message(interaction)
        assert embed is not None


# ── /cancel ───────────────────────────────────────────────────────────────────

class TestCancelCommand:

    @pytest.mark.asyncio
    async def test_cancel_with_no_active_wizard(self, seeded_db):
        """/cancel run without an active wizard should reply gracefully —
        not crash, not stay silent."""
        from train_cog import TrainCog
        cog = _make_cog(TrainCog)
        try:
            interaction = make_mock_interaction()
            role = MagicMock(); role.name = "Leadership"
            interaction.user.roles = [role]
            # /cancel works in any channel, no leadership-channel gate

            await cog.cancel.callback(cog, interaction)

            # It should have replied something
            assert (
                interaction.response.send_message.called
                or interaction.followup.send.called
            )
        finally:
            try:
                cog.check_reminder.cancel()
            except Exception:
                pass


# ── /help — module-level command in bot.py ────────────────────────────────────

# Discord's hard cap on combined embed text (title + description + footer +
# every field name and value). Any single embed that exceeds this fails the
# slash-command call with HTTP 400 (50035) — which is exactly the regression
# this assertion guards against.
DISCORD_EMBED_CHAR_LIMIT = 6000


def _embed_total_chars(embed) -> int:
    parts = [embed.title or "", embed.description or ""]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    for f in embed.fields:
        parts.append(f.name or "")
        parts.append(f.value or "")
    return sum(len(p) for p in parts)


class TestHelpCommand:

    @pytest.mark.asyncio
    async def test_help_replies_with_overview_embed_and_view(self, seeded_db):
        """`/help` should reply with the overview embed + a HelpView with
        a category dropdown."""
        import bot as bot_module
        import premium
        from help_content import HelpView
        premium.clear_cache()

        help_cmd = bot_module.bot.tree.get_command("help")
        assert help_cmd is not None, "bot.tree has no /help command"

        interaction = make_mock_interaction(is_admin=True)
        interaction.entitlements = []

        await help_cmd.callback(interaction)

        # send_message was called with embed= and view= — pull both.
        send = interaction.response.send_message
        assert send.called, "/help should call response.send_message"
        kwargs = send.call_args.kwargs
        embed = kwargs.get("embed")
        view = kwargs.get("view")
        assert embed is not None, "/help should reply with an embed"
        assert "Commands" in (embed.title or "")
        assert isinstance(view, HelpView), "/help should attach a HelpView"
        assert _embed_total_chars(embed) < DISCORD_EMBED_CHAR_LIMIT

    def test_every_category_embed_fits_discord_limit(self):
        """Every category embed (free + premium tier) must stay under the
        6000-char cap so a future content edit can't silently break /help."""
        from help_content import HELP_CATEGORIES, build_category_embed, build_overview_embed

        for is_premium in (False, True):
            overview = build_overview_embed(is_premium)
            assert _embed_total_chars(overview) < DISCORD_EMBED_CHAR_LIMIT, (
                f"Overview embed exceeds {DISCORD_EMBED_CHAR_LIMIT} "
                f"(is_premium={is_premium})"
            )
            for cat_id in HELP_CATEGORIES:
                embed = build_category_embed(cat_id, is_premium)
                size = _embed_total_chars(embed)
                assert size < DISCORD_EMBED_CHAR_LIMIT, (
                    f"Category '{cat_id}' embed is {size} chars "
                    f"(is_premium={is_premium}); Discord limit is {DISCORD_EMBED_CHAR_LIMIT}"
                )

    @pytest.mark.asyncio
    async def test_help_dropdown_swaps_in_category_embed(self, seeded_db):
        """Selecting a category in the dropdown should edit the message
        with the category embed."""
        from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch
        from help_content import HelpView, HELP_CATEGORIES

        view = HelpView(is_premium=False, origin=None)
        select = view.children[0]

        # Pick the first real category (not the Overview sentinel).
        first_cat = next(iter(HELP_CATEGORIES))

        select_interaction = MagicMock()
        select_interaction.response = MagicMock()
        select_interaction.response.edit_message = AsyncMock()

        # Select.values is a read-only property; patch at the class.
        with patch.object(type(select), "values",
                          new_callable=PropertyMock) as mock_values:
            mock_values.return_value = [first_cat]
            await select.callback(select_interaction)

        select_interaction.response.edit_message.assert_called_once()
        kwargs = select_interaction.response.edit_message.call_args.kwargs
        embed = kwargs.get("embed")
        assert embed is not None
        assert HELP_CATEGORIES[first_cat]["label"] in (embed.title or "")

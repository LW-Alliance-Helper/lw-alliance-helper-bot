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
    OGV_GUILD_ID,
    make_mock_interaction,
    make_mock_user,
    make_mock_channel,
    make_mock_guild,
)


# ── Expected registered commands per cog ──────────────────────────────────────

EXPECTED_COG_COMMANDS = {
    "SetupCog": {
        "setup", "view_configuration", "setup_reset",
        "setup_train", "setup_growth", "setup_birthdays",
        "setup_desertstorm", "setup_canyonstorm",
        "setup_events", "setup_survey",
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


# Helper: build an interaction in the leadership channel with the
# leadership role on a fully-set-up guild.
def _make_leadership_interaction(guild_id=TEST_GUILD_ID):
    interaction = make_mock_interaction(guild_id=guild_id, is_admin=True)
    role = MagicMock()
    role.name = "Leadership"
    interaction.user.roles = [role]
    interaction.channel.category_id = 0  # leadership_category_id default
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
        "setup_events", "setup_survey",
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


# ── Storm + LogCog command gates (leadership channel + role) ──────────────────

class TestStormCommandsGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "desertstorm_draft", "canyonstorm_draft",
        "desertstorm", "canyonstorm",
    ])
    async def test_rejects_caller_outside_leadership_channel(self, seeded_db, command_name):
        from storm import StormCog
        cog = _make_cog(StormCog)

        interaction = make_mock_interaction()
        # Channel category != leadership_category_id => guard fails
        interaction.channel.category_id = 99999999
        role = MagicMock(); role.name = "Leadership"
        interaction.user.roles = [role]

        cmd = getattr(cog, command_name)
        await cmd.callback(cog, interaction)

        content, _ = _last_message(interaction)
        assert "leadership channel" in (content or "").lower()


class TestLogCommandsGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "desertstorm_participation", "canyonstorm_participation",
        "desertstorm_log", "canyonstorm_log",
        "desertstorm_remind", "canyonstorm_remind",
    ])
    async def test_rejects_caller_outside_leadership_channel(self, seeded_db, command_name):
        from storm_log import LogCog
        cog = _make_cog(LogCog)

        interaction = make_mock_interaction()
        interaction.channel.category_id = 99999999
        role = MagicMock(); role.name = "Leadership"
        interaction.user.roles = [role]

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
        assert "leadership channel" in (content or "").lower()


# ── Survey commands ───────────────────────────────────────────────────────────

class TestSurveyCommandsGate:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command_name", [
        "survey_post", "survey", "survey_remind",
    ])
    async def test_rejects_caller_outside_leadership_channel(self, seeded_db, command_name):
        from survey import SurveyCog
        cog = _make_cog(SurveyCog)
        try:
            interaction = make_mock_interaction()
            interaction.channel.category_id = 99999999
            role = MagicMock(); role.name = "Leadership"
            interaction.user.roles = [role]

            cmd = getattr(cog, command_name)
            await cmd.callback(cog, interaction)

            content, _ = _last_message(interaction)
            assert "leadership channel" in (content or "").lower()
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
    async def test_rejects_caller_outside_leadership_channel(self, seeded_db, command_name):
        from train_cog import TrainCog
        cog = _make_cog(TrainCog)
        try:
            interaction = make_mock_interaction()
            interaction.channel.category_id = 99999999
            role = MagicMock(); role.name = "Leadership"
            interaction.user.roles = [role]

            cmd = getattr(cog, command_name)
            try:
                await cmd.callback(cog, interaction)
            except TypeError:
                await cmd.callback(cog, interaction, None)  # /train_log [date]

            content, _ = _last_message(interaction)
            assert "leadership channel" in (content or "").lower()
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

class TestHelpCommand:

    @pytest.mark.asyncio
    async def test_help_replies_with_embed(self, seeded_db):
        """`/help` from bot.py should always succeed and return an embed
        listing every section."""
        import bot as bot_module
        import premium
        premium.clear_cache()

        # /help is registered as a Command on bot.tree. Pull it by name.
        help_cmd = bot_module.bot.tree.get_command("help")
        assert help_cmd is not None, "bot.tree has no /help command"

        interaction = make_mock_interaction(is_admin=True)
        interaction.entitlements = []

        await help_cmd.callback(interaction)

        _, embed = _last_message(interaction)
        assert embed is not None, "/help should reply with an embed"
        assert "Commands" in (embed.title or "")

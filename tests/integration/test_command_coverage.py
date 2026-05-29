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
from discord import app_commands

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
        # Post-#201: the 12 individual /setup_* commands collapsed into
        # a single /setup button hub (setup_hub.py). All wizard handlers
        # remain at their existing entry points and are dispatched into
        # by the hub buttons.
        "setup",
    },
    "SurveyCog": {
        # Top-level: just /survey group. Subcommands (overview / post
        # / remind) are introspected separately.
        "survey",
    },
    "TrainCog": {
        # Top-level: /train group + the standalone /birthdays
        # (member-facing list) and /cancel (wizard exit). Subcommands
        # of /train (overview / log / birthdays) are introspected
        # separately.
        "train",
        "birthdays",
        "cancel",
    },
    "MemberRosterCog": {
        # Post-#195 + #201: just the /members group (overview / sync).
        # The pre-#201 /setup_members slash command has collapsed into
        # the /setup hub's `👥 Members` button.
        "members",
    },
    "DonateCog": {
        # Top-level commands: /donate, /upgrade, and the /premium group.
        # Subcommands of the /premium group are introspected separately
        # in `test_donate_cog_premium_group_has_expected_subcommands`.
        "donate",
        "upgrade",
        "premium",
    },
    "ExportImportCog": {
        # Top-level: /config group. Subcommands (overview / export /
        # import) are introspected in `test_export_import_cog_config_group_has_expected_subcommands`.
        "config",
    },
}


# Storm commands consolidated under `/desertstorm` and `/canyonstorm` parent
# groups (#143). The root cog owns both groups and dispatches into the
# per-feature modules; nothing else registers storm slash commands.
# Post-#187: /desertstorm and /canyonstorm are single top-level
# commands (no subcommand tree). Every storm action lives behind a
# button in the event-hub view (storm_event_hub.py).
EXPECTED_STORM_TOP_LEVEL_COMMANDS = {"desertstorm", "canyonstorm"}

# Module-level slash commands defined directly in bot.py (not on a cog).
# `/events` is now an `app_commands.Group` with overview / show / log
# subcommands (#197). `/admin` is also a Group but scoped via
# `BOT_ADMIN_GUILD_IDS`, so it doesn't appear in the global tree under
# the production registration — exercised separately in
# `tests/unit/test_guild_install_metadata.py`.
EXPECTED_MODULE_COMMANDS = {"growth", "events", "help"}
EXPECTED_GROWTH_SUBCOMMANDS = {"overview", "breakdown"}


# ── Cog instantiation helpers ─────────────────────────────────────────────────


def _make_cog(cog_class):
    """Instantiate a cog with a MagicMock bot and a no-op task loop."""
    bot = MagicMock()
    bot.add_view = MagicMock()
    bot.add_dynamic_items = MagicMock()
    return cog_class(bot)


def _commands_on(cog) -> set[str]:
    """All top-level slash command names registered on a cog instance.

    Covers both `@app_commands.command` leaves and `app_commands.Group`
    class attributes — Groups appear in the slash picker as a single
    top-level entry. Subcommands of a Group also surface as class
    attributes (with `.parent = the_group`), but they're NOT top-level
    commands; filter them out and let each cog's dedicated test inspect
    the Group's `.commands` directly."""
    out: set[str] = set()
    import discord.app_commands as _ac

    for attr_name in dir(cog):
        attr = getattr(cog, attr_name, None)
        if isinstance(attr, (_ac.Command, _ac.Group)):
            if getattr(attr, "parent", None) is not None:
                continue  # sub-command of a Group — surfaced via that Group
            out.add(attr.name)
    return out


def _subcommands_on(group) -> set[str]:
    """Names of the subcommands attached to a given app_commands.Group."""
    return {c.name for c in group.commands}


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

    def test_storm_commands_root_cog_registers_two_top_level_commands(self, seeded_db):
        """Post-#187: `/desertstorm` and `/canyonstorm` are single
        top-level slash commands, not parent groups. Each opens the
        event-hub view. Every action that used to be a subcommand is
        now a button on the hub view."""
        from storm_commands_root import StormCommandsRootCog

        cog = _make_cog(StormCommandsRootCog)
        # The cog stashes the registered Command objects on instance
        # attributes so the test can find them without poking through
        # `bot.tree`.
        assert cog.desertstorm_cmd.name == "desertstorm"
        assert cog.canyonstorm_cmd.name == "canyonstorm"
        # Neither command has any subcommands.
        for cmd in (cog.desertstorm_cmd, cog.canyonstorm_cmd):
            # Commands are not Groups; they don't expose a `.commands`
            # property. Defensive check: if the cog accidentally
            # registered a Group, this would either fail at construction
            # or expose subcommands here.
            assert not isinstance(cmd, app_commands.Group), (
                f"/{cmd.name} should be a single command after #187, got a Group instead."
            )

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
    async def test_survey_cog_survey_group_has_expected_subcommands(self, seeded_db):
        """/survey is a top-level Group containing overview / post /
        remind."""
        from survey import SurveyCog

        cog = _make_cog(SurveyCog)
        try:
            assert _subcommands_on(cog.survey_group) == {
                "overview",
                "post",
                "remind",
            }
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

    @pytest.mark.asyncio
    async def test_train_cog_train_group_has_expected_subcommands(self, seeded_db):
        """/train is a top-level Group containing overview / log /
        birthdays. /birthdays (standalone member-facing list) and
        /cancel stay top-level and aren't part of the group."""
        from train_cog import TrainCog

        cog = _make_cog(TrainCog)
        try:
            assert _subcommands_on(cog.train_group) == {
                "overview",
                "log",
                "birthdays",
            }
        finally:
            try:
                cog.check_reminder.cancel()
            except Exception:
                pass

    def test_member_roster_cog_registers_expected_commands(self, seeded_db):
        from member_roster import MemberRosterCog

        cog = _make_cog(MemberRosterCog)
        assert _commands_on(cog) == EXPECTED_COG_COMMANDS["MemberRosterCog"]

    def test_member_roster_cog_members_group_has_expected_subcommands(self, seeded_db):
        """/members is a top-level Group containing overview / sync.
        Subcommands are introspected here rather than via `_commands_on`."""
        from member_roster import MemberRosterCog

        cog = _make_cog(MemberRosterCog)
        assert _subcommands_on(cog.members_group) == {
            "overview",
            "sync",
        }

    def test_donate_cog_registers_expected_commands(self, seeded_db):
        from donate import DonateCog

        cog = _make_cog(DonateCog)
        assert _commands_on(cog) == EXPECTED_COG_COMMANDS["DonateCog"]

    def test_donate_cog_premium_group_has_expected_subcommands(self, seeded_db):
        """/premium is a top-level Group containing overview / assign /
        unassign — split out from `_commands_on` because Groups surface
        as a single top-level entry, with subcommands hanging off the
        Group's `.commands` property."""
        from donate import DonateCog

        cog = _make_cog(DonateCog)
        assert _subcommands_on(cog.premium_group) == {
            "overview",
            "assign",
            "unassign",
        }

    def test_export_import_cog_registers_expected_commands(self, seeded_db):
        from export_import_cog import ExportImportCog

        cog = _make_cog(ExportImportCog)
        assert _commands_on(cog) == EXPECTED_COG_COMMANDS["ExportImportCog"]

    def test_export_import_cog_config_group_has_expected_subcommands(self, seeded_db):
        """/config is a top-level Group containing overview / export /
        import — the data-portability hub. Subcommands are introspected
        here rather than via `_commands_on`."""
        from export_import_cog import ExportImportCog

        cog = _make_cog(ExportImportCog)
        assert _subcommands_on(cog.config_group) == {
            "overview",
            "export",
            "import",
        }

    def test_module_level_commands_registered_on_bot_tree(self, seeded_db):
        """bot.py defines a handful of commands directly on `bot.tree`
        rather than via a cog. Walk the registered command tree and
        confirm every expected command is present."""
        import bot as bot_module

        # CommandTree.get_commands() returns a list[Command] for the
        # global scope. Map by name and assert membership.
        registered = {c.name: c for c in bot_module.bot.tree.get_commands()}
        for name in EXPECTED_MODULE_COMMANDS:
            assert name in registered, (
                f"bot.py's command tree is missing /{name}. "
                f"Registered commands: {sorted(registered)}"
            )

        # Post-#249: /events is now a single hub command (no subcommands).
        # The pre-hub `/events overview|show|log` group was replaced by
        # the in-hub buttons.
        events_cmd = registered.get("events")
        assert events_cmd is not None
        assert not hasattr(events_cmd, "commands"), "/events should be a flat command, not a Group"

        # /growth is still a Group post-#200.
        growth_grp = registered.get("growth")
        assert growth_grp is not None
        growth_subs = _subcommands_on(growth_grp)
        assert growth_subs == EXPECTED_GROWTH_SUBCOMMANDS, (
            f"/growth subcommands mismatch: got {growth_subs}, "
            f"expected {EXPECTED_GROWTH_SUBCOMMANDS}"
        )

    @pytest.mark.asyncio
    async def test_no_unexpected_extra_commands(self, seeded_db):
        """Catch the inverse: a command that exists on a cog but isn't in
        our expected set (e.g. someone added /foo without updating docs).
        Async because SurveyCog/TrainCog start tasks.loops at construction.
        Storm is exercised in the parent-group test above — the root cog
        doesn't surface its commands as top-level attributes the way the
        per-feature cogs used to."""
        from setup_cog import SetupCog
        from survey import SurveyCog
        from train_cog import TrainCog
        from member_roster import MemberRosterCog
        from donate import DonateCog
        from export_import_cog import ExportImportCog

        for cog_class in (
            SetupCog,
            SurveyCog,
            TrainCog,
            MemberRosterCog,
            DonateCog,
            ExportImportCog,
        ):
            cog = _make_cog(cog_class)
            expected = EXPECTED_COG_COMMANDS[cog_class.__name__]
            actual = _commands_on(cog)
            extra = actual - expected
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
    interaction.user.roles = []  # no leadership role
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
    for sender in (interaction.response.send_message, interaction.followup.send):
        if sender.call_args:
            args, kwargs = sender.call_args
            content = args[0] if args else kwargs.get("content") or ""
            embed = kwargs.get("embed")
            return (content, embed)
    return ("", None)


# ── /setup hub: leadership-or-admin gates (post-#201) ────────────────────────


class TestSetupHubLaunchersGateNonAdmins:
    """Post-#201: every per-feature wizard the hub dispatches into via
    button callback retains the pre-#201 leadership-or-admin gate. The
    helpers are tested directly here because the buttons themselves
    can't be parametrised without driving real Discord interactions."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "launcher_name",
        [
            "_launch_train_setup",
            "_launch_growth_setup",
            "_launch_birthday_setup",
            "_launch_event_setup",
            "_launch_survey_setup",
            "_launch_shiny_tasks_setup",
        ],
    )
    async def test_launcher_rejects_non_privileged_caller(
        self,
        seeded_db,
        launcher_name,
    ):
        import setup_cog

        launcher = getattr(setup_cog, launcher_name)
        bot = MagicMock()
        interaction = _make_nonprivileged_interaction()
        await launcher(interaction, bot)
        content, _ = _last_message(interaction)
        lowered = (content or "").lower()
        assert "leadership" in lowered or "admin" in lowered, (
            f"{launcher_name} should reject non-privileged caller, got: {content!r}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", ["DS", "CS"])
    async def test_storm_launcher_rejects_non_privileged_caller(
        self,
        seeded_db,
        event_type,
    ):
        import setup_cog

        bot = MagicMock()
        interaction = _make_nonprivileged_interaction()
        await setup_cog._launch_storm_setup(interaction, bot, event_type)
        content, _ = _last_message(interaction)
        lowered = (content or "").lower()
        assert "leadership" in lowered or "admin" in lowered


class TestSetupHubGateNonAdmins:
    """/setup is admin-only — the hub itself + the reset flow both
    reject non-admins."""

    @pytest.mark.asyncio
    async def test_setup_command_rejects_non_admin(self, seeded_db):
        from setup_cog import SetupCog

        cog = _make_cog(SetupCog)
        interaction = make_mock_interaction(is_admin=False)
        await cog.setup.callback(cog, interaction)
        content, _ = _last_message(interaction)
        assert "admin" in (content or "").lower()

    @pytest.mark.asyncio
    async def test_reset_flow_rejects_non_admin(self, seeded_db):
        import setup_cog

        interaction = make_mock_interaction(is_admin=False)
        await setup_cog._run_reset_flow(interaction)
        content, _ = _last_message(interaction)
        assert "admin" in (content or "").lower()


# ── /members group + /setup_members ──────────────────────────────────────────


class TestMemberRosterCommandsGate:
    """Post-#195 + #201: `/sync_members` is now `/members sync` and
    `/setup_members` folded into the setup hub's `👥 Members` button.
    The hub-button dispatch goes through `_launch_member_roster_setup`,
    which retains the same gates as the pre-#201 slash command."""

    @pytest.mark.asyncio
    async def test_members_sync_rejects_non_privileged(self, seeded_db):
        import premium

        premium.clear_cache()

        from member_roster import MemberRosterCog

        cog = _make_cog(MemberRosterCog)

        interaction = _make_nonprivileged_interaction()
        await cog.members_sync.callback(cog, interaction)

        content, _ = _last_message(interaction)
        lowered = (content or "").lower()
        assert "leadership" in lowered or "admin" in lowered

    @pytest.mark.asyncio
    async def test_setup_members_launcher_rejects_non_privileged(self, seeded_db):
        import premium, member_roster

        premium.clear_cache()
        bot = MagicMock()

        interaction = _make_nonprivileged_interaction()
        await member_roster._launch_member_roster_setup(interaction, bot)

        content, _ = _last_message(interaction)
        lowered = (content or "").lower()
        assert "leadership" in lowered or "admin" in lowered

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    async def test_members_sync_premium_locked_for_free_admin(self, seeded_db):
        """An admin on a free guild gets the premium-locked embed."""
        import premium

        premium.clear_cache()

        from member_roster import MemberRosterCog

        cog = _make_cog(MemberRosterCog)

        interaction = make_mock_interaction(is_admin=True)
        # Free guild — no entitlements
        interaction.entitlements = []
        await cog.members_sync.callback(cog, interaction)

        _, embed = _last_message(interaction)
        assert embed is not None, (
            "cog.members_sync on free tier should show the premium-locked embed"
        )
        assert "Premium" in (embed.title or "")


# ── Storm command gates (under the consolidated parent groups) ──────────────


def _resolve_storm_top_level(name: str):
    """Return the callback of the `/desertstorm` or `/canyonstorm`
    top-level command (post-#187: each is a single Command, no Group)."""
    from storm_commands_root import StormCommandsRootCog

    cog = _make_cog(StormCommandsRootCog)
    cmd = cog.desertstorm_cmd if name == "desertstorm" else cog.canyonstorm_cmd
    return cmd.callback


class TestStormCommandsGate:
    """Post-#187: `/desertstorm` and `/canyonstorm` are single top-level
    commands that open the event-hub view (storm_event_hub.handle_event_hub).
    The hub itself runs the leadership-or-admin check before rendering
    any buttons. These tests confirm the gate fires for non-leadership
    callers."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["desertstorm", "canyonstorm"])
    async def test_hub_rejects_caller_without_leadership_role(
        self,
        seeded_db,
        name,
    ):
        callback = _resolve_storm_top_level(name)
        interaction = make_mock_interaction()
        interaction.user.roles = []  # no leadership role

        await callback(interaction)

        content, _ = _last_message(interaction)
        assert "leadership" in (content or "").lower(), (
            f"/{name} should reject non-leadership caller, got: {content!r}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", ["DS", "CS"])
    async def test_strategy_list_helper_rejects_non_leadership(
        self,
        seeded_db,
        event_type,
    ):
        """The hub's `🧮 Manage strategy presets` button calls
        `open_strategy_list`. Confirm that path enforces the same
        leadership gate the legacy `/<event> strategy list` subcommand
        did."""
        from storm_strategy import open_strategy_list

        interaction = make_mock_interaction()
        interaction.user.roles = []
        await open_strategy_list(interaction, event_type)
        content, _ = _last_message(interaction)
        assert "leadership" in (content or "").lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("event_type", ["DS", "CS"])
    async def test_member_rule_list_helper_rejects_non_leadership(
        self,
        seeded_db,
        event_type,
    ):
        """Same idea for the `👤 Manage member rules` button."""
        from storm_member_rules import open_member_rule_list

        interaction = make_mock_interaction()
        interaction.user.roles = []
        await open_member_rule_list(interaction, event_type, member_filter=None)
        content, _ = _last_message(interaction)
        assert "leadership" in (content or "").lower()


# ── Survey commands ───────────────────────────────────────────────────────────


class TestSurveyCommandsGate:
    """Post-#199: survey commands live under the /survey group as
    `survey_overview`, `survey_post`, and `survey_remind` on the cog."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command_attr",
        [
            "survey_post",
            "survey_overview",
            "survey_remind",
        ],
    )
    async def test_rejects_caller_without_leadership_role(self, seeded_db, command_attr):
        from survey import SurveyCog

        cog = _make_cog(SurveyCog)
        try:
            interaction = make_mock_interaction()
            interaction.user.roles = []  # no leadership role

            cmd = getattr(cog, command_attr)
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
    """Post-#198: the train commands live under the /train group as
    `train_overview`, `train_log`, and `train_birthdays` on the cog;
    /birthdays remains a standalone top-level command (member-facing
    list of upcoming birthdays)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command_attr",
        [
            "train_overview",
            "train_log",
            "train_birthdays",
            "birthdays",
        ],
    )
    async def test_rejects_caller_without_leadership_role(self, seeded_db, command_attr):
        from train_cog import TrainCog

        cog = _make_cog(TrainCog)
        try:
            interaction = make_mock_interaction()
            interaction.user.roles = []  # no leadership role

            cmd = getattr(cog, command_attr)
            try:
                await cmd.callback(cog, interaction)
            except TypeError:
                await cmd.callback(cog, interaction, None)  # /train log [date]

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
            role = MagicMock()
            role.name = "Leadership"
            interaction.user.roles = [role]
            # /cancel works in any channel, no leadership-channel gate

            await cog.cancel.callback(cog, interaction)

            # It should have replied something
            assert interaction.response.send_message.called or interaction.followup.send.called
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
                f"Overview embed exceeds {DISCORD_EMBED_CHAR_LIMIT} (is_premium={is_premium})"
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
        with patch.object(type(select), "values", new_callable=PropertyMock) as mock_values:
            mock_values.return_value = [first_cat]
            await select.callback(select_interaction)

        select_interaction.response.edit_message.assert_called_once()
        kwargs = select_interaction.response.edit_message.call_args.kwargs
        embed = kwargs.get("embed")
        assert embed is not None
        assert HELP_CATEGORIES[first_cat]["label"] in (embed.title or "")

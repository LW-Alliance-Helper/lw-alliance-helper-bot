"""
Tests for the user-facing error-messaging surface:

  * `bot._format_command_error(error, event_id)` — categorises common
    Discord exceptions and returns an actionable user-facing message
    that includes a Sentry event-id reference for ticket reports.
  * `setup_cog._missing_wizard_perms(interaction)` — returns the list
    of human-readable Discord permission names the bot is missing in
    the interaction's channel. Empty list = the bot can drive a wizard.
  * `setup_cog._check_wizard_can_run(interaction, command_name)` —
    if the bot has the perms, returns True. Otherwise sends an
    ephemeral message describing exactly what's missing and how to
    fix it, and returns False.

These were added so that:
  - Misconfigured channels (the most common cause of slash-command
    failures in production) get a clear, actionable message instead
    of "Something went wrong."
  - When a real bug does fire, the user gets a Sentry event id they
    can paste into a ticket so support can correlate to the dashboard.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# We need bot.py imported but it requires a DISCORD_TOKEN to be set on
# import. The value doesn't have to be valid; we only call helper
# functions, never connect to Discord.
os.environ.setdefault("DISCORD_TOKEN", "fake-token-for-tests")


# ── _format_command_error: categorisation ──────────────────────────────────────


class TestFormatCommandError:
    """Each Discord exception subtype produces a message tailored to its
    cause; the Sentry event id (when available) is appended as a
    `Reference:` line so tickets correlate to the dashboard."""

    def _make_forbidden(self, code: int) -> discord.Forbidden:
        # discord.Forbidden requires a Response and the parsed payload.
        resp = MagicMock()
        resp.status = 403
        return discord.Forbidden(resp, {"message": "Test", "code": code})

    def _make_not_found(self, code: int = 10003) -> discord.NotFound:
        resp = MagicMock()
        resp.status = 404
        return discord.NotFound(resp, {"message": "Unknown Channel", "code": code})

    def _make_http_exception(self, status: int = 500, code: int = 0) -> discord.HTTPException:
        resp = MagicMock()
        resp.status = status
        return discord.HTTPException(resp, {"message": "Internal Error", "code": code})

    def test_forbidden_50001_explains_missing_access(self):
        from bot import _format_command_error

        err = self._make_forbidden(50001)
        msg = _format_command_error(err, "abc123")
        assert "I don't have access to this channel" in msg
        assert "Send Messages" in msg
        assert "leadership channel" in msg.lower()
        assert "`abc123`" in msg

    def test_forbidden_50013_lists_required_permissions(self):
        from bot import _format_command_error

        err = self._make_forbidden(50013)
        msg = _format_command_error(err, "abc123")
        assert "missing a Discord permission" in msg
        assert "Send Messages" in msg
        assert "Embed Links" in msg
        assert "View Channel" in msg
        assert "Read Message History" in msg

    def test_other_forbidden_surfaces_code(self):
        from bot import _format_command_error

        err = self._make_forbidden(99999)
        msg = _format_command_error(err, "abc123")
        assert "Discord blocked" in msg
        assert "99999" in msg

    def test_not_found_suggests_setup_view_configuration_button(self):
        """Post-#201: /view_configuration folded into the /setup hub's
        🗂️ View configuration button — the NotFound rescue copy now
        points users at that nav path instead of the bare slash."""
        from bot import _format_command_error

        err = self._make_not_found()
        msg = _format_command_error(err, "abc123")
        assert "Discord couldn't find" in msg
        assert "/setup" in msg
        assert "View configuration" in msg

    def test_http_exception_includes_status_and_code(self):
        from bot import _format_command_error

        err = self._make_http_exception(status=502, code=0)
        msg = _format_command_error(err, "abc123")
        assert "Discord's API returned an error" in msg
        assert "502" in msg

    def test_generic_error_says_bug_not_config(self):
        from bot import _format_command_error

        err = RuntimeError("something blew up")
        msg = _format_command_error(err, "abc123")
        assert "looks like a bug on my side" in msg

    def test_event_id_omitted_when_none(self):
        """Sentry not initialised → capture_exception() returns None →
        the formatter must not append an empty `Reference:` line."""
        from bot import _format_command_error

        err = RuntimeError("x")
        msg = _format_command_error(err, None)
        assert "Reference" not in msg

    def test_every_message_links_to_issue_tracker(self):
        """Every error category should link the user to the issue tracker
        so ticket reporting is one click away."""
        from bot import _format_command_error, ISSUE_TRACKER_URL

        cases = [
            self._make_forbidden(50001),
            self._make_forbidden(50013),
            self._make_forbidden(99999),
            self._make_not_found(),
            self._make_http_exception(),
            RuntimeError("generic"),
        ]
        for err in cases:
            msg = _format_command_error(err, "abc123")
            assert ISSUE_TRACKER_URL in msg, f"Missing tracker link for {type(err).__name__}"


# ── _missing_wizard_perms / _check_wizard_can_run ──────────────────────────────


class TestMissingWizardPerms:
    """Returns the list of human-readable perms the bot is missing in
    `interaction.channel`."""

    def _make_interaction(self, **perm_overrides) -> MagicMock:
        """An interaction whose channel.permissions_for(bot) returns a
        Permissions-like object. `perm_overrides` lets tests set specific
        perms to False; everything else defaults to True."""
        permissions = MagicMock()
        permissions.view_channel = perm_overrides.get("view_channel", True)
        permissions.send_messages = perm_overrides.get("send_messages", True)
        permissions.embed_links = perm_overrides.get("embed_links", True)
        permissions.read_message_history = perm_overrides.get("read_message_history", True)

        channel = MagicMock()
        channel.permissions_for = MagicMock(return_value=permissions)

        guild = MagicMock()
        guild.me = MagicMock()  # a non-None Member-like object

        interaction = MagicMock()
        interaction.guild = guild
        interaction.channel = channel
        return interaction

    def test_all_perms_present_returns_empty(self):
        from setup_cog import _missing_wizard_perms

        assert _missing_wizard_perms(self._make_interaction()) == []

    def test_missing_send_messages_listed_in_human_form(self):
        from setup_cog import _missing_wizard_perms

        out = _missing_wizard_perms(self._make_interaction(send_messages=False))
        assert out == ["Send Messages"]

    def test_multiple_missing_returned_in_required_order(self):
        from setup_cog import _missing_wizard_perms

        out = _missing_wizard_perms(
            self._make_interaction(
                send_messages=False,
                embed_links=False,
            )
        )
        # _WIZARD_REQUIRED_PERMS order: view_channel, send_messages, embed_links, read_message_history
        assert out == ["Send Messages", "Embed Links"]

    def test_dm_context_returns_empty(self):
        """DM interactions have guild = None; not a relevant context for
        wizard perms checks, so return empty (no error message)."""
        from setup_cog import _missing_wizard_perms

        interaction = self._make_interaction()
        interaction.guild = None
        assert _missing_wizard_perms(interaction) == []


class TestCheckWizardCanRun:
    """When perms are present, returns True without sending anything.
    When perms are missing, sends an ephemeral message with the missing
    perms list and returns False."""

    def _make_interaction(self, *, response_done=False, **perm_overrides) -> MagicMock:
        permissions = MagicMock()
        permissions.view_channel = perm_overrides.get("view_channel", True)
        permissions.send_messages = perm_overrides.get("send_messages", True)
        permissions.embed_links = perm_overrides.get("embed_links", True)
        permissions.read_message_history = perm_overrides.get("read_message_history", True)

        channel = MagicMock()
        channel.permissions_for = MagicMock(return_value=permissions)
        channel.mention = "<#999>"

        guild = MagicMock()
        guild.me = MagicMock()

        interaction = MagicMock()
        interaction.guild = guild
        interaction.channel = channel
        interaction.response.is_done = MagicMock(return_value=response_done)
        interaction.response.send_message = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    @pytest.mark.asyncio
    async def test_returns_true_when_perms_ok(self):
        from setup_cog import _check_wizard_can_run

        interaction = self._make_interaction()
        ok = await _check_wizard_can_run(interaction, "setup_train")
        assert ok is True
        # Nothing sent.
        interaction.response.send_message.assert_not_called()
        interaction.followup.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_and_sends_ephemeral_when_missing(self):
        from setup_cog import _check_wizard_can_run

        interaction = self._make_interaction(send_messages=False)
        ok = await _check_wizard_can_run(interaction, "setup_train")
        assert ok is False

        # Should send ephemeral via response.send_message (not followup)
        # since the response wasn't done yet.
        interaction.response.send_message.assert_called_once()
        call = interaction.response.send_message.call_args
        msg = call.args[0] if call.args else call.kwargs["content"]
        assert "Send Messages" in msg
        assert "/setup_train" in msg
        assert "<#999>" in msg  # uses channel.mention
        assert call.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_uses_followup_when_response_already_done(self):
        """If the slash command already responded (e.g. with a defer or
        intermediate message), the pre-check should use followup.send."""
        from setup_cog import _check_wizard_can_run

        interaction = self._make_interaction(response_done=True, send_messages=False)
        ok = await _check_wizard_can_run(interaction, "setup_events")
        assert ok is False
        interaction.followup.send.assert_called_once()
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_lists_all_missing_perms(self):
        from setup_cog import _check_wizard_can_run

        interaction = self._make_interaction(
            send_messages=False,
            embed_links=False,
            view_channel=False,
        )
        ok = await _check_wizard_can_run(interaction, "setup_train")
        assert ok is False
        msg = interaction.response.send_message.call_args.args[0]
        assert "Send Messages" in msg
        assert "Embed Links" in msg
        assert "View Channel" in msg

    @pytest.mark.asyncio
    async def test_message_offers_two_remediation_paths(self):
        """The error tells the user what to fix AND how to work around
        it (run from a different channel)."""
        from setup_cog import _check_wizard_can_run

        interaction = self._make_interaction(send_messages=False)
        await _check_wizard_can_run(interaction, "setup_train")
        msg = interaction.response.send_message.call_args.args[0]
        # Both remediation paths should be mentioned.
        assert "Edit this channel's permissions" in msg
        assert "Run `/setup_train` from a channel" in msg

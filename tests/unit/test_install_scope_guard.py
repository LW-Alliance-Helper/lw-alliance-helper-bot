"""
Tests for `bot.reject_commands_only_install` — the global tree check that
refuses commands from guilds where the app's commands are installed but
the bot user never joined.

That state comes from a guild install granting `applications.commands`
without the `bot` scope. Those guilds are invisible to `len(bot.guilds)`
and every gate downstream misreports them: `guard` answers NOT_SET_UP,
telling leadership to run `/setup` for a bot that was never in the server.

These tests pin both directions. The rejection path matters because it's
the only recovery route those alliances have, and the three allow cases
matter more: a false positive here tells a correctly-installed alliance
that the bot isn't there, on every command they run.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from tests.constants import TEST_GUILD_ID


def _interaction(guild_id):
    """Minimal interaction double: the check only reads `guild_id` and
    replies through `response.send_message`."""
    inter = MagicMock()
    inter.guild_id = guild_id
    inter.response.send_message = AsyncMock()
    return inter


# ── Allowed: the bot really is in the guild ──────────────────────────────────


async def test_allows_when_bot_is_a_member():
    """The normal case for every properly-installed alliance."""
    import bot

    inter = _interaction(TEST_GUILD_ID)
    with (
        patch.object(bot.bot, "is_ready", return_value=True),
        patch.object(bot.bot, "get_guild", return_value=MagicMock()),
    ):
        assert await bot.reject_commands_only_install(inter) is True

    inter.response.send_message.assert_not_awaited()


async def test_allows_before_ready():
    """The guild cache is still filling during startup, so an empty
    lookup proves nothing. Must not accuse a real install."""
    import bot

    inter = _interaction(TEST_GUILD_ID)
    with (
        patch.object(bot.bot, "is_ready", return_value=False),
        patch.object(bot.bot, "get_guild", return_value=None),
    ):
        assert await bot.reject_commands_only_install(inter) is True

    inter.response.send_message.assert_not_awaited()


async def test_allows_when_there_is_no_guild():
    """No guild_id means there's no install to be wrong about."""
    import bot

    inter = _interaction(None)
    with (
        patch.object(bot.bot, "is_ready", return_value=True),
        patch.object(bot.bot, "get_guild", return_value=None),
    ):
        assert await bot.reject_commands_only_install(inter) is True

    inter.response.send_message.assert_not_awaited()


# ── Refused: commands installed, bot absent ──────────────────────────────────


async def test_rejects_and_explains_when_bot_is_not_a_member():
    import bot
    from messages import BOT_NOT_IN_GUILD

    inter = _interaction(TEST_GUILD_ID)
    with (
        patch.object(bot.bot, "is_ready", return_value=True),
        patch.object(bot.bot, "get_guild", return_value=None),
    ):
        assert await bot.reject_commands_only_install(inter) is False

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.call_args
    assert args[0] == BOT_NOT_IN_GUILD
    assert kwargs["ephemeral"] is True


async def test_still_rejects_when_the_reply_fails():
    """An expired interaction token must not let the command through."""
    import bot

    inter = _interaction(TEST_GUILD_ID)
    inter.response.send_message = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "unknown interaction")
    )
    with (
        patch.object(bot.bot, "is_ready", return_value=True),
        patch.object(bot.bot, "get_guild", return_value=None),
    ):
        assert await bot.reject_commands_only_install(inter) is False


# ── Wiring + copy ────────────────────────────────────────────────────────────


def test_check_is_wired_to_the_command_tree():
    """The check is only useful if the tree actually calls it. This is
    the piece a refactor would silently drop."""
    import bot

    assert bot.bot.tree.interaction_check is bot.reject_commands_only_install


def test_notice_carries_the_recovery_link():
    """The re-invite link is the entire point of the message — these
    alliances have no other route back."""
    from messages import BOT_NOT_IN_GUILD

    assert "lw-alliance-helper.github.io/setup.html" in BOT_NOT_IN_GUILD
    assert "Manage Server" in BOT_NOT_IN_GUILD

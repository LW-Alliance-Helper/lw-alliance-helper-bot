"""
Unit tests for dm.py — premium-gated DM helpers used by birthday, train,
survey-reminder, and storm-reminder flows.

Each helper:
  * Returns False/early when the guild isn't premium (no DM is attempted).
  * Swallows discord.Forbidden (closed DMs) and other exceptions silently.
  * Otherwise awaits user.send and returns True.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID
from config import OGV_GUILD_ID


# ── send_dm_to_id ─────────────────────────────────────────────────────────────

class TestSendDmToId:

    @pytest.mark.asyncio
    async def test_returns_false_for_free_tier(self):
        import dm
        bot = MagicMock()
        ok  = await dm.send_dm_to_id(bot, TEST_GUILD_ID, 123, content="hi")
        assert ok is False

    @pytest.mark.asyncio
    async def test_sends_to_user_for_premium_guild(self):
        import dm

        user      = AsyncMock()
        user.send = AsyncMock()
        bot       = MagicMock()
        bot.fetch_user = AsyncMock(return_value=user)

        ok = await dm.send_dm_to_id(bot, OGV_GUILD_ID, 12345, content="hi there")

        assert ok is True
        user.send.assert_awaited_once_with(content="hi there", embed=None)
        bot.fetch_user.assert_awaited_once_with(12345)

    @pytest.mark.asyncio
    async def test_returns_false_when_user_closed_dms(self):
        import dm

        user      = AsyncMock()
        user.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "DMs closed"))
        bot       = MagicMock()
        bot.fetch_user = AsyncMock(return_value=user)

        ok = await dm.send_dm_to_id(bot, OGV_GUILD_ID, 99, content="hi")
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_when_user_not_found(self):
        import dm

        bot = MagicMock()
        bot.fetch_user = AsyncMock(side_effect=discord.NotFound(MagicMock(), "no such user"))

        ok = await dm.send_dm_to_id(bot, OGV_GUILD_ID, 1, content="x")
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_for_invalid_id(self):
        import dm

        bot = MagicMock()
        ok  = await dm.send_dm_to_id(bot, OGV_GUILD_ID, "not-a-number", content="x")
        assert ok is False


# ── send_dm (roster lookup) ───────────────────────────────────────────────────

class TestSendDmByName:

    @pytest.mark.asyncio
    async def test_returns_false_for_free_tier(self):
        import dm
        bot = MagicMock()
        ok  = await dm.send_dm(bot, TEST_GUILD_ID, "Alice", content="hi")
        assert ok is False

    @pytest.mark.asyncio
    async def test_returns_false_when_name_not_in_roster(self, seeded_db):
        import dm

        bot = MagicMock()
        with patch("dm.lookup_discord_id_for_name", return_value=None):
            ok = await dm.send_dm(bot, OGV_GUILD_ID, "Unknown", content="hi")
        assert ok is False

    @pytest.mark.asyncio
    async def test_finds_id_in_roster_and_dms(self, seeded_db):
        import dm

        user      = AsyncMock()
        user.send = AsyncMock()
        bot       = MagicMock()
        bot.fetch_user = AsyncMock(return_value=user)

        with patch("dm.lookup_discord_id_for_name", return_value="555"):
            ok = await dm.send_dm(bot, OGV_GUILD_ID, "Alice", content="hi roster")

        assert ok is True
        user.send.assert_awaited_once_with(content="hi roster", embed=None)


# ── mention_or_name ───────────────────────────────────────────────────────────

class TestMentionOrName:

    @pytest.mark.asyncio
    async def test_free_tier_returns_plain_name(self):
        import dm
        bot = MagicMock()
        out = await dm.mention_or_name(bot, TEST_GUILD_ID, "Alice")
        assert out == "Alice"

    @pytest.mark.asyncio
    async def test_premium_with_roster_returns_mention(self, seeded_db):
        import dm
        bot = MagicMock()
        with patch("dm.lookup_discord_id_for_name", return_value="777"):
            out = await dm.mention_or_name(bot, OGV_GUILD_ID, "Alice")
        assert out == "<@777>"

    @pytest.mark.asyncio
    async def test_premium_without_roster_match_returns_plain_name(self, seeded_db):
        import dm
        bot = MagicMock()
        with patch("dm.lookup_discord_id_for_name", return_value=None):
            out = await dm.mention_or_name(bot, OGV_GUILD_ID, "Stranger")
        assert out == "Stranger"

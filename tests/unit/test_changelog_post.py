"""
Unit tests for changelog_post.maybe_post_changelog (#92).

The bot posts the release changelog itself, from `on_ready`. That hook
fires again on every gateway reconnect and every Railway redeploy, so the
single most important behaviour here is that it doesn't repost — the same
trap that made birthday auto-population re-fire on every deploy (#29).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import discord

import changelog_post
from tests.conftest import TEST_GUILD_ID  # noqa: F401  (pulls in the db fixtures)

BLOCK_FILE = """# preamble

---

**1.9.0** — 2026-09-01
- A thing changed

---

**1.9.1** — 2026-09-02
- A smaller thing changed

---

**1.9.2** — 2026-09-03
NO POST: dependency bumps only
"""


@pytest.fixture
def channel():
    ch = MagicMock()
    ch.send = AsyncMock(return_value=MagicMock(id=555))
    ch.fetch_message = AsyncMock()
    return ch


@pytest.fixture
def bot(channel):
    b = MagicMock()
    b.get_channel = MagicMock(return_value=channel)
    return b


@pytest.fixture
def configured(seeded_db, tmp_path):
    """Changelog channel set and the block file pointed at a temp copy."""
    from config import set_app_setting

    path = tmp_path / "DISCORD_CHANGELOG.md"
    path.write_text(BLOCK_FILE, encoding="utf-8")
    set_app_setting(changelog_post.CHANNEL_SETTING, "999")
    with patch.object(changelog_post, "CHANGELOG_PATH", path):
        yield path


class TestRestartSafety:
    """on_ready runs on every reconnect and redeploy."""

    @pytest.mark.asyncio
    async def test_a_second_boot_on_the_same_version_posts_nothing(self, configured, bot, channel):
        first = await changelog_post.maybe_post_changelog(bot, "1.9.0")
        second = await changelog_post.maybe_post_changelog(bot, "1.9.0")

        assert "posted 1.9.0" in first
        assert "already posted" in second
        channel.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_force_reposts_the_same_version(self, configured, bot, channel):
        await changelog_post.maybe_post_changelog(bot, "1.9.0")
        # A fresh message, not an append, because force is for fixing a
        # botched post rather than continuing a burst.
        with patch.object(changelog_post, "BURST_WINDOW_SECONDS", 0):
            await changelog_post.maybe_post_changelog(bot, "1.9.0", force=True)

        assert channel.send.await_count == 2

    @pytest.mark.asyncio
    async def test_a_version_with_no_block_is_recorded_so_it_stops_retrying(
        self, configured, bot, channel
    ):
        from config import get_app_setting

        result = await changelog_post.maybe_post_changelog(bot, "9.9.9")

        assert "no usable block" in result
        channel.send.assert_not_awaited()
        assert get_app_setting(changelog_post.LAST_VERSION_SETTING) == "9.9.9"

    @pytest.mark.asyncio
    async def test_a_no_post_version_is_recorded_too(self, configured, bot, channel):
        from config import get_app_setting

        result = await changelog_post.maybe_post_changelog(bot, "1.9.2")

        assert changelog_post.NO_POST_MARKER in result
        channel.send.assert_not_awaited()
        assert get_app_setting(changelog_post.LAST_VERSION_SETTING) == "1.9.2"


class TestNotConfigured:
    @pytest.mark.asyncio
    async def test_no_channel_set_does_nothing_and_records_nothing(self, seeded_db, bot, channel):
        from config import get_app_setting

        result = await changelog_post.maybe_post_changelog(bot, "1.9.0")

        assert "no changelog channel" in result
        channel.send.assert_not_awaited()
        # Nothing recorded, so pointing it at a channel later still posts.
        assert not get_app_setting(changelog_post.LAST_VERSION_SETTING)

    @pytest.mark.asyncio
    async def test_a_channel_the_bot_cannot_see_reports_rather_than_raising(self, configured, bot):
        bot.get_channel = MagicMock(return_value=None)
        assert "not found" in await changelog_post.maybe_post_changelog(bot, "1.9.0")


class TestBursts:
    @pytest.mark.asyncio
    async def test_a_following_release_edits_the_same_message(self, configured, bot, channel):
        await changelog_post.maybe_post_changelog(bot, "1.9.0")

        existing = MagicMock()
        existing.edit = AsyncMock()
        channel.fetch_message = AsyncMock(return_value=existing)

        result = await changelog_post.maybe_post_changelog(bot, "1.9.1")

        assert "appended 1.9.1" in result
        channel.send.assert_awaited_once()  # still just the one message
        body = existing.edit.await_args.kwargs["content"]
        assert "**1.9.0**" in body and "**1.9.1**" in body

    @pytest.mark.asyncio
    async def test_a_release_after_the_window_starts_a_new_message(self, configured, bot, channel):
        await changelog_post.maybe_post_changelog(bot, "1.9.0")
        with patch.object(changelog_post, "BURST_WINDOW_SECONDS", 0):
            await changelog_post.maybe_post_changelog(bot, "1.9.1")

        assert channel.send.await_count == 2

    @pytest.mark.asyncio
    async def test_a_deleted_message_falls_back_to_a_new_one(self, configured, bot, channel):
        """A duplicate beats silence."""
        await changelog_post.maybe_post_changelog(bot, "1.9.0")
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "gone")
        )

        result = await changelog_post.maybe_post_changelog(bot, "1.9.1")

        assert "posted 1.9.1" in result
        assert channel.send.await_count == 2


class TestPostFailures:
    """A failed post must not take down on_ready."""

    @pytest.mark.asyncio
    async def test_forbidden_is_reported_not_raised(self, configured, bot, channel):
        channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "nope"))
        assert "cannot post" in await changelog_post.maybe_post_changelog(bot, "1.9.0")

    @pytest.mark.asyncio
    async def test_an_http_error_is_reported_not_raised(self, configured, bot, channel):
        channel.send = AsyncMock(side_effect=discord.HTTPException(MagicMock(status=500), "boom"))
        result = await changelog_post.maybe_post_changelog(bot, "1.9.0")
        assert "post failed" in result

    @pytest.mark.asyncio
    async def test_a_failed_post_is_not_recorded_as_done(self, configured, bot, channel):
        """Otherwise a transient 500 would silently cost that release its post."""
        from config import get_app_setting

        channel.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(status=403), "nope"))
        await changelog_post.maybe_post_changelog(bot, "1.9.0")

        assert get_app_setting(changelog_post.LAST_VERSION_SETTING) != "1.9.0"


class TestLoadBlock:
    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert changelog_post.load_block("1.9.0", tmp_path / "nope.md") is None

    def test_an_over_long_block_is_refused(self, tmp_path):
        path = tmp_path / "d.md"
        path.write_text("---\n\n**1.9.0** — d\n" + ("- pad\n" * 500), encoding="utf-8")
        assert changelog_post.load_block("1.9.0", path) is None

    def test_the_shipped_file_resolves_the_running_version_shape(self):
        """Guards the path wiring — docs/ sits next to the module."""
        assert changelog_post.CHANGELOG_PATH.exists()
        assert changelog_post.load_block("1.8.4") is not None

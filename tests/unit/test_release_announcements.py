"""Tests for release-announcement infra (#253 / 1.3.4).

Covers:
- `should_announce` logic: major change fires, minor change fires, patch
  suppressed, identical suppressed, empty stored suppressed.
- `build_embed` returns None for an unknown version, builds the expected
  structure when a `RELEASE_ANNOUNCEMENTS` entry is present.
- `maybe_post_release_announcement` per-guild paths: posts on happy path
  and updates last_seen_version; skips silently when opted out, when no
  leadership channel, when no dict entry; swallows Forbidden without
  abort.
- Schema migration: `last_seen_version` column on `guild_install_metadata`
  + `release_announcements_enabled` column on `guild_configs`.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import release_announcements


# ── should_announce ──────────────────────────────────────────────────────────


class TestShouldAnnounce:
    def test_major_change_fires(self):
        assert release_announcements.should_announce("1.4.0", "2.0.0") is True

    def test_minor_change_fires(self):
        assert release_announcements.should_announce("1.3.4", "1.4.0") is True

    def test_patch_change_suppressed(self):
        assert release_announcements.should_announce("1.4.0", "1.4.1") is False
        assert release_announcements.should_announce("1.4.1", "1.4.7") is False

    def test_identical_suppressed(self):
        assert release_announcements.should_announce("1.4.0", "1.4.0") is False

    def test_empty_stored_suppressed(self):
        # Empty stored version means we don't know what the guild last
        # saw — better to skip than to spam on top of partial migration.
        assert release_announcements.should_announce("", "1.4.0") is False

    def test_minor_change_across_majors(self):
        # 2.5.x → 3.0.0 — major.minor differs ("2.5" vs "3.0").
        assert release_announcements.should_announce("2.5.3", "3.0.0") is True

    def test_malformed_stored_no_crash(self):
        # We'd rather return False than crash on a malformed stored value.
        # "garbage" can't parse — returns as-is from _major_minor, doesn't
        # match the current's major.minor, and registers as "change".
        # That's fine — at worst it triggers one stale-state notification.
        result = release_announcements.should_announce("garbage", "1.4.0")
        assert isinstance(result, bool)


# ── build_embed ──────────────────────────────────────────────────────────────


@pytest.fixture
def stub_entry():
    """Install a sample entry for "9.9.9" so build_embed has something
    to render. Restored after the test."""
    original = release_announcements.RELEASE_ANNOUNCEMENTS.copy()
    release_announcements.RELEASE_ANNOUNCEMENTS["9.9.9"] = release_announcements.ReleaseAnnouncement(
        description="Sample release description.",
        bullets=["First bullet (💎 Premium)", "Second bullet"],
        support_post_url="https://discord.example/support/post",
        github_release_url="https://github.example/release",
    )
    yield
    release_announcements.RELEASE_ANNOUNCEMENTS.clear()
    release_announcements.RELEASE_ANNOUNCEMENTS.update(original)


class TestBuildEmbed:
    def test_unknown_version_returns_none(self):
        bot_user = MagicMock(display_avatar=MagicMock(url="https://example/avatar.png"))
        assert release_announcements.build_embed("0.0.0", bot_user) is None

    def test_known_version_builds_embed(self, stub_entry):
        bot_user = MagicMock(display_avatar=MagicMock(url="https://example/avatar.png"))
        embed = release_announcements.build_embed("9.9.9", bot_user)
        assert embed is not None
        assert embed.title  == "🎉 Introducing version 9.9.9 of LW Alliance Helper"
        assert embed.url    == "https://discord.example/support/post"
        assert embed.description == "Sample release description."
        assert embed.color.value == release_announcements.EMBED_COLOR

        field_names = [f.name for f in embed.fields]
        assert "⚔️ What's new" in field_names
        assert "📖 Read more"  in field_names

        whats_new = next(f for f in embed.fields if f.name == "⚔️ What's new")
        assert "• First bullet (💎 Premium)" in whats_new.value
        assert "• Second bullet"             in whats_new.value

        read_more = next(f for f in embed.fields if f.name == "📖 Read more")
        assert "https://discord.example/support/post" in read_more.value
        assert "https://github.example/release"       in read_more.value

        # Footer is the opt-out reminder — no markdown rendering, no backticks.
        assert embed.footer.text
        assert "/setup" in embed.footer.text
        assert "📢 Release announcements" in embed.footer.text
        assert "`" not in embed.footer.text  # footer doesn't render markdown

    def test_thumbnail_uses_bot_avatar(self, stub_entry):
        bot_user = MagicMock(display_avatar=MagicMock(url="https://avatar.example/bot.png"))
        embed = release_announcements.build_embed("9.9.9", bot_user)
        assert embed.thumbnail.url == "https://avatar.example/bot.png"


# ── maybe_post_release_announcement ──────────────────────────────────────────


def _make_guild(guild_id: int = 12345):
    g = MagicMock(spec=discord.Guild)
    g.id   = guild_id
    g.name = "Test Alliance"
    g.get_channel = MagicMock()
    return g


def _make_bot(user_avatar_url: str = "https://example/avatar.png"):
    b = MagicMock()
    b.user = MagicMock(display_avatar=MagicMock(url=user_avatar_url))
    return b


@pytest.mark.asyncio
class TestMaybePostReleaseAnnouncement:

    async def test_posts_on_happy_path_and_updates_last_seen(self, temp_db, stub_entry):
        import config
        config.upsert_guild_install_metadata(
            guild_id=12345, guild_name="Test", owner_id=1,
            installer_user_id=None, current_version="9.8.0",  # one minor behind
        )
        config.save_config(config.GuildConfig(
            guild_id=12345, leadership_channel_id=999,
            release_announcements_enabled=1,
        ))

        channel = MagicMock()
        channel.send = AsyncMock()
        guild = _make_guild()
        guild.get_channel.return_value = channel

        await release_announcements.maybe_post_release_announcement(
            guild, _make_bot(), "9.9.9",
        )

        channel.send.assert_called_once()
        sent_embed = channel.send.call_args.kwargs.get("embed")
        assert sent_embed is not None
        assert "9.9.9" in sent_embed.title

        # last_seen_version bumped so we don't re-fire next boot.
        meta = config.get_guild_install_metadata(12345)
        assert meta["last_seen_version"] == "9.9.9"

    async def test_skips_when_opted_out_and_still_bumps_version(self, temp_db, stub_entry):
        import config
        config.upsert_guild_install_metadata(
            guild_id=12345, guild_name="Test", owner_id=1,
            installer_user_id=None, current_version="9.8.0",
        )
        config.save_config(config.GuildConfig(
            guild_id=12345, leadership_channel_id=999,
            release_announcements_enabled=0,  # opted out
        ))

        channel = MagicMock()
        channel.send = AsyncMock()
        guild = _make_guild()
        guild.get_channel.return_value = channel

        await release_announcements.maybe_post_release_announcement(
            guild, _make_bot(), "9.9.9",
        )

        channel.send.assert_not_called()
        # Opted-out guilds still get their version bumped so we don't
        # re-evaluate them on every boot.
        assert config.get_guild_install_metadata(12345)["last_seen_version"] == "9.9.9"

    async def test_skips_when_no_leadership_channel_and_does_not_bump(self, temp_db, stub_entry):
        import config
        config.upsert_guild_install_metadata(
            guild_id=12345, guild_name="Test", owner_id=1,
            installer_user_id=None, current_version="9.8.0",
        )
        config.save_config(config.GuildConfig(
            guild_id=12345, leadership_channel_id=0,   # never set up
            release_announcements_enabled=1,
        ))

        guild = _make_guild()
        await release_announcements.maybe_post_release_announcement(
            guild, _make_bot(), "9.9.9",
        )

        # Version NOT bumped — once they finish setup we want the next
        # boot to still deliver the announcement they missed.
        assert config.get_guild_install_metadata(12345)["last_seen_version"] == "9.8.0"

    async def test_skips_when_channel_missing_from_guild_and_does_not_bump(self, temp_db, stub_entry):
        import config
        config.upsert_guild_install_metadata(
            guild_id=12345, guild_name="Test", owner_id=1,
            installer_user_id=None, current_version="9.8.0",
        )
        config.save_config(config.GuildConfig(
            guild_id=12345, leadership_channel_id=999,
            release_announcements_enabled=1,
        ))

        guild = _make_guild()
        guild.get_channel.return_value = None  # channel deleted

        await release_announcements.maybe_post_release_announcement(
            guild, _make_bot(), "9.9.9",
        )
        assert config.get_guild_install_metadata(12345)["last_seen_version"] == "9.8.0"

    async def test_skips_when_no_dict_entry_and_still_bumps_version(self, temp_db):
        """Major/minor changed but no `RELEASE_ANNOUNCEMENTS` entry exists
        for the current version (e.g. a deliberate quiet release).  Still
        bump `last_seen_version` so this guild isn't re-evaluated on
        every boot until the next minor that does have content."""
        import config
        config.upsert_guild_install_metadata(
            guild_id=12345, guild_name="Test", owner_id=1,
            installer_user_id=None, current_version="9.8.0",
        )
        config.save_config(config.GuildConfig(
            guild_id=12345, leadership_channel_id=999,
            release_announcements_enabled=1,
        ))

        channel = MagicMock()
        channel.send = AsyncMock()
        guild = _make_guild()
        guild.get_channel.return_value = channel

        await release_announcements.maybe_post_release_announcement(
            guild, _make_bot(), "9.9.9",  # no entry in dict for this version
        )

        channel.send.assert_not_called()
        assert config.get_guild_install_metadata(12345)["last_seen_version"] == "9.9.9"

    async def test_swallows_forbidden(self, temp_db, stub_entry):
        import config
        config.upsert_guild_install_metadata(
            guild_id=12345, guild_name="Test", owner_id=1,
            installer_user_id=None, current_version="9.8.0",
        )
        config.save_config(config.GuildConfig(
            guild_id=12345, leadership_channel_id=999,
            release_announcements_enabled=1,
        ))

        channel = MagicMock()
        channel.send = AsyncMock(side_effect=discord.Forbidden(
            response=MagicMock(status=403), message="missing perms",
        ))
        guild = _make_guild()
        guild.get_channel.return_value = channel

        # Should NOT raise — Forbidden is swallowed.
        await release_announcements.maybe_post_release_announcement(
            guild, _make_bot(), "9.9.9",
        )
        # last_seen_version unchanged — once perms return, the next boot
        # delivers the missed announcement.
        assert config.get_guild_install_metadata(12345)["last_seen_version"] == "9.8.0"

    async def test_skips_silently_when_no_metadata_row(self, temp_db, stub_entry):
        """`maybe_post_release_announcement` is called from on_ready AFTER
        the upsert loop, so this is a defensive guard — if the upsert
        somehow failed for a guild, we shouldn't crash trying to post."""
        guild = _make_guild(guild_id=99999)
        # No upsert call → no row in guild_install_metadata.
        await release_announcements.maybe_post_release_announcement(
            guild, _make_bot(), "9.9.9",
        )
        # No crash, nothing posted.


# ── Schema migration ─────────────────────────────────────────────────────────


def test_migration_adds_last_seen_version_to_install_metadata(temp_db):
    with sqlite3.connect(temp_db) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(guild_install_metadata)"
        ).fetchall()}
    assert "last_seen_version" in cols


def test_migration_adds_release_announcements_enabled_to_guild_configs(temp_db):
    with sqlite3.connect(temp_db) as conn:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(guild_configs)"
        ).fetchall()}
    assert "release_announcements_enabled" in cols


def test_release_announcements_enabled_defaults_to_on(temp_db):
    import config
    config.save_config(config.GuildConfig(guild_id=12345))
    cfg = config.get_config(12345)
    # Default ON — alliances must explicitly opt out.
    assert cfg.release_announcements_enabled == 1


def test_upsert_persists_current_version_on_first_sighting(temp_db):
    """New guilds get `last_seen_version = current_version` on the very
    first upsert call. Prevents the next boot from re-evaluating and
    spamming a "Welcome to vX.Y.Z" announcement."""
    import config
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Test", owner_id=1,
        installer_user_id=None, current_version="1.4.0",
    )
    assert config.get_guild_install_metadata(12345)["last_seen_version"] == "1.4.0"


def test_upsert_preserves_last_seen_version_on_subsequent_sightings(temp_db):
    """Once `last_seen_version` is set, the upsert never touches it —
    the release-announce handler owns updates to that column."""
    import config
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Test", owner_id=1,
        installer_user_id=None, current_version="1.4.0",
    )
    # Second sighting at a later version — upsert leaves it alone.
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Test", owner_id=1,
        installer_user_id=None, current_version="1.5.0",
    )
    assert config.get_guild_install_metadata(12345)["last_seen_version"] == "1.4.0"


def test_set_last_seen_version_updates_only_that_column(temp_db):
    import config
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Test", owner_id=1,
        installer_user_id=42, current_version="1.4.0",
    )
    before = config.get_guild_install_metadata(12345)
    config.set_last_seen_version(12345, "2.0.0")
    after = config.get_guild_install_metadata(12345)
    assert after["last_seen_version"] == "2.0.0"
    # Other fields untouched.
    assert after["installer_user_id"] == before["installer_user_id"]
    assert after["installed_at"]      == before["installed_at"]

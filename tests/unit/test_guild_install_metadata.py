"""
Unit tests for the guild_install_metadata table + helpers (issue #67).

Covers:
- Schema migration: the table exists after init_db.
- upsert semantics: first sighting writes all fields; later sightings refresh
  guild_name / owner_id / last_seen_at; installed_at is preserved; the
  installer_user_id is preserved if already set and backfilled if it was
  previously NULL.
- get_guild_install_metadata returns a dict or None.
- delete_guild_install_metadata returns True only when a row was deleted.
"""
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def test_schema_creates_metadata_table(temp_db):
    """init_db creates guild_install_metadata with the expected columns."""
    with sqlite3.connect(temp_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(guild_install_metadata)").fetchall()}
    assert cols == {
        "guild_id", "guild_name", "owner_id",
        "installer_user_id", "installed_at", "last_seen_at",
    }


def test_upsert_first_sighting_writes_all_fields(temp_db):
    import config
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Alpha Wolves", owner_id=999, installer_user_id=777,
    )
    row = config.get_guild_install_metadata(12345)
    assert row["guild_id"]          == 12345
    assert row["guild_name"]        == "Alpha Wolves"
    assert row["owner_id"]          == 999
    assert row["installer_user_id"] == 777
    # installed_at and last_seen_at are stamped at the same instant on first write
    assert row["installed_at"] == row["last_seen_at"]
    assert row["installed_at"]  # non-empty ISO string


def test_upsert_preserves_installed_at_and_refreshes_other_fields(temp_db):
    import config
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Alpha Wolves", owner_id=999, installer_user_id=777,
    )
    first = config.get_guild_install_metadata(12345)
    # Force a measurable gap so the new last_seen_at differs from installed_at
    time.sleep(0.01)
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Renamed Wolves", owner_id=1001, installer_user_id=777,
    )
    second = config.get_guild_install_metadata(12345)
    assert second["installed_at"] == first["installed_at"]
    assert second["last_seen_at"] != first["last_seen_at"]
    assert second["guild_name"]   == "Renamed Wolves"
    assert second["owner_id"]     == 1001


def test_upsert_preserves_installer_when_already_set(temp_db):
    """Second call with installer_user_id=None must not wipe the existing one."""
    import config
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Alpha", owner_id=999, installer_user_id=777,
    )
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Alpha", owner_id=999, installer_user_id=None,
    )
    assert config.get_guild_install_metadata(12345)["installer_user_id"] == 777


def test_upsert_backfills_installer_when_previously_null(temp_db):
    """First call without an installer, then a later call that has one — the
    later value should land. Models the case where on_ready ran first
    (no audit log) and on_guild_join fired later (with audit log)."""
    import config
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Alpha", owner_id=999, installer_user_id=None,
    )
    assert config.get_guild_install_metadata(12345)["installer_user_id"] is None
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Alpha", owner_id=999, installer_user_id=42,
    )
    assert config.get_guild_install_metadata(12345)["installer_user_id"] == 42


def test_get_returns_none_for_absent_guild(temp_db):
    import config
    assert config.get_guild_install_metadata(99999) is None


def test_delete_returns_true_then_false(temp_db):
    import config
    config.upsert_guild_install_metadata(
        guild_id=12345, guild_name="Alpha", owner_id=999, installer_user_id=None,
    )
    assert config.delete_guild_install_metadata(12345) is True
    assert config.get_guild_install_metadata(12345) is None
    # Second delete is a no-op
    assert config.delete_guild_install_metadata(12345) is False


def test_delete_only_affects_target_guild(temp_db):
    """Make sure DELETE WHERE guild_id = ? is correctly scoped."""
    import config
    config.upsert_guild_install_metadata(
        guild_id=11111, guild_name="A", owner_id=1, installer_user_id=None,
    )
    config.upsert_guild_install_metadata(
        guild_id=22222, guild_name="B", owner_id=2, installer_user_id=None,
    )
    config.delete_guild_install_metadata(11111)
    assert config.get_guild_install_metadata(11111) is None
    assert config.get_guild_install_metadata(22222) is not None


def test_admin_commands_registered():
    """Sanity check: bot.py exposes /admin_guild_info and /admin_forget_guild
    on the command tree. Booting Discord isn't needed — `bot.tree` is built
    at import time."""
    import bot as bot_module
    names = {c.name for c in bot_module.bot.tree.get_commands()}
    assert "admin_guild_info"   in names
    assert "admin_forget_guild" in names

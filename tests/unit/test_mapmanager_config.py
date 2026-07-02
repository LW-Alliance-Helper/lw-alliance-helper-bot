"""Tests for the Map Manager guild-link config helpers (6C, #316).

Exercises ``save_guild_alliance_mapping`` / ``get_guild_alliance_mapping`` /
``revoke_guild_alliance_mapping`` against a temp SQLite DB: upsert-on-relink,
active-only reads, soft-revoke, and re-link reactivation.
"""

from __future__ import annotations

import config

GUILD = 999000111


def test_get_absent_returns_none(temp_db):
    assert config.get_guild_alliance_mapping(GUILD) is None


def test_save_then_get(temp_db):
    config.save_guild_alliance_mapping(
        guild_id=GUILD,
        alliance_name="Nox",
        server=738,
        mm_alliance_id="alliance-uuid",
        mm_server_grouping_id="grouping-uuid",
    )
    m = config.get_guild_alliance_mapping(GUILD)
    assert m is not None
    assert m["alliance_name"] == "Nox"
    assert m["server"] == 738
    assert m["mm_alliance_id"] == "alliance-uuid"
    assert m["mm_server_grouping_id"] == "grouping-uuid"
    assert m["revoked_at"] is None
    assert m["linked_at"]  # stamped


def test_null_grouping_roundtrips(temp_db):
    config.save_guild_alliance_mapping(
        guild_id=GUILD,
        alliance_name="Nox",
        server=738,
        mm_alliance_id="alliance-uuid",
        mm_server_grouping_id=None,
    )
    m = config.get_guild_alliance_mapping(GUILD)
    assert m is not None
    assert m["mm_server_grouping_id"] is None


def test_relink_overwrites_in_place(temp_db):
    config.save_guild_alliance_mapping(GUILD, "Nox", 738, "a1", "g1")
    config.save_guild_alliance_mapping(GUILD, "Renamed", 740, "a2", "g2")
    m = config.get_guild_alliance_mapping(GUILD)
    assert m["alliance_name"] == "Renamed"
    assert m["server"] == 740
    assert m["mm_alliance_id"] == "a2"
    assert m["mm_server_grouping_id"] == "g2"

    # Still exactly one row for the guild (PK upsert, not an insert).
    with config._get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM guild_alliance_mappings WHERE guild_id = ?", (GUILD,)
        ).fetchone()[0]
    assert count == 1


def test_revoke_hides_from_active_read(temp_db):
    config.save_guild_alliance_mapping(GUILD, "Nox", 738, "a1", "g1")
    assert config.revoke_guild_alliance_mapping(GUILD) is True

    # Active read now returns None ...
    assert config.get_guild_alliance_mapping(GUILD) is None
    # ... but the revoked row is still there for audit.
    revoked = config.get_guild_alliance_mapping(GUILD, include_revoked=True)
    assert revoked is not None
    assert revoked["revoked_at"] is not None


def test_revoke_without_active_link_returns_false(temp_db):
    # No link at all.
    assert config.revoke_guild_alliance_mapping(GUILD) is False
    # And double-revoke is a no-op the second time.
    config.save_guild_alliance_mapping(GUILD, "Nox", 738, "a1", "g1")
    assert config.revoke_guild_alliance_mapping(GUILD) is True
    assert config.revoke_guild_alliance_mapping(GUILD) is False


def test_relink_reactivates_after_revoke(temp_db):
    config.save_guild_alliance_mapping(GUILD, "Nox", 738, "a1", "g1")
    config.revoke_guild_alliance_mapping(GUILD)
    # Re-running setup clears revoked_at.
    config.save_guild_alliance_mapping(GUILD, "Nox", 738, "a1", "g1")
    m = config.get_guild_alliance_mapping(GUILD)
    assert m is not None
    assert m["revoked_at"] is None

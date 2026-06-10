"""Unit tests for the guild_transfer_config storage layer (#16) in
config.py: schema creation, default fallback, allowlist-guarded partial
updates, clear, and the poll-loop work-list query.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import config

GUILD = 555000000000000001
GUILD2 = 555000000000000002


def test_default_config_when_unconfigured(temp_db):
    cfg = config.get_transfer_config(GUILD)
    assert cfg["guild_id"] == GUILD
    assert cfg["enabled"] == 0
    assert cfg["poll_frequency_minutes"] == 60
    assert cfg["alliance_column_map_json"] == "{}"
    assert cfg["notification_filter_json"] == ""
    assert cfg["last_seen_state_json"] == "{}"


def test_has_transfer_config_false_then_true(temp_db):
    assert config.has_transfer_config(GUILD) is False
    config.update_transfer_config_field(GUILD, "alliance_sheet_id", "sheet-abc")
    assert config.has_transfer_config(GUILD) is True


def test_update_single_field_persists(temp_db):
    config.update_transfer_config_field(GUILD, "alliance_sheet_id", "sheet-abc")
    config.update_transfer_config_field(GUILD, "poll_frequency_minutes", 15)
    cfg = config.get_transfer_config(GUILD)
    assert cfg["alliance_sheet_id"] == "sheet-abc"
    assert cfg["poll_frequency_minutes"] == 15
    # Untouched fields keep their defaults.
    assert cfg["enabled"] == 0


def test_update_field_rejects_unknown_column(temp_db):
    with pytest.raises(ValueError):
        config.update_transfer_config_field(GUILD, "drop table", "x")


def test_update_fields_bulk(temp_db):
    config.update_transfer_config_fields(
        GUILD,
        enabled=1,
        alliance_sheet_id="abc",
        alliance_sheet_tab="Applicants",
        notification_channel_id=999,
    )
    cfg = config.get_transfer_config(GUILD)
    assert cfg["enabled"] == 1
    assert cfg["alliance_sheet_id"] == "abc"
    assert cfg["alliance_sheet_tab"] == "Applicants"
    assert cfg["notification_channel_id"] == 999


def test_update_fields_rejects_unknown(temp_db):
    with pytest.raises(ValueError):
        config.update_transfer_config_fields(GUILD, enabled=1, bogus="x")


def test_update_fields_empty_is_noop(temp_db):
    # No fields → no row created, no crash.
    config.update_transfer_config_fields(GUILD)
    assert config.has_transfer_config(GUILD) is False


def test_clear_config(temp_db):
    config.update_transfer_config_field(GUILD, "enabled", 1)
    assert config.has_transfer_config(GUILD) is True
    config.clear_transfer_config(GUILD)
    assert config.has_transfer_config(GUILD) is False
    # Back to default dict.
    assert config.get_transfer_config(GUILD)["enabled"] == 0


def test_enabled_guilds_requires_enabled_and_sheet(temp_db):
    # Enabled but no sheet → excluded.
    config.update_transfer_config_field(GUILD, "enabled", 1)
    assert config.get_transfer_enabled_guilds() == []

    # Sheet but disabled → excluded.
    config.update_transfer_config_fields(GUILD2, enabled=0, alliance_sheet_id="s2")
    assert config.get_transfer_enabled_guilds() == []

    # Enabled + sheet → included.
    config.update_transfer_config_field(GUILD, "alliance_sheet_id", "s1")
    rows = config.get_transfer_enabled_guilds()
    assert [r["guild_id"] for r in rows] == [GUILD]


def test_state_update_roundtrip(temp_db):
    config.update_transfer_config_fields(
        GUILD,
        last_seen_state_json='{"abc": {"confirmed": "true"}}',
        last_polled_at="2026-06-08T12:00:00",
    )
    cfg = config.get_transfer_config(GUILD)
    assert cfg["last_seen_state_json"] == '{"abc": {"confirmed": "true"}}'
    assert cfg["last_polled_at"] == "2026-06-08T12:00:00"

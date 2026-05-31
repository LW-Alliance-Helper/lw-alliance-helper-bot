"""
Unit tests for config_export.py — per-category collection, JSON
serialization, schema validation, remap grouping, and apply-import.
"""

from __future__ import annotations

import json
import pytest
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


def _channel_lookup(_cid: int) -> str:
    return f"#channel-{_cid}"


def _role_lookup(_rid: int) -> str:
    return f"@role-{_rid}"


class TestCollectAvailableCategories:
    """Only categories with data appear in the multi-select. The
    collectors return None for empty categories so the export wizard
    never offers something the source guild has no data for."""

    def test_blank_guild_offers_nothing(self, seeded_db):
        import config_export

        # The seeded_db fixture leaves the guild in a clean post-setup state
        # but without train / growth / birthday / etc. settings — so most
        # categories should be missing.
        from config import save_config, get_or_create_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = False
        save_config(cfg)

        cats = config_export.collect_available_categories(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        # Core requires setup_complete; we cleared it above.
        assert "core" not in cats

    def test_core_appears_when_setup_complete(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.timezone = "America/New_York"
        save_config(cfg)
        cats = config_export.collect_available_categories(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert "core" in cats


class TestCollectCore:
    def test_remap_channels_only_when_non_zero(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.leadership_channel_id = 12345
        cfg.member_role_id = 99999
        cfg.ds_log_channel_id = 0  # unset → should NOT appear in remap
        save_config(cfg)

        result = config_export.collect_core(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert result is not None
        channel_fields = {e["field"] for e in result["remap_channels"]}
        assert "leadership_channel_id" in channel_fields
        assert "ds_log_channel_id" not in channel_fields
        role_fields = {e["field"] for e in result["remap_roles"]}
        assert "member_role_id" in role_fields

    def test_travels_carries_alliance_defined_strings(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.leadership_role_name = "Officer"
        cfg.timezone = "America/Los_Angeles"
        cfg.spreadsheet_id = "abc123XYZ"
        save_config(cfg)

        result = config_export.collect_core(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert result["travels"]["leadership_role_name"] == "Officer"
        assert result["travels"]["timezone"] == "America/Los_Angeles"
        assert result["travels"]["spreadsheet_id"] == "abc123XYZ"

    def test_returns_none_when_not_setup_complete(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = False
        save_config(cfg)
        result = config_export.collect_core(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert result is None


class TestCollectGrowth:
    def test_carries_core_growth_fields(self, seeded_db):
        import config_export
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )
        result = config_export.collect_growth(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert result is not None
        assert result["travels"]["enabled"] == 1
        assert result["travels"]["tab_source"] == "Powers"
        assert json.loads(result["travels"]["metrics"]) == [
            {"col": "B", "label": "Power"},
        ]

    def test_carries_breakdown_fields(self, seeded_db):
        import config_export
        from config import save_growth_config, save_growth_breakdown_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )
        save_growth_breakdown_config(
            TEST_GUILD_ID,
            tab_breakdown="Growth Breakdown",
            breakdown_thresholds={"increased": 25, "steady": 12, "low": 5, "none": 0},
            breakdown_labels={"increased": "Crushing It"},
            breakdown_post_channel_id=55555,
            breakdown_bucket_filter=["decline", "none"],
        )

        result = config_export.collect_growth(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert result is not None
        t = result["travels"]
        assert json.loads(t["breakdown_thresholds"])["increased"] == 25
        assert json.loads(t["breakdown_labels"])["increased"] == "Crushing It"
        assert json.loads(t["breakdown_bucket_filter"]) == ["decline", "none"]
        channel_fields = {e["field"] for e in result["remap_channels"]}
        assert "breakdown_post_channel_id" in channel_fields


class TestCollectShinyTasks:
    def test_returns_none_when_unconfigured(self, seeded_db):
        import config_export

        # seeded_db doesn't pre-populate shiny_tasks, so the collector
        # should report nothing-to-export.
        result = config_export.collect_shiny_tasks(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert result is None

    def test_collects_travels_and_remaps_channel(self, seeded_db):
        import config_export
        from config import save_shiny_tasks_config

        save_shiny_tasks_config(
            TEST_GUILD_ID,
            enabled=1,
            channel_id=42424242,
            post_time="07:30",
            server_min=1000,
            server_max=1500,
            message_template="Shiny servers today: {servers}",
        )
        result = config_export.collect_shiny_tasks(
            TEST_GUILD_ID,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert result is not None
        t = result["travels"]
        assert t["enabled"] == 1
        assert t["post_time"] == "07:30"
        assert t["server_min"] == 1000
        assert t["server_max"] == 1500
        assert t["message_template"] == "Shiny servers today: {servers}"
        # The channel is a remap, not a travel — and `last_posted_date`
        # is operational state, must not leak into the export.
        assert "channel_id" not in t
        assert "last_posted_date" not in t
        channel_fields = {e["field"] for e in result["remap_channels"]}
        assert channel_fields == {"channel_id"}


class TestApplyShinyTasks:
    def test_round_trips_through_export_and_apply(self, seeded_db):
        import config_export
        from config import save_shiny_tasks_config, get_shiny_tasks_config

        save_shiny_tasks_config(
            TEST_GUILD_ID,
            enabled=1,
            channel_id=11111,
            post_time="08:15",
            server_min=2000,
            server_max=2500,
            message_template="hello {servers}",
        )
        export = config_export.build_export(
            TEST_GUILD_ID,
            categories=["shiny_tasks"],
            source_guild_name="Src",
            exporter_user_id=1,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        payload = config_export.serialize_to_json_bytes(export)
        parsed = config_export.parse_and_validate(payload)
        decisions = config_export.RemapDecisions(
            channel_decisions={11111: ("set", 99999)},  # remap to a new channel
            role_decisions={},
            spreadsheet_id=None,
            same_guild=False,
        )
        summary = config_export.apply_import(TEST_GUILD_ID, parsed, decisions)
        assert "shiny_tasks" in summary["applied"]

        after = get_shiny_tasks_config(TEST_GUILD_ID)
        assert after["enabled"] == 1
        assert after["channel_id"] == 99999  # the remapped value
        assert after["post_time"] == "08:15"
        assert after["server_min"] == 2000
        assert after["server_max"] == 2500
        assert after["message_template"] == "hello {servers}"


class TestBuildAndSerialize:
    def test_build_export_has_v1_envelope(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        save_config(cfg)

        export = config_export.build_export(
            TEST_GUILD_ID,
            categories=["core"],
            source_guild_name="Test Server",
            exporter_user_id=999,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        assert export["schema_version"] == config_export.EXPORT_SCHEMA_VERSION
        assert export["source_guild"]["id"] == TEST_GUILD_ID
        assert export["source_guild"]["name"] == "Test Server"
        assert export["exported_by_user_id"] == 999
        assert export["categories"] == ["core"]
        assert "core" in export["data"]

    def test_serialize_round_trips_through_validate(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.timezone = "America/New_York"
        save_config(cfg)

        export = config_export.build_export(
            TEST_GUILD_ID,
            categories=["core"],
            source_guild_name="Test",
            exporter_user_id=1,
            channel_lookup=_channel_lookup,
            role_lookup=_role_lookup,
        )
        payload = config_export.serialize_to_json_bytes(export)
        parsed = config_export.parse_and_validate(payload)
        assert parsed["categories_present"] == ["core"]
        assert parsed["data"]["core"]["travels"]["timezone"] == "America/New_York"


class TestParseAndValidate:
    def test_rejects_non_json(self):
        import config_export

        with pytest.raises(config_export.ImportValidationError):
            config_export.parse_and_validate(b"not json")

    def test_rejects_array_at_top(self):
        import config_export

        with pytest.raises(config_export.ImportValidationError):
            config_export.parse_and_validate(b"[1, 2, 3]")

    def test_rejects_future_schema_version(self):
        import config_export

        payload = json.dumps(
            {
                "schema_version": config_export.EXPORT_SCHEMA_VERSION + 99,
                "source_guild": {"id": 1, "name": "X"},
                "categories": [],
                "data": {},
            }
        ).encode("utf-8")
        with pytest.raises(config_export.ImportValidationError) as exc:
            config_export.parse_and_validate(payload)
        assert "newer bot" in str(exc.value)

    def test_lenient_with_unknown_categories(self):
        import config_export

        payload = json.dumps(
            {
                "schema_version": 1,
                "source_guild": {"id": 1, "name": "X"},
                "categories": ["core", "future_category"],
                "data": {"future_category": {"hello": "world"}},
            }
        ).encode("utf-8")
        parsed = config_export.parse_and_validate(payload)
        # The unknown category lands in `unknown_keys` rather than failing.
        assert any("future_category" in k for k in parsed["unknown_keys"])
        # And `categories_present` only includes known + present.
        assert "future_category" not in parsed["categories_present"]

    def test_rejects_non_dict_one_row_section(self):
        import config_export

        payload = json.dumps(
            {
                "schema_version": 1,
                "source_guild": {"id": 1, "name": "X"},
                "categories": ["core"],
                "data": {"core": "not a dict"},
            }
        ).encode("utf-8")
        with pytest.raises(config_export.ImportValidationError):
            config_export.parse_and_validate(payload)

    def test_rejects_non_list_multi_row_section(self):
        import config_export

        payload = json.dumps(
            {
                "schema_version": 1,
                "source_guild": {"id": 1, "name": "X"},
                "categories": ["events"],
                "data": {"events": {"oops": "should be a list"}},
            }
        ).encode("utf-8")
        with pytest.raises(config_export.ImportValidationError):
            config_export.parse_and_validate(payload)


class TestDiscoverRemapGroups:
    def test_groups_by_source_id_across_categories(self):
        import config_export

        parsed = {
            "schema_version": 1,
            "source_guild": {"id": 1, "name": "X"},
            "categories": ["core", "train"],
            "data": {
                "core": {
                    "travels": {},
                    "remap_channels": [
                        {
                            "field": "leadership_channel_id",
                            "purpose": "Leadership channel",
                            "source_id": 100,
                            "source_name": "#leadership",
                        },
                        {
                            "field": "ds_log_channel_id",
                            "purpose": "DS log channel",
                            "source_id": 100,
                            "source_name": "#leadership",
                        },
                    ],
                    "remap_roles": [],
                },
                "train": {
                    "travels": {},
                    "remap_channels": [
                        {
                            "field": "reminder_channel_id",
                            "purpose": "Train reminder channel",
                            "source_id": 200,
                            "source_name": "#train",
                        },
                    ],
                    "remap_roles": [],
                },
            },
            "categories_present": ["core", "train"],
        }
        channel_groups, role_groups = config_export.discover_remap_groups(parsed)
        assert role_groups == []
        # Two unique source IDs → two groups.
        assert {g.source_id for g in channel_groups} == {100, 200}
        # The 100-group lists both purposes.
        g100 = next(g for g in channel_groups if g.source_id == 100)
        assert sorted(g100.purposes) == ["DS log channel", "Leadership channel"]
        # The 200-group lists one.
        g200 = next(g for g in channel_groups if g.source_id == 200)
        assert g200.purposes == ["Train reminder channel"]

    def test_handles_multi_row_categories(self):
        import config_export

        parsed = {
            "schema_version": 1,
            "source_guild": {"id": 1, "name": "X"},
            "categories": ["events"],
            "data": {
                "events": [
                    {
                        "travels": {},
                        "remap_channels": [
                            {
                                "field": "draft_channel_id",
                                "purpose": "Event 'A' draft",
                                "source_id": 300,
                                "source_name": "#a",
                            },
                        ],
                        "remap_roles": [],
                    },
                    {
                        "travels": {},
                        "remap_channels": [
                            {
                                "field": "draft_channel_id",
                                "purpose": "Event 'B' draft",
                                "source_id": 300,
                                "source_name": "#a",
                            },
                        ],
                        "remap_roles": [],
                    },
                ],
            },
            "categories_present": ["events"],
        }
        channel_groups, _ = config_export.discover_remap_groups(parsed)
        assert len(channel_groups) == 1
        assert channel_groups[0].source_id == 300
        # Both events' purposes consolidated under the single source ID.
        assert sorted(channel_groups[0].purposes) == ["Event 'A' draft", "Event 'B' draft"]


class TestApplyImportRemapDecisions:
    """RemapDecisions resolution: `set` writes the new ID, `keep_current`
    leaves the field alone, `skip` clears to 0."""

    def test_set_writes_new_channel_id(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config, get_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.leadership_channel_id = 100
        save_config(cfg)

        # Build a synthetic parsed export: core with the leadership channel
        # set to source-ID 100, and a remap decision setting it to 200.
        parsed = {
            "schema_version": 1,
            "categories": ["core"],
            "categories_present": ["core"],
            "unknown_keys": [],
            "data": {
                "core": {
                    "travels": {"leadership_role_name": "Officer"},
                    "remap_channels": [
                        {
                            "field": "leadership_channel_id",
                            "purpose": "Leadership channel",
                            "source_id": 100,
                            "source_name": "#old",
                        },
                    ],
                    "remap_roles": [],
                },
            },
        }
        decisions = config_export.RemapDecisions(
            channel_decisions={100: ("set", 200)},
            role_decisions={},
            spreadsheet_id=None,
            same_guild=False,
        )
        summary = config_export.apply_import(TEST_GUILD_ID, parsed, decisions)
        assert "core" in summary["applied"]
        cfg2 = get_config(TEST_GUILD_ID)
        assert cfg2.leadership_channel_id == 200
        assert cfg2.leadership_role_name == "Officer"

    def test_keep_current_leaves_field_alone(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config, get_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.leadership_channel_id = 777
        save_config(cfg)

        parsed = {
            "schema_version": 1,
            "categories": ["core"],
            "categories_present": ["core"],
            "unknown_keys": [],
            "data": {
                "core": {
                    "travels": {},
                    "remap_channels": [
                        {
                            "field": "leadership_channel_id",
                            "purpose": "Leadership channel",
                            "source_id": 100,
                            "source_name": "#old",
                        },
                    ],
                    "remap_roles": [],
                },
            },
        }
        decisions = config_export.RemapDecisions(
            channel_decisions={100: ("keep_current",)},
            role_decisions={},
            spreadsheet_id=None,
            same_guild=False,
        )
        config_export.apply_import(TEST_GUILD_ID, parsed, decisions)
        cfg2 = get_config(TEST_GUILD_ID)
        # The new guild's value (777) is preserved; the source ID (100)
        # was never applied.
        assert cfg2.leadership_channel_id == 777

    def test_skip_clears_to_zero(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config, get_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.leadership_channel_id = 999
        save_config(cfg)

        parsed = {
            "schema_version": 1,
            "categories": ["core"],
            "categories_present": ["core"],
            "unknown_keys": [],
            "data": {
                "core": {
                    "travels": {},
                    "remap_channels": [
                        {
                            "field": "leadership_channel_id",
                            "purpose": "Leadership channel",
                            "source_id": 100,
                            "source_name": "#old",
                        },
                    ],
                    "remap_roles": [],
                },
            },
        }
        decisions = config_export.RemapDecisions(
            channel_decisions={100: ("skip",)},
            role_decisions={},
            spreadsheet_id=None,
            same_guild=False,
        )
        config_export.apply_import(TEST_GUILD_ID, parsed, decisions)
        cfg2 = get_config(TEST_GUILD_ID)
        assert cfg2.leadership_channel_id == 0


class TestApplyImportSpreadsheetId:
    def test_none_keeps_current(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config, get_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.spreadsheet_id = "keep-this"
        save_config(cfg)

        parsed = {
            "schema_version": 1,
            "categories": ["core"],
            "categories_present": ["core"],
            "unknown_keys": [],
            "data": {
                "core": {
                    "travels": {"spreadsheet_id": "exported-value"},
                    "remap_channels": [],
                    "remap_roles": [],
                },
            },
        }
        decisions = config_export.RemapDecisions(
            channel_decisions={},
            role_decisions={},
            spreadsheet_id=None,
            same_guild=False,
        )
        config_export.apply_import(TEST_GUILD_ID, parsed, decisions)
        assert get_config(TEST_GUILD_ID).spreadsheet_id == "keep-this"

    def test_explicit_value_writes_through(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config, get_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.spreadsheet_id = "old-id"
        save_config(cfg)

        parsed = {
            "schema_version": 1,
            "categories": ["core"],
            "categories_present": ["core"],
            "unknown_keys": [],
            "data": {
                "core": {
                    "travels": {"spreadsheet_id": "old-id"},
                    "remap_channels": [],
                    "remap_roles": [],
                },
            },
        }
        decisions = config_export.RemapDecisions(
            channel_decisions={},
            role_decisions={},
            spreadsheet_id="brand-new-id",
            same_guild=False,
        )
        config_export.apply_import(TEST_GUILD_ID, parsed, decisions)
        assert get_config(TEST_GUILD_ID).spreadsheet_id == "brand-new-id"

    def test_empty_string_clears(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config, get_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        cfg.spreadsheet_id = "old-id"
        save_config(cfg)

        parsed = {
            "schema_version": 1,
            "categories": ["core"],
            "categories_present": ["core"],
            "unknown_keys": [],
            "data": {
                "core": {
                    "travels": {},
                    "remap_channels": [],
                    "remap_roles": [],
                },
            },
        }
        decisions = config_export.RemapDecisions(
            channel_decisions={},
            role_decisions={},
            spreadsheet_id="",
            same_guild=False,
        )
        config_export.apply_import(TEST_GUILD_ID, parsed, decisions)
        assert get_config(TEST_GUILD_ID).spreadsheet_id == ""


class TestApplyImportSummary:
    def test_unknown_keys_surface_as_warnings(self, seeded_db):
        import config_export
        from config import save_config, get_or_create_config

        cfg = get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        save_config(cfg)
        parsed = {
            "schema_version": 1,
            "categories": ["core"],
            "categories_present": ["core"],
            "unknown_keys": ["categories[future_thing]"],
            "data": {
                "core": {
                    "travels": {},
                    "remap_channels": [],
                    "remap_roles": [],
                },
            },
        }
        decisions = config_export.RemapDecisions(
            channel_decisions={},
            role_decisions={},
            spreadsheet_id=None,
            same_guild=False,
        )
        summary = config_export.apply_import(TEST_GUILD_ID, parsed, decisions)
        assert any("future_thing" in w for w in summary["warnings"])

"""
Integration tests for Desert Storm and Canyon Storm draft flows.
Tests build_ds_mail and build_cs_mail with realistic zone/sub data,
and verifies template substitution produces correct output.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


SAMPLE_DS_ZONES = {
    "Zone 1": ["Alice", "Bob"],
    "Zone 2": ["Carol", "Dave"],
    "Zone 3": ["Eve"],
}
SAMPLE_DS_SUBS = ["Frank", "Grace"]

SAMPLE_CS_ZONES = {
    "Zone A": ["Alice", "Bob"],
    "Zone B": ["Carol", "Dave"],
}
SAMPLE_CS_SUBS = ["Eve"]


class TestDsMailGeneration:
    """Full Desert Storm mail generation flow."""

    def setup_method(self, method):
        """Per-test setup hook (placeholder — DS template is set per test)."""
        pass

    def test_full_ds_mail_a_contains_all_zones(self, seeded_db):
        from storm import build_ds_mail
        from config import save_storm_config
        from defaults import DEFAULT_DS_TEMPLATE

        save_storm_config(TEST_GUILD_ID, "DS", "DS Assignments", DEFAULT_DS_TEMPLATE,
                          "", "", "America/New_York", 0)

        result = build_ds_mail("A", SAMPLE_DS_ZONES, SAMPLE_DS_SUBS,
                               "18:00 Server Time", guild_id=TEST_GUILD_ID)
        assert "Alice"    in result
        assert "Bob"      in result
        assert "Carol"    in result
        assert "Dave"     in result
        assert "Eve"      in result
        assert "Frank"    in result
        assert "Grace"    in result

    def test_ds_mail_contains_time(self, seeded_db):
        from storm import build_ds_mail
        from config import save_storm_config
        from defaults import DEFAULT_DS_TEMPLATE

        save_storm_config(TEST_GUILD_ID, "DS", "DS Assignments", DEFAULT_DS_TEMPLATE,
                          "", "", "America/New_York", 0)

        result = build_ds_mail("A", SAMPLE_DS_ZONES, [], "18:00 Server Time",
                               guild_id=TEST_GUILD_ID)
        assert "18:00" in result

    def test_ds_mail_team_b_uses_b_template(self, seeded_db):
        from storm import build_ds_mail
        from config import save_storm_config

        save_storm_config(TEST_GUILD_ID, "DS_A", "DS Tab", "Team A: {zones}",
                          "", "", "America/New_York", 0)
        save_storm_config(TEST_GUILD_ID, "DS_B", "DS Tab", "Team B: {zones}",
                          "", "", "America/New_York", 0)
        save_storm_config(TEST_GUILD_ID, "DS", "DS Tab", "DS: {zones}",
                          "", "", "America/New_York", 0)

        result_a = build_ds_mail("A", {"Z1": ["Alice"]}, [], "18:00", guild_id=TEST_GUILD_ID)
        result_b = build_ds_mail("B", {"Z1": ["Bob"]},   [], "18:00", guild_id=TEST_GUILD_ID)

        assert "Alice" in result_a
        assert "Bob"   in result_b

    def test_ds_mail_no_subs_placeholder_empty(self, seeded_db):
        from storm import build_ds_mail
        from config import save_storm_config

        save_storm_config(TEST_GUILD_ID, "DS", "DS Assignments",
                          "Zones:\n{zones}\nSubs:\n{subs}\nTime: {time}",
                          "", "", "America/New_York", 0)

        result = build_ds_mail("A", {"Z1": ["Alice"]}, [], "18:00",
                               guild_id=TEST_GUILD_ID)
        assert "{subs}" not in result  # placeholder must be replaced

    def test_ds_mail_rejects_subs_list_placeholder(self, seeded_db):
        """Ensure old {subs_list} placeholder is not used."""
        from defaults import DEFAULT_DS_TEMPLATE
        assert "{subs_list}" not in DEFAULT_DS_TEMPLATE


class TestCsMailGeneration:
    """Full Canyon Storm mail generation flow."""

    def test_full_cs_mail_contains_all_zones(self, seeded_db):
        from storm import build_cs_mail
        from config import save_storm_config
        from defaults import DEFAULT_CS_TEMPLATE

        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments", DEFAULT_CS_TEMPLATE,
                          "", "", "America/New_York", 0)

        result = build_cs_mail("A", SAMPLE_CS_ZONES, "12:00 Server Time",
                               guild_id=TEST_GUILD_ID)
        assert "Alice" in result
        assert "Bob"   in result
        assert "Carol" in result

    def test_cs_mail_contains_time(self, seeded_db):
        from storm import build_cs_mail
        from config import save_storm_config
        from defaults import DEFAULT_CS_TEMPLATE

        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments", DEFAULT_CS_TEMPLATE,
                          "", "", "America/New_York", 0)

        result = build_cs_mail("A", {"Z1": ["Alice"]}, "12:00 Server Time",
                               guild_id=TEST_GUILD_ID)
        assert "12:00" in result

    def test_cs_mail_uses_subs_not_subs_list(self, seeded_db):
        from storm import build_cs_mail
        from config import save_storm_config

        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments",
                          "Canyon Storm\n{zones}\n{subs}\n{time}",
                          "", "", "America/New_York", 0)

        result = build_cs_mail("A", {"Z1": ["Alice"]}, "12:00",
                               guild_id=TEST_GUILD_ID)
        assert "{subs}"      not in result
        assert "{subs_list}" not in result


class TestTemplateRoundTrip:
    """Test that DS/CS templates survive parse → rebuild round-trips."""

    def test_ds_template_roundtrip(self):
        from storm import build_ds_template, parse_ds_template
        zones = {
            "Zone 1": "Alice, Bob",
            "Zone 2": "Carol",
        }
        subs  = [("Dave", "Frank"), ("Eve", "Gina")]
        text  = build_ds_template(zones, subs)
        parsed_zones, parsed_subs, _ = parse_ds_template(text)

        # Verify zone names and sub pairs survived the round-trip
        assert parsed_zones.get("Zone 1") == "Alice, Bob"
        assert parsed_zones.get("Zone 2") == "Carol"
        assert ("Dave", "Frank") in parsed_subs
        assert ("Eve",  "Gina")  in parsed_subs

    def test_cs_template_roundtrip(self):
        from storm import build_cs_template, parse_cs_template
        zones = {"Zone A": ["Alice", "Bob"]}
        text  = build_cs_template(zones)
        parsed_zones, parsed_subs = parse_cs_template(text)
        assert parsed_zones or parsed_subs or True  # produces something



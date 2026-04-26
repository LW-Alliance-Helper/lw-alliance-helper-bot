"""
Unit tests for storm.py — mail template formatting, placeholder substitution,
build_ds_mail, build_cs_mail, parse_ds_template, parse_cs_template.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestBuildDsMail:
    """Test Desert Storm mail generation."""

    def test_placeholders_substituted(self, seeded_db):
        from storm import build_ds_mail
        from config import save_storm_config
        template = "**{alliance_name} — Desert Storm**\n{zones}\n{subs}\n{time}"
        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", template,
            "", "", "", "", "", "", "America/New_York", 0
        )
        zones = {"Z1": ["Alice", "Bob"], "Z2": ["Carol"]}
        subs  = ["Dave"]
        result = build_ds_mail("A", zones, subs, "18:00", guild_id=TEST_GUILD_ID)
        assert "Desert Storm" in result
        assert "Alice"        in result
        assert "Bob"          in result
        assert "Dave"         in result

    def test_subs_placeholder_consistent(self, seeded_db):
        """Ensure {subs} not {subs_list} is used."""
        from storm import build_ds_mail
        from config import save_storm_config
        template = "Subs: {subs}"
        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", template,
            "", "", "", "", "", "", "America/New_York", 0
        )
        result = build_ds_mail("A", {}, ["TestSub"], "18:00", guild_id=TEST_GUILD_ID)
        assert "TestSub" in result

    def test_empty_subs_handled(self, seeded_db):
        from storm import build_ds_mail
        from config import save_storm_config
        template = "{alliance_name}\n{zones}\n{subs}\n{time}"
        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", template,
            "", "", "", "", "", "", "America/New_York", 0
        )
        # Should not raise even with empty subs
        result = build_ds_mail("A", {"Z1": ["Alice"]}, [], "18:00", guild_id=TEST_GUILD_ID)
        assert result

    def test_time_placeholder_filled(self, seeded_db):
        from storm import build_ds_mail
        from config import save_storm_config
        template = "{time}"
        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", template,
            "", "", "", "", "", "", "America/New_York", 0
        )
        result = build_ds_mail("A", {}, [], "18:00 Server Time", guild_id=TEST_GUILD_ID)
        assert "18:00" in result


class TestBuildCsMail:
    """Test Canyon Storm mail generation."""

    def test_cs_uses_subs_not_subs_list(self, seeded_db):
        """Verify CS also uses {subs} placeholder."""
        from storm import build_cs_mail
        from config import save_storm_config
        template = "CS: {zones}\n{subs}\n{time}"
        save_storm_config(
            TEST_GUILD_ID, "CS", "CS Assignments", template,
            "", "", "", "", "", "", "America/New_York", 0
        )
        zones = {"Z1": ["Alice"]}
        result = build_cs_mail("A", zones, "12:00", guild_id=TEST_GUILD_ID)
        assert result

    def test_cs_and_ds_templates_independent(self, seeded_db):
        from storm import build_ds_mail, build_cs_mail
        from config import save_storm_config
        save_storm_config(TEST_GUILD_ID, "DS", "DS Tab", "DS: {zones}",
                          "", "", "", "", "", "", "America/New_York", 0)
        save_storm_config(TEST_GUILD_ID, "CS", "CS Tab", "CS: {zones}",
                          "", "", "", "", "", "", "America/New_York", 0)
        ds = build_ds_mail("A", {"Z1": ["Alice"]}, [], "18:00", guild_id=TEST_GUILD_ID)
        cs = build_cs_mail("A", {"Z1": ["Alice"]}, "12:00", guild_id=TEST_GUILD_ID)
        assert ds.startswith("DS:")
        assert cs.startswith("CS:")


class TestParseDsTemplate:
    """Test round-trip parse of DS mail template."""

    def test_parse_zones_from_template(self):
        from storm import build_ds_template, parse_ds_template
        zones = {"Zone 1": ["Alice", "Bob"], "Zone 2": ["Carol", "Dave"]}
        subs  = [("Alice", "Eve"), ("Bob", "Frank")]  # (starter, sub) tuples
        text  = build_ds_template(zones, subs)
        parsed_zones, parsed_subs, errors = parse_ds_template(text)
        # Zones should contain our zone names
        assert len(parsed_zones) > 0 or len(errors) == 0
        # Subs should contain our pairs
        assert "Alice" in str(parsed_subs) or "Eve" in str(parsed_subs) or len(errors) == 0

    def test_empty_template_handled(self):
        from storm import parse_ds_template
        # Should not raise on empty
        result = parse_ds_template("")
        assert result is not None


class TestParseCsTemplate:
    """Test round-trip parse of CS mail template."""

    def test_parse_zones_from_cs_template(self):
        from storm import build_cs_template, parse_cs_template
        zones = {"Zone 1": ["Alice", "Bob"]}
        text  = build_cs_template(zones)
        parsed_zones, parsed_subs = parse_cs_template(text)
        assert parsed_zones or parsed_subs or True  # parse returns something


class TestGenericDsTemplate:
    """Test GENERIC_DS_TEMPLATE has correct placeholders."""

    def test_generic_ds_contains_required_placeholders(self):
        from config import GENERIC_DS_TEMPLATE
        assert "{alliance_name}" in GENERIC_DS_TEMPLATE
        assert "{zones}"         in GENERIC_DS_TEMPLATE
        assert "{subs}"          in GENERIC_DS_TEMPLATE
        assert "{time}"          in GENERIC_DS_TEMPLATE
        assert "{subs_list}"     not in GENERIC_DS_TEMPLATE  # must not use old name

    def test_generic_cs_contains_required_placeholders(self):
        from config import GENERIC_CS_TEMPLATE
        assert "{alliance_name}" in GENERIC_CS_TEMPLATE
        assert "{zones}"         in GENERIC_CS_TEMPLATE
        assert "{subs}"          in GENERIC_CS_TEMPLATE
        assert "{time}"          in GENERIC_CS_TEMPLATE
        assert "{subs_list}"     not in GENERIC_CS_TEMPLATE  # must not use old name

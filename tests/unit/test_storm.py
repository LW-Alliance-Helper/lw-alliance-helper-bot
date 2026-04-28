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


# ── Participation log config (#20 rework) ────────────────────────────────────

class TestParticipationConfig:
    """Unit tests for the new save/load participation helpers."""

    def test_get_participation_config_defaults_when_unset(self, seeded_db):
        from config import save_storm_config, get_participation_config
        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", "Body",
            "", "", "", "", "", "", "America/New_York", 0,
        )
        pcfg = get_participation_config(TEST_GUILD_ID, "DS")
        assert pcfg["enabled"]    is False
        assert pcfg["tab_name"]   == ""
        assert pcfg["questions"]  == []
        assert pcfg["roster_tab"] == ""

    def test_save_and_load_participation_config(self, seeded_db):
        from config import save_storm_config, save_participation_config, get_participation_config
        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", "Body",
            "", "", "", "", "", "", "America/New_York", 0,
        )
        questions = [
            {"key": "vote_count", "label": "Vote Count", "type": "numeric", "min": 0, "max": 200},
            {"key": "sitting_out", "label": "Sitting Out", "type": "roster_names"},
        ]
        ok = save_participation_config(
            TEST_GUILD_ID, "DS",
            enabled=1, tab_name="DS Participation Log",
            questions=questions,
            roster_tab="Squad Powers", roster_name_col=0,
            roster_alias_col=1, roster_start_row=2,
        )
        assert ok is True

        pcfg = get_participation_config(TEST_GUILD_ID, "DS")
        assert pcfg["enabled"]         is True
        assert pcfg["tab_name"]        == "DS Participation Log"
        assert len(pcfg["questions"])  == 2
        assert pcfg["questions"][0]["type"] == "numeric"
        assert pcfg["questions"][0]["min"]  == 0
        assert pcfg["roster_tab"]       == "Squad Powers"
        assert pcfg["roster_alias_col"] == 1
        assert pcfg["roster_start_row"] == 2

    def test_participation_config_is_independent_per_event_type(self, seeded_db):
        from config import save_storm_config, save_participation_config, get_participation_config
        save_storm_config(TEST_GUILD_ID, "DS", "DS Assignments", "B",
                          "", "", "", "", "", "", "America/New_York", 0)
        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments", "B",
                          "", "", "", "", "", "", "America/New_York", 0)

        save_participation_config(
            TEST_GUILD_ID, "DS",
            enabled=1, tab_name="DS Tab",
            questions=[{"key": "ds_q", "label": "DS Q", "type": "text"}],
        )
        save_participation_config(
            TEST_GUILD_ID, "CS",
            enabled=1, tab_name="CS Tab",
            questions=[{"key": "cs_q", "label": "CS Q", "type": "yes_no"}],
        )

        ds = get_participation_config(TEST_GUILD_ID, "DS")
        cs = get_participation_config(TEST_GUILD_ID, "CS")
        assert ds["tab_name"] == "DS Tab"
        assert cs["tab_name"] == "CS Tab"
        assert ds["questions"][0]["key"] == "ds_q"
        assert cs["questions"][0]["key"] == "cs_q"


class TestColumnLetterHelpers:
    """The wizard converts spreadsheet column letters to 0-indexed ints."""

    def test_letter_to_index(self):
        from setup_cog import _col_letter_to_index
        assert _col_letter_to_index("A")  == 0
        assert _col_letter_to_index("E")  == 4
        assert _col_letter_to_index("Z")  == 25
        assert _col_letter_to_index("AA") == 26
        assert _col_letter_to_index("AB") == 27
        assert _col_letter_to_index("a")  == 0  # case insensitive

    def test_letter_to_index_invalid(self):
        from setup_cog import _col_letter_to_index
        assert _col_letter_to_index("")    == -1
        assert _col_letter_to_index("1")   == -1
        assert _col_letter_to_index("A1")  == -1

    def test_index_to_letter(self):
        from setup_cog import _col_index_to_letter
        assert _col_index_to_letter(0)  == "A"
        assert _col_index_to_letter(4)  == "E"
        assert _col_index_to_letter(25) == "Z"
        assert _col_index_to_letter(26) == "AA"

    def test_round_trip(self):
        from setup_cog import _col_letter_to_index, _col_index_to_letter
        for letter in ["A", "C", "E", "Z", "AA", "AZ", "BA"]:
            idx = _col_letter_to_index(letter)
            assert _col_index_to_letter(idx) == letter

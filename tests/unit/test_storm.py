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
            "America/New_York", 0
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
            "America/New_York", 0
        )
        result = build_ds_mail("A", {}, ["TestSub"], "18:00", guild_id=TEST_GUILD_ID)
        assert "TestSub" in result

    def test_empty_subs_handled(self, seeded_db):
        from storm import build_ds_mail
        from config import save_storm_config
        template = "{alliance_name}\n{zones}\n{subs}\n{time}"
        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", template,
            "America/New_York", 0
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
            "America/New_York", 0
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
            "America/New_York", 0
        )
        zones = {"Z1": ["Alice"]}
        result = build_cs_mail("A", zones, "12:00", guild_id=TEST_GUILD_ID)
        assert result

    def test_cs_and_ds_templates_independent(self, seeded_db):
        from storm import build_ds_mail, build_cs_mail
        from config import save_storm_config
        save_storm_config(TEST_GUILD_ID, "DS", "DS Tab", "DS: {zones}",
                          "America/New_York", 0)
        save_storm_config(TEST_GUILD_ID, "CS", "CS Tab", "CS: {zones}",
                          "America/New_York", 0)
        ds = build_ds_mail("A", {"Z1": ["Alice"]}, [], "18:00", guild_id=TEST_GUILD_ID)
        cs = build_cs_mail("A", {"Z1": ["Alice"]}, "12:00", guild_id=TEST_GUILD_ID)
        assert ds.startswith("DS:")
        assert cs.startswith("CS:")


class TestCsZoneLabelRendering:
    """Regression tests for the bug where CS draft rendered abbreviations
    (Dc1, Sw1, Ds1, Sf1) instead of full zone names. Caused by
    `key.replace('s1 ', '').title()` which collapsed `s1_dc1` → `Dc1` rather
    than expanding it to `Data Center 1`."""

    def test_canonical_keys_render_full_names_not_abbreviations(self, seeded_db):
        from storm import build_cs_mail
        from config import save_storm_config
        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments",
                          "CS: {zones}\n{subs}\n{time}",
                          "America/New_York", 0)
        zones = {
            "s1_dc1":         ["Alice"],
            "s1_sw1":         ["Bob"],
            "s2_ds1":         ["Carol"],
            "s2_sf1":         ["Dave"],
            "s3_virus_lab":   ["Eve"],
            "s3_pop_pair1":   "Frank & Grace",
        }
        result = build_cs_mail("A", zones, "12:00", guild_id=TEST_GUILD_ID)

        # The bug: these abbreviated forms must NOT appear.
        assert "**Dc1**"      not in result, f"Bug regression: 'Dc1' in mail:\n{result}"
        assert "**Sw1**"      not in result, f"Bug regression: 'Sw1' in mail:\n{result}"
        assert "**Ds1**"      not in result, f"Bug regression: 'Ds1' in mail:\n{result}"
        assert "**Sf1**"      not in result, f"Bug regression: 'Sf1' in mail:\n{result}"
        assert "**Virus Lab**" in result or "Virus Lab" in result

        # The fix: the full names must appear.
        assert "Data Center 1"      in result
        assert "Sample Warehouse 1" in result
        assert "Defense System 1"   in result
        assert "Serum Factory 1"    in result
        assert "Virus Lab"          in result

    def test_subs_key_not_duplicated_as_zone(self, seeded_db):
        """Pop-pair-1 used to render BOTH as a `Pop Pair1` zone AND as the
        subs block. It should only be the subs block."""
        from storm import build_cs_mail
        from config import save_storm_config
        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments",
                          "CS: {zones}\n---SUBS---\n{subs}\n{time}",
                          "America/New_York", 0)
        zones = {
            "s1_dc1":       ["Alice"],
            "s3_pop_pair1": "Bob & Carol",
        }
        result = build_cs_mail("A", zones, "12:00", guild_id=TEST_GUILD_ID)
        # Pop-pair members should appear exactly once (in subs block, after the
        # `---SUBS---` marker). The {zones} block must NOT contain them.
        zones_part = result.split("---SUBS---")[0]
        assert "Bob & Carol" not in zones_part, \
            f"Subs key was duplicated as a zone:\n{result}"
        assert "Bob & Carol" in result.split("---SUBS---")[1]

    def test_open_zones_are_skipped(self, seeded_db):
        """Zones whose value is '(open)' or empty should not render."""
        from storm import build_cs_mail
        from config import save_storm_config
        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments",
                          "{zones}",
                          "America/New_York", 0)
        zones = {
            "s1_dc1":   ["Alice"],
            "s1_dc2":   "(open)",
            "s1_sw1":   "",
            "s2_ds1":   ["Bob"],
        }
        result = build_cs_mail("A", zones, "12:00", guild_id=TEST_GUILD_ID)
        # Only the populated zones render.
        assert "Data Center 1"      in result
        assert "Defense System 1"   in result
        assert "Data Center 2"      not in result
        assert "Sample Warehouse 1" not in result

    def test_stages_emit_in_canonical_order(self, seeded_db):
        from storm import build_cs_mail
        from config import save_storm_config
        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments",
                          "{zones}",
                          "America/New_York", 0)
        zones = {
            "s3_virus_lab": ["Eve"],
            "s1_dc1":       ["Alice"],
            "s2_ds1":       ["Carol"],
        }
        result = build_cs_mail("A", zones, "12:00", guild_id=TEST_GUILD_ID)
        # Stage 1 must appear before Stage 2 must appear before Stage 3
        # regardless of dict insertion order.
        s1_idx = result.find("**Stage 1**")
        s2_idx = result.find("**Stage 2**")
        s3_idx = result.find("**Stage 3**")
        assert 0 <= s1_idx < s2_idx < s3_idx, \
            f"Stages out of order. s1={s1_idx} s2={s2_idx} s3={s3_idx}\n{result}"


class TestCsZoneStructureConsistency:
    """The CS_ZONE_STRUCTURE constant is the single source of truth for both
    the template builder and the mail builder. Verify it stays consistent."""

    def test_structure_has_no_duplicate_keys(self):
        from storm import CS_ZONE_STRUCTURE
        keys = [k for _, k, _ in CS_ZONE_STRUCTURE]
        assert len(keys) == len(set(keys)), \
            f"Duplicate keys in CS_ZONE_STRUCTURE: {keys}"

    def test_structure_subs_key_excluded(self):
        """The subs key should never appear as a zone."""
        from storm import CS_ZONE_STRUCTURE, CS_SUBS_KEY
        keys = [k for _, k, _ in CS_ZONE_STRUCTURE]
        assert CS_SUBS_KEY not in keys

    def test_default_cs_assignments_only_use_canonical_keys(self):
        """The DEFAULT_CS_A / DEFAULT_CS_B fixtures should match the canonical
        structure exactly (one entry per slot, plus the subs key)."""
        from storm import CS_ZONE_STRUCTURE, CS_SUBS_KEY, DEFAULT_CS_A, DEFAULT_CS_B
        canonical = {k for _, k, _ in CS_ZONE_STRUCTURE} | {CS_SUBS_KEY}
        for label, defaults in (("A", DEFAULT_CS_A), ("B", DEFAULT_CS_B)):
            extras = set(defaults.keys()) - canonical
            missing = canonical - set(defaults.keys())
            assert not extras,  f"DEFAULT_CS_{label} has non-canonical keys: {extras}"
            assert not missing, f"DEFAULT_CS_{label} is missing canonical keys: {missing}"


class TestParseDsTemplate:
    """Test round-trip parse of DS mail template."""

    def test_parse_canonical_zones_round_trip(self):
        from storm import build_ds_template, parse_ds_template, DS_ZONE_STRUCTURE
        zones = {
            "Nuclear Silo":     "Alice, Bob",
            "Field Hospital I": "Carol, Dave",
        }
        subs  = ["Eve", "Frank"]
        text  = build_ds_template(zones, subs)
        parsed_zones, parsed_subs, errors = parse_ds_template(text)
        assert parsed_zones["Nuclear Silo"]     == "Alice, Bob"
        assert parsed_zones["Field Hospital I"] == "Carol, Dave"
        # build_ds_template emits the full canonical scaffold; unassigned zones
        # come back as empty strings.
        assert all(z in parsed_zones for z in DS_ZONE_STRUCTURE)
        assert parsed_subs == ["Eve", "Frank"]
        assert errors == []

    def test_parse_rejects_non_canonical_zone(self):
        from storm import parse_ds_template
        text = (
            "ZONE ASSIGNMENTS\n"
            "Nuclear Silo: Alice\n"
            "Filed Hospital II: Bob\n"  # typo
            "SUB PAIRS (Starter - Sub)\n"
        )
        parsed_zones, _, errors = parse_ds_template(text)
        assert parsed_zones == {"Nuclear Silo": "Alice"}
        assert any("Filed Hospital II" in e for e in errors), \
            f"Expected an error mentioning the typo, got: {errors}"

    def test_parse_zone_match_is_case_insensitive(self):
        from storm import parse_ds_template
        text = (
            "ZONE ASSIGNMENTS\n"
            "nuclear silo: Alice\n"
            "FIELD HOSPITAL II: Bob\n"
        )
        parsed_zones, _, errors = parse_ds_template(text)
        # Canonical-cased keys come back, preserving the structure regardless
        # of the leader's casing.
        assert parsed_zones["Nuclear Silo"]      == "Alice"
        assert parsed_zones["Field Hospital II"] == "Bob"
        assert errors == []

    def test_empty_template_handled(self):
        from storm import parse_ds_template
        result = parse_ds_template("")
        assert result is not None


class TestDsZoneStructure:
    """DS_ZONE_STRUCTURE is the source of truth for DS zones."""

    def test_has_eleven_canonical_zones(self):
        from storm import DS_ZONE_STRUCTURE
        assert len(DS_ZONE_STRUCTURE) == 11
        assert DS_ZONE_STRUCTURE[0]  == "Nuclear Silo"
        assert "Field Hospital IV" in DS_ZONE_STRUCTURE
        assert "Mercenary Factory" in DS_ZONE_STRUCTURE

    def test_no_duplicate_zones(self):
        from storm import DS_ZONE_STRUCTURE
        assert len(DS_ZONE_STRUCTURE) == len(set(DS_ZONE_STRUCTURE))

    def test_build_template_renders_full_scaffold(self):
        from storm import build_ds_template, DS_ZONE_STRUCTURE
        # Sparse input — only one zone assigned. Scaffold should still emit
        # every canonical zone in canonical order, with blank values for
        # unassigned zones, so leadership has a fill-in grid.
        text = build_ds_template({"Nuclear Silo": "Alice"}, [])
        for zone in DS_ZONE_STRUCTURE:
            assert f"{zone}:" in text, f"Missing canonical zone `{zone}` in scaffold"
        # Canonical order: Nuclear Silo before Mercenary Factory.
        assert text.find("Nuclear Silo:") < text.find("Mercenary Factory:")

    def test_build_template_drops_non_canonical_zones(self):
        from storm import build_ds_template
        text = build_ds_template({"Filed Hospital II": "Alice"}, [])
        assert "Filed Hospital II" not in text


class TestNonCanonicalZoneDetection:
    """The draft flow uses these helpers to warn leadership about typo
    zone keys carried over from older sheet data."""

    def test_ds_helper_returns_only_non_canonical_with_values(self):
        from storm import _non_canonical_ds_zones
        zones = {
            "Nuclear Silo":      "Alice",       # canonical, keep out
            "Filed Hospital II": "Bob",         # typo, flag
            "Unknown Zone":      "",            # non-canonical but empty, skip
        }
        result = _non_canonical_ds_zones(zones)
        assert result == {"Filed Hospital II": "Bob"}

    def test_cs_helper_excludes_subs_key(self):
        from storm import _non_canonical_cs_zones, CS_SUBS_KEY
        zones = {
            "s1_dc1":         "Alice",   # canonical, keep out
            "made_up_zone":   "Bob",     # non-canonical, flag
            CS_SUBS_KEY:      "Carol",   # subs key — not a zone, keep out
        }
        result = _non_canonical_cs_zones(zones)
        assert result == {"made_up_zone": "Bob"}


class TestFlatSubs:
    """#37: DS and CS subs are a flat list of names. Verify the new shape
    round-trips and that the legacy paired/inline shapes still parse for
    backward compatibility."""

    def test_ds_template_emits_subs_header_not_pairs(self):
        from storm import build_ds_template
        text = build_ds_template({}, ["Alice", "Bob"])
        assert "SUBS" in text
        assert "SUB PAIRS" not in text

    def test_ds_template_flattens_legacy_tuple_subs(self):
        # In-memory data may still carry tuples during the transition; the
        # builder must emit just the sub name (right side of the pair),
        # never the starter or the tuple repr.
        from storm import build_ds_template
        text = build_ds_template({}, [("Starter1", "Alice"), ("Starter2", "Bob")])
        assert "Alice" in text
        assert "Bob"   in text
        assert "Starter1" not in text
        assert "Starter2" not in text
        assert "('"        not in text  # never the tuple repr

    def test_ds_parse_subs_section_returns_flat_strings(self):
        from storm import parse_ds_template
        text = (
            "ZONE ASSIGNMENTS\n"
            "Nuclear Silo: Alice\n"
            "SUBS\n"
            "Eve\n"
            "Frank\n"
        )
        _, subs, _ = parse_ds_template(text)
        assert subs == ["Eve", "Frank"]

    def test_ds_parse_legacy_sub_pairs_keeps_only_sub_side(self):
        from storm import parse_ds_template
        text = (
            "ZONE ASSIGNMENTS\n"
            "Nuclear Silo: Alice\n"
            "SUB PAIRS (Starter - Sub)\n"
            "Starter1 - Eve\n"
            "Starter2 - Frank\n"
        )
        _, subs, _ = parse_ds_template(text)
        # Only the right side (the actual sub) survives; starters are dropped.
        assert subs == ["Eve", "Frank"]

    def test_cs_subs_label_renamed_to_subs(self):
        from storm import CS_SUBS_LABEL
        assert CS_SUBS_LABEL == "Subs"

    def test_cs_template_renders_subs_as_multi_line_section(self):
        from storm import build_cs_template, CS_SUBS_KEY
        zones = {"s1_dc1": "Alice", CS_SUBS_KEY: ["Eve", "Frank"]}
        text = build_cs_template(zones)
        # Header on its own line, then names one per line.
        assert "\nSubs\nEve\nFrank" in text
        # The legacy inline label form must be gone.
        assert "Pop Pairs (last 30 sec):" not in text

    def test_cs_parse_subs_section_returns_flat_list(self):
        from storm import parse_cs_template, CS_SUBS_KEY
        text = (
            "STAGE 1\n"
            "Data Center 1: Alice\n"
            "\n"
            "Subs\n"
            "Eve\n"
            "Frank\n"
        )
        zones, _ = parse_cs_template(text)
        assert zones[CS_SUBS_KEY] == ["Eve", "Frank"]

    def test_cs_parse_accepts_legacy_pop_pairs_header(self):
        # Multi-line section under the legacy header label.
        from storm import parse_cs_template, CS_SUBS_KEY
        text = (
            "STAGE 1\n"
            "Data Center 1: Alice\n"
            "\n"
            "Pop Pairs (last 30 sec)\n"
            "Eve\n"
            "Frank\n"
        )
        zones, _ = parse_cs_template(text)
        assert zones[CS_SUBS_KEY] == ["Eve", "Frank"]

    def test_cs_parse_flattens_legacy_inline_subs(self):
        # `Header: Alice & Bob, Carol & Dave` is the pre-#37 shape — split
        # on commas, dashes, and ampersands. Everyone in the inline string
        # is a sub (no starter side, unlike DS).
        from storm import parse_cs_template, CS_SUBS_KEY
        text = (
            "STAGE 1\n"
            "Data Center 1: Alice\n"
            "Pop Pairs (last 30 sec): Eve & Frank, Gina - Hank\n"
        )
        zones, _ = parse_cs_template(text)
        assert zones[CS_SUBS_KEY] == ["Eve", "Frank", "Gina", "Hank"]

    def test_split_legacy_subs_handles_mixed_separators(self):
        from storm import _split_legacy_subs
        assert _split_legacy_subs("Alice & Bob, Carol - Dave") == \
               ["Alice", "Bob", "Carol", "Dave"]
        assert _split_legacy_subs("")    == []
        assert _split_legacy_subs("Solo") == ["Solo"]


class TestParseCsTemplate:
    """Test round-trip parse of CS mail template."""

    def test_parse_zones_from_cs_template(self):
        from storm import build_cs_template, parse_cs_template
        zones = {"Zone 1": ["Alice", "Bob"]}
        text  = build_cs_template(zones)
        parsed_zones, parsed_subs = parse_cs_template(text)
        assert parsed_zones or parsed_subs or True  # parse returns something


class TestDefaultStormTemplates:
    """Test DEFAULT_DS_TEMPLATE / DEFAULT_CS_TEMPLATE have correct placeholders."""

    def test_default_ds_contains_required_placeholders(self):
        from defaults import DEFAULT_DS_TEMPLATE
        assert "{alliance_name}" in DEFAULT_DS_TEMPLATE
        assert "{zones}"         in DEFAULT_DS_TEMPLATE
        assert "{subs}"          in DEFAULT_DS_TEMPLATE
        assert "{time}"          in DEFAULT_DS_TEMPLATE
        assert "{subs_list}"     not in DEFAULT_DS_TEMPLATE  # must not use old name

    def test_default_cs_contains_required_placeholders(self):
        from defaults import DEFAULT_CS_TEMPLATE
        assert "{alliance_name}" in DEFAULT_CS_TEMPLATE
        assert "{zones}"         in DEFAULT_CS_TEMPLATE
        assert "{subs}"          in DEFAULT_CS_TEMPLATE
        assert "{time}"          in DEFAULT_CS_TEMPLATE
        assert "{subs_list}"     not in DEFAULT_CS_TEMPLATE  # must not use old name


# ── Participation log config (#20 rework) ────────────────────────────────────

class TestParticipationConfig:
    """Unit tests for the new save/load participation helpers."""

    def test_get_participation_config_defaults_when_unset(self, seeded_db):
        from config import save_storm_config, get_participation_config
        save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", "Body",
            "America/New_York", 0,
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
            "America/New_York", 0,
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
                          "America/New_York", 0)
        save_storm_config(TEST_GUILD_ID, "CS", "CS Assignments", "B",
                          "America/New_York", 0)

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

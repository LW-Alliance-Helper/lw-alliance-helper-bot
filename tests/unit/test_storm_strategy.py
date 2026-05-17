"""
Tests for storm_strategy.py (#126).

Covers:
  * power magnitude parsing + formatting
  * PresetBuffer mutation (upsert, remove, dirty flag)
  * canonical_zones_for + seed_default_preset
  * Sheet I/O round-trips (load_preset / save_preset / list_presets /
    delete_preset) using a fake gspread spreadsheet

The slash command handlers + editor view are integration territory and
not unit-tested here.
"""

import pytest
from unittest.mock import patch

import storm_strategy as ss


class TestPowerParsing:
    def test_basic_int_string(self):
        assert ss.parse_power("300") == 300

    def test_m_suffix(self):
        assert ss.parse_power("250M") == 250_000_000
        assert ss.parse_power("250m") == 250_000_000

    def test_k_suffix(self):
        assert ss.parse_power("12K") == 12_000

    def test_b_suffix(self):
        assert ss.parse_power("1.2B") == 1_200_000_000

    def test_with_commas(self):
        assert ss.parse_power("300,000,000") == 300_000_000

    def test_empty_string_returns_zero(self):
        assert ss.parse_power("") == 0

    def test_garbage_returns_none(self):
        assert ss.parse_power("abc") is None
        assert ss.parse_power("12xyz") is None

    def test_round_trip_format(self):
        assert ss.format_power(250_000_000) == "250M"
        assert ss.format_power(1_200_000_000) == "1.2B"
        assert ss.format_power(12_000) == "12K"
        assert ss.format_power(0) == "0"


class TestParsePowerCell:
    """`_parse_power_cell` is the new helper that distinguishes "blank"
    from "garbage" so the loader doesn't silently turn a typo'd Sheet
    cell into a `0` floor (which would pass the eligibility filter for
    everyone — directly contradicting the design's "exclude unknown
    power" rule)."""

    def test_blank_is_not_garbage(self):
        value, bad = ss._parse_power_cell("")
        assert value == 0
        assert bad is False

    def test_none_is_not_garbage(self):
        value, bad = ss._parse_power_cell(None)
        assert value == 0
        assert bad is False

    def test_valid_power_string_parses(self):
        value, bad = ss._parse_power_cell("250M")
        assert value == 250_000_000
        assert bad is False

    def test_garbage_string_flags_as_bad(self):
        value, bad = ss._parse_power_cell("tbd")
        assert value == 0
        assert bad is True

    def test_na_value_flags_as_bad(self):
        # "n/a" / "tbd" are common alliance shorthand for "not set yet"
        # — they were silently becoming `0` (= no floor) before this fix.
        for token in ("n/a", "tbd", "?", "later"):
            value, bad = ss._parse_power_cell(token)
            assert value == 0, f"{token!r} should not parse to a number"
            assert bad is True, f"{token!r} should flag as bad"


class TestSafeInt:
    """`_safe_int` replaces the `int(value or 0)` idiom that raised
    ValueError on truthy garbage strings ("abc" is truthy, falls
    through to int())."""

    def test_int_passthrough(self):
        assert ss._safe_int(5) == 5

    def test_blank_returns_default(self):
        assert ss._safe_int("") == 0
        assert ss._safe_int(None) == 0

    def test_numeric_string_parses(self):
        assert ss._safe_int("42") == 42

    def test_float_string_truncates(self):
        assert ss._safe_int("3.7") == 3

    def test_garbage_returns_default_not_raises(self):
        assert ss._safe_int("abc") == 0
        assert ss._safe_int("abc", default=-1) == -1


class TestPresetBuffer:
    def test_upsert_new_zone(self):
        buf = ss.PresetBuffer(name="P", event_type="DS")
        buf.upsert_zone(ss.ZoneRow(zone="Power Tower", max_players=4))
        assert len(buf.zones) == 1
        assert buf.dirty is True

    def test_upsert_existing_zone_updates_in_place(self):
        buf = ss.PresetBuffer(name="P", event_type="DS",
                              zones=[ss.ZoneRow(zone="Power Tower", max_players=2)])
        buf.dirty = False
        buf.upsert_zone(ss.ZoneRow(zone="Power Tower", max_players=4, min_power_a=300_000_000))
        assert len(buf.zones) == 1
        assert buf.zones[0].max_players == 4
        assert buf.zones[0].min_power_a == 300_000_000
        assert buf.dirty is True

    def test_remove_zone_returns_true_when_found(self):
        buf = ss.PresetBuffer(name="P", event_type="DS",
                              zones=[ss.ZoneRow(zone="Power Tower")])
        buf.dirty = False
        assert buf.remove_zone("Power Tower") is True
        assert len(buf.zones) == 0
        assert buf.dirty is True

    def test_remove_zone_returns_false_when_missing(self):
        buf = ss.PresetBuffer(name="P", event_type="DS")
        assert buf.remove_zone("Power Tower") is False
        assert buf.dirty is False

    def test_total_capacity(self):
        buf = ss.PresetBuffer(name="P", event_type="DS", zones=[
            ss.ZoneRow(zone="A", max_players=4),
            ss.ZoneRow(zone="B", max_players=4),
            ss.ZoneRow(zone="C", max_players=2),
        ])
        assert buf.total_capacity() == 10

    def test_find_zone_case_insensitive(self):
        buf = ss.PresetBuffer(name="P", event_type="DS",
                              zones=[ss.ZoneRow(zone="Power Tower")])
        assert buf.find_zone("power tower") is not None
        assert buf.find_zone("POWER TOWER") is not None
        assert buf.find_zone("nothing") is None


class TestCanonicalSeed:
    def test_ds_seed_has_canonical_zones(self):
        import storm
        buf = ss.seed_default_preset("Test", "DS")
        assert [z.zone for z in buf.zones] == list(storm.DS_ZONE_STRUCTURE)

    def test_cs_seed_has_canonical_zones(self):
        import storm
        buf = ss.seed_default_preset("Test", "CS")
        cs_names = [name for _, name, _ in storm.CS_ZONE_STRUCTURE]
        assert [z.zone for z in buf.zones] == cs_names

    def test_seed_zones_have_zero_max_initially(self):
        buf = ss.seed_default_preset("Test", "DS")
        assert all(z.max_players == 0 for z in buf.zones)


# ── Sheet I/O ────────────────────────────────────────────────────────────────


class _FakeWorksheet:
    """Tiny gspread-shaped fake for Sheet I/O tests."""

    def __init__(self, title: str, rows: list[list[str]] | None = None):
        self.title = title
        self._rows = list(rows or [])
        self.update_calls: list[tuple[str, list]] = []

    def get_all_records(self):
        if not self._rows:
            return []
        header = self._rows[0]
        return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in self._rows[1:]]

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def append_row(self, row, value_input_option=None):
        self._rows.append([str(c) for c in row])

    def clear(self):
        self._rows = []

    def update(self, range_, values, value_input_option=None):
        # Range_ assumed A1; rewrite from top.
        self.update_calls.append((range_, values))
        self._rows = [list(r) for r in values]


class _FakeSpreadsheet:
    def __init__(self):
        self._tabs: dict[str, _FakeWorksheet] = {}

    def worksheet(self, title: str):
        if title not in self._tabs:
            raise Exception(f"Worksheet '{title}' not found")
        return self._tabs[title]

    def add_worksheet(self, title: str, rows: int = 0, cols: int = 0):
        ws = _FakeWorksheet(title)
        self._tabs[title] = ws
        return ws


@pytest.fixture
def fake_sheet_factory(seeded_db):
    """Yield a function (event_type) -> _FakeSpreadsheet, plus a way to
    plug it into config.get_spreadsheet. Pre-enables the structured
    flow so strategies_tab is configured."""
    import config
    fake = _FakeSpreadsheet()

    # Ensure the guild has a storm row to attach structured-flow config to.
    from tests.unit.test_config import TEST_GUILD_ID
    for et in ("DS", "CS"):
        config.save_storm_config(
            TEST_GUILD_ID, et,
            tab_name=f"{et} Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, et,
            structured_flow_enabled=True,
        )

    with patch.object(config, "get_spreadsheet", return_value=fake):
        yield fake, TEST_GUILD_ID


class TestSheetRoundTrip:
    def test_save_then_load_ds(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        buf = ss.PresetBuffer(name="Standard", event_type="DS", zones=[
            ss.ZoneRow(zone="Nuclear Silo", max_players=4,
                       min_power_a=300_000_000, min_power_b=180_000_000, priority=1),
            ss.ZoneRow(zone="Oil Refinery I", max_players=4,
                       min_power_a=250_000_000, min_power_b=150_000_000, priority=2),
        ])
        ok = ss.save_preset(gid, "DS", buf)
        assert ok is True

        loaded = ss.load_preset(gid, "DS", "Standard")
        assert loaded is not None
        assert loaded.name == "Standard"
        assert len(loaded.zones) == 2
        assert loaded.zones[0].zone == "Nuclear Silo"
        assert loaded.zones[0].max_players == 4
        assert loaded.zones[0].min_power_a == 300_000_000
        assert loaded.zones[0].min_power_b == 180_000_000
        assert loaded.zones[0].priority    == 1

    def test_save_then_load_cs(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        buf = ss.PresetBuffer(name="Rulebringers", event_type="CS", faction="Rulebringers", zones=[
            ss.ZoneRow(zone="Some CS Zone", max_players=4,
                       min_power_a=250_000_000, priority=1),
        ])
        ok = ss.save_preset(gid, "CS", buf)
        assert ok is True

        loaded = ss.load_preset(gid, "CS", "Rulebringers")
        assert loaded is not None
        assert loaded.faction == "Rulebringers"
        assert loaded.zones[0].min_power_a == 250_000_000
        # CS rows don't use min_power_b
        assert loaded.zones[0].min_power_b == 0

    def test_list_presets_returns_unique_names(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        for name in ("Standard", "Saturation"):
            buf = ss.PresetBuffer(name=name, event_type="DS", zones=[
                ss.ZoneRow(zone="Nuclear Silo", max_players=4),
            ])
            ss.save_preset(gid, "DS", buf)
        names = ss.list_presets(gid, "DS")
        assert set(names) == {"Standard", "Saturation"}

    def test_delete_preset_removes_rows(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        for name in ("Standard", "Saturation"):
            buf = ss.PresetBuffer(name=name, event_type="DS", zones=[
                ss.ZoneRow(zone="Nuclear Silo", max_players=4),
            ])
            ss.save_preset(gid, "DS", buf)
        assert ss.delete_preset(gid, "DS", "Standard") is True
        assert set(ss.list_presets(gid, "DS")) == {"Saturation"}
        # Re-deleting a missing preset returns False.
        assert ss.delete_preset(gid, "DS", "Standard") is False

    def test_save_replaces_prior_rows_for_same_name(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        buf = ss.PresetBuffer(name="Standard", event_type="DS", zones=[
            ss.ZoneRow(zone="Nuclear Silo", max_players=4),
            ss.ZoneRow(zone="Oil Refinery I", max_players=4),
        ])
        ss.save_preset(gid, "DS", buf)
        # Re-save with one fewer zone — old row should be gone.
        buf2 = ss.PresetBuffer(name="Standard", event_type="DS", zones=[
            ss.ZoneRow(zone="Nuclear Silo", max_players=4),
        ])
        ss.save_preset(gid, "DS", buf2)
        loaded = ss.load_preset(gid, "DS", "Standard")
        assert len(loaded.zones) == 1

    def test_load_missing_preset_returns_none(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        assert ss.load_preset(gid, "DS", "Nope") is None

    def test_load_with_no_sheet_returns_none(self, seeded_db):
        """If get_spreadsheet returns None, load_preset returns None
        rather than crashing."""
        import config
        from tests.unit.test_config import TEST_GUILD_ID
        with patch.object(config, "get_spreadsheet", return_value=None):
            assert ss.load_preset(TEST_GUILD_ID, "DS", "Anything") is None
            assert ss.list_presets(TEST_GUILD_ID, "DS") == []


# ── #148: configured-teams gate ─────────────────────────────────────────────


class TestZoneRowRenderLine:
    def test_ds_both_renders_both_floors(self):
        row = ss.ZoneRow(zone="Nuclear Silo", max_players=4,
                         min_power_a=300_000_000, min_power_b=180_000_000)
        line = row.render_line("DS", teams="both")
        assert "Min A:" in line
        assert "Min B:" in line
        assert "300M" in line
        assert "180M" in line

    def test_ds_a_only_drops_min_b(self):
        row = ss.ZoneRow(zone="Nuclear Silo", max_players=4,
                         min_power_a=300_000_000, min_power_b=180_000_000)
        line = row.render_line("DS", teams="A")
        assert "Min:" in line
        assert "300M" in line
        assert "Min A:" not in line
        assert "Min B:" not in line
        assert "180M" not in line

    def test_ds_b_only_shows_only_b(self):
        row = ss.ZoneRow(zone="Nuclear Silo", max_players=4,
                         min_power_a=300_000_000, min_power_b=180_000_000)
        line = row.render_line("DS", teams="B")
        assert "180M" in line
        assert "300M" not in line

    def test_cs_ignores_teams_param(self):
        row = ss.ZoneRow(zone="Power Tower", max_players=4,
                         min_power_a=250_000_000)
        # CS storage uses min_power_a for the single floor.
        line = row.render_line("CS", teams="A")
        assert "Min:" in line
        assert "250M" in line

    def test_default_teams_is_both(self):
        row = ss.ZoneRow(zone="Nuclear Silo", max_players=4,
                         min_power_a=300_000_000, min_power_b=180_000_000)
        assert row.render_line("DS") == row.render_line("DS", teams="both")


class TestResolveDsTeams:
    def test_unconfigured_falls_back_to_both(self):
        # Patch the config read to return an empty dict (never-configured
        # alliance shape).
        with patch("config.get_storm_config", return_value={}):
            assert ss._resolve_ds_teams(123) == "both"

    def test_reads_saved_value(self):
        with patch("config.get_storm_config", return_value={"teams": "A"}):
            assert ss._resolve_ds_teams(123) == "A"
        with patch("config.get_storm_config", return_value={"teams": "B"}):
            assert ss._resolve_ds_teams(123) == "B"
        with patch("config.get_storm_config", return_value={"teams": "both"}):
            assert ss._resolve_ds_teams(123) == "both"

    def test_invalid_value_falls_back_to_both(self):
        with patch("config.get_storm_config", return_value={"teams": "weird"}):
            assert ss._resolve_ds_teams(123) == "both"

    def test_config_exception_falls_back_to_both(self):
        with patch("config.get_storm_config", side_effect=RuntimeError("db down")):
            assert ss._resolve_ds_teams(123) == "both"


class TestTeamsConfigPersistence:
    """End-to-end check that `save_storm_config(teams=...)` round-trips
    through `get_storm_config` on a real SQLite. Covers the schema
    migration and the new column."""

    def test_default_when_unset(self, seeded_db):
        import config
        from tests.unit.test_config import TEST_GUILD_ID
        # The seeded_db fixture creates a guild with default columns;
        # `teams` should read as 'both'.
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg.get("teams") == "both"

    def test_save_a_only(self, seeded_db):
        import config
        from tests.unit.test_config import TEST_GUILD_ID
        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", "tpl",
            "America/New_York", log_channel_id=0,
            teams="A",
        )
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg.get("teams") == "A"

    def test_save_invalid_normalizes_to_both(self, seeded_db):
        import config
        from tests.unit.test_config import TEST_GUILD_ID
        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments", "tpl",
            "America/New_York", log_channel_id=0,
            teams="garbage",
        )
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg.get("teams") == "both"


# ── #149: apply-to-similar ─────────────────────────────────────────────────


class TestZoneFamilyPrefix:
    def test_strips_roman_numerals(self):
        assert ss._zone_family_prefix("Field Hospital I") == "Field Hospital"
        assert ss._zone_family_prefix("Field Hospital II") == "Field Hospital"
        assert ss._zone_family_prefix("Field Hospital III") == "Field Hospital"
        assert ss._zone_family_prefix("Field Hospital IV") == "Field Hospital"
        assert ss._zone_family_prefix("Oil Refinery I") == "Oil Refinery"
        assert ss._zone_family_prefix("Oil Refinery II") == "Oil Refinery"

    def test_strips_arabic_numerals(self):
        assert ss._zone_family_prefix("Sample Warehouse 1") == "Sample Warehouse"
        assert ss._zone_family_prefix("Sample Warehouse 4") == "Sample Warehouse"
        assert ss._zone_family_prefix("Data Center 1") == "Data Center"
        assert ss._zone_family_prefix("Defense System 2") == "Defense System"

    def test_returns_input_when_no_numeric_suffix(self):
        assert ss._zone_family_prefix("Arsenal") == "Arsenal"
        assert ss._zone_family_prefix("Nuclear Silo") == "Nuclear Silo"
        assert ss._zone_family_prefix("Mercenary Factory") == "Mercenary Factory"
        assert ss._zone_family_prefix("Power Tower") == "Power Tower"
        assert ss._zone_family_prefix("Virus Lab") == "Virus Lab"

    def test_handles_empty(self):
        assert ss._zone_family_prefix("") == ""
        assert ss._zone_family_prefix(None) == ""


class TestSiblingZoneNames:
    def _ds_preset(self):
        return [
            ss.ZoneRow(zone="Nuclear Silo"),
            ss.ZoneRow(zone="Oil Refinery I"),
            ss.ZoneRow(zone="Oil Refinery II"),
            ss.ZoneRow(zone="Field Hospital I"),
            ss.ZoneRow(zone="Field Hospital II"),
            ss.ZoneRow(zone="Field Hospital III"),
            ss.ZoneRow(zone="Field Hospital IV"),
            ss.ZoneRow(zone="Arsenal"),
            ss.ZoneRow(zone="Mercenary Factory"),
        ]

    def test_field_hospital_family(self):
        zones = self._ds_preset()
        sibs = ss._sibling_zone_names(zones, "Field Hospital II")
        assert sorted(sibs) == [
            "Field Hospital I", "Field Hospital III", "Field Hospital IV",
        ]

    def test_oil_refinery_family(self):
        zones = self._ds_preset()
        sibs = ss._sibling_zone_names(zones, "Oil Refinery I")
        assert sibs == ["Oil Refinery II"]

    def test_singleton_zone_has_no_siblings(self):
        zones = self._ds_preset()
        assert ss._sibling_zone_names(zones, "Arsenal") == []
        assert ss._sibling_zone_names(zones, "Mercenary Factory") == []
        assert ss._sibling_zone_names(zones, "Nuclear Silo") == []

    def test_zone_not_in_preset(self):
        zones = self._ds_preset()
        # A zone whose name isn't in the preset at all — function should
        # still detect siblings if there are any with a matching prefix.
        # (This is the path the modal takes BEFORE upsert, but in the
        # actual call the zone IS in the buffer; covering both branches.)
        sibs = ss._sibling_zone_names(zones, "Field Hospital V")
        assert "Field Hospital I" in sibs
        assert "Field Hospital IV" in sibs

    def test_cs_sample_warehouse_family(self):
        zones = [
            ss.ZoneRow(zone="Sample Warehouse 1"),
            ss.ZoneRow(zone="Sample Warehouse 2"),
            ss.ZoneRow(zone="Sample Warehouse 3"),
            ss.ZoneRow(zone="Sample Warehouse 4"),
            ss.ZoneRow(zone="Virus Lab"),
        ]
        sibs = ss._sibling_zone_names(zones, "Sample Warehouse 2")
        assert sorted(sibs) == [
            "Sample Warehouse 1", "Sample Warehouse 3", "Sample Warehouse 4",
        ]

    def test_excludes_self_case_insensitively(self):
        zones = [
            ss.ZoneRow(zone="Field Hospital I"),
            ss.ZoneRow(zone="Field Hospital II"),
        ]
        # Self should be excluded regardless of casing on the query.
        sibs = ss._sibling_zone_names(zones, "FIELD HOSPITAL I")
        assert sibs == ["Field Hospital II"]


# ── #152: phase-aware strategy presets ──────────────────────────────────────


class TestPhaseAwarePresets:
    """Locks the phase-related additions to ZoneRow + PresetBuffer so a
    future refactor can't silently drop them."""

    def test_zone_row_defaults_phase_fields_to_zero(self):
        row = ss.ZoneRow(zone="Info Center")
        assert row.max_phase1 == 0
        assert row.max_phase2 == 0

    def test_zone_row_accepts_explicit_phase_caps(self):
        row = ss.ZoneRow(zone="Info Center", max_phase1=4, max_phase2=2)
        assert row.max_phase1 == 4
        assert row.max_phase2 == 2

    def test_preset_buffer_defaults_uses_phases_false(self):
        buf = ss.PresetBuffer(name="Flat", event_type="DS")
        assert buf.uses_phases is False
        assert buf.phase_count == 0

    def test_preset_buffer_accepts_uses_phases_true(self):
        # Back-compat shim: passing uses_phases=True canonicalises to
        # phase_count=2.
        buf = ss.PresetBuffer(name="Phases", event_type="DS", uses_phases=True)
        assert buf.uses_phases is True
        assert buf.phase_count == 2

    def test_preset_buffer_accepts_phase_count_three(self):
        buf = ss.PresetBuffer(name="ThreePhase", event_type="CS", phase_count=3)
        assert buf.uses_phases is True
        assert buf.phase_count == 3

    def test_render_line_flat_keeps_max_label(self):
        row = ss.ZoneRow(zone="Info Center", max_players=4,
                         min_power_a=200_000_000, min_power_b=100_000_000)
        line = row.render_line("DS", phase_count=0)
        assert "Max: 4" in line
        assert "P1:" not in line
        assert "P2:" not in line

    def test_render_line_phase_aware_uses_per_phase_rows(self):
        """#172 / Rule L: phase-aware presets render the capacity readout
        per-zone-per-phase — one indented row per phase under the zone
        header — instead of the pre-#172 inline `(P1: 3, P2: 1)` shape."""
        row = ss.ZoneRow(zone="Info Center", max_players=4,
                         max_phase1=3, max_phase2=1,
                         min_power_a=200_000_000, min_power_b=100_000_000)
        line = row.render_line("DS", phase_count=2)
        assert "Max:" not in line
        # Per-phase rows under the zone header.
        assert "Phase 1: cap 3" in line
        assert "Phase 2: cap 1" in line
        assert "Phase 3" not in line

    def test_render_line_three_phase_includes_p3(self):
        row = ss.ZoneRow(zone="Power Tower", max_players=0,
                         max_phase1=4, max_phase2=2, max_phase3=3,
                         min_power_a=200_000_000)
        line = row.render_line("CS", phase_count=3)
        assert "Max:" not in line
        assert "Phase 1: cap 4" in line
        assert "Phase 2: cap 2" in line
        assert "Phase 3: cap 3" in line

    def test_total_capacity_flat_sums_max_players(self):
        buf = ss.PresetBuffer(name="Flat", event_type="DS", zones=[
            ss.ZoneRow(zone="Info Center", max_players=4),
            ss.ZoneRow(zone="Nuclear Silo", max_players=6),
        ])
        assert buf.total_capacity() == 10

    def test_total_capacity_phase_aware_sums_max_phase_columns(self):
        buf = ss.PresetBuffer(name="Phases", event_type="DS",
                              uses_phases=True, zones=[
            ss.ZoneRow(zone="Info Center", max_players=4,
                       max_phase1=4, max_phase2=2),
            ss.ZoneRow(zone="Nuclear Silo", max_players=6,
                       max_phase1=0, max_phase2=4),
        ])
        # 4+2 + 0+4 = 10
        assert buf.total_capacity() == 10

    def test_upsert_zone_copies_phase_fields(self):
        buf = ss.PresetBuffer(name="P", event_type="DS", zones=[
            ss.ZoneRow(zone="Info Center", max_players=4,
                       max_phase1=4, max_phase2=2),
        ])
        # Update with new phase caps — should replace, not append.
        buf.upsert_zone(ss.ZoneRow(zone="Info Center", max_players=4,
                                   max_phase1=3, max_phase2=3))
        assert len(buf.zones) == 1
        assert buf.zones[0].max_phase1 == 3
        assert buf.zones[0].max_phase2 == 3

    def test_save_then_load_ds_round_trips_phase_fields(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        buf = ss.PresetBuffer(name="Phased", event_type="DS",
                              uses_phases=True, zones=[
            ss.ZoneRow(zone="Info Center", max_players=4,
                       max_phase1=4, max_phase2=2,
                       min_power_a=200_000_000, min_power_b=100_000_000),
            ss.ZoneRow(zone="Arsenal", max_players=0,
                       max_phase1=0, max_phase2=4,
                       min_power_a=0, min_power_b=0),
        ])
        assert ss.save_preset(gid, "DS", buf) is True
        loaded = ss.load_preset(gid, "DS", "Phased")
        assert loaded is not None
        assert loaded.uses_phases is True
        # Zones round-trip with their phase capacities intact.
        ic = loaded.find_zone("Info Center")
        assert ic.max_phase1 == 4
        assert ic.max_phase2 == 2
        ars = loaded.find_zone("Arsenal")
        assert ars.max_phase1 == 0
        assert ars.max_phase2 == 4

    def test_save_then_load_cs_round_trips_phase_fields(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        buf = ss.PresetBuffer(name="Phased CS", event_type="CS",
                              uses_phases=True, faction="Rulebringers",
                              zones=[
            ss.ZoneRow(zone="Power Tower", max_players=4,
                       max_phase1=3, max_phase2=1,
                       min_power_a=250_000_000),
        ])
        assert ss.save_preset(gid, "CS", buf) is True
        loaded = ss.load_preset(gid, "CS", "Phased CS")
        assert loaded is not None
        assert loaded.uses_phases is True
        assert loaded.faction == "Rulebringers"
        pt = loaded.find_zone("Power Tower")
        assert pt.max_phase1 == 3
        assert pt.max_phase2 == 1

    def test_flat_preset_round_trip_preserves_uses_phases_false(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        buf = ss.PresetBuffer(name="Flat", event_type="DS",
                              uses_phases=False, zones=[
            ss.ZoneRow(zone="Info Center", max_players=4,
                       min_power_a=200_000_000, min_power_b=100_000_000),
        ])
        assert ss.save_preset(gid, "DS", buf) is True
        loaded = ss.load_preset(gid, "DS", "Flat")
        assert loaded is not None
        assert loaded.uses_phases is False
        assert loaded.phase_count == 0
        # Phase fields default to 0 when the preset is flat.
        ic = loaded.find_zone("Info Center")
        assert ic.max_phase1 == 0
        assert ic.max_phase2 == 0
        assert ic.max_phase3 == 0

    def test_save_then_load_three_phase_cs_preset(self, fake_sheet_factory):
        fake, gid = fake_sheet_factory
        buf = ss.PresetBuffer(name="ThreePhase", event_type="CS",
                              phase_count=3, faction="Rulebringers",
                              zones=[
            ss.ZoneRow(zone="Power Tower", max_players=0,
                       max_phase1=4, max_phase2=2, max_phase3=3,
                       min_power_a=200_000_000,
                       priority_phase1=1, priority_phase2=3, priority_phase3=2),
            ss.ZoneRow(zone="Virus Lab", max_players=0,
                       max_phase3=4,
                       min_power_a=0,
                       priority_phase3=1),
        ])
        assert ss.save_preset(gid, "CS", buf) is True
        loaded = ss.load_preset(gid, "CS", "ThreePhase")
        assert loaded is not None
        assert loaded.phase_count == 3
        assert loaded.faction == "Rulebringers"
        pt = loaded.find_zone("Power Tower")
        assert pt.max_phase1 == 4
        assert pt.max_phase2 == 2
        assert pt.max_phase3 == 3
        assert pt.priority_phase1 == 1
        assert pt.priority_phase2 == 3
        assert pt.priority_phase3 == 2
        vl = loaded.find_zone("Virus Lab")
        assert vl.max_phase1 == 0
        assert vl.max_phase2 == 0
        assert vl.max_phase3 == 4
        assert vl.priority_phase3 == 1

    def test_legacy_use_phases_column_reads_as_two_phase(self, fake_sheet_factory):
        """Pre-3-phase data on the sheet wrote `Use Phases = TRUE` and
        no `Phase Count`. The loader must canonicalise that to
        `phase_count = 2` so saved presets from earlier commits in this
        feature branch keep working."""
        fake, gid = fake_sheet_factory
        # Inject legacy-shaped rows directly (header is the OLD shape
        # without Phase Count + Max Phase 3).
        ws = fake.add_worksheet("DS Strategies")
        ws.append_row(
            ["Preset Name", "Zone", "Max Players",
             "Max Phase 1", "Max Phase 2",
             "Min Power A", "Min Power B", "Priority", "Use Phases"],
            value_input_option="RAW",
        )
        ws.append_row(
            ["Legacy", "Info Center", "0", "4", "2",
             "200000000", "100000000", "1", "TRUE"],
            value_input_option="RAW",
        )
        # Wire the strategies_tab pointer so the loader looks at this ws.
        import config
        config.save_structured_storm_config(
            gid, "DS", strategies_tab="DS Strategies",
        )
        loaded = ss.load_preset(gid, "DS", "Legacy")
        assert loaded is not None
        assert loaded.phase_count == 2
        assert loaded.uses_phases is True

    def test_parse_uses_phases_truthy_strings(self):
        for raw in ("TRUE", "true", "True", "yes", "Y", "1", "on", "phases"):
            assert ss._parse_uses_phases(raw) is True

    def test_parse_uses_phases_falsy_strings(self):
        for raw in ("", "FALSE", "false", "no", "n", "0", "off", None, "  "):
            assert ss._parse_uses_phases(raw) is False


class TestStrategyListView:
    """#169 / Rule M: `/<parent> strategy list` now ships an inline
    Create / Edit / Delete row alongside the preset summary. Empty state
    surfaces the same row with Edit + Delete disabled."""

    def test_empty_state_enables_only_create(self):
        view = ss._StrategyListView(owner_id=1, event_type="DS", names=[])
        labels_disabled = {
            getattr(c, "label", ""): getattr(c, "disabled", False)
            for c in view.children
        }
        # All three buttons render, but Edit + Delete are disabled when
        # no presets exist so the officer can't open an empty Select.
        assert any("Create" in lab for lab in labels_disabled)
        assert any("Edit" in lab for lab in labels_disabled)
        assert any("Delete" in lab for lab in labels_disabled)
        for label, disabled in labels_disabled.items():
            if "Create" in label:
                assert disabled is False
            if "Edit" in label or "Delete" in label:
                assert disabled is True

    def test_populated_state_enables_all_three(self):
        view = ss._StrategyListView(
            owner_id=1, event_type="DS", names=["Standard DS"],
        )
        labels_disabled = {
            getattr(c, "label", ""): getattr(c, "disabled", False)
            for c in view.children
        }
        for label, disabled in labels_disabled.items():
            if any(action in label for action in ("Create", "Edit", "Delete")):
                assert disabled is False


class TestPresetEditorPolish:
    """#174 / Decisions 10 + 13: the editor view drops the [➕ Add zone]
    affordance (zones are game-defined), renames action buttons to be
    self-describing, drops the redundant 'Yes — ' prefix on the phase-
    mode dropdown, and reframes the dirty-state + mode-toggle copy."""

    def test_add_zone_modal_class_removed(self):
        # Decision #13: zones come exclusively from DS_ZONE_STRUCTURE /
        # CS_ZONE_STRUCTURE; alliances can't add their own.
        assert not hasattr(ss, "_AddZoneModal")

    def test_phase_mode_dropdown_drops_yes_prefix(self):
        """The pre-#174 labels were 'Yes — 2 Phases' / 'Yes — 3 Phases'.
        The 'Yes — ' was redundant once 'Flat (no phases)' became the
        no-phase option."""
        # Build the editor view to inspect its components without going
        # through the slash command path.
        buf = ss.PresetBuffer(name="P", event_type="DS")
        view = ss._PresetEditorView(guild_id=1, user_id=1, buf=buf)
        phase_selects = [
            c for c in view.children
            if isinstance(c, __import__("discord").ui.Select)
            and "Phase mode" in (c.placeholder or "")
        ]
        assert len(phase_selects) == 1
        labels = [opt.label for opt in phase_selects[0].options]
        assert "Flat (no phases)" in labels
        assert "2 Phases" in labels
        assert "3 Phases" in labels
        assert not any("Yes —" in lab for lab in labels)

    def test_action_button_labels_self_describe(self):
        """Decision #13's button-sweep: '✏️ Rename' → '✏️ Rename preset',
        '🔙 Abandon' → '🔙 Abandon this preset' so the button is
        understandable out of context (e.g. on mobile where the embed
        is collapsed)."""
        buf = ss.PresetBuffer(name="P", event_type="DS")
        view = ss._PresetEditorView(guild_id=1, user_id=1, buf=buf)
        labels = [getattr(c, "label", "") for c in view.children]
        assert "✏️ Rename preset" in labels
        assert "🔙 Abandon this preset" in labels
        # The Add Zone button is gone.
        assert not any("Add zone" in lab for lab in labels)

    def test_unsaved_changes_footer_uses_new_wording(self):
        buf = ss.PresetBuffer(name="P", event_type="DS")
        buf.dirty = True
        embed = ss._build_editor_embed(buf, teams="both")
        body = embed.description or ""
        assert "Unsaved changes" in body
        # New phrasing: "Save preset to save your changes."; old phrasing
        # ("hit Save Preset to commit") is gone.
        assert "Save preset to save your changes" in body
        assert "to commit" not in body


class TestPresetPickerView:
    """The Edit / Delete buttons open this picker. Capped at 25 options
    (Discord Select limit); the action dictates the downstream handler."""

    def test_picker_lists_sorted_names_case_insensitive(self):
        view = ss._PresetPickerView(
            owner_id=1, event_type="DS",
            names=["zeta", "Alpha", "beta"],
            action="edit",
        )
        # First child is the Select.
        select = view.children[0]
        labels = [opt.label for opt in select.options]
        assert labels == ["Alpha", "beta", "zeta"]

    def test_picker_caps_at_25_options(self):
        names = [f"Preset {i:02d}" for i in range(40)]
        view = ss._PresetPickerView(
            owner_id=1, event_type="DS", names=names, action="delete",
        )
        select = view.children[0]
        assert len(select.options) == 25

    def test_picker_placeholder_reflects_action(self):
        view = ss._PresetPickerView(
            owner_id=1, event_type="DS", names=["X"], action="delete",
        )
        select = view.children[0]
        assert "delete" in select.placeholder.lower()

    def test_overflow_notice_empty_when_under_cap(self):
        view = ss._PresetPickerView(
            owner_id=1, event_type="DS",
            names=[f"P{i}" for i in range(10)],
            action="edit",
        )
        # 10 < 25 → no notice.
        assert view.overflow_notice == ""
        assert view.truncated_count == 0

    def test_overflow_notice_at_exactly_25_is_empty(self):
        view = ss._PresetPickerView(
            owner_id=1, event_type="DS",
            names=[f"P{i:02d}" for i in range(25)],
            action="edit",
        )
        # Boundary: 25 fits exactly, no truncation.
        assert view.overflow_notice == ""
        assert view.truncated_count == 0

    def test_overflow_notice_surfaces_count_when_over_cap(self):
        """The picker silently dropped names past 25 before — officers
        searching for an older preset that didn't appear had no signal.
        Notice now surfaces the gap."""
        view = ss._PresetPickerView(
            owner_id=1, event_type="DS",
            names=[f"P{i:02d}" for i in range(40)],
            action="delete",
        )
        notice = view.overflow_notice
        assert notice != ""
        assert "first 25" in notice
        assert "40" in notice  # total count surfaced
        assert view.truncated_count == 15

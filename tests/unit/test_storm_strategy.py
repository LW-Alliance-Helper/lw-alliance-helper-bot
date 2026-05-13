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

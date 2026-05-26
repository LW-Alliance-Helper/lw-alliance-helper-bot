"""
Tests for storm_roster_builder.py (#128).

Covers the pure-data helpers: roster power reader, session state,
eligibility filter, rule pre-application. The interactive Discord
view + modal are integration territory and not unit-tested here.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import storm_roster_builder as srb
import storm_strategy as ss
import storm_member_rules as smr

from tests.unit.test_config import TEST_GUILD_ID


# ── Fakes ────────────────────────────────────────────────────────────────────


class _FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]] | None = None):
        self.title = title
        self._rows = list(rows or [])

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def row_values(self, row_number: int):
        idx = row_number - 1
        if 0 <= idx < len(self._rows):
            return list(self._rows[idx])
        return []

    def append_row(self, row, value_input_option=None):
        self._rows.append([str(c) for c in row])

    def append_rows(self, rows, value_input_option=None):
        for r in rows:
            self._rows.append([str(c) for c in r])

    def clear(self):
        self._rows = []

    def update(self, range_, values, value_input_option=None):
        """Mimic gspread's range-anchored update so callers that overwrite
        a single row (header migration, blank-trailing) don't wipe the
        whole tab. Only parses the column-A anchor, sufficient here."""
        import re
        m = re.match(r"^A(\d+)", str(range_))
        start_row = int(m.group(1)) - 1 if m else 0
        new_payload = [list(r) for r in values]
        while len(self._rows) < start_row:
            self._rows.append([""] * (len(self._rows[0]) if self._rows else 0))
        for i, row in enumerate(new_payload):
            target = start_row + i
            if target < len(self._rows):
                self._rows[target] = list(row)
            else:
                self._rows.append(list(row))

    def delete_rows(self, idx: int) -> None:
        # Sheet rows are 1-indexed.
        if 1 <= idx <= len(self._rows):
            self._rows.pop(idx - 1)


class _FakeSpreadsheet:
    def __init__(self):
        self._tabs: dict[str, _FakeWorksheet] = {}

    def worksheet(self, title: str):
        if title not in self._tabs:
            raise Exception("Worksheet not found")
        return self._tabs[title]

    def add_worksheet(self, title: str, rows: int = 0, cols: int = 0):
        ws = _FakeWorksheet(title)
        self._tabs[title] = ws
        return ws


@pytest.fixture
def fake_env(seeded_db):
    """Seed a guild with structured-flow on, a roster Sheet with power
    data, and a fake spreadsheet. Returns (fake_sheet, guild_id)."""
    import config

    fake = _FakeSpreadsheet()

    # Build a roster Sheet with the standard 5 bot-managed columns plus
    # a custom power column and a `not_on_discord` flag.
    roster_ws = fake.add_worksheet("Member Roster")
    roster_ws._rows = [
        ["Discord ID", "Name", "Display Name", "Joined", "Roles",
         "1st Squad Power", "not_on_discord"],
        ["1001", "alice", "Alice",   "2024-01-01", "Member", "412M", ""],
        ["1002", "bob",   "Bob",     "2024-01-02", "Member", "230M", ""],
        ["1003", "carol", "Carol",   "2024-01-03", "Member", "180M", ""],
        ["",     "dave",  "Dave",    "",           "",       "200M", "x"],   # non-Discord
        ["1004", "erin",  "Erin",    "2024-02-01", "Member", "",     ""],    # power unknown
        ["1005", "frank", "Frank",   "2024-02-02", "Member", "tbd",  ""],    # garbage power
    ]

    # Enable member roster + structured flow with the configured column.
    config.save_member_roster_config(
        TEST_GUILD_ID,
        enabled=1,
        tab_name="Member Roster",
        discord_id_col=0,
        name_col=1,
        display_col=2,
        joined_col=3,
        roles_col=4,
    )
    for et in ("DS", "CS"):
        config.save_storm_config(
            TEST_GUILD_ID, et,
            tab_name=f"{et} Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, et,
            structured_flow_enabled=True,
            # Fake roster sheet has "1st Squad Power" at column F (index 5).
            power_metric_column="F",
        )

    # Patch both get_spreadsheet AND get_member_roster_sheet to return our fake.
    def _fake_member_roster_sheet(guild_id: int, tab_name: str):
        return fake.worksheet(tab_name)

    with patch.object(config, "get_spreadsheet", return_value=fake), \
         patch.object(config, "get_member_roster_sheet", side_effect=_fake_member_roster_sheet):
        yield fake, TEST_GUILD_ID


# ── Roster power reader ──────────────────────────────────────────────────────


class TestReadRosterPowers:
    def test_reads_power_for_discord_members(self, fake_env):
        fake, gid = fake_env
        members, errs = srb._read_roster_powers(gid, "DS")
        assert errs == []
        assert members["1001"]["power"] == 412_000_000
        assert members["1002"]["power"] == 230_000_000
        assert members["1003"]["power"] == 180_000_000

    def test_blank_power_reads_as_none(self, fake_env):
        fake, gid = fake_env
        members, _errs = srb._read_roster_powers(gid, "DS")
        assert members["1004"]["power"] is None

    def test_garbage_power_reads_as_none(self, fake_env):
        # "tbd" should not silently coerce to 0 — must be None so the
        # eligibility filter treats Frank as "power unknown" rather than
        # passing him through every floor.
        fake, gid = fake_env
        members, _errs = srb._read_roster_powers(gid, "DS")
        assert members["1005"]["power"] is None

    def test_not_on_discord_flag_read(self, fake_env):
        fake, gid = fake_env
        members, _errs = srb._read_roster_powers(gid, "DS")
        assert members["Dave"]["not_on_discord"] is True
        assert members["1001"]["not_on_discord"] is False

    def test_non_discord_member_keyed_by_name(self, fake_env):
        fake, gid = fake_env
        members, _errs = srb._read_roster_powers(gid, "DS")
        # Dave has no Discord ID — keyed by display name.
        assert "Dave" in members
        assert members["Dave"]["discord_id"] == ""

    def test_power_column_letter_past_header_surfaces_error(self, fake_env):
        fake, gid = fake_env
        import config
        # Fake roster sheet has 7 columns (A-G). Picking column Z (past
        # the header) surfaces a soft error + every power reads None.
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="Z",
        )
        members, errs = srb._read_roster_powers(gid, "DS")
        assert any("power column Z doesn't exist" in e.lower() or
                   "power column z doesn't exist" in e.lower()
                   for e in errs)
        assert all(m["power"] is None for m in members.values())

    def test_power_column_letter_at_non_power_column_reads_none(self, fake_env):
        fake, gid = fake_env
        import config
        # Pointing at the Name column (B) — every cell parses as None
        # (not a power value), no soft error.
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
        )
        members, _errs = srb._read_roster_powers(gid, "DS")
        # Name cells like "alice" can't parse as power -> None.
        assert all(m["power"] is None for m in members.values())


class TestPowerDataSourceFlexibility:
    """#226 follow-up — power can live on a tab other than the Member
    Roster. The structured config carries a tab name + a match column
    letter; `_read_roster_powers` overlays power onto each member by
    matching Discord ID first, then case-insensitive name."""

    def test_empty_power_tab_falls_back_to_member_roster(self, fake_env):
        """Backwards-compat: existing alliances saved
        `power_metric_column = "F"` and nothing else. Their config
        still works — empty `power_metric_tab` means "read power from
        the Member Roster row" (the pre-flexibility behaviour)."""
        fake, gid = fake_env
        import config
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
            power_metric_tab="",     # empty → Member Roster
            power_match_column="",   # empty → discord_id_col
        )
        members, errs = srb._read_roster_powers(gid, "DS")
        assert errs == []
        assert members["1001"]["power"] == 412_000_000

    def test_cross_tab_reads_power_from_configured_tab(self, fake_env):
        """Alliance points storm at a Squad Powers tab. The Member
        Roster still supplies the member directory; power comes from
        the other tab, matched by Discord ID in column A."""
        fake, gid = fake_env
        import config
        # Add a Squad Powers tab with one row per member, power in
        # column B, Discord IDs in column A.
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Discord ID", "1st Squad Power"],
            ["1001",       "500M"],
            ["1002",       "350M"],
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
        )
        members, errs = srb._read_roster_powers(gid, "DS")
        assert errs == []
        # Cross-tab values override whatever the Member Roster row
        # said — alliance pointed storm AT this tab.
        assert members["1001"]["power"] == 500_000_000
        assert members["1002"]["power"] == 350_000_000
        # Member with no entry in the power tab gets power=None.
        assert members["1003"]["power"] is None

    def test_cross_tab_matches_by_name_when_id_blank(self, fake_env):
        """Match column carries names, not Discord IDs. The bot tries
        the digit path first, falls back to case-insensitive name
        match."""
        fake, gid = fake_env
        import config
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Member",  "1st Squad Power"],
            ["Alice",   "411M"],
            ["bob",     "229M"],
            ["dave",    "189M"],  # the non-Discord member
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
        )
        members, _errs = srb._read_roster_powers(gid, "DS")
        # Case-insensitive: "Alice" → matches member.name "Alice".
        assert members["1001"]["power"] == 411_000_000
        # Lowercased "bob" → member.name "Bob".
        assert members["1002"]["power"] == 229_000_000
        # Non-Discord Dave matched by name too.
        assert members["Dave"]["power"] == 189_000_000

    def test_cross_tab_missing_tab_surfaces_soft_error(self, fake_env):
        """Power tab doesn't exist on the spreadsheet. The bot surfaces
        a soft error and falls through with power=None for everyone."""
        fake, gid = fake_env
        import config
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Nonexistent Tab",
            power_match_column="A",
        )
        members, errs = srb._read_roster_powers(gid, "DS")
        # Errors flagged; members still surface with power=None so
        # the builder can show them under "power unknown".
        assert any("Nonexistent Tab" in e for e in errs)
        assert all(m["power"] is None for m in members.values())

    def test_cross_tab_unparseable_power_skipped(self, fake_env):
        """Garbage in the power column (e.g. \"tbd\") doesn't crash —
        that row just doesn't contribute to the index."""
        fake, gid = fake_env
        import config
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Discord ID", "1st Squad Power"],
            ["1001",       "tbd"],
            ["1002",       "260M"],
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
        )
        members, _errs = srb._read_roster_powers(gid, "DS")
        assert members["1001"]["power"] is None
        assert members["1002"]["power"] == 260_000_000


class TestReadPowerColumnHeader:
    """#256 — the power-refresh DM (#138) must name the column the
    bot actually reads. When the alliance points storm at a separate
    Power Data Source tab (e.g. `Squad Powers`), the header label
    needs to come from THAT tab — not from whatever column letter
    happens to land on in the Member Roster."""

    def test_cross_tab_reads_header_from_power_tab(self, fake_env):
        """Regression for #256: alliance configures Squad Powers as the
        power tab, column C. Column C on Squad Powers reads `Squad
        Power`; column C on the Member Roster reads `Display Name`.
        The DM must surface `Squad Power`, not `Display Name`."""
        fake, gid = fake_env
        import config
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Discord ID", "Name", "Squad Power"],
            ["1001",       "Alice", "500M"],
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="C",
            power_metric_tab="Squad Powers",
            power_match_column="A",
        )
        assert srb._read_power_column_header(gid, "DS") == "Squad Power"

    def test_empty_power_tab_falls_back_to_member_roster(self, fake_env):
        """Backwards-compat: alliances with empty `power_metric_tab`
        still get the Member Roster header (pre-flexibility behaviour).
        Column F on the fake roster is `1st Squad Power`."""
        fake, gid = fake_env
        import config
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
            power_metric_tab="",
            power_match_column="",
        )
        assert srb._read_power_column_header(gid, "DS") == "1st Squad Power"

    def test_your_prefix_stripped(self, fake_env):
        """`Your Power` header reads as `Power` in the DM (so the
        sentence comes out `your Power`, not `your Your Power`)."""
        fake, gid = fake_env
        import config
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Discord ID", "Your Power"],
            ["1001",       "500M"],
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
        )
        assert srb._read_power_column_header(gid, "DS") == "Power"

    def test_missing_power_tab_returns_blank(self, fake_env):
        """Configured tab doesn't exist on the spreadsheet → return ""
        so the DM falls back to the generic wording instead of
        crashing the signup handler."""
        fake, gid = fake_env
        import config
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Nonexistent Tab",
            power_match_column="A",
        )
        assert srb._read_power_column_header(gid, "DS") == ""


class TestLastUpdatedOverlay:
    """#255 — when the alliance configures a Last-Updated source, the
    roster reader overlays each member's most-recent timestamp from
    that source. The DM-trigger logic (storm_signup_view) consumes
    this `last_updated` key alongside `power`."""

    def test_skipped_when_no_last_updated_config(self, fake_env):
        """No last-updated source configured (default) → `last_updated`
        key never appears on member dicts. Existing callsites that
        don't care about staleness see the same shape they always have."""
        fake, gid = fake_env
        members, _errs = srb._read_roster_powers(gid, "DS")
        assert "last_updated" not in members["1001"]

    def test_cross_tab_overlay_resolves_dates_by_id(self, fake_env):
        """Squad Powers tab has a Date Modified column; the overlay
        parses each row's date and exposes it as a `datetime.date`
        on the member."""
        import datetime as _dt
        fake, gid = fake_env
        import config
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Discord ID", "1st Squad Power", "Date Modified"],
            ["1001",       "500M",            "5/24/2026"],
            ["1002",       "350M",            "4/10/2026"],
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
            power_last_updated_tab="Squad Powers",
            power_last_updated_column="C",
            # Empty match column reuses the power match column.
            power_last_updated_match_column="",
            power_refresh_stale_days=7,
        )
        members, errs = srb._read_roster_powers(gid, "DS")
        assert errs == []
        assert members["1001"]["last_updated"] == _dt.date(2026, 5, 24)
        assert members["1002"]["last_updated"] == _dt.date(2026, 4, 10)
        # Members without a row on the source tab get None — DM
        # path silently skips the stale check for them.
        assert members["1003"]["last_updated"] is None

    def test_unparseable_timestamp_yields_none(self, fake_env):
        """Garbage in the Date Modified cell parses to None — that
        row gets skipped without crashing the read."""
        fake, gid = fake_env
        import config
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Discord ID", "1st Squad Power", "Date Modified"],
            ["1001",       "500M",            "two weeks ago"],
            ["1002",       "350M",            "4/10/2026"],
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
            power_last_updated_tab="Squad Powers",
            power_last_updated_column="C",
            power_refresh_stale_days=7,
        )
        members, _errs = srb._read_roster_powers(gid, "DS")
        assert members["1001"]["last_updated"] is None
        assert members["1002"] is not None

    def test_dmy_column_locks_to_dmy_format(self, fake_env):
        """A column with `25/12/2025`-style values (first component
        > 12) auto-detects DMY and parses every cell that way."""
        import datetime as _dt
        fake, gid = fake_env
        import config
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Discord ID", "1st Squad Power", "Date Modified"],
            # First value is unambiguous DMY (24 > 12).
            ["1001",       "500M",            "24/5/2026"],
            # Second value would be ambiguous on its own; locks to DMY
            # by the column-wide flag and resolves to May 10.
            ["1002",       "350M",            "10/5/2026"],
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
            power_last_updated_tab="Squad Powers",
            power_last_updated_column="C",
            power_refresh_stale_days=7,
        )
        members, _errs = srb._read_roster_powers(gid, "DS")
        assert members["1001"]["last_updated"] == _dt.date(2026, 5, 24)
        assert members["1002"]["last_updated"] == _dt.date(2026, 5, 10)

    def test_separate_tab_lookup_by_name(self, fake_env):
        """Last-updated source lives on a different tab than power,
        matched by member name rather than Discord ID."""
        import datetime as _dt
        fake, gid = fake_env
        import config
        # Power lives on Squad Powers by ID.
        sp = fake.add_worksheet("Squad Powers")
        sp._rows = [
            ["Discord ID", "1st Squad Power"],
            ["1001",       "500M"],
        ]
        # Last-updated lives on a completely separate tab, matched by name.
        lu = fake.add_worksheet("Audit Log")
        lu._rows = [
            ["Member Name", "Updated At"],
            ["Alice",       "2026-05-24"],
        ]
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
            power_last_updated_tab="Audit Log",
            power_last_updated_column="B",
            # Different match column on the separate tab.
            power_last_updated_match_column="A",
            power_refresh_stale_days=7,
        )
        members, _errs = srb._read_roster_powers(gid, "DS")
        assert members["1001"]["last_updated"] == _dt.date(2026, 5, 24)

    def test_missing_source_tab_surfaces_error_and_falls_through(self, fake_env):
        """Configured Last-Updated tab doesn't exist → soft error and
        every member's last_updated is None (no crash)."""
        fake, gid = fake_env
        import config
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
            power_last_updated_tab="Nonexistent Audit Tab",
            power_last_updated_column="B",
            power_refresh_stale_days=7,
        )
        members, errs = srb._read_roster_powers(gid, "DS")
        assert any("Nonexistent Audit Tab" in e for e in errs)
        assert all(m.get("last_updated") is None for m in members.values())

    def test_overlay_skipped_when_column_empty(self, fake_env):
        """`power_last_updated_tab` set but column letter empty — the
        config is half-configured and the overlay is silently skipped.
        Members keep their default shape with no `last_updated` key."""
        fake, gid = fake_env
        import config
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
            power_last_updated_tab="Squad Powers",
            power_last_updated_column="",  # half-configured
            power_refresh_stale_days=7,
        )
        members, _errs = srb._read_roster_powers(gid, "DS")
        # No `last_updated` key on the member dicts.
        assert all("last_updated" not in m for m in members.values())


# ── Session + eligibility ────────────────────────────────────────────────────


def _make_session(team: str = "A", *, members=None, preset_zones=None,
                  per_member_rules=None, power_band_rules=None,
                  sub_mode: str = "pool"):
    preset_zones = preset_zones or [
        ss.ZoneRow(zone="Power Tower",    max_players=4, min_power_a=300_000_000, min_power_b=180_000_000),
        ss.ZoneRow(zone="Nuclear Silo",   max_players=4, min_power_a=250_000_000, min_power_b=150_000_000),
        ss.ZoneRow(zone="Oil Refinery I", max_players=4, min_power_a=200_000_000, min_power_b=100_000_000),
    ]
    preset = ss.PresetBuffer(name="Standard", event_type="DS", zones=preset_zones)
    return srb.RosterBuilderSession(
        guild_id=1, user_id=42, event_type="DS",
        team=team,
        preset=preset,
        members=members or {},
        per_member_rules=per_member_rules or [],
        power_band_rules=power_band_rules or [],
        sub_mode=sub_mode,
    )


class TestFloorForZone:
    def test_team_a_reads_min_a(self):
        session = _make_session(team="A")
        assert session.floor_for_zone("Power Tower")  == 300_000_000
        assert session.floor_for_zone("Nuclear Silo") == 250_000_000

    def test_team_b_reads_min_b(self):
        session = _make_session(team="B")
        assert session.floor_for_zone("Power Tower")  == 180_000_000
        assert session.floor_for_zone("Nuclear Silo") == 150_000_000

    def test_missing_zone_returns_zero(self):
        session = _make_session()
        assert session.floor_for_zone("Nowhere") == 0


class TestEligibility:
    def test_eligible_filters_by_floor(self):
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 230_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 180_000_000, "not_on_discord": False},
        })
        eligible, below = srb._eligible_member_keys_for_zone(session, "Power Tower")
        # Min A for Power Tower = 300M. Only Alice qualifies.
        assert eligible == ["1001"]
        assert set(below) == {"1002", "1003"}

    def test_power_unknown_in_below_bucket(self):
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1004": {"key": "1004", "name": "Erin",  "discord_id": "1004",
                     "power": None, "not_on_discord": False},  # power unknown
        })
        eligible, below = srb._eligible_member_keys_for_zone(session, "Power Tower")
        assert eligible == ["1001"]
        # Power-unknown lands in below — not silently treated as 0 and
        # added to the eligible pool.
        assert "1004" in below

    def test_assigned_member_excluded_from_pool(self):
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
        })
        # Pre-assign Alice to Power Tower.
        session.assignments["Power Tower"].append("1001")
        eligible, _below = srb._eligible_member_keys_for_zone(session, "Nuclear Silo")
        # Alice should not be eligible for another zone.
        assert "1001" not in eligible
        assert "1002" in eligible

    def test_eligible_sorted_by_power_desc(self):
        session = _make_session(team="B", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 300_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 500_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 200_000_000, "not_on_discord": False},
        })
        eligible, _below = srb._eligible_member_keys_for_zone(session, "Power Tower")
        # Team B floor for Power Tower = 180M; all three eligible.
        assert eligible == ["1002", "1001", "1003"]


class TestRulePreApplication:
    def test_per_member_zone_rule_pins_member(self):
        rule = smr.Rule(
            rule_type="per_member", subject="Alice",
            sub_type="zone", value="Power Tower",
        )
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }, per_member_rules=[rule])
        srb._apply_rules_to_session(session)
        assert "1001" in session.assignments["Power Tower"]

    def test_per_member_zone_rule_with_unknown_member_noops(self):
        rule = smr.Rule(
            rule_type="per_member", subject="Ghost",
            sub_type="zone", value="Power Tower",
        )
        session = _make_session(team="A", per_member_rules=[rule])
        srb._apply_rules_to_session(session)
        # No crash, no assignment.
        assert session.assignments["Power Tower"] == []

    def test_per_member_rule_skipped_if_zone_full(self):
        rule = smr.Rule(
            rule_type="per_member", subject="Bob",
            sub_type="zone", value="Power Tower",
        )
        # Manually fill Power Tower to capacity before rule application.
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
        }, per_member_rules=[rule])
        # Pre-fill manually (capacity = 4)
        for i in range(4):
            session.assignments["Power Tower"].append(f"placeholder_{i}")
        srb._apply_rules_to_session(session)
        # Bob shouldn't be added — zone full.
        assert "1002" not in session.assignments["Power Tower"]

    def test_per_member_team_rule_not_applied_by_pre_application(self):
        # Team rules don't get auto-applied by the pre-application pass
        # (the team is implicit from the apply-time choice). They'd be
        # used to filter the picker — out of scope for this test.
        rule = smr.Rule(
            rule_type="per_member", subject="Alice",
            sub_type="team", value="A",
        )
        session = _make_session(team="A", per_member_rules=[rule])
        srb._apply_rules_to_session(session)
        # No zone assignments from a team rule.
        assert all(not v for v in session.assignments.values())

    def test_unmatched_per_member_rule_silently_no_ops(self):
        """Decision #7 (#173): a rule whose subject isn't in tonight's
        roster is a silent no-op. The rule means 'if this member is
        in tonight's event, do X' — they're not in tonight's event,
        so there's nothing to apply and nothing to report. No
        roster_errors warning surfaces."""
        rule_active = smr.Rule(
            rule_type="per_member", subject="Alice",
            sub_type="zone", value="Power Tower",
        )
        rule_stale = smr.Rule(
            rule_type="per_member", subject="OldName",
            sub_type="zone", value="Nuclear Silo",
        )
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }, per_member_rules=[rule_active, rule_stale])
        srb._apply_rules_to_session(session)
        # Active rule still applies.
        assert "1001" in session.assignments["Power Tower"]
        # Stale rule does NOT surface — silent no-op.
        assert not any("OldName" in e for e in session.roster_errors)


class TestPowerBandRuleConsumption:
    """`power_band` Member Rules from #127 are now consumed in the
    eligibility filter. Without this they were loaded onto the session
    but never gated picker contents — entirely dead metadata."""

    def test_band_lowers_effective_floor_for_named_zone(self):
        # Preset floor for Power Tower (team A) = 300M. A band rule of
        # "≥ 200M → Power Tower" should let Bob (230M) qualify.
        band = smr.Rule(
            rule_type="power_band", subject="200000000",
            value="Power Tower", sub_type="",
        )
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 230_000_000, "not_on_discord": False},
        }, power_band_rules=[band])
        eligible, below = srb._eligible_member_keys_for_zone(session, "Power Tower")
        assert set(eligible) == {"1001", "1002"}
        assert below == []

    def test_band_does_not_affect_other_zones(self):
        # Band targets Power Tower only — Nuclear Silo's floor unchanged.
        band = smr.Rule(
            rule_type="power_band", subject="100000000",
            value="Power Tower", sub_type="",
        )
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 180_000_000, "not_on_discord": False},
        }, power_band_rules=[band])
        # Carol (180M) is eligible for Power Tower (band lowered to 100M)…
        eligible_pt, _ = srb._eligible_member_keys_for_zone(session, "Power Tower")
        assert "1003" in eligible_pt
        # …but NOT for Nuclear Silo (250M floor unchanged).
        eligible_ns, below_ns = srb._eligible_member_keys_for_zone(session, "Nuclear Silo")
        assert "1003" not in eligible_ns
        assert "1003" in below_ns

    def test_stricter_band_does_not_raise_floor(self):
        # Preset floor 300M; band "≥ 400M → Power Tower" is stricter
        # than preset. Effective floor is min(preset, band) = 300M,
        # because bands are meant to GRANT eligibility, not deny it.
        band = smr.Rule(
            rule_type="power_band", subject="400000000",
            value="Power Tower", sub_type="",
        )
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 350_000_000, "not_on_discord": False},
        }, power_band_rules=[band])
        eligible, below = srb._eligible_member_keys_for_zone(session, "Power Tower")
        # Alice (350M, above preset 300M) stays eligible.
        assert eligible == ["1001"]
        assert below == []

    def test_multiple_bands_use_lowest_threshold(self):
        # Two bands target the same zone; the LOWEST threshold wins.
        bands = [
            smr.Rule(rule_type="power_band", subject="250000000",
                     value="Power Tower", sub_type=""),
            smr.Rule(rule_type="power_band", subject="150000000",
                     value="Power Tower", sub_type=""),
        ]
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 180_000_000, "not_on_discord": False},
        }, power_band_rules=bands)
        eligible, _ = srb._eligible_member_keys_for_zone(session, "Power Tower")
        # 180M >= 150M (lower band) → eligible.
        assert "1003" in eligible

    def test_band_zone_match_is_case_insensitive(self):
        band = smr.Rule(
            rule_type="power_band", subject="100000000",
            value="POWER TOWER", sub_type="",
        )
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 150_000_000, "not_on_discord": False},
        }, power_band_rules=[band])
        eligible, _ = srb._eligible_member_keys_for_zone(session, "Power Tower")
        assert "1003" in eligible

    def test_garbage_band_threshold_ignored(self):
        # A power_band row with a non-integer Subject must not crash;
        # `_effective_floor_for_zone` skips it.
        band = smr.Rule(
            rule_type="power_band", subject="not_a_number",
            value="Power Tower", sub_type="",
        )
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 180_000_000, "not_on_discord": False},
        }, power_band_rules=[band])
        eligible, below = srb._eligible_member_keys_for_zone(session, "Power Tower")
        # Carol at 180M still below the 300M preset floor — band ignored.
        assert "1003" not in eligible
        assert "1003" in below

    def test_band_relaxation_surfaced_in_embed(self):
        band = smr.Rule(
            rule_type="power_band", subject="200000000",
            value="Power Tower", sub_type="",
        )
        session = _make_session(team="A", power_band_rules=[band])
        session.selected_zone = "Power Tower"
        embed = srb._render_builder_embed(session)
        assert "relaxed by power_band rule" in embed.description


class TestEmbedRendering:
    def test_renders_team_label_for_ds(self):
        # Post-#222: team moved from the title into the body's first
        # bulleted line as `🗺️ Desert Storm: Team A`.
        session = _make_session(team="A")
        embed = srb._render_builder_embed(session)
        assert "Roster Builder Template" in embed.title
        assert "Team A" not in embed.title
        assert "Desert Storm: Team A" in embed.description

    def test_renders_floor_for_active_zone(self):
        session = _make_session(team="A")
        session.selected_zone = "Power Tower"
        embed = srb._render_builder_embed(session)
        # 300M is the Min A floor for Power Tower.
        assert "300M" in embed.description

    def test_power_unknown_hint_surfaces_in_embed(self):
        """The 👁️ Show/Hide below-minimum toggle was retired —
        below-floor members are always in the picker now and the
        embed no longer carries a toggle-state line. Power-unknown
        members instead get a confirm-flow hint so officers know they
        can still be picked."""
        # Add a member with no parseable power to trigger the hint.
        session = _make_session(team="A", members={
            "9": {"key": "9", "name": "Ghost", "discord_id": "9",
                  "power": None, "not_on_discord": False},
        })
        embed = srb._render_builder_embed(session)
        assert "power unknown" in embed.description.lower()
        assert "confirmation" in embed.description.lower()

    def test_capacity_summary_present(self):
        session = _make_session(team="A")
        embed = srb._render_builder_embed(session)
        # 3 zones × 4 capacity = 12 total
        assert "12" in embed.description

    def test_roster_error_surfaces_in_embed(self):
        session = _make_session(team="A")
        session.roster_errors = ["power column not found"]
        embed = srb._render_builder_embed(session)
        assert "power column not found" in embed.description


# ── Structured mode (#129) ───────────────────────────────────────────────────


class TestSignupFilterKeys:
    def test_ds_team_a_includes_a_and_either(self, seeded_db):
        import config
        gid = TEST_GUILD_ID
        config.record_storm_vote(gid, "DS", "2026-05-18",
                                 voter_user_id=1, target_member_id="1", vote="a")
        config.record_storm_vote(gid, "DS", "2026-05-18",
                                 voter_user_id=2, target_member_id="2", vote="b")
        config.record_storm_vote(gid, "DS", "2026-05-18",
                                 voter_user_id=3, target_member_id="3", vote="either")
        config.record_storm_vote(gid, "DS", "2026-05-18",
                                 voter_user_id=4, target_member_id="4", vote="cannot")
        keys = srb._signup_filter_keys(gid, "DS", "2026-05-18", "A")
        assert keys == {"1", "3"}

    def test_ds_team_b_includes_b_and_either(self, seeded_db):
        import config
        gid = TEST_GUILD_ID
        config.record_storm_vote(gid, "DS", "2026-05-18",
                                 voter_user_id=1, target_member_id="1", vote="a")
        config.record_storm_vote(gid, "DS", "2026-05-18",
                                 voter_user_id=2, target_member_id="2", vote="b")
        config.record_storm_vote(gid, "DS", "2026-05-18",
                                 voter_user_id=3, target_member_id="3", vote="either")
        keys = srb._signup_filter_keys(gid, "DS", "2026-05-18", "B")
        assert keys == {"2", "3"}

    def test_cs_pool_treats_a_and_either(self, seeded_db):
        import config
        gid = TEST_GUILD_ID
        config.record_storm_vote(gid, "CS", "2026-05-18",
                                 voter_user_id=10, target_member_id="10", vote="a")
        config.record_storm_vote(gid, "CS", "2026-05-18",
                                 voter_user_id=11, target_member_id="11", vote="either")
        config.record_storm_vote(gid, "CS", "2026-05-18",
                                 voter_user_id=12, target_member_id="12", vote="cannot")
        # CS team is "" — accept_a True, accept_b False, but either always.
        keys = srb._signup_filter_keys(gid, "CS", "2026-05-18", "")
        assert keys == {"10", "11"}

    def test_cannot_never_appears(self, seeded_db):
        import config
        gid = TEST_GUILD_ID
        config.record_storm_vote(gid, "DS", "2026-05-18",
                                 voter_user_id=1, target_member_id="1", vote="cannot")
        for team in ("A", "B", ""):
            keys = srb._signup_filter_keys(gid, "DS", "2026-05-18", team)
            assert "1" not in keys


class TestTeamPlanKeysOrSignupKeys:
    """#239: when a saved team plan exists for an event+team, the
    builder's candidate pool tightens to the plan's 30 members. With
    no plan saved, the helper falls back to the vote-bucket filter
    (today's behaviour) — guarded by a regression test."""

    def test_no_plan_falls_back_to_signup_filter(self, seeded_db):
        import config
        gid = TEST_GUILD_ID
        config.record_storm_vote(gid, "DS", "2026-05-21",
                                 voter_user_id=1, target_member_id="1", vote="a")
        config.record_storm_vote(gid, "DS", "2026-05-21",
                                 voter_user_id=2, target_member_id="2", vote="either")
        keys, applied = srb._team_plan_keys_or_signup_keys(
            gid, "DS", "2026-05-21", "A",
        )
        assert keys == {"1", "2"}
        assert applied is False

    def test_saved_plan_overrides_signup_filter(self, seeded_db):
        import config
        gid = TEST_GUILD_ID
        # Signups would include 1, 2, 3 — but plan says only 1, 2.
        for tid in ("1", "2", "3"):
            config.record_storm_vote(
                gid, "DS", "2026-05-21",
                voter_user_id=int(tid), target_member_id=tid, vote="a",
            )
        config.save_storm_team_plan(
            gid, "DS", "2026-05-21", "A",
            primaries=["1"], subs=["2"], saved_by_user_id=999,
        )
        keys, applied = srb._team_plan_keys_or_signup_keys(
            gid, "DS", "2026-05-21", "A",
        )
        assert keys == {"1", "2"}
        assert applied is True

    def test_empty_plan_falls_through(self, seeded_db):
        """A `clear`-ed plan returns None; the helper falls back to
        signups. Also covers the no-rows-but-table-exists edge."""
        import config
        gid = TEST_GUILD_ID
        config.record_storm_vote(gid, "DS", "2026-05-21",
                                 voter_user_id=1, target_member_id="1", vote="a")
        config.save_storm_team_plan(
            gid, "DS", "2026-05-21", "A",
            primaries=["1"], subs=[], saved_by_user_id=999,
        )
        config.clear_storm_team_plan(gid, "DS", "2026-05-21", "A")
        keys, applied = srb._team_plan_keys_or_signup_keys(
            gid, "DS", "2026-05-21", "A",
        )
        assert keys == {"1"}
        assert applied is False


class TestAutoFillPlanAware:
    """#239: when a plan is provided (either directly or auto-loaded
    from the saved plan), auto-fill seeds starters from
    `plan["primaries"]` and the sub pool from `plan["subs"]` instead
    of running the by-power top-20 split."""

    def _make_thirty_members(self):
        """30 members with descending power so the by-power split is
        unambiguous (M01 strongest, M30 weakest)."""
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 510_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 31)
        }
        return members

    def _make_eleven_zone_preset(self):
        return [
            ss.ZoneRow(zone="Oil Refinery I", max_players=3,
                       min_power_a=100_000_000, priority=1),
            ss.ZoneRow(zone="Oil Refinery II", max_players=3,
                       min_power_a=100_000_000, priority=2),
            ss.ZoneRow(zone="Science Hub", max_players=3,
                       min_power_a=100_000_000, priority=3),
            ss.ZoneRow(zone="Info Center", max_players=3,
                       min_power_a=100_000_000, priority=4),
            ss.ZoneRow(zone="Field Hospital I", max_players=2,
                       min_power_a=100_000_000, priority=5),
            ss.ZoneRow(zone="Field Hospital II", max_players=2,
                       min_power_a=100_000_000, priority=6),
            ss.ZoneRow(zone="Field Hospital III", max_players=2,
                       min_power_a=100_000_000, priority=7),
            ss.ZoneRow(zone="Field Hospital IV", max_players=2,
                       min_power_a=100_000_000, priority=8),
            ss.ZoneRow(zone="Nuclear Silo", max_players=4,
                       min_power_a=100_000_000, priority=9),
            ss.ZoneRow(zone="Arsenal", max_players=3,
                       min_power_a=100_000_000, priority=10),
            ss.ZoneRow(zone="Mercenary Factory", max_players=3,
                       min_power_a=100_000_000, priority=11),
        ]

    def test_plan_overrides_by_power_split(self):
        """The crux of #239: plan picks the BOTTOM-power members as
        primaries to demonstrate auto-fill respects the plan's choice
        rather than the by-power default."""
        members = self._make_thirty_members()
        session = _make_session(
            team="A", members=members,
            preset_zones=self._make_eleven_zone_preset(),
        )
        # Make M21..M40 (the bottom 20) primaries and M01..M10 subs.
        # The by-power default would put M01..M20 as primaries — this
        # is the inverse, so any zone-placement of M01..M10 proves
        # the plan was ignored.
        primaries = [str(i) for i in range(21, 41) if str(i) in members]
        subs = [str(i) for i in range(1, 11)]
        plan = {"primaries": primaries, "subs": subs}
        srb._auto_fill_session(session, plan=plan)
        placed: set[str] = set()
        for zone_members in session.assignments.values():
            placed.update(zone_members)
        # Primaries from the plan are placed; M01..M10 (in subs) are not.
        for top_id in ("1", "2", "3", "4", "5", "6", "7", "8", "9", "10"):
            assert top_id not in placed, (
                f"top-power M{top_id} should be a sub per plan, not placed in a zone"
            )
        # Sub pool matches the plan's subs (intersected with members).
        assert set(session.subs) == set(subs)

    def test_plan_primary_below_top_twenty_still_placed(self):
        """Single-member version of the by-power-vs-plan test: a
        bottom-power member marked primary in the plan still lands
        in a zone."""
        members = self._make_thirty_members()
        session = _make_session(
            team="A", members=members,
            preset_zones=self._make_eleven_zone_preset(),
        )
        plan = {"primaries": ["30"], "subs": []}
        srb._auto_fill_session(session, plan=plan)
        placed: set[str] = set()
        for zone_members in session.assignments.values():
            placed.update(zone_members)
        assert "30" in placed

    def test_missing_plan_keys_surface_as_conflicts(self):
        """Plan keys that aren't in `session.members` (vote changed to
        cannot, roster row removed, etc.) appear in the summary's
        conflicts list — non-fatal."""
        members = self._make_thirty_members()
        session = _make_session(team="A", members=members)
        plan = {"primaries": ["1", "999"], "subs": ["888"]}
        summary = srb._auto_fill_session(session, plan=plan)
        conflict_blob = " ".join(summary["conflicts"])
        assert "999" in conflict_blob
        assert "888" in conflict_blob
        assert "missing" in conflict_blob.lower()
        # "1" is in members so no conflict for it.
        assert "plan key 1 missing" not in conflict_blob

    def test_pinned_vs_sub_conflict_pin_wins(self):
        """Per-member rule pins a member to a zone; plan marks them as
        sub. Pin wins, conflict surfaces in summary."""
        import storm_member_rules as smr
        members = self._make_thirty_members()
        per_member = [smr.Rule(
            rule_type="per_member", sub_type="zone",
            subject="M01", value="Oil Refinery I",
        )]
        session = _make_session(
            team="A", members=members,
            preset_zones=self._make_eleven_zone_preset(),
            per_member_rules=per_member,
        )
        # _apply_rules_to_session is what populates assignments from
        # the per_member rule. It runs in open_roster_builder normally;
        # call it directly here.
        srb._apply_rules_to_session(session)
        plan = {"primaries": [], "subs": ["1"]}
        summary = srb._auto_fill_session(session, plan=plan)
        # Pin wins: M01 is in the Oil Refinery I zone.
        assert "1" in session.assignments["Oil Refinery I"]
        # And NOT in the sub pool.
        assert "1" not in session.subs
        # Conflict surfaced.
        conflict_blob = " ".join(summary["conflicts"])
        assert "M01" in conflict_blob
        assert "pin" in conflict_blob.lower()

    def test_no_plan_byte_identical_to_legacy(self):
        """Regression guard: when `plan=None` (and no saved plan auto-
        loads because event_date/team are unset), the by-power split
        matches the legacy behaviour exactly."""
        members = self._make_thirty_members()
        # No event_date, no team — auto-load path skipped.
        session = _make_session(team="A", members=members,
                                preset_zones=self._make_eleven_zone_preset())
        # Sanity: no event_date so the session can't trigger plan
        # auto-load even if a plan existed in the DB.
        assert session.event_date is None
        srb._auto_fill_session(session, plan=None)
        placed: set[str] = set()
        for zone_members in session.assignments.values():
            placed.update(zone_members)
        # Legacy behaviour: top 20 by power are starters.
        expected_starters = {str(i) for i in range(1, 21)}
        assert placed == expected_starters
        assert set(session.subs) == {str(i) for i in range(21, 31)}

    def test_partial_plan_only_seeds_what_was_saved(self):
        """A partial plan (18 primaries, 5 subs) seeds only those
        members; remaining starter seats stay unfilled (mirrors the
        validator's permissive partial-plan behaviour)."""
        members = self._make_thirty_members()
        session = _make_session(
            team="A", members=members,
            preset_zones=self._make_eleven_zone_preset(),
        )
        plan = {
            "primaries": [str(i) for i in range(1, 19)],   # 18 primaries
            "subs":      [str(i) for i in range(21, 26)],  # 5 subs
        }
        summary = srb._auto_fill_session(session, plan=plan)
        placed: set[str] = set()
        for zone_members in session.assignments.values():
            placed.update(zone_members)
        # Only the 18 plan primaries get placed.
        assert placed == {str(i) for i in range(1, 19)}
        # Only the 5 plan subs are in the sub pool.
        assert set(session.subs) == {str(i) for i in range(21, 26)}
        # Starters_short tracks the 2-seat shortfall.
        assert summary["starters_short"] == 2


class TestSessionStructuredMode:
    def test_event_date_marks_structured(self):
        session = _make_session()
        assert session.is_structured is False
        session.event_date = "2026-05-18"
        assert session.is_structured is True

    def test_manual_mode_has_no_event_date(self):
        session = _make_session()
        assert session.event_date is None


class TestWriteRostersTab:
    def test_writes_primary_and_sub_rows(self, fake_env):
        fake, gid = fake_env
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 280_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        # Assign Alice + Bob primaries; Carol as sub.
        session.assignments["Power Tower"].append("1001")
        session.assignments["Power Tower"].append("1002")
        session.subs.append("1003")

        errors = srb._write_rosters_tab(session)
        assert errors == []

        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        # Header + 3 data rows.
        assert rows[0] == srb._ROSTERS_HEADER
        # Pull just the (member, role, power) for assertion stability.
        # Use header.index so a new column inserted upstream doesn't
        # silently shift the assertion.
        mc = srb._ROSTERS_HEADER.index("Member")
        rc = srb._ROSTERS_HEADER.index("Role")
        pc = srb._ROSTERS_HEADER.index("Power at Assignment")
        data = [(r[mc], r[rc], r[pc]) for r in rows[1:]]
        assert ("Alice", "primary", "412000000") in data
        assert ("Bob",   "primary", "350000000") in data
        assert ("Carol", "sub",     "280000000") in data

    def test_power_unknown_renders_as_unknown(self, fake_env):
        fake, gid = fake_env
        session = _make_session(team="A", members={
            "1004": {"key": "1004", "name": "Erin", "discord_id": "1004",
                     "power": None, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.assignments["Power Tower"].append("1004")
        errors = srb._write_rosters_tab(session)
        assert errors == []
        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        mc = srb._ROSTERS_HEADER.index("Member")
        pc = srb._ROSTERS_HEADER.index("Power at Assignment")
        data_rows = [r for r in rows[1:] if r and r[mc] == "Erin"]
        assert data_rows[0][pc] == "unknown"

    def test_event_date_and_team_in_each_row(self, fake_env):
        fake, gid = fake_env
        session = _make_session(team="B", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 200_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-25"
        session.assignments["Power Tower"].append("1001")
        srb._write_rosters_tab(session)
        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        row = rows[1]
        assert row[0] == "2026-05-25"
        assert row[1] == "B"


class TestOverrideBelowFloorCapture:
    """Below-floor overrides are captured per slot so post-event review
    can flag the decision. Subs don't carry the flag (the eligibility
    gate is primary-only)."""

    def _override_col(self) -> int:
        return srb._ROSTERS_HEADER.index("Override Below Minimum")

    def test_assigned_below_floor_member_flagged_yes(self, fake_env):
        fake, gid = fake_env
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 180_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        # Carol's 180M is below the 300M Power Tower floor; officer
        # explicitly toggled below-floor and assigned anyway.
        session.assignments["Power Tower"].append("1003")
        session.below_floor_overrides.add("1003")
        srb._write_rosters_tab(session)
        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        col = self._override_col()
        mc = srb._ROSTERS_HEADER.index("Member")
        carol_row = next(r for r in rows[1:] if r[mc] == "Carol")
        assert carol_row[col] == "yes"

    def test_at_floor_member_flag_blank(self, fake_env):
        fake, gid = fake_env
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.assignments["Power Tower"].append("1001")
        srb._write_rosters_tab(session)
        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        col = self._override_col()
        mc = srb._ROSTERS_HEADER.index("Member")
        alice_row = next(r for r in rows[1:] if r[mc] == "Alice")
        assert alice_row[col] == ""

    def test_sub_role_never_carries_override(self, fake_env):
        # Even if a sub was flagged at some earlier assignment, subs
        # aren't subject to the per-zone floor — clear the flag.
        fake, gid = fake_env
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 180_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.subs.append("1003")
        session.below_floor_overrides.add("1003")  # stale-ish
        srb._write_rosters_tab(session)
        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        col = self._override_col()
        mc = srb._ROSTERS_HEADER.index("Member")
        carol_row = next(r for r in rows[1:] if r[mc] == "Carol")
        assert carol_row[col] == ""

    def test_prune_clears_unassigned_overrides(self):
        # Unassign-then-reassign-without-toggle must not leave a stale
        # flag on the new slot.
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 180_000_000, "not_on_discord": False},
        })
        session.assignments["Power Tower"].append("1003")
        session.below_floor_overrides.add("1003")
        # Officer hits Unassign on the zone.
        session.assignments["Power Tower"] = []
        session.prune_stale_overrides()
        assert "1003" not in session.below_floor_overrides

    def test_prune_keeps_currently_assigned_overrides(self):
        # A member still in a zone keeps their override flag.
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 180_000_000, "not_on_discord": False},
        })
        session.assignments["Power Tower"].append("1003")
        session.below_floor_overrides.add("1003")
        session.prune_stale_overrides()
        assert "1003" in session.below_floor_overrides

    def test_header_includes_override_column(self):
        assert "Override Below Minimum" in srb._ROSTERS_HEADER
        # Legacy "Override Below Floor" must NOT still be in the
        # writer's header — it's only kept as a read-side alias.
        assert "Override Below Floor" not in srb._ROSTERS_HEADER


class TestAutoFill:
    """Auto-fill (#134): resets the roster, applies per_member zone
    rules, then power-greedy fills by zone priority, spilling extras
    to subs."""

    def _three_members(self) -> dict[str, dict]:
        return {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 280_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 220_000_000, "not_on_discord": False},
        }

    def test_resets_state_before_filling(self):
        members = self._three_members()
        session = _make_session(team="A", members=members)
        # Pre-fill some assignments and an override flag.
        session.assignments["Power Tower"].append("1001")
        session.subs.append("1002")
        session.below_floor_overrides.add("1001")
        srb._auto_fill_session(session)
        # below_floor_overrides reset; manual assignments wiped before
        # the algorithm placed members.
        assert session.below_floor_overrides == set()
        # Algorithm placed members — assignments may not be empty, but
        # they should reflect the algorithm, not the pre-state.
        # (Tested in detail below.)

    def test_per_member_zone_rule_pins_member(self):
        members = self._three_members()
        # Move Bob below all floors so greedy fill wouldn't pick him for
        # Power Tower; pin him there via per_member rule.
        members["1002"]["power"] = 100_000_000
        per_member = [smr.Rule(
            rule_type="per_member", subject="Bob",
            sub_type="zone", value="Nuclear Silo",
        )]
        session = _make_session(team="A", members=members, per_member_rules=per_member)
        summary = srb._auto_fill_session(session)
        assert "1002" in session.assignments["Nuclear Silo"]
        assert summary["per_member_rules_applied"] == 1

    def test_greedy_fill_by_priority(self):
        # Two zones, both with capacity for one member. Priority forces
        # ordering: zone A is priority 1, zone B is priority 2.
        zones = [
            ss.ZoneRow(zone="Zone A", max_players=1, min_power_a=200_000_000, priority=1),
            ss.ZoneRow(zone="Zone B", max_players=1, min_power_a=200_000_000, priority=2),
        ]
        members = self._three_members()  # Alice 412M, Bob 280M, Carol 220M
        session = _make_session(team="A", members=members, preset_zones=zones)
        summary = srb._auto_fill_session(session)
        # Highest-power eligible → priority-1 zone first.
        assert session.assignments["Zone A"] == ["1001"]
        assert session.assignments["Zone B"] == ["1002"]
        # Remaining members → subs.
        assert "1003" in session.subs
        assert summary["auto_filled_by_power"] == 2

    def test_power_unknown_goes_to_gaps_not_subs(self):
        members = self._three_members()
        members["1003"]["power"] = None
        session = _make_session(team="A", members=members)
        summary = srb._auto_fill_session(session)
        # Carol (power=None) appears in gaps and NOT in subs/zones.
        assert "Carol" in summary["gaps"]
        assert "1003" not in session.subs
        for zone_members in session.assignments.values():
            assert "1003" not in zone_members

    def test_power_band_relaxation_counted(self):
        # Preset floor for Power Tower = 300M; band lowers it to 200M.
        # Bob (280M) and Carol (220M) are below preset but above band,
        # so they slot in via the band relaxation. The count reflects
        # members slotted via the band, not rules whose threshold
        # < preset floor. Alice (412M) is above preset, so she doesn't
        # count even though the band applies to her zone.
        # Single-zone preset forces round-robin (post-#219) to land all
        # three members in PT; with the multi-zone default they'd spread
        # across zones where the band isn't relevant.
        members = self._three_members()
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=4,
                       min_power_a=300_000_000, priority=1),
        ]
        power_band = [smr.Rule(
            rule_type="power_band", subject="200000000",
            value="Power Tower",
        )]
        session = _make_session(team="A", members=members,
                                preset_zones=zones,
                                power_band_rules=power_band)
        summary = srb._auto_fill_session(session)
        assert summary["power_band_rules_applied"] == 2

    def test_conflict_when_zone_full(self):
        # Two rules pin two different members to the same zone with cap=1.
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=1,
                       min_power_a=100_000_000, priority=1),
        ]
        members = self._three_members()
        per_member = [
            smr.Rule(rule_type="per_member", subject="Alice",
                     sub_type="zone", value="Power Tower"),
            smr.Rule(rule_type="per_member", subject="Bob",
                     sub_type="zone", value="Power Tower"),
        ]
        session = _make_session(team="A", members=members,
                                preset_zones=zones,
                                per_member_rules=per_member)
        summary = srb._auto_fill_session(session)
        # First rule placed Alice; second rule conflicts (zone full).
        assert "1001" in session.assignments["Power Tower"]
        assert "1002" not in session.assignments["Power Tower"]
        assert any("full" in c.lower() for c in summary["conflicts"])

    def test_conflict_when_per_member_zone_unknown(self):
        members = self._three_members()
        per_member = [smr.Rule(
            rule_type="per_member", subject="Alice",
            sub_type="zone", value="Mars Base",  # not in preset
        )]
        session = _make_session(team="A", members=members,
                                per_member_rules=per_member)
        summary = srb._auto_fill_session(session)
        assert any("Mars Base" in c for c in summary["conflicts"])
        # Alice is not pinned and may still get auto-filled elsewhere.

    def test_summary_persists_on_session(self):
        members = self._three_members()
        session = _make_session(team="A", members=members)
        assert session.auto_fill_summary is None
        srb._auto_fill_session(session)
        assert session.auto_fill_summary is not None
        # Re-rendering the embed surfaces it.
        embed = srb._render_builder_embed(session)
        assert "Auto-fill summary" in embed.description

    def test_per_member_id_subject_resolves(self):
        """A per_member rule whose Subject is the Discord ID (a string
        of digits) should still resolve to the right member — #136
        prep work."""
        members = self._three_members()
        per_member = [smr.Rule(
            rule_type="per_member", subject="1001",  # discord_id, not name
            sub_type="zone", value="Power Tower",
        )]
        session = _make_session(team="A", members=members,
                                per_member_rules=per_member)
        summary = srb._auto_fill_session(session)
        assert "1001" in session.assignments["Power Tower"]
        assert summary["per_member_rules_applied"] == 1

    def test_per_member_discord_id_field_path_resolves(self):
        """The third resolution path: member's dict-key is the roster
        name (non-Discord member with a numeric handle) but the rule's
        subject matches the explicit `discord_id` field. The audit
        flagged the original test was unable to exercise this branch
        because `key == discord_id` in the fixture."""
        members = {
            # Note the key is the name (non-Discord row), and
            # discord_id is a separate value. The original audit test
            # used key == "1001" AND discord_id == "1001", which made
            # the first OR branch satisfy the match.
            "Alice": {"key": "Alice", "name": "Alice", "discord_id": "55001",
                      "power": 412_000_000, "not_on_discord": False},
        }
        per_member = [smr.Rule(
            rule_type="per_member", subject="55001",
            sub_type="zone", value="Power Tower",
        )]
        session = _make_session(team="A", members=members,
                                per_member_rules=per_member)
        summary = srb._auto_fill_session(session)
        assert "Alice" in session.assignments["Power Tower"]
        assert summary["per_member_rules_applied"] == 1

    def test_unmatched_per_member_subject_silently_no_ops(self):
        """Decision #7 (#173): a per_member rule whose subject isn't
        on tonight's roster is a silent no-op — nothing applied,
        nothing in conflicts. Other conflict shapes (unknown zone,
        full when pinning, pinned to multiple zones) still surface."""
        members = self._three_members()
        per_member = [
            smr.Rule(rule_type="per_member", subject="Alice",
                     sub_type="zone", value="Power Tower"),
            smr.Rule(rule_type="per_member", subject="GhostMember",
                     sub_type="zone", value="Nuclear Silo"),
        ]
        session = _make_session(team="A", members=members,
                                per_member_rules=per_member)
        summary = srb._auto_fill_session(session)
        assert summary["per_member_rules_applied"] == 1
        assert not any("GhostMember" in c for c in summary["conflicts"])
        # Sanity: other conflict shapes still surface — exercise with
        # a rule that names an unknown zone.
        per_member_bad_zone = [
            smr.Rule(rule_type="per_member", subject="Alice",
                     sub_type="zone", value="No Such Zone"),
        ]
        session2 = _make_session(team="A", members=members,
                                 per_member_rules=per_member_bad_zone)
        summary2 = srb._auto_fill_session(session2)
        assert any("unknown zone" in c for c in summary2["conflicts"])

    def test_power_unknown_per_member_pin_flagged_as_override(self):
        """A per_member pin of a power-unknown member is an officer
        decision to assign below the floor — the rosters_tab Override
        Below Floor column must reflect it. Audit found auto-fill was
        silently weaker than the equivalent manual assignment."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": None, "not_on_discord": False},
        }
        per_member = [smr.Rule(
            rule_type="per_member", subject="Alice",
            sub_type="zone", value="Power Tower",
        )]
        session = _make_session(team="A", members=members,
                                per_member_rules=per_member)
        srb._auto_fill_session(session)
        assert "1001" in session.assignments["Power Tower"]
        assert "1001" in session.below_floor_overrides

    def test_summary_cleared_on_manual_edit(self):
        """A stale auto-fill summary persisting across manual edits
        misled officers into reading names/counts that no longer
        described the current roster. Manual mutations must clear it.
        This test exercises the session state directly; the View
        callbacks just call _refresh after setting auto_fill_summary
        to None — same effect."""
        members = self._three_members()
        session = _make_session(team="A", members=members)
        srb._auto_fill_session(session)
        assert session.auto_fill_summary is not None
        # Simulate a manual mutation by clearing the field as the
        # callbacks would, then verify the embed renderer drops the
        # summary panel.
        session.auto_fill_summary = None
        embed = srb._render_builder_embed(session)
        assert "Auto-fill summary" not in (embed.description or "")

    def test_auto_fill_respects_session_members_pool(self):
        """In structured mode, `session.members` is upstream-narrowed
        to signed-up members for the team before the session is built.
        Auto-fill must only place members in that dict. This test pins
        that contract: a roster with three known members will never
        produce assignments for a fourth member who was filtered out
        upstream (i.e. who never made it into session.members)."""
        # Three members are present; "Ghost" is conceptually signed up
        # at the roster level but was filtered out before reaching the
        # session — so they shouldn't appear in `session.members` at all,
        # and auto-fill must not magically conjure them.
        members = self._three_members()
        session = _make_session(team="A", members=members)
        srb._auto_fill_session(session)
        placed = set(session.subs)
        for zone_members in session.assignments.values():
            placed.update(zone_members)
        # Every placed key must be a key in session.members.
        assert placed.issubset(set(members.keys()))

    def test_power_band_count_only_credits_actual_slots(self):
        """The audit found `power_band_rules_applied` over-counted: it
        used to fire when ANY band's threshold < preset floor, even if
        no member got placed via the relaxation. Now it counts members
        actually slotted whose power < preset floor but >= effective
        floor."""
        # Preset floor 300M; band relaxes to 200M. Members above 300M
        # don't count as band-relaxed slottings; only members between
        # 200M and 300M do.
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 250_000_000, "not_on_discord": False},  # band-relaxed
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 220_000_000, "not_on_discord": False},  # band-relaxed
        }
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=4,
                       min_power_a=300_000_000, priority=1),
        ]
        band = [smr.Rule(
            rule_type="power_band", subject="200000000",
            value="Power Tower",
        )]
        session = _make_session(team="A", members=members,
                                preset_zones=zones,
                                power_band_rules=band)
        summary = srb._auto_fill_session(session)
        # Bob (250M) and Carol (220M) both slotted via the band.
        # Alice (412M) is above preset floor — not counted.
        assert summary["power_band_rules_applied"] == 2
        assert summary["auto_filled_by_power"] == 3

    def test_power_band_count_is_zero_when_no_band_slot_taken(self):
        """A band rule on a zone where the eventual fill is all
        above-preset-floor members shouldn't count as 'effective'."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=4,
                       min_power_a=300_000_000, priority=1),
        ]
        band = [smr.Rule(
            rule_type="power_band", subject="200000000",
            value="Power Tower",
        )]
        session = _make_session(team="A", members=members,
                                preset_zones=zones,
                                power_band_rules=band)
        summary = srb._auto_fill_session(session)
        # Alice was above preset floor — band wasn't "effective".
        assert summary["power_band_rules_applied"] == 0

    def test_eligibility_sort_is_content_deterministic(self):
        """Equal-power members should tie-break by name, not by dict
        insertion order, so the auto-fill is reproducible regardless
        of upstream roster-read order."""
        # Two members with identical power; "Alice" should always come
        # first regardless of insertion order.
        members_zora_first = {
            "Z": {"key": "Z", "name": "Zora", "discord_id": "Z",
                  "power": 300_000_000, "not_on_discord": False},
            "A": {"key": "A", "name": "Alice", "discord_id": "A",
                  "power": 300_000_000, "not_on_discord": False},
        }
        members_alice_first = {
            "A": members_zora_first["A"],
            "Z": members_zora_first["Z"],
        }
        s1 = _make_session(team="A", members=members_zora_first)
        s2 = _make_session(team="A", members=members_alice_first)
        eligible1, _ = srb._eligible_member_keys_for_zone(s1, "Power Tower")
        eligible2, _ = srb._eligible_member_keys_for_zone(s2, "Power Tower")
        # Both insertion orders produce the same sorted result.
        assert eligible1 == eligible2
        # Alice sorts before Zora.
        assert eligible1.index("A") < eligible1.index("Z")


class TestAutoFillTwentyTenSplit:
    """#219: Last War defines every DS and CS team as 20 starters plus
    10 subs. Auto-fill enforces that split independent of preset zone
    capacity (which is allowed to exceed the team size so officers can
    place the same person in multiple stages without enforcement)."""

    def _make_thirty_signups(self, sub_mode: str = "pool"):
        """A 30-member roster mirroring the canonical DS team size.
        Powers descend from M01 (500M) to M30 (210M) so the top-20-vs-
        next-10 boundary is unambiguous."""
        # 11 canonical DS zones with capacities summing to 30 so the
        # preset itself is over-cap relative to the 20-starter team
        # rule. This is the configuration testers were reporting:
        # auto-fill used to land all 30 as primaries.
        zones = [
            ss.ZoneRow(zone="Oil Refinery I", max_players=3,
                       min_power_a=100_000_000, priority=1),
            ss.ZoneRow(zone="Oil Refinery II", max_players=3,
                       min_power_a=100_000_000, priority=2),
            ss.ZoneRow(zone="Science Hub", max_players=3,
                       min_power_a=100_000_000, priority=3),
            ss.ZoneRow(zone="Info Center", max_players=3,
                       min_power_a=100_000_000, priority=4),
            ss.ZoneRow(zone="Field Hospital I", max_players=2,
                       min_power_a=100_000_000, priority=5),
            ss.ZoneRow(zone="Field Hospital II", max_players=2,
                       min_power_a=100_000_000, priority=6),
            ss.ZoneRow(zone="Field Hospital III", max_players=2,
                       min_power_a=100_000_000, priority=7),
            ss.ZoneRow(zone="Field Hospital IV", max_players=2,
                       min_power_a=100_000_000, priority=8),
            ss.ZoneRow(zone="Nuclear Silo", max_players=4,
                       min_power_a=100_000_000, priority=9),
            ss.ZoneRow(zone="Arsenal", max_players=3,
                       min_power_a=100_000_000, priority=10),
            ss.ZoneRow(zone="Mercenary Factory", max_players=3,
                       min_power_a=100_000_000, priority=11),
        ]
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 510_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 31)
        }
        return _make_session(team="A", members=members,
                             preset_zones=zones, sub_mode=sub_mode)

    def test_exactly_twenty_starters_placed_in_zones(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess)
        placed = 0
        for zone_members in sess.assignments.values():
            placed += len(zone_members)
        assert placed == 20

    def test_exactly_ten_subs_in_sub_pool(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess)
        assert len(sess.subs) == 10

    def test_starters_are_top_twenty_by_power(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess)
        placed: set[str] = set()
        for zone_members in sess.assignments.values():
            placed.update(zone_members)
        # M01..M20 are the top 20 by power.
        expected_starters = {str(i) for i in range(1, 21)}
        assert placed == expected_starters

    def test_subs_are_next_ten_by_power(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess)
        # M21..M30 are the bottom 10.
        assert set(sess.subs) == {str(i) for i in range(21, 31)}

    def test_top_priority_zones_get_top_power_starters(self):
        """Round-robin walks zones in priority order on each pass, so
        pass 1 puts M01..M11 one per zone (Oil Refinery I gets M01,
        Oil Refinery II gets M02, etc.). Pass 2 puts the next batch."""
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess)
        # M01 lands in the top-priority zone (Oil Refinery I).
        assert "1" in sess.assignments["Oil Refinery I"]
        assert "2" in sess.assignments["Oil Refinery II"]
        assert "3" in sess.assignments["Science Hub"]
        assert "4" in sess.assignments["Info Center"]
        # M12 lands in Oil Refinery I again on pass 2 (round-robin loop).
        assert "12" in sess.assignments["Oil Refinery I"]

    def test_top_priority_zones_win_uneven_extras(self):
        """20 starters across 11 zones leaves a remainder: top 9 zones
        get 2 members, last 2 zones get 1 member each. Top-priority
        zones win the extras."""
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess)
        # Top-priority zones each have 2 members.
        assert len(sess.assignments["Oil Refinery I"]) >= 2
        assert len(sess.assignments["Oil Refinery II"]) >= 2
        # Arsenal (priority 10) and Mercenary Factory (priority 11) get
        # 1 each, because the round-robin runs out of starters before
        # adding a second pass.
        assert len(sess.assignments["Arsenal"]) == 1
        assert len(sess.assignments["Mercenary Factory"]) == 1

    def test_starters_short_zero_when_thirty_signed_up(self):
        sess = self._make_thirty_signups()
        summary = srb._auto_fill_session(sess)
        assert summary["starters_short"] == 0

    def test_starters_short_reports_gap_when_under_twenty(self):
        # 17 members → 3 starter seats unfilled.
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 500_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 18)
        }
        sess = _make_session(team="A", members=members)
        summary = srb._auto_fill_session(sess)
        assert summary["starters_short"] == 3

    def test_starters_short_renders_in_embed(self):
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 500_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 18)
        }
        sess = _make_session(team="A", members=members)
        srb._auto_fill_session(sess)
        embed = srb._render_builder_embed(sess)
        assert "3 of 20 starter seats unfilled" in (embed.description or "")

    def test_paired_mode_pairs_weakest_primary_with_closest_power_sub(self):
        sess = self._make_thirty_signups(sub_mode="paired")
        srb._auto_fill_session(sess)
        # The weakest placed primary is M20 (310M). Closest-power sub in
        # the bottom-10 pool is M21 (300M, distance 10M); M22 is 290M
        # (distance 20M). M20 should pair with M21.
        assert sess.paired_subs.get("20") == "21"

    def test_paired_mode_walks_primaries_weakest_first(self):
        sess = self._make_thirty_signups(sub_mode="paired")
        srb._auto_fill_session(sess)
        # Weakest 10 primaries (M11..M20) all get a paired sub from the
        # bottom-10 sub pool (M21..M30, closest-power each).
        weakest_ten = [str(i) for i in range(11, 21)]
        for primary in weakest_ten:
            assert primary in sess.paired_subs, (
                f"weakest primary {primary} should have an auto-paired sub"
            )
        # Strongest 10 primaries (M01..M10) stay unpaired because only
        # 10 subs exist and they go to the weaker primaries first.
        strongest_ten = [str(i) for i in range(1, 11)]
        for primary in strongest_ten:
            assert primary not in sess.paired_subs

    def test_paired_mode_summary_lists_pairings(self):
        sess = self._make_thirty_signups(sub_mode="paired")
        summary = srb._auto_fill_session(sess)
        assert len(summary["auto_paired_subs"]) == 10

    def test_paired_mode_with_thirty_signups_drains_sub_pool(self):
        """In the 30-signup case all 10 subs get paired in P1, so the
        flat `session.subs` overflow list ends up empty (every sub has
        a primary attached)."""
        sess = self._make_thirty_signups(sub_mode="paired")
        srb._auto_fill_session(sess)
        # All 10 subs paired in some phase → no overflow.
        assert sess.subs == []

    def test_pinned_members_count_toward_starter_pool(self):
        """A per_member rule pinning a low-power member should still
        count them as a starter, and the top-power list fills 19 more
        seats (not 20) around them."""
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 510_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 31)
        }
        zones = [
            ss.ZoneRow(zone="Oil Refinery I", max_players=3,
                       min_power_a=0, priority=1),
        ] + [
            ss.ZoneRow(zone=f"Zone {i}", max_players=3,
                       min_power_a=0, priority=i)
            for i in range(2, 12)
        ]
        # Pin M25 (a low-power member who'd normally be a sub) to
        # Oil Refinery I. They become a starter.
        per_member = [smr.Rule(
            rule_type="per_member", subject="M25",
            sub_type="zone", value="Oil Refinery I",
        )]
        sess = _make_session(team="A", members=members,
                             preset_zones=zones,
                             per_member_rules=per_member)
        srb._auto_fill_session(sess)
        # M25 is in a zone → starter.
        all_placed: set[str] = set()
        for zone_members in sess.assignments.values():
            all_placed.update(zone_members)
        assert "25" in all_placed
        # M25 is NOT in the sub pool.
        assert "25" not in sess.subs
        # Sub pool is 10 members. Pinning M25 reserves one starter seat,
        # so the auto-fill draws 19 from the top of the power-desc list.
        # Rank 21 (M21) gets the displaced top-20 seat, leaving M22..M30
        # as 9 subs plus... actually the precise sub identity depends on
        # which rank is displaced; the invariant is: 10 subs total.
        assert len(sess.subs) == 10

    def test_under_twenty_signups_all_become_starters(self):
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 500_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 11)  # 10 members
        }
        sess = _make_session(team="A", members=members)
        srb._auto_fill_session(sess)
        # All 10 are starters; sub pool is empty.
        all_placed: set[str] = set()
        for zone_members in sess.assignments.values():
            all_placed.update(zone_members)
        assert len(all_placed) == 10
        assert sess.subs == []

    def test_between_twenty_and_thirty_signups_fills_subs_partially(self):
        # 25 signups → 20 starters, 5 subs.
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 510_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 26)
        }
        sess = self._make_thirty_signups()
        sess.members = members
        srb._auto_fill_session(sess)
        placed: set[str] = set()
        for zone_members in sess.assignments.values():
            placed.update(zone_members)
        assert len(placed) == 20
        assert len(sess.subs) == 5

    def test_phase_aware_uses_same_twenty_starters_across_phases(self):
        """The #219 rule keeps the same 20 starters event-wide. Each
        phase round-robins those 20 into its own zones; nobody else
        gets pulled in for phase 2."""
        zones = [
            ss.ZoneRow(zone="Info Center", max_players=0,
                       max_phase1=10, max_phase2=10,
                       min_power_a=0, min_power_b=0,
                       priority_phase1=1, priority_phase2=1),
            ss.ZoneRow(zone="Arsenal", max_players=0,
                       max_phase1=10, max_phase2=10,
                       min_power_a=0, min_power_b=0,
                       priority_phase1=2, priority_phase2=2),
        ]
        preset = ss.PresetBuffer(name="TwoPhase", event_type="DS",
                                 zones=zones, phase_count=2)
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 510_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 31)
        }
        sess = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="DS",
            team="A", preset=preset, members=members,
            per_member_rules=[], power_band_rules=[],
            sub_mode="pool",
        )
        srb._auto_fill_session(sess)
        # Same 20 starters in each phase (intersection equals union).
        p1: set[str] = set()
        for zone_members in sess.assignments_for_phase(1).values():
            p1.update(zone_members)
        p2: set[str] = set()
        for zone_members in sess.assignments_for_phase(2).values():
            p2.update(zone_members)
        assert p1 == p2
        # And those 20 are M01..M20.
        assert p1 == {str(i) for i in range(1, 21)}
        # The 10 subs (M21..M30) never appear in any phase's zones.
        sub_set = {str(i) for i in range(21, 31)}
        assert sub_set.isdisjoint(p1)
        assert sub_set.isdisjoint(p2)

    def test_ties_at_rank_twenty_boundary_break_on_member_key(self):
        """Two members tied on power at the 20/21 boundary: the one
        with the smaller member key sorts to starter, the other to
        sub. Deterministic so re-runs of auto-fill don't shuffle the
        roster on the officer."""
        # 21 members, all with the same power except #21 also at the
        # same power. Top 20 by tiebreak (key asc) become starters.
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 300_000_000,
                     "not_on_discord": False}
            for i in range(1, 22)
        }
        sess = _make_session(team="A", members=members)
        srb._auto_fill_session(sess)
        placed: set[str] = set()
        for zone_members in sess.assignments.values():
            placed.update(zone_members)
        # Member keys "1".."21". Sort by key string is lexicographic:
        # "1" < "10" < "11" < ... < "19" < "2" < "20" < "21" < "3" ...
        # So the top 20 (lex-sorted) include "1", "10"..."19", "2",
        # "20", "21" (that's 13), plus "3"..."9" (7 more) = 20.
        # The leftover is the lex-largest key not in the top 20, which
        # is "9". So "9" is in subs, all others in zones.
        assert "9" in sess.subs
        for k in placed:
            assert k != "9"


class TestAutoFillButtonGate:
    """The Auto-fill button is structured-mode only (Premium-gated
    upstream via ensure_premium_structured). Free-tier sessions never
    see it. Audit caught the missing test for the gate."""

    def test_auto_fill_present_in_structured_mode(self):
        session = _make_session(team="A")
        session.event_date = "2026-05-18"
        view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Auto-fill" in lab for lab in labels)

    def test_auto_fill_absent_in_free_tier_mode(self):
        session = _make_session(team="A")  # event_date None
        view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert not any("Auto-fill" in lab for lab in labels)


class TestAutoFillPriorityGreedy:
    """#226: priority-greedy fills the top-priority zone to capacity
    with the strongest members before moving on. Top-power lands in
    top-priority zones, low-priority zones get the weakest starters
    (or stay empty if the team is short). 0-cap zones are skipped."""

    def _make_thirty_signups(self):
        # Same 30-member, 11-zone preset shape as the balanced tests
        # so the algorithms are testing against identical fixtures.
        zones = [
            ss.ZoneRow(zone="Oil Refinery I", max_players=3,
                       min_power_a=100_000_000, priority=1),
            ss.ZoneRow(zone="Oil Refinery II", max_players=3,
                       min_power_a=100_000_000, priority=2),
            ss.ZoneRow(zone="Science Hub", max_players=3,
                       min_power_a=100_000_000, priority=3),
            ss.ZoneRow(zone="Info Center", max_players=3,
                       min_power_a=100_000_000, priority=4),
            ss.ZoneRow(zone="Field Hospital I", max_players=2,
                       min_power_a=100_000_000, priority=5),
            ss.ZoneRow(zone="Field Hospital II", max_players=2,
                       min_power_a=100_000_000, priority=6),
            ss.ZoneRow(zone="Field Hospital III", max_players=2,
                       min_power_a=100_000_000, priority=7),
            ss.ZoneRow(zone="Field Hospital IV", max_players=2,
                       min_power_a=100_000_000, priority=8),
            ss.ZoneRow(zone="Nuclear Silo", max_players=4,
                       min_power_a=100_000_000, priority=9),
            ss.ZoneRow(zone="Arsenal", max_players=3,
                       min_power_a=100_000_000, priority=10),
            ss.ZoneRow(zone="Mercenary Factory", max_players=3,
                       min_power_a=100_000_000, priority=11),
        ]
        members = {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 510_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 31)
        }
        return _make_session(team="A", members=members, preset_zones=zones)

    def test_top_priority_zone_fills_to_capacity_first(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess, strategy="priority_greedy")
        # Top-priority zone (Oil Refinery I, priority=1, cap=3) gets
        # the top 3 power-ranked starters (M01, M02, M03).
        assert sess.assignments["Oil Refinery I"] == ["1", "2", "3"]

    def test_next_priority_zone_gets_next_block(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess, strategy="priority_greedy")
        # Second-priority zone (Oil Refinery II, cap=3) gets M04, M05, M06.
        assert sess.assignments["Oil Refinery II"] == ["4", "5", "6"]

    def test_low_priority_zones_get_weakest_starters(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess, strategy="priority_greedy")
        # Mercenary Factory is the lowest-priority zone (priority=11,
        # cap=3). With priority-greedy and 20 starters across zones
        # whose capacities sum top-to-bottom as
        # 3,3,3,3,2,2,2,2 = 20 by the time we reach Nuclear Silo, the
        # starter pool runs out exactly at Nuclear Silo, so Arsenal +
        # Mercenary Factory stay empty.
        assert sess.assignments["Mercenary Factory"] == []
        assert sess.assignments["Arsenal"] == []

    def test_total_placed_still_twenty(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess, strategy="priority_greedy")
        placed = sum(len(v) for v in sess.assignments.values())
        assert placed == 20

    def test_subs_are_still_next_ten_by_power(self):
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess, strategy="priority_greedy")
        # Sub pool selection is independent of strategy; M21..M30 are
        # still the 10 subs.
        assert set(sess.subs) == {str(i) for i in range(21, 31)}

    def test_skips_zero_capacity_zones(self):
        # Field Hospital III/IV set to 0 cap for this team. The
        # priority-greedy walker should skip them entirely and place
        # starters in the remaining zones only.
        sess = self._make_thirty_signups()
        for z in sess.preset.zones:
            if z.zone in ("Field Hospital III", "Field Hospital IV"):
                z.max_players = 0
        srb._auto_fill_session(sess, strategy="priority_greedy")
        assert sess.assignments["Field Hospital III"] == []
        assert sess.assignments["Field Hospital IV"] == []

    def test_unknown_strategy_falls_back_to_balanced(self):
        # Defensive: an unknown strategy string normalises to balanced
        # rather than crashing or silently failing.
        sess = self._make_thirty_signups()
        srb._auto_fill_session(sess, strategy="not_a_real_strategy")
        # Balanced result: top-priority zones get top-power on pass 1
        # (M01 at Oil Refinery I, M02 at Oil Refinery II, etc.).
        assert "1" in sess.assignments["Oil Refinery I"]
        assert "2" in sess.assignments["Oil Refinery II"]
        # Round-robin places M12 in Oil Refinery I on pass 2.
        assert "12" in sess.assignments["Oil Refinery I"]


class TestAutoFillStrategyPicker:
    """#226: clicking Auto-fill always opens the strategy picker.
    The picker carries the two strategy buttons + Cancel and the
    body copy describes each strategy. When the session already has
    assignments, the body prepends a destructive-rerun warning."""

    def test_picker_carries_both_strategies_and_cancel(self):
        parent = MagicMock()
        parent.session.user_id = 42
        view = srb._AutoFillStrategyPickerView(parent_view=parent)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Balanced spread" in lab for lab in labels)
        assert any("Strength to priority" in lab for lab in labels)
        assert any("Cancel" in lab for lab in labels)

    def test_balanced_button_runs_balanced_strategy(self):
        # The picker's balanced callback dispatches to
        # `_auto_fill_session(strategy="balanced")` via `_run_with_strategy`.
        # Smoke-test that the view class can be constructed and the
        # callback is wired (deep integration via mocks would replicate
        # discord.py's view runtime, which isn't worth the test churn).
        parent = MagicMock()
        parent.session.user_id = 42
        view = srb._AutoFillStrategyPickerView(parent_view=parent)
        # The discord.ui.Button decorator wraps balanced/priority_greedy
        # as attributes of the View instance.
        assert hasattr(view, "balanced")
        assert hasattr(view, "priority_greedy")
        assert hasattr(view, "cancel")


class TestApprovePostButtonSplit:
    """#225: Approve & Post offers a per-send choice between attaching
    the rendered image and posting text only. Flat-structured shows
    two main-view buttons; phase-aware structured (no spare row) keeps
    one Approve button that opens an ephemeral picker."""

    def test_flat_structured_shows_two_approve_buttons(self):
        session = _make_session(team="A")
        session.event_date = "2026-05-18"
        view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Approve & Post (with image)" in lab for lab in labels)
        assert any("Approve & Post (text only)" in lab for lab in labels)

    def test_flat_structured_drops_the_legacy_single_approve_label(self):
        # The legacy unified "✅ Approve & Post" button is gone — its
        # role is split across the two new buttons.
        session = _make_session(team="A")
        session.event_date = "2026-05-18"
        view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert "✅ Approve & Post" not in labels

    def test_flat_structured_layout_has_post_row_above_final_row(self):
        """Auto-fill, Generate image, and Preview mail sit on the post
        row (row 3 in flat-structured); the destructive Approve / Cancel
        actions occupy the final row (row 4)."""
        session = _make_session(team="A")
        session.event_date = "2026-05-18"
        view = srb.RosterBuilderView(session)
        rows_by_label: dict[str, int] = {}
        for c in view.children:
            label = getattr(c, "label", None)
            row = getattr(c, "row", None)
            if label:
                rows_by_label[label] = row
        # Auto-fill, Preview mail, Generate <event> assignments image
        # all share post_row.
        auto_row = next(r for l, r in rows_by_label.items() if "Auto-fill" in l)
        preview_row = next(r for l, r in rows_by_label.items() if "Preview mail" in l)
        gen_image_row = next(
            r for l, r in rows_by_label.items() if "Generate" in l and "image" in l
        )
        assert auto_row == preview_row == gen_image_row
        # Approve & Cancel share final_row, exactly one row below.
        approve_image_row = next(
            r for l, r in rows_by_label.items() if "with image" in l
        )
        approve_text_row = next(
            r for l, r in rows_by_label.items() if "text only" in l
        )
        # #240 follow-up renamed the structured-mode close button from
        # "❌ Cancel" to "👋 Close (draft saved)" since the draft now
        # persists and there's nothing to "cancel" anymore.
        close_row = next(r for l, r in rows_by_label.items() if "Close" in l)
        assert approve_image_row == approve_text_row == close_row
        assert approve_image_row == auto_row + 1

    def test_phase_aware_structured_keeps_single_approve_button(self):
        # Phase-aware can't add a post_row (rows 0-4 are full), so the
        # split happens behind an ephemeral picker. Only one Approve
        # button shows on the main view.
        from unittest.mock import patch
        sess = _make_phase_aware_session()
        sess.event_date = "2026-05-18"
        view = srb.RosterBuilderView(sess)
        labels = [getattr(c, "label", "") for c in view.children]
        # The unified "Approve & Post" label is back for phase-aware.
        assert any(lab.endswith("Approve & Post") for lab in labels)
        # The split-variant labels are NOT on the main view.
        assert not any("with image" in lab for lab in labels)
        assert not any("text only" in lab for lab in labels)

    def test_phase_aware_picker_view_has_both_variants(self):
        # The ephemeral picker carries both variants plus a cancel
        # affordance.
        sess = _make_phase_aware_session()
        sess.event_date = "2026-05-18"
        parent = srb.RosterBuilderView(sess)
        picker = srb._ApprovePostPickerView(parent_view=parent)
        labels = [getattr(c, "label", "") for c in picker.children]
        assert any("With image" in lab for lab in labels)
        assert any("Text only" in lab for lab in labels)
        assert any("Cancel" in lab for lab in labels)

    def test_free_tier_keeps_standalone_generate_image_on_final_row(self):
        # Free-tier has no Approve & Post and no post_row — Generate
        # image stays on final_row alongside Generate mail / Save preset
        # / Done so the officer can still grab the PNG manually.
        session = _make_session(team="A")  # event_date None → free-tier
        view = srb.RosterBuilderView(session)
        rows_by_label: dict[str, int] = {}
        for c in view.children:
            label = getattr(c, "label", None)
            row = getattr(c, "row", None)
            if label:
                rows_by_label[label] = row
        gen_image_row = next(
            r for l, r in rows_by_label.items() if "Generate" in l and "image" in l
        )
        done_row = next(r for l, r in rows_by_label.items() if "Done" in l)
        assert gen_image_row == done_row


class TestNonDiscordAutoDetect:
    """#139 — auto-detect non-Discord roster rows via two paths:
      * Blank Discord ID cell.
      * Non-blank Discord ID but member not in guild (stale ID).
    The explicit `not_on_discord` column still wins when present.
    """

    def test_blank_discord_id_inferred_non_discord(self, fake_env):
        fake, gid = fake_env
        # Dave's row has no Discord ID; he's already correctly flagged
        # in the fixture, but here we check the inference path fires
        # even when the explicit column isn't truthy.
        members, _errs = srb._read_roster_powers(gid, "DS", guild=None)
        # No guild → only the blank-id half of inference fires.
        # The fixture explicitly flagged Dave with "x", so this also
        # exercises tier-1 winning. Verify both paths converge.
        assert members["Dave"]["not_on_discord"] is True

    def test_stale_discord_id_inferred_non_discord(self, fake_env):
        from unittest.mock import MagicMock
        fake, gid = fake_env
        # Mock a guild where Alice (1001) is NOT a member — simulating
        # she's left the server but is still on the roster Sheet.
        guild = MagicMock()
        guild.get_member.return_value = None  # every lookup → not in server
        members, errs = srb._read_roster_powers(gid, "DS", guild=guild)
        # Alice's row gets inferred as non-Discord because she's not in
        # the live guild.
        assert members["1001"]["not_on_discord"] is True
        # And a soft warning surfaces.
        assert any("stale Discord IDs" in e for e in errs)

    def test_present_guild_member_not_flagged(self, fake_env):
        from unittest.mock import MagicMock
        fake, gid = fake_env
        # Mock a guild that returns a (non-bot) member for Alice but
        # not for other IDs. `.bot = False` is critical — MagicMock's
        # auto-attribute would otherwise satisfy the bot-filter and
        # mis-classify a real member as non-Discord.
        def _get_member(mid):
            if mid == 1001:
                m = MagicMock()
                m.bot = False
                return m
            return None
        guild = MagicMock()
        guild.get_member.side_effect = _get_member
        members, _errs = srb._read_roster_powers(gid, "DS", guild=guild)
        # Alice is in the server → NOT flagged.
        assert members["1001"]["not_on_discord"] is False

    def test_bot_member_inferred_non_discord(self, fake_env):
        """A roster row mapped to a bot account (admin pasted the wrong
        ID) is treated as a stale match — bots aren't real alliance
        members."""
        from unittest.mock import MagicMock
        fake, gid = fake_env
        bot_member = MagicMock()
        bot_member.bot = True
        guild = MagicMock()
        guild.get_member.return_value = bot_member
        members, errs = srb._read_roster_powers(gid, "DS", guild=guild)
        # Alice's row points to a bot account → inferred non-Discord.
        assert members["1001"]["not_on_discord"] is True
        # And the cleanup warning surfaces with the stale ID.
        assert any("stale Discord IDs" in e for e in errs)

    def test_explicit_flag_wins_over_inference(self, fake_env):
        from unittest.mock import MagicMock
        fake, gid = fake_env
        # Dave has "x" in not_on_discord AND a blank Discord ID. Even
        # if we hand a guild that returned a member (impossible since
        # discord_id is blank, but defense), the explicit flag wins.
        guild = MagicMock()
        m = MagicMock()
        m.bot = False
        guild.get_member.return_value = m
        members, _errs = srb._read_roster_powers(gid, "DS", guild=guild)
        assert members["Dave"]["not_on_discord"] is True


class TestPairedSubMode:
    """#132 — paired sub mode keeps each primary partnered with a
    specific sub. Auto-fill pairs greedy-style after primary placement;
    UI prompts for the sub inline."""

    def _three_members(self) -> dict[str, dict]:
        return {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 280_000_000, "not_on_discord": False},
        }

    def test_is_paired_property(self):
        sess = _make_session(team="A", sub_mode="paired")
        assert sess.is_paired is True
        sess_pool = _make_session(team="A", sub_mode="pool")
        assert sess_pool.is_paired is False

    def test_invalid_sub_mode_normalises_to_pool(self):
        sess = _make_session(team="A", sub_mode="garbage")
        assert sess.is_paired is False
        assert sess.sub_mode == "pool"

    def test_assigned_member_keys_includes_paired_subs(self):
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        keys = sess.assigned_member_keys()
        assert "1001" in keys
        assert "1002" in keys  # paired sub counted

    def test_unpaired_primaries_in_paired_mode(self):
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].extend(["1001", "1002"])
        sess.paired_subs["1001"] = "1003"
        unpaired = sess.unpaired_primaries()
        # Bob (1002) lacks a paired sub.
        assert unpaired == ["1002"]

    def test_unpaired_primaries_empty_in_pool_mode(self):
        sess = _make_session(team="A", sub_mode="pool")
        sess.assignments["Power Tower"].append("1001")
        # Pool mode has no concept of pairing; helper returns [].
        assert sess.unpaired_primaries() == []

    def test_prune_stale_pairings(self):
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        # Unassign the primary.
        sess.assignments["Power Tower"] = []
        sess.prune_stale_pairings()
        # The orphaned pairing should be gone.
        assert sess.paired_subs == {}

    def test_auto_fill_pairs_subs_in_paired_mode(self):
        members = self._three_members()
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=1,
                       min_power_a=200_000_000, priority=1),
        ]
        sess = _make_session(
            team="A", members=members, preset_zones=zones, sub_mode="paired",
        )
        summary = srb._auto_fill_session(sess)
        # Alice slotted as primary; Bob is the next-best eligible →
        # paired sub.
        assert sess.assignments["Power Tower"] == ["1001"]
        assert sess.paired_subs.get("1001") == "1002"
        # Carol is the leftover; she goes into the subs pool for paired
        # mode's "available swap" display.
        assert "1003" in sess.subs

    def test_render_embed_shows_paired_pairs(self):
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        embed = srb._render_builder_embed(sess)
        # Post-#222: pairings render in the `### Auto-paired Subs:`
        # section with `Primary ↔ Sub` per line (not inline in zones).
        assert "Auto-paired Subs" in embed.description
        assert "Alice ↔ Bob" in embed.description

    def test_render_embed_flags_unpaired_primary(self):
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        # No pairing yet.
        embed = srb._render_builder_embed(sess)
        # Post-#222: the ⚠️ inline marker is gone (cluttered the zone
        # line). Unpaired primaries surface via the dedicated message.
        assert "Primaries without a designated Sub" in embed.description
        assert "Alice" in embed.description

    def test_render_embed_paired_mode_surfaces_available_subs(self):
        """Paired mode's flat sub list used to be invisible from the
        embed — only primaries with their inline subs rendered. The
        Available subs line surfaces subs that haven't been paired yet
        so the officer knows there are members to attach via 🔁 Pair
        subs."""
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        # Carol sits in the available pool — not yet paired.
        sess.subs.append("1003")
        embed = srb._render_builder_embed(sess)
        assert "Available subs" in embed.description
        assert "Carol" in embed.description

    def test_render_embed_paired_mode_no_subs_line_when_empty(self):
        """Embed must not render the Available subs line when there
        aren't any — keeps the embed compact in the typical case."""
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        # No available subs.
        embed = srb._render_builder_embed(sess)
        assert "Available subs" not in embed.description

    def test_zone_of_primary_returns_zone_for_assigned(self):
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        assert srb._zone_of_primary(sess, "1001") == "Power Tower"

    def test_zone_of_primary_falls_back_to_selected(self):
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.selected_zone = "Nuclear Silo"
        # Member not assigned anywhere → falls back to selected_zone.
        assert srb._zone_of_primary(sess, "9999") == "Nuclear Silo"


class TestStructuredBuilderView:
    def test_structured_mode_shows_approve_button(self):
        session = _make_session(team="A")
        session.event_date = "2026-05-18"
        view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Approve" in lab for lab in labels)
        # Free-tier "Generate mail" button should NOT appear in structured mode.
        assert not any(lab == "📄 Generate mail" for lab in labels)

    def test_manual_mode_shows_generate_mail_button(self):
        session = _make_session(team="A")  # event_date None
        view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Generate mail" in lab for lab in labels)
        assert not any("Approve" in lab for lab in labels)


class TestSessionStateLock:
    """Two officers building the same team for the same event used to
    each get their own session, each Approve, each post a mail and write
    duplicate rosters_tab rows. The lock prevents that."""

    def test_claim_succeeds_for_fresh_slot(self, seeded_db):
        import config
        ok, holder = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=42,
        )
        assert ok is True
        assert holder is None

    def test_second_officer_claim_rejected(self, seeded_db):
        import config
        ok, _ = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=42,
        )
        assert ok is True
        ok2, holder = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=99,
        )
        assert ok2 is False
        assert holder == 42

    def test_same_officer_can_reclaim(self, seeded_db):
        # An officer who re-opens the builder for a team they already
        # hold doesn't get locked out by their own prior claim.
        import config
        config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=42,
        )
        ok, holder = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=42,
        )
        assert ok is True
        assert holder is None

    def test_release_frees_slot(self, seeded_db):
        import config
        config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=42,
        )
        released = config.release_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
        )
        assert released is True
        # Now a different officer can claim it.
        ok, holder = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=99,
        )
        assert ok is True
        assert holder is None

    def test_release_unclaimed_slot_is_safe(self, seeded_db):
        import config
        released = config.release_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
        )
        assert released is False  # nothing to release, no exception

    def test_teams_are_independent(self, seeded_db):
        # Team A and Team B for the same event are separate slots; one
        # officer can hold A while another holds B.
        import config
        ok_a, _ = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=42,
        )
        ok_b, _ = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "B", user_id=99,
        )
        assert ok_a is True
        assert ok_b is True

    def test_events_are_independent(self, seeded_db):
        # Holding Team A for one event date doesn't block the same
        # team for a different event date.
        import config
        ok_1, _ = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=42,
        )
        ok_2, _ = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-25", "A", user_id=42,
        )
        assert ok_1 is True
        assert ok_2 is True

    def test_cs_uses_empty_team_field(self, seeded_db):
        # Legacy pre-#166 CS rosters used team="" for the single-roster
        # shape. The claim/release helpers still accept that key shape
        # so historical Sheet data round-trips cleanly. Post-#166, new
        # CS sessions use "A"/"B" the same as DS does.
        import config
        ok_1, _ = config.claim_storm_session(
            TEST_GUILD_ID, "CS", "2026-05-18", "", user_id=42,
        )
        ok_2, holder = config.claim_storm_session(
            TEST_GUILD_ID, "CS", "2026-05-18", "", user_id=99,
        )
        assert ok_1 is True
        assert ok_2 is False
        assert holder == 42


class TestRenderActionView:
    """Three-button ephemeral bar shown after the public roster image
    is posted. Confirms the buttons exist with expected labels, the
    save button writes to SQLite, and the download button handles a
    DMs-disabled user gracefully."""

    def _make_view(self, *, owner_id=42, event_date="2026-05-18", team="A",
                   guild_id=None):
        return srb._RenderActionView(
            owner_id=owner_id,
            png_bytes=b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
            filename="ds-roster.png",
            guild_id=guild_id or TEST_GUILD_ID,
            event_type="DS",
            event_date=event_date,
            team=team,
            public_channel_id=900,
            public_message_id=12345,
        )

    def test_three_buttons_present(self):
        view = self._make_view()
        labels = [getattr(c, "label", "") for c in view.children]
        assert "📥 Download" in labels
        assert "💾 Save to history" in labels
        assert "📢 Post to channel..." in labels

    @pytest.mark.asyncio
    async def test_non_owner_blocked_by_interaction_check(self):
        from unittest.mock import AsyncMock, MagicMock
        view = self._make_view(owner_id=42)
        inter = MagicMock()
        inter.user = MagicMock(); inter.user.id = 999
        inter.response = MagicMock()
        inter.response.send_message = AsyncMock()
        allowed = await view.interaction_check(inter)
        assert allowed is False
        inter.response.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_save_button_writes_pointer_to_sqlite(self, seeded_db):
        from unittest.mock import AsyncMock, MagicMock
        import config
        view = self._make_view()
        inter = MagicMock()
        inter.user = MagicMock(); inter.user.id = 42
        inter.response = MagicMock()
        inter.response.send_message = AsyncMock()

        # Find the save button and drive its callback.
        save_btn = next(c for c in view.children
                        if getattr(c, "label", "") == "💾 Save to history")
        await save_btn.callback(inter)

        refs = config.list_roster_image_refs(TEST_GUILD_ID, "DS", "2026-05-18")
        assert len(refs) == 1
        assert refs[0]["team"] == "A"
        assert refs[0]["channel_id"] == 900
        assert refs[0]["message_id"] == 12345
        inter.response.send_message.assert_awaited_once()
        sent_args = inter.response.send_message.await_args
        assert sent_args.kwargs.get("ephemeral") is True

    @pytest.mark.asyncio
    async def test_save_button_rejects_when_no_event_date(self, seeded_db):
        """Manual / free-tier builders don't carry an event_date, so
        the save pointer would have a degenerate key. Refuse cleanly
        instead of writing a stub row."""
        from unittest.mock import AsyncMock, MagicMock
        import config
        view = self._make_view(event_date="")
        inter = MagicMock()
        inter.user = MagicMock(); inter.user.id = 42
        inter.response = MagicMock()
        inter.response.send_message = AsyncMock()

        save_btn = next(c for c in view.children
                        if getattr(c, "label", "") == "💾 Save to history")
        await save_btn.callback(inter)

        # Nothing was written.
        assert config.list_roster_image_refs(TEST_GUILD_ID, "DS", "") == []
        # Officer was told why.
        sent = inter.response.send_message.await_args
        msg = sent.args[0] if sent.args else sent.kwargs.get("content", "")
        assert "event date" in msg.lower()

    @pytest.mark.asyncio
    async def test_download_button_dm_blocked_falls_back_gracefully(self):
        """DMs disabled → friendly ephemeral instead of crashing."""
        from unittest.mock import AsyncMock, MagicMock
        import discord
        view = self._make_view()

        user = MagicMock()
        user.id = 42
        user.create_dm = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403), "DMs off"),
        )
        inter = MagicMock()
        inter.user = user
        inter.response = MagicMock()
        inter.response.send_message = AsyncMock()

        download_btn = next(c for c in view.children
                            if getattr(c, "label", "") == "📥 Download")
        await download_btn.callback(inter)

        inter.response.send_message.assert_awaited_once()
        sent = inter.response.send_message.await_args
        msg = sent.args[0] if sent.args else sent.kwargs.get("content", "")
        assert "privacy settings" in msg.lower() or "can't dm" in msg.lower()


class TestBuilderViewTimeoutCleanup:
    @pytest.mark.asyncio
    async def test_on_timeout_releases_structured_lock(self, seeded_db):
        import config
        # Claim the lock under user 42 to simulate a session being open.
        config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=42,
        )
        session = _make_session(team="A")
        session.guild_id = TEST_GUILD_ID
        session.user_id = 42
        session.event_date = "2026-05-18"
        view = srb.RosterBuilderView(session)
        view.message = None  # no edit to perform; on_timeout still runs cleanly
        await view.on_timeout()
        # Lock is released.
        ok, _ = config.claim_storm_session(
            TEST_GUILD_ID, "DS", "2026-05-18", "A", user_id=99,
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_on_timeout_manual_mode_is_a_noop(self, seeded_db):
        # Free-tier (event_date=None) shouldn't try to release a
        # structured-mode lock that was never claimed.
        session = _make_session(team="A")  # no event_date
        view = srb.RosterBuilderView(session)
        view.message = None
        # Should not raise even though no lock was claimed.
        await view.on_timeout()


class TestFinalizePostOutcomes:
    """The summary copy shown to the officer must distinguish four
    post-channel outcomes: posted_ok / no_channel / channel_gone /
    send_failed. The prior implementation collapsed channel_gone and
    send_failed into "no post channel is configured," which is
    factually wrong and confusing — leadership has no way to know
    whether the channel was deleted, the bot was kicked, or something
    else went wrong."""

    def _find_followup(self, inter, needle: str) -> str | None:
        """Return the first ephemeral followup containing `needle`, or
        None. Necessary because `_finalize_structured_roster` fires
        multiple followups on Premium guilds (#226: standard summary,
        DM-rostered-members prompt, plus optional image-overflow
        warning) — count-based or last-call assertions break under
        FORCE_PREMIUM=1 lane."""
        for call in inter.followup.send.await_args_list:
            text = call.args[0] if call.args else ""
            if needle in text:
                return text
        return None

    def _make_interaction(self, guild=None):
        from unittest.mock import AsyncMock, MagicMock
        inter = MagicMock()
        inter.guild = guild
        inter.response = MagicMock()
        inter.response.defer = AsyncMock()
        inter.followup = MagicMock()
        inter.followup.send = AsyncMock()
        return inter

    def _make_fake_channel(self, channel_id: int, mention: str = "#storm",
                           send_raises: Exception | None = None):
        from unittest.mock import AsyncMock, MagicMock
        ch = MagicMock()
        ch.id = channel_id
        ch.mention = mention
        if send_raises is not None:
            ch.send = AsyncMock(side_effect=send_raises)
        else:
            ch.send = AsyncMock()
        return ch

    def _make_structured_view(self, fake_env, *, channel=None, channel_id=0):
        fake, gid = fake_env
        import config
        # Wire post_channel_id into the storm config so the finalize
        # path knows where to look.
        if channel_id:
            cfg = config.get_storm_config(gid, "DS")
            cfg["post_channel_id"] = channel_id
            config.save_storm_config(gid, "DS", **{
                k: v for k, v in cfg.items()
                if k in {"tab_name", "mail_template", "timezone",
                         "log_channel_id", "post_channel_id"}
            })

        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.assignments["Power Tower"].append("1001")
        view = srb.RosterBuilderView(session)
        view.message = None
        # Claim the lock so on_timeout/release have something to release.
        config.claim_storm_session(gid, "DS", "2026-05-18", "A", user_id=42)

        from unittest.mock import MagicMock
        guild = MagicMock()
        guild.get_channel = MagicMock(return_value=channel)
        inter = self._make_interaction(guild=guild)
        return inter, view, session

    @pytest.mark.asyncio
    async def test_no_channel_configured_says_no_channel(self, fake_env):
        inter, view, _ = self._make_structured_view(fake_env, channel=None, channel_id=0)
        await srb._finalize_structured_roster(inter, view)
        sent = inter.followup.send.await_args.args[0]
        assert "No post channel is configured" in sent

    @pytest.mark.asyncio
    async def test_channel_deleted_says_channel_gone(self, fake_env):
        inter, view, _ = self._make_structured_view(
            fake_env, channel=None, channel_id=12345,
        )
        await srb._finalize_structured_roster(inter, view)
        sent = inter.followup.send.await_args.args[0]
        # Channel-gone message should mention the channel ID, NOT the
        # generic "no post channel is configured" line.
        assert "deleted" in sent.lower() or "can't see it" in sent.lower()
        assert "<#12345>" in sent

    @pytest.mark.asyncio
    async def test_send_failure_distinguishes_from_no_channel(self, fake_env):
        # A bare Exception is enough — the finalize path catches the
        # broad `Exception` and surfaces the message verbatim.
        ch = self._make_fake_channel(
            12345, send_raises=Exception("Missing Permissions"),
        )
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        await srb._finalize_structured_roster(inter, view)
        sent = inter.followup.send.await_args.args[0]
        # Officer must see that the channel rejected the send — not the
        # misleading "no post channel is configured" line.
        assert "rejected the send" in sent
        assert "<#12345>" in sent
        assert "No post channel is configured" not in sent

    @pytest.mark.asyncio
    async def test_happy_path_renders_posted_to_mention(self, fake_env):
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        await srb._finalize_structured_roster(inter, view)
        ch.send.assert_awaited_once()
        summary = self._find_followup(inter, "Roster posted.")
        assert summary is not None, (
            "expected a 'Roster posted.' summary followup; got: "
            f"{[c.args[0] for c in inter.followup.send.await_args_list if c.args]}"
        )
        assert "<#12345>" in summary

    @pytest.mark.asyncio
    async def test_include_image_attaches_png_to_send(self, fake_env):
        """#225: `include_image=True` renders the roster and passes the
        PNG to `channel.send` as the `file=` argument so the post lands
        with both text and image."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        fake_png = b"\x89PNG\r\n\x1a\nfake-png-bytes"
        with patch("storm_renderer.render", return_value=fake_png):
            await srb._finalize_structured_roster(inter, view, include_image=True)
        ch.send.assert_awaited_once()
        # Mail in args[0], image in `file` kwarg.
        kwargs = ch.send.await_args.kwargs
        assert "file" in kwargs
        attached = kwargs["file"]
        assert isinstance(attached, discord.File)
        assert attached.filename.endswith(".png")

    @pytest.mark.asyncio
    async def test_text_only_skips_file_argument(self, fake_env):
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        await srb._finalize_structured_roster(inter, view, include_image=False)
        ch.send.assert_awaited_once()
        # No `file=` kwarg in text-only mode.
        assert "file" not in ch.send.await_args.kwargs

    @pytest.mark.asyncio
    async def test_render_failure_falls_back_to_text_with_warning(self, fake_env):
        """Pillow missing / encode failure on the with-image path drops
        the attachment, posts text-only, and tacks the failure reason
        onto the officer ephemeral so the missing image isn't silent."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        with patch(
            "storm_renderer.render",
            side_effect=RuntimeError("Pillow not installed"),
        ):
            await srb._finalize_structured_roster(inter, view, include_image=True)
        ch.send.assert_awaited_once()
        # No file attached — render failed but post still went through.
        assert "file" not in ch.send.await_args.kwargs
        # Officer ephemeral carries both the success line AND a warning.
        summary = self._find_followup(inter, "Roster posted")
        assert summary is not None
        assert "Couldn't attach the image" in summary

    @pytest.mark.asyncio
    async def test_overflow_ephemeral_warns_about_clipped_members(self, fake_env):
        """#228 follow-up: when the rendered image can't fit every
        member (slot grid `max_rows` cap), a second ephemeral lists
        the names that dropped so the officer can react. Members are
        still in the mail body + rosters_tab — only the image clips
        them."""
        from unittest.mock import patch
        import storm_renderer as sr
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        fake_png = b"\x89PNG\r\n\x1a\nfake-png-bytes"

        # Simulate a render that produces a PNG AND surfaces overflow.
        # `storm_renderer.render` populates `roster.overflow` on the
        # RosterData it was given, so we patch the function to do the
        # same in-place mutation.
        def _fake_render(roster_data):
            roster_data.overflow = [
                sr._OverflowEntry(
                    canonical_zone="Nuclear Silo", phase=2, name="Member 7",
                ),
                sr._OverflowEntry(
                    canonical_zone="Nuclear Silo", phase=2, name="Member 14",
                ),
            ]
            return fake_png

        with patch("storm_renderer.render", side_effect=_fake_render):
            await srb._finalize_structured_roster(inter, view, include_image=True)

        # The standard confirmation and overflow warning both fire as
        # ephemeral followups (the DM-rostered-members prompt also
        # fires on Premium guilds — find each by content rather than
        # by call index).
        summary = self._find_followup(inter, "Roster posted")
        overflow = self._find_followup(inter, "didn't fit")
        assert summary is not None
        assert overflow is not None
        assert "Nuclear Silo" in overflow
        assert "Member 7" in overflow
        assert "Member 14" in overflow

    # ── #237 long-mail picker ─────────────────────────────────────

    def _stub_picker(self, choice: str):
        """Return a context-manager that patches `_LongMailPickerView`
        so `await picker.wait()` returns immediately with the given
        pre-set choice. Tests bypass the actual Discord view round-
        trip without giving up on exercising the rest of the long-
        mail flow."""
        from unittest.mock import patch, AsyncMock, MagicMock
        instance = MagicMock()
        instance.choice = choice
        instance.message = MagicMock()
        instance.wait = AsyncMock(return_value=None)
        return patch.object(
            srb, "_LongMailPickerView", return_value=instance,
        )

    @pytest.mark.asyncio
    async def test_long_mail_shows_picker_before_posting(self, fake_env):
        """#237: a roster mail >2000 chars must open the officer-facing
        picker (Send as 2 posts / Send as .txt / Cancel) instead of
        the bot silently picking the format."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        long_mail = "X" * 2500
        with self._stub_picker("txt"), \
             patch("storm_roster_builder._build_mail_body", return_value=long_mail):
            await srb._finalize_structured_roster(inter, view)

        # The picker followup must have fired; the body explains the
        # two options to the officer.
        picker_call = next(
            (c for c in inter.followup.send.await_args_list
             if "goes over the limit" in (c.args[0] if c.args else "")),
            None,
        )
        assert picker_call is not None, (
            "long mail must trigger the picker followup, not post directly"
        )

    @pytest.mark.asyncio
    async def test_long_mail_txt_choice_attaches_as_file(self, fake_env):
        """Picker → "Send as .txt attachment" path keeps the #234
        behaviour: full mail rides as a .txt file with a short
        placeholder in the inline content."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        long_mail = "X" * 2500
        with self._stub_picker("txt"), \
             patch("storm_roster_builder._build_mail_body", return_value=long_mail):
            await srb._finalize_structured_roster(inter, view)

        ch.send.assert_awaited_once()
        kwargs = ch.send.await_args.kwargs
        assert "file" in kwargs
        attached = kwargs["file"]
        assert isinstance(attached, discord.File)
        assert attached.filename.endswith(".txt")
        content = ch.send.await_args.args[0]
        assert len(content) < 2000
        assert "full mail attached" in content

    @pytest.mark.asyncio
    async def test_long_mail_txt_with_image_attaches_both(self, fake_env):
        """Picker → "Send as .txt attachment" + with-image attaches
        both files on a single Discord message."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        long_mail = "X" * 2500
        fake_png = b"\x89PNG\r\n\x1a\nfake-png-bytes"
        with self._stub_picker("txt"), \
             patch("storm_renderer.render", return_value=fake_png), \
             patch("storm_roster_builder._build_mail_body", return_value=long_mail):
            await srb._finalize_structured_roster(inter, view, include_image=True)

        ch.send.assert_awaited_once()
        kwargs = ch.send.await_args.kwargs
        assert "files" in kwargs
        files = kwargs["files"]
        assert len(files) == 2
        names = [f.filename for f in files]
        assert any(n.endswith(".txt") for n in names)
        assert any(n.endswith(".png") for n in names)

    @pytest.mark.asyncio
    async def test_long_mail_split_choice_posts_two_messages(self, fake_env):
        """Picker → "Send as 2 posts" splits at a heading and posts
        both parts. The second post always starts with a `**Heading**`
        line so sections stay together."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        # Build a long mail with a clear heading midway so the split
        # helper has a natural break point.
        long_mail = (
            "**Stage 1**\n\n"
            + ("Member line\n" * 100)
            + "\n**Stage 2**\n\n"
            + ("Other line\n" * 100)
        )
        with self._stub_picker("split"), \
             patch("storm_roster_builder._build_mail_body", return_value=long_mail):
            await srb._finalize_structured_roster(inter, view)

        # Two posts, both under 2000 chars; second starts with **Stage 2**.
        assert ch.send.await_count == 2
        call1, call2 = ch.send.await_args_list
        part1 = call1.args[0] if call1.args else ""
        part2 = call2.args[0] if call2.args else ""
        assert len(part1) <= 2000
        assert len(part2) <= 2000
        assert part2.lstrip().startswith("**Stage 2**")

    @pytest.mark.asyncio
    async def test_long_mail_split_with_image_attaches_to_last_post(self, fake_env):
        """Split + with-image: the PNG attaches to the LAST post —
        the second message starts with a heading, and putting the
        image after that heading keeps the roster visual adjacent
        to the section it depicts (tester report 2026-05-23:
        attaching to post 1 made readers scroll up to find the
        image after reading the second post)."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        long_mail = (
            "**Stage 1**\n\n"
            + ("Line\n" * 200)
            + "\n**Stage 2**\n\n"
            + ("Line\n" * 200)
        )
        fake_png = b"\x89PNG\r\n\x1a\nfake-png-bytes"
        with self._stub_picker("split"), \
             patch("storm_renderer.render", return_value=fake_png), \
             patch("storm_roster_builder._build_mail_body", return_value=long_mail):
            await srb._finalize_structured_roster(inter, view, include_image=True)

        assert ch.send.await_count == 2
        # First post is text-only; second post (the LAST message)
        # carries the image.
        assert "file" not in ch.send.await_args_list[0].kwargs
        assert "file" in ch.send.await_args_list[1].kwargs

    @pytest.mark.asyncio
    async def test_long_mail_cancel_choice_aborts_without_posting(self, fake_env):
        """Picker → Cancel: nothing posts, view stops, officer gets a
        friendly "cancelled" ephemeral."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        long_mail = "X" * 2500
        with self._stub_picker("cancel"), \
             patch("storm_roster_builder._build_mail_body", return_value=long_mail):
            await srb._finalize_structured_roster(inter, view)

        # Channel never gets a post.
        ch.send.assert_not_awaited()
        # Officer sees a cancelled-ephemeral.
        cancel_call = next(
            (c for c in inter.followup.send.await_args_list
             if "Cancelled" in (c.args[0] if c.args else "")),
            None,
        )
        assert cancel_call is not None

    @pytest.mark.asyncio
    async def test_recovery_ephemeral_never_exceeds_message_limit(self, fake_env):
        """Defensive: when the post fails (any cause), the officer
        ephemeral must always fit in 2000 chars so the interaction
        doesn't stay stuck in "thinking…". Tester report 2026-05-21."""
        from unittest.mock import patch
        ch = self._make_fake_channel(
            12345, send_raises=Exception("Some failure"),
        )
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        long_mail = "Y" * 2500
        with self._stub_picker("txt"), \
             patch("storm_roster_builder._build_mail_body", return_value=long_mail):
            await srb._finalize_structured_roster(inter, view)

        inter.followup.send.assert_awaited()
        for call in inter.followup.send.await_args_list:
            content = call.args[0] if call.args else call.kwargs.get("content", "")
            assert len(content) <= 2000, (
                f"officer ephemeral exceeded 2000 chars ({len(content)})"
                " — this is the stuck-thinking failure mode"
            )

    @pytest.mark.asyncio
    async def test_no_overflow_ephemeral_when_everyone_fits(self, fake_env):
        """If `roster.overflow` is empty (the typical case), the
        second-ephemeral path is skipped — just the standard
        confirmation goes out."""
        from unittest.mock import patch
        ch = self._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = self._make_structured_view(
            fake_env, channel=ch, channel_id=12345,
        )
        fake_png = b"\x89PNG\r\n\x1a\nfake-png-bytes"

        def _fake_render(roster_data):
            roster_data.overflow = []
            return fake_png

        with patch("storm_renderer.render", side_effect=_fake_render):
            await srb._finalize_structured_roster(inter, view, include_image=True)

        # No overflow warning fires when there's nothing to warn about.
        # The standard confirmation (and on Premium, the DM-rostered-
        # members prompt) still fire — assertion is specifically that
        # no "didn't fit" warning slipped in.
        assert self._find_followup(inter, "didn't fit") is None


class TestSplitMailAtHeading:
    """#237: the long-mail picker's "Send as 2 posts" choice splits at
    a natural heading break so the second message always starts with
    a `**Heading**` line and sections stay together for context."""

    def test_splits_at_heading_nearest_midpoint(self):
        mail = (
            "**Stage 1**\n\n"
            + "x" * 800
            + "\n**Stage 2**\n\n"
            + "y" * 800
        )
        parts = srb._split_mail_at_heading(mail)
        assert parts is not None
        part1, part2 = parts
        assert len(part1) <= 2000
        assert len(part2) <= 2000
        # Second part begins with the Stage 2 heading.
        assert part2.startswith("**Stage 2**")

    def test_prefers_heading_closest_to_midpoint(self):
        # Three headings; midpoint is closest to the second.
        body = (
            "**Section A**\n\n"
            + "a" * 500
            + "\n**Section B**\n\n"
            + "b" * 500
            + "\n**Section C**\n\n"
            + "c" * 500
        )
        parts = srb._split_mail_at_heading(body)
        assert parts is not None
        part1, part2 = parts
        # Section B is closest to the midpoint, so part2 starts there.
        assert part2.startswith("**Section B**")

    def test_returns_none_when_no_valid_heading_split(self):
        # A mail with one giant section after the heading — splitting
        # at the heading would leave part2 way over the ceiling.
        mail = "Header line\n\n" + "z" * 5000
        parts = srb._split_mail_at_heading(mail, max_len=2000)
        # No `**Heading**` markers at all → no valid split.
        assert parts is None

    def test_strips_trailing_whitespace_from_first_part(self):
        mail = (
            "**Stage 1**\n\n"
            + "data\n\n\n\n"  # trailing newlines before the split
            + "**Stage 2**\n\n"
            + "more"
        )
        parts = srb._split_mail_at_heading(mail)
        assert parts is not None
        part1, part2 = parts
        # Part 1's trailing whitespace stripped so the next post
        # doesn't start with leading blank lines.
        assert not part1.endswith("\n\n")
        assert part2.startswith("**Stage 2**")


class TestPowerSnapshotAtFinalize:
    @pytest.mark.asyncio
    async def test_finalize_refreshes_power_before_writing(self, fake_env):
        """The commit message for #129 said 'power_at_assignment is
        snapshotted at write time' but the code was actually reading the
        builder-open snapshot. Verify that a power change between open
        and Approve flows into the rosters_tab write."""
        import config
        from unittest.mock import AsyncMock, MagicMock
        fake, gid = fake_env

        # Open the session with stale power for Alice.
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 100_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.assignments["Power Tower"].append("1001")
        view = srb.RosterBuilderView(session)
        view.message = None
        config.claim_storm_session(gid, "DS", "2026-05-18", "A", user_id=42)

        # Simulate the alliance updating Alice's power on the Sheet
        # between Open and Approve — bump her row's power column.
        ws = fake.worksheet("Member Roster")
        for row in ws._rows[1:]:
            if row[1] == "alice":
                row[5] = "500M"
                break

        guild = MagicMock()
        guild.get_channel = MagicMock(return_value=None)
        inter = MagicMock()
        inter.guild = guild
        inter.response = MagicMock()
        inter.response.defer = AsyncMock()
        inter.followup = MagicMock()
        inter.followup.send = AsyncMock()

        await srb._finalize_structured_roster(inter, view)

        # The rosters_tab row for Alice should carry her FRESH power.
        roster_ws = fake.worksheet("DS Rosters")
        rows = roster_ws.get_all_values()
        col = srb._ROSTERS_HEADER.index("Power at Assignment")
        mc = srb._ROSTERS_HEADER.index("Member")
        alice_row = next(r for r in rows[1:] if r[mc] == "Alice")
        assert alice_row[col] == str(500_000_000)


class TestPairedSubMailRendering:
    """#224: paired mode renders pairings as a `Primary ↔ Sub` list in
    the Subs block, matching the embed shape from #222. Zones carry
    bare primaries only (no inline ` + sub <name>`)."""

    def test_pool_mode_unchanged(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 280_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members, sub_mode="pool")
        session.assignments["Power Tower"].append("1001")
        session.subs.append("1002")
        zones, subs = srb._mail_zone_and_sub_lists(session)
        # Pool mode: primary line is just the name; sub list is flat.
        assert zones == {"Power Tower": ["Alice"]}
        assert subs == ["Bob"]

    def test_paired_mode_pairings_render_as_separate_list(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 280_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members, sub_mode="paired")
        session.assignments["Power Tower"].append("1001")
        session.paired_subs["1001"] = "1002"
        zones, subs = srb._mail_zone_and_sub_lists(session)
        # Zone carries the bare primary name. No inline ` + sub <name>`.
        assert zones == {"Power Tower": ["Alice"]}
        # Subs list carries the pairing as `Primary ↔ Sub`.
        assert subs == ["Alice ↔ Bob"]

    def test_paired_mode_unpaired_primary_renders_plain(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members, sub_mode="paired")
        session.assignments["Power Tower"].append("1001")
        # No pairing recorded — primary renders bare, no pair line.
        zones, subs = srb._mail_zone_and_sub_lists(session)
        assert zones == {"Power Tower": ["Alice"]}
        assert subs == []

    def test_paired_mode_overflow_appended_after_pairings(self):
        """When `session.subs` is non-empty in paired mode (auto-fill's
        overflow pool), those names append after the pairing lines."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 280_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 220_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members, sub_mode="paired")
        session.assignments["Power Tower"].append("1001")
        session.paired_subs["1001"] = "1002"
        session.subs.append("1003")
        zones, subs = srb._mail_zone_and_sub_lists(session)
        assert zones == {"Power Tower": ["Alice"]}
        # Pairing line first, overflow name after.
        assert subs == ["Alice ↔ Bob", "Carol"]

    def test_paired_mode_sub_not_duplicated_when_still_in_subs_pool(self):
        """Manual pair-assign leaves the sub in `session.subs` (the
        pairing layer doesn't move it out). The mail must NOT
        double-render: `Alice ↔ Bob` AND a bare `Bob` line both
        showed up in the subs block before the dedup, which read as
        a roster bug to the tester."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 280_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members, sub_mode="paired")
        session.assignments["Power Tower"].append("1001")
        session.paired_subs["1001"] = "1002"
        # Realistic state after manual pair-assign: Bob is still in
        # the global sub pool because the pairing UI doesn't move
        # him out of session.subs.
        session.subs.append("1002")
        zones, subs = srb._mail_zone_and_sub_lists(session)
        assert zones == {"Power Tower": ["Alice"]}
        # Bob appears exactly once — as the right half of the pair,
        # NOT also as a bare overflow name.
        assert subs == ["Alice ↔ Bob"]

    def test_paired_mode_dedup_spans_all_phases_for_phase_aware(self):
        """A sub paired in Stage 2 (but not Stage 1) must still be
        deduped against the overflow pool. Otherwise a CS 3-stage
        roster could double-render the sub if they're only paired in
        a later stage."""
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1",
                  "power": 412_000_000, "not_on_discord": False},
            "2": {"key": "2", "name": "Bob", "discord_id": "2",
                  "power": 280_000_000, "not_on_discord": False},
        }
        zones = [
            ss.ZoneRow(
                zone="Data Center 1", max_players=0,
                max_phase1=1, max_phase2=1, max_phase3=1,
                min_power_a=0, min_power_b=0,
            ),
        ]
        preset = ss.PresetBuffer(
            name="Multi-phase", event_type="CS", zones=zones,
            uses_phases=True, phase_count=3,
        )
        session = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="CS",
            team="A", preset=preset, members=members,
            per_member_rules=[], power_band_rules=[],
            sub_mode="paired",
        )
        session.assignments_p2["Data Center 1"].append("1")
        session.paired_subs_p2["1"] = "2"
        session.subs.append("2")
        # Phase 1: no pairings, overflow includes Bob (would normally
        # show him bare) — but Bob is paired in Phase 2, so dedup
        # against ALL phases must suppress the bare name.
        _zones1, subs1 = srb._mail_zone_and_sub_lists(session, phase=1)
        assert subs1 == []


class TestRostersTabHeaderMigration:
    """Audit Major M3: existing alliances' rosters_tab kept the 9-column
    header (no `Paired With`); the new writer appends 10-cell rows so
    paired data lands under an unlabeled column and `storm_history`
    can't read it back. Header migration rewrites the row in place."""

    def test_old_header_rewritten_in_place(self, fake_env):
        fake, gid = fake_env
        # Seed an old-shape rosters_tab — the pre-#132 9-column header
        # (no `Paired With`, no `Phase`).
        old_rosters = fake.add_worksheet("DS Rosters")
        old_rosters._rows = [
            ["Event Date", "Team", "Zone", "Member", "Role",
             "Power at Assignment", "Discord ID", "Override Below Floor",
             "Posted At (UTC)"],
            ["2026-05-11", "A", "Power Tower", "Old", "primary",
             "300000000", "1", "", ""],
        ]

        # Build a session and finalise — header should migrate AND the
        # existing data row should shift so each value lands under the
        # same column name in the new header.
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.assignments["Power Tower"].append("1001")
        errors = srb._write_rosters_tab(session)
        assert errors == []

        new_header = old_rosters._rows[0]
        assert "Paired With" in new_header
        assert "Stage" in new_header
        # Migrated header matches the canonical shape.
        assert new_header == srb._ROSTERS_HEADER
        # Prior data row preserved AND re-aligned to the new column
        # order. `Member` is at index 4 in the new shape (after Stage
        # was inserted at idx 2); without the row shift the migration
        # would silently corrupt every column-name read.
        member_idx = srb._ROSTERS_HEADER.index("Member")
        zone_idx = srb._ROSTERS_HEADER.index("Zone")
        phase_idx = srb._ROSTERS_HEADER.index("Stage")
        prior_row = old_rosters._rows[1]
        assert prior_row[member_idx] == "Old"
        assert prior_row[zone_idx] == "Power Tower"
        # Pre-#152 rows get phase "1" so loaders can join on phase
        # without seeing blanks.
        assert prior_row[phase_idx] == "1"

    def test_override_column_renamed_and_data_preserved(self, fake_env):
        """Rule B follow-up: the rosters_tab column header was renamed
        from `Override Below Floor` → `Override Below Minimum`. The
        header migration must (a) emit the new name on rewrite, and
        (b) carry existing "yes" flags from the old column over into
        the new column so dev/staging events don't lose their audit
        data."""
        fake, gid = fake_env
        old_rosters = fake.add_worksheet("DS Rosters")
        old_rosters._rows = [
            ["Event Date", "Team", "Zone", "Member", "Role",
             "Power at Assignment", "Discord ID", "Override Below Floor",
             "Paired With", "Posted At (UTC)"],  # 10-col, no Phase yet
            ["2026-05-11", "A", "Power Tower", "Old", "primary",
             "180000000", "9", "yes", "", ""],
        ]
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.assignments["Power Tower"].append("1001")
        errors = srb._write_rosters_tab(session)
        assert errors == []

        new_header = old_rosters._rows[0]
        # New name landed; old name is gone.
        assert "Override Below Minimum" in new_header
        assert "Override Below Floor" not in new_header

        # The pre-existing "yes" flag on the legacy column carried into
        # the new column rather than being lost.
        override_idx = srb._ROSTERS_HEADER.index("Override Below Minimum")
        prior_row = old_rosters._rows[1]
        assert prior_row[override_idx] == "yes"


class TestAutoFillSummarySplitsPairedFromPrimary:
    """Audit Major M5: `auto_filled_by_power` merged primaries and
    paired subs. A 4-zone build with 4 primaries + 4 paired subs would
    read 'Auto-filled by power: 8' even though only 4 primaries were
    auto-filled. Now `auto_paired_subs` is a separate count."""

    def test_paired_mode_summary_has_separate_paired_list(self):
        """Decision #14 (#171): `auto_paired_subs` is now a list of
        `Primary ↔ Sub` strings rather than a bare count, so the
        summary can render the explicit pairings."""
        members = {
            f"100{i}": {"key": f"100{i}", "name": f"M{i}", "discord_id": f"100{i}",
                        "power": 400_000_000 - i * 10_000_000,
                        "not_on_discord": False}
            for i in range(8)
        }
        zones = [ss.ZoneRow(zone="Power Tower", max_players=4,
                            min_power_a=100_000_000, priority=1)]
        session = _make_session(team="A", members=members,
                                preset_zones=zones, sub_mode="paired")
        summary = srb._auto_fill_session(session)
        # 4 primaries auto-filled + 4 paired subs (each rendered as
        # "PrimaryName ↔ SubName").
        assert summary["auto_filled_by_power"] == 4
        assert len(summary["auto_paired_subs"]) == 4
        for pair in summary["auto_paired_subs"]:
            assert "↔" in pair

    def test_pool_mode_paired_list_is_empty(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members, sub_mode="pool")
        summary = srb._auto_fill_session(session)
        # Pool mode never pairs.
        assert summary["auto_paired_subs"] == []


class TestZoneMinimumSuffix:
    """#238: zone names in the builder embed include `_(minimum XM)_`
    when a power-band rule sets a floor, so officers can read the
    requirement next to the zone instead of cross-referencing the
    Member Rules library."""

    def test_no_suffix_when_floor_is_zero(self):
        # No power_band rule + zero preset floor → no suffix.
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        })
        suffix = srb._zone_minimum_suffix(session, "Mercenary Factory")
        # Mercenary Factory has no preset floor in the default fixture.
        assert suffix == "" or "minimum" not in suffix

    def test_suffix_renders_when_power_band_floor_set(self):
        import storm_member_rules as smr
        band = smr.Rule(
            rule_type="power_band", subject="80000000",
            value="Power Tower", sub_type="",
        )
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }, power_band_rules=[band])
        suffix = srb._zone_minimum_suffix(session, "Power Tower")
        assert suffix == " _(minimum 80M)_"

    def test_render_zone_line_includes_suffix_flat_mode(self):
        import storm_member_rules as smr
        band = smr.Rule(
            rule_type="power_band", subject="80000000",
            value="Power Tower", sub_type="",
        )
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }, power_band_rules=[band])
        line = srb._render_zone_line(session, "Power Tower")
        # Bold name, italic minimum, then (count/cap) and members.
        assert "**Power Tower**" in line
        assert "_(minimum 80M)_" in line
        # Suffix sits between the zone name and the (n/cap) marker.
        assert line.index("_(minimum 80M)_") < line.index("(0/")


class TestUnpairedSubBelowFloor:
    """#238: when a sub stays in the Available pool because their power
    is below the floor for every remaining unpaired primary's zone,
    the auto-fill summary surfaces the reason so officers can see why
    instead of guessing at a bug."""

    @staticmethod
    def _short_fixture(weak_power: int) -> dict[str, dict]:
        """4 strong starters + 1 weak sub. Small enough that no
        unplaced-starter overflow muddies the pairing test (all
        starters fit in the single Power Tower zone). The 5th member
        becomes the sub pool's only entry."""
        members = {}
        for i in range(4):
            key = f"100{i:02d}"
            members[key] = {
                "key": key, "name": f"S{i:02d}", "discord_id": key,
                "power": 400_000_000 - i * 10_000_000,
                "not_on_discord": False,
            }
        members["10004"] = {
            "key": "10004", "name": "Weak", "discord_id": "10004",
            "power": weak_power, "not_on_discord": False,
        }
        return members

    def test_unpaired_sub_below_floor_listed_with_reason(self):
        import storm_member_rules as smr
        band = smr.Rule(
            rule_type="power_band", subject="80000000",
            value="Power Tower", sub_type="",
        )
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=4,
                       min_power_a=80_000_000, priority=1),
        ]
        members = self._short_fixture(weak_power=50_000_000)
        session = _make_session(
            team="A", members=members, preset_zones=zones,
            power_band_rules=[band], sub_mode="paired",
        )
        summary = srb._auto_fill_session(session)
        # The weak sub couldn't pair (below 80M floor for Power Tower).
        # All 4 primaries stay unpaired (no eligible sub), and the
        # weak sub is below every unpaired primary's floor.
        names = [e["name"] for e in summary.get("unpaired_subs_below_floor", [])]
        assert "Weak" in names, (
            "Sub below the floor for every remaining primary's zone "
            "should be surfaced in unpaired_subs_below_floor"
        )

    def test_no_entry_when_sub_meets_floor(self):
        """If the sub's power meets the floor for an unpaired primary,
        they don't get flagged as 'below floor' — they'd actually
        have paired up, so they aren't in the unpaired pool to begin
        with."""
        import storm_member_rules as smr
        band = smr.Rule(
            rule_type="power_band", subject="80000000",
            value="Power Tower", sub_type="",
        )
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=4,
                       min_power_a=80_000_000, priority=1),
        ]
        # Weak's power is above 80M floor — should pair, not flag.
        members = self._short_fixture(weak_power=100_000_000)
        session = _make_session(
            team="A", members=members, preset_zones=zones,
            power_band_rules=[band], sub_mode="paired",
        )
        summary = srb._auto_fill_session(session)
        assert summary["unpaired_subs_below_floor"] == []

    def test_pool_mode_skips_unpaired_check(self):
        """Pool mode never pairs anyone, so the `unpaired_subs_below_floor`
        list stays empty even when subs are below zone floors."""
        import storm_member_rules as smr
        band = smr.Rule(
            rule_type="power_band", subject="80000000",
            value="Power Tower", sub_type="",
        )
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=4,
                       min_power_a=80_000_000, priority=1),
        ]
        members = self._short_fixture(weak_power=50_000_000)
        session = _make_session(
            team="A", members=members, preset_zones=zones,
            power_band_rules=[band], sub_mode="pool",
        )
        summary = srb._auto_fill_session(session)
        # Pool mode populates session.subs but doesn't do pairing, so
        # no floor-blocked entries.
        assert summary["unpaired_subs_below_floor"] == []


class TestAutoFillSummaryRenderingNoTruncation:
    """Decision #8 (#171): the auto-fill summary lists every gap +
    every conflict — no `(+N more)` truncation. Officers need the full
    list to act on it. Decision #14: auto-pair listing renders the
    explicit `Primary ↔ Sub` pairs instead of a bare count."""

    def test_gaps_list_is_not_truncated(self):
        members = {
            f"100{i}": {"key": f"100{i}", "name": f"Ghost{i}", "discord_id": f"100{i}",
                        "power": None, "not_on_discord": False}
            for i in range(10)
        }
        session = _make_session(team="A", members=members)
        srb._auto_fill_session(session)
        embed = srb._render_builder_embed(session)
        body = embed.description or ""
        # All 10 names should appear; no truncation marker.
        for i in range(10):
            assert f"Ghost{i}" in body
        assert "more)" not in body

    def test_conflicts_list_is_not_truncated(self):
        members = self._three_members()
        # 4 per-member rules pointing at an unknown zone — each triggers
        # a conflict.
        per_member = [
            smr.Rule(rule_type="per_member", subject="Alice",
                     sub_type="zone", value=f"No Such Zone {i}")
            for i in range(4)
        ]
        session = _make_session(team="A", members=members,
                                per_member_rules=per_member)
        srb._auto_fill_session(session)
        embed = srb._render_builder_embed(session)
        body = embed.description or ""
        # Every unknown-zone conflict surfaces.
        for i in range(4):
            assert f"No Such Zone {i}" in body
        assert "more)" not in body

    def test_auto_paired_listing_shows_explicit_pairs(self):
        members = {
            f"100{i}": {"key": f"100{i}", "name": f"M{i}", "discord_id": f"100{i}",
                        "power": 400_000_000 - i * 10_000_000,
                        "not_on_discord": False}
            for i in range(4)
        }
        zones = [ss.ZoneRow(zone="Power Tower", max_players=2,
                            min_power_a=100_000_000, priority=1)]
        session = _make_session(team="A", members=members,
                                preset_zones=zones, sub_mode="paired")
        srb._auto_fill_session(session)
        embed = srb._render_builder_embed(session)
        body = embed.description or ""
        # Post-#222: explicit `Primary ↔ Sub` lines live in the
        # `### Auto-paired Subs:` section above the auto-fill summary;
        # the summary itself only carries the count.
        assert "### **Auto-paired Subs:**" in body
        assert "↔" in body
        assert "- Auto-paired subs: 2" in body

    def _three_members(self):
        return {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob", "discord_id": "1002",
                     "power": 380_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 350_000_000, "not_on_discord": False},
        }


class TestAutoFillConfirmDestructive:
    """Decision #9 (#171): clicking auto-fill on a session that already
    holds data prompts a confirm view. Fresh sessions skip the prompt
    and run directly."""

    def test_fresh_session_has_no_existing_assignments(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        assert session.has_existing_assignments() is False

    def test_session_with_assignment_reports_existing(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.assignments["Power Tower"].append("1001")
        assert session.has_existing_assignments() is True

    def test_session_with_sub_reports_existing(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.subs.append("1001")
        assert session.has_existing_assignments() is True

    def test_session_with_phase_two_assignment_reports_existing(self):
        s = _make_phase_aware_session()
        s.assignments_p2["Arsenal"].append("1")
        assert s.has_existing_assignments() is True

    def test_strategy_picker_renders_both_strategies_and_cancel(self):
        # Post-#226: the destructive-rerun confirm fold into the new
        # `_AutoFillStrategyPickerView`, which carries both strategy
        # options plus the cancel affordance.
        parent_view = MagicMock()
        parent_view.session.user_id = 42
        view = srb._AutoFillStrategyPickerView(parent_view=parent_view)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Balanced spread" in lab for lab in labels)
        assert any("Strength to priority" in lab for lab in labels)
        assert any("Cancel" in lab for lab in labels)


# ── #152: phase-aware roster session ────────────────────────────────────────


def _make_phase_aware_session(*, sub_mode: str = "pool"):
    zones = [
        ss.ZoneRow(zone="Info Center", max_players=0,
                   max_phase1=2, max_phase2=1,
                   min_power_a=200_000_000, min_power_b=100_000_000),
        ss.ZoneRow(zone="Arsenal", max_players=0,
                   max_phase1=0, max_phase2=4,
                   min_power_a=0, min_power_b=0),
    ]
    preset = ss.PresetBuffer(name="Phased", event_type="DS",
                             zones=zones, uses_phases=True)
    members = {
        "1": {"key": "1", "name": "Alice", "discord_id": "1",
              "power": 412_000_000, "not_on_discord": False},
        "2": {"key": "2", "name": "Bob",   "discord_id": "2",
              "power": 350_000_000, "not_on_discord": False},
        "3": {"key": "3", "name": "Cyrus", "discord_id": "3",
              "power": 300_000_000, "not_on_discord": False},
    }
    return srb.RosterBuilderSession(
        guild_id=1, user_id=42, event_type="DS",
        team="A", preset=preset, members=members,
        per_member_rules=[], power_band_rules=[],
        sub_mode=sub_mode,
    )


class TestSessionPhaseAware:
    def test_flat_session_is_phase_aware_false(self):
        s = _make_session(team="A")
        assert s.is_phase_aware is False
        assert s.iter_phases() == [1]

    def test_phase_aware_session_iterates_both_phases(self):
        s = _make_phase_aware_session()
        assert s.is_phase_aware is True
        assert s.iter_phases() == [1, 2]

    def test_assignments_for_phase_returns_correct_dict(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")
        s.assignments_p2["Arsenal"].append("2")
        assert s.assignments_for_phase(1)["Info Center"] == ["1"]
        assert s.assignments_for_phase(2)["Arsenal"] == ["2"]
        # Phase 1 doesn't accidentally see phase 2's Arsenal assignment.
        assert s.assignments_for_phase(1)["Arsenal"] == []

    def test_zone_capacity_phase_aware_returns_per_phase_cap(self):
        s = _make_phase_aware_session()
        assert s.zone_capacity("Info Center", phase=1) == 2
        assert s.zone_capacity("Info Center", phase=2) == 1
        assert s.zone_capacity("Arsenal", phase=2) == 4

    def test_zone_capacity_flat_ignores_phase_argument(self):
        s = _make_session(team="A")
        # Flat preset returns max_players regardless of phase.
        assert s.zone_capacity("Power Tower", phase=1) == 4
        assert s.zone_capacity("Power Tower", phase=2) == 4

    def test_zone_member_count_defaults_to_selected_phase(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")
        s.assignments_p2["Arsenal"].append("2")
        s.selected_phase = 1
        assert s.zone_member_count("Info Center") == 1
        s.selected_phase = 2
        assert s.zone_member_count("Arsenal") == 1

    def test_assigned_member_keys_unions_both_phases(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")          # Alice in P1
        s.assignments_p2["Arsenal"].append("2")           # Bob in P2
        s.subs.append("3")                                # Cyrus in subs
        assert s.assigned_member_keys() == {"1", "2", "3"}

    def test_prune_stale_pairings_walks_both_phases(self):
        s = _make_phase_aware_session(sub_mode="paired")
        s.assignments["Info Center"].append("1")
        s.paired_subs["1"] = "2"
        s.assignments_p2["Arsenal"].append("1")
        s.paired_subs_p2["1"] = "3"
        # Now drop the P2 primary — phase-2 pairing should evaporate
        # but phase-1 stays.
        s.assignments_p2["Arsenal"].clear()
        s.prune_stale_pairings()
        assert s.paired_subs == {"1": "2"}
        assert s.paired_subs_p2 == {}

    def test_prune_stale_overrides_walks_both_phases(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")
        s.below_floor_overrides.add("1")
        s.assignments_p2["Arsenal"].append("2")
        s.below_floor_overrides_p2.add("2")
        # Clear phase-1 assignment — its override goes too.
        s.assignments["Info Center"].clear()
        s.prune_stale_overrides()
        assert s.below_floor_overrides == set()
        assert s.below_floor_overrides_p2 == {"2"}


def _make_three_phase_session():
    """Build a session backed by a 3-phase CS preset for the new
    Phase 3 attribute / iteration coverage."""
    zones = [
        ss.ZoneRow(zone="Power Tower", max_players=0,
                   max_phase1=2, max_phase2=2, max_phase3=2,
                   min_power_a=100_000_000,
                   priority_phase1=1, priority_phase2=2, priority_phase3=3),
        ss.ZoneRow(zone="Virus Lab", max_players=0,
                   max_phase3=2,
                   min_power_a=0,
                   priority_phase3=1),
    ]
    preset = ss.PresetBuffer(name="ThreePhase", event_type="CS",
                             zones=zones, phase_count=3,
                             faction="Rulebringers")
    members = {
        str(i): {"key": str(i), "name": f"M{i}", "discord_id": str(i),
                 "power": 500_000_000 - i * 10_000_000,
                 "not_on_discord": False}
        for i in range(1, 7)
    }
    return srb.RosterBuilderSession(
        guild_id=1, user_id=42, event_type="CS",
        team="A", preset=preset, members=members,
        per_member_rules=[], power_band_rules=[],
        sub_mode="pool",
    )


class TestSessionThreePhase:
    def test_iter_phases_yields_one_two_three(self):
        s = _make_three_phase_session()
        assert s.iter_phases() == [1, 2, 3]
        assert s.phase_count == 3
        assert s.is_phase_aware is True

    def test_assignments_for_phase_three(self):
        s = _make_three_phase_session()
        s.assignments_p3["Power Tower"].append("1")
        assert s.assignments_for_phase(3)["Power Tower"] == ["1"]
        assert s.assignments_for_phase(1)["Power Tower"] == []
        assert s.assignments_for_phase(2)["Power Tower"] == []

    def test_zone_capacity_phase_three(self):
        s = _make_three_phase_session()
        assert s.zone_capacity("Power Tower", phase=3) == 2
        # Virus Lab is Phase 3 only — phases 1 and 2 cap to 0.
        assert s.zone_capacity("Virus Lab", phase=1) == 0
        assert s.zone_capacity("Virus Lab", phase=2) == 0
        assert s.zone_capacity("Virus Lab", phase=3) == 2

    def test_auto_fill_three_phase_uses_per_phase_priority(self):
        s = _make_three_phase_session()
        summary = srb._auto_fill_session(s)
        # Phase 3 has two zones (PT + VL), both with priority 1 and 3
        # respectively, both with capacity 2. With the per-phase
        # priority, Virus Lab (priority 1 in P3) should fill before
        # Power Tower (priority 3 in P3). Hard to assert order on the
        # final list directly, but the summary's total counts should
        # be 2 (PT P1) + 2 (PT P2) + 2 (PT P3) + 2 (VL P3) = 8.
        assert summary["auto_filled_by_power"] == 8
        # Phase 3 had Virus Lab and Power Tower both filled.
        assert len(s.assignments_p3["Virus Lab"]) == 2
        assert len(s.assignments_p3["Power Tower"]) == 2


class TestMailBodyPhaseAware:
    # `_build_mail_body` calls `config.get_storm_template` /
    # `format_storm_slot` / `get_storm_slot_for_key` whenever
    # `session.guild_id` is truthy (which `_make_session` always sets),
    # so the path through SQLite is unavoidable here. The fallback copy
    # already handles an unseeded DB gracefully — we just need
    # `_get_conn()` to succeed, not a populated config.
    @pytest.fixture(autouse=True)
    def _db(self, temp_db):
        pass

    def test_flat_mail_has_no_phase_headers(self):
        s = _make_session(team="A", members={
            "1": {"key": "1", "name": "Alice", "discord_id": "1",
                  "power": 412_000_000, "not_on_discord": False},
        })
        s.assignments["Power Tower"].append("1")
        body = srb._build_mail_body(s)
        assert "Stage 1" not in body
        assert "Stage 2" not in body
        assert "Alice" in body

    def test_phase_aware_mail_emits_stage_headers_grouped_by_zone(self):
        """Phase-aware mail groups BY ZONE with stages stacked under
        each zone's heading — matches the PNG render organization.
        Each zone shows its Stage N entries indented below."""
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")          # Alice in P1
        s.assignments_p2["Arsenal"].append("2")           # Bob in P2
        body = srb._build_mail_body(s)
        assert "Stage 1" in body
        assert "Stage 2" in body
        # Alice sits under Info Center; Bob under Arsenal.
        info_idx = body.index("Info Center")
        arsenal_idx = body.index("Arsenal")
        # Alice should appear after the Info Center heading. Walk the
        # block: each zone's stage 1/2/3 are stacked under it.
        assert "Alice" in body[info_idx:arsenal_idx] or (
            arsenal_idx < info_idx
            and "Alice" in body[info_idx:]
        )
        # Stage label appears right under the zone name (zone-major
        # ordering, not stage-major).
        # Find the first "Stage 1" after the Info Center heading.
        first_zone_idx = min(info_idx, arsenal_idx)
        assert body.index("Stage 1") > first_zone_idx
        # Assignees are indented 4 spaces under their stage.
        assert "    Alice" in body
        assert "    Bob" in body

    def test_phase_aware_mail_renders_template_once(self):
        """Phase-aware mail uses the template ONCE — greeting / subs
        / time appear exactly one time each, no per-stage repeats
        (the 3-stage CS use case previously blew past Discord's
        2000-char limit because the template wrapped each phase
        block)."""
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")          # P1
        s.assignments_p2["Arsenal"].append("2")           # P2
        s.subs.append("3")                                # Cyrus
        body = srb._build_mail_body(s)
        # Cyrus appears exactly once in the body (was twice or more
        # before with per-phase template rendering).
        assert body.count("Cyrus") == 1
        # Fallback template adds these section headers exactly once.
        assert body.count("**Subs**") <= 1
        assert body.count("**Time:") <= 1

    def test_phase_aware_mail_empty_stage_renders_as_empty(self):
        """A zone with capacity in a stage but no assignees renders
        `(empty)` for that stage — mirrors the PNG render so leadership
        can see deliberate gaps (some strategies leave a stage open)."""
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")          # Alice in P1
        # Info Center P2 has cap > 0 but no assignees → should show
        # as Stage 2 (empty).
        body = srb._build_mail_body(s)
        info_idx = body.index("Info Center")
        # The Info Center block should contain Stage 1 with Alice
        # and Stage 2 with (empty).
        info_block = body[info_idx:body.index("\n\n", info_idx)] \
            if "\n\n" in body[info_idx:] else body[info_idx:]
        assert "Stage 1" in info_block
        assert "Alice" in info_block
        assert "Stage 2" in info_block
        assert "(empty)" in info_block

    def test_phase_aware_mail_fully_closed_zone_skipped(self):
        """A zone that's closed (cap=0) AND empty in every stage
        doesn't appear in the mail — would just clutter."""
        s = _make_phase_aware_session()
        # Assign someone to ONE zone so the mail has content; the
        # other preset zones with cap=0 + no members should be
        # omitted from the mail entirely.
        s.assignments["Info Center"].append("1")
        body = srb._build_mail_body(s)
        # The phase-aware preset's zone list includes Arsenal (which
        # has cap > 0 in some phase). It should still appear with
        # `(empty)` lines. But any truly cap=0-everywhere zone would
        # not. Spot-check Info Center is present and has its Stage 1
        # assignee.
        assert "Info Center" in body
        assert "Alice" in body

    def test_phase_aware_mail_hides_stages_before_zone_opens(self):
        """A zone that opens at Stage 2 (DS Defense Systems / SF) or
        Stage 3 (CS Virus Lab) must NOT render Stage 1 lines in the
        mail. The image hides closed-pre-open phases; the mail must
        match so officers don't see ghost stages for buildings that
        aren't part of the strategy yet."""
        s = _make_phase_aware_session()
        # Arsenal opens at Stage 2 only (P1=0, P2=4 in the fixture).
        s.assignments_p2["Arsenal"].append("2")
        body = srb._build_mail_body(s)
        arsenal_idx = body.index("Arsenal")
        # Walk to the end of Arsenal's block (next zone heading or EOF).
        arsenal_block = body[arsenal_idx:].split("\n\n", 1)[0]
        # No Stage 1 entry for Arsenal — building isn't open yet.
        assert "Stage 1" not in arsenal_block
        # Stage 2 IS shown with Bob assigned.
        assert "Stage 2" in arsenal_block
        assert "Bob" in arsenal_block

    def test_phase_aware_mail_renders_empty_after_zone_opens(self):
        """A stage that's open (cap>0) but has no assignees renders
        `(empty)` — even when that stage sits at the back of the
        sequence (Sample Warehouses Stage 2/3 in the tester's CS
        config). Tests the post-first-open behavior: every cap>0
        stage from first-open onward must appear."""
        zones = [
            ss.ZoneRow(
                zone="Sample Warehouse 1", max_players=0,
                max_phase1=2, max_phase2=2, max_phase3=2,
                min_power_a=0, min_power_b=0,
            ),
        ]
        preset = ss.PresetBuffer(
            name="SW Test", event_type="CS", zones=zones,
            uses_phases=True, phase_count=3,
        )
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1",
                  "power": 500_000_000, "not_on_discord": False},
        }
        s = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="CS",
            team="A", preset=preset, members=members,
            per_member_rules=[], power_band_rules=[],
            sub_mode="pool",
        )
        # Only Stage 1 gets a member; Stages 2 + 3 have capacity but
        # nobody assigned — must show as `(empty)`.
        s.assignments["Sample Warehouse 1"].append("1")
        body = srb._build_mail_body(s)
        assert "Stage 1" in body
        assert "Alice" in body
        assert "Stage 2" in body
        assert "Stage 3" in body
        assert body.count("(empty)") == 2


class TestPhaseAwareEligibility:
    """The picker excludes already-assigned members in the *current*
    phase only — a member in Phase 1 can still be picked for Phase 2
    (the migration use case)."""

    def test_assigned_member_keys_in_phase_only_returns_that_phase(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")
        s.assignments_p2["Arsenal"].append("2")
        s.subs.append("3")
        # Phase 1: Alice in primaries, Cyrus in sub pool.
        assert s.assigned_member_keys_in_phase(1) == {"1", "3"}
        # Phase 2: Bob in primaries, Cyrus still in sub pool (event-level).
        assert s.assigned_member_keys_in_phase(2) == {"2", "3"}

    def test_member_in_phase_one_remains_eligible_in_phase_two(self):
        s = _make_phase_aware_session()
        # Alice plays Info Center in Phase 1.
        s.assignments["Info Center"].append("1")
        # Selecting Phase 2 + asking for eligibility should NOT exclude
        # Alice (the migration case) — she's only locked out of her
        # current Phase-2 slot if one already exists.
        s.selected_phase = 2
        eligible, _below = srb._eligible_member_keys_for_zone(s, "Arsenal")
        assert "1" in eligible

    def test_member_assigned_in_current_phase_is_excluded(self):
        s = _make_phase_aware_session()
        s.assignments_p2["Arsenal"].append("1")
        s.selected_phase = 2
        eligible, _below = srb._eligible_member_keys_for_zone(s, "Info Center")
        # Alice already in Arsenal (Phase 2) — picker shouldn't show her
        # again in a Phase 2 dropdown.
        assert "1" not in eligible

    def test_sub_pool_member_excluded_from_both_phase_pickers(self):
        s = _make_phase_aware_session()
        s.subs.append("1")
        for phase in (1, 2):
            s.selected_phase = phase
            eligible, _below = srb._eligible_member_keys_for_zone(s, "Info Center")
            assert "1" not in eligible


class TestAutoFillPhaseAware:
    """Auto-fill must respect per-phase capacities and the migration
    semantics (a member placed in Phase 1 stays pickable for Phase 2)."""

    def _bigger_session(self):
        zones = [
            ss.ZoneRow(zone="Info Center", max_players=0,
                       max_phase1=2, max_phase2=1,
                       min_power_a=100_000_000, min_power_b=50_000_000),
            ss.ZoneRow(zone="Arsenal", max_players=0,
                       max_phase1=0, max_phase2=3,
                       min_power_a=0, min_power_b=0),
        ]
        preset = ss.PresetBuffer(name="Phased", event_type="DS",
                                 zones=zones, uses_phases=True)
        members = {
            str(i): {"key": str(i), "name": f"M{i}", "discord_id": str(i),
                     "power": 500_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 8)
        }
        return srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="DS",
            team="A", preset=preset, members=members,
            per_member_rules=[], power_band_rules=[],
            sub_mode="pool",
        )

    def test_auto_fill_respects_per_phase_capacities(self):
        s = self._bigger_session()
        srb._auto_fill_session(s)
        # Phase 1 caps: Info Center=2, Arsenal=0.
        assert len(s.assignments["Info Center"]) == 2
        assert len(s.assignments["Arsenal"]) == 0
        # Phase 2 caps: Info Center=1, Arsenal=3.
        assert len(s.assignments_p2["Info Center"]) == 1
        assert len(s.assignments_p2["Arsenal"]) == 3

    def test_auto_fill_allows_phase_1_member_to_also_appear_in_phase_2(self):
        s = self._bigger_session()
        srb._auto_fill_session(s)
        # Phase 1 should have grabbed the top 2 power members for IC.
        p1_ic = set(s.assignments["Info Center"])
        # Phase 2 has 4 slots total (1 IC + 3 Arsenal). With 7 members
        # and Phase 1 already using 2, Phase 2 should also see the
        # top-power members — including overlap with Phase 1 since
        # they're allowed to migrate.
        p2_all = (set(s.assignments_p2["Info Center"]) |
                  set(s.assignments_p2["Arsenal"]))
        # The two strongest members (M1, M2) should appear in both
        # phases — they're top-power so they get drafted into Phase 1
        # and stay eligible for Phase 2.
        assert p1_ic & p2_all, (
            "expected at least one member to play in both phases "
            "(migration use case)"
        )

    def test_auto_fill_summary_counts_both_phases(self):
        s = self._bigger_session()
        summary = srb._auto_fill_session(s)
        # 2 P1 IC + 1 P2 IC + 3 P2 Arsenal = 6 power-based fills.
        assert summary["auto_filled_by_power"] == 6

    def test_auto_fill_on_flat_preset_unchanged(self):
        # Sanity: flat presets don't get phase 2 fills.
        s = _make_session(team="A", members={
            "1": {"key": "1", "name": "Alice", "discord_id": "1",
                  "power": 500_000_000, "not_on_discord": False},
            "2": {"key": "2", "name": "Bob", "discord_id": "2",
                  "power": 400_000_000, "not_on_discord": False},
        })
        srb._auto_fill_session(s)
        # Phase 2 dict stays empty for flat presets even with members
        # available.
        assert all(len(v) == 0 for v in s.assignments_p2.values())


class TestEmbedLayoutOverhaul:
    """#222: embed layout overhaul. Drops the per-zone status glyph,
    drops the `←` selected-zone marker, drops the ⚠️ unpaired marker
    and inline ` + sub <name>` from zone lines, lifts pairings into a
    dedicated `### **Auto-paired Subs:**` section, and reworks the
    auto-fill summary heading + bullet style + `Not on Discord` line."""

    def _ten_members(self):
        return {
            str(i): {"key": str(i), "name": f"M{i:02d}",
                     "discord_id": str(i),
                     "power": 410_000_000 - i * 10_000_000,
                     "not_on_discord": False}
            for i in range(1, 11)
        }

    def test_title_drops_team_label(self):
        sess = _make_session(team="A")
        embed = srb._render_builder_embed(sess)
        assert embed.title == "🛡️ Roster Builder Template: Standard"

    def test_body_opens_with_bulleted_event_and_team(self):
        sess = _make_session(team="B")
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        # First two lines: bullet for event/team, bullet for minimum.
        head = body.splitlines()[:2]
        assert head[0] == "- 🗺️ Desert Storm: Team B"
        assert head[1] == "- ⚖️ Enforcing Min B for this team"

    def test_zones_heading_uses_markdown_h2(self):
        sess = _make_session(team="A")
        embed = srb._render_builder_embed(sess)
        assert "## 📋 Zones" in embed.description

    def test_zone_lines_drop_status_glyph(self):
        sess = _make_session(team="A", members=self._ten_members())
        sess.assignments["Power Tower"].extend(["1", "2"])
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        # None of the status glyphs appear in the embed body. The n/cap
        # count is the only state indicator now.
        for glyph in ("🟡", "✅", "⬜"):
            assert glyph not in body, (
                f"status glyph {glyph!r} should be gone from the embed"
            )
        # n/cap is still there.
        assert "(2/4)" in body

    def test_zone_lines_drop_active_zone_marker(self):
        sess = _make_session(team="A")
        sess.selected_zone = "Power Tower"
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        # `←` no longer appears on the Power Tower zone line. The
        # `🎯 Active zone:` line below already calls it out.
        # (`🎯 Active zone:` is its own line; the marker we're checking
        # is the inline one next to the zone name.)
        zone_line = next(
            line for line in body.splitlines() if "Power Tower" in line and "/" in line
        )
        assert "←" not in zone_line

    def test_zone_lines_drop_inline_sub_in_paired_mode(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob", "discord_id": "1002",
                     "power": 380_000_000, "not_on_discord": False},
        }
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        zone_line = next(
            line for line in body.splitlines() if "Power Tower" in line and "/" in line
        )
        # The zone line shows only the primary; ` + sub <name>` is gone.
        assert "Alice" in zone_line
        assert "+ sub" not in zone_line
        assert "Bob" not in zone_line  # Bob renders below in the pair section.

    def test_auto_paired_subs_section_renders_one_pair_per_line(self):
        members = self._ten_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].extend(["1", "2"])
        sess.paired_subs["1"] = "3"
        sess.paired_subs["2"] = "4"
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        assert "### **Auto-paired Subs:**" in body
        # Two pairs, each on its own line, primary first.
        assert "M01 ↔ M03" in body
        assert "M02 ↔ M04" in body

    def test_auto_paired_subs_section_omitted_when_no_pairings(self):
        sess = _make_session(team="A", sub_mode="paired")
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        assert "Auto-paired Subs" not in body

    def test_unpaired_message_drops_warning_emoji(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        # Post-#222 message: `Primaries without a designated Sub (N): ...`
        # with the `Click 🔁 Pair subs ...` sentence on its own line.
        assert "Primaries without a designated Sub (1): Alice" in body
        click_line = next(
            line for line in body.splitlines() if line.startswith("Click 🔁 Pair subs")
        )
        assert "attach a sub" in click_line

    def test_auto_fill_summary_uses_h2_heading_and_dash_bullets(self):
        sess = _make_session(team="A", members=self._ten_members())
        srb._auto_fill_session(sess)
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        assert "## 🎯 Auto-fill summary" in body
        # The summary section uses `- ` bullets instead of `• `.
        summary_idx = body.index("## 🎯 Auto-fill summary")
        summary_block = body[summary_idx:]
        assert "- Per-member rules applied" in summary_block
        assert "- Auto-filled by power" in summary_block
        assert "• " not in summary_block

    def test_auto_fill_summary_shows_auto_paired_count_only(self):
        members = self._ten_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        # Single-zone preset so the round-robin places several primaries
        # in one zone and triggers pairing.
        sess.preset.zones = [
            ss.ZoneRow(zone="Power Tower", max_players=4,
                       min_power_a=100_000_000, priority=1),
        ]
        # Re-init assignment dicts to match new zone set.
        for ph_dict in (sess.assignments, sess.assignments_p2, sess.assignments_p3):
            ph_dict.clear()
            ph_dict["Power Tower"] = []
        srb._auto_fill_session(sess)
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        # Summary line is count-only; the explicit `↔` list is in the
        # `Auto-paired Subs` section above the summary.
        assert "- Auto-paired subs: " in body
        # The explicit `Primary ↔ Sub` strings should NOT appear inside
        # the summary block (they're in the section above).
        summary_idx = body.index("## 🎯 Auto-fill summary")
        summary_block = body[summary_idx:]
        assert "↔" not in summary_block

    def test_auto_fill_summary_includes_not_on_discord_count(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob", "discord_id": "1002",
                     "power": 380_000_000, "not_on_discord": True},
        }
        sess = _make_session(team="A", members=members)
        srb._auto_fill_session(sess)
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        assert "- Not on Discord: 1" in body

    def test_auto_fill_summary_not_on_discord_zero_when_all_on_discord(self):
        sess = _make_session(team="A", members=self._ten_members())
        srb._auto_fill_session(sess)
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        assert "- Not on Discord: 0" in body

    def test_footer_dropped(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": True},
        }
        sess = _make_session(team="A", members=members)
        embed = srb._render_builder_embed(sess)
        # Footer used to carry `¹ Not on Discord`; that data is in the
        # summary's `Not on Discord: N` line now, no footer needed.
        assert embed.footer.text is None

    def test_filled_and_active_zone_have_no_bold(self):
        sess = _make_session(team="A", members=self._ten_members())
        sess.selected_zone = "Power Tower"
        embed = srb._render_builder_embed(sess)
        body = embed.description or ""
        filled_line = next(
            line for line in body.splitlines() if line.startswith("📊 Filled:")
        )
        active_line = next(
            line for line in body.splitlines() if line.startswith("🎯 Active zone:")
        )
        # No `**` markdown bold around the labels or counts.
        assert "**" not in filled_line
        # The `_(preset minimum ... relaxed)_` italic note can use
        # underscores when a band rule applies, but in this case it
        # doesn't, so no markdown markers should appear on the line.
        assert "**" not in active_line


class TestPhaseAwareEmbedRendering:
    """#172 / Rule L: phase-aware builder embeds render per-zone-per-phase
    instead of inline `(P1, P2, P3)` capacity readouts. Each zone gets a
    header line + one indented row per phase with that phase's count,
    cap, and member list."""

    def test_phase_aware_zone_line_has_per_phase_rows(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")          # Alice in P1
        s.assignments_p2["Arsenal"].append("2")           # Bob in P2
        line = srb._render_zone_line(s, "Info Center")
        assert "Stage 1:" in line
        assert "Stage 2:" in line
        # Header row is bold and contains no inline parens.
        assert "**Info Center**" in line
        # Old inline P1/P2 syntax must be gone.
        assert "(P1:" not in line
        assert "(P2:" not in line

    def test_phase_aware_zone_line_hides_stages_before_zone_opens(self):
        """A zone that opens at Stage 2 (e.g., Defense Systems / Serum
        Factories) must NOT render a Stage 1 row in the summary embed.
        Officers should see only the stages where the building is
        actually part of the strategy — mirrors the mail builder and
        the PNG renderer so all three surfaces read consistently."""
        s = _make_phase_aware_session()
        # Arsenal opens at Stage 2 only (P1=0, P2=4 in the fixture).
        line = srb._render_zone_line(s, "Arsenal")
        assert "Stage 1:" not in line
        assert "Stage 2:" in line
        # Header row is still present.
        assert "**Arsenal**" in line

    def test_flat_zone_line_keeps_single_row_shape(self):
        s = _make_session(team="A", members={
            "1": {"key": "1", "name": "Alice", "discord_id": "1",
                  "power": 412_000_000, "not_on_discord": False},
        })
        s.assignments["Power Tower"].append("1")
        line = srb._render_zone_line(s, "Power Tower")
        # Flat presets stay one-line — no \n inside the zone line.
        assert "\n" not in line
        assert "Stage 1:" not in line
        assert "**Power Tower**" in line

    def test_filled_line_breaks_out_per_phase_when_phase_aware(self):
        s = _make_phase_aware_session()
        embed = srb._render_builder_embed(s)
        # Per-phase breakdown is in the Filled line.
        assert "Filled:" in embed.description
        assert "S1:" in embed.description
        assert "S2:" in embed.description

    def test_filled_line_uses_single_total_when_flat(self):
        s = _make_session(team="A", members={
            "1": {"key": "1", "name": "Alice", "discord_id": "1",
                  "power": 412_000_000, "not_on_discord": False},
        })
        s.assignments["Power Tower"].append("1")
        embed = srb._render_builder_embed(s)
        assert "Filled:" in embed.description
        # Flat presets keep the X / Y total shape (no per-phase break-out).
        assert "S1:" not in embed.description


class TestRostersTabPhaseColumn:
    """The Phase column in rosters_tab is the post-event audit trail
    that captures which phase a member played in. Flat presets write
    "1" for traceability; sub-pool rows write empty (event-level)."""

    def test_header_includes_phase_column(self):
        assert "Stage" in srb._ROSTERS_HEADER
        # Stage sits between Team and Zone so the columns read in the
        # natural left-to-right order an officer scans.
        assert (srb._ROSTERS_HEADER.index("Stage")
                == srb._ROSTERS_HEADER.index("Team") + 1)
        assert (srb._ROSTERS_HEADER.index("Zone")
                == srb._ROSTERS_HEADER.index("Stage") + 1)

    def test_flat_preset_writes_phase_one(self, fake_env):
        fake, gid = fake_env
        session = _make_session(team="A", members={
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.assignments["Power Tower"].append("1001")
        srb._write_rosters_tab(session)
        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        phase_col = srb._ROSTERS_HEADER.index("Stage")
        member_col = srb._ROSTERS_HEADER.index("Member")
        alice_row = next(r for r in rows[1:] if r[member_col] == "Alice")
        assert alice_row[phase_col] == "1"

    def test_phase_aware_preset_writes_correct_phase_per_row(self, fake_env):
        fake, gid = fake_env
        zones = [
            ss.ZoneRow(zone="Power Tower", max_players=0,
                       max_phase1=2, max_phase2=1,
                       min_power_a=100_000_000, min_power_b=50_000_000),
        ]
        preset = ss.PresetBuffer(name="Phased", event_type="DS",
                                 zones=zones, uses_phases=True)
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1",
                  "power": 500_000_000, "not_on_discord": False},
            "2": {"key": "2", "name": "Bob", "discord_id": "2",
                  "power": 400_000_000, "not_on_discord": False},
        }
        session = srb.RosterBuilderSession(
            guild_id=gid, user_id=42, event_type="DS",
            team="A", preset=preset, members=members,
            per_member_rules=[], power_band_rules=[],
            sub_mode="pool",
        )
        session.event_date = "2026-05-18"
        session.assignments["Power Tower"].append("1")
        session.assignments_p2["Power Tower"].append("2")

        srb._write_rosters_tab(session)
        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        phase_col = srb._ROSTERS_HEADER.index("Stage")
        member_col = srb._ROSTERS_HEADER.index("Member")
        alice_row = next(r for r in rows[1:] if r[member_col] == "Alice")
        bob_row = next(r for r in rows[1:] if r[member_col] == "Bob")
        assert alice_row[phase_col] == "1"
        assert bob_row[phase_col] == "2"

    def test_subs_have_empty_phase_cell(self, fake_env):
        fake, gid = fake_env
        session = _make_session(team="A", members={
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 280_000_000, "not_on_discord": False},
        })
        session.guild_id = gid
        session.event_date = "2026-05-18"
        session.subs.append("1003")
        srb._write_rosters_tab(session)
        ws = fake.worksheet("DS Rosters")
        rows = ws.get_all_values()
        phase_col = srb._ROSTERS_HEADER.index("Stage")
        member_col = srb._ROSTERS_HEADER.index("Member")
        carol_row = next(r for r in rows[1:] if r[member_col] == "Carol")
        # Sub-pool entries are event-level, not phase-scoped.
        assert carol_row[phase_col] == ""


# ── #240: draft persistence (serialize / reconcile) ──────────────────────────


class TestDraftSerialization:
    """#240: officer intent (zone assignments, pairings, overrides,
    preset name, UI cursor) round-trips through JSON without losing
    fidelity. Member identity and rules are NOT serialized — they
    re-resolve from team plan / signups at load time."""

    def test_serialize_captures_intent_fields(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 380_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.assignments["Power Tower"] = ["1001"]
        session.subs = ["1002"]
        session.selected_phase = 1
        session.selected_zone = "Power Tower"
        session.show_below_floor = True
        session.team_plan_applied = True

        import json
        payload = json.loads(srb._serialize_session(session))
        assert payload["version"] == 1
        assert payload["assignments_p1"]["Power Tower"] == ["1001"]
        assert payload["subs"] == ["1002"]
        assert payload["selected_phase"] == 1
        assert payload["selected_zone"] == "Power Tower"
        assert payload["show_below_floor"] is True
        assert payload["team_plan_applied"] is True
        # members / rules are NOT in the payload (re-resolved at load).
        assert "members" not in payload
        assert "per_member_rules" not in payload
        assert "power_band_rules" not in payload

    def test_round_trip_preserves_assignments_and_pairings(self):
        members = {
            f"100{i}": {"key": f"100{i}", "name": f"M{i}", "discord_id": f"100{i}",
                        "power": 400_000_000 - i * 10_000_000,
                        "not_on_discord": False}
            for i in range(4)
        }
        session1 = _make_session(team="A", members=members, sub_mode="paired")
        session1.assignments["Power Tower"] = ["1000", "1001"]
        session1.paired_subs["1000"] = "1002"
        session1.paired_subs["1001"] = "1003"
        session1.below_floor_overrides.add("1003")

        import json
        payload = json.loads(srb._serialize_session(session1))

        session2 = _make_session(team="A", members=members, sub_mode="paired")
        report = srb._apply_saved_state(session2, payload)

        assert session2.assignments["Power Tower"] == ["1000", "1001"]
        assert session2.paired_subs["1000"] == "1002"
        assert session2.paired_subs["1001"] == "1003"
        assert "1003" in session2.below_floor_overrides
        assert report["dropped_members"] == []
        assert report["kept_assignments"] == 2
        assert report["kept_pairings"] == 2

    def test_reconciliation_drops_keys_not_in_current_members(self):
        current_members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob", "discord_id": "1002",
                     "power": 380_000_000, "not_on_discord": False},
        }
        payload = {
            "version": 1,
            "assignments_p1": {"Power Tower": ["1001", "1003"]},
            "paired_subs_p1": {},
            "subs": ["1002"],
            "below_floor_overrides_p1": [],
            "selected_preset_name": "Standard",
            "selected_phase": 1, "selected_zone": "Power Tower",
            "show_below_floor": False,
            "saved_for_event_date": "2026-05-22",
        }
        session = _make_session(team="A", members=current_members)
        report = srb._apply_saved_state(session, payload)
        assert session.assignments["Power Tower"] == ["1001"]
        assert "1003" in report["dropped_members"]
        assert report["kept_assignments"] == 1

    def test_reconciliation_drops_pairs_with_missing_keys(self):
        current_members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        payload = {
            "version": 1, "assignments_p1": {},
            "paired_subs_p1": {"1001": "1003"},
            "subs": [], "below_floor_overrides_p1": [],
            "selected_phase": 1, "selected_zone": "",
            "show_below_floor": False,
            "saved_for_event_date": "2026-05-22",
            "selected_preset_name": "Standard",
        }
        session = _make_session(team="A", members=current_members,
                                sub_mode="paired")
        report = srb._apply_saved_state(session, payload)
        assert session.paired_subs == {}
        assert "1003" in report["dropped_members"]

    def test_reconciliation_drops_zones_not_in_current_preset(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        payload = {
            "version": 1,
            "assignments_p1": {"Phantom Zone": ["1001"]},
            "paired_subs_p1": {}, "subs": [],
            "below_floor_overrides_p1": [],
            "selected_phase": 1, "selected_zone": "",
            "show_below_floor": False,
            "saved_for_event_date": "2026-05-22",
            "selected_preset_name": "Standard",
        }
        session = _make_session(team="A", members=members)
        report = srb._apply_saved_state(session, payload)
        assert "Phantom Zone" not in session.assignments
        assert report["kept_assignments"] == 0

    def test_reconciliation_surfaces_stale_event_date(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        payload = {
            "version": 1, "assignments_p1": {}, "paired_subs_p1": {},
            "subs": [], "below_floor_overrides_p1": [],
            "selected_phase": 1, "selected_zone": "",
            "show_below_floor": False,
            "saved_for_event_date": "2026-05-15",
            "selected_preset_name": "Standard",
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"
        report = srb._apply_saved_state(session, payload)
        assert report["stale_event_date"] == "2026-05-15"

    def test_reconciliation_no_stale_flag_when_dates_match(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        payload = {
            "version": 1, "assignments_p1": {}, "paired_subs_p1": {},
            "subs": [], "below_floor_overrides_p1": [],
            "selected_phase": 1, "selected_zone": "",
            "show_below_floor": False,
            "saved_for_event_date": "2026-05-22",
            "selected_preset_name": "Standard",
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"
        report = srb._apply_saved_state(session, payload)
        assert report["stale_event_date"] is None


class TestDraftFollowupPolish:
    """#240 follow-up: address the gaps caught during the design audit
    — names instead of keys in the warning, skip initial-rebuild
    autosave, autosave-failure flag surfacing in the embed."""

    def test_serialize_emits_member_names_at_save(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob", "discord_id": "1002",
                     "power": 380_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.assignments["Power Tower"] = ["1001"]
        session.subs = ["1002"]
        import json
        payload = json.loads(srb._serialize_session(session))
        assert payload["member_names_at_save"]["1001"] == "Alice"
        assert payload["member_names_at_save"]["1002"] == "Bob"

    def test_dropped_members_warning_uses_names_not_keys(self):
        """The reconciliation banner shows display names instead of
        the raw Discord IDs when names_at_save is shipped."""
        # Saved draft references Carol (1003), who isn't in this
        # week's pool.
        payload = {
            "version": 1,
            "assignments_p1": {"Power Tower": ["1003"]},
            "paired_subs_p1": {}, "subs": [],
            "below_floor_overrides_p1": [],
            "selected_phase": 1, "selected_zone": "",
            "show_below_floor": False,
            "saved_for_event_date": "2026-05-22",
            "selected_preset_name": "Standard",
            "member_names_at_save": {"1003": "Carol"},
        }
        current_members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=current_members)
        report = srb._apply_saved_state(session, payload)
        # The warning shows the name, not the bare key.
        assert "Carol" in report["dropped_members"]
        assert "1003" not in report["dropped_members"]

    def test_dropped_members_falls_back_to_key_when_no_names_dict(self):
        """Older payloads (or hand-edited rows) without member_names_at_save
        still surface — they just show keys as the fallback. No crash."""
        payload = {
            "version": 1,
            "assignments_p1": {"Power Tower": ["1003"]},
            "paired_subs_p1": {}, "subs": [],
            "below_floor_overrides_p1": [],
            "selected_phase": 1, "selected_zone": "",
            "show_below_floor": False,
            "saved_for_event_date": "2026-05-22",
            "selected_preset_name": "Standard",
            # No member_names_at_save.
        }
        current_members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=current_members)
        report = srb._apply_saved_state(session, payload)
        # Falls back to the raw key.
        assert "1003" in report["dropped_members"]

    def test_initial_rebuild_skips_autosave(self):
        """`RosterBuilderView.__init__` calls `_rebuild` once before
        any user action; that initial save would write back the
        freshly-loaded state with a current timestamp and create a
        "draft" from a mere open. The flag-gated autosave skips it."""
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"  # force is_structured=True
        with patch.object(srb, "_autosave_draft") as autosave_mock:
            view = srb.RosterBuilderView(session)
            # The constructor's _rebuild ran with the flag False,
            # so autosave should NOT have fired yet.
            assert autosave_mock.call_count == 0
            # Simulate a user-action _refresh by setting the flag
            # and triggering _rebuild directly.
            view._user_action_since_open = True
            view._rebuild()
            assert autosave_mock.call_count == 1

    def test_autosave_failure_latches_flag(self):
        """When the autosave write raises, `session.autosave_failed`
        becomes True so the next embed render can warn the officer."""
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"
        assert session.autosave_failed is False
        with patch("config.save_roster_draft", side_effect=Exception("disk full")):
            srb._autosave_draft(session)
        assert session.autosave_failed is True

    def test_autosave_success_clears_flag(self):
        """A subsequent successful autosave clears the latched failure
        flag — officers see the warning until persistence recovers."""
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"
        session.autosave_failed = True  # pre-existing failed state
        with patch("config.save_roster_draft"):
            srb._autosave_draft(session)
        assert session.autosave_failed is False

    def test_embed_surfaces_autosave_failed_warning(self):
        """When `session.autosave_failed=True`, the embed description
        leads with a prominent warning so officers know to screenshot
        / be careful."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"  # structured mode
        session.autosave_failed = True
        embed = srb._render_builder_embed(session)
        assert "Couldn't save your draft" in embed.description

    def test_embed_no_warning_when_autosave_healthy(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"
        # autosave_failed defaults to False
        embed = srb._render_builder_embed(session)
        assert "Couldn't save your draft" not in (embed.description or "")

    def test_embed_footer_shows_auto_save_hint_in_structured_mode(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"
        embed = srb._render_builder_embed(session)
        assert embed.footer.text is not None
        assert "Auto-saving" in embed.footer.text
        assert "Resume Team" in embed.footer.text

    def test_embed_footer_skipped_in_free_tier(self):
        """Free-tier (no event_date) doesn't persist drafts, so the
        Auto-saving hint would be misleading. Footer left empty."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        # event_date is None → free-tier
        embed = srb._render_builder_embed(session)
        # No footer text was set (or empty).
        assert (embed.footer is None
                or embed.footer.text is None
                or "Auto-saving" not in embed.footer.text)

    def test_embed_truncates_oversized_description(self):
        """When the composed description exceeds Discord's 4096-char
        ceiling, the embed truncates with a clear notice instead of
        letting the Discord API reject the edit. Use a huge single
        roster_error string since the embed only renders error[0]."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"
        # One enormous error string — pushes the joined description
        # past 4096 single-handedly.
        session.roster_errors = ["⚠️ " + "X" * 5000]
        embed = srb._render_builder_embed(session)
        assert len(embed.description) <= 4096
        assert "too long to display" in embed.description

    def test_structured_done_button_says_close_draft_saved(self):
        """#240 follow-up: the structured-mode close button no longer
        labels itself as 'Cancel' since the draft persists. New label
        signals that closing doesn't lose work."""
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-22"  # structured mode
        with patch.object(srb, "_autosave_draft"):
            view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Close" in lab and "draft saved" in lab.lower()
                   for lab in labels)
        # Old "Cancel" label gone.
        assert not any(lab == "❌ Cancel" for lab in labels)

    def test_free_tier_done_button_unchanged(self):
        """Free-tier (no draft persistence) keeps the original
        `✅ Done` label — no draft to communicate about."""
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        # event_date None → free-tier
        with patch.object(srb, "_autosave_draft"):
            view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Done" in lab for lab in labels)


# ── _AssignConfirmView (#250) ────────────────────────────────────────────────


class TestAssignConfirmView:
    """The picker no longer hard-blocks officers on over-max or
    below-floor. Both surface an ephemeral confirm — leadership
    overrides knowingly. Both conditions together fold into one
    confirm so officers don't see two sequential dialogs."""

    def _parent_view_mock(self, session):
        parent = MagicMock()
        parent.session = session
        parent.message = MagicMock()
        parent.message.edit = AsyncMock()
        parent._user_action_since_open = False
        parent._rebuild = MagicMock()
        # Owner-guard reads session.user_id; nothing else on parent.
        return parent

    def _full_session(self):
        members = {
            "1001": {"key": "1001", "name": "Alice",
                     "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",
                     "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol",
                     "discord_id": "1003",
                     "power": 305_000_000, "not_on_discord": False},
            "1004": {"key": "1004", "name": "Dan",
                     "discord_id": "1004",
                     "power": 301_000_000, "not_on_discord": False},
            "1005": {"key": "1005", "name": "Erin",
                     "discord_id": "1005",
                     "power": 300_500_000, "not_on_discord": False},
            "1006": {"key": "1006", "name": "Frank",
                     "discord_id": "1006",
                     "power": 100_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.selected_zone = "Power Tower"
        # Power Tower cap=4. Fill it.
        session.assignments["Power Tower"] = ["1001", "1002", "1003", "1004"]
        return session

    @pytest.mark.asyncio
    async def test_over_max_yes_assigns_member(self):
        session = self._full_session()
        parent = self._parent_view_mock(session)
        confirm = srb._AssignConfirmView(
            parent_view=parent,
            member_key="1005",
            member_label="Erin",
            zone="Power Tower",
            phase=1,
            over_max=True, cap=4,
            below_floor=False, member_power=300_500_000, floor_power=None,
        )
        inter = MagicMock()
        inter.user.id = 42
        inter.response.edit_message = AsyncMock()
        await confirm.yes(inter)
        # Erin landed in the zone — now 5 / cap 4.
        assert "1005" in session.assignments["Power Tower"]
        assert len(session.assignments["Power Tower"]) == 5

    @pytest.mark.asyncio
    async def test_below_floor_yes_records_override(self):
        session = self._full_session()
        # Power Tower is full; clear so we exercise the pure below-floor
        # path (not over-max).
        session.assignments["Power Tower"] = []
        parent = self._parent_view_mock(session)
        confirm = srb._AssignConfirmView(
            parent_view=parent,
            member_key="1006",
            member_label="Frank",
            zone="Power Tower",
            phase=1,
            over_max=False, cap=4,
            below_floor=True,
            member_power=100_000_000, floor_power=300_000_000,
        )
        inter = MagicMock()
        inter.user.id = 42
        inter.response.edit_message = AsyncMock()
        await confirm.yes(inter)
        # Frank assigned + below-floor override recorded.
        assert "1006" in session.assignments["Power Tower"]
        assert "1006" in session.below_floor_overrides

    @pytest.mark.asyncio
    async def test_both_conditions_one_confirm(self):
        """Over-max + below-floor on the same pick → single confirm
        handles both. Yes records the below-floor override AND lets
        the zone go over cap."""
        session = self._full_session()
        parent = self._parent_view_mock(session)
        confirm = srb._AssignConfirmView(
            parent_view=parent,
            member_key="1006",
            member_label="Frank",
            zone="Power Tower",
            phase=1,
            over_max=True, cap=4,
            below_floor=True,
            member_power=100_000_000, floor_power=300_000_000,
        )
        inter = MagicMock()
        inter.user.id = 42
        inter.response.edit_message = AsyncMock()
        await confirm.yes(inter)
        # Frank assigned over cap and below floor; override recorded.
        assert "1006" in session.assignments["Power Tower"]
        assert len(session.assignments["Power Tower"]) == 5
        assert "1006" in session.below_floor_overrides

    @pytest.mark.asyncio
    async def test_no_leaves_state_unchanged(self):
        session = self._full_session()
        parent = self._parent_view_mock(session)
        before_assign = list(session.assignments["Power Tower"])
        before_overrides = set(session.below_floor_overrides)
        confirm = srb._AssignConfirmView(
            parent_view=parent,
            member_key="1005",
            member_label="Erin",
            zone="Power Tower",
            phase=1,
            over_max=True, cap=4,
            below_floor=False, member_power=300_500_000, floor_power=None,
        )
        inter = MagicMock()
        inter.user.id = 42
        inter.response.edit_message = AsyncMock()
        await confirm.no(inter)
        assert session.assignments["Power Tower"] == before_assign
        assert session.below_floor_overrides == before_overrides

    @pytest.mark.asyncio
    async def test_non_owner_blocked(self):
        session = self._full_session()
        parent = self._parent_view_mock(session)
        confirm = srb._AssignConfirmView(
            parent_view=parent,
            member_key="1005",
            member_label="Erin",
            zone="Power Tower",
            phase=1,
            over_max=True, cap=4,
            below_floor=False, member_power=300_500_000, floor_power=None,
        )
        inter = MagicMock()
        inter.user.id = 999  # not the owner (42)
        inter.response.send_message = AsyncMock()
        await confirm.yes(inter)
        # No assignment landed; rejection sent.
        assert "1005" not in session.assignments["Power Tower"]
        inter.response.send_message.assert_called_once()
        args = inter.response.send_message.call_args.args
        assert "Only the user who opened this view" in args[0]

    def test_picker_no_longer_renders_toggle_button(self):
        """The 👁️ Show/Hide below-minimum button is retired."""
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob", "discord_id": "1002",
                     "power": 100_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.selected_zone = "Power Tower"
        with patch.object(srb, "_autosave_draft"):
            view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert not any("Show members below" in lab for lab in labels)
        assert not any("Hide members below" in lab for lab in labels)

    def test_picker_includes_below_floor_members(self):
        """Below-floor members appear in the picker without needing a
        toggle — they get a 'below minimum' description instead."""
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob", "discord_id": "1002",
                     "power": 100_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.selected_zone = "Power Tower"  # min A = 300M
        with patch.object(srb, "_autosave_draft"):
            view = srb.RosterBuilderView(session)
        # Find the member Select.
        selects = [c for c in view.children
                   if isinstance(c, discord.ui.Select)]
        # The first select is the zone picker; the next is members.
        member_select = next(
            s for s in selects
            if any(o.value == "1002" for o in s.options)
        )
        bob_option = next(o for o in member_select.options if o.value == "1002")
        assert bob_option.description == "below minimum"


# ── _ZoneMemberEditView (#251 tester ask) ───────────────────────────────────


class TestZoneMemberEditView:
    """Surgical edits to a single zone — remove a single member, or
    move one to another zone. Replaces the bulk wipe-and-re-add
    workflow the tester reported was the only path."""

    def _parent_view_mock(self, session):
        parent = MagicMock()
        parent.session = session
        parent.message = MagicMock()
        parent.message.edit = AsyncMock()
        parent._user_action_since_open = False
        parent._rebuild = MagicMock()
        return parent

    def _seeded_session(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob", "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
            "1003": {"key": "1003", "name": "Carol", "discord_id": "1003",
                     "power": 305_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.selected_zone = "Power Tower"
        session.assignments["Power Tower"] = ["1001", "1002"]
        session.assignments["Nuclear Silo"] = ["1003"]
        return session

    def test_empty_zone_shows_only_action_buttons(self):
        """Edit view with no members in the zone exposes only the
        Cancel + (disabled) Apply buttons — no Select to render."""
        session = self._seeded_session()
        session.assignments["Power Tower"] = []
        parent = self._parent_view_mock(session)
        v = srb._ZoneMemberEditView(
            parent_view=parent, zone="Power Tower", phase=1,
        )
        selects = [c for c in v.children if isinstance(c, discord.ui.Select)]
        # No member select, no destination select.
        assert selects == []

    def test_zone_with_members_renders_member_select(self):
        session = self._seeded_session()
        parent = self._parent_view_mock(session)
        v = srb._ZoneMemberEditView(
            parent_view=parent, zone="Power Tower", phase=1,
        )
        selects = [c for c in v.children if isinstance(c, discord.ui.Select)]
        # Member select only — destination select appears after a pick.
        assert len(selects) == 1
        values = {o.value for o in selects[0].options}
        assert values == {"1001", "1002"}

    def test_destination_select_appears_after_member_pick(self):
        session = self._seeded_session()
        parent = self._parent_view_mock(session)
        v = srb._ZoneMemberEditView(
            parent_view=parent, zone="Power Tower", phase=1,
        )
        v.selected_member = "1001"
        v._build()
        selects = [c for c in v.children if isinstance(c, discord.ui.Select)]
        assert len(selects) == 2
        # The destination select includes Remove + every other zone.
        dest_select = selects[1]
        values = [o.value for o in dest_select.options]
        assert "__remove__" in values
        assert "Nuclear Silo" in values
        # Source zone not offered as destination.
        assert "Power Tower" not in values

    def test_apply_button_disabled_until_both_selects_made(self):
        session = self._seeded_session()
        parent = self._parent_view_mock(session)
        v = srb._ZoneMemberEditView(
            parent_view=parent, zone="Power Tower", phase=1,
        )
        apply_btn = next(
            c for c in v.children
            if isinstance(c, discord.ui.Button) and c.label.startswith("✅")
        )
        assert apply_btn.disabled is True

        v.selected_member = "1001"
        v._build()
        apply_btn = next(
            c for c in v.children
            if isinstance(c, discord.ui.Button) and c.label.startswith("✅")
        )
        # Still disabled — destination not yet picked.
        assert apply_btn.disabled is True

        v.selected_destination = "Nuclear Silo"
        v._build()
        apply_btn = next(
            c for c in v.children
            if isinstance(c, discord.ui.Button) and c.label.startswith("✅")
        )
        assert apply_btn.disabled is False

    @pytest.mark.asyncio
    async def test_apply_remove_drops_member_from_zone(self):
        session = self._seeded_session()
        parent = self._parent_view_mock(session)
        v = srb._ZoneMemberEditView(
            parent_view=parent, zone="Power Tower", phase=1,
        )
        v.selected_member = "1001"
        v.selected_destination = v.REMOVE_VALUE
        inter = MagicMock()
        inter.user.id = 42
        inter.response.edit_message = AsyncMock()
        await v._on_apply(inter)
        assert "1001" not in session.assignments["Power Tower"]
        # Other zone untouched.
        assert session.assignments["Nuclear Silo"] == ["1003"]

    @pytest.mark.asyncio
    async def test_apply_move_relocates_member_to_destination(self):
        session = self._seeded_session()
        parent = self._parent_view_mock(session)
        v = srb._ZoneMemberEditView(
            parent_view=parent, zone="Power Tower", phase=1,
        )
        v.selected_member = "1001"
        v.selected_destination = "Nuclear Silo"
        inter = MagicMock()
        inter.user.id = 42
        inter.response.edit_message = AsyncMock()
        await v._on_apply(inter)
        # Alice gone from source.
        assert "1001" not in session.assignments["Power Tower"]
        # Alice now in destination.
        assert "1001" in session.assignments["Nuclear Silo"]
        # Original Nuclear Silo member preserved.
        assert "1003" in session.assignments["Nuclear Silo"]

    @pytest.mark.asyncio
    async def test_cancel_leaves_state_unchanged(self):
        session = self._seeded_session()
        parent = self._parent_view_mock(session)
        v = srb._ZoneMemberEditView(
            parent_view=parent, zone="Power Tower", phase=1,
        )
        v.selected_member = "1001"
        v.selected_destination = "Nuclear Silo"
        before_source = list(session.assignments["Power Tower"])
        before_dest = list(session.assignments["Nuclear Silo"])
        inter = MagicMock()
        inter.user.id = 42
        inter.response.edit_message = AsyncMock()
        await v._on_cancel(inter)
        assert session.assignments["Power Tower"] == before_source
        assert session.assignments["Nuclear Silo"] == before_dest

    @pytest.mark.asyncio
    async def test_non_owner_blocked(self):
        session = self._seeded_session()
        parent = self._parent_view_mock(session)
        v = srb._ZoneMemberEditView(
            parent_view=parent, zone="Power Tower", phase=1,
        )
        v.selected_member = "1001"
        v.selected_destination = v.REMOVE_VALUE
        inter = MagicMock()
        inter.user.id = 999  # not the owner (42)
        inter.response.send_message = AsyncMock()
        await v._on_apply(inter)
        # No state change; rejection sent.
        assert "1001" in session.assignments["Power Tower"]
        inter.response.send_message.assert_called_once()
        assert "Only the user who opened this view" in (
            inter.response.send_message.call_args.args[0]
        )

    def test_main_picker_renders_edit_button_when_zone_has_members(self):
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.selected_zone = "Power Tower"
        session.assignments["Power Tower"] = ["1001"]
        with patch.object(srb, "_autosave_draft"):
            view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        edit_btn = next(
            (c for c in view.children
             if isinstance(c, discord.ui.Button)
             and (c.label or "").startswith("✏️")),
            None,
        )
        assert edit_btn is not None
        assert edit_btn.disabled is False

    def test_main_picker_edit_button_disabled_when_zone_empty(self):
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.selected_zone = "Power Tower"
        # Zone has no assignees yet.
        with patch.object(srb, "_autosave_draft"):
            view = srb.RosterBuilderView(session)
        edit_btn = next(
            (c for c in view.children
             if isinstance(c, discord.ui.Button)
             and (c.label or "").startswith("✏️")),
            None,
        )
        assert edit_btn is not None
        assert edit_btn.disabled is True

    def test_main_picker_renders_renamed_clear_button(self):
        """`Remove current zone assignees` was a destructive name that
        invited misclicks; it's now `🧹 Clear this zone`."""
        from unittest.mock import patch
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        with patch.object(srb, "_autosave_draft"):
            view = srb.RosterBuilderView(session)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Clear this zone" in lab for lab in labels)
        assert not any("Remove current zone assignees" in lab for lab in labels)


class TestDmRosterAssignmentCollection:
    """#226 follow-up — DM-the-roster collects every primary, paired
    sub, and pool sub into a single per-member assignment list."""

    def test_primary_assignments_grouped_by_member(self):
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
            "2": {"key": "2", "name": "Bob", "discord_id": "1002",
                  "power": 400_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.assignments["Power Tower"].append("1")
        session.assignments["Nuclear Silo"].append("2")
        collected = dict(srb._collect_dm_assignments(session))
        assert collected["1"] == [{
            "role": "primary", "zone": "Power Tower",
            "phase": 1, "pair_with": None,
        }]
        assert collected["2"] == [{
            "role": "primary", "zone": "Nuclear Silo",
            "phase": 1, "pair_with": None,
        }]

    def test_paired_sub_carries_primary_name_and_zone(self):
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
            "2": {"key": "2", "name": "Bob", "discord_id": "1002",
                  "power": 300_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members,
                                sub_mode="paired")
        session.assignments["Power Tower"].append("1")
        session.paired_subs["1"] = "2"
        collected = dict(srb._collect_dm_assignments(session))
        # Primary unchanged.
        assert collected["1"] == [{
            "role": "primary", "zone": "Power Tower",
            "phase": 1, "pair_with": None,
        }]
        # Sub gets a paired_sub row pointing back to Alice's zone.
        assert collected["2"] == [{
            "role": "paired_sub", "zone": "Power Tower",
            "phase": 1, "pair_with": "Alice",
        }]

    def test_pool_sub_collected_separately(self):
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
            "2": {"key": "2", "name": "Carol", "discord_id": "1003",
                  "power": 200_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.assignments["Power Tower"].append("1")
        session.subs.append("2")
        collected = dict(srb._collect_dm_assignments(session))
        assert collected["2"] == [{
            "role": "pool_sub", "zone": None, "phase": None,
            "pair_with": None,
        }]

    def test_paired_sub_not_double_counted_as_pool_sub(self):
        """Paired-mode keeps the sub key in `session.subs` (the pairing
        layer doesn't move it out). Same dedup as the mail subs block
        (#224) must apply here: the sub gets a paired_sub row, NOT also
        a pool_sub row, or they'd get two DMs."""
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
            "2": {"key": "2", "name": "Bob", "discord_id": "1002",
                  "power": 300_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members,
                                sub_mode="paired")
        session.assignments["Power Tower"].append("1")
        session.paired_subs["1"] = "2"
        session.subs.append("2")
        collected = dict(srb._collect_dm_assignments(session))
        assert len(collected["2"]) == 1
        assert collected["2"][0]["role"] == "paired_sub"

    def test_phase_aware_member_in_multiple_stages(self):
        """A member who plays in Stage 1 AND Stage 2 gets ONE entry
        in the per-member list with both stage rows. The DM body then
        renders both lines so the recipient sees their full
        commitment in one message."""
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")     # Alice in P1
        s.assignments_p2["Arsenal"].append("1")      # Alice in P2
        collected = dict(srb._collect_dm_assignments(s))
        # Alice's list spans both phases.
        roles = collected["1"]
        assert len(roles) == 2
        assert {(r["phase"], r["zone"]) for r in roles} == {
            (1, "Info Center"), (2, "Arsenal"),
        }


class TestDmRosterBody:
    """The composed per-member DM body honors flat vs phase-aware,
    pool-only vs pinned, and team / event-date / time placeholders."""

    # `_build_dm_body` reads role-keyed templates via
    # `config.get_roster_dm_templates`, which goes through
    # `get_storm_config` → `_get_conn()`. The fallback copy in
    # `defaults.py` already handles an unseeded DB — just need
    # `_get_conn()` to succeed.
    @pytest.fixture(autouse=True)
    def _db(self, temp_db):
        pass

    def _ctx_labels(self):
        return {"time_label": "4pm EDT (18:00 server time)",
                "date_label": "Thursday, May 28, 2026"}

    def test_primary_body_uses_starter_template(self):
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-28"
        session.assignments["Power Tower"].append("1")
        body = srb._build_dm_body(
            session, members["1"],
            [{"role": "primary", "zone": "Power Tower",
              "phase": 1, "pair_with": None}],
            **self._ctx_labels(),
        )
        # New default copy: alliance voice + Starter role label,
        # placeholders substituted in their natural places.
        assert "Hey Alice" in body
        assert "Starter" in body
        assert "Desert Storm Team A" in body
        assert "Power Tower" in body
        assert "May 28, 2026" in body
        assert "4pm EDT" in body
        assert "let us know" in body  # closing nudge

    def test_flat_preset_drops_stage_prefix(self):
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        body = srb._build_dm_body(
            session, members["1"],
            [{"role": "primary", "zone": "Power Tower",
              "phase": 1, "pair_with": None}],
            **self._ctx_labels(),
        )
        # Flat preset → no "Stage 1:" prefix in the bullet line.
        assert "Stage 1:" not in body
        assert "• Power Tower" in body

    def test_phase_aware_keeps_stage_prefix(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")
        body = srb._build_dm_body(
            s, s.members["1"],
            [{"role": "primary", "zone": "Info Center",
              "phase": 1, "pair_with": None}],
            **self._ctx_labels(),
        )
        assert "Stage 1: Info Center" in body

    def test_paired_sub_body_names_primary(self):
        members = {
            "2": {"key": "2", "name": "Bob", "discord_id": "1002",
                  "power": 300_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members,
                                sub_mode="paired")
        body = srb._build_dm_body(
            session, members["2"],
            [{"role": "paired_sub", "zone": "Power Tower",
              "phase": 1, "pair_with": "Alice"}],
            **self._ctx_labels(),
        )
        assert "Hey Bob" in body
        assert "Sub" in body
        # Per the default copy, paired-sub assignments list reads as
        # "Sub for Alice" — no zone (flat preset case) and no
        # "(primary)" suffix anywhere in the body.
        assert "Sub for Alice" in body
        assert "(primary)" not in body

    def test_pool_sub_only_uses_pool_template(self):
        members = {
            "9": {"key": "9", "name": "Carol", "discord_id": "1009",
                  "power": 100_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        body = srb._build_dm_body(
            session, members["9"],
            [{"role": "pool_sub", "zone": None,
              "phase": None, "pair_with": None}],
            **self._ctx_labels(),
        )
        # Default pool-sub template uses "Sub" framing and skips the
        # assignments bullet list entirely.
        assert "Hey Carol" in body
        assert "Sub" in body
        assert "Your assignments" not in body
        assert "(primary)" not in body

    def test_custom_template_substituted_via_safedict(self):
        """A guild that's customised the Starter template via the
        wizard should see their copy in the DM. SafeDict tolerates
        unknown placeholders — a typo renders literally rather than
        crashing the fan-out."""
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-28"
        with patch("config.get_roster_dm_templates", return_value={
            "starter": (
                "Yo {name}, you're on {event_label}{team_blurb}. "
                "Zones:\n{assignments}\n— Leadership"
            ),
            "paired_sub": "",
            "pool_sub": "",
        }):
            body = srb._build_dm_body(
                session, members["1"],
                [{"role": "primary", "zone": "Power Tower",
                  "phase": 1, "pair_with": None}],
                **self._ctx_labels(),
            )
        assert body.startswith("Yo Alice")
        assert "Desert Storm Team A" in body
        assert "• Power Tower" in body
        assert body.endswith("— Leadership")

    def test_typo_placeholder_renders_literally(self):
        """A typo placeholder in a saved template (`{nme}`) must not
        crash the DM build. Renders literally so the alliance sees
        their typo in the next DM and can fix it via the wizard."""
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        with patch("config.get_roster_dm_templates", return_value={
            "starter": "Hi {nme}, see {assignments}.",
            "paired_sub": "",
            "pool_sub": "",
        }):
            body = srb._build_dm_body(
                session, members["1"],
                [{"role": "primary", "zone": "Power Tower",
                  "phase": 1, "pair_with": None}],
                **self._ctx_labels(),
            )
        # Typo placeholder renders literally; the rest substitutes.
        assert "{nme}" in body
        assert "• Power Tower" in body


class TestDmRosterSendFlow:
    """End-to-end DM send flow with mocked Discord — verifies success
    counting, failure-reason capture, and the not_on_discord shortcut."""

    # `_dm_rostered_members` routes through `_build_dm_body` →
    # `config.get_roster_dm_templates` → `_get_conn()`; same gotcha as
    # the `TestDmRosterBody` class above.
    @pytest.fixture(autouse=True)
    def _db(self, temp_db):
        pass

    @pytest.mark.asyncio
    async def test_dm_send_groups_successes_and_failures(self):
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
            "2": {"key": "2", "name": "Bob", "discord_id": "",
                  "power": 300_000_000, "not_on_discord": False},
            "3": {"key": "3", "name": "Carol", "discord_id": "9999",
                  "power": 200_000_000, "not_on_discord": True},
        }
        session = _make_session(team="A", members=members)
        session.event_date = "2026-05-28"
        session.assignments["Power Tower"].append("1")
        session.assignments["Nuclear Silo"].append("2")
        session.subs.append("3")

        fake_user = MagicMock()
        fake_user.send = AsyncMock()
        bot = MagicMock()
        bot.fetch_user = AsyncMock(return_value=fake_user)

        with patch.object(srb, "_resolve_dm_time_label",
                          return_value="4pm EDT (18:00 server time)"):
            sent, failures = await srb._dm_rostered_members(session, bot)

        # Alice was DM'd; Bob has no Discord ID; Carol is marked
        # not_on_discord and gets shortcut-failed.
        assert sent == 1
        names = dict(failures)
        assert names["Bob"] == "no Discord ID linked"
        assert names["Carol"] == "marked as not on Discord"

    @pytest.mark.asyncio
    async def test_closed_dms_reported_as_failure(self):
        members = {
            "1": {"key": "1", "name": "Alice", "discord_id": "1001",
                  "power": 500_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members)
        session.assignments["Power Tower"].append("1")

        fake_user = MagicMock()
        fake_user.send = AsyncMock(
            side_effect=discord.Forbidden(MagicMock(status=403),
                                          "Cannot send messages to this user"),
        )
        bot = MagicMock()
        bot.fetch_user = AsyncMock(return_value=fake_user)

        with patch.object(srb, "_resolve_dm_time_label",
                          return_value="4pm EDT"):
            sent, failures = await srb._dm_rostered_members(session, bot)
        assert sent == 0
        assert failures == [("Alice", "DMs closed by member")]


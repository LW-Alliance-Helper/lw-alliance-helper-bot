"""
Tests for storm_roster_builder.py (#128).

Covers the pure-data helpers: roster power reader, session state,
eligibility filter, rule pre-application. The interactive Discord
view + modal are integration territory and not unit-tested here.
"""

import pytest
from unittest.mock import patch

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
            power_column_name="1st Squad Power",
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

    def test_missing_power_column_surfaces_error(self, fake_env):
        fake, gid = fake_env
        import config
        # Re-save structured config with a power column that doesn't exist.
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_column_name="Nonexistent Column",
        )
        members, errs = srb._read_roster_powers(gid, "DS")
        # Members still read, but every power is None and an error is
        # surfaced for leadership to see.
        assert any("Nonexistent Column" in e for e in errs)
        assert all(m["power"] is None for m in members.values())

    def test_unconfigured_power_column_surfaces_error(self, fake_env):
        fake, gid = fake_env
        import config
        config.save_structured_storm_config(
            gid, "DS",
            structured_flow_enabled=True,
            power_column_name="",  # not set
        )
        members, errs = srb._read_roster_powers(gid, "DS")
        assert any("no power metric column" in e.lower() for e in errs)
        # All powers read as None.
        assert all(m["power"] is None for m in members.values())


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

    def test_unmatched_per_member_rule_surfaces_warning(self):
        """A rule whose subject doesn't match any roster member used to
        silently no-op — leadership had no signal their rule wasn't
        firing. Now it surfaces a soft warning into roster_errors."""
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
        # Stale rule surfaces a warning naming the unmatched subject.
        assert any("OldName" in e for e in session.roster_errors)


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
        session = _make_session(team="A")
        embed = srb._render_builder_embed(session)
        assert "Team A" in embed.title

    def test_renders_floor_for_active_zone(self):
        session = _make_session(team="A")
        session.selected_zone = "Power Tower"
        embed = srb._render_builder_embed(session)
        # 300M is the Min A floor for Power Tower.
        assert "300M" in embed.description

    def test_renders_below_floor_toggle_state(self):
        session = _make_session(team="A")
        session.show_below_floor = True
        embed = srb._render_builder_embed(session)
        assert "below-floor" in embed.description.lower() or \
               "below floor" in embed.description.lower()

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
        return srb._ROSTERS_HEADER.index("Override Below Floor")

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
        assert "Override Below Floor" in srb._ROSTERS_HEADER


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
        # so they slot in via the band relaxation — the count reflects
        # *members slotted via the band*, not *rules whose threshold
        # < preset floor*. Alice (412M) is above preset, so she doesn't
        # count even though the band applies to her zone.
        members = self._three_members()
        power_band = [smr.Rule(
            rule_type="power_band", subject="200000000",
            value="Power Tower",
        )]
        session = _make_session(team="A", members=members, power_band_rules=power_band)
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

    def test_unmatched_per_member_subject_recorded_as_conflict(self):
        """The audit fix: when a per_member subject doesn't match any
        roster member, count it as a conflict so the summary's
        Per-member rules applied count + conflicts count up to the
        total rule count the officer sees in the editor."""
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
        assert any("GhostMember" in c for c in summary["conflicts"])

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


class TestFindJudicatorCandidates:
    """#137 — `_find_judicator_candidates` returns assigned member keys
    whose per_member.special_role rule is `judicator`. Used by the
    CS Apply Faction Roles button to know who gets the role."""

    def test_assigned_judicator_returned(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
        }
        rules = [
            smr.Rule(rule_type="per_member", subject="Alice",
                     sub_type="special_role", value="judicator"),
        ]
        sess = _make_session(team="A", members=members, per_member_rules=rules)
        sess.assignments["Power Tower"].append("1001")
        assert srb._find_judicator_candidates(sess) == ["1001"]

    def test_unassigned_judicator_excluded(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        rules = [
            smr.Rule(rule_type="per_member", subject="Alice",
                     sub_type="special_role", value="judicator"),
        ]
        sess = _make_session(team="A", members=members, per_member_rules=rules)
        # Alice is on the roster but NOT assigned anywhere.
        assert srb._find_judicator_candidates(sess) == []

    def test_paired_subs_included(self):
        """Paired subs are designated participants the moment the
        primary no-shows. Apply Faction Roles fires AFTER matchmaking
        reveals the faction, so a paired sub who's a Judicator
        candidate is a legitimate target — the officer wouldn't click
        the button if the sub hadn't actually taken the slot."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
        }
        rules = [
            smr.Rule(rule_type="per_member", subject="Bob",
                     sub_type="special_role", value="judicator"),
        ]
        sess = _make_session(
            team="A", members=members, per_member_rules=rules, sub_mode="paired",
        )
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        # Bob is the paired sub → included as a candidate.
        assert srb._find_judicator_candidates(sess) == ["1002"]

    def test_flat_sub_pool_excluded(self):
        """Flat sub pool ≠ paired sub. Members in session.subs are
        the bench; they're not designated to take any specific slot,
        so they're not Judicator candidates by virtue of being subs."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
            "1002": {"key": "1002", "name": "Bob",   "discord_id": "1002",
                     "power": 350_000_000, "not_on_discord": False},
        }
        rules = [
            smr.Rule(rule_type="per_member", subject="Bob",
                     sub_type="special_role", value="judicator"),
        ]
        sess = _make_session(team="A", members=members, per_member_rules=rules)
        sess.assignments["Power Tower"].append("1001")
        sess.subs.append("1002")
        # Bob is in the bench pool only, not a designated paired sub.
        assert srb._find_judicator_candidates(sess) == []

    def test_commander_rule_does_not_match(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        rules = [
            smr.Rule(rule_type="per_member", subject="Alice",
                     sub_type="special_role", value="commander"),
        ]
        sess = _make_session(team="A", members=members, per_member_rules=rules)
        sess.assignments["Power Tower"].append("1001")
        # commander != judicator → no candidate
        assert srb._find_judicator_candidates(sess) == []

    def test_discord_id_subject_resolves(self):
        """Per [#136](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/136), per_member subjects may be Discord IDs.
        The candidate finder accepts either form."""
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        rules = [
            smr.Rule(rule_type="per_member", subject="1001",  # discord_id form
                     sub_type="special_role", value="judicator"),
        ]
        sess = _make_session(team="A", members=members, per_member_rules=rules)
        sess.assignments["Power Tower"].append("1001")
        assert srb._find_judicator_candidates(sess) == ["1001"]


class TestFactionRolesPermsPreflight:
    """Audit Major for #137: applying the Judicator role with the bot
    missing Manage Roles, or sitting below the target role in the
    hierarchy, produced N identical Forbidden rows. Now caught up-front
    with a single explanatory ephemeral."""

    def _make_view(self, *, manage_roles: bool, bot_top_pos: int,
                   role_pos: int):
        from unittest.mock import AsyncMock, MagicMock
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        preset = ss.PresetBuffer(
            name="P", event_type="CS", faction="Rulebringers",
            zones=[ss.ZoneRow(zone="Z", max_players=4)],
        )
        sess = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="CS", team="",
            preset=preset, members=members,
            per_member_rules=[], power_band_rules=[],
            event_date="2026-05-18",
        )
        sess.assignments["Z"].append("1001")
        view = srb._FactionRolesView(
            session=sess, judicator_role_id=12345, candidate_keys=["1001"],
        )

        # Fake guild that returns a role with `role_pos`, and a `me`
        # with the given top-role position + perms.
        role = MagicMock()
        role.id = 12345
        role.position = role_pos

        me = MagicMock()
        me.guild_permissions.manage_roles = manage_roles
        me.top_role.position = bot_top_pos

        guild = MagicMock()
        guild.get_role.return_value = role
        guild.me = me

        inter = MagicMock()
        inter.guild = guild
        inter.user = MagicMock()
        inter.user.id = 42  # match session.user_id so the owner-lock passes
        inter.message = MagicMock()
        inter.message.edit = AsyncMock()
        inter.response = MagicMock()
        inter.response.defer = AsyncMock()
        inter.response.send_message = AsyncMock()
        inter.followup = MagicMock()
        inter.followup.send = AsyncMock()
        return view, inter

    @pytest.mark.asyncio
    async def test_missing_manage_roles_aborts_before_loop(self):
        view, inter = self._make_view(
            manage_roles=False, bot_top_pos=100, role_pos=50,
        )
        await view.children[0].callback(inter)  # Rulebringers button
        sent = inter.followup.send.await_args.args[0]
        assert "Manage Roles" in sent
        # We surface ONE clear message, not N per-member errors.
        assert inter.followup.send.await_count == 1

    @pytest.mark.asyncio
    async def test_role_above_bot_aborts_before_loop(self):
        view, inter = self._make_view(
            manage_roles=True, bot_top_pos=50, role_pos=100,
        )
        await view.children[0].callback(inter)
        sent = inter.followup.send.await_args.args[0]
        assert "above my own role" in sent
        assert inter.followup.send.await_count == 1

    @pytest.mark.asyncio
    async def test_role_at_same_position_aborts(self):
        # Discord requires STRICTLY above — equal positions still fail.
        view, inter = self._make_view(
            manage_roles=True, bot_top_pos=50, role_pos=50,
        )
        await view.children[0].callback(inter)
        sent = inter.followup.send.await_args.args[0]
        assert "above my own role" in sent

    @pytest.mark.asyncio
    async def test_apply_summary_says_nothing_to_apply_when_zero_applied(self):
        """The summary header lied — it said '✅ Judicator role applied:'
        even when every candidate was already-had / off-Discord. Now
        gated on `applied` being non-empty."""
        from unittest.mock import AsyncMock, MagicMock
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        preset = ss.PresetBuffer(
            name="P", event_type="CS", faction="Rulebringers",
            zones=[ss.ZoneRow(zone="Z", max_players=4)],
        )
        sess = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="CS", team="",
            preset=preset, members=members,
            per_member_rules=[], power_band_rules=[],
            event_date="2026-05-18",
        )
        sess.assignments["Z"].append("1001")
        view = srb._FactionRolesView(
            session=sess, judicator_role_id=12345, candidate_keys=["1001"],
        )

        role = MagicMock(); role.id = 12345; role.position = 1
        target_member = MagicMock()
        target_member.roles = [role]  # already has it

        me = MagicMock()
        me.guild_permissions.manage_roles = True
        me.top_role.position = 100

        guild = MagicMock()
        guild.get_role.return_value = role
        guild.get_member.return_value = target_member
        guild.me = me

        inter = MagicMock()
        inter.guild = guild
        inter.message = MagicMock()
        inter.message.edit = AsyncMock()
        inter.response = MagicMock()
        inter.response.defer = AsyncMock()
        inter.followup = MagicMock()
        inter.followup.send = AsyncMock()
        inter.user = MagicMock(); inter.user.id = 42  # owner

        await view.children[0].callback(inter)
        sent = inter.followup.send.await_args.args[0]
        # The misleading "✅ applied" header is gone when nothing applied.
        assert "✅ Judicator role applied:" not in sent
        assert "nothing to apply" in sent
        # The "already had" line still surfaces for context.
        assert "Already had the role" in sent


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
        # Rendered as "Alice + sub Bob"
        assert "Alice" in embed.description
        assert "Bob" in embed.description
        assert "sub" in embed.description.lower()

    def test_render_embed_flags_unpaired_primary(self):
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        # No pairing yet.
        embed = srb._render_builder_embed(sess)
        # The ⚠️ flag surfaces in the embed for unpaired primaries.
        assert "⚠️" in embed.description
        assert "Unpaired primaries" in embed.description

    def test_render_embed_paired_mode_surfaces_overflow_subs(self):
        """Paired mode's flat sub list (overflow pool) used to be
        invisible from the embed — only primaries with their inline
        subs rendered. The fix adds an Overflow subs line so the
        officer knows extra subs exist."""
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        # Carol ends up in the overflow pool (every primary already
        # has a paired sub, no zone seat left).
        sess.subs.append("1003")
        embed = srb._render_builder_embed(sess)
        assert "Overflow subs" in embed.description
        assert "Carol" in embed.description

    def test_render_embed_paired_mode_no_overflow_line_when_empty(self):
        """Embed must not render the Overflow subs line when there
        aren't any — keeps the embed compact in the typical case."""
        members = self._three_members()
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.assignments["Power Tower"].append("1001")
        sess.paired_subs["1001"] = "1002"
        # No overflow.
        embed = srb._render_builder_embed(sess)
        assert "Overflow subs" not in embed.description

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
        # CS has one roster per faction per event; team field is "".
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
        sent = inter.followup.send.await_args.args[0]
        assert "Roster posted." in sent
        assert "<#12345>" in sent


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
    """Audit Critical C2: paired subs were invisible in the mail. The
    mail builders only saw `session.subs` (the overflow pool); paired
    subs lived in `session.paired_subs` and never reached the template.
    Fix: `_mail_zone_and_sub_lists` renders "Alice + sub Bob" inline."""

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

    def test_paired_mode_renders_sub_inline(self):
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
        # The paired sub renders INLINE on the primary's line. Mail
        # builders previously never saw "Bob" at all in this state.
        assert zones == {"Power Tower": ["Alice + sub Bob"]}
        # Flat sub list is empty (Bob is paired inline, not in overflow).
        assert subs == []

    def test_paired_mode_unpaired_primary_renders_plain(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members, sub_mode="paired")
        session.assignments["Power Tower"].append("1001")
        # No pairing recorded — primary still renders, no "+ sub" suffix.
        zones, subs = srb._mail_zone_and_sub_lists(session)
        assert zones == {"Power Tower": ["Alice"]}
        assert subs == []

    def test_paired_mode_overflow_in_flat_sub_list(self):
        """When `session.subs` is non-empty in paired mode (auto-fill's
        overflow pool), those names still appear in the mail's sub block."""
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
        assert zones == {"Power Tower": ["Alice + sub Bob"]}
        # Carol is overflow → still surfaces in the flat sub block.
        assert subs == ["Carol"]


class TestRostersTabHeaderMigration:
    """Audit Major M3: existing alliances' rosters_tab kept the 9-column
    header (no `Paired With`); the new writer appends 10-cell rows so
    paired data lands under an unlabeled column and `storm_history`
    can't read it back. Header migration rewrites the row in place."""

    def test_old_header_rewritten_in_place(self, fake_env):
        fake, gid = fake_env
        # Seed an old-shape rosters_tab — the pre-#132 9-column header.
        old_rosters = fake.add_worksheet("DS Rosters")
        old_rosters._rows = [
            ["Event Date", "Team", "Zone", "Member", "Role",
             "Power at Assignment", "Discord ID", "Override Below Floor",
             "Posted At (UTC)"],
            ["2026-05-11", "A", "Power Tower", "Old", "primary",
             "300000000", "1", "", ""],
        ]

        # Build a session and finalise — header should migrate.
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
        # Migrated header matches the canonical shape.
        assert new_header == srb._ROSTERS_HEADER
        # Prior data row preserved (header migration only touches row 1).
        assert old_rosters._rows[1][3] == "Old"


class TestFactionRolesSchemaAndApply:
    """Audit Critical for #137: `CREATE TABLE guild_storm_config` was
    missing the `judicator_role_id` column — only the ALTER TABLE
    migration block added it. Fresh installs would fail on the first
    save. Audit Major: no up-front Manage Roles / hierarchy check.
    """

    def test_create_table_has_judicator_column(self, seeded_db):
        import config
        # Fresh seeded_db went through init_db's CREATE TABLE. The
        # column must be present even without the ALTER fallback firing.
        # (Fresh installs only hit CREATE TABLE.)
        with config._get_conn() as conn:
            cols = [
                row[1] for row in conn.execute(
                    "PRAGMA table_info(guild_storm_config)"
                ).fetchall()
            ]
        assert "judicator_role_id" in cols
        # Defense in depth — the #138 column too, since it had the
        # same gap and was added in the same CREATE TABLE fix.
        assert "power_refresh_dm_enabled" in cols

    def test_judicator_round_trip(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "CS",
            tab_name="CS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "CS",
            structured_flow_enabled=True,
            judicator_role_id=99887766,
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "CS")
        assert cfg["judicator_role_id"] == 99887766

    def test_negative_judicator_role_id_clamped(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "CS",
            tab_name="CS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "CS",
            structured_flow_enabled=True,
            judicator_role_id=-1,
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "CS")
        assert cfg["judicator_role_id"] == 0


class TestAutoFillSummarySplitsPairedFromPrimary:
    """Audit Major M5: `auto_filled_by_power` merged primaries and
    paired subs. A 4-zone build with 4 primaries + 4 paired subs would
    read 'Auto-filled by power: 8' even though only 4 primaries were
    auto-filled. Now `auto_paired_subs` is a separate count."""

    def test_paired_mode_summary_has_separate_paired_count(self):
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
        # 4 primaries auto-filled + 4 paired subs.
        assert summary["auto_filled_by_power"] == 4
        assert summary["auto_paired_subs"] == 4

    def test_pool_mode_paired_count_is_zero(self):
        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        session = _make_session(team="A", members=members, sub_mode="pool")
        summary = srb._auto_fill_session(session)
        # Pool mode never pairs.
        assert summary["auto_paired_subs"] == 0


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


class TestMailBodyPhaseAware:
    def test_flat_mail_has_no_phase_headers(self):
        s = _make_session(team="A", members={
            "1": {"key": "1", "name": "Alice", "discord_id": "1",
                  "power": 412_000_000, "not_on_discord": False},
        })
        s.assignments["Power Tower"].append("1")
        body = srb._build_mail_body(s)
        assert "Phase 1" not in body
        assert "Phase 2" not in body
        assert "Alice" in body

    def test_phase_aware_mail_emits_phase_headers(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")          # Alice in P1
        s.assignments_p2["Arsenal"].append("2")           # Bob in P2
        body = srb._build_mail_body(s)
        assert "**Phase 1**" in body
        assert "**Phase 2**" in body
        # Both members appear, each under its phase block.
        p1_start = body.index("**Phase 1**")
        p2_start = body.index("**Phase 2**")
        assert "Alice" in body[p1_start:p2_start]
        assert "Bob"   in body[p2_start:]

    def test_phase_aware_mail_subs_only_in_phase_one_block(self):
        s = _make_phase_aware_session()
        s.assignments["Info Center"].append("1")
        s.assignments_p2["Arsenal"].append("2")
        s.subs.append("3")
        body = srb._build_mail_body(s)
        # Phase 1 block carries the subs line (Cyrus); phase 2 doesn't
        # double-print them.
        p1_start = body.index("**Phase 1**")
        p2_start = body.index("**Phase 2**")
        p1_block = body[p1_start:p2_start]
        p2_block = body[p2_start:]
        assert "Cyrus" in p1_block
        assert "Cyrus" not in p2_block


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


class TestRostersTabPhaseColumn:
    """The Phase column in rosters_tab is the post-event audit trail
    that captures which phase a member played in. Flat presets write
    "1" for traceability; sub-pool rows write empty (event-level)."""

    def test_header_includes_phase_column(self):
        assert "Phase" in srb._ROSTERS_HEADER
        # Phase sits between Team and Zone so the columns read in the
        # natural left-to-right order an officer scans.
        assert (srb._ROSTERS_HEADER.index("Phase")
                == srb._ROSTERS_HEADER.index("Team") + 1)
        assert (srb._ROSTERS_HEADER.index("Zone")
                == srb._ROSTERS_HEADER.index("Phase") + 1)

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
        phase_col = srb._ROSTERS_HEADER.index("Phase")
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
        phase_col = srb._ROSTERS_HEADER.index("Phase")
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
        phase_col = srb._ROSTERS_HEADER.index("Phase")
        member_col = srb._ROSTERS_HEADER.index("Member")
        carol_row = next(r for r in rows[1:] if r[member_col] == "Carol")
        # Sub-pool entries are event-level, not phase-scoped.
        assert carol_row[phase_col] == ""

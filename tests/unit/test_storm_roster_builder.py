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
        self._rows = [list(r) for r in values]

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
                  per_member_rules=None, power_band_rules=None):
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
        data = [(r[3], r[4], r[5]) for r in rows[1:]]
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
        data_rows = [r for r in rows[1:] if r and r[3] == "Erin"]
        assert data_rows[0][5] == "unknown"

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
        carol_row = next(r for r in rows[1:] if r[3] == "Carol")
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
        alice_row = next(r for r in rows[1:] if r[3] == "Alice")
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
        carol_row = next(r for r in rows[1:] if r[3] == "Carol")
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

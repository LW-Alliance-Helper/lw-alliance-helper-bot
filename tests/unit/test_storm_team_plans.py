"""
Tests for the storm_team_plans CRUD layer (#239).

Covers `config.save_storm_team_plan`, `get_storm_team_plan`,
`get_storm_team_plans_for_event`, and `clear_storm_team_plan`.

The picker UI and roster-builder integration are exercised in
test_storm_officer_view.py and test_storm_roster_builder.py
respectively.
"""

import pytest
import sqlite3

from tests.unit.test_config import TEST_GUILD_ID


class TestSaveAndGetRoundTrip:
    def test_happy_path(self, temp_db):
        import config
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11", "12", "13"],
            subs=["14", "15"],
            saved_by_user_id=999,
        )
        assert ok is True
        assert errors == []
        plan = config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        )
        assert plan is not None
        assert sorted(plan["primaries"]) == ["11", "12", "13"]
        assert sorted(plan["subs"]) == ["14", "15"]
        assert plan["saved_by_user_id"] == 999
        # Saved_at is an ISO 8601 string with the UTC offset suffix.
        assert plan["saved_at"].endswith("+00:00")

    def test_re_save_replaces_no_row_leak(self, temp_db):
        import config
        # First save: 3 primaries, 2 subs.
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11", "12", "13"], subs=["14", "15"],
            saved_by_user_id=999,
        )
        # Re-save: completely different roster.
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["21"], subs=["22"],
            saved_by_user_id=999,
        )
        plan = config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        )
        assert plan["primaries"] == ["21"]
        assert plan["subs"] == ["22"]

    def test_get_returns_none_when_unsaved(self, temp_db):
        import config
        plan = config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        )
        assert plan is None

    def test_on_behalf_name_key_preserved(self, temp_db):
        """On-behalf votes store the roster name as target_member_id;
        the plan must round-trip that key shape unchanged."""
        import config
        ok, _ = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["Alice Smith"], subs=["Bob Jones"],
            saved_by_user_id=999,
        )
        assert ok
        plan = config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        )
        assert plan["primaries"] == ["Alice Smith"]
        assert plan["subs"] == ["Bob Jones"]


class TestValidation:
    def test_rejects_overlap_between_primaries_and_subs(self, temp_db):
        import config
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11", "12"], subs=["12", "13"],
            saved_by_user_id=999,
        )
        assert ok is False
        assert any("primary and sub" in e for e in errors)
        assert config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        ) is None

    def test_rejects_too_many_primaries(self, temp_db):
        import config
        primaries = [str(i) for i in range(21)]  # 21 > 20
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=primaries, subs=[],
            saved_by_user_id=999,
        )
        assert ok is False
        assert any("Too many primaries" in e for e in errors)

    def test_rejects_too_many_subs(self, temp_db):
        import config
        subs = [str(i) for i in range(100, 111)]  # 11 > 10
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["1"], subs=subs,
            saved_by_user_id=999,
        )
        assert ok is False
        assert any("Too many subs" in e for e in errors)

    def test_accepts_full_20_plus_10(self, temp_db):
        import config
        primaries = [str(i) for i in range(20)]
        subs = [str(i) for i in range(100, 110)]
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=primaries, subs=subs,
            saved_by_user_id=999,
        )
        assert ok is True
        assert errors == []

    def test_accepts_partial_plan(self, temp_db):
        """A partial 18+5 plan is allowed — the builder can fall back
        to power-desc for unfilled starter seats."""
        import config
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=[str(i) for i in range(18)],
            subs=[str(i) for i in range(100, 105)],
            saved_by_user_id=999,
        )
        assert ok is True


class TestCrossTeamUniqueness:
    """Players can only be on one team per event (matches the in-game
    rule that a submitted team can't be moved)."""

    def test_save_rejects_member_already_on_other_team(self, temp_db):
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11"], subs=["12"], saved_by_user_id=999,
        )
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "B",
            primaries=["12"], subs=[], saved_by_user_id=999,
        )
        assert ok is False
        assert any("already on the other team" in e.lower() for e in errors)
        assert any("12" in e for e in errors)
        # Team A still intact.
        plan_a = config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        )
        assert plan_a["subs"] == ["12"]
        # Team B never created.
        assert config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "B",
        ) is None

    def test_re_save_same_team_doesnt_self_conflict(self, temp_db):
        """Re-saving Team A with the same roster must not trip the
        cross-team check — it's the same row, same team."""
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11", "12"], subs=["13"], saved_by_user_id=999,
        )
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11"], subs=["12", "13"], saved_by_user_id=999,
        )
        assert ok is True, errors

    def test_db_index_is_belt_and_braces(self, temp_db):
        """If the validator is bypassed (raw SQL), the UNIQUE INDEX
        still blocks a cross-team duplicate."""
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        # Bypass the validator with raw SQL.
        with config._get_conn() as conn:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO storm_team_plans "
                    "(guild_id, event_type, event_date, team, "
                    " target_member_id, role, saved_by_user_id, saved_at) "
                    "VALUES (?, 'DS', '2026-05-21', 'B', '11', 'primary', "
                    " 999, '2026-05-21T00:00:00+00:00')",
                    (TEST_GUILD_ID,),
                )


class TestIsolation:
    def test_per_date_isolation(self, temp_db):
        """A Team A plan on date X doesn't conflict with a Team A plan
        on date Y — same member can be on both because they're
        different events."""
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-28", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        assert ok is True, errors

    def test_per_event_type_isolation(self, temp_db):
        """DS plan doesn't conflict with CS plan on the same date —
        they're independent events."""
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        ok, errors = config.save_storm_team_plan(
            TEST_GUILD_ID, "CS", "2026-05-21", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        assert ok is True, errors

    def test_cs_has_both_a_and_b(self, temp_db):
        """Regression guard against the 'CS is single-team' misread —
        Canyon Storm uses the same A/B model as Desert Storm."""
        import config
        ok_a, _ = config.save_storm_team_plan(
            TEST_GUILD_ID, "CS", "2026-05-21", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        ok_b, _ = config.save_storm_team_plan(
            TEST_GUILD_ID, "CS", "2026-05-21", "B",
            primaries=["21"], subs=[], saved_by_user_id=999,
        )
        assert ok_a is True
        assert ok_b is True
        plan_a = config.get_storm_team_plan(
            TEST_GUILD_ID, "CS", "2026-05-21", "A",
        )
        plan_b = config.get_storm_team_plan(
            TEST_GUILD_ID, "CS", "2026-05-21", "B",
        )
        assert plan_a["primaries"] == ["11"]
        assert plan_b["primaries"] == ["21"]


class TestGetPlansForEvent:
    def test_returns_both_teams(self, temp_db):
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11", "12"], subs=["13"], saved_by_user_id=999,
        )
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "B",
            primaries=["21"], subs=["22"], saved_by_user_id=999,
        )
        plans = config.get_storm_team_plans_for_event(
            TEST_GUILD_ID, "DS", "2026-05-21",
        )
        assert set(plans.keys()) == {"A", "B"}
        assert plans["A"]["primaries"] == ["11", "12"]
        assert plans["B"]["primaries"] == ["21"]

    def test_returns_empty_when_no_plans(self, temp_db):
        import config
        plans = config.get_storm_team_plans_for_event(
            TEST_GUILD_ID, "DS", "2026-05-21",
        )
        assert plans == {}

    def test_returns_only_team_with_plan(self, temp_db):
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        plans = config.get_storm_team_plans_for_event(
            TEST_GUILD_ID, "DS", "2026-05-21",
        )
        assert set(plans.keys()) == {"A"}


class TestClear:
    def test_clear_returns_row_count(self, temp_db):
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11", "12"], subs=["13"], saved_by_user_id=999,
        )
        n = config.clear_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        )
        assert n == 3
        assert config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        ) is None

    def test_clear_no_op_when_unsaved(self, temp_db):
        import config
        n = config.clear_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        )
        assert n == 0

    def test_clear_doesnt_touch_other_team(self, temp_db):
        import config
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "B",
            primaries=["21"], subs=[], saved_by_user_id=999,
        )
        config.clear_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        )
        assert config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
        ) is None
        assert config.get_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "B",
        ) is not None

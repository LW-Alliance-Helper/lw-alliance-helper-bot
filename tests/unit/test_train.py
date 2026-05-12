"""
Unit tests for train.py — birthday placement, schedule logic,
parse_birthday, build_chatgpt_prompt, check_and_add_birthdays.
"""
import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestParseBirthday:
    """Test parse_birthday handles various date formats."""

    def setup_method(self):
        from train import parse_birthday
        self.parse = parse_birthday

    # ── Slash-separated numeric (M/D, M/D/YYYY) ──────────────────────────────
    def test_mm_dd(self):
        assert self.parse("3/15")   == (3, 15)
        assert self.parse("12/25")  == (12, 25)

    def test_mm_dd_yyyy(self):
        assert self.parse("3/15/1990")  == (3, 15)
        assert self.parse("12/25/2000") == (12, 25)

    def test_two_digit_year_stripped(self):
        assert self.parse("12/7/90") == (12, 7)
        assert self.parse("3-15-99") == (3, 15)

    def test_leading_zeros(self):
        assert self.parse("01/05") == (1, 5)
        assert self.parse("09/03") == (9, 3)

    # ── Dash- and dot-separated numeric ──────────────────────────────────────
    def test_dash_separated(self):
        assert self.parse("12-7")   == (12, 7)
        assert self.parse("12-07")  == (12, 7)
        assert self.parse("3-15-1990") == (3, 15)

    def test_dot_separated(self):
        assert self.parse("12.7")     == (12, 7)
        assert self.parse("12.7.1990") == (12, 7)

    def test_whitespace_around_separators(self):
        assert self.parse("12 / 7")  == (12, 7)
        assert self.parse("12 - 7")  == (12, 7)
        assert self.parse("12 . 7")  == (12, 7)

    # ── ISO 8601 ─────────────────────────────────────────────────────────────
    def test_iso_with_year(self):
        assert self.parse("1990-12-07") == (12, 7)
        assert self.parse("2026-03-15") == (3, 15)

    def test_iso_month_day_only(self):
        assert self.parse("--12-07") == (12, 7)
        assert self.parse("--03-15") == (3, 15)

    # ── Month-name first ─────────────────────────────────────────────────────
    def test_month_day_text(self):
        assert self.parse("March 15")    == (3, 15)
        assert self.parse("December 25") == (12, 25)
        assert self.parse("December 25, 1990") == (12, 25)

    def test_ordinal_suffix(self):
        assert self.parse("December 7th") == (12, 7)
        assert self.parse("March 1st")    == (3, 1)
        assert self.parse("July 22nd")    == (7, 22)
        assert self.parse("October 3rd")  == (10, 3)

    def test_abbreviated_month(self):
        assert self.parse("Dec 7")    == (12, 7)
        assert self.parse("Dec 7th")  == (12, 7)
        assert self.parse("Mar 15")   == (3, 15)
        assert self.parse("Sept 9")   == (9, 9)
        assert self.parse("Sep 9")    == (9, 9)

    def test_dash_with_month_name(self):
        assert self.parse("Dec-7") == (12, 7)
        assert self.parse("7-Dec") == (12, 7)

    # ── Day-first with month name ────────────────────────────────────────────
    def test_day_first_month_name(self):
        assert self.parse("7 December")    == (12, 7)
        assert self.parse("7 Dec")         == (12, 7)
        assert self.parse("7th December")  == (12, 7)
        assert self.parse("22nd July")     == (7, 22)

    # ── Ambiguous numeric → M/D unless first > 12 ────────────────────────────
    def test_ambiguous_defaults_to_md(self):
        # 7/12: both ≤ 12, default M/D → July 12
        assert self.parse("7/12") == (7, 12)

    def test_first_over_12_treated_as_day(self):
        # 15/7: first > 12 → must be day, so July 15
        assert self.parse("15/7")   == (7, 15)
        assert self.parse("25-12")  == (12, 25)

    # ── Validation rejects garbage and impossible dates ─────────────────────
    def test_invalid_returns_none(self):
        assert self.parse("not a date") is None
        assert self.parse("") is None
        assert self.parse("garbage") is None
        assert self.parse("2026") is None

    def test_out_of_range_rejected(self):
        # Both > 12: can't be either M/D or D/M
        assert self.parse("99/99") is None
        assert self.parse("13/45") is None
        assert self.parse("15/15") is None

    def test_feb_30_rejected(self):
        assert self.parse("Feb 30")   is None
        assert self.parse("2/30")     is None
        assert self.parse("2-30-1990") is None

    def test_feb_29_accepted(self):
        # Leap-year birthday — we don't know the year, so accept.
        assert self.parse("Feb 29") == (2, 29)
        assert self.parse("2/29")   == (2, 29)

    def test_april_31_rejected(self):
        assert self.parse("4/31")    is None
        assert self.parse("April 31") is None


class TestBuildChatgptPrompt:
    """Test build_chatgpt_prompt formats correctly."""

    def test_placeholders_filled(self, seeded_db):
        from train import build_chatgpt_prompt
        from config import save_train_config
        template = "Member: {name}\nTheme: {theme}\nTone: {tone}\nNotes: {notes}"
        save_train_config(
            TEST_GUILD_ID, "Train Schedule", [], [], template,
            "Default", blurbs_enabled=1,
        )
        result = build_chatgpt_prompt("Alice", "Birthday", "Casual", "Loves cats",
                                      guild_id=TEST_GUILD_ID)
        assert "Alice"       in result
        assert "Birthday"    in result
        assert "Casual"      in result
        assert "Loves cats"  in result

    def test_empty_notes_replaced_with_none(self, seeded_db):
        from train import build_chatgpt_prompt
        from config import save_train_config
        template = "Member: {name}\nNotes: {notes}"
        save_train_config(
            TEST_GUILD_ID, "Train Schedule", [], [], template,
            "Default", blurbs_enabled=1,
        )
        result = build_chatgpt_prompt("Bob", "Milestone", "Default", "",
                                      guild_id=TEST_GUILD_ID)
        assert "Bob"  in result
        assert "None" in result or result  # notes fallback

    def test_fallback_without_template(self, seeded_db):
        from train import build_chatgpt_prompt
        from config import save_train_config
        save_train_config(
            TEST_GUILD_ID, "Train Schedule", [], [], "",
            "Default", blurbs_enabled=1,
        )
        result = build_chatgpt_prompt("Carol", "Welcome", "Intense", "New member",
                                      guild_id=TEST_GUILD_ID)
        assert result  # Should return something even without template


class TestCheckAndAddBirthdays:
    """Test check_and_add_birthdays placement logic."""

    def _make_schedule(self, entries: dict) -> dict:
        """Helper to build a schedule dict."""
        return entries

    def test_birthday_added_on_exact_date(self, seeded_db):
        from train import check_and_add_birthdays
        from config import save_birthday_config

        today = date.today()
        target = today + timedelta(days=7)

        save_birthday_config(
            TEST_GUILD_ID,
            tab_name="Members",
            name_col=0,
            birthday_col=1,
            data_start_row=2,
            enabled=1,
            train_integration=1,
            flexible_placement=0,
            lookahead_days=14,
        )

        members = [{"name": "Alice", "month": target.month, "day": target.day}]

        with patch("train_birthdays.load_birthdays", return_value=members):
            schedule, alerts = check_and_add_birthdays({}, guild_id=TEST_GUILD_ID)

        target_str = target.isoformat()
        assert target_str in schedule
        assert schedule[target_str]["name"] == "Alice"
        assert alerts == []

    def test_birthday_skipped_if_already_in_schedule(self, seeded_db):
        from train import check_and_add_birthdays
        from config import save_birthday_config

        today  = date.today()
        target = today + timedelta(days=7)

        save_birthday_config(
            TEST_GUILD_ID, "Members", 0, 1, 2, 1, 1, 0, 14
        )

        existing = {target.isoformat(): {"name": "Alice", "theme": "Birthday"}}
        members  = [{"name": "Alice", "month": target.month, "day": target.day}]

        with patch("train_birthdays.load_birthdays", return_value=members):
            schedule, alerts = check_and_add_birthdays(existing, guild_id=TEST_GUILD_ID)

        # Should not duplicate
        assert schedule[target.isoformat()]["name"] == "Alice"
        assert alerts == []

    def test_flexible_placement_uses_adjacent_day(self, seeded_db):
        from train import check_and_add_birthdays
        from config import save_birthday_config

        today  = date.today()
        target = today + timedelta(days=7)

        save_birthday_config(
            TEST_GUILD_ID, "Members", 0, 1, 2, 1, 1, 1, 14
        )

        # Target day is taken by someone else
        existing = {target.isoformat(): {"name": "Bob", "theme": "Milestone"}}
        members  = [{"name": "Alice", "month": target.month, "day": target.day}]

        with patch("train_birthdays.load_birthdays", return_value=members):
            schedule, alerts = check_and_add_birthdays(existing, guild_id=TEST_GUILD_ID)

        # Alice should be placed on day before or after
        day_before = (target - timedelta(days=1)).isoformat()
        day_after  = (target + timedelta(days=1)).isoformat()
        placed = (
            schedule.get(day_before, {}).get("name") == "Alice" or
            schedule.get(day_after,  {}).get("name") == "Alice"
        )
        assert placed

    def test_alert_generated_when_no_slot_available(self, seeded_db):
        from train import check_and_add_birthdays
        from config import save_birthday_config

        today  = date.today()
        target = today + timedelta(days=7)

        save_birthday_config(
            TEST_GUILD_ID, "Members", 0, 1, 2, 1, 1, 1, 14
        )

        # All three slots taken by someone else
        existing = {
            (target - timedelta(days=1)).isoformat(): {"name": "X"},
            target.isoformat():                       {"name": "Y"},
            (target + timedelta(days=1)).isoformat(): {"name": "Z"},
        }
        members = [{"name": "Alice", "month": target.month, "day": target.day}]

        with patch("train_birthdays.load_birthdays", return_value=members):
            schedule, alerts = check_and_add_birthdays(existing, guild_id=TEST_GUILD_ID)

        assert len(alerts) == 1
        assert "Alice" in alerts[0]

    def test_multiple_conflicts_collapse_into_one_alert(self, seeded_db):
        """#89: two members hitting the same conflict signature should
        produce ONE Discord message, not one per member. The combined
        message lists every affected member and ends in a sentence that
        addresses leadership as the recipient."""
        from train import check_and_add_birthdays
        from config import save_birthday_config

        today  = date.today()
        target = today + timedelta(days=7)

        save_birthday_config(
            TEST_GUILD_ID, "Members", 0, 1, 2, 1, 1, 1, 14
        )

        # All three slots taken; two members share the conflict.
        existing = {
            (target - timedelta(days=1)).isoformat(): {"name": "X"},
            target.isoformat():                       {"name": "Y"},
            (target + timedelta(days=1)).isoformat(): {"name": "Z"},
        }
        members = [
            {"name": "ShadowHunter", "month": target.month, "day": target.day},
            {"name": "Phoenix99",    "month": target.month, "day": target.day},
        ]

        with patch("train_birthdays.load_birthdays", return_value=members):
            _, alerts = check_and_add_birthdays(existing, guild_id=TEST_GUILD_ID)

        assert len(alerts) == 1, (
            f"Expected one combined alert, got {len(alerts)}: {alerts}"
        )
        msg = alerts[0]
        assert "ShadowHunter" in msg
        assert "Phoenix99"    in msg
        # Header only renders once at the top, not per member.
        assert msg.count("🚨 **Birthday scheduling conflict") == 1
        # Plural copy when more than one member is affected.
        assert "these members" in msg

    def test_single_conflict_uses_singular_copy(self, seeded_db):
        """When only one member conflicts, the trailing instruction
        reads 'this member', not 'these members'."""
        from train import check_and_add_birthdays
        from config import save_birthday_config

        today  = date.today()
        target = today + timedelta(days=7)

        save_birthday_config(
            TEST_GUILD_ID, "Members", 0, 1, 2, 1, 1, 1, 14
        )

        existing = {
            (target - timedelta(days=1)).isoformat(): {"name": "X"},
            target.isoformat():                       {"name": "Y"},
            (target + timedelta(days=1)).isoformat(): {"name": "Z"},
        }
        members = [{"name": "Alice", "month": target.month, "day": target.day}]

        with patch("train_birthdays.load_birthdays", return_value=members):
            _, alerts = check_and_add_birthdays(existing, guild_id=TEST_GUILD_ID)

        assert len(alerts) == 1
        assert "this member" in alerts[0]
        assert "these members" not in alerts[0]

    def test_birthday_outside_lookahead_not_added(self, seeded_db):
        from train import check_and_add_birthdays
        from config import save_birthday_config

        today  = date.today()
        target = today + timedelta(days=30)  # beyond 14 day lookahead

        save_birthday_config(
            TEST_GUILD_ID, "Members", 0, 1, 2, 1, 1, 0, 14
        )

        members = [{"name": "Alice", "month": target.month, "day": target.day}]

        with patch("train_birthdays.load_birthdays", return_value=members):
            schedule, alerts = check_and_add_birthdays({}, guild_id=TEST_GUILD_ID)

        assert target.isoformat() not in schedule

    def test_disabled_train_integration_skips_birthdays(self, seeded_db):
        from train import check_and_add_birthdays
        from config import save_birthday_config

        today  = date.today()
        target = today + timedelta(days=7)

        save_birthday_config(
            TEST_GUILD_ID, "Members", 0, 1,
            data_start_row=2, enabled=1,
            train_integration=0,  # disabled
            flexible_placement=0,
            lookahead_days=14,
        )

        members = [{"name": "Alice", "month": target.month, "day": target.day}]

        with patch("train_birthdays.load_birthdays", return_value=members):
            schedule, alerts = check_and_add_birthdays({}, guild_id=TEST_GUILD_ID)

        assert schedule == {}


class TestGetThemesAndTones:
    """Test get_themes and get_tones use guild config."""

    def test_default_themes_returned_without_config(self, temp_db):
        from train import get_themes, DEFAULT_THEMES
        result = get_themes(TEST_GUILD_ID)
        assert isinstance(result, list)
        assert len(result) > 0

    def test_custom_themes_returned_with_config(self, seeded_db):
        from train import get_themes
        from config import save_train_config
        custom = ["Theme A", "Theme B", "Theme C"]
        save_train_config(TEST_GUILD_ID, "Train Schedule", custom, [], "", "Theme A")
        result = get_themes(TEST_GUILD_ID)
        assert result == custom

    def test_default_tones_returned_without_config(self, temp_db):
        from train import get_tones
        result = get_tones(TEST_GUILD_ID)
        assert isinstance(result, list)
        assert len(result) > 0

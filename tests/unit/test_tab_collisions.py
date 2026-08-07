"""
Unit tests for #441 — two features pointed at the same sheet tab.

Around a dozen tab names are configurable across separate wizards, and
nothing checked whether the name being saved was already somebody else's.
Point Growth Tracking at a survey's stats tab and the monthly snapshot
writes metric columns straight into it.

The rule these encode: warn, don't reject. Some overlaps are deliberate
(the Buddy System reads profession off the survey's stats tab on
purpose), so a blanket rejection would be wrong.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestTabsInUse:
    def test_reports_the_feature_behind_each_configured_tab(self, seeded_db):
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO guild_growth_config (guild_id, tab_growth, tab_breakdown) "
                "VALUES (?, ?, ?)",
                (TEST_GUILD_ID, "Growth Tracking", "Growth Breakdown"),
            )
            conn.commit()

        claimed = config.tabs_in_use(TEST_GUILD_ID)
        assert claimed["growth tracking"] == "your Growth Tracking snapshots"
        assert claimed["growth breakdown"] == "your Growth Breakdown"

    def test_labels_name_the_feature_not_the_column(self, seeded_db):
        """Leadership recognises 'your Birthdays list', not 'tab_name'."""
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO guild_birthday_config (guild_id, tab_name) VALUES (?, ?)",
                (TEST_GUILD_ID, "Birthdays"),
            )
            conn.commit()

        assert config.tabs_in_use(TEST_GUILD_ID)["birthdays"] == "your Birthdays list"

    def test_the_field_being_edited_is_excluded(self, seeded_db):
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO guild_growth_config (guild_id, tab_growth) VALUES (?, ?)",
                (TEST_GUILD_ID, "Growth Tracking"),
            )
            conn.commit()

        claimed = config.tabs_in_use(TEST_GUILD_ID, exclude_field="tab_growth")
        assert "growth tracking" not in claimed

    def test_the_buddy_profession_tab_is_never_reported(self, seeded_db):
        """It points at the survey's stats tab on purpose. Reporting a
        deliberate overlap trains people to click past the warning."""
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO guild_buddy_config "
                "(guild_id, buddy_tab, profession_tab) VALUES (?, ?, ?)",
                (TEST_GUILD_ID, "Buddy System", "Squad Powers"),
            )
            conn.commit()

        claimed = config.tabs_in_use(TEST_GUILD_ID)
        assert claimed.get("squad powers") != "your Buddy System list"
        assert claimed["buddy system"] == "your Buddy System list"

    def test_surveys_are_named_individually(self, seeded_db):
        import config

        config.save_extra_survey(
            TEST_GUILD_ID,
            "intake",
            survey_name="New Member Intake",
            tab_squad_powers="New Member Intake",
            tab_history="New Member Intake History",
        )

        claimed = config.tabs_in_use(TEST_GUILD_ID)
        assert claimed["new member intake"] == "the New Member Intake survey"

    def test_a_survey_can_be_excluded_from_its_own_check(self, seeded_db):
        import config

        config.save_extra_survey(
            TEST_GUILD_ID,
            "intake",
            survey_name="New Member Intake",
            tab_squad_powers="New Member Intake",
            tab_history="New Member Intake History",
        )

        claimed = config.tabs_in_use(TEST_GUILD_ID, exclude_survey_id="intake")
        assert "new member intake" not in claimed

    def test_storm_tabs_name_their_event(self, seeded_db):
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO guild_storm_config (guild_id, event_type, tab_name) "
                "VALUES (?, ?, ?)",
                (TEST_GUILD_ID, "CS", "CS Assignments"),
            )
            conn.commit()

        assert "Canyon Storm" in config.tabs_in_use(TEST_GUILD_ID)["cs assignments"]


class TestWarnIfTabClaimed:
    @pytest.mark.asyncio
    async def test_warns_and_names_the_other_feature(self):
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        with patch("config.tabs_in_use", return_value={"shared": "your Growth Breakdown"}):
            warned = await setup_cog.warn_if_tab_claimed(
                channel, TEST_GUILD_ID, "Shared", exclude_field="tab_growth"
            )

        assert warned is True
        msg = channel.send.call_args[0][0]
        assert "your Growth Breakdown" in msg
        assert "Shared" in msg

    @pytest.mark.asyncio
    async def test_says_nothing_when_the_tab_is_free(self):
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        with patch("config.tabs_in_use", return_value={}):
            warned = await setup_cog.warn_if_tab_claimed(
                channel, TEST_GUILD_ID, "Fresh Tab", exclude_field="tab_growth"
            )

        assert warned is False
        channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_it_warns_rather_than_blocking(self):
        """Deliberate overlaps exist, so the step has to keep going."""
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        with patch("config.tabs_in_use", return_value={"shared": "your Member Roster"}):
            result = await setup_cog.warn_if_tab_claimed(
                channel, TEST_GUILD_ID, "Shared", exclude_field="tab_growth"
            )

        assert result is True  # reported, not raised

    @pytest.mark.asyncio
    async def test_a_broken_lookup_never_ends_a_wizard(self):
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        with patch("config.tabs_in_use", side_effect=Exception("db gone")):
            warned = await setup_cog.warn_if_tab_claimed(
                channel, TEST_GUILD_ID, "Anything", exclude_field="tab_growth"
            )

        assert warned is False
        channel.send.assert_not_awaited()


class TestFollowSurveyTabRename:
    """Renaming the survey's stats tab used to strand the Buddy System,
    which reads profession off it by name from a different wizard."""

    def _set_buddy(self, profession_tab):
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO guild_buddy_config "
                "(guild_id, buddy_tab, profession_tab) VALUES (?, ?, ?)",
                (TEST_GUILD_ID, "Buddy System", profession_tab),
            )
            conn.commit()

    def _buddy_tab(self):
        import config

        with config._get_conn() as conn:
            return conn.execute(
                "SELECT profession_tab FROM guild_buddy_config WHERE guild_id = ?",
                (TEST_GUILD_ID,),
            ).fetchone()[0]

    def test_buddy_follows_the_rename(self, seeded_db):
        import config

        self._set_buddy("Squad Powers")
        assert config.follow_survey_tab_rename(TEST_GUILD_ID, "Squad Powers", "Alliance Stats")
        assert self._buddy_tab() == "Alliance Stats"

    def test_buddy_pointed_somewhere_else_is_left_alone(self, seeded_db):
        """Only a buddy config that was tracking *this* tab should move."""
        import config

        self._set_buddy("Some Other Tab")
        assert not config.follow_survey_tab_rename(TEST_GUILD_ID, "Squad Powers", "Alliance Stats")
        assert self._buddy_tab() == "Some Other Tab"

    def test_no_rename_is_a_no_op(self, seeded_db):
        import config

        self._set_buddy("Squad Powers")
        assert not config.follow_survey_tab_rename(TEST_GUILD_ID, "Squad Powers", "Squad Powers")
        assert not config.follow_survey_tab_rename(TEST_GUILD_ID, "", "Alliance Stats")
        assert self._buddy_tab() == "Squad Powers"

    def test_matching_ignores_case(self, seeded_db):
        import config

        self._set_buddy("squad powers")
        assert config.follow_survey_tab_rename(TEST_GUILD_ID, "Squad Powers", "Alliance Stats")
        assert self._buddy_tab() == "Alliance Stats"

    def test_a_guild_with_no_buddy_config_is_fine(self, seeded_db):
        import config

        with config._get_conn() as conn:
            conn.execute("DELETE FROM guild_buddy_config WHERE guild_id = ?", (TEST_GUILD_ID,))
            conn.commit()

        assert not config.follow_survey_tab_rename(TEST_GUILD_ID, "Squad Powers", "Alliance Stats")

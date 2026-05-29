"""
Integration tests for the survey submission flow.
Tests run_survey with mocked Discord thread, verifying correct
questions are asked and responses routed to update_squad_powers
and append_survey_history.
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, make_mock_channel


def make_message(content: str, author=None, channel=None):
    msg = MagicMock()
    msg.content = content
    msg.author = author or MagicMock()
    msg.channel = channel or make_mock_channel()
    return msg


class TestSurveyFlow:
    """Test the full survey thread flow with mocked sheets."""

    def _make_thread(self):
        thread = AsyncMock()
        thread.send = AsyncMock(return_value=MagicMock(id=999))
        return thread

    def _make_user(self, guild_id=TEST_GUILD_ID):
        user = MagicMock()
        user.id = 123456789
        user.display_name = "Alice"
        guild = MagicMock()
        guild.id = guild_id
        user.guild = guild
        return user

    @pytest.mark.asyncio
    async def test_survey_asks_all_configured_questions(self, seeded_db):
        """Verify run_survey iterates all configured questions."""
        from survey import run_survey
        from config import save_survey_config

        questions = [
            {
                "key": "q1",
                "label": "Squad Power",
                "type": "text",
                "options": [],
                "placeholder": "e.g. 43.27",
                "max_chars": 5,
            },
            {
                "key": "q2",
                "label": "Role",
                "type": "dropdown",
                "options": ["War Leader", "Engineer"],
                "placeholder": "",
                "max_chars": 0,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        thread = self._make_thread()
        user = self._make_user()
        bot = AsyncMock()
        # get_channel is sync on real discord.Bot — return None so the
        # leadership-notify branch in run_survey skips cleanly.
        bot.get_channel = MagicMock(return_value=None)

        # Simulate user replies
        replies = iter(["43.27"])

        async def fake_wait_for(event, check=None, timeout=None):
            return make_message(next(replies, "done"), author=user, channel=thread)

        bot.wait_for = AsyncMock(side_effect=fake_wait_for)

        # Dropdown responds via DropdownView
        mock_dropdown = MagicMock()
        mock_dropdown.selected = "War Leader"
        mock_dropdown.confirmed = True
        mock_dropdown.wait = AsyncMock()

        saved_powers = {}
        saved_history = {}

        def fake_update(discord_id, username, data, guild_id=None, survey=None):
            saved_powers.update(data)

        def fake_history(discord_id, username, data, guild_id=None, survey=None):
            saved_history.update(data)

        with (
            patch("survey.DropdownView", return_value=mock_dropdown),
            patch("survey.update_squad_powers", side_effect=fake_update),
            patch("survey.append_survey_history", side_effect=fake_history),
            patch("survey._finalize_survey_thread", new_callable=AsyncMock),
        ):
            await run_survey(bot, thread, user)

        assert saved_powers.get("q1") == "43.27"
        assert saved_powers.get("q2") == "War Leader"
        assert saved_history.get("q1") == "43.27"

    @pytest.mark.asyncio
    async def test_survey_no_questions_shows_error(self, seeded_db):
        """If no questions configured, survey should tell user to contact leadership."""
        from survey import run_survey
        from config import save_survey_config

        save_survey_config(TEST_GUILD_ID, "Stats", "History", [], "")

        thread = self._make_thread()
        user = self._make_user()
        bot = AsyncMock()
        # get_channel is sync on real discord.Bot — return None so the
        # leadership-notify branch in run_survey skips cleanly.
        bot.get_channel = MagicMock(return_value=None)

        with (
            patch("survey.update_squad_powers") as mock_up,
            patch("survey.append_survey_history") as mock_ah,
        ):
            await run_survey(bot, thread, user)
            mock_up.assert_not_called()
            mock_ah.assert_not_called()

        # Should have sent an error message
        thread.send.assert_called()
        sent_msgs = [c.args[0] if c.args else str(c.kwargs) for c in thread.send.call_args_list]
        assert any(
            "Survey" in m or "No survey" in m or "not configured" in m.lower() for m in sent_msgs
        )

    @pytest.mark.asyncio
    async def test_survey_timeout_exits_gracefully(self, seeded_db):
        """If user times out mid-survey, flow exits without writing."""
        from survey import run_survey
        from config import save_survey_config

        questions = [
            {
                "key": "q1",
                "label": "Power",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 5,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        thread = self._make_thread()
        user = self._make_user()
        bot = AsyncMock()
        # get_channel is sync on real discord.Bot — return None so the
        # leadership-notify branch in run_survey skips cleanly.
        bot.get_channel = MagicMock(return_value=None)

        bot.wait_for = AsyncMock(side_effect=asyncio.TimeoutError)

        with (
            patch("survey.update_squad_powers") as mock_up,
            patch("survey.append_survey_history") as mock_ah,
        ):
            await run_survey(bot, thread, user)
            mock_up.assert_not_called()
            mock_ah.assert_not_called()

    @pytest.mark.asyncio
    async def test_survey_text_max_chars_enforced(self, seeded_db):
        """
        If user keeps typing more than max_chars, the survey eventually
        bails (after the retry budget is exhausted) rather than saving.
        """
        from survey import run_survey
        from config import save_survey_config

        questions = [
            {
                "key": "q1",
                "label": "Power",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 3,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        thread = self._make_thread()
        user = self._make_user()
        bot = AsyncMock()
        # get_channel is sync on real discord.Bot — return None so the
        # leadership-notify branch in run_survey skips cleanly.
        bot.get_channel = MagicMock(return_value=None)

        # Every reply is too long — survey burns through all retries
        bot.wait_for = AsyncMock(
            return_value=make_message("TOOLONGVALUE", author=user, channel=thread)
        )

        with (
            patch("survey.update_squad_powers") as mock_up,
            patch("survey.append_survey_history") as mock_ah,
        ):
            await run_survey(bot, thread, user)
            mock_up.assert_not_called()

    @pytest.mark.asyncio
    async def test_survey_text_max_chars_recovers_on_retry(self, seeded_db):
        """
        A user who fat-fingers a too-long value should be re-prompted for
        the *same* question rather than have the whole survey cancel.
        Regression guard for the THP-millions slip reported by an OGV
        member (typed `153,725,881` instead of `154`).
        """
        from survey import run_survey
        from config import save_survey_config

        questions = [
            {
                "key": "thp",
                "label": "Total Hero Power (THP)",
                "type": "text",
                "options": [],
                "placeholder": "e.g. 301",
                "max_chars": 3,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        thread = self._make_thread()
        user = self._make_user()
        bot = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)

        # First reply is too long; second is valid → survey should recover
        replies = iter(["153,725,881", "154"])

        async def fake_wait_for(*a, **kw):
            return make_message(next(replies), author=user, channel=thread)

        bot.wait_for = AsyncMock(side_effect=fake_wait_for)

        captured = {}

        def fake_update(discord_id, username, data, guild_id=None, survey=None):
            captured.update(data)

        with (
            patch("survey.update_squad_powers", side_effect=fake_update) as mock_up,
            patch("survey.append_survey_history"),
            patch("survey._finalize_survey_thread", new_callable=AsyncMock),
        ):
            await run_survey(bot, thread, user)

        # The valid second answer landed in the saved data
        assert captured.get("thp") == "154"
        mock_up.assert_called_once()

        # User saw the per-question retry prompt, not the "try the survey
        # again" cancel-the-whole-thing message
        sent_text = " ".join(
            (c.args[0] if c.args else c.kwargs.get("content", ""))
            for c in thread.send.call_args_list
            if c.args and isinstance(c.args[0], str)
        )
        assert "re-enter your answer for this question" in sent_text
        assert "try the survey again" not in sent_text

    @pytest.mark.asyncio
    async def test_survey_all_text_questions(self, seeded_db):
        """Survey with only text questions completes without dropdown."""
        from survey import run_survey
        from config import save_survey_config

        questions = [
            {
                "key": "p1",
                "label": "1st Power",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 5,
            },
            {
                "key": "p2",
                "label": "2nd Power",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 5,
            },
            {
                "key": "p3",
                "label": "3rd Power",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 5,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        thread = self._make_thread()
        user = self._make_user()
        bot = AsyncMock()
        # get_channel is sync on real discord.Bot — return None so the
        # leadership-notify branch in run_survey skips cleanly.
        bot.get_channel = MagicMock(return_value=None)

        replies = iter(["43.27", "38.50", "35.00"])

        async def fake_wait_for(*a, **kw):
            return make_message(next(replies, "0"), author=user, channel=thread)

        bot.wait_for = AsyncMock(side_effect=fake_wait_for)

        captured = {}

        def fake_update(discord_id, username, data, guild_id=None, survey=None):
            captured.update(data)

        with (
            patch("survey.update_squad_powers", side_effect=fake_update),
            patch("survey.append_survey_history"),
            patch("survey._finalize_survey_thread", new_callable=AsyncMock),
        ):
            await run_survey(bot, thread, user)

        assert captured.get("p1") == "43.27"
        assert captured.get("p2") == "38.50"
        assert captured.get("p3") == "35.00"

    @pytest.mark.asyncio
    async def test_survey_sheet_error_handled_gracefully(self, seeded_db):
        """If sheet write fails, user sees error message but bot doesn't crash."""
        from survey import run_survey
        from config import save_survey_config

        questions = [
            {
                "key": "q1",
                "label": "Power",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 5,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        thread = self._make_thread()
        user = self._make_user()
        bot = AsyncMock()
        # get_channel is sync on real discord.Bot — return None so the
        # leadership-notify branch in run_survey skips cleanly.
        bot.get_channel = MagicMock(return_value=None)

        bot.wait_for = AsyncMock(return_value=make_message("43.27", author=user, channel=thread))

        with patch("survey.update_squad_powers", side_effect=Exception("Sheet permission denied")):
            # Should not raise — error should be caught
            await run_survey(bot, thread, user)

        # Should have sent an error message to the thread
        sent = [str(c) for c in thread.send.call_args_list]
        assert any("error" in s.lower() or "⚠️" in s for s in sent)

"""
Unit tests for survey.py — question config, dynamic column mapping,
update_squad_powers, append_survey_history (with mocked sheets).
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock, call
import sys, os

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestParseMagnitudeInput:
    """`_parse_magnitude_input` accepts the shapes players naturally type for
    in-game stats (THP, squad power, kills) and normalises them to the field's
    declared magnitude. Bare numbers ≥ 1M short-circuit the heuristic so a
    player who pastes the full in-game number gets it stored as-is."""

    @pytest.mark.parametrize(
        "raw, magnitude, expected",
        [
            # Bare shorthand — multiplied by the field's magnitude
            ("301", "M", 301_000_000),
            ("43.27", "M", 43_270_000),
            ("1.2", "B", 1_200_000_000),
            ("5", "K", 5_000),
            ("150", "raw", 150),
            ("70", None, 70),
            # Explicit suffix — overrides whatever the field's magnitude is
            ("300m", "M", 300_000_000),
            ("300M", "M", 300_000_000),
            ("300mil", "M", 300_000_000),
            ("300mill", "M", 300_000_000),
            ("300million", "M", 300_000_000),
            ("300m", "raw", 300_000_000),  # suffix wins over raw too
            ("300m", None, 300_000_000),
            ("1.2b", "M", 1_200_000_000),
            ("1.2B", "M", 1_200_000_000),
            ("1.2billion", "M", 1_200_000_000),
            ("5k", "M", 5_000),
            ("5K", "M", 5_000),
            # Comma grouping
            ("304,743,912", "M", 304_743_912),
            ("1,000,000", "M", 1_000_000),  # ≥1M heuristic — stored as-is
            ("999999", "M", 999_999_000_000),  # just below threshold → scaled
            # Whitespace tolerance
            ("  301  ", "M", 301_000_000),
            ("300 m", "M", 300_000_000),
            # ≥1M heuristic — raw numbers don't get re-multiplied
            ("304743912", "M", 304_743_912),
            ("1500000", "K", 1_500_000),  # 1.5M as bare ≥ threshold → raw
        ],
    )
    def test_valid_inputs(self, raw, magnitude, expected):
        from survey import _parse_magnitude_input

        assert _parse_magnitude_input(raw, magnitude) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "abc",
            "300xyz",  # unrecognised suffix
            "1.2.3",  # not a number
            None,
        ],
    )
    def test_invalid_inputs_return_none(self, raw):
        from survey import _parse_magnitude_input

        assert _parse_magnitude_input(raw, "M") is None

    def test_returns_int_even_for_decimal_input(self):
        """Stored values are always integers — `43.27` × 1M is `43_270_000`,
        not `43_270_000.0`. The Sheets writer relies on integer typing."""
        from survey import _parse_magnitude_input

        result = _parse_magnitude_input("43.27", "M")
        assert isinstance(result, int)
        assert result == 43_270_000

    def test_unknown_magnitude_treated_as_raw(self):
        """A typo or stale magnitude value should pass-through, not crash."""
        from survey import _parse_magnitude_input

        assert _parse_magnitude_input("301", "raw") == 301
        assert _parse_magnitude_input("301", "bogus") == 301


class TestFormatResponseValue:
    """`_fmt_response_value` formats stored numeric responses for the
    leadership notification embed — `304743912` becomes `304,743,912` so
    leadership doesn't have to mentally insert commas every submission."""

    def test_comma_formats_numeric_int_string(self):
        from survey import _fmt_response_value

        assert _fmt_response_value("304743912", "numeric") == "304,743,912"

    def test_comma_formats_numeric_float_string(self):
        from survey import _fmt_response_value

        assert _fmt_response_value("1234.5", "numeric") == "1,234.5"

    def test_passes_through_dropdown_value(self):
        from survey import _fmt_response_value

        assert _fmt_response_value("Missile", "dropdown") == "Missile"

    def test_passes_through_text_value(self):
        from survey import _fmt_response_value

        assert _fmt_response_value("43.27", "text") == "43.27"

    def test_empty_renders_as_em_dash(self):
        from survey import _fmt_response_value

        assert _fmt_response_value("", "numeric") == "—"
        assert _fmt_response_value(None, "numeric") == "—"

    def test_unparseable_numeric_passes_through(self):
        """A stored value that can't be parsed (legacy data, garbage entry)
        should render as-is rather than crashing the embed."""
        from survey import _fmt_response_value

        assert _fmt_response_value("not-a-number", "numeric") == "not-a-number"


class TestSurveyQuestionConfig:
    """Test that survey questions save and load correctly."""

    def test_default_questions_have_required_keys(self, temp_db):
        from defaults import DEFAULT_SURVEY_QUESTIONS

        required_keys = {"key", "label", "type", "options", "placeholder"}
        for q in DEFAULT_SURVEY_QUESTIONS:
            for k in required_keys:
                assert k in q, f"Question missing key '{k}': {q}"

    def test_dropdown_questions_have_options(self, temp_db):
        from defaults import DEFAULT_SURVEY_QUESTIONS

        for q in DEFAULT_SURVEY_QUESTIONS:
            if q["type"] == "dropdown":
                assert len(q["options"]) > 0, f"Dropdown has no options: {q['label']}"

    def test_text_questions_have_empty_options(self, temp_db):
        from defaults import DEFAULT_SURVEY_QUESTIONS

        for q in DEFAULT_SURVEY_QUESTIONS:
            if q["type"] == "text":
                assert q["options"] == [], f"Text question has options: {q['label']}"

    def test_numeric_questions_declare_magnitude(self, temp_db):
        """Every numeric question in the default set must declare a magnitude
        — otherwise the parser falls back to raw int/float and players' shorthand
        (`301` for 301M) silently stops being scaled."""
        from defaults import DEFAULT_SURVEY_QUESTIONS

        for q in DEFAULT_SURVEY_QUESTIONS:
            if q["type"] == "numeric":
                assert "magnitude" in q, f"Numeric question missing magnitude: {q['label']}"
                assert q["magnitude"] in ("raw", "K", "M", "B"), (
                    f"Invalid magnitude '{q['magnitude']}' on {q['label']}"
                )

    def test_lw_power_questions_use_millions(self, temp_db):
        """Squad power, THP, and total kills are entered as shorthand-in-millions
        in Last War conversation — the defaults need to scale them accordingly."""
        from defaults import DEFAULT_SURVEY_QUESTIONS

        m_keys = {"squad1_power", "squad2_power", "squad3_power", "thp", "total_kills"}
        seen = {q["key"] for q in DEFAULT_SURVEY_QUESTIONS if q.get("magnitude") == "M"}
        assert m_keys.issubset(seen), f"Missing magnitude=M on: {m_keys - seen}"

    def test_all_questions_have_unique_keys(self, temp_db):
        from defaults import DEFAULT_SURVEY_QUESTIONS

        keys = [q["key"] for q in DEFAULT_SURVEY_QUESTIONS]
        assert len(keys) == len(set(keys)), "Duplicate question keys found"

    def test_custom_questions_saved_and_loaded(self, seeded_db):
        from config import save_survey_config, get_survey_config

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
                "options": ["Attacker", "Defender"],
                "placeholder": "",
                "max_chars": 0,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "Welcome!")
        cfg = get_survey_config(TEST_GUILD_ID)
        assert len(cfg["questions"]) == 2
        assert cfg["questions"][0]["label"] == "Squad Power"
        assert cfg["questions"][1]["options"] == ["Attacker", "Defender"]


class TestUpdateSquadPowers:
    """Test update_squad_powers writes correct columns to sheet."""

    def _make_mock_ws(self, existing_rows=None):
        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=existing_rows or [])
        ws.row_count = len(existing_rows or []) + 1
        ws.update = MagicMock()
        ws.append_row = MagicMock()
        ws.row_values = MagicMock(return_value=[])
        return ws

    def test_new_member_appended(self, seeded_db):
        from survey import update_squad_powers
        from config import save_survey_config
        from defaults import DEFAULT_SURVEY_QUESTIONS

        save_survey_config(TEST_GUILD_ID, "Squad Powers", "History", DEFAULT_SURVEY_QUESTIONS, "")

        mock_ws = self._make_mock_ws([])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        data = {"squad1_power": "43.27", "squad1_type": "Missile"}

        with patch("survey._get_spreadsheet", return_value=mock_sh):
            update_squad_powers("123456", "Alice", data, guild_id=TEST_GUILD_ID)

        mock_ws.append_row.assert_called_once()
        call_args = mock_ws.append_row.call_args[0][0]
        assert "Alice" in call_args
        assert "123456" in call_args

    def test_existing_member_updated_not_duplicated(self, seeded_db):
        from survey import update_squad_powers
        from config import save_survey_config
        from defaults import DEFAULT_SURVEY_QUESTIONS

        save_survey_config(TEST_GUILD_ID, "Squad Powers", "History", DEFAULT_SURVEY_QUESTIONS, "")

        # Existing row: Username, Discord ID, ...
        existing = [
            ["Username", "Discord ID", "1st Squad Power"],
            ["Alice", "123456", "40.00"],
        ]
        mock_ws = self._make_mock_ws(existing)
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        data = {"squad1_power": "43.27"}

        with patch("survey._get_spreadsheet", return_value=mock_sh):
            update_squad_powers("123456", "Alice", data, guild_id=TEST_GUILD_ID)

        # Should call update, not append_row
        mock_ws.update.assert_called()
        mock_ws.append_row.assert_not_called()

    def test_dynamic_columns_match_question_keys(self, seeded_db):
        """Verify the written row includes values for each question key."""
        from survey import update_squad_powers
        from config import save_survey_config

        questions = [
            {
                "key": "metric_a",
                "label": "Metric A",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 0,
            },
            {
                "key": "metric_b",
                "label": "Metric B",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 0,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        mock_ws = self._make_mock_ws([])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        data = {"metric_a": "100", "metric_b": "200"}

        with patch("survey._get_spreadsheet", return_value=mock_sh):
            update_squad_powers("123456", "Alice", data, guild_id=TEST_GUILD_ID)

        call_args = mock_ws.append_row.call_args[0][0]
        assert "100" in call_args
        assert "200" in call_args


class TestAppendSurveyHistory:
    """Test append_survey_history appends correct timestamped row."""

    def test_history_row_appended(self, seeded_db):
        from survey import append_survey_history
        from config import save_survey_config
        from defaults import DEFAULT_SURVEY_QUESTIONS

        save_survey_config(TEST_GUILD_ID, "Stats", "History", DEFAULT_SURVEY_QUESTIONS, "")

        mock_ws = MagicMock()
        mock_ws.row_values = MagicMock(return_value=[])
        mock_ws.update = MagicMock()
        mock_ws.append_row = MagicMock()
        mock_ws.set_basic_filter = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        data = {"squad1_power": "43.27", "profession": "War Leader"}

        with patch("survey._get_spreadsheet", return_value=mock_sh):
            append_survey_history("123456", "Alice", data, guild_id=TEST_GUILD_ID)

        mock_ws.append_row.assert_called_once()
        row = mock_ws.append_row.call_args[0][0]
        assert "Alice" in row
        assert "123456" in row

    def test_history_header_created_on_first_submission(self, seeded_db):
        from survey import append_survey_history
        from config import save_survey_config

        questions = [
            {
                "key": "power",
                "label": "Power",
                "type": "text",
                "options": [],
                "placeholder": "",
                "max_chars": 0,
            },
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        mock_ws = MagicMock()
        mock_ws.row_values = MagicMock(return_value=[])  # empty sheet
        mock_ws.update = MagicMock()
        mock_ws.append_row = MagicMock()
        mock_ws.set_basic_filter = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        with patch("survey._get_spreadsheet", return_value=mock_sh):
            append_survey_history("123456", "Alice", {"power": "50"}, guild_id=TEST_GUILD_ID)

        # Header should have been written
        mock_ws.update.assert_called()
        header_call = mock_ws.update.call_args_list[0]
        header_row = header_call[0][1][0]
        assert "Timestamp" in header_row
        assert "Discord ID" in header_row
        assert "Power" in header_row


class TestAddTranslationHelper:
    """Survey threads are private, so a translate bot only sees the prompts if
    it's added as a thread member. Every failure path has to degrade to an
    untranslated-but-working survey, never a blocked member."""

    def _thread_and_guild(self, *, helper=None, add_error=None):
        thread = MagicMock()
        thread.id = 555
        thread.add_user = AsyncMock(side_effect=add_error)
        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.get_member = MagicMock(return_value=helper)
        return thread, guild

    async def test_adds_configured_helper(self):
        from survey import add_translation_helper

        helper = MagicMock()
        helper.bot = True
        thread, guild = self._thread_and_guild(helper=helper)

        assert await add_translation_helper(thread, guild, 42) is True
        thread.add_user.assert_awaited_once_with(helper)

    async def test_no_helper_configured_is_a_noop(self):
        from survey import add_translation_helper

        thread, guild = self._thread_and_guild()

        assert await add_translation_helper(thread, guild, 0) is False
        thread.add_user.assert_not_awaited()
        # Never even looks the member up when the feature is off.
        guild.get_member.assert_not_called()

    async def test_helper_no_longer_in_guild_is_skipped(self):
        from survey import add_translation_helper

        thread, guild = self._thread_and_guild(helper=None)

        assert await add_translation_helper(thread, guild, 42) is False
        thread.add_user.assert_not_awaited()

    async def test_missing_guild_is_skipped(self):
        from survey import add_translation_helper

        thread = MagicMock()
        thread.add_user = AsyncMock()

        assert await add_translation_helper(thread, None, 42) is False
        thread.add_user.assert_not_awaited()

    @pytest.mark.parametrize(
        "error",
        [
            discord.Forbidden(MagicMock(status=403), "missing access"),
            discord.HTTPException(MagicMock(status=500), "boom"),
        ],
    )
    async def test_discord_failures_do_not_raise(self, error):
        from survey import add_translation_helper

        helper = MagicMock()
        thread, guild = self._thread_and_guild(helper=helper, add_error=error)

        # The survey must continue even when the helper can't be added.
        assert await add_translation_helper(thread, guild, 42) is False


class TestDescribeHelperGaps:
    """Thread members inherit parent-channel permissions, so a helper that
    can't view the survey channel or post in its threads is useless. Both are
    silent at survey time, so they're reported when the bot is picked."""

    def _channel_granting(self, *, view=True, send_in_threads=True):
        channel = MagicMock()
        perms = MagicMock()
        perms.view_channel = view
        perms.send_messages_in_threads = send_in_threads
        channel.permissions_for = MagicMock(return_value=perms)
        return channel

    def test_no_gaps_when_fully_permitted(self):
        from survey import _describe_helper_gaps

        assert _describe_helper_gaps(MagicMock(), self._channel_granting()) == []

    def test_reports_missing_view_channel(self):
        from survey import _describe_helper_gaps

        gaps = _describe_helper_gaps(MagicMock(), self._channel_granting(view=False))
        assert gaps == ["**View Channel**"]

    def test_reports_missing_send_in_threads(self):
        from survey import _describe_helper_gaps

        gaps = _describe_helper_gaps(MagicMock(), self._channel_granting(send_in_threads=False))
        assert gaps == ["**Send Messages in Threads**"]

    def test_reports_both_gaps(self):
        from survey import _describe_helper_gaps

        gaps = _describe_helper_gaps(
            MagicMock(), self._channel_granting(view=False, send_in_threads=False)
        )
        assert gaps == ["**View Channel**", "**Send Messages in Threads**"]


class TestTranslationHelperConfig:
    """The setting is guild-level (one helper for every survey, default and
    extra) and defaults to off so existing alliances see no change."""

    def test_defaults_to_no_helper(self, seeded_db):
        from config import get_config

        assert get_config(TEST_GUILD_ID).survey_translate_bot_id == 0

    def test_round_trips_through_update_config_field(self, seeded_db):
        from config import get_config, update_config_field

        update_config_field(TEST_GUILD_ID, "survey_translate_bot_id", 987654321)
        assert get_config(TEST_GUILD_ID).survey_translate_bot_id == 987654321

        update_config_field(TEST_GUILD_ID, "survey_translate_bot_id", 0)
        assert get_config(TEST_GUILD_ID).survey_translate_bot_id == 0

    def test_survives_save_config_round_trip(self, seeded_db):
        from config import get_config, save_config

        cfg = get_config(TEST_GUILD_ID)
        cfg.survey_translate_bot_id = 111222333
        save_config(cfg)
        assert get_config(TEST_GUILD_ID).survey_translate_bot_id == 111222333

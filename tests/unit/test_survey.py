"""
Unit tests for survey.py — question config, dynamic column mapping,
update_squad_powers, append_survey_history (with mocked sheets).
"""
import pytest
from unittest.mock import patch, MagicMock, call
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestSurveyQuestionConfig:
    """Test that survey questions save and load correctly."""

    def test_ogv_default_questions_have_required_keys(self, temp_db):
        from config import OGV_SURVEY_QUESTIONS
        required_keys = {"key", "label", "type", "options", "placeholder"}
        for q in OGV_SURVEY_QUESTIONS:
            for k in required_keys:
                assert k in q, f"Question missing key '{k}': {q}"

    def test_dropdown_questions_have_options(self, temp_db):
        from config import OGV_SURVEY_QUESTIONS
        for q in OGV_SURVEY_QUESTIONS:
            if q["type"] == "dropdown":
                assert len(q["options"]) > 0, f"Dropdown has no options: {q['label']}"

    def test_text_questions_have_empty_options(self, temp_db):
        from config import OGV_SURVEY_QUESTIONS
        for q in OGV_SURVEY_QUESTIONS:
            if q["type"] == "text":
                assert q["options"] == [], f"Text question has options: {q['label']}"

    def test_all_questions_have_unique_keys(self, temp_db):
        from config import OGV_SURVEY_QUESTIONS
        keys = [q["key"] for q in OGV_SURVEY_QUESTIONS]
        assert len(keys) == len(set(keys)), "Duplicate question keys found"

    def test_custom_questions_saved_and_loaded(self, seeded_db):
        from config import save_survey_config, get_survey_config
        questions = [
            {"key": "q1", "label": "Squad Power", "type": "text",
             "options": [], "placeholder": "e.g. 43.27", "max_chars": 5},
            {"key": "q2", "label": "Role", "type": "dropdown",
             "options": ["Attacker", "Defender"], "placeholder": "", "max_chars": 0},
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
        ws.update    = MagicMock()
        ws.append_row= MagicMock()
        ws.row_values= MagicMock(return_value=[])
        return ws

    def test_new_member_appended(self, seeded_db):
        from survey import update_squad_powers
        from config import save_survey_config, OGV_SURVEY_QUESTIONS

        save_survey_config(TEST_GUILD_ID, "Squad Powers", "History",
                           OGV_SURVEY_QUESTIONS, "")

        mock_ws = self._make_mock_ws([])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        data = {"squad1_power": "43.27", "squad1_type": "Missile"}

        with patch("survey._get_spreadsheet", return_value=mock_sh):
            update_squad_powers("123456", "Alice", data, guild_id=TEST_GUILD_ID)

        mock_ws.append_row.assert_called_once()
        call_args = mock_ws.append_row.call_args[0][0]
        assert "Alice"  in call_args
        assert "123456" in call_args

    def test_existing_member_updated_not_duplicated(self, seeded_db):
        from survey import update_squad_powers
        from config import save_survey_config, OGV_SURVEY_QUESTIONS

        save_survey_config(TEST_GUILD_ID, "Squad Powers", "History",
                           OGV_SURVEY_QUESTIONS, "")

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
            {"key": "metric_a", "label": "Metric A", "type": "text",
             "options": [], "placeholder": "", "max_chars": 0},
            {"key": "metric_b", "label": "Metric B", "type": "text",
             "options": [], "placeholder": "", "max_chars": 0},
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
        from config import save_survey_config, OGV_SURVEY_QUESTIONS

        save_survey_config(TEST_GUILD_ID, "Stats", "History",
                           OGV_SURVEY_QUESTIONS, "")

        mock_ws = MagicMock()
        mock_ws.row_values  = MagicMock(return_value=[])
        mock_ws.update      = MagicMock()
        mock_ws.append_row  = MagicMock()
        mock_ws.set_basic_filter = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        data = {"squad1_power": "43.27", "profession": "War Leader"}

        with patch("survey._get_spreadsheet", return_value=mock_sh):
            append_survey_history("123456", "Alice", data, guild_id=TEST_GUILD_ID)

        mock_ws.append_row.assert_called_once()
        row = mock_ws.append_row.call_args[0][0]
        assert "Alice"  in row
        assert "123456" in row

    def test_history_header_created_on_first_submission(self, seeded_db):
        from survey import append_survey_history
        from config import save_survey_config

        questions = [
            {"key": "power", "label": "Power", "type": "text",
             "options": [], "placeholder": "", "max_chars": 0},
        ]
        save_survey_config(TEST_GUILD_ID, "Stats", "History", questions, "")

        mock_ws = MagicMock()
        mock_ws.row_values  = MagicMock(return_value=[])  # empty sheet
        mock_ws.update      = MagicMock()
        mock_ws.append_row  = MagicMock()
        mock_ws.set_basic_filter = MagicMock()
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        with patch("survey._get_spreadsheet", return_value=mock_sh):
            append_survey_history("123456", "Alice", {"power": "50"}, guild_id=TEST_GUILD_ID)

        # Header should have been written
        mock_ws.update.assert_called()
        header_call = mock_ws.update.call_args_list[0]
        header_row  = header_call[0][1][0]
        assert "Timestamp"  in header_row
        assert "Discord ID" in header_row
        assert "Power"      in header_row

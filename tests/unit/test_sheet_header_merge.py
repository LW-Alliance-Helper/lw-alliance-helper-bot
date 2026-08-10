"""
Unit tests for #440 — sheet writers driven by editable question lists.

The bug: the header was written once, when the tab was blank, and every
row after that was appended positionally from the current question list.
Edit the questions and new rows kept landing in the old column order,
under headers that no longer described them.

These tests drive the writers against a stateful fake worksheet and
assert the *resulting sheet*, because the thing that matters is whether
a value ends up under the column that names it.
"""

import pytest
from unittest.mock import patch
import sys, os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class FakeWS:
    """Worksheet that actually stores what's written to it."""

    def __init__(self, rows=None):
        self.rows = [list(r) for r in (rows or [])]
        self.filtered = False

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def row_values(self, n):
        return list(self.rows[n - 1]) if len(self.rows) >= n else []

    def update(self, rng, values, value_input_option=None):
        start = int(rng[1:]) - 1 if len(rng) > 1 else 0
        for i, row in enumerate(values):
            while len(self.rows) <= start + i:
                self.rows.append([])
            self.rows[start + i] = list(row)

    def append_row(self, row, value_input_option=None):
        self.rows.append(list(row))

    def set_basic_filter(self):
        self.filtered = True


class FakeSheet:
    def __init__(self, ws):
        self._ws = ws

    def worksheet(self, name):
        return self._ws

    def add_worksheet(self, title=None, rows=0, cols=0):
        return self._ws

    def column_at(self, column: str):
        """Every stored value under `column`, top row down."""
        header = self._ws.rows[0]
        if column not in header:
            return None
        i = header.index(column)
        return [(r[i] if i < len(r) else "") for r in self._ws.rows[1:]]


def q(key, label, qtype="text"):
    return {"key": key, "label": label, "type": qtype, "options": []}


class TestMergeSheetHeader:
    def test_blank_sheet_takes_the_desired_header(self):
        from config import merge_sheet_header

        assert merge_sheet_header([], ["A", "B"]) == ["A", "B"]
        assert merge_sheet_header(["", ""], ["A", "B"]) == ["A", "B"]

    def test_a_new_question_is_appended_on_the_right(self):
        from config import merge_sheet_header

        assert merge_sheet_header(["Ts", "A"], ["Ts", "A", "B"]) == ["Ts", "A", "B"]

    def test_a_dropped_question_keeps_its_column(self):
        """Its answers are still history — removing the column would strand
        every value already stored under it."""
        from config import merge_sheet_header

        assert merge_sheet_header(["Ts", "A", "B"], ["Ts", "B"]) == ["Ts", "A", "B"]

    def test_a_renamed_question_reads_as_a_drop_plus_an_add(self):
        from config import merge_sheet_header

        assert merge_sheet_header(["Ts", "Old"], ["Ts", "New"]) == ["Ts", "Old", "New"]

    def test_existing_column_order_is_never_rearranged(self):
        from config import merge_sheet_header

        assert merge_sheet_header(["Ts", "B", "A"], ["Ts", "A", "B"]) == ["Ts", "B", "A"]

    def test_pin_last_keeps_its_column_rightmost(self):
        from config import merge_sheet_header

        merged = merge_sheet_header(
            ["Name", "A", "Date Modified"],
            ["Name", "A", "B", "Date Modified"],
            pin_last="Date Modified",
        )
        assert merged == ["Name", "A", "B", "Date Modified"]

    def test_pin_last_is_added_when_the_sheet_never_had_it(self):
        from config import merge_sheet_header

        merged = merge_sheet_header(
            ["Name", "A"], ["Name", "A", "Date Modified"], pin_last="Date Modified"
        )
        assert merged == ["Name", "A", "Date Modified"]

    def test_trailing_blank_padding_is_ignored(self):
        from config import merge_sheet_header

        assert merge_sheet_header(["Ts", "A", "", ""], ["Ts", "A"]) == ["Ts", "A"]

    def test_a_desired_column_already_present_under_its_legacy_name_is_not_duplicated(self):
        """#456: a tab whose header predates a label rename shouldn't grow a
        second column once the rename ships — the legacy header already
        covers it."""
        from config import merge_sheet_header

        merged = merge_sheet_header(
            ["Ts", "1st Squad"],
            ["Ts", "1st Squad Power"],
            legacy_aliases={"1st Squad": "1st Squad Power"},
        )
        assert merged == ["Ts", "1st Squad"]

    def test_legacy_alias_does_not_suppress_an_unrelated_new_column(self):
        from config import merge_sheet_header

        merged = merge_sheet_header(
            ["Ts", "1st Squad"],
            ["Ts", "1st Squad Power", "New Question"],
            legacy_aliases={"1st Squad": "1st Squad Power"},
        )
        assert merged == ["Ts", "1st Squad", "New Question"]

    def test_legacy_alias_is_irrelevant_once_the_modern_column_already_exists(self):
        """A sheet that already hit the bug (both columns present) isn't
        touched further — reconciling it is a manual, one-time fix."""
        from config import merge_sheet_header

        merged = merge_sheet_header(
            ["Ts", "1st Squad", "1st Squad Power"],
            ["Ts", "1st Squad Power"],
            legacy_aliases={"1st Squad": "1st Squad Power"},
        )
        assert merged == ["Ts", "1st Squad", "1st Squad Power"]


class TestRowForHeader:
    def test_values_follow_their_column_name(self):
        from config import row_for_header

        assert row_for_header(["A", "B", "C"], {"C": 3, "A": 1}) == ["1", "", "3"]


class TestSurveyResponsesTab:
    """One row per member, so it can be remapped wholesale when columns move."""

    def _run(self, ws, questions, data, discord_id="111", username="Alice"):
        from survey import update_squad_powers

        sheet = FakeSheet(ws)
        survey = {"tab_squad_powers": "Answers", "questions": questions}
        with patch("survey._get_spreadsheet", return_value=sheet):
            update_squad_powers(discord_id, username, data, guild_id=TEST_GUILD_ID, survey=survey)
        return sheet

    def test_adding_a_question_keeps_stored_answers_under_their_own_columns(self, seeded_db):
        ws = FakeWS(
            [
                ["Username", "Discord ID", "Time Zone", "Date Modified"],
                ["Alice", "111", "UTC+1", "1/1/2026"],
                ["Bob", "222", "UTC-5", "1/2/2026"],
            ]
        )
        sheet = self._run(
            ws,
            [q("tz", "Time Zone"), q("role", "Preferred Role")],
            {"tz": "UTC+2", "role": "Engineer"},
        )

        assert ws.rows[0] == [
            "Username",
            "Discord ID",
            "Time Zone",
            "Preferred Role",
            "Date Modified",
        ]
        # Bob never answered the new question and must not inherit a value.
        assert sheet.column_at("Time Zone") == ["UTC+2", "UTC-5"]
        assert sheet.column_at("Preferred Role") == ["Engineer", ""]

    def test_date_modified_stays_the_rightmost_column(self, seeded_db):
        ws = FakeWS(
            [
                ["Username", "Discord ID", "Time Zone", "Date Modified"],
                ["Alice", "111", "UTC+1", "1/1/2026"],
            ]
        )
        self._run(ws, [q("tz", "Time Zone"), q("role", "Preferred Role")], {"tz": "UTC+2"})

        assert ws.rows[0][-1] == "Date Modified"
        assert ws.rows[1][-1] != ""

    def test_a_dropped_question_keeps_its_history_and_new_rows_blank_it(self, seeded_db):
        ws = FakeWS(
            [
                ["Username", "Discord ID", "Time Zone", "Retired", "Date Modified"],
                ["Bob", "222", "UTC-5", "kept", "1/2/2026"],
            ]
        )
        sheet = self._run(ws, [q("tz", "Time Zone")], {"tz": "UTC+9"}, discord_id="111")

        assert "Retired" in ws.rows[0]
        assert sheet.column_at("Retired") == ["kept", ""]

    def test_an_unchanged_header_is_left_alone(self, seeded_db):
        ws = FakeWS(
            [
                ["Username", "Discord ID", "Time Zone", "Date Modified"],
                ["Alice", "111", "UTC+1", "1/1/2026"],
            ]
        )
        self._run(ws, [q("tz", "Time Zone")], {"tz": "UTC+2"})

        assert len(ws.rows) == 2
        assert ws.rows[1][2] == "UTC+2"

    def test_a_question_labelled_like_an_identity_column_cannot_overwrite_it(self, seeded_db):
        """The upsert matches on Discord ID, so that cell has to stay true."""
        ws = FakeWS([])
        self._run(ws, [q("who", "Username")], {"who": "not-alice"})

        assert ws.rows[1][0] == "Alice"


class TestLegacySquadPowerLabels:
    """#456: alliances whose sheet predates the 1st/2nd/3rd Squad → ...Power
    label rename must keep writing to their existing column instead of
    growing a duplicate one."""

    def test_update_squad_powers_writes_under_the_legacy_header(self, seeded_db):
        from survey import update_squad_powers

        ws = FakeWS(
            [
                ["Username", "Discord ID", "1st Squad", "Date Modified"],
                ["Alice", "111", "40.00", "1/1/2026"],
            ]
        )
        sheet = FakeSheet(ws)
        survey = {
            "tab_squad_powers": "Answers",
            "questions": [{"key": "squad1_power", "label": "1st Squad Power", "type": "numeric"}],
        }
        with patch("survey._get_spreadsheet", return_value=sheet):
            update_squad_powers(
                "111", "Alice", {"squad1_power": "43.27"}, guild_id=TEST_GUILD_ID, survey=survey
            )

        # No duplicate "1st Squad Power" column, and the header is untouched.
        assert ws.rows[0] == ["Username", "Discord ID", "1st Squad", "Date Modified"]
        assert sheet.column_at("1st Squad") == ["43.27"]

    def test_append_survey_history_writes_under_the_legacy_header(self, seeded_db):
        from survey import append_survey_history

        ws = FakeWS([["Timestamp", "Discord ID", "Username", "1st Squad"]])
        sheet = FakeSheet(ws)
        survey = {
            "tab_history": "History",
            "questions": [{"key": "squad1_power", "label": "1st Squad Power", "type": "numeric"}],
        }
        with patch("survey._get_spreadsheet", return_value=sheet):
            append_survey_history(
                "111", "Alice", {"squad1_power": "43.27"}, guild_id=TEST_GUILD_ID, survey=survey
            )

        assert ws.rows[0] == ["Timestamp", "Discord ID", "Username", "1st Squad"]
        assert sheet.column_at("1st Squad") == ["43.27"]

    def test_a_sheet_using_the_current_label_already_is_unaffected(self, seeded_db):
        """Sanity check: aliasing must not interfere with the common case."""
        from survey import update_squad_powers

        ws = FakeWS([["Username", "Discord ID", "1st Squad Power", "Date Modified"]])
        sheet = FakeSheet(ws)
        survey = {
            "tab_squad_powers": "Answers",
            "questions": [{"key": "squad1_power", "label": "1st Squad Power", "type": "numeric"}],
        }
        with patch("survey._get_spreadsheet", return_value=sheet):
            update_squad_powers(
                "111", "Alice", {"squad1_power": "43.27"}, guild_id=TEST_GUILD_ID, survey=survey
            )

        assert ws.rows[0] == ["Username", "Discord ID", "1st Squad Power", "Date Modified"]
        assert sheet.column_at("1st Squad Power") == ["43.27"]


class TestSurveyHistoryTab:
    """Append-only: every submission ever, so columns only grow rightward."""

    def _run(self, ws, questions, data):
        from survey import append_survey_history

        sheet = FakeSheet(ws)
        survey = {"tab_history": "History", "questions": questions}
        with patch("survey._get_spreadsheet", return_value=sheet):
            append_survey_history("111", "Alice", data, guild_id=TEST_GUILD_ID, survey=survey)
        return sheet

    def test_a_new_question_extends_the_header_without_touching_stored_rows(self, seeded_db):
        ws = FakeWS(
            [
                ["Timestamp", "Discord ID", "Username", "Time Zone"],
                ["1/1/2026 10:00 UTC", "222", "Bob", "UTC-5"],
            ]
        )
        before = list(ws.rows[1])
        sheet = self._run(
            ws,
            [q("tz", "Time Zone"), q("role", "Preferred Role")],
            {"tz": "UTC+2", "role": "Engineer"},
        )

        assert ws.rows[0] == ["Timestamp", "Discord ID", "Username", "Time Zone", "Preferred Role"]
        assert ws.rows[1] == before, "an already-submitted row must not be rewritten"
        assert sheet.column_at("Preferred Role") == ["", "Engineer"]

    def test_answers_land_under_their_own_column_after_a_reorder(self, seeded_db):
        """The config lists these the other way round; the sheet's order wins."""
        ws = FakeWS([["Timestamp", "Discord ID", "Username", "B", "A"]])
        sheet = self._run(ws, [q("a", "A"), q("b", "B")], {"a": "avalue", "b": "bvalue"})

        assert sheet.column_at("A") == ["avalue"]
        assert sheet.column_at("B") == ["bvalue"]

    def test_a_blank_tab_gets_the_header_and_a_filter(self, seeded_db):
        ws = FakeWS([])
        self._run(ws, [q("tz", "Time Zone")], {"tz": "UTC+2"})

        assert ws.rows[0] == ["Timestamp", "Discord ID", "Username", "Time Zone"]
        assert ws.filtered is True

    def test_multi_select_answers_are_flattened(self, seeded_db):
        ws = FakeWS([])
        sheet = self._run(ws, [q("picks", "Picks", "multi_select")], {"picks": ["A", "B"]})

        assert sheet.column_at("Picks") == ["A, B"]


class TestParticipationLog:
    """The sibling in storm_log that #440 was actually filed against."""

    def _run(self, ws, questions, answers):
        import storm_log

        sheet = FakeSheet(ws)
        pcfg = {"tab_name": "DS Participation Log", "questions": questions}
        with (
            patch("storm_log._get_spreadsheet", return_value=sheet),
            patch("config.get_participation_config", return_value=pcfg),
        ):
            storm_log.append_participation_row(TEST_GUILD_ID, "DS", date(2026, 8, 7), answers)
        return sheet

    def test_editing_the_questions_no_longer_shifts_answers_into_wrong_columns(self, seeded_db):
        ws = FakeWS(
            [
                ["Date", "Event", "Showed up", "Sat out"],
                ["8/1/2026", "DS", "Alice", "Bob"],
            ]
        )
        # "Showed up" was removed from the config and a new question added.
        sheet = self._run(
            ws,
            [q("sat", "Sat out"), q("late", "Late")],
            {"sat": "Carol", "late": "Dave"},
        )

        assert sheet.column_at("Sat out") == ["Bob", "Carol"]
        assert sheet.column_at("Showed up") == ["Alice", ""]
        assert sheet.column_at("Late") == ["", "Dave"]

    def test_date_and_event_are_always_filled(self, seeded_db):
        ws = FakeWS([])
        sheet = self._run(ws, [q("sat", "Sat out")], {"sat": "Carol"})

        assert sheet.column_at("Date") == ["8/7/2026"]
        assert sheet.column_at("Event") == ["DS"]

    def test_list_answers_are_flattened(self, seeded_db):
        ws = FakeWS([])
        sheet = self._run(ws, [q("sat", "Sat out")], {"sat": ["Bob", "Carol"]})

        assert sheet.column_at("Sat out") == ["Bob, Carol"]

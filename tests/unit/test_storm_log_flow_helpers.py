"""The pure helpers behind the participation log walk (`storm_log_flow`)."""

from datetime import date
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from storm_log_flow import (
    bound_hint,
    parse_numeric,
    parse_answer_date,
    roster_preview,
    prefill_preview,
    top_counts,
    summary_text,
)


class TestBoundHint:
    def test_none(self):
        assert bound_hint(None, None) == ""

    def test_min_only(self):
        assert bound_hint(1, None) == " *(min `1`)*"

    def test_both(self):
        assert bound_hint(1, 10) == " *(min `1`, max `10`)*"


class TestParseNumeric:
    def test_integer_stays_integer(self):
        assert parse_numeric("42", None, None) == ("42", None)

    def test_decimal_is_float(self):
        assert parse_numeric("3.50", None, None) == ("3.5", None)

    def test_not_a_number(self):
        assert parse_numeric("x", None, None) == (
            None,
            "⚠️ `x` isn't a number. Please re-enter your answer.",
        )

    def test_below_min(self):
        assert parse_numeric("0", 1, None) == (None, "⚠️ Must be at least **1**. Please re-enter.")

    def test_above_max(self):
        assert parse_numeric("11", None, 10) == (None, "⚠️ Must be at most **10**. Please re-enter.")

    def test_bound_is_inclusive(self):
        assert parse_numeric("10", 1, 10) == ("10", None)


class TestParseAnswerDate:
    def test_matches(self):
        assert parse_answer_date("04/14/2026", "%m/%d/%Y") == ("2026-04-14", None)

    def test_mismatch(self):
        assert parse_answer_date("nope", "%m/%d/%Y") == (
            None,
            "⚠️ `nope` doesn't match `%m/%d/%Y`. Please re-enter.",
        )


class TestPreviews:
    def test_roster_short_list(self):
        assert roster_preview(["Alpha", "Bravo"]) == "Alpha, Bravo"

    def test_roster_long_list_collapses(self):
        assert roster_preview([f"M{i}" for i in range(26)]) == "26 members loaded"

    def test_prefill_empty(self):
        assert prefill_preview(set()) == ""

    def test_prefill_sorted_and_capped(self):
        names = {f"M{i:02d}" for i in range(7)}
        assert prefill_preview(names) == "M00, M01, M02, M03, M04 (+2 more)"

    def test_prefill_exactly_five(self):
        assert prefill_preview({"e", "d", "c", "b", "a"}) == "a, b, c, d, e"


class TestTopCounts:
    def test_orders_by_count_then_name_and_drops_zero(self):
        assert top_counts({"Bravo": 2, "Alpha": 2, "Zulu": 0, "Mike": 5}) == [
            "Mike (5)",
            "Alpha (2)",
            "Bravo (2)",
        ]

    def test_caps_at_n(self):
        counts = {f"M{i}": 10 - i for i in range(8)}
        assert len(top_counts(counts)) == 5


class TestSummaryText:
    def test_labels_and_none_for_blank(self):
        questions = [
            {"key": "outcome", "label": "Outcome", "type": "text"},
            {"key": "note", "label": "Note", "type": "text"},
            {"key": "orphan"},
        ]
        text = summary_text("Desert Storm", date(2026, 9, 12), questions, {"outcome": "Win"})
        assert text == (
            "📋 **Desert Storm Log: Saturday, September 12, 2026**\n"
            "**Outcome:** Win\n"
            "**Note:** None\n"
            "**orphan:** None"
        )

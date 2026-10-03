"""The participation tab in words and real dates (#729).

Date is written ISO (a real date in every locale) and Event as the storm's
name; the readers take both those and the old `M/D/YYYY` text and `DS` /
`CS` codes, so rows written before keep working."""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from tests.constants import TEST_GUILD_ID  # noqa: E402


class _WS:
    """A participation tab: `get_all_values` as displayed, and column A
    unformatted through `batch_get`, as a formatted tab reads."""

    def __init__(self, rows, column_a=None):
        self.rows = rows
        self.column_a = column_a

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def batch_get(self, ranges, **kwargs):
        if self.column_a is None:
            raise RuntimeError("not a formatted tab")
        return [[[v] for v in self.column_a]]


def _serial(iso: str) -> int:
    return (date.fromisoformat(iso) - date(1899, 12, 30)).days


ROWS = [
    ["Date", "Event", "Who sat out?"],
    ["9/28/2026", "DS", "Alpha"],  # written before #729
    ["10/2/2026", "CS", "Bravo"],
    ["2026-10-05", "Desert Storm", "Charlie"],  # written now, read before formatting
]


def _recent(ws, event, n=10):
    import storm_log

    with patch("storm_log._get_log_sheet", return_value=ws):
        return storm_log.list_recent_log_dates(event, n, guild_id=TEST_GUILD_ID)


def _lookup(ws, event, day):
    import storm_log

    with patch("storm_log._get_log_sheet", return_value=ws):
        return storm_log.lookup_log_entry(event, day, guild_id=TEST_GUILD_ID)


def test_recent_dates_read_old_and_new_rows():
    assert _recent(_WS(ROWS), "DS") == [date(2026, 10, 5), date(2026, 9, 28)]
    assert _recent(_WS(ROWS), "CS") == [date(2026, 10, 2)]


def test_lookup_finds_an_old_row_and_a_new_one():
    old = _lookup(_WS(ROWS), "DS", date(2026, 9, 28))
    assert old["fields"] == [("Who sat out?", "Alpha")]
    new = _lookup(_WS(ROWS), "DS", date(2026, 10, 5))
    assert new["fields"] == [("Who sat out?", "Charlie")]
    assert _lookup(_WS(ROWS), "CS", date(2026, 10, 5)) is None


def test_a_formatted_date_reads_whatever_the_locale_shows():
    """Once Date is a real date the displayed text is the locale's
    (`05/10/2026` here); the reader goes by the cell's value."""
    ws = _WS(
        [["Date", "Event", "Who sat out?"], ["05/10/2026", "Desert Storm", "Charlie"]],
        column_a=[_serial("2026-10-05")],
    )
    assert _recent(ws, "DS") == [date(2026, 10, 5)]
    assert _lookup(ws, "DS", date(2026, 10, 5))["fields"] == [("Who sat out?", "Charlie")]


def test_event_words_read_any_case():
    import storm_log

    assert storm_log.STORM_EVENT_WORDS.code("canyon storm") == "CS"
    assert storm_log.STORM_EVENT_WORDS.code("ds") == "DS"
    assert storm_log.STORM_EVENT_WORDS.word("CS") == "Canyon Storm"


def test_the_write_formats_the_tab_with_a_date_and_frozen_columns(seeded_db):
    import storm_log
    from tests.unit.test_sheet_header_merge import FakeSheet, FakeWS

    ws = FakeWS([])
    pcfg = {"tab_name": "CS Participation Log", "questions": [{"key": "s", "label": "Sat"}]}
    with (
        patch("storm_log._get_spreadsheet", return_value=FakeSheet(ws)),
        patch("config.get_participation_config", return_value=pcfg),
        patch("sheet_format.ensure_formatted") as fmt,
    ):
        storm_log.append_participation_row(TEST_GUILD_ID, "CS", date(2026, 10, 5), {"s": "x"})
    spec = fmt.call_args.args[1]
    assert (spec.date, spec.frozen_columns) == ((0,), 2)

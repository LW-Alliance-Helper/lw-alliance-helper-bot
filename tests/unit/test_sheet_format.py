"""The house-style formatting pass for bot-created tabs (#729)."""

from unittest.mock import MagicMock

import pytest

import sheet_format as sf


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(sf, "_met", set())


def _by_kind(requests, kind):
    return [r[kind] for r in requests if kind in r]


def test_header_is_dark_gray_white_bold_14_wrapped():
    header, body = _by_kind(sf.format_requests(5, sf.TabSpec()), "repeatCell")[:2]
    assert header["range"] == {"sheetId": 5, "startRowIndex": 0, "endRowIndex": 1}
    fmt = header["cell"]["userEnteredFormat"]
    assert fmt["backgroundColor"] == {"red": 67 / 255, "green": 67 / 255, "blue": 67 / 255}
    assert fmt["textFormat"] == {
        "foregroundColor": {"red": 1, "green": 1, "blue": 1},
        "bold": True,
        "fontSize": 14,
    }
    assert fmt["wrapStrategy"] == "WRAP"
    # The body runs to the bottom of the sheet: no end row.
    assert body["range"] == {"sheetId": 5, "startRowIndex": 1}
    assert body["cell"]["userEnteredFormat"]["textFormat"]["fontSize"] == 14
    assert body["cell"]["userEnteredFormat"]["horizontalAlignment"] == "LEFT"


def test_number_formats_per_column():
    spec = sf.TabSpec(quantity=(3,), text=(6,), date=(0,), datetime=(4,), time=(5,))
    formats = {
        r["range"]["startColumnIndex"]: r["cell"]["userEnteredFormat"]["numberFormat"]
        for r in _by_kind(sf.format_requests(5, spec), "repeatCell")
        if "startColumnIndex" in r["range"]
    }
    assert formats == {
        3: {"type": "NUMBER", "pattern": "#,##0"},
        6: {"type": "TEXT"},
        0: {"type": "DATE"},
        4: {"type": "DATE_TIME"},
        5: {"type": "TIME"},
    }


def test_frozen_header_and_name_column_and_no_filter():
    requests = sf.format_requests(5, sf.TabSpec(frozen_columns=2))
    (props,) = _by_kind(requests, "updateSheetProperties")
    assert props["properties"]["gridProperties"] == {"frozenRowCount": 1, "frozenColumnCount": 2}
    # Filters are the alliance's to add (Kevin, 2026-10-03); the pass never
    # sets, clears or replaces one.
    assert not any("Filter" in kind for r in requests for kind in r)


def test_header_is_styled():
    def body(*cells):
        return {"sheets": [{"data": [{"rowData": [{"values": list(cells)}]}]}]}

    plain = {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}}
    filled = {"userEnteredFormat": {"backgroundColor": {"red": 0.8, "green": 1, "blue": 1}}}
    bold = {"userEnteredFormat": {"textFormat": {"bold": True}}}
    assert sf.header_is_styled(body(plain, {})) is False
    assert sf.header_is_styled(body(plain, filled)) is True
    assert sf.header_is_styled(body(bold)) is True
    assert sf.header_is_styled({"sheets": [{"data": [{}]}]}) is False


def _sheet(marked=(), header_body=None):
    sh = MagicMock()
    sh.id = "abc"
    meta = [
        {"developerMetadata": {"metadataKey": sf.MARK_KEY, "location": {"sheetId": s}}}
        for s in marked
    ]
    sh.client.request.return_value.json.return_value = {"matchedDeveloperMetadata": meta}
    sh.fetch_sheet_metadata.return_value = header_body or {}
    ws = MagicMock()
    ws.id = 5
    ws.title = "Train History"
    return sh, ws


def test_a_new_tab_is_formatted_and_marked_once():
    sh, ws = _sheet()
    assert sf.ensure_formatted(ws, sf.TabSpec(), sh=sh) is True
    requests = sh.batch_update.call_args.args[0]["requests"]
    assert _by_kind(requests, "repeatCell")
    (mark,) = _by_kind(requests, "createDeveloperMetadata")
    assert mark["developerMetadata"]["location"] == {"sheetId": 5}
    # The same process never searches again.
    assert sf.ensure_formatted(ws, sf.TabSpec(), sh=sh) is False
    assert sh.client.request.call_count == 1


def test_a_marked_tab_is_left_alone():
    sh, ws = _sheet(marked=(5,))
    assert sf.ensure_formatted(ws, sf.TabSpec(), sh=sh) is False
    sh.batch_update.assert_not_called()


def test_a_tab_someone_styled_is_marked_but_not_formatted():
    styled = {
        "sheets": [
            {
                "data": [
                    {
                        "rowData": [
                            {"values": [{"userEnteredFormat": {"textFormat": {"bold": True}}}]}
                        ]
                    }
                ]
            }
        ]
    }
    sh, ws = _sheet(header_body=styled)
    assert sf.ensure_formatted(ws, sf.TabSpec(), sh=sh) is False
    requests = sh.batch_update.call_args.args[0]["requests"]
    assert [list(r) for r in requests] == [["createDeveloperMetadata"]]


def test_a_failed_search_formats_nothing():
    sh, ws = _sheet()
    sh.client.request.side_effect = RuntimeError("quota")
    assert sf.ensure_formatted(ws, sf.TabSpec(), sh=sh) is False
    sh.batch_update.assert_not_called()


def test_a_failed_format_does_not_raise():
    sh, ws = _sheet()
    sh.batch_update.side_effect = RuntimeError("quota")
    assert sf.ensure_formatted(ws, sf.TabSpec(), sh=sh) is False


# ── Reading dates back (#729) ────────────────────────────────────────────────


class _UnformattedWS:
    """Answers the unformatted read the way the Sheets API does: a date as a
    serial number, a number as a number, text as text."""

    def __init__(self, rows):
        self.rows = rows
        self.kwargs = None

    def get_all_values(self, **kwargs):
        self.kwargs = kwargs
        return [list(r) for r in self.rows]


def test_read_values_turns_serial_dates_into_iso_text():
    ws = _UnformattedWS(
        [
            ["Date", "Member", "Posted At", "Count"],
            [46293, "Alpha", 46293.8334375, 5.0],
            ["2026-09-29", "Bravo", "2026-09-29T22:00:12+00:00", 12],
            ["", "Charlie", "", ""],
            ["typed by hand", "Delta", "soon", True],
        ]
    )
    spec = sf.TabSpec(date=(0,), datetime=(2,))
    rows = sf.read_values(ws, spec)
    assert ws.kwargs == {
        "value_render_option": "UNFORMATTED_VALUE",
        "date_time_render_option": "SERIAL_NUMBER",
    }
    assert rows[0] == ["Date", "Member", "Posted At", "Count"]
    assert rows[1] == ["2026-09-28", "Alpha", "2026-09-28 20:00:09", "5"]
    # An old UTC stamp reads in server time (UTC-2).
    assert rows[2] == ["2026-09-29", "Bravo", "2026-09-29 20:00:12", "12"]
    assert rows[3] == ["", "Charlie", "", ""]
    assert rows[4] == ["typed by hand", "Delta", "soon", "TRUE"]


def test_read_values_falls_back_for_a_plain_worksheet():
    class Plain:
        def get_all_values(self):
            return [["Date"], ["2026-09-28"]]

    assert sf.read_values(Plain(), sf.TabSpec(date=(0,))) == [["Date"], ["2026-09-28"]]


def test_server_stamp_is_server_time_without_a_zone():
    from datetime import datetime, timezone

    from time_helpers import server_stamp

    moment = datetime(2026, 9, 28, 22, 0, 5, tzinfo=timezone.utc)
    assert server_stamp(moment) == "2026-09-28 20:00:05"


def test_dropdowns_list_the_words_and_flag_rather_than_refuse():
    spec = sf.TabSpec(dropdowns=((2, ("Auto", "VS")),))
    (dv,) = _by_kind(sf.format_requests(5, spec), "setDataValidation")
    assert dv["range"] == {
        "sheetId": 5,
        "startRowIndex": 1,
        "startColumnIndex": 2,
        "endColumnIndex": 3,
    }
    assert [v["userEnteredValue"] for v in dv["rule"]["condition"]["values"]] == ["Auto", "VS"]
    assert dv["rule"]["strict"] is False and dv["rule"]["showCustomUi"] is True

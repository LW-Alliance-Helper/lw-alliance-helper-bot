"""
Hidden column labels on the growth tabs (#668).

The bot finds its columns by a developer-metadata tag first and by header
text second, so a person can rename, shorten or move a column the bot owns
without breaking it. Covers the tag store (`sheet_tags`), the column finders
(`growth_columns`, `breakdown_columns`), and the snapshot and breakdown paths
that read and write through them.
"""

import json
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.constants import TEST_GUILD_ID


def _growth_tag(kind, metric=None, period=None):
    tag = {"t": "growth", "k": kind}
    if metric:
        tag.update({"m": metric, "p": period})
    return tag


def _bd_tag(kind, metric, prev, curr):
    return {"t": "breakdown", "k": kind, "m": metric, "from": prev, "to": curr}


def _match(col, tag, *, sheet_id=0, key="lwah.column", end=None, dimension="COLUMNS"):
    return {
        "developerMetadata": {
            "metadataKey": key,
            "metadataValue": json.dumps(tag),
            "location": {
                "dimensionRange": {
                    "sheetId": sheet_id,
                    "dimension": dimension,
                    "startIndex": col,
                    "endIndex": col + 1 if end is None else end,
                }
            },
        }
    }


# ── The tag store ────────────────────────────────────────────────────────────


class TestParseSearchResponse:
    def test_groups_tags_by_sheet_and_column(self):
        from sheet_tags import parse_search_response

        body = {
            "matchedDeveloperMetadata": [
                _match(0, _growth_tag("name"), sheet_id=11),
                _match(4, _growth_tag("metric", "Power", "Jan 2026"), sheet_id=11),
                _match(2, _growth_tag("id"), sheet_id=22),
            ]
        }

        assert parse_search_response(body) == {
            11: {0: _growth_tag("name"), 4: _growth_tag("metric", "Power", "Jan 2026")},
            22: {2: _growth_tag("id")},
        }

    def test_skips_anything_that_is_not_one_tagged_column(self):
        from sheet_tags import parse_search_response

        bad_json = _match(3, {})
        bad_json["developerMetadata"]["metadataValue"] = "not json"
        not_an_object = _match(5, {})
        not_an_object["developerMetadata"]["metadataValue"] = "[1, 2]"
        body = {
            "matchedDeveloperMetadata": [
                _match(0, _growth_tag("name"), key="someone.else"),
                _match(1, _growth_tag("name"), dimension="ROWS"),
                _match(2, _growth_tag("name"), end=4),
                bad_json,
                not_an_object,
            ]
        }

        assert parse_search_response(body) == {}

    def test_empty_or_odd_bodies_read_as_no_tags(self):
        from sheet_tags import parse_search_response

        assert parse_search_response({}) == {}
        assert parse_search_response(None) == {}
        assert parse_search_response(MagicMock()) == {}


class TestReadColumnTags:
    def test_searches_by_the_bot_key_in_one_call(self):
        from sheet_tags import TAG_KEY, read_column_tags

        sh = MagicMock()
        sh.id = "sheet-123"
        sh.client.request.return_value.json.return_value = {
            "matchedDeveloperMetadata": [_match(1, _growth_tag("id"), sheet_id=9)]
        }

        assert read_column_tags(sh) == {9: {1: _growth_tag("id")}}
        method, url = sh.client.request.call_args.args
        assert method == "post"
        assert url.endswith("/spreadsheets/sheet-123/developerMetadata:search")
        lookup = sh.client.request.call_args.kwargs["json"]["dataFilters"][0]
        assert lookup == {"developerMetadataLookup": {"metadataKey": TAG_KEY}}

    def test_a_failed_search_reads_as_no_tags(self):
        """Readers then fall back to header text, the behaviour before tags."""
        from sheet_tags import read_column_tags

        sh = MagicMock()
        sh.client.request.side_effect = RuntimeError("quota")

        assert read_column_tags(sh) == {}


class TestTagColumns:
    def test_one_batch_update_with_project_visible_column_tags(self):
        from sheet_tags import TAG_KEY, tag_columns

        sh, ws = MagicMock(), MagicMock()
        ws.id = 42
        tag_columns(sh, ws, {3: _growth_tag("id"), 1: _growth_tag("name")})

        requests = sh.batch_update.call_args.args[0]["requests"]
        metas = [r["createDeveloperMetadata"]["developerMetadata"] for r in requests]
        assert [m["location"]["dimensionRange"]["startIndex"] for m in metas] == [1, 3]
        first = metas[0]
        assert first["metadataKey"] == TAG_KEY
        assert first["visibility"] == "PROJECT"
        assert first["location"]["dimensionRange"] == {
            "sheetId": 42,
            "dimension": "COLUMNS",
            "startIndex": 1,
            "endIndex": 2,
        }
        assert json.loads(first["metadataValue"]) == _growth_tag("name")

    def test_nothing_to_tag_makes_no_call(self):
        from sheet_tags import tag_columns

        sh = MagicMock()
        tag_columns(sh, MagicMock(), {})

        sh.batch_update.assert_not_called()

    def test_a_failed_write_does_not_raise(self):
        """The column just stays untagged until the next write tags it."""
        from sheet_tags import tag_columns

        sh = MagicMock()
        sh.batch_update.side_effect = RuntimeError("quota")

        tag_columns(sh, MagicMock(), {0: _growth_tag("name")})

    def test_the_failure_log_cannot_raise_either(self):
        """The error path reads the tab's title for its log line; a tab
        object without one must not turn a logged failure into a crash."""
        from types import SimpleNamespace

        from sheet_tags import tag_columns

        tag_columns(SimpleNamespace(), SimpleNamespace(id=1), {0: _growth_tag("name")})


class TestEnsureColumns:
    def test_widens_a_tab_that_is_too_narrow(self):
        from sheet_tags import ensure_columns

        ws = MagicMock()
        ws.col_count = 50
        ensure_columns(ws, 53)

        ws.add_cols.assert_called_once_with(3)

    def test_leaves_a_wide_enough_tab_alone(self):
        from sheet_tags import ensure_columns

        ws = MagicMock()
        ws.col_count = 50
        ensure_columns(ws, 50)

        ws.add_cols.assert_not_called()


# ── Finding the Growth Tracking tab's columns ────────────────────────────────


class TestGrowthColumns:
    def test_header_text_finds_untagged_columns_and_queues_their_tags(self):
        from growth import growth_columns

        header = ["Name", "Power (Jan 2026)", "Power (Feb 2026)", "Discord ID"]
        cols = growth_columns(header, ["Power"])

        assert cols.name == 0
        assert cols.identity == 3
        assert cols.metrics == {("Power", "Jan 2026"): 1, ("Power", "Feb 2026"): 2}
        assert cols.to_tag == {
            0: _growth_tag("name"),
            1: _growth_tag("metric", "Power", "Jan 2026"),
            2: _growth_tag("metric", "Power", "Feb 2026"),
            3: _growth_tag("id"),
        }

    def test_a_tagged_column_is_found_whatever_its_header_says(self):
        """The point of #668: a person renamed every header and moved the
        name column, and the bot still knows what each column is."""
        from growth import growth_columns

        header = ["Notes", "Member", "1st Squad, Jan", "ID"]
        tags = {
            1: _growth_tag("name"),
            2: _growth_tag("metric", "Power", "Jan 2026"),
            3: _growth_tag("id"),
        }
        cols = growth_columns(header, ["Power"], tags)

        assert cols.name == 1
        assert cols.identity == 3
        assert cols.metrics == {("Power", "Jan 2026"): 2}
        assert cols.to_tag == {}

    def test_a_tag_beats_header_text_for_the_same_column(self):
        from growth import growth_columns

        header = ["Name", "Power (Jan 2026)", "Power (Jan 2026) copy"]
        tags = {2: _growth_tag("metric", "Power", "Jan 2026")}
        cols = growth_columns(header, ["Power"], tags)

        assert cols.metrics == {("Power", "Jan 2026"): 2}
        assert 1 not in cols.to_tag

    def test_periods_come_out_in_calendar_order(self):
        """A moved column can't reorder history: the previous period is the
        previous month, not the column to the left."""
        from growth import growth_columns

        header = ["Name", "Power (Mar 2026)", "Power (Jan 2026)", "Power (Feb 2026)"]
        cols = growth_columns(header, ["Power"])

        assert cols.periods == ["Jan 2026", "Feb 2026", "Mar 2026"]

    def test_other_tabs_tags_are_ignored(self):
        from growth import growth_columns

        tags = {1: _bd_tag("pct", "Power", "Jan 2026", "Feb 2026")}
        cols = growth_columns(["Name", "Power (Jan 2026)"], ["Power"], tags)

        assert cols.metrics == {("Power", "Jan 2026"): 1}

    def test_adding_a_column_appends_and_queues_its_tag(self):
        from growth import growth_columns

        header = ["Name"]
        cols = growth_columns(header, ["Power"], {0: _growth_tag("name")})
        cols.add_metric(header, "Power", "Mar 2026")
        cols.add_identity(header)

        assert header == ["Name", "Power (Mar 2026)", "Discord ID"]
        assert cols.to_tag == {
            1: _growth_tag("metric", "Power", "Mar 2026"),
            2: _growth_tag("id"),
        }


# ── Finding the Growth Breakdown tab's columns ───────────────────────────────


class TestBreakdownColumns:
    def test_header_text_splits_the_metric_longest_label_first(self):
        """`Squad Power` must not be read as the metric `Power` with a period
        ending in `Squad`."""
        from growth import breakdown_columns

        header = [
            "Name",
            "Jan 2026 - Feb 2026 Squad Power %",
            "Jan 2026 - Feb 2026 Squad Power Bucket",
        ]
        cols = breakdown_columns(header, ["Power", "Squad Power"])

        key = ("Jan 2026", "Feb 2026", "Squad Power")
        assert cols.pct == {key: 1}
        assert cols.bucket == {key: 2}
        assert cols.to_tag[1] == _bd_tag("pct", "Squad Power", "Jan 2026", "Feb 2026")

    def test_tagged_columns_are_found_under_short_headers(self):
        from growth import breakdown_columns

        header = ["Name", "% Change", "Bucket", "% Change", "Bucket"]
        tags = {
            1: _bd_tag("pct", "Power", "Feb 2026", "Mar 2026"),
            2: _bd_tag("bucket", "Power", "Feb 2026", "Mar 2026"),
            3: _bd_tag("pct", "Power", "Jan 2026", "Feb 2026"),
            4: _bd_tag("bucket", "Power", "Jan 2026", "Feb 2026"),
        }
        cols = breakdown_columns(header, ["Power"], tags)

        assert cols.transitions == [("Jan 2026", "Feb 2026"), ("Feb 2026", "Mar 2026")]
        assert cols.metrics_for("Feb 2026", "Mar 2026") == ["Power"]
        assert cols.bucket[("Feb 2026", "Mar 2026", "Power")] == 2

    def test_adding_a_transition_appends_both_columns_tagged(self):
        from growth import breakdown_columns

        header = ["Name"]
        cols = breakdown_columns(header, ["Power"])
        cols.add_transition_metric(header, "Jan 2026", "Feb 2026", "Power")

        assert header[1:] == ["Jan 2026 - Feb 2026 Power %", "Jan 2026 - Feb 2026 Power Bucket"]
        assert cols.to_tag[1] == _bd_tag("pct", "Power", "Jan 2026", "Feb 2026")
        assert cols.to_tag[2] == _bd_tag("bucket", "Power", "Jan 2026", "Feb 2026")


# ── Reading and writing through the tags ─────────────────────────────────────


def _growth_cfg():
    return {
        "enabled": 1,
        "tab_source": "Squad Powers",
        "tab_growth": "Growth Tracking",
        "tab_breakdown": "Growth Breakdown",
        "metrics": [{"col": "B", "label": "Power"}],
        "breakdown_labels": {},
        "breakdown_thresholds": {},
    }


class TestReadLatestBreakdownByTag:
    def test_reads_a_breakdown_whose_headers_a_person_renamed(self):
        from growth import read_latest_breakdown

        rows = [
            ["Member", "% Change", "Bucket", "Discord ID"],
            ["Alpha Tester", "25.00%", "Increased", "1"],
            ["Bravo Tester", "-3.00%", "Decline", "2"],
        ]
        tags = {
            0: {"t": "breakdown", "k": "name"},
            1: _bd_tag("pct", "Power", "Jan 2026", "Feb 2026"),
            2: _bd_tag("bucket", "Power", "Jan 2026", "Feb 2026"),
        }
        ws = MagicMock()
        ws.id = 5
        ws.get_all_values.return_value = rows
        sh = MagicMock()
        sh.worksheet.return_value = ws

        with (
            patch("config.get_growth_config", return_value=_growth_cfg()),
            patch("growth._get_spreadsheet", return_value=sh),
            patch("sheet_tags.read_column_tags", return_value={5: tags}),
        ):
            out = read_latest_breakdown(TEST_GUILD_ID)

        assert out["has_data"] is True
        assert (out["prev_period_label"], out["curr_period_label"]) == ("Jan 2026", "Feb 2026")
        assert out["summary"]["Power"]["increased"] == ["Alpha Tester"]
        assert out["summary"]["Power"]["decline"] == ["Bravo Tester"]


class TestSnapshotTagging:
    """`_run_growth_snapshot_inner` against a mocked Growth Tracking tab."""

    def _run(self, growth_rows, tags=None, *, col_count=50):
        from growth import _run_growth_snapshot_inner

        ws = MagicMock()
        ws.id = 7
        ws.title = "Growth Tracking"
        ws.get_all_values.return_value = growth_rows
        ws.row_values.return_value = growth_rows[0]
        ws.row_count = 100
        ws.col_count = col_count
        sh = MagicMock()
        sh.worksheet.return_value = ws
        members = [{"name": "Alpha Tester", "row_index": 2, "Power": 200.0}]
        cfg = MagicMock(spreadsheet_id="sheet-1")

        with (
            patch("config.get_config", return_value=cfg),
            patch("config.get_growth_config", return_value=_growth_cfg()),
            patch("growth._get_spreadsheet", return_value=sh),
            patch("growth.load_member_data", return_value=members),
            patch("growth.load_identity_map", return_value={"alpha tester": "1"}),
            patch("growth._write_breakdown_for_snapshot"),
            patch("sheet_tags.read_column_tags", return_value={7: tags or {}}),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)
        return ws, sh

    @staticmethod
    def _tagged(sh):
        tagged = {}
        for call in sh.batch_update.call_args_list:
            for req in call.args[0]["requests"]:
                meta = req["createDeveloperMetadata"]["developerMetadata"]
                col = meta["location"]["dimensionRange"]["startIndex"]
                tagged[col] = json.loads(meta["metadataValue"])
        return tagged

    @staticmethod
    def _this_month():
        from growth import ET

        return datetime.now(tz=ET).strftime("%b %Y")

    def test_an_untagged_tab_gets_every_column_tagged(self):
        """A tab written before #668 converts itself on its first snapshot."""
        month = self._this_month()
        ws, sh = self._run([["Name", "Power (Jan 2026)"], ["Alpha Tester", "100"]])

        assert self._tagged(sh) == {
            0: _growth_tag("name"),
            1: _growth_tag("metric", "Power", "Jan 2026"),
            2: _growth_tag("metric", "Power", month),
            3: _growth_tag("id"),
        }

    def test_a_renamed_tab_is_written_by_tag_and_not_retagged(self):
        month = self._this_month()
        rows = [["Member", "Jan", "ID"], ["Alpha Tester", "100", "1"]]
        tags = {
            0: _growth_tag("name"),
            1: _growth_tag("metric", "Power", "Jan 2026"),
            2: _growth_tag("id"),
        }
        ws, sh = self._run(rows, tags)

        assert self._tagged(sh) == {3: _growth_tag("metric", "Power", month)}
        updates = [u for c in ws.batch_update.call_args_list for u in c.args[0]]
        assert {"range": "D2", "values": [[200.0]]} in updates
        # Row found by the tagged ID column; nothing was re-stamped or renamed.
        assert not [u for u in updates if u["range"] in ("A2", "C2")]

    def test_this_months_column_under_a_renamed_header_counts_as_taken(self):
        month = self._this_month()
        rows = [["Name", "This month"], ["Alpha Tester", "150"]]
        tags = {0: _growth_tag("name"), 1: _growth_tag("metric", "Power", month)}
        ws, _ = self._run(rows, tags)

        ws.update.assert_not_called()
        ws.batch_update.assert_not_called()

    def test_a_full_tab_is_widened_before_new_columns_are_written(self):
        ws, _ = self._run([["Name", "Power (Jan 2026)"], ["Alpha Tester", "100"]], col_count=2)

        ws.add_cols.assert_called_once_with(2)


class TestBreakdownWriterTagging:
    def _run(self, bd_rows, bd_tags):
        from growth import _write_breakdown_for_snapshot

        growth_header = ["Name", "Power (Jan 2026)", "Power (Feb 2026)", "Discord ID"]
        growth_rows = [growth_header, ["Alpha Tester", "100", "", "1"]]
        ws_bd = MagicMock()
        ws_bd.id = 8
        ws_bd.title = "Growth Breakdown"
        ws_bd.get_all_values.return_value = bd_rows
        ws_bd.col_count = 50
        sh = MagicMock()
        sh.worksheet.return_value = ws_bd
        members = [{"name": "Alpha Tester", "Power": 150.0}]

        with patch("growth.load_identity_map", return_value={"alpha tester": "1"}):
            _write_breakdown_for_snapshot(
                sh,
                _growth_cfg(),
                members,
                ["Power"],
                growth_rows,
                growth_header,
                curr_period_label="Feb 2026",
                guild_id=None,
                all_tags={8: bd_tags},
            )
        return ws_bd, sh

    def test_a_transition_already_written_under_short_headers_is_skipped(self):
        rows = [["Member", "% Change", "Bucket"], ["Alpha Tester", "50.00%", "Increased"]]
        tags = {
            0: {"t": "breakdown", "k": "name"},
            1: _bd_tag("pct", "Power", "Jan 2026", "Feb 2026"),
            2: _bd_tag("bucket", "Power", "Jan 2026", "Feb 2026"),
        }
        ws_bd, _ = self._run(rows, tags)

        ws_bd.update.assert_not_called()
        ws_bd.batch_update.assert_not_called()

    def test_a_new_transition_is_written_and_tagged(self):
        ws_bd, sh = self._run([["Name"]], {})

        header = ws_bd.update.call_args.args[1][0]
        assert header == [
            "Name",
            "Jan 2026 - Feb 2026 Power %",
            "Jan 2026 - Feb 2026 Power Bucket",
            "Discord ID",
        ]
        tagged = {}
        for call in sh.batch_update.call_args_list:
            for req in call.args[0]["requests"]:
                meta = req["createDeveloperMetadata"]["developerMetadata"]
                tagged[meta["location"]["dimensionRange"]["startIndex"]] = json.loads(
                    meta["metadataValue"]
                )
        assert tagged == {
            0: {"t": "breakdown", "k": "name"},
            1: _bd_tag("pct", "Power", "Jan 2026", "Feb 2026"),
            2: _bd_tag("bucket", "Power", "Jan 2026", "Feb 2026"),
            3: {"t": "breakdown", "k": "id"},
        }
        updates = [u for c in ws_bd.batch_update.call_args_list for u in c.args[0]]
        assert {"range": "B2", "values": [["50.00%"]]} in updates
        assert {"range": "C2", "values": [["Increased"]]} in updates

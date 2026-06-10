"""Regression tests for the transfer_setup select-option helpers (#16).

Discord caps a SelectOption *value* at 1–100 chars, but a real sheet header
can be a long survey question or blank — which crashed the column-mapping
view on a live sheet. Options are now keyed by column index; these pin that.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import transfer_setup


class TestHeaderOptions:
    def test_long_and_blank_headers(self):
        headers = ["Name", "", "Q " + "x" * 200]
        opts = transfer_setup._header_options(headers, selected=["Name"])
        # Blank header skipped; values are the original column indices.
        assert [o.value for o in opts] == ["0", "2"]
        # Every value + label stays within Discord's 1–100 limit.
        assert all(1 <= len(o.value) <= 100 for o in opts)
        assert all(1 <= len(o.label) <= 100 for o in opts)
        # The pre-selected header is defaulted.
        assert opts[0].default is True

    def test_caps_at_25(self):
        headers = [f"Col {i}" for i in range(40)]
        assert len(transfer_setup._header_options(headers)) == 25


class TestHeadersFromValues:
    HEADERS = ["A", "B", "C", "D"]

    def test_maps_indices_back_in_sheet_order(self):
        # Selected out of order → returned in sheet order.
        assert transfer_setup._headers_from_values(self.HEADERS, ["2", "0"]) == ["A", "C"]

    def test_ignores_bad_values(self):
        assert transfer_setup._headers_from_values(self.HEADERS, ["9", "x", "1"]) == ["B"]

    def test_empty(self):
        assert transfer_setup._headers_from_values(self.HEADERS, []) == []


class TestColumnMapStatusFlag:
    """The read-only watch mode (mode 3) drops the Status picker, so a watched
    shared sheet never carries status columns to track changes on."""

    HEADERS = ["In Game Username", "Current Server", "Confirmed", "Total Hero Power"]
    FULL = {
        "name": "In Game Username",
        "status": ["Confirmed"],
        "display": ["Total Hero Power"],
        "identity_extra": ["Current Server"],
    }

    def test_include_status_true_keeps_status(self):
        v = transfer_setup._ColumnMapView(
            owner_id=1, headers=self.HEADERS, initial_map=self.FULL, include_status=True
        )
        cm = v.column_map()
        assert cm["status"] == ["Confirmed"]
        assert cm["identity_extra"] == ["Current Server"]

    def test_include_status_false_drops_status(self):
        # Even when the seed carries a status, the flag suppresses it.
        v = transfer_setup._ColumnMapView(
            owner_id=1, headers=self.HEADERS, initial_map=self.FULL, include_status=False
        )
        cm = v.column_map()
        assert "status" not in cm
        assert cm["name"] == "In Game Username"
        assert cm["display"] == ["Total Hero Power"]


class TestMapEmbedLegend:
    def test_status_legend_present_when_included(self):
        out = transfer_setup._map_embed({"name": "Name"}, False, include_status=True).description
        assert "Status to watch" in out
        assert "Identity Fallback" in out
        assert "④" in out

    def test_status_legend_dropped_when_excluded(self):
        out = transfer_setup._map_embed({"name": "Name"}, False, include_status=False).description
        assert "Status to watch" not in out
        assert "Identity Fallback" in out
        assert "④" not in out  # only three rows now


class TestModeLabels:
    def test_all_modes_have_labels(self):
        for mode in (
            transfer_setup._MODE_SOURCE_TO_OWN,
            transfer_setup._MODE_OWN,
            transfer_setup._MODE_WATCH,
        ):
            assert transfer_setup._MODE_LABELS[mode]


class TestPaging:
    """Column pickers page through >25-column sheets; selections are tracked as
    global indices so they survive page flips."""

    HEADERS = [f"C{i}" for i in range(60)]  # 3 pages of 25/25/10

    def test_page_count(self):
        assert transfer_setup._page_count(self.HEADERS) == 3
        assert transfer_setup._page_count([]) == 1
        assert transfer_setup._page_count(["a"]) == 1

    def test_page_options_use_global_indices_and_default(self):
        opts = transfer_setup._page_options(self.HEADERS, 1, {30})  # page 1 = idx 25..49
        assert opts[0].value == "25"
        assert opts[-1].value == "49"
        assert any(o.default and o.value == "30" for o in opts)

    def test_page_options_skip_blank_and_fall_back(self):
        opts = transfer_setup._page_options(["", "  ", ""], 0, set())
        assert len(opts) == 1 and opts[0].value == "-1"

    def test_page_index_set(self):
        assert transfer_setup._page_index_set(self.HEADERS, 2) == set(range(50, 60))

    def test_merge_preserves_offpage_selection(self):
        # Existing picks {2 (page0), 40 (page1)}; on page 1 the user now picks 41.
        on_page = transfer_setup._page_index_set(self.HEADERS, 1)  # {25..49}
        merged = transfer_setup._merge_page_selection({2, 40}, on_page, ["41"])
        assert merged == {2, 41}  # page-0's 2 survives; page-1's 40 replaced by 41

    def test_merge_drops_placeholder_value(self):
        assert transfer_setup._merge_page_selection(set(), {0, 1, 2}, ["-1"]) == set()

    def test_idx_for_headers_first_match_and_drop_missing(self):
        assert transfer_setup._idx_for_headers(["A", "B", "C"], ["c", "A"]) == [2, 0]
        assert transfer_setup._idx_for_headers(["A", "B"], ["zzz"]) == []

    def test_wide_column_map_view_seeds_and_resolves_beyond_page0(self):
        v = transfer_setup._ColumnMapView(
            owner_id=1,
            headers=self.HEADERS,
            initial_map={"name": "C40", "status": ["C50"], "display": ["C5"]},
            include_status=True,
        )
        assert v.pages == 3
        assert v.name_idx == 40
        assert v.status_idx == {50}
        cm = v.column_map()
        assert cm["name"] == "C40"
        assert cm["status"] == ["C50"]
        assert cm["display"] == ["C5"]

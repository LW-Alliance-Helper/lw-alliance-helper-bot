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

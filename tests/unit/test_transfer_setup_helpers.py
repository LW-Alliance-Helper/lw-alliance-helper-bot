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


class TestAdaptiveColumnMap:
    """Wide sheets use the one-field-at-a-time adaptive mapper so paging never
    disturbs a field you aren't editing."""

    HEADERS = [f"C{i}" for i in range(60)]  # 3 pages

    def _view(self, include_status=True):
        return transfer_setup._AdaptiveColumnMapView(
            owner_id=1,
            headers=self.HEADERS,
            initial_map={"name": "C40", "status": ["C50"], "display": ["C5"]},
            include_status=include_status,
        )

    def test_seeds_and_resolves_across_pages(self):
        v = self._view()
        assert v.pages == 3
        assert v.name_idx == 40
        cm = v.column_map()
        assert cm["name"] == "C40"
        assert cm["status"] == ["C50"]
        assert cm["display"] == ["C5"]

    def test_hub_has_one_button_per_field_plus_save(self):
        assert len(self._view().children) == 5  # name/status/display/identity + save
        assert len(self._view(include_status=False).children) == 4  # status dropped

    def test_field_view_is_one_select_plus_nav(self):
        v = self._view()
        v._render_field("status")
        kinds = [type(c).__name__ for c in v.children]
        assert kinds.count("Select") == 1
        assert kinds.count("Button") == 3  # ◀ ▶ Done

    def test_name_field_is_single_select(self):
        v = self._view()
        v._render_field("name")
        sel = next(c for c in v.children if type(c).__name__ == "Select")
        assert sel.max_values == 1

    def test_paging_shows_only_that_pages_columns(self):
        v = self._view()
        v.page = 2
        v._render_field("display")
        sel = next(c for c in v.children if type(c).__name__ == "Select")
        assert sel.options[0].value == "50"
        assert sel.options[-1].value == "59"


class TestEditMenuSections:
    """The re-entry menu shows sections appropriate to the setup mode."""

    def _labels(self, mode):
        v = transfer_setup._EditMenuView(owner_id=1, mode=mode)
        return [c.label for c in v.children]

    def test_own_shows_every_section(self):
        labels = self._labels(transfer_setup._MODE_OWN)
        for needle in (
            "Column mapping",
            "Channel",
            "Notification style",
            "Frequency",
            "Filter",
            "Intake",
            "Templates",
            "Decisions",
            "Removal",
            "Fill-in",
            "Change sheets",
            "Done",
        ):
            assert any(needle in label for label in labels), needle

    def test_watch_hides_intake_and_decisions_keeps_filter(self):
        labels = self._labels(transfer_setup._MODE_WATCH)
        assert not any("Intake" in label for label in labels)
        assert not any("Decisions" in label for label in labels)
        assert not any("Removal" in label for label in labels)
        assert not any("Fill-in" in label for label in labels)
        assert any("Filter" in label for label in labels)

    def test_source_to_own_hides_standalone_filter_keeps_intake(self):
        labels = self._labels(transfer_setup._MODE_SOURCE_TO_OWN)
        assert not any("Filter" in label for label in labels)
        assert any("Intake" in label for label in labels)
        assert any("Fill-in" in label for label in labels)
        assert any("Decisions" in label for label in labels)


class TestDecisionsManager:
    """The decisions manager lists every decision with its values so users can
    see what exists (and not make duplicates), and pick one to edit/delete."""

    DS = [
        {"column": "Confirmed", "kind": "yesno", "options": []},
        {"column": "Status", "kind": "pickone", "options": ["Pending", "Confirmed", "Declined"]},
    ]

    def test_empty_embed_prompts_to_add(self):
        assert "Add a decision" in transfer_setup._decisions_embed([]).description

    def test_list_embed_shows_each_with_its_values(self):
        d = transfer_setup._decisions_embed(self.DS).description
        assert "1. Confirmed" in d and "Yes / No" in d
        assert "2. Status" in d and "Pending, Confirmed, Declined" in d

    def test_pick_view_one_option_per_decision(self):
        v = transfer_setup._DecisionPickView(owner_id=1, decisions=self.DS, verb="delete")
        sel = next(c for c in v.children if type(c).__name__ == "Select")
        assert [o.value for o in sel.options] == ["0", "1"]
        assert sel.options[0].label == "Confirmed"


class TestSaveDecisions:
    """_save_decisions persists status (watched) + decisions (shape) into the
    column map and flips writeback_enabled, preserving the rest of the map."""

    GUILD = 770000000000000001

    def test_round_trip_and_writeback_on(self, temp_db):
        import config
        import transfer

        config.update_transfer_config_field(
            self.GUILD, "alliance_column_map_json", '{"name": "IGN", "display": ["Power"]}'
        )
        decisions = [
            {"column": "Confirmed", "kind": "yesno", "options": []},
            {"column": "Status", "kind": "pickone", "options": ["Pending", "Confirmed"]},
        ]
        transfer_setup._save_decisions(self.GUILD, decisions)
        cfg = config.get_transfer_config(self.GUILD)
        cm = transfer.parse_column_map(cfg["alliance_column_map_json"])
        assert cm["name"] == "IGN"  # untouched
        assert cm["display"] == ["Power"]  # untouched
        assert cm["status"] == ["Confirmed", "Status"]
        assert cm["decisions"] == decisions
        assert cfg["writeback_enabled"] == 1

    def test_clearing_turns_writeback_off(self, temp_db):
        import config
        import transfer

        config.update_transfer_config_fields(
            self.GUILD,
            alliance_column_map_json=(
                '{"name": "IGN", "status": ["X"], "decisions": '
                '[{"column": "X", "kind": "yesno", "options": []}]}'
            ),
            writeback_enabled=1,
        )
        transfer_setup._save_decisions(self.GUILD, [])
        cfg = config.get_transfer_config(self.GUILD)
        cm = transfer.parse_column_map(cfg["alliance_column_map_json"])
        assert "status" not in cm and "decisions" not in cm
        assert cm["name"] == "IGN"  # preserved
        assert cfg["writeback_enabled"] == 0

"""Unit tests for transfer.py — the Transfer Management (#16) pure-logic
core: header-name addressing, auto-suggest, the AND-only filter DSL, change
detection (identity hashing + status snapshots/diffs/deletions), display
fields, value coercion, and template rendering.

Column model: only a Name column is special-and-required. ``status`` and
``display`` are free-choice header lists; identity = Name + chosen
``identity_extra`` columns; filters target any column by header. All
side-effect-free; no DB / Discord needed.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import transfer
from defaults import DEFAULT_TRANSFER_TEMPLATES


# An alliance-curated sheet: Name + a few stats + status columns + an extra.
HEADER = ["Name", "Tier", "Total Power", "Want?", "Confirmed", "Declined", "Notes", "Bear vs Lion"]
HIDX = transfer.header_index(HEADER)
ROW = ["Bad Pew", "Pioneer", "199M", "TRUE", "", "", "note", "Pink Cat"]


# ── Column-letter utilities ──────────────────────────────────────────────────


class TestColumnLetters:
    @pytest.mark.parametrize(
        "letter,idx",
        [("A", 0), ("a", 0), ("Z", 25), ("AA", 26), ("AB", 27), ("  c ", 2)],
    )
    def test_letter_to_index(self, letter, idx):
        assert transfer.col_letter_to_index(letter) == idx

    @pytest.mark.parametrize("bad", ["", "   ", "3", "A1", None])
    def test_letter_to_index_bad(self, bad):
        assert transfer.col_letter_to_index(bad) is None

    @pytest.mark.parametrize("idx,letter", [(0, "A"), (25, "Z"), (26, "AA"), (27, "AB")])
    def test_index_to_letter(self, idx, letter):
        assert transfer.col_index_to_letter(idx) == letter

    def test_roundtrip(self):
        for i in range(0, 100):
            assert transfer.col_letter_to_index(transfer.col_index_to_letter(i)) == i


# ── Column-map decode + header addressing ────────────────────────────────────


class TestParseColumnMap:
    MAP = {"name": "Name"}

    def test_passthrough_dict(self):
        assert transfer.parse_column_map(self.MAP) is self.MAP

    def test_json_string(self):
        assert transfer.parse_column_map('{"name": "Name"}') == {"name": "Name"}

    @pytest.mark.parametrize("bad", ["", None, "{not json", "[]", "42"])
    def test_bad_input_returns_empty(self, bad):
        assert transfer.parse_column_map(bad) == {}


class TestHeaderIndex:
    def test_basic(self):
        assert transfer.header_index(["A", "B", "C"]) == {"a": 0, "b": 1, "c": 2}

    def test_case_and_whitespace_insensitive(self):
        assert transfer.header_index(["  Total  Power "]) == {"total power": 0}

    def test_duplicate_header_first_wins(self):
        assert transfer.header_index(["X", "X"]) == {"x": 0}

    def test_blank_headers_skipped(self):
        assert transfer.header_index(["A", "", "C"]) == {"a": 0, "c": 2}


class TestSummarizeColumnMap:
    def test_full_map(self):
        m = {
            "name": "In Game Username",
            "identity_extra": ["Current Server"],
            "status": ["Want?", "Confirmed"],
            "display": ["Total Hero Power", "Anticipated Seat Color"],
        }
        out = transfer.summarize_column_map(m)
        assert "**Name:** In Game Username" in out
        assert "Also-identity:** Current Server" in out
        assert "Want?, Confirmed" in out
        assert "Total Hero Power, Anticipated Seat Color" in out

    def test_minimal_map(self):
        out = transfer.summarize_column_map({"name": "Name"})
        assert "**Name:** Name" in out
        assert "Status watched:** *none*" in out
        assert "Shown in notices:** *none*" in out
        assert "Also-identity" not in out  # omitted when empty

    def test_empty_map(self):
        assert "*not set*" in transfer.summarize_column_map({})


class TestCellFor:
    def test_resolves_by_header(self):
        assert transfer.cell_for(ROW, HIDX, "Name") == "Bad Pew"
        assert transfer.cell_for(ROW, HIDX, "Total Power") == "199M"
        assert transfer.cell_for(ROW, HIDX, "bear vs lion") == "Pink Cat"  # case-insensitive

    def test_unknown_header_returns_none(self):
        assert transfer.cell_for(ROW, HIDX, "Missing") is None

    def test_row_too_short_returns_none(self):
        assert transfer.cell_for(["only one"], HIDX, "Total Power") is None

    def test_letter_fallback(self):
        # No header_index entry, but "C" reads as a literal column letter.
        assert transfer.cell_for(["a", "b", "c"], {}, "C") == "c"

    def test_strips_whitespace(self):
        assert transfer.cell_for(["  spaced  "], {"name": 0}, "name") == "spaced"


# ── Auto-suggest ─────────────────────────────────────────────────────────────


class TestSuggestColumnMap:
    # The real server-wide Form_Responses header row (#16 design conversation).
    FORM_HEADER = [
        "Timestamp",
        "In Game Username",
        "Current Server",
        "Current Alliance",
        "Total Hero Power",
        "Arena Total Hero Power",
        "Main March Power",
        "Main March Type",
        "Total Kills",
        "Anticipated Seat Color",
        "Where are you currently located? (city, state, country)",
        "Why are you interested in transferring?",
        "Requested Landing Alliance",
    ]

    def test_name_and_identity_on_real_sheet(self):
        s = transfer.suggest_column_map(self.FORM_HEADER)
        assert s["name"] == "In Game Username"
        assert s["identity_extra"] == ["Current Server"]

    def test_intake_sheet_has_no_status(self):
        # No want/confirmed/declined columns, and the prose "Why are you
        # interested in transferring?" must not be mistaken for status.
        s = transfer.suggest_column_map(self.FORM_HEADER)
        assert "status" not in s

    def test_display_seeds_all_power_stats(self):
        s = transfer.suggest_column_map(self.FORM_HEADER)
        d = s["display"]
        assert "Total Hero Power" in d
        assert "Arena Total Hero Power" in d
        assert "Main March Power" in d
        assert "Anticipated Seat Color" in d
        assert "Total Kills" in d
        # Categorical "Main March Type" isn't a stat → not seeded.
        assert "Main March Type" not in d

    def test_alliance_curated_sheet_maps_status(self):
        header = ["Name", "Total Power", "Want?", "Confirmed", "Declined", "Notes"]
        s = transfer.suggest_column_map(header)
        assert s["name"] == "Name"
        assert s["status"] == ["Want?", "Confirmed", "Declined"]
        assert s["display"] == ["Total Power"]
        assert "identity_extra" not in s  # no server column here

    def test_no_match_returns_empty(self):
        assert transfer.suggest_column_map(["Foo", "Bar", "Baz"]) == {}

    def test_each_header_claimed_once(self):
        # A "Server" header is identity, not also display/status.
        s = transfer.suggest_column_map(["Name", "Server"])
        assert s["name"] == "Name"
        assert s["identity_extra"] == ["Server"]


# ── Value coercion ───────────────────────────────────────────────────────────


class TestCoerceNumber:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("199M", 199_000_000),
            ("1.2b", 1_200_000_000),
            ("300k", 300_000),
            ("304,743,912", 304_743_912),
            ("100", 100),
            (100, 100),
            (43.27, 43.27),
            ("  55.4 M ", 55_400_000),
            # Real-world messy formats from a live transfer sheet.
            ("125 971 854", 125_971_854),  # space-separated thousands
            ("216,976,226", 216_976_226),
            ("168,359,484 as of 5/5", 168_359_484),  # trailing prose
            ("100M as of 5/5/26", 100_000_000),  # suffix + trailing prose
            ("271m", 271_000_000),
            ("186M", 186_000_000),
            ("69", 69),
        ],
    )
    def test_parses(self, value, expected):
        assert transfer.coerce_number(value) == pytest.approx(expected)

    def test_euro_decimal_is_known_limitation(self):
        assert transfer.coerce_number("174,5") == 1745

    @pytest.mark.parametrize("bad", ["", "  ", "abc", None, "M", [1], "as of 5/5", "N/A"])
    def test_unparseable_returns_none(self, bad):
        assert transfer.coerce_number(bad) is None


class TestCoerceBool:
    @pytest.mark.parametrize("v", ["TRUE", "yes", "x", "1", "✅", "confirmed"])
    def test_truthy(self, v):
        assert transfer.coerce_bool(v) is True

    @pytest.mark.parametrize("v", ["false", "no", "0", "", "❌"])
    def test_falsy(self, v):
        assert transfer.coerce_bool(v) is False

    @pytest.mark.parametrize("v", ["maybe", "Pioneer", None])
    def test_ambiguous(self, v):
        assert transfer.coerce_bool(v) is None


# ── Filter DSL ───────────────────────────────────────────────────────────────


class TestParseFilter:
    def test_empty_is_none(self):
        assert transfer.parse_filter("") is None
        assert transfer.parse_filter(None) is None

    def test_malformed_is_none(self):
        assert transfer.parse_filter("{bad") is None

    def test_no_and_key_is_none(self):
        assert transfer.parse_filter('{"or": []}') is None

    def test_valid(self):
        f = transfer.parse_filter('{"and": [{"column": "Total Power", "op": ">=", "value": 100}]}')
        assert f == {"and": [{"column": "Total Power", "op": ">=", "value": 100}]}


class TestEvaluateFilter:
    def test_no_filter_passes_everything(self):
        assert transfer.evaluate_filter(None, ROW, HIDX) is True
        assert transfer.evaluate_filter({}, ROW, HIDX) is True
        assert transfer.evaluate_filter("", ROW, HIDX) is True

    def test_numeric_ge(self):
        f = {"and": [{"column": "Total Power", "op": ">=", "value": "100M"}]}
        assert transfer.evaluate_filter(f, ROW, HIDX) is True
        f["and"][0]["value"] = "250M"
        assert transfer.evaluate_filter(f, ROW, HIDX) is False

    def test_in_list(self):
        f = {"and": [{"column": "Tier", "op": "in", "value": ["Pioneer", "Contributor"]}]}
        assert transfer.evaluate_filter(f, ROW, HIDX) is True
        f["and"][0]["value"] = ["Elite"]
        assert transfer.evaluate_filter(f, ROW, HIDX) is False

    def test_contains(self):
        f = {"and": [{"column": "Tier", "op": "contains", "value": "ion"}]}
        assert transfer.evaluate_filter(f, ROW, HIDX) is True

    def test_equals_case_insensitive(self):
        f = {"and": [{"column": "Tier", "op": "equals", "value": "pioneer"}]}
        assert transfer.evaluate_filter(f, ROW, HIDX) is True

    def test_is_true_false(self):
        assert (
            transfer.evaluate_filter({"and": [{"column": "Want?", "op": "is_true"}]}, ROW, HIDX)
            is True
        )
        assert (
            transfer.evaluate_filter({"and": [{"column": "Want?", "op": "is_false"}]}, ROW, HIDX)
            is False
        )

    def test_two_numeric_columns_anded(self):
        # The real ask: "≥ 250M Total Hero Power AND ≥ 75M Main March Power."
        hidx = transfer.header_index(["Total Hero Power", "Main March Power"])
        f = {
            "and": [
                {"column": "Total Hero Power", "op": ">=", "value": "250M"},
                {"column": "Main March Power", "op": ">=", "value": "75M"},
            ]
        }
        assert transfer.evaluate_filter(f, ["260M", "80M"], hidx) is True
        assert transfer.evaluate_filter(f, ["260M", "50M"], hidx) is False  # march too low
        assert transfer.evaluate_filter(f, ["200M", "80M"], hidx) is False  # power too low

    def test_missing_column_soft_passes(self):
        f = {"and": [{"column": "Ghost", "op": ">=", "value": 1}]}
        assert transfer.evaluate_filter(f, ROW, HIDX) is True

    def test_unparseable_numeric_soft_passes(self):
        row = ["Bad Pew", "Pioneer", "n/a"]
        f = {"and": [{"column": "Total Power", "op": ">=", "value": "100M"}]}
        assert transfer.evaluate_filter(f, row, HIDX) is True

    def test_raw_json_string_input(self):
        f = '{"and": [{"column": "Total Power", "op": ">=", "value": "250M"}]}'
        assert transfer.evaluate_filter(f, ROW, HIDX) is False

    def test_unknown_operator_soft_passes(self):
        f = {"and": [{"column": "Tier", "op": "regex", "value": ".*"}]}
        assert transfer.evaluate_filter(f, ROW, HIDX) is True


# ── Change detection ─────────────────────────────────────────────────────────

# A sheet with a Server column so identity can vary per row.
SRV_HEADER = ["Name", "Tier", "Total Power", "Confirmed", "Server"]
SRV_HIDX = transfer.header_index(SRV_HEADER)
SRV_MAP = {"name": "Name", "identity_extra": ["Server"], "status": ["Confirmed"]}


def _id(name, server=""):
    return transfer.identity_hash(name, server)


class TestIdentityHash:
    def test_stable(self):
        assert transfer.identity_hash("Bad Pew", "738") == transfer.identity_hash("Bad Pew", "738")

    def test_normalizes_case_and_space(self):
        assert transfer.identity_hash("Bad  Pew ") == transfer.identity_hash("bad pew")

    def test_distinct_members_differ(self):
        assert transfer.identity_hash("Bad Pew") != transfer.identity_hash("Spartan Ghost")

    def test_server_distinguishes(self):
        assert transfer.identity_hash("Bad Pew", "738") != transfer.identity_hash("Bad Pew", "739")


class TestRowIdentity:
    def test_name_plus_identity_extra(self):
        row = ["Bad Pew", "Pioneer", "199M", "", "738"]
        assert transfer.row_identity(row, SRV_HIDX, SRV_MAP) == _id("Bad Pew", "738")

    def test_blank_name_returns_none(self):
        assert transfer.row_identity(["", "Pioneer"], SRV_HIDX, SRV_MAP) is None
        assert transfer.row_identity([], SRV_HIDX, SRV_MAP) is None

    def test_excludes_power_and_tier(self):
        a = ["Bad Pew", "Pioneer", "199M", "", "738"]
        b = ["Bad Pew", "Elite", "250M", "", "738"]
        assert transfer.row_identity(a, SRV_HIDX, SRV_MAP) == transfer.row_identity(
            b, SRV_HIDX, SRV_MAP
        )


class TestStatusSnapshot:
    def test_keys_by_configured_header(self):
        snap = transfer.status_snapshot(ROW, HIDX, ["Want?", "Confirmed", "Declined"])
        assert snap == {"Want?": "TRUE", "Confirmed": "", "Declined": ""}

    def test_unresolved_status_header_skipped(self):
        assert transfer.status_snapshot(ROW, HIDX, ["Nonexistent"]) == {}

    def test_empty_status_list(self):
        assert transfer.status_snapshot(ROW, HIDX, []) == {}


class TestDisplayFields:
    def test_ordered_pairs(self):
        fields = transfer.display_fields(ROW, HIDX, ["Tier", "Total Power", "Bear vs Lion"])
        assert fields == [
            ("Tier", "Pioneer"),
            ("Total Power", "199M"),
            ("Bear vs Lion", "Pink Cat"),
        ]

    def test_skips_unresolved(self):
        fields = transfer.display_fields(ROW, HIDX, ["Tier", "Ghost"])
        assert fields == [("Tier", "Pioneer")]


class TestStatusWasSet:
    @pytest.mark.parametrize(
        "snap",
        [{"Confirmed": "TRUE"}, {"Declined": "Them"}, {"Want?": "yes", "Confirmed": ""}],
    )
    def test_set(self, snap):
        assert transfer.status_was_set(snap) is True

    @pytest.mark.parametrize(
        "snap",
        [{}, None, {"Confirmed": ""}, {"Want?": "false", "Confirmed": "no", "Declined": "0"}],
    )
    def test_not_set(self, snap):
        assert transfer.status_was_set(snap) is False


class TestDiffStatus:
    def test_detects_change(self):
        old = {"Confirmed": "false", "Want?": "true"}
        new = {"Confirmed": "true", "Want?": "true"}
        assert transfer.diff_status(old, new) == [("Confirmed", "false", "true")]

    def test_no_change(self):
        snap = {"Confirmed": "true"}
        assert transfer.diff_status(snap, dict(snap)) == []

    def test_added_key_counts_as_change(self):
        assert transfer.diff_status({}, {"Confirmed": "true"}) == [("Confirmed", "", "true")]

    def test_handles_none(self):
        assert transfer.diff_status(None, None) == []


# ── Poll orchestration ───────────────────────────────────────────────────────


class TestComputePollDiff:
    def test_baseline_announces_nothing_but_records_state(self):
        rows = [
            ["Bad Pew", "Pioneer", "199M", "", "738"],
            ["Spartan Ghost", "Elite", "250M", "TRUE", "739"],
        ]
        diff = transfer.compute_poll_diff(rows, SRV_HIDX, SRV_MAP, prior_state={}, baseline=True)
        assert diff.new_applicants == []
        assert diff.status_changes == []
        assert diff.deletions == []
        assert set(diff.next_state) == {_id("Bad Pew", "738"), _id("Spartan Ghost", "739")}

    def test_new_applicant_fires(self):
        rows = [["Bad Pew", "Pioneer", "199M", "", "738"]]
        diff = transfer.compute_poll_diff(rows, SRV_HIDX, SRV_MAP, prior_state={})
        assert len(diff.new_applicants) == 1
        assert diff.new_applicants[0].hash == _id("Bad Pew", "738")

    def test_next_state_carries_name_and_status(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        diff = transfer.compute_poll_diff(rows, SRV_HIDX, SRV_MAP, prior_state={})
        entry = diff.next_state[_id("Bad Pew", "738")]
        assert entry == {"name": "Bad Pew", "status": {"Confirmed": "TRUE"}}

    def test_new_applicant_filtered_out_still_bookmarked(self):
        rows = [["Low Guy", "Pioneer", "10M", "", "738"]]
        f = {"and": [{"column": "Total Power", "op": ">=", "value": "100M"}]}
        diff = transfer.compute_poll_diff(rows, SRV_HIDX, SRV_MAP, prior_state={}, filter_obj=f)
        assert diff.new_applicants == []
        assert _id("Low Guy", "738") in diff.next_state

    def test_status_change_fires(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        h = _id("Bad Pew", "738")
        diff = transfer.compute_poll_diff(
            rows, SRV_HIDX, SRV_MAP, prior_state={h: {"Confirmed": "FALSE"}}
        )
        assert len(diff.status_changes) == 1
        assert diff.status_changes[0].changes == [("Confirmed", "FALSE", "TRUE")]

    def test_status_change_not_filtered(self):
        rows = [["Low Guy", "Pioneer", "10M", "TRUE", "738"]]
        h = _id("Low Guy", "738")
        f = {"and": [{"column": "Total Power", "op": ">=", "value": "100M"}]}
        diff = transfer.compute_poll_diff(
            rows, SRV_HIDX, SRV_MAP, prior_state={h: {"Confirmed": ""}}, filter_obj=f
        )
        assert len(diff.status_changes) == 1

    def test_no_change_is_quiet(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        h = _id("Bad Pew", "738")
        diff = transfer.compute_poll_diff(
            rows, SRV_HIDX, SRV_MAP, prior_state={h: {"Confirmed": "TRUE"}}
        )
        assert diff.new_applicants == []
        assert diff.status_changes == []

    def test_blank_rows_skipped(self):
        rows = [["", "", "", "", ""], ["Bad Pew", "Pioneer", "199M", "", "738"]]
        diff = transfer.compute_poll_diff(rows, SRV_HIDX, SRV_MAP, prior_state={})
        assert len(diff.new_applicants) == 1
        assert len(diff.next_state) == 1

    def test_same_name_different_server_distinct(self):
        rows = [
            ["Bad Pew", "Pioneer", "199M", "", "738"],
            ["Bad Pew", "Pioneer", "199M", "", "739"],
        ]
        diff = transfer.compute_poll_diff(rows, SRV_HIDX, SRV_MAP, prior_state={})
        assert len(diff.new_applicants) == 2


class TestDeletions:
    def test_status_bearing_deletion_surfaces(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        keep, gone = _id("Bad Pew", "738"), _id("Old Guy", "700")
        prior = {keep: {"Confirmed": "TRUE"}, gone: {"Confirmed": "TRUE"}}
        diff = transfer.compute_poll_diff(rows, SRV_HIDX, SRV_MAP, prior_state=prior)
        assert [d.hash for d in diff.deletions] == [gone]
        assert gone not in diff.next_state
        assert keep in diff.next_state

    def test_pending_deletion_not_surfaced(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        gone = _id("Pending Guy", "700")
        prior = {_id("Bad Pew", "738"): {"Confirmed": "TRUE"}, gone: {"Confirmed": ""}}
        diff = transfer.compute_poll_diff(rows, SRV_HIDX, SRV_MAP, prior_state=prior)
        assert diff.deletions == []
        assert gone not in diff.next_state

    def test_baseline_never_surfaces_deletions(self):
        prior = {_id("Old Guy", "700"): {"Confirmed": "TRUE"}}
        diff = transfer.compute_poll_diff([], SRV_HIDX, SRV_MAP, prior_state=prior, baseline=True)
        assert diff.deletions == []

    def test_deletion_carries_name_from_state(self):
        # New-shape prior state ({name, status}) → the removal notice can name them.
        gone = _id("Old Guy", "700")
        prior = {gone: {"name": "Old Guy", "status": {"Confirmed": "TRUE"}}}
        diff = transfer.compute_poll_diff([], SRV_HIDX, SRV_MAP, prior_state=prior)
        assert len(diff.deletions) == 1
        assert diff.deletions[0].name == "Old Guy"
        assert diff.deletions[0].snapshot == {"Confirmed": "TRUE"}

    def test_legacy_bare_snapshot_state_still_diffs(self):
        # A state written by an older build (bare snapshot, no name) still works.
        h = _id("Bad Pew", "738")
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        diff = transfer.compute_poll_diff(
            rows, SRV_HIDX, SRV_MAP, prior_state={h: {"Confirmed": "FALSE"}}
        )
        assert diff.status_changes[0].changes == [("Confirmed", "FALSE", "TRUE")]


class TestPollIsDue:
    NOW = datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc)

    def test_never_polled_is_due(self):
        assert transfer.poll_is_due("", 60, self.NOW) is True
        assert transfer.poll_is_due(None, 60, self.NOW) is True

    def test_bad_timestamp_is_due(self):
        assert transfer.poll_is_due("not-a-date", 60, self.NOW) is True

    def test_recent_is_not_due(self):
        last = (self.NOW - timedelta(minutes=30)).isoformat()
        assert transfer.poll_is_due(last, 60, self.NOW) is False

    def test_elapsed_is_due(self):
        last = (self.NOW - timedelta(minutes=61)).isoformat()
        assert transfer.poll_is_due(last, 60, self.NOW) is True

    def test_naive_timestamp_treated_as_utc(self):
        last = (self.NOW - timedelta(minutes=90)).replace(tzinfo=None).isoformat()
        assert transfer.poll_is_due(last, 60, self.NOW) is True


class TestSelectRowsToCopy:
    SRC_HEADER = ["Name", "Power", "Preferred Alliance"]
    SRC_HIDX = transfer.header_index(SRC_HEADER)
    SRC_MAP = {"name": "Name"}

    def test_copies_matching_uncopied_rows(self):
        rows = [["Bad Pew", "199M", "OGV"], ["Other", "50M", "XYZ"]]
        f = {"and": [{"column": "Preferred Alliance", "op": "contains", "value": "OGV"}]}
        to_copy, copied = transfer.select_rows_to_copy(
            rows, self.SRC_HIDX, self.SRC_MAP, already_copied=set(), filter_obj=f
        )
        assert to_copy == [["Bad Pew", "199M", "OGV"]]
        assert transfer.identity_hash("Bad Pew") in copied

    def test_dedups_already_copied(self):
        rows = [["Bad Pew", "199M", "OGV"]]
        seen = {transfer.identity_hash("Bad Pew")}
        to_copy, copied = transfer.select_rows_to_copy(
            rows, self.SRC_HIDX, self.SRC_MAP, already_copied=seen
        )
        assert to_copy == []
        assert copied == seen

    def test_no_filter_copies_all_new(self):
        rows = [["A", "1M", "X"], ["B", "2M", "Y"]]
        to_copy, copied = transfer.select_rows_to_copy(
            rows, self.SRC_HIDX, self.SRC_MAP, already_copied=set()
        )
        assert len(to_copy) == 2
        assert len(copied) == 2

    def test_blank_name_skipped(self):
        rows = [["", "1M", "X"], ["B", "2M", "Y"]]
        to_copy, _ = transfer.select_rows_to_copy(
            rows, self.SRC_HIDX, self.SRC_MAP, already_copied=set()
        )
        assert to_copy == [["B", "2M", "Y"]]


# ── Templates ────────────────────────────────────────────────────────────────


class TestFieldToken:
    @pytest.mark.parametrize(
        "header,token",
        [
            ("Total Hero Power", "total_hero_power"),
            ("Tier", "tier"),
            ("  Arena  Power ", "arena_power"),
        ],
    )
    def test_token(self, header, token):
        assert transfer.field_token(header) == token


class TestRenderTemplate:
    def test_name_substitution(self):
        assert transfer.render_transfer_template("Hi {name}!", name="Bad Pew") == "Hi Bad Pew!"

    def test_display_column_token(self):
        out = transfer.render_transfer_template(
            "{name}: {total_hero_power}", name="Bad Pew", total_hero_power="199M"
        )
        assert out == "Bad Pew: 199M"

    def test_unknown_placeholder_renders_literally(self):
        assert transfer.render_transfer_template("Hi {nme}", name="Bad Pew") == "Hi {nme}"

    def test_missing_known_key_renders_blank(self):
        assert transfer.render_transfer_template("Hi {name}{alliance_name}", name="X") == "Hi X"

    def test_none_value_renders_blank(self):
        assert transfer.render_transfer_template("Hi {name}", name=None) == "Hi "

    def test_stray_brace_does_not_crash(self):
        assert isinstance(transfer.render_transfer_template("100% {name", name="X"), str)


class TestResolveTemplate:
    def test_uses_default_when_blank(self):
        assert (
            transfer.resolve_template({"template_apply_invitation": ""}, "apply_invitation")
            == (DEFAULT_TRANSFER_TEMPLATES["apply_invitation"])
        )

    def test_uses_saved_override(self):
        cfg = {"template_decline": "Custom decline {name}"}
        assert transfer.resolve_template(cfg, "decline") == "Custom decline {name}"

    def test_whitespace_only_falls_back(self):
        assert (
            transfer.resolve_template({"template_confirm_request": "   "}, "confirm_request")
            == (DEFAULT_TRANSFER_TEMPLATES["confirm_request"])
        )

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            transfer.resolve_template({}, "bogus")

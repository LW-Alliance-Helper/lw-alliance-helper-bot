"""Unit tests for transfer.py — the Transfer Management (#16) pure-logic
core: column-map addressing, the AND-only filter DSL, change detection
(identity hashing + status snapshots/diffs), value coercion, and in-game
message template rendering.

All side-effect-free; no DB / Discord needed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import transfer
from defaults import DEFAULT_TRANSFER_TEMPLATES


# A representative column map: Member name + Power + Tier + status columns +
# a free-form extra.
MAP = {
    "member": "A",
    "power": "C",
    "tier": "B",
    "want": "D",
    "confirmed": "E",
    "declined": "F",
    "notes": "G",
    "extras": [{"label": "Bear vs Lion", "letter": "H"}],
}


# ── Column addressing ────────────────────────────────────────────────────────


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


class TestParseColumnMap:
    def test_passthrough_dict(self):
        assert transfer.parse_column_map(MAP) is MAP

    def test_json_string(self):
        assert transfer.parse_column_map('{"member": "A"}') == {"member": "A"}

    @pytest.mark.parametrize("bad", ["", None, "{not json", "[]", "42"])
    def test_bad_input_returns_empty(self, bad):
        assert transfer.parse_column_map(bad) == {}


class TestColumnIndex:
    def test_top_level_key(self):
        assert transfer.column_index(MAP, "power") == 2

    def test_extra_by_label_case_insensitive(self):
        assert transfer.column_index(MAP, "bear vs lion") == 7

    def test_unmapped_key(self):
        assert transfer.column_index(MAP, "nope") is None

    def test_empty_key(self):
        assert transfer.column_index(MAP, "") is None


class TestCellValue:
    ROW = ["Bad Pew", "Pioneer", "199M", "TRUE", "", "", "note", "Pink Cat"]

    def test_mapped(self):
        assert transfer.cell_value(self.ROW, MAP, "member") == "Bad Pew"
        assert transfer.cell_value(self.ROW, MAP, "power") == "199M"
        assert transfer.cell_value(self.ROW, MAP, "bear vs lion") == "Pink Cat"

    def test_unmapped_returns_none(self):
        assert transfer.cell_value(self.ROW, MAP, "missing") is None

    def test_row_too_short_returns_none(self):
        assert transfer.cell_value(["only one"], MAP, "power") is None

    def test_strips_whitespace(self):
        assert transfer.cell_value(["  spaced  "], {"member": "A"}, "member") == "spaced"


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
        ],
    )
    def test_parses(self, value, expected):
        assert transfer.coerce_number(value) == pytest.approx(expected)

    @pytest.mark.parametrize("bad", ["", "  ", "abc", None, "M", [1]])
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
        f = transfer.parse_filter('{"and": [{"column": "power", "op": ">=", "value": 100}]}')
        assert f == {"and": [{"column": "power", "op": ">=", "value": 100}]}


class TestEvaluateFilter:
    # Row: Bad Pew, Pioneer, 199M, want=TRUE, ...
    ROW = ["Bad Pew", "Pioneer", "199M", "TRUE", "", "", "", ""]

    def test_no_filter_passes_everything(self):
        assert transfer.evaluate_filter(None, self.ROW, MAP) is True
        assert transfer.evaluate_filter({}, self.ROW, MAP) is True
        assert transfer.evaluate_filter("", self.ROW, MAP) is True

    def test_numeric_ge_pass(self):
        f = {"and": [{"column": "power", "op": ">=", "value": "100M"}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is True

    def test_numeric_ge_fail(self):
        f = {"and": [{"column": "power", "op": ">=", "value": "250M"}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is False

    def test_in_list(self):
        f = {"and": [{"column": "tier", "op": "in", "value": ["Pioneer", "Contributor"]}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is True
        f2 = {"and": [{"column": "tier", "op": "in", "value": ["Elite"]}]}
        assert transfer.evaluate_filter(f2, self.ROW, MAP) is False

    def test_contains(self):
        f = {"and": [{"column": "tier", "op": "contains", "value": "ion"}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is True

    def test_equals_case_insensitive(self):
        f = {"and": [{"column": "tier", "op": "equals", "value": "pioneer"}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is True

    def test_is_true(self):
        f = {"and": [{"column": "want", "op": "is_true", "value": None}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is True

    def test_is_false_on_true_cell_fails(self):
        f = {"and": [{"column": "want", "op": "is_false", "value": None}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is False

    def test_and_of_multiple(self):
        f = {
            "and": [
                {"column": "power", "op": ">=", "value": "100M"},
                {"column": "tier", "op": "in", "value": ["Pioneer"]},
            ]
        }
        assert transfer.evaluate_filter(f, self.ROW, MAP) is True
        f["and"][1]["value"] = ["Elite"]
        assert transfer.evaluate_filter(f, self.ROW, MAP) is False

    def test_missing_column_soft_passes(self):
        # Filter references a column that isn't mapped → don't drop the row.
        f = {"and": [{"column": "ghost", "op": ">=", "value": 1}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is True

    def test_unparseable_numeric_soft_passes(self):
        row = ["Bad Pew", "Pioneer", "n/a"]
        f = {"and": [{"column": "power", "op": ">=", "value": "100M"}]}
        assert transfer.evaluate_filter(f, row, MAP) is True

    def test_raw_json_string_input(self):
        f = '{"and": [{"column": "power", "op": ">=", "value": "250M"}]}'
        assert transfer.evaluate_filter(f, self.ROW, MAP) is False

    def test_unknown_operator_soft_passes(self):
        f = {"and": [{"column": "tier", "op": "regex", "value": ".*"}]}
        assert transfer.evaluate_filter(f, self.ROW, MAP) is True


# ── Change detection ─────────────────────────────────────────────────────────


class TestIdentityHash:
    def test_stable(self):
        a = transfer.identity_hash("Bad Pew", "FSU", "738")
        b = transfer.identity_hash("Bad Pew", "FSU", "738")
        assert a == b

    def test_normalizes_case_and_space(self):
        a = transfer.identity_hash("Bad  Pew ", "fsu", "738")
        b = transfer.identity_hash("bad pew", "FSU", "738")
        assert a == b

    def test_distinct_members_differ(self):
        assert transfer.identity_hash("Bad Pew") != transfer.identity_hash("Spartan Ghost")

    def test_excludes_power_and_tier(self):
        # Same identity, different power/tier → same hash (power/tier never
        # feed the hash, only the immutable identity fields do).
        row_a = ["Bad Pew", "Pioneer", "199M"]
        row_b = ["Bad Pew", "Elite", "250M"]
        assert transfer.row_identity(row_a, MAP) == transfer.row_identity(row_b, MAP)

    def test_server_distinguishes(self):
        assert transfer.identity_hash("Bad Pew", "FSU", "738") != transfer.identity_hash(
            "Bad Pew", "FSU", "739"
        )


class TestRowIdentity:
    def test_blank_member_returns_none(self):
        assert transfer.row_identity(["", "Pioneer"], MAP) is None
        assert transfer.row_identity([], MAP) is None

    def test_passes_alliance_server(self):
        h = transfer.row_identity(["Bad Pew"], {"member": "A"}, alliance="FSU", server="738")
        assert h == transfer.identity_hash("Bad Pew", "FSU", "738")


class TestStatusSnapshot:
    def test_only_mapped_status_keys(self):
        row = ["Bad Pew", "Pioneer", "199M", "TRUE", "yes", "", "note", "Pink Cat"]
        snap = transfer.status_snapshot(row, MAP)
        assert snap == {"want": "TRUE", "confirmed": "yes", "declined": ""}

    def test_omits_unmapped_status(self):
        # Map with no status columns → empty snapshot, only "new applicant"
        # notifications are ever possible.
        snap = transfer.status_snapshot(["Bad Pew"], {"member": "A"})
        assert snap == {}


class TestDiffStatus:
    def test_detects_change(self):
        old = {"confirmed": "false", "want": "true"}
        new = {"confirmed": "true", "want": "true"}
        assert transfer.diff_status(old, new) == [("confirmed", "false", "true")]

    def test_no_change(self):
        snap = {"confirmed": "true"}
        assert transfer.diff_status(snap, dict(snap)) == []

    def test_added_key_counts_as_change(self):
        assert transfer.diff_status({}, {"confirmed": "true"}) == [("confirmed", "", "true")]

    def test_handles_none(self):
        assert transfer.diff_status(None, None) == []


# ── Templates ────────────────────────────────────────────────────────────────


class TestRenderTemplate:
    def test_name_substitution(self):
        out = transfer.render_transfer_template("Hi {name}!", name="Bad Pew")
        assert out == "Hi Bad Pew!"

    def test_multiple_placeholders(self):
        out = transfer.render_transfer_template(
            "{name} ({tier}) for {alliance_name}",
            name="Bad Pew",
            tier="Pioneer",
            alliance_name="OGV",
        )
        assert out == "Bad Pew (Pioneer) for OGV"

    def test_unknown_placeholder_renders_literally(self):
        out = transfer.render_transfer_template("Hi {nme}", name="Bad Pew")
        assert out == "Hi {nme}"

    def test_missing_key_renders_blank(self):
        out = transfer.render_transfer_template("Hi {name}{tier}", name="Bad Pew")
        assert out == "Hi Bad Pew"

    def test_none_value_renders_blank(self):
        out = transfer.render_transfer_template("Hi {name}", name=None)
        assert out == "Hi "

    def test_stray_brace_does_not_crash(self):
        # A lone unescaped brace would raise in str.format; we fall back.
        out = transfer.render_transfer_template("100% {name", name="X")
        assert isinstance(out, str)


class TestResolveTemplate:
    def test_uses_default_when_blank(self):
        cfg = {"template_apply_invitation": ""}
        assert (
            transfer.resolve_template(cfg, "apply_invitation")
            == (DEFAULT_TRANSFER_TEMPLATES["apply_invitation"])
        )

    def test_uses_saved_override(self):
        cfg = {"template_decline": "Custom decline {name}"}
        assert transfer.resolve_template(cfg, "decline") == "Custom decline {name}"

    def test_whitespace_only_falls_back(self):
        cfg = {"template_confirm_request": "   "}
        assert (
            transfer.resolve_template(cfg, "confirm_request")
            == (DEFAULT_TRANSFER_TEMPLATES["confirm_request"])
        )

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError):
            transfer.resolve_template({}, "bogus")


# ── Poll orchestration ───────────────────────────────────────────────────────

# Map with a "Server" extra so identity can vary per row.
SRV_MAP = {
    "member": "A",
    "tier": "B",
    "power": "C",
    "confirmed": "D",
    "extras": [{"label": "Server", "letter": "E"}],
}


def _identity(member, server="", alliance="OGV"):
    return transfer.identity_hash(member, alliance, server)


class TestComputePollDiff:
    def test_baseline_announces_nothing_but_records_state(self):
        rows = [
            ["Bad Pew", "Pioneer", "199M", "", "738"],
            ["Spartan Ghost", "Elite", "250M", "TRUE", "739"],
        ]
        diff = transfer.compute_poll_diff(
            rows, SRV_MAP, prior_state={}, alliance="OGV", baseline=True
        )
        assert diff.new_applicants == []
        assert diff.status_changes == []
        assert set(diff.next_state) == {
            _identity("Bad Pew", "738"),
            _identity("Spartan Ghost", "739"),
        }

    def test_new_applicant_fires(self):
        rows = [["Bad Pew", "Pioneer", "199M", "", "738"]]
        diff = transfer.compute_poll_diff(rows, SRV_MAP, prior_state={}, alliance="OGV")
        assert len(diff.new_applicants) == 1
        assert diff.new_applicants[0].hash == _identity("Bad Pew", "738")
        assert diff.new_applicants[0].row == rows[0]

    def test_new_applicant_filtered_out_still_bookmarked(self):
        rows = [["Low Guy", "Pioneer", "10M", "", "738"]]
        filt = {"and": [{"column": "power", "op": ">=", "value": "100M"}]}
        diff = transfer.compute_poll_diff(
            rows, SRV_MAP, prior_state={}, filter_obj=filt, alliance="OGV"
        )
        # Filtered out → no notification...
        assert diff.new_applicants == []
        # ...but recorded so it won't re-fire if the filter later loosens.
        assert _identity("Low Guy", "738") in diff.next_state

    def test_status_change_fires(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        h = _identity("Bad Pew", "738")
        prior = {h: {"confirmed": "FALSE"}}
        diff = transfer.compute_poll_diff(rows, SRV_MAP, prior_state=prior, alliance="OGV")
        assert diff.new_applicants == []
        assert len(diff.status_changes) == 1
        sc = diff.status_changes[0]
        assert sc.changes == [("confirmed", "FALSE", "TRUE")]

    def test_status_change_not_filtered(self):
        # A low-power applicant marked Confirmed must still notify even when a
        # new-applicant power filter is set — status changes bypass the filter.
        rows = [["Low Guy", "Pioneer", "10M", "TRUE", "738"]]
        h = _identity("Low Guy", "738")
        prior = {h: {"confirmed": ""}}
        filt = {"and": [{"column": "power", "op": ">=", "value": "100M"}]}
        diff = transfer.compute_poll_diff(
            rows, SRV_MAP, prior_state=prior, filter_obj=filt, alliance="OGV"
        )
        assert len(diff.status_changes) == 1

    def test_no_change_is_quiet(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        h = _identity("Bad Pew", "738")
        prior = {h: {"confirmed": "TRUE"}}
        diff = transfer.compute_poll_diff(rows, SRV_MAP, prior_state=prior, alliance="OGV")
        assert diff.new_applicants == []
        assert diff.status_changes == []

    def test_deleted_row_is_forgotten_silently(self):
        # Prior state has a row that's no longer present → dropped from
        # next_state, no notification.
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        gone = _identity("Old Applicant", "700")
        keep = _identity("Bad Pew", "738")
        prior = {gone: {"confirmed": ""}, keep: {"confirmed": "TRUE"}}
        diff = transfer.compute_poll_diff(rows, SRV_MAP, prior_state=prior, alliance="OGV")
        assert gone not in diff.next_state
        assert keep in diff.next_state
        assert diff.status_changes == []

    def test_blank_rows_skipped(self):
        rows = [["", "", "", "", ""], ["Bad Pew", "Pioneer", "199M", "", "738"]]
        diff = transfer.compute_poll_diff(rows, SRV_MAP, prior_state={}, alliance="OGV")
        assert len(diff.new_applicants) == 1
        assert len(diff.next_state) == 1

    def test_same_name_different_server_are_distinct(self):
        rows = [
            ["Bad Pew", "Pioneer", "199M", "", "738"],
            ["Bad Pew", "Pioneer", "199M", "", "739"],
        ]
        diff = transfer.compute_poll_diff(rows, SRV_MAP, prior_state={}, alliance="OGV")
        assert len(diff.new_applicants) == 2
        assert len(diff.next_state) == 2


class TestSelectRowsToCopy:
    SRC_MAP = {
        "member": "A",
        "power": "B",
        "extras": [{"label": "Preferred Alliance", "letter": "C"}],
    }

    def test_copies_matching_uncopied_rows(self):
        rows = [["Bad Pew", "199M", "OGV"], ["Other", "50M", "XYZ"]]
        filt = {"and": [{"column": "Preferred Alliance", "op": "contains", "value": "OGV"}]}
        to_copy, copied = transfer.select_rows_to_copy(
            rows, self.SRC_MAP, already_copied=set(), filter_obj=filt
        )
        assert to_copy == [["Bad Pew", "199M", "OGV"]]
        assert transfer.identity_hash("Bad Pew") in copied

    def test_dedups_already_copied(self):
        rows = [["Bad Pew", "199M", "OGV"]]
        seen = {transfer.identity_hash("Bad Pew")}
        to_copy, copied = transfer.select_rows_to_copy(rows, self.SRC_MAP, already_copied=seen)
        assert to_copy == []
        assert copied == seen

    def test_no_filter_copies_all_new(self):
        rows = [["A", "1M", "X"], ["B", "2M", "Y"]]
        to_copy, copied = transfer.select_rows_to_copy(rows, self.SRC_MAP, already_copied=set())
        assert len(to_copy) == 2
        assert len(copied) == 2

    def test_blank_member_skipped(self):
        rows = [["", "1M", "X"], ["B", "2M", "Y"]]
        to_copy, _ = transfer.select_rows_to_copy(rows, self.SRC_MAP, already_copied=set())
        assert to_copy == [["B", "2M", "Y"]]

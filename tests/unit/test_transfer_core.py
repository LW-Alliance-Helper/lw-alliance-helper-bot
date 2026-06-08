"""Unit tests for transfer.py — the Transfer Management (#16) pure-logic
core: header-name column resolution, the AND-only filter DSL, change
detection (identity hashing + status snapshots/diffs/deletions), value
coercion, and in-game message template rendering.

All side-effect-free; no DB / Discord needed.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest

import transfer
from defaults import DEFAULT_TRANSFER_TEMPLATES


# A resolved column map (what resolve_columns produces): normalised logical
# key / extra label → 0-based column index. Rows below are aligned to it.
RES = {
    "member": 0,
    "tier": 1,
    "power": 2,
    "want": 3,
    "confirmed": 4,
    "declined": 5,
    "notes": 6,
    "bear vs lion": 7,
}


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


# ── Column-map decode + header resolution ────────────────────────────────────


class TestParseColumnMap:
    MAP = {"member": "Name"}

    def test_passthrough_dict(self):
        assert transfer.parse_column_map(self.MAP) is self.MAP

    def test_json_string(self):
        assert transfer.parse_column_map('{"member": "Name"}') == {"member": "Name"}

    @pytest.mark.parametrize("bad", ["", None, "{not json", "[]", "42"])
    def test_bad_input_returns_empty(self, bad):
        assert transfer.parse_column_map(bad) == {}


class TestResolveColumns:
    HEADER = ["Name", "Tier", "Total Power", "Want?", "Confirmed", "Server", "Notes"]
    MAP = {
        "member": "Name",
        "tier": "Tier",
        "power": "total power",  # case-insensitive match
        "want": "Want?",
        "confirmed": "Confirmed",
        "server": "Server",
        "notes": "Notes",
        "extras": [{"label": "Bear vs Lion", "header": "BvL (missing)"}],
    }

    def test_resolves_top_level_keys(self):
        res = transfer.resolve_columns(self.HEADER, self.MAP)
        assert res["member"] == 0
        assert res["tier"] == 1
        assert res["power"] == 2  # matched case-insensitively
        assert res["confirmed"] == 4
        assert res["server"] == 5

    def test_missing_header_is_dropped(self):
        # "BvL (missing)" isn't in the header row → the extra disappears.
        res = transfer.resolve_columns(self.HEADER, self.MAP)
        assert "bear vs lion" not in res

    def test_extra_resolved_by_label(self):
        header = ["Name", "BvL Result"]
        m = {"member": "Name", "extras": [{"label": "Bear vs Lion", "header": "BvL Result"}]}
        res = transfer.resolve_columns(header, m)
        assert res["bear vs lion"] == 1

    def test_whitespace_and_case_insensitive(self):
        header = ["  NAME  ", "total   power"]
        m = {"member": "name", "power": "Total Power"}
        res = transfer.resolve_columns(header, m)
        assert res == {"member": 0, "power": 1}

    def test_letter_fallback_when_no_header_match(self):
        # A configured value that matches no header but looks like a bare
        # column letter is taken literally (power-user escape hatch).
        header = ["Col0", "Col1", "Col2"]
        m = {"member": "C"}
        res = transfer.resolve_columns(header, m)
        assert res["member"] == 2

    def test_legacy_letter_key_on_extra(self):
        header = ["Name", "Whatever"]
        m = {"member": "Name", "extras": [{"label": "X", "letter": "B"}]}
        res = transfer.resolve_columns(header, m)
        assert res["x"] == 1

    def test_top_level_wins_over_colliding_extra(self):
        header = ["Notes Col"]
        m = {"notes": "Notes Col", "extras": [{"label": "notes", "header": "Notes Col"}]}
        res = transfer.resolve_columns(header, m)
        assert res["notes"] == 0


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
        "What timezone are you in? (PST, MST, CST, EST)",
        "Have you transferred servers before?",
        "Why are you interested in transferring?",
        "Requested Landing Alliance",
        "Are you a part of any pairs or groups applying",
        "Do you know anyone currently playing in 738",
        "Long time gamer or rookie?",
    ]

    def test_maps_identity_columns_on_real_sheet(self):
        s = transfer.suggest_column_map(self.FORM_HEADER)
        assert s["member"] == "In Game Username"
        assert s["server"] == "Current Server"
        assert s["alliance"] == "Current Alliance"
        assert s["tier"] == "Anticipated Seat Color"

    def test_strongest_power_column_wins(self):
        # Three power-ish columns; only the best (exact "Total Hero Power")
        # is claimed — the others are left for the recruiter to add as extras.
        s = transfer.suggest_column_map(self.FORM_HEADER)
        assert s["power"] == "Total Hero Power"

    def test_intake_sheet_has_no_status_columns(self):
        # A form/intake sheet carries no want/confirmed/declined columns, and
        # the prose "Why are you interested in transferring?" must NOT be
        # mistaken for a "want" status column.
        s = transfer.suggest_column_map(self.FORM_HEADER)
        assert "want" not in s
        assert "confirmed" not in s
        assert "declined" not in s

    def test_alliance_curated_sheet_maps_status(self):
        header = ["Name", "Total Power", "Want?", "Confirmed", "Declined", "Notes"]
        s = transfer.suggest_column_map(header)
        assert s["member"] == "Name"
        assert s["want"] == "Want?"
        assert s["confirmed"] == "Confirmed"
        assert s["declined"] == "Declined"
        assert s["notes"] == "Notes"

    def test_no_match_omits_key(self):
        s = transfer.suggest_column_map(["Foo", "Bar", "Baz"])
        assert s == {}

    def test_each_header_claimed_once(self):
        # "Server" shouldn't be claimed by two keys.
        s = transfer.suggest_column_map(["Server", "Server"])
        assert list(s.values()).count("Server") == 1


class TestCellValue:
    ROW = ["Bad Pew", "Pioneer", "199M", "TRUE", "", "", "note", "Pink Cat"]

    def test_mapped(self):
        assert transfer.cell_value(self.ROW, RES, "member") == "Bad Pew"
        assert transfer.cell_value(self.ROW, RES, "power") == "199M"
        # Extra lookup is case-insensitive against the normalised label.
        assert transfer.cell_value(self.ROW, RES, "Bear vs Lion") == "Pink Cat"

    def test_unmapped_returns_none(self):
        assert transfer.cell_value(self.ROW, RES, "missing") is None

    def test_row_too_short_returns_none(self):
        assert transfer.cell_value(["only one"], RES, "power") is None

    def test_strips_whitespace(self):
        assert transfer.cell_value(["  spaced  "], {"member": 0}, "member") == "spaced"


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
            # Real-world messy formats pulled from a live transfer sheet.
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
        # "174,5" (174.5 in EU notation) is read as thousands → 1745. Pinned
        # so the behavior is intentional, not an accident.
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
        f = transfer.parse_filter('{"and": [{"column": "power", "op": ">=", "value": 100}]}')
        assert f == {"and": [{"column": "power", "op": ">=", "value": 100}]}


class TestEvaluateFilter:
    # Row aligned to RES: Bad Pew, Pioneer, 199M, want=TRUE, ...
    ROW = ["Bad Pew", "Pioneer", "199M", "TRUE", "", "", "", ""]

    def test_no_filter_passes_everything(self):
        assert transfer.evaluate_filter(None, self.ROW, RES) is True
        assert transfer.evaluate_filter({}, self.ROW, RES) is True
        assert transfer.evaluate_filter("", self.ROW, RES) is True

    def test_numeric_ge_pass(self):
        f = {"and": [{"column": "power", "op": ">=", "value": "100M"}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is True

    def test_numeric_ge_fail(self):
        f = {"and": [{"column": "power", "op": ">=", "value": "250M"}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is False

    def test_in_list(self):
        f = {"and": [{"column": "tier", "op": "in", "value": ["Pioneer", "Contributor"]}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is True
        f2 = {"and": [{"column": "tier", "op": "in", "value": ["Elite"]}]}
        assert transfer.evaluate_filter(f2, self.ROW, RES) is False

    def test_contains(self):
        f = {"and": [{"column": "tier", "op": "contains", "value": "ion"}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is True

    def test_equals_case_insensitive(self):
        f = {"and": [{"column": "tier", "op": "equals", "value": "pioneer"}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is True

    def test_is_true(self):
        f = {"and": [{"column": "want", "op": "is_true", "value": None}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is True

    def test_is_false_on_true_cell_fails(self):
        f = {"and": [{"column": "want", "op": "is_false", "value": None}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is False

    def test_and_of_multiple(self):
        f = {
            "and": [
                {"column": "power", "op": ">=", "value": "100M"},
                {"column": "tier", "op": "in", "value": ["Pioneer"]},
            ]
        }
        assert transfer.evaluate_filter(f, self.ROW, RES) is True
        f["and"][1]["value"] = ["Elite"]
        assert transfer.evaluate_filter(f, self.ROW, RES) is False

    def test_missing_column_soft_passes(self):
        # Filter references a column that isn't resolved → don't drop the row.
        f = {"and": [{"column": "ghost", "op": ">=", "value": 1}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is True

    def test_unparseable_numeric_soft_passes(self):
        row = ["Bad Pew", "Pioneer", "n/a"]
        f = {"and": [{"column": "power", "op": ">=", "value": "100M"}]}
        assert transfer.evaluate_filter(f, row, RES) is True

    def test_raw_json_string_input(self):
        f = '{"and": [{"column": "power", "op": ">=", "value": "250M"}]}'
        assert transfer.evaluate_filter(f, self.ROW, RES) is False

    def test_unknown_operator_soft_passes(self):
        f = {"and": [{"column": "tier", "op": "regex", "value": ".*"}]}
        assert transfer.evaluate_filter(f, self.ROW, RES) is True


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
        # Same identity, different power/tier → same hash.
        row_a = ["Bad Pew", "Pioneer", "199M"]
        row_b = ["Bad Pew", "Elite", "250M"]
        assert transfer.row_identity(row_a, RES) == transfer.row_identity(row_b, RES)

    def test_server_distinguishes(self):
        assert transfer.identity_hash("Bad Pew", "FSU", "738") != transfer.identity_hash(
            "Bad Pew", "FSU", "739"
        )


class TestRowIdentity:
    def test_blank_member_returns_none(self):
        assert transfer.row_identity(["", "Pioneer"], RES) is None
        assert transfer.row_identity([], RES) is None

    def test_passes_alliance_server(self):
        h = transfer.row_identity(["Bad Pew"], {"member": 0}, alliance="FSU", server="738")
        assert h == transfer.identity_hash("Bad Pew", "FSU", "738")


class TestStatusSnapshot:
    def test_only_mapped_status_keys(self):
        row = ["Bad Pew", "Pioneer", "199M", "TRUE", "yes", "", "note", "Pink Cat"]
        snap = transfer.status_snapshot(row, RES)
        assert snap == {"want": "TRUE", "confirmed": "yes", "declined": ""}

    def test_omits_unmapped_status(self):
        snap = transfer.status_snapshot(["Bad Pew"], {"member": 0})
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


class TestStatusWasSet:
    @pytest.mark.parametrize(
        "snap",
        [
            {"confirmed": "TRUE"},
            {"declined": "Them"},
            {"want": "yes", "confirmed": ""},
        ],
    )
    def test_set(self, snap):
        assert transfer.status_was_set(snap) is True

    @pytest.mark.parametrize(
        "snap",
        [
            {},
            None,
            {"confirmed": ""},
            {"want": "false", "confirmed": "no", "declined": "0"},
        ],
    )
    def test_not_set(self, snap):
        assert transfer.status_was_set(snap) is False


# ── Poll orchestration ───────────────────────────────────────────────────────

# Resolved map with a "server" column so identity can vary per row.
SRV = {"member": 0, "tier": 1, "power": 2, "confirmed": 3, "server": 4}


def _identity(member, server="", alliance="OGV"):
    return transfer.identity_hash(member, alliance, server)


class TestComputePollDiff:
    def test_baseline_announces_nothing_but_records_state(self):
        rows = [
            ["Bad Pew", "Pioneer", "199M", "", "738"],
            ["Spartan Ghost", "Elite", "250M", "TRUE", "739"],
        ]
        diff = transfer.compute_poll_diff(rows, SRV, prior_state={}, alliance="OGV", baseline=True)
        assert diff.new_applicants == []
        assert diff.status_changes == []
        assert diff.deletions == []
        assert set(diff.next_state) == {
            _identity("Bad Pew", "738"),
            _identity("Spartan Ghost", "739"),
        }

    def test_new_applicant_fires(self):
        rows = [["Bad Pew", "Pioneer", "199M", "", "738"]]
        diff = transfer.compute_poll_diff(rows, SRV, prior_state={}, alliance="OGV")
        assert len(diff.new_applicants) == 1
        assert diff.new_applicants[0].hash == _identity("Bad Pew", "738")
        assert diff.new_applicants[0].row == rows[0]

    def test_new_applicant_filtered_out_still_bookmarked(self):
        rows = [["Low Guy", "Pioneer", "10M", "", "738"]]
        filt = {"and": [{"column": "power", "op": ">=", "value": "100M"}]}
        diff = transfer.compute_poll_diff(
            rows, SRV, prior_state={}, filter_obj=filt, alliance="OGV"
        )
        assert diff.new_applicants == []
        assert _identity("Low Guy", "738") in diff.next_state

    def test_status_change_fires(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        h = _identity("Bad Pew", "738")
        prior = {h: {"confirmed": "FALSE"}}
        diff = transfer.compute_poll_diff(rows, SRV, prior_state=prior, alliance="OGV")
        assert diff.new_applicants == []
        assert len(diff.status_changes) == 1
        assert diff.status_changes[0].changes == [("confirmed", "FALSE", "TRUE")]

    def test_status_change_not_filtered(self):
        rows = [["Low Guy", "Pioneer", "10M", "TRUE", "738"]]
        h = _identity("Low Guy", "738")
        prior = {h: {"confirmed": ""}}
        filt = {"and": [{"column": "power", "op": ">=", "value": "100M"}]}
        diff = transfer.compute_poll_diff(
            rows, SRV, prior_state=prior, filter_obj=filt, alliance="OGV"
        )
        assert len(diff.status_changes) == 1

    def test_no_change_is_quiet(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        h = _identity("Bad Pew", "738")
        prior = {h: {"confirmed": "TRUE"}}
        diff = transfer.compute_poll_diff(rows, SRV, prior_state=prior, alliance="OGV")
        assert diff.new_applicants == []
        assert diff.status_changes == []

    def test_blank_rows_skipped(self):
        rows = [["", "", "", "", ""], ["Bad Pew", "Pioneer", "199M", "", "738"]]
        diff = transfer.compute_poll_diff(rows, SRV, prior_state={}, alliance="OGV")
        assert len(diff.new_applicants) == 1
        assert len(diff.next_state) == 1

    def test_same_name_different_server_are_distinct(self):
        rows = [
            ["Bad Pew", "Pioneer", "199M", "", "738"],
            ["Bad Pew", "Pioneer", "199M", "", "739"],
        ]
        diff = transfer.compute_poll_diff(rows, SRV, prior_state={}, alliance="OGV")
        assert len(diff.new_applicants) == 2
        assert len(diff.next_state) == 2


class TestDeletions:
    def test_status_bearing_deletion_surfaces(self):
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        keep = _identity("Bad Pew", "738")
        gone = _identity("Old Guy", "700")
        prior = {keep: {"confirmed": "TRUE"}, gone: {"confirmed": "TRUE"}}
        diff = transfer.compute_poll_diff(rows, SRV, prior_state=prior, alliance="OGV")
        assert [d.hash for d in diff.deletions] == [gone]
        assert gone not in diff.next_state
        assert keep in diff.next_state

    def test_pending_deletion_not_surfaced(self):
        # A removed row that never had a status set → forgotten silently.
        rows = [["Bad Pew", "Pioneer", "199M", "TRUE", "738"]]
        gone = _identity("Pending Guy", "700")
        prior = {_identity("Bad Pew", "738"): {"confirmed": "TRUE"}, gone: {"confirmed": ""}}
        diff = transfer.compute_poll_diff(rows, SRV, prior_state=prior, alliance="OGV")
        assert diff.deletions == []
        assert gone not in diff.next_state

    def test_baseline_never_surfaces_deletions(self):
        prior = {_identity("Old Guy", "700"): {"confirmed": "TRUE"}}
        diff = transfer.compute_poll_diff([], SRV, prior_state=prior, baseline=True)
        assert diff.deletions == []


class TestSelectRowsToCopy:
    # Resolved map for a server-wide source sheet.
    SRC = {"member": 0, "power": 1, "preferred alliance": 2}

    def test_copies_matching_uncopied_rows(self):
        rows = [["Bad Pew", "199M", "OGV"], ["Other", "50M", "XYZ"]]
        filt = {"and": [{"column": "Preferred Alliance", "op": "contains", "value": "OGV"}]}
        to_copy, copied = transfer.select_rows_to_copy(
            rows, self.SRC, already_copied=set(), filter_obj=filt
        )
        assert to_copy == [["Bad Pew", "199M", "OGV"]]
        assert transfer.identity_hash("Bad Pew") in copied

    def test_dedups_already_copied(self):
        rows = [["Bad Pew", "199M", "OGV"]]
        seen = {transfer.identity_hash("Bad Pew")}
        to_copy, copied = transfer.select_rows_to_copy(rows, self.SRC, already_copied=seen)
        assert to_copy == []
        assert copied == seen

    def test_no_filter_copies_all_new(self):
        rows = [["A", "1M", "X"], ["B", "2M", "Y"]]
        to_copy, copied = transfer.select_rows_to_copy(rows, self.SRC, already_copied=set())
        assert len(to_copy) == 2
        assert len(copied) == 2

    def test_blank_member_skipped(self):
        rows = [["", "1M", "X"], ["B", "2M", "Y"]]
        to_copy, _ = transfer.select_rows_to_copy(rows, self.SRC, already_copied=set())
        assert to_copy == [["B", "2M", "Y"]]


# ── Templates ────────────────────────────────────────────────────────────────


class TestRenderTemplate:
    def test_name_substitution(self):
        assert transfer.render_transfer_template("Hi {name}!", name="Bad Pew") == "Hi Bad Pew!"

    def test_multiple_placeholders(self):
        out = transfer.render_transfer_template(
            "{name} ({tier}) for {alliance_name}",
            name="Bad Pew",
            tier="Pioneer",
            alliance_name="OGV",
        )
        assert out == "Bad Pew (Pioneer) for OGV"

    def test_unknown_placeholder_renders_literally(self):
        assert transfer.render_transfer_template("Hi {nme}", name="Bad Pew") == "Hi {nme}"

    def test_missing_key_renders_blank(self):
        assert transfer.render_transfer_template("Hi {name}{tier}", name="Bad Pew") == "Hi Bad Pew"

    def test_none_value_renders_blank(self):
        assert transfer.render_transfer_template("Hi {name}", name=None) == "Hi "

    def test_stray_brace_does_not_crash(self):
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

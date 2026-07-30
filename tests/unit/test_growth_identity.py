"""
Unit tests for growth identity matching (#418) — the Discord-ID-first,
name-fallback row resolution that keeps a member's growth history intact
across an in-game name change.

Covers the pure helpers (`build_row_maps`, `resolve_row`), the roster
identity map that feeds them, and the snapshot write path end to end.
"""

from unittest.mock import patch, MagicMock
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.constants import TEST_GUILD_ID


# ── Pure row-resolution helpers ──────────────────────────────────────────────


class TestBuildRowMaps:
    def test_indexes_by_identity_and_name(self):
        from growth import build_row_maps

        rows = [["Alice", "100", "111"], ["Bob", "200", "222"]]
        id_to_row, name_to_row = build_row_maps(rows, id_idx=2, start=2)

        assert id_to_row == {"111": 2, "222": 3}
        assert name_to_row == {"alice": 2, "bob": 3}

    def test_rows_without_identity_are_name_only(self):
        from growth import build_row_maps

        rows = [["Alice", "100", ""], ["Bob", "200", "222"]]
        id_to_row, name_to_row = build_row_maps(rows, id_idx=2, start=2)

        assert id_to_row == {"222": 3}
        assert name_to_row == {"alice": 2, "bob": 3}

    def test_short_rows_do_not_raise(self):
        """A row written before the identity column existed is shorter than
        `id_idx`; indexing it must not blow up the whole snapshot."""
        from growth import build_row_maps

        id_to_row, name_to_row = build_row_maps([["Alice"], ["Bob", "200"]], id_idx=2, start=2)

        assert id_to_row == {}
        assert name_to_row == {"alice": 2, "bob": 3}

    def test_blank_rows_skipped(self):
        from growth import build_row_maps

        rows = [["Alice", "1", "111"], ["", "", ""], ["Bob", "2", "222"]]
        _, name_to_row = build_row_maps(rows, id_idx=2, start=2)

        assert name_to_row == {"alice": 2, "bob": 4}

    def test_duplicate_resolves_to_the_first_row(self):
        """A tab that already grew a duplicate from this bug keeps pointing at
        the older row, which is the one carrying the baseline."""
        from growth import build_row_maps

        rows = [["Alice", "100", "111"], ["Alice", "", "111"]]
        id_to_row, name_to_row = build_row_maps(rows, id_idx=2, start=2)

        assert id_to_row == {"111": 2}
        assert name_to_row == {"alice": 2}


class TestResolveRow:
    def test_identity_wins_over_name(self):
        """The rename case: the row is filed under the old name, but the
        identity still points at it."""
        from growth import resolve_row

        row = resolve_row("SyIvia", "111", {"111": 5}, {"sylvia": 5, "syivia": 9})

        assert row == 5

    def test_falls_back_to_name_without_identity(self):
        from growth import resolve_row

        assert resolve_row("Alice", "", {"111": 5}, {"alice": 7}) == 7

    def test_falls_back_to_name_when_identity_unknown(self):
        """Row predates identity stamping: the map has no entry for it yet."""
        from growth import resolve_row

        assert resolve_row("Alice", "999", {"111": 5}, {"alice": 7}) == 7

    def test_returns_none_for_a_genuinely_new_member(self):
        from growth import resolve_row

        assert resolve_row("Newbie", "999", {"111": 5}, {"alice": 7}) is None

    def test_name_match_is_case_and_space_insensitive(self):
        from growth import resolve_row

        assert resolve_row("  ALICE  ", "", {}, {"alice": 7}) == 7


class TestLoadIdentityMap:
    def test_unreadable_roster_degrades_to_empty(self):
        """An empty map means every lookup misses and matching stays on names,
        which is the pre-#418 behaviour."""
        from growth import load_identity_map

        with patch("member_roster.roster_identity_map", side_effect=RuntimeError("boom")):
            assert load_identity_map(TEST_GUILD_ID) == {}


# ── Roster identity map ──────────────────────────────────────────────────────


class TestRosterIdentityMap:
    def _patch_roster(self, values, rcfg=None):
        cfg = {"tab_name": "Member Roster", "discord_id_col": 0, "name_col": 1, "display_col": 2}
        cfg.update(rcfg or {})
        return (
            patch("config.get_member_roster_config", return_value=cfg),
            patch("config.read_member_roster_values", return_value=values),
        )

    def test_maps_name_and_display_name_to_the_id(self):
        from member_roster import roster_identity_map

        values = [
            ["Discord ID", "Name", "Display Name"],
            ["111", "Alice", "AliceInGame"],
        ]
        p1, p2 = self._patch_roster(values)
        with p1, p2:
            out = roster_identity_map(TEST_GUILD_ID)

        assert out == {"alice": "111", "aliceingame": "111"}

    def test_keeps_non_numeric_hand_maintained_identifiers(self):
        """Alliances use their own stable keys for members who aren't in
        Discord. `parse_roster_rows` nulls these; here they count."""
        from member_roster import roster_identity_map

        values = [["Discord ID", "Name", "Display Name"], ["no-disc-3", "Mari", ""]]
        p1, p2 = self._patch_roster(values)
        with p1, p2:
            out = roster_identity_map(TEST_GUILD_ID)

        assert out == {"mari": "no-disc-3"}

    def test_rows_with_no_identifier_are_omitted(self):
        from member_roster import roster_identity_map

        values = [["Discord ID", "Name", "Display Name"], ["", "Alice", ""], ["222", "Bob", ""]]
        p1, p2 = self._patch_roster(values)
        with p1, p2:
            out = roster_identity_map(TEST_GUILD_ID)

        assert out == {"bob": "222"}

    def test_unreadable_sheet_returns_empty(self):
        from member_roster import roster_identity_map

        with (
            patch("config.get_member_roster_config", side_effect=RuntimeError("nope")),
            patch("config.read_member_roster_values", return_value=[]),
        ):
            assert roster_identity_map(TEST_GUILD_ID) == {}

    def test_header_only_roster_returns_empty(self):
        from member_roster import roster_identity_map

        p1, p2 = self._patch_roster([["Discord ID", "Name", "Display Name"]])
        with p1, p2:
            assert roster_identity_map(TEST_GUILD_ID) == {}


# ── Snapshot write path ──────────────────────────────────────────────────────


class TestSnapshotIdentityWrites:
    """End-to-end over `_run_growth_snapshot_inner`'s write behaviour."""

    def _configure(self):
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Squad Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

    def _run(self, growth_rows, identities, source_rows=None):
        """Run a snapshot against a mocked growth tab. Returns (ws, batch_updates)."""
        from growth import _run_growth_snapshot_inner

        self._configure()
        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=growth_rows)
        ws.row_count = 100
        ws.row_values = MagicMock(return_value=growth_rows[0] if growth_rows else [])
        sh = MagicMock()
        sh.worksheet = MagicMock(return_value=ws)

        members = source_rows or [{"name": "SyIvia", "row_index": 2, "Power": 200.0}]

        with (
            patch("growth._get_spreadsheet", return_value=sh),
            patch("growth.load_member_data", return_value=members),
            patch("growth.load_identity_map", return_value=identities),
            patch("growth._write_breakdown_for_snapshot"),
            patch("growth._format_period_columns", create=True),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        updates = []
        for c in ws.batch_update.call_args_list:
            updates.extend(c.args[0])
        return ws, updates

    def test_identity_column_is_added_to_the_header(self, seeded_db):
        from growth import ID_HEADER

        ws, _ = self._run(
            [["Name", "Power (Jan 2026)"], ["SyIvia", "100"]],
            {"syivia": "111"},
        )

        header = ws.update.call_args_list[0].args[1][0]
        assert ID_HEADER in header

    def test_renamed_member_updates_the_existing_row_not_a_new_one(self, seeded_db):
        """The whole point of #418: the row is filed under the old name and
        carries the baseline, so the snapshot must land on it."""
        ws, updates = self._run(
            [["Name", "Power (Jan 2026)", "Discord ID"], ["Sylvia", "100", "111"]],
            {"syivia": "111"},
        )

        ws.append_rows.assert_not_called()
        # Metric written to row 2, the pre-existing row.
        assert any(u["range"].endswith("2") for u in updates)

    def test_renamed_member_has_their_name_cell_refreshed(self, seeded_db):
        _, updates = self._run(
            [["Name", "Power (Jan 2026)", "Discord ID"], ["Sylvia", "100", "111"]],
            {"syivia": "111"},
        )

        assert {"range": "A2", "values": [["SyIvia"]]} in updates

    def test_identity_stamped_onto_a_row_that_predates_the_column(self, seeded_db):
        _, updates = self._run(
            [["Name", "Power (Jan 2026)"], ["SyIvia", "100"]],
            {"syivia": "111"},
        )

        assert any(u["values"] == [["111"]] for u in updates)

    def test_no_redundant_write_when_identity_already_matches(self, seeded_db):
        _, updates = self._run(
            [["Name", "Power (Jan 2026)", "Discord ID"], ["SyIvia", "100", "111"]],
            {"syivia": "111"},
        )

        assert not any(u["values"] == [["111"]] for u in updates)
        assert not any(u["range"].startswith("A") for u in updates)

    def test_new_member_row_carries_its_identity(self, seeded_db):
        ws, _ = self._run(
            [["Name", "Power (Jan 2026)", "Discord ID"], ["Alice", "100", "999"]],
            {"syivia": "111"},
        )

        appended = ws.append_rows.call_args.args[0]
        assert appended[0][0] == "SyIvia"
        assert "111" in appended[0]

    def test_without_roster_identities_behaviour_is_unchanged(self, seeded_db):
        """Alliances that keep no ID column stay on pure name matching."""
        ws, updates = self._run(
            [["Name", "Power (Jan 2026)"], ["Sylvia", "100"]],
            {},
        )

        # No identity to match on, so the rename still appends — as before.
        ws.append_rows.assert_called_once()
        assert not any(u["range"] == "A2" for u in updates)

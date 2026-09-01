"""
Unit tests for growth.py — load_member_data, _run_growth_snapshot_inner,
snapshot skipping logic, column header generation, and the
compute_next_snapshot helper used to surface "next fire" date in
/setup_growth and /growth.
"""

from datetime import datetime, timezone
import pytest
from unittest.mock import patch, MagicMock, call
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.constants import TEST_GUILD_ID
import growth

# The instant CI was standing on when `dev` went red on 2026-09-01. In UTC it
# is already September; in `America/New_York`, the clock `growth.py` snapshots
# on, it is still 23:19 on 31 August.
#
# Freezing here rather than at some quiet mid-month moment is the point. Every
# period label below is the **ET** month, so a snapshot that read the runner's
# wall clock -- or UTC, or anything but ET -- labels these columns `Sep 2026`
# and all seven of these tests fail. The bug that took `dev` red is what these
# assertions are now pinned against.
FROZEN_UTC = datetime(2026, 9, 1, 3, 19, 48, tzinfo=timezone.utc)
FROZEN_PERIOD = "Aug 2026"
FROZEN_PERIOD_UTC = "Sep 2026"  # what the naive clock used to produce


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin `growth`'s `datetime.now` to `FROZEN_UTC`, leaving the rest alone.

    `growth.py` does `from datetime import datetime`, so the module global is
    the seam. `strptime` and friends are inherited untouched, and the naive
    branch deliberately returns UTC -- that is exactly what a GitHub runner
    hands a caller that forgets the timezone.
    """

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return FROZEN_UTC.replace(tzinfo=None)
            return FROZEN_UTC.astimezone(tz)

    monkeypatch.setattr(growth, "datetime", _Frozen)
    return FROZEN_UTC


class TestLoadMemberData:
    """Test load_member_data reads correct columns from configured source tab."""

    def _mock_sheet(self, rows):
        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=rows)
        sh = MagicMock()
        sh.worksheet = MagicMock(return_value=ws)
        return sh

    def test_loads_name_and_metrics(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Squad Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}, {"col": "C", "label": "THP"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        rows = [
            ["Name", "Power", "THP"],
            ["Alice", "43.27", "301"],
            ["Bob", "38.50", "280"],
        ]
        mock_sh = self._mock_sheet(rows)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 2
        assert members[0]["name"] == "Alice"
        assert members[0]["Power"] == 43.27
        assert members[0]["THP"] == 301.0
        assert members[1]["name"] == "Bob"

    def test_skips_empty_name_rows(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Members",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        rows = [
            ["Name", "Power"],
            ["Alice", "43.27"],
            ["", "38.50"],  # empty name — should be skipped
            ["Carol", "41.00"],
        ]
        mock_sh = self._mock_sheet(rows)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 2
        names = [m["name"] for m in members]
        assert "Alice" in names
        assert "Carol" in names

    def test_returns_empty_without_config(self, seeded_db):
        from growth import load_member_data

        # Growth not configured (enabled=0 by default)
        members = load_member_data(TEST_GUILD_ID)
        assert members == []

    def test_custom_column_letters(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Roster",
            name_col="D",
            metrics=[{"col": "E", "label": "Squad1"}, {"col": "G", "label": "Squad2"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        # D=col3, E=col4, G=col6 (0-indexed)
        rows = [
            ["A", "B", "C", "Name", "Squad1", "X", "Squad2"],
            ["", "", "", "Alice", "43.27", "", "38.50"],
        ]
        mock_sh = self._mock_sheet(rows)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 1
        assert members[0]["name"] == "Alice"
        assert members[0]["Squad1"] == 43.27
        assert members[0]["Squad2"] == 38.50


class TestSafeFloat:
    """gspread returns the *formatted* display string, so any metric with a
    thousands-separator number format arrived as "65,200,000" and bare float()
    rejected it — every big metric snapshotted as 0 while drone level (too
    small for a comma) came through fine."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("65,200,000", 65_200_000.0),  # the regression
            ("1,234.56", 1234.56),
            ("43270000", 43_270_000.0),
            ("187", 187.0),
            ("43.27", 43.27),
            ("250M", 250_000_000.0),
            ("1.2b", 1_200_000_000.0),
            ("12k", 12_000.0),
            ("6.52E+07", 65_200_000.0),  # Sheets' scientific rendering
            (" 300 000 ", 300_000.0),
            ("1_000_000", 1_000_000.0),
            (0, 0.0),
            (187, 187.0),
            ("", 0.0),
            ("   ", 0.0),
            (None, 0.0),
            ("Missile", 0.0),
            ("N/A", 0.0),
        ],
    )
    def test_parses(self, raw, expected):
        from growth import _safe_float

        assert _safe_float(raw) == pytest.approx(expected)


class TestColIndex:
    """Bare ord() arithmetic raised TypeError on two-letter columns, which the
    caller's except swallowed into a silently skipped snapshot."""

    @pytest.mark.parametrize(
        "letter,expected",
        [
            ("A", 0),
            ("D", 3),
            ("Z", 25),
            ("AA", 26),
            ("AB", 27),
            ("BA", 52),
            ("aa", 26),
            (" c ", 2),
            ("", None),
            (None, None),
            ("A1", None),
            ("3", None),
        ],
    )
    def test_index(self, letter, expected):
        from growth import _col_index

        assert _col_index(letter) == expected

    def test_round_trips_with_col_letter(self):
        from growth import _col_index, _col_letter

        for i in range(0, 200):
            assert _col_index(_col_letter(i)) == i


class TestLoadMemberDataParsing:
    """Regression coverage for the two load_member_data defects."""

    def _mock_sheet(self, rows):
        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=rows)
        sh = MagicMock()
        sh.worksheet = MagicMock(return_value=ws)
        return sh

    def test_comma_formatted_values_are_not_zeroed(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Squad Powers",
            name_col="A",
            metrics=[
                {"col": "B", "label": "1st Squad Power"},
                {"col": "C", "label": "Drone Level"},
                {"col": "D", "label": "Total Kills"},
            ],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        rows = [
            ["Name", "1st Squad Power", "Drone Level", "Total Kills"],
            ["MotherGoose", "65,200,000", "187", "12,000,000"],
        ]
        mock_sh = self._mock_sheet(rows)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 1
        assert members[0]["1st Squad Power"] == 65_200_000.0
        assert members[0]["Drone Level"] == 187.0
        assert members[0]["Total Kills"] == 12_000_000.0

    def test_two_letter_metric_column(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Squad Powers",
            name_col="A",
            metrics=[{"col": "AB", "label": "Total Kills"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        # AB is index 27, so the row needs 28 cells.
        header = [f"c{i}" for i in range(28)]
        row = ["Alice"] + [""] * 26 + ["57,000,000"]
        mock_sh = self._mock_sheet([header, row])

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 1
        assert members[0]["Total Kills"] == 57_000_000.0

    def test_invalid_metric_column_skips_metric_not_snapshot(self, seeded_db):
        from growth import load_member_data
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Squad Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}, {"col": "", "label": "Broken"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        mock_sh = self._mock_sheet([["Name", "Power"], ["Alice", "1,500"]])

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            members = load_member_data(TEST_GUILD_ID)

        assert len(members) == 1
        assert members[0]["Power"] == 1500.0
        assert "Broken" not in members[0]


class TestRunGrowthSnapshotInner:
    """Test _run_growth_snapshot_inner skipping and write logic."""

    def test_skips_when_disabled(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=0,
            tab_source="Squad Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        with patch("growth._get_spreadsheet") as mock_sh:
            _run_growth_snapshot_inner(TEST_GUILD_ID)
            mock_sh.assert_not_called()

    def test_skips_when_no_spreadsheet_id(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config, get_config, save_config

        cfg = get_config(TEST_GUILD_ID)
        cfg.spreadsheet_id = ""
        save_config(cfg)

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Tab",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        with patch("growth._get_spreadsheet") as mock_sh:
            _run_growth_snapshot_inner(TEST_GUILD_ID)
            mock_sh.assert_not_called()

    def test_skips_duplicate_period(self, seeded_db, frozen_clock):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        from datetime import datetime

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        month_label = FROZEN_PERIOD

        # Sheet already has this period's column
        existing_headers = ["Name", f"Power ({month_label})"]
        mock_ws = MagicMock()
        mock_ws.row_count = 5
        mock_ws.row_values = MagicMock(return_value=existing_headers)
        mock_ws.get_all_values = MagicMock(return_value=[existing_headers])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        # A real roster, deliberately. `_run_growth_snapshot_inner` returns at
        # `if not members` *before* it ever reaches the duplicate-period check,
        # so an empty one made this test vacuous: it passed whether the period
        # was recognised as a duplicate or not.
        members = [{"name": "Alice", "row_index": 2, "Power": 130.0}]
        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        # The metric-column write is what a duplicate period skips, and the
        # header rewrite is the visible half of it. Asserting on the header
        # rather than on "nothing happened" is deliberate: #85 has the
        # breakdown writer fire either way, so this run is not silent.
        header_writes = [c for c in mock_ws.update.call_args_list if c[0][0] == "A1"]
        assert header_writes == [], (
            f"header rewritten for a period already present: {header_writes}"
        )
        mock_ws.batch_update.assert_not_called()

    def test_writes_member_data(self, seeded_db, frozen_clock):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        from datetime import datetime

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        month_label = FROZEN_PERIOD
        col_name = f"Power ({month_label})"

        mock_ws = MagicMock()
        mock_ws.row_count = 10
        mock_ws.row_values = MagicMock(return_value=["Name"])
        mock_ws.get_all_values = MagicMock(return_value=[["Name"]])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        members = [
            {"name": "Alice", "row_index": 2, "Power": 43.27},
            {"name": "Bob", "row_index": 3, "Power": 38.50},
        ]

        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        # Header should be updated with new column
        mock_ws.update.assert_called()
        header_call = mock_ws.update.call_args_list[0]
        updated_header = header_call[0][1][0]
        assert col_name in updated_header

    def test_the_period_is_the_et_month_not_the_runners(self, seeded_db, frozen_clock):
        """The regression that took `dev` red on 2026-09-01.

        `growth.py` snapshots on `America/New_York`, because that is the clock
        the scheduler fires on. The runner's is UTC, up to four hours ahead --
        so for the four hours after 00:00 UTC on the first of a month the two
        disagree about which month it is, and a snapshot that read the wrong
        one files a whole alliance's power under the month that has not started.

        The frozen instant is exactly that window. Anything but ET labels this
        column `Sep 2026`.
        """
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        mock_ws = MagicMock()
        mock_ws.row_count = 10
        mock_ws.row_values = MagicMock(return_value=["Name"])
        mock_ws.get_all_values = MagicMock(return_value=[["Name"]])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch(
                "growth.load_member_data",
                return_value=[{"name": "Alice", "row_index": 2, "Power": 43.27}],
            ),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        written = mock_ws.update.call_args_list[0][0][1][0]
        assert f"Power ({FROZEN_PERIOD})" in written, written
        assert f"Power ({FROZEN_PERIOD_UTC})" not in written, (
            f"snapshot used the runner's clock, not ET: {written}"
        )

    def test_new_members_appended_in_one_batched_call(self, seeded_db):
        """Regression for #40: a populated roster (60+ members) on a first-ever
        snapshot must not call ws.append_row once per member — that exhausts
        the 60/min Sheets write quota mid-write and aborts the snapshot. The
        snapshot must use ws.append_rows (plural) once with all new rows."""
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        from datetime import datetime

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        mock_ws = MagicMock()
        mock_ws.row_count = 100
        mock_ws.row_values = MagicMock(return_value=["Name"])
        mock_ws.get_all_values = MagicMock(return_value=[["Name"]])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        members = [
            {"name": f"Member{i:02d}", "row_index": i + 2, "Power": float(i)} for i in range(60)
        ]

        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        mock_ws.append_row.assert_not_called()
        mock_ws.append_rows.assert_called_once()
        appended = mock_ws.append_rows.call_args[0][0]
        assert len(appended) == 60, (
            f"expected 60 rows in the single append_rows call, got {len(appended)}"
        )
        assert appended[0][0] == "Member00"
        assert appended[-1][0] == "Member59"

    def test_writes_columns_past_z(self, seeded_db):
        """A growth tab accumulates metric columns every period, so it crosses
        Z within a few snapshots. chr(ord("A") + 26) is "[", an invalid A1
        range that fails the whole batch write."""
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        # 26 existing columns (A..Z), so this period's column lands at AA.
        existing_header = ["Name"] + [f"Filler{i}" for i in range(25)]
        assert len(existing_header) == 26

        mock_ws = MagicMock()
        mock_ws.row_count = 100
        mock_ws.row_values = MagicMock(return_value=list(existing_header))
        mock_ws.get_all_values = MagicMock(
            return_value=[list(existing_header), ["Alice"] + [""] * 25]
        )
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=mock_ws)

        members = [{"name": "Alice", "row_index": 2, "Power": 65_200_000.0}]

        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        mock_ws.batch_update.assert_called_once()
        updates = mock_ws.batch_update.call_args[0][0]
        assert [u["range"] for u in updates] == ["AA2"]
        assert updates[0]["values"] == [[65_200_000.0]]

    def test_creates_growth_tab_if_missing(self, seeded_db):
        from growth import _run_growth_snapshot_inner
        from config import save_growth_config
        import gspread

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="New Growth Tab",  # tab that doesn't exist yet
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

        mock_new_ws = MagicMock()
        mock_new_ws.row_count = 10
        mock_new_ws.row_values = MagicMock(return_value=[])
        mock_new_ws.get_all_values = MagicMock(return_value=[])
        mock_new_ws.update = MagicMock()
        mock_new_ws.batch_update = MagicMock()

        mock_source_ws = MagicMock()
        mock_source_ws.get_all_values = MagicMock(
            return_value=[
                ["Name", "Power"],
                ["Alice", "43.27"],
            ]
        )

        def fake_worksheet(name):
            if name == "Powers":
                return mock_source_ws
            raise gspread.exceptions.WorksheetNotFound(name)

        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(side_effect=fake_worksheet)
        mock_sh.add_worksheet = MagicMock(return_value=mock_new_ws)

        with patch("growth._get_spreadsheet", return_value=mock_sh):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        mock_sh.add_worksheet.assert_called_once_with(title="New Growth Tab", rows=500, cols=50)


# ── Growth Breakdown (#34) ─────────────────────────────────────────────────


class TestClassifyBucket:
    """Verify the bucket classifier at every default-threshold edge plus
    the prev=0 blank-out and the custom-threshold override."""

    def test_returns_none_when_prev_is_zero(self):
        from growth import classify_bucket

        assert classify_bucket(0, 100) is None

    def test_returns_none_when_prev_is_negative(self):
        from growth import classify_bucket

        assert classify_bucket(-5, 100) is None

    def test_returns_none_when_prev_unparseable(self):
        from growth import classify_bucket

        assert classify_bucket("", 100) is None
        assert classify_bucket("nope", 100) is None

    def test_decline_when_curr_below_prev(self):
        from growth import classify_bucket

        assert classify_bucket(100, 99) == "decline"
        assert classify_bucket(100, 50) == "decline"

    def test_none_bucket_under_low_threshold(self):
        from growth import classify_bucket

        # 0% and 4.99% sit in the None bucket under default thresholds.
        assert classify_bucket(100, 100) == "none"
        assert classify_bucket(100, 104.9) == "none"

    def test_low_bucket_at_five_percent(self):
        from growth import classify_bucket

        # Default thresholds: low ≥ 5%, steady ≥ 10%.
        assert classify_bucket(100, 105) == "low"
        assert classify_bucket(100, 109) == "low"

    def test_steady_bucket_at_ten_percent(self):
        from growth import classify_bucket

        assert classify_bucket(100, 110) == "steady"
        assert classify_bucket(100, 119) == "steady"

    def test_increased_bucket_at_twenty_percent(self):
        from growth import classify_bucket

        assert classify_bucket(100, 120) == "increased"
        assert classify_bucket(100, 200) == "increased"

    def test_custom_thresholds_shift_boundaries(self):
        from growth import classify_bucket

        # Tighter standards: Increased ≥ 30%, Steady ≥ 15%.
        thresh = {"increased": 30, "steady": 15, "low": 5, "none": 0}
        # 20% used to be `increased` under defaults; now sits in `steady`.
        assert classify_bucket(100, 120, thresholds=thresh) == "steady"
        # 30% hits the new `increased` floor.
        assert classify_bucket(100, 130, thresholds=thresh) == "increased"

    def test_custom_thresholds_ignore_unknown_keys(self):
        from growth import classify_bucket

        # Junk keys should not crash the classifier; valid keys still apply.
        thresh = {"increased": 25, "garbage": "nope"}
        assert classify_bucket(100, 125, thresholds=thresh) == "increased"
        assert classify_bucket(100, 124, thresholds=thresh) == "steady"


class TestComputePctChange:
    def test_returns_none_when_prev_zero(self):
        from growth import compute_pct_change

        assert compute_pct_change(0, 100) is None

    def test_returns_none_for_unparseable(self):
        from growth import compute_pct_change

        assert compute_pct_change("x", 100) is None

    def test_rounds_to_two_decimals(self):
        from growth import compute_pct_change

        assert compute_pct_change(100, 114.333) == 14.33

    def test_handles_negative_delta(self):
        from growth import compute_pct_change

        assert compute_pct_change(100, 99) == -1.0


class TestExtractPeriodLabels:
    def test_returns_unique_periods_in_first_appearance_order(self):
        from growth import _extract_period_labels

        header = [
            "Name",
            "Power (Apr 2026)",
            "THP (Apr 2026)",
            "Power (May 2026)",
            "THP (May 2026)",
        ]
        assert _extract_period_labels(header, ["Power", "THP"]) == ["Apr 2026", "May 2026"]

    def test_empty_header_returns_empty_list(self):
        from growth import _extract_period_labels

        assert _extract_period_labels([], ["Power"]) == []

    def test_ignores_columns_for_unconfigured_metrics(self):
        from growth import _extract_period_labels

        # `Other (Apr 2026)` shouldn't register a period — Other isn't in
        # metric_labels.
        header = ["Name", "Other (Apr 2026)", "Power (May 2026)"]
        assert _extract_period_labels(header, ["Power"]) == ["May 2026"]


class TestFormatBreakdownEmbed:
    def test_one_field_per_metric_with_buckets_listed(self):
        from growth import format_breakdown_embed

        summary = {
            "Power": {
                "increased": ["Alice"],
                "steady": ["Bob"],
                "low": [],
                "none": [],
                "decline": ["Carol"],
            },
            "THP": {b: [] for b in ["increased", "steady", "low", "none", "decline"]},
        }
        embed = format_breakdown_embed(
            metric_labels=["Power", "THP"],
            breakdown_summary=summary,
            prev_period_label="Apr 2026",
            curr_period_label="May 2026",
        )
        # One field per metric, in metric order.
        field_names = [f.name for f in embed.fields]
        assert field_names == ["Power", "THP"]
        # Power field shows the three non-empty buckets with their members.
        power_field = embed.fields[0].value
        assert "Increased" in power_field and "Alice" in power_field
        assert "Steady" in power_field and "Bob" in power_field
        assert "Decline" in power_field and "Carol" in power_field
        # Empty buckets aren't rendered.
        assert "Low" not in power_field
        assert "No Change" not in power_field

    def test_bucket_filter_omits_unselected_buckets(self):
        from growth import format_breakdown_embed

        summary = {
            "Power": {
                "increased": ["Alice"],
                "steady": [],
                "low": [],
                "none": [],
                "decline": ["Carol"],
            },
        }
        embed = format_breakdown_embed(
            metric_labels=["Power"],
            breakdown_summary=summary,
            prev_period_label="A",
            curr_period_label="B",
            bucket_filter=["decline"],  # only Decline alerts
        )
        value = embed.fields[0].value
        assert "Carol" in value
        assert "Alice" not in value
        assert "Increased" not in value

    def test_custom_labels_used_in_render(self):
        from growth import format_breakdown_embed

        summary = {"Power": {b: [] for b in ["increased", "steady", "low", "none", "decline"]}}
        summary["Power"]["increased"] = ["Alice"]
        embed = format_breakdown_embed(
            metric_labels=["Power"],
            breakdown_summary=summary,
            prev_period_label="A",
            curr_period_label="B",
            label_overrides={"increased": "Crushing It"},
        )
        assert "Crushing It" in embed.fields[0].value
        assert "Increased" not in embed.fields[0].value


class TestSnapshotBreakdownWriting:
    """Integration: _run_growth_snapshot_inner must trigger breakdown writes
    on the second snapshot, be idempotent on re-runs, skip the very first
    snapshot, and write blanks when a member's prev value is 0."""

    def _build_mocks(
        self, growth_header, growth_rows, members, breakdown_header=None, breakdown_rows=None
    ):
        """Return (mock_sh, growth_ws, breakdown_ws). The breakdown tab
        is registered with whatever name the test config uses; the source
        tab returns the supplied members verbatim."""
        import gspread

        growth_ws = MagicMock()
        growth_ws.row_count = 100
        growth_ws.row_values = MagicMock(return_value=growth_header)
        growth_ws.get_all_values = MagicMock(return_value=[growth_header] + growth_rows)
        growth_ws.update = MagicMock()
        growth_ws.batch_update = MagicMock()
        growth_ws.append_rows = MagicMock()

        bd_ws = MagicMock()
        bd_ws.row_count = 100
        bd_ws.get_all_values = MagicMock(
            return_value=([breakdown_header] + (breakdown_rows or [])) if breakdown_header else []
        )
        bd_ws.update = MagicMock()
        bd_ws.batch_update = MagicMock()
        bd_ws.append_rows = MagicMock()

        def fake_worksheet(name):
            if name in ("Growth", "Growth Tracking"):
                return growth_ws
            if name == "Growth Breakdown":
                return bd_ws
            raise gspread.exceptions.WorksheetNotFound(name)

        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(side_effect=fake_worksheet)
        mock_sh.add_worksheet = MagicMock(return_value=bd_ws)
        return mock_sh, growth_ws, bd_ws

    def _seed_config(self):
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )

    def test_first_snapshot_skips_breakdown(self, seeded_db):
        from growth import _run_growth_snapshot_inner

        self._seed_config()
        mock_sh, growth_ws, bd_ws = self._build_mocks(
            growth_header=["Name"],
            growth_rows=[],
            members=[],
        )
        members = [{"name": "Alice", "row_index": 2, "Power": 100.0}]
        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        # No breakdown writes — only one period exists after this snapshot.
        bd_ws.batch_update.assert_not_called()
        bd_ws.append_rows.assert_not_called()

    def test_second_snapshot_writes_breakdown(self, seeded_db, frozen_clock):
        from datetime import datetime
        from growth import _run_growth_snapshot_inner

        self._seed_config()

        month_label = FROZEN_PERIOD
        prev_label = "Apr 2026"  # any label that isn't the current month
        # Pre-existing growth tab: one prev-period column with Alice's value.
        growth_header = ["Name", f"Power ({prev_label})"]
        growth_rows = [["Alice", "100"], ["Bob", "0"]]
        mock_sh, growth_ws, bd_ws = self._build_mocks(
            growth_header=growth_header,
            growth_rows=growth_rows,
            members=[],
        )

        members = [
            {"name": "Alice", "row_index": 2, "Power": 130.0},  # +30% → increased
            {"name": "Bob", "row_index": 3, "Power": 50.0},  # prev 0 → blank
        ]
        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        # Breakdown header should land with the two new transition columns.
        first_update = bd_ws.update.call_args_list[0]
        bd_header_written = first_update[0][1][0]
        pct_col = f"{prev_label} - {month_label} Power %"
        bucket_col = f"{prev_label} - {month_label} Power Bucket"
        assert pct_col in bd_header_written
        assert bucket_col in bd_header_written

        # batch_update should have been called with at least Alice's row +
        # Bob's blank row, two cells each (% and Bucket).
        assert bd_ws.batch_update.called, "Breakdown batch_update never fired on second snapshot"

        # Extract the values written. Each batch_update entry has
        # `{"range": "<col><row>", "values": [[value]]}`.
        cells = {}
        for call_args in bd_ws.batch_update.call_args_list:
            for entry in call_args[0][0]:
                rng = entry["range"]
                val = entry["values"][0][0]
                cells[rng] = val
        # Find the % values for Alice (row 2) and Bob (row 3). The exact
        # column letter depends on header layout, so look for any cell
        # whose value matches the expectation.
        values_only = list(cells.values())
        assert any(v == "30.00%" for v in values_only), (
            f"Alice's 30% breakdown not in writes: {values_only}"
        )
        assert any(v == "Increased" for v in values_only)
        # Bob had prev=0 — both his cells must be blank.
        # We can't isolate Bob's cells without column mapping, but the
        # combined writes shouldn't include any 'Decline' or extra bucket.
        assert sum(1 for v in values_only if v == "Increased") == 1
        assert not any(v == "Decline" for v in values_only)

    def test_breakdown_baseline_parses_comma_formatted_prev(self, seeded_db, frozen_clock):
        """#417: the growth tab carries a thousands-separator number format, so
        gspread returns "100,000,000" for the previous period. A bare float()
        raised, the except zeroed the baseline, and classify_bucket treats
        prev <= 0 as "no baseline" — so every member with a comma-carrying
        value silently fell out of every bucket and the embed rendered "No
        members in the included buckets"."""
        from datetime import datetime
        from growth import _run_growth_snapshot_inner

        self._seed_config()

        prev_label = "Apr 2026"
        growth_header = ["Name", f"Power ({prev_label})"]
        # The formatted read, exactly as gspread hands it back.
        growth_rows = [["Alice", "100,000,000"]]
        mock_sh, growth_ws, bd_ws = self._build_mocks(
            growth_header=growth_header,
            growth_rows=growth_rows,
            members=[],
        )

        members = [{"name": "Alice", "row_index": 2, "Power": 130_000_000.0}]  # +30%
        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        values_only = []
        for call_args in bd_ws.batch_update.call_args_list:
            for entry in call_args[0][0]:
                values_only.append(entry["values"][0][0])
        assert any(v == "30.00%" for v in values_only), (
            f"comma-formatted baseline was not parsed: {values_only}"
        )
        assert any(v == "Increased" for v in values_only)

    def test_snapshot_formats_new_period_columns(self, seeded_db, frozen_clock):
        """#417: a fresh period must land with the same thousands-separator
        format the alliance keeps on their source columns, instead of a bare
        65190000 beside the previous period's 57,150,000."""
        from datetime import datetime
        from growth import _run_growth_snapshot_inner

        self._seed_config()

        month_label = FROZEN_PERIOD
        mock_sh, growth_ws, bd_ws = self._build_mocks(
            growth_header=["Name", "Power (Apr 2026)"],
            growth_rows=[["Alice", "100"]],
            members=[],
        )

        members = [{"name": "Alice", "row_index": 2, "Power": 130.0}]
        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        assert growth_ws.format.called, "no number format applied to the new period"
        ranges, fmt = growth_ws.format.call_args[0]
        assert fmt == {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}
        # Column C is this period's `Power ({month})`, appended after
        # Name + Power (Apr 2026). Formatted row 2 down so the header keeps
        # its own styling.
        # C is the right target only because it is *this period's* column.
        # Assert that rather than assume the position -- a snapshot that
        # resolved the wrong month appends a third column here just the same.
        header_written = growth_ws.update.call_args_list[0][0][1][0]
        assert header_written[2] == f"Power ({month_label})", header_written
        assert ranges == ["C2:C"], f"unexpected format target for {month_label}: {ranges}"

    def test_number_format_failure_does_not_fail_snapshot(self, seeded_db):
        """The values are already committed when the format call runs, so a
        Sheets error there must not propagate."""
        from growth import _run_growth_snapshot_inner

        self._seed_config()
        mock_sh, growth_ws, bd_ws = self._build_mocks(
            growth_header=["Name", "Power (Apr 2026)"],
            growth_rows=[["Alice", "100"]],
            members=[],
        )
        growth_ws.format = MagicMock(side_effect=Exception("429 quota"))

        members = [{"name": "Alice", "row_index": 2, "Power": 130.0}]
        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)  # must not raise

        assert growth_ws.batch_update.called

    def test_duplicate_period_still_writes_missing_breakdown(self, seeded_db, frozen_clock):
        """Regression for #85. The seeder (or a previous in-period
        manual run) leaves the current period's columns on the growth
        tab. Re-running 'Snapshot Now' must NOT skip silently — the
        breakdown writer needs to run so a missing breakdown can be
        filled in without forcing leadership to hand-edit the sheet."""
        from datetime import datetime
        from growth import _run_growth_snapshot_inner

        self._seed_config()

        month_label = FROZEN_PERIOD
        prev_label = "Apr 2026"
        # Growth tab already carries the current period's columns
        # (period_already_exists=True path) AND a previous period.
        growth_header = ["Name", f"Power ({prev_label})", f"Power ({month_label})"]
        growth_rows = [["Alice", "100", "130"]]
        # Breakdown tab is empty — the bug being fixed.
        mock_sh, growth_ws, bd_ws = self._build_mocks(
            growth_header=growth_header,
            growth_rows=growth_rows,
            members=[],
        )

        members = [{"name": "Alice", "row_index": 2, "Power": 130.0}]
        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        # The snapshot side skips writing metric columns (duplicate period).
        growth_ws.batch_update.assert_not_called()
        # But the breakdown writer DOES run and writes the missing transition.
        assert bd_ws.batch_update.called, "Breakdown writer didn't run on the duplicate-period path"
        # Header row should have been written with the new transition columns.
        assert bd_ws.update.called
        first_bd_update = bd_ws.update.call_args_list[0]
        bd_header_written = first_bd_update[0][1][0]
        assert f"{prev_label} - {month_label} Power %" in bd_header_written
        assert f"{prev_label} - {month_label} Power Bucket" in bd_header_written

    def test_second_snapshot_idempotent_on_rerun(self, seeded_db, frozen_clock):
        """Re-running the same snapshot must not write breakdown rows
        twice — idempotency check on the transition columns in the
        breakdown tab header."""
        from datetime import datetime
        from growth import _run_growth_snapshot_inner

        self._seed_config()

        month_label = FROZEN_PERIOD
        prev_label = "Apr 2026"
        # The growth-tab side already carries the current period (idempotent
        # path of the snapshot itself), and the breakdown tab already has
        # the transition columns from a prior run.
        growth_header = ["Name", f"Power ({prev_label})", f"Power ({month_label})"]
        growth_rows = [["Alice", "100", "130"]]
        existing_bd_header = [
            "Name",
            f"{prev_label} - {month_label} Power %",
            f"{prev_label} - {month_label} Power Bucket",
        ]
        existing_bd_rows = [["Alice", "30.00%", "Increased"]]

        mock_sh, growth_ws, bd_ws = self._build_mocks(
            growth_header=growth_header,
            growth_rows=growth_rows,
            members=[],
            breakdown_header=existing_bd_header,
            breakdown_rows=existing_bd_rows,
        )

        members = [{"name": "Alice", "row_index": 2, "Power": 130.0}]
        with (
            patch("growth._get_spreadsheet", return_value=mock_sh),
            patch("growth.load_member_data", return_value=members),
        ):
            _run_growth_snapshot_inner(TEST_GUILD_ID)

        # The snapshot side returns early (duplicate period), so neither
        # tab gets any batch_update or append calls.
        growth_ws.batch_update.assert_not_called()
        bd_ws.batch_update.assert_not_called()
        bd_ws.append_rows.assert_not_called()


class TestMaybePostBreakdown:
    """The Premium auto-post path. discord.py 2.4+ raises when `bot.loop`
    is accessed from a non-async context (the thread-pool worker that
    runs the scheduled snapshot is exactly that), so the helper has to
    use the captured event loop in `bot_state`. `bot_state` lives in a
    third module so the running `bot.py` (loaded as `__main__`) and any
    downstream `import bot` (a separate idle copy) both point at the
    same shared state. Regression for #87."""

    def _summary_stub(self):
        return {"Power": {b: [] for b in ["increased", "steady", "low", "none", "decline"]}}

    def test_no_op_when_bot_state_not_populated(self):
        """If `on_ready` hasn't populated `bot_state.event_loop` yet (or
        `bot_state.bot` is None), the helper logs a friendly message
        and returns without raising."""
        from growth import _maybe_post_breakdown
        import bot_state

        previous_loop = bot_state.event_loop
        previous_bot = bot_state.bot
        bot_state.event_loop = None
        bot_state.bot = None
        try:
            _maybe_post_breakdown(
                guild_id=12345,
                post_channel_id=99999,
                prev_period_label="Apr 2026",
                curr_period_label="May 2026",
                metric_labels=["Power"],
                breakdown_summary=self._summary_stub(),
                gcfg={"breakdown_labels": {}, "breakdown_bucket_filter": []},
            )
        finally:
            bot_state.event_loop = previous_loop
            bot_state.bot = previous_bot

    def test_schedules_on_captured_loop_when_ready(self):
        """When `bot_state` carries a running loop and a bot, the helper
        hands the coroutine to `asyncio.run_coroutine_threadsafe` on
        that loop."""
        from growth import _maybe_post_breakdown
        import bot_state

        fake_loop = MagicMock()
        fake_loop.is_running = MagicMock(return_value=True)
        fake_bot = MagicMock()
        previous_loop = bot_state.event_loop
        previous_bot = bot_state.bot
        bot_state.event_loop = fake_loop
        bot_state.bot = fake_bot
        try:
            with patch("asyncio.run_coroutine_threadsafe") as rcts:
                _maybe_post_breakdown(
                    guild_id=12345,
                    post_channel_id=99999,
                    prev_period_label="Apr 2026",
                    curr_period_label="May 2026",
                    metric_labels=["Power"],
                    breakdown_summary=self._summary_stub(),
                    gcfg={"breakdown_labels": {}, "breakdown_bucket_filter": []},
                )
            assert rcts.called, "run_coroutine_threadsafe was never called"
            args, _ = rcts.call_args
            # Second arg is the loop — must be our captured fake.
            assert args[1] is fake_loop
        finally:
            bot_state.event_loop = previous_loop
            bot_state.bot = previous_bot


class TestReadLatestBreakdown:
    def test_empty_tab_returns_has_data_false(self, seeded_db):
        from growth import read_latest_breakdown
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )
        bd_ws = MagicMock()
        bd_ws.get_all_values = MagicMock(return_value=[])
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=bd_ws)
        with patch("growth._get_spreadsheet", return_value=mock_sh):
            result = read_latest_breakdown(TEST_GUILD_ID)
        assert result["has_data"] is False

    def test_parses_latest_transition_only(self, seeded_db):
        from growth import read_latest_breakdown
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )
        # Two transitions in the tab; the reader should pick the rightmost.
        bd_values = [
            [
                "Name",
                "Mar 2026 - Apr 2026 Power %",
                "Mar 2026 - Apr 2026 Power Bucket",
                "Apr 2026 - May 2026 Power %",
                "Apr 2026 - May 2026 Power Bucket",
            ],
            ["Alice", "5.00%", "Low", "30.00%", "Increased"],
            ["Bob", "-2.00%", "Decline", "1.00%", "None"],
        ]
        bd_ws = MagicMock()
        bd_ws.get_all_values = MagicMock(return_value=bd_values)
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=bd_ws)
        with patch("growth._get_spreadsheet", return_value=mock_sh):
            result = read_latest_breakdown(TEST_GUILD_ID)
        assert result["has_data"] is True
        assert result["prev_period_label"] == "Apr 2026"
        assert result["curr_period_label"] == "May 2026"
        assert result["metric_labels"] == ["Power"]
        # Latest transition: Alice → Increased, Bob → No Change. Bob's cell
        # carries the pre-#417 "None" label, which must still round-trip.
        assert result["summary"]["Power"]["increased"] == ["Alice"]
        assert result["summary"]["Power"]["none"] == ["Bob"]
        # Earlier transition's data must not leak through.
        assert result["summary"]["Power"]["decline"] == []

    def test_current_and_legacy_none_labels_both_map(self, seeded_db):
        """#417 renamed the `none` bucket's display label from "None" to "No
        Change". Breakdown tabs are never rewritten, so a single tab can hold
        both spellings and both must classify."""
        from growth import read_latest_breakdown
        from config import save_growth_config

        save_growth_config(
            TEST_GUILD_ID,
            enabled=1,
            tab_source="Powers",
            name_col="A",
            metrics=[{"col": "B", "label": "Power"}],
            tab_growth="Growth",
            snapshot_frequency="monthly",
            snapshot_day=1,
            snapshot_interval=30,
            data_start_row=2,
        )
        bd_values = [
            ["Name", "Apr 2026 - May 2026 Power %", "Apr 2026 - May 2026 Power Bucket"],
            ["Alice", "1.00%", "None"],  # written before the rename
            ["Bob", "2.00%", "No Change"],  # written after
        ]
        bd_ws = MagicMock()
        bd_ws.get_all_values = MagicMock(return_value=bd_values)
        mock_sh = MagicMock()
        mock_sh.worksheet = MagicMock(return_value=bd_ws)
        with patch("growth._get_spreadsheet", return_value=mock_sh):
            result = read_latest_breakdown(TEST_GUILD_ID)

        assert result["summary"]["Power"]["none"] == ["Alice", "Bob"]


# ── Next-snapshot date helper ─────────────────────────────────────────────────


class TestComputeNextSnapshot:
    """The wizard and /growth use this to tell users when the next snapshot
    will fire. Must stay aligned with the scheduling logic in
    bot.growth_task — same epoch (2026-01-01), same fire hour (22:00 ET)."""

    def _now(self, year, month, day, hour=12):
        from growth import ET

        return datetime(year, month, day, hour, 0, tzinfo=ET)

    def test_returns_none_when_disabled(self):
        from growth import compute_next_snapshot

        assert compute_next_snapshot({}) is None
        assert compute_next_snapshot({"enabled": 0}) is None

    # ── Monthly schedule ──────────────────────────────────────────────────────

    def test_monthly_today_before_snapshot_day_returns_this_month(self):
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 5, 1),
        )
        assert nxt.date().isoformat() == "2026-05-05"
        assert nxt.hour == 22

    def test_monthly_today_equals_snapshot_day_before_22_returns_today(self):
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 5, 5, hour=14),
        )
        assert nxt.date().isoformat() == "2026-05-05"

    def test_monthly_today_equals_snapshot_day_after_22_returns_next_month(self):
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 5, 5, hour=23),
        )
        assert nxt.date().isoformat() == "2026-06-05"

    def test_monthly_today_after_snapshot_day_returns_next_month(self):
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 5, 15),
        )
        assert nxt.date().isoformat() == "2026-06-05"

    def test_monthly_december_wraps_into_january(self):
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=self._now(2026, 12, 10),
        )
        assert nxt.date().isoformat() == "2027-01-05"

    def test_monthly_clamps_snapshot_day_to_28(self):
        """Stored snapshot_day is already clamped to 1..28 by the wizard,
        but the helper guards against bad data slipping through."""
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 99},
            now=self._now(2026, 5, 1),
        )
        # Clamps to 28, so May 28 is the next valid fire.
        assert nxt.day == 28

    # ── Interval schedule ─────────────────────────────────────────────────────

    def test_interval_today_is_epoch_before_22_returns_today(self):
        """2026-01-01 is the epoch — interval=14 means today is a fire day."""
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 14},
            now=self._now(2026, 1, 1, hour=14),
        )
        assert nxt.date().isoformat() == "2026-01-01"
        assert nxt.hour == 22

    def test_interval_today_is_epoch_after_22_skips_to_next_window(self):
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 14},
            now=self._now(2026, 1, 1, hour=23),
        )
        assert nxt.date().isoformat() == "2026-01-15"

    def test_interval_today_not_a_multiple_returns_next_multiple(self):
        from growth import compute_next_snapshot

        # Jan 5 → 4 days into the cycle, next 14-day mark is Jan 15.
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 14},
            now=self._now(2026, 1, 5),
        )
        assert nxt.date().isoformat() == "2026-01-15"

    def test_interval_one_day_returns_today_or_tomorrow(self):
        """interval=1 should always be today (before 22:00) or tomorrow."""
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 1},
            now=self._now(2026, 5, 15, hour=14),
        )
        assert nxt.date().isoformat() == "2026-05-15"

        nxt2 = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": 1},
            now=self._now(2026, 5, 15, hour=23),
        )
        assert nxt2.date().isoformat() == "2026-05-16"

    def test_interval_alignment_matches_scheduler_epoch(self):
        """Critical: the helper's "next fire" must agree with the
        scheduler's `(today - epoch).days % interval == 0` rule. If we
        ever change INTERVAL_EPOCH on one side and forget the other,
        users will see one date but get a snapshot on a different one."""
        from growth import compute_next_snapshot, INTERVAL_EPOCH

        # Sanity: epoch matches the bot.growth_task hardcode.
        assert INTERVAL_EPOCH.isoformat() == "2026-01-01"

        # Pick an arbitrary day, verify the helper returns a date that
        # would actually trigger the scheduler.
        from growth import ET
        from datetime import date as _date

        now = self._now(2026, 7, 4)
        for interval in (1, 7, 14, 30, 90):
            nxt = compute_next_snapshot(
                {"enabled": 1, "snapshot_frequency": "interval", "snapshot_interval": interval},
                now=now,
            )
            delta = (nxt.date() - INTERVAL_EPOCH).days
            assert delta % interval == 0, (
                f"interval={interval}: next={nxt.date()} (delta={delta}) is not a multiple"
            )

    # ── Defensive ─────────────────────────────────────────────────────────────

    def test_unknown_frequency_returns_none(self):
        from growth import compute_next_snapshot

        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "weekly"},
            now=self._now(2026, 5, 1),
        )
        assert nxt is None

    def test_naive_datetime_treated_as_et(self):
        """Pass a naive datetime — the helper should interpret it as ET
        rather than UTC, otherwise the "before 22:00" comparison wraps."""
        from growth import compute_next_snapshot

        naive = datetime(2026, 5, 5, 14, 0)  # no tzinfo
        nxt = compute_next_snapshot(
            {"enabled": 1, "snapshot_frequency": "monthly", "snapshot_day": 5},
            now=naive,
        )
        assert nxt.date().isoformat() == "2026-05-05"

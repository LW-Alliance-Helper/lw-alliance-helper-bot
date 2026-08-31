"""
Tests for volume_health — the Railway volume watchdog added in 1.8.9.

The failure it exists for is invisible to the obvious check. Railway's
volume is a thin-provisioned ZFS zvol: it allocates blocks on write and
never returns them when a file is deleted, so SQLite's default rollback
journal (created and deleted on every write transaction) climbed the
*reported* volume usage ~55 MB/day while `df` inside the container showed
under 2 MB. A threshold on `shutil.disk_usage` would have stayed silent the
whole time.

So the alarm watches the cause instead — a database not in WAL, or a
`-journal` file present on the volume — and those are what these cover.
"""

from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import volume_health  # noqa: E402


def _make_db(path, journal_mode="wal"):
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    conn.execute("CREATE TABLE IF NOT EXISTS things (id INTEGER PRIMARY KEY, body TEXT)")
    conn.executemany("INSERT INTO things (body) VALUES (?)", [("x" * 200,) for _ in range(50)])
    conn.commit()
    conn.close()
    return str(path)


# ── The alarm ────────────────────────────────────────────────────────────────


class TestCheck:
    def test_wal_database_raises_no_alarm(self, tmp_path):
        _make_db(tmp_path / "guild_configs.db", journal_mode="wal")

        assert volume_health.check(str(tmp_path)) == []

    def test_non_wal_database_is_flagged(self, tmp_path):
        """The whole incident in one condition. `delete` is SQLite's default
        rollback journal — the mode production was silently running."""
        _make_db(tmp_path / "guild_configs.db", journal_mode="delete")

        alarms = volume_health.check(str(tmp_path))

        assert len(alarms) == 1
        assert "journal_mode regression" in alarms[0]
        assert "guild_configs.db" in alarms[0]

    def test_a_stray_journal_file_is_flagged(self, tmp_path):
        """A `-journal` at rest is direct evidence the rollback journal is
        live right now, whatever the PRAGMA reports."""
        _make_db(tmp_path / "guild_configs.db", journal_mode="wal")
        (tmp_path / "guild_configs.db-journal").write_bytes(b"stale")

        alarms = volume_health.check(str(tmp_path))

        assert any("Rollback journal" in a for a in alarms)

    def test_filesystem_threshold_is_a_separate_alarm(self, tmp_path, monkeypatch):
        """The disk check cannot catch the zvol problem — it is kept for the
        ordinary one, a volume genuinely filling with data."""
        _make_db(tmp_path / "guild_configs.db", journal_mode="wal")
        monkeypatch.setattr(volume_health, "FILESYSTEM_ALERT_PERCENT", 0)

        alarms = volume_health.check(str(tmp_path))

        assert any("Filesystem at" in a for a in alarms)

    def test_missing_directory_does_not_raise(self, tmp_path):
        """The watchdog must never be the thing that breaks."""
        assert volume_health.check(str(tmp_path / "not-mounted")) == []


# ── Reading a database ───────────────────────────────────────────────────────


class TestDatabaseStats:
    def test_reports_journal_mode_and_pages(self, tmp_path):
        path = _make_db(tmp_path / "guild_configs.db", journal_mode="wal")

        stats = volume_health.database_stats(path)

        assert stats["error"] is None
        assert str(stats["journal_mode"]).lower() == "wal"
        assert stats["page_count"] > 0
        assert stats["file_bytes"] == stats["page_size"] * stats["page_count"]

    def test_ranks_tables_by_size(self, tmp_path):
        path = _make_db(tmp_path / "guild_configs.db")

        stats = volume_health.database_stats(path)

        assert stats["tables"], "expected at least one table"
        assert stats["tables"][0][0] == "things"

    def test_reading_never_writes(self, tmp_path):
        """A diagnostic that mutates what it is diagnosing is worse than no
        diagnostic. The connection is opened read-only and query_only."""
        path = _make_db(tmp_path / "guild_configs.db")
        volume_health.database_stats(path)

        conn = sqlite3.connect(path)
        count = conn.execute("SELECT COUNT(*) FROM things").fetchone()[0]
        conn.close()
        assert count == 50

    def test_unreadable_file_is_reported_not_raised(self, tmp_path):
        junk = tmp_path / "broken.db"
        junk.write_bytes(b"this is not a database")

        stats = volume_health.database_stats(str(junk))

        assert stats["error"] is not None


# ── The report ───────────────────────────────────────────────────────────────


class TestFormatReport:
    def test_lists_files_and_the_database(self, tmp_path):
        _make_db(tmp_path / "guild_configs.db", journal_mode="wal")

        report = volume_health.format_report(str(tmp_path))

        assert "guild_configs.db" in report
        assert "journal_mode=wal" in report
        assert "things" in report

    def test_calls_out_a_non_wal_database(self, tmp_path):
        _make_db(tmp_path / "guild_configs.db", journal_mode="delete")

        report = volume_health.format_report(str(tmp_path))

        assert "Not WAL" in report

    def test_empty_volume_says_so_instead_of_crashing(self, tmp_path):
        report = volume_health.format_report(str(tmp_path))

        assert "No files" in report


@pytest.mark.parametrize(
    "size,expected",
    [(512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")],
)
def test_human_sizes(size, expected):
    assert volume_health.human(size) == expected

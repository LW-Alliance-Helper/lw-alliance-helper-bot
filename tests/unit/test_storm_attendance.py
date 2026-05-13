"""
Tests for storm_attendance.py (#133).

Covers Sheet I/O round-trips (read rosters / read attendance / save),
session state (pre-fill from existing rows, status counts, page
slicing), and the embed render. The interactive view + status picker
are integration territory.
"""

import datetime as _dt
from unittest.mock import patch

import pytest

import storm_attendance as sa
from tests.unit.test_config import TEST_GUILD_ID


# ── Fake gspread plumbing ────────────────────────────────────────────────────


class _FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]] | None = None):
        self.title = title
        self._rows = list(rows or [])

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def append_row(self, row, value_input_option=None):
        self._rows.append([str(c) for c in row])

    def append_rows(self, rows, value_input_option=None):
        for r in rows:
            self._rows.append([str(c) for c in r])

    def clear(self):
        self._rows = []

    def update(self, range_, values, value_input_option=None):
        self._rows = [list(r) for r in values]


class _FakeSpreadsheet:
    def __init__(self):
        self._tabs: dict[str, _FakeWorksheet] = {}

    def worksheet(self, title: str):
        if title not in self._tabs:
            raise Exception("not found")
        return self._tabs[title]

    def add_worksheet(self, title: str, rows: int = 0, cols: int = 0):
        ws = _FakeWorksheet(title)
        self._tabs[title] = ws
        return ws


@pytest.fixture
def fake_env(seeded_db):
    """Seed a guild with structured flow on + a pre-built rosters tab
    representing one event. Patches config.get_spreadsheet."""
    import config

    fake = _FakeSpreadsheet()

    # Seed rosters_tab with 3 primary + 1 sub slots for DS 2026-05-18.
    rosters_ws = fake.add_worksheet("DS Rosters")
    rosters_ws._rows = [
        ["Event Date", "Team", "Zone", "Member", "Role",
         "Power at Assignment", "Discord ID", "Posted At (UTC)"],
        ["2026-05-18", "A", "Power Tower",  "Alice", "primary", "412000000", "1001", ""],
        ["2026-05-18", "A", "Power Tower",  "Bob",   "primary", "350000000", "1002", ""],
        ["2026-05-18", "A", "Nuclear Silo", "Carol", "primary", "280000000", "1003", ""],
        ["2026-05-18", "A", "",             "Dan",   "sub",     "220000000", "1004", ""],
        # Different event date — must NOT be included by the loader.
        ["2026-05-25", "A", "Power Tower", "Erin", "primary", "200000000", "1005", ""],
    ]

    for et in ("DS", "CS"):
        config.save_storm_config(
            TEST_GUILD_ID, et,
            tab_name=f"{et} Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, et, structured_flow_enabled=True,
        )

    with patch.object(config, "get_spreadsheet", return_value=fake):
        yield fake, TEST_GUILD_ID


# ── Loaders ──────────────────────────────────────────────────────────────────


class TestLoadRosteredSlots:
    def test_reads_primary_and_sub_for_event_date(self, fake_env):
        fake, gid = fake_env
        slots, errors = sa.load_rostered_slots(gid, "DS", "2026-05-18")
        assert errors == []
        names = {s["member"] for s in slots}
        assert names == {"Alice", "Bob", "Carol", "Dan"}
        # Roles preserved.
        dan = next(s for s in slots if s["member"] == "Dan")
        assert dan["role"] == "sub"

    def test_excludes_other_event_dates(self, fake_env):
        fake, gid = fake_env
        slots, _errs = sa.load_rostered_slots(gid, "DS", "2026-05-18")
        names = {s["member"] for s in slots}
        assert "Erin" not in names  # Erin is on 2026-05-25

    def test_missing_rosters_tab_returns_friendly_error(self, fake_env):
        fake, gid = fake_env
        # Delete the rosters tab.
        del fake._tabs["DS Rosters"]
        slots, errors = sa.load_rostered_slots(gid, "DS", "2026-05-18")
        assert slots == []
        assert errors and "doesn't exist" in errors[0]


class TestLoadAttendance:
    def test_empty_when_no_attendance_tab(self, fake_env):
        fake, gid = fake_env
        existing, errors = sa.load_attendance(gid, "DS", "2026-05-18")
        assert existing == {}
        assert errors == []

    def test_reads_prior_status(self, fake_env):
        fake, gid = fake_env
        # Pre-populate attendance tab.
        att_ws = fake.add_worksheet("DS Attendance")
        att_ws._rows = [
            list(sa._ATTENDANCE_HEADER),
            ["2026-05-18", "A", "Power Tower", "Alice", "attended", "999", ""],
            ["2026-05-18", "A", "Power Tower", "Bob",   "no_show",  "999", ""],
        ]
        existing, _errs = sa.load_attendance(gid, "DS", "2026-05-18")
        assert existing[("A", "Power Tower", "Alice")]["status"] == "attended"
        assert existing[("A", "Power Tower", "Bob")]["status"]   == "no_show"

    def test_isolates_event_dates(self, fake_env):
        fake, gid = fake_env
        att_ws = fake.add_worksheet("DS Attendance")
        att_ws._rows = [
            list(sa._ATTENDANCE_HEADER),
            ["2026-05-18", "A", "Power Tower", "Alice", "attended", "999", ""],
            ["2026-05-25", "A", "Power Tower", "Frank", "attended", "999", ""],
        ]
        existing, _errs = sa.load_attendance(gid, "DS", "2026-05-18")
        # Frank (different date) must not appear.
        keys = set(existing.keys())
        assert ("A", "Power Tower", "Frank") not in keys
        assert ("A", "Power Tower", "Alice") in keys


# ── Save ─────────────────────────────────────────────────────────────────────


class TestSaveAttendance:
    def test_writes_one_row_per_status(self, fake_env):
        fake, gid = fake_env
        statuses = {
            ("A", "Power Tower",  "Alice"): "attended",
            ("A", "Power Tower",  "Bob"):   "no_show",
            ("A", "Nuclear Silo", "Carol"): "sub_activated",
            ("A", "",             "Dan"):   "",            # unrecorded → skipped
        }
        errors = sa.save_attendance(
            gid, "DS", "2026-05-18",
            statuses=statuses, officer_id=999,
        )
        assert errors == []
        att = fake.worksheet("DS Attendance")
        rows = att.get_all_values()
        # Header + 3 recorded rows (Dan unrecorded → skipped).
        assert len(rows) == 4
        data = {(r[3], r[4]) for r in rows[1:]}
        assert ("Alice", "attended")      in data
        assert ("Bob",   "no_show")       in data
        assert ("Carol", "sub_activated") in data
        # Dan unrecorded → not present.
        assert not any(r[3] == "Dan" for r in rows[1:])

    def test_replaces_prior_rows_for_same_event(self, fake_env):
        fake, gid = fake_env
        # Pre-populate stale entries.
        att = fake.add_worksheet("DS Attendance")
        att._rows = [
            list(sa._ATTENDANCE_HEADER),
            ["2026-05-18", "A", "Power Tower", "Alice", "no_show", "111", ""],
            ["2026-05-18", "A", "Power Tower", "Bob",   "attended", "111", ""],
            ["2026-05-25", "B", "Power Tower", "Frank", "attended", "111", ""],
        ]
        statuses = {("A", "Power Tower", "Alice"): "attended"}
        errors = sa.save_attendance(
            gid, "DS", "2026-05-18",
            statuses=statuses, officer_id=999,
        )
        assert errors == []
        rows = att.get_all_values()
        # 2026-05-18 rows replaced; 2026-05-25 preserved.
        data_for_18 = [r for r in rows[1:] if r[0] == "2026-05-18"]
        assert len(data_for_18) == 1
        assert data_for_18[0][3] == "Alice"
        assert data_for_18[0][4] == "attended"
        # The unrelated event date row survives.
        assert any(r[0] == "2026-05-25" and r[3] == "Frank" for r in rows[1:])


# ── Session ──────────────────────────────────────────────────────────────────


def _slots_fixture():
    return [
        {"team": "A", "zone": "Power Tower",  "member": "Alice", "discord_id": "1", "role": "primary"},
        {"team": "A", "zone": "Power Tower",  "member": "Bob",   "discord_id": "2", "role": "primary"},
        {"team": "A", "zone": "Nuclear Silo", "member": "Carol", "discord_id": "3", "role": "primary"},
        {"team": "A", "zone": "",             "member": "Dan",   "discord_id": "4", "role": "sub"},
    ]


class TestSession:
    def test_init_prefills_from_existing(self):
        slots = _slots_fixture()
        existing = {
            ("A", "Power Tower", "Alice"): {"status": "attended", "recorded_by": "", "recorded_at": ""},
        }
        sess = sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=slots, existing=existing,
        )
        assert sess.statuses[("A", "Power Tower", "Alice")] == "attended"
        # Others default to unrecorded.
        assert sess.statuses[("A", "Power Tower", "Bob")] == ""

    def test_counts(self):
        slots = _slots_fixture()
        sess = sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=slots, existing={},
        )
        sess.statuses[("A", "Power Tower", "Alice")] = "attended"
        sess.statuses[("A", "Power Tower", "Bob")]   = "no_show"
        sess.statuses[("A", "Nuclear Silo", "Carol")] = "sub_activated"
        # Dan stays unrecorded.
        counts = sess.counts()
        assert counts["attended"]      == 1
        assert counts["no_show"]       == 1
        assert counts["sub_activated"] == 1
        assert counts[""]              == 1

    def test_pagination(self):
        slots = [{"team": "A", "zone": "Z", "member": f"M{i}", "discord_id": str(i), "role": "primary"}
                 for i in range(60)]
        sess = sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=slots, existing={},
        )
        assert sess.total_pages() == 3
        assert len(sess.page_slots()) == 25
        sess.page = 2
        assert len(sess.page_slots()) == 10


# ── Embed render ─────────────────────────────────────────────────────────────


class TestRenderEmbed:
    def test_empty_slots_message(self):
        sess = sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=[], existing={},
        )
        embed = sa._render_embed(sess)
        assert "No roster slots" in (embed.description or "")

    def test_renders_each_slot_with_status(self):
        slots = _slots_fixture()
        sess = sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=slots, existing={},
        )
        sess.statuses[("A", "Power Tower", "Alice")] = "attended"
        embed = sa._render_embed(sess)
        assert "Alice" in (embed.description or "")
        assert "Attended" in (embed.description or "")

    def test_footer_summary(self):
        slots = _slots_fixture()
        sess = sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=slots, existing={},
        )
        sess.statuses[("A", "Power Tower", "Alice")] = "attended"
        embed = sa._render_embed(sess)
        footer = embed.footer.text or ""
        # Footer has counts.
        assert "✅ 1" in footer
        assert "❌ 0" in footer

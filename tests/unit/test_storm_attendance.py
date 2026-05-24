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
        """Mimic gspread's range-anchored update.

        `range_` of "A1" overwrites starting at row 1; "A4" overwrites
        starting at row 4 (so the trailing-blank pattern works). Only
        the column-A anchor is parsed — sufficient for these tests.
        """
        import re
        m = re.match(r"^A(\d+)", str(range_))
        start_row = int(m.group(1)) - 1 if m else 0
        new_payload = [list(r) for r in values]
        # Pad self._rows with blank rows if the write starts past the
        # current end (defensive — gspread would auto-extend the sheet).
        while len(self._rows) < start_row:
            self._rows.append([""] * (len(self._rows[0]) if self._rows else 0))
        # Replace the rows at [start_row .. start_row + len(new_payload)).
        for i, row in enumerate(new_payload):
            target = start_row + i
            if target < len(self._rows):
                self._rows[target] = list(row)
            else:
                self._rows.append(list(row))


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
    # Header order matches storm_roster_builder._ROSTERS_HEADER from
    # production so a column-index regression is caught by these tests.
    rosters_ws = fake.add_worksheet("DS Rosters")
    rosters_ws._rows = [
        ["Event Date", "Team", "Zone", "Member", "Role",
         "Power at Assignment", "Discord ID", "Override Below Minimum",
         "Posted At (UTC)"],
        ["2026-05-18", "A", "Power Tower",  "Alice", "primary", "412000000", "1001", "",    ""],
        ["2026-05-18", "A", "Power Tower",  "Bob",   "primary", "350000000", "1002", "",    ""],
        ["2026-05-18", "A", "Nuclear Silo", "Carol", "primary", "280000000", "1003", "yes", ""],
        ["2026-05-18", "A", "",             "Dan",   "sub",     "220000000", "1004", "",    ""],
        # Different event date — must NOT be included by the loader.
        ["2026-05-25", "A", "Power Tower", "Erin", "primary", "200000000", "1005", "",    ""],
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

    def test_missing_rosters_tab_auto_creates_and_returns_empty(self, fake_env):
        """Rule D: the rosters tab auto-creates on first read. Attendance
        before any Approve & Post returns an empty slot list with no
        errors — the downstream UI surfaces 'No structured roster
        found for this date' via the empty-slot branch."""
        fake, gid = fake_env
        # Delete the rosters tab.
        del fake._tabs["DS Rosters"]
        slots, errors = sa.load_rostered_slots(gid, "DS", "2026-05-18")
        assert slots == []
        assert errors == []
        # Tab was recreated by the read.
        assert "DS Rosters" in fake._tabs


class TestLoadAttendance:
    """Post-#245 attendance reads from the unified `<DS|CS> Member
    Log` tab and expands each member's `showed_up` flag back across
    their assigned slots so the picker can pre-check existing
    recorded statuses."""

    def test_empty_when_no_slots_provided(self, fake_env):
        """Without a slots list there's no key to expand against —
        the function returns empty rather than guessing."""
        fake, gid = fake_env
        existing, errors = sa.load_attendance(gid, "DS", "2026-05-18", slots=[])
        assert existing == {}
        assert errors == []

    def test_empty_when_no_member_log_tab(self, fake_env):
        fake, gid = fake_env
        slots = [{"team": "A", "zone": "Power Tower", "member": "Alice",
                  "discord_id": "1", "role": "primary"}]
        existing, errors = sa.load_attendance(
            gid, "DS", "2026-05-18", slots=slots,
        )
        assert existing == {}
        assert errors == []

    def test_expands_yes_to_attended_for_assigned_slot(self, fake_env):
        """Post-#245 only `yes` is written for showed_up; historical
        `no` cells (from pre-fix writes) still read back as no_show
        on the picker so officers can correct them in place."""
        fake, gid = fake_env
        ml = fake.add_worksheet("DS Member Log")
        ml._rows = [
            ["Event Date", "Member", "showed_up"],
            ["2026-05-18", "Alice", "yes"],
            ["2026-05-18", "Bob",   "no"],  # legacy "no" still readable
        ]
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "discord_id": "1", "role": "primary"},
            {"team": "A", "zone": "Power Tower", "member": "Bob",
             "discord_id": "2", "role": "primary"},
        ]
        existing, _errs = sa.load_attendance(
            gid, "DS", "2026-05-18", slots=slots,
        )
        assert existing[("A", "Power Tower", "Alice")]["status"] == "attended"
        assert existing[("A", "Power Tower", "Bob")]["status"]   == "no_show"

    def test_isolates_event_dates(self, fake_env):
        fake, gid = fake_env
        ml = fake.add_worksheet("DS Member Log")
        ml._rows = [
            ["Event Date", "Member", "showed_up"],
            ["2026-05-18", "Alice", "yes"],
            ["2026-05-25", "Frank", "yes"],
        ]
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "discord_id": "1", "role": "primary"},
        ]
        existing, _errs = sa.load_attendance(
            gid, "DS", "2026-05-18", slots=slots,
        )
        keys = set(existing.keys())
        assert ("A", "Power Tower", "Alice") in keys
        assert not any(k[2] == "Frank" for k in keys)

    def test_unknown_member_flag_dropped(self, fake_env):
        """Member Log carries a flag for a member that isn't in the
        current roster slots (e.g., the member was unassigned after
        attendance was recorded). The expansion drops them — without
        slot context there's no team/zone key to assign."""
        fake, gid = fake_env
        ml = fake.add_worksheet("DS Member Log")
        ml._rows = [
            ["Event Date", "Member", "showed_up"],
            ["2026-05-18", "Ghost", "yes"],
        ]
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "discord_id": "1", "role": "primary"},
        ]
        existing, _errs = sa.load_attendance(
            gid, "DS", "2026-05-18", slots=slots,
        )
        assert existing == {}


# ── Save ─────────────────────────────────────────────────────────────────────


class TestSaveAttendanceWritesMemberLog:
    """Post-#245 `save_attendance` writes ONLY to the unified
    `<DS|CS> Member Log` tab. The legacy `DS Attendance` /
    `CS Attendance` tab is no longer appended to — existing data
    there is preserved on the Sheet but not touched by the bot."""

    def test_writes_showed_up_column_to_member_log(self, fake_env):
        """Only `attended` writes `yes`. Everything else (no_show,
        sub_activated, unrecorded) writes blank — see the docstring on
        `_collapse_slot_statuses_to_member_flag` for the rationale
        (member marked no_show may actually have sat out; blank
        avoids inflating no-show counts in Trends queries)."""
        fake, gid = fake_env
        statuses = {
            ("A", "Power Tower",  "Alice"): "attended",
            ("A", "Power Tower",  "Bob"):   "no_show",       # → ""
            ("A", "Nuclear Silo", "Carol"): "sub_activated",  # legacy → ""
            ("A", "",             "Dan"):   "",               # unrecorded → ""
        }
        errors = sa.save_attendance(
            gid, "DS", "2026-05-18",
            statuses=statuses, officer_id=999,
        )
        assert errors == []
        ml = fake.worksheet("DS Member Log")
        rows = ml.get_all_values()
        # Header + 4 member rows (Alice, Bob, Carol, Dan).
        assert rows[0] == ["Event Date", "Member", "showed_up"]
        data = {r[1]: r[2] for r in rows[1:]}
        assert data["Alice"] == "yes"
        assert data["Bob"]   == ""
        assert data["Carol"] == ""
        assert data["Dan"]   == ""

    def test_legacy_attendance_tab_untouched(self, fake_env):
        """The legacy `DS Attendance` tab is not created or written
        by the bot post-cutover. Officers who pre-populated it manually
        on the Sheet keep that history; the bot's writes go elsewhere."""
        fake, gid = fake_env
        sa.save_attendance(
            gid, "DS", "2026-05-18",
            statuses={("A", "Power Tower", "Alice"): "attended"},
            officer_id=999,
        )
        assert "DS Attendance" not in fake._tabs

    def test_rerun_for_same_event_replaces_member_rows(self, fake_env):
        """Officer re-runs attendance for the same event: the prior
        flags for those members are replaced cleanly (upsert), not
        duplicated. Rows for other event dates stay intact."""
        fake, gid = fake_env
        ml = fake.add_worksheet("DS Member Log")
        ml._rows = [
            ["Event Date", "Member", "showed_up"],
            ["2026-05-18", "Alice", "no"],
            ["2026-05-18", "Bob",   "no"],
            ["2026-05-25", "Alice", "yes"],
        ]
        sa.save_attendance(
            gid, "DS", "2026-05-18",
            statuses={("A", "Power Tower", "Alice"): "attended"},
            officer_id=999,
        )
        rows = ml.get_all_values()
        # No duplicate Alice row for 2026-05-18; the 2026-05-25 entry
        # is preserved.
        on_18 = [r for r in rows[1:] if r[0] == "2026-05-18"]
        alice_18 = [r for r in on_18 if r[1] == "Alice"]
        assert len(alice_18) == 1
        assert alice_18[0][2] == "yes"
        on_25 = [r for r in rows[1:] if r[0] == "2026-05-25"]
        assert any(r[1] == "Alice" and r[2] == "yes" for r in on_25)

    def test_multi_slot_attended_anywhere_collapses_to_yes(self, fake_env):
        """A member playing two slots (different zones, same team) —
        attended on one, unrecorded on the other — collapses to
        showed_up=yes. Aggregation rule: ANY attended → yes."""
        fake, gid = fake_env
        statuses = {
            ("A", "Power Tower",  "Alice"): "attended",
            ("A", "Nuclear Silo", "Alice"): "",          # unrecorded
        }
        sa.save_attendance(
            gid, "DS", "2026-05-18",
            statuses=statuses, officer_id=999,
        )
        ml = fake.worksheet("DS Member Log")
        rows = ml.get_all_values()
        alice_rows = [r for r in rows[1:] if r[1] == "Alice"]
        assert len(alice_rows) == 1
        assert alice_rows[0][2] == "yes"

    def test_multi_slot_no_show_anywhere_collapses_to_blank(self, fake_env):
        """A member playing two slots, no_show on one and unrecorded
        on the other → showed_up="" (blank). The `no_show` mark
        alone doesn't drive a "no" cell because the member may have
        actually sat out — we only ever write a positive `yes` when
        explicitly marked attended."""
        fake, gid = fake_env
        statuses = {
            ("A", "Power Tower",  "Bob"): "no_show",
            ("A", "Nuclear Silo", "Bob"): "",
        }
        sa.save_attendance(
            gid, "DS", "2026-05-18",
            statuses=statuses, officer_id=999,
        )
        ml = fake.worksheet("DS Member Log")
        rows = ml.get_all_values()
        bob_rows = [r for r in rows[1:] if r[1] == "Bob"]
        assert len(bob_rows) == 1
        assert bob_rows[0][2] == ""

    def test_empty_statuses_short_circuits(self, fake_env):
        """No slots at all → nothing to write, no errors."""
        fake, gid = fake_env
        errors = sa.save_attendance(
            gid, "DS", "2026-05-18", statuses={}, officer_id=999,
        )
        assert errors == []
        # No tab created when there's nothing to write.
        assert "DS Member Log" not in fake._tabs


class TestCollapseHelper:
    """Direct test of the per-slot → per-member status aggregation."""

    def test_attended_wins_over_no_show(self):
        flags = sa._collapse_slot_statuses_to_member_flag({
            ("A", "Z1", "Alice"): "attended",
            ("A", "Z2", "Alice"): "no_show",
        })
        assert flags["Alice"] == "yes"

    def test_no_show_only_collapses_to_blank(self):
        """`no_show` is no longer written as a "no" cell — see the
        collapse helper's docstring for why (member marked no_show
        may actually have sat out)."""
        flags = sa._collapse_slot_statuses_to_member_flag({
            ("A", "Z1", "Bob"): "no_show",
        })
        assert flags["Bob"] == ""

    def test_unrecorded_only(self):
        flags = sa._collapse_slot_statuses_to_member_flag({
            ("A", "Z1", "Carol"): "",
        })
        assert flags["Carol"] == ""

    def test_legacy_sub_activated_treated_as_unrecorded(self):
        flags = sa._collapse_slot_statuses_to_member_flag({
            ("A", "Z1", "Dan"): "sub_activated",
        })
        assert flags["Dan"] == ""

    def test_blank_member_skipped(self):
        flags = sa._collapse_slot_statuses_to_member_flag({
            ("A", "Z1", ""): "attended",
        })
        assert flags == {}


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

    def test_footer_drops_sub_activated_count(self):
        """Rule K (#171): footer is ✅ / ❌ / — only. Any legacy
        `sub_activated` rows count toward the unrecorded bucket so the
        math stays correct, but the bare 🔄 column is gone."""
        slots = _slots_fixture()
        sess = sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=slots, existing={},
        )
        sess.statuses[("A", "Power Tower", "Alice")] = "attended"
        sess.statuses[("A", "Power Tower", "Bob")] = "sub_activated"
        embed = sa._render_embed(sess)
        footer = embed.footer.text or ""
        assert "🔄" not in footer
        assert "✅ 1" in footer
        assert "❌ 0" in footer
        # 3 unrecorded slots (Carol, Dan) plus Bob's legacy
        # sub_activated row roll into the — bucket → 3 total.
        assert "— 3" in footer


class TestAttendanceViewInteractions:
    """#171 / Decision #5: the view's two action buttons branch on
    whether a slot is selected — bulk-mark when empty, single-write
    when selected. Empty-state hides the buttons entirely. The pre-
    #171 `_StatusPickerView` ephemeral is gone (and no longer exists)."""

    def _session(self):
        slots = _slots_fixture()
        return sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=slots, existing={},
        )

    def test_status_picker_view_removed(self):
        assert not hasattr(sa, "_StatusPickerView")

    def test_sub_activated_is_no_longer_a_pickable_status(self):
        assert sa.STATUS_SUB_ACTIVATED not in sa._VALID_STATUSES

    def test_empty_state_hides_action_buttons(self):
        sess = sa._AttendanceSession(
            guild_id=1, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=[], existing={},
        )
        view = sa._AttendanceView(sess)
        # Empty roster → no buttons or select.
        assert view.children == []

    def test_no_selection_buttons_say_mark_all(self):
        view = sa._AttendanceView(self._session())
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Mark all unrecorded as attended" in lab for lab in labels)
        assert any("Mark all unrecorded as did not attend" in lab for lab in labels)
        assert not any("Mark as attended" in lab and "all" not in lab for lab in labels)

    def test_selection_swaps_buttons_to_single_mode(self):
        view = sa._AttendanceView(self._session())
        view.selected_key = ("A", "Power Tower", "Alice")
        view._build()
        labels = [getattr(c, "label", "") for c in view.children]
        assert any(lab == "✅ Mark as attended" for lab in labels)
        assert any(lab == "❌ Mark as did not attend" for lab in labels)
        # The all-variant copy is gone now.
        assert not any("Mark all" in lab for lab in labels)


class TestOverrideBelowFloorSurface:
    """The Override Below Minimum column from rosters_tab (added in
    audit commit 15509bb; renamed from `Override Below Floor` in the
    Rule B header-rename pass) is surfaced in the attendance view so
    leadership sees which slots were below-minimum at build time when
    recording attendance — same audit lineage as the rosters_tab
    column itself."""

    def test_override_flag_read_from_rosters_tab(self, fake_env):
        fake, gid = fake_env
        slots, _errs = sa.load_rostered_slots(gid, "DS", "2026-05-18")
        by_name = {s["member"]: s for s in slots}
        assert by_name["Carol"]["override_below_floor"] is True
        assert by_name["Alice"]["override_below_floor"] is False

    def test_legacy_override_header_still_readable(self, fake_env):
        """Dev/staging sheets pre-Rule-B-rename carried the column
        as `Override Below Floor`. The reader falls through to the
        legacy name so existing flagged rows continue to surface
        until `_write_rosters_tab` runs the header migration."""
        fake, gid = fake_env
        rosters = fake.worksheet("DS Rosters")
        rosters._rows[0][7] = "Override Below Floor"  # revert header
        slots, _errs = sa.load_rostered_slots(gid, "DS", "2026-05-18")
        by_name = {s["member"]: s for s in slots}
        assert by_name["Carol"]["override_below_floor"] is True
        assert by_name["Alice"]["override_below_floor"] is False

    def test_override_marker_not_rendered_in_embed(self, fake_env):
        """Decision #6 (#171): the Override Below Minimum ⚠️ glyph + the
        trailing "Assigned below the zone minimum" footnote are dropped
        from the attendance UI. The Sheet still records the flag for
        post-event audit (see `test_override_flag_read_from_rosters_tab`)
        but officers recording attendance don't need it surfaced."""
        fake, gid = fake_env
        slots, _ = sa.load_rostered_slots(gid, "DS", "2026-05-18")
        sess = sa._AttendanceSession(
            guild_id=gid, user_id=42, event_type="DS",
            event_date="2026-05-18", slots=slots, existing={},
        )
        embed = sa._render_embed(sess)
        body = embed.description or ""
        # Neither member's row carries the ⚠️ glyph anymore.
        carol_line = next(line for line in body.split("\n") if "Carol" in line)
        alice_line = next(line for line in body.split("\n") if "Alice" in line)
        assert "⚠️" not in carol_line
        assert "⚠️" not in alice_line
        # Footnote is gone.
        assert "Assigned below the zone minimum" not in body

    def test_override_truthy_values_all_accepted(self, fake_env):
        # Officers may hand-edit the Sheet — accept the usual yes-set.
        fake, gid = fake_env
        rosters = fake.worksheet("DS Rosters")
        rosters._rows[1][7] = "1"      # Alice
        rosters._rows[2][7] = "TRUE"   # Bob
        rosters._rows[4][7] = "x"      # Dan
        slots, _ = sa.load_rostered_slots(gid, "DS", "2026-05-18")
        by_name = {s["member"]: s for s in slots}
        assert by_name["Alice"]["override_below_floor"] is True
        assert by_name["Bob"]["override_below_floor"] is True
        assert by_name["Dan"]["override_below_floor"] is True


class TestSaveAttendanceMemberLogAtomicity:
    """Member-Log write semantics live in `storm_log.upsert_member_log_rows`
    (covered in `test_storm_member_log.py`). At the attendance layer we
    confirm only that a Member-Log write failure surfaces as a soft
    error rather than swallowing silently."""

    def test_member_log_write_failure_surfaces_soft_error(
        self, fake_env, monkeypatch,
    ):
        fake, gid = fake_env

        def _raise_upsert(*args, **kwargs):
            raise RuntimeError("simulated 503")

        with patch("storm_log.upsert_member_log_rows", _raise_upsert):
            errors = sa.save_attendance(
                gid, "DS", "2026-05-18",
                statuses={("A", "Power Tower", "Alice"): "attended"},
                officer_id=999,
            )
        assert errors and "member-log write failed" in errors[0]

"""
Tests for storm_history.py (#135).

Covers the Sheet readers (list event dates, load roster, load attendance)
and the renderers (event embed + history list embed). The slash command
+ button view are integration territory.
"""

import pytest
from unittest.mock import patch

import storm_history as sh
from tests.unit.test_config import TEST_GUILD_ID


class _FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]] | None = None):
        self.title = title
        self._rows = list(rows or [])

    def get_all_values(self):
        return [list(r) for r in self._rows]


class _FakeSpreadsheet:
    def __init__(self):
        self._tabs: dict[str, _FakeWorksheet] = {}

    def worksheet(self, title: str):
        if title not in self._tabs:
            raise Exception("not found")
        return self._tabs[title]


@pytest.fixture
def fake_env(seeded_db):
    import config

    fake = _FakeSpreadsheet()

    # Two events on the rosters tab.
    rosters = _FakeWorksheet("DS Rosters", [
        ["Event Date", "Team", "Zone", "Member", "Role",
         "Power at Assignment", "Discord ID", "Posted At (UTC)",
         "Override Below Floor"],
        ["2026-05-18", "A", "Power Tower",  "Alice", "primary", "412000000", "1001", "", ""],
        ["2026-05-18", "A", "Power Tower",  "Bob",   "primary", "350000000", "1002", "", ""],
        ["2026-05-18", "A", "Nuclear Silo", "Carol", "primary", "280000000", "1003", "", ""],
        ["2026-05-18", "A", "",             "Dan",   "sub",     "220000000", "1004", "", ""],
        ["2026-05-11", "A", "Power Tower",  "Alice", "primary", "400000000", "1001", "", ""],
        ["2026-05-11", "A", "Power Tower",  "Erin",  "primary", "190000000", "1005", "",
         "yes"],  # below-floor override
    ])
    fake._tabs["DS Rosters"] = rosters

    attendance = _FakeWorksheet("DS Attendance", [
        ["Event Date", "Team", "Zone", "Member", "Status",
         "Recorded By", "Recorded At (UTC)"],
        ["2026-05-18", "A", "Power Tower",  "Alice", "attended",      "999", ""],
        ["2026-05-18", "A", "Power Tower",  "Bob",   "no_show",       "999", ""],
        ["2026-05-18", "A", "Nuclear Silo", "Carol", "sub_activated", "999", ""],
    ])
    fake._tabs["DS Attendance"] = attendance

    for et in ("DS", "CS"):
        config.save_storm_config(
            TEST_GUILD_ID, et, tab_name=f"{et} Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, et, structured_flow_enabled=True,
        )

    with patch.object(config, "get_spreadsheet", return_value=fake):
        yield fake, TEST_GUILD_ID


# ── Loaders ──────────────────────────────────────────────────────────────────


class TestListEventDates:
    def test_returns_unique_descending(self, fake_env):
        fake, gid = fake_env
        dates, errors = sh.list_event_dates(gid, "DS", limit=8)
        assert errors == []
        # Two distinct dates, newer first.
        assert dates == ["2026-05-18", "2026-05-11"]

    def test_limit_truncates(self, fake_env):
        fake, gid = fake_env
        dates, _ = sh.list_event_dates(gid, "DS", limit=1)
        assert dates == ["2026-05-18"]

    def test_empty_when_no_tab(self, fake_env):
        fake, gid = fake_env
        del fake._tabs["DS Rosters"]
        dates, errors = sh.list_event_dates(gid, "DS", limit=8)
        assert dates == []
        assert errors == []  # missing tab = no history yet, not an error


class TestLoadEventRoster:
    def test_filters_to_event_date(self, fake_env):
        fake, gid = fake_env
        slots, errors = sh.load_event_roster(gid, "DS", "2026-05-18")
        assert errors == []
        names = {s["member"] for s in slots}
        assert names == {"Alice", "Bob", "Carol", "Dan"}
        # Erin (on 2026-05-11) not included.
        assert "Erin" not in names

    def test_captures_override_below_floor(self, fake_env):
        fake, gid = fake_env
        slots, _errs = sh.load_event_roster(gid, "DS", "2026-05-11")
        erin = next(s for s in slots if s["member"] == "Erin")
        assert erin["override_below_floor"] is True
        alice = next(s for s in slots if s["member"] == "Alice")
        assert alice["override_below_floor"] is False

    def test_captures_role(self, fake_env):
        fake, gid = fake_env
        slots, _errs = sh.load_event_roster(gid, "DS", "2026-05-18")
        dan = next(s for s in slots if s["member"] == "Dan")
        assert dan["role"] == "sub"

    def test_missing_event_returns_empty(self, fake_env):
        fake, gid = fake_env
        slots, _errs = sh.load_event_roster(gid, "DS", "2099-01-01")
        assert slots == []


class TestLoadEventAttendance:
    def test_returns_keyed_dict(self, fake_env):
        fake, gid = fake_env
        att, errors = sh.load_event_attendance(gid, "DS", "2026-05-18")
        assert errors == []
        assert att[("A", "Power Tower",  "Alice")] == "attended"
        assert att[("A", "Power Tower",  "Bob")]   == "no_show"
        assert att[("A", "Nuclear Silo", "Carol")] == "sub_activated"

    def test_missing_attendance_returns_empty(self, fake_env):
        fake, gid = fake_env
        del fake._tabs["DS Attendance"]
        att, errors = sh.load_event_attendance(gid, "DS", "2026-05-18")
        assert att == {}
        assert errors == []


# ── Renderers ────────────────────────────────────────────────────────────────


class TestRenderEventEmbed:
    def test_renders_attendance_glyphs(self):
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "role": "primary", "power": "412000000",
             "discord_id": "1", "override_below_floor": False},
        ]
        attendance = {("A", "Power Tower", "Alice"): "attended"}
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance=attendance,
        )
        assert "Alice" in (embed.description or "")
        assert "✅" in (embed.description or "")

    def test_no_attendance_falls_through(self):
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "role": "primary", "power": "412000000",
             "discord_id": "1", "override_below_floor": False},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        # Renders an unrecorded marker, not crash.
        assert "—" in (embed.description or "")
        # Footer hints how to record.
        assert "/storm_attendance" in (embed.footer.text or "")

    def test_below_floor_override_visible(self):
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Erin",
             "role": "primary", "power": "190000000",
             "discord_id": "5", "override_below_floor": True},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-11",
            slots=slots, attendance={},
        )
        assert "override" in (embed.description or "").lower()

    def test_empty_slots_message(self):
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=[], attendance={},
        )
        assert "No structured roster" in (embed.description or "")

    def test_grouping_by_team_and_zone(self):
        slots = [
            {"team": "A", "zone": "Power Tower",  "member": "Alice",
             "role": "primary", "power": "", "discord_id": "1", "override_below_floor": False},
            {"team": "A", "zone": "Nuclear Silo", "member": "Bob",
             "role": "primary", "power": "", "discord_id": "2", "override_below_floor": False},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        body = embed.description or ""
        assert "Team A" in body
        # Zone headers present.
        assert "Power Tower"  in body
        assert "Nuclear Silo" in body

    def test_footer_summary_counts(self):
        slots = [
            {"team": "A", "zone": "Z", "member": f"M{i}", "role": "primary",
             "power": "", "discord_id": str(i), "override_below_floor": False}
            for i in range(3)
        ]
        attendance = {
            ("A", "Z", "M0"): "attended",
            ("A", "Z", "M1"): "no_show",
            ("A", "Z", "M2"): "sub_activated",
        }
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance=attendance,
        )
        footer = embed.footer.text or ""
        assert "✅ 1" in footer
        assert "❌ 1" in footer
        assert "🔄 1" in footer
        assert "recorded 3 of 3" in footer


class TestRenderHistoryListEmbed:
    def test_empty_list_message(self):
        embed = sh.render_history_list_embed("DS", [])
        assert "No structured rosters" in (embed.description or "")

    def test_populated_list_has_clickable_hint(self):
        embed = sh.render_history_list_embed("DS", ["2026-05-18", "2026-05-11"])
        # The actual buttons are on the View, not the embed; the embed
        # just sets up context.
        assert "Click" in (embed.description or "")

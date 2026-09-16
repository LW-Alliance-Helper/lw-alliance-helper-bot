"""
Characterization tests for the roster power reader
(`storm_roster_builder._read_roster_powers`, moved to
`storm_roster_powers.read_roster_powers` in the #589 step 11 refactor).

`tests/unit/test_storm_roster_builder.py` already pins same-tab power
parsing, the legacy `not_on_discord` flag, the cross-tab power overlay
and the last-updated overlay. These pin the rest: the bot-maintained
presence column, the live-guild inference and its stale-ID notice, the
participation alias column, the failure exits, the row-skip rules and
the last-updated match-column fallback. Written against the pre-refactor
function; they pass unchanged against the refactored one.
"""

from unittest.mock import MagicMock, patch

import pytest

import storm_roster_builder as srb
from tests.unit.test_config import TEST_GUILD_ID
from tests.unit.test_storm_roster_builder import fake_env  # noqa: F401  (fixture)


def _read(gid, guild=None):
    return srb._read_roster_powers(gid, "DS", guild=guild)


def _rows(fake, rows):
    fake.worksheet("Member Roster")._rows = rows


HEADER = ["Discord ID", "Name", "Display Name", "Joined", "Roles", "1st Squad Power"]


def _guild(members: dict[int, object]):
    """A guild whose `get_member` answers from `members` (None = gone)."""
    g = MagicMock()
    g.get_member = MagicMock(side_effect=lambda i: members.get(i))
    return g


def _member(name="Someone", bot=False):
    m = MagicMock()
    m.display_name = name
    m.bot = bot
    return m


# ── Result shape ─────────────────────────────────────────────────────────────


class TestShape:
    def test_every_member_carries_the_five_fields(self, fake_env):
        fake, gid = fake_env
        members, errs = _read(gid)
        assert errs == []
        for key, m in members.items():
            assert set(m) == {"key", "name", "discord_id", "power", "not_on_discord"}
            assert m["key"] == key

    def test_display_name_is_the_name(self, fake_env):
        fake, gid = fake_env
        members, _ = _read(gid)
        assert members["1001"]["name"] == "Alice"

    def test_rows_with_nothing_are_skipped_and_name_only_rows_key_by_name(self, fake_env):
        fake, gid = fake_env
        _rows(
            fake,
            [
                HEADER,
                ["", "", "", "", "", "100M"],
                ["", "zed", "", "", "", "90M"],
            ],
        )
        members, _ = _read(gid)
        assert list(members) == ["zed"]
        assert members["zed"] == {
            "key": "zed",
            "name": "zed",
            "discord_id": "",
            "power": 90_000_000,
            "not_on_discord": True,
        }

    def test_duplicate_ids_last_row_wins(self, fake_env):
        fake, gid = fake_env
        _rows(
            fake,
            [HEADER, ["1001", "a", "First", "", "", "1M"], ["1001", "a", "Second", "", "", "2M"]],
        )
        members, _ = _read(gid)
        assert members["1001"]["name"] == "Second"
        assert members["1001"]["power"] == 2_000_000


# ── Non-Discord detection ────────────────────────────────────────────────────


class TestPresenceColumn:
    def _rows_with_presence(self, fake, presence_by_id):
        rows = [HEADER + ["Is this user in Discord?"]]
        for i, (did, presence) in enumerate(presence_by_id.items()):
            rows.append([did, f"n{i}", f"N{i}", "", "", "10M", presence])
        _rows(fake, rows)

    def test_yes_and_no_are_definitive(self, fake_env):
        fake, gid = fake_env
        self._rows_with_presence(fake, {"1001": "Yes", "1002": "no"})
        # Even with a guild that knows neither member, the column wins.
        members, errs = _read(gid, guild=_guild({}))
        assert members["1001"]["not_on_discord"] is False
        assert members["1002"]["not_on_discord"] is True
        assert errs == []

    def test_blank_presence_falls_through_to_inference(self, fake_env):
        fake, gid = fake_env
        self._rows_with_presence(fake, {"1001": "", "1002": "maybe"})
        members, errs = _read(gid, guild=_guild({1001: _member("Alice")}))
        assert members["1001"]["not_on_discord"] is False
        assert members["1002"]["not_on_discord"] is True
        assert errs == ["stale Discord IDs on roster (member likely left the server): N1 (id 1002)"]

    def test_presence_no_keeps_a_blank_id_row_keyed_by_name(self, fake_env):
        fake, gid = fake_env
        _rows(
            fake, [HEADER + ["Is this user in Discord?"], ["", "dave", "Dave", "", "", "5M", "No"]]
        )
        members, _ = _read(gid)
        assert members["Dave"]["not_on_discord"] is True
        assert members["Dave"]["discord_id"] == ""


class TestInference:
    def test_no_guild_means_every_id_row_is_on_discord(self, fake_env):
        fake, gid = fake_env
        members, errs = _read(gid)
        assert members["1001"]["not_on_discord"] is False
        assert errs == []

    def test_member_gone_or_bot_is_stale(self, fake_env):
        fake, gid = fake_env
        guild = _guild({1001: _member("Alice"), 1002: _member("Bot", bot=True)})
        members, errs = _read(gid, guild=guild)
        assert members["1001"]["not_on_discord"] is False
        assert members["1002"]["not_on_discord"] is True  # bot
        assert members["1003"]["not_on_discord"] is True  # gone
        assert errs == [
            "stale Discord IDs on roster (member likely left the server): "
            "Bob (id 1002), Carol (id 1003), Erin (id 1004), Frank (id 1005)"
        ]

    def test_stale_notice_caps_at_five_names(self, fake_env):
        fake, gid = fake_env
        rows = [HEADER] + [[str(2000 + i), f"m{i}", f"M{i}", "", "", "1M"] for i in range(7)]
        _rows(fake, rows)
        _, errs = _read(gid, guild=_guild({}))
        assert errs == [
            "stale Discord IDs on roster (member likely left the server): "
            "M0 (id 2000), M1 (id 2001), M2 (id 2002), M3 (id 2003), M4 (id 2004) (+2 more)"
        ]

    def test_non_numeric_id_is_a_placeholder(self, fake_env):
        fake, gid = fake_env
        _rows(fake, [HEADER, ["TBD", "g", "Ghost", "", "", "1M"]])
        members, errs = _read(gid, guild=_guild({}))
        assert members["TBD"]["not_on_discord"] is True
        assert members["TBD"]["discord_id"] == "TBD"
        assert errs == []

    def test_legacy_flag_beats_a_live_member(self, fake_env):
        fake, gid = fake_env
        _rows(
            fake,
            [HEADER + ["not on discord"], ["1001", "a", "Alice", "", "", "1M", "yes"]],
        )
        members, errs = _read(gid, guild=_guild({1001: _member("Alice")}))
        assert members["1001"]["not_on_discord"] is True
        assert errs == []


# ── Column resolution ────────────────────────────────────────────────────────


class TestColumns:
    def test_participation_alias_column_overrides_display(self, fake_env):
        import config

        fake, gid = fake_env
        config.save_participation_config(
            gid,
            "DS",
            enabled=1,
            tab_name="Log",
            questions=[],
            roster_tab="Member Roster",
            roster_name_col=0,
            roster_alias_col=1,
            roster_start_row=2,
        )
        members, _ = _read(gid)
        assert members["1001"]["name"] == "alice"

    def test_alias_column_off_uses_display_column(self, fake_env):
        import config

        fake, gid = fake_env
        config.save_participation_config(
            gid,
            "DS",
            enabled=1,
            tab_name="Log",
            questions=[],
            roster_tab="Member Roster",
            roster_name_col=0,
            roster_alias_col=-1,
            roster_start_row=2,
        )
        members, _ = _read(gid)
        assert members["1001"]["name"] == "Alice"

    def test_blank_display_falls_back_to_name_then_guild(self, fake_env):
        fake, gid = fake_env
        _rows(fake, [HEADER, ["1001", "alice", "", "", "", "1M"], ["1002", "", "", "", "", "2M"]])
        members, _ = _read(gid, guild=_guild({1001: _member("Nick"), 1002: _member("Live")}))
        assert members["1001"]["name"] == "alice"
        assert members["1002"]["name"] == "Live"


# ── Exits ────────────────────────────────────────────────────────────────────


class TestExits:
    def test_sync_disabled(self, fake_env):
        import config

        fake, gid = fake_env
        config.save_member_roster_config(gid, enabled=0, tab_name="Member Roster")
        assert _read(gid) == (
            {},
            [
                "member-roster sync isn't enabled — without /members sync the "
                "builder can't see your alliance's roster."
            ],
        )

    def test_sheet_read_failure(self, fake_env):
        fake, gid = fake_env
        with patch("config.read_member_roster_values", side_effect=RuntimeError("quota")):
            assert _read(gid) == ({}, ["roster-sheet read failed: quota"])

    def test_empty_sheet(self, fake_env):
        fake, gid = fake_env
        _rows(fake, [])
        assert _read(gid) == ({}, [])

    def test_header_only_sheet(self, fake_env):
        fake, gid = fake_env
        _rows(fake, [HEADER])
        assert _read(gid) == ({}, [])

    def test_config_read_failures(self, fake_env):
        fake, gid = fake_env
        with patch("config.get_member_roster_config", side_effect=RuntimeError("db")):
            assert _read(gid) == ({}, ["roster-config read failed: db"])
        with patch("config.get_structured_storm_config", side_effect=RuntimeError("db2")):
            assert _read(gid) == ({}, ["structured-config read failed: db2"])


# ── Overlays ─────────────────────────────────────────────────────────────────


class TestOverlays:
    def test_cross_tab_uses_the_configured_match_column(self, fake_env):
        import config

        fake, gid = fake_env
        powers = fake.add_worksheet("Powers")
        powers._rows = [["Who", "Power", "ID"], ["Alice", "7M", "1001"], ["x", "9M", "1002"]]
        config.save_structured_storm_config(
            gid,
            "DS",
            structured_flow_enabled=True,
            power_metric_tab="Powers",
            power_metric_column="B",
            power_match_column="C",
        )
        members, errs = _read(gid)
        assert errs == []
        assert members["1001"]["power"] == 7_000_000
        assert members["1002"]["power"] == 9_000_000
        assert members["1003"]["power"] is None

    def test_last_updated_match_falls_back_to_the_power_match_column(self, fake_env):
        import config
        from datetime import date

        fake, gid = fake_env
        lu = fake.add_worksheet("Updated")
        lu._rows = [["Who", "When", "ID"], ["?", "2026-09-01", "1001"], ["?", "2026-08-15", "1002"]]
        config.save_structured_storm_config(
            gid,
            "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
            power_match_column="C",  # unused for same-tab power, reused for last-updated
            power_last_updated_tab="Updated",
            power_last_updated_column="B",
            power_last_updated_match_column="",
        )
        members, errs = _read(gid)
        assert errs == []
        assert members["1001"]["last_updated"] == date(2026, 9, 1)
        assert members["1002"]["last_updated"] == date(2026, 8, 15)
        assert members["1003"]["last_updated"] is None

    def test_last_updated_explicit_match_column(self, fake_env):
        import config
        from datetime import date

        fake, gid = fake_env
        lu = fake.add_worksheet("Updated")
        lu._rows = [["Name", "When"], ["Alice", "09/01/2026"], ["Bob", "bad date"]]
        config.save_structured_storm_config(
            gid,
            "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
            power_last_updated_tab="Updated",
            power_last_updated_column="B",
            power_last_updated_match_column="A",
        )
        members, errs = _read(gid)
        assert errs == []
        assert members["1001"]["last_updated"] == date(2026, 9, 1)
        assert members["1002"]["last_updated"] is None

    def test_no_last_updated_config_means_no_key(self, fake_env):
        fake, gid = fake_env
        members, _ = _read(gid)
        assert all("last_updated" not in m for m in members.values())

    def test_overlays_skip_an_empty_roster(self, fake_env):
        import config

        fake, gid = fake_env
        _rows(fake, [HEADER])
        config.save_structured_storm_config(
            gid,
            "DS",
            structured_flow_enabled=True,
            power_metric_tab="Nope",
            power_metric_column="B",
            power_last_updated_tab="Nope",
            power_last_updated_column="B",
        )
        # Neither missing tab is opened, so no soft error surfaces.
        assert _read(gid) == ({}, [])

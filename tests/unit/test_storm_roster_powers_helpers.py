"""The pure pieces of the roster power reader (`storm_roster_powers`):
column resolution, the per-row parse, the not-on-Discord verdict and the
stale-ID notice. No sheet, no database."""

from unittest.mock import MagicMock

import pytest

import storm_roster_powers as srp

HEADER = ["Discord ID", "Name", "Display Name", "Joined", "Roles", "Power", "not_on_discord"]
ROSTER_CFG = {
    "enabled": True,
    "tab_name": "Member Roster",
    "discord_id_col": 0,
    "name_col": 1,
    "display_col": 2,
}
STRUCTURED = {"power_metric_column": "F", "power_metric_tab": "", "power_match_column": ""}
NO_ALIAS = {"roster_alias_col": -1}


def _cols(header=HEADER, roster_cfg=ROSTER_CFG, structured=STRUCTURED, participation=NO_ALIAS):
    return srp.resolve_columns(header, roster_cfg, structured, participation)


def _guild(members):
    g = MagicMock()
    g.get_member = MagicMock(side_effect=lambda i: members.get(i))
    return g


def _member(bot=False, name="Live"):
    m = MagicMock()
    m.bot = bot
    m.display_name = name
    return m


class TestSmallHelpers:
    @pytest.mark.parametrize(
        "value, expected",
        [("b", "B"), (" c ", "C"), ("", ""), (None, ""), ("AA", ""), ("1", "")],
    )
    def test_col_letter(self, value, expected):
        assert srp.col_letter(value) == expected

    def test_find_header_column_is_case_and_space_insensitive(self):
        assert (
            srp.find_header_column(["A", " Is This User In Discord? "], "is this user in discord?")
            == 1
        )
        assert srp.find_header_column(["A"], "missing") == -1

    def test_stale_notice_caps_at_five(self):
        assert srp.stale_ids_notice(["A (id 1)"]) == (
            "stale Discord IDs on roster (member likely left the server): A (id 1)"
        )
        seven = [f"M{i} (id {i})" for i in range(7)]
        assert srp.stale_ids_notice(seven).endswith("M4 (id 4) (+2 more)")


class TestResolveColumns:
    def test_defaults_on_the_member_roster(self):
        cols = _cols()
        assert (cols.id_col, cols.name_col, cols.username_col) == (0, 2, 1)
        assert cols.power_col == 5 and cols.power_letter == "F"
        assert cols.same_power_tab is True
        assert cols.power_col_header == "Power"
        assert cols.power_column_missing is False
        assert cols.cross_tab_match_col == 0
        assert cols.presence_col == -1 and cols.not_disc_col == 6
        assert cols.power_tab_for_logging == "Member Roster"

    def test_power_letter_past_the_header_is_missing(self):
        cols = _cols(structured={**STRUCTURED, "power_metric_column": "Z"})
        assert cols.power_column_missing is True
        assert cols.power_col_header == ""

    def test_blank_header_cell_is_missing_too(self):
        cols = _cols(header=HEADER[:5] + ["", "not_on_discord"])
        assert cols.power_column_missing is True

    def test_cross_tab_when_power_tab_differs(self):
        cols = _cols(
            structured={**STRUCTURED, "power_metric_tab": "Squad Powers", "power_match_column": "c"}
        )
        assert cols.same_power_tab is False
        assert cols.power_tab == "Squad Powers"
        assert cols.power_tab_for_logging == "Squad Powers"
        assert cols.cross_tab_match_col == 2
        assert cols.power_column_missing is False  # validated by the overlay instead

    def test_power_tab_equal_to_the_roster_tab_is_same_tab(self):
        cols = _cols(structured={**STRUCTURED, "power_metric_tab": "Member Roster"})
        assert cols.same_power_tab is True

    def test_alias_column_wins_over_display(self):
        cols = _cols(participation={"roster_alias_col": 1})
        assert cols.name_col == 1 and cols.part_alias_col == 1
        assert cols.display_col == 2

    def test_non_int_alias_is_ignored(self):
        cols = _cols(participation={"roster_alias_col": "1"})
        assert cols.name_col == 2 and cols.part_alias_col == -1

    def test_presence_column_and_legacy_spelling(self):
        cols = _cols(header=HEADER[:6] + ["Is this user in Discord?", "not on discord"])
        assert cols.presence_col == 6 and cols.not_disc_col == 7


class TestNotOnDiscord:
    def _cell(self, presence="", legacy=""):
        cells = {6: presence, 7: legacy}
        return lambda idx: cells.get(idx, "")

    def _cols(self):
        return _cols(header=HEADER[:6] + ["Is this user in Discord?", "not on discord"])

    def test_presence_yes_no(self):
        cols = self._cols()
        assert srp.not_on_discord(cols, self._cell("yes"), "", _guild({})) == (False, False)
        assert srp.not_on_discord(cols, self._cell("No"), "123", _guild({123: _member()})) == (
            True,
            False,
        )

    def test_legacy_flag(self):
        cols = self._cols()
        assert srp.not_on_discord(cols, self._cell("", "x"), "123", _guild({123: _member()})) == (
            True,
            False,
        )
        assert srp.not_on_discord(
            cols, self._cell("", "nope"), "123", _guild({123: _member()})
        ) == (False, False)

    def test_inference(self):
        cols = self._cols()
        assert srp.not_on_discord(cols, self._cell(), "", None) == (True, False)
        assert srp.not_on_discord(cols, self._cell(), "TBD", None) == (True, False)
        assert srp.not_on_discord(cols, self._cell(), "123", None) == (False, False)
        assert srp.not_on_discord(cols, self._cell(), "123", _guild({})) == (True, True)
        assert srp.not_on_discord(cols, self._cell(), "123", _guild({123: _member(bot=True)})) == (
            True,
            True,
        )
        assert srp.not_on_discord(cols, self._cell(), "123", _guild({123: _member()})) == (
            False,
            False,
        )


class TestParseMemberRow:
    def _parse(self, row, cols=None, guild=None):
        return srp.parse_member_row(row, cols or _cols(), guild, guild_id=1, event_type="DS")

    def test_full_row(self):
        member, stale = self._parse(["1001", "alice", "Alice", "", "", "412M", ""])
        assert stale is None
        assert member == {
            "key": "1001",
            "name": "Alice",
            "discord_id": "1001",
            "power": 412_000_000,
            "not_on_discord": False,
        }

    def test_empty_row_is_nothing(self):
        assert self._parse(["", "", "", "", "", "9M", ""]) == (None, None)

    def test_short_row_is_padded(self):
        member, _ = self._parse(["1001"])
        assert member["name"] == "1001" and member["power"] is None

    def test_name_only_row_keys_by_name(self):
        member, _ = self._parse(["", "zed", "", "", "", "", ""])
        assert member["key"] == "zed" and member["not_on_discord"] is True

    def test_garbage_power_is_none(self):
        member, _ = self._parse(["1001", "a", "A", "", "", "tbd", ""])
        assert member["power"] is None

    def test_cross_tab_skips_the_power_cell(self):
        cols = _cols(structured={**STRUCTURED, "power_metric_tab": "Powers"})
        member, _ = self._parse(["1001", "a", "A", "", "", "412M", ""], cols=cols)
        assert member["power"] is None

    def test_stale_label(self):
        member, stale = self._parse(["1001", "a", "Alice", "", "", "1M", ""], guild=_guild({}))
        assert member["not_on_discord"] is True
        assert stale == "Alice (id 1001)"

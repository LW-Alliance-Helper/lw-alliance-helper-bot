"""
Tests for storm_member_rules.py (#127).

Covers Sheet I/O (list / save / delete-at-index) and duplicate detection
for both rule types. Slash command handlers + the paginated list view
are integration territory and not unit-tested here.
"""

import pytest
from unittest.mock import patch

import storm_member_rules as smr


class _FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]] | None = None):
        self.title = title
        self._rows = list(rows or [])

    def get_all_records(self):
        if not self._rows:
            return []
        header = self._rows[0]
        return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in self._rows[1:]]

    def get_all_values(self):
        return [list(r) for r in self._rows]

    def append_row(self, row, value_input_option=None):
        self._rows.append([str(c) for c in row])

    def clear(self):
        self._rows = []

    def update(self, range_, values, value_input_option=None):
        self._rows = [list(r) for r in values]

    def delete_rows(self, index, end_index=None):
        """gspread's atomic row delete (1-indexed)."""
        if end_index is None:
            end_index = index
        # Convert 1-indexed inclusive range to 0-indexed slice.
        del self._rows[index - 1:end_index]


class _FakeSpreadsheet:
    def __init__(self):
        self._tabs: dict[str, _FakeWorksheet] = {}

    def worksheet(self, title: str):
        if title not in self._tabs:
            raise Exception("Worksheet not found")
        return self._tabs[title]

    def add_worksheet(self, title: str, rows: int = 0, cols: int = 0):
        ws = _FakeWorksheet(title)
        self._tabs[title] = ws
        return ws


@pytest.fixture
def fake_sheet(seeded_db):
    import config
    from tests.unit.test_config import TEST_GUILD_ID
    fake = _FakeSpreadsheet()
    # Ensure storm rows exist and structured flow is on (so member_rules_tab resolves).
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


class TestSaveAndList:
    def test_save_power_band_round_trip(self, fake_sheet):
        fake, gid = fake_sheet
        ok, _ = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="power_band",
            subject="250000000", value="Power Tower",
        ))
        assert ok is True
        rules = smr.list_rules(gid, "DS")
        assert len(rules) == 1
        assert rules[0].rule_type == "power_band"
        assert rules[0].value     == "Power Tower"
        assert rules[0].subject   == "250000000"

    def test_save_per_member_team(self, fake_sheet):
        fake, gid = fake_sheet
        ok, _ = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="per_member", subject="Alice",
            sub_type="team", value="A",
        ))
        assert ok is True
        rules = smr.list_rules(gid, "DS")
        assert rules[0].subject  == "Alice"
        assert rules[0].sub_type == "team"
        assert rules[0].value    == "A"

    def test_save_per_member_zone(self, fake_sheet):
        fake, gid = fake_sheet
        ok, _ = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="per_member", subject="Charlie",
            sub_type="zone", value="Power Tower",
        ))
        assert ok is True
        rules = smr.list_rules(gid, "DS")
        assert rules[0].sub_type == "zone"
        assert rules[0].value    == "Power Tower"

    def test_save_per_member_special_role(self, fake_sheet):
        fake, gid = fake_sheet
        ok, _ = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="per_member", subject="Bob",
            sub_type="special_role", value="judicator",
        ))
        assert ok is True
        rules = smr.list_rules(gid, "DS")
        assert rules[0].sub_type == "special_role"
        assert rules[0].value    == "judicator"


class TestDuplicateDetection:
    def test_power_band_duplicate_rejected(self, fake_sheet):
        fake, gid = fake_sheet
        smr.save_rule(gid, "DS", smr.Rule(
            rule_type="power_band", subject="250000000", value="Power Tower",
        ))
        ok, msg = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="power_band", subject="250000000", value="Power Tower",
        ))
        assert ok is False
        assert "already exists" in msg.lower()

    def test_power_band_different_zone_accepted(self, fake_sheet):
        fake, gid = fake_sheet
        smr.save_rule(gid, "DS", smr.Rule(
            rule_type="power_band", subject="250000000", value="Power Tower",
        ))
        ok, _ = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="power_band", subject="250000000", value="Nuclear Silo",
        ))
        assert ok is True
        assert len(smr.list_rules(gid, "DS")) == 2

    def test_per_member_duplicate_same_sub_type_rejected(self, fake_sheet):
        fake, gid = fake_sheet
        smr.save_rule(gid, "DS", smr.Rule(
            rule_type="per_member", subject="Alice",
            sub_type="team", value="A",
        ))
        ok, _ = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="per_member", subject="Alice",
            sub_type="team", value="B",  # different value, same key
        ))
        assert ok is False

    def test_per_member_different_sub_type_accepted(self, fake_sheet):
        fake, gid = fake_sheet
        smr.save_rule(gid, "DS", smr.Rule(
            rule_type="per_member", subject="Alice", sub_type="team", value="A",
        ))
        # Same member, different sub_type — accepted.
        ok, _ = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="per_member", subject="Alice", sub_type="zone",
            value="Power Tower",
        ))
        assert ok is True


class TestDeleteAtIndex:
    def test_delete_removes_row(self, fake_sheet):
        fake, gid = fake_sheet
        for i in range(3):
            smr.save_rule(gid, "DS", smr.Rule(
                rule_type="power_band", subject=f"{100 * (i + 1)}000000",
                value=f"Zone {i}",
            ))
        assert smr.delete_rule_at(gid, "DS", 1) is True
        rules = smr.list_rules(gid, "DS")
        assert len(rules) == 2
        # Indexes 0 and 1 in the new list correspond to the old 0 and 2.
        assert rules[0].value == "Zone 0"
        assert rules[1].value == "Zone 2"

    def test_delete_out_of_range_returns_false(self, fake_sheet):
        fake, gid = fake_sheet
        smr.save_rule(gid, "DS", smr.Rule(
            rule_type="power_band", subject="100000000", value="Zone 0",
        ))
        assert smr.delete_rule_at(gid, "DS", 5)  is False
        assert smr.delete_rule_at(gid, "DS", -1) is False
        # Original row still present.
        assert len(smr.list_rules(gid, "DS")) == 1


class TestRenderLabel:
    def test_power_band_label_formats_magnitude(self):
        r = smr.Rule(rule_type="power_band", subject="250000000", value="Power Tower")
        label = r.render_label()
        assert "250M" in label
        assert "Power Tower" in label

    def test_per_member_team_label(self):
        r = smr.Rule(rule_type="per_member", subject="Alice",
                     sub_type="team", value="A")
        assert "Alice" in r.render_label()
        assert "Team A" in r.render_label()

    def test_per_member_zone_label(self):
        r = smr.Rule(rule_type="per_member", subject="Charlie",
                     sub_type="zone", value="Power Tower")
        assert "Charlie" in r.render_label()
        assert "Power Tower" in r.render_label()

    def test_per_member_special_role_label(self):
        r = smr.Rule(rule_type="per_member", subject="Bob",
                     sub_type="special_role", value="judicator")
        label = r.render_label()
        assert "Bob" in label
        assert "Judicator" in label

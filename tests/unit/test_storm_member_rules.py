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


class TestSubjectResolution:
    """#136 — slash commands accept either a `discord.Member` picker or
    a free-text name. `_resolve_subject` enforces exactly one and
    returns (subject_for_storage, display_name)."""

    def test_member_user_returns_discord_id_string(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = 12345
        m.display_name = "Alice"
        m.bot = False
        subject, display = smr._resolve_subject(m, None)
        assert subject == "12345"
        assert display == "Alice"

    def test_name_only_returns_verbatim(self):
        subject, display = smr._resolve_subject(None, "Carol")
        assert subject == "Carol"
        assert display == "Carol"

    def test_name_with_whitespace_stripped(self):
        subject, display = smr._resolve_subject(None, "  Bob  ")
        assert subject == "Bob"
        assert display == "Bob"

    def test_neither_returns_none(self):
        subject, display = smr._resolve_subject(None, None)
        assert subject is None
        assert display == ""

    def test_neither_empty_name_returns_none(self):
        subject, display = smr._resolve_subject(None, "   ")
        assert subject is None
        assert display == ""

    def test_both_provided_rejected(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = 12345
        m.display_name = "Alice"
        m.bot = False
        subject, display = smr._resolve_subject(m, "Bob")
        assert subject is None
        assert display == ""

    def test_bot_member_rejected(self):
        """Discord's Member picker can include bot accounts; a rule
        saved against a bot ID would silently never resolve at apply
        time. Reject the bot at the input layer."""
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = 12345
        m.display_name = "GoodBot"
        m.bot = True
        subject, display = smr._resolve_subject(m, None)
        assert subject is None
        assert display == ""

    def test_typed_name_matches_discord_member_normalizes_to_id(self):
        """If the officer types a name that matches a Discord member's
        display name (case-insensitive), the subject is normalized to
        the Discord ID. Without this, the same person can be referenced
        by both forms — one rule via picker, one via name typo — and
        both fire at apply time."""
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = 12345
        m.display_name = "Alice"
        m.bot = False
        guild = MagicMock()
        guild.members = [m]
        subject, display = smr._resolve_subject(None, "alice", guild=guild)
        # Normalized to the Discord ID; display surfaces the canonical name.
        assert subject == "12345"
        assert display == "Alice"

    def test_typed_name_no_guild_keeps_verbatim(self):
        """Without a guild reference, the normalization is skipped —
        sub-test paths that don't have access to the guild still work."""
        subject, display = smr._resolve_subject(None, "Alice")
        assert subject == "Alice"
        assert display == "Alice"

    def test_typed_name_no_match_keeps_verbatim(self):
        """A name that doesn't match any Discord member stays as a
        free-text non-Discord subject."""
        from unittest.mock import MagicMock
        m = MagicMock()
        m.id = 12345
        m.display_name = "Bob"
        m.bot = False
        guild = MagicMock()
        guild.members = [m]
        subject, display = smr._resolve_subject(None, "Carol", guild=guild)
        assert subject == "Carol"
        assert display == "Carol"

    def test_typed_name_bot_match_keeps_verbatim(self):
        """A name match against a bot is ignored — bots aren't real
        alliance members."""
        from unittest.mock import MagicMock
        bot = MagicMock()
        bot.id = 99
        bot.display_name = "Alice"
        bot.bot = True
        guild = MagicMock()
        guild.members = [bot]
        subject, display = smr._resolve_subject(None, "Alice", guild=guild)
        # Keeps the verbatim name; the bot doesn't claim the subject.
        assert subject == "Alice"
        assert display == "Alice"

    def test_typed_name_ambiguous_match_keeps_verbatim(self):
        """Two Discord members with the same display name → ambiguous;
        leave as free-text so the officer can re-enter via the picker."""
        from unittest.mock import MagicMock
        m1, m2 = MagicMock(), MagicMock()
        m1.id, m2.id = 1, 2
        m1.display_name = m2.display_name = "Alice"
        m1.bot = m2.bot = False
        guild = MagicMock()
        guild.members = [m1, m2]
        subject, display = smr._resolve_subject(None, "Alice", guild=guild)
        assert subject == "Alice"
        assert display == "Alice"


class TestDisplayNameResolution:
    """Discord-ID subjects resolve to the current display name when a
    Guild is available — survives renames between rule creation and
    rendering. Falls back to the raw subject otherwise."""

    def test_resolves_discord_id_to_current_name(self):
        from unittest.mock import MagicMock
        m = MagicMock()
        m.display_name = "Alice (renamed)"
        guild = MagicMock()
        guild.get_member.return_value = m
        rule = smr.Rule(
            rule_type="per_member", subject="12345",
            sub_type="team", value="A",
        )
        label = rule.render_label(guild=guild)
        assert "Alice (renamed)" in label

    def test_falls_back_when_member_not_in_guild(self):
        from unittest.mock import MagicMock
        guild = MagicMock()
        guild.get_member.return_value = None
        rule = smr.Rule(
            rule_type="per_member", subject="12345",
            sub_type="team", value="A",
        )
        label = rule.render_label(guild=guild)
        # The raw ID surfaces — better than a missing-name crash.
        assert "12345" in label

    def test_non_numeric_subject_left_alone(self):
        from unittest.mock import MagicMock
        guild = MagicMock()
        rule = smr.Rule(
            rule_type="per_member", subject="Carol",
            sub_type="zone", value="Power Tower",
        )
        label = rule.render_label(guild=guild)
        assert "Carol" in label
        # Defensive: guild.get_member should NOT be hit for a name (non-
        # digit) subject — keeps the lookup cheap for non-Discord members.
        guild.get_member.assert_not_called()

    def test_no_guild_falls_back_to_raw_subject(self):
        rule = smr.Rule(
            rule_type="per_member", subject="12345",
            sub_type="team", value="A",
        )
        label = rule.render_label()
        assert "12345" in label

    def test_render_label_back_compat_no_kwarg(self):
        """Existing callsites that pass no guild kwarg keep working."""
        rule = smr.Rule(
            rule_type="per_member", subject="Carol",
            sub_type="team", value="A",
        )
        label = rule.render_label()
        assert "Carol" in label


class TestModuleLevelResolveSubjectDisplay:
    """`resolve_subject_display` is the module-level helper that the
    `_list` member filter calls. The audit fix promoted it from
    `Rule._resolve_display_name` so callers don't reach into private
    method names."""

    def test_module_helper_matches_back_compat_method(self):
        from unittest.mock import MagicMock
        member = MagicMock()
        member.display_name = "AliceCurrent"
        guild = MagicMock()
        guild.get_member.return_value = member

        # Both should yield identical results.
        via_method = smr.Rule(
            rule_type="per_member", subject="12345",
            sub_type="team", value="A",
        )._resolve_display_name(guild)
        via_helper = smr.resolve_subject_display("12345", guild)
        assert via_method == via_helper == "AliceCurrent"

    def test_module_helper_blank_subject(self):
        assert smr.resolve_subject_display("", None) == ""
        assert smr.resolve_subject_display(None, None) == ""

    def test_module_helper_non_digit_subject(self):
        from unittest.mock import MagicMock
        guild = MagicMock()
        assert smr.resolve_subject_display("Dave", guild) == "Dave"
        guild.get_member.assert_not_called()


class TestRenameThenFilterEndToEnd:
    """The most novel behaviour from #136: a Discord-ID-keyed rule's
    member renames, and `_list`'s filter should find the rule when
    the officer searches by the NEW name. Audit found no test for
    this end-to-end path."""

    def test_filter_finds_renamed_member_by_current_display_name(
        self, fake_sheet,
    ):
        # The fake_sheet fixture is in this test module — uses gspread
        # mocks to land rules on the Sheet.
        from unittest.mock import MagicMock
        _fake, gid = fake_sheet

        # Save a rule keyed by Discord ID (post-#136 storage convention).
        ok, _ = smr.save_rule(gid, "DS", smr.Rule(
            rule_type="per_member", subject="12345",
            sub_type="team", value="A",
        ))
        assert ok

        # Simulate a rename: the guild's get_member returns a Member
        # whose display_name is "AliceRenamed".
        renamed = MagicMock()
        renamed.display_name = "AliceRenamed"
        guild = MagicMock()
        guild.get_member.return_value = renamed

        # The filter looks up the resolved display name when the raw
        # subject doesn't match. "AliceRenamed" should find the rule.
        rules = smr.list_rules(gid, "DS")
        target = "alicerenamed"
        matched = [
            r for r in rules
            if r.rule_type == smr._RULE_TYPE_PER_MEMBER
            and (
                r.subject.strip().lower() == target
                or smr.resolve_subject_display(r.subject, guild).strip().lower() == target
            )
        ]
        assert len(matched) == 1
        assert matched[0].subject == "12345"


class TestThreeWayResolutionInLoader:
    """`_apply_rules_to_session` previously only matched on display
    name, so every Discord-ID-keyed rule silently failed to pre-assign
    AND falsely warned 'stale rule' in the embed. Now it uses the same
    three-way resolution as `_auto_fill_session`."""

    def test_discord_id_subject_resolves_in_session_open(self):
        """Pre-application via the loader path matches the auto-fill
        path on Discord-ID-keyed rules — no false-positive stale warning."""
        import storm_roster_builder as srb
        import storm_strategy as ss

        members = {
            "1001": {"key": "1001", "name": "Alice", "discord_id": "1001",
                     "power": 412_000_000, "not_on_discord": False},
        }
        preset = ss.PresetBuffer(
            name="Standard", event_type="DS",
            zones=[ss.ZoneRow(zone="Power Tower", max_players=4)],
        )
        rule = smr.Rule(
            rule_type="per_member", subject="1001",  # Discord ID, not name
            sub_type="zone", value="Power Tower",
        )
        session = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="DS",
            team="A", preset=preset, members=members,
            per_member_rules=[rule], power_band_rules=[],
        )
        srb._apply_rules_to_session(session)
        # The rule pre-assigned Alice — and no false-positive stale warning.
        assert "1001" in session.assignments["Power Tower"]
        assert not any(
            "rename or remove" in e or "1001" in e
            for e in session.roster_errors
        )

    def test_discord_id_field_path_resolves(self):
        """Member key is the roster name (non-Discord-style row), but
        the explicit `discord_id` field holds the ID — the rule subject
        matches that field."""
        import storm_roster_builder as srb
        import storm_strategy as ss

        members = {
            "Alice": {"key": "Alice", "name": "Alice", "discord_id": "9999",
                      "power": 412_000_000, "not_on_discord": False},
        }
        preset = ss.PresetBuffer(
            name="Standard", event_type="DS",
            zones=[ss.ZoneRow(zone="Power Tower", max_players=4)],
        )
        rule = smr.Rule(
            rule_type="per_member", subject="9999",
            sub_type="zone", value="Power Tower",
        )
        session = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="DS",
            team="A", preset=preset, members=members,
            per_member_rules=[rule], power_band_rules=[],
        )
        srb._apply_rules_to_session(session)
        assert "Alice" in session.assignments["Power Tower"]


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


class TestRulesListAddRuleButton:
    """#169 (Rule M): the list view surfaces a [➕ Add rule] button
    alongside the per-rule Clear buttons. Empty state shows the same
    button so officers can add their first rule from the list."""

    def test_add_rule_button_present_on_empty_list(self):
        view = smr._RulesListView(
            guild_id=123, user_id=456, event_type="DS", rules=[],
        )
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("Add rule" in lab for lab in labels)

    def test_add_rule_button_present_with_existing_rules(self):
        rules = [
            smr.Rule(rule_type="power_band", subject="250000000", value="Power Tower"),
            smr.Rule(rule_type="per_member", subject="Alice", sub_type="zone", value="Power Tower"),
        ]
        view = smr._RulesListView(
            guild_id=123, user_id=456, event_type="DS", rules=rules,
        )
        labels = [getattr(c, "label", "") for c in view.children]
        # Both Clear buttons + the Add Rule button.
        assert any("Clear 1" in lab for lab in labels)
        assert any("Clear 2" in lab for lab in labels)
        assert any("Add rule" in lab for lab in labels)


class TestAddRuleTypePickerView:
    """The Add-rule choice view branches into the InlinePowerBandView
    (zone-Select + power-modal) for power-band rules, or points the
    officer at the slash command for per-member rules (Discord modals
    can't host a member picker)."""

    def test_renders_choice_buttons_for_ds(self):
        view = smr._AddRuleTypePickerView(event_type="DS", owner_id=1)
        labels = [getattr(c, "label", "") for c in view.children]
        assert any("power-band" in lab.lower() for lab in labels)
        assert any("per-member" in lab.lower() for lab in labels)
        assert any("Cancel" in lab for lab in labels)
        assert view.parent == "desertstorm"

    def test_renders_choice_buttons_for_cs(self):
        view = smr._AddRuleTypePickerView(event_type="CS", owner_id=1)
        assert view.parent == "canyonstorm"


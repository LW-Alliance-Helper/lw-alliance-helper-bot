"""
Tests for storm_strategy_ui.py (#126, split from storm_strategy.py in #371).

Covers the Discord-facing surface: the strategy list view's inline
Create/Edit/Delete affordances, the preset editor's polish details
(stage-mode dropdown, self-describing button labels), the preset
picker's 25-option cap, and the configured-teams resolver the editor
embed uses.

The pure data layer (power parsing, PresetBuffer mutation,
Sheet I/O round-trips, zone-family / phase-aware preset logic) is
covered in test_storm_strategy.py instead.
"""

import pytest
from unittest.mock import patch

import storm_strategy as ss
import storm_strategy_ui as ssui


class TestResolveDsTeams:
    def test_unconfigured_falls_back_to_both(self):
        # Patch the config read to return an empty dict (never-configured
        # alliance shape).
        with patch("config.get_storm_config", return_value={}):
            assert ssui._resolve_ds_teams(123) == "both"

    def test_reads_saved_value(self):
        with patch("config.get_storm_config", return_value={"teams": "A"}):
            assert ssui._resolve_ds_teams(123) == "A"
        with patch("config.get_storm_config", return_value={"teams": "B"}):
            assert ssui._resolve_ds_teams(123) == "B"
        with patch("config.get_storm_config", return_value={"teams": "both"}):
            assert ssui._resolve_ds_teams(123) == "both"

    def test_invalid_value_falls_back_to_both(self):
        with patch("config.get_storm_config", return_value={"teams": "weird"}):
            assert ssui._resolve_ds_teams(123) == "both"

    def test_config_exception_falls_back_to_both(self):
        with patch("config.get_storm_config", side_effect=RuntimeError("db down")):
            assert ssui._resolve_ds_teams(123) == "both"


class TestStrategyListView:
    """#169 / Rule M: `/<parent> strategy list` now ships an inline
    Create / Edit / Delete row alongside the preset summary. Empty state
    surfaces the same row with Edit + Delete disabled."""

    def test_empty_state_enables_only_create(self):
        view = ssui._StrategyListView(owner_id=1, event_type="DS", names=[])
        labels_disabled = {
            getattr(c, "label", ""): getattr(c, "disabled", False) for c in view.children
        }
        # All three buttons render, but Edit + Delete are disabled when
        # no presets exist so the officer can't open an empty Select.
        assert any("Create" in lab for lab in labels_disabled)
        assert any("Edit" in lab for lab in labels_disabled)
        assert any("Delete" in lab for lab in labels_disabled)
        for label, disabled in labels_disabled.items():
            if "Create" in label:
                assert disabled is False
            if "Edit" in label or "Delete" in label:
                assert disabled is True

    def test_populated_state_enables_all_three(self):
        view = ssui._StrategyListView(
            owner_id=1,
            event_type="DS",
            names=["Standard DS"],
        )
        labels_disabled = {
            getattr(c, "label", ""): getattr(c, "disabled", False) for c in view.children
        }
        for label, disabled in labels_disabled.items():
            if any(action in label for action in ("Create", "Edit", "Delete")):
                assert disabled is False


class TestPresetEditorPolish:
    """#174 / Decisions 10 + 13: the editor view drops the [➕ Add zone]
    affordance (zones are game-defined), renames action buttons to be
    self-describing, drops the redundant 'Yes — ' prefix on the phase-
    mode dropdown, and reframes the dirty-state + mode-toggle copy."""

    def test_add_zone_modal_class_removed(self):
        # Decision #13: zones come exclusively from DS_ZONE_STRUCTURE /
        # CS_ZONE_STRUCTURE; alliances can't add their own.
        assert not hasattr(ssui, "_AddZoneModal")

    def test_phase_mode_dropdown_drops_yes_prefix(self):
        """The pre-#174 labels were 'Yes — 2 Phases' / 'Yes — 3 Phases'.
        The 'Yes — ' was redundant once 'Flat (no phases)' became the
        no-phase option."""
        # Build the editor view to inspect its components without going
        # through the slash command path.
        buf = ss.PresetBuffer(name="P", event_type="DS")
        view = ssui._PresetEditorView(guild_id=1, user_id=1, buf=buf)
        phase_selects = [
            c
            for c in view.children
            if isinstance(c, __import__("discord").ui.Select)
            and "Stage mode" in (c.placeholder or "")
        ]
        assert len(phase_selects) == 1
        labels = [opt.label for opt in phase_selects[0].options]
        assert "Flat (no stages)" in labels
        assert "2 Stages" in labels
        assert "3 Stages" in labels
        assert not any("Yes —" in lab for lab in labels)

    def test_action_button_labels_self_describe(self):
        """Decision #13's button-sweep: '✏️ Rename' → '✏️ Rename preset',
        '↩️ Abandon' → '↩️ Abandon this preset' so the button is
        understandable out of context (e.g. on mobile where the embed
        is collapsed)."""
        buf = ss.PresetBuffer(name="P", event_type="DS")
        view = ssui._PresetEditorView(guild_id=1, user_id=1, buf=buf)
        labels = [getattr(c, "label", "") for c in view.children]
        assert "✏️ Rename preset" in labels
        assert "↩️ Abandon this preset" in labels
        # The Add Zone button is gone.
        assert not any("Add zone" in lab for lab in labels)

    def test_unsaved_changes_footer_uses_new_wording(self):
        buf = ss.PresetBuffer(name="P", event_type="DS")
        buf.dirty = True
        embed = ssui._build_editor_embed(buf, teams="both")
        body = embed.description or ""
        assert "Unsaved changes" in body
        # New phrasing: "Save preset to save your changes."; old phrasing
        # ("hit Save Preset to commit") is gone.
        assert "Save preset to save your changes" in body
        assert "to commit" not in body


class TestPresetPickerView:
    """The Edit / Delete buttons open this picker. Capped at 25 options
    (Discord Select limit); the action dictates the downstream handler."""

    def test_picker_lists_sorted_names_case_insensitive(self):
        view = ssui._PresetPickerView(
            owner_id=1,
            event_type="DS",
            names=["zeta", "Alpha", "beta"],
            action="edit",
        )
        # First child is the Select.
        select = view.children[0]
        labels = [opt.label for opt in select.options]
        assert labels == ["Alpha", "beta", "zeta"]

    def test_picker_caps_at_25_options(self):
        names = [f"Preset {i:02d}" for i in range(40)]
        view = ssui._PresetPickerView(
            owner_id=1,
            event_type="DS",
            names=names,
            action="delete",
        )
        select = view.children[0]
        assert len(select.options) == 25

    def test_picker_placeholder_reflects_action(self):
        view = ssui._PresetPickerView(
            owner_id=1,
            event_type="DS",
            names=["X"],
            action="delete",
        )
        select = view.children[0]
        assert "delete" in select.placeholder.lower()

    def test_overflow_notice_empty_when_under_cap(self):
        view = ssui._PresetPickerView(
            owner_id=1,
            event_type="DS",
            names=[f"P{i}" for i in range(10)],
            action="edit",
        )
        # 10 < 25 → no notice.
        assert view.overflow_notice == ""
        assert view.truncated_count == 0

    def test_overflow_notice_at_exactly_25_is_empty(self):
        view = ssui._PresetPickerView(
            owner_id=1,
            event_type="DS",
            names=[f"P{i:02d}" for i in range(25)],
            action="edit",
        )
        # Boundary: 25 fits exactly, no truncation.
        assert view.overflow_notice == ""
        assert view.truncated_count == 0

    def test_overflow_notice_surfaces_count_when_over_cap(self):
        """The picker silently dropped names past 25 before — officers
        searching for an older preset that didn't appear had no signal.
        Notice now surfaces the gap."""
        view = ssui._PresetPickerView(
            owner_id=1,
            event_type="DS",
            names=[f"P{i:02d}" for i in range(40)],
            action="delete",
        )
        notice = view.overflow_notice
        assert notice != ""
        assert "first 25" in notice
        assert "40" in notice  # total count surfaced
        assert view.truncated_count == 15

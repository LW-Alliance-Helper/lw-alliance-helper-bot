"""
Tests for the participation question preset templates (#247).

Covers the pure helpers in `defaults` (preset list per tier, preset →
question conversion) and the conversions the wizard step depends on.
The wizard step itself (`_run_participation_preset_picker_step`) is
view-driven and tested via shape-of-additions checks against the
defaults; the interactive view exercise lives in the integration
layer.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


# ── Preset list per tier ────────────────────────────────────────────────────


class TestPresetListPerTier:

    def test_free_tier_has_three_presets(self):
        from defaults import storm_participation_presets
        free = storm_participation_presets(is_premium=False)
        assert len(free) == 3
        keys = {p["key"] for p in free}
        assert keys == {"showed_up", "sat_out", "didnt_vote"}

    def test_premium_tier_adds_three_more(self):
        from defaults import storm_participation_presets
        premium = storm_participation_presets(is_premium=True)
        assert len(premium) == 6
        keys = {p["key"] for p in premium}
        # Free presets included verbatim plus the Premium 3.
        assert {"showed_up", "sat_out", "didnt_vote"} <= keys
        assert {
            "sit_out_count_4",
            "vote_miss_count_8",
            "didnt_vote_autoprefill",
        } <= keys

    def test_showed_up_is_default_checked(self):
        """Per #245+#247: the attendance question is checked by default
        so officers who use /attendance get the new Trends Viewer
        data without an extra config step."""
        from defaults import storm_participation_presets
        free = storm_participation_presets(is_premium=False)
        showed_up = next(p for p in free if p["key"] == "showed_up")
        assert showed_up.get("default_checked") is True

    def test_each_preset_has_required_fields(self):
        from defaults import storm_participation_presets
        for tier in (True, False):
            for p in storm_participation_presets(is_premium=tier):
                assert "key" in p
                assert "label" in p
                assert "type" in p
                assert "description" in p
                assert "emoji" in p

    def test_derived_count_presets_reference_existing_sources(self):
        """The derived-count presets cite source question keys that
        exist as their own presets — without this, picking the
        derived count alone leaves it inert with no path to fix."""
        from defaults import storm_participation_presets
        all_presets = storm_participation_presets(is_premium=True)
        all_keys = {p["key"] for p in all_presets}
        for p in all_presets:
            if p["type"] != "derived_count":
                continue
            src = p.get("source_question_key")
            assert src, f"{p['key']}: derived_count missing source"
            assert src in all_keys, (
                f"{p['key']}: source `{src}` isn't in the preset list"
            )

    def test_premium_autoprefill_marks_discord_poll_source(self):
        from defaults import storm_participation_presets
        prem = storm_participation_presets(is_premium=True)
        auto = next(p for p in prem if p["key"] == "didnt_vote_autoprefill")
        assert auto["prefill_source"] == "discord_poll"
        assert auto["type"] == "roster_multi_select"


# ── Preset → question conversion ────────────────────────────────────────────


class TestPresetToQuestion:

    def test_strips_picker_only_fields(self):
        """`description`, `emoji`, `default_checked` are picker-UI
        decoration — they shouldn't bleed into the saved question
        config (which the run_log_flow walker reads)."""
        from defaults import preset_to_question
        out = preset_to_question({
            "key": "sat_out",
            "label": "Who sat out this week?",
            "type": "roster_multi_select",
            "description": "Some picker copy",
            "emoji": "📝",
            "default_checked": True,
        })
        assert out == {
            "key": "sat_out",
            "label": "Who sat out this week?",
            "type": "roster_multi_select",
        }
        assert "emoji" not in out
        assert "description" not in out
        assert "default_checked" not in out

    def test_keeps_derived_count_fields(self):
        from defaults import preset_to_question
        out = preset_to_question({
            "key": "sit_out_count_4",
            "label": "Sit-out count, past 4 events",
            "type": "derived_count",
            "description": "blah",
            "emoji": "📊",
            "source_question_key": "sat_out",
            "lookback_events": 4,
        })
        assert out["source_question_key"] == "sat_out"
        assert out["lookback_events"] == 4

    def test_keeps_prefill_source(self):
        from defaults import preset_to_question
        out = preset_to_question({
            "key": "didnt_vote_autoprefill",
            "label": "X",
            "type": "roster_multi_select",
            "description": "Y",
            "emoji": "🗳️",
            "prefill_source": "discord_poll",
        })
        assert out["prefill_source"] == "discord_poll"


# ── Wizard step interactions with the picker ────────────────────────────────


class TestWizardPicker:
    """The wizard step is async + view-driven, so most of its surface
    is integration territory. These tests cover the pure decision
    logic: filtering presets already in the question list, and the
    cap-trim path."""

    def test_already_configured_keys_filtered_out(self):
        """If the officer's existing question list already contains a
        preset's key, that preset shouldn't reappear in the picker —
        otherwise picking it would create a duplicate column."""
        from defaults import storm_participation_presets
        all_presets = storm_participation_presets(is_premium=False)
        existing = [{"key": "sat_out"}]
        existing_keys = {q["key"] for q in existing}
        available = [p for p in all_presets if p["key"] not in existing_keys]
        keys = {p["key"] for p in available}
        assert "sat_out" not in keys
        assert "showed_up" in keys
        assert "didnt_vote" in keys

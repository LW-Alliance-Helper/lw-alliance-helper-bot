"""The storm wizard's four views, clicked.

`tests/integration/test_storm_setup.py` answers each view by attribute, the
way the scripted harness does, so it never runs a button callback. Before
the move to `storm_setup.py` (#611 round 2) the views were defined inline
and could not be constructed on their own. Now they can, and each click is
held to the exact acknowledgement the inline class rendered: the prompt
echoed above the answer for the team and slot pickers, the bare
acknowledgement for the template views, every button disabled, the view
stopped.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import storm_setup  # noqa: E402

SLOTS = ["8:00 PM (00:00 server time)", "10:00 PM (02:00 server time)"]


async def _click(view, index):
    """Run the callback of the button at `index` and return what it edited."""
    edited = {}

    async def _edit(inter, **kwargs):
        edited.update(kwargs)

    with patch("wizard_registry.safe_edit_response", side_effect=_edit):
        await view.children[index].callback(MagicMock())
    return edited


def _labels(view):
    return [c.label for c in view.children]


# ── TeamChoiceView ───────────────────────────────────────────────────────────


class TestTeamChoiceView:
    PROMPT = "**Step 2 of 9: Which teams do you run for Desert Storm?**"

    @pytest.mark.asyncio
    async def test_fresh_setup_has_no_keep_current(self):
        v = storm_setup.TeamChoiceView(
            prompt=self.PROMPT, saved_teams="both", already_configured=False
        )
        assert _labels(v) == ["Team A & Team B", "Team A only", "Team B only"]
        assert v.selected is None and v.timeout == 120

    @pytest.mark.asyncio
    async def test_reentry_keep_current_is_leftmost_and_names_the_saved_teams(self):
        v = storm_setup.TeamChoiceView(prompt=self.PROMPT, saved_teams="B", already_configured=True)
        assert _labels(v)[0] == "Keep current: Team B only"

    @pytest.mark.parametrize(
        "index, selected, ack",
        [
            (0, "both", "✅ Teams: **Team A & Team B**"),
            (1, "A", "✅ Teams: **Team A only**"),
            (2, "B", "✅ Teams: **Team B only**"),
        ],
    )
    @pytest.mark.asyncio
    async def test_each_choice_echoes_the_prompt_above_its_answer(self, index, selected, ack):
        v = storm_setup.TeamChoiceView(
            prompt=self.PROMPT, saved_teams="both", already_configured=False
        )
        edited = await _click(v, index)
        assert v.selected == selected
        assert edited["content"] == f"{self.PROMPT}\n\n{ack}"
        assert all(c.disabled for c in v.children) and v.is_finished()

    @pytest.mark.asyncio
    async def test_keep_current_keeps_the_saved_teams(self):
        v = storm_setup.TeamChoiceView(prompt=self.PROMPT, saved_teams="A", already_configured=True)
        edited = await _click(v, 0)
        assert v.selected == "A"
        assert edited["content"] == f"{self.PROMPT}\n\n✅ Teams: **Team A only** (kept current)"


# ── TeamSlotView ─────────────────────────────────────────────────────────────


class TestTeamSlotView:
    PROMPT = "Which time slot does **Team A** run for Desert Storm?"

    @pytest.mark.asyncio
    async def test_fresh_setup_offers_the_two_slots(self):
        v = storm_setup.TeamSlotView(
            prompt=self.PROMPT, team_letter="A", slot_labels=SLOTS, saved_idx=None
        )
        assert _labels(v) == SLOTS
        assert v.timeout == 180

    @pytest.mark.asyncio
    async def test_reentry_keep_current_names_the_saved_slot(self):
        v = storm_setup.TeamSlotView(
            prompt=self.PROMPT, team_letter="A", slot_labels=SLOTS, saved_idx=2
        )
        assert _labels(v) == [f"Keep current: {SLOTS[1]}", *SLOTS]

    @pytest.mark.parametrize("index, idx", [(0, 1), (1, 2)])
    @pytest.mark.asyncio
    async def test_each_slot_echoes_the_prompt_above_its_answer(self, index, idx):
        v = storm_setup.TeamSlotView(
            prompt=self.PROMPT, team_letter="A", slot_labels=SLOTS, saved_idx=None
        )
        edited = await _click(v, index)
        assert v.selected == idx
        assert edited["content"] == f"{self.PROMPT}\n\n✅ Team A: **{SLOTS[idx - 1]}**"
        assert all(c.disabled for c in v.children) and v.is_finished()

    @pytest.mark.asyncio
    async def test_keep_current_keeps_the_saved_slot(self):
        v = storm_setup.TeamSlotView(
            prompt=self.PROMPT, team_letter="B", slot_labels=SLOTS, saved_idx=1
        )
        edited = await _click(v, 0)
        assert v.selected == 1
        assert edited["content"] == f"{self.PROMPT}\n\n✅ Team B: **{SLOTS[0]}** (kept current)"


# ── TemplateChoiceView ───────────────────────────────────────────────────────


class TestTemplateChoiceView:
    @pytest.mark.asyncio
    async def test_fresh_setup_drops_keep_and_promotes_use_default(self):
        import discord

        v = storm_setup.TemplateChoiceView(team_label="Team A", saved_is_custom=False)
        assert _labels(v) == ["↩️ Use default template", "✏️ Edit template"]
        assert v.children[0].style == discord.ButtonStyle.success
        assert v.timeout == 300 and v.outcome is None

    @pytest.mark.asyncio
    async def test_saved_custom_offers_keep_first(self):
        import discord

        v = storm_setup.TemplateChoiceView(team_label="Team A", saved_is_custom=True)
        assert _labels(v) == [
            "Keep current custom template",
            "↩️ Use default template",
            "✏️ Edit template",
        ]
        assert v.children[1].style == discord.ButtonStyle.secondary

    @pytest.mark.asyncio
    async def test_keep(self):
        v = storm_setup.TemplateChoiceView(team_label="Team A & B", saved_is_custom=True)
        edited = await _click(v, 0)
        assert v.outcome == "keep"
        assert edited["content"] == "✅ Keeping your saved custom template for Team A & B."

    @pytest.mark.asyncio
    async def test_use_default_reverts_when_a_custom_was_saved(self):
        v = storm_setup.TemplateChoiceView(team_label="Team B", saved_is_custom=True)
        edited = await _click(v, 1)
        assert v.outcome == "default"
        assert edited["content"] == "✅ Reverted to default template for Team B."

    @pytest.mark.asyncio
    async def test_use_default_on_fresh_setup(self):
        v = storm_setup.TemplateChoiceView(team_label="Team B", saved_is_custom=False)
        edited = await _click(v, 0)
        assert v.outcome == "default"
        assert edited["content"] == "✅ Using default template for Team B."

    @pytest.mark.asyncio
    async def test_edit_leaves_the_prompt_alone(self):
        v = storm_setup.TemplateChoiceView(team_label="Team A", saved_is_custom=False)
        edited = await _click(v, 1)
        assert v.outcome == "edit"
        assert "content" not in edited
        assert all(c.disabled for c in v.children) and v.is_finished()


# ── SharedTemplateView ───────────────────────────────────────────────────────


class TestSharedTemplateView:
    @pytest.mark.asyncio
    async def test_first_time_has_no_keep_current(self):
        v = storm_setup.SharedTemplateView(saved_share_mode=None)
        assert _labels(v) == ["One template for both teams", "Separate templates per team"]
        assert v.timeout == 120

    @pytest.mark.parametrize(
        "mode, label, ack",
        [
            (
                "shared",
                "Keep current: One shared template",
                "✅ Kept current: **One shared template** for Team A & B",
            ),
            (
                "separate",
                "Keep current: Separate templates",
                "✅ Kept current: **Separate templates** for Team A & Team B",
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_keep_current_names_and_keeps_the_saved_mode(self, mode, label, ack):
        v = storm_setup.SharedTemplateView(saved_share_mode=mode)
        assert _labels(v)[0] == label
        edited = await _click(v, 0)
        assert v.selected == mode
        assert edited["content"] == ack

    @pytest.mark.asyncio
    async def test_shared(self):
        v = storm_setup.SharedTemplateView(saved_share_mode=None)
        edited = await _click(v, 0)
        assert v.selected == "shared"
        assert edited["content"] == "✅ **One shared template** for Team A & B"

    @pytest.mark.asyncio
    async def test_separate(self):
        v = storm_setup.SharedTemplateView(saved_share_mode=None)
        edited = await _click(v, 1)
        assert v.selected == "separate"
        assert edited["content"] == "✅ **Separate templates** for Team A & Team B"
        assert all(c.disabled for c in v.children) and v.is_finished()

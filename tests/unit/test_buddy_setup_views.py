"""The buddy wizard's reliability-source view, clicked.

`tests/integration/test_buddy_setup.py` answers it by attribute; before the
move to `buddy_setup.py` (#611 round 2) the class was inline and could not
be constructed alone. Each button is held to the choice it records and the
label it carries; the modal's submit is held to the values it hands back.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import buddy_setup  # noqa: E402


def _view(**over):
    kw = dict(
        saved_custom=False,
        current_is_default=True,
        default_rel_tab="Member Roster",
        modal_tab="Member Roster",
        modal_col="D",
    )
    kw.update(over)
    return buddy_setup._RelChoiceView(**kw)


def _labels(v):
    return [c.label for c in v.children]


async def _click(button):
    edited = {}

    async def _edit(inter, **kw):
        edited.update(kw)

    with patch("wizard_registry.safe_edit_response", side_effect=_edit):
        await button.callback(MagicMock())
    return edited


class TestRelChoiceView:
    def test_fresh_setup_offers_default_and_custom(self):
        import discord

        v = _view()
        assert _labels(v) == ["✅ Use default", "✏️ Define my own"]
        assert v.children[0].style == discord.ButtonStyle.success
        assert v.timeout == buddy_setup.WIZARD_STEP_TIMEOUT

    def test_saved_custom_adds_keep_first_and_spells_out_the_default(self):
        import discord

        v = _view(saved_custom=True, current_is_default=False, default_rel_tab="Powers")
        assert _labels(v) == ["Keep current", "↩️ Use default (Powers; D)", "✏️ Define my own"]
        assert v.children[1].style == discord.ButtonStyle.secondary

    def test_saved_custom_that_equals_the_default_keeps_the_short_label(self):
        v = _view(saved_custom=True, current_is_default=True)
        assert _labels(v) == ["Keep current", "✅ Use default", "✏️ Define my own"]

    @pytest.mark.asyncio
    async def test_keep_and_default_record_the_choice(self):
        v = _view(saved_custom=True)
        edited = await _click(v.children[0])
        assert v.choice == "keep" and edited == {"view": v}
        assert all(c.disabled for c in v.children) and v.is_finished()
        v = _view()
        await _click(v.children[0])
        assert v.choice == "default"

    @pytest.mark.asyncio
    async def test_define_my_own_opens_the_modal_and_takes_its_values(self):
        v = _view(modal_tab="Scores", modal_col="E")
        inter = MagicMock()
        inter.response = AsyncMock()
        inter.edit_original_response = AsyncMock()

        async def _send_modal(modal):
            assert modal.title == "Reliability Source"
            assert (modal._tab.default, modal._col.default) == ("Scores", "E")
            modal._tab._value = " Sheet2 "
            modal._col._value = "g"
            await modal.on_submit(MagicMock(response=AsyncMock()))

        inter.response.send_modal = AsyncMock(side_effect=_send_modal)
        await v.children[1].callback(inter)
        assert v.choice == "custom" and v.modal_out == ("Sheet2", "G")
        assert all(c.disabled for c in v.children) and v.is_finished()

    @pytest.mark.asyncio
    async def test_a_dismissed_modal_records_no_choice(self):
        v = _view()
        inter = MagicMock()
        inter.response = AsyncMock()
        inter.edit_original_response = AsyncMock()

        async def _send_modal(modal):
            modal.stop()

        inter.response.send_modal = AsyncMock(side_effect=_send_modal)
        await v.children[1].callback(inter)
        assert v.choice is None and v.modal_out is None and v.is_finished()

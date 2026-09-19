"""The birthday wizard's placement view, clicked.

`tests/integration/test_birthday_setup.py` answers it by attribute; before
the move to `birthday_setup.py` (#611 round 2) the class was inline and
could not be constructed alone. Each button is held to the value it sets
and the acknowledgement it renders.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import birthday_setup as bs  # noqa: E402


async def _click(item):
    edited = {}

    async def _edit(inter, **kw):
        edited.update(kw)

    inter = MagicMock()
    inter.response = AsyncMock()
    with patch("wizard_registry.safe_edit_response", side_effect=_edit):
        await item.callback(inter)
    return edited


class TestPlacementView:
    def test_shape(self):
        v = bs.PlacementView()
        assert [c.label for c in v.children] == ["🎂 Birthday only", "↔️ Assign nearby if taken"]
        assert v.selected is None and v.timeout == 120

    @pytest.mark.parametrize(
        "index, value, ack",
        [
            (0, 0, "✅ Placement: **Birthday only**"),
            (1, 1, "✅ Placement: **Assign 1 day before or after if birthday is taken**"),
        ],
    )
    @pytest.mark.asyncio
    async def test_buttons(self, index, value, ack):
        v = bs.PlacementView()
        edited = await _click(v.children[index])
        assert v.selected == value and edited == {"content": ack, "view": v}
        assert all(c.disabled for c in v.children) and v.is_finished()


class TestLetter:
    def test_saved_index_renders_as_a_letter_or_blank(self):
        assert bs._letter(0) == "A" and bs._letter(26) == "AA"
        assert bs._letter(-1) == "" and bs._letter(None) == "" and bs._letter("B") == ""
